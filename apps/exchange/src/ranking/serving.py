"""The wiring that puts :func:`~exchange.ranking.rank` on a served request (T-310).

``apps/exchange/src/ranking`` was T-032's deliverable and, until this module existed, no
process imported it: ``exchange.main`` globs ``*/routes.py``, ``ranking/`` had none, and the
auction route's import block named eligibility, orchestration, fan-out and state and nothing
else. So a served auction answered with whatever order the fan-out happened to return, R19's
hard constraints never ran on a real request, and ``docs/demo/starting-slice.md`` §3.4's claim
that "``exchange.ranking.rank`` applies the hard-constraint filters, then the published weighted
formula, and builds the shortlist" described a code path no request took.

This module holds the three things a served ranking needs that the pure ranker deliberately
does not:

* **collaborators**, resolved from ``app.state`` the same lazy way the auction route already
  resolves its machine, solicitor and eligibility source — and with the same fail-closed
  defaults. An exchange nobody has wired holds no trust snapshot and no registered domains, so
  every candidate is excluded rather than every candidate admitted (R12/C10);
* **the weight set**, loaded from the environment through ``contracts.ranking`` — the published
  loader, not a second reading of the same variables;
* **somewhere to keep the answer**, because the published contract declares
  ``GET /auctions/{auction_id}/shortlist`` and the auction record (``auction/state.py``) holds a
  roster and a history but no bids.

Nothing here re-implements any ranking rule. Every filter, the formula, the tie-breaks and the
slot assignment stay in :mod:`exchange.ranking`; this module hands them their inputs.

It also finishes the SLOT, for the same structural reason. R2 wants one shortlist slot to show
PRODUCT, PRICE, COMMITMENTS, a store trust indicator and provenance labels; ``ranking/shortlist.py``
can only build the last two, because it is handed rank ROWS and a rank row carries no ``offer``.
:func:`rank_auction` holds the built shortlist and the projected candidates at the same time, so
the three offer-derived fields are read and merged here — see :func:`_with_offer_fields`, and see
its docstring for why a field the exchange cannot read is served as ``null`` rather than dropped.

**And "hands them their inputs" is now literal.** :func:`rank_auction` is the only place that
holds a served auction's bids, its roster and this exchange's catalogue at once, so it is the
only place that can run :mod:`.verification` and then :mod:`.features` over them in that order
— which is what makes ``verified_claim_ratio``, ``price_value`` and ``delivery_fit`` exist on a
served candidate at all. Without that pair of lines the formula still runs and still returns a
number, but four of its five terms are neutral and the number is ``0.4 + 0.2*trust``.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, NamedTuple

from contracts.protocol import Claim, Discount, Shortlist
from contracts.ranking import RankingWeights
from pydantic import ValidationError

from ..auction.state import AUCTION_TTL_SECONDS
from ..checkout.codes import UnusableOffer, expiry_epoch
from ..policy.exploration import exposure_shares, plan_exploration
from . import rank
from . import shortlist as _shortlist
from .candidates import candidates_from_entries
from .features import attach_features
from .filters import read, trust_row
from .verification import NoCatalogSnapshots, attest_candidates, declared_attributes

__all__ = [
    "DEFAULT_SHORTLIST_CAPACITY",
    "ENV_RANKING_WEIGHTS",
    "SHOWN_KIND",
    "SLOT_OFFER_FIELDS",
    "ShortlistStore",
    "bandit_posteriors_of",
    "catalog_of",
    "claim_dimensions_of",
    "configure_ranking",
    "rank_auction",
    "record_shown",
    "registered_domains_of",
    "shortlist_commitments",
    "shortlist_price",
    "shortlist_product",
    "shortlist_store",
    "trust_snapshot_of",
    "weights_of",
    "with_intent_match",
]

#: The frozen ledger kind a filled shortlist slot is announced under.
SHOWN_KIND = "shown"

#: The three R2 fields this module adds to a slot, on top of the five the ranker already builds.
#:
#: Named as data so a test can assert the set rather than restate it, the same way
#: :data:`~exchange.ranking.candidates.CANDIDATE_FIELDS` does for the projection.
SLOT_OFFER_FIELDS: tuple[str, ...] = ("product", "price", "commitments")

#: How many auctions' shortlists one process keeps at once.
#:
#: There is a cap at all because ``POST /auctions`` is unauthenticated and the exchange runs
#: under a 256 MiB limit (``compose.yaml``): a store keyed by a caller-triggered id with only a
#: time bound is a memory leak anybody can drive by posting in a loop. The TTL is the auction's
#: own — DESIGN pins ``auction:{id}`` at 15 minutes — so a shortlist never outlives the auction
#: it describes, and the cap evicts the oldest first when a burst arrives inside one window.
#:
#: **The cap is itself reachable by an unauthenticated caller**, and that is stated here rather
#: than left as a footnote: 513 cheap posts inside one window evict every shortlist written
#: before them. The route's 404 therefore names eviction among its causes, because an operator
#: told "the TTL took it away" about a 30-second-old auction is being told the wrong thing.
DEFAULT_SHORTLIST_CAPACITY = 512

#: The weight set this process ranks with, resolved ONCE at import.
#:
#: At import, not per request, and the difference is the whole point. ``from_env`` raises on a
#: malformed set or one that does not sum to 1.0 — deliberately, because a shortlist produced
#: under weights nobody can reproduce is worse than a failure. Resolved lazily, that raise
#: became a 500 on every ``POST /auctions`` in a container that still booted and still answered
#: its ``/openapi.json`` healthcheck: measured, ``RANK_W_M=0.9`` with the other four unset gave
#: a healthy-looking exchange whose only auction-opening route was totally dead. Resolved here,
#: the same typo fails ``create_app()``, so the container never reports healthy.
#:
#: What the operator is told depends on WHICH mistake was made, and this is stated exactly
#: because the first draft of this comment claimed the variable is always named and that was
#: measured false. A non-numeric value (``RANK_W_M=abc``) names ``RANK_W_M`` — the published
#: loader raises with the variable in the message. A set that parses but does not SUM to 1.0
#: does not: the message is ``w_m=0.9+w_e=0.2+w_t=0.2+w_v=0.15+w_d=0.1 = 1.55``, which names
#: the fields rather than the environment variables. Legible either way, and loud either way;
#: only in the second case does the operator have to map ``w_m`` back to ``RANK_W_M``.
ENV_RANKING_WEIGHTS: RankingWeights = RankingWeights.from_env()


class ShortlistStore:
    """The shortlists this process is currently holding, oldest first.

    In-memory on purpose, and it is the same trade ``InMemoryAuctionStore`` makes: the route
    that writes an entry is the route that computes it, one uvicorn worker serves the exchange
    (``Dockerfile``: ``--workers 1``), and a shortlist is derived data that the auction can
    always produce again. A deployment that wants it to survive a restart replaces this object
    through :func:`configure_ranking`; the route asks only for ``get``/``put``.
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_SHORTLIST_CAPACITY,
        ttl_seconds: float = AUCTION_TTL_SECONDS,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.ttl_seconds = float(ttl_seconds)
        self._entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def put(self, auction_id: str, shortlist: Mapping[str, Any], *, now: float) -> None:
        """Record one auction's shortlist, evicting the oldest when the cap is reached.

        Stored as a deep copy, and read back as one. ``dict(shortlist)`` alone is shallow: the
        same ``slots`` list would be simultaneously in this store and in the
        ``CreateAuctionResponse`` the route hands to pydantic, so one caller mutating what it
        was given would silently rewrite what the next reader of
        ``GET /auctions/{id}/shortlist`` is served. Nothing mutates it today; a store whose
        contents can be changed from outside it is not a store.
        """
        key = str(auction_id)
        self._entries.pop(key, None)
        self._entries[key] = (float(now), deepcopy(dict(shortlist)))
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def get(self, auction_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        """One auction's shortlist, or ``None`` once its TTL has taken it away.

        The comparison is ``>=``: at exactly ``ttl_seconds`` the entry is gone. With ``>`` an
        auction whose Redis record had expired at the same instant still had a readable
        shortlist here, which is the one instant this store is not allowed to outlive it by.
        """
        entry = self._entries.get(str(auction_id))
        if entry is None:
            return None
        written_at, shortlist = entry
        moment = time.time() if now is None else float(now)
        if moment - written_at >= self.ttl_seconds:
            self._entries.pop(str(auction_id), None)
            return None
        return deepcopy(shortlist)

    def __len__(self) -> int:
        return len(self._entries)


def configure_ranking(
    app: Any,
    *,
    trust_snapshot: Any = None,
    registered_domains: Any = None,
    shortlists: ShortlistStore | None = None,
    weights: RankingWeights | None = None,
    catalog: Any = None,
    claim_dimensions: Any = None,
) -> None:
    """Wire an app's ranking collaborators. Anything omitted keeps what is already there.

    ``claim_dimensions`` is the ``claim_type -> trust dimension`` routing the exchange
    announces a minted verdict under — see :func:`claim_dimensions_of` for why it has to be
    injected rather than held here, and :func:`~.verification.attest_candidate_claims` for what
    an exchange without one does instead.
    """
    if trust_snapshot is not None:
        app.state.trust_snapshot = trust_snapshot
    if registered_domains is not None:
        app.state.ranking_registered_domains = registered_domains
    if shortlists is not None:
        app.state.shortlists = shortlists
    if weights is not None:
        app.state.ranking_weights = weights
    if catalog is not None:
        app.state.ranking_catalog = catalog
    if claim_dimensions is not None:
        app.state.ranking_claim_dimensions = claim_dimensions


def shortlist_store(app: Any) -> ShortlistStore:
    """This app's shortlist store, created on first use."""
    store = getattr(app.state, "shortlists", None)
    if store is None:
        store = ShortlistStore()
        app.state.shortlists = store
    return store


def trust_snapshot_of(app: Any) -> Any:
    """This app's trust snapshot — ``{store_id: row}``.

    The default is EMPTY, not absent, and the difference is R12's whole point: a store with no
    row cannot be shown to be off the blacklist, so it is denied. An exchange nobody has
    connected to the trust service therefore ranks nothing, exactly as an exchange nobody has
    connected to an eligibility source solicits nobody.
    """
    snapshot = getattr(app.state, "trust_snapshot", None)
    if snapshot is None:
        snapshot = {}
        app.state.trust_snapshot = snapshot
    return snapshot


def registered_domains_of(app: Any) -> Any:
    """This app's registered-domain source, falling back to the process-wide one.

    The fallback is :func:`~exchange.accept.offer.platform_registered_domains`, so a deployment
    that has already wired the seller registry for the accept path does not have to wire it
    twice — and cannot end up with the two paths disagreeing about which host a store owns.
    """
    source = getattr(app.state, "ranking_registered_domains", None)
    if source is not None:
        return source
    # Imported here rather than at module scope: the accept package is a sibling feature, and
    # this is a fallback, not a dependency of ranking.
    from ..accept.offer import platform_registered_domains  # noqa: PLC0415

    return platform_registered_domains()


def catalog_of(app: Any) -> Any:
    """This app's catalog-snapshot source — what the exchange grades a store's claims against.

    The default is :class:`~exchange.ranking.verification.NoCatalogSnapshots`, which holds a
    snapshot for nobody, and the consequence is stated plainly rather than left to be
    discovered: an exchange with no catalog wired verifies no claim, so no hard constraint is
    satisfied and a hard-constrained auction shortlists nobody (ESC-020). It is the same
    direction :func:`trust_snapshot_of` fails in — an exchange that cannot check something
    denies rather than admits — and, like the trust snapshot, it is a wiring the operator has
    to do rather than one this module can invent, because the alternative to "no catalog" is
    "the bidder's own catalog", which is no check at all.

    The operator does it in the deployment document's ``catalog`` key
    (:mod:`~exchange.composition`), which binds through :func:`configure_ranking` here. Until
    that key existed the default below was not a posture but a dead end: no document could
    say anything else, so **every** deployed exchange verified nothing and shortlisted nobody
    the moment a shopper stated a must-have.
    """
    catalog = getattr(app.state, "ranking_catalog", None)
    if catalog is None:
        catalog = NoCatalogSnapshots()
        app.state.ranking_catalog = catalog
    return catalog


def claim_dimensions_of(app: Any) -> Any:
    """This app's ``claim_type -> trust dimension`` routing, or ``None``.

    ``None`` is the DEFAULT and it is not an oversight: this exchange announces no
    ``claim_verified`` event until a deployment hands it the routing. The frozen payload for
    that kind is ``(claim_ref, status, dim)``; the table that produces ``dim`` is human-approved
    ground truth (``fixtures/manifest.json``, T-080/D18) which ``apps/trust`` consumes
    read-only, and the exchange imports nothing from ``trust`` — its image ships neither
    ``trust`` nor ``fixtures/``. So the only two options here are "be given the table" and
    "invent one", and inventing a routing nobody approved is precisely the failure
    ``trust.scoring.claim_dimension`` raises rather than defaults through. Announcing nothing is
    the same direction :func:`trust_snapshot_of` and :func:`catalog_of` fail in.

    Either shape works: a callable ``claim_type -> dimension`` (``trust.scoring.claim_dimension``
    itself is one, and raising for an unmapped type is handled) or a plain mapping.
    """
    return getattr(app.state, "ranking_claim_dimensions", None)


def bandit_posteriors_of(app: Any) -> Any:
    """This app's bandit posterior book, or ``None`` — the SAME object the outcomes door writes.

    ``app.state.bandit_posteriors`` is the key :mod:`exchange.policy.routes` records into; read
    here so the exploration slice on a served auction consumes the state ``POST
    /internal/outcomes`` produced, rather than a second model that would learn nothing.

    **Read, never created.** ``policy/routes.py``'s own accessor builds the book on first use
    because refusing to *record* is not a safety property; a READER that manufactured one would
    put an empty model into ``app.state`` as a side effect of ranking, so a process that had
    only ever served auctions would report a posterior book it had never written to.

    ``None`` does not disable the slice. The trust-seeded prior is what a posterior is before any
    outcome exists, and a deployment that has taken no outcomes yet is exactly the one where a
    newcomer can never earn any — see :mod:`exchange.policy.exploration`.
    """
    return getattr(app.state, "bandit_posteriors", None)


def weights_of(app: Any) -> RankingWeights:
    """The weight set this app ranks with: its own override, else this process's.

    ``contracts.ranking.RankingWeights.from_env`` is the published loader and reads
    ``RANK_W_M``…``RANK_W_D`` plus ``RANK_WEIGHTS_VERSION``, falling back to the published
    defaults when none is set. It is called exactly once, at this module's import — see
    :data:`ENV_RANKING_WEIGHTS` for why a lazy call was a live outage rather than a tidier
    default.
    """
    weights = getattr(app.state, "ranking_weights", None)
    return ENV_RANKING_WEIGHTS if weights is None else weights


def _finite(value: Any) -> float | None:
    """``value`` as a real, finite number, or ``None``.

    :func:`~exchange.auction.collect._number`'s predicate, restated for the same reason that one
    restates the boundary's: no coercion and no ``bool``. ``"79.99"`` is a string a seller wrote
    rather than a price, ``True`` is not one dollar, and NaN compares false against every bound a
    shopper could set — so all three are absences here, and an absence omits the field.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    """``value`` as a non-empty string, or ``None``. No ``str()`` — a ref is a ref or it is not."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def shortlist_product(offer: Any) -> dict[str, Any] | None:
    """R2's PRODUCT for one slot: which catalogue thing this bid is offering.

    ``None`` — so the slot serves ``product: null`` — when the offer names no readable
    ``product_ref``. That is not a hypothetical: an R10 fallback minted from a roster row with no
    ``product_ref`` carries ``{"product_ref": None, ...}`` verbatim, because ``_list_price_bid``
    copies the row's value whatever it is, and a slot publishing ``{"product_ref": null}`` would
    fail the pinned ``ShortlistProduct`` and turn the buyer's own route into a 500.
    """
    product_ref = _text(read(offer, "product_ref", None))
    if product_ref is None:
        return None
    product: dict[str, Any] = {"product_ref": product_ref}
    variant_ref = _text(read(offer, "variant_ref", None))
    if variant_ref is not None:
        product["variant_ref"] = variant_ref
    return product


def _slot_discount(offer: Any) -> dict[str, Any] | None:
    """The offer's stated discount as a published :class:`~contracts.protocol.Discount`, or ``None``.

    Validated rather than copied. Nothing on the auction path validates a bid against the schema
    — ``validate_bid`` has no call site in ``apps/exchange/src``, which ``auction/routes.py``
    already states — so ``offer["discount"]`` is arbitrary store-written JSON. The T-177 price
    wall reads only its ``value``, so a bid discounting ``{"value": 5}`` with no ``type`` is
    shortlisted today and would be published here as a ``Discount`` missing a required field.
    """
    raw = read(offer, "discount", None)
    if raw is None:
        return None
    try:
        return Discount.model_validate(raw).model_dump(mode="json")
    except ValidationError:
        return None


def _slot_expires_at(offer: Any) -> str | None:
    """The offer's expiry as ONE spelling: ISO-8601 UTC, with a ``Z``.

    Read through :func:`~exchange.checkout.codes.expiry_epoch` — the reader
    :func:`~exchange.ranking.filters.expiry_reason` already decided this candidate's eligibility
    with — and then rendered the way :func:`~exchange.auction.collect.fallback_expires_at` renders
    the exchange's own. Neither half is a new parser: T-182 is the measurement of what two parsers
    for one field cost, and every fixture in this tree writes a float epoch into a property the
    schema declares ``format: date-time``, so a buyer-facing surface that passed the value through
    would publish whichever spelling the bidder happened to pick.

    ``None`` where the instant cannot be read or cannot be rendered. A shortlisted candidate has
    already passed the expiry filter, so this is the unreachable branch rather than the normal
    one — but "unreachable" is a property of code two modules away, and the field is omitted
    rather than guessed at if that ever stops being true.
    """
    raw = read(offer, "expires_at", None)
    if raw is None:
        return None
    try:
        epoch = expiry_epoch(raw)
    except (UnusableOffer, TypeError, ValueError):
        return None
    if not math.isfinite(epoch):
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return None


def shortlist_price(offer: Any) -> dict[str, Any] | None:
    """R2's PRICE for one slot: what this store is asking, and until when.

    ``None`` unless BOTH ``unit_price`` and ``total_price`` read as finite numbers, which is the
    published ``ShortlistPrice``'s own rule and the reason it requires them together. That case is
    real rather than defensive padding: ``_list_price_bid`` mints an offer with neither price when
    the roster prices the product at zero, at a negative, at something unreadable or not at all
    (T-277), and R10 still puts that store in front of the buyer. A slot for it serves
    ``price: null`` — never a ``0.0``, which is the cheapest number there is and would beat every
    real bid in the shortlist beside it.
    """
    unit_price = _finite(read(offer, "unit_price", None))
    total_price = _finite(read(offer, "total_price", None))
    if unit_price is None or total_price is None:
        return None
    price: dict[str, Any] = {"unit_price": unit_price, "total_price": total_price}
    currency = _text(read(offer, "currency", None))
    if currency is not None:
        price["currency"] = currency
    discount = _slot_discount(offer)
    if discount is not None:
        price["discount"] = discount
    expires_at = _slot_expires_at(offer)
    if expires_at is not None:
        price["expires_at"] = expires_at
    return price


def shortlist_commitments(offer: Any) -> list[dict[str, Any]] | None:
    """R2's COMMITMENTS for one slot: what this store promises beside the price.

    Each entry is validated against the published :class:`~contracts.protocol.Claim` and dropped
    if it does not conform, for the reason :func:`_slot_discount` gives: no schema validation runs
    on a bid anywhere on the auction path, so ``offer["commitments"]`` is whatever the store
    wrote. A commitment with no provenance is not a weaker commitment, it is not one — nothing
    could later grade the store against it — so publishing it under a name the buyer reads as a
    promise would be the exchange vouching for a string.

    ``None`` — served as ``commitments: null`` — when the offer states no commitments at all, and
    ``None`` again when every one it stated was unreadable. An empty list is not the same
    statement: it reads as "this store committed to nothing", which is a claim about the store
    rather than about what the exchange could read.
    """
    raw = read(offer, "commitments", None)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    commitments: list[dict[str, Any]] = []
    for item in raw:
        try:
            claim = Claim.model_validate(item)
        except ValidationError:
            continue
        commitments.append(claim.model_dump(mode="json"))
    return commitments or None


def _slot_offer_fields(offer: Any) -> dict[str, Any]:
    """The three R2 fields this offer can support, with the ones it cannot left out."""
    fields = {
        "product": shortlist_product(offer),
        "price": shortlist_price(offer),
        "commitments": shortlist_commitments(offer),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _with_offer_fields(shortlist: Mapping[str, Any], candidates: Sequence[Any]) -> dict[str, Any]:
    """``shortlist`` with each slot carrying its candidate's product, price and commitments.

    ADDITIVE, and additive in the strict sense: a key the slot already carries a VALUE under is
    never overwritten, so the five fields ``ranking.shortlist.build`` produced — including the
    trust summary and the provenance labels, which are the ranker's reading and not the offer's —
    are exactly what they were. The three added here are keyed off ``bid_ref``, which is the
    minted ``bid_id`` and the only handle a slot has back to the candidate it came from.

    **The result is re-validated through the pinned model, and that is what makes the two doors
    agree rather than a claim that they do.** ``GET /auctions/{auction_id}/shortlist`` declares
    ``response_model=Shortlist``, so FastAPI re-validates whatever is stored and serializes the
    MODEL — which fills every optional it was not given with an explicit ``null`` and every
    ``Claim``'s unstated fields with theirs. ``POST /auctions`` types its ``shortlist`` as
    ``dict[str, Any]`` and passes the stored object through verbatim. So a stored slot that is
    not already a fixed point of ``Shortlist.model_validate(...).model_dump(mode="json")`` is
    served in two different spellings by two routes that document themselves as serving "the same
    object", and ``test_the_shortlist_is_readable_at_the_published_path_after_the_auction_closes``
    fails — measured, on the first draft of this function, which omitted an unsupported key
    instead of publishing ``null``: the GET body carried ``price: null`` and the POST body carried
    no ``price`` at all.

    A field the exchange could not read is therefore served as ``null`` rather than as an absent
    key. That is the contract's own spelling for it (all three properties admit ``null``), it is
    the one spelling both doors can produce, and it is emphatically not a zero: a slot with no
    readable price says ``price: null``, never ``{"unit_price": 0.0}``.
    """
    by_bid_id: dict[str, Any] = {}
    for candidate in candidates:
        bid_id = str(read(candidate, "bid_id", "") or "")
        # First wins. `_best_bid_per_store` keeps the FIRST row with a given `bid_id`, so a
        # duplicate id — which arrives from a misbehaving fan-out, and which that function
        # exists because of — resolves to the same candidate the slot was built from.
        if bid_id and bid_id not in by_bid_id:
            by_bid_id[bid_id] = candidate

    slots: list[dict[str, Any]] = []
    for slot in shortlist.get("slots", ()) or ():
        enriched = dict(slot)
        candidate = by_bid_id.get(str(enriched.get("bid_ref", "")))
        supported = _slot_offer_fields(read(candidate, "offer", None))
        for key, value in supported.items():
            if enriched.get(key) is None:
                enriched[key] = value
        slots.append(enriched)
    return Shortlist.model_validate({**shortlist, "slots": slots}).model_dump(mode="json")


def record_shown(
    recorder: Any,
    candidates: Sequence[Any],
    *,
    auction_id: str,
    shortlist: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Announce one ``shown`` event per shortlist slot this auction actually filled.

    **The kind existed and nothing wrote it.** D34 freezes ``shown`` with the payload
    ``(bid_ref, slot)`` — a bid, and which of the four differentiated slots it was awarded —
    and until this function the only thing in the tree that emitted one was the S1 end-to-end
    harness, writing it for itself out of a shortlist it had also ranked itself. A shortlist
    the exchange served and did not record is an exchange that cannot say afterwards what it
    put in front of a buyer, which is the whole point of an append-only ledger on this path.

    Emitted where the shortlist is STORED, so what is announced is what a reader of
    ``GET /auctions/{auction_id}/shortlist`` will be served, not a candidate list that might
    still be filtered. One event per slot, and slots are unique per auction by construction
    (``ranking.shortlist.build`` keeps one bid per store and assigns each name once).

    ``candidates`` supplies the ``store_id`` for the event's own top-level field: a slot
    carries ``bid_ref`` and no store, and the ledger's ``store_id`` must be the exchange's
    attribution rather than anything read off a bid. A slot whose ``bid_ref`` matches no
    candidate is announced with no store rather than dropped — the slot WAS shown, and losing
    the record of it to a join miss would be the worse error.

    The payload's two published keys are the slot's own, and the shortlist has already been
    through ``Shortlist.model_validate`` in :func:`_with_offer_fields`, so the frozen shape is
    guaranteed upstream rather than re-checked here.

    Returns:
        The events emitted, in slot order. Never raises: an audit trail must not be able to
        fail a live auction.
    """
    record = getattr(recorder, "record", None)
    if not callable(record):
        return []
    store_by_bid: dict[str, str] = {}
    for candidate in candidates or ():
        bid_id = str(read(candidate, "bid_id", "") or "")
        if bid_id and bid_id not in store_by_bid:
            store_by_bid[bid_id] = str(read(candidate, "store_id", "") or "")

    events: list[dict[str, Any]] = []
    for slot in shortlist.get("slots", ()) or ():
        bid_ref = str(read(slot, "bid_ref", "") or "")
        payload = {"bid_ref": bid_ref, "slot": read(slot, "slot", None)}
        events.append(
            record(
                SHOWN_KIND,
                auction_id=auction_id,
                store_id=store_by_bid.get(bid_ref) or None,
                payload=payload,
            )
        )
    return events


def with_intent_match(candidates: Sequence[Any], measured: Mapping[str, float] | None) -> list[Any]:
    """Every candidate carrying the PLATFORM's fit measurement for its store, where there is one.

    New records, never mutated ones — :func:`rank` promises its inputs are never written to, and
    :func:`~.features.attach_features` one line above keeps the same promise.

    ``intent_match`` is the one published feature no module in this package can COMPUTE:
    DESIGN.md:132 says it comes from retrieval+rerank, and a served auction is handed a roster
    rather than a query against an index. So it arrives from the caller that ran the retrieval,
    and the only question this function answers is what to do with what it was handed.

    **An unreadable measurement is ABSENT, not scored.** ``None``, ``NaN``, ``inf``, a string, a
    list, a bool — anything that is not a finite ``int``/``float`` — leaves the key off the record
    entirely, so :func:`.scoring.feature_vector` applies the published ``INTENT_MATCH_WHEN_ABSENT``.
    That is the same rule ``.features`` applies to every feature it cannot compute, and the reason
    is the same: writing a number for "we do not know" moves rankings for a reason nobody could
    audit. Refusing it HERE rather than relying on the scorer's own NaN guard matters because
    absence and unreadability are then the same state on the record itself, so a reader of
    ``projected`` sees what the scorer saw.

    **Stricter than** :func:`.scoring._number`, deliberately: that function accepts ``"0.99"``
    because it reads whatever reached a candidate record, while this map is a PLATFORM-INTERNAL
    answer from the retrieval, which states floats. A string arriving here means something upstream
    is not the component it claims to be, and coercing it would be this seam repairing a producer
    it cannot see and then vouching for the repair — the same refusal
    :func:`.candidates.fallback_checkout_url` makes about a malformed registry row.

    A store the map does not name keeps the absent term too. A roster may mix sources — a request
    body naming stores the graph never scored, an R10 fallback minted for a silent one — and "we
    did not measure this one" is a true thing to say about it.

    Clamping is deliberately NOT done here: ``feature_vector`` clamps every feature to the
    published normalization bounds, and a second clamp in a second place is how two readings of
    one bound drift apart.
    """
    records = [dict(c) if isinstance(c, Mapping) else c for c in candidates or ()]
    if not measured:
        return records
    for record in records:
        if not isinstance(record, dict):
            continue
        raw = measured.get(str(record.get("store_id") or ""))
        # `bool` is an `int`, and `True` would score 1.0 — a perfect fit minted by a producer
        # that meant "yes". Excluded by name rather than left to the isinstance below.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        fit = float(raw)
        if math.isfinite(fit):
            record["intent_match"] = fit
    return records


class _Explored(NamedTuple):
    """The shortlist an exploration slice produced, and the record of what it cost."""

    shortlist: Mapping[str, Any]
    payload: dict[str, Any]


def _promoted_order(
    rows: Sequence[dict[str, Any]], *, promote: str, displace: str
) -> list[dict[str, Any]]:
    """``rows`` with the promoted bid moved to sit where the displaced bid sits.

    Reordering the input to :func:`~.shortlist.build` rather than editing the shortlist it
    produced, because the slot NAMES are assigned by leading dimension over the pool
    (:func:`~.shortlist.assign_slot_names`) — swapping a ``bid_ref`` inside a finished shortlist
    would leave the promoted candidate wearing a label that was computed for somebody else.
    """
    promoted = next((row for row in rows if str(row.get("bid_id") or "") == promote), None)
    if promoted is None:
        return list(rows)
    kept = [row for row in rows if row is not promoted]
    at = next(
        (index for index, row in enumerate(kept) if str(row.get("bid_id") or "") == displace),
        None,
    )
    if at is None:
        return list(rows)
    return [*kept[:at], promoted, *kept[at:]]


def _explore(
    ranked: Mapping[str, Any],
    *,
    auction_id: str,
    intent: Any,
    trust_snapshot: Any,
    book: Any,
) -> _Explored | None:
    """R12's exploration slice for one ranked auction, or ``None`` to leave the shortlist alone.

    The whole of the policy — how many slots, which candidates may be promoted, which one is —
    lives in :mod:`exchange.policy.exploration`. This function is the join: it is the only place
    holding the ranked rows, the trust snapshot they were scored against, and the auction id that
    seeds the sampler.

    **Never raises.** An exploration slice is an improvement to what a buyer is shown, and an
    improvement that can 500 a live auction is not one. Anything that goes wrong leaves the
    published shortlist exactly as ``rank()`` built it.
    """
    rows = list(ranked.get("ranked") or ())
    if not rows:
        return None
    try:
        pool = _shortlist.slot_pool(rows)
        bench = _shortlist.bench(rows)
        if not bench:
            return None

        # Handed as a CALLABLE, not as a computed map, and the difference is measured rather
        # than stylistic: the sampler draws 512 joint samples with one `betavariate` per store
        # per draw, inside R10's synchronous window — 2.5 ms over 5 stores, 24 ms over 50,
        # 250 ms over 500, against a `MAX_ROSTER_ENTRIES` of 500. `plan_exploration` decides
        # every cheap structural clause first and then asks for exactly the stores that could
        # decide the slot. Measured end to end over 500 candidates, on this tree: `rank_auction`
        # costs 18 ms with nothing to explore, 20 ms with one low-data challenger and 52 ms with
        # all 500 low-data — against 330 ms for the draft that sampled before deciding.
        def sampled(stores: Sequence[str]) -> Mapping[str, float]:
            return exposure_shares(
                book,
                stores=stores,
                cluster_id=str(read(intent, "cluster_id", "") or ""),
                # The SAME rows the ranker scored against, read through the same accessor, so
                # the blacklist and the `low_data` mark the slice is decided on cannot disagree
                # with the ones the shortlist's own trust summary published.
                trust_snapshot={
                    str(row.get("store_id") or ""): trust_row(
                        str(row.get("store_id") or ""), trust_snapshot
                    )
                    for row in rows
                },
                seed=auction_id,
            )

        plan = plan_exploration(pool, bench, sampled)
        if plan is None:
            return None
        promoted = _shortlist.build(
            _promoted_order(rows, promote=plan.bid_id, displace=plan.displaced_bid_id),
            str(ranked["shortlist"].get("auction_id") or auction_id),
        )
        # The slot NAME is resolved off the shortlist that was actually built, never predicted:
        # a promoted candidate takes whichever dimension it leads on, which is not knowable
        # before the pool it joined is known.
        slot = next(
            (
                s.get("slot")
                for s in promoted.get("slots", ())
                if str(s.get("bid_ref") or "") == plan.bid_id
            ),
            None,
        )
        if slot is None:
            # The promotion did not actually fill a slot. Nothing is published and nothing is
            # changed: a report of a cost nobody paid is worse than no report.
            return None
        return _Explored(shortlist=promoted, payload=plan.as_payload(slot))
    except Exception:  # noqa: BLE001 - an audit-and-exposure improvement must not fail an auction
        return None


def rank_auction(
    entries: Sequence[Any],
    *,
    auction_id: str,
    intent: Any,
    now: float,
    trust_snapshot: Any,
    registered_domains: Any = None,
    weights: RankingWeights | None = None,
    catalog: Any = None,
    product_refs: Any = None,
    recorder: Any = None,
    claim_dimensions: Any = None,
    intent_match: Mapping[str, float] | None = None,
    bandit_posteriors: Any = None,
) -> dict[str, Any]:
    """Rank one closed auction's collected bids and build its shortlist.

    ``now`` is the instant the auction closed, passed in rather than read here, so the expiry
    filter and the auction's own ``closed_at`` are the same instant and a shortlist is
    reproducible from its inputs.

    The eligibility SOURCE is not passed through to :func:`rank`. R12's gate already ran over
    exactly this roster inside ``solicit_bids`` — every entry here belongs to a store that
    cleared it, and a store that did not is in ``denied`` rather than in ``entries`` — so
    handing the source to the ranker as well would read the eligibility backend a second time,
    once per candidate, inside the synchronous window R10 bounds. The blacklist still fails
    closed here: it is derived from ``trust_snapshot``, which is the published four-argument
    surface, and a store with no row is denied.

    The CATALOG is passed through, and it is what makes the claims on these candidates
    evidence rather than assertions (ESC-020). Between the projection and the ranking, each
    store's claims are checked by :func:`claim_verification.verify` against the snapshot this
    exchange holds for that store, and the verdict is attested with a key the bidder does not
    have. Whatever the store wrote under ``status`` is dropped on the way through and is read
    by nothing. A catalog of ``None`` verifies nothing, which is a denial rather than an
    admission: see :func:`catalog_of`.

    ``product_refs`` is ``{store_id: product_ref}`` off the auction's ROSTER, so which
    catalogue entry a store's claims are graded against is the auction's fact rather than the
    bidder's. Without it a store bidding one product could have its claims verified against
    another product in its own catalogue — a real verdict about the wrong thing.

    ``intent_match`` is ``{store_id: fit}``, **the platform's own retrieval measurement**, and it
    is the seam that closes the largest published term. ``w_m = 0.35`` and until a graph reached
    the auction route nothing produced it, so every candidate of every served auction took
    ``INTENT_MATCH_WHEN_ABSENT`` and more than a third of the score was a constant. There was no
    seam because the projection in :mod:`.candidates` names its fields and copies no published
    feature — the R11 property that stops a bidder writing ``intent_match: 1.0`` into its own
    reply — so ``auction/routes.py`` applied the term by calling the public :func:`rank` a SECOND
    time over the candidates this function had already returned. This parameter replaces that:
    correct either way, but one filter/score/shortlist pass instead of two.

    **Adding it does not weaken R11**, and the ordering is what makes that true rather than the
    intent. The map arrives as a keyword argument from the CALLER — the auction route, out of the
    retrieval's per-shop answer — and is applied AFTER :func:`attach_features`, so the last writer
    of the key is the platform. Nothing a bidder sends can reach it: the projection never copies
    the name, ``Bid`` is ``additionalProperties: false`` and declares no feature fields, and a
    store not named in the map keeps the ABSENT term rather than a fabricated one.

    ``bandit_posteriors`` is R12's exploration slice, and it is the READ half of the loop
    ``POST /internal/outcomes`` writes. See :mod:`exchange.policy.exploration` for the bound —
    at most one of the four slots, never the leader, never past eligibility — and for why a
    saturating score plus a slow trust signal makes the guarantee load-bearing rather than
    decorative. ``None`` is not "off": the trust-seeded prior IS the posterior before any
    outcome exists, which is exactly the deployment where a newcomer would otherwise be locked
    out permanently. What genuinely turns it off is an auction with no low-data candidate below
    the cut, and then the shortlist is byte-identical to what it was before this parameter.
    """
    candidates = candidates_from_entries(
        entries,
        auction_id=auction_id,
        registered_domains=registered_domains,
    )
    # `recorder` and `claim_dimensions` are what turn the attestation into a RECORD. Without
    # them a served auction ran the claim verifier over every candidate, decided hard
    # constraints and `verified_claim_ratio` on the answers, and then dropped every verdict
    # when the request ended — nothing in the ledger said a claim had ever been checked. Both
    # are no-ops when omitted: see `verification.attest_candidate_claims`.
    candidates = attest_candidates(
        candidates,
        catalog=catalog,
        product_refs=product_refs,
        recorder=recorder,
        auction_id=auction_id,
        dimensions=claim_dimensions,
    )
    # The published formula's INPUTS, and the reason this line is not optional: without it
    # four of the five features are absent on every served candidate, each takes its published
    # neutral, and `rank_score` is `0.4 + 0.2*trust` — a one-term formula wearing a five-term
    # one's name. Measured over a real socket, inverting every list price on the roster then
    # returned bit-identical scores and the identical shortlist.
    #
    # AFTER `attest_candidates`, never before: `verified_claim_ratio` counts the verdicts this
    # exchange attested, and over unattested claims it would count none of them — a silent 0.0
    # for every honest store. `entries` are handed over positionally for the roster's
    # `list_price`, which exists on no candidate; see :mod:`.features` for what each feature
    # reads and for the one (`intent_match`) this exchange still cannot produce.
    #
    # `intent` is handed over because `verified_claim_ratio` is BUYER-CONDITIONAL now: its
    # numerator counts only the verified claims whose key lands on something this shopper
    # actually asked about (D55's persuasion market — see `.features` for the term-by-term
    # measurement of why the formula paid nothing for customization before). Without it every
    # candidate's evidence term is absent and the one feature a store can move for THIS buyer
    # goes flat, which is the defect the redefinition exists to close. Only the intent's asks
    # are read; `Intent.preferences[].weight` never touches the published weights (D50).
    #
    # `trust_snapshot` is handed over because `delivery_fit` is a comparison of CREDIBLE
    # delivery promises now, not declared ones (D57). It carries `w_d = 0.10` and its feed was
    # `Offer.delivery_estimate_days` verbatim — a number the bidding store types — so a store
    # bought up to a tenth of the published score by promising sooner, with nothing anywhere
    # asking whether it had ever shipped that fast. `features.dispatch_credibility` reads the
    # `shipped_on_time` DIMENSION off this same snapshot (not its aggregate `score`, which is
    # `trust`'s own term and is read separately below) and divides the quote by it before the
    # auction-normalisation runs. **Without this argument every promise is unadmitted**, so
    # `delivery_fit` is absent on every candidate and reads its published neutral — which is the
    # honest failure, and is why the parameter is not optional in practice even though it
    # defaults to `None`.
    candidates = attach_features(candidates, entries, intent=intent, trust_snapshot=trust_snapshot)
    # The PLATFORM's fit measurement, applied last so it is the last writer of the key. See
    # `with_intent_match` for what an unreadable measurement does and why it is refused here.
    candidates = with_intent_match(candidates, intent_match)
    ranked = rank(
        candidates,
        intent,
        trust_snapshot,
        {"now": float(now), "auction_id": auction_id},
        weights=weights,
        # WHICH attributes this exchange's own catalogues declare for these stores, and the
        # only reason this function can supply it: it holds the catalog and `rank()` does not.
        # It is what lets the ranker tell "this candidate failed the must-have" apart from
        # "this network cannot decide the must-have for anybody" — a shopper who says
        # "espresso" states a constraint no catalogue here carries, and before this the whole
        # shortlist was emptied by it with nothing said. `None` when the catalog is unwired or
        # declares nothing, and then nothing is ever relaxed (ESC-020's direction).
        network_attributes=declared_attributes(
            catalog,
            [read(candidate, "store_id", None) for candidate in candidates],
            product_refs=product_refs,
        ),
    )
    # `projected`, ADDITIVE, and it is the repair for T-349. `rank()` answers with its own
    # ROW projection under `"candidates"` — `bid_id`, `eligible`, `rank_score`, the trust
    # summary — and that row carries neither `offer` nor `store_domain`. Both are on the
    # candidates built above, and both are read by the ACCEPT path, so a caller that only
    # ever saw `rank()`'s output had no way to record a bid the accept door could use: every
    # recorded bid was written with `offer: {}`, which cost it its expiry, its pre-mint host
    # check and its cart permalink. This function is the only place that holds both, so it
    # is the only place that can hand both over. Nothing is removed and no existing key
    # changes, so `_excluded_out(ranking["candidates"])` — which genuinely does want the row
    # — is untouched.
    #
    # The shortlist is rewritten for the SAME reason, and it is R2's other three fields.
    # `ranking/shortlist.py` is handed rank ROWS, which carry no `offer` at all, so PRODUCT,
    # PRICE and COMMITMENTS cannot be read there — measured: `build`'s input has `bid_id`,
    # `store_id`, `rank_score`, the trust summary and the provenance labels, and nothing else
    # about what was offered. This function holds the built shortlist and the projected
    # candidates at the same time, so it is the only place the join is available.
    # R12's exploration slice, applied to the SHORTLIST and to nothing else. `ranked["ranked"]`
    # — the published order, the scores and every component — is returned exactly as `rank()`
    # produced it, which is `policy/bandit.py`'s own rule stated as code: the bandit "adjusts
    # exposure and exploration only ... it never touches rank".
    explored = _explore(
        ranked,
        auction_id=auction_id,
        intent=intent,
        trust_snapshot=trust_snapshot,
        book=bandit_posteriors,
    )
    shortlist = ranked["shortlist"] if explored is None else explored.shortlist
    return {
        **ranked,
        "shortlist": _with_offer_fields(shortlist, candidates),
        "projected": list(candidates),
        # `None` on every auction that explored nothing, which is most of them. Published on
        # the RESPONSE rather than on the slot for the reason `relaxed_constraints` is:
        # `ShortlistSlot` is a pinned `additionalProperties: false` contract, and a fact that
        # reached the buyer only through a schema change would not have reached them at all.
        "exploration": None if explored is None else explored.payload,
    }
