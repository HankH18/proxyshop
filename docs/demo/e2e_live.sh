#!/usr/bin/env bash
# docs/demo/e2e_live.sh — what `make e2e-live` runs.
#
# WHAT THIS IS. `make e2e-live` is the LIVE development-store demo procedure: the starting
# slice re-run against seeded Shopify dev stores instead of `services/shopify-stub`. It is
# demo procedure only. SPEC C9 and DESIGN:47 both say it in the same words — a live dev-store
# run "is never a ticket verify" and no ticket may depend on it — so nothing in `make verify`
# calls this file and nothing ever should.
#
# WHAT THIS DOES, AND WHY IT IS NOT THE PROCEDURE ITSELF. The procedure — app installation,
# storefront password, the Bogus Gateway, the onboarding interview beat — is the extension
# runbook's (T-087, docs/demo/shopify-onboarding-extension.md), and that document has not
# landed. So this file is the part that can be written honestly today: a PREFLIGHT that reads
# the repo's own configuration surface and reports, per item, whether this machine could host
# a live run at all. It then REFUSES, with a non-zero status and a named reason.
#
# IT ALWAYS EXITS NON-ZERO, ON PURPOSE. An `exit 0` here would be the exact defect this file
# was written to end: a command an operator runs, that runs nothing, and reports success.
# There is no argument, no flag and no environment in which this script exits 0 — until the
# day it actually drives a live run.
#
#   exit 2  the machine cannot host a live run; the unmet preconditions are named
#   exit 3  every precondition is met, but the live driver has not landed (see above)
#
# WHY THE CHECKS ARE COMPUTED RATHER THAN LISTED. Every LIVE-* check below calls the same
# function the running system calls — `merchant_svc.install.config.app_config()`,
# `admin_base_url()`, `exchange.checkout.resolve_provider()`,
# `ingest.embeddings.get_embedding_provider()`. A hardcoded checklist would go stale the first
# time one of those moved; this one fails to import instead, which is visible.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

RUNBOOK_STARTING="docs/demo/starting-slice.md"
RUNBOOK_EXTENSION="docs/demo/shopify-onboarding-extension.md"

echo "=== make e2e-live — live development-store preflight ==="
echo "repo root: $ROOT"
echo

# ---------------------------------------------------------------------------------------
# Hard preconditions. Neither is about the live path; without them there is nothing to
# preflight WITH, so they are reported separately and exit immediately.
# ---------------------------------------------------------------------------------------

# D38: every run in this repo is per-worker isolated, and a shell-prefix assignment does not
# survive an `&&` chain. Same failure and same wording as scripts/verify.sh, deliberately.
if [ -z "${PROXYSHOP_WORKER:-}" ]; then
  echo "FATAL: PROXYSHOP_WORKER is unset (D38). Every run is per-worker isolated." >&2
  echo "       Export it and re-run, e.g.:  PROXYSHOP_WORKER=1 make e2e-live" >&2
  exit 2
fi
echo "OK    PROXYSHOP_WORKER=$PROXYSHOP_WORKER (D38)"

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "FATAL: no virtualenv at .venv/bin/python. Run 'make bootstrap' first." >&2
  exit 2
fi
echo "OK    virtualenv present at .venv/bin/python"

# D18 / decisions.md: `make e2e-live` is the one environment that selects the real embedding
# model rather than the hash double, and it does so UNCONDITIONALLY — an inherited
# `EMBEDDING_PROVIDER=hash` is overridden here rather than honoured, because a live run that
# silently kept the hash double would rank a real catalogue on noise. Exported before the
# preflight so LIVE-5 grades the provider this script would actually run with.
export EMBEDDING_PROVIDER="local_bge"
echo "OK    EMBEDDING_PROVIDER=$EMBEDDING_PROVIDER (D18: e2e-live selects the real model)"
echo

# ---------------------------------------------------------------------------------------
# The live preconditions. Each prints one line beginning LIVE-<n> <OK|MISSING>, and the
# python block's exit status is the number of unmet ones.
# ---------------------------------------------------------------------------------------

echo "--- live-run preconditions ---"
set +e
PYTHONPATH="$ROOT/.pkgroot${PYTHONPATH:+:$PYTHONPATH}" "$ROOT/.venv/bin/python" - <<'PY'
import os
import sys

unmet = []


def report(tag: str, ok: bool, detail: str) -> None:
    print(f"{tag} {'OK     ' if ok else 'MISSING'} {detail}")
    if not ok:
        unmet.append(tag)


# LIVE-1 — the app's own identity. Read through the merchant app's config surface rather
# than os.environ, so a rename of either variable shows up here as an ImportError.
from merchant_svc.install.config import (  # noqa: E402
    DEFAULT_APP_URL,
    ENV_ADMIN_BASE_URL,
    admin_base_url,
    app_config,
)

cfg = app_config()
report(
    "LIVE-1",
    bool(cfg.api_key.strip()) and bool(cfg.api_secret.strip()),
    "SHOPIFY_API_KEY / SHOPIFY_API_SECRET are set "
    f"(key={'set' if cfg.api_key.strip() else 'unset'}, "
    f"secret={'set' if cfg.api_secret.strip() else 'unset'}) — "
    "the OAuth install and every webhook HMAC are keyed by the secret",
)

# LIVE-2 — a public origin Shopify can actually reach. The default is a `.example` host on
# purpose: it does not resolve, so a live run configured with it installs a web pixel that
# beacons into nowhere and subscribes webhooks that are never delivered.
report(
    "LIVE-2",
    cfg.app_url != DEFAULT_APP_URL.rstrip("/"),
    f"MERCHANT_APP_URL is a reachable public origin (currently {cfg.app_url!r}); "
    f"the built-in default {DEFAULT_APP_URL!r} is deliberately unresolvable",
)

# LIVE-3 — the Admin client must NOT be pointed back at the offline stub. This is the check
# most likely to catch a "live" run that is quietly not live: SHOPIFY_STUB_URL overrides the
# Admin origin unconditionally, so a .env copied from .env.example sends every Admin mutation
# to localhost:8787 while the operator believes they are talking to a development store.
stub_override = os.environ.get(ENV_ADMIN_BASE_URL, "")
would_post_to = admin_base_url("example-dev-store.myshopify.com")
report(
    "LIVE-3",
    not stub_override,
    f"SHOPIFY_STUB_URL is unset (currently {stub_override or 'unset'}); "
    f"a live Admin call would post to {would_post_to}",
)

# LIVE-4 — the checkout mode. `redirect` is SimulatedRedirectProvider, which mints locally
# and never contacts a merchant; it is the whole reason the starting slice needs no account,
# and it is therefore the wrong provider for a live run.
from exchange.checkout import (  # noqa: E402
    DEFAULT_CHECKOUT_MODE,
    UnknownCheckoutMode,
    registered_modes,
    resolve_provider,
)

mode = (os.environ.get("CHECKOUT_MODE") or DEFAULT_CHECKOUT_MODE).strip().lower()
try:
    provider = resolve_provider(mode)
except UnknownCheckoutMode as exc:
    report("LIVE-4", False, f"CHECKOUT_MODE={mode!r} resolves to no provider: {exc}")
else:
    report(
        "LIVE-4",
        mode != DEFAULT_CHECKOUT_MODE,
        f"CHECKOUT_MODE={mode!r} -> {type(provider).__name__} "
        f"(registered: {registered_modes()}); {DEFAULT_CHECKOUT_MODE!r} is the simulated "
        "provider that mints without a merchant",
    )

# LIVE-5 — the real embedding model. Selecting local_bge succeeds offline and with no
# weights; only embedding something proves the optional extra is installed, so embed.
from ingest.embeddings import get_embedding_provider  # noqa: E402

try:
    get_embedding_provider(os.environ["EMBEDDING_PROVIDER"]).embed("preflight")
except Exception as exc:  # noqa: BLE001 - any failure here is the same verdict
    report(
        "LIVE-5",
        False,
        f"EMBEDDING_PROVIDER={os.environ['EMBEDDING_PROVIDER']!r} cannot embed: "
        f"{type(exc).__name__}: {exc}",
    )
else:
    report("LIVE-5", True, f"EMBEDDING_PROVIDER={os.environ['EMBEDDING_PROVIDER']!r} embeds")

print(f"--- {len(unmet)} of 5 live preconditions unmet ---")
sys.exit(len(unmet))
PY
UNMET=$?
set -e
echo

# ---------------------------------------------------------------------------------------
# The refusal. Two distinct non-zero statuses, because "your machine is not set up" and
# "your machine is set up and the procedure does not exist yet" are different problems.
# ---------------------------------------------------------------------------------------

if [ -f "$ROOT/$RUNBOOK_EXTENSION" ]; then
  echo "NOTE  the extension runbook is present: $RUNBOOK_EXTENSION"
else
  echo "NOTE  the extension runbook has NOT landed: $RUNBOOK_EXTENSION (T-087)"
fi

if [ "$UNMET" -gt 0 ]; then
  echo
  echo "REFUSED: this machine cannot host a live development-store run — $UNMET of the 5"
  echo "         preconditions above are unmet. Each MISSING line names what to set."
  echo
  echo "         Nothing was run. The offline starting slice needs none of this:"
  echo "           $RUNBOOK_STARTING"
  exit 2
fi

echo
echo "REFUSED: every live precondition is met, but the live procedure itself has not landed."
echo "         The dev-store provisioning, app-installation and onboarding beat live in"
echo "         $RUNBOOK_EXTENSION (T-087), and this script becomes its driver when it does."
echo
echo "         Nothing was run — and this script never exits 0 while that is true, so it"
echo "         cannot be mistaken for a passing live run."
exit 3
