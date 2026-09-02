"""Content hashing and snapshot refs for change detection (T-020).

The adapter's promise is that **unchanged content costs nothing downstream**: if a
storefront serves the same catalog today as yesterday, no claim is re-extracted and no
node is re-upserted. That promise is only as good as the hash, and a naive
``sha256(response_body)`` breaks it in two ordinary, non-malicious ways:

* Shopify serialises ``products.json`` from a dict. Key order and whitespace are not
  stable across their deploys, so byte hashing reports "changed" on a catalog that is
  semantically identical, and the whole crawl re-extracts for nothing.
* Conversely, hashing a *parsed and re-serialised* structure while ignoring values the
  adapter actually reads would report "unchanged" on a real price move.

So there are two functions with different jobs. :func:`content_hash` is the byte-exact
digest, used for policy pages and any opaque body, and is what goes in a snapshot ref.
:func:`canonical_json_hash` normalises JSON structure — sorted keys, no insignificant
whitespace — before digesting, so it is stable against re-serialisation while remaining
sensitive to every value. Neither ever touches the network.

Digests are returned prefixed (``"sha256:…"``) so a stored hash carries its own algorithm
and a future migration to a different one is a comparison that fails loudly rather than a
silent mismatch.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "HASH_ALGORITHM",
    "HASH_PREFIX",
    "canonical_json_hash",
    "content_hash",
    "has_changed",
    "snapshot_ref",
]

HASH_ALGORITHM = "sha256"
HASH_PREFIX = f"{HASH_ALGORITHM}:"


def content_hash(payload: str | bytes) -> str:
    """Byte-exact digest of ``payload``, prefixed with its algorithm.

    Text is encoded as UTF-8 so the digest of a ``str`` and of its UTF-8 bytes agree.
    """
    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def canonical_json_hash(payload: Any) -> str:
    """Digest of a JSON document's *structure and values*, not its serialisation.

    Accepts an already-parsed object or raw JSON text. Raw text that does not parse falls
    back to :func:`content_hash`, which is the conservative answer: an unparseable body
    might be a truncated response, and reporting it as "the same as last time" would let a
    truncation stick.
    """
    if isinstance(payload, str | bytes | bytearray):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return content_hash(payload)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return content_hash(canonical)


def has_changed(previous_hash: str | None, current_hash: str) -> bool:
    """Whether content must be re-processed.

    ``previous_hash is None`` means "never seen", which is a change: there is nothing to
    compare against, so the only safe answer is to do the work.
    """
    if not previous_hash:
        return True
    return str(previous_hash).strip() != str(current_hash).strip()


def snapshot_ref(url: str, digest: str) -> str:
    """A stable, human-readable pointer to one observed version of one URL.

    Shaped ``snapshot://<host><path>@sha256:…`` so a provenance record names both *what*
    was read and *which version* of it, and two observations of the same unchanged page
    produce the same ref.
    """
    split = urlsplit(str(url))
    host = (split.hostname or "").lower()
    port = f":{split.port}" if split.port and split.port not in (80, 443) else ""
    path = split.path or "/"
    query = f"?{split.query}" if split.query else ""
    prefixed = digest if str(digest).startswith(HASH_PREFIX) else HASH_PREFIX + str(digest)
    return f"snapshot://{host}{port}{path}{query}@{prefixed}"
