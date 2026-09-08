# The proxyshop-merchant-svc container.
#
# The merchant svc API — the Shopify OAuth install flow, the web-pixel/webhook
# registration and the HMAC-verified webhook door. httpx is its Admin GraphQL client.
#
# THIS IMAGE SERVES NO DASHBOARD, and that is now a choice rather than a fact about the repo.
# `apps/merchant/app/` grew a real vite build (`package.json` -> `build:ui`), and
# `dashboard/routes.py` mounts it at `/dashboard` from whatever directory `MERCHANT_UI_DIST`
# names — so this image answers every `/dashboard/*` request with the 503 page that asks for a
# build. The sibling `apps/merchant/Dockerfile.web` is this same service WITH the bundle built
# in and `MERCHANT_UI_DIST` set, and it exists as a separate file because the bundle needs a
# `COPY --from=` that `proxyshop_support/tests/test_artifact_copyset.py` deliberately refuses
# to model in a file named `Dockerfile`. Its header carries the full argument. Deploy that one
# wherever an operator is meant to reach the dashboard.
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
COPY apps/merchant/svc/src/      /app/apps/merchant/svc/src/
# T-298. `apps/merchant/svc/src/codes/offer.py:36` is
# `from exchange.checkout.discounts import UnusableDiscount, shopify_discount_percentage`
# at COLUMN 0, in a module `merchant_svc.codes.routes` imports and `merchant_svc.main`
# mounts. Without the lines below `uvicorn merchant_svc.main:app` raises
# ModuleNotFoundError before uvicorn has an app: the container cannot start. Not degraded,
# not slow — dead on first boot, and seven of the image's own modules unimportable.
#
# This is a DELIBERATELY NARROW copy, and the wholesale one is a trap that was measured.
# `apps/exchange/Dockerfile:61` copies `apps/exchange/src/` entire; applied verbatim here it
# also ships `exchange.retrieval` and `exchange.ranking`, whose module-scope
# `from ingest.graph import ...` would turn this image's T-298 into T-299 — 11 more
# unimportable modules — and the repair for THAT would be shipping the ingest graph library
# into the merchant container too. Shipping a package to silence a checker about code the
# process never runs is the opposite of what the artifact gates exist for.
#
# What is shipped is the MEASURED runtime closure of `exchange.checkout.discounts` and
# nothing else: `checkout/` in full (its `__init__.py` is a re-export barrel, so importing
# one of its modules executes all nine siblings) plus the four `auction/` modules
# `checkout/provider.py:40`'s `from ..auction.ledger import build_published_event` reaches
# through `auction/__init__.py`. 16 modules of 42.
#
# `auction/routes.py` is EXCLUDED on purpose, and it is the reason `auction/` is named file
# by file instead of as a directory: it is the auction's HTTP surface, served by
# `exchange.main`, which this image never runs, and its module-scope `..eligibility` /
# `..orchestration` imports (routes.py:39-40) would drag in three more subpackages — nine
# more modules, 16 -> 25 measured — for a router nothing here mounts. The merchant needs
# the auction's ledger event builder, not the auction service.
#
# No new pip requirement: the only third-party name the shipped subset reaches is pydantic,
# via `contracts.boundary`, and contracts is already here.
#
# This subset is not a private convention that can rot silently. Every module an image ships
# is imported inside a container-shaped tree by
# proxyshop_support/tests/test_artifact_copyset.py::test_t301_every_shipped_module_imports_inside_the_container_shaped_tree,
# so the day a `checkout/` or `auction/` module grows a module-scope import into a
# subpackage this COPY set does not cover, that gate goes red here — instead of the merchant
# container crash-looping in a deploy.
COPY apps/exchange/src/__init__.py /app/apps/exchange/src/
COPY apps/exchange/src/checkout/   /app/apps/exchange/src/checkout/
COPY apps/exchange/src/auction/__init__.py \
     apps/exchange/src/auction/collect.py \
     apps/exchange/src/auction/fanout.py \
     apps/exchange/src/auction/ledger.py \
     apps/exchange/src/auction/state.py \
     /app/apps/exchange/src/auction/

# `.pkgroot/<pkg> -> <member>/src` is what makes the FLAT layout importable (R1b/D42).
# The repo tracks these as git symlinks; the image RECREATES them instead of copying them
# so the build never depends on how a daemon happens to treat symlinks in a context tar.
RUN mkdir -p /app/.pkgroot \
 && ln -s ../packages/contracts/src /app/.pkgroot/contracts \
 && ln -s ../apps/merchant/svc/src /app/.pkgroot/merchant_svc \
 && ln -s ../apps/exchange/src /app/.pkgroot/exchange

# Non-root: nothing in these services writes to the filesystem, so there is no reason for
# the process to be able to.
RUN useradd --create-home --uid 10001 proxyshop
USER proxyshop

EXPOSE 8082

# One worker on purpose. The frozen entrypoint builds the app at import time and a feature
# router may hold in-process state (the auction registry, the install nonce table); a
# second worker would answer half the requests from a different copy of it.
CMD ["uvicorn", "merchant_svc.main:app", "--host", "0.0.0.0", "--port", "8082", "--workers", "1"]
