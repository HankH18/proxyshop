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

The example above wires ``apps.exchange.src.accept`` while :mod:`exchange.main` builds the
served app from ``exchange.accept``. Those are the same file, and used to be two module
objects with two copies of ``_platform_domains`` — so following this docstring left the
served app unwired. :mod:`._spellings`, bound at the bottom of this file, is what makes the
two names one module; see it for the measurement and for why the sentinel matters too.

**A refusal names a declared code.** ``AcceptResult.denial_reason`` is persisted into a
``policy_event`` and published as the 409 body, so its vocabulary is a contract:
``DENIAL_REASONS`` is that declaration and every reason this package emits reads
``"<declared code>: <free prose>"``. See :mod:`.reasons`.

**Served over HTTP by** :mod:`.routes` — ``POST /auctions/{auction_id}/accept``, the path
``packages/contracts/openapi/exchange.openapi.json`` publishes. That module is also the
deployment call site that wires the platform's seller registry, through
:func:`.routes.configure_accept`.
"""

from __future__ import annotations

from ._spellings import bind_package
from .claims import (
    AcceptanceClaims,
    ClaimOutcome,
    InMemoryAcceptanceClaims,
    StoreAcceptanceClaims,
    acceptance_claims_scope,
    platform_acceptance_claims,
    use_acceptance_claims,
)
from .gate import accept_offer
from .offer import (
    ACCEPT_REFUSED,
    NO_DISCOUNT_ON_A_FALLBACK,
    AcceptResult,
    accept,
    next_slot,
    platform_registered_domains,
    use_registered_domains,
)
from .reasons import (
    DENIAL_ALREADY_ACCEPTED,
    DENIAL_AUCTION_NOT_ACCEPTABLE,
    DENIAL_BLACKLISTED,
    DENIAL_CHECKOUT_REFUSED,
    DENIAL_REASONS,
    DENIAL_UNAVAILABLE,
    DENIAL_UNKNOWN_BID,
    DENIAL_UNRECORDABLE_ACCEPTANCE,
    DENIAL_UNROUTABLE_FALLBACK,
    DENIAL_UNSPECIFIED,
    denial_code,
    denial_reason,
)

__all__ = [
    "ACCEPT_REFUSED",
    "DENIAL_ALREADY_ACCEPTED",
    "DENIAL_AUCTION_NOT_ACCEPTABLE",
    "DENIAL_BLACKLISTED",
    "DENIAL_CHECKOUT_REFUSED",
    "DENIAL_REASONS",
    "DENIAL_UNAVAILABLE",
    "DENIAL_UNKNOWN_BID",
    "DENIAL_UNRECORDABLE_ACCEPTANCE",
    "DENIAL_UNROUTABLE_FALLBACK",
    "DENIAL_UNSPECIFIED",
    "NO_DISCOUNT_ON_A_FALLBACK",
    "AcceptResult",
    "AcceptanceClaims",
    "ClaimOutcome",
    "InMemoryAcceptanceClaims",
    "StoreAcceptanceClaims",
    "accept",
    "accept_offer",
    "acceptance_claims_scope",
    "denial_code",
    "denial_reason",
    "next_slot",
    "platform_acceptance_claims",
    "platform_registered_domains",
    "use_acceptance_claims",
    "use_registered_domains",
]

# LAST, and it is not decoration: this tree is importable as `exchange.accept` and as
# `apps.exchange.src.accept`, and without this Python executes every file here TWICE — once
# per spelling — leaving TWO copies of `_platform_domains`, so `use_registered_domains`
# through the spelling this file's own docstring documents leaves the spelling the served
# app runs on unwired, and every accept it serves falls back to the bid's own `store_domain`.
# Measured on this worktree: `apps.exchange.src.accept is exchange.accept` was False while
# the two files' inodes were equal. See `_spellings.py`.
bind_package(__name__)
