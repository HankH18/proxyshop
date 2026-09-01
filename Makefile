# ProxyShop root Makefile — orchestrator-owned (T-000), frozen (B6(i)).
# GNU Make 3.81-safe: one line per recipe. `.ONESHELL` and `.SHELLFLAGS` are silently
# ignored by the make on this host, so never rely on them.
SHELL := /bin/bash
.PHONY: bootstrap preflight deps-up deps-down check verify lint types test-py test-ts demo-seed e2e-live clean

bootstrap:  ; @./scripts/bootstrap.sh
preflight:  ; @./scripts/preflight.sh
deps-up:    ; @docker compose up -d --wait postgres neo4j redis
deps-down:  ; @docker compose down -v
check:      ; @./scripts/verify.sh check
verify:     ; @./scripts/verify.sh all
lint:       ; @./scripts/verify.sh lint
types:      ; @./scripts/verify.sh types
test-py:    ; @./scripts/verify.sh pytest
test-ts:    ; @./scripts/verify.sh vitest
demo-seed:  ; @./.venv/bin/python -m fixtures.seed --category "$(SEED_CATEGORY)"
e2e-live:   ; @bash docs/demo/e2e_live.sh
clean:      ; @rm -rf .pytest_cache .mypy_cache .ruff_cache
