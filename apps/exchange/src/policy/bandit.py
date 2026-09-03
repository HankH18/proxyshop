"""Contextual Thompson sampling over intent-cluster x store (R16, R12, S4).

What this decides, and what it deliberately does not
----------------------------------------------------
This module decides **exposure**: what share of a cluster's shortlist opportunity each
store draws. It never touches rank. DESIGN publishes exactly one rank formula
(``w_m*intent_match + w_e*verified_claim_ratio + w_t*trust + w_v*price_value +
w_d*delivery_fit - policy_penalties``) and the bandit adjusts exposure and exploration
only -- so nothing here reads a bid, a price, or a fee.

The model
---------
One Beta-Bernoulli posterior per ``(cluster, store)`` pair over the conversion event.
:func:`initial_state` seeds each pair from the trust snapshot -- a store the trust system
already rates well starts with a mildly optimistic prior, scaled by the snapshot's own
``confidence`` so a confident 0.9 counts for more than a hesitant one. The prior is
deliberately weak (at most :data:`PRIOR_WEIGHT` pseudo-observations) because conversion
is the bandit's own signal and trust is only where it starts looking.

:func:`update` folds conversion outcomes into the posterior of the cluster they happened
in, and nowhere else: an outcome in ``cluster-1`` cannot move ``cluster-2``'s exposure.

:func:`exposure` runs the sampler. For a fixed seed it draws
:data:`DEFAULT_DRAWS` joint samples, one theta per store per draw, and gives each store
the fraction of draws it came out on top -- the standard probability-of-being-best
estimate. The estimate is Laplace-smoothed, so a store the bandit has soured on is
starved rather than silenced, and **zero exposure means exactly one thing: banned**.

The order of the two overrides, which is the whole trick
--------------------------------------------------------
Two R12 rules sit on top of the sampled shares, and they are applied in this order:

1. **The exploration floor, per eligible low-data store.** Every store the trust snapshot
   marks ``low_data`` and does not blacklist is lifted to at least
   ``config["exploration_floor"]``. It is a floor *each* such store gets, not one slice
   they divide: with a floor of 0.25 and one low-data store among five, that store takes
   0.25 and the other four share the remaining 0.75; with two low-data stores they take
   0.25 *each* and the rest share 0.50. The lift is a water-filling loop, so pinning one
   store cannot push a second low-data store back under the floor.

   The slice is bounded twice. A blacklisted store claims none of it -- budgeting
   exploration for a store that can never be exposed only starves an eligible new one --
   and the pinned mass as a whole is capped at :data:`EXPLORATION_BUDGET` whenever any
   eligible store has a record to exploit, falling back to ``1/n`` of the cluster when
   nobody is exploiting. Both caps exist for the same reason: ``len(low_data) * floor >=
   1`` used to leave every established store at *exactly* 0.0, and 0.0 is the value this
   module reserves for "banned".

2. **The blacklist, and it never gives anything back.** Every store whose eligibility
   answer is anything other than an explicit "not blacklisted" is set to exactly 0.0, and
   the survivors renormalize over what is left. Running this second is what makes a
   blacklisted low-data store report 0.0 instead of the exploration floor; re-running the
   floor afterwards would hand the slice straight back to the store the blacklist just
   removed, which is the bug this ordering exists to prevent.

The eligibility answer is **fail-closed** (R12): a store missing from the snapshot, a
record that is ``None``, a missing ``blacklisted`` key and a ``blacklisted`` of ``None``
are all treated as blacklisted. "We could not tell" is never "let it through".

Determinism
-----------
``exposure(state, cluster_id, seed)`` is a pure function of its three arguments. The RNG
is seeded from a BLAKE2b digest of ``(seed, cluster_id)`` rather than from :func:`hash`,
whose string hashing is salted per process and would make the same seed mean different
things in two runs. S4 asks for simulation assertions at fixed seeds; this is what makes
them mean something.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "DEFAULT_DRAWS",
    "EXPLORATION_BUDGET",
    "PRIOR_WEIGHT",
    "BanditState",
    "Posterior",
    "exposure",
    "initial_state",
    "update",
]

#: Joint samples drawn per :func:`exposure` call. High enough that the probability-of-best
#: estimate is stable to well under a percentage point, low enough to stay sub-millisecond.
DEFAULT_DRAWS = 512

#: Maximum pseudo-observations the trust snapshot may contribute to a starting posterior,
#: before the snapshot's own ``confidence`` scales it down.
PRIOR_WEIGHT = 4.0

#: Most of a cluster the exploration floor may pin, when there is any eligible store with a
#: record to exploit. Exploration that takes 100% is not exploration, it is a lottery: the
#: stores that earned their exposure would each be left at exactly 0.0, the value this
#: module reserves for "banned". The remaining tenth is shared out by the sampler, so a
#: store with a real conversion record is never mistaken for a banned one.
EXPLORATION_BUDGET = 0.9

#: Tolerance for the water-filling comparison, so float noise cannot re-pin a store that
#: is already sitting exactly on the floor.
_EPS = 1e-12

_MISSING = object()


@dataclass(frozen=True)
class Posterior:
    """A Beta posterior over one store's conversion rate in one cluster."""

    alpha: float
    beta: float

    def won(self) -> Posterior:
        """This pair after one conversion."""
        return Posterior(self.alpha + 1.0, self.beta)

    def lost(self) -> Posterior:
        """This pair after one non-conversion."""
        return Posterior(self.alpha, self.beta + 1.0)


@dataclass(frozen=True)
class BanditState:
    """Everything :func:`exposure` needs. Treat as immutable -- :func:`update` copies."""

    stores: tuple[str, ...]
    clusters: tuple[str, ...]
    posteriors: dict[str, dict[str, Posterior]]
    blacklisted: frozenset[str]
    low_data: frozenset[str]
    exploration_floor: float
    draws: int


# --- tolerant record access ---------------------------------------------------------
def _read(record: Any, key: str, default: Any = _MISSING) -> Any:
    """Read ``key`` off a mapping or an object. Callers decide what absence means."""
    if record is None:
        return default
    if isinstance(record, Mapping):
        return record[key] if key in record else default
    if hasattr(record, key):
        return getattr(record, key)
    return default


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _is_blacklisted(record: Any) -> bool:
    """R12, fail-closed: anything short of an explicit "no" is a yes.

    A store absent from the snapshot, a ``None`` record, an absent ``blacklisted`` key and
    a ``blacklisted`` of ``None`` all mean the eligibility question went unanswered, and an
    unanswered eligibility question denies participation.
    """
    if record is None or record is _MISSING:
        return True
    answer = _read(record, "blacklisted", _MISSING)
    if answer is _MISSING or answer is None:
        return True
    return bool(answer)


def _prior(record: Any) -> Posterior:
    """The starting posterior for one pair, seeded from the trust snapshot."""
    raw_score = _read(record, "score", None)
    score = _clamp01(float(raw_score)) if raw_score is not None else 0.5
    raw_confidence = _read(record, "confidence", None)
    confidence = _clamp01(float(raw_confidence)) if raw_confidence is not None else 0.0
    weight = PRIOR_WEIGHT * confidence
    return Posterior(1.0 + weight * score, 1.0 + weight * (1.0 - score))


def _unique(values: Iterable[str], label: str) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        name = str(value)
        if name in out:
            raise ValueError(f"duplicate {label} {name!r}")
        out.append(name)
    if not out:
        raise ValueError(f"at least one {label} is required")
    return tuple(out)


# --- the public surface -------------------------------------------------------------
def initial_state(
    stores: Sequence[str],
    clusters: Sequence[str],
    trust_snapshot: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> BanditState:
    """Build the bandit's starting state for ``stores`` across ``clusters``.

    Args:
        stores: every store the policy may expose, in a stable order. The order is the
            tie-break when two sampled thetas land on the same value, so it is part of
            the determinism contract.
        clusters: the intent clusters exposure is decided within.
        trust_snapshot: ``{store_id: record}``, where a record carries ``score``,
            ``confidence``, ``blacklisted`` and ``low_data``. A store missing from here
            is treated as blacklisted (R12 fail-closed).
        config: ``exploration_floor`` (default 0.0) and, optionally, ``draws``.

    Returns:
        A :class:`BanditState` whose every ``(cluster, store)`` posterior is that store's
        trust-seeded prior. Clusters start identical; only outcomes separate them.

    Raises:
        ValueError: on an empty or duplicated store/cluster list, or a floor outside
            ``[0, 1]``.
    """
    store_ids = _unique(stores, "store")
    cluster_ids = _unique(clusters, "cluster")
    config = config or {}

    raw_floor = _read(config, "exploration_floor", 0.0)
    floor = 0.0 if raw_floor is None else float(raw_floor)
    if not 0.0 <= floor <= 1.0:
        raise ValueError(f"exploration_floor must be in [0, 1], got {floor!r}")

    raw_draws = _read(config, "draws", DEFAULT_DRAWS)
    draws = DEFAULT_DRAWS if raw_draws is None else int(raw_draws)
    if draws < 1:
        raise ValueError(f"draws must be at least 1, got {draws!r}")

    # Store ids are stringified by `_unique`, so the snapshot is keyed the same way before
    # lookup. Without this a snapshot keyed by, say, the int 5 misses the store id "5" and
    # the store is fail-closed banned -- the right default, reached for the wrong reason.
    raw_snapshot: Mapping[Any, Any] = trust_snapshot or {}
    snapshot = {str(key): value for key, value in raw_snapshot.items()}
    records = {sid: _read(snapshot, sid, _MISSING) for sid in store_ids}

    blacklisted = frozenset(sid for sid in store_ids if _is_blacklisted(records[sid]))
    low_data = frozenset(
        sid for sid in store_ids if bool(_read(records[sid], "low_data", False) or False)
    )
    priors = {sid: _prior(None if records[sid] is _MISSING else records[sid]) for sid in store_ids}

    return BanditState(
        stores=store_ids,
        clusters=cluster_ids,
        posteriors={cid: dict(priors) for cid in cluster_ids},
        blacklisted=blacklisted,
        low_data=low_data,
        exploration_floor=floor,
        draws=draws,
    )


def update(state: BanditState, outcomes: Iterable[Mapping[str, Any]]) -> BanditState:
    """Fold conversion outcomes into a **copy** of ``state`` and return it.

    Args:
        state: the state to build on. It is never mutated, so a caller holding the old
            state keeps the exposure it used to report -- which is what lets a simulation
            compare before and after honestly.
        outcomes: records carrying ``store_id``, ``cluster_id`` and ``converted``. Each
            lands only in the posterior of its own ``(cluster, store)`` pair.

    Returns:
        A new :class:`BanditState`.

    Raises:
        ValueError: if an outcome names a cluster or store this state does not carry, or
            omits ``converted``. A misrouted outcome is loud on purpose: dropping it
            silently would leave a posterior wrong with nothing to show for it.
    """
    posteriors = {cid: dict(per_store) for cid, per_store in state.posteriors.items()}

    for index, outcome in enumerate(outcomes):
        cluster_id = _read(outcome, "cluster_id", _MISSING)
        if cluster_id is _MISSING or cluster_id is None:
            raise ValueError(f"outcome {index} carries no cluster_id")
        cluster_id = str(cluster_id)
        if cluster_id not in posteriors:
            raise ValueError(
                f"outcome {index}: unknown cluster {cluster_id!r}; "
                f"this state covers {sorted(posteriors)}"
            )

        store_id = _read(outcome, "store_id", _MISSING)
        if store_id is _MISSING or store_id is None:
            raise ValueError(f"outcome {index} carries no store_id")
        store_id = str(store_id)
        if store_id not in posteriors[cluster_id]:
            raise ValueError(
                f"outcome {index}: unknown store {store_id!r}; "
                f"this state covers {sorted(posteriors[cluster_id])}"
            )

        converted = _read(outcome, "converted", _MISSING)
        if converted is _MISSING or converted is None:
            raise ValueError(
                f"outcome {index} for store {store_id!r} carries no converted flag; "
                f"an outcome with no result is not an outcome"
            )

        pair = posteriors[cluster_id][store_id]
        posteriors[cluster_id][store_id] = pair.won() if bool(converted) else pair.lost()

    return replace(state, posteriors=posteriors)


def exposure(state: BanditState, cluster_id: str, seed: Any) -> dict[str, float]:
    """Return ``{store_id: share}`` for ``cluster_id`` at ``seed``.

    Shares are non-negative and sum to 1.0 whenever the cluster has at least one eligible
    store. A share of exactly 0.0 means blacklisted and nothing else.

    Args:
        state: the state to read. Never modified.
        cluster_id: the intent cluster to decide exposure within.
        seed: anything stringifiable. The same seed always yields the same shares for the
            same state and cluster, across processes and machines.

    Raises:
        ValueError: if ``cluster_id`` is not one of this state's clusters. An empty map
            would read as "nobody gets exposure", which is a different fact.
    """
    key = str(cluster_id)
    if key not in state.posteriors:
        raise ValueError(f"unknown cluster {key!r}; this state covers {sorted(state.posteriors)}")

    order = state.stores
    sampled = _probability_of_best(state.posteriors[key], order, _rng(key, seed), state.draws)
    floored = _apply_exploration_floor(
        sampled, order, state.low_data, state.blacklisted, state.exploration_floor
    )
    return _apply_blacklist(floored, order, state.blacklisted)


# --- the sampler and the two overrides ----------------------------------------------
def _rng(cluster_id: str, seed: Any) -> random.Random:
    """A generator seeded reproducibly from ``(seed, cluster_id)``.

    BLAKE2b rather than :func:`hash`: string hashing is salted per interpreter, so a
    ``hash``-derived seed would silently mean something different on the next run and the
    fixed-seed assertions S4 asks for would be worthless.
    """
    material = f"{seed}\x00{cluster_id}".encode()
    digest = hashlib.blake2b(material, digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _probability_of_best(
    posteriors: Mapping[str, Posterior],
    order: Sequence[str],
    rng: random.Random,
    draws: int,
) -> dict[str, float]:
    """Estimate each store's probability of having the best conversion rate.

    One joint draw samples a theta per store and credits the argmax. Ties go to the
    earlier store in ``order``, which is why that order is part of the contract.

    The counts are Laplace-smoothed before they become shares, so every store in the
    roster keeps a positive estimate however badly it has done. A store the bandit has
    given up on should be starved, not silenced -- and it leaves exactly 0.0 free to mean
    "blacklisted", which is the one thing the caller must be able to read off unambiguously.
    """
    wins = dict.fromkeys(order, 0)
    for _ in range(draws):
        best: str | None = None
        best_value = -1.0
        for store_id in order:
            pair = posteriors[store_id]
            value = rng.betavariate(pair.alpha, pair.beta)
            if value > best_value:
                best_value = value
                best = store_id
        if best is not None:
            wins[best] += 1

    total = float(draws + len(order))
    return {store_id: (wins[store_id] + 1.0) / total for store_id in order}


def _apply_exploration_floor(
    shares: Mapping[str, float],
    order: Sequence[str],
    low_data: frozenset[str],
    blacklisted: frozenset[str],
    floor: float,
) -> dict[str, float]:
    """Lift every eligible low-data store to at least ``floor`` (R12's exploration slice).

    The floor is per low-data store, not one slice divided among them. Water-filling:
    pin the stores that fall short, rescale what is left over the rest, and look again --
    because pinning one store shrinks the pool and can drop a second low-data store under
    the floor it had cleared a moment ago.

    Two things bound the slice, and both exist because without them the guarantee eats the
    cluster:

    **Blacklisted stores claim no slice.** A banned store can never be exposed, so
    budgeting exploration for it buys nothing and takes the slice out of an eligible new
    store's mouth. Reading eligibility here is not "applying the blacklist first": the
    banned store still competes in the sampled draw above and is still reported, at 0.0, by
    :func:`_apply_blacklist` below -- and the slice is still never handed back to it.

    **Exploration never takes the whole cluster.** If exploitation has any eligible store at
    all, the pinned mass is capped at :data:`EXPLORATION_BUDGET`, so the stores with a real
    record keep a positive share between them. Without that cap, ``len(low_data) * floor >=
    1`` left every established store at *exactly* 0.0 -- which is the value this module
    reserves for "banned", so ten new stores at a 0.10 floor could silently retire three
    stores with six hundred conversions apiece. The per-store floor is only reduced when it
    genuinely cannot be paid: nine low-data stores at 0.10 still get 0.10 each.

    When no eligible store is exploiting, the budget is the whole cluster and the floor is
    capped at ``1/len(lows)`` -- a guarantee that cannot be met for everyone it names is
    better reported as an equal split than as a number that does not add up.
    """
    lows = [store_id for store_id in order if store_id in low_data and store_id not in blacklisted]
    if floor <= 0.0 or not lows:
        return dict(shares)

    exploiting = [
        store_id for store_id in order if store_id not in blacklisted and store_id not in low_data
    ]
    budget = EXPLORATION_BUDGET if exploiting else 1.0
    effective = min(floor, budget / len(lows))
    entitled = frozenset(lows)
    pinned: dict[str, float] = {}

    while True:
        free = [store_id for store_id in order if store_id not in pinned]
        if not free:
            break
        remaining = 1.0 - sum(pinned.values())
        free_mass = sum(shares[store_id] for store_id in free)
        if free_mass > 0.0:
            scale = remaining / free_mass
            short = [
                store_id
                for store_id in free
                if store_id in entitled and shares[store_id] * scale < effective - _EPS
            ]
        else:
            even = remaining / len(free)
            short = [
                store_id for store_id in free if store_id in entitled and even < effective - _EPS
            ]
        if not short:
            break
        for store_id in short:
            pinned[store_id] = effective

    out = dict(pinned)
    free = [store_id for store_id in order if store_id not in pinned]
    if free:
        remaining = 1.0 - sum(pinned.values())
        free_mass = sum(shares[store_id] for store_id in free)
        if free_mass > 0.0:
            for store_id in free:
                out[store_id] = shares[store_id] / free_mass * remaining
        else:
            for store_id in free:
                out[store_id] = remaining / len(free)
    return {store_id: out[store_id] for store_id in order}


def _apply_blacklist(
    shares: Mapping[str, float],
    order: Sequence[str],
    blacklisted: frozenset[str],
) -> dict[str, float]:
    """Zero every blacklisted store and renormalize over the survivors.

    Applied **after** the exploration floor, and it does not re-run it. A blacklisted store
    still carried its sampled share into the floor step; zeroing it here is what makes it
    report 0.0, and its mass goes to the survivors in proportion to what they already held
    -- never back to the store just removed.

    Blacklisted stores stay in the result at 0.0 rather than disappearing, so a caller can
    tell "banned" from "not in this cluster's roster".

    Note for anyone changing the floor above: this renormalization is unconditional, so it
    will quietly rescale a malformed floor result -- shares summing to 1.6, or to 0.0 --
    into something that looks well-formed. That is why ``test_bandit.py`` asserts the
    floor's own output is a distribution rather than only checking what comes out here.
    """
    out = {
        store_id: (0.0 if store_id in blacklisted else float(shares[store_id]))
        for store_id in order
    }
    survivors = [store_id for store_id in order if store_id not in blacklisted]
    total = sum(out[store_id] for store_id in survivors)
    if total > 0.0:
        for store_id in survivors:
            out[store_id] = out[store_id] / total
    return out
