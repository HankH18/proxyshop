# Cycle 18 — the deployable system starts, and the ticket-VERIFY tier is still mostly ungraded

Measured at `4e2fe2d` · acceptance **120/120 (100.00%)** · **12/12 metrics `at_target`** · `all_targets_met` still
**`false`** · harness hermeticity **PARTIAL** · goal amendments **19** · escalations **0 open / 19 resolved** · graph 263 →
**287 tickets**, ledger-closed 96 → **148**, READY **140**, blocked 31 → **2** · epoch `20ba0da..e03eff0`, **68 commits**, 8
lanes merged · 55 findings recorded (1 CRITICAL, 22 HIGH, 23 MEDIUM, 9 LOW).

The headline is not a metric. Every one of the twelve numbers read at target through cycle 17 while three of five shipped
images could not start; this epoch made the images start and made the demo runbook's commands resolve, and **no frozen
number moved** — because no frozen number was ever watching either thing.

## 1. Escalations — nothing is waiting on you

`escalate --list` reports **no open escalations**. Both escalations of this epoch were filed and resolved inside it, and
there is no outstanding ask.

- **ESC-018** (`frozen-test`, filed at `ecb9d6a`, resolved 12:48) — `test_e8_proofs.py:677` certifies a demo runbook whose
  two headline commands both fail (`e2e/test_s1_flow.py` absent → pytest exit 4; `docs/demo/e2e_live.sh` absent → exit 127).
  **Hank's ruling: do NOT amend the frozen test now.** Sequence it the other way — (1) land the executability check in the
  **non-frozen** tier, which needs no amendment and takes no metric red; (2) make the runbook's claims TRUE, which dissolves
  the failure at its source; (3) only then revisit amending the frozen gate. The resolution explicitly deferred rather than
  answered step (3), and required a **fresh escalation** for it.
- **ESC-019** (`frozen-test`, resolved 16:46) — filed because step (3) had arrived. **GRANTED by Hank** ("Cool, yeah, then
  do it."). The amendment grades executability rather than shape in the frozen E8 test. The resolve note records the reading
  explicitly, because a peer message about a *different* amendment landed in the same window: this grant covers
  `test_e8_proofs.py` only, and is **not** the authority under which the separate 130-ticket verify-field amendment proceeds
  (that one runs on ESC-013's standing grant). Cost measured to zero before asking: 17/17 runbook commands pass,
  `e8_proofs_passing` stays 8, acceptance stays 120/120 — no board taken red, which is the sequencing ESC-018 ruled on.

## 2. What landed — 8 lanes, and the system now starts

**`task/C19-images` → `cf07eab` — the deployable system starts.** Broken modules per image, before → after, measured by
importing **inside real running containers**, not by the gate's model of them:

| image | before | after |
|---|---|---|
| exchange | 12/68 | **0/79** |
| merchant | 8/63 | **0/79** |
| buyer | 1/60 | **0/67** |
| trust | 1/64 | **0/64** |

Gate now grades **9 images, 511 modules, 0 broken**, and all **9 `docker build` exit 0**. Inside the running containers
first-party packages resolve under `/app/.pkgroot/` — which is what proves the shipped tree rather than a checkout: merchant
resolves `exchange` with `ingest` **absent**; exchange resolves `ingest`; buyer resolves `llm` and `build_llm("buyer")`
returns `DeterministicLLM` with `anthropic` **not imported**; numpy/scipy/torch/neo4j/bs4/lxml/anthropic/mcp absent from
every image. All seven gates were **red at the merge base** (`red-check --close` says "red — a real gate" for each at
`af9ea48`) — this run's first complete ticket lifecycle: gate written, red at base, fixed, green. Two decisions where the
obvious repair was wrong: T-298 took a **narrow** copy (the in-repo precedent would have dragged `ingest` into merchant,
converting T-298 into T-299), and T-300 fixed the **COPY set, not `routes.py`**, though its scope named the code. An eighth
defect had to be fixed for T-301 to go green: a column-0 `import pytest` in `proxyshop_support/fixture_loader.py`, shipped
into every image, none of which carries pytest.

**`task/C19-e2e` → `d03ffa7`, re-merged `fdb14c5` — the runbook's claims became true.** Executability gate **17 commands /
13 PASS 4 FAIL → 17 / 17 PASS, zero xfail**; `e2e/test_s1_flow.py` 27 tests / 27 passed in 1.2–1.5s; `make e2e-live`
resolves with four exit statements audited and **none of them zero**; `make verify` `OK: all` — 5477 py passed, 0 skipped,
817 vitest, ruff/mypy clean. The T-082 rejection was **reproduced before anything was claimed**: replacing the HMAC verify
with a constant, or `mint_code` with a constant, still leaves the rejected draft green — **and still leaves current main
green** — with probes showing the patched functions ran 3× and 1× inside the real flow. On the new suite **9/9 sabotages
caught**, zero hangs, zero expiries. `make e2e-live` deliberately **always refuses**: a live dev-store run needs a Shopify
account, so the honest artifact is a preflight that reports five preconditions — the sharpest being that `SHOPIFY_STUB_URL`
silently redirects "live" Admin calls to `localhost:8787`, which is how a live run is quietly not live.

**`task/C18-trust` → `350f6de` (T-154, T-167, T-193, T-237).** Trust fixtures now connect as `trust_rw`, the role the ledger
writer actually ships as (the old `app` role was never a conservative subset — migration 0004 grants it tables the writer
cannot touch while withholding the DELETE the writer has). Four byte-identical 7935 B shims (md5 `56d63d33…`, empty diff)
collapse to one. 581 deletions all read: 489 are the duplicate shims; of the remaining 92, **zero are executable guards**.
Recorded cost, not glossed: the suite no longer has negative-privilege coverage on the ledger table; append-only is now
enforced by trigger, not by grant.

**`task/C19-served` → `6c9964d` (T-309/T-310/T-312 gates).** Three `xfail(strict=True)` repro gates, 955 lines added, **zero
deletions**. Each is a general property — construct the app, read `app.openapi()['paths']`, read the published contract,
require agreement in **both** directions (the divergence runs both ways for four of five services). Controls on scratch
copies: T-309 flips green when a route reaching `receive_bid` is added; T-312 flips green by editing **only the contract**,
proving it grades both sides. The lane caught its own arming defect: four probe checks sat inside a `strict=True` body where
any exception reads green.

**`task/C19-artifact` → `a420285`** landed the static import-vs-COPY-set checker (1672 lines) and the runtime
container-shaped import probe that `cf07eab` then turned green. **`task/C19-observ` → `f24b02f`** landed T-308's behavioural
blackout property — a sentinel must be *delivered* to a real sink after all 7 ASGI factories are built and their lifespans
run. **`task/C19-boundary` → `ab7ec54`, then `9d6bcea`** — see §7. **`task/C19-runbook` → `a4ce515`, hardened at `e57268e`**
over four adversarial rounds: 27-case battery, zero unexpected — nine parser bypasses, six recipe-internal, four
Makefile-shape and three artifact stubs (0-byte `.sh`, comment-only `.sh`, empty test file) stay red; ten legitimate
spellings stay at baseline. The author's own falsified premise is worth keeping: careful markdown parsing was **not** what
armed the sweep — every shape-enumeration rule became a bypass and a false positive simultaneously; cross-checking two
independent parses is what holds. (`e57268e` was itself later found weaker than what it replaced — §7.)

Also landed: `5f1822e` fixed D39, where `redis_db_index = worker % REDIS_DB_COUNT` **collided instead of isolating** —
measured on the live cluster, **37 of 53** `proxyshop_w*` databases carry an index ≥ 16, and `conftest`'s per-test
`flushdb()` ignores the `w{N}:` key prefix, so colliding workers wiped each other's Redis state at every test boundary.

## 3. Tickets — and a bookkeeping correction I made against myself

- Graph **263 → 287** tickets (24 minted from findings, freeze-log seq 41–46).
- **Ledger-closed 96 → 148.** Blocked **31 → 2**. READY **140**.
- Placeholder gates (`false  # NO GATE YET`) in `tickets.json`: **158 → 146**, measured at both endpoints. Amendments 18 and
  19 cleared 37 of them; **the 24 newly-minted tickets put most of it back**, because findings kept minting faster than
  gates were written. The count fell by 12 net across an epoch that wrote 37 gates.
- `tickets.json`'s own `status` field still reads **96 closed at both endpoints** — it is a frozen file, so status cannot be
  edited without an amendment, and every closure count above is derived from the append-only dispatch ledger instead.
  `frontier` states the caveat plainly: those 148 are closed on an **operator-recorded verdict, not on anything
  re-verified**, and a record naming a branch that never existed closes its ticket exactly like a real one.
- The 35 false-open closures at `8c7ec0e` (T-010..T-130) were **not** taken on paper: each ticket's own declared verify was
  re-run at main on an isolated database, all 35 exit 0 with real selection, 26 to 5465 tests each, no vacuous pass. T-054
  was held open deliberately (its gate is vacuous by path construction), as were the 7 whose verify names a file that does
  not exist.

**THE CORRECTION.** Six tickets — **T-306, T-307, T-308, T-309, T-310, T-312** — were briefly recorded **CLOSED in error by
the orchestrator**. Three gate lanes were closed with `--ticket <the tickets they gated> --verdict accepted`; those lanes
wrote **gates only**, by their own briefs, and the closing notes said so *in prose* ("the TICKETS remain OPEN as fix work").
The prose is not what the ledger reads. The machine-readable field closed them, so six tickets with live defects were
recorded done. Found by an **independent sweep, not by me**. Verified before reversing: all six gates are RED at HEAD (T-306
"1 failed", T-309 "1 failed", T-312 "3 failed"), and under `--runxfail` a FAILED gate means the defect is still live.
Reopened via the only mechanism that reopens a ticket — a rejected verdict — at `7d18e90`; **closed 154 → 148, READY 122 →
128**. This is the run's own recurring failure shape turned on my own records: the state described accurately in a
human-readable note, and the opposite recorded in the field that is actually read again.

The same sweep **refuted** the original T-204 hypothesis, measured rather than assumed: no closed ticket has a failing
*unselected* grader node — 189 unselected nodes across 60 flagged tickets, 182 passed, 7 failed, 0 errored, 0 skipped, and
all 88 nodes a closed ticket actually claims pass. The 7 failures are other tickets' xfail latches, each selected by the
gate that owns it.

## 4. Amendments 18 and 19 — 37 verify fields, all measured in a bare throwaway worktree

Both are ESC-013's approved class (`tickets.json` verify fields only), both sealed at head `af9ea48`. Combined git diff
across the two: **37 insertions / 37 deletions, zero changed lines that are not a `verify` line** (measured; a first attempt
that re-encoded the whole file to 497/497 was reverted rather than shipped).

- **Amendment 18** (seq 39, 14:40:10) — 13 tickets (T-298..T-301, T-304, T-306..T-310, T-312..T-314) that all carried the
  placeholder and were undispatchable. All 13 measured in a **bare throwaway worktree of main — no `.venv`, no
  `node_modules`, `PROXYSHOP_WORKER` unset**, which is what `red-check` actually experiences, and **not** in the primary
  checkout, which is precisely where amendment 8 went wrong. All 13 exit 1, each selects 1–3 tests, each fails on an
  `AssertionError` **raised inside the gate's own body** naming that ticket's subject. No collection errors, no skips, no
  import-file-mismatch. Slowest 12s wall / 4.72s pytest. Two controls proved the measurement itself real: dropping the
  `export` makes conftest hard-abort with "PROXYSHOP_WORKER is unset", and a `-k` matching nothing gives rc=5 with
  everything deselected — so the WEAK verdict is detectable and none of the 13 hit it.
- **Amendment 19** (seq 40, 14:45:30) — 24 tickets: **23 repoints** at gates that already existed and nobody had pointed
  them at, plus **one widening**. No new test was written for any of them. Measured **twice**, stable: 24 of 24 rc=1,
  exactly 1 selected and 1 failed, under 12s, no Postgres. Zero came back green, so none is a closure candidate. Three
  corrections the measurement forced that no paper review would have caught: 15 of 24 candidate commands named
  `.venv/bin/pytest` (absent in the bare worktree), one used the prefix-assignment form that does not survive `&&`, several
  `-k` substrings over-selected; and T-296's recorded `target_test_file` named the wrong package. **T-297 was refused a
  repoint and stays gateless, deliberately** — its candidate test is *T-204's* gate, and repointing would have dispatched a
  lane to do another ticket's work. **T-204's widening** came out of that refusal: its verify named only the exchange-side
  node, which now passes, while its contracts-side half is still red — a closed ticket's gate answering green on a half-red
  gate. Two incompletenesses recorded rather than glossed: T-266's gate cannot be turned green by a T-266 lane alone, and
  T-296's node covers 3 of the 8 operations it records.

**`--runxfail` is load-bearing in BOTH directions and must not be tidied away.** All these gates are
`@pytest.mark.xfail(strict=True)` with long multi-line decorators (a naive grep reports zero; an AST parse was needed).
Measured on T-309 in the bare worktree: *with* `--runxfail`, "1 failed, 1 deselected", rc=1, gate red and correct; **without
it, "1 deselected, 1 xfailed", rc=0, gate silently green.** And after a fix lands the flag is still required, because a
strict-xfail test that starts passing reports XPASS(strict) → FAILED without it, so a green-check would go red on a
*successful* repair.

## 5. Credibility — 12/12 is the ACCEPTANCE tier and says nothing about the ticket-VERIFY tier

All twelve metrics read `at_target` at cycle 18, and `analyze` **correctly WITHHELD** `all_targets_met`: a `freeze --amend`
moved a target whose baseline was last measured at **cycle 7**, and `measure --cycle 7` has never been re-run. An amendment
invalidates its own baseline, and a hand-entered `record` does not satisfy it. The flag is honest and the debt is real.

**And 12/12 is a statement about one tier only.** The frozen acceptance suite is the scorer. It is not what ticket gates
run, and the two are nearly disjoint facts:

- `graded_ticket_fraction` is **13.6%** — 37 of 273 tickets carry ≥1 frozen test; **236 tickets are graded by nobody but
  their own author.**
- **Only 16 of 141 ticket gates reach the frozen acceptance suite.** All 120 `@pytest.mark.ticket(...)` nodes live under
  `.swarm-loop/acceptance/`, which `norecursedirs` hides from every ordinary pytest run — `make verify` collects 2822 of
  2943 nodes and **zero** acceptance nodes. The exclusion is *correct and deliberate* (mixing them would let the graded tree
  grade itself); the defect is the consequence nobody stated — the markers `graded_ticket_fraction` counts live in a tier
  ticket gates almost never execute, so the two tiers can drift arbitrarily far apart with no signal.
- **44 of 141 gated tickets have no traceable grader anywhere.**
- **47 of the 151 closed tickets carry `false  # NO GATE YET`** — closed with no executable gate at all, so "closed" rests
  entirely on an operator's recorded verdict with **nothing re-runnable behind it**.
- **Three closed tickets have a RED gate at HEAD: T-158, T-204, T-212.** Measured by running every closed ticket's recorded
  verify verbatim — 104 closed tickets carry a real gate, 9 came back red, 6 of those were the bookkeeping error of §3, and
  these three are not. All three already carry `--runxfail`, so no added flag manufactured the red. T-204's is
  client-facing: `denial_reason` is persisted and client-visible with an open-ended published vocabulary. **NOT MEASURED,
  and it changes what this means:** whether each gate was red *at the time the ticket was closed*, or went red afterwards
  through later merges. That needs a per-ticket bisect against each closure timestamp. Until someone does it, these are
  **either three premature closures or three regressions nothing was watching**, and nobody will look again.
- **Selection tracking is unavailable for all 12 metrics.** `measure` records selected/deselected only when the metric
  command's stdout is pytest-shaped, and a correctly-authored frozen wrapper prints a bare number — so both columns are
  written empty. Read every "no selection regression" as **NOT MEASURED**, never as clean. A missing token means UNKNOWN,
  not zero. This is the only one of three enforcement points that can see a filter added *after* the freeze, and this run
  has two such filters.

## 6. Harness hermeticity — PARTIAL, and this epoch added a specific residual

Frozen bytes re-hashed clean against `manifest.json`, itself reconciled against the append-only `freeze-log.jsonl`. **That
is the whole of what `verify` says.** The standing residual is unchanged: `verify` answers only *"did the frozen bytes
move"*, and product code imported by the acceptance suite runs **inside the scorer's own process**, where it can tamper with
the scorer's in-process state. Nothing in the harness detects that.

**This epoch's new residual, and it is concrete.** The frozen metric `build_succeeds` runs as `if PROXYSHOP_WORKER=0 make
verify …`, and at HEAD **130 ticket `verify` strings pin `PROXYSHOP_WORKER=0`** (measured: 130 of the 141 non-placeholder
gates; the other 11 pin no worker at all). Postgres and Redis are per-worker, so a ticket gate running while a measurement is in flight
**shares the scorer's database and corrupts a scored metric**. This is not hypothetical — the same collision was observed on
lanes this epoch (§7). An amendment moving all 130 to worker 15 exists **uncommitted in the working tree** (130 insertions /
130 deletions, zero non-`verify` lines changed, verified) and is **pending a validation pass**; the sealed manifest hash
still matches the HEAD bytes, so the frozen file at `e03eff0` is intact and the pending edit is outside the seal.

## 7. Process-loop findings — the most transferable output of this epoch

**A randomized property with a PINNED seed is a parametrized probe wearing a costume.** The T-306/T-307 brief asserted that
a randomized property closes the class where a parametrized probe closes one key. **Five adversarial rounds measured 44+
gaming keys and disproved it:** a pinned seed makes the draws an enumerable table, and every round that closed a per-field
key produced new *whole-payload* keys. What holds is three things together — an **unpinned second seed**, drawn **SHAPES**
rather than only values (pools including the repo's own fixture spellings), and a **canary asserting the generator's own
spread**. 20 deliberate generator narrowings each turn an ordinary run red; that is the load-bearing check, because a
silently narrowed generator otherwise stays green everywhere while resurrecting the keys it was built to close. The same
defect was then found in T-233's already-merged gate: its generator draws `expires_at` in 2027–2999 against now=2026, so no
draw resembles a realistic bid, and a 4,959-test differential is an empty diff.

**A textually-clean auto-merge produced a file that does not import.** Two lanes changed the runbook gate for compatible
reasons — one removed the xfail marker and the `import pytest` that died with it, the other kept eight xfail usages. **`git
merge-tree` said CLEAN, git auto-merged with no conflict marker, `check-branch` raised no veto, and both lanes' verifies
passed.** The merged file fails at **collection** with `NameError`. None of those gates executes anything. Found only by
running the merged tree — exactly what a clean merge tempts an integrator to skip. Merge discarded, main untouched, the lane
sent back to reconcile.

**A gate can be defanged by the change that adds it.** The T-306/T-307 midnight floor began counting `HOOK_PROVENANCE`'s
**own fixture constant** once fixture-shaped draws carried that block — reading **50/250 with zero drawn midnights**. The
floor was satisfied by the thing it was measuring. Adjacent shape, same epoch: T-301's static COPY-set blind spot is
**circular** — removing a COPY removes the importer that is the evidence the COPY is missing, so the checker goes silent;
only the real-image control caught that sabotage.

**Six lanes were dispatched onto ONE shared Postgres and Redis DB.** All six ran `PROXYSHOP_WORKER=0`, so all six shared
`proxyshop_w0` and one Redis logical DB; D38 gives every worker its own database and the task packets did not use it. **A
lane found it by reading `pg_stat_activity`** — six processes on worker 0, one dropping a database mid-run, and a logged
`DeadlockDetected`. Under worker 0 `make verify` gave 8 failures, then a **different 7** on re-run; isolated, the same 8
pass. Two tickets were held ambiguous purely by that contention and both came back clean. **Worktrees isolate the repository
and nothing else.**

**The rung-2 verifier caught a fail-open the orchestrator had merged an hour earlier — and it had been skipped at
integration time.** The rung-2 adversarial verifier that should have been dispatched at integration was not; run late, it
returned PASS on four lanes with evidence and FAIL on one, and the one was the merge made an hour before. F5 HIGH: the
runbook gate's own hardening rewrite (`e57268e`) is **weaker** than what it replaced, three measured ways, each reproduced
on two disjoint copies — re-fencing a step to ` ```console ` makes it vanish from the sweep (17 commands → 16, rc
unchanged); a recipe naming a missing *directory* component now passes; and the raw-text cross-check misses "Then make X"
because its guard is case-sensitive while its match is IGNORECASE. All three invisible to the exit code, because a new FAIL
lands inside a strict-xfail gate already expected to fail. A rewrite whose stated purpose was to fix a sweep going quiet,
reopening it three ways. **The consequence for the harness: because the verifier was skipped, the push gate's verifier
condition had been passing vacuously.** The verifier's other half of value is what it *confirmed* rather than assumed — the
`trust_rw` privilege claim against a live `has_table_privilege` matrix, with the append-only trigger proven to refuse UPDATE
and DELETE **after appending a real row first**, because a row-level BEFORE trigger cannot fire on an empty table.

**Related, recorded rather than repaired:** while a gate is an `xfail(strict=True)` latch over N known failures, an N+1th
failure of the same kind joins the latch and the exit code does not move — measured, sabotages that made a runbook step
*vanish* returned ordinary rc=0. The lane refused to "fix" it by pinning the current failure count, because pinning today's
failures pins the defect. Correct, and self-closing once the marker comes off.

## 8. Decisions for the next epoch

1. **T-323 — wiring the ranker is not "add a route."** Measured while settling the exchange/ingest boundary: materialising
   `apps/exchange/Dockerfile`'s COPY set plus the four proposed lines makes `import exchange.ranking` succeed — and that is
   all it does. There is **no Neo4j session construction anywhere in `apps/exchange/src`**, **no `NEO4J_` in
   `apps/exchange/compose.yaml`**, and **no neo4j driver in the Dockerfile's pip layer**. The COPY that fixes T-299 makes
   the ranker *importable, not functional*, and nothing has costed the difference. Related and blocking: wiring it today
   turns a booting exchange into a crash-looping one, via a module-scope import chain into an `ingest` the image does not
   ship.
2. **T-306 / T-307 are a two-language BEHAVIOUR CHANGE, not a patch.** Blast radius measured twice independently: **99
   tests**. The two-site repair is insufficient — normalising only `boundary.py:637` and `:677` diverges at seed 20260904
   draw 3, because `_price_reasons:804` still reads `authorized = depth if list_prices is None else 0.0`; it is **three
   sites**. The fix removes **five** xfail markers, not two (T-161, T-162 and T-194 also XPASS). The TypeScript door carries
   the identical asymmetry at `boundary.ts:545/:580`, **and the parity corpus cannot see the divergence between them** —
   Python tests `list_prices is None` while TypeScript tests `listPrices === undefined`, so an explicit `null` abstains in
   Python and is refused in TypeScript; `boundary.test.ts:956` maps `?? undefined`, which is what hides it. The
   cross-language parity apparatus reports agreement on a pair of doors that already behave differently. And the character
   of the ticket differs from its text: the no-roster abstention is a **documented deliberate opt-in** pinned by existing
   tests (including `test_the_cap_is_never_consulted_without_a_roster`, the exact inverse of T-307's claim), so these
   tickets assert the opt-in itself is wrong. Size and schedule it as a decided behaviour change.
3. **The 88 LIVE tickets from the triage sweep.** Run 2 of the gate-less triage is authoritative: a 195-agent workflow,
   6,680,217 tokens, 1,729 tool calls, all 97 refuters returned, 0 new failures — **88 `CONFIRMED_LIVE`, 9
   `REFUTED_ALREADY_FIXED`, 1 null**, no `UNCERTAIN` verdict anywhere. Run 1 was incomplete (66 refuters killed by a session
   rate limit, and the workflow falls back to the triage verdict when the refuter returns nothing, so 67 of 98 verdicts were
   single-source). Exactly one bucket moved, and it is the one that matters: **T-264 moved ALREADY_FIXED → LIVE** on a
   refuter that granted every positive claim in the triage evidence and still reproduced the headline defect three ways.
   Gate designs, write scopes and gate commands are preserved at
   `~/claude-build/artifacts/proxyshop-gateless-triage-20260904/` (`INDEX.md`, `triage-result-run2.json`,
   `gate-commands-run2.tsv`).

<sub>Cycle 18 · epoch `20ba0da..e03eff0`, 68 commits · **measured at `4e2fe2d`**, with the last 30 commits —
including the T-306/T-307 randomized properties, the image fixes, the ESC-018 step-b runbook work and the rung-2 audit — in
**no number above** · frozen bytes re-verified clean at amendment 19, sealed `tickets.json` hash matches HEAD · goal
amendments 19 · escalations 0 open / 19 resolved · written against `main` at `e03eff0`, repo untouched by this report. **The
working tree is NOT clean and was changing while this was written** — live lanes are editing `tickets.json`,
`.swarm-loop/{goals,manifest,freeze-log,escalations}`, `docker-compose.yml` and `proxyshop_support/**`, so any file-level
claim here is a snapshot, not a steady state.</sub>
