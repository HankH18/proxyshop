# ProxyShop — Spec

## Problem & intent
Product name: **ProxyShop** — a buyer-side shopping agent network (the "pitch protocol" is the underlying seller-bid contract, not the product name).

Build the capstone implementation of the buyer-side shopping-agent network: a buyer agent clarifies a shopper's intent, per-store advocate agents place sealed bids for that shopper's business, a neutral exchange ranks the bids, the winning offer is redeemed on the merchant's Shopify checkout via a single-use discount code, and a trust engine scores each store's pitch-to-outcome reliability from observed results. Merchants are simulated by real Shopify development stores. Everything runs against the same interfaces production would use.

## User-visible behavior
Buyer (web chat app):
- R1: WHEN a buyer states a shopping need, THE SYSTEM SHALL ask at most 3 clarifying questions and produce a structured intent (use case, constraints, budget band) visible to the buyer for confirmation.
- R2: WHEN an intent is confirmed, THE SYSTEM SHALL solicit bids from all eligible participating stores and present a shortlist of up to 4 differentiated slots (best fit / best value / most reliable / specialist), each showing product, price, commitments, store trust indicator, and provenance labels ("store-confirmed" vs "from their website").
- R3: WHEN the buyer accepts an offer, THE SYSTEM SHALL create a single-use discount code on that store, validate it (validity window, combinesWith), and redirect to the store's checkout via cart permalink with the code pre-applied.
- R4: WHEN a purchase completes, THE SYSTEM SHALL record the outcome (pixel event and/or order webhook) and reconcile the two, with the webhook authoritative.
- R5: Buyers authenticate with email magic link; stores never receive buyer identity — only a rotating pseudonym and a coarsened profile.

Merchant (network app + dashboard):
- R6: WHEN a merchant installs the network's Shopify app, THE SYSTEM SHALL register the web pixel, subscribe order webhooks, and start a plain-language onboarding interview that produces an economic envelope (floors, max discount, budget cap, intents to pursue, standing commitments) the merchant approves in writing before activation.
- R7: WHEN an envelope exists but is not yet activated, the store's agent SHALL run in shadow mode: it computes bids for live intents and logs them with reasoning, but submits nothing.
- R8: WHEN active, a network-hosted store agent SHALL bid within hard envelope walls, and every claim and discount it emits SHALL come through a provenance-tagged tool hook; a hosted bid violating this is rejected at the boundary. External bids (Tier-2 agents, seller reference personas) MAY carry free text and asserted claims with provenance `seller_asserted`; these are admitted and routed to claim extraction + verification (R18), never silently trusted.
- R9: The dashboard SHALL show every bid with its rationale, win/loss reports aggregated by intent cluster with reason categories only (fit / price / commitments / trust; no rival amounts), trust score with per-dimension breakdown and event payloads, a kill switch, and envelope editing (versioned).

Exchange & trust:
- R10: Bids are solicited synchronously and in parallel with a hard timeout; a non-responding Tier-1 store falls back to a list-price offer; Tier-0 stores are always represented at list price.
- R11: Ranking SHALL be a published deterministic combination of fit score, offer value, and trust; it SHALL be blind to network fees, tier, and envelope contents. First-price sealed auction.
- R12: Trust score per store = one merged observation framework: claim-verification outcomes (verified/contradicted/unsupported/ambiguous, with contradicted weighted heavier than unsupported) AND transaction-observed dimensions {price_honored, discount_honored, shipped_on_time, not_returned, feedback_match}, combined per-dimension with observation-type weights and time decay; verification observations provide trust signal before any transactions exist. New stores start at a neutral low-confidence prior; a published threshold triggers blacklisting bound to business identity, with review/appeal/expiry states and fail-closed reads before solicitation, ranking, and checkout; a guaranteed exploration slice exposes low-data stores.
- R13: WHEN a trust-relevant event lands (e.g. failed commitment, routed-buyer feedback), THE SYSTEM SHALL push the full pseudonymous event payload to the affected store agent.
- R14: Post-purchase feedback: only network-routed buyers, one structured prompt ("did it match the pitch?"), weighted by buyer track record, cross-checked against return behavior.
- R15: The trust ledger is append-only; recomputing all trust scores from the ledger SHALL reproduce the served scores exactly.

- R18: THE SYSTEM SHALL decompose pitch text and asserted claims into atomic typed claims with source spans, and verify each against the current catalog snapshot with deterministic comparators, yielding verified | contradicted | unsupported | ambiguous with evidence refs and confidence; results are idempotent per (pitch, verifier version, catalog snapshot) and unsupported/ambiguous are never treated as true.
- R19: Hard constraints are eligibility filters, never score terms, and require verified supporting facts; ambiguous cannot satisfy a hard constraint; a contradicted hard constraint cannot win regardless of pitch quality.

Learning loops:
- R16: The exchange ranking policy SHALL update from conversion outcomes (contextual bandit over intent-cluster × store) such that simulated outcome shifts measurably reorder future shortlists.
- R17: Each store agent SHALL update its own bid policy from its own outcomes (discount depth × commitment set), initialized from network priors built from pitch/value-prop outcomes only — never from discount data of other stores.

## Constraints
- C1: Monorepo; Python for agents/services, TypeScript for buyer UI, merchant app, pixel. Buyer/merchant/exchange/trust are separate deployables.
- C2: Neo4j (catalog, claims, provenance, entity resolution; native vector index, cosine, 1024 dims). Postgres (event ledger, sealed store-agent state, identity vault, buyer accounts). Redis (session/bid state, TTL).
- C3: The exchange service has no code path or DB grant that can read envelopes. Enforced by schema grants and import lint.
- C4: Anthropic API for LLM calls; model IDs are config, not code. Embeddings via a local open-source model behind an EmbeddingProvider interface.
- C5: Shopify: app created in Dev Dashboard; GraphQL Admin only; web pixel app extension is the only checkout observation path; no protected-customer-data scopes; `read_orders` (60-day window) is sufficient.
- C6: Catalog ingestion behind an adapter interface; adapters: Catalog MCP (production primary), signed page fetcher (products.json + JSON-LD + policy pages, supports storefront password for dev stores). Identified bot, robots.txt honored.
- C7: All services run under docker-compose locally; verification never requires cloud infra.
- C9: Ticket verification runs offline: a local Shopify stub service implements the exact Admin GraphQL mutations, webhook deliveries, permalink redemption, and pixel-event surface the system uses; LLM calls and embeddings use deterministic test doubles in tests. Live dev-store runs are a demo/e2e-live procedure, never a ticket verify.
- C8: No timelines anywhere in these docs.
- C10: Security baseline (adopted from partner architecture): crawler is SSRF-guarded (private/link-local blocked, redirects bounded to allowed hosts) and identified; scraped text and all pitch content are untrusted data, never instructions — LLM extraction is isolated from tool credentials and validated against strict schemas; checkout URLs validate against the registered seller domain; low-confidence extraction is quarantined; cross-domain endpoints ship OpenAPI examples + consumer contract tests.
- C11: The simulated/redirect checkout path and the Shopify path emit the identical LedgerEvent kinds; the event schema is the E5 upgrade seam and is frozen jointly.

## Non-goals
- No real merchants, payments on the network, ACP/UCP checkout integration, AP2/TAP mandates, or payouts/billing (the per-category rate card is displayed, never charged).
- No buyer PII to merchants; no protected-customer-data scopes; no free-text reviews or review moderation.
- No Tier-2 self-host distribution work beyond keeping `packages/store-agent` importable and the bid API accepting externally signed bids.
- No claim *truth* verification (efficacy, lab testing). Trust is reliability calibration only.
- No mobile clients. No multi-category ontology — one seed category per environment, parameterized.
- No k-anonymity enforcement beyond a configurable floor (default 1 in fixtures; production knob documented).

## Success criteria
- S1: End-to-end: intent → ≤3 clarifications → parallel bids → shortlist → accepted offer → code created & validated → permalink checkout completes → webhook + pixel reconciled → ledger event → trust update. Checkable by e2e test against the Shopify stub (C9); the same flow against seeded dev stores (Bogus Gateway) is the documented demo procedure.
- S2: The scripted dishonest store (behaviors defined in the human-approved fixture manifest) falls below the blacklist threshold within the simulation episode budget and disappears from shortlists. Checkable against the manifest, not the trust engine's own config.
- S3: Ledger replay reproduces served trust scores bit-for-bit (R15). Checkable by replay test.
- S4: In simulation, shifting a store's conversion outcomes reorders shortlists for the affected intent cluster (R16), and a store agent's discount depth distribution shifts in the direction of its own win/loss record (R17). Checkable by simulation assertions with fixed seeds.
- S5: A bid containing a claim with no provenance tag is rejected; every claim in every accepted bid resolves to a provenance record (R8). Checkable by protocol boundary tests.
- S6: Merchant onboarding: interview → approved envelope → shadow-mode bid log → activation, demonstrable on a fresh dev store in one sitting (R6–R7). Checkable by integration test with mocked interview transcript + one manual demo run.
- S7: Exchange code cannot read envelopes: grant test + import-lint rule both fail the build if violated (C3).
- S8: A golden pitch containing both true and false claims yields distinct atomic verification outcomes with evidence (R18). Release blockers, each with a failing test until satisfied: no blacklisted seller ever surfaces as eligible, no contradicted hard constraint ever wins, no checkout URL outside the registered seller domain is ever returned.

## Reconciliation amendments (approved)
Partner (Sohail) architecture/implementation docs reconciled; approved decisions: (1) merged trust observation framework, verification-first; (2) dual-path claim entry — harnessed-structured for hosted agents, extract-and-verify for external/personas — replacing blanket rejection; (3) starting-slice checkout is simulated/redirect emitting the frozen LedgerEvent schema (C11), Shopify/E5 unchanged behind the same seam; (4) both seller kinds are first-class: hosted harnessed agents and unharnessed reference personas (the personas exercise the external door adversarially); (5) one published rank formula with hard-constraint filters, weights set jointly.

## Adversarial review (resolved)
- A1: Dev stores are password-protected, so they are absent from Shopify's Global Catalog and their public endpoints sit behind the storefront password. → Catalog MCP adapter is contract-tested against a recorded mock; the signed fetcher accepts a storefront password; fixtures use the fetcher path (C6).
- A2: LLM fit scoring is nondeterministic, conflicting with replayable/seed-fixed assertions. → Deterministic test doubles for reranker and embeddings in all verifies (C9); trust replay (S3) covers trust only, and fit inputs are logged per bid for audit, not replay.
- A3: "Trust engine catches the dishonest store" is circular if the dishonest behaviors are defined by the trust engine's own config. → Behaviors live in a human-approved fixture manifest; S2 references the manifest; obtaining approval is a ticket step.
- A4: Verify commands that hit real Shopify credentials violate offline verification. → Shopify stub service (C9) is a first-class fixture with its own ticket; live runs are demo procedure.
- A5: Single-use discount codes race under concurrent redemption. → One unique code per accepted offer with short expiry; redemption reconciliation treats duplicate use as an offer-integrity event, not a crash.
- A6: With few stores, 4 shortlist slots and a k-anonymity floor can be unsatisfiable. → Slots collapse gracefully to available distinct stores; k defaults to 1 in fixtures (non-goal notes the production knob).

## Open questions
None blocking. Seed category is a fixture input (`SEED_CATEGORY`) chosen at environment-seed time; auction mechanism refinements and production k-value are logged as design decisions with defaults.
