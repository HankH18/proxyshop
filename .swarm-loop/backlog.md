# ProxyShop — ticket ledger (backlog)

> **DERIVED VIEW. `tickets.json` is the ground truth.** Regenerated from the graph; never
> hand-edit. Any amendment that changes `tickets.json` must regenerate this file, or the human
> view goes stale in exactly the way nobody checks — which is what happened before this pass.

Regenerated **2026-09-04** from `tickets.json` at `main` = **`635d442`**.

## The count (stated once, here, and nowhere else)

| | |
|---|---|
| **tickets in the graph** | **197** |
| dependency edges | 138 |
| **closed** (positive evidence only) | **63** |
| **OPEN — everything below** | **134** |
| …of which carry a real gate | 74 |
| …of which carry the `false  # NO GATE YET` placeholder | 60 |
| …of which touch a protected path (orchestrator-owned) | 10 |

Closed tickets are NOT listed here. They survive in git history, in `dispatch.jsonl`, and in
`.swarm-loop/reports/cycle-*.md`. A ledger that only grows is a defect.

### How closure was decided (re-derived, not recalled)

```
PROXYSHOP_WORKER=0 .venv/bin/python ~/.claude/skills/swarm-loop/scripts/swarmloop.py \
    frontier --closed-from-ledger --closed-from-merged --base main
```

- **63** closed by a `dispatch --close … --verdict accepted` record in
  `dispatch.jsonl` (the durable source; last verdict per ticket wins).
- **9** of those 63 are independently corroborated by merged-branch
  evidence — a `task/*` branch merged into `main` whose tip has moved past the sha a dispatch
  record pinned: T-022, T-023, T-052, T-053, T-071, T-072, T-073, T-081, T-228.
- **0** closed on merged-branch evidence alone. The merged set is a strict subset of the ledger set.
- **2 UNDETERMINED → left OPEN**: T-082, T-223. Their branch tips sit on
  `main`'s first-parent history, which is equally true of a lane merged fast-forward and of a lane
  provisioned and never built; nothing in git can tell those apart. Corroborating that they are
  genuinely unfinished, measured this pass: `task/T-082` is +0 ahead / 5 behind `main`, `task/T-223` is +0 ahead / 5 behind `main`. Bias to open: a false-CLOSED silently drops real work, a false-OPEN only burns a lane.

### The graph's own `status` field contradicts the evidence — evidence wins

`tickets.json` carries a `status` / `status_evidence` pair on 168 tickets that `frontier` does
**not** read. It disagrees with the measured closure in both directions:

- **59 tickets say `"status": "closed"` but have no accepted dispatch verdict and no
  merged-branch proof.** They are listed below as OPEN and tagged `graph-says-closed`. Do not
  re-dispatch one without reading its `status_evidence` first — several name a landing sha
  (e.g. T-010: *"landed: 62c19ea … merged 088e818"*). Closing them for real means recording
  `dispatch --close <ref> --ticket <id> --verdict accepted`; until then they burn a lane, which
  is the cheap failure.
- **22 tickets say `"status": "open"` but have an accepted verdict** and are therefore
  absent below: T-022, T-023, T-032, T-034, T-035, T-042, T-043, T-044, T-045, T-052, T-053, T-071, T-072, T-073, T-081, T-168, T-191, T-200, T-206, T-211, T-215, T-216.

- **45 of the 105 placeholder-gated tickets in the graph were accepted anyway** — a
  ticket was closed while its `verify` was still literally `false`. Those closures rest on the
  operator's verdict alone, with no gate behind them.

<!-- ============================================================================
     RESUME STATE — carried forward and re-derived at the cycle-16 epoch boundary.
     Read this block FIRST on resume. Facts that only the machine could answer are
     marked UNVERIFIED rather than restated: a stale fact stated confidently is worse
     than an absent one.
     ============================================================================ -->

## RESUME STATE (read this first)

### Run constants that are NOT in state.json

| | |
|---|---|
| worktree root | `../proxyshop-worktrees` (recorded in state.json) |
| **scratch root** | **`/Users/hankholcomb/Documents/code_parent_folders/gauntlet_repos/proxyshop-scratch`** |

**The scratch root is NOT recorded in `state.json` and `resume` will print it as
UNRECORDED.** It was chosen by hand, deliberately, because the run's sync instructions forbid
`init --scratch-root` and `init --force` (`--force` rebuilds state at cycle 0 and unfreezes the
goals). Every lane gets `<scratch-root>/<task-id>/`; the root itself is shared and is not
isolated the way a worktree is. Use the value above; do not invent a second one.

### Where the run stands (git facts re-derived this pass)

- `main` = **`635d442`** — `merge: trust reproductions + honest T-193 probe`.
- **Unpushed.** Local remote-tracking refs say `gitlab/main` = `f642e43` (34 commits behind
  HEAD) and `github/main` = `e13524c` (108 behind). Those are *local* refs, not re-read from
  the remotes this pass — the previous claim in this block ("pushed to both remotes … zero
  unpushed") was true of `f642e43` and is now false. Verify by reading each remote's SHA back
  before claiming a push.
- `state.json` `current_cycle` = **16** (checkpointed at `9ef97f8`, 713 tracked files), `max_cycles` 102,
  `audit_loops` 3 (0 completed), `stall_count` 0/3, `goal_stall_count` 0, autonomy `approve-cycle-1`, push `when-clean`.
- **Metrics: UNVERIFIED.** The HEAD merge commit's own subject claims acceptance **119/120**;
  that is the commit message, not a measurement taken this pass. Nothing here re-ran the
  acceptance suite. Per-goal standing, `build_succeeds`, and the at-target count are
  **UNVERIFIED** — re-measure before quoting any of them.
- **Escalations: UNVERIFIED** from this pass. Read `.swarm-loop/ESCALATIONS.md`.

### 7 lanes are in flight (OPEN dispatch records, re-derived from `dispatch.jsonl`)

| lane | subject | branch | dispatched | ahead of main (measured) |
|---|---|---|---|---|
| `lane-C16-pthleak` | `T-193-repro` | `task/C16-pthleak` | 2026-09-04T00:27:46 | **0** — nothing committed yet |
| `lane-R-buyer` | `repro-buyer-merchant` | `repro/R-buyer` | 2026-09-04T00:27:46 | **0** — nothing committed yet |
| `lane-T-223` | `T-223` | `task/T-223` | 2026-09-04T00:27:46 | **0** — nothing committed yet |
| `lane-T-229` | `T-229` | `task/T-229` | 2026-09-04T00:27:46 | 1 (`3f28560`) |
| `lane-R-core` | `repro-core-packages` | `repro/R-core` | 2026-09-04T00:32:09 | **0** — nothing committed yet |
| `lane-R-exchange2` | `repro-exchange` | `repro/R-exchange2` | 2026-09-04T00:32:09 | **0** — nothing committed yet |
| `lane-T-082` | `T-082,T-085` | `task/T-082` | 2026-09-04T00:36:25 | **0** — nothing committed yet |

A lane at **0** ahead has committed nothing. It is either still working, or failed/interrupted —
`git -C <worktree> status` before re-dispatching; they were told to commit WIP.

Ahead of `main` with **no** open dispatch — a finished-but-unmerged lane, a stale branch, or a
pending integration branch. Inspect before deleting: `task/C13-esc007` (+2, 141 behind).

### THE ONE MECHANISM THAT MUST NOT BE LOST

**60 open tickets carry the `findings` placeholder gate (`false  # NO GATE YET`) and cannot
be scheduled until each has a real one.** That is the run's largest structural blocker and the
reason the `repro/*` lanes exist. (The graph holds
105 placeholder gates in total; 45 of them are on tickets already closed by verdict.)

A bug ticket's gate has to be RED at the commit before the fix, so the reproduction test must
already exist on `main` and already fail there — but a plainly failing test on `main` takes
`build_succeeds` back to 0. The mechanism that satisfies both, **measured in both directions
before use**:

```python
@pytest.mark.xfail(strict=True, reason="T-NNN: <the defect>; remove this marker with the fix")
def test_<name>():
    ...   # asserts the CORRECT behaviour, so it fails today
```

- normal run -> `1 xfailed`, exit 0, so **the build gate stays green**;
- the ticket's gate is `uv run python -m pytest <file> -q --runxfail -k <name>`
  -> `1 failed` with a test **selected**, which is what `red-check` requires;
- `strict=True` means the fix turns it XPASS, which fails the *normal* run and forces whoever
  fixes it to delete the marker. It cleans itself up.

Do not replace this with a plain failing test, a skip, or a non-strict xfail. A skip is not a
gate and a non-strict xfail never forces the marker's removal.

### Standing cautions measured on this run

- Every python invocation is `.venv/bin/python`, never a bare `python`. Prefix
  `PROXYSHOP_WORKER=0`, and write it as `export PROXYSHOP_WORKER=0 && …` — a command-prefix
  assignment does not survive an `&&` chain.
- `uv sync` intermittently leaves `.venv/lib/python3.12/site-packages` flagged macOS
  `UF_HIDDEN`. CPython >=3.11 skips hidden `.pth` files, so `.pkgroot` never reaches `sys.path`
  in a FRESH interpreter while in-process pytest keeps working — it hides until something shells
  out. Provisioning now runs `chflags -R nohidden` unconditionally and asserts with a
  fresh-interpreter import. Check it before believing any subprocess result.
- `.swarm-loop/acceptance/run.py` does NOT require `PROXYSHOP_WORKER` (it passes
  `--confcutdir`); `red-check` and a bare `pytest` DO.
- Never give a lane `make verify must exit 0` as a done-condition when the gate also grades
  files outside its ownership: `verify.sh` runs under `set -e`, so it dies at the first bad
  stage and the later stages never run at all.
- Two harness defects are filed upstream (`claude-build --issues`): `B09032015-01` (every
  `findings` ticket is undispatchable until a human runs `freeze --amend`, the root cause of the
  60 above) and `B09032120-01` (a scratch-copy sabotage silently runs the REAL repo because
  the venv's `.pth` hardcodes the primary checkout).

<!-- ==================== end RESUME STATE ==================== -->

## (a) OPEN with a REAL gate — dispatchable (71)

These have a `verify` command that can be red-checked. `READY` means every dependency is closed; `BLOCKED on …` names the open dependencies. Sorted by how much each unblocks.

- **T-010** · READY (unblocks 50) `graph-says-closed` — Contracts generate typed models and enforce the dual-path bid boundary
  - deps: T-000 · scope: `packages/contracts/**` · gate: **REAL**
- **T-011** · READY (unblocks 41) `graph-says-closed` — Postgres schemas enforce role isolation and hash-chained ledger
  - deps: T-000 · scope: `db/migrations/**, apps/trust/src/ledger/**, apps/trust/tests/**` · gate: **REAL**
- **T-013** · READY (unblocks 33) `graph-says-closed` — Shopify stub reproduces the exact surface the system uses
  - deps: T-000 · scope: `services/shopify-stub/**` · gate: **REAL**
- **T-014** · READY (unblocks 27) `graph-says-closed` — LLM client with per-role config, cache-first prompts, and test doubles
  - deps: T-000 · scope: `packages/llm/**` · gate: **REAL**
- **T-012** · READY (unblocks 23) `graph-says-closed` — Neo4j attribute-node catalog with vector retrieval through EmbeddingProvider
  - deps: T-000 · scope: `services/ingest/src/graph/**, services/ingest/src/embeddings/**, services/ingest/tests/**` · gate: **REAL**
- **T-111** · READY (unblocks 1) `graph-says-closed` — Provisioning fails loudly when the flat namespaces are dead
  - deps: T-000 · scope: `scripts/bootstrap.sh` · gate: **REAL**
- **T-086** · READY — Onboarding drives a shadow store to its first real bid
  - deps: T-043, T-053 · scope: `e2e/test_onboarding.py, e2e/support/onboarding/**` · gate: **REAL**
- **T-223** · READY `UNDETERMINED-kept-open` — THE PRICE FLOOR IS AN EXACT EQUALITY, SO ANY POSITIVE PRICE CLEARS IT AND A 100.00 PROD…
  - deps: — · scope: `packages/contracts/src/boundary.py:814-819 and apps/exchange/src/auction/collect.py` · gate: **REAL**
- **T-224** · READY — UNAUTHENTICATED HTTP 500 OUT OF THE MIDDLE OF AN AUCTION. `list_price: 0.0` is an accep…
  - deps: — · scope: `apps/exchange/src/auction/routes.py:231 and apps/exchange/src/auction/collect.py` · gate: **REAL**
- **T-229** · READY — THE VALIDATED BODY IS NOT THE ENQUEUED BODY. receive_bid accepts any Mapping and never…
  - deps: — · scope: `packages/store-agent/src/external/door.py` · gate: **REAL**
- **T-230** · READY — receive_bid is documented 'Never raises' at door.py:224 and three inputs make it raise,…
  - deps: — · scope: `packages/store-agent/src/external/door.py` · gate: **REAL**
- **T-231** · READY — freshness_window_seconds = nan or inf DISABLES BOTH FRESHNESS GATES, making the replay…
  - deps: — · scope: `packages/store-agent/src/external/door.py` · gate: **REAL**
- **T-232** · READY — THE DEFAULT nonce_store SILENTLY DISABLES REPLAY PROTECTION. door.py:327 mints a fresh…
  - deps: — · scope: `packages/store-agent/src/external/door.py` · gate: **REAL**
- **T-233** · READY — ELIGIBILITY DEFAULT IS FAIL-OPEN, AND THE ABSENT ARGUMENT IS MORE PERMISSIVE THAN THE E…
  - deps: — · scope: `packages/store-agent/src/external/door.py` · gate: **REAL**
- **T-234** · READY — NonceStore.consume (nonces.py:55-59) is a NON-ATOMIC check-then-set: parse_timestamp(re…
  - deps: — · scope: `packages/store-agent/src/external/nonces.py` · gate: **REAL**
- **T-241** · READY — EXTENDS T-229 FROM 3 FIELDS TO 6, MEASURED. The rung-2 verifier for cycle 15 batch 1 co…
  - deps: — · scope: `packages/store-agent/src/external/door.py` · gate: **REAL**
- **T-020** · BLOCKED on T-012 `graph-says-closed` — Signed fetch adapter ingests a storefront including password-protected dev stores
  - deps: T-000, T-012 · scope: `services/ingest/src/adapters/**, services/ingest/tests/**` · gate: **REAL**
- **T-021** · BLOCKED on T-014, T-020 `graph-says-closed` — Policy pages and marketing claims land in the graph with provenance
  - deps: T-014, T-020 · scope: `services/ingest/src/extraction/**, services/ingest/tests/**, fixtures/pages/**` · gate: **REAL**
- **T-024** · BLOCKED on T-021 — Differential refresh keeps the graph current at field-appropriate cadence
  - deps: T-021, T-023 · scope: `services/ingest/src/scheduler/**, services/ingest/tests/**` · gate: **REAL**
- **T-030** · BLOCKED on T-010, T-013 `graph-says-closed` — Auctions fan out, time out, and always represent every store
  - deps: T-010, T-013 · scope: `apps/exchange/src/auction/**, apps/exchange/src/eligibility/**, apps/exchange/src/orchestration/**, apps/exch…` · gate: **REAL**
- **T-031** · BLOCKED on T-012, T-030 `graph-says-closed` — Candidate retrieval and fit scoring feed the ranker
  - deps: T-012, T-030 · scope: `apps/exchange/src/retrieval/**, apps/exchange/tests/**` · gate: **REAL**
- **T-033** · BLOCKED on T-030 `graph-says-closed` — Accepting an offer produces a validated code and permalink
  - deps: T-030, T-036 · scope: `apps/exchange/src/accept/**, apps/exchange/src/orchestration/**, apps/exchange/tests/**` · gate: **REAL**
- **T-040** · BLOCKED on T-010, T-011 `graph-says-closed` — Tool hooks are the only way facts and discounts enter a bid
  - deps: T-010, T-011 · scope: `packages/store-agent/src/hooks/**, packages/store-agent/tests/**, fixtures/envelopes/**` · gate: **REAL**
- **T-041** · BLOCKED on T-014, T-040 `graph-says-closed` — Advocate runtime bids within walls and defaults deterministically cold
  - deps: T-014, T-040 · scope: `packages/store-agent/src/runtime/**, packages/store-agent/tests/**` · gate: **REAL**
- **T-050** · BLOCKED on T-013, T-010 `graph-says-closed` — Installing the app wires pixel and webhooks against the stub
  - deps: T-013, T-010 · scope: `apps/merchant/**` · gate: **REAL**
- **T-051** · BLOCKED on T-050 — Pixel reports checkout outcomes the collector can join
  - deps: T-050 · scope: `pixel/**, apps/merchant/svc/src/collector/**, apps/merchant/svc/tests/**` · gate: **REAL**
- **T-054** · BLOCKED on T-063 — The dashboard shows the walls and the window
  - deps: T-035, T-043, T-052, T-053, T-063 · scope: `apps/merchant/app/dashboard/**` · gate: **REAL**
- **T-060** · BLOCKED on T-011 `graph-says-closed` — Every event lands once, chained, and replays exactly
  - deps: T-011 · scope: `apps/trust/src/events/**, apps/trust/tests/**` · gate: **REAL**
- **T-061** · BLOCKED on T-051, T-060 `graph-says-closed` — Webhook truth reconciles lossy pixel signals
  - deps: T-051, T-060 · scope: `apps/trust/src/reconcile/**, apps/trust/tests/**` · gate: **REAL**
- **T-062** · BLOCKED on T-060, T-065, T-080 `graph-says-closed` — Trust merges verification and outcome observations against the approved manifest
  - deps: T-060, T-065, T-080 · scope: `apps/trust/src/scoring/**, apps/trust/tests/**` · gate: **REAL**
- **T-063** · BLOCKED on T-062 `graph-says-closed` — Trust events reach the store agent and buyers close the loop
  - deps: T-043, T-062 · scope: `apps/trust/src/feedback/**, apps/trust/tests/**` · gate: **REAL**
- **T-064** · BLOCKED on T-062 `graph-says-closed` — Exchange consumes one snapshot shape for trust and exploration
  - deps: T-062 · scope: `apps/trust/src/snapshot/**, apps/trust/tests/**` · gate: **REAL**
- **T-065** · BLOCKED on T-010, T-021, T-060, T-080 `graph-says-closed` — A golden pitch yields all four verification statuses with evidence
  - deps: T-010, T-021, T-060, T-080 · scope: `packages/verification/**, apps/trust/src/verification/**, apps/trust/tests/**` · gate: **REAL**
- **T-070** · BLOCKED on T-010, T-011 `graph-says-closed` — Buyers authenticate lightly and stores never see who they are
  - deps: T-010, T-011 · scope: `apps/buyer/**` · gate: **REAL**
- **T-080** · BLOCKED on T-013 `graph-says-closed` — The approved manifest and seed generator define ground truth
  - deps: T-013 · scope: `fixtures/manifest/**, fixtures/approval/**, fixtures/seed/**, fixtures/catalog/**, fixtures/personas/**, fixt…` · gate: **REAL**
- **T-082** · BLOCKED on T-061, T-041, T-062 `UNDETERMINED-kept-open` — One scripted run proves the full S1 flow
  - deps: T-061, T-072, T-081, T-032, T-041, T-062 · scope: `e2e/**` · gate: **REAL**
- **T-083** · BLOCKED on T-061 — Both learning loops demonstrably move under seeded outcomes
  - deps: T-034, T-042, T-081, T-061 · scope: `e2e/**` · gate: **REAL**
- **T-084** · BLOCKED on T-062, T-083 — The dishonest store ends below threshold and off the shortlist
  - deps: T-062, T-083 · scope: `e2e/**` · gate: **REAL**
- **T-085** · BLOCKED on T-082 — The starting-slice demo is a runbook anyone on the team can execute
  - deps: T-082 · scope: `docs/demo/starting-slice.md, docs/tests/**` · gate: **REAL**
- **T-087** · BLOCKED on T-084, T-085 — The Shopify and onboarding extension runbook covers the beats off the starting path
  - deps: T-053, T-084, T-085 · scope: `docs/demo/shopify-onboarding-extension.md` · gate: **REAL**
- **T-100** · BLOCKED on T-013 `graph-says-closed` — Shopify stub never emits an off-domain checkout Location
  - deps: T-013 · scope: `services/shopify-stub/**` · gate: **REAL**
- **T-101** · BLOCKED on T-012 `graph-says-closed` — A partial re-embed is never certified as complete
  - deps: T-012 · scope: `services/ingest/**` · gate: **REAL**
- **T-102** · BLOCKED on T-011 `graph-says-closed` — The chain_head guard trigger's DELETE arm is graded by a test
  - deps: T-011 · scope: `apps/trust/**` · gate: **REAL**
- **T-103** · BLOCKED on T-010 `graph-says-closed` — The TypeScript signer refuses integers the wire cannot state
  - deps: T-010 · scope: `packages/contracts/**` · gate: **REAL**
- **T-104** · BLOCKED on T-014 `graph-says-closed` — The prompt cache key cannot collide on separator text
  - deps: T-014 · scope: `packages/llm/**` · gate: **REAL**
- **T-105** · BLOCKED on T-013 `graph-says-closed` — Money arithmetic is asserted absolutely, not against itself
  - deps: T-013 · scope: `services/shopify-stub/**` · gate: **REAL**
- **T-106** · BLOCKED on T-010, T-011 `graph-says-closed` — A conformance gate keeps the two JCS canonicalizers from drifting
  - deps: T-010, T-011 · scope: `e2e/**` · gate: **REAL**
- **T-107** · BLOCKED on T-010 `graph-says-closed` — claim_id is computed with JCS, not json.dumps
  - deps: T-010 · scope: `packages/contracts/**` · gate: **REAL**
- **T-108** · BLOCKED on T-010 `graph-says-closed` — The two envelope gates agree on whitespace
  - deps: T-010 · scope: `packages/contracts/**` · gate: **REAL**
- **T-110** · BLOCKED on T-011 `graph-says-closed` — Role passwords have one source of truth
  - deps: T-011 · scope: `db/init/**` · gate: **REAL**
- **T-112** · BLOCKED on T-110 `graph-says-closed` — The role password has one source of truth on the project's own fresh volume
  - deps: T-110 · scope: `docker-compose.yml, proxyshop_support/**, .env.example` · gate: **REAL**
- **T-113** · BLOCKED on T-103 `graph-says-closed` — The TypeScript signing path refuses unsafe integers by default, not opt-in
  - deps: T-103 · scope: `packages/contracts/**` · gate: **REAL**
- **T-114** · BLOCKED on T-102 `graph-says-closed` — The chain_head trigger test does not block the ENABLE ALWAYS hardening
  - deps: T-102 · scope: `apps/trust/**, db/migrations/**` · gate: **REAL**
- **T-115** · BLOCKED on T-108 `graph-says-closed` — The envelope blank rule is engine-independent again
  - deps: T-108 · scope: `packages/contracts/**` · gate: **REAL**
- **T-116** · BLOCKED on T-101 `graph-says-closed` — A single degenerate product cannot black out vector search
  - deps: T-101 · scope: `services/ingest/**` · gate: **REAL**
- **T-118** · BLOCKED on T-101, T-102, T-104, T-105, T-108, T-110 `graph-says-closed` — Wave-2 residue: eight low-severity findings from the lane verifiers
  - deps: T-101, T-102, T-104, T-105, T-108, T-110 · scope: `packages/llm/**, services/ingest/**, services/shopify-stub/**, packages/contracts/**, apps/trust/**, db/init/…` · gate: **REAL**
- **T-119** · BLOCKED on T-106 `graph-says-closed` — The ledger canonicaliser is one module object under both import spellings
  - deps: T-106 · scope: `apps/trust/**` · gate: **REAL**
- **T-120** · BLOCKED on T-112 `graph-says-closed` — Using PROXYSHOP_ROLE_PASSWORD does not turn the repo gate red
  - deps: T-112 · scope: `proxyshop_support/**` · gate: **REAL**
- **T-121** · BLOCKED on T-119 `graph-says-closed` — The JCS conformance suite's prose matches the code T-119 changed
  - deps: T-119 · scope: `e2e/**` · gate: **REAL**
- **T-122** · BLOCKED on T-119 `graph-says-closed` — Subprocess tests hand the child .pkgroot instead of clobbering PYTHONPATH
  - deps: T-119 · scope: `apps/trust/**, packages/**, services/**, e2e/**, proxyshop_support/**` · gate: **REAL**
- **T-124** · BLOCKED on T-112 `graph-says-closed` — Fresh-volume tests remove the containers and volumes they create
  - deps: T-112 · scope: `apps/trust/**, proxyshop_support/**` · gate: **REAL**
- **T-125** · BLOCKED on T-113 `graph-says-closed` — The signing door and the canonicalizer agree, and the gate watches both
  - deps: T-113 · scope: `packages/contracts/**, e2e/**` · gate: **REAL**
- **T-126** · BLOCKED on T-119 `graph-says-closed` — The ledger spelling binding survives a concurrent first import
  - deps: T-119 · scope: `apps/trust/**` · gate: **REAL**
- **T-127** · BLOCKED on T-114 `graph-says-closed` — The chain_head DELETE arm is graded by property, not by enumeration
  - deps: T-114 · scope: `apps/trust/**, db/migrations/**` · gate: **REAL**
- **T-128** · BLOCKED on T-113 `graph-says-closed` — Guards that cannot refuse anything are removed, not tested tautologically
  - deps: T-113 · scope: `packages/contracts/**` · gate: **REAL**
- **T-129** · BLOCKED on T-113, T-116, T-118 `graph-says-closed` — Wave-3 verification residue: nine findings across four lanes
  - deps: T-113, T-116, T-118 · scope: `packages/contracts/**, services/ingest/**, services/shopify-stub/**, apps/trust/**` · gate: **REAL**
- **T-130** · BLOCKED on T-030, T-040, T-050, T-070, T-080 — Sub-HIGH findings from the feature-wave checkers (backlog sweep, not a wave)
  - deps: T-030, T-040, T-050, T-070, T-080 · scope: `apps/exchange/**, packages/store-agent/**, apps/merchant/**, apps/buyer/**, fixtures/**` · gate: **REAL**
- **T-131** · BLOCKED on T-080 `graph-says-closed` — The golden answer key is graded weakly, and the dishonest store's flagship lie is not g…
  - deps: T-080 · scope: `fixtures/**` · gate: **REAL**
- **T-132** · BLOCKED on T-070 `graph-says-closed` — Rotating pseudonyms are trivially re-linkable, so T-070's central guarantee does not ho…
  - deps: T-070 · scope: `apps/buyer/**` · gate: **REAL**
- **T-133** · BLOCKED on T-070 `graph-says-closed` — Buyer residue: shared session state, unwired publish half, and unbounded stores
  - deps: T-070 · scope: `apps/buyer/**, packages/**` · gate: **REAL**
- **T-134** · BLOCKED on T-050 — Merchant residue: a scope guard that shreds strings, a token in repr, and six inbox fin…
  - deps: T-050 · scope: `apps/merchant/**` · gate: **REAL**

## (b) OPEN with the PLACEHOLDER gate — blocked on a reproduction test (53)

`verify` is literally `false  # NO GATE YET`. None of these can be dispatched until someone writes a reproduction test that is RED before the fix (use the xfail-strict recipe above) and amends the graph via `freeze --amend`. `red-check` will refuse them as-is.

- **T-140** · READY `graph-says-closed` — AccountDirectory has exactly one implementation and no production populator. build_auth…
  - deps: — · scope: `apps/buyer/svc/src/auth/magic_link.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-141** · READY `graph-says-closed` — The magic-link token is delivered to the default no-op `_drop` (line 166) in every depl…
  - deps: — · scope: `apps/buyer/svc/src/auth/magic_link.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-142** · READY `graph-says-closed` — publish_profile — the only writer of app.buyer_accounts, described as 'the store-visibl…
  - deps: — · scope: `apps/buyer/svc/src/profile/__init__.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-148** · READY `graph-says-closed` — `configure_auctions` — the only way to give the exchange a real solicitor, a real eligi…
  - deps: — · scope: `apps/exchange/src/auction/routes.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-150** · READY `graph-says-closed` — The ledger writer has zero producers. Nothing in the repository writes an event into it…
  - deps: — · scope: `apps/exchange/src/auction/ledger.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-154** · READY — EVENTS_ROLE = "app", and its comment asserts app is 'the role the writer connects as...…
  - deps: — · scope: `apps/trust/tests/_fixtures_events.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-156** · READY — offer.total_price is never reconciled against unit_price: an offer stating unit_price=8…
  - deps: — · scope: `packages/store-agent/src/hooks/provenance.py (_price_reconciliation_refusal)` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-157** · READY — An off-domain permalink from a delegating merchant client leaves a LIVE single-use disc…
  - deps: — · scope: `apps/exchange/src/checkout/provider.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-158** · READY — The one-accept-per-auction guard is only as durable as the record handed in. accept() s…
  - deps: — · scope: `apps/exchange/src/accept/offer.py (module docstring) + AuctionStateMachine` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-159** · READY — SYSTEMIC GATE-CREDIBILITY DEFECT: tests that swallow a missing subject with `except Imp…
  - deps: — · scope: `apps/exchange/tests/test_checkout_provider.py:406 (and repo-wide)` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-161** · READY — A provenance block nested inside a hook-provenanced claim's opaque `value` is not walke…
  - deps: — · scope: `packages/contracts/src/boundary.py:157 (_source_verdict)` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-162** · READY — The boundary checks provenance SOURCE but never discount AUTHORISATION. make_bid(claims…
  - deps: — · scope: `packages/contracts/src/boundary.py (validate_bid) — external path` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-163** · READY — T-133 item (c): SessionStore.open() validates only that the subject carries the 'psn-'…
  - deps: — · scope: `apps/buyer/svc/src/auth/sessions.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-164** · READY — build_buckets() and anonymise_cohort() are public and run NO identity-leak check; only…
  - deps: — · scope: `apps/buyer/svc/src/profile/__init__.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-165** · READY — No rate limiter on the unauthenticated magic-link endpoint, and the session/pending-lin…
  - deps: — · scope: `apps/buyer/svc/src/auth/routes.py (POST /buyer/auth/magic-link) + routes.py:57 ProcessLocalStateUnsafe` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-166** · READY — test_replay_with_snapshots_names_the_missing_scorer branches on `if response.status_cod…
  - deps: — · scope: `apps/trust/tests/test_events.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-167** · READY — Four BYTE-IDENTICAL copies of the elected-primary module-binding shim (md5 56d63d33...)…
  - deps: — · scope: `apps/trust/src/{scoring,reconcile,feedback,snapshot}/_binding.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-169** · READY — T-033's registered-domain guard is DECORATIVE in the deployed configuration. `_platform…
  - deps: — · scope: `apps/exchange/src/accept/offer.py:94,:328 + apps/exchange/src/accept/__init__.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-170** · READY — T-033's objective opens 'Accept endpoint resolves a CheckoutProvider by CHECKOUT_MODE..…
  - deps: — · scope: `apps/exchange/src/accept/ (no routes.py) vs apps/exchange/src/main.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-172** · READY — T-109's per-service inference has an 8-item hole and every item in it is a Postgres-onl…
  - deps: — · scope: `proxyshop_support/service_markers.py:78-85 (whole-stack fallback)` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-175** · READY — Neither the new price-reconciliation wall nor the pre-existing floor wall looks at Offe…
  - deps: — · scope: `packages/store-agent/src/hooks/provenance.py:830-834 vs DESIGN.md` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-181** · READY — T-151's DSN ORDER RESTS ON A SINGLE ASSERTION IN A SINGLE TEST OF 327. Sabotage S3a (re…
  - deps: — · scope: `apps/trust/tests/test_events_hardening.py (DSN precedence coverage)` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-193** · READY — T-065 CRASHES IN THE DEPLOYED IMAGE. apps/trust/Dockerfile does not COPY packages/verif…
  - deps: — · scope: `apps/trust/Dockerfile (COPY set) vs packages/verification/` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-194** · READY — THE TWO DOORS DO NOT RUN THE SAME SCHEMA CHECK — 21 measured ok-divergences. TypeScript…
  - deps: — · scope: `packages/contracts/src/ts/schemas.ts:63-65 (ajv + ajv-formats) vs packages/contracts/src/boundary.py:353-365…` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-197** · READY — Identity fragments shorter than 4 characters are never tracked ANYWHERE in the leak bac…
  - deps: — · scope: `apps/buyer/svc/src/profile/__init__.py:436 (_MIN_LEAKABLE = 4)` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-198** · READY — Two residual identity channels the T-189 fix deliberately stopped short of. (a) A numbe…
  - deps: — · scope: `apps/buyer/svc/src/profile/__init__.py:918 (_identity_sources) and` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-199** · READY — The bucket vocabulary holds every CATEGORY_TAXONOMY label out of the leak haystack on t…
  - deps: — · scope: `apps/buyer/svc/src/profile/__init__.py:1038 (_BUCKET_VOCABULARY['category_affinity'])` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-202** · READY — T-157 IS FIXED AT THE PORT AND STILL OPEN END TO END. The checkout port now carries the…
  - deps: — · scope: `apps/exchange/src/accept/offer.py:390 (bare `except Exception`) vs the new OrphanedCheckoutCode.orphan` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-203** · READY — BINDING REQUIREMENT ON A SCHEDULED TICKET, recorded the way T-041 carried T-152's. The…
  - deps: — · scope: `apps/merchant/svc/src/codes/ (T-052) — binding requirement, not a defect` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-204** · READY — REFUSAL REASONS ARE UNENUMERATED AND NOTHING ASSERTS ON THEM. accept() formats type(exc…
  - deps: — · scope: `packages/contracts/openapi/exchange.openapi.json:169 (denial_reason) + apps/exchange/src/accept/offer.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-207** · READY — The engine's gloss on the weight table CONTRADICTS THE APPROVED MANIFEST on what mismat…
  - deps: — · scope: `apps/trust/src/scoring/engine.py (weight-table comment) vs fixtures/manifest.json observation_weights` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-208** · READY — There is no published weight for a buyer complaint that is NOT corroborated by a return…
  - deps: — · scope: `fixtures/manifest.json observation_weights — no weight for an uncorroborated complaint` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-209** · READY — THE CONTRACTS BOUNDARY WAS TIGHTENED AND THE STORE-AGENT'S OWN AUDITOR WAS NOT. T-195's…
  - deps: — · scope: `packages/store-agent/src/hooks/provenance.py:129,:496-507 (commitments walk, no pydantic gate)` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-210** · READY — TWO CORRECTIONS, ONE OF THEM TO MY OWN ASSERTION. (1) I claimed the chflags window coul…
  - deps: — · scope: `ORCHESTRATION — measuring while the swarm runs (supersedes my T-174 rationale)` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-218** · READY — T-209 CONFIRMED BUT UNDERSTATED, and the cause is generic rather than specific to commi…
  - deps: — · scope: `packages/store-agent/src/hooks/provenance.py:523 (`if node is None: return`) — T-209 is one of a family of at…` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-220** · READY — The cycle-detection replacement for MAX_SWEEP_DEPTH is correct, but nothing in the suit…
  - deps: — · scope: `packages/store-agent/tests/test_price_reconciliation.py (depth parametrization) — cycle detection has an invi…` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-221** · READY — THE K-ANONYMITY FLOOR T-138 DELIVERED IS SWITCHED OFF BY DEFAULT, so the re-linkability…
  - deps: — · scope: `apps/buyer/svc/src/profile/__init__.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-222** · READY — A DEFECT WAS CONVERTED INTO DOCUMENTATION AND THEN PINNED BY A PASSING TEST, so the sui…
  - deps: — · scope: `apps/exchange/src/accept/offer.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-225** · READY — A TEST THAT ASSERTS NOTHING AND REPORTS GREEN, in three copies. `test_scope_directories…
  - deps: — · scope: `apps/seller-reference/tests/test_scaffold_smoke.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-235** · READY — ONE LEDGER KIND, TWO BODIES: the successful checkout path emits a code_created event ca…
  - deps: — · scope: `apps/exchange/src/checkout/provider.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-236** · READY — THE INGESTION PIPELINE DOES NOT EXIST AS A RUNNING THING. No production code constructs…
  - deps: — · scope: `services/ingest/src/main.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-237** · READY — THE S2 CHAIN HAS NO PRODUCT CODE CLOSING IT: nothing turns a sub-threshold trust score…
  - deps: — · scope: `apps/trust/src/snapshot/builder.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-238** · READY — BARE urlsplit ON ATTACKER-CONTROLLED REDIRECT TARGETS. netguard.py:398 and transport.py…
  - deps: — · scope: `services/ingest/src/adapters/netguard.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-239** · READY — ENVELOPE VERSION HISTORY IS PROCESS-LOCAL AND DIES WITH THE PROCESS. EnvelopeVersions k…
  - deps: — · scope: `apps/merchant/svc/src/envelope/store.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-240** · READY — THE PINNED MERCHANT CONTRACT HAS NOWHERE TO PUT AN ENVELOPE APPROVAL ARTIFACT, AND ITS…
  - deps: — · scope: `packages/contracts/openapi/merchant.openapi.json` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-242** · READY — THE SIMULATION VALIDATES ONLY HALF ITS OWN LEDGER, AND THE HALF IT SKIPS CONTAINS THE E…
  - deps: — · scope: `services/sim/src/runner.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-243** · READY — THE TWO-LEDGERS-UNDER-TWO-SPELLINGS HOLE IS PRESENT IN MERCHANT, AND T-071'S FIX DID NO…
  - deps: — · scope: `apps/merchant/svc/src/envelope/store.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-244** · READY — MEASUREMENT CREDIBILITY: receive_bid has 28 callers and ZERO of them are production. sw…
  - deps: — · scope: `packages/store-agent/src/external/door.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-245** · READY — T-023'S NEW SHARED MAPPING HAS NEVER REACHED A GRAPH, AND E2'S GREEN OVER IT IS STRUCTU…
  - deps: — · scope: `services/ingest/src/adapters/mapping.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-246** · READY — NO PRODUCTION CONSUMER READS is_live, SO NOTHING PROVES A SHADOW OR KILLED STORE ACTUAL…
  - deps: — · scope: `apps/merchant/svc/src/envelope/model.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-247** · READY — ORDER-DEPENDENT GLOBAL LEAK IN THE MERCHANT WEBHOOK SINK. webhooks.py:418 boots _sink =…
  - deps: — · scope: `apps/merchant/svc/src/install/webhooks.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-248** · READY — EnvelopeVersions.record() ACCEPTS A CALLER-ASSERTED activation:'active' WITH NO APPROVA…
  - deps: — · scope: `apps/merchant/svc/src/envelope/store.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-249** · READY — T-023 BEHAVIOURAL DRIFT THAT NO TEST COVERS, and the 'moved VERBATIM' claim is PARTIAL.…
  - deps: — · scope: `services/ingest/src/adapters/mapping.py` · gate: **PLACEHOLDER `false # NO GATE YET`**

## (c) OPEN under a PROTECTED path — orchestrator-owned escalations, NOT worker tickets (10)

Their scope names one of `.swarm-loop/**`, `tickets.json`, `conftest.py`, `scripts/verify.sh`, `pyproject.toml` (plus `Makefile` and `scripts/check_verify_contracts.py`, which `state.json` also protects). A worker lane cannot land these — a hook blocks the write. Route them through `escalate --kind harness`. Where a protected file is named only as the *mechanism* of a defect whose fix lives elsewhere (T-205 names `conftest.py` as the import path; T-171 names `pyproject.toml:71` alongside a normal source file), a worker may still own the non-protected half — split the ticket rather than assuming either answer.

- **T-109** · READY (unblocks 1) `graph-says-closed` — A datastore blip cannot silently empty the security gate
  - deps: T-000 · scope: `conftest.py, proxyshop_support/**` · gate: **REAL**
- **T-117** · BLOCKED on T-109 `graph-says-closed` — [BLOCKED: protected path] The per-ticket gate runs the tests that grade its own accepta…
  - deps: T-109 · scope: `scripts/verify.sh, conftest.py, proxyshop_support/**` · gate: **REAL**
- **T-123** · BLOCKED on T-111 `graph-says-closed` — The .pkgroot namespaces survive a pytest run (root cause: site-packages itself is flagg…
  - deps: T-111 · scope: `scripts/bootstrap.sh, conftest.py, proxyshop_support/**` · gate: **REAL**
- **T-160** · READY — These three tickets' recorded `verify` commands pass IDENTICALLY with and without their…
  - deps: — · scope: `tickets.json (T-109, T-111, T-123 verify fields)` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-171** · READY — The D37 Neo4j lock is MACHINE-GLOBAL (/private/tmp/proxyshop-neo4j.lock) and PROXYSHOP_…
  - deps: — · scope: `proxyshop_support/neo4j_lock.py:79,119-131 + pyproject.toml:71 + conftest.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-192** · READY — THE 26/26 THAT MOVED E6 TO TARGET IS A WEAKER INSTRUMENT THAN THE LANE'S OWN UNIT SUITE…
  - deps: — · scope: `.swarm-loop/acceptance/test_e6_trust.py (the E6 metric as an instrument)` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-205** · READY — SECOND LATENT except-ImportError VACUITY, found by the AST sweep T-159 asked for. The w…
  - deps: — · scope: `services/shopify-stub/tests/test_stub_contract.py:40 via conftest.py` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-219** · READY — NOTHING CONSUMES THE SELECTION COUNT, so T-117's defect is closed for the `docker` mark…
  - deps: — · scope: `scripts/verify.sh SELECTION line + .swarm-loop/goals.json build_succeeds — T-117 acceptance 2 is only literal…` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-226** · READY — EVERY MEMBER'S tests/ DIRECTORY IS OUTSIDE THE TYPE GATE, so type errors in tests never…
  - deps: — · scope: `pyproject.toml` · gate: **PLACEHOLDER `false # NO GATE YET`**
- **T-227** · READY — THE FROZEN TEST AND THE PRODUCT CAN SELECT DIFFERENT MANIFEST DOCUMENTS, so the suite w…
  - deps: — · scope: `.swarm-loop/acceptance/test_e4_store_agent.py::_load_fixture_manifest vs apps/seller-reference/src/personas/_…` · gate: **PLACEHOLDER `false # NO GATE YET`**

## Scheduling constraints — blast radius among the 134 open tickets (34 contested paths)

Every path below is declared in the `scope` of two or more OPEN tickets. Those tickets may never
be in flight together; `check-wave` refuses them mechanically. Listed so the next frontier is
built knowing it. Derived from the open set only — closed tickets no longer constrain anything.

- `packages/contracts/**` — 10: T-010, T-103, T-107, T-108, T-113, T-115, T-118, T-125, T-128, T-129
- `apps/trust/**` — 9: T-102, T-114, T-118, T-119, T-122, T-124, T-126, T-127, T-129
- `apps/trust/tests/**` — 7: T-011, T-060, T-061, T-062, T-063, T-064, T-065
- `e2e/**` — 7: T-082, T-083, T-084, T-106, T-121, T-122, T-125
- `packages/store-agent/src/external/door.py` — 7: T-229, T-230, T-231, T-232, T-233, T-241, T-244
- `proxyshop_support/**` — 7: T-109, T-112, T-117, T-120, T-122, T-123, T-124
- `apps/buyer/svc/src/profile/__init__.py` — 6: T-142, T-164, T-197, T-198, T-199, T-221
- `apps/exchange/src/accept/offer.py` — 5: T-158, T-169, T-202, T-204, T-222
- `conftest.py` — 5: T-109, T-117, T-123, T-171, T-205
- `services/shopify-stub/**` — 5: T-013, T-100, T-105, T-118, T-129
- `apps/buyer/**` — 4: T-070, T-130, T-132, T-133
- `packages/contracts/src/boundary.py` — 4: T-161, T-162, T-194, T-223
- `packages/store-agent/src/hooks/provenance.py` — 4: T-156, T-175, T-209, T-218
- `services/ingest/**` — 4: T-101, T-116, T-118, T-129
- `services/ingest/tests/**` — 4: T-012, T-020, T-021, T-024
- `apps/exchange/tests/**` — 3: T-030, T-031, T-033
- `apps/merchant/**` — 3: T-050, T-130, T-134
- `apps/merchant/svc/src/envelope/store.py` — 3: T-239, T-243, T-248
- `db/migrations/**` — 3: T-011, T-114, T-127
- `fixtures/manifest.json` — 3: T-080, T-207, T-208
- `packages/llm/**` — 3: T-014, T-104, T-118
- `apps/buyer/svc/src/auth/magic_link.py` — 2: T-140, T-141
- `apps/exchange/src/auction/collect.py` — 2: T-223, T-224
- `apps/exchange/src/auction/routes.py` — 2: T-148, T-224
- `apps/exchange/src/checkout/provider.py` — 2: T-157, T-235
- `apps/exchange/src/orchestration/**` — 2: T-030, T-033
- `db/init/**` — 2: T-110, T-118
- `fixtures/**` — 2: T-130, T-131
- `packages/**` — 2: T-122, T-133
- `packages/store-agent/tests/**` — 2: T-040, T-041
- `pyproject.toml` — 2: T-171, T-226
- `scripts/bootstrap.sh` — 2: T-111, T-123
- `scripts/verify.sh` — 2: T-117, T-219
- `services/ingest/src/adapters/mapping.py` — 2: T-245, T-249

---

_Regenerate with the `frontier` command quoted above; do not hand-edit. Closed tickets leave this file by design._
