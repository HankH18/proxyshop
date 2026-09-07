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

import json
import time
import uuid
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from .. import redact_addresses
from ..eligibility import StaticSellerEligibility
from ..orchestration import solicit_bids
from ..ranking.candidates import mint_bid_id
from ..ranking.serving import (
    catalog_of,
    claim_dimensions_of,
    rank_auction,
    record_shown,
    registered_domains_of,
    shortlist_store,
    trust_snapshot_of,
    weights_of,
)
from ..reports.log import record_losses
from ..reports.routes import loss_log_of
from ..retrieval.clusters import assign_cluster, configure_clusters, intent_clusters_of
from ..retrieval.criteria import MAX_CANDIDATE_LIMIT
from ..retrieval.fit import FitLogError, annotate_bid_payload
from .fanout import parallel_fan_out
from .state import AuctionStateMachine, UnknownAuction

__all__ = [
    "BOUNDED_INTENT_IDENTIFIERS",
    "DEFAULT_BID_TIMEOUT_SECONDS",
    "MAX_BID_TIMEOUT_SECONDS",
    "MAX_EXCLUSION_REASONS_PER_BID",
    "MAX_HARD_CONSTRAINT_BYTES",
    "MAX_HARD_CONSTRAINTS",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_RECORDED_OFFER_DEPTH",
    "MAX_RECORDED_OFFER_ITEMS",
    "MAX_RECORDED_OFFER_VALUE_CHARS",
    "MAX_ROSTER_ENTRIES",
    "RECORDED_OFFER_ACCEPTED_TYPES",
    "RECORDED_OFFER_ATOMIC_TYPES",
    "RECORDED_OFFER_CONTAINER_TYPES",
    "RECORDED_OFFER_FIELDS",
    "RECORDED_OFFER_TEXT_TYPES",
    "NullSolicitor",
    "RenderableJSONResponse",
    "RenderableValidationErrorRoute",
    "bid_window_seconds",
    "collected_bid_records",
    "configure_auctions",
    "merged_candidates",
    "renderable_validation_detail",
    "router",
]


# =====================================================================================
# Rendering a validation failure that quotes a number JSON cannot spell (T-270)
# =====================================================================================
#
# THE 500 IS IN THE ERROR RENDERER, NOT IN ANY FIELD. `json.loads` accepts `NaN`,
# `Infinity` and an overflowing exponent such as `1e400` (which becomes `inf`), pydantic
# then REJECTS the value, and FastAPI's stock `request_validation_exception_handler`
# builds `{"detail": jsonable_encoder(exc.errors())}` — where every row carries `input`,
# the caller's own value. Starlette's `JSONResponse.render` serialises that with
# `allow_nan=False` (starlette/responses.py:198), so the encode raises
# `ValueError: Out of range float values are not JSON compliant: nan` and an
# UNAUTHENTICATED POST gets a 500. Note the T-224 repair (`allow_inf_nan=False` on
# `RosterEntry.list_price`, below) does not close this and cannot: it makes the value a
# REJECTION, and rejection is exactly the path that 500s.
#
# WHY A ROUTE CLASS AND NOT AN EXCEPTION HANDLER. `main.py` is orchestrator-frozen
# (B6(iii)) and its line 52 is a module-level `app = create_app()` — the object
# `uvicorn exchange.main:app` serves — which runs BEFORE any feature module is imported
# and binds FastAPI's default handlers by `setdefault`. So an
# `add_exception_handler` installed while this module is being imported reaches every app
# built afterwards and NONE built before, i.e. it repairs a fresh `create_app()` while
# leaving the deployed object answering 500. A route class travels with the ROUTER, is
# applied by `include_router` at include time, and is therefore build-order independent.
#
# WHY NOT A BLANKET REFUSAL. Answering 400 from a catch-all handler or middleware also
# removes the 5xx, and destroys the 422 contract while doing it: the caller can no longer
# tell which field it got wrong, and a genuine bug becomes indistinguishable from a typo.
# The value is RENDERED (as its JSON spelling, quoted) rather than suppressed.


def renderable_validation_detail(errors: Any) -> list[Any]:
    """``errors`` with every non-finite float replaced by the string JSON would have spelt.

    ``NaN``/``inf``/``-inf`` become ``"NaN"``/``"Infinity"``/``"-Infinity"`` — the token a
    strict client cannot parse as a number, handed back as a string it can. The caller still
    learns which field it got wrong and what it sent there.

    **The walk is delegated to ``json`` rather than written here, and that is deliberate.**
    ``input`` is the caller's own body, so its nesting depth is caller-chosen; a hand-rolled
    recursive rewrite would fault on exactly the input this function exists to render, turning
    the 500 it removes into a different 500 (the same reasoning as
    :func:`_within_the_recorded_offer_budget`). ``json.dumps(..., allow_nan=True)`` emits the
    bare tokens and ``parse_constant`` intercepts each one on the way back in.

    Anything that still cannot be rendered — a recursion limit, a value ``jsonable_encoder``
    cannot reach — falls back to the field paths WITHOUT their values, so the 422 keeps a
    non-empty per-field ``detail`` list in every case rather than degrading to a 5xx.
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

    **UNGRADED DEFENCE IN DEPTH — no gate in this repository is red without it, and an earlier
    version of this docstring claimed otherwise.** What it claimed was: "Measured: ``1e999`` at
    ``intent.hard_constraints`` answered 500 on both apps with the 422 path already repaired."
    That measurement was true when it was taken and is now stale, because :func:`~..retrieval.
    clusters._constraints_of` — added in the same commit, one layer earlier — drops the value
    before any response is built. Re-measured after an adversarial review: with this class
    removed from both routers the T-270 gate is green 15 runs of 15, and that same ``1e999``
    body answers 201. Keeping a stale measurement in a docstring is how a class comes to look
    load-bearing when nothing depends on it, so the correction is recorded here rather than
    quietly dropped.

    It is kept because the reasoning behind it survives the correction even though the witness
    did not: ``intent`` is annotated ``dict[str, Any]``, so a non-finite value nested inside it
    is ACCEPTED — correctly; that is what the annotation says — and any future path that echoes
    such a value into a response meets the same ``allow_nan=False`` encode. The gate does not
    ask for those positions to be refused (it says so in as many words); it asks that the
    answer be readable.

    The fast path is starlette's own encode, untouched. Only a body that would otherwise have
    raised takes the second pass, where ``allow_nan=True`` emits the bare tokens and
    ``parse_constant`` turns each into the string a strict client can read.
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

    Only :class:`RequestValidationError` is intercepted. An unhandled bug still becomes a 500,
    which is the correct answer for one and is what keeps this from being the blanket refusal
    the ticket's gate rejects.
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
    tags=["auctions"],
    route_class=RenderableValidationErrorRoute,
    default_response_class=RenderableJSONResponse,
)

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


#: The most bytes one intent's ``hard_constraints`` may occupy, and the most characters an
#: identifier on a roster row may run to.
#:
#: **Counting the constraints was not enough, and this is the second half of the same bound.**
#: Every exclusion reason INTERPOLATES the caller's own strings — the constraint's ``field``
#: and the row's ``store_id`` — so the cost is candidates x constraints x *the length of what
#: the caller wrote*. Capping only the first two factors moved the hole rather than closing it.
#: Measured against a request that satisfies every count cap (500 rostered stores, 64
#: constraints), varying only the length of ``field``::
#:
#:       1 KB field ( 32 KiB request) -> 201,   3.8 MiB body,   154 MiB peak RSS
#:       4 KB field (293 KiB request) -> 201,  12.4 MiB body,   434 MiB peak RSS
#:      20 KB field (1.3 MiB request) -> 201,  58.2 MiB body, 1,746 MiB peak RSS
#:     200 KB field (12 MiB request)  -> 201, 573.0 MiB body, 3,344 MiB peak RSS
#:
#: — the same one-request OOM against ``mem_limit: 256m``, reached through length instead of
#: through count. Found by an adversarial re-run of the count fix, which is the only reason it
#: is closed here rather than in production.
#:
#: The budget is on the constraints TOGETHER rather than on each one, because 64 constraints of
#: 4 KB each is the same amount of echoed text as one of 256 KB and there is no reason to allow
#: either. 16 KiB is ~256 bytes per constraint at the count cap, which is a generous
#: ``{"field": ..., "op": "gte", "value": ...}``. An identifier is a name, not a document.
MAX_HARD_CONSTRAINT_BYTES = 16 * 1024
MAX_IDENTIFIER_LENGTH = 128

#: The identifiers the INTENT lets a caller choose, and which this door therefore bounds
#: (T-352). Named as a tuple rather than spelled twice in the guard for the reason
#: ``external_bids/routes.py``'s ``BOUNDED_IDENTIFIERS`` is — the set is the property, and a
#: set that lives in one place cannot half-drift.
#:
#: **These two are RETAINED, which is what separates them from the rest of the intent.**
#: ``CreateAuctionRequest.intent`` is ``dict[str, Any]``, so pydantic validates nothing inside
#: it and :func:`_refuse_an_oversized_intent` weighs only ``hard_constraints``;
#: :func:`create_auction` then writes these two into the :class:`~.state.AuctionRecord`, whose
#: in-memory default store is a plain dict its own comment calls "Unbounded" — no capacity, no
#: TTL, no eviction — and copies them a second time into the ``auction_opened`` and
#: ``auction_closed`` ledger payloads, whose key vocabulary
#: (``contracts.ledger``) is frozen at ``(intent_id, cluster_id, roster_size)``.
#: ``GET /auctions/{auction_id}`` then hands both back to an anonymous reader. Measured over
#: the served app with no credential of any kind, one rostered store, 64 requests::
#:
#:     honest ids                          -> 201 x64,  0.029 MiB retained
#:     30,000-char intent_id + cluster_id  -> 201 x64,  3.690 MiB retained
#:     GET /auctions/{id}                  -> 200, 30,003-char intent_id, 60,233-byte body
#:     intent_id as ["A"*128] * 20,000     -> 201 x1,   2,640,454 chars retained
#:
#: growing linearly with the request count and with no plateau, against
#: ``apps/exchange/compose.yaml``'s ``mem_limit: 256m``.
#:
#: **The last line is why the guard grades the TYPE as well as the length.** ``machine.create``
#: is handed ``str(intent.get("intent_id", ""))``, so a caller who sends a list instead of a
#: name has the route SPELL an identifier for it out of a structure — the same lever, reached
#: around a check that only looks at ``str`` values, and cheaper for the attacker because one
#: request did 2.5 MiB. Nothing on contract is lost by refusing it: the published ``Intent``
#: declares ``intent_id`` as a ``string`` and ``cluster_id`` as ``string``/``null``.
BOUNDED_INTENT_IDENTIFIERS: Final[tuple[str, ...]] = ("intent_id", "cluster_id")

#: The offer keys a recorded bid carries into the book, and the ONLY ones.
#:
#: **This whitelist is a memory bound, not tidiness, and it was measured.** T-349 put the
#: store's offer into the book where the book used to hold ``{}``; the offer is a document the
#: STORE wrote, capped only by ``composition.MAX_BID_RESPONSE_BYTES`` (256 KiB), and
#: ``InMemoryAuctionBids.record`` deep-copies each record while ``collect_bids`` builds one
#: entry per roster ROW — so 500 duplicate rows naming one store multiply one fat reply 500
#: times. Driven at exactly that shape with a 214 KB offer, all of it in one padding field::
#:
#:     record whitelisted   ->  book retained 827.4 MiB   (against a 256 MiB container)
#:     record projected     ->  book retained     0.6 MiB
#:
#: The set is every key the accept and checkout path actually READS — ``checkout_url``,
#: ``expires_at`` and ``quantity`` in ``checkout/codes.py``, ``variant_ref``/``variant_id`` and
#: the prices in ``checkout/provider.py``, ``discount`` in ``checkout/discounts.py`` — and
#: nothing else. A key the accept path does not read is a key the book has no reason to hold,
#: which is the same discipline ``ranking.candidates.CANDIDATE_FIELDS`` applies one layer up.
#: The set is the INTERSECTION of two lists, and the intersection is the point: every key the
#: accept and checkout path READS, and every key the published ``Offer`` schema DECLARES.
#: ``protocol.schema.json``'s ``Offer`` is ``additionalProperties: false`` over
#: ``bid_offer_id, checkout_url, commitments, currency, delivery_estimate_days, discount,
#: expires_at, product_ref, total_price, unit_price, variant_ref`` — and nothing on the auction
#: path validates a bid against it (``validate_bid`` has no call site in ``apps/exchange/src``),
#: so this whitelist is where that schema is actually enforced for the book.
#:
#: **``quantity`` and ``variant_id`` are read by the checkout path and are DELIBERATELY NOT
#: HERE, because the contract does not declare them.** While the book held ``offer: {}`` that
#: was moot — ``offer_quantity`` always returned 1 and ``default_permalink`` always built
#: ``/cart/<variant>:1``. Recording the real offer made it live, and an adversarial pass drove
#: it: a bid whose ``quantity`` was ``10**9`` was published to the buyer's agent with
#: ``total_price: 100.0`` and then sent the shopper to
#: ``…/cart/1:1000000000``, because nothing reconciles ``quantity`` against
#: ``unit_price``/``total_price`` (T-177's wall in ``collect.py`` reads neither). Honouring an
#: undeclared, store-written field that multiplies what the shopper buys is not a thing to do
#: on the strength of nobody having forbidden it. A direct caller of ``checkout()`` may still
#: pass one; the BOOK does not carry it.
RECORDED_OFFER_FIELDS: tuple[str, ...] = (
    "checkout_url",
    "currency",
    "discount",
    "expires_at",
    "product_ref",
    "total_price",
    "unit_price",
    "variant_ref",
)

#: The most one recorded offer VALUE may weigh, in characters — **summed over every string
#: anywhere inside it**, not just an outermost one.
#:
#: The whitelist above bounds the number of fields; this bounds their size, because a store
#: that cannot add a padding key can still put 256 KiB inside ``checkout_url``. A bid carrying
#: an over-long value is NOT recorded — not truncated, because a truncated checkout URL is a
#: wrong checkout URL and sending a shopper to one is worse than refusing the bid, and not
#: silently emptied, because an offer with its URL removed reads as a fallback and would take
#: the R10 handoff. An unrecorded bid is refused ``unknown_bid``, which is the same fail-closed
#: direction everything else on this path takes. 4096 is far above any real cart permalink.
#:
#: The budget is PER VALUE rather than per offer, and that is deliberate rather than lax:
#: ``deepcopy`` shares immutable strings, so the 500 records a duplicated roster produces hold
#: 500 references to ONE string object. Measured: all eight whitelisted fields at exactly 4096
#: chars, 500 duplicate rows — book retained 0.29 MiB. Characters are not what multiplies.
MAX_RECORDED_OFFER_VALUE_CHARS = 4096

#: The most CONTAINER SLOTS one recorded offer may occupy, counted across every kept value and
#: every level of nesting **together**.
#:
#: **This is the cap that was measured missing, and the measurement is the reason it is
#: recursive.** The bound above it used to read ``len(value) > 64`` against the OUTER container
#: only, so ``{"currency": [[0] * 80000]}`` presented an outer length of 1 and was recorded
#: whole. Containers are what multiplies, because ``InMemoryAuctionBids.record`` deep-copies
#: each record and a duplicated roster produces one record per ROW: unlike a string, a list is
#: rebuilt 500 times. Driven at the T-349 shape — 500 duplicate rows naming one store, one
#: hostile bid of 234.6 KiB, comfortably UNDER ``composition.MAX_BID_RESPONSE_BYTES``::
#:
#:     outer-length bound   ->  HTTP 201, 500 records, book retained 339.82 MiB, peak RSS 428 MiB
#:     no bound at all      ->  HTTP 201, 500 records, book retained 339.82 MiB
#:     this bound           ->  HTTP 201, 0 records,   book retained  0.00 MiB, peak RSS  64 MiB
#:
#: The middle line is why the cap is written this way rather than tightened: against a nested
#: payload the outer-length bound was not weak, it was *inert* — deleting it entirely changed
#: nothing. The same hole was reachable through ``currency``, through ``variant_ref``, and one
#: level further down through ``discount``'s ``{type, value}``, which the projection keeps.
#:
#: Budgeted TOGETHER, for the reason :data:`MAX_HARD_CONSTRAINT_BYTES` gives: 64 slots spread
#: over eight fields is the same retained memory as 64 in one, and there is no reason to allow
#: either. 64 is far above anything the published ``Offer`` declares — the only nested value in
#: :data:`RECORDED_OFFER_FIELDS` is ``discount``, which spends two.
MAX_RECORDED_OFFER_ITEMS = 64

#: How deep a kept value may nest before the bid is dropped.
#:
#: Not redundant with the slot budget, because depth costs stack rather than slots: a value
#: nested 600 deep occupies 600 slots — inside the budget — and still crashes the process, since
#: ``InMemoryAuctionBids.record``'s ``deepcopy`` recurses once per level. Measured in this
#: worktree: ``json.loads`` parses a 2000-deep list happily (the C scanner's limit is far above
#: the interpreter's), ``deepcopy`` raises ``RecursionError`` from about 600, and the whole
#: payload is ~1.2 KiB of JSON — three orders of magnitude cheaper than the memory attack above.
#: So the depth cap is refused HERE, before the value can reach the copy that would fault on it.
#: 4 is generous: every value the published ``Offer`` declares is a scalar except ``discount``,
#: which is one level deep.
MAX_RECORDED_OFFER_DEPTH = 4

#: The ONLY types a recorded offer may contain, split by how the walk below charges each —
#: see :func:`_within_the_recorded_offer_budget`. Anything that is not an instance of one of
#: them is REFUSED.
#:
#: **Named here, rather than spelled inline in the walk, because the walk being a WHITELIST is
#: the property and a property needs an address.** The first version of this bound was a
#: blacklist — three ``isinstance`` branches and an ``else: continue`` that waved through every
#: type it had not been told about — and an adversarial pass drove it: ``deque(range(200_000))``
#: is the same value as the ``list`` one branch up, ``deepcopy`` copies it just the same, and it
#: was RECORDED. 500 records retained **792.7 MiB** in 54.55s. ``array('q', ...)``, ``UserList``,
#: ``UserString``, ``memoryview`` and any ordinary object holding a list as an attribute all
#: rode through the same hole. A blacklist here has to enumerate every copyable type Python
#: has — including the ones a future release adds; a whitelist has to enumerate the ones a bid
#: legitimately contains, which is these three tuples plus ``Mapping``.
#:
#: **Nothing reachable over HTTP is lost by refusing the rest**, and that is measured rather
#: than assumed: the production solicitor parses bid replies with ``json.loads``
#: (``composition.HttpBidSolicitor.solicit``, ``composition.py:820``), whose output is
#: exactly ``str``/``dict``/``list``/``int``/``float``/``bool``/``None`` — every one of them on
#: this list. So the refusal branch protects the ``collect_bids`` SEAM, which is public and
#: which a future in-process solicitor could hand anything, and it fails closed in the same
#: direction as the rest of this path.
RECORDED_OFFER_TEXT_TYPES: Final = (str, bytes, bytearray)
#: Charged one slot per element, at every level.
RECORDED_OFFER_CONTAINER_TYPES: Final = (list, tuple, set, frozenset)
#: Charged nothing, because ``deepcopy`` returns these unchanged (``copy._deepcopy_atomic``):
#: 500 records hold 500 references to one object rather than 500 copies. Measured — a
#: 4300-digit int recorded into 500 records: book 0.29 MiB, the honest baseline. Their size is
#: bounded by the reply cap; multiplication, which is what this budget is about, does not happen.
RECORDED_OFFER_ATOMIC_TYPES: Final = (type(None), bool, int, float, complex)
#: The union the walk actually accepts, in one name so a gate can derive the hostile set from
#: it instead of restating it. ``Mapping`` is the abstract one on purpose — ``dict``,
#: ``OrderedDict``, ``Counter``, ``UserDict`` and any registered mapping are all charged by
#: slot — and it is the only entry here that is not a concrete class.
RECORDED_OFFER_ACCEPTED_TYPES: Final[tuple[type, ...]] = (
    RECORDED_OFFER_TEXT_TYPES
    + (Mapping,)
    + RECORDED_OFFER_CONTAINER_TYPES
    + RECORDED_OFFER_ATOMIC_TYPES
)


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

    #: Bounded in LENGTH as well as required, because every exclusion reason the ranking emits
    #: interpolates it once per unsatisfied constraint — see :data:`MAX_IDENTIFIER_LENGTH`. A
    #: store id is a name; a 20 KB one is a lever on the response size, not an identifier.
    store_id: str = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    tier: int = 1
    product_ref: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
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


#: The most hard constraints one intent may carry into a served auction.
#:
#: The published ``Intent`` schema puts no ``maxItems`` on ``hard_constraints``, so this number
#: is a judgement and is written down as one. It exists for the same reason
#: :data:`MAX_BID_TIMEOUT_SECONDS` does — the value arrives on an unauthenticated body and
#: decides how much work a worker does — and it became load-bearing when the ranker reached the
#: served path: the eligibility gate evaluates every constraint against every candidate and
#: emits one reason string per failure, so the cost of a request is O(roster x constraints)
#: rather than O(roster). Uncapped and measured, an 93 KiB request built 641,600 reason strings
#: and drove peak RSS to 831 MB against ``compose.yaml``'s ``mem_limit: 256m``.
#:
#: 64 is chosen as "more must-haves than any buyer states, far fewer than any attack needs".
#: Refused rather than truncated: silently ranking against fewer constraints than the buyer
#: sent would answer a different question from the one asked, and answering it with a 201 is
#: worse than refusing.
MAX_HARD_CONSTRAINTS = 64

#: The most rostered stores one auction may carry. Not a new opinion —
#: :data:`~exchange.retrieval.criteria.MAX_CANDIDATE_LIMIT` is the published ceiling on how
#: many candidates the exchange will consider for one intent, and a roster is that same set
#: arriving by a different door. Imported rather than restated so the two cannot drift.
#:
#: This one is not a T-310 regression: the roster has always been unbounded here, and each
#: entry already costs an eligibility read and a fan-out slot. It is capped in the same change
#: because the ranking multiplies it, and because a ceiling that exists in the retrieval path
#: and not on the request that feeds the auction is a ceiling with a door beside it.
MAX_ROSTER_ENTRIES = MAX_CANDIDATE_LIMIT


class CreateAuctionRequest(BaseModel):
    intent: dict[str, Any]
    profile: dict[str, Any] | None = None
    roster: list[RosterEntry] = Field(default_factory=list, max_length=MAX_ROSTER_ENTRIES)
    #: R10's hard timeout for this auction, in seconds.
    bid_timeout_seconds: float = DEFAULT_BID_TIMEOUT_SECONDS


class AuctionEntryOut(BaseModel):
    #: **No ``bid_ref`` here, and its absence is a reported gap rather than an oversight.**
    #:
    #: ``entries`` reports every rostered store; ``ranked``, ``excluded`` and
    #: ``shortlist.slots`` are the only places a bid's reference is published, and an auction
    #: whose candidates were all excluded has none of the three. So a buyer's agent reading
    #: ``entries`` alone has no reference to accept with — the "second face" of T-294 that its
    #: own docstring names. Adding the field here is a one-line change and is NOT made in this
    #: ticket because ``test_auction.py::test_post_auctions_runs_the_gate_the_fan_out_and_the_
    #: state_machine`` pins this model's exact key set, and editing a test that is not wrong to
    #: widen a response is a change that belongs to whoever owns that assertion.
    #:
    #: What IS closed is the half that made the missing field matter: the reference published
    #: in ``ranked``/``shortlist`` now resolves at the accept door, and so does the reference
    #: the store minted for its own bid — see :func:`collected_bid_records`.
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


class RelaxedConstraintOut(BaseModel):
    """One hard constraint this auction did NOT apply, and why it did not.

    Published because the alternative is the failure it exists to replace. A buyer who says
    "espresso" and is handed a shortlist that ignored it, silently, has been given the wrong
    answer more confidently than an empty shortlist gives them no answer. So the constraint
    comes back verbatim — the field, the op and the value the buyer stated — next to a reason
    naming the auction-wide fact that made it undecidable. It appears only for a constraint
    no candidate carried any verified reading for; see :func:`~exchange.ranking.rank`.
    """

    field: str
    op: str
    value: Any = None
    reason: str


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
    #: The hard constraints this auction set aside, each saying why. Empty on every ordinary
    #: auction. It is published HERE rather than on the shortlist because ``Shortlist`` is a
    #: pinned two-field contract (``extra="forbid"``), and a reason that reached the buyer
    #: only through a schema change would not have reached them at all.
    relaxed_constraints: list[RelaxedConstraintOut] = Field(default_factory=list)


def configure_auctions(
    app: FastAPI,
    *,
    machine: AuctionStateMachine | None = None,
    solicitor: Any | None = None,
    eligibility: Any | None = None,
    bids: Any | None = None,
    clusters: Any | None = None,
) -> None:
    """Wire an app's auction dependencies. Anything omitted keeps what is already there.

    ``bids`` is the same ``app.state.auction_bids`` book :func:`~..accept.routes.
    configure_accept` wires, named here as well because the auction is what WRITES it: this
    route records what it collected and the accept route reads it back. Omitting it is the
    normal case — :func:`_bid_book` installs an
    :class:`~..accept.routes.InMemoryAuctionBids` on first use — and it is named only so a
    deployment that wants the book to outlive one process can hand over its own.

    ``clusters`` is the named-cluster catalogue this exchange assigns intents against — see
    :mod:`~..retrieval.clusters`. Omitting it leaves
    :class:`~..retrieval.clusters.NoIntentClusters`, which assigns nothing and leaves every
    intent's ``cluster_id`` exactly as it arrived.
    """
    if machine is not None:
        app.state.auction_machine = machine
    if solicitor is not None:
        app.state.bid_solicitor = solicitor
    if eligibility is not None:
        app.state.seller_eligibility = eligibility
    if bids is not None:
        app.state.auction_bids = bids
    if clusters is not None:
        configure_clusters(app, clusters)


def _machine(request: Request) -> AuctionStateMachine:
    """This app's state machine, built on first use — with a ledger that leaves the process.

    ``AuctionStateMachine()`` with no sink is what shipped, and its ``LedgerRecorder`` installs
    an :class:`~..auction.ledger.InMemoryLedgerSink`: every transition a SERVED auction made
    went into a list discarded with the app, so nothing a request produced ever reached the
    chained ledger in ``apps/trust/src/events`` (T-150). The sink is chosen by the composition
    root instead — :func:`~..composition.default_ledger_sink` — which posts each event to
    trust's published ``POST /events`` while still keeping the in-process record every
    readback here depends on.

    It is a DEFAULT and not a configuration key, because ``create_app()`` with nothing set is
    both what the ticket's gate builds and what ``docker compose up`` starts. A deployment that
    has to name its trust service can still do so (``trust_url`` in the deployment document, or
    ``TRUST_URL``): ``_bind_the_deployment`` runs before this function in ``POST /auctions`` and
    in ``POST /auctions/{auction_id}/accept``, so on both write paths the document wins over
    this fallback rather than racing it.

    Stated exactly, because it is one door short of "always", and this is the ONE lazy default
    in this module of which that is true: every other one (``_solicitor``, ``_eligibility``,
    ``_bid_book``) is reached only from inside a handler the hook has already run in, while
    ``GET /auctions/{auction_id}`` below reaches this function with **no** composition hook in
    front of it. So a read arriving before this app's first write installs the fallback, and a
    document's ``trust_url`` is then never applied for the life of the process. The failure is
    bounded and in the safe direction — the seam degrades to
    :data:`~..composition.DEFAULT_TRUST_URL`, never to a sink that goes nowhere.

    The import is deferred for the reason ``_bind_the_deployment``'s is: ``composition``
    imports the route modules, so a module-scope import here would be a cycle.
    """
    machine = getattr(request.app.state, "auction_machine", None)
    if machine is None:
        from ..composition import default_ledger_sink  # noqa: PLC0415 — see the docstring

        machine = AuctionStateMachine(ledger=default_ledger_sink())
        request.app.state.auction_machine = machine
    return machine


def _solicitor(request: Request) -> Any:
    solicitor = getattr(request.app.state, "bid_solicitor", None)
    if solicitor is None:
        solicitor = NullSolicitor()
        request.app.state.bid_solicitor = solicitor
    return solicitor


def _bound_solicitor(
    request: Request,
    *,
    auction_id: str,
    intent: Any,
    profile: Any,
    respond_by: float,
) -> Any:
    """This app's solicitor, told which auction it is being asked about — when it can be.

    The solicitor port is ``solicit(store)``: one roster row, and nothing else. That is all an
    in-process double needs and it is **not** enough for the real outbound client, because the
    published ``BidRequest`` a store agent answers carries ``{auction_id, intent, profile,
    respond_by}`` — none of which reaches ``solicit``. Until this hook existed no HTTP
    solicitor could be written against the port at all, which is a large part of why every
    deployment ran on ``NullSolicitor`` and every store fell back to its list price.

    ``for_auction`` is therefore OPTIONAL and additive: a solicitor that exposes it is handed
    the context and returns a view bound to this auction; one that does not — ``NullSolicitor``,
    every test double in this repository, ``e2e``'s ``HostedAgentSolicitor`` — is used exactly
    as before. A hook that raised is treated as one that is not there: a client whose binding
    is broken must not take the auction down, it must fail to bid like any other store.
    """
    solicitor = _solicitor(request)
    bind = getattr(solicitor, "for_auction", None)
    if not callable(bind):
        return solicitor
    try:
        bound = bind(
            auction_id=auction_id,
            intent=intent,
            profile=profile,
            respond_by=respond_by,
        )
    except Exception:
        return solicitor
    return solicitor if bound is None else bound


def _eligibility(request: Request) -> Any:
    eligibility = getattr(request.app.state, "seller_eligibility", None)
    if eligibility is None:
        # Fail closed by default: no rows, and an unknown store answers UNAVAILABLE.
        eligibility = StaticSellerEligibility()
        request.app.state.seller_eligibility = eligibility
    return eligibility


def _bind_the_deployment(request: Request) -> None:
    """Run the composition root once for this app, before anything reads a collaborator.

    ``apps/exchange/src/main.py`` is orchestrator-frozen (B6(iii)), so a deployment cannot be
    composed inside ``create_app`` — which is exactly why nothing composed it. This is the
    start-up hook, taken at the top of the request instead, and it binds nothing that is
    already bound, so a test or a deployment calling ``configure_auctions`` itself still wins.

    A malformed deployment document is a **503**, not a 500 and not a silent fail-closed
    answer: an exchange told to read a registry it cannot read is misconfigured, and that is a
    different thing from an exchange nobody has configured. Same status and same reasoning as
    ``accept/routes.py``'s unregistered ``CHECKOUT_MODE``.
    """
    from ..composition import DeploymentConfigurationError, ensure_configured  # noqa: PLC0415

    try:
        ensure_configured(request.app)
    except DeploymentConfigurationError as exc:
        raise HTTPException(status_code=503, detail=redact_addresses(exc)) from exc


def _bid_book(request: Request) -> Any:
    """The book this app records an auction's collected bids in.

    Created on first use, exactly as :func:`_machine` creates the state machine, and for the
    same reason: the auction is the WRITER of this book, so the first ``POST /auctions`` is
    when it has to exist. The accept route's own accessor installs
    :class:`~..accept.routes.NoRecordedBids` when it finds nothing — the fail-closed default
    for a service whose auctions are somebody else's — and this is the door that makes that
    default unnecessary rather than the one that overrides it: an explicitly wired source is
    never replaced here.
    """
    from ..accept.routes import InMemoryAuctionBids  # noqa: PLC0415 — sibling feature

    book = getattr(request.app.state, "auction_bids", None)
    if book is None:
        book = InMemoryAuctionBids()
        request.app.state.auction_bids = book
    return book


def _within_the_recorded_offer_budget(value: Any, slots_left: int) -> int | None:
    """Slots remaining after charging ``value``, or ``None`` if it does not fit.

    Walks ``value`` to its leaves rather than measuring its outermost container, and charges
    three budgets while doing so: :data:`MAX_RECORDED_OFFER_VALUE_CHARS` over every string it
    contains, ``slots_left`` of the shared :data:`MAX_RECORDED_OFFER_ITEMS` over every
    container slot at every level, and :data:`MAX_RECORDED_OFFER_DEPTH` over its nesting.

    **Iterative on an explicit stack, not recursive, and that is a requirement rather than a
    style.** This function's whole job is to refuse a value that is too deep to copy safely; a
    recursive implementation would fault on exactly the input it exists to reject, turning a
    refusal into the unauthenticated 500 it was written to prevent. The depth cap is applied
    per item as it is pushed, so the walk never descends past it either.

    Termination does not depend on the value being acyclic: every container slot visited spends
    one from a finite budget, so a self-referential structure exhausts it and is refused rather
    than walked forever. (JSON cannot express one; a direct caller of ``collect_bids`` can.)
    """
    chars_left = MAX_RECORDED_OFFER_VALUE_CHARS
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_RECORDED_OFFER_DEPTH:
            return None
        if isinstance(item, RECORDED_OFFER_TEXT_TYPES):
            chars_left -= len(item)
            if chars_left < 0:
                return None
            continue
        if isinstance(item, Mapping):
            # A GENERATOR, never a list comprehension, and the difference is measurable rather
            # than stylistic: materialising the pairs walks the whole mapping BEFORE the budget
            # below can refuse it, which hands back an amplification on the axis this function
            # exists to close — the walk runs once per roster row, so a hostile store pays for
            # it 500 times. Measured, 500 calls against one 30,000-key mapping:
            #
            #     list comprehension  ->  0.8186s   (1637.2 us/call)
            #     generator           ->  0.0028s   (   5.6 us/call)
            #
            # Same verdict either way; only the work done to reach it differs.
            children: Any = (part for pair in item.items() for part in pair)
        elif isinstance(item, RECORDED_OFFER_CONTAINER_TYPES):
            children = item
        elif isinstance(item, RECORDED_OFFER_ATOMIC_TYPES):
            # Free, for the reason `RECORDED_OFFER_ATOMIC_TYPES` records.
            continue
        else:
            # **Anything else is REFUSED, and the branches above are a WHITELIST for that
            # reason** — see `RECORDED_OFFER_ACCEPTED_TYPES` for the measurement that made this
            # branch a `return None` rather than the `continue` it used to be, and for why
            # nothing reachable over HTTP is lost by it. This line is graded by
            # `test_recorded_offer_budget.py::test_a_value_whose_type_is_off_the_accept_list_
            # is_refused_rather_than_recorded`, which derives its hostile corpus from the
            # accept-list above rather than naming types, so widening the whitelist moves a
            # value out of that corpus instead of leaving a test grading a type now allowed.
            return None
        for child in children:
            slots_left -= 1
            if slots_left < 0:
                return None
            stack.append((child, depth + 1))
    return slots_left


def _recordable_offer(offer: Any) -> dict[str, Any] | None:
    """``offer`` projected onto :data:`RECORDED_OFFER_FIELDS`, or ``None`` to drop the bid.

    ``None`` means "do not record this bid at all" and is returned when the kept values do not
    fit the budgets :func:`_within_the_recorded_offer_budget` charges. See
    :data:`MAX_RECORDED_OFFER_VALUE_CHARS` for why dropping beats truncating and beats
    emptying, and :data:`MAX_RECORDED_OFFER_ITEMS` for why the check has to reach the leaves.

    ``discount`` is the one nested value in the set, so it is projected in turn rather than
    copied: it is read for a ``type`` and a ``value`` (``checkout/discounts.py``) and a store
    could otherwise park its padding one level down. That projection is a whitelist, not a
    bound — ``{"type": [[0] * 80000], "value": 0}`` survives it intact — so the budget below
    is what actually stops it.

    A bid that FITS is recorded whole, with the real published fields the accept and checkout
    path read (T-349). Nothing here empties an offer that was merely large.
    """
    if not isinstance(offer, Mapping):
        return {}
    kept: dict[str, Any] = {}
    slots_left = MAX_RECORDED_OFFER_ITEMS
    for field in RECORDED_OFFER_FIELDS:
        if field not in offer:
            continue
        value = offer[field]
        if field == "discount" and isinstance(value, Mapping):
            value = {key: value[key] for key in ("type", "value") if key in value}
        remaining = _within_the_recorded_offer_budget(value, slots_left)
        if remaining is None:
            return None
        slots_left = remaining
        kept[field] = value
    return kept


def merged_candidates(
    rows: Sequence[Mapping[str, Any]],
    projected: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The rank ROW and the candidate PROJECTION for one bid, in a single mapping (T-349).

    Neither half is a superset of the other, which is why this joins rather than swaps.
    ``rank()``'s row carries ``eligible`` — the ranking's own verdict, and the only thing
    that decides whether a bid is recorded at all — and carries neither ``offer`` nor
    ``store_domain``. ``ranking/candidates.py``'s projection carries both and no verdict.
    Passing the row alone wrote every record with ``offer: {}``; passing the projection
    alone would record nothing, because it has no ``eligible``.

    **The join lives here rather than inside :func:`collected_bid_records`, and that is a
    correction rather than a preference.** It was a third parameter on that function
    first, and the signature is not private: ``test_ranking_served.py`` substitutes a
    two-argument replacement for it to suppress the ``fallback`` stamp, so a third
    positional argument broke that substitution with ``TypeError: without_the_flag() takes
    2 positional arguments but 3 were given``. The test was right and the signature change
    was wrong: a seam something else wraps is part of the contract. Merging first keeps
    that seam exactly as it was.

    The ROW wins on any key both carry, so a verdict can never be overwritten by the
    projection; the projection only fills in what the row does not have. A row with no
    match is passed through unchanged, so an empty ``projected`` degrades to the old
    behaviour instead of dropping bids.
    """
    by_bid = {str(row.get("bid_id") or ""): row for row in projected if row.get("bid_id")}
    return [{**by_bid.get(str(row.get("bid_id") or ""), {}), **row} for row in rows]


def collected_bid_records(
    candidates: Sequence[Mapping[str, Any]],
    entries: Sequence[Any],
) -> list[dict[str, Any]]:
    """Every bid this auction collected, in the shape ``accept()`` reads.

    ``accept()`` looks a bid up by ``bid_id`` (or ``bid_ref``) and then reads ``store_id``,
    ``store_domain`` and ``offer`` off it. All four are assembled HERE rather than copied from
    the store's reply, for the reason ``ranking/candidates.py`` gives at length: two of them
    decide the accept, and a bid is a document the *store* wrote. ``store_domain`` in
    particular is the platform registry's answer, never ``bid["store_domain"]`` — a store that
    supplied both halves of the C10/D22 check would pass its own check (T-169).

    So the records are meant to come from the ranking's own projected candidates, which already
    carry the minted ``bid_id`` and the platform's domain, and are the same rows ``ranked``,
    ``excluded`` and ``shortlist.slots`` name.

    **MEASURED DEFECT, NOW REPAIRED (T-349); what follows is the record of it.** The caller
    used to pass ``ranking["candidates"]`` alone, and that is not the projection in
    ``ranking/candidates.py``: ``ranking/__init__.py`` sets ``"candidates": rows``, the rank-ROW
    projection, whose keys are ``bid_id``, ``components``, ``eligible``, ``exclusion_reasons``,
    ``features``, ``price``, ``provenance_labels``, ``rank_score``, ``store_id``, ``trust``,
    ``trust_summary`` and ``verified_hard_fit_count``. There is no ``offer`` and no
    ``store_domain`` on it, so ``candidate.get("offer") or {}`` below records the EMPTY DICT and
    the ``store_domain`` key is never written. Measured over the HTTP door, one hosted bid and
    one silent store, both shortlisted::

        [{"bid_id": "…:store-a",      "offer": {}, "store_id": "store-a"},
         {"bid_id": "…:store-silent", "offer": {}, "store_id": "store-silent"}]

    It is blind to which kind of bid it is: a hosted reply carrying
    ``checkout_url=https://store-a.example.com/cart/77:1?ref=hosted``, an ``expires_at``, and
    ``variant_ref: "77"``, ``quantity: 3`` loses all four identically. Four measured
    consequences, on every served bid rather than only on fallbacks:

    * ``code_expiry(now, {})`` takes the flat ``MAX_CODE_TTL_SECONDS`` ceiling — 172800 s — so a
      code outlives the offer it discounts, which is the one thing that function exists to
      prevent. An offer set to expire in 60 seconds still minted a 48-hour code.
    * the PRE-mint ``assert_on_domain`` at ``checkout/provider.py`` is guarded by ``if
      request.checkout_url:`` and ``request.checkout_url`` is ``""``, so it never runs. The only
      host check that fires is the post-mint one, after a live discount exists.
    * ``assert_offer_is_mintable`` inspects nothing: ``code_expiry(0.0, {})`` and
      ``offer_quantity({})`` both take their default branch.
    * ``default_permalink`` reads ``variant_ref``/``variant_id``/``quantity`` off the offer, so
      the buyer is sent to ``/cart/1:1`` rather than to the ``/cart/77:3`` the store bid.

    **The repair was not the one-line swap it looked like**, and the paragraph that said so is
    kept because it is what the repair had to satisfy: this function gates on
    ``candidate.get("eligible")``, a key the projection does not carry, so handing it the
    projected candidates ALONE records nothing and every accept becomes ``unknown_bid``. The
    route also had no handle on the projection — ``ranking.serving.rank_auction`` built it
    locally and returned only ``rank()``'s output.

    So both halves are joined BEFORE this function is called, by :func:`merged_candidates`,
    and this function's two-argument signature is unchanged — deliberately, because
    ``test_ranking_served.py`` substitutes its own two-argument replacement for it and a
    third parameter broke that substitution. ``rank_auction`` now returns the projection
    under ``"projected"`` — additive, no existing key changed, so ``_excluded_out``'s
    argument is untouched because that one does need the row. A caller that merges nothing
    in gets exactly the old behaviour rather than silently recording nothing.

    MEASURED over the real socket in ``test_composition_root.py``, by spying on this
    function's return with the repair reverted and restored — the test PASSES either way,
    which is what made the defect silent::

        reverted:  bid_id='auction-ef75…:s1'  offer_keys=[]  store_domain=None
        restored:  bid_id='auction-55be…:s1'  offer_keys=['checkout_url', 'commitments',
                   'currency', 'expires_at', 'product_ref', 'total_price', 'unit_price']
                   checkout_url='https://s1.example.com/cart/44352913:1'
                   expires_at='2999-01-01T00:00:00Z'  store_domain='s1.example.com'

    The defect predated this branch — ``git log -L`` dates the line to ``ec4f2b4``, an
    ancestor of ``main``.

    **Only the candidates the ranking found ELIGIBLE are recorded**, and the first draft of
    this function got that wrong in the expensive direction. It recorded every collected
    candidate and justified it by claiming "the accept path re-runs R12, the domain check and
    the offer's mintability itself". **That is false**, and an adversarial pass measured it:
    :func:`~..accept.gate.accept_offer` re-reads the injected ``SellerEligibility`` source and
    nothing else. Nothing on the accept path reads ``trust_snapshot`` — ``composition.py`` says
    in as many words that R12's eligibility and the trust snapshot are two independent reads —
    so a store the ranking refused was buyable. Driven over HTTP against a deployment whose
    trust snapshot carries ``"blacklisted": true`` for ``s1``::

        ranked  : [('s2', 'auction-2d54…:s2')]
        excluded: [{"bid_ref": "auction-2d54…:s1", "store_id": "s1", "exclusion_reasons":
                    ["blacklisted_store: 's1' is blacklisted and may not participate (R12)"]}]
        accept 'auction-2d54…:s1' -> 200 {"code": "PSX-FPWZHVZD", "permalink_url": ...}

    and the same 200 for an offer that had already expired and for one that failed a hard
    constraint. The excluded ``bid_ref`` is published in the 201 body, so the buyer's agent is
    handed exactly the reference it needs. The book therefore holds what the ranking admitted,
    and nothing else: **an auction can only be asked to accept a bid it was prepared to show.**

    The cost is named rather than hidden: a candidate the exchange collected and published in
    ``entries`` but the ranking excluded is refused ``unknown_bid`` — a true statement about
    the bid *book* and a vague one about the auction. Naming the exclusion at the accept door
    would be better and needs the reasons carried alongside; refusing it is what matters.

    A **second** record is written for a store that minted its own reference, and only when
    that reference is unambiguous. A buyer's agent is told a bid's ref by the store that made
    it (that is the ref ``BidEntry.bid['bid_id']`` carries out of ``collect_bids``), so an
    exchange that answers only to its own spelling refuses a ref it did in fact collect. The
    alias is dropped when it collides with anything else in this auction — a minted ref, or
    another store's alias — because ``_find_bid`` returns the FIRST match, so honouring a
    colliding ref would let one bidder decide which offer another store's reference accepts.
    """
    by_store = {str(getattr(entry, "store_id", "")): entry for entry in entries}

    records: list[dict[str, Any]] = []
    minted: set[str] = set()
    for candidate in candidates:
        # THE line this function turns on. `eligible` is the ranking's own verdict, the same
        # field `_excluded_out` reads to decide what to report as refused.
        if not candidate.get("eligible"):
            continue
        bid_id = str(candidate.get("bid_id") or "")
        if not bid_id:
            continue
        store_id = str(candidate.get("store_id") or "")
        offer = _recordable_offer(candidate.get("offer"))
        if offer is None:
            # Over-long value: the bid is dropped rather than recorded in a shape that is
            # either wrong (truncated URL) or misread (missing URL reads as a fallback).
            continue
        record: dict[str, Any] = {
            "bid_id": bid_id,
            "store_id": store_id,
            "offer": offer,
        }
        domain = candidate.get("store_domain")
        if domain:
            record["store_domain"] = str(domain)
        # Carried so the accept door can tell a price a STORE quoted from one the exchange
        # manufactured for it (R10). Read off the `BidEntry`, which is `collect_bids`' own
        # verdict, and never off `candidate` — the rank row does not carry it, and a bid is a
        # document the store wrote. See `accept.offer`'s fallback handoff for what it is for:
        # accepting one succeeds, mints no discount code, and sends the buyer to that store's
        # own checkout — so this flag is what tells the two apart at the accept door.
        if getattr(by_store.get(store_id), "fallback", False):
            record["fallback"] = True
        records.append(record)
        minted.add(bid_id)

    aliases: dict[str, dict[str, Any]] = {}
    collided: set[str] = set()
    for record in list(records):
        entry = by_store.get(record["store_id"])
        bid = getattr(entry, "bid", None)
        if not isinstance(bid, Mapping):
            continue
        claimed = str(bid.get("bid_id") or bid.get("bid_ref") or "").strip()
        # Bounded for the reason `MAX_IDENTIFIER_LENGTH` exists: this key is chosen by the
        # bidding store, it is kept for the auction's whole TTL, and a reference is a name.
        # Without the cap a store could park up to `MAX_BID_RESPONSE_BYTES` of its own text in
        # the exchange's memory per auction by spelling its bid id at length.
        if not claimed or len(claimed) > MAX_IDENTIFIER_LENGTH or claimed in minted:
            continue
        if claimed in aliases:
            collided.add(claimed)
            continue
        aliases[claimed] = {**record, "bid_id": claimed}
    for ref in collided:
        aliases.pop(ref, None)

    records.extend(aliases.values())
    return records


#: The frozen ledger kind one collected bid is receipted under.
BID_PLACED_KIND = "bid_placed"


def record_bid_receipts(
    recorder: Any,
    entries: Sequence[Any],
    *,
    auction_id: str,
) -> list[dict[str, Any]]:
    """Announce one ``bid_placed`` event per bid this auction collected.

    **The exchange kept no record that a bid was ever received.** D34 freezes ``bid_placed``
    with the payload ``(bid_ref, store_id, offer)``, and the only producer of it in the tree
    was :func:`~..retrieval.fit.record_fit_scores` — a retrieval-side helper whose own
    docstring says, in as many words, that "when the auction layer starts emitting its own bid
    receipts, this call site must become :func:`~..retrieval.fit.annotate_bid_payload` on that
    event instead". This is that emission, and the annotation is applied here, so fit reaches
    the ledger on the receipt the auction was going to write rather than on a second event.

    Emitted AFTER ``solicit_bids`` and BEFORE the close, which is the order the ledger should
    read in: ``auction_opened``, one receipt per collected bid, ``auction_closed``. One per
    :class:`~.collect.BidEntry` — so a store the exchange represented at its list price (R10)
    gets a receipt too, because a manufactured offer is still an offer the buyer can be shown,
    and an absence here would make a silent store indistinguishable from a store that was
    never solicited (T-086 reads exactly that absence).

    ``bid_ref`` is :func:`~..ranking.candidates.mint_bid_id`'s spelling, imported rather than
    restated: the receipt has to name the same reference ``ranked``, ``excluded``,
    ``shortlist.slots`` and the accept door use, and a receipt nobody can join to a bid is not
    a receipt.

    The OFFER is projected through :func:`_recordable_offer` — the same whitelist and the same
    three budgets the bid BOOK is held to, and for the same measured reason: the offer is a
    document the STORE wrote, bounded only by ``composition.MAX_BID_RESPONSE_BYTES`` (256 KiB),
    and one event per roster row means a fat reply is multiplied by the roster. A bid whose
    offer does not fit is receipted with an empty one rather than dropped — the exchange really
    did collect a bid and really did decline to hold its offer, and losing the receipt would
    lose the more important half of that sentence.

    The payload is **not** re-validated here and does not need to be: the three published keys
    are the three this function writes, unconditionally, and ``annotate_bid_payload`` validates
    the annotated form itself (that is what its ``FitLogError`` is). ``build_event`` still
    refuses a kind outside the frozen vocabulary.

    Returns:
        The events emitted, in entry order. Never raises: the audit trail must not be able to
        fail a live auction, so a fit annotation that cannot be built (an offer naming no
        ``product_ref``) falls back to the plain published payload.
    """
    record = getattr(recorder, "record", None)
    if not callable(record):
        return []
    events: list[dict[str, Any]] = []
    for entry in entries:
        store_id = str(getattr(entry, "store_id", "") or "")
        bid = getattr(entry, "bid", None)
        offer = _recordable_offer(bid.get("offer") if isinstance(bid, Mapping) else None)
        payload: dict[str, Any] = {
            "bid_ref": mint_bid_id(auction_id, store_id),
            "store_id": store_id,
            "offer": {} if offer is None else offer,
        }
        try:
            # No assessments: a served auction is handed a ROSTER and queries no index, so the
            # fit block records `fit_unavailable` rather than a number nobody measured. Same
            # reasoning as `ranking.features`' `intent_match`.
            payload = annotate_bid_payload(payload, (), auction_id=auction_id)
        except FitLogError:
            # The annotation needs `offer.product_ref` to join an assessment to. A roster row
            # that named no product, or an offer too large to hold, has none — the receipt is
            # still written, without the fit block.
            pass
        events.append(
            record(
                BID_PLACED_KIND,
                auction_id=auction_id,
                store_id=store_id,
                payload=payload,
            )
        )
    return events


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


#: How many exclusion reasons one candidate may report before the rest are summarised.
#:
#: This is a **memory bound on an unauthenticated response**, not a display preference, and
#: the number it replaces was measured rather than feared. ``exclusion_reasons`` carries ONE
#: string per unsatisfied hard constraint (``ranking/filters.py``), and both dimensions arrive
#: on the request body: ``roster`` has no length limit and ``intent`` is a free ``dict``, so an
#: uncapped report is O(roster x hard_constraints). Measured on this tree before the cap, with
#: a single request and no credential::
#:
#:     300 stores x 300 constraints ( 36 KiB request) -> 201,   20.4 MB response
#:     800 stores x 800 constraints ( 93 KiB request) -> 201,  142 MB body, peak RSS 831 MB
#:    1000 stores x 1000 constraints (120 KiB request) -> 201,  226 MB response
#:
#: against ``compose.yaml``'s ``mem_limit: 256m`` and a single uvicorn worker — a one-request
#: OOM kill. Capping the per-candidate list returns the response to O(roster), which is the
#: order ``entries`` already had and therefore the exposure the roster already carried.
#:
#: The overflow is SUMMARISED rather than silently dropped: a truncated list that did not say
#: it was truncated would be a store told it failed eight constraints when it failed ninety.
MAX_EXCLUSION_REASONS_PER_BID = 8


def _exclusion_reasons_out(row: Mapping[str, Any]) -> list[str]:
    reasons = [str(reason) for reason in row.get("exclusion_reasons") or ()]
    if len(reasons) <= MAX_EXCLUSION_REASONS_PER_BID:
        return reasons
    hidden = len(reasons) - MAX_EXCLUSION_REASONS_PER_BID
    return [
        *reasons[:MAX_EXCLUSION_REASONS_PER_BID],
        f"... and {hidden} further exclusion reason(s) not reported: this candidate failed "
        f"{len(reasons)} checks and the response reports the first "
        f"{MAX_EXCLUSION_REASONS_PER_BID}",
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
            exclusion_reasons=_exclusion_reasons_out(row),
        )
        for row in rows
        if not row.get("eligible")
    ]


def _relaxed_out(entries: Sequence[Mapping[str, Any]]) -> list[RelaxedConstraintOut]:
    """The constraints the ranking set aside, rendered for the response.

    Nothing is truncated the way ``_excluded_out``'s reasons are: the intent's own hard
    constraints are already bounded by :data:`MAX_HARD_CONSTRAINTS` at the door, so this list
    cannot be longer than the request the caller sent.
    """
    return [
        RelaxedConstraintOut(
            field=str(entry.get("field") or ""),
            op=str(entry.get("op") or ""),
            value=entry.get("value"),
            reason=str(entry.get("reason") or ""),
        )
        for entry in entries
    ]


def _refuse_an_oversized_intent(intent: Any) -> None:
    """422 an intent carrying more hard constraints than :data:`MAX_HARD_CONSTRAINTS`.

    Checked HERE rather than on ``CreateAuctionRequest``, because ``intent`` is a free
    ``dict[str, Any]`` on that model — the route accepts whatever shape a buyer service sends
    and lets ``exchange.ranking.filters.read_criteria`` decide what it means. A pydantic
    constraint would need the model to know the intent's shape, which is precisely what it
    declines to know.

    A non-list ``hard_constraints`` is not refused for its SHAPE here: ``read_criteria``
    already answers that with an undecidable-intent exclusion for every candidate, which is
    fail-closed and names the reason. This function is about size only.

    But it measures the size of anything that HAS one, not only of a ``Sequence``. The first
    version tested ``isinstance(constraints, Sequence)`` and returned early otherwise, which
    made the bound depend on an argument about the transport rather than on the value: a JSON
    body cannot carry a ``set`` or a generator, so over HTTP the two are equivalent — and a
    guard whose correctness rests on "the only caller is JSON" stops being correct the first
    time it has a second caller. ``Collection`` — sized AND iterable, which a list, a tuple, a set and a dict all
    are and a generator is not — costs one word and needs no such argument.
    """
    if not isinstance(intent, Mapping):
        return
    constraints = intent.get("hard_constraints")
    if isinstance(constraints, (str, bytes)) or not isinstance(constraints, Collection):
        return
    if len(constraints) > MAX_HARD_CONSTRAINTS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"intent.hard_constraints carries {len(constraints)} entries; this exchange "
                f"evaluates at most {MAX_HARD_CONSTRAINTS} per auction. Every constraint is "
                f"decided against every rostered candidate, so the request is refused rather "
                f"than answered against a subset of what was asked"
            ),
        )

    # The second factor, and it has to be measured rather than assumed from the count: an
    # exclusion reason quotes the constraint back, so 64 constraints of 4 KB cost as much as
    # 1,024 short ones. `default=str` so a value this exchange cannot serialise is still
    # WEIGHED rather than raising out of a size check — an unserialisable constraint is
    # `read_criteria`'s problem to name, not this function's to crash on.
    try:
        weight = len(json.dumps(list(constraints), default=str))
    except (TypeError, ValueError, RecursionError):
        # Unmeasurable is not small. A constraint list that cannot be sized is one this
        # function cannot promise anything about, so it is refused rather than admitted.
        weight = MAX_HARD_CONSTRAINT_BYTES + 1
    if weight > MAX_HARD_CONSTRAINT_BYTES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"intent.hard_constraints occupies {weight} bytes; this exchange accepts at "
                f"most {MAX_HARD_CONSTRAINT_BYTES}. Every constraint is quoted back once per "
                f"candidate it excludes, so the size of the answer is the size of the "
                f"question multiplied by the roster"
            ),
        )


def _refuse_an_oversized_identifier(intent: Any) -> None:
    """422 an intent whose caller-chosen identifiers are not names this door can keep (T-352).

    Three refusals, in this order and for the same reason — both fields are RETAINED for the
    auction's lifetime and served back to any reader: a value that is not a string (the route
    would spell one out of it with ``str()``), a string past the ceiling, and a string no UTF-8
    response can emit. Each has its own comment below with what it measured.

    The two fields are named by :data:`BOUNDED_INTENT_IDENTIFIERS`, which carries the
    measurement; the ceiling is :data:`MAX_IDENTIFIER_LENGTH`, which is the number this same
    request body already holds the same anonymous caller to on ``RosterEntry.store_id`` and
    ``RosterEntry.product_ref``, which ``collected_bid_records`` holds a bidding store's own
    reference to, and which ``external_bids/routes.py`` and ``policy/routes.py`` both IMPORT
    from here rather than restate. A door that admitted an ``intent_id`` longer than the
    ``store_id`` sitting beside it in the same body would be two ceilings on one request.

    **Checked HERE rather than on ``CreateAuctionRequest``**, for the reason
    :func:`_refuse_an_oversized_intent` gives: ``intent`` is a free ``dict[str, Any]`` on that
    model, and a pydantic constraint would need the model to know the intent's shape, which is
    precisely what it declines to know.

    **Checked BEFORE :func:`~..retrieval.clusters.assign_cluster`**, so what is bounded is the
    string the CALLER chose. A ``cluster_id`` the catalogue assigns replaces it and is the
    operator's own configured name, not an anonymous body's — bounding that one would turn a
    misconfiguration into a refused request, which is a different subject.

    **The refusal shape**: a 422 whose detail is built from a field name off the module-level
    tuple above, an integer length, and an integer ceiling — the oversized value is never
    interpolated, so the answer cannot grow with what it refused, and cannot carry the caller's
    bytes into a response encoder (the T-270 shape). The external bid door refuses the same
    ceiling as a 400 ``BidValidationResult`` because that is the contract IT publishes; this
    door's published answer for a body it will not accept is the 422 every other guard on it
    already gives. The bound and its properties agree; only the two doors' own vocabularies
    differ.
    """
    if not isinstance(intent, Mapping):
        return
    for field in BOUNDED_INTENT_IDENTIFIERS:
        value = intent.get(field)
        # Absent, or an explicit `null` the published `Intent` allows for `cluster_id`. Neither
        # is a string whose length the caller chose, and both keep whatever this route already
        # made of them.
        if value is None:
            continue
        if not isinstance(value, str):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"intent.{field} is a {type(value).__name__}; this exchange requires the "
                    f"string the published Intent declares. An identifier spelled out of a "
                    f"structure carries that structure's size into the auction record and "
                    f"into the ledger, which is the thing the length ceiling exists to stop"
                ),
            )
        if len(value) > MAX_IDENTIFIER_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"intent.{field} is {len(value)} characters; this exchange accepts at most "
                    f"{MAX_IDENTIFIER_LENGTH}. It is retained for the auction's whole lifetime, "
                    f"written into every ledger event this auction emits, and served to any "
                    f"reader of GET /auctions/{{auction_id}} — an identifier is a name, not a "
                    f"document"
                ),
            )
        # A SECOND defect on the same two fields, and the length check above does not touch
        # it: a LONE SURROGATE (`"\ud800"`, written as a plain `\uXXXX` escape, which
        # `json.loads` accepts into an ordinary `str`) is three characters long and survives
        # `AuctionRecord.to_json`, because `json.dumps` defaults to `ensure_ascii=True` and
        # stores the escape. Starlette renders with `ensure_ascii=False` and then
        # `.encode("utf-8")`, which raises `UnicodeEncodeError` on it. Measured over the served
        # app with no credential: `POST /auctions` with `intent_id: "x\ud800y"` answered 201
        # and `GET /auctions/{auction_id}` then answered **500**, for that record's whole
        # lifetime. The same SHAPE as T-270 and as the defect `external_bids/routes.py`'s
        # `_renderable` closes, reached through the field T-352 is about.
        #
        # REFUSED HERE rather than replaced on the way out, which is deliberately the opposite
        # of what that sibling does: the external door has to render a refusal, so it must
        # substitute; an identifier this exchange could never serve back is one it should not
        # have taken. The encode runs only after the length check, so it is bounded by
        # `MAX_IDENTIFIER_LENGTH` and cannot itself be an amplifier.
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"intent.{field} carries a character no UTF-8 response can spell. It is "
                    f"served back by GET /auctions/{{auction_id}}, so accepting it would open "
                    f"an auction whose every reader is answered 500"
                ),
            ) from exc


@router.post("/auctions", response_model=CreateAuctionResponse, status_code=201)
async def create_auction(body: CreateAuctionRequest, request: Request) -> CreateAuctionResponse:
    """Open an auction, gate the roster, fan out with a hard timeout, close, and answer."""
    _bind_the_deployment(request)
    machine = _machine(request)
    intent = body.intent
    _refuse_an_oversized_intent(intent)
    # Both retained identifiers are bounded BEFORE `assign_cluster`, so everything below this
    # line — the `machine.create` call, the two ledger payloads it writes, and what
    # `GET /auctions/{auction_id}` serves back — is holding a name whose size the caller could
    # not choose. See `BOUNDED_INTENT_IDENTIFIERS` for what each unbounded one cost (T-352).
    _refuse_an_oversized_identifier(intent)

    # DESIGN.md:34's first exchange job, and the one nothing performed: address this intent to
    # a NAMED catalogue cluster. The buyer's clarifier mints `cluster_id` by hashing the query
    # — it has no catalogue in scope and, by its own contract, no exchange handle — while a
    # merchant's envelope authorises bidding inside names. A hash is never a member of a set of
    # names, so before this line every solicited store answered `204 cluster_not_pursued` and
    # every shortlist was empty (measured live over loopback; see `retrieval.clusters`).
    #
    # It happens HERE, before `machine.create` and before the fan-out, so that all three
    # readers agree: the auction record, the `auction_opened` ledger payload, and the
    # `BidRequest` each store agent is handed. Assigning it later would put the exchange's own
    # audit trail in one namespace and the store's authorization check in another.
    #
    # An exchange with no cluster catalogue wired assigns nothing and this is a no-op.
    assignment = assign_cluster(intent, intent_clusters_of(request.app))
    if assignment.assigned:
        intent = assignment.applied_to(intent)

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
        solicitor=_bound_solicitor(
            request, auction_id=auction_id, intent=intent, profile=body.profile, respond_by=deadline
        ),
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

    # The receipts for what the fan-out actually collected, written BEFORE the close so the
    # ledger reads in the order the auction happened. See `record_bid_receipts` for why the
    # exchange writing its own is what lets `retrieval.fit`'s `record_fit_scores` stop being
    # the only producer of the kind in the tree.
    record_bid_receipts(machine.ledger, result.entries, auction_id=auction_id)

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
        catalog=catalog_of(request.app),
        # Which product each store is bidding on is the ROSTER's answer, never the reply's:
        # a store that named a different product on its bid would otherwise choose which of
        # its own catalogue entries its claims are graded against (ESC-020).
        product_refs={
            str(entry.get("store_id") or ""): entry.get("product_ref")
            for entry in roster
            if entry.get("product_ref") is not None
        },
        # The ranker's own audit trail. Every verdict it mints for this auction is announced
        # as `claim_verified` on the way through, instead of being consumed by the filters and
        # dropped when the request ends — see `ranking.verification.attest_candidate_claims`,
        # including for why an exchange with no `claim_dimensions` wired announces nothing.
        recorder=machine.ledger,
        claim_dimensions=claim_dimensions_of(request.app),
    )
    shortlist = ranking["shortlist"]
    shortlist_store(request.app).put(auction_id, shortlist, now=closed_at)
    # What this auction PUT IN FRONT OF THE BUYER, recorded where it is stored so the ledger
    # names the same object `GET /auctions/{auction_id}/shortlist` will serve.
    record_shown(
        machine.ledger,
        ranking.get("projected") or (),
        auction_id=auction_id,
        shortlist=shortlist,
    )

    # The bids this auction collected, kept where the accept route reads them. Without this
    # line `POST /auctions` renders `entries` and then DROPS the `BidEntry` list, so
    # `app.state.auction_bids` stayed on its `NoRecordedBids` default and every accept of a
    # bid the exchange had itself just published was refused `unknown_bid` (T-294). Recorded
    # AFTER the ranking because the ranking's own projected candidates are the shape the
    # accept path reads, minted ref and platform domain included — see
    # :func:`collected_bid_records` for why nothing here is copied from the store's reply.
    #
    # BOTH projections are handed over, and that is T-349's repair. `ranking["candidates"]` is
    # `rank()`'s ROW projection — it carries `eligible`, the verdict that decides whether a bid
    # is recorded at all, and carries neither `offer` nor `store_domain`. `ranking["projected"]`
    # is `ranking/candidates.py`'s projection and carries both. Passing the row alone wrote
    # every record with `offer: {}`, costing each served bid its expiry, its pre-mint host
    # check and its cart permalink; passing the projection alone would record NOTHING, because
    # the projection has no `eligible`. `collected_bid_records` joins them on the minted
    # `bid_id`; its docstring carries the before/after measurement.
    book = _bid_book(request)
    recorder = getattr(book, "record", None)
    if callable(recorder):
        recorder(
            auction_id,
            collected_bid_records(
                merged_candidates(ranking["candidates"], ranking.get("projected") or ()),
                result.entries,
            ),
        )

    # R9's win/loss log, written where the verdicts are: this is the only place holding the
    # ranker's rows, the shortlist that says who was SHOWN, and the intent whose cluster the
    # report aggregates by, all at once. Until this line `exchange.reports.build_loss_report`
    # had no producer anywhere in the repository — its only input was a test fixture — so a
    # merchant-facing route on top of it would have served an empty report forever.
    #
    # `ranking["candidates"]` is `rank()`'s ROW projection, which is the one carrying `eligible`,
    # `exclusion_reasons` and `components`; those three are what decide a loss's category, and
    # none of them is on `ranking["projected"]`. Deliberately NOT the merged rows the bid book
    # gets: the merge folds in the offer and the platform domain, which a loss row has no use
    # for and which would widen what the report's egress scan has to suppress for nothing.
    record_losses(
        loss_log_of(request.app),
        auction_id=auction_id,
        cluster_id=str(intent.get("cluster_id", "")),
        candidates=ranking["candidates"],
        shortlist=shortlist,
        intent=intent,
        now=closed_at,
    )

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
        relaxed_constraints=_relaxed_out(ranking.get("relaxed_constraints") or ()),
    )


@router.get("/auctions/{auction_id}")
async def read_auction(auction_id: str, request: Request) -> dict[str, Any]:
    """The auction's current state — 404 once its 15-minute TTL has taken it away."""
    try:
        record = _machine(request).get(auction_id)
    except UnknownAuction as exc:
        raise HTTPException(status_code=404, detail=redact_addresses(exc)) from exc
    return {
        "auction_id": record.auction_id,
        "state": record.state,
        "intent_id": record.intent_id,
        "cluster_id": record.cluster_id,
        "accepted_bid_ref": record.accepted_bid_ref,
        "history": record.history,
    }
