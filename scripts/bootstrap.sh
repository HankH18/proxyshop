#!/usr/bin/env bash
# Provision THIS worktree. Orchestrator-owned (T-000), frozen.
#
# Both installs are deliberately done in the current directory: a shared root venv with
# editable installs would make every worker's tests import the main tree's source instead
# of its own, which is exactly the failure mode worktree isolation exists to prevent.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"

# Never inherit another tree's environment.
unset VIRTUAL_ENV PYTHONPATH NODE_PATH

echo "==> uv sync --frozen   ($ROOT)"
uv sync --frozen

echo "==> npm ci --prefer-offline   ($ROOT)"
npm ci --prefer-offline

# Prove the environment is this worktree's, not a neighbour's.
"$ROOT/.venv/bin/python" - "$ROOT" <<'PY'
import sys
root = sys.argv[1]
assert sys.prefix.startswith(root), f"venv escaped the worktree: {sys.prefix} not under {root}"
assert sys.version_info[:2] == (3, 12), f"wrong interpreter: {sys.executable} {sys.version}"
print(f"    python {sys.version.split()[0]} at {sys.prefix}")
PY
node -e 'const p=require("typescript/package.json"); if(!p.version.startsWith("5.")) {console.error("FATAL: typescript "+p.version+" is not 5.x (D8)");process.exit(1)} console.log("    typescript "+p.version)'

echo "OK: bootstrap"
