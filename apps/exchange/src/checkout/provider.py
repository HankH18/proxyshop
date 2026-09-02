"""``CheckoutProvider`` — the single port every accept path goes through (R3, A5, C11, D45).

A won offer becomes a discount code and a permalink in exactly one place: here. The port is
a **template method**, and the split between what the port does and what an implementation
does is the entire point of T-036:

===============================================  =====================================
the PORT does, for every provider, always        an IMPLEMENTATION does
===============================================  =====================================
validate the offer's host against the registered  :meth:`CheckoutProvider.mint` — turn an
seller domain, **before** anything is minted      approved offer into a code + permalink
validate the permalink the provider handed back
emit the three ordered `LedgerEvent` kinds
===============================================  =====================================

Two consequences, both deliberate:

**No implementation can opt out of the domain check.** It is not a rule providers are asked
to follow — :meth:`CheckoutProvider.checkout` runs it and is final:
``__init_subclass__`` refuses, at class-definition time, any subclass that overrides
``checkout``. A provider written next year by someone who never read D22 still cannot mint a
code for ``attacker.tld``.

**Nothing downstream can tell which provider ran (C11).** The events are built by the port
from the request, not by the provider, so ``redirect`` and the Shopify spellings emit the
identical ordered kinds by construction rather than by two implementations happening to
agree. D23 and `DESIGN.md:84` explicitly reject divergent event schemas per mode, and the
frozen suite asserts the equality (``test_e3_exchange.py:688``).

The Shopify path (T-052) is one adapter behind this port — not the route to a code.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from ..auction.ledger import build_event
from .codes import assert_offer_is_mintable, build_cart_permalink, code_expiry, offer_quantity
from .domain import OffDomainCheckout, assert_on_domain

__all__ = [
    "CHECKOUT_EVENT_KINDS",
    "CheckoutProvider",
    "CheckoutRequest",
    "CheckoutResult",
    "MintedCheckout",
    "PortMethodIsFinal",
    "RegisteredDomains",
    "default_permalink",
    "registered_domain_for",
]


class RegisteredDomains(Protocol):
    """The platform's own answer to "what domain is this seller registered at?".

    Implementations read ``app.sellers`` (or whatever holds the registration) and return
    ``None`` for a store they do not know. A callable taking ``store_id`` works too.
    """

    def domain_for(self, store_id: str) -> str | None: ...


#: C11: the ordered `LedgerEvent` kinds every checkout emits, whichever provider ran.
CHECKOUT_EVENT_KINDS: tuple[str, str, str] = ("accepted", "code_created", "checkout_redirect")


class PortMethodIsFinal(TypeError):
    """A provider tried to override a method the port performs on every provider's behalf."""


@dataclass(frozen=True)
class CheckoutRequest:
    """Everything a provider is allowed to see about a won offer.

    ``store_domain`` is *supposed* to be the seller's **registered** domain — the one the
    platform holds in ``app.sellers``. On a bid it is read as ``bid["store_domain"]``, and
    that is where the sharp edge is: **a bid is a store's own reply**, so a store that writes
    ``store_domain: "attacker.tld"`` beside ``checkout_url: "https://attacker.tld/…"``
    supplies both halves of the comparison and the host check admits its own domain. The
    check then rejects only a store that contradicts *itself*, which no attacker does.

    :attr:`registered_domains` is the fix and the reason this field exists: give the request
    the platform's own lookup and the port compares against **that**, ignoring whatever the
    bid claimed. Leave it unset and the legacy behaviour stands — the bid's word is taken —
    which is what the frozen contract (``bid['store_domain']``) currently pins, so wiring the
    source is a one-line change at the call site that builds this request rather than a
    change to any provider.
    """

    auction_id: str
    bid_ref: str
    store_id: str
    store_domain: str
    offer: Mapping[str, Any]
    mode: str = "redirect"
    #: The merchant ``POST /codes`` client, when one is injected. The simulated provider
    #: does not use it; the Shopify adapter does. Kept on the request so registering a
    #: further provider adds no parameter to ``accept()``.
    code_creator: Any | None = None
    now: float = 0.0
    #: The platform's registered-domain lookup (:class:`RegisteredDomains`), when the caller
    #: has one. Present, it overrides :attr:`store_domain` entirely and a store it has never
    #: heard of mints nothing — fail closed. Absent, :attr:`store_domain` is used as-is.
    registered_domains: Any | None = None

    @property
    def checkout_url(self) -> str:
        return str(self.offer.get("checkout_url") or "")


@dataclass(frozen=True)
class MintedCheckout:
    """What a provider returns: a code, and where to send the buyer to redeem it."""

    code: str
    permalink_url: str
    expires_at: float | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckoutResult:
    """What the port returns. ``events`` is the C11 sequence, in order."""

    code: str
    permalink_url: str
    events: Sequence[Mapping[str, Any]]
    mode: str
    provider: str
    checkout_token: str
    expires_at: float | None = None

    @property
    def kinds(self) -> list[str]:
        return [str(event["kind"]) for event in self.events]


class CheckoutProvider:
    """The port. Subclass and implement :meth:`mint`; never override :meth:`checkout`."""

    #: Human-readable provider name, recorded on the result for operators — never used to
    #: branch on, because nothing downstream may behave differently per provider (C11).
    name: ClassVar[str] = "checkout-provider"

    #: Methods the port performs on every provider's behalf. Overriding one would let an
    #: implementation opt out of a guarantee the port makes, so it is refused.
    _FINAL_METHODS: ClassVar[frozenset[str]] = frozenset({"checkout"})

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        overridden = sorted(name for name in cls._FINAL_METHODS if name in cls.__dict__)
        if overridden:
            raise PortMethodIsFinal(
                f"{cls.__name__} overrides {overridden}, which CheckoutProvider performs for "
                f"every provider (the registered-domain check and the C11 event sequence). "
                f"Implement mint() instead."
            )

    # --- the final template method -------------------------------------------------
    def checkout(self, request: CheckoutRequest) -> CheckoutResult:
        """Turn a won offer into a code and a permalink, with the port's guarantees applied.

        Order matters and is asserted downstream: the domain check runs **before** ``mint``,
        so a refused offer has no code created for it anywhere — not by this provider, not
        by an injected merchant client, not by Shopify.
        """
        # 1. Resolve the trusted half FIRST. If the caller wired a registered-domain
        #    source, the bid's claim about its own domain is discarded here.
        registered = registered_domain_for(request)

        # 2. Untrusted input, checked against the registered domain, before anything else.
        #
        #    Only when there IS one. "No checkout_url" and "a checkout_url pointing at
        #    attacker.tld" are not the same condition and must not get the same answer:
        #    `collect_bids` manufactures a list-price fallback offer for every Tier-0 and
        #    silent store (R10), and that offer carries no checkout_url by construction —
        #    it is catalog data, not a store's reply. Refusing an absent URL therefore
        #    refused every fallback bid the exchange had just built for itself, so a Tier-0
        #    store could be ranked and shortlisted but never bought from.
        #
        #    Nothing is relaxed by allowing it: with no URL there is no untrusted host in
        #    play at all, the provider builds the permalink from `registered` below, and
        #    step 5 validates that. A seller with no registered domain still cannot get a
        #    code — the permalink it would be built from has no host to match.
        if request.checkout_url:
            assert_on_domain(request.checkout_url, registered, what="offer checkout_url")

        # 3. The rest of the offer must be usable too, and this has to happen before the
        #    mint: the Shopify adapter's mint issues a real merchant discount, and a field
        #    that only blows up afterwards leaves that code live and unrecorded.
        assert_offer_is_mintable(request.offer)

        # 4. The provider's only job.
        minted = self.mint(request)

        # 5. The provider's OWN output is untrusted too: a buggy or hostile adapter that
        #    returns a permalink on another host must not be able to hand the buyer over.
        assert_on_domain(minted.permalink_url, registered, what=f"{self.name} permalink_url")

        checkout_token = secrets.token_hex(16)
        return CheckoutResult(
            code=minted.code,
            permalink_url=minted.permalink_url,
            events=self._events(request, minted, checkout_token),
            mode=request.mode,
            provider=self.name,
            checkout_token=checkout_token,
            expires_at=(
                minted.expires_at
                if minted.expires_at is not None
                else code_expiry(request.now, request.offer)
            ),
        )

    # --- the extension point --------------------------------------------------------
    def mint(self, request: CheckoutRequest) -> MintedCheckout:
        """Turn an already-domain-checked offer into a code and an on-domain permalink."""
        raise NotImplementedError(f"{type(self).__name__} must implement mint(request)")

    # --- C11: built by the port, identical for every provider ------------------------
    def _events(
        self,
        request: CheckoutRequest,
        minted: MintedCheckout,
        checkout_token: str,
    ) -> list[Mapping[str, Any]]:
        offer = dict(request.offer)
        common = {"auction_id": request.auction_id, "store_id": request.store_id}
        return [
            build_event(
                "accepted",
                payload={
                    "checkout_token": checkout_token,
                    "bid_ref": request.bid_ref,
                    "offer": {
                        "product_ref": offer.get("product_ref"),
                        "unit_price": offer.get("unit_price"),
                        "total_price": offer.get("total_price"),
                        "discount": offer.get("discount"),
                    },
                },
                **common,
            ),
            build_event(
                "code_created",
                payload={"checkout_token": checkout_token, "discount_code": minted.code},
                **common,
            ),
            build_event(
                "checkout_redirect",
                payload={
                    "checkout_token": checkout_token,
                    "permalink_url": minted.permalink_url,
                    "discount_code": minted.code,
                },
                **common,
            ),
        ]


def _usable(domain: Any, request: CheckoutRequest) -> str:
    """A registered domain a permalink can actually be built on, or an explicit refusal."""
    if not domain or not str(domain).strip():
        raise OffDomainCheckout(
            f"no registered domain is on file for {request.store_id!r}, so there is no host "
            f"a checkout for it could be on (C10/D22)"
        )
    return str(domain)


def registered_domain_for(request: CheckoutRequest) -> str:
    """The domain the port compares against: the platform's, when the caller wired one.

    With no :attr:`CheckoutRequest.registered_domains` source this returns
    ``request.store_domain`` — the legacy behaviour the frozen contract pins, in which the
    bid supplies the domain it is checked against.

    With a source, the source wins outright and there is no falling back to the bid's claim:
    a lookup that raises, or that does not know the store, refuses the checkout. That is the
    only order that is safe — falling back on a lookup failure would mean a store could get
    its own claim honoured by making the lookup fail.

    Either way the result is a **usable** domain or an exception. It is called before
    ``mint``, so "there is no domain here" is settled while refusing still costs nothing;
    discovering it afterwards, when the permalink is checked, would mean the merchant had
    already issued a real code for a seller the platform cannot place.
    """
    source = request.registered_domains
    if source is None:
        return _usable(request.store_domain, request)

    lookup = getattr(source, "domain_for", None)
    if not callable(lookup):
        if not callable(source):
            raise TypeError(
                f"registered_domains {source!r} exposes neither domain_for(store_id) "
                f"nor __call__(store_id)"
            )
        lookup = source

    try:
        domain = lookup(request.store_id)
    except Exception as exc:
        raise OffDomainCheckout(
            f"registered domain lookup for {request.store_id!r} failed "
            f"({type(exc).__name__}: {exc}); refusing to check out against the bid's own "
            f"claim {request.store_domain!r}"
        ) from exc

    if not domain or not str(domain).strip():
        raise OffDomainCheckout(
            f"the platform holds no registered domain for {request.store_id!r}; the bid's "
            f"claim {request.store_domain!r} is not evidence of one"
        )
    return _usable(domain, request)


def default_permalink(request: CheckoutRequest, code: str) -> str:
    """The D22 cart permalink on the seller's registered domain, for providers that build one."""
    offer = request.offer
    return build_cart_permalink(
        shop_domain=registered_domain_for(request),
        code=code,
        variant_id=offer.get("variant_ref") or offer.get("variant_id") or 1,
        quantity=offer_quantity(offer),
    )
