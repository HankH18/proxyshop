<!-- PROVENANCE — read this before trusting anything below.

This file is DERIVED, not authored. It was extracted by AST-parsing the nine frozen
acceptance test files under .swarm-loop/acceptance/ after they were finalised, and
independently re-checked against them: 35 product modules, 76 symbols, zero missing.

It is a MIRROR of the frozen suite, and the suite is the authority. If the two ever
disagree, the suite wins and this file is stale — regenerate it rather than edit it.
Editing this file changes nothing about what the goals require.

WHY IT EXISTS: every module path, callable, keyword argument, attribute and constant
the frozen suite imports became an immutable contract the moment `freeze` hashed the
suite. A worker that builds the right behaviour under a different name does not fail
loudly — its test keeps raising ModuleNotFoundError or AttributeError, which at cycle 0
is indistinguishable from "not built yet". That goal then stays red for the rest of the
run with nothing to point at. Paste the relevant ticket's block into its task packet.

Symbols marked 🆕 are names the suite's author CHOSE — they appear in no SPEC, DESIGN or
tickets.json text, so they are the ones a worker is most likely to name differently.
-->

# The public surface the frozen acceptance suite requires

Generated from the **frozen source** at
`/Users/hankholcomb/Documents/code_parent_folders/gauntlet_repos/proxyshop/.swarm-loop/acceptance`
(9 test files, 103 marked tests), by parsing every file with `ast` and extracting every
in-body import, every attribute read off an imported symbol, every call arity and keyword,
and every constant whose exact value the suite asserts. **Not** derived from the acceptance
plan — the plan's appendix is incomplete (audit F6).

Every name below is an **immutable contract**. A worker who spells one differently makes the
goal permanently unreachable; the suite is hashed and may not be edited.

Legend:

* 🆕 = **author-chosen name.** It appears nowhere in `SPEC.md`, `DESIGN.md`, `TASKS.md`,
  `EXECUTION.md` or `tickets.json`. A worker reading only the ticket packet will not guess
  it. These are the highest-risk names in the whole contract.
* ⚠️ = a disagreement between the test's `ticket()` marker and the ticket whose `scope`
  glob actually permits writing the file.

---

## 0. Rules that apply to every module below

1. **Import path is by repo-root dotted path.** `conftest.py` puts the repo root on
   `sys.path` itself. `apps/exchange/src/ranking.py` (or `.../ranking/__init__.py`) is
   imported as `apps.exchange.src.ranking`. Every intermediate directory must be an
   importable package or PEP-420 namespace portion.
2. **Hyphenated trees are aliased with underscores by `conftest.py`, and only these three:**
   * `packages.store_agent`  → `packages/store-agent`
   * `apps.seller_reference` → `apps/seller-reference`
   * `services.shopify_stub` → `services/shopify-stub`
   The directory names on disk keep their hyphens (the scope globs match on those). Do not
   rename the directories, and do not add your own aliases.
3. **Return shapes are read tolerantly.** Every file carries a `_get`/`_field` helper that
   accepts either a mapping key or an attribute. So a `dict`, a dataclass and a pydantic
   model all satisfy "returns `{ok, reasons}`". What is **not** tolerant: the *field name*.
4. **Nothing is read from a wall clock, a socket, or a live datastore.** Where a reference
   instant is needed the suite injects it (`now=`, `as_of=`, `_config()["now"]`). Several
   tests monkeypatch `socket` to raise; product code that opens a socket on those paths fails.
5. **Collections are compared exactly** (`==` on a set), not by containment, wherever a
   vocabulary is named below.

---

## T-000 — Monorepo skeleton
scope `**` · 4 tests, all in `test_spec_criteria.py`

No product imports. This ticket is graded on the **filesystem**.

### Directories that must exist (exact, `is_dir()`)
```
apps/buyer          apps/exchange        apps/trust           apps/merchant
apps/seller-reference
packages/contracts  packages/store-agent packages/verification packages/llm
services/ingest     services/shopify-stub services/sim
pixel               db/migrations        e2e                  fixtures
```

### Must NOT exist
* `packages/protocol` — and the literal strings `packages/protocol` / `packages.protocol`
  must not appear in **any** text file under
  `apps, packages, services, pixel, db, e2e, fixtures, graph, evals`
  (suffixes scanned: `.py .ts .tsx .js .jsx .mjs .cjs .json .toml .cfg .ini .yml .yaml .sql .md .mdx .txt .sh .env .graphql .prisma` and extensionless).

### docker-compose (a `docker-compose*.y*ml` at repo root)
* Top-level `services:` block.
* A service matching each of `postgres`, `neo4j`, `redis` **by service name or `image:`**,
  and **each of those three declares a `healthcheck:`**.
* Four **distinct** services whose names contain `buyer`, `merchant`, `exchange`, `trust`.

### Repo-root `Makefile`
Must exist and must define every `make <target>` the demo runbook names — including
`e2e-live` (see T-085).

---

## T-010 — `packages.contracts`
scope `packages/contracts/**` · 8 tests (7 in `test_e1_foundation.py`, 1 in `test_spec_criteria.py`)

```python
import packages.contracts as contracts
from packages.contracts import Bid, Intent, LedgerEvent, Provenance, validate_bid
```

### The 14 pinned protocol objects — exported from `packages.contracts` as **classes**
```
Intent  BuyerProfile  BidRequest  Claim  Provenance  Offer  Bid
VerificationResult  Shortlist  LossReport  LedgerEvent  TrustSnapshot
TrustEventPayload  Envelope
```
Each must:
* construct from its DESIGN §Interfaces payload via `Cls(**data)`, `Cls.model_validate(data)`
  or `Cls.parse_obj(data)`;
* serialize via `.model_dump()` or `.dict()` (dataclasses also accepted);
* **round-trip** dump → `json.dumps` → load with `==` equality intact (`Bid`, `LedgerEvent`);
* **reject `{}`** — an empty payload must raise.

### Closed enums (rejecting an out-of-set value is asserted)
* `Provenance.source` — rejects `"made-up-source"`. Values used across the suite:
  `owner_statement, envelope_rule, learned_policy, pixel_feed, network, scraped, seller_asserted`.
* `Envelope.activation` — exactly `shadow | active | killed`; rejects `"whenever"`.
* `Intent.hard_constraints[].op` — rejects `"regex"`; must accept `eq, lte, gte, in, contains`.
* `Intent.preferences[].direction` — rejects `"sideways"`; must accept `maximize, minimize, prefer`.
* `Intent.hard_constraints[]` **must reject a `weight` key** (R19: a filter is not a score).
* `Intent.preferences[]` carries a numeric `weight`.

### 🆕 `LedgerEventKind` — the 18-member vocabulary, asserted for **exact set equality**
Preferred: an enum exported as `LedgerEventKind` (`LedgerEventKindEnum` also read). Fallback:
a `typing.Literal` / enum annotation on `LedgerEvent.kind` readable through
`model_fields`/`__fields__`/`get_type_hints`.
```python
{
  "bid_placed", "shown", "accepted", "code_created", "checkout_redirect",
  "checkout_pixel", "order_paid", "order_fulfilled", "refund", "feedback",
  "reconciled", "claim_verified", "policy_event",
  "auction_opened", "auction_closed", "offer_integrity",
  "blacklisted", "blacklist_expired",
}
```
No more, no fewer.

### 🆕 `validate_bid` — the dual-path boundary
```python
validate_bid(bid_dict, *, path: str, trust_snapshot: dict) -> result
# path is exactly "hosted" or "external"
# trust_snapshot is {store_id: {"store_id":…, "score": float, "blacklisted": bool}}
```
Result fields read: 🆕 `ok` (bool), 🆕 `reasons` (non-empty iterable when `ok is False`),
🆕 `requires_verification` (bool).

| case | `path` | `ok` | `requires_verification` |
|---|---|---|---|
| claim with `provenance.source == "seller_asserted"` | `hosted` | **False** | – |
| same claim | `external` | **True** | **True** |
| all claims hook-provenanced (`owner_statement`) | `hosted` | True | – |
| all claims hook-provenanced | `external` | True | **False** |
| claim with **no** `provenance` key | both | False | – |
| claim with `provenance.source == ""` | both | False | – |
| `offer.expires_at` in the past | both | False | – |
| store blacklisted in `trust_snapshot` | both | False | – |

### Field names that must survive construction and round-trip
`Intent`: `intent_id, cluster_id, query, category, hard_constraints[].{field,op,value},
preferences[].{field,direction,weight}, ship_to, currency, budget_band, created_at, schema_version`
`BuyerProfile`: `pseudonym, buckets.{budget_band,category_affinity,frequency_tier,region,first_time}`
`BidRequest`: `auction_id, intent, profile, respond_by`
`Claim`: `key, value, provenance`
`Provenance`: `source, ref, observed_at, authority_rank`
`Offer`: `product_ref, unit_price, discount.{type,value,provenance}, commitments, total_price, expires_at, checkout_url`
`Bid`: `auction_id, store_id, offer, claims, message, agent_version, signature, schema_version`
`VerificationResult`: `pitch_ref, catalog_snapshot, claims[].{claim_ref,status,observed_value,evidence_refs,confidence}, verified_claim_ratio, verifier_version`
`Shortlist`: `auction_id, slots[].{slot,bid_ref,fit_score,trust_summary,provenance_labels}`
`LossReport`: `store_id, window.{start,end}, by_cluster[].{cluster_id,lost,reasons,unmet_criteria}`
`LedgerEvent`: `event_id, ts, kind, auction_id, store_id, order_ref, payload` (payload keeps arbitrary nesting)
`TrustSnapshot`: `store_id, score, dims.<dim>.{alpha,beta,decayed_at}, blacklisted`
`TrustEventPayload`: `store_id, event, dim, delta, pseudonymous_context.{cluster_id,pseudonym}`
`Envelope`: `store_id, version, floors[].{product_ref,min_price}, max_discount_pct, budget_cap, pursue_clusters, standing_commitments, activation`

**Consumed by other tickets:** `packages.contracts.Provenance` is constructed with
`Provenance(source=, ref=, observed_at=, authority_rank=)` inside `test_e2_ingestion.py`
(unmarked helper) and `test_e7_buyer.py` (marker T-072). Both wrap it in `try/except`, so a
missing contracts package degrades rather than errors — but the kwarg spelling is fixed.

---

## T-011 — Postgres schemas, roles, ledger chain
scope `db/migrations/**`, `apps/trust/src/ledger/**`, `apps/trust/tests/**` · 2 tests in `test_spec_criteria.py`

No product import in T-011's own two tests. Graded statically:

### `apps/exchange/**/*.py` — import + literal scan (non-test files)
* No `import`/`from` whose dotted name contains `envelope` or `sealed` (case-insensitive).
* No **string literal** matching `/sealed\.|envelopes/i`. (Test files under `apps/exchange`
  are exempt from the literal scan only, never from the import scan.)
* `apps/exchange` must exist and contain at least one `.py` file, or the test fails.

### `db/migrations/**/*.sql`
* At least one `.sql` file.
* Statements matching `/\bexchange[a-z0-9_]*\b/` and containing `grant`:
  * must not mention `sealed` or `vault`;
  * must not be a role-membership grant (`GRANT <role> TO exchange…` with no `ON`).
* The union of schemas granted via `GRANT … ON SCHEMA <s[,s]> TO …exchange…` must equal
  **exactly `{"ledger", "app"}`**.

⚠️ **Scope/marker split:** `apps/trust/src/ledger/**` is in **T-011's** scope, but the module
`apps.trust.src.ledger` is imported only by tests marked **T-060** and **T-062**. See the
Ledger block under T-060.

---

## T-012 — Embeddings
scope `services/ingest/src/graph/**`, `services/ingest/src/embeddings/**`, `services/ingest/tests/**` · 1 test

```python
from services.ingest.src.embeddings import EmbeddingProvider, HashEmbedding
```
* `EmbeddingProvider` — an interface **class** declaring a public `embed`. Its public
  callable members are enumerated with `inspect.getmembers(EmbeddingProvider, callable)`.
* `HashEmbedding` — constructs with **no arguments**; implements every public member of
  `EmbeddingProvider` with a **byte-identical `inspect.signature`**.
* `HashEmbedding().embed(text: str)` → a sequence of **exactly 1024** `float`s.
  * deterministic across instances and repeat calls;
  * two different strings **of the same length** must give different vectors;
  * a vector must not be constant across its own dimensions (`len(set(v)) > 1`).

> `services/ingest/src/graph/**` is in scope but **no test imports it**.

---

## T-014 — LLM config + offline double
scope `packages/llm/**` · 2 tests

```python
from packages.llm import resolve_model, RecordedLLM
```

### 🆕 `resolve_model(role: str) -> str`
Roles and the env var each must read — **these four spellings are frozen**:

| role | env var |
|---|---|
| `"buyer"` | `BUYER_MODEL` |
| `"store_agent"` | `STORE_AGENT_MODEL` |
| `"interview"` | `INTERVIEW_MODEL` |
| `"extract"` | `EXTRACT_MODEL` |

With the var set, returns it verbatim. With the var unset, returns a **non-empty documented
default string** (different from the sentinel).

### Static rule on `packages/llm/**/*.py`
Any string constant starting with `"claude-"` must appear only inside the value of a
**module-level** `Assign`/`AnnAssign` whose target name contains `DEFAULT` (case-insensitive).
Anywhere else it is a failure.

### 🆕 `RecordedLLM`
```python
double = RecordedLLM(recordings: dict[str, str])
double.complete(prompt: str) -> str        # exact lookup in `recordings`
```
Deterministic across repeat calls and across a second instance built from the same dict.
Runs with `socket.socket`, `socket.create_connection`, `socket.getaddrinfo` monkeypatched to
raise — **`RecordedLLM` must not touch the network.**

---

## T-020 — Signed fetch guards
scope `services/ingest/src/adapters/**`, `services/ingest/tests/**` · 3 tests

```python
from services.ingest.src.adapters import is_fetch_allowed, is_redirect_chain_allowed, may_fetch, USER_AGENT
```

### 🆕 `is_fetch_allowed(url: str) -> bool`
Returns literal `True`/`False` (identity-compared). Must **resolve DNS before deciding** —
the test monkeypatches `socket.getaddrinfo`/`gethostbyname`/`gethostbyname_ex` and a
perfectly ordinary hostname resolving to `10.0.0.5` must return `False` (rebinding).
`True` for `https://store.example.com/products.json` (resolves to `93.184.216.34`).
`False` for: `http://127.0.0.1/`, `http://127.0.0.1:8080/admin`, `http://10.0.0.5/internal`,
`http://192.168.1.1/`, `http://169.254.169.254/latest/meta-data/`, `http://[::1]/`,
`http://localhost:5432/`, `http://0.0.0.0/`.

### 🆕 `is_redirect_chain_allowed(chain: list[str], allowed_hosts: list[str], max_hops: int) -> bool`
3 positional args. `True` for a 2-hop on-host chain with `max_hops=5`.
`False` for: leaving the allow-list; `store.example.com.attacker.tld` (suffix spoof);
`evilstore.example.com` (prefix spoof); a hop to `169.254.169.254`; a 9-element all-on-host
chain with `max_hops=3`.

### 🆕 `USER_AGENT: str`
Length ≥ 3 after strip. Lower-cased, must **not** contain any of
`mozilla, applewebkit, chrome, safari, gecko, edg/`. Must contain `bot` **or** `http`.
Its leading product token is `re.split(r"[/\s]", ua.strip())[0]` and must be non-empty.

### 🆕 `may_fetch(robots_txt: str, url: str, user_agent: str) -> bool`
3 positional args, robots text first.
* `"User-agent: *\nDisallow: /admin/\nDisallow: /cart\nAllow: /\n"` → `/products.json` True,
  `/admin/orders` False, `/cart` False.
* `""` (empty robots) → True.
* A group naming the product token with `Disallow: /` **beats** a permissive `*` group → False.

---

## T-021 — Extraction
scope `services/ingest/src/extraction/**`, `services/ingest/tests/**`, `fixtures/pages/**` · 3 tests

```python
from services.ingest.src.extraction import content_hash, needs_extraction, extract_claims
```
* 🆕 `content_hash(text: str) -> str` — non-empty, deterministic, different text ⇒ different digest.
  Opens no socket.
* 🆕 `needs_extraction(previous_hash: str | None, text: str) -> bool` — 2 positional args,
  **hash first**. `False` when the hash matches the text; `True` when it does not;
  `True` when `previous_hash is None`.
* 🆕 `extract_claims(text: str, source, confidence_floor: float = …) -> result`
  * `source` is a `packages.contracts.Provenance` **or** a mapping with
    `source, source_class, source_id, url, ref, content_hash, observed_at, extractor_version, authority_rank`.
  * Result must expose an upsert batch under one of `claims` / `upserts` / `upserted`
    (or be a bare list) **and** a hold-back batch under 🆕 `quarantined` / `quarantine`
    (this one has no bare-list fallback — the field must exist).
  * Each claim: `key` (non-empty str), `provenance.source == "scraped"`,
    non-empty `provenance.ref`, non-empty `provenance.observed_at`, and a
    `confidence` in `[0,1]` on the claim (or on its `provenance`/`source`).
  * `confidence_floor=0.0` ⇒ nothing quarantined. Floor above the max confidence ⇒ the upsert
    batch is `[]` and quarantine holds **all** of them. At an intermediate floor the two
    collections **partition** the batch exactly (`kept >= floor`, `held < floor`, no losses).

---

## T-022 — Entity resolution
scope `services/ingest/src/er/**`, `services/ingest/tests/**`, `fixtures/er/**` · 1 test

```python
from services.ingest.src.er import match
match(left: dict, right: dict, threshold: float) -> result   # 3 positional
```
Records carry `product_id, gtin, canonical_name, brand`.
Result fields: `linked` (bool), `confidence` (float in `[0,1]`).
* Equal `gtin` ⇒ `linked is True` and `confidence == 1.0`, **even when the names are unrelated**.
* Symmetric: `match(b, a, 0.95)` equals `match(a, b, 0.95)` or at least also links.
* An unrelated pair at threshold `0.9` ⇒ `linked is False` and `confidence < 0.9`.
* Deterministic: identical inputs give an identical confidence.

---

## T-023 — Catalog adapters
scope `services/ingest/src/adapters/**`, `services/ingest/tests/**`, `fixtures/mcp/**` · 1 test

```python
from services.ingest.src.adapters import CatalogAdapter, catalog_mcp, signed_fetch
```
* 🆕 `CatalogAdapter` — a class with at least one public method, and **must declare both**
  🆕 `fetch_catalog` and 🆕 `to_upserts`. Each of those two must take **at least one
  parameter besides `self`**.
* `signed_fetch` and `catalog_mcp` — each is either a **class** or a **module exporting
  exactly one public class whose name contains "adapter"**. They must resolve to **two
  distinct classes**, neither of which is `CatalogAdapter` itself.
* Each implementation must define **its own** `fetch_catalog` and `to_upserts`
  (`methods[name] is not protocol_methods[name]` — an inherited stub fails).
* For **every** public method on `CatalogAdapter`, each implementation's parameter list
  (minus `self`) must be **exactly equal**, in order, to the protocol's.

> Note: `signed_fetch` here is the **adapter name**; the T-020 guard functions live in the
> same `services.ingest.src.adapters` package. One package, both surfaces.

---

## T-024 · T-031 · T-054 · T-082 · T-083 · T-084 — **no acceptance tests**
These tickets carry acceptance criteria in `tickets.json` but appear in **no test's
`ticket()` marker**. Nothing in the frozen suite can turn green or red for them.

* T-024 differential refresh — `services/ingest/src/scheduler/**` is imported by no test.
* T-031 retrieval + fit scoring — `apps/exchange/src/retrieval/**` is imported by no test.
* T-054 dashboard — TS only.
* T-082 / T-083 / T-084 — `e2e/**` is required to **exist** as a directory (T-000) but no
  test imports anything from it.

**T-013** joins them: `conftest.py` registers the `services.shopify_stub` alias, but no test
in the suite ever imports through it, so `services/shopify-stub/**` is graded only by the
T-000 directory-existence check.

---

## T-030 — Auction fan-out
scope `apps/exchange/src/auction/**`, `apps/exchange/tests/**` · 2 tests

```python
from apps.exchange.src.auction import collect_bids
entries = list(collect_bids(roster, responses, deadline))   # 3 positional
```
* `roster`: `[{"store_id","tier","product_ref","list_price"}]`
* `responses`: `[{"store_id","received_at", "bid": {...Bid...}}]`
* `deadline`: a float epoch (`T_NOW = 1_700_000_000.0`)

Contract:
* Returns **one entry per rostered store** — silent stores and every Tier-0 store included.
* Each entry exposes `store_id` and 🆕 `fallback` (bool).
* Unit price is read at `entry["bid"]["offer"]["unit_price"]`, or `entry["offer"]["unit_price"]`
  when the entry carries no `bid` key.
* A store that responded on time: `fallback is False`, price from the response.
* A silent or Tier-0 store: `fallback is True`, price = the roster's `list_price`.
* A response with `received_at > deadline` is **rejected**: `fallback is True` and the late
  price must not survive.

---

## T-032 — Ranking
scope `apps/exchange/src/ranking/**`, `apps/exchange/tests/**` · 8 tests (6 E3 + 2 S8 blockers)

```python
from apps.exchange.src.ranking import rank
result = rank(candidates, intent, trust_snapshot, config)   # 4 positional
```
`config` is `{"now": <float epoch>}` — no wall clock.

### Candidate record the suite hands in (identical in `test_e3_exchange.py` and `test_spec_criteria.py`)
```
bid_id, store_id, store_domain, tier, network_fee, fee_rate,
envelope_max_discount_pct, envelope_budget_cap,
offer: {product_ref, unit_price, total_price, checkout_url, expires_at(float epoch)},
claims: [{key, value, provenance:{source,ref,observed_at,authority_rank}, status}],
intent_match, price_value, delivery_fit, verified_claim_ratio, policy_penalties
```
`trust_snapshot` is `{store_id: {store_id, score, confidence, dims{5×{alpha,beta,decayed_at}}, blacklisted, low_data}}`.

### Result shape — three top-level keys
* 🆕 `result["ranked"]` — eligible candidates in published order.
* 🆕 `result["candidates"]` — one row **per input candidate**, eligible or not
  (`ranked_candidates` / `candidate_records` also accepted by `test_spec_criteria.py`, but
  `test_e3_exchange.py` reads **only `candidates`** — use `candidates`).
* `result["shortlist"]["slots"]` — the `Shortlist`.

Per-candidate row fields: 🆕 `eligible` (bool), `rank_score` (float, **`None` when ineligible**),
🆕 `components` (mapping; **falsy when ineligible**), 🆕 `exclusion_reasons` (non-empty iterable
when ineligible), `bid_id`.

Slot fields: `slot`, `bid_ref`, `fit_score` (float), `trust_summary` (truthy),
`provenance_labels`.

### Behaviour
* **Fee/tier blindness (R11).** Changing `network_fee`, `fee_rate`, `tier`,
  `envelope_max_discount_pct`, `envelope_budget_cap` must leave both the order **and every
  `rank_score`** bit-identical.
* **Determinism + published tie-break.** Identical inputs ⇒ identical order and identical
  `components` maps. Three candidates identical on every scoring feature but differing in raw
  price and bid id must **score identically**, and order **price ascending, then bid id
  ascending** → `["bid-b", "bid-a", "bid-c"]`. Reversing the input order must not change it.
* **Eligibility filters run before scoring.** Excluded rows carry `rank_score is None` and
  no components. `exclusion_reasons` must contain a substring naming the filter:
  `blacklist`, `expir`, `domain`, `constraint` — for blacklisted, expired
  (`expires_at` in the past vs `config["now"]`), off-domain `checkout_url`
  (host ≠ `store_domain`), and hard-constraint failure respectively.
* **Only `verified` satisfies a hard constraint.** With `intent.hard_constraints =
  [{"field":"capacity_l","op":"gte","value":30}]` and a claim whose **value is 35** (so the
  value passes), a claim `status` of `ambiguous`, `unsupported` or `contradicted` must make
  the candidate **ineligible**; only `verified` is eligible.
* **Shortlist collapse.** 1/2/3 distinct eligible stores ⇒ 1/2/3 slots without error;
  5 ⇒ exactly 4. No duplicate `bid_ref`, no store in two slots.
* **Four differentiated slots.** With 4 eligible stores the `slot` values are 4 distinct
  members of `{"fit", "value", "reliability", "specialist"}`.
* **Provenance labels.** A candidate whose first claim's `provenance.source` is `scraped`
  gets labels containing `"from their website"` and **not** `"store-confirmed"`; any other
  source gets `"store-confirmed"` and not `"from their website"`.
* **S8-1 blocker:** a blacklisted seller is never eligible and never occupies a slot, even
  when maximal on every scoring feature.
* **S8-2 blocker:** a `contradicted` hard-constraint claim never wins.

---

## T-033 — Accept
scope `apps/exchange/src/accept/**`, `apps/exchange/tests/**` · 4 tests (3 E3 + 1 S8 blocker)

```python
from apps.exchange.src.accept import accept
result = accept(auction, bid_id, code_creator, checkout_mode)   # 4 positional
```
* `auction` = `{auction_id, intent_id, cluster_id, bids:[{bid_id, store_id, store_domain,
  offer:{product_ref, unit_price, total_price, checkout_url, expires_at}}],
  accepted_bid_ref: None, now: <float epoch>}`
* `code_creator` — both `creator.create_code(store_id, offer)` and `creator(store_id, offer)`
  must work (the double aliases `__call__ = create_code`); returns
  `{"code": …, "permalink_url": …}`.
* `checkout_mode` — exactly the strings `"shopify"` and `"redirect"`.

### Result
* 🆕 `result["permalink_url"]` — a non-empty string containing the created `code`, whose
  `urlsplit(...).hostname` is the **seller's registered `store_domain`**.
* 🆕 `result["events"]` — a list of records each with `kind`. Filtering to the golden three
  must yield them **in this order**:
  `["accepted", "code_created", "checkout_redirect"]`.
* `redirect` and `shopify` modes must emit **identical ordered `kind` lists** (C11 parity),
  and all three golden kinds must be present in both.
* **Double accept:** a second `accept` on the same auction object must refuse — either raise,
  or return no `permalink_url` **and** emit no second `code_created`. `create_code` must have
  been called exactly once in total.
* **S8-3 blocker:** an offer whose `checkout_url` host is not the bid's `store_domain` is
  refused — no permalink, no code created.

---

## T-034 — Exposure bandit
scope `apps/exchange/src/policy/**`, `apps/exchange/tests/**` · 3 tests

```python
from apps.exchange.src.policy import initial_state, update, exposure
state   = initial_state(stores, clusters, trust_snapshot, config)   # 4 positional
updated = update(state, outcomes)                                    # 2 positional
shares  = exposure(state, cluster_id, seed)                          # 3 positional
```
* `stores`: `list[str]`; `clusters`: `list[str]`.
* `config`: 🆕 `{"exploration_floor": <float>}` — this key spelling is frozen.
* `outcomes`: `[{"store_id","cluster_id","converted": bool}]`.
* `exposure(...)` returns either `{store_id: float}` **or** an iterable of records each
  carrying `store_id` plus one of `exposure` / `share` / `weight`.

Behaviour:
* Positive conversions raise the converting store's share in **its own cluster only** —
  another cluster's map must be **unchanged**.
* Seed-deterministic: the same `(state, cluster, seed)` gives the same map.
* A store flagged `low_data` in the snapshot keeps `>= exploration_floor` share for
  seeds `0..4`, even against four established winners at floor `0.25`.
* A store flagged `blacklisted` draws **exactly `0.0`** for seeds `0..9`, exploration floor
  and winning outcomes notwithstanding — while some other store still draws `> 0`.

⚠️ **Name collision:** `initial_state` and `update` are ALSO required from
`packages.store_agent.src.learning` (T-042) with **different arities**. See §Collisions.

---

## T-035 — Loss reports
scope `apps/exchange/src/reports/**`, `apps/exchange/tests/**` · 1 test

```python
from apps.exchange.src.reports import build_loss_report
report = build_loss_report(auction_log, window)   # 2 positional
```
* `auction_log` rows: `{auction_id, cluster_id, ts, store_id, won, reason, unmet_criteria,
  offer:{unit_price,total_price,discount}, winning_price, rival_store_id}`.
* `window`: `{"start": float, "end": float}`; rows outside must not be counted.
* Return: one `LossReport`, or a list of them (length 1 here).

Contract:
* `report["store_id"] == "store-loser"`.
* `report["by_cluster"]` — rows keyed by `cluster_id`, each with `lost` (int) and
  `reasons` whose key set is **exactly `{"fit","price","commitments","trust"}`**
  (zero-count categories must be present with `0`).
* `unmet_criteria` survives per cluster.
* **Leak ban.** No key anywhere in the report may be named
  `unit_price, total_price, discount, winning_price, rival_store_id, offer, amount`,
  and the values `199.99, 149.5, 15.0` and the strings `store-rival-alpha`,
  `store-rival-beta`, `"199.99"`, `"149.5"` must not appear anywhere in it.

---

## T-040 — Tool hooks
scope `packages/store-agent/src/hooks/**`, `packages/store-agent/tests/**`, `fixtures/envelopes/**` · 3 tests

```python
from packages.store_agent.src.hooks import ToolHooks, Denied, HookProvenanceError, enforce_hook_provenance
```

### 🆕 `ToolHooks`
Constructed as `ToolHooks(context)` **or** `ToolHooks(**context)` — the suite tries the
positional form and falls back to kwargs on `TypeError`. The context mapping:
```
store_id, envelope, catalog{product_ref -> {product_ref,list_price,material,gtin}},
live_state{product_ref -> {in_stock,units_left}}, learned_policy, network_priors{cluster -> {...}}
```

**The six hooks and the provenance source class each must stamp** — exact set equality on
`{provenance.source}` of every claim returned:

| hook (frozen name and arity) | `provenance.source` |
|---|---|
| `hooks.get_product_fact(product_ref, key)` | `"scraped"` |
| `hooks.get_live_state(product_ref)` | `"pixel_feed"` |
| `hooks.get_owner_commitments(cluster_id)` | `"owner_statement"` |
| `hooks.authorize_discount(product_ref, pct)` | `"envelope_rule"` |
| `hooks.choose_policy_action({"cluster_id","intent","product_ref"})` | `"learned_policy"` |
| `hooks.get_network_prior(cluster_id)` | `"network"` |

A "claim" is any reachable dict carrying a `provenance` key.

### 🆕 `Denied`
May be a sentinel object, a marker class, or an exception class. A denial is recognised as:
raising a `Denied` subclass of `BaseException`; returning an instance of `Denied`;
returning the sentinel itself; or returning `None`.
With an envelope of `max_discount_pct = 20.0` and floors
`prod-floor: min_price 95.0`, `prod-cap: min_price 10.0`, list price `100.0`:
* `authorize_discount("prod-cap", 25.0)` → **denied** (over cap)
* `authorize_discount("prod-cap", 20.0)` → **granted** (exactly at cap)
* `authorize_discount("prod-floor", 10.0)` → **denied** (90 < 95 floor, inside cap)
* `authorize_discount("prod-floor", 3.0)` → **granted**
Granted results must carry a claim with `provenance.source == "envelope_rule"`.

### 🆕 `HookProvenanceError` / 🆕 `enforce_hook_provenance(claims, hooks)`
2 positional args, claims first. Must **accept** the exact claim list a hook emitted, and
must **raise `HookProvenanceError`** when the list is that same list plus a hand-built claim
carrying a legitimate-looking `owner_statement` provenance pointing at a real envelope ref.
A guard that only inspects `provenance.source` fails.

---

## T-041 — Advocate runtime
scope `packages/store-agent/src/runtime/**`, `packages/store-agent/tests/**` · 2 tests

```python
from packages.store_agent.src.runtime import bid as make_bid
make_bid(request, context)                # 2 positional
make_bid(request, context, hooks=hooks)   # + hooks kwarg
```
`request` is a `BidRequest` dict `{auction_id, intent, profile, respond_by}`.

* **Cold start** (`context["learned_policy"] is None`): two calls with identical inputs must
  be **byte-identical**. The bid carries an `offer` whose `product_ref` is in the catalog,
  `unit_price == catalog[product_ref]["list_price"]`, `total_price` is `None` or equal to it,
  `discount` is absent/zero, and `commitments` keys **exactly equal** the envelope's
  `standing_commitments` keys.
* **Hook-only claims:** with `hooks=ToolHooks(context)` passed in, every claim in the emitted
  bid must be traceable to a hook emission. Identity is the 4-tuple
  `(key, json(value), provenance.source, provenance.ref)`.

### ⚠️ Two attributes on `ToolHooks` that T-041 requires but T-040 owns the file
* 🆕 `hooks.call_log` — truthy after the runtime has bid; records the calls made.
* 🆕 `hooks.emitted_claims` — the claims the facade emitted; must be non-empty.

The test is marked **T-041** but `packages/store-agent/src/hooks/**` is **T-040's scope**.
Whoever writes `ToolHooks` must ship both attributes.

---

## T-042 — Store learning loop
scope `packages/store-agent/src/learning/**`, `packages/store-agent/tests/**` · 3 tests

```python
from packages.store_agent.src.learning import build_network_prior, initial_state, sample_depth, update
prior   = build_network_prior(records)         # 1 positional
state   = initial_state(prior)                 # 1 positional
depth   = sample_depth(state, cluster_id, seed)# 3 positional
updated = update(state, outcome_records)       # 2 positional
```
* Own-outcome records: `{cluster_id, store_id, discount_depth, commitments, won, order_ref}`.
* Prior records also carry `value_prop, pitch_claims, discount_depth, discount_pct, offer{...}`.

Contract:
* 🆕 `sample_depth` is a **pure function of `(state, cluster, seed)`** — equal for a repeated seed.
* Feeding outcomes where deep discounts win raises the mean of
  `sample_depth(state, cluster, s) for s in range(400)`; shallow-winning outcomes lower it.
* 🆕 `build_network_prior` must be **byte-identical with and without every key whose name
  contains "discount"** (recursively stripped) — R17: elasticity is never pooled.
  It must not be a constant (`"null" / "{}" / "[]" / '""'` all fail) and must change when
  the `won` flags flip.
* `initial_state(prior)` twice gives identical states; `update` must **not mutate** its input
  state; two stores fed different outcomes end in different states.

⚠️ **Name collision:** `initial_state` / `update` also exist in `apps.exchange.src.policy`
(T-034) with 4- and 2-arg signatures respectively. Here they are 1- and 2-arg.

---

## T-043 — Shadow mode, activation, trust intake
scope `packages/store-agent/src/modes/**`, `packages/store-agent/tests/**` · 3 tests

```python
from packages.store_agent.src.modes import AgentRunner
runner = AgentRunner(context, sink=sink, submitter=submitter, mode="shadow")
```
🆕 `AgentRunner` — **1 positional (the store context) + three keyword-only-in-practice args
`sink=`, `submitter=`, `mode=`.** The doubles are protocol-agnostic recorders: whatever
method name the runner calls on them is recorded, so only *whether* it logged/submitted matters.

Required members:
* 🆕 `runner.run(bid_request) -> None` — computes and logs a bid.
* 🆕 `runner.mode` — a **settable attribute**, values `"shadow" | "active" | "killed"`.
  Flipping it on the **same object** must change behaviour with no restart.
* 🆕 `runner.ingest_trust_event(trust_event_payload) -> None`.

Behaviour:
* `mode="shadow"` × 3 runs ⇒ submitter called **0** times, sink called **3** times.
* Every sink payload contains an `offer` dict with a non-`None` `unit_price` and a
  non-empty **`rationale` string**. 🆕 `rationale` is a frozen field name.
* `mode="active"` ⇒ the next run submits once. `mode="killed"` ⇒ submission stops again.
  Every mode still logs.
* A pushed `TrustEventPayload` (`{store_id, event, dim, delta, pseudonymous_context}`) must
  **deterministically change** the next logged bid's `rationale`, and the same event twice
  must produce the same changed rationale.

---

## T-044 — Signed external door
scope `packages/store-agent/src/external/**`, `packages/store-agent/tests/**` · 2 tests

```python
from packages.store_agent.src.external import sign_bid, receive_bid
signature = sign_bid(payload, key)                                    # 2 positional
result    = receive_bid(payload, signature, keyring, queue=queue)     # 3 positional + queue=
result    = receive_bid(payload, signature, keyring, queue=queue, blacklist=[store_id])
```
* `keyring`: `{store_id: secret_key}`.
* `queue`: an injected recorder; `queue.count` must be exactly `1` on acceptance and `0` on
  every rejection.
* 🆕 `blacklist=` — a keyword accepting a list of store ids.

Result fields (read anywhere in the reachable structure):
* 🆕 `accepted` — `True` on the happy path, **never `True`** on any rejection.
* Unverified marking: either `verified is False`, or one of `verification_status` /
  `status` normalising to `unverified` / `pending` / `pending_verification`.
* The queued work item must carry the `seller_asserted` claims.

Must reject (raise, or `accepted is not True`, **and** never enqueue):
wrong key · garbage signature string · `None` signature · a signature replayed from a
different payload · an offer whose `expires_at` is in the past · a store named in `blacklist=`.

---

## T-045 — Adversarial seller persona
scope `apps/seller-reference/**` · 1 test

```python
from apps.seller_reference.src.personas import build_persona
pitch = build_persona("aggressive").pitch(intent)   # 1 positional; .pitch(intent) 1 positional
```
🆕 `build_persona(name: str)` returns an object with a 🆕 `.pitch(intent: dict)` method.

Ground truth is the fixture manifest (found by `rglob("manifest*.json")` under `fixtures/`,
first one declaring a `personas` block):
* `manifest["personas"]["aggressive"]` must exist, with `scripted_claims` (or `claims`) as a
  non-empty list, and the manifest must define `fixture_intent` / `intent` / `intents[0]`.
* The persona's emitted claim **key set must equal** the scripted key set exactly — no more,
  no fewer (normalised by `str().strip().lower()`).
* Where a scripted claim carries a `value`, the emitted value must match (normalised).
* **Every** emitted claim must have `provenance.source == "seller_asserted"`.

---

## T-050 — Install
scope `apps/merchant/**` · 2 tests

```python
from apps.merchant.svc.src.install import install, REQUIRED_SCOPES
install("acceptance-store.myshopify.com", admin_client)   # 2 positional
```
The admin client double records **every** call shape (`execute/query/mutate/request/__call__`
or any convenience method name) and returns an auto-vivifying mapping.

* `install` must make at least one call.
* It must call something whose serialized transcript contains **`webPixelCreate`**
  (case- and underscore-insensitive match), with a **non-empty `settings` payload**
  (a `{}`, `""`, `null`, `None` or `[]` tail fails).
* The set of order-lifecycle webhook topics appearing anywhere in the transcript, intersected
  with a 19-topic lifecycle vocabulary, must equal **exactly**:
  ```
  {"orders/paid", "orders/fulfilled", "refunds/create"}
  ```
  Subscribing `orders/create`, `orders/updated`, `checkouts/create`, `carts/update`, etc. is
  a **failure**, not a superset.

### 🆕 `REQUIRED_SCOPES`
An iterable of strings (lower-cased and stripped by the test). Must be non-empty, must
contain `read_orders`, and must contain **none** of:
```
read_customers, write_customers, read_all_orders, read_customer_payment_methods,
read_customer_merge, write_customer_merge, read_customer_events, read_marketing_events
```

---

## T-051 — Pixel collector
scope `pixel/**`, `apps/merchant/svc/src/collector/**`, `apps/merchant/svc/tests/**` · 2 tests

```python
from apps.merchant.svc.src.collector import accept_pixel_event
accept_pixel_event(event: dict)   # 1 positional
```
The clean event — these key spellings are the join-key contract:
```python
{"clientId", "checkoutToken", "orderId", "discountApplications":[{"code","value","type"}]}
```
* The clean payload is **accepted** (returns non-`None`/non-`False`, with no falsy
  `accepted/ok/valid/allowed` and no truthy `rejected/refused/error/errors/violations`).
* Adding **any** of `email, phone, first_name, last_name, address, customer_id, customerId`
  must be **rejected** (raising counts), and the rejection record must not echo the PII value.
* A dropped event (`orderId = None`, `discountApplications = None`) must **not raise**, and
  the returned record must carry a *truthy* marker whose key or string value contains one of
  🆕 `gap, missing, incomplete, dropped, unresolved`. A complete event must **not** carry one.
  (A permanently-present `"gaps": []` reads as "no gap" — the check is truthiness, not substring.)

> `pixel/**` must exist as a directory (T-000) but no test imports from it.

---

## T-052 — Discount codes
scope `apps/merchant/svc/src/codes/**`, `apps/merchant/svc/tests/**` · 3 tests

```python
from apps.merchant.svc.src.codes import create_code, on_redemption
create_code(store_id, offer, admin_client)                 # 3 positional
create_code(store_id, offer, admin_client, now=_T_NOW)     # + now= (a tz-aware datetime)
on_redemption(code, order)                                 # 2 positional
```
The `offer` mapping: `offer_id, auction_id, bid_ref, product_ref, variant_id, quantity,
list_price, unit_price, currency, discount_pct, discount_type, checkout_url`.

`create_code` must:
* issue a call whose transcript mentions **`discountCodeBasicCreate`**;
* declare `usageLimit`/`usage_limit` = **1** in those arguments;
* declare an ISO-8601 `endsAt`/`expiresAt`/`expiry`/`expires`; its lifetime measured from the
  code's own `startsAt` — or, absent one, from the **injected `now=`** — must be
  `0 < lifetime <= 48 hours`. **`create_code` must anchor the window at the `now` it is given**,
  never at the machine clock;
* return `code` (or `discount_code`/`discountCode`) and `permalink_url`
  (or `permalink`/`permalinkUrl`/`url`), the permalink starting with `http` and **containing
  the code**; the code must also appear in the mutation arguments.

**`combinesWith` conflict.** The client double seeds shop config. Given
`combinesWith: {orderDiscounts:false, productDiscounts:false, shippingDiscounts:false}` plus
`hasActiveAutomaticDiscount: true`, `create_code` must either **refuse** or return an
`applied` discount (`discount_pct`/`discountPct`/`applied_discount_pct`) **different from the
requested `10.0`** — before any permalink exists. With the permissive seed
(`all three true`) it must go through untouched at the requested pct.

**`on_redemption(code, {"id": …, "discountCodes": [code]})`:**
* first call carries **no** truthy marker named `integrity, duplicate, reuse, reused, already, double`;
* second call **must** carry one, and the serialized result must contain the key `kind`
  (i.e. it returns a `LedgerEvent` — kind `offer_integrity`). It must **not raise**.

---

## T-053 — Onboarding interview
scope `apps/merchant/svc/src/onboarding/**`, `apps/merchant/svc/tests/**`, `fixtures/interviews/**` · 3 tests

```python
from apps.merchant.svc.src.onboarding import envelope_from_transcript, approval_digest, activate, edit
envelope = envelope_from_transcript(transcript)   # 1 positional
digest   = approval_digest(envelope)              # 1 positional -> str
activated = activate(envelope, approval)          # 2 positional
v2        = edit(envelope, {"max_discount_pct": n})  # 2 positional (envelope, changes-dict)
```

### 🆕 Fixture convention — frozen, quote it into the packet
A JSON file under `fixtures/interviews/` carrying **both** documents:
* one object with `transcript` (or `dialogue`/`messages`/`turns`) **and**
  `expected_envelope` (or `golden_envelope`/`envelope`, which must be an object); **or**
* a sibling pair `<name>.transcript.json` + `<name>.envelope.json`.

The golden envelope must carry all eight keys
`store_id, version, floors, max_discount_pct, budget_cap, pursue_clusters,
standing_commitments, activation`, and `activation == "shadow"`.

Contract:
* `envelope_from_transcript(transcript)` reproduces the golden envelope **key for key**
  (`produced[key] == expected[key]` for every golden key), and is deterministic.
* 🆕 `approval_digest(envelope)` returns a **non-empty string** (`.strip()` is called on it).
  A different envelope must yield a different digest.
* `activate(envelope, approval)` where `approval = {"approver", "approved_at", "envelope_hash"}`:
  * with a complete, digest-matching approval → returns an envelope whose `activation == "active"`;
  * with `None`, with no `approver`, with no `approved_at`, or with an `envelope_hash` of a
    **different** envelope → must not produce `activation == "active"` (raising is fine).
  * Negative cases run on deep copies first, so `activate` must not mutate shared state.
* `edit(envelope, changes)` returns a **new** object with an **integer `version` strictly
  greater** than the input's, the change applied, and **must not mutate** the prior version —
  asserted again after a second edit.

---

## T-060 — Append-only event store
scope `apps/trust/src/events/**`, `apps/trust/tests/**` · 2 tests

```python
from apps.trust.src.events import InMemoryEventStore, append
store = InMemoryEventStore()          # no arguments
append(store, event)                  # 2 positional, store first
```
🆕 `InMemoryEventStore` must expose 🆕 `events` and 🆕 `head_hash` — each may be a
**property or a zero-argument method** (the suite calls it if callable).

* Appending the **same `event_id` twice** must leave `len(events)` and `head_hash` unchanged.
* Appending a **new** event must increase `len(events)` by 1 and change `head_hash`.

### ⚠️ Ledger — `apps.trust.src.ledger` (scope belongs to **T-011**)
```python
from apps.trust.src.ledger import verify_chain   # marked T-060
verify_chain(events)   # 1 positional
```
Returns 🆕 `ok` (bool) and 🆕 `broken_at`.
* A clean stream from the store: `ok is True`, `broken_at` in `(None, -1)`.
* Mutating `events[2]["payload"]["type"]`: `ok is False` and **`broken_at == 2`**
  (the index of the tampered event).

Ledger events in this file are shaped
`{event_id, ts, kind, store_id, order_ref, payload}` with
`payload = {"dim","type","observed_at"}` and default `kind = "claim_verified"`.

---

## T-061 — Reconciliation
scope `apps/trust/src/reconcile/**`, `apps/trust/tests/**` · 3 tests

```python
from apps.trust.src.reconcile import reconcile
emitted = reconcile(events)   # 1 positional: a list of LedgerEvents
```
Input events, by `kind`:
* `accepted` — `payload.{checkout_token, offer{product_ref, unit_price, total_price, discount{type,value}}}`
* `checkout_pixel` — `payload.{clientId, checkout_token, total_price, discountApplications[{type,value}]}`
* `order_paid` — `payload.{checkout_token, order_id, total_price, discountApplications[…]}`

Output: `reconcile` must emit **exactly one** event with `kind == "reconciled"`.
Its `payload` fields (all frozen names):
* 🆕 `price_honored` (bool) · 🆕 `discount_honored` (bool)
* 🆕 `pixel_missing` (bool) — `False` when a pixel event was present, `True` when it dropped
* `order_ref` — `"o-1"`
* 🆕 `observed_price` (float)

Behaviour: **the webhook is authoritative.** With a wrong pixel and a matching webhook,
`price_honored is True` and `observed_price == 100.0`. With a matching pixel and a diverging
webhook, `price_honored is False`, `discount_honored is False`, `observed_price == 120.0`.
With no pixel at all, reconciliation still succeeds and `pixel_missing is True`.

---

## T-062 — Trust scoring
scope `apps/trust/src/scoring/**`, `apps/trust/tests/**` · 6 tests

```python
from apps.trust.src.scoring import score, OBSERVATION_WEIGHTS, BLACKLIST_THRESHOLD, Blacklist, is_blacklisted
snapshot = score(observations, as_of=AS_OF)   # 1 positional + as_of= (an ISO-8601 string)
```
`observations`: `[{"store_id","dim","type","observed_at"}]`.

### The five trust dimensions — frozen, used as literal keys everywhere
```
price_honored  discount_honored  shipped_on_time  not_returned  feedback_match
```

### `score(...)` result
* `score` (float, strictly in `(0,1)` for the empty prior)
* `confidence` (float, strictly in `(0,1)` at the prior; must **rise** with observations)
* 🆕 `score_version` (non-empty string)
* `dims[dim] = {alpha, beta, decayed_at}` — the empty prior is **Beta(2,2)**:
  `alpha == 2.0 and beta == 2.0` for every one of the five dims.

### 🆕 `OBSERVATION_WEIGHTS` — a published mapping with **exact values**
```python
OBSERVATION_WEIGHTS["contradicted"]   == 2.0
OBSERVATION_WEIGHTS["severe_policy"]  == 3.0
OBSERVATION_WEIGHTS["mismatch_return"]== 1.5
```
And behaviourally: one `contradicted` observation adds **exactly 2.0** to that dim's `beta`;
one `unsupported` adds a weight strictly **between 0 and 2.0**. Score ordering must be
`contradicted < unsupported < prior`.

Observation `type` values the suite uses: `verified, contradicted, unsupported, fulfilled,
mismatch_return, severe_policy` (the last two via the weight table and the manifest).

Verification-only observations must move both `score` and `confidence` off the prior —
upward for `verified`, downward for `contradicted` — **before any transaction exists**.

### 🆕 `BLACKLIST_THRESHOLD`
A float strictly in `(0,1)`. The manifest's scripted dishonest store, replayed for
`manifest.episode_budget` episodes, must end **below** it; a control store running the same
number of clean episodes must end **above** it.

### 🆕 `Blacklist` / 🆕 `is_blacklisted`
```python
bl = Blacklist()                                  # no arguments
bl.add(business_identity=…, reason_code=…, status=…, expires_at=…)   # ALL FOUR KEYWORD
entry = bl.lookup(business_identity)               # 1 positional
is_blacklisted(blacklist, store_record)            # 2 positional
```
* `bl.add` is called **only with keywords** — the four names are frozen.
* `status` round-trips through `lookup` for each of 🆕 `"active", "under_review", "appealed", "expired"`;
  `entry` exposes `status`, `reason_code`, `expires_at`.
* `is_blacklisted(bl, {"store_id":…, "business_identity":…})` binds to **`business_identity`**,
  not `store_id`: a re-registered store under a new `store_id` is still `True`.
  `"active"` → `True`; `"expired"` → `False`.
* 🆕 **Fail closed:** if `blacklist.lookup(...)` raises, `is_blacklisted` must return `True`.

### ⚠️ Ledger replay — `apps.trust.src.ledger` (scope belongs to **T-011**)
```python
from apps.trust.src.ledger import replay   # marked T-062
replayed_by_store = replay(events, as_of=AS_OF)   # 1 positional + as_of=
replayed = replayed_by_store["s-1"]                # keyed by store_id
```
`replay` must reproduce `score`, `confidence`, `score_version` and every
`dims[dim].{alpha,beta,decayed_at}` **exactly** as `score(...)` served them for the same
observation script.

---

## T-063 — Trust push + buyer feedback
scope `apps/trust/src/feedback/**`, `apps/trust/tests/**` · 3 tests

```python
from apps.trust.src.feedback import push_trust_event, accept_feedback
push_trust_event(delta, sink)                                      # 2 positional
accept_feedback(order_ref, response, routed_orders=routed)         # 2 positional + routed_orders=
```

### `push_trust_event`
`delta` = `{store_id, dim, delta, event}`. The `sink` double records `sink.send(store_id, payload)`.
* Exactly **one** push, to the affected `store_id`.
* `payload` carries `store_id`, `dim`, `delta` (float), the **full** `event`
  (`event.event_id`, `event.kind` preserved), and a non-`None` 🆕 `pseudonymous_context`.
* No string anywhere in the payload may be the buyer's email or name, and **no key** may be
  named `buyer_email, buyer_name, email, address, account_id` — the source event carries all
  of them, so they must be stripped, not copied.

### 🆕 `accept_feedback`
`routed_orders` = `{order_ref: {order_ref, store_id, returned: bool, buyer_pseudonym}}`.
Returns 🆕 `accepted` (bool) and 🆕 `weight` (float).
* Order not in `routed_orders` ⇒ `accepted is False`.
* Routed order ⇒ `accepted is True` and `weight > 0.0`.
* `{"matched_pitch": True}` from a buyer whose order has `returned: True` ⇒ still
  `accepted is True`, but `0.0 < returned_weight < kept_weight`.

---

## T-064 — Trust snapshot
scope `apps/trust/src/snapshot/**`, `apps/trust/tests/**` · 1 test

```python
from apps.trust.src.snapshot import build_snapshot
snapshot = build_snapshot(stores, blacklist=blacklist, as_of=AS_OF)   # 1 positional + 2 kwargs
```
`stores`: `[{"store_id", "business_identity", "observations": [...]}]`.
`blacklist`: an `apps.trust.src.scoring.Blacklist` instance (⚠️ cross-ticket: T-062's class).

Result:
* 🆕 `snapshot["version"]` — non-empty when stringified.
* 🆕 `snapshot["stores"][store_id]` → `{store_id, score∈[0,1], confidence∈[0,1],
  dims[5×{alpha,beta,decayed_at}], blacklisted, low_data}`.
* `blacklisted is True` exactly for the store whose `business_identity` is in the blacklist.
* 🆕 `low_data is True` for a store with **fewer than `manifest.new_store_prior_n` clean
  episodes**, and `False` for one with `3 × prior_n`.

---

## T-065 — Claim verification
scope `packages/verification/**`, `apps/trust/src/verification/**`, `apps/trust/tests/**` · 5 tests

```python
from packages.verification import verify, satisfies_hard_constraint, FIELD_TOLERANCES
result = verify(pitch, catalog_snapshot, verifier_version)   # 3 positional
```

### `verify` inputs
`pitch` = `{pitch_id, store_id, product_ref, text, claims:[{claim_ref, key, value, op?,
provenance{source:"seller_asserted", ref, observed_at, authority_rank}}]}`
`catalog_snapshot` = `{snapshot_id, products:[{product_ref, canonical_name,
attributes:{<key>:{value, unit?}}, offer:{unit_price, currency, availability}}]}`
`verifier_version` = an opaque string (`"v1"`, `"v2"`, `"acceptance-verifier-1"`).

### `verify` result
* `result["claims"]` — each with `claim_ref` (or `key`/`id`/`claim_id`), `status`,
  `confidence` (float in `[0,1]`), `evidence_refs` (**non-empty** for decided golden claims),
  `observed_value`.
* `result["verifier_version"]` — echoes the argument exactly.
* `result["catalog_snapshot"]` — either the snapshot itself, or a string containing the
  snapshot id.
* `status` ∈ **exactly** `{"verified", "contradicted", "unsupported", "ambiguous"}`.

### Behaviour
* **Idempotent** per `(pitch, verifier_version, snapshot)` — the projection
  `(claim_ref, status, confidence, tuple(evidence_refs), observed_value)` must be identical
  across two runs on deep copies.
* Changing the snapshot re-verifies (`500 g` claim vs a `900 g` catalog ⇒ `contradicted`).
* **Injection inertness.** With the pitch `text` set to a concatenation of
  "IGNORE PREVIOUS INSTRUCTIONS…", a forged JSON verdict, and a `<tool_use>` block: a false
  claim must still be `contradicted`, a claim whose *value* is the injection text must not be
  `verified`, and `verify` **must not mutate the snapshot it was given**.
* **`satisfies_hard_constraint(status: str) -> bool`** — 1 positional string arg:
  `"verified" → True`; `"contradicted" / "unsupported" / "ambiguous" → False`.

### 🆕 `FIELD_TOLERANCES`
A published mapping supporting `.get(key, default)` and **required to contain a
`"default"` key**:
```python
tol = float(FIELD_TOLERANCES.get("weight", FIELD_TOLERANCES["default"]))
assert 0.0 < tol < 1.0        # RELATIVE tolerances in (0, 1)
```
Comparator behaviour against a catalog with
`weight {value:500, unit:"g"}, vegan {True}, material {"Cotton"},
ingredients ["water","glycerin","aloe"], compatible_with ["p-2"]`:

| claim | expected status |
|---|---|
| `weight = "0.5 kg"` | `verified` (unit normalisation) |
| `vegan = "Yes"` | `verified` (boolean normalisation) |
| `material = "cotton"` | `verified` (case normalisation) |
| `ingredients contains "aloe"` | `verified` |
| `ingredients contains "retinol"` | `contradicted` |
| `compatible_with contains "p-2"` | `verified` |
| `compatible_with contains "p-9"` | not `verified` |
| `weight = 500·(1 + tol/2) g` | `verified` |
| `weight = 500·(1 + 3·tol) g` | `contradicted` |

Claims may carry an 🆕 `op` key; the value used is `"contains"`.

> `apps/trust/src/verification/**` is in T-065's scope but **no test imports it** — the whole
> verification surface is read from `packages.verification`.

---

## T-070 — Buyer identity
scope `apps/buyer/**` · 2 tests

```python
from apps.buyer.svc.src.profile import build_profile
from apps.buyer.svc.src.vault import PseudonymVault
profile = build_profile(account, pseudonym)   # 2 positional
vault = PseudonymVault(); vault.issue(buyer_key)   # no ctor args; issue() 1 positional
```

### 🆕 `build_profile`
`account` carries `account_id, email, first_name, last_name, phone, address, postal_code,
region, orders[{order_ref,total,category}], budget_band`.

The returned `BuyerProfile` must serialize to an object whose **top-level key set is exactly
`{"pseudonym", "buckets"}`**, and whose `buckets` key set is **exactly**:
```
{budget_band, category_affinity, frequency_tier, region, first_time}
```
`buckets["first_time"]` is a `bool`; `buckets["category_affinity"]` is a `list`.
No key anywhere may match the identity regex (`email/phone/first_name/last_name/full_name/
given_name/surname/address/street/postal/zip_code/account_id/customer_id/buyer_id/user_id/
identity/ip_address/device_id`), and none of the values
`dana, reyes, example.com, 555-0100, alder way, acct-9f3c21, 97205` may appear anywhere.

### 🆕 `PseudonymVault`
`vault.issue(buyer_key)` returns a non-empty string. 64 issues for one buyer must all be
**distinct**; 64 for a second buyer must be distinct **and disjoint** from the first set. No
pseudonym may embed `dana`, `reyes` or `example.com`.

---

## T-071 — Intent clarification
scope `apps/buyer/svc/src/intent/**`, `apps/buyer/app/intent/**`, `apps/buyer/svc/tests/**`, `fixtures/dialogues/**` · 3 tests

```python
from apps.buyer.svc.src.intent import clarify, confirm
outcome = clarify(turns, llm)                        # 2 positional
created = confirm(intent, auction_client, confirmed=True)   # 2 positional + confirmed=
```
`llm` is a protocol-agnostic scripted double: **whatever method name the product calls on it**
returns the next scripted reply, so the LLM client protocol is not pinned — but `clarify`
must accept the client as its **second positional argument**.

### 🆕 Fixture convention — `fixtures/dialogues/*.json`, **at least 3 files**
Each carries:
* `turns` — non-empty list of non-empty buyer utterance strings;
* `llm_script` — optional list of scripted replies;
* `expected_intent` — an object pinning at least `{query, hard_constraints, preferences, budget_band}`.
At least one fixture must be a maximally vague opener that provokes a question.

### `clarify` result
* 🆕 `outcome["questions"]` — a `list`/`tuple` of non-empty strings, **`len <= 3`**.
  Across the fixture set, `max(len) >= 1` (at least one dialogue must ask something).
* 🆕 `outcome["intent"]` — never `None`; serializes to an object.
  * non-empty `query` and `budget_band`;
  * `hard_constraints` is a `list`; each item has a non-empty `field`, an `op` ∈
    `{eq, lte, gte, in, contains}`, and **`weight` must be `None`/absent**;
  * `preferences` is a `list`; each item has a non-empty `field`, a `direction` ∈
    `{maximize, minimize, prefer}`, and a numeric non-bool `weight`;
  * **every key in `expected_intent` must match** under order-insensitive canonicalisation.

### `confirm`
* `confirm(intent, client, confirmed=False)` must make **zero** calls on the client
  (raising is acceptable, but not `ImportError/AttributeError/TypeError/NameError`).
* `confirm(intent, client, confirmed=True)` must make **exactly one** call and return
  non-`None`.

---

## T-072 — Shortlist provenance + accept
scope `apps/buyer/app/shortlist/**`, `apps/buyer/svc/src/accept/**`, `apps/buyer/svc/tests/**` · 2 tests

```python
from apps.buyer.svc.src.accept import provenance_label, accept
label  = provenance_label(provenance)     # 1 positional
result = accept(slot, exchange_client)    # 2 positional
```

### `provenance_label` — the frozen label map
```python
"owner_statement" -> "store-confirmed"
"envelope_rule"   -> "store-confirmed"
"learned_policy"  -> "store-confirmed"
"pixel_feed"      -> "store-confirmed"
"network"         -> "store-confirmed"
"scraped"         -> "from their website"
"seller_asserted" -> neither of the above, and lower-cased must CONTAIN "unverified"
```
Argument is a `packages.contracts.Provenance` when importable, otherwise a duck-typed object
with `.source/.ref/.observed_at/.authority_rank`.

### 🆕 `accept`
`slot` = `{slot, bid_ref, auction_id, fit_score, trust_summary, checkout_url, variant_id,
discount_code}` — the `checkout_url` is a **decoy pointing at `attacker.example`**.
`accept` must delegate to the exchange **exactly once** and return the exchange's
`permalink_url` **unmodified** (a bare string, or an object with `permalink_url`).
The string `attacker.example` must not appear anywhere in the result.

---

## T-073 — Buyer feedback
scope `apps/buyer/app/feedback/**`, `apps/buyer/svc/tests/**` · 2 tests
⚠️ **The Python module these tests import is `apps/buyer/svc/src/feedback` — which T-073's
scope does NOT cover.** Only T-070's `apps/buyer/**` permits writing it. See §Scope conflicts.

```python
from apps.buyer.svc.src.feedback import feedback_prompt, submit_feedback
prompt = feedback_prompt(order)                # 1 positional
submit_feedback(order, response, sink)         # 3 positional
```
`order` = `{order_ref, store_id, auction_id, routed: bool}` — 🆕 `routed` is the frozen flag.

### 🆕 `feedback_prompt`
* `routed: False` ⇒ falsy return (`None`, `{}`, `[]` …).
* `routed: True` ⇒ a **single mapping** (not a collection) with:
  * 🆕 `question` — non-empty string,
  * 🆕 `options` — a `list` of **length ≥ 2**.
* **No free-text anywhere.** No key may be named
  `free_text, freetext, free_response, open_text, open_response, comment, comments, note,
  notes, textarea, message`; and any key named `type/input_type/input/kind/widget` must not
  have a value in `{text, textarea, string, free_text, freetext, open}`.

### 🆕 `submit_feedback`
`response` = `{"question_id": "matched_pitch", "choice": "yes_as_described"}`.
Must emit into `sink` **exactly one** object that serializes to a mapping containing `kind`,
with `kind == "feedback"` and `order_ref == "ord-e7-101"`. Both `"matched_pitch"` and
`"yes_as_described"` must survive into the event. No identity-shaped key may appear.
The sink is protocol-agnostic: any method name works; positional args and kwarg values are
both collected.

---

## T-080 — Ground truth: manifest, golden set, seed generator
scope `fixtures/**` · 4 tests in `test_e8_proofs.py` (plus every E4/E6 test that reads ground truth)

### `fixtures/manifest.json` — **exactly one, at that pinned path**
No globbing: `test_e8_proofs.py` asserts that **no other `manifest.json` exists anywhere
under `fixtures/`**. (`test_e4_store_agent.py` does still `rglob` for `manifest*.json` and
takes the first one declaring `personas` — so `personas` must live in this same file.)

Frozen top-level schema:
```jsonc
{
  "seed_category":  "<non-empty string>",
  "seed":           <int>,
  "blacklist_threshold": <float, 0 < t < 1>,
  "episode_budget": <int > 0>,
  "new_store_prior_n": <int > 0>,
  "dishonest_store": {
    "store_id": "<non-empty>",
    "behaviours": [ { "kind": "<non-empty>", "dim": "<one of the 5 trust dims>", "type": "<non-empty>" } ]
  },
  "expected_trust_trajectory": [ { "episode": <int, 0..budget>, "score": <0..1>, "tolerance": <float > 0> } ],
  "golden_set": { "path": "fixtures/golden/golden_set.json", "sha256": "<64 hex>", "count": <int> },
  "personas": { "aggressive": { "scripted_claims": [ {"key":…, "value":…}, … ] }, … },
  "fixture_intent": { … },          // or "intent", or "intents":[…]
  "approval": {
    "approver":     "<a human's name — must NOT match /claude|gpt|llm|agent|bot|swarm|automat|ai|system/i, nor be tbd/todo/none/n\\/a/unknown>",
    "approved_at":  "<ISO-8601 with explicit offset, e.g. 2026-01-01T00:00:00+00:00>",
    "artifact":     "fixtures/<...>",   // must start with "fixtures/", no ".."
    "content_hash": "<64 hex>"
  }
}
```

🆕 **The approval digest rule (frozen, and reproduced identically in `test_e6_trust.py`):**
```python
body = {k: v for k, v in manifest.items() if k != "approval"}
text = json.dumps(body, sort_keys=True, separators=(",", ":"))   # ensure_ascii either way
content_hash == hashlib.sha256(text.encode("utf-8")).hexdigest()
```
The file named by `approval.artifact` must exist **and quote both the same digest and the
same approver**.

Trajectory rule: strictly increasing episodes, ≥ 2 points; `first.score > blacklist_threshold`
and `last.score + last.tolerance < blacklist_threshold`.

### `fixtures/golden/golden_set.json`
```jsonc
{ "pitches": [ {
    "pitch_id": "<unique, non-empty>",
    "text": "<non-empty>",
    "catalog_snapshot": { "snapshot_id": "<non-empty>", "products": [ … ] },
    "gates": [ "<eval gate token>" ],
    "claims": [ { "claim_ref": "<unique within the pitch>", "text": "<non-empty>",
                  "expected_status": "verified|contradicted|unsupported|ambiguous" } ]
} ] }
```
🆕 **The label key is `expected_status`, never `status`** — the two sides of the S8
comparison may not share a spelling.
* The union of `expected_status` across the set must equal **exactly**
  `{verified, contradicted, unsupported, ambiguous}`.
* At least one pitch must carry **both** a `verified` and a `contradicted` claim.
* **At least one pitch must carry all four statuses** — `test_e6_trust.py` searches for it and
  fails if none exists.
* The manifest's `golden_set.sha256` must equal `sha256(file bytes)` and `count == len(pitches)`.

🆕 **Every one of these 11 eval gates must appear in some pitch's `gates`** (any alias, matched
after lower-casing and squashing punctuation):

| canonical | accepted aliases |
|---|---|
| `contradiction` | `contradiction`, `contradictions`, `contradicted` |
| `stale_evidence` | `stale_evidence`, `stale_or_absent_evidence`, `stale_absent_evidence`, `absent_evidence` |
| `wrong_variant` | `wrong_variant`, `variant_mismatch` |
| `wrong_units` | `wrong_units`, `wrong_unit`, `unit_mismatch` |
| `injection` | `injection`, `prompt_injection` |
| `claim_splitting` | `claim_splitting`, `claim_split` |
| `verbosity_gaming` | `verbosity_gaming`, `verbosity` |
| `timeout` | `timeout`, `timeouts` |
| `duplicate_pitch` | `duplicate_pitch`, `duplicate_pitches`, `duplicate` |
| `expired_offer` | `expired_offer`, `expired_offers` |
| `blacklist_expiry` | `blacklist_expiry`, `blacklist_expiration` |

### 🆕 `fixtures/generator` — an importable module
```python
from fixtures.generator import generate, apply
payload = generate(seed_category: str, seed: int)   # 2 positional
report  = apply(payload, target: dict)              # 2 positional
```
* `generate` must be **byte-identical** across two calls for the same `(seed_category, seed)`,
  and **different** for `seed + 1`.
* `payload` must carry non-empty 🆕 `catalog`, 🆕 `stores`, 🆕 `event_script`
  (each a `list` or `dict`). `stores` must include `manifest.dishonest_store.store_id`
  (as a bare id, or as `{"store_id": …}`).
* `payload` must be **JSON-serializable plain data** (`json.dumps(..., default=None)`).
* `apply(payload, {})` returns a report with int 🆕 `created > 0` and int 🆕 `unchanged == 0`.
  Applying the **same payload to the same target again** returns `created == 0` and
  `unchanged == <the first run's created>` — idempotent.

---

## T-081 — Dishonest simulation script
scope `services/sim/**` · 1 test

```python
from services.sim.src.dishonest import run_dishonest_script
emitted = run_dishonest_script(manifest, seed)   # 2 positional
```
🆕 Returns a **`list`** of records, each with:
* 🆕 `kind` — non-empty string;
* 🆕 `episode` — an `int` (not `bool`) with `0 <= episode <= manifest.episode_budget`.

Contract:
* `[r["kind"] for r in emitted]` must **equal, element for element**,
  `[b["kind"] for b in manifest["dishonest_store"]["behaviours"]]`.
* Episodes must be **non-decreasing**.
* Deterministic: a second call with the same `(manifest, seed)` is byte-identical.

---

## T-085 — Demo runbook
scope `docs/demo/**`, `docs/tests/**` · 2 tests

### `docs/demo/**/*.md` (at least one file; all concatenated)
* Section headings (normalised) must include the tokens 🆕 `provisioning`, 🆕 `auction`,
  🆕 `interview`.
* The text must contain `t-053` (case-insensitive) and must match `/live[\s_\-]*llm/i`.
* Every `make <target>` the runbook tells a human to run (a line starting `make ` after
  stripping a leading `$`) must be **defined in the repo-root `Makefile`**
  (`^target\s*:` , not `:=`). 🆕 **`make e2e-live` must be one of them.**

### No timeline language (also T-085's marker, in `test_spec_criteria.py`)
`SPEC.md`, `DESIGN.md`, `TASKS.md`, `EXECUTION.md` and every `.md` under `docs/` whose path
parts do **not** include `tests` must contain none of (outside fenced code / inline code):
weeks, weekly, sprints, deadlines, milestones, months, monthly, quarters, days, daily,
overnight, eta, timeframes, any weekday name, any month name except March/May (absent from
the list), `q1`–`q4`, or an ISO date `\d{4}-\d{2}-\d{2}`.

---

# Cross-cutting findings

## A. Name collisions — same name, different module, different signature
| name | module A | module B |
|---|---|---|
| `initial_state` | `apps.exchange.src.policy` — **4 positional** `(stores, clusters, snapshot, config)` (T-034) | `packages.store_agent.src.learning` — **1 positional** `(prior)` (T-042) |
| `update` | `apps.exchange.src.policy` — `(state, outcomes)` (T-034) | `packages.store_agent.src.learning` — `(state, records)` (T-042) |
| `accept` | `apps.exchange.src.accept` — **4 positional** (T-033) | `apps.buyer.svc.src.accept` — **2 positional** (T-072) |
| `feedback` | `apps.trust.src.feedback` (T-063) | `apps.buyer.svc.src.feedback` (T-073) |
| `signed_fetch` | the T-020 guard functions live beside it in `services.ingest.src.adapters` | the T-023 **adapter class/module** of the same name in the same package |
| `verify` | `packages.verification.verify` (T-065) | `apps.trust.src.ledger.verify_chain` is the *ledger* one — do not merge them |
| `append` | `apps.trust.src.events.append(store, event)` — a **module-level function**, not a store method | — |

## B. Scope-glob vs `ticket()` marker disagreements
The marker names who is **blamed** when the test is red; the scope glob names who is
**allowed to write the file**. Where they differ, the packet must say so explicitly.

| module | scope glob permits | test marker blames | note |
|---|---|---|---|
| `apps/buyer/svc/src/feedback` | **T-070** only (`apps/buyer/**`) | **T-073** | T-073's scope is `apps/buyer/app/feedback/**` + `apps/buyer/svc/tests/**` — it may **not** create the Python service module its own tests import. **Either widen T-073's scope or make T-070 ship it.** |
| `apps/trust/src/ledger` (`verify_chain`, `replay`) | **T-011** | **T-060**, **T-062** | T-060's scope is `apps/trust/src/events/**`, T-062's is `apps/trust/src/scoring/**`. Neither may write `src/ledger/**`. T-011's own two tests never import it. |
| `packages/store-agent/src/hooks` (`call_log`, `emitted_claims`) | **T-040** | **T-041** (`test_every_claim_in_a_hosted_bid_traces_to_a_hook_call`) | T-041 owns `src/runtime/**` only. The two attributes must be shipped by T-040. |
| `apps/trust/src/events` | **T-060** | T-060 **and T-062** | T-062's replay test constructs `InMemoryEventStore` and calls `append`. Read-only cross-ticket use; T-062 `depends_on` must include T-060. |
| `apps/trust/src/scoring` (`Blacklist`) | **T-062** | T-062 **and T-064** | T-064's snapshot test constructs `Blacklist()` and calls `.add(...)`. Read-only cross-ticket use. |
| `packages/contracts` (`Provenance`) | **T-010** | T-010 **and T-072** (+ an unmarked E2 helper) | Read-only cross-ticket use, guarded by `try/except`. |
| `apps/merchant/svc/src/{codes,collector,onboarding}` | **T-050** (`apps/merchant/**`) and the specific ticket | T-052 / T-051 / T-053 | Benign: T-050's glob is a superset, so no conflict. |
| `apps/buyer/svc/src/{accept,intent}` | **T-070** (`apps/buyer/**`) and T-072 / T-071 | T-072 / T-071 | Benign superset. |
| `services/ingest/src/adapters` | T-020 **and** T-023 | T-020 and T-023 | **Two tickets legitimately share one package.** They must not both create `__init__.py`. |

## C. Module paths NO ticket scope covers
**None.** Every dotted module the suite imports resolves inside at least one ticket's scope
glob (T-000's `**` is excluded from the analysis as a universal catch-all). The nearest thing
to an unreachable goal is the `apps/buyer/svc/src/feedback` row in §B: it is reachable only
through **T-070's** glob, not through the ticket the marker blames.

Non-module path contracts and their owners:
`fixtures/manifest.json`, `fixtures/golden/golden_set.json`, `fixtures/generator` → **T-080**
(`fixtures/**`) · `fixtures/interviews/` → **T-053** · `fixtures/dialogues/` → **T-071** ·
`db/migrations/*.sql` → **T-011** · `docs/demo/*.md` → **T-085** ·
root `Makefile`, `docker-compose*.yml`, the 16 required directories → **T-000** (`**`).

## D. Tickets with acceptance criteria but ZERO acceptance tests
`T-013`, `T-024`, `T-031`, `T-054`, `T-082`, `T-083`, `T-084`.
Nothing in the frozen suite can score them. `services/shopify-stub` is aliased in
`conftest.py` but never imported; `services/ingest/src/scheduler/**`,
`apps/exchange/src/retrieval/**`, `apps/merchant/app/dashboard/**`, `apps/trust/src/verification/**`,
`services/ingest/src/graph/**` and `e2e/**` are imported by no test.

## E. Every 🆕 author-chosen name, in one list
Names a worker will **not** find in `SPEC.md`, `DESIGN.md`, `TASKS.md`, `EXECUTION.md` or
`tickets.json`. Paste the relevant subset into each packet verbatim.

**Modules:** `apps/buyer/svc/src/feedback`, `apps/buyer/svc/src/profile`,
`apps/buyer/svc/src/vault`, `apps/merchant/svc/src/install`,
`apps/seller-reference/src/personas`, `fixtures/generator`, `services/sim/src/dishonest`.

**Types / constants:** `LedgerEventKind`, `REQUIRED_SCOPES`, `USER_AGENT`,
`OBSERVATION_WEIGHTS`, `BLACKLIST_THRESHOLD`, `FIELD_TOLERANCES`, `RecordedLLM`,
`PseudonymVault`, `InMemoryEventStore`, `ToolHooks`, `HookProvenanceError`, `AgentRunner`,
`CatalogAdapter`.

**Functions:** `validate_bid`, `resolve_model`, `is_fetch_allowed`,
`is_redirect_chain_allowed`, `may_fetch`, `content_hash`, `needs_extraction`,
`extract_claims`, `collect_bids`, `build_loss_report`, `enforce_hook_provenance`,
`build_network_prior`, `initial_state`, `sample_depth`, `sign_bid`, `receive_bid`,
`build_persona`, `create_code`, `on_redemption`, `accept_pixel_event`,
`envelope_from_transcript`, `approval_digest`, `verify_chain`, `replay`, `is_blacklisted`,
`push_trust_event`, `accept_feedback`, `build_snapshot`, `satisfies_hard_constraint`,
`build_profile`, `feedback_prompt`, `submit_feedback`, `generate`, `apply`,
`run_dishonest_script`, `fetch_catalog`, `to_upserts`.

**Attributes / result fields:** `call_log`, `emitted_claims`, `ingest_trust_event`,
`rationale`, `head_hash`, `broken_at`, `ok`, `reasons`, `requires_verification`,
`quarantined`, `fallback`, `eligible`, `components`, `exclusion_reasons`, `candidates`,
`ranked`, `events` (on the accept result), `accepted`, `weight`, `pseudonymous_context`,
`price_honored`, `discount_honored`, `pixel_missing`, `observed_price`, `score_version`,
`low_data`, `created`, `unchanged`, `catalog`, `stores`, `event_script`, `episode`, `kind`,
`question`, `options`, `routed`, `expected_status`.

**Keywords:** `path=`, `trust_snapshot=`, `confidence_floor=`, `as_of=`, `blacklist=`,
`queue=`, `hooks=`, `routed_orders=`, `confirmed=`, `now=`, `business_identity=`,
`reason_code=`, `status=`, `expires_at=`, `sink=`, `submitter=`, `mode=`,
and the config keys `exploration_floor`, `default` (in `FIELD_TOLERANCES`).
