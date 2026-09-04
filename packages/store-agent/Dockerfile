# The proxyshop-store-agent container. (T-313)
#
# The store agent — the bidding runtime, the provenance hooks, the learning grid and the
# external door an unharnessed seller speaks through. `store_agent.main:app` is the frozen
# entrypoint.
#
# STATED PLAINLY, because a reader deserves it before they deploy this: the app serves NO
# business routes. `main.py:29-30` mounts routers by globbing `*/routes.py` and there is no
# `routes.py` anywhere under packages/store-agent/src, so `app.state.mounted_routers` is []
# and the container answers only FastAPI's own /openapi.json, /docs and /redoc. That is not
# a packaging defect and this Dockerfile does not paper over it: the store agent's surface
# is a LIBRARY consumed in-process (services/sim drives it; apps/exchange solicits through
# it), and the HTTP entrypoint is the frozen T-000 scaffold waiting for a router. The image
# is real, it starts, and its healthcheck is honest about probing only that.
#
# Build context is the REPO ROOT (see the sibling compose.yaml), not this directory: the
# flat src layout (D42) means a service image has to reach across member directories.

FROM python:3.12-slim

# The prologue of all four service Dockerfiles is BYTE-IDENTICAL (base image, ENV, WORKDIR,
# pip layer, contracts/support copy). That is deliberate: the layer cache is keyed on the
# parent image plus the instruction, so building all four services costs ONE dependency
# download rather than four. Change the pin list here and you change it in all four.
#
# The versions below MIRROR the root pyproject.toml's pins — they are not a second source of
# truth and they are not a manifest a worker may edit. If the root manifest moves, this file
# follows it; it must never lead. The set is the measured closure of what the four service
# packages import: fastapi/uvicorn/pydantic for the app itself, httpx for the merchant's
# Shopify Admin client, psycopg for the ledger and the vault, redis for D39's worker client,
# jsonschema (which pulls referencing) for contracts.registry, email-validator because
# buyer_svc.auth.routes types a field `EmailStr` and pydantic imports the validator lazily
# at schema-build time — an extras dependency an import scan cannot see, and the reason
# buyer-svc crash-looped on its first deploy. numpy, scipy, lxml, anthropic
# and mcp are in no deployable's closure and are deliberately absent.
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
      "python-multipart==0.0.32" \
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
COPY packages/store-agent/src/   /app/packages/store-agent/src/

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
# `proxyshop_support/` is deliberately NOT copied: nothing under packages/store-agent/src
# imports it at any scope.
RUN mkdir -p /app/.pkgroot \
 && ln -s ../packages/contracts/src /app/.pkgroot/contracts \
 && ln -s ../packages/store-agent/src /app/.pkgroot/store_agent

# Non-root: nothing in these services writes to the filesystem, so there is no reason for
# the process to be able to.
RUN useradd --create-home --uid 10001 proxyshop
USER proxyshop

EXPOSE 8086

# One worker on purpose. The frozen entrypoint builds the app at import time and a feature
# router may hold in-process state; a second worker would answer half the requests from a
# different copy of it.
CMD ["uvicorn", "store_agent.main:app", "--host", "0.0.0.0", "--port", "8086", "--workers", "1"]
