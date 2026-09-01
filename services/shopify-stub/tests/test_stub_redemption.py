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

import pytest
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


async def test_a_conflicting_code_is_silently_ignored(stub: StubClient) -> None:
    """The "conflicting" half of acceptance 2, over HTTP rather than in a unit.

    A shop with an order-level automatic discount running cannot also apply a code whose
    ``combinesWith.orderDiscounts`` is false. Shopify drops the code; it does not error.
    This is the case T-052 has to detect *before* redirecting a buyer, so the stub has to
    make it reachable rather than only representable.
    """
    await stub.create_code(
        "PSX-NOCOMBIN",
        ends_at=_future(24),
        combines_with={
            "orderDiscounts": False,
            "productDiscounts": False,
            "shippingDiscounts": False,
        },
    )
    reference = await _cart(stub, None)

    await stub.configure(has_active_automatic_discount=True)
    cart = await _cart(stub, "PSX-NOCOMBIN")
    assert cart["discount_code"] is None
    assert cart["total_price"] == reference["total_price"]
    assert set(cart) == set(reference), "a conflict must not add an error member either"

    detail = (await stub.checkout(cart["token"])).json()
    assert detail["rejection_reason"] == "conflicts_with_existing_discount"


async def test_a_combining_code_still_applies_against_an_automatic_discount(
    stub: StubClient,
) -> None:
    """The negative control for the conflict test.

    Without it, an implementation that dropped *every* code whenever an automatic discount
    was running would pass the test above. The combinability flag has to be what decides.
    """
    await stub.create_code(
        "PSX-COMBINES",
        ends_at=_future(24),
        combines_with={
            "orderDiscounts": True,
            "productDiscounts": True,
            "shippingDiscounts": True,
        },
    )
    await stub.configure(has_active_automatic_discount=True)
    cart = await _cart(stub, "PSX-COMBINES")
    assert cart["discount_code"] == "PSX-COMBINES"
    assert cart["total_price"] == "90.00"


async def test_a_non_combining_code_applies_when_no_automatic_discount_runs(
    stub: StubClient,
) -> None:
    """The other negative control: the conflict must depend on the SHOP's state.

    A non-combining code is perfectly valid on a shop with no automatic discount, and an
    implementation that rejected it unconditionally would pass both tests above.
    """
    await stub.create_code(
        "PSX-NOCOMBIN",
        ends_at=_future(24),
        combines_with={
            "orderDiscounts": False,
            "productDiscounts": False,
            "shippingDiscounts": False,
        },
    )
    assert (await stub.config())["has_active_automatic_discount"] is False
    cart = await _cart(stub, "PSX-NOCOMBIN")
    assert cart["discount_code"] == "PSX-NOCOMBIN"


async def test_a_single_use_code_cannot_be_redeemed_twice_from_two_open_carts(
    stub: StubClient,
) -> None:
    """The race A5 names: two carts opened before either is completed.

    Both carts see a usage count of zero and both apply the code, so validating only at the
    cart would let a ``usageLimit: 1`` code be redeemed twice. Shopify re-checks the discount
    when payment is taken, and the second order comes through at FULL PRICE — which is what
    makes the duplicate observable downstream as an order whose discount was not honoured,
    rather than as a missing order or an exception.
    """
    await stub.create_code("PSX-RACE0001", percentage=0.10, ends_at=_future(24))

    first = await _cart(stub, "PSX-RACE0001")
    second = await _cart(stub, "PSX-RACE0001")
    assert first["discount_code"] == "PSX-RACE0001"
    assert second["discount_code"] == "PSX-RACE0001", (
        "both carts legitimately apply the code: neither has been paid for yet"
    )

    winner = (await stub.complete(first["token"])).json()
    assert winner["discount_code"] == "PSX-RACE0001"
    assert winner["total_price"] == "90.00"

    loser = (await stub.complete(second["token"])).json()
    assert loser["discount_code"] is None, "the second redemption must not be honoured"
    assert loser["total_price"] == "100.00", "and the shopper pays full price"

    codes = (await stub.codes())["codes"]
    assert codes["PSX-RACE0001"]["usage_count"] == 1, (
        "usage_count must never exceed usageLimit; a count of 2 means the single-use "
        "guarantee is not enforced anywhere"
    )

    detail = (await stub.checkout(second["token"])).json()
    assert detail["rejection_reason"] == "usage_limit_reached"


async def test_a_code_that_expires_between_cart_and_payment_is_not_honoured(
    stub: StubClient,
) -> None:
    """The other re-validation case, and the reason it is a re-check and not a cache.

    A cart built while the code was live must not carry that discount into an order paid
    after it expired.
    """
    from datetime import UTC, datetime, timedelta

    import time_machine

    start = datetime(2026, 6, 1, tzinfo=UTC)
    with time_machine.travel(start, tick=False) as traveller:
        await stub.create_code(
            "PSX-EXPIRING",
            starts_at=start.isoformat().replace("+00:00", "Z"),
            ends_at=(start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        )
        cart = await _cart(stub, "PSX-EXPIRING")
        assert cart["discount_code"] == "PSX-EXPIRING"

        traveller.shift(timedelta(hours=2))
        completed = (await stub.complete(cart["token"])).json()

    assert completed["discount_code"] is None
    assert completed["total_price"] == "100.00"


@pytest.mark.parametrize(
    ("percentage", "quantity"),
    [
        (0.12345, 1),  # 12.345 -> the half-way case the two roundings disagreed on
        (0.005, 1),  # 0.50 exactly
        (0.33333, 3),  # a repeating fraction over a multi-unit line
        (0.075, 1),  # 7.50
        (0.1, 7),  # a round rate over an odd quantity
        (0.999, 1),  # nearly the whole line
    ],
)
async def test_the_cart_quote_equals_the_amount_the_order_charges(
    stub: StubClient, percentage: float, quantity: int
) -> None:
    """A shopper must be charged exactly the total the cart quoted them.

    This is a REGRESSION TEST for a real defect. The cart route quantized the discount with
    Python's default ``ROUND_HALF_EVEN`` while order creation used ``ROUND_HALF_UP``, so a
    12.345% discount on a 100.00 line quoted 87.66 and charged 87.65. Every other test in
    this file used round percentages (10%, 25%) whose two roundings agree, so the whole suite
    was green over a live money bug.

    The parametrised rates are deliberately awkward for that reason: a rate whose discount
    lands exactly on a half-cent is the only kind that can expose a rounding disagreement,
    and a test suite made entirely of round numbers cannot.
    """
    code = f"PSX-RND{int(percentage * 100000):05d}"[:12].ljust(12, "0")
    created = await stub.create_code(code, percentage=percentage, ends_at=_future(24))
    assert created.json()["data"]["discountCodeBasicCreate"]["userErrors"] == []

    response = await stub.visit_cart(VARIANT_ID, quantity=quantity, code=code)
    assert response.status_code == 303
    cart = response.json()
    assert cart["discount_code"] == code

    completed = (await stub.complete(cart["token"])).json()
    assert completed["total_price"] == cart["total_price"], (
        f"quoted {cart['total_price']} but charged {completed['total_price']}"
    )

    # And the GraphQL view of the same order must agree with both.
    (node,) = [edge["node"] for edge in (await stub.orders()).json()["data"]["orders"]["edges"]]
    assert node["totalPriceSet"]["shopMoney"]["amount"] == cart["total_price"]
    assert node["totalDiscountsSet"]["shopMoney"]["amount"] == cart["total_discount"]
