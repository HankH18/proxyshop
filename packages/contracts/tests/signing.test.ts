/**
 * The signing envelope and the canonical bytes, in TypeScript.
 *
 * `EXPECTED_CANONICAL_BYTES` below is pinned to the SAME string as
 * `tests/test_signing_envelope.py::EXPECTED_CANONICAL_BYTES`. That single shared constant is what
 * makes the two implementations one protocol rather than two: without it, each language would
 * only be checked against itself, and a bid signed by a Node seller could stop verifying at the
 * Python exchange with nothing failing anywhere.
 */
import {describe, expect, it} from "vitest";

import {
  REQUIRED_SIGNING_FIELDS,
  SIGNED_FIELDS,
  canonicalJson,
  canonicalSigningBytes,
  isSignedBidSubmission,
  keyringSecret,
  missingSigningFields,
  payloadHash,
  signingEnvelopeErrors,
} from "../src/ts/signing.js";
import {isValid} from "../src/ts/schemas.js";
import {makeBid, makeOffer, makeSubmission} from "./fixtures.js";

/** Pinned identically in the Python suite. Changing one without the other is the bug. */
const EXPECTED_CANONICAL_BYTES =
  '{"auction_id":"auc-0100","issued_at":"2026-01-01T00:00:00Z","key_id":"key-2026-01",' +
  '"nonce":"nonce-ext-0001",' +
  '"payload_hash":"sha256:3879714cea211d2b69e088a8535f30d00f7f571b8a38a21b0728466159cbd512",' +
  '"schema_version":"1.0.0","signer_id":"store-external-1","store_id":"store-external-1"}';

const decode = (bytes: Uint8Array) => new TextDecoder().decode(bytes);

describe("D52 — the five envelope fields are required", () => {
  it("names exactly the five D52 fields", () => {
    expect([...REQUIRED_SIGNING_FIELDS]).toEqual([
      "signer_id",
      "key_id",
      "issued_at",
      "nonce",
      "schema_version",
    ]);
  });

  it("validates a complete submission", () => {
    // The positive control. Without it the rejections below would prove nothing.
    expect(isSignedBidSubmission(makeSubmission())).toBe(true);
  });

  it.each([...REQUIRED_SIGNING_FIELDS])("rejects a submission missing %s", (field) => {
    const payload = makeSubmission();
    delete payload[field];
    expect(isSignedBidSubmission(payload)).toBe(false);
  });

  it.each([...REQUIRED_SIGNING_FIELDS])("rejects an empty %s", (field) => {
    expect(isSignedBidSubmission(makeSubmission({[field]: ""}))).toBe(false);
  });

  it("reports every gap at once", () => {
    const payload = makeSubmission();
    delete payload["nonce"];
    payload["key_id"] = "  ";
    expect(new Set(missingSigningFields(payload))).toEqual(new Set(["nonce", "key_id"]));
    expect(missingSigningFields(makeSubmission())).toEqual([]);
    expect(signingEnvelopeErrors(makeSubmission())).toEqual([]);
  });

  it("does not accept a plain Bid as an external submission", () => {
    expect(isSignedBidSubmission(makeBid())).toBe(false);
    // ...and the plain Bid is still a valid Bid: the envelope is a property of the submission.
    expect(isValid("Bid", makeBid())).toBe(true);
  });
});

describe("the canonical signing bytes", () => {
  it("is deterministic, order-independent and stable across a JSON round trip", () => {
    const payload = makeSubmission();
    const base = decode(canonicalSigningBytes(payload));
    expect(decode(canonicalSigningBytes(payload))).toBe(base);

    const reordered = Object.fromEntries(Object.entries(payload).reverse());
    expect(decode(canonicalSigningBytes(reordered))).toBe(base);
    expect(decode(canonicalSigningBytes(JSON.parse(JSON.stringify(payload))))).toBe(base);
  });

  it("matches the bytes the Python implementation produces", () => {
    expect(decode(canonicalSigningBytes(makeSubmission()))).toBe(EXPECTED_CANONICAL_BYTES);
  });

  it("covers every signed field verbatim, and the body digest", () => {
    const payload = makeSubmission();
    const text = decode(canonicalSigningBytes(payload));
    for (const field of SIGNED_FIELDS) {
      expect(text, `the signing bytes do not cover ${field}`).toContain(String(payload[field]));
    }
    expect(text).toContain(payloadHash(payload));
  });

  it.each([
    ["auction_id", {auction_id: "auc-0999"}],
    ["signer_id", {signer_id: "store-external-2"}],
    ["store_id", {store_id: "store-external-2"}],
    ["issued_at", {issued_at: "2026-01-01T00:00:01Z"}],
    ["nonce", {nonce: "nonce-ext-0002"}],
    ["key_id", {key_id: "key-2026-07"}],
    ["schema_version", {schema_version: "2"}],
  ])("changes when %s changes", (_label, mutation) => {
    expect(decode(canonicalSigningBytes(makeSubmission(mutation)))).not.toBe(
      EXPECTED_CANONICAL_BYTES,
    );
  });

  it("changes when the offer body changes", () => {
    const mutated = makeSubmission({offer: makeOffer({unit_price: 88.0})});
    expect(decode(canonicalSigningBytes(mutated))).not.toBe(EXPECTED_CANONICAL_BYTES);
  });

  it.each([...REQUIRED_SIGNING_FIELDS])(
    "refuses to canonicalize a submission missing %s",
    (field) => {
      const payload = makeSubmission();
      delete payload[field];
      expect(() => canonicalSigningBytes(payload)).toThrow(/incomplete signing envelope/);
    },
  );

  it.each(["auction_id", "store_id"])("refuses to canonicalize without %s", (field) => {
    expect(() => canonicalSigningBytes(makeSubmission({[field]: ""}))).toThrow();
  });
});

describe("the payload hash", () => {
  it("is a real digest, stable across a JSON round trip and key order", () => {
    const payload = makeSubmission();
    const digest = payloadHash(payload);
    expect(digest.startsWith("sha256:")).toBe(true);
    expect(digest.length).toBeGreaterThanOrEqual(32);
    expect(payloadHash(JSON.parse(JSON.stringify(payload)))).toBe(digest);
    expect(payloadHash(Object.fromEntries(Object.entries(payload).reverse()))).toBe(digest);
  });

  it("changes when the bid body changes", () => {
    const base = payloadHash(makeSubmission());
    expect(payloadHash(makeSubmission({offer: makeOffer({unit_price: 88.0})}))).not.toBe(base);
    expect(payloadHash(makeSubmission({message: "something else entirely"}))).not.toBe(base);
  });

  it("ignores the envelope and the signature", () => {
    // The envelope rides in the signing bytes in its own right, and a signature cannot cover
    // itself.
    const base = payloadHash(makeSubmission());
    expect(payloadHash(makeSubmission({nonce: "nonce-ext-0002"}))).toBe(base);
    expect(payloadHash(makeSubmission({signature: "sig-whatever"}))).toBe(base);
  });
});

describe("canonical JSON", () => {
  it("writes integral numbers the way RFC 8785 requires", () => {
    // 89.0 and 89 are the same number; the Python side normalizes to match.
    expect(canonicalJson({n: 89.0})).toBe('{"n":89}');
    expect(canonicalJson({n: 44.1})).toBe('{"n":44.1}');
    expect(canonicalJson({b: true})).toBe('{"b":true}');
    expect(canonicalJson({b: [1.0, {c: 2.0}]})).toBe('{"b":[1,{"c":2}]}');
  });

  it("sorts nested keys, not only the top level", () => {
    // `JSON.stringify(v, Object.keys(v).sort())` only sorts the top level, which would let two
    // structurally identical payloads sign differently.
    expect(canonicalJson({b: {z: 1, a: 2}, a: 3})).toBe('{"a":3,"b":{"a":2,"z":1}}');
  });

  it("refuses non-finite numbers", () => {
    expect(() => canonicalJson({n: Number.NaN})).toThrow();
    expect(() => canonicalJson({n: Number.POSITIVE_INFINITY})).toThrow();
  });
});

describe("the keyring", () => {
  const keyring = {
    "store-external-1": {"key-2026-01": "secret-1", "key-2026-07": "secret-2"},
    "store-external-2": {"key-2026-01": "secret-3"},
  };

  it("is indexed by signer, then key", () => {
    expect(keyringSecret(keyring, "store-external-1", "key-2026-01")).toBe("secret-1");
    expect(keyringSecret(keyring, "store-external-1", "key-2026-07")).toBe("secret-2");
    // Both signers use the key_id "key-2026-01": a lookup on key_id alone cannot pass this.
    expect(keyringSecret(keyring, "store-external-2", "key-2026-01")).toBe("secret-3");
  });

  it("never falls back to another key of the same signer", () => {
    // Falling back is precisely what would make a revoked key still work.
    expect(keyringSecret(keyring, "store-external-1", "key-2027-01")).toBeUndefined();
    expect(keyringSecret(keyring, "store-external-9", "key-2026-01")).toBeUndefined();
    expect(keyringSecret({}, "store-external-1", "key-2026-01")).toBeUndefined();
  });
});
