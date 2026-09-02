"""The ASGI application. ``shopify_stub.app:app`` is the one name the repo pins.

The root ``conftest.py``'s ``shopify_stub_url`` fixture imports ``shopify_stub.app`` and
serves its ``app`` attribute on an ephemeral port (D41), so that module path and that
attribute name are a hard contract; everything else in this package is free to move.

The HTTP surface, in three groups
---------------------------------

**Shopify's own surface** — what a real store answers:

======================================== ======= ==================================
Route                                    Method  What it is
======================================== ======= ==================================
``/admin/api/{version}/graphql.json``    POST    Admin GraphQL (four root fields)
``/cart/{items}``                        GET     Cart permalink redemption
======================================== ======= ==================================

**The control plane** — everything under ``/_stub`` is *not* Shopify. The prefix is there so
that no consumer can mistake a test affordance for a real endpoint, and so that a grep for
``_stub`` finds every place a consumer has coupled itself to the stub rather than to the
API:

============================================== ======= =============================
Route                                          Method  What it is
============================================== ======= =============================
``/_stub/config``                              GET/PUT Read/modify the knobs
``/_stub/reset``                               POST    Wipe all state
``/_stub/seed``                                POST    Idempotent catalog seeding
``/_stub/checkouts/{token}``                   GET     Introspect a checkout
``/_stub/checkouts/{token}/complete``          POST    Complete → order + webhook
``/_stub/orders/{id}/fulfill``                 POST    → ``orders/fulfilled``
``/_stub/orders/{id}/refund``                  POST    → ``refunds/create``
``/_stub/events``                              GET     Emitted pixel events
``/_stub/events/suppressed``                   GET     Events that did NOT fire
``/_stub/webhooks/deliveries``                 GET     One row per delivery, with its
                                                       attempt count
============================================== ======= =============================

**Operational**: ``GET /healthz``.

Why the completion step is an explicit call rather than something the cart route does:
visiting a cart permalink is not a purchase. Collapsing the two would make it impossible to
test the case that matters most — a code applied to a cart that is then abandoned must not
consume its single use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from shopify_stub import graphql_admin
from shopify_stub.codes import RejectionReason, discount_amount_for
from shopify_stub.orders import (
    create_order_from_checkout,
    fulfil_order,
    iso,
    new_client_id,
    new_token,
    order_webhook_payload,
    refund_order,
    refund_webhook_payload,
)
from shopify_stub.permalink import store_url
from shopify_stub.state import (
    Checkout,
    PixelMode,
    StubConfig,
    StubState,
    Variant,
    WebhookTopic,
    money,
)
from shopify_stub.telemetry import PixelEmitter, collector_payload
from shopify_stub.webhooks import WebhookDispatcher

#: Timeout for the POST that carries a pixel event to the collector. Short: loopback only.
PIXEL_POST_TIMEOUT_SECONDS = 5.0


class Stub:
    """One stub instance: its state, its emitter, its dispatcher.

    Bundled rather than left as module globals so two stubs can run in one process without
    sharing a discount-code table — which is exactly what the per-test ``shopify_stub_url``
    fixture promises.
    """

    def __init__(self, config: StubConfig | None = None) -> None:
        self.state = StubState(config)
        self.emitter = PixelEmitter(self.state)
        self.dispatcher = WebhookDispatcher(self.state)

    def reset(self, config: StubConfig | None = None) -> None:
        self.state.reset(config)
        self.emitter = PixelEmitter(self.state)
        self.dispatcher = WebhookDispatcher(self.state)


def create_app(config: StubConfig | None = None) -> FastAPI:
    """Build a stub application with its own isolated state."""
    stub = Stub(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Nothing to open or close: the stub holds no connections. The hook exists because
        # the server runs with `lifespan="on"`, and an app without one logs a warning that
        # reads like a failure.
        yield

    application = FastAPI(
        title="ProxyShop Shopify stub",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    application.state.stub = stub
    application.include_router(_shopify_router(stub))
    application.include_router(_control_router(stub))

    @application.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "shop_domain": stub.state.config.shop_domain}

    return application


# ---------------------------------------------------------------------------------------
# Shopify's own surface
# ---------------------------------------------------------------------------------------


def _shopify_router(stub: Stub) -> APIRouter:
    router = APIRouter()

    @router.post("/admin/api/{version}/graphql.json")
    async def admin_graphql(
        version: str,
        request: Request,
        x_shopify_access_token: str | None = Header(default=None),
    ) -> Response:
        """The Admin GraphQL endpoint.

        ``version`` must be the configured one. It used to be captured and immediately
        discarded on the reasoning that "Shopify serves several versions at once and
        rejecting an unknown one would make this stub stricter than the thing it stands in
        for". That has it backwards. Shopify serves a *fixed, published set* of versions and
        answers **404** for anything outside it; accepting every string makes the stub
        **looser** than the real API, in the direction that costs the most: a consumer pinned
        to ``2019-04``, or carrying a typo, gets ``200`` and real data here and a ``404`` in
        production, with nothing in between to tell it.

        The version is not incidental either — it is a configured value the stub already
        treats as first-class, echoed in every webhook's ``X-Shopify-API-Version`` header and
        in ``webPixelCreate``'s ``apiVersion.handle``. Answering on a version the stub then
        contradicts in its own headers is the kind of quiet disagreement this whole ticket
        exists to eliminate.

        The refusal uses Shopify's HTTP-layer error shape: ``{"errors": "<string>"}`` — a
        bare string, not the list of error objects a GraphQL-level failure produces. See
        :class:`shopify_stub.graphql_admin.GraphQLHTTPError`.
        """
        configured = stub.state.config.api_version
        if version != configured:
            return JSONResponse(
                status_code=404,
                content={
                    "errors": (
                        f"Not Found: this stub answers the Admin API at version "
                        f"{configured!r}, and the request asked for {version!r}. The stub "
                        f"models exactly the version its recorded fixtures were derived "
                        f"from; a consumer pinned to any other version would pass here and "
                        f"404 against Shopify."
                    )
                },
            )
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                status_code=400, content={"errors": "Request body is not valid JSON"}
            )
        if not isinstance(body, dict) or not isinstance(body.get("query"), str):
            return JSONResponse(
                status_code=400,
                content={"errors": "Request body must carry a 'query' string"},
            )
        status, payload = graphql_admin.execute(
            stub.state,
            document=body["query"],
            variables=body.get("variables") if isinstance(body.get("variables"), dict) else None,
            access_token=x_shopify_access_token,
            now=stub.state.now(),
        )
        return JSONResponse(status_code=status, content=payload)

    @router.get("/cart/{items}")
    async def cart_permalink(items: str, request: Request) -> Response:
        """Cart-permalink redemption: ``/cart/{variant_id}:{quantity}?discount={code}``.

        **Acceptance criterion 2 lives here.** A code that is unknown, expired, not yet
        active, already used, or non-combinable is *silently ignored*: the cart is built
        without it, the response is a normal 303 to checkout, and nothing in the
        shopper-facing response says a code was refused. Only the control-plane route
        ``GET /_stub/checkouts/{token}`` reveals which rejection happened, and it is
        explicitly not part of Shopify's surface.

        A **structurally** broken link is different and is not silent: an unknown variant is
        a 404, because that is a bug in whoever built the link rather than a shopper
        pasting a stale coupon.

        Shopify's permalink format allows a comma in **two** places — a multi-variant path
        and a multi-code ``discount`` — and the README promises the stub "refuses the multi
        form explicitly rather than half-handling it". Only the path form kept that promise.
        The query form was silently accepted, looked up as one nonexistent code, and answered
        ``303`` with ``total_discount: 0.00`` — which is the worst available divergence
        direction: the stub applies *nothing* where production would apply a code, so a
        consumer's link looks fine here and discounts a real order. It is refused explicitly,
        exactly like the path form.
        """
        state = stub.state
        if "," in items:
            return JSONResponse(
                status_code=404,
                content={
                    "errors": (
                        "This stub implements the single-variant cart permalink only "
                        "(D22); multi-variant permalinks are out of scope."
                    )
                },
            )
        if "," in (request.query_params.get("discount") or ""):
            return JSONResponse(
                status_code=404,
                content={
                    "errors": (
                        "This stub implements the single-code cart permalink only (D22); "
                        "comma-separated multi-code discounts are out of scope. Accepting "
                        "one silently would quote a 0.00 discount for a link that discounts "
                        "a real order in production."
                    )
                },
            )
        variant_id, _, quantity_text = items.partition(":")
        if not variant_id.isdigit() or not quantity_text.isdigit():
            return JSONResponse(
                status_code=404, content={"errors": "Cart path must be <variant>:<quantity>"}
            )
        quantity = int(quantity_text)
        if quantity < 1:
            return JSONResponse(status_code=404, content={"errors": "Quantity must be >= 1"})
        variant = state.variants.get(int(variant_id))
        if variant is None or not variant.available:
            return JSONResponse(
                status_code=404, content={"errors": f"Variant {variant_id} is not available"}
            )

        requested_code = request.query_params.get("discount")
        applied, rejection = _evaluate_code(stub, requested_code)
        checkout = Checkout(
            token=new_token(),
            client_id=new_client_id(),
            variant_id=variant.variant_id,
            quantity=quantity,
            requested_code=requested_code,
            applied_code=applied,
            rejection=rejection,
            created_at=state.now(),
        )
        state.checkouts[checkout.token] = checkout

        subtotal = variant.price * quantity
        discount = _preview_discount(stub, applied, subtotal)
        # The body deliberately carries no hint that a code was refused: that is the
        # "silently ignores" contract. `discount_code` is null exactly as it would be if
        # the shopper had never supplied one.
        body = {
            "token": checkout.token,
            "client_id": checkout.client_id,
            "currency": variant.currency,
            "items": [
                {
                    "variant_id": variant.variant_id,
                    "product_id": variant.product_id,
                    "title": variant.title,
                    "quantity": quantity,
                    "price": money(variant.price),
                    "line_price": money(subtotal),
                }
            ],
            "discount_code": applied,
            "total_discount": money(discount),
            "items_subtotal_price": money(subtotal),
            "total_price": money(subtotal - discount),
        }
        return JSONResponse(
            status_code=303,
            content=body,
            headers={
                # Through `store_url`, never an f-string: this header is the one that
                # actually moves a shopper's browser, and a `shop_domain` of
                # `good.example.com@attacker.tld` interpolated raw here answered a real 303
                # to `attacker.tld` over real HTTP. `StubConfig` now refuses such a domain,
                # and this call refuses to render one even if it ever got past that.
                "Location": store_url(
                    shop_domain=state.config.shop_domain,
                    path=f"/checkouts/{checkout.token}",
                )
            },
        )

    return router


def _evaluate_code(stub: Stub, candidate: str | None) -> tuple[str | None, Any]:
    """Decide whether ``candidate`` applies, and record why not when it does not.

    Both halves of acceptance 2's "invalid/conflicting" split are decided here. *Invalid* is
    a property of the code (unknown, expired, not yet active, used up); *conflicting* is a
    property of the **cart** — a non-combining code meeting a shop that already has an
    order-level automatic discount running. Neither produces an error; both produce
    ``(None, reason)`` and a cart that looks exactly like one where no code was supplied.
    """
    if candidate in (None, ""):
        return None, None
    state = stub.state
    discount = state.find_code(candidate)
    if discount is None:
        return None, RejectionReason.UNKNOWN_CODE
    rejection = discount.rejection(
        now=state.now(),
        cart_has_order_discount=state.config.has_active_automatic_discount,
    )
    if rejection is not None:
        return None, rejection
    return discount.code, None


def _preview_discount(stub: Stub, applied: str | None, subtotal: Decimal) -> Decimal:
    """What the applied code takes off this cart, without redeeming it.

    Delegates to :func:`shopify_stub.codes.discount_amount_for` rather than repeating the
    arithmetic. The quote the shopper is shown here MUST equal the amount the order charges;
    a second copy of this calculation is how those two drift apart.
    """
    if applied is None:
        return Decimal("0")
    return discount_amount_for(stub.state.find_code(applied), subtotal)


# ---------------------------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------------------------


def _control_router(stub: Stub) -> APIRouter:  # noqa: C901 - a flat route table
    router = APIRouter(prefix="/_stub")

    @router.get("/config")
    async def read_config() -> dict[str, object]:
        config = stub.state.config
        return {
            "shop_domain": config.shop_domain,
            "api_version": config.api_version,
            "pixel_drop_rate": config.pixel_drop_rate,
            "pixel_mode": config.pixel_mode.value,
            "pixel_seed": config.pixel_seed,
            "pixel_collector_url": stub.state.pixel_collector_url,
            "has_active_automatic_discount": config.has_active_automatic_discount,
        }

    @router.put("/config")
    async def write_config(payload: dict[str, Any]) -> Response:
        """Change the knobs. Unknown keys are a 400, never a silent no-op.

        A typo'd knob name that returns 200 is how a test ends up asserting the *default*
        behaviour while believing it configured something.

        The write is **all-or-nothing**: a candidate config is built, validated, and only
        then swapped in. Mutating the live config field by field and validating afterwards
        leaves a rejected value in place — so a caller that sent ``pixel_drop_rate: -0.1``,
        saw its 400, and carried on would be running against a nonsense configuration it
        was explicitly told had been refused.
        """
        allowed = {
            "shop_domain",
            "api_version",
            "access_token",
            "webhook_secret",
            "pixel_drop_rate",
            "pixel_mode",
            "pixel_seed",
            "pixel_collector_url",
            "has_active_automatic_discount",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            return JSONResponse(
                status_code=400,
                content={"errors": f"unknown config keys: {', '.join(unknown)}"},
            )
        candidate = replace(stub.state.config)
        if "pixel_mode" in payload:
            try:
                candidate.pixel_mode = PixelMode(payload["pixel_mode"])
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "errors": (
                            f"pixel_mode must be one of "
                            f"{', '.join(mode.value for mode in PixelMode)}"
                        )
                    },
                )
        for key in ("shop_domain", "api_version", "access_token", "webhook_secret"):
            if key in payload:
                setattr(candidate, key, payload[key])
        if "pixel_drop_rate" in payload:
            try:
                candidate.pixel_drop_rate = float(payload["pixel_drop_rate"])
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=400, content={"errors": "pixel_drop_rate must be a number"}
                )
        if "pixel_seed" in payload:
            candidate.pixel_seed = payload["pixel_seed"]
        if "has_active_automatic_discount" in payload:
            candidate.has_active_automatic_discount = bool(payload["has_active_automatic_discount"])
        try:
            candidate.validate()
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"errors": str(exc)})
        stub.state.config = candidate
        if "pixel_collector_url" in payload:
            stub.state.pixel_collector_url = payload["pixel_collector_url"]
        stub.emitter.reseed()
        return JSONResponse(status_code=200, content=await read_config())

    @router.post("/reset")
    async def reset() -> dict[str, object]:
        """Wipe every collection, keeping the current configuration."""
        stub.reset(stub.state.config)
        return {"status": "reset"}

    @router.post("/seed")
    async def seed(payload: dict[str, Any]) -> Response:
        """Idempotently upsert catalog variants.

        Body: ``{"variants": [{"variant_id", "product_id", "title", "price",
        "currency"?, "sku"?, "available"?}, …]}``.

        Returns ``{"created": n, "unchanged": m, "updated": k}``. Idempotency is the point:
        the seeder that drives this runs from ``make demo-seed`` and must be safe to run
        twice, so a variant whose payload is byte-identical to what is already stored counts
        as *unchanged* rather than being rewritten.

        The seed is **all-or-nothing**. Parsing every variant before storing any of them
        matters more here than it looks: a seed that half-applied and then 400'd would leave
        the store in a state no rerun reproduces — the first N variants present, the rest
        absent — and the whole value of a seeded demo is that it is reproducible.
        """
        variants = payload.get("variants")
        if not isinstance(variants, list):
            return JSONResponse(
                status_code=400, content={"errors": "body must carry a 'variants' list"}
            )
        parsed: list[Variant] = []
        for raw in variants:
            if not isinstance(raw, dict):
                return JSONResponse(
                    status_code=400, content={"errors": "each variant must be an object"}
                )
            try:
                parsed.append(
                    Variant(
                        variant_id=int(raw["variant_id"]),
                        product_id=int(raw["product_id"]),
                        title=str(raw["title"]),
                        price=Decimal(str(raw["price"])),
                        currency=str(raw.get("currency", "USD")),
                        sku=str(raw.get("sku", "")),
                        available=bool(raw.get("available", True)),
                    )
                )
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                return JSONResponse(status_code=400, content={"errors": f"invalid variant: {exc}"})
        created = unchanged = updated = 0
        for variant in parsed:
            existing = stub.state.variants.get(variant.variant_id)
            if existing is None:
                created += 1
            elif existing == variant:
                unchanged += 1
                continue
            else:
                updated += 1
            stub.state.variants[variant.variant_id] = variant
        return JSONResponse(
            status_code=200,
            content={"created": created, "unchanged": unchanged, "updated": updated},
        )

    @router.get("/codes")
    async def read_codes() -> dict[str, object]:
        """The discount-code table, plus D22's ``offer_id`` index.

        Shopify has no endpoint that answers "which code belongs to offer X" — the index is
        the *app's* bookkeeping, not the platform's. It is exposed here so a test can prove
        the code was stored under the offer id without also proving it was derived from it.

        Two views of the same index, because they answer different questions:

        ``by_offer``
            ``offer_id`` -> the **most recently minted** code for it. Every key is a key of
            ``codes``, so ``codes[by_offer[offer_id]]`` always resolves — which it did not
            before: the index stored the code as created while ``codes`` is keyed upper-case,
            so a lowercase-created code produced a ``KeyError`` on the obvious lookup.
        ``codes_by_offer``
            ``offer_id`` -> **every** code minted for it, in creation order. The single-code
            view is last-write-wins by construction, so a second mint for one offer used to
            vanish from the index while staying live and redeemable in ``codes``. Whether an
            offer may hold two live codes is T-052's question; the stub's job is to let the
            answer be observed rather than to hide it.
        """
        return {
            "codes": {
                code: {
                    "code": discount.code,
                    "offer_id": discount.offer_id,
                    "usage_limit": discount.usage_limit,
                    "usage_count": discount.usage_count,
                    "starts_at": iso(discount.starts_at) if discount.starts_at else None,
                    "ends_at": iso(discount.ends_at) if discount.ends_at else None,
                    "combines_with": discount.combines_with.to_wire(),
                }
                for code, discount in stub.state.codes.items()
            },
            "by_offer": {
                offer_id: minted[-1] for offer_id, minted in stub.state.codes_by_offer.items()
            },
            "codes_by_offer": {
                offer_id: list(minted) for offer_id, minted in stub.state.codes_by_offer.items()
            },
        }

    @router.get("/checkouts/{token}")
    async def read_checkout(token: str) -> Response:
        """Introspect a checkout, including *why* a discount code did not apply.

        This is the only place the rejection reason is visible. It is under ``/_stub`` for
        that reason: Shopify tells the shopper nothing, and a test that wants to know which
        silent no-op happened has to ask a question Shopify does not answer.
        """
        checkout = stub.state.checkouts.get(token)
        if checkout is None:
            return JSONResponse(status_code=404, content={"errors": "no such checkout"})
        return JSONResponse(
            status_code=200,
            content={
                "token": checkout.token,
                "client_id": checkout.client_id,
                "variant_id": checkout.variant_id,
                "quantity": checkout.quantity,
                "requested_discount_code": checkout.requested_code,
                "applied_discount_code": checkout.applied_code,
                "rejection_reason": (checkout.rejection.value if checkout.rejection else None),
                "completed": checkout.completed,
                "order_id": checkout.order_id,
                "created_at": iso(checkout.created_at),
            },
        )

    @router.post("/checkouts/{token}/complete")
    async def complete_checkout(token: str) -> Response:
        """Complete a checkout: create the order, fire the pixel, deliver ``orders/paid``.

        Ordering is deliberate and observable: the pixel is attempted **before** the webhook
        is dispatched, mirroring a real store where the browser beacon leaves first. A
        consumer must never depend on that ordering — the whole point of T-061 is that the
        pixel may never arrive — but a stub that fired them the other way round would hide
        an ordering bug in a consumer that does.
        """
        state = stub.state
        checkout = state.checkouts.get(token)
        if checkout is None:
            return JSONResponse(status_code=404, content={"errors": "no such checkout"})
        try:
            order = create_order_from_checkout(state, checkout)
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"errors": str(exc)})

        now = state.now()
        event = stub.emitter.emit_checkout_completed(checkout=checkout, order=order, now=now)
        posted = False
        if event is not None and state.pixel_collector_url:
            posted = await _post_pixel(
                state.pixel_collector_url,
                collector_payload(
                    checkout=checkout,
                    order=order,
                    degraded=state.config.pixel_mode is PixelMode.PARTIAL,
                ),
            )
        deliveries = await stub.dispatcher.dispatch(
            topic=WebhookTopic.ORDERS_PAID,
            payload=order_webhook_payload(order, shop_domain=state.config.shop_domain, state=state),
            now=now,
        )
        return JSONResponse(
            status_code=201,
            content={
                "order_id": order.id,
                "order_name": order.name,
                "checkout_token": order.checkout_token,
                "client_id": order.client_id,
                "discount_code": order.discount_code,
                "total_price": money(order.total_price),
                "pixel_event_emitted": event is not None,
                "pixel_event_posted": posted,
                "webhook_deliveries": [_delivery_summary(d) for d in deliveries],
            },
        )

    @router.post("/orders/{order_id}/fulfill")
    async def fulfill(order_id: int, payload: dict[str, Any] | None = None) -> Response:
        state = stub.state
        order = state.orders.get(order_id)
        if order is None:
            return JSONResponse(status_code=404, content={"errors": "no such order"})
        body = payload or {}
        fulfil_order(
            state,
            order,
            tracking_company=body.get("tracking_company"),
            tracking_number=body.get("tracking_number"),
        )
        deliveries = await stub.dispatcher.dispatch(
            topic=WebhookTopic.ORDERS_FULFILLED,
            payload=order_webhook_payload(order, shop_domain=state.config.shop_domain, state=state),
        )
        return JSONResponse(
            status_code=200,
            content={
                "order_id": order.id,
                "fulfillment_status": order.fulfillment_status,
                "webhook_deliveries": [_delivery_summary(d) for d in deliveries],
            },
        )

    @router.post("/orders/{order_id}/refund")
    async def refund(order_id: int, payload: dict[str, Any] | None = None) -> Response:
        state = stub.state
        order = state.orders.get(order_id)
        if order is None:
            return JSONResponse(status_code=404, content={"errors": "no such order"})
        body = payload or {}
        raw_amount = body.get("amount")
        try:
            amount = Decimal(str(raw_amount)) if raw_amount is not None else None
            record = refund_order(state, order, amount=amount, note=body.get("note"))
        except (ValueError, InvalidOperation) as exc:
            return JSONResponse(status_code=409, content={"errors": str(exc)})
        deliveries = await stub.dispatcher.dispatch(
            topic=WebhookTopic.REFUNDS_CREATE,
            payload=refund_webhook_payload(order, record),
        )
        return JSONResponse(
            status_code=201,
            content={
                "refund_id": record.id,
                "order_id": order.id,
                "amount": money(record.amount),
                "financial_status": order.financial_status,
                "webhook_deliveries": [_delivery_summary(d) for d in deliveries],
            },
        )

    @router.get("/events")
    async def events() -> dict[str, object]:
        return {
            "events": [
                {
                    "id": event.id,
                    "name": event.name,
                    "timestamp": iso(event.timestamp),
                    "client_id": event.client_id,
                    "payload": event.payload,
                }
                for event in stub.state.pixel_events
            ]
        }

    @router.get("/events/suppressed")
    async def suppressed() -> dict[str, object]:
        return {
            "suppressed": [
                {
                    "checkout_token": item.checkout_token,
                    "client_id": item.client_id,
                    "reason": item.reason.value,
                    "at": iso(item.at),
                }
                for item in stub.state.suppressed_events
            ]
        }

    @router.get("/webhooks/deliveries")
    async def deliveries() -> dict[str, object]:
        return {"deliveries": [_delivery_summary(d) for d in stub.state.deliveries]}

    return router


def _delivery_summary(delivery: Any) -> dict[str, object]:
    return {
        "id": delivery.id,
        "topic": delivery.topic.value,
        "callback_url": delivery.callback_url,
        "webhook_id": delivery.webhook_id,
        "hmac": delivery.hmac,
        "attempts": delivery.attempts,
        "delivered": delivery.delivered,
        "status_code": delivery.status_code,
        "error": delivery.error,
        "sent_at": iso(delivery.sent_at),
    }


async def _post_pixel(url: str, payload: dict[str, object]) -> bool:
    """POST an emitted pixel event to the collector.

    Returns ``False`` on any failure and **never raises**: a browser beacon that cannot
    reach the collector is one more way the pixel is lossy, and turning it into a 500 on the
    checkout-completion call would make the pixel able to break the order — the precise
    inversion of "the webhook is the truth".
    """
    try:
        async with httpx.AsyncClient(timeout=PIXEL_POST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
        return 200 <= response.status_code < 300
    except httpx.HTTPError:
        return False


#: The module-level application the root ``conftest.py`` serves.
app = create_app()
