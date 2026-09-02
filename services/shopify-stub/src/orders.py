"""Order lifecycle and the two wire shapes an order has.

An order in this system is reachable through two APIs with two different naming
conventions, and the stub reproduces both rather than picking one:

* **The webhook payload** (``orders/paid``, ``orders/fulfilled``, ``refunds/create``) is the
  REST Admin resource: ``snake_case``, money as decimal **strings**, ids as **integers**,
  plus ``admin_graphql_api_id`` carrying the GID.
* **The GraphQL Admin ``orders`` query** returns ``camelCase`` fields, ids as GIDs, money
  inside a ``MoneyBag`` (``{shopMoney: {amount, currencyCode}}``), and statuses as
  SCREAMING_SNAKE enums (``PAID``, ``FULFILLED``) rather than the REST lowercase strings.

Confusing the two is the most likely way a consumer of this stub writes code that passes
here and fails against Shopify, so the stub keeps them strictly separate and never
normalises one into the other.

Two deliberate divergences from real Shopify, both narrowing:

**No customer PII, anywhere.** Real ``orders/paid`` carries a full ``customer`` object with
``email``, ``phone``, ``first_name``, ``last_name`` and ``default_address``. SPEC C5 says
this app requests no protected-customer-data scopes, and the frozen collector contract
rejects exactly those keys, so the stub emits ``"customer": null`` — the shape a guest
checkout produces. Emitting PII the system is not entitled to would let a consumer build on
a field that is absent in production.

**A subset of the keys, never a superset.** Real ``orders/paid`` has 88 top-level keys. The
stub emits the ones this system reads. Every key it does emit is a documented real key —
that is what ``tests/test_stub_recordings.py`` checks in both directions.
"""

from __future__ import annotations

import base64
import binascii
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from shopify_stub.codes import discount_amount_for
from shopify_stub.permalink import store_url
from shopify_stub.state import (
    Checkout,
    Fulfillment,
    LineItem,
    Order,
    Refund,
    StubState,
    money,
)

#: The note-attribute name the stub uses to carry the web-pixel client id through checkout
#: onto the order. Shopify does not do this for you; see :mod:`shopify_stub.telemetry`.
CLIENT_ID_ATTRIBUTE = "proxyshop_client_id"


def iso(moment: datetime) -> str:
    """Shopify's timestamp format: RFC 3339, UTC, ``Z``-suffixed."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def new_token() -> str:
    """A 32-hex-character token, the shape Shopify's checkout/cart tokens have."""
    return uuid.uuid4().hex


def new_client_id() -> str:
    """A web-pixel client id. Shopify's is a UUID stored in a first-party cookie."""
    return str(uuid.UUID(bytes=secrets.token_bytes(16)))


def create_order_from_checkout(
    state: StubState,
    checkout: Checkout,
    *,
    now: datetime | None = None,
) -> Order:
    """Turn a completed checkout into a paid order, redeeming its code if one applied.

    Redemption increments the code's ``usage_count`` **here**, not when the permalink was
    visited. That ordering matters: visiting a cart permalink twice with a ``usageLimit:1``
    code must not burn it, because a shopper who opens the link and abandons the cart has
    not redeemed anything. Only a completed checkout consumes a use.

    Args:
        state: the stub's world. The checkout's variant must be in ``state.variants``.
        checkout: the checkout being completed. Must not already be completed.
        now: completion instant; defaults to the stub's clock.

    Returns:
        The created :class:`~shopify_stub.state.Order`, already stored in ``state``.

    Raises:
        ValueError: the checkout has already been completed, or its variant is unknown.
            Double-completion would emit a second ``orders/paid`` for one checkout token,
            which is exactly the duplicate a reconciler must never have to guess about.
    """
    if checkout.completed:
        raise ValueError(f"checkout {checkout.token} is already completed")
    variant = state.variants.get(checkout.variant_id)
    if variant is None:
        raise ValueError(f"unknown variant {checkout.variant_id}")
    moment = now or state.now()
    line = LineItem(
        variant_id=variant.variant_id,
        product_id=variant.product_id,
        title=variant.title,
        quantity=checkout.quantity,
        price=variant.price,
        sku=variant.sku,
    )
    subtotal = line.line_total
    discount_amount = Decimal("0")
    applied_code = checkout.applied_code
    applied = state.find_code(applied_code)
    if applied is not None:
        # RE-VALIDATE AT PAYMENT, not just when the cart was built. Two carts can be opened
        # with the same `usageLimit: 1` code before either is completed — both see a usage
        # count of zero and both apply it — so validating only at the cart would let a
        # single-use code be redeemed twice. Shopify re-checks the discount when the payment
        # is taken, and the second order simply comes through at full price. That is what
        # makes A5's "duplicate use is an offer-integrity event, not a crash" observable: the
        # system sees an order whose discount was not honoured, rather than a missing order.
        rejection = applied.rejection(
            now=moment,
            cart_has_order_discount=state.config.has_active_automatic_discount,
        )
        if rejection is not None:
            checkout.rejection = rejection
            applied_code = None
            applied = None
        else:
            # The SAME function the cart quote used. See discount_amount_for's docstring for
            # why this is not inlined: the two used to round differently and the shopper was
            # charged a different number from the one they were quoted.
            discount_amount = discount_amount_for(applied, subtotal)
            applied.usage_count += 1
    order = Order(
        id=state.next_order_id(),
        order_number=state.next_order_number(),
        checkout_token=checkout.token,
        cart_token=new_token(),
        client_id=checkout.client_id,
        created_at=moment,
        processed_at=moment,
        updated_at=moment,
        currency=variant.currency,
        line_items=[line],
        discount_code=applied_code,
        discount_amount=discount_amount,
    )
    state.orders[order.id] = order
    checkout.completed = True
    checkout.order_id = order.id
    return order


def fulfil_order(
    state: StubState,
    order: Order,
    *,
    tracking_company: str | None = None,
    tracking_number: str | None = None,
    now: datetime | None = None,
) -> Fulfillment:
    """Mark an order fulfilled and record the fulfillment."""
    moment = now or state.now()
    fulfillment = Fulfillment(
        id=state.next_fulfillment_id(),
        order_id=order.id,
        status="success",
        created_at=moment,
        tracking_company=tracking_company,
        tracking_number=tracking_number,
    )
    order.fulfillments.append(fulfillment)
    order.fulfillment_status = "fulfilled"
    order.updated_at = moment
    return fulfillment


def refund_order(
    state: StubState,
    order: Order,
    *,
    amount: Decimal | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> Refund:
    """Refund some or all of an order.

    Args:
        amount: how much to refund. ``None`` refunds the whole remaining total, which is the
            common case and the one the reconciler cares about.

    Returns:
        The recorded :class:`~shopify_stub.state.Refund`.

    Raises:
        ValueError: the refund would exceed what remains refundable. Real Shopify rejects
            that too, and silently clamping it would let a consumer's arithmetic bug pass.
    """
    moment = now or state.now()
    remaining = order.total_price - order.total_refunded
    value = remaining if amount is None else amount
    if value <= 0:
        raise ValueError("refund amount must be positive")
    if value > remaining:
        raise ValueError(f"refund of {value} exceeds refundable remainder {remaining}")
    refund = Refund(
        id=state.next_refund_id(),
        order_id=order.id,
        created_at=moment,
        amount=value,
        currency=order.currency,
        note=note,
    )
    order.refunds.append(refund)
    order.updated_at = moment
    order.financial_status = (
        "refunded" if order.total_refunded >= order.total_price else "partially_refunded"
    )
    return refund


# ---------------------------------------------------------------------------------------
# REST / webhook shapes (snake_case)
# ---------------------------------------------------------------------------------------


def _money_set(amount: Decimal, currency: str) -> dict[str, object]:
    """A REST ``*_set``: the amount in shop and presentment currency, both as strings."""
    value = {"amount": money(amount), "currency_code": currency}
    return {"shop_money": dict(value), "presentment_money": dict(value)}


def _discount_value_pair(order: Order, state: StubState) -> tuple[str, str]:
    """``(value, value_type)`` for ``discount_applications``.

    Shopify reports a percentage discount as ``value: "10.0"`` with
    ``value_type: "percentage"``, and a money-off discount as the amount with
    ``value_type: "fixed_amount"``. Reporting the *allocated money* under a
    ``percentage`` value type — the easy mistake — makes a 10% discount on a $100 order look
    like a 10-point discount and a 10% discount on a $50 order look like a 5-point one, so
    the two are kept apart here.
    """
    code = state.find_code(order.discount_code)
    if code is not None and code.percentage is not None:
        return (str(round(code.percentage * 100, 4)), "percentage")
    return (money(order.discount_amount), "fixed_amount")


def order_webhook_payload(order: Order, *, shop_domain: str, state: StubState) -> dict[str, object]:
    """The ``orders/paid`` and ``orders/fulfilled`` body, in the REST Admin order shape."""
    value, value_type = _discount_value_pair(order, state)
    return {
        "id": order.id,
        "admin_graphql_api_id": f"gid://shopify/Order/{order.id}",
        "name": order.name,
        "order_number": order.order_number,
        "number": order.order_number - 1000,
        "token": order.checkout_token,
        "checkout_token": order.checkout_token,
        "cart_token": order.cart_token,
        "created_at": iso(order.created_at),
        "processed_at": iso(order.processed_at),
        "updated_at": iso(order.updated_at),
        "currency": order.currency,
        "financial_status": order.financial_status,
        "fulfillment_status": order.fulfillment_status,
        "test": order.test,
        "subtotal_price": money(order.subtotal_price),
        "total_discounts": money(order.discount_amount),
        "total_price": money(order.total_price),
        "current_total_price": money(order.total_price - order.total_refunded),
        "total_price_set": _money_set(order.total_price, order.currency),
        "subtotal_price_set": _money_set(order.subtotal_price, order.currency),
        # `store_url`, not an f-string: this is a live link a merchant app may follow or
        # hand to a buyer, so it is held to the same bare-host guarantee as the cart
        # redirect. See `shopify_stub.permalink.store_url`.
        "order_status_url": store_url(
            shop_domain=shop_domain, path=f"/orders/{order.checkout_token}/authenticate"
        ),
        "discount_codes": (
            [
                {
                    "code": order.discount_code,
                    "amount": money(order.discount_amount),
                    "type": value_type,
                }
            ]
            if order.discount_code
            else []
        ),
        "discount_applications": (
            [
                {
                    "target_type": "line_item",
                    "type": "discount_code",
                    "value": value,
                    "value_type": value_type,
                    "allocation_method": "across",
                    "target_selection": "all",
                    "code": order.discount_code,
                }
            ]
            if order.discount_code
            else []
        ),
        "note_attributes": [
            {"name": CLIENT_ID_ATTRIBUTE, "value": order.client_id},
        ],
        "line_items": [
            {
                "id": item.variant_id,
                "admin_graphql_api_id": f"gid://shopify/LineItem/{item.variant_id}",
                "variant_id": item.variant_id,
                "product_id": item.product_id,
                "title": item.title,
                "name": item.title,
                "quantity": item.quantity,
                "current_quantity": item.quantity,
                "price": money(item.price),
                "price_set": _money_set(item.price, order.currency),
                "sku": item.sku,
                "total_discount": money(order.discount_amount),
            }
            for item in order.line_items
        ],
        "fulfillments": [
            # Exactly the seven keys the abbreviated fulfillment shape documents, and no
            # eighth: the webhook reference's own `fulfillments` array is empty in every
            # sample, so anything beyond those seven would be invention.
            {
                "created_at": iso(fulfillment.created_at),
                "id": fulfillment.id,
                "order_id": order.id,
                "status": fulfillment.status,
                "tracking_company": fulfillment.tracking_company,
                "tracking_number": fulfillment.tracking_number,
                "updated_at": iso(fulfillment.created_at),
            }
            for fulfillment in order.fulfillments
        ],
        # C5: no protected-customer-data scopes. See this module's docstring.
        "customer": None,
    }


def refund_webhook_payload(order: Order, refund: Refund) -> dict[str, object]:
    """The ``refunds/create`` body, in the REST Admin refund shape."""
    return {
        "id": refund.id,
        "admin_graphql_api_id": f"gid://shopify/Refund/{refund.id}",
        "order_id": order.id,
        "created_at": iso(refund.created_at),
        "processed_at": iso(refund.created_at),
        "note": refund.note,
        "user_id": None,
        "restock": True,
        "duties": [],
        "order_adjustments": [],
        "refund_shipping_lines": [],
        "return": None,
        "total_duties_set": _money_set(Decimal("0"), refund.currency),
        "refund_line_items": [
            {
                "id": refund.id + index,
                "line_item_id": item.variant_id,
                "location_id": None,
                "quantity": item.quantity,
                "restock_type": "return",
                "subtotal": money(item.line_total),
                "subtotal_set": _money_set(item.line_total, refund.currency),
                "total_tax": "0.00",
                "total_tax_set": _money_set(Decimal("0"), refund.currency),
            }
            for index, item in enumerate(order.line_items)
        ],
        "transactions": [
            {
                "id": refund.id + 500,
                "admin_graphql_api_id": f"gid://shopify/OrderTransaction/{refund.id + 500}",
                "order_id": order.id,
                "kind": "refund",
                "gateway": "bogus",
                "status": "success",
                "message": None,
                "amount": money(refund.amount),
                "currency": refund.currency,
                "created_at": iso(refund.created_at),
                "processed_at": iso(refund.created_at),
                "test": True,
            }
        ],
    }


# ---------------------------------------------------------------------------------------
# GraphQL shapes (camelCase, GIDs, MoneyBag)
# ---------------------------------------------------------------------------------------


def _money_bag(amount: Decimal, currency: str) -> dict[str, object]:
    """Shopify's ``MoneyBag``: the same amount in shop and presentment currency."""
    value = {"amount": money(amount), "currencyCode": currency}
    return {"shopMoney": dict(value), "presentmentMoney": dict(value)}


def cursor(raw: str) -> str:
    """An opaque, base64-looking cursor. Shopify's are opaque; consumers must not parse."""
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def is_cursor(value: str) -> bool:
    """``True`` iff ``value`` is syntactically a cursor at all.

    The check is deliberately *syntactic* and not "is this one of the cursors I just
    issued". A cursor is opaque by contract — consumers must not parse one, and symmetrically
    the server has no business demanding a particular payload inside it. What it can insist
    on is that the string is a well-formed encoding, which is exactly what catches the two
    real caller bugs:

    * passing ``node.id`` (``gid://shopify/Order/5500000000001``) where ``edge.cursor``
      belongs — the single most common paging mistake against a Relay connection;
    * a cursor that was truncated, re-wrapped, or lost its padding in transit.

    Both used to return a silent empty page, which reads to a consumer as "no more results"
    and ends the loop early with data missing and nothing logged. That is the same class of
    defect as a filter that silently matches everything, which this module already refuses
    (see ``_apply_search``).

    A syntactically valid cursor that names no row in the current set is a **different**
    thing and stays a normal empty page: it is what a cursor past the end of the set looks
    like, and erroring on it would break correct paging against a shrinking collection.
    """
    if not value:
        return False
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def order_graphql_node(order: Order, state: StubState) -> dict[str, object]:
    """One ``Order`` node as the GraphQL Admin ``orders`` query returns it.

    ``checkoutToken`` is on this type in the current API version — it is *not* a
    REST/webhook-only field, and it is the field that lets a consumer correlate an order
    with the web-pixel event without ever touching the webhook. Omitting it would have
    forced every reconciliation path through the webhook body.
    """
    financial = {
        "paid": "PAID",
        "refunded": "REFUNDED",
        "partially_refunded": "PARTIALLY_REFUNDED",
    }[order.financial_status]
    fulfillment = "FULFILLED" if order.fulfillment_status == "fulfilled" else "UNFULFILLED"
    value, value_type = _discount_value_pair(order, state)
    discount_value: dict[str, object]
    if value_type == "percentage":
        discount_value = {"percentage": float(value)}
    else:
        discount_value = {"amount": value, "currencyCode": order.currency}
    return {
        "id": f"gid://shopify/Order/{order.id}",
        "legacyResourceId": str(order.id),
        "name": order.name,
        "checkoutToken": order.checkout_token,
        "cartToken": order.cart_token,
        "createdAt": iso(order.created_at),
        "processedAt": iso(order.processed_at),
        "updatedAt": iso(order.updated_at),
        "displayFinancialStatus": financial,
        "displayFulfillmentStatus": fulfillment,
        "currencyCode": order.currency,
        "test": order.test,
        "discountCodes": [order.discount_code] if order.discount_code else [],
        "subtotalPriceSet": _money_bag(order.subtotal_price, order.currency),
        "totalDiscountsSet": _money_bag(order.discount_amount, order.currency),
        "totalPriceSet": _money_bag(order.total_price, order.currency),
        "currentTotalPriceSet": _money_bag(
            order.total_price - order.total_refunded, order.currency
        ),
        "totalRefundedSet": _money_bag(order.total_refunded, order.currency),
        "customAttributes": [
            {"key": CLIENT_ID_ATTRIBUTE, "value": order.client_id},
        ],
        # C5: no protected-customer-data scopes. See this module's docstring.
        "customer": None,
        "discountApplications": {
            "edges": (
                [
                    {
                        "cursor": cursor(f"discount:{order.id}"),
                        "node": {
                            "__typename": "DiscountCodeApplication",
                            "allocationMethod": "ACROSS",
                            "code": order.discount_code,
                            "index": 0,
                            "targetSelection": "ALL",
                            "targetType": "LINE_ITEM",
                            "value": discount_value,
                        },
                    }
                ]
                if order.discount_code
                else []
            )
        },
        "lineItems": {
            "edges": [
                {
                    "cursor": cursor(f"line:{item.variant_id}"),
                    "node": {
                        "id": f"gid://shopify/LineItem/{item.variant_id}",
                        "title": item.title,
                        "quantity": item.quantity,
                        "sku": item.sku,
                        "variant": {"id": f"gid://shopify/ProductVariant/{item.variant_id}"},
                        "originalTotalSet": _money_bag(item.line_total, order.currency),
                    },
                }
                for item in order.line_items
            ]
        },
    }


def orders_connection(
    orders: list[Order],
    state: StubState,
    *,
    first: int,
    after: str | None = None,
    reverse: bool = False,
) -> dict[str, object]:
    """Build the ``orders`` connection: ``edges``/``cursor``/``node`` plus ``pageInfo``.

    Cursor semantics match Shopify's: ``after`` is *exclusive*, and ``hasNextPage`` is true
    when the underlying set has more rows past the returned window. A consumer that pages
    correctly here pages correctly against Shopify; one that assumes ``after`` is inclusive
    double-counts an order, which in this system is a double-counted conversion.
    """
    ordered = sorted(orders, key=lambda order: order.id, reverse=reverse)
    cursors = [cursor(f"order:{order.id}") for order in ordered]
    start = 0
    if after is not None:
        start = cursors.index(after) + 1 if after in cursors else len(ordered)
    window = ordered[start : start + first]
    edges = [
        {"cursor": cursors[start + index], "node": order_graphql_node(order, state)}
        for index, order in enumerate(window)
    ]
    return {
        "edges": edges,
        "pageInfo": {
            "hasNextPage": start + first < len(ordered),
            "hasPreviousPage": start > 0,
            "startCursor": edges[0]["cursor"] if edges else None,
            "endCursor": edges[-1]["cursor"] if edges else None,
        },
    }
