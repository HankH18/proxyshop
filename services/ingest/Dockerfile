# The proxyshop-ingest container. (T-313)
#
# The ingest API — entity resolution and claim extraction. `ingest.main:app` mounts the two
# routers its glob finds, `/er` and `/extraction`.
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
# This image keeps that block VERBATIM even though ingest itself needs only fastapi, uvicorn
# and pydantic of it. Deviating would buy nothing: the layer is keyed on the instruction
# text, so an identical one is the SAME layer on disk and the same single download shared
# with the other images, while a shortened one is a second download and a second copy. The
# wheels ingest does not import cost no build time and no additional disk.
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

# Ingest's own layer, and the one place this image is genuinely bigger than its siblings.
# Every one of these is imported LAZILY, inside the function that needs it, so none of them
# is required for the image to start or for any module to import — which is exactly why an
# import scan cannot see them and why they have to be reasoned about rather than derived:
#
#   neo4j            graph/reembed.py:386, inside graph_driver(). Without it the graph
#                    write path raises on first use; ingest's whole job is writing the graph.
#   beautifulsoup4   adapters/signed_fetch.py:446, inside SignedFetchAdapter._blocks(),
#   lxml             which asks BeautifulSoup for the "lxml" parser at :449 and falls back
#                    to html.parser and then to a regex. Without them extraction still runs
#                    and quietly gets worse, which is the failure mode this lane exists to
#                    stop shipping.
#   protego          adapters/robots.py:97,110, inside a try/except that FAILS CLOSED at
#                    :102-103 — so an image without protego does not crawl at all, it just
#                    declines every URL and reports nothing wrong.
#
# NOT numpy and NOT scipy, and this corrects a claim rather than following one:
# `docs/deploy.md:118-120` says ingest "additionally needs numpy/scipy/lxml/bs4". Measured
# against the tree, `numpy` and `scipy` do not appear anywhere under services/ingest/src —
# not at module scope, not inside a function, not in a TYPE_CHECKING block. The vector work
# is stdlib `math`: proxyshop_support/embedding.py's cosine, and er/matching.py:76 which
# imports it. sentence-transformers/torch are likewise absent, and deliberately: D18 keeps
# them out of the default install, embeddings/local_bge.py:93 defers the import into
# _load(), and EMBEDDING_PROVIDER=hash is the path the compose fragment selects.
RUN pip install --no-cache-dir \
      "neo4j==5.28.5" \
      "beautifulsoup4==4.15.0" \
      "lxml==6.1.2" \
      "protego==0.6.2"

# No `packages/contracts` here, and that is measured rather than an oversight: nothing under
# services/ingest/src imports `contracts` at any scope. Ingest speaks the graph model and
# its own claim dataclasses; the contract envelopes are the exchange's and the trust
# ledger's boundary, not this service's.
COPY proxyshop_support/             /app/proxyshop_support/
COPY services/ingest/src/        /app/services/ingest/src/
# `extraction/llm_extractor.py:136` is `from llm import build_llm`, inside
# build_extract_client() and — unlike the buyer's equivalent — NOT wrapped in try/except, so
# an image without it raises the moment the LLM-backed extractor is built. It is the same
# defect shape as T-300 and it is closed here rather than left to be found later. Costs
# nothing in the pip layer: the whole packages/llm tree is stdlib-only at module scope and
# `import anthropic` lives inside AnthropicLLM._ensure_client(). `fixtures/` comes with it
# because recordings.py:45 resolves the recordings as
# `Path(__file__).resolve().parents[1] / "fixtures" / "recorded"` — the T-304 class.
COPY packages/llm/src/              /app/packages/llm/src/
COPY packages/llm/fixtures/         /app/packages/llm/fixtures/

# `.pkgroot/<pkg> -> <member>/src` is what makes the FLAT layout importable (R1b/D42).
# The repo tracks these as git symlinks; the image RECREATES them instead of copying them
# so the build never depends on how a daemon happens to treat symlinks in a context tar.
RUN mkdir -p /app/.pkgroot \
 && ln -s ../services/ingest/src /app/.pkgroot/ingest \
 && ln -s ../packages/llm/src /app/.pkgroot/llm

# Non-root: nothing in these services writes to the filesystem, so there is no reason for
# the process to be able to.
RUN useradd --create-home --uid 10001 proxyshop
USER proxyshop

EXPOSE 8085

# One worker on purpose. The frozen entrypoint builds the app at import time and a feature
# router may hold in-process state; a second worker would answer half the requests from a
# different copy of it.
CMD ["uvicorn", "ingest.main:app", "--host", "0.0.0.0", "--port", "8085", "--workers", "1"]
