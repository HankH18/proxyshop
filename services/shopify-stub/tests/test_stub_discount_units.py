"""T-183: the unit a discount crosses the exchange/stub boundary in, pinned from both ends.

Two halves of this system spell "20% off" differently, and both spellings are correct:

=================================  =========================  =========================
where                              spelling                   why it is right there
=================================  =========================  =========================
``contracts.Discount.value``       ``20.0`` — percentage      the protocol's own unit. The
                                   points                     envelope cap is
                                                              ``max_discount_pct`` with
                                                              ``maximum: 100``, and
                                                              ``apps/trust`` reconciles
                                                              against
                                                              ``discountApplications[].value``,
                                                              which Shopify reports in
                                                              points.
``customerGets.value.percentage``  ``0.2`` — a fraction in    the real Admin API's input
                                   ``[0.0, 1.0]``             constraint. The recorded
                                                              fixture
                                                              ``admin_discount_code_basic_create.json``
                                                              carries the sentence "Value
                                                              must be between 0.00 - 1.00"
                                                              against the INPUT object, and
                                                              the stub reproduces it.
=================================  =========================  =========================

So neither side is wrong; what was missing was the **conversion between them**, and a 20.0
arriving where a 0.2 was expected is a two-orders-of-magnitude error at the exact point money
is decided. ``exchange.checkout.shopify_discount_percentage`` is the one place that
conversion happens, and this module is the only test in the repo that drives a protocol
``Discount`` all the way through it into the stub and reads the money back.

It is here rather than beside the exchange's own tests because only this directory can serve
a live stub. It imports from ``exchange`` deliberately: a test that exercised one side alone
could not fail when the *other* side drifted, and drift on either side is the whole risk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from contracts import Discount
from exchange.checkout import shopify_discount_percentage
from shopify_stub.testing import SEED_VARIANT, StubClient

VARIANT_ID = int(SEED_VARIANT["variant_id"])

#: The protocol grant a store agent issues, verbatim: 20 percentage points off.
GRANTED_PERCENT = 20.0

#: What Shopify's discount input must receive for that same grant.
EXPECTED_FRACTION = 0.2

#: 20% of the 100.00 seed variant. Hand-computed from the rule, not read back from the code.
EXPECTED_DISCOUNT = "20.00"
EXPECTED_TOTAL = "80.00"


def _future(hours: int) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _payload(response: Any) -> dict[str, Any]:
    body = response.json()
    assert "errors" not in body, body
    return body["data"]["discountCodeBasicCreate"]


async def test_a_protocol_discount_reaches_the_merchant_as_the_right_amount_of_money(
    stub: StubClient,
) -> None:
    """The whole crossing, end to end: ``Discount(value=20.0)`` takes 20.00 off a 100.00 cart.

    This fails if EITHER side drifts. Change the stub to read ``customerGets`` as points and
    the mutation now applies 0.2% — ``total_price`` becomes ``99.80``. Change the exchange's
    converter to pass the value through and the mutation is refused outright with a
    ``userErrors`` entry. Change ``contracts.Discount`` to mean a fraction and the converter's
    range check refuses 20.0 before the stub is ever reached.
    """
    granted = Discount(type="percentage", value=GRANTED_PERCENT)
    fraction = shopify_discount_percentage(granted)
    assert fraction == EXPECTED_FRACTION

    created = await stub.create_code("PSX-UNIT0020", percentage=fraction, ends_at=_future(24))
    payload = _payload(created)
    assert payload["userErrors"] == [], payload
    node = payload["codeDiscountNode"]["codeDiscount"]
    assert node["customerGets"]["value"]["percentage"] == EXPECTED_FRACTION

    cart = (await stub.visit_cart(VARIANT_ID, code="PSX-UNIT0020")).json()
    assert cart["items_subtotal_price"] == "100.00"
    assert cart["total_discount"] == EXPECTED_DISCOUNT
    assert cart["total_price"] == EXPECTED_TOTAL

    completed = (await stub.complete(cart["token"])).json()
    assert completed["total_price"] == EXPECTED_TOTAL


async def test_the_unconverted_protocol_value_is_refused_rather_than_applied(
    stub: StubClient,
) -> None:
    """The negative control, and the failure this ticket exists to make impossible.

    Sending ``contracts.Discount.value`` straight through — the thing a producer does when
    nobody told it there were two units — must be a loud refusal. A stub that clamped, or one
    that read points, would take 100.00 off a 100.00 order and hand the shopper a free one.
    """
    response = await stub.create_code("PSX-RAWPCT20", percentage=GRANTED_PERCENT)
    payload = _payload(response)
    assert payload["codeDiscountNode"] is None
    (error,) = payload["userErrors"]
    assert error["code"] == "INVALID"
    assert error["field"] == ["basicCodeDiscount", "customerGets", "value", "percentage"]


async def test_the_order_side_reports_the_discount_back_in_the_protocols_own_unit(
    stub: StubClient,
) -> None:
    """The return leg. ``apps/trust`` reconciles a promised percent against this number.

    Shopify's ``discount_applications[].value`` is in percentage POINTS even though the
    mutation's input was a fraction — the two units genuinely coexist inside one API — so a
    stub that echoed the fraction back here would make every reconciliation of a 20% promise
    look like a 0.2% delivery and fail an honest store's trust score.
    """
    fraction = shopify_discount_percentage({"type": "percentage", "value": GRANTED_PERCENT})
    created = await stub.create_code("PSX-ROUNDTRP", percentage=fraction, ends_at=_future(24))
    assert _payload(created)["userErrors"] == []

    cart = (await stub.visit_cart(VARIANT_ID, code="PSX-ROUNDTRP")).json()
    assert (await stub.complete(cart["token"])).status_code == 201

    (edge,) = (await stub.orders()).json()["data"]["orders"]["edges"]
    (application,) = edge["node"]["discountApplications"]["edges"]
    assert application["node"]["value"]["percentage"] == pytest.approx(GRANTED_PERCENT)
    assert application["node"]["code"] == "PSX-ROUNDTRP"
