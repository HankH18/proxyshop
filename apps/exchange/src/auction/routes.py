"""``POST /auctions`` — the HTTP door onto the whole R10/R12 path.

DESIGN pins the route: ``POST /auctions`` (Intent + profile -> auction_id). One request runs
the auction end to end, because R10 makes solicitation *synchronous*: the buyer is waiting,
so the call opens the auction, gates the roster, fans out in parallel, closes at the
deadline, and answers with what every store is offering.

The sequence is the point, and it is the same sequence the unit tests drive:

.. code-block:: text

    create -> OPEN --(auction_opened)-->  solicit_bids   -> close --(auction_closed)-->
              R12 gate on every rostered store    parallel fan-out, hard timeout
           -> rank --(R19 filters, published formula, D29 shortlist)--> answer

The ranking step is the last one and it is not optional (T-310). Until it was wired, a served
auction answered with the offers in whatever order the fan-out returned them: R19's hard
constraints, R12's blacklist read and C10's checkout-domain check ran in ``exchange.ranking``
on no request at all, so a candidate ``rank()`` would have excluded reached the buyer anyway.
``entries`` still reports every rostered store — that is the auction's own record of who
offered what — while ``ranked``, ``excluded`` and ``shortlist`` are what the ranking decided,
and ``shortlist`` is the buyer-facing object the published contract serves at
``GET /auctions/{auction_id}/shortlist``.

Wiring is injected, never imported into place, and the defaults are chosen so that an
un-wired service is **safe rather than convenient**:

* ``app.state.seller_eligibility`` defaults to
  :class:`~apps.exchange.src.eligibility.StaticSellerEligibility` with no rows, whose default
  answer is ``UNAVAILABLE`` — so an exchange nobody has connected to a trust service denies
  every store instead of quietly admitting every store. That is R12's fail-closed rule
  applied to the *deployment*, not just to the read.
* ``app.state.bid_solicitor`` defaults to a solicitor that answers nothing, so every
  eligible store is represented at its list price (R10) rather than the request failing.
* ``app.state.trust_snapshot`` defaults to EMPTY, and an empty snapshot denies every store
  rather than admitting every store: a store with no row cannot be shown to be off the
  blacklist (R12). ``app.state.ranking_registered_domains`` behaves the same way — an exchange
  that has not been given the platform's seller registry can vouch for no checkout host, so
  every candidate is off-domain (C10/D22). Both are the *deployment* reading of a fail-closed
  rule, exactly as the eligibility default above is.

:func:`configure_auctions` is how a deployment (or a test) replaces either of the first two;
:func:`~exchange.ranking.serving.configure_ranking` is how it replaces the ranking's.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from ..eligibility import StaticSellerEligibility
from ..orchestration import solicit_bids
from ..ranking.serving import (
    rank_auction,
    registered_domains_of,
    shortlist_store,
    trust_snapshot_of,
    weights_of,
)
from .fanout import parallel_fan_out
from .state import AuctionStateMachine, UnknownAuction

__all__ = [
    "DEFAULT_BID_TIMEOUT_SECONDS",
    "MAX_BID_TIMEOUT_SECONDS",
    "NullSolicitor",
    "bid_window_seconds",
    "configure_auctions",
    "router",
]

router = APIRouter(tags=["auctions"])

#: R10's hard timeout. Short on purpose: a buyer is synchronously waiting on this call.
DEFAULT_BID_TIMEOUT_SECONDS = 3.0

#: The **server's** ceiling on that timeout, and it is not negotiable by the caller.
#:
#: ``bid_timeout_seconds`` arrives on an unauthenticated request body and this route is
#: synchronous end to end: the window the caller names is time a worker spends parked. With
#: only a lower clamp (``max(0.0, ...)``) anyone could post ``bid_timeout_seconds: 86400``
#: and hold a worker for a day, and enough such requests take the service down without a
#: single credential. So the caller may ask for *less* than the default and is capped here
#: when it asks for more. Clamping rather than rejecting is deliberate: a client that asks
#: for too long is not attacking anyone in particular, and giving it the maximum window is
#: a better answer than a 422 it has no way to interpret.
MAX_BID_TIMEOUT_SECONDS = 10.0


def bid_window_seconds(requested: float) -> float:
    """The real window this auction gets: what was asked for, clamped at both ends.

    Total order matters more than it looks. ``nan`` compares false against everything, so
    ``max(0.0, nan)`` is ``0.0`` and the ``min`` below leaves it there — a garbage timeout
    becomes "no window", never "an infinite one". ``inf`` clamps to the ceiling.
    """
    return min(MAX_BID_TIMEOUT_SECONDS, max(0.0, float(requested)))


class NullSolicitor:
    """The default outbound client: asks nobody, so every store falls back to list price.

    A deployment replaces it with a real ``POST /v1/bid-requests`` client. It exists so an
    unconfigured exchange degrades to catalog prices instead of raising.
    """

    def solicit(self, store: Mapping[str, Any]) -> None:
        return None

    __call__ = solicit


class RosterEntry(BaseModel):
    """One rostered store, **as the unauthenticated request body states it**.

    Every field here is caller-supplied. The exchange has no authentication of any kind
    (``git grep -nE "Depends|api_key|Authorization" apps/exchange/src`` is empty), so
    ``list_price`` and ``max_discount_pct`` are not facts the exchange holds about a catalog —
    they are assertions the caller makes about one, and the T-177 price wall in
    :mod:`~apps.exchange.src.auction.collect` is only as good as they are. That is a known,
    unclosed gap and it is written down here rather than implied: the authoritative cap needs a
    derived-authorization port of its own (the shape R12's ``SellerEligibility`` already uses),
    because C3/S7 forbids the exchange from ever reading a merchant's `Envelope` — the C3 contract
    in ``.importlinter`` enforces exactly that. (Cited by its C3 name rather than spelled out: the
    frozen C3/S7 acceptance check scans string literals here, so quoting the rule's full name in a
    docstring trips the rule itself.)

    What is closed here is the part that does not wait on that port: **pricing the product at
    nothing** — by omitting ``list_price`` or by writing a zero into it — is a 422, and a bid
    cannot be priced at nothing, or at a positive number that is not a price, on any row.

    This docstring used to claim "a free item cannot be minted through this model whatever the
    caller writes in it", and that was measured false twice over. Both holes are now closed, and
    both are written down here because an overclaiming comment is how the next reader stops
    looking:

    * ``list_price: 0.0`` was an accepted value (``Field(ge=0.0)``). On such a row a silent store
      minted a 0.00 rankable fallback — ``HTTP 201, entries=[{fallback: true, unit_price: 0.0,
      fallback_reason: 'no_response'}]`` — which is the same free item the missing-field 422
      closed, reached by writing the zero instead of omitting it; and the same row switched off
      the guard that turns an unreadable store price into a fallback, so ``unit_price: "cheap"``
      raised ``ValueError`` out of the middle of the auction as an unauthenticated **HTTP 500**.
      T-224. The field is ``Field(gt=0.0)`` now, and the 500 is closed a second time in
      :func:`~apps.exchange.src.auction.collect._price_is_unreadable`, which asks nothing of the
      roster: a repair that lives only in a request model is a repair a second caller of
      ``collect_bids`` does not get.
    * the zero-price floor was an equality (``priced == 0.0``), so under ``max_discount_pct: 100``
      an offer at ``unit_price: 0.001`` — or, on a row stating no cap at all, ``1e-09`` — was
      admitted through the real door as a rankable bid for a 100.00 product. T-223. It is a
      threshold now, absolute and proportional, in
      :func:`~apps.exchange.src.auction.collect._below_the_price_floor`.

    What is still open is what it always was: everything BETWEEN the floor and the cap is the
    request body's word, and closing that needs the derived-authorization port named above.
    """

    store_id: str
    tier: int = 1
    product_ref: str | None = None
    #: **Required, and strictly above zero.** It used to default to ``0.0``, which minted a free
    #: item with no bid involved at all: a roster row naming no price produced a 0.00 *fallback*
    #: offer for a silent store, and that offer wins every ranking there is. Measured before that
    #: change — ``POST /auctions`` with ``{"store_id": "s1", "tier": 1, "product_ref": "prod-1"}``
    #: and no solicitor — ``HTTP 201, entries=[{fallback: true, unit_price: 0.0}]``.
    #:
    #: Making it required left the same free item one keystroke away, because ``ge=0.0`` accepted
    #: the zero it had just stopped defaulting to, with the identical measured result. ``gt``, not
    #: ``ge``: a caller that cannot price a product cannot auction it, and "prices it at nothing"
    #: is not a different statement from "does not price it". That zero was also the switch that
    #: turned the price wall off entirely on the row carrying it — see the class docstring.
    #: ``allow_inf_nan=False`` is load-bearing and was added after a rung-2 verifier drove a free
    #: item through this field. ``gt=0.0`` does NOT refuse ``+inf``: ``inf > 0.0`` is ``True``, and
    #: ``1e400`` is legal RFC-8259 JSON needing no malformed body and no lenient parser. An ``inf``
    #: row then read as UNREADABLE everywhere downstream — ``_number`` excludes non-finite by
    #: design — so ``_below_the_price_floor`` and ``_priced_at_nothing`` both took their
    #: ``listed is None`` early-out and the entire price wall switched off on that row, while
    #: ``_list_price_bid`` minted a rankable ``0.00``. That is T-224's own reproduction reached
    #: through a field T-224 was supposed to have closed.
    list_price: float = Field(gt=0.0, allow_inf_nan=False)
    #: The deepest percentage discount the caller states is authorized on this product — the
    #: policy `Envelope`'s own spelling. Optional, and its absence is not permissive: a bid
    #: DECLARING a discount on a row that authorizes none is refused and falls back to the list
    #: price (T-177). Its presence is not permissive either — the wall's floor holds at
    #: ``max_discount_pct: 100`` — but everything between the floor and the cap IS this number's
    #: word, which is the gap the class docstring names. Omitting it costs an auction its
    #: discounted bids, never its safety, which is the direction to fail in on a field that
    #: decides money.
    max_discount_pct: float | None = Field(default=None, ge=0.0, le=100.0)


class CreateAuctionRequest(BaseModel):
    intent: dict[str, Any]
    profile: dict[str, Any] | None = None
    roster: list[RosterEntry] = Field(default_factory=list)
    #: R10's hard timeout for this auction, in seconds.
    bid_timeout_seconds: float = DEFAULT_BID_TIMEOUT_SECONDS


class AuctionEntryOut(BaseModel):
    store_id: str
    tier: int
    fallback: bool
    unit_price: float
    total_price: float
    fallback_reason: str | None = None


class DenialOut(BaseModel):
    store_id: str
    status: str
    reason: str


class RankedBidOut(BaseModel):
    """One candidate the published ranking scored, best first in the response."""

    bid_ref: str
    store_id: str
    rank_score: float
    #: The weighted terms, which sum to ``rank_score``. Returned so a reader can see WHICH
    #: feature produced a placement without re-running the ranker — the auditability
    #: :mod:`~exchange.ranking.scoring` builds them for is worth nothing if the served answer
    #: throws them away.
    components: dict[str, float] = Field(default_factory=dict)


class ExcludedBidOut(BaseModel):
    """One candidate the filters refused, and every reason they refused it.

    Every reason, not the first: a candidate that is both blacklisted and off-domain has two
    things wrong with it, and reporting one of them makes the second invisible to whoever
    fixes the first.
    """

    bid_ref: str
    store_id: str
    exclusion_reasons: list[str] = Field(default_factory=list)


class CreateAuctionResponse(BaseModel):
    auction_id: str
    state: str
    solicited: list[str]
    entries: list[AuctionEntryOut]
    denied: list[DenialOut]
    #: The eligible candidates in published rank order (D13). Empty on an exchange with no
    #: trust snapshot and no registered domains, because both of those fail closed.
    ranked: list[RankedBidOut] = Field(default_factory=list)
    #: The candidates the eligibility filters excluded, each naming its reasons.
    excluded: list[ExcludedBidOut] = Field(default_factory=list)
    #: The buyer-facing shortlist (R2/A6/D29/D30) — the same object
    #: ``GET /auctions/{auction_id}/shortlist`` serves, and the pinned ``Shortlist`` shape.
    shortlist: dict[str, Any] = Field(default_factory=dict)


def configure_auctions(
    app: FastAPI,
    *,
    machine: AuctionStateMachine | None = None,
    solicitor: Any | None = None,
    eligibility: Any | None = None,
) -> None:
    """Wire an app's auction dependencies. Anything omitted keeps what is already there."""
    if machine is not None:
        app.state.auction_machine = machine
    if solicitor is not None:
        app.state.bid_solicitor = solicitor
    if eligibility is not None:
        app.state.seller_eligibility = eligibility


def _machine(request: Request) -> AuctionStateMachine:
    machine = getattr(request.app.state, "auction_machine", None)
    if machine is None:
        machine = AuctionStateMachine()
        request.app.state.auction_machine = machine
    return machine


def _solicitor(request: Request) -> Any:
    solicitor = getattr(request.app.state, "bid_solicitor", None)
    if solicitor is None:
        solicitor = NullSolicitor()
        request.app.state.bid_solicitor = solicitor
    return solicitor


def _eligibility(request: Request) -> Any:
    eligibility = getattr(request.app.state, "seller_eligibility", None)
    if eligibility is None:
        # Fail closed by default: no rows, and an unknown store answers UNAVAILABLE.
        eligibility = StaticSellerEligibility()
        request.app.state.seller_eligibility = eligibility
    return eligibility


def _entries_out(entries: Sequence[Any]) -> list[AuctionEntryOut]:
    out: list[AuctionEntryOut] = []
    for entry in entries:
        offer = entry.bid.get("offer", {})
        out.append(
            AuctionEntryOut(
                store_id=entry.store_id,
                tier=entry.tier,
                fallback=entry.fallback,
                unit_price=float(offer.get("unit_price", 0.0)),
                total_price=float(offer.get("total_price", offer.get("unit_price", 0.0))),
                fallback_reason=entry.fallback_reason,
            )
        )
    return out


def _ranked_out(ranked: Sequence[Mapping[str, Any]]) -> list[RankedBidOut]:
    return [
        RankedBidOut(
            bid_ref=str(row["bid_id"]),
            store_id=str(row["store_id"]),
            rank_score=float(row["rank_score"]),
            components={str(k): float(v) for k, v in (row.get("components") or {}).items()},
        )
        for row in ranked
    ]


def _excluded_out(rows: Sequence[Mapping[str, Any]]) -> list[ExcludedBidOut]:
    """The ineligible rows, in the order they were offered — never the eligible ones.

    Read off ``candidates`` (every row) rather than off a second ranker call: ``rank()``
    returns the ranked rows and the full set, and asking it twice would be asking a question
    that already has an answer.
    """
    return [
        ExcludedBidOut(
            bid_ref=str(row["bid_id"]),
            store_id=str(row["store_id"]),
            exclusion_reasons=[str(reason) for reason in row.get("exclusion_reasons") or ()],
        )
        for row in rows
        if not row.get("eligible")
    ]


@router.post("/auctions", response_model=CreateAuctionResponse, status_code=201)
async def create_auction(body: CreateAuctionRequest, request: Request) -> CreateAuctionResponse:
    """Open an auction, gate the roster, fan out with a hard timeout, close, and answer."""
    machine = _machine(request)
    intent = body.intent
    auction_id = f"auction-{uuid.uuid4()}"
    roster = [entry.model_dump() for entry in body.roster]

    opened_at = time.time()
    # Taken next to `opened_at`, and for the same instant: this is the monotonic reading the
    # whole window is measured from. Everything between here and the fan-out (two ledger
    # writes and an eligibility read per rostered store) is I/O, and it is spent INSIDE the
    # window — which is what makes R10's timeout a bound on this request rather than only on
    # the part of it that talks to stores.
    started_at = time.monotonic()
    window = bid_window_seconds(body.bid_timeout_seconds)
    deadline = opened_at + window

    machine.create(
        auction_id,
        intent_id=str(intent.get("intent_id", "")),
        cluster_id=str(intent.get("cluster_id", "")),
        roster=roster,
        deadline=deadline,
    )
    machine.open(auction_id, now=opened_at)

    result = solicit_bids(
        roster=roster,
        solicitor=_solicitor(request),
        eligibility=_eligibility(request),
        now=deadline,
        fan_out=parallel_fan_out,
        # The real duration of the window, so the exchange's arrival clock and this
        # request's deadline are the same window measured two ways. Without it a store
        # answering after `bid_timeout_seconds` would be stamped against the platform
        # default instead of the timeout this auction actually granted.
        window=window,
        started_at=started_at,
    )

    # ONE clock reading, used for the close transition and for the ranking's `now`. Two
    # readings would let an offer expire between the auction closing and the ranking that
    # decides whether it was live at the close, which is not a question two instants can
    # answer consistently.
    closed_at = time.time()
    record = machine.close(auction_id, now=closed_at)

    ranking = rank_auction(
        result.entries,
        auction_id=auction_id,
        intent=intent,
        now=closed_at,
        trust_snapshot=trust_snapshot_of(request.app),
        registered_domains=registered_domains_of(request.app),
        weights=weights_of(request.app),
    )
    shortlist = ranking["shortlist"]
    shortlist_store(request.app).put(auction_id, shortlist, now=closed_at)

    return CreateAuctionResponse(
        auction_id=auction_id,
        state=record.state,
        solicited=list(result.solicited),
        entries=_entries_out(result.entries),
        denied=[
            DenialOut(store_id=d.store_id, status=d.status, reason=d.reason) for d in result.denied
        ],
        ranked=_ranked_out(ranking["ranked"]),
        excluded=_excluded_out(ranking["candidates"]),
        shortlist=shortlist,
    )


@router.get("/auctions/{auction_id}")
async def read_auction(auction_id: str, request: Request) -> dict[str, Any]:
    """The auction's current state — 404 once its 15-minute TTL has taken it away."""
    try:
        record = _machine(request).get(auction_id)
    except UnknownAuction as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "auction_id": record.auction_id,
        "state": record.state,
        "intent_id": record.intent_id,
        "cluster_id": record.cluster_id,
        "accepted_bid_ref": record.accepted_bid_ref,
        "history": record.history,
    }
