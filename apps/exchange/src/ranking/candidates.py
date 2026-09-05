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
a field the store filled in. So the candidate is assembled from a fixed list of keys. The
published features are deliberately absent: ``intent_match`` is retrieval's output (T-031, and
unwired — see T-260), and an absent feature takes its published neutral value, which is the "we
do not know" the formula already has a rule for. This also agrees with the published shape:
``Bid`` in ``packages/contracts/schemas/protocol.schema.json`` is ``additionalProperties:
false`` and declares no feature fields at all, so a store cannot even state one without failing
validation. Nothing here needs to *strip* them; it simply never copies them.

**What that costs today, measured, because it is a real property of the served ranking and not
a footnote.** With ``intent_match``, ``verified_claim_ratio``, ``price_value`` and
``delivery_fit`` all absent on every served candidate, four of the formula's five terms take
their neutral value on every request — so ``rank_score`` is a function of the trust snapshot
alone, and candidates from stores with equal trust TIE exactly. The published tie-breaks then
decide the order, and ``shortlist.assign_slot_names`` falls through to rank position, so D29's
four slot names read out as 1st/2nd/3rd/4th rather than as four different reasons to pick. The
formula is being applied correctly to inputs that do not exist yet; the producer for the one
feature DESIGN names (``intent_match``, from retrieval+rerank) is T-260's, and until it lands
the served ranking is an eligibility gate plus a trust ordering rather than a five-term score.
That is worth stating plainly, because a shortlist that comes back looking sensible is exactly
the shape in which nobody notices.

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
``bid_id`` and forbids extra properties, so a well-formed reply cannot carry one, and honouring
one anyway would hand a store the last published tie-break (D13) as a free text field. Minting
does NOT make that tie-break unreachable, and saying so was an overclaim in the first draft of
this file: the mint is ``{auction_id}:{store_id}`` and ``store_id`` arrives on the same
unauthenticated roster, so a caller that rosters itself as ``aaa-store`` still sorts ahead of
``zzz-store`` on an exact tie — measured, in both input orders. What minting buys is that the
id is the exchange's *format*, uniform, and not an arbitrary string chosen per reply; the
residual lever is a name the buyer also sees. A list-price fallback (R10) needs the mint from
the other direction: it is a real, rankable offer that arrived from no store at all and still
has to be referable. The id is deterministic, unique inside one auction (``collect_bids``
returns exactly one entry per rostered store), and reproducible — two runs over the same
auction produce the same shortlist ``bid_ref``.

What this module does NOT close, stated here because an overclaiming comment is how the next
reader stops looking
-----------------------------------------------------------------------------------------
``offer`` and ``claims`` are copied from the store's document, because they are what the store
is answering WITH. Two consequences, both measured through the HTTP door:

* **A store no longer satisfies a hard constraint by writing ``"status": "verified"`` onto its
  own claim (ESC-020 — closed).** It used to: ``ranking/filters.py`` read that field, no
  ``status`` exists on the published ``Claim`` at all (``additionalProperties: false``), so
  nothing validated it and nothing produced it but the bidder — two identical stores, one
  adding the string, and the liar was ranked while the honest one was excluded
  ``hard_constraint_unsatisfied``, with ``verified_hard_fit_count`` (the FIRST published
  tie-break) moved to match. What closed it is a claim-verification producer plus an
  attestation, not a strip: ``ranking/verification.py`` runs ``claim_verification.verify``
  over each store's claims against the catalog snapshot THIS EXCHANGE holds, and stamps the
  verdict with an HMAC the bidder cannot compute (``ranking/attestation.py``);
  ``ranking/filters.py`` reads only that. This projection still copies ``claims`` verbatim, and that is now safe rather
  than merely admitted: whatever a store writes under ``status`` or under
  ``exchange_verification`` is dropped before verification and read by nothing after it. The
  cost is a real operational requirement — an exchange with no catalog wired verifies nothing
  and shortlists nobody on a hard-constrained intent.
* **The ``price`` tie-break is the store's own ``total_price``.** The T-177 price wall in
  ``auction/collect.py`` is what stands between that and a 0.01 bid; the ranker does not know
  the wall exists.

The five published FEATURES are the part this module does close: none of them is copied, so no
key a store invents can move its own ``rank_score``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .filters import read

__all__ = [
    "CANDIDATE_FIELDS",
    "candidate_from_entry",
    "candidates_from_entries",
    "fallback_checkout_url",
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


def fallback_checkout_url(registered_domain: str) -> str:
    """The D22 cart destination on a domain the PLATFORM registered, with no discount on it.

    ``build_cart_permalink`` is not reused here and the difference is not cosmetic: that
    function's whole output is ``…/cart/{variant}:{qty}?discount={code}``, and before an accept
    there **is** no code. A fallback's pre-accept URL is a destination, not an entitlement, so it
    names the cart and nothing else. The authoritative permalink — the one with a real
    single-use code on it — is still built at accept time by
    :func:`~apps.exchange.src.checkout.provider.default_permalink`, out of the same registry
    lookup, so the two never name different hosts.

    ``1:1`` because a manufactured list-price offer names neither variant nor quantity: it is
    one unit of the rostered product at its catalogue price. ``default_permalink`` defaults to
    exactly the same pair on exactly the same offer (``offer.get("variant_ref") or
    offer.get("variant_id") or 1``, and ``offer_quantity`` -> 1).

    The domain is interpolated **verbatim**, deliberately. A registry row spelled
    ``https://shop.example.com`` produces ``https://https://shop.example.com/cart/1:1``, whose
    host is ``https`` and which ``is_on_domain`` therefore refuses — the candidate is excluded
    ``off_domain_checkout``, which is the direction to fail in. Normalising the value here would
    be this module quietly repairing the platform's own record and then vouching for the repair.
    """
    return f"https://{registered_domain}/cart/1:1"


def _completed_fallback_offer(offer: Any, registered_domain: str | None) -> Any:
    """A manufactured list-price offer, given the checkout URL the platform's registry implies.

    **This is the other half of R10, and it is here rather than in ``collect_bids`` because this
    is where the trusted answer lives.** ``auction/collect.py`` builds the fallback offer out of
    catalogue data and cannot complete it: the only defensible source for a checkout host is the
    platform's ``store_id -> domain`` registry, and handing that registry to a pure collector
    would put the C10/D22 lookup in two places. So the offer arrives with no ``checkout_url``,
    ``ranking.filters.domain_reason`` fails closed on it — "the offer carries no usable checkout
    URL … failing closed (C10)" — and every fallback the exchange manufactured for itself was
    excluded before it could be ranked. R10 says a silent store "can still reach the shortlist";
    it did not.

    Three properties, and each is a refusal to make this convenient:

    * **Fallbacks only.** ``entry.fallback`` is ``collect_bids``' own verdict, set on the branch
      that discards whatever arrived under that store's name and substitutes a catalogue offer
      the exchange wrote. A HOSTED bid that omits its checkout URL is left exactly as it was and
      is still excluded, because completing it would let a store post no URL and be handed a
      platform-built one.

      **But "hosted" is not the complement of "fallback", and an earlier draft of this bullet
      claimed it was.** ``entry.fallback`` is true for all seven reasons in
      :data:`~apps.exchange.src.auction.collect.FALLBACK_REASONS`, not only ``no_response``, so
      a store CAN reach this completion by answering — it just has to answer *unusably*.
      Measured over the HTTP door: a reply whose ``offer`` is ``[]``, ``"free"``, ``null``, ``3``
      or absent comes back ``fallback=True`` with ``fallback_reason:
      bid_price_unreconcilable``, and the entry is completed and shortlisted. So is a Tier-0
      store, and so is one the T-177 price wall degraded.

      That is a real widening of the door and it is written down rather than implied — but it is
      not a lever, because of WHAT is on the other side of it. Everything the store wrote is
      already gone by then: ``_list_price_bid`` rebuilds the offer from the ROSTER, at the
      roster's list price, with an empty ``claims`` list, and ``shortlist`` labels the slot
      ``unverified`` rather than ``store-confirmed``. A store that garbles its reply to reach
      this branch trades its own price and every claim it could have made for its catalogue
      list price. There is no bid it could have sent that this is better than.
    * **Never over a URL that is already there.** A fallback carries none by construction; if one
      is somehow present it is not overwritten, so this can only ever add a destination where
      there was none, never redirect one.
    * **No domain, no URL.** ``NoRegisteredDomains`` is the wired default and knows nobody, so an
      exchange with no seller registry still shortlists no fallback. The absent lookup is a
      denial here exactly as it is everywhere else on this path.

    **C10/S8 is satisfied here, not loosened, and that distinction is the whole safety
    property.** S8's rule is that no checkout URL is ever returned off the seller's registered
    domain. The URL this builds IS the seller's registered domain — the platform's own record,
    read through the same ``RegisteredDomains`` port the accept path mints against. Nothing
    about the comparison in ``ranking.filters.domain_reason`` changes: the candidate still has
    to pass ``is_on_domain`` against ``store_domain``, and it passes because the destination can
    now be *established*, not because the check was relaxed for anyone. Reading any
    store-supplied value to build this URL WOULD loosen S8 — which is why the reply, the roster
    row and ``bid["store_domain"]`` are all unreachable from this function.

    **What the filter is worth on a fallback, stated plainly because an overclaiming comment is
    how the next reader stops looking:** ``domain_reason`` compares this URL against the same
    registry value it was built from, so for a completed fallback the C10 check is a tautology
    and provides no independent evidence. It can only fail where the registry ROW is malformed
    enough to move ``urlsplit``'s host — which is exactly the fail-closed behaviour the verbatim
    interpolation above is for. The consequence is that a bad registry row is not caught here:
    rows spelled ``127.0.0.1``, ``localhost``, ``*.example.com`` or a look-alike homograph all
    build a URL that passes, because the platform said that is where the seller lives. That is
    garbage-in on the platform's own record, not a spoof a store can mount — the store cannot
    write a registry row — but the check vouches for nothing on this path, and the real evidence
    for a fallback's destination is the registry's own correctness.

    Nothing read here came from the silent store. It could not have: the store never answered.
    """
    if not isinstance(offer, Mapping):
        return offer
    if read(offer, "checkout_url", None):
        return offer
    if not registered_domain or not str(registered_domain).strip():
        return offer
    return {**offer, "checkout_url": fallback_checkout_url(str(registered_domain))}


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
    store_domain = _registered_domain(registered_domains, store_id)
    offer = read(bid, "offer", None)
    if getattr(entry, "fallback", False):
        # R10's second half. A COPY, never a mutation: the same offer dict is still inside the
        # `BidEntry` the route renders as `entries`, and a projection that edited its input
        # would be rewriting what the auction reports it collected.
        offer = _completed_fallback_offer(offer, store_domain)
    claims = getattr(entry, "claims", None)
    if claims is None:
        claims = read(bid, "claims", None)

    return {
        "bid_id": mint_bid_id(auction_id, store_id),
        "store_id": store_id,
        "store_domain": store_domain,
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
