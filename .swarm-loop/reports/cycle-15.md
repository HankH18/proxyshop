# Cycle 15 — the red that held the push goes green, and for the first time nothing is regressing

HEAD at measurement `8ba6c36` · acceptance **111/120 (92.50%)**, from 103/120 (85.83%) · 7/12 metrics at target
· harness hermeticity **PARTIAL** (frozen bytes intact, amendment 11) · codebase stall 0/3 · goal-progress 0/3.

Thirty-four commits, `b255f31..8ba6c36`, in two integration batches. **`build_succeeds` went 0 → 1** — the single
red that held the push gate for two consecutive epochs — and **acceptance went 103 → 111 of 120**. Tracked files
613 → 663. The structural result matters more than either number: **no metric on the board is `regressing` or
`stalled`; all twelve are `at_target` or `converging_on_track`, for the first time in the run.** Five still carry
error, E7 at 5/9 most of it.

## Escalations: nothing open — four ruled this cycle

**`OPEN (0)`. Nothing is waiting on Hank.** Four changed state this epoch, two by the loop's own hand:

- **ESC-011 — WITHDRAWN by the loop** (2026-09-03T20:17:07), never answered. Its repair (prefix the nine epic
  gates with `uv run python -m`) was **insufficient** — a closure audit measured a second defect it had not seen.
  Withdrawn because acting on it as written would have produced a second amendment that did not work.
- **ESC-012 — WITHDRAWN by the loop** (same timestamp), never answered. It said seven finding-tickets carry a
  placeholder gate; the **measured** number is **49 of 72 READY tickets**, so its scope was wrong by a factor of
  seven and its proposal was the wrong shape at that size. The cause is a harness defect,
  filed upstream as **`B09032015-01`** in `~/claude-build/observations/swarm-loop-HARNESS-OPEN.md`: `findings`
  should accept a verify on the finding record and seal it in the mint it already re-seals, so an unattended loop
  can dispatch its own findings without waking a human.
- **ESC-013 — RESOLVED, GRANTED by Hank**: *"Go with your best suggestions for both."* Landed as amendments 10/11.
- **ESC-014 — RESOLVED, DECIDED by Hank** under the same words: option (a), leave the frozen suite alone. Its
  subsection below is now required in every remaining report.

Filing ESC-011 unmeasured repeated amendment 8's mistake; catching it before Hank acted is the only part right.

## What merged

**Batch 1** — merge `55eaacd`, pre-batch `main` `cf5dd864`: **T-228**, **T-023**, **T-053**, **T-081**. **Batch 2**
— merge `8ba6c36`: **T-071**.

- **T-228** — the eight mypy errors that held `build_succeeds` at 0, **and a live replay-protection bypass those
  errors exposed** (below).
- **T-023** — the catalog MCP adapter, replayed against recorded contracts; it extracted `signed_fetch.to_upserts`
  into a shared `adapters/mapping.build_upserts`, so the catalog→graph mapping exists **once**. E2 6 → 7.
- **T-053** — merchant onboarding and envelope version algebra, from **0-byte packages**. An edit clears the
  approval and drops to shadow, so approve-at-20%-then-edit-to-99% cannot go live. E5 4 → 7.
- **T-071** — buyer intent clarification, also from a 0-byte package. E7 2 → 5.
- **T-081** — the dishonest-actor simulation; reproduces the approved trust trajectory to within **0.0002**. E8 6 → 7.

## Metrics

Quoted verbatim from `.swarm-loop/analysis/cycle-15.md`. Measured at `8ba6c36`.

| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |
|---|---|---|---|---|---|---|---|
| acceptance_pass_rate | converging_on_track | cycle 15 | 92.5 | 100 | 7.5 | -5.80281 | 0 |
| e7_buyer_passing | converging_on_track | cycle 15 | 5 | 9 | 4 | -0.254412 | 0 |
| e5_merchant_passing | converging_on_track | cycle 15 | 7 | 10 | 3 | -0.439706 | 0 |
| e2_ingestion_passing | converging_on_track | cycle 15 | 7 | 8 | 1 | -0.582353 | 0 |
| e8_proofs_passing | converging_on_track | cycle 15 | 7 | 8 | 1 | -0.563235 | 0 |
| acceptance_collected | at_target | cycle 15 | 120 | 120 | 0 | – | – |
| build_succeeds | at_target | cycle 15 | 1 | 1 | 0 | – | – |
| spec_criteria_passing | at_target | cycle 15 | 8 | 8 | 0 | – | – |
| e1_foundation_passing | at_target | cycle 15 | 10 | 10 | 0 | – | – |
| e3_exchange_passing | at_target | cycle 15 | 21 | 21 | 0 | – | – |
| e4_store_agent_passing | at_target | cycle 15 | 20 | 20 | 0 | – | – |
| e6_trust_passing | at_target | cycle 15 | 26 | 26 | 0 | – | – |

**Seven at target**, up from six, and nothing regressing or stalled. **The goalposts have now moved eleven times**
— amendments 10 and 11 both landed this epoch, both `tickets.json` verify fields only — and the analysis carries
its standing banner saying so: *"this trend is not against a single fixed target."* A slope fitted across sixteen
points is fitted against a moving target: steering signal, not measurement. The analysis again recomputed the
stored `error` column for five metrics whose history disagreed with the target in force.

### What `e2_ingestion_passing` and `e5_merchant_passing` do NOT mean — ESC-014, decided

**Hank decided option (a): leave the frozen suite alone.** No `freeze --amend` on `.swarm-loop/acceptance/`; cycles
0–15 stay comparable with what follows. The price is that the narrowness gets recorded instead, **in this report
and in every remaining one**:

- **The E2 seam test grades shape only.** `test_both_catalog_adapters_satisfy_one_interface` never calls
  `fetch_catalog` and never calls `to_upserts` — the lane that satisfied it said so unprompted: *two stub bodies
  would satisfy it in full.* The behaviour behind that seam is graded only by
  `services/ingest/tests/test_catalog_mcp.py`, which the same lane wrote.
- **The three E5 envelope tests have a narrow, nastier hole than first reported.** A *bare* constant
  `approval_digest` does **not** pass — measured **1 failed, 2 passed**, with
  `test_activation_requires_a_recorded_written_approval_artifact` blowing up at `test_e5_merchant.py:767` on
  `assert 'active' != 'active'`. The hole is the negative case: `test_e5_merchant.py:741-747` wraps the altered
  envelope's digest in `except Exception: other_digest = digest[::-1]`, so **an implementation that returns a
  constant for real `Envelope` objects but RAISES on a plain mapping degrades into that fallback and passes all
  three.** Proven in a sabotage copy, `__pycache__` purged between variants, with a positive control (a witness in
  `approval_digest` fired five times). Consequence: an approval signed for a 20% cap could activate a 99% cap with
  the frozen goal still green. T-053's own unit suite **does** catch it — 15 failed — so the code is verified even
  though the metric is not. **My first formulation ("a constant string satisfies every assertion") was overstated
  and was corrected by measurement**; the record carries the precise variant.

**Therefore `e2_ingestion_passing: 7/8` and `e5_merchant_passing: 7/10` must not be read as evidence those
capabilities work.** Both gaps close with product work and lane-authored tests — T-236 wires the ingestion pipeline
— not with a moved goalpost.

## Three lanes, three live defects no frozen test could see

Cycle 14's seven-for-seven held again, and this is the report's most important content.

1. **T-228 — a signed bid replayed because the envelope was read five times.** `receive_bid` accepted any `Mapping`
   and re-read it. A payload truthful on reads 1, 2, 4 and 5 and **lying on read 3** produced a correctly signed,
   canonicalized, **VERIFIED** submission whose spent nonce was `None` — the real nonce left unburned, so the
   identical signed bytes were admitted a **second time**. Repaired by reading `signer_id`/`key_id`/`nonce`
   **once** after canonicalization and proving all three are `str`, failing closed on the reason gate 2 would have
   given. Its two new tests FAIL against the pre-fix source with the exact signature `accepted=True, nonce=None`.
   The frozen suite was green throughout; only the mypy errors made anyone look at `door.py`.
2. **T-071 — a JSON string bought a live auction.** `ConfirmBody.confirmed` was declared `bool`, and pydantic
   2.13's lax mode coerces the JSON string `"yes"` to `True`. Measured, not reasoned: `POST /buyer/intent/confirm
   {"confirmed": "yes"}` returned **HTTP 201 with a live auction id and one real call to the exchange**, for a body
   carrying no boolean at all. Now `StrictBool`.
3. **T-071 — one package, two module objects, two ledgers.** The buyer intent package was importable under two
   dotted names that were **not the same module object**, so Python executed every file twice: two
   `ConfirmationWithheld` classes that do not catch each other, and **two confirmation ledgers** — so "one
   confirmation opens one auction" held only *per spelling*. Fixed (`828c75d`). The lane reports
   `buyer_svc.profile` and `buyer_svc.vault` share the duality, harmless **today** only for holding no
   module-level mutable state.

The frozen suite is the *goal* — eight previously-red tests went green here — and not the *verification*: all three
came from a lane sabotaging its own core.

**The rung-2 verifier then found both merged repairs incomplete, HIGH severity each.** (1) **T-228's replay fix
covers 3 of 6 fields.** `_work_item` re-reads **all six** identity fields, and because `payload_hash` covers only
the bid body, four of them yield an enqueued item that is internally consistent — a downstream worker re-hashing
the submission cannot detect the swap. Exploitability is currently **zero**: `receive_bid` has no production
callers. (2) **T-081's sim validates half its own ledger.** `runner.py:727` validates `raw_events` while the
auction state machine writes to a separate `ledger_sink` (`:559-560`) that is **never read** — four unreported
deviations, true count **7, not 3** — and `accepted` is emitted correctly on one path and incorrectly on the other
**in the same run**: the identical one-kind-two-bodies defect the simulation exists to catch.

## Amendments 10 and 11 — the gates, repaired and then self-corrected

Both approved by one sentence from Hank (*"Go with your best suggestions for both"*), both `tickets.json` **verify
fields only**, `git diff` exactly **10 insertions / 10 deletions, all on `verify` lines**. **Amendment 10** pointed
the nine epic gates at the **frozen acceptance tests that already grade them**, prefixed `uv run python -m`. That
closed **two** defects per gate at once: the bare `pytest` resolved to Anaconda 6.2.4 against `pyproject.toml:70`
`minversion = 8.2` and selected nothing, **and** five of the nine named a test file that does not exist. T-072 and
T-073 additionally had the `npx` half ordered first, so with no `node_modules` the pytest half never ran at all.

**T-081's old gate was worse than weak.** `pytest services/sim -q` **PASSED**, on two vacuous smoke tests, while
`services/sim/src/__init__.py` was **zero bytes** and the only source file in the package — a green gate certifying
entirely unbuilt work, the same class T-160 documents. A WEAK gate gets looked at again; a GREEN one never does.

**Amendment 11 self-corrected amendment 10 under the same approval.** Doing what ESC-013 promised — re-running
`red-check` on all ten before merging anything — stamped **T-228 WEAK**: `exit 1, but NO tests were SELECTED`.
`red-check`'s notion of a real gate is **pytest-shaped**, so a pure type-checker command can never stamp red under
it and `check-branch --ticket` vetoes on that stamp. Right about the product, wrong about the harness; repaired to
run store-agent's own suite first, then mypy.

**Retro red-check stamps against merge base `6b6040f`, all measured in bare throwaway worktrees** — not the primary
checkout, which is precisely where amendment 8 went wrong: **T-023 red (1 selected), T-053 red (3), T-071 red (3),
T-081 red (1), T-228 red (280).** Five real gates; all five check-branch vetoes cleared. T-022, T-052, T-072, T-073
and T-085 have amended gates but are not built yet.

## An orchestration defect of mine, confirmed by three lanes independently

**Every greenfield packet this cycle carried `PROXYSHOP_WORKER=0 make verify must exit 0` as a done-condition. It
is unsatisfiable from those worktrees.** `verify.sh` runs under `set -e` and dies at the mypy step on T-228's
errors — code outside all of their ownership — so **pytest and vitest never execute inside `make verify` at all**,
and the gate could not have graded their work even had it passed. A second packet claim was **also** wrong and I
propagated it in a mid-flight correction: the acceptance runner does **not** need `PROXYSHOP_WORKER` set —
`run.py` passes `--confcutdir` and runs fine unprefixed, measured with a scrubbed environment. Both errors were
mine, and both were in the packet rather than in any lane's work.

## Scheduling repair: closure derived from positive evidence

The frontier now derives closure from **positive evidence** — the frozen acceptance suite carries a per-test ticket
id, so a ticket whose tests all pass has shipped. That moved the graph from **`0 closed` to 79 closed** and put real
remaining work at the top of the frontier. `backlog.md` was regenerated from the graph **twice** this cycle; it had
drifted, stating 14/26/168 and never the real count.

## Tickets

**Closed by merge (5):** T-228, T-023, T-053, T-081, T-071.

**Minted (12):** **T-228** (the type gate), **T-229–T-234** from T-228's adversarial pass, **T-235–T-240** from the
wave's reports.

- **T-229 (CRITICAL)** — `_work_item` **still re-reads the payload**, so the validated body is not the enqueued
  body: price rewritten **89.0 → 1.0** with `accepted=True`. The same trick T-228 closed, one layer in.
- **T-235 (HIGH)** — `code_created` emits **none of its three published payload keys** on the success path, while
  the orphan path emits them correctly.
- **T-236 (HIGH)** — **no production code constructs any `CatalogAdapter`**, and the already-merged
  `SignedFetchAdapter` and `apply_upserts` are equally unreached: ingestion is not a running thing, it is a library
  nobody calls. The product half of ESC-014's E2 finding.

One LOW finding was recorded below threshold; one under a **protected path** was refused a ticket by the partition
and routed to **ESC-014**.

**Still open and NOT closed this cycle:** **T-223 (CRITICAL** — exact-equality price floor; `unit_price: 1e-09`
returns **HTTP 201 as a rankable bid**, still the highest-value single repair known**)**, **T-224 (HIGH)**, T-221,
T-222, T-225, T-226, T-227, plus all 12 new findings. **All carry the `findings` placeholder gate and cannot
dispatch until each gets a real one** (`B09032015-01`).

## `pending_remeasure: [7]` — deliberately NOT cleared

Unchanged from cycle 14 and left open on purpose: running `measure --cycle 7` today would file **today's values
under cycle 7** and corrupt the trajectory every verdict here is fitted against. It withholds `all_targets_met`, and
that is not close.

## Decisions for cycle 16

- **The frontier is T-072, T-052 and T-022** — ready, disjoint scopes, all three dispatchable now. **T-073 waits on
  T-072**; **T-085 waits on T-082, which is genuinely unbuilt** — `e2e/` has no `test_s1_flow.py`.
- **T-223 remains the highest-value single repair known**, unchanged since cycle 13, still unmade, and needing a
  reproduction test proven red at base before it can dispatch.
- **A gate that PASSES is not evidence of built work** — T-081's old gate is the run's second instance. Check what
  a green gate *selected*, not just that it was green.
- **Lane-authored sabotage stays mandatory in every packet** — seven-for-seven in cycle 14, three-for-three here.

## Harness hermeticity: PARTIAL

`verify` answers **did the frozen bytes move** — they did not; the manifest is reconciled against the append-only
`freeze-log.jsonl`, now at amendment 11. That is a real, load-bearing question, and **strictly narrower** than *can
this measurement be gamed*.

The structural residual is unchanged and still open: **product code imported by the acceptance suite runs in the
scorer's own process and can tamper with the scorer's in-process state.** That is inherent to any in-process
black-box suite; it is not hardened away, only *moved*, by a subprocess-per-test design this one does not pay for.
Hermeticity is **PARTIAL**. It has never been sealed, it is not sealed now, and no line in this report should be
read as saying otherwise. ESC-014 is that residual made concrete: two frozen tests grade less than their metric
names imply, and the replay bypass in `door.py` was live while the frozen suite was green throughout.

The vocabulary trap in the artifact persists: `.swarm-loop/analysis/cycle-15.json` records `harness_integrity:
"intact"`. That field is the **byte-equality verdict** — the narrow question — not a statement about hermeticity.

## Open, recorded, not fixed

Everything cycle 14 recorded under this heading stands except where this cycle says otherwise. **The rung-2
cross-branch interaction lens for the cycle-13 batch is still owed**, now carried for a third epoch — T-214's lock ×
T-216's shared-database handling × T-206's ledger replay, in one process, against post-wave `main`; no green in
this report covers it. ESC-008's stale `scripts/verify.sh:179-186` paragraph stands by ruling, known-false.

---

<sub>Cycle 15 · epoch `b255f31..8ba6c36` (34 commits) · measured at `8ba6c36` ·
written against swarm-loop skill `3527702`.</sub>
