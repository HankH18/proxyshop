# Learnings — process loop (how the swarm is run, not what it builds)

Imperative, specific, curated. Merge duplicates on append; ~30 entries is the ceiling.
Entries are injected selectively into task packets — only those relevant to a task's area.

**This file holds process habits only.** Defects, design rules and anything actionable about
the harness, the gates or the measurement apparatus live in `harness-review.md` (entries
H-1..H-29), which is where the harness work is tracked. A cycle-1 curation pass moved every
issue-shaped entry there; the cycle-13 pass merged eight near-duplicates into their siblings
and left the **31** entries below. If you are about to append something that names a bug, a
tool that misbehaves, or a fix someone should make — it belongs in `harness-review.md`, not
here.

---

## Pre-dispatch and scheduling

- **Probe the runtime environment live before writing a single packet, and probe the HOST as
  well as the stack.** Reading DESIGN told us Neo4j needed a 1024-dim cosine vector index; only
  standing the container up proved it works on this machine — and, in the same session, proved
  Neo4j Community cannot create a second database, which reshapes the entire parallel-isolation
  design. The host half is the same lesson at a different layer: this machine was already
  running Supabase (11 containers), OpenEMR, MariaDB and a pgvector Postgres, consuming ~2.7 GiB
  of a 7.75 GiB Docker VM and holding port 55432, which failed the first compose attempt
  instantly. Doc claims about the environment are hypotheses until a container answers, and port
  and RAM budgets are measured facts, never defaults.

- **Write per-file ownership into `scope` at intake; a directory glob silently serializes
  lanes whose real ownership is disjoint.** `check-wave` intersects the globs, not the truth,
  so six ProxyShop tickets declaring `apps/exchange/tests/**` and `packages/store-agent/tests/**`
  were vetoed against each other in every pair — only one of each trio could ever be in flight,
  for thirteen cycles, while their real ownership (`test_ranking.py` / `test_bandit.py` /
  `test_loss_reports.py`) never overlapped at all. Globs describe neighbourhoods; verify
  commands and per-file scopes describe addresses. **When a veto fires, check whether it is a
  real collision or a coarse glob before you split the wave** — and narrow the scope rather
  than serializing, because narrowing makes the veto SHARPER: two lanes that would genuinely
  write the same file are still refused, and no lane gains a file it could not touch before.

- **Author a greenfield ticket's `verify` against the FROZEN acceptance tests it is graded by,
  never against a test file the lane must itself create.** Such a gate can NEVER satisfy the
  retro red-check: at the merge base the named file does not exist, pytest exits 4 with nothing
  selected, and red-check stamps WEAK — permanently, because the stamp is a fact about the
  COMMAND, not about the branch, so re-running it re-stamps WEAK forever. The veto is right and
  must not be weakened (a gate that selects nothing is the vacuous-gate failure this repo has
  been bitten by repeatedly); what is wrong is the command. A correct gate is red at the base
  **with tests genuinely selected**.

- **Install the complete dependency set in the scaffold ticket, and provision every worktree
  from EVERY committed lockfile — not just the first one found.** Parallel workers may never
  edit a manifest, so any dependency discovered mid-wave becomes a serialized blocking ticket.
  This repo carries both `package-lock.json` and `uv.lock`; provisioning ran only `uv sync`, so
  every lane came up without `node_modules`, and two lanes independently burned time diagnosing
  the same npx failure as if it were their own bug. Enumerate the lockfiles, not the ecosystem
  you happen to be thinking about.

- **Rank the frontier by GRADED-METRIC ERROR CONTRIBUTION, never by `unblocks` count.**
  `unblocks` measures graph *shape* and is blind to the goals you are actually scored on, so a
  ticket worth fifteen acceptance points ranks below a scaffold ticket that gates six cheap
  ones. Thirteen cycles of it left the largest single block of error in the run **never
  dispatched** — zero records across 114 dispatch entries — while adjacent defect tickets were
  polished. The same correction covers the human-gate case that first exposed it: T-080 sat at
  depth 2 but held 15 of 44 tickets behind an approval, and T-013, its only parent, inherited
  that urgency without ever topping the unblock count. Rank by *what moves the measured error*,
  including distance to a human whose signature is on the critical path.

---

## Orchestrator conduct

- **Phase 8 does not end at the ledger commit — it ends at `push-gate` and the push. Run
  both in the SAME turn as the commit.** Measured, cycle 13: the ledger was committed at
  `15958ab` and the orchestrator went straight into an approved amendment and a seven-lane
  dispatch. `push-gate` was never run, so nothing was pushed, and the omission was silent —
  an orchestrator that never pushes prints nothing about not pushing. It surfaced only
  because the user asked why. By then **31 commits** were unpushed: the whole of cycle 13
  plus a merged HIGH security fix, with both remotes still sitting on the cycle-12 record.
  The failure mode is treating the commit as the end of the phase because it *feels* like
  the closing act; the gate's own last condition is that the commit has already happened,
  which is exactly what makes it the step AFTER, not a step you can defer to "later in the
  cycle." There is no later — the next dispatch consumes the turn.
  **Score the two operator conditions explicitly rather than skipping them**, and read the
  push back with `git ls-remote` rather than trusting `git push`'s exit code.

- **The orchestrator did delegable work itself during Phase 0/1 and burned context for it —
  do not repeat this.** Recorded at the user's instruction, because it happened despite the
  rule being known and written down. Specifically: the orchestrator personally ran the
  Docker/Neo4j/Postgres/vitest probes, personally authored `codebase-map.md`,
  `decisions.md`, `learnings.md` and the acceptance runner, and personally read all five
  planning docs — while a 7-agent intake workflow was already running and idle capacity was
  available. Only the skill/reference reading was genuinely non-delegable (the orchestrator
  must hold the protocol it enforces).
  **The rationalisation to watch for is "this is quick" and "I need the facts in my own
  head."** Both are false economies: an agent returns the same verified fact for a fraction
  of the orchestrator's context, and the orchestrator only ever needs the *conclusion*, not
  the transcript that produced it. The context spent is unrecoverable and shortens the run.
  **Rule: during Phase 0/1, the orchestrator's own hands are for decisions, dispatch, merges
  and the protocol — everything else, including environment probing and document authoring,
  goes to a subagent. Route large intake artifacts to FILES that subagents read, never
  through the orchestrator's window.**
  **When you do delegate, hand over the numbers AND tell the agent to re-derive them.** The
  ledger agent was given 28 verified unblock counts and instructed to re-derive rather than
  copy. It reproduced all 28, caught an error in the orchestrator's own framing (four of five
  frontier tickets moved +1, not +2), and independently found four defects nobody had flagged
  — including that `acceptance_collected` is pinned at 120 rather than 103. Handing over facts
  saves the agent's context; demanding re-derivation is what makes the handoff safe.

- **Probe the environment AFTER dispatch too — and correct the packets in flight.** Three facts
  arrived from the readiness probe minutes after the wave went out: `make deps-up` exits non-zero
  without `PROXYSHOP_WORKER` even though every container comes up healthy; a contended Neo4j
  flock surfaces as `pytest-timeout` at 300s instead of its own 600s diagnostic, so the legible
  error D37 was written to produce is unreachable; and two environment variables a pinned
  decision claims the dispatcher exports do not exist anywhere in the repo. All three were sent
  to the affected workers as corrections. A packet is not immutable once dispatched — the cost
  of a follow-up message is trivial against a worker debugging a phantom.

---

## Verification

- **Budget for several adversarial verification rounds; do not treat the first as a
  formality.** T-000 took three rounds and surfaced eight real defects. **The agent that
  wrote the code found zero of them.** Every one came from an agent that had not written it.
  A "verify" round that finds nothing is evidence the verifier was too close to the builder,
  not evidence the build was clean — re-brief and run it again rather than banking the green.

- **Give the verifier a mandate to build, not just to read.** The rounds that found real
  defects were the ones where the verifier wrote its **own** reference implementation of the
  behaviour and its **own** negative control, then compared. A verifier that re-runs the
  fixer's tests and re-reads the fixer's diff inherits the fixer's blind spot by
  construction: it can only confirm that the artifact matches itself. The Redis DB-index
  defect (`from_url` letting the URL's `/1` beat the explicit `db=` kwarg, so every worker
  shared DB 1 and each `flushdb()` wiped its siblings) was found by an independent client
  written from the docs, not by reading the wrapper.
  **The corollary is that a review programme made only of readers has a shaped blind spot.**
  The first defect this run found by a BUILDER rather than an auditor was found by *executing a
  document* — D6 came from an agent that tried to run the thing and watched it fail. Auditors
  read for contradiction; builders discover unrunnability. Staff for both.

---

## Reading the sources of truth

- **Check the executable source of truth against the prose plan before building from the
  prose.** The intake report's §3.1 scaffold tree and §4 ownership map rendered a nested
  `src/<package>/<subpackage>/` layout, while **30 scope globs across 29 of 44 tickets** in
  `tickets.json` use the flat `src/<subpackage>/` form and **zero** use the nested one. The
  ticket graph is what a worker actually obeys; the prose is a description of it that can
  drift. Diff them mechanically, then pin the winner as a decision (D42) so no worker
  re-litigates it.

- **A green gate proves nothing until you have made it go red on purpose — and check that it
  is reading the right artifact at all.** `build_succeeds` was only trusted after an unused
  import drove it to 0 and removing the import drove it back to 1. Sabotage every binary gate
  once, in both directions, before recording its baseline: a gate that has never been observed
  failing is indistinguishable from a gate that cannot fail. The worst instance of "cannot
  fail" this run was an integrity check written against `freeze-log.jsonl` alone — the log
  records that a freeze happened and when, and carries **no per-file digest**, so a gate that
  iterates its entries and reports "no drift" is structurally incapable of detecting drift, in
  the one place where a false green is most expensive. The hash map lives in `manifest.json`'s
  16-entry `files` map. Point the check at a mutated copy and watch it go red before trusting
  a green.

- **Prove the shared fixture layer in one place, before dispatch, or every ticket
  re-discovers it separately.** `pg_role` — the least-privilege factory ~10 tickets depend on,
  carrying the D5/C3/S7 grant proof — was exercised by nothing until a verifier noticed. Same
  round: every Postgres test silently skipped because nothing created `proxyshop_w<N>`. **For
  each scaffold fixture, ask "which committed test fails if I break this?" and write that
  test if the answer is none.**

- **When a fix relocates behaviour, re-read every comment and docstring that described the
  old location in the same commit.** The D37 Neo4j lock moved from one directory's conftest
  to the root conftest for every session, and the frozen datastore-proof docstring still
  describes the deleted override. Agents read those docstrings to learn the model, so a stale
  one is a defect that propagates into other tickets' code.

- **Re-derive the ticket graph from the executable source at every dispatch, give `frontier` a
  closure source EVERY time, and reconcile the ledger against the graph at every resume.**
  `backlog.md` claimed 45 tickets; `tickets.json` had 47. A user-approved harness amendment had
  rewritten five dependency edges, added two tickets, retitled two and widened one scope — and
  the ledger, which is what the scheduler reads, recorded none of it. The frontier happened to
  be unchanged, so nothing was mis-dispatched, but that was luck. The sharper form of the same
  failure: with no closure source wired, `frontier` reported **`0 closed` on every invocation**
  for thirteen cycles. That is a **broken instrument, not a scheduling opinion** — with nothing
  recorded as closed it degenerates to the graph's roots and ranks the already-built scaffold
  first, forever. Treat a suspiciously round `0 closed` as an error to diagnose, never as an
  answer to act on. A ledger is a cache of the graph, and a cache nobody invalidated is the
  default state.

- **Read a ticket's own acceptance text against the frozen fixtures before quoting it into a
  packet — the ticket can be the wrong one.** T-010's acceptance 5 says a `Bid` missing any of
  `signer_id/key_id/issued_at/nonce/schema_version` must fail validation. The frozen `Bid`
  fixture carries none of those four and is asserted to validate at four control sites. Read
  literally, the ticket demands the suite be broken. The resolution (a separate `SigningEnvelope`
  type) is inferable from the objective, but only if someone checks — and the packet, not the
  worker, is where that check belongs.

- **An integrity check written against `freeze-log.jsonl` alone silently finds nothing — the
  hash map lives only in `manifest.json`.** The freeze log records that a freeze happened and
  when; it carries no per-file digest. So a harness-integrity gate that reads the log, iterates
  its entries and reports "no drift" is structurally incapable of detecting drift — it is the
  project's recurring *guard that rejects nothing*, in the one place where a false green is
  most expensive. Verify against `manifest.json`'s 16-entry `files` map, and confirm the check
  can fail: point it at a mutated copy and watch it go red before trusting a green.

- **`rev-list --count main..<branch>` answers "ahead", and the reciprocal question is the one
  that bites.** Seven lanes were dispatched onto worktrees 26-27 commits behind main; the
  ahead-count read 0 for two of them, which is what "never dispatched" and "dangerously stale"
  both look like from that side. Staleness is invisible to every gate that matters:
  `git merge-tree` was CLEAN for all five populated lanes, because textual mergeability is
  unaffected by an old base. The failure mode is a lane coding against a superseded API,
  merging clean, and breaking at runtime. Check `rev-list --count $(git merge-base main $b)..main`
  at dispatch, and then do the part that actually decides the response: intersect the changed
  files with each lane's *working scope*. Five of seven lanes had zero intersection and were
  correctly left alone mid-flight; two were building on rewritten ground. A blanket rebase and
  a blanket shrug are both wrong.
  **Scope-intersection is necessary and not sufficient — a lane need not EDIT a changed file to
  DEPEND on one.** The follow-up check is a reference scan of each lane's own directory (not the
  whole worktree — every worktree is a full checkout, so an unscoped grep counts files no lane
  owns and returns junk). Four of five "inert" lanes did reference changed modules; reading the
  diffs is what settled it — ~1050 of 1198 insertions were new tests, and the `src/` changes were
  docstring corrections plus one strictly-stricter regex and one purely additive class. Ancestry
  says *whether* to look; the diff says whether it *matters*.

- **When a session's findings are "lost with its context", search `git log --all --format=%B`
  before concluding they are gone.** Two of four HIGH defects believed unrecoverable were sitting
  in the branch tips' own commit messages. The orchestrator's practice when killing an agent
  mid-task was to preserve the work in a `WIP(...)` commit and write the *intent* — mechanism,
  file:line, and the required acceptance test — into the message body. That makes preservation
  commits the highest-yield archaeology target in the repo, above escalations.jsonl, decisions.md
  and the analysis JSONs, none of which held these. The specific miss worth not repeating: one
  WIP message was read and mined, the sibling WIP message on another branch never was, and an
  agent was dispatched to re-derive blind a defect the repository already recorded in full.
  Read every preservation commit's body, not just the one that happens to be in front of you.
  **And forward a recovered defect's *required test* even when the code is already fixed.** The
  T-040 agent had independently derived the fix (binding a discount claim to its product ref)
  before the original text was recovered. What the recovered text still added was the acceptance
  criterion: "mint for product A and prove it cannot be used for B." An agent that derived only
  the ref-format change would naturally assert on the ref *string*, and that assertion passes
  while cross-product replay still works. The fix and the proof of the fix are separately
  losable; recovering one does not recover the other.

- **Measure a weak guard's actual catch rate instead of accepting the reported one.** The
  store-agent lint was recorded as "at least three ordinary spellings pass it." Measured, it
  caught **0 of 3** — `Claim as Fact`, `P = Provenance`, and `Claim.model_validate(row)` all
  sailed through. "Some inputs evade it" and "it rejects nothing" are different severities and
  the difference is one experiment. Every fix for such a guard needs a negative test too, or the
  repair is just a guard that matches more of everything.

  *The same rule applied to preserved work: INTENT is reliable, PROGRESS estimates are not.*
  `7cdc9db` described itself as "probably less than half the fix" and was 0% effective — its 51
  lines had zero call sites repo-wide, were absent from both `__all__` lists, never touched the
  enforcement function, and carried docstrings asserting behaviour that did not exist, so the
  suite was green over a fully live HIGH. Its sibling `935ddaa` — same author, same session, same
  kill — described itself as "substantially fixed" and the wiring check found it genuinely wired
  at two independent points on the real command path. Self-assessed progress is therefore not
  reliably biased in either direction; it was 100% generous on one and roughly accurate on the
  other. **Run the call-site-and-`__all__` check on every preserved commit regardless of what its
  message claims.** Neither prose reading was the discriminator — a two-command grep was. And
  note why this class is so expensive: dead code that *describes* the guarantee reads exactly like
  code that *provides* it, in a diff, in a review, and to the next agent.

  *And the static check does not finish the job.* Wiring proves the call exists on the path, not
  that the refusal fires. In the fixtures case `load_manifest()` was already raising
  `DigestMismatchError` correctly on a tampered tree **while the defect was live** — so a test
  proving *some* function detects the tamper passes without touching the bug. Name the function
  whose refusal is the guarantee, and assert on that one.

- **A correct, firing, well-named guard sitting one call away from a defect proves nothing about
  that defect.** This is the sharpest instance the project has produced, and it is a harder case
  than the freeze-log trap above. There, a guard could not fail. Here, `load_manifest()` **was**
  detecting the tampered golden file and raising `DigestMismatchError` correctly — at the same
  moment `record_approval()` accepted the same tampered tree and re-sealed it. Nothing was broken
  about the working guard; it simply was not the one carrying the guarantee. So "confirm the check
  can fail before trusting a green" is necessary but insufficient: a check that demonstrably fails
  on bad input still tells you nothing unless it is the check on the path the guarantee runs
  through. Before writing a regression test, name the exact function whose refusal *is* the
  guarantee, and assert on that function — not on a neighbour that happens to inspect the same
  bytes.

- **A findings ledger minted by string-formatting the source records is lossy, and the loss is
  invisible at the point of use.** T-130's eight MEDIUMs were generated with
  `f"[{severity}] {lane}: {str(location).split('/')[-1][:60]} - {str(defect)[:170]}"`. Three
  independent defects in one line: `.split('/')[-1]` keeps only the text after the LAST slash, so
  a finding with MULTIPLE locations silently loses every path but one (four of eight hit this, and
  one lost a second `hmac.compare_digest` call site on a public unauthenticated endpoint that then
  appeared in NO on-disk record); `[:170]` truncated six of eight mid-sentence, dropping the
  consequence clause and, in one case, an explicit guardrail saying which behaviour must NOT be
  weakened by the fix; and the `reproduction` field was never read at all, so every executed repro
  was lost. The result reads like a complete ledger — correct format, plausible paths, one entry
  per finding — while pointing four sweeps at files that do not contain the defect. **Never derive
  the durable record from a lossy projection of a richer one; serialise the structured finding.**
  If a summary line is wanted for humans, it belongs BESIDE the record, never instead of it.

- **A blind adversarial audit covers guard-correctness well and deployment-wiring badly.** Measured
  across five lanes auditing their own code without the prior session's findings: every lane found
  logic defects in guards it could reason about locally, and the "the code is right but nothing
  calls it" class was systematically under-weighted. The buyer lane HAD noticed that
  `auth_service()` builds a bare `MagicLinkAuth()` with every store in-memory, and filed it as
  "MEDIUM, wiring incomplete, report don't fix" — never working out that this made
  `PostgresPseudonymStore` and `publish_profile` unreachable dead code, nor that sessions therefore
  break across replicas. It took an external pointer to get fixed. The same class showed up in
  three other lanes (a checkout seam that exists but is not wired, a generator whose `stores`,
  `catalog` and `event_script` output is discarded, an enforcement function with no production
  caller). **So audit briefs should name the wiring class explicitly and ask for it separately:**
  "for each capability you built, name its production call site, or state that it has none."
  A local reasoner does not spontaneously ask whether anything upstream calls it.

- **Dispatch briefs carried three factual errors in one wave; every one was caught by an agent
  measuring the premise instead of accepting it. Instruct for that explicitly.** The errors, all
  mine, and each is its own transferable trap:
  1. *"That file was rewritten on main" does not imply its public surface moved — measure the
     surface, not the diff.* I cited `apps/trust/src/ledger/__init__.py:141` and warned a lane
     that the public surface "may have moved". The ancestry fact was true; the inferred
     consequence was wrong. The rewrite (T-126) was entirely import-sequencing machinery,
     `__all__` and the `_LAZY` map were BYTE-IDENTICAL to the stale copy, and `canonical.py`,
     `chain.py`, `store.py`, `replay.py`, `errors.py` were untouched — only a line number moved
     (141 → ~208). Cite line numbers as hints that may have drifted, never as contract; hand a
     lane the ancestry fact and let it measure impact, rather than shipping an inference about
     impact as if it were measured.
  2. *A prescribed fix derived from one exemplar is a hypothesis about the defect class, not a
     solution.* I forwarded a recovered defect with a fix — "match whole normalised tokens
     against individual bucket values, not unanchored substrings of a concatenation" — that would
     NOT have closed the lane's own broader case: `none@example.com` on a fresh account, where
     `frequency_tier == "none"` makes the fragment `"none"` EQUAL a bucket value exactly rather
     than be a substring of one. The lane's independent fix covered my case; mine did not cover
     theirs. Hand over the repro and the constraint, and let the lane derive the fix.
  3. *A named build target can simply be the wrong one.* I told the T-020 lane to build against
     the shopify-stub, which is an Admin-GraphQL/control-plane service with no `/products.json`,
     `/robots.txt`, `/password` or product HTML — structurally incapable of being a storefront.
     That lane verified the routes, concluded the brief was wrong, and built its own raw-ASGI
     storefront fixture in its own scope.

  None of the three cost real work, because each lane checked before building. The transferable
  rule: **a brief is a hypothesis, and should say so.** Tell every agent that file:line references
  may have drifted, that a named test target may be the wrong one, and that a prescribed fix is a
  starting point — and ask each to report, in its final message, any premise of the brief it found
  to be false. The lanes that did this volunteered corrections; a lane told only to "follow the
  brief" would have built against a stub with no storefront routes and reported the failure as its
  own.

## The T-132 "REPAIR" guidance is mathematically wrong — do not follow it (cycle 9)

`.swarm-loop/reports/T-132-amendment-design-REFUTED.json` tells the next author to assert,
over a generated population:

    min(class) >= K   AND   len(classes) <= len(pop)//K

claiming the second half "kills the degenerate implementation that collapses everything
into one class". It does not, and this was carried forward verbatim in a session handoff
as "the key one".

The second condition is IMPLIED BY the first: sizes sum to N and each is >= K, so
N >= C*K, so C <= N/K. Always. And the degenerate implementation — one class holding all
N buyers — passes BOTH: min(class) = N >= K, and len(classes) = 1 <= N//K. Measured, not
argued: at N=4000, K=5 the collapse-everything implementation returns True for both.

What actually catches the degenerate case is a LOWER bound on the class count
(`len(classes) >= some floor`), plus a utility assertion that a stated fraction of buyers
retain each facet. Anyone following the recorded guidance gets a test that cannot catch
the failure it was written to catch.

Found by the lane that was told to implement it, which is the point: a brief is a
hypothesis, and the agent executing it is the one positioned to falsify it. Say so in
every packet.

## Disjoint file scopes are not disjoint state — the worktree AND the scratchpad are shared

Cycle 10, T-031. The lane did what its packet asked — ran its own adversarial pass — but
spawned sub-agents that mutated the SAME worktree it was building in. Two of them collided:
the test auditor "observed `if False:` appear in rerank.py between its own restore and its
next read" (the sibling's concurrent mutation testing), and its whole first run's results
were unusable. Worse, one sub-agent had been told it could revert source files with
`git checkout` — which would have silently destroyed 112 lines of uncommitted fix sitting in
the tree. The PreToolUse git-guard is what stood between that instruction and the loss.

Cycle 13 produced the second surface of the same physics: **the orchestrator's scratchpad
directory is SHARED across a session's subagents, not per-lane.** One lane overwrote another
lane's probe script mid-run, with no error from either side — same filename, same directory,
two writers.

The skill's isolation invariant is written about the ORCHESTRATOR's waves; the same physics
applies one level down, and nothing in the packet said so.

APPLY, in every packet:
- your sub-agents are READ-ONLY in your worktree; if one must mutate to test (sabotage,
  mutation testing), it works on its own copy outside the tree, never in yours;
- no sub-agent may run a repo-global git op, ever;
- **prefix every scratch filename with your ticket id** (`T-044-probe.py`, not `probe.py`),
  because the scratchpad is one directory shared by every lane in the session.

## Name task branches `task/<ticket-id>` exactly — a cycle prefix silently breaks three commands

Cycle 13. A branch named `task/C14-T044` rather than `task/T-044` breaks three harness commands
that parse the ticket id out of the branch name: `conflicts`, `frontier --closed-from-merged`,
and `check-wave`'s fork accounting. None of them errors. Each reports "nothing to compare" or
`0 closed` — a plausible, quiet, wrong answer, which is the most expensive failure shape this
repo has, and the same shape as the `0 closed` frontier above.

APPLY: the branch name is a machine key, not a label. No cycle prefix, no lane number, no
description — `task/<ticket-id>` and nothing else. Put the cycle in the commit message.

## Assert a lane's worktree is CLEAN before believing anything it reported

Cycle 10, T-031. The lane's verifier found the worktree dirty at the moment it was briefed:
`criteria.py` and `test_retrieval.py` carried 112 uncommitted lines relative to the commit it
was told to judge, and the "verified facts" it was handed (46 tests green, verify.sh exit 0)
had been measured against the DIRTY tree, not against the commit. The pinned SHA was
therefore not what produced the numbers.

Pinning the tip is necessary and NOT sufficient: a tip pins what was committed, and says
nothing about what is sitting uncommitted beside it.

APPLY: at collection, run `git -C <worktree> status --porcelain` and require it EMPTY before
accepting a branch. If it is non-empty the lane has work it never committed — ask for it to be
committed and re-pin, rather than merging a tip whose reported greens came from other bytes.

## CodeGraph does not work from a worktree — its index is gitignored and never comes along

Cycle 10. Every packet this cycle told its lane to prefer `codegraph explore` over grep. In a
worktree that instruction is actively harmful: `.codegraph/` is untracked, so `git worktree add`
does not bring it, and the CLI silently resolves some OTHER index instead of failing. Measured
side by side on the same query:

  primary tree      "validate_bid boundary" -> 48 symbols / 3 files, correctly naming
                    packages/contracts/src/boundary.py:225 and its 11 callers
  worktree (T-135)  same query -> 25 symbols / 1 file of PYDANTIC internals (validate_call,
                    AnyCallableT, FORMAT_FUNCTIONS) — none of them this repo

The T-135 lane caught it and fell back to direct reads; it reported foreign source from
unrelated repositories. Any lane that did NOT notice acted on another codebase's symbols while
believing it was reading its own. This is worse than an unavailable tool, because it answers.

APPLY: in every worktree packet say — CodeGraph is available in the PRIMARY checkout only; from
your worktree use `git grep` and direct reads, and if you run `codegraph explore` at all, verify
the returned paths are inside your tree before believing a word of it. The orchestrator keeps
CodeGraph for its own primary-tree work, where it is correct.

## CORRECTED — `retire` finds worktrees outside the recorded root; the idle guard is what holds them

**This entry previously said the opposite and was wrong.** It claimed that a missing
`worktree_root` in state.json made `retire` unable to find the run's worktrees. An adversarial
audit refuted that at the source: `retire` and `resume` BOTH find worktrees outside the recorded
root, through an ownership union built for exactly this case — a tree counts as the run's if its
branch is under `refs/heads/task/`, regardless of where it sits on disk. So the trees were found
and reported all along.

The real reason `retire --dry-run` printed "0 retired · 5 kept" was the **900-second idle guard**
plus one branch that had not landed — and both reasons were printed on the kept lines, which the
orchestrator did not read before diagnosing. The bare `retire` the checkpoint banner recommends
scans at idle 0 while the subcommand defaults to 900, so the banner names a command that will
reclaim nothing at the moment it fires.

What IS true and worth keeping: with no `worktree_root` recorded, `retire` and `checkpoint`
print a worktree-root path that does not exist and report the root's size as unmeasured, which
reads like "found nothing" and is what misled the diagnosis.

APPLY: pass `--worktree-root` at init for accurate reporting — but when teardown reclaims
nothing, READ THE KEPT LINES before concluding anything: they state the reason per tree. Prefer
`retire --idle-seconds 0` when acting on the checkpoint's own banner. And treat this entry as
the standing example of the failure it describes: a confident diagnosis written from a summary
line instead of from the tool's own explanation of itself.

## "Zero production callers": the discriminator is whether a FROZEN TEST already counts it

Cycle 10 refuted seven findings whose whole content was "this landed symbol has no production
caller", then minted two HIGH findings of apparently the same shape hours later. The ledger
regeneration caught the contradiction. Adjudicated at ground truth, and the rule generalises:

**An unwired half is scheduled work. It becomes a DEFECT the moment a frozen acceptance test
counts it as satisfying a requirement.**

Applied to the actual cases:
- T-136/T-141/T-142/T-147/T-148/T-150 — no frozen test asserts any of them is wired, so "no
  caller" means "the consumer is a scheduled ticket". REFUTED, correctly.
- T-169 — the frozen S8-3 release blocker `test_offdomain_checkout_url_is_refused` PASSES, and
  it passes by comparing the permalink host against `bid['store_domain']`, a BIDDER-CONTROLLED
  field, because no registry is ever wired. A bidder that lies consistently (store_domain and
  checkout_url both attacker.tld) defeats it, and the test cannot see that because it never
  wires a registry. The metric therefore counts a security property as met that is not enforced.
  NOT the zero-caller pattern. HIGH stands.
- T-170 ("the accept package ships no routes.py") — checked: NO frozen test asserts an HTTP
  accept endpoint (no TestClient in the acceptance suite), and T-052 owns
  apps/merchant/svc/src/codes/**, not the exchange route. So this IS the zero-caller pattern.
  DOWNGRADE from HIGH to a graph gap: no ticket in the 123-ticket graph owns
  apps/exchange/src/accept/routes.py, which is worth recording precisely because nothing will
  otherwise schedule it.

APPLY: before accepting or refuting any "nothing calls this" finding, ask ONE question — does a
frozen acceptance test currently pass because of this thing? If yes it is a measurement-
credibility defect regardless of callers; if no it is unfinished work, and the answer is a
schedule, not a ticket. Record which answer you got, so the next pass cannot re-litigate it.
