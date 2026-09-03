"""One store's learned discount-depth policy: how it is held, updated, and sampled.

The three promises of R17 pull against each other, and the design is the shape that satisfies
all three at once rather than two of them plus a convention.

**A store's depth moves toward its OWN winners.** Each cluster carries a win/loss tally per rung
of the depth grid, and :func:`sample_depth` is Thompson sampling over those tallies: draw a
plausible win rate for each rung from its Beta posterior and take the depth that drew best. With
no evidence every rung is Beta(1, 1) and the draw is a uniform pick over the grid, which is the
right cold behaviour — the loop explores before it has anything to exploit. Forty wins at 20%
and forty losses at 0% make the 20% rung Beta(41, 1) and the 0% rung Beta(1, 41), and the
sampler concentrates there without ever being told to.

**Two stores learn independently — structurally.** :class:`StoreLearningState` is a frozen
dataclass whose every field is a tuple, an int, a str, or another frozen dataclass. There is no
list, dict or set anywhere inside one, so there is nothing for two stores to share even when
they are seeded from the same :class:`~store_agent.learning.prior.NetworkPrior` object — which
they are, deliberately, because that object is frozen too. :func:`update` returns a new state
and cannot do otherwise; the module holds no registry, no cache and no global. Independence
that depends on callers copying carefully is independence until the first caller forgets.

**"Its own outcomes."** A state may be bound to a `store_id`, and a bound state drops every row
that names a different store — including rows mixed into the same batch as its own. Enforcing
that here rather than trusting the caller's query is the difference between a criterion and a
comment.

Nothing here reads a clock, a network or an unseeded random source: `sample_depth` derives its
seed from `(cluster_id, seed)` by hash, so it is a pure function of its arguments and reproduces
across processes.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .grid import (
    DEFAULT_DEPTH_BUCKETS,
    as_fraction,
    as_percent,
    bucket_index,
    percent_as_fraction,
)
from .prior import NetworkPrior, Tally, best_label, cluster_prior, field

#: Beta(1, 1) — a flat posterior on every rung before the store has seen anything. Uniform
#: exploration over the grid, and the reason a cold store's mean sampled depth is the middle
#: of the grid rather than zero.
PRIOR_WINS = 1.0
PRIOR_LOSSES = 1.0

#: Outcome-row fields carrying a depth as a FRACTION (`0.2`), tried in order.
DEPTH_FRACTION_FIELDS: tuple[str, ...] = ("discount_depth",)

#: Outcome-row fields carrying a depth as a PERCENT (`20.0`), tried after the fraction fields.
#:
#: Split from the fraction fields on purpose. Reading both through one "guess the unit" helper
#: turns `discount_pct: 0.5` — half a percent off — into a 50% discount in the tally, because
#: 0.5 is a perfectly plausible fraction. The field name states the unit; there is nothing to
#: infer, so nothing infers.
DEPTH_PERCENT_FIELDS: tuple[str, ...] = ("discount_pct",)

#: Every field an outcome row's depth may be read from. Unlike the prior's allowlist this one
#: DOES include discount evidence: a store learning its own elasticity from its own outcomes is
#: the whole point of the loop. The wall is that these rows never cross a store boundary.
OUTCOME_DEPTH_FIELDS: tuple[str, ...] = DEPTH_FRACTION_FIELDS + DEPTH_PERCENT_FIELDS

#: Prefix of the policy version a rendered `learned_policy` reports.
POLICY_VERSION_PREFIX = "learned"


@dataclass(frozen=True)
class DepthTally:
    """One rung of the depth grid, and how this store has fared at it."""

    depth: float
    wins: int
    losses: int

    @property
    def observations(self) -> int:
        return self.wins + self.losses

    @property
    def posterior_mean(self) -> float:
        """The Beta(wins + 1, losses + 1) mean — 0.5 with no evidence, never 0 or 1."""
        return (self.wins + PRIOR_WINS) / (self.wins + self.losses + PRIOR_WINS + PRIOR_LOSSES)


@dataclass(frozen=True)
class ClusterLearning:
    """What one store has learned about one cluster."""

    cluster_id: str
    depths: tuple[DepthTally, ...]
    commitments: tuple[Tally, ...]
    observations: int
    wins: int


@dataclass(frozen=True)
class StoreLearningState:
    """One store's learned state. Immutable all the way down; see the module docstring.

    Attributes:
        store_id: the store these outcomes belong to, or ``None`` for an unbound state that
            accepts whatever it is fed. A bound state filters foreign rows out.
        depth_buckets: the candidate depths, as fractions. A constant grid, not evidence.
        prior: the shared, frozen cross-store pitch prior. It never carries depth evidence.
        clusters: per-cluster depth and commitment tallies, sorted by cluster id.
        observations: how many of this store's own rows have been folded in.
    """

    store_id: str | None
    depth_buckets: tuple[float, ...]
    prior: NetworkPrior
    clusters: tuple[ClusterLearning, ...]
    observations: int

    def cluster(self, cluster_id: str) -> ClusterLearning | None:
        wanted = str(cluster_id)
        for learned in self.clusters:
            if learned.cluster_id == wanted:
                return learned
        return None


def initial_state(
    prior: NetworkPrior,
    store_id: str | None = None,
    depth_buckets: Sequence[float] = DEFAULT_DEPTH_BUCKETS,
) -> StoreLearningState:
    """Seed a store from the network prior. Two calls with one prior give equal, separate states.

    The prior contributes pitch evidence and NOT depth evidence: every rung starts flat. That is
    R17's line drawn in the constructor — a store that has run no auctions has learned nothing
    about its own elasticity no matter how much the network knows about everyone else's.
    """
    return StoreLearningState(
        store_id=str(store_id) if store_id is not None else None,
        depth_buckets=tuple(float(b) for b in depth_buckets),
        prior=prior,
        clusters=(),
        observations=0,
    )


def _is_own(state: StoreLearningState, record: Any) -> bool:
    """R17: a bound state learns from its own rows only, whatever else is in the batch."""
    if state.store_id is None:
        return True
    row_store = field(record, "store_id")
    return row_store is not None and str(row_store) == state.store_id


def _row_depth(record: Any) -> float | None:
    """The row's discount depth as a fraction, or ``None`` when it states none usably.

    Each field is read in its own declared unit. A row that states no usable depth is dropped
    rather than tallied at rung zero: "we do not know what depth this was" and "this converted
    at no discount" are opposite pieces of evidence, and conflating them teaches the loop that
    zero wins.
    """
    for name in DEPTH_FRACTION_FIELDS:
        depth = as_fraction(field(record, name))
        if depth is not None:
            return depth
    for name in DEPTH_PERCENT_FIELDS:
        depth = percent_as_fraction(field(record, name))
        if depth is not None:
            return depth
    return None


def _labels(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Mapping):
        return tuple(sorted(str(k) for k in value))
    if isinstance(value, Sequence):
        out = []
        for item in value:
            label = item.get("key") if isinstance(item, Mapping) else item
            if label:
                out.append(str(label))
        return tuple(out)
    return (str(value),)


class _ClusterScratch:
    """Mutable accumulator, local to one :func:`update` call. Never escapes it."""

    __slots__ = ("cluster_id", "wins", "losses", "commitments", "observations", "won_count")

    def __init__(self, cluster_id: str, rungs: int) -> None:
        self.cluster_id = cluster_id
        self.wins = [0] * rungs
        self.losses = [0] * rungs
        self.commitments: dict[str, list[int]] = {}
        self.observations = 0
        self.won_count = 0

    @classmethod
    def of(cls, learned: ClusterLearning | None, cluster_id: str, rungs: int) -> _ClusterScratch:
        scratch = cls(cluster_id, rungs)
        if learned is None:
            return scratch
        for index, tally in enumerate(learned.depths[:rungs]):
            scratch.wins[index] = tally.wins
            scratch.losses[index] = tally.losses
        for tally in learned.commitments:
            scratch.commitments[tally.label] = [tally.wins, tally.observations]
        scratch.observations = learned.observations
        scratch.won_count = learned.wins
        return scratch

    def freeze(self, buckets: tuple[float, ...]) -> ClusterLearning:
        return ClusterLearning(
            cluster_id=self.cluster_id,
            depths=tuple(
                DepthTally(depth=buckets[i], wins=self.wins[i], losses=self.losses[i])
                for i in range(len(buckets))
            ),
            commitments=tuple(
                Tally(label=label, wins=counts[0], observations=counts[1])
                for label, counts in sorted(self.commitments.items())
            ),
            observations=self.observations,
            wins=self.won_count,
        )


def update(state: StoreLearningState, records: Any) -> StoreLearningState:
    """Fold this store's own outcomes into a NEW state. `state` is never touched.

    Args:
        state: the state to evolve. Returned unchanged in content if nothing applies.
        records: outcome rows carrying `cluster_id`, `won`, a depth (`discount_depth` as a
            fraction or `discount_pct` as a percent) and optionally `commitments`. A row from
            another store is dropped when the state is bound to one.

    Returns:
        A new frozen state. Never the same object, never a view onto the old one.
    """
    buckets = state.depth_buckets
    rungs = len(buckets)
    scratch: dict[str, _ClusterScratch] = {}
    applied = 0

    for record in records or ():
        if not _is_own(state, record):
            continue
        cluster_id = str(field(record, "cluster_id") or "")
        if not cluster_id:
            continue
        depth = _row_depth(record)
        if depth is None:
            continue
        won = bool(field(record, "won"))
        slot = scratch.get(cluster_id)
        if slot is None:
            slot = scratch[cluster_id] = _ClusterScratch.of(
                state.cluster(cluster_id), cluster_id, rungs
            )
        index = bucket_index(buckets, depth)
        if won:
            slot.wins[index] += 1
            slot.won_count += 1
        else:
            slot.losses[index] += 1
        slot.observations += 1
        for label in _labels(field(record, "commitments")):
            counts = slot.commitments.setdefault(label, [0, 0])
            counts[0] += 1 if won else 0
            counts[1] += 1
        applied += 1

    untouched = tuple(c for c in state.clusters if c.cluster_id not in scratch)
    evolved = tuple(scratch[c].freeze(buckets) for c in sorted(scratch))
    return StoreLearningState(
        store_id=state.store_id,
        depth_buckets=buckets,
        prior=state.prior,
        clusters=tuple(sorted(untouched + evolved, key=lambda c: c.cluster_id)),
        observations=state.observations + applied,
    )


def depth_weights(state: StoreLearningState, cluster_id: str) -> tuple[float, ...]:
    """The Beta posterior mean per rung. Flat 0.5s for a cluster this store has not run."""
    learned = state.cluster(cluster_id)
    if learned is None:
        flat = PRIOR_WINS / (PRIOR_WINS + PRIOR_LOSSES)
        return tuple(flat for _ in state.depth_buckets)
    return tuple(tally.posterior_mean for tally in learned.depths)


def _seed_int(cluster_id: str, seed: Any) -> int:
    """A stable integer seed from `(cluster_id, seed)`.

    Hashed rather than combined arithmetically because Python's `hash()` of a str is salted per
    process: `random.Random(("cluster", 7))` reproduces within one run and silently does not
    across two, which is exactly the kind of determinism failure S4 exists to catch.
    """
    digest = hashlib.blake2b(f"{cluster_id}\x1f{seed}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def sample_depth(state: StoreLearningState, cluster_id: str, seed: Any) -> float:
    """Thompson-sample a discount depth (a FRACTION) for `cluster_id` under `seed`.

    A pure function of `(state`'s value`, cluster_id, seed)`: two equal states sample the same
    sequence, and the same seed always gives the same rung. There is no per-instance generator
    and no ambient entropy.

    Each rung's plausible win rate is drawn from its Beta(wins + 1, losses + 1) posterior and the
    best draw wins. With no evidence that is a uniform pick over the grid; with evidence it
    concentrates on the rungs this store actually converted at, while still occasionally trying
    the others — which is what stops a loop that got lucky once from never looking again.
    """
    buckets = state.depth_buckets
    if not buckets:
        return 0.0
    learned = state.cluster(cluster_id)
    rng = random.Random(_seed_int(str(cluster_id), seed))

    best_index = 0
    best_draw = -1.0
    for index in range(len(buckets)):
        if learned is None or index >= len(learned.depths):
            wins = losses = 0
        else:
            wins, losses = learned.depths[index].wins, learned.depths[index].losses
        draw = rng.betavariate(wins + PRIOR_WINS, losses + PRIOR_LOSSES)
        if draw > best_draw:
            best_index, best_draw = index, draw
    return float(buckets[best_index])


def best_depth(state: StoreLearningState, cluster_id: str) -> float:
    """The rung this store would exploit — the highest posterior mean, no randomness."""
    buckets = state.depth_buckets
    if not buckets:
        return 0.0
    weights = depth_weights(state, cluster_id)
    best_index = 0
    for index in range(1, len(buckets)):
        if weights[index] > weights[best_index]:
            best_index = index
    return float(buckets[best_index])


def _fingerprint(state: StoreLearningState) -> str:
    """A short content digest of the learned state — equal states, equal version."""
    parts = [state.store_id or "", ",".join(f"{b:.6f}" for b in state.depth_buckets)]
    for learned in state.clusters:
        parts.append(learned.cluster_id)
        parts.extend(f"{t.depth:.6f}:{t.wins}:{t.losses}" for t in learned.depths)
        parts.extend(f"{t.label}:{t.wins}:{t.observations}" for t in learned.commitments)
    blob = "\x1f".join(parts).encode()
    return hashlib.blake2b(blob, digest_size=6).hexdigest()


def to_learned_policy(state: StoreLearningState) -> dict[str, Any]:
    """Render the state as the `learned_policy` mapping `ToolHooks` reads.

    `hooks.ToolHooks.choose_policy_action` reads `version` and
    `actions[cluster_id]{discount_pct, commitment_keys, value_prop}`, and it re-asks the envelope
    about the depth before letting it near a bid — a 25% ask against a 20% envelope fails closed.
    So this renders the store's best *intent*; the wall stays where it is.

    `discount_pct` is a PERCENT. The state holds fractions. :func:`grid.as_percent` is the one
    place that conversion happens.
    """
    clusters = {learned.cluster_id for learned in state.clusters}
    clusters.update(c.cluster_id for c in state.prior.clusters)

    actions: dict[str, Any] = {}
    for cluster_id in sorted(clusters):
        learned = state.cluster(cluster_id)
        from_network = cluster_prior(state.prior, cluster_id)
        commitments: tuple[Tally, ...] = ()
        if learned is not None and learned.commitments:
            commitments = learned.commitments
        elif from_network is not None:
            commitments = from_network.commitments
        action: dict[str, Any] = {
            "discount_pct": as_percent(best_depth(state, cluster_id)),
            "commitment_keys": sorted({t.label for t in commitments}),
        }
        value_prop = best_label(from_network.value_props) if from_network is not None else None
        if value_prop:
            action["value_prop"] = value_prop
        actions[cluster_id] = action

    return {
        "version": f"{POLICY_VERSION_PREFIX}-{state.observations}-{_fingerprint(state)}",
        "store_id": state.store_id,
        "actions": actions,
    }


__all__ = [
    "DEPTH_FRACTION_FIELDS",
    "DEPTH_PERCENT_FIELDS",
    "OUTCOME_DEPTH_FIELDS",
    "POLICY_VERSION_PREFIX",
    "PRIOR_LOSSES",
    "PRIOR_WINS",
    "ClusterLearning",
    "DepthTally",
    "StoreLearningState",
    "best_depth",
    "depth_weights",
    "initial_state",
    "sample_depth",
    "to_learned_policy",
    "update",
]
