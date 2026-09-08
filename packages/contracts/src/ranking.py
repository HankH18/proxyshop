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

Two things published here are NOT weights, and they are here because they are the other half of
what a served score is reproducible from (R15/S3)
------------------------------------------------------------------------------------------------
* :data:`RANKING_FEATURES_VERSION` — the version of the FEATURE DEFINITIONS the weights are
  applied to. Weights and features are versioned separately because they change independently and
  a replay needs both: redefining `verified_claim_ratio` under an unchanged
  `RANKING_WEIGHTS_VERSION` re-scores every historical auction with nothing anywhere recording
  that anything moved, which is exactly the silent divergence the weights version exists to make
  impossible.
* the constants the two buyer-conditional features are DEFINED by —
  :data:`EVIDENCE_GAIN_BY_RELEVANCE`, :data:`INTENT_MATCH_WHEN_ABSENT` and
  :data:`PREFERENCE_FIELD_TERMS`. A number the exchange invents at the call site is a number
  nobody can reproduce a shortlist from, which is the same argument that put the weights here.

D50 and the layering these constants live inside
------------------------------------------------
D50 rules that there is ONE published weight set, "fixed per environment and identical for every
buyer", and that `Intent.preferences[].weight` feeds `intent_match` ONLY and never touches the
published weights. Per-buyer WEIGHTS are therefore forbidden. What is not forbidden — and is the
layering D50 prescribes — is a buyer's asks feeding a FEATURE, which is already how `intent_match`
is meant to work. :data:`EVIDENCE_GAIN_BY_RELEVANCE` is that layering applied to
`verified_claim_ratio`: same field name, same `w_e = 0.20`, no contract rename, and the weight
every buyer is scored with is still the one number this module publishes.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Mapping
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

#: The policy-event kind minted for a claim THIS EXCHANGE's own catalogue snapshot contradicts.
#: Named here rather than spelled at the producer, because the producer and the catalogue that
#: prices it disagreeing about the string would be a penalty that silently costs 0.0.
CONTRADICTED_CLAIM = "contradicted_claim"

#: D13's penalty catalogue. The ranker reads penalties from here instead of inventing them at the
#: call site.
#:
#: ``contradicted_claim`` = **0.15**, and the number is bounded from both sides on purpose:
#:
#: * **Large enough to deter.** Before it existed, a claim this exchange caught contradicting its
#:   own snapshot cost one auction's share of one ratio and nothing else — verdicts are minted per
#:   auction and carried between none — so with cheap generation an aggressive claim had positive
#:   expected value. 0.15 is 1.5x the most the ENTIRE evidence term can pay a store
#:   (``w_e * (feature_max - verified_claim_ratio_when_absent)`` = ``0.20 * 0.5`` = 0.10), so no
#:   amount of true, buyer-relevant evidence buys out one lie. It equals ``w_v``, the whole
#:   published weight of `price_value`: one contradicted claim costs exactly what clearing this
#:   auction's price band outright is worth, which is the deepest legitimate lever a store has.
#: * **Small enough that one bad verdict is not a death sentence.** It is half of
#:   ``severe_policy_violation`` (0.30) — being caught overstating one product fact is not the
#:   same act — and it is recoverable: ``w_m = 0.35`` for fit and ``w_t = 0.20`` for trust are
#:   both larger, so a store that fits the intent and has a record still ranks. With
#:   ``max_total_penalty`` at 1.0 it takes SEVEN contradicted claims to reach the bound, and the
#:   bound is what stops a repeated kind driving `rank_score` arbitrarily negative.
#:
#: This is the only asymmetric downside in the design, and it is why a persuasion market here is
#: not a lying market: every other term pays zero for a false claim, and zero is not a deterrent.
DEFAULT_PENALTIES_PER_KIND: Mapping[str, float] = {
    "severe_policy_violation": 0.30,
    CONTRADICTED_CLAIM: 0.15,
}

#: D13's tie-break order, applied left to right.
DEFAULT_TIE_BREAKERS: tuple[str, ...] = (
    "verified_hard_fit_count",
    "trust",
    "price",
    "bid_id",
)

RANKING_WEIGHTS_VERSION = "1.0.0"

#: The version of the FEATURE DEFINITIONS the published weights are applied to.
#:
#: ``3.0.0`` because `delivery_fit`'s FEED was replaced (D57). It read
#: ``Offer.delivery_estimate_days`` — a number the bidding store writes — normalised against the
#: other declared numbers in the auction, so a store bought up to ``w_d = 0.10`` of the published
#: score by promising sooner and nothing asked whether it had ever shipped that fast. It now
#: reads a CREDIBLE estimate: the quote divided by the store's `shipped_on_time` posterior off
#: the trust snapshot, with an unwatched store's promise not admitted at all (absent, therefore
#: the published neutral). Same name, same weight, same neutral, different number — which is
#: exactly the kind of change this constant exists to make visible.
#:
#: ``2.0.0`` was the definition set before that: two of the five features REDEFINED, not retuned —
#: `verified_claim_ratio` became buyer-conditional evidence (see
#: :data:`EVIDENCE_GAIN_BY_RELEVANCE`) and `price_value` became saturating at the auction's own
#: price band. Both kept their published name and their published weight, so
#: `RANKING_WEIGHTS_VERSION` did not move — and that is precisely the case this constant exists
#: for. R15/S3 promise that replaying the ledger reproduces the served scores; a feature redefined
#: under an unchanged weights version breaks that promise SILENTLY, because every recorded number
#: still validates and every published weight still matches. A replay must compare BOTH versions
#: before it may claim its recomputation reproduces a score.
#:
#: ``1.0.0`` is the definition set that shipped before both: `verified_claim_ratio` as the raw
#: share of decided claims verified, and `price_value` as the unsaturated
#: ``clamp((list_price - total_price)/list_price, 0, 1)``.
RANKING_FEATURES_VERSION = "3.0.0"

#: What `intent_match` reads when it is ABSENT — published, at last, rather than inherited.
#:
#: It has always been 0.5, and until now that 0.5 was the midpoint of
#: ``[feature_min, feature_max]`` picked up inside the scorer's fallback branch. Numerically
#: identical, epistemically not: a reader of a served score could not tell a COMPUTED 0.5
#: (retrieval measured this candidate as an exactly average fit) from an ABSENT one (a served
#: auction is handed a roster and queries no index, so nothing produced the term at all) — the
#: two are the same float and one of them is a statement about the store while the other is a
#: statement about this exchange's wiring. `NormalizationBounds` publishes the same distinction
#: for `delivery_fit` and `verified_claim_ratio` and could not publish it for this one, because
#: its property set is closed (`additionalProperties: false`) and the schema is not this module's
#: to change; publishing the number here is what a reader can reach either way.
INTENT_MATCH_WHEN_ABSENT = 0.5

#: Relevance tier -> the share of the REMAINING headroom one verified claim at that tier takes.
#:
#: This is the aggregation `verified_claim_ratio` uses instead of a raw ratio, and the shape is a
#: noisy-OR: ``value = 1 - Π(1 - gain_i)`` over this candidate's verified, buyer-relevant claims.
#: Four properties, and each one is load-bearing:
#:
#: * **Diminishing returns.** The first relevant fact a store proves is worth 0.55-0.70; the
#:   second is worth that share of what is LEFT. Ten facts are worth more than three and nowhere
#:   near three times as much, so the pitch that wins is the one that picks the right facts, not
#:   the one that lists the most.
#: * **It never saturates.** The product never reaches 0, so the value never reaches 1.0 and one
#:   more relevant verified fact is always worth something. The ceiling that matters is not this
#:   curve's: it is (this buyer's asks ∩ this store's catalogue facts that survive verification),
#:   which differs per store, so two stores at full effort still differ.
#: * **Order-independence.** Multiplication commutes, so the value does not depend on the order a
#:   bid happens to list its claims in (R11, and S3's replay properties).
#: * **Every tier clears the neutral on ONE claim.** All three gains are above
#:   ``verified_claim_ratio_when_absent`` (0.5), so a store that proves a single thing this buyer
#:   asked about already scores better than a store that said nothing. Below 0.5 the term would
#:   pay stores to stay silent, which is the opposite of what it is for.
#:
#: The tiers rank how hard the buyer asked: a hard constraint is a must-have they stated, a
#: preference is a want they stated, a query term is a word they typed. That ordering is what
#: makes the SAME fact worth different amounts to different buyers, which is the whole mechanism
#: — a store's best pitch is now a function of who it is pitching to.
EVIDENCE_GAIN_BY_RELEVANCE: Mapping[str, float] = {
    "hard_constraint": 0.70,
    "preference": 0.60,
    "query_term": 0.55,
    "term_scored_ask": 0.55,
}

#: Relevance tiers, strongest first. Published so a producer resolving a field that appears in
#: more than one place in an intent picks the strongest rather than whichever it read last.
#:
#: ``term_scored_ask`` is the buyer asking on an axis a PUBLISHED TERM already scores — a price
#: preference, a delivery bound. It sits at the bottom, and it is a separate tier rather than
#: simply refused, because the two things being measured are genuinely different quantities and
#: only one of them is scored twice:
#:
#: * `price_value` measures the offer's price LEVEL. Admitting a price preference into
#:   `intent_match` would put a monotone function of that same level into the formula a second
#:   time — which is the refusal :data:`PREFERENCE_FIELD_TERMS` mandates, and it is absolute.
#: * A verified ``list_price`` claim measures whether the store told the TRUTH about its
#:   catalogue price. It is a boolean about honesty, not a function of the price, and a store
#:   bidding at full price earns it while scoring 0.0 on `price_value`.
#:
#: It is the weakest tier because a claim on an already-scored axis is the same claim for every
#: buyer, so it is the least customized thing a store can say — it earns slightly more than
#: silence (0.55 against the 0.5 neutral) and far less than proving a fact this buyer named.
RELEVANCE_TIERS: tuple[str, ...] = (
    "hard_constraint",
    "preference",
    "query_term",
    "term_scored_ask",
)

#: Preference fields a PUBLISHED TERM ALREADY SCORES, and which term scores them.
#:
#: **The rule: `intent_match` must refuse every field in this map.** A preference on price is
#: already scored by `price_value` and a preference on delivery is already scored by
#: `delivery_fit`; letting either one back in through `intent_match` scores one thing twice under
#: two names, which contradicts DESIGN's "this is the only rank formula in the system" and
#: silently repartitions the published weights.
#:
#: The hazard is measured, not hypothetical. S1's intent carries exactly ONE preference —
#: ``{price, minimize, 1.0}`` — so with the min-max-across-the-eligible-set normalisation
#: `retrieval.service` applies, an `intent_match` wired naively over that intent IS normalised
#: inverse price. Price's share of the published weight would go from ``w_v = 0.15`` to
#: ``w_v + w_m = 0.50``: the formula would be a price auction wearing a fit term's name, which is
#: the exact market SPEC's core tenet rules out.
#:
#: The same refusal governs which preferences may make a claim buyer-relevant for
#: `verified_claim_ratio`, and for the same reason: a verified ``price_usd`` claim scored there
#: would be a third helping of the same number.
#:
#: Keys are canonical (:func:`canonical_field`), so ``price_usd``, ``Price USD`` and ``price-usd``
#: are one field here and not three.
PREFERENCE_FIELD_TERMS: Mapping[str, str] = {
    "price": "price_value",
    "price-usd": "price_value",
    "unit-price": "price_value",
    "total-price": "price_value",
    "list-price": "price_value",
    "cost": "price_value",
    "discount": "price_value",
    "discount-pct": "price_value",
    "delivery": "delivery_fit",
    "delivery-days": "delivery_fit",
    "delivery-estimate-days": "delivery_fit",
    "shipping": "delivery_fit",
    "shipping-days": "delivery_fit",
    "shipping-speed": "delivery_fit",
    "trust": "trust",
    "trust-score": "trust",
}

_NON_FIELD_CHARS = re.compile(r"[^a-z0-9]+")


def canonical_field(field: Any) -> str:
    """``field`` folded to the one spelling this module compares fields in.

    Lowercase, every run of non-alphanumerics collapsed to ``-``, ends trimmed:
    ``price_usd``, ``Price USD`` and ``price-usd`` are all ``price-usd``. It agrees with the
    exchange's own `ingest.graph.model.slug` on every field name either one sees, and it is
    restated here rather than imported because `packages/contracts` is the base layer — a
    contract that imported a service would invert the dependency this package exists to define.
    """
    return _NON_FIELD_CHARS.sub("-", str(field).lower()).strip("-")


def preference_term_conflict(field: Any) -> str | None:
    """The published term that ALREADY scores ``field``, or ``None`` when none does.

    A producer of `intent_match` calls this and drops every preference it answers for. Returning
    the term's NAME rather than a bool is deliberate: the caller that has to explain a dropped
    preference to a store operator ("your price preference is scored by `price_value`") needs to
    say which term took it, and a bool cannot.
    """
    return PREFERENCE_FIELD_TERMS.get(canonical_field(field))


def diminishing_evidence(gains: Iterable[float]) -> float:
    """``1 - Π(1 - gain)`` over ``gains`` — the published aggregation for verified evidence.

    See :data:`EVIDENCE_GAIN_BY_RELEVANCE`. Empty input is ``0.0``, which is the value of having
    proved nothing the buyer asked about — NOT the value of having said nothing at all. The
    difference between those two is the caller's to make: a candidate with no relevant decided
    claims has no evidence to aggregate, so its feature is ABSENT and reads
    `verified_claim_ratio_when_absent`, while a candidate whose relevant claims were all decided
    against it has evidence and it came to nothing.
    """
    remaining = 1.0
    for gain in gains:
        value = float(gain)
        if not math.isfinite(value) or value <= 0.0:
            continue
        remaining *= 1.0 - min(value, 1.0)
    return 1.0 - remaining


class RankingWeights(_RankingWeightsSchema):
    """The generated `RankingWeights` schema type plus the cross-field rule JSON Schema cannot say.

    JSON Schema has no way to express "these five numbers sum to 1.0", so the constraint lives in
    the model. The TypeScript half enforces the identical rule in `rankingWeightsErrors`, and
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

    @model_validator(mode="after")
    def _penalties_are_non_negative(self) -> RankingWeights:
        """A negative penalty is a bonus wearing a penalty's name, and would let a store improve
        its rank by accumulating policy events."""
        negative = {
            kind: value for kind, value in self.penalties.per_kind.items() if float(value) < 0
        }
        if negative:
            raise ValueError(f"policy-event penalties must be non-negative; got {negative}")
        return self

    @property
    def weights(self) -> dict[str, float]:
        """`{symbol: weight}` for the five published weights."""
        return {field: float(getattr(self, field)) for field in WEIGHT_FIELDS}

    @property
    def feature_weights(self) -> dict[str, float]:
        """`{feature_name: weight}` — the formula written out against the candidate record."""
        return dict(
            zip(RANK_FEATURES, (float(getattr(self, f)) for f in WEIGHT_FIELDS), strict=True)
        )

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
    "CONTRADICTED_CLAIM",
    "DEFAULT_PENALTIES_PER_KIND",
    "DEFAULT_RANKING_WEIGHTS",
    "DEFAULT_TIE_BREAKERS",
    "EVIDENCE_GAIN_BY_RELEVANCE",
    "INTENT_MATCH_WHEN_ABSENT",
    "PREFERENCE_FIELD_TERMS",
    "RANKING_FEATURES_VERSION",
    "RANKING_WEIGHTS_VERSION",
    "RANK_FEATURES",
    "RELEVANCE_TIERS",
    "VERSION_ENV_VAR",
    "WEIGHT_ENV_VARS",
    "WEIGHT_FIELDS",
    "WEIGHT_SUM_TOLERANCE",
    "NormalizationBounds",
    "PenaltyCatalogue",
    "RankingWeights",
    "canonical_field",
    "diminishing_evidence",
    "preference_term_conflict",
]
