/**
 * Consumer contract tests, TypeScript side.
 *
 * The same three rules the Python suite checks — every pinned route declared, every JSON body
 * carrying an example, every example validating against its declared schema — run here too,
 * because a contract only one language checks is a contract only one language keeps.
 */
import {describe, expect, it} from "vitest";

import {PINNED_ROUTES, documents, exampleErrors, examples, routes} from "../src/ts/openapi.js";

const ALL_EXAMPLES = examples();
const key = (route: {domain: string; method: string; path: string}) =>
  `${route.domain}|${route.method}|${route.path}`;

describe("the pinned cross-domain routes", () => {
  it("are all declared", () => {
    const declared = new Set(routes().map(key));
    const missing = PINNED_ROUTES.filter((r) => !declared.has(key(r))).map(key);
    expect(missing).toEqual([]);
  });

  it("are the only routes declared", () => {
    // An undocumented extra route is a cross-domain surface nobody agreed to.
    const pinned = new Set(PINNED_ROUTES.map(key));
    const extra = routes()
      .map(key)
      .filter((k) => !pinned.has(k));
    expect(extra).toEqual([]);
  });

  it("cover all five domains", () => {
    expect(Object.keys(documents).sort()).toEqual([
      "exchange",
      "ingest",
      "merchant",
      "store-agent",
      "trust",
    ]);
  });
});

describe("the checked-in examples", () => {
  it("exist in useful numbers", () => {
    // Guards the assertion below against passing vacuously on an empty document set.
    expect(ALL_EXAMPLES.length).toBeGreaterThanOrEqual(25);
  });

  it.each(ALL_EXAMPLES.map((e) => [`${e.domain} ${e.method} ${e.path} ${e.where}`, e] as const))(
    "%s validates against its declared schema",
    (_label, example) => {
      expect(exampleErrors(example)).toEqual([]);
    },
  );

  it("accompany every JSON body that declares a schema", () => {
    const missing: string[] = [];
    for (const [domain, document] of Object.entries(documents)) {
      for (const [path, operations] of Object.entries(dig(document, "paths"))) {
        for (const [method, operation] of Object.entries(asObject(operations))) {
          const bodies: [string, Json][] = [];
          const request = dig(dig(asObject(operation), "requestBody"), "content")[
            "application/json"
          ];
          if (request !== undefined) bodies.push(["requestBody", asObject(request)]);
          for (const [status, response] of Object.entries(dig(asObject(operation), "responses"))) {
            const content = dig(asObject(response), "content")["application/json"];
            if (content !== undefined) bodies.push([`responses.${status}`, asObject(content)]);
          }
          for (const [where, content] of bodies) {
            if ("schema" in content && !("example" in content)) {
              missing.push(`${domain} ${method.toUpperCase()} ${path} ${where}`);
            }
          }
        }
      }
    }
    expect(missing).toEqual([]);
  });
});

describe("the two doors are documented as different doors", () => {
  it("requires the signing envelope on the external submission route only", () => {
    const content = jsonBody("exchange", "/v1/auctions/{auction_id}/bids", "post", "requestBody");
    expect(String(asObject(content["schema"])["$ref"])).toMatch(/#\/\$defs\/SignedBidSubmission$/);
    const example = Object.keys(asObject(content["example"]));
    for (const field of ["signer_id", "key_id", "issued_at", "nonce", "schema_version"]) {
      expect(example).toContain(field);
    }
  });

  it("carries no envelope on the solicitation route", () => {
    // A hosted Tier-1 agent never crosses the external boundary and holds no key to sign with.
    const content = jsonBody("store-agent", "/v1/bid-requests", "post", "responses.200");
    const example = Object.keys(asObject(content["example"]));
    for (const field of ["signer_id", "key_id", "issued_at", "nonce"]) {
      expect(example).not.toContain(field);
    }
  });
});

// --- small readers, so the tests above assert rather than wrestle with `unknown` ---------
type Json = Record<string, unknown>;

function asObject(value: unknown): Json {
  return typeof value === "object" && value !== null ? (value as Json) : {};
}

function dig(value: unknown, key: string): Json {
  return asObject(asObject(value)[key]);
}

function jsonBody(domain: string, path: string, method: string, where: string): Json {
  const operation = dig(dig(documents[domain], "paths"), path)[method];
  const holder =
    where === "requestBody"
      ? dig(asObject(operation), "requestBody")
      : dig(dig(asObject(operation), "responses"), where.split(".")[1] ?? "");
  return dig(holder, "content")["application/json"] as Json;
}

// ----------------------------------------------------------------------------------------------
// Negative controls.
//
// Everything above asserts `exampleErrors(...) === []`. On its own that is unfalsifiable: an
// `exampleErrors` that returned `[]` unconditionally would satisfy all ~29 cases and this file
// would have no teeth at all. These prove the checker can say no.
// ----------------------------------------------------------------------------------------------
describe("the example checker can reject", () => {
  const bidExample = examples().find(
    (e) => e.domain === "store-agent" && e.where === "responses.200",
  )!;
  // Was `trust GET /stores/{store_id}/trust`, whose schema was a bare `$ref` to TrustSnapshot.
  // T-312 unpinned that operation — published, served by nothing, and declaring no identity
  // parameter — so this anchors on `GET /snapshot`, which reaches the same definition through
  // `additionalProperties: {$ref}`. That is stricter: a resolver that followed a root `$ref`
  // and silently dropped a nested one would have passed the old control and fails this one.
  const snapshotExample = examples().find(
    (e) => e.domain === "trust" && e.path === "/snapshot",
  )!;

  it("rejects a Bid whose store_id is the wrong type", () => {
    const value = {...(bidExample.value as Record<string, unknown>), store_id: 12345};
    const problems = exampleErrors({...bidExample, value});
    expect(problems.length).toBeGreaterThan(0);
    expect(problems.join(" ")).toContain("store_id");
  });

  it("rejects a smuggled field", () => {
    const value = {...(bidExample.value as Record<string, unknown>), network_fee: 0};
    expect(exampleErrors({...bidExample, value}).length).toBeGreaterThan(0);
  });

  it("rejects an empty body", () => {
    expect(exampleErrors({...bidExample, value: {}}).length).toBeGreaterThan(0);
  });

  it("resolves protocol $refs rather than silently ignoring them", () => {
    // If the `$ref` did not resolve, every example would validate against `true` and this whole
    // file would be green for the wrong reason.
    const byStore = snapshotExample.value as Record<string, Record<string, unknown>>;
    const storeId = Object.keys(byStore)[0]!;
    const snapshot = byStore[storeId]!;
    const fiveDims = {...(snapshot["dims"] as Record<string, unknown>)};
    delete fiveDims["catalog_claim_accuracy"];
    const problems = exampleErrors({
      ...snapshotExample,
      value: {...byStore, [storeId]: {...snapshot, dims: fiveDims}},
    });
    expect(problems.length).toBeGreaterThan(0);
  });
});
