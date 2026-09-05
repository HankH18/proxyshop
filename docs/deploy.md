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

`docker compose config --services` names nine services in one project (`name: proxyshop`,
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

# 2. The schema. `make deps-up` creates an EMPTY database; this is the step that was
#    missing from every revision of this runbook.
./.venv/bin/python scripts/db_migrate.py

# 3. The services.
docker compose up -d --build buyer-svc merchant-svc exchange trust ingest

docker compose ps          # every row (healthy) — and it now means something
```

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

## Two things that bit, recorded so they do not bite twice

1. **`packages/contracts/src/generated` is a symlink** to `../generated/python`. Copying
   `src/` alone puts a dangling link in the image and every service dies at import on
   `ModuleNotFoundError: No module named 'contracts.generated'`. The Dockerfiles copy the
   symlink's target as well.
2. **`email-validator` is invisible to an import scan.** `buyer_svc.auth.routes` types a
   field `EmailStr`; pydantic imports the validator lazily while building the schema, so
   buyer-svc crash-looped on a dependency no `import` statement mentions. It is pinned in
   the shared pip layer now.

## Still not done

* **`packages/store-agent/compose.yaml` still probes `/openapi.json`**, and is deliberately
  untouched: that path belongs to a live lane building the store agent's HTTP surface. Its
  fragment declares no datastore dependency and its own comment is honest about what the
  probe proves, so it is not currently lying — but the moment that service reaches a
  datastore, its probe needs the same treatment. Same for `services/shopify-stub`, which
  already probes its own real `/healthz`.
* **The exchange's neo4j dependency is unprobed.** `exchange/retrieval/sources.py` names a
  neo4j source, but `apps/exchange/Dockerfile` ships `ingest.graph` **without** the `neo4j`
  driver (lazily imported at `graph/reembed.py:386`, installed only in the ingest image), so
  a `--neo4j` check there would fail on the import rather than on the datastore. Fixing it
  means adding the driver to the exchange image — an image change owed to the exchange lane,
  not a compose change.
* **`apps/buyer/app/` and `apps/merchant/app/`** are TypeScript scaffolds with no build
  script and no entrypoint, so neither web app is containerised. When one grows a build it
  becomes a second service in the same fragment (`buyer-web` / `merchant-web`).
* **Images have never been pushed anywhere**; `docker compose` builds them locally by name.
* **No test reaches a running container.** The readiness logic is covered by
  `proxyshop_support/tests/test_deploy_readiness.py` — static checks over the compose
  fragments plus unit tests over `service_launch` — but the end-to-end evidence above is a
  measurement, not a gate. It will not re-run itself.
