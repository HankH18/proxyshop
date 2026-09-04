"""R3: the buyer follows the exchange's permalink, and mints nothing (T-072).

SPEC R3 is a rule about *authority*, not about string formatting. There is exactly one
party allowed to say where a buyer's checkout is — the exchange, which minted the
single-use discount code that the permalink pre-applies (R3/C5) — and this module's only
job is to ask it and then not improvise.

That is why :func:`accept` has no URL construction in it anywhere. Not "does not currently
build one": there is no template, no ``urljoin``, no f-string with a host in it, and the
one URL-shaped value it handles is the one that came off the exchange's response. The
frozen acceptance suite tests this adversarially — the slot it passes carries a
``checkout_url`` of ``https://attacker.example/cart/1:1?discount=PS-ABC123``, a decoy that
spells exactly what a buyer minting its own URL from ``variant_id`` and ``discount_code``
would produce — and :class:`AcceptedOffer` is built so that value cannot reach a caller by
accident either: :meth:`AcceptedOffer.to_dict` publishes named fields and never echoes the
slot it was given.

The measured hazard this module was written around
--------------------------------------------------
``buyer_svc.intent.confirm`` returns an ``AuctionCreated`` whose ``auction_id`` is the
**empty string** when the exchange accepted the auction but returned no id — the auction
genuinely exists, T-071 logs a WARNING and proceeds. A shortlist slot built from that
receipt carries ``auction_id=""``. Accepting it must not become a request the exchange can
only guess at, and must certainly not become a redirect to a URL with a hole in it, so it
is a :class:`~buyer_svc.accept.errors.MissingAuctionReference` refusal *before* the client
is touched. Same for a blank ``bid_ref``: "accept the offer" with no offer named is not a
request, it is a bug upstream, and the honest thing is to say so rather than to send it.

One accept per auction
----------------------
Recorded in an :class:`AcceptLedger` before the client is called, and released if the call
itself fails so a genuine retry after a network error still works. This is the same shape
T-071 gives confirmation, for the same reason and with the same honesty about its limits:
it is process-local, it is not a distributed lock, and the exchange's own double-accept
refusal (E3: "a double accept on one auction must not issue a second permalink") is what
actually holds. What this closes is the common case — one process, one buyer, two clicks.

The client contract
-------------------
``exchange_client`` is whatever talks to ``POST /auctions/{auction_id}/accept``. It may
expose any of :data:`EXCHANGE_ACCEPT_METHODS`, or be a bare callable, and it is called
**exactly once** with **one positional mapping**::

    {"auction_id": "auc-e7-3", "bid_ref": "bid-e7-7"}

The auction id rides in the body as well as (presumably) the path because the client owns
the URL shape and this module must not: constructing ``/auctions/{id}/accept`` here would
be a second place that knows the exchange's routing. Nothing in this package builds a URL —
that is the whole rule, and it applies to the API URL as much as to the checkout one.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ._reading import read, text
from .errors import (
    AcceptRefusedByExchange,
    ExchangeClientUnusable,
    MissingAuctionReference,
    NoPermalinkReturned,
    OfferAlreadyAccepted,
    UnusableSlot,
)
from .permalink import verify_permalink

_log = logging.getLogger(__name__)

__all__ = [
    "EXCHANGE_ACCEPT_METHODS",
    "PERMALINK_FIELDS",
    "AcceptLedger",
    "AcceptedOffer",
    "accept",
    "accepted",
    "reset_accepted",
]

#: Method names an exchange client may expose, most specific first. The first callable one
#: wins; a bare callable client is the last resort. Mirrors
#: ``buyer_svc.intent.confirmation.AUCTION_CLIENT_METHODS``.
EXCHANGE_ACCEPT_METHODS: tuple[str, ...] = (
    "accept_offer",
    "accept_bid",
    "accept",
    "post_accept",
    "create_checkout",
    "checkout",
)

#: Where a permalink may be found on the exchange's answer, in order. ``permalink_url`` is
#: the spelling the frozen OpenAPI document and the E3 suite both use; the rest are
#: tolerated because this is a wire boundary, not because they are equally correct.
PERMALINK_FIELDS: tuple[str, ...] = ("permalink_url", "permalink", "permalinkUrl", "url")


@dataclass(frozen=True, slots=True)
class AcceptedOffer:
    """The receipt for one accepted offer — and the buyer's next destination.

    ``permalink_url`` is byte-for-byte what the exchange returned.

    :meth:`to_dict` is the **published** projection and is deliberately narrower than the
    object: it names the fields a screen or a log line needs and echoes nothing that came in
    on the slot. That is not tidiness. A slot may carry a ``checkout_url`` the buyer must
    never follow (R3), and a receipt that round-tripped it would put an attacker-chosen URL
    one careless template away from a link. ``response`` — the exchange's raw answer, kept
    for callers that need the discount code or the denial reason — is reachable as an
    attribute and is not part of that projection for the same reason.
    """

    permalink_url: str
    auction_id: str
    bid_ref: str
    slot: str
    accepted_at: str
    called: str
    response: Any = None

    def to_dict(self) -> dict[str, Any]:
        """The buyer-facing projection. Never echoes the slot it was built from."""
        return {
            "permalink_url": self.permalink_url,
            "auction_id": self.auction_id,
            "bid_ref": self.bid_ref,
            "slot": self.slot,
            "accepted_at": self.accepted_at,
            "called": self.called,
        }

    def __getitem__(self, key: str) -> Any:
        """Subscriptable as well as attribute-addressed, like T-071's ``ClarifyOutcome``."""
        try:
            return self.to_dict()[key]
        except KeyError:
            raise KeyError(key) from None


class AcceptLedger:
    """Which auctions this process has already handed off, and to which permalink."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._permalinks: dict[str, str] = {}

    def claim(self, auction_id: str) -> None:
        """Reserve ``auction_id``. Raises if it was already accepted."""
        with self._lock:
            if auction_id in self._permalinks:
                issued = self._permalinks[auction_id]
                raise OfferAlreadyAccepted(
                    f"auction {auction_id!r} has already been accepted; one auction gets one "
                    f"checkout. Follow the permalink that was already issued rather than "
                    f"asking the exchange for a second one.",
                    issued,
                )
            self._permalinks[auction_id] = ""

    def record(self, auction_id: str, permalink_url: str) -> None:
        with self._lock:
            self._permalinks[auction_id] = permalink_url

    def release(self, auction_id: str) -> None:
        """Give an unspent claim back, so a failed accept can honestly be retried."""
        with self._lock:
            if self._permalinks.get(auction_id) == "":
                del self._permalinks[auction_id]

    def permalink_for(self, auction_id: str) -> str | None:
        with self._lock:
            return self._permalinks.get(auction_id)

    def reset(self) -> None:
        with self._lock:
            self._permalinks.clear()


_LEDGER = AcceptLedger()


def accepted() -> AcceptLedger:
    """The process-wide ledger :func:`accept` uses when none is injected."""
    return _LEDGER


def reset_accepted() -> None:
    """Forget every accept. For tests and for a fresh process only."""
    accepted().reset()


def accept(
    slot: Any,
    exchange_client: Any,
    *,
    expected_domain: str | None = None,
    ledger: AcceptLedger | None = None,
    now: datetime | None = None,
) -> AcceptedOffer:
    """Accept one shortlist slot and hand back the exchange's permalink (R3).

    Args:
        slot: the shortlist slot the buyer chose — a ``ShortlistSlot``, its ``dict``, or a
            :class:`~buyer_svc.accept.labels.LabelledSlot`. It must name an ``auction_id``
            and a ``bid_ref``. Its ``checkout_url``, if it has one, is **ignored**.
        exchange_client: whatever talks to ``POST /auctions/{id}/accept``. Any object
            exposing one of :data:`EXCHANGE_ACCEPT_METHODS`, or a bare callable. Called
            exactly once.
        expected_domain: the store domain the buyer was shown, for the redirect check.
            ``None`` (the default) means "use the slot's ``store_domain`` if it carries
            one"; an explicit ``""`` means "no host constraint". Never taken from the
            slot's ``checkout_url`` — that value is not authority for anything (R3).
        ledger: the accept ledger; defaults to the process-wide one.
        now: timestamp for the receipt.

    Returns:
        :class:`AcceptedOffer`, whose ``permalink_url`` is the exchange's answer unmodified.

    Raises:
        UnusableSlot: this is not a shortlist slot.
        MissingAuctionReference: the slot names no auction or no bid. **Nothing was
            called.** See this module's docstring for why an empty ``auction_id`` is a real
            input rather than a hypothetical one.
        OfferAlreadyAccepted: this auction was already handed off in this process.
        ExchangeClientUnusable: there is no way to accept on this client.
        AcceptRefusedByExchange: the exchange said no, with a reason.
        NoPermalinkReturned: the exchange said yes and returned no permalink.
        UnsafePermalink / OffDomainPermalink: the exchange's permalink is not somewhere a
            browser may be sent.
    """
    auction_id, bid_ref, slot_name, slot_domain = _slot_reference(slot)

    domain = slot_domain if expected_domain is None else str(expected_domain)

    book = ledger if ledger is not None else accepted()
    book.claim(auction_id)

    try:
        name, method = _accept_entrypoint(exchange_client)
        response = method({"auction_id": auction_id, "bid_ref": bid_ref})
    except Exception:
        # No checkout was issued, so the claim was not spent. Give it back, or a transient
        # exchange outage would permanently refuse this buyer's chosen offer.
        book.release(auction_id)
        raise

    try:
        _refuse_if_denied(response, auction_id=auction_id, bid_ref=bid_ref)
        raw = _permalink_of(response)
        if raw is None:
            raise NoPermalinkReturned(
                f"the exchange answered the accept for auction {auction_id!r} / bid "
                f"{bid_ref!r} without a permalink (looked for {list(PERMALINK_FIELDS)} on "
                f"{type(response).__name__}). R3 forbids the buyer minting one of its own, "
                f"so this buyer is not being sent anywhere."
            )
        permalink = verify_permalink(raw, domain, what="the exchange's checkout permalink")
    except Exception:
        book.release(auction_id)
        raise

    book.record(auction_id, permalink)
    if not domain:
        # Worth a line in the log rather than nothing: on today's `ShortlistSlot` contract
        # there is no store domain to compare against, so the host check ran with scheme
        # and host-presence only. A composition root that knows the domain should pass it.
        _log.debug(
            "accepted auction %s with no expected store domain; the permalink host was not "
            "pinned to a store",
            auction_id,
        )
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return AcceptedOffer(
        permalink_url=permalink,
        auction_id=auction_id,
        bid_ref=bid_ref,
        slot=slot_name,
        accepted_at=moment.isoformat().replace("+00:00", "Z"),
        called=name,
        response=response,
    )


def _slot_reference(slot: Any) -> tuple[str, str, str, str]:
    """``(auction_id, bid_ref, slot_name, store_domain)`` off a slot, or refuse."""
    if slot is None or isinstance(slot, (str, bytes, int, float, bool)):
        raise UnusableSlot(
            f"accept() takes the shortlist slot the buyer chose, not {type(slot).__name__} "
            f"({slot!r}). Pass the slot object or its dict — it is what names the auction "
            f"and the bid."
        )
    auction_id = text(read(slot, "auction_id", ""))
    bid_ref = text(read(slot, "bid_ref", ""))
    missing = [
        field for field, value in (("auction_id", auction_id), ("bid_ref", bid_ref)) if not value
    ]
    if missing:
        raise MissingAuctionReference(
            f"the shortlist slot names no {' and no '.join(missing)}, so there is nothing "
            f"for the exchange to accept. An empty auction_id is a real value here: "
            f"buyer_svc.intent.confirm returns one when the exchange created the auction "
            f"but answered without an id, and following a checkout for it would mean "
            f"guessing which auction the buyer meant."
        )
    return auction_id, bid_ref, text(read(slot, "slot", "")), text(read(slot, "store_domain", ""))


def _accept_entrypoint(exchange_client: Any) -> tuple[str, Any]:
    """The one callable that accepts an offer on this client."""
    if exchange_client is None:
        raise ExchangeClientUnusable(
            "accept() was given no exchange client, so there is nobody to ask for a "
            "checkout permalink. Pass the client that owns POST /auctions/{id}/accept."
        )
    for name in EXCHANGE_ACCEPT_METHODS:
        candidate = getattr(exchange_client, name, None)
        if callable(candidate):
            return name, candidate
    if callable(exchange_client):
        return "__call__", exchange_client
    raise ExchangeClientUnusable(
        f"the exchange client {type(exchange_client).__name__} exposes none of "
        f"{list(EXCHANGE_ACCEPT_METHODS)} and is not callable, so this offer cannot be "
        f"accepted."
    )


def _refuse_if_denied(response: Any, *, auction_id: str, bid_ref: str) -> None:
    """Turn the exchange's 409 body into a refusal with its reason attached.

    ``{"accepted": false, "denial_reason": "blacklist"}`` is a documented answer
    (``packages/contracts/openapi/exchange.openapi.json``), and a client that returns the
    parsed body rather than raising for status hands it straight here. ``accepted is False``
    rather than ``not accepted``: a client that omits the field entirely on the happy path
    must not be read as a denial.
    """
    decided = read(response, "accepted", None)
    reason = text(read(response, "denial_reason", ""))
    if decided is False or (decided is None and reason):
        raise AcceptRefusedByExchange(
            f"the exchange refused the accept for auction {auction_id!r} / bid {bid_ref!r}"
            + (f": {reason}" if reason else " and gave no reason"),
            reason,
        )


def _permalink_of(response: Any) -> Any:
    """The permalink off whatever the exchange handed back, or ``None`` if it carries none.

    ``None`` and "an empty string" are deliberately different answers: the first means the
    field was absent and the second means the exchange sent an empty one. Both refuse, but
    only through :func:`~buyer_svc.accept.permalink.verify_permalink`, so exactly one place
    decides what an unusable permalink is.
    """
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        for key in PERMALINK_FIELDS:
            if key in response:
                return response[key]
        return None
    for key in PERMALINK_FIELDS:
        value = getattr(response, key, None)
        if value is not None:
            return value
    return None
