"""Discount codes: minting one, validating it before it exists, and judging its redemption.

R3/A5/C5 — a winning offer becomes a **single-use** discount code with a **bounded** expiry
and a cart permalink, a conflicting ``combinesWith`` configuration is caught **before** any
permalink is handed out, and a duplicate redemption is an offer-integrity **event**, not a
crash. Those three sentences are the whole package, and each one is a module:

``mint``
    The D22 code shape and the one place a code comes into existence. It takes no offer
    identifier at all, which is what makes "not guessable" mechanical rather than promised.
``offer``
    Every code-shaping field read off the accepted offer, and refused there — ahead of the
    mint, so a malformed field can never strand a live discount.
``combines``
    The shop's live discount configuration, and the three-valued verdict on whether this
    offer's code can actually be honoured. ``UNKNOWN`` is a first-class answer.
``create``
    The order of operations, which is the specification: refuse, check, mint, create,
    permalink, record.
``redemption``
    Single use, enforced in one critical section, with a second redemption recorded as an
    ``offer_integrity`` event instead of raised.
``ledger``
    The producing boundary. Every event is validated against its published body before it
    exists.
``permalink``
    The D22 cart URL, built in one place so the stub parser and the exchange's builder
    cannot drift from it.
``routes``
    ``POST /codes``, mounted by the frozen ``merchant_svc.main.create_app``.

The two entry points the rest of the system uses are :func:`create_code` — or
:class:`CodeCreator`, the two-argument object T-036's ``CheckoutProvider`` port injects —
and :func:`on_redemption`.
"""

from __future__ import annotations

from ._spellings import bind_package
from .combines import (
    CombinesWithPolicy,
    CombinesWithVerdict,
    DiscountClass,
    ShopConfigurationUnavailable,
    ShopDiscountConfiguration,
    combines_with_verdict,
    read_shop_discount_configuration,
)
from .create import (
    DISCOUNT_CODE_BASIC_CREATE,
    USAGE_LIMIT,
    CodeCreationRefused,
    CodeCreator,
    CombinesWithConflict,
    CreatedCode,
    DiscountCodeRefused,
    create_code,
)
from .ledger import (
    CODE_LEDGER,
    CodeLedger,
    MalformedLedgerPayload,
    UnknownLedgerEventKind,
    build_event,
)
from .mint import (
    CODE_ALPHABET,
    CODE_BODY_LENGTH,
    CODE_PREFIX,
    canonical_code,
    is_well_formed,
    mint_code,
)
from .offer import (
    MAX_CODE_LIFETIME,
    OffDomainOffer,
    OfferDiscount,
    UnusableOffer,
    assert_offer_is_mintable,
    code_expiry,
    offer_discount,
)
from .permalink import build_cart_permalink
from .redemption import (
    REDEMPTIONS,
    IssuedCode,
    Redemption,
    RedemptionOutcome,
    RedemptionRegister,
    on_redemption,
)

__all__ = [
    "CODE_ALPHABET",
    "CODE_BODY_LENGTH",
    "CODE_LEDGER",
    "CODE_PREFIX",
    "DISCOUNT_CODE_BASIC_CREATE",
    "MAX_CODE_LIFETIME",
    "REDEMPTIONS",
    "USAGE_LIMIT",
    "CodeCreationRefused",
    "CodeCreator",
    "CodeLedger",
    "CombinesWithConflict",
    "CombinesWithPolicy",
    "CombinesWithVerdict",
    "CreatedCode",
    "DiscountClass",
    "DiscountCodeRefused",
    "IssuedCode",
    "MalformedLedgerPayload",
    "OffDomainOffer",
    "OfferDiscount",
    "Redemption",
    "RedemptionOutcome",
    "RedemptionRegister",
    "ShopConfigurationUnavailable",
    "ShopDiscountConfiguration",
    "UnknownLedgerEventKind",
    "UnusableOffer",
    "assert_offer_is_mintable",
    "build_cart_permalink",
    "build_event",
    "canonical_code",
    "code_expiry",
    "combines_with_verdict",
    "create_code",
    "is_well_formed",
    "mint_code",
    "offer_discount",
    "on_redemption",
    "read_shop_discount_configuration",
]

# LAST, and it is not decoration: this tree is importable as `merchant_svc.codes` and as
# `apps.merchant.svc.src.codes`, and without this Python executes every file here TWICE —
# once per spelling — leaving TWO redemption registers, so "a minted code may be redeemed
# once" would hold only per spelling. A pytest session running the frozen acceptance suite
# (`apps.merchant.svc.src.codes`) beside the FastAPI app (`merchant_svc.codes`) is exactly
# such a process. Measured on this worktree before this existed:
# `apps.merchant.svc.src.envelope is merchant_svc.envelope` was False. See `_spellings.py`.
bind_package(__name__)
