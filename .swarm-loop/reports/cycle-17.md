# Cycle 17 — 12/12 at target, and a survey that says the system does not run

Measured at `7b5d1f0` · acceptance **120/120 (100.00%)**, from 119/120 (99.17%) · **12/12 metrics `at_target`** ·
harness hermeticity **PARTIAL** (frozen bytes intact, amendment 17) · codebase stall 0/3 · goal-progress 0/3 ·
tracked files 715 → 733 · epoch `c9e5d5f..20ba0da`, 49 commits, of which **18 landed after the measurement and
are in no number on this board.**

`all_targets_met` is nonetheless recorded **`false`**, and that is not a rounding artifact: a `freeze --amend`
moved a target whose baseline was last measured at **cycle 7**, and `measure --cycle 7` has never been re-run.
The analyzer withholds the flag until it is. 12/12 is the per-metric verdict; the run-level flag is still owed a
re-measure.

## 1. OPEN ESCALATION — ESC-018, waiting on you

This is the one part of this epoch that is not waiting on the swarm.

- **ID / kind** — `ESC-018`, `frozen-test`, filed 2026-09-04T12:17 at HEAD `ecb9d6a`, **blocking: false**.
- **Subject** — `.swarm-loop/acceptance/test_e8_proofs.py:677` certifies a demo runbook whose two headline
  commands both fail.
- **Reason (measured, both halves)** — `e8_proofs` reads 8/8 and acceptance reads 120/120 while sections 3 and 4
  of `docs/demo/starting-slice.md` — the part an operator would run in front of an audience — have no executable
  behind them. (1) `starting-slice.md:189` instructs `uv run python -m pytest e2e/test_s1_flow.py -q`; that file
  is **not on main** (pytest exits 4, "file or directory not found") — it exists only on the unmerged T-082
  branches, and T-082 was rejected this epoch on its merits. (2) `starting-slice.md:228` instructs
  `make e2e-live`; `docs/demo/e2e_live.sh` **does not exist** (exit 127) — the Makefile target is real, the
  script was never written. The gate misses both because lines 677-692 check only that the make target is
  **defined in the Makefile** — never that its script exists, never that any instructed command resolves. Worse:
  `make check` already prints "7 ticket verify path(s) not created yet:" and names `T-082 e2e/test_s1_flow.py`.
  The repo says the file is missing on every run, and the frozen suite certifies the runbook regardless.
- **Proposed change** — amend `test_e8_proofs.py`'s runbook test to grade **executability, not shape**. Two
  static additions, no subprocess, no network, no daemon: (1) for every `make <target>` the runbook instructs,
  resolve it in the Makefile and assert every script or file its recipe names **exists** (this alone catches
  `make e2e-live` → `docs/demo/e2e_live.sh`); (2) for every path a runbook command names as a pytest argument or
  script path, assert the path exists in the tree (this catches `e2e/test_s1_flow.py`). Explicitly **not**
  proposed: executing the runbook — that needs Docker and would make the frozen suite non-hermetic.
- **The sequencing you are being asked to rule on** — this takes `e8_proofs` 8/8 → **7/8** and acceptance
  120/120 → **119/120 the moment it lands**, and holds red until either T-082 is rebuilt or the runbook stops
  naming what does not exist. **(a)** land the gate first and accept a red board — the honest ordering and the
  filer's recommendation; **(b)** write `e2e_live.sh` and rebuild T-082 first, so the board never goes red but
  the gap stays uncaught meanwhile; **(c)** land the gate and delete the two unrunnable commands from the
  runbook — keeps the board green, and is the option the filer likes least, because it settles a
  documentation-versus-reality conflict by deleting the documentation's ambition.
- **The exact commands that close it** (a protected path, so it is yours either way) — edit
  `.swarm-loop/acceptance/test_e8_proofs.py` first, then re-lock around the new bytes:

```
export PROXYSHOP_WORKER=0 && .venv/bin/python ~/.claude/skills/swarm-loop/scripts/swarmloop.py \
  freeze --amend "<why you approved this harness change>"
export PROXYSHOP_WORKER=0 && .venv/bin/python ~/.claude/skills/swarm-loop/scripts/swarmloop.py \
  escalate --resolve ESC-018 --note "<what you decided>"
```

No urgency: the defect is that a green number overstates what works, and the run is already stopped.

## 2. Tickets closed this epoch

Twelve accepted, one rejected — every one a dispatch-ledger `close` record carrying a verdict.

- **T-250** (02:04) — exact-equality price floor ported into the contracts boundary, both language peers.
  Verified **through the exchange path**, not only at the boundary: with an uncapped row 99.0 / 80.0 / 1.0 still
  admit (the three R10 controls the reproduction insisted survive), while 0.001 and 1e-09 are refused with
  `price_unreconcilable:offer.unit_price:below_price_floor`.
- **T-229, T-230, T-231, T-232, T-234, T-241** (02:12, one lane) — store-agent door hardening, merged at
  `48d0510`. A collection agent audited all 152 removed lines: zero assertions weakened, zero expected values
  changed, all 48 test-side removals were `xfail` marker blocks; the two source deletions that removed working
  paths both removed **fail-open defaults**.
- **T-235** (02:12, T-158 lane) — `build_published_event` now validates at the producing boundary; `expires_at`
  was moved verbatim, not weakened, old keys retained so every consumer survives (confirmed against the sim's
  byte-for-byte determinism).
- **T-085** (02:20) — the starting-slice demo runbook at `7b5d1f0`, deliberately separated from the held T-082.
  Gate discriminates both directions (merge base `c1a650f`: "1 failed, 7 deselected"; branch: "1 passed"). This
  close took `e8_proofs` to 8/8 — and is the one ESC-018 says was certified on shape.
- **T-233** (04:48) — **the headline close, and it required amendment 17.** An omitted `trust_snapshot` was more
  permissive than an explicit empty one, and the frozen suite *asserted the permissive side*. See §7.
- **T-082 — REJECTED** (02:21) to the backlog as unfinished WIP; branch `task/T-082` and its worktree preserved
  as the retry packet's input. Three reasons, none of them the gate: salvaged WIP from a lane that died on a
  session rate limit and never ran its own gates against these bytes; `e2e/test_s1_flow.py` carries three ruff
  I001 errors that would take `build_succeeds` to 0; and two sabotage-proven blind spots (replacing the HMAC
  verify with `lambda: True`, or `mint_code` with a constant, each left all 23 of its tests passing).

**A ledger gap worth recording:** **T-169, T-170, T-204, T-264 and T-278** all landed on `main` this epoch
(`5592fff`, `be8fb5a`, `8d3f3ff`, `330eb13`, merged at `65e647a`/`af2c775` and `b5c1bf6`/`2563203`) with **no
`open` and no `close` record in `dispatch.jsonl` at all.** The push gate scores `rung2-returned` off that ledger;
work that never enters it passes that rung vacuously. Cycle 16 recorded the same hazard from the other side.

## 3. Tickets minted this epoch — 18, in three distinct sweeps

**T-298..T-301 — the artifact-blindness class** (`d8d76f7`, freeze-log #32). Nothing in this repo grades the
deployable **artifacts**, only the checkout. Of five shipped images: one **cannot start** (T-298, CRITICAL —
`apps/merchant/Dockerfile` never COPYs the exchange package that `svc/src/codes/offer.py:36` imports eagerly at
module scope); one **starts crippled** (T-299, HIGH — exchange image missing ingest, 11 modules unimportable,
ranking and retrieval dead); one **runs a feature permanently degraded** (T-300, HIGH — `packages/llm` absent
from the buyer image, lazily imported *and* swallowed, so no runtime probe at any depth can see it); T-301 (HIGH)
specifies the missing gate. Confirmed by two independent methods sharing no assumptions — a runtime probe over
each Dockerfile's own COPY set, and a static import-vs-COPY-set check — whose results agree through a transitive
closure neither computed for the other. Exactly one image had such a test already (trust, via T-193's repaired
probe), and **the four unguarded images are where every defect is.** All true at 120/120 with `make verify` green.

**T-302..T-305 — the trust-lane residue** (same commit, freeze-log #33): three frozen ledger kinds reserved in
all four vocabularies and emitted by no product code (T-302); T-237 half-closed with its remaining halves in
other lanes' scopes (T-303); a fixtures path that resolves correctly in the checkout and wrongly in the image
(T-304); four comments made false by this cycle's own fixes, self-reported by the lane that invalidated them
(T-305).

**T-306, T-307 — the T-233 lane's sweep** (`ecb9d6a`, freeze-log #35, minted at the ESC-017 merge `3d366c0`).
Both HIGH, both at `packages/contracts/src/boundary.py:637`, both found by looking one argument over from the
defect just fixed. **T-306: `list_prices` is the identical fail-open to T-233, and it is on the money path** — a
signed bid awarding itself 85% off is accepted when the roster is omitted. **T-307: `max_discount_pct` supplied
without `list_prices` is never read**, so a caller that sets a 0% ceiling and forgets the roster silently admits
an 85%-off bid.

**T-308..T-315 — the demo-readiness survey** (`20ba0da`, freeze-log #36), an operator survey of what is actually
live: **T-308** (HIGH) no observability at all — a repo-wide grep for `basicConfig|dictConfig|FileHandler|structlog`
returns nothing, the root logger has no handlers, every INFO call is discarded, and six of eight services contain
zero logging statements; **T-309** (HIGH) the store agent serves **zero** HTTP paths — `app.openapi()['paths']`
empty, no `routes.py` anywhere; **T-310** (HIGH) the ranker is orphaned, nothing in `apps/exchange/src` imports
`exchange.ranking`; **T-311** (MEDIUM) neither UI is runnable — no `scripts` block, no host, no dev server, no
entry point in either `package.json`; **T-312** (MEDIUM) seven published routes unserved across three services,
measured by constructing each app; **T-313** (MEDIUM) four components have no Dockerfile at all — ingest,
store-agent, sim, seller-reference; **T-314** (MEDIUM) the one fully-working service's image has never been
built, and the runbook's step 1 tells you to run exactly that; **T-315** (LOW) `DESIGN.md:145` promises two
runbooks in `docs/demo/` and the directory holds one. The survey's ninth finding landed as **ESC-018** rather
than a ticket, because it falls under a protected path.

## 4. The analysis table

| metric | baseline (c0) | cycle 16 | **cycle 17** | target | verdict |
|---|---|---|---|---|---|
| `acceptance_pass_rate` | 3.33 | 99.17 | **100.0** | 100 | `at_target` |
| `acceptance_collected` | 120 | 120 | **120** | 120 | `at_target` |
| `build_succeeds` | 1 | 1 | **1** | 1 | `at_target` |
| `spec_criteria_passing` | 4 | 8 | **8** | 8 | `at_target` |
| `e1_foundation_passing` | 0 | 10 | **10** | 10 | `at_target` |
| `e2_ingestion_passing` | 0 | 8 | **8** | 8 | `at_target` |
| `e3_exchange_passing` | 0 | 21 | **21** | 21 | `at_target` |
| `e4_store_agent_passing` | 0 | 20 | **20** | 20 | `at_target` |
| `e5_merchant_passing` | 0 | 10 | **10** | 10 | `at_target` |
| `e6_trust_passing` | 0 | 26 | **26** | 26 | `at_target` |
| `e7_buyer_passing` | 0 | 9 | **9** | 9 | `at_target` |
| `e8_proofs_passing` | 0 | 7 | **8** | 8 | `at_target` |

Every error is 0. No slope, r², or projection is reported — the analyzer fits only metrics still carrying error,
and none do. The one movement this epoch is `e8_proofs_passing` 7 → 8 (T-085's runbook), which carried
`acceptance_pass_rate` 99.17 → 100.0. Everything else held.

Three qualifications the numbers do not carry on their face:

- **`all_targets_met: false`** — the re-measure owed for **cycle 7**; only `measure --cycle 7` satisfies it.
- **Selection tracking is UNAVAILABLE for all twelve metrics.** Every "no selection regression" in every analysis
  to date means **NOT MEASURED**, not clean: `measure` records selected/deselected only when the metric command's
  stdout is pytest-shaped, and a correct frozen wrapper prints a bare number, so both columns are empty at every
  cycle including the cycle-0 baseline. The deselection backstop is the only one of three enforcement points that
  can see a filter added *after* the freeze. Carried unrepaired for a third epoch.
- **The stored `error` column disagreed with the current target** for `acceptance_collected`, `e3_exchange`,
  `e4_store_agent`, `e6_trust` and `e8_proofs`; every error above is recomputed from `value` against the target
  in force now, never read from history.

## 5. The credibility caveat — read this before quoting 12/12

**All twelve frozen metrics read at target, acceptance is 120/120, and an independent operator survey run in the
same epoch found the deployable system largely non-functional.** Both are measured, both are true, and together
they mean the frozen board is answering a narrower question than its labels suggest. At the same commit that
scores 120/120:

- the **merchant image cannot start at all** (T-298);
- **four components have no Dockerfile** — ingest, store-agent, sim, seller-reference (T-313);
- the **store agent serves zero HTTP paths** (T-309);
- **nothing on any served path imports the ranker** (T-310);
- **neither UI has a `scripts` block or an entry point** (T-311);
- **nothing anywhere configures logging**, so every INFO call is silently discarded (T-308);
- the exchange image starts and cannot rank or retrieve (T-299); the buyer's clarification loop runs model-less
  in the shipped image (T-300); seven published routes are unserved (T-312); the one fully-working service's
  image has never been built (T-314); the demo runbook's two headline commands both fail (ESC-018).

**12/12 green means "built as the acceptance tests call it." It has never meant "delivered."** The suite imports
product code and exercises it in-process from a checkout with every package on `sys.path`. It never builds an
image, starts a service, resolves a route, runs a UI, or executes the runbook. Every gap above is invisible to it
*by construction* — a category the suite structurally cannot enter, not an oversight in any one test (T-301).

The gaps are encoded, not merely noticed. **T-298..T-301** encode artifact-blindness (merchant unstartable,
exchange crippled, buyer degraded, plus the missing gate). **T-302..T-305** encode the trust-lane residue and the
checkout-versus-image path divergence. **T-306..T-307** encode the money-path fail-open. **T-308..T-315** encode
observability, the store agent's empty router, the orphaned ranker, both unrunnable UIs, the unserved routes, the
four missing Dockerfiles, the never-built image, and the missing second runbook. **ESC-018** encodes the runbook
gate itself, and is the only one of them that needs your decision.

## 6. Harness hermeticity is PARTIAL — it is not sealed, and nothing here says it is

`.swarm-loop/analysis/cycle-17.json` records `harness_integrity: "intact"`. **That field is the byte-equality
verdict — the narrow question — and it is not a statement about hermeticity.** All frozen files re-hashed clean
against `manifest.json`, itself reconciled against the append-only `freeze-log.jsonl`. That is all it says.

**The residual, stated plainly: `verify` answers only "did the frozen bytes move." Product code imported by the
acceptance suite runs inside the scorer's own process and can tamper with the scorer's in-process state.** Nothing
in the harness detects that. Cycle 16's `_proxyshop.pth` finding is the proof by example — a file not frozen, not
in the manifest, not in git, that put the checkout on `sys.path` at interpreter startup in a way that survived a
scrubbed `PYTHONPATH`, an empty cwd, and `-I`, and made a reproduction test report RESOLVED on its first run. No
amount of byte-equality checking on `.swarm-loop/` could have seen it; a lane instructed to sabotage its own core
found it, and nothing else did.

Hermeticity has never been sealed, it is not sealed now, and **no line here says otherwise.**

## 7. Amendment 17 — the frozen suite stopped mandating a fail-open

- **What moved:** exactly one file, `.swarm-loop/acceptance/test_e4_store_agent.py`.
- **Hash:** `ad27deb55be16392f5ead5721c88793ff371ca98baf5efc0fac1bc16824dcecf` →
  `0b4e92d0fc3ea1baf2d9a38053f583ee37fce520dcd661beb96b49e0cfdd5fb3` (freeze-log seq 34, sealed
  2026-09-04T04:46:52 at HEAD `3d366c0`).
- **Why:** the frozen suite **asserted the permissive side of a security fail-open**, so T-233 could not be closed
  by any lane. Measured on the suite's own payload helper against the unmodified door: `trust_snapshot` omitted →
  `accepted=True`; `{}` → `accepted=False reasons=('trust_snapshot_unavailable:store-external-1',)`; `None` →
  `accepted=True`. Line 1180 asserted `accepted is True` on exactly that call. Applying the T-233 fix against the
  unamended suite gave 7 failed / 13 passed, taking `e4_store_agent_passing` from 20 to 13. Four escape routes
  were implemented and measured dead; a fifth was hunted for and not found. `tickets.json` is itself frozen, so
  even re-pointing T-233's gate would have been an amendment — **there was no do-nothing option.**
- **What changed:** 35 insertions, **zero deletions, zero assertions touched** — one helper deriving a populated
  trust snapshot from each payload's own `store_id`, plus one argument line in `_present` (the choke point for
  all 21 `_accepts`/`_rejects` calls) and one on the single direct `receive_bid` call. Two edit sites, not
  fourteen. Count stays 120 and E4 stays 20 before and after, measured with real pytest.
- **Granted by:** Hank, by explicit delegation ("the three things you were waiting on me for, I'll take your best
  judgment on"), ruled by the orchestrator, executed by a lane, verified by three independent readers — who also
  refuted three countable claims in the orchestrator's own ruling while the substance survived.

**Standing warning, and it applies to every comparison above:** the goalposts have moved **17 times**. Any metric
comparison crossing an amendment boundary must say so. Cycle 16 → 17 crosses amendment 17, which touched a frozen
E4 acceptance file — E4 held at 20/20 across it by measurement, but the comparison is not against a single fixed
target and must never be presented as one.

## 8. Decisions for the next epoch — the orchestrator's priority order

1. **T-298..T-301 — artifact-blindness. Build the static import-vs-COPY-set gate first.** The largest credibility
   gap on the board, and the only one whose fix also fixes the *class*: a gate that grades what is shipped rather
   than what is checked out. Static check primary, runtime probe secondary and explicitly **not** redundant
   (T-301). One design question is recorded unresolved rather than assumed — whether ingest belongs in the
   exchange image at all — and it must be answered inside this work.
2. **T-306 / T-307 — the `list_prices` fail-open on the money path.** Same shape as the T-233 defect just closed,
   one argument over, and it admits an 85%-off signed bid. Highest severity per line of fix on the board.
3. **T-308 — observability.** Nothing configures logging anywhere, so every diagnostic the next epoch would want
   is currently discarded. Doing this early makes items 1 and 4 cheaper to debug.
4. **T-309 / T-310 — the store agent and the ranker have no served path.** Two components with tests, no route,
   no importer; both load-bearing for the demo narrative.

**ESC-018 is not in this order because it is not the swarm's to sequence.** It wants a ruling on (a) / (b) / (c);
option (a) — land the gate first and accept 119/120 — is the filer's recommendation and the honest ordering.

<sub>Cycle 17 · epoch `c9e5d5f..20ba0da`, 49 commits · **measured at `7b5d1f0`**, 31 commits into the range —
the last 18, including T-169/T-170/T-204/T-264/T-278 and the T-233 close, are in **no number above** · frozen
bytes re-verified clean at amendment 17 · goal amendments 17 · escalations 1 open / 17 resolved · written
against `main` at `20ba0da`, working tree untouched.</sub>
