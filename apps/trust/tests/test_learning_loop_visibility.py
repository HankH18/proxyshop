"""A disabled learning loop is LOUD, an undelivered delta is READABLE, and a push is RETRIED.

Three defects, one file, and all three had the same shape: work that happened (or did not) with
nothing outside the process able to tell.

1. **Nothing said the loop was off.** ``TRUST_STORE_AGENT_ENDPOINTS`` had five occurrences in
   the repository and not one set a value, so a store agent's only learning input was dead in
   every shipped configuration — and no log line, no counter and no served surface reported it.
   A stack whose loop was switched off looked exactly like a stack in which nothing had
   happened yet.
2. **Nothing could read what was lost.** The undelivered ring was a ``deque`` on an object
   reachable from no route. It counted correctly and could not be asked.
3. **One attempt, then gone.** :meth:`StoreAgentSink.send` made exactly one POST with no loop
   and no backoff, so an agent that was restarting when a delta arrived never got it.

Everything below is graded against the SERVED behaviour or against the real sink over a real
loopback socket. The one place a plain function is called (:func:`learning_loop_report`) is a
pure reading of the environment, which is what it is.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Response
from trust.feedback import (
    ENV_EXCHANGE_OUTCOMES_URL,
    ENV_STORE_AGENT_ENDPOINTS,
    EXCHANGE_OUTCOMES_PATH,
    TRUST_EVENT_PATH,
    ExchangeOutcomeSink,
    StoreAgentSink,
    build_sink,
    exchange_outcomes_url,
    learning_loop_report,
    log_learning_loop_state,
)

from proxyshop_support.asgi_server import serve
from proxyshop_support.postgres import role_dsn

DEMO_BOOK = {"gaiaherbs.com": "http://store-agent-gaiaherbs:8086"}


def _payload(store_id: str = "s-1", event_id: str = "ev-1", cluster: str | None = "c-1") -> dict:
    """The shape ``push_trust_event`` hands a sink, reduced to what a sink reads off it."""
    return {
        "store_id": store_id,
        "event": {"event_id": event_id, "ts": "2026-01-01T00:00:00Z", "kind": "feedback"},
        "dim": "feedback_match",
        "delta": -0.25,
        "pseudonymous_context": {"cluster_id": cluster, "pseudonym": None},
    }


@contextlib.contextmanager
def _flaky_agent(fail_times: int, status: int = 503) -> Iterator[tuple[str, list[int]]]:
    """A store agent that refuses its first ``fail_times`` calls and then accepts.

    The exact failure a retry exists for: an agent that is coming up. Its ``attempts`` list is
    the evidence that a second POST was actually made, rather than a counter this test keeps.
    """
    app = FastAPI()
    attempts: list[int] = []
    lock = threading.Lock()

    @app.post(TRUST_EVENT_PATH)
    def _door(response: Response) -> dict[str, str]:
        with lock:
            attempts.append(len(attempts) + 1)
            index = len(attempts)
        if index <= fail_times:
            response.status_code = status
            return {"error": "not_ready"}
        return {"ok": "true"}

    with contextlib.ExitStack() as stack:
        url = stack.enter_context(serve(app))
        yield url, attempts


# ==========================================================================================
# 1 — the configuration says which halves of the loop are addressed, out loud, at boot
# ==========================================================================================
def test_an_unaddressed_deployment_is_reported_as_disabled_in_both_halves() -> None:
    report = learning_loop_report({})

    assert report["enabled"] is False
    assert report["store_agent_push"] == {
        "enabled": False,
        "variable": ENV_STORE_AGENT_ENDPOINTS,
        "stores": [],
    }
    assert report["exchange_outcomes"] == {
        "enabled": False,
        "variable": ENV_EXCHANGE_OUTCOMES_URL,
        "url": None,
    }


def test_an_addressed_deployment_names_the_stores_and_the_exchange() -> None:
    report = learning_loop_report(
        {
            ENV_STORE_AGENT_ENDPOINTS: json.dumps(DEMO_BOOK),
            ENV_EXCHANGE_OUTCOMES_URL: "http://exchange:8083",
        }
    )

    assert report["enabled"] is True
    assert report["store_agent_push"]["stores"] == ["gaiaherbs.com"]
    assert report["exchange_outcomes"]["url"] == f"http://exchange:8083{EXCHANGE_OUTCOMES_PATH}"


@pytest.mark.parametrize(
    "configured",
    [
        "http://exchange:8083",
        "http://exchange:8083/",
        f"http://exchange:8083{EXCHANGE_OUTCOMES_PATH}",
    ],
)
def test_the_exchange_url_is_the_same_deployment_however_it_is_spelled(configured: str) -> None:
    """A base url and the full outcomes url must not be one working stack and one 404ing one."""
    assert (
        exchange_outcomes_url({ENV_EXCHANGE_OUTCOMES_URL: configured})
        == f"http://exchange:8083{EXCHANGE_OUTCOMES_PATH}"
    )


def test_a_disabled_loop_warns_at_boot_and_names_the_variable_that_would_fix_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The line an operator greps for. Silence here is the original defect, exactly."""
    logger = logging.getLogger("trust.test.boot.off")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_learning_loop_state({}, log=logger)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2, f"expected both halves to warn, got {[r.message for r in warnings]}"
    said = " ".join(r.getMessage() for r in warnings)
    assert ENV_STORE_AGENT_ENDPOINTS in said
    assert ENV_EXCHANGE_OUTCOMES_URL in said
    assert "no store's advocate will ever learn" in said
    assert "exposure will not shift with results" in said


def test_an_addressed_loop_says_so_at_boot_and_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The mirror image, so the warning above is not simply "this function always warns"."""
    logger = logging.getLogger("trust.test.boot.on")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_learning_loop_state(
            {
                ENV_STORE_AGENT_ENDPOINTS: json.dumps(DEMO_BOOK),
                ENV_EXCHANGE_OUTCOMES_URL: "http://exchange:8083",
            },
            log=logger,
        )

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "gaiaherbs.com" in said
    assert "/internal/outcomes" in said


# ==========================================================================================
# 2 — an undelivered delta is readable, with its reason, and says it is not durable
# ==========================================================================================
def test_an_unaddressed_sink_records_what_it_could_not_deliver_and_names_the_variable() -> None:
    """The state every shipped configuration was in, now legible instead of silent.

    A sink is built even with an empty address book precisely so this record exists: before,
    an unaddressed deployment resolved its sink to ``None``, skipped the announce entirely, and
    left nothing at all behind.
    """
    sink = StoreAgentSink({})

    assert sink.send("gaiaherbs.com", _payload(store_id="gaiaherbs.com")) is False

    report = sink.report()
    assert report["addressed"] is False
    assert report["lost"] == 1
    assert report["durable"] is False, "an in-process ring must not claim to survive a restart"
    assert len(report["undelivered"]) == 1
    entry = report["undelivered"][0]
    assert entry["store_id"] == "gaiaherbs.com"
    assert entry["event_id"] == "ev-1"
    assert ENV_STORE_AGENT_ENDPOINTS in entry["reason"]


def test_the_readable_ring_carries_ids_and_reasons_and_never_the_payload() -> None:
    """R13's privacy rule survives the readback: the ring is what an operator reads."""
    sink = StoreAgentSink({})
    sink.send("s-1", _payload())

    rendered = json.dumps(sink.report())
    assert "pseudonymous_context" not in rendered
    assert "feedback_match" not in rendered, "the ring is publishing the delta's payload"


def test_the_ring_is_bounded_so_an_outage_cannot_grow_it_without_a_ceiling() -> None:
    sink = StoreAgentSink({})
    for index in range(200):
        sink.send("s-1", _payload(event_id=f"ev-{index}"))

    report = sink.report()
    assert report["lost"] == 200, "every failure must be counted even when the ring drops it"
    assert len(report["undelivered"]) == report["undelivered_ring_capacity"]


# ==========================================================================================
# 3 — a push that did not land is tried again, with backoff, off the request path
# ==========================================================================================
def test_a_delta_a_restarting_agent_refused_lands_on_a_later_attempt() -> None:
    """Two real HTTP calls to one real server: the second is the one that delivers."""
    with _flaky_agent(fail_times=1) as (url, attempts):
        sink = StoreAgentSink({"s-1": url}, retry_backoff=0.01)
        try:
            assert sink.send("s-1", _payload()) is False, "the inline attempt must have failed"
            _wait_until(lambda: sink.delivered == 1, "the retry never delivered")
        finally:
            sink.close()

    assert len(attempts) == 2, f"the agent saw {len(attempts)} attempt(s), not two"
    report = sink.report()
    assert report["retried"] == 1
    assert report["lost"] == 0, "a delta that landed on a retry is not lost"
    assert report["delivering"] is True, "the recovery must clear the outage"


def test_a_delta_no_number_of_attempts_can_deliver_is_given_up_on_and_said_so() -> None:
    """Retrying forever is its own silence: the ring has to end up holding this."""
    with _flaky_agent(fail_times=99) as (url, attempts):
        sink = StoreAgentSink({"s-1": url}, retry_attempts=3, retry_backoff=0.01)
        try:
            sink.send("s-1", _payload())
            _wait_until(lambda: sink.lost == 1, "the sink never gave up")
        finally:
            sink.close()

    assert len(attempts) == 3, f"expected exactly 3 attempts, the agent saw {len(attempts)}"
    assert "given up after 3 attempt(s)" in sink.report()["undelivered"][-1]["reason"]


def test_a_refusal_the_receiver_calls_a_bad_message_is_not_retried() -> None:
    """A 409 misroute is wrong on the second attempt too; four of them is four times the noise."""
    with _flaky_agent(fail_times=99, status=409) as (url, attempts):
        sink = StoreAgentSink({"s-1": url}, retry_backoff=0.01)
        try:
            sink.send("s-1", _payload())
        finally:
            sink.close()

    assert len(attempts) == 1, f"a 4xx was retried {len(attempts)} times"
    assert sink.lost == 1


def test_the_inline_attempt_is_the_only_one_that_can_hold_the_callers_thread() -> None:
    """``send`` returns immediately on failure; the retries happen on the sink's own thread."""
    import time

    with _flaky_agent(fail_times=99) as (url, _attempts):
        sink = StoreAgentSink({"s-1": url}, retry_attempts=4, retry_backoff=2.0)
        try:
            started = time.monotonic()
            sink.send("s-1", _payload())
            elapsed = time.monotonic() - started
        finally:
            sink.close()

    assert elapsed < 1.0, (
        f"send() held its caller for {elapsed:.2f}s; the backoff is running on the request path"
    )


# ==========================================================================================
# 4 — the exchange half: an outcome that cannot be routed is named, not posted
# ==========================================================================================
def test_an_outcome_with_no_cluster_is_refused_here_rather_than_by_the_exchange() -> None:
    """``POST /internal/outcomes`` 400s an outcome naming no cluster. Do not make it say so."""
    posted: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post(EXCHANGE_OUTCOMES_PATH, status_code=204)
    def _door(body: dict[str, Any]) -> Response:
        posted.append(body)
        return Response(status_code=204)

    with contextlib.ExitStack() as stack:
        url = stack.enter_context(serve(app))
        sink = ExchangeOutcomeSink(f"{url}{EXCHANGE_OUTCOMES_PATH}")
        try:
            assert sink.send("s-1", _payload(cluster=None)) is False
            assert sink.send("s-1", _payload(cluster="c-1")) is True
        finally:
            sink.close()

    assert len(posted) == 1, "the unroutable outcome opened a socket it did not need to"
    assert posted[0]["pseudonymous_context"]["cluster_id"] == "c-1"
    assert "names no cluster" in sink.report()["undelivered"][0]["reason"]


def test_the_fanout_tells_the_store_agent_and_the_exchange_from_one_delta() -> None:
    """One trust movement, two recipients, and neither can stop the other being told."""
    agent_hits: list[dict[str, Any]] = []
    exchange_hits: list[dict[str, Any]] = []
    agent, exchange = FastAPI(), FastAPI()

    @agent.post(TRUST_EVENT_PATH)
    def _agent_door(body: dict[str, Any]) -> dict[str, str]:
        agent_hits.append(body)
        return {"ok": "true"}

    @exchange.post(EXCHANGE_OUTCOMES_PATH, status_code=204)
    def _exchange_door(body: dict[str, Any]) -> Response:
        exchange_hits.append(body)
        return Response(status_code=204)

    with contextlib.ExitStack() as stack:
        agent_url = stack.enter_context(serve(agent))
        exchange_url = stack.enter_context(serve(exchange))
        sink = build_sink(
            {
                ENV_STORE_AGENT_ENDPOINTS: json.dumps({"s-1": agent_url}),
                ENV_EXCHANGE_OUTCOMES_URL: exchange_url,
            }
        )
        try:
            assert sink.send("s-1", _payload()) is True
        finally:
            sink.close()

    assert len(agent_hits) == 1
    assert len(exchange_hits) == 1
    report = sink.report()
    assert report["enabled"] is True
    assert report["store_agents"]["delivered"] == 1
    assert report["exchange_outcomes"]["delivered"] == 1


def test_an_exchange_that_is_down_does_not_stop_the_store_agent_being_told() -> None:
    agent_hits: list[dict[str, Any]] = []
    agent = FastAPI()

    @agent.post(TRUST_EVENT_PATH)
    def _agent_door(body: dict[str, Any]) -> dict[str, str]:
        agent_hits.append(body)
        return {"ok": "true"}

    with contextlib.ExitStack() as stack:
        agent_url = stack.enter_context(serve(agent))
        sink = build_sink(
            {
                ENV_STORE_AGENT_ENDPOINTS: json.dumps({"s-1": agent_url}),
                ENV_EXCHANGE_OUTCOMES_URL: "http://127.0.0.1:1",
            },
            retry_attempts=1,
        )
        try:
            assert sink.send("s-1", _payload()) is True
        finally:
            sink.close()

    assert len(agent_hits) == 1
    assert sink.report()["exchange_outcomes"]["lost"] == 1


# ==========================================================================================
# helpers
# ==========================================================================================
def _wait_until(predicate: Any, message: str, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


# ==========================================================================================
# 5 — SERVED. The two doors an operator actually has.
# ==========================================================================================
@pytest.mark.docker("postgres")
def test_post_events_reports_what_the_notification_did(
    events_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A producer must be able to tell "no delta in this event" from "told to nobody"."""
    monkeypatch.setenv(ENV_STORE_AGENT_ENDPOINTS, "{}")
    monkeypatch.delenv(ENV_EXCHANGE_OUTCOMES_URL, raising=False)

    silent = events_client.post(
        "/events",
        json={
            "event_id": "ev-notify-quiet-1",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "auction_opened",
            "auction_id": "a-notify-1",
            "payload": {"intent_id": "i-1", "cluster_id": "c-1", "roster_size": 1},
        },
    )
    assert silent.status_code == 201, silent.text
    assert silent.json()["notification"]["pushed"] is False
    assert "no trust observation" in silent.json()["notification"]["reason"]

    moved = events_client.post(
        "/events",
        json={
            "event_id": "ev-notify-moved-1",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "feedback",
            "store_id": "store-notify-1",
            "order_ref": "ord-notify-1",
            "payload": {
                "dim": "feedback_match",
                "type": "contradicted",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        },
    )
    assert moved.status_code == 201, moved.text
    notification = moved.json()["notification"]
    assert notification["store_id"] == "store-notify-1"
    assert notification["pushed"] is False, "nothing is addressed, so nothing can have landed"
    # `dim` is None ON PURPOSE, and this assertion is the one that pins it. With no recipient
    # addressed, `announce_trust_event` records the non-delivery WITHOUT computing the delta —
    # which is what stops an unaddressed deployment paying a ledger read and two scoring passes
    # per observation-carrying append to produce a number nobody will ever receive. The
    # `store_id` above and the `reason` below are what the producer needs; the arithmetic is
    # not, and this asserts it was skipped rather than merely absent.
    assert notification["dim"] is None, (
        "a delta was computed for an event nobody can be told about; the pre-flight in "
        "announce_trust_event is not running"
    )
    assert ENV_STORE_AGENT_ENDPOINTS in notification["reason"], (
        f"the reason must name the variable that would have delivered it: {notification['reason']}"
    )


@pytest.mark.docker("postgres")
def test_get_events_verify_serves_the_undelivered_ring(
    events_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readback. Before this the ring was reachable by nothing an operator could call."""
    monkeypatch.setenv(ENV_STORE_AGENT_ENDPOINTS, "{}")
    monkeypatch.delenv(ENV_EXCHANGE_OUTCOMES_URL, raising=False)

    events_client.post(
        "/events",
        json={
            "event_id": "ev-notify-ring-1",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "feedback",
            "store_id": "store-notify-ring",
            "order_ref": "ord-notify-ring",
            "payload": {
                "dim": "feedback_match",
                "type": "contradicted",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        },
    )

    verify = events_client.get("/events/verify")
    assert verify.status_code == 200, verify.text
    body = verify.json()
    assert body["ok"] is True, "the chain's own verdict must still be the headline"

    notifications = body["store_agent_notifications"]
    assert notifications["available"] is True
    assert notifications["store_agents"]["addressed"] is False
    assert notifications["store_agents"]["durable"] is False
    lost = [
        entry
        for entry in notifications["store_agents"]["undelivered"]
        if entry["event_id"] == "ev-notify-ring-1"
    ]
    assert lost, (
        "the delta this append computed is not on the served ring; an operator still cannot "
        f"read what was lost. Ring: {notifications['store_agents']['undelivered']}"
    )
    assert ENV_STORE_AGENT_ENDPOINTS in lost[0]["reason"]


# ==========================================================================================
# 6 — SERVED. A completed purchase reaches both learners, which it never did before.
# ==========================================================================================
@pytest.mark.docker("postgres")
def test_post_reconcile_tells_the_store_agent_and_the_exchange_about_a_completed_purchase(
    ledger_clean: Any, worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4's authority, wired to the two things that learn from it.

    ``POST /reconcile`` appends through ``trust.events.store.append`` directly rather than
    through ``POST /events``, so until now every ``offer_integrity`` observation it minted --
    the whole transaction half of trust, the half a store can actually act on -- was sealed
    into the chain and told to nobody. This drives the real door against the real database and
    reads the deltas off two receivers on real loopback sockets.
    """
    from fastapi.testclient import TestClient

    store_id = "store-reconcile-notify"
    cluster = "cluster-reconcile-notify"
    order = "ord-reconcile-notify"
    token = "tok-reconcile-notify"

    agent_hits: list[dict[str, Any]] = []
    exchange_hits: list[dict[str, Any]] = []
    agent, exchange = FastAPI(), FastAPI()

    @agent.post(TRUST_EVENT_PATH)
    def _agent_door(body: dict[str, Any]) -> dict[str, str]:
        agent_hits.append(body)
        return {"ok": "true"}

    @exchange.post(EXCHANGE_OUTCOMES_PATH, status_code=204)
    def _exchange_door(body: dict[str, Any]) -> Response:
        exchange_hits.append(body)
        return Response(status_code=204)

    dsn = role_dsn("trust_rw", database=worker_database)
    with contextlib.ExitStack() as stack:
        agent_url = stack.enter_context(serve(agent))
        exchange_url = stack.enter_context(serve(exchange))
        for variable in (
            "PROXYSHOP_LEDGER_DSN",
            "PROXYSHOP_PG_DSN_TRUST_RW",
            "PROXYSHOP_PG_DSN_APP",
        ):
            monkeypatch.setenv(variable, dsn)
        monkeypatch.setenv(ENV_STORE_AGENT_ENDPOINTS, json.dumps({store_id: agent_url}))
        monkeypatch.setenv(ENV_EXCHANGE_OUTCOMES_URL, exchange_url)

        from trust.main import create_app

        app = create_app()
        stack.callback(lambda: getattr(getattr(app.state, "event_store", None), "close", bool)())
        client = stack.enter_context(TestClient(app))

        for event in (
            {
                "event_id": f"accepted:{store_id}:{order}",
                "ts": "2026-09-01T00:00:00Z",
                "kind": "accepted",
                "store_id": store_id,
                "auction_id": "auc-reconcile-notify",
                "payload": {
                    "bid_ref": f"auc-reconcile-notify:{store_id}",
                    "checkout_token": token,
                    "state": "accepted",
                    "intent_id": "int-reconcile-notify",
                    "cluster_id": cluster,
                    "offer": {
                        "unit_price": 40.0,
                        "total_price": 40.0,
                        "product_ref": "p-1",
                        "delivery_estimate_days": 3,
                    },
                },
            },
            {
                "event_id": f"order_paid:{store_id}:{order}",
                "ts": "2026-09-02T00:00:00Z",
                "kind": "order_paid",
                "store_id": store_id,
                "order_ref": order,
                "payload": {"checkout_token": token, "order_ref": order, "total_price": 55.0},
            },
            {
                "event_id": f"order_fulfilled:{store_id}:{order}",
                "ts": "2026-09-25T00:00:00Z",
                "kind": "order_fulfilled",
                "store_id": store_id,
                "order_ref": order,
                "payload": {"order_ref": order, "fulfilled_at": "2026-09-25T00:00:00Z"},
            },
        ):
            seeded = client.post("/events", json=event)
            assert seeded.status_code == 201, seeded.text

        assert agent_hits == [], "none of the three checkout events carries an observation"

        folded = client.post("/reconcile")
        assert folded.status_code == 200, folded.text
        body = folded.json()
        assert body["appended"]["offer_integrity"] == 2, body["appended"]
        assert body["notifications"] == {"attempted": 2, "pushed": 2, "not_pushed": 0}

        # Re-running the fold is a no-op, so the store must not be charged a second time.
        again = client.post("/reconcile")
        assert again.status_code == 200, again.text
        assert again.json()["notifications"] == {"attempted": 0, "pushed": 0, "not_pushed": 0}

    assert len(agent_hits) == 2, f"the store agent saw {len(agent_hits)} deltas"
    assert {hit["dim"] for hit in agent_hits} == {"price_honored", "shipped_on_time"}
    assert {hit["store_id"] for hit in agent_hits} == {store_id}

    assert len(exchange_hits) == 2, f"the exchange saw {len(exchange_hits)} outcomes"
    assert {hit["pseudonymous_context"]["cluster_id"] for hit in exchange_hits} == {cluster}, (
        "an outcome with no cluster cannot be routed to a posterior and the exchange 400s it"
    )


# ==========================================================================================
# 7 — the three the first version of this file did not grade, each found by an adversary
# ==========================================================================================
def test_the_boot_line_fires_from_the_real_import_path_and_not_only_when_called() -> None:
    """The BOOT block is graded here, and it was graded by nothing before.

    Mutation, run to prove the point: neuter ``log_learning_loop_state``, import
    ``trust.events.routes``, restore — i.e. exactly the tree with the boot call deleted — and
    the whole trust suite still read ``899 passed``. Every other test in this file calls the
    function DIRECTLY with an injected logger, which cannot see whether the call site exists,
    whether it survived, or whether it runs early enough for a handler to be installed.

    So this one reloads the module the way ``trust.main.create_app`` imports it and reads the
    real ``trust.events.routes`` logger. ``configure_logging`` has to have run first or the
    record is dropped before it is formatted, which is the failure mode that would have made
    the line true in the source and absent from every started process.
    """
    import importlib

    import trust.events.routes as routes_module

    with caplog_at("trust.events.routes") as records:
        importlib.reload(routes_module)

    said = " ".join(record.getMessage() for record in records)
    assert ENV_STORE_AGENT_ENDPOINTS in said or ENV_EXCHANGE_OUTCOMES_URL in said, (
        f"importing trust.events.routes said nothing about the learning loop; the boot line "
        f"is not on the import path a started process takes. Records: {said!r}"
    )


@contextlib.contextmanager
def caplog_at(logger_name: str) -> Iterator[list[logging.LogRecord]]:
    """Collect records from ``logger_name`` without pytest's caplog handler ordering."""
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Collector(level=logging.DEBUG)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_a_store_with_no_address_is_not_reported_as_an_outage() -> None:
    """Configuration is not failure, and conflating them made the health surface flap.

    The demo corpus rosters ten storefronts and ships four agents. With every unaddressed store
    filed as an outage, an ordinary run alternated ``ERROR trust events stopped reaching store
    agents`` and ``INFO … reaching them again`` on consecutive events with nothing wrong, and
    ``delivering`` reported whichever store the LAST append happened to be about.
    """
    logger = logging.getLogger("trust.test.outage.split")
    with contextlib.ExitStack() as stack:
        agent = FastAPI()

        @agent.post(TRUST_EVENT_PATH)
        def _door() -> dict[str, str]:
            return {"ok": "true"}

        url = stack.enter_context(serve(agent))
        sink = StoreAgentSink({"addressed.example": url}, log=logger)
        stack.callback(sink.close)

        with caplog_at(logger.name) as records:
            assert (
                sink.send("unaddressed.example", _payload(store_id="unaddressed.example")) is False
            )
            assert sink.send("addressed.example", _payload(store_id="addressed.example")) is True
            assert sink.send("other.example", _payload(store_id="other.example")) is False

    report = sink.report()
    assert report["delivering"] is True, (
        "an unaddressed store flipped `delivering` to false; a store this deployment never "
        "named is not a peer that stopped answering"
    )
    assert report["undeliverable"] == 2
    assert report["lost"] == 2, "an undeliverable delta is still a delta that did not land"
    assert [r for r in records if r.levelno >= logging.ERROR] == [], (
        f"an outage was declared for a store with no address: {[r.getMessage() for r in records]}"
    )
    # ...and the ring still holds both, because "not an outage" is not "not worth recording".
    assert {entry["store_id"] for entry in report["undelivered"]} == {
        "unaddressed.example",
        "other.example",
    }


def test_an_outcome_with_no_cluster_is_not_reported_as_an_exchange_outage() -> None:
    """R14 buyer feedback carries no cluster and never will — it must not read as an outage."""
    logger = logging.getLogger("trust.test.outage.cluster")
    sink = ExchangeOutcomeSink("http://127.0.0.1:1/internal/outcomes", log=logger)
    try:
        with caplog_at(logger.name) as records:
            assert sink.send("s-1", _payload(cluster=None)) is False
    finally:
        sink.close()

    report = sink.report()
    assert report["delivering"] is True
    assert report["undeliverable"] == 1
    assert [r for r in records if r.levelno >= logging.ERROR] == []


def test_a_real_outage_is_still_reported_as_one() -> None:
    """The control for the two above: without it they would pass on a sink that never errors."""
    logger = logging.getLogger("trust.test.outage.real")
    sink = StoreAgentSink({"s-1": "http://127.0.0.1:1"}, retry_attempts=1, log=logger)
    try:
        with caplog_at(logger.name) as records:
            assert sink.send("s-1", _payload()) is False
    finally:
        sink.close()

    assert sink.report()["delivering"] is False
    assert sink.report()["undeliverable"] == 0
    assert [r for r in records if r.levelno >= logging.ERROR], (
        "an addressed peer that refused the connection produced no ERROR"
    )


def test_the_readback_survives_the_retry_thread_writing_to_the_ring() -> None:
    """``report()`` used to iterate the deque live and raise mid-outage.

    Measured before the snapshot: ``RuntimeError: deque mutated during iteration``, which
    ``notification_report`` then served as ``available: false`` — so the readback failed
    precisely while the outage it exists to describe was in progress.
    """
    import threading as _threading

    sink = StoreAgentSink({}, retry_attempts=1)
    stop = _threading.Event()

    def _churn() -> None:
        index = 0
        while not stop.is_set():
            sink.send("s-1", _payload(event_id=f"ev-{index}"))
            index += 1

    writer = _threading.Thread(target=_churn, daemon=True)
    writer.start()
    try:
        for _ in range(500):
            json.dumps(sink.report())
    finally:
        stop.set()
        writer.join(timeout=5.0)
        sink.close()


def test_the_readback_publishes_no_roster_and_no_peer_url() -> None:
    """``GET /events/verify`` takes no credential — see ``apps/trust``'s own docstrings.

    Which stores have an agent is the network's supply roster (D55: an advocate is what a shop
    BUYS by joining), and the exchange's address is internal. Neither belongs in a body any
    anonymous caller can fetch. The store an event is ABOUT is already public on ``GET /events``,
    so the ring itself stays.
    """
    sink = build_sink(
        {
            ENV_STORE_AGENT_ENDPOINTS: json.dumps(
                {"gaiaherbs.com": "http://a:8086", "toniiq.com": "http://b:8086"}
            ),
            ENV_EXCHANGE_OUTCOMES_URL: "http://exchange.internal.example:8083",
        }
    )
    try:
        sink.send("unlisted.example", _payload(store_id="unlisted.example"))
        rendered = json.dumps(sink.report())
    finally:
        sink.close()

    assert "gaiaherbs.com" not in rendered, "the readback publishes the supply roster"
    assert "toniiq.com" not in rendered
    assert "exchange.internal.example" not in rendered, "the readback publishes the peer's url"
    assert sink.report()["store_agents"]["addressed_stores"] == 2, "the COUNT is still readable"
    assert "unlisted.example" in rendered, "the ring must still name what was lost"
