# Cycle 13/102 analysis

Targets met: 5/12

Harness integrity at analysis time: **intact** — every frozen file re-hashed against `.swarm-loop/manifest.json`, which is itself reconciled against the append-only `.swarm-loop/freeze-log.jsonl`.

> **GOALPOSTS MOVED 6x — this trend is not against a single fixed target.** Last amendment 2026-09-03T11:11:47: "ESC-007, authorized by Hank in the cycle-13 handoff ('identify an unkeyed node by its provenance and add _hook_identities ... verify both directions, then freeze --amend'). The frozen E4 helper _claim_identity keyed every provenance-bearing node on (key, value, source, ref); a Discount has no 'key', so str(node.get('key')) collapsed EVERY discount onto the single identity 'None' and a legitimate hook-granted discount read as SMUGGLED. Latent only because the fixture context is cold (learned_policy=None, no intro rule) so no discount is produced; T-042/T-043 build discounted bids and would have turned it red on CORRECT code. Amendment: an unkeyed node is identified as ('<unkeyed>', value, source, ref), and _hook_identities lends that unkeyed slot ONLY to the authorized_discount_pct grant (DISCOUNT_GRANT_KEY) - a first draft lent it to every hook claim, which an adversarial verifier measured as admitting a 100%-off discount wearing a scraped list_price provenance; that is repaired and now permanently asserted. WEAKENS NOTHING, verified in the primary tree by me: per-test results are IDENTICAL before and after (15 failed / 5 passed, same 5 passing), and disabling the new guard turns test_every_claim_in_a_hosted_bid_traces_to_a_hook_call RED, so the restriction is load-bearing rather than decorative. The file gains assertions it did not have: a fabricated-provenance forgery, a deeper-than-granted forgery, a forged ref, and a loop over EVERY non-grant hook claim the auction emits, guarded by 'assert non_grant' so the loop cannot pass vacuously.". Full record: `.swarm-loop/freeze-log.jsonl`.

| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |
|---|---|---|---|---|---|---|---|
| acceptance_pass_rate | converging_on_track | cycle 13 | 63.33 | 100 | 36.67 | -4.9836 | 0 |
| e4_store_agent_passing | converging_on_track | cycle 13 | 5 | 20 | 15 | -0.461538 | 0 |
| e3_exchange_passing | converging_on_track | cycle 13 | 9 | 21 | 12 | -0.843956 | 0 |
| e7_buyer_passing | converging_on_track | cycle 13 | 2 | 9 | 7 | -0.210989 | 0 |
| e5_merchant_passing | converging_on_track | cycle 13 | 4 | 10 | 6 | -0.408791 | 0 |
| e2_ingestion_passing | converging_on_track | cycle 13 | 6 | 8 | 2 | -0.613187 | 0 |
| e8_proofs_passing | converging_on_track | cycle 13 | 6 | 8 | 2 | -0.58022 | 0 |
| acceptance_collected | at_target | cycle 13 | 120 | 120 | 0 | – | – |
| build_succeeds | at_target | cycle 13 | 1 | 1 | 0 | – | – |
| spec_criteria_passing | at_target | cycle 13 | 8 | 8 | 0 | – | – |
| e1_foundation_passing | at_target | cycle 13 | 10 | 10 | 0 | – | – |
| e6_trust_passing | at_target | cycle 13 | 26 | 26 | 0 | – | – |

**RE-MEASURE OWED — cycle(s) 7. A `freeze --amend` moved at least one target and the baseline it moved has not been re-measured since. All-targets-met is WITHHELD until 'measure --cycle <n>' has run for each: an amendment invalidates its own baseline, and a hand-entered 'record' does not satisfy it.**

_Note: the stored `error` column disagreed with the current target for acceptance_collected, e3_exchange_passing, e4_store_agent_passing, e6_trust_passing, e8_proofs_passing; every error above is recomputed from `value` against the target in force now, never read from history._

