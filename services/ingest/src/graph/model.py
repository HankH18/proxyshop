"""The Neo4j catalog model — labels, edge types, node payloads and stable IDs (T-012).

Transcribed from ``DESIGN.md`` §Data models ("**Neo4j** (partner attribute-node model
adopted + retrieval layer)"), which is the reconciled model six other tickets (T-020…T-024,
T-031) write into and read from. Three of its clauses are load-bearing and are enforced
mechanically rather than by convention:

* **Uniqueness constraints on every stable ID.** Every label below names exactly one
  ``id_property``; :mod:`ingest.graph.schema` turns that list into constraints. Entity
  resolution (T-022) has nothing to stand on without it.
* **``SUPPORTED_BY`` → ``Source`` from every material fact.** See
  :data:`MATERIAL_FACT_LABELS` / :data:`MATERIAL_FACT_EDGES` and
  :func:`ingest.graph.upsert.provenance_violations`.
* **Never match products by free-text name alone.** Nothing here exposes a name lookup;
  :mod:`ingest.graph.query` retrieves by vector and by attribute node, and refuses a
  request that carries neither.

Stable IDs that DESIGN does not spell out
-----------------------------------------
DESIGN gives explicit id properties for ``Store``, ``Product``, ``Variant``, ``Offer`` and
``Source``, and names ``Category``, ``AttributeValue``, ``Ingredient``, ``PolicyPage`` and
``IntentCluster`` without one. A uniqueness constraint needs a property, so this module
fixes the missing five: ``category_id``, ``attr_id``, ``ingredient_id``, ``page_id``,
``cluster_id`` — and ``asset_id`` for :class:`MediaAsset`, which DESIGN does not name at
all because the crawl used to throw ``images[]`` away. ``attr_id`` and ``ingredient_id`` and ``category_id`` are **content
hashes** (:func:`attribute_value_id`, :func:`ingredient_id`, :func:`category_id`) rather
than free identifiers, so two adapters that observe "SPF 50" independently converge on one
``AttributeValue`` node — which is what makes the attribute half of the candidate query a
graph traversal instead of a string scan.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------------------
# Labels, edges and stable IDs
# ---------------------------------------------------------------------------------------

#: label -> the single property carrying that label's stable ID. Every entry becomes a
#: uniqueness constraint in :mod:`ingest.graph.schema`; the list is the schema.
ID_PROPERTY: dict[str, str] = {
    "Store": "store_id",
    "Product": "product_id",
    "Variant": "variant_id",
    "Offer": "offer_id",
    "Category": "category_id",
    "AttributeValue": "attr_id",
    "Ingredient": "ingredient_id",
    "MediaAsset": "asset_id",
    "PolicyPage": "page_id",
    "Source": "source_id",
    "IntentCluster": "cluster_id",
}

#: Nodes that *assert* something observed about the world. Every one of these must carry at
#: least one ``-[:SUPPORTED_BY]->(:Source)`` edge; :func:`ingest.graph.upsert.
#: provenance_violations` reports any that does not.
MATERIAL_FACT_LABELS: frozenset[str] = frozenset(
    {"Store", "Product", "Variant", "Offer", "AttributeValue", "MediaAsset", "PolicyPage"}
)

#: Nodes that are *vocabulary*, not observation: a category or an ingredient is a term, and
#: the observed fact is a product's attachment to it. Those attachments are edges, and
#: edges carry their provenance as :data:`SOURCE_ID_PROPERTY` (see below).
#:
#: THE INVARIANT THAT MAKES THIS SAFE, and the one a future ticket can break: a vocabulary
#: node's properties are **derived from the term itself and nothing else**. Provenancing the
#: term would be actively worse — it would attribute a shared node to whichever adapter
#: happened to mint it first. But the moment someone adds an *observed* property
#: (``Ingredient.is_allergen``, ``Category.regulated_in``, ``IntentCluster.observed_volume``)
#: a real-world claim lands here unsourced and :func:`ingest.graph.upsert.provenance_violations`
#: stays green, because these labels are excluded from the node audit by construction.
#: Adding an observed property means moving the label into :data:`MATERIAL_FACT_LABELS`, not
#: widening the dataclass. ``test_graph.py`` pins each vocabulary node's property set exactly
#: so that widening one is a deliberate, visible act.
VOCABULARY_LABELS: frozenset[str] = frozenset({"Category", "Ingredient", "IntentCluster"})

#: The provenance edge itself. Never a material fact — it *is* the provenance.
SUPPORTED_BY = "SUPPORTED_BY"

#: Relationship-shaped material facts, each with its ``(from_label, to_label)`` endpoints.
#:
#: Neo4j has no relationship-on-a-relationship, so a fact that is an edge cannot literally
#: grow a ``SUPPORTED_BY`` edge of its own. Its provenance is therefore the
#: :data:`SOURCE_ID_PROPERTY` written onto the edge, which must resolve to an existing
#: ``Source`` node — checked by the same audit that checks the node half, so the
#: requirement is enforced uniformly even though its two representations differ.
MATERIAL_FACT_EDGES: dict[str, tuple[str, str]] = {
    "SELLS": ("Store", "Product"),
    "MAKES_OFFER": ("Store", "Offer"),
    "FOR": ("Offer", "Variant"),
    "HAS_VARIANT": ("Product", "Variant"),
    "IN_CATEGORY": ("Product", "Category"),
    "HAS_ATTRIBUTE": ("Product", "AttributeValue"),
    "HAS_MEDIA": ("Product", "MediaAsset"),
    "CONTAINS": ("Product", "Ingredient"),
    "COMPATIBLE_WITH": ("Product", "Product"),
    "SAME_AS": ("Product", "Product"),
    "STATES": ("PolicyPage", "AttributeValue"),
}

#: The property carrying an edge's provenance, and the reason a raw ``MERGE`` of any edge in
#: :data:`MATERIAL_FACT_EDGES` without it is a defect the audit reports.
SOURCE_ID_PROPERTY = "source_id"

#: The property the D6 vector index is built on: ``Product.embedding``.
EMBEDDING_PROPERTY = "embedding"

#: D6's vector width. Lives here rather than in :mod:`ingest.graph.schema` so that
#: :mod:`ingest.graph.upsert` can validate a vector's length without importing the schema
#: module, and so there is exactly one literal ``1024`` in the package.
EMBEDDING_DIMENSIONS = 1024

#: ``Provenance.source`` (DESIGN §Interfaces): the closed vocabulary ``Source.source_class``
#: carries. Mirrored here rather than imported from ``packages/contracts`` so that the graph
#: library does not take a build-order dependency on the generated schema package; the
#: pairing is asserted in ``services/ingest/tests/test_graph.py`` if that package is present.
SOURCE_CLASSES: frozenset[str] = frozenset(
    {
        "scraped",
        "pixel_feed",
        "owner_statement",
        "envelope_rule",
        "learned_policy",
        "network",
        "seller_asserted",
    }
)


# ---------------------------------------------------------------------------------------
# Canonicalisation and content-hashed IDs
# ---------------------------------------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def canonical_text(value: str) -> str:
    """Fold ``value`` to the form the content hashes are taken over.

    NFKC-normalised, case-folded and whitespace-collapsed, so ``"SPF  50"``, ``"spf 50"``
    and ``"SPF 50"`` are one attribute value rather than three.

    Args:
        value: raw observed text.

    Returns:
        The canonical form.
    """
    folded = unicodedata.normalize("NFKC", value).casefold().strip()
    return _WHITESPACE.sub(" ", folded)


def slug(value: str) -> str:
    """A URL-safe slug of ``value``, used as the human-readable half of a vocabulary ID.

    Args:
        value: raw observed text.

    Returns:
        Lowercase ``a-z0-9`` runs joined by ``-``; ``"_"`` when nothing survives.
    """
    stripped = _NON_SLUG.sub("-", canonical_text(value)).strip("-")
    return stripped or "_"


def _digest(*parts: str) -> str:
    """A stable 32-hex-char digest over ``parts``, unambiguously delimited.

    Args:
        *parts: the components of the identity, in a fixed order.

    Returns:
        The first 32 hex characters of ``SHA-256`` over the ``\\x1f``-joined parts. The unit
        separator cannot occur in the canonicalised inputs, so ``("a", "bc")`` and
        ``("ab", "c")`` can never collide by concatenation.
    """
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def category_id(name: str, *, parent: str | None = None) -> str:
    """The stable ID of a category term.

    Args:
        name: the category name as observed.
        parent: the parent category name, when the taxonomy is nested.

    Returns:
        ``cat_<slug>_<digest>`` — readable in a query result, still collision-safe.
    """
    return f"cat_{slug(name)}_{_digest('category', canonical_text(parent or ''), canonical_text(name))}"


def ingredient_id(name: str) -> str:
    """The stable ID of an ingredient term.

    Args:
        name: the ingredient name as observed (e.g. ``"Niacinamide"``).

    Returns:
        ``ing_<slug>_<digest>``.
    """
    return f"ing_{slug(name)}_{_digest('ingredient', canonical_text(name))}"


def _number_token(value: float | int | None) -> str:
    """Render a number for hashing so that ``50`` and ``50.0`` are one identity.

    Args:
        value: the numeric attribute value, or ``None``.

    Returns:
        A canonical decimal string, or ``""`` for ``None``.

    Raises:
        ValueError: ``value`` is NaN or infinite — neither is a catalog fact, and both
            would make the ID unstable (``NaN != NaN``).
    """
    if value is None:
        return ""
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"attribute value_number must be finite, got {value!r}")
    if number == int(number):
        return str(int(number))
    return repr(number)


def attribute_value_id(
    key: str,
    *,
    value_string: str | None = None,
    value_number: float | None = None,
    value_bool: bool | None = None,
    unit: str | None = None,
) -> str:
    """The stable ID of an ``AttributeValue`` node, as a content hash.

    Two adapters that independently observe "SPF 50" must land on the **same** node, or the
    attribute half of the candidate query degenerates into a scan over per-product private
    nodes. So the identity is the content, not an allocated key.

    Args:
        key: the attribute key, e.g. ``"spf"``.
        value_string: the string-valued reading, if any.
        value_number: the numeric reading, if any.
        value_bool: the boolean reading, if any.
        unit: the unit of ``value_number``, if any.

    Returns:
        ``av_<slug(key)>_<digest>``.

    Raises:
        ValueError: ``key`` is blank, no value component was supplied, or ``value_number``
            is not finite.
    """
    if not key or not key.strip():
        raise ValueError("AttributeValue.key must be non-empty")
    if value_string is None and value_number is None and value_bool is None:
        raise ValueError(
            f"AttributeValue {key!r} carries no value: give value_string, value_number "
            f"or value_bool"
        )
    bool_token = "" if value_bool is None else ("true" if value_bool else "false")
    # THE DIGEST HASHES THE SAME FOLD THE PREFIX SHOWS. It used to hash
    # `canonical_text(key)` while the prefix rendered `slug(key)`, and the two folds do not
    # agree on separators: "fragrance_free" and "fragrance free" both render the prefix
    # `av_fragrance-free_` and produced *different* digests, so one fact became two nodes
    # and an attribute filter partitioned the catalog by separator. The case/whitespace half
    # converged correctly, which is why only a same-prefix/different-id assertion catches it.
    return "av_{}_{}".format(
        slug(key),
        _digest(
            "attribute",
            slug(key),
            canonical_text(value_string or ""),
            _number_token(value_number),
            bool_token,
            canonical_text(unit or ""),
        ),
    )


# ---------------------------------------------------------------------------------------
# What counts as a legitimate embedding vector
# ---------------------------------------------------------------------------------------


class InvalidEmbeddingVector(ValueError):
    """A vector that the ``product_embedding`` index would store and then never return."""


def embedding_vector_defect(
    vector: Sequence[float], *, dimensions: int = EMBEDDING_DIMENSIONS
) -> tuple[str, str] | None:
    """The single definition of "a legitimate embedding vector", shared by every entry point.

    One definition, in one place, because there are two doors into the vector index — the
    write side (:func:`ingest.graph.upsert.set_product_embedding`) and the read side
    (:func:`ingest.graph.query.candidate_products`) — and each had a *different* idea of what
    it would accept. The write side checked only the length, so ``hash_embed("")``, which is
    exactly the all-zero 1024-d vector, was stored without complaint and the product then
    vanished from every vector query permanently while ``products_missing_embeddings()``
    still reported ``[]``. The read side checked nothing at all.

    Three components, and the third is not ``not any(vector)``. A vector of 1024 components
    of ``1e-200`` is non-zero by ``any()``, but every square underflows to ``0.0`` and its L2
    norm is exactly ``0.0`` — cosine against it is a division by zero, which Neo4j resolves
    to "never matches" rather than to an error. So the norm is what is measured.

    Args:
        vector: the candidate vector.
        dimensions: the width the live index was built for.

    Returns:
        ``None`` when the vector is usable. Otherwise ``(kind, reason)`` where ``kind`` is
        ``"dimension"`` or ``"value"`` — the caller picks its exception from the kind — and
        ``reason`` is a phrase that completes "refusing a vector that …".
    """
    if len(vector) != dimensions:
        return (
            "dimension",
            f"is {len(vector)}-d, but the {EMBEDDING_PROPERTY} index is {dimensions}-d",
        )
    if not all(math.isfinite(float(component)) for component in vector):
        return ("value", "contains a non-finite component; NaN and inf have no cosine")
    norm = math.sqrt(math.fsum(float(component) ** 2 for component in vector))
    if norm <= 0.0:
        return (
            "value",
            f"has an L2 norm of {norm}; a zero-length vector is orthogonal to everything, so "
            f"the index would store it and then never return it from any query",
        )
    return None


# ---------------------------------------------------------------------------------------
# Node payloads
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """``Source{source_id, url, content_hash, observed_at, extractor_version, confidence,
    source_class}`` — DESIGN's ``Source ≡ our Provenance``.

    Every upsert in :mod:`ingest.graph.upsert` takes one of these as a **required**
    keyword-only argument. That is the enforcement point for "every material fact upserts
    with a ``SUPPORTED_BY`` ``Source``": there is no code path through this library that
    writes a fact without naming where it came from.
    """

    source_id: str
    url: str
    content_hash: str
    observed_at: str
    extractor_version: str
    confidence: float
    source_class: str

    def __post_init__(self) -> None:
        """Validate the closed vocabulary and the confidence range.

        A ``Source`` whose fields are all blank satisfies "every material fact has a
        Source" while pointing at nothing, which would make the whole provenance rule
        vacuous — an adapter could green the audit with ``Source("x", "", "", "", "", 0.0,
        "seller_asserted")``. So every field that makes the record *traceable* is required,
        not just the ones that make it well-formed.

        Raises:
            ValueError: ``source_class`` is outside :data:`SOURCE_CLASSES`, ``confidence``
                is outside ``[0, 1]``, or any of ``source_id`` / ``url`` /
                ``content_hash`` / ``observed_at`` / ``extractor_version`` is blank.
        """
        for field_name in ("source_id", "url", "content_hash", "observed_at", "extractor_version"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(
                    f"Source.{field_name} must be non-empty: a Source that points at nothing "
                    f"satisfies the provenance audit without providing any provenance"
                )
        if self.source_class not in SOURCE_CLASSES:
            raise ValueError(
                f"Source.source_class {self.source_class!r} is not one of "
                f"{sorted(SOURCE_CLASSES)} (DESIGN Provenance.source)"
            )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(f"Source.confidence must be in [0, 1], got {self.confidence!r}")

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write.

        Returns:
            A plain mapping suitable for ``SET s += $props``.
        """
        return {
            "source_id": self.source_id,
            "url": self.url,
            "content_hash": self.content_hash,
            "observed_at": self.observed_at,
            "extractor_version": self.extractor_version,
            "confidence": float(self.confidence),
            "source_class": self.source_class,
        }


@dataclass(frozen=True)
class Store:
    """``Store{store_id, domain, business_identity, tier}``."""

    store_id: str
    domain: str
    business_identity: str = ""
    tier: int = 2

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write."""
        return {
            "store_id": self.store_id,
            "domain": self.domain,
            "business_identity": self.business_identity,
            "tier": int(self.tier),
        }


@dataclass(frozen=True)
class Product:
    """``Product{product_id, canonical_name, brand, status, embedding}``.

    ``embedding`` is written by :func:`ingest.graph.upsert.set_product_embedding` or by the
    re-embed pass, never inline here: an upsert that carried a vector would make every
    caller responsible for choosing an :class:`~ingest.embeddings.EmbeddingProvider`, and
    D19's config-only swap depends on exactly one place making that choice.
    """

    product_id: str
    canonical_name: str
    brand: str = ""
    status: str = "active"

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write."""
        return {
            "product_id": self.product_id,
            "canonical_name": self.canonical_name,
            "brand": self.brand,
            "status": self.status,
        }


@dataclass(frozen=True)
class Variant:
    """``Variant{variant_id, seller_sku, name, status}``."""

    variant_id: str
    seller_sku: str = ""
    name: str = ""
    status: str = "active"

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write."""
        return {
            "variant_id": self.variant_id,
            "seller_sku": self.seller_sku,
            "name": self.name,
            "status": self.status,
        }


@dataclass(frozen=True)
class Offer:
    """``Offer{offer_id, price, currency, availability, observed_at}`` — the **graph**
    Offer (D25).

    D25 names three different objects "Offer". This is the third: a store's *observed
    listing* in the catalog graph. It is not the protocol ``Offer`` in
    ``packages/contracts`` (which carries ``total_price``, ``commitments``,
    ``checkout_url``, ``expires_at``) and it is not the ``app.offers`` table row. Do not
    give this one protocol fields.
    """

    offer_id: str
    price: float
    currency: str
    availability: str
    observed_at: str

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write."""
        return {
            "offer_id": self.offer_id,
            "price": float(self.price),
            "currency": self.currency,
            "availability": self.availability,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class Category:
    """``Category`` — a vocabulary term. ``category_id`` is a content hash of the name."""

    name: str
    parent: str | None = None
    category_id: str = field(default="")

    def __post_init__(self) -> None:
        """Derive :attr:`category_id` from the name when the caller did not pin one."""
        if not self.category_id:
            object.__setattr__(self, "category_id", category_id(self.name, parent=self.parent))

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write."""
        return {
            "category_id": self.category_id,
            "name": self.name,
            "slug": slug(self.name),
            "parent": self.parent or "",
        }


@dataclass(frozen=True)
class Ingredient:
    """``Ingredient`` — a vocabulary term. ``ingredient_id`` is a content hash of the name."""

    name: str
    ingredient_id: str = field(default="")

    def __post_init__(self) -> None:
        """Derive :attr:`ingredient_id` from the name when the caller did not pin one."""
        if not self.ingredient_id:
            object.__setattr__(self, "ingredient_id", ingredient_id(self.name))

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write."""
        return {
            "ingredient_id": self.ingredient_id,
            "name": self.name,
            "canonical_name": canonical_text(self.name),
        }


@dataclass(frozen=True)
class AttributeValue:
    """``AttributeValue{key, value_string, value_number, value_bool, unit}``.

    Shared across every product that carries the same reading — see
    :func:`attribute_value_id` for why that matters.
    """

    key: str
    value_string: str | None = None
    value_number: float | None = None
    value_bool: bool | None = None
    unit: str | None = None
    attr_id: str = field(default="")

    def __post_init__(self) -> None:
        """Derive :attr:`attr_id` from the content when the caller did not pin one.

        Raises:
            ValueError: the key is blank or no value component was supplied.
        """
        if not self.attr_id:
            object.__setattr__(
                self,
                "attr_id",
                attribute_value_id(
                    self.key,
                    value_string=self.value_string,
                    value_number=self.value_number,
                    value_bool=self.value_bool,
                    unit=self.unit,
                ),
            )

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write."""
        return {
            "attr_id": self.attr_id,
            "key": self.key,
            # `slug`, not `canonical_text`: this is the property the candidate query
            # compares an AttributeFilter's folded key against, and it must be the *same*
            # fold `attribute_value_id` hashes or reads and writes go out of symmetry.
            "canonical_key": slug(self.key),
            "value_string": self.value_string,
            # Canonicalised alongside the raw reading so the candidate query can compare
            # against exactly what attribute_value_id() hashed. Folding inside Cypher with
            # toLower/trim would be a *different* fold (no NFKC, no internal whitespace
            # collapse), and two adapters could then share an attr_id but fail to match the
            # same filter.
            "canonical_value_string": (
                None if self.value_string is None else canonical_text(self.value_string)
            ),
            "value_number": None if self.value_number is None else float(self.value_number),
            "value_bool": self.value_bool,
            "unit": self.unit,
            "canonical_unit": None if self.unit is None else canonical_text(self.unit),
        }


#: The longest ``alt`` text a ``MediaAsset`` keeps. Alt text is merchant-authored free text
#: (C10) and one product in the recorded corpus ships a 300-character caption; a node
#: property is not the place for an unbounded string, and nothing downstream reads past a
#: caption's worth.
MEDIA_ALT_MAX_CHARS = 512


@dataclass(frozen=True)
class MediaAsset:
    """``MediaAsset`` — one image the catalogue *published a record of*, never its bytes.

    THIS NODE IS A CLAIM, NOT AN ASSET. Ingest reads ``images[]`` off the catalogue surface
    and writes what the seller said about each one — where it lives, how big the seller says
    it is, where it sits in the gallery, and the digest of that record. It does **not**
    fetch a single image byte, so nothing here asserts that the URL resolves or that the
    pixels behind it are the ones the catalogue described.

    That restraint is the point. The owner's ruling is *verified-primary media, with
    labelled-unverified as the fallback*: an asset scores only if it (1) resolves, (2) sits
    on the seller's registered domain, and (3) its content hash matches the catalogue
    snapshot for that ``product_ref``. Two of those three legs need a fetch, and a fetch is
    not ingest's job. What ingest owes the downstream verifier is that the question be
    **decidable** — every input to legs (2) and (3) present, provenanced, and attached to
    the product it was published under. See :attr:`on_seller_domain` and
    :attr:`catalogue_hash`.

    Attributes:
        asset_id: ``mda_…``, derived from (store, the store's product key, the store's own
            image id — or the URL when it publishes none), so a re-crawl of an unchanged
            gallery lands on the same node instead of churning one per run.
        url: the image URL exactly as the catalogue published it, untouched. Shopify's
            ``?v=<epoch>`` cache-buster is part of the identity of the version claimed and
            is deliberately not stripped.
        host: the URL's lower-cased hostname, stored so leg (2) is a comparison rather than
            a re-parse of merchant-controlled text in whoever asks next.
        on_seller_domain: whether :attr:`host` is the store's own registered domain or a
            subdomain of it. **The only leg of the verified check ingest can decide**, and
            it is decided from two facts ingest already holds and has sourced.
            **Measured, and it matters:** all 18,165 image records in
            ``fixtures/real-catalogs/`` are on ``cdn.shopify.com`` — *zero* on any seller's
            own domain. Under a literal reading of leg (2) not one real image would score,
            so whoever implements scoring has to decide whether a merchant's platform CDN
            counts as the registered domain. That decision is theirs; this flag reports the
            fact rather than pre-judging it.
        position: the catalogue's own ordering, 1-based. ``1`` is the primary image, which
            is the one "verified-primary" is about.
        alt: merchant-authored alt text, truncated to :data:`MEDIA_ALT_MAX_CHARS`.
        width / height: the pixel dimensions **the catalogue claims**, ``0`` when unstated.
            A fetched asset whose real dimensions differ from these contradicts the record.
        catalogue_hash: the digest of the image record as the snapshot published it (URL,
            dimensions and the seller's own updated-at). This is the anchor for leg (3):
            it changes the moment the seller edits the record, so a verifier that pinned a
            byte digest against one ``catalogue_hash`` knows exactly when its pin expired.
            It is **not** a hash of the image bytes and must never be compared to one.

    WHAT A DOWNSTREAM CONSUMER STILL NEEDS, exactly, to finish the verified check. Ingest
    supplies the first three and none of the last four:

    ============================================  =====================================
    already here                                  still owed by the verifier
    ============================================  =====================================
    the URL the catalogue published (:attr:`url`) a **fetch** of it, under the same SSRF
                                                  guard the crawler runs
                                                  (:mod:`ingest.adapters.netguard`); leg 1
    the host, pre-parsed (:attr:`host`) and the   the **ruling** on whether a merchant's
    seller-domain verdict                         platform CDN counts as its registered
    (:attr:`on_seller_domain`)                    domain — every image in the recorded
                                                  corpus is on ``cdn.shopify.com``
    the seller's own record of the file           a **byte digest** of what it fetched,
    (:attr:`catalogue_hash`, :attr:`width`,       stored against that ``catalogue_hash``,
    :attr:`height`), provenanced and attached     so a later fetch can be compared and a
    to its ``Product``                            seller edit re-opens the check; leg 3
                                                  a **place to record the verdict** — a
                                                  property, a node or a table of its own.
                                                  Nothing in ingest writes one, because a
                                                  verdict ingest cannot compute would be a
                                                  claim without evidence
    ============================================  =====================================

    Two things ingest deliberately does **not** do, so nobody looks for them: it opens no
    connection to an image host, and it emits no scoring term. An unverified asset may still
    be shown, labelled; whether it *scores* is the ranker's decision and the ranker is not
    touched here.
    """

    asset_id: str
    url: str
    catalogue_hash: str
    host: str = ""
    on_seller_domain: bool = False
    position: int = 0
    alt: str = ""
    width: int = 0
    height: int = 0

    def __post_init__(self) -> None:
        """Refuse an asset that could not be verified or even displayed later.

        Raises:
            ValueError: ``asset_id``, ``url`` or ``catalogue_hash`` is blank. A media node
                with no URL is undisplayable *and* unverifiable, so it is the media-shaped
                version of the hollow ``Source`` — it would satisfy "the catalogue carried
                images" while carrying nothing anybody can check.
        """
        for field_name in ("asset_id", "url", "catalogue_hash"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(
                    f"MediaAsset.{field_name} must be non-empty: an asset with no "
                    f"{field_name} can be neither shown nor checked"
                )

    @property
    def primary(self) -> bool:
        """Whether the catalogue published this as the product's first image."""
        return int(self.position) == 1

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write."""
        return {
            "asset_id": self.asset_id,
            "url": self.url,
            "host": self.host,
            "on_seller_domain": bool(self.on_seller_domain),
            "position": int(self.position),
            "alt": str(self.alt or "")[:MEDIA_ALT_MAX_CHARS],
            "width": int(self.width),
            "height": int(self.height),
            "catalogue_hash": self.catalogue_hash,
        }


@dataclass(frozen=True)
class PolicyPage:
    """``PolicyPage{kind, hash, snapshot_ref}`` plus the ``page_id`` its constraint needs."""

    page_id: str
    kind: str
    hash: str
    snapshot_ref: str = ""

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write."""
        return {
            "page_id": self.page_id,
            "kind": self.kind,
            "hash": self.hash,
            "snapshot_ref": self.snapshot_ref,
        }


@dataclass(frozen=True)
class IntentCluster:
    """``IntentCluster`` — the retrieval-side grouping T-031 and the exchange key off."""

    cluster_id: str
    label: str = ""

    def as_properties(self) -> dict[str, Any]:
        """The node properties to write."""
        return {"cluster_id": self.cluster_id, "label": self.label}


__all__ = [
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_PROPERTY",
    "ID_PROPERTY",
    "MEDIA_ALT_MAX_CHARS",
    "MATERIAL_FACT_LABELS",
    "MATERIAL_FACT_EDGES",
    "VOCABULARY_LABELS",
    "SOURCE_CLASSES",
    "SOURCE_ID_PROPERTY",
    "SUPPORTED_BY",
    "AttributeValue",
    "Category",
    "InvalidEmbeddingVector",
    "Ingredient",
    "IntentCluster",
    "MediaAsset",
    "Offer",
    "PolicyPage",
    "Product",
    "Source",
    "Store",
    "Variant",
    "attribute_value_id",
    "canonical_text",
    "category_id",
    "embedding_vector_defect",
    "ingredient_id",
    "slug",
]
