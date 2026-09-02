"""``CHECKOUT_MODE`` -> provider. One registry, and no silent fallback.

D23 pins the vocabulary to ``{redirect, shopify_stub}`` with ``redirect`` the default; the
rest of the doc set also spells the Shopify mode ``shopify``, and the frozen suite calls
``accept`` with all three (``test_spec_criteria.py:1036``). All three are registered.

**An unregistered mode raises.** That is the whole reason this is a registry rather than an
``if`` chain: a chain ends in an ``else``, and whatever that ``else`` does becomes the
behaviour of every mode nobody thought about — typically "quietly do the simulated thing",
which means a deployment configured for a real payment path silently ships a fake one.
:func:`resolve_provider` raises :class:`UnknownCheckoutMode` instead, naming what *is*
registered.

Registration is a plain function call, so adding a provider adds no parameter to
``accept(auction, bid_id, code_creator, mode)`` — the mode argument it already receives is
the selector.
"""

from __future__ import annotations

from .provider import CheckoutProvider
from .providers import ShopifyCheckoutProvider, SimulatedRedirectProvider

__all__ = [
    "CHECKOUT_MODES",
    "DEFAULT_CHECKOUT_MODE",
    "UnknownCheckoutMode",
    "register_provider",
    "registered_modes",
    "resolve_provider",
]

#: D23's default.
DEFAULT_CHECKOUT_MODE = "redirect"

_REGISTRY: dict[str, CheckoutProvider] = {}


class UnknownCheckoutMode(LookupError):
    """A CHECKOUT_MODE with no registered provider. Never a fallback."""


def _normalise(mode: str | None) -> str:
    return str(mode if mode is not None else DEFAULT_CHECKOUT_MODE).strip().lower()


def register_provider(mode: str, provider: CheckoutProvider) -> CheckoutProvider:
    """Register ``provider`` under ``mode``, replacing any previous registration."""
    if not isinstance(provider, CheckoutProvider):
        raise TypeError(
            f"a checkout provider must subclass CheckoutProvider so the port's "
            f"registered-domain check and C11 event sequence apply to it; got {provider!r}"
        )
    key = _normalise(mode)
    if not key:
        raise ValueError("a checkout mode must be a non-empty string")
    _REGISTRY[key] = provider
    return provider


def resolve_provider(mode: str | None = None) -> CheckoutProvider:
    """Return the provider registered for ``mode``, or raise. There is no fallback."""
    key = _normalise(mode)
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownCheckoutMode(
            f"no checkout provider is registered for CHECKOUT_MODE {key!r}; "
            f"registered modes are {registered_modes()}"
        ) from None


def registered_modes() -> list[str]:
    """Every registered mode spelling, sorted."""
    return sorted(_REGISTRY)


# --- the modes `accept()` already resolves ------------------------------------------
register_provider("redirect", SimulatedRedirectProvider())
register_provider("shopify_stub", ShopifyCheckoutProvider())
register_provider("shopify", ShopifyCheckoutProvider())

#: The spellings shipped out of the box, in the order D23 and the doc set introduce them.
CHECKOUT_MODES: tuple[str, str, str] = ("redirect", "shopify_stub", "shopify")
