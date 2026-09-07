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

import hashlib
import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from merchant_svc.collector import PIXEL_INBOX, PixelEventRejected, accept_pixel_event
from merchant_svc.composition import publish_pixel_observation
from merchant_svc.http_limits import BodyTooLarge, read_capped_body
from merchant_svc.install.config import COLLECTOR_PATH

_log = logging.getLogger(__name__)

#: How many hex characters of a checkout token's digest the gap line carries.
#:
#: 12 hex characters is 48 bits. :class:`~merchant_svc.collector.PixelInbox` holds 512 live
#: observations, so by the birthday bound the chance any two of them share a digest is about
#: ``512**2 / 2**49`` — roughly one in two billion, far below the rate at which an operator
#: reading these lines would misjudge a coincidence anyway. What the width buys is that it is
#: FIXED: the line's size is the same for every beacon, so it is not the sender's to choose.
TOKEN_DIGEST_CHARS = 12

router = APIRouter()


def _token_digest(token: str) -> str:
    """A constant-width handle for ``token``, safe to write into a log (T-365).

    This module's own rule, stated for the refusal path a few lines below, is that an error
    body is as much a place data lives as a database is — and a log is more so, because a
    database has a capacity and a log file does not. The refusal path honoured it by naming
    neither the value nor the key. The accept path did not: it interpolated
    ``observation.checkout_token`` verbatim, which is attacker-chosen text arriving on a route
    that takes no credential. Measured before this fix: a 900,000-character token wrote a
    900,090-character record, one per request, amplification 1.00x and linear in aggregate
    with nothing to evict it. A newline inside a token could forge a log line, too.

    A digest keeps the only thing an operator actually needs from this line — whether two
    incomplete beacons are the same checkout — at a width no input can change. The token
    itself goes two places, neither of which is this log line: the in-process
    :data:`~merchant_svc.collector.PIXEL_INBOX`, which an operator and a fresh-process probe
    read, and the ``checkout_pixel`` row
    :func:`~merchant_svc.composition.publish_pixel_observation` appends to E6's chained
    ledger, which is where the reconciler actually reads it from. This docstring used to
    name the ring as that reader; it was not one, and saying so was how the missing hop
    stayed invisible.

    ``surrogatepass`` because a JSON string may decode to a lone surrogate (``"\ud800"``),
    which plain UTF-8 encoding refuses with ``UnicodeEncodeError``. Hashing has to be total
    over everything the parser can produce, or this function is itself a 500.
    """
    digest = hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()
    return digest[:TOKEN_DIGEST_CHARS]


@router.post(COLLECTOR_PATH, status_code=204, response_class=Response)
async def collect_pixel_event(request: Request) -> Response:
    """Accept one client-side checkout observation.

    A malformed or PII-carrying beacon is a 400 and nothing is stored. The refusal names
    **neither the offending values nor their keys** — both are attacker-chosen text, and an
    error body is as much a place data lives as a database is. It reports how many fields
    were refused; the accepted set is published in :mod:`merchant_svc.collector`, which is
    what an integrator actually needs to fix their beacon.

    The body is size-capped: this route takes no credential, so an unbounded read is a
    denial of service that costs the sender one connection. Every field *inside* the body is
    length-capped too, against
    :data:`~merchant_svc.collector.MAX_PIXEL_FIELD_CHARS` — the body cap bounds the request,
    which is not the same thing as bounding what the collector keeps, and an over-long field
    arrives here as the same 400 as any other refusal.
    """
    try:
        raw = await read_capped_body(request)
    except BodyTooLarge:
        return JSONResponse(status_code=413, content={"error": "body-too-large"})

    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 - any decode failure is the same 400, never a 500
        return JSONResponse(status_code=400, content={"error": "unparseable-body"})

    try:
        observation = accept_pixel_event(payload)
    except PixelEventRejected as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "rejected", "detail": str(exc), "refused_fields": len(exc.fields)},
        )

    # THE RING FIRST, AND ALWAYS. Its readback is what a fresh-process hardening probe reads
    # and what this file's own tests assert on, and it is the only record that survives a
    # trust service that is not answering. Publishing first would lose the observation on
    # exactly the occasions an operator needs it most.
    PIXEL_INBOX.record(observation)
    # ...and then the chained ledger, which is where R4's reconciler actually reads. This
    # call cannot raise: see `composition.publish_pixel_observation`. It answers False for a
    # lost write and the 204 below is unconditional either way, because this route is a
    # beacon from a shopper's checkout page and a trust outage is not that shopper's problem.
    publish_pixel_observation(observation)
    if observation.gaps:
        # R4: a lossy beacon is recorded WITH its gap, never completed by inference.
        # The digest, never the token: see `_token_digest`. `gaps` is drawn from the fixed
        # `GAP_KEYS` vocabulary, so with a constant-width digest this whole line is constant
        # width — the sender cannot size it.
        _log.info(
            "pixel observation %s is incomplete; missing %s",
            _token_digest(observation.checkout_token),
            ", ".join(observation.gaps),
        )
    return Response(status_code=204)
