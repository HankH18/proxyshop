"""R19's filter/score split, and the intent's agreement with the real schema (T-071).

Two things are proved here that a behavioural test of the loop cannot see:

* the split is **structural** — a hard constraint has no ``weight`` attribute to set and a
  preference has no ``op``, so neither can quietly become the other;
* what this package emits actually validates against ``packages.contracts.Intent``, the
  authoritative schema (T-010), rather than merely against this package's own idea of it.
  Without that, both halves could drift into agreement with nothing.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from apps.buyer.svc.src.intent import (
    BUDGET_BAND_UNSPECIFIED,
    BUDGET_BAND_VOCABULARY,
    CONSTRAINT_OPS,
    PREFERENCE_DIRECTIONS,
    HardConstraint,
    Intent,
    InvalidConstraint,
    InvalidPreference,
    Preference,
    UnstructuredIntent,
    band_for_amount,
    clarify,
    intent_from_payload,
)

DIALOGUE_DIR = pathlib.Path(__file__).resolve().parents[4] / "fixtures" / "dialogues"


# --- the split is a shape, not a convention -----------------------------------------


def test_a_hard_constraint_has_no_weight_to_set() -> None:
    constraint = HardConstraint(field="color", op="eq", value="navy")
    assert not hasattr(constraint, "weight")
    assert "weight" not in constraint.to_dict()
    with pytest.raises(TypeError):
        HardConstraint(field="color", op="eq", value="navy", weight=0.5)  # type: ignore[call-arg]


def test_a_preference_has_no_op_to_set() -> None:
    preference = Preference(field="rating", direction="maximize", weight=0.6)
    assert not hasattr(preference, "op")
    assert "op" not in preference.to_dict()


@pytest.mark.parametrize("op", ["lt", "gt", "ne", "regex", "between", "", "LTE", "<"])
def test_an_op_outside_r19s_closed_set_is_refused_not_coerced(op: str) -> None:
    with pytest.raises(InvalidConstraint):
        HardConstraint(field="price_usd", op=op, value=20)


@pytest.mark.parametrize("op", CONSTRAINT_OPS)
def test_every_r19_op_constructs(op: str) -> None:
    assert HardConstraint(field="f", op=op, value=1).op == op


@pytest.mark.parametrize("direction", ["min", "max", "asc", "up", "", "MAXIMIZE"])
def test_a_direction_outside_r19s_closed_set_is_refused(direction: str) -> None:
    with pytest.raises(InvalidPreference):
        Preference(field="price_usd", direction=direction, weight=1.0)


@pytest.mark.parametrize("direction", PREFERENCE_DIRECTIONS)
def test_every_r19_direction_constructs(direction: str) -> None:
    assert Preference(field="f", direction=direction, weight=1).weight == 1.0


@pytest.mark.parametrize("weight", [True, False, None, "1.0", [], {}])
def test_a_non_numeric_weight_is_refused(weight) -> None:
    """``True`` is not a weight, even though ``True + 0.5`` is 1.5."""
    with pytest.raises(InvalidPreference):
        Preference(field="rating", direction="maximize", weight=weight)


def test_a_constraint_and_a_preference_need_a_field() -> None:
    with pytest.raises(InvalidConstraint):
        HardConstraint(field="   ", op="eq", value=1)
    with pytest.raises(InvalidPreference):
        Preference(field="", direction="prefer", weight=0.1)


# --- the intent -----------------------------------------------------------------------


def test_an_intent_without_a_query_or_a_band_cannot_exist() -> None:
    kwargs = dict(intent_id="int-1", cluster_id="cl-1", created_at="2026-01-01T00:00:00Z")
    with pytest.raises(UnstructuredIntent):
        Intent(query="  ", budget_band="0-50", **kwargs)
    with pytest.raises(UnstructuredIntent):
        Intent(query="coffee", budget_band="", **kwargs)
    with pytest.raises(UnstructuredIntent):
        Intent(query="coffee", budget_band="0-50", intent_id="", cluster_id="cl-1", created_at="x")


def test_the_serialized_intent_omits_optional_fields_nobody_set() -> None:
    payload = Intent(
        query="coffee",
        budget_band="0-50",
        intent_id="int-1",
        cluster_id="cl-1",
        created_at="2026-01-01T00:00:00Z",
    ).to_dict()
    assert "ship_to" not in payload and "category" not in payload
    assert payload["hard_constraints"] == [] and payload["preferences"] == []


def test_an_intent_round_trips_through_its_serialized_form() -> None:
    original = clarify(["a daypack for commuting", "between $60 and $120", "navy"]).intent
    assert intent_from_payload(original.to_dict()) == original


# --- agreement with the authoritative schema (T-010) ----------------------------------


@pytest.mark.parametrize(
    "turns",
    [
        ["I want a light roast under $20"],
        ["hmm"],
        ["a daypack for commuting", "between $60 and $120", "navy and sustainable"],
        ["trail running shoes, size 10, waterproof"],
    ],
    ids=["priced", "vague", "range", "boolean-feature"],
)
def test_every_produced_intent_validates_against_contracts_intent(turns) -> None:
    contracts = pytest.importorskip("packages.contracts")
    model = contracts.Intent.model_validate(clarify(turns).intent.to_dict())
    assert model.query
    assert model.budget_band
    for constraint in model.hard_constraints:
        assert str(constraint.op) in CONSTRAINT_OPS
    for preference in model.preferences:
        assert str(preference.direction) in PREFERENCE_DIRECTIONS


def test_the_op_vocabulary_matches_the_generated_schema() -> None:
    contracts = pytest.importorskip("packages.contracts")
    assert {member.value for member in contracts.ConstraintOp} == set(CONSTRAINT_OPS)
    # `PreferenceDirection` is NOT re-exported by `packages.contracts` the way its sibling
    # `ConstraintOp` is (measured: `packages.contracts.PreferenceDirection` raises
    # AttributeError, and `contracts.protocol` re-exports only `ConstraintOp`), so the
    # enum is read off the field that uses it. Reaching through the model is worse than an
    # import and is done anyway, because comparing this package's vocabulary against the
    # generated one is the only thing that stops the two drifting.
    direction_enum = contracts.Preference.model_fields["direction"].annotation
    assert {member.value for member in direction_enum} == set(PREFERENCE_DIRECTIONS)


# --- agreement with T-070's budget bands ---------------------------------------------


def test_the_band_vocabulary_is_the_profiles_own() -> None:
    """One vocabulary. ``Intent.budget_band`` and ``BuyerProfile.buckets`` are two views."""
    from apps.buyer.svc.src.profile import BUDGET_BANDS, TOP_BUDGET_BAND

    profile_labels = {label for _low, _high, label in BUDGET_BANDS} | {TOP_BUDGET_BAND}
    assert profile_labels < set(BUDGET_BAND_VOCABULARY)
    assert set(BUDGET_BAND_VOCABULARY) - profile_labels == {BUDGET_BAND_UNSPECIFIED}


@pytest.mark.parametrize(
    "amount,band",
    [(0.0, "0-50"), (20.0, "0-50"), (50.0, "50-100"), (250.0, "250-500"), (5000.0, "1000+")],
)
def test_amounts_snap_onto_the_profiles_bands(amount: float, band: str) -> None:
    assert band_for_amount(amount) == band


def test_the_unspecified_band_is_not_one_of_the_numeric_ones() -> None:
    """A filter written against the numeric bands must not match "we never asked"."""
    assert BUDGET_BAND_UNSPECIFIED not in {
        band for band in BUDGET_BAND_VOCABULARY if any(ch.isdigit() for ch in band)
    }


# --- the shipped dialogue fixtures ----------------------------------------------------


def test_the_shipped_dialogues_are_well_formed() -> None:
    files = sorted(DIALOGUE_DIR.glob("*.json"))
    assert len(files) >= 3, f"R1's golden dialogues live in {DIALOGUE_DIR}"
    saw_a_question = False
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["turns"], f"{path.name}: needs buyer utterances"
        expected = data["expected_intent"]
        assert {"query", "hard_constraints", "preferences", "budget_band"} <= set(expected)
        assert expected["budget_band"] in BUDGET_BAND_VOCABULARY, path.name
        for item in expected["hard_constraints"]:
            assert item["op"] in CONSTRAINT_OPS, path.name
            assert "weight" not in item, path.name
        for item in expected["preferences"]:
            assert item["direction"] in PREFERENCE_DIRECTIONS, path.name
        saw_a_question = saw_a_question or data.get("_questions_expected", 0) >= 1
    assert saw_a_question, "no shipped dialogue exercises the clarification loop at all"


def test_one_shipped_dialogue_reaches_the_three_question_ceiling() -> None:
    """The cap is only tested by a dialogue that actually reaches it."""
    counts = [
        json.loads(path.read_text(encoding="utf-8")).get("_questions_expected", 0)
        for path in sorted(DIALOGUE_DIR.glob("*.json"))
    ]
    assert max(counts) == 3, counts
