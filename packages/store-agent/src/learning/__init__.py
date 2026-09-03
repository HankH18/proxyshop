"""T-042 — the store agent's learning loop (R17).

Four verbs, and one line drawn through the middle of them:

    prior  = build_network_prior(cross_store_pitch_outcomes)   # pitches pool
    state  = initial_state(prior, store_id="store-alpha")      # depth does not
    state  = update(state, this_store_s_own_outcomes)          # own rows only
    depth  = sample_depth(state, cluster_id, seed)             # a fraction, seeded

**The line.** What a *pitch* wins is an observation about buyers and pools across stores. What a
*discount* buys is a fact about one store's margins and does not — pooling it would move a price
signal between competitors through the platform. So :func:`build_network_prior` is structurally
blind to discount fields (it reads a named allowlist and never enumerates a record), while depth
is learned per store from that store's own outcomes and starts flat no matter how much the
network knows.

**Independence is structural.** A :class:`~store_agent.learning.state.StoreLearningState` is a
frozen dataclass of tuples, ints and strs, recursively. Two stores seeded from one prior share
that prior object deliberately — it is frozen too — and there is no mutable structure anywhere
for one store's update to reach the other through. This module holds no registry, no cache and
no global; per-store state lives in the value the caller holds.

**Units.** Depth is a FRACTION here and in outcome records (`0.2`); it is a PERCENT in the
envelope and in `learned_policy` (`20.0`). :func:`~store_agent.learning.grid.as_percent` is the
only crossing, and :func:`~store_agent.learning.state.to_learned_policy` is the only caller.

Offline and clock-free: `sample_depth` derives its generator from `(cluster_id, seed)` by hash,
so it reproduces across processes (S4), and nothing here touches a network, an LLM or a clock.
"""

from __future__ import annotations

from .grid import (
    DEFAULT_DEPTH_BUCKETS,
    FRACTION_CEILING,
    PERCENT_PER_UNIT,
    as_fraction,
    as_percent,
    bucket_index,
    percent_as_fraction,
)
from .prior import (
    DISCOUNT_MARKER,
    PRIOR_RECORD_FIELDS,
    ClusterPrior,
    NetworkPrior,
    Tally,
    best_label,
    build_network_prior,
    cluster_prior,
    prior_view,
    reject_discount_fields,
    to_context_priors,
)
from .state import (
    DEPTH_FRACTION_FIELDS,
    DEPTH_PERCENT_FIELDS,
    OUTCOME_DEPTH_FIELDS,
    POLICY_VERSION_PREFIX,
    PRIOR_LOSSES,
    PRIOR_WINS,
    ClusterLearning,
    DepthTally,
    StoreLearningState,
    best_depth,
    depth_weights,
    initial_state,
    sample_depth,
    to_learned_policy,
    update,
)

__all__ = [
    "DEFAULT_DEPTH_BUCKETS",
    "DEPTH_FRACTION_FIELDS",
    "DEPTH_PERCENT_FIELDS",
    "DISCOUNT_MARKER",
    "FRACTION_CEILING",
    "OUTCOME_DEPTH_FIELDS",
    "PERCENT_PER_UNIT",
    "POLICY_VERSION_PREFIX",
    "PRIOR_LOSSES",
    "PRIOR_RECORD_FIELDS",
    "PRIOR_WINS",
    "ClusterLearning",
    "ClusterPrior",
    "DepthTally",
    "NetworkPrior",
    "StoreLearningState",
    "Tally",
    "as_fraction",
    "as_percent",
    "best_depth",
    "best_label",
    "bucket_index",
    "build_network_prior",
    "cluster_prior",
    "depth_weights",
    "initial_state",
    "percent_as_fraction",
    "prior_view",
    "reject_discount_fields",
    "sample_depth",
    "to_context_priors",
    "to_learned_policy",
    "update",
]
