"""The snapshot -> graph-upsert mapping **both** catalog adapters share (T-023, C6).

SPEC C6 puts catalog ingestion behind one adapter interface, and T-023 acceptance 2 requires
the MCP adapter's "mapping to graph upserts" to *match ``signed_fetch`` semantics*. Two
implementations of the same mapping cannot be relied on to match — they match on the day they
are written and drift on the next change to either — so the mapping exists once, here, and
both :class:`~ingest.adapters.signed_fetch.SignedFetchAdapter` and
:class:`~ingest.adapters.catalog_mcp.CatalogMCPAdapter` call it. "Matches" is then structural
rather than a claim a test has to keep re-proving.

Everything in this module is **pure**: no network, no clock, no session, no randomness. The
only inputs are the snapshot and the caller's provenance identity, so the same snapshot always
produces the same ops in the same order. That is what lets ``to_upserts`` be asserted
byte-for-byte and what makes "unchanged content produced zero work" checkable by counting.

Two identity rules live here rather than in either adapter, because they are what makes the
seam a seam:

``product_id_for`` / ``variant_id_for``
    A product read over MCP and the same product read off the storefront resolve to the *same*
    graph node, because both IDs are derived from ``(store_id, the store's own identifier)``
    and nothing else. If each adapter minted its own IDs, ingesting one store through both
    adapters would silently double every product in the graph.

``composite_hash``
    A product's change-detection digest covers every surface it was built from. The parts are
    joined with a separator rather than concatenated so that a one-surface adapter and a
    two-surface adapter cannot collide on the same digest by accident.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit

from ..graph.model import Category, Offer, Product, Source, Store, Variant
from .base import CatalogSnapshot, UpsertOp
from .hashing import content_hash

__all__ = [
    "AVAILABILITY_VOCABULARY",
    "build_upserts",
    "catalog_source",
    "coerce_availability",
    "coerce_price",
    "composite_hash",
    "native_key",
    "native_product_key",
    "price_is_stated",
    "product_id_for",
    "safe_host",
    "safe_split",
    "stable_id",
    "variant_id_for",
]

#: schema.org / storefront availability tokens -> the vocabulary `ingest.graph` stores on an
#: Offer. One table for every adapter: a second copy would drift, and an availability string
#: the graph does not recognise is indistinguishable downstream from "we never looked".
AVAILABILITY_VOCABULARY: dict[str, str] = {
    "instock": "in_stock",
    "in_stock": "in_stock",
    "onlineonly": "in_stock",
    "available": "in_stock",
    "limitedavailability": "limited",
    "limited": "limited",
    "outofstock": "out_of_stock",
    "out_of_stock": "out_of_stock",
    "soldout": "out_of_stock",
    "discontinued": "discontinued",
    "preorder": "preorder",
    "presale": "preorder",
    "backorder": "backorder",
}


def coerce_availability(value: Any) -> str:
    """Fold a store's availability token into the graph's closed vocabulary.

    Anything unrecognised becomes ``"unknown"`` rather than passing through: a free-text
    availability string stored on an Offer would be read downstream as a state nobody handles.
    """
    if isinstance(value, bool):
        return "in_stock" if value else "out_of_stock"
    token = str(value or "").strip().lower().rsplit("/", 1)[-1].replace(" ", "")
    return AVAILABILITY_VOCABULARY.get(token, "unknown")


def coerce_price(value: Any) -> float | None:
    """Read a price out of untrusted store input, or ``None`` when there is no usable one.

    ``None`` means "no offer" — :func:`build_upserts` emits an ``Offer`` only for a variant
    that has a price, so a refused value costs the offer rather than poisoning the graph.

    Two classes of input are refused rather than coerced, and both are reachable from a store
    that simply serves what it likes (C10):

    * **Non-finite.** ``json`` parses the bare tokens ``NaN``, ``Infinity`` and ``-Infinity``
      by default, and ``float("nan")`` accepts the string form too. A NaN price compares false
      against *everything*, so one NaN offer silently corrupts every price filter, sort and
      price-honoured trust observation that touches it — and it never raises.
    * **Negative.** A negative price is not a discount, it is nonsense that would win any
      cheapest-offer ranking outright.

    Booleans are refused too: ``float(True)`` is ``1.0``, and a store that serves
    ``"price": true`` should not be quoted a price of one.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if price != price or price in (float("inf"), float("-inf")):  # NaN, ±Infinity
        return None
    if price < 0:
        return None
    return price


def price_is_stated(value: Any) -> bool:
    """Whether the store said anything at all in this price field.

    :func:`coerce_price` answers ``None`` for two situations a consumer must not confuse:
    *"there is no price here"* and *"there is a price here and it is unusable"*. A merge that
    reads both as "not stated" lets a store choose which of its surfaces prices a product by
    making the other one unusable — write ``-5.00`` in ``products.json`` and the theme's
    JSON-LD wins, which is the opposite of the documented precedence.

    Stated means the field carries something. Absent, JSON ``null`` and whitespace are the
    three ways a store says nothing; a hostile number, a boolean and unparseable text are all
    *statements*, and a statement we refuse costs the offer rather than promoting another
    surface's number in its place.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def safe_split(url: Any) -> SplitResult | None:
    """``urlsplit`` that answers ``None`` instead of raising on a URL that will not parse.

    ``urlsplit("http://[")`` raises ``ValueError: Invalid IPv6 URL``, and the strings this
    package splits include ones a *merchant* chose (a product's ``online_store_url``) and ones
    a caller passed in. An adapter whose contract is "a bad read is a warning, never an
    exception" cannot have a store-supplied string that aborts the whole run.
    """
    try:
        return urlsplit(str(url or ""))
    except ValueError:
        return None


def safe_host(url: Any) -> str:
    """The lower-cased hostname of ``url``, or ``""`` when there is not one to be had."""
    split = safe_split(url)
    return (split.hostname or "").lower() if split is not None else ""


def stable_id(*parts: Any) -> str:
    """A deterministic 32-hex digest over ``parts``, separated so they cannot run together.

    ``("ab", "c")`` and ``("a", "bc")`` must not hash alike: the unit separator between parts
    is what keeps two different identities from colliding into one graph node.
    """
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def composite_hash(*parts: str) -> str:
    """The digest of everything one record was built from, in order.

    A missing surface is an empty part rather than an omitted one, so "catalog entry, no page"
    and "catalog entry, page that hashed to nothing" stay distinguishable.
    """
    return content_hash("|".join("" if p is None else str(p) for p in parts))


def native_key(value: Any) -> str:
    """One store-supplied identifier, reduced to the form every adapter agrees on.

    Shopify states the *same* identifier two ways depending on which surface you read:
    ``products.json`` serves ``8123456`` and the catalog MCP server serves
    ``gid://shopify/Product/8123456``. Both name one product, so both have to reduce to one
    key — otherwise ingesting a store through both adapters writes two ``Product`` nodes for
    every product it sells, each supported by its own ``Source``, and nothing downstream can
    tell they are the same thing. A GID's identity is its final path segment; a query string
    or fragment is not part of it.

    Booleans are refused rather than stringified: ``"True"`` is not an identifier, and letting
    one through would give every such record the same node.
    """
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if text.lower().startswith("gid://"):
        tail = text.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1].strip()
        return tail or text
    return text


def native_product_key(entry: Mapping[str, Any]) -> str:
    """The store's own identifier for a product, whatever field it publishes it under.

    ``products.json`` and the MCP catalog both call it ``id``; a store that publishes neither
    is identified by its ``handle``.
    """
    for key in ("id", "product_id", "handle"):
        text = native_key(entry.get(key))
        if text:
            return text
    return ""


def product_id_for(store_id: str, entry: Mapping[str, Any]) -> str:
    """The graph's stable ID for a storefront product.

    Derived from the store and the store's own identifier for the product, so the same product
    keeps the same node across crawls (a re-crawl of unchanged content cannot churn the graph
    through ID drift) **and** across adapters (C6: one seam, one node).

    An entry that names *no* identifier is refused rather than hashed. The empty key is one
    value, so hashing it gives every unidentifiable entry from a store the **same** ``prod_``
    id: two different products merge into one graph node, and a store's whole unnamed catalog
    collapses into a single product, silently, on the first real write. Both adapters skip such
    an entry with a warning before reaching here; this raise is what stops a third one from
    reintroducing the collision by forgetting to.

    Raises:
        ValueError: ``entry`` names no ``id``, ``product_id`` or ``handle``.
    """
    native = native_product_key(entry)
    if not native:
        raise ValueError(
            "product_id_for needs the store's own identifier: an entry naming no id, "
            "product_id or handle would hash the empty key, and every such entry from this "
            "store would land on that one product node"
        )
    return f"prod_{stable_id(store_id, native)}"


def variant_id_for(store_id: str, native_product: str, native_variant: str) -> str:
    """The graph's stable ID for one purchasable variant of one store's product."""
    return f"var_{stable_id(store_id, native_product, native_variant)}"


def catalog_source(
    url: str,
    digest: str,
    observed_at: str,
    extractor_version: str,
    *,
    confidence: float = 0.9,
    source_class: str = "scraped",
) -> Source:
    """A ``Source`` node for one observed catalog surface — DESIGN: ``Source`` is Provenance.

    ``source_id`` is derived from the URL and the digest, so re-observing unchanged content
    produces the *same* Source rather than a new one every run.

    ``source_class`` stays ``"scraped"`` for both adapters on purpose. D29 routes the class to
    a buyer-facing label — ``scraped`` reads "from their website", while ``seller_asserted``
    carries no label and diverts the fact into the R18 verification queue. A catalog we read
    ourselves, whether off the storefront or out of the catalog MCP server, is an observation
    of the store's own published catalog, not a claim the seller submitted to us; classing the
    MCP read as ``seller_asserted`` would silently unlabel every MCP-sourced product.
    """
    return Source(
        source_id=f"src_{stable_id(url, digest)}",
        url=url,
        content_hash=digest,
        observed_at=observed_at,
        extractor_version=extractor_version,
        confidence=confidence,
        source_class=source_class,
    )


def build_upserts(
    snapshot: CatalogSnapshot,
    *,
    extractor_version: str = "",
    source_class: str = "scraped",
) -> list[UpsertOp]:
    """Map a catalog snapshot to ordered graph writes. Pure — no I/O, no clock.

    Only ``snapshot.changed_products`` are visited, which is where the "unchanged content
    produces zero re-extraction work" guarantee actually lives: for a run whose every hash
    matched, this returns ``[]`` without inspecting a single product.

    Order is dependency order and is load-bearing — ``ingest.graph.upsert`` refuses to attach
    an ``Offer`` to a ``Variant`` that does not exist yet rather than creating a placeholder,
    so the store precedes its products, a product precedes its variants, and a variant
    precedes its offer.

    Args:
        snapshot: what the adapter observed.
        extractor_version: the fallback provenance version, used only when the snapshot does
            not carry one of its own. The snapshot's value wins so that the provenance names
            the adapter that actually did the reading.
        source_class: the provenance class for every ``Source`` minted here.

    Raises:
        ValueError: neither the snapshot nor the caller named an extractor version, which
            would produce a ``Source`` that cannot be traced back to any code.
    """
    changed = snapshot.changed_products
    if not changed:
        return []

    version = str(snapshot.extractor_version or "").strip() or str(extractor_version or "").strip()
    if not version:
        raise ValueError(
            "build_upserts needs an extractor version: a Source that cannot name the code "
            "that produced it satisfies the provenance audit without providing provenance"
        )

    store_source = catalog_source(
        snapshot.base_url,
        content_hash(snapshot.base_url),
        snapshot.observed_at,
        version,
        confidence=1.0,
        source_class=source_class,
    )
    ops: list[UpsertOp] = [
        UpsertOp(
            kind="store",
            node=Store(
                store_id=snapshot.store_id,
                domain=safe_host(snapshot.base_url),
            ),
            source=store_source,
        )
    ]

    for product in changed:
        source = catalog_source(
            product.source_url or snapshot.base_url,
            product.content_hash or content_hash(product.product_id),
            snapshot.observed_at,
            version,
            source_class=source_class,
        )
        ops.append(
            UpsertOp(
                kind="product",
                node=Product(
                    product_id=product.product_id,
                    canonical_name=product.canonical_name,
                    brand=product.brand,
                    status=product.status,
                ),
                source=source,
            )
        )
        ops.append(
            UpsertOp(
                kind="sells",
                node=None,
                source=source,
                context={"store_id": snapshot.store_id, "product_id": product.product_id},
            )
        )
        for name in product.categories:
            ops.append(
                UpsertOp(
                    kind="category",
                    node=Category(name=name),
                    source=source,
                    context={"product_id": product.product_id},
                )
            )
        for variant in product.variants:
            ops.append(
                UpsertOp(
                    kind="variant",
                    node=Variant(
                        variant_id=variant.variant_id,
                        seller_sku=variant.seller_sku,
                        name=variant.name,
                        status=variant.status,
                    ),
                    source=source,
                    context={"product_id": product.product_id},
                )
            )
            if variant.price is None:
                continue
            ops.append(
                UpsertOp(
                    kind="offer",
                    node=Offer(
                        offer_id=f"off_{stable_id(snapshot.store_id, variant.variant_id)}",
                        price=float(variant.price),
                        currency=variant.currency,
                        availability=variant.availability,
                        observed_at=snapshot.observed_at,
                    ),
                    source=source,
                    context={
                        "store_id": snapshot.store_id,
                        "variant_id": variant.variant_id,
                    },
                )
            )
    return ops
