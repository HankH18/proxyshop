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
``list_price`` and, when the caller supplied one, a ``max_discount_pct`` — the deepest discount
the exchange is told is authorized on that product. It is *told*: C3/S7 forbids the exchange from
ever reading a merchant's `Envelope` itself, so this number arrives with the roster and is only as
trustworthy as the roster is. See the last section here and `RosterEntry` in ``routes.py``.

A bid that DECLARES a discount is making a claim about authorization, and until this the only
wall checking that claim ran inside our own store-agent runtime, on the emitting side. A Tier-2
store does not run our runtime. Measured on the boundary before this: a bid declaring 20% and
charging 15.00 for a product the exchange lists at 100.00 came back ``ok=True, reasons=[]``, and
so did the same bid declaring 85% — the depth bounded the price and the bid chose the depth.
:func:`contracts.boundary.price_reasons` is that wall; this module runs it, and a bid that fails
it is replaced by the store's list-price fallback with ``fallback_reason`` naming why.

Silence is not an exemption
---------------------------

The wall used to run only on offers DECLARING a non-zero discount, and that handed the emitter the
switch again one spelling over. Measured through ``POST /auctions`` before this change, on a roster
row ``{product_ref: 'prod-1', list_price: 100.0, max_discount_pct: 20.0}`` answered by a bid with
**no** ``discount`` key at ``unit_price: 0.0``::

    HTTP 201  entries=[{store_id: 's1', fallback: false, unit_price: 0.0, fallback_reason: null}]

The same bid and the same row handed straight to the boundary
(:func:`contracts.boundary.price_reasons`) come back ``['price_under_declared_depth:offer.unit_price']``.
The refusal already existed; this module simply declined to ask for it. An undeclared price under
list IS a discount — one that entered the bid through no hook at all — so the question asked here is
no longer "did the offer declare one" but **"does the exchange hold a statement to judge it
against"**, and there are three ways it does:

1. **The offer declares a discount.** Always judged: it is asserting an authorization, and an
   assertion is checkable on its own terms.
2. **The roster row states a ``max_discount_pct``.** Judged whether or not a discount is declared.
   An implicit depth is not a different animal from a declared one — 15.00 against a 100.00 list
   under a 20% cap is an 85% discount however the paperwork is spelled — and a row that names the
   authorized depth is exactly the statement needed to say so.
3. **Neither of those, and the offer gives the product away.** Judged. A non-positive or unreadable
   price for a product the roster prices above zero is not an aggressive bid, it is a free item,
   and it is refused *whatever the request body claims the cap is* — see :func:`_priced_at_nothing`.

Everything else — an undeclared undercut on a row that states no authorized depth — is admitted,
and that is not a gap left open by preference. **R10 forces it.**
``.swarm-loop/acceptance/test_e3_exchange.py::test_every_store_is_represented_including_silent_and_tier0``
rosters ``{"store_id": "store-r1", "tier": 1, "product_ref": "product-1", "list_price": 120.0}``
with no ``max_discount_pct``, answers it with ``{"product_ref": "product-1", "unit_price": 80.0,
"total_price": 80.0}`` carrying no ``discount`` at all, and then asserts ``fallback is False`` and
``_unit_price(...) == 80.0``. That is a 33.3% *undeclared* undercut the frozen suite requires the
exchange to keep, and with no authorized depth on the row there is no line the exchange can draw
between an aggressive auction bid and an unauthorized discount.

What R10 does **not** require is that 0.00 be admitted. No case in any frozen acceptance suite
offers a zero or negative price — ``grep -rn 'unit_price": 0' .swarm-loop/acceptance/`` is empty —
so case 3 closes the free item without touching a single frozen assertion. An earlier pass read the
80.00 case as covering 0.00 as well and pinned all three prices as admitted; the frozen text does
not say that, and the pin was wider than the constraint.

Where the cap comes from, and where it does not
-----------------------------------------------

``max_discount_pct`` reaches this function on the roster, and the roster reaches the exchange on an
**unauthenticated ``POST /auctions`` request body** (``RosterEntry`` in ``routes.py``; the exchange
has no authentication of any kind — ``git grep -nE "Depends|api_key|Authorization" apps/exchange/src``
is empty). Whatever the caller says the cap is, is the cap — and the same is true of ``list_price``,
so the trust problem is the whole roster, not this one field.

The obvious repair — read the merchant's approved envelope — is **forbidden here, deliberately**.
``protocol.schema.json``'s `Envelope` says it "NEVER crosses into the exchange (C3/S7): it is sealed
state", and ``.importlinter``'s ``c3-exchange-cannot-read-envelopes`` contract enforces exactly that
by forbidding ``exchange`` from importing ``merchant_svc.envelope``. So an authoritative cap needs a
*derived-authorization port* — the shape R12's eligibility already uses, where the exchange consults
a versioned ``SellerEligibility`` interface rather than the trust ledger — and that port does not
exist in this codebase yet. Case 3 above is the part that does not wait on it: it holds at
``max_discount_pct: 100`` and on a row with no cap at all.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from contracts.boundary import (
    MAX_DISCOUNT_ROSTER_KEY,
    REASON_PRICE_UNRECONCILABLE,
    price_reasons,
)

__all__ = [
    "BidEntry",
    "FALLBACK_REASONS",
    "ILLEGIBLE_OFFER_REASON",
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

#: The boundary's verdict on an offer it cannot read as a record AT ALL — `"offer": []`,
#: `"offer": "free"`, no `offer` key. :func:`contracts.boundary.price_reasons` answers those with
#: an empty list, because a boundary that cannot find an offer has nothing to say ABOUT one; and
#: an empty list means "no refusal", so the store was admitted — at :attr:`BidEntry.unit_price`'s
#: `float(offer.get("unit_price", 0.0))` default of **0.00**. Measured before this existed, on a
#: rostered row listing at 100.00::
#:
#:     offer=[]  ->  fallback=False  unit_price=0.0  price_reasons=[]
#:
#: which is the same free item as a 0.00 bid, reached by sending no price at all instead of a
#: cheap one. The exchange does not get to read the boundary's silence as an admission, so an
#: illegible offer is named here and refused. Spelled in the boundary's own vocabulary because
#: that is what `BidEntry.price_reasons` carries.
ILLEGIBLE_OFFER_REASON = f"{REASON_PRICE_UNRECONCILABLE}:offer:illegible"

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
    discount that takes nothing off the price asserts no authorization and needs none. It is
    **not** on its own a reason to skip the wall; see :func:`_is_judged`.
    """
    discount = offer.get("discount") if isinstance(offer, Mapping) else None
    if discount is None:
        return False
    value = discount.get("value") if isinstance(discount, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    return float(value) != 0.0


def _number(value: Any) -> float | None:
    """``value`` as a real number, or ``None`` — the same read the boundary does.

    Deliberately no coercion and no ``bool``: ``"0"`` is a string a seller wrote, not a price, and
    ``True`` is not one dollar. NaN and ±inf are excluded because every comparison against them is
    silently false, which is the fail-OPEN direction on a wall. This is
    :func:`contracts.boundary._finite_number`'s predicate, restated rather than imported because
    that one is private; the two must agree, and the tests below pin the cases where it matters.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _states_an_authorized_depth(rostered: Mapping[str, Any]) -> bool:
    """Does this roster row make any statement at all about how deep a discount is authorized?

    Presence, not readability: a row carrying ``max_discount_pct: "20"`` or ``max_discount_pct:
    500`` has made a statement the exchange cannot read, and the boundary refuses that
    (``unreadable_authorized_depth``) rather than treating it as silence. Reading past an
    unreadable cap here would let a roster disarm the wall by writing garbage into the field that
    turns it on — the same "make the paperwork illegible" move :func:`_declares_a_discount` refuses
    on the bid's side.
    """
    try:
        return rostered.get(MAX_DISCOUNT_ROSTER_KEY) is not None
    except Exception:  # noqa: BLE001 - a hostile roster row is a row that states nothing
        return False


def _priced_at_nothing(offer: Any, rostered: Mapping[str, Any]) -> bool:
    """Is this offer giving away a product the roster prices above zero?

    The floor under everything else in :func:`_is_judged`, and the only one of its three cases that
    survives a caller-supplied cap of 100. A store that answers 0.00 — or -5.00, or ``"free"``, or
    ``null`` — for a product the exchange was asked to auction at 100.00 is not undercutting
    anybody; there is no auction semantics under which the winning consideration is nothing. R10
    protects the store that bids aggressively, and no frozen case anywhere asks the exchange to
    rank a zero.

    ``total_price`` counts too: a 0.00 total on an 80.00 unit is the same free item wearing the
    other field. And an *unreadable* price is a priced-at-nothing as well, because
    :attr:`BidEntry.unit_price` would go on to call ``float()`` on it — so this is also the reason a
    store answering ``unit_price: "cheap"`` degrades to its list price instead of raising
    ``ValueError`` out of the middle of an auction every other store is bidding in.

    **That last sentence holds only where the roster prices the product above zero, and this
    function is the reason why.** It returns ``False`` when ``listed <= 0``, so on a row carrying
    ``list_price: 0.0`` the offer is never judged here and ``float("cheap")`` does raise, out of
    the middle of the auction, as HTTP 500. Measured through ``POST /auctions``. That is not a
    regression — ``git show main:`` of this module raises the identical ``ValueError`` — but the
    protection this paragraph describes is conditional, and the condition is caller-supplied.
    ``list_price: 0.0`` is an accepted roster value (``Field(ge=0.0)``), so a caller can reach
    this branch with one keystroke; it is scoped to the derived-authorization follow-up ticket
    along with the rest of the untrusted-roster surface.

    ``False`` when the roster itself prices the product at zero or cannot price it: there is then
    no free item to detect *by this rule* — see the paragraph above for what that costs — and a
    row that names no ``list_price`` is handled by the boundary's own ``list_price_unavailable``
    refusal on the paths that reach it.
    """
    listed = _number(rostered.get("list_price"))
    if listed is None or listed <= 0.0:
        return False
    if not isinstance(offer, Mapping):
        return True
    for site in ("unit_price", "total_price"):
        priced = _number(offer.get(site))
        if priced is None or priced <= 0.0:
            return True
    return False


def _is_judged(offer: Any, rostered: Mapping[str, Any]) -> bool:
    """Does the exchange hold a statement this offer's price can be reconciled against?

    Three ways it does, and the module docstring's "Silence is not an exemption" section carries
    the argument for each. In one line: a declared discount is a claim, a rostered
    ``max_discount_pct`` is an authorization, and a free item needs neither.
    """
    return (
        _declares_a_discount(offer)
        or _states_an_authorized_depth(rostered)
        or _priced_at_nothing(offer, rostered)
    )


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

    Empty list when :func:`_is_judged` says the exchange holds nothing to judge this offer
    against — an undeclared undercut on a row that states no authorized depth, which R10 requires
    the exchange to keep. That is the ONLY abstention left; it used to be "any offer that declares
    no discount", which admitted a 0.00 bid on a 100.00 product at the production door.
    """
    offer = bid.get("offer")
    if not _is_judged(offer, rostered):
        return []
    refused = price_reasons(bid, list_prices={rostered.get("product_ref"): rostered})
    if refused or isinstance(offer, Mapping):
        return refused
    # The boundary read no offer here, so it said nothing — and nothing means "no refusal".
    # See :data:`ILLEGIBLE_OFFER_REASON` for why silence must not be an admission.
    return [ILLEGIBLE_OFFER_REASON]


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
            ``max_discount_pct`` — the deepest discount the caller states is authorized on
            that product. Roster order is the output order. A row that names no
            ``max_discount_pct`` authorizes no discount at all: a bid declaring one is
            refused rather than measured against a depth it chose for itself, and falls back
            to the row's list price. A row that DOES name one is the exchange's licence to
            judge an undeclared price too — silence stops being an exemption — and a bid
            giving the product away is refused on either kind of row.
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
            # T-177. The store answered in time with a well-formed bid; the remaining question
            # is whether the price it CHARGES is one this roster authorizes — declared or not. A
            # bid that fails that is not a bid the exchange may rank: it is a store helping itself
            # to an authorization, whether it wrote the authorization down or stayed quiet about
            # it. So it degrades to its list price like any other unusable answer, with its own
            # reason.
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
