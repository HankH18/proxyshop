"""The one published rank formula, applied.

DESIGN publishes exactly one combination (D13/D50), and `packages/contracts` publishes the
weights and the tie-break order it is applied with::

    rank_score = w_m*intent_match + w_e*verified_claim_ratio + w_t*trust
               + w_v*price_value + w_d*delivery_fit − policy_penalties

Three properties this module exists to keep, each of which is a rule the formula would
quietly stop having if the code merely multiplied five numbers together:

* **Blindness (R11).** The formula reads five features and nothing else. A network fee, a
  store tier and a store's own discount ceiling are not among them, and none of them is
  reachable from here — the feature vector below is built by naming the five, not by
  sweeping the candidate record for numbers. That is what makes two runs over the same
  candidates with different fees produce bit-identical scores rather than merely similar
  ones.
* **A missing feature is NEUTRAL, not zero.** `delivery_fit` and `verified_claim_ratio` read
  the published `when_absent` values (D13/D14). Scoring an unknown as 0 would punish every
  store the verifier has not reached yet, which is a systematic bias towards whoever was
  crawled first rather than a measurement of anything.
* **The score is auditable.** Every term is returned as its own component, so the sum of the
  components is the score and a reader can see which feature produced a placement without
  re-running the ranker.
"""

from __future__ import annotations

from typing import Any

from contracts.ranking import DEFAULT_RANKING_WEIGHTS, RANK_FEATURES, RankingWeights

from .filters import read

#: The component key carrying the (negative) policy-penalty term, so the components still
#: sum to `rank_score` once penalties are involved.
PENALTY_COMPONENT = "policy_penalties"

#: Where each published feature is read from. `trust` is the only one that does not come off
#: the candidate record: it is the store's snapshot score, which is the whole reason the
#: snapshot is a separate argument to `rank()`.
CANDIDATE_FEATURES: tuple[str, ...] = (
    "intent_match",
    "verified_claim_ratio",
    "price_value",
    "delivery_fit",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, weights: RankingWeights) -> float:
    low = float(weights.normalization.feature_min)
    high = float(weights.normalization.feature_max)
    return low if value < low else high if value > high else value


def feature_vector(
    candidate: Any, trust_score: float | None, weights: RankingWeights
) -> dict[str, float]:
    """The five published features for one candidate, in the published order.

    A feature that is absent or unreadable takes its published neutral value rather than
    zero; `intent_match`, `price_value` and `trust` have no published `when_absent`, so a
    missing one takes the midpoint of the normalization range, which is the same "we do not
    know" that `when_absent` encodes for the other two.
    """
    bounds = weights.normalization
    midpoint = (float(bounds.feature_min) + float(bounds.feature_max)) / 2.0
    when_absent = {
        "delivery_fit": float(bounds.delivery_fit_when_absent),
        "verified_claim_ratio": float(bounds.verified_claim_ratio_when_absent),
    }

    vector: dict[str, float] = {}
    for name in RANK_FEATURES:
        if name == "trust":
            raw = trust_score
        else:
            raw = _number(read(candidate, name, None))
        if raw is None:
            raw = when_absent.get(name, midpoint)
        vector[name] = _clamp(float(raw), weights)
    return vector


def penalty_of(candidate: Any, weights: RankingWeights) -> float:
    """The bounded policy penalty for one candidate.

    Two spellings are accepted because two producers exist: a caller that has already summed
    its open policy events hands a number in `policy_penalties`, and one that has the raw
    event kinds hands them in `policy_events`. Both go through the published catalogue's
    `max_total_penalty`, so neither can drive a score arbitrarily negative.
    """
    kinds = read(candidate, "policy_events", None)
    if kinds:
        return float(weights.total_penalty(kinds))
    raw = _number(read(candidate, "policy_penalties", None))
    if raw is None or raw <= 0.0:
        return 0.0
    return min(raw, float(weights.penalties.max_total_penalty))


def score(
    candidate: Any,
    trust_score: float | None,
    weights: RankingWeights | None = None,
) -> tuple[float, dict[str, float], dict[str, float]]:
    """`(rank_score, components, features)` for one ELIGIBLE candidate.

    Never called for an ineligible one: an excluded candidate has no score, and this function
    returning a number for it is precisely the thing the eligibility gate exists to prevent.

    `components` are the weighted terms, so they sum to `rank_score`; `features` are the raw
    inputs those terms were computed from. Both are returned because they answer different
    questions — "what made this placement" and "what did we believe about this store" — and
    dividing one back out of the other is not possible for a zero weight.
    """
    weights = DEFAULT_RANKING_WEIGHTS if weights is None else weights
    vector = feature_vector(candidate, trust_score, weights)
    feature_weights = weights.feature_weights

    components = {name: feature_weights[name] * vector[name] for name in RANK_FEATURES}
    penalty = penalty_of(candidate, weights)
    if penalty:
        components[PENALTY_COMPONENT] = -penalty

    return float(sum(components.values())), components, vector


__all__ = [
    "CANDIDATE_FEATURES",
    "PENALTY_COMPONENT",
    "feature_vector",
    "penalty_of",
    "score",
]
