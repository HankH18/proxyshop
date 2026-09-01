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
  parseTimestamp,
  validateBid,
} from "../src/ts/boundary.js";
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
