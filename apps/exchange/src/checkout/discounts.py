"""The one place a protocol ``Discount`` becomes a number Shopify's discount input accepts.

Two units, both correct, one conversion (T-183)
-----------------------------------------------

``contracts.Discount.value`` is in **percentage points**: ``20.0`` means 20% off. That is the
protocol's unit throughout, and it is not an accident of one field —
the policy `Envelope`'s ``max_discount_pct`` is published with ``minimum: 0, maximum: 100``,
``packages/store-agent`` prices a grant as ``list_price * (100 - pct) / 100``, and
``apps/trust`` reconciles a promise against ``discountApplications[].value``, which Shopify
reports in points as well.

``customerGets.value.percentage`` on Shopify's ``discountCodeBasicCreate`` input is a
**fraction in [0.0, 1.0]**: ``0.2`` means 20% off. That is the real Admin API's constraint,
not a stub invention — ``services/shopify-stub/fixtures/recorded/
admin_discount_code_basic_create.json`` records the sentence "Value must be between 0.00 -
1.00" against the INPUT object, and ``shopify_stub.graphql_admin`` refuses anything outside
it rather than clamping.

So neither side is wrong and neither side moves. What was missing is the conversion between
them, and its absence is a two-orders-of-magnitude error at the precise point money is
decided: a 20.0 arriving where a 0.2 belongs asks for 2000% off. Real Shopify refuses that
outright, which is the *lucky* outcome; the unlucky one is any consumer that clamps, because
clamping a 2000% request lands on 100% and hands the shopper a free order.

Why the conversion lives in one function
----------------------------------------

``value / 100`` is one line, which is exactly why it would otherwise be written at three call
sites and eventually at a fourth in the wrong direction. The other reason is that the
conversion is **not** always well defined and a call site is the worst place to discover it:
``{"type": "percentage", "value": 0.2}`` is a legal 0.2% discount and is indistinguishable
from a fraction that arrived in the wrong unit. Nothing here guesses. A value outside
``[0, 100]``, a non-numeric value, or a ``type`` this module has never heard of is refused
with :class:`UnusableDiscount` — fail closed, because the failure mode of guessing is money.

A fixed-amount discount has no percentage to send at all; it belongs in Shopify's
``discountAmount`` half of the same input union, so :func:`shopify_discount_percentage`
answers ``None`` for one rather than inventing a rate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .. import describe
from .codes import UnusableOffer

__all__ = [
    "FIXED_AMOUNT_DISCOUNT_TYPES",
    "MAX_DISCOUNT_PERCENT",
    "PERCENTAGE_DISCOUNT_TYPES",
    "PERCENT_PER_UNIT_FRACTION",
    "UnusableDiscount",
    "discount_percent",
    "offer_discount_percentage",
    "shopify_discount_percentage",
]


class UnusableDiscount(UnusableOffer):
    """A discount whose unit cannot be established, so no code may be minted from it.

    An :class:`~.codes.UnusableOffer`, because that is what it is: the offer carries a field
    the minting path cannot turn into a number, and it must be refused before anything is
    minted for the same reason every other unusable field is.
    """


#: ``contracts.Discount.type`` spellings that mean "a share of the price". The protocol types
#: the field as a free string, so the tolerated spellings are named rather than assumed.
PERCENTAGE_DISCOUNT_TYPES: frozenset[str] = frozenset({"percentage", "percent", "pct"})

#: Spellings that mean "a sum of money". These carry no percentage at all.
FIXED_AMOUNT_DISCOUNT_TYPES: frozenset[str] = frozenset({"fixed_amount", "fixed", "amount"})

#: Percentage points per unit fraction. The whole of the conversion, named so a reader can
#: see which direction it goes without deriving it from a division.
PERCENT_PER_UNIT_FRACTION = 100.0

#: The protocol's own ceiling: the policy `Envelope`'s `max_discount_pct` is `maximum: 100`.
MAX_DISCOUNT_PERCENT = 100.0


def _read(discount: Any, key: str) -> Any:
    """Read ``key`` off a mapping, a pydantic ``Discount``, or any object carrying it."""
    if isinstance(discount, Mapping):
        return discount.get(key)
    return getattr(discount, key, None)


def discount_percent(discount: Any) -> float | None:
    """The discount in the PROTOCOL's unit — percentage points — or ``None``.

    ``None`` means "this discount states no percentage": there is no discount, or it is a
    fixed money amount that belongs in Shopify's ``discountAmount`` instead.

    Raises:
        UnusableDiscount: the type is one this module cannot place, or the value is not a
            number in ``[0, 100]``. Both are refusals rather than corrections — see the
            module docstring on why 0.2 cannot be repaired into 20.
    """
    if discount is None:
        return None

    raw_type = _read(discount, "type")
    kind = str(raw_type).strip().lower() if raw_type is not None else ""
    if kind in FIXED_AMOUNT_DISCOUNT_TYPES:
        return None
    if kind not in PERCENTAGE_DISCOUNT_TYPES:
        raise UnusableDiscount(
            # `describe`: the discount is the BIDDING STORE's own JSON, and
            # `UnusableDiscount` subclasses `UnusableOffer`, so this message reaches the
            # same `checkout_refused` denial reason every other offer defect does.
            f"discount type {describe(raw_type)} is neither a percentage "
            f"({sorted(PERCENTAGE_DISCOUNT_TYPES)}) nor a fixed amount "
            f"({sorted(FIXED_AMOUNT_DISCOUNT_TYPES)}), so its unit cannot be established"
        )

    raw_value = _read(discount, "value")
    if isinstance(raw_value, bool) or raw_value is None:
        raise UnusableDiscount(f"discount value {describe(raw_value)} is not a number of percent")
    try:
        percent = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise UnusableDiscount(
            f"discount value {describe(raw_value)} is not a number of percent"
        ) from exc

    if not 0.0 <= percent <= MAX_DISCOUNT_PERCENT:
        raise UnusableDiscount(
            f"discount value {percent!r} is outside 0..{MAX_DISCOUNT_PERCENT:g} percentage "
            f"points; if it was meant as a 0.0-1.0 fraction it is already in Shopify's unit "
            f"and must not be converted twice"
        )
    return percent


def shopify_discount_percentage(discount: Any) -> float | None:
    """The same discount as the fraction ``customerGets.value.percentage`` takes, or ``None``.

    ``shopify_discount_percentage({"type": "percentage", "value": 20.0}) == 0.2``.

    ``None`` means the discount carries no percentage — send ``discountAmount`` instead, or
    no ``customerGets`` value at all when there is no discount.
    """
    percent = discount_percent(discount)
    if percent is None:
        return None
    return percent / PERCENT_PER_UNIT_FRACTION


def offer_discount_percentage(offer: Mapping[str, Any] | Any) -> float | None:
    """:func:`shopify_discount_percentage` of the discount an offer carries, if it carries one."""
    return shopify_discount_percentage(_read(offer, "discount"))
