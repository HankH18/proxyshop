"""R14's loop closes: a shopper's answer reaches a trust service and moves a store (R14/R12).

WHAT WAS MEASURED, AND WHY THIS FILE EXISTS
--------------------------------------------
``POST /buyer/feedback`` reads its sink from ``app.state.ledger_sink``
(``feedback/routes.py``'s ``LEDGER_SINK_ATTR``) and **no composition root anywhere in this
repository set it**. Measured on the app exactly as ``buyer_svc.main.create_app()`` builds
it, with no test wiring at all::

    POST /buyer/feedback {"order": {...routed...}, "response": {...}}
        -> 503 {"detail": "submit_feedback() was given no ledger sink, ..."}

So the one channel in the whole system where a human grades a *pitch* answered 503 in every
deployment. ``apps/buyer/svc/tests/test_feedback_routes.py`` is green because its ``client``
fixture writes ``app.state.ledger_sink = RecordingSink()`` itself — a test that wires the app
it is testing measures the wiring it wrote.

THE SECOND HALF, WHICH IS WORSE BECAUSE IT SURVIVES THE FIRST
---------------------------------------------------------------
Even with a sink, the event landed on the ledger and produced **zero** trust observations.
``trust.ledger.replay.observations_from_events`` — the one projection the trust scorer,
``GET /events/replay?snapshots=true`` and ``POST /events``' own poison check all run — makes
an observation only from an event whose ``payload`` names both a ``dim`` and a ``type``. The
``feedback`` payload named neither::

    >>> observations_from_events([{"kind": "feedback", "store_id": "st-1",
    ...                            "payload": {"matched_pitch": True,
    ...                                        "reason": "yes_as_described"}}])
    []

The translation lives on the EMITTER side in this repository, by precedent and by argument:
``trust.reconcile.engine.observation_events``' own docstring says so — "these events are
shaped to what that consumer already requires, rather than the consumer being asked to learn
what a ``reconciled`` payload means". The buyer service is the emitter of ``feedback``.

WHAT IS DRIVEN HERE
--------------------
A real HTTP server stands in for ``apps/trust``'s published ``POST /events`` and records what
arrives, so every assertion below is about **bytes that left the buyer process** rather than
about a double someone handed the app. ``TRUST_URL`` names it, which is the same variable
``apps/buyer/compose.yaml`` already forwards to this service and which nothing in
``apps/buyer`` read before this.
"""

from __future__ import annotations

import importlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.buyer.svc.src.feedback import reset_submitted

SUBMIT = "/buyer/feedback"
PROMPT = "/buyer/feedback/prompt"

ORDER = {
    "order_ref": "ord-r14-wiring-1",
    "store_id": "st-r14-1",
    "auction_id": "auc-r14-1",
    "routed": True,
}
ANSWER = {"question_id": "matched_pitch", "choice": "yes_as_described"}
DISAPPOINTED = {"question_id": "matched_pitch", "choice": "not_as_described"}


class TrustDouble:
    """A real socket answering ``POST /events`` the way ``apps/trust`` answers it.

    Not a stubbed client object: the whole point of this file is that the bytes leave the
    buyer process, so the publisher, the URL resolution, the timeout and the JSON body are all
    exercised. ``received`` holds ``(path, parsed body)`` per request, in order.
    """

    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.received: list[tuple[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except Exception:  # noqa: BLE001 - an unparseable body is a finding, not a crash
                    parsed = {"_unparseable": raw.decode("utf-8", "replace")}
                outer.received.append((self.path, parsed))
                body = json.dumps({"inserted": True, "event": parsed}).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # keep pytest output readable
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> TrustDouble:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _dead_port() -> int:
    """A port nothing is listening on: bound to learn the number, then closed."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def trust_double():
    with TrustDouble() as double:
        yield double


def _served_app(monkeypatch, trust_url: str) -> TestClient:
    """The app ``create_app()`` builds, with NOTHING wired onto ``app.state`` by this test."""
    monkeypatch.setenv("TRUST_URL", trust_url)
    main = importlib.import_module("buyer_svc.main")
    return TestClient(main.create_app())


@pytest.fixture()
def served(monkeypatch, trust_double):
    reset_submitted()
    with _served_app(monkeypatch, trust_double.url) as client:
        yield client
    reset_submitted()


@pytest.fixture()
def served_against_nothing(monkeypatch):
    """A buyer service whose configured trust address answers nothing at all."""
    reset_submitted()
    with _served_app(monkeypatch, f"http://127.0.0.1:{_dead_port()}") as client:
        yield client
    reset_submitted()


# --- the seam: a deployment nobody hand-wired still records feedback -------------------------


def test_a_deployment_that_wired_nothing_by_hand_lands_feedback_on_the_trust_service(
    served, trust_double
) -> None:
    """MEASURED before this: 503 in every deployment, because nothing set app.state.ledger_sink."""
    response = served.post(SUBMIT, json={"order": ORDER, "response": ANSWER})
    assert response.status_code == 201, response.text

    assert [path for path, _ in trust_double.received] == ["/events"], (
        "the shopper's answer did not reach the trust service's published append door"
    )
    _, body = trust_double.received[0]
    assert body["kind"] == "feedback"
    assert body["order_ref"] == ORDER["order_ref"]
    assert body["store_id"] == ORDER["store_id"]
    assert body["auction_id"] == ORDER["auction_id"]
    assert body["event_id"] == response.json()["event_id"]


def test_the_composition_root_binds_the_attribute_the_route_actually_reads() -> None:
    """One spelling of ``ledger_sink``, pinned rather than trusted (as T-071's pair is)."""
    composition = importlib.import_module("buyer_svc.composition")
    routes = importlib.import_module("buyer_svc.feedback.routes")
    assert composition.LEDGER_SINK_ATTR == routes.LEDGER_SINK_ATTR == "ledger_sink"


def test_a_sink_a_deployment_wired_itself_is_never_replaced(monkeypatch, trust_double) -> None:
    """The composition root binds what is unset and nothing else — T-072's rule, unchanged."""
    reset_submitted()

    class Recording:
        def __init__(self) -> None:
            self.events: list[Any] = []

        def append(self, event: Any) -> None:
            self.events.append(event)

    mine = Recording()
    with _served_app(monkeypatch, trust_double.url) as client:
        client.app.state.ledger_sink = mine
        assert client.post(SUBMIT, json={"order": ORDER, "response": ANSWER}).status_code == 201
    reset_submitted()

    assert len(mine.events) == 1
    assert trust_double.received == [], "a hand-wired sink was overwritten by the default"


# --- the projection: a landed event is a trust observation ----------------------------------


def test_the_landed_event_becomes_exactly_one_feedback_match_observation(
    served, trust_double
) -> None:
    """The consumer is trust's OWN projection, run over the bytes that left this process.

    MEASURED before this: ``observations_from_events`` returned ``[]`` for every feedback
    event this service could produce, so the loop from a shopper's answer to a store's trust
    posture did not exist — with a sink wired and a 201 on the wire.
    """
    from apps.trust.src.ledger import observations_from_events

    assert served.post(SUBMIT, json={"order": ORDER, "response": ANSWER}).status_code == 201
    _, body = trust_double.received[0]

    observations = observations_from_events([body])
    assert len(observations) == 1, "the feedback event projects into no trust observation"
    assert observations[0]["store_id"] == ORDER["store_id"]
    assert observations[0]["dim"] == "feedback_match"
    assert observations[0]["type"] == "fulfilled"
    assert observations[0]["observed_at"] == body["ts"]


def test_a_disappointed_shopper_is_the_negative_observation_type(served, trust_double) -> None:
    """`mismatch_return` is the manifest's buyer-reported negative; `fulfilled` is not it."""
    from apps.trust.src.ledger import observations_from_events

    assert served.post(SUBMIT, json={"order": ORDER, "response": DISAPPOINTED}).status_code == 201
    _, body = trust_double.received[0]

    assert body["payload"]["matched_pitch"] is False
    assert observations_from_events([body])[0]["type"] == "mismatch_return"


def test_the_two_observation_types_are_the_manifest_s_own_published_vocabulary() -> None:
    """D18/A3: ground truth is ``fixtures/manifest.json``, never a constant in a service.

    This is the anti-drift pin. The buyer spells the two names itself (it may not import
    ``apps/trust``), so the names are checked against the approved document the trust scorer
    reads its weights from, and against ``contracts.TRUST_DIMENSIONS`` for the dimension.
    """
    from pathlib import Path

    from contracts import TRUST_DIMENSIONS

    from apps.buyer.svc.src.feedback import (
        FEEDBACK_DIMENSION,
        FEEDBACK_NEGATIVE_TYPE,
        FEEDBACK_POSITIVE_TYPE,
    )

    root = Path(__file__).resolve().parents[4]
    manifest = json.loads((root / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    weights = manifest["observation_weights"]

    assert FEEDBACK_DIMENSION in TRUST_DIMENSIONS
    for observation_type in (FEEDBACK_POSITIVE_TYPE, FEEDBACK_NEGATIVE_TYPE):
        assert isinstance(weights.get(observation_type), (int, float)), (
            f"{observation_type!r} carries no published weight in the approved manifest, so the "
            f"trust scorer refuses it and POST /events refuses the whole event"
        )


# --- R5: nothing that names the buyer reaches an append-only, publicly readable ledger -------


def test_no_buyer_identity_reaches_the_trust_ledger(served, trust_double) -> None:
    """R5, and the ledger's read door is unauthenticated — anything published is public forever.

    The order record carries a pseudonym and (through ``extra="allow"``) a real address, both
    of which a caller can put there. Neither may leave this process.
    """
    order = dict(
        ORDER,
        buyer_pseudonym="psn-r14-dana",
        customer_email="dana.reyes@example.com",
        customer_name="Dana Reyes",
    )
    assert served.post(SUBMIT, json={"order": order, "response": ANSWER}).status_code == 201

    _, body = trust_double.received[0]
    blob = json.dumps(body)
    for secret in ("psn-r14-dana", "dana.reyes@example.com", "Dana Reyes"):
        assert secret not in blob, f"{secret!r} reached the append-only trust ledger"

    assert set(body) == {"event_id", "ts", "kind", "store_id", "auction_id", "order_ref", "payload"}
    assert set(body["payload"]) == {"matched_pitch", "reason", "dim", "type"}


def test_a_session_pseudonym_is_used_to_check_ownership_and_never_published() -> None:
    """The library half of the same rule, driven without a database."""
    from apps.buyer.svc.src.feedback import submit_feedback
    from apps.buyer.svc.src.feedback.submission import FeedbackLedger

    seen: list[Any] = []
    event = submit_feedback(
        dict(ORDER, buyer_pseudonym="psn-r14-dana"),
        ANSWER,
        seen.append,
        buyer_pseudonym="psn-r14-dana",
        ledger=FeedbackLedger(),
    )
    assert "psn-r14-dana" not in json.dumps(
        {"payload": dict(event.payload), "order_ref": event.order_ref, "store_id": event.store_id}
    )


# --- the unset / unreachable posture --------------------------------------------------------


def test_a_trust_service_that_is_not_answering_is_a_503_that_can_be_retried_never_a_quiet_201(
    served_against_nothing,
) -> None:
    """R14 promises the answer LANDS. A 201 for an answer that landed nowhere is the lie."""
    response = served_against_nothing.post(SUBMIT, json={"order": ORDER, "response": ANSWER})
    assert response.status_code == 503, response.text

    detail = response.json()["detail"]
    assert detail["retry_with_event_id"] is True
    assert detail["event_id"], "the shopper's answer has no id to re-attempt it under"
    assert "127.0.0.1" not in response.text, "the trust host travelled into a shopper's response"


def test_an_outage_is_counted_on_the_publisher_rather_than_raising(
    served_against_nothing,
) -> None:
    """The never-raising publisher's counters stay readable after the log line scrolls away."""
    served_against_nothing.post(SUBMIT, json={"order": ORDER, "response": ANSWER})

    status = served_against_nothing.app.state.ledger_sink.status()
    assert status["delivering"] is False
    assert status["lost"] == 1
    assert status["delivered"] == 0
    assert status["last_failure"]


def test_the_answer_can_be_re_attempted_under_the_id_the_503_handed_back(
    monkeypatch, trust_double
) -> None:
    """The 503 says "retry with this event_id"; over HTTP there was no way to do it.

    Without this the default binding would make a trust outage BURN the shopper's one piece of
    feedback: the claim stands (the write may have landed), every later submission is a 409,
    and the order can never be answered again in this process.
    """
    reset_submitted()
    with _served_app(monkeypatch, f"http://127.0.0.1:{_dead_port()}") as client:
        first = client.post(SUBMIT, json={"order": ORDER, "response": ANSWER})
        assert first.status_code == 503
        event_id = first.json()["detail"]["event_id"]

        # Same process, same app; the operator has repaired the address.
        client.app.state.ledger_sink = importlib.import_module(
            "buyer_svc.composition"
        ).bind_ledger_sink(trust_double.url)

        retried = client.post(
            SUBMIT, json={"order": ORDER, "response": ANSWER, "event_id": event_id}
        )
        assert retried.status_code == 201, retried.text
        assert retried.json()["event_id"] == event_id
    reset_submitted()

    assert len(trust_double.received) == 1
    assert trust_double.received[0][1]["event_id"] == event_id


def test_a_caller_may_not_choose_the_id_of_a_row_on_an_append_only_ledger(
    served, trust_double
) -> None:
    """An ``event_id`` is honoured ONLY as the re-attempt of an id this process already holds."""
    response = served.post(
        SUBMIT, json={"order": ORDER, "response": ANSWER, "event_id": "fb-chosen-by-the-caller"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["event_id"] != "fb-chosen-by-the-caller"
    assert trust_double.received[0][1]["event_id"] != "fb-chosen-by-the-caller"


def test_asking_for_a_prompt_still_reaches_no_trust_service_at_all(served, trust_double) -> None:
    """``/prompt`` cannot write, and a composition root on the sibling route must not change it."""
    assert served.post(PROMPT, json={"order": ORDER}).status_code == 200
    assert trust_double.received == []
