# ProxyShop starting-slice demo

The starting path, end to end, on one laptop, with no Shopify account.

A shopper's intent is clarified, per-store agents bid for the business, the exchange ranks
the sealed bids and shows a shortlist, the buyer accepts one, a single-use discount code is
minted and reached through the `CheckoutProvider` port, the order completes at a local
merchant stub, the web pixel and the `orders/paid` webhook are reconciled against what was
promised, and the store's trust score moves on the result.

Every step below runs against `services/shopify-stub` and the `SimulatedRedirectProvider`.
There is no Shopify app to install, no development store to provision, and no merchant
onboarding to sit through — those are the extension lane, and where they live is the last
section of this page.

## What this run proves

- **S1.** intent → up to three clarifications → parallel bids → shortlist → accepted offer →
  code created and validated → checkout through the `CheckoutProvider` port → webhook and
  pixel reconciled → ledger event → trust update.
- **C9.** All of it offline. Nothing in this procedure reaches the public network.
- **C11.** The simulated redirect path emits the same canonical `LedgerEvent` kinds a
  Shopify checkout would, so the pixel and reconciliation obligations hold either way.
- **R10.** A store whose agent never answers is still represented, at its catalogue list
  price, and can still reach the shortlist.
- **S8.** No blacklisted seller is eligible at any gate, and no checkout URL is ever returned
  off the seller's registered domain.

## What you need, and what you do not

You need Docker, `uv`, and Node. You do **not** need a Shopify account, a Partner account, a
development store, an app installation, a storefront password, a payment gateway, or an LLM
API key. The buyer's clarifier, the store agents and the extractor all run against
deterministic offline doubles (D19), and the merchant is `services/shopify-stub` — a local
implementation of exactly the Shopify surface this system uses, and nothing more.

Two environment values carry the whole "no account needed" property:

```bash
CHECKOUT_MODE=redirect   # D22/D23 default: SimulatedRedirectProvider mints the code locally
LLM_PROVIDER=double      # D19: recorded and scripted answers, never a live model
```

Both are already the defaults in `.env.example`. Copy it and leave them alone:

```bash
cp .env.example .env
```

**Then load it into your shell.** The copy gives Compose its interpolation values, and nothing
on the path you are about to walk reads it — not the Makefile, not the database-init script,
not any Python at import — so the next step, straight after the copy, would fail with
`FATAL: PROXYSHOP_WORKER is unset`:

```bash
set -a && . ./.env && set +a
```

That is also where `PROXYSHOP_WORKER` comes from, and `.env.example` ships `1`. **Do not use
worker 0**: it is reserved for this project's scored measurement run, and a demo that takes
it corrupts the number the project is graded on.

## 1. Bring up the stack

```bash
make bootstrap
make deps-up
```

`bootstrap` builds the virtualenv and installs the Node toolchain. `deps-up` starts
Postgres, Neo4j and Redis, waits for them to report healthy, and then initialises this
worker's database.

`deps-up` creates that database **empty** — it applies no migrations, and nothing else in the
repo did either outside the test fixtures, so a stack brought up this way used to answer 503
on every database-backed route. Apply the schema:

```bash
./.venv/bin/python scripts/db_migrate.py
```

Idempotent; run it again after any `make deps-down`, which destroys the volumes.

The merchant stub is not started by `deps-up` — it carries the `e2e`
compose profile, because the tests build it in-process on an ephemeral port instead. For the
live demo you want it running as a service:

```bash
docker compose --profile e2e up -d shopify-stub
```

Confirm the whole toolchain is sound before going further:

```bash
make check
```

## 2. Provisioning the seeded catalogue into the merchant stub

The demo's stores and products are generated from the human-approved fixture manifest
(`fixtures/manifest.json`) under a fixed seed, so every operator provisioning this demo gets
byte-identical catalogues. Nothing here touches a live store.

```bash
make demo-seed
```

The target reads `SEED_CATEGORY` from your environment and falls back to the category the
approved manifest names. Provisioning is idempotent: run it twice and the second run changes
nothing. To see what it would write without contacting the stub at all:

```bash
uv run python -m fixtures.seed --dry-run
```

Seeded into the stub are the honest control store, the store the trust engine is meant to
catch, a store that answers slowly, a store carrying an expired blacklist entry, and a brand
new store with no history. Their behaviours are defined in the approved manifest rather than
in the trust engine's own configuration — otherwise "the trust engine catches the dishonest
store" would be the engine grading its own homework.

## 3. The live-auction demo beat

This is the beat to run in front of an audience. **This is the command:**

```bash
./.venv/bin/python -m proxyshop_demo
```

It takes about eight seconds and prints the whole journey as a narrative — the shopper's
words, the questions asked back, the store that is denied and why, each store's bid as it
arrives, the ranked shortlist with the reason each slot placed where it did, the real
single-use code, the permalink, the order the merchant closed, the pixel beacon and the
HMAC-signed webhook. Every arrow it prints is a real HTTP round trip.

**Everything above this section except `make bootstrap` is optional for this command.** The
driver starts what it needs on loopback ports of its own — the buyer service, one store agent
per hosted store, the exchange, the merchant stub, a pixel collector and a webhook receiver —
and touches no datastore, so `make deps-up`, the migration, the seeded catalogue and the
long-running stub are for the rest of this page, not for this beat. Nothing it does reaches
the public network.

Nothing is wired by the driver, which is the property that makes it worth watching: the
exchange reads its collaborators out of a deployment document it is pointed at with
`EXCHANGE_DEPLOYMENT`, and each store agent reads its approved envelope and catalogue out of
the file named in `STORE_AGENT_CONTEXT`. That is what a person deploying these containers
does, and it is the hop that a test which configures the app it is testing never exercises.

The roster, the prices and the four store roles come from the same S1 run fixture the scripted
proof in section 4 uses, so the demo and the proof are about one purchase rather than two that
resemble each other.

### What it will tell you it cannot do

**One beat on this page does not run yet, and the driver prints it as a `DOES NOT RUN
YET` block instead of skipping it.** Read it out loud rather than scrolling past: a demo that
quietly omitted it would be the same claim as a green board over a broken system. It is
*measured* by the run — the driver makes the call and prints the answer it got — rather than
asserted from having read the source:

1. **Reconciliation and the trust projection cannot run from anything served** — see 3.6/3.7.

**This section used to list four.** Three of them have since been closed, and the driver
measures all three every run rather than taking anyone's word for it:

- The shopper's "yes" now reaches the exchange. `POST /buyer/intent/confirm` answers **201**
  with an `auction_id`; it used to answer 503 for want of a composition root binding an
  auction client.
- The clarifier's cluster now names something. The exchange assigns a named cluster —
  `cluster-espresso` on this scenario — and the store agent that used to decline
  `cluster_not_pursued` for want of a namespace mapping now declines `no_matching_product`,
  which is a merchant's answer about its catalogue rather than a wiring hole. The same store
  bids normally in the auction itself.
- The silent store's fallback now reaches the shortlist, in the `value` slot at its catalogue
  list price. See 3.4.

Each step below names the component that does the work, so a question about "what actually
happened there" has an answer in the code.

### 3.1 Intent and the clarifying questions

The buyer states a rough intent. `buyer_svc.intent.clarify` asks at most three clarifying
questions and stops — the ceiling is a published constraint, not a heuristic — and returns a
structured intent plus the transcript. It never self-confirms: confirmation is the shopper's
separate act, and until it happens no auction exists.

### 3.2 The auction opens and the roster is gated

The confirmed intent is posted to the exchange's `POST /auctions`. The auction state machine
opens the auction and writes `auction_opened`. Before any store is asked for a bid the seller
eligibility source is read, and it fails closed: a store whose status cannot be read is
treated as unavailable, and a blacklisted store is denied here — it is never asked, never
collected, never ranked, never shown.

Point out that the blacklisted store appears in the response's `denied` list with its status
and reason. That is the first of the four gates it has to fail.

### 3.3 Pitches, verification and eligibility

Solicitation fans out to every eligible store inside a bounded bid window. Each hosted store
agent prices from its own catalogue, moves only within the discount depth its approved
envelope authorises, and returns a sealed bid with the claims it is asserting. No model is
anywhere near a price.

The store that stays quiet is the interesting one: when the window closes, the exchange
manufactures a list-price offer for it from catalogue data and marks the entry `fallback`
with the reason `no_response`. A silent store loses its ability to discount, not its place in
the auction.

Each hosted pitch's claims are then graded by `claim_verification.verify` against that
store's catalogue snapshot, producing one of four statuses per claim — `verified`,
`contradicted`, `unsupported` or `ambiguous`. Only a `verified` claim counts as evidence for
a hard constraint. A fallback offer asserts no claims at all, which is why it can never be
the evidence that satisfies one.

### 3.4 Ranking and the shortlist

`exchange.ranking.rank` applies the hard-constraint filters, then the published weighted
formula, and builds the shortlist. The ranking is fee-blind and tier-blind: paying the
network more cannot buy a better position. The blacklist filter reads the served trust
snapshot and, again, fails closed — a store with no row is excluded rather than defaulted in.

**The shortlist carries both a real hosted bid and the silent store's list-price fallback, and
the driver measures it every run.** On this scenario it prints three slots and zero exclusions:
the silent `store-slowreply` takes the `value` slot at its catalogue list price of $519.00. Both
halves of R10 hold on this path — the store is represented, the entry says why it fell back, and
it can still reach the shortlist.

This subsection previously recorded the opposite, because the fallback the exchange
manufactured carried no `expires_at` and no `checkout_url`, both filters failed closed, and the
entry was excluded `expired_offer` + `off_domain_checkout` before it could be ranked. That is
fixed; the reasons are kept here only so a reader who remembers the old behaviour knows it
changed rather than wondering which page to believe.

What the shortlist *does* carry is worth pointing at: each slot names why it placed there. The
driver prints the weighted terms, and they sum to the score — in a normal run the two hosted
stores differ on the `trust` term alone, because the approved manifest calls one of them its
honest control store and the other its scripted dishonest one, and the served snapshot says so.

Each filled slot is written to the ledger as `shown`.

### 3.5 Acceptance, the single-use code and the simulated redirect

The shopper accepts one slot. `exchange.accept.accept` re-checks eligibility, then resolves a
provider for `CHECKOUT_MODE`. In `redirect` that is `SimulatedRedirectProvider`, which mints
a single-use code locally and builds a cart permalink on the seller's **registered** domain —
the platform's record of that domain, not the domain the bid claimed for itself.

The host comparison is exact. Worth demonstrating: a bid whose checkout URL points at
`checkout.<seller>` or `evil-<seller>.attacker.tld` or `<seller>@attacker.tld` is refused,
and refused *before* a discount code is created. The port writes `accepted`, `code_created`
and `checkout_redirect`.

### 3.6 The pixel, the webhook and reconciliation

Follow the permalink into the stub and complete the checkout. Two things happen, and their
asymmetry is the point:

- the **web pixel** fires a browser-side beacon at the merchant app's collector. It is lossy
  and untrusted by design — the stub can be configured to drop it — and it carries no
  customer PII.
- the **`orders/paid` webhook** is delivered server to server, signed with HMAC-SHA256 over
  the exact bytes on the wire. The merchant refuses an unsigned or re-serialised body.

`trust.reconcile.reconcile` joins the accepted offer, the beacon and the webhook into one
`reconciled` verdict: was the promised price honoured, was the promised discount honoured,
was the pixel missing. The webhook is the truth; the pixel is corroboration.

**Everything up to that join runs live, and the join itself does not.** The driver really
completes the checkout, really receives the beacon at a collector, really receives the signed
delivery and really verifies it with the merchant app's own code — order `#1001` for `$389.00`,
`200 recorded`, ledger kind `order_paid`. It also offers the spent code a second time, and
prints the outcome carefully, because two different things look alike there: the second
checkout is *not* refused, it completes carrying no discount. Single use means "the second
order redeemed nothing", not "the second attempt errored".

Then it stops, for two reasons it prints:

- `reconcile` reads a page of ledger events. The exchange's ledger is an in-process sink that
  no HTTP route serves, and this repository's pixel source directory is empty, so nothing
  deployed emits `checkout_pixel` at all. A driver that manufactured those events would be
  supplying the join whose absence is the defect.
- Even given the page, the join key is missing. The checkout provider invents its
  `checkout_token` and never transmits it — the permalink carries the discount code and
  nothing else — while the merchant mints its own, unrelated token when the cart is visited.
  `reconcile` joins on exactly that key. `e2e/support/s1` closes the gap by deriving the
  binding from the single-use code and says so in its own docstring; nothing deployed does.

### 3.7 The trust projection

The reconciliation outcome and the verification verdicts become observations against the six
trust dimensions — five transaction dimensions plus `catalog_claim_accuracy`, which is where
a product-fact claim lands. `trust.scoring.score` and `trust.snapshot.build_snapshot` fold
them in and serve an updated snapshot, which is the same snapshot the ranker filters on. That
closes the loop: what a store did last time changes what it can win next time.

**The loop does not close from anything the live beat serves**, for the reason 3.6 gives: with
no `reconciled` verdict there are no observations to fold in. The live demo shows the snapshot
being *read* — the driver's deployment document states one, the ranker filters on it, and the
`trust` term is what separates the two hosted stores' scores — and it does not show the
snapshot being *written*. Section 4's scripted proof is where the whole loop is exercised
today, and the driver says so at the point it stops.

## 4. Run the whole beat as one scripted proof

Everything in section 3 runs unattended, offline, in about a second and a half:

```bash
export PROXYSHOP_WORKER=1
uv run python -m pytest e2e/test_s1_flow.py -q
```

This used to say `export PROXYSHOP_WORKER=0`, which contradicted `.env.example`'s
`PROXYSHOP_WORKER=1` and pointed the demo at the index reserved for the project's scored
measurement run. Any index other than 0 works; 1 is what the copied `.env` already gives you,
so in a shell that ran `set -a && . ./.env && set +a` the export above is a no-op.

One scripted pass drives every component named above and then asserts the acceptance
criteria against it, including an exact per-kind count over all eighteen frozen
`LedgerEvent` kinds — zeros written out explicitly, so "this never happened" is a checked
claim rather than a gap. Run it before the demo. If it is green, the beat will work.

## Reading the ledger

The run's own ledger is the artifact to show. The kinds it produces, and what each one
means:

| kind | written when |
|---|---|
| `auction_opened` / `auction_closed` | the auction's state machine transitions |
| `bid_placed` | once per store the exchange solicited |
| `shown` | once per shortlist slot the shopper saw |
| `claim_verified` | once per graded claim, carrying the trust dimension it routes to |
| `accepted` / `code_created` / `checkout_redirect` | the checkout port, in one shot |
| `checkout_pixel` | the browser beacon the collector accepted |
| `order_paid` | the signed webhook the merchant authenticated |
| `reconciled` | the join of promise and outcome |

The ledger is append-only and hash-chained, so the sequence is tamper-evident: an edited
event breaks the chain rather than passing quietly.

## Off the starting path: dev-store provisioning, the onboarding interview, and `make e2e-live`

**None of the following is a step in this runbook.** The starting slice above is complete
without them, which is the whole reason it needs no Shopify account. They are documented
here only so a reader knows where the boundary is and where to go next.

- **Shopify development stores.** The Shopify adapter implements the same `CheckoutProvider`
  port behind the same seam and emits the identical `LedgerEvent` kinds (C11). Running the
  demo against seeded development stores — app installation, storefront password, the Bogus
  Gateway — is the extension runbook's procedure, driven by:

  ```bash
  make e2e-live
  ```

  A live development-store run is **demo procedure only**. It is never a ticket's
  verification gate (C9), and no ticket may depend on it. The target is defined in the repo
  root Makefile and shells out to `docs/demo/e2e_live.sh`.

  The live *procedure* lands with the extension runbook. Until it does, the script is a
  **preflight**: it selects the real embedding model (D18), then reads the repo's own
  configuration surface and reports, one line each, whether five live preconditions hold —
  the app's `SHOPIFY_API_KEY`/`SHOPIFY_API_SECRET`, a `MERCHANT_APP_URL` Shopify can actually
  reach, `SHOPIFY_STUB_URL` **unset** (it silently redirects "live" Admin calls back to the
  offline stub), a `CHECKOUT_MODE` that is not the simulated `redirect` provider, and an
  `EMBEDDING_PROVIDER` that can really embed. It then refuses, naming what is unmet.

  **It always exits non-zero** — `2` when a precondition is unmet, `3` when they all hold and
  only the live driver is missing. That is deliberate: a command that runs nothing and reports
  success is the failure this whole page is meant to be free of. Nothing about the offline
  starting slice above needs any of it.

- **The merchant onboarding interview.** T-053 turns a plain-language interview into a
  drafted envelope, a written merchant approval and a versioned record, and its gate is a
  recorded transcript fixture rather than a model call. The live-LLM variant of that beat is
  opt-in and is flagged as such in the extension runbook; the starting slice never runs it.

- **The dishonest-store trust beat.** Watching a store's score fall until it is blacklisted
  takes a multi-episode simulation run, which is again the extension runbook's territory.

## Troubleshooting

- **`FATAL: run 'make bootstrap' first`** — `deps-up` found no virtualenv. Run `make
  bootstrap`.
- **Tests abort before collecting anything** — `PROXYSHOP_WORKER` is unset. Every pytest
  invocation needs it; a shell-prefix assignment does not survive an `&&` chain, so export it.
- **`compose datastore stack is not reachable`** — the datastores are down. Run `make
  deps-up`. Tests that need them skip rather than fail; the scripted S1 proof needs none of
  them and must never skip.
- **The stub returns 404 on a cart permalink** — the catalogue was not provisioned into the
  running stub. Run `make demo-seed` against the stub you actually started.
- **A checkout is refused as off-domain** — that is the guard working. Check that the
  seller's registered domain matches the host in the bid's checkout URL exactly; there are no
  subdomain wildcards and no suffix matching.
