"""Fit scores, their measured inputs, and the per-bid audit record (T-031, acceptance 2).

A2 draws the line this module sits on: fit scoring is not replayable, so "fit inputs are
logged per bid for audit, not replay". That is why :class:`FitAssessment` carries
:class:`FitFeatures` and the reranker's name alongside the number — a score with no record of
what produced it is not auditable, and the trust replay (S3) deliberately does not cover it.

:func:`record_fit_scores` writes one ``bid_placed`` event per bid. Three properties, each
load-bearing:

* **The kind is frozen.** ``bid_placed`` is one of the 18 kinds T-010 pinned (D24) and the
  exchange invents none; the payload is checked against the published
  ``LEDGER_PAYLOAD_SHAPES`` entry *before* it is emitted, at the producing boundary, where a
  malformed payload can still be fixed.
* **The join is the bid's product, not its position.** A bid is matched to an assessment by
  ``offer.product_ref``, so reordering the bids cannot shift a fit score onto another store.
* **A missing assessment is recorded as missing.** A bid whose product never passed the hard
  filter gets ``fit_score: None`` and a ``fit_unavailable`` reason. Substituting a neutral
  number there would put a fabricated measurement into an audit trail whose whole purpose is
  to say what was measured.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from contracts.ledger import validate_ledger_payload

__all__ = [
    "FIT_LEDGER_KIND",
    "FitAssessment",
    "FitFeatures",
    "FitLogError",
    "record_fit_scores",
]

#: The frozen `LedgerEventKind` a fit record rides on. The exchange may not invent a kind
#: (D24), and the fit of a bid is a fact about that bid entering the auction.
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
    """One candidate's fit, with the inputs and the reranker that produced it."""

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
    if isinstance(bid, Mapping):
        return bid
    dump = getattr(bid, "model_dump", None)
    if callable(dump):
        return dict(dump())
    raise FitLogError(f"expected a bid mapping or protocol model, got {type(bid).__name__}")


def record_fit_scores(
    recorder: Any,
    *,
    auction_id: str,
    bids: Iterable[Any],
    assessments: Sequence[FitAssessment],
) -> list[dict[str, Any]]:
    """Log one fit record per bid, against the auction.

    Args:
        recorder: an :class:`~exchange.auction.LedgerRecorder`. Its contract — a sink that is
            down is recorded in ``failures`` rather than propagated — is what keeps the audit
            trail from being able to fail a live auction, and it is relied on here.
        auction_id: the auction these bids belong to. Every event carries it, which is what
            makes the records readable back with ``sink.for_auction(...)``.
        bids: bid mappings carrying ``store_id`` and ``offer.product_ref``, and optionally
            ``bid_ref``. A bid with no ``bid_ref`` gets ``"{auction_id}:{store_id}"``, which
            is stable and unique within the auction.
        assessments: what :meth:`~exchange.retrieval.service.CandidateRetrieval.retrieve`
            produced. Matched to bids by product, never by position.

    Returns:
        The events, in bid order.

    Raises:
        FitLogError: a bid carries no ``store_id`` or no ``offer.product_ref``, or the
            assembled payload does not satisfy the frozen shape for ``bid_placed``. Both are
            producer bugs, caught here rather than at the trust service's door.
    """
    by_product = {assessment.product_id: assessment for assessment in assessments}
    events: list[dict[str, Any]] = []

    for raw in bids:
        bid = _bid_fields(raw)
        store_id = str(bid.get("store_id") or "")
        if not store_id:
            raise FitLogError(f"bid carries no store_id, so its fit is unattributable: {bid!r}")
        offer = bid.get("offer")
        if not isinstance(offer, Mapping):
            offer = {} if offer is None else dict(_bid_fields(offer))
        product_ref = str(offer.get("product_ref") or "")
        if not product_ref:
            raise FitLogError(
                f"bid {store_id!r} carries no offer.product_ref, so no assessment can be "
                f"joined to it"
            )
        bid_ref = str(bid.get("bid_ref") or f"{auction_id}:{store_id}")

        payload: dict[str, Any] = {
            "bid_ref": bid_ref,
            "store_id": store_id,
            "offer": dict(offer),
            "product_ref": product_ref,
        }
        assessment = by_product.get(product_ref)
        if assessment is None:
            payload["fit_score"] = None
            payload["fit_features"] = None
            payload["reranker"] = None
            payload["fit_unavailable"] = (
                f"no candidate assessment for product_ref {product_ref!r}: it was not "
                f"retrieved, or it did not satisfy the intent's hard constraints"
            )
        else:
            payload["fit_score"] = assessment.fit_score
            payload["fit_features"] = assessment.features.as_payload()
            payload["reranker"] = assessment.reranker

        problems = validate_ledger_payload(FIT_LEDGER_KIND, payload)
        if problems:
            raise FitLogError(
                f"fit record for bid {bid_ref!r} does not satisfy the frozen {FIT_LEDGER_KIND!r} "
                f"payload shape: {problems}"
            )
        events.append(
            recorder.record(
                FIT_LEDGER_KIND, auction_id=auction_id, store_id=store_id, payload=payload
            )
        )
    return events
