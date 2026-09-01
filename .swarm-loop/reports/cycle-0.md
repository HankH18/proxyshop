# Cycle 0 — baseline epoch

**Head:** `705cf34` (`merge(T-000): monorepo scaffold — flat layout, real gates, per-worker isolation`)
**Tracked files at that head:** 232 · **now:** 245 (the freeze added the nine acceptance test
modules, `manifest.json`, `freeze-log.jsonl` and this cycle's analysis)
**Codebase hash:** `78458198bdc1f587c3c1e6c235a62c6f173ef100b7cad06c1b1cb9b4f4cfdb36`
**Targets met:** 2 / 12 · **Tickets closed:** 1 · **Tickets minted:** 1 (`T-086`, post-checkpoint — §6)
**Ticket graph:** 44 open of 45 (was 43 of 44 before §9-A was approved)

---

## 1. What was built

**T-000, the monorepo scaffold** — and, alongside it, the frozen measuring apparatus the
next 102 cycles are scored against.

The scaffold: a flat-`src` monorepo (D42) across 13 Python members and 4 npm workspaces
(`packages/contracts`, `apps/buyer`, `apps/merchant`, `pixel`), the root `pyproject.toml` as
the repo's **only** pytest configuration (D36), vitest + tsc + eslint over the workspaces,
a standalone `.importlinter` carrying the C3/S7 and D39 contracts,
`docker-compose.yml` for Postgres/Neo4j/Redis on verified-free ports, `db/init/00-roles.sql`
creating the four cluster-global least-privilege roles, and a 20 KB root `conftest.py`
holding the shared fixture layer roughly forty tickets stand on — `pg_admin`, `pg_role`,
`redis_client`, `neo4j_session`, the `_neo4j_guard` D37 flock and the `PROXYSHOP_WORKER`
session assertion.

The apparatus: **12 metrics over a 103-test acceptance suite, 16 files hashed** into
`manifest.json` at `2026-09-01T09:48:33`, with `freeze-log.jsonl` recording amendment 0.

### The verification ladder, and what it cost

T-000 took **three adversarial rounds** and produced **eight real defects**. Every one was
found by an agent that had not written the code; **the builder found none of them.** That
ratio is the single most important number in this report, and it is why the ladder is not
optional in later cycles.

| # | defect | why no per-ticket gate would have caught it | fix |
|---|---|---|---|
| 1 | `make verify` **self-deadlocked for 600 s** once two lanes each took a session-scoped `fcntl.flock` in one process | exists only in the *pair*; every single ticket's verify was green. Mid-run it detonates looking like a hang | `429f29e` |
| 2 | **Redis per-worker isolation did not exist** — redis-py's `from_url` lets the URL's `db` component beat the explicit kwarg, and `.env.example` shipped `/1`, so every worker shared DB 1 and each fixture `flushdb()` wiped its siblings | the wrapper read correctly; only an independently-written client exposed it | `17c1527` |
| 3 | **Every Postgres test silently skipped** — nothing created `proxyshop_w<N>` | a skip is green | `429f29e` |
| 4 | **`uv sync --frozen` ignored a changed manifest and exited 0**, making a dependency addition a silent no-op | the command succeeded; only the effect was missing | `09fb42e` |
| 5 | `.importlinter`'s `* -> redis.exceptions` matched **one segment only**, so a legal import broke the gate for four downstream tickets | the gate was green precisely because nothing legal had been written yet | `80431f0` |
| 6 | `e2e/` wrote the shared Neo4j graph with **no lock and no reset**; and `neo4j_driver` being session-scoped meant the **first requesting directory's guard won for the whole session** | cross-directory, cross-lane; invisible to any one lane | `c37b417` |
| 7 | A **duplicate fixture name killed an entire test directory** at conftest-import time — including already-merged tickets' tests | shared-conftest blast radius; the touched directory alone still collected | `1964c9d` |
| 8 | **`pg_role` was exercised by nothing** — the least-privilege factory ~10 tickets depend on, carrying the D5/C3/S7 grant proof | no test existed to fail | `94bbffa` |

## 2. Metric baseline

Twelve metrics, all `maximize`, all `tolerance: 0`. Measured `2026-09-01T09:48:51`.

| metric | baseline | target | error | verdict |
|---|---|---|---|---|
| `acceptance_pass_rate` | **3.88** | 100 | 96.12 | insufficient_data |
| `acceptance_collected` | **103** | 103 | 0 | **at_target** |
| `build_succeeds` | **1** | 1 | 0 | **at_target** |
| `spec_criteria_passing` | **4** | 8 | 4 | insufficient_data |
| `e1_foundation_passing` | 0 | 10 | 10 | insufficient_data |
| `e2_ingestion_passing` | 0 | 8 | 8 | insufficient_data |
| `e3_exchange_passing` | 0 | 18 | 18 | insufficient_data |
| `e4_store_agent_passing` | 0 | 14 | 14 | insufficient_data |
| `e5_merchant_passing` | 0 | 10 | 10 | insufficient_data |
| `e6_trust_passing` | 0 | 20 | 20 | insufficient_data |
| `e7_buyer_passing` | 0 | 9 | 9 | insufficient_data |
| `e8_proofs_passing` | 0 | 6 | 6 | insufficient_data |

Suite composition, re-measured at report time: **103 collected — 4 passed, 99 failed,
0 skipped, 0 collection errors.** Every failure is a genuine unmet goal (`ImportError:
cannot import name 'rank' from 'apps.exchange.src.ranking'` and its siblings), which is
exactly the shape the lazily-importing design was built to produce: an unbuilt feature reads
as a test failure, never as a broken measuring stick.

`insufficient_data` on ten metrics is correct and expected — regression needs more than one
point. Slopes become meaningful from cycle 2.

## 3. Tickets closed

| ID | title | resolution |
|---|---|---|
| **T-000** | Monorepo skeleton with green empty verify pipeline | **CLOSED**, merged to `main` at `705cf34`. Three adversarial rounds, eight defects found and fixed (§1). Closed against **D1 and the §3.1 tree**, not against its own acceptance 3, which still reads `packages/protocol` — a stale string D1 already overruled and which `test_monorepo_layout_matches_design_and_has_no_packages_protocol` now enforces the opposite of. |

## 4. Tickets minted

**One — `T-086`, and not from the verification ladder.**

No defect from the T-000 ladder required a new ticket: every one was fixable inside T-000's
own scope, and the four deliberately deferred are carried as backlog entries (CF-1…CF-4)
with existing tickets as owners rather than as new work.

`T-086` was minted separately, **after** the cycle-0 checkpoint, when Hank approved intake
§9-A (§6). It closes a coverage gap rather than a defect: SPEC **S6** — the
interview → approved envelope → shadow-mode bid log → activation seam — was owned by
nobody. `T-053` and `T-043` each build one side of it, ref neither S6 nor each other, and
live in different packages, so nothing asserted the join.

**It carries no frozen acceptance coverage, and that is not fixable.** The suite was frozen
at 103 tests mapped to the tickets that existed at freeze time; `T-086` appears **zero**
times under `.swarm-loop/acceptance/`. The only S6 mention in the whole frozen suite is an
incidental docstring on a test marked **T-085** — S6's *manual demo* half. So `T-086` is
measured by its own `verify` command alone and moves **no** frozen metric, not even
`spec_criteria_passing`. A green `T-086` will be real work that shows up in no number;
read it from the ticket ledger, never from the metric table.

## 5. The two at-target metrics — why each is legitimate, not vacuous

Both metrics that read "done" at cycle 0 are at target *by construction*, so each needs its
own evidence that the construction is honest.

### `acceptance_collected = 103 / 103`

**Legitimate.** The count is at target because the frozen suite was written to cover the
whole goal set before freezing, not because collection was tuned to a number. The evidence
is the per-epic breakdown, which matches every per-epic target **exactly**:

| epic | collected | that epic's target | |
|---|---|---|---|
| E1 | 10 | 10 | ✓ |
| E2 | 8 | 8 | ✓ |
| E3 | 18 | 18 | ✓ |
| E4 | 14 | 14 | ✓ |
| E5 | 10 | 10 | ✓ |
| E6 | 20 | 20 | ✓ |
| E7 | 9 | 9 | ✓ |
| E8 | 6 | 6 | ✓ |
| SPEC | 8 | 8 | ✓ |
| **total** | **103** | **103** | ✓ |

A suite padded to hit a total would not partition correctly nine ways. The metric's real job
is **anti-deletion**: with `tolerance: 0` and `direction: maximize`, any worker who deletes,
renames or `xfail`s a frozen test drops `acceptance_collected` below 103 and the cycle
registers a regression. It is a tripwire that happens to start armed, not an achievement.

Two structural properties back it: a filter selecting zero tests is **empty stdout + exit 1**,
never a `0` (D43), so a broken selector cannot be read as a legitimate score; and every
acceptance test is marked with its epic and ticket — asserted by a suite test that passes
today (`test_every_acceptance_test_is_marked_with_epic_and_ticket`), so the partition itself
is self-checking.

### `build_succeeds = 1 / 1`

**Legitimate as a gate reading, and narrower than it looks.**

*Legitimate:* the metric was **sabotage-tested in both directions** before the baseline was
recorded. An unused import drove it to 0; removing the import drove it back to 1. A binary
gate that has never been observed failing is indistinguishable from a gate that cannot fail,
and this one has been observed failing on purpose. What it actually executes, re-measured at
report time in **8 s**: ruff format + lint, mypy (`Success: no issues found in 74 source
files`), `lint-imports` (`Analyzed 169 files, 518 dependencies. Contracts: 2 kept, 0 broken`),
eslint + tsc, `npx vitest run` (4 files, 7 tests), the whole pytest tree, and the six
`check_verify_contracts.py` guards (pytest-config, test-path-filter, schema-package,
non-empty-test-dir, raw-Redis-client, unique-fixture-name). That is a real chain, and D36's
"only one pytest configuration" guard plus the non-empty-test-dir guard exist specifically to
stop the vacuous-green failure mode where a directory collects nothing and reports success.

*This was nearly recorded far narrower than it looks, and the catch is worth keeping.* The
first cycle-0 sweep ran with the compose stack **down**: the pytest leg was **95 passed, 15
skipped**, and all 15 skips were the `@pytest.mark.docker` datastore tests — precisely the
layer the gate appears to prove, and precisely where four of the eight T-000 defects lived
(`pg_role` and the D5 grants #8, the D37 Neo4j flock #6, the per-worker Redis index #2, the
per-worker Postgres database #3). `build_succeeds` reads **1** either way, so nothing in the
number itself would have revealed it. The skip is loud (`compose datastore stack is not
reachable … Run 'make deps-up' first`), which is right for `make check` and wrong as the
condition under which to freeze a baseline.

**The baseline in the table above was therefore re-measured with `make deps-up` first, and
is the stack-up reading: 110 passed, 0 skipped.** `measure --cycle 0` was re-run; `history.csv`
is append-only and `analyze` keeps the last row per cycle in file order, so the stack-up
reading is the one that anchors the regression.

**Standing consequence: `make deps-up` before every metric sweep, and record `passed /
skipped` next to every `build_succeeds` reading.** A binary gate cannot tell you what it
skipped; only the skip count can.

### Not at target, but worth a note: `spec_criteria_passing = 4 / 8`

The four that pass are three T-000 structural criteria — no module-scope product imports in
the acceptance suite, every acceptance test epic/ticket-marked, monorepo layout matches
DESIGN with no `packages/protocol` — plus one T-011 criterion,
`test_exchange_source_never_imports_envelope_or_sealed_surfaces`. That fourth one passes
**vacuously today**: there is no exchange source yet to violate it. It is a real criterion and
it must stay green as T-030/T-032/T-033 write `apps/exchange/src/**`, but it should not be
read as evidence that C3/S7 is enforced in built code. The three S8 release blockers
(S8-1 blacklisted seller never eligible, S8-2 contradicted hard constraint never wins,
S8-3 off-domain checkout URL refused) all fail, as they should.

## 6. Plan divergences

### Applied this epoch

| # | change | why |
|---|---|---|
| §9-A7 | **`T-073.scope += apps/buyer/svc/src/feedback/**`** | T-073's own verify (`pytest apps/buyer/svc/tests/test_feedback.py`) tested service code the ticket was not allowed to write. Rendered in D42's **flat** form, not the intake report's nested `src/buyer_svc/feedback/**` — the report's tree is superseded on layout. |
| §9-B | **`T-082` acceptance 1 → D34's exact per-kind multiset** over the complete `LedgerEvent` enum | The old "each kind appears exactly once" is **unsatisfiable against T-082's own acceptance 2** (fallback + hosted bids ⇒ ≥2 `bid_placed`, ≥2 `shown`), so the ticket could never close no matter what was built. **Hank approved this one specifically.** |

Graph re-validated after both edits: **44 tickets, 80 edges, acyclic, 133 criteria** — the
edit count is unchanged because both are scope/text edits, not edges.

Applied in a previous epoch and standing (D11, `722733e`): §9-A1 — `T-065.depends_on += T-080`
with `fixtures/golden/**` removed from T-065's scope, so T-065 no longer authors the golden set
it is graded against. That edit is what moved the gated set to 15 and buildable-meanwhile to 28.

### Applied after this checkpoint — intake §9-A at `e196ef5`

**Hank approved §9-A after this report's checkpoint was taken, and it was applied.** Recorded
here rather than deferred to cycle 1's report because it reverses six rows of the *Declined for
now* table immediately below, and a divergence log that says "declined" while the graph says
otherwise is worse than no log. Seven edits:

| # | edit | why |
|---|---|---|
| §9-A2 | **`T-082.depends_on += T-032, T-041, T-062`** | Its acceptance asserts hosted bids, blacklist eligibility and the full `LedgerEvent` set; its old parents (T-061, T-072, T-081) guaranteed none of the three, so it could be scheduled ready with its assertions unreachable. Depth unchanged at 6. |
| §9-A3 | **`T-050.depends_on += T-010`** | The merchant app emits contract-typed `LedgerEvent`s without depending on the package that defines them. This is decision 8 of §8 below, discharged — the scheduler no longer carries the edge by hand. |
| §9-A4 | **`T-083.depends_on += T-061`** | D31's outcome producer was not a declared dependency of its consumer. Depth unchanged at 8. |
| §9-A5 | **`T-080.scope` narrowed off `fixtures/**`** to `fixtures/{manifest,approval,seed,catalog,personas,golden,tests}/**` | Closes residual risk 4 outright: the seven paths are disjoint from all six other tickets' fixture directories, so `parallel_safe: true` is now truthful for T-080. |
| §9-A6 | **`T-053.scope += apps/merchant/svc/src/envelope/**`** (D33, flat per D42) | Same class as the A7 edit above: T-053 needed to write envelope code its scope excluded. |
| §9-A8 | **new ticket T-086**, "Onboarding drives a shadow store to its first real bid" | SPEC S6's machine-checkable half, previously owned by nobody — T-053 and T-043 each cover one side of the interview → envelope → shadow → activation seam, ref neither S6 nor each other, and live in different packages. Depth 5, deps T-043 + T-053, 4 criteria, `parallel_safe: false`, scope `e2e/test_onboarding.py` + `e2e/support/onboarding/**` (with a `.gitkeep` so the support directory materialises in a worktree). This reverses §4's "not minted this epoch". |
| — | **`T-032.refs += DESIGN#interfaces-contracts-between-tickets`** | Applied *instead of* the proposed `T-064 → T-032` edge, which was **deliberately not applied**: it would serialize the whole exchange lane behind the human gate for no verification gain. EXECUTION rule 1 restricts a worker to the sections its ticket refs, so the ref is what actually lets T-032 load the contract shapes it must honour. |

Graph re-validated after all seven: **45 tickets, 87 edges, acyclic, single root T-000, max
depth still 10, T-086 at depth 5, T-080 still gating 15** — and buildable-meanwhile moves 28 → 29,
T-086 being the twenty-ninth. No depth moved anywhere in the graph; seventeen open tickets'
unblock counts rose, T-010's from 26 to 32. Full re-derivation and the retired `fixtures/**`
constraint are in `backlog.md`.

**None of this touches a frozen target, which is why it was safe to apply post-freeze.** Every
metric in `goals.json` measures by *executing the frozen suite* — `run.py --pass-rate`,
`run.py --total`, `run.py --count-passing --epic <E1…E8|SPEC>` — plus `make verify` for
`build_succeeds`. **Not one metric command reads `tickets.json`.** Targets were set from the
frozen suite's own per-epic composition, never from ticket criterion counts, so adding a ticket,
adding an edge or rewriting a scope glob cannot move a baseline, a target, or a reading. The
sixteen hashed files are untouched and `freeze-log.jsonl` stays at amendment 0.

The corollary is the cost: **the criteria total moved 133 → 137** (T-086's four), and **that
number is not frozen and never was** — it is a property of the plan, reported for orientation.
It appears in no metric and in no `history.csv` column. Symmetrically, **T-086 carries no frozen
acceptance coverage at all**: the suite was frozen at 103 tests mapped to the tickets that
existed then, `T-086` appears zero times under `.swarm-loop/acceptance/`, and the only S6
reference in the frozen suite is an incidental docstring in a test marked T-085 (the manual-demo
half of S6). T-086 is therefore measured by its own verify command alone and moves no frozen
metric. A reader must not read a green T-086 as progress on any number in this report.

### Declined at this checkpoint — six rows since reversed

Hank declined the remainder of intake §9-A/B *at the time of this checkpoint*. **§9-A2, A3, A4,
A5, A6 and A8 were subsequently approved and applied at `e196ef5`** — see the section directly
above; their rows below are superseded and the consequences they describe no longer stand. The
§9-B row is still declined and its consequence is live. Kept in full so the reasoning is not
silently re-raised:

| # | status | proposal | consequence of declining |
|---|---|---|---|
| §9-A2 | ~~declined~~ **APPLIED `e196ef5`** | `T-082.depends_on += T-032, T-041, T-062` | T-082's acceptance asserts hosted bids, blacklist eligibility and the full `LedgerEvent` set; its current deps (T-061, T-072, T-081) do not guarantee any of the three. T-082 can be scheduled ready while its assertions are unreachable. |
| §9-A3 | ~~declined~~ **APPLIED `e196ef5`** | `T-050.depends_on += T-010` | The merchant app emits contract-typed `LedgerEvent`s but does not depend on the package that defines them. Free to add (T-010 is depth 1); until then the scheduler must not start T-050 before T-010 merges. |
| §9-A4 | ~~declined~~ **APPLIED `e196ef5`** | `T-083.depends_on += T-061` | D31's outcome producer is not a declared dependency of the ticket that consumes it. |
| §9-A5 | ~~declined~~ **APPLIED `e196ef5`** | narrow `T-080.scope` from `fixtures/**` | **Live hazard.** `fixtures/**` strictly contains six other tickets' fixture directories (T-021, T-022, T-023, T-040, T-053, T-071) while T-080 is marked `parallel_safe: true`. Ownership must be enforced per-file from §4. |
| §9-A6 | ~~declined~~ **APPLIED `e196ef5`** | `T-053.scope += .../envelope/**` | Same class as A7, unfixed: T-053 may need to write envelope code its scope excludes. |
| §9-A8 | ~~declined~~ **APPLIED `e196ef5`** | mint **T-086** for S6's integration half | S6's integration coverage gap stays open; no ticket owns it. |
| §9-B (rest) | **still declined** | acceptance-text edits to T-000, T-060, T-054, T-062, T-011, T-012, T-013, T-085 | Each ticket's text keeps a known imprecision. The two consequential ones: **T-011** does not carry the import-lint criterion (D35) or the second-database migration criterion (D39), and **T-054** acceptance 2 asserts a cross-service behaviour its vitest-only verify cannot check. |

Not a divergence, but decided this epoch and settled: **report-reuse is removed from the
frozen runner permanently (D43).** `--write-report`/`--from-report` were built for intake
blocker B2, and two verifiers then drove both the pass-rate metric and its companion count to
target **from a one-line, worker-written JSON file** while three of four frozen tests still
failed and `verify` reported the harness intact throughout. Hashing the frozen directory into
the report does not close it — workers can read that directory and compute any hash they must
match. Measured cost of doing it honestly: the eleven acceptance-runner invocations take
**9.2 s** and `make verify` takes **8 s**, so the full twelve-metric sweep is ~17 s. The
shortcut bought nothing. Do not re-propose it.

## 7. Residual risks

1. **Harness hermeticity is PARTIAL, never sealed.** Three metric exploits were found and
   closed in the frozen runner — `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` (no third-party plugin
   can hook the scorer), `-o pythonpath=` (no worker-controlled path injection), and pinned
   discovery patterns (no renamed file sneaks in or out). The **fourth is open and structural**:
   product code imported by the suite runs in the scorer's own process and can tamper with its
   in-process state. This is inherent to any in-process black-box suite and cannot be hardened
   away — only moved (subprocess-per-test) at a cost not paid this epoch. **Report the harness
   as partially hermetic in every cycle report.** A harness described as sealed stops being
   audited at exactly the seam that is still open.

2. **The git-guard is a guard rail against accidents, not a sandbox.** It tokenizes the Bash
   `command` string with `shlex`, follows shell keywords, wrappers, aliases, `bash -c` and
   literal `eval` payloads, and blocks `stash`/`reset --hard`/`checkout -- <path>`/`clean -f`
   and their plumbing equivalents by git's own abbreviation rule. Documented blind spots, all
   still open:
   - **heredoc bodies piped to a shell** (`bash <<EOF … EOF`) — heredoc bodies are treated as
     data everywhere **by design**, because the loop's own dispatch writes task packets by
     heredoc whose text quotes `git stash`; false-blocking that would wedge the run;
   - **payloads that are only strings at run time** — `eval "$cmd"` (the literal
     `eval 'git stash'` *is* caught, the variable's contents are not),
     `python3 -c 'os.system("git stash")'`, and **a destructive op inside a script file**,
     which the guard never opens;
   - a program named by a variable that does not mention git (`$X stash` where `X=git`);
   - anything after a `cd` the guard cannot follow — the ref-vs-path question for
     `git checkout <operand>` is answered from the *hook's* cwd, not the command's.

   **The real protection is per-agent worktree isolation**, and it must not be relaxed on the
   strength of the guard.

3. **`build_succeeds` can read 1 with the datastore layer entirely unexercised** (§5). Fifteen
   docker-marked tests skip when compose is down, including every proof of the isolation model
   that four of the eight T-000 defects were about.

4. **T-080 is `parallel_safe: true` with scope `fixtures/**`**, overlapping six other tickets
   (§6, A5). The flag is wrong at file granularity and the scheduler must not trust it.

5. **Four known defects carried unfixed** — CF-1 (stale docstring in the frozen datastore
   proof), CF-2 (`.importlinter`'s `**` carve-out has no regression guard), CF-3 (D5's wording
   will make T-011 grant `USAGE` without `SELECT`), CF-4 (idle-in-transaction deadlock between
   `pg_role` and `pg_admin`). Full text, evidence and owners in `backlog.md`. **CF-3 and CF-4
   both land on T-011 and must be in its task packet**, or T-011 reproduces the wrong grant
   model and hits a lock hang while debugging it.

6. **Ten of twelve metrics are `insufficient_data`.** Regression steering is not available
   until cycle 2. Cycle 1's dispatch order is a judgment call from the graph, not from the
   evaluator, and should be treated as such.

## 8. Decisions for the next epoch

1. **Dispatch the five-way frontier as one batch: T-010, T-013, T-011, T-014, T-012** — all
   ready, all mutually independent. **Rank T-013 first among equals.** It is second by unblock
   count (25 vs T-010's 26) but it is T-080's *only* parent, and T-080 gates 15 of 44 behind a
   human approval that cannot even be requested until T-013 merges.
2. **Dispatch T-080 the moment T-013 merges**, ahead of anything else newly ready, so the
   manifest approval reaches Hank while he is awake. Twenty-eight tickets remain buildable
   meanwhile, so the gate need not idle the run.
3. **One graph writer at a time.** T-012 is the only frontier ticket that writes Neo4j; give
   it the graph surface alone, and never co-schedule `e2e/` with a graph lane. The
   `/tmp/proxyshop-neo4j.lock` flock is a safety net that makes a collision slow, not a plan
   that makes one safe.
4. **`make deps-up` before every metric sweep**, and record `passed / skipped` alongside every
   `build_succeeds` reading. A 1 with 15 skips is a different fact from a 1 with 0 skips.
5. **Put CF-2, CF-3 and CF-4 in T-011's task packet verbatim.** T-011 already owns
   `db/migrations` and the D35 import-lint proof; these are corrections to work it is about to
   do, not new work.
6. **Keep the verification ladder at full strength, with the mandate widened.** Cycle 0's
   record is 8 defects, all found by non-builders, none by the builder. Two specific
   requirements carried forward: every verifier writes its **own** reference implementation and
   its **own** negative control rather than re-running the fixer's artifacts; and the
   integration gate gets an explicit **two-lanes-in-one-process** step, because the 600 s
   `make verify` self-deadlock was invisible to every per-ticket gate by construction.
7. **The frozen harness stays frozen.** Sixteen hashed files, amendment 0. Any change goes
   through `freeze-log.jsonl` as a recorded amendment with a reason, never as an edit. Report
   reuse is settled (D43) and is not to be re-proposed.
8. **Re-raise §9-A3 (`T-050 → T-010`) at the next approval checkpoint.** It is free — T-010 is
   depth 1 and the critical path is unchanged — and until it is applied the scheduler is
   carrying by hand an edge the graph should carry itself.
