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

/**
 * The version of the FEATURE DEFINITIONS the published weights are applied to.
 *
 * `2.0.0` because two features were REDEFINED rather than retuned: `verified_claim_ratio` became
 * buyer-conditional evidence and `price_value` became saturating at the auction's own price band.
 * Both kept their published name and weight, so `RANKING_WEIGHTS_VERSION` did not move — which is
 * exactly why this second version exists. R15/S3 promise a replay reproduces served scores, and a
 * feature redefined under an unchanged weights version breaks that silently: every recorded number
 * still validates and every published weight still matches.
 */
export const RANKING_FEATURES_VERSION = "2.0.0";

/**
 * What `intent_match` reads when it is ABSENT.
 *
 * Always 0.5, but until now inherited from the scorer's `[feature_min, feature_max]` midpoint and
 * published nowhere — so nobody could tell a COMPUTED 0.5 (an exactly average fit) from an ABSENT
 * one (a served auction is handed a roster and queries no index). `NormalizationBounds` publishes
 * the same distinction for `delivery_fit` and `verified_claim_ratio`; its property set is closed,
 * so this one is published here.
 */
export const INTENT_MATCH_WHEN_ABSENT = 0.5;

/** The policy-event kind minted for a claim the exchange's own catalogue snapshot contradicts. */
export const CONTRADICTED_CLAIM = "contradicted_claim";

/**
 * D13's penalty catalogue. Published so the ranker cannot invent a penalty.
 *
 * `contradicted_claim` = 0.15, bounded from both sides: 1.5x the most the whole evidence term can
 * pay (`w_e * (1 - 0.5)` = 0.10), so no amount of true evidence buys out one lie, and equal to
 * `w_v` — one contradicted claim costs what clearing the auction's price band outright is worth.
 * Half of `severe_policy_violation`, and recoverable against `w_m` (0.35) and `w_t` (0.20), so one
 * bad verdict is serious rather than fatal.
 */
export const DEFAULT_PENALTIES_PER_KIND: Readonly<Record<string, number>> = {
  severe_policy_violation: 0.3,
  [CONTRADICTED_CLAIM]: 0.15,
};

/**
 * Relevance tier -> the share of the REMAINING headroom one verified claim at that tier takes.
 *
 * `verified_claim_ratio` aggregates as a noisy-OR, `1 - Π(1 - gain_i)`, over this candidate's
 * verified claims whose key lands on something in THIS buyer's intent. Diminishing (the second
 * relevant fact is worth a share of what is left), never saturating (the product never reaches 0),
 * order-independent (multiplication commutes, so R11 and S3 replay hold), and every tier is above
 * `verified_claim_ratio_when_absent` (0.5) so one relevant proved fact already beats silence.
 */
export const EVIDENCE_GAIN_BY_RELEVANCE: Readonly<Record<string, number>> = {
  hard_constraint: 0.7,
  preference: 0.6,
  query_term: 0.55,
};

/** Relevance tiers, strongest first. */
export const RELEVANCE_TIERS: readonly string[] = ["hard_constraint", "preference", "query_term"];

/**
 * Preference fields a PUBLISHED TERM ALREADY SCORES, and which term scores them.
 *
 * `intent_match` must refuse every field here. Measured hazard: S1's intent carries exactly one
 * preference, `{price, minimize, 1.0}`, so an `intent_match` wired naively over it IS normalised
 * inverse price and price's share of the published weight goes from `w_v` = 0.15 to
 * `w_v + w_m` = 0.50 — a price auction wearing a fit term's name.
 */
export const PREFERENCE_FIELD_TERMS: Readonly<Record<string, string>> = {
  price: "price_value",
  "price-usd": "price_value",
  "unit-price": "price_value",
  "total-price": "price_value",
  "list-price": "price_value",
  cost: "price_value",
  discount: "price_value",
  "discount-pct": "price_value",
  delivery: "delivery_fit",
  "delivery-days": "delivery_fit",
  "delivery-estimate-days": "delivery_fit",
  shipping: "delivery_fit",
  "shipping-days": "delivery_fit",
  "shipping-speed": "delivery_fit",
  trust: "trust",
  "trust-score": "trust",
};

/** `field` folded to the one spelling fields are compared in: lowercase, non-alphanumerics to `-`. */
export function canonicalField(field: string): string {
  return String(field)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

/** The published term that ALREADY scores `field`, or `undefined` when none does. */
export function preferenceTermConflict(field: string): string | undefined {
  return PREFERENCE_FIELD_TERMS[canonicalField(field)];
}

/** `1 - Π(1 - gain)` — the published aggregation for verified, buyer-relevant evidence. */
export function diminishingEvidence(gains: readonly number[]): number {
  let remaining = 1;
  for (const gain of gains) {
    if (!Number.isFinite(gain) || gain <= 0) continue;
    remaining *= 1 - Math.min(gain, 1);
  }
  return 1 - remaining;
}

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
