"""The Neo4j catalog graph: model, idempotent schema, provenanced upserts, retrieval (T-012).

The layer T-020…T-024 write into and T-031 reads from. Four modules, four jobs:

* :mod:`ingest.graph.model` — labels, edge types, node payloads, content-hashed stable IDs.
* :mod:`ingest.graph.schema` — ``IF NOT EXISTS`` constraints on every stable ID plus D6's
  1024-d cosine ``product_embedding`` index.
* :mod:`ingest.graph.upsert` — every write takes a required ``source``, and
  :func:`~ingest.graph.upsert.provenance_violations` audits the result.
* :mod:`ingest.graph.query` — vector **and** attribute candidate retrieval; a request with
  neither raises rather than falling back to a name scan.

Typical use::

    from ingest.graph import Source, Product, apply_schema, upsert_product

    apply_schema(session)
    src = Source("src-1", "https://store.example/p/1", "sha256:…", "2026-01-01T00:00:00Z",
                 "adapter@1", 0.9, "scraped")
    upsert_product(session, Product("p-1", "Gentle Vitamin C Serum", brand="Acme"),
                   source=src)

D38: Neo4j Community has exactly one database, so isolation is scheduler serialization plus
the ``/tmp/proxyshop-neo4j.lock`` flock the root ``conftest.py`` takes — not tenant scoping.
Nothing in this package is tenant-parameterised, deliberately.
"""

from __future__ import annotations

from .model import (
    EMBEDDING_PROPERTY,
    ID_PROPERTY,
    MATERIAL_FACT_EDGES,
    MATERIAL_FACT_LABELS,
    SOURCE_CLASSES,
    SOURCE_ID_PROPERTY,
    SUPPORTED_BY,
    VOCABULARY_LABELS,
    AttributeValue,
    Category,
    Ingredient,
    IntentCluster,
    Offer,
    PolicyPage,
    Product,
    Source,
    Store,
    Variant,
    attribute_value_id,
    canonical_text,
    category_id,
    ingredient_id,
    slug,
)
from .query import (
    AttributeFilter,
    Candidate,
    UnretrievableQuery,
    candidate_products,
    cosine_from_score,
    products_missing_embeddings,
)
from .reembed import ReembedReport, embedding_text, graph_driver, reembed_products
from .schema import (
    LOOKUP_INDEXES,
    VECTOR_INDEX_DIMENSIONS,
    VECTOR_INDEX_NAME,
    VECTOR_INDEX_SIMILARITY,
    VECTOR_INDEX_STATEMENT,
    SchemaReport,
    apply_schema,
    await_indexes,
    constraint_name,
    constraint_statements,
    lookup_index_statements,
    rebuild_vector_index,
    schema_report,
    schema_statements,
)
from .upsert import (
    ProvenanceRequired,
    ProvenanceViolation,
    assert_provenance_complete,
    link_category,
    link_compatible_with,
    link_ingredient,
    link_same_as,
    link_sells,
    link_states,
    provenance_violations,
    seed_products,
    set_product_embedding,
    upsert_attribute,
    upsert_category,
    upsert_ingredient,
    upsert_intent_cluster,
    upsert_offer,
    upsert_policy_page,
    upsert_product,
    upsert_source,
    upsert_store,
    upsert_variant,
)

__all__ = [
    "EMBEDDING_PROPERTY",
    "ID_PROPERTY",
    "LOOKUP_INDEXES",
    "MATERIAL_FACT_EDGES",
    "MATERIAL_FACT_LABELS",
    "SOURCE_CLASSES",
    "SOURCE_ID_PROPERTY",
    "SUPPORTED_BY",
    "VECTOR_INDEX_DIMENSIONS",
    "VECTOR_INDEX_NAME",
    "VECTOR_INDEX_SIMILARITY",
    "VECTOR_INDEX_STATEMENT",
    "VOCABULARY_LABELS",
    "AttributeFilter",
    "AttributeValue",
    "Candidate",
    "Category",
    "Ingredient",
    "IntentCluster",
    "Offer",
    "PolicyPage",
    "Product",
    "ProvenanceRequired",
    "ProvenanceViolation",
    "ReembedReport",
    "SchemaReport",
    "Source",
    "Store",
    "UnretrievableQuery",
    "Variant",
    "apply_schema",
    "assert_provenance_complete",
    "attribute_value_id",
    "await_indexes",
    "candidate_products",
    "canonical_text",
    "category_id",
    "constraint_name",
    "constraint_statements",
    "cosine_from_score",
    "embedding_text",
    "graph_driver",
    "ingredient_id",
    "link_category",
    "link_compatible_with",
    "link_ingredient",
    "link_same_as",
    "link_sells",
    "link_states",
    "lookup_index_statements",
    "products_missing_embeddings",
    "provenance_violations",
    "rebuild_vector_index",
    "reembed_products",
    "schema_report",
    "schema_statements",
    "seed_products",
    "set_product_embedding",
    "slug",
    "upsert_attribute",
    "upsert_category",
    "upsert_ingredient",
    "upsert_intent_cluster",
    "upsert_offer",
    "upsert_policy_page",
    "upsert_product",
    "upsert_source",
    "upsert_store",
    "upsert_variant",
]
