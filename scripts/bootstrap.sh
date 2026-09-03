#!/usr/bin/env bash
# Provision THIS worktree. Orchestrator-owned (T-000), frozen.
#
#   ./scripts/bootstrap.sh                     full provisioning
#   ./scripts/bootstrap.sh --check-namespaces  only the .pth clear + the import assertion
#
# Both installs are deliberately done in the current directory: a shared root venv with
# editable installs would make every worker's tests import the main tree's source instead
# of its own, which is exactly the failure mode worktree isolation exists to prevent.
#
# THE MEASUREMENT TREE IS NOT SPECIAL (T-111 acceptance 3). Whatever tree the frozen
# metrics are read in — the primary checkout included — must be provisioned by THIS script,
# exactly as every worktree is, and `--check-namespaces` re-run immediately before the
# measurement. Two readings were already lost to a tree that skipped it: acceptance_pass_
# rate came back 5.00 instead of 13.33, and build_succeeds fell 1 -> 0, both with no
# product cause anywhere. A measurement taken in an unprovisioned tree measures the tree.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"

# Never inherit another tree's environment.
unset VIRTUAL_ENV PYTHONPATH NODE_PATH

SITE_PACKAGES="$ROOT/.venv/lib/python3.12/site-packages"

# --------------------------------------------------------------------------------------
# The .pth guarantee, and the assertion that it actually holds (T-111, T-123)
# --------------------------------------------------------------------------------------
#
# macOS writes this venv's site-packages with the UF_HIDDEN file flag, and CPython's
# site.addpackage silently skips hidden .pth files ("Skipping hidden .pth file"). That
# makes `dev-mode-dirs` inert, so `import exchange` works under pytest (via the `pythonpath`
# ini) but NOT under `python -m fixtures.seed`, `uvicorn exchange.main:app`, or any other
# real entry point.
#
# The DIRECTORY carries the flag, not merely our own .pth — all three .pth files in it
# (_proxyshop, _virtualenv, a1_coverage) were flagged as well. That is why the original
# one-file `chflags nohidden "$pth"` looked like it "kept coming back": it was a partial
# repair, never a fix. The clear is therefore recursive over site-packages.
#
# WHAT RE-HIDES IT IS STILL NOT KNOWN, and this script does not pretend otherwise (T-123
# acceptance 4). Two sessions probed it independently and got results that contradict each
# other: one measured `pytest --version` re-hiding the flag reliably with no concurrent
# writer, the other ran nine probes (bare python, `import pytest`, `import _pytest.config`,
# `pytest --version` with and without plugin autoload, `python -m pytest --version`,
# `import uv`, a read-only ls) and reproduced nothing, and separately refuted uv itself in
# an isolated scratch venv. Neither could make the other's result appear. The two are
# reconcilable only through the directory flag, which differed between the venvs they were
# probing — which is a description of the disagreement, not of the trigger.
#
# So the mitigation is defended on its own terms rather than as a cure: clear the flag
# recursively and then ASSERT THE EFFECT — import a namespace from an unrelated cwd and
# print its resolved path. It is the same "assert the effect, never the exit code" rule
# this run applies to every installer, applied to the one provisioning step that was
# exempt from it. `proxyshop_support/tests/test_bootstrap_provisioning.py` proves the
# assertion survives a real pytest run, with the bare import taken AFTER the suite.
#
# Neither step may swallow a failure. Both `|| true` and `|| echo WARNING >&2` were here
# before, and between them they let provisioning print "OK: bootstrap" and exit 0 with the
# flat namespaces dead.

clear_hidden_flag() {
  command -v chflags >/dev/null 2>&1 || return 0   # not macOS: there is no flag to clear
  if [ ! -d "$SITE_PACKAGES" ]; then
    echo "FATAL: $SITE_PACKAGES does not exist — the venv was never created." >&2
    return 1
  fi
  if ! chflags -R nohidden "$SITE_PACKAGES"; then
    echo "FATAL: could not clear UF_HIDDEN recursively on $SITE_PACKAGES." >&2
    echo "       site.addpackage silently skips hidden .pth files, so this leaves every" >&2
    echo "       .pkgroot namespace unimportable outside pytest. The step used to end in" >&2
    echo "       '|| true' and could not fail the provisioning it is the guarantee for." >&2
    return 1
  fi
}

assert_flat_namespaces() {
  local resolved
  local program='import contracts, llm, trust, exchange, store_agent, fixtures; print(contracts.__file__)'
  if ! resolved="$(cd / && "$ROOT/.venv/bin/python" -c "$program" 2>&1)"; then
    echo "FATAL: the .pkgroot flat namespaces do not import from an unrelated cwd." >&2
    echo "$resolved" >&2
    echo '       Every service entry point and every `make demo-seed` child depends on' >&2
    echo '       this, and pytest hides the breakage because its `pythonpath` ini adds' >&2
    echo '       .pkgroot itself — so a green `make verify` is not evidence here.' >&2
    echo "       Re-run ./scripts/bootstrap.sh. Until it passes, run services with" >&2
    echo "       PYTHONPATH=$ROOT/.pkgroot as a stopgap; that is a workaround, not a fix." >&2
    return 1
  fi
  echo "    .pkgroot namespaces import from an unrelated cwd: $resolved"
}

if [ "${1:-}" = "--check-namespaces" ]; then
  clear_hidden_flag
  assert_flat_namespaces
  echo "OK: namespaces"
  exit 0
elif [ "$#" -gt 0 ]; then
  echo "usage: ${BASH_SOURCE[0]} [--check-namespaces]" >&2
  exit 2
fi

# --------------------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------------------

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

# Runs after `uv sync`, which is what writes the .pth files in the first place.
clear_hidden_flag

# Prove the environment is this worktree's, not a neighbour's.
"$ROOT/.venv/bin/python" - "$ROOT" <<'PY'
import sys
root = sys.argv[1]
assert sys.prefix.startswith(root), f"venv escaped the worktree: {sys.prefix} not under {root}"
assert sys.version_info[:2] == (3, 12), f"wrong interpreter: {sys.executable} {sys.version}"
print(f"    python {sys.version.split()[0]} at {sys.prefix}")
PY
node -e 'const p=require("typescript/package.json"); if(!p.version.startsWith("5.")) {console.error("FATAL: typescript "+p.version+" is not 5.x (D8)");process.exit(1)} console.log("    typescript "+p.version)'

# Prove the flat-src import namespaces work OUTSIDE pytest too — that is what the recursive
# un-hiding above buys, and a silent regression here breaks `make demo-seed` and every
# service entry point while `make verify` stays green.
assert_flat_namespaces

echo "OK: bootstrap"
