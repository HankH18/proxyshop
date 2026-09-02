"""Shared Neo4j catalog fixtures (T-012), auto-loaded by ``services/ingest/tests/conftest.py``.

T-020…T-024 and T-031 all write into or read from the graph this ticket builds, and all of
them need the same three things: a session whose schema is applied, a ``Source`` to hang
provenance off, and a small seeded catalog with embeddings. They live here rather than
inside ``test_graph.py`` so a sibling ticket can take them without importing another
ticket's test module.

Every name is ``graph_``-prefixed: seven tickets share this directory and
``scripts/check_verify_contracts.py`` fails the gate of whichever ticket introduces a
duplicate fixture name.

All three require ``@pytest.mark.docker`` (they reach the compose stack) and
``@pytest.mark.graph`` (they write). The D37 flock is taken for you by the root conftest's
``_neo4j_guard``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

#: A fixed instant, so nothing in a seeded graph depends on a wall clock.
GRAPH_OBSERVED_AT = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def graph_source() -> Any:
    """A valid :class:`~ingest.graph.model.Source` for provenanced writes.

    Returns:
        A ``Source`` with ``source_class="scraped"``, the ordinary adapter case.
    """
    from ingest.graph import Source

    return Source(
        source_id="src-catalog-fixture",
        url="https://store.example/collections/skincare",
        content_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        observed_at=GRAPH_OBSERVED_AT,
        extractor_version="fixture@1",
        confidence=0.9,
        source_class="scraped",
    )


@pytest.fixture
def graph_schema_session(neo4j_session: Any) -> Iterator[Any]:
    """A Neo4j session on an **empty** graph with the full T-012 schema applied.

    The graph is emptied per test rather than per session. ``reset_graph`` runs once, in the
    session-scoped ``neo4j_driver`` fixture, which is not enough when several tests in one
    file each seed products: the second test would retrieve the first test's rows and its
    "top-1" assertion would be measuring the wrong graph. Constraints and indexes survive
    ``DETACH DELETE`` and are re-applied here anyway, so a test that deliberately drops
    schema cannot leak into its neighbours.

    Args:
        neo4j_session: the root conftest's session, already inside the D37 flock.

    Yields:
        The same session, with an empty, fully-constrained graph.
    """
    from ingest.graph import apply_schema

    neo4j_session.run("MATCH (n) DETACH DELETE n").consume()
    apply_schema(neo4j_session)
    yield neo4j_session


#: The seeded catalog. Deliberately small, deliberately *structured*: every product carries
#: attributes and ingredients, because a candidate query that can only see names is the
#: thing DESIGN forbids and a fixture with name-only products could not detect it.
GRAPH_SAMPLE_PRODUCTS: tuple[dict[str, Any], ...] = (
    {
        "product_id": "prod-serum-c",
        "canonical_name": "Gentle Vitamin C Serum",
        "brand": "Northlight",
        "category": "Serum",
        "attributes": [
            ("spf", {"value_number": 0.0}),
            ("fragrance_free", {"value_bool": True}),
            ("volume", {"value_number": 30.0, "unit": "ml"}),
            ("skin_type", {"value_string": "Sensitive"}),
        ],
        "ingredients": ["Ascorbic Acid", "Niacinamide", "Glycerin"],
    },
    {
        "product_id": "prod-cream-night",
        "canonical_name": "Heavy Fragranced Night Cream",
        "brand": "Bellmark",
        "category": "Moisturizer",
        "attributes": [
            ("fragrance_free", {"value_bool": False}),
            ("volume", {"value_number": 50.0, "unit": "ml"}),
            ("skin_type", {"value_string": "Dry"}),
        ],
        "ingredients": ["Shea Butter", "Parfum", "Glycerin"],
    },
    {
        "product_id": "prod-spf-daily",
        "canonical_name": "Daily Mineral Sunscreen",
        "brand": "Northlight",
        "category": "Sunscreen",
        "attributes": [
            ("spf", {"value_number": 50.0}),
            ("fragrance_free", {"value_bool": True}),
            ("volume", {"value_number": 50.0, "unit": "ml"}),
            ("skin_type", {"value_string": "Sensitive"}),
        ],
        "ingredients": ["Zinc Oxide", "Glycerin"],
    },
    {
        "product_id": "prod-discontinued",
        "canonical_name": "Retired Clay Mask",
        "brand": "Bellmark",
        "status": "discontinued",
        "category": "Mask",
        "attributes": [("fragrance_free", {"value_bool": True})],
        "ingredients": ["Kaolin"],
    },
)


def _sample_records() -> list[dict[str, Any]]:
    """Materialise :data:`GRAPH_SAMPLE_PRODUCTS` into ``seed_products`` input.

    Returns:
        Records whose ``attributes`` are real
        :class:`~ingest.graph.model.AttributeValue` objects.
    """
    from ingest.graph import AttributeValue

    records = []
    for spec in GRAPH_SAMPLE_PRODUCTS:
        record = {k: v for k, v in spec.items() if k != "attributes"}
        record["attributes"] = [AttributeValue(key, **kwargs) for key, kwargs in spec["attributes"]]
        records.append(record)
    return records


@pytest.fixture
def graph_seeded_catalog(graph_schema_session: Any, graph_source: Any) -> dict[str, Any]:
    """Seed :data:`GRAPH_SAMPLE_PRODUCTS` with provenance and embed every product.

    Args:
        graph_schema_session: an empty, fully-constrained session.
        graph_source: the provenance for every seeded fact.

    Returns:
        ``{"session", "source", "product_ids", "report"}`` — ``report`` is the
        :class:`~ingest.graph.reembed.ReembedReport` from the embedding pass, so a test can
        assert on it without re-running one.
    """
    from ingest.embeddings import get_embedding_provider
    from ingest.graph import reembed_products, seed_products

    product_ids = seed_products(graph_schema_session, _sample_records(), source=graph_source)
    report = reembed_products(graph_schema_session, get_embedding_provider("hash"))
    return {
        "session": graph_schema_session,
        "source": graph_source,
        "product_ids": product_ids,
        "report": report,
    }
