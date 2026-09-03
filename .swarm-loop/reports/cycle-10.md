# Cycle 10 — the E6 trust epic, and four vacuous gates

HEAD at measurement `e0ffd1a` · acceptance **73/120 (60.83%)**, from 43/120 (35.83%)
· 5/12 metrics at target · harness intact (16 frozen files) · codebase stall 0/3 ·
goal-progress stall 0/3, explicitly **moved**.

The largest single-cycle move of the run: **+25.00 points**, against a previous best of
+7. Tracked files 514 → 526 → 558, monotonic across both integrations. Nothing was
pushed to either remote.

## Escalations — 2 OPEN, both needing a human ruling

### ESC-006 (open) · `fixtures/manifest.json:213-246` — the approved manifest contradicts itself

`expected_trust_trajectory` disagrees with the manifest's own `observation_weights`.
Replaying the seven `dishonest_store` behaviours once per episode against those published
weights puts **every non-zero episode outside its own stated band**: 0.234 at ep2 against
[0.34, 0.50]; 0.154 at ep4 against [0.26, 0.40]; 0.084 at ep9 against [0.14, 0.24]; 0.066
at ep12 against [0.08, 0.18]. Only ep0 — the 0.5 prior — is inside. The store crosses the
0.35 threshold at **episode 1** rather than the intended 4-6.

Latent today because `test_e8_proofs.py:413` asserts the trajectory's *shape*, not the
engine's numbers. It stops being latent the moment T-081's simulator is graded against it:
that ticket fails through no fault of its own, and the failure looks like a simulator bug
rather than a document inconsistency. It will also mis-grade T-084.

Measured by the T-062 lane while building the scoring engine that reads those same
weights. `fixtures/manifest.json` is T-080 single-writer ground truth, its approval digest
covers the body, and it is `pending_human_approval` — **the orchestrator did not touch
it, and will not.** The decision is which half is authoritative.

### ESC-005 (open) · `scripts/verify.sh:105` (T-117) — a gate cannot tell "passed" from "deselected"

CONFIRMED real, structurally undispatchable: the fix lives at a protected path that
`check-branch` vetoes every time, correctly, and the ticket's own non-goals forbid dodging
the veto by relocating the logic. It needs a user-approved `freeze --amend`. Cycle 9 lost
integration time twice to gate blindness of this class; cycle 10 found four more instances
of it (below). This is the same failure wearing different hats, and it will sit open
forever unless ruled on.

## Push gate: NOT PUSHED

Stated plainly because the run is otherwise green and it would be easy to assume otherwise.

Conditions **met**: `verify` passed; `analyze` did not exit 1; both integrations green;
tracked-file count monotonic.

Conditions **unmet**: (a) batch-1's rung 2 returned findings that materially affect a
branch merged this epoch — T-169's decorative pass; (b) batch-2's rung 2 had not returned
when the gate was evaluated. Both carry to cycle 11's gate. Nothing reached
`labs.gauntletai.com` or GitHub this cycle.

## Metrics

| metric | cycle 9 | cycle 10 | target |
|---|---|---|---|
| acceptance_pass_rate | 35.83 | **60.83** | 100 |
| e6_trust_passing | 2 | **26** | 26 — AT TARGET |
| e3_exchange_passing | 3 | **9** | 21 |
| e2_ingestion_passing | 6 | 6 | 8 |
| e4_store_agent_passing | 3 | 3 | 20 |
| e5_merchant_passing | 4 | 4 | 10 |
| e7_buyer_passing | 2 | 2 | 9 |
| e8_proofs_passing | 5 | 5 | 8 |
| acceptance_collected · build_succeeds · spec_criteria_passing · e1_foundation_passing | 120 · 1 · 8 · 10 | unchanged | all AT TARGET |

**Every metric is `converging_on_track` or `at_target`. Zero regressing, zero stalled, and
the `converging_off_track` verdict that dominated cycle 9 is gone.** Steepest remaining
slopes: e3 at -0.60, e8 at -0.55, e2 at -0.57. Flattest: e7 at -0.22 and e4 at -0.33,
which is where cycle 11's weight belongs — e4 is 3/20 and has not moved in three cycles.

Carried, unchanged: the analysis still reports **goalposts moved 3x** and a **re-measure
owed for cycle 7**. All-targets-met stays withheld until that re-measure runs. Neither is
new this cycle; both remain true.

## The cycle's real theme: gate credibility, not feature work

The +25 points are the visible result. The durable result is that four gates were proven
**vacuous** — green while their defect was live — by four independent routes that did not
know about each other.

1. **`red-check` on T-111 and T-123.** Both gates **exit 0 with the defect present**, at
   3046 and 210 tests selected respectively. This is the direct explanation for something
   cycle 9 could only record as an anomaly: earlier waves reported both tickets fixed
   while git says the files were never touched. The lanes were not lying. The gate could
   not see the defect, so passing it was not evidence of anything.
2. **A build lane tripped over `test_checkout_provider.py:406`.** Vacuous via a bare
   `except ImportError` — the module it tested did not exist, so the test passed by
   skipping its own subject. T-033 created the module, and the test immediately went red.
   It had been green for its entire life and had never once run.
3. **The ticket graph's own `verify` strings are vacuous** (T-160). The strings that
   decide whether a ticket may close are themselves unchecked.
4. **One datastore blip silently skipped all 244 docker-marked tests at exit 0**, before
   T-109. Measured after the fix with Redis genuinely down: 233 pass, 11 skip — the
   difference between a suite that reports what it could not run and one that reports
   nothing at all.

Four routes, four vacuous gates, one shape. This is the same defect ESC-005 asks to close
at the harness level, which is why that escalation matters more than its size suggests.

## E6 was never "off track" — it had never started

Cycle 9 named `e6_trust_passing` the priority and the only `converging_off_track` verdict,
slope -0.19. The diagnosis was wrong, and measurement said so immediately: all 24 failures
were **ImportError against six 0-byte files** carrying nothing but the T-000 skeleton
commit. Not slow convergence. No code at all.

The scheduler was holding all five E6 tickets behind dependencies **their tests never
touch**. Released, the epic went 2 → 26 in one batch and hit target.

One structural fact found on the way, worth keeping: **T-062 and T-065 are mutually
dependent at test time** — T-062's tests import `packages.verification`, T-065's import
`apps.trust.src.scoring` — while the graph declares only one direction. They could not
have been separate lanes under any scheduling. The graph's dependency edges and the tests'
actual import closure are two different graphs, and only one of them was being consulted.

## The triage saving

Twenty-one finding-tickets went through adversarial triage before any lane was dispatched.
Result: **6 confirmed, 9 refuted, 6 already fixed.**

Fifteen lanes of work avoided. Several would have been actively harmful, not merely
wasted: **T-149's entire scope is the one file T-151 was in the middle of fixing** — a
lane would have rewritten green, tested code underneath a live branch. Six of the nine
refutations were the "zero production callers" pattern that cycle 9 already identified as
unreliable here.

And a fact worth recording exactly as it happened: **T-124 had already been fixed by the
very commit that regenerated the backlog still calling it broken.** The backlog described
a tree that the same commit had made obsolete.

Triage is cheaper than dispatch by more than an order of magnitude, and this cycle it paid
for itself fifteen times over.

## Rung 2 found what per-branch review structurally cannot

Five verifier lenses — four read-only, one sabotage — run by agents that built none of the
code. What they found is not a longer list of the same things a branch reviewer finds. It
is a different class, visible only on the combined tree.

**T-177 · CRITICAL · T-153's wall is on the wrong door.** Measured on the clean tree,
through the real boundary: a hosted bid with a genuine 20% grant, charging **15.00 against
a 100.00 list price**, gets `validate_bid` → `ok=True, reasons=[]`. Price reconciliation
exists only on the **emitting** side, in store-agent. `contracts.boundary` — the
**validating** side, and the only door the exchange actually runs — has none. A store not
running our runtime bypasses the check entirely, which is precisely the threat model the
check exists for.

**T-169 · HIGH · a frozen release blocker passes decoratively.** Nothing calls
`use_registered_domains`. In the deployed app the guard falls back to
`bid['store_domain']` — a **bidder-controlled field**. A bidder that lies consistently
(`store_domain` and `checkout_url` both `attacker.tld`) defeats it, and the frozen test
cannot see that because it never wires a registry. `spec_criteria_passing` counts S8-3 as
met. It is not enforced.

**T-178 / T-179 · HIGH · two price walls a full green suite does not defend.**
Bid-wide reconciliation — one grant licensing every priced node — passed 3368 tests with
an execution witness. The test written to catch it is vacuous because its second offer is
priced at 40.00; the one-literal fix is 80.0. Separately, tolerance is undefended below
0.50: both 0.49 and 0.25 pass everything, and
`test_a_cent_of_currency_rounding_is_not_an_unauthorised_discount` survives a tolerance of
**1.00** by a margin of 0.0015. It defends nothing it claims to defend.

**T-173 · HIGH · a removed line the per-branch audit CLEARED became a silent fail-open.**
The orchestrator inspected T-153's seven deletions and judged them benign. One was not:
`list_price()` swallows `TypeError`/`ValueError`, and `or 0.0` coerces the resulting None.
A price that cannot be parsed becomes a free item. **Only the combined diff caught it, and
only after the orchestrator had explicitly signed it off.** That is an orchestrator error,
recorded below as one.

**T-171 / T-172 / T-180 · the infrastructure the gates run on.** The D37 Neo4j lock is
**machine-global** with a 600s wait that is unreachable behind the repo-wide
`--timeout=300`. T-109's service inference has an 8-item hole, five of which are the
schema-grants security tests. And a dropped `FIXTURE_SERVICES` entry silently restores the
old defect with no signal at all.

### What held, with credit

Rung 2 is not only a defect list, and the things that survived it survived real attacks:

- The `registered_domains` guard caught **two attacks nobody had claimed to defend
  against** — fail-open-on-miss and a self-seeded registry — with `domain_verified` doing
  real work rather than decorating.
- **T-151 goes red on behaviour, not source text.** Proven by a sabotage that left the
  tuple correct and walked it wrongly; the test still failed. That is the property most
  gates in this repo do not have.
- The bootstrap guarantees **fail loudly when swallowed**, which is the whole point of
  T-109 and the reason the 244-test skip cannot recur silently.

## Harness hermeticity: PARTIAL — and the residual is now two things

`verify` answers *did the frozen bytes move* — they did not, all 16 files re-hashed clean
against `manifest.json`, itself reconciled against the append-only `freeze-log.jsonl`. It
does not answer *can the measurement be gamed*.

The **structural seam is unchanged**: product code imported by the acceptance suite runs
in the scorer's own process and can tamper with its in-process state. Inherent to any
in-process black-box suite; moved, not closed, by a subprocess-per-test design this run
does not pay for.

Cycle 10 adds a **second, sharper residual: a frozen test can pass decoratively.** T-169
is the proof — the bytes are untouched, the test is green, and the property it names is
not enforced in the deployed app. So **"at target" is not the same as "enforced"**, and
`spec_criteria_passing == 8` should be read as "eight frozen tests pass", not "eight spec
criteria hold". Both residuals are open. Neither is closable by re-hashing anything.

## Tickets

**Closed by code (15):** T-151 (the ledger writer resolves its own per-role DSN instead of
`app`), T-153 (bid price reconciled against declared discount depth), T-033 (accept mints
code + permalink + a C11 event trail; the first consumer of T-036's checkout port),
T-109 / T-111 / T-123 (per-service reachability; provisioning fails loudly; `UF_HIDDEN`
cleared recursively), the whole E6 trust epic T-061 / T-062 / T-063 / T-064 / T-065,
T-135 (the boundary walks every claim-bearing site, not just `bid.claims`), T-031
(candidate retrieval), T-139 and T-133 (buyer privacy).

**Closed by triage — already fixed or refuted (16):** T-124, T-130, T-132, T-136, T-137,
T-138, T-140, T-141, T-142, T-143, T-144, T-145, T-147, T-148, T-149, T-150.

**In flight:** T-041 is accepted and integrating as cycle 11's first merge. It discharges
T-152 by calling `enforce_bid_provenance` on the **whole Bid** — the exact gap cycle 9
recorded as open and unfixed.

**Minted:** T-154 through T-183, **30 tickets** from 30 recorded findings.

**Ledger re-seeded:** 123 tickets / 141 edges · 75 closed / 48 open.

## Integrations

Two batches, both `OK: all`, **zero merge conflicts** across nine lanes.

- **Batch 1 — `0e96d1f`** · T-151, T-153, T-033, T-109, T-111, T-123 ·
  3368 pytest / 644 vitest / mypy over 182 files.
- **Batch 2 — `e0ffd1a`** · T-061, T-062, T-063, T-064, T-065, T-135, T-031, T-139,
  T-133 · 3756 pytest / 668 vitest / mypy over 202 files.

## Orchestrator errors — recorded without softening

1. **Told every lane to prefer `codegraph explore`, which cannot work in a worktree.**
   `.codegraph/` is gitignored, so it does not exist there, and the CLI **silently answers
   from another index** rather than failing. Measured: the same query returned 48 symbols
   across 3 files correctly in the primary tree, and 25 symbols in 1 file of pydantic
   internals in a worktree. Every lane that followed the instruction got confidently wrong
   answers with no error. Retracted mid-flight to all live lanes.
2. **Cleared a removed line as benign that was a fail-open** — T-173. The per-branch audit
   looked at all seven of T-153's deletions and passed them. One was a silent fail-open on
   price parsing, and it took the combined diff to find what the sign-off had missed.
3. **Gave every lane the same scratchpad path without saying it was shared.** Two lanes
   collided on `mutate.py`, and one executed with **another lane's worktree as cwd**. It
   died on a nonexistent path; forensics confirms no damage. That outcome was luck, not
   design.
4. **Briefed a lane that a test file was new when it already existed at 526 lines.**
5. **Ruled 7 findings refuted on "no production caller", then minted 2 of the same shape
   hours later.** Caught by the ledger regeneration, adjudicated at ground truth: the
   discriminator is whether a **frozen test currently passes because of the thing**.
   T-176 records the ruling so the next pass cannot re-litigate it.
6. **`state.json` carries no `worktree_root`**, so `retire` reclaimed nothing — it named
   the branches to delete and reported "0 retired · 5 kept · worktree root size:
   unmeasured", leaving ~4 GB on disk. Worse, a resumed session would see **zero live
   worktrees** regardless of how many exist, and re-dispatch every in-flight ticket into a
   second set. Inherited, not introduced — but unrecorded until now, which is the error.

## Process learnings

Recorded in full in `.swarm-loop/learnings.md`; the operative rules:

- **A lane's own sub-agents must not mutate its worktree.** Pinning a branch tip is
  necessary and not sufficient — assert the worktree is **clean** before collecting.
- **CodeGraph is primary-tree-only**, and fails silently rather than loudly outside it.
- **The shared scratchpad needs lane-private filenames**, stated in the brief.
- **Pass `--worktree-root` at `init`.** `state.json` is guard-protected against in-place
  rewrites, so the gap cannot be patched after the fact and survives every later session.
- **"Zero production callers" is not a defect by itself.** An unwired half is scheduled
  work; it becomes a defect the moment a frozen acceptance test counts it as satisfying a
  requirement. Ask that one question first, and record the answer.

## Open, recorded, not fixed

- **T-177** — `contracts.boundary` performs no price reconciliation. The exchange's only
  door is unguarded; a non-cooperating store walks through it.
- **T-169** — the registered-domain guard is unwired in the deployed app, and a frozen
  release-blocker test counts it as met.
- **T-178 / T-179** — bid-wide reconciliation is undefended, and the discount tolerance is
  undefended below 0.50.
- **T-171** — the D37 Neo4j lock's 600s wait is unreachable behind `--timeout=300`.
- **T-172 / T-180** — T-109's inference has an 8-item hole including five security tests,
  and a dropped `FIXTURE_SERVICES` entry silently restores the old defect.
- **T-170** — no ticket in the 123-ticket graph owns
  `apps/exchange/src/accept/routes.py`. Not a defect; a **schedule gap**, which is worse,
  because nothing will otherwise pick it up.
- **ESC-005 and ESC-006** remain open and are the only two items in this report that the
  loop cannot act on by itself.
