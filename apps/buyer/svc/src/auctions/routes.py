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
from collections.abc import Mapping
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
RECORDED_KEYS: tuple[str, ...] = (
    "entries",
    "excluded",
    "denied",
    "ranked",
    "solicited",
    "relaxed_constraints",
)

#: The recorded diagnostics that are mappings rather than arrays, forwarded through
#: :func:`_recorded_mapping`. See :attr:`AuctionView.market`, :attr:`AuctionView.exploration`
#: and :attr:`AuctionView.roster_source`.
#:
#: ``state`` is deliberately NOT here. It is the only field on that answer whose value can have
#: changed since it was recorded, and this view keeps its live half and its recorded half
#: labelled as such; a recorded ``state`` would read as a live one and be wrong exactly when a
#: reader most needed it. The live shortlist above already answers "does the exchange still
#: know this auction".
RECORDED_MAPPING_KEYS: tuple[str, ...] = ("market", "exploration", "roster_source")

#: Kept as the singular spelling too: it was the first mapping forwarded and
#: ``test_auctions_shortlist.py`` names it.
RECORDED_MAPPING_KEY = "market"


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
    #: RECORDED. Which of the SHOPPER'S OWN hard constraints the exchange loosened in order to
    #: fill the shortlist, and ``[]`` when it loosened none.
    #:
    #: This is the one of these that is not merely diagnostic. Serving a shopper a row that
    #: violates something they stated, without saying so, is the thing this project's whole
    #: credibility argument is against — and it looks identical to an honest result from the
    #: outside.
    relaxed_constraints: list[Any] = []
    #: RECORDED, and a MAPPING rather than one of the five arrays above, which is why it is
    #: named separately. The exchange's one-line verdict on the market it just ran --
    #: ``{"solicited", "sponsored", "list_price", "timed_out", "not_asked", "denied",
    #: "bid_window_seconds", "fallback_reasons", "all_fallback"}`` -- computed once so the
    #: 201, the ``auction_closed`` ledger entry and the exchange's log cannot disagree.
    #:
    #: Forwarded because the interesting case is invisible without it. When every solicited
    #: store falls back, the shortlist is a normal-looking list of catalogue prices: the same
    #: shape, the same number of slots, no error anywhere. ``all_fallback`` is the only field
    #: that distinguishes "this market ran and nobody bid" from "this market ran". The
    #: exchange publishes it once, on the answer this service records, and it survived nowhere
    #: else on this side -- the same argument the five arrays above are kept for.
    #:
    #: ``None`` when this service holds no record, or when the recorded answer carried no
    #: ``market``. Never ``{}``: an exchange too old to publish one and an exchange reporting
    #: an empty market are different facts, and only one of them exists.
    market: dict[str, Any] | None = None
    #: RECORDED. R12's exploration slice: which shortlist slot, if any, was granted to a store
    #: the trust snapshot marks ``low_data``, and on what basis. ``None`` in the ordinary case
    #: where no slot was explored — that is the exchange's own value, not this service's.
    #:
    #: Forwarded because it is the DISCLOSURE half of the one place a signal other than the
    #: published ranking features decides who is seen. The exchange bounds that slice to a
    #: single slot and publishes what it did on the same answer; a page that shows the slot and
    #: not the disclosure shows the effect and hides the cause.
    exploration: dict[str, Any] | None = None
    #: RECORDED. Where the candidate set came from and what it cost:
    #: ``{"source", "shops", "products_considered", "reason", "elapsed_ms"}``. ``None`` when the
    #: recorded answer carried none.
    #:
    #: An empty shortlist has two very different causes — nobody was RETRIEVED, or everybody
    #: retrieved was refused — and the five arrays only distinguish them once retrieval found
    #: somebody. ``source`` and ``products_considered`` are what separate "the graph answered
    #: with nothing" from "the graph answered and the gate denied them all".
    roster_source: dict[str, Any] | None = None
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

    ``[]`` rather than ``None`` because every one of :data:`RECORDED_KEYS` is a list in every
    answer the exchange gives, and a screen that has to tell ``null`` from ``[]`` for them
    would be reading meaning into this service's bookkeeping instead of into the exchange's.
    """
    if not isinstance(recorded, dict):
        return []
    answer = recorded.get("response")
    if not isinstance(answer, dict):
        return []
    value = answer.get(key)
    return list(value) if isinstance(value, list) else []


def _recorded_mapping(recorded: Any, key: str) -> dict[str, Any] | None:
    """One diagnostic MAPPING off the recorded answer, or ``None`` when it carried none.

    ``None`` and not ``{}``, unlike :func:`_recorded_rows`' ``[]``, and the difference is the
    point: the five arrays are in every answer the exchange gives, so their absence says
    nothing, while ``market`` is absent exactly when the exchange that answered predates it.
    Flattening that to ``{}`` would report "a market with no stores in it".
    """
    if not isinstance(recorded, dict):
        return None
    answer = recorded.get("response")
    if not isinstance(answer, dict):
        return None
    value = answer.get(key)
    return dict(value) if isinstance(value, Mapping) else None


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

    # Merged into ONE mapping before unpacking, and annotated, because two `**` expressions
    # each carry a homogeneous value type — `dict[str, Any] | None` for the mappings and
    # `list[Any]` for the arrays — and mypy matches a `**` argument's value type against every
    # field it could fill. Unpacking them separately made each one an error about the other's
    # fields; one `dict[str, Any]` says what is actually true, which is that these keys have
    # different types and the model declares which.
    recorded_fields: dict[str, Any] = {
        key: _recorded_mapping(recorded, key) for key in RECORDED_MAPPING_KEYS
    }
    recorded_fields.update({key: _recorded_rows(recorded, key) for key in RECORDED_KEYS})

    return AuctionView(
        auction_id=auction_id,
        shortlist=dict(shortlist) if shortlist is not None else None,
        recorded_at=str(recorded["recorded_at"]) if recorded is not None else None,
        **recorded_fields,
    )


# This module is not imported by the package `__init__` (it would drag FastAPI into every
# consumer of the package), so it binds its own alternate spelling here. See
# `buyer_svc.intent._spellings` for what goes wrong without it.
bind_spellings(sys.modules[__name__])
