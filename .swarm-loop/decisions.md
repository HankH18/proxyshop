# Pinned decisions — quoted into every task packet

These exist to remove ambiguity that two tickets would otherwise resolve differently.
Where the doc set is ambiguous or self-contradictory, **this file is the ruling**, and each
ruling names the evidence it rests on. The doc set still owns *what* to build; this file only
settles *which reading of it is authoritative*.

Workers: follow these exactly. If one of these rulings looks wrong, say so in `CONCERNS` —
do not quietly implement the other reading.

Sections marked **[verified]** were established by running something on this machine, not by
reading a document.

---

## D1 — The schema package is `packages/contracts`, never `packages/protocol`

DESIGN's *Interfaces* section opens "All cross-service schemas live in `packages/protocol`",
and T-000's acceptance criterion 3 repeats "packages/protocol". Both are **stale**. DESIGN's
own merged-layout paragraph supersedes them: "`packages/contracts` is the schema home
(formerly 'protocol')". Every one of the 44 ticket scope globs uses `packages/contracts`, as
does T-010's objective and verify command — and `tickets.json` is the executable source of
truth (TASKS.md says so explicitly).

**Ruling:** the directory is `packages/contracts`. The string `packages/protocol` must not
appear anywhere in the source tree.

## D2 — Python is 3.12, managed by uv; the system 3.9 is never used **[verified]**

`python3` on this machine is 3.9.7. uv has CPython 3.12.13 available. All Python runs —
including every metric command — go through the project virtualenv, so metric commands
reference `.venv/bin/python`, never a bare `python3`.

## D3 — Verification is offline, always (C9) **[verified: no keys present]**

Neither `ANTHROPIC_API_KEY` nor any Shopify credential exists in this environment. Every
ticket's `verify` command must pass with no network, no live LLM, and no live Shopify —
against `services/shopify-stub` and deterministic doubles. Live dev-store runs belong to the
T-085 demo runbook only, never to a ticket gate. A ticket whose verify needs a credential is
a defect in that ticket, to be reported, not worked around.

## D4 — Neo4j Community has exactly ONE database; graph isolation is by tenant, not by database **[verified]**

`CREATE DATABASE worker1` on neo4j:5.26 Community returns `Unsupported administration
command`; `SHOW DATABASES` lists only `neo4j` and `system`. Per-worker Neo4j *databases* are
therefore impossible.

**Ruling:** graph-touching tests isolate by tenant scoping inside the single database, and
the scheduler treats "writes to Neo4j" as a shared surface. Postgres isolates by per-worker
database (`CREATE DATABASE proxyshop_w<n>` — verified working) and Redis by per-worker
logical DB index (16 available — verified).

## D5 — The Postgres grant model that satisfies C3/S7 is proven; reproduce it exactly **[verified]**

Verified live: with `GRANT USAGE ON SCHEMA ledger` only, role `exchange` reads `ledger.*`
successfully; `SELECT * FROM sealed.envelopes` fails with `ERROR: permission denied for
schema sealed`; `vault.*` likewise; `information_schema.tables WHERE table_schema='sealed'`
returns **0 rows** for that role; and `INSERT` into `ledger` is denied, matching DESIGN's
"`exchange` role: no write".

**Ruling:** schema-level `USAGE` grants (never table-level grants into `sealed`) are the
enforcement mechanism for C3. The exchange role is granted `USAGE` on `ledger` and `app`
only, and never on `sealed` or `vault`.

## D6 — Neo4j vector index parameters are fixed and proven **[verified]**

`CREATE VECTOR INDEX product_embedding FOR (p:Product) ON (p.embedding) OPTIONS
{indexConfig: {'vector.dimensions': 1024, 'vector.similarity_function': 'cosine'}}` works on
neo4j:5.26.30 Community, and `db.index.vector.queryNodes` returns correct cosine scores for a
1024-dim vector. Use exactly these parameters (DESIGN C2: 1024 dims, cosine).

## D7 — `npx vitest run <path>` is a path FILTER and isolates correctly; only the root verify passes with no tests **[verified]**

Verified: `vitest run pkgA` ran only pkgA's tests and exited 0 while a sibling package's
failing test was ignored — so every ticket's `npx vitest run <path>` form works as written.
Verified also: a filter matching zero test files exits **1**, and `--passWithNoTests` makes
it exit 0.

**Ruling:** the root `make verify` uses `--passWithNoTests` (so T-000's empty scaffold is
green, per its acceptance criterion 1). Individual ticket verify commands do **not** use it —
a ticket whose tests do not exist yet is correctly red.

## D8 — Pin TypeScript to the 5.x line **[verified: `typescript@latest` is 7.0.2]**

`npm install typescript@latest` now resolves to TypeScript **7.0.2**, the rewritten
Go-based compiler. Next.js, React Router v7 and the `@shopify/*` toolchain are not
established on it, and an unattended run is the wrong place to discover that.

**Ruling:** pin `typescript` to the 5.x line in every `package.json`. Node 25.9.0 itself is
fine — vitest 4.1.11 and tsc both run correctly on it **[verified]**.

## D9 — Bind only ports verified free; this host is busy **[verified]**

Already bound on this machine: `3000, 4011, 5000, 5173, 7000, 8300, 8320, 8765, 8877, 8901,
9300, 54321–54324, 54327, 55432` (Supabase ×11, OpenEMR, MariaDB, keystone-postgres). The
first compose attempt failed on 55432 precisely this way.

**Ruling:** ProxyShop's compose binds a contiguous block verified free at scaffold time, and
records it in `.env.example`. No service hard-codes a default port that collides with the list
above.

## D10 — Memory is the binding constraint, not CPU **[verified]**

The Docker VM is 7.75 GiB and ~2.7 GiB is already committed to unrelated stacks, leaving
**~5.1 GiB**. Every compose service carries an explicit memory limit, and the DB stack is
budgeted to fit well inside that with room for concurrent test suites. A container OOM-killed
mid-wave would surface as an inexplicable test failure, so the limits are set deliberately
rather than left unbounded.

---

_Further rulings (rank formula, hash-chain definition, embeddings default, LedgerEvent
payload shape, checkout URL shape) are appended from the adversarial intake report before
first dispatch._
