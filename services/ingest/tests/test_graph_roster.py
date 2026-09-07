"""The shop-shaped read: "which shops plausibly serve this need?" (D55).

Why this file exists at all. Before it, ``services/ingest/src/graph/query.py`` contained
**zero** occurrences of ``Store``, ``Offer``, ``SELLS`` or ``MAKES_OFFER`` — measured — while
``graph/upsert.py`` wrote all four. The write side stored the whole supply side of DESIGN's
model and the read side could only ever answer "which *products*". Under D55 the exchange's
first question is which SHOPS to solicit, so the read side had to grow a shop-shaped answer.

The provenance half is the point, not a decoration. D55 lets the buyer-side agent assemble a
pitch **only** from facts the platform has already checked, because when the platform writes
the copy the platform owns the false claim. A roster row assembled from an unsourced ``Store``
or an unsourced ``SELLS`` edge is a shop the platform has nothing checked to say about, so the
roster must not emit it — and it must not emit it *silently*, which is what
:func:`roster_provenance_exclusions` is for. Every refusal below has a positive control in the
same test: the honest twin of the sabotaged row is asserted present in the same roster.
"""

from __future__ import annotations

from typing import Any

import pytest
from ingest.embeddings import EMBEDDING_DIM, EmbeddingProvider, HashEmbedding
from ingest.graph import (
    EmbeddingProviderMismatch,
    Offer,
    Source,
    Store,
    UnretrievableQuery,
    Variant,
    link_sells,
    reembed_products,
    upsert_offer,
    upsert_store,
    upsert_variant,
)
from ingest.graph.query import (
    RosterExclusion,
    ShopCandidate,
    candidate_shops,
    roster_provenance_exclusions,
)
from ingest.graph.reembed import embedding_text, read_products

from proxyshop_support.embedding import hash_embed

OBSERVED_AT = "2026-01-01T00:00:00+00:00"

#: A second provenance, distinct from ``graph_source``, so a test can prove the roster reads
#: the *graph's* provenance rather than the one the fixture happened to use.
SECOND_SOURCE = Source(
    source_id="src-roster-second",
    url="https://bellmark.example/products.json",
    content_hash="sha256:1111111111111111111111111111111111111111111111111111111111111111",
    observed_at=OBSERVED_AT,
    extractor_version="fixture@1",
    confidence=0.8,
    source_class="scraped",
)


def _text_for(session: Any, product_id: str) -> str:
    """The exact document ``reembed_products`` embedded for ``product_id``.

    The hash provider is content-addressed (D19), so the only query text that reliably ranks
    a specific product first is the one its own vector was built from. Every roster test that
    wants a *known* product at rank 1 queries with this rather than with a human phrase — see
    ``test_embedding_ranking_gate.py`` for the measurement that makes that necessary.
    """
    for row in read_products(session, limit=100):
        if row["product_id"] == product_id:
            return embedding_text(row)
    raise AssertionError(f"{product_id} is not in the seeded catalog")


def _seed_marketplace(session: Any, source: Source) -> None:
    """Three provenanced shops over the seeded catalog, with offers.

    ``store-north`` sells the serum and the sunscreen and prices the serum at 28.00.
    ``store-bell`` sells the serum too, at 31.50, so the roster has something to order.
    ``store-quiet`` sells the serum with **no** offer at all — a scraped shop the platform
    crawled but for which it never observed a price. D55 says that shop is the organic result
    and must still appear; what it must not do is appear carrying a price nobody observed.
    """
    for store, store_source in (
        (Store("store-north", "north.example", "Northlight Retail Ltd", 1), source),
        (Store("store-bell", "bell.example", "Bellmark Trading", 2), SECOND_SOURCE),
        (Store("store-quiet", "quiet.example", "Quiet Goods", 2), source),
    ):
        upsert_store(session, store, source=store_source)

    link_sells(session, store_id="store-north", product_id="prod-serum-c", source=source)
    link_sells(session, store_id="store-north", product_id="prod-spf-daily", source=source)
    link_sells(session, store_id="store-bell", product_id="prod-serum-c", source=SECOND_SOURCE)
    link_sells(session, store_id="store-quiet", product_id="prod-serum-c", source=source)

    upsert_variant(
        session,
        Variant("var-serum-30", "SKU-SERUM-30", "30 ml"),
        product_id="prod-serum-c",
        source=source,
    )
    upsert_offer(
        session,
        Offer("off-north-serum", 28.00, "USD", "in_stock", OBSERVED_AT),
        store_id="store-north",
        variant_id="var-serum-30",
        source=source,
    )
    upsert_offer(
        session,
        Offer("off-bell-serum", 31.50, "USD", "in_stock", OBSERVED_AT),
        store_id="store-bell",
        variant_id="var-serum-30",
        source=SECOND_SOURCE,
    )


@pytest.fixture
def roster_catalog(graph_seeded_catalog: dict[str, Any]) -> dict[str, Any]:
    """The seeded product catalog plus the three-shop marketplace above."""
    _seed_marketplace(graph_seeded_catalog["session"], graph_seeded_catalog["source"])
    return graph_seeded_catalog


# =======================================================================================
# 1. The roster answers "which shops"
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_the_roster_returns_the_shops_that_sell_the_matching_products(
    roster_catalog: dict[str, Any],
) -> None:
    """The read the exchange could not make before: shops, not products.

    Everything a roster row needs is here — identity, domain, tier, which matched products
    the shop carries, and the observed price when one was observed with provenance.
    """
    session = roster_catalog["session"]
    shops = candidate_shops(session, query_text=_text_for(session, "prod-serum-c"), limit=10)

    assert {s.store_id for s in shops} == {"store-north", "store-bell", "store-quiet"}, [
        s.store_id for s in shops
    ]
    by_id = {s.store_id: s for s in shops}

    north = by_id["store-north"]
    assert isinstance(north, ShopCandidate)
    assert north.domain == "north.example"
    assert north.tier == 1
    assert north.business_identity == "Northlight Retail Ltd"
    assert "prod-serum-c" in north.product_ids
    assert north.lowest_price == pytest.approx(28.00)
    assert north.currencies == ["USD"]
    assert [o.offer_id for o in north.offers] == ["off-north-serum"]
    assert north.offers[0].availability == "in_stock"
    assert north.offers[0].variant_id == "var-serum-30"
    assert north.offers[0].product_id == "prod-serum-c"

    assert by_id["store-bell"].lowest_price == pytest.approx(31.50)

    quiet = by_id["store-quiet"]
    assert quiet.offers == [] and quiet.lowest_price is None, (
        "a crawled shop with no observed offer is still the organic result (D55); it just "
        "has no price to state"
    )
    assert quiet.product_ids == ["prod-serum-c"]


@pytest.mark.docker
@pytest.mark.graph
def test_the_roster_is_ranked_by_the_shop_s_best_matching_product(
    roster_catalog: dict[str, Any],
) -> None:
    """Shop order is derived from product relevance, and ties break deterministically."""
    session = roster_catalog["session"]
    shops = candidate_shops(session, query_text=_text_for(session, "prod-spf-daily"), limit=10)
    assert shops[0].store_id == "store-north", [s.store_id for s in shops]
    assert [s.best_score for s in shops] == sorted((s.best_score for s in shops), reverse=True)

    # Three shops all carry the serum, so their best product score is identical; the tie must
    # break on store_id, not on Neo4j's storage order, or every downstream ranking assertion
    # against this roster is flaky.
    tied = candidate_shops(session, query_text=_text_for(session, "prod-serum-c"), limit=10)
    assert [s.store_id for s in tied] == ["store-bell", "store-north", "store-quiet"]
    again = candidate_shops(session, query_text=_text_for(session, "prod-serum-c"), limit=10)
    assert [s.store_id for s in again] == [s.store_id for s in tied]


@pytest.mark.docker
@pytest.mark.graph
def test_the_roster_carries_the_provenance_of_every_fact_it_states(
    roster_catalog: dict[str, Any],
) -> None:
    """Each row names the ``Source`` ids behind it, so a pitch can cite what it stands on."""
    session = roster_catalog["session"]
    shops = candidate_shops(session, query_text=_text_for(session, "prod-serum-c"), limit=10)
    by_id = {s.store_id: s for s in shops}
    assert by_id["store-bell"].source_ids == ["src-roster-second"]
    assert by_id["store-north"].source_ids == ["src-catalog-fixture"]
    assert by_id["store-north"].offers[0].source_ids == ["src-catalog-fixture"]


@pytest.mark.docker
@pytest.mark.graph
def test_the_roster_refuses_the_query_the_product_read_refuses(
    roster_catalog: dict[str, Any],
) -> None:
    """No vector and no structured predicate is unanswerable here for the same reason.

    The roster is a *pivot* over the product read, so it inherits that read's refusals
    rather than opening a second, laxer door into the same catalog. Without this, "give me
    every shop" would be expressible through the roster while being forbidden through the
    query it is built on.
    """
    with pytest.raises(UnretrievableQuery):
        candidate_shops(roster_catalog["session"])


@pytest.mark.docker
@pytest.mark.graph
def test_the_roster_honours_the_structured_predicates_of_the_product_read(
    roster_catalog: dict[str, Any],
) -> None:
    """A structured-only roster works, and narrowing the products narrows the shops."""
    session = roster_catalog["session"]
    sunscreen = candidate_shops(session, category="Sunscreen", limit=10)
    assert [s.store_id for s in sunscreen] == ["store-north"]
    assert sunscreen[0].scored is False and sunscreen[0].best_cosine is None, (
        "the structured path measures no similarity, so the roster must not report one"
    )


# =======================================================================================
# 2. Provenance: what the roster refuses, and that it says so
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_store_with_no_source_never_reaches_the_roster(
    roster_catalog: dict[str, Any],
) -> None:
    """Sabotage with a positive control in the same assertion.

    A raw-Cypher ``Store`` with a raw ``SELLS`` edge is exactly what an adapter that skipped
    ``ingest.graph.upsert`` would leave behind. Under D55 the platform has checked nothing
    about it, so it must not be handed to a buyer-side agent as something to pitch.
    """
    session = roster_catalog["session"]
    session.run(
        "MATCH (p:Product {product_id:'prod-serum-c'}) "
        "CREATE (s:Store {store_id:'store-ghost', domain:'ghost.example', "
        "business_identity:'Ghost', tier:2})-[:SELLS {source_id:'src-catalog-fixture'}]->(p)"
    ).consume()
    shops = candidate_shops(session, query_text=_text_for(session, "prod-serum-c"), limit=10)
    returned = {s.store_id for s in shops}
    assert "store-ghost" not in returned, "an unsourced Store was laundered onto the roster"
    assert "store-north" in returned, "positive control: the honest shop still appears"

    excluded = roster_provenance_exclusions(session, product_ids=["prod-serum-c"])
    assert RosterExclusion("store-ghost", "prod-serum-c", "unsourced_store") in excluded, excluded


@pytest.mark.docker
@pytest.mark.graph
def test_an_unsourced_sells_edge_is_not_a_reason_to_solicit_a_shop(
    roster_catalog: dict[str, Any],
) -> None:
    """The edge half of the same rule; the node half passing is not enough.

    ``store-quiet`` is a fully provenanced ``Store``. The claim "it sells the night cream" is
    written with an unresolvable ``source_id``, so that CLAIM is dropped — and the shop is
    not, because it has an honest ``SELLS`` on the serum. That distinction is the assertion:
    the row survives and the unchecked product is missing from it. A test that demanded the
    whole shop disappear would pass just as happily against a roster that dropped shops for
    the wrong reason, and would be wrong about D55 besides: a checked fact does not stop
    being checked because an unchecked one was written next to it.
    """
    session = roster_catalog["session"]
    session.run(
        "MATCH (s:Store {store_id:'store-quiet'}), (p:Product {product_id:'prod-cream-night'}) "
        "CREATE (s)-[:SELLS {source_id:'src-does-not-exist'}]->(p)"
    ).consume()
    shops = candidate_shops(session, query_text=_text_for(session, "prod-cream-night"), limit=10)
    quiet = next(s for s in shops if s.store_id == "store-quiet")
    assert "prod-cream-night" not in quiet.product_ids, (
        f"an unsourced SELLS claim reached the roster: {quiet.product_ids}"
    )
    # Positive control, in the same row: the honestly-sourced claim is still there.
    assert quiet.product_ids == ["prod-serum-c"]
    # And the honest twin of the sabotaged edge — store-bell's sourced SELLS — still carries.
    assert "prod-serum-c" in next(s for s in shops if s.store_id == "store-bell").product_ids

    excluded = roster_provenance_exclusions(session, product_ids=["prod-cream-night"])
    assert RosterExclusion("store-quiet", "prod-cream-night", "unsourced_sells") in excluded


@pytest.mark.docker
@pytest.mark.graph
def test_an_unsourced_offer_costs_the_price_not_the_shop(
    roster_catalog: dict[str, Any],
) -> None:
    """The finest-grained refusal, and the one D55 actually turns on.

    An ``Offer`` reached through an edge whose ``source_id`` resolves to nothing is an
    unchecked price. The shop is still a real, provenanced shop that really sells the
    product, so it stays on the roster — and it stays there with **no price**, because the
    alternative is the platform stating a number it never observed.
    """
    session = roster_catalog["session"]
    session.run(
        "MATCH (s:Store {store_id:'store-quiet'}), (v:Variant {variant_id:'var-serum-30'}) "
        "CREATE (s)-[:MAKES_OFFER {source_id:'src-catalog-fixture'}]->"
        "(o:Offer {offer_id:'off-quiet-serum', price:19.99, currency:'USD', "
        "availability:'in_stock', observed_at:$at})-[:FOR {source_id:'src-nope'}]->(v)",
        at=OBSERVED_AT,
    ).consume()
    shops = candidate_shops(session, query_text=_text_for(session, "prod-serum-c"), limit=10)
    quiet = next(s for s in shops if s.store_id == "store-quiet")
    assert quiet.offers == [] and quiet.lowest_price is None, (
        f"an unchecked 19.99 reached the roster: {quiet.offers}"
    )
    # Positive control: a *sourced* offer chain on the same graph does produce a price.
    north = next(s for s in shops if s.store_id == "store-north")
    assert north.lowest_price == pytest.approx(28.00)

    excluded = roster_provenance_exclusions(session, product_ids=["prod-serum-c"])
    assert RosterExclusion("store-quiet", "prod-serum-c", "unsourced_offer") in excluded


@pytest.mark.docker
@pytest.mark.graph
def test_the_exclusion_report_is_empty_on_an_honest_graph(
    roster_catalog: dict[str, Any],
) -> None:
    """Not a ``return []``: the tests above prove it fires, this proves it also stops."""
    session = roster_catalog["session"]
    assert (
        roster_provenance_exclusions(
            session, product_ids=["prod-serum-c", "prod-spf-daily", "prod-cream-night"]
        )
        == []
    )


# =======================================================================================
# 3. The offer chain is walked, not assumed
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_shop_reachable_only_through_its_offer_chain_is_still_on_the_roster(
    roster_catalog: dict[str, Any],
) -> None:
    """``MAKES_OFFER -> Offer -> FOR -> Variant <- HAS_VARIANT`` is a carrying relation too.

    ``link_sells`` and ``upsert_offer`` are separate calls, so an adapter that observed a
    priced listing without separately asserting ``SELLS`` leaves a store connected to the
    product only through the offer chain. That store demonstrably carries the product — the
    graph says so, with provenance — and dropping it would be a false negative created by
    writer convention rather than by evidence.
    """
    session = roster_catalog["session"]
    source = roster_catalog["source"]
    upsert_store(
        session, Store("store-offeronly", "offeronly.example", "Offer Only", 2), source=source
    )
    upsert_offer(
        session,
        Offer("off-only-serum", 25.25, "USD", "in_stock", OBSERVED_AT),
        store_id="store-offeronly",
        variant_id="var-serum-30",
        source=source,
    )
    shops = candidate_shops(session, query_text=_text_for(session, "prod-serum-c"), limit=10)
    only = next(s for s in shops if s.store_id == "store-offeronly")
    assert only.lowest_price == pytest.approx(25.25)
    assert only.product_ids == ["prod-serum-c"]
    assert only.via == ["MAKES_OFFER"], only.via

    north = next(s for s in shops if s.store_id == "store-north")
    assert north.via == ["MAKES_OFFER", "SELLS"]


@pytest.mark.docker
@pytest.mark.graph
def test_an_offer_node_with_no_price_states_no_price_rather_than_crashing(
    roster_catalog: dict[str, Any],
) -> None:
    """A raw-Cypher ``Offer`` carrying provenance but no ``price`` is not a priced listing.

    ``Offer`` requires a price, so ``upsert_offer`` always writes one — which is exactly why
    this case can only arrive from outside this library, and exactly the threat model
    ``provenance_violations`` exists for. The roster must neither invent a number nor take
    the whole shop down with a ``float(None)``.
    """
    session = roster_catalog["session"]
    session.run(
        "MATCH (src:Source {source_id:'src-catalog-fixture'}), "
        "(s:Store {store_id:'store-quiet'}), (v:Variant {variant_id:'var-serum-30'}) "
        "CREATE (s)-[:MAKES_OFFER {source_id:'src-catalog-fixture'}]->"
        "(o:Offer {offer_id:'off-quiet-priceless', currency:'USD', availability:'in_stock', "
        "observed_at:$at})-[:FOR {source_id:'src-catalog-fixture'}]->(v) "
        "CREATE (o)-[:SUPPORTED_BY]->(src)",
        at=OBSERVED_AT,
    ).consume()
    shops = candidate_shops(session, query_text=_text_for(session, "prod-serum-c"), limit=10)
    quiet = next(s for s in shops if s.store_id == "store-quiet")
    assert quiet.offers == [] and quiet.lowest_price is None
    # Positive control: the fully-formed offer chain on the same graph still yields a price.
    assert next(s for s in shops if s.store_id == "store-north").lowest_price == pytest.approx(
        28.00
    )


class _OtherProvider(EmbeddingProvider):
    """A second, entirely valid, 1024-d provider — the shape of the ``local_bge`` swap.

    Declares the same width as ``hash``, so the write side's width guard is structurally
    incapable of noticing the swap and only the recorded run's ``provider`` can.
    """

    name = "other"
    dimension = EMBEDDING_DIM

    def embed(self, text: str) -> list[float]:
        """Any valid unit vector in a different space; its contents are irrelevant here."""
        return hash_embed("other::" + text, dim=self.dimension)


@pytest.mark.docker
@pytest.mark.graph
def test_the_roster_inherits_the_vector_index_refusals_rather_than_assuming_them(
    roster_catalog: dict[str, Any],
) -> None:
    """ "By construction" is the phrase that hides bugs, so the inheritance is driven, not argued.

    ``candidate_shops`` delegates its retrieval to ``candidate_products``, which refuses to
    rank across two vector spaces. If that delegation were ever replaced by a second copy of
    the Cypher, the roster would happily pivot a noise ranking into a shop roster and nothing
    else in this file would notice — a roster of the wrong shops looks exactly like a roster
    of the right ones.

    The pre-computed-vector entry point is used because it reaches the guard without calling
    ``embed`` on a provider whose weights are absent (D3).
    """
    session = roster_catalog["session"]
    reembed_products(session, _OtherProvider())

    with pytest.raises(EmbeddingProviderMismatch, match="other"):
        candidate_shops(
            session,
            embedding=hash_embed("anything", dim=EMBEDDING_DIM),
            provider=HashEmbedding(),
            limit=10,
        )
    # Positive control: the provider that actually wrote the vectors still gets a roster.
    shops = candidate_shops(
        session,
        embedding=_OtherProvider().embed("anything"),
        provider=_OtherProvider(),
        limit=10,
    )
    assert {s.store_id for s in shops} == {"store-north", "store-bell", "store-quiet"}
