"""The evidence half of D55: what the PLATFORM checked about one (store, product) pair.

``candidate_shops`` decides which shops may make their case; ``catalogue_entry`` decides what
that case is checked *against*. The distinction is the whole reason this file exists
separately from ``test_graph_roster.py``, and it is a distinction the type system cannot
express: both reads return provenanced rows off the same graph, and a snapshot assembled from
the wrong provenance looks exactly like one assembled from the right provenance.

Three things would break silently without these tests, and none of them would turn a single
other test in this repo red.

**A snapshot built from the seller's own words.** ``SOURCE_CLASSES`` contains
``seller_asserted`` and ``owner_statement`` — a store telling the platform about itself. Those
are perfectly valid provenance, and the roster is right to accept them, because being wrong
there costs one wasted solicitation. Here they are poison: grading "our serum is SPF 50"
against an ``AttributeValue`` the seller asserted is a check that cannot fail, and it would
report as *verified* rather than as *unchecked*. That is the exact failure D55's
verification asymmetry exists to prevent — the platform's rendering of its own crawl is the
organic result, and the seller's purchased message is the one that gets adversarially checked.
Widening ``source_classes`` back to "anything sourced" is a one-word edit that no linter, no
type checker and no other test would notice, and the resulting verifier would be *greener*.

**A price nobody observed.** ``CatalogueEntry.offer`` is ``None`` for "the platform never
checked a price here", and a snapshot that filled that in with ``0.0`` — or that let an
unsourced ``Offer`` through — hands the buyer-side agent a number the platform must then stand
behind. D55 makes that the platform's false claim, not the store's.

**A refusal that degrades instead of denying.** Every gate here fails toward ``None`` or
toward an absent key, so a claim comes back "the catalogue records no such thing" rather than
being graded against something unchecked. A gate that returned a partial entry instead would
be indistinguishable in every consumer until a claim was graded against the hole.

Every refusal below carries a positive control **in the same test**: the honest twin of the
sabotaged fact is asserted present in the same call or the same graph. A test that only
asserted ``is None`` would pass just as happily against a ``catalogue_entry`` that returned
``None`` for everything.

Sabotage is raw Cypher on purpose. ``ingest.graph.upsert`` cannot write an unsourced fact —
``_require_source`` is a required keyword-only argument and ``_fact_node`` writes the node and
its ``SUPPORTED_BY`` in one statement — so the corruptions these gates defend against can only
arrive from an adapter that skipped the library. That is precisely why the gate has to live in
the read's Cypher rather than in a Python convention on the write side.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest
from ingest.graph import (
    AttributeValue,
    Offer,
    Source,
    Store,
    Variant,
    link_sells,
    upsert_attribute,
    upsert_offer,
    upsert_store,
    upsert_variant,
)
from ingest.graph.query import (
    PLATFORM_OBSERVED_SOURCE_CLASSES,
    CatalogueEntry,
    ShopOffer,
    candidate_shops,
    catalogue_entry,
    latest_instant,
)

OBSERVED_AT = "2026-01-01T00:00:00+00:00"

#: The attribute keys ``graph_seeded_catalog`` writes for ``prod-serum-c``, all of them
#: ``scraped``. This is the positive-control set: every sabotage below must leave it whole.
HONEST_SERUM_KEYS = {"fragrance_free", "skin_type", "spf", "volume"}

#: A second *platform* provenance, distinct from ``graph_source``, so a test can prove the
#: snapshot reads the **graph's** provenance rather than echoing the one the fixture happened
#: to use. ``store-north`` is written with this and nothing else, so every assertion on
#: ``CatalogueEntry.source_ids`` fails if the read ever hard-codes the fixture's source.
SECOND_SOURCE = Source(
    source_id="src-catalogue-second",
    url="https://north.example/products.json",
    content_hash="sha256:1111111111111111111111111111111111111111111111111111111111111111",
    observed_at=OBSERVED_AT,
    extractor_version="fixture@1",
    confidence=0.8,
    source_class="scraped",
)

#: The store talking about itself. Valid provenance, accepted by the roster, and **not**
#: platform observation — the single most important distinction in this file.
SELLER_SOURCE = Source(
    source_id="src-seller-says",
    url="https://asserted.example/onboarding/self-report",
    content_hash="sha256:2222222222222222222222222222222222222222222222222222222222222222",
    observed_at=OBSERVED_AT,
    extractor_version="fixture@1",
    confidence=1.0,
    source_class="seller_asserted",
)

#: The other half of "the store said so": an owner's statement during onboarding. Refused for
#: the same reason as :data:`SELLER_SOURCE`, and asserted separately because
#: ``PLATFORM_OBSERVED_SOURCE_CLASSES`` is an allow-list of two, not a deny-list of one.
OWNER_SOURCE = Source(
    source_id="src-owner-says",
    url="https://quiet.example/onboarding/owner-statement",
    content_hash="sha256:3333333333333333333333333333333333333333333333333333333333333333",
    observed_at=OBSERVED_AT,
    extractor_version="fixture@1",
    confidence=1.0,
    source_class="owner_statement",
)

#: The platform's own instrumentation. Nobody but the platform authored it, so it counts.
PIXEL_SOURCE = Source(
    source_id="src-pixel-feed",
    url="https://pixel.proxyshop.example/events/north",
    content_hash="sha256:4444444444444444444444444444444444444444444444444444444444444444",
    observed_at=OBSERVED_AT,
    extractor_version="fixture@1",
    confidence=0.7,
    source_class="pixel_feed",
)


def _seed_marketplace(session: Any, source: Source) -> None:
    """Three provenanced shops over the seeded catalog, reachable three different ways.

    ``store-north`` is the fully-formed pair: it ``SELLS`` the serum *and* prices it through a
    complete offer chain, and its ``Store`` node is supported by :data:`SECOND_SOURCE` alone.
    It also prices the sunscreen through a **separate** variant and offer, which is what makes
    a positive control possible in the offer-sabotage tests — every serum-side corruption
    there leaves this chain untouched.

    ``store-quiet`` sells both products with no offer at all: a shop the platform crawled but
    for which it never observed a price. Under D55 that shop is the organic result and must
    still produce a snapshot; what it must not produce is a price.

    ``store-offeronly`` has no ``SELLS`` edge whatsoever and is reachable only through its
    offer chain, so "carrying relation" cannot quietly narrow to "``SELLS``".
    """
    upsert_store(
        session,
        Store("store-north", "north.example", "Northlight Retail Ltd", 1),
        source=SECOND_SOURCE,
    )
    upsert_store(session, Store("store-quiet", "quiet.example", "Quiet Goods", 2), source=source)
    upsert_store(
        session, Store("store-offeronly", "offeronly.example", "Offer Only", 2), source=source
    )

    link_sells(session, store_id="store-north", product_id="prod-serum-c", source=source)
    link_sells(session, store_id="store-north", product_id="prod-spf-daily", source=source)
    link_sells(session, store_id="store-quiet", product_id="prod-serum-c", source=source)
    link_sells(session, store_id="store-quiet", product_id="prod-spf-daily", source=source)

    upsert_variant(
        session,
        Variant("var-serum-30", "SKU-SERUM-30", "30 ml"),
        product_id="prod-serum-c",
        source=source,
    )
    upsert_variant(
        session,
        Variant("var-spf-50", "SKU-SPF-50", "50 ml"),
        product_id="prod-spf-daily",
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
        Offer("off-north-spf", 42.00, "USD", "in_stock", OBSERVED_AT),
        store_id="store-north",
        variant_id="var-spf-50",
        source=source,
    )
    upsert_offer(
        session,
        Offer("off-only-serum", 31.50, "USD", "in_stock", OBSERVED_AT),
        store_id="store-offeronly",
        variant_id="var-serum-30",
        source=source,
    )


@pytest.fixture
def catalogue_market(graph_seeded_catalog: dict[str, Any]) -> dict[str, Any]:
    """The seeded product catalog plus the three-shop marketplace above."""
    _seed_marketplace(graph_seeded_catalog["session"], graph_seeded_catalog["source"])
    return graph_seeded_catalog


def _keys(entry: CatalogueEntry | None) -> set[str]:
    """The attribute keys on an entry, as a set.

    Args:
        entry: the snapshot under test; ``None`` is an assertion failure here rather than a
            silently empty set, because "no entry" and "an entry with no attributes" are
            different answers and a helper must not let them look alike.

    Returns:
        Every :attr:`~ingest.graph.query.CatalogueAttribute.key` on the entry.
    """
    assert entry is not None, "expected a snapshot, got None"
    return {attribute.key for attribute in entry.attributes}


def _assert_states_no_price(entry: CatalogueEntry, forbidden: str) -> None:
    """Assert the entry reports no price of any kind — ``0.0`` included.

    ``offer is None`` alone is not the whole claim. The dangerous regression is not "the offer
    field stayed populated", it is a snapshot that grows a *second* price channel (a
    ``lowest_price: float = 0.0`` convenience field, say) which reads as **free** rather than
    as **never checked** — and which a downstream "we beat their price" verifier would happily
    grade a claim against. So the absence is asserted structurally as well as by value.

    Args:
        entry: the snapshot that must state nothing about price.
        forbidden: the sabotaged price as it would render, e.g. ``"28.0"``.
    """
    assert entry.offer is None, f"an unchecked price reached the snapshot: {entry.offer}"
    assert [f.name for f in fields(entry) if "price" in f.name] == [], (
        "CatalogueEntry grew a scalar price field alongside `offer`; a numeric default there "
        "states 'free' where the platform observed nothing"
    )
    assert forbidden not in repr(entry), f"the unchecked price survived on the entry: {entry}"


# =======================================================================================
# 1. The snapshot the exchange grades claims against
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_the_snapshot_carries_everything_a_claim_can_be_graded_against(
    catalogue_market: dict[str, Any],
) -> None:
    """The read that did not exist: one pair, with every fact the platform checked about it.

    A verifier can only grade a claim it has a *reading* for, so an entry that carried
    identity and price but no attributes would leave every product claim undecided while
    looking perfectly healthy. Each field is asserted individually rather than as a blob
    because the failure mode is one field quietly going empty — ``coalesce(p.brand, '')``
    turns a missing brand into ``""``, which no exception and no type check would flag.

    ``store-north``'s ``Store`` node is supported by :data:`SECOND_SOURCE` and by nothing
    else, so ``source_ids`` naming both it and the fixture's source is the proof that the read
    walks the graph's provenance rather than reporting the source it was seeded from.
    """
    session = catalogue_market["session"]
    entry = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")

    assert isinstance(entry, CatalogueEntry)
    assert entry.store_id == "store-north"
    assert entry.domain == "north.example"
    assert entry.product_id == "prod-serum-c"
    assert entry.canonical_name == "Gentle Vitamin C Serum"
    assert entry.brand == "Northlight"
    assert entry.status == "active"
    # SORTED, which is `ShopCandidate.via`'s order too. Same field name, same two-value
    # vocabulary, and a consumer holds both at once — two orders for one pair of facts is a
    # trap for anyone comparing or merging a roster row with the snapshot behind it.
    assert entry.via == ["MAKES_OFFER", "SELLS"], entry.via
    assert entry.source_ids == ["src-catalog-fixture", "src-catalogue-second"], entry.source_ids
    assert entry.observed_at == OBSERVED_AT

    readings = {(a.key, a.value) for a in entry.attributes}
    assert readings == {
        ("fragrance_free", True),
        ("skin_type", "Sensitive"),
        ("spf", 0.0),
        ("volume", 30.0),
    }, readings
    by_key = {a.key: a for a in entry.attributes}
    assert by_key["volume"].unit == "ml"
    assert by_key["spf"].unit is None, "no unit stated is None, never a dimensionless default"
    assert by_key["fragrance_free"].canonical_key == "fragrance-free"
    assert by_key["skin_type"].source_ids == ["src-catalog-fixture"]
    assert by_key["skin_type"].observed_at == OBSERVED_AT

    offer = entry.offer
    assert isinstance(offer, ShopOffer)
    assert offer.offer_id == "off-north-serum"
    assert offer.product_id == "prod-serum-c"
    assert offer.variant_id == "var-serum-30"
    assert offer.price == pytest.approx(28.00)
    assert offer.currency == "USD"
    assert offer.availability == "in_stock"
    assert offer.observed_at == OBSERVED_AT
    assert offer.source_ids == ["src-catalog-fixture"]


@pytest.mark.docker
@pytest.mark.graph
def test_a_pair_the_graph_does_not_hold_answers_none_rather_than_raising(
    catalogue_market: dict[str, Any],
) -> None:
    """Absence is a real answer here, and the caller must be able to get it cheaply.

    The exchange asks for a snapshot per (store, product) pair named by an auction, and those
    names come from outside this graph. If an absent pair raised, every caller would need a
    ``try``/``except`` whose except branch is indistinguishable from a genuine graph outage —
    and the safe reading of "we hold nothing" would be one refactor away from becoming a 500.

    The last case is the one that is not about missing nodes at all: two perfectly real,
    perfectly sourced nodes with no observed carrying relation between them. The platform
    never saw ``store-offeronly`` carry the sunscreen, so its catalogue of the sunscreen is
    evidence about somebody else's shelf.
    """
    session = catalogue_market["session"]
    assert catalogue_entry(session, store_id="store-nope", product_id="prod-nope") is None
    assert catalogue_entry(session, store_id="store-north", product_id="prod-nope") is None
    assert catalogue_entry(session, store_id="store-nope", product_id="prod-serum-c") is None
    assert catalogue_entry(session, store_id="store-offeronly", product_id="prod-spf-daily") is None
    assert catalogue_entry(session, store_id="store-north", product_id="prod-serum-c") is not None


# =======================================================================================
# 2. The three-tier provenance rule: what is refused, and how far the refusal reaches
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_store_the_platform_never_observed_yields_no_snapshot_at_all(
    catalogue_market: dict[str, Any],
) -> None:
    """An unsourced ``Store`` costs the whole entry, not a field of it.

    A raw-Cypher ``Store`` with a properly sourced ``SELLS`` edge is what an adapter that
    skipped ``ingest.graph.upsert`` leaves behind, and it is the dangerous shape: the *claim*
    that this store sells this product is provenanced, so a gate that only checked edges would
    pass it. The platform has checked nothing about the shop itself, so it holds no evidence
    about it, and a degraded entry — identity blank, attributes intact — would be worse than
    none: a verifier would grade the seller's claims against readings that belong to a shop the
    platform cannot even name.
    """
    session = catalogue_market["session"]
    session.run(
        "MATCH (p:Product {product_id:'prod-serum-c'}) "
        "CREATE (s:Store {store_id:'store-ghost', domain:'ghost.example', "
        "business_identity:'Ghost', tier:2})-[:SELLS {source_id:'src-catalog-fixture'}]->(p)"
    ).consume()

    assert catalogue_entry(session, store_id="store-ghost", product_id="prod-serum-c") is None, (
        "an unsourced Store was laundered into a catalogue snapshot"
    )
    # Positive control, same graph, same product: the honest shop still has a full snapshot.
    honest = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    assert honest is not None and _keys(honest) == HONEST_SERUM_KEYS


@pytest.mark.docker
@pytest.mark.graph
def test_a_product_the_platform_never_observed_yields_no_snapshot_at_all(
    catalogue_market: dict[str, Any],
) -> None:
    """The product half of the same tier, and it is not implied by the store half.

    The two are separate ``MATCH … WHERE`` clauses in the read, so one can be deleted without
    touching the other and only this test would notice. Stripping the ``SUPPORTED_BY`` off a
    product leaves every other fact about it — name, brand, status, attributes, the sourced
    ``SELLS`` edge pointing at it — completely intact, which is exactly why an entry built
    from those would look correct.
    """
    session = catalogue_market["session"]
    session.run(
        "MATCH (:Product {product_id:'prod-serum-c'})-[r:SUPPORTED_BY]->(:Source) DELETE r"
    ).consume()

    assert catalogue_entry(session, store_id="store-north", product_id="prod-serum-c") is None, (
        "an unsourced Product was laundered into a catalogue snapshot"
    )
    # Positive control, same store: a product whose provenance is intact still reads.
    other = catalogue_entry(session, store_id="store-north", product_id="prod-spf-daily")
    assert other is not None and other.product_id == "prod-spf-daily"
    assert other.offer is not None and other.offer.price == pytest.approx(42.00)


@pytest.mark.docker
@pytest.mark.graph
def test_a_pair_with_no_provenanced_carrying_relation_yields_no_snapshot(
    catalogue_market: dict[str, Any],
) -> None:
    """Both carrying relations must fail before the entry does — and then it must.

    ``store-quiet`` is a fully provenanced shop and the serum is a fully provenanced product;
    only the claim *connecting* them is written with a ``source_id`` that resolves to nothing,
    and ``store-quiet`` has no offer chain to fall back on. The platform therefore never
    observed this store carrying this product, and its catalogue of the product is evidence
    about somebody else's shelf.

    The three positive controls are what stop this from passing against a read that refuses
    for the wrong reason: the same store's honestly-sold product still reads (so the ``Store``
    node was not condemned), another store's honest ``SELLS`` on the same product still reads
    (so the ``Product`` was not condemned), and a store reachable **only** through a
    provenanced offer chain still reads (so "carrying relation" has not narrowed to ``SELLS``,
    which would silently drop every store an adapter recorded a listing for without separately
    asserting a shelf).
    """
    session = catalogue_market["session"]
    session.run(
        "MATCH (:Store {store_id:'store-quiet'})-[r:SELLS]->(:Product {product_id:'prod-serum-c'}) "
        "SET r.source_id = 'src-does-not-exist'"
    ).consume()

    assert catalogue_entry(session, store_id="store-quiet", product_id="prod-serum-c") is None, (
        "an unsourced SELLS claim became a catalogue snapshot the seller's claims are graded on"
    )

    intact = catalogue_entry(session, store_id="store-quiet", product_id="prod-spf-daily")
    assert intact is not None and intact.via == ["SELLS"], "the Store node itself is fine"
    assert catalogue_entry(session, store_id="store-north", product_id="prod-serum-c") is not None

    offer_only = catalogue_entry(session, store_id="store-offeronly", product_id="prod-serum-c")
    assert offer_only is not None, "a provenanced offer chain is a carrying relation too"
    assert offer_only.via == ["MAKES_OFFER"], offer_only.via
    assert offer_only.offer is not None and offer_only.offer.price == pytest.approx(31.50)


#: Each corruption breaks exactly one link of ``MAKES_OFFER -> Offer -> FOR -> Variant <-
#: HAS_VARIANT`` for ``store-north``'s serum listing, leaving the other three and the
#: ``SELLS`` edge honest. Four separate cases because the read checks four separate
#: conditions, and three of them could be deleted while a single-case test stayed green.
_OFFER_CHAIN_SABOTAGE: dict[str, str] = {
    "offer_node_unsupported": (
        "MATCH (:Offer {offer_id:'off-north-serum'})-[r:SUPPORTED_BY]->(:Source) DELETE r"
    ),
    "makes_offer_edge_dangling": (
        "MATCH (:Store {store_id:'store-north'})-[r:MAKES_OFFER]->"
        "(:Offer {offer_id:'off-north-serum'}) SET r.source_id = 'src-does-not-exist'"
    ),
    "for_edge_missing_source": (
        "MATCH (:Offer {offer_id:'off-north-serum'})-[r:FOR]->(:Variant) REMOVE r.source_id"
    ),
    "has_variant_edge_dangling": (
        "MATCH (:Product {product_id:'prod-serum-c'})-[r:HAS_VARIANT]->"
        "(:Variant {variant_id:'var-serum-30'}) SET r.source_id = 'src-does-not-exist'"
    ),
}


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.parametrize("case", sorted(_OFFER_CHAIN_SABOTAGE))
def test_an_unsourced_offer_chain_costs_the_price_not_the_snapshot(
    catalogue_market: dict[str, Any], case: str
) -> None:
    """The finest-grained refusal in the read, and the one a naive gate gets backwards.

    The store really is this store, the product really is this product, and the ``SELLS`` edge
    saying it is carried is honestly sourced. Only the *price* is unchecked. Dropping the whole
    entry would throw away every reading a claim could legitimately be graded against — the
    verifier would answer "unsupported" for attribute claims it had perfectly good evidence
    for — while keeping the price would have the platform state a number it never observed,
    which under D55 is the platform's false claim rather than the store's.

    Each of the four links is broken on its own because the read tests them as four separate
    conditions. Three of those conditions could be deleted and a test that only broke the
    fourth would stay green, which is the same shape of hole that lets an "unsourced offer"
    gate ship while only checking the node.
    """
    session = catalogue_market["session"]
    session.run(_OFFER_CHAIN_SABOTAGE[case]).consume()

    entry = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    assert entry is not None, f"{case}: the SELLS edge is still sourced, so the row must survive"
    _assert_states_no_price(entry, "28.0")
    assert entry.via == ["SELLS"], entry.via

    # Everything the platform *did* check survives untouched: the row is not degraded, only
    # the price is missing.
    assert _keys(entry) == HONEST_SERUM_KEYS
    assert entry.source_ids == ["src-catalog-fixture", "src-catalogue-second"]
    assert entry.canonical_name == "Gentle Vitamin C Serum"

    # Positive control: an untouched chain on the same store in the same graph still prices.
    priced = catalogue_entry(session, store_id="store-north", product_id="prod-spf-daily")
    assert priced is not None and priced.offer is not None
    assert priced.offer.price == pytest.approx(42.00)


@pytest.mark.docker
@pytest.mark.graph
def test_an_observed_listing_with_no_price_still_proves_the_store_carries_the_product(
    catalogue_market: dict[str, Any],
) -> None:
    """A price-less ``Offer`` costs the PRICE, never the carrying relation.

    ``store-offeronly`` is reachable only through its offer chain — it has no ``SELLS`` edge —
    and every link of that chain is honestly sourced. Strip the ``price`` PROPERTY off the
    ``Offer`` node and the platform has still observed this store listing this product; what it
    has not observed is what the store charges. Dropping the whole entry there would refuse to
    grade a claim the platform holds perfectly good evidence for (its readings, its canonical
    name) on the grounds that a different, unrelated fact was missing.

    It is also the rule :func:`candidate_shops` already keeps — that read puts such a shop on
    the roster with ``lowest_price`` ``None`` — so the two would otherwise disagree about
    whether the same graph shape means "this shop sells it" or "this shop does not exist".

    ``Offer`` requires a price, so ``upsert_offer`` always writes one; this shape is reachable
    only from raw Cypher or another writer, which is exactly the threat model the provenance
    conditions are Cypher rather than a Python convention for.
    """
    session = catalogue_market["session"]
    session.run("MATCH (o:Offer {offer_id:'off-only-serum'}) REMOVE o.price").consume()

    entry = catalogue_entry(session, store_id="store-offeronly", product_id="prod-serum-c")
    assert entry is not None, (
        "the whole entry was dropped over a missing price, so a store the platform observed "
        "listing this product can have no claim graded against the crawl at all"
    )
    assert entry.via == ["MAKES_OFFER"], entry.via
    _assert_states_no_price(entry, "31.5")
    assert _keys(entry) == HONEST_SERUM_KEYS
    assert entry.canonical_name == "Gentle Vitamin C Serum"

    # Positive control: the same chain WITH its price prices, and the shop the roster keeps for
    # this shape is the same shop this read keeps.
    priced = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    assert priced is not None and priced.offer is not None
    assert priced.offer.price == pytest.approx(28.00)


@pytest.mark.docker
@pytest.mark.graph
def test_an_unsourced_attribute_is_dropped_rather_than_graded(
    catalogue_market: dict[str, Any],
) -> None:
    """A reading nobody checked must be *absent*, not present-with-a-caveat.

    Both halves of an attribute are separate material facts and both are separately
    provenanced: the ``AttributeValue`` node is "a reading of pH 5.5 was observed somewhere",
    and the ``HAS_ATTRIBUTE`` edge is "*this product* was observed to have it". Either one
    failing means the platform cannot stand behind the reading for this product, and the two
    are checked by different clauses in the read, so each is sabotaged on its own.

    Absence is the load-bearing part. A dropped key makes a claim on it answer "this catalogue
    records no such key" — undecided, and visibly so. A key that survived with an unchecked
    value would be graded, and a claim graded against unchecked evidence is indistinguishable
    from a verified one in every consumer downstream.
    """
    session = catalogue_market["session"]
    source = catalogue_market["source"]
    upsert_attribute(
        session,
        product_id="prod-serum-c",
        attribute=AttributeValue("ph", value_number=5.5),
        source=source,
    )
    upsert_attribute(
        session,
        product_id="prod-serum-c",
        attribute=AttributeValue("texture", value_string="Lightweight"),
        source=source,
    )
    # Both are honest right now — assert that, or the drops below prove nothing.
    assert {"ph", "texture"} <= _keys(
        catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    )

    # (a) the reading itself is supported by nothing.
    session.run("MATCH (:AttributeValue {key:'ph'})-[r:SUPPORTED_BY]->() DELETE r").consume()
    # (b) the attachment claim names a Source that does not exist.
    session.run(
        "MATCH (:Product {product_id:'prod-serum-c'})-[r:HAS_ATTRIBUTE]->"
        "(:AttributeValue {key:'texture'}) SET r.source_id = 'src-does-not-exist'"
    ).consume()

    entry = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    assert entry is not None, "an unchecked attribute costs the attribute, never the entry"
    assert "ph" not in _keys(entry), "an AttributeValue with no Source reached the snapshot"
    assert "texture" not in _keys(entry), "an unsourced HAS_ATTRIBUTE reached the snapshot"
    # Positive control: every honest sibling on the same product is still there.
    assert _keys(entry) == HONEST_SERUM_KEYS


# =======================================================================================
# 3. Sourced is not the same as observed-by-us (D55's verification asymmetry)
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_store_s_own_statement_is_provenance_but_is_not_platform_observation(
    catalogue_market: dict[str, Any],
) -> None:
    """The gate this whole read exists for: a store cannot be its own evidence.

    ``store-asserted`` is *correctly* provenanced. ``provenance_violations`` reports nothing
    about it, ``candidate_shops`` puts it on the roster, and it is right to — a roster row only
    decides who gets asked. But its only ``Source`` is the store telling the platform about
    itself, so a snapshot built from it would hand the verifier the seller's assertion as the
    evidence for the seller's claim. That check cannot fail, and it would report as *verified*
    rather than as *unchecked*, which is strictly worse than holding no snapshot at all.

    This is the one refusal in the file that no amount of ``source_id``-resolution testing
    would catch: every id here resolves, to a real ``Source``, with a valid ``source_class``.
    Only the class filter separates it from the honest case.

    The escape hatch is asserted too. If widening ``source_classes`` did not actually change
    the answer, the default would be an accident of the query rather than a policy — and a
    later reader would have no way to tell which. It changes the answer, so the narrow default
    is a decision someone made.
    """
    session = catalogue_market["session"]
    upsert_store(
        session,
        Store("store-asserted", "asserted.example", "Self Reported Ltd", 2),
        source=SELLER_SOURCE,
    )
    link_sells(
        session,
        store_id="store-asserted",
        product_id="prod-serum-c",
        source=SELLER_SOURCE,
    )

    assert catalogue_entry(session, store_id="store-asserted", product_id="prod-serum-c") is None, (
        "a store's own statement became the evidence its own claims are graded against"
    )
    # Positive control: a shop the platform actually crawled still gets a snapshot.
    assert catalogue_entry(session, store_id="store-north", product_id="prod-serum-c") is not None

    widened = catalogue_entry(
        session,
        store_id="store-asserted",
        product_id="prod-serum-c",
        source_classes=("scraped", "seller_asserted"),
    )
    assert widened is not None, "widening source_classes must actually reach the same data"
    assert widened.store_id == "store-asserted"
    assert widened.via == ["SELLS"]
    assert widened.source_ids == ["src-catalog-fixture", "src-seller-says"], widened.source_ids

    assert PLATFORM_OBSERVED_SOURCE_CLASSES == frozenset({"scraped", "pixel_feed"}), (
        "the default is an allow-list of the platform's own observation; adding a class the "
        "seller authors to it is the D55 failure this read is shaped to prevent"
    )


@pytest.mark.docker
@pytest.mark.graph
def test_an_attribute_only_the_seller_asserted_is_not_evidence_about_the_seller(
    catalogue_market: dict[str, Any],
) -> None:
    """The same gate at attribute resolution, which is where a claim is actually graded.

    ``claim_verification`` matches a claim's key against this snapshot's keys. A seller who
    asserts "clinically proven" and whose assertion is written into the graph as an
    ``AttributeValue`` would, without this gate, have that claim come back **supported** — by
    itself. The store node here is honest and scraped, so nothing coarser than a per-attribute
    class check can catch it: the shop is real, the shelf is real, and only this one reading
    came from the shop's own mouth.

    The scraped sibling asserted in the same call is what proves the drop is the class filter
    and not the read losing attributes generally.
    """
    session = catalogue_market["session"]
    upsert_attribute(
        session,
        product_id="prod-serum-c",
        attribute=AttributeValue("clinically_proven", value_bool=True),
        source=SELLER_SOURCE,
    )

    entry = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    assert entry is not None
    assert "clinically_proven" not in _keys(entry), (
        "the seller's own claim was filed as the platform's evidence for that claim"
    )
    assert _keys(entry) == HONEST_SERUM_KEYS, "positive control: the scraped siblings survive"

    widened = catalogue_entry(
        session,
        store_id="store-north",
        product_id="prod-serum-c",
        source_classes=("scraped", "seller_asserted"),
    )
    assert widened is not None
    assert ("clinically_proven", True) in {(a.key, a.value) for a in widened.attributes}, (
        "the reading is present in the graph and reachable; only the default policy hides it"
    )


@pytest.mark.docker
@pytest.mark.graph
def test_a_price_only_the_owner_stated_is_not_a_price_the_platform_observed(
    catalogue_market: dict[str, Any],
) -> None:
    """``owner_statement`` is refused too, and the price is the tempting place to relax it.

    ``PLATFORM_OBSERVED_SOURCE_CLASSES`` is an allow-list of two, not a deny-list containing
    ``seller_asserted``; testing only that one class would leave the other five — including
    ``owner_statement``, ``network`` and ``learned_policy`` — free to be added by a one-line
    edit. An owner-stated 9.99 on a shelf the platform genuinely crawled is precisely the
    number a store would like quoted back at it, and a buyer-side pitch built on it would have
    the platform advertising a price nobody checked.

    ``store-quiet``'s ``SELLS`` edge stays honest, so the entry survives — the price is what is
    refused, exactly as for a structurally unsourced chain.
    """
    session = catalogue_market["session"]
    upsert_variant(
        session,
        Variant("var-serum-quiet", "SKU-QUIET-30", "30 ml"),
        product_id="prod-serum-c",
        source=OWNER_SOURCE,
    )
    upsert_offer(
        session,
        Offer("off-quiet-serum", 9.99, "USD", "in_stock", OBSERVED_AT),
        store_id="store-quiet",
        variant_id="var-serum-quiet",
        source=OWNER_SOURCE,
    )

    entry = catalogue_entry(session, store_id="store-quiet", product_id="prod-serum-c")
    assert entry is not None and entry.via == ["SELLS"], "the crawled shelf is still a snapshot"
    _assert_states_no_price(entry, "9.99")
    assert _keys(entry) == HONEST_SERUM_KEYS

    # Positive control: a scraped chain in the same graph still states its price.
    north = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    assert north is not None and north.offer is not None
    assert north.offer.price == pytest.approx(28.00)

    widened = catalogue_entry(
        session,
        store_id="store-quiet",
        product_id="prod-serum-c",
        source_classes=("scraped", "owner_statement"),
    )
    assert widened is not None and widened.offer is not None
    assert widened.offer.price == pytest.approx(9.99), "the listing is there; the policy hides it"


@pytest.mark.docker
@pytest.mark.graph
def test_the_platform_s_own_pixel_feed_is_observation_and_counts(
    catalogue_market: dict[str, Any],
) -> None:
    """``scraped`` is not the only thing the platform observes, and the gate must say so.

    A gate written as "reject ``seller_asserted``" and a gate written as "accept ``scraped``"
    both pass every test above; they differ only on ``pixel_feed``, which is the platform's own
    instrumentation and therefore evidence by exactly the same argument that admits the crawl.
    Getting this wrong silently deletes every behavioural reading the platform measured itself
    — the readings a seller has the least ability to author and the verifier has the most
    reason to trust.

    The ``seller_asserted`` reading written into the same graph, on the same product, and
    dropped from the same call, is what makes this a set-membership assertion rather than a
    "does anything survive" one.
    """
    session = catalogue_market["session"]
    upsert_attribute(
        session,
        product_id="prod-serum-c",
        attribute=AttributeValue("return_rate", value_number=0.03),
        source=PIXEL_SOURCE,
    )
    upsert_attribute(
        session,
        product_id="prod-serum-c",
        attribute=AttributeValue("dermatologist_recommended", value_bool=True),
        source=SELLER_SOURCE,
    )

    entry = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    assert entry is not None
    by_key = {a.key: a for a in entry.attributes}
    assert "return_rate" in by_key, "the platform's own instrumentation was refused as evidence"
    assert by_key["return_rate"].value == pytest.approx(0.03)
    assert by_key["return_rate"].source_ids == ["src-pixel-feed"]
    assert "dermatologist_recommended" not in by_key, (
        "the class filter is an allow-list, not 'anything that is not scraped'"
    )
    assert HONEST_SERUM_KEYS <= set(by_key), "positive control: the scraped siblings survive"


# =======================================================================================
# 4. Shape the consumer depends on: several readings per key, sorted, and the cheapest offer
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_multi_valued_attribute_survives_as_several_readings(
    catalogue_market: dict[str, Any],
) -> None:
    """One key, two observed values, two readings — the consumer folds them into a list.

    ``AttributeValue`` nodes are content-addressed, so "skin type: Sensitive" and "skin type:
    Normal" are two distinct nodes hanging off the same product, and a catalogue that
    legitimately records both is ordinary rather than corrupt. A read that keyed its result by
    ``key`` — a ``dict`` comprehension is the obvious way to write it — would silently keep
    whichever value Neo4j returned last, and a claim of "suitable for normal skin" would then
    be graded against a snapshot that had thrown the supporting reading away. Nothing would
    raise; the answer would just be wrong roughly half the time.
    """
    session = catalogue_market["session"]
    upsert_attribute(
        session,
        product_id="prod-serum-c",
        attribute=AttributeValue("skin_type", value_string="Normal"),
        source=catalogue_market["source"],
    )

    entry = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    assert entry is not None
    skin = [a for a in entry.attributes if a.key == "skin_type"]
    assert [a.value for a in skin] == ["Normal", "Sensitive"], skin
    assert {a.canonical_key for a in skin} == {"skin-type"}
    # Positive control: nothing else was disturbed by the second reading of the same key.
    assert _keys(entry) == HONEST_SERUM_KEYS


@pytest.mark.docker
@pytest.mark.graph
def test_the_snapshot_is_deterministic_and_sorted(
    catalogue_market: dict[str, Any],
) -> None:
    """Two identical calls return equal entries, with every list in a defined order.

    Neo4j does not promise a collection order, so every list on this entry is ordered in
    Python or not at all. This matters more here than in most reads: a snapshot is the input
    to an LLM verifier and to a buyer-side pitch, and a prompt whose facts are shuffled between
    two runs makes every downstream evaluation non-reproducible — a graded claim would flip
    without a single fact changing. It also makes the entry usable as a cache key and
    comparable with ``==``, which the equality assertion below is the proof of.

    Extra provenance is written first so the assertions are not vacuous: the store, the product
    and the offer each end up with several supporting sources, inserted in an order that is
    deliberately not the sorted one.
    """
    session = catalogue_market["session"]
    source = catalogue_market["source"]
    store = Store("store-north", "north.example", "Northlight Retail Ltd", 1)
    offer = Offer("off-north-serum", 28.00, "USD", "in_stock", OBSERVED_AT)

    # Insertion order: second, pixel, fixture — none of which is the sorted order.
    upsert_store(session, store, source=PIXEL_SOURCE)
    upsert_store(session, store, source=source)
    upsert_offer(
        session, offer, store_id="store-north", variant_id="var-serum-30", source=PIXEL_SOURCE
    )
    upsert_offer(
        session, offer, store_id="store-north", variant_id="var-serum-30", source=SECOND_SOURCE
    )
    upsert_attribute(
        session,
        product_id="prod-serum-c",
        attribute=AttributeValue("skin_type", value_string="Normal"),
        source=source,
    )

    first = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    second = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    assert first is not None and first == second, (first, second)

    ordering = [(a.key, str(a.value)) for a in first.attributes]
    assert len(ordering) == 5, ordering
    assert ordering == sorted(ordering), ordering

    assert first.source_ids == [
        "src-catalog-fixture",
        "src-catalogue-second",
        "src-pixel-feed",
    ], first.source_ids
    assert first.offer is not None
    assert first.offer.source_ids == [
        "src-catalog-fixture",
        "src-catalogue-second",
        "src-pixel-feed",
    ], first.offer.source_ids


@pytest.mark.docker
@pytest.mark.graph
def test_the_cheapest_provenanced_offer_is_the_one_the_snapshot_states(
    catalogue_market: dict[str, Any],
) -> None:
    """A pair with two observed listings states the cheaper, and states the roster's number.

    A store with several variants has several offers, and "the price" is then a choice. It has
    to be the same choice ``exchange.retrieval.roster`` makes, or the platform quotes one
    number when it solicits the shop and grades the shop's claims against a different one —
    "we are cheaper than the listed price" would be adjudicated against a price the buyer was
    never shown. Nothing connects the two rules but this assertion: they are separate Cypher in
    separate functions, and each is individually reasonable while disagreeing.
    """
    session = catalogue_market["session"]
    source = catalogue_market["source"]
    upsert_variant(
        session,
        Variant("var-serum-15", "SKU-SERUM-15", "15 ml"),
        product_id="prod-serum-c",
        source=source,
    )
    upsert_offer(
        session,
        Offer("off-north-serum-small", 24.50, "USD", "in_stock", OBSERVED_AT),
        store_id="store-north",
        variant_id="var-serum-15",
        source=source,
    )

    entry = catalogue_entry(session, store_id="store-north", product_id="prod-serum-c")
    assert entry is not None and entry.offer is not None
    assert entry.offer.offer_id == "off-north-serum-small", entry.offer
    assert entry.offer.price == pytest.approx(24.50)
    assert entry.offer.variant_id == "var-serum-15"

    shops = candidate_shops(session, category="Serum", limit=10)
    north = next(shop for shop in shops if shop.store_id == "store-north")
    assert north.lowest_price == pytest.approx(entry.offer.price), (
        f"the roster quotes {north.lowest_price} and the snapshot grades against "
        f"{entry.offer.price}"
    )


# =======================================================================================
# 5. The instant comparison the entry's `captured_at` rests on
# =======================================================================================
def test_the_latest_observation_is_decided_by_instant_and_not_by_punctuation() -> None:
    """``captured_at`` is compared as a TIME, and this graph really does hold two spellings.

    The crawl stamps ``%Y-%m-%dT%H:%M:%SZ`` (``ingest.scheduler.catalog.observed_now``) while
    this suite's own fixtures write ``+00:00`` — and ``"2026-01-02T00:00:00+00:00"`` sorts
    BELOW ``"2026-01-01T00:00:00Z"`` as text, because ``+`` is a smaller byte than ``Z``. A
    text ``max`` therefore picks the older observation out of a re-crawled graph, and
    ``captured_at`` is the reference instant ``claim_verification``'s stale-evidence gate
    measures an attribute's own ``observed_at`` back from: an older reference makes stale
    evidence look fresh, which is the permissive direction.

    The winner keeps its own spelling, unchanged, so the snapshot carries what the graph says
    rather than a re-rendering of it — and a stamp that will not parse is ignored rather than
    allowed to win a comparison it has no business winning.
    """
    assert latest_instant(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00+00:00"]) == (
        "2026-01-02T00:00:00+00:00"
    ), "the later instant lost to the earlier one on punctuation"
    assert latest_instant(["2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00Z"]) == (
        "2026-01-02T00:00:00+00:00"
    ), "the answer must not depend on the order the rows came back in"
    assert latest_instant(["2026-03-04T05:06:07Z"]) == "2026-03-04T05:06:07Z"
    assert latest_instant(["", "   ", "not-a-date"]) == ""
    assert latest_instant([]) == ""
    assert latest_instant(["gibberish", "2026-01-01T00:00:00Z"]) == "2026-01-01T00:00:00Z"
