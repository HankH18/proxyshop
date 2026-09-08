# ProxyShop shopper demo — the compose stack

A shopper types what they want, real store agents pitch for the business over HTTP, the
exchange ranks the sealed bids and returns a shortlist carrying both voices, and the shopper
accepts one. All of it in a browser, on one laptop, out of a fresh clone.

**This page is about the compose stack** — thirteen running containers after `make demo-up`,
a real Postgres, a real Neo4j holding ten real storefronts. Thirteen is the ten services §3
tables plus the three datastores their `depends_on` pulls in; this line used to promise
fourteen, counting the generic unconfigured `store-agent` container that `demo-up` names no
service for, on purpose (§3 says why). `starting-slice.md` beside it is about the in-process driver
(`proxyshop_demo`), which starts its own servers on loopback ports, touches no datastore and
takes about two seconds. That page is the beat to run in front of an audience; this one is
the beat that proves the *deployment* works, which is a different claim and a harder one.

## What was wrong, so you can tell this page from the one it replaces

Bringing this stack up used to give you eleven containers that every readiness probe called
`(healthy)`, and no way to serve a shopper at all. Five reasons, all configuration rather than code:

- `EXCHANGE_DEPLOYMENT` and `BUYER_DEPLOYMENT` both defaulted to the **empty string**, and no
  example deployment document existed anywhere in the repository to point them at. With none,
  the exchange knows no sellers, no domains and no bid endpoints, and answers every auction
  `ranked: []`.
- `EXCHANGE_SHOP_ROSTER=graph` — the switch that lets `POST /auctions` find candidates by
  walking the catalogue graph, which is the **first step of this whole product** — was set in
  no compose file, no env file and no runbook. The graph door was shut in every deployment.
- `STORE_AGENT_CONTEXT` is process-wide and names one file, and compose defined one
  `store-agent`. A multi-store auction was not expressible.
- No service served the shopper page at all. The built SPA had exactly one server anywhere in
  the repository, and it was the devstack launcher.
- Neo4j came up empty and nothing ever loaded the recorded catalogues into it.

Every one of those is closed below, and §4 is the measurement that says so.

## What you need

Docker, `uv` and Node. You do **not** need a Shopify account, a Partner account, a
development store, an LLM API key or a network connection past the first build: the
clarifier, the store agents and the extractor all run against deterministic offline doubles
(D19), embeddings are the dependency-free `lexical` provider, and checkout is
`CHECKOUT_MODE=redirect`, which mints a single-use code locally.

Copy the environment file and load it, exactly as `starting-slice.md` says:

```bash
cp .env.example .env
set -a && . ./.env && set +a
```

`PROXYSHOP_WORKER` comes from there and `.env.example` ships `1`. **Do not use worker 0** —
it is reserved for this project's scored measurement run.

## 1. Bring up the datastores and the schema

```bash
make bootstrap
make deps-up
```

`bootstrap` builds the virtualenv and installs the Node toolchain. `deps-up` starts Postgres,
Neo4j and Redis, waits for them to report healthy, creates this worker's database **and now
applies the migrations** — `deps-up` used to stop at an empty database, and the migration
step was a line an operator typed by hand and re-typed after every `make deps-down` (which
destroys the volumes). It is `make db-migrate`, it still exists as a target of its own for a
database that predates a new migration, and it is idempotent.

## 2. Load the ten real storefronts into the graph

```bash
make demo-corpus
```

This replays `fixtures/real-catalogs/` — a point-in-time recording of ten real supplement
storefronts' public `products.json`, 3,093 products, taken under robots.txt — through the
same crawl, upsert and embed path a live crawl takes, and writes it into Neo4j. Nothing here
opens a socket to the public network: the recorded transport reassembles each page from
stored bytes and refuses to serve a page whose digest does not match what the live fetch saw.

It is the slow step: about 44,803 graph writes, several minutes, and **it prints nothing at
all until it finishes**. That silence is real — the loader accumulates its per-store report
and prints it in one block at the end — so do not read a quiet terminal as a hang. Watch the
graph instead, from another shell:

```bash
docker compose exec neo4j cypher-shell -u neo4j -p proxyshop_dev_pw "MATCH (p:Product) RETURN count(p) AS products"
```

The number climbs to 3,093. When the loader finishes it prints a per-store table, a node and
relationship census, `provenance violations 0`, `products missing embedding 0`, and then the
roster its `--probe` asked for — the shops the graph returns for the demo's own query.

Idempotent: run it again and it re-reads nothing that has not changed.

## 3. Start the market

```bash
make demo-up
```

That brings the whole stack up under the `demo` compose profile and waits for it, and that
profile is what turns four real merchants on. The stack it brings up:

| service | port | what it is |
|---|---|---|
| `buyer-web` | 8080 | nginx: the shopper page, and the same-origin proxy in front of the API |
| `buyer-svc` | 8081 | the buyer's own service — clarify, confirm, shortlist, accept |
| `merchant-svc` | 8082 | the merchant app: collector, webhooks, and the console at `/dashboard/` (§6) |
| `exchange` | 8083 | the auction, the ranker, the checkout port |
| `trust` | 8084 | the hash-chained ledger and the trust projection |
| `ingest` | 8085 | entity resolution and claim extraction |
| `store-agent-gaiaherbs` | 8090 | a real storefront's agent, advocating for it alone |
| `store-agent-toniiq` | 8091 | " |
| `store-agent-paradiseherbs` | 8092 | " |
| `store-agent-oregonswildharvest` | 8093 | " |

Four agents, four containers, four context files — not one service scaled to four. A store
agent's identity lives in the file `STORE_AGENT_CONTEXT` names, and replicas of one service
share their environment and volumes byte for byte, so four replicas would be four processes
advocating for the *same* store while the exchange believed it had found four merchants.

The other six storefronts in the corpus get no agent **on purpose**. The exchange still finds
them in the graph and still rosters them; nobody answers their bid door; and the auction
represents each at its catalogue list price and marks the entry `fallback / no_response`.
That is R10, and both halves of it are on screen in one run.

## 4. Prove the graph found the shops — the money path

```bash
make demo-check
```

This is the step the container healthchecks cannot be: it opens a real auction over HTTP with
**no `roster` key in the request body at all**, which is the one request only the catalogue
graph can answer. Every other request carries its own candidate set and never touches Neo4j.

It fails, loudly and by name, on each of the ways this can come back empty — a
`roster_source.source` of `unwired` (the graph roster is not bound), an empty `shops` count
(nothing in Neo4j), no `entries` (the deployment document's `sellers` do not name the graph's
store ids), an empty shortlist.

The request it sends, if you would rather drive it yourself:

```bash
curl -sS -X POST localhost:8083/auctions -H 'content-type: application/json' -d '{"intent":{"intent_id":"demo-probe-1","cluster_id":"cluster-liver-support","query":"milk thistle silymarin liver support extract","hard_constraints":[],"preferences":[],"currency":"USD","created_at":"2026-09-07T23:30:00Z","schema_version":"1.0.0"},"profile":{"pseudonym":"demo-probe-shopper","buckets":{"budget_band":"0-50","first_time":true}}}'
```

**Send a COMPLETE `Intent` and `BuyerProfile`, not the minimum this door accepts.** The
exchange types `intent` as a bare object and forwards it to each store agent verbatim, and the
agent validates it against the published contract — so a four-key intent is a cheerful `201`
here and a `422` at every agent's door. Measured, with `preferences`, `created_at`,
`schema_version` and the whole `profile` left out: seven shops found, and all seven entries
`"fallback": true, "fallback_reason": "store_refused:422"`, with nothing in the auction
response naming the missing fields. `make demo-check` fails on that now rather than reporting
a healthy-looking roster.

Read `roster_source` first. `"source": "neo4j"` with a non-zero `shops` is the proof that the
candidate set was **found**, not supplied. `"unwired"` means the roster was never bound;
`"request"` means something put a roster in the body.

### What a good run looks like

```json
{"source": "neo4j", "shops": 7, "products_considered": 25, "reason": null, "elapsed_ms": 55.426}
```

Seven shops out of ten, with no roster sent. The three the graph leaves out are the two the
corpus calls negative controls — storefronts carrying no liver-support inventory at all —
plus one more that carries none matching. A shortlist that never has to reject anybody
demonstrates nothing, which is why the corpus ships those stores.

`entries` then carries both halves of the market in one response: four hosted stores that
really bid (`"fallback": false`, at their agent's own price) and three with no agent
(`"fallback": true, "fallback_reason": "no_response"`) represented at their catalogue list
price. `shortlist.slots` comes back with four filled slots — `value`, `fit`, `specialist`,
`reliability` — each carrying the store's own `commitments` beside the platform's
`trust_summary` and `provenance_labels`. Those are the two voices.

### What this used to get wrong, kept because the shape is worth recognising

This section used to say a hosted store's bid was scored with `policy_penalties: -0.15`
against it for a product-identity gap rather than a lie — and it was right. `BidRequest` named
no product, so an agent picked one out of its own catalogue by reading the intent text, and the
exchange graded the resulting claims against the product **the auction** rostered. An honest
`list_price` claim about the agent's own product was contradicted by the snapshot entry for a
different one.

Closed as D58: the solicitation carries `product_ref` now, so the agent is told what it is
being asked about. On this stack the honest stores come back with zero contradicted claims and
no penalty, and the store claiming thirty units where the crawl says two is still contradicted
and still penalised.

**Two things about it are worth keeping.** The penalty was one of a matched pair — a false
`-0.15` alongside a `price_value` credited `(78 - 39) / 78` for a discount nobody gave, a ratio
between two different products' prices. They cancelled to within a rounding of each other, so
`rank_score` read the same either way and nothing in the ranking looked wrong. And the obvious
fix — grade whatever the offer names — is exploitable and was built and withdrawn: on the
hosted path `product_ref`, `variant_ref` and `checkout_url` are three independent
store-written strings nothing joins, so changing one field to a crawled sibling turned a
contradiction into a verified claim while still selling the original variant. D58 records the
condition under which it can land.

## 5. The shopper journey in a browser

Open **http://localhost:8080/**.

`buyer-web` is nginx and it is not merely a file server. Every endpoint in the SPA is a bare
relative path (`/buyer/intent/clarify`, `/buyer/auctions/<id>`, `/buyer/shortlist/render`)
fetched with a plain `fetch`, and the buyer service installs no CORS middleware — so the page
and the API have to be one origin. The config proxies `/buyer/` to `buyer-svc:8081` and
serves everything else from the bundle with `try_files ... /index.html`, which is the SPA
fallback the alternative could not have: mounting the bundle on the buyer service uses
`StaticFiles(html=True)`, which 404s every deep link.

Type an intent — *"milk thistle silymarin for liver support, capsules, under $40"* is the one
this market is tuned for. The clarifier asks up to three questions and stops, the confirmation
opens a real auction on the exchange, the store agents price from their own approved
envelopes, and the shortlist comes back with each slot's reason on it.

**Sign-in is required before the exchange is asked anything**, and the page says so where it
stops: *"the next step is the one that leaves it: the exchange solicits real stores... That
pseudonym has to come from the service's vault rather than from this page."* Sign-in is a
single-use link, and a demo stack has nowhere to mail it. So choose the local transport, which
writes the link to the service's own log instead:

```bash
export PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT=console
```

Then read the link out of the service:

```bash
docker compose logs -f buyer-svc | grep 'token='
```

The value is matched literally: `console` works, `Console` and `stdout` and `1` are a boot
failure naming what would have been accepted, and leaving the variable unset is still a 503
with no token printed anywhere. That is the point — a service that is merely MISSING mail
configuration must refuse to log anyone in rather than publish a bearer credential to be
helpful. `console` is a choice you make out loud, the service shouts what it is at first use,
and it is an authentication bypass for anyone who can read the log. Never set it on a
deployment whose logs you do not own.

For a stack that really should mail, set `PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT=smtp` plus
`PROXYSHOP_BUYER_MAGIC_LINK_SMTP_URL` and `PROXYSHOP_BUYER_MAGIC_LINK_SENDER`. Naming `smtp`
with no reachable MTA is a boot failure rather than a silent 503 at the first login attempt.

`PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL` needs no export here: `.env.example` carries
`http://localhost:8080/`, which is `buyer-web` — the origin that serves the PAGE. It said 8081
until this stack landed, written when buyer-svc was the only thing that could conceivably
serve one; a link built on the API's origin 404s before the page loads, holding a credential
that is now spent.

## 6. The merchant console in a browser

The supply side has a page too, and this runbook used to leave it out entirely — §3's table
named `merchant-svc`'s dashboard and no line on this page said where to point a browser.

Open **http://localhost:8082/dashboard/?store=gaiaherbs.com**.

It is the same container as the API: `apps/merchant/Dockerfile.web` builds the vite bundle in
a node stage and copies it into the python image, and one uvicorn serves both, so there is no
second port and nothing to start. The `?store=` parameter just pre-fills the sign-in field.

Sign in with the store id and the admin bearer — `dev-merchant-admin-token`, which is what
`.env.example` ships as `MERCHANT_ADMIN_TOKEN`. The token is held for the tab only. Any of the
four hosted stores works: `gaiaherbs.com`, `toniiq.com`, `paradiseherbs.com`,
`oregonswildharvest.com`.

**Two of the six cards carry real data on this stack, and the rest say why they cannot.** That
is the console working, not the console broken — a card with nothing to show routes through the
same notice component and names the variable an operator would have to set, because a blank
region reads as "no losses" and that is a claim this deployment cannot make. Measured after §4,
against `GET /stores/gaiaherbs.com/dashboard`:

| card | what you see here |
|---|---|
| Onboarding | the real interview, question by question, answerable in the page. The pursue question offers `liver support`: `apps/merchant/compose.yaml` now states `NETWORK_INTENT_CLUSTERS=cluster-liver-support`, the one cluster `deploy/demo/exchange-deployment.json` actually runs auctions in, and `onboarding.script.cluster_options` puts a configured cluster ahead of anything the store's own envelope or loss report names. Unset — which it was in every deployment until that line — the panel reports `missing: ["NETWORK_INTENT_CLUSTERS"]` and a brand-new store, having no envelope and no losses, is offered nothing, so its answer is stored as `pursue_clusters: []` and its agent declines every auction `cluster_not_pursued` |
| Trust | event payloads from the auctions §4 and §5 just ran — `bid_placed` and the rest, straight off the chained ledger, narrowed to this store. The score above them is deliberately blank: the trust service holds no observations for a store nobody has bought from yet, and a neutral low-confidence prior (R12) is said in words rather than drawn as a zero |
| Economic envelope | `absent` — no envelope has ever been recorded for this store id. The store agents bid from `deploy/demo/store-contexts/<host>.json`, which is the agent's own file, not the merchant service's record |
| Kill switch | `shadow`, not bidding, *"no envelope has ever been recorded for this store"*. It is armed by typing the store id, and it is live: it posts the real `POST /stores/{id}/kill` |
| Where you lost | `not_configured`: set `EXCHANGE_URL` and `MERCHANT_REPORT_TOKENS`. The bearer is how the exchange resolves which store is asking, so it is one token per store and the merchant service holds it — the browser never sees it. Nothing in this repository ships one yet |
| Bid activity | `not_configured`: set `STORE_AGENT_URL`. `merchant-svc` is one service and the four agents are four containers, so a single address cannot serve all four; the demo fragment states none rather than pick one |

To watch the interview turn into an envelope, answer the questions and press the button. It
writes version 1 in **shadow**, and the store still bids nothing until you type your name into
the approval form that then appears. That is R6 and R7 end to end in the product, without a
transcript file or a `python -m` on anyone's laptop. Driven against this stack, over the same
routes the buttons call:

```http
PUT /stores/<id>/envelope   {"turns":[…], "completed_at":…}
→ 200   version: 1   activation: shadow

GET /stores/<id>/dashboard
→ step: approval   envelope: ok   may_bid: False   approval offered for v1, sha256:b06332d3d2a8f…

PUT /stores/<id>/envelope   {"activation":"active"}   X-Envelope-Approval: {…}
→ 200   activation: active

GET /stores/<id>/dashboard
→ step: active   activation: active   may_bid: True   "the store's current envelope is active"
```

**Those are HTTP requests the buttons make, not shell commands** — `→` is the response. They
carried a `$` shell prompt in an earlier draft, which is a promise that they can be typed into
a terminal, and everything else prompted in this runbook can be. The read half you *can* type,
with the same bearer the page signs in with:

```bash
curl -sS localhost:8082/stores/gaiaherbs.com/dashboard -H 'authorization: Bearer dev-merchant-admin-token'
```

Two things worth knowing before you try it on a store of your own invention. The store id in
the URL has to equal the shop domain **minus** `.myshopify.com` — answer the first question
`acme.myshopify.com` and the envelope belongs to `acme`, so `/stores/acme.com/envelope`
answers `409 wrong-store`. (The four hosted stores are already consistent: `gaiaherbs.com` is
what `gaiaherbs.com.myshopify.com` reduces to.) And an answer the service cannot parse is
refused rather than filed as blank — `400 unreadable-interview`, naming the sentence — which
is the console's own copy being literally true.

Envelopes live in the merchant service's in-memory store, because `merchant-svc` is deployed
with no `PROXYSHOP_PG_DSN_*` at all. Restarting the container forgets what you approved, which
can only ever *stop* a store bidding, never start one.

## What can go wrong, and what each thing means

| symptom | cause |
|---|---|
| `roster_source.source` is `unwired` | `EXCHANGE_SHOP_ROSTER` is not `graph` in the container, **or** `EXCHANGE_DEPLOYMENT` names no document — the roster is only bound once a deployment document is read, so the two are one switch |
| `shops: 0` and a `reason` mentioning a connection | the exchange cannot reach Neo4j; check `NEO4J_URI` is the service name, not localhost |
| `shops: 0` and a `reason` naming two provider names | vectors were written under one embedding provider and queried under another. Leave `EMBEDDING_PROVIDER` unset in **both** the loader and the exchange; both fall through to `lexical` |
| `shops: 0`, no reason worth reading | Neo4j is empty — `make demo-corpus` |
| the demo worked and then stopped finding shops | **something ran the graph tests in this checkout.** See the warning below |
| shops found, `entries: []` | every rostered store failed the eligibility gate: the deployment document's `sellers` do not name the graph's store ids (which are bare hosts, e.g. `gaiaherbs.com`) |
| every entry is `fallback / no_response` | the agents are not running; the `demo` profile was not used |
| a service is `(healthy)` and answers 503 | the migrations are not applied — `make db-migrate` |
| the mailed sign-in link 404s | the base URL is buyer-svc's origin, not buyer-web's — see §5 |
| `POST /buyer/auth/magic-link` answers 503 | no mail transport configured; the service says so rather than promising a mail nothing will send |
| everything authenticates badly on a stack that used to work | you exported `PROXYSHOP_ROLE_PASSWORD` **after** the pgdata volume existed. That hook runs once; `make deps-down` then `make deps-up` |

### The one that will catch you out: the test suite empties this graph

**Do not run the Python test suite against the same Neo4j the demo is using.** Every graph
test resets the store — `MATCH (n) DETACH DELETE n` — because Neo4j Community has one
database and the tests need a clean one. It does not ask whether the demo is loaded.

This was measured mid-run on this page: the corpus loaded, the probe passed with seven shops
found, a test suite ran in the same checkout, and the next probe answered

```text
roster_source: source='neo4j' shops=0 products_considered=0 reason="no product in this
exchange's catalogue graph both matches this intent and satisfies its hard constraints"
```

with the graph holding four products and three stores — a graph test's own fixture, not the
corpus. `make demo-corpus` puts it back. The probe catching it rather than reporting a
healthy-looking roster is the point of running it after every deploy.

**Locking does not save you, and it is worth saying why so nobody goes looking for a lock to
take.** There is a machine-global flock that serialises graph tests against each other, and
the loader could take it — but the reset runs at the START of every graph test, so a suite
that begins after a perfectly-locked load has finished still empties the graph the moment it
acquires the lock in its turn. A loaded corpus and a running test suite cannot share one
Neo4j; Community edition has one database and the tests need a clean one. Give the demo a
quiet checkout, or point it at its own Neo4j with `NEO4J_URI`.

To take it all down, volumes intact:

```bash
make demo-down
```

## Where the configuration comes from

Nothing in `deploy/demo/` is hand-written. One generator writes all seven documents from the
recorded corpus, so every store id, product reference and price traces to a real storefront's
own published bytes:

```bash
./.venv/bin/python scripts/build_demo_deployment.py --check
```

That reports drift instead of writing; drop `--check` to regenerate. What it emits:

- `deploy/demo/exchange-deployment.json` — ten eligible sellers with their real registered
  domains, bid endpoints for the four hosted agents, the stated trust snapshot, the one named
  intent cluster those agents' envelopes pursue, and the catalogue snapshots the claim
  verifier grades against.
- `deploy/demo/buyer-deployment.json` and `deploy/demo/buyer-roster.json` — where the
  exchange is, and the candidate set the buyer opens an auction with. Separate files because
  the buyer document is capped at 64 KiB and the roster at 512 KiB.
- `deploy/demo/store-contexts/<host>.json` — one per hosted agent: that store's whole priced
  catalogue, its approved envelope, and its live state.

**The intent cluster is the piece that has to line up in three places**, and it is worth
knowing because getting it wrong looks like a merchant declining rather than a wiring hole.
An envelope that pursues no cluster pursues nothing — fail-closed by design — so an agent
whose `pursue_clusters` do not name the cluster of the auction it is asked about answers
`cluster_not_pursued`. The buyer's clarifier does *not* mint that name: it hashes the query
into something like `cl-69eb1ce2b0244041`. The exchange re-assigns it from the
`intent_clusters` vocabulary in its own deployment document, and the agents' envelopes name
that same id. Verified on this stack: a clarified intent carrying `cl-69eb1ce2b0244041`
comes out of `assign_cluster` as `cluster-liver-support`, which is what all four envelopes
pursue.

Three numbers in there are *stated by a person* rather than read from a storefront, because
no storefront publishes them, and they are the same three the composition root says must be
stated: the approved envelope's discount depth, the trust score, and the intent-cluster
vocabulary. Everything else is the corpus.

The generated documents are tracked, so a clone runs the demo without running the generator.
Re-run it after the corpus moves, and `--check` reports drift instead of writing.

## What this page does not prove

- **Reconciliation and the trust projection do not close from what this stack serves.** This
  bullet used to blame a missing producer — "`checkout_pixel` has no producer on any served
  path" — and that is no longer true: `POST /pixel/collect` on `merchant-svc` writes a real
  `checkout_pixel` row to the chained ledger. What this stack does not do is *fire* the beacon.
  `CHECKOUT_MODE` is `redirect`, so §5's acceptance mints a simulated permalink rather than
  taking a shopper through a Shopify checkout, and nothing here posts a beacon or an
  `orders/paid` webhook. So `reconcile` still folds no verdict over the chain the exchange
  writes. `starting-slice.md` §3.6/§3.7 measures that gap and §4's scripted proof is where the
  whole loop is exercised.
- **The claim grading here is as good as the shipped catalogue snapshots.** They are trimmed
  to sixty products per hosted store; a `product_ref` the graph rosters from outside that
  window grades `ambiguous`, which R19 will not let satisfy a hard constraint.
- **The shopper journey in §5 supplies its own roster.** The buyer service refuses to open an
  auction with no candidate set — that is its own fail-closed posture — so the browser path
  sends `buyer-roster.json` and the exchange uses it rather than the graph. §4 is where the
  graph is proved, and it is the same exchange, the same request shape and the same auction
  machine, minus one key in the body.
- **A green container list is not a working demo.** Every container here can report
  healthy while the market is dead, which is why `make demo-check` drives a real auction
  instead of asking the containers how they feel.
