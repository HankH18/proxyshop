"""The `CatalogAdapter` seam and the records that cross it (T-020, SPEC C6, DESIGN §Architecture).

C6 says catalog ingestion sits behind **one** adapter interface. DESIGN names two
implementations — ``catalog_mcp`` (production primary) and ``signed_fetch`` (the page
fetcher that reaches password-protected dev stores, A1) — and the point of the seam is that
everything downstream of ingestion is written against the interface, never against either
implementation.

The interface is deliberately two methods, because catalog ingestion is deliberately two
separable steps:

``fetch_catalog(request)``
    Talk to the store. Everything network-shaped, source-specific and untrusted lives here:
    HTTP or MCP, authentication, pagination, robots, budgets. Returns a
    :class:`CatalogSnapshot` — plain data, already hashed, with provenance attached.

``to_upserts(snapshot)``
    Talk to the graph. Pure: no I/O, no clock, no network. Turns a snapshot into an ordered
    list of :class:`UpsertOp`, which :func:`apply_upserts` replays against Neo4j.

Splitting them is what makes the seam testable and what makes change detection cheap.
Because ``to_upserts`` is pure and driven only by the snapshot, a snapshot whose resources
all came back unchanged produces an **empty** op list without any further thought — the
"unchanged content does zero work" guarantee is structural rather than a special case
someone has to remember to code.

Everything a snapshot carries is data the store said, and is therefore **untrusted** (C10).
Nothing in this module interpolates fetched text into a query, a prompt or a URL.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..graph.model import PolicyPage, Source
from .budgets import BudgetUsage, CrawlBudget
from .netguard import FetchPolicy

__all__ = [
    "CatalogAdapter",
    "CatalogRequest",
    "CatalogSnapshot",
    "FetchedResource",
    "ImageRecord",
    "ProductRecord",
    "UpsertOp",
    "VariantRecord",
    "apply_upserts",
]


@dataclass(frozen=True)
class CatalogRequest:
    """What to ingest, and under what limits.

    Attributes:
        store_id: the graph's stable ID for this store.
        base_url: the storefront root, e.g. ``https://dev-store.example.com``.
        storefront_password: the dev-store password (A1). ``None`` for a public store.
        allowed_hosts: extra hosts a redirect may reach. The base URL's own host is always
            allowed; anything else has to be named here.
        known_hashes: what was seen last time, as produced by
            :attr:`CatalogSnapshot.hash_index`. Two key shapes share the mapping and must
            not collide: a bare **URL** keys a fetched resource's own body digest, and
            ``"product:<product_id>"`` keys a product's *composite* digest (its catalog
            entry plus its page). They are namespaced apart because a product's page URL is
            also a resource URL, and hashing the same key to two different values is how a
            change-detector silently reports everything as changed forever. Pass the
            previous run's ``hash_index`` straight back in.
        budget: crawl ceilings.
        policy: the SSRF posture. The default refuses everything not publicly routable.
        max_products: stop after this many products even if the budget would allow more.
        fetch_product_pages: whether to follow each product to its HTML page for JSON-LD.
    """

    store_id: str
    base_url: str
    storefront_password: str | None = None
    allowed_hosts: tuple[str, ...] = ()
    known_hashes: Mapping[str, str] = field(default_factory=dict)
    budget: CrawlBudget = field(default_factory=CrawlBudget)
    policy: FetchPolicy = field(default_factory=FetchPolicy)
    max_products: int = 250
    fetch_product_pages: bool = True


@dataclass(frozen=True)
class FetchedResource:
    """One URL as it was observed, with the digest that decides whether it is new."""

    url: str
    status: int
    content_hash: str
    media_type: str = ""
    changed: bool = True
    snapshot_ref: str = ""
    bytes_downloaded: int = 0
    redirect_chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class VariantRecord:
    """A purchasable variant plus the offer terms observed for it."""

    variant_id: str
    seller_sku: str = ""
    name: str = ""
    price: float | None = None
    currency: str = "USD"
    availability: str = "unknown"
    status: str = "active"


@dataclass(frozen=True)
class ImageRecord:
    """One image the catalogue published a **record** of. URL metadata, never bytes.

    The scraper used to read ``title``, ``price``, ``sku`` and ``variants`` and drop
    ``images[]`` on the floor, so a shortlist slot had nothing to show and the owner's
    verified-primary media rule had no inputs to be decided from. This record is what
    survives the crawl now.

    **Nothing here is fetched.** ``src`` is copied out of the catalogue verbatim and is
    untrusted store input like every other field on a snapshot (C10); ``width``/``height``
    are what the *seller claims*, not measurements. Ingest deliberately opens no connection
    to an image host — see :class:`~ingest.graph.model.MediaAsset` for the two legs of the
    verified check that a fetch, and only a fetch, can close.

    Attributes:
        native_id: the store's own identifier for the image, when it publishes one. Falls
            back to the URL, so a store that numbers nothing still gets stable asset ids.
        src: the image URL, exactly as published — cache-buster query and all, because
            ``?v=1754936059`` is part of *which version* the catalogue claimed.
        position: 1-based gallery position. ``1`` is the primary image.
        alt: merchant-authored alt text.
        width / height: the pixel dimensions the catalogue states; ``0`` when it does not.
        updated_at: the seller's own last-modified stamp for this image record, when
            published. Part of :attr:`catalogue_digest_parts` because an edit that keeps the
            URL and changes the file is otherwise invisible.
    """

    native_id: str
    src: str
    position: int = 0
    alt: str = ""
    width: int = 0
    height: int = 0
    updated_at: str = ""

    @property
    def catalogue_digest_parts(self) -> tuple[str, ...]:
        """The parts of the record a downstream verifier's pin is taken over.

        Everything the seller states about the file itself, and nothing about where it sits
        in the gallery: reordering a gallery does not change any image, so a reorder must
        not expire a verifier's byte pin.
        """
        return (
            str(self.src),
            str(int(self.width)),
            str(int(self.height)),
            str(self.updated_at or ""),
        )


@dataclass(frozen=True)
class ProductRecord:
    """A product as the storefront presented it. Every field is untrusted store input."""

    product_id: str
    canonical_name: str
    brand: str = ""
    status: str = "active"
    handle: str = ""
    source_url: str = ""
    content_hash: str = ""
    changed: bool = True
    variants: tuple[VariantRecord, ...] = ()
    categories: tuple[str, ...] = ()
    attributes: Mapping[str, str] = field(default_factory=dict)
    #: The product's gallery, already bounded by the adapter — see
    #: :data:`~ingest.adapters.mapping.MEDIA_PER_PRODUCT_LIMIT`. Ordered by the catalogue's
    #: own ``position`` so the primary image is first.
    images: tuple[ImageRecord, ...] = ()


@dataclass(frozen=True)
class CatalogSnapshot:
    """The result of one ingestion run: plain, hashed, provenance-carrying data."""

    store_id: str
    base_url: str
    observed_at: str
    adapter: str
    products: tuple[ProductRecord, ...] = ()
    resources: tuple[FetchedResource, ...] = ()
    policy_pages: tuple[PolicyPage, ...] = ()
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    robots_txt: str = ""
    extractor_version: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def changed_products(self) -> tuple[ProductRecord, ...]:
        """Only the products whose content differs from what was last seen."""
        return tuple(p for p in self.products if p.changed)

    @property
    def unchanged_urls(self) -> tuple[str, ...]:
        return tuple(r.url for r in self.resources if not r.changed)

    @property
    def hash_index(self) -> dict[str, str]:
        """Everything this run observed, keyed for the next run's ``known_hashes``.

        Feeding this back into the next :class:`CatalogRequest` is the whole change-
        detection contract; building the map by hand is how the two key namespaces get
        confused, so callers should not.
        """
        index = {resource.url: resource.content_hash for resource in self.resources}
        index.update(
            {f"product:{product.product_id}": product.content_hash for product in self.products}
        )
        return index


@dataclass(frozen=True)
class UpsertOp:
    """One graph write, described rather than performed.

    Keeping these as data — instead of having the adapter hold a Neo4j session — is what
    lets ``to_upserts`` stay pure, lets a caller inspect or count the work before doing
    any of it, and lets "unchanged content produced zero work" be asserted directly.
    """

    kind: str
    source: Source
    node: Any = None
    context: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class CatalogAdapter(Protocol):
    """The one interface every catalog source implements (C6).

    Deliberately minimal. Anything an implementation needs that is specific to its
    transport — a storefront password, an MCP session, a recorded-fixture directory —
    is constructor state, not a parameter here, so that callers downstream of ingestion
    can hold a ``CatalogAdapter`` and know nothing else.
    """

    def fetch_catalog(self, request: CatalogRequest) -> CatalogSnapshot:
        """Read the store's catalog under the request's budgets and guards."""
        ...

    def to_upserts(self, snapshot: CatalogSnapshot) -> list[UpsertOp]:
        """Map a snapshot to graph writes. Pure: no network, no clock, no session."""
        ...


def apply_upserts(session: Any, ops: Iterable[UpsertOp]) -> list[str]:
    """Replay :class:`UpsertOp` records against a Neo4j session, in order.

    Order matters and is the caller's (i.e. ``to_upserts``') responsibility: an ``Offer``
    cannot be attached before its ``Store`` and ``Variant`` exist, and ``ingest.graph``
    enforces that with :class:`~ingest.graph.upsert.ProvenanceRequired` rather than
    creating a placeholder node behind your back.

    Returns:
        The stable IDs written, in the order they were written.
    """
    from ..graph import upsert as graph_upsert

    written: list[str] = []
    for op in ops:
        context = dict(op.context or {})
        if op.kind == "store":
            written.append(graph_upsert.upsert_store(session, op.node, source=op.source))
        elif op.kind == "product":
            written.append(graph_upsert.upsert_product(session, op.node, source=op.source))
        elif op.kind == "variant":
            written.append(
                graph_upsert.upsert_variant(
                    session, op.node, product_id=context.get("product_id"), source=op.source
                )
            )
        elif op.kind == "offer":
            written.append(
                graph_upsert.upsert_offer(
                    session,
                    op.node,
                    store_id=context["store_id"],
                    variant_id=context["variant_id"],
                    source=op.source,
                )
            )
        elif op.kind == "policy_page":
            written.append(graph_upsert.upsert_policy_page(session, op.node, source=op.source))
        elif op.kind == "sells":
            graph_upsert.link_sells(
                session,
                store_id=context["store_id"],
                product_id=context["product_id"],
                source=op.source,
            )
        elif op.kind == "category":
            written.append(
                graph_upsert.link_category(
                    session,
                    product_id=context["product_id"],
                    category=op.node,
                    source=op.source,
                )
            )
        elif op.kind == "attribute":
            written.append(
                graph_upsert.upsert_attribute(
                    session,
                    product_id=context["product_id"],
                    attribute=op.node,
                    source=op.source,
                )
            )
        elif op.kind == "media":
            written.append(
                graph_upsert.upsert_media_asset(
                    session,
                    product_id=context["product_id"],
                    asset=op.node,
                    source=op.source,
                )
            )
        else:
            raise ValueError(f"unknown UpsertOp kind: {op.kind!r}")
    return written
