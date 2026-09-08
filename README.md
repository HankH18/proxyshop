# ProxyShop

ProxyShop is a working prototype of an **agent-mediated shopping auction**, and it runs
entirely on your laptop.

A shopper says what they want in plain language. A buyer service asks at most three
clarifying questions and turns the answers into a structured intent — hard constraints plus
a budget band. The shopper confirms, and that opens an auction at an exchange. The exchange
first gates the store roster on eligibility, so a blacklisted store is denied *before* it is
asked for anything. It then solicits a sealed bid from each eligible store's own agent
inside a bounded window; a store that does not answer in time is still represented, at its
catalogue list price, marked `fallback`. The exchange verifies the claims in each bid,
filters out candidates that fail a hard constraint, ranks what is left with a published
weighted formula, and returns a shortlist of named slots — `fit`, `value`, `reliability` —
each carrying a fit score, a trust summary and provenance labels. The shopper accepts one
slot. The exchange re-checks eligibility, mints a single-use discount code and builds a cart
permalink on that seller's **registered** domain (exact host equality — no subdomain
wildcards). Following that permalink completes a checkout at a merchant, which emits a
browser web-pixel beacon and an HMAC-signed `orders/paid` webhook.

None of that touches the outside world. There is no Shopify account, no development store,
no payment gateway and no LLM API key involved. With `LLM_PROVIDER` unset, the buyer service
resolves `build_llm("buyer")` to a deterministic offline double — the running UI reports it
as `<DeterministicLLM role='buyer'>, model="double:buyer"`. That is the designed default,
not a fallback from a failure.

The rest of this file is mostly quoted output. Everything in a fenced block below came off a
real run on a clean checkout, and where the system is unfinished, the section on that says so
in the same detail.

---

## Setting up

```sh
make bootstrap
```

`scripts/bootstrap.sh` runs `uv sync --locked` and `npm ci --prefer-offline` **in this
checkout**, then asserts the result — that the venv it built is the one under this directory,
that the interpreter is 3.12, and that the flat import namespaces resolve outside pytest too.
The repo pins Python 3.12.13 (`.python-version`) and Node ≥ 22.12.0 (`package.json`
`engines`; `.nvmrc` says 25.9.0). Docker is needed only for the datastores the test suite
uses, not for either demo.

Two conventions that will bite you if you skip them:

**Use `./.venv/bin/python`, never bare `python` or `python3`.** The virtualenv lives inside
the checkout and is not activated for you. Every Python command in this file is written with
that prefix on purpose.

**`PROXYSHOP_WORKER` isolates a run.** Anything that touches a datastore gets its own
Postgres database, `proxyshop_w$N`, chosen by this variable; `scripts/verify.sh` exits 2
immediately if it is unset. Any number works — except that **worker 0 is reserved for the
project's scored measurement run and must not be used.** The examples below use `1`.

---

## Demo 1 — the narrated command-line run

```sh
./.venv/bin/python -m proxyshop_demo
```

This one needs only `make bootstrap` — no `PROXYSHOP_WORKER`, no datastore, no external
connection. (Measured: run with `PROXYSHOP_WORKER` unset and with Postgres, Redis and Neo4j
irrelevant, it still exits 0.) It prints 439 lines: a header, the roster, seven narrated beats,
and a closing summary that states both what ran and what did not.

It opens with this banner:

```
================================================================================
  ProxyShop — the S1 starting slice, live
  five ProxyShop deployables, one purchase, no Shopify account
================================================================================
```

**That second line counts ProxyShop deployables, and it counts them correctly.** It used to say
"four deployables, four loopback ports", which undercounted; the trust service was the one it
left out. Five ProxyShop services come up: two store agents (one per hosted store), the buyer
service, the exchange and the trust service. Measured, the driver starts **eight uvicorn
servers** in all, each on its own ephemeral loopback port, all as daemon threads inside **one
OS process** — those five, plus the three the merchant side of beat 6 adds and names where it
adds them: the Shopify stub, a pixel collector and a webhook receiver. There is no
`subprocess`, `Popen`, `multiprocessing` or `fork` anywhere in `proxyshop_demo/s1.py`; the
servers come from `proxyshop_support/asgi_server.py`'s `serve()`, which is
`uvicorn.Config(app, port=0)` on a `threading.Thread`. A ninth port is bound and then closed
deliberately, so that the silent store hands the exchange a genuine connection-refused rather
than a simulated one.

The roster of four sellers, each cast in a different role:

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

  Solicited (3): store-northroast, store-brightbean, store-slowreply
```

The bids come back over real HTTP, and the silent store is represented anyway:

```
      - store-northroast   bid            $389.00 unit / $389.00 total
          answered at: http://127.0.0.1:62346/v1/bid-requests
      - store-brightbean   bid            $449.00 unit / $449.00 total
          answered at: http://127.0.0.1:62347/v1/bid-requests
      - store-slowreply    did NOT bid    -> represented at list price $519.00
          fallback_reason: no_response
```

Both agents here are a cold start with no learned policy, so neither takes a discount off
list. Ranking shows its terms rather than just a number:

```
  Ranked, best first (3):
      - #1  store-northroast   score 0.5720
          delivery_fit=+0.050  intent_match=+0.175  price_value=+0.075  trust=+0.172  verified_claim_ratio=+0.100
```

and the shortlist the shopper is actually shown:

```
  The shortlist the shopper is shown (3 slot(s)):
      - slot 'fit'  store-northroast
          price        $389.00
          fit score    0.5720   (why it placed here)
          trust        score 0.86  available=True
          provenance   from their website, store-confirmed
```

Accepting a slot mints the code and the permalink:

```
  What the shopper walks away with:
      winning store              store-northroast
      discount code              PSX-JQE7CB30
      permalink                  https://store-northroast.example.com/cart/1:1?discount=PSX-JQE7CB30
```

The code is freshly minted on every run — `PSX-JQE7CB30` is from one run of mine, and you
will get a different one. Following that permalink is a real checkout against the local
Shopify stub:

```
  >> GET   http://127.0.0.1:62363/cart/1:1?discount=PSX-JQE7CB30   -> 303   cart
  >> POST  http://127.0.0.1:62363/_stub/checkouts/<token>/complete   -> 201   paid

  The order the merchant closed:
      order                      #1001
      total paid                 $389.00
      discount code redeemed     PSX-JQE7CB30
```

and the demo then proves single use by trying the same code again, and reporting exactly what
happened rather than asserting a property:

```
  And the code is single use. The same shopper offers it a second time:
      - the second checkout completed as order #1002 and carries NO discount (redeemed: nothing)
```

It closes by counting what it did, and then naming what it could not do:

```
  Ran, live, over HTTP:
      - the shopper was asked 3 clarifying question(s)
      - 1 store denied at the eligibility gate, before being asked
      - 3 stores solicited; 3 bid(s) ranked
      - 0 candidate(s) refused, each naming every reason
      - 3 shortlist slot(s) shown to the shopper
      - one single-use code minted: PSX-JQE7CB30
      - one permalink on the registered domain: https://store-northroast.example.com/cart/1:1?discount=PSX-JQE7CB30
      - one order closed at the merchant: #1001 for $389.00
      - one web pixel beacon read by the merchant app (order gid://shopify/Order/5500000000001)
      - one HMAC-signed webhook verified and read as order_paid (order gid://shopify/Order/5500000000001)
      - 8 ledger event(s) delivered to the trust service and served back as a VERIFIED hash chain (head ...)

  Did NOT run, and why (1):
      - reconciliation and the trust update cannot run over the ledger chain this run writes
```

That last block is not boilerplate. It is the demo's own gap list, and there is exactly one
entry in it. See [What is not finished](#what-is-not-finished).

---

## Demo 2 — the browser

```sh
PROXYSHOP_WORKER=1 npm run demo --workspace @proxyshop/buyer
```

That npm script is `npm run build:ui && ../../.venv/bin/python devstack/run.py`: it builds the
Vite/React SPA and then boots the stack. `npm run devstack --workspace @proxyshop/buyer` is the
same thing under a different name — both build first, because `apps/buyer/dist` is gitignored
and a stack booted without it serves the API and a "THE UI IS NOT BUILT" banner instead of the
page. (`npm run devstack:nobuild` is the one that skips the build, for API work.) The build
took 152 ms and 25 transformed modules on my run, and `http://127.0.0.1:8100/` answered
`HTTP/1.1 200 OK` within a second of the banner appearing.

This launcher runs **five servers in one process** — three store agents, the exchange and the
buyer app. Only the buyer app takes a fixed port; everything else binds port 0. `--no-open`
(pass it after `--`) skips opening a browser, and `--port N` or `BUYER_UI_PORT=N` moves the
page off 8100. Ctrl-C shuts the whole thing down. This demo needs no datastore either —
measured by running it at a `PROXYSHOP_WORKER` whose database was never created, which
still clarified, opened an auction and returned a two-slot shortlist.

The banner tells you where everything landed:

```
======================================================================================
  proxyshop devstack - the real backend, three real stores, one buyer app
======================================================================================

  STORE AGENTS (each is one store's advocate; port 0, D40)
    demo-woolworks       http://127.0.0.1:63067/v1/bid-requests   [demo-woolworks.example.com, trust 0.82]
    demo-alpine-supply   http://127.0.0.1:63068/v1/bid-requests   [demo-alpine-supply.example.com, trust 0.61]
    demo-fastfleece      http://127.0.0.1:63069/v1/bid-requests   [demo-fastfleece.example.com, BLACKLISTED]

  EXCHANGE   http://127.0.0.1:63070
  BUYER APP  http://127.0.0.1:8100      <- open this
```

…and then a **SIGN IN** block, which is the next section: the journey has a sign-in step and
this launcher is what makes it completable without a mail server.

### Sign in first — the confirm button does not exist until you do

**Do this before you type anything into the shopping box.** Step 2's **Confirm and ask
stores** button is not disabled while you are signed out; it is *absent from the page*,
because confirming is the step that leaves this origin — the exchange solicits real stores
and each of them is told a pseudonym minted by the buyer service's vault, which the browser
cannot mint. And redeeming a sign-in link reloads the page, so a conversation you started
first is gone when you come back.

There is no password. You type an address, the service issues a single-use link, and opening
it starts the session. On a workstation there is no mail server to send it through, so the
devstack sets `PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT=console` and the service **prints the
link into the terminal you launched it from** — no mailbox needs to exist and the address you
type does not have to be real:

1. type anything into **Email address** and press **Email me a link**
2. read the block that appears in the launcher's terminal
3. open the `http://127.0.0.1:8100/?token=…` URL it prints

```
======================================================================================
  MAGIC LINK (printed because this deployment asked for the console
  transport; this is a live single-use credential)

    to       demo-buyer@example.com
    open     http://127.0.0.1:8100/?token=K1xHjIKZMNAzrGTYJ_qz1PJPn1IFvnVnnkyD39ijCRA
    expires  2026-09-07T23:05:26.678704+00:00
======================================================================================
```

That token is a real bearer credential and the console is a **local-development transport**:
whoever can read the process's stdout can sign in as whoever asked for a link. It has to be
named exactly — a deployment that simply forgets to configure mail still refuses every login
with `503` and prints nothing, which is the behaviour every non-devstack deployment gets. See
`PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT` in `.env.example`.

The page comes back signed in, and tells you the only thing the stores will be told about
you — a fresh handle such as `psn-b8af82a121c2d6e27df53151513395ce`.

### Then type the scripted conversation exactly

The banner also prints two scripted conversations, and it is not being precious about them.
`cluster_id` is a hash over the clarified query, the budget band and the constraints, and a
store agent declines any cluster its envelope does not pursue — so **every extra answer you
type changes the hash, after which no store bids and you get an empty page for a reason that
is your own keystrokes.** The shortest of the two is a single line with no follow-up answers:

```
I want a warm merino wool beanie for winter, under $100
```

Type that, press Send, and step 2 comes back with the extraction (cluster
`cl-4d3c3e4edadaa5e7`):

```
Here is what we understood
What you asked for   I want a warm merino wool beanie for winter, under $100
Budget               100-250
Must have            material is merino-wool
                     price_usd at most 100
```

Press **Confirm and ask stores** — the button is there now — and step 3 is a real auction.
Three stores are solicited and two come back on the shortlist, one slot each. From a run of
this exact walkthrough on this branch:

| slot    | store                | unit / total | `fit_score` | trust | labels |
|---------|----------------------|--------------|-------------|-------|--------|
| `fit`   | `demo-woolworks`     | 78.00 / 78.00 USD | `0.57685` | 0.82 | from their website · store-confirmed · unverified |
| `value` | `demo-alpine-supply` | 72.00 / 72.00 USD | `0.53485` | 0.61 | from their website · store-confirmed · unverified |

Each slot carries the store's own commitment (`free returns — 30 days`,
`ships within — 2 business days`, both store-confirmed) and the platform's case for it,
written from the facts rather than from adjectives:

```
You said price was a must-have, and here it is: 78.00 USD. Also offer held until:
2026-09-07; reliability: 82%.
```

Three stores were asked and two are shown, because the third is excluded on the record. The
auction view gives both of its reasons for `demo-fastfleece` in full:

```
"blacklisted_store: 'demo-fastfleece' is blacklisted and may not participate (R12)"
"hard_constraint_unsatisfied: 'material': the candidate carries no such attribute, so the
 constraint is undecidable and does not count as satisfied (R19) — only a verified
 supporting claim satisfies a hard constraint (R19)"
```

Click **Accept this one** on the `fit` slot and the page hands off to the seller's own domain,
and says whose decision that was:

```
Your checkout is at demo-woolworks.example.com. We did not choose that address — the store's
exchange did.
```

The same journey driven over HTTP against the running stack gives the same thing in JSON —
`POST /buyer/shortlist/accept` answers 200 with
`"permalink_url": "https://demo-woolworks.example.com/cart/44352913:1?discount=PSX-ARNSGGXH"`.
The discount code and the auction id are minted per run, so yours will differ; the stores, the
prices and the exclusion reasons will not.

---

## What is in the repo

Every service is a FastAPI app imported as `<namespace>.main:create_app` unless noted; the
ports listed are what its Dockerfile's CMD binds.

### `apps/`

| Path | What it is |
|---|---|
| `apps/buyer` | Three things in one directory: the Vite/React SPA (`@proxyshop/buyer`), the FastAPI buyer service (`buyer_svc`, port 8081), and `devstack/run.py`, the launcher demo 2 uses. Routes: `POST /buyer/intent/clarify`, `POST /buyer/intent/confirm`, `GET /buyer/auctions/{auction_id}`, `POST /buyer/shortlist/render`, `POST /buyer/shortlist/accept`, `POST /buyer/feedback` and `/prompt`, plus the auth and profile routes. |
| `apps/exchange` | The auction itself (`exchange`, port 8083): `POST /auctions`, `GET /auctions/{id}`, `GET /auctions/{id}/shortlist`, `POST /auctions/{id}/accept`. |
| `apps/merchant` | A React-Router/Shopify-Polaris embedded admin app (`@proxyshop/merchant`) plus the FastAPI merchant service (`merchant_svc`, port 8082): `GET /install`, `/install/callback`, `/install/shops`, `POST /webhooks/shopify/{topic}`, `POST /pixel/collect`, `POST /codes`, `GET\|PUT /stores/{id}/envelope`, `POST /stores/{id}/kill`. |
| `apps/trust` | The hash-chained, append-only event ledger plus scoring, reconciliation and snapshots (`trust`, port 8084). It mounts four routers and serves ten operations: `POST\|GET /events`, `/events/head`, `/events/verify`, `/events/replay`, `/events/{id}`, `POST /claims/verifications`, `GET /snapshot`, and `GET\|POST /reconcile`. Those ten are exactly the ten `trust.openapi.json` declares — the drift both ways is closed. |
| `apps/seller-reference` | **Not a service.** Its whole tracked source is `src/__init__.py` and `src/personas/__init__.py` — no `main.py`, no `routes.py` — and its Dockerfile CMD is a one-shot self-check that builds every persona and exits. |

### `services/`

| Path | What it is |
|---|---|
| `services/ingest` | Storefront adapters, LLM extraction, entity resolution and the Neo4j graph (`ingest`, port 8085). Routes under `/er` and `/extraction`. |
| `services/shopify-stub` | A local implementation of exactly the Shopify surface this system uses (`shopify_stub.app:create_app`, port 8787): `POST /admin/api/{version}/graphql.json`, `GET /cart/{items}`, and a `/_stub` control surface (`/_stub/seed`, `/_stub/checkouts/{token}/complete`, `/_stub/webhooks/deliveries`, …). This is what stands in for a development store. |
| `services/sim` | **Not a server.** A headless CLI — `python -m sim --json` — whose exit code is the finding. |

### `packages/`

| Path | What it is |
|---|---|
| `packages/store-agent` | Despite living under `packages/`, this **is** a deployable service (`store_agent`, port 8086) — one process per store, one route: `POST /v1/bid-requests`, answering 200 with a Bid or 204 with a Decline. |
| `packages/contracts` | A dual Python + npm package: the JSON-schema registry, protocol models, JCS signing, OpenAPI helpers and codegen. |
| `packages/llm` | Library: the Anthropic client wrapper, configuration, the offline doubles, and prompt recordings. |
| `packages/verification` | Library: claim comparators, normalisation and verification statuses. |

### Everything else

| Path | What it is |
|---|---|
| `proxyshop_support/` | Shared runtime helpers every service imports — the port-0 `serve()` context manager, the Postgres/Redis/Neo4j clients, the clock, the fixture loader, and the LLM double. |
| `pixel/` | The npm workspace `@proxyshop/pixel`: the Shopify web pixel extension. `src/` holds `beacon.ts` (the pure `checkout_completed` → collector-body transform, built from a four-key allowlist so no customer PII can leak into it), `transport.ts`, `settings.ts`, `pixel.ts` and `index.ts`, with tests beside them under `tests/`. What it emits reaches `merchant_svc.collector` and stops there — see below. |
| `proxyshop_demo/` | The narrated CLI demo from demo 1 (`s1.py` is the driver, `narrate.py` the prose). |
| `e2e/` | Cross-service pytest: `test_s1_flow.py` (the scripted S1 proof), `test_jcs_conformance.py`, `test_scaffold_smoke.py`, and the fixtures under `e2e/support/s1/`. |
| `db/` | Raw SQL — one init script and four migrations. |
| `fixtures/` | A uv workspace member with a digest-pinned `manifest.json` and its loader. |
| `docs/deploy.md` | **Read this before deploying or scaling anything.** How each service is told about the others, and why `--workers 1` on the exchange is a correctness pin rather than tuning: raising it, adding `deploy.replicas`, or running `--scale exchange=N` re-opens a double-spend that mints two live discount codes for one purchase. Nothing shipped is broken — the pin holds it shut — but nothing else in the documentation will tell you that before you change it. |

---

## What is not finished

This section is the measured gap list, not a summary of it.

### The demo's own gap: reconciliation and the trust update

The CLI demo prints exactly one `DOES NOT RUN YET` block, and this is it.
`trust.reconcile.reconcile` joins the accepted offer, the browser beacon and the webhook into
a single `reconciled` verdict, and that verdict is what moves a store's trust score — the same
snapshot the ranker filters on.

Both halves exist, are tested, and are now wired to each other. `POST /reconcile` is a served
route on `apps/trust`. A served accept files the whole C11 trio into the chain — `accepted`
from the state machine's own transition, then `code_created` and `checkout_redirect` forwarded
by `_record_checkout_bridge` (`apps/exchange/src/accept/routes.py:692`, called at `:901`) — and
the merchant posts its HMAC-verified `order_paid` to the trust service over HTTP. Measured on one
run of demo 1: **seventeen** events in eight kinds — `auction_opened`, `bid_placed`,
`auction_closed`, `shown`, `accepted`, `code_created`, `checkout_redirect`, `order_paid` —
written by two different applications, read back through `GET /events` and verified as an
unbroken hash chain. (This paragraph said eight events in six kinds, from a run before
`bid_placed` and `shown` had served producers. The count moves with how many stores answer and
how many slots fill: read it off the run.)

**The fold over that chain still returns 0 verdicts.** Two things stop it, and the demo
measures both rather than asserting them:

1. **The two halves of the purchase name the seller differently.** `reconcile` namespaces every
   join key by the store that owns it — it has to, a Shopify `order_id` is a per-shop number and
   two shops both have order 1001. The signed `order_paid` names
   `proxyshop-demo.myshopify.com`, because an unsigned `X-Shopify-Shop-Domain` header is the
   only shop identity a signed delivery carries at all. The mapping back to a `store_id` is
   built — `trust.reconcile.routes.resolve_store_aliases` reads the platform's own
   `app.sellers` roster — but that table is Postgres, and neither demo starts one. The demo
   prints a *probe* beside the real answer: state the names and the same events, the same
   webhook and the same fold produce one real verdict, price honoured, `389.0 / 389.0`.
2. **This driver never posts a beacon, so the run's chain carries no `checkout_pixel`.** The
   *producer* is no longer missing, and that is the part that changed: `POST /pixel/collect`
   writes a real `checkout_pixel` row to the chained ledger
   (`apps/merchant/svc/src/collector/routes.py:115` → `composition.publish_pixel_observation`),
   and the e2e S1 driver drives that served route and gets one. What the CLI demo does not do
   is call it — beat 6 reads a `PixelObservation` in process and stops there — so this run's
   fold sees no beacon. That costs *evidence* rather than the verdict: a group with no beacon
   grades `pixel_missing`, which by design is not a blocker. Until the CLI driver posts to the
   collector the way the e2e one does, this stays a gap in the demo rather than in the tree.

### The four things the buyer UI itself says are not wired

The running page renders a "What is not wired yet" panel
(`apps/buyer/app/journey/Journey.tsx:757`). Read the page, not this list — the panel is
measured on the stack in front of you and this paragraph is not. At the time of writing it
carries four items, and the earlier three this section used to name are gone: sign-in and the
browser-minted pseudonym both closed (the page signs in by magic link —
`apps/buyer/app/journey/SignIn.tsx`, `journey/magic-link.ts`), price landed on the slot, and
so did the store domain — `ShortlistSlot.store_domain`
(`packages/contracts/src/generated/protocol.py:694`) is written from the platform's registry
and never from the bid, so the redirect guard *can* pin a checkout host to a named store. The
four the panel lists now:

1. **Whose price it is: not on the slot** (`gap-fallback`). When a store does not answer, the
   exchange stands in for it at its roster list price and that number reaches the card as the
   slot's `price` like any other. `ShortlistSlot` declares no `fallback` flag, so a card
   cannot say which of the two happened; `entries[]` can, per store.
2. **Product: a reference, not a name** (`gap-product-name`). The slot carries `product_ref`
   (and `variant_ref` where the bid named one), which is what the roster and the offer agree
   on. Nothing in this app resolves a title, so the card shows the reference rather than
   inventing a name.
3. **The shop's own voice does not reach the page** (`gap-store-voice`). A store agent really
   does write a per-shopper pitch onto `Bid.message`, and `POST /buyer/shortlist/render`
   really does carry one back verbatim when a slot arrives holding one — but
   `contracts.protocol.ShortlistSlot` is `extra="forbid"` and declares no message field, so
   the seller's words are dropped at that boundary. Every slot shows the organic voice only.
4. **The clarifying questions came from no live model** (`gap-model`). With `LLM_PROVIDER`
   unset, `build_llm("buyer")` returns the offline double `<DeterministicLLM role='buyer'>`,
   `model="double:buyer"`. That is the intended default rather than a failure, but it means
   the questions and the extraction are the buyer service's own wording, not a model's.

### The trust service's contract and its routes now name the same ten

This section used to say the two drifted: the contract declared `GET /stores/{store_id}/trust`
and `POST /feedback/{order_ref}` and no route served them, while `GET|POST /reconcile` were
served and undeclared. Both directions are closed.
`packages/contracts/openapi/trust.openapi.json` declares ten operations and `apps/trust` serves
the same ten — `GET|POST /events`, `/events/head`, `/events/verify`, `/events/replay`,
`/events/{event_id}`, `POST /claims/verifications`, `GET /snapshot`, `GET|POST /reconcile`.

The two published-but-unserved paths were *unpinned* rather than served, and the reason matters
because it is a standing rule rather than a deferral: each was unservable as published — the
per-store read declared no identity parameter of any kind, and the feedback path declared no
routing evidence. `packages/contracts/src/openapi.py` carries the ruling and
`apps/trust/tests/test_contract_surface.py` holds it. Re-pin either one in the change that
serves it, with an identity parameter and with the caller that reads it.

The direction that used to matter most is closed too. The trust score the ranker filters on no
longer has to be typed into a deployment document by hand: `GET /snapshot` is served, and
`exchange.composition.bind_trust_snapshot_reader` points `app.state.trust_snapshot` at it on
every rung, so a document that states no `trust_snapshot` gets a live view instead of `{}`. A
document that *does* state one still wins — that key is a person's statement and composition
does not overrule it.

### Where the rest of the known defects are written down

The strict xfails a full `make verify` reports are this repository's open-defect register, and
each one carries its reason in its own decorator. **Run the grep rather than trusting the list
below** — the register moves as tickets close, and a count typed here goes stale the moment one
does:

```sh
grep -rn "^@pytest.mark.xfail" --include='*.py' apps packages services proxyshop_support
```

That grep is the locator, not `git ls-files | grep test_repro_open_tickets.py`, which is what
this section used to say: eleven files match that name and nine of them carry no xfail at all,
while three markers live in files it does not match
(`apps/exchange/tests/test_accept_denials.py`,
`proxyshop_support/tests/test_repro_ticket_graph.py` and
`services/ingest/tests/test_embedding_ranking_gate.py`).

Measured at the time of writing: **six** markers in five files, of which **four** fire under a
default `make verify` and name a ticket — T-158 and T-325 (exchange), T-142 (buyer) and T-262
(`proxyshop_support`). The other two are the pair in
`services/ingest/tests/test_embedding_ranking_gate.py`, conditional on
`EMBEDDING_PROVIDER=hash`; the default is `lexical` (D19 as amended), so on an ordinary run
they do not fire and they name no ticket — they are a guard on a provider choice, not an open
defect. An earlier version of this section claimed eight markers naming T-204 (contracts),
T-321 (store-agent), T-312 (exchange) and T-302 (trust) alongside the four above; none of those
four markers exists in the tree.

Some of what they name is larger than anything listed above. Read them before assuming a
behaviour works.

Nothing else in this repo should be read as a promise. If a behaviour is not demonstrated by
one of the two demos or by the test suite, treat it as unbuilt.

Where prose and a demo run disagree, believe the run: the gap list the driver prints is
measured on the spot, and the prose is not.

**And do not trust a list of stale prose either — including this one.** This paragraph used to
name `proxyshop_demo/s1.py` and `e2e/support/s1/flow.py` as the two files claiming `pixel/src/`
held nothing but an empty `.gitkeep`; both were corrected in the very commit that wrote the
list, and other stale files it did not name existed at the same time. A register of stale text
is stale by construction, because the thing that makes an entry wrong — somebody fixing the
code — is exactly the thing that does not update the register. The durable rule is the one
above it: measure the tree, and treat a present-tense claim in any comment, docstring, runbook
or README bullet as a claim to re-check rather than a fact. Roughly a third of the apparent
defects found in this repository over a two-cycle audit were prose asserting a defect that had
already been fixed.

---

## Running the tests

```sh
PROXYSHOP_WORKER=1 make verify
```

`make verify` is `scripts/verify.sh all`. It exits 2 before doing anything at all if
`PROXYSHOP_WORKER` is unset — every run is per-worker isolated and gets its own Postgres
database, `proxyshop_w$N`. **Worker 0 is reserved for the project's scored measurement run;
do not use it.**

The datastores come up separately:

```sh
make deps-up      # Postgres + Neo4j + Redis via docker compose, then db-init
make deps-down
```

`verify.sh all` runs these stages in order:

1. `ruff check .`, `ruff format --check .`, `lint-imports`, a grep over every tracked file for
   a banned global Redis flush, then `eslint .`
2. `mypy`, `tsc -b`
3. `pytest -q -m "not needs_model"`
4. `vitest run --passWithNoTests`
5. `python scripts/check_verify_contracts.py`
6. `echo "OK: $STEP"`

A full run takes six to seven minutes. Below is what one **recorded** run printed. The shape is
what to read it for; the counts move with every commit, and two of them have moved a long way
since:

```
5980 passed, 1 deselected, 56 xfailed, 11 warnings in 363.01s (0:06:03)
SELECTION: 1 deselected, 0 skipped  <-  pytest -q -m not needs_model

 Test Files  16 passed (16)
      Tests  864 passed (864)

check_verify_contracts:
  6 ticket verify path(s) not created yet (expected on a fresh scaffold):
    T-024  services/ingest/tests/test_refresh.py
    ...
  OK: pytest-config, test-path-filter, schema-package, non-empty-test-dir, raw-Redis-client and unique-fixture-name contracts all hold.
OK: all
```

`OK: all` on the last line and exit 0 is the whole result.

**The two numbers that have moved, both checkable without a six-minute run.** There are now
**8** strict xfails, not 56 — `grep -rn "^@pytest.mark.xfail" --include='*.py' apps packages
services proxyshop_support` finds every one, and they are named under [Where the rest of the
known defects are written down](#where-the-rest-of-the-known-defects-are-written-down). And
`./.venv/bin/python scripts/check_verify_contracts.py` now reports **two** unwritten verify
paths, not six: T-086 (`e2e/test_onboarding.py`) and T-087 (`docs/tests/test_runbook.py`).
T-024's file exists.

The xfails are not noise: each one names an open defect in its own reason string, so the suite
carries its known gaps in the open rather than deleting the tests that expose them. The "not
created yet" verify paths are tickets whose test file nobody has written.

Two things the gate does deliberately and loudly, worth knowing before you read its output:
collecting zero tests is a **failure**, not a pass (pytest's exit 5 is fatal here, including
when every test was deselected), and the deselected count is printed on its own line every
time, so "passed" can never be mistaken for "ran".

Narrower targets, all real in the `Makefile`:

| Target | What it does |
|---|---|
| `make bootstrap` | `uv sync --locked` + `npm ci --prefer-offline`, in this checkout |
| `make check` | The per-ticket gate — runs the docker tests, which skip per-service when their datastore is down |
| `make lint` | ruff + import-linter + the banned-reset gate + eslint |
| `make types` | mypy + `tsc -b` |
| `make test-py` | Python tests only |
| `make test-ts` | TypeScript tests only |
| `make demo-seed` | `./.venv/bin/python -m fixtures.seed --category "$(SEED_CATEGORY)"` |
| `make deps-up` / `make deps-down` | Bring the datastores up (then `db-init`) or tear them down |

One target that behaves unusually on purpose: `make e2e-live` is a preflight that **always**
exits non-zero — 2 means a live precondition is unmet, 3 means all the preconditions hold but
the live driver itself is missing. A non-zero exit from it is not news. Exit 3 is currently
permanent: the driver lives in `docs/demo/shopify-onboarding-extension.md`, which
`docs/demo/e2e_live.sh:36` names and which does not exist — `git ls-files docs/demo` returns
`e2e_live.sh`, `shopper-demo.md` and `starting-slice.md`, and no fourth entry. (That
enumeration used to name only the first and last of the three; `shopper-demo.md` landed after
it was written and is not the missing extension runbook.)
