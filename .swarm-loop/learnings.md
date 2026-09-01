# Learnings — process loop (how the swarm is run, not what it builds)

Imperative, specific, curated. Merge duplicates on append; ~30 entries is the ceiling.
Entries are injected selectively into task packets — only those relevant to a task's area.

---

## Pre-dispatch (from Phase 0/1 intake, before wave 1)

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
  sits at depth 2 but transitively blocks 12 of 44 tickets behind a human approval. Scheduled
  by depth it would be reached late, and the gate would then idle the run; scheduled by
  leverage, the approval request reaches the human while they are still awake.

- **Make the frozen acceptance suite hermetic and lazily-importing.** Tests that import
  product code at module scope turn every unmet goal into a collection error, which the
  runner cannot distinguish from a broken measuring stick. Import inside the test function,
  and cut the frozen suite off from the project's root conftest (`--confcutdir`) so no
  worker's fixture can reach the goals.

- **Smoke the metric runner against synthetic pass/fail/skip/missing cases before freezing
  it.** Five minutes with four throwaway tests proved skipped-is-not-passed, that a missing
  epic fails loudly with empty stdout, and that lazy-import failures count as ordinary test
  failures — all three of which the regression depends on and none of which are visible by
  reading the code.

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
