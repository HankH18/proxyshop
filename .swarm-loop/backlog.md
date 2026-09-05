# Backlog ledger — REGENERATED from tickets.json

- generated: `2026-09-05T21:01:50Z`  ·  main sha: `7396f8f`
- source of truth: `tickets.json` (repo root)
- closure source: `python3 ~/.claude/skills/swarm-loop/scripts/swarmloop.py frontier --closed-from-ledger --closed-from-merged --base main --json`
- rejection reasons: `.swarm-loop/dispatch.jsonl` — 295 record(s), sha256 `a540431599420de318807942f148543d484316d62d0e07f03946f39e19e933fb`

**GRAPH tickets.json IS GROUND TRUTH; this file is derived from it. Regenerate on every amendment.**

## Counts (exact, derived)

| metric | value |
| --- | --- |
| tickets in graph | 296 |
| closed | 145 |
| open | 151 |
| &nbsp;&nbsp;· READY (dispatchable) | 149 |
| &nbsp;&nbsp;· BLOCKED | 2 |
| max dependency depth (whole graph) | 10 |
| max dependency depth (open tickets) | 10 |
| open tickets with NO-GATE placeholder verify | 96 |
| &nbsp;&nbsp;· of which READY | 96 |
| NO-GATE placeholder across the whole graph | 143 |
| distinct scope paths claimed by >1 open ticket | 23 |
| scope paths with >1 EFFECTIVE (glob-aware) owner | 62 |

Closed tickets are deliberately absent (ledger hygiene: a closed ticket leaves the ledger).
`NO-GATE` marks the placeholder verify `false  # NO GATE YET — write one that fails on this finding first`;
such a ticket cannot be red-checked and must have a real gate written before it is dispatched.
A ticket's own `status` field is NOT consulted anywhere in this file: the graph carries
128 `no status field`, 96 `closed`, 66 `open`, 6 `unknown`, and the harness ignores all of it. Closure comes only from the command above.

Closure additionally named 3 id(s) that are NOT in the graph, so they count toward
nothing above and close nothing: `demo-runbook-executability`, `repro-buyer-merchant`, `repro-core-packages`.

## READY (dispatchable) — 149

Ordered as `frontier` emits them (by what each unblocks — structural, NOT a priority ruling).
A ready set is NOT a wave: run `check-wave <ids>` before dispatching, and `red-check` before trusting any verify.

### T-051 — depth 3 · unblocks 7
- title: Pixel reports checkout outcomes the collector can join
- scope: `pixel/**`, `apps/merchant/svc/src/collector/**`, `apps/merchant/svc/tests/**`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-1} && npx vitest run pixel && uv run python -m pytest apps/merchant/svc/tests/test_collector.py -q`

### T-083 — depth 8 · unblocks 2
- title: Both learning loops demonstrably move under seeded outcomes
- scope: `e2e/**`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-14} uv run python -m pytest e2e/test_learning.py -q`

### T-140 — depth 0 · unblocks 0
- title: AccountDirectory has exactly one implementation and no production populator. build_auth_service() never passe…
- scope: `apps/buyer/svc/src/auth/magic_link.py`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-9} uv run python -m pytest apps/buyer/svc/tests/test_repro_open_tickets.py -q --runxfail -k t140`

### T-141 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The magic-link token is delivered to the default no-op `_drop` (line 166) in every deployment, and set_auth_s…
- scope: `apps/buyer/svc/src/auth/magic_link.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-142 — depth 0 · unblocks 0
- title: publish_profile — the only writer of app.buyer_accounts, described as 'the store-visible working set' — has z…
- scope: `apps/buyer/svc/src/profile/__init__.py`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-3} uv run python -m pytest apps/buyer/svc/tests/test_repro_open_tickets.py -q --runxfail -k t142`

### T-148 — depth 0 · unblocks 0  **[NO-GATE]**
- title: `configure_auctions` — the only way to give the exchange a real solicitor, a real eligibility source, a Redis…
- scope: `apps/exchange/src/auction/routes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-150 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The ledger writer has zero producers. Nothing in the repository writes an event into it - not by import, not …
- scope: `apps/exchange/src/auction/ledger.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-156 — depth 0 · unblocks 0
- title: offer.total_price is never reconciled against unit_price: an offer stating unit_price=80.0 with total_price=1…
- scope: `packages/store-agent/src/hooks/provenance.py (_price_reconciliation_refusal)`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-5} uv run python -m pytest packages/store-agent/tests/test_repro_open_tickets.py -q --runxfail -k t156`

### T-159 — depth 0 · unblocks 0  **[NO-GATE]**
- title: SYSTEMIC GATE-CREDIBILITY DEFECT: tests that swallow a missing subject with `except ImportError` are VACUOUS …
- scope: `apps/exchange/tests/test_checkout_provider.py:406 (and repo-wide)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-160 — depth 0 · unblocks 0
- title: These three tickets' recorded `verify` commands pass IDENTICALLY with and without their defect, which is the …
- scope: `tickets.json (T-109, T-111, T-123 verify fields)`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-8} uv run python -m pytest proxyshop_support/tests/test_repro_open_tickets.py -q --runxfail -k t160`

### T-161 — depth 0 · unblocks 0
- title: A provenance block nested inside a hook-provenanced claim's opaque `value` is not walked. Measured: make_bid(…
- scope: `packages/contracts/src/boundary.py:157 (_source_verdict)`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-1} && uv run python -m pytest packages/contracts/tests/test_repro_open_tickets.py -q --runxfail -k test_a_provenance_nested_in_a_claim_value_is_not_invisible_to_the_hosted_door`

### T-162 — depth 0 · unblocks 0
- title: The boundary checks provenance SOURCE but never discount AUTHORISATION. make_bid(claims=[make_claim("policy",…
- scope: `packages/contracts/src/boundary.py (validate_bid) — external path`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-9} && uv run python -m pytest packages/contracts/tests/test_repro_open_tickets.py -q --runxfail -k test_an_external_submitters_self_asserted_discount_authorisation_is_not_taken_on_trust`

### T-163 — depth 0 · unblocks 0
- title: T-133 item (c): SessionStore.open() validates only that the subject carries the 'psn-' PREFIX — it never chec…
- scope: `apps/buyer/svc/src/auth/sessions.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-9} && uv run python -m pytest apps/buyer/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t163_a_session_subject_that_no_vault_ever_issued_cannot_open_a_session`

### T-164 — depth 0 · unblocks 0
- title: build_buckets() and anonymise_cohort() are public and run NO identity-leak check; only build_profile()/build_…
- scope: `apps/buyer/svc/src/profile/__init__.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-9} && uv run python -m pytest apps/buyer/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t164_no_public_bucket_builder_publishes_identity_without_the_backstop`

### T-165 — depth 0 · unblocks 0
- title: No rate limiter on the unauthenticated magic-link endpoint, and the session/pending-link store behind it is p…
- scope: `apps/buyer/svc/src/auth/routes.py (POST /buyer/auth/magic-link) + routes.py:57 ProcessLocalStateUnsafe`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-8} && uv run python -m pytest apps/buyer/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t165_repeated_unauthenticated_magic_link_requests_are_eventually_refused`

### T-166 — depth 0 · unblocks 0
- title: test_replay_with_snapshots_names_the_missing_scorer branches on `if response.status_code == 503`. Now that T-…
- scope: `apps/trust/tests/test_events.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-7} && uv run python -m pytest apps/trust/tests/test_repro_open_tickets.py -q --runxfail -k test_the_replay_snapshot_test_does_not_branch_on_a_status_it_can_never_reach`

### T-171 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The D37 Neo4j lock is MACHINE-GLOBAL (/private/tmp/proxyshop-neo4j.lock) and PROXYSHOP_WORKER does not isolat…
- scope: `proxyshop_support/neo4j_lock.py:79,119-131 + pyproject.toml:71 + conftest.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-172 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-109's per-service inference has an 8-item hole and every item in it is a Postgres-only test that a Redis-on…
- scope: `proxyshop_support/service_markers.py:78-85 (whole-stack fallback)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-175 — depth 0 · unblocks 0  **[NO-GATE]**
- title: Neither the new price-reconciliation wall nor the pre-existing floor wall looks at Offer.total_price (PRICE_F…
- scope: `packages/store-agent/src/hooks/provenance.py:830-834 vs DESIGN.md`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-181 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-151's DSN ORDER RESTS ON A SINGLE ASSERTION IN A SINGLE TEST OF 327. Sabotage S3a (reorder the tuple, i.e. …
- scope: `apps/trust/tests/test_events_hardening.py (DSN precedence coverage)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-192 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE 26/26 THAT MOVED E6 TO TARGET IS A WEAKER INSTRUMENT THAN THE LANE'S OWN UNIT SUITE. 7 of 19 real mutatio…
- scope: `.swarm-loop/acceptance/test_e6_trust.py (the E6 metric as an instrument)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-194 — depth 0 · unblocks 0
- title: THE TWO DOORS DO NOT RUN THE SAME SCHEMA CHECK — 21 measured ok-divergences. TypeScript validates through ajv…
- scope: `packages/contracts/src/ts/schemas.ts:63-65 (ajv + ajv-formats) vs packages/contracts/src/boundary.py:353-365 (pydantic)`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-5} && uv run python -m pytest packages/contracts/tests/test_repro_open_tickets.py -q --runxfail -k test_the_python_door_enforces_the_date_time_format_the_typescript_door_enforces`

### T-197 — depth 0 · unblocks 0
- title: Identity fragments shorter than 4 characters are never tracked ANYWHERE in the leak backstop, so short names …
- scope: `apps/buyer/svc/src/profile/__init__.py:436 (_MIN_LEAKABLE = 4)`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-3} && uv run python -m pytest apps/buyer/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t197_a_three_letter_name_in_a_category_slug_is_still_a_leak`

### T-198 — depth 0 · unblocks 0
- title: Two residual identity channels the T-189 fix deliberately stopped short of. (a) A number REGROUPED rather tha…
- scope: `apps/buyer/svc/src/profile/__init__.py:918 (_identity_sources) and`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-10} && uv run python -m pytest apps/buyer/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t198_a_a_regrouped_phone_number_in_a_category_slug_is_still_a_leak`

### T-199 — depth 0 · unblocks 0
- title: The bucket vocabulary holds every CATEGORY_TAXONOMY label out of the leak haystack on the grounds that they a…
- scope: `apps/buyer/svc/src/profile/__init__.py:1038 (_BUCKET_VOCABULARY['category_affinity'])`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-7} && uv run python -m pytest apps/buyer/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t199_a_surname_that_is_also_a_taxonomy_label_is_still_a_leak`

### T-203 — depth 0 · unblocks 0  **[NO-GATE]**
- title: BINDING REQUIREMENT ON A SCHEDULED TICKET, recorded the way T-041 carried T-152's. The percent-vs-fraction mi…
- scope: `apps/merchant/svc/src/codes/ (T-052) — binding requirement, not a defect`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-204 — depth 0 · unblocks 0
- title: REFUSAL REASONS ARE UNENUMERATED AND NOTHING ASSERTS ON THEM. accept() formats type(exc).__name__ into denial…
- scope: `packages/contracts/openapi/exchange.openapi.json:169 (denial_reason) + apps/exchange/src/accept/offer.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-11} && uv run python -m pytest apps/exchange/tests/test_repro_open_tickets.py packages/contracts/tests/test_repro_open_tickets.py -q --runxfail -k "test_t204_a_denial_reason_is_drawn_from_a_declared_vocabulary or test_the_published_denial_reason_has_an_enumerated_vocabulary"`

### T-205 — depth 0 · unblocks 0
- title: SECOND LATENT except-ImportError VACUITY, found by the AST sweep T-159 asked for. The whole module skips if s…
- scope: `services/shopify-stub/tests/test_stub_contract.py:40 via conftest.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-2} && uv run python -m pytest services/shopify-stub/tests/test_repro_open_tickets.py -q --runxfail -k test_the_stub_url_fixture_does_not_turn_a_broken_import_into_a_skip`

### T-207 — depth 0 · unblocks 0
- title: The engine's gloss on the weight table CONTRADICTS THE APPROVED MANIFEST on what mismatch_return means. The c…
- scope: `apps/trust/src/scoring/engine.py (weight-table comment) vs fixtures/manifest.json observation_weights`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-1} && uv run python -m pytest apps/trust/tests/test_repro_open_tickets.py -q --runxfail -k test_the_mismatch_return_gloss_agrees_with_the_approved_manifest`

### T-208 — depth 0 · unblocks 0  **[NO-GATE]**
- title: There is no published weight for a buyer complaint that is NOT corroborated by a return. It currently lands a…
- scope: `fixtures/manifest.json observation_weights — no weight for an uncorroborated complaint`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-209 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE CONTRACTS BOUNDARY WAS TIGHTENED AND THE STORE-AGENT'S OWN AUDITOR WAS NOT. T-195's fix regenerates the m…
- scope: `packages/store-agent/src/hooks/provenance.py:129,:496-507 (commitments walk, no pydantic gate)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-210 — depth 0 · unblocks 0
- title: TWO CORRECTIONS, ONE OF THEM TO MY OWN ASSERTION. (1) I claimed the chflags window could corrupt a metric rea…
- scope: `ORCHESTRATION — measuring while the swarm runs (supersedes my T-174 rationale)`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-9} uv run python -m pytest proxyshop_support/tests/test_repro_open_tickets.py -q --runxfail -k t210`

### T-212 — depth 0 · unblocks 0
- title: FOURTH independent sighting of the InvalidSchemaName flake, from a fourth lane and a fourth worker, again in …
- scope: `apps/trust/tests/test_events_hardening.py — FOURTH sighting, plus the reason it cannot skip`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-2} && uv run python -m pytest apps/trust/tests/test_repro_open_tickets.py -q --runxfail -k test_every_database_backed_hardening_test_carries_the_docker_marker`

### T-218 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-209 CONFIRMED BUT UNDERSTATED, and the cause is generic rather than specific to commitments. The store-agen…
- scope: `packages/store-agent/src/hooks/provenance.py:523 (`if node is None: return`) — T-209 is one of a family of at least six`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-219 — depth 0 · unblocks 0  **[NO-GATE]**
- title: NOTHING CONSUMES THE SELECTION COUNT, so T-117's defect is closed for the `docker` marker but not as a class.…
- scope: `scripts/verify.sh SELECTION line + .swarm-loop/goals.json build_succeeds — T-117 acceptance 2 is only literally satisfied`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-220 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The cycle-detection replacement for MAX_SWEEP_DEPTH is correct, but nothing in the suite would notice a depth…
- scope: `packages/store-agent/tests/test_price_reconciliation.py (depth parametrization) — cycle detection has an invisible ceiling`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-221 — depth 0 · unblocks 0
- title: THE K-ANONYMITY FLOOR T-138 DELIVERED IS SWITCHED OFF BY DEFAULT, so the re-linkability T-138 exists to preve…
- scope: `apps/buyer/svc/src/profile/__init__.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-12} && uv run python -m pytest apps/buyer/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t221_the_k_anonymity_floor_is_on_by_default_and_reaches_the_production_path`

### T-225 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A TEST THAT ASSERTS NOTHING AND REPORTS GREEN, in three copies. `test_scope_directories_exist` reads `for rel…
- scope: `apps/seller-reference/tests/test_scaffold_smoke.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-226 — depth 0 · unblocks 0  **[NO-GATE]**
- title: EVERY MEMBER'S tests/ DIRECTORY IS OUTSIDE THE TYPE GATE, so type errors in tests never reach `make types`. R…
- scope: `pyproject.toml`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-227 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE FROZEN TEST AND THE PRODUCT CAN SELECT DIFFERENT MANIFEST DOCUMENTS, so the suite would grade one documen…
- scope: `.swarm-loop/acceptance/test_e4_store_agent.py::_load_fixture_manifest vs apps/seller-reference/src/personas/__init__.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-236 — depth 0 · unblocks 0
- title: THE INGESTION PIPELINE DOES NOT EXIST AS A RUNNING THING. No production code constructs any CatalogAdapter. s…
- scope: `services/ingest/src/main.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-7} && uv run python -m pytest services/ingest/tests/test_repro_open_tickets.py -q --runxfail -k test_the_running_ingest_app_can_reach_a_catalog_adapter_that_is_actually_built`

### T-238 — depth 0 · unblocks 0
- title: BARE urlsplit ON ATTACKER-CONTROLLED REDIRECT TARGETS. netguard.py:398 and transport.py:353 call bare urlspli…
- scope: `services/ingest/src/adapters/netguard.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-9} && uv run python -m pytest services/ingest/tests/test_repro_open_tickets.py -q --runxfail -k test_a_malformed_redirect_location_is_refused_rather_than_raised`

### T-239 — depth 0 · unblocks 0
- title: ENVELOPE VERSION HISTORY IS PROCESS-LOCAL AND DIES WITH THE PROCESS. EnvelopeVersions keeps append-only histo…
- scope: `apps/merchant/svc/src/envelope/store.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-2} && uv run python -m pytest apps/merchant/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t239_the_envelope_version_store_has_a_durability_seam`

### T-240 — depth 0 · unblocks 0
- title: THE PINNED MERCHANT CONTRACT HAS NOWHERE TO PUT AN ENVELOPE APPROVAL ARTIFACT, AND ITS OWN EXAMPLE INVITES SE…
- scope: `packages/contracts/openapi/merchant.openapi.json`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-10} && uv run python -m pytest packages/contracts/tests/test_repro_open_tickets.py -q --runxfail -k test_the_pinned_merchant_contract_can_express_an_envelope_approval`

### T-242 — depth 0 · unblocks 0
- title: THE SIMULATION VALIDATES ONLY HALF ITS OWN LEDGER, AND THE HALF IT SKIPS CONTAINS THE EXACT DEFECT CLASS THE …
- scope: `services/sim/src/runner.py`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-1} uv run python -m pytest services/sim/tests/test_repro_open_tickets.py -q --runxfail -k t242`

### T-243 — depth 0 · unblocks 0
- title: THE TWO-LEDGERS-UNDER-TWO-SPELLINGS HOLE IS PRESENT IN MERCHANT, AND T-071'S FIX DID NOT COVER IT. Measured a…
- scope: `apps/merchant/svc/src/envelope/store.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-8} && uv run python -m pytest apps/merchant/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t243_the_merchant_envelope_store_has_exactly_one_module_identity`

### T-244 — depth 0 · unblocks 0  **[NO-GATE]**
- title: MEASUREMENT CREDIBILITY: receive_bid has 28 callers and ZERO of them are production. swarmloop reachable retu…
- scope: `packages/store-agent/src/external/door.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-245 — depth 0 · unblocks 0
- title: T-023'S NEW SHARED MAPPING HAS NEVER REACHED A GRAPH, AND E2'S GREEN OVER IT IS STRUCTURAL ONLY. apply_upsert…
- scope: `services/ingest/src/adapters/mapping.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-4} && uv run python -m pytest services/ingest/tests/test_repro_open_tickets.py -q --runxfail -k test_catalog_entries_with_no_identifier_do_not_collapse_onto_one_product_node`

### T-246 — depth 0 · unblocks 0
- title: NO PRODUCTION CONSUMER READS is_live, SO NOTHING PROVES A SHADOW OR KILLED STORE ACTUALLY STOPS BIDDING. is_l…
- scope: `apps/merchant/svc/src/envelope/model.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-2} && uv run python -m pytest apps/merchant/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t246_some_production_code_reads_the_envelope_activation_decision`

### T-247 — depth 0 · unblocks 0
- title: ORDER-DEPENDENT GLOBAL LEAK IN THE MERCHANT WEBHOOK SINK. webhooks.py:418 boots _sink = default_sink, but app…
- scope: `apps/merchant/svc/src/install/webhooks.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-11} && uv run python -m pytest apps/merchant/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t247_the_install_suite_leaves_the_webhook_sink_as_it_found_it`

### T-248 — depth 0 · unblocks 0
- title: EnvelopeVersions.record() ACCEPTS A CALLER-ASSERTED activation:'active' WITH NO APPROVAL ARTIFACT. Measured: …
- scope: `apps/merchant/svc/src/envelope/store.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-4} && uv run python -m pytest apps/merchant/svc/tests/test_repro_open_tickets.py -q --runxfail -k test_t248_an_envelope_cannot_be_recorded_live_without_an_approval_artifact`

### T-249 — depth 0 · unblocks 0
- title: T-023 BEHAVIOURAL DRIFT THAT NO TEST COVERS, and the 'moved VERBATIM' claim is PARTIAL. coerce_price now retu…
- scope: `services/ingest/src/adapters/mapping.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-3} && uv run python -m pytest services/ingest/tests/test_repro_open_tickets.py -q --runxfail -k test_a_refused_entry_price_does_not_promote_the_other_surfaces_price`

### T-251 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The test's comment asserts the repo root 'is not handed over ... on purpose', but the venv's _proxyshop.pth h…
- scope: `packages/contracts/tests/test_claim_identity.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-252 — depth 0 · unblocks 0  **[NO-GATE]**
- title: Imports from a scratch tree whose module name happens not to exist in the live checkout. Safe today purely by…
- scope: `proxyshop_support/tests/test_fixture_loader.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-253 — depth 0 · unblocks 0  **[NO-GATE]**
- title: utc_now() has zero callers repo-wide, and its docstring asserts an invariant that is false: it claims to exis…
- scope: `services/shopify-stub/src/codes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-254 — depth 0 · unblocks 0  **[NO-GATE]**
- title: ExtractedClaim.as_claim() has zero callers and zero tests. This is the DESIGN Claim projection ({key, value, …
- scope: `services/ingest/src/extraction/claims.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-255 — depth 0 · unblocks 0  **[NO-GATE]**
- title: DiscountCode.is_redeemable_at() has zero callers and zero tests. The live redemption path uses rejection() at…
- scope: `services/shopify-stub/src/codes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-256 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-065's PERSISTENCE HALF IS ENTIRELY UNIMPLEMENTED. Its objective requires verification results to persist to…
- scope: `apps/trust/src/verification/__init__.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-257 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-112's RECORDED GATE COLLECTS NONE OF ITS OWN GRADING TESTS. The gate is 'pytest apps/trust/tests/test_schem…
- scope: `tickets.json:T-112`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-258 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-102's gate is FULLY VACUOUS on any machine without docker. Every behavioural and catalog assertion for T-10…
- scope: `apps/trust/tests/test_ledger_chain.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-259 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-063's acceptance 1 grades a FIXTURE, not the system. Every push_trust_event call in this file and in the fr…
- scope: `apps/trust/tests/test_feedback_push.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-260 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The exchange's retrieval package has ZERO production callers. CandidateRetrieval at service.py:106 and record…
- scope: `apps/exchange/src/retrieval/service.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-261 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-064's acceptance criterion 3 -- the exchange client caches the trust snapshot and refreshes it on a version…
- scope: `apps/trust/src/snapshot/builder.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-262 — depth 0 · unblocks 0
- title: T-087's GATE NAMES A FILE ITS OWN SCOPE FORBIDS IT TO CREATE, and that file sits inside a DIFFERENT ticket's …
- scope: `tickets.json:T-087`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-7} uv run python -m pytest proxyshop_support/tests/test_repro_ticket_graph.py -q --runxfail -k t262`

### T-263 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A ticket whose DELIVERABLE IS ITS OWN GATE TARGET can never be red-checked at its merge base, so the retro re…
- scope: `tickets.json:T-082`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-264 — depth 0 · unblocks 0
- title: A MEMORY ADDRESS IS RENDERED INTO A PERSISTED, CLIENT-VISIBLE EVENT PAYLOAD. A TypeError raised from an unusa…
- scope: `apps/exchange/src/checkout/provider.py`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-1} uv run python -m pytest apps/exchange/tests/test_accept_denials.py -q --runxfail -k t264`

### T-265 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A TEST WHITELISTS THE DEFECT IT IS SUPPOSED TO GUARD, BY NAME, AND STAYS GREEN BOTH BEFORE AND AFTER THE REPA…
- scope: `services/sim/tests/test_simulation.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-266 — depth 0 · unblocks 0
- title: THE SERVED EXCHANGE IS MISSING FOUR PUBLISHED PATHS, NOT ONE. create_app().openapi() answers only ['/auctions…
- scope: `apps/exchange/src/main.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-8} && uv run python -m pytest apps/exchange/tests/test_repro_open_tickets.py -q --runxfail -k test_t312_the_exchange_serves_exactly_the_operations_its_contract_publishes`

### T-267 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The published OpenAPI example for denial_reason is 'blacklist', a value the code has never produced -- the ga…
- scope: `packages/contracts/openapi/exchange.openapi.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-268 — depth 0 · unblocks 0  **[NO-GATE]**
- title: Dead line: 'return parsed.timestamp()' is unreachable, sitting after a try block that either returns or raise…
- scope: `apps/exchange/src/checkout/codes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-269 — depth 0 · unblocks 0  **[NO-GATE]**
- title: expiry_epoch accepts three shapes that contracts.parse_timestamp refuses, so the two parsers disagree: True -…
- scope: `apps/exchange/src/checkout/codes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-270 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A caller-supplied NaN still produces an UNAUTHENTICATED HTTP 500. list_price: NaN and tier: 1e400 both return…
- scope: `apps/exchange/src/auction/routes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-271 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE PRICE FLOOR'S MAGNITUDE IS ESSENTIALLY UNPINNED -- mutation-tested, not inferred. Setting MINIMUM_PAYABLE…
- scope: `apps/exchange/src/auction/collect.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-272 — depth 0 · unblocks 0  **[NO-GATE]**
- title: _tier SILENTLY DOWNGRADES WELL-FORMED INPUT, and the line it replaced did not. _number excludes bool and str …
- scope: `apps/exchange/src/auction/collect.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-273 — depth 0 · unblocks 0  **[NO-GATE]**
- title: _price_is_unreadable TURNS A GOOD BID INTO A 0.00 FALLBACK on a roster row with no list_price. _number(offer.…
- scope: `apps/exchange/src/auction/collect.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-274 — depth 0 · unblocks 0  **[NO-GATE]**
- title: AMENDMENT 15 WIRED A RED GATE ONTO A CLOSED, REFUTED TICKET. T-212 in tickets.json carries status 'closed' wi…
- scope: `tickets.json:T-212`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-275 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-237'S GATE CANNOT SEE ITS OWN FIX. Its _KIND_EMISSION pattern matches only QUOTED STRING LITERALS, but the …
- scope: `apps/trust/tests/test_repro_open_tickets.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-276 — depth 0 · unblocks 0  **[NO-GATE]**
- title: AN UNTESTED CHEAP-PRODUCT BAND, where the floor refuses everything. _price_floor(0.01) == 0.01, so a product …
- scope: `apps/exchange/src/auction/collect.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-277 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A CONDITIONAL ASSERTION THAT NOW NEVER EXERCISES ITS OWN CASE. test_t224_a_roster_row_that_prices_nothing_can…
- scope: `apps/exchange/tests/test_repro_untrusted_roster.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-278 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-250'S TYPESCRIPT HALF SHIPS COMPLETELY UNTESTED. The shared parity corpus that exists specifically to catch…
- scope: `packages/contracts/tests/price_parity_corpus.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-279 — depth 0 · unblocks 0
- title: A TOTAL try/except Exception ERODES THE GATE IT WAS MEANT TO SATISFY. receive_bid (door.py:358) is now a thin…
- scope: `packages/store-agent/src/external/door.py`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-12} uv run python -m pytest packages/store-agent/tests/test_repro_open_tickets.py -q --runxfail -k t279`

### T-280 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A REFUSAL RECEIPT WITH NO IDENTITY. door.py:498 returns _refuse(REASON_MALFORMED_SUBMISSION) with no `payload…
- scope: `packages/store-agent/src/external/door.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-281 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A TEST PINS THE EXACT BEHAVIOUR AN OPEN TICKET SAYS IS WRONG. test_repro_external_door.py:283 hard-asserts `r…
- scope: `packages/store-agent/tests/test_repro_external_door.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-282 — depth 0 · unblocks 0  **[NO-GATE]**
- title: MalformedLedgerPayload IS NOT RE-EXPORTED, so `from exchange.auction import MalformedLedgerPayload` raises Im…
- scope: `apps/exchange/src/auction/__init__.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-283 — depth 0 · unblocks 0  **[NO-GATE]**
- title: AN EXCEPTION ON A PATH WHOSE STATED CONTRACT IS 'NEVER FAIL THE AUCTION'. build_published_event (apps/exchang…
- scope: `apps/exchange/src/auction/ledger.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-284 — depth 0 · unblocks 0  **[NO-GATE]**
- title: FOUR IN-REPO SITES STILL DESCRIBE T-235 AS A LIVE DEFECT. T-235 was closed by the T-158 lane and merged to ma…
- scope: `apps/buyer/svc/src/feedback/submission.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-285 — depth 0 · unblocks 0  **[NO-GATE]**
- title: AN UNWIRED TEST HELPER: THE SyntaxWarning IT WAS WRITTEN TO MUTE STILL LEAKS. _parse(path) at apps/merchant/s…
- scope: `apps/merchant/svc/tests/test_repro_open_tickets.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-286 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE S1 E2E FLOW HAS TWO BLIND SPOTS, THOUGH IT IS OTHERWISE GENUINE. FILED AGAINST HELD WORK: e2e/test_s1_flo…
- scope: `e2e/test_s1_flow.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-287 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-207'S GATE ASSERTS A SUBSTRING NEGATION, NOT THE AGREEMENT ITS NAME PROMISES, AND strict=True TURNS THAT IN…
- scope: `apps/trust/tests/test_repro_open_tickets.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-288 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-166'S AST GATE IS EVADABLE BY A ONE-LINE HOIST THAT PRESERVES THE DEFECT. The walk at lines 140-144 collect…
- scope: `apps/trust/tests/test_repro_open_tickets.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-289 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-181'S TEST IS GREEN UNDER THE EXACT MUTATION IT EXISTS TO CATCH, because it derives its expectation from th…
- scope: `apps/trust/tests/test_repro_open_tickets.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-290 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-167's gate computes `bodies = {path.read_bytes() for path in copies}` at line 179 and NEVER ASSERTS ON IT -…
- scope: `apps/trust/tests/test_repro_open_tickets.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-291 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-212's gate hardcodes its fixture roster: _DATASTORE_FIXTURES at lines 446-458 is a literal frozenset of nin…
- scope: `apps/trust/tests/test_repro_open_tickets.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-292 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A COMMENT ASSERTS HERMETICITY THAT IS MEASURABLY FALSE, and a load-bearing line is documented as unnecessary.…
- scope: `packages/contracts/tests/test_claim_identity.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-293 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A LIVE MEMORY ADDRESS REACHES AN UNAUTHENTICATED HTTP CLIENT AND A PERSISTED EVENT. This is T-264's exact def…
- scope: `apps/exchange/src/checkout/providers.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-294 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE ACCEPT PATH T-170 JUST SHIPPED CANNOT ACCEPT ANYTHING, BECAUSE NOTHING IN THE REPOSITORY PERSISTS AN AUCT…
- scope: `apps/exchange/src/auction/routes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-295 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THREE MORE `!r` LEAKS OF T-264'S SHAPE, RANKED BY MEASURED EXPOSURE, ALL OUTSIDE THE FIXING LANE'S SCOPE. (1)…
- scope: `apps/exchange/src/orchestration/solicitation.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-296 — depth 0 · unblocks 0
- title: EIGHT PUBLISHED OPENAPI OPERATIONS THAT NO APP SERVES, AND FOUR OF THEM ARE OWNED BY NOBODY. Measured by BOOT…
- scope: `packages/contracts/openapi/trust.openapi.json`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-15} && uv run python -m pytest apps/exchange/tests/test_repro_open_tickets.py -q --runxfail -k test_t312_the_trust_service_serves_exactly_the_operations_its_contract_publishes`

### T-297 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-204 IS HALF-CLOSED ON A HALF-RED GATE, AND T-267 CARRIES A PREMISE THAT IS MEASURABLY FALSE. Two separate h…
- scope: `packages/contracts/openapi/exchange.openapi.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-302 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THREE FROZEN LEDGER KINDS ARE RESERVED IN ALL FOUR VOCABULARIES AND EMITTED BY NO PRODUCT CODE -- three sibli…
- scope: `apps/trust/src/reconcile/engine.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-303 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-237 IS HALF-CLOSED AND THE REMAINING HALVES ARE IN OTHER LANES' SCOPES. The new delisting module correctly …
- scope: `apps/trust/src/snapshot/delisting.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-305 — depth 0 · unblocks 0  **[NO-GATE]**
- title: FOUR COMMENTS MADE FALSE BY THE FIXES THAT LANDED THIS CYCLE, self-reported by the lane that invalidated them…
- scope: `apps/trust/src/verification/__init__.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-306 — depth 0 · unblocks 0
- title: list_prices IS THE IDENTICAL FAIL-OPEN TO T-233, ONE ARGUMENT OVER, and it is on the money path. A signed bid…
- scope: `packages/contracts/src/boundary.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-1} && uv run python -m pytest packages/contracts/tests/test_repro_open_tickets.py -q --runxfail -k test_t306`

### T-307 — depth 0 · unblocks 0
- title: max_discount_pct SUPPLIED WITHOUT list_prices IS NEVER READ, so a caller that sets a 0% ceiling and forgets t…
- scope: `packages/contracts/src/boundary.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-13} && uv run python -m pytest packages/contracts/tests/test_repro_open_tickets.py -q --runxfail -k test_t307`

### T-308 — depth 0 · unblocks 0
- title: THE SYSTEM HAS NO OBSERVABILITY AT ALL, AND EVERY INFO-LEVEL CALL IS SILENTLY DISCARDED. Repo-wide grep for b…
- scope: `proxyshop_support/`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-1} && uv run python -m pytest proxyshop_support/tests/test_repro_observability.py -q --runxfail -k test_t308`

### T-309 — depth 0 · unblocks 0
- title: THE STORE AGENT SERVES ZERO HTTP PATHS. Its app object exists and app.openapi()['paths'] is EMPTY; no routes.…
- scope: `packages/store-agent/src/main.py`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-2} && uv run python -m pytest packages/store-agent/tests/test_repro_open_tickets.py -q --runxfail -k test_t309`

### T-311 — depth 0 · unblocks 0  **[NO-GATE]**
- title: NEITHER UI IS RUNNABLE -- there is no host, no dev server, no build, and no entry point. apps/buyer/package.j…
- scope: `apps/buyer/package.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-312 — depth 0 · unblocks 0
- title: SEVEN PUBLISHED ROUTES ARE UNSERVED ACROSS THREE SERVICES, measured by constructing each app and reading app.…
- scope: `packages/contracts/openapi/trust.openapi.json`
- verify: `export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-14} && uv run python -m pytest apps/exchange/tests/test_repro_open_tickets.py services/ingest/tests/test_repro_open_tickets.py -q --runxfail -k test_t312`

### T-315 — depth 0 · unblocks 0  **[NO-GATE]**
- title: DESIGN.md:145 states docs/demo/ should hold TWO runbooks and that 'the S6 lint reads their union', but the di…
- scope: `docs/demo/`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-316 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-310's fix is BLOCKED ON T-299 and wiring it today turns a booting service into a crash-looping one. ranking…
- scope: `apps/exchange/src/auction/routes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-317 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE MERCHANT SERVES THREE ROUTES THAT APPEAR IN NO PUBLISHED CONTRACT: GET /install, GET /install/callback, G…
- scope: `apps/merchant/svc/src/onboarding/routes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-318 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-312 UNDERCOUNTS ITS OWN FINDING BY A FACTOR OF MORE THAN THREE. T-312 records the contract/served divergenc…
- scope: `packages/contracts/openapi/trust.openapi.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-319 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-288'S STRENGTHENED AST GATE IS STILL EVADABLE BY TWO SHAPES THAT PRESERVE THE DEAD 503 ARM. _names_holding_…
- scope: `apps/trust/tests/test_repro_open_tickets.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-320 — depth 0 · unblocks 0  **[NO-GATE]**
- title: _lapsed_reasons at delisting.py:112 iterates the registry, so a LOOKUP-ONLY registry implementation emits zer…
- scope: `apps/trust/src/snapshot/delisting.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-321 — depth 0 · unblocks 0  **[NO-GATE]**
- title: SIX CONTRACT/SERVED SWEEP HELPERS NOW EXIST IN THREE COPIES, one per package, with nothing enforcing that the…
- scope: `packages/store-agent/tests/test_repro_open_tickets.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-322 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A LATENT GATE FLAKE THAT INTERMITTENTLY REDS SEVEN TICKETS' GATES, AND IT IS NOT THE DATABASE. packages/contr…
- scope: `packages/contracts/tests/signing.test.ts`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-323 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-310 IS NOT 'ADD A ROUTE' — THE COPY THAT FIXES T-299 MAKES THE RANKER IMPORTABLE, NOT FUNCTIONAL, AND NOTHI…
- scope: `apps/exchange/src/auction/routes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-324 — depth 0 · unblocks 0  **[NO-GATE]**
- title: DESIGN.md:45'S REPO LAYOUT NAMES TWO PATHS THAT DO NOT EXIST — a top-level `graph/` and a `packages/ranking` …
- scope: `DESIGN.md`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-325 — depth 0 · unblocks 0
- title: THREE TICKETS NOW SHARE TWO GATE NODES, AND TWO OF THEM WILL GO GREEN ON A FIX THAT DOES NOT ADDRESS THEM. Am…
- scope: `apps/exchange/tests/test_repro_open_tickets.py`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-3} uv run python -m pytest apps/exchange/tests/test_accept_denials.py -q --runxfail -k "t325 or ticket_gate_ownership_scan"`

### T-326 — depth 0 · unblocks 0
- title: THE SAME ADDRESS LEAK T-264 DESCRIBES, ONE FILE OVER AND STILL UNFIXED, ON A CLIENT-VISIBLE PATH. providers.p…
- scope: `apps/exchange/src/checkout/providers.py`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-15} uv run python -m pytest apps/exchange/tests/test_accept_denials.py -q --runxfail -k t326`

### T-327 — depth 0 · unblocks 0
- title: T-264'S PROPOSED GATE IS UNSOUND AND WOULD CERTIFY THE LEAK. Run verbatim against HEAD it reports '3 passed i…
- scope: `apps/exchange/tests/test_accept_denials.py`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-5} uv run python -m pytest apps/exchange/tests/test_accept_denials.py -q --runxfail -k "injected_collaborator"`

### T-328 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-198 IS THE FIRST CONFIRMED UNDER-SELECTION WITH A LIVE FAILURE, and it is an OPEN ticket. Its verify select…
- scope: `apps/buyer/svc/tests/test_repro_open_tickets.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-329 — depth 0 · unblocks 0  **[NO-GATE]**
- title: ONLY 16 OF 141 TICKET GATES REACH THE FROZEN ACCEPTANCE SUITE, AND 44 OF 141 GATED TICKETS HAVE NO TRACEABLE …
- scope: `pyproject.toml`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-330 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE RUNBOOK GATE'S POST-MERGE REWRITE INTRODUCED THREE FAIL-OPENS, ALL INVISIBLE TO ITS EXIT CODE. The harden…
- scope: `docs/tests/test_runbook_executability.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-331 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-233'S RANDOMIZED GATE CANNOT SEE THE FAIL-OPEN CLASS IT WAS BUILT FOR, AND THE FIX ALREADY EXISTS ONE FILE …
- scope: `packages/store-agent/tests/test_repro_external_door.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-332 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A DELISTING EVENT NAMING NO STORE PASSES EVERY LAYER, AND IS THEN INVISIBLE TO THE AUDITOR THE MODULE EXISTS …
- scope: `apps/trust/src/snapshot/delisting.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-333 — depth 0 · unblocks 0  **[NO-GATE]**
- title: delisting.py IS THE ONLY LEDGER-EVENT PRODUCER THAT NEVER CALLS validate_ledger_payload. Every other producin…
- scope: `apps/trust/src/snapshot/delisting.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-334 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-301'S BLIND SPOT IS CIRCULAR: REMOVING A COPY REMOVES THE EVIDENCE THAT IT IS MISSING. _unwired_for reports…
- scope: `proxyshop_support/tests/test_artifact_copyset.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-335 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-237'S STATED END-STATE IS NOT REACHED, AND ONE OF ITS GATES WAS ALREADY GREEN BEFORE THE WORK. snapshot['de…
- scope: `apps/exchange/src/auction/routes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-336 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE TYPESCRIPT DOOR CARRIES THE IDENTICAL list_prices ASYMMETRY, SO T-306/T-307 ARE A TWO-LANGUAGE FIX. bound…
- scope: `packages/contracts/src/ts/boundary.ts`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-337 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE OBVIOUS TWO-SITE FIX FOR T-306/T-307 IS INSUFFICIENT, AND THE SCOPE IS 99 TESTS ACROSS TWO LANGUAGES. Mea…
- scope: `packages/contracts/src/boundary.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-338 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A TEXTUALLY-CLEAN AUTO-MERGE PRODUCED A FILE THAT DOES NOT IMPORT, AND EVERY MERGE GATE PASSED IT. Two lanes …
- scope: `docs/tests/test_runbook_executability.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-339 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE REPRODUCTION-GATE IDIOM HAS A STRUCTURAL BLIND SPOT THAT ONLY DISAPPEARS WHEN THE DEFECT IS FIXED, AND IT…
- scope: `docs/tests/test_runbook_executability.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-340 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A BIDDER SATISFIES ANY HARD CONSTRAINT BY WRITING status=verified ON ITS OWN CLAIM, AND T-310'S WIRING JUST M…
- scope: `apps/exchange/src/ranking/filters.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-341 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE FIVE-TERM RANK FORMULA IS INERT ON EVERY SERVED REQUEST, so the shortlist the demo shows is trust-only. N…
- scope: `apps/exchange/src/ranking/serving.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-342 — depth 0 · unblocks 0  **[NO-GATE]**
- title: POST /auctions' 201 BODY HAS NEVER MATCHED ITS PUBLISHED CONTRACT, and no test in the repo checks a 201 body …
- scope: `packages/contracts/openapi/exchange.openapi.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-343 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE RUNBOOK PROMISES A LIST-PRICE FALLBACK IN THE SHORTLIST AND THE C10 FILTER MAKES IT IMPOSSIBLE. starting-…
- scope: `docs/demo/starting-slice.md`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-344 — depth 0 · unblocks 0  **[NO-GATE]**
- title: Offer.expires_at IS TYPED AS AN ISO DATE-TIME STRING AND EVERY CONSUMER IN THE REPO TREATS IT AS A FLOAT EPOC…
- scope: `packages/contracts/openapi/protocol.schema.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-345 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE MONEY PATH ACCEPTS MALFORMED INPUT INSTEAD OF REFUSING IT, WHICH IS WORSE IN KIND THAN THE LEAK CLASS IT …
- scope: `packages/contracts/src/boundary.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-346 — depth 0 · unblocks 0  **[NO-GATE]**
- title: FOUR MORE REPR-LEAK SITES THAT NO TICKET NAMES, ALL ON CLIENT-VISIBLE PATHS. checkout/codes.py:187 renders {e…
- scope: `apps/exchange/src/checkout/codes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-347 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE DEPLOYED ATOMIC PRIMITIVE'S ONLY REGRESSION BARRIER IS CONDITIONAL ON DOCKER BEING UP. RedisAuctionStore.…
- scope: `apps/exchange/tests/ (RedisAuctionStore.reserve has one detector, and it is @pytest.mark.docker)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-348 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A RULING THE USER GAVE IS UNIMPLEMENTED, AND AN INTERIM PLACEHOLDER SHIPS IN ITS PLACE. Hank ruled, verbatim:…
- scope: `apps/exchange/src/accept/ (fallback acceptance) — ESC-025, ruled by Hank 2026-09-05`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-131 — depth 3 · unblocks 0
- title: The golden answer key is graded weakly, and the dishonest store's flagship lie is not graded at all
- scope: `fixtures/**`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-14} ./scripts/verify.sh check`

### T-132 — depth 3 · unblocks 0
- title: Rotating pseudonyms are trivially re-linkable, so T-070's central guarantee does not hold
- scope: `apps/buyer/**`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-10} ./scripts/verify.sh check`

### T-133 — depth 3 · unblocks 0
- title: Buyer residue: shared session state, unwired publish half, and unbounded stores
- scope: `apps/buyer/**`, `packages/**`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-11} ./scripts/verify.sh check`

### T-134 — depth 3 · unblocks 0
- title: Merchant residue: a scope guard that shreds strings, a token in repr, and six inbox findings that never reach…
- scope: `apps/merchant/**`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-11} ./scripts/verify.sh check`

### T-024 — depth 4 · unblocks 0
- title: Differential refresh keeps the graph current at field-appropriate cadence
- scope: `services/ingest/src/scheduler/**`, `services/ingest/tests/**`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-11} uv run python -m pytest services/ingest/tests/test_refresh.py -q`

### T-086 — depth 5 · unblocks 0
- title: Onboarding drives a shadow store to its first real bid
- scope: `e2e/test_onboarding.py`, `e2e/support/onboarding/**`
- verify: `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-2} uv run python -m pytest e2e/test_onboarding.py -q`

### T-054 — depth 7 · unblocks 0
- title: The dashboard shows the walls and the window
- scope: `apps/merchant/app/dashboard/**`
- verify: `npx vitest run apps/merchant/app/dashboard`

## BLOCKED — 2

`waiting on` lists only the UNCLOSED `depends_on` ids, as computed by `frontier`.

- **T-084** (depth 9) waiting on T-083 — The dishonest store ends below threshold and off the shortlist
- **T-087** (depth 10) waiting on T-084 — The Shopify and onboarding extension runbook covers the beats off the starting path

## SCHEDULING CONSTRAINTS — file-ownership overlap among OPEN tickets

Every `scope` entry claimed by MORE THAN ONE open ticket. Two tickets sharing a row here must
NOT be dispatched into the same wave as writers. Sorted by number of claimants, then path.

- distinct colliding scope paths: **23**
- open tickets touching at least one colliding path: **68**

| # claimants | scope path | open tickets claiming it |
| --- | --- | --- |
| 7 | `apps/trust/tests/test_repro_open_tickets.py` | T-275, T-287, T-288, T-289, T-290, T-291, T-319 |
| 6 | `apps/exchange/src/auction/routes.py` | T-148, T-270, T-294, T-316, T-323, T-335 |
| 4 | `apps/exchange/src/auction/collect.py` | T-271, T-272, T-273, T-276 |
| 4 | `apps/trust/src/snapshot/delisting.py` | T-303, T-320, T-332, T-333 |
| 4 | `packages/contracts/src/boundary.py` | T-306, T-307, T-337, T-345 |
| 3 | `apps/buyer/svc/src/profile/__init__.py` | T-142, T-164, T-221 |
| 3 | `apps/exchange/src/checkout/codes.py` | T-268, T-269, T-346 |
| 3 | `apps/merchant/svc/src/envelope/store.py` | T-239, T-243, T-248 |
| 3 | `docs/tests/test_runbook_executability.py` | T-330, T-338, T-339 |
| 3 | `packages/contracts/openapi/exchange.openapi.json` | T-267, T-297, T-342 |
| 3 | `packages/contracts/openapi/trust.openapi.json` | T-296, T-312, T-318 |
| 3 | `packages/store-agent/src/external/door.py` | T-244, T-279, T-280 |
| 2 | `apps/buyer/**` | T-132, T-133 |
| 2 | `apps/buyer/svc/src/auth/magic_link.py` | T-140, T-141 |
| 2 | `apps/exchange/src/auction/ledger.py` | T-150, T-283 |
| 2 | `apps/exchange/src/checkout/providers.py` | T-293, T-326 |
| 2 | `apps/trust/src/verification/__init__.py` | T-256, T-305 |
| 2 | `e2e/**` | T-083, T-084 |
| 2 | `packages/contracts/tests/test_claim_identity.py` | T-251, T-292 |
| 2 | `packages/store-agent/tests/test_repro_external_door.py` | T-281, T-331 |
| 2 | `pyproject.toml` | T-226, T-329 |
| 2 | `services/ingest/src/adapters/mapping.py` | T-245, T-249 |
| 2 | `services/shopify-stub/src/codes.py` | T-253, T-255 |

### Effective (glob-aware) ownership overlap

A literal file scope is ALSO owned by any open ticket whose scope is a directory/glob
covering it (`packages/contracts/**` covers `packages/contracts/src/boundary.py`). This
table resolves that containment and is the one to schedule against: every normalized scope
entry among open tickets that resolves to MORE THAN ONE open owner.

- normalized distinct scope entries among open tickets: **111**
- of those, entries with >1 effective owner: **62**
- no open ticket claims the repo-wide `**` scope.

| # owners | scope path (normalized) | effective open owners |
| --- | --- | --- |
| 34 | `packages/**` | T-133, T-156, T-161, T-162, T-175, T-194, T-204, T-209, T-218, T-220, T-240, T-244, T-251, T-267, T-278, T-279, T-280, T-281, T-292, T-296, T-297, T-306, T-307, T-309, T-312, T-318, T-321, T-322, T-331, T-336, T-337, T-342, T-344, T-345 |
| 15 | `apps/buyer/**` | T-132, T-133, T-140, T-141, T-142, T-163, T-164, T-165, T-197, T-198, T-199, T-221, T-284, T-311, T-328 |
| 11 | `apps/merchant/**` | T-051, T-054, T-134, T-203, T-239, T-243, T-246, T-247, T-248, T-285, T-317 |
| 8 | `apps/buyer/svc/src/profile/__init__.py:1038 (_BUCKET_VOCABULARY['category_affinity'])` | T-132, T-133, T-142, T-164, T-197, T-198, T-199, T-221 |
| 7 | `apps/trust/tests/test_repro_open_tickets.py` | T-275, T-287, T-288, T-289, T-290, T-291, T-319 |
| 6 | `apps/buyer/svc/src/profile/__init__.py` | T-132, T-133, T-142, T-164, T-199, T-221 |
| 6 | `apps/exchange/src/auction/routes.py` | T-148, T-270, T-294, T-316, T-323, T-335 |
| 5 | `packages/contracts/src/boundary.py` | T-133, T-306, T-307, T-337, T-345 |
| 4 | `apps/buyer/svc/src/auth/magic_link.py` | T-132, T-133, T-140, T-141 |
| 4 | `apps/buyer/svc/src/profile/__init__.py:436 (_MIN_LEAKABLE = 4)` | T-132, T-133, T-197, T-199 |
| 4 | `apps/buyer/svc/src/profile/__init__.py:918 (_identity_sources) and` | T-132, T-133, T-198, T-199 |
| 4 | `apps/exchange/src/auction/collect.py` | T-271, T-272, T-273, T-276 |
| 4 | `apps/merchant/svc/src/envelope/store.py` | T-134, T-239, T-243, T-248 |
| 4 | `apps/trust/src/snapshot/delisting.py` | T-303, T-320, T-332, T-333 |
| 4 | `e2e/**` | T-083, T-084, T-086, T-286 |
| 4 | `packages/contracts/openapi/exchange.openapi.json` | T-133, T-267, T-297, T-342 |
| 4 | `packages/contracts/openapi/trust.openapi.json` | T-133, T-296, T-312, T-318 |
| 4 | `packages/store-agent/src/external/door.py` | T-133, T-244, T-279, T-280 |
| 3 | `apps/buyer/package.json` | T-132, T-133, T-311 |
| 3 | `apps/buyer/svc/src/auth/routes.py (POST /buyer/auth/magic-link) + routes.py:57 ProcessLocalStateUnsafe` | T-132, T-133, T-165 |
| 3 | `apps/buyer/svc/src/auth/sessions.py` | T-132, T-133, T-163 |
| 3 | `apps/buyer/svc/src/feedback/submission.py` | T-132, T-133, T-284 |
| 3 | `apps/buyer/svc/tests/test_repro_open_tickets.py` | T-132, T-133, T-328 |
| 3 | `apps/exchange/src/checkout/codes.py` | T-268, T-269, T-346 |
| 3 | `apps/merchant/svc/tests/**` | T-051, T-134, T-285 |
| 3 | `apps/merchant/svc/tests/test_repro_open_tickets.py` | T-051, T-134, T-285 |
| 3 | `docs/tests/test_runbook_executability.py` | T-330, T-338, T-339 |
| 3 | `e2e/support/onboarding/**` | T-083, T-084, T-086 |
| 3 | `e2e/test_onboarding.py` | T-083, T-084, T-086 |
| 3 | `e2e/test_s1_flow.py` | T-083, T-084, T-286 |
| 3 | `packages/contracts/tests/test_claim_identity.py` | T-133, T-251, T-292 |
| 3 | `packages/store-agent/tests/test_repro_external_door.py` | T-133, T-281, T-331 |
| 2 | `apps/exchange/src/auction/ledger.py` | T-150, T-283 |
| 2 | `apps/exchange/src/checkout/providers.py` | T-293, T-326 |
| 2 | `apps/merchant/app/dashboard/**` | T-054, T-134 |
| 2 | `apps/merchant/svc/src/codes/ (T-052) — binding requirement, not a defect` | T-134, T-203 |
| 2 | `apps/merchant/svc/src/collector/**` | T-051, T-134 |
| 2 | `apps/merchant/svc/src/envelope/model.py` | T-134, T-246 |
| 2 | `apps/merchant/svc/src/install/webhooks.py` | T-134, T-247 |
| 2 | `apps/merchant/svc/src/onboarding/routes.py` | T-134, T-317 |
| 2 | `apps/trust/src/verification/__init__.py` | T-256, T-305 |
| 2 | `fixtures/**` | T-131, T-208 |
| 2 | `fixtures/manifest.json observation_weights — no weight for an uncorroborated complaint` | T-131, T-208 |
| 2 | `packages/contracts/openapi/exchange.openapi.json:169 (denial_reason) + apps/exchange/src/accept/offer.py` | T-133, T-204 |
| 2 | `packages/contracts/openapi/merchant.openapi.json` | T-133, T-240 |
| 2 | `packages/contracts/openapi/protocol.schema.json` | T-133, T-344 |
| 2 | `packages/contracts/src/boundary.py (validate_bid) — external path` | T-133, T-162 |
| 2 | `packages/contracts/src/boundary.py:157 (_source_verdict)` | T-133, T-161 |
| 2 | `packages/contracts/src/ts/boundary.ts` | T-133, T-336 |
| 2 | `packages/contracts/src/ts/schemas.ts:63-65 (ajv + ajv-formats) vs packages/contracts/src/boundary.py:353-365 (pydantic)` | T-133, T-194 |
| 2 | `packages/contracts/tests/price_parity_corpus.json` | T-133, T-278 |
| 2 | `packages/contracts/tests/signing.test.ts` | T-133, T-322 |
| 2 | `packages/store-agent/src/hooks/provenance.py (_price_reconciliation_refusal)` | T-133, T-156 |
| 2 | `packages/store-agent/src/hooks/provenance.py:129,:496-507 (commitments walk, no pydantic gate)` | T-133, T-209 |
| 2 | `packages/store-agent/src/hooks/provenance.py:523 (`if node is None: return`) — T-209 is one of a family of at least six` | T-133, T-218 |
| 2 | `packages/store-agent/src/hooks/provenance.py:830-834 vs DESIGN.md` | T-133, T-175 |
| 2 | `packages/store-agent/src/main.py` | T-133, T-309 |
| 2 | `packages/store-agent/tests/test_price_reconciliation.py (depth parametrization) — cycle detection has an invisible ceiling` | T-133, T-220 |
| 2 | `packages/store-agent/tests/test_repro_open_tickets.py` | T-133, T-321 |
| 2 | `pyproject.toml` | T-226, T-329 |
| 2 | `services/ingest/src/adapters/mapping.py` | T-245, T-249 |
| 2 | `services/shopify-stub/src/codes.py` | T-253, T-255 |

## REJECTED / NEEDS RE-PLAN

`frontier` reports **20** ticket(s) carrying a REJECTED ledger verdict that keeps them OPEN:
**T-140, T-142, T-156, T-160, T-172, T-204, T-210, T-212, T-242, T-256, T-257, T-259, T-262, T-279, T-303, T-306, T-307, T-308, T-309, T-312**.
All 20 therefore still appear above, in READY.

3 ticket(s) named by a rejected record have since been CLOSED by a later verdict or a
merged branch and are absent from this ledger: T-082, T-158, T-310.

1 rejected record(s) name no `tickets` field at all. A record with no `tickets` closes
and reopens NOTHING — it is an advisory verdict only, carried below for its content.

Reasons are VERBATIM from `.swarm-loop/dispatch.jsonl`; they are retry packets, and their
value is the exact wording. One record can name several tickets, so it appears once, under
all of them; a ticket rejected twice appears under each of its records.

### (no `tickets` field — advisory verdict, closes nothing)
- dispatch record: action=`close` agent_ref=`rung2-c13` branch=`main` closed_at=`2026-09-03T13:56:42` sha=`15958ab935e07e3c38fc3a2c2e59af6766008e77`
- ticket ids appearing in the prose below, with their CURRENT closure state (derived, not from the record): T-177 (closed), T-206 (closed), T-214 (closed), T-216 (closed), T-223 (closed), T-224 (closed)
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): PARTIAL — the verifier itself was killed by the network outage, but one of its lenses returned before dying and its findings are minted: T-223 (CRITICAL, the price floor is an exact equality so unit_price 0.001 clears a 100.00 product under max_discount_pct 100, and on an uncapped roster row _is_judged skips the check entirely so 1e-09 returns HTTP 201) and T-224 (HIGH, unauthenticated HTTP 500 out of the middle of an auction via list_price 0.0 plus an unparseable price, and a 0.00 rankable fallback on that same row). Verdict on T-177: DEFECTIVE at the production door — the attack its own ticket described is genuinely closed, these three bypasses stand beside it. Two of its hypotheses did NOT survive measurement and are recorded so nobody re-chases them: the suspected __init__.py production break does not exist (the import was reflowed, not deleted, and production imports the submodule directly), and the tolerance concern is backwards (0.01 is ABSOLUTE, so maximum underpayment on a 10,000 item is one cent — absolute is correct here). COVERAGE GAP, must be re-run: the cross-branch interaction lens (T-214's lock x T-216's shared-database handling x T-206's ledger replay, run together in one process) died before reporting. That class has already produced a 600-second self-deadlock in this repo once. Re-dispatch it against the post-wave main rather than against 7279452, since eight lanes are mid-flight.

### T-082 (all since CLOSED)
- dispatch record: action=`close` agent_ref=`lane-T-082-wip` branch=`task/T-082` closed_at=`2026-09-04T02:20:50` sha=`74540b69bcf67a31d446d1f7135eb5f74ea1d6d7`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): REJECTED back to the backlog as unfinished WIP; branch task/T-082 and its worktree are PRESERVED as the retry packet's input. Three independent reasons, none of them the gate: (1) it is salvaged WIP from a lane that died on a session rate limit and never ran its own gates against these bytes; (2) e2e/test_s1_flow.py carries THREE ruff I001 errors (:255, :303, :608) which take build_succeeds from 1 to 0 on merge; (3) two sabotage-proven blind spots - replacing merchant_svc.install.webhooks.verify with 'lambda: True', or mint_code with the constant 'NOT-A-PSX-CODE', leaves all 23 of its tests passing, confirmed with a controlled matched pair so these are genuine misses rather than sabotage that failed to land. What IS good and should be reused: the flow exercises the real product path and sabotage caught 5 of 7 product functions; e2e/test_s1_flow.py:64-80 asserts the run drove no code from outside the worktree and fires on real data (13 modules). SEPARATELY and not a reason for this rejection: its gate names its own deliverable so red-check at the merge base can never stamp red - that is the T-263 class and ESC-015, unaffected by this decision and still the user's to rule on. The retry needs a gate that can go red: either a frozen acceptance test that grades the S1 flow (none exists today) or the harness fix in ESC-015 option 1.

### T-082 (all since CLOSED)
- dispatch record: action=`close` agent_ref=`lane-T-082` branch=`task/T-082` closed_at=`2026-09-04T12:33:02` sha=`74540b69bcf67a31d446d1f7135eb5f74ea1d6d7`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): check-branch VETO: T-082 has no red-check verdict of 'red' — stamped weak twice (pre-dispatch and close) at base 9ef97f8. Its verify command does not select a test that goes red. Ticket returns to backlog needing a real gate.

### T-306, T-307, T-308, T-309, T-310, T-312 (OPEN: T-306, T-307, T-308, T-309, T-312 · since CLOSED: T-310)
- dispatch record: action=`close` agent_ref=`correction-gatelane-closures` closed_at=`2026-09-04T15:32:16`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): BOOKKEEPING CORRECTION BY THE ORCHESTRATOR — NOT a verdict on any lane's work, which was excellent and is merged. I closed three GATE lanes (lane-C19-boundary, lane-C19-observ, lane-C19-served) with '--ticket <the tickets they gated> --verdict accepted'. Those lanes delivered GATES ONLY, by their own briefs, and my notes said in prose 'the TICKETS remain OPEN as fix work' — but the prose is not what the ledger reads. '--ticket ... --verdict accepted' closed the tickets, so six tickets with LIVE defects were recorded as done. Measured at HEAD, every one of their gates is RED right now: T-306 '1 failed', T-309 '1 failed', T-312 '3 failed', and T-306/T-307/T-308/T-309 had already dropped out of the READY frontier. Under --runxfail a FAILED gate means the defect is still live, so these are false CLOSURES — the direction that silently drops real work, which is exactly the asymmetry the frontier rules exist to protect. A rejected verdict is the only mechanism that reopens a ticket, so it is used here to undo my own error; read it as 'the fix work was never done', never as 'the gate lane failed'. Found by an independent sweep that ran every closed ticket's gate verbatim, not by me. LESSON, recorded because it is the run's own recurring shape turned on my bookkeeping: I described the ticket state accurately in a human-readable note and recorded the opposite in the machine-readable field, and only the machine-readable field is ever read again.

### T-212 (OPEN)
- dispatch record: action=`close` agent_ref=`correction-T-212-regression` closed_at=`2026-09-04T18:41:05`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): REOPENED, and the CAUSE IS A THIRD POSSIBILITY NEITHER I NOR THE PEER LISTED. Two records conflicted: recon-c13-T-212 closed it as 'VERIFIED FIXED: the claim was refuted by T-217 and retracted in tickets.json', and then the cycle-18 wave wrote it a gate that fails at HEAD. The peer framed it as 'either the refutation was wrong or a lane re-litigated a settled question' and correctly left the ruling to me. It is neither: A LATER MERGE RE-INTRODUCED THE DEFECT. Measured — the gate fails naming exactly two tests, test_the_writer_connects_to_the_real_database_as_trust_rw_from_the_per_role_var and test_the_writer_appends_to_the_real_ledger_as_trust_rw, and puts their introduction at 83a6dee, 'test(trust): the writer must be able to APPEND as trust_rw, not merely connect' — the C18-trust lane's own work for T-154, merged THIS cycle, long after T-212 was closed. So the original closure was sound at the time and the T-154 work re-opened it by adding two database-backed tests without @pytest.mark.docker. Consequence per the gate's own message: T-109's per-service inference cannot route them to postgres, so they cannot skip cleanly when the stack is down and they run inside unprotected. THIS ALSO ANSWERS THE QUESTION MY CYCLE-18 REPORT LEFT OPEN — 'were these three red when closed, or did they go red later' was recorded as NOT MEASURED. For T-212 the answer is measured and it is: went red later. For T-158 and T-204 it is red-when-closed, since one ancestry verdict closed them without ever running their gates.

### T-158, T-204 (OPEN: T-204 · since CLOSED: T-158)
- dispatch record: action=`close` agent_ref=`correction-ancestry-closures` closed_at=`2026-09-04T18:41:05`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): SECOND BOOKKEEPING CORRECTION BY THE ORCHESTRATOR, and this one is the SIBLING CLASS of the first — found by a peer session sweeping what I only fixed the instance of. My own record 'lane-T-158' at 12:33:02 closed FIVE tickets (T-158, T-169, T-170, T-204, T-235) with ONE verdict on BRANCH-ANCESTRY evidence: 'tip 980893c is an ancestor of main; work landed.' Ancestry proves a branch landed. It cannot say which of five tickets that branch resolved, and here it was 60% right. VERIFIED AT HEAD, each gate run individually: T-169 '1 passed', T-170 '1 passed', T-235 '1 passed' — correctly closed. T-158 '1 failed', T-204 '1 failed' — and under --runxfail on a strict-xfail gate, FAILED means the defect is LIVE. Both are reopened here. T-158 IS THE URGENT ONE AND IT COSTS MONEY: its own gate reason states that accept() stamps accepted_bid_ref on the auction OBJECT it is handed and persists nothing, so two requests that each load the same record and never save it back both pass the one-accept guard and mint TWO live single-use discount codes for one purchase. That has been recorded as done since 12:33. T-204's is the open-ended denial_reason vocabulary on a persisted, client-visible field. THE LESSON, which is my own logged recurring mistake: after fixing the six falsely-closed tickets at 7d18e90 I did not sweep for siblings of that fix, and the sibling class was three more tickets including a double-spend. A peer swept it in ~30 lines. The sweep is now standing practice for this run: for every ledger-accepted ticket, resolve its gate to concrete test nodes and ask whether any node still carries an xfail marker — a remaining marker is this repo's own statement that the defect is live.

### T-158, T-204, T-212 (OPEN: T-204, T-212 · since CLOSED: T-158)
- dispatch record: action=`close` agent_ref=`corroborate-T158-T204-T212` closed_at=`2026-09-04T20:30:22`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): INDEPENDENT CORROBORATION of the 929a86a sibling-class reopen, by an agent that did not write it. Verdict rejected = all three tickets STAY OPEN, but for materially different reasons than 929a86a recorded, and one of my supporting facts was simply wrong. T-158 -- CORROBORATED, with a working exploit through the REAL served route POST /auctions/{id}/accept, not the gate model. create_app() wired as test_accept_routes.py:wired_app, no barrier, no patching, only a realistic 150ms merchant POST /codes latency: two live single-use discount codes minted for one purchase (merchant POST /codes called twice). Reproduces identically when both requests carry the SAME bid_ref, so it is the double-click/retry case, not two different bids. With load/save given a 2ms round trip -- which is what RedisAuctionStore (auction/state.py:162-171) actually is, two separate network calls -- 3 runs of 3 returned HTTP 200 twice and left the buyer holding two permalinks. Guard read at accept/offer.py:459, written back at :534, nothing durable or serialised between them; accept/routes.py:46-56 already says so in a comment. T-158's PRESCRIBED FIX IS WRONG AND MUST NOT BE IMPLEMENTED AS WRITTEN. The ticket says to move the guard onto AuctionStateMachine's "already-serialised ACCEPTED transition". That transition is NOT serialised: _transition (auction/state.py:240-266) is a bare get -> mutate -> store.save() with no lock, no WATCH/MULTI, no compare-and-set. Measured: 2 of 2 concurrent ACCEPTED transitions both succeeded. Anyone following the ticket text ships a non-fix. Fix lane task/T-158 dispatched with this correction and with the requirement that the fix survive a PROCESS boundary. T-158's GATE SEES ONLY HALF THE DEFECT. It exercises accept() against a hypothetical route that never saves back; the real route does. A process-wide in-memory ledger -- the precedent the ticket itself cites, buyer/svc/src/accept/handoff.py:187 -- turns the gate GREEN while the cross-process double-spend stays live. A green T-158 gate must not be read as closed. T-204 -- CORROBORATED, half fixed. 330eb13 added accept/reasons.py with a real DENIAL_REASONS vocabulary and routes.py:_denied republishes unknown codes as unspecified, so the exchange half now passes. The published contract -- which is literally the ticket's scope line -- is still defective: exchange.openapi.json has denial_reason as a bare {"type": "string"}, and grep -c '"enum"' over the whole document returns 0. offer.py:535 still formats type(exc).__name__ into the prose half. Caveat for whoever fixes it: the exchange-side helper _declared_denial_vocabulary falls back to the Python constant, so running only the exchange half gives a FALSE GREEN; packages/contracts/tests/test_repro_open_tickets.py:346 is the test that actually covers the published contract. T-212 -- MY REOPEN WAS WRONG ON THE HARM, AND MY SUPPORTING FACT WAS WRONG ON THE CHRONOLOGY. The factual half is true and is what the gate grades: the two tests still carry no @pytest.mark.docker at HEAD. The harm claim is false and was MEASURED false, not read: with Postgres pointed at a dead port both tests skip cleanly, exit 0, and the skip reason names the endpoint -- delivered by the worker_database fixture (conftest.py:186 -> _require_services -> conftest.py:177), proven by that function's own nag string appearing in the skip text. The second half of the harm claim is also false: verify.sh:187 runs check as -m "not needs_model and not slow", so docker-marked tests are NOT deselected there and adding the marker would change nothing about whether they run in check (verify.sh:152-168 documents removing "not docker" deliberately). _DOCKER_SKIP_KEY (conftest.py:81,152) is written and never read anywhere, so no metric distinguishes the two skip sites either. AND THE FACT I FORWARDED AS VERIFIED WAS BACKWARDS. I told the corroborating agent to trust that 83a6dee added the two unmarked tests and "a later merge re-opened it". 83a6dee did add them -- but it reached main's first-parent chain at 0e96d1f on 2026-09-02 21:43, while T-217's refutation was minted at 3927afd on 2026-09-03 02:48, FIVE HOURS LATER. The unmarked tests were already on main when the claim was measured false. T-212's gate was not written until 132b3d1 on 2026-09-03 22:22, about 20 hours after the refutation. So the gate is a strict xfail encoding an already-refuted claim, and reading its --runxfail FAILED as "the defect is live" is precisely the inference that produced the wrong reopen. T-212 stays OPEN only because the two markers are genuinely still absent; it is cosmetic, it is not the InvalidSchemaName flake in its own title, and that mechanism is T-216, which is correctly still open. TWO LEDGER-EVIDENCE DEFECTS -- right verdicts resting on false supporting facts, which is how a later reader gets misled. T-158's status_evidence claims "apps/exchange/src/accept/ untouched since e0ffd1a; no commit or dispatch note mentions T-158 or the AuctionStateMachine ACCEPTED transition it names" -- both halves false: 7 commits touched that directory since e0ffd1a (including 8d3f3ff, which added routes.py), 9 commits mention T-158, and routes.py:46, :56 and :361 name T-158 and that transition explicitly. T-204's status_evidence claims neither offer.py nor exchange.openapi.json appears in git diff --stat 05cf86c HEAD -- true for the OpenAPI file, false for offer.py, which shows 203 changed lines in that range.

### T-140, T-142 (OPEN)
- dispatch record: action=`close` agent_ref=`gate-provenance-blind-window` branch=`main` closed_at=`2026-09-04T20:59:25` sha=`d74005773491513bf404b89bb32465f7791d12d3`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): ADVERSARIAL GATE-PROVENANCE AUDIT of the eight gates authored while this run's watchdog was blind (18:10-20:03). Verdict rejected = T-140 and T-142 are REOPENED; the other six were already open and stay open. THE REOPEN, which is the actionable half. T-140 and T-142 carried status closed with status_evidence "REFUTED by the cycle-10 triage ... is not a defect", closed in every tickets.json revision back to 2026-09-02 22:33 -- while the gates written for them on 2026-09-04 18:37 reproduce the defect at HEAD. Measured, quoting the gates' own failure text: T-140, "60 of 60 buyers were served a profile that does not reflect the history written for them through the production service's own account directory"; T-142, "the buyer service opened no database connection at all while PROXYSHOP_PG_DSN_APP='postgresql://.../proxyshop_t142_sentinel' was set". These are direct contradictions of the closure. They were closed by TRIAGE NARRATIVE, not by a passing gate, and the gate that refutes the narrative was unreachable because the ticket's verify was the placeholder. Amendment 24 repointed both; this verdict reopens them. THE STRUCTURAL FINDING, larger than the window it came from. ALL EIGHT tickets still carried the literal verify string "false # NO GATE YET" -- not a weak gate, the string false, which exits 1 unconditionally. Traced across all 40 commits that ever touched tickets.json: these eight have NEVER had a real verify command at any point in the run. Grep for the eight gate node names across tickets.json, .swarm-loop/**, every *.md, *.sh, *.json and every non-test *.py returns ZERO hits -- nothing outside the test files knew these gates existed. Amendment 22 reparameterised 130 gate verify strings and skipped all eight, because to the harness they were not gates. make verify runs pytest without --runxfail (scripts/verify.sh:151), so whole-file they read store-agent "3 passed, 3 xfailed" exit 0, buyer "10 xfailed" exit 0, exchange "13 passed, 4 xfailed" exit 0 -- green everywhere. Roughly 1900 lines of careful, controlled gate, reachable only by a human typing the -k selector by hand. The disconnect is fail-CLOSED for closure, which is why it survived unnoticed. THE COMMIT LIST I GAVE THE AUDITOR WAS INCOMPLETE AND IT SAID SO. Ten test-touching commits in range, not six. Two of them -- dafa8f0 (17:49:50, authored the exchange T-264/T-325/T-326/T-327 gates, +488) and b9754d7 (17:56:30, authored the T-156 gate) -- CREATED two of the three gate files about fifteen minutes BEFORE the window I scoped, unobserved for the same reason. It graded them anyway rather than honouring my boundary. THREE DEFECTS FOUND, all now dispatched to task/gate-harden. (1) T-140's gate was a FALSE GREEN as authored: sabotaging InMemoryAccountDirectory with a class-level backing dict, leaving auth/routes.py untouched and the defect fully intact, gave exit 0 / "1 passed" against the d261350 gate file; 838ee0e ANDed on an AST clause 96 seconds later and that clause is the ENTIRE load-bearing difference between a false green and a sound gate -- written by the same lane with no verifier in the loop. It is still loose enough that an unused import plus a shared class store satisfies it. (2) The buyer gate file ships NO arming test: all 10 nodes are xfail(strict=True), so the generator canaries live inside the xfail'd bodies and a collapsed generator reports as "expected failure" -- it cannot tell "defect present" from "fixture broken". (3) T-156's node is named ..._is_refused_by_both_doors but 88710d1 moved the contracts.boundary half to reported-only, so it grades one door under a name promising two. TWO GATES ARE SOUND ONLY AS NODE+ARMING PAIRS, and this changed how I wrote amendment 24's selectors. T-156: replacing enforce_bid_provenance with an unconditional refusal naming total_price, reconciling nothing, PASSES the gate -- the arming test catches it via its 180-bid honest control. T-325: setting every ticket's verify to false passes the gate vacuously -- the arming test catches it ("only 0 verify selector(s) name a test_t<NNN> node; 33 did when this was written"). Every selector in amendment 24 is therefore the short -k form, and one of mine was wrong on the first pass in exactly this way. POSITIVELY VERIFIED, on record: T-279's oracle is not fakeable by renaming REASON_DOOR_FAILED_CLOSED (expected is computed at run time from a live call, and the gate never names the constant) -- 52 escapes either way. The exchange gates ARE satisfiable by the real fix (scrubber plus the gate._denial_reason bypass gives 1 passed each, surfacing as XPASS(strict)). T-156 is satisfiable by a minimal offender in provenance.py. T-142 is NOT a tautology: it monkeypatches psycopg.connect and asserts the write carries each buyer's server-minted pseudonym and server-built buckets, both arriving over HTTP, neither authorable by the fixture. ONE CLAIM DELIBERATELY NOT OVERSTATED. 4d1f877's BYPASS pin is a genuine strengthening, but under the partial fix probed, the pre-pin file stayed red too (on 2 of 17 rather than 3), so the pin added a third witness rather than being the only catch. Its necessity is argued, not measured, and the auditor said so rather than claiming the win. NO REPO FILE WAS MODIFIED BY THIS AUDIT. Four scratch worktrees created and removed; every sabotage ran in a disposable worktree and was reverted via git show HEAD:<path>, each verified by MD5 against a pre-experiment backup.

### T-172, T-256, T-257, T-259, T-303 (OPEN)
- dispatch record: action=`close` agent_ref=`lane-C20-trust` branch=`task/C20-trust` closed_at=`2026-09-05T15:56:42` sha=`bf92ad967855df015771b80fd9afec553d5c353d`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): GATES LANDED, FIX WORK NEVER DONE — verdict is 'rejected' to keep the tickets OPEN, and must NOT be read as a verdict on the lane, whose gate work was good and is merged. The lane's branch task/C20-trust has zero commits ahead of main and its tip is NOT on main's first-parent history, which proves it carried work and entered through a merge's second parent. What it delivered was GRADERS (12 test functions in apps/trust/tests/test_repro_open_tickets.py), not fixes. MEASURED AT HEAD 7396f8f, each de-facto grader run individually, serially, under timeout 900, with services up: T-172 FAILED (9 of 274 docker-marked items declare no service, WIDER than the 8 the ticket records), T-256 FAILED (no trust-side callable persists a verification outcome; 5 ledger tables still have no writer), T-259 FAILED (60 of 60 events trust emits are refused by the real intake, 6 validation errors on the payload), T-303 FAILED (both named halves open: the blacklisted decision never reaches the ledger writer, and the served exchange denies 5 of 5 rostered stores because eligibility has no product caller). T-257 PASSED and is the one candidate for closure, held open only because its RECORDED verify is still the placeholder so the graph holds no pointer to the grader that proves it. Every failing gate's companion _is_armed self-check PASSED, which is what rules out vacuity: the probes work and are reporting a live defect. SEPARATE AND FILED: all five still carry the literal placeholder verify, so none is closable on its recorded gate even when green.

### T-156, T-279 (OPEN)
- dispatch record: action=`close` agent_ref=`lane-C20-store` branch=`task/C20-store` closed_at=`2026-09-05T15:56:43` sha=`9c63434c0cc3dadfdd79f4f90fde4d31918abdf3`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): GATES LANDED, FIX WORK NEVER DONE — verdict is 'rejected' to keep T-156 and T-279 OPEN, not as a verdict on the lane, whose work merged. Branch task/C20-store has zero commits ahead of main with its tip off main's first-parent history, so it carried work and entered via a merge. MEASURED AT HEAD 7396f8f, gates run serially under timeout 900 with services up: T-156 FAILED — enforce_bid_provenance admitted 50+ bids whose total price is below one unit price (unit 95.0 against total 91.07 at 5 percent depth). T-279 FAILED — _receive_bid still raises ArithmeticError/ZeroDivisionError on the hostile inputs, only the outer catch-all holds the property up, and the refusal reads door_failed_closed instead of trust_snapshot_unavailable. Both companion _is_armed self-checks PASSED, so neither red is vacuity.

### T-160, T-210, T-262, T-242 (OPEN)
- dispatch record: action=`close` agent_ref=`lane-C20-support` branch=`task/C20-support` closed_at=`2026-09-05T15:56:43` sha=`3078636828b0d93c89ba389264eca8ea3d9eff58`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): GATES LANDED, FIX WORK NEVER DONE — verdict is 'rejected' to keep T-160, T-210, T-262 and T-242 OPEN, not as a verdict on the lane, whose work merged. Branch task/C20-support has zero commits ahead of main with its tip off main's first-parent history. MEASURED AT HEAD 7396f8f, serially, timeout 900, services up: T-160 FAILED but PARTIALLY landed — T-111 and T-123's verify strings WERE repaired, and the gate is now red on a different ticket, T-133, which runs the whole suite despite having a dedicated grader; T-109's whole-suite verify survives. T-210 FAILED — a Neo4j-lock-contention run and a real-product-defect run both exit 1, so the probe still cannot tell infrastructure from product, which is the ticket's own claim. T-262 FAILED and WIDENED — T-087 still cannot create its own grader (its gate runs docs/tests/test_runbook.py while its scope grants only docs/demo/shopify-onboarding-extension.md) and the sweep found 4 more of the same class: T-158, T-228, T-130, T-134. T-242 FAILED — 43 of 43 injected ledger-sink problems were absent from the reported problems list, which read 0; the sink is not validated at all. All companion _is_armed self-checks PASSED.
