"""Fit scores, their measured inputs, and the per-bid audit record (T-031, acceptance 2).

A2 draws the line this module sits on: fit scoring is not replayable, so "fit inputs are
logged per bid for audit, not replay". That is why :class:`FitAssessment` carries
:class:`FitFeatures` and the reranker's name alongside the number — a score with no record of
what produced it is not auditable, and the trust replay (S3) deliberately does not cover it.

THE LEDGER HAZARD, and why :func:`annotate_bid_payload` is the primary API
-------------------------------------------------------------------------
A fit record is a fact *about a bid*, and the only frozen kind that means "this bid entered
the auction" is ``bid_placed``. But that kind's **count is load-bearing in two frozen
criteria**, so emitting a second one per bid is not a harmless extra row:

* T-082 asserts an EXACT per-kind multiset over the whole enum — ``bid_placed ==
  n_stores_solicited``. A fit event per bid doubles it.
* T-086 proves shadow mode by the **absence of any ``bid_placed``** for the auction. A
  retrieval that logs fit in shadow mode makes an inactive store look active.

And the exchange may not invent a kind (D24), so there is no nineteenth kind to move to.
The resolution is therefore *enrichment, not emission*: :func:`annotate_bid_payload` returns
the bid's own payload with the fit block merged into it — extra keys are explicitly permitted
by :func:`contracts.ledger.validate_ledger_payload` — and emits nothing at all. Whoever owns
the ``bid_placed`` emission carries the fit inside the event it was already going to write.

:func:`record_fit_scores` remains, because acceptance 2 requires fit to be logged with the
auction and nothing else in the repo emits ``bid_placed`` today. It **is** the bid-receipt
emission for the bids handed to it — never a second one alongside another producer's, and
never called in shadow mode. Both constraints are stated on the function.

Two further properties, each load-bearing:

* **The join is the bid's product, not its position.** A bid is matched to an assessment by
  ``offer.product_ref``, so reordering the bids cannot shift a fit score onto another store.
* **A missing assessment is recorded as missing.** A bid whose product never passed the hard
  filter gets ``fit_score: None`` and a ``fit_unavailable`` reason. Substituting a neutral
  number there would put a fabricated measurement into an audit trail whose whole purpose is
  to say what was measured.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from contracts.ledger import validate_ledger_payload

__all__ = [
    "FIT_LEDGER_KIND",
    "FitAssessment",
    "FitFeatures",
    "FitLogError",
    "annotate_bid_payload",
    "intent_match_by_bid",
    "record_fit_scores",
]

#: The frozen `LedgerEventKind` a fit record rides on — as an ANNOTATION of the bid's own
#: event (see this module's header), not as an extra event of its own.
FIT_LEDGER_KIND = "bid_placed"


class FitLogError(ValueError):
    """A fit record could not be written in the shape the frozen ledger vocabulary pins."""


@dataclass(frozen=True)
class FitFeatures:
    """What was actually measured for one candidate.

    Attributes:
        similarity: retrieval similarity in ``[0, 1]``, or ``None`` when the candidate came
            off the structured path and no comparison happened. ``None`` rather than a
            number, for the reason :attr:`ingest.graph.Candidate.scored` exists.
        preference_alignment: how the candidate scored on ``Intent.preferences``, in
            ``[0, 1]``, normalised across the eligible set.
    """

    similarity: float | None
    preference_alignment: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "similarity": self.similarity,
            "preference_alignment": self.preference_alignment,
        }


@dataclass(frozen=True)
class FitAssessment:
    """One candidate's fit, with the inputs and the reranker that produced it.

    Naming note, and it is not cosmetic. :attr:`fit_score` is this module's own per-CANDIDATE
    measurement. The ranker's per-BID feature is called ``intent_match``, and
    ``ShortlistSlot.fit_score`` is a third thing again (D29). Use :func:`intent_match_by_bid`
    to cross the product→bid boundary rather than renaming by hand at the call site, which is
    where the three would get conflated.
    """

    product_id: str
    canonical_name: str
    fit_score: float
    features: FitFeatures
    reranker: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "product_ref": self.product_id,
            "fit_score": self.fit_score,
            "fit_features": self.features.as_payload(),
            "reranker": self.reranker,
        }


def _bid_fields(bid: Any) -> Mapping[str, Any]:
    """Read a bid as a mapping, whatever shape the auction layer handed over.

    Three accepted shapes, because the natural call site is
    :func:`exchange.auction.collect_bids`, which yields :class:`~exchange.auction.BidEntry`
    **dataclasses** rather than mappings — requiring every caller to unwrap ``entry.bid``
    first is an API that reads as though it composes and does not.
    """
    if isinstance(bid, Mapping):
        return bid
    dump = getattr(bid, "model_dump", None)
    if callable(dump):
        return dict(dump())
    inner = getattr(bid, "bid", None)  # exchange.auction.BidEntry
    if isinstance(inner, Mapping):
        merged = dict(inner)
        store_id = getattr(bid, "store_id", None)
        if store_id is not None:
            merged.setdefault("store_id", store_id)
        return merged
    raise FitLogError(
        f"expected a bid mapping, a protocol model, or a BidEntry carrying one, got "
        f"{type(bid).__name__}"
    )


def _bid_view(bid: Any, auction_id: str) -> tuple[str, str, dict[str, Any], str]:
    """(bid_ref, store_id, offer, product_ref) for one bid, validated."""
    fields = _bid_fields(bid)
    store_id = str(fields.get("store_id") or "")
    if not store_id:
        raise FitLogError(f"bid carries no store_id, so its fit is unattributable: {fields!r}")
    raw_offer = fields.get("offer")
    offer: Mapping[str, Any]
    if isinstance(raw_offer, Mapping):
        offer = raw_offer
    elif raw_offer is None:
        offer = {}
    else:
        offer = _bid_fields(raw_offer)
    product_ref = str(offer.get("product_ref") or "")
    if not product_ref:
        raise FitLogError(
            f"bid {store_id!r} carries no offer.product_ref, so no assessment can be joined to it"
        )
    # `bid_id` first: that is what the ranker keys a candidate on, so a bid that already has
    # one must keep it rather than acquiring a synthetic ref the shortlist cannot match.
    bid_ref = str(fields.get("bid_id") or fields.get("bid_ref") or f"{auction_id}:{store_id}")
    return bid_ref, store_id, copy.deepcopy(dict(offer)), product_ref


def _fit_block(
    product_ref: str, assessments_by_product: Mapping[str, FitAssessment]
) -> dict[str, Any]:
    assessment = assessments_by_product.get(product_ref)
    if assessment is None:
        return {
            "fit_score": None,
            "fit_features": None,
            "reranker": None,
            "fit_unavailable": (
                f"no candidate assessment for product_ref {product_ref!r}: it was not "
                f"retrieved, or it did not satisfy the intent's hard constraints"
            ),
        }
    return {
        "fit_score": assessment.fit_score,
        "fit_features": assessment.features.as_payload(),
        "reranker": assessment.reranker,
    }


def annotate_bid_payload(
    payload: Mapping[str, Any],
    assessments: Sequence[FitAssessment],
    *,
    auction_id: str = "",
) -> dict[str, Any]:
    """Merge the fit block into a bid's own ``bid_placed`` payload. **Emits nothing.**

    This is the primary way fit reaches the ledger. The producer that was already going to
    write ``bid_placed`` for this bid writes it once, carrying the fit — so the per-kind
    multiset T-082 pins is untouched and shadow mode (T-086) stays silent, because a
    retrieval that emits no events cannot break an absence assertion.

    Args:
        payload: the bid's ``bid_placed`` payload, carrying at least ``store_id`` and
            ``offer.product_ref``.
        assessments: what ``retrieve()`` produced.
        auction_id: used only to synthesise a ``bid_ref`` when the payload has neither
            ``bid_id`` nor ``bid_ref``.

    Returns:
        A new payload — the input is not mutated, and the offer is deep-copied so the
        annotated event cannot alias the caller's live bid.

    Raises:
        FitLogError: the payload has no ``store_id`` or no ``offer.product_ref``.
    """
    bid_ref, store_id, offer, product_ref = _bid_view(payload, auction_id)
    by_product = {assessment.product_id: assessment for assessment in assessments}
    annotated = copy.deepcopy(dict(payload))
    annotated.update(
        {
            "bid_ref": bid_ref,
            "store_id": store_id,
            "offer": offer,
            "product_ref": product_ref,
            **_fit_block(product_ref, by_product),
        }
    )
    problems = validate_ledger_payload(FIT_LEDGER_KIND, annotated)
    if problems:
        raise FitLogError(
            f"annotated fit payload for bid {bid_ref!r} does not satisfy the frozen "
            f"{FIT_LEDGER_KIND!r} shape: {problems}"
        )
    return annotated


def intent_match_by_bid(
    bids: Iterable[Any], assessments: Sequence[FitAssessment], *, auction_id: str = ""
) -> dict[str, float | None]:
    """The ranker's ``intent_match`` feature, keyed by bid — the product→bid join.

    T-032 consumes one float per bid called ``intent_match``; this module measures one float
    per *product* called ``fit_score``. Nothing else does that join, and doing it by hand at
    the call site is where ``fit_score`` (this module's), ``intent_match`` (the ranker's) and
    ``ShortlistSlot.fit_score`` (D29's, a third concept) get conflated.

    Args:
        bids: bid mappings, protocol models or ``BidEntry`` objects.
        assessments: what ``retrieve()`` produced.
        auction_id: used only to synthesise a key for a bid carrying no ``bid_id``/``bid_ref``.

    Returns:
        ``{bid_ref: intent_match}``. The value is ``None`` — never a substituted neutral —
        for a bid whose product was not assessed, because there is no published
        ``intent_match_when_absent`` in ``NormalizationBounds`` for this module to honour.
        The ranker must decide what an unmeasured bid means; guessing here would hide it.

    Raises:
        FitLogError: a bid carries no ``store_id`` or no ``offer.product_ref``.
    """
    by_product = {assessment.product_id: assessment for assessment in assessments}
    matched: dict[str, float | None] = {}
    for bid in bids:
        bid_ref, _store_id, _offer, product_ref = _bid_view(bid, auction_id)
        assessment = by_product.get(product_ref)
        matched[bid_ref] = None if assessment is None else assessment.fit_score
    return matched


def record_fit_scores(
    recorder: Any,
    *,
    auction_id: str,
    bids: Iterable[Any],
    assessments: Sequence[FitAssessment],
) -> list[dict[str, Any]]:
    """Emit one ``bid_placed`` event per bid, carrying its fit. **Read the two rules first.**

    This function IS the ``bid_placed`` emission for the bids handed to it. Two rules follow
    from that, and both are frozen criteria rather than style:

    1. **Never call it alongside another producer of ``bid_placed`` for the same auction.**
       T-082 asserts ``bid_placed == n_stores_solicited`` as an exact multiset; two producers
       double it. When the auction layer starts emitting its own bid receipts, this call site
       must become :func:`annotate_bid_payload` on that event instead.
    2. **Never call it in shadow mode.** T-086 proves a store is in shadow mode by the
       *absence* of any ``bid_placed`` for the auction, so logging fit there makes an
       inactive store indistinguishable from an active one.

    Args:
        recorder: an :class:`~exchange.auction.LedgerRecorder`. Its contract — a sink that is
            down is recorded in ``failures`` rather than propagated — is what keeps the audit
            trail from being able to fail a live auction, and it is relied on here.
        auction_id: the auction these bids belong to. Every event carries it, which is what
            makes the records readable back with ``sink.for_auction(...)``.
        bids: bid mappings, protocol models or ``BidEntry`` objects carrying ``store_id`` and
            ``offer.product_ref``.
        assessments: what ``retrieve()`` produced. Matched to bids by product, never by
            position.

    Returns:
        The events, in bid order.

    Raises:
        FitLogError: a bid carries no ``store_id`` or no ``offer.product_ref``, or the
            assembled payload does not satisfy the frozen shape for ``bid_placed``.
    """
    events: list[dict[str, Any]] = []
    for bid in bids:
        payload = annotate_bid_payload(_bid_fields(bid), assessments, auction_id=auction_id)
        events.append(
            recorder.record(
                FIT_LEDGER_KIND,
                auction_id=auction_id,
                store_id=payload["store_id"],
                payload=payload,
            )
        )
    return events
