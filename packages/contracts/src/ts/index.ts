/**
 * `@proxyshop/contracts` — the protocol every service agrees on, as TypeScript types and runtime
 * validators.
 *
 * Both halves come from one source of truth,
 * `packages/contracts/schemas/protocol.schema.json`: the types are generated from it and the
 * validators compile it directly. A TypeScript consumer and a Python consumer therefore accept
 * and reject exactly the same payloads.
 */
export type * from "../../generated/ts/protocol.schema.d.ts";

export {
  PROTOCOL_SCHEMA_ID,
  assertValid,
  isValid,
  protocolSchema,
  schemaFor,
  schemaNames,
  validationErrors,
} from "./schemas.js";

export {
  BID_PATHS,
  EXTERNAL_PATH,
  HOOK_PROVENANCE_SOURCES,
  HOSTED_PATH,
  NON_HOOK_PROVENANCE_SOURCES,
  REASON_CLAIM_PROVENANCE_EMPTY_SOURCE,
  REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE,
  REASON_CLAIM_WITHOUT_PROVENANCE,
  REASON_HOSTED_NON_HOOK_PROVENANCE,
  REASON_OFFER_EXPIRED,
  REASON_OFFER_EXPIRY_MISSING,
  REASON_OFFER_EXPIRY_UNPARSEABLE,
  REASON_SCHEMA_INVALID,
  REASON_SIGNATURE_MISSING,
  REASON_SIGNING_ENVELOPE_INCOMPLETE,
  REASON_STORE_BLACKLISTED,
  REASON_TRUST_SNAPSHOT_UNAVAILABLE,
  REASON_UNKNOWN_PATH,
  parseTimestamp,
  validateBid,
  validateExternalSubmission,
} from "./boundary.js";
export type {BidPathName, TrustSnapshotMap, TrustSnapshotRow, ValidateBidOptions} from "./boundary.js";

export {
  CanonicalisationError,
  PAYLOAD_HASH_ALGORITHM,
  REQUIRED_SIGNING_FIELDS,
  SIGNED_FIELDS,
  canonicalJson,
  canonicalSigningBytes,
  canonicalSigningBytesFromJson,
  isSignedBidSubmission,
  keyringSecret,
  missingSigningFields,
  parseSignableJson,
  payloadHash,
  payloadHashFromJson,
  signingEnvelopeErrors,
} from "./signing.js";
// `SigningNumberOptions` is gone with the value-domain guard it configured (T-125/T-129): the
// signing doors now accept every integer that satisfies `float(v) == v`, exactly as
// `canonicalJson` and the Python peer do, so there is nothing left for a caller to opt out of.
export type {Payload} from "./signing.js";

export {
  DEFAULT_PENALTIES_PER_KIND,
  DEFAULT_RANKING_WEIGHTS,
  DEFAULT_TIE_BREAKERS,
  RANKING_WEIGHTS_VERSION,
  RANK_FEATURES,
  WEIGHT_FIELDS,
  WEIGHT_SUM_TOLERANCE,
  assertRankingWeights,
  featureWeights,
  isRankingWeights,
  rankingWeightsErrors,
  totalPenalty,
} from "./ranking.js";

export {
  CLAIM_VERIFICATION_STATUSES,
  CONSTRAINT_OPS,
  DENIAL_CODES,
  ENVELOPE_ACTIVATIONS,
  LABEL_FROM_THEIR_WEBSITE,
  LABEL_STORE_CONFIRMED,
  LABEL_UNVERIFIED,
  LEDGER_EVENT_KINDS,
  LEDGER_JOIN_KEYS,
  PREFERENCE_DIRECTIONS,
  PROVENANCE_AUTHORITY_RANK,
  PROVENANCE_BUYER_LABELS,
  PROVENANCE_SOURCES,
  SHORTLIST_SLOT_NAMES,
  STORE_TIERS,
  TRUST_DIMENSIONS,
  buyerLabel,
  denialCode,
} from "./vocabulary.js";

export {
  PINNED_ROUTES,
  documents,
  exampleErrors,
  examples,
  routes,
} from "./openapi.js";
export type {OpenApiExample, Route} from "./openapi.js";
