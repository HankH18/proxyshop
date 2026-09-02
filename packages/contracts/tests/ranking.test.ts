/**
 * `RankingWeights` in TypeScript — the same numbers and the same rule the Python suite asserts.
 *
 * The rule is that a set which does not sum to 1.0 is REJECTED, not renormalized. Renormalizing
 * looks harmless and means the published weights and the applied weights are different numbers,
 * with nothing recording the difference.
 */
import {describe, expect, it} from "vitest";

import {
  DEFAULT_RANKING_WEIGHTS,
  RANKING_WEIGHTS_VERSION,
  RANK_FEATURES,
  WEIGHT_FIELDS,
  WEIGHT_SUM_TOLERANCE,
  assertRankingWeights,
  featureWeights,
  isRankingWeights,
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
