"""Catalog adapters: the one interface, and the signed page fetcher behind it (T-020, C6).

SPEC C6 puts catalog ingestion behind a single adapter interface with two implementations —
``catalog_mcp`` (production primary) and ``signed_fetch`` (the page fetcher, which is the
only one that can see a password-protected dev store, A1). This package holds the interface
and the fetcher, plus the guards every adapter that touches a network must run through.

Layout::

    base.py        CatalogAdapter + the records that cross the seam (+ apply_upserts)
    netguard.py    the SSRF guard: resolve, decide, pin; redirect chains
    robots.py      crawler identity (USER_AGENT) and robots.txt obedience
    budgets.py     pages / depth / time / bytes ceilings a hostile store cannot evade
    transport.py   the guarded HTTP client everything on the wire goes through
    hashing.py     content hashes and snapshot refs for change detection
    signed_fetch.py the adapter itself

Typical use::

    from ingest.adapters import CatalogRequest, SignedFetchAdapter, apply_upserts

    adapter = SignedFetchAdapter()
    snapshot = adapter.fetch_catalog(
        CatalogRequest(store_id="store-1", base_url="https://dev.example.com",
                       storefront_password="hunter2", known_hashes=previous)
    )
    apply_upserts(session, adapter.to_upserts(snapshot))   # [] when nothing changed

The four names the SPEC C10 guard contract is stated in — :data:`USER_AGENT`,
:func:`is_fetch_allowed`, :func:`is_redirect_chain_allowed` and :func:`may_fetch` — are
re-exported here and are the stable surface other tickets should import.
"""

from __future__ import annotations

from . import signed_fetch
from .base import (
    CatalogAdapter,
    CatalogRequest,
    CatalogSnapshot,
    FetchedResource,
    ProductRecord,
    UpsertOp,
    VariantRecord,
    apply_upserts,
)
from .budgets import BudgetExceeded, BudgetUsage, CrawlBudget, CrawlLedger
from .hashing import canonical_json_hash, content_hash, has_changed, snapshot_ref
from .netguard import (
    BLOCKED_IPV4_NETWORKS,
    BLOCKED_IPV6_NETWORKS,
    DENIED_HOST_SUFFIXES,
    DENIED_HOSTS,
    FetchPolicy,
    FetchRefused,
    FetchVerdict,
    address_refusal,
    decode_numeric_ipv4,
    fetch_verdict,
    host_matches_allowlist,
    is_fetch_allowed,
    is_redirect_chain_allowed,
    normalise_host,
    resolve_host,
)
from .robots import (
    PRODUCT_TOKEN,
    USER_AGENT,
    crawl_delay,
    may_fetch,
    robots_url,
    robots_verdict_for_status,
    user_agent_token,
)
from .signed_fetch import SignedFetchAdapter
from .transport import HTTPResult, RequestSigner, SafeHTTPClient, TransportError

__all__ = [
    "BLOCKED_IPV4_NETWORKS",
    "BLOCKED_IPV6_NETWORKS",
    "DENIED_HOSTS",
    "DENIED_HOST_SUFFIXES",
    "PRODUCT_TOKEN",
    "USER_AGENT",
    "BudgetExceeded",
    "BudgetUsage",
    "CatalogAdapter",
    "CatalogRequest",
    "CatalogSnapshot",
    "CrawlBudget",
    "CrawlLedger",
    "FetchPolicy",
    "FetchRefused",
    "FetchVerdict",
    "FetchedResource",
    "HTTPResult",
    "ProductRecord",
    "RequestSigner",
    "SafeHTTPClient",
    "SignedFetchAdapter",
    "TransportError",
    "UpsertOp",
    "VariantRecord",
    "address_refusal",
    "apply_upserts",
    "canonical_json_hash",
    "content_hash",
    "crawl_delay",
    "decode_numeric_ipv4",
    "fetch_verdict",
    "has_changed",
    "host_matches_allowlist",
    "is_fetch_allowed",
    "is_redirect_chain_allowed",
    "may_fetch",
    "normalise_host",
    "resolve_host",
    "robots_url",
    "robots_verdict_for_status",
    "signed_fetch",
    "snapshot_ref",
    "user_agent_token",
]
