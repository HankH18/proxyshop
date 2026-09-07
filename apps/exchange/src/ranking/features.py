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
    The input existed (``Offer.delivery_estimate_days``) and nothing read it. See
    :func:`delivery_fits` for exactly what is and is not modelled, because this is the one
    feature whose published definition names something the exchange does not hold.
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
  the roster's list price, the offer's total price, the offer's delivery estimate, and the
  exchange's own attested claim verdicts. ``tier``, ``max_discount_pct``, any network fee and
  the envelope's commitments are on objects this module is handed and never reads.
* **Absent stays absent.** A feature this module cannot compute is REMOVED from the candidate
  rather than written as a number, so :func:`.scoring.feature_vector` applies the published
  ``when_absent`` value. Writing 0.0 for "we do not know" is the D13/D14 bias against every
  store nobody has measured yet.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .attestation import ATTESTATION_FIELD, attested_status
from .filters import VERIFIED, read

__all__ = [
    "PRODUCED_FEATURES",
    "UNDECIDED",
    "attach_features",
    "delivery_estimate",
    "delivery_fits",
    "offer_price",
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


def price_value(list_price: Any, offer: Any) -> float | None:
    """``clamp((list_price − total_price)/list_price, 0, 1)`` (DESIGN.md:127), or ``None``.

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
    return _clamp01((listed - charged) / listed)


def verified_claim_ratio(claims: Any, *, store_id: Any = None) -> float | None:
    """The share of this candidate's claims THIS EXCHANGE attested ``verified``, or ``None``.

    The verdict is read only out of the attested :data:`.attestation.ATTESTATION_FIELD` block,
    through :func:`.attestation.attested_status`, which checks a MAC the bidder cannot compute
    and refuses a verdict attested for another store. A ``status`` the claim's author wrote is
    read by nothing here, exactly as it is read by nothing in :func:`.filters.verified_attributes`
    — that equivalence is the point, because a hard constraint and this feature disagreeing
    about what "verified" means would be two rules wearing one word.

    Denominator is every claim this exchange DECIDED, including every one it decided against.
    ``unsupported`` ("the catalogue records nothing behind this") and ``contradicted`` ("the
    catalogue says otherwise") both count, and so does a claim carrying no readable
    attestation at all — dropping those would make the ratio a statement about a list that
    had already been filtered, and a store claiming ten things and evidencing one would read
    1.0.

    **A claim this exchange could not check is not in the denominator, and that is not the
    same rule wearing a looser spelling.** :data:`UNDECIDED` — R18's ``ambiguous`` — is the
    verdict for a claim no comparison was made on, and the case that reaches it in bulk is an
    exchange holding no catalogue snapshot for the store: :func:`.verification
    .attest_candidate_claims` attests every claim of such a candidate ``ambiguous`` with the
    reason "the exchange holds no catalog snapshot for this store, so its claims could not be
    checked". Counting those as failures writes 0.0 for "we do not know", which is exactly the
    D13/D14 bias this module's header says it never writes — and it does it to the stores that
    ANSWERED. MEASURED on the S1 demo, whose driver deliberately states no ``catalog``: two
    hosted stores that bid read ``verified_claim_ratio`` 0.0 while the silent store's R10
    fallback — no claims, so no ratio, so the published neutral — read 0.5 and took the top of
    the shortlist off both of them. Accepting a fallback is a handoff that mints no discount
    code (``accept/reasons.py``'s ``DENIAL_UNROUTABLE_FALLBACK`` note), so the shopper who
    followed the ranking got no code: an exchange with no catalogue wired ranked the store
    that never replied above the stores that did.

    ``None`` for a candidate with no claims at all, and for one whose every claim was
    undecided: a ratio over nothing is not 0.0, it is undefined, and the published neutral is
    what an undefined feature reads (D13/D14). A fallback offer carries no claims by
    construction (R10), so this is the answer it gets, and a store that presents claims this
    exchange checked and could not confirm scores BELOW one that presents none. That ordering
    is deliberate: an unevidenced claim is a cost, and silence is only neutral.

    Nothing a bidder writes can buy an exclusion. A claim the exchange never decided scores
    exactly as if it had not been presented, which is what a store could have had for free by
    staying quiet — so an undecided claim is never worth more than silence, and the only way
    to move this feature upward is still a verdict this exchange minted itself.
    """
    presented = list(claims or ())
    if not presented:
        return None
    verified = 0
    decided = 0
    for claim in presented:
        status = attested_status(
            read(claim, ATTESTATION_FIELD, None),
            key=read(claim, "key", None),
            value=read(claim, "value", None),
            unit=read(claim, "unit", None),
            subject=store_id,
        )
        # `None` — no attestation, a forged one, or one minted for another store — is NOT
        # undecided. It is a claim this exchange has no verdict on because the verdict on it
        # is unreadable, and R12's rule applies: an unreadable answer is not a permissive one.
        # It stays in the denominator, unverified, exactly as it was before this split.
        text = str(getattr(status, "value", status))
        if text == UNDECIDED:
            continue
        decided += 1
        if text == VERIFIED:
            verified += 1
    if not decided:
        return None
    return verified / decided


def delivery_estimate(offer: Any) -> float | None:
    """``Offer.delivery_estimate_days`` as a usable number of days, or ``None``.

    Negative and non-finite values are ``None`` rather than clamped, and that is the whole
    guard on this input: normalised across the auction, the SMALLEST estimate wins the term
    outright, so a store writing ``-1000`` would take it. A promise of minus a thousand days
    is not a fast delivery, it is not a delivery estimate at all.
    """
    days = _number(read(offer, "delivery_estimate_days", None))
    if days is None or days < 0.0:
        return None
    return days


def delivery_fits(estimates: Sequence[float | None]) -> list[float | None]:
    """One ``delivery_fit`` per estimate, normalised across the auction. Read this first.

    **What is modelled:** sooner is better, among the offers this auction actually received.
    The fit is ``(slowest − mine)/(slowest − fastest)`` over the estimates declared in this
    auction, so the fastest declared offer reads 1.0 and the slowest reads 0.0.

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

    * an offer that declares no usable estimate (D13/D14's rule, and the one DESIGN states
      explicitly for this feature);
    * fewer than two declared estimates in the whole auction, so there is no comparison to
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

    Deterministic: ``min``/``max`` over the declared values are order-independent, and the
    same inputs produce the same floats (R11, and S3's replay properties).
    """
    declared = [days for days in estimates if days is not None]
    if len(declared) < 2:
        return [None] * len(estimates)
    fastest, slowest = min(declared), max(declared)
    span = slowest - fastest
    if span <= 0.0:
        return [None] * len(estimates)
    return [None if days is None else _clamp01((slowest - days) / span) for days in estimates]


def attach_features(candidates: Sequence[Any], entries: Sequence[Any] = ()) -> list[Any]:
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
    fits = delivery_fits([delivery_estimate(offer) for offer in offers])

    for record, offer, list_price, fit in zip(records, offers, listed, fits, strict=True):
        if not isinstance(record, dict):
            continue
        produced = {
            "price_value": price_value(list_price, offer),
            "verified_claim_ratio": verified_claim_ratio(
                read(record, "claims", None), store_id=read(record, "store_id", None)
            ),
            "delivery_fit": fit,
        }
        for name in PRODUCED_FEATURES:
            value = produced[name]
            if value is None:
                record.pop(name, None)
            else:
                record[name] = float(value)
    return records
