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
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from contracts.ranking import RankingWeights

from ..auction.state import AUCTION_TTL_SECONDS
from . import rank
from .candidates import candidates_from_entries

__all__ = [
    "DEFAULT_SHORTLIST_CAPACITY",
    "ShortlistStore",
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
DEFAULT_SHORTLIST_CAPACITY = 512


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
        """Record one auction's shortlist, evicting the oldest when the cap is reached."""
        key = str(auction_id)
        self._entries.pop(key, None)
        self._entries[key] = (float(now), dict(shortlist))
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def get(self, auction_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        """One auction's shortlist, or ``None`` once its TTL has taken it away."""
        entry = self._entries.get(str(auction_id))
        if entry is None:
            return None
        written_at, shortlist = entry
        moment = time.time() if now is None else float(now)
        if moment - written_at > self.ttl_seconds:
            self._entries.pop(str(auction_id), None)
            return None
        return dict(shortlist)

    def __len__(self) -> int:
        return len(self._entries)


def configure_ranking(
    app: Any,
    *,
    trust_snapshot: Any = None,
    registered_domains: Any = None,
    shortlists: ShortlistStore | None = None,
    weights: RankingWeights | None = None,
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


def weights_of(app: Any) -> RankingWeights:
    """The weight set this app ranks with, resolved once from the environment.

    ``contracts.ranking.RankingWeights.from_env`` is the published loader and reads
    ``RANK_W_M``…``RANK_W_D`` plus ``RANK_WEIGHTS_VERSION``, falling back to the published
    defaults when none is set. It raises on a set that is malformed or does not sum to 1.0 —
    deliberately, because a shortlist produced under weights nobody can reproduce is worse than
    a request that fails. Resolution is lazy and memoised on ``app.state`` (the pattern the
    auction route already uses for its collaborators); a deployment that wants a malformed
    weight set to fail at BOOT rather than on the first ranked auction passes
    ``configure_ranking(app, weights=RankingWeights.from_env())`` at startup.
    """
    weights = getattr(app.state, "ranking_weights", None)
    if weights is None:
        weights = RankingWeights.from_env()
        app.state.ranking_weights = weights
    return weights


def rank_auction(
    entries: Sequence[Any],
    *,
    auction_id: str,
    intent: Any,
    now: float,
    trust_snapshot: Any,
    registered_domains: Any = None,
    weights: RankingWeights | None = None,
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
    """
    candidates = candidates_from_entries(
        entries,
        auction_id=auction_id,
        registered_domains=registered_domains,
    )
    return rank(
        candidates,
        intent,
        trust_snapshot,
        {"now": float(now), "auction_id": auction_id},
        weights=weights,
    )
