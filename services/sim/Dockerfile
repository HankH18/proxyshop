# The proxyshop-sim container. (T-313)
#
# The seeded market simulation. This one is NOT a server, and the Dockerfile says so rather
# than dressing it as one: `services/sim/src/__main__.py` is an argparse CLI whose exit
# status is the result — 0 the dishonest store was caught inside the episode budget, 1 it
# never was (an S2 failure), 2 the run could not start because the manifest digest chain did
# not verify. A container that "stays up" would destroy that signal, so the sibling
# compose.yaml runs it with `restart: "no"`, no published port and no healthcheck.
#
# It takes no services, opens no sockets and touches no database: the run is a pure function
# of the human-approved manifest and one seed.
#
# Build context is the REPO ROOT (see the sibling compose.yaml), not this directory: the
# flat src layout (D42) means a service image has to reach across member directories.

FROM python:3.12-slim

# The prologue of all EIGHT service Dockerfiles that carry it is BYTE-IDENTICAL (base image,
# ENV, WORKDIR, pip layer, contracts/support copy): buyer, merchant, exchange, trust,
# seller-reference, store-agent, ingest and sim. `services/shopify-stub/Dockerfile` is the
# ninth and installs a smaller set of its own. That is deliberate: the layer cache is keyed on
# the parent image plus the instruction, so building all eight costs ONE dependency download
# rather than eight. Change the pin list here and you change it in all eight -- this said
# "four" while there were four services, and the count went stale as the layer was adopted.
#
# The versions below MIRROR the root pyproject.toml's pins — they are not a second source of
# truth and they are not a manifest a worker may edit. If the root manifest moves, this file
# follows it; it must never lead. The set is the measured closure of what these service
# packages import: fastapi/uvicorn/pydantic for the app itself, httpx for the merchant's
# Shopify Admin client, psycopg for the ledger and the vault, redis for D39's worker client,
# jsonschema (which pulls referencing) for contracts.registry, email-validator because
# buyer_svc.auth.routes types a field `EmailStr` and pydantic imports the validator lazily
# at schema-build time — an extras dependency an import scan cannot see, and the reason
# buyer-svc crash-looped on its first deploy. numpy, lxml and anthropic are in no
# deployable's closure and are deliberately absent. So is python-multipart, which this layer
# used to install -- and it is the one removal here that has to be RE-MEASURED rather than
# re-reasoned, because it looks load-bearing and is not. `starlette/formparsers.py:18` imports
# it AT MODULE SCOPE, spelled `python_multipart` and not `multipart` (grep either name alone
# and you miss it), on the path every `import fastapi` takes. An image without it still boots
# because that import sits under `try/except ModuleNotFoundError`, which leaves
# `multipart = None` and fails only when a form is PARSED -- and neither fastapi nor starlette
# REQUIRES the wheel, both declaring it in an extra. No route in this repo declares a
# `Form`/`File`/`UploadFile` parameter or calls `request.form()`, measured by importing all
# six services with the wheel made absent: every app builds and every published OpenAPI
# carries no form body. Put it back in the same commit as the first form route, never before
# it. (scipy and mcp were named here too, until the same commit removed both from the root
# manifest outright: nothing imported either.)
#
# Kept VERBATIM for the layer-cache reason above. Unlike the two library images, sim really
# does need most of it: the trust tree it ships imports psycopg and redis at module scope
# (`apps/trust/src/ledger/errors.py:36-37`, to name the driver exceptions it maps) and
# fastapi/pydantic (`events/routes.py:39-40`), so those wheels are load-bearing here even
# though the simulation itself opens no socket.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app:/app/.pkgroot

WORKDIR /app

RUN pip install --no-cache-dir \
      "fastapi==0.141.1" \
      "uvicorn[standard]==0.52.4" \
      "pydantic==2.13.5" \
      "email-validator==2.3.0" \
      "httpx==0.28.1" \
      "psycopg[binary,pool]==3.3.5" \
      "redis[hiredis]==8.1.0" \
      "jsonschema[format]==4.26.0"

# The repo layout is reproduced VERBATIM rather than flattened into /app, because
# `contracts.registry` resolves its JSON bundle as
# `Path(__file__).resolve().parent.parent / "schemas"`. `.resolve()` follows the `.pkgroot`
# symlink through to the real path, so in the repo the bundle is found at
# packages/contracts/schemas. Flatten src/ to /app/contracts and that same lookup lands on
# /app/schemas, and the image dies on the first request that validates anything. Copying the
# shape is what keeps the invariant true inside the container.
COPY packages/contracts/__init__.py /app/packages/contracts/__init__.py
COPY packages/contracts/src/        /app/packages/contracts/src/
COPY packages/contracts/schemas/    /app/packages/contracts/schemas/
# `packages/contracts/src/generated` is a SYMLINK to `../generated/python`, so copying src/
# alone lands a dangling link in the image and `contracts.protocol` dies on
# `No module named 'contracts.generated'` at import time — observed, not theorised.
COPY packages/contracts/generated/python/ /app/packages/contracts/generated/python/
COPY proxyshop_support/             /app/proxyshop_support/
# The simulator, WHOLE — and a plain directory copy is the honest instruction here precisely
# because `services/sim/src/` now contains nothing this image cannot run. `python -m sim` is
# the CMD and its closure is the four modules in that directory.
#
# The R14 seeding harness used to live here too, and while it did, this COPY was a lie the
# gate caught: `local_stack.py` imports `buyer_svc` and `population.py` imports
# `exchange.ranking`, neither of which this image carries, so
# `test_t301_every_image_copy_set_covers_every_first_party_import_it_ships` was red on this
# very line. The repair is not to ship those packages — measured, `buyer_svc` cascades to
# `claim_verification` and `llm`, `exchange.ranking` cascades to `exchange.retrieval` and
# the `ingest` graph library, and the whole exchange narrowing below is reversed for code
# whose CMD never runs. The harness is a checkout script rather than a service, so it moved
# to `services/sim/seed/` (import name `seed`, `.pkgroot/seed`), which is outside this build
# context's COPY set by construction rather than by a list someone has to maintain. See
# `services/sim/seed/__init__.py`.
COPY services/sim/src/           /app/services/sim/src/
COPY apps/trust/src/             /app/apps/trust/src/

# The exchange, NARROWED. `services/sim/src/runner.py:512-522` reaches `exchange.accept`,
# `exchange.auction` (+ `.fanout`), `exchange.checkout.sellers`, `exchange.eligibility` and
# `exchange.orchestration` — five of the eight subpackages. The other three are left out on
# purpose, and `retrieval/` is the one that matters: its `criteria.py:45`, `service.py:33`
# and `sources.py:29-31` import `ingest.graph` / `ingest.embeddings` at module scope, so
# copying `apps/exchange/src/` wholesale would oblige this image to carry the ingest graph
# library as well — for code the simulation never executes. `ranking/` reaches the same
# imports through `filters.py:39`. `policy/`, `reports/` and `main.py` are simply unused.
#
# `auction/routes.py` is now EXCLUDED, exactly as apps/merchant/Dockerfile:101-106 excludes
# it, and `auction/` is therefore named file by file rather than as a directory. It USED to be
# included, on the argument that its module-scope `..eligibility` / `..orchestration` imports
# both resolve here anyway; T-310 ended that. Wiring the published ranking onto the served
# auction added `from ..ranking.serving import ...` at module scope, and `ranking/` reaches
# `retrieval/criteria.py` and through it `ingest.graph` — so keeping the directory would
# oblige this image to carry `ranking/`, `retrieval/` AND the ingest graph library, for an
# HTTP router `exchange.main` serves and this image never mounts. The simulation imports
# `exchange.auction` (the package) at `runner.py:513`, never `exchange.auction.routes`;
# measured, `git grep -n 'auction.routes\|configure_auctions' services/sim/src` is empty.
#
# This is the narrowing working rather than rotting, and the gate is what said so: the module
# that grew the new edge turned
# proxyshop_support/tests/test_artifact_copyset.py::test_t301_every_shipped_module_imports_inside_the_container_shaped_tree
# red HERE — `1 of 96 shipped modules cannot be imported ... No module named
# 'exchange.ranking'` — instead of this container failing in a deploy.
COPY apps/exchange/src/__init__.py    /app/apps/exchange/src/
COPY apps/exchange/src/accept/        /app/apps/exchange/src/accept/
COPY apps/exchange/src/auction/__init__.py \
     apps/exchange/src/auction/collect.py \
     apps/exchange/src/auction/fanout.py \
     apps/exchange/src/auction/ledger.py \
     apps/exchange/src/auction/state.py \
     /app/apps/exchange/src/auction/
COPY apps/exchange/src/checkout/      /app/apps/exchange/src/checkout/
COPY apps/exchange/src/eligibility/   /app/apps/exchange/src/eligibility/
COPY apps/exchange/src/orchestration/ /app/apps/exchange/src/orchestration/

# The approved fixture manifest and everything its digest chain covers. This is a hard
# layout requirement, not a convenience: `fixtures/__init__.py:37-40` sets
# `FIXTURES_DIR = Path(__file__).resolve().parent` and `REPO_ROOT = FIXTURES_DIR.parent`,
# and `fixtures/manifest/__init__.py:234-240`'s `manifest_root()` RAISES unless the manifest
# resolves at `<root>/fixtures/manifest.json`. With WORKDIR /app that pins the tree to
# /app/fixtures/, which is what the line below produces. `load_manifest()` re-verifies the
# sha256 of the manifest, the golden set and the seed catalog on every call, so a partial
# copy fails loudly rather than simulating against something else.
COPY fixtures/                   /app/fixtures/

# `.pkgroot/<pkg> -> <member>/src` is what makes the FLAT layout importable (R1b/D42).
# The repo tracks these as git symlinks; the image RECREATES them instead of copying them
# so the build never depends on how a daemon happens to treat symlinks in a context tar.
#
# `/app` is on PYTHONPATH as well as `.pkgroot`, and for this image that is load-bearing
# rather than incidental: `fixtures` is imported by its top-level name (it has no `src/` and
# no `.pkgroot` entry), and the canonical-alias machinery in `apps/trust/src/_shared` and
# `apps/exchange/src/accept` resolves the `apps.trust.src.*` / `apps.exchange.src.*`
# spellings through the PEP 420 namespace at /app. Both spellings must reach the same files.
RUN mkdir -p /app/.pkgroot \
 && ln -s ../packages/contracts/src /app/.pkgroot/contracts \
 && ln -s ../apps/exchange/src /app/.pkgroot/exchange \
 && ln -s ../apps/trust/src /app/.pkgroot/trust \
 && ln -s ../services/sim/src /app/.pkgroot/sim

# Non-root: nothing here writes to the filesystem, so there is no reason for the process to
# be able to.
RUN useradd --create-home --uid 10001 proxyshop
USER proxyshop

# No EXPOSE: this container serves nothing. See the header.
#
# `--json` so the run is machine-readable in a log; the seed and episode budget keep their
# in-code defaults so `docker compose run sim --seed N` can override them by appending args.
CMD ["python", "-m", "sim", "--json"]
