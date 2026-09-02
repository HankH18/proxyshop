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

import pytest
from shopify_stub.orders import cursor as cursor_for
from shopify_stub.orders import is_cursor
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


# ---------------------------------------------------------------------------------------
# A malformed cursor is an error, not a silent empty page
# ---------------------------------------------------------------------------------------

#: The two ways a cursor actually arrives broken in the field.
MALFORMED_CURSORS = {
    # The single most common Relay paging mistake: node.id where edge.cursor belongs.
    "an order gid": "gid://shopify/Order/5500000000001",
    "a legacy id": "5500000000001",
    "not base64 at all": "!!!not-base64!!!",
    # Padding lost to a URL round-trip or a hand-edited config.
    "truncated": "b3JkZXI6NTUwMDAwMDAwMDAwMQ",
    "empty string": "",
}


@pytest.mark.parametrize(("label", "bad"), sorted(MALFORMED_CURSORS.items()))
async def test_a_malformed_cursor_is_an_error_not_a_no_op(
    stub: StubClient, label: str, bad: str
) -> None:
    """The principle ``test_an_unsupported_filter_is_an_error_not_a_no_op`` already states.

    A malformed ``after`` answered ``hasNextPage: false, endCursor: null`` — which a consumer
    reads as *"no more results"*. The paging loop ends early, quietly, with rows missing and
    nothing logged, which is strictly worse than the unsupported filter this module already
    refuses: that one at least returns too much rather than too little. Real Shopify answers
    an ``INVALID`` error here.
    """
    await stub.buy(VARIANT_ID)
    body = (await stub.orders(first=10, after=bad)).json()
    assert "data" not in body, label
    assert "not a valid cursor" in body["errors"][0]["message"], label


async def test_a_well_formed_cursor_naming_no_row_is_still_an_empty_page(
    stub: StubClient,
) -> None:
    """The deliberate line between "malformed" and "past the end", pinned in both directions.

    A cursor is opaque by contract, so the stub validates that it *is* a cursor and not what
    is inside it. A syntactically valid cursor naming no current row is what paging against a
    shrinking collection looks like, and erroring on it would break correct consumers. This is
    the same behaviour ``test_an_unknown_cursor_returns_an_empty_page`` already asserts, kept
    honest against the new validation.
    """
    await stub.buy(VARIANT_ID)
    past_the_end = cursor_for("order:9999999999999")
    body = (await stub.orders(first=10, after=past_the_end)).json()
    assert body["data"]["orders"]["edges"] == []
    assert body["data"]["orders"]["pageInfo"]["hasNextPage"] is False


async def test_a_cursor_the_stub_itself_issued_always_survives_validation(
    stub: StubClient,
) -> None:
    """Negative control for the validator: it must not reject the stub's own cursors."""
    for _ in range(3):
        await stub.buy(VARIANT_ID)
    first_page = (await stub.orders(first=2)).json()["data"]["orders"]
    for edge in first_page["edges"]:
        assert is_cursor(edge["cursor"])
    second = (await stub.orders(first=2, after=first_page["pageInfo"]["endCursor"])).json()
    assert len(second["data"]["orders"]["edges"]) == 1


@pytest.mark.parametrize(
    ("label", "amount", "message"),
    [
        ("zero", "0.00", "refund amount must be positive"),
        ("negative", "-5.00", "refund amount must be positive"),
        ("more than the order", "1000.00", "exceeds refundable remainder"),
    ],
)
async def test_every_refusal_in_refund_orders_raises_clause(
    stub: StubClient, label: str, amount: str, message: str
) -> None:
    """`refund_order`'s Raises clause named one of its two refusals, and neither was tested.

    It documented only "the refund would exceed what remains refundable" while the code
    also raises `ValueError("refund amount must be positive")` for a zero or negative
    amount — so a caller reading the clause would have expected `amount=0` to record a
    zero refund. Both spellings are now in the docstring and both are pinned here, along
    with the 409 the route maps them to and the fact that a refused refund changes nothing.
    """
    result = await stub.buy(VARIANT_ID)
    order_id = result["order_id"]

    response = await stub.refund(order_id, amount=amount)
    assert response.status_code == 409, f"{label}: {response.text}"
    assert message in response.json()["errors"]

    # A refused refund is all-or-nothing: no refund recorded, status untouched.
    accepted = await stub.refund(order_id, amount="1.00")
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["financial_status"] == "partially_refunded"
