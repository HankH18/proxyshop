# Run brief — read this first

Durable state of this swarm-loop run. Phase 0 and most of Phase 1 are **done**.
Everything here was verified by running it. Do not re-derive any of it.

## Read in this order

| file | what it holds |
|---|---|
| `decisions.md` | pinned rulings D1–D11, all `[verified]` on this machine |
| `codebase-map.md` | measured environment facts (RAM, ports, DB probes, toolchain) |
| `intake-report.md` | **the build plan** — 88 KB, §1 blockers … §9 divergences |
| `backlog.md` | the 44-ticket ledger, depths, gate set, verify commands |
| `learnings.md` | process lessons, including the delegation rule |
| `harness-review.md` | the pre-rebuild adversarial review of the skill itself |

Do **not** re-read SPEC/DESIGN/TASKS/tickets.json cover-to-cover. The intake report
distilled them; subagents load them on demand.

## Hank's decisions — do not re-ask

- **Audit-fix loops:** 3.
- **T-080 human gate:** draft the fixture manifest marked **UNAPPROVED**, notify Hank,
  and keep building everything not blocked by it. Never fabricate the approval artifact
  (EXECUTION.md rule 6).
- **Jira mirroring:** no.
- **Git:** conventional commit per merged ticket; push to **both** remotes.
- **Autonomy:** `approve-cycle-1` — Hank approves the plan at the Phase 2 checkpoint,
  then the run is autonomous.
- **B7 / T-065 circularity: ACCEPTED AND APPLIED.** See D11.

## Harness — already initialized, do not re-init

```
swarmloop.py init --max-cycles 102 --audit-loops 3 --project proxyshop \
    --autonomy approve-cycle-1 --push-remote when-clean
```

102 = 2×44 + 3×3 + 5 — a runaway ceiling, **not** a plan. The rebuilt harness reads this
state correctly (`status` and `resume` both exit 0). `resume` currently says:

> Phase 2 incomplete: finish goals + acceptance tests, freeze, then baseline

That is correct. Start there.

## Git

Branch `main`. Remotes configured and authenticated, **nothing pushed yet**:

- `gitlab` → `https://labs.gauntletai.com/hankholcomb/proxyshop.git` — PAT is in the macOS
  keychain (verified via `git credential fill`). **Never ask Hank for a token.**
  Push-to-create makes the private project.
- `github` → `git@github.com:HankH18/proxyshop.git` — `gh auth status` verified (HankH18,
  ssh, repo scope).

Push only in Phase 7, after `measure`/`analyze`, through the harness's push gate.

## Permissions and the git-guard

`.claude/settings.local.json` (gitignored) carries the allowlist, `"disableAllHooks": false`,
`Bash(git push:*)`, and the `PreToolUse` git-guard hook. **It existed before the session
started**, which is required — a settings file created mid-session is never loaded.

**Mandatory first step (SKILL.md ~148): probe that the guard is live.** In a throwaway temp
repo — never this tree — run `git stash` and confirm it comes back refused (exit 2). If it
succeeds the guard is not live: do not dispatch a mutating wave; tell Hank the session needs
restarting.

## Done vs not done

**Done:** environment probed and recorded; ticket graph computed and cross-checked; harness
initialized; permissions and guard wired; docker images pre-pulled; the frozen acceptance
**runner** built and smoke-tested (skip ≠ pass; a missing epic fails loudly with empty stdout
and exit 1; lazy-import failures count as ordinary test failures, not collection errors).

**Not done:** `goals.json` is empty and nothing is frozen; `acceptance/` has the runner but
**zero tests**; there is no scaffold and no product code.

**`run.py` still needs, before freeze:** `--write-report` / `--from-report` modes
(intake B2 — otherwise 12 metrics × 102 cycles = 1224 full suite runs) and a `--blocker`
filter (the conftest already emits the field).

## Next actions, in order

1. Read the files above; check `HARNESS-STATUS.md` for which skill defects are patched.
2. **Probe the git-guard is live.** Do not skip.
3. Add `run.py`'s report-reuse modes (pre-freeze).
4. Build the T-000 scaffold from intake §3 — one agent, one worktree; its scope is `**`, so
   nothing runs beside it. Delete any stale `.swarm-loop/**/__pycache__` before freezing
   (a Python 3.9 artifact was found and removed once already).
5. Author the frozen acceptance suite (intake B1) using §7's metric proposals and the two
   rules in `acceptance/README.md`.
6. Write `goals.json`. Metric commands use a **relative** `.venv/bin/python` prefix, never a
   bare `python3`; must be hermetic per `goal-setting.md`; and **every metric needs a numeric
   `tolerance` and `target`** — see `HARNESS-STATUS.md`.
7. **Present the Phase 2 checkpoint to Hank**: metrics and targets, the narrowed ownership
   map, the initial frontier, and intake §9's remaining plan divergences. Autonomy is
   `approve-cycle-1`, so this approval is required before any dispatch. (B7 is already
   decided — do not re-raise it.)
8. `freeze --smoke-timeout 2400` → `verify` → `measure --cycle 0 --timeout 2400` →
   `checkpoint --cycle 0` → commit `.swarm-loop/` to `main`.
9. Dispatch the frontier: **T-010, T-011, T-012, T-013, T-014** — plus **T-080 as early as
   T-013 allows**, to open the human gate while Hank is awake.

## How to work here

**Delegate by default.** Hank corrected the previous session on exactly this: *"I feel like
you should be using subagents for this instead of clouding your context window so early."*
Your own hands are for decisions, dispatch, merges, and holding the protocol. Probing,
authoring, document intake and verification all go to subagents. Route large artifacts to
files that subagents read, never through your own context. Launch independent streams in one
message so they actually run concurrently. Resist "this is quick" and "I need the facts in my
own head" — both are false.

**Do not take a subagent's clean bill at face value.** In this run one agent reported
"FALSE POSITIVES — none found" for the git-guard while a two-line manual probe found one.
Two agents also lost their `cd` between bash calls and ran probes inside the live repo
despite an explicit read-only instruction. Spot-check claims that matter.
