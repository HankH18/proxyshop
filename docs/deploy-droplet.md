# Deploying the hosted demo

`docs/deploy.md` is about bringing the stack up **on your own machine**. This page is about the
one box the demo is hosted on, and it exists because that path had no written form at all: every
step below was reconstructed from a session transcript the second time somebody needed it, and
four of them fail in ways that look like something else.

Everything here was measured while deploying `e9d7c8c` through `d71205e` — the commits are the
anchor, so a reader can diff against them rather than trust a date.

---

## The box

| | |
|---|---|
| host | `162.243.162.24`, DigitalOcean droplet `proxyshop`, nyc1, 4 vCPU / 8 GB / 160 GB |
| public name | `proxyshop.hankholcomb.com` (A record → the IP above) |
| checkout | `/srv/proxyshop` |
| bare repo | `/srv/proxyshop.git`, which the local repo has as the `droplet` git remote |
| secrets | `/srv/proxyshop/.env` — never in the repo |

**SSH needs an explicit key and is not in `~/.ssh/config`:**

```sh
ssh -i ~/.ssh/proxyshop_deploy root@162.243.162.24
```

A bare `ssh root@162.243.162.24` fails `Permission denied (publickey)`, which reads exactly like
lost access and is not.

---

## The deploy, end to end

```sh
# 1. Ship the code. `droplet` is a bare repo; the checkout pulls from it.
#    The key is NOT in ~/.ssh/config (see "The box"), so git has to be told which one —
#    a bare `git push droplet` fails "make sure you have the correct access rights",
#    which reads as a permissions or missing-repo problem and is neither.
GIT_SSH_COMMAND='ssh -i ~/.ssh/proxyshop_deploy' git push droplet main:main
#    Or `ssh-add ~/.ssh/proxyshop_deploy` once, after which a bare push works.
ssh -i ~/.ssh/proxyshop_deploy root@162.243.162.24 'cd /srv/proxyshop && git pull --ff-only'

# 2. Rebuild. --build and --force-recreate are BOTH required; see trap 1.
#    Name the services explicitly; see trap 2.
ssh -i ~/.ssh/proxyshop_deploy root@162.243.162.24 'cd /srv/proxyshop && \
  docker compose --profile demo up -d --build --force-recreate --wait \
    buyer-web buyer-svc merchant-svc exchange trust ingest \
    store-agent-gaiaherbs store-agent-toniiq store-agent-paradiseherbs \
    store-agent-oregonswildharvest'

# 3. Prove the images are new. "healthy" does not mean "current"; see trap 1.
ssh -i ~/.ssh/proxyshop_deploy root@162.243.162.24 \
  'for c in exchange buyer-svc buyer-web ingest trust merchant-svc store-agent-gaiaherbs; do
     printf "%-28s %s\n" "$c" \
       "$(docker image inspect $(docker inspect proxyshop-$c-1 --format "{{.Image}}") \
          --format "{{.Created}}")"
   done'
```

Every timestamp must be **after** the commit you just shipped. If one is older, that service is
running the previous build and reporting `(healthy)` while it does so.

**A BLANK is not a pass.** A container can run an image that has since been untagged and
garbage-collected, and `docker image inspect` then prints nothing at all — measured on this box,
where `buyer-svc` ran a vanished image for ninety minutes while the loop above printed an empty
timestamp beside its name. An empty field there means *unknown*, which is strictly worse than
old, because nothing on the box can now tell you what code is serving. Treat it as a failure and
recreate that service.

### Reloading the corpus

Only when the corpus itself changed. **Wipe first** — `load_corpus` never deletes, and
`_apply_upserts` has no removal branch, so a new corpus lands *on top of* the old one and the
products that no longer exist stay `status="active"` forever.

```sh
ssh -i ~/.ssh/proxyshop_deploy root@162.243.162.24 'bash -s' <<'SH'
cd /srv/proxyshop
P=$(grep -m1 ^NEO4J_PASSWORD .env | cut -d= -f2-)
docker exec proxyshop-neo4j-1 cypher-shell -u neo4j -p "$P" "MATCH (n) DETACH DELETE n;"
docker compose --profile corpus run --build --rm corpus-loader
SH
```

Takes roughly twenty minutes on this box. Check the report rather than the exit code — the load
prints `TOTAL products= 4903/4903` and `products missing embedding 0`. A store that silently
loaded zero used to exit 0; that is fixed (`9ac007d`), but read the number anyway.

Expected result: **98,001 nodes** — 19 stores, 4,903 products, 28,134 variants, 4,090
`AttributeValue` nodes.

### Seeding trust — not optional

`deploy/demo/exchange-deployment.json` states no `trust_snapshot`, so the exchange reads
`GET /snapshot` and R12 excludes every store the snapshot has no row for. Without this step the
shortlist renders **zero slots**, and it looks like a retrieval failure.

```sh
ssh -i ~/.ssh/proxyshop_deploy root@162.243.162.24 'bash -s' <<'SH'
cd /srv/proxyshop
docker run --rm --network host \
  -v /srv/proxyshop:/repo -w /repo --env-file /srv/proxyshop/.env \
  --entrypoint python "$(docker inspect proxyshop-trust-1 --format '{{.Config.Image}}')" \
  scripts/seed_demo_trust.py
SH
```

`--network host` is the whole trick; see trap 3. Success prints a table of all 19 sellers and
`OK: all 19 demo sellers are in the live trust snapshot`.

### The public edge

TLS and the friendly hostname come from a Caddy container, profile-gated as `public` so it never
starts on a laptop.

```sh
# The domain is a deployment fact and lives in the box's .env, not the repo.
ssh -i ~/.ssh/proxyshop_deploy root@162.243.162.24 \
  'cd /srv/proxyshop && grep -q ^PROXYSHOP_PUBLIC_DOMAIN= .env || \
     echo PROXYSHOP_PUBLIC_DOMAIN=proxyshop.hankholcomb.com >> .env'

ssh -i ~/.ssh/proxyshop_deploy root@162.243.162.24 \
  'cd /srv/proxyshop && docker compose --profile public up -d caddy'
```

**DNS must resolve before this runs.** Caddy asks Let's Encrypt for a certificate on boot, and
the challenge is answered on port 80 of the box the name points at. Pointing the name afterwards
means waiting for a retry.

Adding a second hostname is one line in `deploy/caddy/Caddyfile` plus the DNS record. Let's
Encrypt allows **5 issuances per name in a rolling 7-day window** — the `caddy_data` volume
holds the certificates
and must survive `docker compose down`, or the fifth redeploy locks the name out with an error
that reads as a Caddy fault rather than a quota one.

---

## Verifying — drive it, do not trust a status

Every container in this stack can report `(healthy)` while the demo is dead, and each of these
has happened at least once.

```sh
# The after-deploy probe. Opens a real roster-less auction over HTTP.
ssh -i ~/.ssh/proxyshop_deploy root@162.243.162.24 \
  'cd /srv/proxyshop && DEMO_QUERY="a walnut coffee table for the lounge" bash scripts/demo_check.sh'
```

Expect `entries=2  ranked=2  shortlist slots=2` and `fallback reasons=['tier_0_no_agent']`.

Then check the two things a shortlist count does not cover:

```sh
# 1. The cart link names a REAL variant, not `1`.
#    Take the auction id from demo_check.sh's output, then:
curl -s localhost:8083/auctions/<id>/shortlist | python3 -m json.tool | grep variant_ref
# Expect a 14-digit storefront id. A `null` here means the served slot carries no variant and
# the accept will mint `…/cart/1:1`.

# 2. The published bundle is the one you just built.
curl -s https://proxyshop.hankholcomb.com/ | grep -o '/assets/index-[^"]*\.js'
curl -s https://proxyshop.hankholcomb.com/assets/index-<hash>.js | grep -c '<a string you just changed>'
```

A 200 from the SPA proves nginx is serving *something*, not that it is serving your build.

**Surfaces:**

| | |
|---|---|
| shopper journey | `https://proxyshop.hankholcomb.com/` |
| watch it learn | `https://proxyshop.hankholcomb.com/#/learning` |
| metrics & tracing | `https://proxyshop.hankholcomb.com/#/metrics` |
| merchant console | `https://proxyshop.hankholcomb.com/dashboard/` |

Buyer routes are **hash** routes. The merchant console needs its trailing `/dashboard/` — a bare
`/dashboard` or the service's own `/` is a 404 by design. Console auth is a bearer token,
`MERCHANT_ADMIN_TOKEN`, in the box's `.env`.

---

## The four traps

Each of these cost a real deploy, and each fails as something else.

**1. `docker compose up -d` keeps a stale image.** Services whose image changed are *not*
recreated without `--force-recreate`; `ingest`, `merchant-svc` and `trust` kept pre-deploy image
IDs through a deploy that reported success. Measured again in the other direction:
the local exchange served `variant_ref: null` from an image built fourteen hours before the fix,
while every container reported `(healthy)` — that reading was taken on a laptop running fifteen;
the droplet runs fourteen. **Check image build timestamps, not health.**

**2. A bare `docker compose build` misses five of ten images.** The four per-shop store agents and
`buyer-web` are behind the `demo` profile. Name the services explicitly, as step 2 does.

**3. `localhost` inside a container is the container.** `seed_demo_trust.py` defaults to
`http://localhost:8084` for trust and resolves its Postgres DSN to `localhost:5432`. Run through
`docker compose run` it fails twice in a row — first `Connection refused` on 8084, then on 5432
— and both read as "the service is down" when both services are up and healthy. `docker run
--network host` fixes both at once. Passing `--trust-url http://trust:8084` fixes only the first,
which is the misleading half-fix.

**4. An empty placeholder in a Caddyfile is a parse error, not a default.** `email
{$CADDY_ACME_EMAIL}` with the variable unset expands to a bare `email` with no argument. Caddy
exits before ever contacting Let's Encrypt and restart-loops — eight times in twenty seconds —
and the symptom looks like an ACME or DNS failure. There is no global `email` in the Caddyfile
now, deliberately; certificates issue without one. If you add an address later, put a real one in
rather than a placeholder that may be empty.

A fifth, in compose rather than on the box: **`${VAR:?message}` is interpolated while parsing,
before profiles are applied.** Used on a profile-gated service it breaks `docker compose config`
for everyone who never asked for that service. `deploy/caddy/compose.yaml` uses plain
interpolation for exactly this reason.

---

## Security posture

Worth knowing before pointing anyone else at this box.

Every container publishes on `0.0.0.0`, `ufw` does not filter Docker's published ports, and
`DOCKER-USER` is empty. **The only control closing Postgres, Redis, Neo4j, the buyer service, the
exchange, trust, ingest and the four store agents is the DigitalOcean cloud firewall**
(`proxyshop-demo`, inbound 22/80/443/8080/8082). Detach it and they are world-reachable.

Adding Caddy did not close `:8080` or `:8082` — both remain published and reachable by IP
alongside the domain.

Two known-open items, neither fixed by this page.

**The Postgres superuser is still the development password.** Neo4j is NOT — it was rotated and
carries a 28-character password that matches no dev default, so this is one credential rather
than the general rot the plural would imply.

**Sign-in is an authentication bypass, and the UI hides that rather than preventing it.**
`GET /buyer/auth/sign-in` answers `{"offered": false}` on this box, so the SPA renders no sign-in
form and the door looks closed. It is not: `POST /buyer/auth/magic-link` still answers anyone who
calls it directly — the routes stay live under every transport — and console delivery is a
literal `print(...)` to stdout. So `docker logs proxyshop-buyer-svc-1` lets anyone who can read
it sign in as any address, and checking the UI for a sign-in form is exactly the wrong test.

Both are firewall-dependent rather than safe. The firewall itself is confirmed: `doctl compute
firewall list` shows `proxyshop-demo` on this droplet allowing inbound tcp 22/80/443/8080/8082
from `0.0.0.0/0`. One latent caveat — those rules are **IPv4-only** while every container also
publishes on `[::]`. The droplet has no global IPv6 today, so it is a future footgun rather than
a live hole: enabling IPv6 would expose Postgres, Redis, Neo4j, the exchange, trust and ingest
immediately.
