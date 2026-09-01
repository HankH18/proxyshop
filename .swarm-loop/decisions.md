# Pinned decisions — quoted into every task packet

These exist to remove ambiguity that two tickets would otherwise resolve differently.
Where the doc set is ambiguous or self-contradictory, **this file is the ruling**, and each
ruling names the evidence it rests on. The doc set still owns *what* to build; this file only
settles *which reading of it is authoritative*.

Workers: follow these exactly. If one of these rulings looks wrong, say so in `CONCERNS` —
do not quietly implement the other reading.

Sections marked **[verified]** were established by running something on this machine, not by
reading a document.

---

## D1 — The schema package is `packages/contracts`, never `packages/protocol`

DESIGN's *Interfaces* section opens "All cross-service schemas live in `packages/protocol`",
and T-000's acceptance criterion 3 repeats "packages/protocol". Both are **stale**. DESIGN's
own merged-layout paragraph supersedes them: "`packages/contracts` is the schema home
(formerly 'protocol')". Every one of the 44 ticket scope globs uses `packages/contracts`, as
does T-010's objective and verify command — and `tickets.json` is the executable source of
truth (TASKS.md says so explicitly).

**Ruling:** the directory is `packages/contracts`. The string `packages/protocol` must not
appear anywhere in the source tree.

## D2 — Python is 3.12, managed by uv; the system 3.9 is never used **[verified]**

`python3` on this machine is 3.9.7. uv has CPython 3.12.13 available. All Python runs —
including every metric command — go through the project virtualenv, so metric commands
reference `.venv/bin/python`, never a bare `python3`.

## D3 — Verification is offline, always (C9) **[verified: no keys present]**

Neither `ANTHROPIC_API_KEY` nor any Shopify credential exists in this environment. Every
ticket's `verify` command must pass with no network, no live LLM, and no live Shopify —
against `services/shopify-stub` and deterministic doubles. Live dev-store runs belong to the
T-085 demo runbook only, never to a ticket gate. A ticket whose verify needs a credential is
a defect in that ticket, to be reported, not worked around.

## D4 — Neo4j Community has exactly ONE database; graph isolation is by tenant, not by database **[verified]**

`CREATE DATABASE worker1` on neo4j:5.26 Community returns `Unsupported administration
command`; `SHOW DATABASES` lists only `neo4j` and `system`. Per-worker Neo4j *databases* are
therefore impossible.

**Ruling:** graph-touching tests isolate by tenant scoping inside the single database, and
the scheduler treats "writes to Neo4j" as a shared surface. Postgres isolates by per-worker
database (`CREATE DATABASE proxyshop_w<n>` — verified working) and Redis by per-worker
logical DB index (16 available — verified).

## D5 — The Postgres grant model that satisfies C3/S7 is proven; reproduce it exactly **[verified]**

Verified live: with `GRANT USAGE ON SCHEMA ledger` only, role `exchange` reads `ledger.*`
successfully; `SELECT * FROM sealed.envelopes` fails with `ERROR: permission denied for
schema sealed`; `vault.*` likewise; `information_schema.tables WHERE table_schema='sealed'`
returns **0 rows** for that role; and `INSERT` into `ledger` is denied, matching DESIGN's
"`exchange` role: no write".

**Ruling:** schema-level `USAGE` grants (never table-level grants into `sealed`) are the
enforcement mechanism for C3. The exchange role is granted `USAGE` on `ledger` and `app`
only, and never on `sealed` or `vault`.

## D6 — Neo4j vector index parameters are fixed and proven **[verified]**

`CREATE VECTOR INDEX product_embedding FOR (p:Product) ON (p.embedding) OPTIONS
{indexConfig: {'vector.dimensions': 1024, 'vector.similarity_function': 'cosine'}}` works on
neo4j:5.26.30 Community, and `db.index.vector.queryNodes` returns correct cosine scores for a
1024-dim vector. Use exactly these parameters (DESIGN C2: 1024 dims, cosine).

## D7 — `npx vitest run <path>` is a path FILTER and isolates correctly; only the root verify passes with no tests **[verified]**

Verified: `vitest run pkgA` ran only pkgA's tests and exited 0 while a sibling package's
failing test was ignored — so every ticket's `npx vitest run <path>` form works as written.
Verified also: a filter matching zero test files exits **1**, and `--passWithNoTests` makes
it exit 0.

**Ruling:** the root `make verify` uses `--passWithNoTests` (so T-000's empty scaffold is
green, per its acceptance criterion 1). Individual ticket verify commands do **not** use it —
a ticket whose tests do not exist yet is correctly red.

## D8 — Pin TypeScript to the 5.x line **[verified: `typescript@latest` is 7.0.2]**

`npm install typescript@latest` now resolves to TypeScript **7.0.2**, the rewritten
Go-based compiler. Next.js, React Router v7 and the `@shopify/*` toolchain are not
established on it, and an unattended run is the wrong place to discover that.

**Ruling:** pin `typescript` to the 5.x line in every `package.json`. Node 25.9.0 itself is
fine — vitest 4.1.11 and tsc both run correctly on it **[verified]**.

## D9 — Bind only ports verified free; this host is busy **[verified]**

Already bound on this machine: `3000, 4011, 5000, 5173, 7000, 8300, 8320, 8765, 8877, 8901,
9300, 54321–54324, 54327, 55432` (Supabase ×11, OpenEMR, MariaDB, keystone-postgres). The
first compose attempt failed on 55432 precisely this way.

**Ruling:** ProxyShop's compose binds a contiguous block verified free at scaffold time, and
records it in `.env.example`. No service hard-codes a default port that collides with the list
above.

## D10 — Memory is the binding constraint, not CPU **[verified]**

The Docker VM is 7.75 GiB and ~2.7 GiB is already committed to unrelated stacks, leaving
**~5.1 GiB**. Every compose service carries an explicit memory limit, and the DB stack is
budgeted to fit well inside that with room for concurrent test suites. A container OOM-killed
mid-wave would surface as an inexplicable test failure, so the limits are set deliberately
rather than left unbounded.

---

## D11 — T-065 does not author the golden set it is graded against **[approved by Hank]**

Intake blocker B7: `T-065`'s scope included `fixtures/golden/**` and its `depends_on` did not
include `T-080` — so the ticket authored the golden set used to grade it, which is precisely
the circularity SPEC §A3 exists to prevent (A3 caught the same trap for the dishonest-store
fixture and ruled that behaviours live in a human-approved manifest).

**Ruling (Hank approved, applied to `tickets.json`):**
- `T-065.depends_on` += `T-080`.
- `fixtures/golden/**` **removed** from `T-065.scope` — a prose note is insufficient, because
  the scope glob is what a worker actually obeys.
- Non-goal added to T-065: the golden set is T-080's human-approved ground truth, read-only.

**Verified after the edit:** the graph is still acyclic, still 44 tickets. The T-080 gate set
grows 12 → **15** (`T-032, T-034, T-035, T-045, T-054, T-062, T-063, T-064, T-065, T-073,
T-081, T-082, T-083, T-084, T-085`), leaving **28** tickets buildable without approval.

This is a deliberate, accepted cost: an uncircular S8 proof is the point of the ticket.
Because the gate now blocks a third of the graph, dispatch T-080 as early as `T-013` allows.

---

# Appended from the adversarial intake report — §2 PINNED DECISIONS

These are the intake report's section 2 rulings (`.swarm-loop/intake-report.md:59-125`), which the
report numbered **D11–D40**. That range collided with the existing, Hank-approved **D11** above, so
the whole block is shifted by one and is numbered **D12–D41** here. The mapping is exact and
order-preserving: intake `Dn` → this file's `D(n+1)`. Every cross-reference inside these entries
points at the number as it appears **in this file**; references to `D1`–`D10` are unchanged, because
those rulings pre-date the report and were not renumbered.

Quote these verbatim into every task packet. Where these and DESIGN/SPEC prose disagree, **these
win**; where these are silent, `tickets.json` wins over prose.

None of D12–D41 carries a **[verified]** header: they are rulings, not measurements. Where the
intake report recorded something it actually ran or measured, that fact is quoted inline in the
entry and attributed there.

## D12 — There is ONE rank formula and it is `DESIGN.md:85`

`rank_score = w_m*intent_match + w_e*verified_claim_ratio + w_t*trust + w_v*price_value +
w_d*delivery_fit − policy_penalties`, features normalized to [0,1]. `DESIGN.md:87`'s three-term
`w_f*fit + w_v*offer_value + w_t*trust` is superseded prose — it predates the reconciliation
amendment and lacks the `verified_claim_ratio` term that T-032's dependency on T-065 exists to
supply. Eligibility filters run first, never as score terms (R19).

## D13 — Rank weights are pinned now, in one versioned config object

`RankingWeights` is defined in `packages/contracts` and loaded from env with these defaults:
`w_m=0.35, w_e=0.20, w_t=0.20, w_v=0.15, w_d=0.10` (sum 1.0). `price_value = clamp((list_price −
total_price)/list_price, 0, 1)` against the bidding store's own list price. `delivery_fit` reads
`Offer.delivery_estimate_days` against `Intent.ship_to`, and is **0.5 (neutral) when absent**, never
0. `policy_penalties = Σ per-kind penalties over open `ledger.policy_events` for that store in the
scoring window`; initial catalogue: `severe_policy_violation = 0.30`. Tie-break, in order: verified
hard-fit count → trust → price → `bid_id`.

## D14 — `verified_claim_ratio` is earned by every bid on the same path; hosted bids get no constructed value

The exchange runs the same T-065 comparator pass over every candidate's claims regardless of
provenance. Hook-provenanced hosted claims are catalog-derived, so they return `verified` and the
ratio lands near 1.0 — earned, not granted. When no `VerificationResult` is available (verification
unavailable or timed out) the term is **0.5 for every path** and an `unverified` warning label is
attached. A constructed 1.0 for hosted bids would make `rank_score` a function of tier and fail
T-032 acceptance 2's blindness test (R11).

## D15 — `Intent.preferences[].weight` feeds `intent_match` only, never the outer published weights

Per-intent preference weights are consumed inside T-031's fit scorer. T-032's published weights are
fixed per environment and identical for every buyer. Two weighting layers, one score; they never
mix.

## D16 — The ledger hash chain

A single global chain over `ledger.commerce_events` ordered by insertion sequence. `event_hash =
sha256(prev_hash || canonical_json(event))` where `canonical_json` is RFC-8785 JCS (sorted keys, no
whitespace) and timestamps are UTC RFC-3339 at millisecond precision.
`commerce_events.idempotency_key` **IS** `LedgerEvent.event_id` — there is no second identifier. The
writer takes a row-level advisory lock on the chain tail. Nothing outside `apps/trust/src/ledger/**`
(T-011) defines its own hashing.

## D17 — Trust decay is a pure function of ledger timestamps, never `now()`

`weight = exp(−ln2 · (t_eval − t_obs)/HALF_LIFE)`. `t_eval` is persisted per dimension as
`decayed_at` at snapshot-write time; recomputation from the ledger re-evaluates each observation at
its **recorded** `decayed_at`. This preserves idle decay while making S3 replay exact. Anything
reading a wall clock inside scoring breaks S3.

## D18 — The claim-type → trust-dimension mapping is a T-080 manifest artifact

R12 requires verification outcomes to feed the same five per-dimension Betas (`DESIGN.md:82`
explicitly rejects two trust systems). The table that says which `claim_type` a contradicted claim
penalises — price claims → `price_honored`, shipping claims → `shipped_on_time`, discount claims →
`discount_honored`, etc. — lives in the human-approved manifest and is consumed read-only by T-062.
`HALF_LIFE` and the new-store prior N are likewise manifest constants, not trust-engine config (this
is what keeps S2 checkable "against the manifest, not the trust engine's own config").

## D19 — Embeddings: `HashEmbedding` is the configured default in every environment, not a test-only fallback

`EMBEDDING_PROVIDER` defaults to `hash`. `HashEmbedding` must emit **L2-normalised 1024-d float
vectors** so the real cosine index (D6) is exercised. `torch`/`sentence-transformers` are an
**optional extra**, declared but never installed by default (intake measured the extra at **+679 MB
of wheels**, plus a **~2.2 GB** weight fetch on first call — a C9 violation and an unattended-run
stall). T-012 additionally implements a `LocalBgeEmbedding` provider selected by
`EMBEDDING_PROVIDER=local_bge`, with a marker-gated test deselected from `make verify` that skips
cleanly when weights are absent. `make e2e-live` sets `local_bge`.

## D20 — LLM: the double is the default provider, and the network is blocked in tests

`LLM_PROVIDER` defaults to `double`; the real Anthropic client is constructed **lazily** and only
when `LLM_PROVIDER=anthropic`. Never construct a client at import time — that takes down `pytest
packages/llm`, `test_extraction.py` and `test_onboarding.py` on a machine with no key (D3). The root
`conftest.py` installs a socket guard blocking all non-loopback connections during pytest, and the
dispatcher exports `ANTHROPIC_API_KEY=sk-ant-DOUBLE-DO-NOT-USE`, `HF_HUB_OFFLINE=1`,
`TRANSFORMERS_OFFLINE=1`, so an accidental live call fails in milliseconds with a legible message
instead of hanging on SDK retries.

## D21 — "Recorded" means hand-authored, committed, human-reviewed fixture JSON derived from published API documentation

No ticket verify performs a live capture; re-capture against real credentials is an optional step in
T-085's runbook only. Each ticket's recordings live inside its own scope: T-013 →
`services/shopify-stub/fixtures/recorded/**`, T-014 → `packages/llm/fixtures/recorded/**`, T-023 →
`fixtures/mcp/**`. Every recording file carries a provenance header naming the doc version. Applies
to T-013 acc 1, T-014 acc 3, T-023 acc 2 — nobody blocks waiting for credentials that do not exist
here (D3).

## D22 — Checkout URL and code shape

Cart permalink template, used identically by the stub parser (T-013), the merchant builder (T-052)
and the exchange validator (T-033): `https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}`.
Domain validation compares the permalink **host** to `app.sellers.domain` by exact match — no
subdomain wildcards (C10, S8 release blocker). Code format: `PSX-` + 8 characters of **randomly
generated** Crockford base32, uppercase, `usageLimit:1`, expiry `min(48h, offer.expires_at)`, stored
keyed by `offer_id`. The code is never derived from `offer_id` — a derivable single-use redeemable
is a guessable one.

## D23 — Checkout mode: `CHECKOUT_MODE ∈ {redirect, shopify_stub}`, default `redirect`

SPEC amendment 3 and `DESIGN.md:84` both make the starting slice redirect/simulated. **Both modes
always create the code via merchant `POST /codes`** and both emit an identical `LedgerEvent` kind
sequence (C11 — `DESIGN.md:84` explicitly rejects divergent event schemas per mode). T-033's golden
kind sequence is the shared reference that T-081, T-082, T-083 and T-084 all assert against; all of
them run the default.

## D24 — `LedgerEvent` kind enum, extended once in T-010

Add `auction_opened`, `auction_closed`, `offer_integrity`, `blacklisted`, `blacklist_expired` to the
`DESIGN.md:58` enum. `payload` is a **discriminated union, one shape per kind**. The pixel↔webhook
join keys are pinned on **both** `checkout_pixel` and `order_paid` as `{checkout_token, order_ref,
client_id, discount_code}` — snake_case, exactly these names. T-051 and T-061 import the same
generated type; they do not each invent a key name. Add `C11` to T-010's refs so the ticket that
freezes the enum reads the constraint requiring both checkout paths to emit identical kinds.

## D25 — `Claim` and `Offer` are extended once, in T-010, before anything consumes them

`Claim = {claim_id, key, claim_type, value, unit?, source_span?: {pitch_ref, start, end},
provenance}`. `claim_id` is a content hash over `(pitch_ref, key, canonicalized value, claim_type)` —
stable across re-extraction, which is what makes T-065's idempotency criterion satisfiable and lets
`VerificationResult.claim_ref` point at something. `Bid` gains `pitch_ref`.

`Offer = {offer_id, product_ref, variant_ref, unit_price, currency, discount, commitments,
total_price, checkout_url, expires_at, delivery_estimate_days?}`. `variant_ref` is required — cart
permalinks are variant-scoped and T-013's stub exposes no product→variant query. `expires_at` is
what T-032's expiry filter reads.

Three different objects are called "Offer" in the doc set: the protocol `Offer` above, the Neo4j
`Offer{offer_id, price, currency, availability, observed_at}` node (a store's observed listing), and
the `app.offers` table row. They are distinct; the protocol field is `bid_offer_id` where ambiguity
would otherwise arise.

## D26 — `TrustSnapshot` shape

`{store_id, score, confidence, score_version, snapshot_version, low_data: bool, blacklisted: bool,
dims: {price_honored, discount_honored, shipped_on_time, not_returned, feedback_match}: {alpha,
beta, decayed_at}}`. **Do not** add a sixth `claim_veracity` dimension — `DESIGN.md:82` explicitly
rejects two trust systems and requires verification outcomes to layer onto the same five Betas via
D18's mapping. **Do not** add a `blacklist_state` enum — review/appeal/expiry states live in
`app.seller_blacklist(reason_code, source, starts_at, expires_at, status, reviewed_by)`; the
exchange needs only the boolean for fail-closed exclusion.

## D27 — The exchange writes to the ledger only through trust `POST /events`

`DESIGN.md:77` gives the exchange role **no write** on `ledger.*`, yet `ranking_runs` and
`ranking_candidates` live there. T-030 acceptance 3 already states the pattern ("logged to ledger
via trust API stub"); T-032's ranking runs and `exclusion_reasons` follow it. Bandit posteriors are
**exchange-owned runtime state in Redis** (`bandit:{cluster_id}:{store_id}`), rebuildable by
replaying `policy_event` ledger entries — no new table, no migration, no late edit to T-011's
exclusive `db/migrations/**` scope. If durability across restarts is later required, that is an
EXECUTION rule 5 escalation, not a worker's improvisation.

## D28 — Tiers

Tier-0 = a store present in the catalog graph with no agent and no envelope; always represented by a
synthesized list-price bid. Tier-1 = a network-hosted store-agent (`packages/store-agent` runtime).
Tier-2 = an external self-hosted agent bidding through the signed `POST /bid` door. `Store.tier:
enum[0|1|2]` is pinned in `packages/contracts` by T-010. **Solicitation is tier-aware (R10); ranking
is tier-blind (R11).**

## D29 — Shortlist slots

Filled in the order fit → value → reliability → specialist, each drawn from the eligible set minus
already-slotted bids, collapsing gracefully to the available distinct stores (A6). `fit` = max
`intent_match`; `value` = max `price_value`; `reliability` = max `trust`; `specialist` = the bid
satisfying the **greatest number of non-hard `Intent.preferences`** among those not already slotted,
ties broken by D13's stable tie-break. The `specialist` **slot** is unrelated to the `specialist`
**persona** in T-045.

## D30 — Provenance → buyer-facing label

A generated constant in `packages/contracts`, imported by both T-032 (producer) and T-072
(renderer): `owner_statement | envelope_rule → "store-confirmed"`; `scraped | pixel_feed → "from
their website"` (`pixel_feed` is the merchant's own installed-app pixel, so it is store-confirmed
only if the merchant asserted it — it is not; it is observed, hence "from their website");
`learned_policy | network` are internal and never surface to buyers; `seller_asserted` carries **no**
provenance label — SPEC R2 fixes exactly two label strings — and instead surfaces the R18
verification badge (`verified|contradicted|unsupported|ambiguous`). T-072 renders the
`provenance_labels` the exchange supplied; it does not re-derive them. `Provenance.authority_rank` is
either given defined semantics per hook in T-040 or removed from the schema in T-010 — an unused
required int will be filled arbitrarily by whichever ticket touches it first.

## D31 — R14's buyer track record keys off the `app.*` buyer account id, never `vault.*`

`DESIGN.md:77` restricts `vault.*` to the buyer role and puts buyer accounts in unrestricted `app.*`.
Trust reaches a stable buyer identity by joining `order_ref → app.offers → app.intents → app.buyer
accounts`. Session pseudonyms still rotate per session and stores still never receive identity (R5
intact). T-070's grant test and R14 stop contradicting each other, with no new HMAC primitive.

## D32 — `POST /internal/outcomes`: producer is T-061, consumer is T-034

T-061 owns the authoritative reconciled conversion/refund truth; T-034 exposes the intake route
inside its own `apps/exchange/src/policy/**` scope. Payload pinned in T-010: `{cluster_id, store_id,
auction_id, outcome: enum[converted|not_converted|refunded], observed_at}`. T-034 verifies against a
recorded outcome fixture (no new dependency edge); T-061 is added to T-083's `depends_on` so the e2e
that needs the live path cannot start before the producer exists.

## D33 — Merchant route ownership

`GET/PUT /stores/{id}/envelope` and `POST /stores/{id}/kill` belong to **T-053**, whose scope gains
`apps/merchant/svc/src/envelope/**`. `GET /stores/{id}/trust` belongs to **T-064**. T-054 stays a
pure frontend consumer and its non_goal "No new APIs" stands unchanged.

## D34 — T-082 acceptance 1 is an exact per-kind multiset, not "exactly once"

"Each LedgerEvent kind appears exactly once" is unsatisfiable against T-082's own acceptance 2
("Shortlist contained fallback + hosted bids" ⇒ ≥2 `bid_placed`, ≥2 `shown`) and against kinds the
happy path never emits. Replace with an exact expected-count table over the **complete** enum,
asserted as multiset equality: `bid_placed == n_stores_solicited` (read from the run fixture),
`shown == len(shortlist.slots)`, `auction_opened == auction_closed == accepted == code_created ==
checkout_redirect == checkout_pixel == order_paid == reconciled == 1`, `refund == order_fulfilled ==
feedback == offer_integrity == blacklisted == blacklist_expired == 0`, `claim_verified == the run's
asserted-claim count`. A subset or `>=1` assertion does not satisfy this criterion.

## D35 — The C3/S7 import lint has two owners

T-000 ships a standalone `.importlinter` file (not a table inside the shared root manifest)
containing the forbidden contract "apps.exchange must not import sealed/envelope modules", and wires
`lint-imports` into `make verify` / `make check`. T-011 owns the **proof**: a deliberately-violating
fixture module plus a test asserting the check exits non-zero, added to its verify string. Both
halves must exist or S7 is only half-built.

## D36 — Only one pytest configuration exists in the repo: the root `pyproject.toml`

No package-level `pyproject.toml` may contain `[tool.pytest.ini_options]`; no `pytest.ini`, `tox.ini
[pytest]`, or `setup.cfg [tool:pytest]` may exist anywhere. Six verify commands pass a **directory**
to pytest (`packages/contracts`, `packages/llm`, `packages/verification`, `services/shopify-stub`,
`services/sim`, `apps/seller-reference`); a nested config table shifts rootdir for exactly those
invocations and silently drops the root `pythonpath`, `addopts` and marker registry — producing a
ticket-local failure that `make verify` from the root does not reproduce. Enforced by
`scripts/check_verify_contracts.py`.

## D37 — No test file outside `pixel/` may contain the substring "pixel" (any case) in its repo-relative path

T-051's verify is `npx vitest run pixel`, a case-insensitive substring filter over the whole path,
not a project selector. T-050's acceptance 2 is "webPixelCreate called with expected settings
payload", so the natural filename `webPixelCreate.test.ts` would be collected by T-051's verify.
Name it `apps/merchant/app/routes/install.test.ts`. Enforced by `scripts/check_verify_contracts.py`.

## D38 — Neo4j parallel isolation is scheduler serialization plus an flock, not tenant scoping — this narrows D4

D4's verified half stands (Community has exactly one database, so per-worker Neo4j databases are
impossible). Its design half — tenant scoping inside the single database — is replaced: tenant
properties would force composite uniqueness constraints and tenant-parameterised Cypher across seven
tickets written by seven agents, diverging from DESIGN's "uniqueness constraints on every stable
ID". Instead: (a) the scheduler never co-schedules two graph-writing tickets (T-012, T-020, T-021,
T-022, T-023, T-024, T-031); (b) T-000's shared `services/ingest/tests/conftest.py` and
`apps/exchange/tests/conftest.py` hold a session-scoped `flock` on `/tmp/proxyshop-neo4j.lock` as a
safety net, resetting inside the lock; (c) vector-index creation is `CREATE VECTOR INDEX … IF NOT
EXISTS` so a re-entrant session never errors. Postgres per-worker database and Redis per-worker
logical DB index are unchanged from D4.

This is the **only** pre-existing ruling the intake report overrides, and the report flags it
deliberately (`intake-report.md` §9F).

## D39 — Every ticket runs with `PROXYSHOP_WORKER` set, and the root conftest fails the session if it is unset

A silent fallback to a shared default is how every worker ends up on the same database. Postgres
roles are **cluster-global** (intake verified: `pg_authid.relisshared = true`), so `CREATE ROLE`
lives in a one-time `db/init/00-roles.sql` mounted into the container, and `db/migrations` creates
roles idempotently (`DO $$ … EXCEPTION WHEN duplicate_object THEN NULL; END $$;`) while GRANTs —
which are per-database — stay in the migration so every `proxyshop_w<n>` gets its own correct copy.
T-011 gains an acceptance criterion: migrations apply cleanly a **second** time into a **second**
database on the same cluster.

## D40 — `FLUSHALL` is banned repo-wide

Redis isolation is per-worker logical DB index **plus** a `w{N}:` key prefix applied centrally in
T-000's Redis client wrapper. `FLUSHDB` (current index only) is the permitted reset. A grep gate in
`make verify` fails the build on any occurrence of `FLUSHALL`. `maxmemory-policy` is `noeviction` —
silent eviction of `auction:{id}` would read as a flaky test, never as a config error.

## D41 — Any server a test starts binds port 0 and reports its real port through a fixture

No hard-coded ports in test code. Compose publishes only the datastore and stub ports, all
env-overridable, from the block verified free under D9.

---

# Appended by the orchestrator, before first dispatch

## D42 — The source layout is FLAT: `<package>/src/<subpackage>/`, never `<package>/src/<package>/<subpackage>/` **[verified]**

Measured directly against `tickets.json` on this machine: **30** scope globs across **29 of the 44**
tickets use the flat form — `apps/exchange/src/ranking/**`, `apps/trust/src/ledger/**`,
`services/ingest/src/graph/**`, `packages/store-agent/src/hooks/**`,
`apps/merchant/svc/src/collector/**`, `apps/buyer/svc/src/intent/**`, and so on — and **zero** use
the nested form that repeats the package name (`apps/exchange/src/exchange/ranking/**`). `tickets.json`
is the executable source of truth (TASKS.md says so explicitly), which is the same reasoning D1 used
to settle `packages/contracts` over `packages/protocol`.

**Ruling:** the flat form is the layout. A scope glob is what a worker actually obeys, so any
document that shows the nested form is stale and must not be built from. Specifically, the intake
report's §3.1 directory tree (`intake-report.md:132-158`) and its §4 file-ownership map
(`intake-report.md:549-598`) render the nested form — `apps/exchange/{src/exchange/{auction,…}}`,
`apps/trust/src/trust/ledger/**`, `services/ingest/src/ingest/{graph,embeddings}/**`,
`packages/store-agent/src/store_agent/hooks/**`, `apps/merchant/svc/src/merchant_svc/envelope/**` —
and are **superseded on this point only**; everything else those sections say (ownership boundaries,
read-only carve-outs, orchestrator-owned files) stands. The intake report's own §2 rulings already
use the flat form (D16, D32, D33 above), so §2 needs no correction.

Consequence to hold in view rather than re-litigate: under the flat layout each package's `src/`
children are top-level import names, so the scaffold's per-member packaging (`sources = ["src"]`)
and the fixed import namespaces for the three hyphenated directories (`services/shopify-stub` →
`shopify_stub`, `packages/store-agent` → `store_agent`, `apps/seller-reference` →
`seller_reference`) must be reconciled with it by T-000, not by a feature worker.

## D43 — The frozen acceptance runner has NO report-reuse mode; every metric number is paid for with a real pytest run **[verified]**

The intake report's blocker **B2** (`intake-report.md:17-19`) proposed `--write-report <path>` /
`--from-report <path>` to amortize twelve metrics over one suite pass, and §7's metric table
(`intake-report.md:698-718`) is written against that prelude. Both flags were built and then
**removed**.

Evidence, measured here: two independent adversarial verifiers demonstrated end-to-end that a
**one-line, worker-written JSON file** drove both the pass-rate metric and its companion count
metric to target while **three of the four** frozen tests still failed, and `swarmloop verify`
reported the harness intact throughout. The report lives on disk, every worker has an unrestricted
shell, and binding the report to a hash of the frozen directory does not help — workers may read
that directory and can therefore compute any hash they must match.

**Ruling:** no report reuse, in any form. The frozen runner's modes are `--total`,
`--count-passing`, `--pass-rate` and `--json`, with optional and combinable `--epic Ex` and
`--blocker S8-n` filters; a filter selecting zero tests is **empty stdout + exit 1**, never a `0`.
Every metric command runs the suite. §7's `--from-report` prelude and its per-metric commands must
be rewritten to the direct form before `goals.json` is frozen. This is settled — do not re-propose
report reuse.

## D44 — Remotes: GitLab is verified working; the GitHub repository does not exist yet

**GitLab [verified]:** `labs.gauntletai.com/hankholcomb/proxyshop` authenticates from the macOS
keychain and accepts the branch — a `git push --dry-run` reported `* [new branch] main -> main`. No
token was stored anywhere, and the saved remote carries no embedded credentials.

**GitHub — open item, awaiting Hank.** `git@github.com` SSH auth fails in this environment (the
agent has no registered key), so the remote was switched to HTTPS, where `gh`'s credential helper
works. But the repository **`HankH18/proxyshop` does not exist**, and GitHub has no push-to-create.
The Phase 7 push to that remote will therefore fail until someone creates the repository. This half
is **not** verified and is not something an agent should resolve by creating a repository on Hank's
account.

---

_Rulings the intake report states outside its §2 are **not** restated here and are **not** given D
numbers: the T-000 scaffold specification (`intake-report.md` §3, lines 128-545 — orchestrator-owned
files, frozen after T-000 closes), the narrowed per-ticket file-ownership map (§4, lines 549-598),
and the scheduling constraints SC-1 … SC-5 (§5, lines 602-634). They bind the orchestrator and the
scheduler rather than settling a contested reading of the doc set, they are too large to quote into
a task packet, and §3.1/§4 are superseded on the layout point by D42 above. Read them from the
intake report; the packet-level rulings are D1–D44 in this file._
