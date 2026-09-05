"""``POST /auctions/{auction_id}/accept`` — the HTTP door onto the accept path (T-170, T-169).

``packages/contracts/openapi/exchange.openapi.json`` publishes this path. Until this module
existed nothing served it: :func:`exchange.main.create_app` mounts routers by globbing
``<feature>/routes.py`` and this package shipped none, so the booted app answered exactly
``['/auctions', '/auctions/{auction_id}']``. The whole accept half — the registered-domain
check, the R12 re-read, the mint, the C11 event sequence, the orphan record — was complete,
tested and lint-enforced, and no deployed request could reach any of it. A published path a
service does not answer is a promise the platform is already making to clients, which is why
this is a defect and not a scheduling note.

What this module owns, and what it deliberately does not
--------------------------------------------------------

**It is the deployment call site for the platform's seller registry (T-169).**
:func:`configure_accept` is the only production call to
:func:`~.offer.use_registered_domains` in this repository, and its default is
:class:`~..checkout.sellers.NoRegisteredDomains` — *fail closed*. Before it, nothing anywhere
called that seam, so ``_platform_domains`` stayed ``None`` in every real deployment,
``CheckoutRequest(registered_domains=None)`` was built, and ``registered_domain_for`` fell
back to ``bid["store_domain"]`` — a field the bidding store wrote. The host guard then
compared the store's word to the store's own word and could only refuse a store that
contradicted itself, which no attacker does. The registry is passed to
:func:`~.gate.accept_offer` **explicitly** as well, so this route is bound whether or not the
process-wide seam was ever turned; the seam is wired too so that any other caller in the
process which passes nothing gets the same platform table rather than the bid's claim.

The fail-closed default has a consequence worth stating plainly: an exchange nobody has
connected to a seller registry mints **nothing** through this route. That is the same rule
``StaticSellerEligibility`` already applies to eligibility here — refusing every store beats
quietly trusting every store — and it is what ``sellers.py`` has documented as the default
since it was written.

**It does not persist bids, and today nothing does.** ``accept()`` needs the auction's bids
(``bid_id``, ``store_id``, ``store_domain``, ``offer``); :class:`~..auction.state.AuctionRecord`
carries ``roster`` and no bids at all, and ``POST /auctions`` drops the ``BidEntry`` list it
collected once it has rendered the response. So the bids reach this route through an injected
port, :attr:`app.state.auction_bids`, whose default — :class:`NoRecordedBids` — knows nothing
and therefore refuses everything with ``unknown_bid``. Reconstructing bids from ``roster``
would be worse than refusing: a roster row carries a *list price* and no ``store_domain``, so
an accept built from one would hand the buyer a discount on a price the store never offered.
Wiring the collected bids into this port belongs to whoever owns ``auction/routes.py``; it is
one call to :meth:`InMemoryAuctionBids.record` and it is reported in this ticket's NEEDS
rather than made here.

**It persists the acceptance, and since T-158 it is atomic.** ``accept()`` stamps the auction
*object* it is handed; a route that did not write that back would leave the next request
reading an unstamped record, so this one closes the auction through
:meth:`~..auction.state.AuctionStateMachine.accept`, whose store is where the stamp survives
the request.

What used to stand here said the ``ACCEPTED`` transition was "the serialised one" and that the
read-modify-write window between the legality check and the write was "a different ticket".
Both statements were wrong, and the second one was wrong in the expensive direction. The
transition was a bare ``get`` -> mutate -> ``save``, and the window was not a theoretical one:
driven through *this* route on a store with a 2 ms round trip — which is what
``RedisAuctionStore`` is, two separate network calls — two concurrent requests answered
``HTTP 200`` twice and the merchant minted two live single-use discount codes for one purchase,
3 runs out of 3. It reproduced with the **same** ``bid_ref`` in both requests too, so a
double-clicked button was enough; it did not need two bids or an attacker.

Two things closed it, and the ordering of the second is the whole repair:

* :meth:`~..auction.state.AuctionStore.reserve` makes the state transition genuinely atomic —
  one ``SET … NX`` against the store the auction lives in, so it holds across processes; and
* :func:`configure_accept` wires :class:`~.claims.StoreAcceptanceClaims` over that same store,
  and :func:`~.offer.accept` takes that claim **before** ``POST /codes``. An atomic transition
  applied *after* the mint would have made the loser's 409 correct and left its live discount
  code in the seller's account regardless.

The legality of the transition is still checked before anything is minted, for the same reason
``_acceptance_is_recordable`` is: an auction that cannot record its acceptance cannot refuse
the second accept either, and discovering that after ``POST /codes`` has issued a live
single-use discount is discovering it too late.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..auction.state import (
    ACCEPTED,
    TRANSITIONS,
    AuctionStateMachine,
    IllegalAuctionTransition,
    UnknownAuction,
)
from ..checkout import DEFAULT_CHECKOUT_MODE, NoRegisteredDomains, UnknownCheckoutMode
from ..eligibility import StaticSellerEligibility
from ._spellings import bind_spellings
from .claims import StoreAcceptanceClaims, acceptance_claims_scope
from .gate import accept_offer
from .offer import use_registered_domains
from .reasons import (
    DENIAL_AUCTION_NOT_ACCEPTABLE,
    DENIAL_UNSPECIFIED,
    denial_code,
    denial_reason,
)

__all__ = [
    "AcceptBidRequest",
    "AcceptDeniedResponse",
    "AcceptedOfferResponse",
    "InMemoryAuctionBids",
    "NoRecordedBids",
    "configure_accept",
    "router",
]

router = APIRouter(tags=["accept"])

#: The environment variable D23 names for the checkout mode, read once per request so a
#: deployment can change it without a code change. :func:`configure_accept` overrides it.
CHECKOUT_MODE_ENV = "CHECKOUT_MODE"


# =====================================================================================
# The bid port — where an auction's bids come from
# =====================================================================================
class NoRecordedBids:
    """The fail-closed default: this exchange records no bids, so it can accept none.

    Returning an empty sequence rather than raising is deliberate — ``accept()`` turns "this
    auction carries no such bid" into a refusal that names the auction and offers the next
    slot, which is the answer a buyer can act on. See the module docstring for why rebuilding
    bids from the auction's ``roster`` would be worse than refusing.
    """

    def bids_for(self, auction_id: str) -> Sequence[Mapping[str, Any]]:
        return ()

    __call__ = bids_for


class InMemoryAuctionBids:
    """A process-local ``auction_id -> bids`` book, for a deployment (or a test) to fill.

    The shape each bid must have is the shape ``accept()`` reads: ``bid_id`` (or ``bid_ref``),
    ``store_id``, ``store_domain`` and ``offer``. It is the shape ``BidEntry.bid`` already
    carries out of ``collect_bids``.
    """

    def __init__(self) -> None:
        self._bids: dict[str, list[Mapping[str, Any]]] = {}

    def record(self, auction_id: str, bids: Sequence[Mapping[str, Any]]) -> None:
        self._bids[str(auction_id)] = [dict(bid) for bid in bids]

    def bids_for(self, auction_id: str) -> Sequence[Mapping[str, Any]]:
        return tuple(self._bids.get(str(auction_id), ()))

    __call__ = bids_for


# =====================================================================================
# Request / response bodies — the published contract's shapes
# =====================================================================================
class AcceptBidRequest(BaseModel):
    """``{"bid_ref": "bid-0001"}``, and nothing else: the contract forbids extra fields."""

    model_config = ConfigDict(extra="forbid")

    bid_ref: str


class AcceptedOfferResponse(BaseModel):
    """200 — the buyer follows this permalink and never mints its own (R3)."""

    model_config = ConfigDict(extra="forbid")

    permalink_url: str
    code: str | None = None


class AcceptDeniedResponse(BaseModel):
    """409 — the accept was refused, and ``denial_reason`` names the condition."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool = False
    denial_reason: str


# =====================================================================================
# Wiring
# =====================================================================================
def configure_accept(
    app: FastAPI,
    *,
    registered_domains: Any | None = None,
    eligibility: Any | None = None,
    code_creator: Any | None = None,
    checkout_mode: str | None = None,
    bids: Any | None = None,
    machine: AuctionStateMachine | None = None,
    claims: Any | None = None,
) -> None:
    """Wire the accept route's dependencies. Anything omitted keeps what is already there.

    ``registered_domains`` is also handed to :func:`~.offer.use_registered_domains`, which is
    the process-wide seam ``accept()`` consults for any caller that passes nothing. Wiring
    both is the point: the route binds itself explicitly, and no *other* call site in the
    process is left comparing the bidding store's ``store_domain`` against itself.

    ``eligibility`` and ``machine`` share ``app.state`` keys with
    :func:`~..auction.routes.configure_auctions`, so an exchange configured once is
    configured for both doors.

    ``claims`` is the T-158 acceptance-claim table. Omitting it is the *normal* case and does
    not leave the route on a process-local default: :func:`_claims` derives the table from the
    machine's own store, because a claim that does not live where the auction lives is not a
    claim on the auction — it is a claim on this process's memory, and the second uvicorn
    worker mints the second code. Pass it explicitly only to substitute a different durable
    constraint, such as a unique index in Postgres.

    Unlike ``registered_domains`` this is **not** also written to a process-wide seam. That
    seam exists for ``registered_domains`` because a call site that forgets it silently gets a
    bidder-controlled value; a call site that forgets this one gets
    :class:`~.claims.InMemoryAcceptanceClaims`, which is a floor rather than a hole, and a
    per-process global holding a per-app store is a leak between two apps in one process
    rather than a protection.
    """
    if registered_domains is not None:
        app.state.registered_domains = registered_domains
        # THE call site T-169 exists for. Nothing in this repository called this seam
        # before, so `_platform_domains` was `None` under `uvicorn exchange.main:app` and
        # every accept fell back to a bidder-controlled field.
        use_registered_domains(registered_domains)
    if eligibility is not None:
        app.state.seller_eligibility = eligibility
    if code_creator is not None:
        app.state.code_creator = code_creator
    if checkout_mode is not None:
        app.state.checkout_mode = str(checkout_mode)
    if bids is not None:
        app.state.auction_bids = bids
    if machine is not None:
        app.state.auction_machine = machine
    if claims is not None:
        app.state.acceptance_claims = claims
    elif machine is not None:
        # Re-derive whenever the machine changes: a claim table left pointing at the previous
        # machine's store would guard an auction book this app no longer serves.
        app.state.acceptance_claims = StoreAcceptanceClaims(machine.store)


def _machine(request: Request) -> AuctionStateMachine:
    machine = getattr(request.app.state, "auction_machine", None)
    if machine is None:
        machine = AuctionStateMachine()
        request.app.state.auction_machine = machine
    return machine


def _claims(request: Request) -> Any:
    """The acceptance-claim table for this app — the auction store's, unless one was wired.

    Derived rather than defaulted, and that is the T-158 fix at the wiring level: the guard is
    only as durable as where it is kept, so it is kept in the same store the auction record is
    kept in. An app running on :class:`~..auction.state.RedisAuctionStore` gets a constraint
    every process shares; one running on the in-memory default gets a constraint every thread
    in *this* process shares, which is exactly as much as that store can honestly offer.
    """
    claims = getattr(request.app.state, "acceptance_claims", None)
    if claims is None:
        claims = StoreAcceptanceClaims(_machine(request).store)
        request.app.state.acceptance_claims = claims
    return claims


def _eligibility(request: Request) -> Any:
    eligibility = getattr(request.app.state, "seller_eligibility", None)
    if eligibility is None:
        # Fail closed by default: no rows, and an unknown store answers UNAVAILABLE. The
        # same default `POST /auctions` installs, read from the same `app.state` key.
        eligibility = StaticSellerEligibility()
        request.app.state.seller_eligibility = eligibility
    return eligibility


def _registered_domains(request: Request) -> Any:
    source = getattr(request.app.state, "registered_domains", None)
    if source is None:
        # Fail closed for the DEPLOYMENT, not merely for a lookup: an exchange nobody has
        # connected to the platform's seller registry mints nothing, rather than quietly
        # falling back to the bid's own claim about its own domain.
        configure_accept(request.app, registered_domains=NoRegisteredDomains())
        source = request.app.state.registered_domains
    return source


def _bids_for(request: Request, auction_id: str) -> list[Mapping[str, Any]]:
    source = getattr(request.app.state, "auction_bids", None)
    if source is None:
        source = NoRecordedBids()
        request.app.state.auction_bids = source
    reader = getattr(source, "bids_for", None)
    if not callable(reader):
        if not callable(source):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"the wired auction bid source of type {type(source).__name__!r} exposes "
                    f"neither bids_for(auction_id) nor __call__(auction_id)"
                ),
            )
        reader = source
    return list(reader(auction_id) or ())


def _checkout_mode(request: Request) -> str:
    configured = getattr(request.app.state, "checkout_mode", None)
    if configured:
        return str(configured)
    return str(os.environ.get(CHECKOUT_MODE_ENV) or DEFAULT_CHECKOUT_MODE)


def _denied(reason: str) -> JSONResponse:
    """The 409 body the contract publishes: ``{accepted, denial_reason}`` and nothing else.

    This is where the declared vocabulary becomes a property of the **published surface**
    rather than only of ``accept()`` (T-204). A reason arriving here with a code nothing
    declares — a future refusal added elsewhere, an eligibility source's own prose that got
    through — is re-published under ``unspecified`` with its words kept intact, so a client
    parsing ``denial_reason`` never has to handle a token outside
    :data:`~.reasons.DENIAL_REASONS`, and no diagnosis is thrown away to achieve that.
    """
    text = str(reason).strip()
    if denial_code(text) is None:
        text = denial_reason(
            DENIAL_UNSPECIFIED, text or "the accept was refused and named no reason"
        )
    return JSONResponse(
        status_code=409,
        content=AcceptDeniedResponse(accepted=False, denial_reason=text).model_dump(),
    )


# =====================================================================================
# The route
# =====================================================================================
@router.post(
    "/auctions/{auction_id}/accept",
    response_model=AcceptedOfferResponse,
    responses={
        404: {"description": "No such auction — never created, or its 15-minute TTL expired."},
        409: {"model": AcceptDeniedResponse, "description": "The accept was refused."},
        503: {"description": "The exchange is misconfigured for checkout."},
    },
    summary="Accept one shortlisted bid and receive the exchange-minted permalink.",
)
async def accept_bid(auction_id: str, body: AcceptBidRequest, request: Request) -> Any:
    """Re-read eligibility, check out through the port, and answer with the permalink.

    Every refusal that is *about this buyer's offer* is a 409 carrying ``denial_reason`` —
    an unknown bid, a store blacklisted since it bid, an off-domain checkout URL, a merchant
    that would not mint. A 404 means the auction itself is gone, and a 503 means the
    deployment is misconfigured (an unregistered ``CHECKOUT_MODE``, an unusable bid source):
    neither is a decision about this buyer, so neither is dressed up as one.
    """
    machine = _machine(request)
    try:
        record = machine.get(auction_id)
    except UnknownAuction as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Asked BEFORE anything is minted: an auction that cannot record its acceptance cannot
    # refuse the second accept either. See the module docstring on what this does not close.
    if ACCEPTED not in TRANSITIONS.get(record.state, frozenset()):
        return _denied(
            denial_reason(
                DENIAL_AUCTION_NOT_ACCEPTABLE,
                f"auction {auction_id!r} is {record.state!r}, from which {ACCEPTED!r} is not "
                f"a legal move; no discount code is created for it",
            )
        )

    now = time.time()
    auction = {
        "auction_id": record.auction_id,
        "bids": _bids_for(request, auction_id),
        # The stamp as PERSISTED, so a second accept on a reloaded record is refused by the
        # same guard that refuses a second accept on one object.
        "accepted_bid_ref": record.accepted_bid_ref,
        "now": now,
    }

    try:
        # The atomic one-accept guard (T-158) is taken inside `accept()`, before `POST /codes`,
        # against the store THIS app's auctions live in. It is bound for the duration of the
        # call rather than passed down through `accept_offer`, whose parameter list is a
        # published contract partitioned into data and hostile-input-swept collaborators; see
        # `claims._request_claims` for why a module global would be worse than either.
        with acceptance_claims_scope(_claims(request)):
            result = accept_offer(
                auction=auction,
                bid_ref=body.bid_ref,
                code_creator=getattr(request.app.state, "code_creator", None),
                mode=_checkout_mode(request),
                eligibility=_eligibility(request),
                registered_domains=_registered_domains(request),
            )
    except UnknownCheckoutMode as exc:
        # A deployment misconfiguration, not a decision about this buyer — and the registry's
        # contract is that it never falls back to the simulated path.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not result.accepted:
        return _denied(str(result.denial_reason or ""))

    try:
        machine.accept(auction_id, result.bid_ref, now=now)
    except IllegalAuctionTransition as exc:
        # NOT the T-158 window any more: the acceptance claim inside `accept()` is what makes
        # a second accept impossible, and it was taken before `POST /codes`. What is left here
        # is the auction being EXPIRED concurrently — `expire` and `accept` race for the one
        # move out of `closed` and exactly one wins. A code does exist in that case; `accept()`
        # has filed the events that name it, and the buyer is given no permalink for it.
        return _denied(
            denial_reason(
                DENIAL_AUCTION_NOT_ACCEPTABLE,
                f"auction {auction_id!r} could not be stamped as accepted ({exc}); the "
                f"acceptance is not recorded, so no permalink is returned",
            )
        )

    return AcceptedOfferResponse(permalink_url=str(result.permalink_url), code=result.code)


# LAST: this module is reachable as `exchange.accept.routes` and as
# `apps.exchange.src.accept.routes`, and it is imported by `create_app`'s glob rather than by
# this package's `__init__`, so it binds itself. See `_spellings.py`.
bind_spellings(sys.modules[__name__])
