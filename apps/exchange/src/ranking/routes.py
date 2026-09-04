"""``GET /auctions/{auction_id}/shortlist`` — the published ranking's own door.

``packages/contracts/openapi/exchange.openapi.json`` has declared this operation since the
contract was written, and until this module existed the exchange answered it with a 404: the
shortlist a buyer is supposed to read was built by code no request reached (T-310), and the
path was one of three published-but-unserved exchange operations T-312 records.

``exchange.main`` mounts every ``<feature>/routes.py`` it finds beside itself, so this file
existing is what puts the whole ranking package — ``rank()``, the R19 hard-constraint filters,
R12's fail-closed blacklist read and the D29 shortlist builder — into the served app's import
closure. The auction route is what makes it *run*: ``POST /auctions`` ranks its own collected
bids at close and records the shortlist here, which is the only place the answer can live.
``auction/state.py``'s ``AuctionRecord`` holds a roster and a history but no bids, so a route
that tried to rank on read would have nothing to rank.

The response body is the pinned ``Shortlist`` contract type, so what this route answers and
what ``rank()`` builds cannot drift into two shapes.
"""

from __future__ import annotations

from contracts.protocol import Shortlist
from fastapi import APIRouter, HTTPException, Request

from .serving import DEFAULT_SHORTLIST_CAPACITY, shortlist_store

__all__ = ["router"]

router = APIRouter(tags=["ranking"])


@router.get("/auctions/{auction_id}/shortlist", response_model=Shortlist)
async def read_shortlist(auction_id: str, request: Request) -> dict:
    """This auction's shortlist — 404 before it has closed, and once the store has let it go.

    404 rather than an empty shortlist, and the distinction is the point: an auction whose
    every candidate was excluded has a real shortlist with no slots, and answering that with
    the same body as "no such auction" would tell a buyer that a ranking which ran and refused
    everything is indistinguishable from one that never happened.

    The detail names FOUR causes, and the fourth is the one an operator will actually hit:
    :class:`~exchange.ranking.serving.ShortlistStore` holds 512 auctions, so a burst evicts
    entries that are nowhere near their TTL. An earlier draft of this message listed three
    causes and put eviction under "its 15-minute TTL has taken it away", which is a diagnosis
    that sends the reader to the wrong knob.
    """
    shortlist = shortlist_store(request.app).get(auction_id)
    if shortlist is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no shortlist for auction {auction_id!r}: it has not closed yet, it never "
                f"existed, its 15-minute TTL has taken it away, or it was evicted because "
                f"this process has ranked {DEFAULT_SHORTLIST_CAPACITY} more recent auctions"
            ),
        )
    return shortlist
