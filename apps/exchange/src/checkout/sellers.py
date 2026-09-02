"""Registered-domain sources: the *trusted* half of the checkout domain check.

:mod:`.domain` compares a checkout URL's host against "the seller's registered domain", and
that comparison is exactly as trustworthy as where the second value came from. On a bid it
came from ``bid["store_domain"]`` — **a field the store wrote**. A store that posts

.. code-block:: json

    {"store_domain": "attacker.tld",
     "offer": {"checkout_url": "https://attacker.tld/cart/1:1?discount=X"}}

has supplied both halves of its own check, so the check passes, and the buyer is handed a
real discount code on a host the platform never registered. The guard rejected only a store
that contradicted *itself*, which no attacker does.

:class:`RegisteredDomains` (the protocol, in :mod:`.provider`) is the fix; this module is
the pair of implementations a deployment actually gets:

:class:`NoRegisteredDomains`
    Knows nobody, so it refuses everybody. This is the **default**, and it is the same rule
    ``StaticSellerEligibility`` applies to eligibility: an exchange nobody has connected to
    the seller registry must mint nothing, rather than quietly falling back to trusting the
    bid. Fail closed applies to the *deployment*, not only to a single lookup.

:class:`StaticRegisteredDomains`
    A real ``store_id -> registered domain`` table. The deterministic double the tests drive
    and the shape the live seller registry will present.

The reason this is a separate module rather than a dict on the request: the source is
*wiring*, resolved once at app configuration and passed to every ``CheckoutRequest`` the
service builds. Leaving it to each call site is how the bid's own claim got trusted in the
first place — a default that is safe only if every caller remembers to override it is not a
safe default.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["NoRegisteredDomains", "StaticRegisteredDomains"]


class NoRegisteredDomains:
    """The fail-closed default: no store is registered, so no checkout may be minted.

    Returning ``None`` (rather than raising) is deliberate — ``registered_domain_for``
    turns "the platform holds no domain for this store" into an ``OffDomainCheckout`` that
    names the store, which is the message an operator can act on.
    """

    def domain_for(self, store_id: str) -> str | None:
        return None

    __call__ = domain_for


class StaticRegisteredDomains:
    """A ``store_id -> registered domain`` table the platform, not the store, wrote."""

    def __init__(self, domains: Mapping[str, str] | None = None) -> None:
        self._domains = {str(k): str(v) for k, v in (domains or {}).items()}

    def domain_for(self, store_id: str) -> str | None:
        return self._domains.get(str(store_id))

    __call__ = domain_for

    def register(self, store_id: str, domain: str) -> None:
        self._domains[str(store_id)] = str(domain)

    def __len__(self) -> int:
        return len(self._domains)
