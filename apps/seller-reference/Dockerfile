# The proxyshop-seller-reference container. (T-313)
#
# The unharnessed reference seller personas (R18/S8/A3) — the sellers the platform does NOT
# host, which run their own agent and speak through the external door.
#
# STATED PLAINLY, because the honest shape of this component is the whole difficulty of
# containerising it: `apps/seller-reference/src` is TWO files, one of them empty, and it has
# no server, no `main.py`, no `__main__.py` and no `routes.py`. Its entire public surface is
# `seller_reference.personas`'s `build_persona()` / `persona_names()`. There is nothing to
# `uvicorn` and there is nothing to keep running.
#
# So this image does not pretend to be a service. Its CMD is a ONE-SHOT SELF-CHECK that
# imports the package, resolves the personas and exits 0, and the sibling compose.yaml runs
# it with `restart: "no"` and no port. That is a real, useful artifact: it proves the library
# is installable and that the manifest chain it reads verifies inside the container — which
# is precisely the check nothing else in the stack performs — and it does not manufacture a
# long-running process that would only ever idle. The alternative shapes were considered and
# rejected: a `sleep infinity` container claims a service that does not exist, and leaving
# the fragment a `services: {}` stub is the defect T-313 exists to close.
#
# The self-check is not decorative. `persona_names()` calls `load_manifest()`, which
# verifies the sha256 digest chain across fixtures/manifest.json, the golden set and the
# seed catalog on EVERY call — three file reads per call, by design — so a container whose
# fixtures were copied wrong exits non-zero instead of silently building personas from
# something else.
#
# Build context is the REPO ROOT (see the sibling compose.yaml), not this directory: the
# flat src layout (D42) means a member image has to reach across member directories.

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
# Kept VERBATIM for the layer-cache reason above: an identical instruction is the SAME layer
# on disk as the other images', so the wheels this package does not import cost no extra
# download and no extra disk. This package's own measured need is pydantic alone, reached
# through `contracts`; it imports no third-party name itself.
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
COPY apps/seller-reference/src/  /app/apps/seller-reference/src/

# The approved fixture manifest and everything its digest chain covers.
# `personas/__init__.py:84` is a module-scope `from fixtures.manifest import load_manifest`,
# and the layout is a hard requirement rather than a convenience:
# `fixtures/__init__.py:37-40` sets `REPO_ROOT = Path(__file__).resolve().parent.parent` and
# `fixtures/manifest/__init__.py:234-240` RAISES unless the manifest resolves at
# `<root>/fixtures/manifest.json`. With WORKDIR /app that pins the tree to /app/fixtures/.
COPY fixtures/                   /app/fixtures/

# `.pkgroot/<pkg> -> <member>/src` is what makes the FLAT layout importable (R1b/D42).
# The repo tracks these as git symlinks; the image RECREATES them instead of copying them
# so the build never depends on how a daemon happens to treat symlinks in a context tar.
# `/app` is on PYTHONPATH alongside `.pkgroot` because `fixtures` has no `src/` and no
# `.pkgroot` entry — it is imported by its top-level name from the repo root.
RUN mkdir -p /app/.pkgroot \
 && ln -s ../packages/contracts/src /app/.pkgroot/contracts \
 && ln -s ../apps/seller-reference/src /app/.pkgroot/seller_reference

# Non-root: nothing here writes to the filesystem, so there is no reason for the process to
# be able to.
RUN useradd --create-home --uid 10001 proxyshop
USER proxyshop

# No EXPOSE: this container serves nothing. See the header.
CMD ["python", "-c", "from seller_reference.personas import build_persona, persona_names; names = persona_names(); print('seller-reference OK:', len(names), 'personas:', ', '.join(names)); [build_persona(n) for n in names]; print('every persona builds against the verified manifest chain')"]
