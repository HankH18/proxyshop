#!/usr/bin/env bash
# The single verification entrypoint. Orchestrator-owned (T-000), frozen.
#
#   ./scripts/verify.sh all      full pipeline (per-wave integration gate, stack up)
#   ./scripts/verify.sh check    per-ticket gate: skips @pytest.mark.docker and .slow
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
run_pytest() {
  set +e; pytest "$@"; rc=$?; set -e
  if [ $rc -eq 5 ]; then
    echo "FATAL: pytest collected 0 tests in: $*" >&2
    echo "       An empty suite is not a passing suite. If a path is genuinely test-free," >&2
    echo "       it does not belong in a verify command." >&2
    return 1
  fi
  return $rc
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
if [ "$STEP" = check ]; then run_pytest -q -m "not needs_model and not docker and not slow"; fi
if [ "$STEP" = all ] || [ "$STEP" = vitest ]; then npx --no-install vitest run --passWithNoTests; fi
if [ "$STEP" = all ] || [ "$STEP" = check ]; then python scripts/check_verify_contracts.py; fi
echo "OK: $STEP"
