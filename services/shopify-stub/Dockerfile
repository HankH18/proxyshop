# The shopify-stub container. Only the e2e profile uses it (see compose.yaml); unit tests
# build the app in-process on an ephemeral port instead (D41).
#
# The dependency versions below MIRROR the root pyproject.toml's pins — they are not a
# second source of truth and they are not a manifest a worker may edit. If the root manifest
# moves, this file follows it; it must never lead. Pinned rather than floating so a rebuilt
# image is the same image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir \
      "fastapi==0.141.1" \
      "uvicorn[standard]==0.52.4" \
      "httpx==0.28.1" \
      "pydantic==2.13.5"

# The FLAT source layout (D42): src/<module>.py imports as shopify_stub.<module>, which is
# what the `.pkgroot/shopify_stub -> src` symlink provides in the repo. In the image the
# same effect is had by copying src/ to a directory literally named shopify_stub.
COPY services/shopify-stub/src/ /app/shopify_stub/
# T-304. The fixtures land at /app/fixtures/, NOT at /app/services/shopify-stub/fixtures/,
# because the line above FLATTENS src/ and `recordings.py:46` locates the recordings
# relative to its own file:
#     RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "recorded"
# In the checkout that module is services/shopify-stub/src/recordings.py, so parent.parent
# is services/shopify-stub/ and the lookup is right. In the image it is
# /app/shopify_stub/recordings.py, so parent.parent is bare /app and the lookup is
# /app/fixtures/recorded. Reproducing the repo path here — which is what this line used to
# do — put the bytes somewhere the shipped module can never look: the checkout resolved it
# and the artifact did not, with no error, just an empty recordings set for the first
# consumer to import it. The COPY destination follows the flatten instead of fighting it, so
# the module's own path arithmetic is true on both sides.
COPY services/shopify-stub/fixtures/ /app/fixtures/

# T-308 wired `configure_logging()` into every service's `create_app()`, so `src/app.py:94`
# is `from proxyshop_support.logging_config import RequestIdMiddleware, configure_logging`
# at column 0 — fatal at import, i.e. this container could not start without the lines
# below. `/app` is this image's only sys.path root (there is no ENV PYTHONPATH; uvicorn's
# `--app-dir` defaults to `.`, which is WORKDIR, and that is already what makes
# `shopify_stub` importable at all), so copying the package to /app/proxyshop_support is all
# the wiring it needs — no `.pkgroot` link, same as the six images that ship it whole.
#
# The MEASURED CLOSURE rather than the whole package, and the difference is this image's
# four-wheel pip layer. `COPY proxyshop_support/ /app/proxyshop_support/` — what buyer,
# exchange, merchant, trust, ingest and sim do — ships `redis_client.py`, whose line 30 is
# `import redis` at column 0, and this image installs no redis. Measured, not feared: with
# the whole-package copy in place the gate reported "services/shopify-stub/Dockerfile: 1 of
# 27 shipped modules cannot be imported ... proxyshop_support/redis_client.py:
# ModuleNotFoundError: No module named 'redis'". The alternative was pinning redis (and
# hiredis) into a stub that never opens a socket to it; copying what the stub actually
# imports keeps the pip layer honest, and it is the same partial-copy idiom
# `apps/exchange/Dockerfile:85-86` uses for `packages/store-agent` (an `__init__.py` plus
# the one subtree it needs).
#
# `logging_config` is stdlib-only at every scope and `proxyshop_support/__init__.py` imports
# nothing, so this pair is self-contained. Add a second `proxyshop_support.*` import to this
# service and it will NOT silently degrade: that module is absent here, the import is
# module-scope, and the copy-set gate's runtime half fails on the ModuleNotFoundError.
COPY proxyshop_support/__init__.py       /app/proxyshop_support/__init__.py
COPY proxyshop_support/logging_config.py /app/proxyshop_support/logging_config.py

EXPOSE 8787

# One worker on purpose: the stub's whole state is in process memory, and a second worker
# would answer half the requests from an empty discount-code table.
CMD ["uvicorn", "shopify_stub.app:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1"]
