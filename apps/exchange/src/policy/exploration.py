"""The READ half of the bandit's loop: one shortlist slot, spent on a store with no record.

``policy/routes.py`` wired the WRITE half — ``POST /internal/outcomes`` folds a conversion into
:func:`~.bandit.update` — and said in its own source that the other half was still open:
":func:`~.bandit.exposure` has no production call site: nothing on the served path reads the state
this door writes." This module is that call site, and :mod:`exchange.ranking.serving` is where it
runs.

Why the gap mattered more than "a function with no callers"
------------------------------------------------------------
The published quality terms SATURATE — ``price_value`` stops paying at the auction's own price
band, ``verified_claim_ratio`` has diminishing returns — and ``trust`` is exogenous and moves over
months. Together those two make the market self-locking: whoever transacted first holds a score a
newcomer cannot out-earn, so a store with a genuinely better pitch is never shown, and because it
is never shown it never accumulates the outcomes that would prove it. R12 answers exactly that
with "a guaranteed exploration slice exposes low-data stores", and until this module the slice was
a floor inside a sampler nobody called.

What exploration costs, and the bound chosen for it
----------------------------------------------------
This is not free and the module refuses to pretend otherwise. A slot filled by exploration is a
slot NOT filled by the candidate the published ranking put there, so the shopper in front of this
one auction pays — in expectation, with a slightly worse fourth option — for information the
market as a whole needs. The bound is therefore structural rather than statistical, so it holds on
every auction rather than on average:

* **:data:`EXPLORATION_SLOTS` = 1**, out of ``shortlist.MAX_SLOTS`` = 4. At most a quarter of the
  shortlist, and only ever the LAST of the slots that would have been filled — the ranking's
  leader and runners-up are not reachable from here at all. A buyer who reads one slot reads the
  ranking's own answer; a buyer who reads all four sees three of them plus a newcomer.
* **Only when someone was going to be left out anyway.** An auction whose eligible stores all fit
  inside the shortlist explores nothing: there is no slot to take and therefore no cost to pay.
* **Only when the slice is not already filled.** A low-data store that earned a slot on rank alone
  has already had the exposure this exists to buy, so nothing is displaced for a second one.
* **Only among the ELIGIBLE.** This module is handed rows the ranker already admitted. R19's hard
  constraints, R12's blacklist, the expiry filter and the C10 domain check all decided before it
  runs; exploration reorders who is shown and admits nobody.
* **Only for a store the trust snapshot positively marks ``low_data``.** Absent, ``None``, or a
  snapshot that carries no row at all is NOT a yes (R12's fail-closed direction, applied to the
  slice rather than to the ban): a slice handed out on "we could not tell" is budget taken from a
  store the platform actually knows is new.
* **Only :data:`MAX_SAMPLED_CHALLENGERS` of them contest it in one auction.** That last one is a
  CPU bound on R10's synchronous window rather than a policy, and it is the one clause that gives
  something up; :func:`plan_exploration` states exactly what.

WHICH low-data store gets the slot is the only part the posterior decides, and that is the whole
reason this module reads the bandit rather than picking the highest-ranked newcomer: the choice is
:func:`~.bandit.exposure`'s probability-of-being-best, so a newcomer that has started converting is
explored ahead of one that has not, and a store the sampler reports at exactly 0.0 — the value the
bandit reserves for "banned" — is never chosen.

Determinism
-----------
``exposure`` seeds a :class:`random.Random` from a BLAKE2b digest of ``(seed, cluster_id)``,
never from :func:`hash` and never from the clock. The caller passes the AUCTION's own id as the
seed, so one auction has one answer however many times it is ranked, while two auctions draw
independently — which is what makes the slice rotate across auctions instead of pinning one
newcomer forever.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .bandit import BanditState, exposure, initial_state

__all__ = [
    "EXPLORATION_SLOTS",
    "MAX_SAMPLED_CHALLENGERS",
    "ExplorationSlice",
    "exposure_shares",
    "is_low_data",
    "plan_exploration",
]

#: How many of the shortlist's slots exploration may take. **One**, and it is a constant rather
#: than a configurable because the number is the bound: a deployment that could set it to 4 would
#: be a deployment where the published ranking decides nothing.
EXPLORATION_SLOTS = 1

#: How many low-data candidates one auction's exposure draw may contest between.
#:
#: A CPU bound on the synchronous auction window, not a policy: the sampler is O(stores x draws)
#: and ``MAX_ROSTER_ENTRIES`` is 500, so an auction whose whole roster is low-data would spend a
#: quarter of a second deciding one slot. 60 challengers plus the four-slot pool keeps the draw
#: at ~64 stores — measured at about 30 ms — while being far above any roster a catalogue graph
#: returns in practice. Challengers past the cap are dropped in RANK order, so it is the
#: best-ranked low-data stores that contest the slice.
MAX_SAMPLED_CHALLENGERS = 60


@dataclass(frozen=True)
class ExplorationSlice:
    """One slot spent on exploration, and the candidate that paid for it.

    Both halves are carried because only the pair is inspectable. "A newcomer is in slot four" is
    not evidence of anything on its own — it could have ranked there — while "this bid took the
    slot this other bid would have held, at this exposure share" is a statement a reader can check
    against the same auction's ``ranked`` list.
    """

    store_id: str
    bid_id: str
    exposure_share: float
    displaced_store_id: str
    displaced_bid_id: str

    def as_payload(self, slot: Any = None) -> dict[str, Any]:
        """The published shape, for the route that reports it.

        ``slot`` is the shortlist slot NAME the promoted bid ended up holding — resolved by the
        caller after the shortlist is rebuilt, because the names are assigned by leading
        dimension (``shortlist.assign_slot_names``) rather than by position, so which one a
        promoted candidate takes is not knowable here.
        """
        return {
            "bid_ref": self.bid_id,
            "store_id": self.store_id,
            "slot": None if slot is None else str(slot),
            "exposure_share": float(self.exposure_share),
            "displaced_bid_ref": self.displaced_bid_id,
            "displaced_store_id": self.displaced_store_id,
        }


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _floor_of(book: Any) -> float:
    """The exploration floor this deployment's posterior book is seeded with.

    Read off the book rather than restated, so the share arithmetic on the served path is the
    same arithmetic ``POST /internal/outcomes`` records under. Anything unreadable is 0.0 —
    :func:`~.bandit.initial_state` RAISES outside ``[0, 1]``, and a deployment's typo must not be
    able to take down the auction door.

    A floor of 0.0 does not disable this module, and that is deliberate: the floor shapes the
    exposure SHARES, while the slice above is structural. A guarantee that only holds where an
    operator remembered to set a number is not the guarantee R12 describes.
    """
    raw = getattr(book, "exploration_floor", None)
    if raw is None:
        return 0.0
    try:
        floor = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(floor):
        return 0.0
    return min(1.0, max(0.0, floor))


def _learned(book: Any) -> BanditState | None:
    """The posteriors this process has actually recorded, or ``None``.

    Accepts a :class:`~.bandit.BanditState` directly or anything exposing ``state()`` — the shape
    :class:`~.routes.InMemoryBanditPosteriors` publishes and the seam D26's Redis-backed model
    plugs into. A book that raises is a book that knows nothing: exploration must not be able to
    fail an auction.
    """
    if book is None:
        return None
    if isinstance(book, BanditState):
        return book
    reader = getattr(book, "state", None)
    if not callable(reader):
        return None
    try:
        learned = reader()
    except Exception:
        return None
    return learned if isinstance(learned, BanditState) else None


def exposure_shares(
    book: Any,
    *,
    stores: Sequence[str],
    cluster_id: str,
    trust_snapshot: Mapping[str, Any],
    seed: Any,
) -> dict[str, float]:
    """``{store_id: share}`` for THIS auction's stores in THIS auction's cluster.

    The state is built for the auction rather than read out of the book, and that is the load
    bearing difference between this and a plain ``book.state()``: :class:`InMemoryBanditPosteriors`
    grows one ``(cluster, store)`` pair per RECORDED OUTCOME, so a store that has never converted
    — which is every store this module exists for — appears in it nowhere. Reading the book alone
    would report no share for exactly the candidates the exploration slice is meant to find.

    So :func:`~.bandit.initial_state` seeds a state over the auction's own roster from the trust
    snapshot (which is what applies R12's fail-closed blacklist and the ``low_data`` marks), and
    every posterior the book already holds for this cluster is carried over on top. Learning
    survives; ignorance is priced as the prior rather than as absence.

    ``{}`` — never a partial map, and never a guess — whenever the question cannot be asked: no
    stores, no cluster, a snapshot that is not a mapping, or a sampler that refuses its inputs.
    An empty map explores nobody, which is the direction this fails in.
    """
    ordered: list[str] = []
    for store in stores or ():
        name = _text(store)
        if name and name not in ordered:
            ordered.append(name)
    cluster = _text(cluster_id)
    if not ordered or not cluster:
        return {}
    snapshot = trust_snapshot if isinstance(trust_snapshot, Mapping) else {}

    try:
        state = initial_state(ordered, [cluster], snapshot, {"exploration_floor": _floor_of(book)})
    except (TypeError, ValueError):
        return {}

    learned = _learned(book)
    if learned is not None:
        carried = learned.posteriors.get(cluster, {})
        if carried:
            seeded = dict(state.posteriors[cluster])
            for store_id in ordered:
                kept = carried.get(store_id)
                if kept is not None:
                    seeded[store_id] = kept
            state = replace(state, posteriors={cluster: seeded})

    try:
        return exposure(state, cluster, seed)
    except (TypeError, ValueError):
        return {}


def is_low_data(row: Any) -> bool:
    """Whether the exchange's own trust snapshot POSITIVELY marks this candidate low-data.

    Read off the rank row's ``trust_summary``, which :func:`exchange.ranking.shortlist.trust_summary`
    built from the same snapshot row the ranker scored the candidate against — so the flag the
    slice is granted on is the flag the buyer is shown, not a second read that could disagree
    with it.

    Fail-closed: a summary with no ``low_data`` key, a ``None`` under it, and a store the snapshot
    holds no row for are all *not* low-data. R12's rule is that an unanswered question denies, and
    on this side of it the denial is "no slice", because a slice granted on silence is taken out
    of the mouth of a store the platform actually knows is new.
    """
    summary = row.get("trust_summary") if isinstance(row, Mapping) else None
    if not isinstance(summary, Mapping):
        return False
    flag = summary.get("low_data")
    return flag is True


def plan_exploration(
    pool: Sequence[Mapping[str, Any]],
    bench: Sequence[Mapping[str, Any]],
    shares: Mapping[str, float] | Callable[[Sequence[str]], Mapping[str, float]],
) -> ExplorationSlice | None:
    """Which bench candidate takes the last pool slot, or ``None`` to change nothing.

    Args:
        pool: the rows that WOULD fill the shortlist, in rank order — one per store, already
            capped at the slot count by :func:`exchange.ranking.shortlist.slot_pool`.
        bench: the eligible rows that would not, in rank order, one per store.
        shares: this auction's exposure map, or — **on a served path, always** — a callable that
            takes the store ids to sample and produces it. See "What the read costs" below for
            why the served caller must pass the callable and never a precomputed map.

    Returns:
        ``None`` on every auction where the bound above says exploration takes nothing — and
        ``None`` is the answer that leaves the shortlist byte-identical to what it was before this
        module existed, which is what makes every existing served auction unaffected.

    What the read costs, and the two bounds on it
    ---------------------------------------------
    The sampler draws :data:`~.bandit.DEFAULT_DRAWS` (512) joint samples with one
    ``betavariate`` per store per draw, and it runs inside R10's synchronous window. Measured on
    this tree: ``exposure_shares`` costs 2.5 ms over 5 stores, 24 ms over 50 and **250 ms over
    500** — and ``MAX_ROSTER_ENTRIES`` is 500, so the naive version put a quarter of a second on
    every large auction, including the overwhelming majority that explore nothing.

    * **Every cheap structural clause is decided first**, so the sampler is reached only when
      there is genuinely a low-data candidate below the cut.
    * **Only the stores that could decide the slot are sampled** — the pool the challenger would
      join, and the challengers themselves, capped at
      :data:`MAX_SAMPLED_CHALLENGERS`. A bench candidate the trust snapshot does not mark
      ``low_data`` can never take this slot, so putting it in the joint draw costs time to
      answer a question nobody asked.

    Measured end to end over 500 candidates with both bounds in place: ``rank_auction`` costs
    **18 ms** with nothing to explore, **20 ms** with one low-data challenger and **52 ms** with
    all 500 low-data — against **330 ms** for the draft that sampled the whole roster first.

    **What the cap gives up, said plainly:** with more than :data:`MAX_SAMPLED_CHALLENGERS`
    low-data stores below the cut, the ones past the cap are not in this auction's draw. They are
    dropped in RANK order, so what survives is the best-ranked of them, and the next auction has
    a different roster and a different seed — the guarantee is over auctions, not within one.
    """
    if not pool or not bench or len(pool) < EXPLORATION_SLOTS:
        return None
    # The slice is already spent. Not "a newcomer is in the pool by luck" — that IS the exposure
    # this buys, and buying it twice would cost a second slot for nothing.
    if any(is_low_data(row) for row in pool):
        return None

    challengers = [row for row in bench if is_low_data(row)][:MAX_SAMPLED_CHALLENGERS]
    if not challengers:
        return None

    # The ONLY place the sampler is reached, and the line above is the last cheap one.
    sampled = (
        shares([_text(row.get("store_id")) for row in (*pool, *challengers)])
        if callable(shares)
        else shares
    )
    if not isinstance(sampled, Mapping):
        return None

    def share_of(row: Mapping[str, Any]) -> float:
        raw = sampled.get(_text(row.get("store_id")), 0.0)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) else 0.0

    # Highest sampled share wins; the ranking's own order breaks a draw. Indexed by POSITION
    # rather than by `list.index`, which compares rows by equality — two candidates whose rows
    # happen to be equal mappings would resolve to the same tie-break and make the choice depend
    # on which one the fan-out answered first.
    best = max(enumerate(challengers), key=lambda pair: (share_of(pair[1]), -pair[0]))[1]
    chosen, share = best, share_of(best)
    # 0.0 means banned and nothing else — the bandit's own published contract for that value.
    # It is reachable here for a store the trust snapshot holds no row for, which `initial_state`
    # treats as blacklisted; the ranker's filters would normally have excluded such a candidate
    # already, and this is the second lock rather than the first.
    if not math.isfinite(share) or share <= 0.0:
        return None

    displaced = pool[-1]
    return ExplorationSlice(
        store_id=_text(chosen.get("store_id")),
        bid_id=_text(chosen.get("bid_id")),
        exposure_share=share,
        displaced_store_id=_text(displaced.get("store_id")),
        displaced_bid_id=_text(displaced.get("bid_id")),
    )
