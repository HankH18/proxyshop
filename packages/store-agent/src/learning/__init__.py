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

from .arms import (
    DEFAULT_PITCH_VARIANT,
    PITCH_VARIANTS,
    VARIANT_KIND,
    Arm,
    arm_from_action,
    available_commitments,
    cold_arm,
    commitment_keys,
    is_variant,
    variant_or_default,
)
from .grid import (
    DEFAULT_DEPTH_BUCKETS,
    FRACTION_CEILING,
    PERCENT_PER_UNIT,
    as_fraction,
    as_percent,
    bucket_index,
    percent_as_fraction,
)
from .outcomes import OUTCOME_SOURCE, outcome_row, verdict
from .prior import (
    CONTEXT_PRIOR_FIELDS,
    DISCOUNT_MARKER,
    PRIOR_RECORD_FIELDS,
    ClusterPrior,
    NetworkPrior,
    Tally,
    best_label,
    build_network_prior,
    cluster_prior,
    from_context_priors,
    prior_view,
    reject_discount_fields,
    scrub_context_prior,
    to_context_priors,
)
from .state import (
    COMMITMENT_SALT,
    DEPTH_FRACTION_FIELDS,
    DEPTH_PERCENT_FIELDS,
    OUTCOME_DEPTH_FIELDS,
    OUTCOME_VARIANT_FIELDS,
    POLICY_VERSION_PREFIX,
    PRIOR_LOSSES,
    PRIOR_WINS,
    VARIANT_SALT,
    ClusterLearning,
    DepthTally,
    StoreLearningState,
    best_depth,
    best_variant,
    depth_weights,
    initial_state,
    policy_for_auction,
    sample_arm,
    sample_commitment_set,
    sample_depth,
    sample_variant,
    to_learned_policy,
    update,
    variant_weights,
)

__all__ = [
    "COMMITMENT_SALT",
    "CONTEXT_PRIOR_FIELDS",
    "DEFAULT_DEPTH_BUCKETS",
    "DEFAULT_PITCH_VARIANT",
    "DEPTH_FRACTION_FIELDS",
    "DEPTH_PERCENT_FIELDS",
    "DISCOUNT_MARKER",
    "FRACTION_CEILING",
    "OUTCOME_DEPTH_FIELDS",
    "OUTCOME_SOURCE",
    "OUTCOME_VARIANT_FIELDS",
    "PERCENT_PER_UNIT",
    "PITCH_VARIANTS",
    "POLICY_VERSION_PREFIX",
    "PRIOR_LOSSES",
    "PRIOR_RECORD_FIELDS",
    "PRIOR_WINS",
    "VARIANT_KIND",
    "VARIANT_SALT",
    "Arm",
    "ClusterLearning",
    "ClusterPrior",
    "DepthTally",
    "NetworkPrior",
    "StoreLearningState",
    "Tally",
    "arm_from_action",
    "as_fraction",
    "as_percent",
    "available_commitments",
    "best_depth",
    "best_label",
    "best_variant",
    "bucket_index",
    "build_network_prior",
    "cluster_prior",
    "cold_arm",
    "commitment_keys",
    "depth_weights",
    "from_context_priors",
    "initial_state",
    "is_variant",
    "outcome_row",
    "percent_as_fraction",
    "policy_for_auction",
    "prior_view",
    "reject_discount_fields",
    "sample_arm",
    "sample_commitment_set",
    "sample_depth",
    "sample_variant",
    "scrub_context_prior",
    "to_context_priors",
    "to_learned_policy",
    "update",
    "variant_or_default",
    "variant_weights",
    "verdict",
]
