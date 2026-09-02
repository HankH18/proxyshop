"""The external door's signing envelope, and the one canonical form the signature covers (D52).

Two separate things live here and it matters that they stay separate:

**The envelope is a property of a SUBMISSION, not of a `Bid`.** `signer_id`, `key_id`,
`issued_at` and `nonce` are required on every bid that crosses the public boundary at
`POST /v1/auctions/{auction_id}/bids`, alongside the `schema_version` a `Bid` already carries —
five required fields in all. The four identity fields are *not* on `Bid`: a hosted Tier-1 agent
answering `POST /v1/bid-requests` never crosses that boundary and holds no key, so requiring them
on `Bid` itself would demand a signature from something that has nothing to sign with. The
submission shape is `SignedBidSubmission` — `Bid` ∪ `SigningEnvelope`, all five required, no
optional-field mode and no legacy-tolerant variant.

**The canonical bytes are defined once, here, and called by both sides.** `sign_bid` and
`receive_bid` (T-044, `packages.store_agent.src.external`) must produce and check the *same*
bytes; two implementations of a canonicalizer is two protocols. This module owns the single
implementation and T-044 re-exports it, because `packages.store_agent.src.external` is the import
path the frozen suite binds to and physically relocating those functions voids the freeze.

The signed input covers `auction_id`, `signer_id`, `store_id`, `issued_at`, `nonce`, `key_id`,
`schema_version` and `payload_hash(payload)`. Changing any one of them changes the bytes, so a
signature cannot be lifted onto a different auction, signer, store, instant, key or body.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from contracts.protocol import SignedBidSubmission, SigningEnvelope

#: D52: the five fields required on every external submission. Order is the DESIGN order.
REQUIRED_SIGNING_FIELDS: tuple[str, ...] = (
    "signer_id",
    "key_id",
    "issued_at",
    "nonce",
    "schema_version",
)

#: The fields the signature covers, in the order DESIGN §Interfaces lists them. `payload_hash`
#: is computed, not read off the payload.
SIGNED_FIELDS: tuple[str, ...] = (
    "auction_id",
    "signer_id",
    "store_id",
    "issued_at",
    "nonce",
    "key_id",
    "schema_version",
)

#: Keys excluded from the body digest: the envelope rides in the canonical bytes in its own
#: right, and `signature` cannot cover itself.
_NON_BODY_KEYS: frozenset[str] = frozenset(
    {"signer_id", "key_id", "issued_at", "nonce", "signature"}
)

#: Prefix on `payload_hash` output, so a digest is self-describing if the algorithm ever moves.
PAYLOAD_HASH_ALGORITHM = "sha256"

#: Every code point the envelope's blank rule treats as empty. The SAME set
#: `schemas/protocol.schema.json` enumerates in the five envelope fields' `pattern`.
#:
#: Enumerated rather than delegated to `str.strip()`, and enumerated in the schema rather than
#: spelled `\s`, for one reason: `\s` and `strip()` are engine-dependent. Rust's regex crate
#: (what pydantic compiles) and Python's `re` read `\s` as Unicode White_Space, which includes
#: U+0085; ECMAScript (what Ajv compiles) reads it as a fixed list that excludes U+0085 and
#: includes U+FEFF. `str.strip()` follows `str.isspace()`, which is Unicode White_Space plus
#: U+001C-U+001F; `String.prototype.trim()` follows ECMAScript. Measured, the split was exactly
#: two characters — U+0085 blank in Python only, U+FEFF blank in TypeScript only — so the ONE
#: artifact both languages read described two different rules.
#:
#: This set is the UNION of both readings. A union relaxes neither gate (T-108's non-goal): every
#: character either side already called blank is still blank, and the two disputed characters are
#: now blank on both sides. `tests/test_signing_envelope.py` walks all of Unicode and fails if
#: this set and the compiled schema pattern ever disagree; `src/ts/signing.ts` carries the twin.
BLANK_CODE_POINTS: frozenset[int] = frozenset(
    {
        *range(0x09, 0x0E),  # tab, LF, VT, FF, CR
        *range(0x1C, 0x21),  # the four information separators, and SPACE
        0x85,  # NEL — Unicode White_Space, but not ECMAScript whitespace
        0xA0,  # NBSP
        0x1680,  # OGHAM SPACE MARK
        *range(0x2000, 0x200B),  # EN QUAD … HAIR SPACE
        0x2028,  # LINE SEPARATOR
        0x2029,  # PARAGRAPH SEPARATOR
        0x202F,  # NARROW NO-BREAK SPACE
        0x205F,  # MEDIUM MATHEMATICAL SPACE
        0x3000,  # IDEOGRAPHIC SPACE
        0xFEFF,  # ZWNBSP — ECMAScript whitespace, but not Unicode White_Space
    }
)


def is_blank(value: str) -> bool:
    """True when `value` holds no character the envelope's schema `pattern` would accept.

    The Python half of one rule stated in three places that must not drift: this function, the
    schema's `pattern`, and `isBlank` in `src/ts/signing.ts`. `str.strip()` is NOT this rule —
    it treats U+FEFF as content, which the schema and the TypeScript peer do not.

    An empty string is blank, which is what the caller wants: `minLength: 1` and this function
    then agree that `""` is missing rather than disagreeing about a value with no characters.
    """
    return all(ord(char) in BLANK_CODE_POINTS for char in value)


class CanonicalisationError(ValueError, TypeError):
    """The one error a caller has to catch around the canonicalizer.

    Before this existed the module raised `TypeError`, `ValueError`, `AttributeError` and
    `OverflowError` depending on which way the input was wrong — including a bare
    `AttributeError: 'int' object has no attribute 'encode'` for a non-string key, which is a
    latent 500 at a public door. A caller writing `except SomeError` around `canonical_json`
    missed four of the five failure modes.

    It derives from BOTH `ValueError` and `TypeError` so that existing callers (and tests) which
    catch either keep working: narrowing a public exception type is a breaking change, and there
    is no version of this where a caller's `except` clause silently stops matching.
    """


def _representable_as_double(value: int) -> bool:
    """RFC 8785 §3.1: a signable JSON number MUST be expressible as an IEEE-754 double.

    The predicate is `float(v) == v` and nothing else. A safe-integer bound (`abs(v) <= 2**53-1`)
    is the wrong test in BOTH directions: it rejects `10**16` and `2**63`, which are exact
    doubles, and it admits nothing above the bound at all — the old code did not reject those, it
    silently COERCED them, so `{"quantity": 9007199254740993}` signed bytes covering
    `…992`. The signature verified and the exchange acted on a quantity the signature did not
    bind.
    """
    try:
        return float(value) == value
    except OverflowError:
        # `10**400` and friends: too large for a double at all, so not expressible, so not
        # signable. An exception here is the answer, not an error to propagate.
        return False


def _ecmascript_number(value: float) -> str:
    """Serialize a number exactly as ECMAScript's `Number::toString` does (ES2023 6.1.6.1.20).

    RFC 8785 §3.2.2.3 defines canonical JSON numbers as ECMAScript's, and Python's `repr` is NOT
    that. Both produce the shortest round-tripping digit string, but they switch to exponential
    notation at different magnitudes, so the two sides disagree on a real range of real prices::

        1e-6   Python "1e-06"                  ECMAScript "0.000001"
        1e21   Python "1e+21"                  ECMAScript "1e+21"
        1e16   Python "1e+16"                  ECMAScript "10000000000000000"

    `Offer.unit_price` is an unconstrained `number` and `Claim.value` is unconstrained entirely,
    so those magnitudes are reachable from a legal bid — not a theoretical concern. Implementing
    the spec'd algorithm is the only way the two canonicalizers stay one protocol.
    """
    if value != value or value in (float("inf"), float("-inf")):
        raise CanonicalisationError("canonical_json: non-finite numbers are not signable")
    if value == 0:
        return "0"  # ECMAScript prints -0 as "0"
    if value < 0:
        return "-" + _ecmascript_number(-value)

    # Decompose the shortest round-trip form into `digits × 10**(n - k)`, the (s, k, n) of the
    # spec: `k` digits, value = 0.digits × 10**n.
    text = repr(float(value))
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    integer_part, _, fraction_part = mantissa.partition(".")
    raw = integer_part + fraction_part
    stripped = raw.lstrip("0")
    leading_zeros = len(raw) - len(stripped)
    n = len(integer_part) + exponent - leading_zeros
    digits = stripped.rstrip("0") or "0"
    k = len(digits)

    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    sign = "+" if n - 1 > 0 else "-"
    head = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{head}e{sign}{abs(n - 1)}"


def _reject_lone_surrogates(text: str, what: str) -> None:
    """RFC 8785 §3.2.2.2: a lone surrogate MUST terminate canonicalisation with an error.

    In Python every code point in U+D800–U+DFFF inside a `str` IS a lone surrogate — a real
    astral character is one code point at or above U+10000 — so the test is that simple.

    This was a cross-language MISMATCH, and TypeScript was wrong in the more dangerous
    direction: `JSON.stringify("\ud800")` emits `"\ud800"` as escaped hex and SUCCEEDS, while
    Python's `.encode("utf-8")` raised `UnicodeEncodeError` — so a Node seller could sign a bid
    whose `message` or `Claim.value` held a lone surrogate (`Claim.value` is unconstrained, so it
    is trivially reachable) and the Python exchange would raise at the public door instead of
    returning a rejection. §3.2.2.2 says BOTH were wrong: the answer is a typed error on both
    sides, which is what this is.
    """
    for index, char in enumerate(text):
        if 0xD800 <= ord(char) <= 0xDFFF:
            raise CanonicalisationError(
                f"canonical_json cannot sign {what} containing a lone surrogate "
                f"(U+{ord(char):04X} at index {index}): RFC 8785 §3.2.2.2 requires a compliant "
                "implementation to terminate with an error rather than emit bytes for it"
            )


def _utf16_key(key: str) -> bytes:
    """Sort key reproducing JavaScript's string ordering.

    RFC 8785 §3.2.3 orders object members by UTF-16 code unit, which is what JavaScript's `<`
    compares. Python's `<` compares CODE POINTS, and the two disagree for anything above the BMP:
    `"😀" < ""` is true by code point and false by code unit. Encoding to UTF-16-BE and
    comparing bytes reproduces the JavaScript order exactly.
    """
    # `surrogatepass` only so this helper cannot itself raise a bare `UnicodeEncodeError`;
    # `_reject_lone_surrogates` has already refused any key that would need it.
    return key.encode("utf-16-be", errors="surrogatepass")


def canonical_json(value: Any) -> str:
    """RFC-8785 canonical JSON: UTF-16-ordered keys, ECMAScript numbers, no insignificant space.

    Deterministic for a given *content*, which is the only property the signature needs:
    reordering a mapping, round-tripping the payload through JSON, or writing `89` where the
    other side wrote `89.0` must not change the bytes. Written out by hand rather than delegated
    to `json.dumps(sort_keys=True)`, because `json.dumps` gets both the key order and the number
    format subtly wrong for a signature's purposes — see `_ecmascript_number` and `_utf16_key`.

    Scoped to signing. The ledger's hash chain has its own canonicalizer in
    `apps/trust/src/ledger` (D16) and neither borrows the other's.
    """
    out: list[str] = []
    _write_canonical(value, out)
    return "".join(out)


def _write_canonical(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        _reject_lone_surrogates(value, "a string")
        # `ensure_ascii=False` so non-ASCII stays literal, matching `JSON.stringify`.
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, int):
        # RFC 8785 §3.1: the number MUST be expressible as an IEEE-754 double. If it is not, the
        # other side reads a DIFFERENT value out of the same bytes, so there is no honest way to
        # sign it — refusing is the only correct answer, and coercing (what this used to do) is
        # the dangerous one: it produced bytes covering a quantity the seller never wrote.
        if not _representable_as_double(value):
            raise CanonicalisationError(
                f"canonical_json cannot sign the integer {value}: RFC 8785 §3.1 requires a JSON "
                "number to be expressible as an IEEE-754 double, and this one is not. Signing "
                "the nearest double would cover a value the submission does not state."
            )
        out.append(_ecmascript_number(float(value)))
    elif isinstance(value, float):
        out.append(_ecmascript_number(value))
    elif isinstance(value, Mapping):
        # A non-string key is not JSON. Sorting one used to raise `AttributeError: 'int' object
        # has no attribute 'encode'` out of `_utf16_key` — a latent 500 rather than a refusal.
        for key in value:
            if not isinstance(key, str):
                raise CanonicalisationError(
                    f"canonical_json cannot sign a {type(key).__name__} key ({key!r}); JSON "
                    "object members are strings, and coercing one would let two different "
                    "payloads sign identically"
                )
            _reject_lone_surrogates(key, "an object key")
        out.append("{")
        for index, key in enumerate(sorted(value, key=_utf16_key)):
            if index:
                out.append(",")
            out.append(json.dumps(str(key), ensure_ascii=False))
            out.append(":")
            _write_canonical(value[key], out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write_canonical(item, out)
        out.append("]")
    else:
        raise CanonicalisationError(
            f"canonical_json cannot sign a {type(value).__name__}; a signed payload must be "
            "plain JSON so both sides can reproduce the bytes from the wire form alone"
        )


def payload_hash(payload: Mapping[str, Any]) -> str:
    """A digest over the bid BODY — everything except the envelope and the signature itself.

    Stable across a JSON round trip and across mapping insertion order, and it changes whenever
    any part of the offer or the claims changes. That is what makes the signature cover the
    thing the seller is actually promising, rather than only the metadata around it.
    """
    if not isinstance(payload, Mapping):
        raise CanonicalisationError(f"payload_hash expects a mapping, got {type(payload).__name__}")
    body = {key: value for key, value in payload.items() if key not in _NON_BODY_KEYS}
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return f"{PAYLOAD_HASH_ALGORITHM}:{digest}"


def missing_signing_fields(payload: Mapping[str, Any]) -> list[str]:
    """Which of the five required envelope fields are absent or empty. Empty list means complete.

    Required BY TYPE, not merely by presence. All five are `string` in the schema, and a check
    that only asked "is it None or blank?" reported `nonce: 0`, `nonce: false`, `signer_id: []`
    and `issued_at: 12345` as present — every one of which `SigningEnvelope` rejects. `nonce:
    false` is a constant nonce, which is exactly what D52's replay defence exists to make
    impossible, and `canonical_signing_bytes` calls this function rather than `envelope_of`, so
    the loose gate was the only one on the path.

    "Empty" is `is_blank`, not `str.strip()`. `strip()` is Python's own idea of whitespace and
    the schema's `pattern` is the shared one; they differed on U+FEFF, so a value the schema
    called blank was one this function called present. Both now read `BLANK_CODE_POINTS`.
    """
    if not isinstance(payload, Mapping):
        return list(REQUIRED_SIGNING_FIELDS)
    missing = []
    for field in REQUIRED_SIGNING_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or is_blank(value):
            missing.append(field)
    return missing


def canonical_signing_bytes(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes a submission's `signature` covers.

    Raises `CanonicalisationError` when the envelope is incomplete: refusing to produce signing
    input for a submission that cannot legally exist is better than producing bytes nobody can
    verify.
    """
    if not isinstance(payload, Mapping):
        raise CanonicalisationError(
            f"canonical_signing_bytes expects a mapping, got {type(payload).__name__}"
        )

    missing = missing_signing_fields(payload)
    if missing:
        raise CanonicalisationError(
            "cannot canonicalize a submission with an incomplete signing envelope; "
            f"missing {missing} (D52: all of {list(REQUIRED_SIGNING_FIELDS)} are required)"
        )
    if payload.get("auction_id") in (None, "") or payload.get("store_id") in (None, ""):
        raise CanonicalisationError(
            "cannot canonicalize a submission without auction_id and store_id — both are "
            "covered fields, so a signature that omitted them could be lifted across auctions"
        )

    covered: dict[str, Any] = {field: payload.get(field) for field in SIGNED_FIELDS}
    covered["payload_hash"] = payload_hash(payload)
    return canonical_json(covered).encode("utf-8")


def envelope_of(payload: Mapping[str, Any]) -> SigningEnvelope:
    """Lift the five envelope fields out of a submission. Raises if any is missing."""
    return SigningEnvelope.model_validate(
        {field: payload.get(field) for field in REQUIRED_SIGNING_FIELDS}
    )


def keyring_secret(
    keyring: Mapping[str, Mapping[str, str]], signer_id: str, key_id: str
) -> str | None:
    """Look up `(signer_id, key_id)` in the D52 keyring. `None` when the pair is unknown.

    The keyring is nested `{signer_id: {key_id: secret}}` and the OUTER key is the signer, not
    the store. Two signers may legitimately use the same `key_id` string, so a lookup keyed on
    `key_id` alone is wrong; and an unknown pair never falls back to another key of that signer,
    because falling back is what makes a revoked key still work.

    Both ids must be strings, and both lookups are guarded. `signer_id` and `key_id` arrive off
    the wire, and `{"signer_id": {"a": 1}}` makes `dict.get` raise `TypeError: unhashable type`
    — an uncaught 500 from an unauthenticated caller where a rejection belongs. `boundary.py`
    already guards this exact hazard on `store_id`; the reasoning carries here unchanged.

    A stored secret must be a non-empty string. A row seeded with `""` — a placeholder, a
    truncated secret, a key cleared but not deleted — is NOT a usable HMAC key, and returning it
    would make "no such key" and "this key" the same answer.
    """
    if not isinstance(keyring, Mapping):
        return None
    if not isinstance(signer_id, str) or not isinstance(key_id, str):
        return None
    try:
        signer_keys = keyring.get(signer_id)
    except TypeError:  # a keyring whose own keys refuse this lookup is simply not a match
        return None
    if not isinstance(signer_keys, Mapping):
        return None
    try:
        secret = signer_keys.get(key_id)
    except TypeError:
        return None
    return secret if isinstance(secret, str) and secret else None


def signing_envelope_errors(payload: Mapping[str, Any]) -> Sequence[str]:
    """Human-readable reasons a submission's envelope is unacceptable. Empty means acceptable.

    A non-mapping is not an acceptable envelope — it is five errors. Returning `[]` for one would
    mean "acceptable", and a caller trusting the empty list would admit a bare string or a JSON
    array as a signed submission.
    """
    missing = missing_signing_fields(payload)
    return [f"missing required signing field: {field}" for field in missing]


__all__ = [
    "BLANK_CODE_POINTS",
    "PAYLOAD_HASH_ALGORITHM",
    "CanonicalisationError",
    "REQUIRED_SIGNING_FIELDS",
    "SIGNED_FIELDS",
    "SignedBidSubmission",
    "SigningEnvelope",
    "canonical_json",
    "canonical_signing_bytes",
    "envelope_of",
    "is_blank",
    "keyring_secret",
    "missing_signing_fields",
    "payload_hash",
    "signing_envelope_errors",
]
