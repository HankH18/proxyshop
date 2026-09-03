"""Fan-out collection: turn a roster plus whatever came back into one entry per store (R10).

R10 has two halves and this module is the second one. The first half — *asking* — is I/O and
lives above, in :mod:`apps.exchange.src.orchestration`. What is left here is pure and
deterministic: given the roster we asked, the responses that arrived, and the deadline they
had to beat, produce **one entry per rostered store**, never more and never fewer.

A fallback is produced by one of these, and the entry records **which**:

======================================  ===========================================
a Tier-0 store                          has no bidding agent to answer at all
a Tier-1 store that stayed silent       the hard timeout expired on it
a response stamped after the deadline   a late bid is not a bid (R10)
a reply carrying no ``bid``             the store answered with nothing to rank
a reply with no arrival stamp           the exchange's own stamp never got applied
a reply whose stamp will not parse      undatable, therefore uncertifiable
======================================  ===========================================

The last three are *not* lateness and must not be reported as it. They used to be: all
four rejections shared the single label ``response_after_deadline``, so a store that
answered well inside the window with a malformed payload was recorded — and would be
reported back to its operator — as slow. See :data:`FALLBACK_REASONS`.

The late-bid rule is the one worth being blunt about: the deadline is enforced on the
response's ``received_at``, so a bid that arrives at any price after the deadline is
discarded and its store falls back to its **list price**. An implementation that merely
sorted by arrival, or that trusted the last response to win, would let a slow store bid
1.00 after seeing everyone else and take every auction.

That makes ``received_at`` a **privileged field**, and this module is pure: it cannot tell
where the value came from. The stamp must therefore be applied by the exchange, upstream, in
:func:`apps.exchange.src.auction.fanout._stamped`, which overwrites whatever the bidder
sent. Nothing here re-checks that, so a caller assembling ``responses`` by hand is asserting
the stamps are its own — hand this function a store's raw reply and the deadline becomes
advisory.

``collect_bids`` is a pure function of three positional arguments and takes **no eligibility
argument** — D54 is explicit that the R12 gate lives in the orchestration layer above,
which hands this function an already-eligible roster. Putting a gate here would make the
function deny whenever no port was injected, which is the wrong default for a pure helper
and the wrong layer for a system guarantee.

The discount wall (T-177)
-------------------------

There is one thing this function *does* judge about a bid's content, and it is here because
this is the only place in the exchange that holds both halves of the comparison: the store's
answer, and **the roster row it was asked from**. A rostered row carries the product's
``list_price`` and — when the merchant's approved envelope is known — the ``max_discount_pct``
that envelope permits on it.

A bid that DECLARES a discount is making a claim about authorization, and until this the only
wall checking that claim ran inside our own store-agent runtime, on the emitting side. A Tier-2
store does not run our runtime. Measured on the boundary before this: a bid declaring 20% and
charging 15.00 for a product the exchange lists at 100.00 came back ``ok=True, reasons=[]``, and
so did the same bid declaring 85% — the depth bounded the price and the bid chose the depth.
:func:`contracts.boundary.price_reasons` is that wall; this module runs it, and a bid that fails
it is replaced by the store's list-price fallback with ``fallback_reason`` naming why.

**Only bids that declare a discount are judged**, and that boundary is deliberate. R10 lets a
store bid whatever it likes: undercutting its own list price with no discount declared is what an
auction IS, and ``.swarm-loop/acceptance/test_e3_exchange.py`` pins exactly that (an 80.00 bid
against a 120.00 rostered list price is kept, not refused). What the exchange does not admit is a
store awarding itself an authorization nobody granted. So the arithmetic runs on the offers that
claim one, and a store that declares no discount is measured only by the ranker, as before.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from contracts.boundary import price_reasons

__all__ = [
    "BidEntry",
    "FALLBACK_REASONS",
    "MALFORMED_RESPONSE_REASONS",
    "UNRECONCILABLE_PRICE_REASON",
    "collect_bids",
]

#: Why an entry ended up at list price. Recorded on the entry so a downstream reader (the
#: ranker, a loss report, an operator) never has to guess between "nobody home" and "too
#: late" — they are different store behaviours with different consequences.
#:
#: The three *malformed* reasons used to be collapsed into ``response_after_deadline``,
#: which told an operator a lie: a store that answered inside the window with a broken
#: payload was reported as slow. Those are different faults with different owners — one is
#: the store's network, the others are the store's serializer or the transport between us —
#: and only one of them is evidence about latency. A loss report built on the collapsed
#: label would blame the wrong thing, and a store arguing it answered in time would be
#: right.
#: The store answered, in time, with a well-formed bid — and the discount it declared does not
#: reconcile against the roster row it was asked from (T-177). A seventh reason rather than a
#: shrug: this is a *policy* refusal, not a transport fault and not latency, and an operator
#: reading a loss report must not see "you were slow" when what happened is "you declared a
#: discount the exchange has no authorization for". The entry keeps the store's real
#: :attr:`BidEntry.price_reasons` alongside it so the rejection can be quoted back verbatim.
UNRECONCILABLE_PRICE_REASON = "bid_price_unreconcilable"

FALLBACK_REASONS: tuple[str, ...] = (
    "tier_0_no_agent",
    "no_response",
    "response_after_deadline",
    "response_carried_no_bid",
    "response_not_stamped",
    "arrival_stamp_unparseable",
    UNRECONCILABLE_PRICE_REASON,
)

#: The subset of :data:`FALLBACK_REASONS` that means "a reply arrived, and we could not use
#: it" — as opposed to "it arrived too late" or "nothing arrived at all".
MALFORMED_RESPONSE_REASONS: tuple[str, ...] = (
    "response_carried_no_bid",
    "response_not_stamped",
    "arrival_stamp_unparseable",
)


@dataclass
class BidEntry:
    """One rostered store's representation in an auction.

    ``bid`` is always present and always carries an ``offer``: a fallback is a real,
    rankable, list-price offer, not a hole. ``fallback`` says which kind it is.
    """

    store_id: str
    tier: int
    fallback: bool
    bid: dict[str, Any]
    received_at: float | None = None
    fallback_reason: str | None = None
    claims: list[Any] = field(default_factory=list)
    #: The boundary's own reason strings when this entry fell back because its declared discount
    #: did not reconcile (``fallback_reason == UNRECONCILABLE_PRICE_REASON``). Empty otherwise.
    #: Kept because ``fallback_reason`` is a fixed vocabulary a loss report aggregates on, while
    #: *which* relation the bid broke is what the store's operator actually needs told.
    price_reasons: list[str] = field(default_factory=list)

    @property
    def offer(self) -> dict[str, Any]:
        offer = self.bid.get("offer")
        return offer if isinstance(offer, dict) else {}

    @property
    def unit_price(self) -> float:
        return float(self.offer.get("unit_price", 0.0))


def _list_price_bid(entry: Mapping[str, Any], auction_id: str | None) -> dict[str, Any]:
    """The catalog-derived fallback offer for a store that did not (or cannot) bid."""
    list_price = float(entry.get("list_price", 0.0))
    return {
        "auction_id": auction_id,
        "store_id": str(entry["store_id"]),
        "offer": {
            "product_ref": entry.get("product_ref"),
            "unit_price": list_price,
            "total_price": list_price,
            "currency": entry.get("currency", "USD"),
        },
        # R10/R18/R19: a fallback carries no asserted claims. It is catalog data, so it can
        # never be the evidence that satisfies a hard constraint.
        "claims": [],
        "fallback": True,
    }


def _declares_a_discount(offer: Any) -> bool:
    """Does this offer CLAIM a discount — i.e. assert an authorization someone had to grant?

    ``True`` for a stated non-zero depth, and also for one this function cannot read: a
    ``discount`` block whose ``value`` is ``"85"`` or ``null`` is a claim made illegibly, and
    reading past it would let a bid disable the wall by making its own paperwork unreadable. The
    boundary names that case ``price_unreconcilable:offer.discount:depth_not_a_number`` rather
    than guessing, which is why it must be handed the offer rather than skipped.

    ``False`` only for an offer with no ``discount`` at all, or one declaring exactly zero — a
    discount that takes nothing off the price asserts no authorization and needs none.
    """
    discount = offer.get("discount") if isinstance(offer, Mapping) else None
    if discount is None:
        return False
    value = discount.get("value") if isinstance(discount, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    return float(value) != 0.0


def _price_refusal(bid: Mapping[str, Any], rostered: Mapping[str, Any]) -> list[str]:
    """The boundary's verdict on this bid's declared discount, against its own roster row.

    The one-row roster is keyed by **the ROSTER's** ``product_ref``, and the boundary looks the
    row up by the **OFFER's**. Keying it by the offer's instead would hand every bid a row
    whatever product it named — the bid would be choosing which catalog entry it is priced
    against, which is the shape of the defect this whole wall exists to close. So a store
    answering about a product it was not asked about finds no row, and an unpriceable product is
    a refusal rather than an abstention
    (:data:`contracts.boundary.ROSTER_LIST_PRICE_UNAVAILABLE`) — the direction an attacker's
    "you've never heard of that ref" would otherwise walk through.

    Empty list when the offer declares no discount — see the module docstring on why the exchange
    does not run the whole wall here, only the half about claimed authorization.
    """
    if not _declares_a_discount(bid.get("offer")):
        return []
    return price_reasons(bid, list_prices={rostered.get("product_ref"): rostered})


def _unusable_because(response: Mapping[str, Any], deadline: float) -> str | None:
    """Why this response cannot be counted, or ``None`` when it can.

    Every rejection here still fails **closed** — an answer the exchange cannot date is an
    answer it cannot certify arrived in time, so the store falls back to its list price —
    but each one is *named*. An arrival stamp that will not parse is not an exception
    either: this function is fed store-shaped data, and ``float("whenever")`` raises
    ``ValueError``, which used to escape ``collect_bids``, ``solicit_bids`` and the route,
    so one malformed reply took down an auction every other store was bidding in. (``nan``
    already fails the comparison; this makes the string and ``None``-ish cases agree.)

    The four conditions are kept apart because they are four different faults:

    ``response_carried_no_bid``      a reply with no ``bid`` mapping — the store answered,
                                    but with nothing to rank
    ``response_not_stamped``         no ``received_at`` at all, so the exchange's own stamp
                                    never got applied; a fan-out bug, not a store's latency
    ``arrival_stamp_unparseable``    a stamp that is not a number
    ``response_after_deadline``      the only one of the four that is actually about time
    """
    bid = response.get("bid")
    if not isinstance(bid, Mapping):
        return "response_carried_no_bid"
    received_at = response.get("received_at")
    if received_at is None:
        return "response_not_stamped"
    try:
        arrived = float(received_at)
    except (TypeError, ValueError):
        return "arrival_stamp_unparseable"
    if not arrived <= float(deadline):
        return "response_after_deadline"
    return None


def collect_bids(
    roster: Sequence[Mapping[str, Any]],
    responses: Iterable[Mapping[str, Any]],
    now: float,
) -> list[BidEntry]:
    """Represent every rostered store, exactly once, in roster order.

    Args:
        roster: the stores selected for this auction —
            ``{store_id, tier, product_ref, list_price}``, optionally with
            ``max_discount_pct`` — the deepest discount the merchant's approved envelope
            permits on that product. Roster order is the output order. A row that names no
            ``max_discount_pct`` authorizes no discount at all: a bid declaring one is
            refused rather than measured against a depth it chose for itself, and falls back
            to the row's list price. An undiscounted bid never consults the cap.
        responses: whatever came back — ``{store_id, received_at, bid}``. Responses for a
            store that is not on the roster are ignored; a store that answered twice keeps
            its **first** on-time answer, so a second, cheaper resubmission cannot displace
            the bid the store actually committed to inside the window.
        now: the fan-out **deadline**, a float epoch. A response with
            ``received_at > now`` is rejected. Named ``now`` because that is the
            parameter name D54 pins; it is the instant the auction closed.

    Returns:
        One :class:`BidEntry` per rostered store, in roster order.
    """
    deadline = float(now)

    # Materialise once: `responses` is an Iterable and is walked twice below (on-time bids,
    # then the late ones we only need in order to label a fallback honestly). A generator
    # would silently look empty the second time.
    arrived = list(responses)

    on_time: dict[str, Mapping[str, Any]] = {}
    # store_id -> why its first unusable reply was unusable. First, not last: a store that
    # sends junk and then a second reply should be reported by what it actually did first,
    # exactly as `on_time` keeps a store's first committed bid.
    rejected: dict[str, str] = {}
    for response in arrived:
        store_id = response.get("store_id")
        if store_id is None:
            continue
        unusable = _unusable_because(response, deadline)
        if unusable is not None:
            rejected.setdefault(str(store_id), unusable)
            continue
        on_time.setdefault(str(store_id), response)

    auction_ids = {
        str(r["bid"]["auction_id"])
        for r in on_time.values()
        if isinstance(r.get("bid"), Mapping) and r["bid"].get("auction_id") is not None
    }
    auction_id = next(iter(auction_ids)) if len(auction_ids) == 1 else None

    entries: list[BidEntry] = []
    for rostered in roster:
        store_id = str(rostered["store_id"])
        tier = int(rostered.get("tier", 1))
        answer = on_time.get(store_id)

        if tier <= 0:
            # Tier-0 is a catalog-only store: there is no agent to answer, so it is
            # represented at list price whatever arrived under its name.
            reason: str | None = "tier_0_no_agent"
        elif answer is None:
            reason = rejected.get(store_id, "no_response")
        else:
            reason = None

        refused: list[str] = []
        if reason is None and answer is not None:
            # T-177. The store answered in time with a well-formed bid; the remaining question is
            # whether the discount it DECLARES is one this roster authorizes at the price it
            # charges. A bid that fails that is not a bid the exchange may rank — it is a store
            # helping itself to an authorization — so it degrades to its list price like any
            # other unusable answer, with its own reason.
            refused = _price_refusal(dict(answer["bid"]), rostered)
            if refused:
                reason = UNRECONCILABLE_PRICE_REASON

        if reason is None and answer is not None:
            bid = dict(answer["bid"])
            entries.append(
                BidEntry(
                    store_id=store_id,
                    tier=tier,
                    fallback=False,
                    bid=bid,
                    received_at=float(answer["received_at"]),
                    claims=list(bid.get("claims") or []),
                )
            )
        else:
            entries.append(
                BidEntry(
                    store_id=store_id,
                    tier=tier,
                    fallback=True,
                    bid=_list_price_bid(rostered, auction_id),
                    received_at=None,
                    fallback_reason=reason,
                    price_reasons=refused,
                )
            )
    return entries
