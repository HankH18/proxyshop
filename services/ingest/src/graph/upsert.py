"""Provenanced upserts into the catalog graph (T-012, acceptance 1).

Every function that writes a material fact takes ``source: Source`` as a **required
keyword-only argument**, and writes the ``SUPPORTED_BY`` edge (for a node-shaped fact) or
the ``source_id`` property (for an edge-shaped one) in the *same* statement as the fact
itself. There is deliberately no ``upsert_product(product)`` overload without a source, and
no "attach provenance later" helper: the requirement is that a fact cannot exist unsourced,
so the API is shaped so that writing one is not expressible.

The audit that proves it after the fact is :func:`provenance_violations`, which reads the
graph rather than the code — so it also catches facts written by a *different* library, by
raw Cypher in a test, or by an adapter that bypassed this module.

Idempotence
-----------
Every statement is ``MERGE`` on the stable ID and ``SET n += $props``, so re-running an
ingest of unchanged pages converges rather than duplicating. T-021 re-extracts on content
hash change and T-022 resolves entities on top of these nodes; both depend on the node
identity being the ID, never the name (DESIGN: "never match products by free-text name
alone").

Two consequences of that shape, stated because an adapter author will hit both:

* **A node accumulates one ``SUPPORTED_BY`` edge per distinct ``Source``.** That is
  provenance history and is intended — a product asserted by three stores has three
  supports. It is bounded by the number of distinct ``source_id`` values, so make
  ``source_id`` content-addressed (url + content_hash), *not* per-crawl-run. A run-scoped
  id would grow the graph without bound on a re-crawl schedule while every test still
  passed.
* **An edge keeps only its most recent ``source_id``.** ``MERGE`` collapses the
  relationship and ``SET r += $props`` overwrites, so "which sources have ever asserted
  this edge" is not recoverable from the edge. The trade is deliberate: one edge per
  ``(a, type, b)`` keeps traversal cost flat and keeps the provenance audit a single-hop
  check. A ticket that needs full edge-level provenance history should reify the assertion
  as a node rather than change this.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .model import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_PROPERTY,
    ID_PROPERTY,
    MATERIAL_FACT_EDGES,
    MATERIAL_FACT_LABELS,
    SOURCE_ID_PROPERTY,
    SUPPORTED_BY,
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
)


class ProvenanceRequired(ValueError):
    """A material fact was offered without a resolvable :class:`~ingest.graph.model.Source`."""


class EmbeddingDimensionMismatch(ValueError):
    """A vector was offered whose width the ``product_embedding`` index cannot match."""


# ---------------------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------------------

_UPSERT_SOURCE = """
MERGE (s:Source {source_id: $source_id})
SET s += $props
RETURN s.source_id AS source_id
"""


def upsert_source(session: Any, source: Source) -> str:
    """Create or update the ``Source`` node every fact points at.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        source: the provenance record.

    Returns:
        ``source.source_id``.
    """
    session.run(_UPSERT_SOURCE, source_id=source.source_id, props=source.as_properties()).consume()
    return source.source_id


def _require_source(source: Source | None) -> Source:
    """Reject a write that named no provenance.

    Args:
        source: the caller's source, possibly ``None`` if they passed one explicitly.

    Returns:
        The same source.

    Raises:
        ProvenanceRequired: ``source`` is ``None``.
    """
    if source is None:
        raise ProvenanceRequired(
            "every material fact must upsert with a SUPPORTED_BY Source (T-012 acceptance 1)"
        )
    return source


def _fact_node(
    session: Any,
    label: str,
    id_property: str,
    id_value: str,
    props: dict[str, Any],
    source: Source,
) -> str:
    """MERGE a material-fact node and its ``SUPPORTED_BY`` edge in one statement.

    The two halves are one statement on purpose: split across two calls, a failure between
    them leaves an unsourced fact in a graph six other tickets read.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        label: the node label; must be in
            :data:`~ingest.graph.model.MATERIAL_FACT_LABELS`.
        id_property: the label's stable-ID property.
        id_value: the stable ID.
        props: properties to set on the node.
        source: the provenance record; upserted first so the edge always resolves.

    Returns:
        ``id_value``.

    Raises:
        ValueError: ``label`` is not a material-fact label (a vocabulary node must not grow
            a ``SUPPORTED_BY`` edge — its *attachment* is the fact, not the term).
    """
    if label not in MATERIAL_FACT_LABELS:
        raise ValueError(
            f"{label} is not a material-fact label; got {sorted(MATERIAL_FACT_LABELS)}"
        )
    upsert_source(session, source)
    session.run(
        f"""
        MATCH (src:Source {{source_id: $source_id}})
        MERGE (n:{label} {{{id_property}: $id_value}})
        SET n += $props
        MERGE (n)-[r:{SUPPORTED_BY}]->(src)
        SET r.observed_at = src.observed_at, r.confidence = src.confidence
        """,
        source_id=source.source_id,
        id_value=id_value,
        props=props,
    ).consume()
    return id_value


def _vocabulary_node(session: Any, label: str, id_property: str, props: dict[str, Any]) -> str:
    """MERGE a vocabulary term (``Category`` / ``Ingredient`` / ``IntentCluster``).

    Args:
        session: an open ``neo4j.Session`` or transaction.
        label: the node label.
        id_property: the label's stable-ID property.
        props: properties to set, including the ID.

    Returns:
        The stable ID that was merged.
    """
    id_value = props[id_property]
    session.run(
        f"MERGE (n:{label} {{{id_property}: $id_value}}) SET n += $props",
        id_value=id_value,
        props=props,
    ).consume()
    return str(id_value)


def _require_nodes(session: Any, endpoints: Sequence[tuple[str, str]]) -> None:
    """Refuse before writing anything if any endpoint is missing.

    A multi-node upsert (``upsert_offer`` writes an Offer, then MAKES_OFFER, then FOR) is
    three statements, not one transaction, because the ``session`` argument may already be
    inside a caller's transaction and opening another would deadlock against it. Without
    this pre-flight, a bad ``store_id`` let the Offer node land and *then* raised — leaving
    an orphaned Offer that ``provenance_violations()`` correctly reports as clean, because
    it is properly sourced; it is simply attached to nothing. Checking first makes the whole
    operation all-or-nothing in the only way available here.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        endpoints: ``(label, stable_id)`` pairs that must already exist.

    Raises:
        ProvenanceRequired: one or more endpoints do not exist. Named so the caller sees the
            same error whether the endpoint was missing before or during the write.
    """
    missing = [
        f"{label}({identity})"
        for label, identity in endpoints
        if session.run(
            f"MATCH (n:{label} {{{ID_PROPERTY[label]}: $identity}}) RETURN count(n) AS c",
            identity=identity,
        ).single()["c"]
        == 0
    ]
    if missing:
        raise ProvenanceRequired(
            f"cannot write: {', '.join(missing)} do(es) not exist. Upsert the node(s) with "
            f"their own Source first — creating them here would be an unsourced fact, and "
            f"writing the rest anyway would leave an orphan."
        )


def _fact_edge(
    session: Any,
    edge: str,
    from_label: str,
    from_id: str,
    to_label: str,
    to_id: str,
    source: Source,
    edge_props: dict[str, Any] | None = None,
) -> None:
    """MERGE an edge-shaped material fact, carrying its provenance as ``source_id``.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        edge: the relationship type; must be a key of
            :data:`~ingest.graph.model.MATERIAL_FACT_EDGES`.
        from_label: the start node's label.
        from_id: the start node's stable ID.
        to_label: the end node's label.
        to_id: the end node's stable ID.
        source: the provenance record.
        edge_props: extra relationship properties (e.g. ``SAME_AS.confidence``).

    Raises:
        ValueError: ``edge`` is unknown, or its endpoints do not match the model.
        ProvenanceRequired: either endpoint does not exist, which would otherwise create a
            dangling, propertyless node through ``MERGE`` and silently pass the audit.
    """
    if edge not in MATERIAL_FACT_EDGES:
        raise ValueError(f"unknown material-fact edge {edge!r}; got {sorted(MATERIAL_FACT_EDGES)}")
    expected = MATERIAL_FACT_EDGES[edge]
    if (from_label, to_label) != expected:
        raise ValueError(
            f"{edge} runs ({expected[0]})->({expected[1]}), not ({from_label})->({to_label})"
        )
    upsert_source(session, source)
    props = {SOURCE_ID_PROPERTY: source.source_id, "observed_at": source.observed_at}
    props.update(edge_props or {})
    summary = session.run(
        f"""
        MATCH (a:{from_label} {{{ID_PROPERTY[from_label]}: $from_id}})
        MATCH (b:{to_label} {{{ID_PROPERTY[to_label]}: $to_id}})
        MERGE (a)-[r:{edge}]->(b)
        SET r += $props
        RETURN count(r) AS written
        """,
        from_id=from_id,
        to_id=to_id,
        props=props,
    )
    if (summary.single() or {"written": 0})["written"] == 0:
        raise ProvenanceRequired(
            f"cannot write {from_label}({from_id})-[:{edge}]->{to_label}({to_id}): one or "
            f"both endpoints do not exist. Upsert the nodes (with their own Source) first — "
            f"MERGE-ing them here would create unsourced facts."
        )


# ---------------------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------------------


def upsert_store(session: Any, store: Store, *, source: Source) -> str:
    """Upsert a ``Store`` with its provenance.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        store: the store payload.
        source: where the observation came from.

    Returns:
        ``store.store_id``.
    """
    return _fact_node(
        session, "Store", "store_id", store.store_id, store.as_properties(), _require_source(source)
    )


def upsert_product(session: Any, product: Product, *, source: Source) -> str:
    """Upsert a ``Product`` with its provenance.

    The embedding is deliberately not a parameter — see
    :class:`~ingest.graph.model.Product`.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        product: the product payload.
        source: where the observation came from.

    Returns:
        ``product.product_id``.
    """
    return _fact_node(
        session,
        "Product",
        "product_id",
        product.product_id,
        product.as_properties(),
        _require_source(source),
    )


def upsert_variant(
    session: Any, variant: Variant, *, product_id: str | None = None, source: Source
) -> str:
    """Upsert a ``Variant``, optionally attaching it to its product.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        variant: the variant payload.
        product_id: when given, also writes ``(Product)-[:HAS_VARIANT]->(Variant)``.
        source: where the observation came from.

    Returns:
        ``variant.variant_id``.
    """
    resolved = _require_source(source)
    if product_id is not None:
        _require_nodes(session, [("Product", product_id)])
    _fact_node(
        session, "Variant", "variant_id", variant.variant_id, variant.as_properties(), resolved
    )
    if product_id is not None:
        _fact_edge(
            session, "HAS_VARIANT", "Product", product_id, "Variant", variant.variant_id, resolved
        )
    return variant.variant_id


def upsert_offer(
    session: Any,
    offer: Offer,
    *,
    store_id: str,
    variant_id: str,
    source: Source,
) -> str:
    """Upsert the **graph** ``Offer`` (D25) and wire ``MAKES_OFFER`` → ``FOR``.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        offer: the observed listing.
        store_id: the observing store; gets ``(Store)-[:MAKES_OFFER]->(Offer)``.
        variant_id: the listed variant; gets ``(Offer)-[:FOR]->(Variant)``.
        source: where the observation came from.

    Returns:
        ``offer.offer_id``.
    """
    resolved = _require_source(source)
    _require_nodes(session, [("Store", store_id), ("Variant", variant_id)])
    _fact_node(session, "Offer", "offer_id", offer.offer_id, offer.as_properties(), resolved)
    _fact_edge(session, "MAKES_OFFER", "Store", store_id, "Offer", offer.offer_id, resolved)
    _fact_edge(session, "FOR", "Offer", offer.offer_id, "Variant", variant_id, resolved)
    return offer.offer_id


def upsert_policy_page(session: Any, page: PolicyPage, *, source: Source) -> str:
    """Upsert a ``PolicyPage`` with its provenance.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        page: the page payload.
        source: where the observation came from.

    Returns:
        ``page.page_id``.
    """
    return _fact_node(
        session,
        "PolicyPage",
        "page_id",
        page.page_id,
        page.as_properties(),
        _require_source(source),
    )


def upsert_category(session: Any, category: Category) -> str:
    """Merge a ``Category`` vocabulary term.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        category: the term.

    Returns:
        ``category.category_id``.
    """
    return _vocabulary_node(session, "Category", "category_id", category.as_properties())


def upsert_ingredient(session: Any, ingredient: Ingredient) -> str:
    """Merge an ``Ingredient`` vocabulary term.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        ingredient: the term.

    Returns:
        ``ingredient.ingredient_id``.
    """
    return _vocabulary_node(session, "Ingredient", "ingredient_id", ingredient.as_properties())


def upsert_intent_cluster(session: Any, cluster: IntentCluster) -> str:
    """Merge an ``IntentCluster`` grouping node.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        cluster: the cluster.

    Returns:
        ``cluster.cluster_id``.
    """
    return _vocabulary_node(session, "IntentCluster", "cluster_id", cluster.as_properties())


# ---------------------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------------------


def link_sells(session: Any, *, store_id: str, product_id: str, source: Source) -> None:
    """``(Store)-[:SELLS]->(Product)``.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        store_id: the selling store.
        product_id: the product sold.
        source: where the observation came from.
    """
    _fact_edge(session, "SELLS", "Store", store_id, "Product", product_id, _require_source(source))


def link_category(session: Any, *, product_id: str, category: Category, source: Source) -> str:
    """Merge a category term and attach it: ``(Product)-[:IN_CATEGORY]->(Category)``.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        product_id: the product.
        category: the category term.
        source: where the observation came from.

    Returns:
        ``category.category_id``.
    """
    resolved = _require_source(source)
    upsert_category(session, category)
    _fact_edge(
        session, "IN_CATEGORY", "Product", product_id, "Category", category.category_id, resolved
    )
    return category.category_id


def upsert_attribute(
    session: Any, *, product_id: str, attribute: AttributeValue, source: Source
) -> str:
    """Upsert an ``AttributeValue`` node and attach it: ``(Product)-[:HAS_ATTRIBUTE]->(…)``.

    Both halves are material facts and both are provenanced: the node grows a
    ``SUPPORTED_BY`` edge (a reading of "SPF 50" was observed *somewhere*) and the
    attachment edge carries ``source_id`` (this *product* was observed to have it, in this
    page). They are genuinely different claims, which is why the model carries both.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        product_id: the product carrying the attribute.
        attribute: the attribute reading.
        source: where the observation came from.

    Returns:
        ``attribute.attr_id``.
    """
    resolved = _require_source(source)
    _fact_node(
        session, "AttributeValue", "attr_id", attribute.attr_id, attribute.as_properties(), resolved
    )
    _fact_edge(
        session,
        "HAS_ATTRIBUTE",
        "Product",
        product_id,
        "AttributeValue",
        attribute.attr_id,
        resolved,
    )
    return attribute.attr_id


def link_ingredient(
    session: Any, *, product_id: str, ingredient: Ingredient, source: Source
) -> str:
    """Merge an ingredient term and attach it: ``(Product)-[:CONTAINS]->(Ingredient)``.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        product_id: the product.
        ingredient: the ingredient term.
        source: where the observation came from.

    Returns:
        ``ingredient.ingredient_id``.
    """
    resolved = _require_source(source)
    upsert_ingredient(session, ingredient)
    _fact_edge(
        session, "CONTAINS", "Product", product_id, "Ingredient", ingredient.ingredient_id, resolved
    )
    return ingredient.ingredient_id


def link_compatible_with(session: Any, *, product_id: str, other_id: str, source: Source) -> None:
    """``(Product)-[:COMPATIBLE_WITH]->(Product)``.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        product_id: the subject product.
        other_id: the product it is compatible with.
        source: where the observation came from.
    """
    _fact_edge(
        session,
        "COMPATIBLE_WITH",
        "Product",
        product_id,
        "Product",
        other_id,
        _require_source(source),
    )


def link_same_as(
    session: Any, *, product_id: str, other_id: str, confidence: float, source: Source
) -> None:
    """``(Product)-[:SAME_AS {confidence}]->(Product)`` — the T-022 entity-resolution edge.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        product_id: the subject product.
        other_id: the product judged to be the same item.
        confidence: the resolver's confidence in ``[0, 1]``.
        source: where the judgement came from (``source_class="network"`` for a resolver).

    Raises:
        ValueError: ``confidence`` is outside ``[0, 1]``.
    """
    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError(f"SAME_AS.confidence must be in [0, 1], got {confidence!r}")
    _fact_edge(
        session,
        "SAME_AS",
        "Product",
        product_id,
        "Product",
        other_id,
        _require_source(source),
        edge_props={"confidence": float(confidence)},
    )


def link_states(session: Any, *, page_id: str, attribute: AttributeValue, source: Source) -> str:
    """``(PolicyPage)-[:STATES]->(AttributeValue)`` — what a policy page asserts.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        page_id: the policy page.
        attribute: the stated reading (e.g. ``returns_window_days = 30``).
        source: where the observation came from.

    Returns:
        ``attribute.attr_id``.
    """
    resolved = _require_source(source)
    _fact_node(
        session, "AttributeValue", "attr_id", attribute.attr_id, attribute.as_properties(), resolved
    )
    _fact_edge(
        session, "STATES", "PolicyPage", page_id, "AttributeValue", attribute.attr_id, resolved
    )
    return attribute.attr_id


# ---------------------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------------------


def set_product_embedding(
    session: Any,
    *,
    product_id: str,
    embedding: Sequence[float],
    dimensions: int = EMBEDDING_DIMENSIONS,
) -> None:
    """Write ``Product.embedding``, refusing any vector the index cannot match.

    THE LENGTH CHECK IS THE POINT. Measured on neo4j 5.26.30: ``db.create.setNodeVectorProperty``
    accepts a 512-d or a 1025-d vector **without error** and stores it verbatim. Nothing
    downstream then complains — ``db.index.vector.queryNodes`` simply never returns that
    node, ``candidate_products`` never surfaces it, ``products_missing_embeddings()``
    reports it as embedded (it has *an* embedding), and ``provenance_violations()`` is
    clean. The product becomes silently unrankable, which is exactly the failure
    :func:`ingest.graph.reembed.reembed_products` refuses to cause for empty text. So the
    width is checked here, in Python, before the write.

    ``db.create.setNodeVectorProperty`` is still used rather than a plain ``SET`` because it
    is the documented API for writing an indexed vector and coerces the value into the
    index's native representation. Honesty about the strength of that claim: a plain ``SET``
    was measured to work on this build too, so the procedure is the supported path, not a
    load-bearing safety net — the safety net is the check above it.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        product_id: the product to embed.
        embedding: the vector.
        dimensions: the width the live index was built for. Defaults to D6's 1024; pass the
            provider's width after a :func:`ingest.graph.schema.rebuild_vector_index`.

    Raises:
        EmbeddingDimensionMismatch: ``embedding`` is not ``dimensions`` long.
        ProvenanceRequired: the product does not exist. An embedding is a derived value,
            not an independent fact, so it never creates a node — and a ``MERGE`` here would
            create an unsourced ``Product`` out of a typo'd id.
    """
    vector = [float(component) for component in embedding]
    if len(vector) != dimensions:
        raise EmbeddingDimensionMismatch(
            f"refusing to write a {len(vector)}-d vector for product_id={product_id!r}: the "
            f"product_embedding index is {dimensions}-d, and Neo4j would accept the write "
            f"silently and then never return this product from any vector query"
        )
    result = session.run(
        f"""
        MATCH (p:Product {{product_id: $product_id}})
        CALL db.create.setNodeVectorProperty(p, '{EMBEDDING_PROPERTY}', $embedding)
        RETURN count(p) AS updated
        """,
        product_id=product_id,
        embedding=vector,
    )
    if (result.single() or {"updated": 0})["updated"] == 0:
        raise ProvenanceRequired(
            f"no Product with product_id={product_id!r}; embeddings are derived from facts "
            f"and never create them"
        )


# ---------------------------------------------------------------------------------------
# The provenance audit
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvenanceViolation:
    """One material fact with no resolvable ``Source``."""

    kind: str
    label_or_type: str
    identity: str
    detail: str


_UNSOURCED_NODES = """
MATCH (n)
WHERE any(label IN labels(n) WHERE label IN $labels)
  AND NOT EXISTS { (n)-[:SUPPORTED_BY]->(:Source) }
RETURN labels(n) AS labels, properties(n) AS props
"""

_SUPPORTED_BY_NOT_A_SOURCE = """
MATCH (n)-[:SUPPORTED_BY]->(t)
WHERE NOT t:Source
RETURN labels(n) AS labels, properties(n) AS props, labels(t) AS target_labels
"""

_UNSOURCED_EDGES = """
MATCH (a)-[r]->(b)
WHERE type(r) IN $types
RETURN type(r) AS type,
       properties(r) AS props,
       elementId(a) AS a_id,
       elementId(b) AS b_id,
       r.source_id AS source_id,
       EXISTS { MATCH (:Source {source_id: r.source_id}) } AS resolves
"""


def provenance_violations(session: Any) -> list[ProvenanceViolation]:
    """Every material fact currently in the graph that has no resolvable ``Source``.

    Reads the **graph**, not this module's call sites, so it also catches facts written by
    raw Cypher, by another library, or by an adapter that skipped :mod:`ingest.graph.upsert`
    — which is the only way the invariant can be evidence rather than a convention.

    Three violation kinds:

    * ``unsourced_node`` — a :data:`~ingest.graph.model.MATERIAL_FACT_LABELS` node with no
      outgoing ``SUPPORTED_BY``;
    * ``supported_by_non_source`` — a ``SUPPORTED_BY`` edge pointing at something that is
      not a ``Source``;
    * ``unsourced_edge`` — a :data:`~ingest.graph.model.MATERIAL_FACT_EDGES` relationship
      whose ``source_id`` is missing or names a ``Source`` that does not exist.

    Args:
        session: an open ``neo4j.Session``.

    Returns:
        The violations, in a stable order. Empty means the invariant holds.
    """
    violations: list[ProvenanceViolation] = []
    for row in session.run(_UNSOURCED_NODES, labels=sorted(MATERIAL_FACT_LABELS)).data():
        label = next((lab for lab in row["labels"] if lab in MATERIAL_FACT_LABELS), "?")
        identity = str(row["props"].get(ID_PROPERTY.get(label, ""), "<no id>"))
        violations.append(
            ProvenanceViolation(
                kind="unsourced_node",
                label_or_type=label,
                identity=identity,
                detail="no (n)-[:SUPPORTED_BY]->(:Source)",
            )
        )
    for row in session.run(_SUPPORTED_BY_NOT_A_SOURCE).data():
        label = next((lab for lab in row["labels"] if lab in MATERIAL_FACT_LABELS), "?")
        identity = str(row["props"].get(ID_PROPERTY.get(label, ""), "<no id>"))
        violations.append(
            ProvenanceViolation(
                kind="supported_by_non_source",
                label_or_type=label,
                identity=identity,
                detail=f"SUPPORTED_BY points at {row['target_labels']}, not a Source",
            )
        )
    for row in session.run(_UNSOURCED_EDGES, types=sorted(MATERIAL_FACT_EDGES)).data():
        if row["source_id"] and row["resolves"]:
            continue
        violations.append(
            ProvenanceViolation(
                kind="unsourced_edge",
                label_or_type=row["type"],
                identity=f"{row['a_id']}->{row['b_id']}",
                detail=(
                    "missing source_id"
                    if not row["source_id"]
                    else f"source_id={row['source_id']!r} resolves to no Source node"
                ),
            )
        )
    return sorted(violations, key=lambda v: (v.kind, v.label_or_type, v.identity))


def assert_provenance_complete(session: Any) -> None:
    """Raise unless every material fact in the graph is sourced.

    Args:
        session: an open ``neo4j.Session``.

    Raises:
        ProvenanceRequired: at least one violation; the message lists them.
    """
    violations = provenance_violations(session)
    if violations:
        listing = "\n".join(
            f"  {v.kind}: {v.label_or_type}({v.identity}) — {v.detail}" for v in violations
        )
        raise ProvenanceRequired(f"{len(violations)} unsourced material fact(s):\n{listing}")


def seed_products(session: Any, records: Iterable[dict[str, Any]], *, source: Source) -> list[str]:
    """Convenience bulk upsert used by the re-embed sample data and by adapter tests.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        records: mappings with ``product_id``, ``canonical_name``, and the optional keys
            ``brand``, ``status``, ``category``, ``attributes`` (list of
            :class:`~ingest.graph.model.AttributeValue`) and ``ingredients`` (list of names).
        source: where the observations came from.

    Returns:
        The product ids written, in input order.
    """
    resolved = _require_source(source)
    written: list[str] = []
    for record in records:
        product = Product(
            product_id=record["product_id"],
            canonical_name=record["canonical_name"],
            brand=record.get("brand", ""),
            status=record.get("status", "active"),
        )
        upsert_product(session, product, source=resolved)
        if record.get("category"):
            link_category(
                session,
                product_id=product.product_id,
                category=Category(name=record["category"]),
                source=resolved,
            )
        for attribute in record.get("attributes", ()):
            upsert_attribute(
                session, product_id=product.product_id, attribute=attribute, source=resolved
            )
        for name in record.get("ingredients", ()):
            link_ingredient(
                session,
                product_id=product.product_id,
                ingredient=Ingredient(name=name),
                source=resolved,
            )
        written.append(product.product_id)
    return written


__all__ = [
    "EmbeddingDimensionMismatch",
    "ProvenanceRequired",
    "ProvenanceViolation",
    "assert_provenance_complete",
    "link_category",
    "link_compatible_with",
    "link_ingredient",
    "link_same_as",
    "link_sells",
    "link_states",
    "provenance_violations",
    "seed_products",
    "set_product_embedding",
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
