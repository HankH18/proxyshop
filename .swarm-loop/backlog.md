# ProxyShop — ticket ledger (open tickets + scheduling constraints)

Seeded verbatim from `tickets.json`, then amended by the approved intake B7 fix and the
cycle-0 plan edits (§ *Applied plan edits* below).
A closed ticket LEAVES this file the epoch it closes.

**43 open tickets · 130 open acceptance criteria · 8 epics.** None dispatched.
(T-000 closed in cycle 0 — 3 criteria — and now lives in `reports/cycle-0.md` and git history.)

`gate` = blocked by the human-gated T-080 (needs Hank's manifest approval).
Ownership is PER FILE, not per glob — use intake-report.md §4, not `parallel_safe`.

| ID | epic | title | depth | unblocks | gate | depends_on | verify |
|---|---|---|---|---|---|---|---|
| **T-010** | E1 | Contracts generate typed models and enforce the dual-path bid boundary | 1 | 26 | — | T-000 | `pytest packages/contracts -q && npx vitest run packages/contracts` |
| **T-013** | E1 | Shopify stub reproduces the exact surface the system uses | 1 | 25 | — | T-000 | `pytest services/shopify-stub -q` |
| **T-011** | E1 | Postgres schemas enforce role isolation and hash-chained ledger | 1 | 24 | — | T-000 | `pytest apps/trust/tests/test_schema_grants.py apps/trust/tests/test_ledger_chain.py -q` |
| **T-014** | E1 | LLM client with per-role config, cache-first prompts, and test doubles | 1 | 23 | — | T-000 | `pytest packages/llm -q` |
| **T-012** | E1 | Neo4j attribute-node catalog with vector retrieval through EmbeddingProvider | 1 | 18 | — | T-000 | `pytest services/ingest/tests/test_graph.py -q` |
| **T-020** | E2 | Signed fetch adapter ingests a storefront including password-protected dev stores | 2 | 16 | — | T-000, T-012 | `pytest services/ingest/tests/test_signed_fetch.py -q` |
| **T-080** | E8 | The approved manifest and seed generator define ground truth | 2 | 15 | YES | T-013 | `pytest fixtures/tests/test_seed.py -q` |
| **T-060** | E6 | Every event lands once, chained, and replays exactly | 2 | 14 | — | T-011 | `pytest apps/trust/tests/test_events.py -q` |
| **T-030** | E3 | Auctions fan out, time out, and always represent every store | 2 | 13 | — | T-010, T-013 | `pytest apps/exchange/tests/test_auction.py -q` |
| **T-050** | E5 | Installing the app wires pixel and webhooks against the stub | 2 | 13 | — | T-013 | `npx vitest run apps/merchant && pytest apps/merchant/svc/tests/test_install.py -q` |
| **T-040** | E4 | Tool hooks are the only way facts and discounts enter a bid | 2 | 11 | — | T-010, T-011 | `pytest packages/store-agent/tests/test_hooks.py -q` |
| **T-070** | E7 | Buyers authenticate lightly and stores never see who they are | 2 | 5 | — | T-010, T-011 | `npx vitest run apps/buyer && pytest apps/buyer/svc/tests/test_auth_vault.py -q` |
| **T-021** | E2 | Policy pages and marketing claims land in the graph with provenance | 3 | 13 | — | T-014, T-020 | `pytest services/ingest/tests/test_extraction.py -q` |
| **T-041** | E4 | Advocate runtime bids within walls and defaults deterministically cold | 3 | 10 | — | T-014, T-040 | `pytest packages/store-agent/tests/test_runtime.py -q` |
| **T-052** | E5 | Winning offers become single-use validated codes and permalinks | 3 | 9 | — | T-050 | `pytest apps/merchant/svc/tests/test_codes.py -q` |
| **T-031** | E3 | Candidate retrieval and fit scoring feed the ranker | 3 | 7 | — | T-012, T-030 | `pytest apps/exchange/tests/test_retrieval.py -q` |
| **T-051** | E5 | Pixel reports checkout outcomes the collector can join | 3 | 6 | — | T-050 | `npx vitest run pixel && pytest apps/merchant/svc/tests/test_collector.py -q` |
| **T-071** | E7 | Three questions or fewer produce a confirmed structured intent | 3 | 4 | — | T-014, T-070 | `pytest apps/buyer/svc/tests/test_intent.py -q && npx vitest run apps/buyer/app/intent` |
| **T-023** | E2 | Catalog MCP adapter passes recorded-contract tests | 3 | 1 | — | T-020 | `pytest services/ingest/tests/test_catalog_mcp.py -q` |
| **T-053** | E5 | A plain-language interview yields an approved, versioned envelope | 3 | 1 | — | T-014, T-050 | `pytest apps/merchant/svc/tests/test_onboarding.py -q` |
| **T-022** | E2 | Same products across stores link via entity resolution | 3 | 0 | — | T-012, T-020 | `pytest services/ingest/tests/test_entity_resolution.py -q` |
| **T-065** | E6 | A golden pitch yields all four verification statuses with evidence | 4 | 11 | YES | T-010, T-021, T-060, T-080 | `pytest packages/verification apps/trust/tests/test_verification.py -q` |
| **T-033** | E3 | Accepting an offer produces a validated code and permalink | 4 | 7 | — | T-030, T-052 | `pytest apps/exchange/tests/test_accept.py -q` |
| **T-042** | E4 | Store loop learns from its own outcomes only | 4 | 3 | — | T-041 | `pytest packages/store-agent/tests/test_learning.py -q` |
| **T-043** | E4 | Shadow mode logs would-be bids and trust events adjust the agent | 4 | 3 | — | T-041 | `pytest packages/store-agent/tests/test_shadow_trust.py -q` |
| **T-061** | E6 | Webhook truth reconciles lossy pixel signals | 4 | 2 | — | T-051, T-060 | `pytest apps/trust/tests/test_reconciliation.py -q` |
| **T-044** | E4 | External bids enter signed and route to verification, not rejection | 4 | 1 | — | T-041 | `pytest packages/store-agent/tests/test_external_bids.py -q` |
| **T-024** | E2 | Differential refresh keeps the graph current at field-appropriate cadence | 4 | 0 | — | T-021, T-023 | `pytest services/ingest/tests/test_refresh.py -q` |
| **T-062** | E6 | Trust merges verification and outcome observations against the approved manifest | 5 | 8 | YES | T-060, T-065, T-080 | `pytest apps/trust/tests/test_scoring.py -q` |
| **T-032** | E3 | Shortlists rank by the single published formula behind eligibility filters | 5 | 6 | YES | T-031, T-065 | `pytest apps/exchange/tests/test_ranking.py -q` |
| **T-081** | E8 | Simulated buyers exercise the whole network from one seed | 5 | 4 | YES | T-033, T-051, T-080 | `pytest services/sim -q` |
| **T-072** | E7 | Shortlists render with provenance and accept hands off cleanly | 5 | 3 | — | T-033, T-071 | `npx vitest run apps/buyer/app/shortlist && pytest apps/buyer/svc/tests/test_accept_flow.py -q` |
| **T-045** | E4 | Three seller personas exercise the external door adversarially | 5 | 0 | YES | T-014, T-044, T-080 | `pytest apps/seller-reference -q` |
| **T-064** | E6 | Exchange consumes one snapshot shape for trust and exploration | 6 | 4 | YES | T-062 | `pytest apps/trust/tests/test_snapshot.py -q` |
| **T-063** | E6 | Trust events reach the store agent and buyers close the loop | 6 | 2 | YES | T-043, T-062 | `pytest apps/trust/tests/test_feedback_push.py -q` |
| **T-035** | E3 | Loss reports aggregate reasons without leaking amounts | 6 | 1 | YES | T-032 | `pytest apps/exchange/tests/test_loss_reports.py -q` |
| **T-082** | E8 | One scripted run proves the full S1 flow | 6 | 1 | YES | T-061, T-072, T-081 | `pytest e2e/test_s1_flow.py -q` |
| **T-034** | E3 | Exchange bandit shifts exposure with outcomes and honors exploration | 7 | 3 | YES | T-032, T-064 | `pytest apps/exchange/tests/test_bandit.py -q` |
| **T-054** | E5 | The dashboard shows the walls and the window | 7 | 0 | YES | T-035, T-043, T-052, T-053, T-063 | `npx vitest run apps/merchant/app/dashboard` |
| **T-073** | E7 | Routed buyers can answer one structured feedback prompt | 7 | 0 | YES | T-063, T-072 | `npx vitest run apps/buyer/app/feedback && pytest apps/buyer/svc/tests/test_feedback.py -q` |
| **T-083** | E8 | Both learning loops demonstrably move under seeded outcomes | 8 | 2 | YES | T-034, T-042, T-081 | `pytest e2e/test_learning.py -q` |
| **T-084** | E8 | The dishonest store ends below threshold and off the shortlist | 9 | 1 | YES | T-062, T-083 | `pytest e2e/test_dishonest.py -q` |
| **T-085** | E8 | The demo is a runbook anyone on the team can execute | 10 | 0 | YES | T-082, T-084 | `pytest docs/tests/test_runbook.py -q` |

`depends_on` still names T-000 where the graph does; T-000 is satisfied on `main` at `705cf34`,
so those five edges are already discharged and the ledger keeps them only so the graph stays
readable against `tickets.json`.

## Initial frontier

T-000 is merged, so all five of its children are ready **now** and are mutually independent —
they can be dispatched in one batch subject to the Neo4j constraint below.

| ID | unblocks | why it is first |
|---|---|---|
| **T-010** | 26 | Highest leverage in the graph. Every contract-typed downstream ticket stalls without it. |
| **T-013** | 25 | Second by count, **first by consequence** — see below. |
| **T-011** | 24 | Owns the D5 grant migrations and (per D35) the import-lint proof. |
| **T-014** | 23 | LLM doubles; four depth-3 tickets wait on it. |
| **T-012** | 18 | The only frontier ticket that writes Neo4j — schedule it alone on the graph surface. |

**T-013 carries priority beyond its unblock count.** T-080 depends on T-013 and on nothing
else, and T-080 transitively gates **15 of 44** tickets behind a human approval. Until T-013
lands, the approval request cannot even be raised. Rank T-013 first among equals so the
manifest reaches Hank while he is awake; every hour T-080 waits is an hour 15 tickets wait.

## Human gate (T-080)

T-080 is depth 2 but gates **15** tickets: T-032, T-034, T-035, T-045, T-054, T-062, T-063,
T-064, T-065, T-073, T-081, T-082, T-083, T-084, T-085.
**28 tickets are buildable without approval.** Dispatch T-080 as early as T-013 allows.

## Scheduling constraints

Per-ticket ownership is `intake-report.md` §4 (narrowed) and constraint pairs are §5, with
the **flat** source layout of D42 overriding §3.1/§4's nested tree. Beyond those:

**Neo4j is ONE shared database (D4, narrowed by D38).** `CREATE DATABASE` is unsupported on
Community, so there is no per-worker graph. Two consequences the scheduler must honour:

- **Never co-schedule two graph-writing tickets.** The graph-writing set is
  T-012, T-020, T-021, T-022, T-023, T-024, T-031 — they serialize against each other.
- **Never co-schedule `e2e/` with a graph lane.** `e2e/` resets and writes the same shared
  graph. The session-scoped `flock` on `/tmp/proxyshop-neo4j.lock` (root `conftest.py`,
  `_neo4j_guard`) is the safety net, not the plan: it makes a collision slow rather than
  corrupt. Tickets in the `e2e/**` scope are T-082, T-083, T-084.
- Vector-index creation is `CREATE VECTOR INDEX … IF NOT EXISTS`, so a re-entrant session
  never errors. Do not "fix" that by dropping the index.

**Postgres isolates per-worker database.** Each worker gets `proxyshop_w<N>`
(`scripts/db_init.py`, `make db-init`) — verified working. Roles are **cluster-global**
(`pg_authid.relisshared = true`), so `CREATE ROLE` lives once in `db/init/00-roles.sql` and
GRANTs — which are per-database — live in `db/migrations` so every `proxyshop_w<N>` gets its
own copy. No Postgres serialization constraint applies between tickets.

**Redis isolates per-worker logical DB index** (16 available) plus a central `w{N}:` key
prefix in `proxyshop_support`. `FLUSHALL` is banned repo-wide and grepped for in
`make verify`; `FLUSHDB` is the permitted reset. `maxmemory-policy` is `noeviction`. No
Redis serialization constraint applies between tickets.

**`PROXYSHOP_WORKER` must be set for every dispatch.** The root `conftest.py` fails the
session if it is unset — deliberately, because a silent fallback is how every worker lands
in one database.

**T-080's scope is still `fixtures/**`,** which strictly contains six other tickets'
fixture directories: `fixtures/pages/**` (T-021), `fixtures/er/**` (T-022), `fixtures/mcp/**`
(T-023), `fixtures/envelopes/**` (T-040), `fixtures/interviews/**` (T-053),
`fixtures/dialogues/**` (T-071). T-080 is marked `parallel_safe: true` and that flag is
wrong at the file level. Intake §9-A5 proposed narrowing the glob; it was **not** applied, so
the orchestrator must enforce per-file ownership from §4 when any of those six run alongside
T-080.

**Bring the datastore stack up before any cycle metric sweep.** `make deps-up` first.
With the compose stack down, `make verify` is still green — 15 `@pytest.mark.docker` tests
skip cleanly — so `build_succeeds` reads 1 without ever exercising `pg_role`, the D5 grants,
the D37 flock or the Redis prefix. Measured at cycle-0 report time: 95 passed, 15 skipped.

## Carry-forward defects (found in cycle 0, not fixed)

Each was seen by an adversarial verifier during T-000 and consciously deferred. None blocks
dispatch; each has a named owner so it is fixed by the ticket that already touches the file.

- **CF-1 — stale docstring in the frozen datastore proof.** *(cosmetic, misleads agents)*
  `apps/exchange/tests/test_scaffold_datastores.py:9-12` still says "this directory's conftest
  overrides `_neo4j_guard` with the D37 flock". That override was deleted when the lock moved
  to the root `conftest.py` for **every** session; `apps/exchange/tests/conftest.py:17` now
  says the opposite. An agent reading the fixture proof to learn the locking model learns the
  wrong model. Owner: orchestrator (the file is T-000-frozen).

- **CF-2 — `.importlinter`'s `**` pattern has no regression guard.** *(latent gate break)*
  The D39 contract exempts a legal import with `ignore_imports = ** -> redis.exceptions`.
  `*` matches one segment only, which is the bug that broke four downstream tickets; `**`
  is the fix. But **no tracked file imports `redis.exceptions`** — the only occurrence in the
  repo is a comment at `scripts/check_verify_contracts.py:277` — so reverting `**` to `*`
  passes `make verify` today. The first ticket to write `except redis.exceptions.ConnectionError`
  inherits a break it did not cause. Owner: **T-011**, which already owns the import-lint
  proof under D35; add a module that legally imports `redis.exceptions` to the same fixture.

- **CF-3 — D5's wording is imprecise and `db/init/00-roles.sql` repeats it.** *(will fail T-011)*
  D5 and `db/init/00-roles.sql:11` both state the grant model is "schema-level USAGE only".
  `USAGE` on a schema grants **name resolution**, not row access. A T-011 agent that follows
  the text literally will grant `USAGE ON SCHEMA ledger` and then watch `exchange` fail every
  read with `permission denied for table ledger.<t>`. The migrations must **also** issue
  table-level `SELECT` (or `ALTER DEFAULT PRIVILEGES … GRANT SELECT`) on `ledger` and `app`,
  while still granting **nothing at all** on `sealed` and `vault` — the negative half of D5 is
  exact and is what C3/S7 actually turns on. Owner: **T-011** (owns `db/migrations`); the
  packet must carry this correction or D5 will be reproduced wrongly.

- **CF-4 — latent idle-in-transaction deadlock between `pg_role` and `pg_admin`.** *(latent hang)*
  `pg_admin` (`conftest.py:167-190`) is **autocommit**; `pg_role` (`conftest.py:194-236`)
  returns a connection in **default transactional mode**, cached per role for the whole test.
  A `pytest.raises(InsufficientPrivilege)` assertion — the canonical use of `pg_role` — leaves
  that connection idle-in-transaction holding an `ACCESS SHARE` lock. Any ticket that then
  issues `DROP SCHEMA`/`TRUNCATE`/`ALTER` through `pg_admin` in the same test blocks until
  `lock_timeout`, and reads as a hang, not as a lock conflict. Owner: **T-011** first, then
  any ticket combining both fixtures; the packet rule is *roll back or close the `pg_role`
  connection before touching DDL through `pg_admin`*.

Also noticed, not worth a ticket of their own:

- `.importlinter`'s header cites "(D34)" for the import-lint contract; the ruling is **D35**.
  Symmetrically, `intake-report.md` §9-B cites "D33" for the T-082 multiset; it is **D34**.
  Prose-only drift — fold into whichever ticket next edits each file.
- 18 empty `<name> 2` sibling directories exist beside real scope roots (`apps/exchange 2`,
  `db/init 2`, `packages/llm 2`, …) from a Finder/iCloud duplication. They are empty and
  untracked so git ignores them and no scope glob matches them; delete on the next cleanup.

## Applied plan edits (cycle 0)

Recorded here because the ledger, not `tickets.json`'s diff, is what the scheduler reads.

- **T-073.scope += `apps/buyer/svc/src/feedback/**`** (intake §9-A7, rendered in D42's flat
  form, not the report's nested `src/buyer_svc/` form). Its verify tested service code the
  ticket was not allowed to write.
- **T-082 acceptance 1 → D34's exact per-kind multiset.** The old "each kind exactly once"
  is unsatisfiable against T-082's own acceptance 2, so the ticket could never close.
  Approved by Hank specifically.

Graph re-validated after both edits: **44 tickets, 80 edges, acyclic, 133 criteria.**
The rest of intake §9-A/B was declined for now — see `reports/cycle-0.md`.
