# ProxyShop — ticket ledger (open tickets + scheduling constraints)

> **Regenerated 2026-09-02 from `tickets.json`, at `main` = `9710f3e`.**
>
> `tickets.json` is the authoritative ticket graph. **This file is a derived view of it and
> nothing else.** When the two disagree the graph wins and this file is regenerated; the reverse
> — editing `tickets.json` to match this file — corrupts the authoritative source and must never
> be done.
>
> **Provenance note, because it matters for reading the numbers.** This regeneration was
> commissioned against the graph as it stood at `c43b014` — **82 tickets, 141 edges**. The graph
> advanced twice while the pass was running: `d1a73d3` (cycle-8 evaluation) and `9710f3e`, which
> minted **16 new HIGH tickets, T-135 – T-150**, from a rung-2 verification sweep. Every figure
> below is recomputed from the graph **as it is now, at `9710f3e` — 98 tickets, 141 edges**. The
> 82/141 pair is the superseded `c43b014` snapshot and appears nowhere else in this file.
>
> The revision this replaces had drifted far enough to be actively misleading. It was frozen at
> the cycle-0 / amendment-1 view (`c5c2aa7`), was **missing 39 tickets (T-112 … T-150)**, and
> stated the ticket count variously as 3, 16, 30, 44, 45 and 47 and the edge count as 80, 87 and
> 94 — none of which was ever the graph's size.

## Totals — measured from the graph, not remembered

| quantity | value |
|---|---|
| tickets in `tickets.json` | **98** |
| dependency edges | **141** |
| roots (empty `depends_on`) | **17** — T-000, and all sixteen of T-135 – T-150 |
| max depth | **10**, deepest **T-087** |
| depth-1 tickets (the *structural* initial frontier) | T-010, T-011, T-012, T-013, T-014, T-109, T-111 |
| graph health | acyclic, **0** dangling dependency references, **0** duplicate ids |
| **closed** — work landed on `main` | **39** |
| **open** — the contents of this ledger | **59** = **34 READY** + **25 BLOCKED** |
| open acceptance criteria | **151**, across the 43 open tickets that carry an `acceptance` array |
| open tickets with **no** acceptance array and **no** gate | **16** — T-135 – T-150 |
| frozen acceptance tests | **120** (121 `@pytest.mark.ticket` decorators; one test carries two), of which **83** sit on open tickets |
| open tickets with **no** frozen coverage at all | **34 of 59** |

The depth-1 row is a *structural* property — the tickets one edge from T-000. It is not the
dispatch frontier: five of its seven members are already closed, and the sixteen new roots are
not in it because they descend from nothing at all. The live dispatch set is
[The ready frontier](#the-ready-frontier--dispatch-from-here).

## How closed-ness was determined

**Ground truth is git.** A ticket is closed when its work has landed on `main`.

`tickets.json` carries **no status field of any kind**. The 82 original tickets have exactly
`id, title, objective, refs, acceptance, verify, scope, depends_on, non_goals, parallel_safe`;
the 16 new finding tickets have `id, title, verify, scope, depends_on, source, severity,
location, defect, finding_id`. Neither shape has a status, so nothing in the graph *can*
contradict git, and nothing does. Closure was reconstructed from the merge and wave-closure
commits on `main`:

| landed | tickets | evidence on `main` |
|---|---|---|
| cycle 0 | T-000 | `2b408cd` and its four `fix(T-000)` follow-ups |
| wave 1 | T-010, T-011, T-012, T-013, T-014 | `970a0ab` — *"merged T-010, T-011, T-012, T-013 and T-014 to main"* |
| wave 2 | T-100 – T-108, T-110 | `6791206`, plus one `fix`/`test` commit per ticket |
| wave 3 | T-112 – T-116, T-118 – T-122 | `8311b3c` — *"10 tickets closed"* — and the `262d3c8` close-out |
| wave 4 | T-125 – T-129 | `f45d47d` — *"T-125..T-129 merged"* |
| cycle 8 | T-020, T-030, T-036, T-040, T-050, T-060, T-070, T-080 | `c43b014` — seven F1 feature branches; acceptance 16/120 → 36/120 |

Nothing has landed since `c43b014`: `d1a73d3` and `9710f3e` are both ledger-only commits, so the
closed set is 39 and stands.

## Where the run's own prose contradicts the code on `main`

The graph has no status field to be wrong, but three **wave-closure commit messages** claim
closure the tree does not support. Git and the working tree were trusted over the prose, so all
three tickets below are carried as **OPEN**:

- **T-109 is open**, though `6791206` says wave 2 fixed *"11 defect tickets"* while naming and
  evidencing only ten (T-100 – T-108, T-110). T-109's targets have never been touched since
  T-000: `git log main -- proxyshop_support/reachability.py` returns exactly one commit,
  `2b408cd`. `reachability.skip_reason()` still returns a reason when **any** of the three
  endpoints is down, and `conftest.py`'s collection hook still applies that one reason to every
  `@pytest.mark.docker` test — verbatim the defect T-109 describes. Its acceptance 1
  ("reachability is per-service") is unmet.
- **T-111 and T-123 are open.** `8311b3c` narrates T-123's root cause and its repair, but the
  repair was made **in the environment** (a recursive `chflags` run by hand), not in the repo:
  `git log main -- scripts/bootstrap.sh` has no commit after the T-000 era. The script still
  chflags a **single** `.pth` path rather than site-packages recursively, still ends that step in
  `|| true`, and still ends its namespace assertion in `|| echo "    WARNING: …" >&2`. Those are
  T-111 acceptance 1–2 and T-123 acceptance 2 and 6, all unmet — and the flag will come back.
- **T-124 is open, and half-fixed in a way that reads as closed.** `b2abca6` changed
  `apps/trust/tests/test_schema_grants.py:1579` to `docker rm -f -v`, which stops that fixture's
  leak. The *other* throwaway-container fixture named in T-124's own verify command —
  `proxyshop_support/tests/test_role_password_end_to_end.py:459` — still runs
  `_docker("rm", "-f", name)` with no `-v` and still leaks one anonymous volume per run. T-124
  acceptance 1 says **every** such test, and acceptance 3 (a test that fails if the finalizer is
  removed) does not exist. This is the leak that filled the Docker VM disk to 100% mid-run and
  cost 254 hand-reclaimed volumes; it is not finished.

Nothing else in the graph disagrees with git.

## The ready frontier — dispatch from here

**34 of the 59 open tickets have every dependency closed.** This is the dispatchable set at
`9710f3e`; it is computed, not curated — an open ticket is READY iff every id in its
`depends_on` is in the closed set above. Sixteen of the thirty-four (T-135 – T-150) are READY
only because they are *roots*: they were minted with no dependencies at all.

*"Unblocks" counts the ticket's transitive open descendants — how many other open tickets stop
being blocked, eventually, because this one lands. It is the ordering signal, not the whole
answer: the scheduling constraints below veto co-scheduling that the ranking would allow.*

| ticket | unblocks | frozen tests | parallel_safe | title |
|---|---|---|---|---|

| **T-021** | 15 | 3 | no | Policy pages and marketing claims land in the graph with provenance |
| **T-041** | 13 | 2 | no | Advocate runtime bids within walls and defaults deterministically cold |
| **T-033** | 12 | 5 | no | Accepting an offer produces a validated code and permalink |
| **T-031** | 9 | 0 | yes | Candidate retrieval and fit scoring feed the ranker |
| **T-051** | 7 | 2 | no | Pixel reports checkout outcomes the collector can join |
| **T-071** | 5 | 3 | no | Three questions or fewer produce a confirmed structured intent |
| **T-053** | 3 | 3 | yes | A plain-language interview yields an approved, versioned envelope |
| **T-023** | 1 | 1 | yes | Catalog MCP adapter passes recorded-contract tests |
| **T-052** | 1 | 3 | no | Winning offers become single-use validated codes and permalinks |
| **T-109** | 1 | 0 | no | A datastore blip cannot silently empty the security gate |
| **T-111** | 1 | 0 | no | Provisioning fails loudly when the flat namespaces are dead |
| **T-022** | 0 | 1 | yes | Same products across stores link via entity resolution |
| **T-124** | 0 | 0 | no | Fresh-volume tests remove the containers and volumes they create |
| **T-130** | 0 | 0 | no | Sub-HIGH findings from the feature-wave checkers (backlog sweep, not a wave) |
| **T-131** | 0 | 0 | yes | The golden answer key is graded weakly, and the dishonest store's flagship lie is not graded at all |
| **T-132** | 0 | 0 | no | Rotating pseudonyms are trivially re-linkable, so T-070's central guarantee does not hold |
| **T-133** | 0 | 0 | yes | Buyer residue: shared session state, unwired publish half, and unbounded stores |
| **T-134** | 0 | 0 | yes | Merchant residue: a scope guard that shreds strings, a token in repr, and six inbox findings that never reached the lane |
| **T-135** | 0 | 0 | — | The exclusivity property is defeated by moving the payload one level down into the Offer. |
| **T-136** | 0 | 0 | — | Every runtime capability the branch adds is unwired: ToolHooks and its six hooks, … |
| **T-137** | 0 | 0 | — | refresh_digests() re-pins approval.content_hash onto whatever the manifest body currently … |
| **T-138** | 0 | 0 | — | build_buckets is a pure, deterministic function of the account with no k-anonymity floor, … |
| **T-139** | 0 | 0 | — | identity_leaks holds every slug of the account's OWN order categories out of the … |
| **T-140** | 0 | 0 | — | AccountDirectory has exactly one implementation and no production populator. |
| **T-141** | 0 | 0 | — | The magic-link token is delivered to the default no-op `_drop` (line 166) in every … |
| **T-142** | 0 | 0 | — | publish_profile — the only writer of app.buyer_accounts, described as 'the store-visible … |
| **T-143** | 0 | 0 | — | A replayer can relabel a signed delivery onto a topic of its choice AND destroy the … |
| **T-144** | 0 | 0 | — | merchant-svc has no deployable. |
| **T-145** | 0 | 0 | — | R10's "hard timeout" is measured from the moment solicit_bids constructs its … |
| **T-146** | 0 | 0 | — | The hard timeout is bought by abandoning the ThreadPoolExecutor (`shutdown(wait=False, … |
| **T-147** | 0 | 0 | — | T-036 — the whole second half of this lane — is unwired. |
| **T-148** | 0 | 0 | — | `configure_auctions` — the only way to give the exchange a real solicitor, a real … |
| **T-149** | 0 | 0 | — | A connection failure never becomes a StoreUnavailable, so an unreachable or restarting … |
| **T-150** | 0 | 0 | — | The ledger writer has zero producers. |

**Read the frontier with five constraints in hand, in this order:**

1. **The sixteen T-135 – T-150 findings are all HIGH and none of them has a gate.** Every one
   carries `verify: false  # NO GATE YET — write one that fails on this finding first`. Their
   unblock count is 0 by construction (they are leaf roots), which ranks them last in the table
   and is exactly the wrong way to read them: they are the rung-2 verifier's HIGH findings
   against the seven feature branches that landed at `c43b014`, and several assert that a
   headline capability of a *closed* ticket is unwired in production. **A ticket whose verify is
   `false` cannot be closed by running it — the first work in each lane is writing the gate that
   goes red, then making it green.**
2. **Four of the highest-unblock tickets are graph writers and they serialize against each
   other.** T-021, T-022, T-023 and T-031 all write the one shared Neo4j database. At most one
   may be in flight, whatever their unblock counts say. T-021 (unblocks 15) and T-031
   (unblocks 9) are the two most valuable members of that queue.
3. **T-041 and T-033 are the highest-value non-graph work and are mutually safe.** T-041
   (`packages/store-agent/**`, unblocks 13) and T-033 (`apps/exchange/**`, unblocks 12) share no
   scope with each other or with any graph lane, and both sit directly under closed parents.
   Note T-033's scope contains the new **T-145**, so those two serialize.
4. **The three READY infra tickets overlap and must go one at a time.** T-109, T-111 and T-124
   are all READY and all three defects are live in the tree right now (see *Where the run's own
   prose contradicts the code*). This run has already spent five `build_succeeds` zero-readings
   and 254 hand-reclaimed Docker volumes on exactly these. They share `proxyshop_support/**` and
   `conftest.py` with each other and with the blocked T-117/T-123.
5. **The five cycle-8 residue tickets are scope supersets, not siblings.** T-130's scope contains
   **38** other open tickets, T-133 contains 17, T-132 contains 10, T-131 and T-134 contain 7
   each. They cannot ride alongside the lanes they enclose. Of the five, **T-132 carries the
   HIGH**: it voids T-070's central privacy guarantee, and its frozen-contract amendment — which
   the user approved in principle — had its design v1 refuted at `bda0ce0` and recorded rather
   than applied. T-138, T-139 and T-142 are the same defect class arriving one layer down, in
   `apps/buyer/svc/src/profile/__init__.py`.

---

# Open tickets — 59

Grouped by epic, then by the two defect cohorts. Every entry is generated from `tickets.json`:
`depends_on` is verbatim from the graph, annotated ✅ closed / ⛔ open. **READY** means every
dependency is closed. Objectives are trimmed to one line; the graph holds the full text, the
acceptance criteria, the refs and the non-goals.

### E2 — Ingestion

*4 open · 3 ready · 1 blocked*

#### T-021 — Policy pages and marketing claims land in the graph with provenance

Fetch shipping/return/guarantee pages; Haiku-class extraction on hash change; decompose to atomic Claims; upsert with Source(source_class=scraped) and snapshot refs.

- **State:** **READY**
- **depends_on:** T-014 ✅, T-020 ✅
- **Unblocks (open):** 15 — T-024, T-032, T-034, T-035, T-054, T-062, T-063, T-064, T-065, T-073, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** 3 frozen tests
- **verify:** `pytest services/ingest/tests/test_extraction.py -q`
- **Scope:** `services/ingest/src/extraction/**`, `services/ingest/tests/**`, `fixtures/pages/**` · `parallel_safe: false`

#### T-022 — Same products across stores link via entity resolution

GTIN exact match plus embedding+attribute matcher producing SAME_AS edges with confidence; threshold config.

- **State:** **READY**
- **depends_on:** T-012 ✅, T-020 ✅
- **Unblocks (open):** 0
- **Acceptance:** 2 criteria · **Frozen:** 1 frozen test
- **verify:** `pytest services/ingest/tests/test_entity_resolution.py -q`
- **Scope:** `services/ingest/src/er/**`, `services/ingest/tests/**`, `fixtures/er/**` · `parallel_safe: true`

#### T-023 — Catalog MCP adapter passes recorded-contract tests

catalog_mcp CatalogAdapter implementing the production-primary read path against recorded mocks (dev stores are not in Global Catalog — A1).

- **State:** **READY**
- **depends_on:** T-020 ✅
- **Unblocks (open):** 1 — T-024
- **Acceptance:** 2 criteria · **Frozen:** 1 frozen test
- **verify:** `pytest services/ingest/tests/test_catalog_mcp.py -q`
- **Scope:** `services/ingest/src/adapters/**`, `services/ingest/tests/**`, `fixtures/mcp/**` · `parallel_safe: true`

#### T-024 — Differential refresh keeps the graph current at field-appropriate cadence

Scheduler with per-field cadence config; only changed content re-extracts; refresh endpoint.

- **State:** **BLOCKED** by T-021, T-023
- **depends_on:** T-021 ⛔, T-023 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest services/ingest/tests/test_refresh.py -q`
- **Scope:** `services/ingest/src/scheduler/**`, `services/ingest/tests/**` · `parallel_safe: false`

### E3 — Exchange

*5 open · 2 ready · 3 blocked*

#### T-031 — Candidate retrieval and fit scoring feed the ranker

Vector+attribute retrieval from Neo4j, rerank behind an interface (deterministic double in tests), fit score per candidate logged per bid.

- **State:** **READY**
- **depends_on:** T-012 ✅, T-030 ✅
- **Unblocks (open):** 9 — T-032, T-034, T-035, T-054, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest apps/exchange/tests/test_retrieval.py -q`
- **Scope:** `apps/exchange/src/retrieval/**`, `apps/exchange/tests/**` · `parallel_safe: true`

#### T-032 — Shortlists rank by the single published formula behind eligibility filters

Eligibility filters (blacklist fail-closed, offer expiry, checkout-domain validity, hard constraints requiring verified facts per R19), then published rank_score per DESIGN §Decisions with [0,1]-normalized …

- **State:** **BLOCKED** by T-031, T-033, T-065
- **depends_on:** T-031 ⛔, T-033 ⛔, T-065 ⛔
- **Unblocks (open):** 8 — T-034, T-035, T-054, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 4 criteria · **Frozen:** 10 frozen tests
- **verify:** `pytest apps/exchange/tests/test_ranking.py -q`
- **Scope:** `apps/exchange/src/ranking/**`, `apps/exchange/tests/**` · `parallel_safe: false`

#### T-033 — Accepting an offer produces a validated code and permalink

Accept endpoint resolves a CheckoutProvider (T-036) by CHECKOUT_MODE and delegates code creation to it, returns the permalink the provider mints, records the accepted event, and handles code-creation failure …

- **State:** **READY**
- **depends_on:** T-030 ✅, T-036 ✅
- **Unblocks (open):** 12 — T-032, T-034, T-035, T-054, T-072, T-073, T-081, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 5 criteria · **Frozen:** 5 frozen tests
- **verify:** `pytest apps/exchange/tests/test_accept.py -q`
- **Scope:** `apps/exchange/src/accept/**`, `apps/exchange/src/orchestration/**`, `apps/exchange/tests/**` · `parallel_safe: false`

#### T-034 — Exchange bandit shifts exposure with outcomes and honors exploration

Contextual Thompson sampling over cluster×store adjusting exposure/exploration (not formula weights); guaranteed exploration slice for low-data stores from trust snapshot.

- **State:** **BLOCKED** by T-032, T-064
- **depends_on:** T-032 ⛔, T-064 ⛔
- **Unblocks (open):** 3 — T-083, T-084, T-087
- **Acceptance:** 3 criteria · **Frozen:** 3 frozen tests
- **verify:** `pytest apps/exchange/tests/test_bandit.py -q`
- **Scope:** `apps/exchange/src/policy/**`, `apps/exchange/tests/**` · `parallel_safe: false`

#### T-035 — Loss reports aggregate reasons without leaking amounts

Windowed job producing LossReport per store: reason categories from the ranking formula's dominant term, unmet criteria for fit losses, delayed release.

- **State:** **BLOCKED** by T-032
- **depends_on:** T-032 ⛔
- **Unblocks (open):** 1 — T-054
- **Acceptance:** 3 criteria · **Frozen:** 1 frozen test
- **verify:** `pytest apps/exchange/tests/test_loss_reports.py -q`
- **Scope:** `apps/exchange/src/reports/**`, `apps/exchange/tests/**` · `parallel_safe: true`

### E4 — Store agent

*5 open · 1 ready · 4 blocked*

#### T-041 — Advocate runtime bids within walls and defaults deterministically cold

BidRequest→Bid|decline: cache-layout context assembly, hook-only claim emission, deterministic cold-start bid (list price + standing commitments + intro rule only). CONSTRAINT INHERITED FROM T-040 (recorded …

- **State:** **READY**
- **depends_on:** T-014 ✅, T-040 ✅
- **Unblocks (open):** 13 — T-042, T-043, T-044, T-045, T-054, T-063, T-073, T-082, T-083, T-084, T-085, T-086, T-087
- **Acceptance:** 3 criteria · **Frozen:** 2 frozen tests
- **verify:** `pytest packages/store-agent/tests/test_runtime.py -q`
- **Scope:** `packages/store-agent/src/runtime/**`, `packages/store-agent/tests/**` · `parallel_safe: false`

#### T-042 — Store loop learns from its own outcomes only

Per-store Thompson sampling over discount-depth buckets × commitment sets per cluster; network-prior intake; prior builder consumes pitch/value-prop outcomes only.

- **State:** **BLOCKED** by T-041
- **depends_on:** T-041 ⛔
- **Unblocks (open):** 3 — T-083, T-084, T-087
- **Acceptance:** 3 criteria · **Frozen:** 3 frozen tests
- **verify:** `pytest packages/store-agent/tests/test_learning.py -q`
- **Scope:** `packages/store-agent/src/learning/**`, `packages/store-agent/tests/**` · `parallel_safe: true`

#### T-043 — Shadow mode logs would-be bids and trust events adjust the agent

Shadow activation gates submission but logs full bids+rationale to sealed store state; TrustEventPayload intake adjusts policy/commitment posture and is visible in rationale.

- **State:** **BLOCKED** by T-041
- **depends_on:** T-041 ⛔
- **Unblocks (open):** 4 — T-054, T-063, T-073, T-086
- **Acceptance:** 3 criteria · **Frozen:** 3 frozen tests
- **verify:** `pytest packages/store-agent/tests/test_shadow_trust.py -q`
- **Scope:** `packages/store-agent/src/modes/**`, `packages/store-agent/tests/**` · `parallel_safe: true`

#### T-044 — External bids enter signed with a required envelope and route to verification, not rejection

Signature registration + verification on the external submission door POST /v1/auctions/{auction_id}/bids for non-hosted agents; schema validation admits seller_asserted claims and free-text message, marks the …

- **State:** **BLOCKED** by T-041
- **depends_on:** T-010 ✅, T-011 ✅, T-041 ⛔
- **Unblocks (open):** 1 — T-045
- **Acceptance:** 7 criteria · **Frozen:** 8 frozen tests
- **verify:** `pytest packages/store-agent/tests/test_external_bids.py -q`
- **Scope:** `packages/store-agent/src/external/**`, `packages/store-agent/tests/**` · `parallel_safe: true`

#### T-045 — Three seller personas exercise the external door adversarially

apps/seller-reference: value / specialist / aggressive personas with identity, catalog scope, tone, and offer policy; free-text pitches with asserted claims via the external /bid path; the aggressive persona …

- **State:** **BLOCKED** by T-044
- **depends_on:** T-014 ✅, T-044 ⛔, T-080 ✅
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** 1 frozen test
- **verify:** `pytest apps/seller-reference -q`
- **Scope:** `apps/seller-reference/**` · `parallel_safe: true`

### E5 — Merchant

*4 open · 3 ready · 1 blocked*

#### T-051 — Pixel reports checkout outcomes the collector can join

Web pixel extension subscribing standard events; POST via fetch keepalive to collector with clientId, checkout token, order id, discountApplications; collector persists to app schema and forwards LedgerEvents.

- **State:** **READY**
- **depends_on:** T-050 ✅
- **Unblocks (open):** 7 — T-061, T-081, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** 2 frozen tests
- **verify:** `npx vitest run pixel && pytest apps/merchant/svc/tests/test_collector.py -q`
- **Scope:** `pixel/**`, `apps/merchant/svc/src/collector/**`, `apps/merchant/svc/tests/**` · `parallel_safe: false`

#### T-052 — Winning offers become single-use validated codes and permalinks

The Shopify adapter behind T-036's CheckoutProvider port — one implementation of it, not the only route to a code. /codes: discountCodeBasicCreate usageLimit:1 + expiry, validity + combinesWith pre-check, …

- **State:** **READY**
- **depends_on:** T-036 ✅, T-050 ✅
- **Unblocks (open):** 1 — T-054
- **Acceptance:** 4 criteria · **Frozen:** 3 frozen tests
- **verify:** `pytest apps/merchant/svc/tests/test_codes.py -q`
- **Scope:** `apps/merchant/svc/src/codes/**`, `apps/merchant/svc/tests/**` · `parallel_safe: false`

#### T-053 — A plain-language interview yields an approved, versioned envelope

Onboarding interview service: LLM interview (double in tests, transcript fixture), envelope draft in plain English, merchant written approval gate, versioned persistence to sealed schema; shadow default.

- **State:** **READY**
- **depends_on:** T-014 ✅, T-050 ✅
- **Unblocks (open):** 3 — T-054, T-086, T-087
- **Acceptance:** 3 criteria · **Frozen:** 3 frozen tests
- **verify:** `pytest apps/merchant/svc/tests/test_onboarding.py -q`
- **Scope:** `apps/merchant/svc/src/onboarding/**`, `apps/merchant/svc/tests/**`, `fixtures/interviews/**`, `apps/merchant/svc/src/envelope/**` · `parallel_safe: true`

#### T-054 — The dashboard shows the walls and the window

Merchant dashboard: bid log with rationale, loss reports, trust breakdown + event payloads, envelope editor (versioned), kill switch wired to agent mode. Includes verification outcomes per bid (statuses + …

- **State:** **BLOCKED** by T-035, T-043, T-052, T-053, T-063
- **depends_on:** T-035 ⛔, T-043 ⛔, T-052 ⛔, T-053 ⛔, T-063 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `npx vitest run apps/merchant/app/dashboard`
- **Scope:** `apps/merchant/app/dashboard/**` · `parallel_safe: false`

### E6 — Trust

*5 open · 0 ready · 5 blocked*

#### T-061 — Webhook truth reconciles lossy pixel signals

Reconciliation: match checkout_pixel↔order_paid by join keys; emit reconciled events; derive price_honored/discount_honored comparisons from webhook data only.

- **State:** **BLOCKED** by T-051
- **depends_on:** T-051 ⛔, T-060 ✅
- **Unblocks (open):** 5 — T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** 3 frozen tests
- **verify:** `pytest apps/trust/tests/test_reconciliation.py -q`
- **Scope:** `apps/trust/src/reconcile/**`, `apps/trust/tests/**` · `parallel_safe: false`

#### T-062 — Trust merges verification and outcome observations against the approved manifest

Merged observation framework over SIX dimensions (price_honored, discount_honored, shipped_on_time, not_returned, feedback_match, catalog_claim_accuracy) inside ONE trust system (D53): per-dimension Beta with …

- **State:** **BLOCKED** by T-065
- **depends_on:** T-060 ✅, T-065 ⛔, T-080 ✅
- **Unblocks (open):** 10 — T-034, T-054, T-063, T-064, T-073, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 6 criteria · **Frozen:** 11 frozen tests
- **verify:** `pytest apps/trust/tests/test_scoring.py -q`
- **Scope:** `apps/trust/src/scoring/**`, `apps/trust/tests/**` · `parallel_safe: false`

#### T-063 — Trust events reach the store agent and buyers close the loop

TrustEventPayload push to store-agent intake; feedback flow: routed-buyer-only prompt, buyer-track-record weighting, cross-check against return behavior.

- **State:** **BLOCKED** by T-043, T-062
- **depends_on:** T-043 ⛔, T-062 ⛔
- **Unblocks (open):** 2 — T-054, T-073
- **Acceptance:** 3 criteria · **Frozen:** 3 frozen tests
- **verify:** `pytest apps/trust/tests/test_feedback_push.py -q`
- **Scope:** `apps/trust/src/feedback/**`, `apps/trust/tests/**` · `parallel_safe: false`

#### T-064 — Exchange consumes one snapshot shape for trust and exploration

TrustSnapshot API incl. blacklist + low-data flags for the exploration slice; versioned; exchange client. The served shape carries all six dimensions incl. catalog_claim_accuracy (D53) and is the only trust …

- **State:** **BLOCKED** by T-062
- **depends_on:** T-062 ⛔
- **Unblocks (open):** 4 — T-034, T-083, T-084, T-087
- **Acceptance:** 3 criteria · **Frozen:** 1 frozen test
- **verify:** `pytest apps/trust/tests/test_snapshot.py -q`
- **Scope:** `apps/trust/src/snapshot/**`, `apps/trust/tests/**` · `parallel_safe: true`

#### T-065 — A golden pitch yields all four verification statuses with evidence

packages/verification + trust-service wiring: atomic typed claim extraction with source spans (LLM behind strict schemas, doubles in tests), seller/SKU/variant resolution (ambiguous on failure), unit/type …

- **State:** **BLOCKED** by T-021
- **depends_on:** T-010 ✅, T-021 ⛔, T-060 ✅, T-080 ✅
- **Unblocks (open):** 13 — T-032, T-034, T-035, T-054, T-062, T-063, T-064, T-073, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 4 criteria · **Frozen:** 6 frozen tests
- **verify:** `pytest packages/verification apps/trust/tests/test_verification.py -q`
- **Scope:** `packages/verification/**`, `apps/trust/src/verification/**`, `apps/trust/tests/**` · `parallel_safe: false`

### E7 — Buyer

*3 open · 1 ready · 2 blocked*

#### T-071 — Three questions or fewer produce a confirmed structured intent

Buyer agent: clarification loop capped at 3, Intent construction + buyer confirmation UI, profile bucket builder with k-floor config.

- **State:** **READY**
- **depends_on:** T-014 ✅, T-070 ✅
- **Unblocks (open):** 5 — T-072, T-073, T-082, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** 3 frozen tests
- **verify:** `pytest apps/buyer/svc/tests/test_intent.py -q && npx vitest run apps/buyer/app/intent`
- **Scope:** `apps/buyer/svc/src/intent/**`, `apps/buyer/app/intent/**`, `apps/buyer/svc/tests/**`, `fixtures/dialogues/**` · `parallel_safe: false`

#### T-072 — Shortlists render with provenance and accept hands off cleanly

Shortlist UI: slots, trust indicator, provenance labels ('store-confirmed' vs 'from their website'); accept → exchange accept → redirect to permalink.

- **State:** **BLOCKED** by T-033, T-071
- **depends_on:** T-033 ⛔, T-071 ⛔
- **Unblocks (open):** 4 — T-073, T-082, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** 2 frozen tests
- **verify:** `npx vitest run apps/buyer/app/shortlist && pytest apps/buyer/svc/tests/test_accept_flow.py -q`
- **Scope:** `apps/buyer/app/shortlist/**`, `apps/buyer/svc/src/accept/**`, `apps/buyer/svc/tests/**` · `parallel_safe: false`

#### T-073 — Routed buyers can answer one structured feedback prompt

Post-purchase feedback UI + intake wiring to trust feedback API; one prompt, structured options, no free text.

- **State:** **BLOCKED** by T-063, T-072
- **depends_on:** T-063 ⛔, T-072 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** 2 frozen tests
- **verify:** `npx vitest run apps/buyer/app/feedback && pytest apps/buyer/svc/tests/test_feedback.py -q`
- **Scope:** `apps/buyer/app/feedback/**`, `apps/buyer/svc/src/feedback/**`, `apps/buyer/svc/tests/**` · `parallel_safe: true`

### E8 — Proofs, fixtures and runbooks

*7 open · 0 ready · 7 blocked*

#### T-081 — Simulated buyers exercise the whole network from one seed

services/sim: seeded traffic generator driving intents→auctions→acceptances→stub purchases/returns; dishonest-store script executes manifest behaviors.

- **State:** **BLOCKED** by T-033, T-051
- **depends_on:** T-033 ⛔, T-051 ⛔, T-080 ✅
- **Unblocks (open):** 5 — T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** 1 frozen test
- **verify:** `pytest services/sim -q`
- **Scope:** `services/sim/**` · `parallel_safe: false`

#### T-082 — One scripted run proves the full S1 flow

E2E test: intent→clarify→bids→shortlist→accept→code→stub checkout→pixel+webhook→reconcile→ledger→trust update, all against compose.

- **State:** **BLOCKED** by T-061, T-072, T-081, T-032, T-041, T-062
- **depends_on:** T-061 ⛔, T-072 ⛔, T-081 ⛔, T-032 ⛔, T-041 ⛔, T-062 ⛔
- **Unblocks (open):** 2 — T-085, T-087
- **Acceptance:** 4 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest e2e/test_s1_flow.py -q`
- **Scope:** `e2e/**` · `parallel_safe: false`

#### T-083 — Both learning loops demonstrably move under seeded outcomes

Simulation assertions for S4: outcome shifts reorder shortlists in-cluster (exchange loop); a store's discount-depth distribution tracks its own record (store loop).

- **State:** **BLOCKED** by T-034, T-042, T-081, T-061
- **depends_on:** T-034 ⛔, T-042 ⛔, T-081 ⛔, T-061 ⛔
- **Unblocks (open):** 2 — T-084, T-087
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest e2e/test_learning.py -q`
- **Scope:** `e2e/**` · `parallel_safe: false`

#### T-084 — The dishonest store ends below threshold and off the shortlist

Episode test for S2: run manifest budget of simulated episodes; assert trust trajectory matches approved manifest expectations, ends blacklisted, and store disappears from subsequent shortlists.

- **State:** **BLOCKED** by T-062, T-083
- **depends_on:** T-062 ⛔, T-083 ⛔
- **Unblocks (open):** 1 — T-087
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest e2e/test_dishonest.py -q`
- **Scope:** `e2e/**` · `parallel_safe: false`

#### T-085 — The starting-slice demo is a runbook anyone on the team can execute

docs/demo/starting-slice.md: the offline starting-path demo procedure - compose bring-up against services/shopify-stub, `make demo-seed`, and the live-auction demo beat end to end through …

- **State:** **BLOCKED** by T-082
- **depends_on:** T-082 ⛔
- **Unblocks (open):** 1 — T-087
- **Acceptance:** 4 criteria · **Frozen:** 2 frozen tests
- **verify:** `pytest docs/tests/test_runbook.py -q`
- **Scope:** `docs/demo/starting-slice.md`, `docs/tests/**` · `parallel_safe: true`

#### T-086 — Onboarding drives a shadow store to its first real bid

An integration test walks the whole S6 seam in one run: a mocked interview transcript produces an economic envelope, the merchant approves it in writing, the store agent runs in shadow mode logging would-be …

- **State:** **BLOCKED** by T-043, T-053
- **depends_on:** T-043 ⛔, T-053 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 4 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest e2e/test_onboarding.py -q`
- **Scope:** `e2e/test_onboarding.py`, `e2e/support/onboarding/**` · `parallel_safe: false`

#### T-087 — The Shopify and onboarding extension runbook covers the beats off the starting path

docs/demo/shopify-onboarding-extension.md: the extension-lane demo procedure - dev-store provisioning (app install, storefront password, Bogus Gateway), the `make e2e-live` procedure against seeded dev stores, …

- **State:** **BLOCKED** by T-053, T-084, T-085
- **depends_on:** T-053 ⛔, T-084 ⛔, T-085 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 5 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest docs/tests/test_runbook.py -q`
- **Scope:** `docs/demo/shopify-onboarding-extension.md` · `parallel_safe: true`

### Infrastructure and defect tickets (T-100 – T-134)

*10 open · 8 ready · 2 blocked*

#### T-109 — A datastore blip cannot silently empty the security gate

conftest.py:105-121 and proxyshop_support/reachability.py:36-46,64-70: skip_reason() probes Postgres, Neo4j AND Redis, and if any one fails every @pytest.mark.docker test skips. That is 47 of T-011's 110 gate …

- **State:** **READY**
- **depends_on:** T-000 ✅
- **Unblocks (open):** 1 — T-117
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest apps/trust -q && pytest proxyshop_support -q`
- **Scope:** `conftest.py`, `proxyshop_support/**` · `parallel_safe: false`

#### T-111 — Provisioning fails loudly when the flat namespaces are dead

scripts/bootstrap.sh:26-33 un-hides the uv-written _proxyshop.pth (macOS UF_HIDDEN, which site.addpackage silently skips) but ends in '|| true', and its namespace assertion at :48-51 ends in '|| echo WARNING …

- **State:** **READY**
- **depends_on:** T-000 ✅
- **Unblocks (open):** 1 — T-123
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/bootstrap.sh && ./scripts/verify.sh check`
- **Scope:** `scripts/bootstrap.sh` · `parallel_safe: false`

#### T-117 — [BLOCKED: protected path] The per-ticket gate runs the tests that grade its own acceptance

BLOCKED — not dispatchable as scoped. The fix requires editing scripts/verify.sh:105, which is in state.json protected_paths ('.swarm-loop/acceptance', '.swarm-loop/goals.json', 'Makefile', …

- **State:** **BLOCKED** by T-109
- **depends_on:** T-109 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/verify.sh check`
- **Scope:** `scripts/verify.sh`, `conftest.py`, `proxyshop_support/**` · `parallel_safe: false`

#### T-123 — The .pkgroot namespaces survive a pytest run (root cause: site-packages itself is flagged)

`_proxyshop.pth` in the primary checkout keeps reverting to macOS UF_HIDDEN, which `site.addpackage` silently skips, so every .pkgroot flat namespace (contracts, llm, trust, ...) becomes unimportable outside …

- **State:** **BLOCKED** by T-111
- **depends_on:** T-111 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 6 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/bootstrap.sh && ./.venv/bin/python -m pytest packages/llm -q && ./.venv/bin/python -c 'import contracts, llm, trust'`
- **Scope:** `scripts/bootstrap.sh`, `conftest.py`, `proxyshop_support/**` · `parallel_safe: false`

#### T-124 — Fresh-volume tests remove the containers and volumes they create

The run exhausted its own infrastructure. The Docker VM's disk — where BOTH Postgres and Neo4j write — is 58.4G with 27.0M free (100%), while the macOS host has 398Gi free; measuring the host is measuring the …

- **State:** **READY**
- **depends_on:** T-112 ✅
- **Unblocks (open):** 0
- **Acceptance:** 4 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest apps/trust/tests/test_schema_grants.py proxyshop_support/tests/test_role_password_end_to_end.py -q`
- **Scope:** `apps/trust/**`, `proxyshop_support/**` · `parallel_safe: false`

#### T-130 — Sub-HIGH findings from the feature-wave checkers (backlog sweep, not a wave)

Recorded so nothing is lost, per the user's standing instruction: report every severity, but only HIGH and above interrupts the build. These are the MEDIUM findings the five feature-lane checkers raised …

- **State:** **READY**
- **depends_on:** T-030 ✅, T-040 ✅, T-050 ✅, T-070 ✅, T-080 ✅
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/verify.sh check`
- **Scope:** `apps/exchange/**`, `packages/store-agent/**`, `apps/merchant/**`, `apps/buyer/**`, `fixtures/**` · `parallel_safe: false`

#### T-131 — The golden answer key is graded weakly, and the dishonest store's flagship lie is not graded at all

Raised by the T-080 lane's own adversarial pass, reported rather than fixed because closing them exceeded that lane's cheap scope. These matter more than their severity labels suggest: the golden set is the …

- **State:** **READY**
- **depends_on:** T-080 ✅
- **Unblocks (open):** 0
- **Acceptance:** 4 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/verify.sh check`
- **Scope:** `fixtures/**` · `parallel_safe: true`

#### T-132 — Rotating pseudonyms are trivially re-linkable, so T-070's central guarantee does not hold

amend the frozen contract. Note the amendment number is now 4, not 3 — a separate amendment 3 (build_succeeds logging) landed on main at f65816a. [HIGH] T-070's objective is 'rotating pseudonyms, identity-free …

- **State:** **READY**
- **depends_on:** T-070 ✅
- **Unblocks (open):** 0
- **Acceptance:** 4 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/verify.sh check`
- **Scope:** `apps/buyer/**` · `parallel_safe: false`

#### T-133 — Buyer residue: shared session state, unwired publish half, and unbounded stores

Reported by the T-070 lane, not fixed because each crosses its scope or needs shared infrastructure. The lane DID ship a loud refusal for the first item — `build_auth_service` now refuses to start when …

- **State:** **READY**
- **depends_on:** T-070 ✅
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/verify.sh check`
- **Scope:** `apps/buyer/**`, `packages/**` · `parallel_safe: true`

#### T-134 — Merchant residue: a scope guard that shreds strings, a token in repr, and six inbox findings that never reached the lane

findings #6-#11 below were produced by an adversarial verifier the T-050 lane spawned. The verifier's report reached the ORCHESTRATOR but NOT the lane — the lane received only a summary line, requested a …

- **State:** **READY**
- **depends_on:** T-050 ✅
- **Unblocks (open):** 0
- **Acceptance:** 4 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/verify.sh check`
- **Scope:** `apps/merchant/**` · `parallel_safe: true`

### Rung-2 HIGH findings, minted at `9710f3e` (T-135 – T-150) — none has a gate yet

*16 open · 16 ready · 0 blocked*

#### T-135 — The exclusivity property is defeated by moving the payload one level down into the Offer.

The exclusivity property is defeated by moving the payload one level down into the Offer. enforce_hook_provenance inspects only the flat iterable it is handed; nothing in the branch binds Offer.discount.value …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `packages/store-agent/src/hooks/provenance.py:395` · **Finding:** `b31093db765b11056ce1f8bcbd67b288a94cfe35ff567716cbf4fb135fff3ba5`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** scratchpad probe3.py: hooks.authorize_discount('prod-cap', 25.0) returns Denied(over_max_discount_pct, limit 20.0). Build a Bid that passes contracts.Bid.model_validate with …
- **Scope:** `packages/store-agent/src/hooks/provenance.py`

#### T-136 — Every runtime capability the branch adds is unwired: ToolHooks and its six hooks, start_bid, would_authorize, …

Every runtime capability the branch adds is unwired: ToolHooks and its six hooks, start_bid, would_authorize, Denied, HookCall, HookInputError, enforce_hook_provenance, ClaimScopeError, mint_claim, …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `packages/store-agent/src/hooks/tools.py:161` · **Finding:** `0191c9de8c2b4adb7710890267da6f9aca0161cfd695f317300b52135893cf02`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** codegraph explore "who imports or calls ToolHooks, enforce_hook_provenance, mint_claim, hosted_claim_construction_offenders" lists callers only in packages/store-agent/src/hooks/__init__.py and packages/store-agent/tests/test_hooks.py (+ …
- **Scope:** `packages/store-agent/src/hooks/tools.py`

#### T-137 — refresh_digests() re-pins approval.content_hash onto whatever the manifest body currently says while leaving …

refresh_digests() re-pins approval.content_hash onto whatever the manifest body currently says while leaving approver / approved_at / artifact populated. This defeats the exact tamper-evidence property the …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `fixtures/manifest/__init__.py:383` · **Finding:** `49de220d5e6ca84e70814a65421c05aa882f02d8dc1bfa115595ab730caf5403`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** cp fixtures/manifest.json $SCRATCH/m2.json; then in .venv/bin/python: set m['approval'] = {status:'approved', approver:'Hank Holcomb', approved_at:'2026-09-02T12:00:00Z', artifact:'fixtures/approval/REQUEST-manifest-approval.md'} and write …
- **Scope:** `fixtures/manifest/__init__.py`

#### T-138 — build_buckets is a pure, deterministic function of the account with no k-anonymity floor, suppression, or …

build_buckets is a pure, deterministic function of the account with no k-anonymity floor, suppression, or generalisation of any kind, so a rotated pseudonym publishes a byte-identical bucket tuple and every …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/buyer/svc/src/profile/__init__.py:274` · **Finding:** `cd4a6c2838a528361e7dbcdd49ad0e2a2077d5c751a2bff688e2364d06af080d`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** PROXYSHOP_WORKER=17 .venv/bin/python over buyer_svc.build_buckets on 4000 generated accounts (60 categories, 60 weighted regions, 0-20 orders, seed 20260902): distinct classes 3698, min(class size)=1, median 1, max 9, UNIQUELY IDENTIFIED …
- **Scope:** `apps/buyer/svc/src/profile/__init__.py`

#### T-139 — identity_leaks holds every slug of the account's OWN order categories out of the searchable haystack …

identity_leaks holds every slug of the account's OWN order categories out of the searchable haystack (_incidental_bucket_values, line 383), account-wide rather than per-bucket. category_affinity is the one …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/buyer/svc/src/profile/__init__.py:432` · **Finding:** `abcfa66ee367de1571b72ff73934ecd75d20d5b59dffa5bd4cdf4e47e813996e`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** account = {'email':'dana.reyes@example.com','first_name':'Dana','last_name':'Reyes','address':'44 Alder Way, Portland OR 97205','postal_code':'97205','region':'US-OR','orders':[{'total':120.0,'category':'gift for Dana Reyes, 44 Alder Way …
- **Scope:** `apps/buyer/svc/src/profile/__init__.py`

#### T-140 — AccountDirectory has exactly one implementation and no production populator.

AccountDirectory has exactly one implementation and no production populator. build_auth_service() never passes accounts=, so production runs the empty default InMemoryAccountDirectory, and MagicLinkAuth.redeem …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/buyer/svc/src/auth/magic_link.py:193` · **Finding:** `81904f930f71eb17d542e3b914d8aff4abb9211c84824c54303c89441762dd4c`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** routes.set_auth_service(None); svc = routes.build_auth_service() -> accounts=InMemoryAccountDirectory (empty), vault store=InMemoryPseudonymStore, sessions=InMemorySessionStore. Log in three different addresses through POST …
- **Scope:** `apps/buyer/svc/src/auth/magic_link.py`

#### T-141 — The magic-link token is delivered to the default no-op `_drop` (line 166) in every deployment, and …

The magic-link token is delivered to the default no-op `_drop` (line 166) in every deployment, and set_auth_service (routes.py:146) — the documented seam for installing a mail transport before the first …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/buyer/svc/src/auth/magic_link.py:194` · **Finding:** `a1e986a7af5d9c64785c1850e28a301ccd4d67b56237aedc1cc18b3dd9bf7301`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** grep -rn 'set_auth_service|deliver=' --include=*.py apps packages services scripts e2e fixtures -> set_auth_service appears only in its own definition, __all__ and a docstring; every deliver= is in apps/buyer/svc/tests/test_auth_vault.py. …
- **Scope:** `apps/buyer/svc/src/auth/magic_link.py`

#### T-142 — publish_profile — the only writer of app.buyer_accounts, described as 'the store-visible working set' — has …

publish_profile — the only writer of app.buyer_accounts, described as 'the store-visible working set' — has zero production call sites, and no code outside the buyer service consumes BuyerProfile or reads …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/buyer/svc/src/profile/__init__.py:492` · **Finding:** `beaddb94fcb3a85224c9b9604a7e0ae380187f47f9e963111df5d971038acbab`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** codegraph explore 'publish_profile callers' -> 'publish_profile (apps/buyer/svc/src/profile/__init__.py:492) — 1 caller; tests: apps/buyer/svc/tests/test_auth_vault.py'. grep -rn 'BuyerProfile|buyer_accounts' over …
- **Scope:** `apps/buyer/svc/src/profile/__init__.py`

#### T-143 — A replayer can relabel a signed delivery onto a topic of its choice AND destroy the genuine one.

A replayer can relabel a signed delivery onto a topic of its choice AND destroy the genuine one. handle_delivery takes the topic from the UNSIGNED URL path when the X-Shopify-Topic header is absent (`topic = …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/merchant/svc/src/install/webhooks.py:345` · **Finding:** `29cb064c8971e527804fc9dcf4d131a63b639d229fd2c69f04df5ba07ce1cb41`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** With SHOPIFY_API_SECRET=shhh-secret against merchant_svc.main.create_app(): body=b'{"id":7001,"checkout_token":"tok-7001","total_price":"100.00"}', sig=b64(HMAC-SHA256(secret,body)). (1) attacker POSTs body+sig to …
- **Scope:** `apps/merchant/svc/src/install/webhooks.py`

#### T-144 — merchant-svc has no deployable.

merchant-svc has no deployable. The compose fragment this file's own header declares 'Owner: T-050' is still `services: {}`, and nothing anywhere in the repo starts merchant_svc.main:app. Every route, guard …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/merchant/compose.yaml:11` · **Finding:** `467736a4189a7ffc7e514cf62a211ed1b3afce78e4af3e1eb4437176a3a53629`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** `grep -rn 'merchant_svc|merchant-svc' Makefile scripts docker-compose.yml e2e db` -> no matches. `grep -n '^ [a-z-]*:' docker-compose.yml` -> only `postgres:` and `redis:`. `cat apps/merchant/compose.yaml` -> ends in `services: {}` while …
- **Scope:** `apps/merchant/compose.yaml`

#### T-145 — R10's "hard timeout" is measured from the moment solicit_bids constructs its ArrivalClock, not from the …

R10's "hard timeout" is measured from the moment solicit_bids constructs its ArrivalClock, not from the auction's own deadline. ArrivalClock's origin is monotonic() at line 181 and its frame start is `now - …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/exchange/src/orchestration/solicitation.py:181` · **Finding:** `2da65f7350f9b8d9d01c49ed08b4336ca9d7b6ed203ec25fb54e3f2a67dd3bd8`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** PROXYSHOP_WORKER=15 .venv/bin/python: build create_app(); configure_auctions(app, solicitor=<answers 0.9s after being asked>, eligibility=<SellerEligibility whose check() sleeps 0.6s>); POST /auctions with a 2-store roster and …
- **Scope:** `apps/exchange/src/orchestration/solicitation.py`

#### T-146 — The hard timeout is bought by abandoning the ThreadPoolExecutor (`shutdown(wait=False, cancel_futures=True)` …

The hard timeout is bought by abandoning the ThreadPoolExecutor (`shutdown(wait=False, cancel_futures=True)` instead of a `with` block), but a fresh pool is created per request and the abandoned workers are …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/exchange/src/auction/fanout.py:293` · **Finding:** `11d58d35f8c0ee46c20851ec08b8fb6d166dd095b08826a8136c4db6da004ecf`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** PROXYSHOP_WORKER=15 .venv/bin/python: create_app() wired with a solicitor that sleeps 60s, a 6-store roster, bid_timeout_seconds=0.15, then POST /auctions five times, counting threading.enumerate() names starting 'bid-fanout' after each. …
- **Scope:** `apps/exchange/src/auction/fanout.py`

#### T-147 — T-036 — the whole second half of this lane — is unwired.

T-036 — the whole second half of this lane — is unwired. Nothing outside apps/exchange/src/checkout/ ever resolves a provider or calls CheckoutProvider.checkout: the port, both providers, the registry, the …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/exchange/src/checkout/registry.py:61` · **Finding:** `6ceae2ee1b39af81f779e1c23bce8ed52ea98fabb8cb9e60e885b52eb454675e`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** `codegraph explore "who calls CheckoutProvider.checkout, resolve_provider ..., SimulatedRedirectProvider — production call sites outside the checkout package"` returns callers only in …
- **Scope:** `apps/exchange/src/checkout/registry.py`

#### T-148 — `configure_auctions` — the only way to give the exchange a real solicitor, a real eligibility source, a …

`configure_auctions` — the only way to give the exchange a real solicitor, a real eligibility source, a Redis-backed store or a real ledger sink — has no production caller anywhere in the repo. The service as …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/exchange/src/auction/routes.py:103` · **Finding:** `c2d3c196c797f608ead6d548c2be1c2634ee0d9c9ee7177bd8639798df161655`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** PROXYSHOP_WORKER=15 .venv/bin/python: `app = exchange.main.create_app()` with NO configure_auctions call (i.e. exactly what the ASGI entrypoint builds), then POST /auctions with a 2-store roster. Observed: status 201, entries: [], …
- **Scope:** `apps/exchange/src/auction/routes.py`

#### T-149 — A connection failure never becomes a StoreUnavailable, so an unreachable or restarting Postgres returns HTTP …

A connection failure never becomes a StoreUnavailable, so an unreachable or restarting Postgres returns HTTP 500 with an empty body after a 30-second block, instead of the 503 store_unavailable the module …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/trust/src/events/pg.py:206` · **Finding:** `0c867e2649ce1cadb14594860ab99ffd009610b39645f49fb4b62f9ee39d8772`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** PROXYSHOP_WORKER=13 .venv/bin/python -c 'import os,sys,time; sys.path.insert(0,"."); sys.path.insert(0,".pkgroot"); import logging; logging.disable(logging.CRITICAL); from fastapi.testclient import TestClient; from trust.events import …
- **Scope:** `apps/trust/src/events/pg.py`

#### T-150 — The ledger writer has zero producers.

The ledger writer has zero producers. Nothing in the repository writes an event into it - not by import, not over HTTP. A repo-wide grep for `trust.events` / `apps.trust.src.events` / `/events` outside the …

- **State:** **READY**
- **depends_on:** *(none)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/exchange/src/auction/ledger.py:54` · **Finding:** `f21e31a9067af7c22fbdd795d7c55e015d8db0a7a000795c1ae4c68f4d968fe1`
- **Graded by:** nothing yet — no frozen test, and `verify` is `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass. Write the failing gate first.
- **Reproduction:** grep -rn --include='*.py' --include='*.ts' -e 'trust\.events' -e 'apps\.trust\.src\.events' . | grep -v '/\.venv/' | grep -v '^\./apps/trust/' | grep -v '^\./\.swarm-loop/' -> no output. `codegraph explore "create_events_app …
- **Scope:** `apps/exchange/src/auction/ledger.py`

---

# Closed — 39 tickets, indexed here and detailed nowhere

Per this run's own rule, **a closed ticket leaves this ledger**. Its objective, acceptance and
verification live in `tickets.json`, in `reports/`, and in git history. The index exists only so
a reader can tell "absent because closed" from "absent because the ledger drifted again" — the
failure this regeneration was written to repair.

| id | title |
|---|---|

| T-000 | Monorepo skeleton with green empty verify pipeline |
| T-010 | Contracts generate typed models and enforce the dual-path bid boundary |
| T-011 | Postgres schemas enforce role isolation and hash-chained ledger |
| T-012 | Neo4j attribute-node catalog with vector retrieval through EmbeddingProvider |
| T-013 | Shopify stub reproduces the exact surface the system uses |
| T-014 | LLM client with per-role config, cache-first prompts, and test doubles |
| T-020 | Signed fetch adapter ingests a storefront including password-protected dev stores |
| T-030 | Auctions fan out, time out, and always represent every store |
| T-036 | Checkout reaches the merchant through a provider port |
| T-040 | Tool hooks are the only way facts and discounts enter a bid |
| T-050 | Installing the app wires pixel and webhooks against the stub |
| T-060 | Every event lands once, chained, and replays exactly |
| T-070 | Buyers authenticate lightly and stores never see who they are |
| T-080 | The approved manifest and seed generator define ground truth |
| T-100 | Shopify stub never emits an off-domain checkout Location |
| T-101 | A partial re-embed is never certified as complete |
| T-102 | The chain_head guard trigger's DELETE arm is graded by a test |
| T-103 | The TypeScript signer refuses integers the wire cannot state |
| T-104 | The prompt cache key cannot collide on separator text |
| T-105 | Money arithmetic is asserted absolutely, not against itself |
| T-106 | A conformance gate keeps the two JCS canonicalizers from drifting |
| T-107 | claim_id is computed with JCS, not json.dumps |
| T-108 | The two envelope gates agree on whitespace |
| T-110 | Role passwords have one source of truth |
| T-112 | The role password has one source of truth on the project's own fresh volume |
| T-113 | The TypeScript signing path refuses unsafe integers by default, not opt-in |
| T-114 | The chain_head trigger test does not block the ENABLE ALWAYS hardening |
| T-115 | The envelope blank rule is engine-independent again |
| T-116 | A single degenerate product cannot black out vector search |
| T-118 | Wave-2 residue: eight low-severity findings from the lane verifiers |
| T-119 | The ledger canonicaliser is one module object under both import spellings |
| T-120 | Using PROXYSHOP_ROLE_PASSWORD does not turn the repo gate red |
| T-121 | The JCS conformance suite's prose matches the code T-119 changed |
| T-122 | Subprocess tests hand the child .pkgroot instead of clobbering PYTHONPATH |
| T-125 | The signing door and the canonicalizer agree, and the gate watches both |
| T-126 | The ledger spelling binding survives a concurrent first import |
| T-127 | The chain_head DELETE arm is graded by property, not by enumeration |
| T-128 | Guards that cannot refuse anything are removed, not tested tautologically |
| T-129 | Wave-3 verification residue: nine findings across four lanes |

---

# Scheduling constraints

Per-ticket file ownership is `intake-report.md` §4 (narrowed) and constraint pairs are §5, with
the **flat** source layout of D42 overriding §3.1/§4's nested tree. Membership lists below have
been re-derived against the current open set; closed tickets are struck from them because they
can no longer be co-scheduled with anything.

## Neo4j is ONE shared database (D4, narrowed by D38)

`CREATE DATABASE` is unsupported on Community, so there is no per-worker graph.

- **Never co-schedule two graph-writing tickets.** The graph-writing set is T-012, T-020,
  T-021, T-022, T-023, T-024, T-031 — of which **T-021, T-022, T-023, T-024 and T-031 are still
  open**, and four of those five are in the ready frontier. They serialize against each other.
- **Never co-schedule `e2e/` with a graph lane.** `e2e/` resets and writes the same shared
  graph. The session-scoped `flock` on `/tmp/proxyshop-neo4j.lock` (root `conftest.py`,
  `_neo4j_guard`) is the safety net, not the plan: it makes a collision slow rather than
  corrupt. The `e2e/**`-scoped tickets are **T-082, T-083, T-084** (all open, all blocked).
- Vector-index creation is `CREATE VECTOR INDEX … IF NOT EXISTS`, so a re-entrant session never
  errors. Do not "fix" that by dropping the index.

**The hazard is directory-level, not ticket-level — run the declared verify, never the
directory.** `neo4j_driver` calls `reset_graph` (`MATCH (n) DETACH DELETE n`,
`proxyshop_support/neo4j_lock.py:157-169`) once per session, and *any* invocation that collects
a directory rather than a file drags in a frozen scaffold test that requests `neo4j_session`
and wipes the graph. Two directories carry this:

- `e2e/` — via `e2e/test_scaffold_smoke.py::test_the_e2e_lane_holds_the_d37_neo4j_lock`.
  **T-086 is not in the `e2e/**` exclusion** (decided on evidence: its scope is the two narrow
  paths `e2e/test_onboarding.py` and `e2e/support/onboarding/**`, all four of its acceptance
  criteria are Postgres-shaped, and it depends on none of T-012/T-030/T-031). But its agent
  must run `pytest e2e/test_onboarding.py -q`, **never** `pytest e2e/`, while a graph lane holds
  the surface.
- `apps/exchange/tests/` — via the frozen
  `apps/exchange/tests/test_scaffold_datastores.py:69::test_neo4j_session_runs_inside_the_d37_flock`.
  All seven exchange tickets declare a **single-file** verify, so every declared verify is safe
  beside a graph lane; a broadened invocation from any of them is not.

## Shared scope globs — per-file ownership, not `parallel_safe`, is what separates them

`parallel_safe: true` does not mean "shares no directory". Recomputed from `tickets.json` in
this pass; **open members only** — a glob whose other claimants are all closed no longer
constrains scheduling.

| glob | open tickets declaring it |
|---|---|
| `apps/exchange/tests/**` | T-031, T-032, T-033, T-034, T-035 |
| `apps/trust/tests/**` | T-061, T-062, T-063, T-064, T-065 |
| `packages/store-agent/tests/**` | T-041, T-042, T-043, T-044 |
| `services/ingest/tests/**` | T-021, T-022, T-023, T-024 |
| `proxyshop_support/**` | T-109, T-117, T-123, T-124 |
| `apps/buyer/svc/tests/**` | T-071, T-072, T-073 |
| `apps/merchant/svc/tests/**` | T-051, T-052, T-053 |
| `apps/buyer/**` | T-130, T-132, T-133 |
| `apps/buyer/svc/src/profile/__init__.py` | T-138, T-139, T-142 |
| `conftest.py` | T-109, T-117, T-123 |
| `e2e/**` | T-082, T-083, T-084 |
| `apps/buyer/svc/src/auth/magic_link.py` | T-140, T-141 |
| `apps/merchant/**` | T-130, T-134 |
| `fixtures/**` | T-130, T-131 |
| `scripts/bootstrap.sh` | T-111, T-123 |

The last two rows of the T-13x block are new and easy to miss: **T-138, T-139 and T-142 all
rewrite the same file** (`apps/buyer/svc/src/profile/__init__.py`) and **T-140 and T-141 all
rewrite the same file** (`apps/buyer/svc/src/auth/magic_link.py`). Five of the sixteen new HIGH
findings therefore collapse into two single-file lanes, and both of those lanes sit inside
T-132's and T-133's scope as well.

Four more globs now have exactly one open claimant, so the pairing is discharged by history but
the *rule* the split encoded still binds the survivor:

- `apps/exchange/src/orchestration/**` — open **T-033**, closed sibling T-030. D54 states the
  split explicitly: T-030 created the package with `solicit_bids`; **T-033 adds `accept_offer`
  and must not modify `solicit_bids`.**
- `services/ingest/src/adapters/**` — open **T-023**, closed sibling T-020.
- `apps/trust/**` — open **T-124**, eight closed siblings.
- `packages/**` — open **T-133**, closed sibling T-122.

### Scope containment — five open tickets enclose most of the board

`parallel_safe` and the glob table both compare scopes for *equality*. The more dangerous
relation is **containment**: a ticket whose scope is a broad `**` glob silently owns the files of
every narrower ticket underneath it. Recomputed across all 59 open tickets:

| ticket | open tickets whose scope it contains | count |
|---|---|---|
| **T-130** | every open E2–E7 lane plus T-131 – T-150 | **38** |
| **T-133** | T-041 – T-044, T-065, T-071 – T-073, T-130, T-132, T-135, T-136, T-138 – T-142 | **17** |
| **T-132** | T-071 – T-073, T-130, T-133, T-138 – T-142 | **10** |
| **T-131** | T-021, T-022, T-023, T-053, T-071, T-130, T-137 | **7** |
| **T-134** | T-051 – T-054, T-130, T-143, T-144 | **7** |
| **T-124** | T-061 – T-065, T-109, T-117, T-123, T-149 | **9** |

Read that as a dispatch rule, not trivia: **none of these six may be co-dispatched with anything
it contains.** T-130 in particular is a cross-cutting sweep of MEDIUM findings from five feature
lanes and, at its declared scope, conflicts with essentially the whole open board. Take it alone,
split it by lane, or land the narrow tickets first and re-scope it to the remainder.

### Two clusters inside the ready frontier serialize against themselves

This is the constraint most likely to be missed, because both clusters look like independent
small tickets:

- **The infra-debt cluster: T-109, T-111, T-117, T-123, T-124.** They pile onto three shared
  paths — `proxyshop_support/**` (four of the five), `conftest.py` (three) and
  `scripts/bootstrap.sh` (two). Three of them are READY (T-109, T-111, T-124) and **must not be
  co-dispatched**. Ordering that respects both the graph and the overlap: **T-111 → T-123**, and
  **T-109 → T-117** (T-117 additionally needs a protected-path exception), with **T-124** taken
  alone or beside a lane that touches neither `proxyshop_support/**` nor `apps/trust/**`.
- **The cycle-8 residue cluster: T-130, T-131, T-132, T-133, T-134.** All five are READY, and
  **T-130 overlaps every one of the other four** — `apps/buyer/**` with T-132/T-133,
  `apps/merchant/**` with T-134, `fixtures/**` with T-131 — and, since `9710f3e`, eleven of the
  sixteen new T-13x/T-14x findings as well. Dispatch T-130 **alone**, or split it by lane, or
  land the narrow tickets first and re-scope it to what remains.

## A data-shape hazard in `tickets.json` — do not iterate `scope` without normalising

**25 of the 98 tickets store `scope` as a comma-joined STRING, not a list of globs.** All 25 are
T-1xx defect tickets minted in waves 1–4 (T-100 – T-105, T-107 – T-118, T-120, T-121,
T-125 – T-129); the other 73 use a list, and every other list-valued field (`acceptance`,
`depends_on`, `refs`, `non_goals`) is a list wherever it is present. Any tool that does `for g in
ticket["scope"]` therefore iterates **single characters** for those 25 and silently computes
nonsense file ownership. Of the 25, **T-109, T-111 and T-117 are still open**, so the hazard is
live for the exact tickets a scheduler most needs to keep apart. Normalise on read:

```python
scope = [x.strip() for x in s.split(",")] if isinstance(s := t["scope"], str) else list(s)
```

Recorded, not repaired: `tickets.json` is the authoritative source and this ledger does not edit
it. Fixing the shape is a change to the graph and belongs to whoever next amends it.

## T-085 and T-087 share one verify command, and T-087 does not own it

Both declare `pytest docs/tests/test_runbook.py -q`; `docs/tests/**` is in **T-085's** scope and
T-087's `non_goals` forbid touching it. T-085's acceptance 3 makes that lint check the **union**
of every markdown file under `docs/demo/`, and T-085's own objective says the union is only
satisfied once T-087 also lands. T-087 depends on T-085, so ordering discharges the write
conflict; the shared verify is a *reading* hazard — do not read T-087's green as scoped to T-087.

## Datastore isolation

- **Postgres isolates per-worker database.** Each worker gets `proxyshop_w<N>`
  (`scripts/db_init.py`, `make db-init`). Roles are cluster-global (`pg_authid.relisshared`), so
  `CREATE ROLE` lives once in `db/init/00-roles.sql` and GRANTs — per-database — live in
  `db/migrations` so every `proxyshop_w<N>` gets its own copy. No Postgres serialization
  constraint applies between tickets.
- **Redis isolates per-worker logical DB index** (16 available) plus a central `w{N}:` key
  prefix in `proxyshop_support`. `FLUSHALL` is banned repo-wide and grepped for in `make verify`;
  `FLUSHDB` is the permitted reset. `maxmemory-policy` is `noeviction`. No Redis serialization
  constraint applies between tickets.
- **`PROXYSHOP_WORKER` must be set for every dispatch.** The root `conftest.py` fails the session
  if it is unset — deliberately, because a silent fallback is how every worker lands in one
  database.

## Bring the datastore stack up before any cycle metric sweep

`make deps-up` first. With the compose stack down `make verify` is still green — the
`@pytest.mark.docker` tests skip cleanly — so `build_succeeds` reads 1 without ever exercising
`pg_role`, the D5 grants, the D37 flock or the Redis prefix. **T-109 is the ticket that makes
this worse than it sounds**: today a blip on *any one* of the three datastores empties the whole
docker-marked gate, including 100% of T-011's acceptance criteria 1 and 4.

## The human approval gate is discharged

T-080's manifest gate — which stood over 16 tickets — was **approved by the user and closed**
(`8a2841f`, merged in cycle 8). No open ticket is waiting on a human approval gate.
The one open item awaiting a *user ruling* is **T-132**, which needs a frozen-contract amendment
(the user approved amending; design v1 was refuted at `bda0ce0` and recorded rather than applied).
**T-117 is blocked on a protected path** rather than a ruling: its fix requires editing
`scripts/verify.sh`, which is in `state.json.protected_paths`, so it is not dispatchable as
scoped even once T-109 lands.

---

# Frozen acceptance coverage — read green accordingly

The acceptance suite was frozen at cycle 0 and amended once (amendment 1, `31c6be0`) to **120
tests**. Coverage is not uniform, and **34 of the 59 open tickets carry no frozen test at all**:

> T-024, T-031, T-054, T-082, T-083, T-084, T-086, T-087, T-109, T-111, T-117, T-123, T-124,
> T-130, T-131, T-132, T-133, T-134, **and every one of T-135 – T-150**

For those, **the ticket's own `verify` command is the entire grade**, and it is written by the
same agent that writes the code. Weigh their green accordingly.

**For T-135 – T-150 even that is not true yet.** All sixteen carry
`verify: false  # NO GATE YET — write one that fails on this finding first`, so they have
neither a frozen test nor a working verify command: `false` cannot pass. They are graded by
nothing at all until someone writes the gate. That is deliberate — the minting commit
(`9710f3e`) records 99 rung-2 findings and promotes only the 16 HIGH ones to tickets, each with
a `location`, a `defect` and a `reproduction` field instead of an acceptance list — but it means
**no metric will move, and no gate will redden, when one of these defects is present or absent.**
Nothing in `make verify` sees them.

Two cases in that list are not what they look like:

- **T-036's work IS frozen — under T-033's marker.** Four frozen tests call
  `accept(auction, bid_ref, code_creator, mode)` at `test_e3_exchange.py:658, :682, :685, :704,
  :711`, plus `test_spec_criteria.py:1042, :1073` for the S8-3 blocker; every one is marked
  `T-033`. A broken checkout port shows up as a red `e3_exchange_passing` **and** a red
  `spec_criteria_passing`, both attributed to T-033. (T-036 is now closed; the marker asymmetry
  is inherited by T-033, which is in the ready frontier.)
- **T-054, T-082, T-083, T-084 are E-metric-invisible.** They carry no ticket marker, so no
  frozen number moves when they land. Their acceptance is real and gradeable — by their own
  verify commands only.

## T-065's sixth frozen test is graded AFTER T-062 — not a defect in T-065

**ESC-003, ruled by Hank at cycle 1: no amendment.** Recorded because the next reader will
otherwise re-derive it as a blocker, and because T-065's worker must be told.

`test_e6_trust.py:1470`, `test_verification_results_are_typed_and_route_into_the_catalog_claim_dimension`,
carries `@pytest.mark.ticket("T-065")` but does `from apps.trust.src.scoring import
claim_dimension, score`. `apps/trust/src/scoring/**` is **T-062's exclusive scope**, and the
edges run the wrong way: `T-062.depends_on` includes `T-065`, while `T-065.depends_on` does not
include T-062. So T-065 is graded first, when that module does not exist, and the import — which
sits inside the test function — raises `ImportError` at call time. Adding `T-065 → T-062` is
unavailable: it creates a genuine cycle (verified programmatically).

- **T-065's packet must say this explicitly.** Its sixth frozen test will fail with an
  `ImportError` that is *expected* and that the worker **cannot fix from inside its own scope**.
  The natural "fix" — creating the module — is a scope violation `check-branch` rejects.
- **T-065 closes on its other five frozen tests plus its own verify command**, not on 6/6.
- **T-062's packet inherits the other half:** it must ship `claim_dimension` and `score` such
  that this test passes, even though the test is not marked T-062.

**Blast radius is exactly one test.** An AST sweep of all 120 frozen tests found exactly one
violation, this one. The rule worth carrying: **a cross-ticket import is fine iff the imported
module's owner is in the importing ticket's transitive dependency closure.**

---

# Carry-forward defects (found in cycle 0, never ticketed)

Each was seen by an adversarial verifier during T-000 and consciously deferred. None blocks
dispatch; each has a named owner so it is fixed by the ticket that already touches the file.
**CF-2, CF-3 and CF-4 named T-011 as owner and T-011 has since closed** — so they are now
unowned and fall to the next ticket that touches each file.

- **CF-1 — stale docstring in the frozen datastore proof.** *(cosmetic, misleads agents)*
  `apps/exchange/tests/test_scaffold_datastores.py:9-12` still says "this directory's conftest
  overrides `_neo4j_guard` with the D37 flock". That override was deleted when the lock moved to
  the root `conftest.py` for **every** session. Owner: orchestrator (the file is T-000-frozen).
- **CF-2 — `.importlinter`'s `**` pattern has no regression guard.** *(latent gate break)*
  The D39 contract exempts a legal import with `ignore_imports = ** -> redis.exceptions`, but no
  tracked file imports `redis.exceptions`, so reverting `**` to `*` passes `make verify` today.
  The first ticket to write `except redis.exceptions.ConnectionError` inherits a break it did not
  cause. Owner was T-011 (closed) — reassign to the next ticket touching the import-lint proof.
- **CF-3 — D5's wording is imprecise and `db/init/00-roles.sql` repeats it.** *(will mislead)*
  D5 and `db/init/00-roles.sql:11` both call the grant model "schema-level USAGE only". `USAGE`
  on a schema grants **name resolution**, not row access; the migrations must also issue
  table-level `SELECT` on `ledger` and `app` while granting **nothing at all** on `sealed` and
  `vault`. T-011 shipped correctly; the *prose* is still wrong and will mislead the next reader.
- **CF-4 — latent idle-in-transaction deadlock between `pg_role` and `pg_admin`.** *(latent hang)*
  `pg_admin` (`conftest.py:167-190`) is autocommit; `pg_role` (`conftest.py:194-236`) returns a
  connection in default transactional mode, cached per role for the whole test. A
  `pytest.raises(InsufficientPrivilege)` assertion leaves that connection idle-in-transaction
  holding an `ACCESS SHARE` lock; any subsequent `DROP SCHEMA`/`TRUNCATE`/`ALTER` through
  `pg_admin` in the same test blocks until `lock_timeout` and reads as a hang. The packet rule for
  any ticket combining both fixtures — T-061 – T-065 all can — is *roll back or close the
  `pg_role` connection before touching DDL through `pg_admin`*.

Also noticed, not worth a ticket:

- `.importlinter`'s header cites "(D34)" for the import-lint contract; the ruling is **D35**.
  Symmetrically, `intake-report.md` §9-B cites "D33" for the T-082 multiset; it is **D34**.
- 18 empty `<name> 2` sibling directories exist beside real scope roots (`apps/exchange 2`,
  `db/init 2`, `packages/llm 2`, …) from a Finder/iCloud duplication. Empty and untracked, so no
  scope glob matches them; delete on the next cleanup.

---

*Regenerated from `tickets.json` on 2026-09-02 at `main` = `9710f3e`. To regenerate: recompute
the totals, the closed set (from `main`, not from any status field — there is none), and
READY/BLOCKED from the graph, then rewrite this file whole. **Never edit `tickets.json` to agree
with this file.** Note that `scope` is a comma-joined string on 25 tickets and a list on the
other 73 — normalise it before computing ownership.*
