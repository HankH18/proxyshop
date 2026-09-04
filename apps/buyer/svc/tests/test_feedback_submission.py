"""R14: one submission, one `feedback` LedgerEvent, in the published shape (T-073).

The frozen acceptance test proves the happy path lands. These prove the things that must NOT
land: an un-routed order, a second submission, a free-text answer, an answer that contradicts
itself, and an event whose body is not the one ``LEDGER_PAYLOAD_SHAPES`` publishes.
"""

from __future__ import annotations

import json
import re

import pytest
from contracts import LEDGER_PAYLOAD_SHAPES, LedgerEvent, validate_ledger_payload

from apps.buyer.svc.src.feedback import (
    CHOICE_IDS,
    FEEDBACK_CHOICES,
    ContradictoryFeedback,
    FeedbackAlreadySubmitted,
    FeedbackError,
    FeedbackLedger,
    FeedbackNotYours,
    LedgerSinkUnusable,
    MissingFeedbackChoice,
    OrderNotRouted,
    UnknownFeedbackChoice,
    UnknownFeedbackQuestion,
    UnusableOrder,
    feedback_payload,
    reset_submitted,
    submit_feedback,
)

ORDER = {
    "order_ref": "ord-unit-101",
    "store_id": "st-1",
    "auction_id": "auc-unit-3",
    "routed": True,
}
ANSWER = {"question_id": "matched_pitch", "choice": "yes_as_described"}

#: The identity-shaped keys the frozen suite forbids anywhere inside the event.
IDENTITY_KEY_RE = re.compile(
    r"e[-_ ]?mail|phone|first[_ -]?name|last[_ -]?name|full[_ -]?name|"
    r"given[_ -]?name|surname|address|street|postal|zip[_ -]?code|"
    r"account[_ -]?id|customer[_ -]?id|buyer[_ -]?id|user[_ -]?id|"
    r"identity|ip[_ -]?address|device[_ -]?id",
    re.IGNORECASE,
)


class Sink:
    """Records every event handed to it, and how many times it was called."""

    def __init__(self) -> None:
        self.events: list[LedgerEvent] = []

    def append(self, event) -> None:
        self.events.append(event)


class ExplodingSink:
    def __init__(self) -> None:
        self.calls = 0

    def append(self, event):
        self.calls += 1
        raise ConnectionError("the ledger is down")


@pytest.fixture()
def ledger() -> FeedbackLedger:
    """A ledger of this test's own, so nothing here depends on process history."""
    return FeedbackLedger()


@pytest.fixture(autouse=True)
def _clean_process_ledger():
    reset_submitted()
    yield
    reset_submitted()


def _walk(value):
    if isinstance(value, dict):
        for key, sub in value.items():
            yield str(key), sub
            yield from _walk(sub)
    elif isinstance(value, (list, tuple)):
        for sub in value:
            yield from _walk(sub)


# --- the event ----------------------------------------------------------------------------


def test_one_submission_emits_exactly_one_feedback_event(ledger) -> None:
    sink = Sink()
    event = submit_feedback(ORDER, ANSWER, sink, ledger=ledger)

    assert len(sink.events) == 1
    assert sink.events[0] is event
    assert isinstance(event, LedgerEvent)
    assert str(event.kind) == "feedback"
    assert event.order_ref == "ord-unit-101"
    assert event.store_id == "st-1"
    assert event.auction_id == "auc-unit-3"
    assert event.event_id.strip()
    assert event.ts.endswith("Z")
    assert event.prev_hash is None, "the buyer does not chain the ledger"


def test_the_payload_is_the_published_body_for_the_feedback_kind(ledger) -> None:
    """T-235's defect class, checked here: one kind must not grow a second body."""
    event = submit_feedback(ORDER, ANSWER, Sink(), ledger=ledger)

    assert set(event.payload) == set(LEDGER_PAYLOAD_SHAPES["feedback"])
    assert validate_ledger_payload("feedback", event.payload) == []
    assert event.payload == {"matched_pitch": True, "reason": "yes_as_described"}


def test_a_payload_missing_a_published_key_would_be_caught_by_the_validator() -> None:
    """The check `submit_feedback` runs is a real one: prove it FAILS on a wrong body."""
    assert validate_ledger_payload("feedback", {"matched_pitch": True}) != []
    assert validate_ledger_payload("feedback", {"reason": "late"}) != []
    assert validate_ledger_payload("feedback", {}) != []


@pytest.mark.parametrize("choice", FEEDBACK_CHOICES, ids=lambda c: c.id)
def test_every_published_option_produces_a_valid_event(choice, ledger) -> None:
    sink = Sink()
    event = submit_feedback(
        dict(ORDER, order_ref=f"ord-{choice.id}"), {"choice": choice.id}, sink, ledger=ledger
    )
    assert event.payload == {"matched_pitch": choice.matched_pitch, "reason": choice.id}
    assert validate_ledger_payload("feedback", event.payload) == []


def test_no_identity_shaped_key_reaches_the_event(ledger) -> None:
    order = dict(
        ORDER,
        email="dana.reyes@example.com",
        first_name="Dana",
        address="44 Alder Way",
        account_id="acct-9f3c21",
    )
    event = submit_feedback(order, ANSWER, Sink(), ledger=ledger)
    dumped = event.model_dump()
    for key, _value in _walk(dumped):
        assert not IDENTITY_KEY_RE.search(key), key
    blob = json.dumps(dumped, default=str).lower()
    for secret in ("dana", "reyes", "example.com", "alder way", "acct-9f3c21"):
        assert secret not in blob


# --- the R14 gate -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "order",
    [
        {"order_ref": "o", "store_id": "s", "routed": False},
        {"order_ref": "o", "store_id": "s", "auction_id": "a", "routed": False},
        {"order_ref": "o", "store_id": "s", "auction_id": "a", "routed": "false"},
        {"order_ref": "o", "store_id": "s"},
        {"order_ref": "o", "store_id": "s", "routed": True},
    ],
)
def test_an_unrouted_order_emits_nothing(order, ledger) -> None:
    sink = Sink()
    with pytest.raises(OrderNotRouted) as caught:
        submit_feedback(order, ANSWER, sink, ledger=ledger)
    assert sink.events == [], "R14's gate must refuse BEFORE anything reaches the ledger"
    assert caught.value.reason


def test_a_cancelled_order_may_still_have_its_answer_recorded(ledger) -> None:
    """The prompt is a display decision; a cancellation mid-answer must not eat the answer."""
    sink = Sink()
    event = submit_feedback(dict(ORDER, status="cancelled"), ANSWER, sink, ledger=ledger)
    assert len(sink.events) == 1 and event.order_ref == "ord-unit-101"


def test_an_order_with_no_reference_is_refused(ledger) -> None:
    sink = Sink()
    with pytest.raises(UnusableOrder):
        submit_feedback(
            {"store_id": "s", "auction_id": "a", "routed": True}, ANSWER, sink, ledger=ledger
        )
    assert sink.events == []


def test_feedback_about_someone_elses_order_is_refused(ledger) -> None:
    sink = Sink()
    order = dict(ORDER, buyer_pseudonym="psn-aaa")
    with pytest.raises(FeedbackNotYours):
        submit_feedback(order, ANSWER, sink, buyer_pseudonym="psn-bbb", ledger=ledger)
    assert sink.events == []
    assert submit_feedback(order, ANSWER, sink, buyer_pseudonym="psn-aaa", ledger=ledger)


def test_an_unbindable_order_is_recorded_with_a_warning_not_refused(ledger, caplog) -> None:
    """Today's order shape names no buyer; refusing every one of them would ship nothing."""
    with caplog.at_level("WARNING"):
        submit_feedback(ORDER, ANSWER, Sink(), buyer_pseudonym="psn-aaa", ledger=ledger)
    assert any("ownership was NOT checked" in record.message for record in caplog.records)


# --- structured answers only ---------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        {"question_id": "matched_pitch", "choice": "it was fine I guess"},
        {"choice": "yes"},
        {"choice": "YES_AS_DESCRIBED"},
        {"choice": "yes_as_described "},  # stripped, so this one is FINE — see below
    ],
)
def test_only_a_published_option_may_be_recorded(response, ledger) -> None:
    sink = Sink()
    if response["choice"].strip() in CHOICE_IDS:
        assert submit_feedback(ORDER, response, sink, ledger=ledger)
        return
    with pytest.raises(UnknownFeedbackChoice):
        submit_feedback(ORDER, response, sink, ledger=ledger)
    assert sink.events == []


def test_a_free_text_answer_cannot_become_a_ledger_row(ledger) -> None:
    """The prompt has no text box; this is the check that a client cannot add one."""
    essay = "The shoes were lovely but my name is Dana Reyes, dana@example.com" * 200
    sink = Sink()
    with pytest.raises(UnknownFeedbackChoice) as caught:
        submit_feedback(ORDER, {"choice": essay}, sink, ledger=ledger)
    assert sink.events == []
    assert len(str(caught.value)) < 1_000, "an enormous body must not become an enormous refusal"
    assert "dana@example.com" not in str(caught.value)


@pytest.mark.parametrize("response", [{}, {"question_id": "matched_pitch"}, None, "yes", 3])
def test_a_response_that_names_no_choice_is_refused(response, ledger) -> None:
    sink = Sink()
    with pytest.raises(MissingFeedbackChoice):
        submit_feedback(ORDER, response, sink, ledger=ledger)
    assert sink.events == []


def test_a_response_answering_a_different_question_is_refused(ledger) -> None:
    with pytest.raises(UnknownFeedbackQuestion):
        submit_feedback(
            ORDER,
            {"question_id": "would_you_recommend", "choice": "yes_as_described"},
            Sink(),
            ledger=ledger,
        )


def test_a_response_whose_verdict_contradicts_its_choice_is_refused(ledger) -> None:
    with pytest.raises(ContradictoryFeedback):
        submit_feedback(
            ORDER, {"matched_pitch": False, "choice": "yes_as_described"}, Sink(), ledger=ledger
        )
    with pytest.raises(ContradictoryFeedback):
        submit_feedback(
            ORDER, {"matched_pitch": "yes", "choice": "wrong_item"}, Sink(), ledger=ledger
        )
    # An agreeing pair is fine, and so is a matched_pitch this package cannot read.
    assert submit_feedback(
        ORDER, {"matched_pitch": True, "choice": "yes_as_described"}, Sink(), ledger=ledger
    )


def test_the_trust_services_own_response_spelling_round_trips(ledger) -> None:
    """`trust.feedback.engine` reads `matched_pitch` off the response; ours must agree."""
    from apps.trust.src.feedback import accept_feedback

    event = submit_feedback(ORDER, ANSWER, Sink(), ledger=ledger)
    verdict = accept_feedback(
        event.order_ref,
        event.payload,
        routed_orders={"ord-unit-101": {"store_id": "st-1", "returned": False}},
    )
    assert verdict["accepted"] is True
    assert verdict["positive"] is True


# --- once per order ------------------------------------------------------------------------


def test_a_second_submission_for_one_order_is_refused(ledger) -> None:
    sink = Sink()
    first = submit_feedback(ORDER, ANSWER, sink, ledger=ledger)
    with pytest.raises(FeedbackAlreadySubmitted) as caught:
        submit_feedback(ORDER, {"choice": "wrong_item"}, sink, ledger=ledger)
    assert len(sink.events) == 1, "a duplicate must not become a second trust observation"
    assert caught.value.event_id == first.event_id


def test_a_failed_submission_can_honestly_be_retried(ledger) -> None:
    """A claim that was never spent must be given back, or one outage is permanent."""
    broken = ExplodingSink()
    with pytest.raises(ConnectionError):
        submit_feedback(ORDER, ANSWER, broken, ledger=ledger)
    assert broken.calls == 1
    assert ledger.event_for("ord-unit-101") is None

    good = Sink()
    assert submit_feedback(ORDER, ANSWER, good, ledger=ledger)
    assert len(good.events) == 1


def test_a_refused_answer_does_not_burn_the_orders_one_submission(ledger) -> None:
    with pytest.raises(UnknownFeedbackChoice):
        submit_feedback(ORDER, {"choice": "nonsense"}, Sink(), ledger=ledger)
    assert ledger.event_for("ord-unit-101") is None
    assert submit_feedback(ORDER, ANSWER, Sink(), ledger=ledger)


def test_two_different_orders_are_independent(ledger) -> None:
    sink = Sink()
    submit_feedback(ORDER, ANSWER, sink, ledger=ledger)
    submit_feedback(dict(ORDER, order_ref="ord-unit-102"), ANSWER, sink, ledger=ledger)
    assert len(sink.events) == 2


# --- the sink ------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["append", "emit", "record", "publish", "write", "log_event"])
def test_any_published_sink_method_is_used(method, ledger) -> None:
    seen: list[object] = []
    sink = type("S", (), {method: lambda self, event: seen.append(event)})()
    submit_feedback(ORDER, ANSWER, sink, ledger=ledger)
    assert len(seen) == 1


def test_a_bare_callable_sink_works(ledger) -> None:
    seen: list[object] = []
    submit_feedback(ORDER, ANSWER, seen.append, ledger=ledger)
    assert len(seen) == 1


def test_the_sink_is_called_with_one_positional_argument_and_no_keywords(ledger) -> None:
    """The frozen suite's sink collects kwarg VALUES too; two would look like two events."""
    calls: list[tuple] = []

    class Recording:
        def append(self, *args, **kwargs):
            calls.append((args, kwargs))

    submit_feedback(ORDER, ANSWER, Recording(), ledger=ledger)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert len(args) == 1 and kwargs == {}


@pytest.mark.parametrize("sink", [None, object(), "not-a-sink", 7])
def test_a_sink_that_cannot_record_is_a_refusal_not_a_shrug(sink, ledger) -> None:
    with pytest.raises(LedgerSinkUnusable):
        submit_feedback(ORDER, ANSWER, sink, ledger=ledger)


# --- the error hierarchy --------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: submit_feedback({"order_ref": "o", "routed": False}, ANSWER, Sink()),
        lambda: submit_feedback(ORDER, {"choice": "nope"}, Sink()),
        lambda: submit_feedback(ORDER, {}, Sink()),
        lambda: submit_feedback(ORDER, ANSWER, None),
        lambda: submit_feedback(None, ANSWER, Sink()),
    ],
)
def test_every_refusal_is_a_feedback_error_and_never_a_builtin(call) -> None:
    """A domain refusal must be distinguishable from a broken call. T-071/T-072's convention."""
    with pytest.raises(FeedbackError) as caught:
        call()
    assert not isinstance(caught.value, (TypeError, ValueError, KeyError, AttributeError))
    assert isinstance(caught.value, RuntimeError)


def test_feedback_payload_is_usable_on_its_own() -> None:
    assert feedback_payload({"choice": "never_arrived"}) == {
        "matched_pitch": False,
        "reason": "never_arrived",
    }
