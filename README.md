# ProxyShop

ProxyShop is a **buyer-side shopping-agent network**. A shopper describes what they want in
plain language. A graph search over the shops the platform has crawled finds the ones that
plausibly match. Each of those shops is represented in the result — the ones that have joined
the network by their own agent, the ones that have not by the platform's. The shopper picks
one and is handed to that seller's own checkout with a single-use discount code.

The thing to understand before any command is the shape of that market, because it is not the
obvious one.

## Organic and sponsored, taken further

A search engine crawls what is there and renders the non-sponsored result in its own voice. An
advertiser pays for a little more control over the sponsored one. **ProxyShop is that split,
taken much further.**

- A **scraped** shop is the *organic* result. The platform writes its pitch, from the
  platform's own crawl. The shop did nothing, knows nothing about it, and still appears.
- An **in-network** shop is the *sponsored* result. What it buys is a **dedicated advocate
  agent** — one process per shop, holding that shop's own approved economic envelope, which
  writes a pitch for *this* shopper, chooses which commitments to stand behind, and may
  condition a discount on the shopper's coarsened profile.

These are two agents with two different objective functions, and they are not two quality tiers
of one pipeline. The buyer-side agent wants the shopper to buy *a* product, so it pitches
**every** candidate as well as it honestly can, scraped shops included. The shop-side agent
wants the shopper to buy *that shop's* product.

**What a shop buys is therefore not visibility and not a better score.** Visibility is organic
and earned by matching. It buys the right to make its case in its own voice, and the loop that
improves that case. The ranking formula has no fee term and no tier term, and the exchange has
no code path and no database grant that can read a store's envelope at all — enforced by a
Postgres grant test and an import-lint rule that both fail the build.

**And it is emphatically not a price auction.** An early framing as a first-price sealed
auction was rejected, on a mechanical argument: every store has a maximum discount it will
authorise, and a market whose allocation is dominated by discount guarantees every store
reaches that maximum and then differentiates on nothing. That trains the network into a
commodity market and destroys the margin it exists to broker. Discount is one saturating term
of five in the published formula, and clearing the shopper's price band is a qualifier rather
than a differentiator.

## The asymmetry that makes it safe

Two writers produce shopper-facing copy, and they are held to opposite standards of proof.

**A seller's purchased message carries the seller's motive, so it is checked adversarially
against the platform's own crawl.** Pitch text is decomposed into atomic typed claims, and each
one is graded `verified`, `contradicted`, `unsupported` or `ambiguous`
(`packages/contracts/src/generated/protocol.py:100`). Only `verified` can satisfy a hard
constraint — `unsupported` and `ambiguous` are unknowns, and an unknown is never treated as a
yes. The evidence that decides a grade may only come from a source the *platform* authored:
`PLATFORM_OBSERVED_SOURCE_CLASSES` is `{"scraped", "pixel_feed"}`
(`services/ingest/src/graph/query.py:1132`), which is the crawl and the platform's own
instrumentation. A store cannot supply the evidence for its own claim.

**The platform's own copy is held to the mirror-image rule: it may choose how to argue but not
what is true.** It may pick emphasis, ordering, framing and which true facts to lead with —
which is most of persuasion — but it may not introduce a fact the platform has not checked. The
screen is in `apps/buyer/svc/src/pitch/writing.py`: a generated case is thrown away whole if it
contains a number that is not already in the material it was handed, if it uses a word the
platform cannot support, if a withheld profile string leaks into it, if it quotes the shopper
back at themselves, or if it names none of the checked facts at all. Every refusal falls back
to a deterministic assembly of facts the platform already holds.

The reason is liability rather than tidiness. An agent optimising purely for conversion will
oversell, and when the platform writes the copy the platform owns the false claim.

---

# See it run

Everything below runs on one laptop. There is no Shopify account, no development store, no
payment gateway, and no LLM API key required by any of it.

```sh
make bootstrap
```

`scripts/bootstrap.sh` runs `uv sync --locked` and `npm ci --prefer-offline` **in this
checkout**, then asserts the result: that the venv it built is the one under this directory,
that the interpreter is 3.12, and that the flat import namespaces resolve outside pytest too.
The repo pins Python 3.12.13 (`.python-version`) and Node ≥ 22.12.0 (`package.json` `engines`).

Two conventions that will bite you if you skip them:

**Use `./.venv/bin/python`, never bare `python` or `python3`.** The virtualenv lives inside the
checkout and is not activated for you. Every Python command below is written with that prefix
on purpose.

**`PROXYSHOP_WORKER` isolates a run.** Anything that touches a datastore gets its own Postgres
database, `proxyshop_w$N`, chosen by this variable; `scripts/verify.sh` exits 2 immediately if
it is unset. Any number works — except that **worker 0 is reserved for the project's scored
measurement run and must not be used.**

## 1. The narrated run — no datastore, no docker, no configuration

```sh
./.venv/bin/python -m proxyshop_demo
```

Measured on this checkout: **454 lines of stdout, seven beats, exit 0, about three seconds**,
with `PROXYSHOP_WORKER` unset and with Postgres, Neo4j and Redis irrelevant. Two consecutive
runs printed the same line count.

It starts **eight uvicorn servers**, each on its own ephemeral loopback port, all as daemon
threads inside **one OS process** — the trust service, the buyer service, the exchange, two
store agents, and the three the merchant beat adds and names where it adds them (a Shopify
stub, a pixel collector, a webhook receiver). There is no `subprocess`, `Popen`,
`multiprocessing` or `fork` anywhere in `proxyshop_demo/s1.py`; they come from
`proxyshop_support/asgi_server.py`'s `serve()`, which is `uvicorn.Config(app, port=0)` on a
`threading.Thread`. A ninth port is bound and then closed deliberately, so the silent store
hands the exchange a genuine connection-refused rather than a simulated one.

Four sellers, each cast in a different role:

```
  The four sellers on this auction's roster, and the role each one plays:
      - store-northroast   hosted       prod-northroast-hx   list $389.00
      - store-brightbean   hosted       prod-brightbean-hx   list $449.00
      - store-slowreply    silent       prod-slowreply-hx    list $519.00
      - store-blocked      blacklisted  prod-blocked-hx      list $299.00
```

The eligibility gate runs before any store is contacted, and names its reason:

```
  Denied at the gate (1):
      - store-blocked  [blacklisted]
          reason: blacklisted: static-eligibility: store-blocked is blacklisted
```

The bids come back over real HTTP, and the silent store is represented anyway — at its
catalogue list price, marked `fallback`, which is the rule that a shop loses its ability to
discount by staying quiet, not its place in the auction:

```
      - store-northroast   bid            $389.00 unit / $389.00 total
      - store-brightbean   bid            $449.00 unit / $449.00 total
      - store-slowreply    did NOT bid    -> represented at list price $519.00
          fallback_reason: no_response
```

Ranking shows its terms rather than a number:

```
      - #1  store-northroast   score 0.4970
          delivery_fit=+0.050  intent_match=+0.175  price_value=+0.000  trust=+0.172  verified_claim_ratio=+0.100
          (the weighted terms sum to the score; nothing else moved it)
```

`price_value` reads `+0.000` for every candidate here because both agents are cold — no learned
policy — so each bid its own list price and no one undercut anyone. Their envelopes authorise
up to 20% and nothing in this slice makes them spend it. A demo that showed a discount here
would be showing a learning run.

Accepting a slot mints a single-use code and builds a cart permalink on the seller's
**registered** domain — the platform's record of that host, never the host the bid claimed for
itself, compared for exact equality with no subdomain wildcards. Following it completes a real
checkout at the local merchant stub, which emits a browser web-pixel beacon and an HMAC-signed
`orders/paid` webhook. The demo then offers the same code a second time and reports what
happened rather than asserting a property: the second order completes and simply carries no
discount, which is what a spent code does at a real cart.

Beat 7 reads the audit trail back off a **different application** over HTTP:

```
  The chain the trust service holds (17 event(s)):
      - seq 1   auction_opened  prev 000000000000.. -> self edf9112b923d..
      ...
      - seq 17  order_paid      prev e00bc6e0a76e.. -> self 6026682dedd2..
```

**Seventeen events in eight kinds** — `auction_opened`, `bid_placed`, `auction_closed`,
`shown`, `accepted`, `code_created`, `checkout_redirect`, `order_paid` — written by two
different applications and served back as a verified hash chain. (Read the count off your own
run; it moves with how many stores answer and how many slots fill.)

It closes by counting what it did and then naming what it could not do. That gap list is the
demo's own, measured on the spot, and it currently has exactly one entry — see
[What is not finished](#what-is-not-finished).

## 2. The shopper journey in a browser

```sh
PROXYSHOP_WORKER=1 npm run demo --workspace @proxyshop/buyer
```

That npm script builds the Vite/React SPA and then boots a launcher that runs **five ASGI apps
in one process** — three store agents, the exchange and the buyer app — verified against the
kernel as five LISTEN sockets on one pid. Only the buyer app takes a fixed port (8100);
everything else binds port 0, so the banner's other addresses are minted per run. The build is
required — `apps/buyer/dist` is gitignored, and a stack booted without it serves the API and a
"THE UI IS NOT BUILT" banner instead of the page. `npm run devstack:nobuild` is the one that
skips it, for API work. Ctrl-C shuts the whole thing down. No datastore is needed.

**Sign in before you type anything into the shopping box.** Step 2's *Confirm and ask stores*
button is not disabled while you are signed out; it is absent from the page, because confirming
is the step that leaves this origin — the exchange solicits real stores, and each one is told a
pseudonym minted by the buyer service's vault, which the browser cannot mint. Redeeming a
sign-in link reloads the page, so a conversation started first is gone when you come back.

There is no password. You type an address, the service issues a single-use link, and opening it
starts the session. There is no mail server on a workstation, so the launcher sets
`PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT=console` and the service prints the link into the
terminal you launched it from. The address does not have to be real.

That token is a real bearer credential, and `console` is a **local-development transport**:
anyone who can read the process's stdout can sign in as whoever asked for a link. It has to be
named out loud — a deployment that merely forgets to configure mail refuses every login with
`503` and prints nothing, which is what every non-devstack deployment gets.

**Type one of the two scripted conversations the banner prints, exactly, including its stated
number of follow-up answers.** `cluster_id` is a hash over the clarified query, the budget band
and the constraints, and a store agent declines any cluster its envelope does not pursue — so
every extra answer you type changes the hash, after which no store bids and you get an empty
page for a reason that is your own keystrokes.

The single-turn one — *"I want a warm merino wool beanie for winter, under $100"* — solicits
three stores and comes back with two slots. Measured, and stable across runs to the last digit:

| slot | store | unit price | `fit_score` | trust |
|---|---|---|---|---|
| `fit` | `demo-woolworks` | 78.00 USD | `0.57685` | 0.82 |
| `value` | `demo-alpine-supply` | 72.00 USD | `0.53485` | 0.61 |

The third store is excluded on the record, and the page gives the reason in full:
`blacklisted_store: 'demo-fastfleece' is blacklisted and may not participate (R12)`. The
auction id, the intent id and the discount code are minted per run; the stores, the prices, the
scores and the exclusion reason are not.

## 3. The whole deployment, in containers

The two demos above start their servers in one process. The compose stack is the harder claim —
that the *deployment* works — and it is written up in full in
**[`docs/demo/shopper-demo.md`](docs/demo/shopper-demo.md)**, which is the current runbook.

```sh
cp .env.example .env && set -a && . ./.env && set +a
make deps-up        # Postgres + Neo4j + Redis, then this worker's database and its migrations
make demo-corpus    # replay ten recorded storefronts into the Neo4j catalogue graph
make demo-up        # ten services; thirteen containers once the datastores are counted
make demo-check     # drive a real auction over HTTP and fail loudly if the market is dead
```

`make demo-corpus` is the slow one, and it is the one worth understanding. It replays
`fixtures/real-catalogs/` — a point-in-time recording of **ten real supplement storefronts'
public `products.json`, 3,093 products across 18 pages, taken under robots.txt** — through the
same crawl, upsert and embed path a live crawl takes. Whole catalogues, not a category sample:
liver support is 1.1% of the corpus (35 of 3,093), and two of the ten stores carry no
liver-support inventory at all and answer a milk-thistle query with protein stacks. They are
there deliberately. A graph in which almost everything matches proves nothing about matching,
and a shortlist that never has to reject anybody demonstrates nothing.

`make demo-check` is the step the container healthchecks cannot be. It opens an auction with
**no `roster` key in the request body at all**, which is the one request only the catalogue
graph can answer, and it fails by name on each way that can come back empty. Read
`roster_source` first: `"source": "neo4j"` with a non-zero `shops` is the proof that the
candidate set was *found* rather than supplied. Measured on this checkout after a fresh
`make demo-corpus`:

```
roster_source: source='neo4j' shops=7 products_considered=25 reason=None elapsed_ms=60.703
entries=7  ranked=7  shortlist slots=4
hosted bids=4  fallback reasons=['no_response']
```

Seven shops out of ten, with no roster sent — the three left out carry nothing matching. Both
halves of the market are in that one response: four shops that run an agent and really bid, and
three with no agent, represented at their catalogue list price and marked
`fallback / no_response`.

Two things the runbook says that are easy to learn the hard way: **do not run the Python test
suite against the same Neo4j the demo is using** (every graph test opens with
`MATCH (n) DETACH DELETE n`, and it does not ask whether a demo is loaded), and a green
container list is not a working demo — every container in the stack can report healthy while
the market is dead.

Both of those were observed while this page was being written, which is the best argument for
running the probe after every deploy. The block above is a real run on a full corpus. A little
later, with a test suite having run in the same checkout, the same command against the same
healthy stack answered:

```
roster_source: source='neo4j' shops=2 products_considered=25 reason=None elapsed_ms=30.581
entries=2  ranked=2  shortlist slots=2
hosted bids=0  fallback reasons=['no_response']
```

— two shops instead of seven, no hosted bids at all, and Neo4j holding 214 products rather than
3,093. Every container was still `(healthy)`. `make demo-check` failed on it by name;
`make demo-corpus` puts it back.

---

# How it works

## The services

Every service is a FastAPI app imported as `<namespace>.main:create_app` unless noted; the port
listed is what its Dockerfile's CMD binds. **The six product services serve fifty-six routes
between them, and every one of the fifty-six is driven by at least one test** — measured by
`./.venv/bin/python -m proxyshop_support.route_census`, which also checks each service against
its published OpenAPI contract and currently reports no drift in either direction. Run it rather
than believing that sentence; it is the fastest way to find a route somebody built and nobody
reached.

| Path | Port | What it is |
|---|---|---|
| `apps/buyer` | 8081 | Three things in one directory: the Vite/React SPA (`@proxyshop/buyer`), the FastAPI buyer service (`buyer_svc`), and `devstack/run.py`, the launcher demo 2 uses. Fifteen routes — clarify, confirm, read an auction, render and accept a shortlist, magic-link auth and session, profile, feedback and its prompt, a live-check pair, and the store-visible window. |
| `apps/exchange` | 8083 | The auction, the ranker and the checkout port. `POST /auctions`, `GET /auctions/{id}`, `GET /auctions/{id}/shortlist`, `POST /auctions/{id}/accept`, the external Tier-2 bid door `POST /v1/auctions/{id}/bids`, `POST /internal/outcomes` and `GET /reports/losses`. |
| `apps/merchant` | 8082 | The merchant app: install and OAuth callback, the Shopify webhook door, the pixel collector, code minting, envelope read/write, the kill switch, and the merchant console mounted at `/dashboard`. |
| `apps/trust` | 8084 | The hash-chained, append-only ledger plus scoring, reconciliation and snapshots. Ten operations, exactly the ten `trust.openapi.json` declares. |
| `services/ingest` | 8085 | Storefront adapters, LLM extraction, entity resolution, the Neo4j catalogue graph, and the refresh scheduler. |
| `packages/store-agent` | 8086 | Despite living under `packages/`, this **is** a deployable — one process per shop, holding that shop's context file. `POST /v1/bid-requests` answers 200 with a Bid or 204 with a Decline; `POST /v1/trust-events` is how the network tells a shop what its score did. |
| `services/shopify-stub` | 8787 | `shopify_stub.app:create_app`. A local implementation of exactly the Shopify surface this system uses: the Admin GraphQL mutations, cart permalinks, checkout completion, webhook deliveries and the pixel surface. This is what stands in for a development store. |
| `deploy/buyer-web` | 8080 | Not a Python service. The nginx image that serves the built SPA **and proxies `/buyer/` to `buyer-svc`** — the page and the API have to be one origin, because every endpoint in the SPA is a bare relative path and the buyer service installs no CORS middleware. This is the address a shopper opens on the compose stack. |
| `apps/seller-reference` | — | **Not a service.** Its whole tracked source is two `__init__.py` files; its Dockerfile CMD builds every adversarial persona and exits. The personas exercise the external bid door. |
| `services/sim` | — | **Not a server.** A headless CLI — `./.venv/bin/python -m sim --json` — whose exit code is the finding. |

Supporting directories: `packages/contracts` (a dual Python + npm package — the JSON-schema
registry, protocol models, JCS signing, OpenAPI helpers and codegen), `packages/llm` (the
Anthropic client wrapper, configuration, the offline doubles and prompt recordings),
`packages/verification` (claim comparators and verification statuses), `proxyshop_support`
(shared runtime helpers every service imports), `pixel` (the Shopify web pixel extension, whose
beacon is built from a four-key allowlist so no customer PII can enter it), `proxyshop_demo`,
`e2e`, `db`, `fixtures` and `deploy`.

Before you deploy or scale anything, read [`docs/deploy.md`](docs/deploy.md). It explains why
`--workers 1` on the exchange is a correctness pin rather than tuning: raising it, adding
`deploy.replicas`, or running `--scale exchange=N` re-opens a double-spend that mints two live
discount codes for one purchase. Nothing shipped is broken — the pin holds it shut — and
nothing else in the documentation will tell you that before you change it.

## One auction, end to end

1. **Clarify.** The shopper says what they want. The buyer service asks **at most three**
   clarifying questions — a published ceiling, not a heuristic — and turns the answers into a
   structured intent: a query, a category, a budget band, hard constraints and preferences. The
   clarifier never self-confirms.
2. **Confirm.** The shopper's separate act. Until it happens no auction exists. This is the
   step that leaves the buyer's origin, which is why it needs a signed-in session.
3. **Find the shops.** With `EXCHANGE_SHOP_ROSTER=graph` and a deployment document named by
   `EXCHANGE_DEPLOYMENT` — the two are one switch, and neither alone does anything — a
   `POST /auctions` carrying no roster walks the Neo4j catalogue graph for products that both
   match the intent and satisfy its hard constraints, and rosters their shops. Any request that
   carries its own roster uses it and never touches the graph; `roster_source` on the response
   says which happened, and `"unwired"` means the graph door was never opened.
4. **Gate, then solicit.** Eligibility is checked *before* anyone is asked, so a blacklisted
   store is denied without being contacted. Solicitation then fans out to every eligible store
   in parallel inside a bounded window. The solicitation names the product it is asking about.
   A store that does not answer in time is still represented, at its catalogue list price,
   marked `fallback` — and a fallback asserts no claims, so it can never be the evidence that
   satisfies a hard constraint.
5. **Verify, filter, rank.** Every claim in every bid is graded against the platform's crawl.
   Hard constraints are eligibility filters and never score terms: a contradicted hard
   constraint cannot win regardless of how good the pitch is, and an ambiguous one cannot
   satisfy it either. What survives is ranked by the published formula below.
6. **Shortlist.** Up to four differentiated slots — `fit`, `value`, `reliability`,
   `specialist` — each carrying the product, the price, the store's own commitments as
   published `Claim`s with real provenance, a trust summary, provenance labels, and the
   platform's registered domain for that store. Slots collapse gracefully when there are fewer
   distinct stores than slots.
7. **Accept.** Eligibility is re-checked. A single-use discount code is minted and a cart
   permalink built on the seller's registered domain. A bid whose checkout URL pointed at
   `checkout.<seller>` or `evil-<seller>` is refused here, *before* any code is created.
8. **Observe.** The checkout emits a browser web-pixel beacon and an HMAC-signed `orders/paid`
   webhook. The pixel is corroboration and is allowed to go missing; the webhook is evidence
   and carries the money. Both are appended to the same hash-chained ledger, which
   reconciliation folds into a verdict that moves the store's trust score.

Every transition is appended to an append-only ledger on a different service, and each event's
`prev_hash` is the previous event's `event_hash`. `GET /events/verify` checks the links *and*
an anchor recorded outside the row list, so a stream truncated to a shorter but perfectly
linked prefix still fails — a flawless chain of the wrong length is still a tampered one.

## The ranking formula

There is exactly one, it is published, and it is fee-blind and tier-blind
(`packages/contracts/src/ranking.py`):

```
rank_score = 0.35·intent_match + 0.20·verified_claim_ratio + 0.20·trust
           + 0.15·price_value  + 0.10·delivery_fit  −  policy_penalties
```

Weights version `1.0.0`, feature-definition version `3.0.0`. The two are versioned separately
because they change independently and a replay needs both.

| Term | What it reads |
|---|---|
| `intent_match` | How well the offer matches what this shopper actually asked for. |
| `verified_claim_ratio` | `1 − Π(1 − gain)` over the claims the exchange graded `verified` **whose subject this buyer asked about** — weighted by how hard they asked (a hard constraint, a stated preference, a word in the query). Claim-stuffing pays exactly nothing: a verified claim about something nobody asked contributes 0.0, worth precisely what silence is worth. This is the term that pays for a per-shopper pitch, and it is the only one that is both movable at bid time and dependent on this buyer. |
| `trust` | The store's own posterior over six dimensions: `price_honored`, `discount_honored`, `shipped_on_time`, `not_returned`, `feedback_match`, `catalog_claim_accuracy`. Verification outcomes and transaction outcomes update the same Betas, so a store has trust signal before it has ever sold anything. |
| `price_value` | The discount term. It **saturates at the auction's own price band** — there is no marginal return past clearing it. This is the mechanical reason winning on price wins one slot of four rather than the shortlist. |
| `delivery_fit` | Not the store's promise. A quoted dispatch time is divided by the store's own `shipped_on_time` posterior before the auction normalises it, and **a store with too little shipping history is not admitted to the term at all** — the feature reads absent, which scores the published neutral 0.5. A store with no record can neither win this term nor be punished by it. A promise costs nothing to make; this term is the correction for having paid one out before anyone found out whether it was kept. |

Three properties are enforced in the contract rather than left to the ranker. The five weights
**must** sum to 1.0 — a set that does not is invalid, not silently renormalised, because
renormalising means the published weights and the applied weights are different numbers and
nobody can tell which produced a given shortlist. **Missing features read their published
neutral, not zero** — scoring an absent value as 0 punishes every store the verifier has not
reached yet, which is a systematic bias in favour of whoever was crawled first. And
`policy_penalties` is bounded, so one repeated event kind cannot drive a score arbitrarily
negative and stop the formula being a comparison.

---

# What is not finished

This repository writes down what does not work. The list below is measured against the tree,
and the durable instruction is the one at the end of it: **believe the run, not the prose.**

## The demo's own gap: reconciliation returns no verdict

The narrated demo prints exactly one `DOES NOT RUN YET` block, and this is it.
`trust.reconcile.reconcile` joins the accepted offer, the browser beacon and the webhook into a
single `reconciled` verdict, and that verdict is what moves a store's trust score — the same
snapshot the ranker reads.

Both halves exist, are tested, and are wired to each other. `POST /reconcile` is a served route
and the whole event trio reaches one chain. **The fold over that chain still returns 0
verdicts**, for two reasons the demo measures rather than asserts:

1. **The two halves of the purchase name the seller differently.** Reconciliation namespaces
   every join key by the store that owns it — it has to; a Shopify `order_id` is a per-shop
   number and two shops both have order 1001. The `accepted` event now does carry a `store_id`,
   but the signed `order_paid` names `proxyshop-demo.myshopify.com`, because an unsigned
   `X-Shopify-Shop-Domain` header is the only shop identity a signed delivery carries at all.
   The mapping back is built — `trust.reconcile.routes.resolve_store_aliases` reads the
   platform's own seller roster — but that roster is Postgres, and this driver starts none. The
   demo prints a **probe** beside the real answer: supply the two names and the same events,
   the same webhook and the same fold produce one real verdict, price honoured, `389.0 / 389.0`.
2. **This driver posts no beacon.** The producer is not missing — `POST /pixel/collect` writes
   a real `checkout_pixel` row to the chained ledger, and the e2e driver drives that served
   route and gets one. The narrated demo runs no merchant service, so this run's chain carries
   no beacon. That costs *evidence* rather than the verdict: a group with no beacon grades
   `pixel_missing`, which by design is not a blocker.

## What is seeded rather than live

- **The merchant console serves, and on a cold stack it mostly says so.** The SPA is mounted at
  `/dashboard` on `merchant-svc` (port 8082 on the compose stack) behind the
  `MERCHANT_ADMIN_TOKEN` bearer, and `GET /stores/{store_id}/dashboard` returns the whole page in
  one read. It has six panels — onboarding, envelope, losses, trust, trust events, bids — and
  each carries its own `state`, so one unconfigured collaborator is a sentence rather than a
  blank page. On a cold service four of the six read `not_configured` and name the variables
  they want (`EXCHANGE_URL`, `TRUST_URL`, `STORE_AGENT_URL`, `MERCHANT_REPORT_TOKENS`), because
  every panel on this page is downstream of a shopper who does not exist yet. All of its state
  is in-memory per process: restart the service and the console is empty again.
  A populated page does exist as committed bytes — one real 200 body from the real route, in
  `deploy/demo-seed/merchant-dashboard/`, whose README opens *"No merchant has ever looked at
  this page, and not one auction it reports was ever run."* **Nothing in the tree reads it back**,
  so opening the console on a running stack shows you that stack, not the seeded page.
- **The seeded feedback panel is marked, and the door it uses is real.** The buyer page's
  feedback step is unconditional and labelled SEEDED, gated on an order reference carrying a
  `sim-fb-` marker, and submitting it hits the genuine `POST /buyer/feedback` route — which can
  and does refuse it honestly. The trust numbers in `services/sim/seed-data/feedback-population/`
  were produced by driving that same route. **There is no boolean env var that flips feedback
  from seeded to live**, and any documentation claiming one is wrong: going live means real
  traffic arriving at the same door, and the two populations are told apart afterwards by the
  `sim-fb-` marker rather than by a flag.
- **Half the store-visible window is a manufactured corpus.** `GET /buyer/store-window` serves
  the pseudonym, coarsened buckets and provenance a store is allowed to see. Rows are marked
  `seed` or real by a database constraint, and going live changes no serving code — deleting the
  seed rows leaves it serving only real logins. It refuses to serve at all unless
  `PROXYSHOP_BUYER_STORE_WINDOW_TOKENS` is configured (503), and enforces a k-anonymity floor
  that defaults to 2.
- **No live model has written a pitch here.** With `LLM_PROVIDER` unset the default is the
  offline deterministic double, and that is the designed default rather than a failure — the
  running services say so out loud at boot: `llm[store_agent] TEMPLATED: provider=double
  model=claude-sonnet-5 sdk=installed api_key=absent`. Set `LLM_PROVIDER=anthropic` and
  `ANTHROPIC_API_KEY` and the shop's own voice is written by the model; the boot line changes to
  `LIVE`, and it only says `LIVE` when all three of provider, SDK and a non-blank key are
  present. **What has not happened is a run against the real API from this checkout** — the live
  path was proven with a stubbed provider, so the SDK, the client, the POST, the parse and the
  output screen are all genuinely exercised and the model's own reply is not. Note also that a
  missing key does not fail the bid: the store agent catches it, logs it, and ships the
  deterministic fallback pitch.

## What is genuinely not built

- **The shop's own written words still do not reach the shopper. This is the largest open gap
  against the product thesis**, because making its case in its own voice is the thing a shop is
  buying. A store agent really does write a per-shopper pitch onto `Bid.message`; the exchange's
  own candidate projection really does carry it (`apps/exchange/src/ranking/candidates.py:165`);
  and `POST /buyer/shortlist/render` really does publish a two-voice object per slot. But
  `contracts.protocol.ShortlistSlot` is `extra="forbid"` and declares no message field
  (`packages/contracts/src/generated/protocol.py:655`), and `ranking/shortlist.py` never passes
  one, so the seller's prose is dropped at that boundary. Measured on the browser demo, every
  slot renders:

  ```json
  "pitch": {
    "platform_case": "You said price was a must-have, and here it is: 78.00 USD. …",
    "platform_case_source": "assembled",
    "store_pitch": null,
    "voices": ["platform"]
  }
  ```

  The organic voice is on the wire. The sponsored one is computed and thrown away. What a slot
  *does* carry from the store is its `commitments` — real published `Claim`s with provenance,
  which is what makes them gradeable later.
- **The ledger holds enough to reconstruct an auction, and nothing serves that reconstruction.**
  The exchange writes an outcome block that carries the roster in order, who was solicited, who
  answered, every fallback reason, the eligibility denials with their conditions, the ranker's
  exclusions with grounds, which slots were filled by whom, the per-bid policy penalties with
  their clamped total, and the acceptance. The reconstruction that reads it back exists **only
  in a test** (`apps/exchange/tests/test_auction_ledger_reconstruction.py`). There is no served
  route for it; `GET /auctions/{id}` returns in-memory machine state and 404s once the auction
  is let go.
- **`make e2e-live` is a preflight that always exits non-zero, and that is not news.** Exit 2
  means a live precondition is unmet; exit 3 means every precondition holds but the live driver
  itself is missing. Measured on this checkout it exits **2**, naming four of five unmet:
  `SHOPIFY_API_KEY`/`SHOPIFY_API_SECRET` unset, `MERCHANT_APP_URL` deliberately unresolvable,
  `CHECKOUT_MODE` not `redirect`, and `EMBEDDING_PROVIDER=local_bge` unable to embed because
  torch is not installed. It also notes that the extension runbook
  `docs/demo/shopify-onboarding-extension.md` has not landed.

## The page keeps its own gap list

The running shopper page renders a **"What is not wired yet"** panel
(`apps/buyer/app/journey/Journey.tsx`). Read the page rather than this paragraph — the panel is
measured on the stack in front of you and prose is not. It currently carries five items:
`gap-fallback` (a slot cannot say whether its price came from a bid or from a stood-in list
price; `entries[]` can, per store), `gap-product-name` (the slot carries `product_ref` and
`variant_ref`, and nothing here resolves a title, so the card shows the reference rather than
inventing a name), `gap-store-voice`, `gap-feedback-seeded` and `gap-model` — the last three
being the same three described above.

## Where the rest of the known defects are written down

The strict xfails are this repository's open-defect register, and each carries its reason in its
own decorator. **Run the grep rather than trusting any count written down here** — the register
moves as tickets close, and a number typed into prose goes stale the moment one does:

```sh
grep -rn "^@pytest.mark.xfail" --include='*.py' apps packages services proxyshop_support
```

Measured at the time of writing: **five** markers in four files. Three fire on an ordinary run
and name a ticket — T-325 and T-158 (exchange) and T-262 (`proxyshop_support`). The other two,
in `services/ingest/tests/test_embedding_ranking_gate.py`, are conditional on
`EMBEDDING_PROVIDER=hash`; the default is `lexical`, so on an ordinary run they do not fire and
they name no ticket. They are a guard on a provider choice rather than an open defect, and the
choice they guard is worth knowing: driven through the real Neo4j 1024-dimension cosine index,
`hash` answers a "running shoes" query with a coffee grinder, a sunscreen and an espresso
machine, and `lexical` returns the three running shoes in order.

Some of what those markers name is larger than anything listed above. Read them before assuming
a behaviour works.

## And do not trust this list either

Where prose and a run disagree, believe the run: the gap list the demo prints is measured on the
spot and this page is not. A register of stale text is stale by construction, because the thing
that makes an entry wrong — somebody fixing the code — is exactly the thing that does not update
the register. **Roughly a third of the apparent defects found in this repository over a
two-cycle audit were prose asserting a defect that had already been fixed.** Treat a
present-tense claim in any comment, docstring, runbook or README bullet as a claim to re-check
rather than a fact. If a behaviour is not demonstrated by a demo or by the test suite, treat it
as unbuilt.

---

# Working on it

```sh
PROXYSHOP_WORKER=1 make verify
```

`make verify` is `scripts/verify.sh all`, and it exits 2 before doing anything at all if
`PROXYSHOP_WORKER` is unset. The datastores come up separately with `make deps-up` (Postgres,
Neo4j and Redis via docker compose, then the database **and its migrations**) and go down with
`make deps-down`, which destroys the volumes.

`verify.sh all` runs, in order: `ruff check` and `ruff format --check`, `lint-imports`, a grep
over every tracked file for a banned global Redis flush, then `eslint`; `mypy` and `tsc -b`;
`pytest -q -m "not needs_model"`; `vitest run`; and finally
`./.venv/bin/python scripts/check_verify_contracts.py`. A full run takes several minutes.

Two things the gate does deliberately and loudly, worth knowing before you read its output:
**collecting zero tests is a failure, not a pass** (pytest's exit 5 is fatal here, including
when every test was deselected), and the deselected count is printed on its own line every time,
so "passed" can never be mistaken for "ran".

> **Measured here, the last stage fails, so `make verify` does not currently end `OK: all`.**
> `./.venv/bin/python scripts/check_verify_contracts.py` exits 1 on a D36 violation:
> `apps/merchant/svc/tests/test_pixel_ledger.py` has `pixel` in its path but lives outside
> `pixel/`, and `npx vitest run pixel` filters by case-insensitive path substring, so that file
> would silently join the pixel ticket's run. It separately reports two ticket verify paths
> nobody has written yet — T-086 (`e2e/test_onboarding.py`) and T-087
> (`docs/tests/test_runbook.py`) — which is a note rather than the failure.
>
> That is one command and it takes under a second, so **check it rather than believing this
> box.** It is exactly the kind of paragraph that goes stale the moment somebody moves a file.

Narrower targets, all real in the `Makefile`:

| Target | What it does |
|---|---|
| `make bootstrap` | `uv sync --locked` + `npm ci --prefer-offline`, in this checkout |
| `make check` | The per-ticket gate — runs the docker tests, which skip per-service when their datastore is down |
| `make lint` | ruff + import-linter + the banned-reset gate + eslint |
| `make types` | mypy + `tsc -b` |
| `make test-py` / `make test-ts` | Python or TypeScript tests only |
| `make deps-up` / `make deps-down` | Bring the datastores up (then init and migrate) or tear them down |
| `make demo-corpus` / `make demo-up` / `make demo-check` / `make demo-down` | The compose shopper demo — see [`docs/demo/shopper-demo.md`](docs/demo/shopper-demo.md) |
| `make e2e-live` | The live development-store preflight. Always exits non-zero; see above. |

Two more worth running directly:

```sh
./.venv/bin/python -m proxyshop_support.route_census   # every served route, driven or not, vs its contract
./.venv/bin/python -m sim --json                       # the dishonest-store simulation; its exit code is the finding
```

The simulation is the check that the trust engine catches a scripted dishonest store whose
behaviours are defined in a human-approved fixture manifest rather than by the trust engine's
own config — which would make the result circular. On this checkout it exits 0 and reports
`caught_at: 1` over a verified chain.

## Where the rulings are

`.swarm-loop/decisions.md` holds the design decisions, each with the argument that produced it.
The ones that shaped the product as it now stands are **D55** (two agents, two objective
functions; what a shop actually buys), **D56** (the default embedding provider, and the
difference between exercising an index and ranking a catalogue), **D57** (a delivery promise
must be earned before it counts), **D58** (the solicitation names the product) and **D59**
(a stock reading is binary, and it expires).
