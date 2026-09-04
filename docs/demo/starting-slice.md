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

## 1. Bring up the stack

```bash
make bootstrap
make deps-up
```

`bootstrap` builds the virtualenv and installs the Node toolchain. `deps-up` starts
Postgres, Neo4j and Redis, waits for them to report healthy, and then initialises this
worker's database. The merchant stub is not started by `deps-up` — it carries the `e2e`
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

This is the beat to run in front of an audience. Each step names the component that does the
work, so a question about "what actually happened there" has an answer in the code.

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

Show the shortlist carrying both a real hosted bid and the silent store's list-price
fallback. Each filled slot is written to the ledger as `shown`.

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

### 3.7 The trust projection

The reconciliation outcome and the verification verdicts become observations against the six
trust dimensions — five transaction dimensions plus `catalog_claim_accuracy`, which is where
a product-fact claim lands. `trust.scoring.score` and `trust.snapshot.build_snapshot` fold
them in and serve an updated snapshot, which is the same snapshot the ranker filters on. That
closes the loop: what a store did last time changes what it can win next time.

## 4. Run the whole beat as one scripted proof

Everything in section 3 runs unattended, offline, in about a second and a half:

```bash
export PROXYSHOP_WORKER=0
uv run python -m pytest e2e/test_s1_flow.py -q
```

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
  root Makefile and shells out to `docs/demo/e2e_live.sh`, which lands with the extension
  runbook — until then the target exits rather than running anything, which is the correct
  behaviour for a procedure that is off this page's path.

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
