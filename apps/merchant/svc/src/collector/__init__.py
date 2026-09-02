"""T-051 — the web pixel's collector: the only thing at the end of ``collectorUrl``.

``install.config.web_pixel_settings`` points every installed pixel at
``{app_url}/pixel/collect``. This package is what answers there. Until it existed the
package was empty, ``main.create_app``'s ``*/routes.py`` discovery found nothing to mount,
and every shop that installed the app got a pixel beaconing into a 404 — which looks
exactly like a shop whose shoppers never check out. The mismatch was invisible because
nothing checked that the registered URL was a URL this service serves;
``tests/test_merchant_hardening.py`` now does.

**Join keys only, by allowlist.** SPEC C5 grants this app no protected-customer-data scope,
so the collector must be unable to hold customer data even if a future pixel version starts
sending it. The check is therefore an allowlist of the published keys
(:data:`ACCEPTED_FIELDS`) rather than a denylist of PII names: a denylist accepts every
field nobody thought of, and "nobody thought of it" is the normal way a new personal field
arrives. A payload carrying anything else is refused, and the refusal names the *field*,
never the value — a rejection log that quotes the email it refused has stored the email.

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


class PixelEventRejected(ValueError):
    """A beacon was refused. The message names offending fields, never their values."""

    def __init__(self, reason: str, fields: tuple[str, ...] = ()) -> None:
        self.fields = tuple(fields)
        detail = f" ({', '.join(self.fields)})" if self.fields else ""
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


def _read(payload: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for name in aliases:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def _discount_code(payload: dict[str, Any]) -> str | None:
    """The discount code, from the top level or from inside ``discountApplications``."""
    direct = _read(payload, JOIN_KEY_ALIASES["discount_code"])
    if isinstance(direct, str) and direct:
        return direct
    applications = payload.get("discountApplications") or payload.get("discount_applications")
    if isinstance(applications, list):
        for application in applications:
            if isinstance(application, dict):
                code = application.get("code") or application.get("discountCode")
                if isinstance(code, str) and code:
                    return code
    return None


def _total_price(payload: dict[str, Any]) -> float | None:
    raw = payload.get("total_price", payload.get("totalPrice"))
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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
            :data:`ACCEPTED_FIELDS` (which is every PII field, by construction), or carries
            no ``checkout_token`` — without which the observation can never be joined to the
            webhook and is not an observation of anything.
    """
    if not isinstance(payload, dict):
        raise PixelEventRejected(
            f"a pixel event must be a JSON object, got {type(payload).__name__}"
        )

    unknown = tuple(sorted(str(key) for key in payload if str(key) not in ACCEPTED_FIELDS))
    if unknown:
        # The values are deliberately absent from this message: the fields that get here are
        # the ones we refused to hold, and a log line is a place data lives.
        raise PixelEventRejected(
            "the collector accepts published join keys only; refused unknown fields", unknown
        )

    checkout_token = _read(payload, JOIN_KEY_ALIASES["checkout_token"])
    if not isinstance(checkout_token, str) or not checkout_token:
        raise PixelEventRejected(
            "a pixel event carries no checkout_token; it can never be joined to an order"
        )

    order_ref = _read(payload, JOIN_KEY_ALIASES["order_ref"])
    client_id = _read(payload, JOIN_KEY_ALIASES["client_id"])
    discount_code = _discount_code(payload)

    present = {
        "checkout_token": checkout_token,
        "order_ref": order_ref,
        "client_id": client_id,
        "discount_code": discount_code,
    }
    gaps = tuple(key for key in GAP_KEYS if not present[key])

    return PixelObservation(
        checkout_token=checkout_token,
        client_id=str(client_id) if client_id is not None else None,
        order_ref=str(order_ref) if order_ref is not None else None,
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
    "GAP_KEYS",
    "JOIN_KEY_ALIASES",
    "PIXEL_INBOX",
    "PixelEventRejected",
    "PixelInbox",
    "PixelObservation",
    "accept_pixel_event",
]
