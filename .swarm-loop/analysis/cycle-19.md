# Cycle 19/102 analysis

Targets met: 12/12

Harness integrity at analysis time: **intact** — every frozen file re-hashed against `.swarm-loop/manifest.json`, which is itself reconciled against the append-only `.swarm-loop/freeze-log.jsonl`.

> **SELECTION TRACKING IS UNAVAILABLE for 12 metric(s): `acceptance_pass_rate`, `acceptance_collected`, `build_succeeds`, `spec_criteria_passing`, `e1_foundation_passing`, `e2_ingestion_passing`, `e3_exchange_passing`, `e4_store_agent_passing`, `e5_merchant_passing`, `e6_trust_passing`, `e7_buyer_passing`, `e8_proofs_passing`.** Read every 'no selection regression' above as NOT MEASURED, never as clean.
> `measure` records the selected/deselected columns only when the metric command's own stdout is PYTEST-SHAPED, and a correctly-authored frozen wrapper prints a BARE NUMBER as its last stdout line — so the tokens are hidden and both columns are written empty. Measured on a real frozen cycle-0 baseline: all 12 metrics blank, so the regression comparison can never fire for them at any cycle.
> This is not the same as `0 deselected`: a missing token means zero and IS a baseline; an un-parseable output means UNKNOWN and is not. The deselection backstop is the only one of the three enforcement points that can see a filter added after the freeze — this run has two. Restore it by having the wrapper report selection out of band (a sidecar beside its metric log, or stderr) rather than by printing raw pytest output to stdout, which would break the bare-number contract every metric depends on.

Acceptance coverage (the frozen suite vs the ticket graph):
- graded_ticket_fraction: 12.5%  (37 of 296 ticket(s) carry >=1 frozen test)
- marked_test_fraction:   100.0%  (120 of 120 scanned test(s) name a ticket; 0 grade nothing in particular)
- 259 ticket(s) carry NO frozen test at all: T-013, T-024, T-031, T-036, T-054, T-082, T-083, T-084, T-086, T-087, T-100, T-101 …

> **259 TICKET(S) ARE GRADED BY NOBODY BUT THEIR OWN AUTHOR.** ALL TARGETS MET is withheld while this stands.
> Measured on a real run: the graph grew 44 -> 263 tickets while the frozen suite added ZERO test files, so the graded fraction fell 84% -> 14.1% and 76 tickets closed with no frozen test grading them. A metric at target says the tests that exist pass; it says nothing about the tickets no test names.
> **The remedy is not an amendment, but it is not unattended either.** Write tests marked `@pytest.mark.ticket("<id>")` under the frozen acceptance path and run `swarmloop.py freeze --extend`, which adds files and metric ids only and refuses any edit to a frozen byte. It requires `--user-authorized`: a minted file is taken on its FILENAME and its module-level code runs at import, so a human reads it first. Pass `--allow-ungraded-tickets` to analyze only once you have decided the gap is acceptable, and say so in the cycle report.
> Ungraded: T-013, T-024, T-031, T-036, T-054, T-082, T-083, T-084, T-086, T-087, T-100, T-101, T-102, T-103, T-104, T-105, T-106, T-107, T-108, T-109, T-110, T-111, T-112, T-113, T-114, T-115, T-116, T-117, T-118, T-119, T-120, T-121, T-122, T-123, T-124, T-125, T-126, T-127, T-128, T-129 …

> **GOALPOSTS MOVED 30x — this trend is not against a single fixed target.** Last amendment 2026-09-05T13:20:23: 'ESC-026, GRANTED BY HANK IN SESSION 2026-09-05: "I authorize your recommendation on ESC-026." scripts/check_verify_contracts.py, PROSE ONLY, three string constants. WHY IT WAS NEEDED: Hanks own ESC-023 ruling removed --passWithNoTests from verify.sh (amendment 29), which falsified three sentences in this SEPARATE frozen file that describe the flag as present -- the module docstring, check_vitest_projects_have_tests own docstring, and a failure message a HUMAN READS when that check trips. The last is the one that mattered: it is not a comment, it is text shipped to whoever is already investigating, telling them the current behaviour is the opposite of what it is. WHY I ASKED RATHER THAN FOLDING IT IN: his ESC-023 ruling named verify.sh and left this file frozen. The argument for doing it silently was good -- prose-only, no behaviour change, his own ruling caused it -- and that is exactly the shape of reasoning the freeze exists to refuse. I had already withdrawn two escalations tonight on premises I never checked, so I would rather over-ask than discover I had quietly widened a grant. THE CHECK ITSELF IS UNCHANGED AND MUST STAY. Per-project vitest coverage is strictly finer-grained than amendment 29 FATAL: run_vitest fires on a zero TOTAL collection, and cannot see ONE project going empty while the others still report a nonzero total. The corrected prose now says exactly that, so the checks basis is true and its reason for existing is legible rather than resting on a flag that is gone. PROVEN PROSE-ONLY STRUCTURALLY, NOT BY EYE, which is what I said I would do: parsed both versions, blanked every string Constant, and compared the AST dumps -- identical. Control, because a comparison that cannot fail proves nothing: the same comparison WITHOUT blanking is NOT identical, and exactly 3 string constants differ. So the code is byte-equivalent in structure and only prose moved. MEASURED, and it cannot move a metric because behaviour is unchanged: the check runs and reports "OK: pytest-config, test-path-filter, schema-package, non-empty-test-dir, raw-Redis-client and unique-fixture-name contracts all hold", and acceptance is 120/120 before and after. NOT CHANGED: no check added, removed, weakened or reordered; no logic touched; no other frozen path. D7 itself was already marked SUPERSEDED in .swarm-loop/decisions.md and intake-report.md, which are not frozen, preserving D7 first half -- npx vitest run <path> really is a path filter and really does isolate -- because only its no-tests clause died.'. Full record: `.swarm-loop/freeze-log.jsonl`.

| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |
|---|---|---|---|---|---|---|---|
| acceptance_pass_rate | at_target | cycle 19 | 100 | 100 | 0 | – | – |
| acceptance_collected | at_target | cycle 19 | 120 | 120 | 0 | – | – |
| build_succeeds | at_target | cycle 19 | 1 | 1 | 0 | – | – |
| spec_criteria_passing | at_target | cycle 19 | 8 | 8 | 0 | – | – |
| e1_foundation_passing | at_target | cycle 19 | 10 | 10 | 0 | – | – |
| e2_ingestion_passing | at_target | cycle 19 | 8 | 8 | 0 | – | – |
| e3_exchange_passing | at_target | cycle 19 | 21 | 21 | 0 | – | – |
| e4_store_agent_passing | at_target | cycle 19 | 20 | 20 | 0 | – | – |
| e5_merchant_passing | at_target | cycle 19 | 10 | 10 | 0 | – | – |
| e6_trust_passing | at_target | cycle 19 | 26 | 26 | 0 | – | – |
| e7_buyer_passing | at_target | cycle 19 | 9 | 9 | 0 | – | – |
| e8_proofs_passing | at_target | cycle 19 | 8 | 8 | 0 | – | – |

**RE-MEASURE OWED — cycle(s) 7. A `freeze --amend` moved at least one target and the baseline it moved has not been re-measured since. All-targets-met is WITHHELD until 'measure --cycle <n>' has run for each: an amendment invalidates its own baseline, and a hand-entered 'record' does not satisfy it.**

_Note: the stored `error` column disagreed with the current target for acceptance_collected, e3_exchange_passing, e4_store_agent_passing, e6_trust_passing, e8_proofs_passing; every error above is recomputed from `value` against the target in force now, never read from history._

