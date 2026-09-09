# Driving the stack by hand

Every request shape below was driven against a running stack before being written here. It exists
because the same few minutes get spent rediscovering them on every investigation, and because
each wrong guess fails in a way that looks like a product defect rather than a malformed body —
which has already sent one investigation chasing a retrieval bug that was a missing field.

`docs/deploy.md` is how to bring the stack up. `docs/deploy-droplet.md` is the hosted box. This is
how to ask either of them a question.

---

## Ports, and which service answers what

| port | service | what it is |
|---|---|---|
| 8080 | `buyer-web` | the SPA (nginx; also proxies `/buyer/` onward) |
| 8081 | `buyer-svc` | the buyer API — clarify, confirm, feedback, livecheck |
| 8082 | `merchant-svc` | merchant console at `/dashboard/` (bare `/` is a 404 by design) |
| 8083 | `exchange` | auctions, shortlists, accept |
| 8084 | `trust` | snapshot, events, reconcile |

**The single most expensive mistake is asking the wrong one.** `POST :8083/auctions` with no
roster in the body opens a graph-rostered auction; `POST :8081/buyer/intent/confirm` opens one
through the buyer service, which supplies its own roster. Both are legitimate and they answer
differently — see "Two roster paths" below. An investigation that meant one and drove the other
reads its result as a defect.

---

## The shopper path, end to end

### 1. Clarify — `POST :8081/buyer/intent/clarify`

`turns` is a list of **plain strings**, not objects. An object gives
`Input should be a valid string`; omitting the key gives `Field required`.

```sh
curl -s -X POST http://localhost:8081/buyer/intent/clarify \
  -H 'content-type: application/json' \
  -d '{"turns":["a sleeping bag"]}' | python3 -m json.tool
```

Answers `{questions, intent, unresolved, confirmed}`. Later turns are answers to the questions
asked so far, in order:

```sh
-d '{"turns":["I want a cherry wood table under $200",
              "It must be cherry wood, and at least 48 inches wide"]}'
```

### 2. Confirm — `POST :8081/buyer/intent/confirm`

Takes the `intent` object clarify returned, verbatim, plus `confirmed`. **`confirmed` is
`StrictBool`** — `"yes"`, `"true"`, `1` all answer 422, deliberately, because lax coercion once
turned `{"confirmed": "yes"}` into a live auction.

```sh
I=$(curl -s -X POST http://localhost:8081/buyer/intent/clarify \
      -H 'content-type: application/json' -d '{"turns":["a sleeping bag"]}' \
    | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["intent"]))')

curl -s -X POST http://localhost:8081/buyer/intent/confirm \
  -H 'content-type: application/json' -d "{\"intent\": $I, \"confirmed\": true}"
```

Answers `{auction_id, intent_id, created_at}` and **nothing else** — no slots, no market. The
shortlist is a separate read against the exchange. Expecting slots here is a common wrong turn.

### 3. Shortlist — `GET :8083/auctions/{auction_id}/shortlist`

```sh
curl -s "http://localhost:8083/auctions/$A/shortlist" | python3 -c '
import json,sys
for s in json.load(sys.stdin).get("slots", []):
    p = s.get("product") or {}
    print(s.get("store_domain"), (p.get("identity") or {}).get("title"),
          p.get("variant_ref"), (s.get("price") or {}).get("unit_price"), s.get("fallback"))
'
```

Field names that are easy to get wrong: the store is **`store_domain`**, not `store_id`; the price
is **`price.unit_price`**, not `offer.unit_price`; the variant is **`product.variant_ref`**.

### 4. Accept — `POST :8083/auctions/{id}/accept`

```sh
B=$(curl -s "http://localhost:8083/auctions/$A/shortlist" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["slots"][0]["bid_ref"])')
curl -s -X POST "http://localhost:8083/auctions/$A/accept" \
  -H 'content-type: application/json' -d "{\"bid_ref\": \"$B\"}"
```

Answers `permalink_url` — the cart link. A real one names a 14-digit storefront variant id;
`…/cart/1:1` means the served slot carried no `variant_ref`.

---

## Two roster paths, and why the same query answers differently

**The graph path.** `POST :8083/auctions` with **no** `roster` in the body. The exchange walks
Neo4j (`EXCHANGE_SHOP_ROSTER` defaults to `graph`) and may roster any of the nineteen stores on any
of their products. `scripts/demo_check.sh` drives this deliberately, and hard-fails unless
`roster_source.source == "neo4j"`.

**The stated path.** Anything through `:8081/buyer/intent/confirm`. The buyer service sends the
roster named by `BUYER_ROSTER` (`deploy/demo/buyer-roster.json`) — fifteen rows, **one pinned
product per store**. `repoint_organic_products` may move a row onto a product retrieval vouched
for, but a store still gets one shot.

Consequence, measured: `"a walnut coffee table for the lounge"` returns **2 slots on the graph
path and 0 on the stated path**, because no store's pinned product is a coffee table. Neither is
broken. Say which path you drove when you report a slot count.

```sh
# graph path, with the probe that also checks the plumbing
DEMO_QUERY="a sleeping bag" bash scripts/demo_check.sh
```

---

## Reading the answer without misreading it

`entries` lists **every rostered store**, whether or not it reached the shortlist — it is not the
shortlist. `excluded` carries the refusals, as a separate list keyed by store. A priced row in
`entries` that never reached a shopper appears in both, and joining them by store id is the step
that gets skipped: it is what makes an over-ceiling row look like an R19 violation when the ceiling
in fact excluded it.

`market` counts the fan-out: `solicited`, `sponsored`, `list_price`, `no_endpoint`, `denied`,
`shortlisted`, `all_fallback`, `nothing_shown`.

`fallback_reason` is `family:detail`. **`tier_0_no_agent` is a family meaning "no bidding agent for
the exchange to ask"** — it is not a claim about the row's tier, and a `tier 1` row can legitimately
carry it when this deployment holds no endpoint for that store. The detail says whose decision it
was: `no_bid_endpoint` is the deployment's, a bare family is the merchant's.

---

## Test invocation

`PROXYSHOP_WORKER` is mandatory (D38). **An index ≥ 16 trips the Redis guard** — use a low one.

```sh
PROXYSHOP_WORKER=9 ./.venv/bin/python -m pytest <paths> -q -m "not docker and not graph"
npx vitest run apps/buyer          # frontend
```

**Never run `docker`- or `graph`-marked tests against a stack you care about**: that fixture calls
`reset_graph()` and will wipe the loaded corpus, which is a twenty-minute reload.

Give concurrent lanes **distinct worker indices**. Sharing one produces phantom failures that look
like real defects — measured, eleven of them in one session.
