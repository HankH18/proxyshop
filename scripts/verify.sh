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

run_pytest() {                      # exit 5 (nothing collected) tolerated ONLY here
  set +e; pytest "$@"; rc=$?; set -e
  if [ $rc -eq 5 ]; then echo "WARNING: pytest collected 0 tests in: $*" >&2; return 0; fi
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
