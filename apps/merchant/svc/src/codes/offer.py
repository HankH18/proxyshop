"""Reading a winning offer's code-shaping fields — and refusing it *before* anything exists.

Everything here runs ahead of the mint. That order is the whole point: a malformed
``expires_at`` discovered *after* ``discountCodeBasicCreate`` has returned leaves a live,
single-use discount loose in the merchant's Shopify account with no record of it and no way
to expire it. ``apps/exchange/src/checkout/codes.assert_offer_is_mintable`` exists for the
same reason on the exchange's side, and T-157/T-202 are what happens when the check is
missing. :func:`assert_offer_is_mintable` is this package's copy of that discipline.

**Two offer spellings, both real.** ``packages/contracts`` types an ``Offer`` with a nested
``discount: {type, value}`` and a ``variant_ref``; the accepted-offer body the exchange
actually POSTs to ``/codes`` is flatter — ``discount_pct``, ``discount_type``,
``variant_id``. Neither is going to be renamed by this ticket, so every reader below takes
both and says which it took. Guessing between them is not on the table: the unit of a
discount is money, and ``apps/exchange/src/checkout/discounts.py`` documents at length why
a ``0.2`` arriving where a ``20.0`` belongs must be refused rather than repaired.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from contracts.boundary import parse_timestamp

# T-203, recorded as a binding requirement on this ticket: the percent-vs-fraction
# conversion has exactly one implementation in this repository and it lives here. Nothing
# in `.importlinter` forbids `merchant_svc -> exchange`; the contracts that exist run the
# other way (C3/S7 stops the exchange reading sealed merchant state). Importing a converter
# is not importing exchange logic — see this ticket's `non_goals`.
from exchange.checkout.discounts import UnusableDiscount, shopify_discount_percentage
from merchant_svc.install.shop import InvalidShopDomain, normalize_shop_domain

__all__ = [
    "FIXED_AMOUNT_DISCOUNT_TYPES",
    "MAX_CODE_LIFETIME",
    "MAX_DISCOUNT_PERCENT",
    "PERCENTAGE_DISCOUNT_TYPES",
    "OffDomainOffer",
    "OfferDiscount",
    "UnusableOffer",
    "assert_discount_matches_prices",
    "assert_offer_is_mintable",
    "code_expiry",
    "offer_discount",
    "offer_expires_at",
    "offer_quantity",
    "offer_variant_gid",
    "offer_variant_id",
    "parse_instant",
    "shop_domain_for",
]


class UnusableOffer(ValueError):
    """An offer field a code cannot be built from — caught *before* anything is minted."""


class OffDomainOffer(UnusableOffer):
    """The offer names a checkout host that is not the shop the code would be minted on.

    C10/D22: the permalink's host is the shop's own registered domain, never a host that
    arrived in the offer. An offer whose ``checkout_url`` points somewhere else is a
    redirect the buyer would follow away from the store that agreed to the discount, so it
    is refused rather than silently corrected — a silent correction would hide a bidder
    that is trying it.
    """


#: The maximum life of a code, however far away the offer's own expiry is (D22).
MAX_CODE_LIFETIME = timedelta(hours=48)

#: ``discount.type`` spellings that mean "a share of the price". The protocol types the
#: field as a free string, so the tolerated spellings are named rather than assumed.
PERCENTAGE_DISCOUNT_TYPES: frozenset[str] = frozenset({"percentage", "percent", "pct"})

#: Spellings that mean "a sum of money". These carry no percentage at all.
FIXED_AMOUNT_DISCOUNT_TYPES: frozenset[str] = frozenset({"fixed_amount", "fixed", "amount"})

#: The protocol's own ceiling: the policy Envelope's ``max_discount_pct`` is ``maximum: 100``.
MAX_DISCOUNT_PERCENT = 100.0


def _read(offer: Any, *names: str) -> Any:
    """The first present of ``names`` on a mapping or an object, else ``None``."""
    for name in names:
        if isinstance(offer, Mapping):
            if name in offer:
                value = offer[name]
                if value is not None:
                    return value
        else:
            value = getattr(offer, name, None)
            if value is not None:
                return value
    return None


def _finite(value: Any) -> float:
    """``value`` as a float that is a real number.

    Raises:
        UnusableOffer: it is a bool, is not a number at all, or is NaN/±inf. A NaN TTL is
            the interesting one: every comparison against it is ``False``, so a naive
            ``min(cap, expiry)`` would silently return the NaN and the code's window would
            become uncomputable rather than wrong-and-visible.
    """
    if isinstance(value, bool):
        raise UnusableOffer(f"{value!r} is a boolean, not a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise UnusableOffer(f"{value!r} is not a number") from exc
    if not math.isfinite(number):
        raise UnusableOffer(f"{value!r} is not a finite number")
    return number


def parse_instant(value: Any) -> datetime:
    """One instant, in every spelling the protocol and its callers actually produce.

    The ORDER below is load-bearing and the obvious order is wrong — this is T-182's lesson
    restated where it can bite again. Trying ``float()`` first would read ``"20260903"``
    (ISO-8601 basic format, which ``datetime.fromisoformat`` accepts and the contracts
    boundary therefore admits) as the epoch second 20260903, i.e. **23 August 1970**: an
    offer that validated at the door with a future expiry would mint a code that expired
    fifty-six years ago. So the boundary's own parser goes first for anything that is not
    already a number, and ``float()`` is only the fallback that keeps bare numeric strings
    working.

    Raises:
        UnusableOffer: the value is not an instant anything here can read, or it is a naive
            datetime. Naive is refused rather than assumed-UTC: assuming shifts the expiry
            by the host's offset, a bug that appears only outside UTC.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise UnusableOffer(f"{value!r} is a naive datetime; an instant needs a timezone")
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = _finite(value)
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise UnusableOffer(f"{value!r} is not an instant this platform can represent") from exc
    parsed = parse_timestamp(value)
    if parsed is not None:
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    try:
        seconds = _finite(value)
    except UnusableOffer as exc:
        raise UnusableOffer(
            f"{value!r} is neither an epoch number nor an RFC-3339 instant, so a code's "
            f"D22 expiry cannot be computed from it"
        ) from exc
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise UnusableOffer(f"{value!r} is not an instant this platform can represent") from exc


def offer_expires_at(offer: Any) -> datetime | None:
    """The offer's own expiry, or ``None`` when it declares none."""
    raw = _read(offer, "expires_at", "expiresAt", "expiry")
    return None if raw is None else parse_instant(raw)


def code_expiry(now: datetime, offer: Any = None) -> datetime:
    """D22: the code dies at ``min(now + 48h, offer.expires_at)``.

    A code outliving the offer it discounts is a discount the seller never agreed to.

    Raises:
        UnusableOffer: ``now`` is naive, or the offer's expiry is unreadable, or the window
            this produces is empty. An empty window is the case worth naming: an offer that
            expired before the buyer accepted it would otherwise mint a code whose
            ``endsAt`` is at or before its ``startsAt``. Shopify refuses that outright, and
            an implementation that only found out at the Admin API would have refused
            *after* the mint rather than before it.
    """
    if now.tzinfo is None:
        raise UnusableOffer("`now` must be timezone-aware; a naive instant shifts the expiry")
    cap = now + MAX_CODE_LIFETIME
    expires_at = offer_expires_at(offer)
    ends_at = cap if expires_at is None else min(cap, expires_at)
    if ends_at <= now:
        raise UnusableOffer(
            f"the offer's expiry {expires_at.isoformat() if expires_at else '-'} is not after "
            f"{now.isoformat()}, so the code's validity window would be empty"
        )
    return ends_at


def offer_quantity(offer: Any = None) -> int:
    """The permalink's quantity: a positive whole number, or 1 when the offer does not say.

    Raises:
        UnusableOffer: ``quantity`` is present but is not a positive whole number.
    """
    quantity = _read(offer, "quantity", "qty")
    if quantity is None or quantity == "":
        return 1
    try:
        value = int(str(quantity).strip())
    except (TypeError, ValueError) as exc:
        raise UnusableOffer(f"offer quantity {quantity!r} is not a whole number") from exc
    if value < 1:
        raise UnusableOffer(f"offer quantity {quantity!r} is not a positive quantity")
    return value


def offer_variant_id(offer: Any = None) -> str | None:
    """The numeric variant id a cart permalink addresses, or ``None`` for an order discount.

    Shopify's cart permalink takes the **numeric** variant id, while the protocol and the
    Admin API pass GIDs (``gid://shopify/ProductVariant/1001``). The trailing segment of a
    GID is that number, so one is read out of the other rather than the GID being pasted
    into a URL that would 404.
    """
    raw = _read(offer, "variant_id", "variant_ref", "variantId", "variant")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    tail = text.rsplit("/", 1)[-1]
    if not tail:
        raise UnusableOffer(f"offer variant {raw!r} names no variant id")
    return tail


def offer_variant_gid(offer: Any = None) -> str | None:
    """The variant as the Admin API addresses it — a GID — or ``None`` for an order discount.

    The mirror of :func:`offer_variant_id`, which produces the *numeric* form the cart
    permalink needs. Both spellings are derived from the one field so the mutation and the
    URL cannot end up naming different variants.
    """
    raw = _read(offer, "variant_id", "variant_ref", "variantId", "variant")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.startswith("gid://"):
        return text
    return f"gid://shopify/ProductVariant/{text}"


@dataclass(frozen=True)
class OfferDiscount:
    """What the offer promised, in the protocol's own units.

    Attributes:
        type: the discount type as the offer spelled it, normalised to lower case.
        percent: percentage points (``10.0`` means 10% off), or ``None`` for a money amount.
        amount: a fixed money amount off, or ``None`` for a percentage.
        currency: the currency ``amount`` is denominated in, when there is one.
    """

    type: str = "percentage"
    percent: float | None = None
    amount: float | None = None
    currency: str | None = None

    @property
    def shopify_percentage(self) -> float | None:
        """The same discount as the fraction ``customerGets.value.percentage`` takes.

        Shopify's input is a fraction in ``[0.0, 1.0]`` — its recorded schema says "Value
        must be between 0.00 - 1.00" against the INPUT object — while the protocol's unit is
        percentage points. **The conversion is delegated, not rewritten** (T-203): the one
        blessed converter is ``exchange.checkout.discounts.shopify_discount_percentage``,
        and until this package called it that converter had no production caller at all, so
        its tests proved it divides by 100 rather than that anything in the product does.
        A second local ``/ 100`` here would be exactly the drift it exists to prevent — one
        of the two would eventually be written in the wrong direction, and that is a
        two-orders-of-magnitude error at the point money is decided.

        Raises:
            UnusableOffer: the converter refuses the unit. Re-raised as this package's own
                error so a caller has one exception family to catch.
        """
        if self.percent is None:
            return None
        try:
            return shopify_discount_percentage({"type": self.type, "value": self.percent})
        except UnusableDiscount as exc:
            raise UnusableOffer(str(exc)) from exc


def offer_discount(offer: Any) -> OfferDiscount:
    """The discount the offer promised, read from either offer spelling.

    Raises:
        UnusableOffer: the discount's type is one this module cannot place, its value is
            not a number in ``[0, 100]`` percentage points, or the offer names no discount
            at all. All three are refusals rather than corrections: a discount code that
            discounts nothing is not a thing worth minting, and a value whose unit cannot
            be established must never be guessed.
    """
    nested = _read(offer, "discount")
    raw_type = _read(offer, "discount_type", "discountType")
    raw_value: Any = _read(offer, "discount_pct", "discountPct", "discount_percent")
    if isinstance(nested, Mapping) or (nested is not None and not isinstance(nested, (int, float))):
        raw_type = _read(nested, "type") if _read(nested, "type") is not None else raw_type
        nested_value = _read(nested, "value")
        raw_value = nested_value if nested_value is not None else raw_value

    if raw_value is None:
        raw_value = _read(offer, "discount_amount", "discountAmount")
        if raw_value is not None and raw_type is None:
            raw_type = "fixed_amount"

    kind = str(raw_type).strip().lower() if raw_type is not None else "percentage"
    if kind in FIXED_AMOUNT_DISCOUNT_TYPES:
        if raw_value is None:
            raise UnusableOffer("a fixed-amount discount states no amount")
        amount = _finite(raw_value)
        if amount <= 0:
            raise UnusableOffer(f"discount amount {raw_value!r} is not a positive sum of money")
        currency = _read(offer, "currency", "currency_code", "currencyCode")
        return OfferDiscount(type=kind, amount=amount, currency=str(currency) if currency else None)

    if kind not in PERCENTAGE_DISCOUNT_TYPES:
        raise UnusableOffer(
            f"discount type {raw_type!r} is neither a percentage "
            f"({sorted(PERCENTAGE_DISCOUNT_TYPES)}) nor a fixed amount "
            f"({sorted(FIXED_AMOUNT_DISCOUNT_TYPES)}), so its unit cannot be established"
        )
    if raw_value is None:
        raise UnusableOffer("the offer names no discount, so there is nothing to mint a code for")
    percent = _finite(raw_value)
    if not 0.0 < percent <= MAX_DISCOUNT_PERCENT:
        raise UnusableOffer(
            f"discount value {percent!r} is outside 0..{MAX_DISCOUNT_PERCENT:g} percentage "
            f"points; if it was meant as a 0.0-1.0 fraction it is already in Shopify's unit "
            f"and must not be converted twice"
        )
    return OfferDiscount(type=kind, percent=percent)


def _money(value: Any) -> Decimal | None:
    """A price as an exact decimal, or ``None`` when the offer does not state a usable one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return amount


def assert_discount_matches_prices(offer: Any, discount: OfferDiscount) -> None:
    """Refuse a percentage that the offer's own two prices contradict.

    **This is the hole T-203 named, closed with the one piece of evidence the exchange did
    not have.** ``contracts.Discount`` constrains no unit — the value is a bare float with
    no bounds and no unit field — so a ``0.2`` meant as "20%" is indistinguishable from a
    legal 0.2% discount, and the converter's ``[0, 100]`` guard cannot catch it. The
    merchant, though, is handed ``list_price`` *and* ``unit_price`` on the same offer, and
    those two numbers say what the percentage was supposed to be. A 10.0 against 100.00 and
    90.00 checks out; a 0.2 against the same pair implies 99.80 and does not.

    Silent about offers that state only one price, because then there is nothing to check —
    and deliberately generous about rounding (a cent, plus half a percent of the list
    price), because catching a two-orders-of-magnitude unit error does not require
    reproducing Shopify's rounding to the cent.

    Raises:
        UnusableOffer: the stated prices and the stated percentage disagree by more than
            rounding can explain.
    """
    if discount.percent is None:
        return
    list_price = _money(_read(offer, "list_price", "listPrice", "compare_at_price"))
    unit_price = _money(_read(offer, "unit_price", "unitPrice"))
    if list_price is None or unit_price is None:
        return
    expected = list_price * (Decimal(100) - Decimal(str(discount.percent))) / Decimal(100)
    tolerance = Decimal("0.01") + list_price * Decimal("0.005")
    if abs(expected - unit_price) <= tolerance:
        return
    implied = (Decimal(100) * (list_price - unit_price) / list_price).quantize(Decimal("0.001"))
    raise UnusableOffer(
        f"the offer's own prices contradict its discount: {list_price} list and "
        f"{unit_price} unit imply {implied}%, but the offer states "
        f"{discount.percent!r} percentage points. A percentage whose unit the two prices "
        f"disagree with is refused rather than converted — see T-203"
    )


def _host_of(url: Any) -> str | None:
    """The host of a URL-shaped string, or ``None`` when it names none."""
    if not isinstance(url, str) or not url.strip():
        return None
    host = urlsplit(url.strip()).hostname
    return host or None


def shop_domain_for(store_id: str, offer: Any = None) -> str:
    """The ``<name>.myshopify.com`` host this store's permalink is built on.

    The host comes from the STORE, never from the offer: an offer is a bidder's own JSON,
    and a permalink host taken from it is an open redirect wearing a discount code. The
    offer's ``checkout_url`` is still read — but only to be *compared*, so an offer that
    names a different shop is refused rather than quietly redirected to the right one. A
    mismatch means the two halves of the system disagree about which store just sold
    something, and that is worth a refusal.

    Raises:
        UnusableOffer: ``store_id`` names no shop host that could exist.
        OffDomainOffer: the offer names a checkout host that is not this store's.
    """
    raw = str(store_id or "").strip()
    if not raw:
        raise UnusableOffer("store_id is empty, so there is no shop to mint a code on")
    candidate = raw if raw.lower().endswith(".myshopify.com") else f"{raw}.myshopify.com"
    try:
        shop = normalize_shop_domain(candidate)
    except InvalidShopDomain as exc:
        raise UnusableOffer(f"store {store_id!r} names no usable shop host: {exc}") from exc

    declared = _host_of(_read(offer, "checkout_url", "checkoutUrl", "permalink_url", "url"))
    if declared is None:
        declared = _host_of(f"https://{_read(offer, 'shop_domain', 'shopDomain', 'shop') or ''}")
    if declared is not None and declared.lower() != shop:
        raise OffDomainOffer(
            f"the offer's checkout host {declared!r} is not store {store_id!r}'s shop "
            f"({shop}); a permalink is built on the store's own domain (C10/D22)"
        )
    return shop


def assert_offer_is_mintable(store_id: str, offer: Any, *, now: datetime) -> None:
    """Refuse an offer whose code-shaping fields will not parse, before a code exists.

    Every reader this package needs is run here, in one place, ahead of the mint. The order
    is the whole point — see the module docstring.

    Raises:
        UnusableOffer: any of the fields is unreadable; the message names which.
    """
    shop_domain_for(store_id, offer)
    code_expiry(now, offer)
    offer_quantity(offer)
    offer_variant_id(offer)
    discount = offer_discount(offer)
    assert_discount_matches_prices(offer, discount)
    # Runs the T-203 converter here too, so a unit it refuses is refused BEFORE the mint
    # rather than while the mutation variables are being built, which is after.
    if discount.shopify_percentage is None and discount.amount is None:
        raise UnusableOffer("the offer's discount states neither a percentage nor an amount")
