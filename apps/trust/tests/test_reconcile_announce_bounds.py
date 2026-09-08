"""What ``POST /reconcile`` owes a caller once it has started telling the world.

Two properties, both measured over the real door against the real postgres ledger
(``ledger_clean`` + ``worker_database``, psycopg for the row counts), because both are
properties of the SERVED path and neither is visible from a unit test of the fold.

1. **The announce phase is bounded in wall-clock time, not in attempts per event.**
   ``_announce_observations`` pushes each freshly inserted ``offer_integrity`` observation
   through ``trust.feedback.announce_trust_event``, which makes two inline HTTP attempts —
   the store's own agent, then the exchange's bandit — each bounded by
   ``DEFAULT_PUSH_TIMEOUT_SECONDS`` (0.5s). Against peers that accept a connection and then
   say nothing, that is ~1.0s of hold PER OBSERVATION, and the fold's only other ceiling is
   ``MAX_RECONCILE_INPUT_EVENTS`` (50_000 checkout events, ~16k orders, ~33k observations).
   Measured before the budget landed, on ten orders::

       orders folded            : 10
       fresh observations       : 20
       POST /reconcile WALL TIME: 20.50s

   This door takes no credential, so that is an unauthenticated request holding a worker for
   as long as the ledger is long. The budget stops the announce phase and the response says
   how many observations were sealed into the chain and NOT announced.

2. **A partial append failure does not strand a notification.** ``_append_all`` keeps
   whatever it already appended when a later append raises. Before the fix it raised from
   inside the loop, so the events it HAD landed were sealed into the hash chain and no store
   agent ever heard about them — and re-running the fold cannot repair that, because the
   second run's append is a no-op and a no-op is not ``fresh``.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from trust.feedback import ENV_EXCHANGE_OUTCOMES_URL, ENV_STORE_AGENT_ENDPOINTS, TRUST_EVENT_PATH

from proxyshop_support.asgi_server import serve
from proxyshop_support.postgres import role_dsn

STORE = "store-announce-bounds"
CLUSTER = "cluster-announce-bounds"

#: Ten orders, two graded promises each: the same shape the 20.50s measurement above was
#: taken on, so the assertion below is about the case that was actually observed.
ORDERS = 10
OBSERVATIONS_PER_ORDER = 2


def _purchase(index: int) -> list[dict[str, Any]]:
    """One whole checkout — the promise, the payment, the delivery — for order ``index``."""
    order = f"ord-bounds-{index}"
    token = f"tok-bounds-{index}"
    return [
        {
            "event_id": f"accepted:{STORE}:{order}",
            "ts": "2026-09-01T00:00:00Z",
            "kind": "accepted",
            "store_id": STORE,
            "auction_id": f"auc-bounds-{index}",
            "payload": {
                "bid_ref": f"auc-bounds-{index}:{STORE}",
                "checkout_token": token,
                "state": "accepted",
                "intent_id": f"int-bounds-{index}",
                "cluster_id": CLUSTER,
                "offer": {
                    "unit_price": 40.0,
                    "total_price": 40.0,
                    "product_ref": "p-1",
                    "delivery_estimate_days": 3,
                },
            },
        },
        {
            "event_id": f"order_paid:{STORE}:{order}",
            "ts": "2026-09-02T00:00:00Z",
            "kind": "order_paid",
            "store_id": STORE,
            "order_ref": order,
            "payload": {"checkout_token": token, "order_ref": order, "total_price": 55.0},
        },
        {
            "event_id": f"order_fulfilled:{STORE}:{order}",
            "ts": "2026-09-25T00:00:00Z",
            "kind": "order_fulfilled",
            "store_id": STORE,
            "order_ref": order,
            "payload": {"order_ref": order, "fulfilled_at": "2026-09-25T00:00:00Z"},
        },
    ]


@contextlib.contextmanager
def _black_hole() -> Iterator[str]:
    """A TCP door that completes the handshake and then never answers. Yields its base url.

    Deliberately not a refused connection and not an unroutable address: both of those are
    fast failures, and the thing that holds this door open is a peer that is *reachable and
    silent* — an agent whose process is up and whose event loop is wedged, a proxy that
    accepted and is waiting on something else. That is the case ``httpx``'s read timeout
    bounds at 0.5s per attempt and nothing bounds in aggregate.

    Accepted sockets are held rather than left in the listen backlog, so the retry thread's
    background attempts cannot fill it and start turning into fast ``ECONNREFUSED``.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.settimeout(0.25)
    held: list[socket.socket] = []
    stop = threading.Event()

    def _swallow() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            held.append(connection)

    thread = threading.Thread(target=_swallow, name="black-hole", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        stop.set()
        thread.join(timeout=2.0)
        for connection in held:
            with contextlib.suppress(OSError):
                connection.close()
        listener.close()


def _seed(client: Any, events: list[dict[str, Any]]) -> None:
    for event in events:
        seeded = client.post("/events", json=event)
        assert seeded.status_code == 201, seeded.text


def _observation_count(dsn: str) -> int:
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("select count(*) from ledger.trust_observations")
        row = cursor.fetchone()
    return int(row[0]) if row else 0


@pytest.mark.docker("postgres")
def test_the_announce_phase_stops_when_its_wall_clock_budget_is_spent(
    ledger_clean: Any, worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ten orders, twenty fresh observations, two silent peers — and a bounded answer."""
    from fastapi.testclient import TestClient

    dsn = role_dsn("trust_rw", database=worker_database)
    with contextlib.ExitStack() as stack:
        void = stack.enter_context(_black_hole())
        for variable in (
            "PROXYSHOP_LEDGER_DSN",
            "PROXYSHOP_PG_DSN_TRUST_RW",
            "PROXYSHOP_PG_DSN_APP",
        ):
            monkeypatch.setenv(variable, dsn)
        # Both halves of the fanout point into the void, which is what makes one announce
        # cost two full timeouts rather than one.
        monkeypatch.setenv(ENV_STORE_AGENT_ENDPOINTS, json.dumps({STORE: void}))
        monkeypatch.setenv(ENV_EXCHANGE_OUTCOMES_URL, void)

        from trust.main import create_app

        app = create_app()
        stack.callback(lambda: getattr(getattr(app.state, "event_store", None), "close", bool)())
        stack.callback(
            lambda: getattr(getattr(app.state, "trust_event_sink", None), "close", bool)()
        )
        client = stack.enter_context(TestClient(app))

        for index in range(ORDERS):
            _seed(client, _purchase(index))

        started = time.monotonic()
        folded = client.post("/reconcile")
        wall = time.monotonic() - started

    assert folded.status_code == 200, folded.text
    body = folded.json()
    fresh = ORDERS * OBSERVATIONS_PER_ORDER
    assert body["appended"]["offer_integrity"] == fresh, body["appended"]

    from trust.reconcile import routes as reconcile_routes

    # Read off the module rather than imported at the top, so a build with NO budget still
    # takes the measurement above and then fails here naming the missing bound, instead of
    # dying in collection with an ImportError and reporting nothing. The range is part of the
    # assertion: a budget of `inf` would satisfy every other line in this test.
    budget = getattr(reconcile_routes, "ANNOUNCE_BUDGET_SECONDS", None)
    notifications = body["notifications"]
    print(  # noqa: T201 - the measurement this test exists to take
        f"\norders folded            : {body['reconciled']}"
        f"\nfresh observations       : {fresh}"
        f"\nannounce budget          : {budget!r}"
        f"\nPOST /reconcile WALL TIME: {wall:.2f}s"
        f"\nnotifications            : {notifications}"
    )
    assert isinstance(budget, (int, float)) and 0 < float(budget) < 60.0, (
        f"the announce phase carries no finite wall-clock budget "
        f"(ANNOUNCE_BUDGET_SECONDS={budget!r}), so an unauthenticated POST /reconcile holds a "
        f"worker for as long as the ledger is long — this one held it for {wall:.2f}s on "
        f"{fresh} observations"
    )
    budget = float(budget)

    # The bound. One announce in flight when the budget runs out is allowed to finish, so
    # the hold is the budget plus one attempt (2 x 0.5s) plus the fold itself; the headroom
    # below is for the fold and for a loaded box, and it is still less than half the 20.50s
    # this took before the budget existed.
    assert wall < budget + 4.0, (
        f"POST /reconcile held for {wall:.2f}s announcing {fresh} observations to two silent "
        f"peers; the announce phase has no wall-clock ceiling, and this door takes no "
        f"credential"
    )

    # And the remainder is REPORTED, not dropped and not claimed as pushed.
    assert notifications["not_attempted"] > 0, (
        f"the budget bit but the response does not say how many observations were sealed "
        f"into the chain and never announced: {notifications}"
    )
    assert notifications["attempted"] + notifications["not_attempted"] == fresh, notifications
    assert notifications["pushed"] + notifications["not_pushed"] == notifications["attempted"], (
        notifications
    )
    # Nothing was pushed, because nothing could be: the peers never answered.
    assert notifications["pushed"] == 0, notifications
    assert notifications["budget_seconds"] == pytest.approx(budget)

    # The chain and the relational table are untouched by the budget. The announce is the
    # only thing that was cut short, and the response has to make that distinction legible.
    assert body["observations_written"] == fresh, body
    assert _observation_count(dsn) == fresh


@pytest.mark.docker("postgres")
def test_a_partial_append_failure_still_announces_what_it_landed(
    ledger_clean: Any, worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger goes away between two observations of one order. The first is still told.

    Before the fix ``_append_all`` raised from inside its loop, so the caller never reached
    ``_announce_observations`` and the observation that DID land was sealed into the chain
    with nobody told. The second run cannot repair it: the append is a no-op, a no-op is not
    ``fresh``, and there is no other door in this service that re-announces a stored event.
    """
    from fastapi.testclient import TestClient

    received: list[dict[str, Any]] = []
    agent = FastAPI()

    @agent.post(TRUST_EVENT_PATH)
    def _door(body: dict[str, Any]) -> dict[str, str]:
        received.append(body)
        return {"ok": "true"}

    dsn = role_dsn("trust_rw", database=worker_database)
    with contextlib.ExitStack() as stack:
        agent_url = stack.enter_context(serve(agent))
        for variable in (
            "PROXYSHOP_LEDGER_DSN",
            "PROXYSHOP_PG_DSN_TRUST_RW",
            "PROXYSHOP_PG_DSN_APP",
        ):
            monkeypatch.setenv(variable, dsn)
        monkeypatch.setenv(ENV_STORE_AGENT_ENDPOINTS, json.dumps({STORE: agent_url}))
        monkeypatch.delenv(ENV_EXCHANGE_OUTCOMES_URL, raising=False)

        from trust.events.errors import StoreUnavailable
        from trust.main import create_app

        app = create_app()
        stack.callback(lambda: getattr(getattr(app.state, "event_store", None), "close", bool)())
        stack.callback(
            lambda: getattr(getattr(app.state, "trust_event_sink", None), "close", bool)()
        )

        class _FailsOnTheSecondObservation:
            """The real store, with the ledger disappearing part-way through one batch."""

            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self.armed = True
                self.seen = 0

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def append(self, event: Mapping[str, Any]) -> Any:
                if str(event.get("kind")) == "offer_integrity":
                    self.seen += 1
                    if self.armed and self.seen == 2:
                        raise StoreUnavailable(
                            "the ledger went away between two observations of one order"
                        )
                return self._inner.append(event)

        client = stack.enter_context(TestClient(app, raise_server_exceptions=False))

        # Seeded first: `store_for` builds the real store lazily on the first request, so the
        # wrapper has to go on afterwards — and the seeding must go through the real one
        # anyway, since only the observation appends are being interfered with.
        _seed(client, _purchase(0))
        app.state.event_store = _FailsOnTheSecondObservation(app.state.event_store)

        refused = client.post("/reconcile")
        assert refused.status_code == 503, refused.text
        assert refused.json()["detail"]["error"] == "store_unavailable", refused.text

        chain = client.get("/events", params={"limit": 500}).json()["events"]
        sealed = [row["event_id"] for row in chain if row["kind"] == "offer_integrity"]
        assert len(sealed) == 1, (
            f"the injection is supposed to land exactly one observation before the ledger "
            f"goes away; it landed {sealed}"
        )

        # THE POINT. The event is in the hash chain, so the store has to have been told —
        # the 503 is about what did NOT land, and it must not swallow what did.
        assert len(received) == 1, (
            f"{len(sealed)} observation(s) were sealed into the chain and {len(received)} "
            f"announced; {sealed} is in the ledger and no store agent was told, and no later "
            f"run of this fold will ever tell them"
        )
        announced = str(received[0]["event"]["event_id"])
        assert announced == sealed[0], (received, sealed)

        # The refusal is unchanged in status and body once the ledger comes back.
        app.state.event_store.armed = False
        again = client.post("/reconcile")
        assert again.status_code == 200, again.text
        body = again.json()
        assert body["appended"]["offer_integrity"] == 1, body["appended"]
        assert body["already_present"]["offer_integrity"] == 1, body["already_present"]
        assert body["notifications"]["attempted"] == 1, body["notifications"]
        assert body["notifications"]["pushed"] == 1, body["notifications"]

    # Two observations, two deltas: the one the failed run landed and the one the repeat did.
    # Never three — the announced-then-already-present one must not be charged twice.
    assert len(received) == 2, [hit["event"]["event_id"] for hit in received]
    assert {hit["dim"] for hit in received} == {"price_honored", "shipped_on_time"}
    assert _observation_count(dsn) == 2
