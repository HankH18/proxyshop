"""Checkout: the one port a won offer becomes a discount code and a permalink through.

R3 / A5 / C11 / D45, ticket T-036.

.. code-block:: python

    from apps.exchange.src.checkout import CheckoutRequest, resolve_provider

    provider = resolve_provider(mode)            # raises on an unregistered mode
    result = provider.checkout(CheckoutRequest(  # domain-checked, then minted
        auction_id=auction["auction_id"],
        bid_ref=bid["bid_id"],
        store_id=bid["store_id"],
        store_domain=bid["store_domain"],        # the bid's CLAIM about its own domain
        registered_domains=sellers,              # the platform's lookup — the trusted half
        offer=bid["offer"],                      # carries checkout_url — the untrusted half
        mode=mode,
        code_creator=code_creator,               # used by the Shopify adapter only
        now=auction["now"],
    ))
    result.permalink_url, result.code, result.events

``sellers`` there is a :class:`~apps.exchange.src.checkout.provider.RegisteredDomains` — the
platform's own ``store_id -> registered domain`` lookup. **Pass it.** Without it the port
falls back to ``bid["store_domain"]``, and a bid is a store's own reply: a store that writes
``store_domain: "attacker.tld"`` next to ``checkout_url: "https://attacker.tld/…"`` has
written both halves of the host comparison, so the check passes and the buyer is redirected
off-domain with a real discount code. The exact-host comparison in :mod:`.domain` is only as
trustworthy as the domain it is handed.

What the layout buys, module by module:

=================  ================================================================
:mod:`.provider`   the port: the final template method, the request/result records
:mod:`.providers`  ``SimulatedRedirectProvider`` (required) and the Shopify adapter
:mod:`.registry`   ``CHECKOUT_MODE`` -> provider; an unregistered mode **raises**
:mod:`.domain`     the exact-host check the port applies to every provider (D22/C10)
:mod:`.codes`      the ONLY place a code is minted and a permalink is built (D22)
:mod:`.discounts`  the ONLY place a protocol percent becomes a Shopify fraction (T-183)
:mod:`.lint`       the mechanical proof that no code is minted outside this package
=================  ================================================================

`accept()` (T-033) keeps its four positional parameters — ``accept(auction, bid_id,
code_creator, mode)``. ``mode`` is the selector this registry resolves, and ``code_creator``
rides on the request, so registering a further provider adds no parameter to it.
"""

from __future__ import annotations

from .codes import (
    CODE_ALPHABET,
    CODE_BODY_LENGTH,
    CODE_PREFIX,
    MAX_CODE_TTL_SECONDS,
    UnusableOffer,
    assert_offer_is_mintable,
    build_cart_permalink,
    code_expiry,
    expiry_epoch,
    mint_code,
    offer_quantity,
)
from .discounts import (
    FIXED_AMOUNT_DISCOUNT_TYPES,
    MAX_DISCOUNT_PERCENT,
    PERCENT_PER_UNIT_FRACTION,
    PERCENTAGE_DISCOUNT_TYPES,
    UnusableDiscount,
    discount_percent,
    offer_discount_percentage,
    shopify_discount_percentage,
)
from .domain import OffDomainCheckout, assert_on_domain, is_on_domain
from .lint import (
    CHECKOUT_REQUEST,
    INDIRECT_LOOKUPS,
    MINTING_CALLEES,
    MINTING_CLIENTS,
    MINTING_METHODS,
    TRUSTED_DOMAIN_KEYWORD,
    MintingCallSite,
    UnboundCheckoutRequest,
    code_minting_call_sites,
    unbound_checkout_requests,
)
from .provider import (
    CHECKOUT_EVENT_KINDS,
    CheckoutProvider,
    CheckoutRequest,
    CheckoutResult,
    MintedCheckout,
    OrphanedCheckoutCode,
    OrphanedCode,
    OrphanedOffDomainCheckout,
    PortMethodIsFinal,
    RegisteredDomains,
    default_permalink,
    domain_is_platform_verified,
    registered_domain_for,
)
from .providers import (
    CheckoutCreatorError,
    ShopifyCheckoutProvider,
    SimulatedRedirectProvider,
)
from .registry import (
    CHECKOUT_MODES,
    DEFAULT_CHECKOUT_MODE,
    UnknownCheckoutMode,
    register_provider,
    registered_modes,
    resolve_provider,
)
from .sellers import NoRegisteredDomains, StaticRegisteredDomains

__all__ = [
    "CHECKOUT_EVENT_KINDS",
    "CHECKOUT_MODES",
    "CHECKOUT_REQUEST",
    "CODE_ALPHABET",
    "CODE_BODY_LENGTH",
    "CODE_PREFIX",
    "DEFAULT_CHECKOUT_MODE",
    "FIXED_AMOUNT_DISCOUNT_TYPES",
    "MAX_CODE_TTL_SECONDS",
    "MAX_DISCOUNT_PERCENT",
    "PERCENT_PER_UNIT_FRACTION",
    "PERCENTAGE_DISCOUNT_TYPES",
    "INDIRECT_LOOKUPS",
    "MINTING_CALLEES",
    "MINTING_CLIENTS",
    "MINTING_METHODS",
    "TRUSTED_DOMAIN_KEYWORD",
    "CheckoutCreatorError",
    "CheckoutProvider",
    "CheckoutRequest",
    "CheckoutResult",
    "MintedCheckout",
    "MintingCallSite",
    "NoRegisteredDomains",
    "OffDomainCheckout",
    "OrphanedCheckoutCode",
    "OrphanedCode",
    "OrphanedOffDomainCheckout",
    "RegisteredDomains",
    "PortMethodIsFinal",
    "ShopifyCheckoutProvider",
    "StaticRegisteredDomains",
    "SimulatedRedirectProvider",
    "UnboundCheckoutRequest",
    "UnknownCheckoutMode",
    "UnusableDiscount",
    "UnusableOffer",
    "assert_offer_is_mintable",
    "assert_on_domain",
    "build_cart_permalink",
    "code_expiry",
    "code_minting_call_sites",
    "default_permalink",
    "discount_percent",
    "domain_is_platform_verified",
    "expiry_epoch",
    "is_on_domain",
    "mint_code",
    "offer_discount_percentage",
    "offer_quantity",
    "register_provider",
    "registered_domain_for",
    "registered_modes",
    "resolve_provider",
    "shopify_discount_percentage",
    "unbound_checkout_requests",
]
