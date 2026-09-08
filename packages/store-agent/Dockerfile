# The proxyshop-store-agent container. (T-313)
#
# The store agent — the bidding runtime, the provenance hooks, the learning grid and the
# external door an unharnessed seller speaks through. `store_agent.main:app` is the frozen
# entrypoint.
#
# STATED PLAINLY, because a reader deserves it before they deploy this: the HTTP surface is
# THIN. `main.py` mounts routers by globbing `*/routes.py`, and there is exactly one —
# `packages/store-agent/src/solicitation/routes.py`. Measured by running the image built
# from this file: `1 feature router(s) mounted: store_agent.solicitation.routes`, and the
# container otherwise answers only FastAPI's own /openapi.json, /docs and /redoc. (This
# paragraph used to say "NO business routes ... there is no `routes.py` anywhere under
# packages/store-agent/src". That was true when it was written and is not true now; a
# comment about the shipped artifact that a deployer reads has to be re-measured, not
# inherited.) The store agent's surface is still mostly a LIBRARY consumed in-process
# (services/sim drives it; apps/exchange solicits through it), and the healthcheck is honest
# about probing only that the app is up.
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
# Kept VERBATIM here for the layer-cache reason above: an identical instruction is the SAME
# layer on disk as the other images', so the wheels this package does not import cost no
# extra download and no extra disk. store_agent's own measured need is fastapi + pydantic
# (the latter via contracts); jsonschema is reached lazily by contracts.registry.
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

# ---------------------------------------------------------------------------------
# This image's OWN layer, on top of the byte-identical prologue above.
# ---------------------------------------------------------------------------------
# A SEPARATE `RUN pip install`, not an edit to the block above, and that is the established
# shape here rather than a preference: `apps/exchange/Dockerfile` and `services/ingest/
# Dockerfile` both add their extra distributions (neo4j; neo4j/beautifulsoup4/lxml/protego)
# in a second layer for the same reason. The prologue's instruction string is the Docker
# cache key shared by all eight service images, so adding one requirement there would
# re-download the whole dependency set eight times. Adding it here costs one layer in one
# image. The prologue's sentence "numpy, lxml and anthropic are in no deployable's closure
# and are deliberately absent" remains true OF THE PROLOGUE, which is the block it describes.
#
# WHY THIS IMAGE NEEDS IT. D55 says an in-network shop buys the right to make its case in
# its own voice — a dedicated advocate, as against the platform-authored organic pitch a
# scraped shop gets. That voice is written by `runtime.pitch.compose_pitch`, whose client
# comes from `solicitation.copywriter.pitch_client()`, which selects `AnthropicLLM` on
# `LLM_PROVIDER=anthropic`. Without the line below that selection did not fail — it
# SUCCEEDED, because `llm.client.AnthropicLLM.__init__` performs no I/O and imports no SDK
# (D20). The ModuleNotFoundError arrived one frame deeper, on the first `complete()`, inside
# `import anthropic` at `packages/llm/src/client.py:168`, and `compose_pitch` catches
# everything and answers with the deterministic slot-filled fallback so a copywriter outage
# can never cost a store its bid. Net effect in every container-based deployment: the
# product's central claim ran on string interpolation, at 200 OK, with the container
# reporting `(healthy)` and a configuration that said `anthropic`. `.env.example:105`
# already documented the raise; nothing had closed it.
#
# The version MIRRORS the root pyproject.toml pin (`anthropic==1.2.0`) exactly as the
# prologue's set does, and for the same reason: this file follows the root manifest and must
# never lead it.
#
# It pulls anyio, docstring-parser, httpx2, jiter, sniffio and typing-extensions. Note
# httpx2, which is a DIFFERENT distribution from the prologue's `httpx==0.28.1` (anthropic
# 1.x moved to httpx2) — so this adds a second HTTP client rather than upgrading the one the
# Shopify/exchange clients use, and neither pin constrains the other.
#
# The offline double stays the default (D20): with `LLM_PROVIDER` unset this wheel is
# installed and never imported, because `import anthropic` still only runs inside
# `AnthropicLLM._ensure_client()`. What changes is that the live path now exists.
RUN pip install --no-cache-dir "anthropic==1.2.0"

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
# `proxyshop_support/` completes the BYTE-IDENTICAL prologue the block at the top of this
# file describes ("contracts/support copy"), and it is not optional here: T-308 wired
# `configure_logging()` into every service's `create_app()`, so `src/main.py:31` is
# `from proxyshop_support.logging_config import RequestIdMiddleware, configure_logging` at
# column 0 — fatal at import, i.e. the container could not start without this line. It
# lands at /app/proxyshop_support, and `/app` is already a PYTHONPATH root (line 47), so no
# `.pkgroot` link is needed; this is exactly how buyer, exchange, merchant, trust, ingest
# and sim ship it.
COPY proxyshop_support/             /app/proxyshop_support/
COPY packages/store-agent/src/   /app/packages/store-agent/src/

# The store-side advocate (D55) imports `llm` at MODULE SCOPE -- runtime/pitch.py and
# solicitation/copywriter.py reach `llm.prompting`, `llm.client`, `llm.config` and
# `llm.doubles` -- so an image without this package cannot import the bidding runtime at
# all. It shipped without it and `test_artifact_copyset` caught it: the advocate was
# tested, served and unshippable, which is this repo's signature defect wearing a
# deployment hat.
#
# `fixtures/` is not optional either: the offline copywriter path resolves its recorded
# replies out of `packages/llm/fixtures/recorded/`, and a deployment with no API key is
# the DEFAULT configuration, not an edge case.
COPY packages/llm/src/              /app/packages/llm/src/
COPY packages/llm/fixtures/         /app/packages/llm/fixtures/

# `.pkgroot/<pkg> -> <member>/src` is what makes the FLAT layout importable (R1b/D42).
# The repo tracks these as git symlinks; the image RECREATES them instead of copying them
# so the build never depends on how a daemon happens to treat symlinks in a context tar.
#
# The `store_agent` link is load-bearing beyond the usual reason. `runtime/__init__.py:75`,
# `hooks/__init__.py:114` and `modes/__init__.py:76` each call
# `importlib.import_module("store_agent.<sub>")` at import time and compare the result's
# __file__ with their own, to bind one canonical spelling. Reached under any other name that
# lookup fails (caught) or resolves to a SECOND module object with its own copy of the
# module state. This link is what makes `store_agent.*` the only spelling in the image.
#
# There is no `proxyshop_support` link and there must not be one: that package is copied
# straight to /app (see the COPY above), which is a PYTHONPATH root in its own right.
RUN mkdir -p /app/.pkgroot \
 && ln -s ../packages/contracts/src /app/.pkgroot/contracts \
 && ln -s ../packages/store-agent/src /app/.pkgroot/store_agent \
 && ln -s ../packages/llm/src /app/.pkgroot/llm

# Non-root: nothing in these services writes to the filesystem, so there is no reason for
# the process to be able to.
RUN useradd --create-home --uid 10001 proxyshop
USER proxyshop

EXPOSE 8086

# One worker on purpose. The frozen entrypoint builds the app at import time and a feature
# router may hold in-process state; a second worker would answer half the requests from a
# different copy of it.
CMD ["uvicorn", "store_agent.main:app", "--host", "0.0.0.0", "--port", "8086", "--workers", "1"]
