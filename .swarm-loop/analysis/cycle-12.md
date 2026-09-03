# Cycle 12/102 analysis

Targets met: 5/12

Harness integrity at analysis time: **intact** — every frozen file re-hashed against `.swarm-loop/manifest.json`, which is itself reconciled against the append-only `.swarm-loop/freeze-log.jsonl`.

> **GOALPOSTS MOVED 5x — this trend is not against a single fixed target.** Last amendment 2026-09-03T02:42:15: 'ESC-005 amendment 5 — CORRECTING amendment 4\'s own defects, found by a rung-2 verifier.\nscripts/verify.sh only. Same user authorization; this fixes my implementation of it, and\ncorrects two false figures I recorded in amendment 4\'s justification.\n\n1. THE REAL BUG. run_pytest\'s SELECTION parsing ran under `set -euo pipefail`:\n   `line="$(grep -E ... | tail -1)"`. A grep that matches nothing exits 1, pipefail promotes\n   it, and the assignment\'s non-zero status tripped errexit — killing the function before\n   `return "$rc"`, before the SELECTION line, and before `rm -f`. A verifier drove both bodies\n   through a replica of the call site and measured pytest\'s real exit code being destroyed:\n   usage-error 4->1, INTERNALERROR 3->1, SIGKILL 137->1, interrupt 2->1, and a GREEN run whose\n   epilogue could not be parsed 0->1. Fail-closed, so amendment 4 weakened nothing — but it\n   discarded the true status, leaked the temp file, and suppressed the SELECTION line in exactly\n   the case where the parse had failed. Fixed with `|| true` on the capture plus an explicit\n   UNPARSEABLE branch that reports the unknown coverage and returns the real rc. Re-verified\n   across all five modes: 4->4, 3->3, 137->137, 2->2, 0->0.\n\n2. TWO FALSE FIGURES I RECORDED, corrected in place. Amendment 4 claimed "~3520 -> 3908, i.e.\n   ~388 docker tests". Measured truth: old selection 3729 / 247 deselected, new 3975 / 1, so\n   246 newly-run docker tests. My number compared a pre-cycle-11 suite size against a\n   post-cycle-11 one and charged ~142 newly-ADDED tests to this change. Direction right,\n   magnitude wrong by ~58%. And there is NO slow-marked test — `-m slow` collects zero — so\n   "the single slow test" was wrong; the one remaining deselection is needs_model, before and\n   after. `and not slow` is a present-day no-op.\n\n3. THE UNNAMED THIRD BEHAVIOURAL CHANGE, now named in the file. Every `graph`-marked test is\n   also `docker`-marked, so the old `check` never built the session-scoped `_neo4j_guard` and\n   never took the machine-global flock. The new one does, and holds it for most of the run\n   (measured: 196s of a 222s session). Concurrent lanes running `check` serialise on it, and one\n   that waits past the budget fails with Neo4jLockTimeout for a machine reason. Tracked as its\n   own ticket; the fix belongs in the fixture\'s scope, not in this file.\n\nAlso corrected: the usage header at line 5 still said `check` "skips @pytest.mark.docker and\n.slow", which has been false since amendment 4 and is the first line a reader consults.\n\nVerified after: verify.sh check exit 0, 3975 passed / 1 deselected.'. Full record: `.swarm-loop/freeze-log.jsonl`.

| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |
|---|---|---|---|---|---|---|---|
| acceptance_pass_rate | converging_on_track | cycle 12 | 63.33 | 100 | 36.67 | -4.92687 | 0 |
| e4_store_agent_passing | converging_on_track | cycle 12 | 5 | 20 | 15 | -0.450549 | 0 |
| e3_exchange_passing | converging_on_track | cycle 12 | 9 | 21 | 12 | -0.824176 | 0 |
| e7_buyer_passing | converging_on_track | cycle 12 | 2 | 9 | 7 | -0.21978 | 0 |
| e5_merchant_passing | converging_on_track | cycle 12 | 4 | 10 | 6 | -0.417582 | 0 |
| e2_ingestion_passing | converging_on_track | cycle 12 | 6 | 8 | 2 | -0.626374 | 0 |
| e8_proofs_passing | converging_on_track | cycle 12 | 6 | 8 | 2 | -0.582418 | 0 |
| acceptance_collected | at_target | cycle 12 | 120 | 120 | 0 | – | – |
| build_succeeds | at_target | cycle 12 | 1 | 1 | 0 | – | – |
| spec_criteria_passing | at_target | cycle 12 | 8 | 8 | 0 | – | – |
| e1_foundation_passing | at_target | cycle 12 | 10 | 10 | 0 | – | – |
| e6_trust_passing | at_target | cycle 12 | 26 | 26 | 0 | – | – |

**RE-MEASURE OWED — cycle(s) 7. A `freeze --amend` moved at least one target and the baseline it moved has not been re-measured since. All-targets-met is WITHHELD until 'measure --cycle <n>' has run for each: an amendment invalidates its own baseline, and a hand-entered 'record' does not satisfy it.**

_Note: the stored `error` column disagreed with the current target for acceptance_collected, e3_exchange_passing, e4_store_agent_passing, e6_trust_passing, e8_proofs_passing; every error above is recomputed from `value` against the target in force now, never read from history._

