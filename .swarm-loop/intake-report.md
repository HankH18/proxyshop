# ProxyShop — INTAKE REPORT

Sources read directly: `SPEC.md` (80 lines), `DESIGN.md` (103), `TASKS.md` (149), `EXECUTION.md` (23), `tickets.json` (all 44 tickets, every field), `.gitignore`, and the existing `.swarm-loop/` state (`decisions.md` D1–D10, `codebase-map.md`, `learnings.md`, `backlog.md`, `goals.json`, `state.json`, `acceptance/{run.py,conftest.py,README.md}`). All graph numbers below were recomputed by me from `tickets.json`, not taken from analysts.

**Standing facts I re-verified myself:** 44 tickets, 79 edges, acyclic, single root T-000 (43 descendants), six sinks (T-022, T-024, T-045, T-054, T-073, T-085), max depth 10, unique 10-hop critical path, 133 acceptance criteria, 29 concurrency-possible scope overlaps, exactly one duplicate test basename (`test_learning.py`), refs coverage missing for C8/C11/A3, verify shapes = 37 pytest / 6 npx / 1 make.

**`.swarm-loop/decisions.md` D1–D10 already exist and are `[verified]` on this machine.** This report does not re-litigate them. Section 2 appends D11 onward, exactly as that file's own footer anticipates.

---

## 1. BLOCKERS — must be resolved before any dispatch

**B1. `goals.json` is empty and the frozen acceptance suite has zero tests. The run cannot be measured or steered.**
Evidence: `.swarm-loop/goals.json` = `{"frozen": false, "frozen_at": null, "metrics": []}`. `.swarm-loop/acceptance/` contains `run.py`, `conftest.py`, `README.md` and **no test files**. `run.py` `_fail`s with exit 1 on "acceptance report contained no tests — collection found nothing", and again on `--epic Ex` matching nothing ("the suite is miswired"). Every metric would therefore be an unmeasurable error, not a zero.
**Decision:** before dispatch, author the frozen suite — one file per epic (`test_e1.py` … `test_e8.py`) plus `test_spec_criteria.py` — following the two rules already written in `acceptance/README.md` (import product code **inside** the test body; mark every test `@pytest.mark.epic(...)` + `@pytest.mark.ticket(...)`, and the three S8 tests additionally `@pytest.mark.blocker("S8-1|2|3")`). Then apply the one pre-freeze change to `run.py` in B2 below, then `swarmloop.py freeze`. Suite composition and per-epic targets are in §7.

**B2. Ten metric commands would each re-run the whole acceptance suite. Add a report-reuse mode to `run.py` before freezing.**
Evidence: `run.py:run_pytest()` shells `python -m pytest <ACCEPTANCE_DIR>` on every invocation; `main()` calls it unconditionally. Twelve metrics × 102 cycles = 1224 full suite runs.
**Decision:** before freeze, add two flags to `run.py`: `--write-report <path>` (run the suite once, dump the JSON records, print nothing) and `--from-report <path>` (skip pytest, read the records, apply the existing `--epic` / `--count-passing` / `--total` logic). Add a `--blocker` filter alongside `--epic` (the `blocker` field is already emitted by `conftest.py:_marker_arg(item, "blocker", "")` — only the filter is missing). Keep the `_fail`-on-missing-report contract identical: a stale or absent report file must exit 1 with empty stdout. Measurement then runs the suite once per cycle and all twelve metrics read from it.

**B3. All 43 non-T-000 verify commands run bare `pytest` / `npx`, which in a fresh worktree resolve to the wrong toolchain or the network.**
Evidence: `which -a pytest` → `/Users/hankholcomb/opt/anaconda3/bin/pytest` (pytest 6.2.4 on x86_64 Python 3.9.7 under Rosetta); `.gitignore` lines `.venv/`, `node_modules/`, `.swarm-loop/worktrees/` mean a fresh `git worktree` has neither. D2 rules "All Python runs … go through the project virtualenv" but phrases it around metric commands, and 37 ticket verify strings say bare `pytest`. pytest 6.2.4 predates both `pythonpath` (7.0) and `consider_namespace_packages` (8.1), so it silently discards the config that makes first-party imports work.
**Decision:** do **not** rewrite the 44 verify strings (they are the executable contract and are mirrored in `backlog.md`). Instead: (a) worktree setup runs `make bootstrap` = `uv sync --frozen && npm ci --prefer-offline` **in that worktree** — never a shared root venv, which would make a worker test the main tree's code through editable installs; (b) every task packet's verify block is literally
```
cd <worktree> && export PATH="$PWD/.venv/bin:$PWD/node_modules/.bin:$PATH" && <verify string>
```
(c) `scripts/verify.sh` asserts `sys.version_info[:2] == (3,12)` and `node_modules/.bin/vitest` exists, failing loudly rather than falling through.

**B4. `test_learning.py` exists at two paths; whole-repo pytest collection aborts before running anything.**
Evidence: T-042 verify `pytest packages/store-agent/tests/test_learning.py -q`; T-083 verify `pytest e2e/test_learning.py -q`. Computed over all 44 verify strings, this is the **only** duplicate basename. Under pytest's default `prepend` import mode with no `__init__.py`, the second module raises `import file mismatch … Interrupted: 1 error during collection`, aborting the entire run — which is the gate every ticket must pass.
**Decision:** T-000 sets `addopts = "--import-mode=importlib …"`, `consider_namespace_packages = true`, **and** `pythonpath = ["."]` in the single root `[tool.pytest.ini_options]`. All three are required: under importlib alone, a bare sibling-helper import inside a tests dir raises `ModuleNotFoundError`. Do not rename either file.

**B5. T-000's acceptance criterion 3 names a directory that no ticket uses and omits eight that later verify commands require. T-000 is the sole root of all 43 other tickets.**
Evidence: T-000 acc 3 = "package layout matches DESIGN (packages/protocol, packages/store-agent, apps/{buyer,merchant,exchange,trust}, services/{ingest,shopify-stub,sim}, pixel/)". `packages/protocol` appears exactly once in `tickets.json` (that line); `packages/contracts` is T-010's scope and verify. Missing from the list but required by later scopes/verifies: `packages/llm` (T-014), `packages/verification` (T-065), `apps/seller-reference` (T-045), `db/migrations` (T-011), `fixtures/` (T-080 + 6), `e2e/` (T-082-084), `docs/{demo,tests}` (T-085), and `apps/{merchant,buyer}/svc`.
**Decision:** replace acc 3 with the explicit tree in §3.1 (which is the union of every ticket scope and verify path). D1 already rules `packages/contracts`; also correct `DESIGN.md:45`, which is the line T-020/T-035/T-065 actually load via `DESIGN#interfaces-contracts-between-tickets` and which still says `packages/protocol`. Explicitly **do not** create `packages/ranking`, `packages/observability`, top-level `graph/`, or `evals/` — no ticket scope or verify names them.

**B6. Five shared surfaces are required by tickets whose scope cannot reach them, and are owned by nobody.**
Evidence, each verified by grep over `tickets.json`: (i) `Makefile` — 0 hits, yet T-080 acc 3 is "`make demo-seed` idempotent against stub" (scope `fixtures/**`) and T-085 acc 1 is "referenced make targets exist" (scope `docs/**`); (ii) docker-compose app services — `docker` appears only in T-000, whose acc 2 covers "postgres+neo4j+redis" only, yet T-081 acc 3 is "Sim runs headless against compose" and T-082's objective is "all against compose", and C7 says "All services run under docker-compose locally"; (iii) per-service FastAPI entrypoint/router — 0 hits for `main.py`/`router`, yet DESIGN §Service APIs pins 4 routes on exchange, 4 on trust, 5 on merchant, 1 on ingest, 1 on the store-agent runner; (iv) root manifests + lockfiles — 0 hits for `pyproject`, `package.json`, `tsconfig`, `vitest.config`, `uv.lock`, yet nearly every ticket adds a dependency and `codebase-map.md` already rules "No worker touches a dependency manifest"; (v) `conftest.py` — 0 hits, yet six test directories are claimed by 3–7 tickets each.
**Decision:** all five are **orchestrator-owned, created complete by T-000, and frozen** — no worker edits them. Exact contents in §3. Makefile targets delegate into owned directories so `make demo-seed` → `fixtures/seed/` (T-080) and `make e2e-live` → `docs/demo/e2e_live.sh` (T-085) without either ticket touching the Makefile. Compose uses a root file with an `include:` list of per-service fragments, each pre-created as a valid empty-`services:` stub inside its owner's glob. `main.py` auto-discovers `src/*/routes.py`, so every route lands inside its own feature ticket's scope.

**B7. T-065 authors the golden set it is graded against, and has no dependency on the human-approved manifest. This is exactly the circularity SPEC A3 exists to prevent.**
Evidence: T-080 objective — "Manifest also defines the golden intent/pitch set (incl. persona scripts, contradiction/stale/variant/unit/injection/claim-splitting cases) used by verification and eval gates"; acc 1 — "Manifest + golden set committed with recorded human approval artifact". T-065 scope includes `fixtures/golden/**`; acc 1 — "(S8, external ground truth: approved golden set)"; `depends_on = ["T-010","T-021","T-060"]` — I computed T-065's ancestor set as {T-000,T-010,T-011,T-012,T-014,T-020,T-021,T-060}; T-080 is absent. By contrast T-045 and T-062 both carry the T-080 edge.
**Decision:** add `T-080` to `T-065.depends_on` (verified acyclic — T-080's closure is {T-000,T-013}) **and remove `fixtures/golden/**` from T-065's scope**, with a non_goal: "does not author the golden set; `fixtures/golden/**` is T-080's human-approved ground truth, read-only here." A prose note alone is insufficient — the scope glob is what a worker obeys. Cost, computed: the T-080-gated set grows 12 → 15 (adds T-032, T-035, T-065); buildable-without-approval drops 31 → 28.

**B8. `parallel_safe` is unusable as the dispatch predicate in both directions.**
Evidence: `TASKS.md:149` defines it as "scope disjoint from every other open ticket". I recomputed glob-overlap ∧ no-dependency-path: **10 tickets marked true overlap a concurrently-schedulable peer** — T-022→{T-021,T-023,T-024,T-080}, T-023→{T-021,T-022,T-080}, T-031→{T-033}, T-035→{T-033,T-034}, T-042→{T-043,T-044}, T-043→{T-042,T-044}, T-044→{T-042,T-043}, T-053→{T-051,T-052,T-080}, T-064→{T-061,T-063}, T-080→{T-021,T-022,T-023,T-040,T-053,T-065,T-071}. And **10 marked false have zero concurrent overlap** — T-010, T-020, T-030, T-041, T-050, T-054, T-060, T-070, T-072, T-081.
**Decision:** the scheduler computes concurrency from the **narrowed ownership map in §4** (src glob + the exact test files the verify names), never from the flag. `parallel_safe` is demoted to a hint. `EXECUTION.md` rule 3 is superseded accordingly — see §5.

**B9. EXECUTION rule 2 makes every one of 44 workers run the full root `make verify` against one shared Postgres/Neo4j/Redis stack.**
Evidence: `EXECUTION.md:7-9` "A ticket is closed only by its Verify command passing, then the top-level `make verify`"; `tickets.json.full_verify = "make verify"`; `DESIGN.md:98` defines that as "ruff + mypy + eslint + tsc + pytest + vitest against stub services and doubles". D4 already rules that "writes to Neo4j" is a shared surface — and a full-suite run writes to it, on every ticket close, from every worktree.
**Decision:** split the gate. **Per-ticket close = the ticket's own verify + `make check`** (ruff + mypy + eslint + tsc + the worktree's own pytest/vitest, per-worker DB isolation, no cross-package DB tests). **`make verify` (the full pipeline, DB stack up, Neo4j flock held) runs once per merged wave on the integration branch, serialized.** Amend `EXECUTION.md` rule 2 to say so before dispatch; otherwise the run either serializes itself through a global lock or produces phantom failures that rule 4 (revert) and rule 6 (escalate at 2 failures) will misclassify as design defects.

**B10. T-000's own gate cannot go green on an empty tree without explicit flags.**
Evidence: T-000 acc 1 = "`make verify` exits 0 on fresh clone with services up"; non_goals = "No feature code; no schemas". Measured behaviour: `pytest <empty dir>` exits **5**; `pytest <missing path>` exits **4**; `vitest run` with no test files exits **1**. D7 already rules `--passWithNoTests` for the root verify only.
**Decision [SUPERSEDED 2026-09-05 by ESC-023, ruled by Hank — the flag is gone; `run_vitest` now FATALs on a zero collection, mirroring `run_pytest`. See D7 in decisions.md]:** `scripts/verify.sh` passes `--passWithNoTests` **only** on the root vitest invocation (never in `vitest.config.ts`, which would let T-054's filtered verify pass vacuously), and maps pytest exit 5 → 0 **only** in the root verify, printing a loud WARNING naming the empty path. Ticket verifies get neither treatment: a ticket whose tests do not exist yet is correctly red (D7). T-000 additionally ships one smoke test per workspace so the empty-suite path is exercised, not relied upon.

---

## 2. PINNED DECISIONS

These append to `.swarm-loop/decisions.md` as **D11 onward**, exactly as that file's footer anticipates. Quote verbatim into every task packet. Where these and DESIGN/SPEC prose disagree, **these win**; where these are silent, `tickets.json` wins over prose.

**D11 — There is ONE rank formula and it is `DESIGN.md:85`.** `rank_score = w_m*intent_match + w_e*verified_claim_ratio + w_t*trust + w_v*price_value + w_d*delivery_fit − policy_penalties`, features normalized to [0,1]. `DESIGN.md:87`'s three-term `w_f*fit + w_v*offer_value + w_t*trust` is superseded prose — it predates the reconciliation amendment and lacks the `verified_claim_ratio` term that T-032's dependency on T-065 exists to supply. Eligibility filters run first, never as score terms (R19).

**D12 — Rank weights are pinned now, in one versioned config object.** `RankingWeights` is defined in `packages/contracts` and loaded from env with these defaults: `w_m=0.35, w_e=0.20, w_t=0.20, w_v=0.15, w_d=0.10` (sum 1.0). `price_value = clamp((list_price − total_price)/list_price, 0, 1)` against the bidding store's own list price. `delivery_fit` reads `Offer.delivery_estimate_days` against `Intent.ship_to`, and is **0.5 (neutral) when absent**, never 0. `policy_penalties = Σ per-kind penalties over open ledger.policy_events for that store in the scoring window`; initial catalogue: `severe_policy_violation = 0.30`. Tie-break, in order: verified hard-fit count → trust → price → bid_id.

**D13 — `verified_claim_ratio` is earned by every bid on the same path; hosted bids get no constructed value.** The exchange runs the same T-065 comparator pass over every candidate's claims regardless of provenance. Hook-provenanced hosted claims are catalog-derived, so they return `verified` and the ratio lands near 1.0 — earned, not granted. When no `VerificationResult` is available (verification unavailable or timed out) the term is **0.5 for every path** and an `unverified` warning label is attached. A constructed 1.0 for hosted bids would make `rank_score` a function of tier and fail T-032 acceptance 2's blindness test (R11).

**D14 — `Intent.preferences[].weight` feeds `intent_match` only, never the outer published weights.** Per-intent preference weights are consumed inside T-031's fit scorer. T-032's published weights are fixed per environment and identical for every buyer. Two weighting layers, one score; they never mix.

**D15 — The ledger hash chain.** A single global chain over `ledger.commerce_events` ordered by insertion sequence. `event_hash = sha256(prev_hash || canonical_json(event))` where `canonical_json` is RFC-8785 JCS (sorted keys, no whitespace) and timestamps are UTC RFC-3339 at millisecond precision. `commerce_events.idempotency_key` **IS** `LedgerEvent.event_id` — there is no second identifier. The writer takes a row-level advisory lock on the chain tail. Nothing outside `apps/trust/src/ledger/**` (T-011) defines its own hashing.

**D16 — Trust decay is a pure function of ledger timestamps, never `now()`.** `weight = exp(−ln2 · (t_eval − t_obs)/HALF_LIFE)`. `t_eval` is persisted per dimension as `decayed_at` at snapshot-write time; recomputation from the ledger re-evaluates each observation at its **recorded** `decayed_at`. This preserves idle decay while making S3 replay exact. Anything reading a wall clock inside scoring breaks S3.

**D17 — The claim-type → trust-dimension mapping is a T-080 manifest artifact.** R12 requires verification outcomes to feed the same five per-dimension Betas (DESIGN.md:82 explicitly rejects two trust systems). The table that says which `claim_type` a contradicted claim penalises — price claims → `price_honored`, shipping claims → `shipped_on_time`, discount claims → `discount_honored`, etc. — lives in the human-approved manifest and is consumed read-only by T-062. `HALF_LIFE` and the new-store prior N are likewise manifest constants, not trust-engine config (this is what keeps S2 checkable "against the manifest, not the trust engine's own config").

**D18 — Embeddings: `HashEmbedding` is the configured default in every environment, not a test-only fallback.** `EMBEDDING_PROVIDER` defaults to `hash`. `HashEmbedding` must emit **L2-normalised 1024-d float vectors** so the real cosine index (D6) is exercised. `torch`/`sentence-transformers` are an **optional extra**, declared but never installed by default (measured: +679 MB of wheels, plus a ~2.2 GB weight fetch on first call — a C9 violation and an unattended-run stall). T-012 additionally implements a `LocalBgeEmbedding` provider selected by `EMBEDDING_PROVIDER=local_bge`, with a marker-gated test deselected from `make verify` that skips cleanly when weights are absent. `make e2e-live` sets `local_bge`.

**D19 — LLM: the double is the default provider, and the network is blocked in tests.** `LLM_PROVIDER` defaults to `double`; the real Anthropic client is constructed **lazily** and only when `LLM_PROVIDER=anthropic`. Never construct a client at import time — that takes down `pytest packages/llm`, `test_extraction.py` and `test_onboarding.py` on a machine with no key (D3). The root `conftest.py` installs a socket guard blocking all non-loopback connections during pytest, and the dispatcher exports `ANTHROPIC_API_KEY=sk-ant-DOUBLE-DO-NOT-USE`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, so an accidental live call fails in milliseconds with a legible message instead of hanging on SDK retries.

**D20 — "Recorded" means hand-authored, committed, human-reviewed fixture JSON derived from published API documentation.** No ticket verify performs a live capture; re-capture against real credentials is an optional step in T-085's runbook only. Each ticket's recordings live inside its own scope: T-013 → `services/shopify-stub/fixtures/recorded/**`, T-014 → `packages/llm/fixtures/recorded/**`, T-023 → `fixtures/mcp/**`. Every recording file carries a provenance header naming the doc version. Applies to T-013 acc 1, T-014 acc 3, T-023 acc 2 — nobody blocks waiting for credentials that do not exist here (D3).

**D21 — Checkout URL and code shape.** Cart permalink template, used identically by the stub parser (T-013), the merchant builder (T-052) and the exchange validator (T-033): `https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}`. Domain validation compares the permalink **host** to `app.sellers.domain` by exact match — no subdomain wildcards (C10, S8 release blocker). Code format: `PSX-` + 8 characters of **randomly generated** Crockford base32, uppercase, `usageLimit:1`, expiry `min(48h, offer.expires_at)`, stored keyed by `offer_id`. The code is never derived from `offer_id` — a derivable single-use redeemable is a guessable one.

**D22 — Checkout mode: `CHECKOUT_MODE ∈ {redirect, shopify_stub}`, default `redirect`.** SPEC amendment 3 and DESIGN.md:84 both make the starting slice redirect/simulated. **Both modes always create the code via merchant `POST /codes`** and both emit an identical `LedgerEvent` kind sequence (C11 — DESIGN.md:84 explicitly rejects divergent event schemas per mode). T-033's golden kind sequence is the shared reference that T-081, T-082, T-083 and T-084 all assert against; all of them run the default.

**D23 — `LedgerEvent` kind enum, extended once in T-010.** Add `auction_opened`, `auction_closed`, `offer_integrity`, `blacklisted`, `blacklist_expired` to the DESIGN.md:58 enum. `payload` is a **discriminated union, one shape per kind**. The pixel↔webhook join keys are pinned on **both** `checkout_pixel` and `order_paid` as `{checkout_token, order_ref, client_id, discount_code}` — snake_case, exactly these names. T-051 and T-061 import the same generated type; they do not each invent a key name. Add `C11` to T-010's refs so the ticket that freezes the enum reads the constraint requiring both checkout paths to emit identical kinds.

**D24 — `Claim` and `Offer` are extended once, in T-010, before anything consumes them.**
`Claim = {claim_id, key, claim_type, value, unit?, source_span?: {pitch_ref, start, end}, provenance}`. `claim_id` is a content hash over `(pitch_ref, key, canonicalized value, claim_type)` — stable across re-extraction, which is what makes T-065's idempotency criterion satisfiable and lets `VerificationResult.claim_ref` point at something. `Bid` gains `pitch_ref`.
`Offer = {offer_id, product_ref, variant_ref, unit_price, currency, discount, commitments, total_price, checkout_url, expires_at, delivery_estimate_days?}`. `variant_ref` is required — cart permalinks are variant-scoped and T-013's stub exposes no product→variant query. `expires_at` is what T-032's expiry filter reads.
Three different objects are called "Offer" in the doc set: the protocol `Offer` above, the Neo4j `Offer{offer_id, price, currency, availability, observed_at}` node (a store's observed listing), and the `app.offers` table row. They are distinct; the protocol field is `bid_offer_id` where ambiguity would otherwise arise.

**D25 — `TrustSnapshot` shape.** `{store_id, score, confidence, score_version, snapshot_version, low_data: bool, blacklisted: bool, dims: {price_honored, discount_honored, shipped_on_time, not_returned, feedback_match}: {alpha, beta, decayed_at}}`. **Do not** add a sixth `claim_veracity` dimension — DESIGN.md:82 explicitly rejects two trust systems and requires verification outcomes to layer onto the same five Betas via D17's mapping. **Do not** add a `blacklist_state` enum — review/appeal/expiry states live in `app.seller_blacklist(reason_code, source, starts_at, expires_at, status, reviewed_by)`; the exchange needs only the boolean for fail-closed exclusion.

**D26 — The exchange writes to the ledger only through trust `POST /events`.** DESIGN.md:77 gives the exchange role **no write** on `ledger.*`, yet `ranking_runs` and `ranking_candidates` live there. T-030 acceptance 3 already states the pattern ("logged to ledger via trust API stub"); T-032's ranking runs and exclusion_reasons follow it. Bandit posteriors are **exchange-owned runtime state in Redis** (`bandit:{cluster_id}:{store_id}`), rebuildable by replaying `policy_event` ledger entries — no new table, no migration, no late edit to T-011's exclusive `db/migrations/**` scope. If durability across restarts is later required, that is an EXECUTION rule 5 escalation, not a worker's improvisation.

**D27 — Tiers.** Tier-0 = a store present in the catalog graph with no agent and no envelope; always represented by a synthesized list-price bid. Tier-1 = a network-hosted store-agent (`packages/store-agent` runtime). Tier-2 = an external self-hosted agent bidding through the signed `POST /bid` door. `Store.tier: enum[0|1|2]` is pinned in `packages/contracts` by T-010. **Solicitation is tier-aware (R10); ranking is tier-blind (R11).**

**D28 — Shortlist slots.** Filled in the order fit → value → reliability → specialist, each drawn from the eligible set minus already-slotted bids, collapsing gracefully to the available distinct stores (A6). `fit` = max `intent_match`; `value` = max `price_value`; `reliability` = max `trust`; `specialist` = the bid satisfying the **greatest number of non-hard `Intent.preferences`** among those not already slotted, ties broken by D12's stable tie-break. The `specialist` **slot** is unrelated to the `specialist` **persona** in T-045.

**D29 — Provenance → buyer-facing label.** A generated constant in `packages/contracts`, imported by both T-032 (producer) and T-072 (renderer): `owner_statement | envelope_rule → "store-confirmed"`; `scraped | pixel_feed → "from their website"` (`pixel_feed` is the merchant's own installed-app pixel, so it is store-confirmed only if the merchant asserted it — it is not; it is observed, hence "from their website"); `learned_policy | network` are internal and never surface to buyers; `seller_asserted` carries **no** provenance label — SPEC R2 fixes exactly two label strings — and instead surfaces the R18 verification badge (`verified|contradicted|unsupported|ambiguous`). T-072 renders the `provenance_labels` the exchange supplied; it does not re-derive them. `Provenance.authority_rank` is either given defined semantics per hook in T-040 or removed from the schema in T-010 — an unused required int will be filled arbitrarily by whichever ticket touches it first.

**D30 — R14's buyer track record keys off the `app.*` buyer account id, never `vault.*`.** DESIGN.md:77 restricts `vault.*` to the buyer role and puts buyer accounts in unrestricted `app.*`. Trust reaches a stable buyer identity by joining `order_ref → app.offers → app.intents → app.buyer accounts`. Session pseudonyms still rotate per session and stores still never receive identity (R5 intact). T-070's grant test and R14 stop contradicting each other, with no new HMAC primitive.

**D31 — `POST /internal/outcomes`: producer is T-061, consumer is T-034.** T-061 owns the authoritative reconciled conversion/refund truth; T-034 exposes the intake route inside its own `apps/exchange/src/policy/**` scope. Payload pinned in T-010: `{cluster_id, store_id, auction_id, outcome: enum[converted|not_converted|refunded], observed_at}`. T-034 verifies against a recorded outcome fixture (no new dependency edge); T-061 is added to T-083's `depends_on` so the e2e that needs the live path cannot start before the producer exists.

**D32 — Merchant route ownership.** `GET/PUT /stores/{id}/envelope` and `POST /stores/{id}/kill` belong to **T-053**, whose scope gains `apps/merchant/svc/src/envelope/**`. `GET /stores/{id}/trust` belongs to **T-064**. T-054 stays a pure frontend consumer and its non_goal "No new APIs" stands unchanged.

**D33 — T-082 acceptance 1 is an exact per-kind multiset, not "exactly once".** "Each LedgerEvent kind appears exactly once" is unsatisfiable against T-082's own acceptance 2 ("Shortlist contained fallback + hosted bids" ⇒ ≥2 `bid_placed`, ≥2 `shown`) and against kinds the happy path never emits. Replace with an exact expected-count table over the **complete** enum, asserted as multiset equality: `bid_placed == n_stores_solicited` (read from the run fixture), `shown == len(shortlist.slots)`, `auction_opened == auction_closed == accepted == code_created == checkout_redirect == checkout_pixel == order_paid == reconciled == 1`, `refund == order_fulfilled == feedback == offer_integrity == blacklisted == blacklist_expired == 0`, `claim_verified == the run's asserted-claim count`. A subset or `>=1` assertion does not satisfy this criterion.

**D34 — The C3/S7 import lint has two owners.** T-000 ships a standalone `.importlinter` file (not a table inside the shared root manifest) containing the forbidden contract "apps.exchange must not import sealed/envelope modules", and wires `lint-imports` into `make verify` / `make check`. T-011 owns the **proof**: a deliberately-violating fixture module plus a test asserting the check exits non-zero, added to its verify string. Both halves must exist or S7 is only half-built.

**D35 — Only one pytest configuration exists in the repo: the root `pyproject.toml`.** No package-level `pyproject.toml` may contain `[tool.pytest.ini_options]`; no `pytest.ini`, `tox.ini [pytest]`, or `setup.cfg [tool:pytest]` may exist anywhere. Six verify commands pass a **directory** to pytest (`packages/contracts`, `packages/llm`, `packages/verification`, `services/shopify-stub`, `services/sim`, `apps/seller-reference`); a nested config table shifts rootdir for exactly those invocations and silently drops the root `pythonpath`, `addopts` and marker registry — producing a ticket-local failure that `make verify` from the root does not reproduce. Enforced by `scripts/check_verify_contracts.py`.

**D36 — No test file outside `pixel/` may contain the substring "pixel" (any case) in its repo-relative path.** T-051's verify is `npx vitest run pixel`, a case-insensitive substring filter over the whole path, not a project selector. T-050's acceptance 2 is "webPixelCreate called with expected settings payload", so the natural filename `webPixelCreate.test.ts` would be collected by T-051's verify. Name it `apps/merchant/app/routes/install.test.ts`. Enforced by `scripts/check_verify_contracts.py`.

**D37 — Neo4j parallel isolation is scheduler serialization plus an flock, not tenant scoping.** This narrows D4's ruling. D4's verified half stands (Community has exactly one database, so per-worker Neo4j databases are impossible). Its design half — tenant scoping inside the single database — is replaced: tenant properties would force composite uniqueness constraints and tenant-parameterised Cypher across seven tickets written by seven agents, diverging from DESIGN's "uniqueness constraints on every stable ID". Instead: (a) the scheduler never co-schedules two graph-writing tickets (T-012, T-020, T-021, T-022, T-023, T-024, T-031); (b) T-000's shared `services/ingest/tests/conftest.py` and `apps/exchange/tests/conftest.py` hold a session-scoped `flock` on `/tmp/proxyshop-neo4j.lock` as a safety net, resetting inside the lock; (c) vector-index creation is `CREATE VECTOR INDEX … IF NOT EXISTS` so a re-entrant session never errors. Postgres per-worker database and Redis per-worker logical DB index are unchanged from D4.

**D38 — Every ticket runs with `PROXYSHOP_WORKER` set, and the root conftest fails the session if it is unset.** A silent fallback to a shared default is how every worker ends up on the same database. Postgres roles are **cluster-global** (verified: `pg_authid.relisshared = true`), so `CREATE ROLE` lives in a one-time `db/init/00-roles.sql` mounted into the container, and `db/migrations` creates roles idempotently (`DO $$ … EXCEPTION WHEN duplicate_object THEN NULL; END $$;`) while GRANTs — which are per-database — stay in the migration so every `proxyshop_w<n>` gets its own correct copy. T-011 gains an acceptance criterion: migrations apply cleanly a **second** time into a **second** database on the same cluster.

**D39 — `FLUSHALL` is banned repo-wide.** Redis isolation is per-worker logical DB index **plus** a `w{N}:` key prefix applied centrally in T-000's Redis client wrapper. `FLUSHDB` (current index only) is the permitted reset. A grep gate in `make verify` fails the build on any occurrence of `FLUSHALL`. `maxmemory-policy` is `noeviction` — silent eviction of `auction:{id}` would read as a flaky test, never as a config error.

**D40 — Any server a test starts binds port 0 and reports its real port through a fixture.** No hard-coded ports in test code. Compose publishes only the datastore and stub ports, all env-overridable, from the block verified free under D9.

---

## 3. T-000 SCAFFOLD SPECIFICATION

T-000's scope is `["**"]` and it is the only ticket that may create any of this. Everything here is **frozen after T-000 closes** — no worker edits it; a worker that needs it changed escalates per EXECUTION rule 6.

### 3.1 Directory tree (replaces T-000 acceptance 3)

```
packages/contracts/{schemas,src,generated/python,generated/ts,tests}
packages/llm/{src/llm,tests,fixtures/recorded}
packages/store-agent/{src/store_agent/{hooks,runtime,learning,modes,external},tests}
packages/verification/{src/claim_verification,tests}
apps/exchange/{src/exchange/{auction,retrieval,ranking,accept,policy,reports},tests}
apps/trust/{src/trust/{ledger,events,reconcile,scoring,feedback,snapshot,verification},tests}
apps/merchant/app/{routes,dashboard}
apps/merchant/svc/{src/merchant_svc/{collector,codes,onboarding,envelope,install},tests}
apps/buyer/app/{intent,shortlist,feedback}
apps/buyer/svc/{src/buyer_svc/{auth,vault,intent,accept,feedback},tests}
apps/seller-reference/{src/seller_reference,tests}
services/ingest/{src/ingest/{graph,embeddings,adapters,extraction,er,scheduler},tests}
services/shopify-stub/{src/shopify_stub,tests,fixtures/recorded}
services/sim/{src/sim,tests}
pixel/{src,tests}
db/{migrations,init}
fixtures/{manifest,approval,seed,catalog,personas,golden,tests,pages,er,mcp,envelopes,interviews,dialogues}
e2e/support/{s1,learning,dishonest}
docs/{demo,tests}
scripts/
```
`packages/protocol`, `packages/ranking`, `packages/observability`, top-level `graph/` and `evals/` are **not created** — no ticket scope or verify names them.

**Import namespaces are fixed here and stated in every packet** (three directories are hyphenated and therefore not importable by their directory name): `services/shopify-stub` → `shopify_stub`; `packages/store-agent` → `store_agent`; `apps/seller-reference` → `seller_reference`. Every hyphenated fixture subdirectory (`fixtures/{pages,er,mcp,envelopes,interviews,dialogues}`) is created empty with `.gitkeep` so no worker races on the parent.

### 3.2 `/pyproject.toml` (root — the ONLY pytest/ruff/mypy config in the repo)

```toml
[project]
name = "proxyshop"
version = "0.0.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "proxyshop-contracts","proxyshop-llm","proxyshop-store-agent","proxyshop-verification",
  "proxyshop-buyer-svc","proxyshop-merchant-svc","proxyshop-exchange","proxyshop-trust",
  "proxyshop-seller-reference","proxyshop-ingest","proxyshop-shopify-stub","proxyshop-sim",
  "proxyshop-fixtures",
  "fastapi==0.141.1","uvicorn[standard]==0.52.4","python-multipart==0.0.32",
  "pydantic==2.13.5","pydantic-settings==2.15.0","email-validator==2.3.0",
  "httpx==0.28.1","anthropic==1.2.0","mcp==2.1.1",
  "psycopg[binary,pool]==3.3.5","neo4j==5.28.5","redis[hiredis]==8.1.0",
  "jsonschema[format]==4.26.0","cryptography==50.0.1","pyjwt==2.13.0",
  "beautifulsoup4==4.15.0","lxml==6.1.2","protego==0.6.2",
  "numpy==2.5.2","scipy==1.18.1",
  "pyyaml==6.0.3","structlog==26.1.0","tenacity==9.1.2",
]

[project.optional-dependencies]
embeddings = ["sentence-transformers==6.0.1","torch==2.13.0"]   # NEVER installed by default (D18)

[dependency-groups]
dev = [
  "pytest==9.1.1","pytest-asyncio==1.4.0","pytest-cov==7.1.0","pytest-timeout==2.4.0",
  "pytest-socket==0.7.0","hypothesis==6.167.1","respx==0.23.1","time-machine==3.5.0",
  "syrupy==6.0.0","datamodel-code-generator==0.76.0",
  "ruff==0.16.5","mypy==2.3.1","import-linter==2.14",
  "types-jsonschema==4.26.0.20260518","types-pyyaml==6.0.12.20260815",
]

[tool.uv.sources]
proxyshop-contracts = { workspace = true }
proxyshop-llm = { workspace = true }
proxyshop-store-agent = { workspace = true }
proxyshop-verification = { workspace = true }
proxyshop-buyer-svc = { workspace = true }
proxyshop-merchant-svc = { workspace = true }
proxyshop-exchange = { workspace = true }
proxyshop-trust = { workspace = true }
proxyshop-seller-reference = { workspace = true }
proxyshop-ingest = { workspace = true }
proxyshop-shopify-stub = { workspace = true }
proxyshop-sim = { workspace = true }
proxyshop-fixtures = { workspace = true }

[tool.uv.workspace]
# explicit list: an `apps/*` glob does NOT match apps/buyer/svc or apps/merchant/svc
members = [
  "packages/contracts","packages/llm","packages/store-agent","packages/verification",
  "apps/buyer/svc","apps/merchant/svc","apps/exchange","apps/trust","apps/seller-reference",
  "services/ingest","services/shopify-stub","services/sim","fixtures",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
bypass-selection = true

[tool.pytest.ini_options]
minversion = "8.2"
addopts = "--import-mode=importlib --strict-markers --strict-config --timeout=300 -ra"
consider_namespace_packages = true
pythonpath = ["."]
testpaths = ["packages","apps","services","pixel","fixtures","e2e","docs"]
norecursedirs = [".*", "node_modules", "dist", "build", ".venv", "*.egg-info", ".swarm-loop"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
markers = [
  "docker: requires the compose datastore stack",
  "graph: writes to Neo4j (takes the shared flock)",
  "slow: long-running simulation or e2e",
  "needs_model: requires downloaded embedding weights; deselected from make verify",
]

[tool.ruff]
target-version = "py312"
line-length = 100
extend-exclude = ["node_modules",".venv","dist","build",".swarm-loop"]
[tool.ruff.lint]
select = ["E","F","I","UP","B","ASYNC"]
ignore = ["E501"]
[tool.ruff.lint.per-file-ignores]
"**/tests/**" = ["B"]
"e2e/**" = ["B"]

[tool.mypy]
python_version = "3.12"
namespace_packages = true
explicit_package_bases = true
files = ["packages","apps","services","e2e"]
exclude = "(node_modules|\\.venv|dist|build|\\.swarm-loop)"
ignore_missing_imports = true          # workers cannot edit this file to add overrides
strict = false
disallow_untyped_defs = false
warn_unused_ignores = false
```
Rationale for the permissive mypy/ruff posture: `make check` gates every ticket, and no worker may edit this file to add a per-module override. Tighten only as a deliberate serialized change after the graph closes.

### 3.3 Per-member `pyproject.toml` (identical template ×13)

```toml
[project]
name = "proxyshop-NAME"
version = "0.0.0"
requires-python = ">=3.12,<3.13"
dependencies = []                      # ALL deps live in the root manifest only

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
only-include = ["src"]
sources = ["src"]
```
`only-include`/`sources` (rather than enumerating each subpackage) is load-bearing: it makes a subpackage created **after** `uv sync` importable with no re-sync and no manifest edit. Roughly 29 tickets add a new top-level directory under an existing package's `src/`; the enumerating form would make each of them a forbidden manifest edit. **No member file may contain `[tool.pytest.ini_options]`** (D35).

### 3.4 `/.importlinter` (separate file so S7 contracts are not a root-manifest edit)

```ini
[importlinter]
root_packages = exchange trust store_agent merchant_svc buyer_svc ingest contracts llm claim_verification

[importlinter:contract:c3-exchange-cannot-read-envelopes]
name = C3/S7: exchange must never import sealed-state or envelope modules
type = forbidden
source_modules = exchange
forbidden_modules = store_agent.modes merchant_svc.envelope merchant_svc.onboarding trust.ledger.sealed
```
T-011 extends this small file (never the root manifest) with additional sealed module names as they land, and owns the violating fixture proving it fires (D34).

### 3.5 `/package.json` (npm workspaces root — the ONLY place devDependencies live)

```json
{
  "name": "proxyshop", "private": true, "type": "module",
  "workspaces": ["packages/contracts","apps/buyer","apps/merchant","pixel"],
  "engines": { "node": ">=22.12.0" },
  "scripts": { "test": "vitest run", "lint": "eslint .", "typecheck": "tsc -b" },
  "devDependencies": {
    "typescript": "5.9.3", "@types/node": "24.10.1",
    "vite": "8.2.2", "vitest": "4.1.11", "@vitest/coverage-v8": "4.1.11",
    "@vitejs/plugin-react": "6.1.1", "jsdom": "29.1.1",
    "@testing-library/react": "16.3.3", "@testing-library/jest-dom": "6.6.3",
    "eslint": "9.39.5", "typescript-eslint": "8.69.0", "@eslint/js": "9.39.5",
    "json-schema-to-typescript": "16.0.0"
  }
}
```
**Per-workspace manifests:** `apps/buyer` → `next@15.5.25`, `react@19.2.8`, `react-dom@19.2.8`, `@proxyshop/contracts@*`, devDeps `eslint-config-next@15.5.25`, `@types/react@19.2.7`, `@types/react-dom@19.2.4`. `apps/merchant` → `react-router@7.18.3`, `@react-router/{node,serve,fs-routes}@7.18.3`, `@shopify/shopify-app-react-router@2.0.1`, `@shopify/shopify-api@14.0.1`, `@shopify/shopify-app-session-storage-postgresql@7.0.1`, `@shopify/polaris@13.9.5`, `@shopify/app-bridge-react@4.2.13`, **`react@18.3.1`/`react-dom@18.3.1`**, `isbot@5.1.31`, devDeps `@react-router/dev@7.18.3`, `@shopify/cli@4.7.0`, `@types/react@18.3.27`, `@types/react-dom@18.3.7`. `pixel` → devDep `@shopify/web-pixels-extension@2.18.0`. `packages/contracts` → deps `ajv@8.20.0`, `ajv-formats@3.0.1`; script `"codegen": "json2ts -i schemas -o generated/ts"`.

Why these exact pins (each measured against the live registry): `typescript@latest` is **7.0.2** (the Go rewrite) and is outside `typescript-eslint@8.69.0`'s peer range `>=4.8.4 <6.1.0` and `@react-router/dev@7.18.3`'s `^5.1.0 || ^6.0.0` — hence 5.9.3 (D8). `react-router@latest` is **8.3.1** but `@shopify/shopify-app-react-router@2.0.1` peers `^7.6.2` at every published version — hence the whole family at 7.18.3. `@shopify/polaris@13.9.5` peers `react ^18.0.0` and no React-19 Polaris exists on any dist-tag; a single-React workspace hard-fails `npm install` with ERESOLVE, and the split (merchant 18 / buyer 19) was proven to resolve cleanly. `jsdom@30` declares `node ^22.22.2 || ^24.15.0 || >=26.0.0`, excluding the installed Node 25.9.0; `jsdom@29.1.1` runs on both. `.npmrc` sets `save-exact=true`, `fund=false`, `audit=false`, and **does not** set `engine-strict` (which would hard-fail `npm ci` on this host). `.nvmrc` = `25.9.0` (records what the lockfile was resolved on; D8 verified vitest 4.1.11 and tsc run correctly there). `.python-version` = `3.12.13`. Both `uv.lock` and `package-lock.json` are committed.

### 3.6 `/vitest.config.ts`

```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
export default defineConfig({
  test: {
    // vitest 4 removed vitest.workspace.ts — use test.projects.
    // Do NOT set passWithNoTests here: it would let T-054's filtered verify pass with zero tests.
    projects: [
      { test: { name: "contracts", root: "./packages/contracts", environment: "node",
                include: ["tests/**/*.test.ts","src/**/*.test.ts"] } },
      { test: { name: "pixel", root: "./pixel", environment: "node",
                include: ["tests/**/*.test.ts","src/**/*.test.ts"] } },
      { plugins: [react()], test: { name: "merchant", root: "./apps/merchant", environment: "jsdom",
                setupFiles: ["./app/test-setup.ts"], include: ["app/**/*.test.{ts,tsx}"],
                exclude: ["**/node_modules/**","**/build/**","**/.react-router/**"] } },
      { plugins: [react()], test: { name: "buyer", root: "./apps/buyer", environment: "jsdom",
                setupFiles: ["./app/test-setup.ts"], include: ["app/**/*.test.{ts,tsx}"],
                exclude: ["**/node_modules/**","**/.next/**"] } },
    ],
  },
});
```
All eight ticket filters (`packages/contracts`, `apps/merchant`, `pixel`, `apps/merchant/app/dashboard`, `apps/buyer`, `apps/buyer/app/{intent,shortlist,feedback}`) work verbatim against this config. `apps/{merchant,buyer}/app/test-setup.ts` contains `import '@testing-library/jest-dom/vitest'`. Buyer components carrying a vitest render test are `'use client'` with data fetching in an untested server parent — vitest cannot render async Server Components.

### 3.7 `/tsconfig.json`, `/tsconfig.base.json`, `/eslint.config.js`

```json
// tsconfig.json (solution file)
{ "files": [], "references": [
  { "path": "./packages/contracts" }, { "path": "./pixel" },
  { "path": "./apps/merchant" }, { "path": "./apps/buyer" } ] }
```
```json
// tsconfig.base.json
{ "compilerOptions": {
  "target": "ES2022", "lib": ["ES2022","DOM","DOM.Iterable"],
  "module": "ESNext", "moduleResolution": "bundler",
  "strict": true, "noUncheckedIndexedAccess": true, "skipLibCheck": true,
  "esModuleInterop": true, "resolveJsonModule": true, "isolatedModules": true,
  "jsx": "react-jsx", "composite": true, "declaration": true,
  "types": ["vitest/globals"] },
  "exclude": ["**/node_modules", ".swarm-loop"] }
```
```js
// eslint.config.js (flat)
import js from '@eslint/js'
import tseslint from 'typescript-eslint'
export default [
  { ignores: ['**/node_modules/**','**/dist/**','**/build/**','**/.next/**',
              '**/.react-router/**','.venv/**','.swarm-loop/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
]
```
The `.swarm-loop/**` ignore is mandatory: worktrees live at `.swarm-loop/worktrees/<id>` **inside** the repo, so without it the root eslint/tsc/vitest run would lint, typecheck and **execute** sibling agents' half-finished code. (pytest and ruff are already safe — pytest's default `norecursedirs` excludes `.*`, and ruff respects `.gitignore` — but the explicit `norecursedirs` above preserves the `.*` entry.)

### 3.8 `/Makefile` (GNU Make 3.81-safe: one line per recipe; `.ONESHELL` and `.SHELLFLAGS` are silently ignored on this host)

```make
SHELL := /bin/bash
.PHONY: bootstrap preflight deps-up deps-down check verify lint types test-py test-ts demo-seed e2e-live clean

bootstrap:  ; @./scripts/bootstrap.sh
preflight:  ; @./scripts/preflight.sh
deps-up:    ; @docker compose up -d --wait postgres neo4j redis
deps-down:  ; @docker compose down -v
check:      ; @./scripts/verify.sh check
verify:     ; @./scripts/verify.sh all
lint:       ; @./scripts/verify.sh lint
types:      ; @./scripts/verify.sh types
test-py:    ; @./scripts/verify.sh pytest
test-ts:    ; @./scripts/verify.sh vitest
demo-seed:  ; @./.venv/bin/python -m fixtures.seed --category "$(SEED_CATEGORY)"
e2e-live:   ; @bash docs/demo/e2e_live.sh
clean:      ; @rm -rf .pytest_cache .mypy_cache .ruff_cache
```
`demo-seed` and `e2e-live` exist from day one and delegate into `fixtures/seed/` (T-080) and `docs/demo/` (T-085), so neither ticket ever edits the Makefile — closing B6(i). `e2e-live` is never a prerequisite of `verify` (DESIGN.md:100: "never a ticket gate"), and T-085's lint greps the Makefile for target names rather than invoking them.

### 3.9 `/scripts/verify.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
export PATH="$ROOT/.venv/bin:$ROOT/node_modules/.bin:$PATH"
[ -x "$ROOT/.venv/bin/pytest" ] || { echo "FATAL: run 'make bootstrap' (uv sync --frozen)" >&2; exit 2; }
[ -x "$ROOT/node_modules/.bin/vitest" ] || { echo "FATAL: run 'npm ci --prefer-offline'" >&2; exit 2; }
python - <<'PY'
import sys, pytest
assert sys.version_info[:2] == (3,12), f"wrong interpreter: {sys.executable} {sys.version}"
assert tuple(int(x) for x in pytest.__version__.split('.')[:2]) >= (8,2), pytest.__version__
PY

STEP="${1:-all}"
run_pytest() {                      # exit 5 (nothing collected) tolerated ONLY here
  set +e; pytest "$@"; rc=$?; set -e
  if [ $rc -eq 5 ]; then echo "WARNING: pytest collected 0 tests in: $*" >&2; return 0; fi
  return $rc
}
if [ "$STEP" = all ] || [ "$STEP" = check ] || [ "$STEP" = lint ]; then
  ruff check .; ruff format --check .; lint-imports; npx --no-install eslint .
  ! git ls-files | xargs grep -l 'FLUSHALL' 2>/dev/null | grep -q .   # D39
fi
if [ "$STEP" = all ] || [ "$STEP" = check ] || [ "$STEP" = types ]; then
  mypy; npx --no-install tsc -b --pretty false
fi
if [ "$STEP" = all ] || [ "$STEP" = pytest ]; then run_pytest -q -m "not needs_model"; fi
if [ "$STEP" = check ]; then run_pytest -q -m "not needs_model and not docker and not slow"; fi
if [ "$STEP" = all ] || [ "$STEP" = vitest ]; then npx --no-install vitest run --passWithNoTests; fi
if [ "$STEP" = all ] || [ "$STEP" = check ]; then python scripts/check_verify_contracts.py; fi
echo "OK: $STEP"
```
`make check` (the per-ticket gate, B9) skips `@pytest.mark.docker` and `@pytest.mark.slow`; `make verify` (the per-wave integration gate) runs everything with the datastore stack up.

### 3.10 `/scripts/check_verify_contracts.py` (guards the fixed verify contracts)

Fails the build when: any `pyproject.toml` other than the root contains `[tool.pytest.ini_options]`, or any `pytest.ini`/`tox.ini`/`setup.cfg` pytest section exists (D35); any tracked `*.test.*` / `*.spec.*` file outside `pixel/` has "pixel" in its path, case-insensitive (D36); the string `packages/protocol` appears anywhere in the source tree (D1); any ticket whose status is CLOSED has a verify path that does not exist. It also prints (non-fatally) which not-yet-created verify paths are still missing, as a scaffold sanity check.

### 3.11 `/scripts/bootstrap.sh` and `/scripts/preflight.sh`

`bootstrap.sh`: `uv sync --frozen` then `npm ci --prefer-offline` **in the current directory** (so each worktree gets its own venv and `node_modules` — a shared root venv with editable installs would make every worker test the main tree's source). `preflight.sh`: fails with a readable message if any of the compose ports is occupied, if `docker info` reports less than 4 GiB free, if `PROXYSHOP_WORKER` is unset, or if `node -p "process.versions.node.split('.')[0]"` is below 22 (the host has a stale `/usr/local/bin/node` v18.18.0 that wins under a default PATH).

### 3.12 `/docker-compose.yml`

```yaml
name: proxyshop        # MANDATORY: without it a worktree named T-012 spawns its own stack
include:
  - services/shopify-stub/compose.yaml
  - services/ingest/compose.yaml
  - services/sim/compose.yaml
  - apps/exchange/compose.yaml
  - apps/trust/compose.yaml
  - apps/merchant/compose.yaml
  - apps/buyer/compose.yaml
  - apps/seller-reference/compose.yaml
  - packages/store-agent/compose.yaml
services:
  postgres:
    image: postgres:16-alpine        # already cached locally; arm64 native
    pull_policy: missing
    command: ["postgres","-c","shared_buffers=256MB","-c","max_connections=200",
              "-c","fsync=off","-c","synchronous_commit=off","-c","full_page_writes=off"]
    environment: { POSTGRES_USER: proxyshop, POSTGRES_PASSWORD: proxyshop_dev_pw,
                   POSTGRES_DB: proxyshop_template }
    ports: ["${PG_PORT:-5432}:5432"]
    volumes: [ "pgdata:/var/lib/postgresql/data",
               "./db/init:/docker-entrypoint-initdb.d:ro" ]   # 00-roles.sql runs ONCE (D38)
    healthcheck: { test: ["CMD-SHELL","pg_isready -U proxyshop -d proxyshop_template"],
                   interval: 5s, timeout: 5s, retries: 12, start_period: 20s }
    stop_grace_period: 30s
    mem_limit: 768m
  neo4j:
    image: neo4j:5.26-community      # 5.26.30 verified for D6's 1024-d cosine index
    pull_policy: missing
    environment:
      NEO4J_AUTH: neo4j/proxyshop_dev_pw            # 5.x rejects passwords under 8 chars
      NEO4J_server_memory_heap_max__size: 1g        # cap the ceiling…
      NEO4J_server_memory_pagecache_size: 512m      # …but do NOT set heap_initial:
                                                    # the image runs -XX:+AlwaysPreTouch, so a 1G
                                                    # initial heap turns a lazy ceiling into a hard
                                                    # ~1.5G RSS (measured idle default: ~450 MiB)
      NEO4J_db_tx__log_rotation_retention__policy: "100M size"
    ports: ["${NEO4J_HTTP_PORT:-7474}:7474","${NEO4J_BOLT_PORT:-7687}:7687"]
    volumes: [ "neo4jdata:/data" ]
    healthcheck: { test: ["CMD-SHELL","wget -qO- http://localhost:7474 >/dev/null 2>&1 || exit 1"],
                   interval: 5s, timeout: 5s, retries: 20, start_period: 20s }
    stop_grace_period: 30s          # REQUIRED: every container on this host is created with
                                    # StopTimeout=1, which SIGKILLs Neo4j mid-checkpoint
    mem_limit: 2g
  redis:
    image: redis:7-alpine
    pull_policy: missing
    command: ["redis-server","--save","","--appendonly","no",
              "--maxmemory","256mb","--maxmemory-policy","noeviction","--databases","16"]
    ports: ["${REDIS_PORT:-6379}:6379"]
    healthcheck: { test: ["CMD","redis-cli","ping"], interval: 5s, timeout: 3s, retries: 10 }
    mem_limit: 384m
volumes: { pgdata: , neo4jdata: }
```
Total capped footprint 3.15 GiB against the ~5.1 GiB free in the 7.75 GiB VM (D10). Each per-service fragment is pre-created by T-000 as a valid `services: {}` stub inside its owner's glob, so the `include:` list is static from day one and no worker ever edits the root file (B6(ii)); the `shopify-stub` service carries `profiles: ["e2e"]` and is used only by T-081/T-082/T-083/T-084 — unit tickets construct the stub in-process (ASGI transport, port 0), which costs no VM RAM and isolates the pixel-drop-rate knob and the discount-code table.

### 3.13 Shared test scaffolding (all T-000-owned, all frozen)

**Root `conftest.py`:** fails the session if `PROXYSHOP_WORKER` is unset (D38); installs the pytest-socket guard (`--disable-socket --allow-hosts=127.0.0.1,localhost,::1`); provides `pg_admin`, `pg_role(role)`, `neo4j_session`, `redis_client`, `shopify_stub_url` (in-process ASGI), `frozen_clock`, `manual_clock`, `llm_double`, `hash_embedding`; skips `@pytest.mark.docker` with an explicit message when the stack is unreachable — never hangs; deselects `@pytest.mark.needs_model`.
**Per-directory `conftest.py` + `__init__.py`** pre-created for `apps/trust/tests`, `services/ingest/tests`, `apps/exchange/tests`, `packages/store-agent/tests`, `apps/merchant/svc/tests`, `apps/buyer/svc/tests`, `e2e`. Each auto-loads sibling `tests/_fixtures_*.py` via a discovery glob, so a worker adds fixtures in a file **it owns** and never edits a shared conftest. `services/ingest/tests/conftest.py` and `apps/exchange/tests/conftest.py` additionally hold the session-scoped Neo4j flock (D37).
**Per-service `src/<pkg>/main.py`:** globs `src/<pkg>/*/routes.py`, imports each module and `include_router`s its exported `router`; a smoke test asserts the app starts with zero routers. Created for exchange, trust, merchant/svc, buyer/svc, ingest, and the store-agent runner. Every pinned DESIGN route then lands in its own feature ticket's already-owned subdirectory, and `main.py` is never edited by a worker (B6(iii)).
**`packages/contracts/generated/{python,ts}/`** are the committed codegen output paths — never `dist/` or `build/`, which `.gitignore` excludes and which therefore would not survive into a worktree.

### 3.14 `/.env.example`

```
PROXYSHOP_WORKER=1
PROXYSHOP_PG_DSN_ADMIN=postgresql://proxyshop:proxyshop_dev_pw@localhost:5432/proxyshop_w1
PROXYSHOP_PG_DSN_EXCHANGE=postgresql://exchange:x@localhost:5432/proxyshop_w1
PROXYSHOP_PG_DSN_TRUST_RW=postgresql://trust_rw:x@localhost:5432/proxyshop_w1
PROXYSHOP_PG_DSN_VAULT=postgresql://buyer_vault:x@localhost:5432/proxyshop_w1
PROXYSHOP_PG_DSN_APP=postgresql://app:x@localhost:5432/proxyshop_w1
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=proxyshop_dev_pw
REDIS_URL=redis://localhost:6379/1
SHOPIFY_STUB_URL=http://localhost:8787
EMBEDDING_PROVIDER=hash
LLM_PROVIDER=double
BUYER_MODEL=claude-sonnet-4-5
STORE_AGENT_MODEL=claude-sonnet-4-5
INTERVIEW_MODEL=claude-opus-4-1
EXTRACT_MODEL=claude-haiku-4-5
CHECKOUT_MODE=redirect
SEED_CATEGORY=coffee
RANKING_WEIGHTS=w_m=0.35,w_e=0.20,w_t=0.20,w_v=0.15,w_d=0.10
```

### 3.15 Dispatcher environment (set once, per worktree, before any agent starts)

```
COMPOSE_PROJECT_NAME=proxyshop
COMPOSE_FILE=<abs path to repo root>/docker-compose.yml
PROXYSHOP_WORKER=<1..6>
ANTHROPIC_API_KEY=sk-ant-DOUBLE-DO-NOT-USE
HF_HUB_OFFLINE=1 ; TRANSFORMERS_OFFLINE=1
CI=1 ; npm_config_yes=true ; npm_config_audit=false ; npm_config_fund=false ; npm_config_progress=false
GIT_TERMINAL_PROMPT=0 ; DEBIAN_FRONTEND=noninteractive
NODE_OPTIONS=--max-old-space-size=1536
UV_LINK_MODE=hardlink
```

---

## 4. FILE OWNERSHIP MAP — narrowed, per ticket

Rules: a ticket owns its `src` glob verbatim (minus child carve-outs), **plus only the test files its own verify command names**, plus a filename-prefix reservation `test_<topic>*.py` and a data dir `tests/data/<topic>/**`. Everything in §3 is orchestrator-owned and appears in no ticket's list. "RO" = the path exists and must not be modified.

| Ticket | Files you own | RO / notes |
|---|---|---|
| **T-000** | Everything in §3 (tree, all root config, Makefile, compose root + fragment stubs, all `conftest.py` + `tests/__init__.py`, all `main.py`, `.importlinter`, `scripts/**`, `db/init/**`, `.env.example`, lockfiles) | Frozen after close; no worker edits any of it |
| **T-010** | `packages/contracts/**` incl. `generated/{python,ts}/**` | Sole owner; read-only for all 43 others (DESIGN.md:45, EXECUTION rule 5) |
| **T-011** | `db/migrations/**`; `apps/trust/src/trust/ledger/**`; `apps/trust/tests/test_schema_grants*.py`, `test_ledger_chain*.py`, `test_import_wall*.py`; `apps/trust/tests/data/ledger/**`; `.importlinter` contract **additions** only | Sole owner of migrations. NOT `apps/trust/tests/**` |
| **T-012** | `services/ingest/src/ingest/{graph,embeddings}/**`; `.../tests/test_graph*.py`, `test_embeddings*.py`; `.../tests/data/graph/**` | Neo4j writer — scheduler-serialized (D37) |
| **T-013** | `services/shopify-stub/**` (incl. `fixtures/recorded/**` and its `compose.yaml`) | Sole owner. Import package = `shopify_stub` |
| **T-014** | `packages/llm/**` (incl. `fixtures/recorded/**`) | Sole owner |
| **T-020** | `services/ingest/src/ingest/adapters/base.py`, `adapters/signed_fetch/**`, `adapters/http/**`; `.../tests/test_signed_fetch*.py`, `test_ssrf*.py`, `test_robots*.py`, `test_adapter_interface*.py`; `.../tests/data/storefront/**` | Owns the `CatalogAdapter` interface. Neo4j writer |
| **T-021** | `services/ingest/src/ingest/extraction/**`; `.../tests/test_extraction*.py`; `fixtures/pages/**` | RO: `adapters/**`, `packages/llm/**`. Neo4j writer |
| **T-022** | `services/ingest/src/ingest/er/**`; `.../tests/test_entity_resolution*.py`; `fixtures/er/**` | Neo4j writer |
| **T-023** | `services/ingest/src/ingest/adapters/catalog_mcp/**`; `.../tests/test_catalog_mcp*.py`; `fixtures/mcp/**` | RO: `adapters/base.py`, `adapters/signed_fetch/**` (T-020's verified behaviour — escalate per rule 6). Neo4j writer |
| **T-024** | `services/ingest/src/ingest/scheduler/**` incl. `scheduler/routes.py` (`POST /refresh/{store_id}`); `.../tests/test_refresh*.py` | Neo4j writer |
| **T-030** | `apps/exchange/src/exchange/auction/**` incl. `auction/routes.py` (`POST /auctions`); `.../tests/test_auction*.py`; `.../tests/data/auction/**` | Ledger writes go via trust `POST /events` (D26) |
| **T-031** | `apps/exchange/src/exchange/retrieval/**`; `.../tests/test_retrieval*.py` | Owns `intent_match` incl. `Intent.preferences` (D14). Neo4j reader — serialized with graph writers |
| **T-032** | `apps/exchange/src/exchange/ranking/**` incl. `ranking/routes.py` (`GET /auctions/{id}/shortlist`); `.../tests/test_ranking*.py` | RO: `fixtures/golden/**`, `fixtures/manifest/**`. Consumes the generated `TrustSnapshot` type behind a port with a deterministic double |
| **T-033** | `apps/exchange/src/exchange/accept/**` incl. `accept/routes.py` (`POST /auctions/{id}/accept`); `.../tests/test_accept*.py` | Owns the frozen kind-sequence golden (D22) |
| **T-034** | `apps/exchange/src/exchange/policy/**` incl. `policy/routes.py` (`POST /internal/outcomes`); `.../tests/test_bandit*.py` | Posteriors in Redis (D26) |
| **T-035** | `apps/exchange/src/exchange/reports/**`; `.../tests/test_loss_reports*.py` | |
| **T-040** | `packages/store-agent/src/store_agent/hooks/**`; `.../tests/test_hooks*.py`; `fixtures/envelopes/**` | NOT `packages/store-agent/tests/**` |
| **T-041** | `packages/store-agent/src/store_agent/runtime/**` incl. the runner app and `POST /bid`; `.../tests/test_runtime*.py` | |
| **T-042** | `packages/store-agent/src/store_agent/learning/**`; `.../tests/test_learning*.py` | Name is fixed by its verify; the collision is solved by config (B4), not renaming |
| **T-043** | `packages/store-agent/src/store_agent/modes/**`; `.../tests/test_shadow_trust*.py` | Consumes the T-053 activation state; no merchant route needed |
| **T-044** | `packages/store-agent/src/store_agent/external/**`; `.../tests/test_external_bids*.py` | RO: `runtime/**` (T-041's `/bid` handler) |
| **T-045** | `apps/seller-reference/**` | RO: `fixtures/manifest/**`, `fixtures/personas/**` (T-080 ground truth). Import package = `seller_reference` |
| **T-050** | `apps/merchant/app/**` EXCEPT `app/dashboard/**`; `apps/merchant/svc/src/merchant_svc/**` EXCEPT `{collector,codes,onboarding,envelope}/**`; `apps/merchant/svc/tests/test_install*.py`; `apps/merchant/compose.yaml` | Install test must **not** be named `webPixelCreate.test.ts` (D36) |
| **T-051** | `pixel/**`; `apps/merchant/svc/src/merchant_svc/collector/**` incl. `collector/routes.py` (`POST /pixel/collect`); `.../svc/tests/test_collector*.py` | Join keys are the generated type (D23) |
| **T-052** | `apps/merchant/svc/src/merchant_svc/codes/**` incl. `codes/routes.py` (`POST /codes`); `.../svc/tests/test_codes*.py` | Permalink template is D21 |
| **T-053** | `apps/merchant/svc/src/merchant_svc/onboarding/**`; `apps/merchant/svc/src/merchant_svc/envelope/**` incl. routes for `GET/PUT /stores/{id}/envelope` and `POST /stores/{id}/kill` (**scope addition**, D32); `.../svc/tests/test_onboarding*.py`, `test_envelope*.py`; `fixtures/interviews/**` | |
| **T-054** | `apps/merchant/app/dashboard/**` incl. its `*.test.tsx` | RO: `apps/merchant/svc/**` (non_goal "No new APIs" stands) |
| **T-060** | `apps/trust/src/trust/events/**` incl. `events/routes.py` (`POST /events`); `.../tests/test_events*.py` | RO: `trust/ledger/**` (T-011) |
| **T-061** | `apps/trust/src/trust/reconcile/**`; `.../tests/test_reconciliation*.py` | Also emits the D31 outcome POST |
| **T-062** | `apps/trust/src/trust/scoring/**` incl. `scoring/routes.py` (`GET /stores/{id}/trust` handler wiring); `.../tests/test_scoring*.py` | RO: `fixtures/manifest/**`, `fixtures/golden/**` |
| **T-063** | `apps/trust/src/trust/feedback/**` incl. `feedback/routes.py` (`POST /feedback/{order_ref}`); `.../tests/test_feedback_push*.py` | |
| **T-064** | `apps/trust/src/trust/snapshot/**` incl. `snapshot/routes.py` (`GET /snapshot`, `GET /stores/{id}/trust`) and the exchange snapshot client; `.../tests/test_snapshot*.py` | |
| **T-065** | `packages/verification/**`; `apps/trust/src/trust/verification/**`; `apps/trust/tests/test_verification*.py` | **`fixtures/golden/**` REMOVED from scope — RO, owned by T-080 (B7).** Import package = `claim_verification` (avoids shadowing `apps/trust/src/trust/verification`) |
| **T-070** | `apps/buyer/app/**` EXCEPT `app/{intent,shortlist,feedback}/**`; `apps/buyer/svc/src/buyer_svc/**` EXCEPT `{intent,accept,feedback}/**`; `apps/buyer/svc/tests/test_auth_vault*.py` | |
| **T-071** | `apps/buyer/svc/src/buyer_svc/intent/**`; `apps/buyer/app/intent/**`; `.../svc/tests/test_intent*.py`; `fixtures/dialogues/**` | |
| **T-072** | `apps/buyer/app/shortlist/**`; `apps/buyer/svc/src/buyer_svc/accept/**`; `.../svc/tests/test_accept_flow*.py` | Renders exchange-supplied `provenance_labels` (D29) |
| **T-073** | `apps/buyer/app/feedback/**`; `apps/buyer/svc/src/buyer_svc/feedback/**` (**scope addition** — acceptance 2 requires it); `.../svc/tests/test_feedback.py` | |
| **T-080** | `fixtures/manifest/**`; `fixtures/approval/**`; `fixtures/seed/**`; `fixtures/catalog/**`; `fixtures/personas/**`; `fixtures/golden/**`; `fixtures/tests/**` | **NOT** `fixtures/{pages,er,mcp,envelopes,interviews,dialogues}/**` — those belong to T-021/022/023/040/053/071 and are pre-created empty by T-000. RO: Makefile |
| **T-081** | `services/sim/**`; `services/sim/compose.yaml` | RO: `fixtures/manifest/**` |
| **T-082** | `e2e/test_s1_flow.py`; `e2e/support/s1/**` | NOT `e2e/**`; NOT `e2e/conftest.py` |
| **T-083** | `e2e/test_learning.py`; `e2e/support/learning/**` | |
| **T-084** | `e2e/test_dishonest.py`; `e2e/support/dishonest/**` | RO: `fixtures/manifest/**` (non_goal "No manifest edits") |
| **T-085** | `docs/demo/**` (incl. `e2e_live.sh`); `docs/tests/**` | RO: Makefile — `make e2e-live` is pre-created and delegates to `docs/demo/e2e_live.sh` |

---

## 5. SCHEDULING CONSTRAINTS

**5.1 Hard sets that must never be in flight together**

| # | Set | Reason |
|---|---|---|
| **SC-1** | At most **one** of {T-012, T-020, T-021, T-022, T-023, T-024, T-031} | All write or index-query the single Neo4j database. Community Edition has exactly one database (D4, verified), so file-scope disjointness does not imply state disjointness. Backed by an flock (D37) as a safety net, but the scheduler must enforce it — the flock trades a phantom failure for a serialized wait, and waiting workers hold worktrees. |
| **SC-2** | T-011 alone among migration-running tickets | `CREATE ROLE` is cluster-global (verified: `pg_authid.relisshared = true`). T-011's wave-1 peers (T-012/013/014) touch no Postgres, so this costs nothing in practice; it becomes a real constraint only if a later ticket is ever given migration scope. |
| **SC-3** | Global cap: **6 concurrent workers** | 10 CPUs; host RAM 32 GiB with ~8 GiB per agent worst case (pytest + tsc + vitest); Docker VM budget is a separate ~5.1 GiB and holds only the containers. Six is the point where CPU contention starts costing more than the added parallelism. |
| **SC-4** | The full `make verify` runs **once per merged wave**, serialized, on the integration branch | B9. Per-ticket close uses `make check`. |
| **SC-5** | T-080 must be dispatched in the **first wave in which it is ready** (wave 2), ahead of higher-depth work | It is a human approval gate that transitively blocks 15 tickets once B7's edge lands. Scheduled by depth it is reached on time; scheduled by any other heuristic the swarm runs dry waiting on a signature. |

**5.2 Serialized-by-dependency, therefore NOT scheduling constraints** (listed so the orchestrator does not spend budget on them): T-000 ⊃ everything; T-050 ⊃ {T-051,T-052,T-053,T-054}; T-070 ⊃ {T-071,T-072,T-073}; T-071→T-072→T-073; T-020→T-023 (adapters dir — read-only carve-out is a packet instruction, not a wave constraint); T-011→T-060→{T-061..T-065}; T-012→T-020→{T-021..T-024}; T-030→{T-031..T-035}; T-040→T-041→{T-042,T-043,T-044}; T-083→T-084.

**5.3 Corrected `parallel_safe` verdict — every ticket where it differs from `tickets.json`**

| Ticket | tickets.json | Corrected | Why |
|---|---|---|---|
| T-010 | false | **true** | `packages/contracts/**` is sole-owned; zero concurrent overlap |
| T-020 | false | **true**, subject to SC-1 | Narrowed test ownership removes the `services/ingest/tests/**` overlap |
| T-022 | true | **true**, subject to SC-1 | File-level disjoint after narrowing, but a Neo4j writer |
| T-023 | true | **true**, subject to SC-1 | Same |
| T-030 | false | **true** | Sole owner of `src/auction/**` after narrowing |
| T-031 | true | **true**, subject to SC-1 | Neo4j reader |
| T-041 | false | **true** | |
| T-050 | false | **true** | `apps/merchant/**` minus child carve-outs; children are all descendants |
| T-054 | false | **true** | `apps/merchant/app/dashboard/**` is sole-owned |
| T-060 | false | **true** | |
| T-070 | false | **true** | |
| T-072 | false | **true** | |
| T-080 | true | **true** — after narrowing to §4's seven paths | As written (`fixtures/**`) it collides with seven tickets |
| T-081 | false | **true** | `services/sim/**` is sole-owned |
| T-021, T-024, T-032, T-033, T-034, T-035, T-040, T-042, T-043, T-044, T-045, T-051, T-052, T-053, T-061, T-062, T-063, T-064, T-065, T-071, T-073, T-082, T-083, T-084, T-085 | — | **unchanged / true under narrowed ownership** | The declared value is either already correct or made correct by §4 |

---

## 6. INITIAL FRONTIER AND SCHEDULE SHAPE

**6.1 Ready the instant T-000 closes** (computed: the five tickets whose `depends_on == {T-000}`)

| Ticket | unblocks (transitive) | height | note |
|---|---|---|---|
| T-010 Contracts | **26** | 7 | Highest-leverage in the frontier; carries D12/D23/D24/D25/D27/D29/D31 schema pins — staff it strongest |
| T-013 Shopify stub | 24 | 7 | Gates T-030, T-050, T-080 |
| T-011 Postgres + ledger | 24 | 8 | Also owns the S7 import-lint proof (D34) |
| T-014 LLM client | 23 | 8 | |
| T-012 Neo4j + embeddings | 18 | **9** | **On the critical path.** Only Neo4j writer in this wave, so SC-1 is satisfied automatically |

All five have pairwise-disjoint narrowed ownership. Width 5 is safe as-is and within SC-3.

**6.2 Emergent batch simulation** (dependency-readiness ∧ disjoint narrowed ownership ∧ SC-1 ∧ SC-3 cap of 6). Unit cost per ticket; widths are ticket-hop counts, not wall clock.

```
B1  (1)  T-000
B2  (5)  T-010  T-013  T-011  T-014  T-012*
B3  (6)  T-020* T-060  T-030  T-050  T-080(HUMAN GATE) T-070
B4  (6)  T-021* T-040  T-041  T-052  T-051  T-071          held by SC-1: T-022, T-023, T-031
B5  (6)  T-022* T-053  T-033  T-042  T-043  T-044          held by SC-1: T-023, T-031
B6  (6)  T-023* T-061  T-065  T-045  T-054(partial)…       held by SC-1: T-031, T-024
B7  (6)  T-031* T-062  T-072  T-081  T-035  —
B8  (5)  T-024* T-064  T-063  T-082  T-032
B9  (4)  T-034  T-054  T-073  T-085(blocked)
B10 (1)  T-083
B11 (1)  T-084
B12 (1)  T-085
```
`*` = the wave's single Neo4j slot. Pure dependency depth alone gives 11 waves with widths `1,5,7,9,7,5,4,3,1,1,1`; SC-1 and the width cap stretch it to ~12 by pushing the ingest chain out one hop each. **The graph's depth, not its blast radius, is the binding constraint** — the only place ownership costs anything is the ingest lane.

**6.3 Critical path — length 10 hops, unique** (verified: exactly one path achieves depth+height = 10)

```
T-000 → T-012 → T-020 → T-021 → T-065 → T-062 → T-064 → T-034 → T-083 → T-084 → T-085
```
Every ticket on it is a single point of schedule failure. Staff by height, in this order: **T-012 (9) > T-011 / T-014 / T-020 (8) > T-010 / T-013 / T-021 / T-060 (7) > T-065 / T-030 / T-050 / T-080 (6) > T-062 (5) > T-064 (4) > T-034 (3)**. Note that T-065 and T-062 sit mid-spine **and** are the two tickets entangled with the human gate — the two risks compound at exactly the same point.

**6.4 Where parallelism pinches**

- **B1** — T-000 alone. Unavoidable; it is the sole root of 43 tickets, and every §3 artifact must exist before anything else runs.
- **B4–B8, ingest lane** — SC-1 serializes T-020→T-021→T-022→T-023→T-024 plus T-031 into one Neo4j slot. This is the single largest structural cost and is unavoidable on Community Edition.
- **B9–B12** — a strict 3-long chain T-083 → T-084 → T-085 with 5 idle worker slots, preceded by T-081→T-083, i.e. a real 4-long serial tail.
- **Filler work for the tail:** the five non-blocking sinks T-022, T-024, T-045, T-054, T-073 block nothing at all and can be deferred into B9–B12 to keep agents busy — but note T-054 and T-073 are themselves depth-7 and cannot be pulled forward.

**6.5 The T-080 human gate — full transitively blocked set**

`EXECUTION.md:20-21`: "T-080's manifest approval is a human gate — do not fabricate the approval artifact."

- **As tickets.json stands (12 blocked):** T-034, T-045, T-054, T-062, T-063, T-064, T-073, T-081, T-082, T-083, T-084, T-085.
- **After B7's `T-080 → T-065` edge (15 blocked):** the twelve above plus **T-032, T-035, T-065**.
- **Buildable without approval:** 31 today, **28** after the edge — T-000, T-010, T-011, T-012, T-013, T-014, T-020, T-021, T-022, T-023, T-024, T-030, T-031, T-033, T-040, T-041, T-042, T-043, T-044, T-050, T-051, T-052, T-053, T-060, T-061, T-070, T-071, T-072.
- **Measured drain point:** with T-080 withheld, cumulative completion by wave is 1, 6, 12, 21, 28, 30, 31. **By the end of B5 there are only three non-gated tickets left.** Draft the manifest during B2 and put it in front of the human then; if approval is not in hand by the end of B5, the swarm has nothing left to build.
- **Every gated ticket is a proof:** S1 (T-082), S2 (T-084), S4 (T-083), the trust-scoring subtree (T-062/063/064), the exchange bandit (T-034), the dashboard (T-054), and the demo runbook (T-085). The approval is not a formality — it is the run's single highest-priority external event.

---

## 7. PROPOSED METRICS for `goals.json`

All commands run from the repo root and print a bare number as the last stdout line. Every cycle runs **one** suite pass first (`--write-report`, per B2) and all twelve metrics read from it, so measurement costs one acceptance run, not twelve.

Prelude, executed once per measurement cycle:
```
.venv/bin/python .swarm-loop/acceptance/run.py --write-report .swarm-loop/tmp/acc.json
```

| id | name | command | target | dir | tol | traces |
|---|---|---|---|---|---|---|
| **M1** | `acceptance_passing_total` | `.venv/bin/python .swarm-loop/acceptance/run.py --from-report .swarm-loop/tmp/acc.json --count-passing` | = `--total` at freeze | up | 0 | all R1–R19, C1–C11, S1–S8 |
| **M2** | `build_succeeds` | `cd $REPO && if make verify >/dev/null 2>&1; then echo 1; else echo 0; fi` | 1 | up | 0 | T-000; C1, C7; DESIGN#verification-strategy |
| **M3** | `lint_type_error_count` | `cd $REPO && { .venv/bin/ruff check --output-format=concise . 2>/dev/null \| grep -c ':' ; .venv/bin/mypy --no-error-summary . 2>/dev/null \| grep -c ': error:' ; npx --no-install eslint . -f unix 2>/dev/null \| grep -cE 'error\|warning' ; npx --no-install tsc -b --pretty false 2>/dev/null \| grep -c 'error TS' ; .venv/bin/lint-imports >/dev/null 2>&1 \|\| echo 1 ; } \| awk '{s+=$1} END {print s+0}'` | 0 | down | 0 | C1, C3, S7; every ticket |
| **M4** | `release_blockers_passing` | `.venv/bin/python .swarm-loop/acceptance/run.py --from-report .swarm-loop/tmp/acc.json --count-passing --blocker` | **3** | up | 0 | S8; T-032, T-033, T-082 |
| **M5** | `e1_foundation_passing` | `… --count-passing --epic E1` | `--total --epic E1` | up | 0 | C1,C2,C3,C4,C7,C9,C10; R8,R15,R18,R19; S3,S5,S7; T-000,T-010–T-014 |
| **M6** | `e2_ingestion_passing` | `… --epic E2` | `--total --epic E2` | up | 0 | C6, A1, R8; T-020–T-024 |
| **M7** | `e3_exchange_passing` | `… --epic E3` | `--total --epic E3` | up | 0 | R2,R3,R9,R10,R11,R12,R16,R19; A5,A6; S8-2, S8-3; T-030–T-035 |
| **M8** | `e4_storeagent_passing` | `… --epic E4` | `--total --epic E4` | up | 0 | R7,R8,R10,R13,R17,R18; S5,S8; T-040–T-045 |
| **M9** | `e5_merchant_passing` | `… --epic E5` | `--total --epic E5` | up | 0 | R3,R4,R6,R7,R9; C5; T-050–T-054 |
| **M10** | `e6_trust_passing` | `… --epic E6` | `--total --epic E6` | up | 0 | R4,R12,R13,R14,R15,R18,R19; S2,S3,S8; T-060–T-065 |
| **M11** | `e7_buyer_passing` | `… --epic E7` | `--total --epic E7` | up | 0 | R1,R2,R3,R5,R14; T-070–T-073 |
| **M12** | `e8_e2e_passing` | `… --epic E8` | `--total --epic E8` | up | 0 | S1,S2,S4,S6; C9; T-080–T-085 |

Notes on the design. M1 is the headline and equals the sum of M5–M12 plus the `SPEC`-marked cross-cutting tests (which roll into M1 and M4 and get no metric of their own). Per-epic metrics are deliberately **not** blended into one number: a stalled epic must be visible in the same cycle it stalls, and E3/E6/E8 carry every success criterion. All targets are set at freeze from `run.py --total --epic Ex`, so the suite's own composition defines them and no number is guessed. Tolerance is 0 everywhere — these are counts of proven behaviours, not noisy measurements. Baseline at cycle 0 is 0 for every count metric, which is correct here (greenfield); M2 separates "not built yet" from "build broken".

**Suite composition to author before freeze** (B1): one acceptance test per machine-checkable SPEC id, marked with its epic and owning ticket — R1 (T-071/E7), R2 (T-032/E3), R3 (T-033/E3), R4 (T-061/E6), R5 (T-070/E7), R6 (T-050/E5), R7 (T-043/E4), R8 ×2 hosted-reject + external-admit (T-010/E1), R9 (T-054/E5), R10 (T-030/E3), R11 blindness (T-032/E3), R12 (T-062/E6), R13 (T-063/E6), R14 (T-063/E6), R15 (T-060/E6), R16 (T-034/E3), R17 (T-042/E4), R18 (T-065/E6), R19 (T-032/E3); C2 (T-012/E1), C3 grant (T-011/E1), C5 (T-050/E5), C6 (T-020/E2), C9 offline (T-013/E1), C10 SSRF + injection + checkout-domain (T-020/T-065/T-033), C11 kind parity (T-033/E3); S1 (T-082/E8), S2 (T-084/E8), S3 (T-062/E6), S4 (T-083/E8), S5 ×2 (T-010/E1 + T-082/E8), S6 integration half (new E8 test), S7 ×2 grant + import-lint (T-011/E1); plus the three `@pytest.mark.blocker` tests **S8-1** no blacklisted seller eligible (T-032), **S8-2** no contradicted hard constraint wins (T-032), **S8-3** no off-domain checkout URL (T-033). That is ~40 tests; the exact per-epic totals are read off `--total --epic Ex` at freeze and written into `goals.json`.

**NOT machine-checkable — track as backlog notes, never as metrics:**
- **S6's manual half** — "demonstrable on a fresh dev store in one sitting" needs live Shopify credentials, which D3 says do not exist and EXECUTION rule 7 forbids in any verify. Only S6's integration half (mocked transcript → approved envelope → shadow log → activation → submitted bid) is measurable; the demo run is T-085's doc-lint boolean.
- **S1's live half** — "the same flow against seeded dev stores (Bogus Gateway) is the documented demo procedure." Offline half only.
- **C4's local embedding model** — real weights cannot load under C7/C9. The `needs_model`-marked test is deselected from `make verify` and from the suite.
- **C5's Shopify-platform conformance** — the stub asserts parity against hand-authored recordings (D20); genuine platform conformance is unprovable offline by construction (SPEC A4 already adjudicated this trade).
- **C8 "no timelines anywhere in these docs"** — enforceable as a lint but it is a docs property, not a system behaviour. Keep it as T-085 acceptance 3, widened to `SPEC.md`, `DESIGN.md`, `TASKS.md`, `EXECUTION.md` and `docs/**`, and note the one near-hit that must not false-positive: `DESIGN.md:77`'s `pitch_requests(deadline_at)` column name.
- **R9's subjective half** — "shows every bid with its rationale" is rendered, not verified; the render tests cover pane presence and the absence of amount fields, not usefulness.

---

## 8. REQUIREMENT COVERAGE GAPS

Computed over every ticket's `refs` array plus a read of every acceptance string.

| Gap | Evidence | Action |
|---|---|---|
| **S3 — trust-score replay is not covered by any ticket** | S3 is referenced by T-011 and T-060 only. T-011 acc 3 replays a **stream hash**; T-060 acc 3 replays **snapshot hashes** with `non_goals: ["No scoring"]` and `depends_on: ["T-011"]` — no trust score exists at that point in the graph. T-062, the only scoring ticket, refs `["R12","S2",…]` — no R15, no S3, and none of its three criteria mention replay. | Add `R15`,`S3` to T-062's refs and a 4th criterion: "Recomputing all trust scores from the fixture `commerce_events` stream reproduces the persisted `trust_scores` rows byte-identically (score, confidence, score_version, and per-dimension alpha/beta/decayed_at), with the decay evaluated at each observation's recorded `decayed_at` (D16)." Also reword T-060 acc 3 to say "event-stream hash" so it cannot be mistaken for the trust-score assertion. |
| **S7 — the import-lint half has zero tickets** | `grep -i "import.lint" tickets.json` → 0 hits. S7's only ref is T-011, whose acceptance covers the grant test alone. | D34: T-000 ships `.importlinter` + wires `lint-imports`; T-011 gains a criterion and a violating fixture, with the new test file added to its verify string. |
| **S6 — the machine-checkable half is owned by nobody** | S6's only ref is T-085, a docs lint (`pytest docs/tests/test_runbook.py -q`, scope `docs/**`). T-053 (interview→envelope) and T-043 (shadow→activation) each cover half the seam, ref neither S6 nor each other, and live in different packages. | Add **T-086** "Onboarding drives a shadow store to its first real bid": `refs ["S6","R6","R7"]`, `depends_on ["T-043","T-053"]`, scope `["e2e/test_onboarding.py","e2e/support/onboarding/**"]`, verify `pytest e2e/test_onboarding.py -q`. Do **not** hang it on T-054, whose verify is vitest-only and cannot execute a Python transcript fixture. |
| **S5 — the run-wide half is unasserted** | S5's only ref is T-010 (the rejection half). The resolution half appears only in T-041 acc 2, scoped to hosted agents. T-082 has no provenance assertion. | Add S5 to T-082's refs and a criterion: "every claim in every accepted bid resolves to a Source/provenance record in the ledger." Optional but better: add an external/persona bid to the S1 run (requires adding T-044/T-045/T-065 to T-082's deps) so the dual path is exercised end to end. |
| **C11 — no ticket carries it in refs** | Verified: C11 appears in no `refs` array. It is covered inline by T-033's objective and acc 4. | Add `C11` to T-010's refs (D23) — the ticket that freezes the event enum must read the constraint requiring both checkout paths to emit identical kinds. |
| **C8 — no ticket carries it in refs** | Same scan. Covered only by T-085 acc 3, scoped to `docs/**`. | Add `C8` to T-085's refs and widen the lint to the five planning docs. |
| **A3 — no ticket carries it in refs** | Same scan; discharged in practice via T-080's `SPEC#adversarial-review-resolved` ref. | Discharged by B7's edge. Add `A3` to T-080's refs so a refs-derived matrix reports it. |
| **C1/C7 — "separate deployables under docker-compose" is unproven** | T-000's acc 2 covers only the three datastores; no ticket adds an app service. | §3.12's `include:` list + per-owner fragments. Each owning ticket gains a one-line acceptance addition: "fills its own `compose.yaml` fragment with a buildable stanza so `docker compose config` stays valid." |
| **C4 — no real embedding provider exists** | T-012's objective names only `EmbeddingProvider + HashEmbedding double`; `grep bge tickets.json` → 0. Everything passes on hash vectors, so the demo would run retrieval on noise. | D18: T-012 gains a 4th criterion for `LocalBgeEmbedding` selectable by config with a `needs_model`-marked test; T-085's runbook sets `EMBEDDING_PROVIDER=local_bge`. Score C4 as **partial** until then. |
| **R9 — the dashboard's trust breakdown and versioned envelope editor have no named assertion** | T-054 acc 1 is "Each pane renders from fixture APIs" — unfalsifiable at the requirement level. | Split acc 1 into named panes: bid log with rationale + per-bid verification statuses; trust pane showing all five dimensions plus one event payload; envelope editor producing a new version and rendering history. Raises T-054 to 5 criteria and the total to 135 — **apply before freezing any criterion-count target.** |
| **`POST /internal/outcomes` has no owner on either end** | DESIGN.md:64 pins it; no ticket's objective, acceptance or scope mentions it; T-034 has no dependency that supplies outcomes. R16's loop is therefore unwired. | D31. |
| **`POST /stores/{id}/kill`, `GET/PUT /stores/{id}/envelope`, `GET /stores/{id}/trust` have no owner** | DESIGN.md:66-67 pins them; no ticket claims them; T-054 acc 2 depends on the kill route while its non_goal forbids new APIs. | D32. |
| **`evals/` and DESIGN's eval-gate list have no home** | DESIGN.md:102 lists nine eval-gate cases; `evals/` is scoped by no ticket. Five of the nine (verbosity gaming, duplicate pitches, timeouts, expired offers, blacklist expiry) appear in no ticket's golden set. | Fold into T-080's golden set — extend its objective to enumerate all nine. Do not create `evals/`. |

---

## 9. PLAN DIVERGENCES to report at the approval checkpoint

Everything below changes the doc set as written. Grouped by cost to the user.

**A. Ticket-graph edits (7 edges, 3 scope changes, 1 new ticket).**
1. `T-065.depends_on += T-080`; **remove `fixtures/golden/**` from T-065's scope**. Closes the A3 circularity (B7). Cost: gated set 12 → 15, buildable-meanwhile 31 → 28.
2. `T-082.depends_on += T-032, T-041, T-062`. T-082's own acceptance asserts hosted bids, blacklist eligibility and the full LedgerEvent set, none of which its current dependencies guarantee. Verified free: T-082 is already depth 6→7 and the 10-hop critical path is unchanged.
3. `T-050.depends_on += T-010`. The merchant app emits contract-typed `LedgerEvent`s; T-010 is depth 1, so this is free.
4. `T-083.depends_on += T-061` (D31's outcome producer).
5. `T-080.scope` narrowed from `["fixtures/**"]` to the seven paths in §4 — it currently strictly contains seven other tickets' fixture directories while marked `parallel_safe`.
6. `T-053.scope += apps/merchant/svc/src/merchant_svc/envelope/**` (D32).
7. `T-073.scope += apps/buyer/svc/src/buyer_svc/feedback/**` — its acceptance 2 ("Submission lands as feedback LedgerEvent") is unreachable from a frontend-only scope.
8. **New T-086** for S6's integration half (§8).
9. Explicitly **NOT** adding `T-064 → T-032`: it would serialize the whole exchange lane behind the human gate for no verification gain, since D25 pins the TrustSnapshot shape in `packages/contracts` and T-032 consumes the generated type behind a port with a deterministic double. Instead, add `DESIGN#interfaces-contracts-between-tickets` to T-032's refs so its agent actually loads that shape (EXECUTION rule 1 restricts it to the sections it refs).

**B. Acceptance-text edits.**
- T-000 acc 3 → §3.1's tree (B5).
- T-082 acc 1 → the exact per-kind multiset table (D33). As written it is unsatisfiable against T-082's own acc 2.
- T-060 acc 3 → "event-stream hash", to stop it reading as the S3 trust-score assertion.
- T-054 acc 1 → split into named panes; acc 2 reworded to what its vitest-only verify can actually check ("posts to `POST /stores/{id}/kill` and reflects the returned activation state"), with the cross-service half moved to T-086 or T-043.
- T-062 += the S3 replay criterion; T-011 += the import-lint criterion and the second-database migration criterion; T-012 += the `LocalBgeEmbedding` criterion.
- T-013 acc 1 "recorded real-API shapes" → clarified per D20, so no worker blocks waiting for credentials.
- T-085 acc 3's timeline lint widened to the five planning docs.

**C. Refs additions** (so a refs-derived coverage matrix is honest): C11→T-010; C8+A3→T-085/T-080; R15+S3→T-062; S5→T-082; C9→T-012, T-021, T-022, T-014; `DESIGN#interfaces-contracts-between-tickets`→T-032, T-051, T-052, T-053, T-064; `DESIGN#verification-strategy`→T-000. Also normalize the nine tickets that ref `DESIGN#decisions--rationale` — the real heading slug is `decisions--rationale-do-not-improve-away`, and four other DESIGN refs preserve their parenthetical, so this one is inconsistent.

**D. Doc-set corrections.** `DESIGN.md:45` still says "All cross-service schemas live in `packages/protocol` … treat `packages/protocol` as read-only", contradicting `DESIGN.md:41` — and :45 is the line T-020/T-035/T-065 actually load. Fix it. `DESIGN.md:87`'s three-term formula is superseded by :85 (D11) — mark it. `TASKS.md:149`'s definition of `∥` is false for 10 tickets — correct it to "eligible for concurrent dispatch; ownership is per-file, not per-glob."

**E. EXECUTION.md amendments.** Rule 2 → the `make check` / `make verify` split (B9). Rule 3 → superseded by narrowed-ownership scheduling (B8). New rule: root-level integration files (Makefile, compose root, manifests, lockfiles, all root tool configs, all `conftest.py`, all `main.py`) are orchestrator-owned and never edited by a ticket worker. New rule: exactly one pytest configuration exists, in the root `pyproject.toml` (D35). New rule: every verify runs with the venv and `node_modules/.bin` first on PATH (B3).

**F. Overrides of an existing pinned decision.** D37 narrows **D4**: its verified half (Neo4j Community has one database) stands unchanged; its design half (tenant scoping inside that database) is replaced by scheduler serialization plus an flock, because tenant properties would force composite uniqueness constraints and tenant-parameterised Cypher across seven tickets written by seven different agents — an invasive change that must be right in all seven, diverging from DESIGN's "uniqueness constraints on every stable ID". This is the only existing ruling this report overrides, and it is flagged here deliberately.

**G. Things intake deliberately did NOT change, and why** (so they are not re-raised mid-run): no rewrite of the 44 verify strings (they are the executable contract; the PATH preamble solves it); no rename of `test_learning.py` (config solves it); no `db/migrations/additive/` lane (it would convert the only verified single-writer directory into a shared one — late schema needs escalate per rule 5); no `claim_veracity` sixth trust dimension (DESIGN.md:82 rejects two trust systems); no `buyer_key` HMAC primitive (D30's `app.*` join is cheaper and already permitted); no Node downgrade (D8 verified 25.9.0 works, and no version manager exists on this host); no `packages/ranking`, `packages/observability`, `graph/` or `evals/` directories (no ticket owns them).