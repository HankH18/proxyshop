"""Checkout: the one port a won offer becomes a discount code and a permalink through.

R3 / A5 / C11 / D45, ticket T-036.

.. code-block:: python

    from apps.exchange.src.checkout import CheckoutRequest, resolve_provider

    provider = resolve_provider(mode)            # raises on an unregistered mode
    result = provider.checkout(CheckoutRequest(  # domain-checked, then minted
        auction_id=auction["auction_id"],
        bid_ref=bid["bid_id"],
        store_id=bid["store_id"],
        store_domain=bid["store_domain"],        # the REGISTERED domain — the trusted half
        offer=bid["offer"],                      # carries checkout_url — the untrusted half
        mode=mode,
        code_creator=code_creator,               # used by the Shopify adapter only
        now=auction["now"],
    ))
    result.permalink_url, result.code, result.events

What the layout buys, module by module:

=================  ================================================================
:mod:`.provider`   the port: the final template method, the request/result records
:mod:`.providers`  ``SimulatedRedirectProvider`` (required) and the Shopify adapter
:mod:`.registry`   ``CHECKOUT_MODE`` -> provider; an unregistered mode **raises**
:mod:`.domain`     the exact-host check the port applies to every provider (D22/C10)
:mod:`.codes`      the ONLY place a code is minted and a permalink is built (D22)
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
    build_cart_permalink,
    code_expiry,
    mint_code,
)
from .domain import OffDomainCheckout, assert_on_domain, is_on_domain
from .lint import MINTING_CALLEES, MintingCallSite, code_minting_call_sites
from .provider import (
    CHECKOUT_EVENT_KINDS,
    CheckoutProvider,
    CheckoutRequest,
    CheckoutResult,
    MintedCheckout,
    PortMethodIsFinal,
    default_permalink,
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

__all__ = [
    "CHECKOUT_EVENT_KINDS",
    "CHECKOUT_MODES",
    "CODE_ALPHABET",
    "CODE_BODY_LENGTH",
    "CODE_PREFIX",
    "DEFAULT_CHECKOUT_MODE",
    "MAX_CODE_TTL_SECONDS",
    "MINTING_CALLEES",
    "CheckoutCreatorError",
    "CheckoutProvider",
    "CheckoutRequest",
    "CheckoutResult",
    "MintedCheckout",
    "MintingCallSite",
    "OffDomainCheckout",
    "PortMethodIsFinal",
    "ShopifyCheckoutProvider",
    "SimulatedRedirectProvider",
    "UnknownCheckoutMode",
    "assert_on_domain",
    "build_cart_permalink",
    "code_expiry",
    "code_minting_call_sites",
    "default_permalink",
    "is_on_domain",
    "mint_code",
    "register_provider",
    "registered_modes",
    "resolve_provider",
]
