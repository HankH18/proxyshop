"""Fan-out collection: turn a roster plus whatever came back into one entry per store (R10).

R10 has two halves and this module is the second one. The first half — *asking* — is I/O and
lives above, in :mod:`apps.exchange.src.orchestration`. What is left here is pure and
deterministic: given the roster we asked, the responses that arrived, and the deadline they
had to beat, produce **one entry per rostered store**, never more and never fewer.

Three rules produce a fallback, and only these three:

======================================  ===========================================
a Tier-0 store                          has no bidding agent to answer at all
a Tier-1 store that stayed silent       the hard timeout expired on it
a response stamped after the deadline   a late bid is not a bid (R10)
======================================  ===========================================

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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["BidEntry", "FALLBACK_REASONS", "collect_bids"]

#: Why an entry ended up at list price. Recorded on the entry so a downstream reader (the
#: ranker, a loss report, an operator) never has to guess between "nobody home" and "too
#: late" — they are different store behaviours with different consequences.
FALLBACK_REASONS: tuple[str, str, str] = (
    "tier_0_no_agent",
    "no_response",
    "response_after_deadline",
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


def _usable_response(response: Mapping[str, Any], deadline: float) -> bool:
    """A response counts only if it arrived at or before the deadline and carries a bid.

    An arrival stamp that will not parse as a number is treated as *not on time*, never as
    an exception. This function is fed store-shaped data, and ``float("whenever")`` raises
    ``ValueError`` — which used to escape ``collect_bids``, ``solicit_bids`` and the route,
    so one malformed reply took down an auction every other store was bidding in. Fail
    closed instead: an answer the exchange cannot date is an answer it cannot certify
    arrived in time, so the store falls back to its list price. (``nan`` already fails the
    comparison; this makes the string and ``None``-ish cases agree with it.)
    """
    bid = response.get("bid")
    if not isinstance(bid, Mapping):
        return False
    received_at = response.get("received_at")
    if received_at is None:
        return False
    try:
        arrived = float(received_at)
    except (TypeError, ValueError):
        return False
    return arrived <= float(deadline)


def collect_bids(
    roster: Sequence[Mapping[str, Any]],
    responses: Iterable[Mapping[str, Any]],
    now: float,
) -> list[BidEntry]:
    """Represent every rostered store, exactly once, in roster order.

    Args:
        roster: the stores selected for this auction —
            ``{store_id, tier, product_ref, list_price}``. Roster order is the output order.
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
    late: set[str] = set()
    for response in arrived:
        store_id = response.get("store_id")
        if store_id is None:
            continue
        if not _usable_response(response, deadline):
            late.add(str(store_id))
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
            reason = "response_after_deadline" if store_id in late else "no_response"
        else:
            reason = None

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
                )
            )
    return entries
