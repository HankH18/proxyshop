# Cycle 12 — the manifest became ground truth, and the guards that only held while it hadn't

HEAD at measurement `3927afd` · acceptance **76/120 (63.33%)**, from 75/120 (62.50%)
· 5/12 metrics at target · harness intact (16 frozen files, amendment 5) · codebase stall
0/3 · goal-progress stall 0/3.

A **measurement epoch, not a build epoch.** Ten commits, `ee099c2..3927afd`, and **not one
of them closed a build ticket.** The whole epoch is one human ruling and its consequences:
ESC-006 resolved, `fixtures/manifest.json` approved by Hank as ground truth, the
approval-guard tests that first approval proved wrong, and amendment 5 correcting my own
amendment 4. Tracked files 569 → 572, monotonic. The single acceptance point gained is
E8's, and it was gained by a human signature rather than by code.

## Harness hermeticity: PARTIAL

`verify` answers *did the frozen bytes move* — they did not; 16 frozen files, `manifest.json`
reconciled against the append-only `freeze-log.jsonl`, now at amendment 5 / `log_seq` 5. It
does **not** answer *can this measurement be gamed*.

The structural seam is unchanged and still open: **product code imported by the acceptance
suite runs in the scorer's own process and can tamper with the scorer's in-process state.**
That is inherent to any in-process black-box suite. It is not hardened away — only *moved*,
by a subprocess-per-test design, at a cost this design does not pay. So "did the frozen bytes
move" is a real and load-bearing question, and a strictly narrower one than "can this
measurement be gamed." Hermeticity is **PARTIAL**. It has never been sealed and is not sealed
now.

Cycle 10's second residual also stands unchanged: a frozen test can pass **decoratively**, so
`at target` is not the same as `enforced`. This epoch added a third instance of that shape and
it is recorded below as T-219 — the SELECTION line that amendment 4 introduced is *advisory
text*, and no machine consumer reads it.

## Metrics

Measured 2026-09-03T02:48:23–02:53:12 at HEAD `3927afd`. Values read from
`.swarm-loop/history.csv`, verdicts from `.swarm-loop/analysis/cycle-12.json`.

| metric | cycle 11 | cycle 12 | target | verdict |
|---|---|---|---|---|
| acceptance_pass_rate | 62.50 | **63.33** | 100 | converging_on_track |
| acceptance_collected | 120 | 120 | 120 | **at_target** |
| build_succeeds | 1 | 1 | 1 | **at_target** |
| spec_criteria_passing | 8 | 8 | 8 | **at_target** |
| e1_foundation_passing | 10 | 10 | 10 | **at_target** |
| e6_trust_passing | 26 | 26 | 26 | **at_target** |
| e8_proofs_passing | 5 | **6** | 8 | converging_on_track |
| e2_ingestion_passing | 6 | 6 | 8 | converging_on_track |
| e3_exchange_passing | 9 | 9 | 21 | converging_on_track |
| e4_store_agent_passing | 5 | 5 | 20 | converging_on_track |
| e5_merchant_passing | 4 | 4 | 10 | converging_on_track |
| e7_buyer_passing | 2 | 2 | 9 | converging_on_track |

**One metric moved: `e8_proofs_passing`, 5 → 6**, and it carries the whole
`acceptance_pass_rate` gain — 75 → 76 of 120. Six of the seven `converging_on_track`
metrics did not move at all this epoch. Read that verdict for what the analyzer says it
is: a linear fit over 13 points of per-cycle error, which the analysis file itself labels
*trend detection, not inference — a steering signal, not a statistical claim*. It describes
the run, not this epoch. This epoch's honest reading is **flat except E8**.

`all_targets_met: false`. `harness_integrity: intact`, no harness notes. No selection
regressions, no stale metrics, no escalated metrics, no manual records. Cycle 7 remains in
`pending_remeasure`.

## Push gate: PUSHED

Stated plainly because cycle 10 was NOT PUSHED and the difference should not be inferred.
Both remote-tracking tips — `gitlab/main` and `github/main` — sit at `e9849f7`, the cycle-12
measurement record commit and the single commit after this epoch's head. The whole range
`ee099c2..3927afd` is an ancestor of both.

## Tickets closed

**None.** No build ticket closed this epoch, by code or by triage. Saying so plainly is
more useful than padding the section: the epoch's work was the ESC-006 resolution, the
approval-guard test repair it exposed, and harness amendment 5. The ledger went 161 → 168
tickets against 141 edges, entirely by minting.

## Tickets minted

Seven, `T-214` … `T-220`, all from the **cycle-11 rung-2 verifier's** findings. Three of
the seven correct the orchestrator's own records rather than the product.

- **T-214** — amendment 4's unnamed operational regression. Every `graph`-marked test is
  also `docker`-marked, so the *old* `check` never built the session-scoped `_neo4j_guard`
  and never took the machine-global flock; the new one holds it for **196s of a 221.78s
  session (88%)**, measured with a 1 Hz probe. Every lane runs `check`, so concurrent lanes
  now serialise on a machine-global lock and the loser fails red for a machine reason — the
  false-red class cycles 10-11 have been closing. Fix belongs in the fixture's scope, not
  `verify.sh`.
- **T-215** — T-202 confirmed *and understated*. `accept()` never reads `exc.orphan`, so the
  live merchant discount code is still neither recorded nor revoked; worse, the denial reason
  is built as `f"checkout_refused: {type(exc).__name__}: {exc}"` and
  `OrphanedOffDomainCheckout`'s message embeds the discount code **verbatim**
  (`provider.py:310`). The unrevokable code is written into a persisted `policy_event`
  payload as free prose.
- **T-216** — the `InvalidSchemaName` flake has one mechanism and it is neither of the two
  previously recorded. Not order-dependence (T-196), not cross-lane collision (T-201): the
  unmarked test at `test_events_hardening.py:657` needs the `ledger` schema and requests no
  migration fixture, while session-scoped `ledger_migrated` **drops all four owned schemas**
  before re-applying. T-196 and T-201 are superseded, not merged.
- **T-217** — a retraction. T-212's central claim is false: the skip is delivered by the
  *fixture*, not the marker — `worker_database` calls `_require_services("postgres")`, which
  skips cleanly with Postgres down. Adding `@pytest.mark.docker` would change nothing.
- **T-218** — T-209 confirmed but understated, and generic rather than commitment-specific.
  The store-agent's walk short-circuits on **any** `None` while `--strict-nullable` makes a
  growing set of fields non-nullable at the pydantic door; the verifier enumerated **at least
  six shapes** the auditor admits and the contracts door refuses. Wants a property test
  asserting the two doors agree on nullability, not six point fixes.
- **T-219** — nothing consumes the SELECTION count, so T-117 is closed for the `docker`
  marker but not as a class. `grep -rn SELECTION` hits only `scripts/verify.sh`, and
  `build_succeeds` reads exit status alone. Demonstrated:
  `PYTEST_ADDOPTS='-k test_the_corpus_is_the_committed_one' ./scripts/verify.sh pytest` exits
  **0** while printing `SELECTION: 3975 deselected, 0 skipped` — a green gate having run 1 test
  of 3976. Visible to a human is not the same as binding on a machine.
- **T-220** — the cycle-detection replacement for `MAX_SWEEP_DEPTH` is correct, but the two
  tests grading "there is no depth at which a bid stops being checked" parametrize layers as
  `[1, 11, 13, 14, 64]` and `[1, 13, 14, 64]`, chosen either side of the **old** bound of 12.
  A re-introduced bound at 65 or beyond is invisible. The tests pin the fix, not the property.

## Escalations — 1 OPEN at epoch head

Open count went **3 → 1** across the epoch.

**ESC-006 · RESOLVED 2026-09-03T02:47:01** · `fixtures/manifest.json:213-246
expected_trust_trajectory (T-080 approved document)`, filed 2026-09-02T22:13:36 in cycle 10.
The approved manifest contradicted itself: replaying all seven `dishonest_store.behaviours`
once per episode against its own published `observation_weights` put **every non-zero episode
outside its own stated band** — 0.234 at ep2 against [0.34,0.50], 0.154 at ep4 against
[0.26,0.40], 0.084 at ep9 against [0.14,0.24], 0.066 at ep12 against [0.08,0.18]. The recorded
decision: trajectory **recomputed from the manifest's own published weights with the shipped
engine — 0.5 / 0.235 / 0.158 / 0.120 / 0.090 / 0.073 — derived twice independently, before and
after cycle 11 changed scoring, identical both times.** The replay schedule those numbers assume
is now recorded as part of the contract, since it was previously only inferable, which is how the
two halves drifted apart. Hank Holcomb approved the result himself at 2026-09-03T07:14:09Z
against body digest `0ce80606`; no agent filled `approver` / `approved_at` / `artifact`.

**ESC-005 · RESOLVED 2026-09-03T01:28:03** · `scripts/verify.sh:105 (T-117)`. Resolved on
cycle 11's clock; the resolution record and its follow-up both landed inside this range.
Amendment 4 was granted there — a SELECTION line, and `check` no longer blanket-deselecting
docker. **Amendment 5 landed here** (`fda3264`) and is not a refinement, it is a repair of my
own work, found by a rung-2 verifier auditing an amendment I also grade myself with:

1. **The bug.** `run_pytest`'s SELECTION parse ran under `set -euo pipefail` as
   `line="$(grep -E ... | tail -1)"`. A no-match grep exits 1, pipefail promotes it, errexit
   kills the function — before `return "$rc"`, before the SELECTION line, before `rm -f`.
   Measured exit codes destroyed: usage-error 4→1, INTERNALERROR 3→1, SIGKILL 137→1,
   interrupt 2→1, and a **green** run with an unparseable epilogue 0→1. Fail-closed, so it
   weakened nothing, but it discarded the true status and suppressed the SELECTION line in
   exactly the case where the parse had failed. Re-verified after: 4→4, 3→3, 137→137, 2→2, 0→0.
2. **Two false figures of mine, corrected in place.** Amendment 4 claimed "~3520 → 3908,
   ~388 docker tests". Truth: **3729 → 3975, so 246** newly-run docker tests — I had compared a
   pre-cycle-11 suite size against a post-cycle-11 one, charging ~142 newly-*added* tests to the
   change, wrong by ~58%. And there is **no** slow-marked test (`-m slow` collects zero), so
   "the single slow test" was wrong; `and not slow` is a present-day no-op.
3. **A third behavioural change I failed to name**, now named in the file — the Neo4j flock,
   ticketed as T-214.

**ESC-007 · still OPEN** · `.swarm-loop/acceptance/test_e4_store_agent.py::_claims_in /
_claim_identity`, filed 2026-09-02T23:30:29 in cycle 10. A frozen helper will fail on correct
code and is latent only by fixture accident: T-135 made provenance on `offer.discount`
mandatory, but `_claim_identity` keys a claim on `(key, value, source, ref)` and a `Discount`
has no `key`, so the very provenance now required makes the node unmatchable and it reads as
smuggled. Recorded as blocked on running the acceptance suite. **This is the one item in the
epoch the loop cannot act on by itself.**

## The approval exposed five guard tests that were wrong, and had always been

Fixing the manifest was the smaller half. Hank's first-ever approval turned **five** guard
tests red — the tests that enforce *no agent may approve* — and they were wrong rather than
newly broken.

Four asserted `manifest["approval"]["approver"] is None` after a refused call. That is a
different claim from the one they document ("the refused call wrote nothing"), and the two
coincide only while the sandboxed manifest happens to be unapproved — and `_sandbox()` copies
the repository's real manifest. **Proven by execution, not argument:** the same four bodies
run against a tree built from the pre-approval manifest at `2bad5f2` all pass, and after one
legitimate `record_approval("Ada Lovelace")` on that same untouched tree all four fail
identically. They break on any first approval, of any manifest, by anyone. Each now snapshots
every file under the sandbox and asserts the tree is **byte-identical** after — strictly
stronger, covering the golden set, catalog, request and record file, none of which were
asserted before. `is None` cannot see a refused call that *overwrites* a human's recorded
approval with a different name; the tree check catches it, naming the file.

The fifth doctored the committed manifest one field at a time to build a "half-written"
approval. Half-written is a claim relative to a **known** starting state, and after `ed1bc98`
`approver = "Grace Hopper"` is not half a decision — it is a whole one, misattributed. Each
state is now **built** by replacing the approval block outright: two half-states became eight,
plus two positive controls, so a body that refused every input could not pass.

Two controls settle that this is a real strengthening rather than a rewrite to green: on
today's approved tree the **old** bodies fail with no sabotage at all — their verdict was a
constant, and every catch they appeared to make was a false positive; and on the pre-approval
tree a sabotage that moves the automation check after document reading slips straight past the
old body and is caught by the new one. Eight sabotages caught in total, including a refused
call that rewrites the human's approval record.

The orchestrator did **not** repair its own restraint unexamined — it delegated this precisely
because it had just attempted the approval itself.

## Decisions carried into the next epoch

- **The replay schedule is now part of the manifest contract**, not something a builder infers.
  Under the published weights the store crosses the 0.35 threshold at **episode 1, not 4-6**;
  the lever for a slower demo decline is the replay schedule, not these numbers. T-081 and
  T-084 are graded against that rule.
- **ESC-007 is the only open ask.** T-042 and T-043 produce learned-policy discounts, which is
  exactly what makes the frozen helper fail; they must be told before they build, whichever way
  the ruling goes.
- **T-216 supersedes T-196 and T-201** — the flake has one mechanism, and the two prior
  diagnoses should be closed as superseded rather than merged with it.
- **T-217 retracts T-212's central claim.** The record, not the product, was wrong.
- **T-219 stands as the class-level residual of T-117.** Acceptance 2 is satisfied *literally*;
  nothing machine-readable consumes the SELECTION count, so a vacuous green is still a green
  to every consumer that matters.
- **T-214's fix belongs in the fixture's scope**, narrowing `_neo4j_guard` below session scope
  or releasing around the lock — not in `verify.sh`, which is protected and already at
  amendment 5.

## Open, recorded, not fixed

Everything cycle 10 recorded under this heading remains open except where a later cycle says
otherwise: T-177, T-169, T-178 / T-179, T-171, T-172 / T-180, T-170. This epoch adds T-215,
T-216, T-218, T-219 and T-220, and closes none of them. **No build ticket moved.**

---

<sub>Cycle 12 · epoch `ee099c2..3927afd` (10 commits) · measured at `3927afd` ·
written against swarm-loop skill `aac0b63`.</sub>
