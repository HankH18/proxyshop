"""R9's merchant dashboard: the built SPA, and the one read that fills it.

Discovered and mounted by the frozen ``merchant_svc.main.create_app`` because this file is
``<feature>/routes.py`` and exports ``router``.

WHAT WAS MEASURED BEFORE THIS FILE EXISTED
==========================================
``apps/merchant/app/dashboard/`` held one empty ``.gitkeep``. The merchant TypeScript tree was
a config module and one route file, with no build script and no entry point; its
``package.json`` declared 17 npm dependencies and not one was imported by any file in the
repository. ``apps/merchant/compose.yaml`` said so in its own words: *"a TypeScript scaffold
with no build script and no entrypoint of its own"*. Meanwhile the exchange's
``GET /reports/losses`` had a real producer writing a row at every auction close, the trust
service served per-dimension snapshots, and this service served the kill switch and the
envelope routes — three finished surfaces with no reader.

THREE THINGS HERE ARE LOAD-BEARING
==================================
**The bundle is a `Mount`, and the two JSON reads are published operations.** A `Mount`
declares no HTTP operation, so it is invisible to
``test_repro_open_tickets._served_operations`` and no contract is owed for a directory of
JavaScript. The two operations below are real surface and are declared in
``packages/contracts/openapi/merchant.openapi.json`` in the same change that serves them —
that file's own note asks for exactly this ("Re-pin either one in the change that serves it").
``test_dashboard.py`` grades both halves of that sentence.

**The bundle is resolved per request, not at import.** ``MERCHANT_UI_DIST`` names a directory
a build produces; a service that resolved it once at start-up would answer 404 forever for a
bundle built a second later, and — worse — a ``StaticFiles`` constructed against a missing
directory raises at construction time, which inside a mount is a 500 rather than an answer.
:class:`DashboardBundle` resolves it on each request and renders a page naming the build
command when there is nothing there.

**The store is resolved from the path and the report bearer from the environment.** The
exchange resolves a merchant from its token, so that token is an identity and it stays on this
side of the wire. The browser is given the report, never the key to it.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from merchant_svc.bidding import store_agent_context
from merchant_svc.envelope import ENVELOPES, UnknownStore
from merchant_svc.http_limits import BodyTooLarge, read_capped_body
from merchant_svc.install.routes import ADMIN_TOKEN_ENV
from merchant_svc.install.signatures import secure_equals

from .config import (
    UI_DIST_ENV,
    ReportTokensUnreadable,
    dashboard_config,
    ui_dist,
)
from .journal import SOLICITATIONS
from .upstream import (
    NOT_CONFIGURED,
    OK,
    Panel,
    Solicitation,
    fetch_losses,
    fetch_trust_events,
    fetch_trust_snapshot,
    solicit_bid,
)

__all__ = ["DASHBOARD_MOUNT", "DashboardBundle", "router"]

_log = logging.getLogger(__name__)

router = APIRouter()

#: Where the built SPA is served. Named rather than spelled twice, so the test drives the
#: constant the mount registers.
DASHBOARD_MOUNT: Final[str] = "/dashboard"

#: How far back the loss report reaches when the caller states no window. Thirty days is the
#: horizon a merchant reviews a policy over; it is a DEFAULT, echoed back in the response, and
#: never a silent one.
DEFAULT_WINDOW_DAYS: Final[int] = 30

#: The build that produces the bundle, quoted to the operator who has not run it.
BUILD_COMMAND: Final[str] = "npm --workspace @proxyshop/merchant run build:ui"


# ======================================================================================
# The bundle
# ======================================================================================
_UNBUILT_PAGE = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>ProxyShop merchant dashboard — not built</title>
<style>
 body {{ font: 15px/1.6 ui-sans-serif, system-ui, sans-serif; margin: 0; padding: 3rem 1.5rem;
        background: #10131a; color: #e6e9ef; }}
 main {{ max-width: 44rem; margin: 0 auto; }}
 h1 {{ font-size: 1.35rem; margin: 0 0 .5rem; }}
 code {{ background: #1b2030; padding: .15rem .4rem; border-radius: 4px; color: #9fd0ff; }}
 pre {{ background: #1b2030; padding: 1rem; border-radius: 8px; overflow-x: auto; }}
 p.why {{ color: #9aa3b5; }}
</style></head>
<body><main>
<h1>The merchant dashboard has not been built.</h1>
<p class="why">This is the service saying what is missing, not a broken page. The dashboard is
a Vite bundle; this process serves whatever directory <code>{UI_DIST_ENV}</code> names, and
right now that variable is unset or points at a directory that is not there.</p>
<pre>{BUILD_COMMAND}
export {UI_DIST_ENV}=apps/merchant/app/dist</pre>
<p class="why">The JSON the page reads is served either way, at
<code>GET /stores/&lt;store_id&gt;/dashboard</code>, behind the same
<code>{ADMIN_TOKEN_ENV}</code> bearer the envelope routes use.</p>
</main></body></html>
"""


class DashboardBundle:
    """The mounted ASGI app that serves the built SPA, or says why it cannot.

    Deliberately NOT a ``StaticFiles`` mounted directly. Two reasons, both measured:

    * ``StaticFiles(directory=...)`` raises ``RuntimeError`` at construction when the directory
      is absent, and the mount is constructed at import — so a service whose bundle had not
      been built yet could not start at all.
    * the directory is named by an environment variable a deployment sets after the image is
      built, so resolving it once at import answers 404 for the rest of the process's life.

    The resolved ``StaticFiles`` is cached per directory, so a served page costs one
    ``is_dir()`` and not a new file-system app per request.
    """

    def __init__(self) -> None:
        self._cache: dict[str, StaticFiles] = {}

    def _static_for(self, directory: Path) -> StaticFiles:
        key = str(directory)
        cached = self._cache.get(key)
        if cached is None:
            cached = StaticFiles(directory=key, html=True)
            self._cache[key] = cached
        return cached

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        directory = ui_dist()
        if directory is None:
            response = HTMLResponse(_UNBUILT_PAGE, status_code=503)
            await response(scope, receive, send)
            return
        await self._static_for(directory)(scope, receive, send)


router.mount(DASHBOARD_MOUNT, DashboardBundle(), name="merchant-dashboard")


# ======================================================================================
# Shared refusals — the same rule the envelope routes apply, for the same reason
# ======================================================================================
def _problem(status: int, reason: str, **detail: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": reason, **detail})


def _refuse_unless_admin(request: Request) -> JSONResponse | None:
    """``None`` when the caller may read this merchant's dashboard; a refusal otherwise.

    The dashboard carries the store's floors, its discount ceiling, its whole loss history and
    its trust posture. That is the sealed state (S7) the envelope routes already refuse to
    serve anonymously, assembled onto one page, so it takes the same token and refuses in the
    same two ways: 503 while none is configured, 401 for the wrong one.
    """
    import os  # noqa: PLC0415 - read per request, so an operator's fix takes effect at once

    configured = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
    if not configured:
        return _problem(
            503,
            "admin-api-not-configured",
            missing=[ADMIN_TOKEN_ENV],
            detail="the dashboard refuses every caller until a token is configured",
        )
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not secure_equals(configured, supplied.strip()):
        return _problem(401, "unauthorized")
    return None


def _envelope_panel(store_id: str) -> dict[str, Any]:
    """R9's versioned envelope, plus the bidding decision the merchant service derives.

    The history is what makes an edit reviewable: R9 asks for envelope editing *(versioned)*,
    and a page showing only the current terms cannot tell a merchant what they changed. Old
    versions are values and never change (``merchant_svc.envelope.model``), so this is a read
    of the record rather than a reconstruction of it.
    """
    context = store_agent_context(store_id, versions=ENVELOPES)
    try:
        current = ENVELOPES.current(store_id)
    except UnknownStore:
        return {
            "state": "absent",
            "detail": (
                "no envelope has ever been recorded for this store, so there is nothing to "
                "edit and nothing to activate. Onboarding writes version 1 "
                "(POST /stores/{store_id}/envelope's interview); until then the agent is in "
                "shadow and submits nothing."
            ),
            "activation": context["envelope"]["activation"],
            "may_bid": context["may_bid"],
            "reason": context["reason"],
            "versions": [],
        }
    history = [
        {
            "version": version.version,
            "activation": version.activation,
            "approved_by": getattr(version.approval, "approver", None),
            "approved_at": getattr(version.approval, "approved_at", None),
        }
        for version in ENVELOPES.history(store_id)
    ]
    return {
        "state": OK,
        "detail": "",
        "current": current.to_contract().model_dump(mode="json"),
        "versions": history,
        "activation": current.activation,
        "may_bid": context["may_bid"],
        "reason": context["reason"],
    }


def _window(start: float | None, end: float | None) -> tuple[float, float]:
    """The loss window, defaulted to the last :data:`DEFAULT_WINDOW_DAYS` and echoed back."""
    finish = time.time() if end is None else end
    begin = finish - timedelta(days=DEFAULT_WINDOW_DAYS).total_seconds() if start is None else start
    return begin, finish


# ======================================================================================
# GET /stores/{store_id}/dashboard — everything R9 names, in one read
# ======================================================================================
@router.get("/stores/{store_id}/dashboard")
async def read_dashboard(
    store_id: str,
    request: Request,
    start: float | None = Query(None, description="Loss window start, epoch seconds."),
    end: float | None = Query(None, description="Loss window end, epoch seconds."),
) -> Any:
    """One store's whole R9 page, assembled from this service and the two it reads.

    **One read, not five.** The page needs the envelope, the losses, the trust score, the
    trust payloads and the bid journal to render at all; five endpoints would mean five
    partial states in the browser and five places for a refusal to be swallowed. Every region
    is a panel with a state, so a failure in one is a sentence in that panel and never an
    empty page.

    Nothing here is cached. A merchant looking at their kill switch is looking at it *now*.
    """
    refusal = _refuse_unless_admin(request)
    if refusal is not None:
        return refusal
    try:
        config = dashboard_config()
    except ReportTokensUnreadable as exc:
        # A NAMED table that cannot be read is a misconfiguration, never "this merchant has no
        # losses". It refuses the whole read rather than degrading one panel, because the same
        # variable is the one the operator has to fix.
        return _problem(503, "report-tokens-unreadable", detail=str(exc))

    begin, finish = _window(start, end)
    losses = await fetch_losses(config, store_id, start=begin, end=finish)
    snapshot = await fetch_trust_snapshot(config, store_id)
    events = await fetch_trust_events(config, store_id)

    agent_missing = config.store_agent_missing()
    entries = [entry.as_json() for entry in SOLICITATIONS.entries(store_id)]
    if agent_missing:
        bids: dict[str, Any] = Panel(
            state=NOT_CONFIGURED,
            missing=agent_missing,
            detail=(
                "this deployment states no address for the store's own agent, so the dashboard "
                f"cannot solicit a bid: set {', '.join(agent_missing)}."
            ),
        ).as_json()
    else:
        bids = {
            "state": OK,
            "detail": (
                "solicitations made from this dashboard against the store's own agent, newest "
                "first. These are rehearsals, not the auctions the exchange ran: the "
                "per-auction rationale a live bid was written with lives in the agent's own "
                "in-process shadow log (store_agent.solicitation.advocate.BoundedBidLog) and "
                "that log has no served route, so no service can read it. A row flagged "
                "`contradiction` is a store this merchant has stopped whose agent bid anyway — "
                "see SolicitationRecord.contradiction for the measured cause."
            ),
            "entries": entries,
        }

    return JSONResponse(
        content={
            "store_id": store_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "envelope": _envelope_panel(store_id),
            "losses": losses.as_json(),
            "trust": snapshot.as_json(),
            "trust_events": events.as_json(),
            "bids": bids,
        }
    )


# ======================================================================================
# POST /stores/{store_id}/bids/solicit — the kill switch, observed
# ======================================================================================
def _bid_request(store_id: str, submitted: Mapping[str, Any]) -> dict[str, Any]:
    """A `BidRequest` for this store, defaulted from the store's own envelope.

    The cluster defaults to the first cluster the merchant's envelope says they pursue, because
    a solicitation for a cluster they do not pursue is answered ``cluster_not_pursued`` and
    would tell the merchant nothing about the state they are actually checking.

    The pseudonym is a fixed, obviously-synthetic value. It is NOT a rotating buyer pseudonym
    and must never be one: R5 keeps buyer identity away from stores, and minting a plausible
    pseudonym here would put a fake shopper in the store's own learning state.
    """
    try:
        envelope = ENVELOPES.current(store_id)
        pursued = list(envelope.pursue_clusters)
    except UnknownStore:
        pursued = []
    cluster = str(submitted.get("cluster_id") or (pursued[0] if pursued else "") or "")
    now = datetime.now(UTC)
    intent: dict[str, Any] = {
        "intent_id": f"dashboard-{uuid.uuid4()}",
        "query": str(
            submitted.get("query") or "a rehearsal solicitation from the merchant dashboard"
        ),
        "hard_constraints": list(submitted.get("hard_constraints") or ()),
        "preferences": list(submitted.get("preferences") or ()),
        "created_at": now.isoformat(),
        "schema_version": "1",
    }
    if cluster:
        intent["cluster_id"] = cluster
    for optional in ("category", "ship_to", "currency", "budget_band"):
        value = submitted.get(optional)
        if value:
            intent[optional] = str(value)
    return {
        "auction_id": f"dashboard-rehearsal-{uuid.uuid4()}",
        "intent": intent,
        "profile": {
            "pseudonym": "dashboard-rehearsal",
            "buckets": {
                "budget_band": intent.get("budget_band"),
                "category_affinity": [cluster] if cluster else [],
                "frequency_tier": None,
                "region": None,
                "first_time": None,
            },
        },
        "respond_by": (now + timedelta(seconds=30)).isoformat(),
    }


@router.post("/stores/{store_id}/bids/solicit")
async def solicit_this_stores_agent(store_id: str, request: Request) -> Any:
    """Ask this store's own agent for a bid, and record what it answered.

    This is how the kill switch becomes **observable**. R9 asks for a kill switch; a control
    whose effect cannot be seen from the page that offers it is a control nobody can trust,
    and this one was inert until a store agent learned to answer ``204 store_killed``. Kill,
    then solicit: the row that comes back is the proof, and it is the same evidence an operator
    would get from ``curl``.

    The answer is recorded verbatim and never invented. A decline is a decline with the agent's
    own reason token; an agent nobody configured is a 503 naming the variable; an agent that
    does not answer is a 502 naming the address. None of the three is ever rendered as "this
    store chose not to bid".
    """
    refusal = _refuse_unless_admin(request)
    if refusal is not None:
        return refusal
    try:
        raw = await read_capped_body(request)
    except BodyTooLarge:
        return _problem(413, "body-too-large")
    submitted: Mapping[str, Any] = {}
    if raw.strip():
        import json  # noqa: PLC0415 - only this branch parses a body

        try:
            decoded = json.loads(raw)
        except ValueError:
            return _problem(400, "unparseable-body")
        if not isinstance(decoded, Mapping):
            return _problem(400, "not-a-solicitation", detail=f"got a {type(decoded).__name__}")
        submitted = decoded

    config = dashboard_config()
    context = store_agent_context(store_id, versions=ENVELOPES)
    activation = str(context["envelope"]["activation"])
    may_bid = bool(context["may_bid"])

    bid_request = _bid_request(store_id, submitted)
    answered = await solicit_bid(config, bid_request)

    if isinstance(answered, Panel):
        if answered.state == NOT_CONFIGURED:
            return _problem(
                503,
                "store-agent-not-configured",
                missing=list(answered.missing),
                detail=answered.detail,
            )
        return _problem(502, "store-agent-unreachable", detail=answered.detail)

    assert isinstance(answered, Solicitation)  # noqa: S101 - the only two return types
    auction_id = str(bid_request["auction_id"])
    if answered.outcome == "bid" and answered.bid is not None:
        record = SOLICITATIONS.append_bid(
            store_id=store_id,
            auction_id=auction_id,
            activation=activation,
            may_bid=may_bid,
            bid=answered.bid,
        )
    else:
        record = SOLICITATIONS.append_decline(
            store_id=store_id,
            auction_id=auction_id,
            activation=activation,
            may_bid=may_bid,
            reason=answered.decline_reason,
            detail=answered.detail,
        )
    _log.info(
        "dashboard solicitation for %r answered %s (activation=%s, reason=%s)",
        store_id,
        answered.outcome,
        activation,
        answered.decline_reason,
    )
    return JSONResponse(content=record.as_json())
