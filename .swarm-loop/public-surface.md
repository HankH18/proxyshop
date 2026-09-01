<!-- PROVENANCE — read this before trusting anything below.

This file is DERIVED, not authored. It was extracted by AST-parsing the nine frozen
acceptance test files (plus `conftest.py` and `run.py`) under .swarm-loop/acceptance/ and
independently re-checked against them: 37 product modules, 91 symbols, zero missing.

REGENERATED FOR AMENDMENT 1. The suite went 103 -> 120 tests when the user approved
amendment 1 (commit c5c2aa7; rulings D52, D53, D54). Five test files changed —
`test_e1_foundation.py`, `test_e3_exchange.py`, `test_e4_store_agent.py`,
`test_e6_trust.py`, `test_e8_proofs.py`. The three big shifts, each of which invalidates
something a worker may have been told earlier:
  * D52 — the external signing envelope is REQUIRED and the keyring is nested by `key_id`.
    T-044 went 2 -> 8 tests and its two original tests were REWRITTEN, not extended.
  * D53 — `catalog_claim_accuracy` is a real SIXTH trust dimension. Anything that says
    "the five trust dimensions" is stale.
  * D54 — eligibility is asserted by denial at a new public orchestration boundary,
    `apps.exchange.src.orchestration`, over a new port, `apps.exchange.src.eligibility`.
    `collect_bids/3` and `accept/4` are UNCHANGED and gain no required parameter.

It is a MIRROR of the frozen suite, and the suite is the authority. If the two ever
disagree, the suite wins and this file is stale — regenerate it rather than edit it.
Editing this file changes nothing about what the goals require.

PENDING AMENDMENT 2 AT TIME OF WRITING: `test_spec_criteria.py` on disk no longer matches the
hash in `.swarm-loop/manifest.json`. The on-disk change is self-labelled "AMENDMENT 2" and
teaches T-000's compose test to read `include:`-ed compose fragments; the re-freeze had not
been recorded when this file was regenerated. Only T-000's compose block is affected, and
the change only widens what passes. Every other block mirrors a file whose hash still matches
the manifest (15/16 verified byte-identical).

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
(9 test files, **120 marked tests**, post-amendment-1), by parsing every file with `ast` and
extracting every in-body import, every attribute read off an imported symbol, every call arity
and keyword, and every constant whose exact value the suite asserts. **Not** derived from the
acceptance plan — the plan's appendix is incomplete (audit F6).

Measured totals: **120** marked tests across **37** distinct tickets; **37** product modules;
**91** distinct `(module, symbol)` import pairs. Per-epic goal targets (`goals.json`):
SPEC 8 · E1 10 · E2 8 · E3 21 · E4 20 · E5 10 · E6 26 · E7 9 · E8 8.
**Ten tickets have zero acceptance tests** — see §D.

Every name below is an **immutable contract**. A worker who spells one differently makes the
goal permanently unreachable; the suite is hashed and may not be edited.

Legend:

* 🆕 = **author-chosen name.** It appears nowhere in `SPEC.md`, `DESIGN.md`, `TASKS.md`,
  `EXECUTION.md` or `tickets.json`. A worker reading only the ticket packet will not guess
  it. These are the highest-risk names in the whole contract.
* ⚠️ = a disagreement between the test's `ticket()` marker and the ticket whose `scope`
  glob actually permits writing the file.
* The `from X import a, b, c` blocks below are **syntheses** — they collect every symbol the
  suite imports from a module across all its tests. No single such line necessarily exists
  verbatim in the suite. The module→symbol mapping is exact; the grouping is editorial.

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
3. **Return shapes are read tolerantly — with two exceptions.** Most files carry a
   `_get`/`_field` helper that accepts either a mapping key or an attribute, so a `dict`, a
   dataclass and a pydantic model all satisfy "returns `{ok, reasons}`". `test_e4_store_agent.py`
   normalises through `_plain`/`_first_value` instead, which is equally tolerant. What is
   **not** tolerant: the *field name*. And where the suite writes `is True` / `is False` a
   truthy value is **not** enough — it must be the `bool` singleton (this applies to
   `validate_bid`'s `ok`/`requires_verification`, `collect_bids`'s `fallback`,
   `accept_offer`'s `accepted`, `is_blacklisted`, `satisfies_hard_constraint`,
   `NonceStore.seen`, and `receive_bid`'s `accepted`).
   🔴 **`test_e8_proofs.py` is NOT tolerant at all.** It has no `_get`/`_field`; it reads
   through `_require` (`:165-167`), whose first line is
   `assert isinstance(mapping, dict), f"{where} must be a JSON object, got …"`. So
   **everything T-080 and T-081 return must be a literal `dict`/`list` of plain JSON data** —
   `generate(...)`'s payload, `apply(...)`'s report, and every record from
   `run_dishonest_script(...)`. A dataclass or pydantic model fails there even though it
   would pass everywhere else in the suite.
4. **Nothing is read from a wall clock, a socket, or a live datastore.** Where a reference
   instant is needed the suite injects it (`now=`, `as_of=`, `_config()["now"]`). Several
   tests monkeypatch `socket` to raise; product code that opens a socket on those paths fails.
5. **Collections are compared exactly** (`==` on a set), not by containment, wherever a
   vocabulary is named below.
6. **The six trust dimensions (D53) are one vocabulary, spelled identically in four files.**
   `price_honored, discount_honored, shipped_on_time, not_returned, feedback_match,
   catalog_claim_accuracy`. The literal `catalog_claim_accuracy` appears in exactly three test
   files; canonical definitions: `test_e6_trust.py:83-97`, `test_e8_proofs.py:76-88`,
   `test_e1_foundation.py:433-445`. Anything that still says
   "five dimensions" is pre-amendment and wrong.
7. 🔴 **A `packages/<x>/` directory with only `src/` inside does NOT satisfy
   `from packages.<x> import Name`.** Measured: `find packages -maxdepth 2 -name __init__.py`
   returns **nothing** — `packages/contracts/`, `packages/llm/` and `packages/verification/`
   hold `pyproject.toml`, `src/` and `tests/` and no top-level `__init__.py`. `conftest.py`
   puts only the repo root on `sys.path` (`:27-36`) and deliberately refuses to depend on a
   worker-editable `pythonpath`, so each of those resolves as an **empty PEP-420 namespace
   package** and every `from packages.<x> import …` raises `ImportError`. The three tickets
   that own them (**T-010**, **T-014**, **T-065**) must ship a top-level `__init__.py`
   re-exporting from `src/`, or place the symbols directly under that name. This is invisible
   at cycle 0 because the failure looks identical to "not built yet".
   *Not affected:* `packages.store_agent`, `apps.seller_reference` and `services.shopify_stub`,
   which `conftest.py:82-84` aliases straight to their hyphenated directories, and
   `apps.*.src.*` / `services.ingest.src.*`, which already carry `__init__.py` chains.

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

### docker-compose — 🔴 **one of exactly four filenames at the repo root**
`_compose_path()` (`test_spec_criteria.py:267-273`) iterates a closed list and takes the
first that exists — **there is no globbing**:
```
docker-compose.yml   docker-compose.yaml   compose.yml   compose.yaml
```
`docker-compose.dev.yml` or `docker-compose.local.yaml` will not be found, and the test fails.
* Top-level `services:` block.
* A service matching each of `postgres`, `neo4j`, `redis` **by service name or `image:`**,
  and **each of those three declares a `healthcheck:`**.
* Four **distinct** services whose names contain `buyer`, `merchant`, `exchange`, `trust`.

> ⏳ **PENDING "AMENDMENT 2" — on disk but NOT re-frozen at the time this file was written.**
> `test_spec_criteria.py` on disk carries `_compose_include_paths` / `_compose_stack_services`
> (new, around `:315-365`), and `test_compose_declares_healthchecked_postgres_neo4j_redis_and_separate_deployables`
> now reads the root file's own `services:` block **plus the block of every fragment its
> top-level `include:` list pulls in** (short form `- a/b.yaml` and long form `- path: a/b.yaml`;
> only fragments that exist on disk). `.swarm-loop/manifest.json` still records the
> **pre-amendment** hash for that file, so the re-freeze had not been recorded — treat the
> detail as provisional and re-check before relying on it.
> **Direction of the change is safe either way:** it only *widens* what counts, so a compose
> file that declares all seven services in the root satisfies both the old and the new test.
> A per-lane fragment that is still an empty `services: {}` stub contributes nothing.

### Repo-root `Makefile`
Must exist and must define every `make <target>` the demo runbook names — including
`e2e-live` (see T-085). Targets defined today (`Makefile:7-21`): `bootstrap`, `preflight`,
`deps-up`, `deps-down`, `db-init`, `check`, `verify`, `lint`, `types`, `test-py`, `test-ts`,
`demo-seed`, `e2e-live`, `clean`. The `Makefile` is itself hashed — a runbook may reference
**only** these unless the Makefile is amended.

---

## T-010 — `packages.contracts`
scope `packages/contracts/**` · 8 tests (7 in `test_e1_foundation.py`, 1 in `test_spec_criteria.py`)

```python
import packages.contracts as contracts
from packages.contracts import Bid, Intent, LedgerEvent, Provenance, validate_bid
```

> 🔴 **Import-surface trap (rule 0.7).** `packages/contracts/` has **no top-level
> `__init__.py`** today, only `src/`. `from packages.contracts import Bid` will raise
> `ImportError` until one exists. Ship `packages/contracts/__init__.py` re-exporting the
> generated models from `src/`. The same applies to T-014 (`packages/llm/`) and T-065
> (`packages/verification/`).

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

### 🔴 AMENDED — `TrustSnapshot.dims` is SIX dimensions (D53)
`test_e1_foundation.py:428-456`. The frozen construction payload builds `dims` by iterating

```python
("price_honored", "discount_honored", "shipped_on_time",
 "not_returned", "feedback_match", "catalog_claim_accuracy")   # :433-445
```

each mapped to `{"alpha": 2.0, "beta": 1.0, "decayed_at": "2026-01-01T00:00:00Z"}`, and the
round-trip probe reads four dotted paths, **two of them on the new dimension**:

```
dims.price_honored.alpha            == 2.0     # :451
dims.feedback_match.beta            == 1.0     # :452
dims.catalog_claim_accuracy.alpha   == 2.0     # :453
dims.catalog_claim_accuracy.beta    == 1.0     # :454
```

A five-dimension `TrustSnapshot` fails construction **and** round-trip. A model that
validates `dims` against a closed five-name vocabulary fails.
**But note:** the contracts payload carries only `{alpha, beta, decayed_at}` per dim —
`coverage` (required on `apps.trust.src.scoring.score(...)`'s dims, see T-062) is **not**
part of the `packages.contracts` payload and must not be made mandatory here.

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
`model_fields`/`__fields__`/`get_type_hints`. `test_spec_criteria.py:849`.
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
`TrustSnapshot`: `store_id, score, dims.<dim>.{alpha,beta,decayed_at}, blacklisted` — **six dims**
`TrustEventPayload`: `store_id, event, dim, delta, pseudonymous_context.{cluster_id,pseudonym}`
`Envelope`: `store_id, version, floors[].{product_ref,min_price}, max_discount_pct, budget_cap, pursue_clusters, standing_commitments, activation`

**Consumed by other tickets:** `packages.contracts.Provenance` is constructed with
`Provenance(source=, ref=, observed_at=, authority_rank=)` inside `test_e2_ingestion.py`
(unmarked helper) and `test_e7_buyer.py` (marker T-072). Both wrap it in `try/except`, so a
missing contracts package degrades rather than errors — but the kwarg spelling is fixed.

**Obligation with no acceptance test:** amendment 1 also adds a `SigningEnvelope` contract
type to this package (`tickets.json` T-010 acceptance 5) mirroring D52's five required
fields. **No frozen test imports it** — the signing surface is graded only through
`packages.store_agent.src.external` (T-044). Build it for the ticket's own `verify`
command; it cannot move the frozen suite either way. The `SellerEligibility` port is **NOT**
in this package — D54 moved it to `apps.exchange.src.eligibility` (T-030).

---

## T-011 — Postgres schemas, roles, ledger chain
scope `db/migrations/**`, `apps/trust/src/ledger/**`, `apps/trust/tests/**` · 2 tests in `test_spec_criteria.py`

Both tests are textually unchanged by amendment 1 (`test_spec_criteria.py` was not touched).
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

### 🔴 Two obligations with NO acceptance test — D52 puts them in T-011's migrations
Measured: the strings `bid_nonces`, `seller_endpoints` and the pair `(signer_id, nonce)`
appear in **zero** files under `.swarm-loop/acceptance/`. Nothing in the frozen suite can
turn green or red for either table. They are still required by the ticket's own `verify`
command and by T-044, which depends on T-011.

* `app.seller_endpoints` — one row **per live key**: `(signer_id, store_id, key_id,
  public_key, status)`. `key_id` is unique only **within a signer**, never globally: two
  signers may legitimately reuse the same `key_id` string, and the frozen T-044 tests prove
  it (`test_e4_store_agent.py:881`, `:1277-1283`). A schema with one key per store, or a
  unique index on `key_id` alone, is wrong.
* `app.bid_nonces` — `(signer_id, nonce, auction_id, consumed_at, retain_until)`, **UNIQUE on
  `(signer_id, nonce)`**, retained past the auction's `respond_by`. This is the durable store
  behind T-044's `NonceStore` port.

⚠️ **Scope/marker split:** `apps/trust/src/ledger/**` is in **T-011's** scope, but the module
`apps.trust.src.ledger` is imported only by tests marked **T-060** and **T-062**. See the
Ledger blocks under T-060 and T-062.

---

## T-012 — Embeddings
scope `services/ingest/src/graph/**`, `services/ingest/src/embeddings/**`, `services/ingest/tests/**` · 1 test in `test_e1_foundation.py`

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
scope `packages/llm/**` · 2 tests in `test_e1_foundation.py`

```python
from packages.llm import resolve_model, RecordedLLM
```
> 🔴 Rule 0.7 applies: `packages/llm/` has no top-level `__init__.py`. Ship one.

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

🔴 **An unrecorded prompt must RAISE.** `test_e1_foundation.py:906-911`:
```python
with pytest.raises(Exception) as excinfo:
    double.complete("a prompt that was never recorded")
assert not isinstance(excinfo.value, _NetworkBlocked)   # …and must not have tried the network
```
A `recordings.get(prompt, "")` implementation returns `""` instead of raising and leaves this
test permanently red. The raised exception must be a **product** exception, not the socket
guard's `_NetworkBlocked` — so the lookup must fail *before* any transport is touched.

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
* `content_hash(text: str) -> str` — non-empty, deterministic, different text ⇒ different digest.
  Opens no socket.
* 🆕 `needs_extraction(previous_hash: str | None, text: str) -> bool` — 2 positional args,
  **hash first**. `False` when the hash matches the text; `True` when it does not;
  `True` when `previous_hash is None`.
* 🆕 `extract_claims(text: str, source, confidence_floor: float = …) -> result`
  * `source` is a `packages.contracts.Provenance` **or** a mapping with
    `source, source_class, source_id, url, ref, content_hash, observed_at, extractor_version, authority_rank`.
  * Result must expose an upsert batch under one of `claims` / `upserts` / `upserted`
    (or be a bare list) **and** a hold-back batch under `quarantined` / `quarantine`
    (this one has no bare-list fallback — the field must exist).
  * 🔴 **The upsert batch must be NON-EMPTY** for the frozen input:
    `assert claims, "the policy page must yield at least one atomic claim"`
    (`test_e2_ingestion.py:380`, again at `:409`). The input is the six-line literal
    `POLICY_PAGE_TEXT` at `test_e2_ingestion.py:135-142` — read it before building the
    extractor; an extractor that returns `[]` for that text fails two of T-021's three tests.
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
* `CatalogAdapter` — a class with at least one public method, and **must declare both**
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

## T-030 — Auction fan-out **and the solicitation eligibility gate**
scope `apps/exchange/src/auction/**`, `apps/exchange/src/eligibility/**`,
`apps/exchange/src/orchestration/**`, `apps/exchange/tests/**` · **3 tests** (was 2)

### The unchanged low-level function
```python
from apps.exchange.src.auction import collect_bids       # test_e3_exchange.py:310, :356
entries = list(collect_bids(roster, responses, T_NOW))   # 3 positional — :334, :386
```
**Those two lines are the ONLY call sites of `collect_bids` in the whole suite.** No keyword
argument, no fourth argument, no eligibility parameter — anywhere. D54 is explicit: it
**gains no required parameter**. An eligibility port reaching it must be an optional keyword
whose absence is not itself a denial.

* `roster`: `[{"store_id","tier","product_ref","list_price"}]`
* `responses`: `[{"store_id","received_at", "bid": {...Bid...}}]`
* `deadline`: a float epoch (`T_NOW = 1_700_000_000.0`, `test_e3_exchange.py:41`)

Contract:
* Returns **one entry per rostered store** — silent stores and every Tier-0 store included.
* Each entry exposes `store_id` and `fallback` (compared with `is True` / `is False`).
* Unit price is read at `entry["bid"]["offer"]["unit_price"]`, or `entry["offer"]["unit_price"]`
  when the entry carries no `bid` key (`_unit_price`, `test_e3_exchange.py:234-236`).
* A store that responded on time: `fallback is False`, price from the response.
* A silent or Tier-0 store: `fallback is True`, price = the roster's `list_price`.
* A response with `received_at > deadline` is **rejected**: `fallback is True` and the late
  price must not survive.

### 🔴 NEW (D54) — `apps.exchange.src.eligibility`, the versioned port
```python
from apps.exchange.src.eligibility import (          # test_e3_exchange.py:1002-1009,
    BLACKLISTED, ELIGIBLE, SELLER_ELIGIBILITY_INTERFACE_VERSION,   # :1087-1094, :1179-1186
    UNAVAILABLE, EligibilityDecision, SellerEligibility,
)
```
Six names, always imported together, at three sites.

| symbol | what the suite literally requires | file:line |
|---|---|---|
| `SELLER_ELIGIBILITY_INTERFACE_VERSION` | a `str`, non-blank after `.strip()`. **No literal value is asserted** — pick one and publish it. | `:1189`, `:1192` |
| `ELIGIBLE` / `BLACKLISTED` / `UNAVAILABLE` | **hashable and mutually distinct** (`len({…}) == 3`). No literal values asserted; `str` constants or enum members both work. They are **status values passed as `status=`**, not decisions. | `:1193-1194`, `:952-956` |
| 🆕 `EligibilityDecision` | callable with **exactly the keywords** `store_id=`, `status=`, `reason=`. The result must expose readable `store_id` and `status`. `reason` is never read off a decision. | ctor `:952-956`; reads `:1196-1198` |
| `SellerEligibility` | used **only as a base class** (`class _Source(base):`, `:941`), never instantiated directly. The concrete subclass takes **zero ctor args** and does not call `super().__init__()`. Protocol / ABC / plain class all satisfy, provided `check` is its only abstract member. | `:941-956` |
| `SellerEligibility.check` | method named **`check`**, `check(self, store_id)` — one parameter. Returns an `EligibilityDecision`. | `:947-956` |
| `interface_version` | a **class attribute** the subclass sets (`:942`). The base must not make it a read-only property. The product reads it off the source to gate on version. | `:942`; gating `:1239-1257`, `:1259-1282` |

**Fail-closed means three things.** `BLACKLISTED` denies, `UNAVAILABLE` denies, and a `check`
that **raises** denies exactly like `UNAVAILABLE`. The fixture raises
`RuntimeError(f"eligibility backend unreachable for {store_id}")` (`:950`) and the product
must swallow it into a denial — it is never asserted to propagate. Because no decision exists
in that case, **the product must synthesize its own `"unavailable"` reason**; propagating the
fixture's reason string alone cannot satisfy the raising store.

### 🔴 NEW (D54) — `apps.exchange.src.orchestration.solicit_bids`
```python
from apps.exchange.src.orchestration import solicit_bids     # :1010, :1187
result = solicit_bids(roster=roster, solicitor=solicitor,
                      eligibility=source, now=T_NOW)         # :1035, :1211-1216, :1244-1246
```
**Every call site is 100 % keyword, exactly these four names, no extras.** All four are passed
every time, so a keyword-only definition is safe.

* `solicitor` — a recorder exposing **`.solicit(store)`** with `__call__ = solicit` (`:961-995`);
  either call style works. It is handed the **roster entry**, and returns
  `{"store_id","received_at","bid":{...}}` or `None`.
* Its record `solicitor.asked` is the proof: `assert solicitor.asked == ["store-ok1","store-ok2"]`
  (`:1043`) — an **exact ordered list**, and `== []` on a version refusal (`:1254`).
* `assert sorted(source.checked) == sorted(r["store_id"] for r in roster)` (`:1037`) —
  eligibility is consulted for **every** rostered store, including the one that raises.

Result fields:

| field | requirement | file:line |
|---|---|---|
| 🆕 `solicited` | iterable of store ids in **exact roster order** `["store-ok1","store-ok2"]`; `[]` on version refusal | `:1047`, `:1220`, `:1250` |
| `entries` | one per **solicited** store only. Each carries `store_id`, `fallback` (**`is False`**), and a price reachable through the same `_unit_price` accessor as `collect_bids`. `[]` on version refusal. **An ineligible store must not appear even as a list-price fallback entry.** | `:1049-1063`, `:1253` |
| 🆕 `denied` | one record per denied store, each with `store_id` and a non-empty `reason` | `:1066-1079` |

Denial reasons are matched as **substrings of any reachable string, lower-cased**
(`_strings`, `:89-91`):

| store | condition | reason must contain |
|---|---|---|
| `store-black` | `BLACKLISTED` | `"blacklist"` |
| `store-unknown` | `UNAVAILABLE` | `"unavailable"` |
| `store-boom` | `check` **raises** | `"unavailable"` |

Prices that must survive the orchestration layer unchanged: `store-ok1 → 80.0`,
`store-ok2 → 85.0` (`:1057-1063`).

> The suite never asserts that `solicit_bids` calls `collect_bids` — there is no spy and no
> import check. Delegation is permitted, not required. But a naive pass-through of the full
> roster fails `:1051-1054`, because `collect_bids` emits an entry for **every** rostered
> store while `solicit_bids` must emit them only for **eligible** ones.

---

## T-032 — Ranking **and the ranking eligibility gate**
scope `apps/exchange/src/ranking/**`, `apps/exchange/tests/**` · **9 tests** (7 E3 + 2 S8 blockers)

```python
from apps.exchange.src.ranking import rank
result = rank(candidates, intent, trust_snapshot, config)   # 4 positional — UNCHANGED
```
`config` is `{"now": <float epoch>}` — no wall clock. `rank` is called with **4 positional
arguments at 14 sites** (9 in `test_e3_exchange.py`, 5 in `test_spec_criteria.py`) and gains no
new parameter.

### Candidate record the suite hands in (identical in `test_e3_exchange.py` and `test_spec_criteria.py`)
```
bid_id, store_id, store_domain, tier, network_fee, fee_rate,
envelope_max_discount_pct, envelope_budget_cap,
offer: {product_ref, unit_price, total_price, checkout_url, expires_at(float epoch)},
claims: [{key, value, provenance:{source,ref,observed_at,authority_rank}, status}],
intent_match, price_value, delivery_fit, verified_claim_ratio, policy_penalties
```

### ⚠️ The snapshot handed to the ranker is still **FIVE**-dimensional
`test_e3_exchange.py:188-197` and `test_spec_criteria.py:491-500` both build
`dims` over the five **transaction** dimensions only:
`{price_honored, discount_honored, shipped_on_time, not_returned, feedback_match}`,
each `{alpha, beta, decayed_at}`, alongside `store_id, score, confidence, blacklisted, low_data`.
This is deliberate — D53/T-032 say ranking reads the snapshot's `score`/`confidence` and
**never re-derives dimensions**. **A ranker that validates its input snapshot as requiring
six dims fails both of these tests.** T-064's *served* snapshot is six-dimensional; the
ranker's *frozen fixture* is not.

### Result shape — three top-level keys
* 🆕 `result["ranked"]` — eligible candidates in published order.
* `result["candidates"]` — one row **per input candidate**, eligible or not
  (`ranked_candidates` / `candidate_records` also accepted by `test_spec_criteria.py`, but
  `test_e3_exchange.py` reads **only `candidates`** — use `candidates`).
* `result["shortlist"]["slots"]` — the `Shortlist`.

Per-candidate row fields: `eligible` (bool), `rank_score` (float, **`None` when ineligible**),
`components` (mapping; **falsy when ineligible**), `exclusion_reasons` (non-empty iterable
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

### 🔴 NEW (D54) — the versioned-interface test T-032 is blamed for
`test_e3_exchange.py:1176` `test_both_eligibility_gates_require_a_versioned_seller_eligibility_interface`
imports **from three modules T-032 does not own**:

```python
from apps.exchange.src.eligibility import (…six names…)      # :1179-1186
from apps.exchange.src.orchestration import accept_offer, solicit_bids   # :1187
```

It asserts: `SELLER_ELIGIBILITY_INTERFACE_VERSION` is a non-blank `str` (`:1189-1192`); the
three statuses are distinct (`:1193-1194`); and **both boundary gates refuse a source whose
`interface_version` is `"0.0.0-not-the-published-interface"`** (`:1239`) while accepting one
that publishes the right version. On refusal: `solicited == []`, `entries == []`,
`bad_solicitor.asked == []` (`:1250-1254`); `accepted is False`, `permalink_url is None`,
`bad_creator.calls == []` (`:1276-1280`). A `try/except Exception` wraps the calls
(`:1247`, `:1273`), so **raising is an acceptable refusal** — but the side-effect assertions
run **outside** the `try`, so a raise *after* soliciting or minting a code still fails.

⚠️ **This is a scope/marker disagreement.** The test is marked **T-032**, whose scope is
`apps/exchange/src/ranking/**` only. The files it exercises belong to **T-030**
(`eligibility/`, `solicit_bids`) and **T-033** (`accept_offer`). `tickets.json` adds a
`T-032 → T-033` dependency edge for exactly this reason. T-032 must not create either module.

---

## T-033 — Accept **and the checkout eligibility gate**
scope `apps/exchange/src/accept/**`, `apps/exchange/src/orchestration/**`,
`apps/exchange/tests/**` · **5 tests** (4 E3 + 1 S8 blocker)

### The unchanged low-level function
```python
from apps.exchange.src.accept import accept
result = accept(auction, bid_id, code_creator, checkout_mode)   # 4 positional — UNCHANGED
```
Call sites, all 4-positional, no keywords: `test_e3_exchange.py:658, :682, :685, :704, :711`
and `test_spec_criteria.py:1042, :1073`. **No site passes a fifth argument.** D54: `accept`
**gains no required parameter**; any eligibility port reaching it is an optional keyword whose
absence is not itself a denial.

* `auction` = `{auction_id, intent_id, cluster_id, bids:[{bid_id, store_id, store_domain,
  offer:{product_ref, unit_price, total_price, checkout_url, expires_at}}],
  accepted_bid_ref: None, now: <float epoch>}` (`test_e3_exchange.py:270-297`). The object is
  **mutated in place** across the double-accept calls (`:704`, `:711`) — that shared object is
  how the refusal is driven.
* `code_creator` — both `creator.create_code(store_id, offer)` and `creator(store_id, offer)`
  must work (the double aliases `__call__ = create_code`, `:268`); returns
  `{"code": …, "permalink_url": …}`.
* `checkout_mode` — **three spellings must all be handled**: `"redirect"`, `"shopify_stub"`,
  `"shopify"` (`test_spec_criteria.py:1036`). D23 pins `CHECKOUT_MODE` to
  `{redirect, shopify_stub}`; `"shopify"` is the doc-set spelling. Every spelling must refuse
  the off-domain offer, and **at least one must complete an on-domain accept** — that positive
  control is what stops a surface that refuses everything from satisfying the blocker
  (`test_spec_criteria.py:1052-1055`).

### Result
* `result["permalink_url"]` — a non-empty string containing the created `code`, whose
  `urlsplit(...).hostname` is the **seller's registered `store_domain`**.
* `result["events"]` — a list of records each with `kind`. Filtering to the golden three
  must yield them **in this order**:
  `["accepted", "code_created", "checkout_redirect"]`.
* `redirect` and `shopify` modes must emit **identical ordered `kind` lists** (C11 parity),
  and all three golden kinds must be present in both (`test_e3_exchange.py:677-689`).
* **Double accept:** a second `accept` on the same auction object must refuse — either raise,
  or return no `permalink_url` **and** emit no second `code_created`. `create_code` must have
  been called exactly once in total.
* **S8-3 blocker** (`test_spec_criteria.py:1028`): the checkout host is compared by **exact
  equality against the registered domain and nothing weaker**. Five hostile URLs, each
  defeating a different sloppy check, must be refused in **every** mode with **no code
  created**:

| label | URL shape | defeats (the suite's own attribution, `test_spec_criteria.py:1057-1059`) |
|---|---|---|
| plain other domain | `https://<rival>/cart/…` | — |
| suffix spoof | `https://evil-<seller>.attacker.tld/…` | `in` / substring matching |
| glued suffix spoof | `https://evil-<seller>/…` | `in` / substring matching |
| userinfo spoof | `https://<seller>@attacker.tld/…` | `startswith` on the raw URL |
| subdomain | `https://checkout.<seller>/…` | `endswith` |

  If `accept` raises instead of returning, `creator.calls` must still be empty — the domain
  must be validated **before** a code is minted (`test_spec_criteria.py:1075-1078`).

### 🔴 NEW (D54) — `apps.exchange.src.orchestration.accept_offer`
```python
from apps.exchange.src.orchestration import accept_offer        # :1095, :1187
result = accept_offer(auction=…, bid_ref="bid-a", code_creator=creator,
                      mode="shopify", eligibility=source)
```
**Every call site is 100 % keyword, exactly these five names, no extras** —
`:1111-1117`, `:1129-1135`, `:1148-1154`, `:1161-1167`, `:1223-1234`, `:1261-1272`.
Note the parameter is **`bid_ref`**, not `bid_id`, and **`mode` is `"shopify"` at every
`accept_offer` site** (the three-spelling requirement belongs to the low-level `accept`).

Result fields:

| field | requirement | file:line |
|---|---|---|
| `accepted` | strict `is True` / `is False` — a real `bool`, not merely truthy | `:1118`, `:1136`, `:1155`, `:1168`, `:1235`, `:1276` |
| `permalink_url` | on success a `str` containing the creator's `code` (`"PROXY-TEST-CODE"`) whose `urlsplit(...).hostname == "store-a.example.com"`; on **any** refusal exactly `None` (`is None`) | `:1119-1121`, `:1137-1139`, `:1156`, `:1279` |
| 🆕 `denial_reason` | exactly `None` on success; on refusal its reachable strings contain `"blacklist"` / `"unavailable"` | `:1123`, `:1143-1144` |

Side-effect contract, and it is the whole point:
* exactly **one** `create_code` call on success (`:1122`, `:1171`, `:1236`);
* **zero** `create_code` calls on every refusal path — blacklisted, unavailable, raising read,
  and stale interface version (`:1140`, `:1157`, `:1280`);
* eligibility must actually be consulted: `assert "store-a" in clean.checked` (`:1124`);
* **per-store, not blanket**: with `store-a` BLACKLISTED, `bid_ref="bid-b"` must still return
  `accepted is True` and mint one code (`:1161-1171`). A gate that denies everything fails.

`accept_offer` **re-reads** eligibility at accept time — a store blacklisted *after* it bid
gets no code and no permalink. The suite never asserts that `accept_offer` calls `accept`;
delegation is permitted, not required.

⚠️ **Shared package, two owners.** `apps/exchange/src/orchestration/**` is in **both** T-030's
and T-033's scope. T-030 creates the package with `solicit_bids`; T-033 adds `accept_offer`
and **must not modify `solicit_bids`**. They never run concurrently (T-033 depends on T-030).

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
`packages.store_agent.src.learning` (T-042) with **different arities**. See §A.

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
* 🔴 `report["by_cluster"]` — **an ITERABLE of records, not a mapping.** The suite does
  `{_field(c, "cluster_id"): c for c in _field(report, "by_cluster")}`
  (`test_e3_exchange.py:869`), so it **iterates** and reads `cluster_id` **off each element**.
  A `{"cluster-1": {...}}` return iterates to bare `str` keys and dies inside `_field`.
  The cluster id set must be exactly `{"cluster-1", "cluster-2"}` (`:870`).
  Each record carries `cluster_id`, `lost` (int) and
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
(`pytest.raises(HookProvenanceError)` at `test_e4_store_agent.py:592` — the only
`pytest.raises` in that file.)

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
  non-empty **`rationale` string**. `rationale` is a frozen field name.
* `mode="active"` ⇒ the next run submits once. `mode="killed"` ⇒ submission stops again.
  Every mode still logs.
* A pushed `TrustEventPayload` (`{store_id, event, dim, delta, pseudonymous_context}`) must
  **deterministically change** the next logged bid's `rationale`, and the same event twice
  must produce the same changed rationale.

---

## T-044 — Signed external door — **REWRITTEN BY AMENDMENT 1 (D52)**
scope `packages/store-agent/src/external/**`, `packages/store-agent/tests/**` · **8 tests** (was 2)

> ⛔ **The pre-amendment block for this ticket is void.** The two original tests were not
> extended, they were **replaced**: the old flat keyring `{store_id: secret}` and the old
> envelope-free `_external_payload` were deleted. A worker holding the old contract will
> build a receiver that the frozen suite rejects at `test_e4_store_agent.py:1287-1293`.

```python
from packages.store_agent.src.external import (
    NonceStore, canonical_signing_bytes, payload_hash, receive_bid, sign_bid
)
```
**Exactly five public symbols.** Import sites: `:981, :1026, :1069, :1134, :1159, :1188,
:1244, :1301`. They appear in no other file in the suite.

### Signatures, exactly as the suite calls them

```python
sign_bid(payload, secret)                      # 2 positional, never a keyword
canonical_signing_bytes(payload)               # 1 positional
payload_hash(payload)                          # 1 positional
NonceStore()                                   # zero-argument constructor
receive_bid(payload, signature, keyring, *,    # 3 POSITIONAL, then keywords only
            queue=…, nonce_store=…, now=…, auction_deadline=…,
            blacklist=…, freshness_window_seconds=…)
```
**Frozen keyword names on `receive_bid`:** `queue`, `nonce_store`, `now`, `auction_deadline`,
`blacklist`, `freshness_window_seconds`. The three positional parameter *names* are never used
as keywords and so are free; the **arity and order are not** — position 3 is the keyring.

### 🆕 The required signing envelope — five fields, all mandatory
```python
REQUIRED_SIGNING_FIELDS = ("signer_id", "key_id", "issued_at", "nonce", "schema_version")  # :892
```
`test_e4_store_agent.py:1131` deletes each one in turn and requires a rejection every time
(`:1141-1148`). The full payload the suite hands in (`_external_payload`, `:906-943`):
```
auction_id, store_id, offer{product_ref, unit_price, total_price, discount, commitments,
expires_at}, claims[{key, value, provenance{source,ref,observed_at,authority_rank}}],
message, agent_version, schema_version, signer_id, key_id, issued_at, nonce
```
`signer_id` **defaults to `store_id`** (`:939`) but is a **separate field**: `signer_id` indexes
the keyring and scopes the nonce; `store_id` must independently affect the canonical bytes
(`:1113`).

### 🆕 The keyring is nested `{signer_id: {key_id: secret}}` (`_keyring`, `:895-903`)
```python
{
  "store-external-1": {"key-2026-01": "external-secret-key-0001",
                       "key-2026-07": "external-secret-key-0002"},
  "store-external-2": {"key-2026-01": "external-secret-key-0003"},
}
```
The two signers **deliberately reuse the same `key_id` string** (`EXTERNAL_KEY_ID_2 ==
EXTERNAL_KEY_ID`, `:881`), so a lookup keyed on `key_id` alone resolves signer 2's bid to
signer 1's secret and fails at `:1278`. The **flat** pre-amendment shape
`{"store-external-1": "external-secret-key-0001"}` is passed at `:1291` and **must be
rejected** — there is no legacy fallback.

Key selection, not key trial (`:1260-1269`): `key_id` picks **exactly one** secret. Signing
with the signer's *other* live secret must reject in both directions. An unregistered
`(signer_id, key_id)` pair rejects with **no fallback** to that signer's other key (`:1271-1274`).

### 🆕 `canonical_signing_bytes(payload)`

| property | requirement | file:line |
|---|---|---|
| return type | `bytes`, `bytearray` **or** `str`, non-empty | `:1073` |
| deterministic | same dict ⇒ identical output | `:1079` |
| order-independent | `dict(reversed(list(payload.items())))` ⇒ identical output | `:1080-1083` |
| JSON round-trip stable | `canonical_signing_bytes(json.loads(json.dumps(p))) == base` | `:1084-1086` |
| embeds the digest | `payload_hash(payload)` must be a **verbatim substring** of the text form | `:1098-1101` |

**Verbatim substring coverage** (`:1104-1107`) — for each of `auction_id`, `signer_id`,
`issued_at`, `nonce`, `key_id`, `str(payload[field])` must appear in the text.

**Mutation sensitivity** — the bytes must differ for each of (`:1110-1126`):
`auction_id`, `signer_id`, `store_id`, `issued_at`, `nonce`, `key_id`,
`offer.unit_price` (nested), `schema_version`.
`store_id` and `offer.unit_price` are not in the verbatim list — they are covered transitively
if `payload_hash` covers the body and the digest rides inside the bytes.

### 🆕 `payload_hash(payload)`
A `str` of **length ≥ 32** (`:1090`), stable across a JSON round trip (`:1093`), and it must
**change when `offer.unit_price` changes** (`:1094-1097`). Nothing requires it to be invariant
under envelope-field changes, so hashing the whole payload is permitted.

### 🆕 `NonceStore` — a concrete, directly instantiable class
`NonceStore()` is constructed at **18** sites and is **never subclassed and never replaced by
a duck-typed stand-in**. At `:1190` the instance is bound to a local and **reused across two
`receive_bid` calls** — that is the site which forces a genuinely stateful store. It therefore **cannot be an abstract Protocol/ABC** — `NonceStore()`
must produce a working in-memory store. It is simultaneously the port name and its default
implementation as far as the frozen suite can see.

```python
store.seen(EXTERNAL_SIGNER, NONCE)   # 2 positional, signer first  — :1197, :1218, :1222
store.purge_expired(BEFORE_DEADLINE) # 1 positional                — :1217, :1221
```
Both return values: `seen(...)` is compared `is True` / `is False` — a real `bool`.
**The parameter names are NOT pinned** — the suite never passes them as keywords. `signer_id`
/ `nonce` / `as_of` are D52's spellings and are the sensible choice, but only the **method
names, arity and argument order** are frozen.

**`seen` must be a pure read.** Proof (`:1216-1224`): `purge_expired(BEFORE_DEADLINE)` then
`seen(...) is True`, then `purge_expired(AFTER_DEADLINE)` then `seen(...) is False`. If `seen`
recorded-on-read with a fresh entry, the first read would re-arm the nonce and the second
assertion would fail. The **name of the method `receive_bid` uses to record** a nonce is
unconstrained — a real `NonceStore` is always injected, so nothing pins it.

**Expiry is derived from `auction_deadline`.** The suite never tells the store the deadline;
the only path is `receive_bid(..., auction_deadline="2026-01-01T00:05:00Z")`. The recorded
nonce must survive `purge_expired("2026-01-01T00:04:59Z")` and be gone after
`purge_expired("2026-01-01T00:05:01Z")`. The exact boundary is untested.

**Nonce scoping is per signer, never global** (`:1205-1214`): a different nonce from the same
signer accepts; the *same* nonce string from a **different** signer accepts.

### Result shape read back from `receive_bid`

| field | requirement | file:line |
|---|---|---|
| `accepted` | `is True` on the happy path — a real `bool`. **Never `True`** on any rejection. | `:963`, `:997` |
| unverified marking | either `verified is False`, **or** `verification_status` / `status` normalising to `unverified` / `pending` / `pending_verification` | `:1000-1005` |

**No `reason` / `error` field is read anywhere in T-044.** No error string or substring is
asserted; every string literal in the block is an assertion *message*. Raising is an equally
valid refusal — `_present` (`:960-961`) catches any exception — **provided the queue was never
called**.

### The injected `queue` (`_Recorder`, `:199-232`)
Protocol-agnostic: `write = append = submit = send = log = record = put = enqueue = __call__`.

* `queue.count == 1` on acceptance (`:968`, `:1006`) — not zero, not twice.
* `queue.count == 0` on **every** rejection (`:973`) — rejection always precedes enqueue.
* The queued item must carry a node with `provenance.source == "seller_asserted"` (`:1010-1011`)
  **and** the literal nonce string `"nonce-ext-0001"` (`:1015`).

### Must reject (raise, or `accepted is not True`, **and** never enqueue)
wrong key · garbage signature string · `None` signature · a signature replayed from a
different payload · an offer whose `expires_at` is in the past · a store named in `blacklist=`
(a `list[str]` of store ids, `:1052`) · an unregistered signer · any missing required envelope
field · a tampered signed field · a replayed `(signer_id, nonce)` · arrival after the auction
deadline · an `issued_at` outside the freshness window · a flat keyring.

### Time and freshness — the traps
**Every instant is an ISO-8601 string ending in `Z`**, never a `datetime` — `now`,
`auction_deadline`, `issued_at`, `offer.expires_at` and `purge_expired`'s argument
(`:884-889`). `datetime.fromisoformat` accepts a trailing `Z` only on Python 3.11+.

| case | `issued_at` | `now` | window | verdict | file:line |
|---|---|---|---|---|---|
| control | 00:00:00 | 00:00:05 (+5 s) | default | accept | `:1309-1312` |
| stale | 00:00:00 | 2026-01-31 (+30 d) | default | reject | `:1314-1317` |
| future-dated | 2026-01-01 | 2025-12-31 (issued 86 400 s **ahead**) | default | reject | `:1319-1323` |
| inside explicit | 00:00:00 | 00:02:00 (+120 s) | 300 | accept | `:1327-1334` |
| outside explicit | 00:00:00 | 00:10:00 (+600 s) | 300 | reject | `:1335-1342` |

* Unit is **seconds**. The window is **two-sided** — a future `issued_at` must reject.
* The default window is only bounded to **admit 5 s and reject 2 592 000 s**, with forward
  skew tolerance that must **reject 86 400 s**. Whether the comparison is `<=` or `<` at those
  exact points is unobservable. Nothing pins the default tighter.
* The exact boundary (`age == window`) is untested — inclusive vs exclusive is free.
* **The deadline check is distinct from the freshness check.** The late submission
  (`:1227-1235`) has `issued_at == AUCTION_DEADLINE` and `now == AFTER_DEADLINE`, an age of
  **1 second** — freshness cannot reject it, so a separate deadline check is required. All five
  freshness cases pass `auction_deadline=FAR_FUTURE` so they cannot be rejected by the deadline.

### Suite-local constants (inputs, not contract — but the values are frozen)
```
EXTERNAL_STORE  = EXTERNAL_SIGNER = "store-external-1"    EXTERNAL_SIGNER_2 = "store-external-2"
EXTERNAL_KEY_ID = EXTERNAL_KEY_ID_2 = "key-2026-01"       EXTERNAL_KEY_ID_ROTATED = "key-2026-07"
ISSUED_AT = "2026-01-01T00:00:00Z"   NOW = "2026-01-01T00:00:05Z"
AUCTION_DEADLINE = "2026-01-01T00:05:00Z"
BEFORE_DEADLINE  = "2026-01-01T00:04:59Z"   AFTER_DEADLINE = "2026-01-01T00:05:01Z"
FAR_FUTURE = "2999-01-01T00:00:00Z"          NONCE = "nonce-ext-0001"
```
The module exports **no constants** — the suite imports none and asserts no module-level value.

---

## T-045 — Adversarial seller persona
scope `apps/seller-reference/**` · 1 test

```python
from apps.seller_reference.src.personas import build_persona
pitch = build_persona("aggressive").pitch(intent)   # 1 positional; .pitch(intent) 1 positional
```
🆕 `build_persona(name: str)` returns an object with a 🆕 `.pitch(intent: dict)` method.

Ground truth is the fixture manifest, loaded by `_load_fixture_manifest`
(`test_e4_store_agent.py:461-484`): `sorted(fixtures.rglob("manifest*.json")) +
sorted(fixtures.rglob("*manifest.json"))`, taking the **first one declaring a `personas`
block**:
* `manifest["personas"]["aggressive"]` must exist, with `scripted_claims` (or `claims`) as a
  non-empty list, and the manifest must define `fixture_intent` / `intent` / `intents[0]`.
* The persona's emitted claim **key set must equal** the scripted key set exactly — no more,
  no fewer (normalised by `str().strip().lower()`).
* Where a scripted claim carries a `value`, the emitted value must match (normalised).
* **Every** emitted claim must have `provenance.source == "seller_asserted"`.

⚠️ **Two manifests, two readers — see §A.** T-080's frozen schema for `fixtures/manifest.json`
(`test_e8_proofs.py:30-39`) contains **no `personas` and no `fixture_intent`**, while
`test_e8_proofs.py:305-313` forbids any *second* file named exactly `manifest.json` under
`fixtures/`. Two legal resolutions: put `personas` + `fixture_intent` **into**
`fixtures/manifest.json` (they then fall inside the approval digest body, which is fine), or
ship a differently-named file such as `fixtures/personas/persona_manifest.json` — the stray
check only forbids the exact name `manifest.json`. Note the scan order: `manifest*.json` is
globbed first, so `fixtures/manifest.json` is inspected before any persona file and is skipped
only if it has no `personas` key.

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
  (`test_e5_merchant.py:383` is `assert any(not empty.match(tail) for tail in tails)` — **one**
  non-empty `settings` occurrence suffices; a `{}`, `""`, `null`, `None` or `[]` tail fails).
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
* second call **must** carry one, and the serialized result must contain the substring
  `kind` (`test_e5_merchant.py:665`, a substring test on a lower-cased dump). It must
  **not raise**. *The literal `offer_integrity` is never asserted here* — it is the natural
  `LedgerEventKind` for this event, but the suite only requires a `kind` field to exist.

---

## T-053 — Onboarding interview
scope `apps/merchant/svc/src/onboarding/**`, `apps/merchant/svc/tests/**`, `fixtures/interviews/**`,
`apps/merchant/svc/src/envelope/**` · 3 tests

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
  *"A different envelope yields a different digest" is never asserted directly* —
  `test_e5_merchant.py:741-746` computes `other_digest` inside a `try/except` that falls back
  to `digest[::-1]`, so the property is only exercised through the mismatched-approval case.
  Build it as a real digest anyway; the mismatch case depends on it.
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
`payload = {"dim","type","observed_at"}` and default `kind = "claim_verified"`
(`test_e6_trust.py:187-209`).

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
* `price_honored` (bool) · `discount_honored` (bool)
* 🆕 `pixel_missing` (bool) — `False` when a pixel event was present, `True` when it dropped
* `order_ref` — `"o-1"`
* 🆕 `observed_price` (float)

Behaviour: **the webhook is authoritative.** With a wrong pixel and a matching webhook,
`price_honored is True` and `observed_price == 100.0`. With a matching pixel and a diverging
webhook, `price_honored is False`, `discount_honored is False`, `observed_price == 120.0`.
With no pixel at all, reconciliation still succeeds and `pixel_missing is True`.

---

## T-062 — Trust scoring — **AMENDED BY AMENDMENT 1 (D53)**
scope `apps/trust/src/scoring/**`, `apps/trust/tests/**` · **11 tests** (was 6)

```python
from apps.trust.src.scoring import (
    BLACKLIST_THRESHOLD, Blacklist, CLAIM_TYPE_DIMENSIONS, OBSERVATION_WEIGHTS,
    TRUST_DIMENSIONS, UnmappedClaimType, claim_dimension, is_blacklisted, score,
)
snapshot = score(observations, as_of=AS_OF)   # 1 positional + as_of= (an ISO-8601 string)
```
`observations`: `[{"store_id","dim","type","observed_at"}]` — exactly those four keys
(`test_e6_trust.py:197-199`). `AS_OF = "2026-01-01T00:00:00Z"` (`:79`). Every one of the ~21
`score(...)` call sites uses **one positional list plus `as_of=`** and nothing else.
`score([])` must work and return the prior (`:724`). `score` returns **one snapshot for one
store** — it is `replay` that returns a mapping keyed by `store_id`.

### 🔴 The SIX trust dimensions — frozen (D53). This replaces the old five.
```
price_honored  discount_honored  shipped_on_time  not_returned  feedback_match
catalog_claim_accuracy
```
Canonical site `test_e6_trust.py:83-97`: `TRANSACTION_DIMS` is the 5-tuple, `CATALOG_DIM =
"catalog_claim_accuracy"`, `DIMS = TRANSACTION_DIMS + (CATALOG_DIM,)`.

🆕 **`TRUST_DIMENSIONS`** is imported and compared for **set equality with the six**, plus
`len(set(...)) == 6` (`:893`, `:896-901`). **Order is not asserted and the type is not
asserted** — tuple, list, set or frozenset all pass, as long as `tuple(TRUST_DIMENSIONS)`
yields the six names.

`catalog_claim_accuracy` must **not be an alias**: after catalog-only observations,
`(alpha, beta)` for **every one of the five transaction dims** must equal the prior exactly
(`:929-935`, again at `:1510-1513`). Aggregate `score` still moves — verified up, contradicted
down (`:917`, `:920`) — so the sixth dimension feeds the same rollup.

### `score(...)` result
* `score` — float, strictly in `(0,1)` for the empty prior.
* `confidence` — float, strictly in `(0,1)` at the prior; must **rise** with observations.
* 🆕 `score_version` — non-empty string; must equal `replay`'s.
* `dims[dim]` — for **all six** dims:
  * `alpha`, `beta` — the empty prior is **Beta(2,2)**: `alpha == 2.0 and beta == 2.0`
    for every dim, `catalog_claim_accuracy` included (`:729-730`, `:908`).
  * `decayed_at` — existence and serve/replay equality only; the value is never constrained.
  * 🆕 **`coverage`** — `0.0 <= coverage <= 1.0`, read for **all six** dims at `:987-990`.
    The ordering assertions at `:1021-1025` are single-dimension (`catalog_claim_accuracy`)
    and compare `verified` only against `unsupported` and `ambiguous` — never against
    `contradicted` or the prior (`_coverage`, `:230-236`).
    **This field is new and easy to miss.** It is required on `score(...)`'s dims; it is
    **not** part of `packages.contracts.TrustSnapshot` (T-010) and **not** read by
    `build_snapshot` (T-064).

### 🆕 `OBSERVATION_WEIGHTS` — a published mapping, read by `__getitem__`

| key | assertion | file:line |
|---|---|---|
| `"contradicted"` | `== 2.0` | `:748`, again `:956` |
| `"severe_policy"` | `== 3.0` | `:749` |
| `"mismatch_return"` | `== 1.5` | `:750` |
| `"ambiguous"` | `== 0.0` | `:993` |
| `"unsupported"` | `0.0 <= w < 1.0` | `:1002-1006` |

Weights are consumed as **beta increments**: one `contradicted` observation adds **exactly
2.0** to that dim's `beta` (`:769`, `:956`). One `unsupported` adds a strictly positive amount
below 2.0 (`:772`); combined with the `< 1.0` cap, the effective weight lies in `(0.0, 1.0)`.
Score ordering must be `contradicted < unsupported < prior`.

Observation `type` values the suite passes into `score(...)`: `verified`, `contradicted`,
`unsupported`, `ambiguous`, `fulfilled`. **`severe_policy` and `mismatch_return` appear only
as `OBSERVATION_WEIGHTS` keys** (`:749-750`) — neither is ever fed to `score` as an
observation type. The vocabulary is also **open**: `_behaviour_obs` (`:344`) feeds
`manifest.dishonest_store.behaviours[].type` straight through, so `score` must tolerate any
type the approved manifest scripts.

### 🔴 Outcome treatment — the exact split (D53), `test_e6_trust.py:940-1043`

| `type` | `alpha` | `beta` | mean | `coverage` | `confidence` |
|---|---|---|---|---|---|
| `verified` | `> prior` (`:952`) | `== prior` (`:953`) | `> prior` (`:962`) | `> unsupported` and `> ambiguous` (`:1021`, `:1024`) | `> prior` (`:1027`) |
| `contradicted` | `== prior` (`:955`) | `prior + 2.0` **exactly** (`:956`) | `< prior` (`:962`) | – | – |
| `unsupported` | `== prior` (`:1007`) | delta `<= weight × n` (`:1010`) | `<= prior` (`:1013`), movement **strictly smaller** than contradicted's (`:1016-1018`) | `< verified` (`:1021`) | `< verified` (`:1031`) |
| `ambiguous` | **`(alpha, beta) == prior` exactly** (`:996`) | same | unchanged | `< verified` (`:1024`) | `< verified` (`:1034`) |

`ambiguous` produces **zero** mean movement and lowers coverage/confidence only.

### 🆕 `BLACKLIST_THRESHOLD`
Float-coercible, `0.0 < t < 1.0` (`:809-810`). **No exact value is pinned.** The manifest's
scripted dishonest store, replayed for `manifest.episode_budget` episodes, must end
**strictly below** it (`:816`); a control store running the same number of clean episodes must
end **strictly above** it (`:828`). Both compare the aggregate `score`, not a per-dim mean.

### 🆕 `Blacklist` / 🆕 `is_blacklisted`
```python
bl = Blacklist()                                   # no arguments
bl.add(business_identity=…, reason_code=…, status=…, expires_at=…)   # ALL FOUR KEYWORD
entry = bl.lookup(business_identity)               # 1 positional
is_blacklisted(blacklist, store_record)            # 2 positional
```
* `bl.add` is called **only with keywords** — the four names are frozen (`:840-845`, `:858-863`).
* `status` round-trips through `lookup` for each of 🆕 `"active", "under_review", "appealed",
  "expired"` (`:857`); `entry` exposes `status`, `reason_code`, `expires_at`.
* `is_blacklisted(bl, {"store_id":…, "business_identity":…})` binds to **`business_identity`**,
  not `store_id`: a re-registered store under a new `store_id` is still `True` (`:852`).
  Returns the **`bool` singletons** (`is True` / `is False`).
* **The decision is driven by `status`, not by a clock.** Both the `active` and the `expired`
  entries are added with the **same** `expires_at=AS_OF` (`:862`), and `is_blacklisted`
  receives no `as_of`. `"active"` → `True`; `"expired"` → `False`. `"under_review"` and
  `"appealed"` blacklisted-ness is **not** asserted.
* 🆕 **Fail closed:** the stub is
  `class _UnavailableBlacklist: def lookup(self, business_identity): raise RuntimeError(...)`
  (`:871-873`), and `is_blacklisted(_UnavailableBlacklist(), original) is True` (`:875`).
  Two consequences: an exception from `lookup` is swallowed and answered `True`; and
  `is_blacklisted` may touch **no attribute of the blacklist object other than `lookup`** —
  the stub has nothing else, and it is duck-typed, never `isinstance`-checked.

### 🔴 NEW (D53) — the typed exhaustive claim-type table
```python
from apps.trust.src.scoring import CLAIM_TYPE_DIMENSIONS, UnmappedClaimType, claim_dimension  # :1050
```
* 🆕 `UnmappedClaimType` — `issubclass(UnmappedClaimType, Exception)` (`:1052`).
* 🆕 `CLAIM_TYPE_DIMENSIONS` — `dict(...)`-able, non-empty; every **value** must be one of the
  six dims (`:1055-1061`).
* 🆕 `claim_dimension(claim_type: str) -> str` — one positional. Raises `UnmappedClaimType` for
  `"no_such_claim_type_zz"` **and** for `""` (`:1088-1090`).
* **Exhaustiveness** is a *superset* check: every member of `PUBLISHED_CLAIM_TYPES` must be a
  key of the table; extra keys are allowed (`:1063-1067`).
* The engine's routing must **equal the approved manifest's `claim_type_dimensions` table**
  entry for entry (`:1104-1107`), and the manifest must route at least one type to
  `catalog_claim_accuracy` (`:1108-1111`). **The manifest is the authority, not the engine.**

The 14 published claim types (`test_e6_trust.py:101-119`; the same 14 pairs are restated at
`test_e8_proofs.py:91-108`, with different surrounding comments):

| claim_type | → dimension |
|---|---|
| `price`, `unit_price`, `total_price` | `price_honored` |
| `discount`, `promo_eligibility` | `discount_honored` |
| `delivery`, `shipping_speed`, `dispatch_window` | `shipped_on_time` |
| `return_policy`, `warranty` | `not_returned` |
| `ingredients`, `compatibility`, `nutrition`, `specifications` | `catalog_claim_accuracy` |

The product-fact half is additionally asserted `landed not in TRANSACTION_DIMS` (`:1083`).
**No claim type maps to `feedback_match`** — it is a valid dimension with no claim route,
because D53 hands it back its own post-purchase meaning.

### ⚠️ Cross-module imports inside T-062's own tests
* `from packages.verification import satisfies_hard_constraint` (`:974`) — T-065's module.
  Neither `unsupported` nor `ambiguous` may satisfy a hard constraint (`:1039-1041`).
* `from apps.trust.src.events import InMemoryEventStore, append` (`:569`) — T-060's module.
* `from apps.trust.src.ledger import replay` (`:570`) — **T-011's scope**.

### ⚠️ Ledger replay — `apps.trust.src.ledger` (scope belongs to **T-011**)
```python
replayed_by_store = replay(events, as_of=AS_OF)   # 1 positional + as_of=
replayed = replayed_by_store["s-1"]                # keyed by store_id
```
`replay` must reproduce `score`, `confidence`, `score_version` and every
`dims[dim].{alpha,beta,decayed_at}` **exactly** as `score(...)` served them for the same
observation script (`:590`, `:596-599`).

### Ground-truth dependency
Two T-062 tests fail unless `fixtures/manifest.json` exists and hashes correctly:
`:803` (the dishonest script and `episode_budget`) and `:1095` (`claim_type_dimensions`).
Every E6 manifest read re-verifies `approval.content_hash` (`:295-300`).

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
  (`event.event_id`, `event.kind` preserved), and a non-`None` `pseudonymous_context`.
* `_strings` (`:176-184`) collects string **values** as well as keys, so
  `buyer_email, buyer_name, email, address, account_id` are forbidden as **either** a key or
  a value anywhere in the payload (`:1170-1171`), as are the buyer's literal email and name.
  The source event carries all of them, so they must be stripped, not copied.

### 🆕 `accept_feedback`
`routed_orders` = `{order_ref: {order_ref, store_id, returned: bool, buyer_pseudonym}}`.
Returns `accepted` (bool) and 🆕 `weight` (float).
* Order not in `routed_orders` ⇒ `accepted is False`.
* Routed order ⇒ `accepted is True` and `weight > 0.0`.
* `{"matched_pitch": True}` from a buyer whose order has `returned: True` ⇒ still
  `accepted is True`, but `0.0 < returned_weight < kept_weight`.

---

## T-064 — Trust snapshot
scope `apps/trust/src/snapshot/**`, `apps/trust/tests/**` · 1 test

```python
from apps.trust.src.scoring import Blacklist        # ⚠️ T-062's class
from apps.trust.src.snapshot import build_snapshot
snapshot = build_snapshot(stores, blacklist=blacklist, as_of=AS_OF)   # 1 positional + 2 kwargs
```
`stores`: `[{"store_id", "business_identity", "observations": [...]}]`, where the observations
are built as `_obs(store_id, dim, "verified")` **for every dim in `DIMS`** — i.e. all six
(`test_e6_trust.py:1236`).
`blacklist`: an `apps.trust.src.scoring.Blacklist` instance, populated with
`add(business_identity=…, reason_code=…, status="active", expires_at=None)` (`:1237-1243`).

Result:
* 🆕 `snapshot["version"]` — non-empty when stringified.
* 🆕 `snapshot["stores"][store_id]` → `{store_id, score∈[0,1], confidence∈[0,1],
  dims[…], blacklisted, low_data}`.
* 🔴 **`dims` is iterated over all SIX dimensions**, and for each of them the three fields
  `alpha`, `beta`, `decayed_at` must be readable (`:1263-1266`). A five-dimension served
  snapshot fails. `coverage` is **not** read here.
* `blacklisted is True` exactly for the store whose `business_identity` is in the blacklist.
* 🆕 `low_data is True` for a store with **fewer than `manifest.new_store_prior_n` clean
  episodes**, and `False` for one with `3 × prior_n`.

---

## T-065 — Claim verification
scope `packages/verification/**`, `apps/trust/src/verification/**`, `apps/trust/tests/**` · **6 tests** (was 5)

```python
from packages.verification import verify, satisfies_hard_constraint, FIELD_TOLERANCES
result = verify(pitch, catalog_snapshot, verifier_version)   # 3 positional, never keywords
```

> 🔴 **Import-path trap.** The suite imports from **`packages.verification` directly**, not
> `packages.verification.src`. On disk `packages/verification/` today holds `pyproject.toml`,
> `src/` and `tests/` and has **no top-level `__init__.py`**, so it resolves as an empty PEP-420
> namespace package and every T-065 import fails with `ImportError`. The three symbols must be
> importable from `packages.verification` itself — an `__init__.py` there re-exporting from
> `src/`, or equivalent. Note the asymmetry with T-062, which imports the real subpackage path
> `apps.trust.src.scoring`.

### `verify` inputs
`pitch` = `{pitch_id, store_id, product_ref, text, claims:[{claim_ref, key, value, op?,
claim_type?, provenance{source:"seller_asserted", ref, observed_at, authority_rank}}]}`
`catalog_snapshot` = `{snapshot_id, products:[{product_ref, canonical_name,
attributes:{<key>:{value, unit?}}, offer:{unit_price, currency, availability}}]}`
`verifier_version` = an opaque string (`"v1"`, `"v2"`, `"acceptance-verifier-1"`).

### `verify` result
* `result["claims"]` — each with an identifier (first present of `claim_ref`, `key`, `id`,
  `claim_id`), `status`, `confidence` (float in `[0,1]`), `evidence_refs` (**non-empty** for
  decided golden claims), `observed_value` (optional, but must be stable across runs).
* `result["verifier_version"]` — echoes the argument exactly.
* `result["catalog_snapshot"]` — either the snapshot itself, or a string containing the
  snapshot id.
* `status` ∈ **exactly** `{"verified", "contradicted", "unsupported", "ambiguous"}`.
* 🔴 **NEW (D53) — `claim_type` on every result claim.** `str(claim.claim_type)` must echo
  the input claim's declared `claim_type` verbatim (`test_e6_trust.py:1492-1496`).
  *"An untyped outcome cannot reach a dimension."*

### Behaviour
* **Idempotent** per `(pitch, verifier_version, snapshot)` — the projection
  `(claim_ref, status, confidence, tuple(evidence_refs), observed_value)`, sorted by ref, must
  be identical across two runs on deep copies (`:1337-1341`). No clocks, no randomness, no
  set-iteration order in those fields.
* Changing the snapshot re-verifies (`500 g` claim vs a `900 g` catalog ⇒ `contradicted`).
* A version bump `"v1" → "v2"` is recorded on the result (`:1352-1356`).
* **`verify` must not mutate the snapshot it was given** — `assert snapshot == frozen`
  (`:1400`, `:1411`).
* **Injection inertness.** With the pitch `text` set to a concatenation of
  "IGNORE PREVIOUS INSTRUCTIONS…", a forged JSON verdict, and a `<tool_use>` block
  (`:123-127`): a false claim must still be `contradicted` (`:1405`), and a claim whose *value*
  is the injection text must not be `verified` (`:1408`).
* **`satisfies_hard_constraint(status: str) -> bool`** — 1 positional string arg, returning the
  **`bool` singletons**: `"verified" → True`; `"contradicted" / "unsupported" / "ambiguous" →
  False`. Behaviour on any other input is not asserted.

### 🆕 `FIELD_TOLERANCES`
A published mapping supporting **both** `.get(key, default)` and `__getitem__`, and **required
to contain a `"default"` key** — Python evaluates the `.get` default eagerly, so
`FIELD_TOLERANCES["default"]` runs unconditionally and a missing key raises `KeyError` even
when `"weight"` is present.
```python
tol = float(FIELD_TOLERANCES.get("weight", FIELD_TOLERANCES["default"]))   # :1423
assert 0.0 < tol < 1.0        # RELATIVE tolerances in (0, 1)              # :1424
```
Comparator behaviour against a catalog with
`weight {value:500, unit:"g"}, vegan {True}, material {"Cotton"},
ingredients ["water","glycerin","aloe"], compatible_with ["p-2"]`:

| claim_ref | claim | expected status |
|---|---|---|
| `c-unit` | `weight = "0.5 kg"` | `verified` (unit normalisation) |
| `c-bool` | `vegan = "Yes"` | `verified` (boolean normalisation) |
| `c-string` | `material = "cotton"` | `verified` (case normalisation) |
| `c-contains` | `ingredients contains "aloe"` | `verified` |
| `c-missing` | `ingredients contains "retinol"` | `contradicted` |
| `c-rel` | `compatible_with contains "p-2"` | `verified` |
| `c-rel-absent` | `compatible_with contains "p-9"` | **not** `verified` (weaker than `c-missing`) |
| `c-inside` | `weight = 500·(1 + tol/2) g` | `verified` |
| `c-outside` | `weight = 500·(1 + 3·tol) g` | `contradicted` |

Claims may carry an 🆕 `op` key; the only value used is `"contains"`.

### 🔴 NEW (D53) — the verification → trust bridge (`test_e6_trust.py:1463-1513`)
The route asserted, field by field:
1. `verify(pitch, snapshot, "v1")` where each pitch claim declares `"claim_type": "ingredients"`.
2. Each result claim carries `claim_type`, echoed verbatim (`:1492-1496`).
3. `apps.trust.src.scoring.claim_dimension(claim_type) == "catalog_claim_accuracy"` (`:1497`).
4. The trust observation is `_obs("s-1", claim_dimension(claim_type), str(claim.status))`
   (`:1498`) — **the verification `status` string is used directly as the trust observation
   `type`.** That pins the two vocabularies together: `score` must accept observation types
   `verified`, `contradicted`, `unsupported` and `ambiguous`.
5. `score(observations, as_of=AS_OF)` moves `dims.catalog_claim_accuracy.{alpha,beta}` off the
   prior (`:1504-1509`) and leaves all five transaction dims byte-identical (`:1510-1513`).

Ground truth en route: `c-true-fact` (`ingredients` contains `"aloe"`) → `verified`;
`c-false-fact` (contains `"lanolin"`) → `contradicted` (`:1486-1487`).

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
`profile["pseudonym"]` must **echo the second argument verbatim** —
`assert plain["pseudonym"] == pseudonym` (`test_e7_buyer.py:423`).
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

⚠️ **Name collision:** this `accept` is 2-positional and lives in
`apps.buyer.svc.src.accept`. The exchange's `accept` (T-033) is 4-positional and lives in
`apps.exchange.src.accept`. `test_e7_buyer.py:568` calls the buyer one. See §A.

---

## T-073 — Buyer feedback
scope `apps/buyer/app/feedback/**`, `apps/buyer/svc/src/feedback/**`, `apps/buyer/svc/tests/**` · 2 tests

> ✅ The scope conflict recorded in the previous revision of this file is **fixed**.
> `apps/buyer/svc/src/feedback/**` is now explicitly in T-073's `scope`.

```python
from apps.buyer.svc.src.feedback import feedback_prompt, submit_feedback
prompt = feedback_prompt(order)                # 1 positional
submit_feedback(order, response, sink)         # 3 positional
```
`order` = `{order_ref, store_id, auction_id, routed: bool}` — `routed` is the frozen flag.

### 🆕 `feedback_prompt`
* `routed: False` ⇒ falsy return (`None`, `{}`, `[]` …).
* `routed: True` ⇒ a **single mapping** (not a collection) with:
  * 🆕 `question` — non-empty string,
  * `options` — a `list` of **length ≥ 2**.
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

## T-080 — Ground truth: manifest, golden set, seed generator — **AMENDED (D53)**
scope `fixtures/manifest/**`, `fixtures/approval/**`, `fixtures/seed/**`, `fixtures/catalog/**`,
`fixtures/personas/**`, `fixtures/golden/**`, `fixtures/tests/**`, `fixtures/manifest.json`,
`fixtures/generator/**`, `fixtures/__init__.py` · **6 tests** in `test_e8_proofs.py`
(plus every E4/E6 test that reads ground truth)

> ⚠️ **Ship the PACKAGE spelling of the generator.** The scope covers `fixtures/generator/**`,
> so the importable artifact must be **`fixtures/generator/__init__.py`** — a flat
> `fixtures/generator.py` matches no ticket's scope. `fixtures/manifest.json` and
> `fixtures/__init__.py` are in scope as of the orchestrator's fix made while this file was
> being regenerated; they were not before. See §C.

### Exact paths the suite loads

| repo-relative path | built at |
|---|---|
| `fixtures/manifest.json` | `test_e8_proofs.py:69` (`MANIFEST_REL`), resolved `:141`, `:301`; duplicated at `test_e6_trust.py:244` |
| `fixtures/golden/golden_set.json` | `test_e8_proofs.py:70` (`GOLDEN_SET_REL`), resolved `:237`; duplicated at `test_e6_trust.py:245-246` |
| `<manifest.approval.artifact>` | `test_e8_proofs.py:345-353` — must start with `fixtures/`, contain no `..`, and exist |
| `docs/demo/**/*.md` | `test_e8_proofs.py:271-274`, `:634-636` |
| module `fixtures.generator` | `test_e8_proofs.py:526` |

**Stray-manifest rule:** `FIXTURES_DIR.rglob("manifest.json")` must yield **exactly** the
canonical file (`:305-313`). A file named `*_manifest.json` is **not** caught by this — see the
T-045 note.

### `fixtures/manifest.json` — frozen top-level schema
```jsonc
{
  "seed_category":  "<non-empty string>",                       // :380
  "seed":           <int>,                                      // :381
  "blacklist_threshold": <number, 0 < t < 1>,                   // :383-384
  "episode_budget": <int, not bool, > 0>,                       // :386-389
  "new_store_prior_n": <int, not bool, > 0>,                    // :391-395
  "claim_type_dimensions": { "<claim_type>": "<dimension>" },   // :263-268  🔴 NEW
  "dishonest_store": {
    "store_id":  "<non-empty>",                                 // :398
    "behaviours": [ { "kind": "<non-empty>",                    // :405
                      "dim":  "<one of the SIX trust dims>",    // :406-411  🔴 SIX
                      "type": "<non-empty>" } ]                 // :407
  },
  "expected_trust_trajectory": [ { "episode": <int, 0..budget>, // :418-424
                                   "score": <0..1>,
                                   "tolerance": <float > 0> } ],
  "golden_set": { "path": "fixtures/golden/golden_set.json",    // :225, :229-232
                  "sha256": "<64 hex, lower-cased before matching>",  // :226, :233-235, :241-245
                  "count": <int == len(pitches)> },             // :227, :250-252
  "approval": { "approver": …, "approved_at": …,                // :317-321
                "artifact": "fixtures/…", "content_hash": "<64 hex>" },

  // required by T-045's separate reader — see the T-045 note
  "personas": { "aggressive": { "scripted_claims": [ {"key":…, "value":…}, … ] }, … },
  "fixture_intent": { … }          // or "intent", or "intents":[…]
}
```

### 🔴 `dishonest_store.behaviours[].dim` — the SIX-dimension vocabulary
`test_e8_proofs.py:76-88` defines `CATALOG_DIM = "catalog_claim_accuracy"`,
`TRANSACTION_DIMENSIONS` (the five), and `TRUST_DIMENSIONS = TRANSACTION_DIMENSIONS |
{CATALOG_DIM}`. Every `dim` must be one of:
```
price_honored  discount_honored  shipped_on_time  not_returned  feedback_match
catalog_claim_accuracy
```
Asserted `:408-411`. **And at least one behaviour must carry `dim ==
"catalog_claim_accuracy"`** (`:745-754`). A manifest written against the old five-dimension
vocabulary fails both.

### `approval` — the human-approval artifact
| key | constraint | file:line |
|---|---|---|
| `approver` | non-empty `str`; `.strip().lower()` **not in** `{tbd, todo, none, n/a, unknown}`; must **not** match `/(claude\|gpt\|llm\|\bagent\b\|\bbot\b\|swarm\|automat\|\bai\b\|\bsystem\b)/` | `:318`, `:332-341` |
| `approved_at` | ISO-8601 **with explicit offset** — `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z\|[+-]\d{2}:\d{2})` | `:319`, `:326-331` |
| `artifact` | non-empty `str`, `startswith("fixtures/")`, no `".."`, and the file must exist | `:320`, `:345-353` |
| `content_hash` | non-empty `str` matching `[0-9a-f]{64}` **after `.strip().lower()`** (`:321`), so upper-case hex also passes | `:321`, `:323-325` |

🆕 **The approval digest rule (frozen, reproduced identically in `test_e6_trust.py:272-281`):**
```python
body = {k: v for k, v in manifest.items() if k != "approval"}
text = json.dumps(body, sort_keys=True, separators=(",", ":"))   # ensure_ascii either way
content_hash == hashlib.sha256(text.encode("utf-8")).hexdigest()
```
Enforced at `test_e8_proofs.py:363-368` and on **every** E6 manifest read (`:295-300`).
The file named by `approval.artifact` must contain the digest (case-insensitive, `:355-358`)
**and** the literal `approver.strip()` string (case-sensitive, `:359-361`).

Trajectory rule: strictly increasing episodes, ≥ 2 points (`:427-430`);
`first.score > blacklist_threshold` (`:432-436`) and
`last.score + last.tolerance < blacklist_threshold` (`:437-441`).

### 🔴 NEW — `claim_type_dimensions`, the typed exhaustive table
Exact key spelling: **`claim_type_dimensions`** (`:263`). Non-empty `dict`; every value must
be one of the six dims (`:718-722`). **Exhaustiveness** is a superset check over the 14
published claim types (`:724-728`) — extra keys are allowed, missing ones are not. The 14
required pairs are the table under T-062, asserted at `:730-741`.
`test_e6_trust.py:1095-1111` grades `apps.trust.src.scoring.claim_dimension` **against this
manifest table**, so the manifest is what defines the engine's behaviour — not the reverse.
Note `feedback_match` is a valid `dim` that **no claim type maps to**.

### `fixtures/golden/golden_set.json`
```jsonc
{ "pitches": [ {
    "pitch_id": "<unique, non-empty>",                          // :461, :496-498
    "text": "<non-empty>",                                      // :462
    "catalog_snapshot": { "snapshot_id": "<non-empty>",         // :466-467
                          "products": [ … ] },                  // :468-470 (non-empty)
    "gates": [ "<eval gate token>" ],                           // :472-473
    "claims": [ { "claim_ref": "<unique within the pitch>",     // :481, :489-491
                  "text": "<non-empty>",                        // :482
                  "claim_type": "<a key of claim_type_dimensions>",  // :780-785  🔴 NEW
                  "expected_status": "verified|contradicted|unsupported|ambiguous" } ]  // :483-487
} ] }
```
🆕 **The label key is `expected_status`, never `status`** — the two sides of the S8 comparison
may not share a spelling (`status` is what the *verifier* produces).
* The union of `expected_status` across the set must **equal exactly**
  `{verified, contradicted, unsupported, ambiguous}` (`:499-502`).
* At least one pitch must carry **both** a `verified` and a `contradicted` claim (`:503-506`).
* **At least one pitch must carry all four statuses** — `test_e6_trust.py:1286` searches for it
  and fails if none exists.
* The manifest's `golden_set.sha256` must equal `sha256(file bytes)` and
  `count == len(pitches)`.
* 🔴 **NEW (D53) — product-fact coverage in both directions** (`:763-803`): a claim counts as a
  product fact iff `claim_type_dimensions[claim.claim_type] == "catalog_claim_accuracy"`. The
  set of such claims must contain **≥ 1 with `expected_status == "contradicted"`** (`:794-799`)
  **and ≥ 1 with `expected_status == "verified"`** (`:800-803`). An all-true or all-false set
  fails.
* **Inferred, not asserted:** golden claims almost certainly also need `key` and `value`, and
  pitches need `store_id`/`product_ref`, because `test_e6_trust.py:1303` feeds a golden pitch
  **straight into** `packages.verification.verify(...)`. No assertion in the frozen suite reads
  those fields; the requirement is functional, not textual.

🆕 **Every one of these 11 eval gates must appear in some pitch's `gates`** (`:508-516`; any
alias, matched after `_normalize_token`: lower-case, non-alphanumerics → `_`, collapse, strip):

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

### 🆕 `fixtures.generator` — an importable module
```python
from fixtures.generator import apply, generate          # test_e8_proofs.py:526
payload = generate(seed_category, seed)                 # 2 POSITIONAL — :536, :537, :557
report  = apply(payload, target)                        # 2 POSITIONAL — :563-568
```
**There is no `seed=` keyword.** `seed_category` comes from `manifest["seed_category"]`,
`seed` from `manifest["seed"]`.

* `payload` must be a `dict` and **JSON-serializable plain data**. The canonicaliser is
  `json.dumps(..., sort_keys=True, separators=(",", ":"), default=None)` (`:188-195`).
  `default=None` installs **no** fallback encoder (`json` only calls `default` when it is not
  `None`), so a dataclass or model anywhere inside the payload raises `TypeError`, which
  `:192-195` re-raises as `AssertionError: value is not JSON-serializable plain data`. It
  fails loudly rather than silently comparing equal.
* `payload` must carry non-empty 🆕 `catalog`, `stores`, 🆕 `event_script` (each a `list` or
  `dict`) (`:544-546`). `stores` must include `manifest.dishonest_store.store_id` — as a bare
  id, or as `{"store_id": …}` (`:548-555`).
* **Determinism** is **canonical-JSON string equality**, not object identity (`:539-542`):
  two calls with the same `(seed_category, seed)` must be byte-identical, and
  `generate(seed_category, seed + 1)` must **differ** (`:557-561`). Key order therefore does
  not matter; numeric types do (`1` ≠ `1.0`).
* `apply(payload, {})` returns a report with int 🆕 `created > 0` and int 🆕 `unchanged == 0`.
  Applying the **same payload to the same target object again** returns `created == 0` and
  `unchanged == <the first run's created>` (`:571-577`). The target dict is the only state
  carried between the two calls, so **`apply` must mutate `target` in place**.

> Separately: `Makefile:19` is `demo-seed: ; @./.venv/bin/python -m fixtures.seed --category "$(SEED_CATEGORY)"`.
> `fixtures.seed` is a **different** module, runnable as `__main__`. The frozen suite never
> imports it, but the Makefile is hashed, so it must exist for `make demo-seed` to run.

---

## T-081 — Dishonest simulation script
scope `services/sim/**` · 1 test

```python
from services.sim.src.dishonest import run_dishonest_script
emitted = run_dishonest_script(manifest, seed)   # 2 positional — :598, :621
```
The first argument is the **entire parsed manifest**, not `dishonest_store`.
🆕 Returns a **`list`** of records, each a `dict` with:
* 🆕 `kind` — non-empty string (`:605`);
* 🆕 `episode` — an `int` (not `bool`) with `0 <= episode <= manifest["episode_budget"]` (`:606-610`).

Contract:
* `[r["kind"] for r in emitted]` must **equal, element for element**,
  `[b["kind"] for b in manifest["dishonest_store"]["behaviours"]]` (`:613-616`) — same length,
  same order, same strings. Only `kind` is compared; `dim` and `type` are not checked here.
* Episodes must be **non-decreasing** (`:617-619`).
* Deterministic: a second call with the same `(manifest, seed)` is canonical-JSON identical
  (`:621-624`) — so every record must be plain JSON data.

---

## T-085 — Starting-slice demo runbook
scope **`docs/demo/starting-slice.md`**, `docs/tests/**` · 2 tests

> 🔴 **T-085's scope narrowed.** It is now a **single named file** plus `docs/tests/**` — no
> longer `docs/demo/**`. The second runbook, `docs/demo/shopify-onboarding-extension.md`,
> belongs to **T-087**.

### The one frozen test reads the UNION of every markdown file under `docs/demo/`
`_markdown_under(REPO_ROOT / "docs" / "demo")` = `sorted(p for p in directory.rglob("*.md")
if p.is_file())` (`test_e8_proofs.py:271-274`, `:634-636`), all concatenated with `"\n"`
(`:638`). At least one file must exist.

* Section headings, extracted with `(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$` and normalised, must
  contain the tokens 🆕 `provisioning`, 🆕 `auction`, 🆕 `interview` as **substrings** of the
  heading blob (`:641-653`).
* The text must contain `t-053` (case-insensitive, `:657-659`) and must match
  `/live[\s_\-]*llm/i` (`:660-662`).
* Every `make <target>` the runbook tells a human to run (a line starting `make ` after
  stripping **all** leading `$`; only the **first** non-flag word per line is collected, so
  `make deps-up deps-down` registers `deps-up` alone) must be **defined in the repo-root `Makefile`**
  (`^target\s*:` , not `:=`) — `:664-693`. 🆕 **`make e2e-live` must be one of them** (`:677`).
  The Makefile is hashed: only its 14 existing targets may be referenced.

### ⚠️ This test cannot pass on T-085's file alone
`docs/demo/starting-slice.md` (T-085) covers the **auction** beat and explicitly excludes
Shopify provisioning and the onboarding interview. The `provisioning` and `interview`
headings, the `t-053` reference, the live-LLM flag and `make e2e-live` live in
`docs/demo/shopify-onboarding-extension.md` — **T-087's** file. The single frozen test is
marked **T-085** but is only satisfiable once **both** files exist. T-087 has no marker of its
own (§D) yet its deliverable is load-bearing for a T-085 goal. Schedule accordingly, and note
that a stray `make <target>` in **either** file that the Makefile does not define turns this
test red.

### No timeline language (also T-085's marker, in `test_spec_criteria.py:888`)
`SPEC.md`, `DESIGN.md`, `TASKS.md`, `EXECUTION.md` and every `.md/.mdx/.rst/.txt` under `docs/`
whose path parts do **not** include `tests` must contain none of (outside fenced code / inline
code): weeks, weekly, sprints, deadlines, milestones, months, monthly, quarters, days, daily,
overnight, eta, timeframes, any weekday name, any month name except March/May (absent from
the list), `q1`–`q4`, or an ISO date `\d{4}-\d{2}-\d{2}`.

---

# Cross-cutting findings

## A. Name collisions — same name, different module, different signature
| name | module A | module B |
|---|---|---|
| `initial_state` | `apps.exchange.src.policy` — **4 positional** `(stores, clusters, snapshot, config)` (T-034) | `packages.store_agent.src.learning` — **1 positional** `(prior)` (T-042) |
| `update` | `apps.exchange.src.policy` — `(state, outcomes)` (T-034) | `packages.store_agent.src.learning` — `(state, records)` (T-042) |
| `accept` | `apps.exchange.src.accept` — **4 positional** (T-033), called at `test_e3_exchange.py:658` etc. | `apps.buyer.svc.src.accept` — **2 positional** (T-072), called at `test_e7_buyer.py:568` |
| `accept` vs `accept_offer` | `accept(auction, bid_id, creator, mode)` — 4 **positional** | `accept_offer(auction=, bid_ref=, code_creator=, mode=, eligibility=)` — 5 **keyword**. Same package family, different layer, different calling convention, and the second parameter is spelled **`bid_ref`** not `bid_id`. |
| `feedback` (module) | `apps.trust.src.feedback` — `push_trust_event(delta, sink)`, `accept_feedback(order_ref, response, routed_orders=)` (T-063) | `apps.buyer.svc.src.feedback` — `feedback_prompt(order)`, `submit_feedback(order, response, sink)` (T-073). **`accept_feedback` takes an `order_ref` STRING; `feedback_prompt`/`submit_feedback` take the order DICT.** Both gate on "routed"; the check must not be shared across the buyer/trust boundary. |
| `BLACKLISTED` — five spellings of one concept | `apps.trust.src.scoring.Blacklist` / `is_blacklisted` (T-062, keyed on `business_identity`); `apps.exchange.src.eligibility.BLACKLISTED` (T-030, an eligibility **status constant**) | `"blacklisted"` / `"blacklist_expired"` are frozen **LedgerEvent kinds** (`test_spec_criteria.py:872-873`); `blacklisted` is a **bool flag** on a TrustSnapshot row (`test_e3_exchange.py:198`); `blacklist=` is a **`list[str]` kwarg** on `receive_bid` (T-044). Four modules, one word. |
| `signed_fetch` | the T-020 guard functions live beside it in `services.ingest.src.adapters` | the T-023 **adapter class/module** of the same name in the same package |
| `verify` | `packages.verification.verify` (T-065) | `apps.trust.src.ledger.verify_chain` is the *ledger* one — do not merge them |
| `append` | `apps.trust.src.events.append(store, event)` — a **module-level function**, not a store method | — |
| `Blacklist` / `is_blacklisted` | defined once in `apps.trust.src.scoring` (T-062) | but constructed by **T-064**'s test too — read-only cross-ticket use |
| `TRUST_DIMENSIONS` | `apps.trust.src.scoring.TRUST_DIMENSIONS` — the **product** constant, six names, set-compared | `test_e8_proofs.py:88` / `test_e6_trust.py:97` define suite-local copies. The suite's copies are the authority; the product's must match as a set. |
| `status` vs `expected_status` | `status` is what the **verifier** emits (`packages.verification`) | `expected_status` is what the **approved golden set** declares. They must never share a spelling. |
| `type` vs `claim_type` | `type` is a **trust observation type** (`verified`/`contradicted`/…) on `manifest.dishonest_store.behaviours[]` and on `score()`'s observations | `claim_type` is the **routing vocabulary** (`ingredients`, `price`, …) in `claim_type_dimensions`. Different fields, different vocabularies. |
| `key` | in a golden claim, the **catalog attribute name**; the identity field is `claim_ref` | but `test_e6_trust.py:424-429` falls back to `key` as an *identifier* when `claim_ref` is absent, and `test_e4_store_agent.py:1379` uses `key` as the persona claim's identity. **Always carry an explicit `claim_ref`** so the fallback never fires. |
| `golden` | `fixtures/golden/golden_set.json` (T-080) | unrelated to the "golden envelope" in `fixtures/interviews/` (T-053) and the "golden intents" in `fixtures/dialogues/` (T-071) |
| two manifests | `fixtures/manifest.json`, pinned and stray-checked (T-080) | `test_e4_store_agent.py:461-484` (T-045) `rglob`s `manifest*.json` for the first file declaring `personas`. See the T-045 note. |

## B. Scope-glob vs `ticket()` marker disagreements
The marker names who is **blamed** when the test is red; the scope glob names who is
**allowed to write the file**. Where they differ, the packet must say so explicitly.
Resolved by glob-matching against `tickets.json`, treating T-000's `**` as a universal
catch-all.

> **`scope` is advisory, not enforced.** No harness code matches paths against it; it reaches
> a worker only through the ticket packet. So every row below is a **briefing defect** — a
> worker will be told not to touch a file its own test needs — rather than a hard block. That
> makes them cheap to fix (edit `tickets.json`, which is orchestrator-owned and unhashed) and
> expensive to ignore (a worker that obeys its packet leaves the goal red).

| module / path | scope glob permits | test marker blames | note |
|---|---|---|---|
| `apps/exchange/src/eligibility`, `apps/exchange/src/orchestration` | **T-030** (both) and **T-033** (orchestration only) | **T-030**, **T-032**, **T-033** | 🔴 **New with amendment 1.** T-032's `test_both_eligibility_gates_require_a_versioned_seller_eligibility_interface` (`test_e3_exchange.py:1176`) imports six names from `eligibility` and both callables from `orchestration`, but T-032's scope is `apps/exchange/src/ranking/**` only. `tickets.json` adds a `T-032 → T-033` dependency for this. **T-032 must not create either module.** |
| `apps/exchange/src/orchestration` (shared) | **T-030** and **T-033** | — | T-030 creates the package with `solicit_bids`; T-033 adds `accept_offer` and must not touch `solicit_bids`. They never run concurrently (T-033 depends on T-030). |
| `apps/trust/src/ledger` (`verify_chain`, `replay`) | **T-011** | **T-060**, **T-062** | T-060's scope is `apps/trust/src/events/**`, T-062's is `apps/trust/src/scoring/**`. Neither may write `src/ledger/**`. T-011's own two tests never import it. |
| `packages/store-agent/src/hooks` (`call_log`, `emitted_claims`) | **T-040** | **T-041** | T-041 owns `src/runtime/**` only. The two attributes must be shipped by T-040. |
| `apps/trust/src/events` | **T-060** | T-060 **and T-062** | T-062's replay test constructs `InMemoryEventStore` and calls `append`. Read-only cross-ticket use; T-062 `depends_on` must include T-060. |
| `apps/trust/src/scoring` (`Blacklist`) | **T-062** | T-062 **and T-064** | T-064's snapshot test constructs `Blacklist()` and calls `.add(...)`. Read-only cross-ticket use. |
| `packages/verification` (`satisfies_hard_constraint`) | **T-065** | T-065 **and T-062** | `test_e6_trust.py:974`, inside a T-062 test. Read-only cross-ticket use, correctly ordered. |
| 🔴 `apps/trust/src/scoring` (`claim_dimension`, `score`) | **T-062** | **T-065** | `test_e6_trust.py:1470`, inside a **T-065**-marked test. **The dependency edge is missing and the cycle is real** — see B1 below. |
| `packages/contracts` (`Provenance`) | **T-010** | T-010 **and T-072** (+ an unmarked E2 helper) | Read-only cross-ticket use, guarded by `try/except`. |
| `docs/demo/shopify-onboarding-extension.md` | **T-087** | **T-085** | 🔴 **New with amendment 1.** The single frozen runbook test globs `docs/demo/**` and reads the union; three of its five requirements live only in T-087's file. T-087 itself has no marker. See T-085. |
| `apps/merchant/svc/src/{codes,collector,onboarding}` | **T-050** (`apps/merchant/**`) and the specific ticket | T-052 / T-051 / T-053 | Benign: T-050's glob is a superset, so no conflict. |
| `apps/buyer/svc/src/{accept,intent,feedback}` | **T-070** (`apps/buyer/**`) and T-072 / T-071 / T-073 | T-072 / T-071 / T-073 | Benign superset. **The T-073 conflict recorded in the previous revision is fixed** — `apps/buyer/svc/src/feedback/**` is now in T-073's scope. |
| `services/ingest/src/adapters` | T-020 **and** T-023 | T-020 and T-023 | **Two tickets legitimately share one package.** They must not both create `__init__.py`. |

### 🔴 B1 — one cross-ticket import is genuinely UNSCHEDULABLE, not just mis-worded
`test_e6_trust.py:1462-1470` is marked **`ticket("T-065")`** and does
`from apps.trust.src.scoring import claim_dimension, score`, calling `score` at `:1501-1502`.
`apps/trust/src/scoring/**` is **T-062's** scope. But the edges run the wrong way:

```
T-065 depends_on ["T-010", "T-021", "T-060", "T-080"]    <- no T-062 edge
T-062 depends_on ["T-060", "T-065", "T-080"]             <- depends on T-065
```

T-065 is therefore **scheduled before** T-062, and one of its own six goals
(`test_verification_results_are_typed_and_route_into_the_catalog_claim_dimension`) cannot pass
when T-065 merges — `claim_dimension` and `score` do not exist yet. The cycle is real:
T-062 needs `satisfies_hard_constraint` from T-065 (`:974`) and T-065 needs `claim_dimension`
and `score` from T-062. **This needs an orchestrator decision** — either accept that this one
T-065 test only goes green after T-062 merges, or split the shared boundary out. It is not
fixable by re-wording a packet.

**Dependency edges that DO cover the other read-only rows** (verified in `tickets.json`):
`T-062 depends_on [T-060, T-065, T-080]` · `T-064 depends_on [T-062]` ·
`T-032 depends_on [T-031, T-033, T-065]` · `T-033 depends_on [T-030, T-036]` ·
`T-044 depends_on [T-010, T-011, T-041]`. For those, only the **write permission** wording in
the packets is wrong.

### B2 — the flat-vs-package trap
Every `.../<name>/**` scope glob covers **only the package spelling**. For a module that does
not exist yet, the marker's scope permits `X/__init__.py` and **no ticket's scope permits
`X.py`**. This affects `apps/exchange/src/eligibility` and `.../orchestration` (T-030/T-033),
`packages/contracts` (T-010), `packages/llm` (T-014) and `packages/verification` (T-065).
Combined with rule 0.7, the required artifact in every one of those cases is
**`<dir>/__init__.py`**, never `<dir>.py`. No ticket packet says this today.

## C. Paths NO ticket scope covers
Measured over both the flat (`X.py`) and package (`X/__init__.py`) spellings of all 37
modules. As first regenerated, three paths fell outside every ticket's scope glob; the
orchestrator has since closed two. **One remains.** Rows are kept here with their status so
the history is legible:

| path | required by | covered by |
|---|---|---|
| ✅ `fixtures/manifest.json` | T-080 (`test_e8_proofs.py:69`), T-081, T-045, T-062×2, T-064, T-065 | **T-080** — fixed after this file first reported it |
| ✅ `fixtures/generator/__init__.py`, `fixtures/__init__.py` | T-080 (`test_e8_proofs.py:526`) | **T-080** — fixed. ⚠️ Only the **package** spelling is covered: `fixtures/generator.py` still matches no scope, so ship `fixtures/generator/__init__.py` (consistent with B2). |
| 🔴 `docs/demo/e2e_live.sh` | the **hashed** `Makefile:20` — `e2e-live: ; @bash docs/demo/e2e_live.sh` — and `make e2e-live` must be referenced by the runbook (`test_e8_proofs.py:677`) | **still only T-000's `**`.** The Makefile is hashed, so the path cannot be changed to suit a ticket; a scope must be widened instead (T-085 or T-087 is the natural owner). |

**Status:** T-080's globs originally stopped at `fixtures/manifest/**` (the *directory*), not
`fixtures/manifest.json` (the *file* one level up). While this file was being regenerated the
orchestrator widened T-080's scope to add `fixtures/manifest.json`, `fixtures/generator/**`
and `fixtures/__init__.py`, which closes two of the three rows. `docs/demo/e2e_live.sh`
remains uncovered. (`tickets.json` is orchestrator-owned and **not** in the freeze manifest,
so amending a scope glob is not an amendment to the frozen suite.)

Non-module path contracts and their owners:
`fixtures/golden/golden_set.json` and the approval artifact → **T-080** ·
`fixtures/interviews/` → **T-053** · `fixtures/dialogues/` → **T-071** ·
`fixtures/pages/` → T-021 · `fixtures/er/` → T-022 · `fixtures/mcp/` → T-023 ·
`fixtures/envelopes/` → T-040 · `db/migrations/*.sql` → **T-011** ·
`docs/demo/starting-slice.md` → **T-085** · `docs/demo/shopify-onboarding-extension.md` →
**T-087** · root `Makefile`, `docker-compose*.yml`, the 16 required directories → **T-000**.

## D. Tickets with acceptance criteria but ZERO acceptance tests
**Ten of the 47** carry no `ticket()` marker anywhere in the frozen suite. Nothing in it can
score them.

| ticket | subject | is any of it frozen elsewhere? |
|---|---|---|
| **T-013** | Shopify stub | **No.** `conftest.py:84` registers the `services.shopify_stub` alias, but **no test imports through it**. (The token `shopify_stub` also appears at `test_spec_criteria.py:683` — a directory-name string in T-000's layout check — and at `:1032`/`:1036` as a `CHECKOUT_MODE` spelling. Neither is an import.) `services/shopify-stub/**` is graded only by T-000's directory-existence check. |
| **T-024** | Differential refresh | **Nothing.** `grep -rn "scheduler\|refresh\|cadence"` over the suite returns zero. Content-hash gating exists (`needs_extraction`/`content_hash`, `test_e2_ingestion.py:350`) but that is **T-021** and says nothing about cadence config or a refresh route. |
| **T-031** | Retrieval + fit scoring | **Almost nothing.** `grep -rn "retrieval"` → zero. `fit_score` appears only as a **consumed** field — `test_e3_exchange.py:635` asserts a slot's `fit_score` is a `float` (T-032). Retrieval, hard-criteria filtering and the latency budget are unfrozen. |
| **T-036** | Checkout provider port | **Partially — see the breakdown below.** |
| **T-054** | Dashboard | **No, and the suite says so.** `test_e5_merchant.py:18-21`: *"T-054's dashboard is TypeScript and structurally uncoverable by a pytest suite… `e5_merchant_passing == 10` therefore does NOT mean the dashboard was verified."* The kill switch it wires to **is** frozen — but on the agent side (`test_e4_store_agent.py:807`, kill switch at `:823-825`), not the dashboard. |
| **T-082** | Full S1 e2e chain | **Directory only.** `e2e` must exist (`test_spec_criteria.py:687`) and is scanned by a file-content lint (`:697`). The per-kind LedgerEvent multiset the ticket demands is asserted nowhere; `:849` freezes the **enum** (T-010's marker), not any run's counts. |
| **T-083** | Both learning loops move | **Unit-level yes, e2e no.** Exchange loop `test_e3_exchange.py:753-760` (T-034); store loop `test_e4_store_agent.py:702-708` (T-042); cluster isolation `test_e3_exchange.py:757`. The seeded A/B golden reorder run against compose is unfrozen. |
| **T-084** | Dishonest store ends blacklisted | **Entry point only.** `run_dishonest_script` is imported at `test_e8_proofs.py:587` under **T-081's** marker, not T-084's. Trajectory tolerance bands and the blacklist episode ±band are unfrozen; zero post-blacklist exposure is frozen at unit level by `test_e3_exchange.py:797` (T-034). |
| **T-086** | Onboarding → first real bid | **Each half is frozen; the join is not.** T-053's half: `envelope_from_transcript` (`test_e5_merchant.py:690`), `activate`/`approval_digest` (`:723`), `edit` (`:787`). T-043's half: shadow submits nothing (`test_e4_store_agent.py:780-792`), activation and kill switch (`:807`; the flip to `"killed"` is `:823-825`). But T-086's own criteria are stated over the **presence/absence of a `bid_placed` LedgerEvent**, and `bid_placed` appears only at `test_e1_foundation.py:752` (a contract fixture) and `test_spec_criteria.py:855` (the frozen enum). **No test observes `bid_placed` across a shadow-vs-active run.** |
| **T-087** | Shopify/onboarding extension runbook | **Yes, indirectly and load-bearing.** T-087 has no marker, but the T-085-marked test (`test_e8_proofs.py:632`) globs **every** markdown file under `docs/demo/` and asserts over their union — and the `provisioning` heading, the `interview` heading, `t-053`, the live-LLM flag and `make e2e-live` are all content T-085's own acceptance 2 forbids it from writing. **T-087 is a hard prerequisite for that T-085 goal turning green**, and any undefined `make` target it names turns that goal red. **Caveat:** the test reads the *union* and cannot tell which file supplied what, so T-085 could satisfy every assertion alone — in direct contradiction of its own ticket. The **existence** of `docs/demo/shopify-onboarding-extension.md` at its pinned path is not frozen, and neither is T-087's acceptance 4 (the dishonest-store / T-084 beat). |

`services/ingest/src/graph/**`, `apps/trust/src/verification/**` and `pixel/**` are in some
ticket's scope but are imported by no test either.

### T-036 in detail — what T-033's marker does and does not freeze

`apps.exchange.src.checkout` is imported by **no test** (grep across the whole suite: zero
hits), and the identifiers `CheckoutProvider` and `SimulatedRedirectProvider` appear
**nowhere** in the frozen suite.

| T-036 criterion | frozen? | evidence |
|---|---|---|
| **1.** a named `CheckoutProvider` port; an unregistered mode **raises** rather than falling back; a lint forbidding code-minting outside a provider | ❌ **No** | The names appear nowhere. `test_spec_criteria.py:1041-1044` wraps the call in `try: … except Exception: continue`, which **tolerates** a raising mode and never **requires** one. No lint or grep test exists. |
| **2.** C11 parity — every provider emits identical ordered LedgerEvent kinds | ✅ Yes | `test_e3_exchange.py:677`, `:688` `assert redirect_kinds == shopify_kinds`, plus `:691-692` requiring the three golden kinds present so an empty stream is not "parity". |
| **2b.** `SimulatedRedirectProvider` by name, with no Shopify dependency | ❌ No | Name absent. |
| **3.** exact-host validation of `checkout_url` vs `store_domain` **before** minting; five spoofs refused; on-domain completes | ✅ **Yes, word for word** | `test_spec_criteria.py:1028` (blocker S8-3): `:1047` hostname equality, `:1060-1066` the five hostile hosts, `:1088` `assert not creator.calls`, `:1052` the positive control. |
| **4.** `accept(auction, bid_id, code_creator, mode)` keeps four positional parameters | ✅ Yes | every call site, `test_e3_exchange.py:658` … `test_spec_criteria.py:1073`. |

**Verdict:** the *observable behaviour* is fully frozen under **T-033's** marker; the
*architecture* T-036 exists to impose is not. A worker could satisfy every T-033 test with a
hard-coded `if mode == …` chain inside `accept()` and never build a port. T-036's scope
(`apps/exchange/src/checkout/**`) is imported by nothing, so the module location is free —
provided `accept` keeps its four positional parameters.

## E. Every 🆕 author-chosen name, in one list
Names a worker will **not** find in `SPEC.md`, `DESIGN.md`, `TASKS.md`, `EXECUTION.md` or
`tickets.json` (measured by word-boundary search over all five). Paste the relevant subset
into each packet verbatim.

**Modules:** `apps/buyer/svc/src/profile`, `apps/buyer/svc/src/vault`,
`apps/merchant/svc/src/install`, `apps/seller-reference/src/personas`,
`services/sim/src/dishonest`.

**Types / constants:** `LedgerEventKind`, `REQUIRED_SCOPES`, `USER_AGENT`,
`OBSERVATION_WEIGHTS`, `BLACKLIST_THRESHOLD`, `FIELD_TOLERANCES`, `TRUST_DIMENSIONS`,
`CLAIM_TYPE_DIMENSIONS`, `UnmappedClaimType`, `RecordedLLM`, `PseudonymVault`,
`InMemoryEventStore`, `ToolHooks`, `HookProvenanceError`, `AgentRunner`.

**Functions:** `validate_bid`, `resolve_model`, `is_fetch_allowed`,
`is_redirect_chain_allowed`, `may_fetch`, `needs_extraction`, `extract_claims`,
`build_loss_report`, `enforce_hook_provenance`, `build_network_prior`, `initial_state`,
`sample_depth`, `build_persona`, `create_code`, `on_redemption`, `accept_pixel_event`,
`envelope_from_transcript`, `approval_digest`, `activate`, `edit`, `verify_chain`,
`is_blacklisted`, `claim_dimension`, `push_trust_event`, `accept_feedback`, `build_snapshot`,
`satisfies_hard_constraint`, `build_profile`, `provenance_label`, `confirm`,
`feedback_prompt`, `submit_feedback`, `run_dishonest_script`, `fetch_catalog`, `to_upserts`.

**Spelling caveat on the amendment's newest constants.** The docs carry the **manifest key**
`claim_type_dimensions` (lower-case, `DESIGN.md` and `tickets.json`) but **not** the
module-level constant `CLAIM_TYPE_DIMENSIONS` that `apps.trust.src.scoring` must export —
that exact spelling has zero hits. Likewise `TRUST_DIMENSIONS`, `claim_dimension`,
`UnmappedClaimType`, `FIELD_TOLERANCES`, `OBSERVATION_WEIGHTS` and `BLACKLIST_THRESHOLD` have
zero hits even case-insensitively. **Amendment 1's eligibility and signing vocabulary landed
in the docs; its sixth-dimension plumbing did not.** Those six are the highest-risk names in
the current run and every one is asserted structurally:
`issubclass(UnmappedClaimType, Exception)` (`test_e6_trust.py:1052`),
`len(set(TRUST_DIMENSIONS)) == 6` (`:901`),
`FIELD_TOLERANCES["default"]` with `0.0 < tol < 1.0` (`:1422-1424`),
`float(OBSERVATION_WEIGHTS["contradicted"]) == 2.0` (`:748`).

**Attributes / result fields:** `call_log`, `emitted_claims`, `ingest_trust_event`,
`head_hash`, `broken_at`, `ok`, `requires_verification`, `pixel_missing`, `observed_price`,
`question`, `event_script`, `expected_status`.

**Documented as prose, not as a field name — treat with the same care:** **`coverage`** on
`score(...)`'s dims. `tickets.json`'s D53 text says *"its dominant effect is on
confidence/coverage"*; it never names `coverage` as a key of `dims[dim]`, which is what
`test_e6_trust.py:230-236` reads. Same for `fixtures/generator`: the path appears in prose,
the importable module name does not.

### Frozen keyword-argument spellings
These are **not** all 🆕 — most are ordinary words that do appear in the doc set. They are
listed because the suite passes them **by keyword**, so the *parameter name* is part of the
contract and a synonym breaks the call:

`path=`, `trust_snapshot=` (`validate_bid`) · `confidence_floor=` (`extract_claims`) ·
`as_of=` (`score`, `replay`, `build_snapshot`) · `blacklist=` (`receive_bid`, `build_snapshot`) ·
`queue=`, `nonce_store=`, `now=`, `auction_deadline=`, `freshness_window_seconds=` (`receive_bid`) ·
`hooks=` (`runtime.bid`) · `routed_orders=` (`accept_feedback`) · `confirmed=` (`confirm`) ·
`now=` (`create_code`) · `business_identity=`, `reason_code=`, `status=`, `expires_at=` (`Blacklist.add`) ·
`sink=`, `submitter=`, `mode=` (`AgentRunner`) ·
`roster=`, `solicitor=`, `eligibility=`, `now=` (`solicit_bids`) ·
`auction=`, `bid_ref=`, `code_creator=`, `mode=`, `eligibility=` (`accept_offer`) ·
`store_id=`, `status=`, `reason=` (`EligibilityDecision`).
Plus the config keys `exploration_floor` (`policy.initial_state`) and `default`
(`FIELD_TOLERANCES`). Genuinely absent from the doc set among these:
`trust_snapshot`, `confidence_floor`, `routed_orders`, `sink`, `submitter`, `exploration_floor`.

**No longer 🆕 — amendment 1's `tickets.json` rewrite documented these**, so a worker reading
the packet will now meet them: `SellerEligibility`, `SELLER_ELIGIBILITY_INTERFACE_VERSION`,
`ELIGIBLE`, `BLACKLISTED`, `UNAVAILABLE`, `solicit_bids`, `accept_offer`, `denied`,
`denial_reason`, `interface_version`, `canonical_signing_bytes`, `payload_hash`, `NonceStore`,
`seen`, `purge_expired`, `signer_id`, `key_id`, `issued_at`, `nonce`, `schema_version`,
`nonce_store`, `auction_deadline`, `freshness_window_seconds`, `catalog_claim_accuracy`,
`claim_type`, `claim_type_dimensions`, `collect_bids`, `fallback`, `eligible`, `components`,
`exclusion_reasons`, `quarantined`, `rationale`, `low_data`, `score_version`, `content_hash`,
`CatalogAdapter`, `sign_bid`, `receive_bid`, `replay`, `created`, `unchanged`, `episode`,
`options`, `routed`, `pseudonymous_context`, `price_honored`, `discount_honored`,
`accepted`, `weight`, `candidates`, `ranked`, `catalog`, `stores`, `kind`,
`EligibilityDecision`, `solicited`, `reasons`, `apps/buyer/svc/src/feedback`.
A handful of these (`apply`, `generate`) appear in the doc set only as ordinary English words
rather than as identifiers — treat them with the same care as a 🆕 name.
