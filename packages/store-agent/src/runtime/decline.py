"""The other half of ``BidRequest -> Bid | Decline`` (DESIGN §Interfaces, store-agent runner).

A decline is an ANSWER, not a failure. R10 asks that every solicited store either bid or be
accounted for, so "this store is not bidding, and here is the condition that decided it" has to
be a value the exchange can read, log and count — not an exception, and emphatically not a bid
with a placeholder price in it.

`contracts` pins no `Decline`: the fourteen protocol objects are the ones that cross a service
boundary as JSON, and a decline is currently consumed by the solicitation path only. It is
therefore defined here, as a frozen dataclass with plain-JSON fields, so it serializes the same
way a protocol model does (`dataclasses.asdict`, or the frozen suite's `_plain`) and can be
promoted into `packages/contracts` unchanged if a second service ever needs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class DeclineReason(StrEnum):
    """Why the advocate is not bidding. Closed, and every member names a CONDITION.

    Stable strings, because they are logged and counted: a merchant-facing explanation and a
    solicitation tally are both keyed off them, exactly as `authorize_discount`'s denial reasons
    are. A free-text reason would make "how often does store-alpha decline for want of stock"
    a text-search question.
    """

    #: The intent's cluster is not one the approved envelope told the agent to pursue.
    cluster_not_pursued = "cluster_not_pursued"
    #: Nothing in the catalog carries a usable list price, so nothing can be offered at one.
    no_priced_product = "no_priced_product"
    #: Products exist and are priced, but none satisfies the intent's hard constraints (R19).
    no_matching_product = "no_matching_product"
    #: A product matched, but the pixel feed says it is out of stock. Bidding it would be a
    #: claim about availability that the store's own evidence contradicts.
    no_available_product = "no_available_product"
    #: The request does not identify itself: no `auction_id`, or no store to answer for. The
    #: protocol requires both and the models refuse an empty one, so the alternative to this is
    #: a `ValidationError` thrown at whoever solicited the bid.
    unidentified_request = "unidentified_request"
    #: The store context cannot be read as a store context — a catalog entry that is not a
    #: mapping, a floor whose `min_price` is not a number, a commitment with no `key`, a list
    #: price that has no canonical form. The merchant service owns that shape; the advocate
    #: answers rather than raising into the solicitation, and carries the reason verbatim.
    unusable_store_context = "unusable_store_context"
    #: The offer cannot state an expiry that anyone can read, and an offer with no readable
    #: expiry is one `contracts.boundary.validate_bid` refuses outright.
    unstatable_offer_expiry = "unstatable_offer_expiry"
    #: The hook-provenance boundary refused the assembled bid (R8/T-152). This one is a DEFECT
    #: in the runtime rather than a business condition — a hosted agent that cannot prove its
    #: own bid must not emit it — so the detail carries the boundary's full refusal.
    provenance_refused = "provenance_refused"


@dataclass(frozen=True)
class Decline:
    """A refusal to bid, carrying the auction it answers and the condition that decided it."""

    auction_id: str
    store_id: str
    reason: DeclineReason
    detail: str = ""
    agent_version: str = field(default="")
    schema_version: str = field(default="")

    def __str__(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"decline({self.reason}) for auction {self.auction_id!r}{suffix}"


def is_decline(answer: object) -> bool:
    """Whether an answer from :func:`~store_agent.runtime.bid` is a decline.

    Offered so a caller never has to write ``not isinstance(answer, Bid)``, which is the same
    test spelled as a negation and quietly answers True for `None`.
    """
    return isinstance(answer, Decline)


__all__ = ["Decline", "DeclineReason", "is_decline"]
