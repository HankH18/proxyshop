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

**And "hands them their inputs" is now literal.** :func:`rank_auction` is the only place that
holds a served auction's bids, its roster and this exchange's catalogue at once, so it is the
only place that can run :mod:`.verification` and then :mod:`.features` over them in that order
— which is what makes ``verified_claim_ratio``, ``price_value`` and ``delivery_fit`` exist on a
served candidate at all. Without that pair of lines the formula still runs and still returns a
number, but four of its five terms are neutral and the number is ``0.4 + 0.2*trust``.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from contracts.ranking import RankingWeights

from ..auction.state import AUCTION_TTL_SECONDS
from . import rank
from .candidates import candidates_from_entries
from .features import attach_features
from .verification import NoCatalogSnapshots, attest_candidates

__all__ = [
    "DEFAULT_SHORTLIST_CAPACITY",
    "ENV_RANKING_WEIGHTS",
    "ShortlistStore",
    "catalog_of",
    "configure_ranking",
    "rank_auction",
    "registered_domains_of",
    "shortlist_store",
    "trust_snapshot_of",
    "weights_of",
]

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
) -> None:
    """Wire an app's ranking collaborators. Anything omitted keeps what is already there."""
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
    """
    candidates = candidates_from_entries(
        entries,
        auction_id=auction_id,
        registered_domains=registered_domains,
    )
    candidates = attest_candidates(candidates, catalog=catalog, product_refs=product_refs)
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
    candidates = attach_features(candidates, entries)
    ranked = rank(
        candidates,
        intent,
        trust_snapshot,
        {"now": float(now), "auction_id": auction_id},
        weights=weights,
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
    return {**ranked, "projected": list(candidates)}
