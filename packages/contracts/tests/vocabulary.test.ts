/**
 * The closed vocabularies, and the OpenAPI contracts, from the TypeScript side.
 *
 * Every runtime list is asserted to be EXACTLY the enum in the schema bundle. Hand-written
 * constants that only agree with the schema by inspection are how a vocabulary quietly forks.
 */
import {describe, expect, it} from "vitest";

import {protocolSchema} from "../src/ts/schemas.js";
import {
  CLAIM_VERIFICATION_STATUSES,
  CONSTRAINT_OPS,
  ENVELOPE_ACTIVATIONS,
  HOOK_PROVENANCE_SOURCES,
  LABEL_FROM_THEIR_WEBSITE,
  LABEL_STORE_CONFIRMED,
  LEDGER_EVENT_KINDS,
  LEDGER_JOIN_KEYS,
  PREFERENCE_DIRECTIONS,
  PROVENANCE_AUTHORITY_RANK,
  PROVENANCE_BUYER_LABELS,
  PROVENANCE_SOURCES,
  SHORTLIST_SLOT_NAMES,
  STORE_TIERS,
  TRUST_DIMENSIONS,
  buyerLabel,
} from "../src/ts/vocabulary.js";

function enumOf(name: string): unknown[] {
  return (protocolSchema.$defs[name] as {enum: unknown[]}).enum;
}

describe("every published list is exactly the schema's enum", () => {
  it.each([
    ["LedgerEventKind", LEDGER_EVENT_KINDS],
    ["TrustDimension", TRUST_DIMENSIONS],
    ["ProvenanceSource", PROVENANCE_SOURCES],
    ["ConstraintOp", CONSTRAINT_OPS],
    ["PreferenceDirection", PREFERENCE_DIRECTIONS],
    ["EnvelopeActivation", ENVELOPE_ACTIVATIONS],
    ["ShortlistSlotName", SHORTLIST_SLOT_NAMES],
    ["ClaimVerificationStatus", CLAIM_VERIFICATION_STATUSES],
    ["StoreTier", STORE_TIERS],
  ])("%s", (name, published) => {
    expect([...published].sort()).toEqual([...enumOf(name)].sort());
  });
});

describe("C11 / D24 — the ledger vocabulary", () => {
  it("is exactly 18 kinds", () => {
    expect(LEDGER_EVENT_KINDS).toHaveLength(18);
  });

  it("carries the five kinds T-010 added, and the thirteen DESIGN pinned", () => {
    for (const added of [
      "auction_opened",
      "auction_closed",
      "offer_integrity",
      "blacklisted",
      "blacklist_expired",
    ]) {
      expect(LEDGER_EVENT_KINDS).toContain(added);
    }
    for (const original of ["bid_placed", "checkout_pixel", "order_paid", "policy_event"]) {
      expect(LEDGER_EVENT_KINDS).toContain(original);
    }
  });

  it("pins the join keys in snake_case", () => {
    expect([...LEDGER_JOIN_KEYS]).toEqual([
      "checkout_token",
      "order_ref",
      "client_id",
      "discount_code",
    ]);
  });
});

describe("D53 — six trust dimensions", () => {
  it("names catalog_claim_accuracy alongside the five transaction dimensions", () => {
    expect(TRUST_DIMENSIONS).toHaveLength(6);
    expect(TRUST_DIMENSIONS).toContain("catalog_claim_accuracy");
  });
});

describe("R8 — hook versus non-hook provenance", () => {
  it("treats seller_asserted as the only non-hook source", () => {
    const nonHook = PROVENANCE_SOURCES.filter((s) => !HOOK_PROVENANCE_SOURCES.includes(s));
    expect(nonHook).toEqual(["seller_asserted"]);
  });
});

describe("R2 / D30 — buyer-facing provenance labels", () => {
  it.each([
    ["owner_statement", LABEL_STORE_CONFIRMED],
    ["envelope_rule", LABEL_STORE_CONFIRMED],
    ["learned_policy", LABEL_STORE_CONFIRMED],
    ["pixel_feed", LABEL_STORE_CONFIRMED],
    ["network", LABEL_STORE_CONFIRMED],
    ["scraped", LABEL_FROM_THEIR_WEBSITE],
  ])("labels %s as %s", (source, expected) => {
    expect(buyerLabel(source)).toBe(expected);
  });

  it("gives a seller-asserted claim neither R2 label", () => {
    const label = buyerLabel("seller_asserted");
    expect([LABEL_STORE_CONFIRMED, LABEL_FROM_THEIR_WEBSITE]).not.toContain(label);
    expect(label.toLowerCase()).toContain("unverified");
  });

  it("is total over the closed provenance enum", () => {
    // A renderer must never have to invent a label for a source the schema allows.
    expect(Object.keys(PROVENANCE_BUYER_LABELS).sort()).toEqual([...PROVENANCE_SOURCES].sort());
  });

  it("throws for an unknown source rather than defaulting to store-confirmed", () => {
    // Defaulting would tell a buyer the store vouched for something it never saw.
    expect(() => buyerLabel("invented-source")).toThrow();
  });

  it("publishes an authority rank per source, most authoritative first", () => {
    expect(Object.keys(PROVENANCE_AUTHORITY_RANK).sort()).toEqual([...PROVENANCE_SOURCES].sort());
    expect(PROVENANCE_AUTHORITY_RANK.owner_statement).toBe(1);
    expect(PROVENANCE_AUTHORITY_RANK.seller_asserted).toBe(5);
    for (const source of HOOK_PROVENANCE_SOURCES) {
      expect(PROVENANCE_AUTHORITY_RANK[source]).toBeLessThan(
        PROVENANCE_AUTHORITY_RANK.seller_asserted,
      );
    }
  });
});

describe("D28 — store tiers", () => {
  it("is exactly 0, 1 and 2", () => {
    expect([...STORE_TIERS]).toEqual([0, 1, 2]);
  });
});

describe("the label strings themselves, not the constants that name them", () => {
  it("pins the two R2 literals", () => {
    // Comparing `buyerLabel(x)` to `LABEL_STORE_CONFIRMED` imported from the module under test is
    // a tautology: change the constant and both sides move together. These are the strings the
    // buyer actually reads, and the Python peer pins the same two.
    expect(LABEL_STORE_CONFIRMED).toBe("store-confirmed");
    expect(LABEL_FROM_THEIR_WEBSITE).toBe("from their website");
  });

  it("labels each source with the literal the frozen suite requires", () => {
    expect(buyerLabel("owner_statement")).toBe("store-confirmed");
    expect(buyerLabel("envelope_rule")).toBe("store-confirmed");
    expect(buyerLabel("learned_policy")).toBe("store-confirmed");
    expect(buyerLabel("pixel_feed")).toBe("store-confirmed");
    expect(buyerLabel("network")).toBe("store-confirmed");
    expect(buyerLabel("scraped")).toBe("from their website");
  });

  it("keeps the seller-asserted badge out of the two R2 labels", () => {
    expect(["store-confirmed", "from their website"]).not.toContain(buyerLabel("seller_asserted"));
    expect(buyerLabel("seller_asserted").toLowerCase()).toContain("unverified");
  });
});

describe("the vocabularies are non-empty", () => {
  it("guards every it.each above", () => {
    // `it.each([])` registers zero tests silently, which reads as green. These lengths are what
    // makes emptying a vocabulary a failure rather than a disappearance.
    expect(LEDGER_EVENT_KINDS.length).toBe(18);
    expect(TRUST_DIMENSIONS.length).toBe(6);
    expect(PROVENANCE_SOURCES.length).toBe(7);
    expect(HOOK_PROVENANCE_SOURCES.length).toBe(6);
    expect(CONSTRAINT_OPS.length).toBe(5);
    expect(PREFERENCE_DIRECTIONS.length).toBe(3);
    expect(ENVELOPE_ACTIVATIONS.length).toBe(3);
    expect(SHORTLIST_SLOT_NAMES.length).toBe(4);
    expect(CLAIM_VERIFICATION_STATUSES.length).toBe(4);
    expect(STORE_TIERS.length).toBe(3);
    expect(LEDGER_JOIN_KEYS.length).toBe(4);
  });
});
