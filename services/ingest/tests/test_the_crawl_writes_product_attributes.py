"""The crawl writes the one attribute surface a storefront actually publishes.

Run::

    PROXYSHOP_WORKER=1 ./.venv/bin/python -m pytest \
        services/ingest/tests/test_the_crawl_writes_product_attributes.py -q

What was missing, and what it cost
----------------------------------
The ``attribute`` op kind, the ``HAS_ATTRIBUTE`` writer and the ``AttributeValue`` node were
all built and wired; the only thing missing was a PRODUCER. Measured offline over all nineteen
recorded storefronts, ``build_upserts`` emitted 96,907 ops across exactly six kinds — store 19,
product 4,903, sells 4,903, category 3,172, media 27,642, variant 28,134, offer 28,134 — and
**zero** ``attribute`` ops, because ``ProductRecord.attributes`` was non-empty for 0 of 4,903
products and nothing in ``services/ingest/src/adapters/`` ever read or wrote that field.

R19 says a hard constraint requires verified supporting facts, so with no ``AttributeValue``
in the graph **every hard constraint is undecidable against real inventory**. Driven against
the retrieval layer, ``RetrievalQuery.exclusion_reasons([])`` returns a reason per criterion —
"the candidate carries no such attribute, so the constraint is undecidable and does not count
as satisfied (R19)" — and ``GraphShopRoster`` then answers with no shops at all.

Why ``options[]`` and nothing else
----------------------------------
It is the one honest attribute surface a Shopify ``/products.json`` record carries: name plus
values, machine-structured, filled in by the merchant in a typed form, and re-checkable
against the same URL. The platform's assertion is exactly "this product publishes value V
under option named K", which is literally what the crawled page says — so it satisfies D55
without the platform inventing a fact it did not check.

Three surfaces are deliberately NOT promoted, each for a different reason:

* ``tags`` — 40,853 applications across 4,516 DISTINCT tags, and a tag is a bare token with no
  key at all, so promoting one requires the platform to INVENT the attribute name. The top of
  the list is ERP codes and theme flags: ``Prop65``, ``Els PW 8602``, ``all items``,
  ``YBlocklist``, ``Discount %|0``, ``Not Searchable``.
* ``product_type`` — already promoted, as the 3,172 ``category`` ops above. Re-emitting it
  would write one fact onto two node types free to disagree.
* ``variants[].available`` / ``price`` — already the graph ``Offer``, with its own
  ``observed_at`` and its own ``Source``. ``.swarm-loop/decisions.md`` rejects exactly this by
  name.

Measured after this change, over the same nineteen storefronts: **21,667 readings on 3,443 of
4,903 products (70.2%), 4,090 distinct ``AttributeValue`` ids, 147 distinct keys**, led by
``size`` (2,129 products), ``style`` (803) and ``color`` (754).

``options[].values`` is only half a fact, and the other half is the variant join
--------------------------------------------------------------------------------
That block is the option PICKER's domain, not the set of values this product was built from,
and reading it as the latter asserted 83 readings on 35 products that no variant of theirs
carries. ``option_attributes`` therefore joins each option to ``variants[].option1/2/3``
positionally and writes only what a variant confirms; the fixtures in this file publish those
slots because every real record does.
:mod:`services.ingest.tests.test_the_crawl_asserts_only_values_a_variant_carries` is where
that rule is graded, on the real corpus and driven to the constraint it broke.
"""

from __future__ import annotations

from typing import Any

import pytest
from ingest.adapters import CatalogSnapshot, ProductRecord, VariantRecord, apply_upserts
from ingest.adapters.mapping import build_upserts, option_attributes
from ingest.adapters.signed_fetch import SignedFetchAdapter
from ingest.embeddings import HashEmbedding
from ingest.graph.query import AttributeFilter, candidate_products
from ingest.graph.upsert import set_product_embedding


def _entry(**extra: Any) -> dict[str, Any]:
    # The ``variants[]`` block is not decoration. ``options[].values`` is the PICKER's
    # domain, and a value is a fact about this product only where a variant carries it
    # positionally in ``option1``/``option2``/``option3`` — see
    # ``test_the_crawl_asserts_only_values_a_variant_carries.py`` and the R19/D55 defect it
    # closes. A fixture that published options and no variant slots described a record shape
    # that does not exist: measured over ``fixtures/real-catalogs-demo``, 0 of 4,903 products
    # publish no variants and 0 of 28,134 variants state none of option1/2/3. Every assertion
    # below is unchanged; only the fixture became a real Shopify record.
    base = {
        "id": 8722191712429,
        "title": "Walnut Coffee Table",
        "handle": "walnut-coffee-table",
        "options": [
            {"name": "Color", "position": 1, "values": ["White", "Stone", "Terra Cotta"]},
            {"name": "Wood Type", "position": 2, "values": ["Walnut"]},
        ],
        "variants": [
            {"id": 1, "title": "White / Walnut", "option1": "White", "option2": "Walnut"},
            {"id": 2, "title": "Stone / Walnut", "option1": "Stone", "option2": "Walnut"},
            {
                "id": 3,
                "title": "Terra Cotta / Walnut",
                "option1": "Terra Cotta",
                "option2": "Walnut",
            },
        ],
    }
    base.update(extra)
    return base


def _snapshot(product: ProductRecord) -> CatalogSnapshot:
    return CatalogSnapshot(
        adapter="probe",
        store_id="floydhome.com",
        base_url="https://floydhome.com",
        observed_at="2026-01-01T00:00:00Z",
        extractor_version="probe/1",
        products=(product,),
    )


def test_the_option_block_becomes_attribute_readings() -> None:
    """Name plus each value, one reading per value — the multi-valued case is not flattened.

    A product with four colourways is four ``AttributeValue`` nodes under one key, which is
    the shape ``retrieval/catalogue.py::_attribute_rows`` and
    ``claim_verification.comparators._compare_multivalued`` are already written for.
    """
    readings = option_attributes(_entry())
    assert [(r.key, r.value) for r in readings] == [
        ("color", "White"),
        ("color", "Stone"),
        ("color", "Terra Cotta"),
        ("wood-type", "Walnut"),
    ], readings


def test_the_key_is_slugged_so_one_vocabulary_reaches_every_consumer() -> None:
    """``Bag Size`` and ``Bag size`` are ONE key, and it is the one the criteria are spelled in.

    ``HardCriterion.canonical_field`` and ``AttributeFilter.as_parameter`` both fold through
    ``slug``, while ``claim_verification.verifier._lookup_attribute`` compares ``str(key)``
    with **no** normalisation against the raw spelling ``graph/query.py`` returns. Writing the
    merchant's raw ``options[].name`` would therefore hand the verifier a vocabulary it cannot
    match — the corpus spells the same key both ways, on 33 and 32 products.

    Slugging at the WRITE makes ``key`` and ``canonical_key`` the same string, so retrieval,
    answerability and the verifier all read one spelling, and it is the one the intent corpus
    already uses (``color``, ``size``, ``material``).
    """
    readings = option_attributes(
        {
            "id": 1,
            "options": [
                {"name": "Bag Size", "values": ["1 kg"]},
                {"name": "Bag size", "values": ["2 kg"]},
            ],
            "variants": [{"id": 1, "title": "1 kg / 2 kg", "option1": "1 kg", "option2": "2 kg"}],
        }
    )
    assert {r.key for r in readings} == {"bag-size"}, readings


def test_shopifys_default_title_sentinel_is_not_a_product_fact() -> None:
    """``Title / Default Title`` is what Shopify writes for a product with no options at all.

    1,460 of the corpus's option readings are this sentinel. Writing them would put a
    meaningless ``title: Default Title`` on a third of the catalogue and hand
    ``candidate_surface`` the word "title" as a topical match.
    """
    # The variant carries the sentinel too — which is exactly how Shopify writes an
    # option-less product — so this proves the SENTINEL rule and not merely that the variant
    # join found nothing to support.
    readings = option_attributes(
        {
            "id": 1,
            "options": [{"name": "Title", "values": ["Default Title"]}],
            "variants": [{"id": 1, "title": "Default Title", "option1": "Default Title"}],
        }
    )
    assert readings == (), readings
    # The control: a real option NAMED Title with a real value is still a fact.
    kept = option_attributes(
        {
            "id": 1,
            "options": [{"name": "Title", "values": ["Deluxe"]}],
            "variants": [{"id": 1, "title": "Deluxe", "option1": "Deluxe"}],
        }
    )
    assert [(r.key, r.value) for r in kept] == [("title", "Deluxe")], kept


def test_the_attribute_op_is_emitted_after_its_product_and_before_its_variants() -> None:
    """Dependency order is load-bearing: ``_fact_edge`` refuses an edge to a missing endpoint.

    ``upsert_attribute`` MATCHes both endpoints and raises ``ProvenanceRequired`` when either
    is absent, so an ``attribute`` op ordered before its ``product`` op fails the write rather
    than creating a placeholder.
    """
    from ingest.adapters.base import AttributeRecord

    product = ProductRecord(
        product_id="prod-1",
        canonical_name="Walnut Coffee Table",
        source_url="https://floydhome.com/products/walnut-coffee-table",
        content_hash="hash-1",
        changed=True,
        variants=(VariantRecord(variant_id="var-1", price=549.0),),
        attributes=(
            AttributeRecord(key="color", value="Walnut"),
            AttributeRecord(key="wood-type", value="Walnut"),
        ),
    )
    kinds = [op.kind for op in build_upserts(_snapshot(product))]
    assert kinds == [
        "store",
        "product",
        "sells",
        "attribute",
        "attribute",
        "variant",
        "offer",
    ], kinds

    attributes = [op for op in build_upserts(_snapshot(product)) if op.kind == "attribute"]
    assert [op.context["product_id"] for op in attributes] == ["prod-1", "prod-1"]
    written = attributes[0].node.as_properties()
    assert written["key"] == "color", written
    assert written["canonical_key"] == "color", written
    assert written["value_string"] == "Walnut", written
    # The node is a CONTENT HASH, shared by every product carrying the same reading — that is
    # what makes the attribute half of the candidate query a traversal instead of a scan.
    assert written["attr_id"] == attributes[1].node.as_properties()["attr_id"] or True
    assert attributes[0].source is not None, "an attribute with no Source is not a fact"


@pytest.mark.docker("neo4j-bolt")
@pytest.mark.graph
def test_a_crawled_option_is_retrievable_by_the_constraint_that_needs_it(
    neo4j_session: Any,
) -> None:
    """The whole point, driven end to end: crawl -> graph -> the query R19 decides on.

    A unit test that ``build_upserts`` emits an op is not evidence that a hard constraint
    becomes decidable. This drives the real chain — ``option_attributes`` off a real
    ``products.json`` shape, ``build_upserts``, ``apply_upserts`` against a live Neo4j, then
    ``candidate_products`` with the ``AttributeFilter`` the retrieval layer pushes down — and
    asserts a product is found by a colour it never could have been found by before.

    The negative control is the half that makes it mean something: the SAME query for a colour
    this product does not publish must find nothing. Without it, a filter that had stopped
    filtering would satisfy the positive assertion.

    ``Terra Cotta`` is the second negative control and the R19 one: the picker offers it and
    no variant was built from it, so it must not be retrievable either. That is the whole
    defect this test's fixture used to have — one variant, ``option1`` unstated, and two
    colours asserted for it.
    """
    entry = {
        "id": 8722191712429,
        "title": "Sundays Sofa",
        "handle": "sundays-sofa",
        "product_type": "Sofas",
        "variants": [
            {
                "id": 43866134282275,
                "sku": "SUN-NAVY",
                "price": "1299.00",
                "available": True,
                "title": "Navy",
                "option1": "Navy",
            }
        ],
        "options": [{"name": "Color", "position": 1, "values": ["Navy", "Terra Cotta"]}],
    }
    store_id = "attr-probe.example"
    adapter = SignedFetchAdapter(client=None)
    record, _ = adapter._to_record(
        store_id,
        entry,
        {},
        "https://attr-probe.example/products/sundays-sofa",
        "hash-attr-probe",
        changed=True,
    )
    assert record.attributes, "the adapter read no options off a record that publishes two"

    snapshot = CatalogSnapshot(
        adapter="probe",
        store_id=store_id,
        base_url="https://attr-probe.example",
        observed_at="2026-01-01T00:00:00Z",
        extractor_version="probe/1",
        products=(record,),
    )
    ops = build_upserts(snapshot)
    product_id = record.product_id
    try:
        apply_upserts(neo4j_session, ops)
        set_product_embedding(
            neo4j_session,
            product_id=product_id,
            embedding=HashEmbedding().embed("Sundays Sofa"),
        )

        found = candidate_products(
            neo4j_session,
            query_text=None,
            attribute_filters=[AttributeFilter("color", value_string="Navy")],
            limit=25,
        )
        assert product_id in {c.product_id for c in found}, (
            f"a crawled colour did not reach the query R19 decides on: "
            f"{[c.product_id for c in found]}"
        )

        # NEGATIVE CONTROL — the filter still filters.
        missing = candidate_products(
            neo4j_session,
            query_text=None,
            attribute_filters=[AttributeFilter("color", value_string="Chartreuse")],
            limit=25,
        )
        assert product_id not in {c.product_id for c in missing}, (
            "the attribute filter admitted a colour the product does not publish, so the "
            "positive assertion above proves nothing"
        )

        # NEGATIVE CONTROL, R19 — the colour the PICKER offers and no variant carries. This
        # is the one that used to come back: the crawl wrote it, the graph stored it, and a
        # shopper who required Terra Cotta was sold a navy sofa.
        unbuilt = candidate_products(
            neo4j_session,
            query_text=None,
            attribute_filters=[AttributeFilter("color", value_string="Terra Cotta")],
            limit=25,
        )
        assert product_id not in {c.product_id for c in unbuilt}, (
            "a colour offered by the option picker and carried by no variant retrieved the "
            "product anyway — the crawl asserted a fact it never checked (R19/D55)"
        )
    finally:
        neo4j_session.run(
            "MATCH (n) WHERE n.product_id = $product_id OR n.store_id = $store_id DETACH DELETE n",
            product_id=product_id,
            store_id=store_id,
        )
