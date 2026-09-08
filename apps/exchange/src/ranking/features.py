"""The published features, produced for a served auction (R11).

:mod:`.scoring` applies the one published formula::

    rank_score = w_m*intent_match + w_e*verified_claim_ratio + w_t*trust
               + w_v*price_value + w_d*delivery_fit − policy_penalties

and it applies it correctly. What did not exist until this module was that four of those five
features were produced by NOBODY on a served request, so each took its published neutral value
and ``rank_score`` collapsed to ``0.4 + 0.2*trust``. That is not a smaller formula, it is a
different one: offer value had no effect, fit had no effect, and a store could halve its price
without moving. Measured over a real socket before this module existed, three stores bidding
90/100/90 against a roster listing 100/200/300 all answered ``rank_score=0.52``, and inverting
every list price on the roster returned bit-identical scores and the identical shortlist.

**Why the features were absent, per feature, because the four had four different causes:**

``price_value``
    Never computed anywhere, and not computable from the candidate alone: DESIGN.md:127
    publishes it as ``clamp((list_price − total_price)/list_price, 0, 1)``, and ``list_price``
    lives on the auction's ROSTER while ``total_price`` lives on the store's offer. The
    projection in :mod:`.candidates` reads the bid, so it never held both halves.
    :class:`~exchange.auction.collect.BidEntry` now carries the roster's list price, which is
    what makes this one a computation rather than a guess.
``verified_claim_ratio``
    The verdicts existed and were thrown away. :func:`.verification.attest_candidates` already
    runs the claim verifier over every candidate's claims against the exchange's own catalogue
    and stamps each verdict with a MAC; :mod:`.filters` reads them to decide hard constraints,
    and then nothing counted them. SPEC.md:63 defines ``verified_claim_ratio`` as a field of
    ``VerificationResult``, and ``packages/verification`` does not return it — so it is derived
    here, from the attested verdicts and from nothing else.

``delivery_fit``
    The input existed (``Offer.delivery_estimate_days``) and nothing read it. Reading it turned
    out to be only half the fix: the declared number is written by the bidder, so see
    :func:`delivery_fits`, :func:`dispatch_credibility` and D57 for why a delivery PROMISE is now
    weighed against the store's ``shipped_on_time`` record before it counts, and for exactly what
    is and is not modelled — this is still the one feature whose published definition names
    something (``Intent.ship_to``) the exchange does not hold.
``intent_match``
    **Genuinely not computable from what a served auction knows, and therefore not produced.**
    DESIGN.md:132 says it "comes from retrieval+rerank". The producer exists —
    :func:`exchange.retrieval.fit.intent_match_by_bid` — but it consumes ``retrieve()``
    assessments, which come from a candidate source (``ingest.graph``) that ``POST /auctions``
    has no handle on: a served auction is handed a ROSTER by its caller and queries no index.
    Substituting a plausible number would move rankings for a reason nobody could audit, which
    is worse than the neutral it takes today. The term stays neutral, and
    ``test_intent_match_has_no_served_producer_and_says_so_by_staying_neutral`` is the record.

Three properties this module keeps
----------------------------------
* **It COMPUTES; it never copies.** No value here is read from a field the bidder could write
  under a feature's name. What it does read from the store is what the store is *committing
  to* — its price and its delivery estimate — which is what an auction is for, and which the
  buyer is shown. The distinction is the whole of R11's blindness: a bidder may state its
  offer, and may not state its own score.
* **A fee, a tier and an envelope's contents are unreachable from here.** The inputs are
  named, one by one:
  the roster's list price, the offer's total price, the offer's delivery estimate, the trust
  snapshot's ``dims.shipped_on_time`` Beta, and the exchange's own attested claim verdicts.
  ``tier``, ``max_discount_pct``, any network fee and the envelope's commitments are on objects
  this module is handed and never reads — and neither is the snapshot's aggregate ``score``,
  which is ``trust``'s own term and is read one layer up in :func:`.scoring.feature_vector`.
* **Absent stays absent.** A feature this module cannot compute is REMOVED from the candidate
  rather than written as a number, so :func:`.scoring.feature_vector` applies the published
  ``when_absent`` value. Writing 0.0 for "we do not know" is the D13/D14 bias against every
  store nobody has measured yet.

The redefinition this module now carries (features version 3.0.0)
-----------------------------------------------------------------
The product is a MATCHING AND PERSUASION market (D55): a store buys a dedicated advocate that
writes a pitch for THIS shopper. **The published formula paid nothing for that.** Classify each
term by whether a store can move it at bid time and whether its value depends on this buyer:

===================== ====== ============================ ================
term                  weight movable at bid time           buyer-dependent
===================== ====== ============================ ================
intent_match          0.35   no (catalogue)                YES
verified_claim_ratio  0.20   YES                           no
trust                 0.20   no (months)                   no
price_value           0.15   YES                           no
delivery_fit          0.10   only as far as its record     no
===================== ====== ============================ ================

That last cell used to read a plain "YES", and it was the defect D57 closes. A promise costs a
store nothing to type, so an unbacked ``delivery_estimate_days`` bought up to a tenth of the
published score outright; the term is still movable at bid time, but only within what the store's
``shipped_on_time`` record will carry.

The movable-AND-buyer-dependent cell was EMPTY, and :func:`.scoring.score` is strictly additive
with no interaction term — so a store's argmax over pitches was identical for every buyer and
customization returned exactly zero. Worse, the store-agent learning loop would have measured
that correctly and converged every store onto one generic pitch.

Three features are redefined here to fill that cell and to stop the race to the bottom, all under
their existing published names and weights (which is why
:data:`contracts.ranking.RANKING_FEATURES_VERSION` exists — see there):

* :func:`verified_claim_ratio` counts only verified claims whose key lands on something in THIS
  buyer's intent, aggregated with diminishing returns rather than as a raw share. It is now
  movable at bid time AND buyer-dependent, and it cannot saturate, because the attainable set is
  (this buyer's asks ∩ this store's catalogue facts that survive verification) and that differs
  per store.
* :func:`price_value` saturates at :func:`band_depth` — full credit for clearing this auction's
  own price band, zero marginal return past it.
* ``delivery_fit`` compares CREDIBLE delivery estimates instead of declared ones (D57). Its feed
  was ``Offer.delivery_estimate_days`` verbatim, which is a number the bidding store writes: the
  auction normalised the declared numbers against each other, so the store that typed the
  smallest one took the term and nothing checked whether it had ever shipped that fast.
  :func:`dispatch_credibility` reads the store's ``shipped_on_time`` Beta off the trust snapshot
  this exchange already holds at ranking time, and :func:`credible_delivery_estimate` divides the
  quote by that posterior before the normalisation runs. A store nobody has watched dispatch has
  its promise UNADMITTED, so the feature is absent for it and reads the published neutral — not
  good, not bad, which is the whole of D13/D14 applied to a store with no history.

And one penalty is minted: :func:`contradicted_claim_events`, the only asymmetric downside in
the design. Without it, a false claim costs one auction's share of one ratio and nothing else,
so a persuasion market is a lying market.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from contracts.ranking import (
    CONTRADICTED_CLAIM,
    EVIDENCE_GAIN_BY_RELEVANCE,
    RELEVANCE_TIERS,
    canonical_field,
    diminishing_evidence,
    preference_term_conflict,
)

from .attestation import ATTESTATION_FIELD, attested_status
from .filters import VERIFIED, read, trust_row

__all__ = [
    "CONTRADICTED",
    "MIN_DISPATCH_CREDIBILITY",
    "MIN_DISPATCH_OBSERVATIONS",
    "MIN_QUERY_TOKEN",
    "PRODUCED_FEATURES",
    "QUERY_STOPWORDS",
    "TRUST_PRIOR_MASS",
    "UNDECIDED",
    "IntentSurface",
    "attach_features",
    "band_depth",
    "contradicted_claim_events",
    "credible_delivery_estimate",
    "delivery_estimate",
    "delivery_fits",
    "dispatch_credibility",
    "intent_surface",
    "offer_price",
    "price_band",
    "price_value",
    "verified_claim_ratio",
]

#: The features this module produces. `intent_match` is deliberately not among them, and
#: `trust` is not a candidate field at all — it comes off the trust snapshot inside
#: :func:`.scoring.feature_vector`.
PRODUCED_FEATURES: tuple[str, ...] = (
    "verified_claim_ratio",
    "price_value",
    "delivery_fit",
)

#: The R18 status that means this exchange reached NO verdict on a claim — as opposed to
#: reaching a negative one. ``packages/verification/src/statuses.py`` states it: "the catalog
#: says something, but not something that decides this claim", and
#: :func:`exchange.ranking.verification.attest_candidate_claims` attests it for every claim of
#: a store whose snapshot the exchange does not hold at all, with the reason "the exchange
#: holds no catalog snapshot for this store, so its claims could not be checked".
#:
#: It is named here, and only here, because :func:`verified_claim_ratio` is the one place in
#: this module that has to tell "we checked and found nothing behind it" (``unsupported``,
#: ``contradicted`` — the store's cost) apart from "we could not check" (this — the
#: exchange's own unknown, which is what ``when_absent`` exists for).
UNDECIDED = "ambiguous"

#: R18's verdict for a claim this exchange's own catalogue snapshot says OTHERWISE about — as
#: opposed to one it records nothing behind (``unsupported``) or could not compare
#: (:data:`UNDECIDED`). It is the only verdict that mints a penalty, and the distinction is the
#: reason the penalty is defensible: the exchange is not charging a store for its own gaps.
CONTRADICTED = "contradicted"

#: The shortest query token that can make a claim key buyer-relevant. Two-character tokens are
#: noise on this comparison — a bare unit, a stray "hx", the "l" in ``capacity_l`` — and a token
#: that matches everything makes every claim relevant, which is the same as making none of them.
MIN_QUERY_TOKEN = 3

#: Query words that name nothing a claim could be about. Kept deliberately tiny: this is a filter
#: against tokens that would match a key by accident, not a stemmer or a stop-list for retrieval,
#: and the shorter it is the less of the buyer's own wording this exchange silently discards.
QUERY_STOPWORDS: frozenset[str] = frozenset(
    {
        "and",
        "any",
        "are",
        "but",
        "can",
        "for",
        "from",
        "has",
        "have",
        "its",
        "not",
        "one",
        "our",
        "some",
        "than",
        "that",
        "the",
        "them",
        "they",
        "this",
        "under",
        "was",
        "what",
        "which",
        "with",
        "you",
        "your",
    }
)


def _number(value: Any) -> float | None:
    """``value`` as a finite float, or ``None``.

    ``bool`` is refused because ``True`` is not one day and not one dollar, and NaN/±inf are
    refused because every comparison against them is silently false — the fail-open direction
    on anything that then gets normalised or ordered.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def offer_price(offer: Any) -> float | None:
    """What this offer charges — ``total_price``, else ``unit_price``, else ``None``.

    DESIGN names ``total_price`` and that is what is read first. ``unit_price`` is the
    fallback rather than an absence for one reason, and it is an incentive rather than a
    convenience: an absent feature reads the published NEUTRAL 0.5, which is *better* than the
    0.0 an honest bid at the list price earns — so if omitting ``total_price`` made the
    feature absent, omitting it would pay. The published ``Offer`` requires the field; a reply
    that states only a unit price is judged on the price it did state.
    """
    if offer is None:
        return None
    for name in ("total_price", "unit_price"):
        price = _number(read(offer, name, None))
        if price is not None:
            return price
    return None


def price_band(list_prices: Iterable[Any]) -> tuple[float, float] | None:
    """``(low, high)`` — the band THIS auction's own roster spans, or ``None``.

    The roster's list prices and nothing else: the least and the most anybody in this auction
    is asking for the thing. ``None`` when fewer than two of them are readable positive numbers,
    or when they are all the same — an auction with one price has no band, and a band of zero
    width is not a scale anything can be measured against.

    Deterministic and order-independent (``min``/``max``), which R11 and S3's replay properties
    both need.
    """
    listed = [price for price in (_number(value) for value in list_prices) if price is not None]
    priced = [price for price in listed if price > 0.0]
    if len(priced) < 2:
        return None
    low, high = min(priced), max(priced)
    if high <= low:
        return None
    return (low, high)


def band_depth(band: tuple[float, float] | None) -> float | None:
    """The auction's price band expressed as a DISCOUNT DEPTH, or ``None``.

    ``(high − low)/high`` — the depth that would carry the dearest listing in this auction down
    to the cheapest one. That is what "clearing this auction's own price band" costs, measured
    in the same units :func:`price_value` measures a store's discount in, which is what lets the
    two be compared at all.
    """
    if band is None:
        return None
    low, high = band
    if high <= 0.0 or high <= low:
        return None
    return (high - low) / high


def price_value(
    list_price: Any, offer: Any, *, depth_to_clear: float | None = None
) -> float | None:
    """The store's discount depth, SATURATING at this auction's own price band, or ``None``.

    ``clamp(depth / depth_to_clear, 0, 1)`` where ``depth`` is DESIGN.md:127's
    ``clamp((list_price − total_price)/list_price, 0, 1)`` and ``depth_to_clear`` is
    :func:`band_depth` for this auction. Full credit for clearing the band; **zero marginal
    return past it** — the second half of that sentence is the entire point, and it is exactly
    0.000, not "a smaller amount".

    **Why lowering ``w_v`` is not a substitute, and this has to be understood before anyone
    proposes it again.** `price_value` reads only the store's own two numbers, so it is
    rival-independent and strictly increasing in depth: at ANY positive weight, deeper is weakly
    better, forever. Every store has a maximum discount it will authorise, so a term with that
    shape guarantees every store reaches its maximum and then differentiates on nothing — the
    network trains its own participants into a commodity market and destroys the margin it
    exists to broker (SPEC's core tenet, R11). A smaller weight makes the race slower, not
    finite. Saturation makes it finite: past the band there is nothing left to win.

    **What saturation does to roster list-price inflation, stated precisely rather than
    overclaimed.** ``depth`` still divides by the candidate's own ``list_price``, which arrives
    on an unauthenticated request body, so inflating it still raises ``depth``. What changes is
    that it no longer BUYS anything: the prize saturates at 1.0 and an honest store reaches the
    same 1.0 by discounting as deep as this auction's listings already spread. Before, 1.0 was
    reachable only by giving the product away, so inflation was the cheap route to the top of
    the term; now it is worth no more than clearing the band, which is what the term is for.
    Removing ``list_price`` from the denominator entirely would go further and was rejected:
    it makes `price_value` independent of what the store lists at, which hands the cheapest
    LISTING a free 1.0 for not discounting at all — including an R10 fallback from a store that
    never replied, which is the exact ordering `verified_claim_ratio`'s ``ambiguous`` split
    exists to prevent.

    ``depth_to_clear`` of ``None`` — an auction whose roster states no band — falls back to the
    published depth unchanged. There is no band to clear, so nothing saturates, and inventing a
    saturation point from a single price would move rankings for a reason nobody could audit.

    ``None`` — so the feature is absent and reads its neutral — only when the ROSTER states no
    price this exchange can compare against. That is the exchange's own unknown, and there is
    no proportion to take of a list price that is missing, zero, negative or unreadable.

    ``0.0`` when the roster prices the product but the OFFER states no price the exchange can
    read: nothing was charged that anybody can compare, so no saving has been shown. Fail-open
    there would be a store earning the neutral 0.5 by writing its price illegibly.

    Both clamps are load-bearing. A bid ABOVE its list price is not a negative discount, it is
    no discount (0.0), and a bid at or below zero — which the price wall in
    ``auction/collect.py`` refuses long before here — could not earn more than the 1.0 a free
    product earns.
    """
    listed = _number(list_price)
    if listed is None or listed <= 0.0:
        return None
    charged = offer_price(offer)
    if charged is None:
        return 0.0
    depth = _clamp01((listed - charged) / listed)
    scale = _number(depth_to_clear)
    if scale is None or scale <= 0.0:
        return depth
    return _clamp01(depth / scale)


@dataclass(frozen=True)
class IntentSurface:
    """What THIS buyer asked about — the only thing `verified_claim_ratio` counts against.

    ``tiers`` maps a canonical field name to the strongest relevance tier it holds
    (:data:`contracts.ranking.RELEVANCE_TIERS`); ``query_tokens`` are the words the buyer typed
    that survive :data:`QUERY_STOPWORDS` and :data:`MIN_QUERY_TOKEN`; ``refused`` records every
    preference field a published term ALREADY scores, together with which term took it.

    ``refused`` is carried rather than discarded because a dropped preference is a thing a store
    operator will ask about, and "we ignored it" and "`price_value` already scores it" are
    different answers.
    """

    tiers: Mapping[str, str]
    query_tokens: frozenset[str]
    refused: Mapping[str, str]
    #: Published TERMS the buyer asked on — ``{"price_value": "price", ...}``, term to the ask
    #: field that named it. A claim on the same axis is relevant at the weakest tier; see
    #: :data:`contracts.ranking.RELEVANCE_TIERS` for why it is demoted rather than refused.
    scored_axes: Mapping[str, str]

    def __bool__(self) -> bool:
        return bool(self.tiers) or bool(self.query_tokens) or bool(self.scored_axes)

    def relevance(self, key: Any, value: Any = None) -> str | None:
        """The tier ``(key, value)`` lands on in this intent, or ``None`` for neither.

        Fields first — a hard constraint the buyer made mandatory, then a preference they
        stated — because those are asks this exchange can compare by NAME, and a name is the
        unambiguous half of an intent.

        **Then the query, matched against the claim's KEY and nothing else.** The buyer's words
        are prose and a claim key is an identifier, so both sides are folded through
        :func:`_query_tokens_of` and a shared token is the match — ``capacity_l`` answers "does
        it have the capacity", ``frame_material`` answers "a frame material that lasts".

        The claim's VALUE is deliberately NOT matched, and this was tried: matching a verified
        claim's value against the query would let ``boiler_type = "heat exchange"`` answer a
        shopper who typed "heat-exchange", which is genuinely the right semantics. It is out
        because the case that motivated it did not survive measurement — S1's hosted agents
        claim ``list_price``, ``in_stock``, ``units_left`` and ``policy_action``, whose values
        are two numbers, a bool and a dict, so value-matching changed nothing there — and
        because with no measurement behind it, all it adds is a surface: a value is a string a
        store's own catalogue supplies, so quoting the shopper's words back inside a real
        product attribute would be worth an evidence tier. Key-only is what
        `contracts.ranking.RELEVANCE_TIERS` prices, and a store whose catalogue calls the field
        something the buyer did not say is answered by the tiers above, which match by NAME.

        ``value`` is still taken, and read by nothing here, because it is what the attestation
        MAC covers — the caller already holds it, and a later relevance rule that needs it
        should not change every call site to get it.
        """
        field = canonical_field(key) if key is not None else ""
        if field:
            tier = self.tiers.get(field)
            if tier is not None:
                return tier
            if self.query_tokens and _query_tokens_of(field) & self.query_tokens:
                return "query_term"
        # LAST, and weakest: the buyer asked on an axis a published term already SCORES, and this
        # claim is on that same axis. MEASURED on S1, which is why this tier exists at all: its
        # buyer states one preference (`price`) and a prose query, and its hosted stores claim
        # `list_price`, `in_stock`, `units_left` and `policy_action` — so with the price ask
        # refused outright, not one claim in the whole run landed on anything, the two stores
        # that bid scored exactly what the store that never replied scored
        # (`verified_claim_ratio` component 0.1 for all three), and
        # `e2e/test_s1_flow.py::test_the_ranking_moved_on_the_features_the_exchange_computed`
        # went red — correctly. A store proving its catalogue list price is what it says IS
        # answering a buyer who asked about price; what it is not is CUSTOMIZED, which is why it
        # earns 0.55 against silence's 0.5 rather than the 0.70 a proved must-have earns.
        if field and self.scored_axes:
            axis = preference_term_conflict(field)
            if axis is not None and axis in self.scored_axes:
                return "term_scored_ask"
        return None

    def gain(self, key: Any, value: Any = None) -> float:
        """The published evidence gain one verified claim earns. ``0.0`` when nobody asked."""
        tier = self.relevance(key, value)
        return 0.0 if tier is None else float(EVIDENCE_GAIN_BY_RELEVANCE.get(tier, 0.0))


#: An intent this exchange could read nothing out of. Every claim is irrelevant against it, so
#: every candidate's `verified_claim_ratio` is ABSENT and reads the published neutral — the term
#: discriminates nobody rather than guessing which of a store's facts this buyer wanted.
EMPTY_SURFACE = IntentSurface(tiers={}, query_tokens=frozenset(), refused={}, scored_axes={})


def _query_tokens_of(text: Any) -> frozenset[str]:
    """The comparable tokens of ``text`` — canonicalised, split, stopworded, length-filtered."""
    if not isinstance(text, str):
        return frozenset()
    return frozenset(
        token
        for token in canonical_field(text).split("-")
        if len(token) >= MIN_QUERY_TOKEN and token not in QUERY_STOPWORDS and not token.isdigit()
    )


def _strongest(current: str | None, candidate: str) -> str:
    """The stronger of two relevance tiers, in the published order."""
    if current is None:
        return candidate
    order = {tier: index for index, tier in enumerate(RELEVANCE_TIERS)}
    return (
        current if order.get(current, len(order)) <= order.get(candidate, len(order)) else candidate
    )


def intent_surface(intent: Any) -> IntentSurface:
    """What this buyer asked about, folded into the shape :class:`IntentSurface` compares with.

    Three sources, in the published tier order — a hard constraint the buyer made mandatory, a
    preference they stated, and a word they typed into the query.

    **A preference field a published term already scores is REFUSED here**, through
    :func:`contracts.ranking.preference_term_conflict`, and this is the structural rule that
    stops the formula scoring one thing twice. A ``price`` preference is already `price_value`
    and a ``delivery`` preference is already `delivery_fit`; admitting either one would make a
    verified ``price_usd`` claim buy `verified_claim_ratio` as well, so the same number would be
    paid for under two published names. The refusal is the same one `intent_match` owes — see
    :data:`contracts.ranking.PREFERENCE_FIELD_TERMS` for the measured hazard, which is that S1's
    intent carries exactly one preference and it is ``{price, minimize, 1.0}``.

    An intent this function cannot read yields :data:`EMPTY_SURFACE`, and an empty surface is
    NOT an excuse to fall back to counting everything: "this buyer asked about nothing this
    exchange can read" is an unknown, and an unknown feature reads its published neutral rather
    than a number invented from the claims a store chose to send.
    """
    if intent is None:
        return EMPTY_SURFACE
    tiers: dict[str, str] = {}
    refused: dict[str, str] = {}
    scored_axes: dict[str, str] = {}

    def add(raw_field: Any, tier: str) -> None:
        field = canonical_field(raw_field) if raw_field is not None else ""
        if not field:
            return
        axis = preference_term_conflict(field)
        if axis is not None:
            # An ask on an axis a published term already SCORES. It never enters `tiers` — a
            # claim on it is relevant at the weakest tier and never at a constraint's — so the
            # price and delivery axes are the same weight of evidence however the buyer phrased
            # the ask, and neither can be dressed as a must-have.
            scored_axes.setdefault(axis, field)
            return
        tiers[field] = _strongest(tiers.get(field), tier)

    constraints = read(intent, "hard_constraints", None)
    if isinstance(constraints, Iterable) and not isinstance(constraints, (str, bytes)):
        for constraint in constraints:
            add(read(constraint, "field", None), "hard_constraint")

    preferences = read(intent, "preferences", None)
    if isinstance(preferences, Iterable) and not isinstance(preferences, (str, bytes)):
        for preference in preferences:
            raw_field = read(preference, "field", None)
            if raw_field is None:
                continue
            taken = preference_term_conflict(raw_field)
            if taken is not None:
                # RECORDED as refused, because that is the answer `intent_match` owes: a
                # preference a published term already scores may never reach `intent_match`, and
                # a store operator asking why needs the term's name rather than silence.
                refused[canonical_field(raw_field)] = taken
            add(raw_field, "preference")

    return IntentSurface(
        tiers=tiers,
        query_tokens=_query_tokens_of(read(intent, "query", None)),
        refused=refused,
        scored_axes=scored_axes,
    )


def verified_claim_ratio(
    claims: Any, *, store_id: Any = None, surface: IntentSurface | None = None
) -> float | None:
    """This candidate's verified evidence **about what THIS buyer asked**, or ``None``.

    Read :data:`contracts.ranking.EVIDENCE_GAIN_BY_RELEVANCE` first: the value is
    ``1 - Π(1 - gain_i)`` over the claims this exchange attested ``verified`` whose key lands on
    something in this intent, and the gain depends on how hard the buyer asked (a hard
    constraint, a stated preference, a word in the query).

    **Why this is not the share-of-decided-claims ratio it used to be.** The product is a
    matching-and-persuasion market: a store buys a dedicated advocate that writes a pitch for
    THIS shopper (D55). Under a raw ratio nothing in the published formula paid for that — every
    term was either fixed at bid time (`trust`, `intent_match`) or the same number for every
    buyer (`price_value`, `delivery_fit`, and the old ratio), the formula is strictly additive
    with no interaction term, and so a store's argmax over pitches was IDENTICAL FOR EVERY
    BUYER. Customization returned exactly zero. The store-agent learning loop would have
    measured that correctly and converged every store onto one generic pitch. This is the cell
    the redirect needs filled: movable at bid time AND dependent on this buyer.

    Four consequences, each deliberate:

    * **Claim-stuffing pays nothing.** A verified claim about something nobody asked contributes
      exactly 0.0 gain — it does not raise the value and does not dilute it either, so it is
      worth precisely what silence is worth. Ten verified claims about things nobody asked lose
      to three about things this buyer did.
    * **The ceiling is per store, so the term does not go flat at full effort.** The attainable
      set is (this buyer's asks ∩ this store's catalogue facts that survive verification), which
      differs between two stores with different inventory, and the aggregation itself never
      reaches 1.0. There is always another relevant fact worth having.
    * **Silence is still neutral and evidence still beats it.** A candidate with no relevant
      DECIDED claims has no evidence to aggregate, so the feature is ABSENT and reads the
      published `verified_claim_ratio_when_absent` (0.5) — the D13/D14 rule, unchanged. Every
      published gain is above 0.5, so one relevant proved fact already beats saying nothing.
    * **A relevant claim this exchange decided against scores 0.0**, which is below silence. That
      ordering is the old one and it is kept: an unevidenced claim is a cost, silence is only
      neutral. What a CONTRADICTED claim additionally costs is the published
      ``contradicted_claim`` penalty (:func:`contradicted_claim_events`), which is where the
      asymmetric downside of lying now lives.

    Everything about WHOSE verdict counts is unchanged. The status is read only out of the
    attested :data:`.attestation.ATTESTATION_FIELD` block through
    :func:`.attestation.attested_status`, which checks a MAC the bidder cannot compute and
    refuses a verdict attested for another store; a ``status`` the claim's author wrote is read
    by nothing, exactly as in :func:`.filters.verified_attributes`.

    :data:`UNDECIDED` — R18's ``ambiguous``, the verdict for a claim no comparison was made on —
    still counts as nothing at all rather than as a failure. The case that reaches it in bulk is
    an exchange holding no catalogue snapshot for the store, and counting those as failures
    writes 0.0 for "we do not know", to the stores that ANSWERED. MEASURED on the S1 demo before
    that split: two hosted stores that bid read 0.0 while the silent store's R10 fallback — no
    claims, so no evidence, so the published neutral — read 0.5 and took the top of the
    shortlist off both of them, and accepting a fallback mints no discount code.

    ``surface`` of ``None`` means the caller stated no intent, which is
    :data:`EMPTY_SURFACE`: nothing is relevant, so every candidate's feature is absent and this
    term discriminates nobody. That is the honest answer for an auction whose intent this
    exchange could not read, and it is the same direction every other unknown fails in here.
    """
    presented = list(claims or ())
    if not presented:
        return None
    asked = EMPTY_SURFACE if surface is None else surface
    gains: list[float] = []
    decided = 0
    for claim in presented:
        key = read(claim, "key", None)
        value = read(claim, "value", None)
        if asked.relevance(key, value) is None:
            # Nobody asked. Not a cost and not a credit — the same as not having said it.
            continue
        status = attested_status(
            read(claim, ATTESTATION_FIELD, None),
            key=key,
            value=value,
            unit=read(claim, "unit", None),
            subject=store_id,
        )
        # `None` — no attestation, a forged one, or one minted for another store — is NOT
        # undecided. It is a claim this exchange has no readable verdict on, and R12's rule
        # applies: an unreadable answer is not a permissive one. It counts as decided and
        # unverified, exactly as it did before this redefinition.
        text = str(getattr(status, "value", status))
        if text == UNDECIDED:
            continue
        decided += 1
        if text == VERIFIED:
            gains.append(asked.gain(key, value))
    if not decided:
        return None
    return _clamp01(diminishing_evidence(gains))


def contradicted_claim_events(claims: Any, *, store_id: Any = None) -> list[str]:
    """One :data:`contracts.ranking.CONTRADICTED_CLAIM` event per claim the catalogue contradicts.

    **Why a penalty and not a smaller ratio.** Before this, a false claim cost one auction's
    share of one ratio and nothing else: verdicts are minted per auction and carried between
    none, so a store paid for a contradiction only in the auction it was caught in, and with
    cheap generation an aggressive claim had positive expected value. A persuasion market whose
    only feedback on a lie is "that particular sentence scored zero" is a lying market. This is
    the one asymmetric downside in the design.

    **Contradicted only, and regardless of what the buyer asked.** ``unsupported`` ("the
    catalogue records nothing behind this") and :data:`UNDECIDED` are the exchange's own gaps as
    much as the store's, and charging for them would bill stores for an unfinished crawl. A
    contradiction is different in kind: this exchange's own snapshot of that store says
    otherwise. And it is charged whether or not this buyer asked about it — a store does not get
    to lie for free about the things nobody happened to ask.

    The bound is the published catalogue's: :meth:`RankingWeights.total_penalty` clamps the sum
    to ``max_total_penalty``, so a bid carrying a hundred contradicted claims costs what the
    catalogue says the worst case costs and not a hundred times one claim.
    """
    events: list[str] = []
    for claim in claims or ():
        status = attested_status(
            read(claim, ATTESTATION_FIELD, None),
            key=read(claim, "key", None),
            value=read(claim, "value", None),
            unit=read(claim, "unit", None),
            subject=store_id,
        )
        if str(getattr(status, "value", status)) == CONTRADICTED:
            events.append(CONTRADICTED_CLAIM)
    return events


#: The neutral Beta's total mass — ``alpha + beta`` — on a trust dimension nobody has observed.
#:
#: ``fixtures/manifest.json``'s ``trust_prior`` publishes Beta(2, 2): "a new store is not
#: 'probably fine', it is 'unknown'". ``trust.scoring.score`` serves all six dimensions always,
#: an unobserved one at that prior rather than omitted, so 4.0 is the mass a store carries
#: having been observed dispatching exactly nothing.
#:
#: It is restated here rather than imported because :mod:`exchange.ranking` imports nothing out
#: of :mod:`trust` — the exchange READS a served snapshot, it does not run the scorer, and
#: importing the engine to reach one float would couple the serving path to the scorer's
#: deployment.
#:
#: **What a mismatched prior would actually do, stated because the first draft of this comment
#: claimed the line "cannot be erased" and that is false.** The prior is manifest-driven
#: (``trust.scoring.PRIOR_ALPHA``/``PRIOR_BETA``), and a deployment whose prior carried mass 9 or
#: more would clear :data:`MIN_DISPATCH_OBSERVATIONS` with ZERO observations behind it — every
#: store admitted at the prior, which is exactly the "unmeasured store collects the term" failure
#: this constant exists to prevent. The number here has to track the manifest's; it is not a
#: conservative guess that degrades gracefully.
TRUST_PRIOR_MASS = 4.0

#: How much observed dispatch evidence a store needs before its delivery promise is ADMITTED to
#: ``delivery_fit`` at all, measured as ``alpha + beta`` in excess of :data:`TRUST_PRIOR_MASS`.
#:
#: **Read what this actually measures before reading the number: WEIGHTED, DECAYED evidence mass,
#: which is not an episode count and must not be described as one.** ``TrustDims`` publishes
#: exactly ``alpha``, ``beta`` and ``decayed_at`` per dimension (``trust.snapshot.routes``'s
#: ``PUBLISHED_DIMENSION_FIELDS``), so mass is the only evidence signal a served snapshot carries
#: for one dimension — there is no count to read. Two consequences follow and both are real:
#:
#: * **A miss weighs twice a keep.** ``trust.reconcile`` emits ``fulfilled`` at weight 1.0 and
#:   ``contradicted`` at 2.0, so three broken promises (mass 6.0) clear this floor while four kept
#:   ones (mass 4.0) do not. That is the defensible direction — the floor exists to stop an
#:   UNMEASURED store being judged, not to shelter a measured bad one — but it is a real asymmetry
#:   and it is written down rather than left to be discovered.
#: * **Decay moves the line.** Observations decay toward the prior (30-day half-life, D17), so
#:   five clean dispatches clear the floor only if they are contemporaneous; spread across a
#:   half-life they land near mass 7.7 and the promise is NOT admitted. A real store needs closer
#:   to ten dispatches inside a couple of half-lives. That makes this a staleness gate as well as
#:   an evidence gate — an old record is not a current promise — which is intended.
#:
#: **Five is borrowed as a MAGNITUDE from ``fixtures/manifest.json``'s ``new_store_prior_n``, and
#: it is not the same measurement.** That constant is a count of clean episodes and drives the
#: snapshot's store-level ``low_data`` flag; this one is per-dimension weighted mass. They will
#: disagree at the edges, deliberately: ``low_data`` answers "is this store new to the network"
#: over all six dimensions, and the question here is the narrower "has anyone watched THIS store
#: dispatch enough times to believe its next dispatch promise". Taking the magnitude rather than
#: inventing one keeps the two in the same order of evidence without pretending they are one
#: number.
#:
#: For scale, at decay 1.0 against the Beta(2, 2) prior: five kept promises post a posterior of
#: ``7/9 = 0.778`` and five broken ones ``2/14 = 0.143`` (five contradictions at weight 2.0 put
#: 10 on ``beta``). Under the floor the posterior is dominated by the prior, so admitting it would
#: score the exchange's own ignorance as a fact about the store.
MIN_DISPATCH_OBSERVATIONS = 5.0

#: The floor the ``shipped_on_time`` posterior is clamped at before it divides a quoted estimate,
#: which is what bounds the inflation at ``1/0.25 = 4x`` the store's own number.
#:
#: A posterior of 0.25 is a store whose dispatch promises fail three times for every time they
#: hold; that store is already decided, and letting the divisor keep shrinking past it would push
#: the effective quote past every rival's — turning a bounded discount on one term into effective
#: exclusion from it, which is a different and unbounded thing.
#:
#: **What 4x does and does not buy, because the arithmetic is easy to state wrongly.** It does NOT
#: mean an unreliable store always loses to a reliable one; ordering depends on the QUOTES as well
#: as the records, and the break-even is exact: a store quoting ``d_bad`` at or below the floor
#: loses to a rival quoting ``d_good`` with posterior ``p`` if and only if
#: ``d_good / p < d_bad / 0.25`` — that is, ``d_good < 4 * p * d_bad``. Against a rival with a
#: near-spotless ``p = 0.857``, a store promising 1 day it never keeps is beaten by a 3-day
#: promise (3 < 3.43) and beats a 4-day one (4 > 3.43). A term that instead guaranteed the
#: unreliable store lost at every quote would not be a delivery comparison at all — it would be a
#: trust term wearing ``delivery_fit``'s name, which is the double-count D57 exists to avoid.
MIN_DISPATCH_CREDIBILITY = 0.25


def delivery_estimate(offer: Any) -> float | None:
    """``Offer.delivery_estimate_days`` as a usable number of days, or ``None``.

    Negative and non-finite values are ``None`` rather than clamped, and that is the whole
    guard on this input: normalised across the auction, the SMALLEST estimate wins the term
    outright, so a store writing ``-1000`` would take it. A promise of minus a thousand days
    is not a fast delivery, it is not a delivery estimate at all.

    This is the DECLARED number and nothing else. What reaches :func:`delivery_fits` is this
    number after :func:`credible_delivery_estimate` has weighed it against the store's record;
    see there for why the declared value alone is not a feed a published weight may be spent on.
    """
    days = _number(read(offer, "delivery_estimate_days", None))
    if days is None or days < 0.0:
        return None
    return days


def dispatch_credibility(row: Any) -> float | None:
    """One store's ``shipped_on_time`` posterior, or ``None`` when it has not earned one.

    ``row`` is the store's row out of the trust snapshot the exchange already holds at ranking
    time — :func:`.filters.trust_row` is the one reader that turns a snapshot into one, and it is
    reused rather than reimplemented so the blacklist gate (R12) and this feed can never disagree
    about which row belongs to whom.

    ``TrustDims.shipped_on_time`` is a Beta and the posterior is ``alpha / (alpha + beta)``. That
    dimension is the one the trust engine sets by grading a store's PROMISED dispatch window
    against what actually shipped (``trust.reconcile.engine``: one-sided, so shipping early is
    never a broken promise), which is why it — and not the aggregate ``score`` — is what a
    delivery promise has to be weighed against.

    ``None``, meaning **the store's promise is not admitted**, in three cases:

    * there is no row for the store at all — reachable by a direct caller, but NOT on the served
      route, where R12's blacklist gate excludes a store with no row before it can be scored
      (:func:`.filters.blacklist_reason`, ``blacklist_unreadable``). It is handled anyway because
      this is an exported function and "no row" must not mean "raise";
    * the dimension is unreadable as two finite, non-negative numbers, or its mass is zero or not
      finite;
    * the evidence behind it is thinner than :data:`MIN_DISPATCH_OBSERVATIONS` in excess of
      :data:`TRUST_PRIOR_MASS` — the store has not been watched dispatching enough times for its
      posterior to be about the store rather than about the prior.

    **``None`` is "no record", and no record must read NEUTRAL — not good, not bad.** It is
    returned instead of a number because every number available here is a claim: 1.0 hands an
    unmeasured store the full value of a promise it has never once kept, and anything under 1.0
    charges it for a record it has not yet had the chance to build. The caller turns this into an
    ABSENT feature, which is the published neutral 0.5 (D13/D14) — a store with no shipping
    history can neither win this term nor be punished by it.
    """
    dims = read(row, "dims", None) if row is not None else None
    dimension = read(dims, "shipped_on_time", None) if dims is not None else None
    if dimension is None:
        return None
    alpha = _number(read(dimension, "alpha", None))
    beta = _number(read(dimension, "beta", None))
    if alpha is None or beta is None or alpha < 0.0 or beta < 0.0:
        return None
    # `_number` refuses a non-finite alpha or beta one at a time; their SUM can still overflow to
    # inf, and an infinite mass would clear the evidence gate and then read `alpha/inf = 0.0` —
    # the maximum penalty, for a store whose actual posterior is 0.5. Unreadable, not decided.
    mass = _number(alpha + beta)
    if mass is None or mass <= 0.0 or mass - TRUST_PRIOR_MASS < MIN_DISPATCH_OBSERVATIONS:
        return None
    return _clamp01(alpha / mass)


def credible_delivery_estimate(days: float | None, credibility: float | None) -> float | None:
    """A quoted dispatch estimate re-expressed in the days the store's own record supports.

    **The formula**::

        effective_days = days / max(credibility, MIN_DISPATCH_CREDIBILITY)

    where ``credibility`` is :func:`dispatch_credibility` — the store's ``shipped_on_time``
    posterior. A store whose posterior is 1.0 is quoted at FACE VALUE; one at 0.5 has its quote
    doubled; one at or below :data:`MIN_DISPATCH_CREDIBILITY` has it multiplied by 4 and no more.
    (The Beta(2, 2) prior means a real posterior approaches 1.0 without reaching it, so a
    spotless record pays a vanishing inflation rather than exactly none — ``7/9`` after five
    clean dispatches is ``1.29x``, ``22/24`` after twenty is ``1.09x``.)

    Three properties **of this function**, and the reasons each was required. Read the scoping
    literally: they are claims about the adjustment, not about the composed feature, and the
    difference is spelled out under "what this cannot do" below.

    * **monotone** — strictly decreasing in ``credibility`` and increasing in ``days``, so within
      this function keeping a promise never costs a store and quoting sooner never costs it
      either. No band, no bucket, no interval in which shipping later or promising later returns
      a smaller effective quote.
    * **scale-free** — a pure multiplication, so it holds no opinion about what a day is worth.
      No absolute days-to-score curve is introduced here, for the same reason
      :func:`delivery_fits` refuses to introduce one: every constant such a curve needs would be
      invented in this file and would move rankings for a reason nobody could audit.
    * **auditable** — one division by one published posterior against one published floor, all
      three of which a store can read off its own trust snapshot and check.

    ``None`` — the feature is ABSENT for this store, which reads the published neutral 0.5 — when
    either half is missing or unreadable: no quoted estimate (unchanged from before), no
    admissible record behind the quote, or a value that is not a finite number. NaN in particular
    is refused rather than propagated, for the reason :func:`_number` already gives: every
    comparison against NaN is silently false, which is the fail-open direction on a value that is
    about to be normalised and ordered.

    **What this cannot do**, in two parts, both written here rather than discovered later.

    *A quote of exactly 0.0 days is the fixed point of any scale-free map*, so a store with the
    worst possible record still reads 0.0 effective days if it claims same-day dispatch. That
    follows from scale-freedom rather than being an oversight; the guard against it is not in this
    function but in the dimension it feeds, because a store that keeps quoting 0 and keeps missing
    is being graded on exactly that promise by ``trust.reconcile.engine`` every time it ships.

    *The COMPOSED feature is not monotone across the admissibility cliff, and the direction is the
    one D13 already published.* An unadmitted promise reads the neutral 0.5, while an admitted one
    can read as low as 0.0 — so a store whose credible quote would land in the bottom half of its
    auction scores better unadmitted than admitted, and earning a record can cost it up to
    ``w_d/2``. This is the SAME incentive :func:`delivery_fits` already documents for saying
    nothing at all ("a store with a slow delivery scores better by saying nothing than by saying
    it. That is D13's published ``when_absent`` value, not this module's choice"); D57 adds a
    second route to the same absence — letting a dispatch record go thin or stale — and does not
    change the published neutral that creates it. Closing it means moving
    ``delivery_fit_when_absent``, which is a published contract and a different decision.
    """
    quoted = _number(days)
    earned = _number(credibility)
    if quoted is None or earned is None:
        return None
    return quoted / max(_clamp01(earned), MIN_DISPATCH_CREDIBILITY)


def delivery_fits(estimates: Sequence[float | None]) -> list[float | None]:
    """One ``delivery_fit`` per estimate, normalised across the auction. Read this first.

    **What is modelled:** sooner is better, among the offers this auction actually received.
    The fit is ``(slowest − mine)/(slowest − fastest)`` over the estimates handed in for this
    auction, so the fastest reads 1.0 and the slowest reads 0.0.

    **The estimates handed in are CREDIBLE ones, not declared ones**, and that is the whole
    difference between this feed and the one it replaces. ``delivery_fit`` carries a published
    weight of 0.10, and while the input was ``Offer.delivery_estimate_days`` verbatim, a store
    bought up to a tenth of the score by typing a smaller number — nothing anywhere asked whether
    it had ever shipped that fast. :func:`credible_delivery_estimate` divides each quote by the
    store's ``shipped_on_time`` posterior first, and :func:`dispatch_credibility` refuses to
    supply a posterior for a store nobody has watched dispatch. So a promise has to be earned
    before it counts, and an unearned one is ABSENT rather than good or bad (D57).

    **What is NOT modelled, stated plainly because DESIGN's sentence says more than the
    exchange can do:** DESIGN.md:128 has ``delivery_fit`` read ``Offer.delivery_estimate_days``
    *against* ``Intent.ship_to``. ``ship_to`` is a free-text destination and this system holds
    no shipping model — no zone table, no carrier transit times, nothing that could turn a
    destination into an expected number of days. So no absolute days→[0,1] curve is applied,
    because every constant such a curve needs would be invented here and would move rankings
    for a reason nobody could audit. What ``ship_to`` contributes is that every offer in one
    auction is quoting for the SAME destination, which is exactly what makes comparing their
    estimates to each other meaningful and comparing them across auctions meaningless.

    ``None`` — absent, therefore the published neutral 0.5 — in three cases:

    * an offer whose promise is not admitted: it declares no usable estimate (D13/D14's rule,
      and the one DESIGN states explicitly for this feature), or it declares one and the store
      has no dispatch record credible enough to weigh it against;
    * fewer than two admissible estimates in the whole auction, so there is no comparison to
      make. Without this the single store that bothered to state one would collect a free
      1.0 for having no competition;
    * a degenerate range — every declarant said the same thing — for the same reason
      :data:`exchange.retrieval.criteria.NEUTRAL_ALIGNMENT` exists: "nothing to discriminate
      on" is not "worst possible".

    Three consequences that are real and are not hidden:

    * a candidate's fit depends on the other candidates in its auction, which is what
      "normalised across the set" means wherever this codebase already does it;
    * because the published neutral (0.5) sits above the worst declared fit (0.0), a store with
      a slow delivery scores better by saying nothing than by saying it. That is D13's
      published ``when_absent`` value, not this module's choice, and it is written down here
      rather than discovered later;
    * the range spans every candidate in the auction, **including the ones the filters are
      about to exclude** — this runs before :func:`~exchange.ranking.rank` decides eligibility,
      and asking the features to depend on the eligibility verdict would put the filters and
      the scorer in a cycle. An extra estimate can only rescale the term monotonically, so it
      cannot reorder the eligible candidates *on this term*; it can compress or stretch the
      gap between them, which is a lever only for whoever writes the roster — and that is the
      buyer's own agent, not a bidding store.

    Deterministic: ``min``/``max`` over the admissible values are order-independent, and the
    same inputs produce the same floats (R11, and S3's replay properties).
    """
    admissible = [days for days in estimates if days is not None]
    if len(admissible) < 2:
        return [None] * len(estimates)
    fastest, slowest = min(admissible), max(admissible)
    span = slowest - fastest
    if span <= 0.0:
        return [None] * len(estimates)
    return [None if days is None else _clamp01((slowest - days) / span) for days in estimates]


def attach_features(
    candidates: Sequence[Any],
    entries: Sequence[Any] = (),
    *,
    intent: Any = None,
    trust_snapshot: Any = None,
) -> list[Any]:
    """Every candidate of one auction, carrying the features this module can compute.

    New records, never mutated ones: :func:`~exchange.ranking.rank` promises its inputs are
    never written to, and this runs one layer above where that promise is documented.

    ``entries`` are the :class:`~exchange.auction.collect.BidEntry` objects the candidates were
    projected from, positionally aligned — ``candidates_from_entries`` is a one-to-one list
    comprehension over them, and ``attest_candidates`` preserves order — and they are read for
    exactly one thing: the ROSTER's ``list_price``, which exists nowhere on the candidate. A
    caller with no entries to hand gets no ``price_value``, which is absent rather than
    invented.

    **Call this AFTER the claims are attested.** ``verified_claim_ratio`` counts attested
    verdicts, so run over unattested candidates it would count zero of them — a silent 0.0 for
    every honest store rather than a loud failure.

    A feature that cannot be computed is REMOVED from the record rather than written as a
    number. Removing rather than skipping is deliberate: it means the feature keys on a
    ranked candidate are exactly the ones this module produced, so a value that arrived from
    anywhere else — a projection that started copying the bid, a caller assembling candidates
    by hand — cannot reach the formula wearing a published feature's name.

    ``intent`` is the BUYER's, and it is what makes `verified_claim_ratio` buyer-conditional:
    it is folded once, here, into an :class:`IntentSurface` shared by every candidate in the
    auction, so two stores are graded against the same asks. A caller that states no intent gets
    :data:`EMPTY_SURFACE` — nothing is relevant, the feature is absent for everybody, and the
    term discriminates nobody rather than guessing. Nothing on the intent reaches the published
    WEIGHTS: `Intent.preferences[].weight` is not read here at all (D50).

    ``trust_snapshot`` is the same snapshot :func:`~exchange.ranking.rank` is handed, and it is
    read for exactly one thing: each store's ``dims.shipped_on_time`` Beta, which is what makes
    ``delivery_fit`` a comparison of CREDIBLE promises rather than declared ones (D57). It is
    resolved per store through :func:`.filters.trust_row` — by ``store_id`` rather than
    positionally, because a snapshot is a keyed structure and `trust_row` is already the one
    reader that decides which row is whose; two readers of one snapshot that disagreed about that
    would put the blacklist gate and this feed on different stores. **A caller that hands nothing
    gets the absent path for everybody** — every promise unadmitted, `delivery_fit` absent on
    every candidate, the published neutral 0.5 all round — which is the same discipline
    ``entries`` follows and the opposite of inventing a posterior nobody measured. The snapshot's
    aggregate ``score`` is NOT read here; that is `trust`'s own term, read in
    :func:`.scoring.feature_vector`, and the two are separate inputs on purpose.

    ``policy_events`` are APPENDED, never replaced: one
    :data:`contracts.ranking.CONTRADICTED_CLAIM` per claim this exchange's own snapshot
    contradicts. Appending matters because a record may already carry events from a producer
    upstream of this one, and a feature module that silently dropped a policy event would be
    forgiving a store for something it was already being charged for.
    """
    records: list[Any] = [
        dict(candidate) if isinstance(candidate, Mapping) else candidate
        for candidate in candidates or ()
    ]
    listed: list[Any] = [read(entry, "list_price", None) for entry in entries or ()]
    if len(listed) != len(records):
        # Nothing to align against — the caller handed a different number of entries than
        # candidates, so the roster's price cannot be attributed to a store with any
        # confidence. Absent beats guessed: `price_value` reads its neutral for all of them.
        listed = [None] * len(records)

    offers = [read(record, "offer", None) for record in records]
    store_ids = [read(record, "store_id", None) for record in records]
    # The QUOTE, then the RECORD behind it, then the normalisation across the auction — in that
    # order, because the credibility adjustment has to happen before the min/max is taken or the
    # scale it is taken over is still the declared one. A store with no admissible record
    # contributes `None` here, so it is out of the range as well as out of the term: an
    # unadmitted promise must not stretch the scale its rivals are measured on either.
    credibility = [
        dispatch_credibility(trust_row(str(store_id), trust_snapshot))
        if store_id is not None
        else None
        for store_id in store_ids
    ]
    fits = delivery_fits(
        [
            credible_delivery_estimate(delivery_estimate(offer), earned)
            for offer, earned in zip(offers, credibility, strict=True)
        ]
    )
    # ONE band and ONE surface for the whole auction, computed before the loop: both are
    # statements about the auction rather than about a candidate, and deriving either one
    # per candidate would let the answer depend on which candidate was being scored.
    depth_to_clear = band_depth(price_band(listed))
    surface = intent_surface(intent)

    for record, offer, store_id, list_price, fit in zip(
        records, offers, store_ids, listed, fits, strict=True
    ):
        if not isinstance(record, dict):
            continue
        claims = read(record, "claims", None)
        produced = {
            "price_value": price_value(list_price, offer, depth_to_clear=depth_to_clear),
            "verified_claim_ratio": verified_claim_ratio(
                claims, store_id=store_id, surface=surface
            ),
            "delivery_fit": fit,
        }
        for name in PRODUCED_FEATURES:
            value = produced[name]
            if value is None:
                record.pop(name, None)
            else:
                record[name] = float(value)
        events = contradicted_claim_events(claims, store_id=store_id)
        if events:
            existing = read(record, "policy_events", None)
            already = list(existing) if isinstance(existing, (list, tuple)) else []
            record["policy_events"] = [*already, *events]
    return records
