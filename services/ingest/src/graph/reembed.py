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
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..embeddings import EmbeddingProvider, get_embedding_provider
from .schema import (
    EMBEDDING_RUN_COMPLETE,
    EMBEDDING_RUN_RUNNING,
    VECTOR_INDEX_NAME,
    apply_schema,
    await_indexes,
    rebuild_vector_index,
    record_embedding_run,
    schema_report,
)
from .upsert import EmbeddingDimensionMismatch, set_product_embedding

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


_READ_PRODUCTS = """
MATCH (p:Product)
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


def reembed_products(
    session: Any,
    provider: EmbeddingProvider | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    await_index: bool = True,
) -> ReembedReport:
    """Recompute and store ``Product.embedding`` for every product in the graph.

    Records an :class:`~ingest.graph.schema.EmbeddingRun` marker against the vector index —
    ``running`` before the first batch, ``complete`` after the last — so that both "which
    provider wrote these vectors" and "did the pass finish" are readable from the graph
    rather than assumed. :func:`ingest.graph.query.candidate_products` refuses to answer a
    vector query that disagrees with it.

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
    # The width the LIVE index was built for, not the width D6 pins: after an explicit
    # `rebuild_vector_index(dimensions=N)` those differ, and the vectors have to match the
    # index that exists, not the one the decision describes.
    live = schema_report(session)
    index_dimensions = live.vector_dimensions
    if index_dimensions is None:
        raise RuntimeError(
            f"the {VECTOR_INDEX_NAME} index does not exist; call apply_schema(session) "
            f"before embedding, or every vector written here is unreachable"
        )
    if resolved.dimension != index_dimensions:
        raise EmbeddingDimensionMismatch(
            f"provider {resolved.name!r} emits {resolved.dimension}-d vectors but the live "
            f"{VECTOR_INDEX_NAME} index is {index_dimensions}-d. Neo4j would accept every "
            f"write and then match none of them. Rebuild the index for this width first: "
            f"python -m ingest.graph.reembed --provider {resolved.name} --rebuild-index"
        )
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
        texts = [embedding_text(row) for row in rows]
        vectors = resolved.embed_batch(texts)
        for row, text, vector in zip(rows, texts, vectors, strict=True):
            products += 1
            if not text:
                # A product with no name and no structured context embeds to the zero
                # vector, which cosine cannot rank. Leaving it unembedded and *reported* is
                # honest; writing a zero vector would make it silently unreachable instead.
                skipped.append(row["product_id"])
                continue
            set_product_embedding(
                session,
                product_id=row["product_id"],
                embedding=vector,
                dimensions=index_dimensions,
            )
            embedded += 1
        skip += len(rows)
    record_embedding_run(
        session,
        provider=resolved.name,
        dimension=index_dimensions,
        state=EMBEDDING_RUN_COMPLETE,
        products=products,
        embedded=embedded,
    )
    if await_index:
        await_indexes(session)
    return ReembedReport(
        provider=resolved.name,
        dimension=resolved.dimension,
        products=products,
        embedded=embedded,
        skipped=sorted(skipped),
    )


@contextmanager
def graph_driver() -> Iterator[Any]:
    """Open a ``neo4j.Driver`` from ``NEO4J_URI`` / ``NEO4J_USER`` / ``NEO4J_PASSWORD``.

    D41: the connection details are environment-supplied, never literals in a test. The
    fallbacks here match ``.env.example`` and exist so the CLI is runnable from a shell that
    has sourced nothing; test code takes the ``neo4j_driver`` fixture instead.

    Yields:
        A connected driver, closed on exit.
    """
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(
            os.environ.get("NEO4J_USER", "neo4j"),
            os.environ.get("NEO4J_PASSWORD", "proxyshop_dev_pw"),
        ),
        connection_timeout=5,
    )
    try:
        driver.verify_connectivity()
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
        ``0`` when every product was embedded, ``1`` when any product was skipped.
    """
    args = build_parser().parse_args(argv)
    provider = get_embedding_provider(args.provider)
    with graph_driver() as driver, driver.session() as session:
        if args.rebuild_index:
            # Rebuild for THIS provider's width. Rebuilding at D6's 1024 for a 512-d
            # provider — which is what this did before — drops a working index, creates an
            # identical one, and then embeds the entire catalog into an index that can never
            # match it, with exit status 0.
            rebuild_vector_index(session, dimensions=provider.dimension)
        else:
            apply_schema(session)
        try:
            report = reembed_products(session, provider, batch_size=args.batch_size)
        except EmbeddingDimensionMismatch as exc:
            # Loud and non-zero. Warning and proceeding would leave the whole catalog
            # silently unretrievable while the command reported success.
            print(f"FATAL: {exc}", file=sys.stderr)
            return 2
    print(
        f"provider={report.provider} dim={report.dimension} "
        f"products={report.products} embedded={report.embedded} skipped={len(report.skipped)}"
    )
    for product_id in report.skipped:
        print(f"  skipped (no embeddable text): {product_id}", file=sys.stderr)
    return 0 if report.complete else 1


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
