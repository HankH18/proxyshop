/**
 * `RankingWeights` in TypeScript — the same numbers and the same rule the Python suite asserts.
 *
 * The rule is that a set which does not sum to 1.0 is REJECTED, not renormalized. Renormalizing
 * looks harmless and means the published weights and the applied weights are different numbers,
 * with nothing recording the difference.
 */
import {describe, expect, it} from "vitest";

import {
  CONTRADICTED_CLAIM,
  DEFAULT_RANKING_WEIGHTS,
  EVIDENCE_GAIN_BY_RELEVANCE,
  INTENT_MATCH_WHEN_ABSENT,
  PREFERENCE_FIELD_TERMS,
  RANKING_FEATURES_VERSION,
  RANKING_WEIGHTS_VERSION,
  RANK_FEATURES,
  RELEVANCE_TIERS,
  WEIGHT_FIELDS,
  WEIGHT_SUM_TOLERANCE,
  assertRankingWeights,
  canonicalField,
  diminishingEvidence,
  featureWeights,
  isRankingWeights,
  preferenceTermConflict,
  rankingWeightsErrors,
  totalPenalty,
} from "../src/ts/ranking.js";
import {isValid} from "../src/ts/schemas.js";

const asRecord = (weights: unknown) => weights as unknown as Record<string, number>;

describe("the published weight set", () => {
  it("carries the DESIGN numbers", () => {
    const record = asRecord(DEFAULT_RANKING_WEIGHTS);
    expect(record["w_m"]).toBe(0.35);
    expect(record["w_e"]).toBe(0.2);
    expect(record["w_t"]).toBe(0.2);
    expect(record["w_v"]).toBe(0.15);
    expect(record["w_d"]).toBe(0.1);
    expect(DEFAULT_RANKING_WEIGHTS.version).toBe(RANKING_WEIGHTS_VERSION);
  });

  it("sums to 1.0 and is accepted", () => {
    const total = WEIGHT_FIELDS.reduce((sum, f) => sum + asRecord(DEFAULT_RANKING_WEIGHTS)[f]!, 0);
    expect(Math.abs(total - 1)).toBeLessThanOrEqual(WEIGHT_SUM_TOLERANCE);
    expect(rankingWeightsErrors(DEFAULT_RANKING_WEIGHTS)).toEqual([]);
  });

  it("maps the five weights onto the five published features", () => {
    // The formula is the five-term one (D12/D50); `fit` and `offer_value` are not terms.
    expect([...RANK_FEATURES]).toEqual([
      "intent_match",
      "verified_claim_ratio",
      "trust",
      "price_value",
      "delivery_fit",
    ]);
    const mapped = featureWeights(DEFAULT_RANKING_WEIGHTS);
    expect(mapped["verified_claim_ratio"]).toBe(0.2);
    expect(mapped["fit"]).toBeUndefined();
    expect(mapped["offer_value"]).toBeUndefined();
  });

  it("validates against the shared JSON Schema", () => {
    expect(isValid("RankingWeights", DEFAULT_RANKING_WEIGHTS)).toBe(true);
    expect(isValid("RankingWeights", {w_m: 0.35})).toBe(false);
  });
});

describe("the sum-to-one rule", () => {
  it.each([
    ["sums to 1.05", {w_d: 0.15}],
    ["sums to 0.95", {w_d: 0.05}],
    ["sums to 0.90", {w_m: 0.2, w_v: 0.2}],
    ["sums to 1.65", {w_m: 1.0}],
  ] as const)("rejects a set that %s", (_label, override) => {
    const weights = {...DEFAULT_RANKING_WEIGHTS, ...override};
    expect(isRankingWeights(weights)).toBe(false);
    expect(rankingWeightsErrors(weights).join(" ")).toContain("sum to 1.0");
    expect(() => assertRankingWeights(weights)).toThrow(/sum to 1.0/);
  });

  it("accepts a different but valid retuning", () => {
    // The rule is "sums to 1.0", not "equals the defaults".
    const retuned = {
      ...DEFAULT_RANKING_WEIGHTS,
      version: "1.1.0",
      w_m: 0.3,
      w_e: 0.25,
      w_t: 0.2,
      w_v: 0.15,
      w_d: 0.1,
    };
    expect(isRankingWeights(retuned)).toBe(true);
  });

  it("admits representation error and nothing larger", () => {
    const base = asRecord(DEFAULT_RANKING_WEIGHTS);
    const noise = {...DEFAULT_RANKING_WEIGHTS, w_m: base["w_m"]! + WEIGHT_SUM_TOLERANCE / 1000};
    expect(isRankingWeights(noise)).toBe(true);
    const drift = {...DEFAULT_RANKING_WEIGHTS, w_m: base["w_m"]! + 1e-6};
    expect(isRankingWeights(drift)).toBe(false);
  });

  it("rejects a negative or out-of-range weight before it reaches the sum rule", () => {
    expect(isRankingWeights({...DEFAULT_RANKING_WEIGHTS, w_m: -0.15, w_d: 0.6})).toBe(false);
    expect(isRankingWeights({...DEFAULT_RANKING_WEIGHTS, w_m: 1.35, w_e: -0.15})).toBe(false);
  });
});

describe("normalization and penalties", () => {
  it("treats missing features as neutral rather than zero", () => {
    // D13/D14: zero would systematically punish every store the verifier has not reached yet.
    expect(DEFAULT_RANKING_WEIGHTS.normalization.delivery_fit_when_absent).toBe(0.5);
    expect(DEFAULT_RANKING_WEIGHTS.normalization.verified_claim_ratio_when_absent).toBe(0.5);
    expect(DEFAULT_RANKING_WEIGHTS.normalization.feature_min).toBe(0);
    expect(DEFAULT_RANKING_WEIGHTS.normalization.feature_max).toBe(1);
  });

  it("publishes the D13 penalty catalogue and bounds the sum", () => {
    expect(totalPenalty(DEFAULT_RANKING_WEIGHTS, [])).toBe(0);
    expect(totalPenalty(DEFAULT_RANKING_WEIGHTS, ["severe_policy_violation"])).toBeCloseTo(0.3);
    expect(
      totalPenalty(DEFAULT_RANKING_WEIGHTS, Array(50).fill("severe_policy_violation")),
    ).toBeCloseTo(DEFAULT_RANKING_WEIGHTS.penalties.max_total_penalty);
  });

  it("gives an unpublished policy-event kind no penalty at all", () => {
    // The ranker reads penalties from the catalogue; it must not invent one for a new kind.
    expect(totalPenalty(DEFAULT_RANKING_WEIGHTS, ["kind-with-no-published-penalty"])).toBe(0);
  });

  it("publishes the tie-break order", () => {
    expect([...DEFAULT_RANKING_WEIGHTS.tie_breakers]).toEqual([
      "verified_hard_fit_count",
      "trust",
      "price",
      "bid_id",
    ]);
  });
});

describe("features 3.0.0 — the definitions the weights are applied to", () => {
  it("versions the feature definitions separately from the weights", () => {
    // A feature redefined under an unchanged weights version re-scores history silently:
    // every stored number still validates and every published weight still matches (R15/S3).
    expect(RANKING_FEATURES_VERSION).toBeTruthy();
    expect(RANKING_FEATURES_VERSION).not.toBe(RANKING_WEIGHTS_VERSION);
    // The major is a TRIPWIRE, not a description: redefining a published feature must not be
    // possible without a human editing this line. It read "2" from the commit that introduced
    // the constant (d4386a8) until D57 replaced `delivery_fit`'s FEED — the declared
    // `Offer.delivery_estimate_days`, a number the bidding store writes — with the same quote
    // divided by that store's `shipped_on_time` posterior, and made an unwatched store's
    // promise inadmissible. Same name, same weight, same neutral, different number, so a
    // replay must be able to see that the definitions moved.
    expect(RANKING_FEATURES_VERSION.split(".")[0]).toBe("3");
  });

  it("publishes intent_match's neutral without moving it", () => {
    const bounds = DEFAULT_RANKING_WEIGHTS.normalization;
    const midpoint = (bounds.feature_min + bounds.feature_max) / 2;
    expect(INTENT_MATCH_WHEN_ABSENT).toBe(midpoint);
  });

  it("prices a contradicted claim, bounded from both sides", () => {
    const perKind = DEFAULT_RANKING_WEIGHTS.penalties.per_kind as Record<string, number>;
    const penalty = perKind[CONTRADICTED_CLAIM]!;
    const mapped = featureWeights(DEFAULT_RANKING_WEIGHTS);
    const bounds = DEFAULT_RANKING_WEIGHTS.normalization;
    // Large enough: more than the whole evidence term can pay, so true evidence cannot buy
    // out one lie. Small enough: below severe_policy_violation and below the terms a store
    // recovers on, so one bad verdict is survivable.
    expect(penalty).toBeCloseTo(0.15);
    const mostEvidenceCanPay =
      mapped["verified_claim_ratio"]! *
      (bounds.feature_max - bounds.verified_claim_ratio_when_absent);
    expect(penalty).toBeGreaterThan(mostEvidenceCanPay);
    expect(penalty).toBeLessThan(perKind["severe_policy_violation"]!);
    expect(penalty).toBeLessThan(mapped["trust"]!);
    expect(totalPenalty(DEFAULT_RANKING_WEIGHTS, [CONTRADICTED_CLAIM, CONTRADICTED_CLAIM])).toBeCloseTo(
      2 * penalty,
    );
  });

  it("aggregates evidence with diminishing returns that never saturate", () => {
    const neutral = DEFAULT_RANKING_WEIGHTS.normalization.verified_claim_ratio_when_absent;
    expect(Object.keys(EVIDENCE_GAIN_BY_RELEVANCE)).toEqual([...RELEVANCE_TIERS]);
    // Every tier beats silence on ONE claim, or the term pays a store to say nothing.
    for (const tier of RELEVANCE_TIERS) {
      expect(EVIDENCE_GAIN_BY_RELEVANCE[tier]!).toBeGreaterThan(neutral);
    }
    const gain = EVIDENCE_GAIN_BY_RELEVANCE["hard_constraint"]!;
    const values = [0, 1, 2, 3, 4, 5].map((n) => diminishingEvidence(Array(n).fill(gain)));
    expect(values[0]).toBe(0);
    const steps = values.slice(1).map((value, index) => value! - values[index]!);
    for (const step of steps) expect(step).toBeGreaterThan(0);
    for (let i = 1; i < steps.length; i += 1) expect(steps[i]!).toBeLessThan(steps[i - 1]!);
    expect(values[values.length - 1]!).toBeLessThan(DEFAULT_RANKING_WEIGHTS.normalization.feature_max);
    // Order-independent (R11, S3 replay): multiplication commutes.
    const tiers = RELEVANCE_TIERS.map((tier) => EVIDENCE_GAIN_BY_RELEVANCE[tier]!);
    expect(diminishingEvidence(tiers)).toBeCloseTo(diminishingEvidence([...tiers].reverse()), 12);
    expect(diminishingEvidence([0.6, 0, -1, Number.NaN])).toBeCloseTo(0.6);
  });

  it("refuses every preference field a published term already scores", () => {
    // S1's intent carries one preference, {price, minimize, 1.0}: admitted into intent_match
    // it IS normalised inverse price, and price's weight goes from w_v 0.15 to w_v+w_m 0.50.
    const mapped = featureWeights(DEFAULT_RANKING_WEIGHTS);
    expect(mapped["price_value"]! + mapped["intent_match"]!).toBeCloseTo(0.5);
    for (const term of Object.values(PREFERENCE_FIELD_TERMS)) {
      expect(RANK_FEATURES as readonly string[]).toContain(term);
    }
    for (const field of Object.keys(PREFERENCE_FIELD_TERMS)) {
      expect(canonicalField(field)).toBe(field);
    }
    for (const spelling of ["price", "Price USD", "price_usd", "list_price"]) {
      expect(preferenceTermConflict(spelling)).toBe("price_value");
    }
    for (const spelling of ["delivery", "delivery_estimate_days", "Shipping Speed"]) {
      expect(preferenceTermConflict(spelling)).toBe("delivery_fit");
    }
    for (const spelling of ["capacity_l", "material", "boiler_type"]) {
      expect(preferenceTermConflict(spelling)).toBeUndefined();
    }
  });
});
