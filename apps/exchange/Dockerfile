# The proxyshop-exchange container.
#
# The exchange API — the auction and ranking service. C1 makes it one of four
# SEPARATE deployables, and C3/S7 are the reason it is separate: this image contains no
# buyer_svc and no envelope-reading code, so the process cannot read a sealed envelope
# even if someone later writes an import that says it should.
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
# ESC-020. `exchange.ranking.verification` imports `claim_verification` at module scope: the
# exchange now runs T-065's claim verifier itself on the auction path, because a hard
# constraint may only be satisfied by a verdict THIS service produced, never by a `status`
# the bidder wrote. Without these two lines `exchange.ranking.serving` — and therefore
# `POST /auctions` — dies on `ModuleNotFoundError: No module named 'claim_verification'`.
# The package copies its own directory shape for the same reason `contracts` does above:
# `packages/verification/__init__.py` resolves `.pkgroot` relative to its own path. ZERO new
# pip requirements — `verifier.py`, `comparators.py`, `normalize.py` and `statuses.py` import
# nothing but hashlib, json, math, re, datetime, types and typing.
COPY packages/verification/__init__.py /app/packages/verification/__init__.py
COPY packages/verification/src/        /app/packages/verification/src/
# T-244. `exchange.external_bids.routes` imports `store_agent.external.door` at module scope,
# and `exchange.main.create_app` imports every `<feature>/routes.py` it globs — so without
# these two lines `app = create_app()` raises `ModuleNotFoundError: No module named
# 'store_agent'` at IMPORT time and the container never finishes booting. That is a strictly
# worse failure than the one the route repairs, which is why the image is changed in the same
# commit as the route rather than left for the deployment to discover.
#
# ONLY `external/` is copied, never `packages/store-agent/src/` wholesale, and the narrowness
# is the point twice over: the door's entire import closure is `contracts` (already above) plus
# `external/{door,nonces,signatures}.py`, and `.importlinter`'s c3 contract FORBIDS
# `exchange -> store_agent.modes` — a module that is not in the image cannot be imported by
# accident. `packages/store-agent/src/__init__.py` is empty and is copied only so the flat
# layout's package root exists.
COPY packages/store-agent/src/__init__.py /app/packages/store-agent/src/
COPY packages/store-agent/src/external/   /app/packages/store-agent/src/external/
COPY apps/exchange/src/          /app/apps/exchange/src/
# T-299. `ingest.graph` and `ingest.embeddings` are a SHARED LIBRARY that happens to live at
# a service's path, and candidate retrieval is the exchange's own responsibility (DESIGN.md:34),
# not a call across a service boundary: DESIGN.md:23-24's architecture graph has ING->NEO and
# EXC->NEO as two independent edges with no exchange<->ingest edge, DESIGN.md:38 gives ingest
# writes only, ingest.openapi.json is a single write route, and `.importlinter` — the file whose
# entire job is forbidding layering violations — forbids exchange->envelopes and says nothing
# about exchange->ingest. So the repair is to ship the library, not to delete the imports.
#
# WITHOUT these lines eleven of this image's own shipped modules raise
# `ModuleNotFoundError: No module named 'ingest'` — `exchange.ranking` and all four of its
# modules, and every module of `exchange.retrieval` — while `exchange.main` imports perfectly.
# It imports perfectly because `main.py:27-30` mounts routers by globbing `*/routes.py`, and
# neither `retrieval/` nor `ranking/` has one, so a smoke test, a /healthz and any entrypoint
# probe pass this image forever. Among the dead modules is `retrieval/fit.py`, which holds the
# repo's ONLY emitter of the `bid_placed` ledger event (FIT_LEDGER_KIND at :61, emitted at
# :311-316) — a kind whose count is load-bearing in two frozen metrics.
#
# Only `graph/` and `embeddings/` are shipped, because that is the measured closure of what
# exchange actually imports (`retrieval/criteria.py:45-46`, `service.py:33-34`,
# `sources.py:29-31` name `ingest.graph`, `ingest.graph.model`, `ingest.graph.query` and
# `ingest.embeddings` and nothing else). `adapters/`, `er/`, `extraction/`, `scheduler/` and
# `main.py` — the crawler and the ingest service itself — stay out.
#
# ZERO new pip requirements, measured rather than assumed, and that is why this is a COPY and
# not a dependency change: every module in the shipped subset is stdlib-only at module scope.
# The two heavy imports in it are both deferred — `graph/reembed.py:386` imports the neo4j
# driver INSIDE `graph_driver()`, and `embeddings/local_bge.py:93` imports
# sentence_transformers (hence torch) INSIDE `_load()`. The prologue's "numpy, lxml and
# anthropic are in no deployable's closure and are deliberately absent" stays true.
# `src/__init__.py` is empty and is copied anyway: without it `ingest` is not a package.
COPY services/ingest/src/__init__.py /app/services/ingest/src/
COPY services/ingest/src/graph/      /app/services/ingest/src/graph/
COPY services/ingest/src/embeddings/ /app/services/ingest/src/embeddings/

# `.pkgroot/<pkg> -> <member>/src` is what makes the FLAT layout importable (R1b/D42).
# The repo tracks these as git symlinks; the image RECREATES them instead of copying them
# so the build never depends on how a daemon happens to treat symlinks in a context tar.
RUN mkdir -p /app/.pkgroot \
 && ln -s ../packages/contracts/src /app/.pkgroot/contracts \
 && ln -s ../packages/verification/src /app/.pkgroot/claim_verification \
 && ln -s ../apps/exchange/src /app/.pkgroot/exchange \
 && ln -s ../packages/store-agent/src /app/.pkgroot/store_agent \
 && ln -s ../services/ingest/src /app/.pkgroot/ingest

# Non-root: nothing in these services writes to the filesystem, so there is no reason for
# the process to be able to.
RUN useradd --create-home --uid 10001 proxyshop
USER proxyshop

EXPOSE 8083

# `--workers 1` is a MONEY-PATH SAFETY constraint. It is not tuning, not a cache-warmth
# concern and not a memory budget: raise it and this service mints TWO live discount codes for
# ONE purchase. That is T-158, and this pin is currently the only thing holding it shut.
#
# The T-158 guard is a CLAIM IN A STORE, so it is exactly as wide as that store.
# `accept/routes.py::_claims` derives the acceptance-claim table from the auction machine's own
# store, and an exchange configured only by the deployment document never binds a machine:
# `configure_exchange` (src/composition.py) binds eligibility, the bid solicitor, the trust
# snapshot, both registered-domain sets and the checkout mode — but NO auction machine, and
# the document has no key that could supply one. The machine therefore falls back to
# `AuctionStateMachine()`
# over `InMemoryAuctionStore` and the claim table is process-local. Two workers are two
# processes, two stores and two claim tables: both accepts win their own copy of the guard and
# both mint. An independent verifier reproduced that 6-of-6 across a real process boundary and
# reported it dead only once this pin was in place.
#
# WHAT WOULD HAVE TO BE TRUE BEFORE IT COULD BE RAISED — and the same precondition governs a
# second replica: a SHARED, DURABLE acceptance-claim store bound IN CODE.
# `StoreAcceptanceClaims` over `RedisAuctionStore` (nothing on the served path constructs one
# today), or any table with a unique constraint on `(auction_id)` handed to
# `configure_accept(claims=...)`. NO configuration surface can supply it: the deployment
# document (EXCHANGE_DEPLOYMENT / EXCHANGE_DEPLOYMENT_JSON) exposes no key for
# `auction_machine` and none for `acceptance_claims`, so no variable, file or override turns
# it on.
#
# The lesser half, and all this comment used to say: the frozen entrypoint builds the app at
# import time and a feature router may hold in-process state (the auction registry, the install
# nonce table), so a second worker would answer half the requests from a different copy of it.
# True, and a correctness wart. The claim ledger above is money.
CMD ["uvicorn", "exchange.main:app", "--host", "0.0.0.0", "--port", "8083", "--workers", "1"]
