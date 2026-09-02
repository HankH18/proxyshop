"""HTTP surface for the install: the OAuth pair and the three webhook receivers.

Discovered and mounted by the frozen ``merchant_svc.main.create_app`` because this file is
``<feature>/routes.py`` and exports ``router``.

``POST /webhooks/shopify/*`` is the route DESIGN pins for order webhooks. It is the half of
acceptance criterion 3 that "receivable" refers to: a subscription that is registered but
lands on a receiver which cannot authenticate it is a subscription that reconciles nothing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from merchant_svc.install.admin import AdminAPIError, AdminGraphQLClient
from merchant_svc.install.config import app_config
from merchant_svc.install.flow import InstallFailed, install
from merchant_svc.install.oauth import (
    OAuthCallbackRejected,
    authorize_url,
    new_state,
    read_callback,
)
from merchant_svc.install.scopes import REQUIRED_SCOPES, ProtectedScopeRequested
from merchant_svc.install.shop import InvalidShopDomain, normalize_shop_domain
from merchant_svc.install.tokens import TOKENS
from merchant_svc.install.webhooks import handle_delivery

router = APIRouter()

#: ``state`` nonces this process issued, mapped to the shop they were issued for. Bounded
#: so a flood of unfinished installs cannot grow it without limit.
_PENDING_STATES: dict[str, str] = {}
_PENDING_LIMIT = 256


def _remember_state(state: str, shop: str) -> None:
    if len(_PENDING_STATES) >= _PENDING_LIMIT:
        _PENDING_STATES.pop(next(iter(_PENDING_STATES)))
    _PENDING_STATES[state] = shop


def _problem(status: int, reason: str, **detail: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": reason, **detail})


@router.get("/install")
def start_install(shop: str) -> Any:
    """Begin an install: redirect the merchant to Shopify's authorize screen."""
    config = app_config()
    if not config.api_key or not config.api_secret:
        return _problem(503, "app-not-configured", missing=["SHOPIFY_API_KEY/SECRET"])
    try:
        shop_domain = normalize_shop_domain(shop)
    except InvalidShopDomain as exc:
        return _problem(400, "invalid-shop", detail=str(exc))
    state = new_state()
    _remember_state(state, shop_domain)
    try:
        target = authorize_url(
            shop_domain,
            client_id=config.api_key,
            redirect_uri=f"{config.app_url}/install/callback",
            state=state,
            scopes=REQUIRED_SCOPES,
        )
    except ProtectedScopeRequested as exc:  # pragma: no cover - constant is C5-clean
        return _problem(500, "forbidden-scope", detail=str(exc))
    return RedirectResponse(target, status_code=302)


@router.get("/install/callback")
def finish_install(request: Request) -> Any:
    """Verify Shopify's redirect, exchange the code, store the offline token, install."""
    from merchant_svc.install.oauth import exchange_code

    config = app_config()
    if not config.api_key or not config.api_secret:
        return _problem(503, "app-not-configured", missing=["SHOPIFY_API_KEY/SECRET"])

    params = dict(request.query_params)
    supplied_state = str(params.get("state", ""))
    expected_shop = _PENDING_STATES.pop(supplied_state, None)
    if expected_shop is None:
        return _problem(400, "unknown-state")
    try:
        callback = read_callback(params, secret=config.api_secret, expected_state=supplied_state)
    except OAuthCallbackRejected as exc:
        return _problem(401, "callback-rejected", detail=str(exc))
    if callback.shop_domain != expected_shop:
        return _problem(400, "shop-mismatch", expected=expected_shop, got=callback.shop_domain)

    try:
        token = exchange_code(
            callback.shop_domain,
            callback.code,
            client_id=config.api_key,
            client_secret=config.api_secret,
        )
    except OAuthCallbackRejected as exc:
        return _problem(502, "token-exchange-failed", detail=str(exc))

    try:
        with AdminGraphQLClient(shop_domain=callback.shop_domain, access_token=token) as admin:
            result = install(callback.shop_domain, admin, access_token=token, tokens=TOKENS)
    except (AdminAPIError, InstallFailed) as exc:
        return _problem(502, "install-failed", detail=str(exc))

    return {
        "shop": result.shop_domain,
        "scopes": list(result.scopes),
        "web_pixel_id": result.web_pixel_id,
        "webhooks": [
            {
                "topic": registration.topic,
                "uri": registration.callback_url,
                "already_registered": registration.already_registered,
            }
            for registration in result.webhooks
        ],
        "offline_token_stored": result.offline_token_stored,
    }


@router.get("/install/shops")
def installed_shops() -> dict[str, list[str]]:
    """Every shop this process holds an offline token for."""
    return {"shops": list(TOKENS.shops())}


@router.post("/webhooks/shopify/{resource}/{action}")
async def receive_webhook(resource: str, action: str, request: Request) -> Any:
    """Authenticate and record one order webhook delivery.

    The body is read as **bytes** and never re-serialised: the signature covers the exact
    wire bytes, so a receiver that hashes a re-encoded parse rejects legitimate deliveries
    the moment key order or spacing differs.
    """
    body = await request.body()
    decision = handle_delivery(
        body=body,
        headers=dict(request.headers),
        secret=app_config().api_secret,
        path_topic=f"{resource}/{action}",
    )
    if not decision.accepted:
        return _problem(decision.status_code, decision.reason, **decision.detail)
    return JSONResponse(
        status_code=200,
        content={
            "status": decision.reason,
            "topic": decision.event.topic if decision.event else None,
            "duplicate": decision.duplicate,
        },
    )
