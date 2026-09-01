"""`RankingWeights` — the one versioned weight set the one published rank formula reads (D13/D50).

There is exactly ONE rank formula and it is DESIGN's five-term one::

    rank_score = w_m*intent_match + w_e*verified_claim_ratio + w_t*trust
               + w_v*price_value + w_d*delivery_fit − policy_penalties

The three-term `w_f*fit + w_v*offer_value + w_t*trust` that also appears in DESIGN is superseded
prose (D12): it predates the reconciliation amendment and has no `verified_claim_ratio` term, which
is the whole reason T-032 depends on the verifier. `fit` and `value` survive only as D29 shortlist
SLOT names — a different concept, not formula terms.

Three properties are enforced here rather than left to the ranker:

* **The five weights sum to 1.0.** A set that does not is invalid, not "normalized on the way in".
  Silent renormalization would mean the published weights and the applied weights are different
  numbers, and nobody could tell which produced a given shortlist.
* **Missing features are NEUTRAL, not zero.** `delivery_fit` and `verified_claim_ratio` read 0.5
  when absent (D13/D14). Scoring a missing value as 0 punishes every store the verifier has not
  reached yet, which is a systematic bias in favour of whoever was crawled first.
* **Penalties are bounded.** `policy_penalties` is a sum over open policy events; without
  `max_total_penalty` a single repeated event kind drives `rank_score` arbitrarily negative and
  the formula stops being a comparison between candidates.

The weight set is fixed per environment and identical for every buyer, so it is loaded from env
once (`RankingWeights.from_env()`), never per-request and never per-store.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from pydantic import model_validator

from contracts.protocol import NormalizationBounds, PenaltyCatalogue
from contracts.protocol import _GeneratedRankingWeights as _RankingWeightsSchema

#: The five weight symbols, in the order DESIGN publishes them.
WEIGHT_FIELDS: tuple[str, ...] = ("w_m", "w_e", "w_t", "w_v", "w_d")

#: The five features the formula reads, in the same order as `WEIGHT_FIELDS`. These are exactly
#: the candidate-record field names T-032 is graded on.
RANK_FEATURES: tuple[str, ...] = (
    "intent_match",
    "verified_claim_ratio",
    "trust",
    "price_value",
    "delivery_fit",
)

#: Float comparison tolerance for the sum-to-one rule. Tight enough that a real mistake
#: (0.35/0.20/0.20/0.15/0.15) is rejected, loose enough that binary floating point is not.
WEIGHT_SUM_TOLERANCE = 1e-9

#: Environment variables the weight set is read from, per weight symbol.
WEIGHT_ENV_VARS: Mapping[str, str] = {
    "w_m": "RANK_W_M",
    "w_e": "RANK_W_E",
    "w_t": "RANK_W_T",
    "w_v": "RANK_W_V",
    "w_d": "RANK_W_D",
}

#: Environment variable carrying the published version string.
VERSION_ENV_VAR = "RANK_WEIGHTS_VERSION"

#: D13's initial penalty catalogue. One entry today; the point of publishing it is that the
#: ranker reads penalties from here instead of inventing them at the call site.
DEFAULT_PENALTIES_PER_KIND: Mapping[str, float] = {"severe_policy_violation": 0.30}

#: D13's tie-break order, applied left to right.
DEFAULT_TIE_BREAKERS: tuple[str, ...] = (
    "verified_hard_fit_count",
    "trust",
    "price",
    "bid_id",
)

RANKING_WEIGHTS_VERSION = "1.0.0"


class RankingWeights(_RankingWeightsSchema):
    """The generated `RankingWeights` schema type plus the cross-field rule JSON Schema cannot say.

    JSON Schema has no way to express "these five numbers sum to 1.0", so the constraint lives in
    the model. The TypeScript half enforces the identical rule in `validateRankingWeights`, and
    both are asserted against the same fixture so the two languages cannot disagree about which
    weight sets are legal.
    """

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> RankingWeights:
        total = sum(getattr(self, field) for field in WEIGHT_FIELDS)
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                "the five published rank weights must sum to 1.0; "
                f"{'+'.join(f'{f}={getattr(self, f)}' for f in WEIGHT_FIELDS)} = {total!r}. "
                "A set that does not sum to 1.0 is rejected rather than renormalized, so the "
                "published weights and the applied weights can never be different numbers."
            )
        return self

    @property
    def weights(self) -> dict[str, float]:
        """`{symbol: weight}` for the five published weights."""
        return {field: float(getattr(self, field)) for field in WEIGHT_FIELDS}

    @property
    def feature_weights(self) -> dict[str, float]:
        """`{feature_name: weight}` — the formula written out against the candidate record."""
        return dict(zip(RANK_FEATURES, (float(getattr(self, f)) for f in WEIGHT_FIELDS), strict=True))

    def penalty_for(self, policy_event_kind: str) -> float:
        """The published penalty for one policy-event kind. 0.0 for a kind with no entry."""
        return float(self.penalties.per_kind.get(policy_event_kind, 0.0))

    def total_penalty(self, policy_event_kinds: Any) -> float:
        """Σ per-kind penalties over open policy events, clamped to `max_total_penalty`."""
        total = sum(self.penalty_for(str(kind)) for kind in policy_event_kinds or ())
        return min(total, float(self.penalties.max_total_penalty))

    @classmethod
    def defaults(cls) -> RankingWeights:
        """The published defaults: `w_m=0.35, w_e=0.20, w_t=0.20, w_v=0.15, w_d=0.10`."""
        return cls(
            version=RANKING_WEIGHTS_VERSION,
            w_m=0.35,
            w_e=0.20,
            w_t=0.20,
            w_v=0.15,
            w_d=0.10,
            normalization=NormalizationBounds(
                feature_min=0.0,
                feature_max=1.0,
                delivery_fit_when_absent=0.5,
                verified_claim_ratio_when_absent=0.5,
            ),
            penalties=PenaltyCatalogue(
                per_kind=dict(DEFAULT_PENALTIES_PER_KIND),
                max_total_penalty=1.0,
            ),
            tie_breakers=list(DEFAULT_TIE_BREAKERS),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RankingWeights:
        """Load the weight set from the environment, falling back to the published defaults.

        An env-supplied set that does not sum to 1.0 raises here, at load time, rather than
        producing a shortlist nobody can reproduce from the published numbers.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        base = cls.defaults()
        overrides: dict[str, Any] = {}
        for field, var in WEIGHT_ENV_VARS.items():
            raw = source.get(var)
            if raw is None or not str(raw).strip():
                continue
            try:
                overrides[field] = float(raw)
            except ValueError:
                raise ValueError(
                    f"{var}={raw!r} is not a number; rank weights are configuration, and a "
                    "malformed one must fail at load rather than default silently"
                ) from None
        version = source.get(VERSION_ENV_VAR)
        if version and version.strip():
            overrides["version"] = version.strip()
        if not overrides:
            return base
        return cls.model_validate({**base.model_dump(), **overrides})


#: The published weight set. Import this rather than re-deriving the numbers.
DEFAULT_RANKING_WEIGHTS = RankingWeights.defaults()


__all__ = [
    "DEFAULT_PENALTIES_PER_KIND",
    "DEFAULT_RANKING_WEIGHTS",
    "DEFAULT_TIE_BREAKERS",
    "RANKING_WEIGHTS_VERSION",
    "RANK_FEATURES",
    "VERSION_ENV_VAR",
    "WEIGHT_ENV_VARS",
    "WEIGHT_FIELDS",
    "WEIGHT_SUM_TOLERANCE",
    "NormalizationBounds",
    "PenaltyCatalogue",
    "RankingWeights",
]
