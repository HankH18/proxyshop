# Cycle 1/102 analysis

Targets met: 3/12

Harness integrity at analysis time: **intact** — every frozen file re-hashed against `.swarm-loop/manifest.json`, which is itself reconciled against the append-only `.swarm-loop/freeze-log.jsonl`.
- note: .swarm-loop/goals.json cannot be reconciled against .swarm-loop/freeze-log.jsonl — this run was frozen before the canonical goals digest existed, so a moved target is caught only by manifest.json's own digest. The next 'freeze --amend' records it and closes the gap.

> **GOALPOSTS MOVED 2x — this trend is not against a single fixed target.** Last amendment 2026-09-01T14:45:56: "Amendment 2, user-approved (ESC-002). test_compose_declares_healthchecked_postgres_neo4j_redis_and_separate_deployables was UNSATISFIABLE: it read services only from the root docker-compose.yml via a textual parser that never followed 'include:', while the four deployables (buyer/merchant/exchange/trust) live in per-lane fragments by design — the root file's own header states a service ticket fills its own fragment and no ticket ever edits the root. So the test could never pass whatever any ticket built, pinning acceptance_pass_rate (target 100, tolerance 0) permanently below target and spec_criteria_passing at 7/8. Fix: two new helpers, _compose_include_paths() parses the include: block textually (no PyYAML dependency, consistent with _compose_services), and _compose_stack_services() unions the root's services with every included fragment's; the test now reads that union. Verified in both directions before re-freezing: with the four fragments as shipped (empty 'services: {}' stubs) the test is RED for the correct reason, and with the four temporarily filled it is GREEN — so the amendment makes C1 reachable without making it free. No assertion was weakened, no test renamed, no test added or removed; the suite stays at 120. Only test_spec_criteria.py changed.". Full record: `.swarm-loop/freeze-log.jsonl`.

| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |
|---|---|---|---|---|---|---|---|
| e6_trust_passing | stalled | cycle 1 | 0 | 26 | 26 | 0 | 26 |
| e3_exchange_passing | stalled | cycle 1 | 0 | 21 | 21 | 0 | 21 |
| e4_store_agent_passing | stalled | cycle 1 | 0 | 20 | 20 | 0 | 20 |
| e5_merchant_passing | stalled | cycle 1 | 0 | 10 | 10 | 0 | 10 |
| e7_buyer_passing | stalled | cycle 1 | 0 | 9 | 9 | 0 | 9 |
| e2_ingestion_passing | stalled | cycle 1 | 0 | 8 | 8 | 0 | 8 |
| e8_proofs_passing | stalled | cycle 1 | 0 | 8 | 8 | 0 | 8 |
| acceptance_pass_rate | converging_on_track | cycle 1 | 13.33 | 100 | 86.67 | -10 | 0 |
| spec_criteria_passing | converging_on_track | cycle 1 | 6 | 8 | 2 | -2 | 0 |
| acceptance_collected | at_target | cycle 1 | 120 | 120 | 0 | – | – |
| build_succeeds | at_target | cycle 1 | 1 | 1 | 0 | – | – |
| e1_foundation_passing | at_target | cycle 1 | 10 | 10 | 0 | – | – |

_Note: the stored `error` column disagreed with the current target for acceptance_collected, e3_exchange_passing, e4_store_agent_passing, e6_trust_passing, e8_proofs_passing; every error above is recomputed from `value` against the target in force now, never read from history._

Priority for next cycle (worst first): e6_trust_passing, e3_exchange_passing, e4_store_agent_passing, e5_merchant_passing, e7_buyer_passing, e2_ingestion_passing, e8_proofs_passing
