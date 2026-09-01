"""The ``orders`` query subset: pagination, filtering, and the two naming conventions.

Nothing in this repo specifies which fields the "orders query subset" contains — the ticket
says only that phrase — so the subset is this stub's design, chosen to mirror the webhook
payload field for field. What is *not* a free choice is the semantics: a consumer that pages
correctly here must page correctly against Shopify, so ``after`` is exclusive and
``hasNextPage`` means what Shopify means by it. An off-by-one in a cursor is a
double-counted order, which in this system is a double-counted conversion.
"""

from __future__ import annotations

from typing import Any

from shopify_stub.testing import SEED_VARIANT, StubClient

VARIANT_ID = int(SEED_VARIANT["variant_id"])


def _nodes(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [edge["node"] for edge in body["data"]["orders"]["edges"]]


async def test_an_empty_store_returns_an_empty_connection(stub: StubClient) -> None:
    """Empty is a valid connection, not a null and not an error."""
    body = (await stub.orders(first=10)).json()
    connection = body["data"]["orders"]
    assert connection["edges"] == []
    assert connection["pageInfo"]["hasNextPage"] is False
    assert connection["pageInfo"]["startCursor"] is None
    assert connection["pageInfo"]["endCursor"] is None


async def test_orders_carry_the_camel_case_graphql_shape(stub: StubClient) -> None:
    """GraphQL is camelCase with GIDs, MoneyBags and SCREAMING_SNAKE statuses.

    The webhook for the same order is snake_case with integers and lowercase statuses.
    Keeping the two apart is the point: a consumer that normalises them without noticing
    ships code that works on whichever surface it happened to test.
    """
    await stub.create_code("PSX-GQLSHAPE", percentage=0.10)
    result = await stub.buy(VARIANT_ID, code="PSX-GQLSHAPE")

    (node,) = _nodes((await stub.orders()).json())
    assert node["id"] == f"gid://shopify/Order/{result['order_id']}"
    assert node["legacyResourceId"] == str(result["order_id"])
    assert node["displayFinancialStatus"] == "PAID"
    assert node["displayFulfillmentStatus"] == "UNFULFILLED"
    assert node["totalPriceSet"]["shopMoney"] == {"amount": "90.00", "currencyCode": "USD"}
    assert node["discountCodes"] == ["PSX-GQLSHAPE"]
    assert node["checkoutToken"] == result["checkout_token"]


async def test_the_client_id_join_key_survives_into_the_orders_query(
    stub: StubClient,
) -> None:
    """The pixel's client id is reachable from the order without touching the webhook.

    Shopify does not carry a web-pixel client id onto an order by itself; the stub
    propagates it as an order attribute the way a real app would. Without it the four
    pinned join keys are not all reachable from the two payloads.
    """
    result = await stub.buy(VARIANT_ID)
    (node,) = _nodes((await stub.orders()).json())
    assert {"key": "proxyshop_client_id", "value": result["client_id"]} in node["customAttributes"]


async def test_statuses_follow_fulfilment_and_refund(stub: StubClient) -> None:
    order = await stub.buy(VARIANT_ID)
    await stub.fulfil(order["order_id"])
    (node,) = _nodes((await stub.orders()).json())
    assert node["displayFulfillmentStatus"] == "FULFILLED"

    await stub.refund(order["order_id"], amount="40.00")
    (node,) = _nodes((await stub.orders()).json())
    assert node["displayFinancialStatus"] == "PARTIALLY_REFUNDED"
    assert node["totalRefundedSet"]["shopMoney"]["amount"] == "40.00"

    await stub.refund(order["order_id"])
    (node,) = _nodes((await stub.orders()).json())
    assert node["displayFinancialStatus"] == "REFUNDED"


async def test_after_is_exclusive_and_pages_cover_the_set_exactly_once(
    stub: StubClient,
) -> None:
    """Five orders, pages of two. Every order appears once, in order, with no repeats.

    The repeat check is the reason this test exists: a cursor treated as *inclusive* returns
    the boundary order on both pages, which downstream is a conversion counted twice.
    """
    for _ in range(5):
        await stub.buy(VARIANT_ID)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        connection = (await stub.orders(first=2, after=cursor)).json()["data"]["orders"]
        pages += 1
        seen.extend(edge["node"]["id"] for edge in connection["edges"])
        if not connection["pageInfo"]["hasNextPage"]:
            break
        cursor = connection["pageInfo"]["endCursor"]
        assert pages < 10, "pagination did not terminate"

    assert len(seen) == 5
    assert len(set(seen)) == 5, "an inclusive cursor would repeat a boundary order"
    assert seen == sorted(seen, key=lambda gid: int(gid.rsplit("/", 1)[1]))
    assert pages == 3


async def test_has_previous_page_is_true_only_after_the_first_page(
    stub: StubClient,
) -> None:
    for _ in range(3):
        await stub.buy(VARIANT_ID)
    first = (await stub.orders(first=2)).json()["data"]["orders"]
    assert first["pageInfo"]["hasPreviousPage"] is False
    assert first["pageInfo"]["hasNextPage"] is True

    second = (await stub.orders(first=2, after=first["pageInfo"]["endCursor"])).json()["data"][
        "orders"
    ]
    assert second["pageInfo"]["hasPreviousPage"] is True
    assert second["pageInfo"]["hasNextPage"] is False


async def test_an_unknown_cursor_returns_an_empty_page(stub: StubClient) -> None:
    """Past the end of the set. Not an error, and not the whole set from the start."""
    await stub.buy(VARIANT_ID)
    body = (await stub.orders(first=10, after="bm90LWEtcmVhbC1jdXJzb3I=")).json()
    assert body["data"]["orders"]["edges"] == []


async def test_the_checkout_token_filter_selects_one_order(stub: StubClient) -> None:
    first = await stub.buy(VARIANT_ID)
    await stub.buy(VARIANT_ID)
    body = (await stub.orders(query=f"checkout_token:{first['checkout_token']}")).json()
    (node,) = _nodes(body)
    assert node["checkoutToken"] == first["checkout_token"]


async def test_the_discount_code_filter_selects_matching_orders(stub: StubClient) -> None:
    await stub.create_code("PSX-FILTER01", usage_limit=None)
    await stub.buy(VARIANT_ID, code="PSX-FILTER01")
    await stub.buy(VARIANT_ID)
    body = (await stub.orders(query="discount_code:PSX-FILTER01")).json()
    assert len(_nodes(body)) == 1


async def test_an_unsupported_filter_is_an_error_not_a_no_op(stub: StubClient) -> None:
    """A filter that silently matched everything is worse than no filter at all.

    Negative control for the search grammar: without this, ``query: "status:paid"`` would
    return every order and a consumer would believe it had filtered.
    """
    await stub.buy(VARIANT_ID)
    body = (await stub.orders(query="financial_status:paid")).json()
    assert "data" not in body
    assert "Unsupported search field" in body["errors"][0]["message"]

    bare = (await stub.orders(query="paid")).json()
    assert "Unsupported search term" in bare["errors"][0]["message"]


async def test_first_is_required_and_bounded(stub: StubClient) -> None:
    """Shopify requires a page size and caps it at 250."""
    missing = (await stub.graphql("query { orders { edges { cursor } } }")).json()
    assert "missing required arguments" in missing["errors"][0]["message"]

    too_many = (await stub.orders(first=251)).json()
    assert "between 1 and 250" in too_many["errors"][0]["message"]

    zero = (await stub.orders(first=0)).json()
    assert "between 1 and 250" in zero["errors"][0]["message"]


async def test_reverse_flips_the_order(stub: StubClient) -> None:
    for _ in range(3):
        await stub.buy(VARIANT_ID)
    forward = [node["id"] for node in _nodes((await stub.orders()).json())]
    body = (
        await stub.graphql("query { orders(first: 10, reverse: true) { edges { node { id } } } }")
    ).json()
    assert [node["id"] for node in _nodes(body)] == list(reversed(forward))
