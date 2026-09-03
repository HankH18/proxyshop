# ProxyShop — ticket ledger (open tickets + scheduling constraints)

> **Regenerated 2026-09-02 from `tickets.json`, at `main` = `1b08077`.**
>
> `tickets.json` is the authoritative ticket graph. **This file is a derived view of it and
> nothing else.** Every number below was recomputed from the graph in this pass. When the two
> disagree, the graph wins and this file is regenerated. The reverse — editing `tickets.json`
> to make it agree with this file — corrupts the authoritative source and must never be done.
>
> **The graph is 101 tickets and 141 edges.** That pair is stated once, here, and every count
> in this file is consistent with it. The revision this replaces stated the ticket count seven
> different ways (1, 10, 16, 25, 39, 82 and 98) and never once said 101; it was also missing
> **T-151, T-152 and T-153** entirely. Both failures are repaired here.

## Totals — measured from the graph, not remembered

| quantity | value |
|---|---|
| tickets in `tickets.json` | **101** |
| dependency edges | **141** |
| roots (empty `depends_on`) | **20** — T-000 plus T-135 – T-153 (see the next section) |
| max depth | **10**, deepest **T-087** |
| graph health | acyclic, **0** dangling dependency references, **0** duplicate ids |
| **closed** — work landed on `main` | **50** |
| **open** — the contents of this ledger | **51** = **27 READY** + **24 BLOCKED** |
| open acceptance criteria | **136**, across the 39 open tickets that carry an `acceptance` array |
| open tickets with **no** acceptance array and **no** working gate | **12** — the finding tickets |
| frozen acceptance tests | **120** (120 `@pytest.mark.ticket` markers, one per test), of which **79** sit on open tickets |
| open tickets with **no** frozen coverage at all | **27 of 51** |
| last measured acceptance | **43 / 120 passing** (35.83%, cycle 9, `6f78432`) |

## The graph has twenty roots, not one — read every "wave 1" claim against this

**This is a real structural property of `tickets.json`, not an error and not something to
hide.** Twenty tickets have an empty `depends_on`:

> T-000, T-135, T-136, T-137, T-138, T-139, T-140, T-141, T-142, T-143, T-144, T-145, T-146,
> T-147, T-148, T-149, T-150, T-151, T-152, T-153

Every piece of prose in this run's docs that speaks of "wave 1" assumes **exactly one scaffold
root, T-000, that everything else descends from**. That was true of the original 82-ticket
graph. It has not been true since the finding tickets were minted: T-135 – T-153 were created
from verifier sweeps against branches that had *already landed*, so they descend from nothing.
Three consequences, all of which bite a scheduler:

1. **Depth is not seniority.** The depth-1 set — T-010, T-011, T-012, T-013, T-014, T-109,
   T-111 — is the structural frontier under T-000 and is *not* the dispatch frontier. Five of
   its seven members are closed.
2. **The finding roots are READY on day one and stay READY forever.** They are unblocked by
   construction, not by progress, so their presence in the ready frontier carries no
   information about whether the work beneath them is done.
3. **Their `unblocks` count is 0 by construction.** They are leaf roots: nothing depends on
   them. Ranking the frontier by unblock count therefore sorts every finding ticket to the
   bottom, which is exactly backwards — several of them assert that a headline capability of a
   *closed* ticket is unwired in production. Read the frontier table with this in hand.

The graph is still acyclic and still has no dangling references. Twenty roots is a shape, not
a defect; the defect would be continuing to describe it as one.

## How closed-ness was determined

**Ground truth is git.** A ticket is closed when its work has landed on `main`. `tickets.json`
carries **no status field of any kind** — the 82 original tickets have `id, title, objective,
refs, acceptance, verify, scope, depends_on, non_goals, parallel_safe`, and the 19 finding
tickets have `id, title, verify, scope, depends_on, source, severity, location, defect,
finding_id, reproduction`. Neither shape has a status, so nothing in the graph *can*
contradict git.

Closure was reconstructed by reading all 101 ids and their scope globs out of `tickets.json`,
then running `git log --oneline main -- <scope path>` for every glob and `git log --grep=<id>`
across main's 255 commits, discarding mentions that were only ledger, mint or wave-closure
chores. **A scope directory whose sole commit is the T-000 skeleton (`2b408cd`) and which is
still a 0-byte `__init__.py` or `.gitkeep` on disk was treated as unbuilt** — that single test
decides 26 of the 51 open tickets, among them `apps/exchange/src/{accept,ranking,retrieval,
reports,policy}`, `packages/store-agent/src/{runtime,learning,modes,external}`,
`apps/trust/src/{reconcile,scoring,feedback,snapshot,verification}` and
`apps/buyer/svc/src/{intent,accept,feedback}`. For every finding ticket (T-135 – T-153) the
named file was additionally opened at HEAD and the described defect checked for presence,
rather than trusting any lane's claim.

### Closed — 50 tickets, with the commit that closed each

Per this run's rule, **a closed ticket leaves the ledger.** It survives here as evidence and
nowhere else; its objective, acceptance and verification live in `tickets.json`, in `reports/`
and in git history. This table exists so a reader can tell "absent because closed" from
"absent because the ledger drifted again" — the failure this regeneration repairs.

| id | title | evidence on `main` |
|---|---|---|
| T-000 | Monorepo skeleton with green empty verify pipeline | `2b408cd` feat(T-000) monorepo skeleton, plus `955432c`/`09fb42e`/`17c1527`/`429f29e`/`c37b417` fixes; merged `705cf34` |
| T-010 | Contracts generate typed models and enforce the dual-path bid boundary | `62c19ea` schema-driven models + dual-path bid boundary, `d521447` D52 envelope at the external door; merged `088e818`. `packages/contracts/` has 24 commits |
| T-011 | Postgres schemas enforce role isolation and hash-chained ledger | `8417803` schemas / role isolation / D16 hash chain, then `920d127`, `86b3cbf`, `199ce46`, `b3e993d`; merged `2f0d549`. `db/migrations/` and `apps/trust/src/ledger/` are real code |
| T-012 | Neo4j attribute-node catalog with vector retrieval through EmbeddingProvider | `4ea9a54` Neo4j attribute-node catalog, `4e642ee` EmbeddingProvider port, `09f01e3`/`289fb61`/`e2b2939` fixes; merged `fd67161` |
| T-013 | Shopify stub reproduces the exact surface the system uses | `1aefd92` the Shopify surface this system uses, `e320716` recorded fixtures + compose fragment; merged `e6fcaf5`. `services/shopify-stub/` has 32 commits |
| T-014 | LLM client with per-role config, cache-first prompts, and test doubles | `83ad53b` per-role model config, cache-first prompts, offline doubles; `5e85107`, `04840b7`, `9a1ea2f`; merged `43d8855` |
| T-020 | Signed fetch adapter ingests a storefront including password-protected dev stores | `dc2726c` signed-fetch CatalogAdapter with SSRF guard, robots and budgets; `261be9d`, `409017f`. `services/ingest/src/adapters/` is 8 real modules |
| T-021 | Policy pages and marketing claims land in the graph with provenance | `4ae86f0` policy-page claims land with provenance and quarantine; merged `68f753d` (cycle 9, `feat/w6-t021`). `services/ingest/src/extraction/` populated |
| T-030 | Auctions fan out, time out, and always represent every store | `04dfe61` auctions fan out, time out, represent every store; hardened `30b268b` and `63d1f2b`; merged `fb4dada`/`c43b014` |
| T-036 | Checkout reaches the merchant through a provider port | `b7c8bbc` checkout reaches the merchant through a provider port — `apps/exchange/src/checkout/{provider,providers,registry,codes,domain,lint,sellers}.py` all landed. **See the contested section: the port still has no production consumer, tracked as open … |
| T-040 | Tool hooks are the only way facts and discounts enter a bid | `bce9a35` tool hooks are the only door into a hosted bid; `5c80fcb`, `1b13214`, `c08e76e`, `9007243`, `886f571`. `packages/store-agent/src/hooks/` = tools.py + provenance.py + lint.py |
| T-050 | Installing the app wires pixel and webhooks against the stub | `1504bf5` install wires the web pixel and three order webhooks; `2381efa`, `6682f1f`, `c1689dd`, `9b8f409`; merged `f86dcc1`/`c43b014` |
| T-060 | Every event lands once, chained, and replays exactly | `e6d74dd` ledger writer service over the T-011 chain, `551e734` fixtures, `60adde1` 87 tests, `935b15f` hardening; merged `09ed1a3`/`c43b014` |
| T-070 | Buyers authenticate lightly and stores never see who they are | `bc12167` magic-link login, rotating pseudonyms, identity-free profiles; `e016a31` hardening (vault wired, link table bounded); merged `2053197`/`c43b014` |
| T-080 | The approved manifest and seed generator define ground truth | `cd81f36` ground-truth manifest / golden set / seed generator; `16c62a5` approval gate; `1537bcc` content-graded eval gates; human approval recorded at `8a2841f` |
| T-100 | Shopify stub never emits an off-domain checkout Location | `a1569ed` the serving path can no longer emit an off-domain Location |
| T-101 | A partial re-embed is never certified as complete | `6b905ff` stop certifying a partial re-embed as complete |
| T-102 | The chain_head guard trigger's DELETE arm is graded by a test | `ab4e87f` grade the chain_head guard's DELETE arm |
| T-103 | The TypeScript signer refuses integers the wire cannot state | `7e60e8a` refuse wire integers the TypeScript signer cannot state |
| T-104 | The prompt cache key cannot collide on separator text | `78f2538` key a call on its system block texts, not a joined string |
| T-105 | Money arithmetic is asserted absolutely, not against itself | `0d3770f` money and webhook headers asserted absolutely, not against themselves (wave-2 lane, `970a0ab`..`6791206`) |
| T-106 | A conformance gate keeps the two JCS canonicalizers from drifting | `a5aab18` gate the three RFC-8785 canonicalizers against one another; merged `756aeed` then `399851a`. `e2e/test_jcs_conformance.py` is 67 KB on HEAD |
| T-107 | claim_id is computed with JCS, not json.dumps | `a151899` derive claim_id from the shared JCS canonicalizer |
| T-108 | The two envelope gates agree on whitespace | `f68ebf7` make both envelope gates agree on whitespace |
| T-110 | Role passwords have one source of truth | `bf2c113` give the role password one source of truth; `1b3f08c` tests it on a real fresh volume |
| T-112 | The role password has one source of truth on the project's own fresh volume | `d53d03d` role password has one source of truth on the project's own volume; `5abdff0` acceptance 4 (no literal left in `.env.example`) |
| T-113 | The TypeScript signing path refuses unsafe integers by default, not opt-in | `a8e1bb6` the TS signing doors refuse an unsafe integer by default |
| T-114 | The chain_head trigger test does not block the ENABLE ALWAYS hardening | `f690142` install every ledger integrity trigger ENABLE ALWAYS; `7f7e5de` drives the anchor guard from more than one connection identity |
| T-115 | The envelope blank rule is engine-independent again | `165146b` the envelope blank rule is engine-independent again |
| T-116 | A single degenerate product cannot black out vector search | `02129c0` degrade the product, not the catalog, on an unembeddable row |
| T-118 | Wave-2 residue: eight low-severity findings from the lane verifiers | Eight items, each with a landed commit: `c358b24` (a), `6c7129e` (b), `9e2da05` (c), `c5878e4`+`f5af599` (d), `0439d5d` (e), `e38321b` (f), `4647a91` (g), `1311479` (h), plus `b16891c`/`1224885`/`f23ed0c`/`effe6b0`/`dfa2205` adversarial follow-ups. **See … |
| T-119 | The ledger canonicaliser is one module object under both import spellings | `9814d47` bind both import spellings of the ledger to one module object |
| T-120 | Using PROXYSHOP_ROLE_PASSWORD does not turn the repo gate red | `223e20b` scope the default-DSN assertion to the unset case; merged `262d3c8` |
| T-121 | The JCS conformance suite's prose matches the code T-119 changed | `108fa5f` the JCS suite's prose now matches the code T-119 changed; merged `262d3c8` |
| T-122 | Subprocess tests hand the child .pkgroot instead of clobbering PYTHONPATH | `0215bf3` subprocess tests hand the child `.pkgroot`, not a clobbered PYTHONPATH; merged `262d3c8` |
| T-124 | Fresh-volume tests remove the containers and volumes they create | `b2abca6` (`apps/trust/tests/test_schema_grants.py:1579`) and `0ab1187` (`proxyshop_support/tests/test_role_password_end_to_end.py:468`); both throwaway-container fixtures call `_docker('rm','-f','-v', …)` on HEAD. **See contested — the previous ledger … |
| T-125 | The signing door and the canonicalizer agree, and the gate watches both | `bb64f5f` the TS signing door signs every exact double, and the gate watches it |
| T-126 | The ledger spelling binding survives a concurrent first import | `4b480d8` the ledger spelling binding survives a concurrent first import |
| T-127 | The chain_head DELETE arm is graded by property, not by enumeration | `ad08c01` DELETE arm graded by property, not enumeration; `8007f4d` corrects the property's BEGIN split |
| T-128 | Guards that cannot refuse anything are removed, not tested tautologically | `6bd326e` delete the covered-field guard that could refuse nothing |
| T-129 | Wave-3 verification residue: nine findings across four lanes | Nine findings across four lanes, each with a commit: `d3acd6b`, `087aaa4`, `1a110e6`, `ecd3e4b`, `0171a09`, `7737632` (stub); `2778e0e`, `10542a4`, `3e67449`, `30718a3`, `22aeb21`, `f47611c` (ingest); `fb92393` (contracts) |
| T-131 | The golden answer key is graded weakly, and the dishonest store's flagship lie is … | `1537bcc` grade the eval gates by content, not by the string in `gates` — GATE_CONTENT_CHECKS plus `gp-012-misrepresented-ingredients`, the manifest's flagship scripted lie the answer key never graded (`fixtures/golden/golden_set.json` +137 lines, … |
| T-134 | Merchant residue: a scope guard that shreds strings, a token in repr, and six … | `7f75e48` (acceptance 1+2: `assert_scopes_allowed` raises TypeError on a bare str at `scopes.py:106-128`; `access_token: str = field(repr=False)` at `tokens.py:55`) and `c1689dd`/`9b8f409` (acceptance 3: `webhooks.py:516` `except (ValueError, … |
| T-135 | The exclusivity property is defeated by moving the payload one level down into the … | `c08e76e` restore R8 exclusivity — the offer is inside the bid boundary; then `9007243`, `976014e`, `886f571`. `provenance.py:118` `CLAIM_BEARING_FIELDS = ('claims','commitments')`; the Bid/Offer is now walked whole by `collect_claim_material` |
| T-138 | build_buckets is a pure, deterministic function of the account with no k-anonymity … | `958fead` the configurable k-anonymity floor SPEC promises, default 1 — `K_ANONYMITY_ENV`/`DEFAULT_K_ANONYMITY` (`profile/__init__.py:169,175`), `k_anonymity_floor()` `:666`, `anonymise_cohort`/`build_profiles` and the `buckets_at_level` generalisation … |
| T-143 | A replayer can relabel a signed delivery onto a topic of its choice AND destroy … | `c1689dd` item 1 — the `X-Shopify-Topic` header is now the only source of a topic, a disagreeing path is 400, and WebhookInbox binds a digest to its first `bound_topic`; plus `9b8f409` follow-ups. `webhooks.py:23-24,78,448-482` |
| T-144 | merchant-svc has no deployable. The compose fragment this file's own header … | `d742cae` feat(deploy) — `apps/merchant/Dockerfile` added and `apps/merchant/compose.yaml` now defines a `merchant-svc` service with build/ports/healthcheck (was `services: {}`); same commit adds buyer/exchange/trust Dockerfiles and `docs/deploy.md` |
| T-145 | R10's "hard timeout" is measured from the moment solicit_bids constructs its … | `63d1f2b` item 2 — `started_at` anchors ArrivalClock to the monotonic reading taken when the deadline was struck, and `routes.py` passes it; regression tests in `apps/exchange/tests/test_w6_hardening.py` |
| T-146 | The hard timeout is bought by abandoning the ThreadPoolExecutor … | `63d1f2b` item 1 — the per-request ThreadPoolExecutor is replaced by a process-wide `BoundedFanOutPool` with a hard worker ceiling and non-blocking admission; both strategies borrow from it |
| T-149 | A connection failure never becomes a StoreUnavailable, so an unreachable or … | `935b15f` item 1 — connection acquisition moved inside the guarded region and `classify_connection_error` maps OperationalError/InterfaceError/OSError to StoreUnavailable; pool timeout derived from `connect_timeout` (30s+500 becomes ~2s+503) |

## Where the run's own prose contradicts the code on `main`

The graph has no status field to be wrong, but wave-closure commit messages and cycle reports
repeatedly claim closure the tree does not support — and, in one case, the *previous ledger*
claimed an open ticket that git says was fixed. **Git was trusted over the prose in every
case.** Each entry below states the claim, then what git says.

### T-109 — CONFIRMED OPEN

- **The claim:** `6791206`'s subject asserts wave 2 "fixed and verified" 11 defect tickets; its body names and evidences only nine (T-100 – T-105, T-107, T-108, T-110) out of the wave-2 set T-100 – T-111, so T-109 reads as closed in the wave prose.
- **Git says:** `git log main -- proxyshop_support/reachability.py` returns exactly one commit ever — `2b408cd` (T-000) — and `conftest.py`'s newest commit is `c37b417`, also T-000 era; both are T-109's whole scope. The defect is intact at HEAD: `reachability.py:64-70` `skip_reason()` calls `unreachable()` and returns one combined reason whenever **any** of the three endpoints is down, and `conftest.py` applies that single reason to every `@pytest.mark.docker` test. Acceptance 1 ("reachability is per-service") is unmet.

### T-111 — CONFIRMED OPEN

- **The claim:** `8311b3c` ("wave 3 complete — 10 tickets closed, build_succeeds restored") narrates the `.pkgroot`/site-packages namespace failure as diagnosed and repaired, and later prose treats the bootstrap-provisioning half as covered by that repair.
- **Git says:** `git log main -- scripts/bootstrap.sh` returns exactly two commits, `09fb42e` and `2b408cd`, both from the T-000 era; nothing has touched the file since, and `scripts/bootstrap.sh` is T-111's entire scope. Its acceptance 1–2 (fail loudly when the flat namespaces are dead) cannot have landed.

### T-123 — CONFIRMED OPEN

- **The claim:** `8311b3c` states as root cause #2 that the venv's site-packages **directory** carried macOS `UF_HIDDEN`, that a recursive clear survives a full pytest run, and that this was proven by a bare `python -c "import contracts, llm, trust"` after the suite — inside a commit whose subject says 10 tickets closed.
- **Git says:** The repair was made **in the environment by hand, not in the repo.** T-123's scope is `scripts/bootstrap.sh` + `conftest.py` + `proxyshop_support/**`; `bootstrap.sh` has no commit after `09fb42e`/`2b408cd` and `conftest.py` none after `c37b417`. The `proxyshop_support/` commits that do exist (`d53d03d`, `5abdff0`, `0215bf3`, `223e20b`, `0ab1187`) all belong to T-112/T-120/T-122/T-124. The `chflags` step in the script still runs against a single `.pth` path, so the flag returns on the next fresh venv.

### T-124 — REFUTED — the ticket is CLOSED and the previous ledger was stale by one commit

- **The claim:** The previous `backlog.md`, under this very heading, said "T-124 is open, and half-fixed in a way that reads as closed", naming `proxyshop_support/tests/test_role_password_end_to_end.py:459` as still running `docker rm -f` without `-v`.
- **Git says:** `0ab1187` — the very commit that regenerated that backlog — is also the commit that fixed the leak. At HEAD `proxyshop_support/tests/test_role_password_end_to_end.py:468` reads `_docker("rm", "-f", "-v", name, timeout=120)`, matching `apps/trust/tests/test_schema_grants.py:1579` already fixed in `b2abca6`. Both throwaway-container fixtures now remove their anonymous volumes. **T-124 is counted closed here.**

### T-118 — CLOSED on landed work, item (a) only partial

- **The claim:** `8311b3c` counts T-118 among "wave 3 … 10 tickets closed", while the **same commit body** says "T-118 item (a) is UNCLOSABLE without a frozen-test amendment — no str-valued injective rendering can exist".
- **Git says:** All eight items have commits on main (`c358b24` a, `6c7129e` b, `9e2da05` c, `c5878e4`+`f5af599` d, `0439d5d` e, `e38321b` f, `4647a91` g, `1311479` h). But `c358b24` (02:20, seven hours before `8311b3c`) changed only `packages/llm/src/{doubles.py,prompting.py}` — the recording surface — so the non-injective `str` rendering the wave commit calls unclosable is **still on main**. The remaining seven items did land, so the ticket is counted closed with that caveat recorded.

### T-137 — Carried OPEN — adjudicated in prose, unchanged in git

- **The claim:** `6f78432` and `.swarm-loop/reports/cycle-9.md` state that `refresh_digests` does **not** defeat the approval tamper-evidence, because the two-document digest chain turns the frozen test red at `test_e8_proofs.py:355`, so the finding was REFUTED and the lane discarded its work before committing.
- **Git says:** **No behavioural change landed.** `1537bcc` is the only post-mint commit to `fixtures/manifest/__init__.py` and it added a ~20-line docstring to `refresh_digests` explaining the refutation. The code at lines 863-877 still recomputes `body_digest` and re-pins `approval.content_hash` while leaving `approver` / `approved_at` / `artifact` populated, exactly as the ticket describes. A ticket that is adjudicated-not-a-defect in prose but has no fix in git **cannot be evidenced as closed**, so it stays in this ledger. Closing it requires either a commit or an explicit ruling recorded against the graph.

### T-138 — CLOSED — and the "only the knob" framing understates what landed

- **The claim:** `6f78432` says the buyer k-anonymity brief was REFUTED (SPEC.md:56 non-goal, k defaults to 1) and that "the lane discarded a full implementation before committing and shipped only the missing knob, default byte-identical".
- **Git says:** `958fead` added to `apps/buyer/svc/src/profile/__init__.py` not just `K_ANONYMITY_ENV`/`DEFAULT_K_ANONYMITY` (`:169`, `:175`) and `k_anonymity_floor()` (`:666`), but `anonymise_cohort()` (`:799`), `build_profiles()` (`:1089`) and a 7-rung `buckets_at_level` generalisation ladder with a closed merchandising taxonomy (`:210`, `:387`). The ticket's literal claim — "no k-anonymity floor, suppression or generalisation of any kind … no such code exists anywhere in the branch or the current tree" — is **false on HEAD**.

### T-036 / T-147 — BOTH partly right — T-036 CLOSED, T-147 OPEN

- **The claim:** `c43b014` ("merge(cycle8): seven F1 feature branches") and the derived backlog count T-036 among the cycle-8 tickets closed; T-147, minted later at `9710f3e`, says "T-036 — the whole second half of this lane — is unwired … the frozen suite carries no `@pytest.mark.ticket(\"T-036\")` test at all".
- **Git says:** `b7c8bbc` did land seven real modules under `apps/exchange/src/checkout/` (provider, providers, registry, codes, domain, lint, sellers), which is T-036's own scope — so T-036 is closed. But at HEAD `apps/exchange/src/accept/__init__.py` is still 0 bytes, and a repo-wide search finds no `CheckoutRequest(`, `resolve_provider` or registry call anywhere outside `apps/exchange/src/checkout/`. The port has no production consumer. That is T-147, and it is open.

Nothing else in the graph disagrees with git.

## The ready frontier — dispatch from here

**27 of the 51 open tickets have every dependency closed.** This is the dispatchable set at
`1b08077`; it is computed, not curated — an open ticket is READY iff every id in its
`depends_on` is in the closed table above. Twelve of the twenty-seven are READY only because
they are *roots*: they were minted with no dependencies at all.

*"Unblocks" counts the ticket's transitive open descendants — how many other open tickets stop
being blocked, eventually, because this one lands. It is one ordering signal, not the whole
answer: the scheduling constraints below veto co-scheduling that the ranking would allow, and
the finding roots score 0 by construction rather than by unimportance.*

| ticket | unblocks | frozen tests | parallel_safe | title |
|---|---|---|---|---|
| **T-041** | 13 | 2 | no | Advocate runtime bids within walls and defaults deterministically cold |
| **T-065** | 13 | 6 | no | A golden pitch yields all four verification statuses with evidence |
| **T-033** | 12 | 5 | no | Accepting an offer produces a validated code and permalink |
| **T-031** | 9 | 0 | yes | Candidate retrieval and fit scoring feed the ranker |
| **T-051** | 7 | 2 | no | Pixel reports checkout outcomes the collector can join |
| **T-071** | 5 | 3 | no | Three questions or fewer produce a confirmed structured intent |
| **T-053** | 3 | 3 | yes | A plain-language interview yields an approved, versioned envelope |
| **T-023** | 1 | 1 | yes | Catalog MCP adapter passes recorded-contract tests |
| **T-052** | 1 | 3 | no | Winning offers become single-use validated codes and permalinks |
| **T-109** | 1 | 0 | no | A datastore blip cannot silently empty the security gate |
| **T-111** | 1 | 0 | no | Provisioning fails loudly when the flat namespaces are dead |
| **T-022** | 0 | 1 | yes | Same products across stores link via entity resolution |
| **T-130** | 0 | 0 | no | Sub-HIGH findings from the feature-wave checkers (backlog sweep, not a wave) |
| **T-132** | 0 | 0 | no | Rotating pseudonyms are trivially re-linkable, so T-070's central guarantee does not hold |
| **T-133** | 0 | 0 | yes | Buyer residue: shared session state, unwired publish half, and unbounded stores |
| **T-136** | 0 | 0 | — | The store-agent hook surface has no production caller |
| **T-137** | 0 | 0 | — | refresh_digests re-pins an approval onto bytes the approver never saw |
| **T-139** | 0 | 0 | — | The one uncapped profile bucket can publish the buyer's name and address verbatim |
| **T-140** | 0 | 0 | — | AccountDirectory has no production populator, so every buyer profile is empty |
| **T-141** | 0 | 0 | — | The magic link is delivered to a no-op, so login cannot complete in production |
| **T-142** | 0 | 0 | — | publish_profile is dead code, so no store ever receives a BuyerProfile |
| **T-147** | 0 | 0 | — | T-036's checkout port has no production consumer and no frozen marker |
| **T-148** | 0 | 0 | — | configure_auctions has no caller, so the shipped exchange denies every store |
| **T-150** | 0 | 0 | — | The ledger writer has zero producers |
| **T-151** | 0 | 0 | — | Ledger writes silently use the wrong DB role |
| **T-152** | 0 | 0 | — | The hardened R8 boundary is not called by anything |
| **T-153** | 0 | 0 | — | A bid's stated price is never reconciled against its granted discount |

**Read the frontier with five constraints in hand, in this order:**

1. **The twelve open finding tickets have no working gate.** T-136, T-137, T-139 – T-142,
   T-147, T-148, T-150, T-151, T-152, T-153 each carry
   `verify: false  # NO GATE YET — write one that fails on this finding first`, and none has a
   frozen test. **A ticket whose verify is `false` cannot be closed by running it** — the first
   work in each lane is writing the gate that goes red, then making it green. Their unblock
   count of 0 ranks them last in the table and is exactly the wrong way to read them.
2. **The graph writers serialize against each other.** T-022, T-023, T-024 and T-031 all write
   the one shared Neo4j database. At most one may be in flight, whatever their unblock counts
   say. T-031 (unblocks 9) is the most valuable member of that queue; T-021, previously the
   head of it, has closed.
3. **T-041, T-065 and T-033 are the three highest-value lanes and they are mutually safe.**
   T-041 (`packages/store-agent/src/runtime/**` + its tests, unblocks 13), T-065
   (`packages/verification/**` + `apps/trust/src/verification/**` + its tests, unblocks 13) and
   T-033 (`apps/exchange/src/{accept,orchestration}/**` + its tests, unblocks 12) share no
   scope with each other or with any graph lane, and all three sit directly under closed
   parents. **T-065 is the single biggest unlock in the trust epic**: it is the only thing
   standing between the board and T-062, which alone unblocks 10. One caveat on T-041: its
   `src/runtime/**` is exactly T-152's scope, so those two are one lane, not two.
4. **The infra-debt tickets overlap and must go one at a time.** Two are READY — T-109 and
   T-111 — with T-123 and T-117 directly behind them, and all four defects are live in the
   tree right now (see the contested section). They share `proxyshop_support/**` and
   `conftest.py`, and containment confirms it: T-123 encloses both READY tickets. This run has
   already spent five `build_succeeds` zero-readings on exactly these.
5. **T-130, T-132 and T-133 are scope supersets, not siblings.** T-130's scope contains 27
   other open tickets, T-133 contains 14, T-132 contains 6. They cannot ride alongside the
   lanes they enclose. Of the three, **T-132 carries the HIGH**: it voids T-070's central
   privacy guarantee, and its frozen-contract amendment — which the user approved in principle
   — had its design v1 refuted at `bda0ce0` and recorded rather than applied. T-139 and T-142
   are the same defect class arriving one layer down, in the same file.

---

# Open tickets — 51

Grouped by epic, then by the two defect cohorts. Every entry is generated from `tickets.json`:
`depends_on` is verbatim from the graph, annotated ✅ closed / ⛔ open. **READY** means every
dependency is closed. Objectives are trimmed to one line; the graph holds the full text, the
acceptance criteria, the refs and the non-goals.

### E2 — Ingestion

*3 open · 2 ready · 1 blocked*

#### T-022 — Same products across stores link via entity resolution

GTIN exact match plus embedding+attribute matcher producing SAME_AS edges with confidence; threshold config.

- **State:** **READY**
- **depends_on:** T-012 ✅, T-020 ✅
- **Unblocks (open):** 0
- **Acceptance:** 2 criteria · **Frozen:** **1 frozen tests**
- **verify:** `pytest services/ingest/tests/test_entity_resolution.py -q`
- **Scope:** `services/ingest/src/er/**`, `services/ingest/tests/**`, `fixtures/er/**` · `parallel_safe: true`

#### T-023 — Catalog MCP adapter passes recorded-contract tests

catalog_mcp CatalogAdapter implementing the production-primary read path against recorded mocks (dev stores are not in Global Catalog — A1).

- **State:** **READY**
- **depends_on:** T-020 ✅
- **Unblocks (open):** 1 — T-024
- **Acceptance:** 2 criteria · **Frozen:** **1 frozen tests**
- **verify:** `pytest services/ingest/tests/test_catalog_mcp.py -q`
- **Scope:** `services/ingest/src/adapters/**`, `services/ingest/tests/**`, `fixtures/mcp/**` · `parallel_safe: true`

#### T-024 — Differential refresh keeps the graph current at field-appropriate cadence

Scheduler with per-field cadence config; only changed content re-extracts; refresh endpoint.

- **State:** **BLOCKED** by T-023
- **depends_on:** T-021 ✅, T-023 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest services/ingest/tests/test_refresh.py -q`
- **Scope:** `services/ingest/src/scheduler/**`, `services/ingest/tests/**` · `parallel_safe: false`

### E3 — Exchange

*5 open · 2 ready · 3 blocked*

#### T-031 — Candidate retrieval and fit scoring feed the ranker

Vector+attribute retrieval from Neo4j, rerank behind an interface (deterministic double in tests), fit score per candidate logged per bid.

- **State:** **READY**
- **depends_on:** T-012 ✅, T-030 ✅
- **Unblocks (open):** 9 — T-032, T-034, T-035, T-054, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest apps/exchange/tests/test_retrieval.py -q`
- **Scope:** `apps/exchange/src/retrieval/**`, `apps/exchange/tests/**` · `parallel_safe: true`

#### T-032 — Shortlists rank by the single published formula behind eligibility filters

Eligibility filters (blacklist fail-closed, offer expiry, checkout-domain validity, hard constraints requiring verified facts per R19), then published rank_score per DESIGN §Decisions with [0,1]-normalized features, policy penalties, stable tie-breaking, …

- **State:** **BLOCKED** by T-031, T-033, T-065
- **depends_on:** T-031 ⛔, T-033 ⛔, T-065 ⛔
- **Unblocks (open):** 8 — T-034, T-035, T-054, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 4 criteria · **Frozen:** **9 frozen tests**
- **verify:** `pytest apps/exchange/tests/test_ranking.py -q`
- **Scope:** `apps/exchange/src/ranking/**`, `apps/exchange/tests/**` · `parallel_safe: false`

#### T-033 — Accepting an offer produces a validated code and permalink

Accept endpoint resolves a CheckoutProvider (T-036) by CHECKOUT_MODE and delegates code creation to it, returns the permalink the provider mints, records the accepted event, and handles code-creation failure with re-offer of the next slot. The starting slice …

- **State:** **READY**
- **depends_on:** T-030 ✅, T-036 ✅
- **Unblocks (open):** 12 — T-032, T-034, T-035, T-054, T-072, T-073, T-081, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 5 criteria · **Frozen:** **5 frozen tests**
- **verify:** `pytest apps/exchange/tests/test_accept.py -q`
- **Scope:** `apps/exchange/src/accept/**`, `apps/exchange/src/orchestration/**`, `apps/exchange/tests/**` · `parallel_safe: false`

#### T-034 — Exchange bandit shifts exposure with outcomes and honors exploration

Contextual Thompson sampling over cluster×store adjusting exposure/exploration (not formula weights); guaranteed exploration slice for low-data stores from trust snapshot.

- **State:** **BLOCKED** by T-032, T-064
- **depends_on:** T-032 ⛔, T-064 ⛔
- **Unblocks (open):** 3 — T-083, T-084, T-087
- **Acceptance:** 3 criteria · **Frozen:** **3 frozen tests**
- **verify:** `pytest apps/exchange/tests/test_bandit.py -q`
- **Scope:** `apps/exchange/src/policy/**`, `apps/exchange/tests/**` · `parallel_safe: false`

#### T-035 — Loss reports aggregate reasons without leaking amounts

Windowed job producing LossReport per store: reason categories from the ranking formula's dominant term, unmet criteria for fit losses, delayed release.

- **State:** **BLOCKED** by T-032
- **depends_on:** T-032 ⛔
- **Unblocks (open):** 1 — T-054
- **Acceptance:** 3 criteria · **Frozen:** **1 frozen tests**
- **verify:** `pytest apps/exchange/tests/test_loss_reports.py -q`
- **Scope:** `apps/exchange/src/reports/**`, `apps/exchange/tests/**` · `parallel_safe: true`

### E4 — Store agent

*5 open · 1 ready · 4 blocked*

#### T-041 — Advocate runtime bids within walls and defaults deterministically cold

BidRequest→Bid|decline: cache-layout context assembly, hook-only claim emission, deterministic cold-start bid (list price + standing commitments + intro rule only). CONSTRAINT INHERITED FROM T-040 (recorded 2026-09-02, do not drop). `ToolHooks.start_bid()` …

- **State:** **READY**
- **depends_on:** T-014 ✅, T-040 ✅
- **Unblocks (open):** 13 — T-042, T-043, T-044, T-045, T-054, T-063, T-073, T-082, T-083, T-084, T-085, T-086, T-087
- **Acceptance:** 3 criteria · **Frozen:** **2 frozen tests**
- **verify:** `pytest packages/store-agent/tests/test_runtime.py -q`
- **Scope:** `packages/store-agent/src/runtime/**`, `packages/store-agent/tests/**` · `parallel_safe: false`

#### T-042 — Store loop learns from its own outcomes only

Per-store Thompson sampling over discount-depth buckets × commitment sets per cluster; network-prior intake; prior builder consumes pitch/value-prop outcomes only.

- **State:** **BLOCKED** by T-041
- **depends_on:** T-041 ⛔
- **Unblocks (open):** 3 — T-083, T-084, T-087
- **Acceptance:** 3 criteria · **Frozen:** **3 frozen tests**
- **verify:** `pytest packages/store-agent/tests/test_learning.py -q`
- **Scope:** `packages/store-agent/src/learning/**`, `packages/store-agent/tests/**` · `parallel_safe: true`

#### T-043 — Shadow mode logs would-be bids and trust events adjust the agent

Shadow activation gates submission but logs full bids+rationale to sealed store state; TrustEventPayload intake adjusts policy/commitment posture and is visible in rationale.

- **State:** **BLOCKED** by T-041
- **depends_on:** T-041 ⛔
- **Unblocks (open):** 4 — T-054, T-063, T-073, T-086
- **Acceptance:** 3 criteria · **Frozen:** **3 frozen tests**
- **verify:** `pytest packages/store-agent/tests/test_shadow_trust.py -q`
- **Scope:** `packages/store-agent/src/modes/**`, `packages/store-agent/tests/**` · `parallel_safe: true`

#### T-044 — External bids enter signed with a required envelope and route to verification, not rejection

Signature registration + verification on the external submission door POST /v1/auctions/{auction_id}/bids for non-hosted agents; schema validation admits seller_asserted claims and free-text message, marks the bid unverified, and enqueues …

- **State:** **BLOCKED** by T-041
- **depends_on:** T-010 ✅, T-011 ✅, T-041 ⛔
- **Unblocks (open):** 1 — T-045
- **Acceptance:** 7 criteria · **Frozen:** **8 frozen tests**
- **verify:** `pytest packages/store-agent/tests/test_external_bids.py -q`
- **Scope:** `packages/store-agent/src/external/**`, `packages/store-agent/tests/**` · `parallel_safe: true`

#### T-045 — Three seller personas exercise the external door adversarially

apps/seller-reference: value / specialist / aggressive personas with identity, catalog scope, tone, and offer policy; free-text pitches with asserted claims via the external /bid path; the aggressive persona emits manifest-scripted false and unsupported …

- **State:** **BLOCKED** by T-044
- **depends_on:** T-014 ✅, T-044 ⛔, T-080 ✅
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **1 frozen tests**
- **verify:** `pytest apps/seller-reference -q`
- **Scope:** `apps/seller-reference/**` · `parallel_safe: true`

### E5 — Merchant

*4 open · 3 ready · 1 blocked*

#### T-051 — Pixel reports checkout outcomes the collector can join

Web pixel extension subscribing standard events; POST via fetch keepalive to collector with clientId, checkout token, order id, discountApplications; collector persists to app schema and forwards LedgerEvents.

- **State:** **READY**
- **depends_on:** T-050 ✅
- **Unblocks (open):** 7 — T-061, T-081, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** **2 frozen tests**
- **verify:** `npx vitest run pixel && pytest apps/merchant/svc/tests/test_collector.py -q`
- **Scope:** `pixel/**`, `apps/merchant/svc/src/collector/**`, `apps/merchant/svc/tests/**` · `parallel_safe: false`

#### T-052 — Winning offers become single-use validated codes and permalinks

The Shopify adapter behind T-036's CheckoutProvider port — one implementation of it, not the only route to a code. /codes: discountCodeBasicCreate usageLimit:1 + expiry, validity + combinesWith pre-check, unique code per accepted offer, permalink …

- **State:** **READY**
- **depends_on:** T-036 ✅, T-050 ✅
- **Unblocks (open):** 1 — T-054
- **Acceptance:** 4 criteria · **Frozen:** **3 frozen tests**
- **verify:** `pytest apps/merchant/svc/tests/test_codes.py -q`
- **Scope:** `apps/merchant/svc/src/codes/**`, `apps/merchant/svc/tests/**` · `parallel_safe: false`

#### T-053 — A plain-language interview yields an approved, versioned envelope

Onboarding interview service: LLM interview (double in tests, transcript fixture), envelope draft in plain English, merchant written approval gate, versioned persistence to sealed schema; shadow default.

- **State:** **READY**
- **depends_on:** T-014 ✅, T-050 ✅
- **Unblocks (open):** 3 — T-054, T-086, T-087
- **Acceptance:** 3 criteria · **Frozen:** **3 frozen tests**
- **verify:** `pytest apps/merchant/svc/tests/test_onboarding.py -q`
- **Scope:** `apps/merchant/svc/src/onboarding/**`, `apps/merchant/svc/tests/**`, `fixtures/interviews/**`, `apps/merchant/svc/src/envelope/**` · `parallel_safe: true`

#### T-054 — The dashboard shows the walls and the window

Merchant dashboard: bid log with rationale, loss reports, trust breakdown + event payloads, envelope editor (versioned), kill switch wired to agent mode. Includes verification outcomes per bid (statuses + evidence links).

- **State:** **BLOCKED** by T-035, T-043, T-052, T-053, T-063
- **depends_on:** T-035 ⛔, T-043 ⛔, T-052 ⛔, T-053 ⛔, T-063 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `npx vitest run apps/merchant/app/dashboard`
- **Scope:** `apps/merchant/app/dashboard/**` · `parallel_safe: false`

### E6 — Trust

*5 open · 1 ready · 4 blocked*

#### T-061 — Webhook truth reconciles lossy pixel signals

Reconciliation: match checkout_pixel↔order_paid by join keys; emit reconciled events; derive price_honored/discount_honored comparisons from webhook data only.

- **State:** **BLOCKED** by T-051
- **depends_on:** T-051 ⛔, T-060 ✅
- **Unblocks (open):** 5 — T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** **3 frozen tests**
- **verify:** `pytest apps/trust/tests/test_reconciliation.py -q`
- **Scope:** `apps/trust/src/reconcile/**`, `apps/trust/tests/**` · `parallel_safe: false`

#### T-062 — Trust merges verification and outcome observations against the approved manifest

Merged observation framework over SIX dimensions (price_honored, discount_honored, shipped_on_time, not_returned, feedback_match, catalog_claim_accuracy) inside ONE trust system (D53): per-dimension Beta with observation-type weights (contradicted 2.0, …

- **State:** **BLOCKED** by T-065
- **depends_on:** T-060 ✅, T-065 ⛔, T-080 ✅
- **Unblocks (open):** 10 — T-034, T-054, T-063, T-064, T-073, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 6 criteria · **Frozen:** **11 frozen tests**
- **verify:** `pytest apps/trust/tests/test_scoring.py -q`
- **Scope:** `apps/trust/src/scoring/**`, `apps/trust/tests/**` · `parallel_safe: false`

#### T-063 — Trust events reach the store agent and buyers close the loop

TrustEventPayload push to store-agent intake; feedback flow: routed-buyer-only prompt, buyer-track-record weighting, cross-check against return behavior.

- **State:** **BLOCKED** by T-043, T-062
- **depends_on:** T-043 ⛔, T-062 ⛔
- **Unblocks (open):** 2 — T-054, T-073
- **Acceptance:** 3 criteria · **Frozen:** **3 frozen tests**
- **verify:** `pytest apps/trust/tests/test_feedback_push.py -q`
- **Scope:** `apps/trust/src/feedback/**`, `apps/trust/tests/**` · `parallel_safe: false`

#### T-064 — Exchange consumes one snapshot shape for trust and exploration

TrustSnapshot API incl. blacklist + low-data flags for the exploration slice; versioned; exchange client. The served shape carries all six dimensions incl. catalog_claim_accuracy (D53) and is the only trust shape the exchange consumes.

- **State:** **BLOCKED** by T-062
- **depends_on:** T-062 ⛔
- **Unblocks (open):** 4 — T-034, T-083, T-084, T-087
- **Acceptance:** 3 criteria · **Frozen:** **1 frozen tests**
- **verify:** `pytest apps/trust/tests/test_snapshot.py -q`
- **Scope:** `apps/trust/src/snapshot/**`, `apps/trust/tests/**` · `parallel_safe: true`

#### T-065 — A golden pitch yields all four verification statuses with evidence

packages/verification + trust-service wiring: atomic typed claim extraction with source spans (LLM behind strict schemas, doubles in tests), seller/SKU/variant resolution (ambiguous on failure), unit/type canonicalization, deterministic comparators per …

- **State:** **READY**
- **depends_on:** T-010 ✅, T-021 ✅, T-060 ✅, T-080 ✅
- **Unblocks (open):** 13 — T-032, T-034, T-035, T-054, T-062, T-063, T-064, T-073, T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 4 criteria · **Frozen:** **6 frozen tests**
- **verify:** `pytest packages/verification apps/trust/tests/test_verification.py -q`
- **Scope:** `packages/verification/**`, `apps/trust/src/verification/**`, `apps/trust/tests/**` · `parallel_safe: false`

### E7 — Buyer

*3 open · 1 ready · 2 blocked*

#### T-071 — Three questions or fewer produce a confirmed structured intent

Buyer agent: clarification loop capped at 3, Intent construction + buyer confirmation UI, profile bucket builder with k-floor config.

- **State:** **READY**
- **depends_on:** T-014 ✅, T-070 ✅
- **Unblocks (open):** 5 — T-072, T-073, T-082, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** **3 frozen tests**
- **verify:** `pytest apps/buyer/svc/tests/test_intent.py -q && npx vitest run apps/buyer/app/intent`
- **Scope:** `apps/buyer/svc/src/intent/**`, `apps/buyer/app/intent/**`, `apps/buyer/svc/tests/**`, `fixtures/dialogues/**` · `parallel_safe: false`

#### T-072 — Shortlists render with provenance and accept hands off cleanly

Shortlist UI: slots, trust indicator, provenance labels ('store-confirmed' vs 'from their website'); accept → exchange accept → redirect to permalink.

- **State:** **BLOCKED** by T-033, T-071
- **depends_on:** T-033 ⛔, T-071 ⛔
- **Unblocks (open):** 4 — T-073, T-082, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** **2 frozen tests**
- **verify:** `npx vitest run apps/buyer/app/shortlist && pytest apps/buyer/svc/tests/test_accept_flow.py -q`
- **Scope:** `apps/buyer/app/shortlist/**`, `apps/buyer/svc/src/accept/**`, `apps/buyer/svc/tests/**` · `parallel_safe: false`

#### T-073 — Routed buyers can answer one structured feedback prompt

Post-purchase feedback UI + intake wiring to trust feedback API; one prompt, structured options, no free text.

- **State:** **BLOCKED** by T-063, T-072
- **depends_on:** T-063 ⛔, T-072 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **2 frozen tests**
- **verify:** `npx vitest run apps/buyer/app/feedback && pytest apps/buyer/svc/tests/test_feedback.py -q`
- **Scope:** `apps/buyer/app/feedback/**`, `apps/buyer/svc/src/feedback/**`, `apps/buyer/svc/tests/**` · `parallel_safe: true`

### E8 — Proofs, fixtures and runbooks

*7 open · 0 ready · 7 blocked*

#### T-081 — Simulated buyers exercise the whole network from one seed

services/sim: seeded traffic generator driving intents→auctions→acceptances→stub purchases/returns; dishonest-store script executes manifest behaviors.

- **State:** **BLOCKED** by T-033, T-051
- **depends_on:** T-033 ⛔, T-051 ⛔, T-080 ✅
- **Unblocks (open):** 5 — T-082, T-083, T-084, T-085, T-087
- **Acceptance:** 3 criteria · **Frozen:** **1 frozen tests**
- **verify:** `pytest services/sim -q`
- **Scope:** `services/sim/**` · `parallel_safe: false`

#### T-082 — One scripted run proves the full S1 flow

E2E test: intent→clarify→bids→shortlist→accept→code→stub checkout→pixel+webhook→reconcile→ledger→trust update, all against compose.

- **State:** **BLOCKED** by T-061, T-072, T-081, T-032, T-041, T-062
- **depends_on:** T-061 ⛔, T-072 ⛔, T-081 ⛔, T-032 ⛔, T-041 ⛔, T-062 ⛔
- **Unblocks (open):** 2 — T-085, T-087
- **Acceptance:** 4 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest e2e/test_s1_flow.py -q`
- **Scope:** `e2e/**` · `parallel_safe: false`

#### T-083 — Both learning loops demonstrably move under seeded outcomes

Simulation assertions for S4: outcome shifts reorder shortlists in-cluster (exchange loop); a store's discount-depth distribution tracks its own record (store loop).

- **State:** **BLOCKED** by T-034, T-042, T-081, T-061
- **depends_on:** T-034 ⛔, T-042 ⛔, T-081 ⛔, T-061 ⛔
- **Unblocks (open):** 2 — T-084, T-087
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest e2e/test_learning.py -q`
- **Scope:** `e2e/**` · `parallel_safe: false`

#### T-084 — The dishonest store ends below threshold and off the shortlist

Episode test for S2: run manifest budget of simulated episodes; assert trust trajectory matches approved manifest expectations, ends blacklisted, and store disappears from subsequent shortlists.

- **State:** **BLOCKED** by T-062, T-083
- **depends_on:** T-062 ⛔, T-083 ⛔
- **Unblocks (open):** 1 — T-087
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest e2e/test_dishonest.py -q`
- **Scope:** `e2e/**` · `parallel_safe: false`

#### T-085 — The starting-slice demo is a runbook anyone on the team can execute

docs/demo/starting-slice.md: the offline starting-path demo procedure - compose bring-up against services/shopify-stub, `make demo-seed`, and the live-auction demo beat end to end through SimulatedRedirectProvider (intent -> pitches -> verification -> …

- **State:** **BLOCKED** by T-082
- **depends_on:** T-082 ⛔
- **Unblocks (open):** 1 — T-087
- **Acceptance:** 4 criteria · **Frozen:** **2 frozen tests**
- **verify:** `pytest docs/tests/test_runbook.py -q`
- **Scope:** `docs/demo/starting-slice.md`, `docs/tests/**` · `parallel_safe: true`

#### T-086 — Onboarding drives a shadow store to its first real bid

An integration test walks the whole S6 seam in one run: a mocked interview transcript produces an economic envelope, the merchant approves it in writing, the store agent runs in shadow mode logging would-be bids without submitting any, and activation turns …

- **State:** **BLOCKED** by T-043, T-053
- **depends_on:** T-043 ⛔, T-053 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 4 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest e2e/test_onboarding.py -q`
- **Scope:** `e2e/test_onboarding.py`, `e2e/support/onboarding/**` · `parallel_safe: false`

#### T-087 — The Shopify and onboarding extension runbook covers the beats off the starting path

docs/demo/shopify-onboarding-extension.md: the extension-lane demo procedure - dev-store provisioning (app install, storefront password, Bogus Gateway), the `make e2e-live` procedure against seeded dev stores, the interview -> bidding onboarding beat, and …

- **State:** **BLOCKED** by T-053, T-084, T-085
- **depends_on:** T-053 ⛔, T-084 ⛔, T-085 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 5 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest docs/tests/test_runbook.py -q`
- **Scope:** `docs/demo/shopify-onboarding-extension.md` · `parallel_safe: true`

### Infrastructure and cycle-8 residue tickets

*7 open · 5 ready · 2 blocked*

#### T-109 — A datastore blip cannot silently empty the security gate

conftest.py:105-121 and proxyshop_support/reachability.py:36-46,64-70: skip_reason() probes Postgres, Neo4j AND Redis, and if any one fails every @pytest.mark.docker test skips. That is 47 of T-011's 110 gate cases, including 100% of its acceptance criteria …

- **State:** **READY**
- **depends_on:** T-000 ✅
- **Unblocks (open):** 1 — T-117
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `pytest apps/trust -q && pytest proxyshop_support -q`
- **Scope:** `conftest.py`, `proxyshop_support/**` · `parallel_safe: false`

#### T-111 — Provisioning fails loudly when the flat namespaces are dead

scripts/bootstrap.sh:26-33 un-hides the uv-written _proxyshop.pth (macOS UF_HIDDEN, which site.addpackage silently skips) but ends in '|| true', and its namespace assertion at :48-51 ends in '|| echo WARNING >&2'. Neither can fail the provisioning. This …

- **State:** **READY**
- **depends_on:** T-000 ✅
- **Unblocks (open):** 1 — T-123
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/bootstrap.sh && ./scripts/verify.sh check`
- **Scope:** `scripts/bootstrap.sh` · `parallel_safe: false`

#### T-117 — [BLOCKED: protected path] The per-ticket gate runs the tests that grade its own acceptance

BLOCKED — not dispatchable as scoped. The fix requires editing scripts/verify.sh:105, which is in state.json protected_paths ('.swarm-loop/acceptance', '.swarm-loop/goals.json', 'Makefile', 'scripts/verify.sh', 'scripts/check_verify_contracts.py'), so …

- **State:** **BLOCKED** by T-109
- **depends_on:** T-109 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/verify.sh check`
- **Scope:** `scripts/verify.sh`, `conftest.py`, `proxyshop_support/**` · `parallel_safe: false`

#### T-123 — The .pkgroot namespaces survive a pytest run (root cause: site-packages itself is flagged)

`_proxyshop.pth` in the primary checkout keeps reverting to macOS UF_HIDDEN, which `site.addpackage` silently skips, so every .pkgroot flat namespace (contracts, llm, trust, ...) becomes unimportable outside pytest. This has now bitten the run THREE times …

- **State:** **BLOCKED** by T-111
- **depends_on:** T-111 ⛔
- **Unblocks (open):** 0
- **Acceptance:** 6 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/bootstrap.sh && ./.venv/bin/python -m pytest packages/llm -q && ./.venv/bin/python -c 'import contracts, llm, trust'`
- **Scope:** `scripts/bootstrap.sh`, `conftest.py`, `proxyshop_support/**` · `parallel_safe: false`

#### T-130 — Sub-HIGH findings from the feature-wave checkers (backlog sweep, not a wave)

Recorded so nothing is lost, per the user's standing instruction: report every severity, but only HIGH and above interrupts the build. These are the MEDIUM findings the five feature-lane checkers raised against T-030/T-036, T-040, T-050, T-070 and T-080. …

- **State:** **READY**
- **depends_on:** T-030 ✅, T-040 ✅, T-050 ✅, T-070 ✅, T-080 ✅
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/verify.sh check`
- **Scope:** `apps/exchange/**`, `packages/store-agent/**`, `apps/merchant/**`, `apps/buyer/**`, `fixtures/**` · `parallel_safe: false`

#### T-132 — Rotating pseudonyms are trivially re-linkable, so T-070's central guarantee does not hold

DECISION MADE (user, 2026-09-02): amend the frozen contract. Note the amendment number is now 4, not 3 — a separate amendment 3 (build_succeeds logging) landed on main at f65816a. [HIGH] T-070's objective is 'rotating pseudonyms, identity-free profiles'. The …

- **State:** **READY**
- **depends_on:** T-070 ✅
- **Unblocks (open):** 0
- **Acceptance:** 4 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/verify.sh check`
- **Scope:** `apps/buyer/**` · `parallel_safe: false`

#### T-133 — Buyer residue: shared session state, unwired publish half, and unbounded stores

Reported by the T-070 lane, not fixed because each crosses its scope or needs shared infrastructure. The lane DID ship a loud refusal for the first item — `build_auth_service` now refuses to start when WORKER_COUNT_ENVS indicates more than one worker, …

- **State:** **READY**
- **depends_on:** T-070 ✅
- **Unblocks (open):** 0
- **Acceptance:** 3 criteria · **Frozen:** **no frozen coverage** — graded by its own verify only
- **verify:** `./scripts/verify.sh check`
- **Scope:** `apps/buyer/**`, `packages/**` · `parallel_safe: true`

### Open finding tickets (T-136 – T-153) — none has a gate yet

*12 open · 12 ready · 0 blocked.* All twelve are roots and all twelve are HIGH. Seven of the
nineteen minted finding tickets have since closed (T-135, T-138, T-143 – T-146, T-149); these
are the twelve that have not. **T-151, T-152 and T-153 are new to this ledger** — the previous
revision did not contain them at all.

#### T-136 — The store-agent hook surface has no production caller

Every runtime capability the branch adds is unwired: ToolHooks and its six hooks, start_bid, would_authorize, Denied, HookCall, HookInputError, enforce_hook_provenance, ClaimScopeError, mint_claim, mint_provenance, claim_fingerprint, scoped_ref, claim_scope, claim_is_scoped_to. Zero production call sites — only the package's own re-exports, its unit tests, and the frozen acceptance suite. …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `packages/store-agent/src/hooks/tools.py:161` · **Finding:** `0191c9de8c2b4adb…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** codegraph explore "who imports or calls ToolHooks, enforce_hook_provenance, mint_claim, hosted_claim_construction_offenders" lists callers only in packages/store-agent/src/hooks/__init__.py and packages/store-agent/tests/test_hooks.py (+ …
- **Scope:** `packages/store-agent/src/hooks/tools.py`

#### T-137 — refresh_digests re-pins an approval onto bytes the approver never saw

refresh_digests() re-pins approval.content_hash onto whatever the manifest body currently says while leaving approver / approved_at / artifact populated. This defeats the exact tamper-evidence property the frozen acceptance test claims for itself (test_e8_proofs.py:288-291: 'the manifest ... cannot drift after approval without this assertion turning red'). After a re-pin the approval block is fully populated and …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `fixtures/manifest/__init__.py:383` · **Finding:** `49de220d5e6ca84e…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** cp fixtures/manifest.json $SCRATCH/m2.json; then in .venv/bin/python: set m['approval'] = {status:'approved', approver:'Hank Holcomb', approved_at:'2026-09-02T12:00:00Z', artifact:'fixtures/approval/REQUEST-manifest-approval.md'} and write it back -> …
- **Scope:** `fixtures/manifest/__init__.py`

#### T-139 — The one uncapped profile bucket can publish the buyer's name and address verbatim

identity_leaks holds every slug of the account's OWN order categories out of the searchable haystack (_incidental_bucket_values, line 383), account-wide rather than per-bucket. category_affinity is the one bucket with no vocabulary or length cap (coarsen_categories, line 215, caps count only at CATEGORY_LIMIT=3), so the buyer's name, street and postal code can be published verbatim into the store-facing profile …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/buyer/svc/src/profile/__init__.py:432` · **Finding:** `abcfa66ee367de15…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** account = {'email':'dana.reyes@example.com','first_name':'Dana','last_name':'Reyes','address':'44 Alder Way, Portland OR 97205','postal_code':'97205','region':'US-OR','orders':[{'total':120.0,'category':'gift for Dana Reyes, 44 Alder Way Portland …
- **Scope:** `apps/buyer/svc/src/profile/__init__.py`

#### T-140 — AccountDirectory has no production populator, so every buyer profile is empty

AccountDirectory has exactly one implementation and no production populator. build_auth_service() never passes accounts=, so production runs the empty default InMemoryAccountDirectory, and MagicLinkAuth.redeem (line 304) writes only {'email': email} (+orders: []). Every coarsener therefore runs on an empty account in production: GET /buyer/profile returns an identical, information-free profile for every buyer. The …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/buyer/svc/src/auth/magic_link.py:193` · **Finding:** `81904f930f71eb17…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** routes.set_auth_service(None); svc = routes.build_auth_service() -> accounts=InMemoryAccountDirectory (empty), vault store=InMemoryPseudonymStore, sessions=InMemorySessionStore. Log in three different addresses through POST /buyer/auth/magic-link + POST …
- **Scope:** `apps/buyer/svc/src/auth/magic_link.py`

#### T-141 — The magic link is delivered to a no-op, so login cannot complete in production

The magic-link token is delivered to the default no-op `_drop` (line 166) in every deployment, and set_auth_service (routes.py:146) — the documented seam for installing a mail transport before the first request — has ZERO callers anywhere in the repository, including tests, which override the FastAPI dependency instead. No production code path can deliver a login link, so magic-link login cannot complete outside a …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/buyer/svc/src/auth/magic_link.py:194` · **Finding:** `a1e986a7af5d9c64…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** grep -rn 'set_auth_service|deliver=' --include=*.py apps packages services scripts e2e fixtures -> set_auth_service appears only in its own definition, __all__ and a docstring; every deliver= is in apps/buyer/svc/tests/test_auth_vault.py. …
- **Scope:** `apps/buyer/svc/src/auth/magic_link.py`

#### T-142 — publish_profile is dead code, so no store ever receives a BuyerProfile

publish_profile — the only writer of app.buyer_accounts, described as 'the store-visible working set' — has zero production call sites, and no code outside the buyer service consumes BuyerProfile or reads app.buyer_accounts. The R5 deliverable 'the BuyerProfile handed to stores' is never handed to a store: GET /buyer/profile requires an X-Buyer-Session header, which only the buyer holds. The hardening commit names …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/buyer/svc/src/profile/__init__.py:492` · **Finding:** `beaddb94fcb3a852…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** codegraph explore 'publish_profile callers' -> 'publish_profile (apps/buyer/svc/src/profile/__init__.py:492) — 1 caller; tests: apps/buyer/svc/tests/test_auth_vault.py'. grep -rn 'BuyerProfile|buyer_accounts' over apps/packages/db/services/e2e (excluding …
- **Scope:** `apps/buyer/svc/src/profile/__init__.py`

#### T-147 — T-036's checkout port has no production consumer and no frozen marker

T-036 — the whole second half of this lane — is unwired. Nothing outside apps/exchange/src/checkout/ ever resolves a provider or calls CheckoutProvider.checkout: the port, both providers, the registry, the domain guard, mint_code and the minting lint have zero production consumers. apps/exchange/src/accept/__init__.py, which T-036's own ticket says is the path that must go through the port, is a 0-byte file. And …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/exchange/src/checkout/registry.py:61` · **Finding:** `6ceae2ee1b39af81…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** `codegraph explore "who calls CheckoutProvider.checkout, resolve_provider ..., SimulatedRedirectProvider — production call sites outside the checkout package"` returns callers only in apps/exchange/src/checkout/{__init__,registry,providers}.py plus …
- **Scope:** `apps/exchange/src/checkout/registry.py`

#### T-148 — configure_auctions has no caller, so the shipped exchange denies every store

`configure_auctions` — the only way to give the exchange a real solicitor, a real eligibility source, a Redis-backed store or a real ledger sink — has no production caller anywhere in the repo. The service as it actually boots (`uvicorn exchange.main:app`) therefore runs on _eligibility()'s fail-closed default, StaticSellerEligibility() with no rows, whose answer for every store is UNAVAILABLE. The shipped exchange …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/exchange/src/auction/routes.py:103` · **Finding:** `c2d3c196c797f608…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** PROXYSHOP_WORKER=15 .venv/bin/python: `app = exchange.main.create_app()` with NO configure_auctions call (i.e. exactly what the ASGI entrypoint builds), then POST /auctions with a 2-store roster. Observed: status 201, entries: [], solicited: [], denied: …
- **Scope:** `apps/exchange/src/auction/routes.py`

#### T-150 — The ledger writer has zero producers

The ledger writer has zero producers. Nothing in the repository writes an event into it - not by import, not over HTTP. A repo-wide grep for `trust.events` / `apps.trust.src.events` / `/events` outside the package returns only the frozen acceptance suite, the branch's own tests, and `trust.main`'s router glob; codegraph agrees. The component that should be the first producer, the exchange auction state machine, …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/exchange/src/auction/ledger.py:54` · **Finding:** `f21e31a9067af7c2…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** grep -rn --include='*.py' --include='*.ts' -e 'trust\.events' -e 'apps\.trust\.src\.events' . | grep -v '/\.venv/' | grep -v '^\./apps/trust/' | grep -v '^\./\.swarm-loop/' -> no output. `codegraph explore "create_events_app InMemoryEventStore router …
- **Scope:** `apps/exchange/src/auction/ledger.py`

#### T-151 — Ledger writes silently use the wrong DB role

Ledger writes silently use the wrong DB ROLE. DEFAULT_DSN_ENV = ('PROXYSHOP_LEDGER_DSN','PROXYSHOP_PG_DSN_APP') never consults PROXYSHOP_PG_DSN_TRUST_RW, so a deployment that sets only the documented per-role variable writes the ledger as the 'app' role instead of the 'trust_rw' role D5 grants it. Privilege confusion that no test sees because tests never set the per-role variable. Found by running the real …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `apps/trust/src/events/pg.py:67` · **Finding:** `22fceed83fab9f31…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** Set only PROXYSHOP_PG_DSN_TRUST_RW (the documented per-role var) and start the trust service; observe it connects as the app role. Worked around in compose by setting PROXYSHOP_LEDGER_DSN explicitly.
- **Scope:** `apps/trust/src/events/pg.py`

#### T-152 — The hardened R8 boundary is not called by anything

The hardened R8 boundary is not called by anything. enforce_bid_provenance(bid, hooks) must be invoked on the WHOLE bid; passing bid.claims (the flat list) leaves the Offer -- and its discount and stated price -- entirely outside the boundary, which is the original defect. No function can detect this about its own caller. src/runtime/ is empty, so T-041 owns the call site.

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `packages/store-agent/src/runtime/ (empty) + enforce_bid_provenance` · **Finding:** `c3b19780d2b25a50…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** grep for enforce_bid_provenance outside packages/store-agent -> no production caller. Passing bid.claims admits an unauthorised 25% discount carried on the Offer.
- **Scope:** `packages/store-agent/src/runtime/ (empty) + enforce_bid_provenance`

#### T-153 — A bid's stated price is never reconciled against its granted discount

A bid's stated price is not reconciled against its granted discount. The floor wall exists, but unit_price ~= list_price * (1 - pct) is NOT verified. A real Bid with an honest 20% grant and unit_price=1.00 under a 10.00 floor was admitted before the floor wall; the arithmetic relation is still unchecked because quantity/currency semantics for total_price are undecided. contracts.boundary.validate_bid does schema + …

- **State:** **READY**
- **depends_on:** *(none — this is a root)*
- **Unblocks (open):** 0
- **Severity:** HIGH · **Location:** `packages/store-agent/src/hooks/tools.py (price arithmetic)` · **Finding:** `3958348dc2ef784a…`
- **Graded by:** nothing yet — no frozen test, and `verify` is
  `false  # NO GATE YET — write one that fails on this finding first`, which cannot pass.
  **Write the failing gate first.**
- **Reproduction:** Construct a Bid whose unit_price does not follow from list_price and the granted discount pct; the boundary admits it.
- **Scope:** `packages/store-agent/src/hooks/tools.py (price arithmetic)`

---

# Scheduling constraints

Per-ticket file ownership is `intake-report.md` §4 (narrowed) and constraint pairs are §5, with
the **flat** source layout of D42 overriding §3.1/§4's nested tree. Every membership list below
was re-derived against the current 51-ticket open set; closed tickets are struck from them
because they can no longer be co-scheduled with anything.

## Neo4j is ONE shared database (D4, narrowed by D38)

`CREATE DATABASE` is unsupported on Community, so there is no per-worker graph.

- **Never co-schedule two graph-writing tickets.** The graph-writing set is T-012, T-020,
  T-021, T-022, T-023, T-024, T-031 — of which **T-022, T-023, T-024 and T-031 are still
  open**, three of them in the ready frontier. They serialize against each other.
- **Never co-schedule `e2e/` with a graph lane.** `e2e/` resets and writes the same shared
  graph. The session-scoped `flock` on `/tmp/proxyshop-neo4j.lock` (root `conftest.py`,
  `_neo4j_guard`) is the safety net, not the plan: it makes a collision slow rather than
  corrupt. The `e2e/**`-scoped tickets are **T-082, T-083, T-084** — all open, all blocked.
- Vector-index creation is `CREATE VECTOR INDEX … IF NOT EXISTS`, so a re-entrant session never
  errors. Do not "fix" that by dropping the index.

**The hazard is directory-level, not ticket-level — run the declared verify, never the
directory.** `neo4j_driver` calls `reset_graph` (`MATCH (n) DETACH DELETE n`,
`proxyshop_support/neo4j_lock.py:157-169`) once per session, and *any* invocation that collects
a directory rather than a file drags in a frozen scaffold test that requests `neo4j_session`
and wipes the graph. Two directories carry this:

- `e2e/` — via `e2e/test_scaffold_smoke.py::test_the_e2e_lane_holds_the_d37_neo4j_lock`.
  **T-086 is deliberately not in the `e2e/**` exclusion**: its scope is the two narrow paths
  `e2e/test_onboarding.py` and `e2e/support/onboarding/**`, all four of its acceptance criteria
  are Postgres-shaped, and it depends on none of T-012/T-030/T-031. But its agent must run
  `pytest e2e/test_onboarding.py -q`, **never** `pytest e2e/`, while a graph lane holds the
  surface.
- `apps/exchange/tests/` — via the frozen
  `apps/exchange/tests/test_scaffold_datastores.py:69::test_neo4j_session_runs_inside_the_d37_flock`.
  All five open exchange tickets declare a **single-file** verify, so every declared verify is
  safe beside a graph lane; a broadened invocation from any of them is not.

## Shared scope globs — per-file ownership, not `parallel_safe`, is what separates them

`parallel_safe: true` does not mean "shares no directory". Recomputed from `tickets.json` in
this pass; **open members only** — a glob whose other claimants are all closed no longer
constrains scheduling.

| glob | open tickets declaring it |
|---|---|
| `apps/exchange/tests/**` | T-031, T-032, T-033, T-034, T-035 |
| `apps/trust/tests/**` | T-061, T-062, T-063, T-064, T-065 |
| `packages/store-agent/tests/**` | T-041, T-042, T-043, T-044 |
| `apps/buyer/**` | T-130, T-132, T-133 |
| `apps/buyer/svc/tests/**` | T-071, T-072, T-073 |
| `apps/merchant/svc/tests/**` | T-051, T-052, T-053 |
| `conftest.py` | T-109, T-117, T-123 |
| `e2e/**` | T-082, T-083, T-084 |
| `proxyshop_support/**` | T-109, T-117, T-123 |
| `services/ingest/tests/**` | T-022, T-023, T-024 |
| `apps/buyer/svc/src/auth/magic_link.py` | T-140, T-141 |
| `apps/buyer/svc/src/profile/__init__.py` | T-139, T-142 |
| `packages/store-agent/src/hooks/tools.py` | T-136, T-153 |
| `scripts/bootstrap.sh` | T-111, T-123 |

**Four of those rows are single-file collisions between finding tickets and are easy to miss:**
T-139 and T-142 both rewrite `apps/buyer/svc/src/profile/__init__.py`; T-140 and T-141 both
rewrite `apps/buyer/svc/src/auth/magic_link.py`; **T-136 and T-153 both rewrite
`packages/store-agent/src/hooks/tools.py`**. Six of the twelve open finding tickets therefore
collapse into three single-file lanes, and the two buyer lanes sit inside T-132's and T-133's
scope as well.

**T-152 has no scope of its own — it is work inside T-041's lane.** Its declared scope,
`packages/store-agent/src/runtime/ (empty) + enforce_bid_provenance`, normalises to
`packages/store-agent/src/runtime`, which is exactly T-041's `packages/store-agent/src/runtime/**`
and today contains only an `__init__.py`. The ticket's own defect text says so: *"src/runtime/
is empty, so T-041 owns the call site."* **Do not dispatch T-152 as a separate lane** — either
give it to whoever takes T-041, or land T-041 first and re-scope T-152 to the call site it
creates. Dispatching both at once is a guaranteed conflict on an empty directory.

Four more globs now have exactly one open claimant, so the pairing is discharged by history but
the *rule* the split encoded still binds the survivor:

- `apps/exchange/src/orchestration/**` — open **T-033**, closed sibling T-030. D54 states the
  split explicitly: T-030 created the package with `solicit_bids`; **T-033 adds `accept_offer`
  and must not modify `solicit_bids`.**
- `services/ingest/src/adapters/**` — open **T-023**, closed sibling T-020.
- `services/ingest/src/extraction/**` — now has **no** open claimant; T-021 closed at
  `68f753d`, so the ingest extraction surface is free.
- `packages/**` — open **T-133**, closed sibling T-122.

### Scope containment — three open tickets enclose most of the board

`parallel_safe` and the glob table both compare scopes for *equality*. The more dangerous
relation is **containment**: a ticket whose scope is a broad `**` glob silently owns the files
of every narrower ticket underneath it. Recomputed across all 51 open tickets, after
normalising the two finding scopes that carry prose rather than a glob:

| ticket | open tickets whose scope it contains | count |
|---|---|---|
| **T-130** | T-031, T-032, T-033, T-034, T-035, T-041, T-042, T-043, T-044, T-052, T-053, T-054, T-071, T-072, T-073, T-132, T-136, T-137, T-139, T-140, T-141, T-142, T-147, T-148, T-150, T-152, T-153 | **27** |
| **T-133** | T-041, T-042, T-043, T-044, T-072, T-073, T-132, T-136, T-139, T-140, T-141, T-142, T-152, T-153 | **14** |
| **T-132** | T-072, T-073, T-139, T-140, T-141, T-142 | **6** |
| **T-123** | T-109, T-111 | **2** |
| **T-117** | T-109 | **1** |
| **T-084** | T-086 | **1** |
| **T-083** | T-086 | **1** |
| **T-082** | T-086 | **1** |
| **T-041** | T-152 | **1** |

Read that as a dispatch rule, not trivia: **none of these may be co-dispatched with anything it
contains.** T-130 in particular is a cross-cutting sweep of sub-HIGH findings from five feature
lanes and, at its declared scope, conflicts with over half the open board. Take it alone, split
it by lane, or land the narrow tickets first and re-scope it to the remainder. The small
containments matter too: **T-123 contains T-109 and T-111**, and **T-117 contains T-109**, which
is the graph and the file system agreeing for once — the infra-debt cluster is one lane wearing
four ticket numbers.

### Two clusters inside the ready frontier serialize against themselves

This is the constraint most likely to be missed, because both clusters look like independent
small tickets:

- **The infra-debt cluster: T-109, T-111, T-117, T-123.** They pile onto three shared paths —
  `proxyshop_support/**` (three of the four), `conftest.py` (three) and `scripts/bootstrap.sh`
  (two). Two are READY (T-109, T-111) and **must not be co-dispatched**. Ordering that respects
  both the graph and the overlap: **T-111 → T-123**, then **T-109 → T-117** (T-117
  additionally needs a protected-path exception). T-124, the fifth member of this cluster in
  the previous ledger, has closed.
- **The cycle-8 residue cluster: T-130, T-132, T-133.** All three are READY, and **T-130
  overlaps both of the others** — `apps/buyer/**` with T-132 and T-133 — plus eleven of the
  twelve open finding tickets. Dispatch T-130 **alone**, or split it by lane, or land the
  narrow tickets first and re-scope it to what remains. T-131 and T-134, previously in this
  cluster, have closed (`1537bcc` and `7f75e48`).

## A data-shape note on `tickets.json` — one hazard discharged, one live

**Discharged:** the previous ledger warned that 25 tickets stored `scope` as a comma-joined
**string** rather than a list, so any tool doing `for g in ticket["scope"]` would iterate single
characters. **That is no longer true.** Measured in this pass: all 101 tickets store `scope` as
a list. The normalising read is no longer required.

**Live:** two finding tickets store English prose inside a scope entry rather than a path —
`T-152: "packages/store-agent/src/runtime/ (empty) + enforce_bid_provenance"` and
`T-153: "packages/store-agent/src/hooks/tools.py (price arithmetic)"`. Any scheduler that
matches these literally will find no files and conclude the tickets conflict with nothing —
which is wrong in both cases (T-152 collides with T-041, T-153 with T-136). Strip at the first
` (` or ` +` before comparing:

```python
path = entry.split(' (')[0].split(' +')[0].strip().rstrip('/')
```

Recorded, not repaired: `tickets.json` is the authoritative source and this ledger does not edit
it. Fixing the shape is a change to the graph and belongs to whoever next amends it.

## T-085 and T-087 share one verify command, and T-087 does not own it

Both declare `pytest docs/tests/test_runbook.py -q`; `docs/tests/**` is in **T-085's** scope and
T-087's `non_goals` forbid touching it. T-085's acceptance 3 makes that lint check the **union**
of every markdown file under `docs/demo/`, and T-085's own objective says the union is only
satisfied once T-087 also lands. T-087 depends on T-085, so ordering discharges the write
conflict; the shared verify is a *reading* hazard — do not read T-087's green as scoped to T-087.

## Datastore isolation

- **Postgres isolates per-worker database.** Each worker gets `proxyshop_w<N>`
  (`scripts/db_init.py`, `make db-init`). Roles are cluster-global (`pg_authid.relisshared`), so
  `CREATE ROLE` lives once in `db/init/00-roles.sql` and GRANTs — per-database — live in
  `db/migrations` so every `proxyshop_w<N>` gets its own copy. No Postgres serialization
  constraint applies between tickets.
- **Redis isolates per-worker logical DB index** (16 available) plus a central `w{N}:` key
  prefix in `proxyshop_support`. `FLUSHALL` is banned repo-wide and grepped for in `make verify`;
  `FLUSHDB` is the permitted reset. `maxmemory-policy` is `noeviction`. No Redis serialization
  constraint applies between tickets.
- **`PROXYSHOP_WORKER` must be set for every dispatch.** The root `conftest.py` fails the
  session if it is unset — deliberately, because a silent fallback is how every worker lands in
  one database.

## Bring the datastore stack up before any cycle metric sweep

`make deps-up` first. With the compose stack down `make verify` is still green — the
`@pytest.mark.docker` tests skip cleanly — so `build_succeeds` reads 1 without ever exercising
`pg_role`, the D5 grants, the D37 flock or the Redis prefix. **T-109 is the ticket that makes
this worse than it sounds**: today a blip on *any one* of the three datastores empties the whole
docker-marked gate, including 100% of T-011's acceptance criteria 1 and 4.

## Human gates and rulings

T-080's manifest gate — which stood over 16 tickets — was **approved by the user and closed**
(`8a2841f`, merged in cycle 8). No open ticket is waiting on a human approval gate.

- **T-132 awaits a user ruling.** It needs a frozen-contract amendment; the user approved
  amending in principle, and design v1 was refuted at `bda0ce0` and recorded rather than applied.
- **T-117 is blocked on a protected path**, not on a ruling. Its fix requires editing
  `scripts/verify.sh`, which is in `state.json.protected_paths`, so it is not dispatchable as
  scoped even once T-109 lands.
- **T-137 needs an adjudication recorded against the graph, or a commit.** Cycle 9 refuted it in
  prose and shipped only a docstring; as long as the code at `fixtures/manifest/__init__.py:863-877`
  is unchanged, no evidence closes it. Either land a fix or record the refutation as a graph
  amendment — leaving it as prose guarantees the next regeneration carries it forward again.

---

# Frozen acceptance coverage — read green accordingly

The acceptance suite was frozen at cycle 0 and amended once (amendment 1, `31c6be0`) to **120
tests**. Measured by AST in this pass: 120 test functions carry exactly one
`@pytest.mark.ticket` marker each, 120 markers total, no test carries two. **79 of those markers
sit on open tickets.** Coverage is not uniform, and **27 of the 51 open tickets carry no frozen
test at all**:

> T-024, T-031, T-054, T-082, T-083, T-084, T-086, T-087, T-109, T-111, T-117, T-123, T-130, T-132, T-133, T-136, T-137, T-139, T-140, T-141, T-142, T-147, T-148, T-150, T-151, T-152, T-153

For those, **the ticket's own `verify` command is the entire grade**, and it is written by the
same agent that writes the code. Weigh their green accordingly.

**For the twelve open finding tickets even that is not true.** All twelve carry
`verify: false  # NO GATE YET — write one that fails on this finding first`, so they have
neither a frozen test nor a working verify command: `false` cannot pass. They are graded by
nothing at all until someone writes the gate. That is deliberate — the minting commits promoted
only HIGH findings to tickets, each with a `location`, a `defect` and a `reproduction` field
instead of an acceptance list — but it means **no metric will move, and no gate will redden,
when one of these defects is present or absent.** Nothing in `make verify` sees them.

Two cases in that list are not what they look like:

- **T-036's work IS frozen — under T-033's marker.** A repo-wide search finds **zero**
  `@pytest.mark.ticket("T-036")` decorators in the frozen suite. The two frozen tests that
  actually exercise `accept(auction, bid_ref, creator, mode)` —
  `test_e3_exchange.py:697::test_double_accept_on_one_auction_is_rejected` and
  `test_spec_criteria.py:1079::test_offdomain_checkout_url_is_refused` (blocker S8-3) — are both
  marked **T-033**. A broken checkout port therefore shows up as a red `e3_exchange_passing`
  **and** a red `spec_criteria_passing`, both attributed to T-033. T-036 is closed; the marker
  asymmetry is inherited by T-033, which is in the ready frontier. This is also why T-147 is
  correct that T-036 contributes 0 of the epic's passes on its own account.
- **T-054, T-082, T-083, T-084 are E-metric-invisible.** They carry no ticket marker, so no
  frozen number moves when they land. Their acceptance is real and gradeable — by their own
  verify commands only.

## T-065's sixth frozen test is graded AFTER T-062 — not a defect in T-065

**ESC-003, ruled by Hank at cycle 1: no amendment.** Recorded because the next reader will
otherwise re-derive it as a blocker, and because T-065's worker must be told. **This matters
more now than it did**: T-065 is READY, unblocks 13, and is the highest-value trust lane on the
board.

`test_e6_trust.py:1470`, `test_verification_results_are_typed_and_route_into_the_catalog_claim_dimension`,
carries `@pytest.mark.ticket("T-065")` but does `from apps.trust.src.scoring import
claim_dimension, score`. `apps/trust/src/scoring/**` is **T-062's exclusive scope**, and the
edges run the wrong way: `T-062.depends_on` includes `T-065`, while `T-065.depends_on` does not
include T-062. So T-065 is graded first, when that module does not exist, and the import — which
sits inside the test function — raises `ImportError` at call time. Adding `T-065 → T-062` is
unavailable: it creates a genuine cycle.

- **T-065's packet must say this explicitly.** Its sixth frozen test will fail with an
  `ImportError` that is *expected* and that the worker **cannot fix from inside its own scope**.
  The natural "fix" — creating the module — is a scope violation `check-branch` rejects.
- **T-065 closes on its other five frozen tests plus its own verify command**, not on 6/6.
- **T-062's packet inherits the other half:** it must ship `claim_dimension` and `score` such
  that this test passes, even though the test is not marked T-062.

**Blast radius is exactly one test.** An AST sweep of all 120 frozen tests found exactly one
violation, this one. The rule worth carrying: **a cross-ticket import is fine iff the imported
module's owner is in the importing ticket's transitive dependency closure.**

---

# Carry-forward defects (found in cycle 0, never ticketed)

Each was seen by an adversarial verifier during T-000 and consciously deferred. None blocks
dispatch; each has a named owner so it is fixed by the ticket that already touches the file.
**All four were re-checked against HEAD in this pass and all four are still live.** CF-2, CF-3
and CF-4 named T-011 as owner and T-011 has since closed, so they are unowned and fall to the
next ticket that touches each file.

- **CF-1 — stale docstring in the frozen datastore proof.** *(cosmetic, misleads agents)*
  `apps/exchange/tests/test_scaffold_datastores.py:9-12` still says "this directory's conftest
  overrides `_neo4j_guard` with the D37 flock". That override was deleted when the lock moved to
  the root `conftest.py` for **every** session. Owner: orchestrator (the file is T-000-frozen).
- **CF-2 — `.importlinter`'s `**` pattern has no regression guard.** *(latent gate break)*
  The D39 contract exempts a legal import with `ignore_imports = ** -> redis.exceptions`
  (`.importlinter:67`), but no tracked file imports `redis.exceptions`, so reverting `**` to `*`
  passes `make verify` today. The first ticket to write `except redis.exceptions.ConnectionError`
  inherits a break it did not cause. Owner was T-011 (closed) — reassign to the next ticket
  touching the import-lint proof.
- **CF-3 — D5's wording is imprecise and `db/init/00-roles.sql` repeats it.** *(will mislead)*
  D5 and `db/init/00-roles.sql:11` both call the grant model "schema-level USAGE only". `USAGE`
  on a schema grants **name resolution**, not row access; the migrations must also issue
  table-level `SELECT` on `ledger` and `app` while granting **nothing at all** on `sealed` and
  `vault`. T-011 shipped correctly; the *prose* is still wrong and will mislead the next reader.
- **CF-4 — latent idle-in-transaction deadlock between `pg_role` and `pg_admin`.** *(latent hang)*
  `pg_admin` (`conftest.py:167-190`) is autocommit; `pg_role` (`conftest.py:194-236`) returns a
  connection in default transactional mode, cached per role for the whole test. A
  `pytest.raises(InsufficientPrivilege)` assertion leaves that connection idle-in-transaction
  holding an `ACCESS SHARE` lock; any subsequent `DROP SCHEMA`/`TRUNCATE`/`ALTER` through
  `pg_admin` in the same test blocks until `lock_timeout` and reads as a hang. The packet rule
  for any ticket combining both fixtures — T-061 – T-065 all can — is *roll back or close the
  `pg_role` connection before touching DDL through `pg_admin`*.

Also noticed, not worth a ticket:

- `.importlinter`'s header cites "(D34)" for the import-lint contract; the ruling is **D35**.
  Symmetrically, `intake-report.md` §9-B cites "D33" for the T-082 multiset; it is **D34**.
- The 18 empty `<name> 2` sibling directories the previous ledger listed (`apps/exchange 2`,
  `db/init 2`, `packages/llm 2`, …) are **gone** — re-checked in this pass, zero remain. That
  cleanup item is discharged and is not carried forward again.

---

*Regenerated from `tickets.json` on 2026-09-02 at `main` = `1b08077`. The graph is **101
tickets / 141 edges**; 50 closed, 51 open, 27 of the open ready. To regenerate: recompute the
totals, the closed set (from `main`, not from any status field — there is none), and
READY/BLOCKED from the graph, then rewrite this file whole. **Never edit `tickets.json` to agree
with this file.** Two `scope` entries carry English prose rather than a path (T-152, T-153) —
strip at the first ` (` or ` +` before computing file ownership.*
