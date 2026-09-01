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

---

## Cycle 1, wave 1 boundary (frontier dispatch: T-010, T-013, T-011, T-014, T-012)

- **Re-derive the ticket graph from the executable source at every dispatch, never from the
  ledger's own prose.** `backlog.md` claimed 45 tickets; `tickets.json` had 47. A user-approved
  harness amendment had rewritten five dependency edges, added two tickets, retitled two and
  widened one scope — and the ledger, which is what the scheduler reads, recorded none of it.
  The frontier happened to be unchanged, so nothing was mis-dispatched, but that was luck: the
  same staleness had already moved the deepest ticket, the gate count and every unblock number.
  A ledger is a cache of the graph, and a cache nobody invalidated is the default state.

- **A frozen-suite MIRROR goes stale the instant the suite is amended, and it fails silently in
  the one direction that matters.** `public-surface.md` is generated by AST-parsing the frozen
  suite, and packets paste its blocks in as the naming contract. It was generated at 103 tests;
  the suite is now 120. Its T-010 block still described a five-dimension `TrustSnapshot` when
  the frozen payload asserts six — a worker following it would have made its own headline goal
  permanently red, and the failure would have read as "not built yet". **Regenerate every
  derived artifact as part of the amendment, or the amendment silently poisons the packets.**

- **Read a ticket's own acceptance text against the frozen fixtures before quoting it into a
  packet — the ticket can be the wrong one.** T-010's acceptance 5 says a `Bid` missing any of
  `signer_id/key_id/issued_at/nonce/schema_version` must fail validation. The frozen `Bid`
  fixture carries none of those four and is asserted to validate at four control sites. Read
  literally, the ticket demands the suite be broken. The resolution (a separate `SigningEnvelope`
  type) is inferable from the objective, but only if someone checks — and the packet, not the
  worker, is where that check belongs.

- **Measure every ticket's declared verify command BEFORE dispatch and put the reading in the
  packet.** Measured here: three of the five (T-010, T-013, T-014) exit **0** today against
  nothing but T-000 scaffold smoke tests — a worker who writes no code passes. The other two
  exit **4** on a missing path, which any triage that treats non-{0,1} as "environment broken"
  will misread as a harness fault rather than correct pre-build behaviour. Neither shape is
  visible without running it, and both change what "done" means.

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

- **Attribute frozen-test results PER TICKET at collection, in the branch's own worktree —
  the data exists and nothing reads it.** Every metric here is suite- or epic-level, and
  `run.py` has `--epic` and `--blocker` but no `--ticket`, so `@pytest.mark.ticket(...)` —
  the marker whose stated job is naming "which ticket is responsible for making it pass" —
  feeds no metric at all. Combined with verify commands that pass vacuously, a ticket's
  completion was unmeasured from both directions and "collected" was pure orchestrator
  judgment. But the runner records `ticket` on every result and `--json` dumps it, so
  running it inside the returning branch's worktree and grouping by ticket answers the one
  question that matters at the moment you can still send the branch back: *did THIS
  branch's own frozen tests actually turn green?* Two gotchas: `--json` writes to **stderr**
  (so `2>&1` is mandatory) and it returns before the count logic, so it ignores other flags.
  Without this, a ticket merged with its own tests red surfaces only as an epic shortfall at
  cycle end, with no attribution and several merges of distance from the cause.

- **`[verified]` on a pinned decision must mean "an executable artifact in it was EXECUTED",
  and it must be re-checked at intake — otherwise the tag is worse than no tag.** D6 carried
  a `[verified]` header and shipped a `CREATE VECTOR INDEX` statement that does not parse:
  Cypher map keys must be identifiers or backtick-quoted, and D6 single-quoted them, so it
  raised a `SyntaxError` at column 109 on the exact engine the decision named. It survived
  the cycle-0 adversarial pass and a twelve-finding partner review; both read it, neither ran
  it. Six tickets copy that statement verbatim, and the container was up and reachable the
  entire time — the check was available all along and never taken. **The tag is what tells a
  packet-writer "copy this verbatim, it is confirmed", so an unexecuted `[verified]` is a
  claim with the authority of a measurement and the reliability of a guess.** Extract every
  executable snippet from the decisions file at intake — SQL, Cypher, shell, API calls — run
  each against the real dependency before freezing, and record the engine version beside the
  claim. Anything not executed is `[reasoned — not executed]`.

- **The first defect found by a BUILDER rather than an auditor was found by executing a
  document.** Every other defect this run came from an adversarial reader. D6 came from an
  agent that tried to run the thing and watched it fail. Auditors read for contradiction;
  builders discover unrunnability. Both are needed, and a review programme made only of
  readers has a blind spot shaped exactly like this.

- **A rescaling with no error case is worse than a syntax error.** Neo4j returns cosine as
  `(1 + cos) / 2`, so an orthogonal vector scores ≈ 0.5 rather than 0.0 — a plausible weak
  match, not a failure. Anything thresholding on raw similarity inflates silently and
  forever. Put a measured note in every consumer's packet, not only in the decision that
  owns the index.
