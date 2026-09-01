"""The `LedgerEvent` kind vocabulary (C11/D24), and the per-kind payload shapes.

`LedgerEventKind` is frozen jointly across the redirect path and the Shopify checkout path: both
emit the SAME kinds, so a proof written against one holds for the other. It was extended exactly
once — here — with `auction_opened`, `auction_closed`, `offer_integrity`, `blacklisted` and
`blacklist_expired`.

**Why `LedgerEvent.payload` is an open mapping rather than a discriminated union.** D24 asks for
"one shape per kind", and the shapes ARE published below. But the ledger is append-only and its
value is that it records what actually happened: a `checkout_pixel` body is a lossy client-side
observation in Shopify's own camelCase spelling, an `order_paid` body is a webhook in a third
party's shape, and an `accepted` body is protocol-shaped. A closed union on the model would make
the ledger refuse to record a real event because a vendor added a field — which converts a
recoverable data question into a dropped event. So the union lives in
`validate_ledger_payload()`, callable at the PRODUCING boundary where a malformed body can still
be refused, and the stored model stays lossless.

**The pixel↔webhook join.** D24 pins the join keys as `{checkout_token, order_ref, client_id,
discount_code}`, snake_case. In practice `order_ref` is a top-level `LedgerEvent` field (not a
payload key) and the pixel's client id arrives from Shopify's Web Pixels API spelled `clientId`.
`join_key_view()` is the one reader that reconciles those spellings, so T-051 and T-061 join on
the same four values without each inventing a key name.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from contracts.protocol import LedgerEvent, LedgerEventKind

#: The 18 frozen kinds, as plain strings, for callers that want a set rather than an enum.
LEDGER_EVENT_KINDS: frozenset[str] = frozenset(member.value for member in LedgerEventKind)

#: D24's pinned join keys for the pixel↔webhook reconciliation, snake_case, exactly these names.
LEDGER_JOIN_KEYS: tuple[str, ...] = (
    "checkout_token",
    "order_ref",
    "client_id",
    "discount_code",
)

#: The kinds that must be joinable to each other by `LEDGER_JOIN_KEYS`.
JOINABLE_KINDS: frozenset[str] = frozenset(
    {LedgerEventKind.checkout_pixel.value, LedgerEventKind.order_paid.value}
)

#: Alternate spellings a join key legitimately arrives under, because two of these bodies are
#: shaped by Shopify rather than by us. Read through `join_key_view()`, never by hand.
_JOIN_KEY_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "checkout_token": ("checkout_token", "checkoutToken", "token"),
        "order_ref": ("order_ref", "order_id", "orderId", "orderRef"),
        "client_id": ("client_id", "clientId"),
        "discount_code": ("discount_code", "discountCode", "code"),
    }
)

#: The documented body of each kind: the keys a well-formed payload of that kind carries. This is
#: D24's "one shape per kind" in the form the ledger can actually hold — a description a producer
#: is checked against, not a filter the store applies to history.
LEDGER_PAYLOAD_SHAPES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "auction_opened": ("intent_id", "cluster_id", "roster_size"),
        "bid_placed": ("bid_ref", "store_id", "offer"),
        "auction_closed": ("shortlist_size", "reason"),
        "shown": ("bid_ref", "slot"),
        "accepted": ("bid_ref", "checkout_token", "offer"),
        "code_created": ("code", "permalink_url", "expires_at"),
        "checkout_redirect": ("checkout_token", "permalink_url"),
        "checkout_pixel": ("checkout_token", "client_id", "total_price"),
        "order_paid": ("checkout_token", "order_ref", "total_price"),
        "order_fulfilled": ("order_ref", "fulfilled_at"),
        "refund": ("order_ref", "amount", "reason"),
        "feedback": ("matched_pitch", "reason"),
        "reconciled": ("price_honored", "discount_honored", "pixel_missing", "order_ref"),
        "claim_verified": ("claim_ref", "status", "dim"),
        "policy_event": ("kind", "severity", "opened_at"),
        "offer_integrity": ("bid_ref", "field", "promised", "observed"),
        "blacklisted": ("store_id", "reason_code", "source", "expires_at"),
        "blacklist_expired": ("store_id", "reason_code"),
    }
)


def _read(payload: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return None


def join_key_view(event: Any) -> dict[str, Any]:
    """The four D24 join keys for one event, read from the event and its payload.

    Reconciles the spellings the two checkout paths actually produce: `order_ref` is a top-level
    `LedgerEvent` field, the pixel body carries `clientId`, the webhook body carries `order_id`.
    Returns all four keys always; a value we cannot find is `None`, so a caller can see WHICH key
    is missing rather than getting a short dict.
    """
    if isinstance(event, Mapping):
        top: Mapping[str, Any] = event
    else:
        dump = getattr(event, "model_dump", None)
        top = dump() if callable(dump) else {}
    payload = top.get("payload") or {}
    if not isinstance(payload, Mapping):
        payload = {}

    view: dict[str, Any] = {}
    for key, aliases in _JOIN_KEY_ALIASES.items():
        value = _read(payload, aliases)
        if value is None:
            value = _read(top, aliases)
        view[key] = value
    return view


def validate_ledger_payload(kind: Any, payload: Mapping[str, Any]) -> list[str]:
    """Check a payload against its kind's published shape. Returns the problems; empty is fine.

    Called at the PRODUCING boundary. It is deliberately not wired into `LedgerEvent` itself —
    see this module's header for why the stored event must stay lossless. Extra keys are allowed
    (a vendor body carries plenty); missing pinned keys are reported.
    """
    raw_kind = str(getattr(kind, "value", kind) or "")
    problems: list[str] = []
    if raw_kind not in LEDGER_EVENT_KINDS:
        return [f"unknown ledger event kind: {raw_kind!r}"]
    if not isinstance(payload, Mapping):
        return [f"payload for {raw_kind!r} must be a mapping, got {type(payload).__name__}"]

    if raw_kind in JOINABLE_KINDS:
        # C11/D24: both checkout paths must be joinable, so these are checked by the join view
        # rather than by literal key presence — the pixel spells one of them `clientId`.
        view = join_key_view({"kind": raw_kind, "payload": payload})
        for key in ("checkout_token",):
            if view.get(key) is None:
                problems.append(
                    f"{raw_kind!r} payload is missing join key {key!r}; the pixel and the "
                    "webhook cannot be reconciled without it"
                )
        return problems

    expected = LEDGER_PAYLOAD_SHAPES.get(raw_kind, ())
    for key in expected:
        if key not in payload:
            problems.append(f"{raw_kind!r} payload is missing published key {key!r}")
    return problems


__all__ = [
    "JOINABLE_KINDS",
    "LEDGER_EVENT_KINDS",
    "LEDGER_JOIN_KEYS",
    "LEDGER_PAYLOAD_SHAPES",
    "LedgerEvent",
    "LedgerEventKind",
    "join_key_view",
    "validate_ledger_payload",
]
