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

# `--locked`, NOT `--frozen`. `--frozen` installs the lockfile and ignores pyproject.toml
# entirely: add a dependency, run bootstrap, get exit 0 — and then `ModuleNotFoundError` at
# import time with nothing anywhere saying why. `--locked` resolves and FAILS when the lock
# no longer matches the manifests ("The lockfile at `uv.lock` needs to be updated"), which
# is the same loud behaviour `npm ci` already gives us on the JS side. A worker that needs a
# new dependency must run `uv lock` and commit the result; half-adding one is now an error.
echo "==> uv sync --locked   ($ROOT)"
uv sync --locked

echo "==> npm ci --prefer-offline   ($ROOT)"
npm ci --prefer-offline

# macOS: uv writes every .pth with the UF_HIDDEN file flag, and CPython's site.addpackage
# skips hidden .pth files ("Skipping hidden .pth file"). That makes `dev-mode-dirs` inert,
# so `import exchange` works under pytest (via the `pythonpath` ini) but NOT under
# `python -m fixtures.seed`, `uvicorn exchange.main:app`, or any other real entry point.
# Un-hiding our own .pth — and only ours — restores the advertised behaviour.
if command -v chflags >/dev/null 2>&1; then
  for pth in "$ROOT"/.venv/lib/python3.12/site-packages/_proxyshop.pth; do
    [ -f "$pth" ] && chflags nohidden "$pth" || true
  done
fi

# Prove the environment is this worktree's, not a neighbour's.
"$ROOT/.venv/bin/python" - "$ROOT" <<'PY'
import sys
root = sys.argv[1]
assert sys.prefix.startswith(root), f"venv escaped the worktree: {sys.prefix} not under {root}"
assert sys.version_info[:2] == (3, 12), f"wrong interpreter: {sys.executable} {sys.version}"
print(f"    python {sys.version.split()[0]} at {sys.prefix}")
PY
node -e 'const p=require("typescript/package.json"); if(!p.version.startsWith("5.")) {console.error("FATAL: typescript "+p.version+" is not 5.x (D8)");process.exit(1)} console.log("    typescript "+p.version)'

# Prove the flat-src import namespaces work OUTSIDE pytest too — that is what the .pth
# un-hiding above buys, and a silent regression here breaks `make demo-seed` and every
# service entry point while `make verify` stays green.
(cd / && "$ROOT/.venv/bin/python" -c 'import exchange, store_agent, fixtures' \
  && echo "    .pkgroot namespaces import from an unrelated cwd") \
  || echo "    WARNING: .pkgroot namespaces are pytest-only in this venv; run services with PYTHONPATH=$ROOT/.pkgroot" >&2

echo "OK: bootstrap"
