"""`RankingWeights` — the published numbers, and the rule that keeps them honest (D13/D50).

The rule under test is not "the weights are validated". It is that a weight set which does NOT
sum to 1.0 is *rejected*, rather than renormalized on the way in. Renormalization is the failure
mode this guards: it looks harmless, and it means the numbers published in DESIGN and the numbers
actually applied to a shortlist are different, with nothing anywhere recording the difference.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.registry import is_valid
from packages.contracts import (
    DEFAULT_RANKING_WEIGHTS,
    RANK_FEATURES,
    RANKING_WEIGHTS_VERSION,
    WEIGHT_FIELDS,
    RankingWeights,
)


def test_the_published_defaults_are_the_design_numbers() -> None:
    assert DEFAULT_RANKING_WEIGHTS.weights == {
        "w_m": 0.35,
        "w_e": 0.20,
        "w_t": 0.20,
        "w_v": 0.15,
        "w_d": 0.10,
    }
    assert DEFAULT_RANKING_WEIGHTS.version == RANKING_WEIGHTS_VERSION


def test_the_defaults_sum_to_one() -> None:
    assert sum(DEFAULT_RANKING_WEIGHTS.weights.values()) == pytest.approx(1.0)


def test_the_five_weights_map_onto_the_five_published_features() -> None:
    """The formula is the five-term one (D12/D50). `fit` and `offer_value` are not terms."""
    assert tuple(WEIGHT_FIELDS) == ("w_m", "w_e", "w_t", "w_v", "w_d")
    assert tuple(RANK_FEATURES) == (
        "intent_match",
        "verified_claim_ratio",
        "trust",
        "price_value",
        "delivery_fit",
    )
    assert DEFAULT_RANKING_WEIGHTS.feature_weights["verified_claim_ratio"] == pytest.approx(0.20)
    assert "fit" not in DEFAULT_RANKING_WEIGHTS.feature_weights
    assert "offer_value" not in DEFAULT_RANKING_WEIGHTS.feature_weights


@pytest.mark.parametrize(
    "weights",
    [
        {"w_m": 0.35, "w_e": 0.20, "w_t": 0.20, "w_v": 0.15, "w_d": 0.15},  # sums to 1.05
        {"w_m": 0.35, "w_e": 0.20, "w_t": 0.20, "w_v": 0.15, "w_d": 0.05},  # sums to 0.95
        {"w_m": 0.20, "w_e": 0.20, "w_t": 0.20, "w_v": 0.20, "w_d": 0.10},  # sums to 0.90
        {"w_m": 1.00, "w_e": 0.00, "w_t": 0.00, "w_v": 0.00, "w_d": 0.10},  # sums to 1.10
    ],
)
def test_a_weight_set_that_does_not_sum_to_one_is_invalid(weights: dict) -> None:
    with pytest.raises(ValidationError, match="sum to 1.0"):
        RankingWeights.model_validate({**DEFAULT_RANKING_WEIGHTS.model_dump(), **weights})


def test_a_different_but_valid_weight_set_is_accepted() -> None:
    """The rule is "sums to 1.0", not "equals the defaults" — an environment may retune."""
    retuned = RankingWeights.model_validate(
        {
            **DEFAULT_RANKING_WEIGHTS.model_dump(),
            "version": "1.1.0",
            "w_m": 0.30,
            "w_e": 0.25,
            "w_t": 0.20,
            "w_v": 0.15,
            "w_d": 0.10,
        }
    )
    assert retuned.version == "1.1.0"
    assert sum(retuned.weights.values()) == pytest.approx(1.0)


def test_the_tolerance_admits_representation_error_and_nothing_larger() -> None:
    """The tolerance exists for binary floating point, not as slack in the rule. A sum off by
    1e-12 is the same weight set; a sum off by 1e-6 is a different one, and both must be treated
    as such."""
    from contracts.ranking import WEIGHT_SUM_TOLERANCE

    base = DEFAULT_RANKING_WEIGHTS.model_dump()
    noise = RankingWeights.model_validate({**base, "w_m": 0.35 + WEIGHT_SUM_TOLERANCE / 1000})
    assert noise.weights["w_m"] == pytest.approx(0.35)

    with pytest.raises(ValidationError, match="sum to 1.0"):
        RankingWeights.model_validate({**base, "w_m": 0.35 + 1e-6})


def test_a_negative_or_out_of_range_weight_is_invalid() -> None:
    for bad in ({"w_m": -0.15, "w_d": 0.60}, {"w_m": 1.35, "w_e": -0.15}):
        with pytest.raises(ValidationError):
            RankingWeights.model_validate({**DEFAULT_RANKING_WEIGHTS.model_dump(), **bad})


# --- normalization bounds and missing-value behaviour --------------------------------------


def test_missing_features_are_neutral_rather_than_zero() -> None:
    """D13/D14: a missing value is not evidence of a bad one. Zero would punish the uncrawled."""
    bounds = DEFAULT_RANKING_WEIGHTS.normalization
    assert bounds.delivery_fit_when_absent == pytest.approx(0.5)
    assert bounds.verified_claim_ratio_when_absent == pytest.approx(0.5)
    assert (bounds.feature_min, bounds.feature_max) == (0.0, 1.0)


# --- the penalty catalogue ------------------------------------------------------------------


def test_the_published_penalty_catalogue_carries_the_d13_entry() -> None:
    assert DEFAULT_RANKING_WEIGHTS.penalty_for("severe_policy_violation") == pytest.approx(0.30)


def test_an_unknown_policy_event_kind_carries_no_penalty() -> None:
    """The ranker reads penalties from the catalogue; it must not invent one for a new kind."""
    assert DEFAULT_RANKING_WEIGHTS.penalty_for("kind-that-has-no-published-penalty") == 0.0


def test_penalties_accumulate_but_are_bounded() -> None:
    """Unbounded, one repeated kind drives rank_score arbitrarily negative and the formula stops
    being a comparison between candidates."""
    weights = DEFAULT_RANKING_WEIGHTS
    assert weights.total_penalty([]) == 0.0
    assert weights.total_penalty(["severe_policy_violation"]) == pytest.approx(0.30)
    assert weights.total_penalty(["severe_policy_violation"] * 2) == pytest.approx(0.60)
    assert weights.total_penalty(["severe_policy_violation"] * 50) == pytest.approx(
        weights.penalties.max_total_penalty
    )


def test_tie_breakers_are_published_in_order() -> None:
    assert list(DEFAULT_RANKING_WEIGHTS.tie_breakers) == [
        "verified_hard_fit_count",
        "trust",
        "price",
        "bid_id",
    ]


# --- environment loading ---------------------------------------------------------------------


def test_from_env_returns_the_defaults_when_nothing_is_set() -> None:
    assert RankingWeights.from_env({}) == DEFAULT_RANKING_WEIGHTS


def test_from_env_applies_a_complete_override() -> None:
    loaded = RankingWeights.from_env(
        {
            "RANK_W_M": "0.30",
            "RANK_W_E": "0.25",
            "RANK_W_T": "0.20",
            "RANK_W_V": "0.15",
            "RANK_W_D": "0.10",
            "RANK_WEIGHTS_VERSION": "1.2.0",
        }
    )
    assert loaded.weights["w_m"] == pytest.approx(0.30)
    assert loaded.version == "1.2.0"


def test_from_env_rejects_a_partial_override_that_breaks_the_sum() -> None:
    """Overriding one weight without rebalancing the rest is the most likely real mistake."""
    with pytest.raises(ValidationError, match="sum to 1.0"):
        RankingWeights.from_env({"RANK_W_M": "0.50"})


def test_from_env_rejects_a_malformed_number_rather_than_defaulting_silently() -> None:
    with pytest.raises(ValueError, match="not a number"):
        RankingWeights.from_env({"RANK_W_M": "quite a lot"})


# --- the schema half ---------------------------------------------------------------------------


def test_ranking_weights_is_in_the_json_schema_bundle() -> None:
    """It is a generated contract type, importable in both languages, not a Python-only object."""
    assert is_valid("RankingWeights", DEFAULT_RANKING_WEIGHTS.model_dump())
    assert not is_valid("RankingWeights", {"w_m": 0.35})


def test_the_schema_cannot_express_the_sum_rule_so_both_languages_must() -> None:
    """Documents the division of labour: JSON Schema accepts this set, the model does not."""
    broken = {**DEFAULT_RANKING_WEIGHTS.model_dump(), "w_d": 0.5}
    assert is_valid("RankingWeights", broken), (
        "if JSON Schema ever gains a sum constraint this test should be revisited"
    )
    with pytest.raises(ValidationError):
        RankingWeights.model_validate(broken)


def test_the_tolerance_itself_is_pinned() -> None:
    """Without this, the tolerance could be loosened 100x and every other test here would still
    pass: they all derive their inputs from the constant, so any value below 1e-6 satisfies them.
    The number is the contract — it says representation error is forgiven and nothing else is."""
    from contracts.ranking import WEIGHT_SUM_TOLERANCE

    assert WEIGHT_SUM_TOLERANCE == 1e-9


def test_a_set_off_by_a_thousand_times_the_tolerance_is_rejected() -> None:
    """A hard-coded near-miss, independent of the constant, so loosening the constant is caught."""
    with pytest.raises(ValidationError, match="sum to 1.0"):
        RankingWeights.model_validate({**DEFAULT_RANKING_WEIGHTS.model_dump(), "w_m": 0.35 + 1e-6})


def test_a_negative_penalty_is_rejected() -> None:
    """A negative penalty is a bonus wearing a penalty's name: a store could improve its rank by
    accumulating policy events."""
    payload = DEFAULT_RANKING_WEIGHTS.model_dump()
    payload["penalties"] = {**payload["penalties"], "per_kind": {"severe_policy_violation": -0.3}}
    with pytest.raises(ValidationError, match="non-negative"):
        RankingWeights.model_validate(payload)


# --- features 2.0.0: the published definitions the weights are applied TO -------------------


def test_the_feature_definitions_are_versioned_separately_from_the_weights() -> None:
    """R15/S3: a replay reproduces served scores only if it knows BOTH versions.

    Weights and feature definitions change independently. Redefining `verified_claim_ratio`
    under an unchanged `RANKING_WEIGHTS_VERSION` re-scores every historical auction with
    nothing anywhere recording that anything moved — every stored number still validates and
    every published weight still matches — which is exactly the silent divergence the weights
    version exists to make impossible. So there are two versions, and they are not the same
    string: a single version would make one of the two changes invisible.
    """
    from contracts.ranking import RANKING_FEATURES_VERSION

    assert RANKING_FEATURES_VERSION
    assert RANKING_FEATURES_VERSION != RANKING_WEIGHTS_VERSION
    # 2.x, because two of the five features were REDEFINED and not merely retuned.
    assert RANKING_FEATURES_VERSION.split(".")[0] == "2"


def test_intent_match_has_a_published_neutral_of_its_own() -> None:
    """The number does not move; where a reader finds it does.

    It has always been 0.5, inherited from the scorer's `[feature_min, feature_max]` midpoint
    and published nowhere — so a computed 0.5 (an exactly average fit) and an absent one (a
    served auction is handed a roster and queries no index, so nothing produces the term) were
    the same float saying opposite things about whose gap it is. `NormalizationBounds`
    publishes that distinction for the other two and its property set is closed.
    """
    from contracts.ranking import INTENT_MATCH_WHEN_ABSENT

    bounds = DEFAULT_RANKING_WEIGHTS.normalization
    midpoint = (float(bounds.feature_min) + float(bounds.feature_max)) / 2.0
    assert INTENT_MATCH_WHEN_ABSENT == pytest.approx(midpoint), (
        "publishing the neutral CHANGED it, so every historical intent_match-absent score "
        "just moved"
    )
    assert 0.0 <= INTENT_MATCH_WHEN_ABSENT <= 1.0


def test_a_contradicted_claim_is_priced_and_the_price_is_bounded_from_both_sides() -> None:
    """The only asymmetric downside in the design, and why 0.15 rather than any other number.

    Both bounds are asserted rather than described, because both are what make it a deterrent
    that is not also a death sentence.
    """
    from contracts.ranking import CONTRADICTED_CLAIM

    weights = DEFAULT_RANKING_WEIGHTS
    penalty = weights.penalty_for(CONTRADICTED_CLAIM)
    bounds = weights.normalization

    assert penalty == pytest.approx(0.15)
    # LARGE ENOUGH: no amount of true, buyer-relevant evidence buys out one lie. The most the
    # whole evidence term can pay a store is w_e * (max - neutral).
    most_the_evidence_term_can_pay = weights.feature_weights["verified_claim_ratio"] * (
        float(bounds.feature_max) - float(bounds.verified_claim_ratio_when_absent)
    )
    assert penalty > most_the_evidence_term_can_pay == pytest.approx(0.10)
    # SMALL ENOUGH: below `severe_policy_violation`, and below both of the terms a store
    # recovers on, so one bad verdict is survivable.
    assert penalty < weights.penalty_for("severe_policy_violation")
    assert penalty < weights.feature_weights["trust"]
    assert penalty < weights.feature_weights["intent_match"]
    # And it accumulates under the published bound rather than without one.
    assert weights.total_penalty([CONTRADICTED_CLAIM] * 2) == pytest.approx(2 * penalty)
    assert weights.total_penalty([CONTRADICTED_CLAIM] * 100) == pytest.approx(
        weights.penalties.max_total_penalty
    )


def test_the_evidence_aggregation_diminishes_without_ever_saturating() -> None:
    """`verified_claim_ratio`'s published aggregation, and the four properties it is chosen for.

    Both failure modes are real. Without diminishing returns the term is a claim-count race and
    the longest pitch wins; with a hard ceiling it goes flat at full effort and every store
    converges on one pitch again.
    """
    from contracts.ranking import EVIDENCE_GAIN_BY_RELEVANCE, RELEVANCE_TIERS, diminishing_evidence

    bounds = DEFAULT_RANKING_WEIGHTS.normalization
    neutral = float(bounds.verified_claim_ratio_when_absent)

    assert tuple(EVIDENCE_GAIN_BY_RELEVANCE) == RELEVANCE_TIERS
    # EVERY tier beats silence on ONE claim, or the term pays a store to say nothing.
    assert all(gain > neutral for gain in EVIDENCE_GAIN_BY_RELEVANCE.values())
    # The tiers rank how hard the buyer asked.
    ordered = [EVIDENCE_GAIN_BY_RELEVANCE[tier] for tier in RELEVANCE_TIERS]
    assert ordered == sorted(ordered, reverse=True)

    gain = EVIDENCE_GAIN_BY_RELEVANCE["hard_constraint"]
    values = [diminishing_evidence([gain] * n) for n in range(0, 10)]
    assert values[0] == 0.0, "proving nothing the buyer asked about is not worth anything"
    steps = [later - earlier for earlier, later in zip(values, values[1:], strict=False)]
    assert all(step > 0.0 for step in steps), f"the aggregation saturates: {values}"
    assert all(later < earlier for earlier, later in zip(steps, steps[1:], strict=False)), (
        f"the returns are not diminishing: {steps}"
    )
    assert values[-1] < float(bounds.feature_max)

    # Order-independent, which R11 and S3's replay both need: multiplication commutes.
    tiers = list(EVIDENCE_GAIN_BY_RELEVANCE.values())
    assert diminishing_evidence(tiers) == pytest.approx(diminishing_evidence(reversed(tiers)))
    # A non-positive or non-finite gain contributes nothing rather than poisoning the product.
    assert diminishing_evidence([0.6, 0.0, -1.0, float("nan")]) == pytest.approx(0.6)


def test_intent_match_must_refuse_a_preference_a_published_term_already_scores() -> None:
    """The structural rule that stops `intent_match` becoming price under a new name.

    Measured hazard: S1's intent carries exactly ONE preference, `{price, minimize, 1.0}`, so
    with min-max normalisation across the eligible set an `intent_match` wired naively over it
    IS normalised inverse price — and price's share of the published weight goes from
    `w_v` = 0.15 to `w_v + w_m` = 0.50. Scoring one thing twice under two names contradicts
    DESIGN's "this is the only rank formula in the system".
    """
    from contracts.ranking import (
        PREFERENCE_FIELD_TERMS,
        canonical_field,
        preference_term_conflict,
    )

    weights = DEFAULT_RANKING_WEIGHTS.feature_weights
    assert weights["price_value"] + weights["intent_match"] == pytest.approx(0.50)

    # Every refusal names a term the published formula actually has, so nothing is refused on
    # behalf of a term that does not exist.
    assert set(PREFERENCE_FIELD_TERMS.values()) <= set(RANK_FEATURES)
    # Every key is already canonical, so a lookup cannot miss on spelling alone.
    assert all(canonical_field(field) == field for field in PREFERENCE_FIELD_TERMS)

    for spelling in ("price", "Price USD", "price_usd", "total-price", "list_price"):
        assert preference_term_conflict(spelling) == "price_value", spelling
    for spelling in ("delivery", "delivery_estimate_days", "Shipping Speed"):
        assert preference_term_conflict(spelling) == "delivery_fit", spelling
    # …and a field no published term scores is NOT refused, or `intent_match` would have
    # nothing left to score at all.
    for spelling in ("capacity_l", "material", "boiler_type", "warranty_months"):
        assert preference_term_conflict(spelling) is None, spelling
