"""Was this order network-routed? R14's gate, and the only reason this package is careful.

SPEC R14: "Post-purchase feedback: **only network-routed buyers**, one structured prompt (*did
it match the pitch?*), weighted by buyer track record, cross-checked against return behavior."

The clause is a NEGATIVE guarantee, so the interesting case is the one where nothing is offered
and nothing is recorded. ``apps/trust/src/feedback/engine.py`` says why in one line: "without
that gate a store can astroturf its own ``feedback_match`` dimension for the price of a few fake
reviews, and the dimension stops meaning anything." Trust holds the authoritative gate — it
takes a ``routed_orders`` table and refuses anything outside it. This module is the buyer-side
half: it decides whether to *ask*, and refuses to *emit* for an order it cannot attribute to an
auction, so an un-routed submission never reaches trust's gate at all.

Three readings that are wrong, and are the shape of this file
------------------------------------------------------------
* **``bool(order["routed"])``.** The string ``"false"`` is truthy in Python, and pydantic's lax
  mode coerces ``"yes"`` to ``True`` (measured on this tree at 2.13, where it turned a body
  carrying no boolean into a live auction on T-071's confirm route). Both readings hand a prompt
  to an un-routed order. :func:`buyer_svc.feedback._reading.flag` answers ``True``/``False`` only
  for an unambiguous boolean and ``None`` — "this record does not say" — otherwise.
* **"the flag said true, so it was routed".** An order claiming to be routed while naming no
  ``auction_id`` cannot be attributed to an auction. Its feedback event would carry
  ``auction_id=None``, which is a ``feedback`` row nobody can trace back to the offer it is
  about — and T-072 already had to refuse the sibling case (``buyer_svc.intent.confirm`` returns
  an ``AuctionCreated`` whose ``auction_id`` is the **empty string** when the exchange created
  the auction but answered without an id, so a downstream record carrying ``auction_id=""`` is a
  real value on this tree, not a hypothetical). A negative guarantee gets the conservative
  reading: no auction named, not routed.
* **"ask ``buyer_svc.accept``".** T-072 keeps a process-local ledger of the auctions this
  process handed off, and its docstring offers it as the answer to "was this order routed?".
  It is deliberately NOT consulted here. That ledger records *accepts*, which happen in the
  buyer's web process; an order record arrives later, from the exchange's order webhook, in a
  different process and usually on a different day. A gate that consulted it would pass in a
  pytest session (where both happen in one process) and refuse every real order in production —
  the worst possible split between what is tested and what ships.

Routed is not the same question as eligible
-------------------------------------------
:attr:`Routing.routed` is R14's gate and governs what may be *written*.
:attr:`Routing.eligible` additionally requires that there is something to give feedback about,
and governs what is *shown*: an order cancelled before it ever shipped gets no "did it match the
pitch?" prompt, because it never arrived to match anything.

The two are deliberately separate rather than one flag. A cancellation that lands between the
prompt being shown and the buyer answering must not discard the answer — the buyer really did
receive something and really did tell us about it — so :func:`~buyer_svc.feedback.submission.
submit_feedback` gates on ``routed`` and :func:`~buyer_svc.feedback.prompt.feedback_prompt` gates
on ``eligible``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ._reading import first, flag, read
from .errors import UnusableOrder

_log = logging.getLogger(__name__)

__all__ = [
    "ORDER_REF_FIELDS",
    "ROUTED_FIELDS",
    "STATUS_FIELDS",
    "UNDELIVERED_STATUSES",
    "Routing",
    "routing",
]

#: Where an order names itself. ``order_ref`` is the spelling ``LedgerEvent`` uses and the one
#: D24 pins as a join key; the rest are tolerated because this is a seam, not because they are
#: equally correct.
ORDER_REF_FIELDS: tuple[str, ...] = ("order_ref", "order_id", "orderRef", "orderId")

#: Where an order record says whether the network routed it.
ROUTED_FIELDS: tuple[str, ...] = ("routed", "network_routed", "routed_by_network")

#: Where an order record carries its lifecycle state.
STATUS_FIELDS: tuple[str, ...] = ("status", "order_status", "state", "financial_status")

#: Statuses meaning "this order never became a delivery", so there is nothing to compare against
#: the pitch. ``refunded`` and ``returned`` are deliberately ABSENT: an order that arrived and
#: went back is exactly the case R14 cares most about, and trust cross-checks feedback against
#: return behaviour rather than discarding it (``trust.feedback.engine.RETURN_CONTRADICTION_FACTOR``).
UNDELIVERED_STATUSES: frozenset[str] = frozenset({"cancelled", "canceled", "void", "voided"})


@dataclass(frozen=True, slots=True)
class Routing:
    """What this package could establish about one order, and why it refused if it did."""

    #: The order's own reference. ``""`` when the record names none.
    order_ref: str
    #: The store the order was placed with. ``""`` when unknown.
    store_id: str
    #: The auction the order came out of. ``""`` when the record names none.
    auction_id: str
    #: The buyer the network routed, as a pseudonym. ``""`` when the record names none — see
    #: :class:`~buyer_svc.feedback.errors.FeedbackNotYours` for what that costs.
    buyer_pseudonym: str
    #: R14's gate. Governs what may be written to the ledger.
    routed: bool
    #: ``routed`` **and** there is something to give feedback about. Governs what is shown.
    eligible: bool
    #: Why not, in words a screen can show. ``""`` exactly when ``eligible`` is true.
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_ref": self.order_ref,
            "store_id": self.store_id,
            "auction_id": self.auction_id,
            "routed": self.routed,
            "eligible": self.eligible,
            "reason": self.reason,
        }


def routing(order: Any) -> Routing:
    """Read one order record and decide whether R14 lets it be asked, and answered.

    Args:
        order: the order record — the exchange's dict, a model, or a plain mapping. It is read
            through :mod:`buyer_svc.feedback._reading`, so a hostile ``__getitem__`` cannot raise
            out of here.

    Returns:
        :class:`Routing`. Never ``None``, and its ``reason`` is populated whenever ``eligible``
        is false, so a caller never has to re-derive why.

    Raises:
        UnusableOrder: ``order`` is not an order record at all (``None``, a string, a number).
            An order record that merely lacks fields is a :class:`Routing` with ``routed=False``
            and a reason; only "this is not a record" is an exception, because that is a broken
            call rather than a decision about an order.
    """
    if order is None or isinstance(order, (str, bytes, int, float, bool)):
        raise UnusableOrder(
            f"feedback takes the order record, not {type(order).__name__} ({order!r}). Pass the "
            f"order as the exchange sent it — it is what names the order, the store and the "
            f"auction the offer came from."
        )

    order_ref = first(order, ORDER_REF_FIELDS)
    store_id = first(order, ("store_id", "storeId"))
    auction_id = first(order, ("auction_id", "auctionId"))
    pseudonym = first(order, ("buyer_pseudonym", "pseudonym"))
    status = first(order, STATUS_FIELDS).lower()

    said: bool | None = None
    for field_name in ROUTED_FIELDS:
        raw = read(order, field_name)
        if raw is None:
            continue
        said = flag(raw)
        if said is None and raw != "":
            # The record HAS a routing flag and it is not a boolean in any spelling. Falling
            # back to the auction check is the safe reading, but it is also a silent one, and
            # a field that stopped being a boolean upstream would otherwise never be noticed.
            _log.warning(
                "order %s carries %s=%r, which is not a boolean in any spelling; falling back "
                "to whether the order names an auction",
                first(order, ORDER_REF_FIELDS) or "<unnamed>",
                field_name,
                type(raw).__name__,
            )
        break

    if said is False:
        return Routing(
            order_ref=order_ref,
            store_id=store_id,
            auction_id=auction_id,
            buyer_pseudonym=pseudonym,
            routed=False,
            eligible=False,
            reason=(
                "the network did not route this order, so R14 offers no feedback prompt for it. "
                "Feedback is taken only from buyers the network actually routed — otherwise a "
                "store can move its own trust score with reviews of orders it was never given."
            ),
        )

    if not auction_id:
        return Routing(
            order_ref=order_ref,
            store_id=store_id,
            auction_id="",
            buyer_pseudonym=pseudonym,
            routed=False,
            eligible=False,
            reason=(
                "this order names no auction, so it cannot be attributed to an offer the network "
                "made. Its feedback would be a trust observation about nothing in particular, so "
                "it is treated as un-routed."
                + (
                    " The record does claim routed=true; that disagreement is a data bug "
                    "upstream, not a reason to record the feedback."
                    if said is True
                    else ""
                )
            ),
        )

    if status in UNDELIVERED_STATUSES:
        return Routing(
            order_ref=order_ref,
            store_id=store_id,
            auction_id=auction_id,
            buyer_pseudonym=pseudonym,
            routed=True,
            eligible=False,
            reason=(
                f"this order is {status}, so nothing arrived to compare against the pitch. It "
                f"stays network-routed — feedback already given about it is still recorded — but "
                f"no prompt is offered."
            ),
        )

    return Routing(
        order_ref=order_ref,
        store_id=store_id,
        auction_id=auction_id,
        buyer_pseudonym=pseudonym,
        routed=True,
        eligible=True,
        reason="",
    )
