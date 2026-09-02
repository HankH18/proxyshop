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
 * The one error a caller has to catch around the canonicalizer.
 *
 * The peer of `contracts.signing.CanonicalisationError`. Both languages raise exactly this for
 * every way a payload can fail to be canonicalizable, so a caller writing one `catch` is not
 * quietly missing four of the five failure modes.
 */
export class CanonicalisationError extends TypeError {
  override readonly name = "CanonicalisationError";
}

/**
 * RFC 8785 §3.1: a signable JSON number MUST be expressible as an IEEE-754 double.
 *
 * In JavaScript a `number` IS a double, so every finite `number` passes by construction and only
 * `bigint` can carry an integer this rule excludes — `9007199254740993n` has no double, and
 * writing it into signed bytes would cover a value the submission does not state. The Python peer
 * needs the explicit `float(v) == v` test because its `int` is arbitrary precision.
 */
function assertDoubleRepresentable(value: bigint): never {
  const asDouble = Number(value);
  const detail =
    BigInt(Number.isFinite(asDouble) ? Math.trunc(asDouble) : 0) === value
      ? `it would have to be written as the number ${String(asDouble)}, and a bigint is not a JSON number`
      : `${value.toString()} is not expressible as an IEEE-754 double (RFC 8785 §3.1)`;
  throw new CanonicalisationError(`canonicalJson cannot sign a bigint: ${detail}`);
}

/**
 * RFC-8785 canonical JSON: UTF-16-ordered keys, ECMAScript numbers, no insignificant whitespace.
 *
 * Written by hand rather than with `JSON.stringify(value, Object.keys(value).sort())`, because
 * that only sorts the TOP level — a nested `offer` object would serialize in insertion order and
 * two structurally identical payloads would sign differently.
 *
 * `tests/canonicalization_corpus.json` pins 27 inputs against the output of a reference
 * canonicalizer written straight from the RFC, and BOTH languages are checked against that same
 * file. That is what makes the Python `canonical_json` and this function one implementation
 * rather than two that happen to agree on the fixtures someone thought to try.
 */
export function canonicalJson(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    // `a < b` on strings is UTF-16 code-unit order, which is what RFC 8785 §3.2.3 requires.
    const entries = Object.entries(value as Payload)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(",")}}`;
  }
  if (typeof value === "bigint") assertDoubleRepresentable(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new CanonicalisationError("canonicalJson: non-finite numbers are not signable");
    }
    // RFC 8785 §3.2.2.3 defines number serialization as ECMAScript's, which is exactly `String`.
    // The Python side reimplements `Number::toString` for the same reason — `repr` is NOT it.
    return String(value);
  }
  if (typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  throw new CanonicalisationError(
    `canonicalJson cannot sign a ${typeof value}; a signed payload must be plain JSON so both ` +
      "sides can reproduce the bytes from the wire form alone",
  );
}

/** A digest over the bid BODY — everything except the envelope and the signature itself. */
export function payloadHash(payload: Payload): string {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new CanonicalisationError("payloadHash expects an object");
  }
  const body: Payload = {};
  for (const [key, value] of Object.entries(payload)) {
    if (!NON_BODY_KEYS.has(key)) body[key] = value;
  }
  const digest = createHash(PAYLOAD_HASH_ALGORITHM).update(canonicalJson(body), "utf8").digest("hex");
  return `${PAYLOAD_HASH_ALGORITHM}:${digest}`;
}

/**
 * Which of the five required envelope fields are absent or empty. Empty array means complete.
 *
 * Required BY TYPE, not merely by presence. All five are `string` in the schema, and a check that
 * only asked "is it null/undefined or blank?" reported `nonce: 0`, `nonce: false`, `signer_id: []`
 * and `issued_at: 12345` as present — every one of which the schema rejects. `nonce: false` is a
 * constant nonce, which is exactly what D52's replay defence exists to make impossible.
 */
export function missingSigningFields(payload: unknown): string[] {
  const record =
    typeof payload === "object" && payload !== null && !Array.isArray(payload)
      ? (payload as Payload)
      : undefined;
  if (record === undefined) return [...REQUIRED_SIGNING_FIELDS];
  return REQUIRED_SIGNING_FIELDS.filter((field) => {
    const value = record[field];
    return typeof value !== "string" || value.trim() === "";
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
    throw new CanonicalisationError(
      `cannot canonicalize a submission with an incomplete signing envelope; missing ` +
        `${missing.join(", ")} (D52: all of ${REQUIRED_SIGNING_FIELDS.join(", ")} are required)`,
    );
  }
  for (const field of ["auction_id", "store_id"] as const) {
    const value = payload[field];
    if (value === null || value === undefined || value === "") {
      throw new CanonicalisationError(
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
 *
 * Both ids must be strings: they arrive off the wire, and JS would happily index a keyring with
 * `String({})` — `"[object Object]"` — rather than refusing. A stored secret must be a non-empty
 * string; a row seeded with `""` (a placeholder, a truncated secret, a key cleared but not
 * deleted) is NOT a usable HMAC key, and returning it would make "no such key" and "this key"
 * the same answer. The Python peer reads it the same way.
 */
export function keyringSecret(
  keyring: Record<string, Record<string, string> | undefined>,
  signerId: string,
  keyId: string,
): string | undefined {
  if (typeof signerId !== "string" || typeof keyId !== "string") return undefined;
  if (typeof keyring !== "object" || keyring === null) return undefined;
  const signerKeys = keyring[signerId];
  if (typeof signerKeys !== "object" || signerKeys === null) return undefined;
  const secret = (signerKeys as Record<string, unknown>)[keyId];
  return typeof secret === "string" && secret !== "" ? secret : undefined;
}

export type {SignedBidSubmission, SigningEnvelope};
