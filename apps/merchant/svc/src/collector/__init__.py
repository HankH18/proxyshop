"""T-051 — the web pixel's collector: the only thing at the end of ``collectorUrl``.

``install.config.web_pixel_settings`` points every installed pixel at
``{app_url}/pixel/collect``. This package is what answers there. Until it existed the
package was empty, ``main.create_app``'s ``*/routes.py`` discovery found nothing to mount,
and every shop that installed the app got a pixel beaconing into a 404 — which looks
exactly like a shop whose shoppers never check out. The mismatch was invisible because
nothing checked that the registered URL was a URL this service serves;
``tests/test_merchant_hardening.py`` now does.

**Join keys only, by allowlist — of names AND of shapes.** SPEC C5 grants this app no
protected-customer-data scope, so the collector must be unable to hold customer data even if
a future pixel version starts sending it. The check is an allowlist of the published keys
(:data:`ACCEPTED_FIELDS`) rather than a denylist of PII names: a denylist accepts every field
nobody thought of, and "nobody thought of it" is the normal way a new personal field arrives.

A name allowlist alone is **not enough**, and the first version of this module proved it: an
allowlisted key whose value was an object smuggled the whole object in, because the value was
coerced with ``str()`` and stored. ``{"clientId": {"email": "…", "name": "…"}}`` was accepted
and kept verbatim. Every join key is therefore also **shape-checked** — a scalar, or it is
refused — and ``discountApplications`` (the one structured field) is validated entry by entry
against :data:`DISCOUNT_APPLICATION_FIELDS` instead of being carried as an opaque blob.

A name allowlist and a shape allowlist are still not enough, and T-365 proved that too: both
check what a field *is* and neither checks how big it is. The request body is capped at 1 MiB
by :mod:`merchant_svc.http_limits`, but the collector KEEPS what is inside the body, so the
cap bounded the wrong thing — four join keys of 200,000 characters rode under it and a full
ring of them held 391.10 MiB inside a 256 MiB container. Every published field is therefore
also **length-checked** against :data:`MAX_PIXEL_FIELD_CHARS`, and an over-long one is
refused whole rather than truncated; the reasoning is at the check itself in
:func:`accept_pixel_event`.

The refusal names **neither the value nor the key**, only how many fields were refused. A key
is attacker-chosen text too: ``{"shopper@example.com": 1}`` put an address in the response
body and the log of a service whose whole job is to hold no addresses. The published
allowlist is in this module, so "3 field(s)" plus :data:`ACCEPTED_FIELDS` is all an
integrator needs, and it is all anybody else gets.

**A dropped beacon is a visible gap, not a default.** R4 makes the webhook authoritative
and the pixel lossy: a beacon that leaves the browser before the order id exists carries
``orderId: null``. That is not an error and must not raise — but it must not silently look
like a complete observation either, so :class:`PixelObservation` names the join keys it did
not get in :attr:`~PixelObservation.gaps` and a reconciler can see which one is missing.

The spellings are Shopify's, not ours: the Web Pixels API is browser JavaScript and sends
``checkoutToken``/``clientId``. ``contracts.ledger.join_key_view`` is the published reader
that reconciles those with the webhook's snake_case, and :data:`JOIN_KEY_ALIASES` is the
same alias table applied at the door.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: The four D24 join keys, and every spelling each legitimately arrives under. Kept in step
#: with ``contracts.ledger._JOIN_KEY_ALIASES`` — that module is the published reader for the
#: same keys on the way *out*; this is the guard on the way *in*.
JOIN_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "checkout_token": ("checkout_token", "checkoutToken", "token"),
    "order_ref": ("order_ref", "order_id", "orderId", "orderRef"),
    "client_id": ("client_id", "clientId"),
    "discount_code": ("discount_code", "discountCode", "code"),
}

#: Non-join fields a beacon may carry: the discount block the join key is lifted out of, and
#: the order total, which R4 needs to detect a price that was not honoured. Nothing here
#: identifies a person, and nothing else is accepted.
CARRIED_FIELDS: tuple[str, ...] = (
    "discountApplications",
    "discount_applications",
    "total_price",
    "totalPrice",
    "currency",
    "timestamp",
)

#: Every key an accepted payload may contain. Anything outside this set is a refusal.
ACCEPTED_FIELDS: frozenset[str] = frozenset(
    [alias for aliases in JOIN_KEY_ALIASES.values() for alias in aliases] + list(CARRIED_FIELDS)
)

#: The join keys whose absence is reported as a gap. ``total_price`` is deliberately not one
#: of them: a complete ``checkout_completed`` beacon does not always carry it, so treating it
#: as a gap would mark every healthy observation incomplete and make the marker meaningless.
GAP_KEYS: tuple[str, ...] = ("checkout_token", "order_ref", "client_id", "discount_code")

#: The only keys a ``discountApplications`` entry may carry. It is the one structured field
#: the pixel sends, and an unvalidated nested object is a name allowlist with a hole in it.
DISCOUNT_APPLICATION_FIELDS: frozenset[str] = frozenset(
    {"code", "discountCode", "discount_code", "value", "type", "title", "allocationMethod"}
)

#: Most discount applications a beacon may declare. A list is an amplification vector on an
#: unauthenticated route; no real checkout carries anything close to this many.
MAX_DISCOUNT_APPLICATIONS = 32

#: Most characters any single field of a beacon may carry (T-365).
#:
#: :data:`merchant_svc.http_limits.MAX_REQUEST_BODY_BYTES` bounds the REQUEST; this bounds
#: what one request can put in the STORE, and only the second of those is what the collector
#: keeps. Without it :class:`PixelInbox`'s 512-entry ring bounded the entry COUNT while
#: nothing bounded the entry SIZE, and the product was a ceiling outside the box: four join
#: keys of 200,000 characters ride comfortably under the 1 MiB body cap (800,072 bytes
#: encoded) and retain 800,970 bytes, so a full ring held **391.10 MiB** against
#: ``apps/merchant/compose.yaml:66  mem_limit: 256m``. The process OOMs at roughly 330
#: anonymous requests and never reaches its own limit. A bound whose ceiling is larger than
#: the container is the appearance of a bound.
#:
#: **Where 512 comes from.** The longest join key a real beacon carries is a discount code,
#: and Shopify's own maximum for one is 255 characters; a checkout token is 32 hex characters
#: and an Order GID is under 40. 512 is double the longest legitimate value, and at 512 a
#: full ring holds at most ``512 x 4 join keys x 512 slots`` = 1 MiB of text — inside the
#: container with three orders of magnitude to spare, which is the property the old bound
#: did not have.
#:
#: The same axis ``exchange.auction.routes.MAX_RECORDED_OFFER_VALUE_CHARS`` (4096) bounds for
#: a recorded bid, reached through a different door. Deliberately not shared: the two are
#: separate deployables, a cart permalink is not a join key, and importing across them would
#: couple the merchant's ceiling to the exchange's traffic.
MAX_PIXEL_FIELD_CHARS = 512

#: An integer whose ``bit_length`` exceeds this cannot render inside
#: :data:`MAX_PIXEL_FIELD_CHARS` characters: a decimal digit costs log2(10) = 3.33 bits, so
#: four bits per character is a conservative over-estimate and anything past this bound is
#: over the ceiling by construction. Checked INSTEAD of rendering, because ``str()`` on an
#: integer past CPython's 4300-digit conversion limit raises ``ValueError`` — and
#: :class:`PixelEventRejected` subclasses ``ValueError``, so letting that one escape would
#: read to every caller as a refusal this module never made, and answer 500 at the route.
_MAX_SCALAR_INT_BITS = 4 * MAX_PIXEL_FIELD_CHARS


class PixelEventRejected(ValueError):
    """A beacon was refused.

    ``fields`` is structured data for a caller in-process; it is deliberately **not** in the
    message. Both a field's value and its *name* are attacker-chosen text, so quoting either
    puts it in the response body and the log of a service whose entire purpose is to hold
    none of it. The message carries the count.

    The count's noun is plain ``field(s)`` rather than ``unknown field(s)``: since T-365 a
    field can be refused for its LENGTH as well as for its name, and those names come from
    :data:`ACCEPTED_FIELDS` rather than from the sender. Which kind of refusal it is belongs
    in ``reason``, which is this module's text, not the sender's.
    """

    def __init__(self, reason: str, fields: tuple[str, ...] = ()) -> None:
        self.fields = tuple(fields)
        detail = f" ({len(self.fields)} field(s))" if self.fields else ""
        super().__init__(f"{reason}{detail}")


@dataclass(frozen=True)
class PixelObservation:
    """One accepted client-side checkout observation. Never authoritative (R4)."""

    checkout_token: str
    client_id: str | None = None
    order_ref: str | None = None
    discount_code: str | None = None
    total_price: float | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The join keys this beacon did not carry, named so a reconciler can see WHICH is
    #: absent. Empty on a complete observation — an empty marker must read as "no gap".
    gaps: tuple[str, ...] = ()


def _scalar(value: Any) -> str | None:
    """``value`` as text, or ``None`` when it is not a scalar this collector may hold.

    The shape check is the second half of the allowlist. A ``dict`` or ``list`` under an
    allowlisted key used to be coerced with ``str()`` and stored whole, which is how
    ``{"clientId": {"email": "…"}}`` walked through a guard whose entire job was to stop it.
    A join key is a short identifier or it is not a join key.
    """
    if value is None or isinstance(value, bool | dict | list | tuple | set):
        return None
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        text = str(value)
    else:
        return None
    return text or None


def _over_the_field_ceiling(value: Any) -> bool:
    """Whether ``value``, as this collector would keep it, is longer than the ceiling.

    Answers ``False`` for anything that is not a scalar — a dict, a list, ``None``. Those are
    the shape check's business, and a length is not a meaningful thing to say about them.

    An integer is measured by its ``bit_length`` rather than rendered, for the reason
    :data:`_MAX_SCALAR_INT_BITS` gives. Anything under that bound renders in at most 2048
    characters, so the exact comparison below is safe and the boundary stays exact.
    """
    if isinstance(value, str):
        return len(value.strip()) > MAX_PIXEL_FIELD_CHARS
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    if isinstance(value, int):
        return value.bit_length() > _MAX_SCALAR_INT_BITS or len(str(value)) > MAX_PIXEL_FIELD_CHARS
    return False  # a finite float renders in at most ~24 characters; `inf`/`nan` in three


def _oversized_fields(payload: dict[str, Any]) -> tuple[str, ...]:
    """Every published top-level field of ``payload`` that is over the ceiling, sorted.

    Checked across the whole of :data:`ACCEPTED_FIELDS` rather than only the four keys that
    are retained today. ``currency`` and ``timestamp`` are accepted and currently discarded,
    but that is a property of this implementation and not of the contract: a field the
    collector publishes as acceptable is a field a future reader may keep, and a bound that
    only covers today's readers has to be re-derived every time one is added.
    """
    return tuple(
        sorted(
            str(name)
            for name, value in payload.items()
            if str(name) in ACCEPTED_FIELDS and _over_the_field_ceiling(value)
        )
    )


def _read(payload: dict[str, Any], aliases: tuple[str, ...]) -> str | None:
    """The first alias present, as a scalar. A non-scalar reads as absent, never as text."""
    for name in aliases:
        if name in payload:
            scalar = _scalar(payload[name])
            if scalar is not None:
                return scalar
    return None


def _validate_discount_applications(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Check ``discountApplications`` entry by entry, or raise.

    Raises:
        PixelEventRejected: the field is not a list of small objects whose keys are all in
            :data:`DISCOUNT_APPLICATION_FIELDS`, or there are implausibly many of them.
    """
    raw = payload.get("discountApplications", payload.get("discount_applications"))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PixelEventRejected("discountApplications must be a list")
    if len(raw) > MAX_DISCOUNT_APPLICATIONS:
        raise PixelEventRejected(
            f"a beacon may declare at most {MAX_DISCOUNT_APPLICATIONS} discount applications"
        )
    entries: list[dict[str, Any]] = []
    unknown: list[str] = []
    oversized: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise PixelEventRejected("each discountApplications entry must be an object")
        unknown.extend(str(key) for key in entry if str(key) not in DISCOUNT_APPLICATION_FIELDS)
        # The key allowlist above bounds which fields an entry may carry; this bounds how big
        # they may be. `_discount_code` lifts `code` out of an entry and INTO the stored
        # observation, so a ceiling applied only at the top level leaves the same unbounded
        # string arriving one level down.
        oversized.extend(str(key) for key, value in entry.items() if _over_the_field_ceiling(value))
        entries.append(entry)
    if unknown:
        raise PixelEventRejected(
            "a discountApplications entry carries fields outside the published set",
            tuple(sorted(set(unknown))),
        )
    if oversized:
        raise PixelEventRejected(
            "a discountApplications entry carries a field over the "
            f"{MAX_PIXEL_FIELD_CHARS}-character ceiling",
            tuple(sorted(set(oversized))),
        )
    return entries


def _discount_code(payload: dict[str, Any], entries: list[dict[str, Any]]) -> str | None:
    """The discount code, from the top level or from a validated ``discountApplications``."""
    direct = _read(payload, JOIN_KEY_ALIASES["discount_code"])
    if direct:
        return direct
    for entry in entries:
        for name in ("code", "discountCode", "discount_code"):
            if name in entry:
                code = _scalar(entry[name])
                if code:
                    return code
    return None


def _total_price(payload: dict[str, Any]) -> float | None:
    raw = payload.get("total_price", payload.get("totalPrice"))
    if raw is None or isinstance(raw, bool | dict | list):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError, not ValueError: `float(10 ** 400)` raises "int too large to convert
        # to float", which is an ArithmeticError and would otherwise escape this function as
        # a 500. `_oversized_fields` refuses a number that long before it reaches here, so
        # this is the belt to that braces — but a route-facing parser that promises `| None`
        # must keep the promise on its own.
        return None
    # `inf`/`nan` survive `float("1e999")` and `float("nan")`, do not survive a JSON
    # round-trip, and would silently poison R4's "was the price honoured" comparison.
    return value if math.isfinite(value) else None


def accept_pixel_event(payload: Any, *, now: datetime | None = None) -> PixelObservation:
    """Validate one beacon and return the observation, or refuse it.

    Args:
        payload: the decoded JSON body the pixel POSTed.
        now: receipt instant.

    Returns:
        :class:`PixelObservation`, with :attr:`~PixelObservation.gaps` naming any join key
        the beacon did not carry.

    Raises:
        PixelEventRejected: the body is not an object, carries a field outside
            :data:`ACCEPTED_FIELDS`, carries a join key whose value is not a scalar, carries
            a ``discountApplications`` entry outside
            :data:`DISCOUNT_APPLICATION_FIELDS`, or carries no ``checkout_token`` — without
            which the observation can never be joined to the webhook and is not an
            observation of anything.
    """
    if not isinstance(payload, dict):
        raise PixelEventRejected(
            f"a pixel event must be a JSON object, got {type(payload).__name__}"
        )

    unknown = tuple(sorted(str(key) for key in payload if str(key) not in ACCEPTED_FIELDS))
    if unknown:
        # Neither the names nor the values reach this message: a key is attacker-chosen text
        # too, and an error body is as much a place data lives as a database is.
        raise PixelEventRejected(
            "the collector accepts published join keys only; refused unknown fields", unknown
        )

    # ORDER MATTERS, and this check has to come before the shape check below rather than
    # after it: `_scalar` renders an integer with `str()`, and CPython raises ValueError past
    # 4300 digits — so a shape check run first would turn a wall of digits into a 500 on an
    # unauthenticated route. See `_MAX_SCALAR_INT_BITS`.
    #
    # REFUSED WHOLE, NOT TRUNCATED, and that is the decision rather than the default. This is
    # a telemetry endpoint and refusing a beacon loses data, so the trade was weighed both
    # ways: a truncated `checkout_token` is a *wrong* checkout token, and R4 joins the lossy
    # pixel observation to the authoritative webhook on exactly that value. A prefix joins to
    # nothing, or to the wrong order, while still reading as a complete observation — and R4
    # is already built to survive a beacon that never arrives, but not one that arrives
    # pointing at someone else's order. Silently emptying the field is worse again: an absent
    # join key is published as a `gap`, and a fabricated gap makes the marker mean nothing.
    # Nothing legitimate is lost, either: the longest real join key is a 255-character
    # discount code, so a field over 512 characters is not data this endpoint exists for.
    oversized = _oversized_fields(payload)
    if oversized:
        raise PixelEventRejected(
            f"a beacon field may carry at most {MAX_PIXEL_FIELD_CHARS} characters", oversized
        )

    # A join key that is present but not a scalar is a REFUSAL, not a gap. Reading it as
    # absent would be safe for storage but would mark a fabricated "the pixel dropped this"
    # gap, and R4's gap marker only means something if it means a real drop.
    malformed = tuple(
        sorted(
            alias
            for aliases in JOIN_KEY_ALIASES.values()
            for alias in aliases
            if alias in payload and payload[alias] is not None and _scalar(payload[alias]) is None
        )
    )
    if malformed:
        raise PixelEventRejected(
            "a join key must be a scalar; the collector never stores a structured value",
            malformed,
        )

    entries = _validate_discount_applications(payload)

    checkout_token = _read(payload, JOIN_KEY_ALIASES["checkout_token"])
    if not checkout_token:
        raise PixelEventRejected(
            "a pixel event carries no usable scalar checkout_token; it can never be joined "
            "to an order"
        )

    order_ref = _read(payload, JOIN_KEY_ALIASES["order_ref"])
    client_id = _read(payload, JOIN_KEY_ALIASES["client_id"])
    discount_code = _discount_code(payload, entries)

    present = {
        "checkout_token": checkout_token,
        "order_ref": order_ref,
        "client_id": client_id,
        "discount_code": discount_code,
    }
    gaps = tuple(key for key in GAP_KEYS if not present[key])

    return PixelObservation(
        checkout_token=checkout_token,
        client_id=client_id,
        order_ref=order_ref,
        discount_code=discount_code,
        total_price=_total_price(payload),
        received_at=now or datetime.now(UTC),
        gaps=gaps,
    )


class PixelInbox:
    """A bounded log of accepted observations. The seam E6's reconciler replaces."""

    def __init__(self, capacity: int = 512) -> None:
        self.capacity = max(1, capacity)
        self._observations: list[PixelObservation] = []

    def record(self, observation: PixelObservation) -> None:
        """Keep ``observation``, retiring the oldest once the buffer is full."""
        self._observations.append(observation)
        while len(self._observations) > self.capacity:
            self._observations.pop(0)

    def observations(self) -> tuple[PixelObservation, ...]:
        """Everything accepted, oldest first."""
        return tuple(self._observations)

    def clear(self) -> None:
        """Forget everything. Used between tests."""
        self._observations.clear()


#: The process-wide collector buffer the route writes to.
PIXEL_INBOX = PixelInbox()

__all__ = [
    "ACCEPTED_FIELDS",
    "CARRIED_FIELDS",
    "DISCOUNT_APPLICATION_FIELDS",
    "GAP_KEYS",
    "MAX_DISCOUNT_APPLICATIONS",
    "MAX_PIXEL_FIELD_CHARS",
    "JOIN_KEY_ALIASES",
    "PIXEL_INBOX",
    "PixelEventRejected",
    "PixelInbox",
    "PixelObservation",
    "accept_pixel_event",
]
