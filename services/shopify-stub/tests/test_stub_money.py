"""T-105: money asserted against an *absolute*, not against another copy of itself.

Before this module, every money assertion in the suite was one of two kinds, and neither
could see a rounding change:

* a **round** number — every literal used 10% or 25% of 100.00, where ROUND_HALF_UP and
  ROUND_HALF_EVEN produce the same cents, so no literal in the suite was sensitive to the
  rounding mode at all; or
* a **self-comparison** — ``test_the_cart_quote_equals_the_amount_the_order_charges`` does
  use awkward percentages, but it compares the cart total to the order total to the GraphQL
  total, and all three are computed by the same call to
  :func:`~shopify_stub.codes.discount_amount_for`. Change the rounding mode and all three
  move together, so the equality still holds.

Measured, on the suite as it stood: neutering ``discount_amount_for`` from ROUND_HALF_UP to
ROUND_HALF_EVEN shipped **297 passed**. A shopper quoted 87.66 and charged 87.65 is the
worst bug this stub could teach a consumer to tolerate, and the arithmetic that prevents it
had no test that could fail.

So the assertions here are **absolute**: a decimal literal, computed by hand from D22's
rule, that only ROUND_HALF_UP can produce. And they cover the two branches nothing reached —
``fixed_amount`` and the clamp — which had no test of any kind.

ROUND_HALF_UP is the correct mode and is not in question here (it is what
:func:`~shopify_stub.state.money` renders with, so the stored decimal and the wire string
agree). What is in question is whether anything would notice if it changed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any

import pytest
from shopify_stub.codes import DiscountCode, discount_amount_for
from shopify_stub.state import money
from shopify_stub.testing import DISCOUNT_MUTATION, SEED_VARIANT, StubClient

VARIANT_ID = int(SEED_VARIANT["variant_id"])


def _future(hours: int) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _percentage(value: float) -> DiscountCode:
    return DiscountCode(code="PSX-PCT00000", percentage=value)


def _fixed(value: str) -> DiscountCode:
    return DiscountCode(code="PSX-FIX00000", fixed_amount=value)


async def _create_fixed_amount_code(
    stub: StubClient, code: str, amount: str, *, currency: str = "USD"
) -> Any:
    """Mint a ``discountAmount`` code. ``StubClient.create_code`` only sends percentages."""
    return await stub.graphql(
        DISCOUNT_MUTATION,
        {
            "basicCodeDiscount": {
                "title": code,
                "code": code,
                "usageLimit": 1,
                "appliesOncePerCustomer": True,
                "combinesWith": {
                    "orderDiscounts": False,
                    "productDiscounts": False,
                    "shippingDiscounts": False,
                },
                "customerGets": {
                    "value": {"discountAmount": {"amount": amount, "currencyCode": currency}},
                    "items": {"all": True},
                },
                "endsAt": _future(24),
            }
        },
    )


# ---------------------------------------------------------------------------------------
# Acceptance 1: an absolute, over the real route
# ---------------------------------------------------------------------------------------


async def test_a_half_cent_discount_is_rounded_half_up_end_to_end(stub: StubClient) -> None:
    """12.345% of 100.00 is 12.345 exactly. D22's rule says the shopper pays 87.65.

    Every number in this test is a literal computed from the rule, not read back from the
    code under test. ROUND_HALF_EVEN would produce ``12.34``/``87.66`` here — that is the
    entire point of choosing a rate whose discount lands exactly on a half-cent, and of
    choosing one whose preceding cent digit is *even*, since HALF_UP and HALF_EVEN agree
    whenever it is odd.
    """
    created = await stub.create_code("PSX-HALFCENT", percentage=0.12345, ends_at=_future(24))
    assert created.json()["data"]["discountCodeBasicCreate"]["userErrors"] == []

    response = await stub.visit_cart(VARIANT_ID, code="PSX-HALFCENT")
    assert response.status_code == 303, response.text
    cart = response.json()

    assert cart["items_subtotal_price"] == "100.00"
    assert cart["total_discount"] == "12.35"
    assert cart["total_price"] == "87.65"

    completed = (await stub.complete(cart["token"])).json()
    assert completed["total_price"] == "87.65"

    (node,) = [edge["node"] for edge in (await stub.orders()).json()["data"]["orders"]["edges"]]
    assert node["totalPriceSet"]["shopMoney"]["amount"] == "87.65"
    assert node["totalDiscountsSet"]["shopMoney"]["amount"] == "12.35"


#: ``(percentage, quantity, expected_discount, expected_total)`` — every row hand-computed
#: from ``round_half_up(subtotal * percentage, 0.01)`` on the 100.00 seed variant, and every
#: row chosen so ROUND_HALF_EVEN gives a *different* answer. A table of rates where the two
#: modes agree would be exactly the mistake this module exists to correct.
_HALF_UP_ONLY = [
    (0.12345, 1, "12.35", "87.65"),
    (0.05125, 1, "5.13", "94.87"),
    (0.00125, 1, "0.13", "99.87"),
    (0.30625, 1, "30.63", "69.37"),
    (0.00625, 1, "0.63", "99.37"),
    (0.66125, 1, "66.13", "33.87"),
]


@pytest.mark.parametrize(("percentage", "quantity", "discount", "total"), _HALF_UP_ONLY)
async def test_every_half_cent_rate_is_charged_half_up(
    stub: StubClient, percentage: float, quantity: int, discount: str, total: str
) -> None:
    """The same absolute assertion across six rates, so one lucky literal cannot carry it."""
    code = f"PSX-HC{int(percentage * 100000):06d}"[:12].ljust(12, "0")
    created = await stub.create_code(code, percentage=percentage, ends_at=_future(24))
    assert created.json()["data"]["discountCodeBasicCreate"]["userErrors"] == []

    cart = (await stub.visit_cart(VARIANT_ID, quantity=quantity, code=code)).json()
    assert cart["total_discount"] == discount
    assert cart["total_price"] == total


# ---------------------------------------------------------------------------------------
# Acceptance 2: direct unit tests of discount_amount_for
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("percentage", "subtotal", "expected"),
    [
        (0.12345, "100.00", "12.35"),
        (0.05125, "100.00", "5.13"),
        (0.00125, "100.00", "0.13"),
        (0.30625, "100.00", "30.63"),
        (0.5025, "10.00", "5.03"),
        (0.4025, "10.00", "4.03"),
        (0.1875, "12.40", "2.33"),
        (0.10, "100.00", "10.00"),
        (0.25, "100.00", "25.00"),
    ],
)
def test_discount_amount_for_percentage_rounds_half_up(
    percentage: float, subtotal: str, expected: str
) -> None:
    """The percentage branch, against literals no other code path produced.

    The last two rows are the round rates the old suite was built from; they are kept as a
    control, and on their own they prove nothing about the rounding mode — the seven above
    them are the rows that do.
    """
    assert discount_amount_for(_percentage(percentage), Decimal(subtotal)) == Decimal(expected)


@pytest.mark.parametrize(
    ("percentage", "subtotal"),
    [
        (0.12345, "100.00"),
        (0.05125, "100.00"),
        (0.00125, "100.00"),
        (0.30625, "100.00"),
        (0.5025, "10.00"),
        (0.4025, "10.00"),
        (0.1875, "12.40"),
    ],
)
def test_the_percentage_rows_actually_discriminate_the_rounding_mode(
    percentage: float, subtotal: str
) -> None:
    """The meta-assertion: each row above must *disagree* under ROUND_HALF_EVEN.

    Without this, a future edit could quietly replace a discriminating rate with a round one
    and leave the table looking just as thorough while testing nothing. This computes both
    roundings independently of ``discount_amount_for`` and asserts they differ.
    """
    raw = Decimal(subtotal) * Decimal(str(percentage))
    half_up = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    half_even = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    assert half_up != half_even, (
        f"{percentage} of {subtotal} rounds to {half_up} under both modes, so this row "
        f"cannot detect a rounding change"
    )
    assert discount_amount_for(_percentage(percentage), Decimal(subtotal)) == half_up


def test_discount_amount_for_fixed_amount_is_the_amount_itself() -> None:
    """The ``fixed_amount`` branch (``codes.py``), which had no test of any kind."""
    assert discount_amount_for(_fixed("15.00"), Decimal("100.00")) == Decimal("15.00")
    assert discount_amount_for(_fixed("0.01"), Decimal("100.00")) == Decimal("0.01")
    assert discount_amount_for(_fixed("99.99"), Decimal("100.00")) == Decimal("99.99")


def test_a_fixed_amount_reaches_the_wire_rounded_half_up() -> None:
    """A sub-cent fixed amount is quantized where every amount is: :func:`money`.

    ``discount_amount_for`` returns the fixed amount unquantized; the 2-place string the
    shopper sees comes from ``money``, and it must be the same HALF_UP as the percentage
    branch or the two value types would round differently for no reason.
    """
    assert money(discount_amount_for(_fixed("12.345"), Decimal("100.00"))) == "12.35"
    assert money(discount_amount_for(_fixed("0.005"), Decimal("100.00"))) == "0.01"


@pytest.mark.parametrize(
    ("discount", "subtotal", "expected"),
    [
        (_fixed("500.00"), "100.00", "100.00"),  # fixed amount larger than the cart
        (_fixed("100.00"), "100.00", "100.00"),  # exactly the cart
        (_percentage(2.0), "100.00", "100.00"),  # a percentage over 1.0
        (_fixed("-5.00"), "100.00", "0"),  # never negative
        (_percentage(-0.5), "100.00", "0"),  # nor from a negative rate
    ],
)
def test_discount_amount_for_clamps_to_the_cart(
    discount: DiscountCode, subtotal: str, expected: str
) -> None:
    """``min(max(amount, 0), subtotal)`` — the clamp, which also had no test.

    Both ends matter and for different reasons: above ``subtotal`` a discount would produce a
    negative total (the stub paying the shopper); below zero it would *add* to the bill.
    """
    assert discount_amount_for(discount, Decimal(subtotal)) == Decimal(expected)


@pytest.mark.parametrize("subtotal", ["0.00", "-1.00"])
def test_no_discount_is_taken_off_an_empty_or_negative_cart(subtotal: str) -> None:
    assert discount_amount_for(_percentage(0.5), Decimal(subtotal)) == Decimal("0")


def test_no_discount_when_there_is_no_code_or_no_value() -> None:
    assert discount_amount_for(None, Decimal("100.00")) == Decimal("0")
    assert discount_amount_for(DiscountCode(code="PSX-NOVALUE0"), Decimal("100.00")) == (
        Decimal("0")
    )


# ---------------------------------------------------------------------------------------
# The same two branches, over the real route
# ---------------------------------------------------------------------------------------


async def test_a_fixed_amount_code_discounts_the_cart_by_that_amount(stub: StubClient) -> None:
    """``customerGets.value.discountAmount`` end to end — the branch the suite never sent."""
    created = await _create_fixed_amount_code(stub, "PSX-FIXED001", "15.00")
    assert created.json()["data"]["discountCodeBasicCreate"]["userErrors"] == []

    cart = (await stub.visit_cart(VARIANT_ID, code="PSX-FIXED001")).json()
    assert cart["discount_code"] == "PSX-FIXED001"
    assert cart["total_discount"] == "15.00"
    assert cart["total_price"] == "85.00"

    completed = (await stub.complete(cart["token"])).json()
    assert completed["total_price"] == "85.00"


async def test_a_fixed_amount_larger_than_the_cart_is_clamped_not_negative(
    stub: StubClient,
) -> None:
    """The clamp over HTTP: the shopper pays 0.00, and the stub never owes them money."""
    created = await _create_fixed_amount_code(stub, "PSX-FIXED999", "500.00")
    assert created.json()["data"]["discountCodeBasicCreate"]["userErrors"] == []

    cart = (await stub.visit_cart(VARIANT_ID, code="PSX-FIXED999")).json()
    assert cart["items_subtotal_price"] == "100.00"
    assert cart["total_discount"] == "100.00"
    assert cart["total_price"] == "0.00"

    completed = (await stub.complete(cart["token"])).json()
    assert completed["total_price"] == "0.00"
    assert not completed["total_price"].startswith("-")


async def test_the_quantity_multiplies_the_subtotal_before_the_rate_is_applied(
    stub: StubClient,
) -> None:
    """3 x 100.00 at 12.345% is 37.035 -> 37.04, not 3 x 12.35 = 37.05.

    Order of operations, pinned absolutely: rounding each line and then summing gives a
    different cent from rounding the total once, and this is the only test that says which
    one D22 means.
    """
    created = await stub.create_code("PSX-QTY00001", percentage=0.12345, ends_at=_future(24))
    assert created.json()["data"]["discountCodeBasicCreate"]["userErrors"] == []

    cart = (await stub.visit_cart(VARIANT_ID, quantity=3, code="PSX-QTY00001")).json()
    assert cart["items_subtotal_price"] == "300.00"
    assert cart["total_discount"] == "37.04"
    assert cart["total_price"] == "262.96"
