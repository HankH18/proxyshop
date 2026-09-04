"""``POST /codes``: one accepted offer becomes one single-use code and one permalink.

The order of operations here **is** the specification, and every step before the mint is
there because doing it afterwards would be doing it too late:

1. read every code-shaping field off the offer and refuse an unreadable one
   (:func:`~merchant_svc.codes.offer.assert_offer_is_mintable`);
2. ask the shop what discounts it is already running and refuse a ``combinesWith`` conflict
   (:mod:`merchant_svc.codes.combines`) — R3's "detected before redirect", which means
   detected while a refusal is still possible, not detected in time to apologise;
3. **only then** mint (:func:`~merchant_svc.codes.mint.mint_code`);
4. create the discount with ``usageLimit: 1`` and a window of at most 48 hours anchored at
   the instant this function was *given*, never at the machine clock;
5. build the permalink, file the code in the redemption register, and record a
   ``code_created`` event carrying its **published** body.

**Steps 3 through 5 are guarded, and the guard is not decoration.** From the moment
:func:`mint_code` returns, a real single-use discount is about to exist in the merchant's
Shopify account; an exception anywhere after that would strand it, with no record of the
code and nothing able to expire it. That is T-157/T-202, measured on the exchange's side of
the same flow. So the region is wrapped, and a failure inside it still records a
``code_created`` event flagged ``orphaned`` before re-raising — the same shape
``apps/exchange/src/accept/offer.py`` uses, for the same reason.

**Two callers, two signatures, one implementation.** The frozen acceptance suite calls the
module function with the Admin client as a third positional argument. T-036's
``CheckoutProvider`` port calls an injected object as ``creator.create_code(store_id,
offer)`` — two arguments, because the port has no Admin client to give. :class:`CodeCreator`
is that object: it binds a client and forwards. Ticket acceptance criterion 4 is what this
serves — the merchant is *one implementation behind the port*, substitutable for
``SimulatedRedirectProvider`` without any caller changing and without a new LedgerEvent kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from contracts.protocol import LedgerEvent

from .combines import (
    CombinesWithVerdict,
    DiscountClass,
    ShopConfigurationUnavailable,
    ShopDiscountConfiguration,
    combines_with_verdict,
    read_shop_discount_configuration,
)
from .ledger import CODE_LEDGER, CodeLedger, build_event
from .mint import RandomSource, canonical_code, mint_code
from .offer import (
    UnusableOffer,
    assert_discount_matches_prices,
    assert_offer_is_mintable,
    code_expiry,
    offer_discount,
    offer_quantity,
    offer_variant_gid,
    offer_variant_id,
    shop_domain_for,
)
from .permalink import build_cart_permalink
from .redemption import REDEMPTIONS, IssuedCode, RedemptionRegister

__all__ = [
    "DISCOUNT_CODE_BASIC_CREATE",
    "USAGE_LIMIT",
    "CodeCreationRefused",
    "CodeCreator",
    "CombinesWithConflict",
    "CreatedCode",
    "create_code",
]

#: D22: single use. Named rather than inlined so the register checks a redemption against
#: what was promised, not against a literal that could drift away from the mutation.
USAGE_LIMIT = 1

#: The Admin GraphQL mutation, written as a real caller writes one — a named operation with
#: a typed variable and an explicit selection set. The selection is deliberately minimal:
#: it must NOT name ``usageLimit``, ``startsAt`` or ``endsAt``, because those values belong
#: in the arguments and a reader (human or otherwise) that finds them in the selection set
#: cannot tell which occurrence is the value being sent.
DISCOUNT_CODE_BASIC_CREATE = """
mutation MintOfferDiscountCode($basicCodeDiscount: DiscountCodeBasicInput!) {
  discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
    codeDiscountNode { id }
    userErrors { field message code }
  }
}
"""

#: What our own code declares it combines with. All three, on purpose: we want the buyer to
#: keep whatever the shop is already giving them. Whether that actually happens is the
#: shop's decision, which is what step 2 goes and reads.
CODE_COMBINES_WITH: Mapping[str, bool] = {
    "orderDiscounts": True,
    "productDiscounts": True,
    "shippingDiscounts": True,
}


class CodeCreationRefused(RuntimeError):
    """No code was created, and none exists. The message says why."""


class CombinesWithConflict(CodeCreationRefused):
    """The shop's live discounts will not combine with the one this offer promised.

    Refused rather than repriced. Repricing would mean handing the buyer a permalink for a
    price nobody agreed to: the merchant's envelope authorised *this* discount, and the
    buyer was shown *this* price. There is also nothing to reprice *to* — the Admin API
    tells us that an automatic discount exists and that it does not combine, not what it is
    worth, so any new number would be a guess presented as a promise.
    """


class DiscountCodeRefused(CodeCreationRefused):
    """The Admin API refused the ``discountCodeBasicCreate``, with ``userErrors``."""


@dataclass(frozen=True)
class CreatedCode:
    """One minted code and everything a caller or an auditor needs about it.

    Field names are the ones the pinned merchant contract uses (``code``,
    ``permalink_url``, ``expires_at``) so that the 201 body is a projection of this record
    rather than a translation of it.
    """

    code: str
    permalink_url: str
    expires_at: str
    starts_at: str
    store_id: str
    usage_limit: int = USAGE_LIMIT
    discount_pct: float | None = None
    offer_id: str | None = None
    bid_ref: str | None = None
    variant_id: str | None = None
    quantity: int = 1
    event: LedgerEvent | None = None

    def to_contract(self) -> dict[str, Any]:
        """Exactly the three keys ``POST /codes``' pinned 201 response declares."""
        return {
            "code": self.code,
            "permalink_url": self.permalink_url,
            "expires_at": self.expires_at,
        }

    def to_dict(self) -> dict[str, Any]:
        """The whole record, JSON-able, for logs and tests."""
        return {
            "code": self.code,
            "permalink_url": self.permalink_url,
            "expires_at": self.expires_at,
            "starts_at": self.starts_at,
            "store_id": self.store_id,
            "usage_limit": self.usage_limit,
            "discount_pct": self.discount_pct,
            "offer_id": self.offer_id,
            "bid_ref": self.bid_ref,
            "variant_id": self.variant_id,
            "quantity": self.quantity,
            "event": None if self.event is None else self.event.model_dump(mode="json"),
        }


def _text(offer: Any, *names: str) -> str | None:
    for name in names:
        value = offer.get(name) if isinstance(offer, Mapping) else getattr(offer, name, None)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
    return None


def _user_errors(response: Any) -> list[Any]:
    """The mutation's ``userErrors``, read strictly.

    Strictly, because a tolerant test double answers every key with a truthy mapping — so
    "is there an error" must be "is there a non-empty **list** under a key that is really
    there", never a truthiness test on whatever ``get`` decided to invent.
    """
    node: Any = response
    for _ in range(4):
        if not isinstance(node, Mapping):
            return []
        for key in ("userErrors", "user_errors"):
            if key in node:
                value = node[key]
                return list(value) if isinstance(value, list) else []
        for key in ("data", "discountCodeBasicCreate"):
            if key in node:
                node = node[key]
                break
        else:
            return []
    return []


def _mutation_variables(
    *,
    code: str,
    starts_at: datetime,
    ends_at: datetime,
    offer: Any,
    offer_id: str | None,
) -> dict[str, Any]:
    """The ``DiscountCodeBasicInput`` this service sends, built once and in one place."""
    discount = offer_discount(offer)
    variant_gid = offer_variant_gid(offer)
    percentage = discount.shopify_percentage

    value: dict[str, Any]
    if percentage is not None:
        value = {"percentage": percentage}
    else:
        value = {
            "discountAmount": {"amount": discount.amount, "appliesOnEachItem": False},
        }

    items: dict[str, Any]
    if variant_gid is None:
        items = {"all": True}
    else:
        items = {"products": {"productVariantsToAdd": [variant_gid]}}

    basic: dict[str, Any] = {
        "code": code,
        "title": f"ProxyShop {code}",
        "startsAt": starts_at.isoformat(),
        "endsAt": ends_at.isoformat(),
        "usageLimit": USAGE_LIMIT,
        "appliesOncePerCustomer": True,
        "customerSelection": {"all": True},
        "customerGets": {"value": value, "items": items},
        "combinesWith": dict(CODE_COMBINES_WITH),
    }
    if offer_id:
        # The stub reads the offer id back off this tag, which is what lets an offline run
        # assert "one code per accepted offer" without a second index.
        basic["tags"] = [f"offer:{offer_id}"]
    return {"basicCodeDiscount": basic}


def create_code(
    store_id: str,
    offer: Any,
    client: Any,
    *,
    now: datetime | None = None,
    rng: RandomSource | None = None,
    shop_config: ShopDiscountConfiguration | None = None,
    register: RedemptionRegister | None = None,
    ledger: CodeLedger | None = None,
) -> CreatedCode:
    """Mint one single-use discount code for one accepted offer.

    Args:
        store_id: the shop the code is minted on. The permalink's host is derived from it.
        offer: the accepted offer, in either the protocol's nested shape or the flat
            ``discount_pct``/``variant_id`` shape the exchange posts.
        client: the Admin GraphQL client — anything exposing
            ``execute(document, variables)``.
        now: the reference instant. **The validity window is anchored here**, so a caller
            that is given a reference instant gets the same window on every machine at
            every moment. Left unset, the wall clock is read.
        rng: a random source for the code body. Tests only; production draws from
            :mod:`secrets` so a code is not derivable from anything public.
        shop_config: a pre-read shop discount configuration, for a deployment whose Admin
            surface cannot answer the query (the offline stub is one). Supplying it skips
            the read; it never skips the *decision*.
        register: the redemption register to file the code in. Defaults to the
            service-wide one, which is what makes the code single-use.
        ledger: where the ``code_created`` event goes. Defaults to the service-wide one.

    Returns:
        A :class:`CreatedCode`.

    Raises:
        UnusableOffer: an offer field a code cannot be built from. Nothing was minted.
        CombinesWithConflict: the shop's live discounts refuse to combine with this one.
            Nothing was minted, and no permalink exists — which is R3's whole point.
        ShopConfigurationUnavailable: the shop's configuration could not be read, so no
            conflict claim is possible either way. Nothing was minted. Distinct from a
            conflict on purpose: this one is retryable and that one is not.
        DiscountCodeRefused: the Admin API refused the mutation.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise UnusableOffer("`now` must be timezone-aware; a naive instant shifts the expiry")

    # ---- before the mint: everything that can refuse ---------------------------------
    assert_offer_is_mintable(store_id, offer, now=moment)
    shop_domain = shop_domain_for(store_id, offer)
    ends_at = code_expiry(moment, offer)
    quantity = offer_quantity(offer)
    variant_id = offer_variant_id(offer)
    variant_gid = offer_variant_gid(offer)
    discount = offer_discount(offer)
    assert_discount_matches_prices(offer, discount)
    offer_id = _text(offer, "offer_id", "offerId", "id")
    bid_ref = _text(offer, "bid_ref", "bidRef", "bid_id")
    auction_id = _text(offer, "auction_id", "auctionId")

    config = shop_config if shop_config is not None else read_shop_discount_configuration(client)
    discount_class = DiscountClass.PRODUCT if variant_gid is not None else DiscountClass.ORDER
    verdict = combines_with_verdict(config, discount_class)
    if verdict is CombinesWithVerdict.CONFLICT:
        _record_conflict(
            store_id=store_id,
            offer_id=offer_id,
            bid_ref=bid_ref,
            auction_id=auction_id,
            discount=discount,
            config=config,
            discount_class=discount_class,
            moment=moment,
            ledger=ledger if ledger is not None else CODE_LEDGER,
        )
        raise CombinesWithConflict(
            f"store {store_id!r} is running {len(config.automatic_discounts) or 1} automatic "
            f"discount(s) that do not combine with a {discount_class.value} discount, so the "
            f"{discount.percent!r}% this offer promised would not be what the buyer pays; "
            f"refusing before a permalink exists (R3). Shop configuration: {config.describe()}"
        )
    if verdict is CombinesWithVerdict.UNKNOWN:
        raise ShopConfigurationUnavailable(
            f"store {store_id!r} did not state whether its discounts combine with a "
            f"{discount_class.value} discount, so whether this offer's code would be "
            f"honoured is unknown; refusing rather than assuming. Shop configuration: "
            f"{config.describe()}"
        )

    # ---- the mint, and the guarded region after it ------------------------------------
    sink = ledger if ledger is not None else CODE_LEDGER
    code = mint_code(rng=rng)
    permalink_url = ""
    try:
        response = _execute(client, code=code, starts_at=moment, ends_at=ends_at, offer=offer)
        errors = _user_errors(response)
        if errors:
            raise DiscountCodeRefused(
                f"the Admin API refused discountCodeBasicCreate for {code!r}: {errors!r}"
            )
        permalink_url = build_cart_permalink(
            shop_domain=shop_domain,
            code=code,
            variant_id=variant_id,
            quantity=quantity,
        )
        created = CreatedCode(
            code=code,
            permalink_url=permalink_url,
            expires_at=ends_at.isoformat(),
            starts_at=moment.isoformat(),
            store_id=store_id,
            usage_limit=USAGE_LIMIT,
            discount_pct=discount.percent,
            offer_id=offer_id,
            bid_ref=bid_ref,
            variant_id=variant_id,
            quantity=quantity,
        )
        (register if register is not None else REDEMPTIONS).issue(
            IssuedCode(
                code=code,
                key=canonical_code(code),
                store_id=store_id,
                usage_limit=USAGE_LIMIT,
                starts_at=moment,
                expires_at=ends_at,
                offer_id=offer_id,
                bid_ref=bid_ref,
                permalink_url=permalink_url,
            )
        )
        created = replace(
            created,
            event=sink.emit(
                build_event(
                    "code_created",
                    auction_id=auction_id,
                    store_id=store_id,
                    ts=moment,
                    payload={
                        # The published `code_created` body (D24). All three keys, spelled
                        # the way `LEDGER_PAYLOAD_SHAPES` publishes them — see this
                        # package's `ledger` module for the measured case where they were
                        # not.
                        "code": code,
                        "permalink_url": permalink_url,
                        "expires_at": ends_at.isoformat(),
                        # ...and what makes it reconcilable.
                        "offer_id": offer_id,
                        "bid_ref": bid_ref,
                        "usage_limit": USAGE_LIMIT,
                        "starts_at": moment.isoformat(),
                        "combines_with": dict(CODE_COMBINES_WITH),
                        "minted_by": "merchant",
                    },
                )
            ),
        )
    except Exception:
        # A real discount may already exist in the merchant's account, and the caller is
        # about to see an exception that says nothing about it. Record it, flagged, so a
        # reconciler can see a code that was created and never handed out — the T-202
        # lesson, applied on this side of the same flow.
        sink.emit(
            build_event(
                "code_created",
                auction_id=auction_id,
                store_id=store_id,
                ts=moment,
                payload={
                    "code": code,
                    "permalink_url": permalink_url,
                    "expires_at": ends_at.isoformat(),
                    "orphaned": True,
                    "revocation_required": True,
                    "offer_id": offer_id,
                    "bid_ref": bid_ref,
                    "minted_by": "merchant",
                },
            )
        )
        raise
    return created


def _execute(client: Any, *, code: str, starts_at: datetime, ends_at: datetime, offer: Any) -> Any:
    """Send the mutation through whatever call shape the injected client offers.

    ``getattr`` with an explicit ``callable`` test rather than a ``hasattr`` chain: the
    acceptance suite's double answers *every* attribute, so "does it have ``execute``" is
    always yes and the question has to be "is what it gave me callable".
    """
    variables = _mutation_variables(
        code=code,
        starts_at=starts_at,
        ends_at=ends_at,
        offer=offer,
        offer_id=_text(offer, "offer_id", "offerId", "id"),
    )
    execute = getattr(client, "execute", None)
    if not callable(execute):
        execute = client if callable(client) else None
    if execute is None:
        raise CodeCreationRefused(
            f"the injected Admin client {type(client).__name__} exposes neither "
            f"execute(document, variables) nor __call__(...)"
        )
    return execute(DISCOUNT_CODE_BASIC_CREATE, variables)


def _record_conflict(
    *,
    store_id: str,
    offer_id: str | None,
    bid_ref: str | None,
    auction_id: str | None,
    discount: Any,
    config: ShopDiscountConfiguration,
    discount_class: DiscountClass,
    moment: datetime,
    ledger: CodeLedger,
) -> None:
    """Record the refusal, because a refusal nobody records is a refusal nobody can count.

    The conflict is a real, recurring property of a store's configuration — it will refuse
    every offer for that store until somebody changes the shop's automatic discounts — and
    an exception thrown into a caller's log is not a signal anyone aggregates.
    """
    ledger.emit(
        build_event(
            "offer_integrity",
            auction_id=auction_id,
            store_id=store_id,
            ts=moment,
            payload={
                # The published `offer_integrity` body (D24).
                "bid_ref": bid_ref or offer_id or store_id,
                "field": "combines_with",
                "promised": getattr(discount, "percent", None),
                "observed": config.describe(),
                # ...and what makes it actionable.
                "offer_id": offer_id,
                "discount_class": discount_class.value,
                "reason": "the shop's live automatic discounts do not combine with this one",
                "minted": False,
            },
        )
    )


class CodeCreator:
    """T-036's ``code_creator``: the two-argument object the CheckoutProvider port injects.

    The port calls ``creator.create_code(store_id, offer)`` and reads ``code`` and
    ``permalink_url`` off whatever comes back — it has no Admin client to pass and no
    reference instant to inject. This class binds those, so the merchant is *one
    implementation behind the port* and substituting ``SimulatedRedirectProvider`` for it
    changes no caller and emits no new LedgerEvent kind (acceptance criterion 4).

    The port reads the reply with ``getattr`` when it has no ``get``, so a
    :class:`CreatedCode` is returned as-is rather than flattened into a dict — the record
    keeps the expiry and the ledger event that a dict would drop on the floor.
    """

    def __init__(
        self,
        client: Any,
        *,
        clock: Any = None,
        register: RedemptionRegister | None = None,
        ledger: CodeLedger | None = None,
    ) -> None:
        self.client = client
        self._clock = clock
        self._register = register
        self._ledger = ledger

    def now(self) -> datetime:
        """The reference instant for the next code. Injectable, so a test can pin it."""
        if self._clock is None:
            return datetime.now(UTC)
        moment = self._clock() if callable(self._clock) else self._clock
        return moment if isinstance(moment, datetime) else datetime.now(UTC)

    def create_code(self, store_id: str, offer: Any) -> CreatedCode:
        """Mint a code for one offer. The port's two-argument call shape."""
        return create_code(
            store_id,
            offer,
            self.client,
            now=self.now(),
            register=self._register,
            ledger=self._ledger,
        )

    def __call__(self, store_id: str, offer: Any) -> CreatedCode:
        """The alternative shape the port accepts when there is no ``create_code``."""
        return self.create_code(store_id, offer)
