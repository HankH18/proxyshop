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

EXPOSE 8787

# One worker on purpose: the stub's whole state is in process memory, and a second worker
# would answer half the requests from an empty discount-code table.
CMD ["uvicorn", "shopify_stub.app:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1"]
