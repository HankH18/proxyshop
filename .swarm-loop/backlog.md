# ProxyShop — ticket ledger (backlog)

> **This file is a DERIVED HUMAN VIEW. `tickets.json` is the ground truth.**
> This run has a build-docs intake, so the machine-readable graph is authoritative and this
> ledger is regenerated from it. Do not hand-edit; regenerate. Any amendment that changes the
> graph must re-seed this file, or the human view goes stale in exactly the way nobody checks.

Regenerated 2026-09-03 at cycle 15 from tickets.json @ HEAD.

## Counts (from the graph, not recalled)

| | |
|---|---|
| tickets in graph | **176** |
| closed | 74 |
| ready | 72 |
| blocked | 30 |
| in flight this epoch | 5 — T-023, T-053, T-071, T-081, T-228 |

**Closure source.** The graph carries no status field; closure is supplied to `frontier` from
two positive-evidence sources and nothing else: every ticket whose frozen acceptance tests all
pass (28 tickets), unioned with the `dispatch` ledger's `accepted` verdicts. Anything
unproven stays OPEN by design — a false-CLOSED silently drops real work, a false-OPEN only burns
a lane.

## In flight (epoch 15)

- **T-023** — Catalog MCP adapter passes recorded-contract tests
  - scope: `services/ingest/src/adapters/**, services/ingest/tests/**, fixtures/mcp/**`
- **T-053** — A plain-language interview yields an approved, versioned envelope
  - scope: `apps/merchant/svc/src/onboarding/**, apps/merchant/svc/tests/**, fixtures/interviews/**, apps/merchant/svc/src/envelope/**`
- **T-071** — Three questions or fewer produce a confirmed structured intent
  - scope: `apps/buyer/svc/src/intent/**, apps/buyer/app/intent/**, apps/buyer/svc/tests/**, fixtures/dialogues/**`
- **T-081** — Simulated buyers exercise the whole network from one seed
  - scope: `services/sim/**`
- **T-228** — make verify fails the types gate with 8 mypy errors across 3 store-agent modules merged in cycle 14, holding b
  - scope: `packages/store-agent/src`

## Ready (dependencies closed, dispatchable)

- **T-013** — Shopify stub reproduces the exact surface the system uses
  - scope: `services/shopify-stub/**`
- **T-110** — Role passwords have one source of truth
  - scope: `db/init/**`
- **T-102** — The chain_head guard trigger's DELETE arm is graded by a test
  - scope: `apps/trust/**`
- **T-103** — The TypeScript signer refuses integers the wire cannot state
  - scope: `packages/contracts/**`
- **T-106** — A conformance gate keeps the two JCS canonicalizers from drifting
  - scope: `e2e/**`
- **T-031** — Candidate retrieval and fit scoring feed the ranker
  - scope: `apps/exchange/src/retrieval/**, apps/exchange/tests/**`
- **T-101** — A partial re-embed is never certified as complete
  - scope: `services/ingest/**`
- **T-108** — The two envelope gates agree on whitespace
  - scope: `packages/contracts/**`
- **T-104** — The prompt cache key cannot collide on separator text
  - scope: `packages/llm/**`
- **T-109** — A datastore blip cannot silently empty the security gate
  - scope: `conftest.py, proxyshop_support/**`
- **T-111** — Provisioning fails loudly when the flat namespaces are dead
  - scope: `scripts/bootstrap.sh`
- **T-052** — Winning offers become single-use validated codes and permalinks
  - scope: `apps/merchant/svc/src/codes/**, apps/merchant/svc/tests/**`
- **T-140** — AccountDirectory has exactly one implementation and no production populator. build_auth_service() never passes
  - scope: `apps/buyer/svc/src/auth/magic_link.py`
- **T-141** — The magic-link token is delivered to the default no-op `_drop` (line 166) in every deployment, and set_auth_se
  - scope: `apps/buyer/svc/src/auth/magic_link.py`
- **T-142** — publish_profile — the only writer of app.buyer_accounts, described as 'the store-visible working set' — has ze
  - scope: `apps/buyer/svc/src/profile/__init__.py`
- **T-148** — `configure_auctions` — the only way to give the exchange a real solicitor, a real eligibility source, a Redis-
  - scope: `apps/exchange/src/auction/routes.py`
- **T-150** — The ledger writer has zero producers. Nothing in the repository writes an event into it - not by import, not o
  - scope: `apps/exchange/src/auction/ledger.py`
- **T-154** — EVENTS_ROLE = "app", and its comment asserts app is 'the role the writer connects as... deliberately not trust
  - scope: `apps/trust/tests/_fixtures_events.py`
- **T-156** — offer.total_price is never reconciled against unit_price: an offer stating unit_price=80.0 with total_price=1.
  - scope: `packages/store-agent/src/hooks/provenance.py (_price_reconciliation_refusal)`
- **T-157** — An off-domain permalink from a delegating merchant client leaves a LIVE single-use discount code with no ledge
  - scope: `apps/exchange/src/checkout/provider.py`
- **T-158** — The one-accept-per-auction guard is only as durable as the record handed in. accept() stamps the auction OBJEC
  - scope: `apps/exchange/src/accept/offer.py (module docstring) + AuctionStateMachine`
- **T-159** — SYSTEMIC GATE-CREDIBILITY DEFECT: tests that swallow a missing subject with `except ImportError` are VACUOUS u
  - scope: `apps/exchange/tests/test_checkout_provider.py:406 (and repo-wide)`
- **T-160** — These three tickets' recorded `verify` commands pass IDENTICALLY with and without their defect, which is the p
  - scope: `tickets.json (T-109, T-111, T-123 verify fields)`
- **T-161** — A provenance block nested inside a hook-provenanced claim's opaque `value` is not walked. Measured: make_bid(c
  - scope: `packages/contracts/src/boundary.py:157 (_source_verdict)`
- **T-162** — The boundary checks provenance SOURCE but never discount AUTHORISATION. make_bid(claims=[make_claim("policy", 
  - scope: `packages/contracts/src/boundary.py (validate_bid) — external path`
- **T-163** — T-133 item (c): SessionStore.open() validates only that the subject carries the 'psn-' PREFIX — it never check
  - scope: `apps/buyer/svc/src/auth/sessions.py`
- **T-164** — build_buckets() and anonymise_cohort() are public and run NO identity-leak check; only build_profile()/build_p
  - scope: `apps/buyer/svc/src/profile/__init__.py`
- **T-165** — No rate limiter on the unauthenticated magic-link endpoint, and the session/pending-link store behind it is pr
  - scope: `apps/buyer/svc/src/auth/routes.py (POST /buyer/auth/magic-link) + routes.py:57 ProcessLocalStateUnsafe`
- **T-166** — test_replay_with_snapshots_names_the_missing_scorer branches on `if response.status_code == 503`. Now that T-0
  - scope: `apps/trust/tests/test_events.py`
- **T-167** — Four BYTE-IDENTICAL copies of the elected-primary module-binding shim (md5 56d63d33...), one per E6 feature pa
  - scope: `apps/trust/src/{scoring,reconcile,feedback,snapshot}/_binding.py`
- **T-169** — T-033's registered-domain guard is DECORATIVE in the deployed configuration. `_platform_domains` is module sta
  - scope: `apps/exchange/src/accept/offer.py:94,:328 + apps/exchange/src/accept/__init__.py`
- **T-170** — T-033's objective opens 'Accept endpoint resolves a CheckoutProvider by CHECKOUT_MODE...', but the accept pack
  - scope: `apps/exchange/src/accept/ (no routes.py) vs apps/exchange/src/main.py`
- **T-171** — The D37 Neo4j lock is MACHINE-GLOBAL (/private/tmp/proxyshop-neo4j.lock) and PROXYSHOP_WORKER does not isolate
  - scope: `proxyshop_support/neo4j_lock.py:79,119-131 + pyproject.toml:71 + conftest.py`
- **T-172** — T-109's per-service inference has an 8-item hole and every item in it is a Postgres-only test that a Redis-onl
  - scope: `proxyshop_support/service_markers.py:78-85 (whole-stack fallback)`
- **T-175** — Neither the new price-reconciliation wall nor the pre-existing floor wall looks at Offer.total_price (PRICE_FI
  - scope: `packages/store-agent/src/hooks/provenance.py:830-834 vs DESIGN.md`
- **T-181** — T-151's DSN ORDER RESTS ON A SINGLE ASSERTION IN A SINGLE TEST OF 327. Sabotage S3a (reorder the tuple, i.e. r
  - scope: `apps/trust/tests/test_events_hardening.py (DSN precedence coverage)`
- **T-192** — THE 26/26 THAT MOVED E6 TO TARGET IS A WEAKER INSTRUMENT THAN THE LANE'S OWN UNIT SUITE. 7 of 19 real mutation
  - scope: `.swarm-loop/acceptance/test_e6_trust.py (the E6 metric as an instrument)`
- **T-193** — T-065 CRASHES IN THE DEPLOYED IMAGE. apps/trust/Dockerfile does not COPY packages/verification, so trust.verif
  - scope: `apps/trust/Dockerfile (COPY set) vs packages/verification/`
- **T-194** — THE TWO DOORS DO NOT RUN THE SAME SCHEMA CHECK — 21 measured ok-divergences. TypeScript validates through ajv 
  - scope: `packages/contracts/src/ts/schemas.ts:63-65 (ajv + ajv-formats) vs packages/contracts/src/boundary.py:353-365 (pydantic)`
- **T-197** — Identity fragments shorter than 4 characters are never tracked ANYWHERE in the leak backstop, so short names w
  - scope: `apps/buyer/svc/src/profile/__init__.py:436 (_MIN_LEAKABLE = 4)`
- **T-198** — Two residual identity channels the T-189 fix deliberately stopped short of. (a) A number REGROUPED rather than
  - scope: `apps/buyer/svc/src/profile/__init__.py:918 (_identity_sources) and`
- **T-199** — The bucket vocabulary holds every CATEGORY_TAXONOMY label out of the leak haystack on the grounds that they ar
  - scope: `apps/buyer/svc/src/profile/__init__.py:1038 (_BUCKET_VOCABULARY['category_affinity'])`
- **T-202** — T-157 IS FIXED AT THE PORT AND STILL OPEN END TO END. The checkout port now carries the live discount code out
  - scope: `apps/exchange/src/accept/offer.py:390 (bare `except Exception`) vs the new OrphanedCheckoutCode.orphan`
- **T-203** — BINDING REQUIREMENT ON A SCHEDULED TICKET, recorded the way T-041 carried T-152's. The percent-vs-fraction mis
  - scope: `apps/merchant/svc/src/codes/ (T-052) — binding requirement, not a defect`
- **T-204** — REFUSAL REASONS ARE UNENUMERATED AND NOTHING ASSERTS ON THEM. accept() formats type(exc).__name__ into denial_
  - scope: `packages/contracts/openapi/exchange.openapi.json:169 (denial_reason) + apps/exchange/src/accept/offer.py`
- **T-205** — SECOND LATENT except-ImportError VACUITY, found by the AST sweep T-159 asked for. The whole module skips if sh
  - scope: `services/shopify-stub/tests/test_stub_contract.py:40 via conftest.py`
- **T-207** — The engine's gloss on the weight table CONTRADICTS THE APPROVED MANIFEST on what mismatch_return means. The co
  - scope: `apps/trust/src/scoring/engine.py (weight-table comment) vs fixtures/manifest.json observation_weights`
- **T-208** — There is no published weight for a buyer complaint that is NOT corroborated by a return. It currently lands at
  - scope: `fixtures/manifest.json observation_weights — no weight for an uncorroborated complaint`
- **T-209** — THE CONTRACTS BOUNDARY WAS TIGHTENED AND THE STORE-AGENT'S OWN AUDITOR WAS NOT. T-195's fix regenerates the mo
  - scope: `packages/store-agent/src/hooks/provenance.py:129,:496-507 (commitments walk, no pydantic gate)`
- **T-210** — TWO CORRECTIONS, ONE OF THEM TO MY OWN ASSERTION. (1) I claimed the chflags window could corrupt a metric read
  - scope: `ORCHESTRATION — measuring while the swarm runs (supersedes my T-174 rationale)`
- **T-218** — T-209 CONFIRMED BUT UNDERSTATED, and the cause is generic rather than specific to commitments. The store-agent
  - scope: `packages/store-agent/src/hooks/provenance.py:523 (`if node is None: return`) — T-209 is one of a family of at least six`
- **T-219** — NOTHING CONSUMES THE SELECTION COUNT, so T-117's defect is closed for the `docker` marker but not as a class. 
  - scope: `scripts/verify.sh SELECTION line + .swarm-loop/goals.json build_succeeds — T-117 acceptance 2 is only literally satisfied`
- **T-220** — The cycle-detection replacement for MAX_SWEEP_DEPTH is correct, but nothing in the suite would notice a depth 
  - scope: `packages/store-agent/tests/test_price_reconciliation.py (depth parametrization) — cycle detection has an invisible ceiling`
- **T-221** — THE K-ANONYMITY FLOOR T-138 DELIVERED IS SWITCHED OFF BY DEFAULT, so the re-linkability T-138 exists to preven
  - scope: `apps/buyer/svc/src/profile/__init__.py`
- **T-222** — A DEFECT WAS CONVERTED INTO DOCUMENTATION AND THEN PINNED BY A PASSING TEST, so the suite now CERTIFIES the br
  - scope: `apps/exchange/src/accept/offer.py`
- **T-223** — THE PRICE FLOOR IS AN EXACT EQUALITY, SO ANY POSITIVE PRICE CLEARS IT AND A 100.00 PRODUCT SELLS FOR ONE CENT.
  - scope: `packages/contracts/src/boundary.py:814-819 and apps/exchange/src/auction/collect.py`
- **T-224** — UNAUTHENTICATED HTTP 500 OUT OF THE MIDDLE OF AN AUCTION. `list_price: 0.0` is an accepted roster value (`Fiel
  - scope: `apps/exchange/src/auction/routes.py:231 and apps/exchange/src/auction/collect.py`
- **T-225** — A TEST THAT ASSERTS NOTHING AND REPORTS GREEN, in three copies. `test_scope_directories_exist` reads `for rela
  - scope: `apps/seller-reference/tests/test_scaffold_smoke.py`
- **T-226** — EVERY MEMBER'S tests/ DIRECTORY IS OUTSIDE THE TYPE GATE, so type errors in tests never reach `make types`. Ro
  - scope: `pyproject.toml`
- **T-227** — THE FROZEN TEST AND THE PRODUCT CAN SELECT DIFFERENT MANIFEST DOCUMENTS, so the suite would grade one document
  - scope: `.swarm-loop/acceptance/test_e4_store_agent.py::_load_fixture_manifest vs apps/seller-reference/src/personas/__init__.py`
- **T-107** — claim_id is computed with JCS, not json.dumps
  - scope: `packages/contracts/**`
- **T-022** — Same products across stores link via entity resolution
  - scope: `services/ingest/src/er/**, services/ingest/tests/**, fixtures/er/**`
- **T-130** — Sub-HIGH findings from the feature-wave checkers (backlog sweep, not a wave)
  - scope: `apps/exchange/**, packages/store-agent/**, apps/merchant/**, apps/buyer/**, fixtures/**`
- **T-131** — The golden answer key is graded weakly, and the dishonest store's flagship lie is not graded at all
  - scope: `fixtures/**`
- **T-132** — Rotating pseudonyms are trivially re-linkable, so T-070's central guarantee does not hold
  - scope: `apps/buyer/**`
- **T-133** — Buyer residue: shared session state, unwired publish half, and unbounded stores
  - scope: `apps/buyer/**, packages/**`
- **T-134** — Merchant residue: a scope guard that shreds strings, a token in repr, and six inbox findings that never reache
  - scope: `apps/merchant/**`

## Blocked (waiting on an open dependency)

- **T-024** — waiting on T-023 — Differential refresh keeps the graph current at field-appropriate cadence
- **T-054** — waiting on T-052, T-053 — The dashboard shows the walls and the window
- **T-072** — waiting on T-071 — Shortlists render with provenance and accept hands off cleanly
- **T-073** — waiting on T-072 — Routed buyers can answer one structured feedback prompt
- **T-082** — waiting on T-072, T-081 — One scripted run proves the full S1 flow
- **T-083** — waiting on T-081 — Both learning loops demonstrably move under seeded outcomes
- **T-084** — waiting on T-083 — The dishonest store ends below threshold and off the shortlist
- **T-085** — waiting on T-082 — The starting-slice demo is a runbook anyone on the team can execute
- **T-086** — waiting on T-053 — Onboarding drives a shadow store to its first real bid
- **T-087** — waiting on T-053, T-084, T-085 — The Shopify and onboarding extension runbook covers the beats off the starting path
- **T-100** — waiting on T-013 — Shopify stub never emits an off-domain checkout Location
- **T-105** — waiting on T-013 — Money arithmetic is asserted absolutely, not against itself
- **T-112** — waiting on T-110 — The role password has one source of truth on the project's own fresh volume
- **T-113** — waiting on T-103 — The TypeScript signing path refuses unsafe integers by default, not opt-in
- **T-114** — waiting on T-102 — The chain_head trigger test does not block the ENABLE ALWAYS hardening
- **T-115** — waiting on T-108 — The envelope blank rule is engine-independent again
- **T-116** — waiting on T-101 — A single degenerate product cannot black out vector search
- **T-117** — waiting on T-109 — [BLOCKED: protected path] The per-ticket gate runs the tests that grade its own acceptance
- **T-118** — waiting on T-101, T-102, T-104, T-105, T-108, T-110 — Wave-2 residue: eight low-severity findings from the lane verifiers
- **T-119** — waiting on T-106 — The ledger canonicaliser is one module object under both import spellings
- **T-120** — waiting on T-112 — Using PROXYSHOP_ROLE_PASSWORD does not turn the repo gate red
- **T-121** — waiting on T-119 — The JCS conformance suite's prose matches the code T-119 changed
- **T-122** — waiting on T-119 — Subprocess tests hand the child .pkgroot instead of clobbering PYTHONPATH
- **T-123** — waiting on T-111 — The .pkgroot namespaces survive a pytest run (root cause: site-packages itself is flagged)
- **T-124** — waiting on T-112 — Fresh-volume tests remove the containers and volumes they create
- **T-125** — waiting on T-113 — The signing door and the canonicalizer agree, and the gate watches both
- **T-126** — waiting on T-119 — The ledger spelling binding survives a concurrent first import
- **T-127** — waiting on T-114 — The chain_head DELETE arm is graded by property, not by enumeration
- **T-128** — waiting on T-113 — Guards that cannot refuse anything are removed, not tested tautologically
- **T-129** — waiting on T-113, T-116, T-118 — Wave-3 verification residue: nine findings across four lanes

## Scheduling constraints (blast radius the globs make explicit)

These pairs declare INTERSECTING scope globs and may never be in flight together.
`check-wave` refuses them mechanically; they are listed so the next frontier is built knowing it.

- `apps/buyer/**` — shared by T-130, T-132, T-133
- `apps/buyer/svc/src/auth/magic_link.py` — shared by T-140, T-141
- `apps/buyer/svc/src/profile/__init__.py` — shared by T-142, T-164, T-221
- `apps/buyer/svc/tests/**` — shared by T-071, T-072, T-073
- `apps/merchant/**` — shared by T-130, T-134
- `apps/merchant/svc/tests/**` — shared by T-052, T-053
- `apps/trust/**` — shared by T-102, T-114, T-118, T-119, T-122, T-124, T-126, T-127, T-129
- `apps/trust/**, db/migrations/**` — shared by T-114, T-127
- `apps/trust/**, packages/contracts/**, services/ingest/**, services/shopify-stub/**` — shared by T-118, T-129
- `apps/trust/**, proxyshop_support/**` — shared by T-122, T-124
- `conftest.py, proxyshop_support/**` — shared by T-109, T-117, T-123
- `db/init/**` — shared by T-110, T-118
- `e2e/**` — shared by T-082, T-083, T-084, T-106, T-121, T-122, T-125
- `fixtures/**` — shared by T-130, T-131
- `packages/**` — shared by T-122, T-133
- `packages/contracts/**` — shared by T-103, T-107, T-108, T-113, T-115, T-118, T-125, T-128, T-129
- `packages/llm/**` — shared by T-104, T-118
- `proxyshop_support/**` — shared by T-109, T-112, T-117, T-120, T-122, T-123, T-124
- `scripts/bootstrap.sh` — shared by T-111, T-123
- `services/ingest/**` — shared by T-101, T-116, T-118, T-129
- `services/ingest/tests/**` — shared by T-022, T-023, T-024
- `services/shopify-stub/**` — shared by T-013, T-100, T-105, T-118, T-129

