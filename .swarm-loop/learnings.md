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
  sits at depth 2 but transitively blocks 15 of 44 tickets behind a human approval. Scheduled
  by depth it would be reached late, and the gate would then idle the run; scheduled by
  leverage, the approval request reaches the human while they are still awake. Extend the
  same reasoning one hop back: **T-013 is not the highest-unblock ticket on the frontier but
  it is T-080's only parent**, so it inherits the gate's urgency. Rank by *distance to the
  human*, not by unblock count alone.

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

---

## Cycle 0 (T-000 scaffold build, verification ladder, harness freeze)

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

- **Hunt cross-lane defects deliberately at the integration gate — no per-ticket gate can
  see them.** `make verify` self-deadlocked for 600 s the moment two lanes each took a
  session-scoped `fcntl.flock` inside one process. Every individual ticket's verify passed;
  the defect existed only in the pair. Mid-run it would have detonated looking like a hang,
  at the worst possible moment to diagnose. **Add an explicit "run two lanes that share a
  surface, in one process" step to the integration gate** and treat single-ticket green as
  saying nothing about it.

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

- **Ask what a passing gate is evidence OF, then check the skip count.** With the compose
  stack down, `make verify` is green at 95 passed / 15 skipped — and all 15 skipped are the
  `@pytest.mark.docker` datastore tests, i.e. exactly the layer the gate looks like it is
  proving. Skips are silent; the number is not. **Record passed AND skipped alongside every
  binary gate reading**, and bring dependencies up before a measurement sweep.

- **Delete the convenience that creates a forgeable path, and measure its cost before
  defending it.** Report-reuse (`--write-report`/`--from-report`) was built to amortize
  twelve metrics over one suite pass, and two independent verifiers then drove both the
  pass-rate and its companion count to target from a **one-line, worker-written JSON file**
  while `verify` still reported the harness intact. Hashing the frozen directory into the
  report does not close it — workers can read that directory and compute any hash they must
  match. The whole twelve-metric sweep costs ~17 s of real runs. **When a shortcut in the
  measuring apparatus turns out to be forgeable, the first question is what it actually
  bought; here, nothing.**

- **State hermeticity as PARTIAL and name the residual, never as sealed.** Three metric
  exploits were closed in the frozen runner (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`,
  `-o pythonpath=`, pinned discovery patterns). The fourth — product code tampering with the
  scorer's in-process state — is structural to any in-process black-box suite and cannot be
  closed by hardening. Carry it as a written residual risk. A harness described as sealed
  stops being audited; one described as partial keeps being audited at exactly the seam that
  is still open.

- **Prove the shared fixture layer in one place, before dispatch, or every ticket
  re-discovers it separately.** `pg_role` — the least-privilege factory ~10 tickets depend on,
  carrying the D5/C3/S7 grant proof — was exercised by nothing until a verifier noticed. Same
  round: every Postgres test silently skipped because nothing created `proxyshop_w<N>`. **For
  each scaffold fixture, ask "which committed test fails if I break this?" and write that
  test if the answer is none.**

- **A one-line trip hazard in shared test scaffolding takes down a whole directory, including
  already-merged work.** A duplicate fixture name killed an entire test directory at
  conftest-import time — merged tickets' tests included. Shared conftest changes are
  integration changes: gate them with a repo-wide collect (`pytest --collect-only -q` across
  every test root) rather than the touched directory alone.

- **`uv sync --frozen` exits 0 against a changed manifest — never take a package manager's
  exit code as proof the environment matches the manifest.** A dependency addition became a
  silent no-op. Assert the *effect* (the module imports, the version is the pinned one), not
  the command's success.

- **When a fix relocates behaviour, re-read every comment and docstring that described the
  old location in the same commit.** The D37 Neo4j lock moved from one directory's conftest
  to the root conftest for every session, and the frozen datastore-proof docstring still
  describes the deleted override. Agents read those docstrings to learn the model, so a stale
  one is a defect that propagates into other tickets' code.

- **A gate exemption no tracked file exercises is not a gate — it is a coin flip waiting for
  the first caller.** `.importlinter`'s `** -> redis.exceptions` fixed a break that had
  blocked four downstream tickets, but nothing in the repo imports `redis.exceptions`, so
  reverting the fix still passes. **Every rule with a deliberate carve-out needs a committed
  file that uses the carve-out**, or the regression lands on whichever ticket first writes
  the legal code.

- **Quote the *negative* half of a verified decision exactly, and re-derive the positive
  half.** D5 was verified live and still worded imprecisely: "schema-level `USAGE` only"
  grants name resolution, not row access, so a ticket following it literally grants `USAGE`
  and then fails every read. The part the decision actually proves — that `sealed` and
  `vault` are unreachable — is exact and load-bearing; the part it summarises is not.
  A decision that was verified in one direction has not been verified in the other.
