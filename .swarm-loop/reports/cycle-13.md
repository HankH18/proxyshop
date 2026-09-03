# Cycle 13 — four tickets merged, twelve metrics unmoved, and the scheduler that explains both

HEAD at measurement `7279452` · acceptance **76/120 (63.33%)**, from 76/120 (63.33%)
· 5/12 metrics at target · harness hermeticity **PARTIAL** (16 frozen files, amendment 7)
· codebase stall 0/3 · goal-progress stall **1/3**.

A **build epoch that measured flat.** Nineteen commits, `3927afd..7279452`, four merges that each
closed a real defect — T-214 (`e478ff4`), T-206 (`abd79fb`), T-216 (`2b6eec4`), T-177 (`85f79e3`)
— plus ESC-007 landing as amendment 6 (`4f5c592`) and two orchestrator docstring corrections
(`e8aaec5`, `7279452`). Tracked files 572 → 580, monotonic. And **all twelve metrics are
byte-identical to cycle 12**: same values, same errors, same targets. The product tree moved and
the goals did not.

That is not a paradox, and this cycle spent its effort establishing *why* rather than guessing.
The answer is the scheduler, and it is the most consequential finding of the run so far.

## Harness hermeticity: PARTIAL

`verify` answers *did the frozen bytes move* — they did not; 16 frozen files, `manifest.json`
reconciled against the append-only `freeze-log.jsonl`, now at amendment 7. It does **not** answer
*can this measurement be gamed*.

The structural seam is unchanged and still open: **product code imported by the acceptance suite
runs in the scorer's own process and can tamper with the scorer's in-process state.** That is
inherent to any in-process black-box suite. It is not hardened away — only *moved*, by a
subprocess-per-test design, at a cost this design does not pay. So "did the frozen bytes move" is
a real and load-bearing question, and a strictly narrower one than "can this measurement be
gamed." Hermeticity is **PARTIAL**. It has never been sealed and is not sealed now.

Note the vocabulary trap in the artifact itself: `.swarm-loop/analysis/cycle-13.json` records
`harness_integrity: "intact"`. That field is the byte-equality verdict — the narrow question —
and it is not a statement about hermeticity. Read it as *the frozen files did not drift*, which
is what it measures, and nothing more.

## Metrics

Measured 2026-09-03T13:10:16–13:12:33 at HEAD `7279452`. Values read from
`.swarm-loop/history.csv`, verdicts from `.swarm-loop/analysis/cycle-13.json`.

| metric | cycle 12 | cycle 13 | target | verdict |
|---|---|---|---|---|
| acceptance_pass_rate | 63.33 | 63.33 | 100 | converging_on_track |
| acceptance_collected | 120 | 120 | 120 | **at_target** |
| build_succeeds | 1 | 1 | 1 | **at_target** |
| spec_criteria_passing | 8 | 8 | 8 | **at_target** |
| e1_foundation_passing | 10 | 10 | 10 | **at_target** |
| e6_trust_passing | 26 | 26 | 26 | **at_target** |
| e2_ingestion_passing | 6 | 6 | 8 | converging_on_track |
| e3_exchange_passing | 9 | 9 | 21 | converging_on_track |
| e4_store_agent_passing | 5 | 5 | 20 | converging_on_track |
| e5_merchant_passing | 4 | 4 | 10 | converging_on_track |
| e7_buyer_passing | 2 | 2 | 9 | converging_on_track |
| e8_proofs_passing | 6 | 6 | 8 | converging_on_track |

**Not one metric moved.** Seven of the twelve still read `converging_on_track`, over a linear fit
of 14 points of per-cycle error (`acceptance_pass_rate` slope −4.98/cycle, r² 0.82; `e3` −0.84,
r² 0.75; `e4` −0.46, r² 0.80). The analysis file labels its own output *trend detection, not
inference — a steering signal, not a statistical claim*. It describes the run. It does not
describe this epoch, and cycle 12 already had to say the same thing about six of the same seven
metrics. **This epoch's honest reading is FLAT.**

`all_targets_met: false`. No harness notes, no selection regressions, no stale metrics, no
escalated metrics, no manual records. Cycle 7 remains in `pending_remeasure`.

One measurement detail worth preserving, because it is the only movement in the number all epoch
and it was a self-inflicted dip rather than progress: merging T-177 dropped the suite 76 → 75,
caught on a post-merge measurement and traced to
`test_spec_criteria.py::test_exchange_source_never_imports_envelope_or_sealed_surfaces`. That
check scans **string literals** in exchange source against `re.compile(r"sealed\.|envelopes",
re.IGNORECASE)`, and two new docstrings cited the import-linter rule by its full name — which
contains the plural. The prose explaining *why* the exchange may never read a merchant's envelope
tripped the guard that enforces it. There was never a code path: `lint-imports` reported
"Contracts: 2 kept, 0 broken" before and after. Fixed at `7279452` by citing the rule as "the C3
contract in `.importlinter`", restoring SPEC to 8 and the rate to 63.33. The frozen test is
untouched. Worth knowing for every future author in that package: **this check will flag any
docstring containing the plural "envelopes" or the string "sealed." — a blunt proxy that cannot
tell prose from a code path.**

## Push gate: NOT PUSHED

Stated plainly because cycle 12 *was* pushed and the difference should not be inferred. Both
remote-tracking tips — `gitlab/main` and `github/main` — sit at `e9849f7`, the **cycle-12**
measurement record. The entire cycle-13 epoch is local. `push_remote` is `when-clean`, and this
epoch does not close clean: T-177 merged and was then measured defective (below).

## The diagnosis: thirteen cycles, and the largest error in the run was never dispatched

Diagnosed rather than guessed, and measured against HEAD `7279452`.

The 44 red acceptance tests are dominated by two epics: E4 at 5/20 and E3 at 9/21, **27 of the
44**. Independent diagnosis of both epics found that **every one of those 27 failures is an
ImportError or ModuleNotFoundError. Not one assertion is reached.** Seven whole packages were
never written — `apps/exchange/src/{ranking,policy,reports}/` and
`packages/store-agent/src/{learning,modes,external}/` are 0-byte `__init__.py` files with no
siblings, and `apps/seller-reference/src/` holds only `__init__.py`.

The tickets that own them — T-032, T-034, T-035, T-042, T-043, T-044, T-045 — are all open with
**every dependency closed**. And `dispatch.jsonl` held **zero records naming T-042, T-043, T-044
or T-045 across all 114 entries.** Thirteen cycles in which the largest single block of error in
the run was never once handed to a lane.

The cause is mechanical, in two halves, and neither is a judgement call.

**Half one: the frontier ranks by `unblocks`.** That count measures graph *shape* — how many
tickets a node gates — and is blind to the graded metric. A ticket worth 15 acceptance points
ranks below a scaffold ticket that gates six cheap ones. Ranking by graph topology while being
scored on a metric is a ranking function that cannot see its own objective.

**Half two: the frontier had never been given a closure source.** It read `0 closed` on every
invocation, which is not a scheduling opinion — it is a **broken instrument**. With nothing
recorded as closed, the frontier degenerates to the graph's roots and ranks the already-built
scaffold first, forever. A closure store was added this epoch at `29514b2` and the backlog
regenerated against it.

To those two, ESC-009 added a third: **six independent builds were being serialized by a coarse
`scope` glob.** T-032/T-034/T-035 each declared `apps/exchange/tests/**` and T-042/T-043/T-044
each declared `packages/store-agent/tests/**`, so `check-wave` correctly vetoed all six pairs
within the two trios on a glob intersection — only one of each trio could ever be in flight. Their
real ownership is disjoint (`test_ranking.py` / `test_bandit.py` / `test_loss_reports.py`;
`test_external_bids.py` / `test_learning.py` / `test_modes.py`), and each tests directory's
`conftest.py` auto-loads uniquely-named `_fixtures_<topic>.py` siblings, so no lane needs to edit
a shared file. **The glob was coarser than the truth, and `check-wave` intersects the globs, not
the truth.**

## The ledger reconciliation

Run by three independent recon lanes against HEAD, each required to produce `file:line` evidence
rather than a verdict.

**45 tickets closed on recorded verdicts**: 40 verified FIXED at HEAD with evidence, 4 adjudicated
**record-not-work** (T-168, T-176, T-190, T-200 — the record was wrong, not the product), plus
T-000. The frontier went **0 closed / 87 READY → 45 closed / 49 READY**.

The reconciliation itself minted two tickets, both of the shape this repo keeps producing:

- **T-221 (HIGH)** — the k-anonymity floor T-138 delivered is switched off by default, so the
  re-linkability T-138 exists to prevent still holds on the path the product actually takes. Two
  independent reasons: `DEFAULT_K_ANONYMITY = 1` at `profile/__init__.py:175` (a floor of 1 is a
  no-op, raised only if `PROXYSHOP_BUYER_K_ANONYMITY` is set in the environment), and the
  single-account entry point `build_profile` **never calls `anonymise_cohort` at all** — only
  `build_profiles` does. The generalisation ladder, the floor and the cohort function are correct
  code sitting behind a switch that is off and a caller that does not call it. T-138 was closed on
  the *existence* of the mechanism; this ticket is about its *reachability*.
- **T-222 (HIGH)** — a defect was converted into documentation and then pinned by a PASSING test,
  so the suite now certifies the broken behaviour instead of refusing it. `accept/offer.py:38-57`
  carries a section headed "Two limits of this layer, stated so the next caller does not assume
  otherwise" which restates T-157 and T-158 nearly word for word, and names a test that passes by
  asserting the orphaned-code behaviour. **The general rule it violates: a test may pin a LIMIT
  that is a design decision, but it may never pin a DEFECT that has an open ticket against it** —
  once it does, closing the ticket requires editing a passing test, the test-edit ritual makes
  that expensive, and the defect ends up protected by the machinery meant to catch it.

## T-177 landed, and is DEFECTIVE at the production door

Both halves of this belong in the record, and neither cancels the other.

**The attack T-177's own ticket described IS genuinely closed.** A hosted bid carrying a genuine
hook-minted 20% grant, declaring 20%, charging 15.00 on a product listed at 100.00 returned
`ok=True, reasons=[]` from `validate_bid` before this epoch. The reconciliation lived only in the
store-agent's `enforce_bid_provenance` — the **emitting** side — so the wall protected only bids
produced by our own runtime. It now runs on the **validating** side, at the real HTTP door, in
three adversarial passes: an opt-in roster parameter with no caller (refuted), then a capped depth
with a wired caller and TypeScript parity (refuted again — an emitter could disarm the wall by
staying *silent*, omitting the `discount` block and charging 0.00), then the silent-emitter bypass
closed. The evidence went past re-running the lane's tests: 19,200-combination differential fuzz
of `boundary._price_reasons` against `main` with `list_prices=None` produced **zero** verdict
differences; 600-combination differential fuzz of `collect_bids` changed 411 verdicts, **all 411
in the refusing direction**, zero newly admitted; seven independent sabotages, six Python and one
TypeScript, each failed fast.

**And a surviving verifier lens then measured three bypasses at the production door**, minted as
**T-223 (CRITICAL)** and **T-224 (HIGH)**:

1. **The floor is an exact equality.** `boundary.py:819` tests `priced == 0.0`, not a proportional
   floor. Under `max_discount_pct: 100` an offer at `unit_price: 0.001` on a 100.00 product is
   admitted through the real production door. R10 only compels admitting a 53% undercut; this
   admits 99.9999999%.
2. **An uncapped roster row skips the check entirely.** `_is_judged` (`collect.py:347`) returns
   False when `listed <= 0`, so the offer is never judged. `unit_price: 1e-09` on a 100.00 product
   returns **HTTP 201 as a rankable bid**.
3. **`list_price: 0.0` is an accepted roster value** (`Field(ge=0.0)`), and on such a row an
   unparseable price reaches `float(...)` unguarded and raises `ValueError` out of the middle of
   an auction as an **unauthenticated HTTP 500** — killing the auction every other rostered store
   is bidding in. On the same row a silent store still mints a **0.00 rankable fallback**. Not a
   regression (`git show main:` of that module raises the identical `ValueError`), but the
   protection the surrounding docstring describes is conditional and **the condition is
   caller-supplied**.

All three were measured end to end through `POST /auctions`, and all three were independently
written into that module's own docstring by the orchestrator at `e8aaec5` — where they were
deferred to a "derived-authorization follow-up ticket" **that was never minted**. T-223 and T-224
are that follow-up. The single most actionable repair is replacing the exact-equality floor at
`boundary.py:819` with a proportional one, which breaks no frozen assertion.

## Two hypotheses that did not survive measurement — do not re-chase them

Recorded because both are plausible, both were investigated, and both are wrong. An unrecorded
refutation gets re-derived.

- **The suspected `__init__.py` production break does not exist.** The import in question was
  **reflowed, not deleted.** There is no missing export and no broken package.
- **The tolerance concern is backwards.** `PRICE_RECONCILIATION_TOLERANCE = 0.01`
  (`packages/contracts/src/boundary.py:129`, mirrored at `boundary.ts:97`) is **ABSOLUTE, not
  proportional** — it is added to `unit_price` before the comparison, not scaled by the listed
  price. So the maximum underpayment it permits on a 10,000 item is **one cent**, not 1%. The
  worry was that it scaled; it does the opposite.

## A coverage gap that is OWED, not closed

**The cross-branch interaction lens died before reporting.** Its brief was to run T-214's lock ×
T-216's shared-database handling × T-206's ledger replay **together in one process** — not three
lanes verified separately, but the three merged behaviours interacting.

That class is not hypothetical here. It has already produced a **600-second self-deadlock in this
repo once**. Two of the three merges in question are specifically about machine-global contention
(T-214's flock) and shared-database teardown (T-216's session-scoped schema drop), which is
exactly the pair that interacts badly and exactly the pair no single-lane gate can see.

**This lens must be re-run against post-wave `main`.** It is the one piece of verification this
epoch owes and did not deliver, and no green anywhere in this report covers it.

## Escalations

Three resolved this epoch; read them in full in `.swarm-loop/ESCALATIONS.md`.

- **ESC-007 · frozen-test · GRANTED, landed as amendment 6 (`4f5c592`).** The frozen E4 helper
  `_claim_identity` keyed every provenance-bearing node on `(key, value, source, ref)`, and a
  `Discount` has no `key` — so the very provenance T-135 made mandatory collapsed every discount
  onto the single identity `'None'` and read as **smuggled**. It was latent only by fixture
  accident (the context is cold, `learned_policy=None`, so no discount is produced); T-042 and
  T-043 build discounted bids and would have turned it red **on correct code**. An unkeyed node is
  now identified as `('<unkeyed>', value, source, ref)`, and `_hook_identities` lends that unkeyed
  slot **only** to the `authorized_discount_pct` grant. The first draft was refused: an adversarial
  verifier measured it admitting a 100%-off discount wearing a scraped `list_price` provenance.
  That piggyback class is now permanently asserted. Weakens nothing — per-test results identical
  across the amend (15 failed / 5 passed, the same 5 passing), and disabling the new guard turns
  the hosted-trace test RED, so the restriction is load-bearing rather than decorative.
- **ESC-008 · harness · DECLINED by Hank.** The frozen paragraph at `scripts/verify.sh:179-186`
  describes, **in the present tense**, a machine-global flock behaviour that T-214 (merged
  `e478ff4`) removed: `_neo4j_guard` is now function-scoped and the hold is 10.1–10.9% of a run,
  independently re-measured by an adversarial verifier with its own 20 Hz probe. The comment is
  false. It is **prose only** — no behaviour depends on it — and the ruling was not to spend a
  harness amendment on a comment. **The paragraph stays and is hereby known-stale.** Standing
  instruction: if `verify.sh` is ever amended for a substantive reason, correct that paragraph to
  past tense naming `e478ff4` in the same amendment, so the comment costs no amendment of its own.
- **ESC-009 · frozen-goal · APPROVED by Hank, landed as amendment 7.** `tickets.json` only. (a)
  Six scope globs narrowed from directory granularity to real per-file ownership; (b) three
  `depends_on` edges dropped — T-034→T-032, T-035→T-032, T-045→T-044 — each contradicted by the
  **frozen test body read in full** (the policy and reports tests import nothing from ranking and
  feed plain fixture dicts; the persona test imports only the personas module and the manifest, no
  signing, no door, no queue). **The veto gets SHARPER, not looser**: two lanes that would
  genuinely write the same file are still refused, and no lane gains the right to touch a file it
  could not touch before. Graph validated acyclic, no dangling deps, 170 tickets. No metric,
  target or acceptance test changed. Verified it did the job: `check-wave` on all seven tickets
  now exits OK where every pair within the two trios was previously vetoed.

## The session died in an outage, and the recovery is part of the record

Stated because a run that hides its interruptions cannot be audited. The session driving this
epoch **died in a network outage, and a second outage followed.** What the recovery did, and the
labels it used, matter more than the fact of it:

- An interrupted lane's uncommitted edits were **rescued under explicit `INTERRUPTED` /
  `UNVERIFIED` labels** — not merged, not credited, not silently folded into a green.
- A **dead cycle-11 verifier dispatch was closed as dead** rather than left pending, so the ledger
  does not carry a phantom in-flight lane.
- **Four merged worktrees were retired** (`last_retire_at` 2026-09-03T13:09:46, immediately before
  measurement).
- **The git-guard was re-probed live before any new mutating dispatch.** A guard that was armed in
  a dead session is not a guard; the only acceptable evidence is that it answers now.

## Tickets minted

**Four**, in two pairs, and both pairs came from work the epoch did rather than from a sweep:
**T-221** and **T-222** out of the ledger reconciliation; **T-223** and **T-224** out of the
surviving verifier lens on the merged T-177. The graph went 168 → 172 tickets. Titles and full
mechanism text live in `tickets.json`; T-223 and T-224 are summarised above.

## The goalposts have moved seven times

`.swarm-loop/freeze-log.jsonl` now records **amendments 1 through 7**, three of them in this epoch
and the previous one the day before. Every amendment was user-approved and every one is
justified in the log with a both-directions verification — but seven is enough that **the trend
now spans more than one target**, and any read of the 14-point regression must carry that. The
suite went 103 → 120 tests at amendment 1; `goals.json` itself changed at amendments 1 and 3;
`scripts/verify.sh` changed at 4 and 5; `tickets.json` at 6's neighbourhood and at 7. A slope
fitted across that history is fitted across a moving target, and it is a steering signal, not a
measurement.

(For anyone reading the analysis file directly: `cycle-13.json` records `goal_amendments: 6`. It
was generated at 13:14:41 and amendment 7 landed at 13:37:17. The file is not wrong; it is
earlier.)

## Decisions carried into the next epoch

- **Rank the frontier by graded-metric error contribution, not by `unblocks`.** This is the
  epoch's central finding and it is a permanent change of instrument, not a one-off correction.
- **The frontier gets a closure source on every invocation, and the ledger is reconciled against
  the graph at every resume.** `0 closed` is a broken instrument reading, and it must be treated
  as an error rather than as an answer.
- **Per-file ownership is written at intake**, not inferred from a directory glob later. When
  `check-wave` vetoes, the first question is whether the collision is real or an artifact of a
  coarse glob.
- **T-223's proportional floor is the highest-value single repair currently known** — it breaks no
  frozen assertion and closes the largest of the three T-177 bypasses.
- **T-206 must land before buyer feedback is routed through the ledger.** Its own ticket text
  carries that as a standing prohibition, and merging its fix does not by itself retire the
  warning until the replay path is exercised end to end.
- **The cross-branch interaction lens is owed against post-wave `main`.** See above; this is a
  commitment, not a suggestion.

## Open, recorded, not fixed

Everything cycle 12 recorded under this heading remains open except where this cycle says
otherwise. **Closed this epoch by merge: T-214, T-206, T-216, T-177** — with T-177 closing its own
described attack and immediately reopening as T-223/T-224 at the production door. **Added this
epoch and not closed: T-221, T-222, T-223, T-224.** ESC-008's stale `verify.sh` paragraph stands
by ruling, known-false and deliberately unrepaired.

---

<sub>Cycle 13 · epoch `3927afd..7279452` (19 commits) · measured at `7279452` ·
written against swarm-loop skill `aac0b63`.</sub>
