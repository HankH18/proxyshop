# ProxyShop — codebase map

Status: **greenfield**. Only the build-docs doc set exists (SPEC.md, DESIGN.md, TASKS.md,
EXECUTION.md, tickets.json) plus `.gitignore`, `.claude/settings.local.json`, `.swarm-loop/`.
The source tree is created by T-000.

This file is quoted into task packets. Keep it accurate and compact.

---

## Measured environment facts (verified live — trust these, do not re-derive)

Verified by the orchestrator on the real machine before dispatch. Where a doc and this
section disagree about the environment, **this section is right**.

| Fact | Value | How verified |
|---|---|---|
| Platform | macOS arm64 (Darwin 25.4), 10 CPUs | `sysctl -n hw.ncpu` |
| Docker | 29.4.1, compose v5.1.3, daemon running | `docker info` |
| Docker VM RAM | **7.75 GiB total** | `docker system info` |
| Docker RAM already used by unrelated stacks | **~2.7 GiB** (Supabase ×11, OpenEMR, MariaDB, keystone-postgres) | `docker stats` |
| **RAM available to ProxyShop** | **~4.4 GiB — hard budget** | measured headroom |
| Node | v25.9.0 (**non-LTS odd major**), npm 11.12.1 | `node --version` |
| System python3 | 3.9.7 — **too old, do not use** | `python3 --version` |
| uv | installed; CPython **3.12.13** and 3.13.13 available | `uv python list` |
| `ANTHROPIC_API_KEY` | **unset** — every ticket verify must use deterministic doubles (C9/A2) | env probe |
| Shopify credentials | **absent** — stub only; live is demo-only (T-085) | env probe |
| Disk free | 407 GiB | `df -h` |
| CodeGraph | `.codegraph/` indexed, daemon live, CLI v1.5.0 | `codegraph --version` |

### Ports already taken on this host — DO NOT BIND THESE
`3000, 4011, 5000, 5173, 7000, 8300, 8320, 8765, 8877, 8901, 9300,
54321, 54322, 54323, 54324, 54327, 55432` (Supabase, OpenEMR, MariaDB, keystone-postgres,
misc dev servers). ProxyShop must use a port block verified free at compose time.

### Database facts verified by live probe

- **Neo4j 5.26.30 Community**: 1024-dim **cosine** vector index works
  (`CREATE VECTOR INDEX ... OPTIONS {indexConfig:{'vector.dimensions':1024,
  'vector.similarity_function':'cosine'}}`), uniqueness constraints work, and
  `db.index.vector.queryNodes` returns correct scores. **DESIGN C2's graph requirement is
  satisfiable on this machine.**
- **Neo4j Community supports exactly ONE database.** `CREATE DATABASE worker1` returns
  `Unsupported administration command`. `SHOW DATABASES` lists only `neo4j` and `system`.
  → Per-worker Neo4j database isolation is **impossible**; see the isolation design below.
- **Postgres 16**: per-worker databases (`CREATE DATABASE proxyshop_w1..N`) work — this is
  the Postgres parallel-isolation strategy.
- **Postgres schema/role grants satisfy C3/S7 exactly.** Verified: role `exchange` granted
  `USAGE` on `ledger` only can read `ledger.*`; reading `sealed.envelopes` fails with
  `ERROR: permission denied for schema sealed`; reading `vault.*` likewise; and
  `information_schema.tables WHERE table_schema='sealed'` returns **0 rows** for that role —
  the exchange role cannot even enumerate sealed tables. Writing to `ledger` is also denied,
  matching DESIGN's "`exchange` role: no write".
- **Redis 7**: 16 logical databases (`CONFIG GET databases` → 16) — per-worker `SELECT <n>`
  is the Redis isolation strategy.

---

## Conventions in force (apply in every ticket)

- **Python 3.12** via uv. Never the system 3.9.
- **`packages/contracts` is the schema home.** DESIGN's Interfaces section still says
  "packages/protocol" and T-000's acceptance criterion 3 repeats it — that name is **stale**.
  Every ticket scope glob and T-010 use `packages/contracts`, and tickets.json is the
  executable source of truth. See `.swarm-loop/decisions.md`.
- **Offline always (C9).** No ticket verify may require network, live Shopify, or a live LLM.
  LLM calls and embeddings use deterministic test doubles in all verification.
- **Tests are append-only.** Adding tests/assertions is free; modifying or deleting an
  existing assertion requires the recorded `justify-test-edit` justification.
- **No worker touches a dependency manifest.** T-000 installs the complete dependency set up
  front precisely so that no later ticket ever needs to.

## Known hazards

- **RAM is the binding constraint**, not CPU. The whole DB stack must fit in ~4.4 GiB
  alongside up to ~8 concurrent test suites. Compose services carry explicit memory limits.
- **Neo4j single-database** is the one hard limit on swarm width for graph-touching tickets
  (T-012, T-020–T-024, T-031).
- **Node 25 is non-LTS.** Toolchain support is unverified for vitest / Next.js / React
  Router v7 / `@shopify/*`; the scaffold pins a Node version rather than trusting v25.
- Shared test directories are claimed by many tickets each (`apps/exchange/tests/**` by six,
  `apps/trust/tests/**` by seven, etc.). Ownership is narrowed **per test file**, and shared
  `conftest.py` files are created by T-000 so no worker writes them.
