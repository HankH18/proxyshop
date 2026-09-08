"""Which impression a trust movement came from — the join that ties a pitch to an outcome.

A trust delta says *that* a store's posture moved and *why* in one word (``reason_code``). It
does not say **which of the store's own decisions earned it**, and that is the fact a store
agent needs in order to learn: its policy is keyed by cluster and discount rung
(``store_agent.learning.state.sample_depth``), so "your ``feedback_match`` went up 0.03" is an
aggregate it cannot attribute to an arm. It can attribute "the offer you pitched at a 20%
discount took the ``value`` slot in this auction, converted, and the customer said it matched."

WHAT THIS JOINS, AND WHY IT COSTS NOTHING
-----------------------------------------
Every row it reads is already in memory. :func:`~.deltas.delta_for_event` is handed the affected
store's prior ledger rows — read once, index-backed on ``(store_id, seq)`` and bounded by
:data:`~.deltas.MAX_DELTA_HISTORY_EVENTS` — and scores them twice. This module walks that SAME
list. It opens no connection, issues no query, and adds no round trip to ``POST /events``.

That is not a happy accident, it is why the join is done here rather than in a service that
would have to fetch the chain again. Measured on the live ledger, the four kinds it needs all
carry both ``store_id`` and ``auction_id`` as indexed COLUMNS, so all four are in the affected
store's own history:

===============  ============================================================================
``bid_placed``   the arm the store PLAYED: ``payload.offer`` (``discount``, ``unit_price``,
                 ``product_ref``) and ``payload.bid_ref``. Written for every bid, won or lost,
                 which is what makes a loss learnable rather than merely absent.
``shown``        the IMPRESSION: ``payload.slot`` and ``payload.bid_ref``. One per filled
                 shortlist slot. Nothing in this repository read it before this module.
``accepted``     the CONVERSION: the buyer chose this store's offer in this auction.
the event        ``order_ref`` and ``auction_id``, off the row being announced.
===============  ============================================================================

ONE STORE'S OWN RECORD, AND NOBODY ELSE'S
------------------------------------------
The history handed in is one store's rows, selected by the indexed ``store_id`` column, so
every field published here is the store's own decision or its own outcome. **No competitor
appears**, and none can: this module never sees another store's row to leak one. That is the
same property ``feedback/engine.py`` states in its first paragraph and the reason
``StoreAgentSink`` has no wildcard address — a broadcast of one store's trust movement to its
competitors is the leak R13 forbids, and an attribution record naming who else was shown would
be that leak wearing an analytics hat.

WHY IT IS NOT SCRUBBED, WHICH IS A DECISION AND NOT AN OMISSION
----------------------------------------------------------------
``bid_ref`` is ``f"{auction_id}:{store_id}"`` (``ranking.candidates.mint_bid_id``), ``slot`` is
one of four fixed words, and ``auction_id`` and ``order_ref`` are already published verbatim as
``LedgerEvent`` fields. So this record introduces no identifier the wire did not already carry.
``order_ref`` in particular is read RAW for the reason :func:`~.engine._wire_event` gives about
the same field: ``scrub`` redacts any run of nine or more digits, so an ordinary order id like
``"5678901234"`` would arrive as ``"[redacted]"`` — still truthy, so no fallback fires — and the
store would be told its score moved on an order it cannot identify.

**Prices and discounts are the store's own quoted terms**, echoed back to the store that quoted
them. They are not the buyer's anything.

TOTAL BY CONSTRUCTION
---------------------
:func:`impression_for` never raises. It runs on ``POST /events``'s request path, inside a delta
computation whose whole contract is that a notification failure can never fail an append, and a
ledger row from an older build with a payload shaped differently is a real condition rather than
a hypothetical. An attribution that cannot be computed is ``None`` — the delta still goes, with
everything it always carried.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "ATTRIBUTION_SCHEMA_VERSION",
    "BID_KIND",
    "CONVERSION_KIND",
    "IMPRESSION_KIND",
    "impression_for",
]

#: Bumped when the SHAPE below changes in a way a reader must notice. A recipient that does not
#: recognise the version is expected to ignore the record, exactly as with ``TRUST_REPORT_KEY``.
ATTRIBUTION_SCHEMA_VERSION = 1

#: The three ledger kinds this join reads. Spelled as constants rather than inline literals so
#: that the set of kinds this module depends on is greppable from the outside — the thing that
#: was NOT true of ``shown``, whose only reader before this module was the code that wrote it.
BID_KIND = "bid_placed"
IMPRESSION_KIND = "shown"
CONVERSION_KIND = "accepted"

#: The offer fields worth echoing back. Deliberately a fixed list and not "whatever the offer
#: had": ``payload`` is an open mapping, so copying it wholesale would publish whatever a future
#: producer decides to put in an offer, to a recipient, forever, with no review.
_OFFER_FIELDS = ("discount", "unit_price", "currency", "product_ref")


def _mapping(value: Any) -> Mapping[str, Any]:
    """``value`` as a mapping, or an empty one. A payload from an older build is data."""
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    """``value`` as a stripped string, or ``""``. Never raises on an exotic type."""
    try:
        return str(value).strip() if value is not None else ""
    except Exception:  # noqa: BLE001 - a value whose __str__ raises names nothing
        return ""


def _arm(offer: Any) -> dict[str, Any] | None:
    """The rung a bid was played at, projected onto :data:`_OFFER_FIELDS`.

    ``None`` when the bid carried no offer at all, which is different from an offer that
    carried no discount — the second is a real arm (list price) and the agent learns from it.
    """
    body = _mapping(offer)
    if not body:
        return None
    return {field: body.get(field) for field in _OFFER_FIELDS if field in body}


def impression_for(
    event: Mapping[str, Any], history: Iterable[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """The impression ``event`` is an outcome of, or ``None`` when the chain does not say.

    Args:
        event: the ledger row being announced — the same object
            :func:`~.deltas.delta_for_event` is handed.
        history: the affected store's prior rows. Walked once; not consumed if it is a list,
            which is what ``notify.store_history_reader`` returns.

    Returns:
        A mapping carrying ``schema_version``, ``auction_id``, ``bid_ref``, ``slot``,
        ``order_ref``, ``shown``, ``converted`` and ``arm`` — or ``None`` when the event names
        no auction, which is the honest answer rather than a record of empty fields.

        ``shown`` and ``converted`` are booleans about THIS auction and are false rather than
        null when the corresponding row is absent: the history is the store's complete bounded
        record, so "no ``accepted`` row for this auction" is evidence of a loss, not of a gap.
        The one thing that IS null-able is ``slot``, because a store can be bid-and-benched —
        never shown at all — and calling that slot ``""`` would make it sort with a real one.

    **The auction is the join key, not the bid_ref**, and that ordering matters. ``bid_ref`` is
    minted ``f"{auction_id}:{store_id}"``, so for a single store's history the two are
    equivalent — but only the auction id is a first-class indexed COLUMN on every row, while
    ``bid_ref`` lives inside ``payload`` and an older row may spell it differently or not at
    all. Joining on the column and reporting the payload's ``bid_ref`` when it is there keeps
    the join correct on rows this build did not write.
    """
    auction_id = _text(event.get("auction_id") if isinstance(event, Mapping) else None)
    if not auction_id:
        # An observation that names no auction has no impression behind it — a `claim_verified`
        # from the registry, say. There is nothing to attribute and nothing is invented.
        return None

    bid_ref = ""
    slot: str | None = None
    shown = False
    converted = False
    arm: dict[str, Any] | None = None

    for row in history:
        if not isinstance(row, Mapping):
            continue
        if _text(row.get("auction_id")) != auction_id:
            continue
        kind = _text(row.get("kind"))
        payload = _mapping(row.get("payload"))
        # Read off whichever row carries it; they agree, and taking the first non-empty keeps
        # the value available even when one of the three rows predates the field.
        bid_ref = bid_ref or _text(payload.get("bid_ref"))

        if kind == BID_KIND:
            arm = _arm(payload.get("offer")) or arm
        elif kind == IMPRESSION_KIND:
            shown = True
            slot = _text(payload.get("slot")) or slot
        elif kind == CONVERSION_KIND:
            converted = True
            # The ACCEPTED offer wins over the bid's, because a counter-proposal means the
            # terms that actually earned the outcome are not the terms first pitched.
            arm = _arm(payload.get("offer")) or arm

    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "auction_id": auction_id,
        "bid_ref": bid_ref or f"{auction_id}:{_text(event.get('store_id'))}",
        "slot": slot,
        "order_ref": _text(event.get("order_ref")) or None,
        "shown": shown,
        "converted": converted,
        "arm": arm,
    }
