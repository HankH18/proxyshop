"""What the two storefront surfaces are each allowed to decide (T-245, T-249).

``signed_fetch`` builds one product record out of two things a store publishes independently:
``/products.json``, a machine endpoint, and the product page's ``application/ld+json``, which
is theme-authored. The documented precedence is that the machine endpoint wins on anything it
states and JSON-LD fills the gaps. Two defects sat in that merge and neither had a test:

**T-249.** ``coerce_price`` answers ``None`` both for "no price here" and for a price it
refused (negative, NaN, ±inf, boolean, unreadable). The consumer read the second as the first
and reached for the JSON-LD offer, so a store writing ``-5.00`` in ``products.json`` beside
``12.00`` in its page got 12.00 into the graph — the store choosing which of its own surfaces
prices the product by making the other unusable.

**T-245.** An entry naming no ``id``, ``product_id`` or ``handle`` still got a product id,
derived from hashing the empty key — the same id for every such entry from that store, so a
store's whole unidentified catalog merged onto one graph node.

The repro gates next door each assert one instance. These pin the whole rule in both
directions, and the direction that matters most is the one the gates cannot express: the
JSON-LD fallback must still WORK when there genuinely is a gap. A repair that simply stopped
falling through would pass T-249's gate and quietly drop every price the machine endpoint does
not carry.

Both stores are served over a real socket and crawled through the ordinary public entry point.
The shared ``StorefrontStub`` cannot express either case — it derives each product page's
JSON-LD from the same entry it serves in ``products.json``, so its two surfaces always agree.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from ingest.adapters import (
    CatalogRequest,
    FetchPolicy,
    SignedFetchAdapter,
    native_product_key,
    product_id_for,
)
from ingest.adapters.mapping import price_is_stated

from proxyshop_support.asgi_server import serve

LOOPBACK = FetchPolicy(allowed_ports=None, extra_allowed_networks=("127.0.0.0/8",))

HANDLE = "trail-runner-42"
SKU = "TR-42-9"


class _TwoSurfaceStore:
    """A storefront whose ``products.json`` and JSON-LD can be made to disagree.

    ``entry_price`` is spliced into the variant exactly as given, including the sentinel
    :data:`ABSENT`, which omits the key altogether — the difference between "the store said
    nothing" and "the store said something unusable" is the whole subject here, and it cannot
    be expressed by a fixture that always writes a value.
    """

    #: Sentinel: omit the ``price`` key from the variant entirely.
    ABSENT = object()

    def __init__(
        self,
        *,
        entry_price: Any = "129.95",
        page_price: str | None = "12.00",
        entries: list[dict[str, Any]] | None = None,
    ) -> None:
        self.entry_price = entry_price
        self.page_price = page_price
        self._entries = entries

    def _variant(self) -> dict[str, Any]:
        variant: dict[str, Any] = {
            "id": 44352913,
            "title": "US 9",
            "sku": SKU,
            "available": True,
        }
        if self.entry_price is not self.ABSENT:
            variant["price"] = self.entry_price
        return variant

    def entries(self) -> list[dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        return [
            {
                "id": 8123456,
                "title": "Trail Runner 42",
                "handle": HANDLE,
                "vendor": "Cascade",
                "variants": [self._variant()],
            }
        ]

    def _page(self) -> bytes:
        offers: list[dict[str, Any]] = []
        if self.page_price is not None:
            offers.append(
                {
                    "@type": "Offer",
                    "sku": SKU,
                    "price": self.page_price,
                    "priceCurrency": "USD",
                    "availability": "https://schema.org/InStock",
                }
            )
        ld = {
            "@context": "https://schema.org/",
            "@type": "Product",
            "name": "Trail Runner 42",
            "brand": {"@type": "Brand", "name": "Cascade"},
            "offers": offers,
        }
        return (
            '<!doctype html><html><head><script type="application/ld+json">'
            f"{json.dumps(ld)}</script></head><body></body></html>"
        ).encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        while True:
            message = await receive()
            if not message.get("more_body"):
                break

        path = scope["path"]
        if path == "/robots.txt":
            body, media = b"User-agent: *\nAllow: /\n", b"text/plain"
        elif path == "/products.json":
            body, media = json.dumps({"products": self.entries()}).encode(), b"application/json"
        elif path.startswith("/products/"):
            body, media = self._page(), b"text/html"
        else:
            body, media = b"not found", b"text/plain"

        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-type", media)]}
        )
        await send({"type": "http.response.body", "body": body})


def _crawl(store: _TwoSurfaceStore, *, pages: bool = True) -> Any:
    with serve(store) as base_url:
        return SignedFetchAdapter().fetch_catalog(
            CatalogRequest(
                store_id="store-1",
                base_url=base_url,
                policy=LOOPBACK,
                fetch_product_pages=pages,
            )
        )


def _only_variant(snapshot: Any) -> Any:
    assert snapshot.products, f"the crawl read nothing: {list(snapshot.warnings)}"
    return snapshot.products[0].variants[0]


# =========================================================================================
# T-249 — which surface may state the price
# =========================================================================================


@pytest.mark.parametrize(
    "value",
    ["-5.00", "NaN", "Infinity", "-Infinity", True, "free", "12.00 USD"],
    ids=["negative", "nan", "inf", "neg-inf", "boolean", "unreadable", "trailing-currency"],
)
def test_a_stated_but_refused_entry_price_never_promotes_the_pages_price(value: Any) -> None:
    """Every way a store can state an unusable price, and none of them may fall through.

    The gate asserts the negative case. These are the rest of what ``coerce_price`` refuses:
    if any one of them still fell through, a store would keep exactly one way to make its
    theme's number win over its machine endpoint's.
    """
    snapshot = _crawl(_TwoSurfaceStore(entry_price=value, page_price="12.00"))
    assert _only_variant(snapshot).price is None, (
        f"products.json stated {value!r}, which is refused, and the page's 12.00 took its place"
    )


@pytest.mark.parametrize(
    "value",
    [_TwoSurfaceStore.ABSENT, None, "", "   "],
    ids=["key-absent", "json-null", "empty-string", "whitespace"],
)
def test_the_pages_price_still_fills_a_genuine_gap(value: Any) -> None:
    """The direction T-249's gate cannot express, and the one a lazy repair would break.

    "JSON-LD fills the gaps" is the documented contract, not a bug. A fix that stopped falling
    through altogether would pass the gate and silently drop the price of every product whose
    ``products.json`` entry does not carry one — which is most of the reason the page is
    fetched at all.
    """
    snapshot = _crawl(_TwoSurfaceStore(entry_price=value, page_price="12.00"))
    assert _only_variant(snapshot).price == 12.0, (
        f"products.json stated nothing ({value!r}) and the page's 12.00 was not used"
    )


def test_a_free_item_is_a_stated_price_and_not_a_gap() -> None:
    """Zero is a price. Treating it as absence would let a theme overwrite a giveaway."""
    snapshot = _crawl(_TwoSurfaceStore(entry_price="0.00", page_price="12.00"))
    assert _only_variant(snapshot).price == 0.0


def test_the_machine_endpoints_price_still_wins_when_both_surfaces_state_one() -> None:
    """The ordinary case, asserted so the repair cannot have inverted the precedence."""
    snapshot = _crawl(_TwoSurfaceStore(entry_price="129.95", page_price="12.00"))
    assert _only_variant(snapshot).price == 129.95


def test_a_refused_price_costs_the_offer_rather_than_poisoning_the_graph() -> None:
    """``build_upserts`` emits an Offer only for a variant that has a price."""
    snapshot = _crawl(_TwoSurfaceStore(entry_price="-5.00", page_price="12.00"))
    kinds = [op.kind for op in SignedFetchAdapter().to_upserts(snapshot)]
    assert "product" in kinds, f"nothing was mapped at all: {kinds}"
    assert "offer" not in kinds, f"an Offer was written for a refused price: {kinds}"


def test_the_currency_and_availability_still_come_from_the_page() -> None:
    """Only the PRICE branch moved. The other two gap-fills must be untouched."""
    snapshot = _crawl(_TwoSurfaceStore(entry_price="129.95", page_price="12.00"))
    variant = _only_variant(snapshot)
    assert variant.currency == "USD"
    assert variant.availability == "in_stock"


@pytest.mark.parametrize(
    ("value", "stated"),
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("\t\n", False),
        (0, True),
        ("0.00", True),
        (False, True),
        ("-5.00", True),
        ("free", True),
        ("12,50", True),
        # CHANGED, and this is the justification the diff has to carry.
        #
        # These four rows asserted `True` when this file was written an hour ago. The
        # assertion was: "a store that puts a container in a price field has stated
        # something, so the JSON-LD gap-fill must not replace it."
        #
        # Would that assertion still be wrong if I reverted my change? YES — it was wrong on
        # the day it was written, and an adversarial verifier proved it through a real crawl:
        # `{"amount": "12.00", "currency_code": "USD"}` is the money-object shape the SIBLING
        # adapter reads correctly (`catalog_mcp._money`, catalog_mcp.py:774-787). Under the
        # old rule signed_fetch called it a refused statement, suppressed the fall-through,
        # and emitted NO `offer` op at all for a variant whose price is perfectly readable —
        # measured ops `['store', 'product', 'sells', 'variant']`, no offer. Before T-249 the
        # page's 12.00 filled that gap and the Offer existed. So the old row encoded a
        # regression I introduced as if it were the contract.
        #
        # T-249's trade is about hostile SCALARS — "-5.00", NaN, true, unreadable text — where
        # the store has stated a number we refuse. A SHAPE `coerce_price` cannot read at all
        # is not that; it is a surface this adapter does not understand, which is exactly what
        # the gap-fill is for. The rows below assert the corrected contract precisely rather
        # than deleting the cases.
        ([], False),
        ({}, False),
        ({"amount": "12.00", "currency_code": "USD"}, False),
        (("12.00",), False),
    ],
)
def test_price_is_stated_separates_silence_from_a_bad_statement(value: Any, stated: bool) -> None:
    """The predicate's whole table, including the cases nothing else exercises.

    ``False`` reads as *stated*: ``coerce_price`` refuses booleans, and "the store wrote
    ``false``" is not "the store wrote nothing". A container reads as *not* stated — see the
    justification in the parametrize list, which is the one row of this table that changed
    after it was first written.
    """
    assert price_is_stated(value) is stated


def test_a_money_object_price_still_lets_the_page_fill_the_gap() -> None:
    """The regression the predicate's container rule exists to prevent, end to end.

    ``catalog_mcp`` reads ``{"amount": …, "currency_code": …}`` through its own unwrapper.
    ``signed_fetch`` cannot, so for it that field is a gap — and a gap is what the product
    page's JSON-LD is fetched to fill. Asserted through a real crawl rather than against the
    predicate, because the predicate is not the thing that would break.
    """
    snapshot = _crawl(
        _TwoSurfaceStore(
            entry_price={"amount": "12.00", "currency_code": "USD"}, page_price="12.00"
        )
    )
    assert _only_variant(snapshot).price == 12.0, (
        "a money-object price the sibling adapter reads fine was treated as a refused "
        "statement, and the page's price was suppressed"
    )
    assert "offer" in [op.kind for op in SignedFetchAdapter().to_upserts(snapshot)]


def test_a_european_decimal_comma_is_refused_rather_than_read_as_thousands() -> None:
    """``"12,50"`` is not 1250.00, and guessing which it is costs two orders of magnitude.

    ``coerce_price`` stripped commas unconditionally, so a store writing the European decimal
    form got a hundredfold overcharge into the graph as a price — and T-249 then made it
    unrecoverable, because a value that parses is never a gap for the page to fill. Grouped
    thousands separators are still read; anything else is refused, which costs the offer.
    """
    from ingest.adapters.mapping import coerce_price  # noqa: PLC0415

    assert coerce_price("12,50") is None
    assert coerce_price("1,234.56") == 1234.56
    assert coerce_price("1,234,567.89") == 1234567.89
    assert coerce_price("1_000") is None, "a Python literal is not a price a storefront writes"
    assert coerce_price("1e3") is None, "scientific notation in a price field is a misread"

    snapshot = _crawl(_TwoSurfaceStore(entry_price="12,50", page_price="12.00"))
    assert _only_variant(snapshot).price is None, (
        "an ambiguous comma price was coerced to a number instead of being refused"
    )


# =========================================================================================
# T-245 — an entry that names itself, or is skipped
# =========================================================================================


def test_entries_naming_no_identifier_are_skipped_with_a_warning() -> None:
    """The same disposition ``catalog_mcp`` already gives them, so the two adapters agree."""
    anonymous = [
        {"title": "Trail Runner 42", "variants": [{"sku": "A-1", "price": "129.95"}]},
        {"title": "Merino Hiking Sock", "variants": [{"sku": "B-1", "price": "18.00"}]},
    ]
    assert [native_product_key(e) for e in anonymous] == ["", ""], (
        "positive control: these are the entries that name no identifier"
    )

    snapshot = _crawl(_TwoSurfaceStore(entries=anonymous), pages=False)

    assert snapshot.products == (), f"an unidentifiable entry became a product: {snapshot.products}"
    skipped = [w for w in snapshot.warnings if "carries no id or handle" in w]
    assert len(skipped) == 2, f"the skips were silent or miscounted: {snapshot.warnings}"


def test_a_store_mixing_named_and_unnamed_entries_keeps_the_named_ones() -> None:
    """A skip is per entry. One malformed record must not cost a store its whole catalog."""
    mixed = [
        {"title": "Nameless", "variants": [{"sku": "A-1", "price": "1.00"}]},
        {"id": 8123456, "title": "Trail Runner 42", "handle": HANDLE, "variants": []},
        {"title": "Also nameless", "variants": [{"sku": "B-1", "price": "2.00"}]},
    ]
    snapshot = _crawl(_TwoSurfaceStore(entries=mixed), pages=False)

    assert [p.canonical_name for p in snapshot.products] == ["Trail Runner 42"]
    assert len([w for w in snapshot.warnings if "carries no id or handle" in w]) == 2


@pytest.mark.parametrize(
    "entry",
    [
        {"id": 8123456},
        {"product_id": "gid://shopify/Product/8123456"},
        {"handle": HANDLE},
    ],
    ids=["id", "product_id", "handle"],
)
def test_every_field_a_store_may_publish_its_identity_under_still_mints_an_id(
    entry: dict[str, Any],
) -> None:
    """The skip must be narrow: only entries naming NONE of the three."""
    assert product_id_for("store-1", entry).startswith("prod_")


def test_the_shared_identity_rule_refuses_to_hash_the_empty_key() -> None:
    """Defence in depth: a third adapter cannot reintroduce the collision by forgetting.

    Both adapters skip such an entry before reaching here, so this raise is unreachable from
    either of them today. That is the point — it is what makes the collision impossible rather
    than merely absent.
    """
    with pytest.raises(ValueError, match="store's own identifier"):
        product_id_for("store-1", {"title": "Nameless"})


def test_two_unnamed_entries_would_otherwise_have_collided() -> None:
    """Arming control for the whole T-245 half: the collision was real, not hypothetical.

    Reproduces the pre-fix identity derivation directly, so the tests above are known to be
    guarding against a live defect rather than a condition that never arose.
    """
    from ingest.adapters.mapping import stable_id  # noqa: PLC0415

    one = f"prod_{stable_id('store-1', native_product_key({'title': 'Trail Runner 42'}))}"
    two = f"prod_{stable_id('store-1', native_product_key({'title': 'Merino Sock'}))}"
    assert one == two, "the pre-fix derivation no longer collides; these tests guard nothing"
