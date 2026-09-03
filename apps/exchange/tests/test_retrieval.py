"""T-031 — candidate retrieval, the R19 hard-criteria filter, and deterministic fit scoring.

Ticket verify: ``pytest apps/exchange/tests/test_retrieval.py -q``.

The three acceptance criteria, and what each is actually asserted against here:

1. **Retrieval returns hard-criteria-satisfying candidates only.** Every assertion in the
   first section drives an *adversarial* candidate source — one that ignores the structured
   predicates the query pushed down and hands back the whole catalog. That is deliberate:
   pushing a filter into Cypher is an optimisation, and a test that only ever sees a source
   which honoured it cannot tell a real filter from a decorative one. The R19 decision is
   re-made locally on every candidate, so the same predicate is asserted here and on the
   graph path.
2. **Fit scores deterministic under the double; logged with the auction.** Determinism is
   asserted across two independently constructed services and against a shuffled input
   order, not merely by calling one service twice. The logging assertions check the emitted
   events against the *frozen* `contracts.ledger` payload shape, so a payload this ticket
   invents cannot pass.
3. **Latency budget under fixture load.** The load is the real
   ``fixtures.generator.generate`` catalog, amplified across seeds. The budget flag has its
   own negative control (:func:`test_the_budget_verdict_can_actually_fail`) so a
   ``within_budget`` hardcoded to ``True`` fails.

Everything outside the two ``@pytest.mark.docker`` graph tests runs offline against the
deterministic doubles A2 requires: no Neo4j, no network, no clock sensitivity except the
latency section, which is about wall-clock behaviour and says so. Those two exist because one
class of defect is invisible to any double: a *pushdown* narrower than the rule this module
publishes. The doubles apply no filter at all, so a graph-side predicate that silently
discards a satisfying candidate leaves every offline assertion green — see
:func:`test_a_pushdown_narrower_than_the_rule_would_lose_this_candidate`.
"""

from __future__ import annotations

import dataclasses
import math
import random
from typing import Any

import pytest
from contracts.ledger import validate_ledger_payload
from exchange.auction import InMemoryLedgerSink, LedgerRecorder
from exchange.retrieval import (
    DETERMINISTIC_RERANKER_SIMILARITY_SHARE,
    MAX_CANDIDATE_LIMIT,
    NEUTRAL_ALIGNMENT,
    NEUTRAL_SIMILARITY,
    RERANKER_INTERFACE_VERSION,
    RETRIEVAL_LATENCY_BUDGET_MS,
    CandidateRetrieval,
    DeterministicReranker,
    HardCriterion,
    InMemoryCandidateSource,
    MalformedIntent,
    Reranker,
    RerankerContractError,
    RerankItem,
    UndecidableCriterion,
    annotate_bid_payload,
    build_query,
    intent_match_by_bid,
    make_candidate,
    record_fit_scores,
)

# =====================================================================================
# builders
# =====================================================================================

SCHEMA_VERSION = "1.0.0"
CREATED_AT = "2026-01-01T00:00:00Z"


def intent(
    *,
    query: str = "light roast single origin whole beans",
    constraints: list[dict[str, Any]] | None = None,
    preferences: list[dict[str, Any]] | None = None,
    category: str | None = "coffee",
    intent_id: str = "intent-1",
) -> dict[str, Any]:
    """A protocol-shaped ``Intent`` mapping (DESIGN §Interfaces)."""
    return {
        "intent_id": intent_id,
        "cluster_id": "cluster-1",
        "query": query,
        "category": category,
        "hard_constraints": constraints if constraints is not None else [],
        "preferences": preferences if preferences is not None else [],
        "ship_to": "US",
        "currency": "USD",
        "budget_band": "under-25",
        "created_at": CREATED_AT,
        "schema_version": SCHEMA_VERSION,
    }


def product(
    product_id: str,
    *,
    name: str = "Altura Washed Single Origin",
    attributes: dict[str, Any] | None = None,
    similarity: float | None = 0.5,
    store_id: str | None = None,
    categories: tuple[str, ...] = ("coffee",),
    ingredients: tuple[str, ...] = ("arabica coffee beans",),
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "product_id": product_id,
        "canonical_name": name,
        "attributes": attributes or {},
        "categories": list(categories),
        "ingredients": list(ingredients),
    }
    if similarity is not None:
        record["similarity"] = similarity
    if store_id is not None:
        record["store_id"] = store_id
    return record


class LenientSource(InMemoryCandidateSource):
    """A source that answers every query with its whole catalog, filters be damned.

    This is the adversary acceptance 1 needs. A source that honours the pushed-down
    ``AttributeFilter`` list makes the retrieval layer's own R19 decision unobservable — the
    rows would already be correct — so every hard-criteria assertion in this file goes
    through a source that honours nothing.
    """

    name = "lenient"


class RecordingReranker(Reranker):
    """Records what it was handed and returns a fixed score per product id."""

    name = "recording"

    def __init__(self, scores: dict[str, float], *, default: float = 0.5) -> None:
        self.scores = dict(scores)
        self.default = default
        self.seen: list[tuple[str, tuple[str, ...]]] = []

    def rerank(self, query_text: str, items: Any) -> list[float]:
        items = list(items)
        self.seen.append((query_text, tuple(item.product_id for item in items)))
        return [self.scores.get(item.product_id, self.default) for item in items]


class BrokenReranker(Reranker):
    """A reranker that violates its contract in exactly one configurable way."""

    name = "broken"

    def __init__(self, mode: str, *, version: str = RERANKER_INTERFACE_VERSION) -> None:
        self.mode = mode
        self.interface_version = version

    def rerank(self, query_text: str, items: Any) -> list[float]:
        items = list(items)
        if self.mode == "short":
            return [0.5] * max(len(items) - 1, 0)
        if self.mode == "long":
            return [0.5] * (len(items) + 1)
        if self.mode == "above":
            return [1.5] * len(items)
        if self.mode == "below":
            return [-0.25] * len(items)
        if self.mode == "nan":
            return [math.nan] * len(items)
        if self.mode == "raises":
            raise RuntimeError("reranker backend unreachable")
        return [0.5] * len(items)


def service(records: list[dict[str, Any]], **kwargs: Any) -> CandidateRetrieval:
    return CandidateRetrieval(LenientSource(records), **kwargs)


# =====================================================================================
# Acceptance 1 — hard-criteria-satisfying candidates only (R19)
# =====================================================================================


def test_a_candidate_missing_the_constrained_attribute_is_excluded() -> None:
    """R19: an undecidable hard constraint never counts as satisfied. Fail closed."""
    result = service(
        [
            product("p-has", attributes={"roast_level": "light"}),
            product("p-silent", attributes={"origin": "Ethiopia"}),
        ]
    ).retrieve(intent(constraints=[{"field": "roast_level", "op": "eq", "value": "light"}]))

    assert result.product_ids == ("p-has",)
    excluded = {row.product_id: row.reasons for row in result.excluded}
    assert "p-silent" in excluded
    assert any("roast_level" in reason for reason in excluded["p-silent"])


@pytest.mark.parametrize(
    ("constraint", "satisfying", "violating"),
    [
        (
            {"field": "roast_level", "op": "eq", "value": "light"},
            {"roast_level": "Light"},  # canonicalised, so case/space cannot smuggle a pass
            {"roast_level": "dark"},
        ),
        (
            {"field": "caffeine_mg_per_serving", "op": "lte", "value": 120},
            {"caffeine_mg_per_serving": 95},
            {"caffeine_mg_per_serving": 165},
        ),
        (
            {"field": "caffeine_mg_per_serving", "op": "gte", "value": 120},
            {"caffeine_mg_per_serving": 165},
            {"caffeine_mg_per_serving": 95},
        ),
        (
            {"field": "origin", "op": "in", "value": ["Ethiopia", "Colombia"]},
            {"origin": "Colombia"},
            {"origin": "Brazil"},
        ),
        (
            {"field": "certifications", "op": "contains", "value": "organic"},
            {"certifications": ["fair-trade", "organic"]},
            {"certifications": ["fair-trade"]},
        ),
        (
            {"field": "decaf", "op": "eq", "value": False},
            {"decaf": False},
            {"decaf": True},
        ),
        (
            # The case whose absence let a fail-open numeric `eq` survive every mutation:
            # replacing `_equals`' numeric branch with "any numeric reading satisfies any
            # numeric eq" killed no test, because nothing asserted the DROPPING direction.
            {"field": "caffeine_mg_per_serving", "op": "eq", "value": 120},
            {"caffeine_mg_per_serving": 120},
            {"caffeine_mg_per_serving": 121},
        ),
    ],
    ids=["eq-string", "lte", "gte", "in", "contains", "eq-bool", "eq-number"],
)
def test_every_constraint_op_admits_the_satisfying_and_drops_the_violating(
    constraint: dict[str, Any],
    satisfying: dict[str, Any],
    violating: dict[str, Any],
) -> None:
    """Both directions per op. A filter that admits everything fails the second half."""
    result = service(
        [
            product("p-ok", attributes=satisfying),
            product("p-bad", attributes=violating),
        ]
    ).retrieve(intent(constraints=[constraint]))

    assert result.product_ids == ("p-ok",), f"{constraint} did not decide correctly"


def test_a_unit_mismatch_never_satisfies_a_hard_constraint() -> None:
    """`120 mg` is not `120 g`. An unmatched unit is undecidable, so it fails closed."""
    constraint = {"field": "net_weight", "op": "lte", "value": 300, "unit": "g"}
    result = service(
        [
            product("p-grams", attributes={"net_weight": {"value": 250, "unit": "g"}}),
            product("p-kilos", attributes={"net_weight": {"value": 1, "unit": "kg"}}),
            product("p-unitless", attributes={"net_weight": 250}),
        ]
    ).retrieve(intent(constraints=[constraint]))

    assert result.product_ids == ("p-grams",)
    assert {row.product_id for row in result.excluded} == {"p-kilos", "p-unitless"}


def test_all_constraints_must_hold_not_merely_one() -> None:
    result = service(
        [
            product("p-both", attributes={"roast_level": "light", "origin": "Ethiopia"}),
            product("p-half", attributes={"roast_level": "light", "origin": "Brazil"}),
        ]
    ).retrieve(
        intent(
            constraints=[
                {"field": "roast_level", "op": "eq", "value": "light"},
                {"field": "origin", "op": "eq", "value": "Ethiopia"},
            ]
        )
    )

    assert result.product_ids == ("p-both",)


def test_every_excluded_candidate_carries_a_reason_naming_its_constraint() -> None:
    result = service(
        [product("p-bad", attributes={"roast_level": "dark", "origin": "Brazil"})]
    ).retrieve(
        intent(
            constraints=[
                {"field": "roast_level", "op": "eq", "value": "light"},
                {"field": "origin", "op": "eq", "value": "Ethiopia"},
            ]
        )
    )

    assert result.product_ids == ()
    (excluded,) = result.excluded
    assert excluded.product_id == "p-bad"
    assert len(excluded.reasons) == 2
    assert any("roast_level" in reason for reason in excluded.reasons)
    assert any("origin" in reason for reason in excluded.reasons)


def test_a_candidate_outside_the_intents_category_is_excluded_locally() -> None:
    """`category` is a structured predicate, and pushing it down does not make it decided.

    Measured before this was fixed: `intent.category='coffee'` against a candidate in
    `['tea']` RETURNED the candidate, because exclusion_reasons() only ever walked the hard
    constraints. Cypher applied the predicate, so the graph path was clean and every offline
    test was clean — the module's headline claim ("a property of THIS module, not of whichever
    source is plugged in") was false for exactly the two predicates a source can push down.
    """
    result = service(
        [
            product("p-coffee", categories=("coffee",)),
            product("p-tea", categories=("tea",)),
            product("p-uncategorised", categories=()),
        ]
    ).retrieve(intent(constraints=[], category="coffee"))

    assert result.product_ids == ("p-coffee",)
    excluded = {row.product_id: row.reasons for row in result.excluded}
    assert set(excluded) == {"p-tea", "p-uncategorised"}
    assert any("category" in reason for reason in excluded["p-tea"])


def test_a_discontinued_product_cannot_enter_a_shortlist() -> None:
    """T-012 states the rule; only GraphCandidateSource enforced it, so it was source-deep.

    Measured before the fix: forcing `GraphCandidateSource(status=None)` left all 46 offline
    tests green — nothing in the exchange observed product status at all.
    """
    records = [
        {**product("p-live"), "status": "active"},
        {**product("p-dead"), "status": "discontinued"},
    ]
    result = service(records).retrieve(intent(constraints=[]))

    assert result.product_ids == ("p-live",)
    (excluded,) = result.excluded
    assert excluded.product_id == "p-dead"
    assert any("discontinued" in reason for reason in excluded.reasons)

    # ...and the check is disableable on purpose, so "no status filter" is a visible decision.
    unfiltered = CandidateRetrieval(LenientSource(records), require_status=None).retrieve(
        intent(constraints=[])
    )
    assert set(unfiltered.product_ids) == {"p-live", "p-dead"}


def test_an_unbounded_limit_is_refused() -> None:
    """The limit is multiplied by two oversamples before it reaches Cypher."""
    from exchange.retrieval import MAX_CANDIDATE_LIMIT

    build_query(intent(), limit=MAX_CANDIDATE_LIMIT)  # the ceiling itself is allowed
    with pytest.raises(MalformedIntent):
        build_query(intent(), limit=MAX_CANDIDATE_LIMIT + 1)
    with pytest.raises(MalformedIntent):
        build_query(intent(), limit=10**9)


def test_a_source_returning_one_product_twice_cannot_disagree_with_itself() -> None:
    """`fit_for` scanned for the FIRST match while the ledger map kept the LAST, so a
    duplicated product reported one score to the ranker and logged another for audit."""
    duplicated = [
        product("p-dup", attributes={"roast_level": "light"}, similarity=0.9),
        product("p-dup", attributes={"roast_level": "light"}, similarity=0.1),
    ]
    result = service(duplicated).retrieve(
        intent(constraints=[{"field": "roast_level", "op": "eq", "value": "light"}])
    )

    assert result.product_ids == ("p-dup",)
    sink = InMemoryLedgerSink()
    record_fit_scores(
        LedgerRecorder(sink),
        auction_id="auction-dup",
        bids=[bid("store-a", "p-dup")],
        assessments=result.assessments,
    )
    (event,) = sink.events
    assert event["payload"]["fit_score"] == pytest.approx(result.fit_for("p-dup"))


def test_pushdown_expresses_what_cypher_can_and_declines_what_it_cannot() -> None:
    """The graph filter is an optimisation; the ops it cannot express must decline, not lie.

    ``AttributeFilter`` has no disjunction and no substring predicate, so ``in`` and
    ``contains`` have no pushdown. Returning a filter that *approximated* them would silently
    drop satisfying rows before the local decision ever ran.
    """
    assert HardCriterion("roast_level", "eq", "light").pushdown() is not None
    assert HardCriterion("spf", "gte", 30).pushdown() is not None
    assert HardCriterion("spf", "lte", 50).pushdown() is not None
    assert HardCriterion("origin", "in", ["Ethiopia"]).pushdown() is None
    assert HardCriterion("certifications", "contains", "organic").pushdown() is None

    numeric = HardCriterion("spf", "gte", 30, unit="index").pushdown()
    assert numeric is not None
    assert numeric.min_number == 30.0
    assert numeric.as_parameter()["key"] == "spf"


def test_numeric_equality_declines_pushdown_because_cypher_is_narrower_than_the_rule() -> None:
    """A pushdown may never exclude a candidate the local decision would admit.

    The Cypher predicate is ``a.value_number = f.equals_number`` — exact float equality —
    while the local rule compares with ``math.isclose``. A reading stored as
    ``0.30000000000000004`` against a constraint of ``0.3`` is dropped by Neo4j and admitted
    here, so pushing numeric ``eq`` down would make the graph path strictly narrower than the
    rule this module publishes, in a way no double can reveal. ``lte``/``gte`` are safe:
    both sides compare the same two floats with the same operator.
    """
    assert HardCriterion("net_weight", "eq", 0.3).pushdown() is None
    assert HardCriterion("grams", "eq", 250).pushdown() is None
    # ...and the local rule is the tolerant one, which is what makes the decline necessary.
    noisy = [{"key": "net_weight", "value_number": 0.1 + 0.2, "value_string": None, "unit": None}]
    assert HardCriterion("net_weight", "eq", 0.3).decide(noisy).satisfied is True
    assert (0.1 + 0.2) != 0.3  # the exact comparison Cypher would have made

    query = build_query(
        intent(constraints=[{"field": "net_weight", "op": "eq", "value": 0.3}], category=None)
    )
    assert query.attribute_filters == ()
    assert tuple(c.field for c in query.local_only_criteria) == ("net_weight",)


def test_the_query_pushes_the_expressible_filters_at_the_source() -> None:
    source = LenientSource([product("p-1", attributes={"roast_level": "light"})])
    CandidateRetrieval(source).retrieve(
        intent(
            constraints=[
                {"field": "roast_level", "op": "eq", "value": "light"},
                {"field": "origin", "op": "in", "value": ["Ethiopia"]},
            ]
        )
    )

    (query,) = source.queries
    assert len(query.attribute_filters) == 1
    assert query.attribute_filters[0].as_parameter()["key"] == "roast-level"
    assert tuple(c.field for c in query.local_only_criteria) == ("origin",)
    assert query.category == "coffee"


def test_an_undecidable_constraint_is_refused_rather_than_quietly_passing() -> None:
    with pytest.raises(UndecidableCriterion):
        HardCriterion("roast_level", "matches", "^light$")
    with pytest.raises(UndecidableCriterion):
        HardCriterion("spf", "gte", "thirty")
    with pytest.raises(UndecidableCriterion):
        HardCriterion("origin", "in", "Ethiopia")  # a bare string is not an option set
    with pytest.raises(MalformedIntent):
        build_query(intent(query="", constraints=[], category=None))
    with pytest.raises(MalformedIntent):
        build_query(intent(preferences=[{"field": "x", "direction": "sideways", "weight": 1}]))
    with pytest.raises(MalformedIntent):
        build_query(intent(preferences=[{"field": "x", "direction": "maximize", "weight": -1}]))


def test_the_limit_is_applied_after_the_filter_not_before() -> None:
    """A shortlist of 2 out of 40 rows where only 3 satisfy must still return 2 satisfying."""
    records = [
        product(f"p-{i:02d}", attributes={"roast_level": "light" if i % 13 == 0 else "dark"})
        for i in range(40)
    ]
    result = service(records).retrieve(
        intent(constraints=[{"field": "roast_level", "op": "eq", "value": "light"}]), limit=2
    )

    assert len(result.assessments) == 2
    assert result.considered == 40
    assert result.eligible_count == 4  # i = 0, 13, 26, 39


# =====================================================================================
# Acceptance 2 — fit scores deterministic under the double, logged with the auction
# =====================================================================================

FIT_INTENT = intent(
    constraints=[{"field": "roast_level", "op": "eq", "value": "light"}],
    preferences=[
        {"field": "caffeine_mg_per_serving", "direction": "maximize", "weight": 0.6},
        {"field": "certifications", "direction": "prefer", "weight": 0.4},
    ],
)

FIT_RECORDS = [
    product(
        "p-a",
        name="Altura Washed Single Origin",
        attributes={
            "roast_level": "light",
            "caffeine_mg_per_serving": 160,
            "certifications": ["organic"],
        },
        similarity=0.42,
        store_id="store-northroast",
    ),
    product(
        "p-b",
        name="Harbour House Espresso Blend",
        attributes={"roast_level": "light", "caffeine_mg_per_serving": 100},
        similarity=0.88,
        store_id="store-slowreply",
    ),
    product(
        "p-c",
        name="Cold Brew Coarse Grind",
        attributes={"roast_level": "dark", "caffeine_mg_per_serving": 130},
        similarity=0.99,
        store_id="store-brightbean",
    ),
]


def test_the_five_published_constants_are_pinned_to_their_literal_values() -> None:
    """Every expectation below is hand-written, not imported.

    A test that writes its expectations *from* the constant it is checking pins nothing: the
    five values here could each be changed to anything and the rest of this file stays green
    — measured, all five. ``RETRIEVAL_LATENCY_BUDGET_MS`` is the worst of them, because
    widening it 360x silently retires acceptance criterion 3.
    """
    assert DETERMINISTIC_RERANKER_SIMILARITY_SHARE == 0.6
    assert NEUTRAL_SIMILARITY == 0.5
    assert NEUTRAL_ALIGNMENT == 0.5
    assert RERANKER_INTERFACE_VERSION == "reranker/1.0.0"
    assert RETRIEVAL_LATENCY_BUDGET_MS == 250.0
    assert MAX_CANDIDATE_LIMIT == 500


def test_the_deterministic_double_computes_the_blend_it_publishes() -> None:
    """Hand-written arithmetic against share=0.6, so the share itself is what is asserted."""
    items = [
        RerankItem("p-1", "one", similarity=1.0, preference_alignment=0.0),
        RerankItem("p-2", "two", similarity=0.0, preference_alignment=1.0),
        RerankItem("p-3", "three", similarity=None, preference_alignment=1.0),
        RerankItem("p-4", "four", similarity=0.25, preference_alignment=0.75),
    ]
    scores = list(DeterministicReranker().rerank("anything", items))

    assert scores[0] == pytest.approx(0.6)  # 0.6*1.0 + 0.4*0.0
    assert scores[1] == pytest.approx(0.4)  # 0.6*0.0 + 0.4*1.0
    assert scores[2] == pytest.approx(0.7)  # 0.6*0.5 (neutral) + 0.4*1.0
    assert scores[3] == pytest.approx(0.45)  # 0.6*0.25 + 0.4*0.75
    assert DeterministicReranker(similarity_share=0.5).rerank(
        "anything", [items[0]]
    ) == pytest.approx([0.5])


def test_a_pinned_similarity_is_rescaled_the_way_the_cosine_index_reports_it() -> None:
    """Neo4j rescales cosine into [0, 1] as (1 + cos)/2. A double that skipped the rescale
    would let a test pin a number the graph never produces."""
    antipodal = make_candidate({"product_id": "p", "canonical_name": "n", "similarity": -1.0})
    assert antipodal.score == pytest.approx(0.0)
    assert antipodal.cosine == pytest.approx(-1.0)
    assert antipodal.scored is True

    structured = make_candidate({"product_id": "p", "canonical_name": "n"}, scored=False)
    assert structured.cosine is None
    assert structured.score == 0.0


def test_the_hash_embedding_fallback_similarity_is_deterministic_and_query_sensitive() -> None:
    """A2/D18: embeddings in every verify come from the deterministic double."""
    record = {"product_id": "p", "canonical_name": "Altura Washed Single Origin"}
    first = make_candidate(record, query_text="light roast single origin whole beans")
    second = make_candidate(record, query_text="light roast single origin whole beans")
    unrelated = make_candidate(record, query_text="espresso machine descaling powder")

    assert first.score == second.score
    assert first.score != unrelated.score
    assert 0.0 <= first.score <= 1.0


def test_fit_scores_are_identical_across_two_independently_built_services() -> None:
    first = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    second = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)

    assert [(a.product_id, a.fit_score) for a in first.assessments] == [
        (a.product_id, a.fit_score) for a in second.assessments
    ]
    assert first.product_ids == ("p-a", "p-b")  # p-c fails the hard constraint


def test_fit_does_not_depend_on_the_order_the_source_returned_rows_in() -> None:
    baseline = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    shuffled = list(FIT_RECORDS)
    random.Random(31).shuffle(shuffled)
    reordered = service(shuffled).retrieve(FIT_INTENT)

    assert [(a.product_id, a.fit_score) for a in baseline.assessments] == [
        (a.product_id, a.fit_score) for a in reordered.assessments
    ]


def test_every_fit_score_lands_in_the_unit_interval() -> None:
    result = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    assert result.assessments
    for assessment in result.assessments:
        assert 0.0 <= assessment.fit_score <= 1.0


def test_assessments_are_ordered_by_fit_with_a_stable_tie_break() -> None:
    twins = [
        product("p-zz", attributes={"roast_level": "light"}, similarity=0.5),
        product("p-aa", attributes={"roast_level": "light"}, similarity=0.5),
    ]
    result = service(twins).retrieve(
        intent(constraints=[{"field": "roast_level", "op": "eq", "value": "light"}])
    )

    assert result.product_ids == ("p-aa", "p-zz")
    assert result.assessments[0].fit_score == result.assessments[1].fit_score


def test_an_unscored_candidate_uses_the_neutral_similarity_not_the_zero_sentinel() -> None:
    """The structured retrieval path reports ``score=0.0`` and ``scored=False``.

    ``ingest.graph.Candidate`` documents the trap in its own docstring: ``0.0`` is also a
    perfectly real vector score (an antipodal query). Reading the sentinel as a similarity
    would rank every structured-path candidate as maximally dissimilar. The neutral value is
    used instead, and the feature record says ``None`` rather than a number nobody measured.
    """
    structured = InMemoryCandidateSource(
        [
            product(
                "p-structured",
                attributes={"roast_level": "light", "certs": ["organic"]},
                similarity=None,
            )
        ],
        scored=False,
    )
    result = CandidateRetrieval(structured).retrieve(
        intent(
            constraints=[{"field": "roast_level", "op": "eq", "value": "light"}],
            preferences=[{"field": "certs", "direction": "prefer", "weight": 1.0}],
        )
    )

    (assessment,) = result.assessments
    assert assessment.features.similarity is None
    # Alignment is 1.0 (the `prefer` attribute is present), so the fit is
    # 0.6*NEUTRAL_SIMILARITY + 0.4*1.0 = 0.7. Reading the 0.0 sentinel as a similarity would
    # give 0.6*0.0 + 0.4*1.0 = 0.4. The two neutrals both being 0.5 is what made the previous
    # arithmetic here decorative — it came to 0.5 for every possible share.
    assert assessment.features.preference_alignment == pytest.approx(1.0)
    assert assessment.fit_score == pytest.approx(0.7)
    assert assessment.fit_score != pytest.approx(0.4)


def test_a_preference_moves_fit_in_the_direction_it_declares() -> None:
    records = [
        product("p-strong", attributes={"roast_level": "light", "caffeine": 160}, similarity=0.5),
        product("p-weak", attributes={"roast_level": "light", "caffeine": 90}, similarity=0.5),
    ]
    hard = [{"field": "roast_level", "op": "eq", "value": "light"}]

    maximize = service(list(records)).retrieve(
        intent(
            constraints=hard,
            preferences=[{"field": "caffeine", "direction": "maximize", "weight": 1.0}],
        )
    )
    minimize = service(list(records)).retrieve(
        intent(
            constraints=hard,
            preferences=[{"field": "caffeine", "direction": "minimize", "weight": 1.0}],
        )
    )

    assert maximize.product_ids == ("p-strong", "p-weak")
    assert minimize.product_ids == ("p-weak", "p-strong")
    assert maximize.fit_for("p-strong") > maximize.fit_for("p-weak")
    assert minimize.fit_for("p-weak") > minimize.fit_for("p-strong")


def test_a_candidate_missing_a_preferred_attribute_scores_neutral_not_worst() -> None:
    """A preference is a score term, never a filter (R19).

    Scoring an absent attribute 0.0 makes "prefers high caffeine" silently exclude every
    product whose caffeine content was never extracted — a hard constraint the buyer never
    stated, applied to a gap in the catalog rather than to a fact about the product. Neutral
    keeps absence of evidence from becoming evidence of a bad match.
    """
    records = [
        product("p-high", attributes={"roast_level": "light", "caffeine": 160}, similarity=0.5),
        product("p-low", attributes={"roast_level": "light", "caffeine": 90}, similarity=0.5),
        product("p-absent", attributes={"roast_level": "light"}, similarity=0.5),
    ]
    result = service(records).retrieve(
        intent(
            constraints=[{"field": "roast_level", "op": "eq", "value": "light"}],
            preferences=[{"field": "caffeine", "direction": "maximize", "weight": 1.0}],
        )
    )

    assert result.fit_for("p-low") < result.fit_for("p-absent") < result.fit_for("p-high")
    absent = next(a for a in result.assessments if a.product_id == "p-absent")
    assert absent.features.preference_alignment == pytest.approx(NEUTRAL_ALIGNMENT)


def test_a_preference_nothing_can_discriminate_on_scores_everyone_neutral() -> None:
    """An identical reading across the whole eligible set carries no information."""
    records = [
        product("p-1", attributes={"roast_level": "light", "caffeine": 120}, similarity=0.5),
        product("p-2", attributes={"roast_level": "light", "caffeine": 120}, similarity=0.5),
    ]
    result = service(records).retrieve(
        intent(
            constraints=[{"field": "roast_level", "op": "eq", "value": "light"}],
            preferences=[{"field": "caffeine", "direction": "maximize", "weight": 1.0}],
        )
    )

    for assessment in result.assessments:
        assert assessment.features.preference_alignment == pytest.approx(NEUTRAL_ALIGNMENT)


def test_preference_weights_are_honoured_not_merely_counted() -> None:
    """Two preferences pulling opposite ways: the heavier one decides the order."""
    records = [
        product(
            "p-origin",
            attributes={"roast_level": "light", "caffeine": 90, "origin": "Ethiopia"},
            similarity=0.5,
        ),
        product(
            "p-caffeine",
            attributes={"roast_level": "light", "caffeine": 160},
            similarity=0.5,
        ),
    ]
    hard = [{"field": "roast_level", "op": "eq", "value": "light"}]

    origin_heavy = service(list(records)).retrieve(
        intent(
            constraints=hard,
            preferences=[
                {"field": "origin", "direction": "prefer", "weight": 0.9},
                {"field": "caffeine", "direction": "maximize", "weight": 0.1},
            ],
        )
    )
    caffeine_heavy = service(list(records)).retrieve(
        intent(
            constraints=hard,
            preferences=[
                {"field": "origin", "direction": "prefer", "weight": 0.1},
                {"field": "caffeine", "direction": "maximize", "weight": 0.9},
            ],
        )
    )

    assert origin_heavy.product_ids == ("p-origin", "p-caffeine")
    assert caffeine_heavy.product_ids == ("p-caffeine", "p-origin")


def test_an_ineligible_outlier_cannot_compress_the_preference_scale() -> None:
    """Normalisation runs over the eligible set, so a filtered-out extreme is not a yardstick."""
    eligible_only = [
        product("p-a", attributes={"roast_level": "light", "caffeine": 100}, similarity=0.5),
        product("p-b", attributes={"roast_level": "light", "caffeine": 120}, similarity=0.5),
    ]
    with_outlier = [
        *eligible_only,
        product("p-out", attributes={"roast_level": "dark", "caffeine": 5000}, similarity=0.5),
    ]
    query = intent(
        constraints=[{"field": "roast_level", "op": "eq", "value": "light"}],
        preferences=[{"field": "caffeine", "direction": "maximize", "weight": 1.0}],
    )

    without = service(list(eligible_only)).retrieve(query)
    with_it = service(list(with_outlier)).retrieve(query)

    assert [(a.product_id, a.fit_score) for a in without.assessments] == [
        (a.product_id, a.fit_score) for a in with_it.assessments
    ]


def test_a_prefer_direction_rewards_presence_of_the_attribute() -> None:
    records = [
        product("p-certified", attributes={"roast_level": "light", "certs": ["organic"]}),
        product("p-plain", attributes={"roast_level": "light"}),
    ]
    result = service(records).retrieve(
        intent(
            constraints=[{"field": "roast_level", "op": "eq", "value": "light"}],
            preferences=[{"field": "certs", "direction": "prefer", "weight": 1.0}],
        )
    )

    assert result.fit_for("p-certified") > result.fit_for("p-plain")


def test_the_reranker_is_consulted_behind_the_interface_and_its_answer_is_the_fit() -> None:
    """A2: the reranker is a port. Swapping the double must change the fit it produces."""
    reranker = RecordingReranker({"p-a": 0.9, "p-b": 0.1})
    result = CandidateRetrieval(LenientSource(list(FIT_RECORDS)), reranker=reranker).retrieve(
        FIT_INTENT
    )

    (query_text, product_ids) = reranker.seen[-1]
    assert query_text == FIT_INTENT["query"]
    assert set(product_ids) == {"p-a", "p-b"}  # the ineligible p-c never reaches the reranker
    assert result.fit_for("p-a") == pytest.approx(0.9)
    assert result.fit_for("p-b") == pytest.approx(0.1)
    assert result.product_ids == ("p-a", "p-b")
    assert all(a.reranker == "recording" for a in result.assessments)


def test_the_reranker_receives_the_measured_features_not_raw_catalog_rows() -> None:
    """R11 blindness is structural: the port cannot carry a commercial field.

    The FIELD SET is the assertion. A future edit widening ``RerankItem`` to pass the
    candidate through — or adding ``tier`` "just for the LLM prompt" — fails here, where an
    assertion about values would happily pass a reranker handed the whole product. The
    previous body asserted ``reranker.seen == []`` on an object it never wired to anything,
    which was true by construction.
    """
    captured: list[Any] = []

    class Capturing(RecordingReranker):
        def rerank(self, query_text: str, items: Any) -> list[float]:
            items = list(items)
            captured.extend(items)
            return super().rerank(query_text, items)

    CandidateRetrieval(LenientSource(list(FIT_RECORDS)), reranker=Capturing({})).retrieve(
        FIT_INTENT
    )

    assert {f.name for f in dataclasses.fields(RerankItem)} == {
        "product_id",
        "canonical_name",
        "similarity",
        "preference_alignment",
    }
    assert captured
    for item in captured:
        assert 0.0 <= item.preference_alignment <= 1.0
        assert item.similarity is None or 0.0 <= item.similarity <= 1.0


@pytest.mark.parametrize("mode", ["short", "long", "above", "below", "nan", "raises"])
def test_a_reranker_that_breaks_its_contract_is_refused_never_silently_degraded(
    mode: str,
) -> None:
    """Falling back to "similarity only" would be an unattributable ranking change."""
    with pytest.raises(RerankerContractError):
        CandidateRetrieval(
            LenientSource(list(FIT_RECORDS)), reranker=BrokenReranker(mode)
        ).retrieve(FIT_INTENT)


def test_a_reranker_speaking_another_interface_version_is_refused() -> None:
    with pytest.raises(RerankerContractError):
        CandidateRetrieval(
            LenientSource(list(FIT_RECORDS)),
            reranker=BrokenReranker("ok", version="reranker/0.0.1-experimental"),
        ).retrieve(FIT_INTENT)


def test_fit_is_blind_to_tier_and_network_fee() -> None:
    """R11: ranking inputs are fee-blind and tier-blind. `intent_match` is a ranking input."""
    plain = [
        product("p-a", attributes={"roast_level": "light", "caffeine_mg_per_serving": 160}),
        product("p-b", attributes={"roast_level": "light", "caffeine_mg_per_serving": 100}),
    ]
    loaded = [
        product(
            "p-a",
            attributes={
                "roast_level": "light",
                "caffeine_mg_per_serving": 160,
                "tier": 0,
                "network_fee": 0.01,
                "envelope_floor": 5.0,
            },
        ),
        product(
            "p-b",
            attributes={
                "roast_level": "light",
                "caffeine_mg_per_serving": 100,
                "tier": 2,
                "network_fee": 0.30,
                "envelope_floor": 99.0,
            },
        ),
    ]
    blind_intent = intent(
        constraints=[{"field": "roast_level", "op": "eq", "value": "light"}],
        preferences=[{"field": "caffeine_mg_per_serving", "direction": "maximize", "weight": 1.0}],
    )

    without = service(plain).retrieve(blind_intent)
    with_fees = service(loaded).retrieve(blind_intent)

    assert [(a.product_id, a.fit_score) for a in without.assessments] == [
        (a.product_id, a.fit_score) for a in with_fees.assessments
    ]

    recorder = LedgerRecorder(InMemoryLedgerSink())
    events = record_fit_scores(
        recorder,
        auction_id="auction-blind",
        bids=[bid("store-a", "p-a"), bid("store-b", "p-b")],
        assessments=with_fees.assessments,
    )
    blob = repr(events)
    for banned in ("tier", "network_fee", "envelope_floor"):
        assert banned not in blob


# --- logging --------------------------------------------------------------------------


def bid(store_id: str, product_ref: str, *, bid_ref: str | None = None) -> dict[str, Any]:
    return {
        "bid_ref": bid_ref or f"bid-{store_id}",
        "store_id": store_id,
        "offer": {"product_ref": product_ref, "unit_price": 21.5, "total_price": 21.5},
    }


def test_fit_scores_are_logged_once_per_bid_and_carry_the_auction() -> None:
    result = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    sink = InMemoryLedgerSink()
    recorder = LedgerRecorder(sink)

    record_fit_scores(
        recorder,
        auction_id="auction-7",
        bids=[bid("store-northroast", "p-a"), bid("store-slowreply", "p-b")],
        assessments=result.assessments,
    )

    logged = sink.for_auction("auction-7")
    assert len(logged) == 2
    assert {event["kind"] for event in logged} == {"bid_placed"}
    by_bid = {event["payload"]["bid_ref"]: event["payload"] for event in logged}
    assert by_bid["bid-store-northroast"]["fit_score"] == pytest.approx(result.fit_for("p-a"))
    assert by_bid["bid-store-slowreply"]["fit_score"] == pytest.approx(result.fit_for("p-b"))
    assert by_bid["bid-store-northroast"]["reranker"] == "deterministic"
    features = by_bid["bid-store-northroast"]["fit_features"]
    assert set(features) == {"similarity", "preference_alignment"}
    assert recorder.failures == []


def test_the_logged_payload_satisfies_the_frozen_ledger_shape() -> None:
    """D24 pins ``bid_placed`` to (bid_ref, store_id, offer). A payload this ticket invented
    on its own would fail at the trust service's door instead of here."""
    result = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    sink = InMemoryLedgerSink()
    record_fit_scores(
        LedgerRecorder(sink),
        auction_id="auction-8",
        bids=[bid("store-northroast", "p-a")],
        assessments=result.assessments,
    )

    (event,) = sink.events
    assert validate_ledger_payload(event["kind"], event["payload"]) == []


def test_a_bid_whose_product_was_never_assessed_is_logged_as_unavailable() -> None:
    """Fabricating a fit score for an unassessed product would poison the audit trail."""
    result = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    sink = InMemoryLedgerSink()
    record_fit_scores(
        LedgerRecorder(sink),
        auction_id="auction-9",
        bids=[bid("store-brightbean", "p-c")],  # p-c failed the hard constraint
        assessments=result.assessments,
    )

    (event,) = sink.events
    assert event["payload"]["fit_score"] is None
    assert "p-c" in event["payload"]["fit_unavailable"]


def test_logging_survives_a_sink_that_is_down() -> None:
    """Losing an audit record must never fail a live auction (the LedgerRecorder contract)."""

    class DeadSink:
        def emit(self, event: Any) -> None:
            raise ConnectionError("trust api unreachable")

    result = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    recorder = LedgerRecorder(DeadSink())
    events = record_fit_scores(
        recorder,
        auction_id="auction-10",
        bids=[bid("store-northroast", "p-a")],
        assessments=result.assessments,
    )

    assert len(events) == 1
    assert len(recorder.failures) == 1


def test_annotating_a_bid_payload_emits_nothing_at_all() -> None:
    """The frozen ledger counts are why enrichment is the primary path, not a second event.

    ``bid_placed``'s count is load-bearing twice over: T-082 asserts an exact per-kind
    multiset (``bid_placed == n_stores_solicited``), and T-086 proves shadow mode by the
    ABSENCE of any ``bid_placed`` for the auction. A fit event per bid doubles the first and
    breaks the second — and D24 forbids inventing a nineteenth kind to escape to. So the fit
    rides inside the bid's own event, and this asserts that annotating writes no event.
    """
    result = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    sink = InMemoryLedgerSink()
    LedgerRecorder(sink)  # a live recorder that must stay untouched

    annotated = annotate_bid_payload(
        {"bid_id": "bid-7", "store_id": "store-northroast", "offer": {"product_ref": "p-a"}},
        result.assessments,
    )

    assert sink.events == []
    assert annotated["bid_ref"] == "bid-7"  # an existing bid_id is kept, never overwritten
    assert annotated["fit_score"] == pytest.approx(result.fit_for("p-a"))
    assert validate_ledger_payload("bid_placed", annotated) == []


def test_annotation_neither_mutates_nor_aliases_the_caller_s_bid() -> None:
    result = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    original = {
        "bid_id": "bid-9",
        "store_id": "store-northroast",
        "offer": {"product_ref": "p-a", "discount": {"type": "pct", "value": 10}},
    }
    annotated = annotate_bid_payload(original, result.assessments)

    assert "fit_score" not in original
    annotated["offer"]["discount"]["value"] = 99
    assert original["offer"]["discount"]["value"] == 10


def test_fit_logging_composes_with_the_auction_layers_bid_entries() -> None:
    """``collect_bids`` yields ``BidEntry`` DATACLASSES, not mappings.

    Requiring every caller to unwrap ``entry.bid`` first is an API that reads as though it
    composes and does not — this used to raise ``FitLogError('... got BidEntry')``.
    """
    from exchange.auction import BidEntry

    result = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    entries = [
        BidEntry(
            store_id="store-northroast",
            tier=1,
            fallback=False,
            bid={
                "bid_id": "bid-real",
                "store_id": "store-northroast",
                "offer": {"product_ref": "p-a", "unit_price": 21.5, "total_price": 21.5},
            },
        )
    ]
    sink = InMemoryLedgerSink()
    record_fit_scores(
        LedgerRecorder(sink), auction_id="auction-11", bids=entries, assessments=result.assessments
    )

    (event,) = sink.events
    assert event["payload"]["bid_ref"] == "bid-real"
    assert event["payload"]["fit_score"] == pytest.approx(result.fit_for("p-a"))


def test_intent_match_by_bid_does_the_product_to_bid_join_the_ranker_needs() -> None:
    """T-032 consumes one float per BID named ``intent_match``; this module measures one per
    PRODUCT named ``fit_score``, and ``ShortlistSlot.fit_score`` is a third concept (D29).

    Doing that join by hand at the call site is where the three get conflated, so it lives
    here — keyed on ``bid_id``, which is what the frozen ranking candidate keys on.
    """
    result = service(list(FIT_RECORDS)).retrieve(FIT_INTENT)
    matched = intent_match_by_bid(
        [
            {"bid_id": "bid-a", "store_id": "store-northroast", "offer": {"product_ref": "p-a"}},
            {"bid_id": "bid-b", "store_id": "store-slowreply", "offer": {"product_ref": "p-b"}},
            {"bid_id": "bid-c", "store_id": "store-brightbean", "offer": {"product_ref": "p-c"}},
        ],
        result.assessments,
    )

    assert set(matched) == {"bid-a", "bid-b", "bid-c"}
    assert matched["bid-a"] == pytest.approx(result.fit_for("p-a"))
    assert matched["bid-b"] == pytest.approx(result.fit_for("p-b"))
    # p-c failed the hard constraint. None, never a substituted neutral: there is no published
    # `intent_match_when_absent` in NormalizationBounds for this module to honour, so the
    # ranker must decide what an unmeasured bid means rather than be handed a guess.
    assert matched["bid-c"] is None
    assert all(v is None or 0.0 <= v <= 1.0 for v in matched.values())


# =====================================================================================
# Acceptance 3 — latency budget under fixture load
# =====================================================================================


def fixture_load(target: int = 600) -> list[dict[str, Any]]:
    """The real seeded catalog (``fixtures.generator``), amplified across seeds."""
    from fixtures.generator import generate

    records: list[dict[str, Any]] = []
    seed = 0
    while len(records) < target:
        payload = generate("coffee", seed)
        for index, item in enumerate(payload["catalog"]):
            attributes = dict(item["attributes"])
            attributes["net_weight"] = {"value": item["variants"][0]["grams"], "unit": "g"}
            records.append(
                product(
                    f"{item['product_ref']}-s{seed}",
                    name=item["canonical_name"],
                    attributes=attributes,
                    similarity=((index + seed) % 97) / 96.0 * 2.0 - 1.0,
                    store_id=item["store_id"],
                )
            )
        seed += 1
    return records[:target]


LOAD_INTENT = intent(
    query="light roast single origin whole beans, 250 g",
    constraints=[
        {"field": "roast_level", "op": "eq", "value": "light"},
        {"field": "net_weight", "op": "lte", "value": 300, "unit": "g"},
    ],
    preferences=[
        {"field": "caffeine_mg_per_serving", "direction": "maximize", "weight": 0.6},
        {"field": "certifications", "direction": "prefer", "weight": 0.4},
    ],
)


def test_retrieval_meets_its_latency_budget_under_fixture_load() -> None:
    records = fixture_load()
    result = service(records).retrieve(LOAD_INTENT, limit=4)

    assert result.considered == len(records)
    assert result.eligible_count > 0, "the load fixture must contain satisfying candidates"
    assert len(result.assessments) == 4
    assert result.elapsed_ms <= RETRIEVAL_LATENCY_BUDGET_MS, (
        f"retrieval over {len(records)} candidates took {result.elapsed_ms:.1f} ms, "
        f"budget is {RETRIEVAL_LATENCY_BUDGET_MS} ms"
    )
    assert result.within_budget is True


def test_the_budget_verdict_can_actually_fail() -> None:
    """Negative control: a ``within_budget`` hardcoded to ``True`` dies here."""
    result = service(fixture_load(200), budget_ms=0.0).retrieve(LOAD_INTENT)

    assert result.budget_ms == 0.0
    assert result.elapsed_ms > 0.0
    assert result.within_budget is False


def test_the_hard_filter_still_holds_at_fixture_scale() -> None:
    records = fixture_load()
    result = service(records).retrieve(LOAD_INTENT, limit=MAX_CANDIDATE_LIMIT)

    by_id = {record["product_id"]: record for record in records}
    for assessment in result.assessments:
        attributes = by_id[assessment.product_id]["attributes"]
        assert attributes.get("roast_level") == "light"
        assert attributes["net_weight"]["value"] <= 300
    # The partition identity below is true BY CONSTRUCTION (retrieve() splits `fetched` into
    # exactly these two lists), so on its own it survives a filter that excludes EVERYTHING —
    # measured. The exact count is what pins the filter: both over- and under-admission move
    # it, and a vacuous loop over zero assessments no longer passes.
    expected_eligible = sum(
        1
        for record in records
        if record["attributes"].get("roast_level") == "light"
        and record["attributes"]["net_weight"]["value"] <= 300
    )
    assert expected_eligible == 29
    assert len(result.assessments) == expected_eligible
    assert result.eligible_count == expected_eligible
    assert len(result.assessments) + len(result.excluded) == len(records)


# =====================================================================================
# The Neo4j path — the same predicate, against the real graph (T-012's library)
# =====================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_the_graph_source_retrieves_vector_plus_attribute_candidates(
    neo4j_session: Any,
) -> None:
    """The offline assertions above all drive a double; this one drives Neo4j itself."""
    from exchange.retrieval import GraphCandidateSource
    from ingest.embeddings import HashEmbedding
    from ingest.graph import (
        AttributeValue,
        Source,
        apply_schema,
        reembed_products,
        seed_products,
    )

    apply_schema(neo4j_session)
    source_node = Source(
        source_id="src-t031",
        url="https://northroast.example/catalog",
        content_hash="sha256:t031",
        observed_at=CREATED_AT,
        extractor_version="t031@1",
        confidence=0.9,
        source_class="scraped",
    )
    seed_products(
        neo4j_session,
        [
            {
                "product_id": "t031-light",
                "canonical_name": "Altura Washed Single Origin light roast",
                "category": "coffee",
                "attributes": [
                    AttributeValue(key="roast_level", value_string="light"),
                    AttributeValue(key="caffeine_mg_per_serving", value_number=160, unit="mg"),
                ],
            },
            {
                "product_id": "t031-dark",
                "canonical_name": "Harbour House Espresso Blend dark roast",
                "category": "coffee",
                "attributes": [
                    AttributeValue(key="roast_level", value_string="dark"),
                    AttributeValue(key="caffeine_mg_per_serving", value_number=100, unit="mg"),
                ],
            },
        ],
        source=source_node,
    )
    provider = HashEmbedding()
    reembed_products(neo4j_session, provider)

    graph_source = GraphCandidateSource(neo4j_session, provider=provider)
    result = CandidateRetrieval(graph_source).retrieve(
        intent(
            query="light roast single origin whole beans",
            constraints=[{"field": "roast_level", "op": "eq", "value": "light"}],
            category="coffee",
        )
    )

    assert result.product_ids == ("t031-light",)
    (assessment,) = result.assessments
    assert assessment.features.similarity is not None
    assert 0.0 <= assessment.features.similarity <= 1.0


@pytest.mark.docker
@pytest.mark.graph
def test_a_pushdown_narrower_than_the_rule_would_lose_this_candidate(
    neo4j_session: Any,
) -> None:
    """The regression test for the pushdown invariant, against the real Cypher.

    ``0.1 + 0.2`` is stored, ``0.3`` is asked for. The local rule admits it; Neo4j's
    ``a.value_number = f.equals_number`` does not. Re-enable the numeric-``eq`` pushdown in
    :meth:`HardCriterion.pushdown` and this candidate disappears from the graph path while
    every offline test in this file stays green — which is exactly why the assertion lives
    here, on the real database, rather than against a double.
    """
    from exchange.retrieval import GraphCandidateSource
    from ingest.embeddings import HashEmbedding
    from ingest.graph import (
        AttributeValue,
        Source,
        apply_schema,
        reembed_products,
        seed_products,
    )

    apply_schema(neo4j_session)
    seed_products(
        neo4j_session,
        [
            {
                "product_id": "t031-noisy",
                "canonical_name": "Altura Washed Single Origin decimal weight",
                "category": "coffee",
                "attributes": [AttributeValue(key="net_weight", value_number=0.1 + 0.2, unit="kg")],
            }
        ],
        source=Source(
            source_id="src-t031-noisy",
            url="https://northroast.example/catalog",
            content_hash="sha256:t031noisy",
            observed_at=CREATED_AT,
            extractor_version="t031@1",
            confidence=0.9,
            source_class="scraped",
        ),
    )
    provider = HashEmbedding()
    reembed_products(neo4j_session, provider)

    result = CandidateRetrieval(GraphCandidateSource(neo4j_session, provider=provider)).retrieve(
        intent(
            query="single origin whole beans",
            constraints=[{"field": "net_weight", "op": "eq", "value": 0.3, "unit": "kg"}],
            category="coffee",
        )
    )

    assert "t031-noisy" in result.product_ids
