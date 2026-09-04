"""Refusals this package raises (T-072).

Every one of them is a **domain** refusal: the call was well formed and the answer is no.
None of them is a ``TypeError``, ``ValueError``, ``KeyError`` or ``AttributeError``, and
that is the whole point of the hierarchy — a caller catching :class:`AcceptError` catches
"the buyer would not follow this handoff" and nothing else, while a genuinely broken call
(the wrong number of arguments, a typo'd attribute) still surfaces as the builtin it is.
T-071's :mod:`buyer_svc.intent.errors` makes the same promise for the clarification loop
and this package follows it deliberately, so a consumer that already handles ``IntentError``
handles this surface the same way.

:class:`UnsafePermalink` is a subclass of :class:`AcceptError` and *not* of ``ValueError``,
which differs from the exchange's own ``exchange.checkout.domain.OffDomainCheckout``
(a ``ValueError``). The two guards are separate on purpose — see :mod:`.permalink`.
"""

from __future__ import annotations

__all__ = [
    "AcceptError",
    "AcceptRefusedByExchange",
    "ExchangeClientUnusable",
    "MissingAuctionReference",
    "NoPermalinkReturned",
    "OffDomainPermalink",
    "OfferAlreadyAccepted",
    "UnknownProvenanceSource",
    "UnsafePermalink",
    "UnusableSlot",
]


class AcceptError(RuntimeError):
    """Base class for every refusal in :mod:`buyer_svc.accept`."""


class UnusableSlot(AcceptError):
    """The thing handed to :func:`~buyer_svc.accept.handoff.accept` is not a shortlist slot."""


class MissingAuctionReference(AcceptError):
    """The slot names no auction (or no bid), so the exchange cannot be told what to accept.

    This is the refusal for the measured T-071 defect: ``confirm()`` returns an
    ``AuctionCreated`` whose ``auction_id`` is the empty string when the exchange accepted
    the auction but returned no id (it logs a WARNING and proceeds, because the auction
    genuinely exists). A shortlist slot built from that receipt carries ``auction_id=""``,
    and accepting it would POST an accept for auction ``""`` — a request the exchange can
    only answer with a 404 or, worse, guess at. Refusing here keeps the buyer's browser
    where it is instead of redirecting it somewhere malformed.
    """


class OfferAlreadyAccepted(AcceptError):
    """This auction has already been handed off; a second accept is refused.

    The exchange refuses a double accept as well (E3: "a double accept on one auction must
    not issue a second permalink"), and that is the authoritative check. This one is local,
    cheap, and closes the common case — a double-clicked Accept button — before a second
    request leaves the buyer at all.

    :attr:`permalink_url` carries the permalink the first accept was given, so a screen
    handling this refusal can send the buyer to the checkout they already have instead of
    showing them an error about a thing that worked.
    """

    def __init__(self, message: str, permalink_url: str = "") -> None:
        super().__init__(message)
        self.permalink_url = permalink_url


class AcceptRefusedByExchange(AcceptError):
    """The exchange answered, and its answer was no.

    Distinct from :class:`NoPermalinkReturned` on purpose. The exchange's 409 body is
    ``{"accepted": false, "denial_reason": "..."}`` (``packages/contracts/openapi/
    exchange.openapi.json``) — a decision, with a reason the buyer can be shown. A response
    that merely lacks a permalink is a malformed success, which is a different problem and
    gets a different message.
    """

    def __init__(self, message: str, denial_reason: str = "") -> None:
        super().__init__(message)
        self.denial_reason = denial_reason


class ExchangeClientUnusable(AcceptError):
    """No client was supplied, or the one supplied exposes no way to accept an offer."""


class NoPermalinkReturned(AcceptError):
    """The exchange answered without a checkout permalink.

    A refusal rather than a fallback. R3 says the buyer follows the exchange's permalink and
    never mints one of its own, so "the exchange gave me nothing" has exactly one honest
    answer, and it is not "build a URL out of the slot".
    """


class UnsafePermalink(AcceptError):
    """The exchange's permalink is not something a browser may be redirected to."""


class OffDomainPermalink(UnsafePermalink):
    """The permalink's host is not the store domain the buyer was shown."""


class UnknownProvenanceSource(AcceptError):
    """A provenance source outside the pinned enum has no published buyer-facing label."""
