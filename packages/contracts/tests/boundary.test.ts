/**
 * The dual-path bid boundary in TypeScript — the same table the Python suite asserts.
 *
 * Every rejection is paired with a positive control. A boundary that rejected everything would
 * satisfy every rejection assertion here and admit no bids at all.
 */
import {describe, expect, it} from "vitest";

import {
  EXTERNAL_PATH,
  HOOK_PROVENANCE_SOURCES,
  HOSTED_PATH,
  LIST_PRICE_CLAIM_KEY,
  MAX_DISCOUNT_ROSTER_KEY,
  MINIMUM_PAYABLE_AMOUNT,
  NON_HOOK_PROVENANCE_SOURCES,
  OFFER_COMMITMENTS_SITE,
  OFFER_DISCOUNT_SITE,
  OFFER_TOTAL_PRICE_SITE,
  OFFER_UNIT_PRICE_SITE,
  PRICE_FLOOR_FRACTION,
  PRICE_RECONCILIATION_TOLERANCE,
  REASON_CLAIM_PROVENANCE_EMPTY_SOURCE,
  REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE,
  REASON_CLAIM_WITHOUT_PROVENANCE,
  REASON_DISCOUNT_OVER_AUTHORIZED_DEPTH,
  REASON_HOSTED_NON_HOOK_PROVENANCE,
  REASON_OFFER_EXPIRED,
  REASON_OFFER_EXPIRY_MISSING,
  REASON_OFFER_EXPIRY_UNPARSEABLE,
  REASON_PRICE_UNDER_DECLARED_DEPTH,
  REASON_PRICE_UNRECONCILABLE,
  REASON_SCHEMA_INVALID,
  REASON_SIGNATURE_MISSING,
  REASON_STORE_BLACKLISTED,
  REASON_TRUST_SNAPSHOT_UNAVAILABLE,
  REASON_UNKNOWN_PATH,
  REASON_UNVERIFIABLE_CLAIM_SITE,
  ROSTER_LIST_PRICE_UNAVAILABLE,
  ROSTER_MAX_DISCOUNT_UNAVAILABLE,
  ROSTER_PRICE_BELOW_FLOOR,
  parseTimestamp,
  priceFloor,
  priceReasons,
  validateBid,
  validateExternalSubmission,
} from "../src/ts/boundary.js";
import type {PriceRosterMap} from "../src/ts/boundary.js";
import {PROVENANCE_SOURCES} from "../src/ts/vocabulary.js";
import {
  ASSERTED_PROVENANCE,
  HOOK_PROVENANCE,
  LONG_EXPIRED,
  NOW,
  makeBid,
  makeClaim,
  makeOffer,
  makeSnapshotTable,
} from "./fixtures.js";
// The SHARED parity corpus. `test_boundary_dual_path.py` reads this same file, so a case added
// here is asserted against both doors and a door that answers differently goes red.
import corpus from "./price_parity_corpus.json" with {type: "json"};

const BOTH_PATHS = [HOSTED_PATH, EXTERNAL_PATH] as const;

/**
 * What the exchange's OWN catalog says about `makeOffer()`'s product: `prod-1` lists at 49.00 —
 * exactly the price the shared fixture offer charges — and the merchant approved discounts up to
 * 25% on it, deeper than any depth a fixture bid in this file declares through `check()`.
 *
 * WHY THE HELPER PASSES A ROSTER AT ALL, since it never used to (T-306/T-336). `check()` called
 * `validateBid` with no `listPrices`, and an absent roster USED TO MEAN "abstain": the price wall
 * reported nothing at all, so every test in this file measured only the thing it was written
 * about. An absent roster is now the EMPTY roster and refuses every offer with
 * `price_unreconcilable:offer.unit_price:list_price_unavailable`, so a helper that goes on passing
 * nothing turns a file about provenance, expiry and trust into a file about the price wall — 55
 * tests failing on a reason none of them names. Handing the door the catalog restores each test's
 * SUBJECT rather than relaxing anything: the roster prices the fixture product truthfully and
 * authorizes more depth than the fixtures declare, so no price reason can fire unless the test is
 * about prices, and every price test below supplies its own roster explicitly. The Python peer's
 * `FIXTURE_ROSTER`, same numbers.
 */
const FIXTURE_ROSTER: PriceRosterMap = {"prod-1": {list_price: 49.0, max_discount_pct: 25.0}};

/**
 * The catalog the T-177 price tests are written against: `prod-1` lists at 100.00 with 20%
 * approved. It AGREES with the 100.00 those same bids carry as a `list_price` claim, so the roster
 * is not a second, contradicting wall — the reconciliation those tests assert is the one they
 * always asserted. The Python peer's `LIST_100_ROSTER`.
 */
const LIST_100_ROSTER: PriceRosterMap = {"prod-1": {list_price: 100.0, max_discount_pct: 20.0}};

/**
 * A sentinel, not `undefined`: since T-306 an absent roster is itself a case under test, and a
 * helper where "no argument" silently meant `FIXTURE_ROSTER` with no way to say "no roster" would
 * turn those tests into the happy path. Mirrors `_DEFAULT` in `test_boundary_dual_path.py`.
 */
const DEFAULT_ROSTER = Symbol("default price roster");

function check(
  bid: unknown,
  path: string,
  snapshot = makeSnapshotTable(),
  listPrices: PriceRosterMap | null | typeof DEFAULT_ROSTER = DEFAULT_ROSTER,
) {
  const roster = listPrices === DEFAULT_ROSTER ? FIXTURE_ROSTER : listPrices;
  return validateBid(bid, {
    path,
    trustSnapshot: snapshot,
    now: NOW,
    // NOT `roster ?? undefined`: that collapsed the `null` spelling into the `undefined`
    // one, so the "three spellings" assertion below had only two distinct inputs and an
    // explicit `null` reached `validateBid` from nowhere in this file. T-336 is precisely
    // about `null` and `undefined` diverging between the two doors, so the one spelling the
    // Python peer genuinely passes must reach this door too.
    listPrices: roster as PriceRosterMap | undefined,
  });
}

describe("R8 / R18 — the path-sensitive half", () => {
  it("rejects a hosted bid carrying a seller_asserted claim", () => {
    const result = check(makeBid({claims: [makeClaim("spf", 30, ASSERTED_PROVENANCE)]}), HOSTED_PATH);
    expect(result.ok).toBe(false);
    expect(result.reasons.length).toBeGreaterThan(0);
    expect(result.reasons.join(" ")).toContain("hosted_non_hook_provenance");

    // Control: the same bid with a hook-provenanced claim is admitted.
    expect(check(makeBid({claims: [makeClaim("spf", 30, HOOK_PROVENANCE)]}), HOSTED_PATH).ok).toBe(
      true,
    );
  });

  it("admits the identical claim on the external path and flags it", () => {
    const result = check(
      makeBid({claims: [makeClaim("spf", 30, ASSERTED_PROVENANCE)]}),
      EXTERNAL_PATH,
    );
    expect(result.ok).toBe(true);
    expect(result.requires_verification).toBe(true);
    expect(result.unverified_claim_indexes).toEqual([0]);

    // Control: the flag tracks the claim's provenance, not the path alone.
    const hooked = check(makeBid({claims: [makeClaim("spf", 30, HOOK_PROVENANCE)]}), EXTERNAL_PATH);
    expect(hooked.ok).toBe(true);
    expect(hooked.requires_verification).toBe(false);
  });

  it("names only the claims that actually need verifying", () => {
    const bid = makeBid({
      claims: [
        makeClaim("free_returns", "30 days", HOOK_PROVENANCE),
        makeClaim("spf", 30, ASSERTED_PROVENANCE),
        makeClaim("vegan", true, HOOK_PROVENANCE),
        makeClaim("material", "merino", ASSERTED_PROVENANCE),
      ],
    });
    const result = check(bid, EXTERNAL_PATH);
    expect(result.ok).toBe(true);
    expect(result.unverified_claim_indexes).toEqual([1, 3]);
  });

  it.each([...HOOK_PROVENANCE_SOURCES])(
    "admits hook provenance %s on both paths",
    (source: string) => {
      const bid = makeBid({claims: [makeClaim("spf", 30, {...HOOK_PROVENANCE, source})]});
      for (const path of BOTH_PATHS) {
        const result = check(bid, path);
        expect(result.ok, `${path}/${source}: ${result.reasons.join(", ")}`).toBe(true);
        expect(result.requires_verification).toBe(false);
      }
    },
  );
});

describe("S5 — the path-insensitive half", () => {
  it.each(BOTH_PATHS)("rejects a claim with no provenance on %s", (path) => {
    const result = check(makeBid({claims: [{key: "spf", value: 30}]}), path);
    expect(result.ok).toBe(false);
    expect(result.reasons.length).toBeGreaterThan(0);
  });

  it.each(BOTH_PATHS)("rejects a claim with an empty provenance source on %s", (path) => {
    const bid = makeBid({claims: [makeClaim("spf", 30, {...HOOK_PROVENANCE, source: ""})]});
    expect(check(bid, path).ok).toBe(false);
  });

  it.each(BOTH_PATHS)("rejects an unknown provenance source on %s", (path) => {
    const bid = makeBid({claims: [makeClaim("spf", 30, {...HOOK_PROVENANCE, source: "vibes"})]});
    expect(check(bid, path).ok).toBe(false);
  });

  it.each(BOTH_PATHS)("rejects an expired offer on %s", (path) => {
    const result = check(makeBid({offer: makeOffer({expires_at: LONG_EXPIRED})}), path);
    expect(result.ok).toBe(false);
    expect(result.reasons.join(" ")).toContain("offer_expired");
    expect(check(makeBid(), path).ok).toBe(true);
  });

  it.each(BOTH_PATHS)("rejects an offer expiring exactly now on %s", (path) => {
    expect(check(makeBid({offer: makeOffer({expires_at: NOW})}), path).ok).toBe(false);
  });

  it.each(BOTH_PATHS)("fails closed on an offer with no expiry on %s", (path) => {
    const result = check(makeBid({offer: makeOffer({expires_at: null})}), path);
    expect(result.ok).toBe(false);
    expect(result.reasons.join(" ")).toContain("offer_expiry_missing");
  });

  it.each(BOTH_PATHS)("rejects a blacklisted store on %s", (path) => {
    const result = check(makeBid({store_id: "store-bad"}), path);
    expect(result.ok).toBe(false);
    expect(result.reasons.join(" ")).toContain("store_blacklisted");
  });

  it.each(BOTH_PATHS)("fails closed when the store has no snapshot row on %s", (path) => {
    // R12: an UNAVAILABLE eligibility read denies exactly like a positive blacklist hit.
    const result = check(makeBid({store_id: "store-unknown"}), path);
    expect(result.ok).toBe(false);
    expect(result.reasons.join(" ")).toContain("trust_snapshot_unavailable");
  });

  it.each(BOTH_PATHS)("fails closed on an unusable snapshot on %s", (path) => {
    for (const snapshot of [{}, null, "not-a-snapshot"]) {
      expect(check(makeBid(), path, snapshot as never).ok).toBe(false);
    }
  });

  it.each(BOTH_PATHS)("rejects a schema-invalid bid on %s", (path) => {
    const bid = makeBid();
    delete bid["agent_version"];
    const result = check(bid, path);
    expect(result.ok).toBe(false);
    expect(result.reasons.join(" ")).toContain("schema_invalid");
  });
});

describe("the boundary's own contract", () => {
  it("refuses an unknown path rather than guessing", () => {
    for (const path of ["HOSTED", "internal", "", "hosted "]) {
      const result = check(makeBid(), path);
      expect(result.ok).toBe(false);
      expect(result.reasons.join(" ")).toContain("unknown_path");
    }
  });

  it("never throws on malformed input", () => {
    for (const bid of [null, "not-a-bid", 42, [], {claims: "not-a-list"}]) {
      const result = check(bid, HOSTED_PATH);
      expect(result.ok).toBe(false);
      expect(result.reasons.length).toBeGreaterThan(0);
    }
  });

  it("never flags a rejected bid for verification", () => {
    const result = check(
      makeBid({claims: [makeClaim("spf", 30, ASSERTED_PROVENANCE)], store_id: "store-bad"}),
      EXTERNAL_PATH,
    );
    expect(result.ok).toBe(false);
    expect(result.requires_verification).toBe(false);
    expect(result.unverified_claim_indexes).toEqual([]);
  });

  it("gives an admitted bid no reasons", () => {
    const result = check(makeBid(), HOSTED_PATH);
    expect(result.ok).toBe(true);
    expect(result.reasons).toEqual([]);
  });
});

describe("timestamp handling", () => {
  it.each([
    "2026-01-01T00:00:00Z",
    "2026-01-01T00:00:00+00:00",
    "2026-01-01T00:00:00",
    1767225600,
  ])("reads %s", (value) => {
    expect(parseTimestamp(value)).toBeInstanceOf(Date);
  });

  it("treats a naive instant as UTC", () => {
    // Otherwise expiry would depend on the host's timezone, which is not a property a contract has.
    expect(parseTimestamp("2026-01-01T00:00:00")?.toISOString()).toBe(
      parseTimestamp("2026-01-01T00:00:00Z")?.toISOString(),
    );
  });

  it.each([null, undefined, "", "   ", "not-a-date", {}])("refuses %s", (value) => {
    expect(parseTimestamp(value)).toBeUndefined();
  });
});

// ----------------------------------------------------------------------------------------------
// Reason codes and fail-closed edges — the parts a "reject everything" boundary would still pass.
// ----------------------------------------------------------------------------------------------
describe("the reason vocabulary is part of the contract", () => {
  it("names the missing provenance and the claim it belongs to", () => {
    const result = check(makeBid({claims: [{key: "spf", value: 30}]}), HOSTED_PATH);
    expect(result.reasons.some((r) => r.startsWith("claim_without_provenance:0"))).toBe(true);
  });

  it("names an empty provenance source", () => {
    const bid = makeBid({claims: [makeClaim("spf", 30, {...HOOK_PROVENANCE, source: ""})]});
    expect(
      check(bid, HOSTED_PATH).reasons.some((r) => r.startsWith("claim_provenance_empty_source:0")),
    ).toBe(true);
  });

  it("names an unknown provenance source and quotes it", () => {
    const bid = makeBid({claims: [makeClaim("spf", 30, {...HOOK_PROVENANCE, source: "vibes"})]});
    expect(
      check(bid, EXTERNAL_PATH).reasons.some((r) =>
        r.startsWith("claim_provenance_unknown_source:0:vibes"),
      ),
    ).toBe(true);
  });

  it("names the claim index and the source on a hosted refusal", () => {
    const bid = makeBid({
      claims: [
        makeClaim("free_returns", "30 days", HOOK_PROVENANCE),
        makeClaim("spf", 30, ASSERTED_PROVENANCE),
      ],
    });
    expect(check(bid, HOSTED_PATH).reasons).toContain("hosted_non_hook_provenance:1:seller_asserted");
  });

  it("uses the same literal reason strings as the Python peer", () => {
    // These cross a network boundary; a rename is a contract change, not a refactor.
    expect(REASON_UNKNOWN_PATH).toBe("unknown_path");
    expect(REASON_SCHEMA_INVALID).toBe("schema_invalid");
    expect(REASON_CLAIM_WITHOUT_PROVENANCE).toBe("claim_without_provenance");
    expect(REASON_CLAIM_PROVENANCE_EMPTY_SOURCE).toBe("claim_provenance_empty_source");
    expect(REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE).toBe("claim_provenance_unknown_source");
    expect(REASON_HOSTED_NON_HOOK_PROVENANCE).toBe("hosted_non_hook_provenance");
    expect(REASON_OFFER_EXPIRED).toBe("offer_expired");
    expect(REASON_OFFER_EXPIRY_MISSING).toBe("offer_expiry_missing");
    expect(REASON_OFFER_EXPIRY_UNPARSEABLE).toBe("offer_expiry_unparseable");
    expect(REASON_STORE_BLACKLISTED).toBe("store_blacklisted");
    expect(REASON_TRUST_SNAPSHOT_UNAVAILABLE).toBe("trust_snapshot_unavailable");
  });

  it("partitions the whole closed provenance enum", () => {
    // Guards the `it.each` over HOOK_PROVENANCE_SOURCES above: an empty array would silently
    // register zero tests, which reads as green.
    expect(HOOK_PROVENANCE_SOURCES.size).toBe(6);
    expect(NON_HOOK_PROVENANCE_SOURCES.size).toBe(1);
    expect([...HOOK_PROVENANCE_SOURCES, ...NON_HOOK_PROVENANCE_SOURCES].sort()).toEqual(
      [...PROVENANCE_SOURCES].sort(),
    );
  });
});

describe("R12 fail-closed, on the flag spellings a real snapshot uses", () => {
  it.each(BOTH_PATHS)("denies a truthy-but-not-`true` blacklist flag on %s", (path) => {
    // A strict `=== true` here admitted every one of these — fail-OPEN on the one check R12
    // exists to make fail closed.
    for (const flag of [true, 1, "yes", "true", [0]]) {
      const snapshot = {"store-1": {store_id: "store-1", score: 0.6, blacklisted: flag}};
      const result = check(makeBid(), path, snapshot as never);
      expect(result.ok, `blacklisted=${JSON.stringify(flag)} was admitted`).toBe(false);
      expect(result.reasons.join(" ")).toContain("store_blacklisted");
    }
  });

  it.each(BOTH_PATHS)("still admits a falsy blacklist flag on %s", (path) => {
    // The positive control: this is not "reject every row".
    for (const flag of [false, 0, "", null, undefined]) {
      const snapshot = {"store-1": {store_id: "store-1", score: 0.6, blacklisted: flag}};
      expect(check(makeBid(), path, snapshot as never).ok).toBe(true);
    }
  });
});

describe("D52 — the signing envelope at the external door", () => {
  /** A wire submission: `makeBid`'s store and auction, plus the five D52 fields and a signature. */
  function signed(overrides: Record<string, unknown> = {}) {
    return {
      ...makeBid(),
      signer_id: "store-1",
      key_id: "key-2026-01",
      issued_at: "2026-06-01T00:00:00Z",
      nonce: "nonce-0001",
      schema_version: "1.0.0",
      signature: "sig-deadbeef",
      ...overrides,
    };
  }

  const door = (submission: unknown) =>
    validateExternalSubmission(submission, {
      trustSnapshot: makeSnapshotTable(),
      now: NOW,
      // The catalog for the fixture product, for the same reason `check` passes it: these tests
      // are about the SIGNING ENVELOPE, and an unpriced offer would refuse every one of the
      // admission controls below on a price reason none of them names.
      listPrices: FIXTURE_ROSTER,
    });

  it("validateBid alone does not judge the envelope — the documented gap, pinned", () => {
    // `validateBid` checks the R8/R18/S5 table against `Bid`, which carries no envelope.
    expect(check(makeBid(), EXTERNAL_PATH).ok).toBe(true);
  });

  it("rejects an unsigned submission at the external door", () => {
    const result = door(makeBid());
    expect(result.ok, "an unsigned external submission was admitted").toBe(false);
    for (const field of ["signer_id", "key_id", "issued_at", "nonce"]) {
      expect(result.reasons).toContain(`signing_envelope_incomplete:${field}`);
    }
    // Control: the identical bid WITH the envelope is admitted.
    expect(door(signed()).ok, JSON.stringify(door(signed()).reasons)).toBe(true);
  });

  it.each(["signer_id", "key_id", "issued_at", "nonce", "schema_version"])(
    "rejects a submission missing %s",
    (field) => {
      const payload = signed();
      delete (payload as Record<string, unknown>)[field];
      const result = door(payload);
      expect(result.ok).toBe(false);
      expect(result.reasons.join(" ")).toContain("signing_envelope_incomplete");
    },
  );

  it.each([null, "", "   ", 12345, [], {a: 1}])(
    "rejects a submission whose signature is %s",
    (signature) => {
      const result = door(signed({signature}));
      expect(result.ok).toBe(false);
      expect(result.reasons).toContain(REASON_SIGNATURE_MISSING);
    },
  );

  it("still applies the whole R8/R18 table to a signed submission", () => {
    // The envelope is an ADDITIONAL condition, not a replacement.
    for (const [overrides, needle] of [
      [{offer: makeOffer({expires_at: LONG_EXPIRED})}, "offer_expired"],
      [{store_id: "store-bad"}, "store_blacklisted"],
      [{claims: [makeClaim("spf", 30, null)]}, "claim_without_provenance"],
    ] as const) {
      const result = door(signed(overrides as Record<string, unknown>));
      expect(result.ok, JSON.stringify(overrides)).toBe(false);
      expect(result.reasons.join(" ")).toContain(needle);
    }

    const flagged = door(signed({claims: [makeClaim("spf", 30, ASSERTED_PROVENANCE)]}));
    expect(flagged.ok, JSON.stringify(flagged.reasons)).toBe(true);
    expect(flagged.requires_verification).toBe(true);
  });

  it("never throws on a hostile submission", () => {
    // "reject" and "500" must not be the same observable at the public door.
    for (const payload of [null, "a raw string", [1, 2, 3], 42, {}, {store_id: {a: 1}}]) {
      const result = door(payload);
      expect(result.ok).toBe(false);
      expect(result.reasons.length).toBeGreaterThan(0);
    }
  });
});

describe("F4 — a stated UTC offset is part of the instant, not decoration", () => {
  it("honours a non-zero offset rather than discarding it", () => {
    for (const [stated, equivalentUtc] of [
      ["2026-01-01T00:00:00-08:00", "2026-01-01T08:00:00Z"],
      ["2026-01-01T00:00:00+05:30", "2025-12-31T18:30:00Z"],
    ]) {
      expect(parseTimestamp(stated)?.getTime(), `${stated} === ${equivalentUtc}`).toBe(
        parseTimestamp(equivalentUtc)?.getTime(),
      );
    }
    expect(parseTimestamp("2026-01-01T00:00:00-08:00")?.getTime()).not.toBe(
      parseTimestamp("2026-01-01T00:00:00Z")?.getTime(),
    );
  });

  it("lets the offset decide admit versus reject at the edge", () => {
    // A Pacific store's offer expiring at 00:00-08:00 is live at 04:00Z and dead at 09:00Z.
    const offer = makeOffer({expires_at: "2026-01-01T00:00:00-08:00"});
    const at = (now: string) =>
      validateBid(makeBid({offer}), {
        path: EXTERNAL_PATH,
        trustSnapshot: makeSnapshotTable(),
        now,
        listPrices: FIXTURE_ROSTER,
      });
    expect(at("2026-01-01T04:00:00Z").ok, "a live Pacific offer was rejected as expired").toBe(true);
    expect(at("2026-01-01T09:00:00Z").ok).toBe(false);
  });
});

describe("T-135 — the exclusivity property survives MOVING the claim", () => {
  // `claimProvenanceReasons` walked `record["claims"]` and nothing else. The Offer is inside the
  // bid boundary and carries claim material of its own: `offer.commitments` is a list of claims,
  // and `offer.discount` is stamped with the same `provenance` block a claim is. Relocating a
  // `seller_asserted` claim into either one walked straight past R8 with an unauthorised
  // discount attached. Same claim, same source, same bid; only the field moved.
  //
  // The Python peer asserts this exact table in `test_boundary_dual_path.py`; a fix that landed
  // on one side only would leave the two doors admitting different bids.
  const smugglingOffer = () =>
    makeOffer({
      commitments: [makeClaim("spf", 30, ASSERTED_PROVENANCE)],
      discount: {type: "percentage", value: 25.0, provenance: structuredClone(ASSERTED_PROVENANCE)},
    });

  it("refuses a seller_asserted claim relocated into the offer", () => {
    const smuggled = makeBid({
      claims: [makeClaim("free_returns", "30 days", HOOK_PROVENANCE)],
      offer: smugglingOffer(),
    });
    const result = check(smuggled, HOSTED_PATH);
    expect(
      result.ok,
      "a hosted bid with a seller_asserted claim in offer.commitments and an unauthorised 25% " +
        "discount was ADMITTED — moving the claim out of bid.claims defeated R8",
    ).toBe(false);
    expect(
      result.reasons.some((reason) => reason.startsWith(REASON_HOSTED_NON_HOOK_PROVENANCE)),
      `expected the provenance refusal, not an incidental one: ${result.reasons.join(", ")}`,
    ).toBe(true);

    // Positive control: hook provenance everywhere is admitted, so this is not "reject every
    // offer that carries commitments".
    const control = makeBid({
      claims: [makeClaim("free_returns", "30 days", HOOK_PROVENANCE)],
      offer: makeOffer({
        commitments: [makeClaim("spf", 30, HOOK_PROVENANCE)],
        discount: {type: "percentage", value: 25.0, provenance: structuredClone(HOOK_PROVENANCE)},
      }),
    });
    const admitted = check(control, HOSTED_PATH);
    expect(admitted.ok, admitted.reasons.join(", ")).toBe(true);
  });

  it("walks offer.commitments on its own", () => {
    const bid = makeBid({
      offer: makeOffer({commitments: [makeClaim("spf", 30, ASSERTED_PROVENANCE)]}),
    });
    const result = check(bid, HOSTED_PATH);
    expect(result.ok, `offer.commitments is not walked: ${result.reasons.join(", ")}`).toBe(false);
    expect(
      result.reasons.some((reason) => reason.startsWith(REASON_HOSTED_NON_HOOK_PROVENANCE)),
    ).toBe(true);

    const control = makeBid({
      offer: makeOffer({commitments: [makeClaim("spf", 30, HOOK_PROVENANCE)]}),
    });
    expect(check(control, HOSTED_PATH).ok).toBe(true);
  });

  it("walks offer.discount.provenance on its own", () => {
    // The third claim-bearing site, and the one that actually moves money: a 25% discount the
    // seller simply asserted is the payload R8 exclusivity exists to stop.
    const bid = makeBid({
      offer: makeOffer({
        discount: {
          type: "percentage",
          value: 25.0,
          provenance: structuredClone(ASSERTED_PROVENANCE),
        },
      }),
    });
    const result = check(bid, HOSTED_PATH);
    expect(
      result.ok,
      `offer.discount.provenance is not walked: ${result.reasons.join(", ")}`,
    ).toBe(false);
    expect(
      result.reasons.some((reason) => reason.startsWith(REASON_HOSTED_NON_HOOK_PROVENANCE)),
    ).toBe(true);

    // Control: the same discount, hook-minted, is admitted. The refusal is about the SOURCE.
    expect(check(makeBid(), HOSTED_PATH).ok).toBe(true);
  });
});

/**
 * The CROSS-LANGUAGE parity table for the claim-bearing sites.
 *
 * `boundary.py` is the other implementation of this door, and two doors that admit different bids
 * are worse than one door with a hole — the seller simply picks whichever one lets the bid
 * through. So these verdicts are asserted verbatim here and, case for case and string for string,
 * in `test_boundary_dual_path.py::PARITY_TABLE`. Changing one side alone turns the other red.
 *
 * `schema_invalid` reasons are filtered out of the comparison and only there: ajv and pydantic
 * spell a location differently and that text was never a cross-language contract. Every
 * provenance reason IS.
 */
const PARITY_TABLE: Record<
  string,
  {ok: boolean; reasons: string[]; requires_verification: boolean; unverified_claim_indexes: number[]}
> = {
  "claims_seller_asserted/hosted": {
    ok: false,
    reasons: ["hosted_non_hook_provenance:0:seller_asserted"],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
  "claims_seller_asserted/external": {
    ok: true,
    reasons: [],
    requires_verification: true,
    // R18 still routes a bid.claims assertion to verification, by index. Untouched.
    unverified_claim_indexes: [0],
  },
  "offer_commitment_seller_asserted/hosted": {
    ok: false,
    reasons: ["hosted_non_hook_provenance:offer.commitments[0]:seller_asserted"],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
  "offer_commitment_seller_asserted/external": {
    ok: false,
    // NOT flagged: `unverified_claim_indexes` addresses `bid.claims`, so there is no handle to
    // hand the verification queue for this site. Refused, with the site named.
    reasons: ["unverifiable_claim_site:offer.commitments[0]:seller_asserted"],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
  "offer_commitment_unknown_source/hosted": {
    ok: false,
    reasons: ["claim_provenance_unknown_source:offer.commitments[0]:vibes"],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
  "offer_commitment_without_provenance/hosted": {
    ok: false,
    reasons: ["claim_without_provenance:offer.commitments[0]"],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
  "offer_discount_seller_asserted/hosted": {
    ok: false,
    reasons: ["hosted_non_hook_provenance:offer.discount:seller_asserted"],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
  "offer_discount_seller_asserted/external": {
    ok: false,
    reasons: ["unverifiable_claim_site:offer.discount:seller_asserted"],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
  "offer_discount_without_provenance/hosted": {
    ok: false,
    // `Discount.provenance` is optional by schema, so this payload is schema-VALID. It is still
    // refused: the discount is the field that moves money, and "no provenance at all" is not a
    // weaker version of `seller_asserted`, it is the same statement with the label torn off.
    reasons: ["claim_without_provenance:offer.discount"],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
  "offer_without_a_discount/hosted": {
    ok: true,
    reasons: [],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
  "all_hook_provenanced/hosted": {
    ok: true,
    reasons: [],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
  "all_hook_provenanced/external": {
    ok: true,
    reasons: [],
    requires_verification: false,
    unverified_claim_indexes: [],
  },
};

/** The payload for one parity case. Mirrored by `parity_bid` in `test_boundary_dual_path.py`. */
function parityBid(name: string): unknown {
  switch (name) {
    case "claims_seller_asserted":
      return makeBid({claims: [makeClaim("spf", 30, ASSERTED_PROVENANCE)]});
    case "offer_commitment_seller_asserted":
      return makeBid({offer: makeOffer({commitments: [makeClaim("spf", 30, ASSERTED_PROVENANCE)]})});
    case "offer_commitment_unknown_source":
      return makeBid({
        offer: makeOffer({
          commitments: [makeClaim("spf", 30, {...HOOK_PROVENANCE, source: "vibes"})],
        }),
      });
    case "offer_commitment_without_provenance":
      return makeBid({offer: makeOffer({commitments: [makeClaim("spf", 30, null)]})});
    case "offer_discount_seller_asserted":
      return makeBid({
        offer: makeOffer({
          discount: {
            type: "percentage",
            value: 25.0,
            provenance: structuredClone(ASSERTED_PROVENANCE),
          },
        }),
      });
    case "offer_discount_without_provenance":
      return makeBid({offer: makeOffer({discount: {type: "percentage", value: 25.0}})});
    case "offer_without_a_discount":
      return makeBid({offer: makeOffer({discount: null})});
    case "all_hook_provenanced":
      return makeBid({offer: makeOffer({commitments: [makeClaim("spf", 30, HOOK_PROVENANCE)]})});
    default:
      throw new Error(`unknown parity case ${name}`);
  }
}

describe("T-135 parity — the claim-site verdicts match the Python peer", () => {
  it.each(Object.keys(PARITY_TABLE).sort())("%s", (caseName) => {
    const cut = caseName.lastIndexOf("/");
    const [name, path] = [caseName.slice(0, cut), caseName.slice(cut + 1)];
    const expected = PARITY_TABLE[caseName]!;
    const result = check(parityBid(name), path);

    const provenanceReasons = result.reasons.filter((r) => !r.startsWith("schema_invalid"));
    expect(provenanceReasons, caseName).toEqual(expected.reasons);
    expect(result.ok, `${caseName}: ${result.reasons.join(", ")}`).toBe(expected.ok);
    expect(result.requires_verification, caseName).toBe(expected.requires_verification);
    expect(result.unverified_claim_indexes, caseName).toEqual(expected.unverified_claim_indexes);
  });

  it("covers every claim-bearing site on both paths", () => {
    // Guards the `it.each`: an emptied table would register zero tests, which reads as green.
    const cases = Object.keys(PARITY_TABLE);
    expect(cases.length).toBe(12);
    const sites = new Set(cases.map((c) => c.slice(0, c.lastIndexOf("/"))));
    for (const site of [
      "claims_seller_asserted",
      "offer_commitment_seller_asserted",
      "offer_discount_seller_asserted",
    ]) {
      expect(sites.has(site), site).toBe(true);
    }
    for (const path of BOTH_PATHS) {
      expect(cases.some((c) => c.endsWith(`/${path}`)), path).toBe(true);
    }

    // The site labels the reasons are built from are the contract the table pins, and they are
    // the same literals the Python peer exports.
    expect(OFFER_COMMITMENTS_SITE).toBe("offer.commitments");
    expect(OFFER_DISCOUNT_SITE).toBe("offer.discount");
    expect(REASON_UNVERIFIABLE_CLAIM_SITE).toBe("unverifiable_claim_site");
  });
});

describe("T-135 — the offer walk refuses hostile shapes rather than throwing", () => {
  // The relocation exploit's next move is a malformed relocation: put the claim somewhere the
  // walk has to guess at. Both doors must REFUSE every one of these, and neither may throw.
  //
  // Only `ok` is compared with the Python peer here, not the reason text: ajv and pydantic
  // disagree about how to spell a shape error (`commitments` as an object is `schema_invalid`
  // here and an enumerable-of-keys there), and that spelling was never a cross-language
  // contract. "Is this bid admitted" is, and it is what an attacker cares about.
  const hostile: Record<string, unknown> = {
    "commitments as an object": makeOffer({commitments: {0: makeClaim("x", 1, ASSERTED_PROVENANCE)}}),
    "commitments as a string": makeOffer({commitments: "free_returns"}),
    "discount as a list": makeOffer({
      discount: [{type: "percentage", value: 25.0, provenance: structuredClone(ASSERTED_PROVENANCE)}],
    }),
    "discount as a bare number": makeOffer({discount: 25.0}),
    "discount with a null provenance": makeOffer({
      discount: {type: "percentage", value: 25.0, provenance: null},
    }),
    "discount with an empty provenance source": makeOffer({
      discount: {type: "percentage", value: 25.0, provenance: {...HOOK_PROVENANCE, source: ""}},
    }),
    "discount with an unknown provenance source": makeOffer({
      discount: {type: "percentage", value: 25.0, provenance: {...HOOK_PROVENANCE, source: "vibes"}},
    }),
  };

  it.each(Object.keys(hostile))("refuses %s on both paths", (name) => {
    for (const path of BOTH_PATHS) {
      const result = check(makeBid({offer: hostile[name]}), path);
      expect(result.ok, `${name}/${path} was admitted`).toBe(false);
      expect(result.reasons.length).toBeGreaterThan(0);
    }
  });

  it("still admits the offer shapes that are legitimately quiet", () => {
    // The positive controls, so the block above is not "reject every offer".
    for (const offer of [
      makeOffer({discount: null}),
      makeOffer({commitments: []}),
      makeOffer({commitments: [makeClaim("x", 1, HOOK_PROVENANCE)]}),
      makeOffer(),
    ]) {
      for (const path of BOTH_PATHS) {
        const result = check(makeBid({offer}), path);
        expect(result.ok, `${JSON.stringify(offer)}/${path}: ${result.reasons.join(", ")}`).toBe(
          true,
        );
      }
    }
  });
});

// -----------------------------------------------------------------------------------------
// T-184 — `table[key]` on a caller-supplied key is not a lookup, it is a prototype walk.
// -----------------------------------------------------------------------------------------

describe("T-184 — a store_id naming a prototype member is an unavailable read", () => {
  it.each(["__proto__", "constructor", "toString", "valueOf", "hasOwnProperty"])(
    "refuses store_id %s",
    (storeId) => {
      // Measured on the clean tree: `store_id=__proto__` made `table[key]` return
      // `Object.prototype` — an object, non-null, not an array, so `readRecord` accepted it —
      // the `trust_snapshot_unavailable` refusal never fired, and `row["blacklisted"]` was
      // `undefined` and therefore falsy. The bid was ADMITTED with no trust row behind it.
      for (const path of BOTH_PATHS) {
        const result = check(makeBid({store_id: storeId}), path);
        expect(result.ok, `store_id=${storeId} was admitted with no trust row`).toBe(false);
        expect(result.reasons.join(" ")).toContain(REASON_TRUST_SNAPSHOT_UNAVAILABLE);
      }
    },
  );

  it("still admits a store the snapshot really does own, however the table was built", () => {
    // The positive control, and the second half of the fix's claim: an OWN property is found
    // whether the table has a prototype or not.
    const bare = Object.create(null) as Record<string, unknown>;
    bare["store-1"] = {store_id: "store-1", score: 0.6, blacklisted: false};
    for (const snapshot of [makeSnapshotTable(), bare]) {
      for (const path of BOTH_PATHS) {
        const result = check(makeBid(), path, snapshot as never);
        expect(result.ok, result.reasons.join(", ")).toBe(true);
      }
    }
  });

  it("does not read an INHERITED row as a trust row", () => {
    // The general shape, not just the four famous names: anything reachable only through the
    // prototype is an unavailable read, because it is not something the snapshot said.
    const parent = {"store-9": {store_id: "store-9", score: 0.9, blacklisted: false}};
    const child = Object.create(parent) as Record<string, unknown>;
    const result = check(makeBid({store_id: "store-9"}), HOSTED_PATH, child as never);
    expect(result.ok, "an inherited row was read as a trust row").toBe(false);
    expect(result.reasons.join(" ")).toContain(REASON_TRUST_SNAPSHOT_UNAVAILABLE);
  });
});

// -----------------------------------------------------------------------------------------
// T-177 — the price wall, on the door the exchange actually runs.
//
// Line for line the peer of `test_boundary_dual_path.py`'s T-177 block. See `priceReasons` in
// `boundary.ts` for what the first relation deliberately cannot know.
// -----------------------------------------------------------------------------------------

function pricedOffer(unit: unknown, total: unknown, depth: unknown = 20.0, kind = "percentage") {
  return makeOffer({
    unit_price: unit,
    total_price: total,
    discount: {type: kind, value: depth, provenance: structuredClone(HOOK_PROVENANCE)},
  });
}

function listPriceClaim(value: unknown = 100.0) {
  return makeClaim("list_price", value, HOOK_PROVENANCE);
}

describe("T-177 — a bid may not charge more off than the depth it declares", () => {
  it.each(BOTH_PATHS)("refuses the 15.00-on-a-100.00-list reproduction on %s", (path) => {
    const bid = makeBid({
      claims: [listPriceClaim(100.0), makeClaim("authorized_discount_pct", 20.0)],
      offer: pricedOffer(15.0, 15.0),
    });
    const result = check(bid, path, undefined, LIST_100_ROSTER);
    expect(result.ok, "a 20% grant licensed an 85% discount").toBe(false);
    expect(result.reasons).toContain("price_under_declared_depth:offer.unit_price");

    // Control: the same bid at the price that depth prices out at.
    const honest = makeBid({
      claims: [listPriceClaim(100.0), makeClaim("authorized_discount_pct", 20.0)],
      offer: pricedOffer(80.0, 80.0),
    });
    const admitted = check(honest, path, undefined, LIST_100_ROSTER);
    expect(admitted.ok, admitted.reasons.join(", ")).toBe(true);
  });

  it.each(BOTH_PATHS)("reads the list price out of offer.commitments too on %s", (path) => {
    const bid = makeBid({
      offer: makeOffer({
        unit_price: 15.0,
        total_price: 15.0,
        commitments: [listPriceClaim(100.0)],
        discount: {type: "percentage", value: 20.0, provenance: structuredClone(HOOK_PROVENANCE)},
      }),
    });
    const result = check(bid, path, undefined, LIST_100_ROSTER);
    expect(result.ok, "relocating the list price defeated the price wall").toBe(false);
    expect(result.reasons).toContain("price_under_declared_depth:offer.unit_price");
  });

  it.each(BOTH_PATHS)("refuses a total that undercuts the declared depth on %s", (path) => {
    const result = check(makeBid({offer: pricedOffer(100.0, 15.0)}), path, undefined, LIST_100_ROSTER);
    expect(result.ok).toBe(false);
    expect(result.reasons).toContain("price_under_declared_depth:offer.total_price");

    // Controls: the honest total, and a total for a larger quantity.
    for (const total of [80.0, 240.0]) {
      const ok = check(makeBid({offer: pricedOffer(100.0, total)}), path, undefined, LIST_100_ROSTER);
      expect(ok.ok, ok.reasons.join(", ")).toBe(true);
    }
  });

  it.each(BOTH_PATHS)("is one-sided — a shallower discount than declared admits on %s", (path) => {
    const generous = makeBid({claims: [listPriceClaim(100.0)], offer: pricedOffer(95.0, 95.0)});
    const verdict = check(generous, path, undefined, LIST_100_ROSTER);
    expect(verdict.ok, verdict.reasons.join(", ")).toBe(true);
  });

  it.each(BOTH_PATHS)("leaves a cent of slack for a rounded price on %s", (path) => {
    // The roster AGREES with the carried claim at 19.99 and authorizes the 15% declared, so the
    // only thing left deciding these two verdicts is the cent of tolerance this test is named for.
    const roster = {"prod-1": {list_price: 19.99, max_discount_pct: 15.0}};
    const rounded = makeBid({
      claims: [listPriceClaim(19.99)],
      offer: pricedOffer(16.99, 16.99, 15.0),
    });
    const verdict = check(rounded, path, undefined, roster);
    expect(verdict.ok, verdict.reasons.join(", ")).toBe(true);

    const under = makeBid({claims: [listPriceClaim(19.99)], offer: pricedOffer(15.99, 15.99, 15.0)});
    expect(check(under, path, undefined, roster).ok).toBe(false);
  });

  it.each(BOTH_PATHS)("refuses a depth it cannot read rather than skipping it on %s", (path) => {
    const cases: Array<[unknown, string]> = [
      [pricedOffer(15.0, 15.0, 10.0, "amount"), "offer.discount:amount"],
      [pricedOffer(15.0, 15.0, 150.0), "offer.discount:depth_out_of_range"],
      [pricedOffer(15.0, 15.0, -20.0), "offer.discount:depth_out_of_range"],
      [pricedOffer(15.0, 15.0, "20"), "offer.discount:depth_not_a_number"],
      [pricedOffer(15.0, 15.0, true), "offer.discount:depth_not_a_number"],
    ];
    for (const [offer, needle] of cases) {
      const result = check(
        makeBid({claims: [listPriceClaim(100.0)], offer}),
        path,
        undefined,
        LIST_100_ROSTER,
      );
      expect(result.ok, `${needle} was admitted`).toBe(false);
      expect(result.reasons.join(" ")).toContain(needle);
    }

    // Control: the same offer with a depth the door CAN read, priced honestly, is admitted — so
    // this is not "refuse every offer carrying a discount". The Python peer asserts it too.
    const legible = check(
      makeBid({claims: [listPriceClaim(100.0)], offer: pricedOffer(80.0, 80.0)}),
      path,
      undefined,
      LIST_100_ROSTER,
    );
    expect(legible.ok, legible.reasons.join(", ")).toBe(true);
  });

  it.each(BOTH_PATHS)("answers to the carried list price even at a zero depth on %s", (path) => {
    // A roster that PRICES the product and authorizes nothing on it, which is what makes
    // "answers to the LIST price" the literal bound here: with a cap on the row, a zero-depth
    // offer would be measured against the cap instead and 60.00 would admit.
    const unauthorizing100 = {"prod-1": 100.0};
    const under = makeBid({claims: [listPriceClaim(100.0)], offer: pricedOffer(60.0, 60.0, 0.0)});
    const refused = check(under, path, undefined, unauthorizing100);
    expect(refused.ok).toBe(false);
    expect(refused.reasons).toContain("price_under_declared_depth:offer.unit_price");

    const atList = makeBid({
      claims: [listPriceClaim(100.0)],
      offer: pricedOffer(100.0, 100.0, 0.0),
    });
    const admitted = check(atList, path, undefined, unauthorizing100);
    expect(admitted.ok, admitted.reasons.join(", ")).toBe(true);
  });

  it.each(BOTH_PATHS)("refuses an illegible or contradictory list price on %s", (path) => {
    const unreadable = makeBid({claims: [listPriceClaim("n/a")], offer: pricedOffer(15.0, 15.0)});
    const illegible = check(unreadable, path, undefined, LIST_100_ROSTER);
    expect(illegible.ok).toBe(false);
    expect(illegible.reasons.join(" ")).toContain("unreadable_list_price");

    const ambiguous = makeBid({
      claims: [listPriceClaim(100.0), listPriceClaim(120.0)],
      offer: pricedOffer(80.0, 80.0),
    });
    const contradictory = check(ambiguous, path, undefined, LIST_100_ROSTER);
    expect(contradictory.ok).toBe(false);
    expect(contradictory.reasons.join(" ")).toContain("ambiguous_list_price");

    // Control: the same list price stated twice is not a contradiction.
    const twice = makeBid({
      claims: [listPriceClaim(100.0), listPriceClaim(100.0)],
      offer: pricedOffer(80.0, 80.0),
    });
    const admitted = check(twice, path, undefined, LIST_100_ROSTER);
    expect(admitted.ok, admitted.reasons.join(", ")).toBe(true);
  });

  it.each(BOTH_PATHS)(
    "answers one identical refusal to every spelling of no roster on %s",
    (path) => {
      // THE DOCUMENTED GAP — CLOSED BY T-306/T-336, and this test is the record of that.
      //
      // JUSTIFY-TEST-EDIT. Two assertions here were REPLACED, not relaxed. They were:
      //
      //     const silent = makeBid({offer: pricedOffer(15.0, 15.0)});
      //     const result = check(silent, path);            // `check` passed NO roster
      //     expect(result.ok, result.reasons.join(", ")).toBe(true);
      //     expect(result.reasons.filter((r) => r.startsWith("price_"))).toEqual([]);
      //
      // * WHAT THEY CLAIMED ABOUT THE PRODUCT. A bid declaring a 20% discount, charging 15.00,
      //   carrying no `list_price` claim and reaching a door that was handed no roster is
      //   ADMITTED, and the price wall says nothing at all about it.
      // * THE REQUIREMENT THEY ENCODED, AND WHERE IT CAME FROM. `e1a66b5` ("put the price wall on
      //   the validating door") introduced them as the deliberate boundary of that wall: the door
      //   holds no catalog, so with no list price from anywhere the first relation had no number
      //   to be a percentage OF. The abstention was called the opt-in property the parameter
      //   rested on.
      // * WOULD THIS TEST STILL BE WRONG IF THE SOURCE CHANGE WERE REVERTED? YES. Revert
      //   `boundary.ts` to the abstention and the assertion goes green again — and it is still
      //   false, because it was never a statement about a MISSING list price.
      //   `priceReasons(bid, {listPrices: {}})` refused the identical bid the whole time it was in
      //   the tree. The assertion pinned "the caller said nothing" as strictly MORE permissive
      //   than "the caller said it holds no catalog", which is a claim about which ARGUMENTS were
      //   supplied, not about the bid. That is T-306: an omission is the call a caller makes by
      //   forgetting, so the accident was the one that paid, on the money path.
      // * INDEPENDENT PROOF THE CODE IS RIGHT. `test_repro_open_tickets.py::test_t306_...` and
      //   `::test_t307_...` were written as reproductions (`fbc4636`) and failed against the old
      //   behaviour; they pass now. Neither was authored here and neither compares against
      //   anything this file controls.
      // * BLAST RADIUS. The same contract was asserted by the Python peer
      //   (`test_the_wall_answers_one_identical_refusal_to_every_spelling_of_no_roster`, which
      //   carried the old name `test_the_wall_abstains_deliberately_...` when this was written), by
      //   `price_parity_corpus.json`'s `no_list_price_carried` row (in both languages at once),
      //   and by three assertions in `test_boundary_price_roster.py`. All are changed with this
      //   one; none is deleted.
      //
      // What is asserted instead is the contract T-306 requires, and it is STRICTLY STRONGER: the
      // door answers a bid the same way whether the roster is omitted, spelled `null`, or spelled
      // `{}` — and that answer is a refusal that NAMES the missing input.
      const silent = makeBid({offer: pricedOffer(15.0, 15.0)});
      const table = makeSnapshotTable();

      // Byte-identical across all three spellings of "no roster" — the option OMITTED, the option
      // spelled `null`, the option spelled `{}`. Asserted BEFORE the verdict itself, so a future
      // change cannot satisfy it by making all three permissive again without also flipping the
      // refusal below.
      const verdicts = [
        validateBid(silent, {path, trustSnapshot: table, now: NOW}),
        check(silent, path, table, null),
        check(silent, path, table, {}),
      ];
      const distinct = new Set(verdicts.map((v) => JSON.stringify([v.ok, v.reasons])));
      expect(distinct.size, JSON.stringify(verdicts.map((v) => v.reasons))).toBe(1);

      const result = verdicts[0]!;
      expect(result.ok, "an omitted roster was more permissive than an empty one").toBe(false);
      expect(result.reasons).toEqual([
        `${REASON_PRICE_UNRECONCILABLE}:${OFFER_DISCOUNT_SITE}:${ROSTER_MAX_DISCOUNT_UNAVAILABLE}`,
        `${REASON_PRICE_UNRECONCILABLE}:${OFFER_UNIT_PRICE_SITE}:${ROSTER_LIST_PRICE_UNAVAILABLE}`,
      ]);

      // ...and the abstention is GONE rather than moved: a caller that CAN price the product
      // still gets the ordinary arithmetic, and the same silent bid is refused by the wall it
      // underprices rather than by the missing roster.
      const priced = check(silent, path, table, LIST_100_ROSTER);
      expect(priced.ok).toBe(false);
      expect(priced.reasons).toEqual([
        `${REASON_PRICE_UNDER_DECLARED_DEPTH}:${OFFER_UNIT_PRICE_SITE}`,
      ]);

      // And the moment the bid DOES say what it is discounting from, the same offer refuses.
      const named = makeBid({claims: [listPriceClaim(100.0)], offer: pricedOffer(15.0, 15.0)});
      expect(check(named, path, table, LIST_100_ROSTER).ok).toBe(false);
    },
  );

  it("never throws on a hostile offer", () => {
    for (const offer of [null, "an offer", 42, [1, 2], true, {}, {unit_price: NaN}]) {
      for (const path of BOTH_PATHS) {
        const result = check(makeBid({offer}), path);
        expect(result.ok).toBe(false);
        expect(result.reasons.length).toBeGreaterThan(0);
      }
    }
  });
});
describe("T-177 price parity — the SHARED corpus `test_boundary_dual_path.py` also drives", () => {
  // This table used to live here AND in `test_boundary_dual_path.py`, hand-copied, payload
  // builders included. Two copies of a parity table are not a parity test: a divergence
  // introduced on one side alone gets edited into that side's copy and both suites stay green
  // while the doors disagree — which is exactly what happened when the T-177 roster landed in
  // `boundary.py` and not in `boundary.ts` and both copies went on asserting
  // `no_list_price_carried: ok=true`. The cases now live once, as wire payloads, in
  // `price_parity_corpus.json`, and each suite drives its own door with them.
  interface ParityCase {
    name: string;
    why: string;
    bid: unknown;
    list_prices: Record<string, unknown> | null;
    max_discount_pct: number | null;
    ok: boolean;
    reasons: string[];
  }
  const cases = corpus.cases as unknown as ParityCase[];
  const byName = new Map(cases.map((c) => [c.name, c]));

  it.each(cases.map((c) => c.name))("matches the Python verdict for %s", (name) => {
    const expected = byName.get(name)!;
    const result = validateBid(expected.bid, {
      path: corpus.path,
      trustSnapshot: makeSnapshotTable(),
      now: corpus.now,
      listPrices: (expected.list_prices ?? undefined) as PriceRosterMap | undefined,
      maxDiscountPct: expected.max_discount_pct ?? undefined,
    });
    // `schema_invalid:` reasons name ajv/pydantic field paths, the one part of the vocabulary the
    // two implementations are not expected to spell identically.
    const priced = result.reasons.filter((r) => !r.startsWith("schema_invalid"));
    expect(priced, `${name}: ${result.reasons.join(", ")}`).toEqual(expected.reasons);
    expect(result.ok, `${name}: ${result.reasons.join(", ")}`).toBe(expected.ok);
  });

  it("is not quietly empty, and the reason vocabulary is the Python peer's", () => {
    // A table of nothing but rejections is satisfied by a door that refuses everything, and a
    // table of nothing but admissions by a door with no wall in it at all.
    expect(cases.length).toBeGreaterThanOrEqual(20);
    expect(byName.size).toBe(cases.length);
    expect(cases.filter((c) => c.ok).length).toBeGreaterThanOrEqual(5);
    expect(cases.filter((c) => !c.ok).length).toBeGreaterThanOrEqual(12);
    for (const c of cases) expect(c.reasons.length > 0, c.name).toBe(!c.ok);

    expect(OFFER_UNIT_PRICE_SITE).toBe("offer.unit_price");
    expect(OFFER_TOTAL_PRICE_SITE).toBe("offer.total_price");
    expect(REASON_PRICE_UNDER_DECLARED_DEPTH).toBe("price_under_declared_depth");
    expect(REASON_PRICE_UNRECONCILABLE).toBe("price_unreconcilable");
    expect(REASON_DISCOUNT_OVER_AUTHORIZED_DEPTH).toBe("discount_over_authorized_depth");
    expect(LIST_PRICE_CLAIM_KEY).toBe("list_price");
    expect(MAX_DISCOUNT_ROSTER_KEY).toBe("max_discount_pct");
    expect(PRICE_RECONCILIATION_TOLERANCE).toBe(0.01);
  });

  it("still covers every case the hand-copied table pinned", () => {
    for (const name of [
      "charges_under_the_carried_list_price",
      "total_under_the_stated_unit_price",
      "amount_discount",
      "depth_out_of_range",
      "ambiguous_list_price",
      "unreadable_list_price",
      "honest_price",
      "no_list_price_carried",
    ]) {
      expect(byName.has(name), name).toBe(true);
    }
  });

  it("keeps the two rows that grade the third and fourth sites", () => {
    // The T-306 repair has FOUR sites. `authorizedDepth` and `rosterListPrice` are caught by many
    // tests; the `authorized` fallback and the cap read in the zero-depth `else` are caught by
    // exactly one corpus row each, in both languages, and by nothing else. Both were added only
    // after a per-site mutation sweep found them ungraded — reverting either alone left all 174
    // tests here green. Every other guard survives deleting them (40 cases, 11 ok, 29 not-ok
    // still clears every threshold), so this assertion is the only thing between those rows and a
    // silent deletion that reopens the fail-open. The Python peer asserts the same two names.
    for (const name of [
      "a_carried_claim_is_measured_against_the_full_list_price_with_no_roster",
      "a_call_wide_ceiling_is_read_at_a_zero_declared_depth_with_no_roster",
    ]) {
      expect(byName.has(name), `${name} grades a source site nothing else grades`).toBe(true);
      const row = byName.get(name)!;
      expect(row.list_prices, name).toBe(null);
      expect((row.bid as {offer: {unit_price: number}}).offer.unit_price, name).toBe(85.0);
    }
  });

  it("pins the price floor in both directions — T-278's gate for the T-250 fix", () => {
    // T-250 replaced an exact `priced === 0` equality with a THRESHOLD, because an equality has
    // exactly one satisfying value: 0.001 for a product the roster prices at 100.00 was refused
    // by nothing at all. The Python half of that fix was verified through the exchange; the
    // TypeScript half shipped with no case in this corpus. Measured against the pre-T-278 corpus
    // by re-introducing the defect verbatim (`priced === 0`, threshold branch deleted):
    // 683/683 vitest tests GREEN. These rows are what makes that red.
    //
    // Note what was NOT true: deleting the whole floor block was already caught, by the four
    // zero-price rows the block's OTHER branch (`not_positive`) answers. It was the THRESHOLD
    // half — everything between zero and the floor — that no case reached.
    const floorRows = cases.filter((c) =>
      c.reasons.some((r) => r.endsWith(`:${ROSTER_PRICE_BELOW_FLOOR}`)),
    );
    expect(floorRows.length, "the corpus carries no below_price_floor case").toBeGreaterThanOrEqual(
      5,
    );

    // Both halves of `max(listed * fraction, one minor unit)` are exercised, on a listing where
    // each one dominates: 0.005 clears 0.1% of a 0.50 product and is still half a cent, and 0.05
    // is a payable amount that is not a price for a 100.00 one. Either half alone admits what the
    // other refuses, so a corpus reaching only one of them gates only half the arithmetic.
    const listedIn = (c: ParityCase) =>
      (c.list_prices as Record<string, {list_price: number}> | null)?.["prod-1"]?.list_price;
    const listings = new Set(floorRows.map(listedIn));
    expect(listings.has(100.0), "no floor case where the proportional half dominates").toBe(true);
    expect(listings.has(0.5), "no floor case where the absolute half dominates").toBe(true);

    // ...and the boundary value itself is ADMITTED, on both halves. `<`, never `<=`: a wall that
    // refuses the lowest number the contract still calls a price is closed, not fail-closed, and
    // that is the direction this class of bug lives in once the equality is gone.
    for (const name of [
      "a_price_exactly_at_the_proportional_floor_is_admitted",
      "a_price_exactly_at_the_absolute_floor_is_admitted",
    ]) {
      const at = byName.get(name);
      expect(at, name).toBeDefined();
      expect(at!.ok, name).toBe(true);
      expect(at!.reasons, name).toEqual([]);
    }

    expect(PRICE_FLOOR_FRACTION).toBe(0.001);
    expect(MINIMUM_PAYABLE_AMOUNT).toBe(0.01);
    expect(priceFloor(100.0)).toBe(0.1);
    expect(priceFloor(0.5)).toBe(MINIMUM_PAYABLE_AMOUNT);
    // The floor is dropped, not clamped, below one minor unit — a roster genuinely pricing
    // something at 0.005 is not describing a giveaway, and a floor above its own list price
    // would refuse every bid on it.
    expect(priceFloor(0.005)).toBeLessThan(MINIMUM_PAYABLE_AMOUNT);

    // THE CEILING on `PRICE_FLOOR_FRACTION`, in machine-checkable form.
    // `apps/exchange/tests/test_auction_price_wall.py::
    // test_r10_still_admits_an_undeclared_undercut_where_nothing_is_authorized` pins 1.00 on an
    // UNCAPPED 100.00 row as ADMITTED, so a floor at or above 1.00 there breaks a frozen
    // assertion. That ceiling binds the FLOOR only, and only on that row: the sibling test
    // refuses 1.00 on a CAPPED row — by the depth relation, not by the floor — and the two
    // `an_undeclared_dollar_on_a(n)_..._hundred` rows above hold that distinction apart.
    expect(priceFloor(100.0)).toBeLessThan(1.0);
  });

  it("exposes the price walk on its own, for a caller holding no trust snapshot", () => {
    // The peer of `contracts.boundary.price_reasons`. Same arithmetic, no eligibility gate.
    const roster = {"prod-1": {list_price: 100.0, max_discount_pct: 20.0}};
    const deep = makeBid({offer: pricedOffer(15.0, 15.0, 85.0)});

    // JUSTIFY-TEST-EDIT (T-306/T-336). This line used to read `expect(priceReasons(deep))
    // .toEqual([])` — the standalone walk's copy of the same defect the door carried: the roster
    // NOBODY PASSED was silent while the roster passed EMPTY refused, so forgetting the argument
    // was more permissive than passing it empty, on the money path. It would go green again the
    // moment the abstention were restored, which is what makes it a defect and not a
    // requirement. Replaced by the STRONGER property: every spelling of "no roster" is one
    // answer, and that answer NAMES the inputs it is missing.
    for (const reasons of [
      priceReasons(deep),
      priceReasons(deep, {}),
      priceReasons(deep, {listPrices: null as unknown as PriceRosterMap}),
      priceReasons(deep, {listPrices: {}}),
    ]) {
      expect(reasons).toEqual([
        `${REASON_PRICE_UNRECONCILABLE}:${OFFER_DISCOUNT_SITE}:${ROSTER_MAX_DISCOUNT_UNAVAILABLE}`,
        `${REASON_PRICE_UNRECONCILABLE}:${OFFER_UNIT_PRICE_SITE}:${ROSTER_LIST_PRICE_UNAVAILABLE}`,
      ]);
    }

    expect(priceReasons(deep, {listPrices: roster})).toEqual([
      `${REASON_DISCOUNT_OVER_AUTHORIZED_DEPTH}:${OFFER_DISCOUNT_SITE}`,
      `${REASON_PRICE_UNDER_DECLARED_DEPTH}:${OFFER_UNIT_PRICE_SITE}`,
    ]);
    expect(priceReasons(makeBid({offer: pricedOffer(80.0, 80.0)}), {listPrices: roster})).toEqual(
      [],
    );
    // Never throws, on anything.
    for (const hostile of [null, undefined, 42, "a bid", [1, 2]]) {
      expect(() => priceReasons(hostile, {listPrices: roster})).not.toThrow();
    }
  });
});

describe("T-195 — offer.commitments may not be spelled null", () => {
  it.each(BOTH_PATHS)("refuses commitments: null on %s, as ajv always did", (path) => {
    // The schema declares a NON-nullable array with `default: []`, so ajv refused this while
    // the generated pydantic model accepted it — and that nullable spelling was the one shape
    // of `offer.commitments` the Python claim walk skipped, on the field the walk covers.
    const result = check(makeBid({offer: makeOffer({commitments: null})}), path);
    expect(result.ok).toBe(false);
    expect(result.reasons.join(" ")).toContain(REASON_SCHEMA_INVALID);
  });

  it("still admits the two spellings the schema does allow", () => {
    const withoutKey = makeOffer();
    delete (withoutKey as Record<string, unknown>)["commitments"];
    for (const offer of [makeOffer({commitments: []}), withoutKey]) {
      const result = check(makeBid({offer}), HOSTED_PATH);
      expect(result.ok, result.reasons.join(", ")).toBe(true);
    }
  });
});

describe("claims lists — the shapes the Python peer now refuses too", () => {
  // `Array.isArray` has always been this door's rule; the Python peer accepted any iterable, so
  // a generator of claims was walked once, drained, and admitted with its claims unread. These
  // pin the shared verdict rather than a TypeScript-only one.
  it.each(BOTH_PATHS)("refuses a non-array claims list on %s", (path) => {
    for (const shape of [{0: makeClaim()}, "free_returns", 42, new Set([makeClaim()])]) {
      expect(check(makeBid({claims: shape}), path).ok, JSON.stringify(shape)).toBe(false);
      expect(check(makeBid({offer: makeOffer({commitments: shape})}), path).ok).toBe(false);
    }

    // Control: a real array is still walked and admitted.
    expect(check(makeBid({claims: [makeClaim()]}), path).ok).toBe(true);
  });
});

describe("R12 — a trust-snapshot row that is not an object is an unavailable read", () => {
  // This door's `readRecord` has always refused these; the Python peer read `blacklisted` off
  // them with `getattr`, got `False`, and ADMITTED the store. Pinned here so the two doors
  // cannot drift apart on it again.
  it.each(BOTH_PATHS)("refuses a non-object row on %s", (path) => {
    for (const row of [1, "x", [], true, 3.5, 0, "", 0.0, ["blacklisted"], null, undefined]) {
      const snapshot = {"store-1": row};
      const result = check(makeBid(), path, snapshot as never);
      expect(result.ok, `a trust row of ${JSON.stringify(row)} admitted the store`).toBe(false);
      expect(result.reasons.join(" ")).toContain(REASON_TRUST_SNAPSHOT_UNAVAILABLE);
    }

    // Controls: a real row admits; a real blacklisted row denies for its own reason.
    expect(check(makeBid(), path).ok).toBe(true);
    expect(check(makeBid({store_id: "store-bad"}), path).reasons.join(" ")).toContain(
      REASON_STORE_BLACKLISTED,
    );
  });
});
