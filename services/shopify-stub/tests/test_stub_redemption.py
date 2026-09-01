"""Acceptance criterion 2: valid codes apply, invalid and conflicting ones are IGNORED.

"Silently ignores" is the whole criterion, and it has two halves that must both hold:

1. the discount is **not applied** — the cart total is the undiscounted total; and
2. the response is **indistinguishable** from one where no code was supplied at all — same
   status, same body keys, same ``discount_code: null``, no error member, no message.

Half 2 is the one a lazy implementation fails, by returning a 400 or by adding a helpful
``"error": "code expired"`` to the body. Both would be wrong: T-052 and T-033 build against
this behaviour, and a stub that errors here would make them handle an exception path that
real Shopify never produces.

**A documented divergence from the source material.** Neither shopify.dev nor
help.shopify.com states what a cart permalink does with an unusable ``discount`` parameter.
The behaviour asserted here is mandated by this ticket's acceptance criterion 2, and is
consistent with the Storefront API, which models a non-applying code as
``CartDiscountCode.applicable: false`` on an *otherwise successful* mutation rather than as
an error. It is not, however, quoted from a Shopify page — see
``fixtures/recorded/cart_permalink.json``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from shopify_stub.testing import SEED_VARIANT, StubClient

VARIANT_ID = int(SEED_VARIANT["variant_id"])


def _future(hours: int) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _past(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


async def _cart(stub: StubClient, code: str | None) -> dict[str, Any]:
    response = await stub.visit_cart(VARIANT_ID, code=code)
    assert response.status_code == 303, response.text
    return response.json()


async def test_a_valid_code_applies(stub: StubClient) -> None:
    await stub.create_code("PSX-VALID001", percentage=0.10, ends_at=_future(24))
    cart = await _cart(stub, "PSX-VALID001")
    assert cart["discount_code"] == "PSX-VALID001"
    assert cart["items_subtotal_price"] == "100.00"
    assert cart["total_discount"] == "10.00"
    assert cart["total_price"] == "90.00"


async def test_the_response_is_a_redirect_to_checkout(stub: StubClient) -> None:
    """A cart permalink lands the shopper in checkout; the token is the join key."""
    response = await stub.visit_cart(VARIANT_ID)
    assert response.status_code == 303
    token = response.json()["token"]
    assert response.headers["location"].endswith(f"/checkouts/{token}")


async def test_no_code_at_all_is_the_reference_response(stub: StubClient) -> None:
    """The baseline every "silently ignored" case must be indistinguishable from."""
    cart = await _cart(stub, None)
    assert cart["discount_code"] is None
    assert cart["total_discount"] == "0.00"
    assert cart["total_price"] == "100.00"


async def test_an_unknown_code_is_silently_ignored(stub: StubClient) -> None:
    reference = await _cart(stub, None)
    cart = await _cart(stub, "PSX-NEVERMADE")
    assert cart["discount_code"] is None
    assert cart["total_price"] == reference["total_price"]
    assert set(cart) == set(reference), (
        "an ignored code must not add an error or message member to the cart body"
    )


async def test_an_expired_code_is_silently_ignored(stub: StubClient) -> None:
    await stub.create_code("PSX-EXPIRED1", starts_at=_past(72), ends_at=_past(1))
    cart = await _cart(stub, "PSX-EXPIRED1")
    assert cart["discount_code"] is None
    assert cart["total_price"] == "100.00"

    detail = (await stub.checkout(cart["token"])).json()
    assert detail["requested_discount_code"] == "PSX-EXPIRED1"
    assert detail["applied_discount_code"] is None
    assert detail["rejection_reason"] == "expired"


async def test_a_not_yet_active_code_is_silently_ignored(stub: StubClient) -> None:
    await stub.create_code("PSX-FUTURE01", starts_at=_future(1), ends_at=_future(24))
    cart = await _cart(stub, "PSX-FUTURE01")
    assert cart["discount_code"] is None
    detail = (await stub.checkout(cart["token"])).json()
    assert detail["rejection_reason"] == "not_yet_active"


async def test_an_exhausted_single_use_code_is_silently_ignored(stub: StubClient) -> None:
    """D22's ``usageLimit: 1``, exercised through a real redemption.

    This is the case A5 calls out — "single-use discount codes race under concurrent
    redemption" — and the stub's answer is that the second shopper simply pays full price,
    not that the second checkout fails.
    """
    await stub.create_code("PSX-ONEUSE01", ends_at=_future(24))

    first = await _cart(stub, "PSX-ONEUSE01")
    assert first["discount_code"] == "PSX-ONEUSE01"
    completed = await stub.complete(first["token"])
    assert completed.status_code == 201, completed.text

    second = await _cart(stub, "PSX-ONEUSE01")
    assert second["discount_code"] is None
    assert second["total_price"] == "100.00"
    detail = (await stub.checkout(second["token"])).json()
    assert detail["rejection_reason"] == "usage_limit_reached"


async def test_an_abandoned_cart_does_not_consume_the_single_use(stub: StubClient) -> None:
    """Visiting the permalink is not redeeming it. The negative control for the test above.

    Without this, an implementation that decremented the usage limit on *visit* would pass
    every "single use is enforced" test while quietly burning every code a shopper merely
    looked at.
    """
    await stub.create_code("PSX-ABANDON1", ends_at=_future(24))

    for _ in range(5):
        cart = await _cart(stub, "PSX-ABANDON1")
        assert cart["discount_code"] == "PSX-ABANDON1", "an unused code stays usable"

    codes = (await stub.http.get("/_stub/codes")).json()["codes"]
    assert codes["PSX-ABANDON1"]["usage_count"] == 0

    completed = await stub.complete(cart["token"])
    assert completed.status_code == 201
    codes = (await stub.http.get("/_stub/codes")).json()["codes"]
    assert codes["PSX-ABANDON1"]["usage_count"] == 1


async def test_code_matching_is_case_insensitive_over_http(stub: StubClient) -> None:
    """Shopify redeems codes case-insensitively; a lower-cased link must still work."""
    await stub.create_code("PSX-MIXCASE1", ends_at=_future(24))
    cart = await _cart(stub, "psx-mixcase1")
    assert cart["discount_code"] == "PSX-MIXCASE1", "the stored casing is what is applied"


async def test_a_blank_discount_parameter_is_ignored(stub: StubClient) -> None:
    """``?discount=`` is a link-building bug, and it is still a silent no-op."""
    response = await stub.http.get(f"/cart/{VARIANT_ID}:1?discount=")
    assert response.status_code == 303
    body = response.json()
    assert body["discount_code"] is None
    detail = (await stub.checkout(body["token"])).json()
    assert detail["rejection_reason"] is None, (
        "an empty parameter is 'no code supplied', not 'a code that failed'"
    )


async def test_quantity_multiplies_the_line_and_the_discount(stub: StubClient) -> None:
    await stub.create_code("PSX-QTY00001", percentage=0.25, ends_at=_future(24))
    response = await stub.visit_cart(VARIANT_ID, quantity=3, code="PSX-QTY00001")
    cart = response.json()
    assert cart["items_subtotal_price"] == "300.00"
    assert cart["total_discount"] == "75.00"
    assert cart["total_price"] == "225.00"


async def test_an_unknown_variant_is_a_404_not_a_silent_no_op(stub: StubClient) -> None:
    """The silence is for *codes*, not for structurally broken links.

    Negative control for the whole "silently ignore" behaviour: if the stub swallowed this
    too, a permalink built with the wrong variant id would produce an empty cart and a
    green test instead of a loud failure in whoever built the link.
    """
    response = await stub.visit_cart(999_999_999)
    assert response.status_code == 404
    assert "errors" in response.json()


async def test_a_multi_variant_permalink_is_refused(stub: StubClient) -> None:
    """Out of scope by D22, and refused explicitly rather than half-handled."""
    response = await stub.http.get(f"/cart/{VARIANT_ID}:1,{VARIANT_ID}:2")
    assert response.status_code == 404
    assert "single-variant" in response.json()["errors"]


async def test_completing_a_checkout_twice_is_refused(stub: StubClient) -> None:
    """One checkout token must produce at most one ``orders/paid``.

    A second order for the same token would be indistinguishable, downstream, from a
    genuine duplicate-redemption integrity event (A5), so the stub refuses at the source.
    """
    cart = await _cart(stub, None)
    assert (await stub.complete(cart["token"])).status_code == 201
    second = await stub.complete(cart["token"])
    assert second.status_code == 409
    assert "already completed" in second.json()["errors"]


async def test_completing_an_unknown_checkout_is_a_404(stub: StubClient) -> None:
    assert (await stub.complete("deadbeef")).status_code == 404
