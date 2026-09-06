"""Discount-code minting and validity, per D22.

D22 pins the shape of every code this system creates::

    PSX-XXXXXXXX      # 8 characters of *randomly generated* Crockford base32, uppercase
    usageLimit: 1
    expiry: min(now + 48h, offer.expires_at)
    stored keyed by offer_id

The "randomly generated" clause is load-bearing and is the reason this module exists
separately from the GraphQL layer: a code derived from ``offer_id`` (a hash, a truncation,
an encoding) is a *guessable* single-use redeemable, so nothing here may ever read
``offer_id`` on the code-minting path. :func:`mint_code` does not take an ``offer_id``
argument at all — that is the mechanical guarantee, not a comment.

Crockford base32 excludes ``I``, ``L``, ``O`` and ``U`` from the alphabet so that a code
read aloud or typed from a screenshot cannot be transcribed into a different valid code.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

#: Crockford base32, uppercase. 32 symbols; ``I``, ``L``, ``O``, ``U`` are deliberately absent.
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: The prefix every ProxyShop discount code carries (D22).
CODE_PREFIX = "PSX-"

#: Number of random Crockford symbols after the prefix (D22).
CODE_BODY_LENGTH = 8

#: The maximum life of a code, regardless of how far away the offer's own expiry is (D22).
MAX_CODE_LIFETIME = timedelta(hours=48)

#: D22: single use.
DEFAULT_USAGE_LIMIT = 1


class CombinesWithPolicy(StrEnum):
    """Shopify's ``DiscountCombinesWith`` flags, as the stub models them.

    Shopify's real input is an object of three booleans
    (``orderDiscounts``/``productDiscounts``/``shippingDiscounts``); this enum is only the
    stub's internal summary of "does this code combine with an order-level discount that is
    already on the cart". The wire shape stays the three booleans — see
    :mod:`shopify_stub.graphql_admin`.
    """

    NONE = "none"
    ORDER = "order"
    PRODUCT = "product"
    SHIPPING = "shipping"


@dataclass(frozen=True)
class CombinesWith:
    """The three booleans Shopify's ``DiscountCombinesWith`` carries."""

    order_discounts: bool = False
    product_discounts: bool = False
    shipping_discounts: bool = False

    def to_wire(self) -> dict[str, bool]:
        """The camelCase object Shopify returns and accepts."""
        return {
            "orderDiscounts": self.order_discounts,
            "productDiscounts": self.product_discounts,
            "shippingDiscounts": self.shipping_discounts,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object] | None) -> CombinesWith:
        """Parse Shopify's camelCase object. Missing keys default to ``False``."""
        payload = payload or {}
        return cls(
            order_discounts=bool(payload.get("orderDiscounts", False)),
            product_discounts=bool(payload.get("productDiscounts", False)),
            shipping_discounts=bool(payload.get("shippingDiscounts", False)),
        )


class RejectionReason(StrEnum):
    """Why a code was not applied to a cart.

    These are *stub-internal* diagnostics, recorded on the checkout so a test can assert
    **which** silent no-op happened. They are never surfaced to the shopper: acceptance
    criterion 2 requires an invalid or conflicting code to be silently ignored, matching
    real Shopify, which drops the ``discount`` parameter and renders the cart as if it had
    not been supplied.
    """

    UNKNOWN_CODE = "unknown_code"
    EXPIRED = "expired"
    NOT_YET_ACTIVE = "not_yet_active"
    USAGE_LIMIT_REACHED = "usage_limit_reached"
    CONFLICTS_WITH_EXISTING_DISCOUNT = "conflicts_with_existing_discount"
    MALFORMED = "malformed"


def mint_code(*, rng: secrets.SystemRandom | None = None) -> str:
    """Return a fresh ``PSX-XXXXXXXX`` code.

    Deliberately takes **no** offer identifier: D22 forbids deriving the code from
    ``offer_id``, and the cheapest way to keep that true forever is to make the derivation
    impossible to express here.

    Args:
        rng: a random source. Defaults to :class:`secrets.SystemRandom`. Injectable so a
            test can prove the *distribution* (all symbols reachable, no repeats across
            many draws) without reaching for monkeypatching.

    Returns:
        e.g. ``"PSX-7QK2ZB0M"``.
    """
    source = rng or secrets.SystemRandom()
    body = "".join(source.choice(CROCKFORD_ALPHABET) for _ in range(CODE_BODY_LENGTH))
    return f"{CODE_PREFIX}{body}"


def is_well_formed(code: str) -> bool:
    """``True`` iff ``code`` matches the D22 shape exactly.

    Case-sensitive on purpose: D22 says uppercase, and Shopify discount codes are
    *case-insensitive on redemption* but stored as created. The redemption path upper-cases
    before it looks a code up (see :meth:`DiscountCode.matches`); this predicate is about
    what the stub is willing to *mint and store*.
    """
    if not code.startswith(CODE_PREFIX):
        return False
    body = code[len(CODE_PREFIX) :]
    if len(body) != CODE_BODY_LENGTH:
        return False
    return all(character in CROCKFORD_ALPHABET for character in body)


def code_expiry(*, now: datetime, offer_expires_at: datetime | None) -> datetime:
    """``min(now + 48h, offer.expires_at)`` — D22's expiry rule.

    Args:
        now: the instant the code is created. Must be timezone-aware.
        offer_expires_at: the offer's own expiry, or ``None`` when the offer does not
            declare one — in which case the 48-hour cap is the whole rule.

    Returns:
        The instant the code stops being redeemable.

    Raises:
        ValueError: either ``now`` or ``offer_expires_at`` is naive — both are checked, with
            a message naming the one at fault. A naive datetime here silently shifts expiry
            by the host's UTC offset, which is a bug that only appears outside UTC, and it
            shifts it just as far whichever side of the ``min`` it arrives on. This clause
            used to blame ``now`` alone; the second check has been there since the function
            was written and ``test_stub_codes.test_expiry_refuses_naive_datetimes`` has
            always pinned it. The two sets are now compared by
            ``test_stub_codes.test_the_expiry_raises_clause_names_every_argument_that_raises``.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cap = now + MAX_CODE_LIFETIME
    if offer_expires_at is None:
        return cap
    if offer_expires_at.tzinfo is None:
        raise ValueError("offer_expires_at must be timezone-aware")
    return min(cap, offer_expires_at)


@dataclass
class DiscountCode:
    """One ``discountCodeBasicCreate``-created code, as the stub stores it.

    Attributes:
        code: the ``PSX-`` code itself.
        offer_id: the key the code is stored under (D22). Recorded *after* minting; never
            an input to minting.
        title: the discount's title, as passed to the mutation.
        starts_at: when the code becomes redeemable.
        ends_at: when it stops. ``None`` means "never", which this system never creates but
            Shopify's API permits, so the stub models it.
        usage_limit: total redemptions allowed across all customers. ``None`` = unlimited.
        applies_once_per_customer: Shopify's per-customer cap flag.
        combines_with: the three combination booleans.
        percentage: the discount value as a fraction (``0.15`` = 15% off). Exactly one of
            ``percentage``/``fixed_amount`` is set.
        fixed_amount: the discount value as a money amount.
        currency_code: currency for ``fixed_amount``.
        usage_count: how many times it has actually been redeemed.
        node_id: the ``gid://shopify/DiscountCodeNode/<n>`` GID.
    """

    code: str
    offer_id: str | None = None
    title: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    usage_limit: int | None = DEFAULT_USAGE_LIMIT
    applies_once_per_customer: bool = True
    combines_with: CombinesWith = field(default_factory=CombinesWith)
    percentage: float | None = None
    fixed_amount: str | None = None
    currency_code: str = "USD"
    usage_count: int = 0
    node_id: str = ""

    def matches(self, candidate: str) -> bool:
        """Shopify matches discount codes case-insensitively on redemption."""
        return candidate.strip().upper() == self.code.upper()

    def rejection(
        self,
        *,
        now: datetime,
        cart_has_order_discount: bool = False,
    ) -> RejectionReason | None:
        """Why this code cannot be applied right now, or ``None`` if it can.

        Args:
            now: the instant of the redemption attempt (timezone-aware).
            cart_has_order_discount: whether an order-level discount is already on the
                cart. Shopify drops a non-combining code in that situation rather than
                erroring, which is the ``CONFLICTS_WITH_EXISTING_DISCOUNT`` case.

        Returns:
            The reason, or ``None`` when the code applies.
        """
        if self.starts_at is not None and now < self.starts_at:
            return RejectionReason.NOT_YET_ACTIVE
        if self.ends_at is not None and now >= self.ends_at:
            return RejectionReason.EXPIRED
        if self.usage_limit is not None and self.usage_count >= self.usage_limit:
            return RejectionReason.USAGE_LIMIT_REACHED
        if cart_has_order_discount and not self.combines_with.order_discounts:
            return RejectionReason.CONFLICTS_WITH_EXISTING_DISCOUNT
        return None

    def is_redeemable_at(
        self,
        now: datetime,
        *,
        cart_has_order_discount: bool = False,
    ) -> bool:
        """Whether this code applies right now — the boolean form of :meth:`rejection`.

        Args:
            now: the instant of the redemption attempt (timezone-aware).
            cart_has_order_discount: whether an order-level discount is already on the cart,
                exactly as :meth:`rejection` means it.

        Returns:
            ``True`` when :meth:`rejection` returns ``None`` for the same arguments.

        ``cart_has_order_discount`` is forwarded rather than pinned to ``False`` (T-255).
        It used to be pinned, with no parameter for it and a docstring calling that "the
        common case" — but it is not the case any live caller is in: BOTH redemption sites
        (``app.py``'s ``_apply_discount_code`` and ``orders.py``'s re-validation at payment)
        pass ``state.config.has_active_automatic_discount``. A caller reaching for the
        shorter spelling therefore got ``True`` for a cart the redemption path rejects with
        ``CONFLICTS_WITH_EXISTING_DISCOUNT``, silently, and there was no way to ask this
        method the question the platform actually asks. There is now, and the two can no
        longer disagree for the same inputs.
        """
        return self.rejection(now=now, cart_has_order_discount=cart_has_order_discount) is None


def utc_now() -> datetime:
    """The stub's single wall-clock read.

    One function so a test can freeze time (``frozen_clock``) or the control plane can
    override it, without every module reaching for :func:`datetime.now` independently.

    That sentence was aspirational until T-253 and is now enforced: this is the ONLY
    ``datetime.now`` in ``services/shopify-stub/src``, and :meth:`shopify_stub.state.
    StubState.now` — the clock every live read in the stub goes through — delegates here
    through the module attribute, so overriding this name really does move the stub's clock.
    Anything added later that reads the wall clock for itself puts the claim back in the
    state this ticket found it in.
    """
    return datetime.now(UTC)


def discount_amount_for(discount: DiscountCode | None, subtotal: Decimal) -> Decimal:
    """What ``discount`` takes off a cart worth ``subtotal``.

    **There is exactly one implementation of this arithmetic, and this is it.** It exists
    because there were briefly two: the cart route quantized with Python's default
    ``ROUND_HALF_EVEN`` while order creation used ``ROUND_HALF_UP``, so a 12.345% discount on
    a 100.00 line quoted the shopper 87.66 in the cart and charged 87.65 on the order. Every
    test used round numbers, so nothing caught it. A shopper being charged a different number
    from the one they were quoted is the worst class of bug this stub could teach a consumer
    to tolerate, and the fix is structural: both call sites now call this, so they cannot
    drift apart again.

    ``ROUND_HALF_UP`` matches :func:`shopify_stub.state.money`, which is what every amount is
    finally rendered with, so the stored decimal and the wire string agree.

    Args:
        discount: the code being applied, or ``None`` for no discount.
        subtotal: the cart or line subtotal.

    Returns:
        The amount to take off, never more than ``subtotal`` and never negative.
    """
    if discount is None or subtotal <= 0:
        return Decimal("0")
    if discount.percentage is not None:
        amount = (subtotal * Decimal(str(discount.percentage))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    elif discount.fixed_amount is not None:
        amount = Decimal(discount.fixed_amount)
    else:
        return Decimal("0")
    return min(max(amount, Decimal("0")), subtotal)
