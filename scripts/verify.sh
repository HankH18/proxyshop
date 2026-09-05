#!/usr/bin/env bash
# The single verification entrypoint. Orchestrator-owned (T-000), frozen.
#
#   ./scripts/verify.sh all      full pipeline (per-wave integration gate, stack up)
#   ./scripts/verify.sh check    per-ticket gate: RUNS docker tests (they skip per-service
#                                when their datastore is down); excludes needs_model only
#   ./scripts/verify.sh lint     ruff + import-linter + the banned-reset gate + eslint
#   ./scripts/verify.sh types    mypy + tsc -b
#   ./scripts/verify.sh pytest   python tests only
#   ./scripts/verify.sh vitest   typescript tests only
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
export PATH="$ROOT/.venv/bin:$ROOT/node_modules/.bin:$PATH"

# Load .env when there is one, so the DSNs/URLs in it actually reach the tests — nothing
# used to read it. PROXYSHOP_WORKER is explicitly NOT taken from the file: .env.example
# ships `PROXYSHOP_WORKER=1`, and a worktree that copied it would silently re-badge every
# worker as worker 1 and undo every kind of isolation this repo has (D38).
if [ -f "$ROOT/.env" ]; then
  _keep_worker="${PROXYSHOP_WORKER:-}"
  set -a; . "$ROOT/.env"; set +a
  if [ -n "$_keep_worker" ]; then export PROXYSHOP_WORKER="$_keep_worker"; fi
  unset _keep_worker
fi

# D38: every run is per-worker isolated. Fail here rather than 40 seconds later inside
# pytest, and say exactly what to do about it.
if [ -z "${PROXYSHOP_WORKER:-}" ]; then
  echo "FATAL: PROXYSHOP_WORKER is unset (D38). Every run is per-worker isolated." >&2
  echo "       Export it and re-run, e.g.:  PROXYSHOP_WORKER=1 make verify" >&2
  exit 2
fi

[ -x "$ROOT/.venv/bin/pytest" ] || { echo "FATAL: run 'make bootstrap' (uv sync --frozen)" >&2; exit 2; }
[ -x "$ROOT/node_modules/.bin/vitest" ] || { echo "FATAL: run 'npm ci --prefer-offline'" >&2; exit 2; }
python - <<'PY'
import sys, pytest
assert sys.version_info[:2] == (3,12), f"wrong interpreter: {sys.executable} {sys.version}"
assert tuple(int(x) for x in pytest.__version__.split('.')[:2]) >= (8,2), pytest.__version__
PY

STEP="${1:-all}"

# Collecting ZERO tests is a FAILURE here, not a warning.
#
# This used to map pytest's exit 5 to 0 "because the scaffold might be empty". It is not
# empty any more, and the tolerance was a hole big enough to drive the whole gate through:
# deleting every test_*.py in the repo left `make verify` at exit 0, reporting OK. pytest's
# own exit 5 is the only signal that the suite vanished, so it is now fatal. Per-directory
# emptiness — one lane's tests deleted while others remain — is exit 0 as far as pytest is
# concerned; scripts/check_verify_contracts.py catches that separately.
# T-117 / ESC-005: a green gate must NAME what it did not run.
#
# The exit-5 rule below already covers the total case — pytest exits 5 when everything was
# deselected as well as when nothing was collected (measured: a marker matching nothing gives
# `801 deselected`, exit 5), and that is fatal here. The hole it does NOT cover is PARTIAL
# deselection, and that is the one that bit: `check` runs `-m "not needs_model and not docker
# and not slow"`, which silently removed all three of T-110's fresh-volume tests including the
# one grading its acceptance criterion 2 — so the lane reported its gate green while the tests
# written to grade it never executed. Deselection here is by design and a nonzero count is NOT
# an error (the current green log reads `3756 passed, 1 deselected`), so this reports rather
# than refuses: the count is printed on its own line, every time, so "passed" can never again
# be mistaken for "ran".
run_pytest() {
  local log rc line desel skip
  log="$(mktemp)"
  set +e
  pytest "$@" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" -eq 5 ]; then
    rm -f "$log"
    echo "FATAL: pytest collected 0 tests in: $*" >&2
    echo "       An empty suite is not a passing suite. If a path is genuinely test-free," >&2
    echo "       it does not belong in a verify command." >&2
    echo "       (pytest also exits 5 when every collected test was DESELECTED — a gate that" >&2
    echo "        selected none of its own tests proves nothing and is refused here too.)" >&2
    return 1
  fi
  # `|| true` on ALL THREE, and it is load-bearing rather than sloppy. Under this file's
  # `set -euo pipefail`, a grep that matches nothing exits 1, pipefail promotes that to the
  # pipeline's status, and the assignment's non-zero status trips errexit — killing the
  # function HERE, before `return "$rc"`, before the SELECTION line, and before `rm -f`.
  # Reported by a rung-2 verifier that drove both bodies through a replica of this call site:
  # a usage error (4), an INTERNALERROR (3), a SIGKILL (137), an interrupt (2) and even a
  # GREEN run whose epilogue it could not parse (0) all came back as 1. Fail-closed, so it
  # weakened nothing — but it destroyed pytest's real exit code, leaked the temp file, and
  # suppressed the SELECTION line in exactly the case where the parse had failed, which is
  # the one case a reader most needs to see. A missing epilogue is not a gate failure: `rc`
  # was captured at line 68 before any of this and is what the function returns.
  line="$(grep -E '[0-9]+ (passed|failed|deselected|skipped)' "$log" | tail -1 || true)"
  desel="$(printf '%s' "$line" | grep -Eo '[0-9]+ deselected' || true)"
  skip="$(printf '%s' "$line" | grep -Eo '[0-9]+ skipped' || true)"
  rm -f "$log"
  if [ -z "$line" ]; then
    echo "SELECTION: UNPARSEABLE — pytest printed no recognisable summary line for: pytest $*"
    echo "           Treat this run's coverage as unknown; the exit status ($rc) still stands."
    return "$rc"
  fi
  echo "SELECTION: ${desel:-0 deselected}, ${skip:-0 skipped}  <-  pytest $*"
  if [ -n "$desel" ] || [ -n "$skip" ]; then
    echo "           Those tests did NOT run. A green gate is evidence only about what it"
    echo "           selected — if your ticket's own acceptance tests are in that count, this"
    echo "           gate has not graded them (T-117)."
  fi
  return "$rc"
}

# D39: the global Redis reset is banned repo-wide. Implementation notes, both load-bearing:
#  * The needle is assembled from two halves so this gate does not flag itself, and so a
#    doc that legitimately discusses the ban is not what fails the build — only a real
#    occurrence in a source file is.
#  * The candidate list is built into a bash array first. BSD xargs (this host) has no
#    -r/--no-run-if-empty, so `git ls-files | xargs grep` on an empty match set runs grep
#    with no file operands and blocks reading stdin. Never pipe into xargs here.
banned_reset_gate() {
  local needle="FLUSH""ALL"
  local -a candidates=()
  local f
  while IFS= read -r f; do
    case "$f" in
      .swarm-loop/*|SPEC.md|DESIGN.md|TASKS.md|EXECUTION.md|tickets.json|scripts/verify.sh) continue ;;
    esac
    [ -f "$f" ] || continue          # skip submodules and the .pkgroot/* symlinks
    candidates+=("$f")
  done < <(git ls-files 2>/dev/null || find . \
      -path ./.git -prune -o -path ./.venv -prune -o -path ./node_modules -prune -o \
      -path ./.pkgroot -prune -o -path ./.swarm-loop -prune -o -type f -print | sed 's|^\./||')
  if [ ${#candidates[@]} -eq 0 ]; then return 0; fi
  local hits
  hits="$(grep -l -I -F -e "$needle" -- "${candidates[@]}" 2>/dev/null || true)"
  if [ -n "$hits" ]; then
    echo "FATAL: the banned global Redis reset appears in:" >&2
    echo "$hits" >&2
    echo "       Use FLUSHDB (this worker's logical DB only) — see D39." >&2
    return 1
  fi
  return 0
}

if [ "$STEP" = all ] || [ "$STEP" = check ] || [ "$STEP" = lint ]; then
  ruff check .
  ruff format --check .
  PYTHONPATH="$ROOT/.pkgroot${PYTHONPATH:+:$PYTHONPATH}" lint-imports
  banned_reset_gate
  npx --no-install eslint .
fi
if [ "$STEP" = all ] || [ "$STEP" = check ] || [ "$STEP" = types ]; then
  mypy; npx --no-install tsc -b --pretty false
fi
if [ "$STEP" = all ] || [ "$STEP" = pytest ]; then run_pytest -q -m "not needs_model"; fi
# T-117 acceptance 1 + 3 (ESC-005): `docker` is NO LONGER deselected here.
#
# This line used to read `-m "not needs_model and not docker and not slow"`, and that blanket
# `not docker` is the defect T-117 names: it silently removed all three of T-110's fresh-volume
# tests, including the one grading that ticket's acceptance criterion 2, so the lane reported
# its own gate green while the tests written to grade it never executed.
#
# Deselecting them was only ever a proxy for "the datastores may not be up". T-109 replaced that
# with per-service reachability: a docker-marked test now probes the service it actually needs
# and SKIPS cleanly when that service is down, instead of the whole stack being all-or-nothing.
# So the gate can run them and let reachability decide — which is T-117 acceptance 3, "T-109's
# per-service reachability work lands consistently with this".
#
# MEASURED, and CORRECTED after a rung-2 verifier re-measured the original claim:
#   old `-m "not needs_model and not docker and not slow"`  ->  3729 selected / 247 deselected
#   new `-m "not needs_model and not slow"`                 ->  3975 selected /   1 deselected
# So this runs 246 docker-marked tests that the per-ticket gate previously dropped. (The first
# version of this comment said "~3520 -> 3908, ~388 tests" and that was wrong: it compared a
# pre-cycle-11 suite size against a post-cycle-11 one and charged ~142 newly-ADDED tests to this
# change. The direction was right; the magnitude was not.)
#
# `and not slow` is a PRESENT-DAY NO-OP kept for future use: `-m slow` collects zero of 3976, so
# there is no "single slow test" — the one remaining deselection under both expressions is the
# needs_model test. The earlier comment misnamed it.
#
# Verified in both directions with the stack down (every datastore pointed at a closed port):
# all 246 SKIP with a named per-service reason and none passes, so nothing here passes without
# touching its service and nothing fails for a machine reason.
#
# KNOWN COST, named because the first version of this amendment failed to name it: every
# `graph`-marked test is also `docker`-marked, so `check` now builds the session-scoped
# `_neo4j_guard` and holds the MACHINE-GLOBAL flock /tmp/proxyshop-neo4j.lock for most of its
# run (measured: 196s of a 222s session). Concurrent lanes running `check` therefore serialise
# on it, and one that waits past the lock's budget fails with Neo4jLockTimeout for a machine
# reason. That is tracked separately; the fix belongs in the fixture's scope, not here.
if [ "$STEP" = check ]; then run_pytest -q -m "not needs_model and not slow"; fi
run_vitest() {
  # The TypeScript half of `build_succeeds`, held to the same standard as run_pytest above.
  # It used to pass `--passWithNoTests`, which ASKS the runner to report success on an empty
  # collection: measured, `vitest run --passWithNoTests 'no/such/pattern/**'` exits 0, so a
  # glob change, a config edit or a moved directory could take this half of a scored metric
  # from 864 tests to nothing without moving the number. The flag earned its place when this
  # repo had no TypeScript tests at all; that stopped being true long ago.
  local log rc total
  log="$(mktemp)"
  set +e
  npx --no-install vitest run 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  # Read the count BEFORE restoring `set -e`: on an empty collection these greps match
  # nothing and exit nonzero, which under `set -e` would kill this function with vitest's
  # own status before the FATAL below could fire. That is not hypothetical — it is what the
  # first draft of this function did, and the empty-collection control is what caught it.
  total="$(grep -a 'Tests ' "$log" | tail -1 | grep -oE '[0-9]+' | tail -1)"
  set -e
  # Vitest exits 1 on an empty collection now that --passWithNoTests is gone, so rc alone
  # would report "some test failed" for a suite that never ran. Say which it was.
  if [ "$rc" -ne 0 ] && [ -z "$total" ]; then
    rm -f "$log"
    echo "FATAL: vitest collected 0 tests." >&2
    echo "       An empty suite is not a passing suite. If the TypeScript tests genuinely" >&2
    echo "       moved, point the config at them; if they were deleted, that is the finding." >&2
    return 2
  fi
  rm -f "$log"
  return "$rc"
}
if [ "$STEP" = all ] || [ "$STEP" = vitest ]; then run_vitest; fi
if [ "$STEP" = all ] || [ "$STEP" = check ]; then python scripts/check_verify_contracts.py; fi
echo "OK: $STEP"
