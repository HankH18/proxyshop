/**
 * `RankingWeights` in TypeScript — the same versioned weight set, and the same rule about it.
 *
 * There is ONE rank formula (D12/D50):
 *
 *     rank_score = w_m*intent_match + w_e*verified_claim_ratio + w_t*trust
 *                + w_v*price_value + w_d*delivery_fit − policy_penalties
 *
 * The five weights must sum to 1.0. JSON Schema cannot say that, so both languages say it, and
 * `tests/ranking.test.ts` checks the identical fixture the Python tests check — a weight set that
 * is legal in one language and illegal in the other would be worse than no rule at all.
 */
import type {RankingWeights} from "../../generated/ts/protocol.schema.d.ts";
import {validationErrors} from "./schemas.js";

/** The five weight symbols, in the order DESIGN publishes them. */
export const WEIGHT_FIELDS = ["w_m", "w_e", "w_t", "w_v", "w_d"] as const;

/** The five features, aligned index-for-index with `WEIGHT_FIELDS`. */
export const RANK_FEATURES = [
  "intent_match",
  "verified_claim_ratio",
  "trust",
  "price_value",
  "delivery_fit",
] as const;

/** Tight enough to reject a real mistake, loose enough not to reject binary floating point. */
export const WEIGHT_SUM_TOLERANCE = 1e-9;

export const RANKING_WEIGHTS_VERSION = "1.0.0";

/** D13's initial penalty catalogue. Published so the ranker cannot invent a penalty. */
export const DEFAULT_PENALTIES_PER_KIND: Readonly<Record<string, number>> = {
  severe_policy_violation: 0.3,
};

/** D13's tie-break order, applied left to right. */
export const DEFAULT_TIE_BREAKERS: readonly string[] = [
  "verified_hard_fit_count",
  "trust",
  "price",
  "bid_id",
];

/** The published weight set. Import this rather than re-deriving the numbers. */
export const DEFAULT_RANKING_WEIGHTS: RankingWeights = Object.freeze({
  version: RANKING_WEIGHTS_VERSION,
  w_m: 0.35,
  w_e: 0.2,
  w_t: 0.2,
  w_v: 0.15,
  w_d: 0.1,
  normalization: {
    feature_min: 0,
    feature_max: 1,
    // NEUTRAL when absent, never 0 (D13/D14): a missing value is not evidence of a bad one, and
    // scoring it as 0 systematically punishes every store the verifier has not reached yet.
    delivery_fit_when_absent: 0.5,
    verified_claim_ratio_when_absent: 0.5,
  },
  penalties: {
    per_kind: {...DEFAULT_PENALTIES_PER_KIND},
    // Without a bound, one repeated policy-event kind drives rank_score arbitrarily negative and
    // the formula stops being a comparison between candidates.
    max_total_penalty: 1,
  },
  tie_breakers: [...DEFAULT_TIE_BREAKERS],
}) as RankingWeights;

/** Every reason `weights` is not a legal weight set. Empty array means it is. */
export function rankingWeightsErrors(weights: unknown): string[] {
  const problems = validationErrors("RankingWeights", weights);
  if (problems.length > 0) return problems;

  const record = weights as Record<string, number>;
  const total = WEIGHT_FIELDS.reduce((sum, field) => sum + Number(record[field]), 0);
  if (Math.abs(total - 1) > WEIGHT_SUM_TOLERANCE) {
    problems.push(
      `the five published rank weights must sum to 1.0; ` +
        `${WEIGHT_FIELDS.map((f) => `${f}=${record[f]}`).join("+")} = ${total}. ` +
        "A set that does not sum to 1.0 is rejected rather than renormalized, so the published " +
        "weights and the applied weights can never be different numbers.",
    );
  }
  return problems;
}

/** True when `weights` is a legal `RankingWeights`. */
export function isRankingWeights(weights: unknown): weights is RankingWeights {
  return rankingWeightsErrors(weights).length === 0;
}

/** Throw listing every problem when `weights` is not a legal weight set. */
export function assertRankingWeights(weights: unknown): asserts weights is RankingWeights {
  const problems = rankingWeightsErrors(weights);
  if (problems.length > 0) {
    throw new Error(`RankingWeights is invalid:\n  ${problems.join("\n  ")}`);
  }
}

/** `{feature_name: weight}` — the formula written out against the candidate record. */
export function featureWeights(weights: RankingWeights): Record<string, number> {
  const record = weights as unknown as Record<string, number>;
  return Object.fromEntries(
    RANK_FEATURES.map((feature, index) => [feature, Number(record[WEIGHT_FIELDS[index]!])]),
  );
}

/** Σ per-kind penalties over open policy events, clamped to `max_total_penalty`. */
export function totalPenalty(weights: RankingWeights, kinds: readonly string[]): number {
  const perKind = weights.penalties.per_kind as Record<string, number>;
  const total = kinds.reduce((sum, kind) => sum + (perKind[kind] ?? 0), 0);
  return Math.min(total, weights.penalties.max_total_penalty);
}

export type {RankingWeights};
