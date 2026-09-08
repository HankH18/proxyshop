# ProxyShop

A shopper says what they want in plain language. ProxyShop searches its **own crawl of the
web** — not a merchant feed, not a paid index — ranks what it finds on five measured features,
and hands back a short list of real shops with real prices and a working checkout link.

The interesting part is what happens to the *sales pitch* on each result.

| | **Organic** | **Sponsored** |
|---|---|---|
| How the shop got here | ProxyShop crawled it. The shop did nothing and knows nothing about it. | The shop runs an agent inside the network. |
| Who writes the pitch | ProxyShop, from its own crawl. | **The shop, in its own words.** |
| Who sets the price | The shop's public catalogue price, as crawled. | The shop's agent, per shopper, within a discount envelope it approved. |
| Where it ranks | Earned by matching. | **Earned by matching.** |

That last row is the whole design. **A sponsored shop does not buy visibility and does not buy
score.** It buys the right to make its case in its own voice — its price, its discount, its
commitments, its prose — for *this* shopper, on *this* query.

That is only safe because of the asymmetry underneath it: the seller's message is checked
adversarially against the platform's own crawl. Pitch text is decomposed into typed claims and
each is graded `verified`, `contradicted`, `unsupported` or `ambiguous`, and the evidence that
decides a grade may only come from a source the *platform* authored —
`PLATFORM_OBSERVED_SOURCE_CLASSES` is `{"scraped", "pixel_feed"}`
(`services/ingest/src/graph/query.py`). A store cannot supply the evidence for its own claim,
and only `verified` can satisfy a hard constraint. An unknown is never treated as a yes.

**Two things about that check on the demo deployment specifically**, because the mechanism being
right is not the same as the mechanism doing work. First, the crawl the exchange grades against
there is not the whole graph: `deploy/demo/exchange-deployment.json` carries a `catalog`
snapshot of exactly **60 products for each of the four hosted stores** — 240 rows against 3,093
in the graph — so a product the graph rosters from outside that window has no platform evidence
to check and grades `ambiguous`, which cannot satisfy a hard constraint. Second, the check pays
nothing unless the shopper asked for something checkable: in the worked example below,
`verified_claim_ratio` reads its neutral for all seven candidates, because the query carries no
hard constraints and a verified claim about something nobody asked is worth what silence is
worth. Re-run with `price_usd lte 50` and the same term rises to 0.1595 for all four bidding
stores. The gate is real and it is fail-closed; whether it does any work is up to the shopper.

The platform's own copy is held to the mirror-image rule: it may choose *how* to argue but not
*what is true*. `apps/buyer/svc/src/pitch/writing.py` throws away a generated case whole if it
contains a number that was not in the material it was handed, uses a word the platform cannot
support, leaks a withheld profile string, or names none of the checked facts. Every refusal
falls back to a deterministic assembly of facts already held.

**It is not an auction.** An early framing as a first-price sealed auction was rejected on a
mechanical argument: every store has a maximum discount it will authorise, and a market whose
allocation is dominated by discount drives every store to that maximum and then differentiates
on nothing. Discount is one saturating term of five, and clearing the shopper's price band is a
qualifier rather than a differentiator.

---

# One query, end to end

Everything in this section is a real response from the compose stack, captured while writing
this page. Nothing is illustrative.

**The request.** One `POST /auctions` with an intent and a coarsened buyer profile, and
**no `roster` key at all** — the candidate set has to come from the catalogue graph:

```json
{"intent": {"intent_id": "readme-probe-1",
            "query": "milk thistle silymarin liver support extract",
            "cluster_id": "cluster-liver-support", "currency": "USD",
            "hard_constraints": [], "preferences": [],
            "created_at": "2026-09-08T12:00:00Z", "schema_version": "1.0.0"},
 "profile": {"pseudonym": "readme-probe-shopper",
             "buckets": {"budget_band": "0-50", "first_time": true}}}
```

**Send the whole intent, not the interesting half of it.** `Intent` requires `intent_id`,
`query`, `hard_constraints`, `preferences`, `created_at` and `schema_version`, and this page used
to print the block without the last three. That is not a 422 from the exchange —
`CreateAuctionRequest.intent` is a bare `dict`, so the exchange takes it, answers **201**, and
forwards it verbatim; the refusal happens at each store agent's door, which validates against the
published contract. Measured with the short body against the four hosted agents: every one
answered `422`, the auction came back `sponsored: 0` with `fallback_reasons:
{"store_refused": 4}`, and nothing in the auction response said which fields were missing.
`scripts/demo_check.sh` sends the complete intent and carries a comment about exactly this.

**Who was found.** The graph is a point-in-time recording of ten real supplement storefronts'
public `products.json` — 3,093 products, taken under robots.txt — replayed through the live
crawl path:

```json
"roster_source": {"source": "neo4j", "shops": 7, "products_considered": 25,
                  "reason": null, "elapsed_ms": 24.349}
"market": {"solicited": 7, "sponsored": 4, "list_price": 3,
           "timed_out": 0, "not_asked": 0, "denied": 0, "bid_window_seconds": 5.0,
           "fallback_reasons": {"no_response": 3}, "all_fallback": false}
```

**How many shops the graph finds depends on how much of the corpus is actually loaded, and it
moves.** Re-driving the same stack on 2026-09-08 the graph's roster was **six** shops, not seven,
and `purebulk.com` was not among them — `sponsored: 4, list_price: 2, no_response: 2`. The four
sponsored stores are fixed by the deployment document and do not move; the organic tail is
whatever the graph holds at that moment, and a partially-loaded graph is the ordinary outcome of
the content-hash trap [documented below](#2-the-whole-deployment-in-containers). Read the shape
and the split, not the digits.

Seven shops out of the ten in the corpus; the other three carry nothing matching. Of the seven,
**four run an agent and really bid; the remaining three run none and were carried at their
catalogue list price**. That split is not a coincidence of this run — it is the deployment
document: exactly four of the ten sellers carry a `bid_endpoint` and six do not. Both halves of
the market are in one response.

Read `solicited: 7` and `no_response: 3` with a correction, though, because those two fields are
wrong about the three: **nobody asked them.** `HttpBidSolicitor.solicit`
(`apps/exchange/src/composition.py`) returns `None` without opening a socket for a store the
deployment document holds no `bid_endpoint` for, and `collect_bids`
(`apps/exchange/src/auction/collect.py`) then labels a `None` answer `no_response` for any store
whose roster tier is above 0 — which is every store the graph rosters. The right label exists in
the same file and is unreachable on this path. It is written up under
[What is not built](#an-organic-shop-nobody-asked-is-reported-silent). Measured: across the whole
life of the exchange container, `docker logs proxyshop-exchange-1 | grep -ci 'bulksupplements\|nutricost'`
returns **0** — those two names have never appeared in an outbound solicitation.

**How they ranked.** The published formula, term by term, summing exactly to the score:

| store | `intent_match` | `verified_claim_ratio` | `trust` | `price_value` | `delivery_fit` | **`rank_score`** |
|---|---|---|---|---|---|---|
| `gaiaherbs.com` | 0.20525 | 0.100 | 0.172 | 0.000 | 0.050 | **0.52725** |
| `paradiseherbs.com` | 0.20999 | 0.100 | 0.162 | 0.000 | 0.050 | **0.52199** |
| `purebulk.com` | 0.23981 | 0.100 | 0.132 | 0.000 | 0.050 | **0.52181** |
| `oregonswildharvest.com` | 0.21333 | 0.100 | 0.156 | 0.000 | 0.050 | **0.51933** |
| `bulksupplements.com` | 0.22147 | 0.100 | 0.138 | 0.000 | 0.050 | **0.50947** |
| `nutricost.com` | 0.21512 | 0.100 | 0.144 | 0.000 | 0.050 | **0.50912** |
| `toniiq.com` | 0.20678 | 0.100 | 0.148 | 0.000 | 0.050 | **0.50478** |

Read the columns before the ranking. `verified_claim_ratio` and `delivery_fit` are at their
published neutral for every candidate (0.20 × 0.5 and 0.10 × 0.5), and `price_value` is 0.000
because every agent was cold and bid its own list price. That leaves two columns that differ
between stores, and only one of them is a live reading.

`intent_match` is measured on this request, by the platform's own retrieval, per store.
**`trust` is a constant on this deployment.** Every value in that column is 0.20 × a `score`
typed into the `trust_snapshot` key of `deploy/demo/exchange-deployment.json` — fixed
alpha/beta per store, `decayed_at` frozen at `2026-09-01T00:00:00+00:00`. It is not read back
from the trust service, and by design: `_bind_live_ranking_snapshot` in
`apps/exchange/src/composition.py` returns without binding a live reader whenever the document
states that key, because "that key is a person's statement and this function does not overrule
one". So it separates the stores, and nothing that happens during a demo moves it.

It is worth being blunt about how little the exchange is giving up by doing that here. `GET
/snapshot` on the running trust service holds **one** store — `store-breaker`, the simulation's
dishonest merchant — and no record at all for any of the ten demo sellers. So the exchange is not
preferring a stale reading over a fresh one; there is no fresh one. The frozen key is what makes
the demo's `trust` column exist.

**One term did the work here**, not two. That is thin, and this page would rather say so than
let five decimal places imply five live signals.
[Which terms can move, and when](#what-actually-moves-a-score) says why.

**What the shopper is shown.** Four differentiated slots, and this is where sponsored and
organic separate on the wire:

| slot | store | price | crawled product name | bid? | the shop's own words |
|---|---|---|---|---|---|
| `value` | gaiaherbs.com | 25.49 | *Milk Thistle Gummies* (Gaia Herbs) | yes | "Currently in stock and available now, backed by a 30-day return window for peace of mind on your first order." |
| `reliability` | paradiseherbs.com | 11.99 | *Milk Thistle* (Paradise Herbs) | yes | "…backed by a 30-day return window—good peace of mind if this is your first time trying milk thistle extract from us. Only 12 units remain available." |
| `fit` | purebulk.com | 5.75 | — | **no** | **`null`** |
| `specialist` | oregonswildharvest.com | 18.95 | *Milk Thistle, Organic Extract* | yes | "Free returns: 30 return window. Also: in stock: yes; units left: 12." |

Four things to notice.

1. **`purebulk.com` never answered and still took the `fit` slot.** A shop that runs no agent
   loses its ability to discount and to speak. It does not lose its place.
2. **The last row is not the shop's voice, whatever the column header says.** *"Free returns: 30
   return window. Also: in stock: yes; units left: 12."* is `fallback_pitch` output — the store
   agent's deterministic slot-filling, served because the screen refused the model's reply on a
   content rule. The agent logged that solicitation `outcome=ok source=model` anyway — the log now says `pitch_call=answered`, which is what the route can actually attest. Two of the
   three bidding rows here are model prose and one is not, and nothing on the wire says so; see
   [About the model, precisely](#about-the-model-precisely).
3. **The product name is the platform's, not the shop's.** It comes from
   `exchange.ranking.verification.catalog_identity` reading the same crawl snapshot the
   exchange grades claims against, and it is published with the snapshot id that produced it
   (`{"title": "Milk Thistle Gummies", "brand": "Gaia Herbs", "source": "snap-gaiaherbs.com"}`).
   The fallback slot's is `null`, because there is no crawl snapshot joined to its ref.
4. **Both voices reach the page.** `POST /buyer/shortlist/render` publishes one pitch object
   per slot, and each names whose voice is in it:

```json
{"voices": ["store", "platform"], "platform_case_source": "written",
 "platform_case": "Priced at 25.49 USD, with a 30-day free return window confirmed by the
                   store and an 86% reliability rating; this offer holds until 2026-09-08.",
 "store_pitch":   "Currently in stock and available now, backed by a 30-day return window
                   for peace of mind on your first order."}
```

The scraped shop's slot comes back `"voices": ["platform"]` with `store_pitch: null`. That
single field is the difference between an organic result and a sponsored one.

`platform_case_source` is not constant across one response, and that is the mirror-image rule
firing rather than a bug. Measured on four slots of one render: two read `written` and two read
`assembled` — the assembled ones are visibly the deterministic fallback (*"Reliability: 86%. Also
offer held until: 2026-09-08; price: 25.49 USD."*) against a written one (*"Reliability score of
81% based on platform checks, with the price at 11.99 USD held until 2026-09-08, giving a new
buyer time to decide without price pressure."*). The platform threw its own generated case away
on two slots and printed facts it already held instead. Unlike the store agent, it says which it
did, in the response, per slot.

## Now change one thing

Send the *same query* with a `roster` in the request body instead of letting the graph find it.
This is not a hypothetical path — it is the one the browser shopper page takes on every auction,
though with its own fixed roster rather than the ad-hoc one below
([why](#3-the-shopper-page-and-the-learning-demo)). Measured, same stack, minutes later:

```
gaiaherbs.com           intent_match=0.175  trust=0.172  price_value=0.09962   rank_score=0.59662
oregonswildharvest.com  intent_match=0.175  trust=0.156  price_value=0.08681   rank_score=0.56781
toniiq.com              intent_match=0.175  trust=0.148  price_value=0.06912   rank_score=0.54212
paradiseherbs.com       intent_match=0.175  trust=0.162  price_value=0.03833   rank_score=0.52533
purebulk.com            intent_match=0.175  trust=0.132  price_value=0.00000   rank_score=0.45700
```

`intent_match` is **0.175 for everybody** — 0.35 × the published neutral 0.5. It is the one
feature the exchange cannot compute: a served auction handed a roster queries no index, so
there is no retrieval measurement to carry, and the term reads its neutral rather than a
plausible invented number. `price_value` came alive instead, because the caller's roster
asserted list prices the agents then bid under.

Compare the `trust` column with the one in the table above: `0.172`, `0.162`, `0.156`, `0.148`,
`0.132`, digit for digit, on a different request minutes later. That is what a constant looks
like from the outside, and it is the cheapest way to check the claim in the previous section.

This is worth understanding before you read any score: **the exchange is honest about not
knowing.** A feature it cannot compute is *removed from the candidate*, not written as 0.0.
Writing 0.0 for "we have not checked" would punish every store the verifier has not reached
yet, which is a systematic bias in favour of whoever was crawled first.

---

# Running it

Everything below ran on one laptop. No Shopify account, no development store, no payment
gateway, no LLM key required.

Two conventions that will bite you if you skip them:

- **Use `./.venv/bin/python`, never bare `python`.** The virtualenv lives inside the checkout
  and is not activated for you.
- **`PROXYSHOP_WORKER` isolates a run.** Anything touching a datastore gets its own Postgres
  database, `proxyshop_w$N`. `scripts/verify.sh` exits 2 immediately if it is unset. Any number
  works except **0, which is reserved for the project's scored measurement run**.

## 1. Setup, and a full run with no datastore and no docker

```sh
make bootstrap
./.venv/bin/python -m proxyshop_demo
```

Measured on this checkout: `make bootstrap` ran `uv sync --locked` and `npm ci`, asserted the
venv is the one under this directory at Python 3.12.13, and printed `OK: bootstrap`. The
narrated demo then printed **527 lines and exited 0 in about three seconds**, with
`PROXYSHOP_WORKER` unset and Postgres, Neo4j and Redis irrelevant to it.

It starts eight uvicorn servers on ephemeral loopback ports, all as daemon threads in **one OS
process** — trust, buyer, exchange, two store agents, and three the merchant beat adds (a
Shopify stub, a pixel collector, a webhook receiver). There is no `subprocess`, `fork` or
`multiprocessing` anywhere in it. A ninth port is bound and closed deliberately, so the silent
store hands the exchange a genuine connection-refused rather than a simulated one.

It walks one purchase through four sellers cast in four roles — two that bid, one that stays
silent, one that is blacklisted — and ends by reading the audit trail back off a **different
application** over HTTP:

```
  The chain the trust service holds (17 event(s)):
      - seq 1   auction_opened  prev 000000000000.. -> self 06f9b30536cb..
      ...
      - seq 17  order_paid      prev 1b0892dfeeae.. -> self 8572138cb6a5..

  chain intact  True    events verified  17    anchor agrees  True
```

Seventeen events in eight kinds, written by two applications, served back as a verified hash
chain. It closes by counting what it did and then naming what it could not — a gap list
measured on the spot, currently one entry (see [What is not built](#what-is-not-built)).

## 2. The whole deployment, in containers

The runbook is [`docs/demo/shopper-demo.md`](docs/demo/shopper-demo.md). The short version:

```sh
cp .env.example .env
make deps-up        # Postgres + Neo4j + Redis, then this worker's database and its migrations
make demo-corpus    # replay ten recorded storefronts into the Neo4j catalogue graph
make demo-up        # ten named services, plus the datastores they depend on
make demo-check     # drive a real auction over HTTP and fail loudly if the market is dead
```

Measured: `docker compose --env-file .env.example --profile demo config` resolves cleanly (exit
0), and every variable `docker-compose.yml` interpolates without a default is defined in
`.env.example`. The `cp` itself was not re-run when this was checked — a live `.env` was
already in place and overwriting it would have taken a running stack down — so what is claimed
here is that the example file is *sufficient*, which is the checkable half.

**Sufficient is not the same as identical to the stack this page was written on, and the
difference is the store voices.** `.env.example` sets `LLM_PROVIDER=double` and leaves
`ANTHROPIC_API_KEY=` empty. That is the designed default and not a mistake — the offline double
opens no socket, and the whole test suite runs that way — but on a stack brought up from that
file every store's pitch is the deterministic slot-filled fallback, not the model prose quoted
in the worked example above. `docs/demo/shopper-demo.md` says the same thing in its
prerequisites: no LLM API key required. To reproduce the store voices, set `LLM_PROVIDER=anthropic`
and a real `ANTHROPIC_API_KEY` in `.env` before `make demo-up`; the buyer service and the store
agent both ship the SDK and both images forward those two variables. What that buys and what it
still does not prove is in [About the model, precisely](#about-the-model-precisely).

`make demo-corpus` is the slow one and the one worth understanding. It replays
`fixtures/real-catalogs/` — **3,093 products across ten stores, 44,803 graph operations** — through
the same crawl, upsert and embed path a live crawl takes. Whole catalogues, not a category
sample: two of the ten stores carry no liver-support inventory at all and answer a milk-thistle
query with protein stacks. They are there deliberately. A graph in which everything matches
proves nothing about matching.

`make demo-check` is the step the container healthchecks cannot be. It opens an auction with no
`roster` key, which is the one request only the catalogue graph can answer. Measured after a
fresh corpus load:

```
roster_source: source='neo4j' shops=7 products_considered=25 reason=None elapsed_ms=25.061
entries=7  ranked=7  shortlist slots=4
hosted bids=4  fallback reasons=['no_response']

OK: the graph found the candidate set, stores were collected, and the shortlist is non-empty.
```

**What that probe does not cover, and it is the case a real shopper produces.** `demo_check.sh`
sends one body with `"hard_constraints": []` hardcoded; `DEMO_QUERY` changes the words and
nothing changes the constraints. So `hosted bids=4` is a statement about an *unpriced* query
only. A priced one — a shopper saying "under $50" — takes a different path, and that path was
broken until commit `2a06730`: the buyer service emits the constraint under `price_usd`, the
store agent looked the catalogue up under that spelling, found nothing where the catalogue holds
the number under `list_price`, and applied R19's "unverified data cannot satisfy a hard
constraint" — so every store declined `no_matching_product`, hosted bids went to **0**, every
shortlist slot fell back with no store voice, and every container reported healthy throughout.
The repair folds the spellings through `contracts.ranking.canonical_field` in
`packages/store-agent/src/runtime/bidding.py`; it is in this tree, and it was in all four hosted
agent images — that file inside each container hashed identical to the source, which is the check
to re-run after a rebuild rather than a fact to take on trust. Re-measured with `hard_constraints:
[{"field": "price_usd", "op": "lte", "value": 50.0}]`: **`sponsored: 4`**, no price-related
decline from anyone, and `gaiaherbs.com` came back at **24.2155** against its 25.49 list price,
which is the discount the constraint was supposed to provoke.

The gap that is still open is the probe's: nothing in this repository drives a priced auction, so
if that regresses again, `make demo-check` will go green over it.

### Two hazards, both of which bit this page while it was being written

**Do not run the Python test suite against the Neo4j the demo is using.** The graph tests open
with `MATCH (n) DETACH DELETE n` and do not ask whether a demo is loaded. Measured here while
this page was being written: a graph holding **3,093 products was down to 2** roughly ten
minutes later, with every container still reporting `(healthy)`, the auction answering
`shops=0`, and `make demo-check` naming the failure. There is no per-worker Neo4j database;
`PROXYSHOP_WORKER` isolates Postgres and Redis only.

**A re-run of `make demo-corpus` may not repair it.** The loader is idempotent by *content
hash*, not by graph state, so after a wipe it decides eight of the ten stores are unchanged and
writes nothing back for them. Measured: a clean exit 0, a summary line reading
`TOTAL products=3093`, and a graph holding 199. **The loader's exit code is not a statement
about the graph** — a later `--force` run on this same stack also exited 0 while a concurrent
test wiped it underneath, printing `nodes {}`. **Read the `graph:` block, not the `TOTAL`
line, and not the exit code** — the block counts what is in Neo4j and the total counts what was
read off disk. The repair for a stale hash ledger is `--force`:

```sh
docker compose --profile corpus run --rm corpus-loader \
  python -m ingest.scheduler.load_corpus --all --no-lock --force \
  --probe "milk thistle silymarin liver support extract"
```

A green container list is not a working demo. Drive `make demo-check` after every deploy.

## 3. The shopper page, and the learning demo

`http://localhost:8080` on the compose stack is the shopper journey.

**It does not use the catalogue graph, and you should know that before you read a shortlist off
it.** The SPA sends no roster, so `POST /buyer/intent/confirm` falls through to the buyer
service's `BUYER_ROSTER`, which `apps/buyer/compose.yaml` defaults to
`/srv/deploy/buyer-roster.json` — a fixed six rows, one `product_ref` and one `list_price` each,
the same six whatever the shopper types. That is the roster path described under
[Now change one thing](#now-change-one-thing): `intent_match` reads its neutral 0.175 for every
store, so what separates two stores on that page is the frozen `trust` constant and whatever
discount their agents bid. The graph path — the one at the top of this page, where retrieval
actually picks the shops — is reached by `make demo-check` and by any `POST /auctions` with no
`roster` key, not by the browser. `roster_source` on the response says which happened every time.

Two other pages sit on the same origin behind a hash route, so they work on a static host and on
the launcher alike:

- **`#/metrics`** — every number on it comes off a served route and the page names which one.
  It deliberately has no latency chart, no request rate and no uptime, because no service in
  this stack records any of them, and a chart of numbers nobody produces is the exact defect
  this page exists to help find. Where a source exists but this origin cannot reach it, the
  page says so by name, with the reason.
- **`#/learning`** — *"Watch the network learn."* This one is worth the demo time.

The learning page runs a query, shows what the market answered, feeds real outcomes back
through the two doors that actually carry them — `POST /buyer/shortlist/accept` (a purchase,
which the exchange folds into its bandit: a win for the accepted store, a loss for every other
store the same shortlist showed) and `POST /buyer/feedback` (sealed into the trust ledger's
hash chain, after which the trust service pushes the delta to that store's agent over
`POST /v1/trust-events`) — and then **asks the same query again**.

That push is best-effort and reports its own losses, which is worth knowing before you read a
second reading as a null result. `GET /events/verify` carries a `store_agent_notifications`
block; measured on the running stack it reads `delivered: 528, lost: 6, retried: 6,
pending_retry: 0, dropped: 0`, with every loss `ReadTimeout: timed out; given up after 4
attempt(s)`. Six deltas never reached the agent they were addressed to.

The second reading is a second real auction with a real solicitation in between. Nothing on the
page stores a "before" and re-renders it as an "after"; `compare()` in
`apps/buyer/app/learning/loop.ts` is handed two readings and cannot tell where either came
from. If the market did not move, the page says the market did not move.

It also says, on screen, that its orders are manufactured, what marks them (`sim-fb-`, copied
verbatim onto the sealed ledger event, so a seeded answer cannot later be mistaken for an
earned one), what that mark does *not* prove, and which of the five ranking terms can move in
this deployment and which cannot.

There is also a browser demo that needs no datastore at all —
`PROXYSHOP_WORKER=1 npm run demo --workspace @proxyshop/buyer` — which builds the SPA and boots
five ASGI apps in one process. Sign in before typing anything into the shopping box: there is
no password, the launcher prints a single-use magic link into the terminal, and redeeming it
reloads the page, so a conversation started first is gone when you come back. That console
transport is a **local-development transport** — anyone who can read the process's stdout can
sign in as whoever asked for a link — and it has to be named out loud, because a deployment
that merely forgets to configure mail refuses every login with `503`.

---

# How it works

## The services

Every service is a FastAPI app imported as `<namespace>.main:create_app` unless noted; the port
is what its Dockerfile CMD binds. **The six product services serve 57 routes between them, and
every one of the 57 is driven by at least one test** — measured by
`./.venv/bin/python -m proxyshop_support.route_census`, which also checks each service against
its published OpenAPI contract and currently reports no drift in either direction. Run it rather
than believing that sentence; it is the fastest way to find a route somebody built and nobody
reached.

| Path | Port | Routes | What it owns |
|---|---|---|---|
| `apps/buyer` | 8081 | 16 | The Vite/React SPA (`@proxyshop/buyer`), the FastAPI buyer service, and `devstack/run.py`. Clarify, confirm, read an auction, render and accept a shortlist, magic-link auth and session, profile, feedback and its prompt, a live-check pair, and the store-visible window. |
| `apps/exchange` | 8083 | 7 | The auction, the ranker, the checkout port. `POST /auctions`, `GET /auctions/{id}`, `GET /auctions/{id}/shortlist`, `POST /auctions/{id}/accept`, the external Tier-2 bid door `POST /v1/auctions/{id}/bids`, `POST /internal/outcomes`, `GET /reports/losses`. |
| `apps/merchant` | 8082 | 13 | Install and OAuth callback, the Shopify webhook door, the pixel collector, code minting, envelope read/write, the kill switch, and the merchant console at `/dashboard`. |
| `apps/trust` | 8084 | 10 | The hash-chained append-only ledger, plus scoring, reconciliation and snapshots. Exactly the ten `trust.openapi.json` declares. |
| `services/ingest` | 8085 | 9 | Storefront adapters, LLM extraction, entity resolution, the Neo4j catalogue graph, the refresh scheduler. |
| `packages/store-agent` | 8086 | 2 | Despite living under `packages/`, this **is** a deployable — one process per shop, holding that shop's context file. `POST /v1/bid-requests` answers 200 with a Bid or 204 with a Decline; `POST /v1/trust-events` is how the network tells a shop what its score did. |
| `services/shopify-stub` | 8787 | — | A local implementation of exactly the Shopify surface this system uses: Admin GraphQL mutations, cart permalinks, checkout completion, webhook deliveries, the pixel surface. This is what stands in for a development store. |
| `deploy/buyer-web` | 8080 | — | Not Python. The nginx image that serves the built SPA **and proxies `/buyer/` to `buyer-svc`** — page and API must be one origin, because every SPA endpoint is a bare relative path and the buyer service installs no CORS middleware. This is the address a shopper opens. |
| `apps/seller-reference` | — | — | **Not a service.** Its Dockerfile CMD builds every adversarial persona and exits. The personas exercise the external bid door. |
| `services/sim` | — | — | **Not a server.** A headless CLI whose exit code is the finding. |

On the compose demo the store agents are one container per shop —
`store-agent-gaiaherbs`, `-toniiq`, `-paradiseherbs`, `-oregonswildharvest` on 8090–8093. The
exchange's deployment document is where organic and sponsored are actually decided: four of the
ten sellers carry a `bid_endpoint`, six do not.

Supporting directories: `packages/contracts` (dual Python + npm — the JSON-schema registry,
protocol models, JCS signing, OpenAPI helpers, codegen), `packages/llm` (the Anthropic client
wrapper, offline doubles, prompt recordings), `packages/verification` (claim comparators),
`proxyshop_support` (shared runtime helpers), `pixel` (the Shopify web pixel extension, whose
beacon is built from a four-key allowlist so no customer PII can enter it), `proxyshop_demo`,
`e2e`, `db`, `fixtures`, `deploy`.

Before you scale anything, read [`docs/deploy.md`](docs/deploy.md). `--workers 1` on the
exchange is a correctness pin, not tuning: raising it, adding `deploy.replicas`, or running
`--scale exchange=N` re-opens a double-spend that mints two live discount codes for one
purchase. Nothing shipped is broken — the pin holds it shut — and nothing else will tell you
that before you change it.

## One auction, end to end

1. **Clarify.** The buyer service asks **at most three** clarifying questions — a published
   ceiling, not a heuristic — and turns the answers into a structured intent: query, category,
   budget band, hard constraints, preferences. The clarifier never self-confirms.
2. **Confirm.** The shopper's separate act. Until it happens no auction exists. This is the step
   that leaves the buyer's origin, which is why it needs a signed-in session.
3. **Find the shops.** With `EXCHANGE_SHOP_ROSTER=graph` and a deployment document named by
   `EXCHANGE_DEPLOYMENT` — the two are one switch and neither alone does anything — a
   `POST /auctions` carrying no roster walks the catalogue graph for products that both match
   the intent and satisfy its hard constraints, and rosters their shops. A request carrying its
   own roster uses it and never touches the graph. `roster_source` says which happened, and
   `"unwired"` means the graph door was never opened.
4. **Gate, then solicit.** Eligibility is checked *before* anyone is asked, so a blacklisted
   store is denied without being contacted. Solicitation then fans out to every eligible store
   in parallel inside a **bounded window — 5.0 s by default, 10.0 s the hard ceiling a caller
   cannot raise**, since `bid_timeout_seconds` arrives on an unauthenticated body and the wait
   it buys is a worker parked. The window is a ceiling, not a fee: the fan-out returns the
   moment every store has answered. A store that misses it is still represented, at its
   catalogue list price, marked `fallback` — and a fallback asserts no claims, so it can never
   be the evidence that satisfies a hard constraint.
5. **Verify, filter, rank.** Every claim in every bid is graded against the platform's crawl.
   Hard constraints are eligibility filters and never score terms: a contradicted hard
   constraint cannot win however good the pitch is, and an ambiguous one cannot satisfy it
   either. What survives is ranked by the formula below.
6. **Shortlist.** Up to four differentiated slots — `fit`, `value`, `reliability`,
   `specialist` — each carrying the product, the price, the store's own commitments as published
   `Claim`s with provenance, the store's own message, a trust summary, and the platform's
   registered domain for that store. Slots collapse when there are fewer distinct stores.
7. **Accept.** Eligibility is re-checked. A single-use code is minted and a cart permalink built
   on the seller's **registered** domain — the platform's record of that host, never the host
   the bid claimed, compared for exact equality with no subdomain wildcards. A bid pointing at
   `checkout.<seller>` or `evil-<seller>` is refused here, *before* any code is created.
8. **Observe.** The checkout emits a browser web-pixel beacon and an HMAC-signed `orders/paid`
   webhook. The pixel is corroboration and may go missing; the webhook is evidence and carries
   the money. Both append to the same hash-chained ledger.

Every transition is appended to an append-only ledger **on a different service**, and each
event's `prev_hash` is the previous event's `event_hash`. `GET /events/verify` checks the links
*and* an anchor recorded outside the row list, so a stream truncated to a shorter but perfectly
linked prefix still fails — a flawless chain of the wrong length is still a tampered one.
The length is not a fact about this repository — it grows with every auction anyone runs. Three
readings off one stack inside fifteen minutes: **2155, 2215, 2219**. What is invariant, and what
is worth checking, is that `verified`, `length` and `expected_length` are the same number, that
`head_hash`, `expected_head` and `stream_hash` are the same hash, that `broken_at` is `null` and
`anchor_ok` is `true`. All of that held at each reading.

## The ranking formula

There is exactly one, it is published, and it is fee-blind and tier-blind
(`packages/contracts/src/ranking.py`):

```
rank_score = 0.35·intent_match + 0.20·verified_claim_ratio + 0.20·trust
           + 0.15·price_value  + 0.10·delivery_fit  −  policy_penalties
```

Weights version `1.0.0`, feature-definition version `3.0.0`. Versioned separately because they
change independently and a replay needs both.

| Term | What it reads |
|---|---|
| `intent_match` | The **platform's own retrieval measurement** for this store, handed in by the caller that ran the retrieval. The exchange cannot compute it — a served auction handed a roster queries no index — so on the roster path it is absent and reads its neutral. Anything not a finite number is treated as absent rather than coerced. |
| `verified_claim_ratio` | `1 − Π(1 − gain)` over the claims the exchange graded `verified` **whose subject this buyer asked about**, weighted by how hard they asked. Claim-stuffing pays exactly nothing: a verified claim about something nobody asked contributes 0.0, worth precisely what silence is worth. This is the term that pays for a per-shopper pitch, and the only one both movable at bid time and dependent on this buyer. |
| `trust` | The store's own posterior over six dimensions: `price_honored`, `discount_honored`, `shipped_on_time`, `not_returned`, `feedback_match`, `catalog_claim_accuracy`. Verification outcomes and transaction outcomes update the same Betas, so a store has trust signal before it has ever sold anything. **On the demo deployment the exchange reads none of that**: the deployment document states a `trust_snapshot` and the composition root leaves it alone, so this term is a frozen per-store number. See [How they ranked](#one-query-end-to-end). |
| `price_value` | The discount term. It **saturates at the auction's own price band** — no marginal return past clearing it. This is the mechanical reason winning on price wins one slot of four rather than the shortlist. |
| `delivery_fit` | **Not the store's promise.** A quoted dispatch time is divided by the store's own `shipped_on_time` posterior before the auction normalises it, and a store with too little shipping history is **not admitted to the term at all** — the feature reads absent, which scores the neutral 0.5. A store with no record can neither win this term nor be punished by it. A promise costs nothing to make; this is the correction for having paid one out before anyone found out whether it was kept. |

Three properties are enforced in the contract rather than left to the ranker. The five weights
**must** sum to 1.0 — a set that does not is invalid, not silently renormalised, because
renormalising means the published weights and the applied weights are different numbers and
nobody can tell which produced a given shortlist. **Missing features read their published
neutral, not zero.** And `policy_penalties` is bounded, so one repeated event kind cannot drive
a score arbitrarily negative and stop the formula being a comparison.

The one asymmetric downside is `contradicted_claim`, at 0.15. That is 1.5× the most the entire
evidence term can pay a store, so no amount of true buyer-relevant evidence buys out one lie;
and it equals `w_v`, the whole weight of `price_value`, so one contradicted claim costs exactly
what clearing the price band outright is worth. Without it, a false claim costs one auction's
share of one ratio, and a persuasion market is a lying market.

### What actually moves a score

Classify each term by whether a store can move it at bid time and whether it depends on the
shopper:

| term | weight | movable at bid time | buyer-dependent |
|---|---|---|---|
| `intent_match` | 0.35 | no (catalogue) | yes |
| `verified_claim_ratio` | 0.20 | **yes** | **yes** |
| `trust` | 0.20 | no (record) | no |
| `price_value` | 0.15 | yes | no |
| `delivery_fit` | 0.10 | only as far as its record | no |

Only one cell is both. That is deliberate and it is the load-bearing part of the design: if the
movable-and-buyer-dependent cell were empty, a store's best pitch would be identical for every
buyer, customization would return exactly zero, and the store agents' learning loop would
measure that correctly and converge every shop onto one generic pitch.

That table is about the formula. **Which of those terms is actually live in the demo deployment
is a shorter list**, and the two should not be confused. `trust` is a per-store constant typed
into the deployment document, so it separates stores but never changes. `intent_match` is a real
measurement only on the graph path; on the roster path — which is the path the shopper page takes,
see [The shopper page](#3-the-shopper-page-and-the-learning-demo) — it reads its neutral for
everybody. `delivery_fit` reads its neutral for a store with too little shipping history, which
is every store here. That leaves `price_value` and `verified_claim_ratio` as the two terms a
store can move during a demo, and `verified_claim_ratio` only moves if a verified claim lands on
something this shopper actually asked about.

## How a shop cannot buy rank

Two mechanisms, both of which fail the build rather than warn:

- **Import-lint.** `C3/S7: exchange must never import sealed-state or envelope modules` — run
  `make lint` and read it: `Contracts: 2 kept, 0 broken` over 431 files.
- **Postgres grants.** `apps/exchange/tests/test_scaffold_datastores.py` holds
  `test_d5_exchange_cannot_read_sealed_or_vault` and
  `test_d5_exchange_sees_zero_sealed_tables_in_the_catalog`. The exchange's role has no table
  grant into `sealed` and the schema is not even in its catalog.

A store's tier, its fee, its `max_discount_pct` and its envelope's commitments are on objects
the feature module is handed and never reads. So is the trust snapshot's aggregate `score`,
which is `trust`'s own term and is read one layer up.

---

# What is not built

This section is the credibility argument, so it is written to be checkable and it errs toward
saying more. The durable instruction is at the end: **believe the run, not the prose.**

## An organic shop nobody asked is reported silent

Everything else on this list is something the system does not do yet. This is the one item where
it tells a shopper something untrue, which is why it is first. An organic shop — one the exchange
holds no `bid_endpoint` for and therefore deliberately never contacts — is disclosed to the
shopper as a shop that was asked and stayed quiet.

The chain is short and every link is deliberate on its own. `HttpBidSolicitor.solicit`
(`apps/exchange/src/composition.py`) returns `None` for a store with no endpoint rather than
opening a socket — "represented at list price rather than asked a question nobody is home to
hear". `collect_bids` (`apps/exchange/src/auction/collect.py`) turns a `None` answer into
`no_response` for any store whose roster tier is above 0, and mints the correct label,
`tier_0_no_agent`, only at tier 0. No store the graph rosters is ever tier 0: the corpus writes
`tier` explicitly and the demo's Store nodes carry 1, and where a node states none the roster
query supplies `coalesce(s.tier, 2)` (`services/ingest/src/graph/query.py`). So
`tier_0_no_agent` is unreachable on the served path, and `no_response`'s own definition in
`collect.py` — "asked, and nothing ever came back at all" — is not what happened.

What the shopper then reads, from `ShortlistView.tsx`, is: *"This price is Proxyshop's, not this
shop's. The shop did not answer this auction, so the exchange stood in for it at the list price
on its own roster row."* The first sentence is true. The second is about a solicitation that was
never sent. The auction's `market.solicited` count has the same problem — it counts stores the
eligibility gate cleared to ask, not stores that were asked.

Nothing here loses a shop its slot or misstates a price, and an organic result is *supposed* to
be carried at list price with no store voice; that part works. What is wrong is the account of
why the voice is missing.

## Reconciliation returns no verdict over the chain the narrated demo writes

`trust.reconcile.reconcile` joins the accepted offer, the browser beacon and the webhook into a
`reconciled` verdict, and that verdict is what moves a store's trust score — the same snapshot
the ranker reads. Both halves exist, are tested, and are wired: `POST /reconcile` is a served
route and the whole event trio reaches one chain. **The fold still returns 0 verdicts**, for one
reason the demo measures rather than asserts:

Reconciliation namespaces every join key by the store that owns it — it has to; a Shopify
`order_id` is a per-shop number and two shops both have order 1001. The `accepted` event does
carry a `store_id`, but the signed `order_paid` names `proxyshop-demo.myshopify.com`, because an
unsigned `X-Shopify-Shop-Domain` header is the only shop identity a signed delivery carries at
all. The mapping back is built — `trust.reconcile.routes.resolve_store_aliases` reads the
platform's own seller roster — but that roster is Postgres, and the narrated demo starts none.
The demo prints a **probe** beside the real answer: supply the two names and the same events,
and the same fold produces one real verdict, price honoured, `389.0 / 389.0`. The product's
number is the first line: **0**.

(A second, smaller thing: that driver posts no beacon, because it runs no merchant service. That
costs *evidence* rather than the verdict — a group with no beacon grades `pixel_missing`, which
by design is not a blocker. The producer is real: `POST /pixel/collect` writes a
`checkout_pixel` row to the chained ledger and `e2e/support/s1/flow.py` drives that served
route.)

## The Shopify web pixel needs things this repo cannot contain

The **repo's half is live and driveable**: the extension source in `pixel/`, the collector route
`POST /pixel/collect` on the merchant service, the four-key allowlist that keeps customer PII
out of the beacon, and the ledger write. `apps/merchant/svc/tests/test_collector.py` runs the
real extension source under `node` and POSTs over a real loopback socket into the real
`create_app()` route.

What is missing is everything outside the repository: a Shopify **Partner account**, a
`shopify app deploy` to register the extension, `SHOPIFY_API_KEY` and `SHOPIFY_API_SECRET` (the
OAuth install and every webhook HMAC are keyed by the secret), and a **publicly reachable**
`MERCHANT_APP_URL` — the built-in default `https://merchant.proxyshop.example` is deliberately
unresolvable.

`make e2e-live` is the preflight that says so, and it **always exits non-zero**: exit 2 means a
live precondition is unmet, exit 3 means every precondition holds and the live driver is
missing. Measured on this checkout it exits **2**, naming four of five:

```
LIVE-1 MISSING SHOPIFY_API_KEY / SHOPIFY_API_SECRET are set (key=unset, secret=unset)
LIVE-2 MISSING MERCHANT_APP_URL is a reachable public origin
LIVE-3 OK      SHOPIFY_STUB_URL is unset
LIVE-4 MISSING CHECKOUT_MODE='redirect' -> SimulatedRedirectProvider
LIVE-5 MISSING EMBEDDING_PROVIDER='local_bge' cannot embed: torch is not installed
NOTE  the extension runbook has NOT landed: docs/demo/shopify-onboarding-extension.md (T-087)
```

## The ledger can reconstruct an auction; nothing serves that reconstruction

The exchange writes an outcome block carrying the roster in order, who was solicited, who
answered, every fallback reason, the eligibility denials with their conditions, the ranker's
exclusions with grounds, the per-bid policy penalties with their clamped total, which slots were
filled by whom, and the acceptance. The reader that reconstructs an auction from it exists
**only in a test** (`apps/exchange/tests/test_auction_ledger_reconstruction.py`). There is no
served route; `GET /auctions/{id}` returns in-memory machine state and 404s once the auction is
let go.

## Seeded rather than live

- **The merchant console is real and mostly empty.** The SPA is at `/dashboard/` on
  `merchant-svc` — public shell, measured 200, with `/dashboard` (no trailing slash) a 307 to
  it; the data route
  `GET /stores/{store_id}/dashboard` is behind the `MERCHANT_ADMIN_TOKEN` bearer and answers
  `401 {"error":"unauthorized"}` without it. Six panels, each with its own `state`, so one
  unconfigured collaborator is a sentence rather than a blank page. On a cold service four of
  six read `not_configured` and name the variables they want, because every panel is downstream
  of a shopper who does not exist yet. All of its state is in-memory per process. A populated
  page exists as committed bytes in `deploy/demo-seed/merchant-dashboard/`, and
  `deploy/demo-seed/README.md` opens *"Everything in this directory was manufactured. No
  merchant has ever looked at this page, and not one auction it reports was ever run."*
  **No served route reads it back** — one test uses it as a prose corpus and that is all — so
  opening the console shows you that stack, not the seeded page.
- **The buyer page's feedback step is seeded and says so.** It is gated on an order reference
  carrying a `sim-fb-` marker, and submitting it hits the genuine `POST /buyer/feedback`, which
  can and does refuse it honestly. There is **no boolean that flips feedback from seeded to
  live**: going live means real traffic at the same door, and the two populations are told apart
  afterwards by the marker, which is copied verbatim onto the sealed ledger event — so it is
  inside the hash chain and cannot be added or removed without breaking `GET /events/verify`.
- **Half the store-visible window is a manufactured corpus.** `GET /buyer/store-window` serves
  the pseudonym, coarsened buckets and provenance a store is allowed to see. Rows are marked
  `seed` or real by a database constraint, and going live changes no serving code — deleting the
  seed rows leaves it serving only real logins. `read_store_window` answers **503** when no
  token file is configured and **401** for a bad credential, and it keeps those two apart on
  purpose — "nobody can be let in here" is not the same answer as "you are not who you say".
  The compose stack configures one by default (`apps/buyer/compose.yaml` points
  `PROXYSHOP_BUYER_STORE_WINDOW_TOKENS` at `/srv/deploy/buyer-store-window-tokens.json`), so on
  the demo an unauthenticated request gets the 401, not the 503.

## About the model, precisely

With `LLM_PROVIDER` unset, and with the `LLM_PROVIDER=double` that `.env.example` ships, the
provider is an offline deterministic double. That is the designed default rather than a failure,
and the narrated demo says so at boot in one line — including what a shopper actually reads
under it, which is the part worth quoting:

```
llm[store_agent] TEMPLATED: provider=double model=claude-sonnet-5 sdk=installed api_key=absent;
LLM_PROVIDER is 'double' (D20's default), so the offline double answers and the deterministic
slot-filled fallback is what a shopper reads. Set LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY
for the model's own voice
```

**The compose stack this page was written against was configured otherwise, and it is worth
being exact about what that proves.** Measured: `LLM_PROVIDER=anthropic` with a key present,
every store agent booting
`LIVE: provider=anthropic model=claude-sonnet-5 sdk=installed api_key=present`, and the
per-solicitation line reading `outcome=ok pitch_call=answered` with `elapsed` between 1.4 s and
4.5 s against a 4.6 s budget. So a real model is being called and is answering inside the window.

What that line does **not** prove is that the model's bytes are the bytes on the wire. It used to
claim exactly that: the route's table mapped the outcome `ok` to `source=model`, while
`compose_pitch` calls `screen(llm.complete(...))` and, when `screen` returns `None` on a content
rule, falls through to `screen(fallback_pitch(material), material)` without the outcome changing.
So a bid could carry the deterministic template under a log line that said `source=model`. Caught
on a running deployment: `paradiseherbs.com` logged `pitch budget=4.618s elapsed=2.724s
outcome=ok source=model` — the model answered in 2.7 s of a 4.6 s budget — while the message the
buyer received was verbatim `fallback_pitch` output, *"Free returns: 30 return window. Also: in
stock: yes; units left: 12."* Three of eight hosted slots across two auctions did this.

The label is fixed: it is `pitch_call=` now and its words are about the CALL — `answered`,
`budget_missed`, `errored`, `not_called`, `unarmed` — none of which names a provenance, with a
test asserting none ever does. `answered` means the model replied in time and **not** that its
words were used. The provenance itself is still not reported, and that is a real limit rather
than an oversight: the screening happens inside a pure function whose only return value is the
string, so the route never sees which of the two it got. Reporting it honestly means changing
`compose_pitch` to say which one it returned.

**Which of the two a bid carries is not visible from outside the process**, and neither is why.
`screen_reasons` returns the rules that fired and its docstring says it is separated from
`screen` so the refusals are inspectable — but its only non-test caller anywhere in the tree is
`screen` itself, which discards the tuple (`if screen_reasons(text, material): return None`). No
served route, response field or log line carries a reason, so an operator watching a store whose
pitches keep falling back cannot learn which of the eight rules is biting. Contrast the platform's
half of the same mechanism, which publishes `platform_case_source` per slot: it says *which*,
though not *why*. Both are real gaps in the observability rather than hedges in this paragraph.

Two things are unconditional either way. The **offer** is computed with no model call at all —
`store_agent.runtime.pitch` records 12.7 ms median (n=20) in process for it, against 3.35–5.04 s
for the pitch. And a failed, slow or unkeyed model never costs a store its bid: `compose_pitch`
cannot raise, and every path out of it that is not the model's reply lands on the deterministic
fallback.

## The page keeps its own gap list

The shopper page renders a **"What is not wired yet"** panel
(`apps/buyer/app/journey/Journey.tsx`). Read the page rather than this paragraph — it is
measured on the stack in front of you and prose is not. It currently carries **two** items:
`gap-feedback-seeded`, and `gap-model` (the page cannot tell you which client wrote the
clarifying questions, because no served response field says).

Five bullets that used to be on that list have retired, in two batches, and the file keeps the
record of both because a gap that closes quietly is indistinguishable from a gap that was talked
away. `gap-price` and `gap-domain` went first: a slot now carries a `price` and a `store_domain`
read off the platform's registry. Then `gap-fallback`, `gap-product-name` and `gap-store-voice`
together, because they were one gap wearing three faces — `ShortlistSlot` was `extra="forbid"`
and declared no field for whose price it was, what the thing was called, or what the shop had
said. All three fields now exist, and the `extra="forbid"` survived it: nothing was loosened to
make room.

## Where the rest of the known defects are written down

The strict xfails are this repository's open-defect register, each carrying its reason in its
own decorator. **Run the grep rather than trusting a count in prose:**

```sh
grep -rn "^@pytest.mark.xfail" --include='*.py' apps packages services proxyshop_support
```

Measured now: **six markers in five files**, all `strict=True`. Four are unconditional and fire
on an ordinary run; three of those name a ticket — T-325 (`apps/exchange/tests/
test_accept_denials.py`), T-158 (`apps/exchange/tests/test_repro_open_tickets.py`) and T-262
(`proxyshop_support/tests/test_repro_ticket_graph.py`). The fourth,
`packages/verification/tests/test_stock_fact.py`, names no ticket; its reason opens *"OPEN, and
deliberately not closed here"* and explains that the lever it registers — a seller's own
`observed_at` stamp turning `contradicted` into `unsupported` — is closed one layer up, because
`exchange.ranking.verification` strips `provenance` off every claim before `verify` sees it. A
register entry for something unreachable, kept so it cannot be forgotten.

The remaining two, both in `services/ingest/tests/test_embedding_ranking_gate.py`, are
conditional on the embedding provider being `hash`; the default is `lexical`, so on an ordinary
run they do not fire. They guard a provider choice worth knowing: driven through the real Neo4j
1024-dimension cosine index, `hash` answers a "running shoes" query with a coffee grinder, a
sunscreen and an espresso machine, and `lexical` returns the three running shoes in order.

Some of what those markers name is larger than anything above. Read them before assuming a
behaviour works.

## And do not trust this list either

Where prose and a run disagree, believe the run. A register of stale text is stale by
construction, because the thing that makes an entry wrong — somebody fixing the code — is
exactly the thing that does not update the register. **Roughly a third of the apparent defects
found in this repository over a two-cycle audit were prose asserting a defect that had already
been fixed** — including, on this page, a paragraph that called the seller's own voice "the
largest open gap against the product thesis" for some time after `ShortlistSlot.message` had
started carrying it. Treat a present-tense claim in any comment, docstring, runbook or README
bullet as a claim to re-check. If a behaviour is not demonstrated by a demo or by the suite,
treat it as unbuilt.

---

# Working on it

```sh
PROXYSHOP_WORKER=1 make verify
```

The `types` stage was failing when this section was written, on two `arg-type` errors in
`apps/buyer/svc/src/auctions/routes.py`; both are fixed and the stage is green again — measured,
`Success: no issues found in 410 source files`. Kept here rather than quietly deleted because the
point of the stage table below is that it reports what the command actually does, including when
that is "it fails". `types` runs early, and everything after it is gated behind it.

`make verify` is `scripts/verify.sh all`, and it exits 2 before doing anything if
`PROXYSHOP_WORKER` is unset. Datastores come up separately with `make deps-up` (Postgres, Neo4j
and Redis via docker compose, then the database **and its migrations**) and go down with
`make deps-down`, which destroys the volumes.

`verify.sh all` runs, in order: `ruff check` and `ruff format --check`, `lint-imports`, a grep
over every tracked file for a banned global Redis flush, then `eslint`; `mypy` and `tsc -b`; the
frozen acceptance suite, in a process of its own; `pytest -q -m "not needs_model"`; `vitest run`;
a datastore-coverage gate; and finally `./.venv/bin/python scripts/check_verify_contracts.py`.
The script runs under `set -euo pipefail` with each stage a top-level block, so the first failure
ends the run and everything after it is skipped. A full run takes several minutes.

Three things the gate does deliberately and loudly. **Collecting zero tests is a failure, not a
pass** — pytest's exit 5 is fatal here, including when everything was deselected. The deselected
count is printed on its own line every time, so "passed" can never be mistaken for "ran". And
**`all` fails outright if Postgres, Neo4j or Redis is unreachable**, rather than letting the
docker-marked tests skip: with the stack down the live least-privilege checks for C3/S7 report
`skipped`, the run reports `passed`, and nothing has been proven about database grants. There is
no opt-out for `all`; `PROXYSHOP_ALLOW_DEGRADED=1` lets `check` and `pytest` finish and makes them
report DEGRADED instead of OK.

**Measured here, stage by stage, on `PROXYSHOP_WORKER=8`:**

| stage | result |
|---|---|
| `verify.sh lint` | `OK: lint` — import-linter `Contracts: 2 kept, 0 broken` over 431 files, 1323 dependencies |
| `verify.sh types` | passes, exit 0: `Success: no issues found in 410 source files`. It reported two `arg-type` errors on the `AuctionView(...)` construction in `apps/buyer/svc/src/auctions/routes.py` a few hours before this was written — two `**{...}` splats, each carrying one homogeneous value type, matched against fields of different types. They are merged into one annotated mapping now. |
| `verify.sh vitest` | `OK: vitest` — `Test Files 30 passed (30)`, `Tests 1302 passed (1302)` |
| `scripts/check_verify_contracts.py` | **exit 0** — `OK: pytest-config, test-path-filter, schema-package, non-empty-test-dir, raw-Redis-client, unique-fixture-name, verify-runs-the-release-blockers and acceptance-runs-in-its-own-process contracts all hold` |
| `verify.sh pytest` | **not run here**, on purpose — the graph tests wipe the Neo4j the compose demo was serving from. Run it *before* `make demo-corpus`, not after. |

The contract check prints a note that two ticket verify paths do not exist yet (T-086
`e2e/test_onboarding.py`, T-087 `docs/tests/test_runbook.py`). It is a note, not the failure —
the check exits 0. That command takes under a second, so **check it rather than believing this
table**; it is exactly the kind of paragraph that goes stale the moment somebody moves a file.

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
| `make e2e-live` | The live development-store preflight. Needs `PROXYSHOP_WORKER` like everything else, and always exits non-zero; see above. |

Two more worth running directly:

```sh
./.venv/bin/python -m proxyshop_support.route_census   # every served route, driven or not, vs its contract
PROXYSHOP_WORKER=1 ./.venv/bin/python -m sim --json    # the dishonest-store simulation; its exit code is the finding
```

The simulation checks that the trust engine catches a scripted dishonest store whose behaviours
are defined in a human-approved fixture manifest rather than by the trust engine's own config —
which would make the result circular. Measured on this checkout: **exit 0**, `caught_at: 1`,
`chain_ok: true`.

## Where the rulings are

`.swarm-loop/decisions.md` holds the design decisions, each with the argument that produced it.
The ones that shaped the product as it stands are **D55** (two agents, two objective functions;
what a shop actually buys), **D56** (the default embedding provider, and the difference between
exercising an index and ranking a catalogue), **D57** (a delivery promise must be earned before
it counts), **D58** (the solicitation names the product) and **D59** (a stock reading is binary,
and it expires).
