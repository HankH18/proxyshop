# Running the ProxyShop stack under docker compose

This is the deploy lane's record of how the deployables actually start, what was observed
when they did, and what still does not work. Everything below was RUN, not reasoned about;
where something was not run, it says so.

## The defect this page used to describe, and no longer does

The previous revision of this page told you that `PROXYSHOP_ROLE_PASSWORD` was **required**,
while `.env.example` told you to leave it **unset**. Both were honest and they disagreed, so
the documented starting move — `cp .env.example .env` — produced a stack that could not
serve. It was worse than a broken stack, because it did not look broken:

```
$ docker compose ps
SERVICE        STATUS
buyer-svc      Up 12 seconds (healthy)
exchange       Up 12 seconds (healthy)
ingest         Up 12 seconds (healthy)
merchant-svc   Up 12 seconds (healthy)
neo4j          Up About a minute (healthy)
postgres       Up About a minute (healthy)
redis          Up About a minute (healthy)
trust          Up 12 seconds (healthy)

$ curl -s localhost:8084/events/head
{"detail":{"error":"store_unavailable","message":"the ledger writer could not reach its
 datastore: PoolTimeout: couldn't get a connection after 10.00 sec"}}              503
$ curl -s -X POST localhost:8081/buyer/auth/magic-link -d '{"email":"x@example.com"}'
Internal Server Error                                                             500
$ curl -s localhost:8082/install/shops
{"error":"admin-api-not-configured","missing":["MERCHANT_ADMIN_TOKEN"], ...}      503
```

Eight rows of `(healthy)` over a stack whose database-backed routes all failed. Three
separate causes, all of them now fixed, all of them recorded here because the shape of the
failure matters more than any one of them:

1. **The healthchecks could not fail.** Every service probed `/openapi.json`, which proves
   the ASGI app imported and uvicorn is answering — a *liveness* probe wearing a *readiness*
   probe's name. Every fragment said so in place; `apps/exchange/compose.yaml` was explicit:
   *"It does NOT prove the datastores are reachable."* The author knew. It was left owed.
2. **The credential could not resolve.** The fragments interpolated
   `${PROXYSHOP_ROLE_PASSWORD:-}` into their DSNs, so an unset variable produced
   `postgresql://trust_rw:@postgres:5432/...`. An explicitly *empty* password is not the same
   thing as *no* password — libpq rejects it before it reaches the server:
   `fe_sendauth: no password supplied`.
3. **Nothing ever applied the migrations.** `make deps-up` creates `proxyshop_w<N>` and
   stops. The root `conftest.py` migrates for *tests*, so the suite never noticed; the
   deployment, which has no conftest, ran against a database holding nothing but `public`.

## What is deployable

`docker compose config --services` names eleven unprofiled services in one project (`name: proxyshop`,
pinned in the root file so a worktree cannot spawn its own stack):

| service            | port (env override)         | image built from               | entrypoint              |
|--------------------|-----------------------------|--------------------------------|-------------------------|
| `postgres`         | 5432 (`PG_PORT`)            | `postgres:16-alpine`           | —                       |
| `neo4j`            | 7474/7687 (`NEO4J_*`)       | `neo4j:5.26-community`         | —                       |
| `redis`            | 6379 (`REDIS_PORT`)         | `redis:7-alpine`               | —                       |
| `buyer-svc`        | 8081 (`BUYER_SVC_PORT`)     | `apps/buyer/Dockerfile`        | `buyer_svc.main:app`    |
| `merchant-svc`     | 8082 (`MERCHANT_SVC_PORT`)  | `apps/merchant/Dockerfile`     | `merchant_svc.main:app` |
| `exchange`         | 8083 (`EXCHANGE_PORT`)      | `apps/exchange/Dockerfile`     | `exchange.main:app`     |
| `trust`            | 8084 (`TRUST_PORT`)         | `apps/trust/Dockerfile`        | `trust.main:app`        |
| `ingest`           | 8085 (`INGEST_PORT`)        | `services/ingest/Dockerfile`   | `ingest.main:app`       |
| `store-agent`      | 8086 (`STORE_AGENT_PORT`)   | `packages/store-agent/Dockerfile` | `store_agent.main:app` |

`shopify-stub` is on the `e2e` profile, so it starts only with `--profile e2e`.
`services/sim` and `apps/seller-reference` are run-to-completion **jobs**, not services —
`docker compose run --rm sim` / `--rm seller-reference`. Every port above is in
`.env.example` now; they used to be documented only on this page.

**Six more services sit behind two profiles**, and they are the shopper demo rather than the
platform. `--profile demo` adds `buyer-web` (8080, `BUYER_WEB_PORT`) and four configured
store agents — `store-agent-gaiaherbs` (8090), `store-agent-toniiq` (8091),
`store-agent-paradiseherbs` (8092), `store-agent-oregonswildharvest` (8093), all on container
port 8086 and all built from the one store-agent image. `--profile corpus` adds
`corpus-loader`, a run-to-completion job that replays the recorded real catalogues into Neo4j.
The unprofiled `store-agent` above stays the unconfigured template it always was.
`docs/demo/shopper-demo.md` is the runbook for all of it.

## Bringing it up

```bash
# 0. Load the environment. `cp .env.example .env` gives `docker compose` its interpolation
#    values, and nothing on the deps-up path reads the copy — not the Makefile, not
#    scripts/db_init.py, not any Python at import — so `make deps-up` straight after the
#    copy fails with `FATAL: PROXYSHOP_WORKER is unset`. (One thing DOES read .env:
#    scripts/verify.sh sources it on every `make verify`. See the subsection below.)
cp .env.example .env
set -a && . ./.env && set +a

# 1. The datastores, healthchecked, plus this worker's database.
make deps-up

# 2. The schema. `make deps-up` creates an EMPTY database, and this is the step that was
#    missing from every revision of this runbook. It is no longer a line to type: step 1
#    runs it, and this target stays for a database that predates a new migration.
make db-migrate

# 3. The services.
docker compose up -d --build buyer-svc merchant-svc exchange trust ingest

docker compose ps          # every row (healthy) — and it now means something
```

**`(healthy)` is not "the demo works", and the gap is bigger than a probe can close.** The
stack above is CONFIGURED now — `apps/exchange/compose.yaml` mounts a real deployment
document and switches the graph roster on, `apps/buyer/compose.yaml` mounts the buyer's — but
it holds an EMPTY Neo4j and serves no shopper page until two more steps run. For the whole
shopper journey, follow `docs/demo/shopper-demo.md`, which adds the corpus load, the SPA
build, the `demo` compose profile that starts four real store agents, and an after-deploy
probe that drives a real auction rather than reading a health column.

Take it down with `docker compose down`. Note that `make deps-down` is
`docker compose down -v`, which destroys the pgdata and neo4jdata volumes.

`PROXYSHOP_ROLE_PASSWORD` is **not required**, and this page no longer claims it is. Leaving
it unset gives the documented dev default on both the seed side and the connect side. Setting
it before the pgdata volume is first created changes both together — the initdb hook runs
once, so setting it later changes nothing.

### A `.env` in the worktree re-points `make verify`, not just compose

Worth knowing before you relocate anything in it. `scripts/verify.sh:19-24` sources `.env`
**itself**, on every run, with `set -a`:

```sh
if [ -f "$ROOT/.env" ]; then
  _keep_worker="${PROXYSHOP_WORKER:-}"
  set -a; . "$ROOT/.env"; set +a
  if [ -n "$_keep_worker" ]; then export PROXYSHOP_WORKER="$_keep_worker"; fi
  unset _keep_worker
fi
```

Every assignment in the file is exported into the pytest process, and only `PROXYSHOP_WORKER`
is shielded. So a `.env` carrying a relocated port block silently points the whole suite at a
stack that may not be there — and a clean caller shell does not protect you, because the
script does the sourcing.

Measured, on this branch, by running the gate with a relocated `.env` present:

```
2 failed, 5600 passed, 133 skipped, 1 deselected, 57 xfailed in 358.39s
  postgres localhost:15432 · redis localhost:16379 · neo4j-bolt localhost:17687
```

against `5735 passed, 1 deselected, 57 xfailed` and **0 skipped** with no `.env` in the tree.
Same population — 5600 + 133 + 2 = 5735 — with 135 tests diverted onto dead endpoints. The
133 skips are the tell: a datastore-marked test that skips looks exactly like one that passed.

If you relocate ports to run a second stack, remove `.env` before running `make verify`, or
expect the gate to grade a different cluster than you think.

That last sentence used to be the whole mitigation, and it was not one — it asked a reader to
notice a skip count. The gate now notices for you: see the next section.

## `make verify` with the datastores down

Two things `make verify` could not previously see, both fixed in `scripts/verify.sh`.

### 1. The release blockers are now inside the gate

`.swarm-loop/acceptance` holds 120 frozen acceptance criteria, three of which are the **S8
release blockers** — a blacklisted seller never surfacing as eligible, a contradicted hard
constraint never winning, and no checkout URL outside the registered seller domain. `make
verify` did not run one of them. `testpaths` in `pyproject.toml` lists eight directories and
`.swarm-loop` is not among them, and no step in `verify.sh` ever named the directory, so the
build's headline metric was measured over a file set that excluded the tests deciding
whether the release is allowed.

It now runs as its own step:

```console
$ PROXYSHOP_WORKER=4 ./scripts/verify.sh acceptance
ACCEPTANCE: 120/120 frozen criteria passing; S8 release blockers present: S8-1, S8-2, S8-3
OK: acceptance
```

That step is inside `all` and `check`, so `make verify` and `make check` both carry it. It
costs about two seconds and runs first, ahead of the ~7,400-test pytest step, so a red
blocker is reported in the first ten seconds rather than after six minutes.

It runs as a **separate pytest process**, through the frozen suite's own runner
(`.swarm-loop/acceptance/run.py`), and that is not incidental: sharing one pytest session
with `apps`/`packages`/`services` produces phantom failures that vanish in isolation. Do not
"fix" anything here by deleting `.swarm-loop` from `norecursedirs` — that key was never what
hid the suite (`pytest --collect-only .swarm-loop/acceptance` collects all 120 tests with it
in place), and removing it only invites the contaminated session back.

### 2. A run that skipped the datastore checks no longer exits 0

Datastore-backed tests skip cleanly when the store they need is unreachable, which is the
right behaviour per test and the wrong behaviour for the run as a whole. Measured on this
tree with Postgres pointed at a closed port and nothing else changed:

```console
$ PROXYSHOP_PG_DSN_ADMIN=postgresql://nobody@127.0.0.1:1/postgres \
    pytest apps/trust/tests/test_schema_grants.py -q
22 passed, 36 skipped   # exit 0
```

36 live-Postgres least-privilege checks for C3/S7 did not run, and the shell saw success.
Repo-wide the class is 288 `docker`-marked tests. There is no CI here, so a green local run
is the only signal anybody gets.

`verify.sh` now probes the three compose endpoints — through `proxyshop_support.reachability`,
the same per-service prober the tests use, reading the same `PROXYSHOP_PG_DSN_ADMIN` /
`NEO4J_URI` / `REDIS_URL` — after any step that ran python tests. When they all answer:

```
DATASTORE COVERAGE: postgres, neo4j-bolt and redis all answered — the
                    docker-marked tests in this run really ran.
```

When one does not, the run prints a banner naming the unreachable endpoints and the size of
the class that was skipped, and **exits 3**. The check is evaluated last, so every other step
still runs and you get the whole picture before the refusal.

**Docker is not mandatory.** `lint`, `types`, `vitest` and `acceptance` touch no datastore
and are unaffected. `check` and `pytest` run to completion and can be finished offline:

```console
$ PROXYSHOP_ALLOW_DEGRADED=1 ./scripts/verify.sh check
...
DEGRADED: check — finished, but the datastore-backed checks never ran.
          This is NOT a full pass. Do not cite it as one.
```

**There is no opt-out for `all`.** `verify.sh all` is what `make verify` runs, and its exit
status is this repo's release signal — the frozen `build_succeeds` metric is literally
`if make verify; then echo 1; else echo 0; fi` and reads nothing else. A banner it cannot
parse is not a signal to it, and an opt-out living in someone's shell profile or `.env` would
quietly restore the false green. `make verify` requires the stack; `make deps-up` provides it.

This is also what now catches the relocated-`.env` case above: a `.env` pointing the suite at
a cluster that is not there trips this gate instead of showing up as 133 skips nobody reads.

## Continuous integration — the machine that has the datastores

The section above closes the false green *locally*: `make verify` now refuses to exit 0 when
the datastore-backed checks did not run. That refusal is only worth as much as the number of
machines that actually bring the stack up, and until now that number was "however many
developers remembered to run `make deps-up`". This repository had **no CI at all** — no
`.github/`, no `.gitlab-ci.yml`, no `.circleci` — so a green local run was the only signal
anybody got, and an offline one proved nothing about database least-privilege.

Two files now, one gate:

| file | target | how it gets its datastores |
|------|--------|----------------------------|
| `.gitlab-ci.yml` | labs.gauntletai.com — the **submission** target | three CI `services:` |
| `.github/workflows/verify.yml` | the GitHub mirror | `make deps-up`, this repo's own compose file |

**Neither one re-spells the gate.** Both call `./scripts/bootstrap.sh` and then `make verify`,
because `scripts/verify.sh` is where ruff, `ruff format --check`, import-linter, the
banned-reset gate, eslint, mypy, `tsc -b`, the frozen acceptance suite in its own interpreter
(with the three S8 release blockers asserted by id), the ~7,600-test pytest run, vitest, the
datastore coverage gate and `scripts/check_verify_contracts.py` already live. A second copy in
YAML would be free to drift from the copy the frozen `build_succeeds` metric reads. `Makefile`
and `scripts/verify.sh` are hash-pinned in `.swarm-loop/manifest.json`; the pipeline calls
them and does not touch them.

### The runner has to be turned on — that part is the project owner's

Nothing here can enable itself. On labs.gauntletai.com, **Settings → CI/CD → Runners** is
where a runner is enabled for the project, and until one is, the file is inert and no
pipeline appears. What it needs is modest: the **docker** executor, **not** privileged. That
is the whole reason the GitLab job declares CI services instead of running `docker compose` —
a GitLab docker-executor job reaches a service by its alias and never on its own `localhost`,
so compose would mean docker-in-docker and a privileged runner.

### What each job adds beyond `make verify`

Two steps, and both close a hole `verify.sh` structurally cannot:

```console
$ .venv/bin/python scripts/ci_datastore_proof.py wait          # fail in seconds, by name
$ .venv/bin/python scripts/ci_datastore_proof.py prove         # the class really EXECUTED
$ .venv/bin/python scripts/ci_datastore_proof.py check-config  # CI still matches compose
```

`verify.sh`'s coverage gate proves the three endpoints were **reachable**. Reachability is
necessary and it is not sufficient — a stripped marker, a fixture that skips for a reason
that is not reachability, or a marker expression that deselects the class all leave three
ports answering while the class evaporates. `prove` asserts the **outcome** instead: it runs
`-m "docker and not needs_model"` with a JUnit report and refuses unless every member
executed. Measured on this tree, worker 10, with the compose stack up:

```console
$ pytest -q -m "docker and not needs_model"
309 passed, 7354 deselected in 88.53s
```

and the same command with Postgres pointed at a closed port:

```console
$ PROXYSHOP_PG_DSN_ADMIN=postgresql://nobody@127.0.0.1:1/postgres \
    .venv/bin/python scripts/ci_datastore_proof.py prove
151 passed, 163 skipped, 7462 deselected in 32.81s      # pytest exit 0 — the lie

DATASTORE PROOF
    collected         : 314
    executed          : 151
    skipped           : 163
    pytest exit       : 0
DATASTORE PROOF FAILED — the datastore-backed checks did not all execute.
  * 163 datastore-backed tests SKIPPED. ...
                                                        # script exit 1 — the refusal
```

### Three things measured the hard way, all of them now guarded

1. **A `NEO4J_*` variable in GitLab's `variables:` kills the Neo4j service.** GitLab injects
   a job's variables into its *service* containers, and the Neo4j image turns every `NEO4J_*`
   environment variable into a `neo4j.conf` setting:

   ```console
   $ docker run -e NEO4J_AUTH=neo4j/proxyshop_dev_pw -e NEO4J_URI=bolt://neo4j:7687 \
       neo4j:5.26-community
   Failed to read config: Unrecognized setting. No declared setting with name: URI.
   $ docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}}' <id>
   exited exit=1
   ```

   Every variable the *test process* needs to find a datastore is therefore exported in
   `before_script`, where no service container sees it. `check-config` fails the build if one
   moves back into `variables:`.

2. **`PROXYSHOP_PG_DSN_APP` must not be set.** Set to the password-free shape `.env.example`
   ships, it fails two tests in `apps/buyer/svc/tests/test_auth_vault.py` with
   `psycopg.OperationalError: ... fe_sendauth: no password supplied`, because `buyer_svc`
   reads that variable directly instead of through `proxyshop_support.postgres.role_dsn`.
   Bisected one variable at a time: `PROXYSHOP_PG_DSN_ADMIN` and `_VAULT` are harmless, `_APP`
   is not. Left unset, `role_dsn` builds the DSN from `PGHOST`/`PG_PORT` and supplies the
   password itself. `PROXYSHOP_PG_DSN_ADMIN` is the one that *must* be set on GitLab, because
   `proxyshop_support.reachability` reads the Postgres endpoint from it and from nowhere else
   — it does not consult `PGHOST`/`PG_PORT`, so without it the coverage gate would probe
   `localhost:5432` on a job whose Postgres is at `postgres:5432`.

3. **`db/init/00-roles.sql` does not need to be mounted in CI**, which is what makes the
   services approach viable at all. Measured against a bare `postgres:16-alpine` with no
   initdb hook and no `db/init` bind mount:

   ```console
   $ docker exec <pg> psql -U proxyshop -tAc \
       "select rolname from pg_roles where rolname in ('exchange','trust_rw','buyer_vault','app')"
                                                    # nothing — the hook never ran
   $ pytest apps/trust/tests/test_schema_grants.py -q
   58 passed in 14.38s
   $ docker exec <pg> psql ... same query
   app
   buyer_vault
   exchange
   trust_rw
   ```

   `db/migrations/0001_schemas_roles_grants.sql` re-creates the four cluster-global roles
   idempotently before it grants, so the C3/S7 least-privilege gate runs on a stock image.

### The worker index is 12, and that is three constraints at once

`PROXYSHOP_WORKER=12` in both files. Not 0: worker 0 is reserved for the frozen
`build_succeeds` measurement, and `scripts/db_reclaim.py` protects `proxyshop_w0` under every
flag. Under 16: a stock `redis:7-alpine` answers `CONFIG GET databases` with `16`, and
`proxyshop_support.worker.redis_db_index` **refuses** an index at or above the running
server's ceiling rather than silently sharing a logical DB. And clear of 1..10, the band the
local build lanes use. `check-config` fails the build on all three.

### No secrets, no network, no deploy

Neither file reads a secret or a masked variable, and there is no deploy, push or publish
step in either. `LLM_PROVIDER` is deliberately left unset so it keeps its default
deterministic offline double, and the embedding provider keeps `lexical` (D56), so the suite
makes no outbound call; `needs_model` is deselected by `make verify` for the same reason. The
two credentials in `.gitlab-ci.yml` — `POSTGRES_PASSWORD` and `NEO4J_AUTH` — are the ephemeral
dev defaults that already live in `docker-compose.yml`, for containers destroyed when the job
ends, and `check-config` fails the build if either stops matching compose.

### How far this was proven, and where it stops

- `actionlint` and `check-jsonschema --builtin-schema vendor.github-workflows` both accept
  `.github/workflows/verify.yml`, and both reject an unknown job key when one is introduced.
- `check-jsonschema --builtin-schema vendor.gitlab-ci` accepts `.gitlab-ci.yml`. Its green is
  weaker than actionlint's and the difference is worth stating: it rejects wrong types, bad
  enums and wrong shapes (`stages: 5`, `when: banana`, `interruptible: "yes-please"`,
  `services:` as a scalar, an integer in `rules:` — all refused) but **permits an unknown
  job key**, because GitLab jobs legitimately carry extension keys.
- GitLab's own CI Lint API is the authoritative validator and was **not** reachable: the
  credential on this machine is repository-scoped, and `POST /api/v4/projects/:id/ci/lint`
  answers `403 insufficient_scope`. That check is owed the first time a pipeline actually
  runs.
- **No runner has executed either file.** What has been executed, end to end and locally, is
  the job's own command sequence — see below.

### The job's command sequence, executed

Run in a throwaway worktree at the committed `d4386a8` — a fresh checkout, the way a runner
sees one, rather than the working tree with five lanes' uncommitted edits in it. This is the
GitHub job's sequence verbatim; the GitLab job differs only in how the datastores arrive.

```console
$ ./scripts/bootstrap.sh
    python 3.12.13 at .../proxyshop-worktrees/ci-w10/.venv
    typescript 5.9.3
    .pkgroot namespaces import from an unrelated cwd: .../.pkgroot/contracts/__init__.py
OK: bootstrap                                                       # 6.0s, warm caches

$ PROXYSHOP_WORKER=12 make deps-up
 Container proxyshop-redis-1     Healthy
 Container proxyshop-neo4j-1     Healthy
 Container proxyshop-postgres-1  Healthy
OK: database proxyshop_w12 exists (via postgresql://proxyshop:...@localhost:5432/postgres)

$ .venv/bin/python scripts/ci_datastore_proof.py wait
    up: postgres (localhost:5432)
    up: neo4j-bolt (localhost:7687)
    up: redis (localhost:6379)
OK: all three datastores answered.

$ PROXYSHOP_WORKER=12 make verify
All checks passed!                                                  # ruff
Contracts: 2 kept, 0 broken.                                        # import-linter
ACCEPTANCE: 120/120 frozen criteria passing; S8 release blockers present: S8-1, S8-2, S8-3
FAILED apps/exchange/tests/test_loss_reports_served.py::test_the_served_report_carries_no_rival_amount
1 failed, 7596 passed, 1 deselected, 7 xfailed, 19 warnings in 565.55s (0:09:25)
SELECTION: 1 deselected, 0 skipped  <-  pytest -q -m not needs_model

$ ./scripts/verify.sh vitest
 Test Files  20 passed (20)
      Tests  1044 passed (1044)
OK: vitest

$ .venv/bin/python scripts/ci_datastore_proof.py prove
301 passed, 7304 deselected in 85.07s (0:01:25)
DATASTORE PROOF
    collected         : 301
    executed          : 301
    skipped           : 0
    failed / errored  : 0 / 0
OK: all 301 datastore-backed tests executed and passed.             # exit 0

$ .venv/bin/python scripts/ci_datastore_proof.py check-config
OK: .gitlab-ci.yml and .github/workflows/verify.yml agree with docker-compose.yml.

$ .venv/bin/python scripts/check_verify_contracts.py                # exit 0
```

**`SELECTION: 1 deselected, 0 skipped` is the line this whole page has been building toward.**
The single deselection is the `needs_model` test; nothing else in 7,600 was passed over. The
same run offline reports several hundred skips and still exits 0 on the pytest step, which is
the false green `verify.sh`'s coverage gate and `prove` exist to refuse.

**The one failure is not this pipeline's, and it is already fixed in flight.**
`test_the_served_report_carries_no_rival_amount` is red at the commit and **passes on the
working tree** with the exchange lane's uncommitted edits applied — checked both ways. It is
recorded here rather than trimmed out, because a transcript that shows only the green half is
the thing this page is against.

The provisioning half was checked against the real CI base image rather than assumed:

```console
$ docker run --rm node:22-bookworm sh -lc 'node -v; npm -v; make --version | head -1; git --version'
v22.23.2                                    # package.json needs >= 22.12 (D8)
10.9.8
GNU Make 4.3
git version 2.39.5

$ docker run --rm node:22-bookworm sh -lc '
    curl -LsSf "https://astral.sh/uv/0.11.8/install.sh" | env UV_INSTALL_DIR=/usr/local/bin sh
    uv --version'
downloading uv 0.11.8 aarch64-unknown-linux-gnu
installing to /usr/local/bin
uv 0.11.8 (aarch64-unknown-linux-gnu)
```

## Where each service is told about the others

Nothing in this stack discovers a peer. Each service is *told*, out of its own environment,
and this is the record of what each one reads:

| service       | reads                                                                        | how the fragment leaves it |
|---------------|------------------------------------------------------------------------------|----------------------------|
| `exchange`    | `EXCHANGE_DEPLOYMENT` (path) / `EXCHANGE_DEPLOYMENT_JSON` (inline)            | declared and **empty** — `apps/exchange/compose.yaml:83-84` |
| `buyer-svc`   | `BUYER_DEPLOYMENT` (path) / `BUYER_DEPLOYMENT_JSON` (inline), else `EXCHANGE_URL` | `EXCHANGE_URL` is already **populated** — `apps/buyer/compose.yaml:47` |
| `store-agent` | `STORE_AGENT_CONTEXT` (path)                                                  | declared and **empty** — `packages/store-agent/compose.yaml:56` |

Every one of those roots is fail-closed and none of them defaults to anything: a service given
no document binds no collaborators and refuses, rather than inventing a peer. A malformed
document is a `503` naming the offending key on the *next request* — not a `500`, and not a
crash at boot — and the failure is not cached, so fixing the file serves the next request
without a restart.

### The buyer service's exchange address

`POST /buyer/intent/confirm` is the shopper saying yes, and it is the request that opens an
auction. It reads its client off `app.state.auction_client`, and until this branch nothing in
the tree set it — so on a deployed stack the shopper's confirmation had nowhere to go.
Measured against real `uvicorn` processes on loopback, one for the exchange and one for the
buyer service, with no deployment configured:

```
$ curl -X POST :50277/buyer/intent/confirm -d '{"intent":{...},"confirmed":true}'
{"detail":"confirm() was given no auction client, so the confirmed intent has nowhere to
 go. Pass the exchange client that owns POST /auctions."}                          503
```

That is still the answer for a buyer service nobody has configured, deliberately. What
changed is that there is now something to configure. **The compose stack needs no change**:
`apps/buyer/compose.yaml:47` already carries

```yaml
      # The exchange is reached by service name inside the network, never by localhost.
      EXCHANGE_URL: "${EXCHANGE_URL:-http://exchange:8083}"
```

and until this branch `grep -rn EXCHANGE_URL --include='*.py'` over the repo returned
**nothing** — the address was declared, commented, and read by no line of code. It is read
now, as the lowest-precedence of the three sources. Same two processes, the buyer service
started with nothing but that variable:

```
$ EXCHANGE_URL=http://127.0.0.1:57026 uvicorn buyer_svc.main:app --port 57028 --workers 1
$ curl -X POST :57028/buyer/intent/confirm -d '{"intent":{...},"confirmed":true}'
{"auction_id":"auction-ee74abd0-3d71-499f-a2a1-c438d29ada65",
 "intent_id":"int-envurl-1","created_at":"2026-09-05T09:18:01.833627Z"}            201

# and in the exchange's own log, a different process:
INFO:     127.0.0.1:57038 - "POST /auctions HTTP/1.1" 201 Created
```

For anything beyond the address — a longer call timeout — write a document instead, and mount
it or inline it:

```bash
BUYER_DEPLOYMENT_JSON='{"exchange_url":"http://exchange:8083","request_timeout_seconds":20}'
```

The document outranks `EXCHANGE_URL`, and `BUYER_DEPLOYMENT` (a path) outranks the inline one.
Every source takes the same validation, so a typo is loud wherever it was written:
`EXCHANGE_URL=exchange:8083` — no scheme, therefore no host — is a `503` saying so, not a
request to a URL naming no server.

**Not run here, and the ports say so:** none of the above was measured inside a container. It
was measured with `uvicorn` over loopback, one process per service, on ephemeral ports across
two separate runs — which is why the numbers above do not match each other or the `8081/8083`
of the compose stack. That exercises the same code the image runs, and not the image, the
network alias, or the healthcheck. `buyer-svc`'s readiness probe does not check the exchange
either: see the rule in the next section about probing only what a service uses, and note that
this is a peer service rather than a datastore, so an unreachable exchange is a `502` on the
confirm rather than an unhealthy container.

## Readiness, and why the health signal can be trusted now

Each service's healthcheck runs `proxyshop_support.service_launch ready`, which keeps the
`/openapi.json` check and adds the service's **own** datastore dependencies, reached with the
service's **own** credentials:

| service        | readiness checks                                       | why exactly these |
|----------------|--------------------------------------------------------|-------------------|
| `buyer-svc`    | http + postgres as `buyer_vault`, `vault.pseudonym_history` | the role and table `auth/routes.py` connects as and `vault/store.py` reads |
| `merchant-svc` | http + `MERCHANT_ADMIN_TOKEN` set                      | it touches **no** datastore; the token is what its admin routes refuse without |
| `exchange`     | http + postgres as `exchange`, `app.sellers` + redis   | redis holds auction state; postgres is what the fragment declares |
| `trust`        | http + postgres as `trust_rw`, `ledger.commerce_events` | the role D5 grants the ledger, and the table it writes |
| `ingest`       | http + neo4j session                                    | neo4j is the whole of its datastore surface |

Two rules kept this from becoming decoration:

* **Probe only what the service uses.** The dependency set is measured per service
  (`git grep -l psycopg` / `worker_redis` / `neo4j` under each source tree), never copied
  between fragments. `merchant-svc` touches no datastore — `git grep -n "psycopg\|worker_redis\|import redis\|neo4j" -- apps/merchant/svc/src`
  returns nothing — so its unread `PROXYSHOP_PG_DSN_APP`, its unread `REDIS_URL` and its
  `depends_on: postgres` were **deleted** rather than probed. `trust` and `buyer-svc` lost
  their `depends_on: redis` and `REDIS_URL` for the same reason: neither reads the variable
  or builds a client, and the only redis name under either is `ledger/errors.py`'s
  `import redis.exceptions`, which names exception *types* so a psycopg-backed store can
  classify a transient failure — its own docstring says *"no client, no connection pool, no
  `from_url`."* A `depends_on` with no probe behind it makes a container wait on a store it
  never speaks to; a probe with no usage behind it is a green light with nothing behind it.
  Both are the same lie. That pairing is now a test, and it caught these two in this lane's
  own diff rather than in a review.
* **Name the relation, not just the server.** A probe that stopped at "postgres answers"
  passes against an empty database, which is precisely the state the runbook produced.

The credential is resolved at container start by `service_launch serve`, through
`proxyshop_support.postgres.role_dsn` — the one function on the connect side that knows how,
and the one the T-112 tests pin against `db/init/00-roles.sql`'s `coalesce()`. The compose
fragments therefore carry **no password at all**, not even an interpolation of one.

## Observed, on this host

Brought up from a clean `cp .env.example .env` in an isolated compose project
(`COMPOSE_PROJECT_NAME=proxyshop-deploylane`, and the `.env` port block relocated to
`15432/16379/17474/17687/18081-18086` — which is what D40 makes every port overridable
*for*). Nothing else in the file was changed; every credential and provider value is
byte-identical to `.env.example`.

```
$ docker compose ps
SERVICE        STATUS
buyer-svc      Up 19 minutes (healthy)
exchange       Up 19 minutes (healthy)
ingest         Up 19 minutes (healthy)
merchant-svc   Up 21 seconds (healthy)
neo4j          Up 8 minutes (healthy)
postgres       Up 12 minutes (healthy)
redis          Up 6 minutes (healthy)
trust          Up 19 minutes (healthy)

$ curl :8084/events/head
{"head_hash":"0000...0000","length":0}                                             200
$ curl -X POST :8084/events -d '{"event_id":"1111...","kind":"auction_opened",...}'
{"inserted":true,"event":{...,"event_hash":"6119e48ad1bff92b..."},...}             201
$ curl -X POST :8081/buyer/auth/magic-link -d '{"email":"deploy-smoke@example.com"}'
{"expires_at":"2026-09-05T05:57:24.359702Z"}                                       202
$ curl -H 'authorization: Bearer dev-merchant-admin-token' :8082/install/shops
{"shops":[]}                                                                       200
$ curl :8082/install/shops                       # no token
{"error":"unauthorized"}                                                           401
$ curl -X POST :8083/auctions -d '{"intent":{...},"profile":{"buckets":{}}}'
{"auction_id":"auction-...","state":"closed","entries":[],"shortlist":{"slots":[]}} 201
$ curl :8085/er/config
{"default_threshold":0.86, ...}                                                    200
```

The trust POST is the one that goes all the way through. Read back with `psql`:

```
 seq |      kind      |  event_hash_16
-----+----------------+------------------
   1 | auction_opened | 6119e48ad1bff92b
```

The merchant `401` is the *correct* answer to an unauthenticated call. Before this lane it
was a `503 admin-api-not-configured`, because `MERCHANT_ADMIN_TOKEN` appeared in no compose
fragment and no `.env.example` — the admin surface was unreachable in every deployment.

## The controls — the probes were watched failing

A health check that has never been observed failing is indistinguishable from one that
cannot fail, and no test in this repo reaches a running container, so these five runs are the
only evidence that the signal works. Each names the dependency broken, what flipped, and —
just as important — what did **not**.

| control | broken | went `unhealthy` | stayed `healthy` | probe's own message |
|---|---|---|---|---|
| 1 | `docker compose stop postgres` | `trust`, `buyer-svc`, `exchange` (t+50s) | `ingest`, `merchant-svc` | `NOT READY: postgres as 'trust_rw' refused the connection: failed to resolve host 'postgres'` |
| 2 | `docker compose stop neo4j` | `ingest` only (t+40s) | all four others | `NOT READY: neo4j at bolt://neo4j:7687 did not accept a session: Cannot resolve address neo4j:7687` |
| 3 | `docker compose stop redis` | `exchange` only (t+60s) | all four others | `NOT READY: redis at redis://redis:6379 did not answer PING: Error -2 connecting to redis:6379` |
| 4 | `MERCHANT_ADMIN_TOKEN=` | `merchant-svc` only (t+50s) | all four others | `NOT READY: required configuration is unset: MERCHANT_ADMIN_TOKEN` |
| 5 | database exists, **never migrated** | probe exits 1 with postgres **up** | — | `NOT READY: postgres as 'trust_rw': relation 'ledger.commerce_events' does not exist ... the migrations have not been applied` |

Controls 1–4 are *differential*: only the services that actually use the broken store went
red. That is the evidence that the probes are per-service and measured rather than a blanket
check pasted into five files.

Control 5 is the one that justifies naming a relation. Postgres was **up and answering**;
`proxyshop_w9` existed and held only `public`; the probe still refused. Without it, the
green-over-503 state that opened this page would still be reachable.

While `trust` was unhealthy under control 1, `GET /events/head` answered
`503 store_unavailable`. The health signal and the route agree in both directions now, which
is the whole property. Restarting postgres returned every affected service to `healthy`
within 10s, unassisted.

## Scaling the exchange: `--workers 1` is a correctness pin, not tuning

Read this before you raise the uvicorn worker count, add `deploy.replicas`, or reach for
`docker compose up --scale exchange=N`. Nothing in the shipped configuration is broken — the
pin holds and the money path is safe today. The section exists because the pin is the *only*
thing holding it, and because the comment that used to guard it read like a caching note.

Two files carry the pin, and a test holds them to each other:

| file                         | where      | tokens                                                              |
|------------------------------|------------|---------------------------------------------------------------------|
| `apps/exchange/Dockerfile`   | `CMD`      | `uvicorn exchange.main:app --host 0.0.0.0 --port 8083 --workers 1`  |
| `apps/exchange/compose.yaml` | `command:` | the same tokens, after `python -m proxyshop_support.service_launch serve --` |

Compose clears the image's `CMD` whenever a fragment sets `command:`, so that uvicorn line is
one command written twice, and
`test_deploy_readiness.py::test_the_compose_command_mirrors_the_image_cmd` pins the copies
together. What that test does not know is *why* the number is `1`.

**What a second worker costs.** The T-158 guard — one accept per auction, one discount code
per purchase — is a claim held in a store, so it is exactly as wide as that store.
`apps/exchange/src/accept/routes.py::_claims` derives the acceptance-claim table from the
auction machine's own store, and **the document exposes no key for `auction_machine` and none
for `acceptance_claims`**. `configure_exchange` in `apps/exchange/src/composition.py` reads
`sellers`, `trust_url`, `trust_snapshot`, `intent_clusters`, `catalog`, `registered_domains`
and `checkout_mode` — and none of those names a store. It does now bind a machine
unconditionally, with no key required, but only to fix *where the audit trail goes*: the
branch at `composition.py:1909` hands `AuctionStateMachine` a ledger sink built from
`trust_url` and leaves the store exactly as it was, the same `InMemoryAuctionStore` the lazy
default builds. So the claim table is still process-local, and this paragraph's conclusion is
unchanged: two workers are two processes, two stores, two claim tables — both accepts win
their own copy of the guard, both mint, and one purchase leaves **two live discount codes** in
the merchant's account.

That last part is a *reported* measurement and not this lane's: an independent verifier
reproduced the double mint 6-of-6 across a real process boundary and reported it dead only
once the single-worker pin was in place. It was **not** re-run here, and nothing in this
repository reaches a second uvicorn worker — the same blind spot the last bullet of "Still not
done" records for running containers generally.

**Replicas are the same hazard by another route.** `deploy.replicas`, a second `exchange`
service, or `docker compose up --scale exchange=N` gives you the same two processes holding
the same two claim tables; the worker flag inside one container is only the cheapest way to
get there. What partly hides that today is the fragment's fixed host-port publish
(`"${EXCHANGE_PORT:-8083}:8083"`): a naive second replica collides on the host bind rather
than quietly serving. That is an accident of the port line, not a guard, and it goes away the
moment the publish is dropped, ranged, or moved behind a proxy — read off the fragment, and
the collision itself was not run here. What *was* run is that compose plans both containers
without complaint:

```
$ PROXYSHOP_WORKER=8 COMPOSE_PROJECT_NAME=proxyshop-scaleprobe \
    docker compose up --dry-run --no-build --scale exchange=2 exchange
 Container proxyshop-scaleprobe-exchange-2 Creating
 Container proxyshop-scaleprobe-exchange-1 Creating
 Container proxyshop-scaleprobe-exchange-2 Created
 Container proxyshop-scaleprobe-exchange-1 Created
end of 'compose up' output, interactive run is not supported in dry-run mode
```

**What would have to be true before either could be raised.** A shared, durable
acceptance-claim store, bound **in code** — `StoreAcceptanceClaims` over `RedisAuctionStore`
(redis is already this service's declared dependency and already probed by its healthcheck),
or any table with a unique constraint on `(auction_id)` handed to
`configure_accept(claims=...)`. No configuration can supply it: the deployment document has no
key for either collaborator, so there is no variable, no `.env` line and no
`EXCHANGE_DEPLOYMENT` document that turns it on. Nor is one wired today —
`git grep -n "RedisAuctionStore(" -- "*.py"` returns four call sites and all four are under
`apps/exchange/tests/`. Until that wiring exists, `--workers 1` is the guard, and raising it
is a code change with a review, not a capacity decision.

## Two things that bit, recorded so they do not bite twice

1. **`packages/contracts/src/generated` is a symlink** to `../generated/python`. Copying
   `src/` alone puts a dangling link in the image and every service dies at import on
   `ModuleNotFoundError: No module named 'contracts.generated'`. The Dockerfiles copy the
   symlink's target as well.
2. **`email-validator` is invisible to an import scan.** `buyer_svc.auth.routes` types a
   field `EmailStr`; pydantic imports the validator lazily while building the schema, so
   buyer-svc crash-looped on a dependency no `import` statement mentions. It is pinned in
   the shared pip layer now.

## Closed since this page was written

* **The exchange's neo4j dependency is probed.** `apps/exchange/Dockerfile` installs the
  `neo4j` driver in a layer of its own, `apps/exchange/compose.yaml` declares
  `depends_on: neo4j` and carries `--neo4j` in its readiness line, and it hands the container
  `NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD` — without which the driver defaults to the
  container's own loopback. The probe is not decoration: `EXCHANGE_SHOP_ROSTER` now defaults
  to `graph`, so an exchange that cannot open a bolt session cannot answer a roster-less
  auction.
* **The SPA has a server.** `buyer-web` is in `apps/buyer/compose.yaml` under the `demo`
  profile: nginx serving the vite bundle and reverse-proxying `/buyer/` to `buyer-svc`, so
  the page and the API are one origin (every endpoint in the SPA is a bare relative path and
  the buyer service installs no CORS middleware) and deep links resolve through a `try_files`
  fallback that a static mount on the API could not have.
* **Compose can express N store agents.** Four named services, four mounted context files —
  not one service scaled, because replicas share a service's environment and volumes and
  would all advocate for the same store.
* **Deployment documents ship.** `deploy/demo/`, generated from the recorded real catalogues
  by `scripts/build_demo_deployment.py`, and mounted read-only into the exchange and the
  buyer. `EXCHANGE_DEPLOYMENT` and `BUYER_DEPLOYMENT` default to them.

## Still not done

* **`packages/store-agent/compose.yaml` still probes `/openapi.json`**, and is deliberately
  untouched: that path belongs to a live lane building the store agent's HTTP surface. Its
  fragment declares no datastore dependency and its own comment is honest about what the
  probe proves, so it is not currently lying — but the moment that service reaches a
  datastore, its probe needs the same treatment. Same for `services/shopify-stub`, which
  already probes its own real `/healthz`.
* **`apps/merchant/app/` is not containerised.** When it grows an entrypoint it becomes a
  second service in the same fragment (`merchant-web`), the way `buyer-web` now is in
  `apps/buyer/compose.yaml`.
* **`apps/merchant/app/`'s bullet above is itself half stale**: `apps/merchant/package.json`
  does now declare a `build:ui`. What is still missing there is the compose service.
* **Images have never been pushed anywhere**; `docker compose` builds them locally by name.
* **No test reaches a running container.** The readiness logic is covered by
  `proxyshop_support/tests/test_deploy_readiness.py` — static checks over the compose
  fragments plus unit tests over `service_launch` — but the end-to-end evidence above is a
  measurement, not a gate. It will not re-run itself.
