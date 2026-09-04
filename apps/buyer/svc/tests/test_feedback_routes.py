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


@pytest.mark.parametrize("field", ["order_ref", "store_id", "auction_id", "buyer_pseudonym"])
def test_an_unbounded_reference_is_refused_at_the_wire(client, sink, field) -> None:
    """MEASURED before the bound: a 100 000-character order_ref was accepted and echoed.

    It would have been copied verbatim onto an append-only ledger event, where nothing
    downstream can shorten it again.
    """
    order = dict(ORDER, **{field: "x" * 100_000})
    assert client.post(PROMPT, json={"order": order}).status_code == 422
    assert client.post(SUBMIT, json={"order": order, "response": ANSWER}).status_code == 422
    assert sink.events == []


def test_a_reference_of_a_realistic_length_is_still_accepted(client) -> None:
    """A Shopify order GID is about forty characters; the bound must not be in its way."""
    long_but_real = "gid://shopify/Order/4435291300000"
    response = client.post(PROMPT, json={"order": dict(ORDER, order_ref=long_but_real)})
    assert response.status_code == 200
    assert response.json()["prompt"]["order_ref"] == long_but_real


def test_no_body_this_route_accepts_produces_a_5xx(client) -> None:
    """Every refusal must name a caller-facing status. A 500 blames us for their request."""
    bodies = [
        {},
        {"order": None},
        {"order": []},
        {"order": "x"},
        {"order": {"order_ref": ""}},
        {"order": dict(ORDER, status=["cancelled"])},
        {"order": dict(ORDER, auction_id=None)},
        {"order": ORDER, "response": None},
        {"order": ORDER, "response": []},
        {"order": ORDER, "response": {"choice": None}},
        {"order": ORDER, "response": {"choice": {"nested": 1}}},
    ]
    for body in bodies:
        for path in (PROMPT, SUBMIT):
            assert client.post(path, json=body).status_code < 500, (path, body)


class FlakySink:
    """Records and THEN raises: an at-least-once client whose acknowledgement was lost."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def append(self, event) -> None:
        self.events.append(event)
        raise TimeoutError("could not connect: dsn=postgres://svc:hunter2@ledger.internal/db")


def test_an_order_that_does_not_say_it_was_routed_is_offered_no_prompt(client, sink) -> None:
    """MEASURED before the gate failed closed: this was 200 offered=true, then a 201.

    `routed` is not required by the wire model, and `auction_id` is entirely caller-supplied,
    so an order body that simply omitted the flag walked through R14's gate over HTTP.
    """
    unstated = {"order_ref": "ord-http-9", "store_id": "st-1", "auction_id": "auc-http-9"}

    prompt = client.post(PROMPT, json={"order": unstated})
    assert prompt.status_code == 200
    assert prompt.json()["offered"] is False
    assert "not marked as network-routed" in prompt.json()["reason"]

    submit = client.post(SUBMIT, json={"order": unstated, "response": ANSWER})
    assert submit.status_code == 403, submit.text
    assert sink.events == []


@pytest.mark.parametrize("field", ["routed", "network_routed", "routed_by_network"])
@pytest.mark.parametrize("lax", ["yes", "true", "on", "1"])
def test_every_routing_spelling_is_strict_not_just_the_declared_one(
    client, sink, field, lax
) -> None:
    """MEASURED: `{"network_routed": "yes"}` was 200 offered=true while `{"routed": "yes"}` was 422.

    `model_config` allows extras, so declaring `StrictBool` on one spelling left the other two
    arriving untyped and being read by the library's lenient `flag()`. A strict boundary with a
    door beside it is not a strict boundary.
    """
    order = {"order_ref": "ord-http-8", "auction_id": "auc-http-8", field: lax}
    assert client.post(PROMPT, json={"order": order}).status_code == 422
    assert client.post(SUBMIT, json={"order": order, "response": ANSWER}).status_code == 422
    assert sink.events == []


@pytest.mark.parametrize(
    "ref",
    [
        "Dana Reyes dana.reyes@example.com 555-0134 the seller lied to me",
        "dana.reyes@example.com",
        "44 Alder Way, Portland OR 97205",
    ],
)
def test_prose_cannot_reach_the_ledger_through_an_order_reference(client, sink, ref) -> None:
    """MEASURED: `order_ref` had a length bound and no shape, so it was a free-text field.

    It is copied verbatim onto an append-only `LedgerEvent` and into this service's logs, under
    a kind whose whole point is that it carries no prose, and nothing downstream can remove it.
    """
    order = dict(ORDER, order_ref=ref)
    assert client.post(SUBMIT, json={"order": order, "response": ANSWER}).status_code == 422
    assert client.post(PROMPT, json={"order": order}).status_code == 422
    assert sink.events == []


@pytest.mark.parametrize(
    "ref", ["ord-e7-101", "#1001", "gid://shopify/Order/4435291300000", "8f14e45f_ea67"]
)
def test_a_real_order_reference_is_still_accepted(client, ref) -> None:
    """The shape must not be in the way of what a reference actually looks like."""
    response = client.post(PROMPT, json={"order": dict(ORDER, order_ref=ref)})
    assert response.status_code == 200, response.text
    assert response.json()["prompt"]["order_ref"] == ref


def test_a_ledger_that_raises_is_a_503_with_a_retry_id_and_never_a_500(sinkless_client) -> None:
    """MEASURED: any sink exception was an unhandled 500 with the sink's own message in it."""
    flaky = FlakySink()
    sinkless_client.app.state.ledger_sink = flaky

    response = sinkless_client.post(SUBMIT, json={"order": ORDER, "response": ANSWER})
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["retry_with_event_id"] is True
    assert detail["event_id"] == flaky.events[0].event_id
    assert "postgres://" not in response.text and "hunter2" not in response.text


def test_a_blind_retry_after_an_uncertain_write_is_a_409_not_a_second_event(
    sinkless_client,
) -> None:
    """The write may have landed. A second submission would be a second trust observation."""
    flaky = FlakySink()
    sinkless_client.app.state.ledger_sink = flaky

    first = sinkless_client.post(SUBMIT, json={"order": ORDER, "response": ANSWER})
    assert first.status_code == 503

    second = sinkless_client.post(SUBMIT, json={"order": ORDER, "response": ANSWER})
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["event_id"] == flaky.events[0].event_id
    assert len({event.event_id for event in flaky.events}) == 1
