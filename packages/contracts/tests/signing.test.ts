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
  isSignedBidSubmission,
  keyringSecret,
  missingSigningFields,
  parseSignableJson,
  payloadHash,
  signingEnvelopeErrors,
} from "../src/ts/signing.js";
import type {Payload} from "../src/ts/signing.js";
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
