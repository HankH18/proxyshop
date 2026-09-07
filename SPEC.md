# ProxyShop — Spec

## Core tenet — organic and sponsored, taken further

**This is the organic-versus-sponsored split of a search engine, taken to a much greater degree.**
Every other rule in this document is downstream of it.

A search engine scrapes what is there and renders the non-sponsored result in its own voice. An
advertiser pays for a little more control over the sponsored one. ProxyShop is that, further: a
shop that joins the network buys a **dedicated advocate agent** that writes a pitch for *this*
shopper, chooses which commitments to stand behind, and may condition a discount on the profile.

Two agents, two objective functions, and they are NOT two quality tiers of one pipeline:

- The **buyer-side agent** wants the shopper to buy *a* product. It maximises conversion across the
  whole shortlist, so it pitches **every** candidate as well as it honestly can — scraped shops
  included. This is the organic result, authored by the platform from its own crawl.
- The **shop-side agent** wants the shopper to buy *that shop's* product. This is the sponsored
  result, and it is what a shop buys by joining.

What a shop buys is therefore **not visibility and not a better score** — visibility is organic and
earned by matching. It is the right to make its case in its own voice, and the loop that improves it.

The buyer-side agent MAY choose emphasis, ordering, framing and which true facts to lead with. It
MAY NOT introduce a fact the platform has not checked: an agent optimising for conversion will
oversell, and when the platform writes the copy the platform owns the false claim. A seller's
purchased message carries the seller's motive and is therefore the one checked adversarially against
the catalogue snapshot. See `.swarm-loop/decisions.md` D55.

**What this rules out.** Allocation dominated by discount. Every store has a maximum discount it
will authorise; a market whose award rule is mostly price guarantees every store reaches that
maximum and then differentiates on nothing, which trains the network into a commodity market and
destroys the margin it exists to broker. Discount is one saturating term among several, and clearing
the band is a qualifier rather than a differentiator.

## Problem & intent
Product name: **ProxyShop** — a buyer-side shopping agent network (the "pitch protocol" is the underlying seller-bid contract, not the product name).

Build the capstone implementation of the buyer-side shopping-agent network: a buyer agent clarifies a shopper's intent, per-store advocate agents place sealed bids for that shopper's business, a neutral exchange ranks the bids, the winning offer is redeemed at the merchant's checkout via a single-use discount code reached through a **CheckoutProvider** port, and a trust engine scores each store's pitch-to-outcome reliability from observed results. The simulated redirect provider is the required starting implementation of that port and emits the canonical commerce events (the frozen `LedgerEvent` kinds, C11); a Shopify adapter implements the same port behind the same seam. Merchants are simulated offline by a local stub of the merchant platform, and by real Shopify development stores in the live demo. Everything runs against the same interfaces production would use.

## User-visible behavior
Buyer (web chat app):
- R1: WHEN a buyer states a shopping need, THE SYSTEM SHALL ask at most 3 clarifying questions and produce a structured intent (use case, constraints, budget band) visible to the buyer for confirmation.
- R2: WHEN an intent is confirmed, THE SYSTEM SHALL solicit bids from all eligible participating stores and present a shortlist of up to 4 differentiated slots (best fit / best value / most reliable / specialist), each showing product, price, commitments, store trust indicator, and provenance labels ("store-confirmed" vs "from their website").
- R3: WHEN the buyer accepts an offer, THE SYSTEM SHALL create a single-use discount code on that store, validate it (validity window, combinesWith), and redirect to the store's checkout via cart permalink with the code pre-applied.
- R4: WHEN a purchase completes, THE SYSTEM SHALL record the outcome (pixel event and/or order webhook) and reconcile the two, with the webhook authoritative.
- R5: Buyers authenticate with email magic link; stores never receive buyer identity — only a rotating pseudonym and a coarsened profile.

Merchant (network app + dashboard):
- R6: WHEN a merchant onboards to the network, THE SYSTEM SHALL register that store's outcome-observation surface for its configured checkout provider — under the Shopify adapter this is installing the network's Shopify app, registering the web pixel, and subscribing order webhooks — and start a plain-language onboarding interview that produces an economic envelope (floors, max discount, budget cap, intents to pursue, standing commitments) the merchant approves in writing before activation. This is the merchant-install requirement; it is not the only route to checkout, which is reached through the CheckoutProvider port.
- R7: WHEN an envelope exists but is not yet activated, the store's agent SHALL run in shadow mode: it computes bids for live intents and logs them with reasoning, but submits nothing.
- R8: WHEN active, a network-hosted store agent SHALL bid within hard envelope walls, and every claim and discount it emits SHALL come through a provenance-tagged tool hook; a hosted bid violating this is rejected at the boundary. External bids (Tier-2 agents, seller reference personas) MAY carry free text and asserted claims with provenance `seller_asserted`; these are admitted and routed to claim extraction + verification (R18), never silently trusted. Every externally submitted bid SHALL carry a complete signing envelope — `signer_id`, `key_id`, `issued_at`, `nonce` and `schema_version`, all required; a submission missing any one of them is rejected at the exchange boundary before enqueue. The signature covers canonical bytes binding the auction, the signer, the store, the issue time, the nonce, the key id, the schema version and a payload hash, so changing any of them invalidates it. The registered keyring is `{signer_id: {key_id: secret}}` so a signer's keys can rotate and an unknown key id never falls back to another key. `issued_at` is checked against a finite freshness window in both directions, a consumed `(signer_id, nonce)` keeps rejecting until after that auction's `respond_by` has passed, and a submission arriving after `respond_by` is refused outright.
- R9: The dashboard SHALL show every bid with its rationale, win/loss reports aggregated by intent cluster with reason categories only (fit / price / commitments / trust; no rival amounts), trust score with per-dimension breakdown and event payloads, a kill switch, and envelope editing (versioned).

Exchange & trust:
- R10: Bids are solicited synchronously and in parallel with a hard timeout; every **eligible** Tier-0/Tier-1 store selected for the auction receives either a timely bid or a catalog-derived fallback — a non-responding Tier-1 store falls back to a list-price offer, and Tier-0 stores are represented at list price. Fallback facts are subject to the same verification and hard-constraint rules as bid claims (R18, R19): a fallback cannot satisfy a hard constraint on unverified catalog data.
- R11: Ranking SHALL be a published deterministic combination of fit score, offer value, and trust; it SHALL be blind to network fees, tier, and envelope contents. Allocation is a **sealed solicitation with a published multi-axis award**, not a price auction: solicitations are sealed so a late responder cannot mirror a rival's claims (a verifier confirms a copied claim as readily as an original), and the four differentiated slots are awarded on distinct axes so winning on price wins one slot of four rather than the shortlist. The discount term saturates at the auction's own price band; there is no marginal return past it.
- R12: Trust score per store = one merged observation framework over **six** dimensions: {price_honored, discount_honored, shipped_on_time, not_returned, feedback_match, catalog_claim_accuracy}. Claim-verification outcomes (verified/contradicted/unsupported/ambiguous, with contradicted weighted heavier than unsupported) and transaction-observed outcomes update the same per-dimension Betas with observation-type weights and time decay — one trust system, not two — so verification observations provide trust signal before any transactions exist. Each verification outcome routes to exactly one dimension through a typed, exhaustive claim-type mapping: offer-integrity claims (price, discount, delivery, returns) update their transaction dimension; product-fact claims (ingredients, compatibility, nutrition, specifications) update `catalog_claim_accuracy`, which exists because a false catalog claim is none of the transaction dimensions and is not a buyer complaint. `feedback_match` stays what R14 makes it. New stores start at a neutral low-confidence prior; a published threshold triggers blacklisting bound to business identity, with review/appeal/expiry states, exposed through a versioned seller-eligibility interface that answers whether a store may participate. That interface is consulted before solicitation, before ranking, and before checkout; its unavailable state is fail-closed at each of the three gates; and **each gate is verified at the public orchestration boundary by a denial test with a positive control**, not inferred from the pure functions beneath it. A guaranteed exploration slice exposes low-data stores.
- R13: WHEN a trust-relevant event lands (e.g. failed commitment, routed-buyer feedback), THE SYSTEM SHALL push the full pseudonymous event payload to the affected store agent.
- R14: Post-purchase feedback: only network-routed buyers, one structured prompt ("did it match the pitch?"), weighted by buyer track record, cross-checked against return behavior.
- R15: The trust ledger is append-only; recomputing all trust scores from the ledger SHALL reproduce the served scores exactly.

- R18: THE SYSTEM SHALL decompose pitch text and asserted claims into atomic typed claims with source spans, and verify each against the current catalog snapshot with deterministic comparators, yielding verified | contradicted | unsupported | ambiguous with evidence refs and confidence; results are idempotent per (pitch, verifier version, catalog snapshot) and unsupported/ambiguous are never treated as true.
- R19: Hard constraints are eligibility filters, never score terms, and require verified supporting facts; ambiguous cannot satisfy a hard constraint; a contradicted hard constraint cannot win regardless of pitch quality.

Learning loops:
- R16: The exchange ranking policy SHALL update from conversion outcomes (contextual bandit over intent-cluster × store) such that simulated outcome shifts measurably reorder future shortlists.
- R17: Each store agent SHALL update its own bid policy from its own outcomes (pitch variant × commitment set × discount depth), initialized from network priors built from pitch/value-prop outcomes only — never from discount data of other stores.

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
- C10: Security baseline (adopted from partner architecture): the external bid door requires a complete signing envelope on every submission (`signer_id`, `key_id`, `issued_at`, `nonce`, `schema_version`) over canonical signing bytes, with a `{signer_id: {key_id: secret}}` keyring, a finite freshness window, and nonce consumption persisted past the auction's `respond_by` — no optional-field or legacy-tolerant mode exists on the production route; crawler is SSRF-guarded (private/link-local blocked, redirects bounded to allowed hosts) and identified; scraped text and all pitch content are untrusted data, never instructions — LLM extraction is isolated from tool credentials and validated against strict schemas; checkout URLs validate against the registered seller domain; low-confidence extraction is quarantined; cross-domain endpoints ship OpenAPI examples + consumer contract tests.
- C11: The simulated/redirect checkout path and the Shopify path emit the identical LedgerEvent kinds; the event schema is the E5 upgrade seam and is frozen jointly.

## Non-goals
- No real merchants, payments on the network, ACP/UCP checkout integration, AP2/TAP mandates, or payouts/billing (the per-category rate card is displayed, never charged).
- No buyer PII to merchants; no protected-customer-data scopes; no free-text reviews or review moderation.
- No Tier-2 self-host distribution work beyond keeping `packages/store-agent` importable and the bid API accepting externally signed bids.
- No claim *truth* verification (efficacy, lab testing). Trust is reliability calibration only.
- No mobile clients. No multi-category ontology — one seed category per environment, parameterized.
- No k-anonymity enforcement beyond a configurable floor (default 1 in fixtures; production knob documented).

## Success criteria

**Starting path and extension lanes.** Nothing is cut: every component and every ticket in the plan is built, and Shopify stays in the graph. What is fixed is the order of dependence — **no direct Shopify integration sits on the starting path.** The starting path is S1 as written below, run through `SimulatedRedirectProvider` against `services/shopify-stub` and deterministic doubles, offline (C9): intent → pitches → extraction + verification → eligibility → ranking → acceptance → simulated redirect → the canonical `LedgerEvent` kinds → reconciliation → trust projection and replay. The Shopify adapter and its dev-store install, merchant onboarding and the dashboard, buyer feedback, and both learning loops are **extension lanes** hanging off that path; none of them is a prerequisite of the starting path's completion target, and no live Shopify call may appear in any ticket verify (C9). The demo documentation is split the same way: a starting-slice runbook that depends only on the starting-slice e2e target, and a separate Shopify/onboarding extension runbook. Both live under `docs/demo/` and the S6 lint reads their union.

- S1: End-to-end: intent → ≤3 clarifications → parallel bids → shortlist → accepted offer → code created & validated → checkout completes through the CheckoutProvider port → webhook + pixel reconciled → ledger event → trust update. The simulated redirect provider is the required starting implementation of that step; the Shopify adapter is one alternative behind the same port, and both emit the identical `LedgerEvent` kinds (C11), so the pixel and reconciliation obligations hold on either. Checkable by e2e test running offline against the local stub (C9); the same flow through the Shopify adapter against seeded dev stores (Bogus Gateway) is the documented demo procedure.
- S2: The scripted dishonest store (behaviors defined in the human-approved fixture manifest) falls below the blacklist threshold within the simulation episode budget and disappears from shortlists. Checkable against the manifest, not the trust engine's own config.
- S3: Ledger replay reproduces served trust scores bit-for-bit (R15). Checkable by replay test.
- S4: In simulation, shifting a store's conversion outcomes reorders shortlists for the affected intent cluster (R16), and a store agent's pitch-variant distribution shifts in the direction of its own win/loss record (R17), with discount depth as one axis of that policy rather than its whole content. Checkable by simulation assertions with fixed seeds.
- S5: Two properties at the bid boundary, both required.
  - S5a: A claim carrying no provenance record — absent or empty — is rejected on every path, hosted and external; on the hosted path a claim lacking allowed tool-hook provenance is additionally rejected, and every claim in every accepted bid resolves to a provenance record (R8). Checkable by protocol boundary tests.
  - S5b: External prose is admitted as untrusted data, never as instructions, and extraction assigns `seller_asserted` provenance to each claim it derives *before* that claim reaches the boundary; every extracted factual claim then carries a verification record, and no unverified claim satisfies a hard constraint or counts as verified evidence (R18, R19). Checkable by extraction + verification tests.
- S6: Merchant onboarding: interview → approved envelope → shadow-mode bid log → activation, demonstrable on a fresh dev store in one sitting (R6–R7). Checkable by integration test with mocked interview transcript + one manual demo run. This is an extension lane: it is documented in the Shopify/onboarding extension runbook, not in the starting-slice runbook, and it is not a prerequisite of the starting path.
- S7: Exchange code cannot read envelopes: grant test + import-lint rule both fail the build if violated (C3).
- S8: A golden pitch containing both true and false claims yields distinct atomic verification outcomes with evidence (R18). Release blockers, each with a failing test until satisfied: no blacklisted seller ever surfaces as eligible, no contradicted hard constraint ever wins, no checkout URL outside the registered seller domain is ever returned.

## Reconciliation amendments (approved)
Partner (Sohail) architecture/implementation docs reconciled; approved decisions: (1) merged trust observation framework, verification-first; (2) dual-path claim entry — harnessed-structured for hosted agents, extract-and-verify for external/personas — replacing blanket rejection; (3) starting-slice checkout is simulated/redirect emitting the frozen LedgerEvent schema (C11), Shopify/E5 unchanged behind the same seam; (4) both seller kinds are first-class: hosted harnessed agents and unharnessed reference personas (the personas exercise the external door adversarially); (5) one published rank formula with hard-constraint filters, weights set jointly.

**Acceptance-harness amendment 1 (approved).** Three constraints in the frozen acceptance suite were frozen incidentals rather than goals, and each was distorting the architecture. All three are amended deliberately, and the reasoning is recorded in `.swarm-loop/decisions.md` D52–D54: (a) the external signing and replay fields are **required**, not optional — a fixture omission must never dictate a public security contract; (b) `catalog_claim_accuracy` is a real sixth trust dimension inside the one trust system, rather than product facts being mapped into transaction dimensions to fit a frozen five-name vocabulary; (c) eligibility is asserted at the public orchestration boundary by denial, because the frozen positional tests are unit tests of pure functions and do not establish the system guarantee.

## Adversarial review (resolved)
- A1: Dev stores are password-protected, so they are absent from Shopify's Global Catalog and their public endpoints sit behind the storefront password. → Catalog MCP adapter is contract-tested against a recorded mock; the signed fetcher accepts a storefront password; fixtures use the fetcher path (C6).
- A2: LLM fit scoring is nondeterministic, conflicting with replayable/seed-fixed assertions. → Deterministic test doubles for reranker and embeddings in all verifies (C9); trust replay (S3) covers trust only, and fit inputs are logged per bid for audit, not replay.
- A3: "Trust engine catches the dishonest store" is circular if the dishonest behaviors are defined by the trust engine's own config. → Behaviors live in a human-approved fixture manifest; S2 references the manifest; obtaining approval is a ticket step.
- A4: Verify commands that hit real Shopify credentials violate offline verification. → Shopify stub service (C9) is a first-class fixture with its own ticket; live runs are demo procedure.
- A5: Single-use discount codes race under concurrent redemption. → One unique code per accepted offer with short expiry; redemption reconciliation treats duplicate use as an offer-integrity event, not a crash.
- A6: With few stores, 4 shortlist slots and a k-anonymity floor can be unsatisfiable. → Slots collapse gracefully to available distinct stores; k defaults to 1 in fixtures (non-goal notes the production knob).

## Open questions
None blocking. Seed category is a fixture input (`SEED_CATEGORY`) chosen at environment-seed time; auction mechanism refinements and production k-value are logged as design decisions with defaults.
