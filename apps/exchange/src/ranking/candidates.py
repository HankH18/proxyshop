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
a field the store filled in. So the candidate is assembled from a fixed list of keys, and no
published feature is among them. This also agrees with the published shape: ``Bid`` in
``packages/contracts/schemas/protocol.schema.json`` is ``additionalProperties: false`` and
declares no feature fields at all, so a store cannot even state one without failing validation.
Nothing here needs to *strip* them; it simply never copies them.

**Where the features come from instead, because "this module does not copy them" used to mean
"nobody produced them".** :mod:`.features` computes them, out of the roster's list price, the
offer's stated total price and delivery estimate, and the verdicts this exchange attested on
the store's claims — and :func:`.serving.rank_auction` applies it after the attestation, so the
records this module builds reach the scorer carrying the inputs the formula consumes. That
sequencing is the whole repair: with the features produced by nobody, four of the five terms
took their neutral value on every served request, ``rank_score`` was ``0.4 + 0.2*trust``, and
candidates from stores with equal trust TIED exactly — measured over a real socket, three
stores bidding 90/100/90 against a roster listing 100/200/300 all answered ``0.52``, and
inverting every list price returned bit-identical scores and the identical shortlist. The
formula was being applied correctly to inputs that did not exist.

**Copying is still forbidden, and that is not in tension with the paragraph above.** What
:mod:`.features` reads off the store is the store's PRICE and its DELIVERY ESTIMATE — the
commitments it is bidding with, which the buyer is shown and which the checkout and the trust
dimensions hold it to. What no module reads off the store is a number the store wrote under a
feature's name. ``intent_match`` remains produced by nobody on this path: its producer is
retrieval+rerank (T-031, unwired — see T-260) and a served auction has no retrieval source, so
that one term still takes its published neutral, which is the "we do not know" the formula
already has a rule for.

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
* **The ``price`` tie-break is the store's own ``total_price``** — and since :mod:`.features`
  exists, so is one side of ``price_value`` (the other side is the roster's, which the store
  cannot write). The T-177 price wall in ``auction/collect.py`` is what stands between both of
  those and a 0.01 bid; the ranker does not know the wall exists.

The five published FEATURES are the part this module does close, and the claim is exactly this:
**none of them is copied**, so no key a store writes under a feature's NAME can move its own
``rank_score``. It is not the broader claim that nothing a store writes can move its score — a
store moves ``price_value`` by charging less and ``delivery_fit`` by promising sooner, which is
what bidding IS. What it cannot do is assert the score itself, and the difference between
stating an offer and grading one is the whole of R11's blindness.

Why ``message`` is on this deliberately narrow list, when a feature key is not
------------------------------------------------------------------------------
``message`` is the seller's PITCH (``Bid.message``) and it is the only key here that is
free-form, bidder-written PROSE. Adding it to a projection whose entire purpose is to refuse
what a bidder writes deserves the argument spelled out rather than assumed.

**It is admitted as an ASSERTION TO CHECK, never as evidence.** D55 is explicit about the
asymmetry: the platform's own rendering of its crawl is constrained to facts it already holds,
while *"a seller's purchased message carries the seller's motive and is therefore the one that
gets adversarially checked against the snapshot"*. Nothing downstream reads this string as a
fact. Its one and only consumer is :func:`.verification.pitch_text_of`, which hands it to
:func:`claim_verification.decompose_pitch`; the claims that come back are stamped
``seller_asserted`` — never ``scraped``, which would launder an assertion into an observation
the platform vouches for — and each is then verified against the catalogue snapshot THIS
EXCHANGE holds and attested with a MAC the bidder cannot compute
(:mod:`.attestation`). A pitch sentence therefore earns exactly what a structured claim earns:
``verified`` feeds ``verified_claim_ratio``, ``contradicted`` costs the published
``contradicted_claim`` penalty, and ``unsupported``/``ambiguous`` are worth what silence is
worth. **A store cannot score by writing prose; it can only be graded on it.**

**The contrast with a feature key is the point.** ``intent_match: 1.0`` would be a NUMBER read
straight into the formula by a producer that is the bidder. ``"a five-year warranty"`` is a
sentence with no path into ``rank_score`` except through a verifier holding the platform's own
catalogue — and against a 24-month catalogue row that sentence is a *cost*, which is the exact
inversion of what a lever is.

**Three properties that keep it that way, each checkable:**

* the field is read from the bid and written under its own name, so it can never be mistaken
  for one of the five: :data:`CANDIDATE_FIELDS` is asserted exactly, and ``message`` is not in
  ``contracts.ranking.RANK_FEATURES``;
* :func:`.features.attach_features` neither reads nor produces it, so it is not an input to any
  published term. The pitch reaches the score only via the attested verdicts computed one step
  earlier, which is why :func:`.serving.rank_auction` runs the attestation BEFORE the features;
* it is re-published on exactly ONE surface and nowhere else — the buyer-facing shortlist slot.
  ``collected_bid_records`` builds the accept-path record from four named keys (``bid_id``,
  ``store_id``, ``offer``, ``store_domain``) and this is not among them, and neither
  ``_ranked_out`` nor ``_excluded_out`` carries it.

  **That bullet used to say "it is not re-published" full stop, and D55 is why it no longer
  does.** A shop's purchased message is the sponsored half of this market: it buys no
  visibility and no score, and what it buys instead is the right to make its case in its own
  voice. Computing it, grading it and then dropping it at the shortlist boundary meant the
  platform's voice was the only one a shopper ever saw, so the one thing joining the network
  buys was the one thing the product could not show. ``ShortlistSlot.message`` now carries it
  (``SCHEMA_VERSION`` 3.0.0), verbatim, and :func:`.serving._with_offer_fields` is the single
  producer. The reflector this creates is bounded and stated rather than denied: at most four
  slots, at most ``maxLength: 1200`` characters each — the contract's bound, and the same one
  ``buyer_svc.pitch.writing.MAX_STORE_PITCH_CHARS`` already refuses past — and a longer message
  is published as ``null`` rather than cut, because a truncated pitch is words the store did not
  write attributed to the store. What did NOT change is the grading: the string still reaches
  ``rank_score`` only through verdicts this exchange attested under a MAC the bidder cannot
  compute.

**What it costs in memory: nothing new.** The string is already retained for the auction's TTL
inside ``BidEntry.bid`` — bounded at collection by ``MAX_BID_RESPONSE_BYTES`` — and the
decomposer bounds what it will read from it (``MAX_PITCH_CHARS`` / ``MAX_PITCH_CLAIMS``). This
line adds a reference to a string the exchange already holds, not a second copy of it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

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
    "message",
    "fallback",
    "fallback_reason",
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


def fallback_checkout_url(registered_domain: str, *, variant_ref: str | None = None) -> str:
    """The D22 destination on a domain the PLATFORM registered, with no discount on it.

    ``build_cart_permalink`` is not reused here and the difference is not cosmetic: that
    function's whole output is ``…/cart/{variant}:{qty}?discount={code}``, and before an accept
    there **is** no code. A fallback's pre-accept URL is a destination, not an entitlement, so it
    names the cart and nothing else. The authoritative permalink — the one with a real
    single-use code on it — is still built at accept time by
    :func:`~apps.exchange.src.checkout.provider.default_permalink`, out of the same registry
    lookup, so the two never name different hosts.

    Two destinations, decided by whether a variant is actually known
    ---------------------------------------------------------------
    * **``variant_ref`` stated** -> ``…/cart/{variant}:1``. One unit of the variant the offer
      prices. The quantity is ``1`` because a manufactured list-price offer names none, and
      one unit IS the semantic default for a quantity — unlike a variant, where ``1`` is not a
      neutral value but a *different specific variant*.
    * **``variant_ref`` absent** -> the seller's own front door, ``https://{domain}/``.

    This function used to answer ``…/cart/1:1`` unconditionally, and that URL was a lie the
    platform told in its own voice. Measured over ``fixtures/real-catalogs-demo`` (28,134
    variant records across nineteen stores) the native variant ids run 9 to 14 digits —
    histogram ``{9: 12, 10: 3, 11: 23, 12: 1, 13: 73, 14: 28022}`` — so ``1`` names no variant
    any of those stores issues, and ``services/shopify-stub`` answers it
    ``404 {"errors": "Variant 1 is not available"}``. The organic majority of the demo took
    this path: ``deploy/demo/exchange-deployment.json`` gives a ``bid_endpoint`` to 4 of its 19
    sellers, so 11 of the 15 served rows could only be fallbacks.

    **The front door is not a cart, and that is the point.** It is a destination the platform
    can stand behind without asserting anything about the merchant's catalogue, which is the
    same restraint the rest of this module keeps. Returning nothing instead would have been the
    other fail-closed option and it is the wrong one here: ``ranking.filters.domain_reason``
    fails closed on an offer with no checkout URL, so a silent store would be excluded before
    it could be ranked — and R10 says it "can still reach the shortlist", which is the property
    ``_completed_fallback_offer`` exists to keep.

    The domain is interpolated **verbatim**, deliberately. A registry row spelled
    ``https://shop.example.com`` produces ``https://https://shop.example.com/``, whose
    host is ``https`` and which ``is_on_domain`` therefore refuses — the candidate is excluded
    ``off_domain_checkout``, which is the direction to fail in. Normalising the value here would
    be this module quietly repairing the platform's own record and then vouching for the repair.

    Args:
        registered_domain: the platform's own registry row for this seller, verbatim.
        variant_ref: the storefront's own id for the variant the offer prices, or ``None``.
    """
    named = str(variant_ref).strip() if variant_ref is not None else ""
    if not named:
        return f"https://{registered_domain}/"
    return f"https://{registered_domain}/cart/{quote(named, safe='')}:1"


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
      claimed it was.** ``entry.fallback`` is true for every one of the TWELVE reasons in
      :data:`~apps.exchange.src.auction.collect.FALLBACK_REASONS`, not only ``no_response``, so
      a store CAN reach this completion by answering — it just has to answer *unusably*.
      Measured over the HTTP door: a reply whose ``offer`` is ``[]``, ``"free"``, ``null``, ``3``
      or absent comes back ``fallback=True`` with ``fallback_reason:
      bid_price_unreconcilable``, and the entry is completed and shortlisted. So is a Tier-0
      store, and so is one the T-177 price wall degraded.

      The count is twelve rather than seven since ``store_declined`` and ``store_refused``
      landed, and since ``bid_claim_unprovenanced``,
      ``response_timed_out`` and ``fan_out_capacity_exhausted`` were added beside them: a store that answers ``204`` with a decline reason, or
      ``422``, or that answers correctly but after the window closed, is no longer recorded as
      silence but is still a fallback, so it is completed too. That makes the point
      sharper rather than weaker — a store can now reach this branch by *explicitly refusing*
      to bid, which is as deliberate as an act gets. What it collects for doing so is unchanged,
      and is the paragraph below.

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
    return {
        **offer,
        "checkout_url": fallback_checkout_url(
            str(registered_domain),
            # From the OFFER, which is where `_list_price_bid` put the roster row's variant.
            # Reading it here rather than defaulting is what keeps the pre-accept destination
            # and the post-accept handoff naming the same cart: `accept.offer
            # .fallback_destination` reads the same field off the same offer.
            variant_ref=read(offer, "variant_ref", None),
        ),
    }


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
    # `collect_bids`' own verdict about this entry, carried under its own name so the SLOT can
    # say whose price it is publishing (R10/D55). It is the exchange's fact, in exactly the
    # sense `store_domain` is: `entry.fallback` is set on the branch that DISCARDED whatever
    # arrived under this store's name and substituted a catalogue offer the exchange wrote, and
    # `entry.fallback_reason` is one of the twelve values in `collect.FALLBACK_REASONS`. Neither is
    # reachable from `bid`, so this widens nothing a bidder can write — `Bid` declares neither
    # name and forbids extra properties, and a store that DID somehow state one would be
    # overwritten here by the entry's answer rather than believed.
    #
    # Read defensively because this projection accepts "anything exposing `store_id`, `bid` and
    # `claims`": a caller-built entry that states nothing says `fallback: False`, which is the
    # same answer a real bid gives and is the safe direction — the risk being guarded against is
    # a stand-in presented as a quote, not the reverse.
    fallback = bool(getattr(entry, "fallback", False))
    reason = getattr(entry, "fallback_reason", None) if fallback else None
    if fallback:
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
        # `Bid.message` — the seller's PITCH, and the one field on this projection that is
        # bidder-written PROSE. See the module header's "Why prose is safe here and a feature
        # key is not" for why widening the projection by this one key does not widen R11.
        "message": read(bid, "message", None),
        "fallback": fallback,
        "fallback_reason": None if reason is None else str(reason),
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
