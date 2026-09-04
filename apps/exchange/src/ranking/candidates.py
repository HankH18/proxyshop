"""One auction's collected bids, projected into the candidate records :func:`rank` reads.

``collect_bids`` produces :class:`~exchange.auction.collect.BidEntry` objects — the exchange's
own representation of what every rostered store is offering. :func:`~exchange.ranking.rank`
reads a *candidate*: ``bid_id``, ``store_id``, ``store_domain``, an ``offer``, its ``claims``
and the five published features. This module is the one place those two shapes meet, and it is
a separate module rather than three lines inside the route because two of the rules it keeps
are security boundaries rather than plumbing.

**The projection NAMES its fields; it never passes the bid through.** A bid is a document the
*store* wrote. Handing it to the scorer whole would let a bidder write ``intent_match: 1.0``
into its own reply and win every auction it entered — R11's blindness lost not to a leak but to
a field the store filled in. So the candidate is assembled from a fixed list of keys and
nothing else reaches it. The published features are deliberately absent: ``intent_match`` is
retrieval's output (T-031, and unwired — see T-260/T-323), and an absent feature takes its
published neutral value, which is the "we do not know" the formula already has a rule for. This
also agrees with the published shape: ``Bid`` in ``packages/contracts/schemas/protocol.schema.json``
is ``additionalProperties: false`` and declares no feature fields at all, so a store cannot even
state one without failing validation. Nothing here needs to *strip* them; it simply never
copies them.

**``store_domain`` comes from the platform, never from the bid.** ``checkout/sellers.py`` spells
out why in full: on a bid the registered domain came from ``bid["store_domain"]`` — a field the
store wrote — so a store supplying both halves of the C10/D22 check passes its own check and the
buyer is handed a checkout on a host the platform never registered. The lookup here is the same
``RegisteredDomains`` source the accept path uses
(:func:`~exchange.accept.offer.platform_registered_domains`), whose default,
:class:`~exchange.checkout.sellers.NoRegisteredDomains`, knows nobody and therefore refuses
everybody. An exchange nobody has connected to the seller registry ranks nothing, which is the
direction to fail in.

``bid_id`` is **minted here, always**, and never read off the bid. ``Bid`` declares no
``bid_id`` and forbids extra properties, so a well-formed reply cannot carry one — which makes
honouring one pure attack surface: ``bid_id`` is the last published tie-break (D13), so a store
that could name its own would win every otherwise-exact tie by calling itself ``aaa``. A
list-price fallback (R10) has the same need from the other direction: it is a real, rankable
offer that arrived from no store at all and still has to be referable. ``{auction_id}:{store_id}``
is deterministic, unique inside one auction (``collect_bids`` returns exactly one entry per
rostered store), and reproducible — two runs over the same auction produce the same shortlist
``bid_ref``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .filters import read

__all__ = [
    "CANDIDATE_FIELDS",
    "candidate_from_entry",
    "candidates_from_entries",
    "mint_bid_id",
]

#: Every key a projected candidate carries. Stated as data so a test can assert the projection
#: adds nothing else — the whole point of naming the fields is that the set is checkable.
CANDIDATE_FIELDS: tuple[str, ...] = (
    "bid_id",
    "store_id",
    "store_domain",
    "offer",
    "claims",
)


def mint_bid_id(auction_id: str, store_id: str) -> str:
    """The exchange's own identifier for one store's bid in one auction."""
    return f"{auction_id}:{store_id}"


def _registered_domain(source: Any, store_id: str) -> str | None:
    """The platform's registered domain for ``store_id``, or ``None``.

    Accepts either spelling of the ``RegisteredDomains`` port — ``domain_for(store_id)`` or a
    plain callable — because both are in use in this tree, and treats a source that raises as a
    source that knows nothing. A registry that is down must not admit a candidate whose checkout
    destination the platform cannot vouch for; it must fail the same way an unregistered store
    does (C10/D22).
    """
    if source is None:
        return None
    lookup = getattr(source, "domain_for", None)
    if lookup is None and callable(source):
        lookup = source
    if lookup is None:
        return None
    try:
        domain = lookup(str(store_id))
    except Exception:
        return None
    return None if domain is None else str(domain)


def candidate_from_entry(
    entry: Any,
    *,
    auction_id: str,
    registered_domains: Any = None,
) -> dict[str, Any]:
    """One :class:`BidEntry` as a candidate record.

    Args:
        entry: a ``BidEntry`` — anything exposing ``store_id``, ``bid`` and ``claims``.
        auction_id: this auction's id; half of the minted ``bid_id``.
        registered_domains: the platform's ``store_id -> domain`` source. ``None`` means the
            platform holds no domain for anybody, so every candidate is off-domain.

    Returns:
        A mapping whose keys are exactly :data:`CANDIDATE_FIELDS`.
    """
    bid = entry.bid if isinstance(getattr(entry, "bid", None), Mapping) else {}
    # `entry.store_id` and never `bid["store_id"]`: whose bid a reply is, is the exchange's
    # attribution — `collect_bids` already stamps it over whatever the payload claimed.
    store_id = str(getattr(entry, "store_id", "") or "")
    offer = read(bid, "offer", None)
    claims = getattr(entry, "claims", None)
    if claims is None:
        claims = read(bid, "claims", None)

    return {
        "bid_id": mint_bid_id(auction_id, store_id),
        "store_id": store_id,
        "store_domain": _registered_domain(registered_domains, store_id),
        "offer": offer,
        "claims": list(claims or ()),
    }


def candidates_from_entries(
    entries: Sequence[Any],
    *,
    auction_id: str,
    registered_domains: Any = None,
) -> list[dict[str, Any]]:
    """Every entry of one auction, in the order the auction reports them."""
    return [
        candidate_from_entry(
            entry,
            auction_id=auction_id,
            registered_domains=registered_domains,
        )
        for entry in entries
    ]
