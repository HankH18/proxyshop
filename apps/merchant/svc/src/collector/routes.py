"""``POST /pixel/collect`` — the endpoint every installed web pixel beacons to.

Discovered and mounted by the frozen ``merchant_svc.main.create_app`` because this file is
``<feature>/routes.py`` and exports ``router``. The path is not spelled here: it comes from
:data:`merchant_svc.install.config.COLLECTOR_PATH`, the same constant
``web_pixel_settings`` builds ``collectorUrl`` from, so the URL the install registers and
the URL the service answers cannot drift apart by editing one of them.

204, not 200: the contract in ``packages/contracts/openapi/merchant.openapi.json`` pins an
empty response, and there is nothing useful to hand a browser beacon anyway — the pixel is
fire-and-forget and no shopper's page should wait on this app's reply.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from merchant_svc.collector import PIXEL_INBOX, PixelEventRejected, accept_pixel_event
from merchant_svc.install.config import COLLECTOR_PATH

_log = logging.getLogger(__name__)

router = APIRouter()


@router.post(COLLECTOR_PATH, status_code=204, response_class=Response)
async def collect_pixel_event(request: Request) -> Response:
    """Accept one client-side checkout observation.

    A malformed or PII-carrying beacon is a 400 and nothing is stored. The refusal echoes
    the offending *field names* only — the values are the thing this app has decided not to
    hold, and an error body is as much a place data lives as a database is.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any decode failure is the same 400, never a 500
        return JSONResponse(status_code=400, content={"error": "unparseable-body"})

    try:
        observation = accept_pixel_event(payload)
    except PixelEventRejected as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "rejected", "detail": str(exc), "fields": list(exc.fields)},
        )

    PIXEL_INBOX.record(observation)
    if observation.gaps:
        # R4: a lossy beacon is recorded WITH its gap, never completed by inference.
        _log.info(
            "pixel observation for checkout %s is incomplete; missing %s",
            observation.checkout_token,
            ", ".join(observation.gaps),
        )
    return Response(status_code=204)
