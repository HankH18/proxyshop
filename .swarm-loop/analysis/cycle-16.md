# Cycle 16/102 analysis

Targets met: 10/12

Harness integrity at analysis time: **intact** — every frozen file re-hashed against `.swarm-loop/manifest.json`, which is itself reconciled against the append-only `.swarm-loop/freeze-log.jsonl`.

> **SELECTION TRACKING IS UNAVAILABLE for 12 metric(s): `acceptance_pass_rate`, `acceptance_collected`, `build_succeeds`, `spec_criteria_passing`, `e1_foundation_passing`, `e2_ingestion_passing`, `e3_exchange_passing`, `e4_store_agent_passing`, `e5_merchant_passing`, `e6_trust_passing`, `e7_buyer_passing`, `e8_proofs_passing`.** Read every 'no selection regression' above as NOT MEASURED, never as clean.
> `measure` records the selected/deselected columns only when the metric command's own stdout is PYTEST-SHAPED, and a correctly-authored frozen wrapper prints a BARE NUMBER as its last stdout line — so the tokens are hidden and both columns are written empty. Measured on a real frozen cycle-0 baseline: all 12 metrics blank, so the regression comparison can never fire for them at any cycle.
> This is not the same as `0 deselected`: a missing token means zero and IS a baseline; an un-parseable output means UNKNOWN and is not. The deselection backstop is the only one of the three enforcement points that can see a filter added after the freeze — this run has two. Restore it by having the wrapper report selection out of band (a sidecar beside its metric log, or stderr) rather than by printing raw pytest output to stdout, which would break the bare-number contract every metric depends on.

> **GOALPOSTS MOVED 14x — this trend is not against a single fixed target.** Last amendment 2026-09-03T23:21:08: "ESC-013 class, amendment 14 — approved class (Hank granted amendments 10 and 11 and has since said not to re-ask for what is already approved). tickets.json verify fields ONLY, 80 tickets, git diff exactly 80 insertions / 80 deletions and ZERO changed lines that are not a 'verify' line. THE DEFECT: 80 of the 92 non-placeholder gates in this run COULD NOT RUN AT ALL in a clean shell, and every one of them looked verified because I always invoked red-check with PROXYSHOP_WORKER already exported in my own shell, so the variable was inherited by the subprocess. The gates were green about nothing. THREE INDEPENDENT CAUSES, each reproduced before repair: (1) the root conftest's D38 per-worker guard aborts pytest rc=4 'PROXYSHOP_WORKER is unset' and scripts/verify.sh rc=2 'FATAL ... (D38)'; (2) a command-PREFIX assignment does not survive an '&&' chain — measured, 'PROXYSHOP_WORKER=0 true && printf' prints UNSET — so 13 chained gates need 'export PROXYSHOP_WORKER=0 && ...' instead, and my own first list of those was wrong, omitting T-010, T-108, T-115, T-125 and T-228; (3) 50 gates invoke a bare 'pytest', which in a shell without the venv activated resolves to Anaconda's 6.2.4 and dies rc=4 on pyproject.toml:70 minversion=8.2 BEFORE the env var is ever consulted, so fixing only the variable would have left them broken — they are converted to 'uv run python -m pytest', the self-provisioning form amendments 9 through 13 established. EVERY ONE OF THE 11 DISTINCT COMMAND SHAPES WAS EXECUTED IN A GENUINELY CLEAN SHELL (env -u PROXYSHOP_WORKER -u VIRTUAL_ENV -u PYTHONPATH -u CONDA_PREFIX, anaconda still ahead of the venv on PATH, timeout 240) and observed to RUN AND SELECT TESTS rather than abort: make verify rc=0 'OK: all'; verify.sh check rc=0 'OK: check'; the plain pytest shape 93 passed; the acceptance -k shape 1 passed 7 deselected; the --runxfail shape rc=1 '3 failed 2 deselected', a genuine red; all four chained shapes rc=0 with the export surviving every link. THE 12 GATES LEFT ALONE WERE ALSO MEASURED, not assumed: all seven carrying an inline '--confcutdir=.swarm-loop/acceptance' run rc=0 with no variable at all, because the flag stops pytest walking up to the root conftest; four are vitest-only; T-122 already carried the variable. A SEPARATE DEFECT IS RECORDED AND NOT FIXED HERE: seven gates (T-024, T-051, T-082, T-083, T-084, T-086, T-087) name a test file that does not exist and exit rc=4 'no tests ran' — the SAME exit code as the D38 abort, so a checker keying on the return code cannot tell a red gate from a broken one from one pointing at nothing. Their environment and interpreter are corrected here; their real problem is that the ticket is unbuilt and the gate names a file its lane must create. No metric, target, acceptance test, product file, dependency or ticket dependency changed.". Full record: `.swarm-loop/freeze-log.jsonl`.

| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |
|---|---|---|---|---|---|---|---|
| e8_proofs_passing | converging_on_track | cycle 16 | 7 | 8 | 1 | -0.551471 | 0 |
| acceptance_pass_rate | converging_on_track | cycle 16 | 99.17 | 100 | 0.83 | -6.05618 | 0 |
| acceptance_collected | at_target | cycle 16 | 120 | 120 | 0 | – | – |
| build_succeeds | at_target | cycle 16 | 1 | 1 | 0 | – | – |
| spec_criteria_passing | at_target | cycle 16 | 8 | 8 | 0 | – | – |
| e1_foundation_passing | at_target | cycle 16 | 10 | 10 | 0 | – | – |
| e2_ingestion_passing | at_target | cycle 16 | 8 | 8 | 0 | – | – |
| e3_exchange_passing | at_target | cycle 16 | 21 | 21 | 0 | – | – |
| e4_store_agent_passing | at_target | cycle 16 | 20 | 20 | 0 | – | – |
| e5_merchant_passing | at_target | cycle 16 | 10 | 10 | 0 | – | – |
| e6_trust_passing | at_target | cycle 16 | 26 | 26 | 0 | – | – |
| e7_buyer_passing | at_target | cycle 16 | 9 | 9 | 0 | – | – |

**RE-MEASURE OWED — cycle(s) 7. A `freeze --amend` moved at least one target and the baseline it moved has not been re-measured since. All-targets-met is WITHHELD until 'measure --cycle <n>' has run for each: an amendment invalidates its own baseline, and a hand-entered 'record' does not satisfy it.**

_Note: the stored `error` column disagreed with the current target for acceptance_collected, e3_exchange_passing, e4_store_agent_passing, e6_trust_passing, e8_proofs_passing; every error above is recomputed from `value` against the target in force now, never read from history._

