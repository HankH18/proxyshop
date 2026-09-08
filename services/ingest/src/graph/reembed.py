"""Re-embed every ``Product`` in the graph with the configured provider (T-012, acceptance 3).

DESIGN, §Cross-cutting: "swapping providers = re-embed + rebuild index script (shipped)".
This is that script. Run it after changing ``EMBEDDING_PROVIDER``::

    EMBEDDING_PROVIDER=local_bge python -m ingest.graph.reembed --rebuild-index

The provider swap itself is **config-only**: nothing here names a provider class. The name
travels from the environment through :func:`~ingest.embeddings.get_embedding_provider`, and
``--provider`` exists only so an operator can re-embed onto one provider without exporting a
variable that would then also change the read path mid-run.

What text gets embedded
-----------------------
:func:`embedding_text` composes the product's *structured* context — canonical name, brand,
categories, attribute readings, ingredients — rather than the name alone. That is the same
"no free-text-only matching" clause seen from the write side: a vector built from the name
by itself makes the index a fuzzy name matcher wearing a cosine hat, and two unrelated
products with similar names outrank the right product with the right attributes.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..embeddings import EmbeddingProvider, get_embedding_provider
from .schema import (
    EMBEDDING_RUN_COMPLETE,
    EMBEDDING_RUN_DEGRADED,
    EMBEDDING_RUN_RUNNING,
    VECTOR_INDEX_NAME,
    apply_schema,
    await_indexes,
    embedding_run,
    rebuild_vector_index,
    record_embedding_run,
    schema_report,
)
from .upsert import EmbeddingDimensionMismatch, clear_product_embedding, set_product_embedding

#: This module's logger. Named ``ingest.graph.reembed``, which is what an operator greps when
#: a refresh or a re-embed run cannot open a session.
_log = logging.getLogger(__name__)

#: How many products to pull per round trip.
DEFAULT_BATCH_SIZE = 200


@dataclass(frozen=True)
class ReembedReport:
    """The outcome of one re-embed pass."""

    provider: str
    dimension: int
    products: int
    embedded: int
    skipped: list[str]

    @property
    def complete(self) -> bool:
        """True when every product now carries a vector."""
        return not self.skipped and self.embedded == self.products


#: One product page with the structured context :func:`embedding_text` composes from. The
#: ``__SELECTOR__`` hole is the only difference between the whole-catalog pass and the
#: named-subset one, so the two embed **identical** text for the same product and their
#: vectors stay comparable inside one index. Two copies of this Cypher would not stay that
#: way: a category added to one and not the other silently splits the space.
_READ_PRODUCTS_TEMPLATE = """
MATCH (p:Product)
__SELECTOR__
OPTIONAL MATCH (p)-[:IN_CATEGORY]->(c:Category)
WITH p, collect(DISTINCT c.name) AS categories
OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:AttributeValue)
WITH p, categories, collect(DISTINCT {
    key: a.key, value_string: a.value_string, value_number: a.value_number,
    value_bool: a.value_bool, unit: a.unit
}) AS attributes
OPTIONAL MATCH (p)-[:CONTAINS]->(i:Ingredient)
WITH p, categories, attributes, collect(DISTINCT i.name) AS ingredients
RETURN p.product_id AS product_id,
       coalesce(p.canonical_name, '') AS canonical_name,
       coalesce(p.brand, '') AS brand,
       [x IN categories WHERE x IS NOT NULL] AS categories,
       [x IN attributes WHERE x.key IS NOT NULL] AS attributes,
       [x IN ingredients WHERE x IS NOT NULL] AS ingredients
ORDER BY product_id
SKIP $skip LIMIT $limit
"""

#: The whole-catalog page, unchanged: every ``Product`` in the graph, ordered and paged.
_READ_PRODUCTS = _READ_PRODUCTS_TEMPLATE.replace("__SELECTOR__", "")

#: The named-subset page. ``$product_ids`` is an explicit list rather than a store filter
#: because the crawl knows exactly which products it wrote and a store-shaped predicate
#: would re-embed a store's whole catalog on every refresh of one changed product.
_READ_NAMED_PRODUCTS = _READ_PRODUCTS_TEMPLATE.replace(
    "__SELECTOR__", "WHERE p.product_id IN $product_ids"
)


def read_products(
    session: Any, *, skip: int = 0, limit: int = DEFAULT_BATCH_SIZE
) -> list[dict[str, Any]]:
    """Read one page of products with the structured context :func:`embedding_text` needs.

    Public because it is also what an adapter or a verifier wants when it needs to see a
    product exactly as the embedder does — reaching into a private constant to get that
    would couple every caller to this module's internals.

    Args:
        session: an open ``neo4j.Session``.
        skip: how many products to skip, ordered by ``product_id``.
        limit: page size.

    Returns:
        Mappings with ``product_id``, ``canonical_name``, ``brand``, ``categories``,
        ``attributes`` and ``ingredients``, ordered by ``product_id``.
    """
    return list(session.run(_READ_PRODUCTS, skip=int(skip), limit=int(limit)).data())


def _render_attribute(attribute: dict[str, Any]) -> str:
    """Render one attribute reading as a short ``key: value`` phrase.

    Args:
        attribute: a mapping with ``key`` and the value components.

    Returns:
        e.g. ``"spf: 50"``, ``"fragrance free: yes"``, ``"volume: 30 ml"``.
    """
    key = str(attribute.get("key") or "").strip()
    if attribute.get("value_string") is not None:
        value = str(attribute["value_string"])
    elif attribute.get("value_number") is not None:
        number = float(attribute["value_number"])
        value = str(int(number)) if number == int(number) else str(number)
        if attribute.get("unit"):
            value = f"{value} {attribute['unit']}"
    elif attribute.get("value_bool") is not None:
        value = "yes" if attribute["value_bool"] else "no"
    else:  # pragma: no cover - AttributeValue rejects this at construction
        value = ""
    return f"{key}: {value}".strip()


def embedding_text(row: dict[str, Any]) -> str:
    """Compose the text embedded for one product.

    Args:
        row: a mapping with ``canonical_name``, ``brand``, ``categories``, ``attributes``
            and ``ingredients`` — the shape :data:`_READ_PRODUCTS` returns.

    Returns:
        A single newline-joined document. Deterministic: every list component is sorted, so
        two ingests that discovered the same facts in a different order embed identically
        and the vectors stay comparable across re-runs.
    """
    parts = [str(row.get("canonical_name") or "").strip()]
    if row.get("brand"):
        parts.append(f"brand: {row['brand']}")
    if row.get("categories"):
        parts.append("categories: " + ", ".join(sorted(str(c) for c in row["categories"])))
    rendered = sorted(_render_attribute(a) for a in row.get("attributes") or ())
    if rendered:
        parts.append("attributes: " + "; ".join(rendered))
    if row.get("ingredients"):
        parts.append("ingredients: " + ", ".join(sorted(str(i) for i in row["ingredients"])))
    return "\n".join(part for part in parts if part)


_STILL_EMBEDDED = """
MATCH (p:Product)
WHERE p.product_id IN $product_ids AND p.embedding IS NOT NULL
RETURN p.product_id AS product_id
ORDER BY product_id
"""


def _still_embedded(session: Any, product_ids: Sequence[str]) -> list[str]:
    """Which of ``product_ids`` still carry a vector.

    The read-back behind :data:`~ingest.graph.schema.EMBEDDING_RUN_DEGRADED`. That state
    tells every vector query "one space in this index, go ahead", and the only thing making
    that true for a pass that skipped products is that the skipped products' vectors were
    actually removed. This asks the graph instead of trusting the loop.

    Args:
        session: an open ``neo4j.Session``.
        product_ids: the ids to check. An empty sequence short-circuits without a round trip.

    Returns:
        The ids that still have an ``embedding`` property, sorted. Empty is the healthy
        answer after a pass that skipped them.
    """
    if not product_ids:
        return []
    return [
        row["product_id"]
        for row in session.run(_STILL_EMBEDDED, product_ids=list(product_ids)).data()
    ]


def _live_index_width(session: Any, provider: EmbeddingProvider) -> int:
    """The width the **live** ``product_embedding`` index was built for.

    Not the width D6 pins: after an explicit ``rebuild_vector_index(dimensions=N)`` those
    differ, and the vectors have to match the index that exists rather than the one the
    decision describes.

    Args:
        session: an open ``neo4j.Session``.
        provider: the provider about to write vectors.

    Returns:
        The live index's dimension.

    Raises:
        RuntimeError: the index does not exist, so every vector written would be unreachable.
        EmbeddingDimensionMismatch: the provider emits a different width. Neo4j would accept
            every write and then match none of them.
    """
    index_dimensions = schema_report(session).vector_dimensions
    if index_dimensions is None:
        raise RuntimeError(
            f"the {VECTOR_INDEX_NAME} index does not exist; call apply_schema(session) "
            f"before embedding, or every vector written here is unreachable"
        )
    if provider.dimension != index_dimensions:
        raise EmbeddingDimensionMismatch(
            f"provider {provider.name!r} emits {provider.dimension}-d vectors but the live "
            f"{VECTOR_INDEX_NAME} index is {index_dimensions}-d. Neo4j would accept every "
            f"write and then match none of them. Rebuild the index for this width first: "
            f"python -m ingest.graph.reembed --provider {provider.name} --rebuild-index"
        )
    return int(index_dimensions)


def _embed_page(
    session: Any,
    rows: Sequence[dict[str, Any]],
    *,
    provider: EmbeddingProvider,
    dimensions: int,
) -> tuple[list[str], list[str]]:
    """Embed one page of product rows. The single write loop both passes share.

    Args:
        session: an open ``neo4j.Session``.
        rows: product rows in :data:`_READ_PRODUCTS_TEMPLATE`'s shape.
        provider: the provider to embed with.
        dimensions: the live index width, already checked against ``provider``.

    Returns:
        ``(embedded_ids, skipped_ids)`` for this page, in row order.
    """
    texts = [embedding_text(row) for row in rows]
    vectors = provider.embed_batch(texts)
    embedded: list[str] = []
    skipped: list[str] = []
    for row, text, vector in zip(rows, texts, vectors, strict=True):
        if not text:
            # A product with no name and no structured context embeds to the zero vector,
            # which cosine cannot rank. Leaving it unembedded and *reported* is honest;
            # writing a zero vector would make it silently unreachable instead.
            #
            # "Unembedded" has to be made true, not merely intended. Skipping the write and
            # moving on leaves whatever vector a PREVIOUS pass wrote sitting on the product
            # — in the previous provider's space, inside an index this pass is filling with
            # a different one. Measured: the stale row still ranks (0.4911 against a
            # legitimate 0.4953) while `products_missing_embeddings()`,
            # `products_missing_status()` and `provenance_violations()` all report ``[]``,
            # because the product *has* an embedding and a vector is not a material fact.
            # Removing it hands that product to the one detector that needs no marker at
            # all, and costs nothing recoverable: there was no text to re-embed it from.
            skipped.append(row["product_id"])
            clear_product_embedding(session, product_id=row["product_id"])
            continue
        set_product_embedding(
            session, product_id=row["product_id"], embedding=vector, dimensions=dimensions
        )
        embedded.append(row["product_id"])
    return embedded, skipped


def embed_products(
    session: Any,
    product_ids: Sequence[str],
    provider: EmbeddingProvider | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    await_index: bool = True,
) -> ReembedReport:
    """Embed a **named set** of products — what a crawl owes the graph it just wrote.

    :func:`reembed_products` is the operator's whole-catalog script and cannot be what a
    per-store refresh calls: it re-reads and re-embeds every product of every store on every
    crawl, and its :class:`~ingest.graph.schema.EmbeddingRun` marker is a claim about the
    whole catalog. This is the incremental half. Same composition
    (:data:`_READ_PRODUCTS_TEMPLATE`), same provider resolution, same width guard, same
    per-row skip rule — a different selector.

    **It deliberately writes no ``EmbeddingRun`` marker.** The marker answers "who filled
    this index, and did the pass cover the catalog"; a run that read four products of nine
    hundred can answer neither, and stamping ``complete`` after it would certify a coverage
    claim nobody checked. What the marker does instead is *constrain* this call: if a
    recorded pass names another provider, embedding here would put a second vector space
    into an index whose marker says it holds one, and cosine across two spaces is noise. So
    that case refuses rather than writes, the products stay visibly unembedded, and
    :func:`ingest.graph.query.products_missing_embeddings` — which needs no marker at all —
    is what names them.

    Args:
        session: an open ``neo4j.Session``.
        product_ids: the products to embed. Ids that name no ``Product`` are ignored, so a
            caller may pass the ids a write *attempted*; the report counts what was read.
        provider: the provider to embed with. Defaults to
            :func:`~ingest.embeddings.get_embedding_provider`, i.e. to ``EMBEDDING_PROVIDER``
            — the same resolution the read side uses, which is what keeps D19's provider
            swap config-only on this path too.
        batch_size: products per round trip.
        await_index: wait for the vector index to absorb the writes, so a query issued
            straight after this call sees them. That immediacy is the whole point on the
            crawl path: the refresh reports "these products are retrievable now".

    Returns:
        A :class:`ReembedReport` over the named subset. ``products`` is how many of
        ``product_ids`` existed, not how many were asked for.

    Raises:
        ValueError: ``batch_size`` is not positive.
        RuntimeError: the vector index does not exist.
        EmbeddingDimensionMismatch: the provider's width is not the live index's.
        ~ingest.graph.query.EmbeddingProviderMismatch: a recorded pass filled this index with
            a different provider's vectors.
    """
    from .query import EmbeddingProviderMismatch  # noqa: PLC0415 - read side, imported lazily

    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    resolved = provider or get_embedding_provider()
    index_dimensions = _live_index_width(session, resolved)

    recorded = embedding_run(session)
    if recorded is not None and recorded.provider != resolved.name:
        raise EmbeddingProviderMismatch(
            f"refusing to embed {len(product_ids)} product(s) with {resolved.name!r}: the "
            f"last recorded pass over {recorded.index} was {recorded.provider!r}, so these "
            f"vectors would form a SECOND space inside an index whose marker says it holds "
            f"one, and every cosine across the two is noise. Re-embed the catalog first: "
            f"EMBEDDING_PROVIDER={resolved.name} python -m ingest.graph.reembed"
        )

    wanted = list(dict.fromkeys(str(product_id) for product_id in product_ids))
    products = 0
    embedded: list[str] = []
    skipped: list[str] = []
    skip = 0
    while wanted:
        rows = session.run(
            _READ_NAMED_PRODUCTS, product_ids=wanted, skip=skip, limit=batch_size
        ).data()
        if not rows:
            break
        page_embedded, page_skipped = _embed_page(
            session, rows, provider=resolved, dimensions=index_dimensions
        )
        products += len(rows)
        embedded.extend(page_embedded)
        skipped.extend(page_skipped)
        skip += len(rows)
    if await_index and embedded:
        await_indexes(session)
    return ReembedReport(
        provider=resolved.name,
        dimension=index_dimensions,
        products=products,
        embedded=len(embedded),
        skipped=sorted(skipped),
    )


def reembed_products(
    session: Any,
    provider: EmbeddingProvider | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    await_index: bool = True,
) -> ReembedReport:
    """Recompute and store ``Product.embedding`` for every product in the graph.

    Records an :class:`~ingest.graph.schema.EmbeddingRun` marker against the vector index so
    that both "which provider wrote these vectors" and "did the pass cover the whole
    catalog" are readable from the graph rather than assumed. Three states, and the
    distinction between the last two is load-bearing:

    * ``running`` before the first batch — and still ``running`` if the pass never returns,
      which is the only evidence that an interrupted pass mixed two vector spaces into one
      index. :func:`ingest.graph.query.candidate_products` refuses every vector query here.
    * ``complete`` after the last batch when the pass embedded **every** product it read.
    * ``degraded`` after the last batch when it could not, naming the products it skipped.
      The index still holds exactly one vector space, so vector queries are answered; the
      skipped products simply have no vector and
      :func:`ingest.graph.query.products_missing_embeddings` names them. One unembeddable
      product degrades itself, not the catalog (T-116).

      With one exception, and it is the floor of that narrowing: when ``embedded == 0`` —
      every product read was unembeddable — ``degraded`` is still recorded and the index is
      still single-space, but single-space and *queryable* part company, because there is
      no vector in it to rank. :func:`ingest.graph.query.candidate_products` refuses with
      :class:`~ingest.graph.query.EmbeddingIndexEmpty` there rather than answering ``[]``,
      and :func:`main` exits ``3`` and says so instead of printing the advisory that the
      catalog is queryable without the skipped rows.

    A product whose composed text is empty is skipped, and any vector a previous pass left
    on it is *removed* rather than left behind in that pass's vector space. That removal is
    what makes ``degraded`` honest: it is the reason the index is single-space even when the
    pass did not cover everything.

    Args:
        session: an open ``neo4j.Session``.
        provider: the provider to embed with. Defaults to
            :func:`~ingest.embeddings.get_embedding_provider`, i.e. to ``EMBEDDING_PROVIDER``.
        batch_size: products per round trip.
        await_index: wait for the vector index to finish absorbing the writes before
            returning, so a query issued straight after this call sees them.

    Returns:
        A :class:`ReembedReport`.

    Raises:
        ValueError: ``batch_size`` is not positive.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    resolved = provider or get_embedding_provider()
    index_dimensions = _live_index_width(session, resolved)
    # Stamp the index BEFORE the first batch, and again after the last.
    #
    # Two failures this closes, one needing an operator mistake and one needing only a
    # network blip:
    #
    # * A second *valid* 1024-d provider can re-embed the whole catalog and the width guard
    #   above cannot fire, because every registered provider declares
    #   ``EMBEDDING_DIMENSIONS``. Recording ``provider`` is what lets
    #   :func:`ingest.graph.query.candidate_products` notice it is reading someone else's
    #   vector space instead of silently returning a re-ranked catalog.
    # * The per-product writes below auto-commit and are paged with SKIP/LIMIT, so a failure
    #   on page two leaves half the catalog in the new space and half in the old, with
    #   ``products_missing_embeddings() == []``. Nothing rolls back — a pass-spanning
    #   transaction over an arbitrarily large catalog is not the answer either — so the
    #   marker is left in :data:`~ingest.graph.schema.EMBEDDING_RUN_RUNNING` and the
    #   half-finished state becomes *visible* rather than silent.
    record_embedding_run(
        session,
        provider=resolved.name,
        dimension=index_dimensions,
        state=EMBEDDING_RUN_RUNNING,
    )
    products = 0
    embedded = 0
    skipped: list[str] = []
    skip = 0
    while True:
        rows = session.run(_READ_PRODUCTS, skip=skip, limit=batch_size).data()
        if not rows:
            break
        page_embedded, page_skipped = _embed_page(
            session, rows, provider=resolved, dimensions=index_dimensions
        )
        products += len(rows)
        embedded += len(page_embedded)
        skipped.extend(page_skipped)
        skip += len(rows)
    report = ReembedReport(
        provider=resolved.name,
        dimension=resolved.dimension,
        products=products,
        embedded=embedded,
        skipped=sorted(skipped),
    )
    # The marker records what the REPORT says, not merely that the loop reached its end.
    # Stamping `complete` unconditionally certified a pass the report itself called
    # incomplete: `_check_vector_path` then found a finished marker naming the right
    # provider and raised nothing, so a catalog holding one product's worth of a foreign
    # vector space answered queries as if it were whole. `ReembedReport.complete` has always
    # been the honest reading of the same pass — the marker is now derived from it, so the
    # two cannot disagree, and `main()`'s non-zero exit and the graph's own state say the
    # same thing to an operator who reads only one of them.
    #
    # But `not complete` is NOT `running`, and conflating them (T-116) cost far more than
    # the defect it closed. Reaching this line means the pass walked the whole catalog and
    # every vector now in the index came from `resolved` — including for the skipped
    # products, whose stale vectors were REMOVED above rather than left in a foreign space.
    # There is exactly one vector space in the index either way, which is the only question
    # a vector query has to ask. `running` means the opposite: the pass never got here, and
    # two spaces may be interleaved. So a finished-but-partial pass gets its own terminal
    # state, carrying the ids it could not embed. `complete` still means what T-101 made it
    # mean — the whole catalog — and `EmbeddingRun.complete` is still False here.
    #
    # And the "one vector space" claim is CHECKED rather than asserted. Narrowing the refusal
    # to `running` moves the entire weight of it onto the removal above: if a skipped product
    # kept a vector, `degraded` would be a lie of exactly the kind T-101 closed, and the
    # index really would hold two spaces behind a marker that says it is safe to query. So
    # the pass reads back what it claims. Where the claim does not hold, `running` is the
    # honest state and the refusal is the right outcome — the one place T-101's blanket
    # backstop was actually earning its cost.
    unresolved = _still_embedded(session, report.skipped)
    if unresolved:
        state = EMBEDDING_RUN_RUNNING
    elif report.complete:
        state = EMBEDDING_RUN_COMPLETE
    else:
        state = EMBEDDING_RUN_DEGRADED
    record_embedding_run(
        session,
        provider=resolved.name,
        dimension=index_dimensions,
        state=state,
        products=products,
        embedded=embedded,
        skipped=report.skipped,
    )
    if await_index:
        await_indexes(session)
    return report


@contextmanager
def graph_driver() -> Iterator[Any]:
    """Open a ``neo4j.Driver`` from ``NEO4J_URI`` / ``NEO4J_USER`` / ``NEO4J_PASSWORD``.

    D41: the connection details are environment-supplied, never literals in a test. Resolved
    through :func:`proxyshop_support.neo4j_auth.graph_credentials`, which is the ONLY place in
    the tree that reads those three names and supplies a fallback.

    The three defaults used to be spelled here, and the docstring claimed "the fallbacks here
    match ``.env.example``" — true of this function and false of the tree, because
    ``proxyshop_support/service_launch.py``'s readiness probe defaulted the password to ``""``
    while this one and ``exchange.retrieval.roster``'s used ``proxyshop_dev_pw``. Two of the
    three were on served paths (``scheduler.catalog.graph_session`` is this function's caller
    behind ``POST /refresh/{store_id}``), so "which password does this deployment use" had no
    single answer. It has one now, and a failure says which — see
    :meth:`~proxyshop_support.neo4j_auth.GraphCredentials.describe`.

    Yields:
        A connected driver, closed on exit.

    Raises:
        Exception: whatever the driver raises when the server refuses the credential — the
            type is left alone so a caller catching ``neo4j.exceptions.AuthError`` still
            does — with :meth:`~proxyshop_support.neo4j_auth.GraphCredentials.describe`'s
            account of WHERE that credential came from logged beside it. The password itself
            never reaches a log or a message.
    """
    from neo4j import GraphDatabase

    from proxyshop_support.neo4j_auth import graph_credentials

    credentials = graph_credentials()
    driver = GraphDatabase.driver(credentials.uri, auth=credentials.auth, connection_timeout=5)
    try:
        try:
            driver.verify_connectivity()
        except Exception:
            # The driver's own message names the server's complaint and NOT the credential
            # this process offered, which is the difference between "authentication failed"
            # and a diagnosis. Logged rather than wrapped: re-raising a different exception
            # type here would break every caller that catches `neo4j.exceptions.AuthError`,
            # and the operator needs both halves anyway.
            _log.error("neo4j refused a session: %s", credentials.describe())
            raise
        yield driver
    finally:
        driver.close()


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser.

    Returns:
        An ``ArgumentParser`` for ``python -m ingest.graph.reembed``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ingest.graph.reembed",
        description=(
            "Re-embed every Product with the configured EMBEDDING_PROVIDER, and optionally "
            "rebuild the D6 vector index (needed only when the new provider's dimension "
            "differs from the live index's)."
        ),
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="override EMBEDDING_PROVIDER for this run (default: read the environment)",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help=(
            "DROP and re-create the product_embedding index before embedding. Correct only "
            "for a dimension change; never a fix for an error re-running index creation."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the re-embed pass against the configured Neo4j.

    Args:
        argv: command-line arguments; ``sys.argv[1:]`` when omitted.

    Returns:
        The status is read back out of the **graph** — the
        :class:`~ingest.graph.schema.EmbeddingRun` marker every vector query consults —
        rather than inferred from this process's report, so the operator's exit code and the
        catalog's own state cannot disagree about the same pass.

        * ``0`` — the pass reached a terminal state the vector path really can be queried
          under: :data:`~ingest.graph.schema.EMBEDDING_RUN_COMPLETE` when it embedded every
          product, or :data:`~ingest.graph.schema.EMBEDDING_RUN_DEGRADED` when it embedded
          **at least one** and said on stderr which it could not. **Not** an error, and this
          is load-bearing: this command is
          the remediation :class:`~ingest.graph.query.EmbeddingRunIncomplete` names, and it
          used to return ``1`` for exactly the state that message sends the operator to.
          An operator (or a CI step) reading a non-zero status as "it failed, run it again"
          then re-ran a pass that had already done its job, which is the same fixed point
          T-118(c) removed from the prose, re-entered through the exit code. Products with
          no embeddable text are a catalog edit, and stderr says so.
        * ``1`` — the pass reached its end but its read-back found products it skipped still
          carrying a vector, so the marker is
          :data:`~ingest.graph.schema.EMBEDDING_RUN_RUNNING` and vector queries refuse.
          Re-running does not fix this; the stale vectors have to go.
        * ``2`` — the provider's width does not match the live index. Nothing was written.
        * ``3`` — the pass reached its end and embedded **nothing**: it read products and
          every one of them was unembeddable, so :data:`~ingest.graph.schema.VECTOR_INDEX_NAME`
          now holds no vector at all and
          :class:`~ingest.graph.query.EmbeddingIndexEmpty` refuses every vector query. The
          marker is still ``degraded`` and the index is still single-space — this status is
          the one place where "single-space" stops implying "queryable", which is the floor
          of the T-116 per-product narrowing. Re-running is a fixed point: the same rows
          still have no text. The remediation is a catalog edit, and stderr says so.
    """
    args = build_parser().parse_args(argv)
    provider = get_embedding_provider(args.provider)
    with graph_driver() as driver, driver.session() as session:
        # UNCONDITIONAL, and the rebuild is an ADDITION to it rather than an alternative.
        # `--rebuild-index` used to take the `else` branch away from `apply_schema`, so
        # driving this CLI against a stripped schema returned rc 0 with zero constraints —
        # measured, and a second `CREATE (p:Product {product_id:'cli-1'})` then succeeded.
        # Every MERGE in this library is idempotent *because* a uniqueness constraint backs
        # it; without the constraints the whole upsert layer silently loses its backstop,
        # and the one command an operator runs after a schema problem was the command that
        # skipped fixing it.
        apply_schema(session)
        if args.rebuild_index:
            # Rebuild for THIS provider's width. Rebuilding at D6's 1024 for a 512-d
            # provider — which is what this did before — drops a working index, creates an
            # identical one, and then embeds the entire catalog into an index that can never
            # match it, with exit status 0.
            rebuild_vector_index(session, dimensions=provider.dimension)
        try:
            report = reembed_products(session, provider, batch_size=args.batch_size)
        except EmbeddingDimensionMismatch as exc:
            # Loud and non-zero. Warning and proceeding would leave the whole catalog
            # silently unretrievable while the command reported success.
            print(f"FATAL: {exc}", file=sys.stderr)
            return 2
        run = embedding_run(session)
    state = "unrecorded" if run is None else run.state
    print(
        f"provider={report.provider} dim={report.dimension} "
        f"products={report.products} embedded={report.embedded} "
        f"skipped={len(report.skipped)} state={state}"
    )
    for product_id in report.skipped:
        print(f"  skipped (no embeddable text): {product_id}", file=sys.stderr)
    if run is None or not run.finished:
        # The one genuinely non-zero outcome of a pass that returned: the read-back caught
        # skipped products that kept a vector, so the index holds a space this pass did not
        # write and every vector query refuses. Re-running is a fixed point here — see
        # `ingest.graph.query.EmbeddingRunIncomplete`.
        print(
            f"FATAL: the pass finished but {VECTOR_INDEX_NAME} is recorded as {state!r}: "
            f"products it skipped still carry a vector. Remove those vectors before "
            f"querying; re-running this command will reproduce the same state.",
            file=sys.stderr,
        )
        return 1
    if run.products > 0 and run.embedded == 0:
        # The floor of the T-116 narrowing, read off the marker with the SAME predicate
        # `ingest.graph.query._check_vector_path` evaluates, so the operator's status line
        # and the refusal a query gets on this identical graph cannot disagree.
        #
        # Falling through to the advisory below printed "The index is single-space and
        # queryable without them" about an index that answers NOTHING: every vector query
        # raises `EmbeddingIndexEmpty`. Both halves of that sentence were wrong at once —
        # the index is queryable *without* the skipped rows only while some other row is in
        # it, and here there is no other row. Exiting 0 said the same thing in the one
        # signal a CI step reads.
        print(
            f"FATAL: every product read was unembeddable, so {VECTOR_INDEX_NAME} now holds "
            f"no vectors at all. The marker records {state!r} and the index is still "
            f"single-space, but there is nothing in it to rank: every vector query refuses "
            f"with EmbeddingIndexEmpty instead of answering an empty shortlist. (A "
            f"structured query — attributes, category, ingredients, brand — never touches "
            f"the index and still works.) Re-running this command is a fixed point: the "
            f"same rows have no text to embed. Give the products listed above embeddable "
            f"text — that is a catalog edit — and then re-run it.",
            file=sys.stderr,
        )
        return 3
    if report.skipped:
        print(
            "  ^ these have no embeddable text. The rest of the catalog is single-space and "
            "queryable without them; fixing them is a catalog edit, not another re-embed.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "ReembedReport",
    "build_parser",
    "embedding_text",
    "graph_driver",
    "main",
    "read_products",
    "reembed_products",
]
