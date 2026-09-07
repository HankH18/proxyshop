#!/usr/bin/env bash
# Build the buyer SPA bundle that `buyer-web` serves.
#
# WHY IT IS A SCRIPT AND NOT A ONE-LINE MAKE RECIPE. The runbook gate
# (`docs/tests/test_runbook_executability.py`) resolves every program a make recipe invokes
# against the running machine's PATH, and fails a target whose head is neither a repository
# file nor an installed program. `npm` is the operator's toolchain rather than this
# repository's, so a recipe naming it directly turns "Node is not installed on this box" into
# a red gate about the Makefile. Behind a script the gate checks what the repository controls
# -- that the file exists and parses -- and a missing npm is a legible runtime error here.
#
# `apps/buyer/dist/` is gitignored, so this has to run before `docker compose --profile demo
# up`: the bundle is bind-mounted into nginx, and Docker answers a missing bind source by
# creating an empty directory, which serves a 403 rather than saying anything useful.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v npm >/dev/null 2>&1; then
  echo "FATAL: npm is not on PATH. The buyer SPA is a vite build; install Node (the repo's" >&2
  echo "       package.json asks for >= 22.12.0) and re-run, or run 'make bootstrap'." >&2
  exit 2
fi

if [ ! -d node_modules ]; then
  echo "FATAL: node_modules is missing. Run 'make bootstrap' first -- it runs 'npm ci'." >&2
  exit 2
fi

echo "building the buyer SPA (vite) ..."
npm run build:ui --workspace @proxyshop/buyer

# The gate this script exists to be: a build that "succeeded" and wrote nothing is exactly the
# empty-directory failure above, one step earlier and still silent.
if [ ! -f apps/buyer/dist/index.html ]; then
  echo "FATAL: the build reported success but apps/buyer/dist/index.html does not exist." >&2
  exit 1
fi

echo "buyer SPA built:"
echo "  apps/buyer/dist/index.html"
find apps/buyer/dist/assets -type f 2>/dev/null | sed 's/^/  /' || true
