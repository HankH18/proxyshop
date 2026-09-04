# Cycle 15/102 analysis

Targets met: 7/12

Harness integrity at analysis time: **intact** — every frozen file re-hashed against `.swarm-loop/manifest.json`, which is itself reconciled against the append-only `.swarm-loop/freeze-log.jsonl`.

> **GOALPOSTS MOVED 11x — this trend is not against a single fixed target.** Last amendment 2026-09-03T20:52:46: "ESC-013 amendment 11 — SELF-CORRECTION of my own amendment 10, under the same approval Hank gave ('Go with your best suggestions for both'), changing nothing about what was approved. T-228's verify field ONLY; the other nine gates are untouched and still stamp exactly as amendment 10 left them. Amendment 10 set T-228 to 'uv run python -m mypy'. I then did what ESC-013 promised — re-ran red-check on all ten before merging anything — and it stamped T-228 WEAK: 'exit 1, but NO tests were SELECTED'. red-check's notion of a real gate is PYTEST-SHAPED, so a pure type-checker command can never stamp red under it no matter how genuinely red it is, and check-branch --ticket vetoes on that stamp. The gate was correct about the product and wrong about the harness. THE REPAIR: 'uv run python -m pytest packages/store-agent/tests -q && uv run python -m mypy' — store-agent's own suite first, so tests are genuinely selected, then the type gate. SKILL.md names this exact shape as legitimate and unclassifiable-by-machine: 'a wrapper whose red legitimately comes from a linter after a green pytest prints exactly the same thing — read the command, the harness cannot tell those two apart and does not pretend to'. MEASURED IN A BARE THROWAWAY WORKTREE OF THE MERGE BASE 6b6040f, not the primary checkout: the pytest half exits 0 (control flow reached the '&&'), then mypy prints 'Found 8 errors in 3 files (checked 224 source files)' and the whole command exits 1 — red at base, with tests selected. Green at the branch tip 2ce5983, where the same command's mypy half reports 'no issues found in 224 source files'. Same precedent as amendment 9, which repaired amendment 8 under Hank's prior approval for the same reason: the first spelling was verified against the wrong machine state. No metric, target, acceptance test, product file, dependency or ticket dependency changed.". Full record: `.swarm-loop/freeze-log.jsonl`.

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

**RE-MEASURE OWED — cycle(s) 7. A `freeze --amend` moved at least one target and the baseline it moved has not been re-measured since. All-targets-met is WITHHELD until 'measure --cycle <n>' has run for each: an amendment invalidates its own baseline, and a hand-entered 'record' does not satisfy it.**

_Note: the stored `error` column disagreed with the current target for acceptance_collected, e3_exchange_passing, e4_store_agent_passing, e6_trust_passing, e8_proofs_passing; every error above is recomputed from `value` against the target in force now, never read from history._

