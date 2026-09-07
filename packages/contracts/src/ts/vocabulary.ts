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
  DenialCode,
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

/**
 * T-204: the nine codes a 409 `denial_reason` on `POST /auctions/{auction_id}/accept` begins with.
 *
 * The served field is `<code>` or `<code>: <free diagnostic prose>` — measured off a booted
 * exchange, `"blacklisted: chargeback fraud"` and
 * `"checkout_refused: RuntimeError: merchant declined to mint"` — so this list matches the TOKEN
 * `denialCode` reads out, never the whole string.
 */
export const DENIAL_CODES: readonly DenialCode[] = [
  "already_accepted",
  "auction_not_acceptable",
  "blacklisted",
  "checkout_refused",
  "unavailable",
  "unknown_bid",
  "unrecordable_acceptance",
  "unroutable_fallback",
  "unspecified",
];

/**
 * The blank code points the exchange strips from the token before matching it.
 *
 * This is Python's `str.strip()` set, which is what `exchange.accept.reasons.denial_code` uses,
 * and it is the same class the published `pattern` spells out. `trim()` is NOT this rule and
 * disagrees in both directions: it keeps U+001C–U+001F and U+0085, which Python removes, and it
 * removes U+FEFF, which Python keeps. `tests/denial_reason_corpus.json` carries a row for every
 * one of them, generated from the exchange's own parser, so a `trim()`-based rewrite of
 * `denialCode` fails the suite rather than mis-labelling a refusal in a buyer's browser.
 *
 * Deliberately NOT `contracts.signing`'s `BLANK_CODE_POINTS`: that set includes U+FEFF, because
 * the signing envelope's rule is the union of what three engines call whitespace. This one is one
 * engine's `strip()`, and the difference is exactly the U+FEFF rows in the corpus.
 */
const DENIAL_BLANK_CODE_POINTS: ReadonlySet<number> = new Set([
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, // tab, LF, VT, FF, CR
  0x1c, 0x1d, 0x1e, 0x1f, 0x20, // the four information separators, and SPACE
  0x85, // NEL — Python whitespace, not ECMAScript whitespace
  0xa0, // NBSP
  0x1680, // OGHAM SPACE MARK
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, // LINE SEPARATOR
  0x2029, // PARAGRAPH SEPARATOR
  0x202f, // NARROW NO-BREAK SPACE
  0x205f, // MEDIUM MATHEMATICAL SPACE
  0x3000, // IDEOGRAPHIC SPACE
]);

function stripDenialBlanks(token: string): string {
  let start = 0;
  let end = token.length;
  while (start < end && DENIAL_BLANK_CODE_POINTS.has(token.charCodeAt(start))) start += 1;
  while (end > start && DENIAL_BLANK_CODE_POINTS.has(token.charCodeAt(end - 1))) end -= 1;
  return token.slice(start, end);
}

/**
 * The declared code a 409 `denial_reason` begins with, or `null` when it begins with none.
 *
 * The published parser, mirroring `exchange.accept.reasons.denial_code` expression for
 * expression: everything before the FIRST colon, stripped, matched against {@link DENIAL_CODES}.
 * `null` is the answer that matters — the exchange re-publishes an undeclared refusal under
 * `unspecified`, so a `null` here means the caller is holding something that never came from
 * this endpoint.
 *
 * The colon is bare: the exchange writes `": "` itself but forwards a seller-eligibility
 * source's reason verbatim when it already starts with a declared code, so `blacklisted:no
 * space` and a bare `blacklisted:` are values a client really receives.
 *
 * Everything after that colon is diagnosis for a human — an auction id, a refused host, an
 * exception class — and no client should parse it.
 */
export function denialCode(reason: unknown): DenialCode | null {
  // `str(reason or "")` on the Python side: a falsy value is the empty string, and anything
  // else is stringified before the split.
  const text = reason ? String(reason) : "";
  const separator = text.indexOf(":");
  const token = stripDenialBlanks(separator === -1 ? text : text.slice(0, separator));
  return (DENIAL_CODES as readonly string[]).includes(token) ? (token as DenialCode) : null;
}

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
