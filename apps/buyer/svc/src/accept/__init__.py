"""``buyer_svc.accept`` — R2's shortlist labels and R3's checkout handoff (T-072).

    >>> from apps.buyer.svc.src.accept import provenance_label, accept
    >>> provenance_label({"source": "scraped"})
    'from their website'
    >>> provenance_label({"source": "owner_statement"})
    'store-confirmed'
    >>> accepted = accept(slot, exchange)          # ONE call to the exchange
    >>> accepted.permalink_url                     # byte-for-byte what it answered
    'https://store.example.com/cart/44352913:1?discount=PS-ABC123'

Two SPEC clauses live here, and each one is a promise about *authority*:

* **R2 — provenance labels.** ``"store-confirmed"`` for evidence a store stands behind,
  ``"from their website"`` for evidence observed on its public site, and neither of those
  for a ``seller_asserted`` claim, which carries R18's ``"unverified"`` badge instead. The
  table itself is ``contracts.labels``', imported and never restated (D30), so the exchange
  that *produces* ``Shortlist.slots[].provenance_labels`` and this package that *renders*
  them cannot drift into two answers. See :mod:`buyer_svc.accept.labels`.
* **R3 — the exchange owns the checkout URL.** :func:`accept` asks the exchange and returns
  what it was given, unmodified. There is no URL construction anywhere in this package —
  not for the checkout, and not for the exchange's own API either. See
  :mod:`buyer_svc.accept.handoff`, and :mod:`buyer_svc.accept.permalink` for the redirect
  guard that decides whether the exchange's answer is somewhere a browser may be sent.

What the neighbouring tickets consume
-------------------------------------
``T-073`` (``buyer_svc.feedback``) reads this surface and never reaches inside it:

``AcceptedOffer.auction_id`` / ``.bid_ref``
    the auction and the bid an order came from — R14's "was this order network-routed?"
    is answered by whether an accept receipt exists for it.
``AcceptedOffer.to_dict()``
    ``{permalink_url, auction_id, bid_ref, slot, accepted_at, called}``. It is a **narrow**
    projection on purpose and never echoes the shortlist slot it was built from.
``accepted()`` / ``reset_accepted()``
    the process-wide accept ledger, and the way a test empties it.
``AcceptError`` and its subclasses
    every refusal here is one of them, and none of them is a ``TypeError``/``ValueError``/
    ``KeyError``, so a domain refusal is distinguishable from a broken call — the same
    convention ``buyer_svc.intent`` follows with ``IntentError``.

Imports inside this package are relative on purpose; see :mod:`buyer_svc.accept._spellings`
for what goes wrong otherwise (this tree is reachable both as ``buyer_svc.accept`` and as
``apps.buyer.svc.src.accept``, and an absolute import silently picks one).
"""

from __future__ import annotations

from ._spellings import bind_package
from .errors import (
    AcceptError,
    AcceptRefusedByExchange,
    ExchangeClientUnusable,
    MissingAuctionReference,
    NoPermalinkReturned,
    OffDomainPermalink,
    OfferAlreadyAccepted,
    UnknownProvenanceSource,
    UnsafePermalink,
    UnusableSlot,
)
from .handoff import (
    EXCHANGE_ACCEPT_METHODS,
    PERMALINK_FIELDS,
    AcceptedOffer,
    AcceptLedger,
    accept,
    accepted,
    reset_accepted,
)
from .labels import (
    BUYER_PROVENANCE_LABELS,
    LABEL_FROM_THEIR_WEBSITE,
    LABEL_STORE_CONFIRMED,
    LABEL_UNVERIFIED,
    LABELS_ABSENT,
    LABELS_DERIVED,
    LABELS_SUPPLIED,
    PROVENANCE_BUYER_LABELS,
    LabelledSlot,
    label_slot,
    labels_for_claims,
    provenance_label,
    render_shortlist,
    slot_labels,
)
from .permalink import (
    ALLOWED_PERMALINK_SCHEMES,
    permalink_host,
    reason_unsafe,
    verify_permalink,
)

__all__ = [
    "ALLOWED_PERMALINK_SCHEMES",
    "BUYER_PROVENANCE_LABELS",
    "EXCHANGE_ACCEPT_METHODS",
    "LABELS_ABSENT",
    "LABELS_DERIVED",
    "LABELS_SUPPLIED",
    "LABEL_FROM_THEIR_WEBSITE",
    "LABEL_STORE_CONFIRMED",
    "LABEL_UNVERIFIED",
    "PERMALINK_FIELDS",
    "PROVENANCE_BUYER_LABELS",
    "AcceptError",
    "AcceptLedger",
    "AcceptRefusedByExchange",
    "AcceptedOffer",
    "ExchangeClientUnusable",
    "LabelledSlot",
    "MissingAuctionReference",
    "NoPermalinkReturned",
    "OffDomainPermalink",
    "OfferAlreadyAccepted",
    "UnknownProvenanceSource",
    "UnsafePermalink",
    "UnusableSlot",
    "accept",
    "accepted",
    "label_slot",
    "labels_for_claims",
    "permalink_host",
    "provenance_label",
    "reason_unsafe",
    "render_shortlist",
    "reset_accepted",
    "slot_labels",
    "verify_permalink",
]

# LAST, and it is not decoration: this tree is importable as `buyer_svc.accept` and as
# `apps.buyer.svc.src.accept`, and without this Python executes every file here TWICE —
# once per spelling — leaving two `OffDomainPermalink` classes that do not catch each other
# and TWO accept ledgers, so "one auction gets one checkout" would hold only per spelling.
# Measured on this worktree: `apps.buyer.svc.src.accept is buyer_svc.accept` was False.
# See `_spellings.py`.
bind_package(__name__)
