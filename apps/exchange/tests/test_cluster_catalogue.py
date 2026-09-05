"""The assignment rule itself, and the deployment key that states its vocabulary.

``test_cluster_assignment.py`` proves the journey works end to end over three loopback ports.
This file pins the properties that make that green mean something — the ones a live run
cannot distinguish because it only ever exercises one intent:

* nothing is invented (no evidence -> no cluster), because a "closest" cluster would put an
  auction in front of a store whose merchant authorised a different catalogue;
* an id the caller already stated and the catalogue knows is not overruled;
* the answer does not depend on the order a JSON array happened to list clusters in;
* a malformed constraint contributes no evidence rather than raising into the auction;
* a deployment document that states a broken vocabulary is REFUSED, loudly, rather than
  degraded to "no clusters" — which is the same empty shortlist with no line pointing at it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from exchange.composition import (
    ENV_DEPLOYMENT,
    ENV_DEPLOYMENT_JSON,
    DeploymentConfigurationError,
    ensure_configured,
    parse_deployment,
)
from exchange.main import create_app
from exchange.retrieval.clusters import (
    SOURCE_ASSIGNED,
    SOURCE_STATED,
    SOURCE_UNASSIGNED,
    ClusterRow,
    NoIntentClusters,
    StaticIntentClusterCatalogue,
    assign_cluster,
    intent_clusters_of,
)

ESPRESSO: dict[str, Any] = {
    "cluster_id": "cluster-espresso",
    "label": "Espresso machines",
    "category": "coffee",
    "terms": ["espresso machine", "espresso"],
    "attributes": {"brew_method": "espresso"},
}
WARM_LAYERS: dict[str, Any] = {
    "cluster_id": "cluster-warm-layers",
    "label": "Warm layers",
    "category": "apparel",
    "terms": ["fleece", "base layer"],
    "attributes": {"insulation": "down"},
}


def _catalogue(*rows: dict[str, Any]) -> StaticIntentClusterCatalogue:
    return StaticIntentClusterCatalogue.from_rows(list(rows))


def _intent(**overrides: Any) -> dict[str, Any]:
    intent: dict[str, Any] = {
        "intent_id": "int-1",
        "cluster_id": "cl-306c4c3b28e29cfd",
        "query": "I want an espresso machine for the office",
        "category": "coffee",
        "hard_constraints": [{"field": "brew_method", "op": "eq", "value": "espresso"}],
        "preferences": [],
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }
    intent.update(overrides)
    return intent


# =====================================================================================
# The rule
# =====================================================================================
def test_an_intent_that_matches_nothing_is_assigned_nothing() -> None:
    """No "closest" cluster. The nearest miss is not a member of an envelope's set.

    Asserted on an intent with a real category and a real constraint, both of which simply
    belong to another cluster — not on an empty intent, because an empty intent would fail
    this for the wrong reason.
    """
    assignment = assign_cluster(
        _intent(
            query="a merino base layer for winter running",
            category="apparel",
            hard_constraints=[{"field": "insulation", "op": "eq", "value": "synthetic"}],
        ),
        _catalogue(ESPRESSO),
    )
    assert assignment.cluster_id is None
    assert assignment.source == SOURCE_UNASSIGNED
    assert assignment.considered == 1, (
        "the catalogue must be reported as READ; 'considered=0' says the exchange has no "
        "vocabulary wired, which is a different condition an operator must be able to tell apart"
    )


def test_an_unwired_exchange_assigns_nothing_and_says_it_read_no_catalogue() -> None:
    """The fail-closed default, and the distinction a bare ``None`` would lose."""
    assignment = assign_cluster(_intent(), NoIntentClusters())
    assert assignment.cluster_id is None
    assert assignment.source == SOURCE_UNASSIGNED
    assert assignment.considered == 0


def test_a_cluster_the_caller_already_named_is_not_overruled() -> None:
    """A caller that named a real cluster is not second-guessed by an inference.

    The intent below states ``cluster-warm-layers`` while its words, category and constraint
    all point at ``cluster-espresso``. The stated name wins, because it is a statement and the
    rest is evidence.
    """
    assignment = assign_cluster(
        _intent(cluster_id="cluster-warm-layers"), _catalogue(ESPRESSO, WARM_LAYERS)
    )
    assert assignment.cluster_id == "cluster-warm-layers"
    assert assignment.source == SOURCE_STATED


def test_an_id_the_catalogue_does_not_know_is_replaced() -> None:
    """The clarifier's hash is exactly this case, and it is the whole defect."""
    assignment = assign_cluster(_intent(), _catalogue(ESPRESSO, WARM_LAYERS))
    assert assignment.cluster_id == "cluster-espresso"
    assert assignment.source == SOURCE_ASSIGNED
    assert "category=coffee" in assignment.evidence
    assert "brew-method=espresso" in assignment.evidence


def test_the_answer_does_not_depend_on_catalogue_order() -> None:
    """S4: two runs on one intent give one answer, whatever order the document listed.

    Driven with two clusters that tie exactly — same category, same single matching term —
    so the tie-break is the only thing deciding, and it is asserted in both orders.
    """
    left = {"cluster_id": "cluster-aaa", "category": "coffee", "terms": ["espresso"]}
    right = {"cluster_id": "cluster-bbb", "category": "coffee", "terms": ["espresso"]}
    intent = _intent(hard_constraints=[])
    forwards = assign_cluster(intent, _catalogue(left, right))
    backwards = assign_cluster(intent, _catalogue(right, left))
    assert forwards.cluster_id == backwards.cluster_id == "cluster-aaa"
    assert forwards.score == backwards.score


def test_a_term_matches_whole_words_only() -> None:
    """``tea`` must not be found inside ``steam``.

    A substring test would put a shopper asking about a steam wand into the tea cluster on the
    strength of one accident, and an envelope would authorise a bid nobody approved.
    """
    tea = {"cluster_id": "cluster-tea", "terms": ["tea"]}
    assignment = assign_cluster(
        _intent(query="a machine with a steam wand", category=None, hard_constraints=[]),
        _catalogue(tea),
    )
    assert assignment.cluster_id is None, f"matched on {assignment.evidence!r}"


def test_a_constraint_the_module_cannot_decide_contributes_nothing_and_does_not_raise() -> None:
    """A malformed constraint must not become a 500 on a route that answers 422 for it.

    ``op: "regex"`` is not in the pinned ``ConstraintOp`` vocabulary, so ``HardCriterion``
    refuses it. The assignment weighs the rest of the evidence and carries on.
    """
    assignment = assign_cluster(
        _intent(hard_constraints=[{"field": "brew_method", "op": "regex", "value": "esp.*"}]),
        _catalogue(ESPRESSO),
    )
    assert assignment.cluster_id == "cluster-espresso"
    assert not any(reason.startswith("brew-method=") for reason in assignment.evidence), (
        f"an undecidable constraint was counted as evidence: {assignment.evidence!r}"
    )


def test_applying_an_assignment_copies_the_intent_rather_than_mutating_it() -> None:
    """The caller's request body is FastAPI's, and the auction's record of what was asked for
    must not depend on when it was read."""
    intent = _intent()
    applied = assign_cluster(intent, _catalogue(ESPRESSO)).applied_to(intent)
    assert applied["cluster_id"] == "cluster-espresso"
    assert intent["cluster_id"] == "cl-306c4c3b28e29cfd"
    assert applied is not intent


def test_a_cluster_row_needs_a_name() -> None:
    with pytest.raises(ValueError, match="non-empty cluster_id"):
        ClusterRow(cluster_id="  ")


# =====================================================================================
# The deployment key
# =====================================================================================
def test_a_document_that_states_no_clusters_leaves_the_seam_unbound() -> None:
    """Omitting the key is the pre-existing exchange, and must stay that way."""
    deployment = parse_deployment({"sellers": []}, source="test")
    assert deployment.intent_clusters is None


def test_an_explicitly_empty_vocabulary_is_a_statement_and_is_bound() -> None:
    """``"intent_clusters": []`` is a person saying "this exchange has no vocabulary".

    Behaviourally identical to omitting it — nothing is assigned — and recorded differently,
    so an operator reading ``app.state`` can tell a decision from an oversight.
    """
    deployment = parse_deployment({"sellers": [], "intent_clusters": []}, source="test")
    assert deployment.intent_clusters == ()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"intent_clusters": {"cluster_id": "c"}}, "must be a JSON array"),
        ({"intent_clusters": [{"label": "no id"}]}, "non-empty cluster_id"),
        (
            {"intent_clusters": [dict(ESPRESSO), dict(ESPRESSO)]},
            "more than once",
        ),
        ({"intent_clusters": [{"cluster_id": "c", "terms": "espresso"}]}, "array of strings"),
        ({"intent_clusters": [{"cluster_id": "c", "attributes": []}]}, "JSON object"),
    ],
)
def test_a_broken_vocabulary_is_refused_rather_than_degraded(
    document: dict[str, Any], message: str
) -> None:
    """Loud, because the silent version of each of these is an empty shortlist.

    A deployment that shrugged at a typo here would boot, answer ``201``, and have every store
    decline ``cluster_not_pursued`` — the exact symptom the composition root exists to remove,
    with nothing in the output pointing at the row that was wrong.
    """
    with pytest.raises(DeploymentConfigurationError, match=message):
        parse_deployment({"sellers": [], **document}, source="test")


def test_a_deployed_exchange_binds_the_catalogue_its_document_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composition root, end to end: a document on disk becomes a live catalogue.

    Through :func:`ensure_configured` — the request-time hook the routes call — rather than by
    assigning ``app.state`` here, so this measures the wiring an operator gets and not one
    this test wrote.
    """
    document = tmp_path / "deployment.json"
    document.write_text(
        json.dumps({"sellers": [], "intent_clusters": [dict(ESPRESSO)]}), encoding="utf-8"
    )
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    app = create_app()
    bound = ensure_configured(app)
    assert "intent_clusters" in bound, f"the document's vocabulary was not bound: {bound}"

    catalogue = intent_clusters_of(app)
    assert [row.cluster_id for row in catalogue.clusters()] == ["cluster-espresso"]
    assert assign_cluster(_intent(), catalogue).cluster_id == "cluster-espresso"


def test_an_exchange_with_no_document_still_assigns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one property the composition root may not break, stated for this seam too."""
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    app = create_app()
    assert ensure_configured(app) == ()
    assert isinstance(intent_clusters_of(app), NoIntentClusters)
    assert assign_cluster(_intent(), intent_clusters_of(app)).cluster_id is None
