/**
 * The pinned cross-domain routes and their checked-in examples, for the TypeScript consumers.
 *
 * The same documents the Python side reads, validated the same way: an example is checked against
 * the schema its operation declares, with `$ref`s into `protocol.schema.json` resolved LOCALLY.
 * A consumer written in TypeScript and one written in Python therefore agree about what each
 * endpoint accepts, which is the only reason to have a shared contracts package at all.
 */
// The draft 2020-12 build — see `schemas.ts` for why the default `ajv` entry point is wrong here.
import Ajv2020, {type ValidateFunction} from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

import buyerDoc from "../../openapi/buyer.openapi.json" with {type: "json"};
import exchangeDoc from "../../openapi/exchange.openapi.json" with {type: "json"};
import ingestDoc from "../../openapi/ingest.openapi.json" with {type: "json"};
import merchantDoc from "../../openapi/merchant.openapi.json" with {type: "json"};
import storeAgentDoc from "../../openapi/store-agent.openapi.json" with {type: "json"};
import trustDoc from "../../openapi/trust.openapi.json" with {type: "json"};
import {PROTOCOL_SCHEMA_ID, protocolSchema} from "./schemas.js";

export interface Route {
  domain: string;
  method: string;
  path: string;
}

/**
 * Every route this platform publishes a contract for, and it must stay identical to the Python
 * tuple in `../openapi.py` — both are compared against the same documents in both directions.
 *
 * The first block is DESIGN §Interfaces "Service APIs", the cross-domain checklist. The second
 * is the sixteen operations four services were ANSWERING while no document declared them
 * (T-266, T-312, T-317): the exchange's auction read, the trust ledger's five reads and its
 * claim-verification producer, ingest's `er` and `extraction` routers, and the merchant's OAuth
 * install pair plus its administrative shop list. "It does not cross a domain" was never a
 * reason for a reachable route to escape contract review.
 */
export const PINNED_ROUTES: readonly Route[] = [
  // DESIGN §Interfaces, "Service APIs" — the cross-domain contract.
  {domain: "exchange", method: "post", path: "/auctions"},
  {domain: "exchange", method: "get", path: "/auctions/{auction_id}/shortlist"},
  {domain: "exchange", method: "post", path: "/auctions/{auction_id}/accept"},
  {domain: "exchange", method: "post", path: "/v1/auctions/{auction_id}/bids"},
  {domain: "exchange", method: "post", path: "/internal/outcomes"},
  {domain: "store-agent", method: "post", path: "/v1/bid-requests"},
  {domain: "merchant", method: "post", path: "/codes"},
  {domain: "merchant", method: "post", path: "/webhooks/shopify/{topic}"},
  {domain: "merchant", method: "post", path: "/pixel/collect"},
  {domain: "merchant", method: "get", path: "/stores/{store_id}/envelope"},
  {domain: "merchant", method: "put", path: "/stores/{store_id}/envelope"},
  {domain: "merchant", method: "post", path: "/stores/{store_id}/kill"},
  // The kill switch's other half, declared in the change that serves it: the stop is one door
  // and the un-stop is another, so no approval path lifts a kill and no edit resumes a store as
  // a side effect. It reaches `shadow` and cannot reach `active`. The Python twin carries the
  // full reasoning.
  {domain: "merchant", method: "post", path: "/stores/{store_id}/revive"},
  {domain: "trust", method: "post", path: "/events"},
  {domain: "trust", method: "get", path: "/snapshot"},
  // T-312 UNPINNED two trust routes DESIGN §Interfaces still lists. Each was published, served
  // by nothing, and unservable AS PUBLISHED: `GET /stores/{store_id}/trust` declares no identity
  // parameter, so serving it as written hands any anonymous caller any store's full
  // per-dimension posture; `POST /feedback/{order_ref}` declares no routing evidence, so serving
  // it as written takes R14 feedback from a buyer the network never routed. The Python twin
  // carries the full reasoning; re-pin either in the change that serves it.
  {domain: "ingest", method: "post", path: "/refresh/{store_id}"},
  // Served surfaces that were reachable and undeclared until T-266 / T-312 / T-317.
  {domain: "exchange", method: "get", path: "/auctions/{auction_id}"},
  {domain: "trust", method: "get", path: "/events"},
  {domain: "trust", method: "get", path: "/events/head"},
  {domain: "trust", method: "get", path: "/events/verify"},
  {domain: "trust", method: "get", path: "/events/replay"},
  {domain: "trust", method: "get", path: "/events/{event_id}"},
  {domain: "trust", method: "post", path: "/claims/verifications"},
  // R4 + R12's join: where a completed purchase becomes a trust update.
  {domain: "trust", method: "get", path: "/reconcile"},
  {domain: "trust", method: "post", path: "/reconcile"},
  {domain: "ingest", method: "get", path: "/er/config"},
  {domain: "ingest", method: "post", path: "/er/match"},
  {domain: "ingest", method: "post", path: "/er/resolve"},
  {domain: "ingest", method: "get", path: "/extraction/config"},
  {domain: "ingest", method: "post", path: "/extraction/policy-pages"},
  {domain: "ingest", method: "post", path: "/extraction/stores/{store_id}"},
  {domain: "ingest", method: "get", path: "/schedule"},
  {domain: "ingest", method: "post", path: "/schedule/tick"},
  {domain: "merchant", method: "get", path: "/install"},
  {domain: "merchant", method: "get", path: "/install/callback"},
  {domain: "merchant", method: "get", path: "/install/shops"},
  // R9's dashboard, declared in the change that serves it. The store is a path parameter on
  // each, and the caller is `apps/merchant/app/dashboard` — the SPA the merchant service now
  // serves at `/dashboard` from its own vite build. The bundle itself is a `Mount`, which
  // declares no operation and owes no contract; only these two JSON reads are surface.
  {domain: "merchant", method: "get", path: "/stores/{store_id}/dashboard"},
  {domain: "merchant", method: "post", path: "/stores/{store_id}/bids/solicit"},
  // R13's receiving end: the door trust pushes one store's own trust delta through.
  {domain: "store-agent", method: "post", path: "/v1/trust-events"},
  // R9's merchant-facing report door. Deliberately in neither block above: DESIGN §Interfaces
  // does not pin it, and it is not one of the surfaces that were "reachable and undeclared
  // until T-266 / T-312 / T-317" — that heading dates a finding about routes four services
  // were already answering, and this route did not exist then. It is declared under the rule
  // the Python twin's docstring states: a route the service ANSWERS is declared here. The
  // store is resolved from the bearer token, so the published operation carries an
  // `Authorization` header and no `store_id` parameter of any kind.
  {domain: "exchange", method: "get", path: "/reports/losses"},
  // The BUYER service, which until this block published no document at all — not a partial one:
  // `packages/contracts/openapi/` held five files and `buyer.openapi.json` was not among them,
  // while `buyer_svc.main` mounted six routers and answered these fourteen operations. Every
  // gap the three sweeps above found was a service publishing LESS than it served; this was a
  // whole service outside contract review, including its unauthenticated login door and
  // `POST /buyer/shortlist/accept`, the frame that decides where a shopper's browser is sent.
  // `apps/buyer/svc/tests/test_openapi_contract.py` is the served-vs-published gate.
  {domain: "buyer", method: "post", path: "/buyer/shortlist/render"},
  {domain: "buyer", method: "post", path: "/buyer/shortlist/accept"},
  {domain: "buyer", method: "get", path: "/buyer/auctions/{auction_id}"},
  {domain: "buyer", method: "post", path: "/buyer/auth/magic-link"},
  {domain: "buyer", method: "post", path: "/buyer/auth/session"},
  {domain: "buyer", method: "get", path: "/buyer/auth/session"},
  {domain: "buyer", method: "delete", path: "/buyer/auth/session"},
  // The sign-in OFFER, added after the fourteen above: one boolean saying whether this
  // deployment holds a real mail transport and can therefore finish a magic-link login.
  // `apps/buyer/app/journey/Journey.tsx` reads it and renders the sign-in form only when it is
  // true, so a deployment that can send no mail never promises a link nothing would send. It
  // discloses nothing `POST /buyer/auth/magic-link` does not by answering 202 rather than 503.
  // The Python twin carries the full reasoning.
  {domain: "buyer", method: "get", path: "/buyer/auth/sign-in"},
  {domain: "buyer", method: "get", path: "/buyer/profile"},
  // The store's read of the buyer window, not the shopper's: a store-scoped bearer, and a live
  // buyer session is refused. It reached `openapi.py` and `buyer.openapi.json` with T-142 and
  // not this mirror, so `are the only routes declared` had been red in TypeScript ever since —
  // the two lists are compared against the same documents in both directions, and a route in
  // one and not the other is exactly what that pair of tests exists to catch.
  {domain: "buyer", method: "get", path: "/buyer/store-window"},
  {domain: "buyer", method: "post", path: "/buyer/feedback/prompt"},
  {domain: "buyer", method: "post", path: "/buyer/feedback"},
  // R14's post-purchase prompt needs a real order to be about, and it is FETCHED from trust's
  // reconciled record rather than minted at accept. THREE places, not two: this mirror is the
  // one a buyer route has been added without twice now, and both times `vitest
  // packages/contracts` went red while the Python tuple and the JSON document agreed.
  {domain: "buyer", method: "post", path: "/buyer/feedback/order"},
  {domain: "buyer", method: "post", path: "/buyer/intent/clarify"},
  {domain: "buyer", method: "post", path: "/buyer/intent/confirm"},
  {domain: "buyer", method: "post", path: "/buyer/livecheck/run"},
  {domain: "buyer", method: "get", path: "/buyer/livecheck/{auction_id}"},
  // The shopper's follow-up questions about a shortlist they are already looking at, from the
  // ask-about-these-options stream (060225e). It is the one buyer door whose whole subject is
  // what the PLATFORM may say in its own voice, so it is the last one that should escape
  // contract review. Reads only: it fetches the exchange's live shortlist for one auction and
  // answers from it, naming neither of the exchange's two writes.
  //
  // THIRD time, not the second the note above records. The Python tuple and
  // `buyer.openapi.json` both carried it and this mirror did not, so `are the only routes
  // declared` was red in TypeScript from 060225e until now. Detection was never the problem —
  // the pair of tests names the route and the direction on the first run. Both times it sat
  // red, it was because every lane that saw it correctly judged it outside its own file scope.
  {domain: "buyer", method: "post", path: "/buyer/chat/ask"},
];

const HTTP_METHODS = ["get", "put", "post", "delete", "patch", "head", "options", "trace"] as const;

type Doc = Record<string, unknown>;

/** Every OpenAPI document in this package, keyed by its `x-domain`. */
export const documents: Readonly<Record<string, Doc>> = Object.freeze({
  buyer: buyerDoc as unknown as Doc,
  exchange: exchangeDoc as unknown as Doc,
  "store-agent": storeAgentDoc as unknown as Doc,
  merchant: merchantDoc as unknown as Doc,
  trust: trustDoc as unknown as Doc,
  ingest: ingestDoc as unknown as Doc,
});

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

/** Every route actually declared across the checked-in documents, sorted. */
export function routes(): Route[] {
  const found: Route[] = [];
  for (const [domain, document] of Object.entries(documents)) {
    const paths = asRecord(document["paths"]) ?? {};
    for (const [path, operations] of Object.entries(paths)) {
      const record = asRecord(operations) ?? {};
      for (const method of HTTP_METHODS) {
        if (method in record) found.push({domain, method, path});
      }
    }
  }
  return found.sort((a, b) =>
    `${a.domain}|${a.method}|${a.path}`.localeCompare(`${b.domain}|${b.method}|${b.path}`),
  );
}

export interface OpenApiExample extends Route {
  where: string;
  schema: Record<string, unknown>;
  value: unknown;
}

/** Every `application/json` body that carries both a schema and an example. */
export function examples(): OpenApiExample[] {
  const out: OpenApiExample[] = [];
  for (const [domain, document] of Object.entries(documents)) {
    const paths = asRecord(document["paths"]) ?? {};
    for (const [path, operations] of Object.entries(paths)) {
      const byMethod = asRecord(operations) ?? {};
      for (const method of HTTP_METHODS) {
        const operation = asRecord(byMethod[method]);
        if (operation === undefined) continue;

        const requestContent = asRecord(
          asRecord(asRecord(operation["requestBody"])?.["content"])?.["application/json"],
        );
        if (requestContent?.["schema"] !== undefined && "example" in requestContent) {
          out.push({
            domain,
            method,
            path,
            where: "requestBody",
            schema: asRecord(requestContent["schema"])!,
            value: requestContent["example"],
          });
        }

        const responses = asRecord(operation["responses"]) ?? {};
        for (const [status, response] of Object.entries(responses)) {
          const content = asRecord(
            asRecord(asRecord(response)?.["content"])?.["application/json"],
          );
          if (content?.["schema"] !== undefined && "example" in content) {
            out.push({
              domain,
              method,
              path,
              where: `responses.${status}`,
              schema: asRecord(content["schema"])!,
              value: content["example"],
            });
          }
        }
      }
    }
  }
  return out;
}

// One Ajv instance holding the protocol bundle under its `$id`, so every `$ref` into it resolves
// against the bytes in this repo and never over the network (D3).
const ajv = addFormats(new Ajv2020({allErrors: true, strict: false, allowUnionTypes: true}));
ajv.addSchema(protocolSchema as unknown as object, PROTOCOL_SCHEMA_ID);

const compiled = new WeakMap<object, ValidateFunction>();

/** Every reason one checked-in example does not satisfy its declared schema. */
export function exampleErrors(example: OpenApiExample): string[] {
  let validate = compiled.get(example.schema);
  if (validate === undefined) {
    validate = ajv.compile(example.schema);
    compiled.set(example.schema, validate);
  }
  if (validate(example.value)) return [];
  return (validate.errors ?? []).map((error) => {
    const where =
      error.instancePath === ""
        ? "<root>"
        : error.instancePath.replace(/^\//, "").replaceAll("/", ".");
    return `${where}: ${error.message ?? "is invalid"}`;
  });
}
