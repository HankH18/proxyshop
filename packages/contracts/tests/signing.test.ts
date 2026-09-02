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
  CanonicalisationError,
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
} from "../src/ts/signing.js";
import type {Payload} from "../src/ts/signing.js";
import {isValid, protocolSchema} from "../src/ts/schemas.js";
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

// ----------------------------------------------------------------------------------------------
// Cross-language canonicalization, pinned against a reference implementation.
//
// `canonicalization_corpus.json` was produced by a canonicalizer written straight from RFC 8785,
// with ECMAScript as the authority for number formatting (§3.2.2.3) and member ordering (§3.2.3).
// BOTH language implementations are checked against that same file — `test_signing_envelope.py`
// runs the identical corpus — which is what makes them one implementation rather than two that
// happen to agree on whatever fixtures someone thought to try.
// ----------------------------------------------------------------------------------------------
import corpus from "./canonicalization_corpus.json" with {type: "json"};

describe("the RFC 8785 corpus", () => {
  it("is present and substantial", () => {
    // Guards the cases below against passing vacuously on an empty file.
    expect(corpus.length).toBeGreaterThanOrEqual(25);
  });

  it.each(corpus.map((c) => [c.expected.slice(0, 48), c] as const))(
    "canonicalizes %s",
    (_label, testCase) => {
      expect(canonicalJson(testCase.input)).toBe(testCase.expected);
    },
  );
});

describe("canonicalization edge cases the fixtures do not reach", () => {
  it.each([
    [89.0, "89"],
    [44.1, "44.1"],
    [0, "0"],
    [-0, "0"],
    [1e-6, "0.000001"],
    [1e-7, "1e-7"],
    [0.00001, "0.00001"],
    [1e21, "1e+21"],
    [1e16, "10000000000000000"],
    [1.5e-10, "1.5e-10"],
    [5e-324, "5e-324"],
    [1.7976931348623157e308, "1.7976931348623157e+308"],
  ])("writes %s as %s", (value, expected) => {
    // Every one of these is a magnitude where Python's `repr` gives a DIFFERENT string, so the
    // two sides only agree because the Python peer implements Number::toString rather than repr.
    expect(canonicalJson({n: value})).toBe(`{"n":${expected}}`);
  });

  it("orders keys by UTF-16 code unit, not code point", () => {
    // Python's `<` compares code points and disagrees above the BMP; its peer encodes to UTF-16.
    expect(canonicalJson({"Ｚ": 1, "\u{1F600}": 2})).toBe('{"\u{1F600}":2,"Ｚ":1}');
  });

  it("refuses a value it cannot canonicalize", () => {
    expect(() => canonicalJson({n: () => 1})).toThrow();
    expect(() => canonicalJson({n: Symbol("x")})).toThrow();
  });
});

describe("F3 — the five envelope fields are required BY TYPE, not merely by presence", () => {
  it.each(
    REQUIRED_SIGNING_FIELDS.flatMap((field) =>
      [0, 1, false, true, 12345, [], {}, ["x"], {a: 1}, 1.5, null].map(
        (value) => [field, value] as const,
      ),
    ),
  )("counts %s = %s as missing", (field, value) => {
    // All five are `string` in the schema, and `isSignedBidSubmission` rejects every one of these.
    // A check that only asked "is it null/undefined or blank?" called them present — and
    // `nonce: false` is a CONSTANT nonce, the thing D52's replay defence exists to make
    // impossible.
    const payload = makeSubmission({[field]: value});
    expect(missingSigningFields(payload), `${field}=${JSON.stringify(value)}`).toContain(field);
    expect(isSignedBidSubmission(payload)).toBe(false);
  });

  it.each(
    REQUIRED_SIGNING_FIELDS.flatMap((field) =>
      [0, false, [], {}, 12345].map((value) => [field, value] as const),
    ),
  )("refuses to canonicalize %s = %s", (field, value) => {
    // The end-to-end consequence: `{issued_at: 12345}` used to produce signing bytes reading
    // `"issued_at":12345`, over a submission the schema would never have admitted.
    expect(() => canonicalSigningBytes(makeSubmission({[field]: value}))).toThrow(
      /incomplete signing envelope/,
    );
  });
});

describe("F5 / F2 — the keyring guard", () => {
  const keyring = {
    "store-external-1": {
      "key-good": "secret-1",
      "key-empty": "",
      "key-blank": "   ",
      "key-null": null,
      "key-num": 12345,
      "key-arr": ["secret-1"],
      "key-bool": true,
    },
  } as never;

  it.each(["key-empty", "key-null", "key-num", "key-arr", "key-bool"])(
    "does not treat %s as a usable HMAC key",
    (keyId) => {
      // A row seeded with "" — a placeholder, a truncated secret, a key cleared but not deleted
      // — must read as "no such key". The Python peer is pinned the same way.
      expect(keyringSecret(keyring, "store-external-1", keyId)).toBeUndefined();
    },
  );

  it("still returns the real secret, and a whitespace-only one", () => {
    expect(keyringSecret(keyring, "store-external-1", "key-good")).toBe("secret-1");
    // The contract is "non-empty string", not "looks like a key". Pinned so the edge is explicit.
    expect(keyringSecret(keyring, "store-external-1", "key-blank")).toBe("   ");
  });

  it.each([
    [{a: 1}, "key-good"],
    [["x"], "key-good"],
    ["store-external-1", {a: 1}],
    ["store-external-1", null],
    [null, null],
    [12345, 67890],
  ])("returns undefined for a non-string id pair (%s, %s)", (signerId, keyId) => {
    // JS would otherwise index the keyring with `String({})` — "[object Object]" — rather than
    // refusing. The Python peer raises `TypeError: unhashable type` without the same guard.
    expect(keyringSecret(keyring, signerId as never, keyId as never)).toBeUndefined();
  });
});

describe("F7 — a non-mapping is not an acceptable envelope", () => {
  it.each(["a raw string", "", ["signer_id", "key_id"], 42, null, undefined, true])(
    "reports five errors for %s",
    (payload) => {
      // `[]` means "acceptable envelope": a caller trusting an empty list would admit a bare
      // string or a JSON array as a signed submission.
      expect(signingEnvelopeErrors(payload)).toHaveLength(5);
      expect(missingSigningFields(payload)).toEqual([...REQUIRED_SIGNING_FIELDS]);
    },
  );

  it("reports zero errors for a complete submission", () => {
    expect(signingEnvelopeErrors(makeSubmission())).toEqual([]);
  });
});

describe("RFC 8785 §3.1 — the number rule", () => {
  it.each([
    [Number(2n ** 53n - 1n), "9007199254740991"],
    [Number(2n ** 53n), "9007199254740992"],
    [1e16, "10000000000000000"],
    [Number(2n ** 63n), "9223372036854776000"],
    [1e21, "1e+21"],
    [-(2 ** 53), "-9007199254740992"],
  ])("canonicalizes the exact double %s as %s", (value, expected) => {
    // A JS `number` IS a double, so every finite one is representable by construction. These are
    // the same six values the Python peer pins, so the two land on ONE rule rather than two
    // bounds that happen to agree in the middle.
    expect(canonicalJson({n: value})).toBe(`{"n":${expected}}`);
  });

  it.each([2n ** 53n + 1n, 12345678901234567890n, 10n ** 400n, -(2n ** 53n) - 1n, 0n])(
    "refuses the bigint %s",
    (value) => {
      // `bigint` is the only way a non-double integer can reach this function in JS. Writing one
      // into signed bytes would cover a value the submission does not state — the same defect the
      // Python peer had when it silently coerced past 2**53-1.
      expect(() => canonicalJson({n: value})).toThrow(CanonicalisationError);
    },
  );

  it("has no way to express a non-representable integer as a number", () => {
    // The structural reason TS needed no coercion fix: the literal 9007199254740993 IS
    // 9007199254740992 by the time it is a value. The rule still has to be pinned, because the
    // bigint door is open.
    // Built from a string, not written as a literal: `no-loss-of-precision` rejects the literal
    // form, which is itself the point being made.
    expect(Number("9007199254740993")).toBe(Number(2n ** 53n));
    expect(JSON.parse('{"n":9007199254740993}').n).toBe(Number(2n ** 53n));
    expect(Number.isSafeInteger(2 ** 53)).toBe(false);
    // ...and 2**53 is nonetheless an exact double that must canonicalize, which is exactly what
    // a safe-integer bound gets wrong.
    expect(canonicalJson({n: 2 ** 53})).toBe('{"n":9007199254740992}');
  });
});

describe("one exception type for every canonicalisation failure", () => {
  it.each([
    ["a function", () => canonicalJson({n: () => 1})],
    ["a symbol", () => canonicalJson({n: Symbol("x")})],
    ["undefined at the root", () => canonicalJson(undefined)],
    ["NaN", () => canonicalJson({n: Number.NaN})],
    ["Infinity", () => canonicalJson({n: Number.POSITIVE_INFINITY})],
    ["a bigint", () => canonicalJson({n: 2n ** 53n + 1n})],
    ["payloadHash of a non-object", () => payloadHash("nope" as never)],
    ["an incomplete envelope", () => canonicalSigningBytes(makeBid())],
  ])("raises CanonicalisationError for %s", (_label, call) => {
    expect(call).toThrow(CanonicalisationError);
  });

  it("still matches the catch clauses callers already wrote", () => {
    // Narrowing a public exception type is a breaking change; the Python peer derives from both
    // ValueError and TypeError for the same reason.
    expect(new CanonicalisationError("x")).toBeInstanceOf(TypeError);
    expect(new CanonicalisationError("x")).toBeInstanceOf(Error);
  });
});

describe("the corpus itself", () => {
  it("has 27 cases and 27 distinct inputs", () => {
    // The file advertised 27 and had 26 distinct: entries 3 and 4 were both `{"n": 0}`, because
    // the intended `-0` case round-tripped through JSON as `0` when the file was written. `-0`
    // was therefore untested in BOTH languages, and `length >= 25` could not see it.
    // The replacer is load-bearing: `JSON.stringify(-0)` is "0", so a plain stringify would
    // report the fixed corpus as still holding a duplicate.
    const rendered = corpus.map((c) =>
      JSON.stringify(c.input, (_key, value) => (Object.is(value, -0) ? "-0" : value)),
    );
    expect(new Set(rendered).size).toBe(corpus.length);
    expect(corpus.length).toBe(27);
  });

  it("contains a real negative zero", () => {
    const negativeZeros = corpus.filter((c) => {
      const n = (c.input as Record<string, unknown>)["n"];
      return typeof n === "number" && Object.is(n, -0);
    });
    expect(negativeZeros).toHaveLength(1);
    expect(canonicalJson(negativeZeros[0]!.input)).toBe('{"n":0}');
  });
});

// ----------------------------------------------------------------------------------------------
// RFC 8785 §3.2.2.2 — lone surrogates terminate with an error, in BOTH languages.
//
// The cases are written as WIRE text and parsed, because that is exactly how one arrives. This
// list is the same list the Python suite parametrizes, so the two cannot drift apart again.
// ----------------------------------------------------------------------------------------------
const LONE_SURROGATE_WIRE = [
  '"\\ud800"',
  '"\\udfff"',
  '"a\\ud83dz"',
  '"\\udc00\\ud800"',
  '"caf\\u00e9 \\udbff"',
];

describe("RFC 8785 §3.2.2.2 — lone surrogates", () => {
  it.each(LONE_SURROGATE_WIRE)("refuses %s as a string value", (wire) => {
    // `JSON.stringify` does NOT refuse these — it emits escaped hex and succeeds — which made
    // this the more dangerous half of the mismatch: TS produced bytes where Python raised
    // `UnicodeEncodeError` at the public door. §3.2.2.2 says both were wrong.
    const value = JSON.parse(wire);
    expect(() => canonicalJson({s: value})).toThrow(/lone surrogate/);
    expect(() => canonicalJson({s: value})).toThrow(CanonicalisationError);
    expect(() => canonicalJson([value])).toThrow(/lone surrogate/);
  });

  it.each(LONE_SURROGATE_WIRE)("refuses %s as an object KEY", (wire) => {
    expect(() => canonicalJson({[JSON.parse(wire)]: 1})).toThrow(/lone surrogate/);
  });

  it("keeps a lone surrogate out of the signing bytes entirely", () => {
    const payload = makeSubmission({message: JSON.parse('"\\ud800"')});
    expect(() => canonicalSigningBytes(payload)).toThrow(/lone surrogate/);
    expect(() => payloadHash(payload)).toThrow(/lone surrogate/);
  });

  it("still signs well-formed astral characters", () => {
    // The control. An emoji is a surrogate PAIR here and one code point in Python; refusing it
    // would break every bid whose message contains one and put the two languages back out of
    // step.
    expect(canonicalJson({s: "\u{1F600}"})).toBe('{"s":"\u{1F600}"}');
    expect(canonicalJson({"\u{1F600}": 1, "Ｚ": 2})).toBe('{"\u{1F600}":1,"Ｚ":2}');
    expect(canonicalJson({s: JSON.parse('"\\ud83d\\ude00"')})).toBe('{"s":"\u{1F600}"}');
  });

  it("agrees with the Python peer on every one of these inputs", () => {
    // Both suites drive the SAME wire list to the SAME outcome — a typed refusal. That equality
    // is the point: a differential fuzz over 4044 wire inputs found exactly this class of
    // mismatch, 4 cases, all of them lone surrogates.
    for (const wire of LONE_SURROGATE_WIRE) {
      expect(() => canonicalJson({s: JSON.parse(wire)})).toThrow(CanonicalisationError);
    }
  });
});

// --- T-103: RFC 8785 §3.1 at the door JavaScript actually loses precision at -------------

/**
 * Integer literals that have NO exact double, paired with what `JSON.parse` silently delivers
 * instead. Written as wire text, never as JS literals: the whole defect is that the literal form
 * is already the wrong number by the time it is a value, so a test written with literals could
 * not state the input it means.
 *
 * The Python peer refuses all of these inside `canonical_json`
 * (`tests/test_signing_envelope.py`), and refused them while TypeScript signed the coercion —
 * which is a Node seller signing a quantity its own submission does not state.
 */
const NON_DOUBLE_INTEGER_WIRE: ReadonlyArray<readonly [string, string]> = [
  ["9007199254740993", "9007199254740992"], // 2**53 + 1
  ["18446744073709551617", "18446744073709552000"], // 2**64 + 1
  ["100000000000000000000000", "1e+23"], // 10**23
  ["-9007199254740993", "-9007199254740992"],
  ["123456789012345678901234567890", "1.2345678901234568e+29"],
];

/** Integers that ARE exact doubles and must keep parsing. Without these the rule could be
 * satisfied by refusing every large integer, which would break real bids. */
const EXACT_DOUBLE_INTEGER_WIRE: readonly string[] = [
  "0",
  "-0",
  "1",
  "-42",
  "9007199254740992", // 2**53 — outside the safe-integer range and still exact
  "18446744073709551616", // 2**64
  "10000000000000000", // 10**16, which a safe-integer bound wrongly rejects
];

describe("RFC 8785 §3.1 — parsing refuses integers the wire cannot state", () => {
  it.each(NON_DOUBLE_INTEGER_WIRE)("refuses the integer literal %s", (literal) => {
    expect(() => parseSignableJson(`{"quantity":${literal}}`)).toThrow(CanonicalisationError);
    expect(() => parseSignableJson(`{"quantity":${literal}}`)).toThrow(/IEEE-754 double/);
  });

  it.each(NON_DOUBLE_INTEGER_WIRE)(
    "would otherwise have signed %s as %s",
    (literal, coerced) => {
      // The defect, pinned as the reason the guard exists: plain `JSON.parse` hands back a
      // DIFFERENT number and `canonicalJson` writes it into the signed bytes without complaint.
      const parsed = JSON.parse(`{"quantity":${literal}}`) as {quantity: number};
      expect(canonicalJson(parsed)).toBe(`{"quantity":${coerced}}`);
      expect(String(parsed.quantity)).not.toBe(literal);
    },
  );

  it.each(NON_DOUBLE_INTEGER_WIRE)("refuses %s nested anywhere in the payload", (literal) => {
    expect(() => parseSignableJson(`[{"offer":{"quantity":${literal}}}]`)).toThrow(
      CanonicalisationError,
    );
    expect(() => parseSignableJson(`[1,[2,[${literal}]]]`)).toThrow(CanonicalisationError);
    expect(() => parseSignableJson(literal)).toThrow(CanonicalisationError);
  });

  it.each(EXACT_DOUBLE_INTEGER_WIRE)("still parses the exact double %s", (literal) => {
    const parsed = parseSignableJson(`{"quantity":${literal}}`) as {quantity: number};
    expect(parsed.quantity).toBe(Number(literal));
  });

  it("does not mistake digits inside strings for numbers", () => {
    // The scan walks raw text, so it has to know where strings end — including a string whose
    // last character is an escaped quote, and one holding an escaped backslash.
    const wire =
      '{"note":"9007199254740993","escaped":"a\\"9007199254740993","tail":"b\\\\","n":1}';
    expect(parseSignableJson(wire)).toEqual({
      note: "9007199254740993",
      escaped: 'a"9007199254740993',
      tail: "b\\",
      n: 1,
    });
  });

  it("leaves fractional and exponential literals to the float rules both sides share", () => {
    // `json.loads` gives Python a `float` for these too, so there is nothing to disagree about:
    // 0.1 is the same double in both languages, and `1e400` is Infinity in both.
    expect(parseSignableJson('{"a":0.1,"b":1e-5,"c":1.5e300}')).toEqual({
      a: 0.1,
      b: 1e-5,
      c: 1.5e300,
    });
    expect(canonicalJson(parseSignableJson('{"b":1e-5}'))).toBe('{"b":0.00001}');
  });

  it("refuses an integer too large for a double at all", () => {
    expect(() => parseSignableJson(`{"n":${"9".repeat(400)}}`)).toThrow(CanonicalisationError);
  });

  it("still reports malformed JSON as a SyntaxError, not a canonicalisation failure", () => {
    // A parse failure is not a signing failure, and collapsing the two would tell a caller the
    // wrong thing about a truncated request body.
    expect(() => parseSignableJson('{"a":')).toThrow(SyntaxError);
    expect(() => parseSignableJson('{"a":')).not.toThrow(CanonicalisationError);
  });

  it("refuses a non-string argument rather than parsing its coercion", () => {
    expect(() => parseSignableJson(42 as never)).toThrow(CanonicalisationError);
  });

  it("round-trips a real submission unchanged", () => {
    // The positive control: the guard must not change what a legal submission parses to, or the
    // canonical bytes would move and every existing signature with them.
    const payload = makeSubmission();
    const wire = JSON.stringify(payload);
    expect(canonicalSigningBytes(parseSignableJson(wire) as Payload)).toEqual(
      canonicalSigningBytes(payload),
    );
    expect(decode(canonicalSigningBytes(parseSignableJson(wire) as Payload))).toBe(
      EXPECTED_CANONICAL_BYTES,
    );
  });

  it("keeps the coerced quantity out of the payload hash entirely", () => {
    // The end-to-end consequence. Without the guard `payloadHash` digests 9007199254740992 and
    // the signature covers a quantity the seller never wrote.
    const wire = JSON.stringify({...makeSubmission(), quantity: 0}).replace(
      '"quantity":0',
      '"quantity":9007199254740993',
    );
    expect(() => parseSignableJson(wire)).toThrow(CanonicalisationError);
  });
});

// --- T-107: the same canonicalizer now defines `claim_id` ---------------------------------

describe("a JS peer and the Python `claim_id` render the same material identically", () => {
  /** Byte-for-byte the strings `tests/test_claim_identity.py::JCS_DIVERGENCES` pins. Those are
   * the two cases where `json.dumps(sort_keys=True)` — what `claim_id` used to hash — differs
   * from RFC 8785, so this is where a JS peer used to compute a different id for the same
   * claim. Pinning the same literals in both suites is what makes that one definition. */
  it.each([
    [
      {claim_type: "return_policy", key: "free_returns", pitch_ref: "pitch:p-1", value: {rate: 1e-5}},
      '{"claim_type":"return_policy","key":"free_returns","pitch_ref":"pitch:p-1",' +
        '"value":{"rate":0.00001}}',
    ],
    [
      {
        claim_type: "return_policy",
        key: "free_returns",
        pitch_ref: "pitch:p-1",
        value: {"\u{1F600}": 1, "￿": 2},
      },
      '{"claim_type":"return_policy","key":"free_returns","pitch_ref":"pitch:p-1",' +
        '"value":{"\u{1F600}":1,"￿":2}}',
    ],
  ])("renders claim material %#", (material, expected) => {
    expect(canonicalJson(material)).toBe(expected);
    // The rendering `claim_id` used to hash, for contrast: `JSON.stringify` with sorted keys is
    // the JS spelling of `sort_keys=True`, and it disagrees on both rows.
    expect(JSON.stringify(material, Object.keys(material).sort())).not.toBe(expected);
  });
});

// --- T-108: the schema gate and `missingSigningFields` agree on whitespace -----------------

/**
 * Every whitespace-only spelling the envelope's `pattern` excludes, written by code point so no
 * raw control character lands in this file. `String.prototype.trim()` is NOT this set — it keeps
 * U+001C-U+001F, which Python's `str.strip()` removes and the schema's class excludes — so a
 * `trim()`-based check disagreed with the very schema Ajv compiles from the same bundle.
 */
const WHITESPACE_ONLY: readonly string[] = [
  0x20, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0xa0, 0x1680, 0x2000, 0x2003, 0x2028,
  0x2029, 0x202f, 0x205f, 0x3000,
]
  .map((code) => String.fromCodePoint(code))
  .concat(["   ", String.fromCodePoint(0x20, 0x09, 0x0a)]);

describe("T-108 — both envelope gates refuse a whitespace-only value", () => {
  it.each(
    REQUIRED_SIGNING_FIELDS.flatMap((field) =>
      WHITESPACE_ONLY.map((blank) => [field, blank] as const),
    ),
  )("refuses %s = %j", (field, blank) => {
    const payload = makeSubmission({[field]: blank});
    expect(missingSigningFields(payload)).toContain(field);
    expect(isValid("SignedBidSubmission", payload)).toBe(false);
    expect(isValid("SigningEnvelope", envelopeOf(payload))).toBe(false);
    expect(() => canonicalSigningBytes(payload)).toThrow(/incomplete signing envelope/);
  });

  it.each([...REQUIRED_SIGNING_FIELDS])("still admits a real %s", (field) => {
    // The control. A pattern that rejected everything would satisfy the cases above and refuse
    // every legal submission — including the padded-but-non-empty spellings that stay valid.
    const value = field === "issued_at" ? "2026-01-01T00:00:00Z" : " padded ";
    const payload = makeSubmission({[field]: value});
    expect(missingSigningFields(payload)).toEqual([]);
    expect(isValid("SignedBidSubmission", payload)).toBe(true);
    expect(isValid("SigningEnvelope", envelopeOf(payload))).toBe(true);
    expect(canonicalSigningBytes(payload).length).toBeGreaterThan(0);
  });

  it("agrees with the compiled schema on every one of these, character by character", () => {
    // The point of the ticket: two gates on one rule that disagree is ONE gate, and it is
    // whichever one the caller happens to be standing on. `canonicalSigningBytes` stands on
    // `missingSigningFields`; Ajv stands on the schema. They must not differ.
    for (const blank of WHITESPACE_ONLY) {
      const payload = makeSubmission({nonce: blank});
      expect(missingSigningFields(payload).includes("nonce"), JSON.stringify(blank)).toBe(true);
      expect(isValid("SignedBidSubmission", payload), JSON.stringify(blank)).toBe(false);
    }
  });

  it("agrees with itself even where the two regex engines differ", () => {
    // U+0085 (NEL) is Unicode White_Space, so Python's `str.strip()` and the rust-regex `\s`
    // behind pydantic call it blank; JavaScript's `\s` and `trim()` do not. U+FEFF is the mirror
    // image. What T-108 asks for is that the SCHEMA and `missingSigningFields` agree, and on this
    // side they do — for both characters, whichever way the answer falls. The cross-language
    // split lives in `str.strip()` vs `String.trim()` and is older than this ticket.
    for (const code of [0x85, 0xfeff]) {
      const blank = String.fromCodePoint(code);
      const payload = makeSubmission({nonce: blank});
      const functionSaysPresent = !missingSigningFields(payload).includes("nonce");
      const schemaSaysPresent = isValid("SignedBidSubmission", payload);
      expect(functionSaysPresent, `U+${code.toString(16).toUpperCase()}`).toBe(schemaSaysPresent);
    }
  });
});

/** The five envelope fields lifted out of a submission, as `SigningEnvelope` shaped. */
function envelopeOf(payload: Payload): Payload {
  return Object.fromEntries(REQUIRED_SIGNING_FIELDS.map((field) => [field, payload[field]]));
}

// --- T-125: the signing doors and the canonicalizer agree on what a number IS --------------
//
// T-113 built the right guard in the wrong domain. `parseSignableJson` — the LITERAL check —
// was correct and uncalled, so T-113 gave `payloadHash`/`canonicalSigningBytes` a VALUE-domain
// approximation of it: refuse any integer-valued `number` at or beyond 2**53. That refuses
// 10**16, which satisfies `float(v) == v`, which RFC 8785 §3.1 therefore requires, which
// `canonicalJson` renders and `contracts.signing.canonical_signing_bytes` signs — so a Node
// seller could not sign a bid a Python exchange signs happily. The T-106 conformance gate never
// saw it, because it drove the renderers only: `grep -c canonicalSigningBytes
// e2e/test_jcs_conformance.py` was 0. T-125 removes the value-domain guard, keeps the literal
// one where the evidence still exists, and extends that gate to the signing doors.
//
// Everything below drives the DEFAULT entry points — never `parseSignableJson`. The refusals
// still refuse on the T-113 module; the acceptances are what fails on it.

/** A submission whose `quantity` is the literal `wire` says, as `JSON.parse` delivers it. */
function submissionFromWire(literal: string, key = "quantity"): Payload {
  const text = JSON.stringify({...makeSubmission(), [key]: 0}).replace(
    `"${key}":0`,
    `"${key}":${literal}`,
  );
  return JSON.parse(text) as Payload;
}

/** The same submission as raw wire text, for the text-taking doors. */
function wireFor(literal: string, key = "quantity"): string {
  return JSON.stringify({...makeSubmission(), [key]: 0}).replace(
    `"${key}":0`,
    `"${key}":${literal}`,
  );
}

/**
 * The bytes `contracts.signing.canonical_signing_bytes` produces for a submission carrying
 * `quantity: 10**16` — pinned identically by
 * `tests/test_signing_envelope.py::EXPECTED_TEN_16_CANONICAL_BYTES`.
 *
 * The second shared constant in this file, and it exists for the case the first one could not
 * reach: `EXPECTED_CANONICAL_BYTES` carries only safe integers, so it stayed green through the
 * whole of T-113 while the two languages disagreed about every exact double above 2**53.
 */
const EXPECTED_TEN_16_CANONICAL_BYTES =
  '{"auction_id":"auc-0100","issued_at":"2026-01-01T00:00:00Z","key_id":"key-2026-01",' +
  '"nonce":"nonce-ext-0001",' +
  '"payload_hash":"sha256:6f5022c55463964c26b49be49e428ef0804edc4c31b9a9b8795c4d96454ba2f1",' +
  '"schema_version":"1.0.0","signer_id":"store-external-1","store_id":"store-external-1"}';

/**
 * Every number class the doors have to have an answer for, labelled so a failure names it.
 *
 * Deliberately spans both sides of 2**53, both ends of the double range, the ECMAScript
 * exponential switch points, and the two things that are NOT finite doubles — a non-finite
 * `number` and a `bigint` with no double — because the property being asserted is agreement
 * with the renderer, which is only meaningful if the list contains inputs it refuses.
 */
const NUMBER_DOMAIN_CASES: ReadonlyArray<readonly [string, unknown]> = [
  ["0", 0],
  ["-0", -0],
  ["1", 1],
  ["-42", -42],
  ["44.1", 44.1],
  ["1e-5", 1e-5],
  ["1.5", 1.5],
  ["2**53", 2 ** 53],
  ["2**53+2", 2 ** 53 + 2],
  ["1e16", 1e16],
  ["1e21", 1e21],
  ["2**63", 2 ** 63],
  ["2**64", 2 ** 64],
  ["max-double", 1.7976931348623157e308],
  ["min-subnormal", 5e-324],
  ["MAX_SAFE_INTEGER", Number.MAX_SAFE_INTEGER],
  ["-(2**53-1)", -9007199254740991],
  ["NaN", Number.NaN],
  ["Infinity", Number.POSITIVE_INFINITY],
  ["-Infinity", Number.NEGATIVE_INFINITY],
  ["9007199254740993n", 9007199254740993n],
];

/** True when `thunk` threw. The comparison is refusal, never which class was thrown. */
function refuses(thunk: () => unknown): boolean {
  try {
    thunk();
    return false;
  } catch {
    return true;
  }
}

describe("T-125 — the signing doors accept every integer that satisfies float(v) == v", () => {
  it("signs 10**16 through both doors, byte for byte with the Python peer", () => {
    // THE ticket. `float(10**16) == 10**16` is true, so RFC 8785 §3.1 requires this number to
    // be signable, and the Python side signs it — into exactly these bytes.
    const payload = {...makeSubmission(), quantity: 1e16};
    expect(decode(canonicalSigningBytes(payload))).toBe(EXPECTED_TEN_16_CANONICAL_BYTES);
    expect(decode(canonicalSigningBytesFromJson(JSON.stringify(payload)))).toBe(
      EXPECTED_TEN_16_CANONICAL_BYTES,
    );
    expect(payloadHash(payload)).toBe(payloadHashFromJson(JSON.stringify(payload)));
  });

  it.each(EXACT_DOUBLE_INTEGER_WIRE)(
    "the VALUE door signs the exact double %s with no opt-in of any kind",
    (literal) => {
      // There is no flag to pass any more, which is the point: `SigningNumberOptions` existed
      // only to undo a refusal that should never have applied to these values.
      const payload = submissionFromWire(literal);
      expect(canonicalSigningBytes(payload).length).toBeGreaterThan(0);
      expect(payloadHash(payload)).toBe(payloadHashFromJson(wireFor(literal)));
    },
  );

  it.each(NUMBER_DOMAIN_CASES)(
    "refuses %s at the signing doors if and only if canonicalJson refuses it",
    (_label, value) => {
      // The property that makes "over-rejection" checkable rather than a matter of taste. The
      // signing doors are not allowed a narrower number domain than the renderer the
      // conformance gate pins against both Python implementations — that gap IS the divergence
      // class T-106 exists to close, and it is where T-113 put a guard.
      const rendered = refuses(() => canonicalJson({n: value}));
      expect(refuses(() => payloadHash({...makeSubmission(), quantity: value}))).toBe(rendered);
      expect(refuses(() => canonicalSigningBytes({...makeSubmission(), quantity: value}))).toBe(
        rendered,
      );
    },
  );

  it("has no opt-out left to pass, and ignores one offered anyway", () => {
    // `SigningNumberOptions` is gone with the guard it configured. Asserted by BEHAVIOUR, not
    // by arity: `function f(payload, options = {})` also reports `length === 1`, so an arity
    // check would have passed on the T-113 module unchanged and proved nothing. Handing the
    // door an options object must not change the answer — on T-113 the first row of this loop
    // (`{}`, i.e. the default) threw.
    const withOptions: (payload: Payload, options?: unknown) => Uint8Array = canonicalSigningBytes;
    const digestWithOptions: (payload: Payload, options?: unknown) => string = payloadHash;
    const payload = {...makeSubmission(), quantity: 1e16};
    for (const stray of [{}, {allowUnsafeIntegers: false}, {allowUnsafeIntegers: true}]) {
      expect(decode(withOptions(payload, stray))).toBe(EXPECTED_TEN_16_CANONICAL_BYTES);
      expect(digestWithOptions(payload, stray)).toBe(payloadHash(payload));
    }
  });

  it.each(NON_DOUBLE_INTEGER_WIRE)(
    "signs the double JSON.parse delivered for %s, because by then it IS %s",
    (literal, coerced) => {
      // The honest statement of what a value-domain door can and cannot do, and the reason no
      // narrower guard was possible. This value is already `coerced` — an exact double the
      // Python peer signs — and no test on the value could separate it from a submission that
      // spelled `coerced` outright. The proof is the digest: both spellings hash the same.
      const payload = submissionFromWire(literal);
      expect(canonicalJson(payload["quantity"])).toBe(coerced);
      expect(String(payload["quantity"])).not.toBe(literal);
      expect(canonicalSigningBytes(payload).length).toBeGreaterThan(0);
      expect(payloadHash(payload)).toBe(payloadHash(submissionFromWire(coerced)));
    },
  );

  it.each(NON_DOUBLE_INTEGER_WIRE)(
    "the TEXT door still refuses %s, and names the literal it refused",
    (literal) => {
      // Unchanged and load-bearing: this is the door where RFC 8785 §3.1 is still decidable in
      // JavaScript, and T-125's non-goal is that it keeps refusing. `2**53+1` is here.
      expect(() => canonicalSigningBytesFromJson(wireFor(literal))).toThrow(CanonicalisationError);
      expect(() => canonicalSigningBytesFromJson(wireFor(literal))).toThrow(
        new RegExp(`cannot accept the integer ${literal}`),
      );
      expect(() => payloadHashFromJson(wireFor(literal))).toThrow(CanonicalisationError);
    },
  );

  it("the two doors now agree on 1e16 spelled either way, which they did not before", () => {
    // Measured on the T-113 module: `payloadHashFromJson` on this exact text returned
    // sha256:39b0550897…, while `payloadHash(JSON.parse(text))` threw "cannot sign the integer
    // 10000000000000000". One implementation, two doors, two answers — because the literal
    // check skips exponential spellings by design (`json.loads` gives Python a float for those
    // too), so "the literals have been checked" was never true of the whole payload. That was
    // the justification `payloadHashFromJson` gave for switching the value guard off (T-129).
    const exponential = wireFor("1e16");
    const expanded = wireFor("10000000000000000");
    expect(payloadHashFromJson(exponential)).toBe(payloadHashFromJson(expanded));
    expect(payloadHash(JSON.parse(exponential) as Payload)).toBe(payloadHashFromJson(exponential));
  });
});

// --- T-128: the covered-field guard could refuse nothing, and is gone ----------------------
//
// T-113 added `assertSignableNumbers(covered, options, "submission")` plus five `it.each` cases
// to prove it, on the claim that `auction_id` and `store_id` "reach canonicalJson through
// `covered` and never through payloadHash". The claim is false — `NON_BODY_KEYS` is exactly
// {signer_id, key_id, issued_at, nonce, signature}, so both are IN the body — and deleting the
// guard left all 586 vitest cases green, measured. These five replaced those five and grade the
// structural facts the claim got wrong, so the guard cannot come back on the same reasoning.
//
// The fourth case is the one T-125 changed: with the value-domain guard gone, a numeric
// `auction_id` is no longer refused at all (it is an exact double), so what is graded is that
// it reaches the bytes and moves the digest — the property that actually protects a signature
// from being lifted onto another auction.

describe("T-128 — the body digest already reaches every SIGNED_FIELD that can hold a number", () => {
  it("auction_id and store_id are INSIDE the body payloadHash walks", () => {
    const base = payloadHash(makeSubmission());
    expect(payloadHash(makeSubmission({auction_id: "auc-OTHER"}))).not.toBe(base);
    expect(payloadHash(makeSubmission({store_id: "store-OTHER"}))).not.toBe(base);
  });

  it("the SIGNED_FIELDS outside the body are exactly the four envelope identity fields", () => {
    const base = payloadHash(makeSubmission());
    const outside = SIGNED_FIELDS.filter(
      (field) => payloadHash(makeSubmission({[field]: "CHANGED"})) === base,
    );
    expect([...outside].sort()).toEqual(["issued_at", "key_id", "nonce", "signer_id"]);
  });

  it("and every one of those four is already forced to be a non-blank STRING", () => {
    // Which is what makes a number check on `covered` unreachable: the submission is rejected
    // before it, by `missingSigningFields`, with a different and better message.
    for (const field of ["signer_id", "key_id", "issued_at", "nonce"] as const) {
      const numeric = makeSubmission({[field]: 9007199254740992});
      expect(missingSigningFields(numeric)).toContain(field);
      expect(() => canonicalSigningBytes(numeric)).toThrow(/incomplete signing envelope/);
    }
  });

  it("a numeric auction_id reaches the bytes through the body, with no covered check at all", () => {
    // The one input the deleted guard was supposed to be the sole refuser of. It is not refused
    // now — `float(v) == v` holds for it — and the property that actually matters still holds:
    // it changes the digest, so a signature over one auction_id cannot be lifted onto another.
    const numeric = makeSubmission({auction_id: 9007199254740992});
    expect(decode(canonicalSigningBytes(numeric))).toContain('"auction_id":9007199254740992');
    expect(payloadHash(numeric)).not.toBe(payloadHash(makeSubmission()));
    expect(payloadHash(numeric)).not.toBe(payloadHash(makeSubmission({auction_id: "auc-OTHER"})));
  });

  it("the five NON_BODY_KEYS are the only keys the digest ignores", () => {
    // The other half of the same structural fact, stated as a closed set: change anything else
    // and the digest moves. Adding a key to NON_BODY_KEYS turns this red, which is the
    // regression the false claim would otherwise have licensed.
    const base = payloadHash(makeSubmission());
    const ignored = ["signer_id", "key_id", "issued_at", "nonce", "signature"];
    for (const key of ignored) {
      expect(payloadHash(makeSubmission({[key]: "CHANGED"})), key).toBe(base);
    }
    for (const key of ["auction_id", "store_id", "schema_version", "agent_version", "offer"]) {
      expect(payloadHash(makeSubmission({[key]: "CHANGED"})), key).not.toBe(base);
    }
  });
});

describe("T-113 — the text door is the one that reads the literal, and it still does", () => {
  it.each(EXACT_DOUBLE_INTEGER_WIRE)("the TEXT door signs the exact double %s unaided", (literal) => {
    // The control that stops "refuse everything large" from passing this suite. The text door
    // reads the literal, so it can tell an exact double from a coerced one — no flag, no
    // opt-in, and the same answer the Python peer gives.
    expect(canonicalSigningBytesFromJson(wireFor(literal)).length).toBeGreaterThan(0);
    expect(payloadHashFromJson(wireFor(literal))).toMatch(/^sha256:[0-9a-f]{64}$/);
  });

  it("the text door reproduces the pinned bytes for a legal submission", () => {
    // The other control: the guard must not move the bytes of anything that already signed.
    const wire = JSON.stringify(makeSubmission());
    expect(decode(canonicalSigningBytesFromJson(wire))).toBe(EXPECTED_CANONICAL_BYTES);
    expect(payloadHashFromJson(wire)).toBe(payloadHash(makeSubmission()));
  });

  it("still refuses malformed wire text as a SyntaxError, not a signing failure", () => {
    expect(() => canonicalSigningBytesFromJson('{"a":')).toThrow(SyntaxError);
    expect(() => payloadHashFromJson('{"a":')).toThrow(SyntaxError);
  });

  it("the renderer and the signing doors are no longer split, and that is the fix", () => {
    // `canonicalJson` MUST render exact large doubles: `e2e/test_jcs_conformance.py` requires
    // it to agree byte for byte with both Python canonicalizers, and both render these. T-113
    // let the signing doors refuse what the renderer rendered and called the split deliberate;
    // T-125's ruling is that the split IS the divergence class T-106 exists to close, so the
    // doors follow the renderer here.
    expect(canonicalJson({n: 2 ** 53})).toBe('{"n":9007199254740992}');
    expect(canonicalJson({n: 1e16})).toBe('{"n":10000000000000000}');
    expect(canonicalJson({n: 1e21})).toBe('{"n":1e+21}');
    expect(payloadHash({n: 1e21})).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(payloadHash({n: 1e21})).not.toBe(payloadHash({n: 1e16}));
  });

  it("does not refuse anything a real bid actually carries", () => {
    // Fractions, safe integers and negative zero all sign. A guard that refused 44.1 would
    // satisfy every rejection above and break the protocol.
    expect(canonicalSigningBytes(makeSubmission()).length).toBeGreaterThan(0);
    for (const n of [0, -0, 1, -42, 44.1, 1e-5, 1.5, Number.MAX_SAFE_INTEGER, -9007199254740991]) {
      expect(() => payloadHash({...makeSubmission(), quantity: n}), String(n)).not.toThrow();
    }
  });
});

// --- T-115: the blank rule is engine-independent, and asserted as a PROPERTY ---------------
//
// T-108 put a `\s`-based class in `protocol.schema.json`. That shorthand is ENGINE-DEPENDENT:
// Unicode White_Space to Rust's regex crate (what pydantic compiles) and to Python's `re`, a
// different fixed list to ECMAScript (what Ajv compiles). The single artifact whose whole purpose
// is that both languages read the SAME contract therefore stated two rules, and they split on
// U+0085 (blank in Python only) and U+FEFF (blank in TypeScript only).
//
// The guarantee was also pinned against WHITESPACE_ONLY above — ~20 spellings someone thought of,
// which says nothing about the 1,114,092 code points not on it. These walk every code point.

/**
 * The one class, spelled out. The SAME literal `tests/test_signing_envelope.py::BLANK_PATTERN`
 * pins, which is what makes the two languages one rule: each suite proves its own gate agrees
 * with this string over all of Unicode, so the two gates agree with each other.
 */
const BLANK_PATTERN =
  "[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680" +
  "\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]";

/** The spelling T-115 removed, kept so the property can prove it would have caught it. */
const SUPERSEDED_SHORTHAND_PATTERN = ["[^", "\\s", "\\u001c-\\u001f", "]"].join("");

/** Every code point the blank class covers. The twin of `contracts.signing.BLANK_CODE_POINTS`. */
const EXPECTED_BLANK_CODE_POINTS: ReadonlySet<number> = new Set<number>([
  ...[0x09, 0x0a, 0x0b, 0x0c, 0x0d],
  ...[0x1c, 0x1d, 0x1e, 0x1f, 0x20],
  0x85,
  0xa0,
  0x1680,
  ...Array.from({length: 11}, (_unused, index) => 0x2000 + index),
  0x2028,
  0x2029,
  0x202f,
  0x205f,
  0x3000,
  0xfeff,
]);

/** Every code point there is, minus the surrogates, which are not characters. */
function* allCodePoints(): Generator<number> {
  for (let cp = 0; cp <= 0x10ffff; cp += 1) {
    if (cp >= 0xd800 && cp <= 0xdfff) continue;
    yield cp;
  }
}

describe("T-115 — one blank rule, stated so both regex engines read it the same way", () => {
  it("spells the class identically in all ten places, with no engine-dependent shorthand", () => {
    const defs = protocolSchema.$defs as unknown as Record<
      string,
      {properties: Record<string, {minLength?: number; pattern?: string}>}
    >;
    let seen = 0;
    for (const name of ["SigningEnvelope", "SignedBidSubmission"]) {
      for (const field of REQUIRED_SIGNING_FIELDS) {
        const spec = defs[name]!.properties[field]!;
        expect(spec.minLength, `${name}.${field}`).toBe(1);
        expect(spec.pattern, `${name}.${field}`).toBe(BLANK_PATTERN);
        for (const shorthand of ["\\s", "\\S", "\\w", "\\W", "\\d", "\\D", "\\p", "\\P", "\\b"]) {
          expect(spec.pattern, `${name}.${field} still carries ${shorthand}`).not.toContain(
            shorthand,
          );
        }
        seen += 1;
      }
    }
    expect(seen).toBe(10);
  });

  it("agrees with the code-level blank check on every code point in Unicode", () => {
    // The property, under the engine that actually gates data on this side. `isValid` is Ajv
    // compiling the bundle; `missingSigningFields` is the function `canonicalSigningBytes` stands
    // on. Two gates on one rule that disagree is one gate, and it is whichever one the caller
    // happens to be standing on — so they are compared over ALL of Unicode, not over a list.
    const base = {
      signer_id: "store-external-1",
      key_id: "key-2026-01",
      issued_at: "2026-01-01T00:00:00Z",
      nonce: "nonce-ext-0001",
      schema_version: "1.0.0",
    };
    const disagreements: string[] = [];
    for (const cp of allCodePoints()) {
      const payload = {...base, nonce: String.fromCodePoint(cp)};
      const schemaSaysPresent = isValid("SigningEnvelope", payload);
      const functionSaysPresent = !missingSigningFields(payload).includes("nonce");
      const shouldBeBlank = EXPECTED_BLANK_CODE_POINTS.has(cp);
      if (schemaSaysPresent !== functionSaysPresent || schemaSaysPresent === shouldBeBlank) {
        disagreements.push(
          `U+${cp.toString(16).toUpperCase()} schema=${schemaSaysPresent} ` +
            `fn=${functionSaysPresent} expectedBlank=${shouldBeBlank}`,
        );
        if (disagreements.length > 20) break;
      }
    }
    expect(disagreements).toEqual([]);
  });

  it("agrees with the pinned pattern compiled directly, with and without the u flag", () => {
    // Ajv chooses the flags; the rule must not depend on that choice either.
    for (const flags of ["", "u"]) {
      const compiled = new RegExp(BLANK_PATTERN, flags);
      const wrong: string[] = [];
      for (const cp of allCodePoints()) {
        const hasContent = compiled.test(String.fromCodePoint(cp));
        if (hasContent === EXPECTED_BLANK_CODE_POINTS.has(cp)) {
          wrong.push(`U+${cp.toString(16).toUpperCase()}`);
          if (wrong.length > 20) break;
        }
      }
      expect(wrong, `flags="${flags}"`).toEqual([]);
    }
  });

  it("would have failed for the shorthand spelling the ticket removed", () => {
    // What makes the properties above mean something: they must not be satisfiable by the OLD
    // pattern. ECMAScript's `\s` excludes U+0085 and includes U+FEFF; Rust's and Python's include
    // U+0085 and exclude U+FEFF. Same file, two rules — which is the whole finding.
    const old = new RegExp(SUPERSEDED_SHORTHAND_PATTERN, "u");
    expect(old.test(String.fromCodePoint(0x85)), "U+0085 was content under ECMAScript").toBe(true);
    expect(old.test(String.fromCodePoint(0xfeff)), "U+FEFF was blank under ECMAScript").toBe(false);
    const divergent: string[] = [];
    for (const cp of allCodePoints()) {
      if (old.test(String.fromCodePoint(cp)) === EXPECTED_BLANK_CODE_POINTS.has(cp)) {
        divergent.push(`U+${cp.toString(16).toUpperCase()}`);
      }
    }
    expect(divergent).toEqual(["U+85"]);
  });

  it("still calls a real value content", () => {
    // The control. A class that swallowed everything would satisfy every property above.
    const compiled = new RegExp(BLANK_PATTERN, "u");
    for (const value of ["x", " padded ", "\ttabbed", "nonce-ext-0001", "é", "\u{1F600}"]) {
      expect(compiled.test(value), value).toBe(true);
      const payload = makeSubmission({nonce: value});
      expect(missingSigningFields(payload), value).toEqual([]);
      expect(isValid("SigningEnvelope", envelopeOf(payload)), value).toBe(true);
      expect(isValid("SignedBidSubmission", payload), value).toBe(true);
    }
  });
});
