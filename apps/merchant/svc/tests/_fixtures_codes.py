"""Fixtures for T-052's discount-code tests. Loaded by the frozen sibling ``conftest.py``.

Every name here is prefixed ``codes_`` so it cannot collide with ``_fixtures_install.py``
or ``_fixtures_onboarding.py`` in the same directory — ``scripts/check_verify_contracts.py``
fails the gate on a duplicate fixture name, and the prefix is how the three files stay out
of each other's way.

The Admin client double here is **not** the frozen acceptance suite's ``_RecordingAdminClient``.
That one auto-vivifies every missing key, which is the right shape for grading an
implementation blind; this one answers only what a real Admin API would answer, so a test
that passes here is evidence about the response shapes production actually sees.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

#: The reference instant every code test anchors on. A constant, never ``datetime.now()``:
#: a validity-window assertion that reads the machine clock has a different verdict at
#: different moments, which is not a test.
CODES_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

#: A shop whose automatic discounts combine with everything.
PERMISSIVE_SHOP = {
    "combinesWith": {
        "orderDiscounts": True,
        "productDiscounts": True,
        "shippingDiscounts": True,
    }
}

#: A shop running a sitewide automatic discount that combines with nothing.
CONFLICTING_SHOP = {
    "combinesWith": {
        "orderDiscounts": False,
        "productDiscounts": False,
        "shippingDiscounts": False,
    },
    "hasActiveAutomaticDiscount": True,
    "automaticDiscounts": [
        {"id": "gid://shopify/DiscountAutomaticNode/9", "title": "sitewide"},
    ],
}


class RecordingAdmin:
    """An Admin GraphQL double that answers only what a real Admin API would.

    Args:
        shop: the shop-configuration block the discount query answers with. ``None`` makes
            that query answer nothing usable, which is what a limited Admin surface does.
        user_errors: what ``discountCodeBasicCreate`` returns in ``userErrors``.
        config_error: an exception the configuration query raises instead of answering.
        mutation_error: an exception the mutation raises instead of answering.
    """

    def __init__(
        self,
        shop: Any = None,
        *,
        user_errors: list[Any] | None = None,
        config_error: Exception | None = None,
        mutation_error: Exception | None = None,
    ) -> None:
        self.shop = PERMISSIVE_SHOP if shop is None else shop
        self.user_errors = user_errors or []
        self.config_error = config_error
        self.mutation_error = mutation_error
        self.calls: list[tuple[str, Any]] = []

    def execute(self, document: str, variables: Any = None) -> dict[str, Any]:
        self.calls.append((document, variables))
        if "ShopDiscountConfiguration" in document:
            if self.config_error is not None:
                raise self.config_error
            return {"data": dict(self.shop)} if self.shop is not None else {"data": {}}
        if "discountCodeBasicCreate" in document:
            if self.mutation_error is not None:
                raise self.mutation_error
            return {
                "data": {
                    "discountCodeBasicCreate": {
                        "codeDiscountNode": {"id": "gid://shopify/DiscountCodeNode/1"},
                        "userErrors": list(self.user_errors),
                    }
                }
            }
        return {"data": {}}

    def mutations(self) -> list[Any]:
        """The variables of every ``discountCodeBasicCreate`` call, in order."""
        return [
            variables for document, variables in self.calls if "discountCodeBasicCreate" in document
        ]

    def basic_inputs(self) -> list[dict[str, Any]]:
        """The ``basicCodeDiscount`` input of every mutation call, in order."""
        return [dict(variables["basicCodeDiscount"]) for variables in self.mutations()]


def offer_for(
    code_hint: str = "offer-1",
    **overrides: Any,
) -> dict[str, Any]:
    """The flat accepted-offer body the exchange posts to ``/codes``.

    Deliberately the same field set the frozen acceptance suite uses, so a test written
    here and the graded one disagree about nothing except what they assert.
    """
    offer: dict[str, Any] = {
        "offer_id": code_hint,
        "auction_id": "auc-1",
        "bid_ref": "bid-1",
        "product_ref": "gid://shopify/Product/9",
        "variant_id": "gid://shopify/ProductVariant/1001",
        "quantity": 1,
        "list_price": "100.00",
        "unit_price": "90.00",
        "currency": "USD",
        "discount_pct": 10.0,
        "discount_type": "percentage",
        "checkout_url": "https://acceptance-store.myshopify.com/cart",
    }
    offer.update(overrides)
    return offer


@pytest.fixture
def codes_now() -> datetime:
    """The reference instant. Injected, never read from the machine clock."""
    return CODES_NOW


@pytest.fixture
def codes_offer() -> dict[str, Any]:
    """One accepted offer, in the flat shape."""
    return offer_for()


@pytest.fixture
def codes_admin() -> RecordingAdmin:
    """An Admin client double for a shop whose discounts combine with everything."""
    return RecordingAdmin()


@pytest.fixture
def codes_ledger() -> Any:
    """A fresh ledger, so one test's events cannot be counted by another."""
    from merchant_svc.codes.ledger import CodeLedger

    return CodeLedger()


@pytest.fixture
def codes_register(codes_ledger: Any) -> Any:
    """A fresh redemption register writing into ``codes_ledger``."""
    from merchant_svc.codes.redemption import RedemptionRegister

    return RedemptionRegister(ledger=codes_ledger)


@pytest.fixture
def codes_service_state() -> Iterator[tuple[Any, Any]]:
    """The **service-wide** register and ledger, emptied before and after the test.

    Needed by anything exercising the module-level :func:`on_redemption`, which is the
    entry point a webhook handler actually calls. Emptying afterwards as well as before is
    the half that matters: a test that leaves codes in the process-wide register changes
    what the *next* test measures.
    """
    from merchant_svc.codes.ledger import CODE_LEDGER
    from merchant_svc.codes.redemption import REDEMPTIONS

    REDEMPTIONS.clear()
    CODE_LEDGER.clear()
    try:
        yield REDEMPTIONS, CODE_LEDGER
    finally:
        REDEMPTIONS.clear()
        CODE_LEDGER.clear()
