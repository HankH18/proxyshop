/**
 * Wire-shaped payload builders, mirroring `tests/_fixtures_protocol.py` field for field.
 *
 * The two files are deliberately the same fixtures: several tests below compare a TypeScript
 * verdict against a value the Python suite pins, and that comparison only means something if both
 * sides are looking at the same payload.
 */

export const HOOK_PROVENANCE = {
  source: "owner_statement",
  ref: "envelope:store-1:v3#commitment-2",
  observed_at: "2026-01-01T00:00:00Z",
  authority_rank: 1,
} as const;

export const ASSERTED_PROVENANCE = {
  source: "seller_asserted",
  ref: "pitch:p-1#span-4",
  observed_at: "2026-01-01T00:00:00Z",
  authority_rank: 5,
} as const;

export const NOT_EXPIRED = "2999-01-01T00:00:00Z";
export const LONG_EXPIRED = "2000-01-01T00:00:00Z";
export const NOW = "2026-06-01T00:00:00Z";

export type Json = Record<string, unknown>;

export function makeClaim(
  key = "free_returns",
  value: unknown = "30 days",
  provenance: unknown = HOOK_PROVENANCE,
): Json {
  const claim: Json = {key, value};
  if (provenance !== null) claim["provenance"] = structuredClone(provenance);
  return claim;
}

export function makeOffer(overrides: Json = {}): Json {
  return {
    product_ref: "prod-1",
    variant_ref: "44352913",
    unit_price: 49.0,
    currency: "USD",
    discount: {type: "percentage", value: 10.0, provenance: structuredClone(HOOK_PROVENANCE)},
    commitments: [],
    total_price: 44.1,
    expires_at: NOT_EXPIRED,
    checkout_url: "https://store-one.example.com/cart/44352913:1",
    ...overrides,
  };
}

export function makeBid(overrides: Json = {}): Json {
  return {
    auction_id: "auc-1",
    store_id: "store-1",
    offer: makeOffer(),
    claims: [makeClaim()],
    message: null,
    agent_version: "store-agent/1.0.0",
    signature: "sig-deadbeef",
    schema_version: "1.0.0",
    ...overrides,
  };
}

/** A complete external submission: a Bid with the required SigningEnvelope flattened on. */
export function makeSubmission(overrides: Json = {}): Json {
  return {
    auction_id: "auc-0100",
    store_id: "store-external-1",
    offer: makeOffer(),
    claims: [makeClaim("material", "merino wool", ASSERTED_PROVENANCE)],
    message: null,
    agent_version: "store-agent/1.0.0",
    schema_version: "1.0.0",
    signer_id: "store-external-1",
    key_id: "key-2026-01",
    issued_at: "2026-01-01T00:00:00Z",
    nonce: "nonce-ext-0001",
    ...overrides,
  };
}

export function makeSnapshotTable(): Record<string, {store_id: string; score: number; blacklisted: boolean}> {
  return {
    "store-1": {store_id: "store-1", score: 0.6, blacklisted: false},
    "store-bad": {store_id: "store-bad", score: 0.05, blacklisted: true},
  };
}

export function makeTrustDims(): Record<string, Json> {
  const dims: Record<string, Json> = {};
  for (const dim of [
    "price_honored",
    "discount_honored",
    "shipped_on_time",
    "not_returned",
    "feedback_match",
    "catalog_claim_accuracy",
  ]) {
    dims[dim] = {alpha: 2.0, beta: 1.0, decayed_at: "2026-01-01T00:00:00Z"};
  }
  return dims;
}

export function makeTrustSnapshot(overrides: Json = {}): Json {
  return {
    store_id: "store-1",
    score: 0.62,
    dims: makeTrustDims(),
    blacklisted: false,
    ...overrides,
  };
}

export function makeIntent(overrides: Json = {}): Json {
  return {
    intent_id: "int-1",
    cluster_id: "cluster-serum",
    query: "gentle vitamin C serum for sensitive skin",
    category: "skincare",
    hard_constraints: [{field: "fragrance_free", op: "eq", value: true}],
    preferences: [{field: "price", direction: "minimize", weight: 0.6}],
    ship_to: "US-CA",
    currency: "USD",
    budget_band: "40-80",
    created_at: "2026-01-01T00:00:00Z",
    schema_version: "1.0.0",
    ...overrides,
  };
}
