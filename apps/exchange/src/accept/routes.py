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
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .. import describe_exception, redact_addresses
from ..auction.state import (
    ACCEPTED,
    AUCTION_TTL_SECONDS,
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
    "DEFAULT_BID_BOOK_CAPACITY",
    "DEFAULT_BID_BOOK_RECORDS",
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


#: How many auctions' bids one process holds at once.
#:
#: The same number and the same reasoning as
#: :data:`~..ranking.serving.DEFAULT_SHORTLIST_CAPACITY`, which bounds the sibling store for
#: the sibling reason: ``POST /auctions`` is unauthenticated, it now writes one entry here per
#: request, and a book with no bound is a memory leak anybody can drive by posting in a loop —
#: against ``apps/exchange/compose.yaml``'s ``mem_limit: 256m``. The TTL is the auction's own
#: (``auction:{id}``, 15 minutes), so a bid never outlives the auction it belongs to, and the
#: cap evicts the oldest auction first when a burst arrives inside one window.
DEFAULT_BID_BOOK_CAPACITY = 512

#: The most bid records the book holds in total, across every auction in it.
#:
#: **A cap on auctions is not a cap on memory, and this is the second half of the same bound.**
#: An auction may carry up to ``MAX_ROSTER_ENTRIES`` (500) candidates, so 512 auctions at the
#: cap is 256,000 records — measured with ``tracemalloc`` at exactly that shape::
#:
#:     write 512 x 500  ->  26.89s, peak 173.8 MB
#:
#: of a 256 MiB container, for the container structure alone and before the sibling
#: ``ShortlistStore`` (also 512) and the auction store. 20,000 records is roughly 14 MB at the
#: same measurement, and is still 40 concurrent auctions at the roster ceiling or 4,000 at the
#: size an auction actually has. Oldest auction first, same as the count cap.
DEFAULT_BID_BOOK_RECORDS = 20_000


class InMemoryAuctionBids:
    """A process-local ``auction_id -> bids`` book, written by ``POST /auctions``.

    The shape each bid must have is the shape ``accept()`` reads: ``bid_id`` (or ``bid_ref``),
    ``store_id``, ``store_domain`` and ``offer`` —
    :func:`~..auction.routes.collected_bid_records` assembles exactly that.

    Bounded in size and in time. It was neither, and that was safe only while nothing wrote to
    it: an unbounded dict on the request path of an unauthenticated route is a leak with a
    public handle on it. Both bounds are the auction's own, so this store can never claim to
    know about an auction the state machine has already forgotten.
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_BID_BOOK_CAPACITY,
        max_records: int = DEFAULT_BID_BOOK_RECORDS,
        ttl_seconds: float = AUCTION_TTL_SECONDS,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.max_records = max(1, int(max_records))
        self.ttl_seconds = float(ttl_seconds)
        self._bids: OrderedDict[str, tuple[float, list[Mapping[str, Any]]]] = OrderedDict()

    def record(
        self,
        auction_id: str,
        bids: Sequence[Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> None:
        """Keep one auction's collected bids, evicting the oldest when either cap is reached.

        Expired rows are swept HERE as well as on read. Read-only expiry bounds staleness and
        not memory — measured, 512 long-expired auctions sat in the book indefinitely because
        nobody asked for them — and an auction nobody accepts is the common case, not the
        exception.
        """
        key = str(auction_id)
        self._bids.pop(key, None)
        moment = time.time() if now is None else float(now)
        self._sweep(moment)
        self._bids[key] = (moment, [deepcopy(dict(bid)) for bid in bids])
        while len(self._bids) > self.capacity:
            self._bids.popitem(last=False)
        # The record cap is checked after the count cap and never evicts the row just written:
        # an auction whose own bid list exceeds the total budget still gets to be acceptable,
        # because a book that silently forgot the auction it was just handed would answer
        # `unknown_bid` for the bid the very same request published.
        while len(self._bids) > 1 and self._record_count() > self.max_records:
            self._bids.popitem(last=False)

    def _record_count(self) -> int:
        return sum(len(records) for _written_at, records in self._bids.values())

    def _sweep(self, now: float) -> None:
        """Drop every auction whose TTL has passed, so memory follows the TTL too."""
        expired = [
            key
            for key, (written_at, _records) in self._bids.items()
            if now - written_at >= self.ttl_seconds
        ]
        for key in expired:
            self._bids.pop(key, None)

    def bids_for(self, auction_id: str, *, now: float | None = None) -> Sequence[Mapping[str, Any]]:
        """One auction's bids, or nothing once its TTL has taken them away.

        ``>=`` rather than ``>``, for the reason ``ShortlistStore.get`` gives: at exactly
        ``ttl_seconds`` the auction record itself is gone, and this is the one instant this
        store may not outlive it by.

        Read back as a deep copy. The list is handed straight into ``accept()``, which builds
        a ``CheckoutRequest`` out of the offer inside it; a caller that mutated what it was
        given would otherwise rewrite what the next accept on the same auction reads.
        """
        entry = self._bids.get(str(auction_id))
        if entry is None:
            return ()
        written_at, bids = entry
        moment = time.time() if now is None else float(now)
        if moment - written_at >= self.ttl_seconds:
            self._bids.pop(str(auction_id), None)
            return ()
        return tuple(deepcopy(bid) for bid in bids)

    __call__ = bids_for

    def __len__(self) -> int:
        return len(self._bids)


# =====================================================================================
# Request / response bodies — the published contract's shapes
# =====================================================================================
class AcceptBidRequest(BaseModel):
    """``{"bid_ref": "bid-0001"}``, and nothing else: the contract forbids extra fields."""

    model_config = ConfigDict(extra="forbid")

    bid_ref: str


class AcceptedOfferResponse(BaseModel):
    """200 — the buyer follows this permalink and never mints its own (R3).

    ``code`` is ``None`` on exactly one shape of accept: the exchange's own list-price
    fallback for a store whose agent never answered (R10). ``notice`` is what stops that
    ``null`` from being the only thing that says so — it carries a sentence a shopper can
    read, saying no discount applies and why. A buyer that renders the permalink and ignores
    the rest is unaffected, which is why the field is optional rather than required.
    """

    model_config = ConfigDict(extra="forbid")

    permalink_url: str
    code: str | None = None
    notice: str | None = None


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
    bidder-controlled value; a call site that forgets this one gets the table :func:`_claims`
    derives from its own store, and a per-process global holding a per-app store would be a
    leak between two apps in one process rather than a protection. The route reaches
    :func:`~.offer.accept` through a request-scoped :func:`~.claims.acceptance_claims_scope`
    instead.

    A table passed here is **remembered as injected**, so a later
    ``configure_accept(app, machine=...)`` does not quietly throw it away and re-derive one
    from the new machine's store. "Anything omitted keeps what is already there" has to hold
    for the money guard too, and a deployment that wired its own Postgres unique index and
    then re-wired its machine used to end up back on the store-derived table with no signal.
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
        app.state.acceptance_claims_injected = True
    elif machine is not None and not getattr(app.state, "acceptance_claims_injected", False):
        # Re-derive whenever the machine changes, but never over a table the deployment chose:
        # a derived table left pointing at the previous machine's store would guard an auction
        # book this app no longer serves, while an injected one is the deployment's decision
        # and outranks the derivation.
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

    Derived from ``machine.store`` at first use and cached, so replacing ``machine.store``
    after a request has been served leaves this table pointing at the old store. Re-wire
    through :func:`configure_accept` rather than by assigning to ``machine.store``.
    """
    claims = getattr(request.app.state, "acceptance_claims", None)
    if claims is None:
        store = _machine(request).store
        try:
            claims = StoreAcceptanceClaims(store)
        except TypeError as exc:
            # A store that cannot hold a reservation cannot make this exchange safe, and the
            # honest answer is that the DEPLOYMENT is broken — not that this buyer's offer was
            # refused. Same 503 the unusable bid source gets, for the same reason: dressing a
            # misconfiguration up as a decision about the buyer hides it from the operator,
            # and letting it through would mint on a path with no one-accept guard at all.
            raise HTTPException(status_code=503, detail=redact_addresses(exc)) from exc
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


def _bind_the_deployment(request: Request) -> None:
    """Run the composition root once for this app. See ``auction/routes.py``'s twin.

    A malformed deployment document is a 503 for the reason every other 503 on this route is
    one: it is a statement about the DEPLOYMENT, not a decision about this buyer's offer, and
    dressing it up as one hides it from the operator who can fix it.
    """
    from ..composition import DeploymentConfigurationError, ensure_configured  # noqa: PLC0415

    try:
        ensure_configured(request.app)
    except DeploymentConfigurationError as exc:
        raise HTTPException(status_code=503, detail=redact_addresses(exc)) from exc


def _checkout_mode(request: Request) -> str:
    configured = getattr(request.app.state, "checkout_mode", None)
    if configured:
        return str(configured)
    return str(os.environ.get(CHECKOUT_MODE_ENV) or DEFAULT_CHECKOUT_MODE)


def _accepted_offer(result: Any) -> Mapping[str, Any] | None:
    """The offer body the checkout port already built for its own ``accepted`` event.

    Read back off ``result.events`` rather than rebuilt from the bid, so the offer the
    auction stamps into the ledger is the *same* offer the checkout was made against. The
    trust reconciler grades the webhook against exactly this mapping
    (``reconcile.engine._promised`` reads ``product_ref`` / ``unit_price`` / ``total_price`` /
    ``discount`` off it), and a second reading of the bid could disagree with the first.
    ``None`` when the port emitted no ``accepted`` event, which is not something to invent
    a substitute for.
    """
    for event in getattr(result, "events", ()) or ():
        if not isinstance(event, Mapping) or str(event.get("kind", "")) != ACCEPTED:
            continue
        payload = event.get("payload")
        offer = payload.get("offer") if isinstance(payload, Mapping) else None
        if isinstance(offer, Mapping):
            return offer
    return None


def _denied(reason: str) -> JSONResponse:
    """The 409 body the contract publishes: ``{accepted, denial_reason}`` and nothing else.

    This is where the declared vocabulary becomes a property of the **published surface**
    rather than only of ``accept()`` (T-204). A reason arriving here with a code nothing
    declares — a future refusal added elsewhere, an eligibility source's own prose that got
    through — is re-published under ``unspecified`` with its words kept intact, so a client
    parsing ``denial_reason`` never has to handle a token outside
    :data:`~.reasons.DENIAL_REASONS`, and no diagnosis is thrown away to achieve that.
    """
    # Redacted here as well as at the two places a reason is BUILT, because this function is
    # the published surface's last frame and it is reachable with a string neither of them
    # produced — `_denied` is called directly with `str(result.denial_reason or "")` and with
    # a locally formatted transition message. A 409 body is the one sink a client reads, so
    # the invariant is asserted where it is published, not only where it is composed.
    text = redact_addresses(reason).strip()
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
    # The deployment's own collaborators, bound once per app. `POST /auctions` takes the same
    # hook, so in a single-process exchange this is already done; it is taken here as well
    # because an accept can arrive at a process that never served the auction's creation — two
    # replicas behind one `RedisAuctionStore` — and that process would otherwise reach the
    # registered-domain check with nothing wired and refuse a checkout it can vouch for.
    _bind_the_deployment(request)

    machine = _machine(request)
    try:
        record = machine.get(auction_id)
    except UnknownAuction as exc:
        raise HTTPException(status_code=404, detail=redact_addresses(exc)) from exc

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
        raise HTTPException(status_code=503, detail=redact_addresses(exc)) from exc

    if not result.accepted:
        return _denied(str(result.denial_reason or ""))

    try:
        # The token and the offer travel with the stamp, because the `accepted` event this
        # writes is the only record of the promise the trust reconciler ever sees, and it
        # joins on `payload['checkout_token']`. Stamping without them recorded that an
        # acceptance happened while making it impossible to say what was promised or which
        # order it became.
        machine.accept(
            auction_id,
            result.bid_ref,
            now=now,
            checkout_token=result.checkout_token,
            offer=_accepted_offer(result),
        )
    except IllegalAuctionTransition as exc:
        # NOT the T-158 window any more: the acceptance claim inside `accept()` is what makes
        # a second accept impossible, and it was taken before `POST /codes`. What is left here
        # is the auction being EXPIRED concurrently — `expire` and `accept` race for the one
        # move out of `closed` and exactly one wins. A code does exist in that case; `accept()`
        # has filed the events that name it, and the buyer is given no permalink for it.
        return _denied(
            denial_reason(
                DENIAL_AUCTION_NOT_ACCEPTABLE,
                f"auction {auction_id!r} could not be stamped as accepted "
                f"({describe_exception(exc)}); the "
                f"acceptance is not recorded, so no permalink is returned",
            )
        )

    return AcceptedOfferResponse(
        permalink_url=str(result.permalink_url),
        code=result.code,
        notice=result.discount_notice,
    )


# LAST: this module is reachable as `exchange.accept.routes` and as
# `apps.exchange.src.accept.routes`, and it is imported by `create_app`'s glob rather than by
# this package's `__init__`, so it binds itself. See `_spellings.py`.
bind_spellings(sys.modules[__name__])
