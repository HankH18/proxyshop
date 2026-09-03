"""The reranker port, its deterministic double, and the contract read that polices it.

A2 is the constraint this module exists to satisfy: "LLM fit scoring is nondeterministic,
conflicting with replayable/seed-fixed assertions → deterministic test doubles for reranker
and embeddings in all verifies". So the reranker is a **port**, an LLM implementation plugs
in behind it, and :class:`DeterministicReranker` is what every offline verify runs.

The port takes *measured features*, not catalog rows. A reranker handed raw products would
be free to read a store's tier, its fee, or anything else that happened to be on the node —
and R11 requires ``intent_match`` to be fee-blind and tier-blind. :class:`RerankItem` carries
a similarity and a preference alignment and nothing else, so blindness is a property of the
interface rather than of each implementation's good behaviour.

**A reranker that breaks its contract is refused, never degraded.** :func:`read_rerank`
raises :class:`RerankerContractError` on a wrong-length answer, an out-of-range or non-finite
score, a raising implementation, or an implementation declaring another interface version.
The tempting alternative — fall back to "similarity only" — silently substitutes a different
ranking function for the published one, with no record that it happened; the auction would
still produce a shortlist, and nobody could tell which formula ranked it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "DETERMINISTIC_RERANKER_SIMILARITY_SHARE",
    "NEUTRAL_SIMILARITY",
    "RERANKER_INTERFACE_VERSION",
    "DeterministicReranker",
    "RerankItem",
    "Reranker",
    "RerankerContractError",
    "read_rerank",
]

#: The interface version this exchange speaks. A reranker declaring anything else is refused
#: — the meaning of the score is part of the interface, and consulting a source whose
#: vocabulary you do not know is indistinguishable from not consulting one.
RERANKER_INTERFACE_VERSION = "reranker/1.0.0"

#: What similarity contributes when none was measured — the structured retrieval path, where
#: :attr:`ingest.graph.Candidate.scored` is ``False``. 0.5, never 0.0: that library's own
#: docstring records that ``0.0`` is *also* a real vector score (an antipodal query), so
#: reading the structured sentinel as a similarity ranks every structured-path candidate as
#: maximally dissimilar.
NEUTRAL_SIMILARITY = 0.5

#: How the DOUBLE blends its two features. This is the double's own published constant, and
#: it is **not** a rank-formula weight: the ``RankingWeights`` object in `packages/contracts`
#: (``w_m``, ``w_e``, ``w_t``, ``w_v``, ``w_d``) is T-032's, and the whole output of this
#: module enters that formula as the single ``intent_match`` feature. Two weighting layers,
#: one score, and they never mix (DESIGN §Decisions).
DETERMINISTIC_RERANKER_SIMILARITY_SHARE = 0.6


class RerankerContractError(RuntimeError):
    """A reranker answered in a way its interface does not allow."""


@dataclass(frozen=True)
class RerankItem:
    """One candidate as the reranker sees it: measured features, and nothing commercial.

    Attributes:
        product_id: the candidate's stable id.
        canonical_name: the product name, for a text-conditioned implementation.
        similarity: retrieval similarity rescaled to ``[0, 1]``, or ``None`` when the
            candidate came off the structured path and none was measured.
        preference_alignment: how well the candidate matches ``Intent.preferences``, in
            ``[0, 1]``, normalised across the eligible set.
    """

    product_id: str
    canonical_name: str
    similarity: float | None
    preference_alignment: float

    @property
    def effective_similarity(self) -> float:
        """:attr:`similarity`, or :data:`NEUTRAL_SIMILARITY` when nothing was measured."""
        return NEUTRAL_SIMILARITY if self.similarity is None else self.similarity


class Reranker:
    """The port. Implementations answer with one score in ``[0, 1]`` per item, in order."""

    #: A plain class attribute, so an implementation may set it on the class or per instance.
    interface_version: str = RERANKER_INTERFACE_VERSION

    #: The name recorded on every assessment and every ledger payload, so an audit can say
    #: which reranker produced a fit score.
    name: str = "reranker"

    def rerank(self, query_text: str, items: Sequence[RerankItem]) -> Sequence[float]:
        """Score every item. Must return exactly ``len(items)`` scores, in ``items`` order."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement rerank(query_text, items) -> Sequence[float]"
        )


class DeterministicReranker(Reranker):
    """The double (A2). A published, closed-form blend of the two measured features.

    ``fit = share * similarity + (1 - share) * preference_alignment``, rounded to six
    decimals. The rounding is not cosmetic: fit scores are compared for equality across runs
    and are used to order a shortlist, and float noise in the sixteenth digit would make a
    tie-break depend on the order rows arrived in.
    """

    name = "deterministic"

    def __init__(self, *, similarity_share: float = DETERMINISTIC_RERANKER_SIMILARITY_SHARE):
        if not 0.0 <= similarity_share <= 1.0:
            raise ValueError(f"similarity_share must be in [0, 1], got {similarity_share}")
        self.similarity_share = float(similarity_share)

    def rerank(self, query_text: str, items: Sequence[RerankItem]) -> Sequence[float]:
        share = self.similarity_share
        return [
            round(share * item.effective_similarity + (1.0 - share) * item.preference_alignment, 6)
            for item in items
        ]


def read_rerank(reranker: Reranker, query_text: str, items: Sequence[RerankItem]) -> list[float]:
    """Consult a reranker and refuse an answer its interface does not permit.

    Args:
        reranker: the implementation to consult.
        query_text: the buyer's query, passed through verbatim.
        items: the candidates, already reduced to features.

    Returns:
        One score per item, in ``items`` order, each in ``[0, 1]``.

    Raises:
        RerankerContractError: the implementation declares another interface version, raises,
            returns the wrong number of scores, or returns a score that is not a finite
            number in ``[0, 1]``.
    """
    declared = getattr(reranker, "interface_version", None)
    if declared != RERANKER_INTERFACE_VERSION:
        raise RerankerContractError(
            f"{type(reranker).__name__} declares interface_version {declared!r}; this exchange "
            f"speaks {RERANKER_INTERFACE_VERSION!r}. A score whose meaning is unknown is not a "
            f"score, so it is refused rather than trusted."
        )
    if not items:
        return []
    try:
        raw = list(reranker.rerank(query_text, items))
    except RerankerContractError:
        raise
    except Exception as exc:
        raise RerankerContractError(
            f"{type(reranker).__name__} failed while reranking {len(items)} candidates: "
            f"{type(exc).__name__}: {exc}. Retrieval refuses rather than falling back to a "
            f"different scoring function nobody chose."
        ) from exc

    if len(raw) != len(items):
        raise RerankerContractError(
            f"{type(reranker).__name__} returned {len(raw)} scores for {len(items)} candidates; "
            f"the scores are positional, so a short or long answer is unattributable"
        )
    scores: list[float] = []
    for item, value in zip(items, raw, strict=True):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RerankerContractError(
                f"{type(reranker).__name__} scored {item.product_id!r} with "
                f"{value!r}, which is not a number"
            )
        number = float(value)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise RerankerContractError(
                f"{type(reranker).__name__} scored {item.product_id!r} {number!r}; a fit score "
                f"must be a finite number in [0, 1] — it is normalised into the published rank "
                f"formula as intent_match, where an out-of-range value silently rescales "
                f"every other component"
            )
        scores.append(number)
    return scores
