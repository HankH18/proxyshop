# Cycle 19 — epoch report

HEAD at close: see `state.json` head_history. Merged this epoch: **T-348** (merge `fe81880`),
the tier-0 fallback handoff, implementing Hank's ESC-025 ruling.

## Metrics — 12 of 12 at target

| metric | value | target |
|---|---|---|
| acceptance_pass_rate | 100 | 100 |
| acceptance_collected | 120 | 120 |
| build_succeeds | 1 | 1 |
| spec_criteria_passing | 8 | 8 |
| e1_foundation_passing | 10 | 10 |
| e2_ingestion_passing | 8 | 8 |
| e3_exchange_passing | 21 | 21 |
| e4_store_agent_passing | 20 | 20 |
| e5_merchant_passing | 10 | 10 |
| e6_trust_passing | 26 | 26 |
| e7_buyer_passing | 9 | 9 |
| e8_proofs_passing | 8 | 8 |

`analyze` withholds ALL TARGETS MET for two standing reasons, neither new this epoch:
259 tickets carry no frozen test, and a re-measure is owed for cycle 7.

## build_succeeds measured 0, then 1 — and the first reading was the machine

The first sweep recorded `build_succeeds = 0`. Seven tests were red, ALL of them Postgres
event-ledger-chain tests in `apps/trust`: chain-fork, duplicate-id, replay-stream-hash,
tail-truncation-anchor, and three in `test_events.py`. Re-run alone on a quiet machine the
identical tree gave **234 lines, terminal `OK: all`, 6073 passed, zero failures**, and the
second sweep recorded 1. History is append-only and analyze/checkpoint keep the LAST row per
cycle in file order, so the corrected row governs; the 0 row is left in place as the record.

**Mechanism, pinned to one statement.** `test_tail_truncation_is_detected_by_the_stored_anchor`
observed `anchor_ok: True` WITH `length == 2`. A DELETE leaves the anchor stale and `anchor_ok`
false -- that is the entire purpose of that test. Only TRUNCATE resets the anchor alongside the
rows, through the AFTER TRUNCATE trigger in `db/migrations/0002_ledger_tables.sql:334-336`. The
repo has exactly one live TRUNCATE of those tables, the `ledger_clean` fixture at
`apps/trust/tests/_fixtures_ledger_schema.py:420`, and unlike its sibling schema-rebuild fixture
-- which serialises on MIGRATION_LOCK_KEY -- it has NO cross-session protection at all.

So a second process wrote into `proxyshop_w0` mid-run. **The specific origin was never
identified** and this report does not claim one. A live mechanism CLASS was found (18
`reproduction` fields in tickets.json and 24 lines in backlog.md instruct a reader to run at
`PROXYSHOP_WORKER=0`, which is the scored metric's own database), but no evidence places anyone
on that path during the failing window.

**Two of the seven read as product bugs and are not.**
`test_the_same_id_with_different_content_is_a_conflict_over_http` returned a 422 whose MESSAGE
proves the row existed (`ledger/store.py:237`, reachable only when `existing is not None`) and
whose STATUS proves a re-read moments later did not find it (`events/pg.py:396` via `_holds`) --
internally contradictory unless the table emptied in between. And `test_the_chain_cannot_fork`'s
`UNIQUE (prev_hash)` is real and unconditional; the insert succeeded only because the row it
should have collided with was gone.

## Process findings this epoch

**The lane-slot allocator exists and was never opened for this run.** `swarmloop.py slot`
leases the lowest free index in `[1, width)` -- index 0 reserved BY CONSTRUCTION -- with
`--release`, `--gc`, `--list`, and a `--width` that refuses to narrow under a live lease.
`slot --list` reports this run "recorded no lane-slot pool and holds no registry". Every lane's
index has therefore been hand-picked all run. This is an INITIALISATION gap (`init` takes
`--lane-slots` / `--lane-env-file` / `--lane-env-var`), not a missing capability, and nothing in
the run surfaces that the pool is unused: no dispatch path refuses a lane that holds no lease.

**A claim of mine that was wrong, recorded because the remediation was the cost.** I reported
"18 frozen GATES hardcode PROXYSHOP_WORKER=0" from a file-level `grep -c`. Field-scoped: 0 in
`verify`, 18 in `reproduction`; 142 gates use `${PROXYSHOP_GATE_WORKER:-N}` with defaults
spanning 1-15 and deliberately skipping 0. The gates are the part of the system that got it
right, and the fix I proposed would have consumed a lane editing correct fields.

**The harness persists no metric output.** `history.csv` keeps the VALUE and discards the
EVIDENCE, so an infrastructure red and a real red leave identical artifacts. T-210 already says
this probe cannot tell the two apart; this epoch is its first concrete instance, and it resolved
correctly only because the log survived in a scratchpad.
