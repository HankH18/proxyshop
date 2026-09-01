"""Web-pixel event emission — the deliberately **lossy** half of the stub.

Acceptance criterion 3 states the asymmetry that gives this module its whole reason to
exist: *configurable pixel drop-rate; webhooks always delivered.* The real Shopify web
pixel is a browser beacon. It is lost to content blockers, to consent banners, to tab
closes, to flaky mobile networks — and when it is lost, **nothing anywhere records that it
was lost**. The order webhook, by contrast, is a server-to-server delivery Shopify retries
for up to 48 hours. So the pixel is a sample and the webhook is the truth
(``DESIGN.md:136``, ``SPEC.md:13`` R4), and T-061 exists to reconcile the two.

This module therefore supports four states, not two:

============  =================================================================
State         Meaning
============  =================================================================
firing        ``pixel_mode=on``, ``pixel_drop_rate=0.0``. Every checkout emits.
lossy         ``pixel_mode=on``, ``0 < pixel_drop_rate <= 1``. Some emit.
degraded      ``pixel_mode=partial``. Events emit, but ``orderId`` and
              ``discountApplications`` are ``null`` — the beacon left before
              the order existed. The event is there; the join keys are not.
non-firing    ``pixel_mode=off``. **None** emit, whatever the drop rate says.
============  =================================================================

The last two are the *documented non-firing cases* the ticket objective asks for. They are
different failures and a reconciler must tell them apart: an absent event means the webhook
is the only evidence, whereas a degraded event means a row exists that must be marked as a
visible gap rather than counted as a conversion.

Two payload shapes, both emitted
--------------------------------
1. :func:`checkout_completed_payload` — the **Web Pixels API standard event object**, the
   thing a pixel extension's ``analytics.subscribe('checkout_completed', …)`` callback
   receives. camelCase, ``data.checkout.*``, money as ``MoneyV2`` with a **numeric**
   ``amount``.
2. :func:`collector_payload` — the **flattened body the pixel extension POSTs** to the
   merchant collector, which ``DESIGN.md:39`` fixes as *clientId, checkout token, order id,
   discountApplications*. Its four key spellings are a published join-key contract, so the
   stub emits exactly those and nothing else.

Both are produced for the same checkout so a consumer can be tested at whichever boundary it
actually sits on.

Join keys (D24)
---------------
The ledger pins the pixel↔webhook join keys as
``{checkout_token, order_ref, client_id, discount_code}``. Those are the *ledger event's*
snake_case names; the wire shapes differ by design, because the stub reproduces what
Shopify actually sends:

============== ================================ ==================================
Join key       Pixel collector payload          Order webhook
============== ================================ ==================================
checkout_token ``checkoutToken``                ``checkout_token``
order_ref      ``orderId``                      ``admin_graphql_api_id`` / ``id``
client_id      ``clientId``                     ``note_attributes[…client_id]``
discount_code  ``discountApplications[].code``  ``discount_codes[].code``
============== ================================ ==================================

The ``client_id`` row is the one place the stub goes beyond what Shopify does on its own:
Shopify never puts a web-pixel client id on an order webhook. Propagating it through
``note_attributes`` is how a real app carries its own correlation id through checkout, and
without it the four pinned join keys are not all reachable from the two payloads. It is
called out here rather than hidden, because a consumer must know it is an app-level
convention and not a Shopify guarantee.

No PII, ever
------------
The Web Pixels ``Checkout`` type really does carry ``email``, ``phone``, ``billingAddress``
and ``shippingAddress``. This stub emits none of them: C5 forbids protected-customer-data
scopes and the frozen collector contract rejects those keys outright. A stub that emitted
them would let a consumer build on data production will not have.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime

from shopify_stub.state import (
    Checkout,
    Order,
    PixelEvent,
    PixelMode,
    StubState,
    SuppressedEvent,
    SuppressionReason,
)

#: The Web Pixels API standard event this system listens for. It is the only one emitted:
#: nothing in the system subscribes to any other standard event.
CHECKOUT_COMPLETED = "checkout_completed"


class PixelEmitter:
    """Decides whether a pixel event fires, and builds it when it does.

    The RNG is created once per emitter and seeded from
    :attr:`~shopify_stub.state.StubConfig.pixel_seed` when that is set, so a test can pin an
    *exact* emitted count rather than asserting a statistical band. With no seed the source
    is :class:`random.Random` with system entropy, which is what makes the stub feel like a
    real lossy beacon in the e2e demo.
    """

    def __init__(self, state: StubState) -> None:
        self._state = state
        self._rng = random.Random(state.config.pixel_seed)  # noqa: S311 - loss simulation

    def reseed(self) -> None:
        """Rebuild the RNG from the current config. Called when the config changes."""
        self._rng = random.Random(self._state.config.pixel_seed)  # noqa: S311

    def should_emit(self) -> tuple[bool, SuppressionReason | None]:
        """Decide, and say why not when the answer is no.

        Returns:
            ``(True, None)`` to emit, or ``(False, reason)`` to suppress.

        Note the ordering: :attr:`PixelMode.OFF` is checked **first and unconditionally**. A
        non-firing pixel does not consume a random draw, which keeps a seeded drop-rate
        sequence identical whether or not other checkouts happened while the pixel was off.
        """
        if self._state.config.pixel_mode is PixelMode.OFF:
            return False, SuppressionReason.NOT_FIRING
        rate = self._state.config.pixel_drop_rate
        if rate <= 0.0:
            return True, None
        if self._rng.random() < rate:
            return False, SuppressionReason.DROPPED
        return True, None

    def emit_checkout_completed(
        self,
        *,
        checkout: Checkout,
        order: Order,
        now: datetime,
    ) -> PixelEvent | None:
        """Emit (or drop) the ``checkout_completed`` event for a finished checkout.

        Returns:
            The :class:`~shopify_stub.state.PixelEvent` that was recorded, or ``None`` when
            the event was suppressed. Suppression is *always* recorded in
            :attr:`~shopify_stub.state.StubState.suppressed_events`, so a test can tell
            "dropped" from "never attempted" — real Shopify cannot, and that blind spot is
            exactly what the stub is here to make testable.
        """
        emit, reason = self.should_emit()
        if not emit:
            assert reason is not None
            self._state.suppressed_events.append(
                SuppressedEvent(
                    checkout_token=checkout.token,
                    client_id=checkout.client_id,
                    reason=reason,
                    at=now,
                )
            )
            return None
        degraded = self._state.config.pixel_mode is PixelMode.PARTIAL
        event = PixelEvent(
            id=str(uuid.uuid4()),
            name=CHECKOUT_COMPLETED,
            timestamp=now,
            client_id=checkout.client_id,
            payload=checkout_completed_payload(
                checkout=checkout,
                order=order,
                now=now,
                shop_domain=self._state.config.shop_domain,
                sequence=len(self._state.pixel_events),
                degraded=degraded,
            ),
        )
        self._state.pixel_events.append(event)
        return event


def collector_payload(
    *,
    checkout: Checkout,
    order: Order,
    degraded: bool = False,
) -> dict[str, object]:
    """The flattened body the pixel extension POSTs to the merchant collector.

    Exactly four keys, spelled as the published join-key contract spells them. ``degraded``
    nulls the two members a beacon that raced order creation would not have had —
    ``orderId`` and ``discountApplications`` — and leaves ``clientId`` and ``checkoutToken``
    intact, because those come from the browser and are always available.
    """
    if degraded:
        return {
            "clientId": checkout.client_id,
            "checkoutToken": checkout.token,
            "orderId": None,
            "discountApplications": None,
        }
    applications: list[dict[str, object]] = []
    if order.discount_code:
        applications.append(
            {
                "code": order.discount_code,
                "value": discount_percentage_points(order),
                "type": "code",
            }
        )
    return {
        "clientId": checkout.client_id,
        "checkoutToken": checkout.token,
        "orderId": f"gid://shopify/Order/{order.id}",
        "discountApplications": applications,
    }


def checkout_completed_payload(
    *,
    checkout: Checkout,
    order: Order,
    now: datetime,
    shop_domain: str,
    sequence: int = 0,
    degraded: bool = False,
) -> dict[str, object]:
    """Build the Web Pixels API ``checkout_completed`` standard event object.

    camelCase throughout, because the Web Pixels API is a browser JavaScript API and that is
    what it delivers to a subscriber. Mixing this with the webhook's snake_case is the most
    likely way a consumer gets reconciliation wrong, so the stub keeps both shapes honest
    rather than normalising them for the consumer's convenience.

    Money here is ``MoneyV2`` with a **numeric** ``amount`` — the Web Pixels API types it as
    a number, unlike REST, which uses a decimal string. A consumer that assumes one format
    everywhere breaks on whichever surface it did not test against.
    """
    discount_applications: list[dict[str, object]] | None
    if degraded:
        discount_applications = None
    elif order.discount_code:
        discount_applications = [
            {
                # The Web Pixels DiscountApplication type has NO `code` member; for a
                # DISCOUNT_CODE application the documented carrier of the code is `title`.
                "type": "DISCOUNT_CODE",
                "title": order.discount_code,
                "value": {"percentage": discount_percentage_points(order)},
                "allocationMethod": "ACROSS",
                "targetSelection": "ALL",
                "targetType": "LINE_ITEM",
            }
        ]
    else:
        discount_applications = []
    return {
        "id": str(uuid.uuid4()),
        "name": CHECKOUT_COMPLETED,
        "type": "standard",
        "seq": sequence,
        "timestamp": iso(now),
        "clientId": checkout.client_id,
        "context": {
            "document": {
                "location": {
                    "href": f"https://{shop_domain}/checkouts/{checkout.token}/thank_you",
                }
            }
        },
        "data": {
            "checkout": {
                "token": checkout.token,
                "order": None if degraded else {"id": str(order.id)},
                "currencyCode": order.currency,
                "subtotalPrice": {
                    "amount": float(order.subtotal_price),
                    "currencyCode": order.currency,
                },
                "totalPrice": {
                    "amount": float(order.total_price),
                    "currencyCode": order.currency,
                },
                "discountsAmount": {
                    "amount": float(order.discount_amount),
                    "currencyCode": order.currency,
                },
                "discountApplications": discount_applications,
            }
        },
    }


def discount_percentage_points(order: Order) -> float:
    """The discount expressed in percentage **points** (``10.0`` for 10% off).

    Derived from the money actually taken off rather than from the code's configured rate,
    so a partially-applicable discount reports what the shopper really got.
    """
    if order.subtotal_price <= 0:
        return 0.0
    return round(float(order.discount_amount / order.subtotal_price) * 100, 4)


def iso(moment: datetime) -> str:
    """RFC 3339 with a ``Z``, which is what Shopify's JSON carries."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "CHECKOUT_COMPLETED",
    "PixelEmitter",
    "checkout_completed_payload",
    "collector_payload",
    "discount_percentage_points",
]
