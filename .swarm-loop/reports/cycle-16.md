# Cycle 16 — the test written to catch the defect reported it already fixed

HEAD at measurement `9ef97f8` · acceptance **119/120 (99.17%)**, from 111/120 (92.50%) · **10/12 metrics at
target** · harness hermeticity **PARTIAL** (frozen bytes intact, amendment 15) · codebase stall 0/3 ·
goal-progress 0/3 · tracked files 663 → 713.

**The measurement came first, and it closed an epoch that had already happened.** The session inherited a RESUME
STATE block written at the cycle-15/16 boundary; by the time it was read, thirty commits — the T-022, T-052,
T-072 and T-073 merges, amendments 12 and 14, and four reproduction lanes — had already landed on `main` with no
measurement epoch closed behind them. So the first act was `measure --cycle 16` at `9ef97f8`, against work that
was already in the tree. Everything this report describes below the metrics table landed **after** that
measurement, in the ten commits `9ef97f8..a02067d`, and **is not reflected in a single number on the board.**

Two metrics remain off target, and **both are the same one failing test.** `e8_proofs_passing` is 7 of 8 and
`acceptance_pass_rate` is 119 of 120 because
`test_demo_runbook_has_the_required_sections_and_its_make_targets_exist` fails on
`.swarm-loop/acceptance/test_e8_proofs.py:636` — `docs/demo/` exists and contains no markdown. Ten of twelve
metrics are `at_target`; nothing is `regressing` and nothing is `stalled`, holding cycle 15's structural result
for a second epoch.

## Escalations: nothing open

**`OPEN (0)`. Nothing is waiting on Hank.** `.swarm-loop/ESCALATIONS.md` shows fourteen resolved and none open;
no escalation was filed this epoch and none was needed. Every decision below was taken under standing approval —
the ESC-013 amendment class (`tickets.json` verify fields only) and the ESC-014 ruling (leave the frozen suite
alone), both granted by Hank in cycle 15 and neither re-asked.

## What merged

**Before the measurement**, `8ba6c36..9ef97f8`, thirty commits: **T-022** (entity resolution — GTIN identity,
threshold agreement, `SAME_AS` edges), **T-052** (discount-code minting, `combinesWith` pre-check, single-use
redemption), **T-072** (shortlist provenance labels and the exchange-owned checkout handoff), **T-073** (the
feedback prompt and the feedback ledger event, which took **E7 to target** and acceptance to 119/120), plus
**amendment 12** (real red gates for T-223 and T-224) and **amendment 14** (eighty gates that could not run in a
clean shell, all eighty repaired). Those four merges are what the metrics table measures.

**After the measurement**, `9ef97f8..a02067d`, ten commits:

- `132b3d1`, `7813024` — eight reproduction tests for open trust tickets (T-154, T-166, T-167, T-181, T-193,
  T-207, T-212, T-237), each `xfail(strict=True)` so `build_succeeds` stays 1 while every ticket gains a gate
  that is genuinely red.
- `55492fb`, `23203df`, `635d442` — the T-193 probe, repaired twice, below.
- `b5ae9b7`, `82ca8bc`, `c994bb3` — **T-223 (CRITICAL)** and **T-224 (HIGH)**.
- `1d8cf61` — **amendment 15**, real red gates for seven trust tickets.
- `a02067d` — the graph mint, twelve tickets, freeze-log records **#23** and **#24**.

## Metrics

Quoted verbatim from `.swarm-loop/analysis/cycle-16.md`. Measured at `9ef97f8`.

| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |
|---|---|---|---|---|---|---|---|
| e8_proofs_passing | converging_on_track | cycle 16 | 7 | 8 | 1 | -0.551471 | 0 |
| acceptance_pass_rate | converging_on_track | cycle 16 | 99.17 | 100 | 0.83 | -6.05618 | 0 |
| acceptance_collected | at_target | cycle 16 | 120 | 120 | 0 | – | – |
| build_succeeds | at_target | cycle 16 | 1 | 1 | 0 | – | – |
| spec_criteria_passing | at_target | cycle 16 | 8 | 8 | 0 | – | – |
| e1_foundation_passing | at_target | cycle 16 | 10 | 10 | 0 | – | – |
| e2_ingestion_passing | at_target | cycle 16 | 8 | 8 | 0 | – | – |
| e3_exchange_passing | at_target | cycle 16 | 21 | 21 | 0 | – | – |
| e4_store_agent_passing | at_target | cycle 16 | 20 | 20 | 0 | – | – |
| e5_merchant_passing | at_target | cycle 16 | 10 | 10 | 0 | – | – |
| e6_trust_passing | at_target | cycle 16 | 26 | 26 | 0 | – | – |
| e7_buyer_passing | at_target | cycle 16 | 9 | 9 | 0 | – | – |

**Ten at target**, up from seven; E2, E5 and E7 all closed their remaining error this epoch. The analysis carries
its two standing banners and both still bind. **Selection tracking is unavailable for all twelve metrics** — the
frozen wrappers print a bare number, which is the contract every metric depends on, so the selected/deselected
columns are written empty and *"no selection regression" must be read as NOT MEASURED, never as clean.* And
**the goalposts have now moved fifteen times** — fourteen at the moment the analysis was generated, plus
amendment 15 an hour later — so a slope fitted across seventeen points is fitted against a moving target:
steering signal, not measurement. The analysis again recomputed the stored `error` column for five metrics whose
recorded history disagreed with the target now in force.

### What `e2_ingestion_passing` and `e5_merchant_passing` do NOT mean — ESC-014, standing

Required in every remaining report by Hank's cycle-15 ruling, and now more load-bearing than it was, because
**both metrics read 8/8 and 10/10 — at target — for the first time.**

- **The E2 seam test grades shape only.** `test_both_catalog_adapters_satisfy_one_interface` never calls
  `fetch_catalog` and never calls `to_upserts`; the lane that satisfied it said unprompted that two stub bodies
  would satisfy it in full.
- **The three E5 envelope tests have a narrow hole in the negative case.** `test_e5_merchant.py:741-747` wraps
  the altered envelope's digest in `except Exception: other_digest = digest[::-1]`, so an implementation that
  returns a constant for real `Envelope` objects but raises on a plain mapping degrades into that fallback and
  passes all three.

**`e2_ingestion_passing: 8/8` is not evidence that ingestion runs.** T-236 already says no production code
constructs any `CatalogAdapter`; **T-260**, minted this epoch, says the same of the exchange's entire retrieval
package. A metric at target and a capability that works are different claims, and this epoch minted two more
tickets whose whole content is the gap between them.

## The epoch's largest finding — a reproduction test that passed while its defect was live

`test_the_trust_image_copy_set_can_resolve_the_claim_verifier` builds a container-shaped tree from
`apps/trust/Dockerfile`'s own `COPY` lines and probes it in a subprocess. It reported the claim verifier
**resolves**. It does not. `apps/trust/Dockerfile:52-60` still has no `COPY packages/verification`, and its
`.pkgroot` RUN at `:65-67` links only `contracts` and `trust`, so the trust shim raises `ModuleNotFoundError`
inside the real image.

**The probe was importing the live repo.** `.venv/lib/python3.12/site-packages/_proxyshop.pth` is two absolute
lines — the checkout root and its `.pkgroot` — and `site` processes `.pth` files at interpreter startup, so both
land on `sys.path` regardless of `PYTHONPATH` and of cwd. Measured from an empty cwd with
`PYTHONPATH=/nonexistent`: `claim_verification` resolves to `<checkout>/.pkgroot/claim_verification/__init__.py`.
Repaired with `-S`. The two alternatives were measured and rejected rather than reasoned about: `-E -S` also
discards `PYTHONPATH` and fails with `No module named 'trust'`, the wrong red; and **`-I` does not work at all** —
it implies `-E -s`, and `-s` suppresses only the *user* site directory, leaving the venv's `.pth` firing, so `-I`
still resolves against the unfixed Dockerfile. Independently re-measured for this report: plain and `-I` both
resolve to the checkout, `-S` raises `ModuleNotFoundError`.

**A second defect, in the opposite direction, in the same probe.** The `.pkgroot` symlink set was a hardcoded
`("contracts", "trust")` tuple, so a **correct** T-193 fix — which needs a `COPY` *and* a matching `ln -s` — would
still have measured red. The gate could not have seen its own fix. The links are now parsed from the Dockerfile's
own RUN (`test_repro_open_tickets.py:298-307`), and a positive control demonstrated both directions against a
patched scratch Dockerfile.

Repo-wide sweep: **23 files / 30 interpreter launch sites, exactly one affected.** Re-counted independently at
22 files / 29 sites under a stricter inclusion rule; the magnitude is right and the exact pair depends on whether
shell launchers count. The "exactly one" holds — four other launch sites already passed `-S`, and the rest launch
subprocesses that *want* the checkout on the path.

That lane also found this file was **the repo's only `ruff` failure**, so `verify.sh lint` exited non-zero at
`scripts/verify.sh:142` for every lane running against that tree, before ever reaching import-linter, the
banned-reset gate or eslint. Re-measured now: `ruff check .` → `All checks passed!`, `ruff format --check .` →
`459 files already formatted`, both exit 0.

## T-223 (CRITICAL) and T-224 (HIGH) — closed at the door, still open in the library

**T-223.** The price floor tested `priced == 0.0` — a relation with exactly one satisfying value — while every
other relation in the same walk was an inequality against a caller-supplied depth. `max_discount_pct` arrives on
an unauthenticated request body, so `max_discount_pct: 100` made all of them true at once and `0.001` cleared the
floor on a 100.00 product, returning **HTTP 201 with `fallback: false`**. On an **uncapped** row it was worse:
`_is_judged` returned `False`, the walk never ran at all, and `unit_price: 1e-09` came back as a rankable bid.

The repair at `apps/exchange/src/auction/collect.py:419-434` is a **threshold** — `max(list_price × 0.1%, one
minor currency unit)` — judged on every row regardless of the caller's cap, with the absolute half dropped where
the roster itself lists the product under a cent so a genuinely sub-cent catalog is not walled off. The 0.1% is
not arbitrary: `test_auction_price_wall.py:183` pins **1.00 admitted on a 100.00 uncapped row**, which puts the
ceiling on the constant at 1% and takes it with an order of magnitude to spare.

**T-224.** The unreadable-price fallback ran only `if listed > 0`, so the whole protection was conditional on a
number the caller supplies. `list_price: 0.0` was an accepted roster value under `Field(ge=0.0)`, and on such a
row `float("cheap")` raised out of `collect_bids` as an **unauthenticated HTTP 500**; the same row with nobody
answering minted a free item. Closed twice, deliberately, because a repair living only in a request model is one
the library's other callers do not get: `routes.py:152` is now `Field(gt=0.0)`, and `_price_is_unreadable`
(`collect.py:519-521`) judges a price `float()` would raise on **with no roster term in the question at all**.

**Adversarial revert, timed from outside**: 15 failed / 5 passed and 9 failed / 1 passed, both under half a
second, both red, neither hung; files restored byte-identically. **Sibling sweep**: 5 equality-as-floor sites and
75 numeric-coercion call sites examined across `apps/exchange/src`; 3 real defects found, two of which no ticket
had noticed — `_list_price_bid` read `float(entry.get("list_price", 0.0))` and the collector read
`int(rostered.get("tier", 1))`, so a `"cheap"` list price on a silent store and a `"one"` tier on any store raised
from the fallback path.

### A packet error of mine, recorded against the orchestrator and not the lane

**T-223's own `scope` field reads `["packages/contracts/src/boundary.py:814-819 and
apps/exchange/src/auction/collect.py"]` — it names the boundary first. The task packet I wrote granted only
`apps/exchange/src/**`.** The lane obeyed the packet, fixed what it owned, and reported the discrepancy in its
own commit message rather than silently overreaching — which is the correct behaviour and the only reason this
is visible at all.

The consequence is exact and is recorded here rather than smoothed over: **the attack is dead at the exchange
door and the exact equality survives verbatim in the shared boundary.** `git diff b5ae9b7~1 HEAD --
packages/contracts/src/boundary.py` is empty; `contracts.boundary.price_reasons` called directly still returns
`[]` for `0.001` under a cap of 100, and the TypeScript peer door mirrors the same arithmetic. Minted as
**T-250 (HIGH)**.

## The closure sweep, and two things it cannot say

All **57** READY tickets carrying a real gate were red-checked against `main`: **15 genuinely red**, **29 exiting
0 with hundreds of tests selected** — work already built, missing only a closure record — and **13 WEAK**. Three
read-only auditors were then dispatched to settle the 29 **from the code** rather than from the gate.
**Seventeen came back BUILT**, each with an implementation `file:line` and the node id of the test that grades
it, and each recorded as an accepted verdict in `dispatch.jsonl`: T-030, T-031, T-033, T-040, T-041, T-060,
T-061, T-062, T-063, T-080, T-102, T-104, T-109, T-110, T-111, T-112, T-114. Three of those verdicts carry their
own caveat in the ledger (T-031's Neo4j half, T-060's 23-of-87 docker gating, T-063's fixture-graded acceptance),
and **T-064 was deliberately left OPEN** by the audit rather than closed.

**Two caveats that must not be dropped from this record.**

**Docker is not running** (`docker info` exits 1), so `conftest.py:106-152` converts every `@pytest.mark.docker`
item into a skip. Measured: `services/ingest/tests/test_graph.py` collects **184** tests, **107** docker-marked,
and runs **76 passed / 108 skipped**; `apps/trust/tests` collects **534**, **125** docker-marked, and runs **400
passed / 127 skipped**. Those suites exit 0 with a large fraction never executed, and **any verdict resting on
them is code-reading rather than observed execution** — which is why two of the seventeen say exactly that in
their own note. A precision the mechanism deserves: the conftest probes TCP reachability of the compose datastore
ports, not the daemon; the daemon being down is upstream of the thing actually measured.

**Six of the thirteen WEAK verdicts say nothing about their tickets.** They are the `npx vitest` gates — T-050,
T-051, T-054, T-070, T-103, T-113. red-check builds a throwaway worktree with no `node_modules`, and `npx` has no
self-provisioning equivalent to `uv run`, so the gate cannot run at all and stamps WEAK **on the harness rather
than on the work.** Measured directly in the provisioned main checkout, on T-103's and T-113's literal gate
string: `npx vitest run packages/contracts` → **7 files, 683 tests passed, exit 0, 1.45s** — while that same
command reads WEAK in the sweep. Two independent watchdog sessions reproduced this separately. **A WEAK stamp on
a vitest gate is not evidence about the ticket and must never be read as one.**

Six of the remaining seven are the older, honest kind: T-024, T-082, T-083, T-084, T-086 and T-087 name a test
file that does not exist yet, which is what an unbuilt ticket's gate is supposed to look like.

## Ledger regeneration — and a `status` field that disagrees in both directions

`backlog.md` had drifted from the graph and was regenerated from `tickets.json` at `main = 635d442`: **197
tickets, 63 closed, 134 open.** The 134 open partition into **71 dispatchable** (a real gate), **53
placeholder-gated** (`false  # NO GATE YET`, blocked on a reproduction test) and **10 orchestrator-owned** under a
protected path — 71 + 53 + 10 = 134 exactly, counted from the ticket bullets themselves. The summary table's
coarser split (74 real gate / 60 placeholder / 10 protected) is the same population before the protected ten are
drawn out of both buckets, not a second count that disagrees. **The file is already stale again**: the mints
below added tickets it does not list.

The 63 closed are **evidence-derived, not read off the `status` field**, which is why they do not match the
field's own 96/66 and should not be expected to.

**The graph's own `status` field disagrees with the evidence in both directions, and both counts reproduce
exactly.** At the backlog's generation state, **59** tickets say `"closed"` with no accepted verdict in
`dispatch.jsonl` and no merged branch to point at — including the foundation tickets **T-010…T-014**, which is
why they surfaced as READY unblocking 50/41/33/27/23 downstream tickets, and for which no `task/T-01*` branch has
ever existed. In the other direction, **22** say `"open"` while carrying an accepted verdict. `frontier` ignores
that field entirely and derives closure from positive evidence, which is why the count above is trustworthy and
the field is not. Resolved per **bias-to-open**: the disputed `"closed"` entries stay open, tagged, pending the
closure audit. Closure is a thing you prove, not a thing a ticket asserts about itself.

## Amendment 15 — seven undispatchable tickets get real red gates

Seven trust tickets — **T-154, T-166, T-167, T-193, T-207, T-212, T-237** — carried the byte-identical
placeholder `false  # NO GATE YET` that `findings` writes into every ticket it mints, and red-check refuses a
ticket with no real gate, so **seven real defects sat unschedulable**. Each now points at its `xfail(strict=True)`
reproduction in `apps/trust/tests/test_repro_open_tickets.py` via `--runxfail`, in the `export PROXYSHOP_WORKER=0
&& uv run python -m pytest` form amendments 14 and 9 established. The diff was exactly **7 insertions / 7
deletions with zero changed lines that are not a `verify` line**, measured rather than asserted. Both directions
were measured at `main` before amending: the file's normal run is `1 passed, 7 xfailed` exit 0, so
`build_succeeds` stays 1; the `--runxfail` form is a genuine red with the test **SELECTED**, not a "no tests ran".
`redcheck.jsonl` confirms all seven red at `635d442`.

**T-181 was deliberately not amended.** Its reproduction passes today by design, so it could never be a red gate;
converting it would have manufactured a green gate on unbuilt work, which is the failure mode T-081 and T-160
already document.

**Recorded honestly because it happened**: the first attempt at this amendment was made via a JSON round-trip
that reformatted the entire file — **417 changed lines** against a permitted 7. It was reverted from a pre-edit
backup and redone as a surgical line edit. A `verify`-fields-only amendment that rewrites the file is not a
verify-fields-only amendment, whatever its intent.

## Tickets

**Closed this epoch (2, both post-measurement):**

- **T-223 (CRITICAL)** — the exact-equality price floor became a threshold at `max(list_price × 0.1%, one minor
  currency unit)`, judged on every row; closed at the exchange door only, with the boundary equality carried
  forward as T-250.
- **T-224 (HIGH)** — the unauthenticated 500 from an unreadable store price, closed twice: `Field(gt=0.0)` on
  the roster row and a per-row unreadable-price judgement with no roster term in it.

**Closed by the measured merges immediately before this epoch (4):** T-022, T-052, T-072, T-073 — the last of
which took **E7 to target**.

**Settled BUILT by the closure audit (17):** T-030, T-031, T-033, T-040, T-041, T-060, T-061, T-062, T-063,
T-080, T-102, T-104, T-109, T-110, T-111, T-112, T-114.

**Minted (12), across two mints, re-sealed as freeze-log records #23 and #24:**

| id | sev | source | what it says |
|---|---|---|---|
| T-250 | HIGH | T-223 lane, packet-scope discrepancy | the exact-equality floor is still live in `boundary.py:819`; the TypeScript peer door mirrors it |
| T-251 | MEDIUM | T-193 lane, `.pth` sweep | a test's comment asserts the repo root "is not handed over"; the `.pth` hands it over anyway, and the measurement is correct only by `sys.path` ordering |
| T-252 | MEDIUM | T-193 lane, `.pth` sweep | a scratch-tree import is safe purely by name luck; a rename collision starts grading the live checkout |
| T-253 | MEDIUM | reachability audit | `utc_now()` has zero callers and a docstring asserting an invariant that is false — `frozen_clock` does not exist and `state.py:513` calls `datetime.now(UTC)` directly |
| T-254 | MEDIUM | reachability audit | `ExtractedClaim.as_claim()` is defined and never produced — T-021's "decompose to atomic Claims" objective outrunning its wiring, while its sibling `as_attribute()` is wired |
| T-255 | MEDIUM | reachability audit | `DiscountCode.is_redeemable_at()` has zero callers; a future caller silently gets a narrowing the live path does not apply |
| T-256 | HIGH | closure audit ×2, by grep | **T-065's persistence half is entirely unimplemented** — five ledger tables exist and nothing writes any of them; its acceptance item is graded only as pure-function idempotency |
| T-257 | HIGH | closure audit | **T-112's recorded gate collects none of its own grading tests** — the gate would stay green if every T-112 behaviour were deleted |
| T-258 | MEDIUM | closure audit | T-102's gate is fully vacuous without docker; unlike its gate-sharing sibling T-114 it retains no non-docker grader at all |
| T-259 | MEDIUM | closure audit | T-063's acceptance grades a fixture, not the system; nothing joins trust's emitted payload to the intake side, so a field-name drift is invisible to both suites |
| T-260 | MEDIUM | reachability audit | the exchange's retrieval package has zero production callers; `routes.py` never retrieves |
| T-261 | MEDIUM | closure audit | T-064's third acceptance criterion — the client cache and version-bump refresh — **does not exist**, in either language, and the epic-scoped gate hides the gap |

**T-257 deserves a second look.** It is stronger than the known T-110/T-112 non-discrimination already on the
books: the gate is not merely unable to tell the two tickets apart, it **cannot see T-112 at all** — the file it
names contains no occurrence of the string `T-112`, and every actual grader lives in a file that gate never
collects. A gate that is green on work it cannot observe is the same class as T-081's old gate, and it was found
by an auditor reading code, not by any gate.

**Every one of the twelve shipped with the `false  # NO GATE YET` placeholder** that `findings` writes into
anything it mints, so none of them is dispatchable until it earns a red reproduction — the same wall amendment 15
had just taken down for seven trust tickets. That wall is the harness defect filed as `B09032015-01`, and the
loop now walks into it once per mint.

### Landed during report preparation, and not covered by anything above

`main` did not hold still while this was written. After `a02067d` it advanced to **`c9e5d5f`**: `6c5b840` minted
**T-262 and T-263** from a T-087 scope collision (freeze-log **#25**); an exchange reproduction lane landed
`apps/exchange/tests/test_repro_open_tickets.py`; **amendment 16** (freeze-log **#26**) gave real red gates to six
more previously-undispatchable tickets — **T-158, T-169, T-170, T-204, T-235 and T-250** — on the same
verify-fields-only terms, measured in both directions, with the whole `apps/exchange` suite reading `695 passed,
16 skipped, 7 xfailed` post-merge; and a fourth mint (**#27**) added **T-264…T-269**. **T-250, minted in this
epoch as the boundary half of T-223, already has a red gate and is dispatchable.** None of that work is described,
verified or measured in this report.

## Ranking was done by hand this epoch, and here is why

**`frontier` ranks by `unblocks`, while the skill's Phase 3 requires ranking by the latest analysis, largest
error contribution first.** Those are different orders and the tool does not say which one it applied. This is a
known harness defect, filed machine-wide as **S09040030-08**, and it is an instance of the recurring shape
S09040030-01 names: the skill states a rule in prose while its own tool prints or scores the opposite.

**So priorities this epoch were set by hand from `analysis/cycle-16.md`, and this line exists so nobody later
mistakes that for the tool's output.** `e8_proofs_passing` is the only epic metric off target and the single
failing acceptance test is its runbook test, so **T-082** (`e2e/test_s1_flow.py`, which does not exist — the
directory does, the file does not) and **T-085** (the demo runbook, whose frozen acceptance test is the one
failure on the board) were dispatched as the highest-value work available. Ranked by `unblocks` neither would
have been near the top.

## `pending_remeasure: [7]` — deliberately NOT cleared

Unchanged from cycles 14 and 15 and left open for the same reason: a `freeze --amend` moved a target and the
baseline it moved has not been re-measured since. Running `measure --cycle 7` **today** would file **today's
values under cycle 7** and corrupt the trajectory every verdict in this report is fitted against. It withholds
`all_targets_met`, which is correct — with two metrics off target, that is not close, and clearing the flag to
make a number look finished would be the exact tampering the freeze exists to prevent.

## Decisions for cycle 17

- **The frontier is T-082 and T-085, and it was chosen by hand.** They are the only work that touches the one
  failing acceptance test. T-082 must create `e2e/test_s1_flow.py` — the directory exists, the file does not, so
  its gate names a file its own lane has to write; T-085 must put a runbook under `docs/demo/` with the sections
  and make targets the frozen test names. Both were dispatched this epoch and neither has merged.
- **T-250 is the next-highest single repair known**, and it is now dispatchable. The exact-equality floor is
  still in the shared boundary, the TypeScript peer door mirrors the arithmetic, and the exchange repair does not
  reach either. It replaces T-223 in the slot T-223 held since cycle 13.
- **T-256 and T-257 outrank the ten MEDIUMs beneath them.** T-256 says a ticket's entire persistence half was
  never written while its acceptance grades pure-function idempotency; T-257 says a recorded gate cannot see its
  own ticket at all. Both are measurement-credibility defects, not backlog items.
- **A gate that PASSES is still not evidence of built work, and a gate that reads WEAK is not evidence of the
  opposite.** This epoch produced both errors in one sweep: 29 green gates that needed a code audit before any
  could be called closed, and 6 of 13 WEAK stamps that were about `npx` and nothing else. **Read what a gate
  SELECTED, in both directions**, before believing either colour.
- **Lane-authored sabotage stays mandatory in every packet.** Three-for-three again: T-223/T-224's revert was
  timed from outside, and the T-193 lane found two defects in its own gate — in opposite directions — that
  reading the test had not found, and that no green would ever have surfaced.
- **Every ticket a mint produces is undispatchable until someone writes it a red gate.** Twelve arrived this
  epoch and six more arrived while this was being written. Budget the reproduction lane, or the backlog grows a
  tier nothing can schedule.
- **Provision every worktree from BOTH lockfiles.** Six lanes came up with no `node_modules` because the packet
  said `uv.lock` and the repo carries `package-lock.json` too (S09040030-06).

## Harness hermeticity: PARTIAL

`verify` answers **did the frozen bytes move.** They did not: all **17** frozen files re-hash clean against
`.swarm-loop/manifest.json` at `a02067d`, and the manifest is itself reconciled against the append-only
`freeze-log.jsonl` — amendment 15, 25 records, at that sha. That is a real and load-bearing question, and it is
**strictly narrower** than *can this measurement be gamed*.

The structural residual is unchanged and still open: **product code imported by the acceptance suite runs in the
scorer's own process and can tamper with the scorer's in-process state.** That is inherent to any in-process
black-box suite. It is not hardened away — only *moved* — by a subprocess-per-test design, and this run does not
pay for one.

**This epoch's concrete instance is the sharpest the run has produced.** The T-193 defect has been live since
`apps/trust/Dockerfile` was written on 2026-09-02 (`d742cae`), across every cycle since, and **`verify` has been
green the whole time.** Then a reproduction test was written specifically to catch it — and **reported RESOLVED
on its first run**, and would have gone on doing so indefinitely, because the leak was **in the venv rather than
in any frozen byte.** (Precisely: the test was added `132b3d1` on 2026-09-03 and repaired `55492fb` the next day,
so the false green lasted a day, not the run — but nothing in the harness was going to end it, and the
already-live defect it was blind to is two days and four cycles old.)

`_proxyshop.pth` is not a frozen file, is not in the manifest, is not in git, and puts the checkout on `sys.path`
at interpreter startup in a way that survives a scrubbed `PYTHONPATH`, an empty cwd, and `-I`. No amount of
byte-equality checking on `.swarm-loop/` could have seen it. It was found by a lane instructed to sabotage its
own core and prove the red, and by nothing else. Two of this epoch's twelve minted tickets — T-251 and T-252 —
are the same class, still open.

The vocabulary trap in the artifact persists and is worth restating: `.swarm-loop/analysis/cycle-16.json` records
`harness_integrity: "intact"`. **That field is the byte-equality verdict — the narrow question — and is not a
statement about hermeticity.** Hermeticity is PARTIAL. It has never been sealed, it is not sealed now, and no
line in this report should be read as saying otherwise.

## Open, recorded, not fixed

Everything cycle 15 recorded under this heading stands except where superseded above.

- **The rung-2 cross-branch interaction lens for the cycle-13 batch is still owed** — T-214's lock × T-216's
  shared-database handling × T-206's ledger replay, in one process, against post-wave `main`. **Now carried for a
  fourth epoch.** No green in this report covers it.
- **Selection tracking is unavailable for all twelve metrics.** The deselection backstop is the only one of the
  three enforcement points that can see a filter added after the freeze, and this run has two amendments that
  touched gates. Every "no selection regression" in every analysis so far means NOT MEASURED.
- **The exact-equality price floor is still live in `packages/contracts/src/boundary.py:819`** and the TypeScript
  peer door mirrors it. T-250, unfixed.
- **ESC-014's two frozen tests still grade less than their metric names imply**, and E2 and E5 both now read at
  target. Ruled, recorded, not repaired — by decision, not by oversight.
- **`frontier` ranks by `unblocks` against the skill's own priority rule** (S09040030-08), and **provisioning
  uses one lockfile where the repo carries two** (S09040030-06) — every live lane worktree came up from
  `uv.lock` only and had no `node_modules` until `npm ci` was run in six of them. Both filed machine-wide, both
  open.
- **Docker is down**, so every docker-marked test in the repo is a skip and every gate that collects them exits 0
  on a fraction of its own suite. T-258 names one instance; the class is larger than one ticket.
- **`task/C13-esc007` was a dead branch** — its only change against its merge base is an acceptance file whose
  blob is byte-identical to `main`'s, already landed as ESC-007 amendment 6. Reclaimed rather than merged; no
  work was lost and none was gained.

---

<sub>Cycle 16 · epoch `9ef97f8..main` · the ten commits this report describes are `9ef97f8..a02067d`; `main` had
advanced to `c9e5d5f` (fifteen commits) by the time the report was finished, and the difference is listed above
rather than absorbed silently · measured at `9ef97f8`, the range's **base** — nothing above it is in any number
on the board · frozen bytes re-verified at `a02067d`, 17/17 clean against `manifest.json` ·
written against swarm-loop skill `6778268`.</sub>

---

## Push gate: DECLINED for cycle 16 — one condition unmet

`push-gate --cycle 16` scored five mechanical conditions. Four PASS: `verify` intact, no stale metric in
`analysis/cycle-16.json`, tracked files 663 → 713 with no sharp drop, and `.swarm-loop/` clean at HEAD.

**The unmet condition is `rung2-returned`.** One verifier dispatch is still open — `verifier-c16`, the adversarial
verification of the batch `9ef97f8..main`. Its brief covers the thing this epoch most needs a second pair of eyes
on: resolution provenance, i.e. whether the greens on the T-223/T-224 fix executed the merged source rather than
the primary checkout's `.venv` through the `_proxyshop.pth` leak. That is not a hypothetical here — this same epoch
repaired a reproduction test that had been passing for exactly that reason. A verdict still outstanding is an unmet
condition and not a pass, so **nothing is pushed for cycle 16 and the push carries to the next epoch's gate.**

Worth recording that the gate could only see this because the verifier was entered in the dispatch ledger with
`--kind verifier`. It was dispatched some time before it was recorded; had it never been recorded, this rung would
have passed vacuously and 38 commits would have gone offsite unverified. The gate is only as good as the ledger
it reads.

The two OPERATOR conditions the command refuses to score, answered here rather than left implicit:

- **Integrations landed green on `main`** — YES, measured in the primary checkout at merge time, not re-read from
  a log: `apps/exchange` 695 passed / 16 skipped / 7 xfailed after the exchange reproductions merged on top of the
  T-223 threshold; `apps/trust` 400 passed / 127 skipped / 7 xfailed; the trust reproduction file 1 passed /
  7 xfailed on the normal run and `1 failed, 7 deselected` under `--runxfail` for T-193; `ruff check` and
  `ruff format --check` clean across 459 files.
- **No rung-2 finding invalidates a merged branch** — CANNOT BE ANSWERED YET, because the verifier has not
  reported. That is the same open condition, and it is the honest reason the push is declined rather than deferred.
