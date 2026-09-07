"""The crawl stopped throwing ``images[]`` away — ingest half only (media, not scoring).

WHAT WAS WRONG. ``SignedFetchAdapter`` read ``title``, ``price``, ``sku`` and ``variants``
off ``products.json`` and dropped ``images[]`` on the floor. The recorded corpus publishes
**18,165 image records** across 3,093 real products and not one of them reached the graph,
so a shortlist slot had nothing to show and the owner's media ruling had no inputs at all.

THE RULING, and exactly how much of it lives here. Media is **verified-primary, with
labelled-unverified as the fallback**: an asset scores only if it

1. resolves,
2. sits on the seller's **registered domain**, and
3. its content hash matches the catalogue snapshot for that ``product_ref``.

Legs 1 and 3 need somebody to fetch the bytes. **Ingest does not, anywhere**, and this file
asserts that it does not. What ingest owes is that the question be *decidable* later: every
input to leg 2 present and provenanced, and a ``catalogue_hash`` that pins the seller's own
record of the file so a verifier knows when its byte pin expired. There is no scoring term
here and nothing in this file touches the ranker.

THE MEASUREMENT THAT MATTERS MOST, and it is uncomfortable: **all 18,165 image records in
the recorded corpus are on ``cdn.shopify.com``. Zero are on any seller's own registered
domain.** Under a literal reading of leg 2, not one real image would ever score. So
``MediaAsset.on_seller_domain`` reports the fact rather than pre-judging it, and whoever
implements scoring has to decide whether a merchant's platform CDN counts as its registered
domain. That decision is theirs; getting it wrong silently is what this flag prevents.
"""

from __future__ import annotations

import gzip
import json
import socket
from typing import Any
from unittest import mock

import pytest
from ingest.adapters.base import ImageRecord, ProductRecord, apply_upserts
from ingest.adapters.mapping import (
    MEDIA_PER_PRODUCT_LIMIT,
    build_upserts,
    image_records,
    media_asset_id_for,
    on_seller_domain,
)
from ingest.adapters.recorded import RecordedCorpus, RecordedStore, RecordedTransport
from ingest.adapters.signed_fetch import SignedFetchAdapter
from ingest.graph import MediaAsset, provenance_violations
from ingest.graph.model import ID_PROPERTY, MATERIAL_FACT_EDGES, MATERIAL_FACT_LABELS
from ingest.scheduler.load_corpus import corpus_target

#: A store small enough to drive end to end and rich enough to have galleries: 111 real
#: products carrying 595 image records.
MEDIA_HOST = "gaiaherbs.com"


def _crawl(store: RecordedStore) -> Any:
    """Read one recorded store through the live adapter, over the replayed transport.

    The request comes from :func:`~ingest.scheduler.load_corpus.corpus_target`, not from a
    hand-built one: the collector paginated at ``limit=250`` and the recorded byte spans are
    of *those* pages, so a ceiling below 250 asks for a response nobody served and
    :class:`~ingest.adapters.recorded.RecordedTransport` refuses it. Building the request
    here by hand is how a test quietly ends up asserting over an empty snapshot.
    """
    adapter = SignedFetchAdapter(client=RecordedTransport(store=store))
    return adapter, adapter.fetch_catalog(corpus_target(store).request())


def _entry(host: str = MEDIA_HOST, index: int = 0) -> dict[str, Any]:
    """One verbatim recorded catalogue entry, straight off disk."""
    path = RecordedCorpus.load(hosts=(host,)).root / "stores" / f"{host}.products.jsonl.gz"
    lines = [line for line in gzip.decompress(path.read_bytes()).split(b"\n") if line.strip()]
    return json.loads(lines[index])


# =======================================================================================
# 1. The records survive the read
# =======================================================================================


def test_the_crawl_no_longer_discards_the_catalogue_gallery() -> None:
    """The regression this whole item is: ``images[]`` reaching a ``ProductRecord``."""
    entry = _entry()
    assert len(entry["images"]) == 9, "the fixture product publishes nine images"

    record, published = SignedFetchAdapter()._to_record(
        MEDIA_HOST, entry, {}, "https://www.gaiaherbs.com/products/x", "sha256:ab", changed=True
    )
    assert published == 9
    assert len(record.images) == 9
    assert [image.position for image in record.images] == list(range(1, 10))
    assert record.images[0].src == entry["images"][0]["src"]
    assert record.images[0].width == entry["images"][0]["width"] > 0


def test_a_gallery_is_read_in_catalogue_order_so_the_primary_is_never_the_one_dropped() -> None:
    """``position`` orders the gallery, and the kept prefix is its front.

    A bound that dropped from the *front* would throw away exactly the image the
    verified-**primary** rule is about, which is the one way this bound could be worse than
    no bound at all.
    """
    entry = {
        "id": 1,
        "images": [
            {"id": 30, "src": "https://cdn.example/c.png", "position": 3},
            {"id": 10, "src": "https://cdn.example/a.png", "position": 1},
            {"id": 20, "src": "https://cdn.example/b.png", "position": 2},
        ],
    }
    images, published = image_records(entry, limit=2)
    assert published == 3
    assert [image.src for image in images] == [
        "https://cdn.example/a.png",
        "https://cdn.example/b.png",
    ]
    assert images[0].position == 1


def test_the_gallery_is_bounded_against_a_store_that_publishes_thousands() -> None:
    """``images[]`` is merchant-controlled and unbounded; the ceiling is not negotiable.

    The number is measured rather than picked: on the 3,093 recorded products the mean
    gallery is 5.9 images, the median 5, p90 10, p99 17 and the **largest 81**. A cap of
    12 keeps 96.4% of every image in the corpus and truncates 2.8% of its products, while
    capping a hostile store at 12 nodes per product however many URLs it publishes.
    """
    hostile = {
        "id": 1,
        "images": [
            {"id": n, "src": f"https://cdn.example/{n}.png", "position": n} for n in range(1, 5001)
        ],
    }
    images, published = image_records(hostile)
    assert published == 5000
    assert len(images) == MEDIA_PER_PRODUCT_LIMIT == 12


def test_the_recorded_corpus_really_does_exceed_the_bound_and_the_crawl_says_so() -> None:
    """The truncation is reported, aggregated, rather than hidden or shouted 88 times."""
    corpus = RecordedCorpus.load(hosts=("nutricost.com",))
    store = corpus.by_host("nutricost.com")
    over = sum(
        1
        for page in store.pages
        for product in json.loads(page.body)["products"]
        if len(product.get("images") or []) > MEDIA_PER_PRODUCT_LIMIT
    )
    assert over == 40, "measured: 40 nutricost products publish more than 12 images"

    _adapter, snapshot = _crawl(store)
    truncation = [w for w in snapshot.warnings if "images" in w]
    assert truncation == [
        f"{over} product(s) published more than 12 images; kept the first 12 by catalogue position"
    ]
    assert all(len(p.images) <= MEDIA_PER_PRODUCT_LIMIT for p in snapshot.products)


def test_an_image_with_no_url_is_dropped_rather_than_collapsed_onto_one_node() -> None:
    """An entry with no ``src`` can be neither shown nor checked.

    Keeping it would key every such image of a product on the empty string, so a product's
    whole unnamed gallery would MERGE onto one node — the same collision
    :func:`~ingest.adapters.mapping.product_id_for` refuses for products.
    """
    images, published = image_records(
        {
            "id": 1,
            "images": [
                {"id": 1, "src": "", "position": 1},
                {"id": 2, "position": 2},
                "not-a-dict",
                {"id": 3, "src": "https://cdn.example/real.png", "position": 3},
            ],
        }
    )
    assert published == 1
    assert [image.src for image in images] == ["https://cdn.example/real.png"]
    with pytest.raises(ValueError, match="url must be non-empty"):
        MediaAsset(asset_id="mda_x", url="", catalogue_hash="sha256:ab")


def test_ingest_never_fetches_an_image(caplog: Any) -> None:
    """The line this half of the work does not cross, asserted rather than promised.

    A whole real storefront is read and mapped with every socket constructor poisoned. The
    URLs land; the bytes behind them are never touched, because deciding whether an asset
    resolves is the downstream verifier's job and doing it here would put a crawler on
    18,165 CDN URLs.
    """
    store = RecordedCorpus.load(hosts=(MEDIA_HOST,)).by_host(MEDIA_HOST)
    with (
        mock.patch.object(socket, "socket", side_effect=AssertionError("opened a socket")),
        mock.patch.object(
            socket, "create_connection", side_effect=AssertionError("opened a connection")
        ),
    ):
        adapter, snapshot = _crawl(store)
        ops = adapter.to_upserts(snapshot)

    assert snapshot.warnings == (), f"the replay refused something: {snapshot.warnings}"
    assert len(snapshot.products) == 111
    assert sum(len(p.images) for p in snapshot.products) == 595
    assert sum(1 for op in ops if op.kind == "media") == 595


# =======================================================================================
# 2. What a downstream verifier is left with
# =======================================================================================


def test_every_input_to_the_registered_domain_leg_is_on_the_node() -> None:
    """Leg 2 of the owner's rule is a comparison, not a re-parse of merchant text."""
    assert on_seller_domain("cdn.gaiaherbs.com", "gaiaherbs.com") is True
    assert on_seller_domain("gaiaherbs.com", "gaiaherbs.com") is True
    assert on_seller_domain("cdn.shopify.com", "gaiaherbs.com") is False
    # The suffix trap: a bare `endswith` would call this a subdomain of the seller's domain
    # and hand a lookalike host a verified badge.
    assert on_seller_domain("evilgaiaherbs.com", "gaiaherbs.com") is False


def test_every_image_in_the_recorded_corpus_is_off_the_sellers_own_domain() -> None:
    """The finding that decides whether the verified-media rule can ever fire.

    All 18,165 recorded image records live on ``cdn.shopify.com``. Under a literal reading
    of "sits on the seller's registered domain" **not one real image would score**, so
    whoever implements scoring has to rule on whether a merchant's platform CDN counts.
    Nothing in ingest makes that call; it records the fact so the call is an informed one.
    """
    corpus = RecordedCorpus.load()
    hosts: dict[str, int] = {}
    total = 0
    for store in corpus.stores:
        for page in store.pages:
            for product in json.loads(page.body)["products"]:
                for image in product.get("images") or []:
                    from ingest.adapters.mapping import safe_host

                    hosts[safe_host(image.get("src"))] = (
                        hosts.get(safe_host(image.get("src")), 0) + 1
                    )
                    total += 1
    assert total == 18165
    assert set(hosts) == {"cdn.shopify.com"}
    assert all(not on_seller_domain("cdn.shopify.com", store.host) for store in corpus.stores), (
        "no seller in this corpus serves its own images"
    )


def test_the_catalogue_hash_changes_when_the_seller_edits_the_record_and_not_when_it_reorders() -> (
    None
):
    """Leg 3's anchor: what a verifier's byte pin is taken against, and when it expires.

    The digest covers what the seller says about the *file* — URL, dimensions, its own
    updated-at — and nothing about where the image sits in the gallery. Reordering a gallery
    changes no image, so it must not expire a pin; swapping the file behind an unchanged URL
    must.
    """
    base = ImageRecord(
        native_id="7",
        src="https://cdn.example/a.png?v=1",
        position=1,
        width=800,
        height=600,
        updated_at="2026-01-01T00:00:00Z",
    )
    from dataclasses import replace

    assert replace(base, position=4).catalogue_digest_parts == base.catalogue_digest_parts
    assert replace(base, alt="new caption").catalogue_digest_parts == base.catalogue_digest_parts
    assert replace(base, src="https://cdn.example/a.png?v=2").catalogue_digest_parts != (
        base.catalogue_digest_parts
    )
    assert replace(base, width=1200).catalogue_digest_parts != base.catalogue_digest_parts
    assert replace(base, updated_at="2026-06-01T00:00:00Z").catalogue_digest_parts != (
        base.catalogue_digest_parts
    )


def test_an_asset_id_survives_the_cache_buster_a_re_crawl_brings() -> None:
    """Identity is the store's own image id, so a gallery edit does not fork the node.

    Shopify re-stamps ``?v=<epoch>`` on every image edit. A URL-keyed node would mint a
    fresh ``MediaAsset`` on every re-crawl of a store that touched anything, and the old
    ones would linger — sourced, stale and indistinguishable from current.
    """
    first = media_asset_id_for("gaiaherbs.com", "prod_x", "47471239725192")
    second = media_asset_id_for("gaiaherbs.com", "prod_x", "47471239725192")
    assert first == second
    assert first != media_asset_id_for("toniiq.com", "prod_x", "47471239725192")
    assert first != media_asset_id_for("gaiaherbs.com", "prod_y", "47471239725192")

    # A store that numbers no images falls back to the URL, which still gives one node per
    # image rather than one node per product.
    unnumbered = {"id": 1, "images": [{"src": "https://cdn.example/a.png", "position": 1}]}
    images, _ = image_records(unnumbered)
    assert images[0].native_id == "https://cdn.example/a.png"


def test_media_is_a_material_fact_the_provenance_audit_covers() -> None:
    """A media node and its attachment are both observations, so both must be sourced.

    Registering the label and the edge is what puts them inside
    :func:`~ingest.graph.upsert.provenance_violations`. Left out, an unsourced ``HAS_MEDIA``
    would pass the audit silently — the exact carve-out
    :data:`~ingest.graph.model.VOCABULARY_LABELS` warns about.
    """
    assert "MediaAsset" in MATERIAL_FACT_LABELS
    assert ID_PROPERTY["MediaAsset"] == "asset_id"
    assert MATERIAL_FACT_EDGES["HAS_MEDIA"] == ("Product", "MediaAsset")


# =======================================================================================
# 3. It lands in the real graph, provenanced
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_the_gallery_lands_in_neo4j_with_provenance_and_the_fields_leg_two_needs(
    graph_schema_session: Any,
) -> None:
    """One real storefront's 595 image records, into a database that can refuse them."""
    store = RecordedCorpus.load(hosts=(MEDIA_HOST,)).by_host(MEDIA_HOST)
    adapter, snapshot = _crawl(store)
    apply_upserts(graph_schema_session, adapter.to_upserts(snapshot))

    row = graph_schema_session.run(
        "MATCH (p:Product)-[r:HAS_MEDIA]->(m:MediaAsset) "
        "RETURN count(m) AS assets, count(DISTINCT p) AS products, "
        "count(DISTINCT m.asset_id) AS distinct_assets"
    ).single()
    assert row["assets"] == row["distinct_assets"] == 595
    assert row["products"] == 111

    assert provenance_violations(graph_schema_session) == [], (
        "a media node and its attachment are both observations and both need a Source"
    )

    primary = graph_schema_session.run(
        "MATCH (p:Product)-[r:HAS_MEDIA]->(m:MediaAsset) WHERE m.position = 1 "
        "RETURN m.url AS url, m.host AS host, m.on_seller_domain AS on_domain, "
        "m.catalogue_hash AS hash, m.width AS width, r.position AS edge_position "
        "ORDER BY m.asset_id LIMIT 1"
    ).single()
    assert primary["url"].startswith("https://cdn.shopify.com/")
    assert primary["host"] == "cdn.shopify.com"
    assert primary["on_domain"] is False, "gaiaherbs.com does not serve its own images"
    assert primary["hash"].startswith("sha256:")
    assert primary["width"] > 0
    assert primary["edge_position"] == 1, "the primary is identifiable from the edge alone"


@pytest.mark.docker
@pytest.mark.graph
def test_re_crawling_a_gallery_converges_rather_than_accumulating(
    graph_schema_session: Any, graph_source: Any
) -> None:
    """MERGE on the asset id, graded against the real uniqueness constraint."""
    product = ProductRecord(
        product_id="prod_media_idem",
        canonical_name="Milk Thistle Extract",
        images=(
            ImageRecord(native_id="9", src="https://cdn.example/a.png", position=1, width=10),
            ImageRecord(native_id="8", src="https://cdn.example/b.png", position=2, width=10),
        ),
    )
    from ingest.adapters.base import CatalogSnapshot

    snapshot = CatalogSnapshot(
        store_id="store-media",
        base_url="https://store-media.example",
        observed_at="2026-01-01T00:00:00Z",
        adapter="signed_fetch",
        products=(product,),
        extractor_version="test@1",
    )
    ops = build_upserts(snapshot)
    assert sum(1 for op in ops if op.kind == "media") == 2

    apply_upserts(graph_schema_session, ops)
    first = graph_schema_session.run(
        "MATCH (:Product)-[r:HAS_MEDIA]->(m:MediaAsset) RETURN count(r) AS edges, count(m) AS nodes"
    ).single()
    apply_upserts(graph_schema_session, ops)
    second = graph_schema_session.run(
        "MATCH (:Product)-[r:HAS_MEDIA]->(m:MediaAsset) RETURN count(r) AS edges, count(m) AS nodes"
    ).single()

    assert (first["nodes"], first["edges"]) == (2, 2)
    assert (second["nodes"], second["edges"]) == (2, 2)
    assert provenance_violations(graph_schema_session) == []
