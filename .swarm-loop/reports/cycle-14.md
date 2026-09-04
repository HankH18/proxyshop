# Cycle 14 — the seven packages that were never written, E3 and E4 to target, and one red that holds the push

HEAD at measurement `b255f31` · acceptance **103/120 (85.83%)**, from 76/120 (63.33%)
· 6/12 metrics at target · harness hermeticity **PARTIAL** (17 frozen files, amendment 9)
· codebase stall 0/3 · goal-progress stall 0/3.

The flat stretch broke. Fifty-four commits, `7279452..b255f31`, and the seven greenfield packages
diagnosed in cycle 13 were all written and merged. **Acceptance went 76 → 103 of 120**, the largest
single-epoch move of the run; **E3 reached 21/21 and E4 reached 20/20, both at target for the first
time.** Tracked files 580 → 615.

The epoch does not close clean. **`build_succeeds` is 0** — `make verify` fails on eight mypy errors
from modules that landed here — so the push gate is held and nothing left the machine. That failure
was caused by how the worktrees were provisioned, not by any lane's judgement.

## ESC-011 is OPEN and wants a ruling — amendment 10

Non-blocking by its own terms (a `verify` gate is a **merge** condition, not a build one), so lanes
build while it is pending; the merges stop until it is decided.

**The same defect amendment 9 just repaired, on ten more tickets.** Amendment 9 established that
`red-check` builds a *throwaway worktree with no `.venv`*, where a bare `pytest` resolves to
Anaconda's pytest 6.2.4 while `pyproject.toml:70` sets `minversion = 8.2`; pytest aborts (exit 4)
before selecting anything, the gate stamps WEAK, and `check-branch --ticket` reads that stamp as a
merge veto. **All nine epic gates — T-022, T-023, T-052, T-053, T-071, T-072, T-073, T-081, T-085 —
are spelled with exactly the bare form amendment 9 fixed.** Two carry a second, independent fault:
**T-072 and T-073 put the `npx vitest` half FIRST in an `&&` chain**, so with no `node_modules` the
npx half fails and the pytest half — the only part that can select tests — never runs at all.

These nine are about to matter: **all 17 remaining acceptance failures across E2/E5/E7/E8 are
ImportError or ModuleNotFoundError** — nine more packages never written
(`services/ingest/src/{er,adapters}`, `apps/merchant/svc/src/{codes,onboarding}`,
`apps/buyer/svc/src/{intent,accept,feedback}`, `services/sim/src/dishonest`, `docs/demo/`), identical
to what cycle 14 found in E3/E4. Separately **T-228** — minted today from the eight mypy errors, and
the ticket that unblocks the push — carries the `findings` placeholder verify that `red-check`
refuses by design.

**Proposed change (`tickets.json` verify fields only, ten tickets, exactly as amendments 8 and 9
were):** prefix with `uv run python -m ` on T-022, T-023, T-052, T-053, T-081, T-085 and on the
pytest half of T-071; **additionally reorder the `&&` chain on T-072 and T-073** so the pytest half
runs first; replace T-228's placeholder with `uv run python -m mypy`. No metric, target, acceptance
test, product file or dependency changes. Full strings and the grant/decline commands are in
`.swarm-loop/ESCALATIONS.md`.

## What merged

Eight ticket merges plus the epoch merge `0240fcd`: **T-032** `7db7257` (the published ranking —
filters, one formula, one shortlist), **T-034** `3873310` (contextual Thompson sampler over cluster ×
store, floor before blacklist), **T-035** `07082c9` (loss reports that count reasons and carry no
amounts, R9), **T-042** `6fac92c` (the learning loop — pitches pool, discounts do not), **T-043**
`6c6e4eb` (shadow mode, activation, trust intake), **T-044** `3f4d676` (the signed external door),
**T-045** `487ee54` (personas, source spans, concurrent-answer criterion), **T-215** `38a034d` +
`443e17a` (the sanitiser itself was the leak; now total and fails closed). Plus **amendment 8** at
`27ace6f` and **amendment 9** at `75815dc` (post-measurement).

## Metrics

Quoted from `.swarm-loop/analysis/cycle-14.md`. Measured 2026-09-03T15:32:04–15:32:16 at `b255f31`.

| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |
|---|---|---|---|---|---|---|---|
| build_succeeds | regressing | cycle 14 | 0 | 1 | 1 | 0.0178571 | 1.82976 |
| acceptance_pass_rate | converging_on_track | cycle 14 | 85.83 | 100 | 14.17 | -5.45839 | 0 |
| e7_buyer_passing | converging_on_track | cycle 14 | 2 | 9 | 7 | -0.2 | 0 |
| e5_merchant_passing | converging_on_track | cycle 14 | 4 | 10 | 6 | -0.392857 | 0 |
| e2_ingestion_passing | converging_on_track | cycle 14 | 6 | 8 | 2 | -0.589286 | 0 |
| e8_proofs_passing | converging_on_track | cycle 14 | 6 | 8 | 2 | -0.564286 | 0 |
| acceptance_collected | at_target | cycle 14 | 120 | 120 | 0 | – | – |
| spec_criteria_passing | at_target | cycle 14 | 8 | 8 | 0 | – | – |
| e1_foundation_passing | at_target | cycle 14 | 10 | 10 | 0 | – | – |
| e3_exchange_passing | at_target | cycle 14 | 21 | 21 | 0 | – | – |
| e4_store_agent_passing | at_target | cycle 14 | 20 | 20 | 0 | – | – |
| e6_trust_passing | at_target | cycle 14 | 26 | 26 | 0 | – | – |

**Six at target**, up from five. Every metric other than E3, E4 and the top-line rate is
byte-identical to cycle 13 — this epoch moved exactly the two epics it dispatched.

**The goalposts have moved nine times**, and the analysis carries a banner saying so: *"this trend is
not against a single fixed target."* Amendments 7, 8 and 9 all landed this epoch — all user-approved,
all `tickets.json` scope/dependency or verify-field edits, none touching a metric, target, acceptance
test, product file or dependency. A slope fitted across fifteen points is fitted across a moving
target: steering signal, not measurement. The analysis also **recomputed the stored `error` column**
for `acceptance_collected`, `e3`, `e4`, `e6` and `e8`, where history disagreed with the target now in
force; every error above is recomputed from `value`, never read from history.

## `build_succeeds` is 0, and the cause was orchestration

`make verify` fails the types gate with **eight mypy errors in three store-agent modules merged this
epoch**: `modes/runner.py:317`, `external/door.py:271` and `:328`, `learning/state.py:216-217`. All
eight are unsound annotations over correct runtime behaviour — the code enforces the invariant, the
type system does not state it — but the gate does not grade intent.

**The three lanes that introduced them could not have seen them.** The cycle-14 worktrees were
provisioned with `uv sync` only. **This repo has TWO lockfiles — `uv.lock` and
`package-lock.json`** — and with no `node_modules`, `make verify` aborts *before it ever reaches
mypy*. A lane running the full gate there gets what looks like a JS toolchain failure and learns
nothing about its own types. **The one lane that ran `npm ci` itself did pass the full gate.** That
is the control.

Second incident from the same provisioning gap — the first nearly landed unformatted files. It is an
orchestration defect, fixed at the orchestrator, not in any lane's packet: **worktree provisioning
must satisfy every lockfile in the repo, not the one the orchestrator happened to think of.** Repair
is ticketed as T-228, gated on ESC-011.

## Push gate: NOT PUSHED — deliberately

Both remote tips sit at `e13524c`. Unpushed: **39 commits at the measurement record `b255f31`, 41 at
HEAD `75815dc`.** `push_remote` is `when-clean`, and the operator condition — *this epoch's
integrations landed green on `main`* — is **false while `build_succeeds` is 0**. **This is a hold,
not drift**; the epoch pushes the moment T-228 lands and `make verify` goes green.

## Seven lanes, seven real defects the frozen tests could not see

The most transferable result of the epoch: **all seven cycle-14 lanes found real defects their own
frozen acceptance tests were blind to. Seven for seven.** A sample:

- **T-032** — a NaN-driven inconsistent sort comparator, so **input order decided output order**. The
  frozen ranking tests never fed a NaN.
- **T-034** — an exploration floor that handed established stores exactly `0.0`, **the value reserved
  for *banned***. Correct-looking arithmetic landing on a sentinel.
- **T-044** — an envelope check that returned `active` when **no activation was declared at all**, so
  a store whose envelope simply forgot a key was fully green while submitting live bids.

The frozen suite is the *goal*, and 27 previously-red tests went green here. It is not the
*verification*. Every one of these came from a lane sabotaging or fuzzing its own core; none from
re-running a passing test.

## The 13-cycle flat stretch: root cause, closed

**The frontier ranked by `unblocks` count and had never been given a closure source.** It read
`0 closed` on every invocation — a broken instrument reading, not a scheduling opinion — so it
degenerated to the graph's roots and re-ranked already-built scaffold first, forever. Downstream:
**all 27 red tests in E3 and E4 were ImportError**, seven whole packages had never been written,
every owning ticket was open with **every dependency closed**, and `dispatch.jsonl` held **zero
records naming four of them across all 114 entries**. Amendment 7 (ESC-009) removed the mechanical
blocker — coarse `scope` globs that made `check-wave` veto six independent builds — and all seven
dispatched in a single wave.

## `pending_remeasure: [7]` — deliberately NOT cleared

An amendment moved a target and cycle 7's baseline was never re-measured against it, so
`all_targets_met` is withheld. **Left open on purpose.** `measure --cycle 7` run today would file
**today's values under cycle 7** and corrupt the trajectory every verdict here is fitted against — a
fabricated history point traded for a flag that only gates a condition which is far off regardless.
The correct resolution is to re-measure from a checkout of cycle 7's HEAD, or to accept the flag
standing for the rest of the run.

## Tickets

**Closed by merge (8):** T-032, T-034, T-035, T-042, T-043, T-044, T-045, T-215.

**Minted (4)** — graph 172 → 176:

- **T-228** — the eight mypy errors above. Blocks the push. Placeholder verify; see ESC-011.
- **T-225** — **a test that asserts nothing and reports green, in three copies.**
  `test_scope_directories_exist` iterates `for relative in []:` in three `test_scaffold_smoke.py`
  files. Zero iterations, zero assertions, three greens.
- **T-226** — **every member's `tests/` tree is outside the type gate.** Root `pyproject.toml:116`
  points mypy at `.pkgroot`, which reaches each member's `src/` only. Measured, not theoretical: the
  vacuous loop in T-225 produces a real mypy diagnostic (`Need type annotation for "relative"`) the
  gate has never reported — which is how it survived to be found by hand.
- **T-227** — the frozen test and the product **can select different manifest documents**. The frozen
  helper globs `fixtures.rglob('manifest*.json')` and takes the first file with a `personas` key at
  any depth; the product reads `fixtures.manifest.MANIFEST_PATH`. Same file today — latent, and
  silent by construction.

**Open defect tickets already in the graph:**

- **T-223 (CRITICAL)** — the price floor at `packages/contracts/src/boundary.py:819` is an **exact
  equality**, so under `max_discount_pct: 100` an offer at `unit_price: 0.001` buys a 100.00 product.
  Worse, on an **uncapped roster row** `_is_judged` (`apps/exchange/src/auction/collect.py:347`)
  returns False and the check is skipped entirely — `unit_price: 1e-09` returns **HTTP 201 as a
  rankable bid**. Highest-value single repair known; breaks no frozen assertion.
- **T-224 (HIGH)** — `list_price: 0.0` is an accepted roster value, producing an **unauthenticated
  HTTP 500 out of the middle of an auction** plus a 0.00 rankable fallback on the same row.
- **T-221 (HIGH)** — the k-anonymity floor defaults to `DEFAULT_K_ANONYMITY = 1` (a no-op) and
  `build_profile` **never calls `anonymise_cohort`**. Correct code behind a switch that is off.
- **T-222 (HIGH)** — a defect converted into documentation and then pinned by a passing test.

## Coverage this epoch OWES

**The rung-2 cross-branch interaction lens for the cycle-13 batch died before reporting and has not
been re-run.** Its brief: T-214's lock × T-216's shared-database handling × T-206's ledger replay,
**together in one process** — not three lanes verified separately. That class has already produced a
**600-second self-deadlock in this repo once**, and no single-lane gate can see it. It must be re-run
against post-wave `main`. **No green anywhere in this report covers it.**

## Harness hermeticity: PARTIAL

`verify` answers *did the frozen bytes move* — they did not; 17 frozen files, `manifest.json`
reconciled against the append-only `freeze-log.jsonl`, now at amendment 9. That is a real and
load-bearing question, and it is **strictly narrower** than *can this measurement be gamed*.

The structural residual is unchanged and still open: **product code imported by the acceptance suite
runs in the scorer's own process and can tamper with the scorer's in-process state.** That is
inherent to any in-process black-box suite. It is not hardened away — only *moved*, by a
subprocess-per-test design, at a cost this design does not pay. Hermeticity is **PARTIAL**. It has
never been sealed and is not sealed now, and no line in this report should be read as saying the
harness is intact in the broader sense.

The vocabulary trap in the artifact persists: `.swarm-loop/analysis/cycle-14.json` records
`harness_integrity: "intact"`. That field is the **byte-equality verdict** — the narrow question —
and it is not a statement about hermeticity.

## Decisions carried into cycle 15

- **Worktree provisioning satisfies every lockfile in the repo** — `uv sync` **and** `npm ci`, always.
  Two incidents is the evidence; a lane cannot report on a gate that aborts before reaching it.
- **`build_succeeds` is the priority, ahead of every remaining epic.** It is the only `regressing`
  verdict and the sole thing between 41 local commits and a push.
- **Get ESC-011 ruled before the E2/E5/E7/E8 wave finishes.** Nine finished branches with WEAK gates
  is the cycle-14 merge jam repeated at larger scale.
- **A gate spelled without `uv run python -m` is presumed broken.** The throwaway-worktree
  interpreter mismatch is now a reproduced, twice-costly class — check it at intake, not at merge.
- **T-223's proportional floor remains the highest-value single repair known**, unchanged from cycle
  13 and still unmade.
- **The cross-branch interaction lens is owed against post-wave `main`** — a commitment, not a
  suggestion, now carried for a second epoch.

## Open, recorded, not fixed

Everything cycle 13 recorded under this heading stands except where this cycle says otherwise.
**Closed this epoch by merge: T-032, T-034, T-035, T-042, T-043, T-044, T-045, T-215. Added and not
closed: T-225, T-226, T-227, T-228.** T-221, T-222, T-223 and T-224 remain open and unstarted.
ESC-008's stale `scripts/verify.sh:179-186` paragraph stands by ruling, known-false and deliberately
unrepaired. **ESC-011 is open and awaiting a decision.**

---

<sub>Cycle 14 · epoch `7279452..b255f31` (54 commits) · measured at `b255f31` ·
written against swarm-loop skill `3527702`.</sub>
