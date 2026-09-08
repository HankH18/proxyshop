# The proxyshop-buyer-svc container.
#
# The buyer svc API — magic-link auth, the pseudonym vault and the buyer profile.
# apps/buyer/app/ is a TypeScript scaffold with no build of its own yet, so the buyer
# deployable is this API; when the web app grows a build it becomes a second stage here
# or a second service in apps/buyer/compose.yaml.
#
# The repo root is on PYTHONPATH as well as .pkgroot because buyer_svc.profile imports
# `contracts` with a `packages.contracts` fallback (see its try/except) and BOTH
# spellings have to resolve to the same module object.
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

# ---------------------------------------------------------------------------------
# This image's OWN layer, on top of the byte-identical prologue above.
# ---------------------------------------------------------------------------------
# A SEPARATE `RUN pip install`, not an edit to the block above, and that is the established
# shape here rather than a preference: `apps/exchange/Dockerfile` and `services/ingest/
# Dockerfile` both add their extra distributions in a second layer for the same reason. The
# prologue's instruction string is the Docker cache key shared by all eight service images,
# so adding one requirement there would re-download the whole dependency set eight times.
# The prologue's sentence "numpy, lxml and anthropic are in no deployable's closure and are
# deliberately absent" remains true OF THE PROLOGUE, which is the block it describes.
#
# WHY THIS IMAGE NEEDS IT. D55 gives the buyer-side agent its own objective: it pitches EVERY
# candidate on the shortlist, scraped shops included, in the platform's own voice and only
# from facts the exchange already holds. That case is written on `POST /buyer/shortlist/
# render` by `pitch.writing.compose_case`, whose client comes from
# `pitch.writer.pitch_writer()`, which selects `AnthropicLLM` on `LLM_PROVIDER=anthropic`.
# Without the line below that selection did not fail — it SUCCEEDED, because
# `llm.client.AnthropicLLM.__init__` performs no I/O and imports no SDK (D20). The
# ModuleNotFoundError arrived one frame deeper, on the first `complete()`, inside
# `import anthropic` at `packages/llm/src/client.py:168`, and `compose_case` catches
# everything and answers with `assemble()` so a model outage can never cost a shopper a
# slot. Net effect in every container-based deployment: both of D55's voices were the
# deterministic slot-filled fallback, at 200 OK, with a configuration that said `anthropic`.
# The SAME wheel closes the `buyer_svc.intent.routes` clarifier, whose `from llm import
# build_llm` (T-300) has been importable since that ticket and whose live path had the
# identical hole.
#
# The version MIRRORS the root pyproject.toml pin (`anthropic==1.2.0`) exactly as the
# prologue's set does, and for the same reason: this file follows the root manifest and must
# never lead it.
#
# It pulls anyio, docstring-parser, httpx2, jiter, sniffio and typing-extensions. Note
# httpx2, which is a DIFFERENT distribution from the prologue's `httpx==0.28.1` (anthropic
# 1.x moved to httpx2) — so this adds a second HTTP client rather than upgrading the one the
# exchange and trust clients use, and neither pin constrains the other.
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
COPY proxyshop_support/             /app/proxyshop_support/
COPY apps/buyer/svc/src/         /app/apps/buyer/svc/src/
# T-300. `apps/buyer/svc/src/intent/routes.py:99` is `from llm import build_llm`, INDENTED
# inside `buyer_llm()` and wrapped in `except ImportError` that logs a warning and returns
# None. Without the two lines below that except branch is what the DEPLOYED buyer runs, on
# every /buyer/intent/clarify request, for the life of the image: no container crashes, no
# healthcheck fails, every shipped module still imports, and the clarification loop silently
# falls back to its own hard-coded wording and its own extraction. The only evidence is one
# WARNING line, in a service that installs no logging handler at all (T-308). The `# pragma:
# no cover - only with a broken sys.path` on that except branch was describing this image.
#
# This costs NOTHING in the pip layer, which is why it is a COPY and not a dependency
# change. Measured, not assumed: the module-scope closure of the whole `packages/llm` tree
# is stdlib only — `import anthropic` lives at `packages/llm/src/client.py:168`, INSIDE
# `AnthropicLLM._ensure_client()`, and `config.py:82` makes the offline double the default
# provider, so `build_llm("buyer")` returns a `DeterministicLLM` with no SDK, no key and no
# socket. The prologue's "anthropic ... deliberately absent" stays true.
#
# `fixtures/` comes along for the same reason `packages/contracts/schemas/` does above:
# `packages/llm/src/recordings.py:45` resolves its recordings as
# `Path(__file__).resolve().parents[1] / "fixtures" / "recorded"`, so the repo shape has to
# be reproduced or that lookup lands on a directory the image does not have. Nothing on the
# buyer's own path reads it today — which is exactly what made T-304 latent for so long.
COPY packages/llm/src/              /app/packages/llm/src/
COPY packages/llm/fixtures/         /app/packages/llm/fixtures/

# `buyer_svc.livecheck` imports `claim_verification` at MODULE SCOPE, and it is mounted by
# the frozen `main.create_app` glob, so without the line below `importlib.import_module`
# raises inside `create_app()` and the whole service fails to start — not a degraded feature,
# a crash loop. It is a plain module-scope import on purpose: the two lazy/guarded shapes in
# this image (T-300's `from llm import ...`, and `guarded_page_fetcher`'s `ingest` import)
# are both there because the dependency is genuinely optional, and this one is not.
#
# It costs NOTHING in the pip layer, measured the same way T-300's addition was: the
# module-scope closure of `packages/verification/src` is stdlib only — `json`, `hashlib`,
# `math`, `re`, `html.parser`, `dataclasses`, `datetime`, `collections.abc`, `types`,
# `typing`. No fastapi, no pydantic, no beautifulsoup4, no lxml. `live_page.py` parses
# storefront HTML with `html.parser` from the standard library FOR THIS REASON: bs4 and lxml
# are in the root manifest and in NO service image's pip layer, so a parser dependency there
# would import in a checkout and be absent here.
COPY packages/verification/src/     /app/packages/verification/src/

# `.pkgroot/<pkg> -> <member>/src` is what makes the FLAT layout importable (R1b/D42).
# The repo tracks these as git symlinks; the image RECREATES them instead of copying them
# so the build never depends on how a daemon happens to treat symlinks in a context tar.
RUN mkdir -p /app/.pkgroot \
 && ln -s ../packages/contracts/src /app/.pkgroot/contracts \
 && ln -s ../apps/buyer/svc/src /app/.pkgroot/buyer_svc \
 && ln -s ../packages/llm/src /app/.pkgroot/llm \
 && ln -s ../packages/verification/src /app/.pkgroot/claim_verification

# Non-root: nothing in these services writes to the filesystem, so there is no reason for
# the process to be able to.
RUN useradd --create-home --uid 10001 proxyshop
USER proxyshop

EXPOSE 8081

# One worker on purpose. The frozen entrypoint builds the app at import time and a feature
# router may hold in-process state (the auction registry, the install nonce table); a
# second worker would answer half the requests from a different copy of it.
CMD ["uvicorn", "buyer_svc.main:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]
