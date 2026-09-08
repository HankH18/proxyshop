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

import json
import logging
import os
import sys
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
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
from ..checkout import (
    CHECKOUT_EVENT_KINDS,
    DEFAULT_CHECKOUT_MODE,
    NoRegisteredDomains,
    UnknownCheckoutMode,
)
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
    "RenderableJSONResponse",
    "RenderableValidationErrorRoute",
    "configure_accept",
    "renderable_validation_detail",
    "router",
]

_log = logging.getLogger(__name__)


# =====================================================================================
# Rendering a validation failure that quotes a number JSON cannot spell (T-270)
# =====================================================================================
#
# `POST /auctions/{auction_id}/accept` carries this defect too, not only `POST /auctions`:
# measured on merged main, all 24 accept cases in the T-270 corpus answered 500. A body
# spelling `NaN`, `Infinity` or `1e400` is accepted by `json.loads`, rejected by pydantic,
# and then ECHOED back inside FastAPI's stock 422, which starlette serialises with
# `allow_nan=False` and cannot encode. The 500 is in the error renderer, so it belongs to
# every door that declares a pydantic request model rather than to any one field.
#
# DUPLICATED FROM `auction/routes.py` ON PURPOSE, for the reason `policy/routes.py:326`
# gives for its own twin of this problem and `_bind_the_deployment` is duplicated between
# this module and `auction/routes.py`: each feature package owns its own door, and a
# thirty-line renderer is not worth a new cross-feature import edge — especially one that
# would make `exchange.accept.routes` (imported FIRST by `main.py`'s sorted glob) pull the
# whole auction module in behind it. The long-form rationale — why a route class rather
# than an exception handler, and why the value is rendered rather than refused — is in
# `auction/routes.py` above its copy and is not repeated here.


def renderable_validation_detail(errors: Any) -> list[Any]:
    """``errors`` with every non-finite float replaced by the string JSON would have spelt.

    The walk is delegated to ``json`` because ``input`` is the caller's own body and its depth
    is caller-chosen; a hand-rolled recursion would fault on exactly the input this exists to
    render. Anything still unrenderable falls back to field paths without their values, so the
    422 keeps a non-empty per-field ``detail`` list rather than degrading to a 5xx.
    """
    try:
        encoded = jsonable_encoder(errors)
        round_tripped = json.loads(
            json.dumps(encoded, allow_nan=True), parse_constant=lambda token: token
        )
    except (ValueError, TypeError, RecursionError):
        round_tripped = None
    if isinstance(round_tripped, list) and round_tripped:
        return round_tripped
    fallback = [
        {
            "type": str(error.get("type", "value_error")),
            "loc": [str(part) for part in (error.get("loc") or ())],
            "msg": redact_addresses(error.get("msg", "this value could not be validated")),
        }
        for error in (errors or ())
        if isinstance(error, Mapping)
    ]
    return fallback or [
        {"type": "value_error", "loc": ["body"], "msg": "the request body could not be read"}
    ]


class RenderableJSONResponse(JSONResponse):
    """A :class:`JSONResponse` that can still be encoded when the body quotes ``inf``/``nan``.

    **Ungraded defence in depth — no gate is red without it**, and the twin's docstring in
    ``auction/routes.py`` records the re-measurement that established that, along with the
    stale witness an earlier version of both docstrings cited. The reasoning is kept: a value
    that VALIDATES and is echoed back meets the same ``allow_nan=False`` encode as a rejected
    one. The fast path is starlette's own; only a body that would otherwise have raised takes
    the second pass.
    """

    def render(self, content: Any) -> bytes:
        try:
            return super().render(content)
        except ValueError:
            readable = json.loads(
                json.dumps(content, allow_nan=True), parse_constant=lambda token: token
            )
            return super().render(readable)


class RenderableValidationErrorRoute(APIRoute):
    """An :class:`APIRoute` whose 422 is always serialisable (T-270).

    Only :class:`RequestValidationError` is intercepted; an unhandled bug still becomes a 500,
    which is what keeps this from being the blanket refusal the ticket's gate rejects.
    """

    def get_route_handler(self) -> Any:
        handler = super().get_route_handler()

        async def render_validation_errors_safely(request: Request) -> Any:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return RenderableJSONResponse(
                    status_code=422,
                    content={"detail": renderable_validation_detail(exc.errors())},
                )

        return render_validation_errors_safely


router = APIRouter(
    tags=["accept"],
    route_class=RenderableValidationErrorRoute,
    default_response_class=RenderableJSONResponse,
)

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
#:
#: **THAT PER-RECORD FIGURE WAS TAKEN WHEN A RECORD CARRIED ``offer: {}``, and T-349 changed
#: what a record holds.** A record now carries the store's offer, so a count cap stopped
#: being a memory cap on its own: driven at 500 duplicate roster rows naming one store whose
#: reply carried a 214 KB padding field, this book retained **827.4 MiB** against that same
#: 256 MiB container. The size half of the bound therefore lives with the record now —
#: ``auction/routes.py``'s ``RECORDED_OFFER_FIELDS`` whitelist and
#: ``MAX_RECORDED_OFFER_VALUE_CHARS`` — and the same shape re-measures at **1.5 MiB**, back
#: within noise of the 1.1 MiB the empty-offer book held. Anyone changing what a record
#: carries has to re-measure both halves, which is why this paragraph names the probe.
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
    """409 — the accept was refused, and ``denial_reason`` names the condition.

    Two fields say the same thing at two different widths, and both are published because
    they have different readers (T-204).

    ``denial_code`` is the BARE declared code and nothing else — one of the nine members of
    :data:`~.reasons.DENIAL_REASONS`, which is the same closed vocabulary as
    ``protocol.schema.json#/$defs/DenialCode``. It is what a client branches on. Until it
    existed, the only machine-readable form of a refusal was *the token before the first
    colon of a prose string*, which every consumer had to re-derive with a parser that
    disagrees with the exchange's on U+001C–U+001F (Python's ``strip`` removes them,
    JavaScript's ``trim`` keeps them) — a published contract whose correct parsing was a
    footnote in a description field.

    ``denial_reason`` keeps its exact existing value, ``"<code>: <prose>"``, and is NOT
    narrowed to the bare code. It is the diagnosis: the host that failed the domain check,
    the exception the merchant's minting raised, the auction state that made the transition
    illegal. Tests and operators read it for exactly those words, so replacing it with the
    code would have thrown the diagnosis away to publish something already published beside
    it.
    """

    model_config = ConfigDict(extra="forbid")

    accepted: bool = False
    #: The composite ``"<code>: <prose>"``. Unchanged, and still the human-readable one.
    denial_reason: str
    #: The bare declared code — always a member of :data:`~.reasons.DENIAL_REASONS`.
    #:
    #: Typed ``str`` rather than an enum ON PURPOSE: this model is constructed on the
    #: refusal path, and a validation error there would turn a 409 into a 500 — an
    #: eligibility source's unexpected prose must never be able to take the door down. The
    #: vocabulary is guaranteed instead by :func:`_denied`, which republishes anything it
    #: does not recognise as ``unspecified``, and it is PUBLISHED as an ``enum`` on the 409
    #: in ``packages/contracts/openapi/exchange.openapi.json``.
    denial_code: str = DENIAL_UNSPECIFIED


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


def _accepted_store(result: Any) -> str | None:
    """The store the checkout port itself named on its ``accepted`` event.

    Read back off ``result.events`` for the same reason :func:`_accepted_offer` is: a
    second reading of the bid could disagree with the one the checkout was actually made
    against, and the two bridge events the port stamps are scoped to THIS value. The
    envelope and its bridges must land in one scope or the offer cannot be joined to the
    order at all -- ``reconcile`` namespaces every join key by store, because a Shopify
    ``order_id`` is only unique within one shop.

    ``None`` when the port emitted no ``accepted`` event, which is not something to invent
    a substitute for -- an invented store is worse than an unattributed one, because it
    files a real promise under a shop that did not make it.
    """
    for event in getattr(result, "events", ()) or ():
        if not isinstance(event, Mapping) or str(event.get("kind", "")) != ACCEPTED:
            continue
        store_id = event.get("store_id")
        return str(store_id) if store_id is not None else None
    return None


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


#: The frozen ``LedgerEvent`` kind (D24) that :meth:`AuctionStateMachine.accept` records for
#: the acceptance itself. Written out rather than reused from the ``ACCEPTED`` *state* it
#: happens to be spelled like, and rather than imported from ``auction.state``'s private
#: ``_TRANSITION_KIND``: a ledger kind and a state name are two vocabularies, and the one this
#: filter must not let through is the ledger one.
_ACCEPTED_KIND = "accepted"

#: The C11 kinds a checkout files that the auction's own transition does NOT write, in the
#: order ``CheckoutProvider._events`` builds them. ``accepted`` is excluded because the
#: transition already records it — with the auction's ``intent_id`` and ``cluster_id`` on it,
#: which the port's copy has no way to know — so forwarding the port's would put TWO
#: ``accepted`` events in the ledger for one acceptance and an auditor counting acceptances
#: off the chain would read double. ``services/sim/src/runner.py`` measured exactly that
#: (12 and 12 in a default run) and refuses it for the same reason.
_BRIDGE_KINDS: tuple[str, ...] = tuple(
    kind for kind in CHECKOUT_EVENT_KINDS if kind != _ACCEPTED_KIND
)


def _record_checkout_bridge(machine: AuctionStateMachine, result: Any) -> None:
    """Forward the checkout's other two events to the ledger the transition already writes.

    ``CheckoutProvider.checkout`` builds all three of
    :data:`~..checkout.CHECKOUT_EVENT_KINDS` and hands them back on ``CheckoutResult.events``.
    Until this function existed this route read exactly one of them —
    :func:`_accepted_offer` reads the ``accepted`` one for its offer body — and dropped the
    other two, so a served accept put three events on the wire inside the process and one in
    the ledger. Measured end to end against a real trust service: a whole demo chain of five
    events with not one bridge among them.

    **Why the two it dropped are the two that matter.** The exchange mints its
    ``checkout_token`` with ``secrets.token_hex(16)`` *after* the merchant has been called and
    transmits it nowhere; the store mints its own when the cart is visited and that is the one
    that rides onto ``orders/paid``. So ``trust.reconcile.engine.reconcile`` cannot join the
    two halves of a purchase on the token, and bridges them on the single-use discount code
    instead — the one value that genuinely crossed the wire. It reads that code off exactly
    ``code_created`` and ``checkout_redirect`` (``reconcile.engine.CODE_BRIDGE_KINDS``), which
    are exactly these two. Without them an ``accepted`` offer and its ``order_paid`` webhook
    reconcile to nothing at all, which is why reconciliation over a served run produced zero
    verdicts.

    **Forwarded, never rebuilt.** The events are the port's own, ``event_id`` and ``ts``
    included, passed through :meth:`~..auction.ledger.LedgerRecorder.record` — the same
    recorder, the same sink and the same publish path
    :meth:`~..auction.state.AuctionStateMachine._transition` uses. No second client is built
    and no shape is invented here: the bodies were already validated against what
    ``contracts`` publishes, by ``build_published_event`` at the producing boundary. Keeping
    the port's ``event_id`` is what makes a retried delivery land once — ``trust.events``
    admits an ``event_id`` exactly once — rather than twice under two ids.

    **It cannot fail the accept.** ``LedgerRecorder.record`` swallows a sink failure into
    ``failures`` for the reason its own header gives: losing an audit record is bad and
    failing a live auction because the audit sink hiccuped is worse. An event the port did not
    build (T-283 drops a malformed body rather than raising) is simply not here to forward.
    """
    for event in getattr(result, "events", ()) or ():
        if not isinstance(event, Mapping):
            continue
        kind = str(event.get("kind", ""))
        if kind not in _BRIDGE_KINDS:
            continue
        machine.ledger.record(
            kind,
            event_id=event.get("event_id"),
            ts=event.get("ts"),
            auction_id=event.get("auction_id"),
            store_id=event.get("store_id"),
            order_ref=event.get("order_ref"),
            payload=event.get("payload"),
        )


# =====================================================================================
# The learning fold — the only production producer of a bandit outcome in this service
# =====================================================================================
def _shown_stores(
    request: Request,
    *,
    auction_id: str,
    bids: Sequence[Mapping[str, Any]],
) -> list[str] | None:
    """Every store this auction actually showed, de-duplicated, in slot order.

    **THE SHORTLIST IS SHOWN; THE BID BOOK IS NOT.** This function used to read the bid book
    alone, and that is a different set. The book is filled by
    :func:`~..auction.routes.collected_bid_records` with every candidate the ranking found
    ELIGIBLE, while what the buyer was actually put in front of is the shortlist —
    :data:`~..ranking.shortlist.MAX_SLOTS` slots, one per store, and everything past the cut
    is benched by :func:`~..ranking.shortlist.bench` and never rendered. Measured over the
    real ``POST /auctions`` then ``POST /auctions/{id}/accept`` with six eligible stores: four
    slots, six records in the book, and the two benched stores each took a loss for an offer
    no shopper ever saw. That is not a rounding error in the learning signal, it is a ratchet:
    a benched store accrues beta forever and never alpha, its posterior falls, R12's
    exploration slice reads that same posterior and benches it again.

    So the shortlist is the source of truth, read through the accessor this codebase already
    reads it through — :func:`~..ranking.serving.shortlist_store`, the same object
    ``GET /auctions/{auction_id}/shortlist`` is served from — rather than through a second
    reader invented here.

    **Which field names the store, and why that one.** The slot itself is joined back to the
    BID RECORD on ``bid_ref`` (the minted ``bid_id``), and the store is read off the record's
    own top-level ``store_id``. That field is in :data:`~..ranking.candidates.CANDIDATE_FIELDS`
    and the exchange writes it from the roster entry rather than from the store's reply, so it
    is the platform's attribution and the same vocabulary the WIN is filed under — the win and
    the losses of one accept cannot end up naming a store two different ways. The join is the
    one :func:`~..ranking.serving.record_shown` already does for the ``shown`` ledger events,
    and for the same stated reason.

    Two other spellings were available and both are declined. A ``bid_id`` is minted as
    ``f"{auction_id}:{store_id}"``, so a name could be *parsed* back out of one: declined,
    because the mint is one producer's private spelling, a store id may itself contain a
    colon, and a name recovered by splitting a string would attribute a loss to whatever the
    split produced. A slot's ``trust_summary`` does happen to carry a ``store_id`` key
    (``ranking.shortlist.trust_summary`` writes it first): declined, because the contract
    types ``trust_summary`` as a free-form ``dict[str, Any]`` and nothing pins that key, while
    the bid record's ``store_id`` is a named field of a stated projection.

    Returns:
        The stores shown, or ``None`` when this auction has no readable shortlist at all —
        evicted past :class:`~..ranking.serving.ShortlistStore`'s cap, past its TTL, or never
        stored because the auction was assembled without ``POST /auctions``. ``None`` is not
        "nobody was shown": the caller must fold NOTHING for it, win included.

        An empty LIST is a different answer and is kept distinct rather than collapsed into
        ``None`` — it means a shortlist WAS read and named no store the bid book can resolve.
        :func:`_record_auction_outcomes` folds nothing for that one either, for the same
        anti-ratchet reason, but the two are logged as the different failures they are: one
        is a missing shortlist, the other is a shortlist that does not join to the book.

    A slot whose ``bid_ref`` matches no record contributes nothing rather than a guess, which
    costs one loss and never files a wrong one. Two slots naming one store count once.
    """
    from ..ranking.serving import shortlist_store  # noqa: PLC0415 - sibling feature

    shortlist = shortlist_store(request.app).get(auction_id)
    if not isinstance(shortlist, Mapping):
        return None

    # First wins on a duplicate id, the same tie-break `_best_bid_per_store` and
    # `record_shown` take: the slot was built from the first row carrying that `bid_id`.
    store_by_bid: dict[str, str] = {}
    for bid in bids:
        if not isinstance(bid, Mapping):
            continue
        # `bid_id` or `bid_ref`, because `accept.offer._bid_ref_of` reads a record's reference
        # under exactly those two names and this join must resolve every record that door can.
        ref = str(bid.get("bid_id") or bid.get("bid_ref") or "").strip()
        raw = bid.get("store_id")
        if not ref or raw is None or ref in store_by_bid:
            continue
        store = str(raw).strip()
        if store:
            store_by_bid[ref] = store

    shown: list[str] = []
    for slot in shortlist.get("slots", ()) or ():
        if not isinstance(slot, Mapping):
            continue
        store = store_by_bid.get(str(slot.get("bid_ref") or "").strip(), "")
        if store and store not in shown:
            shown.append(store)
    return shown


def _record_auction_outcomes(
    request: Request,
    *,
    auction_id: str,
    cluster_id: str,
    accepted_store: str | None,
    bids: Sequence[Mapping[str, Any]],
) -> None:
    """Fold this accept into the bandit: one win for the winner, one loss for each rival.

    **Why this route is the producer.** ``POST /internal/outcomes`` — the door
    :mod:`..policy.routes` publishes — is served, tested and has no production caller anywhere
    in this repository. Nothing else in this service knows all three things an outcome needs at
    the moment they are all true, and this handler knows them at exactly this point: the
    auction's ``cluster_id`` off the record it already loaded, the winning store off the
    checkout port's own ``accepted`` event, and the stores that were shown off the SHORTLIST
    this auction stored. Until this fold existed :class:`~..policy.routes.InMemoryBanditPosteriors`
    was never written to in a real deployment, so every posterior sat on its seeded prior for
    the life of the process and exposure never moved with results.

    **Both directions, because one direction is not a signal.** A buyer choosing one of four
    shortlisted offers is one conversion for the store chosen and one non-conversion for each
    of the other three: they were shown, in the same auction, to the same shopper, and were not
    taken. Recording only the win would raise every posterior that ever appeared and the
    Thompson sampler would have nothing to discriminate on — an alpha that only ever rises is
    not evidence about a store, it is a count of how often the store was on a shortlist.

    **SHOWN, not merely eligible.** The losses go to the stores :func:`_shown_stores` reads off
    this auction's stored SHORTLIST, never to the whole bid book: a store the ranking benched
    past ``MAX_SLOTS`` was not passed over by anybody, and charging it a loss is the ratchet
    that function's docstring measures.

    **No readable shortlist, no outcome — and that includes the win.** A shortlist evicted,
    expired or never stored means this process cannot say what was put in front of the buyer,
    so it records NOTHING and names the auction in the log. Folding only the win there would be
    the same monotonic ratchet pointed the other way: one store's alpha rising every accept
    while no rival's beta ever moves is not a comparison, and the sampler cannot un-learn it.

    **The winner is credited whether or not a slot names it.** The win is recorded from
    ``accepted_store`` directly and never from the shown list, because the buyer accepted this
    store — which is proof it was shown, whatever a stored shortlist has to say. The shown list
    only ever decides who takes a LOSS, and the winner is skipped there so one accept can never
    be both a win and a loss for the same store.

    **No cluster, no outcome.** ``exposure`` is decided WITHIN a cluster, so an outcome that
    names none cannot be routed to a posterior; pooling it into an invented shared bucket would
    move clusters it never happened in. :func:`~..policy.routes._record_outcome` refuses exactly
    this with a 400 and the ruling is followed rather than re-litigated here — except that an
    accept is not refusable over it, so the outcome is dropped and the auction is named in the
    log instead.

    **No named winner, no outcome either.** :func:`_accepted_store` returns ``None`` when the
    checkout port emitted no ``accepted`` event. Every shown store would then be a rival of
    nobody, so recording the losses would penalise the store that actually won. The whole fold
    is dropped and the auction is named, for the same reason :func:`_accepted_store` refuses to
    invent a substitute: an unattributed outcome is bad, a misattributed one is worse.
    """
    from ..policy.routes import record_conversion  # noqa: PLC0415 - sibling feature

    if not cluster_id:
        _log.warning(
            "exchange.accept: auction=%s recorded no bandit outcome — the auction names no "
            "cluster, and exposure is decided within a cluster",
            auction_id,
        )
        return
    winner = str(accepted_store or "").strip()
    if not winner:
        _log.warning(
            "exchange.accept: auction=%s cluster=%s recorded no bandit outcome — the checkout "
            "named no accepted store, so the losses have no winner to be losses to",
            auction_id,
            cluster_id,
        )
        return

    # READ BEFORE ANYTHING IS RECORDED, deliberately. `None` is "this process cannot say what
    # the buyer was shown", and the honest fold for that is the empty one — a win with no
    # losses beside it moves one posterior up against rivals that were never marked down.
    shown = _shown_stores(request, auction_id=auction_id, bids=bids)
    if shown is None:
        _log.warning(
            "exchange.accept: auction=%s cluster=%s recorded no bandit outcome — no shortlist "
            "is readable for it (evicted, expired, or never stored), so the stores that were "
            "shown and passed over cannot be named and the win is dropped with them",
            auction_id,
            cluster_id,
        )
        return
    if not shown:
        # A DIFFERENT failure from `None`, and it gets the same answer for the same reason. The
        # shortlist was read and still names no store this process can resolve — no slots at
        # all, or every slot's `bid_ref` matching no record in the book, which is what a bid
        # book and a shortlist store re-wired to two different `bid_id` spellings look like.
        # A lone win is the tempting fold here and it is the ratchet again: the winner's alpha
        # would rise on every accept while no rival's beta ever moved. Loud and empty beats
        # quiet and skewed.
        _log.warning(
            "exchange.accept: auction=%s cluster=%s recorded no bandit outcome — its shortlist "
            "was read but resolves to no store against the %d bid record(s) held for it, so not "
            "one store this auction showed can be named and the win is dropped with the losses",
            auction_id,
            cluster_id,
            len(bids),
        )
        return

    # The win comes from `accepted_store`, not from `shown`: the buyer accepted this store, so
    # it was shown, and a slot list that omits it does not get to withhold its credit. `shown`
    # being non-empty is what separates this from the guard above: at least one store the
    # shopper saw is nameable, so the accept is describable as a comparison.
    wins = int(record_conversion(request, store_id=winner, cluster_id=cluster_id, converted=True))
    losses = 0
    for store in shown:
        if store == winner:
            continue
        losses += int(
            record_conversion(request, store_id=store, cluster_id=cluster_id, converted=False)
        )

    # One line per accept, at INFO, because this loop was silently dead: the door existed, the
    # sampler existed, and nothing joined them. "Alive" has to be readable in the log of a
    # running deployment, not inferable from a test.
    _log.info(
        "exchange.accept: auction=%s cluster=%s folded %d win and %d loss outcomes into the "
        "bandit posteriors (winner=%s)",
        auction_id,
        cluster_id,
        wins,
        losses,
        winner,
    )


def _denied(reason: str) -> JSONResponse:
    """The 409 body the contract publishes: ``{accepted, denial_reason, denial_code}``.

    This is where the declared vocabulary becomes a property of the **published surface**
    rather than only of ``accept()`` (T-204). A reason arriving here with a code nothing
    declares — a future refusal added elsewhere, an eligibility source's own prose that got
    through — is re-published under ``unspecified`` with its words kept intact, so a client
    parsing ``denial_reason`` never has to handle a token outside
    :data:`~.reasons.DENIAL_REASONS`, and no diagnosis is thrown away to achieve that.

    **``denial_code`` is that same token, published as its own field** — the closure T-204
    asked for. The code was always computed here (it is how the ``unspecified`` republish
    decides) and was then discarded back into the composite string, so every client had to
    re-derive it from prose with a parser of its own. Splitting it out costs nothing and is
    the difference between a machine-readable refusal and a documented convention.

    ``denial_reason`` is deliberately unchanged rather than narrowed to the bare code: the
    prose after the colon names the host, the exception or the auction state that caused
    this refusal, and callers — including ``apps/exchange/tests/test_accept_routes.py``,
    which asserts a rival domain appears in it — legitimately read it for those words.
    """
    # Redacted here as well as at the two places a reason is BUILT, because this function is
    # the published surface's last frame and it is reachable with a string neither of them
    # produced — `_denied` is called directly with `str(result.denial_reason or "")` and with
    # a locally formatted transition message. A 409 body is the one sink a client reads, so
    # the invariant is asserted where it is published, not only where it is composed.
    text = redact_addresses(reason).strip()
    code = denial_code(text)
    if code is None:
        text = denial_reason(
            DENIAL_UNSPECIFIED, text or "the accept was refused and named no reason"
        )
        code = DENIAL_UNSPECIFIED
    return JSONResponse(
        status_code=409,
        content=AcceptDeniedResponse(
            accepted=False, denial_reason=text, denial_code=code
        ).model_dump(),
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
    # Held in a local rather than read back out of `auction` below, so the learning fold and
    # the checkout resolve a `bid_ref` against the SAME list. The fold no longer treats this
    # book as the shown set — the shortlist decides that, see `_shown_stores` — but it is
    # still the `bid_id -> store_id` table the fold joins the shown slots through, and a
    # re-read between the two calls could resolve one slot against a record the checkout
    # never saw.
    bids = _bids_for(request, auction_id)
    auction = {
        "auction_id": record.auction_id,
        "bids": bids,
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

    # Read ONCE and reused by the stamp and by the learning fold below, so the store the
    # ledger names as the winner and the store the bandit credits are the same value by
    # construction rather than by two agreeing reads of `result.events`.
    accepted_store = _accepted_store(result)

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
            store_id=accepted_store,
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

    # AFTER the stamp, never before: `machine.accept` writes the `accepted` event, and the
    # C11 order (`accepted`, `code_created`, `checkout_redirect`) is the order a reader of the
    # chain sees. Recording the bridge first would put a code before the acceptance it belongs
    # to; recording it on the refusal path above would file a `checkout_redirect` for a buyer
    # who is being handed no permalink. What the refusal path leaves unrecorded is a real and
    # unchanged gap — a live code exists there and the ledger says nothing about it — and it
    # is the expiry race, not this ticket.
    _record_checkout_bridge(machine, result)

    # The bandit's only production producer, and it is HERE for two reasons that are both
    # about position rather than about learning.
    #
    # It is downstream of the stamp, so it cannot fire for an accept that did not happen: a
    # 404, a 409 from the legality guard, a denial from `accept()` and a lost expiry race all
    # return above this line. And it cannot fire TWICE for one auction — VERIFIED, not
    # assumed: `machine.accept` moves the record to `accepted`, and `TRANSITIONS[ACCEPTED]` is
    # `frozenset()` (`auction/state.py:159`), so the guard at the top of this handler refuses
    # every later accept on the same auction before reaching any of this. Concurrently, the
    # T-158 acceptance claim inside `accept()` lets exactly one caller past, so the loser is a
    # denial and returns above too.
    #
    # And it is wrapped, because a learning write may never cost a buyer their permalink. Same
    # posture `_record_checkout_bridge` and `LedgerRecorder.record` take on the two lines
    # above: the accept is finished, the code is minted and live, and the one thing that must
    # not happen now is a 500 that loses the URL the buyer paid attention for.
    try:
        _record_auction_outcomes(
            request,
            auction_id=auction_id,
            cluster_id=str(getattr(record, "cluster_id", "") or "").strip(),
            accepted_store=accepted_store,
            bids=bids,
        )
    except Exception as exc:  # noqa: BLE001 - never fail an accept for a posterior write
        _log.warning(
            "exchange.accept: auction=%s folded no bandit outcome (%s); the acceptance stands",
            auction_id,
            describe_exception(exc),
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
