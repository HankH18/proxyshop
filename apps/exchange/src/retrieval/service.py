"""Candidate retrieval end to end: fetch → R19 filter → relevance → features → rerank (T-031).

The pipeline, and why it is in this order:

1. **Translate** the intent into a :class:`~exchange.retrieval.criteria.RetrievalQuery`.
   Validation happens once, here, so a malformed intent fails before any candidate has been
   judged against it.
2. **Fetch** from the source, with headroom when the query carries a criterion no source can
   pre-filter on.
3. **Filter** — every hard constraint re-decided locally, against whatever came back. This is
   acceptance 1, and it is what makes the guarantee independent of the source.
4. **Judge relevance** — is each candidate *about* what was asked
   (:mod:`~exchange.retrieval.relevance`). A vector index always answers with its top ``k``,
   so "top ``k`` of a good match" and "top ``k`` of nothing relevant" arrive here in the same
   shape; this is the step that tells them apart. Measured on the served route before it
   existed: ``"a walnut coffee table for the lounge"`` returned four supplements, confidently.
   It judges TWO surfaces, weighted: the candidate's own crawled identity
   (:func:`~exchange.retrieval.relevance.candidate_surface`), and beside it the variant names
   the platform observed for it (:func:`~exchange.retrieval.relevance.variant_surface`), which
   may add to a match and may not make one. Off-topic candidates are reported on
   :attr:`RetrievalResult.off_topic` with the reason, never silently dropped — an empty answer
   whose emptiness cannot be explained is the same defect wearing a shorter list.
5. **Measure** the features, over the *relevant* set. Preference alignment is min-max
   normalised across the candidates that survived both filters, so neither an ineligible nor
   an off-topic outlier can compress the scale everyone else is measured on.
6. **Rerank** behind the port, refusing an answer the interface does not allow.
7. **Order** by fit, tie-broken by ``product_id``, and truncate to the caller's limit.
   Truncation is last: a limit applied before the filter would return fewer satisfying
   candidates than were asked for whenever anything was excluded.

The latency budget (acceptance 3) is measured across the whole of that, ``perf_counter`` to
``perf_counter``, and reported on the result rather than asserted inside it. Retrieval
declaring its own timing failure would turn a slow graph into a failed auction; the number is
published so a gate, a metric or a test can decide what to do about it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ingest.graph import Candidate
from ingest.graph.model import canonical_text

from .criteria import NEUTRAL_ALIGNMENT, RetrievalQuery, SoftPreference, build_query
from .fit import FitAssessment, FitFeatures
from .relevance import TopicalRelevance, candidate_surface, variant_surface
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
    #: How many candidates reached the reranker: they satisfied every hard constraint **and**
    #: were about the query. It is deliberately not "how many passed R19" — the number this
    #: reports is the size of the set the features were measured over, which is the set that
    #: was scored, and a count that included off-topic rows would not describe any set the
    #: pipeline ever held. ``considered - eligible_count`` is therefore
    #: ``len(excluded) + len(off_topic)``, and those two are published separately below.
    eligible_count: int
    elapsed_ms: float
    budget_ms: float
    source: str
    reranker: str
    #: The candidates that satisfied every hard constraint and were still not ABOUT the query
    #: (:mod:`~exchange.retrieval.relevance`), each with the verdict's own sentence. Separate
    #: from :attr:`excluded` and not merged into it, because the two say different things to
    #: the shopper reading an empty answer: ``excluded`` is "this product does not meet a
    #: must-have you stated", ``off_topic`` is "this catalogue has nothing about what you
    #: asked". Rolling them together would report a corpus with nothing to offer as a corpus
    #: full of near misses.
    off_topic: tuple[ExcludedCandidate, ...] = ()
    #: The relevance rule that produced :attr:`off_topic`, by name, so an audit can say WHICH
    #: rule refused rather than only that something did.
    relevance: str = ""

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
                excluded, or it was not about the query, or it fell outside the limit.
                Returning 0.0 would be indistinguishable from "measured, and a terrible match".
        """
        for assessment in self.assessments:
            if assessment.product_id == product_id:
                return assessment.fit_score
        raise KeyError(
            f"{product_id!r} is not in this result; retrieved={self.product_ids}, "
            f"excluded={tuple(row.product_id for row in self.excluded)}, "
            f"off_topic={tuple(row.product_id for row in self.off_topic)}"
        )


class CandidateRetrieval:
    """Retrieve candidates for an intent and score their fit.

    **Not thread-safe when the source is not.** :class:`GraphCandidateSource` holds a
    ``neo4j.Session``, and a Session is explicitly single-threaded, so one shared
    ``CandidateRetrieval`` serving concurrent requests corrupts it. Build one per request (or
    per session); the object is cheap and holds no state of its own between calls.
    """

    def __init__(
        self,
        source: CandidateSource,
        *,
        reranker: Reranker | None = None,
        relevance: TopicalRelevance | None = None,
        budget_ms: float = RETRIEVAL_LATENCY_BUDGET_MS,
        require_status: str | None = "active",
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
            relevance: the rule deciding whether a candidate is ABOUT the query
                (:mod:`~exchange.retrieval.relevance`). Defaults to
                :class:`~exchange.retrieval.relevance.TopicalRelevance`, and **there is no way
                to switch it off** — the same reason ``require_status`` re-decides status here
                rather than trusting the source. A knob that turns the honesty check off is a
                knob a deployment ends up in the off position on, and the failure it causes
                (confidently wrong results) is silent. To widen or narrow it, pass a rule with
                different thresholds; to see what it refused, read
                :attr:`RetrievalResult.off_topic`.
            budget_ms: the latency budget reported on each result.
            require_status: the product status a candidate must carry, re-decided **here**
                rather than trusted to the source. ``None`` disables the check. T-012 states
                the requirement ("discontinued products do not silently enter a shortlist")
                and ``GraphCandidateSource`` passes it to Cypher, but a filter that only the
                source applies is a filter this module does not have: any other source — a
                cache, a lenient stub, a future adapter — would admit a discontinued product
                and nothing here would notice.
            clock: monotonic clock, injectable so a test can drive it. Deliberately
                ``perf_counter`` and not the wall clock: the ``frozen_clock`` fixture stops
                ``datetime.now()`` but explicitly does **not** stop ``time.monotonic()``, and
                a duration measured on a stopped clock is always zero.
        """
        self.source = source
        self.reranker: Reranker = reranker if reranker is not None else DeterministicReranker()
        self.relevance: TopicalRelevance = (
            relevance if relevance is not None else TopicalRelevance()
        )
        self.budget_ms = float(budget_ms)
        self.require_status = require_status
        self.clock = clock

    def retrieve(self, intent: Any, *, limit: int | None = None) -> RetrievalResult:
        """Retrieve and score candidates for one intent.

        Args:
            intent: an `Intent` mapping or protocol model.
            limit: how many candidates to return; defaults to
                :data:`~exchange.retrieval.criteria.DEFAULT_CANDIDATE_LIMIT`.

        Returns:
            A :class:`RetrievalResult` whose ``assessments`` satisfy **every** hard constraint
            in the intent **and** are about what it asked, ordered by fit score with
            ``product_id`` as the tie-break. A result with no assessments and a non-empty
            ``off_topic`` is the honest empty answer: the catalogue was searched and holds
            nothing on this subject.

        Raises:
            MalformedIntent: the intent cannot be turned into a query.
            RerankerContractError: the reranker answered outside its interface.
        """
        started = self.clock()
        query = build_query(intent, limit=limit)
        fetched = _first_per_product(self.source.fetch(query))

        eligible: list[Candidate] = []
        excluded: list[ExcludedCandidate] = []
        off_topic: list[ExcludedCandidate] = []
        for candidate in fetched:
            reasons = self._exclusions(query, candidate)
            if reasons:
                excluded.append(
                    ExcludedCandidate(candidate.product_id, candidate.canonical_name, reasons)
                )
                continue
            # Relevance is judged only on what survived R19, and only after it. A candidate
            # already refused for a hard constraint would otherwise be reported twice, under
            # two reasons, and the shopper would be told the catalogue is off-topic when what
            # actually happened is that their own must-have excluded it.
            verdict = self.relevance.judge(
                query.query_text,
                candidate_surface(candidate),
                variant_text=variant_surface(candidate),
            )
            if not verdict.about:
                off_topic.append(
                    ExcludedCandidate(
                        candidate.product_id, candidate.canonical_name, (verdict.detail,)
                    )
                )
                continue
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
            off_topic=tuple(off_topic),
            relevance=str(getattr(self.relevance, "name", type(self.relevance).__name__)),
        )

    def _exclusions(self, query: RetrievalQuery, candidate: Candidate) -> tuple[str, ...]:
        """Every reason this candidate is not eligible. Empty means it is.

        Three predicates, all re-decided locally, for one reason: a filter that only the
        *source* applies is not a filter this module has. The hard constraints are R19's;
        category and status are the two structured predicates a source can push down, and
        pushing them down does not make them decided.
        """
        reasons = list(query.exclusion_reasons(candidate.attributes))
        if query.category is not None:
            wanted = canonical_text(query.category)
            if wanted not in {canonical_text(str(name)) for name in candidate.categories}:
                reasons.append(
                    f"category {query.category!r}: the candidate is in "
                    f"{sorted(candidate.categories)}, which does not include it"
                )
        if self.require_status is not None and candidate.status != self.require_status:
            reasons.append(
                f"status {candidate.status!r}: only {self.require_status!r} products may enter "
                f"a shortlist, so a discontinued one is excluded here and not merely unasked-for"
            )
        return tuple(reasons)


def _first_per_product(fetched: Iterable[Candidate]) -> list[Candidate]:
    """Keep the first candidate per ``product_id``, discarding later duplicates.

    A source returning one product twice is a source bug, but the failure it used to cause
    here was worse than the bug: ``RetrievalResult.fit_for`` scans and returns the **first**
    match while the ledger's product→assessment map keeps the **last**, so the same auction
    would report one fit score to the ranker and log a different one for audit. Collapsing on
    arrival makes the two disagreements impossible rather than merely unlikely.
    """
    seen: set[str] = set()
    unique: list[Candidate] = []
    for candidate in fetched:
        if candidate.product_id in seen:
            continue
        seen.add(candidate.product_id)
        unique.append(candidate)
    return unique


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
    * a candidate missing a **numeric** preference's attribute scores neutral too, never 0.0:
      the preference asks "how much", the catalog does not say, and scoring the gap worst
      would let a ``maximize`` act as a hard filter over missing extraction rather than over
      any fact about the product — which R19 reserves for hard constraints.

    ``prefer`` is the deliberate exception, and the asymmetry is the point: it asks "does this
    product have the attribute at all", so absence is a genuine, observed answer (0.0) rather
    than a gap in what was measured. Scoring it neutral would make ``prefer`` unable to prefer
    anything.
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
