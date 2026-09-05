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
irrelevant, it still exits 0.) It prints 301 lines: a header, the roster, six narrated beats,
and a closing summary that states both what ran and what did not.

It opens with this banner:

```
================================================================================
  ProxyShop — the S1 starting slice, live
  four deployables, four loopback ports, one purchase, no Shopify account
================================================================================
```

**That second line undercounts, and it is the one thing on screen that is wrong.** Measured:
the driver starts **seven uvicorn servers**, each on its own ephemeral loopback port, all as
daemon threads inside **one OS process** — two store agents (one per hosted store), the buyer
service, the exchange, the Shopify stub, a pixel collector and a webhook receiver. There is no
`subprocess`, `Popen`, `multiprocessing` or `fork` anywhere in `proxyshop_demo/s1.py`; the
servers come from `proxyshop_support/asgi_server.py`'s `serve()`, which is
`uvicorn.Config(app, port=0)` on a `threading.Thread`. An eighth port is bound and then closed
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
      ...
  Did NOT run, and why (1):
      - reconciliation and the trust update cannot run from anything this demo served
```

That last block is not boilerplate. It is the demo's own gap list, and there is exactly one
entry in it. See [What is not finished](#what-is-not-finished).

---

## Demo 2 — the browser

```sh
PROXYSHOP_WORKER=1 npm run demo --workspace @proxyshop/buyer
```

That npm script is `npm run build:ui && ../../.venv/bin/python devstack/run.py`: it builds the
Vite/React SPA and then boots the stack. The build took 97 ms and 22 transformed modules on my
run, and `http://127.0.0.1:8100/` answered `HTTP/1.1 200 OK` within a second of the banner
appearing.

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

### Type the scripted conversation exactly

The banner also prints two scripted conversations, and it is not being precious about them.
`cluster_id` is a hash over the clarified query, the budget band and the constraints, and a
store agent declines any cluster its envelope does not pursue — so **every extra answer you
type changes the hash, after which no store bids and you get an empty page for a reason that
is your own keystrokes.** The shortest of the two is a single line with no follow-up answers:

```
I want a warm merino wool beanie for winter, under $100
```

Type that, press Send, and step 2 comes back with the extraction:

```
Here is what we understood
What you asked for   I want a warm merino wool beanie for winter, under $100
Budget               100-250
Must have            material is merino-wool
                     price_usd at most 100
```

Press **Confirm and ask stores**, and step 3 is a real auction result:

```
Auction auction-d945ab99-70e2-4844-bb4d-c368d4554ee1, opened 2026-09-05T16:58:13.437561Z.
The exchange asked 3 stores when the auction opened. Its shortlist, re-fetched for this
page, came back with 2 options.

2 options, one per store
  Fit    fit 0.5640000000000001   score 0.82   from their website / store-confirmed
  Value  fit 0.522                score 0.61   from their website / store-confirmed

fit — demo-woolworks       unit 78, total 78
value — demo-alpine-supply unit 72, total 72
```

Three stores were asked and two are shown, because the third is excluded on the record. Asked
through the API, the exchange gives both of its reasons for `demo-fastfleece` in full:

```
"blacklisted_store: 'demo-fastfleece' is blacklisted and may not participate (R12)"
"hard_constraint_unsatisfied: 'material': the candidate carries no such attribute, so the
 constraint is undecidable and does not count as satisfied (R19)"
```

Click **Accept this one** on the Fit slot and the page hands off to the seller's own domain,
and says whose decision that was:

```
Your checkout is at demo-woolworks.example.com. We did not choose that address — the store's
exchange did.
```

The same journey driven over HTTP against the running stack gives the same thing in JSON —
`POST /buyer/shortlist/accept` answers 200 with
`"permalink_url": "https://demo-woolworks.example.com/cart/1:1?discount=PSX-05JN5156"`.

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
| `apps/trust` | The hash-chained, append-only event ledger plus scoring, reconciliation and snapshots (`trust`, port 8084). It serves one router, `/events`: `POST /events`, `GET /events`, `/events/head`, `/events/verify`, `/events/replay`, `/events/{id}`. Its published contract declares three more paths that it does not serve — see [What is not finished](#what-is-not-finished). |
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
| `pixel/` | The npm workspace `@proxyshop/pixel`. **Scaffold only** — see below. |
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
snapshot the ranker filters on. Both halves exist and are tested. Neither can be reached from
what the demo stands up, for two reasons that were measured rather than assumed:

1. **Nothing emits the beacon event.** `reconcile` reads a page of ledger events —
   `accepted`, `checkout_pixel`, `order_paid`. The exchange's ledger is an in-process sink
   that no HTTP route serves, and `pixel/src/` is empty, so no deployable emits
   `checkout_pixel` at all.
2. **Even given the page, the join key is missing.** The checkout provider invents its
   `checkout_token` with `secrets.token_hex(16)` and never transmits it — the cart permalink
   carries the discount code and nothing else — while the merchant mints its own, unrelated
   token when the cart is visited. `reconcile` joins on exactly that key, finds none, and
   emits nothing.

In the demo's own words: *"this is where the starting slice genuinely stops today."*

### `pixel/src/` is scaffold only

`pixel/src/` contains a single 0-byte `.gitkeep` and nothing else; `pixel/tests/` holds one
scaffold test. The directory and the npm workspace exist, the web pixel does not. This is the
direct cause of reason 1 above.

### The five things the buyer UI itself says are not wired

The running page renders a "What is not wired yet" panel. These are its own five items:

1. **Sign-in is not available.** `POST /buyer/auth/magic-link` answers 202 and, by design,
   never returns the token, so a browser cannot redeem one without an email transport. There
   is no sign-in form on the page, because nothing could complete it.
2. **The pseudonym is minted in the browser.** It is generated client-side for the visit and
   thrown away on reload, because sign-in cannot complete and so there is no session to mint
   one from. Stores are told only the pseudonym (`psn-ed94fae142` on my run) and nothing else.
3. **The store domain is not pinned.** The exchange's shortlist slot carries no
   `store_domain`, so the checkout host can only be checked for scheme and host presence — it
   cannot be pinned to a named store.
4. **Price is not on the slot.** The shortlist slot carries no price field at all. The page
   joins prices out of `entries[]` in the same `GET /buyer/auctions/{id}` answer, matching on
   the store id inside each slot's `bid_ref`, and labels every price *recorded* rather than
   *current*.
5. **The clarifying questions came from no live model.** With `LLM_PROVIDER` unset,
   `build_llm("buyer")` returns the offline double `<DeterministicLLM role='buyer'>`,
   `model="double:buyer"`. That is the intended default rather than a failure, but it means
   the questions and the extraction are the buyer service's own wording, not a model's.

### The trust service publishes a read API it does not serve

`packages/contracts/openapi/trust.openapi.json` declares four paths — `/events`,
`/snapshot`, `/stores/{store_id}/trust` and `/feedback/{order_ref}`. The app mounts one
router, `/events`, and serves six operations under it. The other three paths are published
and not served.

That is why the trust score the ranker filters on reaches the exchange by hand: the exchange
reads a `trust_snapshot` out of its deployment document
(`apps/exchange/src/composition.py:936-938`), because no client can fetch one over HTTP. The
two halves named in the gap above both exist, and there is no wire between them.

### Where the rest of the known defects are written down

The 56 xfails a full `make verify` reports are this repository's open-defect register, and
each one carries its reason in its own decorator. They live in eleven files, one per package:

```sh
git ls-files | grep test_repro_open_tickets.py
```

Some of what they name is larger than anything listed above. Read them before assuming a
behaviour works.

Nothing else in this repo should be read as a promise. If a behaviour is not demonstrated by
one of the two demos or by the test suite, treat it as unbuilt.

Note on the docs: the `proxyshop_demo/s1.py` module docstring still says **four** beats do not
run. That is stale. Three of those four now work — `POST /buyer/intent/confirm` answers 201
with an `auction_id`, the store-agent probe answers 204 `no_matching_product`, and the silent
store `store-slowreply` does reach the shortlist in the `value` slot at $519.00. Only
reconciliation remains. `docs/demo/starting-slice.md` said the same thing and was corrected
alongside this file. Where prose and a demo run disagree, believe the run: the gap list the
driver prints is measured on the spot, and the prose is not.

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

A full run takes six to seven minutes and reports:

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

`OK: all` on the last line and exit 0 is the whole result. The 56 xfails are not noise: each
one names an open defect in its own reason string, so the suite carries its known gaps in the
open rather than deleting the tests that expose them. The six "not created yet" verify paths
are tickets whose test file nobody has written.

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
`docs/demo/e2e_live.sh:37` names and which does not exist — `git ls-files docs/demo` returns
only `.gitkeep`, `e2e_live.sh` and `starting-slice.md`.
