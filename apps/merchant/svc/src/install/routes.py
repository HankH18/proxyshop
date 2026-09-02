"""HTTP surface for the install: the OAuth pair and the webhook receiver.

Discovered and mounted by the frozen ``merchant_svc.main.create_app`` because this file is
``<feature>/routes.py`` and exports ``router``.

``POST /webhooks/shopify/{topic}`` is the route DESIGN pins for order webhooks — **one**
path parameter, as ``packages/contracts`` pins it. It is the half of acceptance criterion 3
that "receivable" refers to: a subscription that is registered but lands on a receiver which
cannot authenticate it is a subscription that reconciles nothing.

Three things here are not obvious and are load-bearing:

* **The topic path parameter is a ``:path`` converter.** The contract pins one parameter,
  ``{topic}``, and a topic's canonical spelling contains a slash (``orders/paid``). A plain
  ``{topic}`` would not match the URL Shopify actually delivers to; two parameters matched
  the URL but served a path no contract-generated consumer would ever call. The ``:path``
  converter matches both spellings and *renders* in the OpenAPI document as the pinned
  ``/webhooks/shopify/{topic}``, so the served route table and the frozen contract agree —
  which ``tests/test_merchant_hardening.py`` asserts, because nothing did before.
* **``GET /install/shops`` is guarded.** It returns the merchant customer list.
* **``GET /install`` holds no server-side state.** See
  :func:`~merchant_svc.install.oauth.issue_install_state`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from merchant_svc.http_limits import BodyTooLarge, read_capped_body
from merchant_svc.install.admin import AdminAPIError, AdminGraphQLClient
from merchant_svc.install.config import WEBHOOK_PATH_PREFIX, admin_base_url, app_config
from merchant_svc.install.flow import InstallFailed, install
from merchant_svc.install.oauth import (
    INSTALL_STATE_TTL_SECONDS,
    InstallStateRejected,
    OAuthCallbackRejected,
    authorize_url,
    issue_install_state,
    read_callback,
    read_install_state,
)
from merchant_svc.install.scopes import REQUIRED_SCOPES, ProtectedScopeRequested
from merchant_svc.install.shop import InvalidShopDomain, normalize_shop_domain
from merchant_svc.install.signatures import secure_equals
from merchant_svc.install.tokens import TOKENS
from merchant_svc.install.webhooks import handle_delivery

_log = logging.getLogger(__name__)

router = APIRouter()

#: The bearer token that may read the administrative routes. There is no default and no
#: fallback: a deployment that has not set it serves nobody (503), because the alternative —
#: an empty expected value compared against an empty supplied one — is a route that opens
#: itself the moment the environment is incomplete.
ADMIN_TOKEN_ENV = "MERCHANT_ADMIN_TOKEN"

#: States already redeemed, mapped to the instant they stop being worth remembering.
#:
#: This is the ONLY residual process-local state in the install flow, and its failure mode is
#: deliberately the safe one. Losing an entry — to eviction, to a restart, to landing on
#: another replica — can only allow a *replay* of a callback whose state has not yet expired,
#: and a replayed callback still needs an authorization code Shopify has already spent.
#: Losing an entry can never break a merchant's install, which is precisely what the
#: pending-state map it replaced did under 256 anonymous requests.
_CONSUMED_STATES: dict[str, float] = {}

#: How many redeemed states to remember. Reaching it is logged, not silent: it means either
#: an install volume this bound is too small for, or somebody trying to age one out.
_CONSUMED_LIMIT = 4096

#: ``finish_install`` is a synchronous ``def``, so FastAPI runs it in the anyio threadpool and
#: two callbacks really are concurrent. Without this lock the prune loop below iterated the
#: dict while another thread inserted into it — measured as a 500 (`RuntimeError: dictionary
#: changed size during iteration`) on a *legitimate* install under 600 concurrent callbacks.
#: A replay guard that fails a real merchant is worse than the replay it was guarding against.
_CONSUMED_LOCK = threading.Lock()


def _consume_state(state: str, *, now: float | None = None) -> bool:
    """Mark ``state`` spent. ``False`` when it was already spent (a replay). Thread-safe."""
    moment = now if now is not None else time.time()
    with _CONSUMED_LOCK:
        for key in [key for key, expiry in _CONSUMED_STATES.items() if expiry <= moment]:
            del _CONSUMED_STATES[key]
        if state in _CONSUMED_STATES:
            return False
        _CONSUMED_STATES[state] = moment + INSTALL_STATE_TTL_SECONDS
        while len(_CONSUMED_STATES) > _CONSUMED_LIMIT:
            del _CONSUMED_STATES[next(iter(_CONSUMED_STATES))]
            _log.warning(
                "the redeemed-install-state window is full (%d entries) and is evicting "
                "un-expired entries; a callback replayed within %ds would not be caught",
                _CONSUMED_LIMIT,
                INSTALL_STATE_TTL_SECONDS,
            )
    return True


#: A redeemed state is NEVER released, not even when the step after it failed.
#:
#: Releasing it on a failed token exchange was tried and reverted: the callback URL carries
#: `code`, `hmac` AND `state`, so it leaks as a unit through browser history, a Referer, or
#: any proxy log — and a failed exchange is precisely the case where Shopify has NOT yet
#: consumed the `code`. Making it redeemable again therefore hands whoever holds that URL a
#: live install ticket for the offline token. The merchant restarting at `GET /install` is a
#: recoverable annoyance; the other direction is not recoverable at all. Stated here because
#: "retry after a transient failure" is the obvious-looking change, and it is the wrong one.


def _problem(status: int, reason: str, **detail: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": reason, **detail})


def _refuse_unless_admin(request: Request) -> JSONResponse | None:
    """``None`` when the caller may read an administrative route; a refusal otherwise.

    ``GET /install/shops`` shipped with no authentication of any kind — ``create_app()``
    installs no middleware and the route carried no dependency — so an anonymous request
    enumerated every shop the app is installed on. That list is the merchant customer list
    and a target list for everything downstream of it.
    """
    configured = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
    if not configured:
        return _problem(
            503,
            "admin-api-not-configured",
            missing=[ADMIN_TOKEN_ENV],
            detail="administrative routes refuse every caller until a token is configured",
        )
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not secure_equals(configured, supplied.strip()):
        return _problem(401, "unauthorized")
    return None


@router.get("/install")
def start_install(shop: str) -> Any:
    """Begin an install: redirect the merchant to Shopify's authorize screen."""
    config = app_config()
    # `.strip()`: a secret of " " is not a configured secret, and treating it as one issues
    # states nobody can verify and refuses every webhook while looking configured.
    if not config.api_key.strip() or not config.api_secret.strip():
        return _problem(503, "app-not-configured", missing=["SHOPIFY_API_KEY/SECRET"])
    try:
        shop_domain = normalize_shop_domain(shop)
    except InvalidShopDomain as exc:
        return _problem(400, "invalid-shop", detail=str(exc))
    # No server-side slot is written here on purpose: this route is unauthenticated, and a
    # bounded map it wrote into was a map any anonymous caller could flush.
    state = issue_install_state(shop_domain, secret=config.api_secret)
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
    if not config.api_key.strip() or not config.api_secret.strip():
        return _problem(503, "app-not-configured", missing=["SHOPIFY_API_KEY/SECRET"])

    params = dict(request.query_params)
    supplied_state = str(params.get("state", ""))
    try:
        expected_shop = read_install_state(supplied_state, secret=config.api_secret)
    except InstallStateRejected:
        # One answer for a forged state, a tampered one and an expired one: telling them
        # apart is a signal handed to whoever is guessing. The wording is unchanged from the
        # map-backed version so an operator's runbook still matches.
        return _problem(400, "unknown-state")
    try:
        callback = read_callback(params, secret=config.api_secret, expected_state=supplied_state)
    except OAuthCallbackRejected as exc:
        return _problem(401, "callback-rejected", detail=str(exc))
    if callback.shop_domain != expected_shop:
        return _problem(400, "shop-mismatch", expected=expected_shop, got=callback.shop_domain)

    # Spend the state only now, once the callback is otherwise fully verified. Consuming it
    # any earlier would let a third party who merely *saw* a state — it travels in a URL —
    # burn a merchant's in-flight install by presenting it with a wrong shop or a bad
    # signature, which is the same denial of service the pending-state map allowed, moved.
    if not _consume_state(supplied_state):
        return _problem(400, "unknown-state")

    try:
        token = exchange_code(
            callback.shop_domain,
            callback.code,
            client_id=config.api_key,
            client_secret=config.api_secret,
            # C9: the same origin override every other Shopify call honours. Without it the
            # token exchange was the one step of the install that always reached the public
            # internet, so an offline run answered 500 from an unhandled transport error
            # instead of the 502 an unreachable token endpoint is supposed to produce.
            base_url=admin_base_url(callback.shop_domain),
        )
    except (OAuthCallbackRejected, httpx.HTTPError) as exc:
        # `httpx.HTTPError` too, and it is the case that actually happens: `exchange_code`
        # converts a refusal and an unparseable answer into `OAuthCallbackRejected`, but a
        # token endpoint that is unreachable or slow raises `ConnectError`/`ReadTimeout`,
        # which escaped and became a 500 — a status Shopify's own retry semantics and every
        # uptime check read as "this app is broken" rather than "Shopify was unreachable".
        # The state stays spent — see the note above `_CONSUMED_STATES` on why.
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
def installed_shops(request: Request) -> Any:
    """Every shop this process holds an offline token for. Administrative; guarded."""
    refusal = _refuse_unless_admin(request)
    if refusal is not None:
        return refusal
    return {"shops": list(TOKENS.shops())}


@router.post(f"{WEBHOOK_PATH_PREFIX}/{{topic:path}}")
async def receive_webhook(topic: str, request: Request) -> Any:
    """Authenticate and record one order webhook delivery.

    The body is read as **bytes** and never re-serialised: the signature covers the exact
    wire bytes, so a receiver that hashes a re-encoded parse rejects legitimate deliveries
    the moment key order or spacing differs.

    ``topic`` here is the path segment, and it is passed to :func:`handle_delivery` as a
    *constraint* only. Nothing signs a URL, so the topic a delivery is filed under comes
    from ``X-Shopify-Topic`` and a path that disagrees is a 400 rather than an override.
    """
    try:
        # Capped: the signature is computed over these bytes, so there is no ordering in
        # which this route can authenticate before it reads. An unbounded read on a route
        # anyone can POST to is a denial of service that costs the sender one connection.
        body = await read_capped_body(request)
    except BodyTooLarge:
        return _problem(413, "body-too-large")
    decision = handle_delivery(
        body=body,
        headers=dict(request.headers),
        secret=app_config().api_secret,
        path_topic=topic,
    )
    if not decision.accepted:
        return _problem(decision.status_code, decision.reason, **decision.detail)
    return JSONResponse(
        status_code=200,
        content={
            "status": decision.reason,
            "topic": decision.event.topic if decision.event else None,
            "duplicate": decision.duplicate,
            # e.g. `recorded_as`, when this body is already spoken for by another topic.
            **decision.detail,
        },
    )
