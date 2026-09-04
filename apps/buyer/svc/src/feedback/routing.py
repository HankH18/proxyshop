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
* **"nothing said it was not routed, so it was".** This is the one that bit. An earlier version
  of this file refused only on an explicit ``routed: false`` and otherwise fell back to "does the
  record name an auction?" — so an order record with **no ``routed`` field at all**, or one whose
  flag was unreadable, came back ROUTED, and ``auction_id`` is entirely caller-supplied. That is a
  fail-OPEN default on the single field R14's clause is about. The gate now requires an
  affirmative, unambiguous ``true``: absence is not evidence of routing, and the published order
  shape (``{order_ref, store_id, auction_id, routed}``) carries the flag, so requiring it costs a
  well-formed record nothing.
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
        # The value is NOT interpolated. A caller who passes a string passes whatever string
        # they had, and this message travels into a log line and an HTTP response body; a buyer
        # who typed their name into the wrong box must not have it echoed back through both.
        raise UnusableOrder(
            f"feedback takes the order record, not {type(order).__name__}. Pass the order as the "
            f"exchange sent it — it is what names the order, the store and the auction the offer "
            f"came from."
        )

    order_ref = first(order, ORDER_REF_FIELDS)
    store_id = first(order, ("store_id", "storeId"))
    auction_id = first(order, ("auction_id", "auctionId"))
    pseudonym = first(order, ("buyer_pseudonym", "pseudonym"))
    status = first(order, STATUS_FIELDS).lower()

    # EVERY routing field is read, not just the first one present, and an explicit `false`
    # anywhere wins over a `true` anywhere else. Stopping at the first PRESENT field was a
    # measured hole: `{"routed": "nope", "network_routed": False}` stopped on the unreadable
    # `routed`, never consulted `network_routed`, and came back routed. A record that
    # contradicts itself is not a record this gate should resolve in the permissive direction.
    said: bool | None = None
    for field_name in ROUTED_FIELDS:
        raw = read(order, field_name)
        if raw is None:
            continue
        reading = flag(raw)
        if reading is False:
            said = False
            break
        if reading is True:
            said = True
            continue
        if not isinstance(raw, str) or raw.strip():
            # The record HAS a routing flag and it is not a boolean in any spelling. Under the
            # fail-closed reading below this is already refused; the line exists because a field
            # that stopped being a boolean upstream would otherwise be invisible, and the refusal
            # would look like a correctly un-routed order. The TYPE is logged, never the value.
            _log.warning(
                "order %s carries %s of type %s, which is not a boolean in any spelling; R14's "
                "gate refuses it rather than guessing",
                order_ref or "<unnamed>",
                field_name,
                type(raw).__name__,
            )

    if said is not True:
        return Routing(
            order_ref=order_ref,
            store_id=store_id,
            auction_id=auction_id,
            buyer_pseudonym=pseudonym,
            routed=False,
            eligible=False,
            reason=(
                "this order is not marked as network-routed, so R14 offers no feedback prompt "
                "for it. Feedback is taken only from buyers the network actually routed — "
                "otherwise a store can move its own trust score with reviews of orders it was "
                "never given — and a record that does not say it was routed is not evidence "
                "that it was."
                if said is None
                else "the network did not route this order, so R14 offers no feedback prompt "
                "for it. Feedback is taken only from buyers the network actually routed — "
                "otherwise a store can move its own trust score with reviews of orders it was "
                "never given."
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
                "this order is marked network-routed but names no auction, so it cannot be "
                "attributed to an offer the network made. Its feedback would be a trust "
                "observation about nothing in particular. That disagreement is a data bug "
                "upstream, not a reason to record the feedback."
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
