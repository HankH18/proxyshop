"""Strip buyer identity out of anything the network pushes to a store agent.

R13/R5. The store agent is a *counterparty*. It has a legitimate need to know what its trust
score did and which of its own events moved it, and no need at all to know who the buyer was.
The proxy layer is the whole product: a store that can re-identify buyers from trust pushes
has been handed the customer list the network exists to keep.

Why the keys go, not just the values
------------------------------------
Nulling ``buyer_email`` and leaving the key is not a scrub — it is a labelled empty box that
tells the recipient exactly what to correlate against, and it survives every future writer who
"helpfully" repopulates the field it finds. So :func:`scrub` removes the KEY, recursively, at
every depth, and separately redacts values that *look* like identity (an e-mail address
sitting in a free-text note) wherever they survive under an innocent key name.

Why this is a denylist and not an allowlist
-------------------------------------------
The pushed event is the store's OWN event, echoed back so it can act on it — which means its
payload shape is whatever the rest of the system writes, and an allowlist here would silently
drop the field a future ticket adds and the store agent needs. The denylist is therefore
deliberately conservative on the identity side (substring matches on ``email``/``address``/
``account``/``phone``, exact matches on the ``*name`` family so ``canonical_name`` survives)
and is asserted directly by ``apps/trust/tests/test_feedback_push.py``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "IDENTITY_KEY_SUBSTRINGS",
    "IDENTITY_KEYS",
    "REDACTED",
    "scrub",
    "scrub_report",
]

#: The replacement left where a *value* had to go but its key is legitimate.
REDACTED = "[redacted]"

#: Exact key names that carry buyer identity. Exact, not substring, because the ``name``
#: family collides with plenty of innocent fields — ``canonical_name``, ``display_name``,
#: ``store_name`` — that a store agent genuinely needs.
IDENTITY_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "buyer",
        "buyer_name",
        "buyer_id",
        "buyer_ref",
        "customer",
        "customer_id",
        "customer_name",
        "first_name",
        "last_name",
        "full_name",
        "recipient",
        "recipient_name",
        "shopper",
        "shopper_name",
        "user_id",
        "ssn",
        "dob",
        "date_of_birth",
        "ip",
        "user_agent",
    }
)

#: Key-name substrings that carry buyer identity wherever they appear. Nothing a store agent
#: needs contains any of these; everything that does is contact or financial identity.
IDENTITY_KEY_SUBSTRINGS: tuple[str, ...] = (
    "email",
    "address",
    "account_id",
    "phone",
    "telephone",
    "postcode",
    "postal_code",
    "zip_code",
    "credit_card",
    "card_number",
    "ip_address",
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LONG_DIGITS = re.compile(r"\b\d{9,}\b")


def _is_identity_key(key: object) -> bool:
    name = str(key).strip().lower()
    if name in IDENTITY_KEYS:
        return True
    return any(fragment in name for fragment in IDENTITY_KEY_SUBSTRINGS)


def _redact_text(value: str) -> str:
    """Redact identity that survived under an innocent key (a note, a description)."""
    redacted = _EMAIL.sub(REDACTED, value)
    return _LONG_DIGITS.sub(REDACTED, redacted)


def _as_plain(value: Any) -> Any:
    """A model object rendered as plain data, so the scrub can actually walk into it.

    Without this the scrub silently passes an object-shaped event straight through: it is not
    a Mapping and not a Sequence, so the recursion bottoms out and every attribute on it —
    including ``buyer_email`` — survives to the wire. The scrub would still look like it
    worked, because everything anybody tested it with was a dict. Pydantic models, dataclasses
    and plain objects all reach here, and anything genuinely opaque falls through to its
    ``repr`` rather than being emitted whole.
    """
    for attribute in ("model_dump", "dict", "_asdict"):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                rendered = method()
            except Exception:  # noqa: BLE001 - a model that will not render is not a scrub failure
                continue
            if isinstance(rendered, Mapping):
                return rendered
    contents = getattr(value, "__dict__", None)
    if isinstance(contents, Mapping) and contents:
        return dict(contents)
    return repr(value)


def scrub(value: Any, *, _removed: list[str] | None = None) -> Any:
    """A deep copy of ``value`` with every buyer-identity key removed.

    Recursive over mappings **and** sequences: identity buried three levels down inside a
    list of line items is identity all the same, and a scrub that only walked the top level
    would look like it worked on the one payload anybody tested it with. Objects are rendered
    to plain data first (see :func:`_as_plain`) for exactly the same reason.

    Returns a NEW structure. The caller's event is never mutated — a scrub with a side effect
    on the ledger's own copy of an event would corrupt the hash chain that event is sealed
    into.
    """
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _is_identity_key(key):
                if _removed is not None:
                    _removed.append(str(key))
                continue
            out[str(key)] = scrub(item, _removed=_removed)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item, _removed=_removed) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [scrub(item, _removed=_removed) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    plain = _as_plain(value)
    if plain is value:  # pragma: no cover - `_as_plain` always returns something else
        return value
    return scrub(plain, _removed=_removed)


def scrub_report(value: Any) -> tuple[Any, int]:
    """:func:`scrub`, plus how many keys it removed.

    The COUNT and not the names: a "we removed ``buyer_email`` and ``buyer_name``" note in the
    outgoing payload re-publishes exactly the field names the scrub exists to keep off the
    wire, and tells the recipient precisely what the network holds about its buyers.
    """
    removed: list[str] = []
    scrubbed = scrub(value, _removed=removed)
    return scrubbed, len(removed)
