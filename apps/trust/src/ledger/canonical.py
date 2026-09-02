"""RFC-8785 JCS canonicalisation and the D16 event hash. Owned by T-011.

D16 fixes one hashing rule for the whole system::

    event_hash = sha256(prev_hash || canonical_json(event))

where ``canonical_json`` is **RFC-8785 JCS** -- object members sorted by their UTF-16 code
units, no insignificant whitespace, ECMAScript number serialisation -- and timestamps are
UTC RFC-3339 at **millisecond** precision. ``commerce_events.idempotency_key`` *is*
``LedgerEvent.event_id``; there is no second identifier.

Nothing outside ``apps/trust/src/ledger/`` may define its own hashing. That is not a style
preference: two implementations of "canonical" JSON that disagree about key ordering, about
``1.0`` versus ``1``, or about whether a ``None`` field is present produce different hashes
for the same event, and the disagreement surfaces as a chain that fails verification with no
tampering anywhere. So the append path, the verifier, the replay reader and the Postgres
round trip all call :func:`canonical_json` here, and callers outside this package import it
rather than re-deriving it (T-060's ``append`` in particular).

Why hand-rolled rather than ``json.dumps(sort_keys=True)``: three of JCS's rules are not
things ``json`` does.

* **Sorting is by UTF-16 code unit, not by code point.** They differ above the BMP: ``"￿"``
  sorts *before* ``"\U0001f600"`` by code point but *after* it by UTF-16 code unit, because
  the emoji encodes to the surrogate pair ``D83D DE00`` and ``D83D < FFFF``. Python's default
  string ordering is by code point, so ``sort_keys=True`` is simply a different order.
* **Numbers use ECMAScript ``Number::toString``.** ``1.0`` serialises as ``1``, and every
  finite double takes its shortest round-tripping form.
* **Non-ASCII characters are literal.** ``ensure_ascii=True`` -- the ``json`` default --
  would emit ``é`` where JCS requires the UTF-8 byte.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

#: The chain's genesis link. Sixty-four ``0`` characters -- a value no SHA-256 digest of any
#: real input will collide with in practice, and one the ``char(64)`` hex CHECK accepts.
GENESIS_HASH = "0" * 64

#: Fields the chain writes *onto* an event. They are excluded from the hashed body, because
#: an event cannot commit to its own digest.
#:
#: ``seq`` is here for a second reason worth stating, since it is the one excluded field that
#: is neither a digest nor derived from one: it is the *position label*, and in Postgres it is
#: a ``bigserial`` the database assigns when the row lands -- strictly after the digest over
#: that row's body has been computed. There is no moment at which it could be hashed. What
#: commits to the position it names is ``prev_hash``: every event carries its predecessor's
#: digest, so the ORDER is sealed even though the label of the order is not. See
#: ``apps/trust/src/events/store.py`` (the in-memory append) for the full argument and
#: ``apps/trust/tests/test_events_hardening.py`` for the tests that pin it.
CHAIN_FIELDS = ("prev_hash", "event_hash", "seq")

#: The LedgerEvent fields carried into the hash, in DESIGN §Interfaces order. Optional ones
#: are omitted entirely when absent rather than hashed as ``null`` -- ``{"a": 1}`` and
#: ``{"a": 1, "store_id": null}`` must not be two different events.
EVENT_FIELDS = ("event_id", "ts", "kind", "auction_id", "store_id", "order_ref", "payload")

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

_RFC3339_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?"
    r"(Z|z|[+-]\d{2}:?\d{2})?$"
)


class CanonicalisationError(ValueError):
    """A value cannot be canonicalised deterministically.

    Raised rather than coerced: a NaN, an infinity or an object of an unsupported type has
    no JCS representation, and silently substituting one would make the chain agree with
    itself while disagreeing with every other reader.
    """


# ---------------------------------------------------------------------------------------
# timestamps
# ---------------------------------------------------------------------------------------


def rfc3339_ms(value: Any) -> str:
    """Normalise ``value`` to UTC RFC-3339 with exactly three fractional digits (D16).

    Accepts a :class:`datetime.datetime` (naive values are read as UTC) or an RFC-3339
    string with any offset and any fractional precision. Sub-millisecond digits are
    **truncated, not rounded**, so a value that has already round-tripped through
    ``timestamptz`` normalises to the same string it did on the way in.

    Args:
        value: the instant to normalise.

    Returns:
        e.g. ``"2026-01-01T00:00:00.000Z"``.

    Raises:
        CanonicalisationError: the value is neither a datetime nor a parseable RFC-3339
            string. A timestamp this function cannot read is a timestamp two readers would
            hash differently.
    """
    if isinstance(value, _dt.datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=_dt.UTC)
        moment = moment.astimezone(_dt.UTC)
        return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"
    if isinstance(value, str):
        match = _RFC3339_RE.match(value.strip())
        if match is None:
            raise CanonicalisationError(f"not an RFC-3339 timestamp: {value!r}")
        year, month, day, hour, minute, second, fraction, offset = match.groups()
        milliseconds = int((fraction or "").ljust(3, "0")[:3] or 0)
        moment = _dt.datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            milliseconds * 1000,
            tzinfo=_dt.UTC,
        )
        if offset and offset not in ("Z", "z"):
            sign = 1 if offset[0] == "+" else -1
            digits = offset[1:].replace(":", "")
            delta = _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            moment -= sign * delta
        return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"
    raise CanonicalisationError(f"cannot normalise {type(value).__name__} as a timestamp")


# ---------------------------------------------------------------------------------------
# JCS
# ---------------------------------------------------------------------------------------


def _reject_lone_surrogate(text: str, role: str) -> None:
    """RFC-8785 §3.2.2.2: a lone surrogate MUST terminate canonicalisation with an error.

    A Python ``str`` can hold an unpaired ``U+D800``-``U+DFFF`` code unit -- ``json.loads``
    produces one from ``"\\ud800"`` -- but UTF-8 cannot encode it, so there are no canonical
    *bytes* to hash. Left alone, that surfaced as a bare ``UnicodeEncodeError`` from
    :func:`_sort_key` or from :func:`canonical_bytes`, a different exception type at each
    door and neither one catchable as :class:`CanonicalisationError`. The RFC's answer is to
    terminate with an error; ours is to terminate with *the* error.
    """
    for character in text:
        if "\ud800" <= character <= "\udfff":
            raise CanonicalisationError(
                f"lone surrogate U+{ord(character):04X} in {role} {text!r}: RFC-8785 "
                f"§3.2.2.2 requires canonicalisation to terminate on unpaired surrogates, "
                f"and UTF-8 cannot encode one, so this value has no canonical bytes."
            )


def _serialise_string(value: str) -> str:
    out = ['"']
    for character in value:
        escape = _ESCAPES.get(character)
        if escape is not None:
            out.append(escape)
        elif character < " ":
            out.append(f"\\u{ord(character):04x}")
        elif "\ud800" <= character <= "\udfff":
            _reject_lone_surrogate(value, "string")
        else:
            out.append(character)
    out.append('"')
    return "".join(out)


def is_representable_as_double(value: int) -> bool:
    """RFC-8785 §3.1: a JSON number MUST be expressible as an IEEE-754 double.

    The predicate is ``float(v) == v`` and nothing else. A safe-integer magnitude bound is
    the wrong test in *both* directions, and this module used to be wrong in one of them: it
    refused ``10**16``, ``2**53 + 2``, ``2**63`` and ``2**64``, every one of which is an
    exact double with an unambiguous ES6 rendering. The cost was not theoretical -- a bid
    carrying ``{"quantity": 10000000000000000}`` signed and verified through the contracts
    canonicaliser and then could not be recorded, because :func:`compute_event_hash` raised
    and the append failed. A valid, unrecordable event is a hole in the provenance chain.

    The bound is wrong in the other direction too, and that half stays refused:
    ``2**53 + 1`` is *not* a double, so serialising it means serialising a different number.
    """
    try:
        return float(value) == value
    except OverflowError:
        # `10**400` has no double at all -- `float()` raises rather than returning `inf`.
        # Not representable, therefore not canonicalisable; the caller raises.
        return False


def _es6_number(value: float) -> str:
    """ECMAScript ``Number::toString`` (ECMA-262 §6.1.6.1.20), which is what JCS mandates.

    ``repr`` is NOT this function and the difference is not cosmetic. Python switches to
    exponential notation below ``1e-4``; ECMAScript switches below ``1e-7``. So ``1e-05``
    is what ``repr`` gives and ``"0.00001"`` is what RFC-8785 requires, and a payload
    holding ``{"rate": 1e-5}`` hashed the ``repr`` way makes every conforming Go, Rust or
    JavaScript verifier report tampering where there is none -- the exact interoperability
    failure D16 names as its reason for existing.

    The algorithm below is the spec's, on the shortest round-tripping decimal digits
    (``repr`` does give us those): with ``x = digits x 10**(n - k)`` where ``k`` is the digit
    count, the four cases are integral, fractional, leading-zero, and exponential.
    """
    if value == 0:  # covers -0.0, which ECMAScript renders as "0"
        return "0"
    if value < 0:
        return "-" + _es6_number(-value)

    digits_tuple, exponent = Decimal(repr(value)).normalize().as_tuple()[1:]
    digits = "".join(str(digit) for digit in digits_tuple)
    k = len(digits)
    n = k + int(exponent)

    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return f"{digits[:n]}.{digits[n:]}"
    if -6 < n <= 0:
        return f"0.{'0' * -n}{digits}"
    mantissa = digits if k == 1 else f"{digits[0]}.{digits[1:]}"
    power = n - 1
    return f"{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"


def _serialise_number(value: float | int) -> str:
    """RFC-8785 §3.2.2.2: a JSON number is an IEEE-754 double, rendered by ES6.

    ``bool`` is an ``int`` subclass in Python, so :func:`canonical_json` must -- and does --
    dispatch ``True``/``False`` before it reaches here.
    """
    if isinstance(value, int):
        if not is_representable_as_double(value):
            # `10**400` has 401 digits; an error message is not the place for all of them.
            digits = str(value)
            shown = digits if len(digits) <= 32 else f"{digits[:24]}...({len(digits)} digits)"
            raise CanonicalisationError(
                f"{shown} is not expressible as an IEEE-754 double, so it has no canonical "
                f"JSON form: RFC-8785 §3.1 defines a JSON number as a double, and this "
                f"value would hash one way as a Python int and another way once any JSON "
                f"parser (or Postgres `jsonb`) has read it back as a float. Rounding it to "
                f"the nearest double would commit the chain to a number the event does not "
                f"state. Carry it as a string."
            )
        # ES6 renders the *double*, not the Python int, and above 2**53 those differ:
        # `2**64` is an exact double whose shortest round-tripping form is
        # "18446744073709552000", which is what JavaScript's `String(2**64)` gives and what
        # every conforming JCS implementation hashes. `str(value)` would emit the exact
        # decimal expansion instead and disagree with all of them.
        return _es6_number(float(value))
    if math.isnan(value) or math.isinf(value):
        raise CanonicalisationError(f"{value!r} has no JSON representation")
    return _es6_number(value)


def _sort_key(name: str) -> tuple[int, ...]:
    """RFC-8785 §3.2.3: object members sort by their UTF-16 code units."""
    try:
        encoded = name.encode("utf-16-be")
    except UnicodeEncodeError:
        # Only a lone surrogate reaches here; re-raise it as this module's own error so a
        # caller's `except CanonicalisationError` covers keys as well as values.
        _reject_lone_surrogate(name, "object key")
        raise  # unreachable: `_reject_lone_surrogate` always raises on this input
    return tuple(int.from_bytes(encoded[i : i + 2], "big") for i in range(0, len(encoded), 2))


def canonical_json(value: Any) -> str:
    """Serialise ``value`` to its RFC-8785 JCS form.

    Args:
        value: any JSON-compatible tree -- mappings, sequences, strings, numbers, booleans
            and ``None``. ``datetime`` values are normalised with :func:`rfc3339_ms` first,
            so a caller never has to remember to do it.

    Returns:
        The canonical string. Two structurally equal trees always produce the same string,
        whatever order their mappings were built in.

    Raises:
        CanonicalisationError: on a non-finite float, an integer that is not exactly an
            IEEE-754 double (RFC-8785 §3.1), a lone surrogate in any key or string
            (§3.2.2.2), a non-string mapping key, or a value of a type JSON cannot carry.
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _serialise_string(value)
    if isinstance(value, _dt.datetime):
        return _serialise_string(rfc3339_ms(value))
    if isinstance(value, int | float):
        return _serialise_number(value)
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalisationError(f"object keys must be strings, got {key!r}")
        members = []
        for key in sorted(value.keys(), key=_sort_key):
            members.append(f"{_serialise_string(key)}:{canonical_json(value[key])}")
        return "{" + ",".join(members) + "}"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    raise CanonicalisationError(f"cannot canonicalise {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    """:func:`canonical_json` as UTF-8 -- the exact bytes that are hashed."""
    return canonical_json(value).encode("utf-8")


# ---------------------------------------------------------------------------------------
# the event body and its hash
# ---------------------------------------------------------------------------------------


def canonical_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """The hashed body of a ``LedgerEvent``: normalised, chain fields removed.

    Three normalisations, each of which exists because the alternative makes a
    database round trip change the hash:

    * ``ts`` is forced to UTC RFC-3339 milliseconds (D16), so the value that comes back out
      of a ``timestamptz`` column hashes to the string that went in;
    * the chain's own fields (:data:`CHAIN_FIELDS`) are dropped -- an event cannot commit to
      its own digest;
    * optional fields that are absent *or* ``None`` are omitted entirely, so a reader that
      materialises ``store_id`` as SQL ``NULL`` agrees with a writer that never set it.

    Any field not in :data:`EVENT_FIELDS` is carried through untouched, so an extended event
    shape still hashes over everything it carries.
    """
    body: dict[str, Any] = {}
    for name, value in event.items():
        if name in CHAIN_FIELDS or value is None:
            continue
        body[name] = rfc3339_ms(value) if name == "ts" else value
    missing = [name for name in ("event_id", "kind") if name not in body]
    if missing:
        raise CanonicalisationError(f"LedgerEvent is missing required field(s): {missing}")
    # `payload` is a required LedgerEvent field (DESIGN §Interfaces) and the column has
    # `DEFAULT '{}'`, so an event written without one comes back out of Postgres carrying
    # `{}`. Defaulting it here is what makes those two the same event: without it the write
    # hashed a body with no `payload` key while the stored row hashed one with an empty
    # object, and the writer's own round-trip guard then rejected the append -- blaming
    # float renormalisation, which had nothing to do with it.
    body.setdefault("payload", {})
    return body


def compute_event_hash(prev_hash: str, event: Mapping[str, Any]) -> str:
    """``sha256(prev_hash || canonical_json(event))`` -- D16, and the only definition of it.

    Args:
        prev_hash: the previous link, or :data:`GENESIS_HASH` for the first event.
        event: the ``LedgerEvent``; chain fields on it are ignored (see
            :func:`canonical_event`), so re-hashing an already-sealed event is safe.

    Returns:
        The 64-character lowercase hex digest.
    """
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(canonical_bytes(canonical_event(event)))
    return digest.hexdigest()
