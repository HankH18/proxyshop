# Handoff — proxyshop swarm-loop, after cycle 9

**State:** `main` = `0820612`, tree clean, **pushed and verified** on both remotes
(gitlab `labs.gauntletai.com/hankholcomb/proxyshop` = submission target, github = backup;
both `ls-remote` confirmed matching, not just "push said OK"). Zero branches besides
`main`, zero worktrees, harness intact (16 frozen files).

**Frozen metric: 43/120 = 35.83%** (was 16/120 at session start). 4/12 metrics at target:
`acceptance_collected`, `build_succeeds`, `e1_foundation` (10), `spec_criteria` (8 — hit
this cycle). Cycle 9 checkpointed; both kill switches 0/3. `swarmloop.py resume` works and
recommends planning cycle 10.

**Read first:** `.swarm-loop/reports/cycle-9.md`. It is the real handoff; this file is the
index.

---

## Do these three things before dispatching anything

1. **Regenerate `.swarm-loop/backlog.md`.** It is accurate as of **98** tickets; the graph
   now has **101**. `tickets.json` is authoritative, the ledger is derived. Missing:
   **T-151** (ledger writes use the wrong DB role), **T-152** (the hardened R8 boundary has
   no caller), **T-153** (bid price never reconciled against its granted discount).
2. **Re-probe the git-guard** (a settings file is only loaded if it existed *before* the
   session started — one created mid-session is a measured no-op). Procedure: swarm-loop
   Phase 4. Hank is changing the harness, so do not assume last session's wiring holds.
3. **Note the standing re-measure flag on cycle 7** and leave it alone. Re-measuring it now
   would run against today's tree and record a false value under cycle 7's label. It blocks
   only `all_targets_met`, which is 77 tests away.

## Priority

`analyze` says **e6_trust_passing** — 2/26, error 24, the only `converging_off_track`
verdict. Everything else is `converging_on_track` or at target.

Ready frontier is large (34 as of the last regeneration). Highest-value unbuilt tickets by
failing acceptance tests: **T-062** (11), **T-032** (9), **T-044** (8), **T-065** (6),
**T-033** (5).

## Things that will waste your time if you don't know them

- **`grep` is ugrep and emits paths without a leading `./`.** Any pipeline filtering on
  `^\./` silently matches nothing. This produced one entirely fictitious "zero producers"
  finding last session. Use `git grep` or codegraph; be suspicious of any empty result.
- **`PROXYSHOP_WORKER=<n>` on every pytest invocation.** It is the only state isolation.
- **"Zero production callers" is not reliably a defect here.** It is often a correctly
  built half whose consumer is a *scheduled* ticket. Indistinguishable from a real defect
  by caller search alone — check whether an open ticket owns the consumer. Three of eight
  refutations last session turned on exactly this.
- **The gate fails on things no single lane can see.** Last session: twice on
  `ruff format --check .` (lanes don't run the formatter) and once on mypy errors that
  exist only on the combined tree. Run `./scripts/verify.sh all` at integration, always.
- **Branch tips move after you collect them.** Two lanes added commits after their
  reported tip. Pin the tip, and re-check before closing the wave.
- **`.swarm-loop/reports/T-132-amendment-design-REFUTED.json` contains WRONG guidance.**
  It says assert `min(class) >= K AND len(classes) <= len(pop)//K`. The second is *implied
  by* the first, and collapse-everything passes both. A **lower** bound on class count is
  what catches the degenerate case. Correction is in `learnings.md`.

## Open, recorded, not fixed

- **T-152 / T-153 (both HIGH).** The hardened store-agent R8 boundary has no caller —
  `enforce_bid_provenance` must run on the *whole bid*; passing `bid.claims` leaves the
  Offer outside, which is the original defect. And bid price is never reconciled against
  the granted discount; `contracts.boundary.validate_bid` has no price/floor check at all.
- **T-109 and T-111/T-123 read as fixed by earlier waves; git says the files were never
  touched.** Back on the open frontier. Trust git over wave-closure prose.
- Fan-out pool bounds the thread leak but cannot reclaim a worker stuck in a socket — the
  real fix is a timeout on the outbound bid client.
- No service has a readiness route; healthchecks prove only that uvicorn answers.

## Two carve-outs that are rules, not preferences

- **T-080's manifest approval is a HUMAN gate.** Never generate it, never fill
  `approver`/`approved_at`/`artifact`, never run `python -m fixtures.approval` in a writing
  form. It is currently `pending_human_approval` and must stay that way until Hank runs it.
  The gate was audited last session and is **sound** — an earlier claim that
  `refresh_digests` defeats it was refuted: the two-document digest chain turns the frozen
  test red at `test_e8_proofs.py:355`. Running it flips a 6th E8 test.
- **Never amend the frozen contract** (`goals.json`, `.swarm-loop/acceptance/`) without
  Hank awake. A branch touching it is auto-rejected; the sanctioned channel for a worker's
  objection is the packet's `NEEDS` field, and the orchestrator must answer or escalate it.

## Standing authorizations (do not ask)

Fix found defects; prune/retire git worktrees and docker volumes. Report after, not before.

## Don't

Modify `.swarm-loop/` beyond bookkeeping · `git stash` / `reset --hard` /
`checkout -- <path>` / `clean -f` · `make deps-down` · `FLUSHALL` · re-`freeze` to clear a
violation (use `restore-harness`).
