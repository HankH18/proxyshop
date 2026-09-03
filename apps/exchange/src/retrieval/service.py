"""Candidate retrieval end to end: fetch → R19 filter → features → rerank (T-031).

The pipeline, and why it is in this order:

1. **Translate** the intent into a :class:`~exchange.retrieval.criteria.RetrievalQuery`.
   Validation happens once, here, so a malformed intent fails before any candidate has been
   judged against it.
2. **Fetch** from the source, with headroom when the query carries a criterion no source can
   pre-filter on.
3. **Filter** — every hard constraint re-decided locally, against whatever came back. This is
   acceptance 1, and it is what makes the guarantee independent of the source.
4. **Measure** the features, over the *eligible* set. Preference alignment is min-max
   normalised across the candidates that survived the filter, so an ineligible outlier cannot
   compress the scale everyone else is measured on.
5. **Rerank** behind the port, refusing an answer the interface does not allow.
6. **Order** by fit, tie-broken by ``product_id``, and truncate to the caller's limit.
   Truncation is last: a limit applied before the filter would return fewer satisfying
   candidates than were asked for whenever anything was excluded.

The latency budget (acceptance 3) is measured across the whole of that, ``perf_counter`` to
``perf_counter``, and reported on the result rather than asserted inside it. Retrieval
declaring its own timing failure would turn a slow graph into a failed auction; the number is
published so a gate, a metric or a test can decide what to do about it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ingest.graph import Candidate

from .criteria import NEUTRAL_ALIGNMENT, RetrievalQuery, SoftPreference, build_query
from .fit import FitAssessment, FitFeatures
from .rerank import DeterministicReranker, Reranker, RerankItem, read_rerank
from .sources import CandidateSource

__all__ = [
    "RETRIEVAL_LATENCY_BUDGET_MS",
    "CandidateRetrieval",
    "ExcludedCandidate",
    "RetrievalResult",
]

#: The wall-clock budget for one retrieval, in milliseconds. Sized against the fixture load
#: (a few hundred candidates through the filter, the feature pass and the deterministic
#: reranker) with headroom for a shared CI machine — it is a **regression** guard, not a
#: performance target: an accidental per-candidate rescan of the whole set, or a per-candidate
#: embed, blows straight through it while a merely busy host does not.
RETRIEVAL_LATENCY_BUDGET_MS = 250.0


@dataclass(frozen=True)
class ExcludedCandidate:
    """A candidate the hard filter refused, and every reason it refused it."""

    product_id: str
    canonical_name: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalResult:
    """One retrieval: what survived, what did not, and what it cost."""

    intent_id: str
    assessments: tuple[FitAssessment, ...]
    excluded: tuple[ExcludedCandidate, ...]
    considered: int
    eligible_count: int
    elapsed_ms: float
    budget_ms: float
    source: str
    reranker: str

    @property
    def product_ids(self) -> tuple[str, ...]:
        """The returned candidates' ids, best fit first."""
        return tuple(assessment.product_id for assessment in self.assessments)

    @property
    def within_budget(self) -> bool:
        """Whether this retrieval met :data:`RETRIEVAL_LATENCY_BUDGET_MS`."""
        return self.elapsed_ms <= self.budget_ms

    def fit_for(self, product_id: str) -> float:
        """The fit score of one returned candidate.

        Raises:
            KeyError: that product is not in this result — it was never retrieved, or it was
                excluded, or it fell outside the limit. Returning 0.0 would be
                indistinguishable from "measured, and a terrible match".
        """
        for assessment in self.assessments:
            if assessment.product_id == product_id:
                return assessment.fit_score
        raise KeyError(
            f"{product_id!r} is not in this result; retrieved={self.product_ids}, "
            f"excluded={tuple(row.product_id for row in self.excluded)}"
        )


class CandidateRetrieval:
    """Retrieve candidates for an intent and score their fit."""

    def __init__(
        self,
        source: CandidateSource,
        *,
        reranker: Reranker | None = None,
        budget_ms: float = RETRIEVAL_LATENCY_BUDGET_MS,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """
        Args:
            source: where candidates come from —
                :class:`~exchange.retrieval.sources.GraphCandidateSource` in the service,
                :class:`~exchange.retrieval.sources.InMemoryCandidateSource` offline.
            reranker: the fit scorer behind the port. Defaults to
                :class:`~exchange.retrieval.rerank.DeterministicReranker`, which is what A2
                requires every offline verify to run.
            budget_ms: the latency budget reported on each result.
            clock: monotonic clock, injectable so a test can drive it. Deliberately
                ``perf_counter`` and not the wall clock: the ``frozen_clock`` fixture stops
                ``datetime.now()`` but explicitly does **not** stop ``time.monotonic()``, and
                a duration measured on a stopped clock is always zero.
        """
        self.source = source
        self.reranker: Reranker = reranker if reranker is not None else DeterministicReranker()
        self.budget_ms = float(budget_ms)
        self.clock = clock

    def retrieve(self, intent: Any, *, limit: int | None = None) -> RetrievalResult:
        """Retrieve and score candidates for one intent.

        Args:
            intent: an `Intent` mapping or protocol model.
            limit: how many candidates to return; defaults to
                :data:`~exchange.retrieval.criteria.DEFAULT_CANDIDATE_LIMIT`.

        Returns:
            A :class:`RetrievalResult` whose ``assessments`` satisfy **every** hard constraint
            in the intent, ordered by fit score with ``product_id`` as the tie-break.

        Raises:
            MalformedIntent: the intent cannot be turned into a query.
            RerankerContractError: the reranker answered outside its interface.
        """
        started = self.clock()
        query = build_query(intent, limit=limit)
        fetched = list(self.source.fetch(query))

        eligible: list[Candidate] = []
        excluded: list[ExcludedCandidate] = []
        for candidate in fetched:
            reasons = query.exclusion_reasons(candidate.attributes)
            if reasons:
                excluded.append(
                    ExcludedCandidate(candidate.product_id, candidate.canonical_name, reasons)
                )
            else:
                eligible.append(candidate)

        items = _rerank_items(query, eligible)
        scores = read_rerank(self.reranker, query.query_text, items)
        reranker_name = str(getattr(self.reranker, "name", type(self.reranker).__name__))

        assessments = [
            FitAssessment(
                product_id=item.product_id,
                canonical_name=item.canonical_name,
                fit_score=score,
                features=FitFeatures(
                    similarity=item.similarity,
                    preference_alignment=item.preference_alignment,
                ),
                reranker=reranker_name,
            )
            for item, score in zip(items, scores, strict=True)
        ]
        assessments.sort(key=lambda assessment: (-assessment.fit_score, assessment.product_id))
        elapsed_ms = (self.clock() - started) * 1000.0

        return RetrievalResult(
            intent_id=query.intent_id,
            assessments=tuple(assessments[: query.limit]),
            excluded=tuple(excluded),
            considered=len(fetched),
            eligible_count=len(eligible),
            elapsed_ms=elapsed_ms,
            budget_ms=self.budget_ms,
            source=str(getattr(self.source, "name", type(self.source).__name__)),
            reranker=reranker_name,
        )


def _rerank_items(query: RetrievalQuery, eligible: Sequence[Candidate]) -> list[RerankItem]:
    """Reduce eligible candidates to the features the reranker is allowed to see."""
    alignments = _preference_alignments(query.preferences, eligible)
    return [
        RerankItem(
            product_id=candidate.product_id,
            canonical_name=candidate.canonical_name,
            similarity=_similarity(candidate),
            preference_alignment=alignments[index],
        )
        for index, candidate in enumerate(eligible)
    ]


def _similarity(candidate: Candidate) -> float | None:
    """The retrieval similarity in ``[0, 1]``, or ``None`` when none was measured.

    Read through :attr:`ingest.graph.Candidate.cosine` rather than off ``score`` directly:
    that accessor returns ``None`` on the structured path, where ``score`` holds a ``0.0``
    sentinel that is indistinguishable from a genuinely antipodal vector match.
    """
    cosine = candidate.cosine
    if cosine is None:
        return None
    return min(max((1.0 + cosine) / 2.0, 0.0), 1.0)


def _preference_alignments(
    preferences: Sequence[SoftPreference], eligible: Sequence[Candidate]
) -> list[float]:
    """Score ``Intent.preferences`` for every eligible candidate, in input order.

    Numeric preferences are min-max normalised **across the eligible set**: "the best
    caffeine content available for this intent" is the only meaning a maximise preference can
    have without an externally published scale, and no such scale exists (the published
    normalisation bounds belong to the rank formula's features, not to arbitrary catalog
    attributes). Two consequences, both deliberate:

    * a degenerate range — every candidate carrying the same value, or one candidate — scores
      :data:`~exchange.retrieval.criteria.NEUTRAL_ALIGNMENT`, because there is nothing to
      discriminate on;
    * a candidate missing the attribute scores neutral too, never 0.0: absence of evidence is
      not evidence of a bad match, and scoring it worst would let a preference act as a hard
      filter, which R19 reserves for hard constraints.
    """
    count = len(eligible)
    if count == 0:
        return []
    total_weight = sum(preference.weight for preference in preferences)
    if not preferences or total_weight <= 0.0:
        return [NEUTRAL_ALIGNMENT] * count

    weighted = [0.0] * count
    for preference in preferences:
        if preference.weight == 0.0:
            continue
        scores = _preference_scores(preference, eligible)
        for index, score in enumerate(scores):
            weighted[index] += preference.weight * score
    return [value / total_weight for value in weighted]


def _preference_scores(preference: SoftPreference, eligible: Sequence[Candidate]) -> list[float]:
    if not preference.numeric:
        return [1.0 if preference.present(candidate.attributes) else 0.0 for candidate in eligible]

    magnitudes: list[float | None] = [
        preference.magnitude(candidate.attributes) for candidate in eligible
    ]
    observed = [value for value in magnitudes if value is not None]
    if not observed:
        return [NEUTRAL_ALIGNMENT] * len(eligible)
    low, high = min(observed), max(observed)
    if high == low:
        return [NEUTRAL_ALIGNMENT] * len(eligible)

    span = high - low
    scores: list[float] = []
    for value in magnitudes:
        if value is None:
            scores.append(NEUTRAL_ALIGNMENT)
            continue
        position = (value - low) / span
        scores.append(position if preference.direction == "maximize" else 1.0 - position)
    return scores
