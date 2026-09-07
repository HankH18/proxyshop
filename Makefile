# ProxyShop root Makefile — orchestrator-owned (T-000), frozen (B6(i)).
# GNU Make 3.81-safe: one line per recipe. `.ONESHELL` and `.SHELLFLAGS` are silently
# ignored by the make on this host, so never rely on them.
SHELL := /bin/bash
.PHONY: bootstrap preflight deps-up deps-down db-init db-migrate check verify lint types test-py test-ts demo-seed demo-ui demo-corpus demo-up demo-check demo-down e2e-live clean

bootstrap:  ; @./scripts/bootstrap.sh
preflight:  ; @./scripts/preflight.sh
deps-up:    ; @docker compose up -d --wait postgres neo4j redis && $(MAKE) --no-print-directory db-init && $(MAKE) --no-print-directory db-migrate
deps-down:  ; @docker compose down -v
# D38: the per-worker database proxyshop_w$(PROXYSHOP_WORKER). Idempotent; safe in parallel.
db-init:    ; @[ -x ./.venv/bin/python ] || { echo "FATAL: run 'make bootstrap' first" >&2; exit 2; }; ./.venv/bin/python scripts/db_init.py
# The SCHEMA, which db-init does not apply. `db_init.py` creates the database and stops, and
# nothing in the repository was the migration runner outside the test fixtures — so a stack
# brought up by `deps-up` alone held a database with no `ledger`, no `vault`, no `app` and no
# `sealed`, and every database-backed route answered 503 while compose reported healthy. It
# used to be a hand-typed line in the runbook, repeated after every `deps-down` (which
# destroys the volumes); `deps-up` now runs it, and it stays a target of its own because it is
# also the repair for a database that predates a new migration. Idempotent, advisory-locked,
# safe from several workers at once.
db-migrate: ; @[ -x ./.venv/bin/python ] || { echo "FATAL: run 'make bootstrap' first" >&2; exit 2; }; ./.venv/bin/python scripts/db_migrate.py
check:      ; @./scripts/verify.sh check
verify:     ; @./scripts/verify.sh all
lint:       ; @./scripts/verify.sh lint
types:      ; @./scripts/verify.sh types
test-py:    ; @./scripts/verify.sh pytest
test-ts:    ; @./scripts/verify.sh vitest
demo-seed:  ; @./.venv/bin/python -m fixtures.seed --category "$(SEED_CATEGORY)"
# ── the compose shopper demo (docs/demo/shopper-demo.md) ────────────────────────────────
# Build the SPA bundle `buyer-web` bind-mounts. Must precede demo-up: apps/buyer/dist is
# gitignored, and Docker answers a missing bind source with an empty directory and a 403.
demo-ui:    ; @bash scripts/demo_ui.sh
# Load the ten recorded storefronts into Neo4j. Minutes, not seconds — 3,093 products and
# 44,803 graph writes — and silent until it finishes, which is why the runbook watches the
# product count rather than the log. Idempotent: a second run re-reads nothing unchanged.
demo-corpus: ; @docker compose --profile corpus run --rm corpus-loader
# The services are NAMED rather than left to the profile, and that is a measured repair
# rather than verbosity. `docker compose --profile demo up -d --wait` brings up every
# unprofiled service too, and two of those — `sim` and `seller-reference` — are
# run-to-completion JOBS with `restart: "no"`. `--wait` waits on them, sees them exit, and
# fails the whole command: measured `container proxyshop-sim-1 exited (0)` / `make: ***
# [demo-up] Error 1` over a stack in which all fourteen containers were `(healthy)`. Handing
# an operator a red exit on a working stack is how a runbook trains people to ignore exit
# codes. Their datastore dependencies come up with them through `depends_on`.
demo-up:    ; @docker compose --profile demo up -d --wait buyer-web buyer-svc merchant-svc exchange trust ingest store-agent-gaiaherbs store-agent-toniiq store-agent-paradiseherbs store-agent-oregonswildharvest
# The after-deploy probe. Drives a real roster-less auction over HTTP, because every container
# in this stack can report healthy while the demo is dead.
demo-check: ; @bash scripts/demo_check.sh
demo-down:  ; @docker compose --profile demo --profile corpus down
e2e-live:   ; @bash docs/demo/e2e_live.sh
clean:      ; @rm -rf .pytest_cache .mypy_cache .ruff_cache
