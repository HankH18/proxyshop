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

Everything outside the one ``@pytest.mark.docker`` graph test runs offline against the
deterministic doubles A2 requires: no Neo4j, no network, no clock sensitivity except the
latency section, which is about wall-clock behaviour and says so.
"""

from __future__ import annotations

import math
import random
from typing import Any

import pytest
from contracts.ledger import validate_ledger_payload
from exchange.auction import InMemoryLedgerSink, LedgerRecorder
from exchange.retrieval import (
    DETERMINISTIC_RERANKER_SIMILARITY_SHARE,
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
    build_query,
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
    ],
    ids=["eq-string", "lte", "gte", "in", "contains", "eq-bool"],
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


def test_the_deterministic_double_computes_the_blend_it_publishes() -> None:
    share = DETERMINISTIC_RERANKER_SIMILARITY_SHARE
    items = [
        RerankItem("p-1", "one", similarity=1.0, preference_alignment=0.0),
        RerankItem("p-2", "two", similarity=0.0, preference_alignment=1.0),
        RerankItem("p-3", "three", similarity=None, preference_alignment=1.0),
    ]
    scores = list(DeterministicReranker().rerank("anything", items))

    assert scores[0] == pytest.approx(share)
    assert scores[1] == pytest.approx(1.0 - share)
    assert scores[2] == pytest.approx(share * NEUTRAL_SIMILARITY + (1.0 - share))
    assert DeterministicReranker().interface_version == RERANKER_INTERFACE_VERSION


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
        [product("p-structured", attributes={"roast_level": "light"}, similarity=None)],
        scored=False,
    )
    result = CandidateRetrieval(structured).retrieve(
        intent(constraints=[{"field": "roast_level", "op": "eq", "value": "light"}], preferences=[])
    )

    (assessment,) = result.assessments
    assert assessment.features.similarity is None
    expected = (
        DETERMINISTIC_RERANKER_SIMILARITY_SHARE * NEUTRAL_SIMILARITY
        + (1.0 - DETERMINISTIC_RERANKER_SIMILARITY_SHARE) * NEUTRAL_ALIGNMENT
    )
    assert assessment.fit_score == pytest.approx(expected)
    assert assessment.fit_score != 0.0


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
    reranker = RecordingReranker({})
    captured: list[Any] = []

    class Capturing(RecordingReranker):
        def rerank(self, query_text: str, items: Any) -> list[float]:
            items = list(items)
            captured.extend(items)
            return super().rerank(query_text, items)

    CandidateRetrieval(LenientSource(list(FIT_RECORDS)), reranker=Capturing({})).retrieve(
        FIT_INTENT
    )
    assert reranker.seen == []
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


def test_fit_is_blind_to_tier_and_network_fee(caplog: pytest.LogCaptureFixture) -> None:
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
    result = service(records).retrieve(LOAD_INTENT, limit=len(records))

    by_id = {record["product_id"]: record for record in records}
    for assessment in result.assessments:
        attributes = by_id[assessment.product_id]["attributes"]
        assert attributes["roast_level"] == "light"
        assert attributes["net_weight"]["value"] <= 300
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
