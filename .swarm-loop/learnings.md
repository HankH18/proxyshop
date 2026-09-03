# Learnings — process loop (how the swarm is run, not what it builds)

Imperative, specific, curated. Merge duplicates on append; ~30 entries is the ceiling.
Entries are injected selectively into task packets — only those relevant to a task's area.

**This file holds process habits only.** Defects, design rules and anything actionable about
the harness, the gates or the measurement apparatus live in `harness-review.md` (entries
H-1..H-29), which is where the harness work is tracked. A cycle-1 curation pass moved every
issue-shaped entry there and left the seventeen below. If you are about to append something
that names a bug, a tool that misbehaves, or a fix someone should make — it belongs in
`harness-review.md`, not here.

---

## Pre-dispatch and scheduling

- **Probe the runtime environment live before writing a single packet.** Reading DESIGN told
  us Neo4j needed a 1024-dim cosine vector index; only standing the container up proved it
  works on this machine — and, in the same session, proved Neo4j Community cannot create a
  second database, which reshapes the entire parallel-isolation design. Doc claims about the
  environment are hypotheses until a container answers.

- **Check the host for pre-existing containers and bound ports before choosing any port or
  memory budget.** This machine was already running Supabase (11 containers), OpenEMR,
  MariaDB and a pgvector Postgres, consuming ~2.7 GiB of a 7.75 GiB Docker VM and holding
  port 55432 — which failed the first compose attempt instantly. Port and RAM budgets are
  measured facts, never defaults.

- **Derive the file-ownership map from the tickets' `verify` commands, not their `scope`
  globs.** Six ProxyShop tickets declare `apps/exchange/tests/**` and seven declare
  `apps/trust/tests/**`, which reads as a mass collision; each ticket's verify command names
  one distinct test file, which is the real ownership. Globs describe neighbourhoods,
  verify commands describe addresses.

- **Install the complete dependency set in the scaffold ticket.** Parallel workers may never
  edit a manifest, so any dependency discovered mid-wave becomes a serialized blocking
  ticket. Enumerate every dependency all tickets will need while planning, and buy it once.

- **Rush a human-gated ticket to the front of the frontier regardless of its depth.** T-080
  sits at depth 2 but transitively blocks 15 of 44 tickets behind a human approval. Scheduled
  by depth it would be reached late, and the gate would then idle the run; scheduled by
  leverage, the approval request reaches the human while they are still awake. Extend the
  same reasoning one hop back: **T-013 is not the highest-unblock ticket on the frontier but
  it is T-080's only parent**, so it inherits the gate's urgency. Rank by *distance to the
  human*, not by unblock count alone.

---

## Orchestrator conduct

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

- **Probe the environment AFTER dispatch too — and correct the packets in flight.** Three facts
  arrived from the readiness probe minutes after the wave went out: `make deps-up` exits non-zero
  without `PROXYSHOP_WORKER` even though every container comes up healthy; a contended Neo4j
  flock surfaces as `pytest-timeout` at 300s instead of its own 600s diagnostic, so the legible
  error D37 was written to produce is unreachable; and two environment variables a pinned
  decision claims the dispatcher exports do not exist anywhere in the repo. All three were sent
  to the affected workers as corrections. A packet is not immutable once dispatched — the cost
  of a follow-up message is trivial against a worker debugging a phantom.

- **Give the file-writing recon agent the numbers AND tell it to re-derive them.** The ledger
  agent was handed 28 verified unblock counts and instructed to re-derive rather than copy. It
  reproduced all 28, caught an error in the orchestrator's own framing (four of five frontier
  tickets moved +1, not +2), and independently found four defects nobody had flagged — including
  that `acceptance_collected` is pinned at 120 rather than 103. Handing over facts saves the
  agent's context; demanding re-derivation is what makes the handoff safe.

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

- **The first defect found by a BUILDER rather than an auditor was found by executing a
  document.** Every other defect this run came from an adversarial reader. D6 came from an
  agent that tried to run the thing and watched it fail. Auditors read for contradiction;
  builders discover unrunnability. Both are needed, and a review programme made only of
  readers has a blind spot shaped exactly like this.

---

## Reading the sources of truth

- **Check the executable source of truth against the prose plan before building from the
  prose.** The intake report's §3.1 scaffold tree and §4 ownership map rendered a nested
  `src/<package>/<subpackage>/` layout, while **30 scope globs across 29 of 44 tickets** in
  `tickets.json` use the flat `src/<subpackage>/` form and **zero** use the nested one. The
  ticket graph is what a worker actually obeys; the prose is a description of it that can
  drift. Diff them mechanically, then pin the winner as a decision (D42) so no worker
  re-litigates it.

- **A green gate proves nothing until you have made it go red on purpose.** `build_succeeds`
  was only trusted after an unused import drove it to 0 and removing the import drove it back
  to 1. Sabotage every binary gate once, in both directions, before recording its baseline —
  a gate that has never been observed failing is indistinguishable from a gate that cannot
  fail.

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

- **Re-derive the ticket graph from the executable source at every dispatch, never from the
  ledger's own prose.** `backlog.md` claimed 45 tickets; `tickets.json` had 47. A user-approved
  harness amendment had rewritten five dependency edges, added two tickets, retitled two and
  widened one scope — and the ledger, which is what the scheduler reads, recorded none of it.
  The frontier happened to be unchanged, so nothing was mis-dispatched, but that was luck: the
  same staleness had already moved the deepest ticket, the gate count and every unblock number.
  A ledger is a cache of the graph, and a cache nobody invalidated is the default state.

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

- **A lane need not EDIT a changed file to DEPEND on one, so scope-intersection is necessary
  but not sufficient.** The follow-up check is a reference scan of each lane's own directory
  (not the whole worktree — every worktree is a full checkout, so an unscoped grep counts files
  no lane owns and returns junk). Four of five "inert" lanes did reference changed modules;
  reading the diffs is what settled it — ~1050 of 1198 insertions were new tests, and the `src/`
  changes were docstring and comment corrections plus one strictly-stricter regex and one purely
  additive class. Ancestry says *whether* to look; the diff says whether it *matters*.

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

- **A recovered defect's *required test* is worth forwarding even when the code is already
  fixed.** The T-040 agent had independently derived the fix (binding a discount claim to its
  product ref) before the original text was recovered. What the recovered text still added was
  the acceptance criterion: "mint for product A and prove it cannot be used for B." An agent
  that derived only the ref-format change would naturally assert on the ref *string*, and that
  assertion passes while cross-product replay still works. The fix and the proof of the fix are
  separately losable; recovering one does not recover the other.

- **A preservation commit's INTENT is reliable; its PROGRESS estimate is not. Check whether the
  preserved code is wired before crediting it.** `7cdc9db` described itself as "probably less
  than half the fix." It was 0% effective: the 51 lines it added had zero call sites repo-wide,
  were absent from both `__all__` lists, never touched the enforcement function, and carried
  docstrings asserting behaviour ("refuses one presented in a bid about a different product")
  that did not exist. The suite was green over a fully live HIGH. Dead code that *describes* the
  guarantee reads exactly like code that *provides* it — in a diff, in a review, and to the next
  agent. The cheap discriminator is a call-site grep and an `__all__` check, not reading the
  implementation.

- **Measure a weak guard's actual catch rate instead of accepting the reported one.** The
  store-agent lint was recorded as "at least three ordinary spellings pass it." Measured, it
  caught **0 of 3** — `Claim as Fact`, `P = Provenance`, and `Claim.model_validate(row)` all
  sailed through. "Some inputs evade it" and "it rejects nothing" are different severities and
  the difference is one experiment. Every fix for such a guard needs a negative test too, or the
  repair is just a guard that matches more of everything.

  *Refinement, from the sibling commit.* `935ddaa` — same author, same session, same kill —
  described itself as "substantially fixed" and the wiring check found it genuinely wired at two
  independent points on the real command path, with the dangerous alternative (`refresh_digests`)
  having zero call sites anywhere in that path. So self-assessed progress is not reliably biased
  generous; it was 100% generous on one commit and roughly accurate on its sibling. The rule that
  survives both cases is narrower and cheaper: **run the call-site-and-`__all__` check on every
  preserved commit regardless of what its message claims in either direction.** Treating "it said
  less than half" as informative would have wasted effort; treating "it said substantially fixed"
  as informative would have under-credited the previous session. Neither prose reading was the
  discriminator — a two-command grep was.

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

- **Check whether a prescribed fix actually covers the reporter's own repro before prescribing it.**
  I forwarded a recovered defect with a fix — "match whole normalised tokens against individual
  bucket values, not unanchored substrings of a concatenation" — and it would NOT have closed the
  lane's own broader case: `none@example.com` on a fresh account, where `frequency_tier == "none"`
  makes the fragment `"none"` EQUAL a bucket value exactly rather than a substring of one. The
  lane's independent fix (hold the coarseners' closed vocabulary and the account's own category
  slugs out of the search) covered my case; mine did not cover theirs. Both halves were needed.
  A prescribed fix derived from one exemplar is a hypothesis about the defect class, not a
  solution — hand over the repro and the constraint, and let the lane derive the fix.

- **"That file was rewritten on main" does not imply its public surface moved. Measure the surface,
  not the diff.** A watchdog correctly flagged that a lane was building over a stale copy of
  `apps/trust/src/ledger/__init__.py`, which wave 4 had rewritten. The ancestry fact was true and
  the warning was worth sending. But the CONSEQUENCE inferred from it — "the module's public
  surface may have moved, so re-derive against it" — was wrong, and the lane checked rather than
  believing it: the rewrite (T-126) was entirely import-sequencing machinery, `__all__` and the
  `_LAZY` map were BYTE-IDENTICAL to the stale copy, and `canonical.py`, `chain.py`, `store.py`,
  `replay.py`, `errors.py` were untouched. Only a line number moved (141 -> ~208). So: cite line
  numbers as hints that may have drifted, never as contract; and when warning a lane about
  staleness, hand it the ancestry fact and let it measure the impact, rather than shipping an
  inference about impact as if it were measured. The resync was still correct — it taught that lane
  a real defect (`trust.events` would have shipped the identical two-spellings bug) — but for a
  reason nobody predicted.

- **Dispatch briefs carried three factual errors in one wave; every one was caught by an agent
  measuring the premise instead of accepting it. Instruct for that explicitly.** The errors, all
  mine: (1) I cited `apps/trust/src/ledger/__init__.py:141` and warned the public surface "may have
  moved" — the surface was byte-identical and only the line number moved; (2) I forwarded a
  recovered defect WITH a prescribed fix that did not cover the reporting lane's own broader repro;
  (3) I told the T-020 lane to build and test against the shopify-stub, and the stub is an
  Admin-GraphQL/control-plane service with no `/products.json`, `/robots.txt`, `/password` or
  product HTML — structurally incapable of being a storefront. That lane verified the routes,
  concluded the brief was wrong, and built its own raw-ASGI storefront fixture in its own scope.
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

## A build lane's OWN sub-agents must not mutate the lane's worktree concurrently

Cycle 10, T-031. The lane did what its packet asked — ran its own adversarial pass — but
spawned sub-agents that mutated the SAME worktree it was building in. Two of them collided:
the test auditor "observed `if False:` appear in rerank.py between its own restore and its
next read" (the sibling's concurrent mutation testing), and its whole first run's results
were unusable. Worse, one sub-agent had been told it could revert source files with
`git checkout` — which would have silently destroyed 112 lines of uncommitted fix sitting in
the tree. The PreToolUse git-guard is what stood between that instruction and the loss.

The skill's isolation invariant is written about the ORCHESTRATOR's waves; the same physics
applies one level down, and nothing in the packet said so. Disjoint file scopes are not
disjoint state.

APPLY: every task packet that tells a lane to run its own adversarial pass must also say —
your sub-agents are READ-ONLY in your worktree; if one must mutate to test (sabotage, mutation
testing), it works on its own copy outside the tree, never in yours; and no sub-agent may run
a repo-global git op, ever.

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
