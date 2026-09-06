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
two live single-use discounts for one purchase is a discount the seller never agreed to. Since
T-158 the guard is an atomic *claim* on the auction id (:mod:`.claims`), taken before the
provider runs, so two concurrent accepts cannot both pass it; a failed accept gives the claim
back and leaves the auction acceptable, and a successful one keeps it forever.

**A failure is a re-offer, not a dead end (A5).** When the offer is refused or the merchant's
code creation fails, the buyer is not left holding nothing: the result names
:attr:`AcceptResult.reoffer_bid_ref` — the next slot on the shortlist, or the next bid when the
auction carries no shortlist — and the auction stays open so that accept can be called again.

Two limits of this layer, stated so the next caller does not assume otherwise
-----------------------------------------------------------------------------

**The one-accept guard is exactly as durable as the claim table you wire (T-158).** The stamp
:func:`accept` writes onto the auction object is now a *second* line of defence, not the guard.
The guard is :mod:`.claims`: an atomic, at-most-once claim on the auction id, taken **before**
the merchant is asked to mint. Wire :class:`~.claims.StoreAcceptanceClaims` over the store the
auction actually lives in — :func:`~.routes.configure_accept` does, so every served accept has
one — and the guarantee reaches across processes, because ``RedisAuctionStore``'s reservation
is one ``SET … NX``.

Wire nothing and there is deliberately **no** default table: the guard falls back to the
object-scoped stamp alone, and :attr:`AcceptResult.claim_verified` is ``False`` so the result
says which guard actually ran. A process-lifetime default is the obvious-looking alternative and
it is the wrong one twice over — it is the in-memory ledger that makes an in-process test green
while the second uvicorn worker mints the second code, and because a test suite reuses one
auction id where production uses ``auction-{uuid4()}``, it refuses legitimate first accepts
(measured: four tests of the frozen acceptance suite, each refused by a *previous test's*
claim). :mod:`.claims` carries that measurement.

The paragraph this replaces told the route to persist the stamp "through
``AuctionStateMachine``, whose ``ACCEPTED`` transition is already the serialised one". Both
halves were wrong. That transition was **not** serialised (``state.py``'s docstring carries the
measurement), and persisting *after* the mint would not have helped if it were: the loser of
the race discovers it has lost only once ``POST /codes`` has issued a live single-use discount,
so the outcome is a correct 409 sitting on top of a real double spend.

**A merchant client that answers off-domain has already minted.** The port checks the offer's
``checkout_url`` before the mint, but that check says nothing about the permalink the provider
hands *back*, so the first host comparison a delegating provider can fail is the one on that
permalink — after ``POST /codes`` issued a real single-use discount. An offer with *no* URL
reaches the mint having faced no host comparison at all. **That used to be the everyday shape
of a bid arriving here — R10 list-price fallback and hosted bid alike — and since T-349 it is
not.** The history is kept because the reasoning that produced it was sound and exactly one
argument was wrong.

A doc sweep (edbc422) asserted that a shortlisted fallback "arrives here carrying a URL". At
the time it did not, and the expectation behind the claim was nevertheless reasonable:
``ranking/candidates.py`` really does complete a fallback entry's offer from the platform's
``store_id -> domain`` registry (``candidates.py:247``), and ``POST /auctions`` really does
record the bid book *after* the ranking has run. What broke the chain was the argument in
between. The route handed :func:`~..auction.routes.collected_bid_records`
``ranking["candidates"]`` alone, and that key is **not** the projection —
``ranking/__init__.py`` sets ``"candidates": rows``, the rank-ROW projection, whose keys are
``bid_id``, ``components``, ``eligible``, ``exclusion_reasons``, ``features``, ``price``,
``provenance_labels``, ``rank_score``, ``store_id``, ``trust``, ``trust_summary`` and
``verified_hard_fit_count``. A rank row has no ``offer`` and no ``store_domain``, so
``candidate.get("offer") or {}`` recorded the empty dict and no ``store_domain`` key was
written at all.

``rank_auction`` now returns that projection alongside the rows under ``"projected"``, and
``collected_bid_records`` joins the two on the minted ``bid_id`` — the row decides eligibility,
the projection supplies ``offer`` and ``store_domain``. Measured over the real socket in
``test_composition_root.py``, with the repair reverted and restored; the test PASSED both ways,
which is what had kept this silent::

    before T-349:  {"bid_id": "auction-f718...:store-a", "offer": {}, "store_id": "store-a"}
    after  T-349:  {"bid_id": "auction-55be...:s1", "store_domain": "s1.example.com",
                    "offer": {"checkout_url": "https://s1.example.com/cart/44352913:1",
                              "expires_at": "2999-01-01T00:00:00Z", ...}}

So a served bid of either kind now DOES face the pre-mint host comparison, and its code expires
with the offer rather than at the flat 48-hour ceiling. **None of that retires the post-mint
check or the orphan machinery below**, and that is the part not to lose: the permalink a
delegating provider hands *back* still does not exist until the provider has run, so it can
still leave the registered domain after ``POST /codes`` has issued a live single-use discount.
The pre-mint check gained a case; it did not become sufficient.

What that paragraph replaced said the pre-mint ``assert_on_domain`` does not run for a served
bid of either kind, that a fallback's derived ``expires_at`` never reaches this path, and that
``assert_offer_is_mintable`` and ``offer_quantity`` pass trivially on ``{}``. All three were
true and none is now: they described the ``offer: {}`` book, and T-349 is what changed it.
Driven live, a served bid's code TTL is now 60 s where the flat ceiling would have made it
172800 s. The claim that survives is the narrow one — the post-mint permalink check remains
the first failable host comparison for an offer that STILL arrives without a URL, which a
direct caller of :func:`accept` can supply and which a fallback for a store the registry
knows no domain for still is.

The buyer is still protected (no permalink is returned, and the auction stays open) and the
live code is **no longer lost**: the port carries it out on
:attr:`~apps.exchange.src.checkout.provider.OrphanedCheckoutCode.orphan`, and :func:`accept`
reads it and files a ``code_created`` event marked ``orphaned``, which is what makes the live
code visible to reconciliation and revocable *at all* — nothing in this repo consumes that
event yet, so "recorded", not "cleaned up", is the claim (T-202).
The code is deliberately absent from :attr:`AcceptResult.denial_reason` and from the persisted
refusal event, which name only a fingerprint that joins to that record (T-215). See
``test_accept.py::test_a_merchant_that_answers_off_domain_leaves_a_code_the_exchange_records``
and ``test_orphaned_code.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .. import describe_exception
from ..auction.ledger import build_published_event
from ..checkout import (
    CheckoutRequest,
    CheckoutResult,
    OrphanedCheckoutCode,
    OrphanedCode,
    assert_on_domain,
    code_fingerprint,
    registered_domain_for,
    resolve_provider,
)
from .claims import platform_acceptance_claims
from .reasons import (
    DENIAL_ALREADY_ACCEPTED,
    DENIAL_CHECKOUT_REFUSED,
    DENIAL_UNKNOWN_BID,
    DENIAL_UNRECORDABLE_ACCEPTANCE,
    DENIAL_UNROUTABLE_FALLBACK,
    denial_reason,
)

__all__ = [
    "ACCEPT_REFUSED",
    "NO_DISCOUNT_ON_A_FALLBACK",
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

#: What a buyer is told when the offer they accepted was the exchange's own list-price
#: fallback (R10) rather than a price a store quoted.
#:
#: It is a sentence rather than a flag because the flag already exists — ``code`` is ``None``
#: — and a ``null`` is not something a shopper reads. The ruling this implements is that a
#: silent store is treated as an ordinary merchant: no discount, and the buyer is sent to that
#: merchant's own checkout. Handing back a destination with a quiet ``null`` beside it would
#: leave a shopper expecting the discount the exchange usually negotiates, which is the one
#: outcome worse than the refusal this replaced.
NO_DISCOUNT_ON_A_FALLBACK = (
    "No discount applies to this offer. This store's agent never answered the exchange, so "
    "the price shown is the store's own catalogue list price rather than anything it quoted "
    "for you, and no discount code was created. You are being sent to the store's ordinary "
    "checkout, at its ordinary prices."
)

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

    .. warning::
       This module is reachable under **two** module names — ``exchange.accept.offer`` (through
       the tracked ``.pkgroot`` symlink, which is how the service imports it) and
       ``apps.exchange.src.accept.offer`` (the repo-root path the frozen acceptance suite uses)
       — and module state does not cross that boundary the way the relative imports and ``str``
       constants elsewhere in this tree do. Wire through the ``exchange.*`` name, which is the
       one :mod:`exchange.main` builds the app from. A caller that cannot be sure passes
       ``registered_domains=`` explicitly, which is unaffected.
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
    #: True means the one-accept guard for this accept was an atomic claim in a real store
    #: (:mod:`.claims`), so a concurrent second accept could not also have been told it won.
    #: False means no claim table was in force and the only guard was the ``accepted_bid_ref``
    #: stamp on the auction object this call was handed — which holds for exactly as long as
    #: that object does. Same purpose as ``domain_verified`` one field up, and the same lesson:
    #: T-158 was a guard that *looked* real, so "which guard actually ran" has to be a value
    #: somebody can read rather than something inferred from the wiring.
    claim_verified: bool = False
    #: Set on a **successful** accept that deliberately minted nothing, and it says why in
    #: words a shopper can read (:data:`NO_DISCOUNT_ON_A_FALLBACK`). ``None`` means the
    #: ordinary case: a store quoted a price and :attr:`code` carries the discount it agreed
    #: to. This is the positive half of the invariant one field down — ``code is None`` says
    #: nothing was minted, and this says the buyer was TOLD.
    discount_notice: str | None = None
    denial_reason: str | None = None
    #: The next slot to offer the buyer when this one could not be completed (A5).
    reoffer_bid_ref: str | None = None
    #: Set when this refusal happened AFTER the merchant had already issued a real
    #: single-use discount (T-202). It carries the code itself, so a caller that can reach
    #: the merchant's revoke API — none exists in this repo yet — would not have to parse it
    #: back out of an event. It is also the reason ``denial_reason`` may not name the code:
    #: the structured field is for a process that revokes, the prose is for the log, and
    #: only one of those is a safe place for a live discount. ``None`` means nothing was
    #: minted and there is nothing to revoke.
    #:
    #: Reading ``.code`` off this is the ONLY way to get the discount out of an
    #: ``AcceptResult``: ``OrphanedCode`` suppresses its generated ``repr`` (T-215), so
    #: formatting the result — which is what a log statement does — cannot publish it.
    orphaned_code: OrphanedCode | None = None
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


def _orphan_record(auction: Any, orphan: OrphanedCode) -> Mapping[str, Any]:
    """The ``code_created`` event for a code that exists but whose checkout was refused.

    This is the whole of T-202's fix, and it is a ``code_created`` for the plain reason that
    a code **was** created: the merchant's ``POST /codes`` returned before anything failed,
    the discount is live in the merchant's system, and ``code_created`` is the frozen kind
    (D24) whose published body — ``code``, ``permalink_url``, ``expires_at`` — is the one
    shaped to hold it. Inventing a kind is not open to the exchange, and filing the code in
    some second place would guarantee a future revoker had two places to look.

    **What this record is NOT, stated plainly: consumed.** There is no revocation path
    anywhere in this repository. ``git grep -ri revoke`` across ``apps``, ``packages`` and
    ``services`` outside this package returns SQL ``GRANT``/``REVOKE`` prose and
    key-rotation comments and nothing else, and the only occurrence of ``code_created``
    outside the exchange is a membership entry in the kind vocabulary at
    ``apps/trust/src/events/store.py``. So this event is a durable, joinable record that
    makes revocation *possible* and makes the orphan *visible to reconciliation* — the
    thing whose absence was the T-157/T-202 defect. Calling it "the revocation path" would
    be describing a consumer that does not exist, and an overclaim in a comment is how the
    next reader is misled into thinking the code is already being cleaned up.

    ``orphaned: True`` is what keeps the record honest. Without it a reconciler joining
    ``code_created`` to ``order_paid`` would read this as a checkout that simply never
    converted, rather than as one the exchange itself refused. The event is emitted
    **beside** the refusal, never instead of it, and no ``accepted`` or
    ``checkout_redirect`` accompanies it — the buyer was handed nothing, and the C11 trio
    describes a checkout that completed.
    """
    return build_published_event(
        "code_created",
        auction_id=str(_read(auction, "auction_id") or orphan.auction_id or ""),
        store_id=orphan.store_id or None,
        payload={
            # The published `code_created` body (D24).
            "code": orphan.code,
            "permalink_url": orphan.permalink_url,
            "expires_at": orphan.expires_at,
            # ...and what makes it actionable rather than merely recorded.
            "orphaned": True,
            "revocation_required": True,
            "bid_ref": orphan.bid_ref,
            "provider": orphan.provider,
            # The join to the refusal's redacted prose (T-215).
            "fingerprint": code_fingerprint(orphan.code),
        },
    )


def _fallback_destination(request: CheckoutRequest) -> str:
    """Where a shopper who accepted a list-price fallback is sent: the store's own checkout.

    Two functions do the work and neither is re-implemented here, which is the point of this
    being three lines:

    * :func:`~apps.exchange.src.checkout.provider.registered_domain_for` is the PLATFORM's
      answer to "what host does this seller live on", read through the same port the minting
      path reads it through — so a fallback's destination and a real accept's permalink can
      never name two different hosts for one store; and
    * :func:`~apps.exchange.src.ranking.candidates.fallback_checkout_url` builds the cart URL
      with no discount on it. Its docstring already carries the reason it is not
      ``build_cart_permalink``: *"A fallback's pre-accept URL is a destination, not an
      entitlement."* The ruling this implements is that it never stops being one — the URL
      the buyer was shown before accepting is the URL they are handed after, unchanged.

    Imported inside the function because ``ranking.serving`` already reaches the other way
    (``from ..accept.offer import platform_registered_domains``), and two module-level
    imports across that seam would be a cycle. The convention is this tree's own.

    **The built URL is then checked against the domain it was built from, and that third line
    is not ceremony.** ``fallback_checkout_url`` interpolates the registry value **verbatim**
    — deliberately, its docstring explains, because normalising would be that function
    quietly repairing the platform's own record. It then says the malformed result is caught
    downstream: *"the candidate is excluded ``off_domain_checkout``, which is the direction to
    fail in."* That is true of its ORIGINAL consumer, where ``ranking.filters.domain_reason``
    runs ``is_on_domain`` over the completed offer. It was **not** true here, and adding an
    unguarded second consumer of a deliberately-unnormalising builder is how that sentence
    became a lie. Measured on this function before the check was added, with the platform
    registry holding a malformed row::

        row 'good.tld:8080@evil.tld'  ->  200  https://good.tld:8080@evil.tld/cart/1:1
        row 'https://x.com'           ->  200  https://https://x.com/cart/1:1

    The first URL's effective host — the one a browser connects to — is ``evil.tld``, by the
    userinfo trick ``checkout/domain.py`` exists to defeat. The same rows are refused on the
    minting path (``checkout_refused: OrphanedOffDomainCheckout``), so the handoff was the
    weaker door of the two. A store cannot write a registry row, so this is the platform's own
    data integrity rather than a spoof anyone can mount — but "only the platform can trigger
    it" is a reason to fail closed cheaply, not a reason to skip the check. S8's rule is that
    no checkout URL is ever returned off the seller's registered domain, and a handoff URL is
    one the buyer's browser follows exactly like a minted permalink.

    Raises:
        Exception: whatever the registry lookup raises — typically ``OffDomainCheckout`` for
            a store the platform holds no domain for — or ``OffDomainCheckout`` from the
            check above when the row is malformed enough to move the URL's host. There is no
            fall back to the bid's own claim, because on a fallback the store never made one;
            see :attr:`CheckoutRequest.store_domain` at the call site, which is emptied for
            exactly this reason.
    """
    from ..ranking.candidates import fallback_checkout_url  # noqa: PLC0415

    domain = registered_domain_for(request)
    destination = fallback_checkout_url(domain)
    # `secret=""` because nothing has been minted and there is nothing to keep out of the
    # message — the same reason the port passes it empty on its own pre-mint call.
    assert_on_domain(destination, domain, what="fallback destination")
    return destination


def _handoff_events(
    auction_id: str,
    bid_ref: str,
    store_id: str,
    offer: Any,
    destination: str,
) -> tuple[Mapping[str, Any], ...]:
    """C11 for a handoff: the two events that happened, and pointedly not the third.

    A minting checkout emits :data:`~apps.exchange.src.checkout.CHECKOUT_EVENT_KINDS` —
    ``accepted``, ``code_created``, ``checkout_redirect``. This emits the first and the last.
    The absent ``code_created`` is not an omission to be tidied up later; it is the ledger
    saying, in the frozen vocabulary (D24) and where a reconciler can read it, that no
    discount was created for this acceptance. An event filed here to "keep the trio
    symmetrical" would be a record of a code that does not exist.

    ``checkout_token`` is ``None`` in both bodies for the same honesty: a token is the
    platform's handle on a checkout it is party to, and it is what
    ``trust.reconcile.engine`` joins an ``order_paid`` webhook back to. This exchange is not
    party to what the shopper does at the store's own till, so inventing a token would
    promise a join that can never be made.

    **This function cannot raise on a hostile ``offer``, and that is a fix rather than
    caution.** ``accept()`` reads the offer as ``_read(bid, "offer") or {}``, which is not a
    mapping check — a bid whose ``offer`` is a truthy non-mapping (a list, say) reaches here
    intact, and ``dict(offer)`` on it raised ``ValueError`` straight out of ``accept()``.
    Measured before the guard: the auction was already stamped and its claim already taken by
    then, so the buyer got a 500, no code, no destination and no re-offer, and every later
    accept on that auction answered ``already_accepted`` — one malformed bid record burning
    an auction permanently. The minting path handles the identical input as an ordinary
    refusal, so this was the handoff being *worse*, not the tree being uniformly fragile.
    Both halves are closed: the shape is coerced here, and the call site now builds these
    events BEFORE the stamp so that anything else that surprises us is a refusal rather than
    a lockout.
    """
    body = dict(offer) if isinstance(offer, Mapping) else {}
    return (
        build_published_event(
            "accepted",
            auction_id=auction_id,
            store_id=store_id,
            payload={
                # The published `accepted` body (D24).
                "bid_ref": bid_ref,
                "checkout_token": None,
                "offer": body,
                # ...and what makes it readable as the handoff it is.
                "fallback": True,
                "discount_applied": False,
            },
        ),
        build_published_event(
            "checkout_redirect",
            auction_id=auction_id,
            store_id=store_id,
            payload={
                # The published `checkout_redirect` body (D24).
                "checkout_token": None,
                "permalink_url": destination,
                # ...and the same two markers, so a consumer reading either event alone can
                # still tell a handoff from a checkout.
                "fallback": True,
                "discount_applied": False,
                # Carried because the module docstring above promises it is: "the
                # `checkout_redirect` event ... records that nothing external was consulted".
                # The minting path's redirect event carries this field and the handoff's did
                # not, which made that sentence false for exactly one of the two paths it
                # describes. Always `True` here — `_fallback_destination` refuses unless the
                # platform registry answered — but written rather than implied, because a
                # reader of one event should not have to know which path produced it.
                "domain_verified": True,
            },
        ),
    )


def _refusal_event(
    auction: Any,
    bid_ref: str,
    store_id: str | None,
    reason: str,
    orphan: OrphanedCode | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Record the refusal against D24's frozen vocabulary.

    A refused accept that leaves no trace is the one an operator cannot investigate, and
    there is no ``accept_refused`` LedgerEvent kind to invent one with — so it is filed as
    the ``policy_event`` it is, carrying that kind's published body plus the reason.

    When the refusal happened *after* a code was minted, the code's own record is emitted
    first — it happened first — and the refusal carries a pointer to it rather than the code.
    The split is deliberate and is T-215: ``reason`` is free prose in a payload the published
    API types as a bare string, so a live discount must not be in it, while
    ``orphaned_code.fingerprint`` and ``orphaned_code.event_id`` let an operator holding only
    the refusal walk straight to the ``code_created`` event that names the code itself.
    (An *operator*, today — see :func:`_orphan_record` on why calling that event's consumer
    "the revocation path" would be inventing one.)
    """
    events: list[Mapping[str, Any]] = []
    pointer: dict[str, Any] | None = None
    if orphan is not None:
        record = _orphan_record(auction, orphan)
        events.append(record)
        pointer = {
            "fingerprint": code_fingerprint(orphan.code),
            "event_id": record["event_id"],
            "kind": "code_created",
            "provider": orphan.provider,
        }

    payload: dict[str, Any] = {
        "kind": ACCEPT_REFUSED,
        "severity": "critical" if orphan is not None else "warning",
        "opened_at": float(_read(auction, "now") or 0.0),
        "bid_ref": bid_ref,
        "reason": reason,
    }
    if pointer is not None:
        payload["orphaned_code"] = pointer

    events.append(
        build_published_event(
            "policy_event",
            auction_id=str(_read(auction, "auction_id") or ""),
            store_id=store_id,
            payload=payload,
        )
    )
    return tuple(events)


def _refused(
    auction: Any,
    bid_ref: Any,
    mode: str,
    reason: str,
    *,
    store_id: str | None = None,
    reoffer_bid_ref: str | None = None,
    orphan: OrphanedCode | None = None,
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
        orphaned_code=orphan,
        events=_refusal_event(auction, ref, store_id, reason, orphan),
    )


def accept(
    auction: Any,
    bid_id: Any,
    code_creator: Any,
    mode: str,
    *,
    registered_domains: Any = _UNSET,
    claims: Any = _UNSET,
) -> AcceptResult:
    """Accept ``bid_id`` on ``auction`` and return the permalink the provider minted.

    The four positional parameters are the published signature (D45) and gain nothing when a
    new provider is registered: ``mode`` is the registry's selector and ``code_creator`` rides
    on the request. **All four are required**, with no defaults —
    ``test_checkout_provider.py::test_registering_a_further_provider_widens_nothing`` counts
    exactly this, and a defaulted ``mode`` would also mean a caller that forgot to pass
    ``CHECKOUT_MODE`` silently got the simulated path in a deployment configured for a real one.

    Args:
        auction: ``{auction_id, bids, accepted_bid_ref, now, ...}`` as a mapping or a record.
            It is **stamped** with the accepted bid ref on success, which is what makes the
            second accept a refusal.
        bid_id: which bid is being accepted.
        code_creator: the merchant ``POST /codes`` client, or ``None``. Used by the Shopify
            adapter only; the simulated redirect provider mints locally and never touches it.
        mode: ``CHECKOUT_MODE``. Resolved through the registry, which **raises** on a mode
            nobody registered rather than quietly running the simulated path.
        registered_domains: the platform's ``store_id -> registered domain`` lookup. Omitted,
            the process-wide source wired by :func:`use_registered_domains` is used; when
            nothing is wired at all the port falls back to the bid's own claim and the result
            says so via :attr:`AcceptResult.domain_verified`.
        claims: the acceptance-claim table (:mod:`.claims`) — the durable one-accept guard,
            taken before the merchant is asked to mint. Omitted, whatever
            :func:`~.claims.platform_acceptance_claims` reports is used: the table this
            request is scoped to (:func:`~.routes.accept_bid` opens one over its own app's
            store on every served accept), else the one wired by
            :func:`~.claims.use_acceptance_claims`, else **nothing**. There is deliberately no
            default table — see :mod:`.claims` on why a process-lifetime one is the wrong
            shape twice over — so a call with none in force runs on the object-scoped stamp
            alone, which is the pre-T-158 behaviour and is not safe on a money path.
            :attr:`AcceptResult.claim_verified` reports which of the two actually applied.
            Passing ``None`` explicitly forces that unguarded mode.

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
            denial_reason(
                DENIAL_UNKNOWN_BID,
                f"auction {str(_read(auction, 'auction_id') or '')!r} carries no bid {ref!r}; "
                f"there is nothing to accept",
            ),
            reoffer_bid_ref=next_slot(auction, ref),
        )

    store_id = str(_read(bid, "store_id") or "")

    already = _read(auction, _ACCEPTED_FIELD)
    if already:
        return _refused(
            auction,
            ref,
            mode,
            denial_reason(
                DENIAL_ALREADY_ACCEPTED,
                f"this auction was already accepted on bid {str(already)!r}; a second accept "
                f"issues no second discount code (R3/A5)",
            ),
            store_id=store_id,
        )

    # R10's list-price fallback is a HANDOFF, not a checkout — see `_fallback_destination`
    # and `_handoff_events`, and the branch further down that returns without minting.
    # Read here, once, off the flag `auction/routes.py::collected_bid_records` stamps from the
    # `BidEntry` (`collect_bids`' own verdict) and never off the store's document, so a store
    # cannot clear it by writing `fallback: false` into its reply.
    fallback = bool(_read(bid, "fallback"))

    if not _acceptance_is_recordable(auction):
        return _refused(
            auction,
            ref,
            mode,
            denial_reason(
                DENIAL_UNRECORDABLE_ACCEPTANCE,
                f"{type(auction).__name__} cannot record {_ACCEPTED_FIELD!r}, so a second "
                f"accept could not be refused; refusing the first rather than issuing a code "
                f"that cannot be made single-use",
            ),
            store_id=store_id,
        )

    # Resolved before the request is built: an unregistered mode must not reach the point of
    # having a request to mint from.
    provider = resolve_provider(mode)

    auction_id = str(_read(auction, "auction_id") or "")
    request = CheckoutRequest(
        auction_id=auction_id,
        bid_ref=ref,
        store_id=store_id,
        # The bid's CLAIM about its own domain. Kept because the frozen contract pins it and
        # because it is what the refusal message needs to quote — but overridden outright
        # whenever `registered_domains` below answers.
        #
        # For a FALLBACK it is dropped instead of kept, and that is not tidiness. With no
        # registry wired `registered_domain_for` returns this field, so it is the one door
        # through which a store's own word could become the host a shopper is sent to — and
        # on a fallback the store never spoke at all, so there is no word of its to honour.
        # Empty, that same function refuses (`_usable`), which is why a fallback destination
        # can only ever be the platform's own record.
        store_domain="" if fallback else str(_read(bid, "store_domain") or ""),
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

    # Where a fallback's shopper is being sent, settled HERE — before the claim, because
    # everything between the claim and the `try` below must be unable to raise, and this
    # lookup can. A destination that cannot be established is a refusal that has cost
    # nothing: no claim taken, no auction stamped, and the next slot offered instead.
    destination = ""
    handoff: tuple[Mapping[str, Any], ...] = ()
    if fallback:
        try:
            destination = _fallback_destination(request)
            # Built HERE, beside the lookup, and not after the stamp further down. Both are
            # the same kind of work — turning this bid into the answer the buyer gets — and
            # a failure in either must land in the same place: a refusal, with no claim
            # taken and no auction stamped. Building the events after `_record_acceptance`
            # is what let one malformed bid record burn an auction permanently; see
            # `_handoff_events`.
            handoff = _handoff_events(auction_id, ref, store_id, request.offer, destination)
        except Exception as exc:
            return _refused(
                auction,
                ref,
                mode,
                denial_reason(
                    DENIAL_UNROUTABLE_FALLBACK,
                    # The exception's TYPE and its message, never the value it was raised
                    # from (T-264). `registered_domain_for` already builds its own messages
                    # out of type names for exactly this reason.
                    f"bid {ref!r} is this exchange's own list-price fallback for {store_id!r} "
                    f"(R10), so the buyer is sent to that store's own checkout rather than "
                    f"handed a discount — but no usable destination could be established for "
                    f"it ({describe_exception(exc)})",
                ),
                store_id=store_id,
                reoffer_bid_ref=next_slot(auction, ref),
            )

    # THE guard (T-158), and its position in this function is the fix. Everything above is a
    # read or a pure construction; this is the first line that cannot be won twice, and it
    # runs BEFORE the merchant is asked for a code. A claim taken after the mint refuses the
    # second buyer a permalink and leaves the second live discount in the seller's account.
    #
    # It sits AFTER `resolve_provider` and after the request is built, and that ordering is
    # deliberate in both directions: everything above this line can raise (an unregistered
    # mode, an offer whose `now` will not parse) and a claim taken in front of a raise is a
    # claim nobody ever gives back — an auction wedged for a whole TTL by a deployment's own
    # misconfiguration. Nothing between here and the `try` below can raise.
    claim_table = platform_acceptance_claims() if claims is _UNSET else claims
    claimed = False
    if claim_table is not None:
        try:
            # `outcome.won` is INSIDE this try, not after it. `claims.py` invites a deployment
            # to inject its own table — "a unique index on (auction_id) in Postgres is the
            # same constraint by another name" — the port is a `Protocol` and so is not
            # runtime-checked, and a table answering a bare `True` used to raise
            # `AttributeError: 'bool' object has no attribute 'won'` straight out of the ASGI
            # app: an HTTP 500 with the claim taken and never released. Guarding the call but
            # not the shape of its answer was guarding half of it.
            #
            # An auction with no id is refused here rather than waved through. There is
            # nothing to key a claim on, so a second accept could not be refused — the same
            # condition `_acceptance_is_recordable` refuses above, and refusing it is what
            # keeps "a claim table is in force" from silently meaning "for auctions that
            # happen to have an id". Not reachable through the route (Starlette's path
            # convertor rejects an empty segment and `POST /auctions` mints
            # `auction-{uuid4()}`), which is why this is fail-closed rather than a 503.
            if not auction_id:
                raise ValueError("the auction carries no auction_id to claim")
            outcome = claim_table.claim(auction_id, ref)
            won = bool(outcome.won)
            holder = str(outcome.holder or "")
        except Exception as exc:
            # Fail CLOSED, and reuse the reason that already says why: an acceptance that
            # cannot be recorded cannot refuse the second accept either, so refusing the
            # first beats issuing a code that cannot be made single-use. A claim table that
            # is unreachable is indistinguishable from one that would have said "taken".
            return _refused(
                auction,
                ref,
                mode,
                denial_reason(
                    DENIAL_UNRECORDABLE_ACCEPTANCE,
                    # The exception's TYPE and nothing else. `{exc}` here would publish
                    # whatever an injected claim table put in its message into a persisted,
                    # client-visible `policy_event` payload — the T-264/T-326 shape, where a
                    # default `__repr__` renders a memory address into the 409 body. The type
                    # name is what an operator needs and is not attacker-authored prose.
                    f"the acceptance claim for auction {auction_id!r} could not be taken "
                    f"({type(exc).__name__}), so a second accept could not be refused; "
                    f"refusing the first rather than issuing a code that cannot be made "
                    f"single-use",
                ),
                store_id=store_id,
            )
        if not won:
            return _refused(
                auction,
                ref,
                mode,
                denial_reason(
                    DENIAL_ALREADY_ACCEPTED,
                    f"this auction was already accepted on bid {holder!r}; a second accept "
                    f"issues no second discount code (R3/A5)",
                ),
                store_id=store_id,
            )
        claimed = True

    if fallback:
        # THE HANDOFF. The claim above has closed the auction, so this stamp is the second
        # line exactly as it is on the minting path below, and it is taken for the same
        # reason: accepting a fallback IS an acceptance of this auction, and a buyer who took
        # the handoff must not then be able to accept a real bid and collect a code as well.
        #
        # `provider` is deliberately never asked for anything. It was resolved above so that
        # an unregistered `CHECKOUT_MODE` is still a deployment error rather than a silently
        # different answer for fallbacks — but a provider's job is to MINT, and there is
        # nothing here to mint. That is the whole invariant, and it holds by construction
        # rather than by configuration: no code_creator is reached, no `POST /codes` is made,
        # and no branch below this line can put a value in `code`.
        _record_acceptance(auction, ref)
        return AcceptResult(
            accepted=True,
            auction_id=auction_id,
            bid_ref=ref,
            mode=str(mode),
            store_id=store_id,
            permalink_url=destination,
            code=None,
            checkout_token=None,
            expires_at=None,
            provider=None,
            # True, and earned rather than assumed: `_fallback_destination` refuses unless
            # the PLATFORM's registry answered, and the bid's own `store_domain` was emptied
            # out of the request above so it could not have answered in the registry's place.
            domain_verified=True,
            claim_verified=claimed,
            discount_notice=NO_DISCOUNT_ON_A_FALLBACK,
            events=handoff,
        )

    try:
        checkout: CheckoutResult = provider.checkout(request)
    except Exception as exc:
        # Deliberately broad, and it stays broad. Everything reachable here is a refusal of
        # THIS offer — an off-domain checkout URL, an unusable offer field, a merchant
        # `POST /codes` that answered with no code or did not answer at all — and A5's
        # promise is that the buyer gets the next slot rather than a stack trace. Narrowing
        # it to the orphan case would turn every other refusal into a 500.
        #
        # What is NOT true, and what T-157 measured false, is the comforting half of the old
        # comment here: "a refusal from any of them has created no code anywhere". The port
        # hoists every check it can ahead of the mint, but one cannot be hoisted — the
        # permalink a provider hands back does not exist until the provider has run, and a
        # pre-mint check on the offer's own URL says nothing about it. When T-157 was
        # measured, the R10 list-price fallback offer carried no `checkout_url` at all, which
        # made that post-mint check the FIRST failable host comparison for a legal, everyday
        # bid. A doc sweep (edbc422) then recorded that this had stopped being true ("a
        # shortlisted fallback now arrives with a URL, so the pre-mint check does run on
        # it"). That was premature when it was written and is accurate now, for a reason
        # worth keeping: `ranking/candidates.py` does complete a shortlisted fallback's offer
        # from the platform's `store_id -> domain` registry (`candidates.py:247`), and the
        # route does record the bid book after the ranking — but the route did not hand that
        # projection over. `auction/routes.py` passed `ranking["candidates"]` alone to
        # `collected_bid_records`, and `ranking/__init__.py` sets `"candidates": rows`, the
        # rank-ROW projection: `bid_id`, `components`, `eligible`, `exclusion_reasons`,
        # `features`, `price`, `provenance_labels`, `rank_score`, `store_id`, `trust`,
        # `trust_summary`, `verified_hard_fit_count` — no `offer`, no `store_domain` — so
        # `candidate.get("offer") or {}` recorded the EMPTY DICT for a hosted bid and for a
        # fallback alike, and the pre-mint check ran on neither.
        #
        # T-349 closed that: `rank_auction` also returns the projection under `"projected"`
        # and `collected_bid_records` joins the two on `bid_id`, so a recorded bid now
        # carries its real `checkout_url`, `expires_at` and `store_domain`. Measured over the
        # real socket, reverted and restored — and the composition test PASSED both ways,
        # which is what had kept this silent:
        #
        #     reverted:  offer_keys=[]                  store_domain=None
        #     restored:  offer_keys=['checkout_url', ...]  store_domain='s1.example.com'
        #
        # WHAT HAS NOT CHANGED, and it is the whole reason this handler stays broad: the
        # post-mint refusal is still reachable — by any provider whose permalink leaves the
        # registered domain, and by any offer that still arrives with no URL to check. By
        # then `POST /codes` has issued a live single-use discount. The pre-mint check gained
        # a case; it did not become sufficient.
        #
        # The port carries that code out on `OrphanedCheckoutCode.orphan`. Reading it is this
        # frame's entire job: dropping it here is the original defect, one frame higher up.
        orphan = exc.orphan if isinstance(exc, OrphanedCheckoutCode) else None
        # A5: a refused accept re-offers the next slot, so the auction must be acceptable
        # again — which means giving the claim back. `release` is a no-op unless this call
        # still holds it, so a claim that changed hands is never handed to the wrong caller.
        if claimed and claim_table is not None and orphan is None:
            try:
                claim_table.release(auction_id, ref)
            except Exception:  # noqa: BLE001 - a claim we cannot release stays taken, which
                # is the fail-closed direction: the auction is refusable rather than
                # re-mintable, and the refusal below is what the buyer is told either way.
                pass
        return _refused(
            auction,
            ref,
            mode,
            # `{exc}` is safe to persist BECAUSE `OrphanedCheckoutCode` redacts its own
            # message (T-215) — and redacts it STRUCTURALLY: every URL it holds as a value
            # is reduced to its scheme and host, so the merchant's spelling of the code
            # inside a path or query cannot survive whatever encoding it chose. This reason
            # lands in a `policy_event` payload the published API types as a bare string;
            # the code itself goes to the `code_created` event `_refused` emits alongside,
            # which is the only place it is written down.
            # THE AMPLIFIER. Every exception `checkout/*` raises funnels through this one
            # line into `denial_reason`, so an address quoted anywhere under that package
            # reaches the persisted event and the 409 from here. `describe_exception`
            # keeps the class name and the message and drops only the address.
            denial_reason(DENIAL_CHECKOUT_REFUSED, describe_exception(exc)),
            store_id=store_id,
            reoffer_bid_ref=next_slot(auction, ref),
            orphan=orphan,
        )

    # The claim above is what closed the auction to further accepts, and it is kept: a
    # successful accept never releases it. This stamp is the SECOND line — it is what a
    # caller holding the object (and a route that saves it back) reads, and what keeps the
    # refusal correct for a caller that wired no claim table at all.
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
        claim_verified=claimed,
        events=tuple(checkout.events),
    )
