/**
 * The external signing envelope and the one canonical form the signature covers (D52), in
 * TypeScript.
 *
 * A peer of `contracts/signing.py`. The equivalence is stated in two halves, because it has two
 * halves and stating it as one was how a real split hid inside it for a whole wave (T-129):
 *
 *   1. **Given the same VALUE**, `canonicalSigningBytes` here and `canonical_signing_bytes`
 *      there produce the SAME bytes and refuse the same submissions — or a bid signed by a Node
 *      seller could not be verified by a Python exchange.
 *   2. **Given the same wire TEXT**, the two sides accept and refuse exactly the same integer
 *      literals.
 *
 * `tests/signing.test.ts` pins the exact bytes against fixtures the Python tests pin too, and
 * `e2e/test_jcs_conformance.py` grades BOTH halves across the language boundary — the value
 * half over the whole conformance corpus, the text half over `SIGNING_TEXT_CASES`. Until T-125
 * that file drove the renderers only, so the header could assert equivalence flatly while
 * `canonicalSigningBytes` refused every exact double at or beyond 2**53 that Python signed.
 *
 * The two halves are separate because the SURFACE is not symmetric, and that asymmetry is real
 * rather than an accident to be tidied away. Python's `int` is arbitrary precision, so
 * `json.loads` hands `canonical_json` the integer the wire actually spelled and RFC 8785 §3.1's
 * `float(v) == v` is a decidable question about the value. JavaScript has no integer type —
 * `JSON.parse` has already rounded before any guard can run — so the same rule is decidable
 * only on the literal. That is why `parseSignableJson` and the `…FromJson` doors exist here and
 * have no Python peer: in Python, `json.loads` followed by the ordinary door already IS the
 * text door.
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
 * RFC 8785 §3.1, enforced where JavaScript can still see the truth: at PARSE time.
 *
 * `assertDoubleRepresentable` only ever sees a `bigint`, and `JSON.parse` never produces one. By
 * the time a wire integer is a `number` the damage is already done and silent —
 * `JSON.parse('{"quantity":9007199254740993}')` yields `9007199254740992`, `2**64+1` yields
 * `18446744073709552000` and `10**23` yields `1e+23` — so `canonicalJson` would sign, and
 * `payloadHash` would digest, a value the submission does not state. The Python peer refuses all
 * three, because its `int` is arbitrary precision and `float(v) == v` is checkable after parsing.
 *
 * The only place TypeScript can still apply the same rule is the literal text. This walks the
 * raw JSON, skipping string contents, and round-trips every INTEGER literal through the double
 * it would become. Fractional and exponential literals are deliberately not checked: Python's
 * `json.loads` gives those to `float` too, so both sides already agree on them.
 */
function assertIntegerLiteralsAreDoubles(text: string): void {
  const QUOTE = 0x22;
  const BACKSLASH = 0x5c;
  const MINUS = 0x2d;
  const isDigit = (unit: number): boolean => unit >= 0x30 && unit <= 0x39;

  let index = 0;
  while (index < text.length) {
    const unit = text.charCodeAt(index);
    if (unit === QUOTE) {
      index += 1;
      while (index < text.length) {
        const inner = text.charCodeAt(index);
        if (inner === BACKSLASH) {
          index += 2; // an escape never ends a string, whatever it escapes
          continue;
        }
        index += 1;
        if (inner === QUOTE) break;
      }
      continue;
    }
    if (unit !== MINUS && !isDigit(unit)) {
      index += 1;
      continue;
    }
    const start = index;
    let integral = true;
    index += 1;
    while (index < text.length) {
      const next = text.charCodeAt(index);
      if (isDigit(next)) {
        index += 1;
        continue;
      }
      // `.`, `e`, `E` and the sign of an exponent are the only other characters a JSON number
      // may carry, and any one of them means this literal parses as a float on BOTH sides.
      if (next === 0x2e || next === 0x65 || next === 0x45 || next === 0x2b || next === MINUS) {
        integral = false;
        index += 1;
        continue;
      }
      break;
    }
    if (integral) assertIntegerLiteralRoundTrips(text.slice(start, index));
  }
}

/** One integer literal, checked against the double `JSON.parse` would hand back. */
function assertIntegerLiteralRoundTrips(literal: string): void {
  const asDouble = Number(literal);
  if (Number.isFinite(asDouble) && Number.isInteger(asDouble) && BigInt(asDouble) === BigInt(literal)) {
    return;
  }
  const delivered = Number.isFinite(asDouble) ? String(asDouble) : "Infinity";
  throw new CanonicalisationError(
    `parseSignableJson cannot accept the integer ${literal}: RFC 8785 §3.1 requires a JSON ` +
      "number to be expressible as an IEEE-754 double, and this one is not. JSON.parse would " +
      `deliver ${delivered}, and signing that would cover a value the submission does not state.`,
  );
}

/**
 * Parse wire JSON that is about to be signed or verified, refusing what the wire cannot state.
 *
 * It is the TypeScript half of the RFC 8785 §3.1 rule the Python peer enforces inside
 * `canonical_json`: an integer with no exact double is refused with `CanonicalisationError` —
 * the same error type, so a caller's one `catch` still covers it — rather than quietly becoming
 * a different number.
 *
 * `canonicalSigningBytesFromJson` and `payloadHashFromJson` call this for you, and are what a
 * caller holding wire text should reach for. Calling it directly is for a caller that wants the
 * parsed value for something other than signing; a caller who forgets it no longer signs a
 * coerced number, because the value-domain doors refuse the ambiguity outright.
 *
 * Malformed JSON is still a `SyntaxError`, unchanged: that is a parse failure, not a signing one.
 */
export function parseSignableJson(text: string): unknown {
  if (typeof text !== "string") {
    throw new CanonicalisationError(
      `parseSignableJson expects the raw JSON text, got ${typeof text}; the check it performs ` +
        "is only possible on the literal, which an already-parsed value has thrown away",
    );
  }
  const value: unknown = JSON.parse(text);
  assertIntegerLiteralsAreDoubles(text);
  return value;
}

/**
 * Why the signing doors carry no VALUE-domain integer guard, written down so it is not re-added.
 *
 * T-113 added one: `payloadHash` and `canonicalSigningBytes` refused an integer-valued `number`
 * at or beyond 2**53 by default, with `{allowUnsafeIntegers: true}` as the opt-out. Its
 * reasoning was true — above 2**53 the gap between adjacent doubles is at least 2, so several
 * wire literals collapse onto one value and the signer cannot know which was written — and the
 * guard was still wrong, because the rule it approximates is a rule about VALUES. RFC 8785 §3.1
 * asks whether the number is expressible as an IEEE-754 double, and every finite JavaScript
 * `number` is one by construction. `10**16` satisfies `float(v) == v`, Python's
 * `canonical_signing_bytes` signs it, `canonicalJson` renders it, the conformance gate requires
 * that — and the guard refused it, so a Node seller could not sign a bid a Python exchange
 * signs happily. That is the divergence class T-106's gate exists to close, re-opened in the
 * one door the gate did not watch (T-125).
 *
 * There is nothing narrower to refuse, either. At `10**16` the gap is 2, so
 * `10000000000000001` collapses onto the same value: the ambiguity IS the magnitude, and any
 * value-domain test that refuses the ambiguity refuses the exact double along with it. A guard
 * cannot be both correct about RFC 8785 §3.1 and suspicious of a large double.
 *
 * So the rule lives where the evidence still exists — `assertIntegerLiteralsAreDoubles`, on the
 * raw text, reached through `parseSignableJson` — and `canonicalSigningBytesFromJson` /
 * `payloadHashFromJson` are the doors a caller holding wire material must use. A caller that
 * runs `JSON.parse` itself and then signs is signing whatever `JSON.parse` produced; that is a
 * property of `JSON.parse`, and moving the refusal into the signing door never fixed it — it
 * only broke the protocol for every exact double above 2**53.
 */

/**
 * RFC 8785 §3.2.2.2: a lone surrogate MUST terminate canonicalisation with an error.
 *
 * A JS string is a sequence of UTF-16 code units, so a well-formed astral character is a HIGH
 * surrogate followed by a LOW one; anything else is unpaired. `JSON.stringify` does NOT refuse
 * these — since ES2019 it emits them as escaped hex and succeeds — which made this the more
 * dangerous half of a cross-language mismatch: a Node seller could sign a bid whose `message` or
 * `Claim.value` held a lone surrogate (`Claim.value` is unconstrained, so it is trivially
 * reachable) and the Python exchange raised `UnicodeEncodeError` at the public door instead of
 * returning a rejection. §3.2.2.2 says both were wrong; both now raise.
 *
 * Hand-rolled rather than `String.prototype.isWellFormed()` so the rule does not depend on the
 * runtime's ES2024 support, and so the error can name the offending index.
 */
function rejectLoneSurrogates(text: string, what: string): void {
  for (let i = 0; i < text.length; i += 1) {
    const unit = text.charCodeAt(i);
    if (unit < 0xd800 || unit > 0xdfff) continue;
    const isHigh = unit <= 0xdbff;
    const next = isHigh ? text.charCodeAt(i + 1) : Number.NaN;
    if (isHigh && next >= 0xdc00 && next <= 0xdfff) {
      i += 1; // a well-formed pair: one real character, keep going
      continue;
    }
    throw new CanonicalisationError(
      `canonicalJson cannot sign ${what} containing a lone surrogate ` +
        `(U+${unit.toString(16).toUpperCase().padStart(4, "0")} at index ${i}): RFC 8785 ` +
        "§3.2.2.2 requires a compliant implementation to terminate with an error rather than " +
        "emit bytes for it",
    );
  }
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
    for (const [key] of entries) rejectLoneSurrogates(key, "an object key");
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
  if (typeof value === "string") {
    rejectLoneSurrogates(value, "a string");
    return JSON.stringify(value);
  }
  if (typeof value === "boolean") return JSON.stringify(value);
  throw new CanonicalisationError(
    `canonicalJson cannot sign a ${typeof value}; a signed payload must be plain JSON so both ` +
      "sides can reproduce the bytes from the wire form alone",
  );
}

/**
 * A digest over the bid BODY — everything except the envelope and the signature itself.
 *
 * Digests exactly what `canonicalJson` renders and refuses exactly what it refuses, which is
 * what makes this the peer of `contracts.signing.payload_hash`: the same body produces the same
 * digest in both languages, including for an exact double above 2**53.
 */
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
 * `payloadHash` from the raw wire text, which is the form RFC 8785 §3.1 can still be decided on.
 *
 * The door for anything that arrived over a network, and the only one that can tell
 * `9007199254740993` (refused, named in the message) from `9007199254740992` (an exact double,
 * digested — the Python peer digests it too). Its scope is the INTEGER literals: a fractional
 * or exponential literal is left to the float rules both languages already share, because
 * `json.loads` gives Python a `float` for those too. So `1e16` passes here on its spelling and
 * `10000000000000000` passes on its round trip, and both reach the same double — which is why
 * this door and `payloadHash` now agree on it rather than splitting, as they did while a
 * value-domain guard was switched off here and on there.
 */
export function payloadHashFromJson(text: string): string {
  return payloadHash(parseSignableJson(text) as Payload);
}

/**
 * Every code point the envelope's blank rule treats as empty. The SAME set
 * `schemas/protocol.schema.json` enumerates in the five envelope fields' `pattern`, and the twin
 * of `contracts.signing.BLANK_CODE_POINTS`.
 *
 * Enumerated rather than left to `String.prototype.trim()`, and enumerated in the schema rather
 * than spelled with the `\s` shorthand, because both of those are ENGINE-DEPENDENT. Rust's regex
 * crate (what pydantic compiles) and Python's `re` read that shorthand as Unicode White_Space,
 * which includes U+0085; ECMAScript (what Ajv compiles) reads it as a fixed list that excludes
 * U+0085 and includes U+FEFF. Measured over all of Unicode the split was exactly those two
 * characters — so the one artifact both languages read described two different rules, which is
 * the single thing a shared contract may not do.
 *
 * This set is the UNION of both readings, so it relaxes neither gate: everything either side
 * already called blank still is. `tests/signing.test.ts` walks every code point and fails if this
 * set and the pattern Ajv compiled ever disagree.
 */
const BLANK_CODE_POINTS: ReadonlySet<number> = new Set([
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, // tab, LF, VT, FF, CR
  0x1c, 0x1d, 0x1e, 0x1f, // the four information separators — `trim()` keeps these
  0x20, // SPACE
  0x85, // NEL — Unicode White_Space, but not ECMAScript whitespace
  0xa0, // NBSP
  0x1680, // OGHAM SPACE MARK
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, // LINE SEPARATOR
  0x2029, // PARAGRAPH SEPARATOR
  0x202f, // NARROW NO-BREAK SPACE
  0x205f, // MEDIUM MATHEMATICAL SPACE
  0x3000, // IDEOGRAPHIC SPACE
  0xfeff, // ZWNBSP — ECMAScript whitespace, but not Unicode White_Space
]);

/**
 * True when `value` holds no character the envelope's schema `pattern` would accept.
 *
 * Iterated by CODE POINT, so an astral character is one item and can never be mistaken for two
 * code units that happen not to be listed. An empty string is blank, which is what `minLength: 1`
 * says about it too.
 */
function isBlank(value: string): boolean {
  for (const character of value) {
    if (!BLANK_CODE_POINTS.has(character.codePointAt(0) ?? -1)) return false;
  }
  return true;
}

/**
 * Which of the five required envelope fields are absent or empty. Empty array means complete.
 *
 * Required BY TYPE, not merely by presence. All five are `string` in the schema, and a check that
 * only asked "is it null/undefined or blank?" reported `nonce: 0`, `nonce: false`, `signer_id: []`
 * and `issued_at: 12345` as present — every one of which the schema rejects. `nonce: false` is a
 * constant nonce, which is exactly what D52's replay defence exists to make impossible.
 *
 * "Empty" is `isBlank`, not `minLength` and not `trim()`. The schema used to say `minLength: 1`
 * alone, so `"   "` satisfied it while this function called the same value missing; the schema
 * now enumerates the same code points `BLANK_CODE_POINTS` does, and both refuse it — including
 * U+0085 and U+FEFF, the two the old engine-dependent spelling split the languages on.
 */
export function missingSigningFields(payload: unknown): string[] {
  const record =
    typeof payload === "object" && payload !== null && !Array.isArray(payload)
      ? (payload as Payload)
      : undefined;
  if (record === undefined) return [...REQUIRED_SIGNING_FIELDS];
  return REQUIRED_SIGNING_FIELDS.filter((field) => {
    const value = record[field];
    return typeof value !== "string" || isBlank(value);
  });
}

/**
 * The exact bytes a submission's `signature` covers.
 *
 * Throws when the envelope is incomplete: producing signing input for a submission that cannot
 * legally exist would just move the failure somewhere nobody is looking. Otherwise it accepts
 * exactly what `contracts.signing.canonical_signing_bytes` accepts and emits the same bytes —
 * `e2e/test_jcs_conformance.py` drives both doors over the same corpus to keep that true.
 * `canonicalSigningBytesFromJson` is the door for material that arrived as text, where the RFC
 * 8785 §3.1 literal rule is still decidable.
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
  // No number check on `covered`, and T-113's comment claiming one was needed here was wrong on
  // its own terms — the claim, not just the guard (T-128). `NON_BODY_KEYS` is exactly
  // {signer_id, key_id, issued_at, nonce, signature}, so `auction_id` and `store_id` are INSIDE
  // the body `payloadHash` builds and walks, not outside it; and the four `SIGNED_FIELDS` that
  // ARE outside the body — signer_id, key_id, issued_at, nonce — are four of the five
  // `missingSigningFields` has already rejected the submission over unless they are non-blank
  // STRINGS, which cannot hold a number at all. A check here could refuse nothing the body
  // walk does not already reach, which is what deleting it proved: all 586 vitest cases stayed
  // green. A guard that cannot refuse anything is not a guard.
  covered["payload_hash"] = payloadHash(payload);
  return new TextEncoder().encode(canonicalJson(covered));
}

/**
 * `canonicalSigningBytes` from the raw wire text — the door for anything off a network.
 *
 * This is the function a Node seller signing a body, or a Node exchange verifying one, should
 * call, and the one place TypeScript can still apply RFC 8785 §3.1: the literal check runs
 * because the door runs it, not because the caller remembered to. `9007199254740993` is refused
 * here with the literal named; `9007199254740992` and `10000000000000000` — exact doubles,
 * which the Python peer signs — are not, and neither is refused by `canonicalSigningBytes`
 * either, because a value-domain guard cannot tell those two classes apart (T-125).
 */
export function canonicalSigningBytesFromJson(text: string): Uint8Array {
  return canonicalSigningBytes(parseSignableJson(text) as Payload);
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
