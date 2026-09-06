# Cycle 20 — epoch report

HEAD at close: `e7f8f6c` (merge of `integration/20-6`), recorded in `state.json` head_history as
cycle 20 — 837 tracked files, 305 tickets, 184 closed in the ledger. Epoch range
`9597237..e7f8f6c` (2026-09-05 14:14 → 2026-09-06 00:52).

## 1. OPEN ESCALATION — ESC-030, waiting on you

**This is the only thing in this run blocked on a human. It is NOT blocking the build.**

> **ESC-030** · harness · `pending_remeasure[7]` — ESC-027's ruling rests on a premise that
> measurement now contradicts. Filed 2026-09-06T01:06:50, cycle 20, HEAD `e7f8f6c`.

**What is wrong.** Your 2026-09-05 ruling on ESC-027 decided `pending_remeasure=[7]` STAYS,
and the reason recorded with it was that no honest action could discharge it: *"measure cannot
be pointed at a past ref, and the only available command would write today's values into cycle
7's row."* That premise is false. `swarmloop.py measure --help` carries `--clears-remeasure N`,
documented as *"discharge the re-measure debt freeze --amend opened on cycle N using THIS
measurement. The row is recorded under `--cycle`, where it is true; cycle N's historical row is
left alone."* `analyze` now prints it as the preferred option over `measure --cycle 7`. Whether
the flag is new or was simply missed on 2026-09-05 was NOT established; the option exists today
either way.

**The proposed change, copied verbatim with its markings — do not strip these:**

```
proposed : Authorize discharging the flag with 'measure --cycle 20 --clears-remeasure 7',
           which records at cycle 20 and leaves cycle 7's historical row untouched — or
           confirm ESC-027 stands as ruled, in which case say so and I will keep reporting
           the flag every cycle as you originally directed.
           UNVERIFIED PROPOSAL — nothing checked this against the code; verify it before acting
           not checked: NOT STATED — the escalator did not say what they left unverified;
                        silence here is not a claim of completeness
```

**What it changes.** Cycle 20 measured all 12 metrics at target with every error 0.
`all_targets_met` is withheld by this flag alone (plus the standing ungraded-ticket withhold).
Discharging it lets `analyze` report all-targets-met — during the build act with tickets still
open that means "keep building", so it is not itself terminal.

**The exact command that answers it:**

```
python3 ~/.claude/skills/swarm-loop/scripts/swarmloop.py escalate --resolve ESC-030 \
    --note "<what you decided>"
```

If you grant it, the harness change is then re-locked with
`swarmloop.py freeze --amend "<why you approved this harness change>"`.

Nothing else in this run is waiting on a person.

## 2. Tickets closed this epoch

Every row below is a `close` action in `dispatch.jsonl` between 2026-09-05T15:56 and 22:07.
**Verdict `rejected` closes the LANE and keeps its tickets OPEN** — it is not a verdict on the
lane's work.

**Landed on main, gates green, `make verify` OK: all**

| ticket(s) | lane / merge | resolution |
|---|---|---|
| T-239 T-243 T-246 T-247 T-248 | lane-merchant → 20-2 | each measured RED at HEAD before the lane started; each gate now green selecting exactly 1 test (1 passed / 4–8 deselected) |
| T-306 T-307 | money-path → 20-3 | absent `list_prices` roster now REFUSES instead of abstaining at all 3 Python + 4 TypeScript sites; a signed bid awarding itself 85% off was accepted whenever the roster was omitted |
| T-156 T-279 | lane-store-agent → 20-3 | T-279 `_receive_bid` 52 escapes → 0 (first fix was wrong: it probed `.get` then passed the caller's object back, which the boundary reads twice); T-156 `total_price` read by nothing, 60/60 escapes → 0 |
| T-236 T-238 T-245 T-249 | lane-ingest → 20-3 | T-236 built the missing scheduler middle as NEW modules so frozen `main.py` was never touched, driven against a real stdlib HTTP server; T-238's recorded location was WRONG — the live unguarded parse was `transport.py:378`, not `netguard.py:398` |
| T-166 T-207 T-212 T-256 T-259 | lane-trust → 20-3 | 6095 passed / 62 xfailed. T-256 added `trust.verification.persist_claim_verification`; six defects in that work were found by adversarial review, worst a cached `autocommit=False` connection that bricked the endpoint permanently |
| T-164 T-197 T-198 T-199 | buyer-identity → 20-4 | buyer suite 643 passed / 5 xfailed, four gates green, 1 selected each. T-197's packet predicted 2 reds; measured 11, because the PARK_LANE fixture's address carries "CO" |
| T-264 T-293 T-326 T-327 | lane-exchange | PARTIAL ACCEPT, branch HELD on a self-reported blocker; the repr-leak class rode into `task/exch-dos` (merge `2a6915e`) and landed in 20-6 |

**Closed on their own recorded gate**

| ticket | resolution |
|---|---|
| T-082 | `PROXYSHOP_WORKER=5 pytest e2e/test_s1_flow.py -q` → 27 passed, exit 0. Not vacuous: 27 selected, and the gate's own hermeticity assertion (`e2e/test_s1_flow.py:64-80`) fires on real data. Reverses a `rejected` verdict that was correct about the BRANCH's bytes, not about the ticket |
| T-257 | 2 passed / 30 deselected. Two graders: an arming control that passes, which is what makes the second's verdict trustworthy. Could not close before ESC-028 because the C20-trust lane wrote the graders, merged them, and never amended the verify field |
| T-309 | 1 passed / 5 deselected. Boots the REAL app via `create_app()` and asserts `served_operations(app) == published_operations(STORE_AGENT_OPENAPI)` in BOTH directions, guarded by an arming assertion so an empty contract cannot manufacture a pass |
| T-160 | 2 passed / 2 deselected in normal mode. Nine offenders when written; eight repaired by earlier amendments; the last was T-133, whose gate was the whole-suite `verify.sh check` while `apps/buyer/svc/tests/test_profile_identity_leaks.py` grades it |

**Closed as already-satisfied or invalid — no gate written, deliberately**

| ticket(s) | resolution |
|---|---|
| T-203 | premise dead. `apps/merchant/svc/src/codes/` is 122 KB across 10 modules; `codes/offer.py:36` imports the converter and `:292` calls it, reached from `codes/create.py:215→219` |
| T-221 | INVALID — the ticket is wrong, not the code. `SPEC.md:56` lists k-anonymity beyond a configurable floor under NON-GOALS; `cycle-9.md:45-51` already ruled this exact question |
| T-331 T-281 T-287 T-288 T-305 | already fixed at HEAD; each verified independently by the lane owning that scope BEFORE the staleness audit reached it. The tickets describe revisions `aae49af`/`f6fd5bd`, not HEAD |

**Lanes closed `rejected` — gates landed, fix work never done, tickets stay OPEN**

- `task/C20-trust` — T-172, T-256, T-257, T-259, T-303. Delivered 12 graders in
  `apps/trust/tests/test_repro_open_tickets.py`, no fixes. T-172 measured WIDER than recorded
  (9 docker-marked items declare no service, not 8).
- `task/C20-store` — T-156, T-279. T-156: `enforce_bid_provenance` admitted 50+ bids whose total
  is below one unit price (unit 95.0 vs total 91.07 at 5% depth).
- `task/C20-support` — T-160, T-210, T-262, T-242.
- `task/C20-exch` — T-148, T-244, T-270, T-293, T-294. Never merged; its stronger probes were
  salvaged onto main's file in `4ace0fc`.

**Wave-2 lanes merged at `e7f8f6c` — close rows NOT yet written to the ledger** (the orchestrator
writes those). `integration/20-6` landed `task/contracts-api` (T-267, T-240), `task/exch-dos`
(T-349, T-294, T-266 partial, T-150/T-282/T-283 gates, T-264/T-293/T-326/T-327, T-301, T-325),
`task/sim-stub` (T-242, T-265, T-253, T-255) and `task/support-lane` (T-334, T-252, T-171, T-205,
T-210). **T-210 is explicitly NOT closed**: its plugin is built and graded end to end, but the
wiring is five names in the root `conftest.py`, outside the lane's write scope, so its
`xfail(strict=True)` gate is still correctly RED.
`task/buyer-auth` is **unmerged**, 4 commits ahead of main (T-165, T-140, T-142, T-163).

## 3. Tickets minted this epoch

Nine findings were recorded at cycle 20 in `findings.jsonl`. Six were minted as
T-352..T-357 in `c386096` (two HIGH); the other three (the `offer: {}` HIGH, the sealed-vault
envelope HIGH, the k-anonymity doc gap) were minted earlier in the epoch under other ids.

| ticket | sev | source | measured |
|---|---|---|---|
| T-352 | HIGH | `apps/exchange/src/auction/state.py` | 512 honest `POST /auctions` retain 0.252 MiB; 512 carrying a 30,000-char `intent_id` + `cluster_id` retain **29.547 MiB**, linear with no plateau. `GET /auctions/{id}` hands the 30,001-character string to an anonymous reader. No cap, no TTL, no eviction |
| T-353 | MED | `apps/exchange/src/accept/routes.py` | unbounded `bid_ref` on the unauthenticated accept door, echoed in the refusal body and persisted into `policy_event`, while its sibling `RosterEntry.store_id` carries `max_length=128`. At 4,000,000 chars: 409 with a 3.9 MiB body. Amplification **1.00x**, so NOT an amplifier — an earlier "cheapest live amplifier" description was overstated and is corrected in the record |
| T-354 | MED | `apps/exchange/src/ranking/routes.py` | the 404 body interpolates the caller's `auction_id` with no length bound and, alone among its sibling doors, without the `redact_addresses` sweep |
| T-355 | MED | `apps/exchange/src/auction/routes.py` | no request-body size cap on `POST /auctions` or `POST /auctions/{id}/accept`. The streaming 64 KiB cap exists in the package, on the policy door only |
| T-356 | HIGH | `apps/buyer/svc/src/vault/pseudonyms.py` | `normalise_buyer_key` only strips and case-folds. **40 of 40** requests across `dana.reyes+0`…`+39` accepted, mailing 40 links to one physical mailbox **under a budget of 5**. Case and whitespace correctly do not buy a fresh budget. The repair is deliberately not local: this is the vault's identity key repo-wide |
| T-357 | MED | `apps/buyer/svc/src/auth/routes.py` | no per-caller or per-IP ceiling on the magic-link door. **300 of 300** requests for 300 distinct addresses from one caller accepted. The per-address budget closed the one-mailbox flood and left the many-mailbox one |

All six carry the NO-GATE placeholder `verify` by design and `red-check` refuses them, so none can
dispatch until someone writes a gate that fails on the finding. T-352 and T-355 both touch the
exchange auction door and were held for `task/exch-dos` — which has now merged.

## 4. Metrics — 12 of 12 at target, every error 0 (`history.csv`, 00:56:58 → 01:04:25)

| metric | value | target | error |
|---|---|---|---|
| acceptance_pass_rate | 100 | 100 | 0 |
| acceptance_collected | 120 | 120 | 0 |
| build_succeeds | 1 | 1 | 0 |
| spec_criteria_passing | 8 | 8 | 0 |
| e1_foundation_passing | 10 | 10 | 0 |
| e2_ingestion_passing | 8 | 8 | 0 |
| e3_exchange_passing | 21 | 21 | 0 |
| e4_store_agent_passing | 20 | 20 | 0 |
| e5_merchant_passing | 10 | 10 | 0 |
| e6_trust_passing | 26 | 26 | 0 |
| e7_buyer_passing | 9 | 9 | 0 |
| e8_proofs_passing | 8 | 8 | 0 |

`checkpoint` exited 0; no kill switch, `stall 0/3` on all three counters. `analyze` withholds ALL
TARGETS MET for two standing reasons: **268 of 305 tickets carry no frozen test**
(graded_ticket_fraction 12.1%), and the cycle-7 re-measure (ESC-030 above).

**Selection tracking remains unavailable for all 12 metrics.** Read every "no selection
regression" as NOT MEASURED, never as clean: the frozen wrappers capture pytest into their own
log and print only the bare number, so `measure` has nothing to parse on stdout OR stderr. The fix
needs no code change here — have the wrapper echo pytest's own summary line to **stderr** while
still printing the bare number on stdout. Never print raw pytest to stdout; that breaks the
bare-number contract.

## 5. The four merged lanes, and what collection found

All five wave-2 lanes were killed mid-work by a rate limit: all had committed, none had reported,
their gates and `make verify` were unrun, `integration/20-5` was parked and superseded. All five
were collected **adversarially, by agents that did not build them**. Every one came back
accept-with-findings; none was rejected. Four needed fixes before merge.

**`task/contracts-api` — the published pattern was wrong about its own emitted set.**
Verified two independent ways that zero paths, methods or schemas were removed. But the lane
introduced a `pattern` on the 409 `denial_reason` requiring `code` then end-of-string or exactly
`": "`, while `gate._denial_reason` passes a source's string through verbatim whenever
`denial_code(reason) == code`, and `denial_code` splits on a **bare** colon and `.strip()`s the
token. So `blacklisted:chargeback fraud`, `blacklisted : x` and `blacklisted:` are all served and
all refused by the published pattern. Fixed contract-side, on the boundary's own rule that the
emitted set is what the pattern was wrong about. Both-ways proof in a scratch tree outside the
repo: without the fix, 2 failed / 1 passed, the sweep naming 1376 served values and the code-point
walk diverging on 30 code points; verified engine-independently against node's `new RegExp`
compiled straight off the checked-in document. The sibling sweep enumerated every string node in
BOTH published documents carrying `pattern`, `enum`, `const`, `format`, `minLength` or
`maxLength` — **20 nodes** (2 exchange, 18 merchant) — and traced each producer: exactly ONE had
this shape. A further **57** were reached through the 13 external `$ref`s into
`protocol.schema.json` and swept too. **Six `format: date-time` passthroughs were deliberately NOT
widened** — there the contract is right and the producers are wrong (already ticketed as T-194),
so widening would publish the bug; every repair for those is producer-side, outside this lane.

**`task/exch-dos` — the DoS fix verified live over a real socket, and the rescue commit gated.**
500 duplicate roster rows, one hostile bid of **234.6 KiB** (under the 256 KiB
`MAX_BID_RESPONSE_BYTES` cap):

```
before, outer-length bound  -> 201, 500 records, book 339.82 MiB, RSS 428 MiB
before, NO bound at all     -> 201, 500 records, book 339.82 MiB
after,  recursive budget    -> 201,   0 records, book   0.00 MiB, RSS  82 MiB
```

The middle line is the credibility control: with `_recordable_offer` replaced by a bare
projection the retained size was IDENTICAL, so against a nested payload the old bound was not
weak, it was **inert**. Per-field, one unauthenticated request retained **81.58 MiB** through
`currency`, `variant_ref` and `discount`.
The lane's tip (`847c9b6`) was a rescue commit shipping source graded by nothing: it flipped the
walk's final branch from a blacklist to a whitelist, and reverting it in a scratch tree left the
whole `apps/exchange` suite byte-identical at 1003 passed / 10 xfailed while
`_recordable_offer({"currency": deque(range(200_000))})` came back RECORDED with 200,000 items.
It is now gated by a corpus wrapping one shared payload every way a caller of `collect_bids` could
— including a class defined in the test file, which no blacklist could ever be written against —
**partitioned by the source's own exported `RECORDED_OFFER_ACCEPTED_TYPES`**, with an arming test
asserting both halves stay non-empty. Retained-bytes leg: 32 rows × 50,000 elements grew the book
12.62 MiB under the blacklist and 0.00 MiB under the whitelist. The lane also bounded two
caller-chosen ids on the route it added (`POST /internal/outcomes`): `store_id` at 30,000 chars
over 256 sends retained 7.416 MiB, and the **sibling sweep found a second instance the collection
had missed**, `cluster_id`, at 0.935 MiB over the same sends. Plus a cheaper crash: `json.loads`
parses a 2000-deep list but `deepcopy` raises RecursionError from ~600 deep, so ~1.2 KiB of JSON
faulted `POST /auctions`.

**`task/support-lane` — code sound, prose wrong, and one real path escape.**
Seven checkable claims in comments, docstrings and the previous commit body were reported false;
all seven were **re-measured before being rewritten**, because a correction written on an
unchecked claim is the same defect wearing the opposite sign. All seven were indeed false. The
most dangerous was `lock_exit_status.py:95` asserting in the present tense that the module is
imported by the root conftest — it is not, and that claim would have convinced a reader a
still-dead ticket was done. Notably the fixer **refuted one item in its own brief**: the finding
said the ticket record attributed the catch to only one probe; the record actually ends *"the
real-image control DID catch this sabotage, AND THE T-193 PROBE DID"*, and the "exactly one"
claim came from an earlier draft of the comment itself, which had dropped a catcher the record
already named.
The one genuinely local hardening: `load_repo_script` did no name validation, so
`load_repo_script("../conftest")` resolved `scripts/../conftest.py`, **executed the root
conftest**, and returned it bound as a module — arbitrary code execution, not a bad lookup. All
four call sites pass literals, but "no caller does this yet" is not a guard. Two independent
conditions added, gated by 10 tests including an armed control proving the escape target is a
real readable file. T-210's plugin re-stamps `session.exitstatus` to **77** when, and only when,
the run's whole evidence says its sole failure was another process holding the machine-global D37
flock — 77 because `verify.sh` remaps 5 to 1 (laundering a machine condition into a product
failure), 2 is its own FATAL, and 0 makes the condition invisible. Graded in real child
processes: without `-p` → contention 1; with `-p` → contention 77, defect 1, both 1.

**`task/sim-stub` — accepted clean, no fix needed.** Seven sabotage mutations all caught, three
of which pass `make verify` and are caught only by a neighbour sweep.

**`task/buyer-auth` — five sabotages of the exact guarantee functions all caught, none hung.**
Fixes were still in flight for: a service-wide login lockout reachable without mailing anything,
an O(tracked) per-request sweep under a global lock (6.2 ms at 50k subjects), `admit` accepting
retired pseudonyms, and a publisher that never reconnects. The branch is unmerged, so none of
this is verifiable from a committed artifact — it lives only in the collection agent's report.

**Integration.** `integration/20-6` took all four in dependency order with no conflicts. The
union of their suites collected clean in ONE process — the check that actually exercises
support-lane's `conftest`/`meta_path` changes against the other lanes. `make verify` finished
`OK: all` and `build_succeeds` recorded 1 at 01:04:13.

## 6. Amendment 36 (ESC-028) — and what was refused is larger than what was accepted

24 tickets were repointed from the NO-GATE placeholder to graders **their own lanes had written,
merged, and never wired into the graph**. Each is an `xfail(strict=True)` reproduction in the
house pattern (an arming node plus property nodes), in `test_repro_open_tickets.py` under
`packages/store-agent`, `apps/trust`, `apps/exchange`, `services/ingest`, `apps/merchant/svc`.

Measured in two passes, because the first was not sufficient. Pass 1, collect-only over all 299
tickets: 149 pytest-shaped, 143 resolving, 6 naming files that never existed in git history, 0
collection errors — which proves node ids resolve and nothing more. Pass 2 executed all 25
candidates serially on a dedicated free datastore index, with the override confirmed to reach
the interpreter. 24 exited non-zero as genuine reds with collected node counts matching exactly;
none hung; none selected an empty set. `red-check` over the amended graph re-measured all 24
against a clean checkout of main: **24 real, 0 VACUOUS, 0 weak, 0 no-gate, 0 SKIPPED**.

**Four were WIDENED before sealing.** T-244, T-270, T-293 and T-294 each have an arming
companion whose NAME does not carry the ticket token, so a `-k` on the token alone never
selected it. As proposed they were single-node gates that could go green iterating an empty
corpus — the exact failure the arming convention exists to prevent. Each now selects its arming
node by name alongside the ticket token; `red-check` confirms all four still red.

**What was refused is bigger than what was accepted.** T-051 exited 0 — both its frozen
acceptance nodes pass — and under this grant a gate may never be pointed at a test chosen
because it passes, so T-051 is recorded as a *closure candidate* instead. Two whole classes were
refused on the same ground: a **46-ticket** tier whose ids appear only in prose (green non-xfail
regression tests for fixes that already landed, so repointing there converts a placeholder into
an instant pass), plus T-024 and T-084 individually. Accepted: 24. Refused: 49.
Harness intact at 18 files before and after; no metric, target, direction, command, acceptance
test, product file or dependency touched; 24 changed lines, every one a `verify` field.

## 7. Ledger drift cleared

`backlog.md` had stopped agreeing with `tickets.json`: 105 ticket ids present in the graph and
absent from the ledger, one id present in the ledger and absent from the graph, and a file that
never stated the graph's count anywhere. Regenerated at `1107381`: **299 tickets, 138 edges, 219
roots, max depth 10, 181 closed, 118 open (116 READY, 2 BLOCKED — T-084 on T-083, T-087 on
T-084)**. Size **962 lines / 87,313 bytes → 273 lines / 38,777 bytes** (verified by diffing the
file at `8967d2c^` against `8967d2c`).

The ghost `T-10` was a **truncation artifact, not a typo for T-010**: the old ledger cut T-258's
title at ~100 chars mid-token inside "…for T-102 carries the docker marker", and the cut landed
between the last two digits. No such ticket ever existed. The new generator truncates on a word
boundary, drops trailing partial ids, and self-checks the whole file before writing.
**`backlog.md` is already 6 tickets stale again** — generated at `1107381`, before `c386096`
minted T-352..T-357. The graph holds 305; the file says 299. Regenerate before the next dispatch.

## 8. Harness hermeticity is PARTIAL — never write that it is sealed

`verify` answers one question: **did the frozen bytes move.** Every frozen file is re-hashed
against `manifest.json`, itself reconciled against the append-only `freeze-log.jsonl`, and that
came back intact at 18 files before and after amendment 36. That is a real guarantee, and it is
narrower than the one that matters. What it does **not** answer is *can this measurement be
gamed*. The residual, concretely: **product code imported by the suite runs in the scorer's own
process.** A conftest, a `sys.meta_path` entry, a module-level side effect, or a fixture in any
package the metric command imports executes with the same privileges as the measurement — this
epoch's own `load_repo_script` escape (`scripts/../conftest.py` executed and bound as a module) is
exactly that shape, found by an adversarial reader rather than by any hash check. Two gaps
compound it: selection tracking is dead for all 12 metrics, so the one enforcement point that
could see a filter added after the freeze is blind; and the harness persists no metric output, so
an infrastructure red and a real red leave identical artifacts (T-210, still open).

Say **partial**. Not sealed.

## 9. Process-loop notes — including this orchestration's own failures

**No `--scratch-root` was ever named.** `status` reports it `null`, `lane_scratch: "UNRECORDED"`.
Five concurrent collectors therefore shared one scratch directory and **two of them deleted each
other's files** — visible in the tree as `43cc69a`, "drop 15 files my own `git add -A` swept in
from another test's scratch dir". Disjoint file scopes are not disjoint state. `init` takes
`--scratch-root`; this run did not use it.

**Collection was run serially when it could have been pipelined.** The five lanes were
independent, already committed, and collected by five different agents — no ordering constraint
existed among them. Running them one after another left the swarm under-saturated for a stretch
of the epoch for no reason a dependency required.

**A `check-branch` invocation without `--ticket` was used at first.** That is the shape that reads
clean over a genuinely vetoed branch: without the ticket the check cannot compare the branch's
writes against the ticket's declared `owns`, so a scope violation passes. Caught and re-run
correctly, but it should never have been the first invocation.

**The lane-slot pool is still unopened** (carried from cycle 19, unchanged). `lane_slots: 14`,
index 0 reserved, but "no lane file is configured, so the index reaches a lane only if something
asks `slot` for it". Every lane index this run has been hand-picked, and no dispatch path refuses
a lane holding no lease.

**Branch naming keeps degrading closure detection.** `--closed-from-merged` closed exactly one
ticket, because 32 of 33 merged `task/*` branches are named by AREA and carry no `T-nnn` prefix.
`frontier` banners it, but it degrades toward zero **without failing** — the dangerous half.

## 10. Decisions for the next epoch

1. **Regenerate `backlog.md` first, before any dispatch.** It is 6 tickets behind the graph.
2. **Wave width is capped by gates, not by lanes.** 93 of 124 open tickets cannot dispatch; the
   highest-value work is writing red gates for them, starting with the two HIGH finding tickets
   T-352 and T-356 (T-352 and T-355 were held for `task/exch-dos`, which has now merged).
3. **Schedule one lane at a time per shared reproduction file** (see below) and pass every lane an
   explicit `--scratch-root`; this epoch's collectors clobbered each other without one.
4. **Pipeline collection.** Independent lanes were collected serially for no reason a dependency
   required; run them concurrently and always pass `check-branch --ticket`.
5. **Land `task/buyer-auth`** — four fixes in flight, then collect it adversarially like the rest.
6. **Do not act on ESC-030 without a ruling.** Keep reporting the flag until one arrives.

## 11. Carried forward

**Scheduling constraint, hard.** All 24 gates sealed by amendment 36 run a **shared per-package
reproduction file that the ticket does not declare in `owns`**. Each gate can therefore move on
work its ticket does not own, and two such tickets serialise behind a dependency neither
declares. This is an ownership-declaration gap, not a gate defect, and it means: **one lane at a
time per shared reproduction file.** Plan waves around it or lanes will corrupt each other's
files. All 24 are also UNANSWERED for frozen-acceptance gate scope — no frozen node carries their
marker at all.

**Dependency readiness is dead as a scheduling signal.** 219 of 299 tickets are roots; 116 of 118
open tickets are READY only because nothing depends on them. Blast radius and file contention (47
concrete paths are contended by more than one open ticket, glob-aware) are the only live signals
left.

**87 of 118 open tickets carry the NO-GATE placeholder and cannot dispatch** (93 of 124 counting
the six minted after the backlog was generated). That is the single largest constraint on the
next wave's width: writing red gates is the work that unblocks work.

**Still open and unchanged:** ESC-030 (section 1); T-210 (plugin built and graded, wiring is five
names in the root `conftest.py`); T-051 as a closure candidate; the 46-ticket prose tier and
T-024/T-084, refused by amendment 36 and still placeholder-gated; `task/buyer-auth` unmerged with
four fixes in flight; `task/C20-exch` unmerged, probes salvaged; selection tracking dark on all 12.

## Figures I could not verify from an artifact

- **`make verify` = 6360 passed / 1 deselected / 51 xfailed `OK: all`**, and the **union suite =
  2805 collected exit 0 / 2784 passed / 21 xfailed**. The four `integration/20-6` merge commits
  carry empty bodies, and `reports/verify-last.log` is a partial capture ending at 42% with no
  pytest summary line. What IS verified: `history.csv` records `build_succeeds = 1` at cycle 20,
  measured 01:04:13. I did not re-run the suite — six lanes are live and it would contend for
  shared datastores and produce false reds.
- **`task/buyer-auth`'s five sabotages and its four in-flight fixes** — branch unmerged.
- The **"22-type corpus"** for the exch-dos whitelist gate: the commit records the partition
  (`RECORDED_OFFER_ACCEPTED_TYPES`, three named tuples plus `Mapping`) and its arming property but
  no type count; the "22/22" in that body is statement coverage, not corpus size.
- **"240 KiB payload"** → the committed measurement is **234.6 KiB**, and 81.58 MiB is per-field
  for one request, not whole-payload retention. Corrected in section 5.
- **"77 constrained string nodes"** → **20** across both published documents (exactly one with the
  offending shape) plus **57** through the external `$ref`s: 77 in total, but two sweeps with
  different scopes. Corrected in section 5.
