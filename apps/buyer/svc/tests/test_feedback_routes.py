"""The HTTP half of R14, through the frozen entrypoint (T-073).

A unit test of ``feedback_prompt``/``submit_feedback`` proves the functions behave. It does not
prove they are *reachable*, and the swarm-loop reachability checker cannot follow a
``@router.post`` decorator hop — it reports a route handler as UNREACHED whether or not the app
serves it. So reachability is proved the only way that is actually evidence: a real request
through :func:`buyer_svc.main.create_app`, with a recording ledger sink on ``app.state``,
asserting on what that sink saw.

The wire boundary carries one guarantee the library cannot: ``routed`` is a ``StrictBool``, so
the JSON string ``"yes"`` is a 422 rather than a coerced ``True``. That coercion is not
hypothetical on this tree — measured at pydantic 2.13, it turned a body carrying no boolean into
a live auction on T-071's confirm route — and here it would be a feedback prompt for an order the
network never routed.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from apps.buyer.svc.src.feedback import reset_submitted

PROMPT = "/buyer/feedback/prompt"
SUBMIT = "/buyer/feedback"

ORDER = {
    "order_ref": "ord-http-101",
    "store_id": "st-1",
    "auction_id": "auc-http-3",
    "routed": True,
}
ANSWER = {"question_id": "matched_pitch", "choice": "yes_as_described"}


class RecordingSink:
    """Every event lands in ``.events``."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def append(self, event) -> None:
        self.events.append(event)


@pytest.fixture()
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture()
def client(sink):
    reset_submitted()
    main = importlib.import_module("buyer_svc.main")
    app = main.create_app()
    app.state.ledger_sink = sink
    with TestClient(app) as test_client:
        yield test_client
    reset_submitted()


@pytest.fixture()
def sinkless_client():
    """An app whose composition root forgot to wire a ledger sink."""
    reset_submitted()
    main = importlib.import_module("buyer_svc.main")
    with TestClient(main.create_app()) as test_client:
        yield test_client
    reset_submitted()


# --- reachability ---------------------------------------------------------------------------


def test_the_feedback_router_is_mounted(client) -> None:
    assert "buyer_svc.feedback.routes" in client.app.state.mounted_routers


def test_a_routed_order_is_offered_one_prompt_over_http(client) -> None:
    response = client.post(PROMPT, json={"order": ORDER})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["offered"] is True
    assert body["reason"] == ""
    prompt = body["prompt"]
    assert isinstance(prompt, dict), "one prompt, not a collection"
    assert prompt["question_id"] == "matched_pitch"
    assert prompt["question"].strip()
    assert len(prompt["options"]) >= 2
    assert prompt["order_ref"] == "ord-http-101"


def test_an_unrouted_order_is_answered_two_hundred_with_no_prompt(client) -> None:
    """ "Is there a prompt?" has two correct answers, and neither of them is an error."""
    response = client.post(PROMPT, json={"order": dict(ORDER, routed=False)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["offered"] is False
    assert body["prompt"] is None
    assert "did not route" in body["reason"]


def test_a_submission_reaches_the_ledger_sink_over_http(client, sink) -> None:
    response = client.post(SUBMIT, json={"order": ORDER, "response": ANSWER})
    assert response.status_code == 201, response.text

    assert len(sink.events) == 1
    event = sink.events[0]
    assert str(event.kind) == "feedback"
    assert event.order_ref == "ord-http-101"
    assert event.payload == {"matched_pitch": True, "reason": "yes_as_described"}

    body = response.json()
    assert body["event_id"] == event.event_id
    assert body["kind"] == "feedback"
    assert body["matched_pitch"] is True
    assert body["reason"] == "yes_as_described"


# --- the wire boundary ----------------------------------------------------------------------


@pytest.mark.parametrize("routed", ["yes", "true", "on", "1", 1, "false", 0])
def test_a_non_boolean_routed_flag_is_refused_at_the_wire(client, sink, routed) -> None:
    """Pydantic lax mode would coerce four of these to True. StrictBool answers 422."""
    prompt = client.post(PROMPT, json={"order": dict(ORDER, routed=routed)})
    assert prompt.status_code == 422, prompt.text

    submit = client.post(SUBMIT, json={"order": dict(ORDER, routed=routed), "response": ANSWER})
    assert submit.status_code == 422, submit.text
    assert sink.events == []


def test_a_body_with_no_order_ref_is_refused(client) -> None:
    assert client.post(PROMPT, json={"order": {"store_id": "s"}}).status_code == 422


def test_an_extra_field_on_the_order_is_carried_not_truncated(client, sink) -> None:
    """A real order record has more on it; the route's answer must match the library's."""
    response = client.post(
        PROMPT, json={"order": dict(ORDER, checkout_token="tok-1", total_price="118.00")}
    )
    assert response.status_code == 200
    assert response.json()["offered"] is True


def test_the_response_body_never_echoes_the_order_record(client, sink) -> None:
    fat = dict(ORDER, email="dana.reyes@example.com", first_name="Dana", notes="x" * 10_000)
    response = client.post(SUBMIT, json={"order": fat, "response": ANSWER})
    assert response.status_code == 201
    blob = response.text.lower()
    for secret in ("dana", "reyes", "example.com", "xxxxx"):
        assert secret not in blob


# --- refusals on the wire -------------------------------------------------------------------


def test_feedback_for_an_unrouted_order_is_a_403_and_reaches_no_sink(client, sink) -> None:
    response = client.post(SUBMIT, json={"order": dict(ORDER, routed=False), "response": ANSWER})
    assert response.status_code == 403, response.text
    assert "did not route" in response.json()["detail"]["reason"]
    assert sink.events == []


def test_a_free_text_answer_is_a_422_and_reaches_no_sink(client, sink) -> None:
    response = client.post(
        SUBMIT, json={"order": ORDER, "response": {"choice": "it was fine, I'm Dana"}}
    )
    assert response.status_code == 422, response.text
    assert "yes_as_described" in response.json()["detail"]["options"]
    assert "Dana" not in response.text, "a free-text answer must not be echoed back"
    assert sink.events == []


def test_a_second_submission_is_a_409_carrying_the_first_event_id(client, sink) -> None:
    first = client.post(SUBMIT, json={"order": ORDER, "response": ANSWER})
    assert first.status_code == 201

    second = client.post(SUBMIT, json={"order": ORDER, "response": {"choice": "wrong_item"}})
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["event_id"] == first.json()["event_id"]
    assert len(sink.events) == 1


def test_a_deployment_with_no_ledger_sink_answers_503(sinkless_client) -> None:
    """R14 promises the submission LANDS. Nowhere to land is a 503, never a quiet 201."""
    response = sinkless_client.post(SUBMIT, json={"order": ORDER, "response": ANSWER})
    assert response.status_code == 503, response.text


def test_asking_for_a_prompt_needs_no_ledger_sink(sinkless_client) -> None:
    """`/prompt` cannot write, and this is the evidence: it works with no sink at all."""
    response = sinkless_client.post(PROMPT, json={"order": ORDER})
    assert response.status_code == 200
    assert response.json()["offered"] is True


def test_a_bad_session_header_is_a_401_and_reaches_no_sink(client, sink) -> None:
    response = client.post(
        SUBMIT,
        json={"order": ORDER, "response": ANSWER},
        headers={"X-Buyer-Session": "not-a-session"},
    )
    assert response.status_code == 401, response.text
    assert sink.events == []


def test_no_session_header_is_accepted_as_it_is_on_the_accept_route(client, sink) -> None:
    """Nothing in this repo logs a buyer in before a shortlist; see this ticket's NEEDS."""
    assert client.post(SUBMIT, json={"order": ORDER, "response": ANSWER}).status_code == 201
    assert len(sink.events) == 1
