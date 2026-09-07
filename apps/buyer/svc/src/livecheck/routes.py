"""HTTP surface for the live-page check — the drain, and the record it produced.

Discovered and mounted by the frozen :func:`buyer_svc.main.create_app`, which globs
``<feature>/routes.py``::

    POST /buyer/livecheck/run              -> 200 the checks this pass completed
    GET  /buyer/livecheck/{auction_id}     -> 200 what was checked for one auction, and what
                                              was NOT, and why

Neither route is on the shopper's path and neither is where the check is *triggered*. The
trigger is ``POST /buyer/shortlist/render``, which enqueues and returns; this is the other end
of that queue. ``/run`` exists because a queue drained by something invisible is a queue
nobody can prove was drained — a worker, a cron, or an operator can all drive it, and so can a
test, which is what makes the served behaviour checkable rather than asserted.

Why the drain is a route and not a background thread
----------------------------------------------------
``apps/buyer/svc/src/main.py`` is orchestrator-frozen (B6(iii)), so there is no start-up hook
to start a worker in; every other seam in this service is bound by a request-time composition
hook for exactly that reason. A module that started a thread at import time would start one
per test module too, and its work would land in whichever process happened to import it —
which is the shape of "it works in pytest and does nothing in production" that this repository
keeps finding. A route is drivable from a worker (`while true; curl -XPOST .../run`), from a
scheduler, and from a test, and all three do the same thing.

``/run`` is idempotent in the way that matters: it takes what is on the queue and leaves an
empty queue behind, so calling it twice does not check anything twice.
"""

from __future__ import annotations

import sys
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ..composition import (
    LEDGER_SINK_ATTR,
    DeploymentConfigurationError,
    ensure_configured,
)
from ..intent._spellings import bind_spellings
from .deferred import (
    LiveCheckLedger,
    LiveCheckQueue,
    live_check_ledger,
    live_check_queue,
    run_live_checks,
)
from .fetching import NoPageFetcher

__all__ = ["LIVE_PAGE_FETCHER_ATTR", "LiveCheckView", "RunResponse", "router"]

router = APIRouter(prefix="/buyer/livecheck", tags=["buyer-livecheck"])

#: Where the page fetcher lives on the app. Bound by :mod:`buyer_svc.composition`; a service
#: that has never been through it holds nothing here and this module uses
#: :class:`~buyer_svc.livecheck.fetching.NoPageFetcher`, which reads no page and decides
#: nothing. Never a default that opens sockets.
LIVE_PAGE_FETCHER_ATTR = "live_page_fetcher"

#: The most targets one ``/run`` may drain. A ceiling on how long one call holds a worker
#: against third-party servers, not a ceiling on the queue.
MAX_DRAIN_PER_RUN = 32


class LiveCheckView(BaseModel):
    """One completed check, with both readings and what was compared."""

    checked_at: str
    outcome: str
    surface: str
    fetch_reason: str = ""
    target: dict[str, Any] = Field(default_factory=dict)
    check: dict[str, Any] = Field(default_factory=dict)
    ledger_events: list[dict[str, Any]] = Field(default_factory=list)


class RunResponse(BaseModel):
    """What one drain did. ``pending`` is what is left, so a caller knows to come back."""

    checked: int
    contradicted: int
    agreed: int
    no_verdict: int
    pending: int
    dropped: int
    fetcher: str
    records: list[LiveCheckView] = Field(default_factory=list)


class AuctionChecksResponse(BaseModel):
    """What was checked for one auction — and what was not, which is the other half.

    ``refused`` is never omitted and never merged into ``records``. "No page was checked for
    this slot" and "the page was checked and agreed" are different answers, and a surface that
    rendered them the same way would be claiming evidence the platform does not have.
    """

    auction_id: str
    records: list[LiveCheckView] = Field(default_factory=list)
    refused: list[dict[str, Any]] = Field(default_factory=list)


def _bind_the_deployment(request: Request) -> None:
    """Run the composition root once for this app, before its seams are read.

    Character for character the hook ``accept/routes.py`` and ``auctions/routes.py`` take, and
    for the same reason: ``main.py`` is orchestrator-frozen, so a deployment is composed at the
    top of a request instead. A malformed document is a **503 naming the problem**, never a 500.
    """
    try:
        ensure_configured(request.app)
    except DeploymentConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


def _seams(request: Request) -> tuple[Any, Any, LiveCheckQueue, LiveCheckLedger]:
    fetcher = getattr(request.app.state, LIVE_PAGE_FETCHER_ATTR, None) or NoPageFetcher()
    sink = getattr(request.app.state, LEDGER_SINK_ATTR, None)
    return fetcher, sink, live_check_queue(), live_check_ledger()


@router.post("/run", response_model=RunResponse)
async def run_route(
    request: Request,
    limit: int = Query(default=MAX_DRAIN_PER_RUN, ge=1, le=MAX_DRAIN_PER_RUN),
) -> RunResponse:
    """Check the pages queued by earlier renders, and publish what disagreed.

    Answers 200 with an empty ``records`` when there is nothing queued, rather than 404: "no
    store needed checking" is a successful answer to this question.
    """
    _bind_the_deployment(request)
    fetcher, sink, queue, ledger = _seams(request)
    records = run_live_checks(queue=queue, fetcher=fetcher, ledger=ledger, sink=sink, limit=limit)
    outcomes = [record.outcome for record in records]
    return RunResponse(
        checked=len(records),
        contradicted=outcomes.count("contradicted"),
        agreed=outcomes.count("agrees"),
        no_verdict=outcomes.count("no_verdict"),
        pending=len(queue),
        dropped=queue.dropped,
        fetcher=type(fetcher).__name__,
        records=[LiveCheckView(**record.to_dict()) for record in records],
    )


@router.get("/{auction_id}", response_model=AuctionChecksResponse)
async def auction_checks_route(auction_id: str, request: Request) -> AuctionChecksResponse:
    """What the platform checked for one auction, and what it declined to check."""
    _bind_the_deployment(request)
    ledger = live_check_ledger()
    return AuctionChecksResponse(
        auction_id=auction_id,
        records=[LiveCheckView(**record.to_dict()) for record in ledger.records(auction_id)],
        refused=[row.to_dict() for row in ledger.refusals(auction_id)],
    )


# This module is not imported by the package `__init__` (it would drag FastAPI into every
# consumer of the pure check), so it binds its own alternate spelling here. See
# `buyer_svc.intent._spellings` for what goes wrong without it.
bind_spellings(sys.modules[__name__])
