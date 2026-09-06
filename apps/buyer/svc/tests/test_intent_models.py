"""R19's filter/score split, and the intent's agreement with the real schema (T-071).

Two things are proved here that a behavioural test of the loop cannot see:

* the split is **structural** — a hard constraint has no ``weight`` attribute to set and a
  preference has no ``op``, so neither can quietly become the other;
* what this package emits actually validates against ``packages.contracts.Intent``, the
  authoritative schema (T-010), rather than merely against this package's own idea of it.
  Without that, both halves could drift into agreement with nothing.
"""

from __future__ import annotations

import importlib
import json
import pathlib
from typing import Any

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


# =====================================================================================
# T-368 — POST /buyer/intent/confirm is anonymous, so what it STORES must be bounded
# =====================================================================================
# The door takes no credential: the only ``Depends`` in this service is a DI provider, not
# a guard. ``ConfirmBody.intent`` is ``dict[str, Any]``, and the ``intent_id`` inside it was
# read by a WHITESPACE NORMALISER rather than by a length check before becoming the key of
# the module-global ``ConfirmationLedger._auctions`` — written with no capacity, no TTL, no
# LRU and no sweep, ``release()`` freeing only an UNSPENT claim and ``reset()`` documented
# test-only. Measured on this branch before the fix: one request carrying a 30 000-character
# ``intent_id`` answered **201**, left one permanent ledger entry, and echoed the whole id
# back in the 201 body; a burst grew linearly at ~30 127 B/request.
#
# Each test below asserts three things at once, and each is a separate way the hole reopens:
# nothing hostile is STORED, nothing hostile is FORWARDED to the exchange, and the refusal is
# a clean 4xx that does not quote the body back.

CONFIRM_PATH = "/buyer/intent/confirm"
CLARIFY_PATH = "/buyer/intent/clarify"

#: The length in T-368's own reproduction. Not rounded: this is the number measured
#: answering 201.
HOSTILE_CHARS = 30_000

#: The most of a refusal this gate will read. A 4xx that quotes the offending body back is
#: the amplifier the bound was supposed to close — a refusal that costs the same bandwidth
#: as the attack is not a refusal — so the answer is checked for SIZE, not only for status.
MAX_REFUSAL_BYTES = 4 * 1024


def _wire_intent(**overrides: Any) -> dict[str, Any]:
    """A structured intent as it arrives on the wire: R1 needs a query and a band."""
    payload: dict[str, Any] = {
        "intent_id": "int-bound-1",
        "cluster_id": "cl-bound-1",
        "query": "a warm merino wool beanie for winter",
        "budget_band": "50-100",
        "hard_constraints": [],
        "preferences": [],
        "currency": "USD",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }
    payload.update(overrides)
    return payload


class _RecordingExchange:
    """Every auction this door opens lands in ``.payloads``, and nothing else does."""

    def __init__(self) -> None:
        self.payloads: list[Any] = []

    def create_auction(self, payload: Any) -> dict[str, str]:
        self.payloads.append(payload)
        return {"auction_id": "auc-bound-1"}


@pytest.fixture()
def confirm_door():
    """The real HTTP door through the frozen entrypoint, with a recording exchange behind it.

    ``raise_server_exceptions=False`` on purpose: an unhandled exception has to be READABLE
    here as the 500 it would be on the wire. Re-raised into the test it looks like a broken
    test rather than like the fail-OPEN answer an anonymous caller would actually receive.
    """
    from fastapi.testclient import TestClient

    from apps.buyer.svc.src.intent import reset_confirmations
    from apps.buyer.svc.src.intent.routes import set_buyer_llm

    main = importlib.import_module("buyer_svc.main")
    reset_confirmations()
    set_buyer_llm(None)
    app = main.create_app()
    exchange = _RecordingExchange()
    app.state.auction_client = exchange
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, exchange
    reset_confirmations()


#: Every lever an anonymous body has on how much this service allocates and keeps: the length
#: of a single field, the number of elements in a list, and — separately — the size of the
#: whole document when no single part of it is over any per-field ceiling.
HOSTILE_INTENTS: dict[str, dict[str, Any]] = {
    "intent-id": _wire_intent(intent_id="i" * HOSTILE_CHARS),
    "cluster-id": _wire_intent(cluster_id="c" * HOSTILE_CHARS),
    "query": _wire_intent(query="q" * HOSTILE_CHARS),
    "budget-band": _wire_intent(budget_band="b" * HOSTILE_CHARS),
    "currency": _wire_intent(currency="u" * HOSTILE_CHARS),
    "created-at": _wire_intent(created_at="t" * HOSTILE_CHARS),
    "schema-version": _wire_intent(schema_version="v" * HOSTILE_CHARS),
    "category": _wire_intent(category="k" * HOSTILE_CHARS),
    "ship-to": _wire_intent(ship_to="s" * HOSTILE_CHARS),
    "constraint-count": _wire_intent(
        hard_constraints=[{"field": f"f{n}", "op": "eq", "value": n} for n in range(5_000)]
    ),
    "constraint-field": _wire_intent(
        hard_constraints=[{"field": "f" * HOSTILE_CHARS, "op": "eq", "value": 1}]
    ),
    "constraint-value": _wire_intent(
        hard_constraints=[{"field": "colour", "op": "eq", "value": "v" * HOSTILE_CHARS}]
    ),
    "constraint-value-list": _wire_intent(
        hard_constraints=[{"field": "colour", "op": "in", "value": ["navy"] * 50_000}]
    ),
    "constraint-unit": _wire_intent(
        hard_constraints=[{"field": "weight", "op": "lte", "value": 2, "unit": "u" * HOSTILE_CHARS}]
    ),
    "preference-count": _wire_intent(
        preferences=[
            {"field": f"p{n}", "direction": "maximize", "weight": 1.0} for n in range(5_000)
        ]
    ),
    "preference-field": _wire_intent(
        preferences=[{"field": "p" * HOSTILE_CHARS, "direction": "maximize", "weight": 1.0}]
    ),
    # Nothing here is over any PER-FIELD ceiling and nothing is over any COUNT ceiling — 60
    # constraints of 2 000 characters each. It is 120 KB of document all the same, which is
    # why a total-size budget is a third bound rather than a restatement of the other two.
    "total-size": _wire_intent(
        hard_constraints=[{"field": f"f{n}", "op": "eq", "value": "v" * 2_000} for n in range(60)]
    ),
}


@pytest.mark.parametrize("shape", sorted(HOSTILE_INTENTS))
def test_an_anonymous_oversized_intent_is_neither_stored_nor_forwarded(confirm_door, shape) -> None:
    """The whole of T-368, one lever at a time."""
    from apps.buyer.svc.src.intent import confirmations

    client, exchange = confirm_door
    intent = HOSTILE_INTENTS[shape]
    response = client.post(CONFIRM_PATH, json={"intent": intent, "confirmed": True})

    assert 400 <= response.status_code < 500, (
        f"{shape}: POST /confirm -> {response.status_code}; an unauthenticated caller must "
        f"not be able to choose how much of this service's storage it consumes"
    )
    assert exchange.payloads == [], f"{shape}: the oversized intent was forwarded to the exchange"
    assert confirmations().auction_for(intent["intent_id"]) is None, (
        f"{shape}: the oversized intent left a permanent entry in the confirmation ledger"
    )
    assert len(response.content) <= MAX_REFUSAL_BYTES, (
        f"{shape}: the refusal is {len(response.content)} bytes — it echoes the offending "
        f"body back, which makes refusing as expensive as accepting"
    )


#: Bodies that are well-formed JSON, small, and say something this package cannot express.
#: Each is a way into the handler that is NOT a size problem, and none of them may be a 500:
#: ``InvalidConstraint`` and ``InvalidPreference`` are ``IntentError`` subclasses that
#: ``confirm_route`` never named, so they escaped as an unauthenticated server fault.
MALFORMED_INTENTS: dict[str, dict[str, Any]] = {
    "op-outside-r19": _wire_intent(
        hard_constraints=[{"field": "price_usd", "op": "lt", "value": 20}]
    ),
    "direction-outside-r19": _wire_intent(
        preferences=[{"field": "rating", "direction": "up", "weight": 1.0}]
    ),
    "weight-is-not-a-number": _wire_intent(
        preferences=[{"field": "rating", "direction": "maximize", "weight": None}]
    ),
    "constraint-is-not-an-object": _wire_intent(hard_constraints=["nope"]),
    "preference-is-not-an-object": _wire_intent(preferences=[42]),
    "constraint-has-no-field": _wire_intent(hard_constraints=[{"op": "eq", "value": 1}]),
}


@pytest.mark.parametrize("shape", sorted(MALFORMED_INTENTS))
def test_a_hostile_body_cannot_be_driven_into_a_500(confirm_door, shape) -> None:
    """Refusals belong to the caller. A 5xx on an unauthenticated door is a lever too."""
    client, exchange = confirm_door
    response = client.post(
        CONFIRM_PATH, json={"intent": MALFORMED_INTENTS[shape], "confirmed": True}
    )
    assert 400 <= response.status_code < 500, (
        f"{shape}: POST /confirm -> {response.status_code}, so an anonymous body chooses "
        f"whether this service reports a server fault"
    )
    assert exchange.payloads == []


def test_a_burst_of_oversized_confirms_leaves_the_ledger_empty(confirm_door) -> None:
    """T-368's linear growth, driven. 50 requests, 50 distinct keys, zero entries."""
    from apps.buyer.svc.src.intent import confirmations

    client, exchange = confirm_door
    ids = [f"{'i' * HOSTILE_CHARS}-{n}" for n in range(50)]
    for intent_id in ids:
        response = client.post(
            CONFIRM_PATH, json={"intent": _wire_intent(intent_id=intent_id), "confirmed": True}
        )
        assert 400 <= response.status_code < 500, response.status_code
    kept = [intent_id for intent_id in ids if confirmations().auction_for(intent_id) is not None]
    assert kept == [], f"{len(kept)} oversized ids are now permanent keys in the ledger"
    assert exchange.payloads == []


def test_an_oversized_confirm_body_is_refused_whole(confirm_door) -> None:
    """The other halves of the body — ``profile`` and ``roster`` — are levers of their own."""
    from apps.buyer.svc.src.intent import confirmations

    client, exchange = confirm_door
    for body in (
        {
            "intent": _wire_intent(),
            "confirmed": True,
            "profile": {"pseudonym": "psn-1", "buckets": {"note": "p" * HOSTILE_CHARS}},
        },
        {
            "intent": _wire_intent(),
            "confirmed": True,
            "roster": [{"store_id": f"s{n}", "tier": 1} for n in range(50_000)],
        },
    ):
        response = client.post(CONFIRM_PATH, json=body)
        assert 400 <= response.status_code < 500, response.status_code
        assert len(response.content) <= MAX_REFUSAL_BYTES
    assert exchange.payloads == []
    assert confirmations().auction_for("int-bound-1") is None


def test_an_oversized_clarify_dialogue_cannot_mint_an_intent_confirm_would_refuse(
    confirm_door,
) -> None:
    """The two doors have to agree, or /clarify hands the buyer an intent /confirm rejects."""
    client, _exchange = confirm_door
    response = client.post(CLARIFY_PATH, json={"turns": ["t" * HOSTILE_CHARS]})
    assert 400 <= response.status_code < 500, response.status_code
    assert len(response.content) <= MAX_REFUSAL_BYTES

    many = client.post(CLARIFY_PATH, json={"turns": ["a daypack"] * 50_000})
    assert 400 <= many.status_code < 500, many.status_code
    assert len(many.content) <= MAX_REFUSAL_BYTES


# --- the bounds are named, and the boundary is exact ----------------------------------


def test_an_honest_confirm_still_opens_exactly_one_auction(confirm_door) -> None:
    """The bound may not cost R1 its own path. Green before the fix and after."""
    from apps.buyer.svc.src.intent import confirmations

    client, exchange = confirm_door
    response = client.post(CONFIRM_PATH, json={"intent": _wire_intent(), "confirmed": True})
    assert response.status_code == 201, response.text
    assert len(exchange.payloads) == 1
    assert confirmations().auction_for("int-bound-1") == "auc-bound-1"


def test_the_identifier_ceiling_is_exact_and_is_the_exchanges_own_number(confirm_door) -> None:
    """At the ceiling: 201. One character over: refused. And the number is not invented."""
    from apps.buyer.svc.src.intent import MAX_IDENTIFIER_LENGTH

    client, exchange = confirm_door
    accepted = client.post(
        CONFIRM_PATH,
        json={"intent": _wire_intent(intent_id="i" * MAX_IDENTIFIER_LENGTH), "confirmed": True},
    )
    assert accepted.status_code == 201, accepted.text

    over = client.post(
        CONFIRM_PATH,
        json={
            "intent": _wire_intent(intent_id="j" * (MAX_IDENTIFIER_LENGTH + 1)),
            "confirmed": True,
        },
    )
    assert 400 <= over.status_code < 500, over.status_code
    assert len(exchange.payloads) == 1, "the one-over id opened an auction"

    exchange_routes = pytest.importorskip("exchange.auction.routes")
    assert MAX_IDENTIFIER_LENGTH == exchange_routes.MAX_IDENTIFIER_LENGTH, (
        "the buyer's identifier ceiling and the exchange's have drifted; the intent_id this "
        "service stores travels to that same door"
    )


def test_every_bound_is_a_named_constant_this_package_publishes() -> None:
    """A magic literal in a handler is a bound nobody downstream can test against."""
    intent_package = importlib.import_module("apps.buyer.svc.src.intent")
    for name in (
        "MAX_IDENTIFIER_LENGTH",
        "MAX_FREE_TEXT_CHARS",
        "MAX_INTENT_TERMS",
        "MAX_INTENT_BYTES",
    ):
        assert name in intent_package.__all__, f"{name} is not published by the package"
        assert isinstance(getattr(intent_package, name), int)


def test_the_library_refuses_an_oversized_intent_object_the_wire_never_built() -> None:
    """``coerce_intent`` hands an ``Intent`` back untouched, so the wire check cannot be the
    only one: the ledger key is minted here, not at the route."""
    from apps.buyer.svc.src.intent import (
        IntentTooLarge,
        confirm,
        confirmations,
        reset_confirmations,
    )

    reset_confirmations()
    calls: list[Any] = []
    intent = Intent(
        query="coffee",
        budget_band="0-50",
        intent_id="i" * HOSTILE_CHARS,
        cluster_id="cl-1",
        created_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(IntentTooLarge):
        confirm(intent, calls.append, confirmed=True)
    assert calls == [], "the exchange was called for an intent too large to store"
    assert confirmations().auction_for(intent.intent_id) is None
    reset_confirmations()


@pytest.mark.parametrize(
    "path,body",
    [
        (CLARIFY_PATH, {"turns": "t" * HOSTILE_CHARS}),
        (CONFIRM_PATH, {"intent": "i" * HOSTILE_CHARS, "confirmed": True}),
        (CONFIRM_PATH, {"intent": {}, "confirmed": True, "roster": "r" * HOSTILE_CHARS}),
    ],
    ids=["clarify-turns", "confirm-intent", "confirm-roster"],
)
def test_the_wire_layers_own_refusal_does_not_quote_the_body_back(confirm_door, path, body) -> None:
    """A field of the wrong TYPE is refused by FastAPI, not by this module — and FastAPI's
    default 422 embeds the offending value under ``input``.

    Measured against a bare FastAPI app with the same model: ``{"turns": "t" * 30000}``
    answers **422 with a 30 104-byte body**, every byte of it the caller's own string. That
    is a refusal costing what accepting cost, on a door with no credential.
    """
    client, exchange = confirm_door
    response = client.post(path, json=body)
    assert 400 <= response.status_code < 500, response.status_code
    assert len(response.content) <= MAX_REFUSAL_BYTES, (
        f"{path}: the wire-layer refusal is {len(response.content)} bytes"
    )
    assert exchange.payloads == []


def test_a_body_this_service_cannot_even_parse_is_a_4xx(confirm_door) -> None:
    """A 4 KB body of nothing but brackets is a lever on the JSON parser, not on storage.

    Measured before the fix: 2 000 nested arrays -> ``RecursionError`` inside Starlette's
    ``await request.json()``, i.e. **HTTP 500** on an unauthenticated door, before any
    handler code ran. The refusal has to be the caller's, so it is caught around the route.
    """
    client, exchange = confirm_door
    raw = '{"intent":' + "[" * 2_000 + "]" * 2_000 + ',"confirmed":true}'
    response = client.post(
        CONFIRM_PATH, content=raw.encode(), headers={"content-type": "application/json"}
    )
    assert 400 <= response.status_code < 500, response.status_code
    assert len(response.content) <= MAX_REFUSAL_BYTES
    assert exchange.payloads == []


@pytest.mark.parametrize(
    "raw,shape",
    [
        (
            '{"intent":{"intent_id":"int-inf","cluster_id":"cl","query":"q","budget_band":"b"},'
            '"confirmed":true,"bid_timeout_seconds":1e400}',
            "an infinite bid window",
        ),
        (
            '{"intent":{"intent_id":"int-nan","cluster_id":"cl","query":"q","budget_band":"b",'
            '"preferences":[{"field":"f","direction":"maximize","weight":1e400}]},'
            '"confirmed":true}',
            "an infinite preference weight",
        ),
    ],
)
def test_a_non_finite_number_is_refused_rather_than_auctioned(confirm_door, raw, shape) -> None:
    """``1e400`` is legal RFC-8259 JSON and Python parses it to ``inf``.

    The exchange learned this on ``list_price`` (``allow_inf_nan=False``, and a docstring
    about why ``gt=0.0`` did not catch it). The same literal arrives here: measured before
    the fix, both bodies answered **201** and the ``inf`` was forwarded to the exchange —
    an unbounded bid window in one case, an undefined ranking score in the other.
    """
    client, exchange = confirm_door
    response = client.post(
        CONFIRM_PATH, content=raw.encode(), headers={"content-type": "application/json"}
    )
    assert 400 <= response.status_code < 500, f"{shape}: {response.status_code}"
    assert exchange.payloads == [], f"{shape} reached the exchange"


def test_the_refusal_names_the_field_and_the_ceiling_but_never_quotes_the_value() -> None:
    from apps.buyer.svc.src.intent import IntentTooLarge

    with pytest.raises(IntentTooLarge) as caught:
        intent_from_payload(_wire_intent(query="q" * HOSTILE_CHARS))
    message = str(caught.value)
    assert "query" in message
    assert "q" * 100 not in message, "the refusal quotes the oversized value back"
    assert len(message) <= 512, f"a {len(message)}-character refusal is an echo by another name"
