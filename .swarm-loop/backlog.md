# ProxyShop — ticket ledger (closure, ready frontier, and what is actually left)

> **Regenerated 2026-09-03 from `tickets.json`, at `main` = `e9849f7`, after cycle 12.**
>
> This supersedes the revision generated at `e0ffd1a`, which described a **123**-ticket graph,
> knew nothing at or above **T-176**, and had to reconstruct closure from prose because the
> graph carried no status. **That has changed: `tickets.json` now carries a `status` field
> (`closed` | `open` | `unknown`) and a `status_evidence` string on every one of its 168 tickets.**
> The graph is now the closure source of record; this file is a derived view of it and nothing
> else. When the two disagree, the graph wins and this file is regenerated — never the reverse.
>
> **Derivation snapshot.** `tickets.json`, 388834 bytes, `sha256:788f68a08382a3e9…`.
> If that digest no longer matches, these numbers are the ones to distrust first.
>
> **`status` is a reconstruction, not a transaction log.** No cycle 0–12 ever recorded closure
> as it happened; every value here was inferred after the fact from commits, dispatch close
> notes, written adjudications and the state of the source tree. Read "How closed-ness was
> determined" before you trust a `closed`.
>
> **READ THIS BEFORE YOU RUN `frontier`.** `swarmloop.py frontier` does **not** read the
> `status` field. Invoked bare it still prints `0 closed · 87 READY` and heads the queue with
> **T-000, the monorepo skeleton that has existed since cycle 0** — because with no closure
> source every ticket reads OPEN and the "READY set" is just the graph's roots, so everything
> already built sorts to the top. That output is not a work queue and the tool says so itself.
> Always pass the closure list explicitly:
>
> ```sh
> CLOSED=$(jq -r '[.tickets[]|select(.status=="closed")|.id]|join(",")' tickets.json)
> .venv/bin/python ~/.claude/skills/swarm-loop/scripts/swarmloop.py frontier --closed "$CLOSED"
> ```
>
> Measured 2026-09-03: bare → `0 closed · 87 READY · 81 blocked`, headed by T-000.
> With `--closed` → `95 closed · 59 READY · 14 blocked`, headed by T-032 (ranking, unblocks 8).
> `--closed-from-ledger` **cannot** work here: it reads `rec["tickets"]`/`rec["verdict"]`, and
> every `dispatch.jsonl` record instead uses `subject` plus a free-text `note`. Future closes
> should pass `--ticket`/`--verdict` so the ledger becomes usable.

## Totals — computed from the graph in this pass

| quantity | value |
|---|---|
| tickets in `tickets.json` | **168** |
| dependency edges | **141** |
| roots (empty `depends_on`) | **87** — T-000 plus every finding ticket T-135 – T-220 |
| graph health | acyclic, **0** dangling dependency references, **0** duplicate ids |
| **closed** | **95** — work landed, defect fixed, finding refuted, or requirement discharged |
| **open** | **67** = **53 READY** + **14 BLOCKED** |
| **unknown** | **6** — evidence is contradictory or the call is a policy question, listed individually below |
| dispatchable now (READY, open or unknown) | **59** = **12 real feature tickets** + **47 finding tickets** |
| blocked | **14**, all of them E2–E8 feature work waiting on the 12 ready ones |
| open finding tickets by severity | **17 HIGH**, **30 MEDIUM** (0 CRITICAL — the two CRITICALs, T-155 and T-177, are both fixed) |
| open tickets with a placeholder `verify: false` gate | **47** — every open finding ticket; none can be closed by a verify run |
| open tickets whose `verify` names a test file that does not exist | **21** — these are the never-built ones |
| frozen acceptance tests | **120**, last measured **76 / 120 = 63.33 %** at cycle 12 |

Per-epic frozen acceptance at that same cycle-12 measurement (`passing / target`): SPEC 8/8,
E1 10/10, E2 6/8, E3 9/21, E4 5/20, E5 4/10, E6 26/26, E7 2/9, E8 6/8. `build_succeeds` = 1.

## How closed-ness was determined — and where not to trust it

Four kinds of evidence were used. Each ticket's own `status_evidence` says which one it rests on.

1. **A named commit on `main` that fixes the described defect.** Strongest. Most `closed`
   finding tickets have one, and for the cycle-10 and cycle-11 ranges the fix was additionally
   re-read in the source at HEAD rather than trusted from the commit message.
2. **A dispatch close note carrying an ACCEPTED verdict** in `.swarm-loop/dispatch.jsonl`,
   plus the merge that put that branch on `main`. Covers T-031, T-033, T-041, T-062, T-109,
   T-133, T-135, T-139, T-151, T-153 and the cycle-11 lane work.
3. **A written adjudication** — the cycle-10 triage that opened a named file at HEAD and found
   the defect absent, or a refutation, or the T-176 orchestrator ruling. No code landed for these.
4. **The state of the tree.** A ticket whose `verify` names a test file that was never created,
   and whose source subpackage is a 0-byte `__init__.py`, is *open* — that is a measurement,
   not an inference.

**Independent corroboration.** The per-epic frozen acceptance counts match this closure split
almost exactly, and were not used to produce it: E1 10/10, E2 6/8, E4 5/20, E6 26/26, E7 2/9 and
E8 6/8 are each *precisely* the number of frozen tests carried by that epic's `closed` tickets.
E3 and E5 each pass one or two more than their closed tickets account for. Two independent
instruments agreeing this closely is the main reason the `closed` count is as high as it is.

**What a reader should NOT trust:**

- **`closed` does not mean "all acceptance criteria pass".** It means the work landed and was
  accepted. E3 is 9/21 and E4 5/20 with every one of their scheduled tickets closed — the
  shortfall is carried by the *open* tickets in those epics, not by a lie in this column.
- **About fourteen closures rest on a prior pass's prose, not on a commit anyone can point at**
  — the nine cycle-10 refutations (T-136, T-137, T-140, T-141, T-142, T-147, T-148, T-150,
  T-152) and the five "closed in the previous pass" rows (T-138, T-143, T-144, T-145, T-149).
  The prior ledger's reliability was tested — all 71 commit SHAs it cited are real commits and
  all 71 are ancestors of HEAD — but these particular rows cite no SHA. If you want one knob to
  turn, turn this group.
- **`T-105`, `T-131`, `T-146` are closed on a content match, not an id.** No commit names them;
  a commit describing exactly their defect lands after they were minted. Named in each
  `status_evidence`.
- **`--closed-from-ledger` still returns nothing.** Every record in `dispatch.jsonl` uses
  `subject` + free-text `note`; the reader wants `tickets` + `verdict`. The notes say ACCEPTED
  in prose and the flag cannot see prose. Future closes should pass `--ticket`/`--verdict`.

## Dispatch from here — the 12 feature tickets that are actually READY

*"Unblocks" counts transitive descendants that are not closed. Finding tickets score 0 by
construction — they are leaf roots — so this ordering says nothing about their importance.*

| ticket | unblocks | depth | frozen tests | parallel_safe | title |
|---|---|---|---|---|---|
| **T-032** | 8 | 5 | 9 | no | Shortlists rank by the single published formula behind eligibility filters |
| **T-051** | 6 | 3 | 2 | no | Pixel reports checkout outcomes the collector can join |
| **T-071** | 5 | 3 | 3 | no | Three questions or fewer produce a confirmed structured intent |
| **T-042** | 3 | 4 | 3 | yes | Store loop learns from its own outcomes only |
| **T-043** | 3 | 4 | 3 | yes | Shadow mode logs would-be bids and trust events adjust the agent |
| **T-053** | 3 | 3 | 3 | yes | A plain-language interview yields an approved, versioned envelope |
| **T-023** | 1 | 3 | 1 | yes | Catalog MCP adapter passes recorded-contract tests |
| **T-044** | 1 | 4 | 8 | yes | External bids enter signed with a required envelope and route to verification, not rejection |
| **T-052** | 1 | 3 | 3 | no | Winning offers become single-use validated codes and permalinks |
| **T-022** | 0 | 3 | 1 | yes | Same products across stores link via entity resolution |
| **T-130** | 0 | 3 | 0 | no | Sub-HIGH findings from the feature-wave checkers (backlog sweep, not a wave) |
| **T-134** | 0 | 3 | 0 | yes | Merchant residue: a scope guard that shreds strings, a token in repr, and six inbox findings that never reached the lane |

**T-032 is the single highest-value thing in the graph** and has been for three cycles: it
unblocks eight tickets, carries nine frozen acceptance tests, and `apps/exchange/src/ranking/`
is still a 0-byte `__init__.py`. E3 cannot move past 9/21 without it. **T-051** is the same
shape one epic over — `pixel/src` is entirely empty, and it gates the whole E8 proof chain
through T-081. **T-071** gates all of E7, which is why E7 reads 2/9.

Three of the twelve are `parallel_safe: true` and mutually file-disjoint — **T-042**
(`store-agent/src/learning`), **T-043** (`store-agent/src/modes`) and **T-053**
(`merchant/svc/src/onboarding` + `envelope`) — so they can go out together. **T-044**
(`store-agent/src/external`) is also parallel-safe and carries the largest frozen block of any
open ticket, eight tests.

Two entries in that table are **unknown, not open** — T-130 and T-134 — and both are
cycle-9 residue sweeps, not feature work. Adjudicate them before dispatching; see below.

### Blocked — 14 tickets, and what each is waiting on

| ticket | waiting on | frozen tests | title |
|---|---|---|---|
| T-024 | T-023 | 0 | Differential refresh keeps the graph current at field-appropriate cadenc |
| T-034 | T-032 | 3 | Exchange bandit shifts exposure with outcomes and honors exploration |
| T-035 | T-032 | 1 | Loss reports aggregate reasons without leaking amounts |
| T-045 | T-044 | 1 | Three seller personas exercise the external door adversarially |
| T-072 | T-071 | 2 | Shortlists render with provenance and accept hands off cleanly |
| T-073 | T-072 | 2 | Routed buyers can answer one structured feedback prompt |
| T-081 | T-051 | 1 | Simulated buyers exercise the whole network from one seed |
| T-084 | T-083 | 0 | The dishonest store ends below threshold and off the shortlist |
| T-085 | T-082 | 2 | The starting-slice demo is a runbook anyone on the team can execute |
| T-086 | T-043, T-053 | 0 | Onboarding drives a shadow store to its first real bid |
| T-082 | T-072, T-081, T-032 | 0 | One scripted run proves the full S1 flow |
| T-083 | T-034, T-042, T-081 | 0 | Both learning loops demonstrably move under seeded outcomes |
| T-087 | T-053, T-084, T-085 | 0 | The Shopify and onboarding extension runbook covers the beats off the st |
| T-054 | T-035, T-043, T-052, T-053 | 0 | The dashboard shows the walls and the window |

Every blocked ticket is E2–E8 feature work. The chain is short: land T-032, T-051, T-071,
T-043, T-053 and T-023 and **all fourteen** become reachable.

## The six UNKNOWN tickets — decide these, do not dispatch them blind

These are the tickets where the evidence contradicts itself, or where "closed" is a policy
question rather than a measurement. They are deliberately not marked closed: a wrong `closed`
deletes real work from the queue permanently.

#### T-130 — the sub-HIGH sweep from the feature waves

CONTRADICTORY. The cycle-10 triage ruled it 'already fixed — triage opened the named scopes at HEAD and found the sub-HIGH items already carried', but the only code commit for it is 935ddaa 'WIP(fixtures): partial T-130 HIGH fix — UNVERIFIED, agent was stopped mid-task ... may be incomplete or wrong'. 9b0bb2b also records that the ticket itself was CORRECTED because four of its eight locations named the wrong file. Not safe to close.

#### T-134 — merchant residue — the scope guard, the token in `repr`, and six transcribed findings

PARTIAL. 7f75e48 explicitly says 'Three findings from the T-134 review of this lane, all reproduced before being fixed' and closes the two orchestrator-verified HIGHs (the C5 scope guard that shredded strings, the token in repr). The ticket also transcribes four further MEDIUM findings marked 'NOT independently re-verified'; c1689dd 'close six audit findings around the webhook/install surface' lands after and may cover them, but nothing ties it to T-134. Closing the whole ticket would be a guess.

#### T-170 — the accept package ships no `routes.py`, so no HTTP path reaches it

POLICY CALL, NOT AN EVIDENCE CALL. T-176 adjudicates it 'DOWNGRADED from HIGH to this graph gap: it is the ordinary zero-caller pattern (unfinished work awaiting a schedule), not a defect' — so it is closed AS A DEFECT. But apps/exchange/src/accept/routes.py still does not exist and no ticket in the graph owns it, so real work would disappear if this were marked closed. Left unknown deliberately.

#### T-176 — the orchestrator ruling on the zero-caller class, and the graph gap it names

POLICY CALL. The adjudication it exists to record IS recorded (tickets.json and .swarm-loop/reports/cycle-10.md), so its deliverable is done — but the graph gap it names is still literally true: no ticket owns apps/exchange/src/accept/routes.py. Closing it would erase the only record that the gap exists.

#### T-190 — the orchestrator's own retraction — T-135 and T-033 do not compose

POLICY CALL. The orchestrator's self-recorded error is acknowledged on main (5e1fb80: 'T-190 records my false claim that T-135 and T-033 compose cleanly'), so the record exists — but the fact it asserts, that the tightened bid boundary has zero production callers because no bid-submission route exists, is still true at HEAD.

#### T-196 — the trust test that needs the `ledger` schema but never requests it

SUPERSEDED IN PART, UNRESOLVED IN PART. 3927afd rules 'T-216 supersedes both recorded diagnoses of the InvalidSchemaName flake', but T-216's fix site is a different file (_fixtures_ledger_schema.py) than the one T-196 names (test_events_hardening.py:657, which requests no migration fixture), and T-196 additionally raises a concern T-216 does not carry — that T-151's cycle-10 acceptance evidence is itself order-dependent.

## Traps a dispatcher must not walk into

### 1. T-157 is recorded as fixed and is not fixed
Commit `4d170af` says the orphaned checkout code is now carried out on
`OrphanedCheckoutCode.orphan` so the exchange can revoke it. It is not: `apps/exchange/src/accept/`
has had **zero commits since `e0ffd1a`**, and `offer.py:390` still catches a bare
`except Exception` and refuses without ever reading `exc.orphan`. T-202 and then T-215 both
re-confirmed it a cycle apart. **T-215 makes it worse than recorded**: the refusal path writes
the un-revocable discount code *verbatim* into a persisted, client-visible `denial_reason`.
Treat T-157/T-202/T-215 as one job.

### 2. The Neo4j lock is getting worse, not better, and it is a live hazard to this swarm
T-191's fix (600 s → 240 s, `8f0659a`) made the failure diagnosable without making it rarer,
and `e5a406b` says so on `main`: "a Neo4jLockTimeout is still a non-zero `make verify` and so
still records `build_succeeds` 0 for a machine condition." **T-214 then measured the amended
`check` holding the machine-global flock at `/tmp/proxyshop-neo4j.lock` for 196 s of a 222 s
session — ~88 %.** With concurrent lanes running, the loser fails red for a machine reason.
T-171 / T-191 / T-210 / T-214 are one problem; nothing is fixed until the lock stops being
machine-global or the scheduler stops overlapping `check` runs.

### 3. Four tickets sighted one flake; only one of them holds
T-196, T-201, T-212 and T-216 are all the `InvalidSchemaName` trust-test flake. `3927afd` rules
that **T-216 supersedes both earlier diagnoses** — the cause is a session-scoped
`ledger_migrated` fixture that DROPS four schemas out from under concurrently-running tests,
not order-dependence and not a cross-lane collision. T-217 separately measured T-212's central
claim false. **Fix T-216.** T-196 is left unknown because it names a defect in a different file
that T-216's fix does not reach.

### 4. Three closed tickets are only closed at one layer
- **T-065** (claim verification) landed and passes its six frozen tests, but **T-193**: 
  `apps/trust/Dockerfile` never `COPY`s `packages/verification/`, so it crashes in the image.
- **T-186/T-188** connected the trust seams in memory, but **T-206**: `observations_from_events`
  drops the new per-observation `weight` on replay, so the same observation scores 2.25 live and
  3.0 after replay — an S3 determinism break. Buyer feedback must not be routed through the
  ledger until that lands.
- **T-183** decided the discount unit and built the converter, but **T-203**: nothing calls it,
  and nothing will until T-052 is built (`merchant/svc/src/codes/` is 0 bytes).

### 5. Six open tickets cannot be fixed by an ordinary lane
**T-219** and **T-192** need protected paths (`scripts/verify.sh`, `.swarm-loop/goals.json`,
`.swarm-loop/acceptance/**`) and must go through `freeze --amend` or an escalation. **T-160**
and **T-176** name defects in `tickets.json` itself. **T-168** and **T-203** are explicitly
*constraints, not defects* — they discharge when T-030's call site moves and when T-052 is
built, and cannot be "fixed" on their own. **T-200** is an integrator's notice with no
repairable content.

### 6. Every open finding ticket has a `verify` of `false  # NO GATE YET`
All 47 of them. `red-check` refuses that placeholder by design. Whoever takes one writes the
failing gate first — the ticket cannot be graded any other way, and none of them can ever be
closed by a verify run.

## Open inventory — the whole ID range, including everything the previous revision missed

### Feature work — 26 tickets

**E2 — Ingestion**

- **T-022** — Same products across stores link via entity resolution — *READY* · gate not written: `services/ingest/tests/test_entity_resolution.py`
- **T-023** — Catalog MCP adapter passes recorded-contract tests — *READY* · gate not written: `services/ingest/tests/test_catalog_mcp.py`
- **T-024** — Differential refresh keeps the graph current at field-appropriate cadence — *BLOCKED on T-023* · gate not written: `services/ingest/tests/test_refresh.py`

**E3 — Exchange**

- **T-032** — Shortlists rank by the single published formula behind eligibility filters — *READY* · gate not written: `apps/exchange/tests/test_ranking.py`
- **T-034** — Exchange bandit shifts exposure with outcomes and honors exploration — *BLOCKED on T-032* · gate not written: `apps/exchange/tests/test_bandit.py`
- **T-035** — Loss reports aggregate reasons without leaking amounts — *BLOCKED on T-032* · gate not written: `apps/exchange/tests/test_loss_reports.py`

**E4 — Store agent and sellers**

- **T-042** — Store loop learns from its own outcomes only — *READY* · gate not written: `packages/store-agent/tests/test_learning.py`
- **T-043** — Shadow mode logs would-be bids and trust events adjust the agent — *READY* · gate not written: `packages/store-agent/tests/test_shadow_trust.py`
- **T-044** — External bids enter signed with a required envelope and route to verification, not rejection — *READY* · gate not written: `packages/store-agent/tests/test_external_bids.py`
- **T-045** — Three seller personas exercise the external door adversarially — *BLOCKED on T-044*

**E5 — Merchant**

- **T-051** — Pixel reports checkout outcomes the collector can join — *READY* · gate not written: `apps/merchant/svc/tests/test_collector.py`
- **T-052** — Winning offers become single-use validated codes and permalinks — *READY* · gate not written: `apps/merchant/svc/tests/test_codes.py`
- **T-053** — A plain-language interview yields an approved, versioned envelope — *READY* · gate not written: `apps/merchant/svc/tests/test_onboarding.py`
- **T-054** — The dashboard shows the walls and the window — *BLOCKED on T-035, T-043, T-052, T-053*

**E7 — Buyer**

- **T-071** — Three questions or fewer produce a confirmed structured intent — *READY* · gate not written: `apps/buyer/svc/tests/test_intent.py`
- **T-072** — Shortlists render with provenance and accept hands off cleanly — *BLOCKED on T-071* · gate not written: `apps/buyer/svc/tests/test_accept_flow.py`
- **T-073** — Routed buyers can answer one structured feedback prompt — *BLOCKED on T-072* · gate not written: `apps/buyer/svc/tests/test_feedback.py`

**E8 — Proofs, fixtures and runbooks**

- **T-081** — Simulated buyers exercise the whole network from one seed — *BLOCKED on T-051*
- **T-082** — One scripted run proves the full S1 flow — *BLOCKED on T-072, T-081, T-032* · gate not written: `e2e/test_s1_flow.py`
- **T-083** — Both learning loops demonstrably move under seeded outcomes — *BLOCKED on T-034, T-042, T-081* · gate not written: `e2e/test_learning.py`
- **T-084** — The dishonest store ends below threshold and off the shortlist — *BLOCKED on T-083* · gate not written: `e2e/test_dishonest.py`
- **T-085** — The starting-slice demo is a runbook anyone on the team can execute — *BLOCKED on T-082* · gate not written: `docs/tests/test_runbook.py`
- **T-086** — Onboarding drives a shadow store to its first real bid — *BLOCKED on T-043, T-053* · gate not written: `e2e/test_onboarding.py`
- **T-087** — The Shopify and onboarding extension runbook covers the beats off the starting path — *BLOCKED on T-053, T-084, T-085* · gate not written: `docs/tests/test_runbook.py`

**Infrastructure / residue**

- **T-130** — Sub-HIGH findings from the feature-wave checkers (backlog sweep, not a wave) — *READY*
- **T-134** — Merchant residue: a scope guard that shreds strings, a token in repr, and six inbox findings that never reache — *READY*

### Finding tickets — 47 open, none with a working gate

Grouped by severity, then id. The previous revision stopped at T-175; everything from **T-176**
down was invisible to it.

#### HIGH — 17

| ticket | where | what is wrong |
|---|---|---|
| **T-154** | `apps/trust/tests/_fixtures_events.py:42` | EVENTS_ROLE = "app", and its comment asserts app is 'the role the writer connects as... deliberately not trust_rw'. That statement is now false: compo |
| **T-157** | `apps/exchange/src/checkout/provider.py:209-222` | An off-domain permalink from a delegating merchant client leaves a LIVE single-use discount code with no ledger record. The offer's checkout_url is ch |
| **T-160** | `tickets.json (T-109, T-111, T-123 verify fields)` | These three tickets' recorded `verify` commands pass IDENTICALLY with and without their defect, which is the precise mechanism by which all three were |
| **T-163** | `apps/buyer/svc/src/auth/sessions.py:170` | T-133 item (c): SessionStore.open() validates only that the subject carries the 'psn-' PREFIX — it never checks vault membership. Any psn--prefixed st |
| **T-169** | `apps/exchange/src/accept/offer.py:94,:328` | T-033's registered-domain guard is DECORATIVE in the deployed configuration. `_platform_domains` is module state initialised to None and NOTHING in th |
| **T-170** | `apps/exchange/src/accept/ (no routes.py)` | T-033's objective opens 'Accept endpoint resolves a CheckoutProvider by CHECKOUT_MODE...', but the accept package ships NO routes.py. main.py discover |
| **T-171** | `proxyshop_support/neo4j_lock.py:79,119-131` | The D37 Neo4j lock is MACHINE-GLOBAL (/private/tmp/proxyshop-neo4j.lock) and PROXYSHOP_WORKER does not isolate it, so it is shared across every worktr |
| **T-172** | `proxyshop_support/service_markers.py:78-85 (whole-stack fall` | T-109's per-service inference has an 8-item hole and every item in it is a Postgres-only test that a Redis-only outage still silently skips at exit 0  |
| **T-190** | `packages/contracts/src/boundary.py:325 (validate_bid) / :421` | ORCHESTRATOR ERROR, RECORDED AGAINST MYSELF. I wrote in the batch-2 merge commit and reported that "T-135's tightened boundary and T-033's accept path |
| **T-191** | `proxyshop_support/neo4j_lock.py:78 (timeout=600.0)` | CONFIRMS THE build_succeeds FALSE-ZERO MECHANISM. The D37 cross-worker Neo4j flock can never complete a contended wait and its own loud failure is unr |
| **T-196** | `apps/trust/tests/test_events_hardening.py:657 (test_the_writ` | ORDER-DEPENDENT TEST: it needs the `ledger` schema but requests only the `worker_database` fixture, never the migration fixture, and is not marked @py |
| **T-202** | `apps/exchange/src/accept/offer.py:390 (bare `except Exceptio` | T-157 IS FIXED AT THE PORT AND STILL OPEN END TO END. The checkout port now carries the live discount code out on OrphanedCheckoutCode.orphan so the e |
| **T-206** | `apps/trust/src/ledger/replay.py (observations_from_events)` | THE NEW WEIGHT CHANNEL BREAKS S3 (replay determinism) THE MOMENT IT IS USED, and is safe today only because nothing uses it — which is not a property  |
| **T-210** | `ORCHESTRATION — measuring while the swarm runs (supersedes m` | TWO CORRECTIONS, ONE OF THEM TO MY OWN ASSERTION. (1) I claimed the chflags window could corrupt a metric read. THAT IS WRONG, measured: a pytest run  |
| **T-214** | `conftest.py (_neo4j_guard, session-scoped)` | MY OWN AMENDMENT INTRODUCED AN OPERATIONAL REGRESSION I FAILED TO NAME. Every `graph`-marked test is also `docker`-marked (`-m "graph and not docker"` |
| **T-215** | `apps/exchange/src/accept/offer.py:390 and :283 (_refusal_eve` | T-202 CONFIRMED OPEN AND UNDERSTATED. accept() catches the post-mint orphan exceptions but never reads exc.orphan, so the live merchant discount code  |
| **T-216** | `apps/trust/tests/_fixtures_ledger_schema.py:165-194 (ledger_` | THE InvalidSchemaName FLAKE HAS ONE MECHANISM AND IT IS NEITHER OF THE TWO I RECORDED. T-196 called it order-dependence; T-201 called it cross-lane co |

#### MEDIUM — 30

| ticket | where | what is wrong |
|---|---|---|
| **T-156** | `packages/store-agent/src/hooks/provenance.py (_price_reconci` | offer.total_price is never reconciled against unit_price: an offer stating unit_price=80.0 with total_price=1.0 is admitted. Offer carries no quantity |
| **T-158** | `apps/exchange/src/accept/offer.py (module docstring)` | The one-accept-per-auction guard is only as durable as the record handed in. accept() stamps the auction OBJECT it receives. A route that loads an Auc |
| **T-159** | `apps/exchange/tests/test_checkout_provider.py:406 (and repo-` | SYSTEMIC GATE-CREDIBILITY DEFECT: tests that swallow a missing subject with `except ImportError` are VACUOUS until the subject exists, and report gree |
| **T-161** | `packages/contracts/src/boundary.py:157 (_source_verdict)` | A provenance block nested inside a hook-provenanced claim's opaque `value` is not walked. Measured: make_bid(claims=[{"key":"x","value":{"provenance": |
| **T-162** | `packages/contracts/src/boundary.py (validate_bid) — external` | The boundary checks provenance SOURCE but never discount AUTHORISATION. make_bid(claims=[make_claim("policy", {"authorized_discount_pct": 25.0}, hook_ |
| **T-164** | `apps/buyer/svc/src/profile/__init__.py:687` | build_buckets() and anonymise_cohort() are public and run NO identity-leak check; only build_profile()/build_profiles() do. Harmless at HEAD because e |
| **T-165** | `apps/buyer/svc/src/auth/routes.py (POST /buyer/auth/magic-li` | No rate limiter on the unauthenticated magic-link endpoint, and the session/pending-link store behind it is process-local (ProcessLocalStateUnsafe), s |
| **T-166** | `apps/trust/tests/test_events.py:1159` | test_replay_with_snapshots_names_the_missing_scorer branches on `if response.status_code == 503`. Now that T-062 has landed a real scorer it ALWAYS ta |
| **T-167** | `apps/trust/src/{scoring,reconcile,feedback,snapshot}/_bindin` | Four BYTE-IDENTICAL copies of the elected-primary module-binding shim (md5 56d63d33...), one per E6 feature package, because the sequencing must run b |
| **T-168** | `apps/exchange/src/retrieval/ (record_fit_scores / annotate_b` | CROSS-TICKET SCHEDULING CONSTRAINT, not a defect in the landed code. The `bid_placed` event COUNT is load-bearing in two frozen places at once: T-082  |
| **T-175** | `packages/store-agent/src/hooks/provenance.py:830-834` | Neither the new price-reconciliation wall nor the pre-existing floor wall looks at Offer.total_price (PRICE_FIELD == 'unit_price'), so a bid can state |
| **T-176** | `tickets.json (graph gap) — apps/exchange/src/accept/routes.p` | ORCHESTRATOR ADJUDICATION of the T-169/T-170 vs refuted-zero-callers contradiction, recorded so it cannot be re-litigated. NO ticket in the 123-ticket |
| **T-181** | `apps/trust/tests/test_events_hardening.py (DSN precedence co` | T-151's DSN ORDER RESTS ON A SINGLE ASSERTION IN A SINGLE TEST OF 327. Sabotage S3a (reorder the tuple, i.e. re-ship the exact defect) and S3b (leave  |
| **T-192** | `.swarm-loop/acceptance/test_e6_trust.py (the E6 metric as an` | THE 26/26 THAT MOVED E6 TO TARGET IS A WEAKER INSTRUMENT THAN THE LANE'S OWN UNIT SUITE. 7 of 19 real mutations to the E6 core leave the frozen count  |
| **T-193** | `apps/trust/Dockerfile (COPY set)` | T-065 CRASHES IN THE DEPLOYED IMAGE. apps/trust/Dockerfile does not COPY packages/verification, so trust.verification.verify raises ModuleNotFoundErro |
| **T-194** | `packages/contracts/src/ts/schemas.ts:63-65 (ajv` | THE TWO DOORS DO NOT RUN THE SAME SCHEMA CHECK — 21 measured ok-divergences. TypeScript validates through ajv WITH ajv-formats, so `format: date-time` |
| **T-197** | `apps/buyer/svc/src/profile/__init__.py:436 (_MIN_LEAKABLE = ` | Identity fragments shorter than 4 characters are never tracked ANYWHERE in the leak backstop, so short names walk straight out: first_name='Ann', last |
| **T-198** | `apps/buyer/svc/src/profile/__init__.py:918 (_identity_source` | Two residual identity channels the T-189 fix deliberately stopped short of. (a) A number REGROUPED rather than repunctuated still escapes: 'gift 55501 |
| **T-199** | `apps/buyer/svc/src/profile/__init__.py:1038 (_BUCKET_VOCABUL` | The bucket vocabulary holds every CATEGORY_TAXONOMY label out of the leak haystack on the grounds that they are values a coarsener chose from a fixed  |
| **T-200** | `packages/store-agent/src/hooks/provenance.py (ClaimMaterial,` | ClaimMaterial gained a fifth field, `unwalkable`, as part of the T-155 cycle-detection fix (a cycle or an unwalkably-deep structure is now NAMED rathe |
| **T-203** | `apps/merchant/svc/src/codes/ (T-052) — binding requirement, ` | BINDING REQUIREMENT ON A SCHEDULED TICKET, recorded the way T-041 carried T-152's. The percent-vs-fraction mismatch (T-183) was resolved by adding a c |
| **T-204** | `packages/contracts/openapi/exchange.openapi.json:169 (denial` | REFUSAL REASONS ARE UNENUMERATED AND NOTHING ASSERTS ON THEM. accept() formats type(exc).__name__ into denial_reason, which is PERSISTED into a policy |
| **T-205** | `services/shopify-stub/tests/test_stub_contract.py:40 via con` | SECOND LATENT except-ImportError VACUITY, found by the AST sweep T-159 asked for. The whole module skips if shopify_stub.app:app fails to import. It i |
| **T-207** | `apps/trust/src/scoring/engine.py (weight-table comment)` | The engine's gloss on the weight table CONTRADICTS THE APPROVED MANIFEST on what mismatch_return means. The comment reads it as 'the buyer said it mat |
| **T-208** | `fixtures/manifest.json observation_weights — no weight for a` | There is no published weight for a buyer complaint that is NOT corroborated by a return. It currently lands at mismatch_return's 1.5 — the same as a c |
| **T-209** | `packages/store-agent/src/hooks/provenance.py:129,:496-507 (c` | THE CONTRACTS BOUNDARY WAS TIGHTENED AND THE STORE-AGENT'S OWN AUDITOR WAS NOT. T-195's fix regenerates the model with --strict-nullable so `offer.com |
| **T-211** | `conftest.py:307 (frozen) — documents a timeout that is now 2` | The root conftest still documents the Neo4j lock's '600 s timeout'. The lock now waits 240s (lowered so its own named Neo4jLockTimeout fires inside py |
| **T-218** | `packages/store-agent/src/hooks/provenance.py:523 (`if node i` | T-209 CONFIRMED BUT UNDERSTATED, and the cause is generic rather than specific to commitments. The store-agent's walk short-circuits on ANY None, whil |
| **T-219** | `scripts/verify.sh SELECTION line` | NOTHING CONSUMES THE SELECTION COUNT, so T-117's defect is closed for the `docker` marker but not as a class. The line is advisory text on stdout: `gr |
| **T-220** | `packages/store-agent/tests/test_price_reconciliation.py (dep` | The cycle-detection replacement for MAX_SWEEP_DEPTH is correct, but nothing in the suite would notice a depth bound being silently RE-INTRODUCED above |

## Escalations

`ESC-005` (the per-ticket gate, T-117) and `ESC-006` (the fixtures manifest, T-080) are both
**resolved** — ESC-006 by a human approval on `main`, `ed1bc98` "approve(T-080): Hank Holcomb
approved the recomputed manifest as ground truth". Neither gates an open ticket any more.
`ESC-007` was filed at the cycle-10 close. **T-219 is the live successor to ESC-005**: the gate
now *names* what it did not run, but nothing consumes the count, so a run of 1 test out of 3 976
still exits 0 and still records `build_succeeds` = 1.

The last record in `dispatch.jsonl` is still an unclosed `open` for `rung2-c11` at head
`5e1fb80`. That verification returned — it minted T-214…T-220 — but was never closed in the
ledger, so the dispatch log currently understates what has been done.

