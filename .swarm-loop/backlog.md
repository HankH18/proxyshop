# ProxyShop — ticket ledger (open tickets + scheduling constraints)

> **Regenerated 2026-09-02 from `tickets.json`, at `main` = `e0ffd1a`.**
>
> `tickets.json` is the authoritative ticket graph. **This file is a derived view of it and
> nothing else.** Every number below was recomputed from the graph in this pass; not one was
> carried forward from the revision this replaces. When the two disagree, the graph wins and
> this file is regenerated. The reverse — editing `tickets.json` to make it agree with this
> file — corrupts the authoritative source and must never be done.
>
> **The graph is 123 tickets and 141 edges.** That pair is stated once, here, and every count
> in this file is consistent with it.
>
> **Derivation snapshot.** Read from `tickets.json` at mtime `22:31:48`,
> `sha256:745b18e24463b95a…`, 239 977 bytes. The graph grew from 116 to 123 tickets *while
> this file was being written* — cycle 10's triage minted T-169 – T-175 mid-pass. If the
> hash above no longer matches, this file is stale again and the numbers below are the ones
> to distrust first.

## Totals — measured from the graph, not remembered

| quantity | value |
|---|---|
| tickets in `tickets.json` | **123** |
| dependency edges | **141** |
| roots (empty `depends_on`) | **42** — T-000 plus every finding ticket T-135 – T-175 |
| max depth | **10**, deepest **T-087** |
| graph health | acyclic, **0** dangling dependency references, **0** duplicate ids |
| **closed** — work landed, already fixed, or refuted | **75** |
| **open** — the contents of this ledger | **48** = **31 READY** + **17 BLOCKED** |
| of the 31 READY, actually dispatchable | **30** — T-117 is READY in the graph and undispatchable in fact (ESC-005) |
| open acceptance criteria | **87**, across the **26** open tickets that carry an `acceptance` array |
| open tickets with **no** acceptance array and **no** working gate | **22** — every open finding ticket |
| open tickets whose recorded `scope` is prose, not a glob | **15** — see "Unenforceable scope strings" |
| frozen acceptance tests | **120** (120 test functions under `.swarm-loop/acceptance/`, exactly one `@pytest.mark.ticket` each), of which **51** sit on open tickets and **69** on closed ones |
| open tickets with **no** frozen coverage at all | **30 of 48** |
| last measured acceptance | **73 / 120 passing** (60.83 %, cycle 10, `e0ffd1a`) — up from 43/120 (35.83 %) at cycle 9 |

Per-epic at that same cycle-10 measurement (`passing / target`): SPEC 8/8, E1 10/10,
E2 6/8, E3 9/21, E4 3/20, E5 4/10, E6 26/26, E7 2/9, E8 5/8. `build_succeeds` = 1.

## What changed since the last regeneration

The previous revision was generated at `1b08077` and described a 101-ticket graph. Since then:

1. **Two integration merges landed on `main`** — `0e96d1f` (cycle-10 batch 1: trust DSN role,
   price reconciliation, accept, provisioning) and `e0ffd1a` (cycle-10 batch 2: the E6 trust
   epic, the contracts boundary, retrieval, buyer privacy). Fourteen tickets closed across
   them.
2. **A cycle-10 adversarial triage pass adjudicated the standing finding backlog** — seven
   tickets ruled already fixed, nine refuted outright, one (T-152) refuted as a standalone
   ticket with its real half folded into T-041 as a binding requirement, and one (T-117)
   confirmed but declared structurally undispatchable.
3. **Twenty-two finding tickets were minted** — T-154 – T-175, all roots, all carrying the
   placeholder `verify`. Seven of those (T-169 – T-175) were minted during this regeneration.

Net: closed went 50 → 75, open went 51 → 48, the graph went 101 → 123.

## How closed-ness was determined

**`tickets.json` carries no status field of any kind.** The plan tickets have
`id, title, objective, refs, acceptance, verify, scope, depends_on, non_goals, parallel_safe`;
the finding tickets have `id, title, verify, scope, depends_on, source, severity, location,
defect, finding_id, reproduction`. Neither shape has a status, so **closure cannot come from
the graph** and nothing in the graph can contradict git.

Closure here is the union of three sources, and each row below says which one it came from:

- **Landed on `main`** — verified with `git log --grep=<id> e0ffd1a --not 1b08077` for every
  ticket claimed to have landed this cycle, i.e. searching *only* the commits added since the
  previous regeneration, so an older mention cannot be mistaken for new work. The fifty rows
  inherited from the previous pass keep their originally measured evidence.
- **Ruled already fixed** by the cycle-10 triage pass, which opened the named file at HEAD and
  found the described defect absent. No code landed for these.
- **Refuted** by the same pass — the described defect is not a defect, or not one at the
  named location. No code landed for these either.

A closed ticket **leaves the ledger**. It survives as a row in the evidence table and nowhere
else; its objective, acceptance and verification live in `tickets.json`, in `reports/` and in
git history. The table exists so a reader can tell "absent because closed" from "absent
because the ledger drifted again".

### Closed this cycle — 25 tickets

**Batch 1, merged `0e96d1f`:**

| id | how | evidence on `main` |
|---|---|---|
| T-033 | landed | `82d71ce` feat(exchange): accept mints a validated code, permalink and event trail; `28d2ae2` fix — four required positionals, residual hazards pinned; merged `19b1845` → `0e96d1f`. **Reopened downstream as T-169/T-170 — see the caveat below.** |
| T-109 | landed | `d46f931` fix(T-109): reachability is per-service, so a Redis blip cannot empty the Postgres gate; merged `13d9f79` → `0e96d1f`. **Hole found afterwards: T-172.** |
| T-111 | landed | `8e87ac6` fix(T-111,T-123): provisioning clears UF_HIDDEN recursively and fails loudly → `0e96d1f` |
| T-123 | landed | `8e87ac6`, same commit as T-111 → `0e96d1f`. **Its new test is itself a hazard: T-174.** |
| T-151 | landed | `06bd152` test(trust): red gate — the ledger writer must resolve its own per-role DSN; merged `7803ba1` → `0e96d1f`. **Fallout: T-154.** |
| T-153 | landed | `72c8e95` test(store-agent) RED — the price a bid states must follow from its granted depth; `76cd5d8` fix; merged `9495359` → `0e96d1f`. **Fallout: T-155, T-156, T-173, T-175.** |

**Batch 2, merged `e0ffd1a`:**

| id | how | evidence on `main` |
|---|---|---|
| T-031 | landed | `3f99d9a` feat(T-031): candidate retrieval, R19 hard filter, deterministic fit scoring; `5dc5de3` decline numeric-eq pushdown; `8bd731f` nine defects from adversarial review; merged `66b3548` → `e0ffd1a` |
| T-061 | landed | `91cc76c` feat(trust): T-061/T-063/T-064 reconciliation, feedback loop, served snapshot; `dac7c9e` five more defects → `e0ffd1a` |
| T-062 | landed | `42eb68b` feat(trust): the merged six-dimension trust engine; `3a146eb` comparators agree with the approved answer key, 36/36; merged `d438b5f` → `e0ffd1a` |
| T-063 | landed | `91cc76c` → `e0ffd1a` |
| T-064 | landed | `91cc76c` → `e0ffd1a` |
| T-065 | landed | `37735dd` feat(verification): deterministic claim verification with evidence and four statuses; `2bddff3` three self-found defects plus the trust-side seam → `e0ffd1a` |
| T-133 | landed | `02c869d` fix(buyer) (a) the leak detector was itself a disclosure path; `76ec8c8` (b) bound the unauthenticated session table; `979958c` format → `e0ffd1a`. **Item (c) was deliberately deferred and is now T-163.** |
| T-139 | landed | `2767c06` fix(buyer): identity riding out inside a category slug; merged `668d6fd` → `e0ffd1a` |

T-135 was re-merged in batch 2 (`5314449`) but had already closed in an earlier cycle; it is
not a new closure and holds its original row.

**Ruled by the cycle-10 triage pass — no code landed:**

| id | how | evidence |
|---|---|---|
| T-130 | already fixed | Sub-HIGH feature-wave sweep; triage opened the named scopes at HEAD and found the sub-HIGH items already carried. Close without dispatch. |
| T-132 | already fixed | `apps/buyer/**` — the pseudonym re-linkability the ticket describes is no longer present at HEAD. Close without dispatch. |
| T-136 | refuted | `packages/store-agent/src/hooks/tools.py:161` — "every runtime capability the branch adds is unwired" is not a defect at the named location. |
| T-137 | refuted | `fixtures/manifest/__init__.py:383` — `refresh_digests` re-pinning an approval is not a defect as described. Note this reverses the previous ledger, which carried T-137 open and adjudicated only in prose. |
| T-140 | refuted | `apps/buyer/svc/src/auth/magic_link.py:193` — AccountDirectory having no production populator. |
| T-141 | refuted | `apps/buyer/svc/src/auth/magic_link.py:194` — the magic link delivered to the `_drop` no-op. |
| T-142 | refuted | `apps/buyer/svc/src/profile/__init__.py:492` — `publish_profile` as dead code. **Its recorded location was wrong regardless: see constraint 3.** |
| T-147 | refuted | `apps/exchange/src/checkout/registry.py:61` — T-036's port having no production consumer. |
| T-148 | refuted | `apps/exchange/src/auction/routes.py:103` — `configure_auctions` having no caller. |
| T-150 | refuted | `apps/exchange/src/auction/ledger.py:54` — the ledger writer having zero producers. |
| T-152 | refuted as a standalone ticket | Its "no caller" half is T-041's unbuilt scope, not separate work. **Its other half is real and is now a binding requirement on T-041 — see constraint 4.** |

T-124, T-138, T-143, T-144, T-145 and T-149 also appear in the triage rulings; all six were
already closed in the previous pass and keep their original rows. They are not new closures.

> **Caveat a future lane must not miss.** Seven of the nine refutations (T-136, T-140, T-141,
> T-142, T-147, T-148, T-150) are the same finding shape: *"this landed symbol has no
> production caller."* Cycle 10 then minted **T-169 and T-170**, which assert exactly that
> shape about T-033's accept package — a decorative domain guard nothing wires, and an accept
> package with no `routes.py` so no HTTP path reaches it. Either the class is not a defect, in
> which case T-169/T-170 will be refuted too, or T-169/T-170 are real, in which case the seven
> refutations need re-reading. **This ledger records both rulings as made and does not
> adjudicate between them.** Whoever dispatches T-169 or T-170 must resolve it first.

### Closed before this cycle — 50 tickets

| id | title | evidence on `main` |
|---|---|---|
| T-000 | Monorepo skeleton with green empty verify pipeline | `2b408cd` feat(T-000) monorepo skeleton, plus `955432c`/`09fb42e`/`17c1527`/`429f29e`/`c37b417` fixes; merged `705cf34` |
| T-010 | Contracts generate typed models and enforce the dual-path bid boundary | `62c19ea` schema-driven models + dual-path bid boundary, `d521447` D52 envelope at the external door; merged `088e818` |
| T-011 | Postgres schemas enforce role isolation and hash-chained ledger | `8417803` schemas / role isolation / D16 hash chain, then `920d127`, `86b3cbf`, `199ce46`, `b3e993d`; merged `2f0d549` |
| T-012 | Neo4j attribute-node catalog with vector retrieval through EmbeddingProvider | `4ea9a54`, `4e642ee` EmbeddingProvider port, `09f01e3`/`289fb61`/`e2b2939`; merged `fd67161` |
| T-013 | Shopify stub reproduces the exact surface the system uses | `1aefd92`, `e320716` recorded fixtures + compose fragment; merged `e6fcaf5` |
| T-014 | LLM client with per-role config, cache-first prompts, and test doubles | `83ad53b`, `5e85107`, `04840b7`, `9a1ea2f`; merged `43d8855` |
| T-020 | Signed fetch adapter ingests a storefront including password-protected dev stores | `dc2726c` signed-fetch CatalogAdapter with SSRF guard, robots and budgets; `261be9d`, `409017f` |
| T-021 | Policy pages and marketing claims land in the graph with provenance | `4ae86f0`; merged `68f753d` |
| T-030 | Auctions fan out, time out, and always represent every store | `04dfe61`, hardened `30b268b` and `63d1f2b`; merged `fb4dada`/`c43b014` |
| T-036 | Checkout reaches the merchant through a provider port | `b7c8bbc` — `apps/exchange/src/checkout/{provider,providers,registry,codes,domain,lint,sellers}.py` all landed |
| T-040 | Hook-mediated economics with a live envelope re-check | landed and hardened; the `start_bid()` caller contract it discovered is carried forward on T-041 |
| T-050 | Merchant service skeleton, app schema and OAuth surface | landed |
| T-060 | Trust service skeleton, ledger reader and event bus | landed |
| T-070 | Buyer service skeleton, pseudonym vault and session seam | landed |
| T-080 | Approved fixtures manifest and the dishonest-store script | landed; **its approved trajectory is now self-contradictory — ESC-006** |
| T-100 – T-108 | cycle-8/9 infrastructure and harness residue (9 tickets) | all landed on `main` in the cycle-8 and cycle-9 integration merges |
| T-110 | Fresh-volume provisioning is graded on tests that actually run | landed; the deselection defect it surfaced is T-117 |
| T-112 – T-116 | cycle-9 harness and bootstrap residue (5 tickets) | all landed on `main` |
| T-118 | cycle-9 residue | closed on landed work, item (a) only partial |
| T-119 – T-122 | cycle-9 residue (4 tickets) | all landed on `main` |
| T-124 | Ledger-staleness finding | refuted in the previous pass — the ticket was closed and the ledger was stale by one commit |
| T-125 – T-129 | cycle-9 residue (5 tickets) | all landed on `main` |
| T-131 | cycle-9 residue | landed on `main` |
| T-134 | cycle-9 residue | landed on `main` |
| T-135 | Contracts boundary finding | landed; re-merged in batch 2 as `5314449` |
| T-138 | Knob-only framing understates what landed | closed in the previous pass |
| T-143, T-144, T-145, T-146, T-149 | finding tickets | closed in the previous pass; T-143/T-144/T-145/T-149 re-affirmed as already fixed by the cycle-10 triage |

## The graph has forty-two roots, not one

**A structural property of `tickets.json`, not an error.** Forty-two tickets have an empty
`depends_on`: T-000, and every finding ticket from T-135 to T-175. Every piece of prose in
this run's docs that speaks of "wave 1" assumes exactly one scaffold root, T-000, that
everything else descends from. That was true of the original 82-ticket graph and has not been
true since the finding tickets were minted — they were created from verifier sweeps against
branches that had *already landed*, so they descend from nothing. Three consequences:

1. **Depth is not seniority.** The depth-1 set under T-000 is a structural frontier, not the
   dispatch frontier.
2. **The finding roots are READY on day one and stay READY forever.** They are unblocked by
   construction, not by progress, so their presence in the ready frontier carries no
   information about whether the work beneath them is done.
3. **Their unblock count is 0 by construction.** They are leaf roots: nothing depends on them.
   Ranking the frontier by unblock count sorts every finding ticket to the bottom, which is
   backwards — several of them assert that a headline capability of a *closed* ticket is
   unwired in production.

## The ready frontier — dispatch from here

**31 of the 48 open tickets have every dependency closed.** Computed, not curated: an open
ticket is READY iff every id in its `depends_on` appears in the closed table above.
Twenty-two of the thirty-one are READY only because they are *roots*.

*"Unblocks" counts the ticket's transitive **open** descendants. It is one ordering signal,
not the whole answer: the scheduling constraints below veto co-scheduling the ranking would
allow, and the finding roots score 0 by construction rather than by unimportance.*

| ticket | unblocks | frozen tests | parallel_safe | severity | title |
|---|---|---|---|---|---|
| **T-041** | 11 | 2 | no | — | Advocate runtime bids within walls and defaults deterministically cold — **IN FLIGHT** |
| **T-032** | 8 | 10 | no | — | Shortlists rank by the single published formula behind eligibility filters |
| **T-051** | 6 | 2 | no | — | Pixel reports checkout outcomes the collector can join |
| **T-071** | 5 | 3 | no | — | Three questions or fewer produce a confirmed structured intent |
| **T-053** | 3 | 3 | yes | — | A plain-language interview yields an approved, versioned envelope |
| **T-023** | 1 | 1 | yes | — | Catalog MCP adapter passes recorded-contract tests |
| **T-052** | 1 | 3 | no | — | Winning offers become single-use validated codes and permalinks |
| **T-022** | 0 | 1 | yes | — | Same products across stores link via entity resolution |
| **T-117** | 0 | 0 | no | — | **NOT DISPATCHABLE** — protected path, blocked on ESC-005 |
| **T-155** | 0 | 0 | — | CRITICAL | `MAX_SWEEP_DEPTH = 12` silently truncates the provenance walk |
| **T-154** | 0 | 0 | — | HIGH | The trust test fixtures connect as `app`; the shipped writer is `trust_rw` |
| **T-157** | 0 | 0 | — | HIGH | An off-domain permalink leaves a live discount code with no ledger record |
| **T-160** | 0 | 0 | — | HIGH | T-109/T-111/T-123's recorded `verify` commands pass with the defect live |
| **T-163** | 0 | 0 | — | HIGH | `SessionStore.open()` checks the `psn-` prefix, never vault membership |
| **T-169** | 0 | 0 | — | HIGH | The registered-domain guard is decorative under the deployed module spelling |
| **T-170** | 0 | 0 | — | HIGH | The accept package ships no `routes.py`, so no HTTP path reaches it |
| **T-171** | 0 | 0 | — | HIGH | The Neo4j lock is machine-global and its own timeout is unreachable |
| **T-172** | 0 | 0 | — | HIGH | Eight Postgres-only tests still skip silently on a Redis-only outage |
| **T-173** | 0 | 0 | — | HIGH | A removed line turned a loud failure into a silent fail-open at 0.0 |
| **T-156** | 0 | 0 | — | MEDIUM | `total_price` is never reconciled against `unit_price` |
| **T-158** | 0 | 0 | — | MEDIUM | The one-accept-per-auction guard is only as durable as the record handed in |
| **T-159** | 0 | 0 | — | MEDIUM | `except ImportError`-tolerant tests are vacuous until their subject exists |
| **T-161** | 0 | 0 | — | MEDIUM | Provenance nested inside a claim's opaque `value` is not walked |
| **T-162** | 0 | 0 | — | MEDIUM | The external path checks provenance source but never discount authorisation |
| **T-164** | 0 | 0 | — | MEDIUM | `build_buckets`/`anonymise_cohort` run no identity-leak check |
| **T-165** | 0 | 0 | — | MEDIUM | No rate limiter on the unauthenticated magic-link endpoint |
| **T-166** | 0 | 0 | — | MEDIUM | A conditional trust test's 503 branch can never execute again |
| **T-167** | 0 | 0 | — | MEDIUM | Four byte-identical copies of the module-binding shim, nothing enforcing agreement |
| **T-168** | 0 | 0 | — | MEDIUM | `bid_placed` is load-bearing twice — **a constraint, not a defect** |
| **T-174** | 0 | 0 | — | MEDIUM | A test `chflags -R hidden`s the shared `site-packages` for up to 600 s |
| **T-175** | 0 | 0 | — | MEDIUM | Neither price wall looks at `Offer.total_price` |

### Blocked — 17 tickets

| ticket | unblocks | frozen tests | parallel_safe | waiting on |
|---|---|---|---|---|
| T-081 | 5 | 1 | no | T-051 |
| T-072 | 4 | 2 | no | T-071 |
| T-034 | 3 | 3 | no | T-032 |
| T-042 | 3 | 3 | yes | T-041 |
| T-043 | 2 | 3 | yes | T-041 |
| T-082 | 2 | 0 | no | T-072, T-081, T-032, T-041 |
| T-083 | 2 | 0 | no | T-034, T-042, T-081 |
| T-035 | 1 | 1 | yes | T-032 |
| T-044 | 1 | 8 | yes | T-041 |
| T-084 | 1 | 0 | no | T-083 |
| T-085 | 1 | 2 | yes | T-082 |
| T-024 | 0 | 0 | no | T-023 |
| T-045 | 0 | 1 | yes | T-044 |
| T-054 | 0 | 0 | no | T-035, T-043, T-052, T-053 |
| T-073 | 0 | 2 | yes | T-072 |
| T-086 | 0 | 0 | no | T-043, T-053 |
| T-087 | 0 | 0 | yes | T-053, T-084, T-085 |

`depends_on` lists only unmet dependencies here; T-082's recorded dependency on T-061 and
T-062, T-083's on T-061, T-084's on T-062 and T-072's on T-033 are all satisfied and omitted.

## Scheduling constraints — measured this cycle, and a lane will get them wrong without them

These are not advice. Each was measured against `main` this cycle, and each describes a way
two tickets interact that neither ticket's own text records.

### 1. `bid_placed` is load-bearing twice, and T-030 is the trigger (T-168)

The `bid_placed` event **count** is pinned in two frozen places simultaneously:

- **T-082** asserts an exact per-kind multiset over the complete `LedgerEvent` enum (D34),
  including `bid_placed == n_stores_solicited` read from the run fixture.
- **T-086** proves shadow mode by the **absence** of any `bid_placed` event for the auction —
  acceptance criterion 3 says so explicitly, "not merely by an empty return value".
- **D24** forbids a nineteenth event kind, so neither can be given its own kind to disambiguate.

T-031 resolved the collision by making `annotate_bid_payload()` the primary path — it merges
fit into the bid's own payload and **emits nothing** — and documenting `record_fit_scores()`
as *being* the bid receipt rather than a second producer.

> **THE CONSTRAINT: when T-030 starts emitting real bid receipts, that call site MUST switch
> to the annotate path. If it does not, T-082 and T-086 break together, and the failure will
> look like a bug in whichever of the two runs first.**

Both functions live in `apps/exchange/src/retrieval/fit.py` (`annotate_bid_payload` at :188,
`record_fit_scores` at :269).

### 2. T-032 is unblocked, and there is exactly one correct join to use

T-031 landing in batch 2 cleared T-032's last dependency; it is READY and carries the largest
frozen-test load of any open ticket (10).

> **T-032 must call `intent_match_by_bid(bids, result.assessments)`.** It must **not** read
> `FitAssessment.fit_score`.

Three differently-scoped concepts share the name `fit_score` and the module says so in its own
docstring (`apps/exchange/src/retrieval/fit.py:95-99`):

- `FitAssessment.fit_score` (`fit.py:103`) — one float **per product**, this module's measurement.
- The ranker's `intent_match` — one float **per bid**, which is what T-032 consumes.
- `ShortlistSlot.fit_score` (`packages/contracts/generated/python/protocol.py:583`) — a third
  thing again, D29's.

`intent_match_by_bid(bids, assessments, *, auction_id="") -> dict[str, float | None]`
(`fit.py:236`) is the only implementation of the product→bid join. Doing the join by hand at
the call site is precisely where the three get conflated.

> **An unassessed bid returns `None`, never a substituted neutral.** The module's own header
> (`fit.py:35`) states that substituting a neutral value is wrong; the filter is expected to
> receive `fit_score: None` with a `fit_unavailable` reason. A ranker that coerces that `None`
> to 0.0 or 0.5 silently invents a measurement.

### 3. Buckets come from `build_profile`, never `build_buckets` (T-164)

`build_buckets()` (`apps/buyer/svc/src/profile/__init__.py:687`) and `anonymise_cohort()`
(:812) are public and run **no identity-leak check**. Only `build_profile()` (:1202) and
`build_profiles()` (:1234) do. This is harmless at HEAD only because every publishing path
currently goes through `build_profile`, and `publish_profile` accepts nothing but a
`BuyerProfile`.

> **Any lane that wires `publish_profile` must source its buckets from `build_profile`. Going
> through `build_buckets` bypasses the identity-leak backstop entirely.**

**A recorded location to distrust while you are in that file.** T-142 records its location as
`apps/buyer/svc/src/profile/__init__.py:492`. That is wrong and has been for at least two
merges: `publish_profile` was at **:1123** at `1b08077` and is at **:1280** at `e0ffd1a`
(measured with `grep -n '^def publish_profile'` at both shas). T-142 is refuted, so nothing
will be dispatched against that line — but the same staleness afflicts other `location` fields
and a lane that trusts one will read the wrong function.

### 4. T-041 carries T-152's requirement, and is in flight

T-041 is the highest-unblock open ticket (11 transitive open descendants) and is **currently
being worked in a worktree** — `proxyshop-worktrees/T-041` on `task/T-041` at `adec8c0`, three
commits ahead of `main` (`14dc739` the advocate runtime, `41e19b0` the offer must state an
expiry, `adec8c0` grade against the second approved envelope). **Do not dispatch it a second
time and do not mark it closed.**

Two requirements attach to it that its own record does not fully carry:

> **From T-152 (refuted as a standalone ticket, binding here): call
> `enforce_bid_provenance(bid, hooks)` on the WHOLE `Bid` — never on `bid.claims`.**
> Signature: `enforce_bid_provenance(bid: Any, hooks: Any, *, product_ref: str | None = None)`
> at `packages/store-agent/src/hooks/provenance.py:1098`. Passing the claims list defeats the
> price and floor walls, which read the offer body, not the claims.

> **From T-040 (already recorded in T-041's objective, repeated because it is a caller
> contract with no fail-closed guard): build one `ToolHooks` per auction.** Constructing a
> facade opens its first bid, so one-per-auction is correct with no ceremony. A facade reused
> across auctions must call `start_bid()` between them — the harness cannot detect a new
> auction by itself. A violation costs S5 audit fidelity (N bids tracing to one
> `authorize_discount` call), not price control; the live envelope re-check still bounds the
> economic damage.

### 5. Unenforceable scope strings — fifteen open tickets cannot be handed a file scope

A `scope` entry is supposed to be a glob a dispatcher can enforce. Fifteen open tickets record
prose there instead — parentheses, symbol names, line numbers, `vs`, `+`, brace expansion —
and none of it matches a path:

| ticket | recorded `scope` | the enforceable scope it should be |
|---|---|---|
| T-156 | `packages/store-agent/src/hooks/provenance.py (_price_reconciliation_refusal)` | `packages/store-agent/src/hooks/provenance.py` |
| T-158 | `apps/exchange/src/accept/offer.py (module docstring) + AuctionStateMachine` | `apps/exchange/src/accept/**` plus wherever `AuctionStateMachine` lives |
| T-159 | `apps/exchange/tests/test_checkout_provider.py:406 (and repo-wide)` | repo-wide sweep — needs an explicit file list, not a line number |
| T-160 | `tickets.json (T-109, T-111, T-123 verify fields)` | `tickets.json` — and see the warning below |
| T-161 | `packages/contracts/src/boundary.py:157 (_source_verdict)` | `packages/contracts/src/boundary.py` |
| T-162 | `packages/contracts/src/boundary.py (validate_bid) — external path` | `packages/contracts/src/boundary.py` |
| T-165 | `apps/buyer/svc/src/auth/routes.py (POST …) + routes.py:57 …` | `apps/buyer/svc/src/auth/routes.py` |
| T-167 | `apps/trust/src/{scoring,reconcile,feedback,snapshot}/_binding.py` | the four real files — brace expansion is not a glob; all four exist and are byte-identical (`md5 56d63d332af0fc3eb7d5bb5baf058f6c`) |
| T-168 | `apps/exchange/src/retrieval/ (record_fit_scores / annotate_bid_payload) + T-030` | `apps/exchange/src/retrieval/**` |
| T-169 | `apps/exchange/src/accept/offer.py:94,:328 + apps/exchange/src/accept/__init__.py` | `apps/exchange/src/accept/**` |
| T-170 | `apps/exchange/src/accept/ (no routes.py) vs apps/exchange/src/main.py` | `apps/exchange/src/accept/**`, `apps/exchange/src/main.py` |
| T-171 | `proxyshop_support/neo4j_lock.py:79,119-131 + pyproject.toml:71 + conftest.py` | `proxyshop_support/neo4j_lock.py`, `pyproject.toml`, `conftest.py` — **`pyproject.toml` and `conftest.py` are shared by every lane** |
| T-172 | `proxyshop_support/service_markers.py:78-85 (whole-stack fallback)` | `proxyshop_support/service_markers.py` |
| T-173 | `packages/store-agent/src/hooks/tools.py:687 … + tools.py:582-604 …` | `packages/store-agent/src/hooks/tools.py` |
| T-175 | `packages/store-agent/src/hooks/provenance.py:830-834 vs DESIGN.md` | `packages/store-agent/src/hooks/provenance.py` (DESIGN.md is read-only here) |

Two closed tickets carry the same defect and are recorded here only so the pattern is not
mistaken for new: T-152 (`packages/store-agent/src/runtime/ (empty) + enforce_bid_provenance`)
and T-153 (`packages/store-agent/src/hooks/tools.py (price arithmetic)`).

> **Do not hand an agent one of these strings as its file scope.** It will match nothing, and
> a scope that matches nothing is indistinguishable from a scope that permits everything
> depending on how the dispatcher fails.

**T-160 additionally proposes writing to `tickets.json` itself.** That is the authoritative
graph. Repairing three tickets' `verify` strings there is a legitimate goal, but it is an
orchestrator action against the source of truth, not a worker's file edit inside a worktree,
and it must not race a mint. Sequence it deliberately.

Ten further open tickets carry well-formed globs that currently match nothing because the work
has not been done yet — `fixtures/er/**` (T-022), `fixtures/mcp/**` (T-023),
`fixtures/interviews/**` (T-053), `apps/merchant/app/dashboard/**` (T-054),
`apps/buyer/app/intent/**` and `fixtures/dialogues/**` (T-071),
`apps/buyer/app/shortlist/**` (T-072), `apps/buyer/app/feedback/**` (T-073),
`docs/demo/starting-slice.md` (T-085), `e2e/test_onboarding.py` and
`e2e/support/onboarding/**` (T-086), `docs/demo/shopify-onboarding-extension.md` (T-087).
**Those are fine** — that is where the work will create files. Do not "fix" them.

### 6. Every finding ticket carries a placeholder `verify` that red-check refuses

All 22 open finding tickets record:

```
verify: false  # NO GATE YET — write one that fails on this finding first
```

> **A ticket whose `verify` is `false` cannot be closed by running it, and red-check will
> refuse to dispatch it.** The first work in each of these lanes is writing the gate that goes
> red on the described defect, and only then making it green. Their unblock count of 0 ranks
> them last in the frontier table and that is exactly the wrong way to read them: T-155 is
> CRITICAL and T-169 asserts that a frozen S8 release-blocker is currently passing on a
> decorative comparison.

This is the same failure class the graph is now tracking in three places at once, and it is
worth stating as one thing rather than three:

- **T-159** — tests wrapped in `except ImportError` are vacuous until their subject exists and
  report green the whole time. `apps/exchange/tests/test_checkout_provider.py:406` had never
  run for real until T-033 created the module, at which point it immediately went red.
- **T-160** — T-109, T-111 and T-123's recorded `verify` commands pass identically with and
  without their defect. Red-check measured it this cycle: T-111's and T-123's both exit 0 with
  nothing implemented (3046 and 210 tests selected); T-109's was green on the parent commit.
  This is the mechanism by which all three were reported closed twice while the defects stayed
  live.
- **T-166** — a conditional trust test whose 503 branch can never be taken again now that
  T-062 landed a real scorer, so it silently stopped covering the case it was written for.

**"A gate that cannot fail is indistinguishable from a gate that passes."** T-117 is the
harness-level instance of the same statement and is blocked on a human decision.

## Escalations that gate open tickets

### ESC-005 — gates T-117, filed cycle 10 at `1b08077`, OPEN

T-117 is CONFIRMED real and structurally undispatchable. `verify.sh check` runs
`-m 'not needs_model and not docker and not slow'`, which **deselected all three of T-110's
fresh-volume tests including the one grading its acceptance criterion 2** — so a lane can
report its gate green while the tests written to grade it never executed.

Why no worker can fix it: the fix belongs at `scripts/verify.sh:105`, a protected path
(`state.json` `protected_paths` = `.swarm-loop/acceptance`, `.swarm-loop/goals.json`,
`Makefile`, `scripts/verify.sh`, `scripts/check_verify_contracts.py`). `check-branch` vetoes
any branch touching it, correctly and every time. T-117's own `non_goals` forbid dodging the
veto by relocating the logic into an unprotected file, and its fallback scope
(`conftest.py` + `proxyshop_support/**`) collides head-on with the bootstrap work.

The proposed amendment makes deselection visible and fatal for a gate's own selection,
mirroring the existing exit-5 "collected 0 tests" rule. It requires `freeze --amend`, which is
the user's call and not the orchestrator's.

> **T-117 shows as READY in the frontier table because the graph says its only dependency
> (T-109) is closed. It is not dispatchable. Do not send an agent at it.**

### ESC-006 — gates T-081 and T-084 in practice, filed cycle 10 at `0e96d1f`, OPEN

`fixtures/manifest.json:213-246` `expected_trust_trajectory` contradicts the same document's
published `observation_weights`. Replaying all seven `dishonest_store.behaviours` once per
episode gives 0.234 at ep2 (stated band [0.34, 0.50]), 0.154 at ep4 (band [0.26, 0.40]), 0.084
at ep9 (band [0.14, 0.24]) and 0.066 at ep12 (band [0.08, 0.18]) — **every non-zero episode
falls outside its own stated band**, and the store crosses the 0.35 threshold at episode 1
rather than the intended ~4-6. Only episode 0 (the 0.5 prior) is inside.

Latent today: `test_e8_proofs.py:413` checks the trajectory's *shape*, not the engine's
numbers. **It stops being latent the moment T-081's simulator is graded against it** — T-084's
acceptance criterion 1 is "trajectory within manifest tolerance bands", so T-084 would fail
through no fault of its own and the failure would read as a simulator bug rather than a
document inconsistency. `fixtures/manifest.json` is T-080 single-writer ground truth with an
approval digest over its body; T-084's `non_goals` say "no manifest edits (would invalidate
ground truth)". Nobody in the swarm may resolve this.

## Open tickets

Twenty-six plan tickets and twenty-two finding tickets. Objectives, full acceptance arrays and
`non_goals` live in `tickets.json`; what follows is the scheduling-relevant summary.

### E2 — Ingestion

#### T-022 — Same products across stores link via entity resolution — READY
GTIN exact match plus an embedding+attribute matcher producing `SAME_AS` edges with
confidence and a configurable threshold.
`scope: services/ingest/src/er/**, services/ingest/tests/**, fixtures/er/**` ·
`verify: pytest services/ingest/tests/test_entity_resolution.py -q` · parallel_safe · 1 frozen test.
Two acceptance criteria: precision ≥ configured floor with GTIN matches always linking; edges
carry confidence and below-threshold pairs are not linked. Non-goal: no cross-category ontology.

#### T-023 — Catalog MCP adapter passes recorded-contract tests — READY
`catalog_mcp` `CatalogAdapter` implementing the production-primary read path against recorded
mocks (dev stores are not in Global Catalog — A1).
`scope: services/ingest/src/adapters/**, services/ingest/tests/**, fixtures/mcp/**` ·
`verify: pytest services/ingest/tests/test_catalog_mcp.py -q` · parallel_safe · 1 frozen test.
Non-goal: no live Shopify calls in verify.

#### T-024 — Differential refresh keeps the graph current — BLOCKED on T-023
Per-field cadence scheduler; only changed content re-extracts; `POST /refresh/{store}`.
`scope: services/ingest/src/scheduler/**, services/ingest/tests/**` ·
`verify: pytest services/ingest/tests/test_refresh.py -q` · **not** parallel_safe · 0 frozen tests.
Third criterion is the sharp one: no re-extraction without a hash change across a full cycle.

### E3 — Exchange

#### T-032 — Shortlists rank by the single published formula — READY, 10 frozen tests
Eligibility filters (blacklist fail-closed, offer expiry, checkout-domain validity, hard
constraints requiring verified facts per R19), then the published `rank_score` with
[0,1]-normalized features, policy penalties, stable tie-breaking, a component map and a
human-readable explanation; slotting ≤ 4 with graceful collapse; provenance and
verification-warning labels. The eligibility filter is the R12 ranking gate (D54): `rank()`
consults `SellerEligibility`, excludes the ineligible candidate with a recorded exclusion
reason, and fails closed on an unavailable read. Trust inputs are the six-dimension
`TrustSnapshot` (D53) — ranking reads score/confidence and never re-derives dimensions.
`scope: apps/exchange/src/ranking/**, apps/exchange/tests/**` ·
`verify: pytest apps/exchange/tests/test_ranking.py -q` · **not** parallel_safe.
Four criteria including a blindness test (tier/fee fields cannot alter score; a contradicted
hard constraint never wins — release blocker) and a by-DENIAL ranking-gate test with a positive
control, so a ranker that excludes everything fails. **See constraint 2 before writing a line
of it.**

#### T-034 — Exchange bandit shifts exposure with outcomes — BLOCKED on T-032
Contextual Thompson sampling over cluster × store adjusting exposure and exploration, not
formula weights; guaranteed exploration slice for low-data stores from the trust snapshot.
`scope: apps/exchange/src/policy/**, apps/exchange/tests/**` ·
`verify: pytest apps/exchange/tests/test_bandit.py -q` · **not** parallel_safe · 3 frozen tests.
Blacklisted stores must draw zero exposure.

#### T-035 — Loss reports aggregate reasons without leaking amounts — BLOCKED on T-032
Windowed job producing a `LossReport` per store: reason categories from the ranking formula's
dominant term, unmet criteria for fit losses, delayed release.
`scope: apps/exchange/src/reports/**, apps/exchange/tests/**` ·
`verify: pytest apps/exchange/tests/test_loss_reports.py -q` · parallel_safe · 1 frozen test.
Criterion 2 is a property test: no prices, amounts or rival ids in the output schema.

### E4 — Store agent and sellers

#### T-041 — Advocate runtime bids within walls — READY, **IN FLIGHT**, unblocks 11
`BidRequest → Bid | decline`: cache-layout context assembly, hook-only claim emission,
deterministic cold-start bid (list price + standing commitments + intro rule only).
`scope: packages/store-agent/src/runtime/**, packages/store-agent/tests/**` ·
`verify: pytest packages/store-agent/tests/test_runtime.py -q` · **not** parallel_safe · 2 frozen tests.
**Being worked at `proxyshop-worktrees/T-041`, branch `task/T-041`, `adec8c0`, 3 commits ahead
of `main`. See constraint 4 for the two requirements it must satisfy.**

#### T-042 — Store loop learns from its own outcomes only — BLOCKED on T-041
Per-store Thompson sampling over discount-depth buckets × commitment sets per cluster;
network-prior intake; the prior builder consumes pitch/value-prop outcomes only.
`scope: packages/store-agent/src/learning/**, packages/store-agent/tests/**` ·
`verify: pytest packages/store-agent/tests/test_learning.py -q` · parallel_safe · 3 frozen tests.
Criterion 2 is schema-level: the prior builder rejects and never reads discount fields.

#### T-043 — Shadow mode logs would-be bids; trust events adjust the agent — BLOCKED on T-041
Shadow activation gates submission but logs full bids and rationale to sealed store state;
`TrustEventPayload` intake adjusts policy and commitment posture, visibly in the rationale.
`scope: packages/store-agent/src/modes/**, packages/store-agent/tests/**` ·
`verify: pytest packages/store-agent/tests/test_shadow_trust.py -q` · parallel_safe · 3 frozen tests.
**Its "zero submissions" guarantee is what T-086 proves by the absence of `bid_placed` —
constraint 1.**

#### T-044 — External bids enter signed with a required envelope — BLOCKED on T-041, 8 frozen tests
Signature registration and verification on the external door
`POST /v1/auctions/{auction_id}/bids`; schema validation admits `seller_asserted` claims and
free-text, marks the bid unverified, and enqueues extraction+verification carrying the
submission's nonce as its idempotency key. The hosted solicitation path
(`POST /v1/bid-requests`) is unchanged. **Amendment 1 (D52) makes the envelope REQUIRED** —
`signer_id`, `key_id`, `issued_at`, `nonce`, `schema_version` all mandatory, and a submission
missing any one is rejected before enqueue.
Public surface, all in `packages/store_agent/src/external`: `sign_bid`, `receive_bid`,
`canonical_signing_bytes`, `NonceStore`. The keyring is `{signer_id: {key_id: secret}}` backed
by `app.seller_endpoints`; **a lookup keyed on `key_id` alone is wrong** because two signers
may reuse a `key_id` string, and an unknown `(signer_id, key_id)` pair must reject rather than
fall back to another key of that signer.
`scope: packages/store-agent/src/external/**, packages/store-agent/tests/**` ·
`verify: pytest packages/store-agent/tests/test_external_bids.py -q` · parallel_safe.
Seven acceptance criteria — the heaviest in the graph — covering the required envelope,
canonical-bytes coverage, key rotation, replay and freshness in both time directions.

#### T-045 — Three seller personas exercise the external door adversarially — BLOCKED on T-044
`apps/seller-reference`: value / specialist / aggressive personas with identity, catalog scope,
tone and offer policy; free-text pitches with asserted claims via the external `/bid` path; the
aggressive persona emits manifest-scripted false and unsupported claims. Personas may tailor
pitches but never redefine catalog facts.
`scope: apps/seller-reference/**` · `verify: pytest apps/seller-reference -q` · parallel_safe · 1 frozen test.

### E5 — Merchant

#### T-051 — Pixel reports checkout outcomes the collector can join — READY, unblocks 6
Web pixel extension subscribing standard events; `POST` via `fetch` keepalive to the collector
with `clientId`, checkout token, order id, `discountApplications`; the collector persists to
the app schema and forwards `LedgerEvent`s.
`scope: pixel/**, apps/merchant/svc/src/collector/**, apps/merchant/svc/tests/**` ·
`verify: npx vitest run pixel && pytest apps/merchant/svc/tests/test_collector.py -q` ·
**not** parallel_safe · 2 frozen tests. Criterion 3: no PII fields accepted, schema rejects.

#### T-052 — Winning offers become single-use validated codes and permalinks — READY
The Shopify adapter **behind T-036's `CheckoutProvider` port** — one implementation of it, not
the only route to a code. `/codes`: `discountCodeBasicCreate` `usageLimit:1` plus expiry, a
validity and `combinesWith` pre-check, a unique code per accepted offer, permalink
construction; duplicate redemption surfaces as an offer-integrity event (A5).
`scope: apps/merchant/svc/src/codes/**, apps/merchant/svc/tests/**` ·
`verify: pytest apps/merchant/svc/tests/test_codes.py -q` · **not** parallel_safe · 3 frozen tests.
Criterion 4 pins the substitution property: swapping `SimulatedRedirectProvider` for it changes
no caller and no emitted `LedgerEvent` kind (C11).

#### T-053 — A plain-language interview yields an approved, versioned envelope — READY, unblocks 3
Onboarding interview service: LLM interview (double in tests, transcript fixture), envelope
draft in plain English, merchant written-approval gate, versioned persistence to the sealed
schema, shadow by default.
`scope: apps/merchant/svc/src/onboarding/**, apps/merchant/svc/tests/**, fixtures/interviews/**,
apps/merchant/svc/src/envelope/**` ·
`verify: pytest apps/merchant/svc/tests/test_onboarding.py -q` · parallel_safe · 3 frozen tests.
Non-goal: no walls enforcement — T-040 owns it.

#### T-054 — The dashboard shows the walls and the window — BLOCKED on T-035, T-043, T-052, T-053
Bid log with rationale, loss reports, trust breakdown with event payloads, versioned envelope
editor, kill switch wired to agent mode, verification outcomes per bid.
`scope: apps/merchant/app/dashboard/**` · `verify: npx vitest run apps/merchant/app/dashboard` ·
**not** parallel_safe · 0 frozen tests. Criterion 3 is a render test asserting the *absence* of
amount fields in the loss-report pane.

### E7 — Buyer

#### T-071 — Three questions or fewer produce a confirmed structured intent — READY, unblocks 5
Buyer agent: clarification loop capped at 3, `Intent` construction plus a buyer-confirmation
UI, profile bucket builder with a k-floor config.
`scope: apps/buyer/svc/src/intent/**, apps/buyer/app/intent/**, apps/buyer/svc/tests/**,
fixtures/dialogues/**` ·
`verify: pytest apps/buyer/svc/tests/test_intent.py -q && npx vitest run apps/buyer/app/intent` ·
**not** parallel_safe · 3 frozen tests.
**Its bucket builder is subject to constraint 3** — buckets through `build_profile`, never
`build_buckets`.

#### T-072 — Shortlists render with provenance and accept hands off cleanly — BLOCKED on T-071
Shortlist UI: slots, trust indicator, provenance labels ("store-confirmed" vs "from their
website"); accept → exchange accept → redirect to permalink.
`scope: apps/buyer/app/shortlist/**, apps/buyer/svc/src/accept/**, apps/buyer/svc/tests/**` ·
`verify: npx vitest run apps/buyer/app/shortlist && pytest apps/buyer/svc/tests/test_accept_flow.py -q` ·
**not** parallel_safe · 2 frozen tests.
**T-170 asserts there is currently no HTTP path from a buyer's accept to the exchange's
`accept()`. If T-170 is real, T-072 criterion 3 cannot pass until it is fixed.**

#### T-073 — Routed buyers can answer one structured feedback prompt — BLOCKED on T-072
Post-purchase feedback UI plus intake wiring to the trust feedback API; one prompt, structured
options, no free text.
`scope: apps/buyer/app/feedback/**, apps/buyer/svc/src/feedback/**, apps/buyer/svc/tests/**` ·
`verify: npx vitest run apps/buyer/app/feedback && pytest apps/buyer/svc/tests/test_feedback.py -q` ·
parallel_safe · 2 frozen tests. Criterion 3 is a render test: no free-text field exists.

### E8 — Proofs, fixtures and runbooks

#### T-081 — Simulated buyers exercise the whole network from one seed — BLOCKED on T-051
`services/sim`: seeded traffic generator driving intents → auctions → acceptances → stub
purchases and returns; the dishonest-store script executes the manifest behaviours.
`scope: services/sim/**` · `verify: pytest services/sim -q` · **not** parallel_safe · 1 frozen test.
**ESC-006 is about the document this ticket is graded against.** Criterion 2 says the dishonest
script emits exactly the manifest behaviours; the manifest's own trajectory disagrees with its
own weights under once-per-episode replay.

#### T-082 — One scripted run proves the full S1 flow — BLOCKED on T-072, T-081, T-032, T-041
E2E: intent → clarify → bids → shortlist → accept → code → stub checkout → pixel + webhook →
reconcile → ledger → trust update, all against compose.
`scope: e2e/**` · `verify: pytest e2e/test_s1_flow.py -q` · **not** parallel_safe · 0 frozen tests.
**Criterion 1 is the exact per-kind multiset over the complete `LedgerEvent` enum (D34) — this
is one half of constraint 1.** Criterion 4 carries two release blockers: no blacklisted seller
eligible anywhere in the run, no off-domain checkout URL returned. **T-169 asserts the frozen
off-domain blocker currently passes on a decorative comparison.**

#### T-083 — Both learning loops demonstrably move — BLOCKED on T-034, T-042, T-081
S4 assertions: outcome shifts reorder shortlists in-cluster (exchange loop); a store's
discount-depth distribution tracks its own record (store loop); a control store is unaffected.
`scope: e2e/**` · `verify: pytest e2e/test_learning.py -q` · **not** parallel_safe · 0 frozen tests.

#### T-084 — The dishonest store ends below threshold and off the shortlist — BLOCKED on T-083
Episode test for S2: run the manifest budget of simulated episodes; assert the trust trajectory
matches the approved manifest, ends blacklisted, and the store disappears from subsequent
shortlists.
`scope: e2e/**` · `verify: pytest e2e/test_dishonest.py -q` · **not** parallel_safe · 0 frozen tests.
Non-goal: no manifest edits — they would invalidate ground truth.
**This is the ticket ESC-006 will fail if it is not resolved first.**

#### T-085 — The starting-slice demo runbook — BLOCKED on T-082
`docs/demo/starting-slice.md`: the offline starting-path demo — compose bring-up against
`services/shopify-stub`, `make demo-seed`, and the live-auction beat end to end through
`SimulatedRedirectProvider`. Requires no Shopify dev store, no app install, no merchant
onboarding. **Also owns `docs/tests/test_runbook.py`, the structure lint both runbooks are
checked by.**
`scope: docs/demo/starting-slice.md, docs/tests/**` · `verify: pytest docs/tests/test_runbook.py -q` ·
parallel_safe · 2 frozen tests.

#### T-086 — Onboarding drives a shadow store to its first real bid — BLOCKED on T-043, T-053
Integration test walking the whole S6 seam: mocked interview transcript → economic envelope →
written merchant approval → shadow mode logging would-be bids without submitting → activation
turns the same live intent into a real submitted bid.
`scope: e2e/test_onboarding.py, e2e/support/onboarding/**` ·
`verify: pytest e2e/test_onboarding.py -q` · **not** parallel_safe · 0 frozen tests.
**Criteria 3 and 4 are the other half of constraint 1**: shadow is proved by the *absence* of
any `bid_placed` event for the auction, and activation by its *appearance*, so the test fails
if activation is a no-op in either direction. Non-goal: no new product code — a gap it finds is
a defect report against T-053 or T-043, not a fix here.

#### T-087 — The Shopify and onboarding extension runbook — BLOCKED on T-053, T-084, T-085
`docs/demo/shopify-onboarding-extension.md`: dev-store provisioning (app install, storefront
password, Bogus Gateway), the `make e2e-live` procedure, the interview → bidding onboarding
beat, and the dishonest-store trust beat.
`scope: docs/demo/shopify-onboarding-extension.md` · `verify: pytest docs/tests/test_runbook.py -q` ·
parallel_safe · 0 frozen tests.
**T-085 and T-087 share one verify command and T-087 does not own it.** `docs/tests/**` is
T-085's scope and T-087's `non_goals` forbid editing it. T-085's criterion 3 makes the lint
check the *union* of every markdown file under `docs/demo/`, so T-087's file is graded by a
test T-087 may not touch — correct by design, and a trap if the two are dispatched together
without that in hand.

### Infrastructure

#### T-117 — The per-ticket gate runs the tests that grade its own acceptance — NOT DISPATCHABLE
CONFIRMED real, blocked on **ESC-005**. `scope: scripts/verify.sh, conftest.py,
proxyshop_support/**` · `verify: ./scripts/verify.sh check` · **not** parallel_safe.
Non-goals, both binding: do not touch `scripts/verify.sh` without a user-approved amendment,
and do not work around the veto by moving the logic elsewhere. Full detail in the escalations
section above.

### Open finding tickets — 22, none with a gate

Every one of these carries `verify: false  # NO GATE YET`, no `acceptance` array and no frozen
test. Severity distribution: **1 CRITICAL, 9 HIGH, 12 MEDIUM.**

#### T-155 — CRITICAL — `MAX_SWEEP_DEPTH = 12` truncates the provenance walk silently
`packages/store-agent/src/hooks/provenance.py:447`. A priced node, claim or discount nested
13+ levels deep in a dict-shaped bid is admitted having been inspected by nothing. **This
bypasses every boundary wall** — the claim provenance walk, the floor wall and the new price
reconciliation alike — so it is strictly wider than the T-153 defect that surfaced it. Raising
the bound does not fix it; refusing everything past the bound would refuse an honestly deep
metadata blob, which `_sweep` is written to tolerate. The bound exists to stop a
self-referential dict hanging the boundary, so **the correct fix is cycle detection replacing
the depth bound.** Measured: wrap `{"product_ref": "prod-cap", "unit_price": 1.0}` in 14 nested
dicts and it is ADMITTED.

#### T-154 — HIGH — the trust tests connect as the wrong DB principal
`apps/trust/tests/_fixtures_events.py:42`. `EVENTS_ROLE = "app"` with a comment asserting `app`
is deliberately not `trust_rw` — false since T-151: compose, `.env.example`,
`proxyshop_support.postgres.ROLES` and the T-151 fix all say the shipped ledger writer is
`trust_rw`. The two roles have **different grant sets** (`app`: SELECT+INSERT on ledger, full
DML on app and sealed; `trust_rw`: full DML on ledger, read-only on app, nothing in sealed), so
every DB-backed test in `apps/trust` is evidence about a principal the deployment does not use.

#### T-157 — HIGH — an off-domain permalink leaves a live code with no ledger record
`apps/exchange/src/checkout/provider.py:209-222`. The offer's `checkout_url` is checked BEFORE
the mint, but an offer with **no** url — the legal R10 list-price fallback shape `collect_bids`
manufactures — gives that check nothing to look at. The first failable host comparison is the
one on the permalink returned BY mint, i.e. after `POST /codes` has already issued a real
discount code. Measured on `task/T-033`: code `PSX-REALCODE` live, accept correctly returns no
permalink and leaves the auction open, and **no `code_created` event exists for that code**.
Carrying the minted code out on `OffDomainCheckout` so the exchange can revoke it is T-036's file.

#### T-160 — HIGH — three tickets' `verify` commands cannot distinguish fixed from broken
`tickets.json`, the T-109/T-111/T-123 `verify` fields. Red-check measured it this cycle:
T-111's `./scripts/bootstrap.sh && ./scripts/verify.sh check` and T-123's
`bootstrap.sh && pytest packages/llm && python -c 'import contracts, llm, trust'` **both exit 0
with nothing implemented** (3046 and 210 tests selected), and T-109's
`pytest apps/trust -q && pytest proxyshop_support -q` was green on the parent commit. This is
the precise mechanism by which all three were reported closed twice while the defects stayed
live. The lanes have now written real gates; the graph's `verify` strings should point at them.
**Writing to `tickets.json` is an orchestrator action — see the scope warning above.**

#### T-163 — HIGH — a `psn-` prefix is treated as a bearer credential
`apps/buyer/svc/src/auth/sessions.py:170`. T-133 item (c): `SessionStore.open()` validates only
the `psn-` PREFIX and never checks vault membership, so any `psn-`-prefixed string opens a
session. The T-139 lane deferred this deliberately and correctly — closing it requires
threading a vault reference into `SessionStore`, changing that seam's contract and touching
`MagicLinkAuth` wiring plus every `SessionStore` subclass. **The largest remaining R5 auth gap.**

#### T-169 — HIGH — the registered-domain guard is decorative in the deployed configuration
`apps/exchange/src/accept/offer.py:94,:328` + `__init__.py:17-21`. `_platform_domains` is module
state initialised to `None` and nothing in the repository calls `use_registered_domains`, so
under `uvicorn exchange.main:app` the resolution yields `None` and `registered_domain_for`
falls back to `bid['store_domain']` — **a field the bidder controls**. The frozen S8-3 blocker
`test_offdomain_checkout_url_is_refused` therefore passes on that decorative comparison, so
`spec_criteria_passing` counts a decorative pass toward its target. Worse: the package's own
wiring docstring names `apps.exchange.src.accept` while the served app reads `exchange.accept`
— the same file by inode, **two different module objects**, so wiring one spelling leaves the
other unwired and an accept through the unwired spelling mints a real single-use code for an
unregistered host. `apps/trust/src/ledger/__init__.py` and
`packages/store-agent/src/hooks/__init__.py` both already implement `_install_canonical_alias()`
for exactly this hazard; the accept package skipped the convention.

#### T-170 — HIGH — the accept package has no HTTP route
`apps/exchange/src/accept/` has no `routes.py`. `main.py` discovers routers by globbing
`<feature>/routes.py`, so the booted app mounts only `exchange.auction.routes` and the exchange
OpenAPI exposes exactly `['/auctions', '/auctions/{auction_id}']`. **There is no HTTP path by
which a buyer's accept can reach `accept()` or `accept_offer()`.** The accept half is complete,
tested and lint-enforced, and no deployed path is protected by any of it.

#### T-171 — HIGH — the Neo4j lock is machine-global and its own timeout is unreachable
`proxyshop_support/neo4j_lock.py:79,119-131` + `pyproject.toml:71` + `conftest.py:310-314`. The
D37 lock is at `/private/tmp/proxyshop-neo4j.lock` and `PROXYSHOP_WORKER` does not isolate it,
so it is shared across every worktree and every concurrent lane. Its 600 s wait is unreachable
by construction because the repo-wide `addopts` carry `--timeout=300`: under contention the
session-scoped `_neo4j_guard` fixture dies inside `time.sleep(poll)` with a pytest-timeout
failure instead of the diagnosable `Neo4jLockTimeout` the module exists to raise. **A
swarm-scale hazard: it turns lock contention into an undiagnosable red that looks like a
product failure.**

#### T-172 — HIGH — T-109's per-service inference has an eight-item hole, all Postgres-only
`proxyshop_support/service_markers.py:78-85`. `services_for` reads a marker argument first,
else the fixture closure against a fixed 7-entry `FIXTURE_SERVICES` map; a test with a bare
`@pytest.mark.docker` requesting none of those seven falls back to the whole stack. Tests that
spin their own fresh-volume Postgres container use no shared fixture and land in that fallback,
so **a Redis-only outage still silently skips them at exit 0** — the pre-T-109 defect,
surviving for exactly these. Five of the eight are the schema-grants and role-password security
tests (`apps/trust/tests/test_schema_grants.py`,
`proxyshop_support/tests/test_role_password_end_to_end.py`) — the same class of check whose
silent skipping the ticket was written to stop. One instance was reintroduced by the batch-2
merge itself: `apps/trust/tests/test_events_hardening.py:146`, T-151's own new test, points at
a closed loopback port and needs no datastore, yet is classified whole-stack.

#### T-173 — HIGH — a removed line turned a loud failure into a silent fail-open
`packages/store-agent/src/hooks/tools.py:687` (`_evaluate_discount`) and :582-604 (the new
`ToolHooks.list_price`). Batch 1 replaced
`list_price = float(listing.get("list_price") or 0.0)` with
`list_price = self.list_price(product_ref) or 0.0`. The new `list_price()` swallows
`TypeError`/`ValueError` and returns `None` for a non-numeric catalog entry; the `or 0.0` at
the call site then coerces that `None` into a real number. **A malformed catalog entry that
previously raised now silently prices at 0.0 inside the discount evaluator** — the exact class
of defect T-153 was created to close, relocated into the code that closes it. The per-branch
removed-lines audit cleared it as benign; only the combined diff shows it.

#### T-156 — MEDIUM — `total_price` is never reconciled against `unit_price`
`packages/store-agent/src/hooks/provenance.py` (`_price_reconciliation_refusal`). An offer
stating `unit_price=80.0` with `total_price=1.0` is admitted. `Offer` carries no quantity and
the quantity that would relate the two lives at `apps/exchange/src/checkout/codes.py:111`, so
the relation is not decidable from a bid alone. The T-153 lane scoped this out explicitly and
documented it in the refusal's docstring rather than leaving it silent. **Closing it requires
deciding where quantity enters the bid — a design question, not a bug fix.** See T-175 for the
same gap with the DESIGN citation attached.

#### T-158 — MEDIUM — the one-accept-per-auction guard is only as durable as the record handed in
`apps/exchange/src/accept/offer.py` + `AuctionStateMachine`. `accept()` stamps the auction
OBJECT it receives. A route that loads an `AuctionRecord` from `RedisAuctionStore`, accepts,
and does not save it back leaves an unstamped record for the next request; and two concurrent
requests reading it can both pass the guard, because the check-then-act window spans the whole
provider call including merchant I/O. **The durable guard belongs on `AuctionStateMachine`'s
already-serialised ACCEPTED transition.**

#### T-159 — MEDIUM — `except ImportError`-tolerant tests are vacuous until their subject exists
`apps/exchange/tests/test_checkout_provider.py:406`, and repo-wide.
`git grep -n 'except ImportError' -- '*test*'` and check each for an assertion that only runs
when the import succeeds. Needs a repo-wide sweep for those and for skip/xfail guards whose
condition is permanently true. **Companion to T-160 and T-166 — see the placeholder-verify
section.**

#### T-161 — MEDIUM — provenance nested inside a claim's opaque `value` is not walked
`packages/contracts/src/boundary.py:157` (`_source_verdict`). Measured:
`make_bid(claims=[{"key":"x","value":{"provenance": seller_asserted}, "provenance": hook}])`
returns hosted `ok=True`; `_source_verdict` reads `holder["provenance"]` only. **Not fixed by
the T-135 lane by deliberate choice**: `Claim.value` is typed `Any`, and walking arbitrary
values for provenance-shaped dicts would reject legitimate structured values. Speculative
today — nothing downstream reads it — and closing it needs a schema decision about `Claim.value`.

#### T-162 — MEDIUM — the external path never checks discount authorisation
`packages/contracts/src/boundary.py` (`validate_bid`, external path). The boundary checks
provenance SOURCE but never discount AUTHORISATION:
`make_bid(claims=[make_claim("policy", {"authorized_discount_pct": 25.0}, hook_provenance)])`
returns hosted `ok=True`. The boundary cannot do better alone — it holds no hook ledger. The
store-agent guard covers this for hosted Tier-1
(`packages/store-agent/src/hooks/provenance.py:555`) but **there is no equivalent for the
external path**, so an external submitter's stated discount authorisation is unchecked by
anything.

#### T-164 — MEDIUM — two public profile entry points run no identity-leak check
`apps/buyer/svc/src/profile/__init__.py:687`. See constraint 3 — this is the ticket that
constraint exists to protect. Fix by running the check inside `build_buckets` or by making the
unguarded entry points private.

#### T-165 — MEDIUM — no rate limit on the unauthenticated magic-link endpoint
`apps/buyer/svc/src/auth/routes.py` (`POST /buyer/auth/magic-link`) and :57
`ProcessLocalStateUnsafe`. The session and pending-link store behind it is process-local, so
state is neither shared across replicas nor durable. T-133 item (b) closed the unbounded-growth
half (sweep expired, then refuse with `SessionsExhausted` at `max_sessions=50000`,
refuse-don't-evict). **The request-rate half needs shared infrastructure plus a migration and
is correctly outside a buyer-scoped lane.**

#### T-166 — MEDIUM — a conditional trust test's 503 branch can never execute again
`apps/trust/tests/test_events.py:1159`.
`test_replay_with_snapshots_names_the_missing_scorer` branches on
`if response.status_code == 503`. Now that T-062 has landed a real scorer it always takes the
200 branch, so the 503 half is dead code — a conditional test that silently stopped covering
the case it was written for. It is `@pytest.mark.docker` (deselected by `verify.sh check`); the
T-062 lane confirmed the 200 branch is satisfied and left it alone as T-060's file.

#### T-167 — MEDIUM — four byte-identical copies of the module-binding shim
`apps/trust/src/{scoring,reconcile,feedback,snapshot}/_binding.py`, all
`md5 56d63d332af0fc3eb7d5bb5baf058f6c`. Four copies because the sequencing must run before the
first relative import and there was no shared home inside one ticket's ownership. **Nothing
enforces that they stay identical**, so a fix applied to one and not the others reintroduces
the dual-spelling module-identity bug the shim exists to prevent — a bug this project already
paid for once (5/5 runs reproduced it). A future ticket owning `apps/trust/src/` should hoist
it to a single module. **T-169 is the same module-identity hazard in the accept package.**

#### T-168 — MEDIUM — `bid_placed` is load-bearing twice
**Not a defect in the landed code — a cross-ticket scheduling constraint, recorded in full as
constraint 1 above.** It is carried in the graph as a ticket so it cannot be lost; closing it
means making T-030's call site switch to the annotate path, not fixing anything today.

#### T-174 — MEDIUM — a test hides the shared `site-packages` for up to 600 s
`proxyshop_support/tests/test_bootstrap_provisioning.py:211-265`.
`test_the_flat_namespaces_survive_a_pytest_run` runs `chflags -R hidden` on the checkout's
**real shared** `.venv/lib/python3.12/site-packages` — the one piece of state
`PROXYSHOP_WORKER` cannot isolate — then runs a nested pytest with a 600 s timeout before
restoring it in a `finally`. For that entire window every other Python process starting in this
checkout loses the `.pkgroot` namespaces and fails with `ModuleNotFoundError`. **That breaks
any concurrently running service, `make demo-seed`, a sibling lane, or a metric measurement** —
and `build_succeeds` has already recorded four false zeros in six cycles.

#### T-175 — MEDIUM — neither price wall looks at `Offer.total_price`
`packages/store-agent/src/hooks/provenance.py:830-834` vs `DESIGN.md:127`. `PRICE_FIELD ==
'unit_price'`, so a bid can state a fully honest `unit_price` and an arbitrary `total_price`
and be admitted — the same unauthorised-discount shape T-153 closed, moved one field over.
`DESIGN.md:127` specifies the ranking price dimension as a function of `total_price`, and the
C11 accepted-event payload records `total_price` verbatim, **so a downstream consumer can treat
an unreconciled number as authoritative.** Companion to T-156, with the design citation attached.

## Shared scope globs — per-file ownership, not `parallel_safe`, is what separates them

`parallel_safe` is a property of the ticket, not of the pair. Four open globs are shared by
more than one open ticket and must not be co-scheduled on the strength of the flag alone:

| glob | open tickets writing it |
|---|---|
| `e2e/**` | T-082, T-083, T-084 — plus T-086, which writes `e2e/test_onboarding.py` and `e2e/support/onboarding/**` inside the same tree |
| `apps/exchange/tests/**` | T-032, T-034, T-035 |
| `packages/store-agent/tests/**` | T-041, T-042, T-043, T-044 |
| `apps/merchant/svc/tests/**` | T-051, T-052, T-053 |
| `services/ingest/tests/**` | T-022, T-023, T-024 |

Additionally: **T-171's scope names `pyproject.toml` and `conftest.py`, both repo-root files
every lane depends on.** A branch touching either serializes against the whole board.
**T-117's fallback scope (`conftest.py`, `proxyshop_support/**`) collides with the same files**,
and T-172 and T-174 both write under `proxyshop_support/`.

Three open tickets enclose most of the board by scope: T-032 and T-034/T-035 share
`apps/exchange/tests/**`; T-041 encloses T-042/T-043/T-044 through
`packages/store-agent/tests/**`; T-085 owns `docs/tests/**` on which T-087 is graded.

## Neo4j is ONE shared database

D4, narrowed by D38: there is a single Neo4j instance, and `PROXYSHOP_WORKER` does not shard
it. Any two lanes whose tests touch the graph serialize on the machine-global lock at
`/private/tmp/proxyshop-neo4j.lock` — and per **T-171** that lock's own timeout is currently
unreachable, so contention surfaces as an undiagnosable pytest-timeout rather than a named
lock error. The graph-writing open tickets are T-022, T-023 and T-024.

## What this file does not decide

- **It does not adjudicate the "no production caller" class.** Nine refutations and two fresh
  HIGH tickets take opposite positions on it. Both are recorded above; whoever dispatches
  T-169 or T-170 resolves it.
- **It does not rule on ESC-005 or ESC-006.** Both need a human decision, and both block real
  tickets (T-117 outright; T-081 and T-084 on their grading document).
- **It does not repair `tickets.json`.** The stale `location` on T-142, the prose `scope`
  strings, and the vacuous `verify` commands on T-109/T-111/T-123 are all defects **in the
  authoritative graph**, listed here as findings. Fixing them means editing `tickets.json`
  through the orchestrator — never editing this file to agree with a repair that has not
  happened.
