"""``POST /codes`` — mint a single-use discount code and permalink for a winning offer.

Discovered and mounted by the frozen ``merchant_svc.main.create_app`` because this file is
``<feature>/routes.py`` and exports ``router``. The path, the request body and the 201 body
come from ``packages/contracts/openapi/merchant.openapi.json``, which
``test_the_served_routes_match_the_pinned_contract`` grades the served table against — and
``/codes`` is the only path this package may serve.

**This route is privileged.** It creates a real, spendable discount on the merchant's
Shopify account. An anonymous caller who could reach it could mint discounts at will, so it
sits behind the same bearer token ``GET /install/shops`` and the envelope routes do, and it
refuses every caller until one is configured. The rule is written out here rather than
imported from ``install.routes``' private helper for the reason that module states: the
constant is shared, so the two cannot drift about *which* token, while an unconfigured
deployment refuses here exactly as it does there.

**Status codes carry the distinction the domain makes.** A ``combinesWith`` conflict is a
409 — the shop's own configuration refuses this offer and retrying changes nothing until a
human edits the shop. A configuration we could not *read* is a 503 — nothing is wrong with
the offer and the call is worth retrying. Collapsing those two into one status would erase
exactly the difference an operator needs at 3am.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from merchant_svc.http_limits import BodyTooLarge, read_capped_body
from merchant_svc.install.admin import AdminAPIError, AdminGraphQLClient
from merchant_svc.install.routes import ADMIN_TOKEN_ENV
from merchant_svc.install.shop import InvalidShopDomain
from merchant_svc.install.signatures import secure_equals
from merchant_svc.install.tokens import TOKENS, OfflineTokenStore, ShopNotInstalled

from ._spellings import bind_spellings
from .combines import ShopConfigurationUnavailable
from .create import CodeCreationRefused, CombinesWithConflict, create_code
from .offer import UnusableOffer, shop_domain_for

_log = logging.getLogger(__name__)

router = APIRouter()

#: The path the frozen contract pins. Spelled once.
CODES_PATH = "/codes"


def _problem(status: int, reason: str, **detail: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": reason, **detail})


def _refuse_unless_admin(request: Request) -> JSONResponse | None:
    """``None`` when the caller may mint a discount; a refusal otherwise."""
    configured = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
    if not configured:
        return _problem(
            503,
            "admin-api-not-configured",
            missing=[ADMIN_TOKEN_ENV],
            detail="POST /codes refuses every caller until a token is configured",
        )
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not secure_equals(configured, supplied.strip()):
        return _problem(401, "unauthorized")
    return None


def admin_client_for(store_id: str, offer: Any, *, tokens: OfflineTokenStore = TOKENS) -> Any:
    """An Admin GraphQL client for the shop this offer belongs to.

    The shop host is resolved from the **store**, not from the offer's ``checkout_url`` —
    see :func:`~merchant_svc.codes.offer.shop_domain_for`, which refuses an offer naming a
    different host rather than following it.

    Raises:
        ShopNotInstalled: no offline token is on file, so the app is not installed there.
        UnusableOffer / InvalidShopDomain: the store names no shop host that could exist.
    """
    shop_domain = shop_domain_for(store_id, offer)
    token = tokens.require(shop_domain)
    return AdminGraphQLClient(shop_domain=shop_domain, access_token=token.access_token)


@router.post(CODES_PATH, status_code=201)
async def create_discount_code(request: Request) -> Any:
    """Mint one single-use code for one accepted offer, and return its permalink.

    The body is ``{"store_id": ..., "offer": {...}}``, and the 201 is exactly
    ``{"code", "permalink_url", "expires_at"}`` — the pinned contract's three keys, no
    more. Everything else the mint produced (the ledger event, the validity window's start,
    the applied percentage) stays on this side of the wire; a caller that needs it reads the
    ledger, which is where an auditor looks anyway.
    """
    refusal = _refuse_unless_admin(request)
    if refusal is not None:
        return refusal

    try:
        raw = await read_capped_body(request)
    except BodyTooLarge:
        return _problem(413, "body-too-large")
    try:
        submitted = json.loads(raw)
    except Exception:  # noqa: BLE001 - any decode failure is the same 400, never a 500
        return _problem(400, "unparseable-body")
    if not isinstance(submitted, dict):
        return _problem(400, "not-a-code-request", detail=f"got a {type(submitted).__name__}")

    store_id = submitted.get("store_id")
    offer = submitted.get("offer")
    if not isinstance(store_id, str) or not store_id.strip():
        return _problem(400, "not-a-code-request", detail="store_id must be a non-empty string")
    if not isinstance(offer, dict):
        return _problem(400, "not-a-code-request", detail="offer must be an object")

    try:
        client = admin_client_for(store_id, offer)
    except ShopNotInstalled as exc:
        return _problem(404, "shop-not-installed", store_id=store_id, detail=str(exc))
    except (UnusableOffer, InvalidShopDomain) as exc:
        return _problem(400, "unusable-offer", store_id=store_id, detail=str(exc))

    try:
        created = create_code(store_id, offer, client)
    except CombinesWithConflict as exc:
        _log.info("store %r: no code minted, combinesWith conflict: %s", store_id, exc)
        return _problem(409, "combines-with-conflict", store_id=store_id, detail=str(exc))
    except ShopConfigurationUnavailable as exc:
        _log.warning("store %r: shop discount configuration unreadable: %s", store_id, exc)
        return _problem(503, "shop-configuration-unavailable", store_id=store_id, detail=str(exc))
    except UnusableOffer as exc:
        return _problem(400, "unusable-offer", store_id=store_id, detail=str(exc))
    except (CodeCreationRefused, AdminAPIError) as exc:
        _log.warning("store %r: the Admin API refused the mint: %s", store_id, exc)
        return _problem(502, "code-not-created", store_id=store_id, detail=str(exc))
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    return JSONResponse(status_code=201, content=created.to_contract())


# This module is not imported by the package `__init__` (it would drag FastAPI into every
# consumer of `create_code`), so it binds its own alternate spelling here. See
# `_spellings.py` for what goes wrong without it.
bind_spellings(sys.modules[__name__])
