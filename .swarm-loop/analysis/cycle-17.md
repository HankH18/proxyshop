# Cycle 17/102 analysis

Targets met: 12/12

Harness integrity at analysis time: **intact** — every frozen file re-hashed against `.swarm-loop/manifest.json`, which is itself reconciled against the append-only `.swarm-loop/freeze-log.jsonl`.

> **SELECTION TRACKING IS UNAVAILABLE for 12 metric(s): `acceptance_pass_rate`, `acceptance_collected`, `build_succeeds`, `spec_criteria_passing`, `e1_foundation_passing`, `e2_ingestion_passing`, `e3_exchange_passing`, `e4_store_agent_passing`, `e5_merchant_passing`, `e6_trust_passing`, `e7_buyer_passing`, `e8_proofs_passing`.** Read every 'no selection regression' above as NOT MEASURED, never as clean.
> `measure` records the selected/deselected columns only when the metric command's own stdout is PYTEST-SHAPED, and a correctly-authored frozen wrapper prints a BARE NUMBER as its last stdout line — so the tokens are hidden and both columns are written empty. Measured on a real frozen cycle-0 baseline: all 12 metrics blank, so the regression comparison can never fire for them at any cycle.
> This is not the same as `0 deselected`: a missing token means zero and IS a baseline; an un-parseable output means UNKNOWN and is not. The deselection backstop is the only one of the three enforcement points that can see a filter added after the freeze — this run has two. Restore it by having the wrapper report selection out of band (a sidecar beside its metric log, or stderr) rather than by printing raw pytest output to stdout, which would break the bare-number contract every metric depends on.

> **GOALPOSTS MOVED 16x — this trend is not against a single fixed target.** Last amendment 2026-09-04T01:00:31: "ESC-013 approved class, amendment 16 — tickets.json verify fields ONLY, 6 tickets (T-158, T-169, T-170, T-204, T-235, T-250), git diff exactly 6 insertions / 6 deletions and ZERO changed lines that are not a 'verify' line (measured). All six carried the placeholder 'false  # NO GATE YET' and were undispatchable. A reproduction lane landed apps/exchange/tests/test_repro_open_tickets.py on main with one xfail(strict=True) test per ticket, each verified in BOTH directions by the lane and re-measured by me after merge: normal run of that file is '7 xfailed' exit 0, and the whole apps/exchange suite post-merge is '695 passed, 16 skipped, 7 xfailed' with the T-223 threshold fix already present, so the new tests do not interact with the cycle-16 exchange changes. Every red was checked by the lane to fail on its INTENDED assertion rather than on setup. Gate form is 'export PROXYSHOP_WORKER=0 && uv run python -m pytest <file> -q --runxfail -k <node>' per amendments 14 and 15. T-169 has two separately-fixable halves in the file; its gate names the production-call-site half specifically so the gate selects exactly one test. A JUDGEMENT CALL RECORDED RATHER THAN BURIED: T-170 is gated even though the earlier T-176 adjudication downgraded it to 'a graph gap, not a defect'. The lane gated it on a ground T-176 did not consider — packages/contracts publishes /auctions/{auction_id}/accept and create_app().openapi() answers only ['/auctions','/auctions/{auction_id}'], so this is a promise already made to clients rather than an unwired function — and I accept that reading. T-250'S RECORDED FINDING TEXT IS PARTLY WRONG AND THE GATE ASSERTS THE CORRECT HALF: I minted T-250 quoting a sibling lane that the no-cap case (price_reasons(unit=1e-09, depth=None)) returns []; the repro lane measured that it actually returns ['price_under_declared_depth:offer.unit_price'] — it IS refused — and that the 'no cap so the walk never runs' behaviour lives in collect.py's _is_judged, not in boundary.py. The defect is real but narrower than I recorded: with max_discount_pct=100.0 the boundary returns [] for 0.001 against a 100.00 roster price, and that is what the gate asserts. Thirteen further tickets in this slice were measured and deliberately NOT gated because the defect is not there to reproduce; seven of those are closed by ledger verdict in this same cycle. No metric, target, acceptance test, product file, dependency or ticket dependency changed.". Full record: `.swarm-loop/freeze-log.jsonl`.

| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |
|---|---|---|---|---|---|---|---|
| acceptance_pass_rate | at_target | cycle 17 | 100 | 100 | 0 | – | – |
| acceptance_collected | at_target | cycle 17 | 120 | 120 | 0 | – | – |
| build_succeeds | at_target | cycle 17 | 1 | 1 | 0 | – | – |
| spec_criteria_passing | at_target | cycle 17 | 8 | 8 | 0 | – | – |
| e1_foundation_passing | at_target | cycle 17 | 10 | 10 | 0 | – | – |
| e2_ingestion_passing | at_target | cycle 17 | 8 | 8 | 0 | – | – |
| e3_exchange_passing | at_target | cycle 17 | 21 | 21 | 0 | – | – |
| e4_store_agent_passing | at_target | cycle 17 | 20 | 20 | 0 | – | – |
| e5_merchant_passing | at_target | cycle 17 | 10 | 10 | 0 | – | – |
| e6_trust_passing | at_target | cycle 17 | 26 | 26 | 0 | – | – |
| e7_buyer_passing | at_target | cycle 17 | 9 | 9 | 0 | – | – |
| e8_proofs_passing | at_target | cycle 17 | 8 | 8 | 0 | – | – |

**RE-MEASURE OWED — cycle(s) 7. A `freeze --amend` moved at least one target and the baseline it moved has not been re-measured since. All-targets-met is WITHHELD until 'measure --cycle <n>' has run for each: an amendment invalidates its own baseline, and a hand-entered 'record' does not satisfy it.**

_Note: the stored `error` column disagreed with the current target for acceptance_collected, e3_exchange_passing, e4_store_agent_passing, e6_trust_passing, e8_proofs_passing; every error above is recomputed from `value` against the target in force now, never read from history._

