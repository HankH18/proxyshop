"""HTTP surface for one auction as this buyer service can honestly describe it.

Discovered and mounted by the frozen :func:`buyer_svc.main.create_app`, which globs
``<feature>/routes.py`` — which is why this route lives here and not on
:mod:`buyer_svc.composition`, where the client it reads was wired::

    GET /buyer/auctions/{auction_id}   -> 200 the live shortlist + the recorded diagnostics

The prefix is free: ``intent/routes.py`` owns ``/buyer/intent``, ``accept/routes.py`` owns
``/buyer/shortlist``, ``feedback/routes.py`` owns ``/buyer/feedback`` and ``auth/routes.py``
owns ``/buyer`` with the paths ``/auth/...`` and ``/profile``. Nothing answers under
``/buyer/auctions``.

**Two halves, kept apart, and a reader must not be able to mistake one for the other.**

``shortlist``
    **LIVE.** Fetched from the exchange on this request, through
    :meth:`~buyer_svc.composition.HttpExchangeClient.shortlist_for`
    (``GET {exchange}/auctions/{id}/shortlist``). It is what the exchange says *now*.
    ``null`` when the exchange has forgotten the auction — which is not the same answer as a
    shortlist with no slots, and stays a different answer here.
``entries`` / ``excluded`` / ``denied`` / ``ranked`` / ``solicited`` / ``recorded_at``
    **RECORDED.** Read out of the answer this service kept when it opened the auction,
    through :meth:`~buyer_svc.composition.HttpExchangeClient.outcome_for`. They describe the
    auction as it ran, not as it is. ``recorded_at`` is *this service's* clock, and it is the
    field that says so.

Nothing is merged and nothing is derived from the other half. The recorded body carries a
``shortlist`` of its own — the exchange puts one in its ``POST /auctions`` answer — and this
route never reads it: a recorded shortlist rendered as a live one would tell a buyer that
slots exist which the exchange may since have dropped. That is the single mistake this module
is shaped to make impossible, which is why the recorded body is reached only through
:func:`_recorded_rows`, for the five diagnostic keys and nothing else, while ``shortlist``
comes from a different call to a different door and never touches it.

The diagnostics exist here at all because ``apps/exchange/src/auction/routes.py``, measured
on this branch, answers ``GET /auctions/{auction_id}`` with state alone::

    return {"auction_id": ..., "state": ..., "intent_id": ..., "cluster_id": ...,
            "accepted_bid_ref": ..., "history": ...}

— no ``entries``, no ``excluded``, no ``denied``, no ``ranked``. See :mod:`buyer_svc.auctions`.

Wiring
------
The exchange client is read from ``app.state.exchange_client`` and is **not** constructed
here, for the same reason ``accept/routes.py`` does not construct one. That attribute is set
by :mod:`buyer_svc.composition` from ``BUYER_DEPLOYMENT`` / ``BUYER_DEPLOYMENT_JSON`` /
``EXCHANGE_URL``, through the request-time hook :func:`_bind_the_deployment` below — the very
same object ``POST /buyer/intent/confirm`` opened the auction with, which is what makes the
record reachable from here at all. A service with no document configured binds nothing and
answers **503**; a malformed document is a **503 naming the problem**, never a 500.

What ``solicited: []`` means here, and what it can no longer mean
----------------------------------------------------------------
It used to be able to mean "this buyer service was configured with an exchange and with no
candidate set, so it opened an auction and asked nobody" — measured on a real ``uvicorn``
pair with ``EXCHANGE_URL`` alone, this route answered ``200`` with all five arrays empty and
a shortlist of ``slots: 0``, which is indistinguishable from "no store had anything for you".
:class:`~buyer_svc.composition.NoRosterBound` closed that: an auction with no roster is now
refused at ``POST /buyer/intent/confirm`` and never opened. So an empty ``solicited`` on this
route is the EXCHANGE's answer about a real roster — every candidate gated out before
solicitation — and the ``denied`` array beside it is where the reason is.
"""

from __future__ import annotations

import sys
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

# `EXCHANGE_CLIENT_ATTR` is imported from the composition root rather than re-spelled the way
# `intent/routes.py` and `accept/routes.py` spell it. Those two predate that module and pin
# the duplication with a test; this file is younger than it, already imports it for
# `ensure_configured`, and there is no cycle to avoid — `composition` imports no route module.
from ..composition import (
    ENV_DEPLOYMENT,
    EXCHANGE_CLIENT_ATTR,
    DeploymentConfigurationError,
    ExchangeCallFailed,
    ensure_configured,
)
from ..intent._spellings import bind_spellings

__all__ = ["AuctionView", "router"]

router = APIRouter(prefix="/buyer/auctions", tags=["buyer-auctions"])

#: The keys read off the RECORDED answer, and the whole of what is read off it. ``shortlist``
#: is deliberately absent: the exchange puts one in its ``POST /auctions`` body and serving it
#: from here would be a stale shortlist wearing a live one's field name.
RECORDED_KEYS: tuple[str, ...] = ("entries", "excluded", "denied", "ranked", "solicited")


class AuctionView(BaseModel):
    """One auction, with its live half and its recorded half labelled as such."""

    auction_id: str
    #: LIVE, from ``GET {exchange}/auctions/{id}/shortlist``. ``None`` when the exchange no
    #: longer knows this auction — never an empty shortlist standing in for a missing one.
    shortlist: dict[str, Any] | None
    #: RECORDED, from the ``POST /auctions`` answer this service kept. ``[]`` when it holds
    #: no record — never invented, never derived from the shortlist above.
    entries: list[Any] = []
    excluded: list[Any] = []
    denied: list[Any] = []
    ranked: list[Any] = []
    solicited: list[Any] = []
    #: When THIS SERVICE recorded the answer above; ``None`` when it holds no record. Not the
    #: exchange's clock, and kept outside the recorded body for exactly that reason.
    recorded_at: str | None = None


def _bind_the_deployment(request: Request) -> None:
    """Run the composition root once for this app, before the exchange client is read.

    Character for character the hook ``accept/routes.py`` takes, and for the same reason:
    ``apps/buyer/svc/src/main.py`` is orchestrator-frozen (B6(iii)), so a deployment cannot be
    composed inside ``create_app``. It binds nothing that is already bound, so a test or a
    deployment that set ``app.state.exchange_client`` itself still wins. A malformed
    deployment document is a **503** naming the problem, never a 500.
    """
    try:
        ensure_configured(request.app)
    except DeploymentConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


def _recorded_rows(recorded: Any, key: str) -> list[Any]:
    """One diagnostic array off the recorded answer, or ``[]`` when it carried none.

    ``[]`` rather than ``None`` because these five are lists in every answer the exchange
    gives, and a screen that has to tell ``null`` from ``[]`` for them would be reading
    meaning into this service's bookkeeping instead of into the exchange's.
    """
    if not isinstance(recorded, dict):
        return []
    answer = recorded.get("response")
    if not isinstance(answer, dict):
        return []
    value = answer.get(key)
    return list(value) if isinstance(value, list) else []


@router.get("/{auction_id}", response_model=AuctionView)
async def read_auction(auction_id: str, request: Request) -> AuctionView:
    """The live shortlist and the recorded diagnostics for one auction, side by side."""
    _bind_the_deployment(request)
    client = getattr(request.app.state, EXCHANGE_CLIENT_ATTR, None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"this buyer service has no exchange client, so it can say nothing about "
                f"auction {auction_id!r}: it neither opened the auction nor has anywhere to "
                f"ask about it. Point {ENV_DEPLOYMENT} at a deployment document naming an "
                f"exchange_url."
            ),
        )

    # RECORDED first, and locally: `outcome_for` reads this process's own ring and cannot
    # fail, so an exchange that is down still lets a buyer see why their shortlist was empty.
    recorded = client.outcome_for(auction_id) if hasattr(client, "outcome_for") else None

    # LIVE, over the wire, every time. Never read off `recorded`.
    try:
        shortlist = client.shortlist_for(auction_id) if hasattr(client, "shortlist_for") else None
    except ExchangeCallFailed as exc:
        # 502: this request was fine and the upstream's answer was not. A 500 would blame this
        # service for the exchange's reply — the same split `accept/routes.py` makes.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    if recorded is None and shortlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no auction {auction_id!r}: this buyer service holds no record of opening "
                f"it, and the exchange has no shortlist for it either. One source knowing it "
                f"would have been enough."
            ),
        )

    return AuctionView(
        auction_id=auction_id,
        shortlist=dict(shortlist) if shortlist is not None else None,
        recorded_at=str(recorded["recorded_at"]) if recorded is not None else None,
        **{key: _recorded_rows(recorded, key) for key in RECORDED_KEYS},
    )


# This module is not imported by the package `__init__` (it would drag FastAPI into every
# consumer of the package), so it binds its own alternate spelling here. See
# `buyer_svc.intent._spellings` for what goes wrong without it.
bind_spellings(sys.modules[__name__])
