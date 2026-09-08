# Pinned decisions — quoted into every task packet

These exist to remove ambiguity that two tickets would otherwise resolve differently.
Where the doc set is ambiguous or self-contradictory, **this file is the ruling**, and each
ruling names the evidence it rests on. The doc set still owns *what* to build; this file only
settles *which reading of it is authoritative*.

Workers: follow these exactly. If one of these rulings looks wrong, say so in `CONCERNS` —
do not quietly implement the other reading.

Sections marked **[verified]** were established by running something on this machine, not by
reading a document.

Sections marked **[amendment 1]** record a deliberate, user-approved edit to the frozen
acceptance suite. Each one names the earlier ruling it supersedes and states plainly why that
ruling was wrong. A ruling superseded by an amendment is dead in the part the amendment names
and alive everywhere else; each amendment says which part.

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

**CORRECTED at cycle 1 — the statement this ruling shipped did not parse.** T-012 reported it
and the orchestrator reproduced it live against neo4j:5.26.30 Community: the single-quoted
form raises `Neo.ClientError.Statement.SyntaxError: Invalid input ''vector.dimensions'':
expected an identifier or '}'` at column 109. Cypher map keys must be identifiers or
**backtick**-quoted; `'…'` makes them string literals, which is not legal in a map key
position. Every *parameter* this ruling fixes was and remains correct — only the quoting was
wrong. Six other tickets copy this statement, so it is repaired at the source rather than
worked around in one of them.

Use exactly this, verified to parse and to read back `1024` / `COSINE`:

```cypher
CREATE VECTOR INDEX product_embedding IF NOT EXISTS FOR (p:Product) ON (p.embedding)
OPTIONS {indexConfig: {`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}}
```

Note `'cosine'` on the right-hand side is a string **value** and is correctly single-quoted;
only the two dotted **keys** take backticks. `db.index.vector.queryNodes` returns correct
cosine scores for a 1024-dim vector (DESIGN C2: 1024 dims, cosine). `IF NOT EXISTS` is
required so a re-entrant session never errors — do not "fix" a re-run error by dropping the
index.

Also measured while verifying this, and load-bearing for every retrieval consumer: **Neo4j
rescales cosine as `(1 + cos) / 2`**. An exact match scores ≈ 1.0 and an orthogonal vector
scores ≈ 0.5, *not* 0.0. Reading a raw 0.5 as "half similar" rather than "unrelated" would
inflate every downstream retrieval score; convert before interpreting.

## D7 — `npx vitest run <path>` is a path FILTER and isolates correctly; only the root verify passes with no tests **[SUPERSEDED 2026-09-05 by ESC-023, ruled by Hank]**

> **The second half of this decision no longer describes the shipped behaviour.** `scripts/verify.sh` no longer
> passes `--passWithNoTests`; `run_vitest` FATALs when vitest collects zero tests, mirroring `run_pytest`.
>
> Why it was reversed: the flag ASKED the runner to report success on an empty collection, so the TypeScript half
> of the scored `build_succeeds` metric was fail-open. Measured before the change,
> `vitest run --passWithNoTests 'no/such/pattern/**'` exited 0 — zero collected, `make verify` green, the metric
> still 1. A glob change or a moved directory could have taken 864 tests to nothing without moving the number.
>
> The first half of D7 still holds and is unchanged: `npx vitest run <path>` really is a path filter and really
> does isolate correctly. Only the no-tests clause is dead.
>
> `scripts/check_verify_contracts.py` cites D7 in three places (module docstring, `check_vitest_projects_have_tests`,
> and a failure message a reader sees). Those citations are now false as to the flag. **The check itself stays** —
> per-project coverage is finer-grained than "zero tests overall" and catches something the new FATAL does not.
> That file is a FROZEN path, so correcting its prose is filed separately rather than folded in here.

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

## D19 — Embeddings: `HashEmbedding` is the configured default in every environment, not a test-only fallback **[the DEFAULT clause is amended by D56; every other clause stands]**

> **Pointer, added by D56.** The sentence "`EMBEDDING_PROVIDER` defaults to `hash`" is
> superseded: the default is now `lexical`. Everything else below — the 1024-d L2-normalised
> requirement, the reason it exists, the +679 MB / ~2.2 GB objection, `local_bge` as an
> optional extra, `make e2e-live` — is unchanged and still binding. D56 also scopes what the
> "so the real cosine index (D6) is exercised" clause ever claimed. Read D56 before quoting
> this section.

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

SPEC amendment 3 and `DESIGN.md:84` both make the starting slice redirect/simulated. ~~**Both modes
always create the code via merchant `POST /codes`**~~ — **that clause is SUPERSEDED by D45**: both
modes create the code through the injected `CheckoutProvider` port, and only the Shopify adapter
calls merchant `POST /codes`. The rest of this ruling stands unchanged — both modes emit an
identical `LedgerEvent` kind sequence (C11 — `DESIGN.md:84` explicitly rejects divergent event
schemas per mode). T-033's golden kind sequence is the shared reference that T-081, T-082, T-083
and T-084 all assert against; all of them run the default.

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

## D45 — Checkout is reached through a `CheckoutProvider` port; Shopify is one adapter — supersedes one clause of D23 **[verified: the frozen `accept/4` takes an injected creator and never imports `apps/merchant`]**

D23 ruled that both checkout modes create the code via merchant `POST /codes`. That clause — and
only that clause — is what puts Shopify on the starting slice's critical path:
`T-033.depends_on` carries `T-052` (discount-code creation), which depends on `T-050` (Shopify app
install), so the simulated-checkout demo cannot be reached without the whole Shopify install lane.

Measured here: the frozen suite already implements the seam the ticket graph denies. Four frozen
tests call `accept(auction, bid_ref, code_creator, mode)` with the creator as the **third
positional** — `test_e3_exchange.py:658, :682, :685, :704, :711`, passing `_RecordingCodeCreator`
("in-process stand-in for the merchant `POST /codes` client", `:255`) — and `test_spec_criteria.py:1042,
:1073` for the S8-3 blocker. `test_e3_exchange.py` contains **no reference to `apps/merchant` at
all** (grepped). The port is already mandatory in the goal; only `tickets.json` disagreed.

**Ruling:** `accept()` reaches checkout through an injected **`CheckoutProvider`** port.
`SimulatedRedirectProvider` is the required starting implementation and mints the code locally; a
Shopify adapter implements the same port and calls merchant `POST /codes`. New ticket **T-036** owns
the port plus the simulated provider; `T-033.depends_on` becomes `["T-030","T-036"]`; `T-052` keeps
the Shopify adapter and leaves the slice path.

**What of D23 survives:** everything but the superseded clause. The mode vocabulary
(`CHECKOUT_MODE ∈ {redirect, shopify_stub}`, default `redirect`) stands, and so does the C11
guarantee that both modes emit an identical `LedgerEvent` kind sequence — that is what
`test_e3_exchange.py:677` asserts, and it is indifferent to who mints the code.

Re-pointing the edge costs no frozen byte. **Deferring** any Shopify ticket is a different ask and
does need a `freeze --amend`: `goals.json` sets `e5_merchant_passing` target 10, and all ten E5
tests hard-import T-050/T-051/T-052/T-053 product code. Do not conflate the two — re-ordering the
graph is free, shrinking the build is user-awake.

## D46 — Eligibility is a gate ABOVE the frozen function surface, at all three R12 points **[verified: frozen `accept/4` and `collect_bids/3` call sites counted]**

`SPEC.md:25` (R12) requires fail-closed reads before **solicitation, ranking and checkout**. Only
ranking has an owner: `T-032`'s objective carries the eligibility filter and
`test_spec_criteria.py:928` makes it release blocker S8-1. `T-030` (solicitation) and `T-033`
(checkout) mention eligibility nowhere — not in objective, not in acceptance, not in `depends_on`.

**Ruling:** a versioned **`SellerEligibility`** interface answers "may this store participate",
fails closed when the read is unavailable, and is consulted at all three gates:

- **solicitation** — in the **roster builder**, which runs *above* `collect_bids`;
- **ranking** — inside `rank()`, where it already is and already blocks release (S8-1);
- **checkout** — in the **accept orchestration**, *above* `accept()`.

`T-010` owns the port and its deterministic double — the same treatment D26 gives `TrustSnapshot` —
while `T-062`/`T-064` own the live implementation. This does **not** reopen intake's decline of a
`T-064 → T-032` edge: that decline was about depending on the trust *service*, and it stands.

**The layering is not a style preference.** Measured here: four frozen tests call
`accept(auction, bid_ref, creator, mode)` across seven call sites and **require it to succeed**
(`test_e3_exchange.py:658, :682, :685, :704, :711`; `test_spec_criteria.py:1042, :1073`), and two
call `collect_bids(roster, responses, now)` with three positionals (`test_e3_exchange.py:334, :386`)
over roster records of `{store_id, tier, product_ref, list_price}` carrying no eligibility field. A
gate *inside* either function that denied when no eligibility port was injected would turn every one
of those red, S8-3 included. One layer up gives the identical guarantee at zero cost to the harness.
**Do not add a required parameter to either frozen signature.**

## D47 — Solicitation and bid submission are two endpoints **[endpoint split stands; constraints 1 and 2 below are SUPERSEDED by D52, amendment 1]**

`DESIGN.md:65` defines store-agent `POST /bid` as `BidRequest → Bid|decline` — the exchange
*solicits*. `DESIGN.md:31`, `:38` and `:95` use the same route as the inbound door through which an
external Tier-2 seller *submits* a signed `Bid`, and `T-044`'s objective describes receipt, not
answering a request. Two operations, one route definition: an executor reading `:65` and an executor
reading T-044 build incompatible things.

**Ruling:** split them.
- **Solicitation (exchange → seller):** `POST /v1/bid-requests`, `BidRequest → Bid | Decline`.
- **Submission (external seller → exchange):** `POST /v1/auctions/{auction_id}/bids`, signed
  `Bid → AcceptedForVerification | Rejected`. Signature verification, replay protection and
  auction/deadline validation are **exchange-boundary** duties.

**~~Two frozen constraints bound the fix; honour both.~~ SUPERSEDED BY D52.** Both were measured
correctly against the PRE-amendment suite, and both have since been amended away on purpose. The
endpoint split above stands unchanged; constraints 1 and 2 below are historical. **Build to D52.**
The keyring is now nested `{signer_id: {key_id: secret}}` and the envelope fields are REQUIRED.
Recorded rather than deleted, because a ruling file that rewrites its own history is worse than one
that shows the reversal. Original text:

1. `test_e4_store_agent.py:892` and `:926` import `receive_bid, sign_bid` from
   `packages.store_agent.src.external`, and the keyring is a flat `{store_id: key}` mapping
   (`:895`, `:928`). The **boundary** moves — the exchange route becomes the library's only public
   caller — but the module path and the flat keyring do not. Physically relocating `receive_bid`
   into `apps/exchange` voids the freeze, and a `key_id`-indexed keyring breaks it too. No worker
   may "tidy" that module across the boundary.
2. `signer_id`, `key_id`, `issued_at` and the nonce/idempotency key are **OPTIONAL** envelope
   fields, validated when present. None of the four appears anywhere under `.swarm-loop/acceptance/`
   (grepped), and the frozen external payload (`test_e4_store_agent.py:861-885`) carries none of
   them — a receiver that required one would fail `:889` and `:923`.

`app.seller_endpoints` holds the registered key per `store_id`, matching that flat shape. Note that
wrong-key, garbage, absent and cross-payload-replayed signatures are *already* frozen rejections at
`:923-960`; what the new nonce closes is same-payload temporal replay, which nothing asserts yet.

## D48 — Product-fact claims map onto the five trust dimensions through a published table; the earlier decline is reversed in substance **[verified: the dimension vocabulary is closed by exactly two frozen assertions]**

**The record first, because the reversal matters more than the outcome.** An earlier intake pass
declined a sixth trust dimension, and D26 states the reason it gave: "`DESIGN.md:82` explicitly
rejects two trust *systems*". That answered a different question than the one asked. A dimension
*within* the single Beta framework is not a second trust system, and `DESIGN.md:82` does not forbid
one. The decline was too quick, and the gap it left is real — product-fact claims (ingredients,
compatibility, nutrition, specifications) had no route into trust at all. It is revisited here and
**reversed in substance**.

What the decline was right about by accident is the *cost*. Measured here, the suite is asymmetric:

- **Snapshot fields are open.** Every frozen `dims` traversal iterates the known five and ignores
  extras — `test_e6_trust.py:505`, `:638`, `:940`. There is **no** `assert set(dims) == DIMS`
  anywhere in the suite (grepped).
- **The dimension vocabulary is closed, by exactly two assertions, and they bind.**
  `test_e6_trust.py:70` pins `DIMS` and `:265` asserts `dim in DIMS`; `test_e8_proofs.py:69` pins
  `TRUST_DIMENSIONS` and `:356` asserts `dim in TRUST_DIMENSIONS`. Both govern
  `fixtures/manifest.json → dishonest_store.behaviours[].dim` — the only route from a contradicted
  claim to a Beta. A sixth dimension that actually carried verification outcomes would have to amend
  both.

**Ruling, in two halves because they cost differently:**

- **Metadata half — adopt in full; it is free.** `TrustSnapshot` gains `confidence`,
  `effective_sample_size`, `score_version`, `snapshot_version` and `computed_through_event`.
  `confidence` and `score_version` are already in D26's shape and already asserted by the frozen
  replay test; the other three appear nowhere under `.swarm-loop/acceptance/` (grepped) and are
  unasserted, permitted additions.
- **Dimension half — a published `claim_type → dimension` mapping table, NOT a sixth dimension.**
  Product-fact claims map explicitly onto an existing integrity dimension through a table published
  in DESIGN and instantiated in T-080's manifest — D18's table, now required to be complete rather
  than illustrative. Offer-fact claims (price, discount, delivery) keep their current dimensions.
  The table also states how `unsupported` and `ambiguous` outcomes move **confidence** rather than
  score.

A genuinely named `catalog_claim_accuracy` dimension remains available as a **deliberate, logged
amendment** if Hank later judges it worth voiding every cycle's scores. It is not being smuggled in
under a mapping table, and this ruling does not claim the table is as expressive as a dimension
would be — only that it closes the gap without an amend.

## D49 — The trust-replay obligation lives on the scoring ticket **[verified: the frozen replay test is stricter than either ticket's text]**

`T-060` replays with `non_goals: ["No scoring"]`. `T-062` is the only scoring ticket, and its refs
are `["R12","S2","DESIGN#decisions--rationale"]` — no `R15`, no `S3`, and none of its three
acceptance criteria mentions replay. R15/S3 promise bit-for-bit replay and no ticket owns it.

**Ruling:** `T-062` owns it. It gains `R15` and `S3` in `refs` and an acceptance criterion requiring
that rebuilding every observation and trust snapshot from an empty projection — using recorded event
timestamps and a versioned scoring configuration — reproduces every served snapshot bit-for-bit.
`T-060`'s replay criterion is reworded to **"event-stream hash"** so it cannot be read as the
trust-score assertion; the frozen T-060 tests already assert exactly that narrower property.

**The ticket is being raised to the goal, not the reverse.** Measured here,
`test_e6_trust.py:477` (`test_replay_reproduces_served_trust_scores_bit_for_bit`) already compares
`score`, `confidence` and `score_version` between served and replayed snapshots, then every one of
the five dimensions' `alpha`, `beta` **and `decayed_at`** (`:500-508`). Decay determinism is pinned
to each observation's recorded as-of time and never to a wall clock — that is D17, and this test is
what makes D17 non-negotiable rather than advisory.

Settle the ownership collision in the same pass: the frozen import is
`from apps.trust.src.ledger import replay`, and `apps/trust/src/ledger/**` is T-011's exclusive
scope, so T-062 is currently forbidden to write the module its own frozen test imports. Keep the
scoring in T-062-owned files and re-export `replay` from T-011's `apps/trust/src/ledger/__init__.py`
— one line, no scoring in it. The frozen import path does not move.

## D50 — One rank formula, and its weights are reachable from the section workers actually read **[verified: both formulas sit in the one DESIGN section T-032 refs; `RankingWeights` has no owning ticket]**

D12 already ruled `DESIGN.md:85` (five-term) authoritative and `DESIGN.md:87` (three-term
`w_f*fit + w_v*offer_value + w_t*trust`) superseded. **That ruling was not enough, for a mechanical
reason.** `EXECUTION.md` rule 1 restricts a worker to "its contract + SPEC/DESIGN sections it Refs +
files in its scope". `T-032.refs` includes `DESIGN#decisions--rationale`, which is the section
spanning `DESIGN.md:81-96` and therefore contains **both** formulas, two bullets apart — and
`decisions.md` is not in that reading set. A T-032 worker obeying rule 1 literally reads both
formulas and never reads D12. A ruling an agent cannot reach is not a ruling.

**Ruling:** delete `DESIGN.md:87`'s formula, or annotate it inline as superseded by `:85`; and fold
D13's concrete weights, normalization, missing-value behaviour and penalty bound into `:85` itself,
so the numbers are visible from the section the ticket refs. `fit` and `value` survive only as D29
shortlist slot names — a different concept from `:87`'s formula terms, and not a reason to keep it.

No frozen byte is at risk. The five weight symbols `w_m`, `w_e`, `w_t`, `w_v`, `w_d` appear nowhere
under `.swarm-loop/acceptance/` (grepped), and the frozen candidate record carries exactly the
five-term feature names — `intent_match`, `price_value`, `delivery_fit`, `verified_claim_ratio`,
`policy_penalties` (`test_e3_exchange.py:154-158`) — while `fit` and `offer_value` are not candidate
fields at all. The suite is already shaped for `:85`.

**And the orphan:** D13 says `RankingWeights` "is defined in `packages/contracts`", but grepping
`tickets.json`, `SPEC.md`, `DESIGN.md`, `TASKS.md` and `EXECUTION.md` for `RankingWeights` returns
nothing — its only occurrence in the repo is `decisions.md:160`, D13 itself. No ticket owns it.
Give it to **`T-010`**, which owns `packages/contracts`.

## D51 — R10 is about *eligible* stores, and fallbacks are subject to the same claim rules **[verified: no frozen contradiction — `collect_bids` and `rank` are different stages]**

`SPEC.md:23` (R10) reads "Tier-0 stores are always represented at list price", which an executor can
read as licence to skip the eligibility filter, sitting alongside S8-1's absolute requirement that a
blacklisted seller never surfaces. Separately, the exchange synthesizes a list-price fallback for a
silent store without passing it through bid validation, so its catalog facts could reach ranking
unverified.

**Ruling:** R10 reads "every **eligible** Tier-0/Tier-1 store selected for the auction roster
receives either a timely bid or a catalog-derived fallback." Tier-2 externals are not solicited at
all — they submit through the signed door (D28, D47). Fallback facts are subject to the same
verification and hard-constraint rules as bid claims: **a fallback cannot satisfy a hard constraint
on unverified catalog data.** T-030's acceptance 1 says "6 ranked entries"; `collect_bids` produces
*collected* entries, and that word is wrong twice over — the stage is wrong, and it reads as a
promise that all six survive ranking, which the eligibility filters may legitimately break.

**There is no frozen contradiction here, and it must not be reported as one.** Measured here,
`collect_bids` is imported from `apps.exchange.src.auction` (`test_e3_exchange.py:310, :356`) and
`rank` from `apps.exchange.src.ranking` (`:404` onward, and `test_spec_criteria.py:932` for S8-1) —
different functions, different stages, disjoint inputs; `collect_bids` receives no trust snapshot
and structurally cannot filter on one. The frozen R10 test's own docstring already says every
"**rostered**" store, and its roster (`:312-319`) is six records of
`{store_id, tier, product_ref, list_price}` with no eligibility field and no blacklisted member. An
upstream eligibility filter is invisible to both frozen T-030 tests. This is a wording fix that
moves SPEC toward what the frozen suite already says — the rare case where the frozen artifact is
the more correct of the two.


## D52 — The external signing envelope is REQUIRED, and the keyring is indexed by `key_id` — this reverses half of D47 **[amendment 1]**

D47 ruled `signer_id`, `key_id`, `issued_at` and the nonce **optional** envelope fields, and it
named its reason outright: none of the four appears anywhere under `.swarm-loop/acceptance/`, and
the frozen external payload (`test_e4_store_agent.py:861-885`) carries none of them, so "a receiver
that required one would fail `:889` and `:923`."

**That reason was the wrong kind of reason, and the record should say so.** A fixture that omits a
field is evidence that the fixture was written before the field existed. It is not an argument that
the public v1 network contract should accept submissions with no signer identity, no key selector,
no issue time and no replay token. What D47 actually did was let a frozen test dictate a public
security contract, and it bought that with the three properties the door is signed for in the first
place: key rotation, freshness, and robust replay protection. Hank approved amending the fixture
instead. The optionality is withdrawn.

**Ruling — the four fields are required, and three further properties come with them:**

1. **Required envelope.** A `Bid` submitted through `POST /v1/auctions/{auction_id}/bids` carries
   `signer_id`, `key_id`, `issued_at`, `nonce` **and `schema_version`** — five required fields, not
   four; the amended suite freezes `schema_version` alongside the other four because a signature
   that does not bind the schema version can be replayed across a schema change. A submission
   missing any one of them is `Rejected` at the exchange boundary, before extraction and before
   verification, with the same finality as a bad signature. There is no legacy-tolerant production
   path. A fixture adapter, if backward compatibility is ever genuinely needed, lives inside a test
   and is never reachable from the route.
2. **Canonical signing bytes.** `canonical_signing_bytes(payload)` covers `auction_id`, `signer_id`,
   `store_id`, `issued_at`, `nonce`, `key_id`, `schema_version` and `payload_hash(payload)` — a
   digest over the bid body — so a signature cannot be lifted onto a different auction, signer,
   store, instant, key or payload. It is order-independent and survives a JSON round trip
   unchanged. Both are **one** exported function each, called by `sign_bid` and `receive_bid`
   alike; neither re-derives the bytes and no third caller re-implements them.
3. **Nonce persistence outlasts the auction.** Consumption goes through an injected `NonceStore`
   (`seen(signer_id, nonce)`, `purge_expired(as_of)`) durably backed by `app.bid_nonces`. A consumed
   `(signer_id, nonce)` is still remembered at every moment before that auction's `respond_by` and
   is forgotten only *after* it passes. Holding it in the Redis auction state would reopen
   same-payload temporal replay the moment that state expires — precisely the window D47 identified
   as still open and then left open. Uniqueness is **per signer**, never global. A submission
   arriving after `respond_by` is refused outright, and `issued_at` is additionally checked against
   a finite `freshness_window_seconds` in both directions, so a far-past or clock-skewed-future
   issue time rejects.
4. **The keyring is indexed by `key_id` under the signer.** Rotation is what `key_id` is for: a
   signer may hold more than one live key, and a signature names which one it used. D47's flat
   `{store_id: key}` mapping cannot express rotation and is superseded — the keyring is
   `{signer_id: {key_id: secret}}`. Note the outer key: it is the **signer**, not the store, and a
   lookup keyed on `key_id` alone is wrong, because two signers may legitimately use the same
   `key_id` string. An unknown `(signer_id, key_id)` pair is a rejection, never a fallback to
   another key of that signer.

**What of D47 survives:** everything but the optionality and the keyring shape. The endpoint split
stands — solicitation is `POST /v1/bid-requests` (exchange → seller, `BidRequest → Bid | Decline`),
submission is `POST /v1/auctions/{auction_id}/bids` (external seller → exchange, signed
`Bid → AcceptedForVerification | Rejected`). The module location stands: `sign_bid`, `receive_bid`
and the canonicalizer remain importable from `packages.store_agent.src.external`, and physically
relocating them into `apps/exchange` still voids the freeze. Signature verification, replay
rejection, auction existence, `respond_by`, offer-expiry and blacklist checks remain
**exchange-boundary** duties that run before an external bid is enqueued. `app.seller_endpoints`
gains per-`key_id` rows; the consumed-nonce store is a new `app.*` table in T-011's migrations.

## D53 — `catalog_claim_accuracy` is a sixth dimension in the ONE trust system — this reverses D48's dimension half **[amendment 1]**

D48 declined the sixth dimension and installed a `claim_type → dimension` mapping table in its
place. Read D48's own reasoning back: the dimension half was declined because "a sixth dimension
that actually carried verification outcomes would have to amend both" frozen vocabulary assertions
(`test_e6_trust.py:70` and `:265`; `test_e8_proofs.py:69` and `:356`). **That is a statement about
what an amendment would cost, not a design argument** — and D48 half-admitted it, closing with the
sixth dimension left "available as a deliberate, logged amendment". This is that amendment; Hank
approved it.

The substance was always on the other side. A false dairy-free claim is not `price_honored`, not
`discount_honored`, not `shipped_on_time`, not `not_returned`, and calling it `feedback_match`
asserts that a routed buyer reported a mismatch when no buyer has yet bought anything. D48's table
routed every product-fact claim into a post-purchase satisfaction Beta, where catalog dishonesty
became arithmetically indistinguishable from a late delivery — corrupting the meaning of both, and
of the score built from them. A mapping table redistributes an existing vocabulary; it cannot supply
a concept the vocabulary lacks.

**Ruling:** trust has **six** dimensions and remains **one** trust system — one Beta framework, one
decay rule, one score, one ledger. Two trust systems are still rejected (`DESIGN`'s reconciliation
bullet); a sixth Beta inside the single framework was never what that bullet forbade.

- `TrustSnapshot.dims` = `{price_honored, discount_honored, shipped_on_time, not_returned,
  feedback_match, catalog_claim_accuracy}`, each `{alpha, beta, decayed_at}`. The same six are the
  vocabulary governing `fixtures/manifest.json → dishonest_store.behaviours[].dim`.
- **The mapping table survives; its right-hand side changes.** It is still required to be **typed
  and exhaustive**. Offer-integrity claim types keep their natural transaction dimension —
  `price`/`unit_price`/`total_price` → `price_honored`; `discount`/`promo_eligibility` →
  `discount_honored`; `delivery`/`shipping_speed`/`dispatch_window` → `shipped_on_time`;
  `return_policy`/`warranty` → `not_returned`. Product-fact claim types — `ingredients`,
  `compatibility`, `nutrition`, `specifications` — route to `catalog_claim_accuracy` instead of
  `feedback_match`. An unmapped `claim_type` raises at manifest load; it never silently defaults to
  a dimension. That failure mode is the whole value of "exhaustive".
- **Outcome treatment, as approved:** `verified` is a positive observation on the mapped dimension.
  `contradicted` is a negative observation at the published weight 2.0. `unsupported` moves the mean
  by a small published policy weight strictly below `contradicted`, and its dominant effect is to
  hold `confidence` and coverage down rather than to move `score`. `ambiguous` produces **no** mean
  movement at all and lowers coverage/confidence only. Neither `unsupported` nor `ambiguous` ever
  satisfies a hard constraint or counts as verified evidence (R19), and `ambiguous` additionally
  cannot satisfy a hard constraint by construction.
- `feedback_match` is handed back its own meaning: the post-purchase, buyer-reported match between
  what was pitched and what arrived (R14), cross-checked against return behaviour.
- D18 and D26 are amended to the six-dimension vocabulary wherever they name five. D48's metadata
  half — `confidence`, `effective_sample_size`, `score_version`, `snapshot_version`,
  `computed_through_event` on `TrustSnapshot` — is untouched and still stands.

**What this costs, stated plainly:** every trust score, served snapshot and manifest trajectory
expressed over five dimensions is void, and the two frozen vocabulary assertions plus the manifest's
`dim` values are amended with them. No product code exists yet, so the bill is the fixture edit and
nothing else. It only ever gets larger.

## D54 — Eligibility is a system guarantee at the public orchestration boundary, not a property of three pure functions **[amendment 1]**

D46 put the three R12 gates one layer above the frozen function surface, and its layering argument
was correct: `accept(auction, bid_ref, creator, mode)` and `collect_bids(roster, responses, now)`
are called positionally by frozen tests that **require them to succeed**, so a gate inside either
that denied on a missing eligibility port would turn seven call sites red, S8-3 included.

What D46 did not say is what that left behind. Measured: ranking's gate is asserted and blocks
release (S8-1, `test_spec_criteria.py:928`); **solicitation's and checkout's gates were asserted
nowhere.** They existed in a ticket objective and in this file. An unasserted gate is a plan, not a
guarantee, and R12's fail-closed promise covers three gates, not one.

The positional tests are not evidence about the gates in either direction. They are **unit tests of
pure functions** handed an already-eligible roster and an already-approved bid: they show those
functions compute correctly on clean input and are structurally silent on whether anything upstream
refuses dirty input — `collect_bids` receives no trust snapshot and its roster records carry no
eligibility field at all. Inferring the system guarantee from a layer those tests bypass is exactly
the inference this ruling forbids.

**Ruling:** the versioned `SellerEligibility` gate is asserted **at the public orchestration
boundary, by denial**, at all three R12 points. The boundary layer is
`apps.exchange.src.orchestration` — the layer that decides *who gets asked* and *whether a checkout
may proceed* — and the port is `apps.exchange.src.eligibility`:

- **solicitation** — `solicit_bids(roster=, solicitor=, eligibility=, now=)` reads eligibility for
  every rostered store before anything is asked. An ineligible store is never solicited and never
  appears even as a list-price fallback entry; each denial is recorded in the result's `denied` list
  with a reason naming the condition (`blacklist` / `unavailable`).
- **ranking** — `rank()` excludes the ineligible candidate and records the exclusion reason (already
  blocking, S8-1).
- **checkout** — `accept_offer(auction=, bid_ref=, code_creator=, mode=, eligibility=)` **re-reads**
  eligibility at accept time, so a store blacklisted after it bid gets no code and no permalink, and
  the code creator is never called.

**Fail-closed means three things, not one:** `BLACKLISTED` denies, `UNAVAILABLE` denies, and an
eligibility read that *raises* denies exactly like `UNAVAILABLE`. The port is versioned —
`SellerEligibility.interface_version` against the module constant
`SELLER_ELIGIBILITY_INTERFACE_VERSION` — and a source speaking another version is refused at both
boundary gates rather than trusted.

Every one of the three carries a **positive control in the same test**: the identical input with an
eligible seller and an available read must succeed, mint exactly one code, and carry the solicited
prices intact through the orchestration layer. A surface that denies everything satisfies a denial
assertion and is not a gate; a stub that returns nothing fails these tests.

**D46's layering constraint is unchanged and still binds.** `collect_bids/3` and `accept/4` keep
their positional signatures and gain **no required parameter**; any eligibility port reaching them
is an optional keyword whose absence is not itself a denial. The denial happens in the callables
above them, and those callables are now named in DESIGN §Interfaces rather than left to an
executor's imagination.

**Ownership is corrected here, and it is not `T-010`.** D46 gave the port to `packages/contracts` on
the strength of D26's treatment of `TrustSnapshot`; the amended suite imports it from
`apps.exchange.src.eligibility`, so `packages/contracts` is the wrong home and that half of D46 is
superseded. `T-030` creates `apps/exchange/src/eligibility/**` and the `orchestration` package with
`solicit_bids`; `T-033` adds `accept_offer` to that same package and must not modify `solicit_bids`
— the two never run concurrently, since `T-033` depends on `T-030`. `T-032` owns the ranking gate
and the versioned-interface assertion, which imports **both** boundary callables and therefore needs
a `T-033` edge it did not have. `T-062`/`T-064` still own the live blacklist lookup behind the port.

---

_Rulings the intake report states outside its §2 are **not** restated here and are **not** given D
numbers: the T-000 scaffold specification (`intake-report.md` §3, lines 128-545 — orchestrator-owned
files, frozen after T-000 closes), the narrowed per-ticket file-ownership map (§4, lines 549-598),
and the scheduling constraints SC-1 … SC-5 (§5, lines 602-634). They bind the orchestrator and the
scheduler rather than settling a contested reading of the doc set, they are too large to quote into
a task packet, and §3.1/§4 are superseded on the layout point by D42 above. Read them from the
intake report; the packet-level rulings are D1–D54 in this file._

_**Amendment 1** to the frozen acceptance suite is D52, D53 and D54, approved by Hank on the
strength of an external review. They supersede parts of D47, D48 and D46 respectively; each names
the part. The runbook split that came out of the same review needed **no** amendment (the frozen
S6 test globs every markdown file under `docs/demo/` and concatenates them before checking
headings), so it is recorded where it belongs — `SPEC.md` §Success criteria and `tickets.json`
T-085/T-087 — and carries no D number._

## D-ESC-027 — `pending_remeasure: [7]` stays, and the final report must say why

**Ruled by Hank, 2026-09-05, in session:** *"I'll go with your suggestion for esc-027"* — Option A
plus Option C, the pair recommended.

**The decision.** `pending_remeasure: [7]` is NOT cleared. No `measure --cycle 7` is run and no
value is hand-recorded. **The final report must state that the run's terminal signal is withheld
by a flag no honest action can clear**, and must treat *"all 12 metrics at target plus the demo's
own measured gap list"* as the real completion signal.

**Why, preserved so nobody re-opens it on a worse basis.** `analyze` makes `not pending` an
absolute veto on `all_done`, with no override flag (the neighbouring `manual_metrics` conjunct has
one; this does not). The flag is CORRECT — amendment 3 moved a target at `2026-09-02T14:42:49` and
cycle 7's measurement is stamped `2026-09-02T13:19:45`, 83 minutes earlier, so cycle 7's baseline
was genuinely invalidated. The problem is not that it fired; it is that nothing can honestly
discharge it:

* cycle 7's HEAD **is** recorded (`dd6027bb` in `state.json` head_history), so an honest re-measure
  is conceivable in principle;
* but `measure` has **no** `--at` / `--ref` / `--worktree` flag — it measures the current checkout,
  so `measure --cycle 7` would stamp TODAY's values into cycle 7's row;
* and `.swarm-loop/state.json` lookup does not walk up from a linked worktree (measured 2026-09-05
  via the `slot` allocator), so running `measure` from a tree checked out at `dd6027bb` cannot
  write the primary checkout's history either;
* and the skill states explicitly that a hand-entered `record` does not satisfy the flag.

So the only mechanically available action corrupts the only record of what the trajectory actually
was, in order to satisfy a flag whose entire purpose is protecting that record. Leaving it standing
costs nothing and destroys nothing, and the flag keeps telling the truth.

**Filed against the harness too** (Option C), because it is a defect in the instrument rather than
a fact about this project: a mechanism that demands a re-measure it provides no way to perform for
a past cycle. See `W09040017-06` and `B09040017-04` in the machine-level harness document, which
already carry it from the watchdog's side.

## D55 — Two agents, two objectives: the buyer's agent pitches every candidate, the shop's agent advocates for one **[owner ruling, 2026-09-07]**

The owner redirected the product away from its first-price-sealed-auction framing. His argument is
mechanical and correct: every store has a maximum discount it will authorise, and a market whose
allocation rule is dominated by discount guarantees every store reaches that maximum and then
differentiates on nothing. The system trains its own participants into a commodity market. What
replaces it is a **matching and persuasion market** — a graph finds plausible shops, and shops
compete on how well they can make their case.

**The part that is a genuine architectural ruling, and is easy to get wrong:** there are TWO
pitching agents with DIFFERENT objective functions, and they are not two quality tiers of one
pipeline.

* The **buyer-side agent** wants the customer to buy *a* product. It maximises conversion across the
  whole shortlist, so it pitches **every** candidate as attractively as it can — including scraped,
  non-network shops that have no agent of their own.
* The **shop-side agent** wants the customer to buy *their* product. It is a dedicated advocate that
  continuously improves one shop's case.

A scraped shop therefore gets the former and not the latter. **"A dedicated advocate" is the service
a shop buys by joining the network** — that is the supply-side business argument for the graph, and
the reason a scraped shop must still be able to appear and still be pitched.

**Ruling, and the constraint that makes it safe:** the buyer-side agent may assemble a pitch ONLY
from facts already in the exchange's own snapshot for that shop. It may choose emphasis, ordering,
framing and which true facts to lead with — which is most of persuasion — but it may NOT introduce a
fact the platform has not checked.

The reason is liability, not tidiness. An agent optimising purely for conversion will oversell, and
when the PLATFORM writes the copy, the platform owns the false claim — a worse position than a store
lying about itself, because the buyer has no reason to discount the platform's own voice. A
buyer-side pitch built from the snapshot is verifiable by construction, since the platform authored
it from data it already holds. The store-authored pitch is the one that needs adversarial
verification, because it is the one with a motive.

**Consequence for scoring, which supersedes an earlier proposal in the redirect plan:** the plan
proposed that a shop with no catalogue snapshot renders "visibly unverified prose scoring zero".
That is now wrong in its premise — a scraped shop has a snapshot (the platform scraped it) and gets
a buyer-side pitch grounded in it. What scores zero is a claim the exchange could not check, not a
shop that lacks an advocate.

**Two further owner rulings recorded at the same time:**

* **Frozen assertions at Phase 3:** invert the three hosted-claim assertions one-for-one in place
  (which holds every metric count exactly, since `goals.json` is `tolerance: 0`) AND cut a new epoch
  at the same targets, so the record shows the GOAL changed rather than hiding a product redirection
  inside a maintenance amendment class. Do not manufacture an amendment class for it.
* **Media:** verified-primary with labelled-unverified as the fallback. An asset scores only if it
  resolves, sits on the seller's registered domain, and its content hash matches the catalogue
  snapshot for that `product_ref`; anything else may still be shown but must be labelled unverified
  and must not score. Note that no media code exists anywhere in the tree today.

**The owner's own framing of D55, which is clearer than the above and should be the one quoted:**
this is the organic-versus-sponsored split, taken further. *"Google already does this. For
non-sponsored results, they just simply scrape what's there. For sponsored results, the advertiser
is able to go and pay for a little bit more control. That's what we're doing here, just to a greater
degree."*

Read that way, several things stop being open questions:

* A scraped shop appearing with a platform-authored pitch is the ORGANIC result. The platform
  renders what it crawled. It is not doing the shop a favour and the shop has not asked for
  anything; the shopper is the customer being served.
* An in-network shop is the SPONSORED result, and what it buys is **control over its own
  presentation** — a dedicated advocate, a pitch conditioned on this buyer, its own choice of
  commitments, a profile-conditioned discount. "To a greater degree" is the product: the control
  purchased here is far beyond ad copy.
* The verification asymmetry follows from the split rather than being an extra rule. The platform's
  rendering of its own crawl carries the platform's voice and is constrained to the platform's own
  facts. A seller's purchased message carries the seller's motive and is therefore the one that gets
  adversarially checked against the snapshot. The rule "the buyer-side agent may not introduce a
  fact the platform has not checked" is just the organic side declining to launder a claim it did
  not verify.
* It also settles what a shop is BUYING, which the earlier framing left vague: not visibility, and
  not a better score. Visibility is organic and earned by matching. What is bought is the right to
  make the case in one's own voice, and the loop that improves it.

## D56 — The default embedding provider is `lexical`; D19's index-coverage promise is kept, and its ranking promise never existed **[amends D19's default clause only]**

D19 said `EMBEDDING_PROVIDER` defaults to `hash` and that `HashEmbedding` must emit
L2-normalised 1024-d float vectors "so the real cosine index (D6) is exercised". That promise
is about **index coverage**, and it is kept in full. **It never said anything about ranking
quality, and six downstream tickets have been reading ranking quality out of it.**

**The scoping sentence, which matters as much as the swap.** *Exercising the index* and
*ranking the catalogue* are two different claims, and only the first was ever true of `hash`.
D19 guaranteed that a real 1024-d cosine index gets real unit vectors written into it and
really answers top-k — the write path, the width, the rescaling, the HNSW traversal, all
genuinely driven rather than stubbed. It did not guarantee, and could not have guaranteed,
that the ORDER that index returns means anything. Any ticket, test or design note that cited
D19 as authority for a retrieval ORDER was citing a claim D19 does not contain.

**What was measured** (gate landed in `342a8fb`,
`services/ingest/tests/test_embedding_ranking_gate.py`: 5 shopper queries x 3 relevant x 3
irrelevant = 45 ordered pairs, threshold `MIN_SPREAD = 0.20` on the per-query mean-relevant
minus mean-irrelevant spread):

| provider | inversions | worst per-query spread |
| --- | --- | --- |
| `hash` (the D19 default) | **28 of 45** | **-0.0250**, and all five spreads negative |
| `er.similarity.text_similarity` (positive control) | 0 of 45 | +0.4728 |
| `lexical` (the new default) | **0 of 45** | **+0.4936** |

Concretely: against the query `"running shoes"`, `hash` scores `"espresso machine"` at
`+0.063` and `"trail running shoe"` at `-0.008`. The query's own singular, `"running shoe"`,
scores `-0.035`. An espresso machine outranks a trail running shoe, and one character of
difference is indistinguishable from a different product category — because `hash_embed` is
SHA-256 bytes reshaped into floats and its cosine measures byte identity. Driven through the
real Neo4j `vector-2.0` 1024-d cosine index, `queryNodes("running shoes", k=3)` under `hash`
returns a coffee grinder, a sunscreen and an espresso machine; under `lexical` it returns the
three running shoes, in order. `hash_embedding.py`'s docstring used to claim the opposite
("a ranking measured against this provider is a ranking, not a fill") and has been corrected.

**Why this is a completion of D19 rather than a reversal.** Every constraint D19 imposed on the
default is satisfied unchanged by the replacement:

- L2-normalised, exactly 1024-d, builtin floats — the same vectors into the same D6 index, so
  index coverage is identical and the re-embed/rebuild machinery is untouched;
- dependency-free and offline — no new wheels, no model weights, no network, stdlib only, so
  C9 holds and D19's measured +679 MB / ~2.2 GB objection is untouched;
- deterministic across processes, machines and runs — SHA-256 feature placement, no `hash()`
  and no `set` iteration anywhere in the path, so it does not move with `PYTHONHASHSEED`
  (verified across separate interpreter invocations, byte-identical vectors);
- `LocalBgeEmbedding` is untouched and remains the real-model option under
  `EMBEDDING_PROVIDER=local_bge`; `make e2e-live` still sets it.

**Ruling.**

1. `DEFAULT_PROVIDER = "lexical"`. `LexicalEmbedding` lives at
   `services/ingest/src/embeddings/lexical.py` and delegates to
   `proxyshop_support.embedding.lexical_embed`, which is the single shared definition — the
   same one-definition rule `hash_embed` has, and for the same reason: two independently
   derived schemes would put seeded vectors and query vectors in different spaces and
   retrieval would score noise while both halves passed their own tests.
2. **`hash` stays registered and selectable.** Deleting it would strand the gate that measures
   it, and its byte-identity behaviour is genuinely the right instrument wherever a test needs
   two unrelated texts to land near-orthogonal. `hash_embed` itself is **unchanged, byte for
   byte** — the frozen acceptance test grades `HashEmbedding` against it, the root
   `conftest.py`'s `hash_embedding` fixture is still that function, and amending a decision is
   not licence to move the thing the decision was measured on.
3. The registry is `{lexical, hash, local_bge}` and `get_embedding_provider` still refuses an
   unknown name rather than falling back.

**What `lexical` is, in one line:** folded words and per-word padded character trigrams, each
bag hashed into 1024 signed buckets, L2-normalised separately and blended 0.6 / 0.4 — the same
split and the same weights as `ingest.er.similarity.text_similarity`, which is why its cosine
tracks that measure.

**BE CLEAR ABOUT WHAT IT CANNOT DO. `lexical` has no semantics. It compares surfaces.** It
will not match "sneakers" to "running shoes" through meaning; the example to keep in mind is
`"laptop"` against `"notebook computer"`, which scores **exactly +0.0000** — identical to an
unrelated product — so a shopper asking for a laptop retrieves nothing unless the catalogue
itself says "laptop". (`"sneakers"` scores `+0.068` against `"running shoes"`, barely above the
`+0.000` it gives `"espresso machine"`, and that margin is shared letters, not shared meaning.)
What it does handle is the plural, the hyphen, the spelling variant and the word-order change:
`"running shoes"` against `"trail running shoe"` scores `+0.509`. Closing the semantic gap needs
`local_bge` or a query-expansion step in front of retrieval. **Overclaiming here is exactly the
defect that made the previous default untrustworthy — do not repeat it.** The limitation is
asserted by tests, not just documented, so a future swap to a real model fails a test that then
tells the reader the caveat can come out.

**Why now, and not when D19 was written.** D55 made graph retrieval the spine of the product:
the shortlist is assembled by MATCHING and the shops then compete on persuasion, so an
organic result that ranks noise is not a cosmetic defect — it is the product's first step
returning the wrong shops. Under the auction framing a bad retrieval order was masked by the
bid ranking downstream of it. It is not masked any more.

**Consequence for anything already built.** A catalogue embedded under `hash` and queried
under `lexical` is a provider mismatch, which `candidate_products` already refuses loudly
(`EmbeddingProviderMismatch`, on the `EmbeddingRun` marker `reembed_products` stamps) rather
than answering with a silently re-ranked shortlist. The migration is one command —
`python -m ingest.graph.reembed` — with no index rebuild, because the width did not change.

**Two `hash` pins outside this ruling's scope that must move with it, or the swap is
config-only everywhere except where it counts** (both are HELD files at the time of writing
and are called out rather than edited): `apps/exchange/compose.yaml` sets
`EMBEDDING_PROVIDER: "${EMBEDDING_PROVIDER:-hash}"`, and
`apps/exchange/src/retrieval/sources.py:165` reads `resolved = provider or HashEmbedding()` —
a concrete provider class named at a call site, which `test_graph.py`'s
`test_no_caller_names_a_concrete_provider_class` cannot see because it scans
`services/ingest/src` only.

## D57 — A delivery promise must be EARNED before it counts; the defect was the feed, not the weight **[owner ruling, 2026-09-07]**

**The defect.** `delivery_fit` carries `w_d = 0.10` in the published rank formula, and its feed was
`Offer.delivery_estimate_days` — a number the bidding store writes into its own reply.
`features.delivery_fits` normalised those declared numbers across the auction: fastest declared
reads 1.0, slowest reads 0.0, absent reads the published neutral 0.5. So a store bought up to a
tenth of the published score by typing a smaller number, and **nothing anywhere checked whether it
had ever shipped that fast**. The repo already admitted this in
`ranking/candidates.py`'s own docstring — *"a store moves `price_value` by charging less and
`delivery_fit` by promising sooner, which is what bidding IS"* — which is true of a PRICE, because a
price is a commitment the buyer collects at checkout and the exchange reconciles, and false of a
DISPATCH ESTIMATE, because a promise costs nothing to make and the term was paid out before anyone
found out whether it was kept.

Note what this is NOT. It is not the R11 hole — the projection copies no published feature, so no
store could ever write `delivery_fit: 1.0` and be believed. The bidder was not asserting its score.
It was asserting an INPUT that the formula then converted into score at face value, which is a
different and quieter failure: every guard in the system was working exactly as designed.

**Ruling (the owner's, verbatim in substance): the ranker should absolutely read delivery — the
problem is the FEED, not the weight.** `w_d` does not move. Delivery speed is a real thing a shopper
wants and a real axis a shop can compete on, and dropping the term to close the hole would answer a
credibility problem by deleting the signal. What changes is what the term is fed:

1. A store's dispatch credibility is its `shipped_on_time` posterior, `alpha / (alpha + beta)`, off
   the `TrustDims` this exchange already holds at ranking time (`filters.trust_row` is the reader,
   reused rather than reimplemented so the R12 blacklist gate and this feed can never disagree about
   which row belongs to whom).
2. **Insufficient observations means the promise is NOT ADMITTED**: the feature is ABSENT for that
   store and therefore reads the published neutral 0.5. A store with no shipping history can neither
   win this term nor be punished by it. The floor is `MIN_DISPATCH_OBSERVATIONS = 5.0` of evidence
   mass in excess of `TRUST_PRIOR_MASS = 4.0`.

   **What that floor measures — corrected here, because the first draft of this entry said
   something false and an adversarial read caught it.** It is WEIGHTED, DECAYED evidence mass, not
   an episode count, and it could not be a count: `TrustDims` publishes exactly `alpha`, `beta` and
   `decayed_at` per dimension, so mass is the only per-dimension evidence signal a served snapshot
   carries. Five is borrowed from the manifest's `new_store_prior_n` as a MAGNITUDE and is **not
   the same measurement** — that constant counts clean episodes and drives the snapshot's
   store-level `low_data` flag. Two measured consequences, both real:

   * a missed promise is `contradicted` at weight 2.0 while a kept one is `fulfilled` at 1.0, so
     three broken promises clear the floor and four kept ones do not. That is the defensible
     direction — the floor exists to stop an UNMEASURED store being judged, not to shelter a
     measured bad one — but it is an asymmetry and it is stated rather than left to be found;
   * decay means five clean dispatches clear the floor only while fresh; spread across a half-life
     they fall back under it. A real store needs closer to ten inside a couple of half-lives.

   `test_the_admissibility_floor_reads_weighted_decayed_mass_not_an_episode_count` drives the real
   trust engine and asserts all four of those numbers, so the false version cannot be restored by
   anyone reasoning from the constant's name.
3. With a record, the quote is adjusted before the auction-normalisation runs:
   `effective_days = days / max(credibility, MIN_DISPATCH_CREDIBILITY)`, with
   `MIN_DISPATCH_CREDIBILITY = 0.25` bounding the inflation at 4x. Scale-free (a pure
   multiplication, so no absolute days-to-score curve is smuggled in — the exchange still holds no
   shipping model and `Intent.ship_to` still contributes only the fact that everyone in one auction
   quotes for the same destination), auditable (one division by one published posterior against one
   published floor, all of which a store can read off its own snapshot), and monotone **in the
   adjustment**: within that function, keeping a promise never costs a store and quoting sooner
   never costs it either.

   **Two things the bound does NOT do, both corrected from a first draft that overclaimed them.**
   The 4x cap does not guarantee an unreliable store loses; ordering depends on the quotes as much
   as the records, and the break-even is exact — a store at the floor quoting `d_bad` loses to a
   rival with posterior `p` quoting `d_good` if and only if `d_good < 4 * p * d_bad`. Against
   `p = 0.857`, a 1-day promise from a store that never keeps it is beaten by a 3-day promise and
   beats a 4-day one. A cap that instead made the unreliable store lose at every quote would not be
   a delivery comparison at all; it would be a trust term wearing `delivery_fit`'s name, which is
   the double-count this ruling exists to avoid. And the COMPOSED feature is not monotone across
   the admissibility cliff: unadmitted reads 0.5 while admitted can read 0.0, so a store whose
   credible quote would land in the bottom half of its auction is better off unadmitted, and
   earning a record can cost it up to `w_d/2`. That is the SAME incentive D13's published
   `when_absent` already creates for declaring no estimate at all — `delivery_fits` has documented
   it since before this ruling — and D57 adds a second route to the same absence rather than
   creating the incentive. Closing it means moving `delivery_fit_when_absent`, which is a published
   contract and a separate decision.
4. Everything else about `delivery_fits` is unchanged: fewer than two admissible estimates is all
   neutral, a degenerate range is all neutral, inputs are never mutated, and the answer is
   deterministic.

**Why reading `shipped_on_time` here is not double-counting `trust`.** State the overlap first,
because there is one and pretending otherwise is how this argument gets discredited later:
`trust.scoring.score` is the MEAN of the six dimensions' Beta means, so `shipped_on_time` already
contributes one sixth of the aggregate — at `w_t = 0.20`, up to `0.033` of `rank_score`. The two
readings are still different things, and the difference is structural rather than a matter of
degree:

* **They are different inputs on different objects.** `trust` is the row's scalar `score`, read in
  `scoring.feature_vector` and nowhere else; `delivery_fit` reads one dimension's Beta, in
  `features.dispatch_credibility`, and never touches `score`. Each is separately addressable, which
  is what makes the claim testable rather than rhetorical —
  `test_the_trust_term_and_the_delivery_term_move_independently` moves each while holding the other
  and asserts the other does not follow, in both directions.
* **They answer different questions.** `trust` is a LEVEL: how much of this store's whole record is
  clean, unconditional on what it did in this auction, and not movable at bid time on any horizon a
  bid can see. `delivery_fit` is CONDITIONAL on a promise made in this auction: the record is not
  added to the score, it is the exchange rate at which a promise is converted into one. A store that
  quotes nothing collects nothing from this path no matter how spotless its dispatch record — which
  is exactly what a term that merely re-credited the same evidence could not do.
* **The alternative is worse and is the actual double-count.** Leaving the feed as the declared
  number does not avoid counting `shipped_on_time` twice; it counts an UNVERIFIED assertion once at
  full weight, alongside the verified record at one sixth of another. The overlap here is the price
  of grading a claim against the evidence for that same claim, which is what
  `verified_claim_ratio` already does for product facts and what `trust.reconcile.engine` does when
  it grades a promised dispatch window against what actually shipped.

**Consequences that are real and are not hidden.**

* `RANKING_FEATURES_VERSION` moves `2.0.0` -> `3.0.0`. Same feature name, same weight, same
  published neutral, a different number — which is precisely the case that constant exists for
  (R15/S3: a replay must compare BOTH versions before claiming it reproduced a served score).
* **An exchange whose trust snapshot carries no `dims` serves `delivery_fit` absent for everybody.**
  That is the honest failure and it is deliberate: the alternative is inventing a posterior. It is
  also load-bearing on the wiring — `rank_auction` hands `attach_features` the snapshot, and a
  caller that hands nothing gets the absent path for all candidates rather than a fabricated one.

  **Named plainly, because "honest failure" is not the same as "nobody is affected": on every
  end-to-end path anyone can drive today, this term is neutral for all stores.**
  `deploy/demo/exchange-deployment.json` states a literal `trust_snapshot` whose rows are
  `{store_id, blacklisted, score}` with no `dims`, and `composition._bind_live_ranking_snapshot`
  is deliberately silent whenever a document states that key — so the demo never installs the live
  trust reader and those dims-free rows reach the ranker. The S1 e2e flow builds its snapshot with
  no dispatch history at all, so every dimension sits at the prior with mass 4 and clears nothing.
  The live wiring is correct and proven (trust's `GET /snapshot` publishes all six dims with
  `alpha`/`beta`, and `snapshot_rows`/`LiveTrustSnapshot` pass them through untouched), but the
  observable demo shows a flat term. The fix belongs to whoever owns `build_demo_deployment.py`:
  emit six Betas whose means each equal the store's existing `TRUST_SCORES` entry — the trust
  engine defines `score` as exactly that mean, so nothing new is invented — with enough mass to
  clear the floor.
* Trust observations decay toward the prior (30-day half-life, D17), so the admissibility floor is
  also a staleness gate: a store whose only dispatches are long past falls back under it and its
  promise stops being admitted. An old record is not a current promise.
* **This ruling stands on `shipped_on_time` being able to move at all, and until `f1bff27` it could
  not.** `CheckoutProvider._events` projected the accepted offer down to
  `{product_ref, unit_price, total_price, discount}` when it wrote the `accepted` ledger event, and
  `trust.reconcile` reads the dispatch promise off precisely that event — so
  `promised_delivery_days` was always `None`, `delivery_comparable` always `False`, and every store
  graded identically on the one dimension this feed consumes. A `delivery_fit` fed from that
  dimension would have read its neutral for every store forever while looking fully wired, which is
  the same defect class as the one D57 closes wearing the opposite face. Both halves are required
  and both are in the tree; neither is sufficient alone.
* **The one thing this cannot do**, written here rather than discovered later: a quote of exactly
  0.0 is the fixed point of any scale-free map, so a store with the worst possible record still
  reads 0.0 effective days if it claims same-day dispatch. That follows from scale-freedom rather
  than from an oversight, and the guard against it is not in the ranker — it is that
  `trust.reconcile.engine` grades exactly that promise every time such a store ships.

## D58 — The solicitation NAMES the product; grading it stays the auction's fact **[owner-delegated ruling, 2026-09-07; amended by its own adversarial verification before landing]**

**The defect.** `BidRequest`'s published properties were exactly
`['auction_id', 'intent', 'profile', 'respond_by']`, all four required. **It named no product.**
So the exchange rostered store X for product P, solicited X *without telling it P*, X picked a
product Q out of its own catalogue — `store_agent.runtime.bidding._Candidate.order` is
cheapest-first — and the exchange then graded X's claims about Q against the platform's snapshot
of P. Measured on the served `POST /auctions`, this repo's own hosted agents on its own demo
market, one store carrying two products and rostered for the dearer one:

```
offer.product_ref = beanie-merino-lite   claims list_price 39.00, units_left 12   (all TRUE)
roster product_ref = beanie-merino-01    the platform lists it at 78.00
components: {..., "policy_penalties": -0.15, "price_value": 0.15, ...}
```

Both numbers are wrong and in opposite directions. The penalty is a `contradicted_claim` against
a store whose every statement was true — graded against the product it actually bid, the
identical claim is `verified` with zero contradiction events. The `price_value` is a full 0.15,
the most that term can pay, credited for a discount nobody gave: `(78 − 39) / 78` is a ratio
between two different products' prices. **On that request they cancelled exactly**, so
`rank_score` matched the correct one and neither error was visible in the ranking.

**Why this is the worst defect this system can have.** D55 makes adversarial verification of the
seller's own message the thing that *justifies* the sponsored half of the market: an organic
result is the platform rendering its own crawl, and what a shop buys by joining is the right to
make its case in its own voice — safe only because that message is checked against the
platform's snapshot. A false contradiction is that mechanism firing at an honest store.

**Why the price wall did not already catch it, measured rather than assumed.**
`auction/collect.py`'s `_price_refusal` was *already* keyed by the ROSTER's `product_ref` and
looked the row up by the OFFER's, documented in its own docstring as closing exactly this. That
wall never ran, because `_is_judged` gated it and none of its five clauses was about the product.
One store, one bid, one price, differing only in whether the roster row stated a
`max_discount_pct`:

```
roster max_discount_pct: 20  -> refused, bid_price_unreconcilable, represented at 78.00
roster states none           -> ADMITTED at 39.00, penalised -0.15, credited price_value 0.15
```

`SolicitedShop.as_roster_row` — the graph-backed roster, D55's organic half — emits
`{store_id, tier, product_ref, list_price}` and **no `max_discount_pct` at all**. So the second
row is what the product served, and a field with nothing to do with which product was offered
was deciding whether the offered product was checked.

**`price_value` verdict: the same root cause, confirmed.** Not a second defect.
`attach_features` divides by `BidEntry.list_price`, which `collect_bids` reads off the ROSTER
row; when the offer names another product that denominator is about a different product from the
numerator.

---

### The ruling

**(a) The wire contract names the product.** `BidRequest` gains `product_ref` — the product this
exchange rostered the store for. `composition.HttpBidSolicitor.solicit` writes it off the same
roster row (per STORE, so it cannot ride on `for_auction`'s per-auction context), and
`store_agent.runtime.bidding._chosen` bids the solicited product **whenever it is admissible** —
admissible being `_gather`'s existing verdict and nothing new: priced, hard constraints
satisfied, in stock. Otherwise the agent's own pick stands and it counter-proposes.

Without (a) the situation is undecidable rather than merely unlucky: a shop that has not been
told what it was asked about cannot deliberately counter-propose, because a counter-proposal is
only a counter-proposal if you know what the proposal was. It also restores meaning to the
roster's own `list_price`, to `intent_match`, and to `retrieval.fit`'s join — all per-product
facts the exchange was computing about a product it never mentioned. **This is what closes the
reported defect**: the honest agent now answers about the product it was asked about, so its
true claims are checked against the platform's snapshot of that same product.

**It is a MAJOR wire change and `SCHEMA_VERSION` moves `1.0.0` -> `2.0.0`.** An earlier draft of
this entry called it minor on the grounds that the field is nullable and unrequired. That is true
and irrelevant: **the compatibility that decides a wire bump is the READER's**, and both
published readers are closed — every object in `protocol.schema.json` is
`additionalProperties: false` and the generated model is `ConfigDict(extra="forbid")`. Measured,
on a real socket, against the verbatim previous `BidRequest` model:

```
body WITH product_ref -> HTTP 422 extra_forbidden ['body', 'product_ref']
body WITHOUT it       -> HTTP 204
```

So: **deploy store agents before the exchange.** Reached the wrong way round it degrades rather
than erroring — `HttpBidSolicitor` maps any non-200 to a refusal and R10 represents the store at
list price, measured end to end as `fallback_reason: "store_refused:422"`, HTTP 201, slot still
filled — but it degrades *silently*, and what goes quiet is the whole sponsored half of the
market. `solicit` therefore omits the key entirely when the auction names no product, so only
the solicitations that actually need it can break a stale agent.

**(b) — "the platform grades and prices what was actually offered" — was built, driven through
`POST /auctions`, and WITHDRAWN. This is the part of the entry that matters.**

It is the attractive option and it was implemented in full: `graded_product_ref` resolved a
candidate's claims against the OFFER's `product_ref` when the platform's own snapshot carried
that product, guarded by `snapshot_carries` so an uncrawled ref fell back to the roster's; and
`collect_bids` took a catalogue port so the price wall could price the offered product out of
the platform's own row. The argument for it was that `offer.product_ref` "is not merely a
grading key — it is the thing the buyer BUYS", so a store naming Q to borrow Q's facts has
offered to sell Q.

**That argument is true only of the reference agent, and adversarial verification measured it
false in general.** A hosted bid is arbitrary third-party JSON; the exchange validates it against
no model on this path, and `product_ref`, `variant_ref` and `checkout_url` are three independent
store-written strings that nothing joins — the checkout permalink is minted from `variant_ref`
(`checkout/provider.py`), never from `product_ref`. Two measurements killed it:

* **Sibling laundering.** Change exactly ONE field of the bid the real hosted runtime produced —
  `offer.product_ref` to a sibling product the platform genuinely crawled — leaving price,
  variant and checkout URL alone. Before the inversion: `contradicted`, `policy_penalties:
  -0.15`. After it: **`verified`, no penalty, slot kept**, while still selling the original
  variant at the original price. That is ESC-020's lever — a store choosing which of its
  catalogue entries its claims are graded against — reopened for the store's whole crawl.
* **The bidder chooses the `price_value` denominator.** Same store, charging the same 44.00 in
  both rows: bidding its rostered product (crawled at 45) reads `price_value` 0.0079; naming a
  sibling the crawl lists at 5000 reads **0.15, the term's saturated maximum** — last place to
  first, for the same money, from one string in an unauthenticated body.

**So which product a bid's claims resolve against, and which listing its price is compared to,
stay the AUCTION's fact.** A bid about any other product is REFUSED
(`price_unreconcilable:offer.unit_price:list_price_unavailable`) and the store is represented at
its rostered list price (R10) — uniformly, which is the one thing `_answers_about_another_product`
adds to `_is_judged` and which closes the `max_discount_pct`-dependent inconsistency above.

**The condition under which (b) becomes safe, so this is a ticket and not a shrug:** the exchange
must be able to bind `offer.product_ref` to what the checkout actually sells. It cannot today —
`GraphCatalogSnapshots.as_snapshot` publishes `product_ref, canonical_name, brand, status,
attributes, offer{price,currency,availability,observed_at}` and **no variant**, and
`checkout/domain.py` checks the permalink's HOST and not its path. Give the snapshot the crawled
variant ids per product and require a hosted offer's `variant_ref` to be one of the offered
product's, and the graded subject and the purchased subject become the same object *in fact*
rather than by assumption about the bidder's implementation. Then (b) can land as written.

**(c) — constraining the agent to bid the rostered product — is rejected.** It closes the defect
by deleting the behaviour: the shop becomes a price quoter for a product the platform picked, and
the sponsored slot stops being "argue your case in your own voice" and becomes "answer this
form". It is also strictly weaker as a *guard*, because a constraint on the wire is enforced by
whoever runs the agent and a Tier-2 store does not run our runtime. (a)'s agent-side default —
bid what you were asked about when you can — is deliberately NOT (c): it is freely overridable,
and the exchange decides what to do with the override on its own terms.

### What the honest counter-proposer actually gets, stated rather than sold

Represented, not ranked. Asked about a product it cannot sell today, its advocate answers about
another one truthfully; the exchange refuses to rank that answer and represents the store at its
rostered list price. **What it no longer does is accuse it**: no claim of that store's is graded
against a product it did not bid, so there is no `contradicted` verdict, no `contradicted_claim`
event and no `policy_penalties` — and the fabricated `price_value` is gone too, reading the
honest 0.0 instead of 0.15. That is a real cost to a behaviour the product wants, it is the price
of not being able to bind the ref, and the paragraph above says exactly what would remove it.

### The one thing a refusal buys a liar, and why it is not new

A refused bid becomes an R10 fallback, a fallback carries no claims, and no verdict is minted
about it — so declining suppresses a contradiction. Measured, that is the SAME thing every store
has always been able to do for free:

```
bids, and lies         -> fallback=False  claims kept, graded, penalised
does not answer at all -> fallback=True   reason=no_response              claims=[]
answers 204 / declines -> fallback=True   reason=response_carried_no_bid  claims=[]
```

The exchange's count of contradictions has always been suppressible by the party being measured,
at the price of the auction. D58 adds a third way to decline; it adds no cheaper one. **Closing
that is a separate decision** — it means grading the claims on a bid whose PRICE was refused,
which today is deliberately not done because a fallback asserts nothing and must satisfy no hard
constraint.

### Two residuals this ruling does NOT close, named rather than left to be found

* **`intent_match` is keyed by STORE.** `ShopRoster.intent_match_by_store` is
  `{store_id: intent_match}`, so a shop rostered for P at fit 0.9 keeps 0.9 whatever it bids — on
  `w_m = 0.35`, the largest published term. It does not bite today, because a bid about another
  product is refused and a fallback is the rostered product; it becomes live the moment (b)
  lands, and it is part of (b)'s ticket. The honest answer for an unmeasured product is the
  published `INTENT_MATCH_WHEN_ABSENT` neutral, not an inherited number.
* **The `bid_placed` receipt reads `fit_unavailable` for a refused counter-proposal.**
  `retrieval.fit.annotate_bid_payload` joins on `offer.product_ref` and `ShopRoster.assessments()`
  filters to rostered products. That is the truthful record — the retrieval did not measure that
  product — and it is the same `fit_unavailable` a request-stated roster already writes.

### What was proven, in both directions, on one served request

`apps/exchange/tests/test_counter_proposed_product.py` drives `POST /auctions` with this repo's
real hosted agents against a graph-shaped roster and asserts, on the same request: two honest
stores admitted, **every decidable claim `verified`, zero `contradicted_claim` events, no
`policy_penalties`**; and a genuinely dishonest store — its live feed says thirty units left, the
platform's crawl of the same product says two — **still `contradicted`, still one
`contradicted_claim` at the published 0.15, still `policy_penalties: -0.15`.** Both withdrawn-(b)
attacks are asserted closed in the same file, so the deletion cannot be silently undone.

## D59 — `in_stock` and `availability` are one BINARY fact, and a stock reading EXPIRES **[owner ruling on scope, 2026-09-07]**

**The defect.** `in_stock` is what every producer of a stock claim in this tree writes — the
hosted agent's live feed (`store_agent.runtime.bidding.IN_STOCK_KEY`), the pitch decomposer's
two flag rules, the live-page reader. `availability` is what the crawl writes, as a closed
vocabulary token, onto the graph `Offer`. They are two names for one fact and **nothing joined
them**: `claim_verification.verifier._lookup_attribute` matches on `str(key)` with no folding,
and the crawl emits `store`/`product`/`sells`/`category`/`media`/`variant`/`offer` ops and no
`attribute` op at all, so the `attributes` block is empty for all 3,093 products of the
recorded corpus. Every stock claim therefore resolved nowhere: `unsupported` from the verifier,
rewritten to `ambiguous` by `attest_candidate_claims` as the exchange's own gap
(`UNDECIDABLE_KEY_REASON`), and `ambiguous` is in `UNDECIDED_STATUSES` — it moves no dimension
mean, earns no `verified_claim_ratio` gain and carries no `contradicted_claim` penalty.

Measured on the served `POST /auctions`, four stores on a crawl-shaped catalogue, every one of
them claiming `in_stock: true`, and the platform's own crawl of the same four products saying
one thing for the first and the opposite for the other three:

```
claim_verdicts: {store-honest: ambiguous, store-liar: ambiguous,
                 store-restocked: ambiguous, store-prose: ambiguous}
policy_events:  []
ranked:         all four at rank_score 0.46499999999999997, byte-identical components
```

**The lie was exactly as expensive as the truth, to the decimal.** That is the D55 mechanism
failing in the direction opposite to D58's: D58 was the adversarial check firing at an honest
store, and this is it not firing at all — and D55 is explicit that the check is what *justifies*
letting a sponsored shop speak in its own voice. A stock claim is the most operationally
consequential thing a seller says (the buyer is sent to a checkout), and it was the one claim
the platform could not check.

**The owner's ruling on scope, and it narrows this a great deal:** *"it's not our job to
perfectly track their inventory. We just need to know if the product is in stock or not."*

---

### The ruling

**(a) The platform publishes a DERIVED binary reading, under its own name for it.**
`claim_verification.verifier.stock_reading` reads the offer block's `availability` token
through `IN_STOCK_BY_AVAILABILITY` and answers `{value: bool, observed_at: …}`;
`_lookup_attribute` resolves `in_stock` to it and `catalog_keys` reports it, so the two cannot
disagree about what is decidable — the rule that function was written beside.

**This is deliberately not a synonym table, and the distinction is load-bearing rather than
verbal.** `retrieval/catalogue.py` states the rule that forbids one — *"the platform states what
it observed under the name it observed it under"* — and it is right: renaming a crawled
attribute makes a verdict cite evidence under a name the crawl never used. So `availability`
keeps its spelling, its verbatim token (`observed_value: "in_stock"`) and its own verdict, and
stays UNTYPED because typing it would route it to a dimension nobody approved. What is added
beside it is a second, derived fact, computed from a token whose meaning the platform itself
defined in `ingest.adapters.mapping.coerce_availability`. The derivation is also LAST in
`_lookup_attribute`'s order, after all three real blocks, so a snapshot that states an
`in_stock` of its own — the demo document and the S1 fixture both do — is answered by what it
states and never by a derivation.

**A token with no binary reading yields no fact at all**, which is where the whole of the
conservatism sits and each case is deliberate. `preorder` and `backorder` describe a purchase
accepted now and filled later, so "is it in stock" is not a question they answer.
`discontinued` says the line is no longer made, which the crawl's own vocabulary keeps SEPARATE
from `out_of_stock` precisely because a discontinued line can still have units on the shelf.
`unknown` is what `coerce_availability` assigns to a token it did not recognise, and reading the
platform's own parser gap as "out of stock" would manufacture a contradiction out of nothing.
Each resolves to `unsupported` — absence of evidence — and never to `contradicted`.

**(b) A stock reading EXPIRES, and that is what makes (a) safe rather than reckless.** An
earlier analysis warned that a bare alias would grade an honest seller `contradicted` whenever
its stock moved since the crawl, and that warning is correct: the restocked seller and the liar
send byte-identical bids and the platform's record says `out_of_stock` for both. The binary
reading alone cannot tell them apart. What tells them apart is how old the reading is.

`_is_stale` already existed and already named this exact case in its own docstring — *"a
six-week-old snapshot's `roast_level` is still evidence… its `availability` is not: that is live
state"* — and it could not fire, on **three** independent counts. An adversarial verification of
the first draft of this entry measured all three; the first draft claimed the rule worked and it
did not, so what follows is what it took to make the sentence true rather than what it took to
write it.

1. **The offer block is bare scalars**, so `availability` was not a Mapping and `_is_stale`
   returned at its first line. Closed by returning the live-state keys — and ONLY those — in the
   `{value, observed_at, stale}` attribute shape. Without this a store claiming
   `availability: "in_stock"` banked a `verified` off a day-old reading while the same store
   claiming `in_stock: true` got `unsupported`: a spelling-shaped hole in the floor. `price` and
   `currency` stay bare scalars, for the reason the offer block was written that way.
2. **There was no reference instant outside the snapshot.** `_is_stale` measured against
   `captured_at`, and `captured_at` is `entry.observed_at`, which `graph.query.catalogue_entry`
   computes as `latest_instant(store, product, attributes, offer)` — **the offer's own stamp is
   one of the maxands.** So the quantity being compared was "is this reading older than the
   newest thing in the same document", which for one crawl pass is exactly zero however old the
   pass was. Measured through the real `catalogue_entry` → `as_snapshot` → served
   `POST /auctions`: an honest restocked store was **`contradicted` at 0h, 1h, 1d, 7d, 30d, 90d
   and 365d alike**, `policy_penalties: -0.15` at every age. On the recorded corpus that is 195
   of 3,093 products (6.3%) — real stores, payable. Closed by `GraphCatalogSnapshots.as_snapshot`
   stamping **`read_at`**, the moment this exchange READ the graph, which `_is_stale` prefers
   over every reference inside the document. The clock is read by the I/O adapter and never by
   `verify`, which stays the pure function its docstring promises.
3. **`Offer.observed_at` is a last-CHANGED stamp, not a last-CONFIRMED one.**
   `ingest.adapters.mapping.build_upserts` does not rewrite an unchanged product's Offer, so a
   shelf the crawler re-read this morning and found unchanged still carries the stamp of
   whenever it last moved — measured at 12,600s of drift against a 3,600s window after a single
   re-crawl, growing without bound while the store churns other SKUs. Reading it as the
   confirmation time would have retired the grading of every well-behaved store while leaving
   the churning ones graded, which is the inverse of the point. So the derived reading's
   timestamp is the snapshot's `captured_at` — the latest platform observation behind the entry,
   i.e. *the crawl looked, and found this* — and never the offer's own.

**The quantity the floor now measures is therefore `read_at - captured_at`: how long it has
been since the platform last looked at this shelf, at the moment it grades a claim about it.**
That is the only honest reading of "is our evidence current", and it is the one an operator can
act on — if it is too large, the answer is to crawl more often, not to grade more bravely. A
snapshot with no `read_at` — every hand-authored deployment document, devstack market and
fixture in the tree, none of which carries an offer `observed_at` either — falls through to the
old references and is taken as current, which is exactly what a stated document asserts.

**And a reading the platform could not keep current costs the store nothing**, which is a
separate guard and was also missing. `catalog_keys` drops `in_stock` from the vocabulary when
the reading is stale, so the exchange rewrites the verdict to `ambiguous` — its OWN gap — rather
than letting a real `unsupported` land in `verified_claim_ratio`'s denominator. Without that,
measured on a buyer who asks about stock, the honest restocked store read `rank_score` **0.365
against 0.465 for a store that said nothing at all**: a direct incentive not to make true stock
claims. A gap on the platform's side must not be a cost on the seller's.

**The derivation may not launder a stale flag either.** The snapshot's own
`attributes["availability"]` row, when it has one, is what the derivation reads — it timestamps
and flags ITSELF — and `stale` is additionally inherited from the product and the snapshot.
Measured on this repo's own stale-evidence gate, `fixtures/golden/golden_set.json`'s
`gp-002-stale-evidence`, which carries `stale: true` at snapshot, product and attribute level
beside an unflagged copy in its offer block: before this, `availability` correctly answered
`unsupported` while `in_stock` answered **`verified`** off a June reading. One document, one
fact, opposite verdicts, and the seller credited with the fresh confirmation that fixture exists
to deny — its own rationale being *"Stale evidence must not be silently treated as fresh
confirmation."*

**Where that window comes from matters more than its value, because it is not a number this
ruling invented.** `ingest.scheduler.cadence.DEFAULT_CADENCE` already publishes
`FieldCadence(field="offer.availability", section=PRODUCTS, max_age_seconds=3_600)`: the
platform has committed in writing that an availability reading older than an hour is due for
re-reading. **Holding its own GRADING to the standard it set for its own CRAWLER is the entire
rule**, and `services/ingest/tests/test_refresh.py::test_the_verifiers_stock_freshness_floor_matches_this_services_own_cadence`
gates the two definitions against drift in both directions — loosen the cadence and the verifier
starts grading readings the crawler has given up on; tighten it and every stock claim quietly
becomes `unsupported` again, which is the hole this entry closes.

An operator's own `freshness_window_days` still applies, and the **tighter of the two wins in
both directions**. An operator who says "nothing older than an hour is evidence here" has bound
the stock fact too; an operator who says fourteen days has not thereby declared a fortnight-old
shelf current, because a policy written for roast levels must not reopen this.

The measurement is against the snapshot's own `captured_at` rather than a wall clock, so
`verify` stays the pure function its module docstring promises. What it asks is "was this
reading already an hour behind the rest of what the platform knew about this product when it
published the snapshot", not "how long ago was that" — deterministic, replayable, and no clock
inside a verdict.

**(c) The claim is typed `specifications`, which invents no dimension.** An untyped verdict is
announced to nobody — `claim_verdict_payload` returns `None` and `trust.scoring.claim_dimension`
raises — so a decidable claim that stayed untyped would be a verdict the trust engine never
sees. `specifications` is already in the human-approved `claim_type -> dimension` table (D18)
and routes to `catalog_claim_accuracy`, and it is **already** what `pitch._FlagRule` mints for a
stock reading read out of prose. So `KEY_CLAIM_TYPES["in_stock"] = "specifications"` only makes
the ASSERTED claim agree with the prose one, which the exchange's own rule already required: *"a
claim read out of prose earns and costs exactly what an asserted one does"*. `availability`
stays out of that table, unchanged.

---

### What was rejected

* **The naive alias** — folding `in_stock` onto `availability` and handing the pair to
  `compare`. Measured through the real comparator:

  ```
  claimed True        vs catalog "in_stock"      -> contradicted   ("the normalised values differ")
  claimed True        vs catalog "out_of_stock"  -> contradicted
  claimed "in_stock"  vs catalog True            -> verified
  ```

  The honest store is punished **on the happy path**, and the failure is asymmetric — the two
  spellings were never comparable in the direction the crawl actually writes them, because
  `isinstance(catalog, bool)` is what selects the boolean comparator and a crawled availability
  is a string. Repairing that much still convicts every restocked seller, so the alias is
  rejected on its own merits rather than merely improved upon: (b) is not decoration on (a), it
  is the half that makes (a) admissible.
* **A general synonym table in `_lookup_attribute`.** `list_price`→`price` is the obvious next
  entry and it is a different decision with a different false-positive profile (a price moves on
  a merchant's schedule, not a shopper's). One derived fact, published under the platform's own
  name, with its own expiry, is a ruling; a synonym table is a standing invitation to add the
  next pair without arguing for it.
* **Quantity, `units_left`, and any reconciliation of counts.** This is the owner's scope line
  and it is kept literally: `IN_STOCK_BY_AVAILABILITY`'s values are exactly `{True, False}`, and
  a third outcome would be an inventory model wearing a boolean's name. `units_left` still
  resolves only where it always did — the `attributes` block — and is untouched, which also
  leaves D58's dishonest-store measurement (a live feed saying thirty against a crawl saying
  two) grading exactly as it did.
* **Making the crawl write `AttributeValue` nodes for stock.** It would work and it is the wrong
  place: the graph `Offer` already holds `availability` with its own `observed_at` and its own
  `Source` provenance, and a second copy of the same fact on a different node is two records
  free to disagree about one shelf. `mapping.build_upserts` is unchanged.
* **Re-stamping `Offer.observed_at` on every crawl pass**, which is the obvious way to turn a
  last-CHANGED stamp into a last-CONFIRMED one and would let the floor read the offer's own
  time. It is rejected because it breaks a property this repo already gates: T-024's "no
  re-extraction without a hash change across a full cycle", which is what
  `ingest.adapters.mapping.build_upserts`' early return exists to satisfy. Writing every
  unchanged Offer on every pass to make one timestamp meaningful is paying in write volume and
  in a frozen acceptance property for something `captured_at` already says.
* **A third, "partially available" verdict.** R18's vocabulary is closed (four statuses) and
  `unsupported` already means precisely "the catalog says nothing that decides this".
* **Moving `RANKING_FEATURES_VERSION`.** This is the opposite call from D57 and the reason is
  stated at the constant: D57 bumped it because `delivery_fits` ITSELF was redefined, whereas
  nothing in `ranking/features.py` is redefined here. `verified_claim_ratio` and
  `policy_penalties` compute exactly what they computed before, from the verdicts they are
  handed; what changed is the EVIDENCE, and the evidence carries its own version — in memory
  and under its MAC, though not yet on any durable record, which is named as a residual below.
  `DEFAULT_VERIFIER_VERSION` moves `verification/1.0.0` -> `verification/2.0.0` instead — MAJOR,
  because the change is not additive (a claim that cost nothing can now cost the published
  `contradicted_claim` penalty), and because `verification_key` names that constant exactly so a
  verdict minted under the old comparator is re-verified rather than inherited. No contract
  changes: `Claim.key` is free text, no enum gains a member, and nothing on the wire moves.

### Residuals, named rather than left to be found

* **The offer block is ONE listing, and the stock reading is that listing's.**
  `graph.query.catalogue_entry` picks the CHEAPEST fully provenanced offer so that the
  snapshot's price and the roster's quote are the same observation. A product whose cheapest
  variant has sold out while a dearer one has not therefore reads out of stock. This is the
  honest residual and the alternative is worse: an offer block pairing one variant's price with
  another variant's shelf is facts about two objects graded as one, which is D58's defect class
  exactly. Closing it is the variant-binding ticket D58 already deferred — *"give the snapshot
  the crawled variant ids per product"* — and this fact is now a second reason to do it.
  **Sized, so it is not filed as theoretical:** replaying `fixtures/real-catalogs` through the
  real `coerce_availability`, 569 of 3,093 products (18.4%) have a cheapest variant reading
  `out_of_stock`, and 195 of those (6.3% of the corpus) have another variant genuinely in
  stock. Those are the rows where an honest store's claim is graded against a shelf that is not
  the one it is selling from. The freshness floor does not help them — their reading is
  perfectly current, it is about the wrong variant — so this residual, not (b), is the largest
  remaining source of a wrong stock verdict.
* **The window is only as good as the crawler.** `read_at - captured_at` is a real age, and it
  says the platform stops being able to grade stock the moment it stops crawling on its own
  published cadence. That is the correct direction — the right to accuse rests on the duty to
  observe — but it is a live operational coupling and it is stated here rather than discovered:
  a stalled crawler silently retires this check for every store, and the symptom is `ambiguous`
  verdicts rather than an error anywhere.
* **The drift gate pins the DEFAULT cadence table, not the deployed one.**
  `test_the_verifiers_stock_freshness_floor_matches_this_services_own_cadence` reads
  `DEFAULT_CADENCE`, while a deployment can retune `offer.availability` through
  `CadenceConfig.from_env` (`PROXYSHOP_INGEST_CADENCE`). An operator who does that moves the
  crawler and not the verifier, and the gate stays green.
* **No DURABLE record carries the verifier version**, which weakens — though it does not
  overturn — the argument above for leaving `RANKING_FEATURES_VERSION` alone. `contracts.ledger`
  pins `claim_verified` to `(claim_ref, status, dim)` and `rank` publishes only
  `ranking_versions: {weights, features}`; the attested claim that carries the version never
  reaches a published surface. So an auditor replaying a recorded auction cannot today tell a
  `1.0.0` verdict from a `2.0.0` one. The fix is a field on the frozen `claim_verified` payload,
  which is a contract change and a separate decision; naming it is what stops the version bump
  above from reading as a guarantee it does not yet deliver.
* **A store that declines still mints no verdict**, so declining still suppresses a stock
  contradiction at the price of the auction. D58 already recorded that every store can do this,
  and D59 adds no cheaper route to it.

### Landed alongside, and closing the same class

`ranking/filters.claimed_attributes` -> `decided_attributes`. The auction's relaxation counted
every attribute key any claim NAMED, whatever verdict it carried, and relaxation is decided ONCE
for the whole auction — so a store making a TRUTHFUL claim on a key no catalogue declares got
`ambiguous`, that `ambiguous` suppressed the relaxation, and **every store in the auction came
back excluded, including the ones that claimed nothing.** Measured before the fix, one honest
sentence from `store-a` and silence from `store-b`: `relaxed_constraints: []`,
`shortlist_slots: []`, both stores excluded `hard_constraint_unsatisfied`. The rule is now the
VERDICT: `verified` or `contradicted` proves the key was answerable, `ambiguous`/`unsupported`
are this exchange reporting its own gap, and a forged or absent attestation is inert in both
directions — the auction reaches the outcome it would have reached had that store said nothing,
which is a stronger ESC-020 property than the old width had.

**That alone did not close it, and the second half is a sibling defect the same adversarial
verification found.** `declared_attributes` read the auction's vocabulary out of `catalog_units`,
which walks the `attributes` block ALONE, while `verify` decides a claim from four places — that
block, the `offer` block, the product record itself, and now the derived `in_stock` reading. So
for every key in the other three classes the exchange could decide the constraint while telling
the relaxation it could not, and the outcome then turned on whether a bidder chose to claim the
key. Reproduced on the **shipped** `deploy/demo/exchange-deployment.json`, unmodified, using
`canonical_name`:

```
gaiaherbs says NOTHING                     -> relaxed=['canonical_name']  shortlist=[gaiaherbs, toniiq]
gaiaherbs claims canonical_name (falsely)  -> relaxed=[]                  shortlist=[]
```

One store's single false claim, and the buyer is shown nothing — by the same store-controlled
lever, one layer down. `declared_attributes` now reads `claim_verification.verifier.catalog_keys`,
which is the function that answers its own question, so the vocabulary the relaxation trusts and
the vocabulary the verifier grades against can no longer disagree. Both halves are required;
neither is sufficient alone.

**And a third fix this ruling made necessary rather than found: the pitch decomposer's stock
rules.** `pitch._FlagRule` matched `out of stock|sold out|back[\s-]?ordered` anywhere in a
sentence, with no reading of negation, tense, condition or scope. That was inert while `in_stock`
was outside every crawl-shaped vocabulary — the reading was rewritten to `ambiguous` and cost
nothing — and making the key decidable turned it into a published `-0.15` against sentences that
are true. Measured against a catalogue that AGREES with the store: *"We never let this one go out
of stock"*, *"It has not been sold out since spring"*, *"Sold out twice last month; back on the
shelf now"*, *"If it does go out of stock we will tell you within the hour"* and *"Back-ordered
orders ship separately at no extra cost"* each minted `in_stock: False` and graded
`contradicted`; *"The 1 kg bag is sold out, but the 250 g is ready to ship today"* minted BOTH
values of one key, so the store was `verified` and `contradicted` at once and paid. The rules now
DECLINE a negated, conditional, time-shifted, package-scoped or attributive match rather than
inverting it, and a pitch that reads one equality-graded key two incompatible ways mints neither.
`PITCH_EXTRACTOR_VERSION` moves `1.0.0` -> `1.1.0`. The general lesson is the one worth keeping:
**a rule that costs nothing is not a rule anyone has tested**, and this ruling's real work was
finding the three places that had been silently free.

### What was proven, in both directions, on one served request

`apps/exchange/tests/test_stock_claim_served.py` drives `POST /auctions` against a crawl-shaped
catalogue — empty `attributes`, an offer block carrying the availability token, and the `read_at`
stamp saying when this exchange read the graph — with four stores all claiming `in_stock: true`:

```
store-honest     in_stock      crawled 20 min before the read -> verified      no penalty, slot kept
store-restocked  out_of_stock  crawled 24 h   before the read -> ambiguous     no penalty, slot kept
store-liar       out_of_stock  crawled 20 min before the read -> contradicted  policy_penalties -0.15
store-prose      out_of_stock  crawled 20 min, claimed in PROSE -> contradicted  policy_penalties -0.15
rank_score: 0.465 / 0.465 / 0.315 / 0.315      penalised == {store-liar, store-prose}
```

`store-liar` and `store-restocked` send byte-identical bids against byte-identical availability
tokens, and every Offer node in that request carries the same ancient `observed_at`. They are
graded oppositely on ONE quantity — how long ago the platform last looked — which is the whole
of (b) stated as one measurement. `packages/verification/tests/test_stock_fact.py` grades the
rule itself: every token's reading, both freshness directions, that the offer's last-changed
stamp is never read as an age, the tighter-window rule, the stated-attribute precedence, the
stale-flag inheritance, that a stale reading leaves the vocabulary rather than costing the
store, and that `availability` keeps its own verbatim verdict and stays untyped.

**And the same pair is driven through the REAL adapter**, not only through a fixture shaped like
its output — `test_on_the_real_adapter_a_fresh_crawl_decides_and_a_stale_one_does_not` builds two
snapshots from `GraphCatalogSnapshots.as_snapshot` over a real `CatalogueEntry` that differ in
one field, when the platform last crawled the shelf, and asserts the key is decidable in one and
absent from the vocabulary in the other. That test exists because the first version of this
ruling passed its hand-written fixtures and was inert on the object the exchange actually
grades against.
