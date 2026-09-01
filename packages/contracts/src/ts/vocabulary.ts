/**
 * The closed vocabularies, as runtime values.
 *
 * The generated `.d.ts` gives these as union TYPES, which vanish at run time. A renderer that has
 * to decide "is this string one of the six trust dimensions?" needs the values, not the type, so
 * they are published here once — and `tests/vocabulary.test.ts` asserts each list is exactly the
 * enum in the schema bundle, so the two cannot drift.
 */
import type {
  ClaimVerificationStatus,
  ConstraintOp,
  EnvelopeActivation,
  LedgerEventKind,
  PreferenceDirection,
  ProvenanceSource,
  ShortlistSlotName,
  StoreTier,
  TrustDimension,
} from "../../generated/ts/protocol.schema.d.ts";

/** C11/D24: the 18 frozen ledger event kinds, extended exactly once in T-010. */
export const LEDGER_EVENT_KINDS: readonly LedgerEventKind[] = [
  "bid_placed",
  "shown",
  "accepted",
  "code_created",
  "checkout_redirect",
  "checkout_pixel",
  "order_paid",
  "order_fulfilled",
  "refund",
  "feedback",
  "reconciled",
  "claim_verified",
  "policy_event",
  "auction_opened",
  "auction_closed",
  "offer_integrity",
  "blacklisted",
  "blacklist_expired",
];

/** D24: the pinned pixel↔webhook join keys, snake_case, exactly these names. */
export const LEDGER_JOIN_KEYS = [
  "checkout_token",
  "order_ref",
  "client_id",
  "discount_code",
] as const;

/** D53: SIX dimensions inside ONE trust system. */
export const TRUST_DIMENSIONS: readonly TrustDimension[] = [
  "price_honored",
  "discount_honored",
  "shipped_on_time",
  "not_returned",
  "feedback_match",
  "catalog_claim_accuracy",
];

export const PROVENANCE_SOURCES: readonly ProvenanceSource[] = [
  "scraped",
  "pixel_feed",
  "owner_statement",
  "envelope_rule",
  "learned_policy",
  "network",
  "seller_asserted",
];

/** R8: the six sources only a tool hook can mint. */
export const HOOK_PROVENANCE_SOURCES: readonly ProvenanceSource[] = [
  "scraped",
  "pixel_feed",
  "owner_statement",
  "envelope_rule",
  "learned_policy",
  "network",
];

export const CONSTRAINT_OPS: readonly ConstraintOp[] = ["eq", "lte", "gte", "in", "contains"];

export const PREFERENCE_DIRECTIONS: readonly PreferenceDirection[] = [
  "maximize",
  "minimize",
  "prefer",
];

export const ENVELOPE_ACTIVATIONS: readonly EnvelopeActivation[] = ["shadow", "active", "killed"];

export const SHORTLIST_SLOT_NAMES: readonly ShortlistSlotName[] = [
  "fit",
  "value",
  "reliability",
  "specialist",
];

/** R18: the verification badge a seller-asserted claim carries instead of a provenance label. */
export const CLAIM_VERIFICATION_STATUSES: readonly ClaimVerificationStatus[] = [
  "verified",
  "contradicted",
  "unsupported",
  "ambiguous",
];

/** D28: 0 = catalog-only, 1 = network-hosted agent, 2 = external agent behind the signed door. */
export const STORE_TIERS: readonly StoreTier[] = [0, 1, 2];

/** SPEC R2 fixes exactly two buyer-facing provenance label strings. */
export const LABEL_STORE_CONFIRMED = "store-confirmed";
export const LABEL_FROM_THEIR_WEBSITE = "from their website";

/** Not an R2 provenance label: a seller-asserted claim surfaces the R18 verification badge. */
export const LABEL_UNVERIFIED = "unverified";

/**
 * D30's provenance → buyer-label map, published once so the exchange (producer) and the buyer app
 * (renderer) cannot drift. Total over `ProvenanceSource`: every source has an answer.
 *
 * NOTE: this follows the frozen acceptance suite, which maps `pixel_feed`, `learned_policy` and
 * `network` to "store-confirmed"; D30's prose maps `pixel_feed` to "from their website" and leaves
 * the other two unlabelled. The suite is the authority. See `contracts/labels.py`.
 */
export const PROVENANCE_BUYER_LABELS: Readonly<Record<ProvenanceSource, string>> = Object.freeze({
  owner_statement: LABEL_STORE_CONFIRMED,
  envelope_rule: LABEL_STORE_CONFIRMED,
  learned_policy: LABEL_STORE_CONFIRMED,
  pixel_feed: LABEL_STORE_CONFIRMED,
  network: LABEL_STORE_CONFIRMED,
  scraped: LABEL_FROM_THEIR_WEBSITE,
  seller_asserted: LABEL_UNVERIFIED,
});

/** D30: 1 is the most authoritative, larger is weaker. */
export const PROVENANCE_AUTHORITY_RANK: Readonly<Record<ProvenanceSource, number>> = Object.freeze({
  owner_statement: 1,
  envelope_rule: 1,
  pixel_feed: 2,
  network: 3,
  learned_policy: 3,
  scraped: 4,
  seller_asserted: 5,
});

/** The buyer-facing label for a provenance source. Throws for a source outside the pinned enum. */
export function buyerLabel(source: string): string {
  const label = (PROVENANCE_BUYER_LABELS as Record<string, string>)[source];
  if (label === undefined) {
    throw new Error(
      `no buyer label is published for provenance source ${JSON.stringify(source)}; ` +
        `the pinned sources are ${PROVENANCE_SOURCES.join(", ")}`,
    );
  }
  return label;
}
