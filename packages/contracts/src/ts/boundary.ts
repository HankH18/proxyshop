/**
 * The dual-path bid boundary (R8 / R18 / S5), in TypeScript.
 *
 * Deliberately a line-for-line peer of `contracts/boundary.py`: same reason codes, same ordering,
 * same fail-closed rules. A boundary that admitted different bids depending on which language the
 * caller happened to be written in would not be a boundary. `tests/boundary.test.ts` checks the
 * TypeScript verdicts against the same table the Python tests use.
 */
import type {Bid, BidValidationResult, LedgerEvent} from "../../generated/ts/protocol.schema.d.ts";
import {validationErrors} from "./schemas.js";
import {missingSigningFields} from "./signing.js";

export type BidPathName = "hosted" | "external";

export const HOSTED_PATH = "hosted" as const;
export const EXTERNAL_PATH = "external" as const;
export const BID_PATHS: readonly BidPathName[] = [HOSTED_PATH, EXTERNAL_PATH];

/**
 * R8: the six provenance sources a store-agent can only mint by calling a tool hook (T-040 pins the
 * hook→source table). Anything else in a hosted bid means the hooks were bypassed.
 */
export const HOOK_PROVENANCE_SOURCES: ReadonlySet<string> = new Set([
  "scraped",
  "pixel_feed",
  "owner_statement",
  "envelope_rule",
  "learned_policy",
  "network",
]);

/** The only source no hook produces — an assertion the seller made in free text. */
export const NON_HOOK_PROVENANCE_SOURCES: ReadonlySet<string> = new Set(["seller_asserted"]);

export const REASON_UNKNOWN_PATH = "unknown_path";
export const REASON_SCHEMA_INVALID = "schema_invalid";
export const REASON_CLAIM_WITHOUT_PROVENANCE = "claim_without_provenance";
export const REASON_CLAIM_PROVENANCE_EMPTY_SOURCE = "claim_provenance_empty_source";
export const REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE = "claim_provenance_unknown_source";
export const REASON_HOSTED_NON_HOOK_PROVENANCE = "hosted_non_hook_provenance";
export const REASON_OFFER_EXPIRED = "offer_expired";
export const REASON_OFFER_EXPIRY_MISSING = "offer_expiry_missing";
export const REASON_OFFER_EXPIRY_UNPARSEABLE = "offer_expiry_unparseable";
export const REASON_STORE_BLACKLISTED = "store_blacklisted";
export const REASON_TRUST_SNAPSHOT_UNAVAILABLE = "trust_snapshot_unavailable";
export const REASON_SIGNING_ENVELOPE_INCOMPLETE = "signing_envelope_incomplete";
export const REASON_SIGNATURE_MISSING = "signature_missing";

export interface TrustSnapshotRow {
  store_id?: string;
  score?: number;
  blacklisted?: boolean;
}

export type TrustSnapshotMap = Record<string, TrustSnapshotRow | undefined>;

export interface ValidateBidOptions {
  path: string;
  trustSnapshot: TrustSnapshotMap;
  /** The instant expiry is judged against. Defaults to now; pass it to stay deterministic. */
  now?: Date | string | number;
  /**
   * When true, D52's five envelope fields and a non-empty `signature` are required, each gap
   * reported as its own reason. Off by default: `Bid` carries no envelope, and a hosted Tier-1
   * agent holds no key. `validateExternalSubmission` is this flag turned on.
   */
  requireSigningEnvelope?: boolean;
}

function readRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

/**
 * Parse an RFC-3339 instant, a `Z`-suffixed instant, or epoch seconds. `undefined` if unreadable.
 * An offset-less value is read as UTC: a boundary whose expiry depended on the host's timezone
 * would not be a contract.
 */
export function parseTimestamp(value: unknown): Date | undefined {
  if (value === null || value === undefined) return undefined;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? undefined : value;
  if (typeof value === "number" && Number.isFinite(value)) return new Date(value * 1000);
  if (typeof value !== "string" || value.trim() === "") return undefined;
  let text = value.trim();
  const hasZone = /(?:Z|z|[+-]\d{2}:?\d{2})$/.test(text);
  if (!hasZone) text = `${text}Z`;
  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed;
}

function claimProvenanceReasons(
  claims: unknown,
  path: BidPathName,
): {reasons: string[]; unverified: number[]} {
  const reasons: string[] = [];
  const unverified: number[] = [];
  if (claims === null || claims === undefined) return {reasons, unverified};
  if (!Array.isArray(claims)) return {reasons: [REASON_SCHEMA_INVALID], unverified};

  claims.forEach((claim, index) => {
    const record = readRecord(claim);
    const provenance = readRecord(record?.["provenance"]);
    if (record === undefined || record["provenance"] === null || record["provenance"] === undefined) {
      reasons.push(`${REASON_CLAIM_WITHOUT_PROVENANCE}:${index}`);
      return;
    }
    const source = String(provenance?.["source"] ?? "").trim();
    if (source === "") {
      reasons.push(`${REASON_CLAIM_PROVENANCE_EMPTY_SOURCE}:${index}`);
      return;
    }
    if (HOOK_PROVENANCE_SOURCES.has(source)) return;
    if (NON_HOOK_PROVENANCE_SOURCES.has(source)) {
      if (path === HOSTED_PATH) {
        // R8/S5: a hosted agent cannot mint this source through any hook.
        reasons.push(`${REASON_HOSTED_NON_HOOK_PROVENANCE}:${index}:${source}`);
      } else {
        // R18: admitted, but it goes to verification before it is shown as fact.
        unverified.push(index);
      }
      return;
    }
    reasons.push(`${REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE}:${index}:${source}`);
  });

  return {reasons, unverified};
}

function expiryReasons(offer: unknown, now: Date): string[] {
  const record = readRecord(offer);
  if (record === undefined) return [REASON_OFFER_EXPIRY_MISSING];
  const raw = record["expires_at"];
  if (raw === null || raw === undefined || (typeof raw === "string" && raw.trim() === "")) {
    // Fail closed. An offer with no stated expiry is an offer nobody can price the risk of.
    return [REASON_OFFER_EXPIRY_MISSING];
  }
  const expiresAt = parseTimestamp(raw);
  if (expiresAt === undefined) return [REASON_OFFER_EXPIRY_UNPARSEABLE];
  return expiresAt.getTime() <= now.getTime() ? [REASON_OFFER_EXPIRED] : [];
}

function eligibilityReasons(storeId: unknown, snapshot: TrustSnapshotMap): string[] {
  const table = readRecord(snapshot);
  if (table === undefined) return [REASON_TRUST_SNAPSHOT_UNAVAILABLE];
  const key = String(storeId ?? "");
  const row = readRecord(table[key]);
  // R12, fail-closed: blacklisted denies, and an unavailable read denies the same way.
  if (row === undefined) return [`${REASON_TRUST_SNAPSHOT_UNAVAILABLE}:${key}`];
  // TRUTHY, not `=== true`. A strict comparison admits a store whose row spells the flag `1` or
  // `"yes"` — the fail-OPEN direction, on the one check R12 exists to make fail closed. The
  // Python peer reads it the same way, and `boundary.test.ts` pins both spellings.
  if (row["blacklisted"]) return [`${REASON_STORE_BLACKLISTED}:${key}`];
  return [];
}

/**
 * D52: the five envelope fields plus a `signature`, or the submission is refused.
 *
 * Read off the raw submission, not off a validated `Bid`: `Bid` carries no envelope by design, so
 * a schema check against it can never see these fields.
 */
function signingEnvelopeReasons(bid: unknown): string[] {
  const record = readRecord(bid) ?? {};
  const reasons = missingSigningFields(record).map(
    (field) => `${REASON_SIGNING_ENVELOPE_INCOMPLETE}:${field}`,
  );
  const signature = record["signature"];
  if (typeof signature !== "string" || signature.trim() === "") {
    reasons.push(REASON_SIGNATURE_MISSING);
  }
  return reasons;
}

/**
 * Decide whether `bid` may enter the auction through `options.path`. Never throws.
 *
 * Does NOT check the signing envelope unless `options.requireSigningEnvelope` is set — see
 * `validateExternalSubmission`, which is the wire door.
 */
export function validateBid(bid: unknown, options: ValidateBidOptions): BidValidationResult {
  const evaluatedAt = parseTimestamp(options.now) ?? new Date();

  if (options.path !== HOSTED_PATH && options.path !== EXTERNAL_PATH) {
    return {
      ok: false,
      path: String(options.path),
      reasons: [`${REASON_UNKNOWN_PATH}:${String(options.path)}`],
      requires_verification: false,
      unverified_claim_indexes: [],
    };
  }
  const path: BidPathName = options.path;
  const reasons: string[] = [];

  // 1. Schema validity — refused on every path, before any field is interpreted. The model
  //    depends on what the caller says it is holding: `Bid` forbids extra keys, so validating a
  //    real D52 submission against it would report the four envelope fields as schema
  //    violations — the mirror image of admitting a bid that has none of them.
  const schemaProblems = validationErrors(
    options.requireSigningEnvelope ? "SignedBidSubmission" : "Bid",
    bid,
  );
  if (schemaProblems.length > 0) {
    reasons.push(`${REASON_SCHEMA_INVALID}:${schemaProblems.slice(0, 8).join(";")}`);
  }

  const record = readRecord(bid) ?? {};

  // 2. Provenance, per claim. Path-sensitive: this is the whole of R8/R18.
  const claims = claimProvenanceReasons(record["claims"], path);
  reasons.push(...claims.reasons);

  // 3. Offer expiry and 4. seller eligibility — path-insensitive.
  reasons.push(...expiryReasons(record["offer"], evaluatedAt));
  reasons.push(...eligibilityReasons(record["store_id"], options.trustSnapshot));

  // 5. D52's signing envelope, when the caller is the external door rather than a component
  //    judging an already-extracted `Bid`.
  if (options.requireSigningEnvelope) reasons.push(...signingEnvelopeReasons(bid));

  const ok = reasons.length === 0;
  return {
    ok,
    path,
    reasons,
    // A rejected bid is not "admitted pending verification": there is nothing to verify.
    requires_verification: ok && claims.unverified.length > 0,
    unverified_claim_indexes: ok ? claims.unverified : [],
  };
}

/**
 * The Tier-2 door: everything `validateBid` judges, plus D52's signing envelope.
 *
 * The function an exchange should call on a body arriving at
 * `POST /v1/auctions/{auction_id}/bids`. It refuses an unsigned submission with the same finality
 * as an expired offer or a blacklisted store, and, like `validateBid`, it never throws.
 */
export function validateExternalSubmission(
  submission: unknown,
  options: Omit<ValidateBidOptions, "path" | "requireSigningEnvelope">,
): BidValidationResult {
  return validateBid(submission, {
    ...options,
    path: EXTERNAL_PATH,
    requireSigningEnvelope: true,
  });
}

export type {Bid, BidValidationResult, LedgerEvent};
