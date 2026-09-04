"""T-023 — the catalog MCP adapter, replayed against recorded contracts.

Run::

    PROXYSHOP_WORKER=<n> ./.venv/bin/python -m pytest services/ingest/tests/test_catalog_mcp.py -q

Three habits shape this file:

**No live calls, and no way to make one by accident.** SPEC A1 requires this adapter to be
contract-tested against a recorded mock, so every test drives a
:class:`~ingest.adapters.catalog_mcp.RecordedMCPSession`, which raises on any call it has no
recording for. A test that stops asking for the calls it should ask for fails loudly instead
of quietly reading a shorter catalog.

**Every guard test names a blocked input AND an allowed one.** A URL guard that refuses
everything and a price coercion that returns ``None`` for everything both pass a suite that
only checks refusals, so each table pairs the attack with a control that must still get
through, asserted from the same call.

**"Matches signed_fetch semantics" is measured, not asserted.** T-023 acceptance 2 is graded
twice here: once on a snapshot both adapters map (they must produce byte-identical ops), and
once end to end against the live storefront stub, where the *same four-product catalog* served
as `products.json` on one side and as MCP results on the other has to land on the same graph
nodes, in the same order.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from ingest.adapters import (
    CatalogAdapter,
    CatalogRequest,
    CatalogSnapshot,
    CrawlBudget,
    FetchPolicy,
    ProductRecord,
    SignedFetchAdapter,
    VariantRecord,
)
from ingest.adapters.catalog_mcp import (
    LIST_PRODUCTS_TOOL,
    CatalogMCPAdapter,
    MCPError,
    MCPToolError,
    RecordedMCPSession,
    UnrecordedMCPCall,
    _satisfies_catalog_adapter,
    decode_tool_result,
    mcp_resource_url,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "mcp"
CASSETTE = FIXTURES / "catalog_list_products.json"
TEXT_ONLY_CASSETTE = FIXTURES / "catalog_text_content_only.json"
ERROR_CASSETTE = FIXTURES / "catalog_server_error.json"

SHOP = "harbour-house.example.com"
BASE_URL = f"https://{SHOP}"
STORE_ID = "store-harbour-house"
OBSERVED_AT = "2026-09-01T00:00:00Z"

#: Every product and variant the recorded cassette contains, so a read that silently drops a
#: page fails on the count rather than passing on the first page.
RECORDED_PRODUCTS = 4
RECORDED_VARIANTS = 7


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def _clock(stamp: str = OBSERVED_AT):
    return lambda: stamp


def _request(**kwargs: Any) -> CatalogRequest:
    defaults: dict[str, Any] = {"store_id": STORE_ID, "base_url": BASE_URL}
    defaults.update(kwargs)
    return CatalogRequest(**defaults)


def _adapter(cassette: Path = CASSETTE, **kwargs: Any) -> CatalogMCPAdapter:
    kwargs.setdefault("page_size", 2)
    kwargs.setdefault("clock", _clock())
    return CatalogMCPAdapter.from_cassette(cassette, **kwargs)


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    """A successful CallToolResult carrying ``payload``."""
    return {
        "isError": False,
        "structuredContent": payload,
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }


def _one_page(products: list[Any], *, shop: str = SHOP, limit: int = 100) -> RecordedMCPSession:
    """A session recording exactly one page of ``products`` and nothing else."""
    return RecordedMCPSession(
        [
            {
                "tool": LIST_PRODUCTS_TOOL,
                "arguments": {"shop_domain": shop, "limit": limit},
                "response": _ok({"products": products, "page_info": {"has_next_page": False}}),
            }
        ],
        name="inline",
    )


def _entry(**overrides: Any) -> dict[str, Any]:
    """A well-formed MCP catalog entry, before a test breaks one field of it."""
    entry: dict[str, Any] = {
        "id": "gid://shopify/Product/7100100",
        "handle": "altura-washed-single-origin",
        "title": "Altura Washed Single Origin",
        "vendor": "Harbour House Coffee",
        "product_type": "Whole Bean Coffee",
        "status": "ACTIVE",
        "online_store_url": f"{BASE_URL}/products/altura-washed-single-origin",
        "variants": [
            {
                "id": "gid://shopify/ProductVariant/41001",
                "sku": "ALT-250",
                "title": "250 g",
                "price": {"amount": "18.50", "currency_code": "USD"},
                "available": True,
            }
        ],
    }
    entry.update(overrides)
    return entry


def _snapshot(*, adapter: str, extractor_version: str) -> CatalogSnapshot:
    """One hand-built snapshot, so both adapters map identical input."""
    return CatalogSnapshot(
        store_id=STORE_ID,
        base_url=BASE_URL,
        observed_at=OBSERVED_AT,
        adapter=adapter,
        extractor_version=extractor_version,
        products=(
            ProductRecord(
                product_id="prod_one",
                canonical_name="Altura Washed Single Origin",
                brand="Harbour House Coffee",
                handle="altura",
                source_url=f"{BASE_URL}/products/altura",
                content_hash="sha256:deadbeef",
                changed=True,
                categories=("Whole Bean Coffee",),
                variants=(
                    VariantRecord(
                        variant_id="var_one",
                        seller_sku="ALT-250",
                        name="250 g",
                        price=18.5,
                        currency="USD",
                        availability="in_stock",
                    ),
                    VariantRecord(variant_id="var_two", seller_sku="ALT-1KG", name="1 kg"),
                ),
            ),
        ),
    )


def _shape(ops: list[Any]) -> list[tuple[str, Any, Any]]:
    """An op list reduced to what the graph will actually contain, provenance aside."""
    return [(op.kind, op.node, dict(op.context or {})) for op in ops]


# ---------------------------------------------------------------------------------------
# The recordings themselves
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [CASSETTE, TEXT_ONLY_CASSETTE, ERROR_CASSETTE])
def test_every_recorded_cassette_is_well_formed_and_self_consistent(path: Path):
    """A cassette whose two encodings of one payload disagree tests the wrong thing.

    A real MCP server sends the same JSON twice — once as ``structuredContent`` and once as a
    text ``content`` block — and the adapter prefers the first. If the recording lets them
    drift, the text-block path is never actually exercised by anything.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["interactions"], f"{path.name} records no interactions"
    for index, interaction in enumerate(document["interactions"]):
        assert interaction["tool"] == LIST_PRODUCTS_TOOL, f"{path.name}[{index}]"
        assert isinstance(interaction["arguments"], dict), f"{path.name}[{index}]"
        response = interaction["response"]
        structured = response.get("structuredContent")
        blocks = [b["text"] for b in response.get("content", []) if b.get("type") == "text"]
        assert blocks, f"{path.name}[{index}] has no text content block"
        if structured is not None:
            assert structured == json.loads("".join(blocks)), (
                f"{path.name}[{index}]: structuredContent and the text block disagree"
            )


def test_the_cassette_directory_holds_no_live_endpoint():
    """A1 / non-goal: nothing in the recorded contract may name a real Shopify host."""
    for path in sorted(FIXTURES.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert "myshopify.com" not in text, f"{path.name} names a live storefront"
        assert "shopify.com/admin" not in text, f"{path.name} names a live admin endpoint"


# ---------------------------------------------------------------------------------------
# Acceptance 1 — the adapter satisfies the CatalogAdapter interface
# ---------------------------------------------------------------------------------------


def test_the_adapter_satisfies_the_one_catalog_adapter_interface():
    adapter = CatalogMCPAdapter()
    assert isinstance(adapter, CatalogAdapter)
    assert callable(adapter.fetch_catalog) and callable(adapter.to_upserts)
    assert _satisfies_catalog_adapter()


def test_both_adapters_are_the_same_interface_and_not_the_same_class():
    assert CatalogMCPAdapter is not SignedFetchAdapter
    for adapter in (CatalogMCPAdapter(), SignedFetchAdapter()):
        assert isinstance(adapter, CatalogAdapter)


# ---------------------------------------------------------------------------------------
# Acceptance 2a — the recorded contract replays
# ---------------------------------------------------------------------------------------


def test_replaying_the_recorded_catalog_reads_every_page():
    adapter = _adapter()
    snapshot = adapter.fetch_catalog(_request())

    assert snapshot.warnings == (), snapshot.warnings
    assert snapshot.adapter == "catalog_mcp"
    assert snapshot.extractor_version == "catalog_mcp@1.0.0"
    assert len(snapshot.products) == RECORDED_PRODUCTS
    assert sum(len(p.variants) for p in snapshot.products) == RECORDED_VARIANTS
    # Both recordings were asked for: a read that stopped after page one leaves one unplayed.
    assert adapter.session.unplayed == ()
    assert snapshot.usage.pages == 2

    altura = snapshot.products[0]
    assert altura.canonical_name == "Altura Washed Single Origin"
    assert altura.brand == "Harbour House Coffee"
    assert altura.categories == ("Whole Bean Coffee",)
    assert altura.status == "active", "the MCP server states ACTIVE; the graph stores active"
    assert altura.source_url == f"{BASE_URL}/products/altura-washed-single-origin"
    assert [v.seller_sku for v in altura.variants] == ["ALT-250", "ALT-1KG"]
    assert [v.price for v in altura.variants] == [18.50, 62.90]
    assert {v.currency for v in altura.variants} == {"USD"}
    assert [v.availability for v in altura.variants] == ["in_stock", "in_stock"]

    espresso = snapshot.products[1]
    assert [v.availability for v in espresso.variants] == ["in_stock", "out_of_stock"], (
        "a variant the server marked unavailable must not read as in stock"
    )


def test_the_adapter_paginates_with_the_cursor_the_server_handed_it():
    adapter = _adapter()
    adapter.fetch_catalog(_request())

    calls = adapter.session.calls
    assert len(calls) == 2, [c.arguments for c in calls]
    assert "cursor" not in json.loads(calls[0].arguments), "page one must not invent a cursor"
    assert json.loads(calls[1].arguments)["cursor"], "page two must carry the server's cursor"
    assert all(c.tool == LIST_PRODUCTS_TOOL for c in calls)


def test_each_page_is_a_separately_hashed_provenance_resource():
    snapshot = _adapter().fetch_catalog(_request())

    urls = [r.url for r in snapshot.resources]
    assert urls[0] == mcp_resource_url(SHOP, LIST_PRODUCTS_TOOL)
    assert urls[1].startswith(f"{urls[0]}?cursor="), urls
    assert len(set(urls)) == 2, "two pages must not share one change-detection key"
    assert len({r.content_hash for r in snapshot.resources}) == 2
    for resource in snapshot.resources:
        assert resource.content_hash.startswith("sha256:")
        assert resource.snapshot_ref.startswith("snapshot://")
        assert resource.changed is True, "nothing was known, so everything is new"


def test_a_call_nobody_recorded_is_refused_rather_than_answered():
    """The teeth of the recorded contract: a miss is an error, never an empty catalog."""
    session = RecordedMCPSession.from_path(CASSETTE)
    with pytest.raises(UnrecordedMCPCall):
        session.call_tool(LIST_PRODUCTS_TOOL, {"shop_domain": SHOP, "limit": 999})
    # Positive control: the recorded call still replays.
    assert session.call_tool(LIST_PRODUCTS_TOOL, {"shop_domain": SHOP, "limit": 2})


def test_an_unrecorded_call_surfaces_as_a_warning_not_a_crash():
    snapshot = _adapter(page_size=99).fetch_catalog(_request())
    assert snapshot.products == ()
    assert any("no recording" in w for w in snapshot.warnings), snapshot.warnings


def test_a_cassette_that_answers_one_call_twice_is_refused_at_load():
    interaction = {
        "tool": LIST_PRODUCTS_TOOL,
        "arguments": {"shop_domain": SHOP, "limit": 1},
        "response": _ok({"products": []}),
    }
    with pytest.raises(MCPError, match="re-records"):
        RecordedMCPSession([interaction, dict(interaction)])


@pytest.mark.parametrize(
    "interaction",
    [
        pytest.param("not an object", id="not-an-object"),
        pytest.param({"arguments": {}, "response": {}}, id="no-tool"),
        pytest.param({"tool": LIST_PRODUCTS_TOOL, "arguments": {}}, id="no-response"),
        pytest.param(
            {"tool": LIST_PRODUCTS_TOOL, "arguments": [], "response": {}}, id="bad-arguments"
        ),
    ],
)
def test_a_malformed_cassette_is_refused_at_load(interaction: Any):
    with pytest.raises(MCPError):
        RecordedMCPSession([interaction])


# ---------------------------------------------------------------------------------------
# The MCP envelope
# ---------------------------------------------------------------------------------------


def test_an_error_result_is_never_parsed_into_a_catalog():
    """`isError` is checked first: the failure text rides in the same field the data does."""
    snapshot = _adapter(ERROR_CASSETTE).fetch_catalog(_request())
    assert snapshot.products == ()
    assert any("503" in w for w in snapshot.warnings), snapshot.warnings

    payload = {"products": [_entry()], "page_info": {"has_next_page": False}}
    with pytest.raises(MCPToolError, match="reported an error"):
        decode_tool_result(dict(_ok(payload), isError=True))
    # Positive control: the identical envelope without the error flag decodes to the payload.
    assert decode_tool_result(_ok(payload)) == payload


def test_a_text_only_result_is_read_when_there_is_no_structured_content():
    snapshot = _adapter(TEXT_ONLY_CASSETTE).fetch_catalog(_request())
    assert snapshot.warnings == (), snapshot.warnings
    assert [p.canonical_name for p in snapshot.products] == ["Harbour House Filter Papers"]


def test_structured_content_wins_over_a_disagreeing_text_block():
    """The two encodings should agree; when they do not, the structured one is the contract."""
    structured = {"products": [_entry(title="Structured")], "page_info": {}}
    lying = {"products": [_entry(title="Text")], "page_info": {}}
    result = {
        "isError": False,
        "structuredContent": structured,
        "content": [{"type": "text", "text": json.dumps(lying)}],
    }
    assert decode_tool_result(result) == structured


@pytest.mark.parametrize(
    "result",
    [
        pytest.param("a bare string", id="not-a-mapping"),
        pytest.param({}, id="empty"),
        pytest.param({"content": []}, id="no-blocks"),
        pytest.param({"content": [{"type": "image", "data": "..."}]}, id="no-text-block"),
        pytest.param({"content": [{"type": "text", "text": "not json"}]}, id="not-json"),
        pytest.param({"content": [{"type": "text", "text": "[1, 2]"}]}, id="json-but-not-object"),
        pytest.param({"structuredContent": None, "content": None}, id="nulls"),
    ],
)
def test_an_unintelligible_result_is_refused_rather_than_guessed_at(result: Any):
    with pytest.raises(MCPToolError):
        decode_tool_result(result, tool=LIST_PRODUCTS_TOOL)


def test_a_session_that_raises_anything_is_a_warning_not_a_crash():
    class Exploding:
        def call_tool(self, name: str, arguments: Any) -> Any:
            raise KeyboardInterruptSubstitute("the transport fell over")

    class KeyboardInterruptSubstitute(RuntimeError):
        pass

    snapshot = CatalogMCPAdapter(session=Exploding(), clock=_clock()).fetch_catalog(_request())
    assert snapshot.products == ()
    assert any("fell over" in w for w in snapshot.warnings), snapshot.warnings


# ---------------------------------------------------------------------------------------
# Acceptance 2b — the mapping matches signed_fetch
# ---------------------------------------------------------------------------------------


def test_both_adapters_map_one_snapshot_to_byte_identical_upserts():
    """T-023 acceptance 2, at the seam: one snapshot in, one op list out, whoever maps it."""
    mcp_ops = CatalogMCPAdapter().to_upserts(
        _snapshot(adapter="catalog_mcp", extractor_version="catalog_mcp@1.0.0")
    )
    fetch_ops = SignedFetchAdapter().to_upserts(
        _snapshot(adapter="catalog_mcp", extractor_version="catalog_mcp@1.0.0")
    )
    assert mcp_ops == fetch_ops
    assert [op.kind for op in mcp_ops] == [
        "store",
        "product",
        "sells",
        "category",
        "variant",
        "offer",
        "variant",
    ], "dependency order is load-bearing; a variant with no price emits no offer"


def test_each_adapter_stamps_its_own_provenance_on_the_snapshot_it_produced():
    """Sharing the mapping must not make MCP-sourced facts indistinguishable from scraped."""
    mcp_ops = CatalogMCPAdapter().to_upserts(
        _snapshot(adapter="catalog_mcp", extractor_version="catalog_mcp@1.0.0")
    )
    fetch_ops = SignedFetchAdapter().to_upserts(
        _snapshot(adapter="signed_fetch", extractor_version="signed_fetch@1.0.0")
    )
    assert {op.source.extractor_version for op in mcp_ops} == {"catalog_mcp@1.0.0"}
    assert {op.source.extractor_version for op in fetch_ops} == {"signed_fetch@1.0.0"}
    # D29: both are observations of the store's published catalog, not seller assertions.
    assert {op.source.source_class for op in mcp_ops + fetch_ops} == {"scraped"}


def test_the_same_catalog_through_either_adapter_lands_on_the_same_graph_nodes(storefront):
    """Acceptance 2, end to end: one store, two transports, one sub-graph.

    The storefront stub serves `products.json`; the inline cassette serves the *same* products
    the way the MCP surface would — GID identifiers, money objects. Identity is derived from
    the store's own IDs, so the two reads have to produce the same Store, Product, Variant,
    Offer and Category nodes in the same order. Provenance legitimately differs (the two
    adapters read different surfaces), so it is compared separately below.
    """
    from services.ingest.tests._fixtures_storefront import DEFAULT_PRODUCTS

    base_url, _stub = storefront
    mcp_products = [
        {
            "id": f"gid://shopify/Product/{p['id']}",
            "handle": p["handle"],
            "title": p["title"],
            "vendor": p["vendor"],
            "product_type": p["product_type"],
            "status": "ACTIVE",
            "variants": [
                {
                    "id": f"gid://shopify/ProductVariant/{v['id']}",
                    "sku": v["sku"],
                    "title": v["title"],
                    "price": {"amount": v["price"], "currency_code": "USD"},
                    "available": v["available"],
                }
                for v in p["variants"]
            ],
        }
        for p in DEFAULT_PRODUCTS
    ]

    request = _request(
        base_url=base_url,
        fetch_product_pages=False,
        budget=CrawlBudget(max_seconds=30.0),
        # The stub binds a loopback port, which the production SSRF posture refuses outright.
        policy=FetchPolicy(allowed_ports=None, extra_allowed_networks=("127.0.0.0/8",)),
    )
    fetched = SignedFetchAdapter(clock=_clock()).fetch_catalog(request)
    shop = base_url.split("//", 1)[1].split(":", 1)[0]
    mcp_adapter = CatalogMCPAdapter(session=_one_page(mcp_products, shop=shop), clock=_clock())
    mapped = mcp_adapter.fetch_catalog(request)

    assert fetched.warnings == (), fetched.warnings
    assert mapped.warnings == (), mapped.warnings
    assert len(fetched.products) == len(mapped.products) == len(DEFAULT_PRODUCTS)

    fetched_ops = SignedFetchAdapter().to_upserts(fetched)
    mapped_ops = mcp_adapter.to_upserts(mapped)
    assert _shape(mapped_ops) == _shape(fetched_ops), (
        "the same catalog read through either adapter must write the same graph"
    )
    # And the provenance still says which adapter saw it.
    assert {op.source.extractor_version for op in mapped_ops} == {"catalog_mcp@1.0.0"}
    assert {op.source.extractor_version for op in fetched_ops} == {"signed_fetch@1.0.0"}


def test_unchanged_content_produces_zero_upserts():
    """The change-detection contract, round-tripped through `hash_index`."""
    first = _adapter().fetch_catalog(_request())
    ops = _adapter().to_upserts(first)
    assert ops, "the first read has to do work, or the second read proves nothing"

    second = _adapter(clock=_clock("2026-09-02T00:00:00Z")).fetch_catalog(
        _request(known_hashes=first.hash_index)
    )
    assert second.changed_products == ()
    assert all(not r.changed for r in second.resources)
    assert _adapter().to_upserts(second) == []

    # Positive control: forget one product's hash and exactly that product comes back.
    partial = dict(first.hash_index)
    del partial[f"product:{first.products[0].product_id}"]
    third = _adapter().fetch_catalog(_request(known_hashes=partial))
    assert [p.product_id for p in third.changed_products] == [first.products[0].product_id]


def test_the_read_is_deterministic():
    first = _adapter().fetch_catalog(_request())
    second = _adapter().fetch_catalog(_request())
    assert first.products == second.products
    assert [r.content_hash for r in first.resources] == [r.content_hash for r in second.resources]
    assert _adapter().to_upserts(first) == _adapter().to_upserts(second)


# ---------------------------------------------------------------------------------------
# Untrusted merchant input (C10)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("18.50", 18.50, id="control-string"),
        pytest.param(18.5, 18.5, id="control-number"),
        pytest.param("1,299.00", 1299.00, id="control-thousands"),
        pytest.param("NaN", None, id="nan-string"),
        pytest.param(float("nan"), None, id="nan-float"),
        pytest.param("Infinity", None, id="infinity"),
        pytest.param(float("-inf"), None, id="negative-infinity"),
        pytest.param("-5.00", None, id="negative"),
        pytest.param(True, None, id="boolean"),
        pytest.param("free", None, id="not-a-number"),
        pytest.param(None, None, id="absent"),
    ],
)
def test_an_untrusted_price_never_reaches_an_offer_unless_it_is_a_usable_number(
    raw: Any, expected: float | None
):
    """A NaN price compares false against everything and never raises — it must not land."""
    entry = _entry(variants=[{"id": "gid://shopify/ProductVariant/1", "sku": "X", "price": raw}])
    adapter = CatalogMCPAdapter(session=_one_page([entry]), clock=_clock())
    snapshot = adapter.fetch_catalog(_request())

    price = snapshot.products[0].variants[0].price
    if expected is None:
        assert price is None, f"{raw!r} became {price!r}"
        assert [op.kind for op in adapter.to_upserts(snapshot)].count("offer") == 0
    else:
        assert price == expected
        offers = [op for op in adapter.to_upserts(snapshot) if op.kind == "offer"]
        assert len(offers) == 1
        assert math.isfinite(offers[0].node.price) and offers[0].node.price > 0


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        pytest.param(f"{BASE_URL}/products/altura", True, id="control-same-host"),
        pytest.param(f"https://cdn.{SHOP}/products/altura", True, id="control-subdomain"),
        pytest.param("javascript:alert(1)", False, id="javascript"),
        pytest.param("file:///etc/passwd", False, id="file"),
        pytest.param("http://169.254.169.254/latest/meta-data/", False, id="link-local"),
        pytest.param("https://evil.example.net/products/altura", False, id="other-host"),
        pytest.param(f"https://{SHOP}.evil.example.net/x", False, id="suffix-lookalike"),
        pytest.param(f"https://user:pw@{SHOP}/x", False, id="embedded-credentials"),
        pytest.param("https://" + SHOP + "/" + "a" * 4000, False, id="over-length"),
        pytest.param("", False, id="empty"),
    ],
)
def test_a_merchant_supplied_product_url_only_becomes_provenance_when_it_is_this_store(
    url: str, kept: bool
):
    """`Source.url` is written to the graph and shown to buyers; the merchant chooses it."""
    adapter = CatalogMCPAdapter(session=_one_page([_entry(online_store_url=url)]), clock=_clock())
    snapshot = adapter.fetch_catalog(_request())
    source_url = snapshot.products[0].source_url

    if kept:
        assert source_url == url
        assert snapshot.warnings == (), snapshot.warnings
    else:
        assert source_url == mcp_resource_url(SHOP, LIST_PRODUCTS_TOOL), source_url
        assert source_url.startswith("mcp://")
    assert [op.source.url for op in adapter.to_upserts(snapshot) if op.kind == "product"] == [
        source_url
    ]


def test_a_duplicate_catalog_entry_keeps_the_first_and_says_so():
    entry = _entry()
    twin = _entry(title="A second listing of the same product")
    adapter = CatalogMCPAdapter(session=_one_page([entry, twin]), clock=_clock())
    snapshot = adapter.fetch_catalog(_request())

    assert [p.canonical_name for p in snapshot.products] == ["Altura Washed Single Origin"]
    assert any("duplicate" in w for w in snapshot.warnings), snapshot.warnings


def test_a_duplicate_variant_keeps_the_first_and_says_so():
    variant = {
        "id": "gid://shopify/ProductVariant/41001",
        "sku": "ALT-250",
        "price": {"amount": "18.50"},
    }
    adapter = CatalogMCPAdapter(
        session=_one_page([_entry(variants=[variant, dict(variant, sku="OTHER")])]),
        clock=_clock(),
    )
    snapshot = adapter.fetch_catalog(_request())

    assert [v.seller_sku for v in snapshot.products[0].variants] == ["ALT-250"]
    assert any("duplicate variant" in w for w in snapshot.warnings), snapshot.warnings


@pytest.mark.parametrize(
    "products",
    [
        pytest.param(["not an object"], id="entry-not-an-object"),
        pytest.param([{}], id="entry-with-no-identity"),
        pytest.param([{"id": True, "title": "x"}], id="boolean-identity"),
        pytest.param([_entry(variants="everything")], id="variants-not-a-list"),
        pytest.param([_entry(variants=["nope"])], id="variant-not-an-object"),
        pytest.param([_entry(variants=[{"sku": ""}])], id="variant-with-no-identity"),
    ],
)
def test_a_malformed_catalog_entry_is_skipped_with_a_warning(products: list[Any]):
    """One bad entry must cost that entry, not the crawl."""
    adapter = CatalogMCPAdapter(session=_one_page(products), clock=_clock())
    snapshot = adapter.fetch_catalog(_request())
    assert snapshot.warnings, "a skipped entry has to be reported"
    assert adapter.to_upserts(snapshot) is not None  # it maps rather than raising


def test_a_result_carrying_no_products_list_is_reported_not_treated_as_an_empty_store():
    session = RecordedMCPSession(
        [
            {
                "tool": LIST_PRODUCTS_TOOL,
                "arguments": {"shop_domain": SHOP, "limit": 100},
                "response": _ok({"page_info": {"has_next_page": False}}),
            }
        ]
    )
    snapshot = CatalogMCPAdapter(session=session, clock=_clock()).fetch_catalog(_request())
    assert snapshot.products == ()
    assert any("`products` list" in w for w in snapshot.warnings), snapshot.warnings


def test_unicode_titles_and_skus_survive_the_read():
    entry = _entry(
        title="Café Solo — Ethiopía تست",
        variants=[
            {
                "id": "gid://shopify/ProductVariant/9",
                "sku": "CAFÉ-250",
                "title": "250 g",
                "price": {"amount": "18.50"},
            }
        ],
    )
    adapter = CatalogMCPAdapter(session=_one_page([entry]), clock=_clock())
    snapshot = adapter.fetch_catalog(_request())
    assert snapshot.products[0].canonical_name == "Café Solo — Ethiopía تست"
    assert snapshot.products[0].variants[0].seller_sku == "CAFÉ-250"
    assert snapshot.products[0].content_hash.startswith("sha256:")


# ---------------------------------------------------------------------------------------
# Bounds: budgets, cursors, dev stores
# ---------------------------------------------------------------------------------------


def test_a_server_that_repeats_its_cursor_does_not_loop_forever():
    """A hostile or buggy server must not be read until the page budget runs out."""
    page = {"products": [_entry()], "page_info": {"has_next_page": True, "end_cursor": "same"}}
    session = RecordedMCPSession(
        [
            {
                "tool": LIST_PRODUCTS_TOOL,
                "arguments": {"shop_domain": SHOP, "limit": 100},
                "response": _ok(page),
            },
            {
                "tool": LIST_PRODUCTS_TOOL,
                "arguments": {"shop_domain": SHOP, "limit": 100, "cursor": "same"},
                "response": _ok(page),
            },
        ]
    )
    adapter = CatalogMCPAdapter(session=session, clock=_clock())
    snapshot = adapter.fetch_catalog(_request())

    assert len(adapter.session.calls) == 2, "one repeat is followed; the second is refused"
    assert any("repeated cursor" in w for w in snapshot.warnings), snapshot.warnings


def test_a_next_page_with_no_cursor_stops_the_read():
    session = RecordedMCPSession(
        [
            {
                "tool": LIST_PRODUCTS_TOOL,
                "arguments": {"shop_domain": SHOP, "limit": 100},
                "response": _ok(
                    {"products": [_entry()], "page_info": {"has_next_page": True}},
                ),
            }
        ]
    )
    snapshot = CatalogMCPAdapter(session=session, clock=_clock()).fetch_catalog(_request())
    assert len(snapshot.products) == 1
    assert any("names no cursor" in w for w in snapshot.warnings), snapshot.warnings


def test_the_page_budget_bounds_the_read():
    adapter = _adapter()
    snapshot = adapter.fetch_catalog(_request(budget=CrawlBudget(max_pages=1)))

    assert len(adapter.session.calls) == 1
    assert len(snapshot.products) == 2, "page one's products are kept"
    assert any("budget" in w for w in snapshot.warnings), snapshot.warnings
    assert adapter.session.unplayed, "the second recording was never asked for"


def test_max_products_bounds_the_read():
    adapter = CatalogMCPAdapter.from_cassette(CASSETTE, page_size=2, clock=_clock())
    snapshot = adapter.fetch_catalog(_request(max_products=2))
    assert len(snapshot.products) == 2
    assert len(adapter.session.calls) == 1


@pytest.mark.parametrize("max_products", [0, -1])
def test_a_non_positive_max_products_reads_nothing_at_all(max_products: int):
    adapter = _adapter()
    snapshot = adapter.fetch_catalog(_request(max_products=max_products))
    assert snapshot.products == ()
    assert adapter.session.calls == (), "no budget for products is no reason to call the server"
    assert snapshot.warnings


def test_a_password_protected_dev_store_is_reported_rather_than_returned_empty():
    """A1: this adapter cannot see a dev store, and must not look like it saw an empty one."""
    adapter = _adapter()
    snapshot = adapter.fetch_catalog(_request(storefront_password="open-sesame"))
    assert any("A1" in w and "signed_fetch" in w for w in snapshot.warnings), snapshot.warnings


def test_an_adapter_with_no_session_reads_nothing_and_says_why():
    snapshot = CatalogMCPAdapter(clock=_clock()).fetch_catalog(_request())
    assert snapshot.products == ()
    assert any("no MCP session" in w for w in snapshot.warnings), snapshot.warnings


def test_a_base_url_with_no_host_reads_nothing_and_says_why():
    snapshot = _adapter().fetch_catalog(_request(base_url="not-a-url"))
    assert snapshot.products == ()
    assert any("names no host" in w for w in snapshot.warnings), snapshot.warnings
