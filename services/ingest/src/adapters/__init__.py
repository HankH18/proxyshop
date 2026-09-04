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

from . import catalog_mcp, signed_fetch
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
from .catalog_mcp import (
    CatalogMCPAdapter,
    MCPError,
    MCPSession,
    MCPToolError,
    RecordedMCPSession,
    UnrecordedMCPCall,
)
from .hashing import canonical_json_hash, content_hash, has_changed, snapshot_ref
from .mapping import (
    build_upserts,
    coerce_availability,
    coerce_price,
    composite_hash,
    native_key,
    native_product_key,
    product_id_for,
    stable_id,
    variant_id_for,
)
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
    "BudgetExceeded",
    "BudgetUsage",
    "CatalogAdapter",
    "CatalogMCPAdapter",
    "CatalogRequest",
    "CatalogSnapshot",
    "CrawlBudget",
    "CrawlLedger",
    "DENIED_HOSTS",
    "DENIED_HOST_SUFFIXES",
    "FetchPolicy",
    "FetchRefused",
    "FetchVerdict",
    "FetchedResource",
    "HTTPResult",
    "MCPError",
    "MCPSession",
    "MCPToolError",
    "PRODUCT_TOKEN",
    "ProductRecord",
    "RecordedMCPSession",
    "RequestSigner",
    "SafeHTTPClient",
    "SignedFetchAdapter",
    "TransportError",
    "USER_AGENT",
    "UnrecordedMCPCall",
    "UpsertOp",
    "VariantRecord",
    "address_refusal",
    "apply_upserts",
    "build_upserts",
    "canonical_json_hash",
    "catalog_mcp",
    "coerce_availability",
    "coerce_price",
    "composite_hash",
    "content_hash",
    "crawl_delay",
    "decode_numeric_ipv4",
    "fetch_verdict",
    "has_changed",
    "host_matches_allowlist",
    "is_fetch_allowed",
    "is_redirect_chain_allowed",
    "may_fetch",
    "native_key",
    "native_product_key",
    "normalise_host",
    "product_id_for",
    "resolve_host",
    "robots_url",
    "robots_verdict_for_status",
    "signed_fetch",
    "snapshot_ref",
    "stable_id",
    "user_agent_token",
    "variant_id_for",
]
