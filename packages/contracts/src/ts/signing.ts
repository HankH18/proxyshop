/**
 * The external signing envelope and the one canonical form the signature covers (D52), in
 * TypeScript.
 *
 * A peer of `contracts/signing.py`, byte for byte: `canonicalSigningBytes` here and
 * `canonical_signing_bytes` there must produce the SAME bytes for the same submission, or a bid
 * signed by a Node seller could not be verified by a Python exchange. `tests/signing.test.ts`
 * pins the exact bytes against a fixture the Python tests pin too.
 */
import {createHash} from "node:crypto";

import type {SignedBidSubmission, SigningEnvelope} from "../../generated/ts/protocol.schema.d.ts";
import {validationErrors} from "./schemas.js";

/** D52: the five fields required on every external submission, in DESIGN order. */
export const REQUIRED_SIGNING_FIELDS = [
  "signer_id",
  "key_id",
  "issued_at",
  "nonce",
  "schema_version",
] as const;

/** The fields the signature covers. `payload_hash` is computed, not read off the payload. */
export const SIGNED_FIELDS = [
  "auction_id",
  "signer_id",
  "store_id",
  "issued_at",
  "nonce",
  "key_id",
  "schema_version",
] as const;

/** Excluded from the body digest: the envelope is covered in its own right, and a signature
 * cannot cover itself. */
const NON_BODY_KEYS: ReadonlySet<string> = new Set([
  "signer_id",
  "key_id",
  "issued_at",
  "nonce",
  "signature",
]);

export const PAYLOAD_HASH_ALGORITHM = "sha256";

export type Payload = Record<string, unknown>;

/**
 * RFC-8785-style canonical JSON: sorted keys, no insignificant whitespace.
 *
 * Written by hand rather than with `JSON.stringify(value, Object.keys(value).sort())`, because
 * that only sorts the TOP level — a nested `offer` object would serialize in insertion order and
 * two structurally identical payloads would sign differently.
 */
export function canonicalJson(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value as Payload)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(",")}}`;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("canonicalJson: non-finite numbers are not signable");
    // RFC 8785 §3.2.2.3 defines number serialization as ECMAScript's, which is what `String`
    // gives: `89.0` and `89` are the same amount and must sign identically. The Python side
    // normalizes integral floats to ints for exactly this reason — see `contracts.signing`.
    return String(value);
  }
  return JSON.stringify(value) ?? "null";
}

/** A digest over the bid BODY — everything except the envelope and the signature itself. */
export function payloadHash(payload: Payload): string {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new TypeError("payloadHash expects an object");
  }
  const body: Payload = {};
  for (const [key, value] of Object.entries(payload)) {
    if (!NON_BODY_KEYS.has(key)) body[key] = value;
  }
  const digest = createHash(PAYLOAD_HASH_ALGORITHM).update(canonicalJson(body), "utf8").digest("hex");
  return `${PAYLOAD_HASH_ALGORITHM}:${digest}`;
}

/** Which of the five required envelope fields are absent or empty. Empty array means complete. */
export function missingSigningFields(payload: unknown): string[] {
  const record =
    typeof payload === "object" && payload !== null && !Array.isArray(payload)
      ? (payload as Payload)
      : undefined;
  if (record === undefined) return [...REQUIRED_SIGNING_FIELDS];
  return REQUIRED_SIGNING_FIELDS.filter((field) => {
    const value = record[field];
    return value === null || value === undefined || (typeof value === "string" && value.trim() === "");
  });
}

/**
 * The exact bytes a submission's `signature` covers.
 * Throws when the envelope is incomplete: producing signing input for a submission that cannot
 * legally exist would just move the failure somewhere nobody is looking.
 */
export function canonicalSigningBytes(payload: Payload): Uint8Array {
  const missing = missingSigningFields(payload);
  if (missing.length > 0) {
    throw new Error(
      `cannot canonicalize a submission with an incomplete signing envelope; missing ` +
        `${missing.join(", ")} (D52: all of ${REQUIRED_SIGNING_FIELDS.join(", ")} are required)`,
    );
  }
  for (const field of ["auction_id", "store_id"] as const) {
    const value = payload[field];
    if (value === null || value === undefined || value === "") {
      throw new Error(
        `cannot canonicalize a submission without ${field} — it is a covered field, so a ` +
          "signature that omitted it could be lifted across auctions",
      );
    }
  }
  const covered: Payload = {};
  for (const field of SIGNED_FIELDS) covered[field] = payload[field];
  covered["payload_hash"] = payloadHash(payload);
  return new TextEncoder().encode(canonicalJson(covered));
}

/** Human-readable reasons a submission's envelope is unacceptable. Empty means acceptable. */
export function signingEnvelopeErrors(payload: unknown): string[] {
  return missingSigningFields(payload).map((field) => `missing required signing field: ${field}`);
}

/** True when `payload` is a complete, schema-valid external submission (D52). */
export function isSignedBidSubmission(payload: unknown): payload is SignedBidSubmission {
  return validationErrors("SignedBidSubmission", payload).length === 0;
}

/**
 * Look up `(signerId, keyId)` in the D52 keyring — nested `{signer_id: {key_id: secret}}`.
 *
 * The OUTER key is the signer, not the store: two signers may legitimately use the same `key_id`
 * string, so a lookup keyed on `key_id` alone is wrong. An unknown pair returns `undefined` and
 * never falls back to another key of that signer — falling back is what makes a revoked key still
 * work.
 */
export function keyringSecret(
  keyring: Record<string, Record<string, string> | undefined>,
  signerId: string,
  keyId: string,
): string | undefined {
  const signerKeys = keyring?.[signerId];
  if (typeof signerKeys !== "object" || signerKeys === null) return undefined;
  const secret = signerKeys[keyId];
  return typeof secret === "string" && secret !== "" ? secret : undefined;
}

export type {SignedBidSubmission, SigningEnvelope};
