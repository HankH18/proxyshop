"""R14's prompt, and the negative half of it that the frozen suite only samples (T-073).

The frozen acceptance test checks one un-routed order and one routed one. That is the shape of
the guarantee, not the extent of it: "only network-routed orders" is a NEGATIVE clause, so the
interesting inputs are the ones that *look* routed. Every case below is one of those.
"""

from __future__ import annotations

import json

import pytest

from apps.buyer.svc.src.feedback import (
    CHOICE_IDS,
    FEEDBACK_CHOICES,
    PROMPT_INPUT_TYPE,
    PROMPT_QUESTION_ID,
    FeedbackPrompt,
    UnusableOrder,
    feedback_prompt,
    routing,
)

ROUTED = {
    "order_ref": "ord-unit-1",
    "store_id": "st-1",
    "auction_id": "auc-unit-3",
    "routed": True,
}

#: Every free-text key the frozen suite bans, plus the input-type values it bans. Restated here
#: because this suite must fail on its own when the prompt grows a comment box, without waiting
#: for the acceptance run.
BANNED_KEYS = {
    "free_text",
    "freetext",
    "free_response",
    "open_text",
    "open_response",
    "comment",
    "comments",
    "note",
    "notes",
    "textarea",
    "message",
}
BANNED_INPUT_VALUES = {"text", "textarea", "string", "free_text", "freetext", "open"}


def _walk(value, path=""):
    if isinstance(value, dict):
        for key, sub in value.items():
            yield str(key), sub
            yield from _walk(sub, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, sub in enumerate(value):
            yield from _walk(sub, f"{path}[{index}]")


# --- the positive case -------------------------------------------------------------------


def test_a_routed_order_is_offered_exactly_one_structured_prompt() -> None:
    prompt = feedback_prompt(ROUTED)
    assert isinstance(prompt, FeedbackPrompt)

    plain = prompt.to_dict()
    assert isinstance(plain, dict), "one prompt, not a collection"
    assert plain["question_id"] == PROMPT_QUESTION_ID == "matched_pitch"
    assert plain["question"].strip()
    assert plain["input_type"] == PROMPT_INPUT_TYPE
    assert plain["order_ref"] == "ord-unit-1"
    assert plain["auction_id"] == "auc-unit-3"
    assert [option["id"] for option in plain["options"]] == list(CHOICE_IDS)
    assert len(plain["options"]) >= 2


def test_the_prompt_question_id_is_the_published_payload_key() -> None:
    """One question, one payload key: there is no mapping table between them to drift."""
    from contracts import LEDGER_PAYLOAD_SHAPES

    assert PROMPT_QUESTION_ID in LEDGER_PAYLOAD_SHAPES["feedback"]


def test_every_option_carries_a_label_and_a_verdict() -> None:
    for choice in FEEDBACK_CHOICES:
        assert choice.id and choice.id == choice.id.strip()
        assert choice.label.strip()
        assert isinstance(choice.matched_pitch, bool)
    assert len({choice.id for choice in FEEDBACK_CHOICES}) == len(FEEDBACK_CHOICES)
    verdicts = {choice.matched_pitch for choice in FEEDBACK_CHOICES}
    assert verdicts == {True, False}, "the prompt must be answerable both ways"


def test_the_prompt_offers_no_free_text_field_anywhere() -> None:
    plain = feedback_prompt(ROUTED).to_dict()
    for key, value in _walk(plain):
        assert key.lower() not in BANNED_KEYS, f"free-text field {key!r} on the prompt"
        if key.lower() in {"type", "input_type", "input", "kind", "widget"}:
            assert str(value).lower() not in BANNED_INPUT_VALUES

    # Belt and braces: nothing in the serialized prompt spells a text input at all.
    blob = json.dumps(plain).lower()
    for needle in ("textarea", "free_text", "<input", 'type="text"'):
        assert needle not in blob


# --- the negative case, attacked ---------------------------------------------------------


@pytest.mark.parametrize(
    ("order", "why"),
    [
        ({"order_ref": "o", "store_id": "s", "routed": False}, "flag is false"),
        ({"order_ref": "o", "store_id": "s", "auction_id": "a", "routed": False}, "false wins"),
        ({"order_ref": "o", "store_id": "s"}, "nothing says it was routed"),
        ({"order_ref": "o", "store_id": "s", "auction_id": ""}, "empty auction id"),
        ({"order_ref": "o", "store_id": "s", "auction_id": "   "}, "blank auction id"),
        ({"order_ref": "o", "auction_id": "a", "routed": "false"}, "the STRING 'false'"),
        ({"order_ref": "o", "auction_id": "a", "routed": "no"}, "the string 'no'"),
        ({"order_ref": "o", "auction_id": "a", "routed": 0}, "integer zero"),
        ({"order_ref": "o", "routed": True}, "claims routed but names no auction"),
        ({"order_ref": "o", "routed": "yes"}, "coercible string, still no auction"),
    ],
)
def test_no_prompt_is_offered_for_an_order_that_is_not_network_routed(order, why) -> None:
    assert feedback_prompt(order) is None, why
    assert not routing(order).routed, why
    assert routing(order).reason, "a refusal must say why, in words a screen can show"


def test_the_string_false_is_not_read_as_true() -> None:
    """`bool("false")` is `True` in Python; that reading would break R14's whole clause."""
    assert bool("false") is True  # the trap, stated
    assert feedback_prompt({"order_ref": "o", "auction_id": "a", "routed": "false"}) is None


def test_the_gate_fails_closed_when_nothing_says_the_order_was_routed() -> None:
    """The measured hole this replaces: absence of a `routed` flag read as ROUTED.

    The earlier rule refused only an explicit `routed: false` and otherwise fell back to "does
    the record name an auction?". `auction_id` is entirely caller-supplied, so an order record
    with no routing flag at all — or one whose flag was unreadable — was offered a prompt and
    could land a `feedback` ledger event. R14's clause is a negative guarantee; absence of
    evidence is not evidence, and this asserts the conservative reading in every spelling of
    "the record does not say".
    """
    for unstated in (
        {},
        {"routed": None},
        {"routed": ""},
        {"routed": "maybe"},
        {"routed": []},
        {"routed": 2},
        {"routed": "False."},
    ):
        order = {"order_ref": "o", "store_id": "s", "auction_id": "a", **unstated}
        assert feedback_prompt(order) is None, f"{unstated!r} was read as routed"
        assert routing(order).routed is False, f"{unstated!r} was read as routed"
        assert "not marked as network-routed" in routing(order).reason

    # ...and an affirmative flag with an auction still works, so the gate is not simply closed.
    assert feedback_prompt({"order_ref": "o", "auction_id": "a", "routed": True}) is not None


def test_an_explicit_false_anywhere_beats_an_unreadable_flag_before_it() -> None:
    """Measured: stopping at the first PRESENT field never consulted the one that said no."""
    order = {"order_ref": "o", "auction_id": "a", "routed": "nope", "network_routed": False}
    assert feedback_prompt(order) is None
    assert routing(order).routed is False

    contradiction = {"order_ref": "o", "auction_id": "a", "routed": True, "network_routed": False}
    assert feedback_prompt(contradiction) is None, (
        "a record that contradicts itself must not be resolved in the permissive direction"
    )


def test_a_numpy_or_decimal_boolean_is_read_rather_than_ignored() -> None:
    """`numpy.bool_` is not a `bool` and `numpy.int64` is not an `int`; numpy is a dependency."""
    from decimal import Decimal

    import numpy

    for truthy in (numpy.True_, numpy.int64(1), Decimal(1)):
        order = {"order_ref": "o", "auction_id": "a", "routed": truthy}
        assert routing(order).routed is True, f"{truthy!r} was not read as routed"
    for falsy in (numpy.False_, numpy.int64(0), Decimal(0)):
        order = {"order_ref": "o", "auction_id": "a", "routed": falsy}
        assert routing(order).routed is False, f"{falsy!r} was not read as un-routed"


@pytest.mark.parametrize("status", ["cancelled", "canceled", "CANCELLED", "voided", "void"])
def test_an_order_cancelled_before_it_arrived_is_offered_no_prompt(status) -> None:
    order = dict(ROUTED, status=status)
    assert feedback_prompt(order) is None
    # ...but it is still network-routed, so feedback already given about it stands.
    assert routing(order).routed is True
    assert routing(order).eligible is False


@pytest.mark.parametrize("status", ["refunded", "returned", "fulfilled", "paid", ""])
def test_a_returned_or_refunded_order_still_gets_the_prompt(status) -> None:
    """R14 cross-checks feedback against returns; it does not silence returning buyers."""
    assert feedback_prompt(dict(ROUTED, status=status)) is not None


@pytest.mark.parametrize("bad", [None, "ord-1", 7, 7.0, True, b"ord-1"])
def test_a_thing_that_is_not_an_order_is_a_domain_refusal_not_a_builtin(bad) -> None:
    with pytest.raises(UnusableOrder):
        feedback_prompt(bad)
    with pytest.raises(RuntimeError):  # UnusableOrder is a FeedbackError is a RuntimeError
        feedback_prompt(bad)


def test_a_hostile_order_record_cannot_raise_out_of_the_prompt() -> None:
    class Hostile:
        def __getattr__(self, name):
            raise ZeroDivisionError(name)

    assert feedback_prompt(Hostile()) is None


def test_an_enormous_order_record_is_read_by_name_and_not_echoed() -> None:
    order = dict(ROUTED, junk="x" * 5_000_000, notes="y" * 100_000)
    plain = feedback_prompt(order).to_dict()
    assert set(plain) == {
        "order_ref",
        "store_id",
        "auction_id",
        "question_id",
        "question",
        "input_type",
        "options",
    }
    assert len(json.dumps(plain)) < 2_000


def test_the_prompt_is_a_pure_function_of_the_record() -> None:
    """Asking twice answers twice. The once-only guarantee lives at the WRITE boundary."""
    first = feedback_prompt(ROUTED)
    second = feedback_prompt(ROUTED)
    assert first is not None and second is not None
    assert first.to_dict() == second.to_dict()


def test_the_prompt_is_subscriptable_as_well_as_attribute_addressed() -> None:
    prompt = feedback_prompt(ROUTED)
    assert prompt["question"] == prompt.question
    with pytest.raises(KeyError):
        prompt["nope"]


@pytest.mark.parametrize(
    ("value", "routed"),
    [
        (False, False),
        (0, False),
        (0.0, False),  # MEASURED hole: `isinstance(0.0, int)` is False, so this used to
        ("0", False),  # read as "the record does not say" and fall through to ROUTED.
        ("off", False),
        (True, True),
        (1, True),
        (1.0, True),
        ("1", True),
        ("on", True),
    ],
)
def test_every_numeric_and_textual_spelling_of_the_flag_is_read_the_same_way(value, routed) -> None:
    order = {"order_ref": "o", "store_id": "s", "auction_id": "a", "routed": value}
    assert routing(order).routed is routed, f"routed={value!r} read the wrong way"
    assert (feedback_prompt(order) is not None) is routed


def test_a_routing_flag_that_is_not_a_boolean_at_all_is_reported(caplog) -> None:
    """Falling back to the auction is the safe reading; doing it silently is not."""
    with caplog.at_level("WARNING"):
        routing({"order_ref": "o", "auction_id": "a", "routed": {"nested": "object"}})
    assert any("not a boolean in any spelling" in record.message for record in caplog.records)


@pytest.mark.parametrize("absent", [None, ""])
def test_an_absent_flag_is_not_reported_as_malformed(absent, caplog) -> None:
    with caplog.at_level("WARNING"):
        routing({"order_ref": "o", "auction_id": "a", "routed": absent})
    assert caplog.records == []


def test_an_order_whose_own_comparison_raises_cannot_raise_out_of_the_gate() -> None:
    """`_reading` promises nothing escapes; the routing scan must not break that promise.

    A `raw != ""` check added to the flag scan called the caller's `__ne__` and propagated
    whatever it raised straight out of `feedback_prompt`, turning a domain question into a
    `ZeroDivisionError`. The scan now decides by TYPE.
    """

    class Explosive:
        def __eq__(self, other):
            raise ZeroDivisionError("comparison")

        __ne__ = __eq__
        __hash__ = None  # type: ignore[assignment]

    order = {"order_ref": "o", "store_id": "s", "auction_id": "a", "routed": Explosive()}
    assert feedback_prompt(order) is None
    assert routing(order).routed is False


def test_a_hostile_record_that_raises_on_every_read_is_simply_un_routed() -> None:
    """Every field read goes through `_reading.read`, which cannot raise."""

    class Hostile:
        def __getattr__(self, name):
            raise ZeroDivisionError(name)

    class HostileMapping(dict):
        def get(self, key, default=None):
            raise RuntimeError(key)

    for record in (Hostile(), HostileMapping({"order_ref": "o", "auction_id": "a"})):
        assert feedback_prompt(record) is None
        assert routing(record).routed is False
