# Cycle 11/102 analysis

Targets met: 5/12

Harness integrity at analysis time: **intact** — every frozen file re-hashed against `.swarm-loop/manifest.json`, which is itself reconciled against the append-only `.swarm-loop/freeze-log.jsonl`.

> **GOALPOSTS MOVED 4x — this trend is not against a single fixed target.** Last amendment 2026-09-03T01:26:53: 'ESC-005 / T-117, user-authorized by Hank directly in-session ("You have my authorization for all three of the proposed changes"). scripts/verify.sh only; goals.json and .swarm-loop/acceptance/ are untouched by this amend.\n\nTwo changes, neither weakening any assertion:\n\n1. run_pytest now EMITS a SELECTION line after every invocation naming the deselected and skipped counts, with a note that those tests did not run. T-117 acceptance 2 ("deselected counts are reported, not swallowed, so a green gate names what it did not run"). Nothing is made easier to pass; a count is printed that was previously invisible.\n\n2. `check` no longer deselects `docker`. That blanket deselection WAS the defect T-117 names: it silently removed all three of T-110\'s fresh-volume tests including the one grading that ticket\'s acceptance criterion 2, so a lane reported its own gate green while the tests written to grade it never ran. T-109 replaced the all-or-nothing datastore assumption with per-service reachability, so a docker-marked test now probes the service it needs and skips cleanly when that service is down. This is T-117 acceptance 1 and 3.\n\nVERIFIED BOTH DIRECTIONS BEFORE AMENDING. Still passes with by-design deselection: `verify.sh check` -> exit 0, 3908 passed / 1 deselected (up from ~3520 selected, i.e. ~388 docker tests that every per-ticket gate had been silently dropping); `verify.sh all` -> exit 0, 3908 pytest / 710 vitest / mypy clean on 205 files. Still refuses a gate that selects nothing: pytest exits 5 when everything is deselected (measured: a marker matching nothing gives `801 deselected`, exit 5) and run_pytest maps exit 5 to a FATAL non-zero, a path unchanged by this amend apart from an added explanatory line.\n\nNOT included: ESC-007\'s change to .swarm-loop/acceptance/test_e4_store_agent.py. It is written and compiles but the acceptance suite cannot be executed in this session (blocked by the auto-mode classifier; pyproject norecursedirs excludes .swarm-loop from pytest collection, so no other route reaches it), and an amend around bytes that were never run is exactly what this mechanism exists to prevent. That file is restored to its frozen bytes and ESC-007 stays open.'. Full record: `.swarm-loop/freeze-log.jsonl`.

| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |
|---|---|---|---|---|---|---|---|
| acceptance_pass_rate | converging_on_track | cycle 11 | 62.5 | 100 | 37.5 | -4.61266 | 0 |
| e4_store_agent_passing | converging_on_track | cycle 11 | 5 | 20 | 15 | -0.412587 | 0 |
| e3_exchange_passing | converging_on_track | cycle 11 | 9 | 21 | 12 | -0.755245 | 0 |
| e7_buyer_passing | converging_on_track | cycle 11 | 2 | 9 | 7 | -0.223776 | 0 |
| e5_merchant_passing | converging_on_track | cycle 11 | 4 | 10 | 6 | -0.412587 | 0 |
| e8_proofs_passing | converging_on_track | cycle 11 | 5 | 8 | 3 | -0.559441 | 0 |
| e2_ingestion_passing | converging_on_track | cycle 11 | 6 | 8 | 2 | -0.618881 | 0 |
| acceptance_collected | at_target | cycle 11 | 120 | 120 | 0 | – | – |
| build_succeeds | at_target | cycle 11 | 1 | 1 | 0 | – | – |
| spec_criteria_passing | at_target | cycle 11 | 8 | 8 | 0 | – | – |
| e1_foundation_passing | at_target | cycle 11 | 10 | 10 | 0 | – | – |
| e6_trust_passing | at_target | cycle 11 | 26 | 26 | 0 | – | – |

**RE-MEASURE OWED — cycle(s) 7. A `freeze --amend` moved at least one target and the baseline it moved has not been re-measured since. All-targets-met is WITHHELD until 'measure --cycle <n>' has run for each: an amendment invalidates its own baseline, and a hand-entered 'record' does not satisfy it.**

_Note: the stored `error` column disagreed with the current target for acceptance_collected, e3_exchange_passing, e4_store_agent_passing, e6_trust_passing, e8_proofs_passing; every error above is recomputed from `value` against the target in force now, never read from history._

