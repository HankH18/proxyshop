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
