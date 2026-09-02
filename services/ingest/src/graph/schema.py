"""Idempotent Neo4j schema: uniqueness constraints on every stable ID, plus the D6 vector
index (T-012, acceptance 1).

Everything here is ``IF NOT EXISTS``. That is a hard requirement, not a nicety: D38 makes
Neo4j Community's single database shared between seven graph tickets serialised by the
scheduler and an ``flock``, so :func:`apply_schema` runs many times per day from many
processes and must be a no-op after the first. **A re-run error is never fixed by dropping
an index** — see :func:`rebuild_vector_index` for the one narrow case where a drop is
correct, and for why it is opt-in.

A NOTE ON D6, MEASURED HERE
---------------------------
D6 pins the vector index parameters and quotes the statement as::

    CREATE VECTOR INDEX product_embedding FOR (p:Product) ON (p.embedding)
    OPTIONS {indexConfig: {'vector.dimensions': 1024, 'vector.similarity_function': 'cosine'}}

That text does **not** parse on the neo4j 5.26.30 Community container this repo runs.
Measured, verbatim::

    Neo.ClientError.Statement.SyntaxError:
    Invalid input ''vector.dimensions'': expected an identifier or '}'
    (line 1, column 109)

Cypher map keys must be identifiers or backtick-quoted; single quotes make a string, and a
string is not a map key. :data:`VECTOR_INDEX_STATEMENT` below therefore backtick-quotes the
two dotted keys. **Every parameter D6 fixes is preserved exactly** — index name
``product_embedding``, label ``Product``, property ``embedding``, 1024 dimensions, cosine
similarity — and ``SHOW INDEXES`` on the created index reports
``{'vector.dimensions': 1024, 'vector.similarity_function': 'COSINE'}``. This is a syntax
correction to an unrunnable quotation, not a change to the decision; it is reported upward
in the T-012 completion report so D6's text can be repaired at the source.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .model import EMBEDDING_DIMENSIONS, EMBEDDING_PROPERTY, ID_PROPERTY

#: The index D6 names. T-031's retrieval and every candidate query go through it.
VECTOR_INDEX_NAME = "product_embedding"

#: D6, exactly: 1024 dimensions, cosine similarity, on ``Product.embedding``.
VECTOR_INDEX_DIMENSIONS = EMBEDDING_DIMENSIONS
VECTOR_INDEX_SIMILARITY = "cosine"


def vector_index_statement(
    dimensions: int = VECTOR_INDEX_DIMENSIONS, similarity: str = VECTOR_INDEX_SIMILARITY
) -> str:
    """Build the ``CREATE VECTOR INDEX`` statement for a given width.

    Parameterised rather than a bare constant because :func:`rebuild_vector_index` exists
    precisely to change the width, and a constant with 1024 baked into it made that
    function unable to do its one job: it dropped a 1024-d index and created another 1024-d
    index, so a swap to a 512-d provider left the whole catalog in an index it could never
    match. The default is D6 and nothing in the normal path passes anything else.

    Args:
        dimensions: the vector width the index is built for.
        similarity: the similarity function (``cosine`` per D6).

    Returns:
        A re-runnable ``IF NOT EXISTS`` statement.

    Raises:
        ValueError: ``dimensions`` is not positive.
    """
    if dimensions <= 0:
        raise ValueError(f"vector index dimensions must be positive, got {dimensions}")
    return (
        f"CREATE VECTOR INDEX {VECTOR_INDEX_NAME} IF NOT EXISTS "
        "FOR (p:Product) ON (p.embedding) "
        "OPTIONS {indexConfig: {"
        f"`vector.dimensions`: {dimensions}, "
        f"`vector.similarity_function`: '{similarity}'"
        "}}"
    )


#: The runnable form of D6's statement. See the module docstring for why the keys are
#: backtick-quoted rather than single-quoted.
VECTOR_INDEX_STATEMENT = vector_index_statement()

#: Non-unique range indexes. Purely a read-path concern — correctness does not depend on
#: them, so they are listed separately from the constraints and a reviewer can tell the two
#: apart at a glance.
#:
#: Split into two groups on purpose. The first group is the set of properties
#: :mod:`ingest.graph.query` actually compares against, and ``test_graph.py`` checks that
#: membership *against the shipped Cypher* rather than against this list — the earlier
#: version of that test simply restated this tuple, and so happily indexed
#: ``value_string`` while the query compared ``canonical_value_string``.
QUERY_PREDICATE_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("attribute_value_key", "AttributeValue", "canonical_key"),
    ("attribute_value_canonical_string", "AttributeValue", "canonical_value_string"),
    ("attribute_value_number", "AttributeValue", "value_number"),
    ("attribute_value_bool", "AttributeValue", "value_bool"),
    ("attribute_value_canonical_unit", "AttributeValue", "canonical_unit"),
    ("product_status", "Product", "status"),
    ("product_brand", "Product", "brand"),
)

#: The second group serves the *adapters* (T-020…T-024) resolving a term or a snapshot by
#: its human-readable value. Nothing in the candidate query touches these.
ADAPTER_LOOKUP_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("attribute_value_string", "AttributeValue", "value_string"),
    ("ingredient_canonical_name", "Ingredient", "canonical_name"),
    ("category_slug", "Category", "slug"),
    ("offer_observed_at", "Offer", "observed_at"),
    ("source_content_hash", "Source", "content_hash"),
    ("store_domain", "Store", "domain"),
)

LOOKUP_INDEXES: tuple[tuple[str, str, str], ...] = (
    *QUERY_PREDICATE_INDEXES,
    *ADAPTER_LOOKUP_INDEXES,
)


def constraint_name(label: str) -> str:
    """The deterministic constraint name for ``label``.

    Args:
        label: a node label from :data:`~ingest.graph.model.ID_PROPERTY`.

    Returns:
        e.g. ``"product_id_unique"``. Naming them explicitly (rather than letting Neo4j
        generate ``constraint_a1b2c3``) is what lets :func:`schema_report` assert the schema
        by name instead of by shape.
    """
    return f"{ID_PROPERTY[label]}_unique"


def constraint_statements() -> list[str]:
    """One ``IS UNIQUE`` constraint per stable ID, in a stable order.

    DESIGN: "Uniqueness constraints on every stable ID." :data:`ID_PROPERTY` *is* that list,
    so this cannot drift from the model by editing one and forgetting the other.

    Returns:
        Cypher statements, all ``IF NOT EXISTS``.
    """
    return [
        f"CREATE CONSTRAINT {constraint_name(label)} IF NOT EXISTS "
        f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
        for label, prop in sorted(ID_PROPERTY.items())
    ]


def lookup_index_statements() -> list[str]:
    """The read-path range indexes.

    Returns:
        Cypher statements, all ``IF NOT EXISTS``.
    """
    return [
        f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
        for name, label, prop in LOOKUP_INDEXES
    ]


def schema_statements() -> list[str]:
    """Every statement :func:`apply_schema` runs, in order.

    Returns:
        Constraints first (they own correctness), then lookup indexes, then the D6 vector
        index.
    """
    return [*constraint_statements(), *lookup_index_statements(), VECTOR_INDEX_STATEMENT]


@dataclass(frozen=True)
class SchemaReport:
    """What the database actually has, read back after applying the schema."""

    constraints: dict[str, str]
    indexes: dict[str, str]
    vector_index: dict[str, Any] | None

    @property
    def vector_dimensions(self) -> int | None:
        """The dimension count the live vector index was created with, or ``None``."""
        if not self.vector_index:
            return None
        config = (self.vector_index.get("options") or {}).get("indexConfig") or {}
        value = config.get("vector.dimensions")
        return None if value is None else int(value)

    @property
    def vector_similarity(self) -> str | None:
        """The similarity function the live vector index was created with, lowercased."""
        if not self.vector_index:
            return None
        config = (self.vector_index.get("options") or {}).get("indexConfig") or {}
        value = config.get("vector.similarity_function")
        return None if value is None else str(value).lower()


def apply_schema(session: Any, *, await_online: bool = True, timeout: int = 300) -> SchemaReport:
    """Create every constraint and index, idempotently, and read the result back.

    Safe to call from any process at any time — the first call creates, every later call is
    a no-op, and two concurrent calls both succeed (``IF NOT EXISTS`` is the whole point of
    D38(c)).

    Args:
        session: an open ``neo4j.Session``.
        await_online: block until the indexes finish populating. A vector index that is
            still ``POPULATING`` answers ``db.index.vector.queryNodes`` with fewer rows than
            it holds, which reads as a retrieval bug rather than a timing one.
        timeout: seconds to wait for population.

    Returns:
        A :class:`SchemaReport` describing what is now in the database.
    """
    for statement in schema_statements():
        session.run(statement).consume()
    if await_online:
        await_indexes(session, timeout=timeout)
    return schema_report(session)


def await_indexes(session: Any, *, timeout: int = 300) -> None:
    """Block until every index has finished populating.

    Call this after a write batch too, not only after :func:`apply_schema`: vector index
    updates land asynchronously, so a query issued microseconds after an upsert can
    legitimately miss the row it just wrote.

    Args:
        session: an open ``neo4j.Session``.
        timeout: seconds to wait.
    """
    session.run("CALL db.awaitIndexes($timeout)", timeout=int(timeout)).consume()


def schema_report(session: Any) -> SchemaReport:
    """Read the live constraints and indexes back out of the database.

    Args:
        session: an open ``neo4j.Session``.

    Returns:
        A :class:`SchemaReport`. Reading back rather than trusting the writes is what makes
        the idempotency test evidence instead of a claim.
    """
    constraints = {
        row["name"]: f"{(row['labelsOrTypes'] or [''])[0]}.{(row['properties'] or [''])[0]}"
        for row in session.run(
            "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties RETURN *"
        ).data()
    }
    indexes: dict[str, str] = {}
    vector_index: dict[str, Any] | None = None
    for row in session.run(
        "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, options, state "
        "RETURN *"
    ).data():
        indexes[row["name"]] = str(row["type"])
        if row["name"] == VECTOR_INDEX_NAME:
            vector_index = dict(row)
    return SchemaReport(constraints=constraints, indexes=indexes, vector_index=vector_index)


def rebuild_vector_index(
    session: Any,
    *,
    dimensions: int = VECTOR_INDEX_DIMENSIONS,
    similarity: str = VECTOR_INDEX_SIMILARITY,
    timeout: int = 300,
) -> SchemaReport:
    """Drop and re-create the vector index. **Only** for a provider dimension change.

    This is the one function in the module that drops anything, and it exists for exactly
    one situation: a re-embed onto a provider whose vectors are a different width, where the
    old index is structurally wrong and ``IF NOT EXISTS`` would therefore keep the wrong
    thing. It is opt-in from :func:`ingest.graph.reembed.main` (``--rebuild-index``) and is
    never reached from :func:`apply_schema`.

    It is explicitly **not** the way to handle an error from re-running index creation.
    Re-running :data:`VECTOR_INDEX_STATEMENT` cannot error — it carries ``IF NOT EXISTS`` —
    so an error there means something else is wrong and dropping the index would destroy a
    working index while hiding the real fault.

    Args:
        session: an open ``neo4j.Session``.
        dimensions: the width to build the new index for. Defaults to D6's 1024.
        similarity: the similarity function. Defaults to D6's ``cosine``.
        timeout: seconds to wait for the fresh index to populate.

    Returns:
        A :class:`SchemaReport` read back after the rebuild.

    Raises:
        RuntimeError: the rebuilt index did not come back with the requested parameters.
            Read back rather than assumed: this function is the last line of defence before
            an entire catalog is embedded into an index that cannot match it.
    """
    session.run(f"DROP INDEX {VECTOR_INDEX_NAME} IF EXISTS").consume()
    session.run(vector_index_statement(dimensions, similarity)).consume()
    await_indexes(session, timeout=timeout)
    report = schema_report(session)
    if report.vector_dimensions != dimensions or report.vector_similarity != similarity.lower():
        raise RuntimeError(
            f"rebuilt {VECTOR_INDEX_NAME} reports "
            f"{report.vector_dimensions}d/{report.vector_similarity}, expected "
            f"{dimensions}d/{similarity}"
        )
    return report


# ---------------------------------------------------------------------------------------
# What the live index actually holds
# ---------------------------------------------------------------------------------------

#: The label of the marker node that records **which provider wrote the vectors currently in
#: the index**, and whether the pass that wrote them finished.
#:
#: Deliberately not a member of :data:`~ingest.graph.model.MATERIAL_FACT_LABELS`,
#: :data:`~ingest.graph.model.VOCABULARY_LABELS` or :data:`~ingest.graph.model.ID_PROPERTY`:
#: it asserts nothing about the world, so it is not a fact needing a ``Source``, and it is
#: not a term. It is operational metadata *about the index*, which is why it lives here
#: beside the index definition rather than in the catalog model.
#:
#: Why it has to exist at all: ``HashEmbedding.dimension`` and ``LocalBgeEmbedding.dimension``
#: are both :data:`~ingest.graph.model.EMBEDDING_DIMENSIONS`, so the width guard in
#: :func:`ingest.graph.reembed.reembed_products` **cannot fire** for the ``hash`` →
#: ``local_bge`` swap ``make e2e-live`` actually performs. Measured: re-embedding with a
#: second valid 1024-d provider and then querying with the default one raised nothing, left
#: ``products_missing_embeddings() == []`` and the index ``ONLINE``, and silently re-ranked
#: the catalog. Nothing in the graph recorded who wrote the vectors — so now something does.
EMBEDDING_RUN_LABEL = "EmbeddingRun"

#: A pass that has started writing and has **not reached its end**. A marker left in this
#: state is the *only* evidence that an interrupted pass has mixed two vector spaces into
#: one index — the per-product writes auto-commit, so nothing rolls back. This is the one
#: state a vector query cannot be answered under, because "which space is this vector in"
#: has no single answer for the index as a whole.
#:
#: It is written by the opening stamp and cleared by the closing one. A pass that *reaches*
#: its end never leaves it here, however many products it had to skip: see
#: :data:`EMBEDDING_RUN_DEGRADED`.
EMBEDDING_RUN_RUNNING = "running"

#: A pass that reached its end and wrote every product it read — and nothing weaker.
#: Stamped from :attr:`ingest.graph.reembed.ReembedReport.complete`, so the marker in the
#: graph and the report handed back to the operator cannot disagree about the same pass.
EMBEDDING_RUN_COMPLETE = "complete"

#: A pass that reached its end having **skipped** products it could not embed (T-116).
#:
#: This is a *terminal* state and deliberately not :data:`EMBEDDING_RUN_COMPLETE`: the pass
#: did not cover the whole catalog, :attr:`EmbeddingRun.complete` stays ``False``, and the
#: ids it could not embed are recorded on the marker. What it is *not* is the mixed-space
#: hazard :data:`EMBEDDING_RUN_RUNNING` describes. Every vector now in the index was written
#: by this pass's provider — a skipped product's stale vector is *removed*, not left behind
#: (:func:`ingest.graph.reembed.reembed_products`) — so cosine across the index is
#: single-space and honest, and the skipped products are simply absent from it.
#:
#: Why the distinction has to exist: folding the two into one state made a *catalog-wide*
#: outage out of a *one-product* fact. One Product whose ``canonical_name`` went empty
#: upstream left the marker open, and :func:`ingest.graph.query.candidate_products` then
#: refused every vector query in the system — with a remediation (re-run the pass) that
#: skips the same product again and so never terminates. The degradation belongs to that
#: product: it has no vector, :func:`ingest.graph.query.products_missing_embeddings` names
#: it, and the rest of the catalog stays retrievable.
EMBEDDING_RUN_DEGRADED = "degraded"

#: The states that mean "this pass reached its end", i.e. every vector in the index was
#: written by the recorded provider. The complement is exactly
#: :data:`EMBEDDING_RUN_RUNNING`, and the complement is what a vector query must refuse on.
EMBEDDING_RUN_FINISHED_STATES = frozenset({EMBEDDING_RUN_COMPLETE, EMBEDDING_RUN_DEGRADED})

_RECORD_EMBEDDING_RUN = f"""
MERGE (r:{EMBEDDING_RUN_LABEL} {{index: $index}})
SET r.provider = $provider,
    r.dimension = $dimension,
    r.state = $state,
    r.products = $products,
    r.embedded = $embedded,
    r.skipped = $skipped
"""

_READ_EMBEDDING_RUN = f"""
MATCH (r:{EMBEDDING_RUN_LABEL} {{index: $index}})
RETURN r.provider AS provider, r.dimension AS dimension, r.state AS state,
       r.products AS products, r.embedded AS embedded, r.skipped AS skipped
LIMIT 1
"""


@dataclass(frozen=True)
class EmbeddingRun:
    """Who wrote the vectors in one vector index, and how far the pass got."""

    index: str
    provider: str
    dimension: int
    state: str
    products: int = 0
    embedded: int = 0
    #: The product ids the pass read but could not embed, sorted. Non-empty exactly when
    #: :data:`state` is :data:`EMBEDDING_RUN_DEGRADED`. Recorded rather than counted so the
    #: operator is handed the finite list of catalog rows to fix, which is what makes the
    #: remediation terminate.
    skipped: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True when the recorded pass covered the whole catalog.

        The strong claim, and the one an audit wants: the pass reached its end **and**
        embedded every product it read. A :data:`EMBEDDING_RUN_DEGRADED` pass is ``False``
        here — it left products unembedded — which is why the marker can never certify a
        partial pass as whole.
        """
        return self.state == EMBEDDING_RUN_COMPLETE

    @property
    def finished(self) -> bool:
        """True when the recorded pass reached its end, however many products it skipped.

        The *weaker* claim, and the only one a vector query needs: every vector currently in
        the index was written by :attr:`provider`, so cosine over it is single-space. A
        query must not read :attr:`complete` for this — doing so turned one unembeddable
        product into a refusal of every vector query in the catalog (T-116).
        """
        return self.state in EMBEDDING_RUN_FINISHED_STATES


def record_embedding_run(
    session: Any,
    *,
    provider: str,
    dimension: int,
    state: str,
    products: int = 0,
    embedded: int = 0,
    skipped: Sequence[str] = (),
    index: str = VECTOR_INDEX_NAME,
) -> EmbeddingRun:
    """Stamp the index with the identity of the pass writing into it.

    One marker per index (``MERGE`` on ``index``), overwritten by each pass — the question
    it answers is "what is in there *now*", not "what has ever been in there". D38 makes the
    single Neo4j database serialised by an ``flock``, so there is one writer and the
    unconstrained ``MERGE`` cannot race.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        provider: :attr:`ingest.embeddings.EmbeddingProvider.name` of the writer.
        dimension: the width of the vectors being written.
        state: :data:`EMBEDDING_RUN_RUNNING`, :data:`EMBEDDING_RUN_COMPLETE` or
            :data:`EMBEDDING_RUN_DEGRADED`.
        products: how many products the pass read.
        embedded: how many it wrote.
        skipped: the ids it read but could not embed. Written on every stamp, including the
            opening one, so the *previous* pass's skip list cannot survive into this pass's
            marker and name products this pass embedded fine.
        index: the vector index the marker describes.

    Returns:
        The :class:`EmbeddingRun` as recorded.
    """
    skipped_ids = tuple(str(product_id) for product_id in skipped)
    session.run(
        _RECORD_EMBEDDING_RUN,
        index=index,
        provider=provider,
        dimension=int(dimension),
        state=state,
        products=int(products),
        embedded=int(embedded),
        skipped=list(skipped_ids),
    ).consume()
    return EmbeddingRun(
        index=index,
        provider=provider,
        dimension=int(dimension),
        state=state,
        products=int(products),
        embedded=int(embedded),
        skipped=skipped_ids,
    )


def embedding_run(session: Any, *, index: str = VECTOR_INDEX_NAME) -> EmbeddingRun | None:
    """Read back who wrote the vectors currently in ``index``.

    Args:
        session: an open ``neo4j.Session``.
        index: the vector index to ask about.

    Returns:
        The :class:`EmbeddingRun`, or ``None`` when no pass has been recorded. ``None`` is
        not an error: a graph seeded directly through
        :func:`ingest.graph.upsert.set_product_embedding` (which is what the adapter tickets
        do) has vectors whose provenance this library genuinely does not know, and inventing
        an answer there would be worse than admitting it.
    """
    row = session.run(_READ_EMBEDDING_RUN, index=index).single()
    if row is None:
        return None
    return EmbeddingRun(
        index=index,
        provider=str(row["provider"]),
        dimension=int(row["dimension"]),
        state=str(row["state"]),
        products=int(row["products"] or 0),
        embedded=int(row["embedded"] or 0),
        # `or ()` and not a required property: a marker written before `skipped` existed
        # reads back NULL, and an old marker must degrade to "no skips recorded" rather
        # than crash the read path that every vector query goes through.
        skipped=tuple(str(product_id) for product_id in (row["skipped"] or ())),
    )


__all__ = [
    "ADAPTER_LOOKUP_INDEXES",
    "EMBEDDING_PROPERTY",
    "EMBEDDING_RUN_COMPLETE",
    "EMBEDDING_RUN_DEGRADED",
    "EMBEDDING_RUN_FINISHED_STATES",
    "EMBEDDING_RUN_LABEL",
    "EMBEDDING_RUN_RUNNING",
    "LOOKUP_INDEXES",
    "QUERY_PREDICATE_INDEXES",
    "VECTOR_INDEX_DIMENSIONS",
    "VECTOR_INDEX_NAME",
    "VECTOR_INDEX_SIMILARITY",
    "VECTOR_INDEX_STATEMENT",
    "EmbeddingRun",
    "SchemaReport",
    "apply_schema",
    "await_indexes",
    "constraint_name",
    "embedding_run",
    "constraint_statements",
    "lookup_index_statements",
    "rebuild_vector_index",
    "record_embedding_run",
    "schema_report",
    "schema_statements",
    "vector_index_statement",
]
