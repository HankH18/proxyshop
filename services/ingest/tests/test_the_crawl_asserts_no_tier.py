"""A crawl establishes catalogue presence and nothing else, so it may not assert a tier.

Run::

    PROXYSHOP_WORKER=1 ./.venv/bin/python -m pytest \
        services/ingest/tests/test_the_crawl_asserts_no_tier.py -q

The defect this file pins closed
--------------------------------
``Store.tier`` defaulted to ``2`` and ``as_properties`` always wrote it, so
:func:`~ingest.adapters.mapping.build_upserts` — the ONLY non-test construction of the graph
``Store`` dataclass in the tree — wrote **tier 2 for every crawled store**. Under D28 that is
the most privileged tier: *0 = catalogue present, no agent, no envelope; 1 = network-hosted
agent; 2 = external self-hosted agent behind the signed door.* A store the platform has only
crawled is 0 by that definition, and the crawl is the one component that cannot know
otherwise — it never contacted an agent.

Worse than the wrong value: ``upsert_store`` delegates to ``_fact_node``, whose
``MERGE (n:Store {store_id: $id}) ... SET n += $props`` runs on **every refresh cycle**. So a
tier written by anything else was stomped the next time that store was crawled, silently,
with the load reporting success.

Two halves, and shipping either alone is a defect
-------------------------------------------------
This file is the FIRST half — the crawl asserts nothing, and an unclaimed store reads back
as tier 0 rather than tier 2. The second half lives in the exchange, which is the only
component holding the endpoint registry; without it, four demo stores that are BOTH crawled
and hosted (they share a ``store_id`` with their crawled selves — ``gaiaherbs.com`` is one
node, not two) would be demoted from bidding to a list-price fallback. See
``apps/exchange/tests/test_a_reachable_store_is_not_demoted_by_the_crawl.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from ingest.adapters import CatalogSnapshot, ProductRecord, VariantRecord
from ingest.adapters.mapping import build_upserts
from ingest.embeddings import HashEmbedding
from ingest.graph import (
    Product,
    Source,
    Store,
    link_sells,
    upsert_product,
    upsert_source,
    upsert_store,
)
from ingest.graph.query import candidate_shops
from ingest.graph.upsert import set_product_embedding


def _snapshot() -> CatalogSnapshot:
    return CatalogSnapshot(
        adapter="probe",
        store_id="gaiaherbs.com",
        base_url="https://gaiaherbs.com",
        observed_at="2026-01-01T00:00:00Z",
        extractor_version="probe/1",
        products=(
            ProductRecord(
                product_id="prod-1",
                canonical_name="Turmeric Supreme",
                source_url="https://gaiaherbs.com/products/turmeric",
                content_hash="hash-1",
                changed=True,
                variants=(VariantRecord(variant_id="var-1", price=29.99),),
            ),
        ),
    )


def test_the_crawl_writes_no_tier_at_all() -> None:
    """The store op must carry no ``tier`` property, so a re-crawl cannot stomp one.

    Asserted on ``as_properties()`` rather than on the dataclass attribute because that
    mapping is what ``MERGE ... SET n += $props`` writes: a tier the model holds and the
    mapping omits is a tier the graph never sees, which is exactly the intent.
    """
    ops = build_upserts(_snapshot())
    stores = [op for op in ops if op.kind == "store"]
    assert len(stores) == 1, [op.kind for op in ops]
    written: dict[str, Any] = stores[0].node.as_properties()
    assert "tier" not in written, (
        f"the crawl asserted a tier it cannot know: {written}. A crawl observes a catalogue; "
        f"whether the store runs an agent is a fact only the endpoint registry holds"
    )
    # The facts a crawl CAN stand behind are still written, so this is a narrowing and not a
    # deletion.
    assert written["store_id"] == "gaiaherbs.com"
    assert written["domain"] == "gaiaherbs.com"


def test_a_tier_a_caller_states_is_still_written() -> None:
    """The control. Omitting an unknown tier must not stop a KNOWN one being recorded."""
    assert (
        Store("store-north", "north.example", "Northlight Retail Ltd", 1).as_properties()["tier"]
        == 1
    )
    assert Store("store-t0", "t0.example", "Catalogue Only", 0).as_properties()["tier"] == 0


@pytest.mark.docker("neo4j-bolt")
@pytest.mark.graph
def test_a_store_with_no_tier_property_reads_back_as_catalogue_only(
    neo4j_session: Any,
) -> None:
    """``coalesce(s.tier, …)`` is the read-back default and it must not be the top tier.

    This is the only test in the tree that exercises the coalesce arm at all: every other
    fixture in this repository writes a tier explicitly, so the default was unmeasured while
    being the value every crawled store in the corpus actually got.

    D28's 0 is the honest answer for a store nobody has established an agent for, and it is
    the direction to fail in: 2 is the most privileged tier and this is the branch reached
    when the graph knows nothing at all.

    The fixture is written under ids nothing else uses and removed in ``finally``, because
    this session shares one Neo4j database with every other graph test (Community edition has
    exactly one; ``@pytest.mark.graph`` serialises them).
    """
    source = Source(
        source_id="src-tier-probe",
        url="https://untiered.example/products.json",
        content_hash="hash-tier-probe",
        observed_at="2026-01-01T00:00:00Z",
        extractor_version="probe/1",
        confidence=1.0,
        source_class="scraped",
    )
    try:
        upsert_source(neo4j_session, source)
        upsert_product(
            neo4j_session,
            Product(product_id="prod-tier-probe", canonical_name="Tier Probe Serum"),
            source=source,
        )
        # A store written with NO tier property at all — the shape `build_upserts` now emits.
        upsert_store(
            neo4j_session,
            Store(store_id="store-untiered", domain="untiered.example", tier=None),
            source=source,
        )
        link_sells(
            neo4j_session,
            store_id="store-untiered",
            product_id="prod-tier-probe",
            source=source,
        )
        # `candidate_shops` pivots off `candidate_products`, which is a VECTOR read: a product
        # with no embedding is retrieved by nothing, and the roster would then be empty for a
        # reason that has nothing to do with tiers.
        provider = HashEmbedding()
        set_product_embedding(
            neo4j_session,
            product_id="prod-tier-probe",
            embedding=provider.embed("Tier Probe Serum"),
        )

        stored = neo4j_session.run(
            "MATCH (s:Store {store_id: 'store-untiered'}) RETURN s.tier AS tier"
        ).single()
        assert stored["tier"] is None, (
            f"the write put a tier on the node ({stored['tier']!r}), so the read-back default "
            f"below is not being exercised at all"
        )

        shops = {
            shop.store_id: shop
            for shop in candidate_shops(
                neo4j_session,
                query_text="Tier Probe Serum",
                provider=provider,
                limit=50,
            )
        }
        assert "store-untiered" in shops, sorted(shops)
        assert shops["store-untiered"].tier == 0, (
            f"a store the platform knows nothing about read back as tier "
            f"{shops['store-untiered'].tier}; D28 says a catalogue-only store is 0"
        )
    finally:
        neo4j_session.run(
            "MATCH (n) WHERE n.store_id = 'store-untiered' OR n.product_id = 'prod-tier-probe' "
            "OR n.source_id = 'src-tier-probe' DETACH DELETE n"
        )
