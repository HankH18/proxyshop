"""``discountCodeBasicCreate`` over HTTP: the response envelope, and the refusals.

Acceptance 1 asks for request/response parity with recorded real-API shapes; the recording
itself is checked in ``test_stub_recordings.py``. This file covers the *behaviour* around
that shape — what the mutation accepts, what it refuses, and the difference between a
``userErrors`` entry and a transport failure. Getting that distinction wrong is the failure
that hurts most: a consumer that treats a ``userErrors`` response as a success ships a code
that does not exist.
"""

from __future__ import annotations

from typing import Any

from shopify_stub.testing import DISCOUNT_MUTATION, StubClient


def _payload(response: Any) -> dict[str, Any]:
    body = response.json()
    assert "errors" not in body, body
    return body["data"]["discountCodeBasicCreate"]


async def test_creates_a_code_and_returns_the_documented_envelope(stub: StubClient) -> None:
    response = await stub.create_code("PSX-ABCDEFGH", percentage=0.15, offer_id="offer-1")
    assert response.status_code == 200
    body = response.json()

    payload = body["data"]["discountCodeBasicCreate"]
    assert payload["userErrors"] == []
    node = payload["codeDiscountNode"]
    assert node["id"].startswith("gid://shopify/DiscountCodeNode/")
    discount = node["codeDiscount"]
    assert discount["codes"]["nodes"] == [{"code": "PSX-ABCDEFGH"}]
    assert discount["usageLimit"] == 1
    assert discount["customerGets"]["value"]["percentage"] == 0.15
    assert discount["combinesWith"] == {
        "orderDiscounts": False,
        "productDiscounts": False,
        "shippingDiscounts": False,
    }
    # Shopify attaches a cost/throttle block to every Admin GraphQL response. A client that
    # paces itself on `throttleStatus` crashes when it is absent, so its presence is part of
    # the contract rather than decoration.
    throttle = body["extensions"]["cost"]["throttleStatus"]
    assert set(throttle) == {"maximumAvailable", "currentlyAvailable", "restoreRate"}


async def test_percentage_is_a_fraction_not_a_percentage_point(stub: StubClient) -> None:
    """``customerGets.value.percentage`` is documented as 0.00–1.00.

    A caller that sends ``10`` meaning "10%" would otherwise silently create a 1000%
    discount, which reads as a free order and a negative total downstream. The stub refuses
    with a ``userErrors`` entry rather than clamping, because clamping would hide the bug in
    the caller.
    """
    response = await stub.create_code("PSX-BADRANGE", percentage=10.0)
    payload = _payload(response)
    assert payload["codeDiscountNode"] is None
    (error,) = payload["userErrors"]
    assert error["code"] == "INVALID"
    assert error["field"] == [
        "basicCodeDiscount",
        "customerGets",
        "value",
        "percentage",
    ]
    assert "0.0" in error["message"] and "1.0" in error["message"]


async def test_a_duplicate_code_is_a_user_error_not_a_second_code(stub: StubClient) -> None:
    """Two codes with the same string would make a single-use redeemable double-usable."""
    first = await stub.create_code("PSX-DUPEDUPE")
    assert _payload(first)["userErrors"] == []

    second = await stub.create_code("PSX-DUPEDUPE")
    payload = _payload(second)
    assert payload["codeDiscountNode"] is None
    (error,) = payload["userErrors"]
    assert error["code"] == "TAKEN"
    assert error["field"] == ["basicCodeDiscount", "code"]
    # And the HTTP status is still 200: a userErrors response is a *successful* GraphQL
    # request. A consumer that only checks the status code sees a success here, which is
    # exactly why the payload check above matters.
    assert second.status_code == 200


async def test_user_errors_carry_all_four_documented_members(stub: StubClient) -> None:
    """``DiscountUserError`` is ``field``, ``message``, ``code`` and ``extraInfo``."""
    response = await stub.create_code("PSX-BADRANGE", percentage=5.0)
    (error,) = _payload(response)["userErrors"]
    assert set(error) == {"field", "message", "code", "extraInfo"}


async def test_end_before_start_is_refused(stub: StubClient) -> None:
    response = await stub.create_code(
        "PSX-BACKWARD",
        starts_at="2026-02-01T00:00:00Z",
        ends_at="2026-01-01T00:00:00Z",
    )
    (error,) = _payload(response)["userErrors"]
    assert error["field"] == ["basicCodeDiscount", "endsAt"]


async def test_a_blank_code_is_refused(stub: StubClient) -> None:
    response = await stub.create_code("")
    (error,) = _payload(response)["userErrors"]
    assert error["code"] == "BLANK"


async def test_zero_usage_limit_is_refused(stub: StubClient) -> None:
    """``usageLimit: 0`` would create a code that can never be redeemed."""
    response = await stub.create_code("PSX-ZEROUSES", usage_limit=0)
    (error,) = _payload(response)["userErrors"]
    assert error["field"] == ["basicCodeDiscount", "usageLimit"]


async def test_a_missing_access_token_is_a_401_with_a_string_errors_member(
    stub: StubClient,
) -> None:
    """Shopify's auth failure is a 401 whose ``errors`` is a **string**, not a list.

    This is the shape that breaks consumers in production: everything else they see has
    ``errors`` as a list of objects, so ``body["errors"][0]["message"]`` works everywhere
    until the token expires. The stub reproduces the asymmetry deliberately.
    """
    response = await stub.graphql(
        DISCOUNT_MUTATION, {"basicCodeDiscount": {"code": "PSX-NOAUTHXX"}}, token=None
    )
    assert response.status_code == 401
    assert isinstance(response.json()["errors"], str)


async def test_a_wrong_access_token_is_also_a_401(stub: StubClient) -> None:
    response = await stub.graphql(
        "query { orders(first: 1) { edges { cursor } } }", token="shpat_wrong"
    )
    assert response.status_code == 401


async def test_an_unimplemented_mutation_is_an_undefined_field_error(
    stub: StubClient,
) -> None:
    """A root field the stub does not implement must NOT look like a success.

    Negative control for the whole GraphQL layer: without it, a stub that answered
    ``{"data": {}}`` to anything would pass every other test in this file, and a consumer
    could ship a call to an operation nobody has ever exercised.
    """
    response = await stub.graphql(
        'mutation { productCreate(input: {title: "x"}) { product { id } } }'
    )
    assert response.status_code == 200
    body = response.json()
    assert "data" not in body
    (error,) = body["errors"]
    assert error["extensions"]["code"] == "undefinedField"
    assert "productCreate" in error["message"]
    assert "Mutation" in error["message"]


async def test_an_unimplemented_query_is_an_undefined_field_error(stub: StubClient) -> None:
    response = await stub.graphql("query { products(first: 1) { edges { cursor } } }")
    (error,) = response.json()["errors"]
    assert error["extensions"]["code"] == "undefinedField"
    assert error["extensions"]["typeName"] == "QueryRoot"


async def test_an_unparseable_document_is_a_graphql_parse_error(stub: StubClient) -> None:
    response = await stub.graphql("this is not graphql")
    assert response.status_code == 200
    assert "Parse error" in response.json()["errors"][0]["message"]


async def test_inline_arguments_work_as_well_as_variables(stub: StubClient) -> None:
    """Both spellings are legal GraphQL; a stub that only parses one constrains callers."""
    response = await stub.graphql(
        """
        mutation {
          discountCodeBasicCreate(basicCodeDiscount: {
            title: "Inline"
            code: "PSX-INLINE01"
            usageLimit: 1
            appliesOncePerCustomer: true
            combinesWith: { orderDiscounts: false, productDiscounts: false,
                            shippingDiscounts: false }
            customerGets: { value: { percentage: 0.2 }, items: { all: true } }
          }) {
            codeDiscountNode { id }
            userErrors { field message code extraInfo }
          }
        }
        """
    )
    payload = _payload(response)
    assert payload["userErrors"] == []
    assert payload["codeDiscountNode"]["id"].startswith("gid://shopify/DiscountCodeNode/")


async def test_an_aliased_root_field_answers_under_its_alias(stub: StubClient) -> None:
    """GraphQL aliases are legal and a caller may use one; the key must follow the alias."""
    response = await stub.graphql(
        """
        mutation Aliased($basicCodeDiscount: DiscountCodeBasicInput!) {
          minted: discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
            userErrors { message }
          }
        }
        """,
        {
            "basicCodeDiscount": {
                "code": "PSX-ALIAS001",
                "customerGets": {"value": {"percentage": 0.1}, "items": {"all": True}},
            }
        },
    )
    body = response.json()
    assert "minted" in body["data"]
    assert "discountCodeBasicCreate" not in body["data"]


async def test_the_code_is_stored_keyed_by_offer_id(stub: StubClient) -> None:
    """D22 stores codes keyed by ``offer_id``; the stub reads it from the ``offer:`` tag.

    Storage *by* offer id and derivation *from* offer id are different things, and only the
    second is forbidden. This asserts the first happened without the second.
    """
    await stub.create_code("PSX-KEYED001", offer_id="offer-77")
    await stub.create_code("PSX-KEYED002", offer_id="offer-78")

    index = (await stub.http.get("/_stub/codes")).json()
    assert index["by_offer"] == {"offer-77": "PSX-KEYED001", "offer-78": "PSX-KEYED002"}
    # Storage *by* offer id and derivation *from* offer id are different things, and only
    # the second is forbidden. The offer id must not be recoverable from the code.
    for offer_id, code in index["by_offer"].items():
        assert offer_id.removeprefix("offer-") not in code


async def test_a_code_created_without_an_offer_tag_is_not_indexed(stub: StubClient) -> None:
    """Negative control for the offer index: no tag, no entry — never a null key."""
    await stub.create_code("PSX-NOOFFER1")
    index = (await stub.http.get("/_stub/codes")).json()
    assert index["by_offer"] == {}
    assert "PSX-NOOFFER1" in index["codes"]


async def test_the_offer_index_agrees_with_the_code_table_for_a_lowercase_code(
    stub: StubClient,
) -> None:
    """``codes[by_offer[offer_id]]`` must resolve, whatever case the code was created in.

    ``state.codes`` is keyed by ``code.upper()``; the offer index used to store the code
    exactly as created. Shopify's ``discountCodeBasicCreate`` does not police case — it
    accepts ``psx-lower01`` and stores it as given — so the two structures disagreed
    outright, ``codes`` holding ``PSX-LOWER01`` while ``by_offer`` held ``psx-lower01``, and
    the obvious lookup raised ``KeyError``.
    """
    response = await stub.create_code("psx-lower01", offer_id="offer-lc")
    assert response.json()["data"]["discountCodeBasicCreate"]["userErrors"] == []

    index = (await stub.http.get("/_stub/codes")).json()
    assert index["by_offer"]["offer-lc"] == "PSX-LOWER01"
    assert index["codes"][index["by_offer"]["offer-lc"]]["code"] == "psx-lower01", (
        "the index key is normalised; the stored code keeps the case it was created with, "
        "exactly as Shopify does"
    )
    assert index["codes_by_offer"]["offer-lc"] == ["PSX-LOWER01"]


async def test_a_second_mint_for_one_offer_does_not_erase_the_first(stub: StubClient) -> None:
    """The offer index was last-write-wins, so a double mint hid a live redeemable code.

    Both codes stay in ``codes`` and both stay redeemable, so an index that remembers only
    the second one is the index disagreeing with the redemption table. A duplicate mint is
    deliberately *not* refused here: the stub mirrors Shopify, which has no per-offer index
    and therefore no rule to break. Whether one offer may hold two live codes is T-052's
    concern — this makes it observable rather than silent.
    """
    await stub.create_code("PSX-FIRST001", offer_id="offer-9")
    await stub.create_code("PSX-SECOND01", offer_id="offer-9")

    index = (await stub.http.get("/_stub/codes")).json()
    assert index["codes_by_offer"]["offer-9"] == ["PSX-FIRST001", "PSX-SECOND01"], (
        "creation order, both codes; the first mint must not vanish"
    )
    assert index["by_offer"]["offer-9"] == "PSX-SECOND01", "the single view is the latest"
    for code in index["codes_by_offer"]["offer-9"]:
        assert code in index["codes"], "every indexed code must be a key of the code table"


async def test_every_offer_index_key_resolves_in_the_code_table(stub: StubClient) -> None:
    """The invariant, stated once over a mixed batch rather than per case."""
    await stub.create_code("PSX-MIXED001", offer_id="offer-a")
    await stub.create_code("psx-mixed002", offer_id="offer-a")
    await stub.create_code("PSX-MIXED003", offer_id="offer-b")
    await stub.create_code("PSX-NOOFFER2")

    index = (await stub.http.get("/_stub/codes")).json()
    assert set(index["by_offer"]) == set(index["codes_by_offer"]) == {"offer-a", "offer-b"}
    for offer_id, minted in index["codes_by_offer"].items():
        assert minted, f"{offer_id} is indexed with no codes"
        assert index["by_offer"][offer_id] == minted[-1]
        for code in minted:
            assert code == code.upper(), "index keys are normalised"
            assert index["codes"][code]["offer_id"] == offer_id
    assert "PSX-NOOFFER2" not in {c for m in index["codes_by_offer"].values() for c in m}
