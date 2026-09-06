# Backlog ledger — OPEN tickets and scheduling constraints

**`tickets.json` at the repo root is GROUND TRUTH. This file is a DERIVED human view of it.**
Seeding is one-way: nothing re-seeds this file, so it goes stale the moment the graph is amended.
Regenerate it from the graph after EVERY amendment (H-25), and never dispatch off this file alone.

- generated: `2026-09-06T04:31:26Z`  ·  HEAD: `1107381` (`110738129a1092ee68acd21b643e000ab5830b0f`)
- derived from: `tickets.json` (299-ticket graph, read-only — it is frozen and hashed into the run manifest)
- closure source: `python3 ~/.claude/skills/swarm-loop/scripts/swarmloop.py frontier --closed-from-ledger --closed-from-merged --json`
- closure is MECHANICAL. A ticket's own `status` field is read NOWHERE in this file — the graph carries
  131 with no `status` field, 96 `closed`, 66 `open`, 6 `unknown`, and the harness ignores all of it.

## Counts — derived, exact, at the sha above

The graph holds **299 tickets** and **138 edges** (`depends_on` links), **max depth 10** (deepest: T-087).
Of those: **181 closed**, **118 open** — 116 READY (dispatchable now) and 2 BLOCKED on an open dependency.

| metric | value |
| --- | --- |
| in the graph | 299 |
| closed (mechanical) | 181 |
| open | 118 |
| &nbsp;&nbsp;· READY | 116 |
| &nbsp;&nbsp;· BLOCKED | 2 |
| edges (`depends_on`) | 138 |
| root tickets (no `depends_on`) | 219 |
| max depth | 10 |
| open with NO-GATE placeholder `verify` | 87 |
| open carrying a REJECTED dispatch verdict | 14 |
| concrete paths contended by >1 open ticket (glob-aware) | 47 |

### How much of that closure is actually re-verified — read this before trusting it

- `--closed-from-ledger` closed **184** ticket(s) on an OPERATOR-RECORDED `accepted` verdict in
  `.swarm-loop/dispatch.jsonl`. It re-verifies nothing; the ledger is append-only.
- `--closed-from-merged` was **near-blind on this run**: 32 of 33 merged `task/*` branches carry no
  `T-nnn` id at the front of the branch name, so they were SKIPPED and closed nothing. Lanes here are
  named by AREA (`task/lane-trust`, `task/buyer-identity`, …), which degrades that source toward zero
  WITHOUT failing. Exactly 1 merged branch (`task/T-158`) closed anything through it.
  Nothing was reported as explicitly UNDETERMINED — the unclassifiable count is those 32 skipped lanes.
- `--closed-from-merged` measures REACHABILITY, not presence: a merge later `git revert`-ed still reads
  CLOSED. Only a REJECTED ledger verdict reopens a ticket.
- 3 closed id(s) named by the ledger are NOT in the graph and count toward nothing above: `demo-runbook-executability`, `repro-buyer-merchant`, `repro-core-packages`.

### Structural warning the graph itself raises

`tickets.json` has **219 root tickets** (no `depends_on`) out of 299. The scheduler, this ledger and every
"wave 1" in the docs assume ONE scaffold ticket that everything descends from; this graph is instead
219 disjoint trees. Only 80 ticket(s) carry any dependency at all. Not fixed here — reported.

## OPEN — BLOCKED (2)

Not dispatchable: every one waits on an OPEN dependency.

| ticket | sev | depth | waiting on | scope / owns | description |
| --- | --- | --- | --- | --- | --- |
| `T-084` | — | 9 | `T-083` | `e2e/**` | The dishonest store ends below threshold and off the shortlist |
| `T-087` | — | 10 | `T-084` | `docs/demo/shopify-onboarding-extension.md` | The Shopify and onboarding extension runbook covers the beats off the starting path |

## OPEN — READY (116)

Ordered as `frontier` emits them: by what each unblocks, then depth, then id. That is STRUCTURAL —
it is not a priority ruling, and it reads no analysis. A ready set is NOT a wave: run `check-wave <ids>`
before dispatching and `red-check` before trusting any `verify`.

Flags: `NO-GATE` = placeholder `verify` (`false # NO GATE YET…`) — cannot be red-checked, needs a real
failing gate written first. `REJECTED` = a prior dispatch verdict rejected it; it is open again.
`SERIAL` = `parallel_safe: false`, do not co-schedule with anything touching its scope.

| ticket | sev | depth | unblocks | flags | depends_on | scope / owns | description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `T-051` | — | 3 | 7 | SERIAL | `T-050` | `pixel/**`, `apps/merchant/svc/src/collector/**`, `apps/merchant/svc/tests/**` | Pixel reports checkout outcomes the collector can join |
| `T-083` | — | 8 | 2 | SERIAL | `T-034`, `T-042`, `T-081`, `T-061` | `e2e/**` | Both learning loops demonstrably move under seeded outcomes |
| `T-140` | HIGH | 0 | 0 | REJECTED | — | `apps/buyer/svc/src/auth/magic_link.py` | AccountDirectory has exactly one implementation and no production populator. build_auth_service() never passes accounts=, so production runs the … |
| `T-141` | HIGH | 0 | 0 | NO-GATE | — | `apps/buyer/svc/src/auth/magic_link.py` | The magic-link token is delivered to the default no-op `_drop` (line 166) in every deployment, and set_auth_service (routes.py:146) — the documented … |
| `T-142` | HIGH | 0 | 0 | REJECTED | — | `apps/buyer/svc/src/profile/__init__.py` | publish_profile — the only writer of app.buyer_accounts, described as 'the store-visible working set' — has zero production call sites, and no code … |
| `T-148` | HIGH | 0 | 0 | NO-GATE REJECTED | — | `apps/exchange/src/auction/routes.py` | `configure_auctions` — the only way to give the exchange a real solicitor, a real eligibility source, a Redis-backed store or a real ledger sink — … |
| `T-150` | HIGH | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/ledger.py` | The ledger writer has zero producers. Nothing in the repository writes an event into it - not by import, not over HTTP. A repo-wide grep for … |
| `T-159` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/exchange/tests/test_checkout_provider.py:406 (and repo-wide)` | SYSTEMIC GATE-CREDIBILITY DEFECT: tests that swallow a missing subject with `except ImportError` are VACUOUS until the subject exists, and report … |
| `T-161` | MEDIUM | 0 | 0 | — | — | `packages/contracts/src/boundary.py:157 (_source_verdict)` | A provenance block nested inside a hook-provenanced claim's opaque `value` is not walked. Measured … |
| `T-162` | MEDIUM | 0 | 0 | — | — | `packages/contracts/src/boundary.py (validate_bid) — external path` | The boundary checks provenance SOURCE but never discount AUTHORISATION. make_bid(claims=[make_claim("policy", {"authorized_discount_pct": 25.0} … |
| `T-163` | HIGH | 0 | 0 | — | — | `apps/buyer/svc/src/auth/sessions.py` | T-133 item (c): SessionStore.open() validates only that the subject carries the 'psn-' PREFIX — it never checks vault membership. Any psn--prefixed … |
| `T-165` | MEDIUM | 0 | 0 | — | — | `apps/buyer/svc/src/auth/routes.py (POST /buyer/auth/magic-link) + routes.py:57 ProcessLocalStateUnsafe` | No rate limiter on the unauthenticated magic-link endpoint, and the session/pending-link store behind it is process-local (ProcessLocalStateUnsafe) … |
| `T-171` | HIGH | 0 | 0 | NO-GATE | — | `proxyshop_support/neo4j_lock.py:79,119-131 + pyproject.toml:71 + conftest.py` | The D37 Neo4j lock is MACHINE-GLOBAL (/private/tmp/proxyshop-neo4j.lock) and PROXYSHOP_WORKER does not isolate it, so it is shared across every … |
| `T-172` | HIGH | 0 | 0 | REJECTED | — | `proxyshop_support/service_markers.py:78-85 (whole-stack fallback)` | T-109's per-service inference has an 8-item hole and every item in it is a Postgres-only test that a Redis-only outage still silently skips at exit 0 … |
| `T-175` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/store-agent/src/hooks/provenance.py:830-834 vs DESIGN.md` | Neither the new price-reconciliation wall nor the pre-existing floor wall looks at Offer.total_price (PRICE_FIELD == 'unit_price'), so a bid can … |
| `T-181` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/trust/tests/test_events_hardening.py (DSN precedence coverage)` | T-151's DSN ORDER RESTS ON A SINGLE ASSERTION IN A SINGLE TEST OF 327. Sabotage S3a (reorder the tuple, i.e. re-ship the exact defect) and S3b (leave … |
| `T-192` | MEDIUM | 0 | 0 | NO-GATE | — | `.swarm-loop/acceptance/test_e6_trust.py (the E6 metric as an instrument)` | THE 26/26 THAT MOVED E6 TO TARGET IS A WEAKER INSTRUMENT THAN THE LANE'S OWN UNIT SUITE. 7 of 19 real mutations to the E6 core leave the frozen count … |
| `T-194` | MEDIUM | 0 | 0 | — | — | `packages/contracts/src/ts/schemas.ts:63-65 (ajv + ajv-formats) vs packages/contracts/src/boundary.py:353-365 (pydantic)` | THE TWO DOORS DO NOT RUN THE SAME SCHEMA CHECK — 21 measured ok-divergences. TypeScript validates through ajv WITH ajv-formats, so `format … |
| `T-204` | MEDIUM | 0 | 0 | REJECTED | — | `packages/contracts/openapi/exchange.openapi.json:169 (denial_reason) + apps/exchange/src/accept/offer.py` | REFUSAL REASONS ARE UNENUMERATED AND NOTHING ASSERTS ON THEM. accept() formats type(exc).__name__ into denial_reason, which is PERSISTED into a … |
| `T-205` | MEDIUM | 0 | 0 | — | — | `services/shopify-stub/tests/test_stub_contract.py:40 via conftest.py` | SECOND LATENT except-ImportError VACUITY, found by the AST sweep T-159 asked for. The whole module skips if shopify_stub.app:app fails to import. It … |
| `T-208` | MEDIUM | 0 | 0 | NO-GATE | — | `fixtures/manifest.json observation_weights — no weight for an uncorroborated complaint` | There is no published weight for a buyer complaint that is NOT corroborated by a return. It currently lands at mismatch_return's 1.5 — the same as a … |
| `T-209` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/store-agent/src/hooks/provenance.py:129,:496-507 (commitments walk, no pydantic gate)` | THE CONTRACTS BOUNDARY WAS TIGHTENED AND THE STORE-AGENT'S OWN AUDITOR WAS NOT. T-195's fix regenerates the model with --strict-nullable so … |
| `T-210` | HIGH | 0 | 0 | REJECTED | — | `ORCHESTRATION — measuring while the swarm runs (supersedes my T-174 rationale)` | TWO CORRECTIONS, ONE OF THEM TO MY OWN ASSERTION. (1) I claimed the chflags window could corrupt a metric read. THAT IS WRONG, measured: a pytest run … |
| `T-218` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/store-agent/src/hooks/provenance.py:523 (if node is None: return) — T-209 is one of a family of at least six` | T-209 CONFIRMED BUT UNDERSTATED, and the cause is generic rather than specific to commitments. The store-agent's walk short-circuits on ANY None … |
| `T-219` | MEDIUM | 0 | 0 | NO-GATE | — | `scripts/verify.sh SELECTION line + .swarm-loop/goals.json build_succeeds — T-117 acceptance 2 is only literally satisfied` | NOTHING CONSUMES THE SELECTION COUNT, so T-117's defect is closed for the `docker` marker but not as a class. The line is advisory text on stdout … |
| `T-220` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/store-agent/tests/test_price_reconciliation.py (depth parametrization) — cycle detection has an invisible ceiling` | The cycle-detection replacement for MAX_SWEEP_DEPTH is correct, but nothing in the suite would notice a depth bound being silently RE-INTRODUCED … |
| `T-225` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/seller-reference/tests/test_scaffold_smoke.py` | A TEST THAT ASSERTS NOTHING AND REPORTS GREEN, in three copies. `test_scope_directories_exist` reads `for relative in []:` — it iterates an empty … |
| `T-226` | MEDIUM | 0 | 0 | NO-GATE | — | `pyproject.toml` | EVERY MEMBER'S tests/ DIRECTORY IS OUTSIDE THE TYPE GATE, so type errors in tests never reach `make types`. Root pyproject.toml:116 points mypy at … |
| `T-227` | MEDIUM | 0 | 0 | NO-GATE | — | `.swarm-loop/acceptance/test_e4_store_agent.py::_load_fixture_manifest vs apps/seller-reference/src/personas/__init__.py` | THE FROZEN TEST AND THE PRODUCT CAN SELECT DIFFERENT MANIFEST DOCUMENTS, so the suite would grade one document against a pitch built from another. … |
| `T-240` | MEDIUM | 0 | 0 | — | — | `packages/contracts/openapi/merchant.openapi.json` | THE PINNED MERCHANT CONTRACT HAS NOWHERE TO PUT AN ENVELOPE APPROVAL ARTIFACT, AND ITS OWN EXAMPLE INVITES SELF-ACTIVATION. The document pins exactly … |
| `T-242` | HIGH | 0 | 0 | REJECTED | — | `services/sim/src/runner.py` | THE SIMULATION VALIDATES ONLY HALF ITS OWN LEDGER, AND THE HALF IT SKIPS CONTAINS THE EXACT DEFECT CLASS THE SIM EXISTS TO CATCH. runner.py:727 … |
| `T-244` | MEDIUM | 0 | 0 | NO-GATE REJECTED | — | `packages/store-agent/src/external/door.py` | MEASUREMENT CREDIBILITY: receive_bid has 28 callers and ZERO of them are production. swarmloop reachable returns TESTS ONLY — 'every caller is a … |
| `T-251` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/contracts/tests/test_claim_identity.py` | The test's comment asserts the repo root 'is not handed over ... on purpose', but the venv's _proxyshop.pth hands it over anyway at interpreter … |
| `T-252` | MEDIUM | 0 | 0 | NO-GATE | — | `proxyshop_support/tests/test_fixture_loader.py` | Imports from a scratch tree whose module name happens not to exist in the live checkout. Safe today purely by name luck: a rename collision would … |
| `T-253` | MEDIUM | 0 | 0 | NO-GATE | — | `services/shopify-stub/src/codes.py` | utc_now() has zero callers repo-wide, and its docstring asserts an invariant that is false: it claims to exist so time can be frozen 'without every … |
| `T-254` | MEDIUM | 0 | 0 | NO-GATE | — | `services/ingest/src/extraction/claims.py` | ExtractedClaim.as_claim() has zero callers and zero tests. This is the DESIGN Claim projection ({key, value, provenance}) named in T-021's objective … |
| `T-255` | MEDIUM | 0 | 0 | NO-GATE | — | `services/shopify-stub/src/codes.py` | DiscountCode.is_redeemable_at() has zero callers and zero tests. The live redemption path uses rejection() at codes.py:213. A future caller would … |
| `T-258` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/trust/tests/test_ledger_chain.py` | T-102's gate is FULLY VACUOUS on any machine without docker. Every behavioural and catalog assertion for T-102 carries @pytest.mark.docker, and the … |
| `T-260` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/exchange/src/retrieval/service.py` | The exchange's retrieval package has ZERO production callers. CandidateRetrieval at service.py:106 and record_fit_scores at fit.py:269 are referenced … |
| `T-261` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/trust/src/snapshot/builder.py` | T-064's acceptance criterion 3 -- the exchange client caches the trust snapshot and refreshes it on a version bump -- DOES NOT EXIST. There is no … |
| `T-262` | HIGH | 0 | 0 | REJECTED | — | `tickets.json:T-087` | T-087's GATE NAMES A FILE ITS OWN SCOPE FORBIDS IT TO CREATE, and that file sits inside a DIFFERENT ticket's scope. T-087.verify is 'pytest … |
| `T-263` | MEDIUM | 0 | 0 | NO-GATE | — | `tickets.json:T-082` | A ticket whose DELIVERABLE IS ITS OWN GATE TARGET can never be red-checked at its merge base, so the retro red-check that guards every merge stamps … |
| `T-265` | HIGH | 0 | 0 | NO-GATE | — | `services/sim/tests/test_simulation.py` | A TEST WHITELISTS THE DEFECT IT IS SUPPOSED TO GUARD, BY NAME, AND STAYS GREEN BOTH BEFORE AND AFTER THE REPAIR. The assertion is 'deviating <= … |
| `T-266` | MEDIUM | 0 | 0 | — | — | `apps/exchange/src/main.py` | THE SERVED EXCHANGE IS MISSING FOUR PUBLISHED PATHS, NOT ONE. create_app().openapi() answers only ['/auctions', '/auctions/{auction_id}'] while … |
| `T-267` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/contracts/openapi/exchange.openapi.json` | The published OpenAPI example for denial_reason is 'blacklist', a value the code has never produced -- the gate emits 'blacklisted … |
| `T-268` | LOW | 0 | 0 | NO-GATE | — | `apps/exchange/src/checkout/codes.py` | Dead line: 'return parsed.timestamp()' is unreachable, sitting after a try block that either returns or raises on every path. |
| `T-269` | LOW | 0 | 0 | NO-GATE | — | `apps/exchange/src/checkout/codes.py` | expiry_epoch accepts three shapes that contracts.parse_timestamp refuses, so the two parsers disagree: True -> 1.0, '2026' -> 2026.0, '20260903.0' -> … |
| `T-270` | HIGH | 0 | 0 | NO-GATE REJECTED | — | `apps/exchange/src/auction/routes.py` | A caller-supplied NaN still produces an UNAUTHENTICATED HTTP 500. list_price: NaN and tier: 1e400 both return 500. The 500 does not come from the … |
| `T-271` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/collect.py` | THE PRICE FLOOR'S MAGNITUDE IS ESSENTIALLY UNPINNED -- mutation-tested, not inferred. Setting MINIMUM_PAYABLE_AMOUNT to 0.0 (deleting the absolute … |
| `T-272` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/collect.py` | _tier SILENTLY DOWNGRADES WELL-FORMED INPUT, and the line it replaced did not. _number excludes bool and str by design, so tier '2' -> 0, tier '1' -> … |
| `T-273` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/collect.py` | _price_is_unreadable TURNS A GOOD BID INTO A 0.00 FALLBACK on a roster row with no list_price. _number(offer.get('total_price')) is None when the key … |
| `T-274` | MEDIUM | 0 | 0 | NO-GATE | — | `tickets.json:T-212` | AMENDMENT 15 WIRED A RED GATE ONTO A CLOSED, REFUTED TICKET. T-212 in tickets.json carries status 'closed' with status_evidence beginning 'REFUTED … |
| `T-275` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/trust/tests/test_repro_open_tickets.py` | T-237'S GATE CANNOT SEE ITS OWN FIX. Its _KIND_EMISSION pattern matches only QUOTED STRING LITERALS, but the repo emits ledger kinds two ways … |
| `T-276` | LOW | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/collect.py` | AN UNTESTED CHEAP-PRODUCT BAND, where the floor refuses everything. _price_floor(0.01) == 0.01, so a product listed at one cent CANNOT BE DISCOUNTED … |
| `T-277` | LOW | 0 | 0 | NO-GATE | — | `apps/exchange/tests/test_repro_untrusted_roster.py` | A CONDITIONAL ASSERTION THAT NOW NEVER EXERCISES ITS OWN CASE. test_t224_a_roster_row_that_prices_nothing_cannot_mint_a_free_offer keeps an … |
| `T-278` | HIGH | 0 | 0 | NO-GATE | — | `packages/contracts/tests/price_parity_corpus.json` | T-250'S TYPESCRIPT HALF SHIPS COMPLETELY UNTESTED. The shared parity corpus that exists specifically to catch the Python and TypeScript doors … |
| `T-280` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/store-agent/src/external/door.py` | A REFUSAL RECEIPT WITH NO IDENTITY. door.py:498 returns _refuse(REASON_MALFORMED_SUBMISSION) with no `payload=`, so the receipt for a mapping whose … |
| `T-282` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/__init__.py` | MalformedLedgerPayload IS NOT RE-EXPORTED, so `from exchange.auction import MalformedLedgerPayload` raises ImportError while every sibling in the … |
| `T-283` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/ledger.py` | AN EXCEPTION ON A PATH WHOSE STATED CONTRACT IS 'NEVER FAIL THE AUCTION'. build_published_event (apps/exchange/src/auction/ledger.py:118) raises … |
| `T-284` | LOW | 0 | 0 | NO-GATE | — | `apps/buyer/svc/src/feedback/submission.py` | FOUR IN-REPO SITES STILL DESCRIBE T-235 AS A LIVE DEFECT. T-235 was closed by the T-158 lane and merged to main at 48d0510 -- build_published_event … |
| `T-285` | LOW | 0 | 0 | NO-GATE | — | `apps/merchant/svc/tests/test_repro_open_tickets.py` | AN UNWIRED TEST HELPER: THE SyntaxWarning IT WAS WRITTEN TO MUTE STILL LEAKS. _parse(path) at … |
| `T-286` | MEDIUM | 0 | 0 | NO-GATE | — | `e2e/test_s1_flow.py` | THE S1 E2E FLOW HAS TWO BLIND SPOTS, THOUGH IT IS OTHERWISE GENUINE. FILED AGAINST HELD WORK: e2e/test_s1_flow.py exists only on branch task/T-082 … |
| `T-289` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/trust/tests/test_repro_open_tickets.py` | T-181'S TEST IS GREEN UNDER THE EXACT MUTATION IT EXISTS TO CATCH, because it derives its expectation from the tuple it purports to defend. Line 215 … |
| `T-290` | LOW | 0 | 0 | NO-GATE | — | `apps/trust/tests/test_repro_open_tickets.py` | T-167's gate computes `bodies = {path.read_bytes() for path in copies}` at line 179 and NEVER ASSERTS ON IT -- the variable appears only in the … |
| `T-291` | LOW | 0 | 0 | NO-GATE | — | `apps/trust/tests/test_repro_open_tickets.py` | T-212's gate hardcodes its fixture roster: _DATASTORE_FIXTURES at lines 446-458 is a literal frozenset of nine fixture names, so a datastore fixture … |
| `T-292` | LOW | 0 | 0 | NO-GATE | — | `packages/contracts/tests/test_claim_identity.py` | A COMMENT ASSERTS HERMETICITY THAT IS MEASURABLY FALSE, and a load-bearing line is documented as unnecessary. The comment states 'the environment is … |
| `T-294` | MEDIUM | 0 | 0 | NO-GATE REJECTED | — | `apps/exchange/src/auction/routes.py` | THE ACCEPT PATH T-170 JUST SHIPPED CANNOT ACCEPT ANYTHING, BECAUSE NOTHING IN THE REPOSITORY PERSISTS AN AUCTION'S BIDS. `AuctionRecord` … |
| `T-295` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/exchange/src/orchestration/solicitation.py` | THREE MORE `!r` LEAKS OF T-264'S SHAPE, RANKED BY MEASURED EXPOSURE, ALL OUTSIDE THE FIXING LANE'S SCOPE. (1) CLIENT-VISIBLE -- solicitation.py:169 … |
| `T-296` | MEDIUM | 0 | 0 | — | — | `packages/contracts/openapi/trust.openapi.json` | EIGHT PUBLISHED OPENAPI OPERATIONS THAT NO APP SERVES, AND FOUR OF THEM ARE OWNED BY NOBODY. Measured by BOOTING each app and reading … |
| `T-297` | LOW | 0 | 0 | NO-GATE | — | `packages/contracts/openapi/exchange.openapi.json` | T-204 IS HALF-CLOSED ON A HALF-RED GATE, AND T-267 CARRIES A PREMISE THAT IS MEASURABLY FALSE. Two separate hazards on adjacent lines of one file. … |
| `T-302` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/trust/src/reconcile/engine.py` | THREE FROZEN LEDGER KINDS ARE RESERVED IN ALL FOUR VOCABULARIES AND EMITTED BY NO PRODUCT CODE -- three siblings of the T-237 shape found by sweeping … |
| `T-303` | MEDIUM | 0 | 0 | REJECTED | — | `apps/trust/src/snapshot/delisting.py` | T-237 IS HALF-CLOSED AND THE REMAINING HALVES ARE IN OTHER LANES' SCOPES. The new delisting module correctly turns a sub-threshold score into … |
| `T-308` | HIGH | 0 | 0 | REJECTED | — | `proxyshop_support/` | THE SYSTEM HAS NO OBSERVABILITY AT ALL, AND EVERY INFO-LEVEL CALL IS SILENTLY DISCARDED. Repo-wide grep for … |
| `T-311` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/buyer/package.json` | NEITHER UI IS RUNNABLE -- there is no host, no dev server, no build, and no entry point. apps/buyer/package.json and apps/merchant/package.json have … |
| `T-312` | MEDIUM | 0 | 0 | REJECTED | — | `packages/contracts/openapi/trust.openapi.json` | SEVEN PUBLISHED ROUTES ARE UNSERVED ACROSS THREE SERVICES, measured by constructing each app and reading app.openapi()['paths']. TRUST publishes GET … |
| `T-315` | LOW | 0 | 0 | NO-GATE | — | `docs/demo/` | DESIGN.md:145 states docs/demo/ should hold TWO runbooks and that 'the S6 lint reads their union', but the directory contains only .gitkeep and … |
| `T-316` | HIGH | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/routes.py` | T-310's fix is BLOCKED ON T-299 and wiring it today turns a booting service into a crash-looping one. ranking/filters.py:39 imports … |
| `T-317` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/merchant/svc/src/onboarding/routes.py` | THE MERCHANT SERVES THREE ROUTES THAT APPEAR IN NO PUBLISHED CONTRACT: GET /install, GET /install/callback, GET /install/shops. Measured by … |
| `T-318` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/contracts/openapi/trust.openapi.json` | T-312 UNDERCOUNTS ITS OWN FINDING BY A FACTOR OF MORE THAN THREE. T-312 records the contract/served divergence as affecting three services in one … |
| `T-319` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/trust/tests/test_repro_open_tickets.py` | T-288'S STRENGTHENED AST GATE IS STILL EVADABLE BY TWO SHAPES THAT PRESERVE THE DEAD 503 ARM. _names_holding_a_status_code propagates taint only … |
| `T-320` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/trust/src/snapshot/delisting.py` | _lapsed_reasons at delisting.py:112 iterates the registry, so a LOOKUP-ONLY registry implementation emits zero blacklist_expired events and no … |
| `T-321` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/store-agent/tests/test_repro_open_tickets.py` | SIX CONTRACT/SERVED SWEEP HELPERS NOW EXIST IN THREE COPIES, one per package, with nothing enforcing that they agree. They were unified once already … |
| `T-322` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/contracts/tests/signing.test.ts` | A LATENT GATE FLAKE THAT INTERMITTENTLY REDS SEVEN TICKETS' GATES, AND IT IS NOT THE DATABASE. packages/contracts/tests/signing.test.ts:1079 — the … |
| `T-323` | HIGH | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/routes.py` | T-310 IS NOT 'ADD A ROUTE' — THE COPY THAT FIXES T-299 MAKES THE RANKER IMPORTABLE, NOT FUNCTIONAL, AND NOTHING HAS COSTED THE DIFFERENCE. Measured … |
| `T-324` | MEDIUM | 0 | 0 | NO-GATE | — | `DESIGN.md` | DESIGN.md:45'S REPO LAYOUT NAMES TWO PATHS THAT DO NOT EXIST — a top-level `graph/` and a `packages/ranking` — and it is the ONLY passage in the … |
| `T-325` | HIGH | 0 | 0 | — | — | `apps/exchange/tests/test_repro_open_tickets.py` | THREE TICKETS NOW SHARE TWO GATE NODES, AND TWO OF THEM WILL GO GREEN ON A FIX THAT DOES NOT ADDRESS THEM. Amendment 19 repointed T-266 and T-296 at … |
| `T-328` | HIGH | 0 | 0 | NO-GATE | — | `apps/buyer/svc/tests/test_repro_open_tickets.py` | T-198 IS THE FIRST CONFIRMED UNDER-SELECTION WITH A LIVE FAILURE, and it is an OPEN ticket. Its verify selects `test_t198_a_...`. The same file holds … |
| `T-329` | MEDIUM | 0 | 0 | NO-GATE | — | `pyproject.toml` | ONLY 16 OF 141 TICKET GATES REACH THE FROZEN ACCEPTANCE SUITE, AND 44 OF 141 GATED TICKETS HAVE NO TRACEABLE GRADER ANYWHERE. All 120 … |
| `T-330` | HIGH | 0 | 0 | NO-GATE | — | `docs/tests/test_runbook_executability.py` | THE RUNBOOK GATE'S POST-MERGE REWRITE INTRODUCED THREE FAIL-OPENS, ALL INVISIBLE TO ITS EXIT CODE. The hardened version merged at e57268e (+492/-201) … |
| `T-332` | HIGH | 0 | 0 | NO-GATE | — | `apps/trust/src/snapshot/delisting.py` | A DELISTING EVENT NAMING NO STORE PASSES EVERY LAYER, AND IS THEN INVISIBLE TO THE AUDITOR THE MODULE EXISTS TO SERVE. delisting.py:180 guards … |
| `T-333` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/trust/src/snapshot/delisting.py` | delisting.py IS THE ONLY LEDGER-EVENT PRODUCER THAT NEVER CALLS validate_ledger_payload. Every other producing boundary does … |
| `T-334` | MEDIUM | 0 | 0 | NO-GATE | — | `proxyshop_support/tests/test_artifact_copyset.py` | T-301'S BLIND SPOT IS CIRCULAR: REMOVING A COPY REMOVES THE EVIDENCE THAT IT IS MISSING. _unwired_for reports a package only if some SHIPPED module … |
| `T-335` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/routes.py` | T-237'S STATED END-STATE IS NOT REACHED, AND ONE OF ITS GATES WAS ALREADY GREEN BEFORE THE WORK. snapshot['delistings'] has ZERO production … |
| `T-336` | HIGH | 0 | 0 | NO-GATE | — | `packages/contracts/src/ts/boundary.ts` | THE TYPESCRIPT DOOR CARRIES THE IDENTICAL list_prices ASYMMETRY, SO T-306/T-307 ARE A TWO-LANGUAGE FIX. boundary.ts:545 and :580 abstain on an absent … |
| `T-337` | HIGH | 0 | 0 | NO-GATE | — | `packages/contracts/src/boundary.py` | THE OBVIOUS TWO-SITE FIX FOR T-306/T-307 IS INSUFFICIENT, AND THE SCOPE IS 99 TESTS ACROSS TWO LANGUAGES. Measured: normalising only boundary.py:637 … |
| `T-338` | HIGH | 0 | 0 | NO-GATE | — | `docs/tests/test_runbook_executability.py` | A TEXTUALLY-CLEAN AUTO-MERGE PRODUCED A FILE THAT DOES NOT IMPORT, AND EVERY MERGE GATE PASSED IT. Two lanes changed this file for compatible … |
| `T-339` | MEDIUM | 0 | 0 | NO-GATE | — | `docs/tests/test_runbook_executability.py` | THE REPRODUCTION-GATE IDIOM HAS A STRUCTURAL BLIND SPOT THAT ONLY DISAPPEARS WHEN THE DEFECT IS FIXED, AND IT SHOULD BE STATED WHEREVER THE IDIOM IS … |
| `T-340` | HIGH | 0 | 0 | NO-GATE | — | `apps/exchange/src/ranking/filters.py` | A BIDDER SATISFIES ANY HARD CONSTRAINT BY WRITING status=verified ON ITS OWN CLAIM, AND T-310'S WIRING JUST MADE IT REACHABLE BY AN HTTP REQUEST. … |
| `T-341` | HIGH | 0 | 0 | NO-GATE | — | `apps/exchange/src/ranking/serving.py` | THE FIVE-TERM RANK FORMULA IS INERT ON EVERY SERVED REQUEST, so the shortlist the demo shows is trust-only. No producer exists for intent_match … |
| `T-342` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/contracts/openapi/exchange.openapi.json` | POST /auctions' 201 BODY HAS NEVER MATCHED ITS PUBLISHED CONTRACT, and no test in the repo checks a 201 body against a contract at all. The contract … |
| `T-343` | MEDIUM | 0 | 0 | NO-GATE | — | `docs/demo/starting-slice.md` | THE RUNBOOK PROMISES A LIST-PRICE FALLBACK IN THE SHORTLIST AND THE C10 FILTER MAKES IT IMPOSSIBLE. starting-slice.md:145 says the shortlist shows a … |
| `T-344` | MEDIUM | 0 | 0 | NO-GATE | — | `packages/contracts/openapi/protocol.schema.json` | Offer.expires_at IS TYPED AS AN ISO DATE-TIME STRING AND EVERY CONSUMER IN THE REPO TREATS IT AS A FLOAT EPOCH. protocol.schema.json declares the … |
| `T-345` | HIGH | 0 | 0 | NO-GATE | — | `packages/contracts/src/boundary.py` | THE MONEY PATH ACCEPTS MALFORMED INPUT INSTEAD OF REFUSING IT, WHICH IS WORSE IN KIND THAN THE LEAK CLASS IT WAS FOUND BESIDE. Measured by the … |
| `T-346` | MEDIUM | 0 | 0 | NO-GATE | — | `apps/exchange/src/checkout/codes.py` | FOUR MORE REPR-LEAK SITES THAT NO TICKET NAMES, ALL ON CLIENT-VISIBLE PATHS. checkout/codes.py:187 renders {expires_at!r} and :224/:226 render … |
| `T-347` | HIGH | 0 | 0 | NO-GATE | — | `apps/exchange/tests/ (RedisAuctionStore.reserve has one detector, and it is @pytest.mark.docker)` | THE DEPLOYED ATOMIC PRIMITIVE'S ONLY REGRESSION BARRIER IS CONDITIONAL ON DOCKER BEING UP. RedisAuctionStore.reserve is the store the deployment … |
| `T-348` | HIGH | 0 | 0 | NO-GATE | — | `apps/exchange/src/accept/ (fallback acceptance) — ESC-025, ruled by Hank 2026-09-05` | A RULING THE USER GAVE IS UNIMPLEMENTED, AND AN INTERIM PLACEHOLDER SHIPS IN ITS PLACE. Hank ruled, verbatim: 'It seems to me like the fallback … |
| `T-349` | HIGH | 0 | 0 | NO-GATE | — | `apps/exchange/src/auction/routes.py` | EVERY RECORDED BID IS WRITTEN WITH offer: {} — the code documents its own defect and no ticket owns it. routes.py:878 calls … |
| `T-350` | HIGH | 0 | 0 | NO-GATE | — | `db/migrations/0003_sealed_vault_app_tables.sql` | THE ENVELOPE TABLE CAN PERSIST AN ACTIVATION THE DOMAIN RULE FORBIDS, so activation cannot cross the persistence boundary. sealed.envelopes declares … |
| `T-351` | MEDIUM | 0 | 0 | NO-GATE | — | `docs/` | THE K-ANONYMITY PRODUCTION KNOB IS UNDOCUMENTED, which is the one half of SPEC.md:56 that is genuinely unmet. That line reads: 'No k-anonymity … |
| `T-131` | — | 3 | 0 | — | `T-080` | `fixtures/**` | The golden answer key is graded weakly, and the dishonest store's flagship lie is not graded at all |
| `T-132` | — | 3 | 0 | SERIAL | `T-070` | `apps/buyer/**` | Rotating pseudonyms are trivially re-linkable, so T-070's central guarantee does not hold |
| `T-133` | — | 3 | 0 | — | `T-070` | `apps/buyer/**`, `packages/**` | Buyer residue: shared session state, unwired publish half, and unbounded stores |
| `T-134` | — | 3 | 0 | — | `T-050` | `apps/merchant/**` | Merchant residue: a scope guard that shreds strings, a token in repr, and six inbox findings that never reached the lane |
| `T-024` | — | 4 | 0 | SERIAL | `T-021`, `T-023` | `services/ingest/src/scheduler/**`, `services/ingest/tests/**` | Differential refresh keeps the graph current at field-appropriate cadence |
| `T-086` | — | 5 | 0 | SERIAL | `T-043`, `T-053` | `e2e/test_onboarding.py`, `e2e/support/onboarding/**` | Onboarding drives a shadow store to its first real bid |
| `T-054` | — | 7 | 0 | SERIAL | `T-035`, `T-043`, `T-052`, `T-053`, `T-063` | `apps/merchant/app/dashboard/**` | The dashboard shows the walls and the window |

## Scheduling constraints — blast-radius overlaps among OPEN tickets

Ownership is PER FILE, not per glob: two open tickets whose `scope` resolves to the same concrete path
must not run in the same wave, whichever of them spelled it as a glob. Resolved glob-aware over the
118 open tickets, 47 concrete path(s) are contended by more than one of them. Each group below is a
"pick ONE per wave" set — `check-wave <ids>` is still the gate, this is the map it should agree with.

### Directly contended paths — two or more open tickets name the SAME path

| contended path | open tickets naming it | also swallowed by a glob owner |
| --- | --- | --- |
| `apps/exchange/src/auction/routes.py` | `T-148` `T-270` `T-294` `T-316` `T-323` `T-335` `T-349` | — |
| `apps/trust/tests/test_repro_open_tickets.py` | `T-275` `T-289` `T-290` `T-291` `T-319` | — |
| `apps/exchange/src/auction/collect.py` | `T-271` `T-272` `T-273` `T-276` | — |
| `apps/trust/src/snapshot/delisting.py` | `T-303` `T-320` `T-332` `T-333` | — |
| `apps/exchange/src/checkout/codes.py` | `T-268` `T-269` `T-346` | — |
| `docs/tests/test_runbook_executability.py` | `T-330` `T-338` `T-339` | — |
| `packages/contracts/openapi/exchange.openapi.json` | `T-267` `T-297` `T-342` | `T-133` |
| `packages/contracts/openapi/trust.openapi.json` | `T-296` `T-312` `T-318` | `T-133` |
| `apps/buyer/**` | `T-132` `T-133` | — |
| `apps/buyer/svc/src/auth/magic_link.py` | `T-140` `T-141` | `T-132` `T-133` |
| `apps/exchange/src/auction/ledger.py` | `T-150` `T-283` | — |
| `e2e/**` | `T-083` `T-084` | `T-086` |
| `packages/contracts/src/boundary.py` | `T-337` `T-345` | `T-133` |
| `packages/contracts/tests/test_claim_identity.py` | `T-251` `T-292` | `T-133` |
| `packages/store-agent/src/external/door.py` | `T-244` `T-280` | `T-133` |
| `pyproject.toml` | `T-226` `T-329` | — |
| `services/shopify-stub/src/codes.py` | `T-253` `T-255` | — |

### Glob owners — a wide `scope` collides with lanes it never names

10 open ticket(s) declare a `scope` glob. The count is how many OTHER open tickets that glob
actually resolves onto; those are the ones it cannot share a wave with.

- **`T-133`** owns `apps/buyer/**`, `packages/**` — collides with 35 open ticket(s): `T-132` `T-140` `T-141` `T-142` `T-161` `T-162` `T-163` `T-165` `T-175` `T-194` `T-204` `T-209` `T-218` `T-220` `T-240` `T-244` `T-251` `T-267` `T-278` `T-280` `T-284` `T-292` `T-296` `T-297` `T-311` `T-312` `T-318` `T-321` `T-322` `T-328` `T-336` `T-337` `T-342` `T-344` `T-345`
- **`T-132`** owns `apps/buyer/**` — collides with 9 open ticket(s): `T-133` `T-140` `T-141` `T-142` `T-163` `T-165` `T-284` `T-311` `T-328`
- **`T-134`** owns `apps/merchant/**` — collides with 4 open ticket(s): `T-051` `T-054` `T-285` `T-317`
- **`T-084`** owns `e2e/**` — collides with 3 open ticket(s): `T-083` `T-086` `T-286`
- **`T-083`** owns `e2e/**` — collides with 3 open ticket(s): `T-084` `T-086` `T-286`
- **`T-086`** owns `e2e/test_onboarding.py`, `e2e/support/onboarding/**` — collides with 2 open ticket(s): `T-083` `T-084`
- **`T-051`** owns `pixel/**`, `apps/merchant/svc/src/collector/**`, `apps/merchant/svc/tests/**` — collides with 2 open ticket(s): `T-134` `T-285`
- **`T-131`** owns `fixtures/**` — collides with 1 open ticket(s): `T-208`
- **`T-054`** owns `apps/merchant/app/dashboard/**` — collides with 1 open ticket(s): `T-134`
- **`T-024`** owns `services/ingest/src/scheduler/**`, `services/ingest/tests/**` — collides with 0 open ticket(s).

## CLOSED — id roster only (181)

No detail, deliberately. A closed ticket LEAVES this ledger: its record survives in that cycle's
`reports/cycle-*.md` and in git history, and its full definition is still in `tickets.json`. The bare
ids are listed for exactly one reason — `resume`'s graph/ledger reconciliation compares the id SETS,
and a ledger that silently omits 181 ids reports as DRIFT. Do not grow this into an archive.

`T-000` `T-010` `T-011` `T-012` `T-013` `T-014` `T-020` `T-021` `T-022` `T-023` `T-030` `T-031`
`T-032` `T-033` `T-034` `T-035` `T-036` `T-040` `T-041` `T-042` `T-043` `T-044` `T-045` `T-050`
`T-052` `T-053` `T-060` `T-061` `T-062` `T-063` `T-064` `T-065` `T-070` `T-071` `T-072` `T-073`
`T-080` `T-081` `T-082` `T-085` `T-100` `T-101` `T-102` `T-103` `T-104` `T-105` `T-106` `T-107`
`T-108` `T-109` `T-110` `T-111` `T-112` `T-113` `T-114` `T-115` `T-116` `T-117` `T-118` `T-119`
`T-120` `T-121` `T-122` `T-123` `T-124` `T-125` `T-126` `T-127` `T-128` `T-129` `T-130` `T-135`
`T-136` `T-137` `T-138` `T-139` `T-143` `T-144` `T-145` `T-146` `T-147` `T-149` `T-151` `T-152`
`T-153` `T-154` `T-155` `T-156` `T-157` `T-158` `T-160` `T-164` `T-166` `T-167` `T-168` `T-169`
`T-170` `T-173` `T-174` `T-176` `T-177` `T-178` `T-179` `T-180` `T-182` `T-183` `T-184` `T-185`
`T-186` `T-187` `T-188` `T-189` `T-190` `T-191` `T-193` `T-195` `T-196` `T-197` `T-198` `T-199`
`T-200` `T-201` `T-202` `T-203` `T-206` `T-207` `T-211` `T-212` `T-213` `T-214` `T-215` `T-216`
`T-217` `T-221` `T-222` `T-223` `T-224` `T-228` `T-229` `T-230` `T-231` `T-232` `T-233` `T-234`
`T-235` `T-236` `T-237` `T-238` `T-239` `T-241` `T-243` `T-245` `T-246` `T-247` `T-248` `T-249`
`T-250` `T-256` `T-257` `T-259` `T-264` `T-279` `T-281` `T-287` `T-288` `T-293` `T-298` `T-299`
`T-300` `T-301` `T-304` `T-305` `T-306` `T-307` `T-309` `T-310` `T-313` `T-314` `T-326` `T-327`
`T-331`

---

Regenerate: `swarmloop.py frontier --closed-from-ledger --closed-from-merged --json`, then rewrite this
file from `tickets.json`. State the ticket count, the edge count and the max depth IN PROSE — `resume`
greps for the literal phrases `N tickets`, `N edges` and `max depth N` — a markdown table cell such as
`| tickets in graph | 296 |` does NOT match. The previous ledger stated its counts ONLY in table cells,
so the only phrase in 962 lines that matched was an unrelated "3 ticket(s) named by a rejected record",
and `resume` correctly reported that the file never states the graph's ticket count and "says 3".

Second trap, also load-bearing: `resume` reconciles the id SETS with the pattern `\bT-\d+\b`, so NEVER
truncate a title mid-token. The previous ledger cut T-258's title at 100 characters in the middle of
the phrase "…every behavioural and catalog assertion for T-102 carries @pytest.mark.docker". The cut
landed between the last two digits, leaving T-102 shorn of its final digit, and `resume` duly reported
a phantom two-digit id "in the ledger, missing from the graph". No such ticket exists and it was NOT a
typo for T-010 (which is real, and closed) — it was a truncation artefact. This generator cuts on a
word boundary and then drops any trailing partial id, so it cannot mint one.
