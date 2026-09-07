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

Two things run on this page and they prove different amounts, so each bullet says which. **§3**
is the live driver a room watches; **§4** is the scripted proof. A claim marked §4 is real and
tested — it is simply not what the room sees.

- **S1** — intent → up to three clarifications → parallel bids → shortlist → accepted offer →
  code created and validated → checkout through the `CheckoutProvider` port → webhook and
  pixel reconciled → ledger event → trust update. *§3 runs it as far as the signed webhook and
  a verified hash chain; the reconciliation and trust-update tail is §4's, and §3 prints a
  `DOES NOT RUN YET` block saying so.*
- **C9** — no network egress. *True of §3 and §4, which is where it matters: neither reaches
  the public network. **Provisioning does**, once — `make bootstrap` runs `uv sync` and
  `npm ci`, and building or pulling the stub's image fetches a base image and wheels.*
- **C11** — the simulated redirect path emits the same canonical `LedgerEvent` kinds a Shopify
  checkout would, so the pixel and reconciliation obligations hold either way. *Neither run
  demonstrates this: both are `CHECKOUT_MODE=redirect`. It is held structurally — both
  providers build their events through the same `CheckoutProvider._events` — and pinned by
  the exchange's own checkout-provider suite ("C11: provider swap is invisible downstream").*
- **R10** — a store whose agent never answers is still represented, at its catalogue list
  price, and can still reach the shortlist. ***§3 measures this every run***, and prints the
  slot it took.
- **S8** — no blacklisted seller is eligible at any gate, and no checkout URL is ever returned
  off the seller's registered domain. *§3 shows the FIRST gate live and says so; the remaining
  gates and the four-way off-domain spoof matrix are §4's.*

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
on the `make deps-up` path reads the file itself — not the Makefile, not the database-init
script, not any Python at import — so the next step, straight after the copy, would fail with
`FATAL: PROXYSHOP_WORKER is unset`. (One script on this page does source it: `scripts/verify.sh`,
behind `make check`, `set -a`-sources `.env` on every invocation while shielding
`PROXYSHOP_WORKER`. `.env.example` records that an earlier revision claiming otherwise was
wrong.)

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

Confirm the whole toolchain is sound before going further. Budget several minutes: this runs
ruff, eslint, mypy, `tsc -b` and the full pytest suite, and it holds a machine-global Neo4j lock
for part of that, so it serialises against anything else running on this box.

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

It takes about two seconds and prints the whole journey as a narrative — the shopper's
words, the questions asked back, the store that is denied and why, each store's bid as it
arrives, the ranked shortlist with the reason each slot placed where it did, the real
single-use code, the permalink, the order the merchant closed, the pixel beacon, the
HMAC-signed webhook, and the hash-chained ledger the exchange wrote while all of that
happened. Every arrow it prints is a real HTTP round trip.

**Everything above this section except `make bootstrap` is optional for this command.** The
driver starts what it needs on loopback ports of its own — the buyer service, one store agent
per hosted store, the exchange, the trust service, the merchant stub, a pixel collector and a
webhook receiver — and touches no datastore, so `make deps-up`, the migration, the seeded
catalogue and the long-running stub are for the rest of this page, not for this beat. Nothing
it does reaches the public network.

The trust service is the one component the driver hands a substitution to, and it is a
datastore rather than a behaviour: an `InMemoryEventStore` is put on `app.state.event_store`,
the seam `trust.events.routes.store_for` reads before it reaches for Postgres. It is the same
append-and-seal path the Postgres writer takes, so the chain beat 7 verifies is the service's
own — and the "touches no datastore" sentence above stays true.

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

1. **Reconciliation and the trust projection cannot run over the ledger chain the exchange
   writes** — see 3.6/3.7. The chain itself is now real and is verified in front of you (3.8);
   what it does not yet carry is two of the three kinds `reconcile` needs.

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

The shopper's confirmation really does open an auction: `POST /buyer/intent/confirm` answers
201 with an `auction_id`, and the auction state machine writes `auction_opened`. **The auction
narrated from here on is a second one**, opened directly on the S1 run fixture's intent so that
this beat and the scripted proof in section 4 are about one purchase rather than two that
resemble each other. The driver prints both auction ids and says which is which, and it is why
the chain in 3.8 carries two `auction_opened`/`auction_closed` pairs.

Before any store is asked for a bid the seller eligibility source is read, and it fails closed:
a store whose status cannot be read is treated as unavailable, and a blacklisted store is denied
here — it is never asked, never collected, never ranked, never shown.

Point out that the blacklisted store appears in the response's `denied` list with its status and
reason. That is the **first** of the gates S1 makes it fail; the rest — never collected, never
ranked, never shown, never accepted, and never in the chain 3.8 reads back — are checked in one
pass by section 4.

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

**Section 4 exercises that grading; the live driver does not, and says so in beat 2.** The
exchange grades claims against a `catalog` key in its deployment document, and this driver
deliberately states none — that snapshot is the exchange's own evidence about a store, and a
demo supplying it would be marking the store's homework. Unstated,
`ranking.serving.catalog_of` keeps its `NoCatalogSnapshots` default, every claim grades
`unsupported`, and R19 will not let an unsupported claim satisfy a hard constraint. The live
shortlist is non-empty only because the fixture's intent carries `"hard_constraints": []`.

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
stores differ on the `trust` term alone. The numbers themselves are **stated by the deployment
document this driver writes**, not served by the trust service; which store gets the honest side
of the split is read from `fixtures/manifest.json`. The derivation is exactly what 3.7 says the
live beat cannot show.

A filled slot is a `shown` event in the frozen vocabulary — but nothing under `apps/`,
`packages/` or `services/` writes one. The scripted proof in section 4 emits its own from the
ranker's real slots, and the S1 flow suite under `e2e/` guards that: it searches the tree for a
producer of `shown`, `checkout_pixel` or `claim_verified` and turns red as soon as one appears. So
the chain 3.8 serves has no `shown` row in it, and that absence is measured rather than assumed.

### 3.5 Acceptance, the single-use code and the simulated redirect

The shopper accepts one slot. `exchange.accept.accept` re-checks eligibility, then resolves a
provider for `CHECKOUT_MODE`. In `redirect` that is `SimulatedRedirectProvider`, which mints
a single-use code locally and builds a cart permalink on the seller's **registered** domain —
the platform's record of that domain, not the domain the bid claimed for itself.

The host comparison is exact: a bid whose checkout URL points at `checkout.<seller-domain>`,
`evil-<seller-domain>` or `<seller-domain>@attacker.tld` is refused, and refused *before* a
discount code is created. **The live driver states that; it does not attempt it.** The four
spoofs are driven for real by section 4. The port **builds** `accepted`, `code_created`
and `checkout_redirect` and hands them back on `CheckoutResult.events` — but no served path
emits them: `exchange.accept.routes` reads the `accepted` one for its `offer` body and drops the
rest. The `accepted` row in the chain 3.8 serves is the state machine's own transition event,
not the port's. That is one half of why reconciliation still cannot run (3.6).

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

- `reconcile` needs three kinds — `accepted`, `checkout_pixel`, `order_paid` — and **two of
  the three have no producer on any served path**. `accepted` is present: it is the last row of
  the chain 3.8 serves, and it carries the offer and the token. The other two cannot be, because
  `AuctionStateMachine._transition` is the exchange's only caller of `ledger.record` **on a
  served path** (`retrieval.fit` holds the other one, the `bid_placed` producer, and no route
  reaches it), so a served run's chain is auction transitions and nothing else. `checkout_pixel`
  has no producer anywhere in the repository — `pixel/src/` holds one empty `.gitkeep`, and
  `merchant_svc.collector` stops at a `PixelObservation` in memory — and the verified
  `order_paid` goes into a bounded in-process hand-off buffer the module itself calls the seam a
  downstream lane replaces. A driver that manufactured those events would be supplying the
  evidence whose absence is the defect.
- The join is still unmade, and beat 7 *measures* it: it prints the token the exchange stamped
  into the ledger beside the token the merchant minted at the cart, and whether they match. They
  do not. In `redirect` mode no merchant is called at all — `SimulatedRedirectProvider` mints
  locally — and the `checkout_token` is invented right after the code is. It reaches the ledger;
  it never reaches the *merchant*, because the only thing handed to the shopper is a cart
  permalink carrying a variant, a quantity and the discount code. The merchant therefore mints
  its own when the cart is visited. `reconcile` no longer joins on that token alone: it also
  bridges an offer to an order through the single-use code, reading `code_created` and
  `checkout_redirect`. Those two are exactly the events the checkout port builds, hands back on
  `CheckoutResult.events`, and that no served path emits — `exchange.accept.routes` reads the
  `accepted` one for its `offer` body and drops the rest. `e2e/support/s1` writes them itself
  from real upstream data, which is what lets section 4 reach a `reconciled` verdict; the
  `checkout_token` binding it still carries is vestigial and its own docstring says so.

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

### 3.8 The chained ledger, read back off the trust service

The exchange's state machine records every auction transition into a ledger sink, and the only
thing its deployment document says about that sink is *where* it posts: one line, `trust_url`.
The driver states the trust service's own loopback address there and then asks a **different
application** what it received — `GET /events` and `GET /events/verify` on `apps/trust`, neither
of which is authenticated. (Different application, not different process: every server on this
page is a uvicorn instance in a thread of the one interpreter. What makes the read meaningful is
the socket and the separate app state, and both of those are real.)

What beat 7 prints is the chain itself: five events (`auction_opened`, `auction_closed`,
`auction_opened`, `auction_closed`, `accepted`), each one's `prev_hash` equal to its
predecessor's `event_hash`, followed by the service's verdict — `ok: true`, `verified: 5`,
`anchor_ok: true`, and the head hash. `anchor_ok` is the half worth pointing at: the chain's
length and head are recorded outside the row list, so a stream truncated to a shorter but
perfectly-linked prefix still fails verification. A flawless chain of the wrong length is still
a tampered one.

This is the beat that used to be impossible. Before T-150 the exchange's ledger was an
in-process list discarded with the app, and `apps/trust`'s hash chaining, once-only landing and
replay were grading a stream no served request produced. Running the demo with the trust service
unreachable is still honest and still visible: `proxyshop_support.trust_ledger` logs one `ERROR`
the moment events stop landing and one `INFO` the moment they land again, and
`TrustLedgerPublisher.status()` keeps the condition readable after the line scrolls away. That
level matters here — the demo's own gate under `docs/tests/` fails on any `ERROR` record, so
"the audit trail lands" is now a checked property of this command rather than a hope.

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

That table is what the *scripted proof* in section 4 produces. A **served** run produces three
kinds and no others: `auction_opened`, `auction_closed`, and an `accepted` — and that `accepted`
has a different provenance from the fifth row above, being the state machine's own transition
event rather than the checkout port's trio. Everything else in the table, `bid_placed` included,
has no emitter any route reaches. 3.8 is where the served chain is shown and verified.

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
