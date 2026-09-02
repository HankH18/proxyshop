/**
 * The generated TypeScript half of the contract: the types exist, and the runtime validators
 * built from the same schema accept and reject the same things the Python models do.
 *
 * The type-level assertions are `satisfies` checks — they are compile-time only, and vitest runs
 * these files through esbuild, which strips types without checking them. So every type assertion
 * here is PAIRED with a runtime schema check, which is the half that actually fails a build.
 */
import {describe, expect, it} from "vitest";

import type {
  Bid,
  Envelope,
  Intent,
  LedgerEvent,
  Provenance,
  TrustSnapshot,
} from "../generated/ts/protocol.schema.d.ts";
import {isValid, schemaFor, schemaNames, validationErrors} from "../src/ts/schemas.js";
import {TRUST_DIMENSIONS} from "../src/ts/vocabulary.js";
import {
  ASSERTED_PROVENANCE,
  HOOK_PROVENANCE,
  makeBid,
  makeIntent,
  makeOffer,
  makeTrustDims,
  makeTrustSnapshot,
} from "./fixtures.js";

const PINNED = [
  "Intent",
  "BuyerProfile",
  "BidRequest",
  "Claim",
  "Provenance",
  "Offer",
  "Bid",
  "VerificationResult",
  "Shortlist",
  "LossReport",
  "LedgerEvent",
  "TrustSnapshot",
  "TrustEventPayload",
  "Envelope",
];

describe("the generated TypeScript protocol types", () => {
  it("defines every pinned protocol object", () => {
    for (const name of PINNED) expect(schemaNames()).toContain(name);
    expect(PINNED).toHaveLength(14);
  });

  it("types a Bid the way the schema does", () => {
    const bid = makeBid() as unknown as Bid;
    expect(bid.auction_id).toBe("auc-1");
    expect(bid.offer.total_price).toBe(44.1);
    expect(bid.claims[0]?.provenance.source).toBe("owner_statement");
    expect(isValid("Bid", bid)).toBe(true);
  });

  it("types an Intent's hard constraints as filters and preferences as scores", () => {
    const intent = makeIntent() as unknown as Intent;
    expect(intent.hard_constraints[0]?.op).toBe("eq");
    expect(intent.preferences[0]?.direction).toBe("minimize");
    expect(intent.preferences[0]?.weight).toBe(0.6);
    expect(isValid("Intent", intent)).toBe(true);
  });
});

describe("runtime validation from the same schema", () => {
  it("accepts each pinned object's wire payload and refuses an empty one", () => {
    for (const [name, payload] of [
      ["Bid", makeBid()],
      ["Intent", makeIntent()],
      ["Offer", makeOffer()],
      ["Provenance", HOOK_PROVENANCE],
      ["TrustSnapshot", makeTrustSnapshot()],
    ] as const) {
      expect(validationErrors(name, payload)).toEqual([]);
      expect(isValid(name, {})).toBe(false);
    }
  });

  it("closes every property set", () => {
    expect(isValid("Bid", {...makeBid(), smuggled_in: "nope"})).toBe(false);
    expect(isValid("Provenance", {...HOOK_PROVENANCE, smuggled_in: "nope"})).toBe(false);
  });

  it("rejects a hard constraint that carries a weight (R19)", () => {
    const weighted = makeIntent({
      hard_constraints: [{field: "fragrance_free", op: "eq", value: true, weight: 0.9}],
    });
    expect(isValid("Intent", weighted)).toBe(false);
    // Positive control: the same constraint without the weight is fine.
    expect(isValid("Intent", makeIntent())).toBe(true);
  });

  it("closes the provenance source enum", () => {
    expect(isValid("Provenance", {...HOOK_PROVENANCE, source: "made-up-source"})).toBe(false);
    for (const source of [
      "scraped",
      "pixel_feed",
      "owner_statement",
      "envelope_rule",
      "learned_policy",
      "network",
      "seller_asserted",
    ]) {
      expect(isValid("Provenance", {...HOOK_PROVENANCE, source})).toBe(true);
    }
  });

  it("closes the envelope activation enum", () => {
    const envelope = {
      store_id: "store-1",
      version: 3,
      floors: [{product_ref: "prod-1", min_price: 30.0}],
      max_discount_pct: 20.0,
      budget_cap: 500.0,
      pursue_clusters: ["cluster-serum"],
      standing_commitments: [],
      activation: "shadow",
    } satisfies Envelope;
    expect(isValid("Envelope", envelope)).toBe(true);
    expect(isValid("Envelope", {...envelope, activation: "whenever"})).toBe(false);
  });

  it("closes the constraint and preference vocabularies", () => {
    expect(
      isValid("Intent", makeIntent({hard_constraints: [{field: "n", op: "regex", value: "^x"}]})),
    ).toBe(false);
    expect(
      isValid(
        "Intent",
        makeIntent({preferences: [{field: "price", direction: "sideways", weight: 0.5}]}),
      ),
    ).toBe(false);
  });
});

describe("D53 — six trust dimensions inside one trust system", () => {
  it("accepts a six-dimension snapshot", () => {
    const snapshot = makeTrustSnapshot() as unknown as TrustSnapshot;
    expect(Object.keys(snapshot.dims).sort()).toEqual([...TRUST_DIMENSIONS].sort());
    expect(validationErrors("TrustSnapshot", snapshot)).toEqual([]);
  });

  it("refuses a snapshot that omits catalog_claim_accuracy", () => {
    const dims = makeTrustDims();
    delete dims["catalog_claim_accuracy"];
    expect(isValid("TrustSnapshot", makeTrustSnapshot({dims}))).toBe(false);
  });

  it("refuses a snapshot naming a seventh dimension", () => {
    const dims = {
      ...makeTrustDims(),
      invented_dimension: {alpha: 1, beta: 1, decayed_at: "2026-01-01T00:00:00Z"},
    };
    expect(isValid("TrustSnapshot", makeTrustSnapshot({dims}))).toBe(false);
  });
});

describe("the shapes the frozen hosted-bid path depends on", () => {
  it("keeps an Offer valid without variant_ref, currency or an offer id", () => {
    expect(
      isValid("Offer", {
        product_ref: "prod-1",
        unit_price: 49.0,
        discount: {type: "percentage", value: 10.0},
        commitments: [],
        total_price: 44.1,
        expires_at: "2999-01-01T00:00:00Z",
        checkout_url: "https://store-one.example.com/cart/1:1",
      }),
    ).toBe(true);
  });

  it("keeps a LedgerEvent's payload open and its auction/order refs optional", () => {
    const event = {
      event_id: "ev-1",
      ts: "2026-01-01T00:00:00Z",
      kind: "bid_placed",
      payload: {bid_ref: "bid-1", nested: {components: [1, 2, 3]}},
    } satisfies LedgerEvent;
    expect(validationErrors("LedgerEvent", event)).toEqual([]);
  });

  it("keeps the signing envelope off Bid", () => {
    const bidProperties = Object.keys(
      (schemaFor("Bid")["$defs"] as Record<string, {properties: Record<string, unknown>}>)["Bid"]!
        .properties,
    );
    for (const field of ["signer_id", "key_id", "issued_at", "nonce"]) {
      expect(bidProperties).not.toContain(field);
    }
  });

  it("still treats a seller-asserted claim as schema-valid — R8 is a boundary rule", () => {
    const provenance = ASSERTED_PROVENANCE satisfies Provenance;
    const bid = makeBid({claims: [{key: "spf", value: 30, provenance}]});
    expect(isValid("Bid", bid)).toBe(true);
  });
});
