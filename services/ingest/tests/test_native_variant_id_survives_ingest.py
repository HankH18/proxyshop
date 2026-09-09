"""The store's OWN variant id has to survive the crawl, because the cart permalink needs it.

Run::

    PROXYSHOP_WORKER=1 ./.venv/bin/python -m pytest \
        services/ingest/tests/test_native_variant_id_survives_ingest.py -q

Why this file exists
--------------------
``/cart/{variant}:{qty}`` is Shopify's own cart URL and the ``{variant}`` in it is the
storefront's numeric variant id — ``43866134282275``, not a key this platform invented. Both
adapters used to read that number, fold it into :func:`~ingest.adapters.mapping.variant_id_for`
and drop it: the graph kept ``var_<32 hex>`` and no record, node or projection carried the
number the cart needs. Measured before this change, over
``fixtures/real-catalogs-demo/stores/*.products.jsonl.gz``, 28,134 of 28,134 variant records
published one and 0 of them reached the graph.

Two properties, and both matter
-------------------------------
1. The id **travels**. It is on the record, on the node, and in the node's written properties.
2. The id is **the id**, never a stand-in. ``native_variant`` — the hash input both adapters
   already compute — falls back to the SKU and then to the product TITLE, because the hash key
   may not be empty. Plumbing *that* value would build carts on SKUs and on titles. Measured on
   the same corpus, the SKU equals the native id 0 times out of 28,134 and is all-digits 5,258
   times, so a SKU-built permalink would look valid and name the wrong thing.

The graph key deliberately does **not** move. ``Offer.offer_id`` is derived from
``variant_id``, the uniqueness constraint on it is global rather than store-scoped, and
nemoequipment.com publishes one native id under two different products 108 times — which the
hash keeps apart and the raw id would silently merge.
"""

from __future__ import annotations

from typing import Any

from ingest.adapters import CatalogSnapshot, ProductRecord, VariantRecord
from ingest.adapters.mapping import build_upserts, variant_id_for


def _snapshot(variants: tuple[VariantRecord, ...]) -> CatalogSnapshot:
    return CatalogSnapshot(
        adapter="probe",
        store_id="store-1",
        base_url="https://shop.example.com",
        observed_at="2026-01-01T00:00:00Z",
        extractor_version="probe/1",
        products=(
            ProductRecord(
                product_id="prod-1",
                canonical_name="Walnut Coffee Table",
                brand="Floyd",
                source_url="https://shop.example.com/products/walnut",
                content_hash="hash-1",
                changed=True,
                variants=variants,
            ),
        ),
    )


def test_the_variant_op_carries_the_storefronts_own_variant_id() -> None:
    """``build_upserts`` must put the native id on the ``Variant`` node it emits.

    The assertion is on ``as_properties()``, not on the dataclass attribute, because
    ``upsert_variant`` writes that mapping wholesale — a field the model holds and the
    mapping omits reaches the graph as nothing at all.
    """
    native = "43866134282275"
    ops = build_upserts(
        _snapshot(
            (
                VariantRecord(
                    variant_id=variant_id_for("store-1", "8722191712429", native),
                    native_variant_id=native,
                    seller_sku="FLOYD-WAL-01",
                    name="Walnut / 48in",
                    price=549.0,
                ),
            )
        )
    )
    variants = [op for op in ops if op.kind == "variant"]
    assert len(variants) == 1, [op.kind for op in ops]
    written: dict[str, Any] = variants[0].node.as_properties()
    assert written.get("native_variant_id") == native, written
    # The graph key is unchanged: it is what `offer_id` is derived from and what the global
    # uniqueness constraint is on.
    assert written["variant_id"].startswith("var_"), written


def test_a_variant_the_storefront_did_not_number_carries_no_stand_in() -> None:
    """Absent means absent. A SKU or a title in this field would build a cart on it.

    ``variant_id`` still has to exist — the graph cannot key a node on nothing — so this is
    exactly the case where the hash input and the native id must disagree.
    """
    ops = build_upserts(
        _snapshot(
            (
                VariantRecord(
                    variant_id=variant_id_for("store-1", "8722191712429", "FLOYD-WAL-01"),
                    native_variant_id="",
                    seller_sku="FLOYD-WAL-01",
                    name="Walnut / 48in",
                    price=549.0,
                ),
            )
        )
    )
    written = [op for op in ops if op.kind == "variant"][0].node.as_properties()
    assert written.get("native_variant_id") == "", written
    assert written["seller_sku"] == "FLOYD-WAL-01", written
