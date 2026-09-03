"""Accept: the won offer becomes a validated discount code and a permalink (R3, A5, C11).

Two callables, one boundary apart — the same split D54 draws for solicitation:

==========================================================  =====================================
``accept(auction, bid_id, code_creator, mode)``             the pure accept; assumes clean input
``accept_offer(auction=, bid_ref=, code_creator=, mode=,    the R12 gate, then that accept
eligibility=)``
==========================================================  =====================================

``accept_offer`` is re-exported from :mod:`apps.exchange.src.orchestration`, which is the
public orchestration boundary the frozen suite reads it from; it lives here because this is
the package that owns what an accept *is*.

.. code-block:: python

    from apps.exchange.src.accept import accept, use_registered_domains
    from apps.exchange.src.checkout import StaticRegisteredDomains

    # Wire the platform's seller registry ONCE, at application configuration.
    use_registered_domains(StaticRegisteredDomains({"store-a": "store-a.example.com"}))

    result = accept(auction, "bid-a", code_creator, "shopify")
    result.permalink_url, result.code, result.events, result.domain_verified

**Wire the registry.** Without it the checkout host guard compares ``bid["checkout_url"]``
against ``bid["store_domain"]`` — both written by the same bidding store — so it rejects only
a store that contradicts itself, which no attacker does. It was measured admitting a bid
claiming ``attacker.tld``. :attr:`~.offer.AcceptResult.domain_verified` is ``False`` for
every such accept, and ``checkout.lint.unbound_checkout_requests`` is what stops a future call
site from dropping the keyword again.
"""

from __future__ import annotations

from .gate import accept_offer
from .offer import (
    ACCEPT_REFUSED,
    AcceptResult,
    accept,
    next_slot,
    platform_registered_domains,
    use_registered_domains,
)

__all__ = [
    "ACCEPT_REFUSED",
    "AcceptResult",
    "accept",
    "accept_offer",
    "next_slot",
    "platform_registered_domains",
    "use_registered_domains",
]
