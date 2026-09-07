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
    FEEDBACK_DIMENSION,
    FEEDBACK_NEGATIVE_TYPE,
    FEEDBACK_POSITIVE_TYPE,
    ContradictoryFeedback,
    FeedbackAlreadySubmitted,
    FeedbackError,
    FeedbackLedger,
    FeedbackNotYours,
    LedgerSinkUnusable,
    LedgerWriteUncertain,
    MalformedFeedbackEvent,
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
    """Raises BEFORE recording: the friendly half, where nothing landed."""

    def __init__(self) -> None:
        self.calls = 0

    def append(self, event):
        self.calls += 1
        raise ConnectionError("the ledger is down")


class FlakySink:
    """Records and THEN raises: an at-least-once client whose acknowledgement was lost.

    This is the shape that produced two `feedback` rows for one order. `ExplodingSink` above
    cannot reproduce it — it raises before appending, so it only ever exercises the half where
    the claim really is unspent.
    """

    def __init__(self, failures: int = 99) -> None:
        self.events: list[LedgerEvent] = []
        self.failures = failures

    def append(self, event) -> None:
        self.events.append(event)
        if self.failures > 0:
            self.failures -= 1
            raise TimeoutError("the write went out and the acknowledgement did not come back")


class LeakySink:
    """A sink whose exception message carries a credential, as real ones do."""

    def append(self, event):
        raise ConnectionError("could not connect: dsn=postgres://svc:hunter2@ledger.internal/db")


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
    """T-235's defect class, checked here: one kind must not grow a second body.

    The body is pinned EXACTLY, and it is four keys rather than two. The two published ones are
    still there and still validated; the other two are the trust routing (`dim`/`type`) without
    which `trust.ledger.replay.observations_from_events` projects this event into nothing at all
    and R14's loop does not exist — see `feedback_payload`. Extra keys are admitted by the
    contract by design ("a vendor body carries plenty"), so this is the published shape plus the
    routing, not a second body for one kind.
    """
    event = submit_feedback(ORDER, ANSWER, Sink(), ledger=ledger)

    assert set(LEDGER_PAYLOAD_SHAPES["feedback"]) <= set(event.payload)
    assert validate_ledger_payload("feedback", event.payload) == []
    assert event.payload == {
        "matched_pitch": True,
        "reason": "yes_as_described",
        "dim": "feedback_match",
        "type": "fulfilled",
    }


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
    assert event.payload == {
        "matched_pitch": choice.matched_pitch,
        "reason": choice.id,
        "dim": FEEDBACK_DIMENSION,
        "type": FEEDBACK_POSITIVE_TYPE if choice.matched_pitch else FEEDBACK_NEGATIVE_TYPE,
    }
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


def test_a_sink_that_wrote_and_then_raised_cannot_be_blindly_retried(ledger) -> None:
    """THE regression test. Measured: this produced two `feedback` rows for one order.

    An at-least-once ledger client whose write commits and whose acknowledgement is then lost
    raises with the row already on the ledger. The old code released the claim on any sink
    exception ("nothing landed, so the claim was not spent"), so the next honest retry minted a
    second event id and wrote a second row — the doubled `feedback_match` observation this whole
    package exists to prevent, produced by the recovery path.
    """
    landed = FlakySink()

    with pytest.raises(LedgerWriteUncertain) as caught:
        submit_feedback(ORDER, ANSWER, landed, ledger=ledger)
    first_id = caught.value.event_id
    assert first_id and landed.events[0].event_id == first_id, "the write did land"
    assert ledger.event_for("ord-unit-101") == first_id, "the claim must still stand"
    assert ledger.landed("ord-unit-101") is False, "...and must not claim it landed"

    # A blind retry — a fresh event id — is refused. This is the line that was red before.
    with pytest.raises(FeedbackAlreadySubmitted):
        submit_feedback(ORDER, ANSWER, landed, ledger=ledger)
    assert len({event.event_id for event in landed.events}) == 1, (
        f"two distinct feedback events for one order: {[event.event_id for event in landed.events]}"
    )


def test_a_deliberate_retry_reuses_the_event_id_so_the_ledger_writes_one_row(ledger) -> None:
    """ "Retryable" and "safe to retry" differ, and only the caller holding the id can tell."""
    flaky = FlakySink(failures=1)  # the first ack is lost; the second attempt is acknowledged
    with pytest.raises(LedgerWriteUncertain) as caught:
        submit_feedback(ORDER, ANSWER, flaky, ledger=ledger)
    event_id = caught.value.event_id

    again = submit_feedback(ORDER, ANSWER, flaky, ledger=ledger, event_id=event_id)
    assert again.event_id == event_id, "the retry must write the SAME row, not a second one"
    assert len({event.event_id for event in flaky.events}) == 1
    assert ledger.landed("ord-unit-101") is True

    # And once it has definitely landed, even the same id is refused.
    with pytest.raises(FeedbackAlreadySubmitted):
        submit_feedback(ORDER, ANSWER, flaky, ledger=ledger, event_id=event_id)


def test_the_sinks_own_exception_message_never_travels(ledger) -> None:
    """A ledger client's error text routinely names a host or a DSN; this one is rendered
    into an HTTP response body by the route above."""
    with pytest.raises(LedgerWriteUncertain) as caught:
        submit_feedback(ORDER, ANSWER, LeakySink(), ledger=ledger)
    assert "postgres://" not in str(caught.value)
    assert "hunter2" not in str(caught.value)
    assert isinstance(caught.value.__cause__, ConnectionError), "still chained for the logs"


def test_a_sink_that_could_never_have_written_gives_the_claim_back(ledger) -> None:
    """The one path where "nothing landed" is knowable: there was nothing to write with."""
    with pytest.raises(LedgerSinkUnusable):
        submit_feedback(ORDER, ANSWER, object(), ledger=ledger)
    assert ledger.event_for("ord-unit-101") is None

    good = Sink()
    assert submit_feedback(ORDER, ANSWER, good, ledger=ledger)
    assert len(good.events) == 1


def test_a_refusal_for_an_in_flight_claim_names_the_event_it_is_waiting_on(ledger) -> None:
    """Measured before the fix: the 409 carried `event_id: ""` for feedback nobody could find."""
    flaky = FlakySink()
    with pytest.raises(LedgerWriteUncertain):
        submit_feedback(ORDER, ANSWER, flaky, ledger=ledger)
    with pytest.raises(FeedbackAlreadySubmitted) as caught:
        submit_feedback(ORDER, ANSWER, Sink(), ledger=ledger)
    assert caught.value.event_id == flaky.events[0].event_id


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


def test_a_body_that_is_not_the_published_shape_is_refused_at_the_producing_boundary(
    ledger, monkeypatch
) -> None:
    """The validator call is real code, not ceremony — this is the test that says so.

    MEASURED before this test existed: replacing `validate_ledger_payload` with a stub that
    returns `[]` left the WHOLE buyer suite green (501 passed, identical to baseline), so the
    check at the producing boundary — the one T-235 records as missing on the exchange's
    `code_created` path — was itself ungraded.
    """
    from apps.buyer.svc.src.feedback import submission as module

    monkeypatch.setattr(
        module, "validate_ledger_payload", lambda kind, payload: ["missing published key 'x'"]
    )
    sink = Sink()
    with pytest.raises(MalformedFeedbackEvent) as caught:
        submit_feedback(ORDER, ANSWER, sink, ledger=ledger)
    assert sink.events == [], "a malformed body must be refused BEFORE it reaches the ledger"
    assert caught.value.problems == ("missing published key 'x'",)
    assert ledger.event_for("ord-unit-101") is None, "and must not burn the order's submission"


def test_a_broken_call_still_raises_the_builtin_it_is(ledger) -> None:
    """The other half of the error convention, and the half a FeedbackError check cannot see.

    `pytest.raises(FeedbackError)` succeeding already proves the refusal is not a TypeError,
    so asserting that afterwards proves nothing. What is worth asserting is the converse: a
    genuinely broken call must NOT be dressed up as a domain refusal.
    """
    with pytest.raises(TypeError):
        submit_feedback(ORDER, ANSWER)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        submit_feedback(ORDER, ANSWER, Sink(), nonsense=1)  # type: ignore[call-arg]


def test_no_caller_supplied_prose_reaches_a_refusal_message(ledger) -> None:
    """Every refusal here is rendered into an HTTP body and a log line by the route above."""
    prose = "the seller lied to me, I'm Dana Reyes, dana.reyes@example.com, 555-0134"

    with pytest.raises(FeedbackError) as bad_response:
        submit_feedback(ORDER, prose, Sink(), ledger=ledger)
    with pytest.raises(FeedbackError) as bad_order:
        submit_feedback(prose, ANSWER, Sink(), ledger=ledger)
    with pytest.raises(FeedbackError) as bad_choice:
        submit_feedback(ORDER, {"choice": prose}, Sink(), ledger=ledger)
    with pytest.raises(FeedbackError) as bad_question:
        submit_feedback(
            ORDER, {"question_id": prose, "choice": "wrong_item"}, Sink(), ledger=ledger
        )

    for caught in (bad_response, bad_order, bad_choice, bad_question):
        message = str(caught.value)
        for secret in ("Dana", "Reyes", "example.com", "555-0134", "lied"):
            assert secret not in message, f"{secret!r} leaked into {message!r}"


def test_a_name_shaped_choice_is_not_quoted_back_but_a_typod_option_id_is(ledger) -> None:
    """`_ECHOABLE` is the option-id shape, not "short enough": `Dana_Reyes_1985` was echoed."""
    with pytest.raises(UnknownFeedbackChoice) as named:
        submit_feedback(ORDER, {"choice": "Dana_Reyes_1985"}, Sink(), ledger=ledger)
    assert "Dana" not in str(named.value)

    with pytest.raises(UnknownFeedbackChoice) as typo:
        submit_feedback(ORDER, {"choice": "yes_as_describd"}, Sink(), ledger=ledger)
    assert "yes_as_describd" in str(typo.value), "a typo'd id is exactly what is worth echoing"


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
        "dim": FEEDBACK_DIMENSION,
        "type": FEEDBACK_NEGATIVE_TYPE,
    }
