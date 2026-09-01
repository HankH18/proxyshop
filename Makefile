# ProxyShop root Makefile — orchestrator-owned (T-000), frozen (B6(i)).
# GNU Make 3.81-safe: one line per recipe. `.ONESHELL` and `.SHELLFLAGS` are silently
# ignored by the make on this host, so never rely on them.
SHELL := /bin/bash
.PHONY: bootstrap preflight deps-up deps-down db-init check verify lint types test-py test-ts demo-seed e2e-live clean

bootstrap:  ; @./scripts/bootstrap.sh
preflight:  ; @./scripts/preflight.sh
deps-up:    ; @docker compose up -d --wait postgres neo4j redis && $(MAKE) --no-print-directory db-init
deps-down:  ; @docker compose down -v
# D38: the per-worker database proxyshop_w$(PROXYSHOP_WORKER). Idempotent; safe in parallel.
db-init:    ; @[ -x ./.venv/bin/python ] || { echo "FATAL: run 'make bootstrap' first" >&2; exit 2; }; ./.venv/bin/python scripts/db_init.py
check:      ; @./scripts/verify.sh check
verify:     ; @./scripts/verify.sh all
lint:       ; @./scripts/verify.sh lint
types:      ; @./scripts/verify.sh types
test-py:    ; @./scripts/verify.sh pytest
test-ts:    ; @./scripts/verify.sh vitest
demo-seed:  ; @./.venv/bin/python -m fixtures.seed --category "$(SEED_CATEGORY)"
e2e-live:   ; @bash docs/demo/e2e_live.sh
clean:      ; @rm -rf .pytest_cache .mypy_cache .ruff_cache
