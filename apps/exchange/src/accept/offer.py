"""``accept()`` — a won offer becomes a validated code and a permalink (R3, A5, C11, T-033).

This module is the **first production consumer** of the T-036 checkout port. Everything that
makes an accept safe already lives behind that port — the registered-domain check, the offer
validation, the mint, the C11 event sequence — so what is written here is deliberately thin:
resolve the provider for ``CHECKOUT_MODE``, build one :class:`CheckoutRequest`, hand it over,
and turn whatever comes back into a result the caller can act on.

Three things this layer owns, because the port cannot
-----------------------------------------------------

**The trusted half of the domain check.** The port compares the offer's checkout host against
a "registered seller domain", and that comparison is worth exactly as much as where the second
value came from. Left unset it is ``bid["store_domain"]`` — *a field the bidding store wrote* —
so a store posting ``store_domain: "attacker.tld"`` beside
``checkout_url: "https://attacker.tld/…"`` supplies both halves of its own check, agrees with
itself, and is handed a real single-use discount code on a host the platform never registered.
That was **measured**, not theorised. Only the call site can fix it, because only the call site
can reach the platform's seller registry, so :func:`accept` takes ``registered_domains`` and
spells the keyword out on the request — which is also what
``checkout.lint.unbound_checkout_requests`` mechanically requires of every call site, and what
this ticket's gate asserts.

With no source wired the legacy behaviour still stands (the frozen contract pins
``bid['store_domain']``), but it is no longer *invisible*: :attr:`AcceptResult.domain_verified`
and the ``checkout_redirect`` event both record that nothing external was consulted.

**Exactly one accept per auction.** A second accept must issue no second code — a buyer holding
two live single-use discounts for one purchase is a discount the seller never agreed to. The
guard is read *before* the provider runs and the auction is stamped only *after* a code exists,
so a failed accept leaves the auction acceptable and a successful one closes it.

**A failure is a re-offer, not a dead end (A5).** When the offer is refused or the merchant's
code creation fails, the buyer is not left holding nothing: the result names
:attr:`AcceptResult.reoffer_bid_ref` — the next slot on the shortlist, or the next bid when the
auction carries no shortlist — and the auction stays open so that accept can be called again.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..auction.ledger import build_event
from ..checkout import (
    DEFAULT_CHECKOUT_MODE,
    CheckoutRequest,
    CheckoutResult,
    resolve_provider,
)

__all__ = [
    "ACCEPT_REFUSED",
    "AcceptResult",
    "accept",
    "next_slot",
    "platform_registered_domains",
    "use_registered_domains",
]

#: The ``policy_event`` payload ``kind`` a refused accept is recorded under. The LedgerEvent
#: vocabulary is frozen (D24) and the exchange may not invent a kind, so a refusal is recorded
#: as the policy event it is rather than as a new spelling of "accepted".
ACCEPT_REFUSED = "accept_refused"

#: Field the auction is stamped with once a code exists for it.
_ACCEPTED_FIELD = "accepted_bid_ref"

#: Distinguishes "the caller passed no registered-domain source" from "the caller explicitly
#: passed ``None``". The first falls back to the deployment default; the second does not.
_UNSET: Any = object()

#: The deployment's registered-domain source, wired once at application configuration.
#:
#: It is module state rather than a parameter with a default because it is *wiring*: a source
#: every call site must remember to pass is a source some call site will forget, which is the
#: exact failure this ticket exists to close. A caller may still override it per call.
_platform_domains: Any | None = None


def use_registered_domains(source: Any | None) -> Any | None:
    """Wire the platform's registered-domain lookup for this process; return the previous one.

    ``source`` is a :class:`~apps.exchange.src.checkout.provider.RegisteredDomains` — anything
    exposing ``domain_for(store_id)`` (or callable with a store id).
    :class:`~apps.exchange.src.checkout.sellers.StaticRegisteredDomains` is the table shape;
    :class:`~apps.exchange.src.checkout.sellers.NoRegisteredDomains` is the fail-closed one.

    The previous value is returned so a test (or a request-scoped override) can restore it.
    """
    global _platform_domains
    previous = _platform_domains
    _platform_domains = source
    return previous


def platform_registered_domains() -> Any | None:
    """The wired registered-domain lookup, or ``None`` when nothing has been wired."""
    return _platform_domains


@dataclass(frozen=True)
class AcceptResult:
    """What an accept did — or refused to do, and why.

    Both outcomes are the same record on purpose: a refusal that is an exception cannot carry
    ``reoffer_bid_ref``, and the caller that has to re-offer the next slot is the one holding
    this result.
    """

    accepted: bool
    auction_id: str
    bid_ref: str
    mode: str
    store_id: str | None = None
    permalink_url: str | None = None
    code: str | None = None
    checkout_token: str | None = None
    expires_at: float | None = None
    provider: str | None = None
    #: False means the host this checkout was checked against came from the *bid*, not from
    #: the platform — the guard compared the store's word to the store's word. See the module
    #: docstring; this is the flag that stops a decorative pass looking like a real one.
    domain_verified: bool = False
    denial_reason: str | None = None
    #: The next slot to offer the buyer when this one could not be completed (A5).
    reoffer_bid_ref: str | None = None
    events: Sequence[Mapping[str, Any]] = field(default_factory=tuple)

    @property
    def kinds(self) -> list[str]:
        return [str(event["kind"]) for event in self.events]


# --- shape-tolerant readers ---------------------------------------------------------
# An auction reaches this layer as a plain mapping from the route, and as a record object
# from the state machine. Neither shape is more correct, and an accept that only worked for
# one of them would push the other caller into building a throwaway dict.
def _read(node: Any, name: str, default: Any = None) -> Any:
    if node is None:
        return default
    if isinstance(node, Mapping):
        return node.get(name, default)
    return getattr(node, name, default)


def _bid_ref_of(bid: Any) -> str:
    return str(_read(bid, "bid_id") or _read(bid, "bid_ref") or "")


def _bids_of(auction: Any) -> list[Any]:
    bids = _read(auction, "bids") or ()
    return list(bids)


def _find_bid(auction: Any, bid_ref: Any) -> Any | None:
    wanted = str(bid_ref)
    for bid in _bids_of(auction):
        if _bid_ref_of(bid) == wanted:
            return bid
    return None


def _slot_order(auction: Any) -> list[str]:
    """The order the buyer was shown, when there is one; otherwise bid order.

    A re-offer means "the next slot on the shortlist" (A5). An auction that never went
    through ranking has no shortlist, and falling back to bid order is what keeps the
    re-offer working for the accept-only paths (the frozen suite's auctions are one such).
    """
    slots = _read(_read(auction, "shortlist"), "slots")
    if slots:
        refs = [str(_read(slot, "bid_ref") or _read(slot, "bid_id") or "") for slot in slots]
        refs = [ref for ref in refs if ref]
        if refs:
            return refs
    return [ref for ref in (_bid_ref_of(bid) for bid in _bids_of(auction)) if ref]


def next_slot(auction: Any, after: Any, exhausted: Sequence[str] = ()) -> str | None:
    """The next slot to offer after ``after`` failed, or ``None`` when the field is spent."""
    order = _slot_order(auction)
    spent = {str(after), *(str(ref) for ref in exhausted)}
    tail = order[order.index(str(after)) + 1 :] if str(after) in order else order
    for ref in tail:
        if ref not in spent:
            return ref
    return None


def _acceptance_is_recordable(auction: Any) -> bool:
    """Can this auction be stamped as accepted?

    Asked **before** anything is minted, and that ordering is the point: an auction that
    cannot record its acceptance cannot refuse the second accept either, so every buyer who
    asked twice would get two live single-use codes. Discovering that after the merchant has
    issued the first one is discovering it too late.
    """
    if isinstance(auction, MutableMapping):
        return True
    try:
        setattr(auction, _ACCEPTED_FIELD, _read(auction, _ACCEPTED_FIELD))
    except Exception:
        return False
    return True


def _record_acceptance(auction: Any, bid_ref: str) -> None:
    if isinstance(auction, MutableMapping):
        auction[_ACCEPTED_FIELD] = bid_ref
        return
    setattr(auction, _ACCEPTED_FIELD, bid_ref)


def _refusal_event(
    auction: Any, bid_ref: str, store_id: str | None, reason: str
) -> tuple[Mapping[str, Any], ...]:
    """Record the refusal against D24's frozen vocabulary.

    A refused accept that leaves no trace is the one an operator cannot investigate, and
    there is no ``accept_refused`` LedgerEvent kind to invent one with — so it is filed as
    the ``policy_event`` it is, carrying that kind's published body plus the reason.
    """
    return (
        build_event(
            "policy_event",
            auction_id=str(_read(auction, "auction_id") or ""),
            store_id=store_id,
            payload={
                "kind": ACCEPT_REFUSED,
                "severity": "warning",
                "opened_at": float(_read(auction, "now") or 0.0),
                "bid_ref": bid_ref,
                "reason": reason,
            },
        ),
    )


def _refused(
    auction: Any,
    bid_ref: Any,
    mode: str,
    reason: str,
    *,
    store_id: str | None = None,
    reoffer_bid_ref: str | None = None,
) -> AcceptResult:
    ref = str(bid_ref)
    return AcceptResult(
        accepted=False,
        auction_id=str(_read(auction, "auction_id") or ""),
        bid_ref=ref,
        mode=str(mode),
        store_id=store_id,
        denial_reason=reason,
        reoffer_bid_ref=reoffer_bid_ref,
        events=_refusal_event(auction, ref, store_id, reason),
    )


def accept(
    auction: Any,
    bid_id: Any,
    code_creator: Any = None,
    mode: str = DEFAULT_CHECKOUT_MODE,
    *,
    registered_domains: Any = _UNSET,
) -> AcceptResult:
    """Accept ``bid_id`` on ``auction`` and return the permalink the provider minted.

    The four positional parameters are the published signature (D45) and gain nothing when a
    new provider is registered: ``mode`` is the registry's selector and ``code_creator`` rides
    on the request.

    Args:
        auction: ``{auction_id, bids, accepted_bid_ref, now, ...}`` as a mapping or a record.
            It is **stamped** with the accepted bid ref on success, which is what makes the
            second accept a refusal.
        bid_id: which bid is being accepted.
        code_creator: the merchant ``POST /codes`` client. Used by the Shopify adapter only;
            the simulated redirect provider mints locally and never touches it.
        mode: ``CHECKOUT_MODE``. Resolved through the registry, which **raises** on a mode
            nobody registered rather than quietly running the simulated path.
        registered_domains: the platform's ``store_id -> registered domain`` lookup. Omitted,
            the process-wide source wired by :func:`use_registered_domains` is used; when
            nothing is wired at all the port falls back to the bid's own claim and the result
            says so via :attr:`AcceptResult.domain_verified`.

    Returns:
        :class:`AcceptResult`. ``accepted`` is the outcome; a refusal carries
        ``denial_reason`` and, where the field is not spent, ``reoffer_bid_ref``.

    Raises:
        UnknownCheckoutMode: ``mode`` has no registered provider. That is a deployment
            misconfiguration rather than a decision about this buyer, and the registry's
            contract is that it never falls back.
    """
    ref = str(bid_id)
    sellers = platform_registered_domains() if registered_domains is _UNSET else registered_domains

    bid = _find_bid(auction, ref)
    if bid is None:
        return _refused(
            auction,
            ref,
            mode,
            f"unknown_bid: auction {str(_read(auction, 'auction_id') or '')!r} carries no bid "
            f"{ref!r}; there is nothing to accept",
            reoffer_bid_ref=next_slot(auction, ref),
        )

    store_id = str(_read(bid, "store_id") or "")

    already = _read(auction, _ACCEPTED_FIELD)
    if already:
        return _refused(
            auction,
            ref,
            mode,
            f"already_accepted: this auction was already accepted on bid {str(already)!r}; a "
            f"second accept issues no second discount code (R3/A5)",
            store_id=store_id,
        )

    if not _acceptance_is_recordable(auction):
        return _refused(
            auction,
            ref,
            mode,
            f"unrecordable_acceptance: {type(auction).__name__} cannot record "
            f"{_ACCEPTED_FIELD!r}, so a second accept could not be refused; refusing the "
            f"first rather than issuing a code that cannot be made single-use",
            store_id=store_id,
        )

    # Resolved before the request is built: an unregistered mode must not reach the point of
    # having a request to mint from.
    provider = resolve_provider(mode)

    request = CheckoutRequest(
        auction_id=str(_read(auction, "auction_id") or ""),
        bid_ref=ref,
        store_id=store_id,
        # The bid's CLAIM about its own domain. Kept because the frozen contract pins it and
        # because it is what the refusal message needs to quote — but overridden outright
        # whenever `registered_domains` below answers.
        store_domain=str(_read(bid, "store_domain") or ""),
        offer=_read(bid, "offer") or {},
        mode=str(mode),
        code_creator=code_creator,
        now=float(_read(auction, "now") or 0.0),
        # The one line this whole ticket turns on. Without it `registered_domain_for` returns
        # the bid's own claim and the host guard compares a store's word to that same store's
        # word — see the module docstring, and `unbound_checkout_requests`, which fails the
        # build for any call site that leaves it out.
        registered_domains=sellers,
    )

    try:
        checkout: CheckoutResult = provider.checkout(request)
    except Exception as exc:
        # Deliberately broad. Everything reachable here is a refusal of THIS offer — an
        # off-domain checkout URL, an unusable offer field, a merchant `POST /codes` that
        # answered with no code or did not answer at all — and A5's promise is that the buyer
        # gets the next slot rather than a stack trace. The port runs its checks before the
        # mint, so a refusal from any of them has created no code anywhere; a failure from
        # the mint itself is exactly the code-creation failure this path exists for.
        return _refused(
            auction,
            ref,
            mode,
            f"checkout_refused: {type(exc).__name__}: {exc}",
            store_id=store_id,
            reoffer_bid_ref=next_slot(auction, ref),
        )

    # Only now, with a code that actually exists, is the auction closed to further accepts.
    _record_acceptance(auction, ref)

    return AcceptResult(
        accepted=True,
        auction_id=request.auction_id,
        bid_ref=ref,
        mode=checkout.mode,
        store_id=store_id,
        permalink_url=checkout.permalink_url,
        code=checkout.code,
        checkout_token=checkout.checkout_token,
        expires_at=checkout.expires_at,
        provider=checkout.provider,
        domain_verified=checkout.domain_verified,
        events=tuple(checkout.events),
    )
