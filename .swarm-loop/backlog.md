# ProxyShop — ticket ledger (open tickets + scheduling constraints)

Seeded verbatim from `tickets.json` (the executable source of truth). T-IDs, dependencies,
scopes and non-goals preserved. A closed ticket LEAVES this file the epoch it closes,
surviving only as a one-line entry in that epoch's report and in git history.

**44 tickets · 133 acceptance criteria · 8 epics.**

Columns: depth = longest dependency chain from T-000. unblocks = tickets transitively
blocked by this one (its scheduling leverage).

| ID | epic | title | depth | unblocks | parallel_safe | depends_on | verify |
|---|---|---|---|---|---|---|---|
| **T-000** | E1 | Monorepo skeleton with green empty verify pipeline | 0 | 43 | no | — | `make verify` |
| **T-010** | E1 | Contracts generate typed models and enforce the dual-path bid boundary | 1 | 26 | no | T-000 | `pytest packages/contracts -q && npx vitest run packages/contracts` |
| **T-011** | E1 | Postgres schemas enforce role isolation and hash-chained ledger | 1 | 24 | yes | T-000 | `pytest apps/trust/tests/test_schema_grants.py apps/trust/tests/test_ledger_chain.py -q` |
| **T-012** | E1 | Neo4j attribute-node catalog with vector retrieval through EmbeddingProvider | 1 | 18 | yes | T-000 | `pytest services/ingest/tests/test_graph.py -q` |
| **T-013** | E1 | Shopify stub reproduces the exact surface the system uses | 1 | 24 | yes | T-000 | `pytest services/shopify-stub -q` |
| **T-014** | E1 | LLM client with per-role config, cache-first prompts, and test doubles | 1 | 23 | yes | T-000 | `pytest packages/llm -q` |
| **T-020** | E2 | Signed fetch adapter ingests a storefront including password-protected dev stores | 2 | 16 | no | T-000, T-012 | `pytest services/ingest/tests/test_signed_fetch.py -q` |
| **T-030** | E3 | Auctions fan out, time out, and always represent every store | 2 | 13 | no | T-010, T-013 | `pytest apps/exchange/tests/test_auction.py -q` |
| **T-040** | E4 | Tool hooks are the only way facts and discounts enter a bid | 2 | 11 | no | T-010, T-011 | `pytest packages/store-agent/tests/test_hooks.py -q` |
| **T-050** | E5 | Installing the app wires pixel and webhooks against the stub | 2 | 13 | no | T-013 | `npx vitest run apps/merchant && pytest apps/merchant/svc/tests/test_install.py -q` |
| **T-060** | E6 | Every event lands once, chained, and replays exactly | 2 | 14 | no | T-011 | `pytest apps/trust/tests/test_events.py -q` |
| **T-070** | E7 | Buyers authenticate lightly and stores never see who they are | 2 | 5 | no | T-010, T-011 | `npx vitest run apps/buyer && pytest apps/buyer/svc/tests/test_auth_vault.py -q` |
| **T-080** | E8 | The approved manifest and seed generator define ground truth | 2 | 12 | yes | T-013 | `pytest fixtures/tests/test_seed.py -q` |
| **T-021** | E2 | Policy pages and marketing claims land in the graph with provenance | 3 | 13 | no | T-014, T-020 | `pytest services/ingest/tests/test_extraction.py -q` |
| **T-022** | E2 | Same products across stores link via entity resolution | 3 | 0 | yes | T-012, T-020 | `pytest services/ingest/tests/test_entity_resolution.py -q` |
| **T-023** | E2 | Catalog MCP adapter passes recorded-contract tests | 3 | 1 | yes | T-020 | `pytest services/ingest/tests/test_catalog_mcp.py -q` |
| **T-031** | E3 | Candidate retrieval and fit scoring feed the ranker | 3 | 7 | yes | T-012, T-030 | `pytest apps/exchange/tests/test_retrieval.py -q` |
| **T-041** | E4 | Advocate runtime bids within walls and defaults deterministically cold | 3 | 10 | no | T-014, T-040 | `pytest packages/store-agent/tests/test_runtime.py -q` |
| **T-051** | E5 | Pixel reports checkout outcomes the collector can join | 3 | 6 | no | T-050 | `npx vitest run pixel && pytest apps/merchant/svc/tests/test_collector.py -q` |
| **T-052** | E5 | Winning offers become single-use validated codes and permalinks | 3 | 9 | no | T-050 | `pytest apps/merchant/svc/tests/test_codes.py -q` |
| **T-053** | E5 | A plain-language interview yields an approved, versioned envelope | 3 | 1 | yes | T-014, T-050 | `pytest apps/merchant/svc/tests/test_onboarding.py -q` |
| **T-071** | E7 | Three questions or fewer produce a confirmed structured intent | 3 | 4 | no | T-014, T-070 | `pytest apps/buyer/svc/tests/test_intent.py -q && npx vitest run apps/buyer/app/intent` |
| **T-024** | E2 | Differential refresh keeps the graph current at field-appropriate cadence | 4 | 0 | no | T-021, T-023 | `pytest services/ingest/tests/test_refresh.py -q` |
| **T-033** | E3 | Accepting an offer produces a validated code and permalink | 4 | 7 | no | T-030, T-052 | `pytest apps/exchange/tests/test_accept.py -q` |
| **T-042** | E4 | Store loop learns from its own outcomes only | 4 | 3 | yes | T-041 | `pytest packages/store-agent/tests/test_learning.py -q` |
| **T-043** | E4 | Shadow mode logs would-be bids and trust events adjust the agent | 4 | 3 | yes | T-041 | `pytest packages/store-agent/tests/test_shadow_trust.py -q` |
| **T-044** | E4 | External bids enter signed and route to verification, not rejection | 4 | 1 | yes | T-041 | `pytest packages/store-agent/tests/test_external_bids.py -q` |
| **T-061** | E6 | Webhook truth reconciles lossy pixel signals | 4 | 2 | no | T-051, T-060 | `pytest apps/trust/tests/test_reconciliation.py -q` |
| **T-065** | E6 | A golden pitch yields all four verification statuses with evidence | 4 | 11 | no | T-010, T-021, T-060 | `pytest packages/verification apps/trust/tests/test_verification.py -q` |
| **T-032** | E3 | Shortlists rank by the single published formula behind eligibility filters | 5 | 6 | no | T-031, T-065 | `pytest apps/exchange/tests/test_ranking.py -q` |
| **T-045** | E4 | Three seller personas exercise the external door adversarially | 5 | 0 | yes | T-014, T-044, T-080 | `pytest apps/seller-reference -q` |
| **T-062** | E6 | Trust merges verification and outcome observations against the approved manifest | 5 | 8 | no | T-060, T-065, T-080 | `pytest apps/trust/tests/test_scoring.py -q` |
| **T-072** | E7 | Shortlists render with provenance and accept hands off cleanly | 5 | 3 | no | T-033, T-071 | `npx vitest run apps/buyer/app/shortlist && pytest apps/buyer/svc/tests/test_accept_flow.py -q` |
| **T-081** | E8 | Simulated buyers exercise the whole network from one seed | 5 | 4 | no | T-033, T-051, T-080 | `pytest services/sim -q` |
| **T-035** | E3 | Loss reports aggregate reasons without leaking amounts | 6 | 1 | yes | T-032 | `pytest apps/exchange/tests/test_loss_reports.py -q` |
| **T-063** | E6 | Trust events reach the store agent and buyers close the loop | 6 | 2 | no | T-043, T-062 | `pytest apps/trust/tests/test_feedback_push.py -q` |
| **T-064** | E6 | Exchange consumes one snapshot shape for trust and exploration | 6 | 4 | yes | T-062 | `pytest apps/trust/tests/test_snapshot.py -q` |
| **T-082** | E8 | One scripted run proves the full S1 flow | 6 | 1 | no | T-061, T-072, T-081 | `pytest e2e/test_s1_flow.py -q` |
| **T-034** | E3 | Exchange bandit shifts exposure with outcomes and honors exploration | 7 | 3 | no | T-032, T-064 | `pytest apps/exchange/tests/test_bandit.py -q` |
| **T-054** | E5 | The dashboard shows the walls and the window | 7 | 0 | no | T-035, T-043, T-052, T-053, T-063 | `npx vitest run apps/merchant/app/dashboard` |
| **T-073** | E7 | Routed buyers can answer one structured feedback prompt | 7 | 0 | yes | T-063, T-072 | `npx vitest run apps/buyer/app/feedback && pytest apps/buyer/svc/tests/test_feedback.py -q` |
| **T-083** | E8 | Both learning loops demonstrably move under seeded outcomes | 8 | 2 | no | T-034, T-042, T-081 | `pytest e2e/test_learning.py -q` |
| **T-084** | E8 | The dishonest store ends below threshold and off the shortlist | 9 | 1 | no | T-062, T-083 | `pytest e2e/test_dishonest.py -q` |
| **T-085** | E8 | The demo is a runbook anyone on the team can execute | 10 | 0 | yes | T-082, T-084 | `pytest docs/tests/test_runbook.py -q` |

## Status

All 44 tickets OPEN. None dispatched yet.

## Scheduling constraints

_(populated from the intake report before first dispatch)_

