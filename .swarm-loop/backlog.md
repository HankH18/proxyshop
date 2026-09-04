# Backlog ledger — REGENERATED from tickets.json

- generated: `2026-09-04T17:29:49Z`  ·  main sha: `20ba0da`
- source of truth: `tickets.json` (repo root)
- closure source: `swarmloop.py frontier --closed-from-ledger --closed-from-merged --base main`

**GRAPH tickets.json IS GROUND TRUTH; this file is derived from it. Regenerate on every amendment.**

## Counts (exact, derived)

| metric | value |
| --- | --- |
| tickets in graph | 263 |
| closed | 96 |
| open | 167 |
| &nbsp;&nbsp;· READY (dispatchable) | 136 |
| &nbsp;&nbsp;· BLOCKED | 31 |
| max dependency depth (whole graph) | 10 |
| max dependency depth (open tickets) | 10 |
| open tickets with NO-GATE placeholder verify | 111 |
| &nbsp;&nbsp;· of which READY | 111 |
| NO-GATE placeholder across the whole graph | 158 |
| distinct scope paths claimed by >1 open ticket | 32 |
| scope paths with >1 EFFECTIVE (glob-aware) owner | 112 |

Closed tickets are deliberately absent (ledger hygiene: a closed ticket leaves the ledger).
`NO-GATE` marks the placeholder verify `false  # NO GATE YET — write one that fails on this finding first`;
such a ticket cannot be red-checked and must have a real gate written before it is dispatched.

## READY (dispatchable) — 136

Ordered as `frontier` emits them (by what each unblocks — structural, NOT a priority ruling).
A ready set is NOT a wave: run `check-wave <ids>` before dispatching, and `red-check` before trusting any verify.

### T-010 — depth 1 · unblocks 50
- title: Contracts generate typed models and enforce the dual-path bid boundary
- scope: `packages/contracts/**`
- verify: `export PROXYSHOP_WORKER=0 && uv run python -m pytest packages/contracts -q && npx vitest run packages/contracts`

### T-011 — depth 1 · unblocks 41
- title: Postgres schemas enforce role isolation and hash-chained ledger
- scope: `db/migrations/**`, `apps/trust/src/ledger/**`, `apps/trust/tests/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest apps/trust/tests/test_schema_grants.py apps/trust/tests/test_ledger_chain.py -q`

### T-013 — depth 1 · unblocks 33
- title: Shopify stub reproduces the exact surface the system uses
- scope: `services/shopify-stub/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest services/shopify-stub -q`

### T-014 — depth 1 · unblocks 27
- title: LLM client with per-role config, cache-first prompts, and test doubles
- scope: `packages/llm/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest packages/llm -q`

### T-012 — depth 1 · unblocks 23
- title: Neo4j attribute-node catalog with vector retrieval through EmbeddingProvider
- scope: `services/ingest/src/graph/**`, `services/ingest/src/embeddings/**`, `services/ingest/tests/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest services/ingest/tests/test_graph.py -q`

### T-064 — depth 6 · unblocks 4
- title: Exchange consumes one snapshot shape for trust and exploration
- scope: `apps/trust/src/snapshot/**`, `apps/trust/tests/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest apps/trust/tests/test_snapshot.py -q`

### T-082 — depth 6 · unblocks 2
- title: One scripted run proves the full S1 flow
- scope: `e2e/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest e2e/test_s1_flow.py -q`

### T-083 — depth 8 · unblocks 2
- title: Both learning loops demonstrably move under seeded outcomes
- scope: `e2e/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest e2e/test_learning.py -q`

### T-140 — depth 0 · unblocks 0  **[NO-GATE]**
- title: AccountDirectory has exactly one implementation and no production populator. build_auth_service() never passe…
- scope: `apps/buyer/svc/src/auth/magic_link.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-141 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The magic-link token is delivered to the default no-op `_drop` (line 166) in every deployment, and set_auth_s…
- scope: `apps/buyer/svc/src/auth/magic_link.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-142 — depth 0 · unblocks 0  **[NO-GATE]**
- title: publish_profile — the only writer of app.buyer_accounts, described as 'the store-visible working set' — has z…
- scope: `apps/buyer/svc/src/profile/__init__.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-148 — depth 0 · unblocks 0  **[NO-GATE]**
- title: `configure_auctions` — the only way to give the exchange a real solicitor, a real eligibility source, a Redis…
- scope: `apps/exchange/src/auction/routes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-150 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The ledger writer has zero producers. Nothing in the repository writes an event into it - not by import, not …
- scope: `apps/exchange/src/auction/ledger.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-154 — depth 0 · unblocks 0
- title: EVENTS_ROLE = "app", and its comment asserts app is 'the role the writer connects as... deliberately not trus…
- scope: `apps/trust/tests/_fixtures_events.py`
- verify: `export PROXYSHOP_WORKER=0 && uv run python -m pytest apps/trust/tests/test_repro_open_tickets.py -q --runxfail -k test_the_events_fixture_connects_as_the_role_the_ledger_writer_actually_ships_as`

### T-156 — depth 0 · unblocks 0  **[NO-GATE]**
- title: offer.total_price is never reconciled against unit_price: an offer stating unit_price=80.0 with total_price=1…
- scope: `packages/store-agent/src/hooks/provenance.py (_price_reconciliation_refusal)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-159 — depth 0 · unblocks 0  **[NO-GATE]**
- title: SYSTEMIC GATE-CREDIBILITY DEFECT: tests that swallow a missing subject with `except ImportError` are VACUOUS …
- scope: `apps/exchange/tests/test_checkout_provider.py:406 (and repo-wide)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-160 — depth 0 · unblocks 0  **[NO-GATE]**
- title: These three tickets' recorded `verify` commands pass IDENTICALLY with and without their defect, which is the …
- scope: `tickets.json (T-109, T-111, T-123 verify fields)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-161 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A provenance block nested inside a hook-provenanced claim's opaque `value` is not walked. Measured: make_bid(…
- scope: `packages/contracts/src/boundary.py:157 (_source_verdict)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-162 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The boundary checks provenance SOURCE but never discount AUTHORISATION. make_bid(claims=[make_claim("policy",…
- scope: `packages/contracts/src/boundary.py (validate_bid) — external path`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-163 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-133 item (c): SessionStore.open() validates only that the subject carries the 'psn-' PREFIX — it never chec…
- scope: `apps/buyer/svc/src/auth/sessions.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-164 — depth 0 · unblocks 0  **[NO-GATE]**
- title: build_buckets() and anonymise_cohort() are public and run NO identity-leak check; only build_profile()/build_…
- scope: `apps/buyer/svc/src/profile/__init__.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-165 — depth 0 · unblocks 0  **[NO-GATE]**
- title: No rate limiter on the unauthenticated magic-link endpoint, and the session/pending-link store behind it is p…
- scope: `apps/buyer/svc/src/auth/routes.py (POST /buyer/auth/magic-link) + routes.py:57 ProcessLocalStateUnsafe`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-166 — depth 0 · unblocks 0
- title: test_replay_with_snapshots_names_the_missing_scorer branches on `if response.status_code == 503`. Now that T-…
- scope: `apps/trust/tests/test_events.py`
- verify: `export PROXYSHOP_WORKER=0 && uv run python -m pytest apps/trust/tests/test_repro_open_tickets.py -q --runxfail -k test_the_replay_snapshot_test_does_not_branch_on_a_status_it_can_never_reach`

### T-167 — depth 0 · unblocks 0
- title: Four BYTE-IDENTICAL copies of the elected-primary module-binding shim (md5 56d63d33...), one per E6 feature p…
- scope: `apps/trust/src/{scoring,reconcile,feedback,snapshot}/_binding.py`
- verify: `export PROXYSHOP_WORKER=0 && uv run python -m pytest apps/trust/tests/test_repro_open_tickets.py -q --runxfail -k test_the_elected_primary_binding_shim_has_exactly_one_home`

### T-169 — depth 0 · unblocks 0
- title: T-033's registered-domain guard is DECORATIVE in the deployed configuration. `_platform_domains` is module st…
- scope: `apps/exchange/src/accept/offer.py:94,:328 + apps/exchange/src/accept/__init__.py`
- verify: `export PROXYSHOP_WORKER=0 && uv run python -m pytest apps/exchange/tests/test_repro_open_tickets.py -q --runxfail -k test_t169_a_production_call_site_wires_the_platform_seller_registry`

### T-170 — depth 0 · unblocks 0
- title: T-033's objective opens 'Accept endpoint resolves a CheckoutProvider by CHECKOUT_MODE...', but the accept pac…
- scope: `apps/exchange/src/accept/ (no routes.py) vs apps/exchange/src/main.py`
- verify: `export PROXYSHOP_WORKER=0 && uv run python -m pytest apps/exchange/tests/test_repro_open_tickets.py -q --runxfail -k test_t170_the_served_exchange_app_exposes_the_published_accept_path`

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

### T-193 — depth 0 · unblocks 0
- title: T-065 CRASHES IN THE DEPLOYED IMAGE. apps/trust/Dockerfile does not COPY packages/verification, so trust.veri…
- scope: `apps/trust/Dockerfile (COPY set) vs packages/verification/`
- verify: `export PROXYSHOP_WORKER=0 && uv run python -m pytest apps/trust/tests/test_repro_open_tickets.py -q --runxfail -k test_the_trust_image_copy_set_can_resolve_the_claim_verifier`

### T-194 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE TWO DOORS DO NOT RUN THE SAME SCHEMA CHECK — 21 measured ok-divergences. TypeScript validates through ajv…
- scope: `packages/contracts/src/ts/schemas.ts:63-65 (ajv + ajv-formats) vs packages/contracts/src/boundary.py:353-365 (pydantic)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-197 — depth 0 · unblocks 0  **[NO-GATE]**
- title: Identity fragments shorter than 4 characters are never tracked ANYWHERE in the leak backstop, so short names …
- scope: `apps/buyer/svc/src/profile/__init__.py:436 (_MIN_LEAKABLE = 4)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-198 — depth 0 · unblocks 0  **[NO-GATE]**
- title: Two residual identity channels the T-189 fix deliberately stopped short of. (a) A number REGROUPED rather tha…
- scope: `apps/buyer/svc/src/profile/__init__.py:918 (_identity_sources) and`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-199 — depth 0 · unblocks 0  **[NO-GATE]**
- title: The bucket vocabulary holds every CATEGORY_TAXONOMY label out of the leak haystack on the grounds that they a…
- scope: `apps/buyer/svc/src/profile/__init__.py:1038 (_BUCKET_VOCABULARY['category_affinity'])`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-203 — depth 0 · unblocks 0  **[NO-GATE]**
- title: BINDING REQUIREMENT ON A SCHEDULED TICKET, recorded the way T-041 carried T-152's. The percent-vs-fraction mi…
- scope: `apps/merchant/svc/src/codes/ (T-052) — binding requirement, not a defect`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-204 — depth 0 · unblocks 0
- title: REFUSAL REASONS ARE UNENUMERATED AND NOTHING ASSERTS ON THEM. accept() formats type(exc).__name__ into denial…
- scope: `packages/contracts/openapi/exchange.openapi.json:169 (denial_reason) + apps/exchange/src/accept/offer.py`
- verify: `export PROXYSHOP_WORKER=0 && uv run python -m pytest apps/exchange/tests/test_repro_open_tickets.py -q --runxfail -k test_t204_a_denial_reason_is_drawn_from_a_declared_vocabulary`

### T-205 — depth 0 · unblocks 0  **[NO-GATE]**
- title: SECOND LATENT except-ImportError VACUITY, found by the AST sweep T-159 asked for. The whole module skips if s…
- scope: `services/shopify-stub/tests/test_stub_contract.py:40 via conftest.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-207 — depth 0 · unblocks 0
- title: The engine's gloss on the weight table CONTRADICTS THE APPROVED MANIFEST on what mismatch_return means. The c…
- scope: `apps/trust/src/scoring/engine.py (weight-table comment) vs fixtures/manifest.json observation_weights`
- verify: `export PROXYSHOP_WORKER=0 && uv run python -m pytest apps/trust/tests/test_repro_open_tickets.py -q --runxfail -k test_the_mismatch_return_gloss_agrees_with_the_approved_manifest`

### T-208 — depth 0 · unblocks 0  **[NO-GATE]**
- title: There is no published weight for a buyer complaint that is NOT corroborated by a return. It currently lands a…
- scope: `fixtures/manifest.json observation_weights — no weight for an uncorroborated complaint`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-209 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE CONTRACTS BOUNDARY WAS TIGHTENED AND THE STORE-AGENT'S OWN AUDITOR WAS NOT. T-195's fix regenerates the m…
- scope: `packages/store-agent/src/hooks/provenance.py:129,:496-507 (commitments walk, no pydantic gate)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-210 — depth 0 · unblocks 0  **[NO-GATE]**
- title: TWO CORRECTIONS, ONE OF THEM TO MY OWN ASSERTION. (1) I claimed the chflags window could corrupt a metric rea…
- scope: `ORCHESTRATION — measuring while the swarm runs (supersedes my T-174 rationale)`
- verify: `false # NO GATE YET — write one that fails on this finding first`

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

### T-221 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE K-ANONYMITY FLOOR T-138 DELIVERED IS SWITCHED OFF BY DEFAULT, so the re-linkability T-138 exists to preve…
- scope: `apps/buyer/svc/src/profile/__init__.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

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

### T-236 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE INGESTION PIPELINE DOES NOT EXIST AS A RUNNING THING. No production code constructs any CatalogAdapter. s…
- scope: `services/ingest/src/main.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-237 — depth 0 · unblocks 0
- title: THE S2 CHAIN HAS NO PRODUCT CODE CLOSING IT: nothing turns a sub-threshold trust score into an eligibility de…
- scope: `apps/trust/src/snapshot/builder.py`
- verify: `export PROXYSHOP_WORKER=0 && uv run python -m pytest apps/trust/tests/test_repro_open_tickets.py -q --runxfail -k test_a_sub_threshold_trust_score_produces_a_blacklisted_ledger_event`

### T-238 — depth 0 · unblocks 0  **[NO-GATE]**
- title: BARE urlsplit ON ATTACKER-CONTROLLED REDIRECT TARGETS. netguard.py:398 and transport.py:353 call bare urlspli…
- scope: `services/ingest/src/adapters/netguard.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-239 — depth 0 · unblocks 0  **[NO-GATE]**
- title: ENVELOPE VERSION HISTORY IS PROCESS-LOCAL AND DIES WITH THE PROCESS. EnvelopeVersions keeps append-only histo…
- scope: `apps/merchant/svc/src/envelope/store.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-240 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE PINNED MERCHANT CONTRACT HAS NOWHERE TO PUT AN ENVELOPE APPROVAL ARTIFACT, AND ITS OWN EXAMPLE INVITES SE…
- scope: `packages/contracts/openapi/merchant.openapi.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-242 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE SIMULATION VALIDATES ONLY HALF ITS OWN LEDGER, AND THE HALF IT SKIPS CONTAINS THE EXACT DEFECT CLASS THE …
- scope: `services/sim/src/runner.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-243 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE TWO-LEDGERS-UNDER-TWO-SPELLINGS HOLE IS PRESENT IN MERCHANT, AND T-071'S FIX DID NOT COVER IT. Measured a…
- scope: `apps/merchant/svc/src/envelope/store.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-244 — depth 0 · unblocks 0  **[NO-GATE]**
- title: MEASUREMENT CREDIBILITY: receive_bid has 28 callers and ZERO of them are production. swarmloop reachable retu…
- scope: `packages/store-agent/src/external/door.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-245 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-023'S NEW SHARED MAPPING HAS NEVER REACHED A GRAPH, AND E2'S GREEN OVER IT IS STRUCTURAL ONLY. apply_upsert…
- scope: `services/ingest/src/adapters/mapping.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-246 — depth 0 · unblocks 0  **[NO-GATE]**
- title: NO PRODUCTION CONSUMER READS is_live, SO NOTHING PROVES A SHADOW OR KILLED STORE ACTUALLY STOPS BIDDING. is_l…
- scope: `apps/merchant/svc/src/envelope/model.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-247 — depth 0 · unblocks 0  **[NO-GATE]**
- title: ORDER-DEPENDENT GLOBAL LEAK IN THE MERCHANT WEBHOOK SINK. webhooks.py:418 boots _sink = default_sink, but app…
- scope: `apps/merchant/svc/src/install/webhooks.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-248 — depth 0 · unblocks 0  **[NO-GATE]**
- title: EnvelopeVersions.record() ACCEPTS A CALLER-ASSERTED activation:'active' WITH NO APPROVAL ARTIFACT. Measured: …
- scope: `apps/merchant/svc/src/envelope/store.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-249 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-023 BEHAVIOURAL DRIFT THAT NO TEST COVERS, and the 'moved VERBATIM' claim is PARTIAL. coerce_price now retu…
- scope: `services/ingest/src/adapters/mapping.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

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

### T-262 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-087's GATE NAMES A FILE ITS OWN SCOPE FORBIDS IT TO CREATE, and that file sits inside a DIFFERENT ticket's …
- scope: `tickets.json:T-087`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-263 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A ticket whose DELIVERABLE IS ITS OWN GATE TARGET can never be red-checked at its merge base, so the retro re…
- scope: `tickets.json:T-082`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-264 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A MEMORY ADDRESS IS RENDERED INTO A PERSISTED, CLIENT-VISIBLE EVENT PAYLOAD. A TypeError raised from an unusa…
- scope: `apps/exchange/src/checkout/provider.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-265 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A TEST WHITELISTS THE DEFECT IT IS SUPPOSED TO GUARD, BY NAME, AND STAYS GREEN BOTH BEFORE AND AFTER THE REPA…
- scope: `services/sim/tests/test_simulation.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-266 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE SERVED EXCHANGE IS MISSING FOUR PUBLISHED PATHS, NOT ONE. create_app().openapi() answers only ['/auctions…
- scope: `apps/exchange/src/main.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

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

### T-279 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A TOTAL try/except Exception ERODES THE GATE IT WAS MEANT TO SATISFY. receive_bid (door.py:358) is now a thin…
- scope: `packages/store-agent/src/external/door.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

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

### T-296 — depth 0 · unblocks 0  **[NO-GATE]**
- title: EIGHT PUBLISHED OPENAPI OPERATIONS THAT NO APP SERVES, AND FOUR OF THEM ARE OWNED BY NOBODY. Measured by BOOT…
- scope: `packages/contracts/openapi/trust.openapi.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-297 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-204 IS HALF-CLOSED ON A HALF-RED GATE, AND T-267 CARRIES A PREMISE THAT IS MEASURABLY FALSE. Two separate h…
- scope: `packages/contracts/openapi/exchange.openapi.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-298 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE MERCHANT CONTAINER CANNOT START. apps/merchant/Dockerfile does not COPY the exchange package, while apps/…
- scope: `apps/merchant/Dockerfile`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-299 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE EXCHANGE IMAGE STARTS AND THEN CANNOT RANK OR RETRIEVE -- silent degradation with a green board, which is…
- scope: `apps/exchange/Dockerfile`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-300 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE BUYER'S CLARIFICATION LOOP RUNS MODEL-LESS IN THE SHIPPED IMAGE, and NO RUNTIME PROBE AT ANY DEPTH CAN SE…
- scope: `apps/buyer/svc/src/intent/routes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-301 — depth 0 · unblocks 0  **[NO-GATE]**
- title: NOTHING IN THIS REPO GRADES THE DEPLOYABLE ARTIFACTS, only the checkout -- and that is a category the existin…
- scope: `tests/`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-302 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THREE FROZEN LEDGER KINDS ARE RESERVED IN ALL FOUR VOCABULARIES AND EMITTED BY NO PRODUCT CODE -- three sibli…
- scope: `apps/trust/src/reconcile/engine.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-303 — depth 0 · unblocks 0  **[NO-GATE]**
- title: T-237 IS HALF-CLOSED AND THE REMAINING HALVES ARE IN OTHER LANES' SCOPES. The new delisting module correctly …
- scope: `apps/trust/src/snapshot/delisting.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-304 — depth 0 · unblocks 0  **[NO-GATE]**
- title: A PATH THAT RESOLVES CORRECTLY IN THE CHECKOUT AND WRONGLY IN THE IMAGE. recordings.py computes parent.parent…
- scope: `services/shopify-stub/src/recordings.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-305 — depth 0 · unblocks 0  **[NO-GATE]**
- title: FOUR COMMENTS MADE FALSE BY THE FIXES THAT LANDED THIS CYCLE, self-reported by the lane that invalidated them…
- scope: `apps/trust/src/verification/__init__.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-306 — depth 0 · unblocks 0  **[NO-GATE]**
- title: list_prices IS THE IDENTICAL FAIL-OPEN TO T-233, ONE ARGUMENT OVER, and it is on the money path. A signed bid…
- scope: `packages/contracts/src/boundary.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-307 — depth 0 · unblocks 0  **[NO-GATE]**
- title: max_discount_pct SUPPLIED WITHOUT list_prices IS NEVER READ, so a caller that sets a 0% ceiling and forgets t…
- scope: `packages/contracts/src/boundary.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-308 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE SYSTEM HAS NO OBSERVABILITY AT ALL, AND EVERY INFO-LEVEL CALL IS SILENTLY DISCARDED. Repo-wide grep for b…
- scope: `proxyshop_support/`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-309 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE STORE AGENT SERVES ZERO HTTP PATHS. Its app object exists and app.openapi()['paths'] is EMPTY; no routes.…
- scope: `packages/store-agent/src/main.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-310 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE RANKER IS ORPHANED: NOTHING IN apps/exchange/src IMPORTS exchange.ranking. Grep across the served source …
- scope: `apps/exchange/src/auction/routes.py`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-311 — depth 0 · unblocks 0  **[NO-GATE]**
- title: NEITHER UI IS RUNNABLE -- there is no host, no dev server, no build, and no entry point. apps/buyer/package.j…
- scope: `apps/buyer/package.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-312 — depth 0 · unblocks 0  **[NO-GATE]**
- title: SEVEN PUBLISHED ROUTES ARE UNSERVED ACROSS THREE SERVICES, measured by constructing each app and reading app.…
- scope: `packages/contracts/openapi/trust.openapi.json`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-313 — depth 0 · unblocks 0  **[NO-GATE]**
- title: FOUR COMPONENTS HAVE NO DOCKERFILE AND CANNOT BE DEPLOYED: ingest, store-agent, sim and seller-reference. The…
- scope: `services/ingest/`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-314 — depth 0 · unblocks 0  **[NO-GATE]**
- title: THE ONE FULLY-WORKING SERVICE HAS AN IMAGE THAT HAS NEVER BEEN BUILT, and the runbook's step 1 tells you to r…
- scope: `services/shopify-stub/compose.yaml`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-315 — depth 0 · unblocks 0  **[NO-GATE]**
- title: DESIGN.md:145 states docs/demo/ should hold TWO runbooks and that 'the S6 lint reads their union', but the di…
- scope: `docs/demo/`
- verify: `false # NO GATE YET — write one that fails on this finding first`

### T-117 — depth 2 · unblocks 0
- title: [BLOCKED: protected path] The per-ticket gate runs the tests that grade its own acceptance
- scope: `scripts/verify.sh`, `conftest.py`, `proxyshop_support/**`
- verify: `PROXYSHOP_WORKER=0 ./scripts/verify.sh check`

### T-123 — depth 2 · unblocks 0
- title: The .pkgroot namespaces survive a pytest run (root cause: site-packages itself is flagged)
- scope: `scripts/bootstrap.sh`, `conftest.py`, `proxyshop_support/**`
- verify: `export PROXYSHOP_WORKER=0 && ./scripts/bootstrap.sh && ./.venv/bin/python -m pytest packages/llm -q && ./.venv/bin/python -c 'import contracts, llm, trust'`

### T-131 — depth 3 · unblocks 0
- title: The golden answer key is graded weakly, and the dishonest store's flagship lie is not graded at all
- scope: `fixtures/**`
- verify: `PROXYSHOP_WORKER=0 ./scripts/verify.sh check`

### T-120 — depth 4 · unblocks 0
- title: Using PROXYSHOP_ROLE_PASSWORD does not turn the repo gate red
- scope: `proxyshop_support/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest proxyshop_support -q`

### T-124 — depth 4 · unblocks 0
- title: Fresh-volume tests remove the containers and volumes they create
- scope: `apps/trust/**`, `proxyshop_support/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest apps/trust/tests/test_schema_grants.py proxyshop_support/tests/test_role_password_end_to_end.py -q`

### T-127 — depth 4 · unblocks 0
- title: The chain_head DELETE arm is graded by property, not by enumeration
- scope: `apps/trust/**`, `db/migrations/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest apps/trust/tests/test_ledger_chain.py -q`

### T-086 — depth 5 · unblocks 0
- title: Onboarding drives a shadow store to its first real bid
- scope: `e2e/test_onboarding.py`, `e2e/support/onboarding/**`
- verify: `PROXYSHOP_WORKER=0 uv run python -m pytest e2e/test_onboarding.py -q`

### T-054 — depth 7 · unblocks 0
- title: The dashboard shows the walls and the window
- scope: `apps/merchant/app/dashboard/**`
- verify: `npx vitest run apps/merchant/app/dashboard`

## BLOCKED — 31

`waiting on` lists only the UNCLOSED `depends_on` ids, as computed by `frontier`.

- **T-020** (depth 2) waiting on T-012 — Signed fetch adapter ingests a storefront including password-protected dev stores
- **T-021** (depth 3) waiting on T-014, T-020 — Policy pages and marketing claims land in the graph with provenance
- **T-024** (depth 4) waiting on T-021 — Differential refresh keeps the graph current at field-appropriate cadence
- **T-050** (depth 2) waiting on T-013, T-010 — Installing the app wires pixel and webhooks against the stub
- **T-051** (depth 3) waiting on T-050 — Pixel reports checkout outcomes the collector can join
- **T-065** (depth 4) waiting on T-010, T-021 — A golden pitch yields all four verification statuses with evidence
- **T-070** (depth 2) waiting on T-010, T-011 — Buyers authenticate lightly and stores never see who they are
- **T-084** (depth 9) waiting on T-083 — The dishonest store ends below threshold and off the shortlist
- **T-087** (depth 10) waiting on T-084 — The Shopify and onboarding extension runbook covers the beats off the starting path
- **T-100** (depth 2) waiting on T-013 — Shopify stub never emits an off-domain checkout Location
- **T-101** (depth 2) waiting on T-012 — A partial re-embed is never certified as complete
- **T-103** (depth 2) waiting on T-010 — The TypeScript signer refuses integers the wire cannot state
- **T-105** (depth 2) waiting on T-013 — Money arithmetic is asserted absolutely, not against itself
- **T-106** (depth 2) waiting on T-010, T-011 — A conformance gate keeps the two JCS canonicalizers from drifting
- **T-107** (depth 2) waiting on T-010 — claim_id is computed with JCS, not json.dumps
- **T-108** (depth 2) waiting on T-010 — The two envelope gates agree on whitespace
- **T-113** (depth 3) waiting on T-103 — The TypeScript signing path refuses unsafe integers by default, not opt-in
- **T-115** (depth 3) waiting on T-108 — The envelope blank rule is engine-independent again
- **T-116** (depth 3) waiting on T-101 — A single degenerate product cannot black out vector search
- **T-118** (depth 3) waiting on T-101, T-105, T-108 — Wave-2 residue: eight low-severity findings from the lane verifiers
- **T-119** (depth 3) waiting on T-106 — The ledger canonicaliser is one module object under both import spellings
- **T-121** (depth 4) waiting on T-119 — The JCS conformance suite's prose matches the code T-119 changed
- **T-122** (depth 4) waiting on T-119 — Subprocess tests hand the child .pkgroot instead of clobbering PYTHONPATH
- **T-125** (depth 4) waiting on T-113 — The signing door and the canonicalizer agree, and the gate watches both
- **T-126** (depth 4) waiting on T-119 — The ledger spelling binding survives a concurrent first import
- **T-128** (depth 4) waiting on T-113 — Guards that cannot refuse anything are removed, not tested tautologically
- **T-129** (depth 4) waiting on T-113, T-116, T-118 — Wave-3 verification residue: nine findings across four lanes
- **T-130** (depth 3) waiting on T-050, T-070 — Sub-HIGH findings from the feature-wave checkers (backlog sweep, not a wave)
- **T-132** (depth 3) waiting on T-070 — Rotating pseudonyms are trivially re-linkable, so T-070's central guarantee does not hold
- **T-133** (depth 3) waiting on T-070 — Buyer residue: shared session state, unwired publish half, and unbounded stores
- **T-134** (depth 3) waiting on T-050 — Merchant residue: a scope guard that shreds strings, a token in repr, and six inbox findings that n…

## SCHEDULING CONSTRAINTS — file-ownership overlap among OPEN tickets

Every `scope` entry claimed by MORE THAN ONE open ticket. Two tickets sharing a row here must
NOT be dispatched into the same wave as writers. Sorted by number of claimants, then path.

- distinct colliding scope paths: **32**
- open tickets touching at least one colliding path: **88**

| # claimants | scope path | open tickets claiming it |
| --- | --- | --- |
| 10 | `packages/contracts/**` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-125, T-128, T-129 |
| 7 | `apps/trust/**` | T-118, T-119, T-122, T-124, T-126, T-127, T-129 |
| 7 | `e2e/**` | T-082, T-083, T-084, T-106, T-121, T-122, T-125 |
| 6 | `apps/trust/tests/test_repro_open_tickets.py` | T-275, T-287, T-288, T-289, T-290, T-291 |
| 5 | `proxyshop_support/**` | T-117, T-120, T-122, T-123, T-124 |
| 5 | `services/shopify-stub/**` | T-013, T-100, T-105, T-118, T-129 |
| 4 | `apps/buyer/**` | T-070, T-130, T-132, T-133 |
| 4 | `apps/exchange/src/auction/collect.py` | T-271, T-272, T-273, T-276 |
| 4 | `apps/exchange/src/auction/routes.py` | T-148, T-270, T-294, T-310 |
| 4 | `services/ingest/**` | T-101, T-116, T-118, T-129 |
| 4 | `services/ingest/tests/**` | T-012, T-020, T-021, T-024 |
| 3 | `apps/buyer/svc/src/profile/__init__.py` | T-142, T-164, T-221 |
| 3 | `apps/merchant/**` | T-050, T-130, T-134 |
| 3 | `apps/merchant/svc/src/envelope/store.py` | T-239, T-243, T-248 |
| 3 | `apps/trust/tests/**` | T-011, T-064, T-065 |
| 3 | `packages/store-agent/src/external/door.py` | T-244, T-279, T-280 |
| 2 | `apps/buyer/svc/src/auth/magic_link.py` | T-140, T-141 |
| 2 | `apps/exchange/src/auction/ledger.py` | T-150, T-283 |
| 2 | `apps/exchange/src/checkout/codes.py` | T-268, T-269 |
| 2 | `apps/trust/src/snapshot/builder.py` | T-237, T-261 |
| 2 | `apps/trust/src/verification/__init__.py` | T-256, T-305 |
| 2 | `conftest.py` | T-117, T-123 |
| 2 | `db/migrations/**` | T-011, T-127 |
| 2 | `fixtures/**` | T-130, T-131 |
| 2 | `packages/**` | T-122, T-133 |
| 2 | `packages/contracts/openapi/exchange.openapi.json` | T-267, T-297 |
| 2 | `packages/contracts/openapi/trust.openapi.json` | T-296, T-312 |
| 2 | `packages/contracts/src/boundary.py` | T-306, T-307 |
| 2 | `packages/contracts/tests/test_claim_identity.py` | T-251, T-292 |
| 2 | `packages/llm/**` | T-014, T-118 |
| 2 | `services/ingest/src/adapters/mapping.py` | T-245, T-249 |
| 2 | `services/shopify-stub/src/codes.py` | T-253, T-255 |

### Effective (glob-aware) ownership overlap

A literal file scope is ALSO owned by any open ticket whose scope is a directory/glob
covering it (`packages/contracts/**` covers `packages/contracts/src/boundary.py`). This
table resolves that containment and is the one to schedule against: every normalized scope
entry among open tickets that resolves to MORE THAN ONE open owner.

- normalized distinct scope entries among open tickets: **128**
- of those, entries with >1 effective owner: **112**
- no open ticket claims the repo-wide `**` scope.

| # owners | scope path (normalized) | effective open owners |
| --- | --- | --- |
| 39 | `packages/**` | T-010, T-014, T-065, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-130, T-133, T-156, T-161, T-162, T-175, T-194, T-204, T-209, T-218, T-220, T-240, T-244, T-251, T-267, T-278, T-279, T-280, T-281, T-292, T-296, T-297, T-306, T-307, T-309, T-312 |
| 30 | `apps/trust/**` | T-011, T-064, T-065, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-154, T-166, T-167, T-181, T-193, T-207, T-237, T-256, T-258, T-259, T-261, T-275, T-287, T-288, T-289, T-290, T-291, T-302, T-303, T-305 |
| 26 | `packages/contracts/**` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-161, T-162, T-194, T-204, T-240, T-251, T-267, T-278, T-292, T-296, T-297, T-306, T-307, T-312 |
| 25 | `services/**` | T-012, T-013, T-020, T-021, T-024, T-100, T-101, T-105, T-116, T-118, T-122, T-129, T-205, T-236, T-238, T-242, T-245, T-249, T-253, T-254, T-255, T-265, T-304, T-313, T-314 |
| 24 | `apps/exchange/**` | T-130, T-148, T-150, T-159, T-169, T-170, T-260, T-264, T-266, T-268, T-269, T-270, T-271, T-272, T-273, T-276, T-277, T-282, T-283, T-293, T-294, T-295, T-299, T-310 |
| 21 | `apps/trust/tests/**` | T-011, T-064, T-065, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-154, T-166, T-181, T-258, T-259, T-275, T-287, T-288, T-289, T-290, T-291 |
| 17 | `apps/buyer/**` | T-070, T-130, T-132, T-133, T-140, T-141, T-142, T-163, T-164, T-165, T-197, T-198, T-199, T-221, T-284, T-300, T-311 |
| 16 | `apps/trust/tests/test_repro_open_tickets.py` | T-011, T-064, T-065, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-275, T-287, T-288, T-289, T-290, T-291 |
| 15 | `services/ingest/**` | T-012, T-020, T-021, T-024, T-101, T-116, T-118, T-122, T-129, T-236, T-238, T-245, T-249, T-254, T-313 |
| 14 | `packages/contracts/openapi/exchange.openapi.json` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-267, T-297 |
| 14 | `packages/contracts/openapi/trust.openapi.json` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-296, T-312 |
| 14 | `packages/contracts/src/boundary.py` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-306, T-307 |
| 14 | `packages/contracts/tests/test_claim_identity.py` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-251, T-292 |
| 13 | `apps/merchant/**` | T-050, T-051, T-054, T-130, T-134, T-203, T-239, T-243, T-246, T-247, T-248, T-285, T-298 |
| 13 | `packages/contracts/openapi/exchange.openapi.json:169 (denial_reason) + apps/exchange/src/accept/offer.py` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-204 |
| 13 | `packages/contracts/openapi/merchant.openapi.json` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-240 |
| 13 | `packages/contracts/src/boundary.py (validate_bid) — external path` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-162 |
| 13 | `packages/contracts/src/boundary.py:157 (_source_verdict)` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-161 |
| 13 | `packages/contracts/src/ts/schemas.ts:63-65 (ajv + ajv-formats) vs packages/contracts/src/boundary.py:353-365 (pydantic)` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-194 |
| 13 | `packages/contracts/tests/price_parity_corpus.json` | T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-122, T-125, T-128, T-129, T-133, T-278 |
| 13 | `packages/store-agent/**` | T-122, T-130, T-133, T-156, T-175, T-209, T-218, T-220, T-244, T-279, T-280, T-281, T-309 |
| 11 | `apps/trust/src/snapshot/**` | T-064, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-237, T-261, T-303 |
| 11 | `apps/trust/tests/_fixtures_events.py` | T-011, T-064, T-065, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-154 |
| 11 | `apps/trust/tests/test_events.py` | T-011, T-064, T-065, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-166 |
| 11 | `apps/trust/tests/test_events_hardening.py (DSN precedence coverage)` | T-011, T-064, T-065, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-181 |
| 11 | `apps/trust/tests/test_feedback_push.py` | T-011, T-064, T-065, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-259 |
| 11 | `apps/trust/tests/test_ledger_chain.py` | T-011, T-064, T-065, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-258 |
| 11 | `services/shopify-stub/**` | T-013, T-100, T-105, T-118, T-122, T-129, T-205, T-253, T-255, T-304, T-314 |
| 10 | `apps/trust/src/snapshot/builder.py` | T-064, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-237, T-261 |
| 10 | `apps/trust/src/verification/**` | T-065, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-256, T-305 |
| 10 | `apps/trust/src/verification/__init__.py` | T-065, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-256, T-305 |
| 10 | `services/ingest/src/adapters/**` | T-020, T-101, T-116, T-118, T-122, T-129, T-238, T-245, T-249, T-313 |
| 10 | `services/ingest/tests/**` | T-012, T-020, T-021, T-024, T-101, T-116, T-118, T-122, T-129, T-313 |
| 9 | `apps/trust/src/snapshot/delisting.py` | T-064, T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-303 |
| 9 | `e2e/**` | T-082, T-083, T-084, T-086, T-106, T-121, T-122, T-125, T-286 |
| 9 | `proxyshop_support/**` | T-117, T-120, T-122, T-123, T-124, T-171, T-172, T-252, T-308 |
| 9 | `services/ingest/src/adapters/mapping.py` | T-020, T-101, T-116, T-118, T-122, T-129, T-245, T-249, T-313 |
| 8 | `apps/trust/Dockerfile (COPY set) vs packages/verification/**` | T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-193 |
| 8 | `apps/trust/src/ledger/**` | T-011, T-118, T-119, T-122, T-124, T-126, T-127, T-129 |
| 8 | `apps/trust/src/reconcile/engine.py` | T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-302 |
| 8 | `apps/trust/src/scoring/engine.py (weight-table comment) vs fixtures/manifest.json observation_weights` | T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-207 |
| 8 | `apps/trust/src/{scoring,reconcile,feedback,snapshot}/_binding.py` | T-118, T-119, T-122, T-124, T-126, T-127, T-129, T-167 |
| 8 | `e2e/support/onboarding/**` | T-082, T-083, T-084, T-086, T-106, T-121, T-122, T-125 |
| 8 | `e2e/test_onboarding.py` | T-082, T-083, T-084, T-086, T-106, T-121, T-122, T-125 |
| 8 | `e2e/test_s1_flow.py` | T-082, T-083, T-084, T-106, T-121, T-122, T-125, T-286 |
| 8 | `services/ingest/src/adapters/netguard.py` | T-020, T-101, T-116, T-118, T-122, T-129, T-238, T-313 |
| 8 | `services/ingest/src/extraction/**` | T-021, T-101, T-116, T-118, T-122, T-129, T-254, T-313 |
| 8 | `services/ingest/src/extraction/claims.py` | T-021, T-101, T-116, T-118, T-122, T-129, T-254, T-313 |
| 8 | `services/shopify-stub/src/codes.py` | T-013, T-100, T-105, T-118, T-122, T-129, T-253, T-255 |
| 7 | `apps/buyer/svc/src/profile/__init__.py` | T-070, T-130, T-132, T-133, T-142, T-164, T-221 |
| 7 | `proxyshop_support/neo4j_lock.py:79,119-131 + pyproject.toml:71 + conftest.py` | T-117, T-120, T-122, T-123, T-124, T-171, T-308 |
| 7 | `proxyshop_support/service_markers.py:78-85 (whole-stack fallback)` | T-117, T-120, T-122, T-123, T-124, T-172, T-308 |
| 7 | `proxyshop_support/tests/test_fixture_loader.py` | T-117, T-120, T-122, T-123, T-124, T-252, T-308 |
| 7 | `services/ingest/src/embeddings/**` | T-012, T-101, T-116, T-118, T-122, T-129, T-313 |
| 7 | `services/ingest/src/graph/**` | T-012, T-101, T-116, T-118, T-122, T-129, T-313 |
| 7 | `services/ingest/src/main.py` | T-101, T-116, T-118, T-122, T-129, T-236, T-313 |
| 7 | `services/ingest/src/scheduler/**` | T-024, T-101, T-116, T-118, T-122, T-129, T-313 |
| 7 | `services/shopify-stub/compose.yaml` | T-013, T-100, T-105, T-118, T-122, T-129, T-314 |
| 7 | `services/shopify-stub/src/recordings.py` | T-013, T-100, T-105, T-118, T-122, T-129, T-304 |
| 7 | `services/shopify-stub/tests/test_stub_contract.py:40 via conftest.py` | T-013, T-100, T-105, T-118, T-122, T-129, T-205 |
| 6 | `apps/buyer/svc/src/auth/magic_link.py` | T-070, T-130, T-132, T-133, T-140, T-141 |
| 6 | `apps/merchant/svc/src/envelope/store.py` | T-050, T-130, T-134, T-239, T-243, T-248 |
| 6 | `packages/store-agent/src/external/door.py` | T-122, T-130, T-133, T-244, T-279, T-280 |
| 5 | `apps/buyer/package.json` | T-070, T-130, T-132, T-133, T-311 |
| 5 | `apps/buyer/svc/src/auth/routes.py (POST /buyer/auth/magic-link) + routes.py:57 ProcessLocalStateUnsafe` | T-070, T-130, T-132, T-133, T-165 |
| 5 | `apps/buyer/svc/src/auth/sessions.py` | T-070, T-130, T-132, T-133, T-163 |
| 5 | `apps/buyer/svc/src/feedback/submission.py` | T-070, T-130, T-132, T-133, T-284 |
| 5 | `apps/buyer/svc/src/intent/routes.py` | T-070, T-130, T-132, T-133, T-300 |
| 5 | `apps/buyer/svc/src/profile/__init__.py:1038 (_BUCKET_VOCABULARY['category_affinity'])` | T-070, T-130, T-132, T-133, T-199 |
| 5 | `apps/buyer/svc/src/profile/__init__.py:436 (_MIN_LEAKABLE = 4)` | T-070, T-130, T-132, T-133, T-197 |
| 5 | `apps/buyer/svc/src/profile/__init__.py:918 (_identity_sources) and` | T-070, T-130, T-132, T-133, T-198 |
| 5 | `apps/exchange/src/auction/collect.py` | T-130, T-271, T-272, T-273, T-276 |
| 5 | `apps/exchange/src/auction/routes.py` | T-130, T-148, T-270, T-294, T-310 |
| 5 | `apps/merchant/svc/tests/**` | T-050, T-051, T-130, T-134, T-285 |
| 5 | `apps/merchant/svc/tests/test_repro_open_tickets.py` | T-050, T-051, T-130, T-134, T-285 |
| 4 | `apps/merchant/Dockerfile` | T-050, T-130, T-134, T-298 |
| 4 | `apps/merchant/app/dashboard/**` | T-050, T-054, T-130, T-134 |
| 4 | `apps/merchant/svc/src/codes/ (T-052) — binding requirement, not a defect` | T-050, T-130, T-134, T-203 |
| 4 | `apps/merchant/svc/src/collector/**` | T-050, T-051, T-130, T-134 |
| 4 | `apps/merchant/svc/src/envelope/model.py` | T-050, T-130, T-134, T-246 |
| 4 | `apps/merchant/svc/src/install/webhooks.py` | T-050, T-130, T-134, T-247 |
| 4 | `fixtures/**` | T-021, T-130, T-131, T-208 |
| 4 | `packages/llm/**` | T-014, T-118, T-122, T-133 |
| 4 | `packages/store-agent/src/hooks/provenance.py (_price_reconciliation_refusal)` | T-122, T-130, T-133, T-156 |
| 4 | `packages/store-agent/src/hooks/provenance.py:129,:496-507 (commitments walk, no pydantic gate)` | T-122, T-130, T-133, T-209 |
| 4 | `packages/store-agent/src/hooks/provenance.py:523 (`if node is None: return`) — T-209 is one of a family of at least six` | T-122, T-130, T-133, T-218 |
| 4 | `packages/store-agent/src/hooks/provenance.py:830-834 vs DESIGN.md` | T-122, T-130, T-133, T-175 |
| 4 | `packages/store-agent/src/main.py` | T-122, T-130, T-133, T-309 |
| 4 | `packages/store-agent/tests/test_price_reconciliation.py (depth parametrization) — cycle detection has an invisible ceiling` | T-122, T-130, T-133, T-220 |
| 4 | `packages/store-agent/tests/test_repro_external_door.py` | T-122, T-130, T-133, T-281 |
| 3 | `apps/exchange/src/auction/ledger.py` | T-130, T-150, T-283 |
| 3 | `apps/exchange/src/checkout/codes.py` | T-130, T-268, T-269 |
| 3 | `fixtures/manifest.json observation_weights — no weight for an uncorroborated complaint` | T-130, T-131, T-208 |
| 3 | `fixtures/pages/**` | T-021, T-130, T-131 |
| 3 | `packages/verification/**` | T-065, T-122, T-133 |
| 2 | `apps/exchange/Dockerfile` | T-130, T-299 |
| 2 | `apps/exchange/src/accept/ (no routes.py) vs apps/exchange/src/main.py` | T-130, T-170 |
| 2 | `apps/exchange/src/accept/offer.py:94,:328 + apps/exchange/src/accept/__init__.py` | T-130, T-169 |
| 2 | `apps/exchange/src/auction/__init__.py` | T-130, T-282 |
| 2 | `apps/exchange/src/checkout/provider.py` | T-130, T-264 |
| 2 | `apps/exchange/src/checkout/providers.py` | T-130, T-293 |
| 2 | `apps/exchange/src/main.py` | T-130, T-266 |
| 2 | `apps/exchange/src/orchestration/solicitation.py` | T-130, T-295 |
| 2 | `apps/exchange/src/retrieval/service.py` | T-130, T-260 |
| 2 | `apps/exchange/tests/test_checkout_provider.py:406 (and repo-wide)` | T-130, T-159 |
| 2 | `apps/exchange/tests/test_repro_untrusted_roster.py` | T-130, T-277 |
| 2 | `conftest.py` | T-117, T-123 |
| 2 | `db/migrations/**` | T-011, T-127 |
| 2 | `docs/demo/**` | T-087, T-315 |
| 2 | `docs/demo/shopify-onboarding-extension.md` | T-087, T-315 |
| 2 | `services/sim/src/runner.py` | T-122, T-242 |
| 2 | `services/sim/tests/test_simulation.py` | T-122, T-265 |

## REJECTED / NEEDS RE-PLAN

`frontier` reports exactly ONE ticket carrying a REJECTED verdict that keeps it OPEN: **T-082**
(it therefore still appears in READY above). The second rejected record below names no `tickets`
field, so it closes/reopens nothing; it is a verifier verdict on T-177, which is CLOSED in the graph.

### T-082 (OPEN)
- dispatch record: action=`close` agent_ref=`lane-T-082-wip` branch=`task/T-082` closed_at=`2026-09-04T02:20:50` sha=`74540b69bcf67a31d446d1f7135eb5f74ea1d6d7`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): REJECTED back to the backlog as unfinished WIP; branch task/T-082 and its worktree are PRESERVED as the retry packet's input. Three independent reasons, none of them the gate: (1) it is salvaged WIP from a lane that died on a session rate limit and never ran its own gates against these bytes; (2) e2e/test_s1_flow.py carries THREE ruff I001 errors (:255, :303, :608) which take build_succeeds from 1 to 0 on merge; (3) two sabotage-proven blind spots - replacing merchant_svc.install.webhooks.verify with 'lambda: True', or mint_code with the constant 'NOT-A-PSX-CODE', leaves all 23 of its tests passing, confirmed with a controlled matched pair so these are genuine misses rather than sabotage that failed to land. What IS good and should be reused: the flow exercises the real product path and sabotage caught 5 of 7 product functions; e2e/test_s1_flow.py:64-80 asserts the run drove no code from outside the worktree and fires on real data (13 modules). SEPARATELY and not a reason for this rejection: its gate names its own deliverable so red-check at the merge base can never stamp red - that is the T-263 class and ESC-015, unaffected by this decision and still the user's to rule on. The retry needs a gate that can go red: either a frozen acceptance test that grades the S1 flow (none exists today) or the harness fix in ESC-015 option 1.

### (no `tickets` field — advisory verifier verdict, closes nothing; concerns T-177, closed)
- dispatch record: action=`close` agent_ref=`rung2-c13` branch=`main` closed_at=`2026-09-03T13:56:42` sha=`15958ab935e07e3c38fc3a2c2e59af6766008e77`
- reason (verbatim from `.swarm-loop/dispatch.jsonl`): PARTIAL — the verifier itself was killed by the network outage, but one of its lenses returned before dying and its findings are minted: T-223 (CRITICAL, the price floor is an exact equality so unit_price 0.001 clears a 100.00 product under max_discount_pct 100, and on an uncapped roster row _is_judged skips the check entirely so 1e-09 returns HTTP 201) and T-224 (HIGH, unauthenticated HTTP 500 out of the middle of an auction via list_price 0.0 plus an unparseable price, and a 0.00 rankable fallback on that same row). Verdict on T-177: DEFECTIVE at the production door — the attack its own ticket described is genuinely closed, these three bypasses stand beside it. Two of its hypotheses did NOT survive measurement and are recorded so nobody re-chases them: the suspected __init__.py production break does not exist (the import was reflowed, not deleted, and production imports the submodule directly), and the tolerance concern is backwards (0.01 is ABSOLUTE, so maximum underpayment on a 10,000 item is one cent — absolute is correct here). COVERAGE GAP, must be re-run: the cross-branch interaction lens (T-214's lock x T-216's shared-database handling x T-206's ledger replay, run together in one process) died before reporting. That class has already produced a 600-second self-deadlock in this repo once. Re-dispatch it against the post-wave main rather than against 7279452, since eight lanes are mid-flight.

