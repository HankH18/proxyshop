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
  REASON_STORE_BLACKLISTED,
  REASON_TRUST_SNAPSHOT_UNAVAILABLE,
  REASON_UNKNOWN_PATH,
  parseTimestamp,
  validateBid,
  validateExternalSubmission,
} from "../src/ts/boundary.js";
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

const BOTH_PATHS = [HOSTED_PATH, EXTERNAL_PATH] as const;

function check(bid: unknown, path: string, snapshot = makeSnapshotTable()) {
  return validateBid(bid, {path, trustSnapshot: snapshot, now: NOW});
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
    validateExternalSubmission(submission, {trustSnapshot: makeSnapshotTable(), now: NOW});

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
