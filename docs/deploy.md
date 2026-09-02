# Running the ProxyShop stack under docker compose

This is the deploy lane's record of how the four deployables actually start, what was
observed when they did, and what still does not work. Everything below was RUN, not
reasoned about; where something was not run, it says so.

## What is deployable

`docker compose config --services` names seven services in one project (`name: proxyshop`,
pinned in the root file so a worktree cannot spawn its own stack):

| service        | port (env override)      | image built from        | entrypoint             |
|----------------|--------------------------|-------------------------|------------------------|
| `postgres`     | 5432 (`PG_PORT`)         | `postgres:16-alpine`    | —                      |
| `neo4j`        | 7474/7687 (`NEO4J_*`)    | `neo4j:5.26-community`  | —                      |
| `redis`        | 6379 (`REDIS_PORT`)      | `redis:7-alpine`        | —                      |
| `buyer-svc`    | 8081 (`BUYER_SVC_PORT`)  | `apps/buyer/Dockerfile` | `buyer_svc.main:app`   |
| `merchant-svc` | 8082 (`MERCHANT_SVC_PORT`)| `apps/merchant/Dockerfile` | `merchant_svc.main:app` |
| `exchange`     | 8083 (`EXCHANGE_PORT`)   | `apps/exchange/Dockerfile` | `exchange.main:app` |
| `trust`        | 8084 (`TRUST_PORT`)      | `apps/trust/Dockerfile` | `trust.main:app`       |

`shopify-stub` is an eighth, on the `e2e` profile, so it starts only with
`--profile e2e`. `services/sim`, `services/ingest`, `packages/store-agent` and
`apps/seller-reference` are still the T-000 `services: {}` stubs — see "Not done" below.

## Bringing it up

```bash
# 1. the datastores, healthchecked, plus this worker's database
make deps-up

# 2. the four services. PROXYSHOP_ROLE_PASSWORD is REQUIRED here (see "Credentials").
PROXYSHOP_ROLE_PASSWORD=<the cluster's role password> \
  docker compose up -d --build buyer-svc merchant-svc exchange trust

docker compose ps          # every row should read (healthy)
```

Take it down with `docker compose down` — note that `make deps-down` is
`docker compose down -v`, which destroys the pgdata and neo4jdata volumes.

## Credentials

T-112 makes `PROXYSHOP_ROLE_PASSWORD` the ONE source of truth for the four
least-privilege role passwords. The service fragments therefore interpolate it *into* the
DSN and give it an EMPTY default rather than the dev literal — a value in a compose file
would be a second copy of the credential, and a second copy is one that stops agreeing
with the cluster. The consequence is worth stating plainly: **with the variable unset the
DB-backed services start, pass their healthcheck, and then answer 500 on any route that
touches postgres**, because a password-less DSN reaches the server and fails auth, and
psycopg's pool turns that into a 30-second `PoolTimeout`. That is what the first run of
this stack did.

## Healthchecks probe `/openapi.json`, not `/healthz`

None of the four services has a health route. `apps/*/src/main.py` is orchestrator-frozen
(B6(iii)) and no feature router under it declares one, so the deploy lane cannot add one
without editing another lane's source. `/openapi.json` is served by FastAPI itself, and a
200 from it proves the ASGI app imported, every discovered router mounted, and uvicorn is
answering on the port. It does **not** prove the datastores are reachable. A real
readiness route belongs to the service lanes.

## Observed, on this host

All four built and ran; all four reported `(healthy)`; each answered a real route.

```
$ docker compose ps
SERVICE        STATUS                        PORTS
buyer-svc      Up 10 seconds (healthy)       0.0.0.0:8081->8081/tcp
exchange       Up 10 seconds (healthy)       0.0.0.0:8083->8083/tcp
merchant-svc   Up 10 seconds (healthy)       0.0.0.0:8082->8082/tcp
neo4j          Up 27 hours (healthy)         0.0.0.0:7474->7474/tcp, 0.0.0.0:7687->7687/tcp
postgres       Up 27 hours (healthy)         0.0.0.0:5432->5432/tcp
redis          Up 27 hours (healthy)         0.0.0.0:6379->6379/tcp
trust          Up About a minute (healthy)   0.0.0.0:8084->8084/tcp

$ curl -X POST :8083/auctions -d '{"intent":{...},"profile":{"buckets":{}}}'
{"auction_id":"auction-f8e0788f-...","state":"closed","solicited":[],"entries":[],"denied":[]}   201

$ curl -X POST :8084/events -d '{"event_id":"...","ts":"...","kind":"auction_opened","payload":{...}}'
{"inserted":true,"event":{...,"seq":1},"head_hash":"197eaefb...","length":1}                     201
$ curl :8084/events/head
{"head_hash":"197eaefb...","length":1}                                                           200

$ curl -X POST :8081/buyer/auth/magic-link -d '{"email":"deploy-smoke@example.com"}'
{"expires_at":"..."}                                                                             202

$ curl -i ":8082/install?shop=proxyshop-demo.myshopify.com"      # with SHOPIFY_API_KEY/SECRET set
HTTP/1.1 302 Found
location: https://proxyshop-demo.myshopify.com/admin/oauth/authorize?client_id=...&scope=...
```

The trust POST above is the one that goes all the way through: the containerised service
opened a psycopg pool to the `postgres` service over the compose network as the `trust_rw`
role and hash-chained a row into `ledger.commerce_events`, which `psql` then read back.

Without `SHOPIFY_API_KEY`/`SHOPIFY_API_SECRET` the merchant install route answers
`503 {"error":"app-not-configured"}`. That is the service failing closed, not a deploy
defect — it is left unset in the fragment on purpose.

## Two things that bit, recorded so they do not bite twice

1. **`packages/contracts/src/generated` is a symlink** to `../generated/python`. Copying
   `src/` alone puts a dangling link in the image and every service dies at import on
   `ModuleNotFoundError: No module named 'contracts.generated'`. The Dockerfiles copy the
   symlink's target as well.
2. **`email-validator` is invisible to an import scan.** `buyer_svc.auth.routes` types a
   field `EmailStr`; pydantic imports the validator lazily while building the schema, so
   buyer-svc crash-looped on a dependency no `import` statement mentions. It is pinned in
   the shared pip layer now.

## Not done

* `services/sim/compose.yaml`, `services/ingest/compose.yaml`,
  `packages/store-agent/compose.yaml` and `apps/seller-reference/compose.yaml` are still
  T-000 stubs. `services/ingest` and `packages/store-agent` do have a `main.py` with an
  `app`, so both are containerisable on the pattern above; ingest additionally needs
  numpy/scipy/lxml/bs4 and the `EMBEDDING_PROVIDER=hash` path, which is a bigger image and
  belongs to the ingest lane.
* `apps/buyer/app/` and `apps/merchant/app/` are TypeScript scaffolds with no build script
  and no entrypoint, so neither web app is containerised. When one grows a build it becomes
  a second service in the same fragment (`buyer-web` / `merchant-web`).
* The new ports are not in `.env.example` — that file belongs to another lane. Until it
  moves, `BUYER_SVC_PORT`, `MERCHANT_SVC_PORT`, `EXCHANGE_PORT` and `TRUST_PORT` are
  documented only here.
* Images have never been pushed anywhere; `docker compose` builds them locally by name.
