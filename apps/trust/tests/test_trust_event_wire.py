"""R13 END TO END, over two real processes: an event on ``POST /events`` moves ONE agent.

The defect this file grades is not "a function is wrong". Both ends of R13 were correct,
unit-tested and reachable by nobody: every call site of ``trust.feedback.push_trust_event``
was a test, every call site of ``store_agent.modes.AgentRunner.ingest_trust_event`` was a
test, neither service had an HTTP door for the other, and nothing anywhere computed the
per-event delta the push has always required. So every assertion here is made against a
SERVED request — the trust ledger writer on a real loopback socket in front of real Postgres,
and one real ``store_agent.main:app`` per store on real loopback sockets of their own — and
never against a function called in-process.

MEASURED BEFORE THE WIRE EXISTED (the red this file was written against)::

    POST /events  ->  201 Created
    the affected store's agent received 0 requests

Four properties, and the last two are the ones that make the first safe to ship:

1. **The chain runs.** An event lands on the ledger's door, a delta is computed for the
   affected store, it is pushed to that store's agent, and the agent's own posture — read back
   out of the served process, not out of the response — has moved.
2. **The delta is the scorer's, not a second opinion.** The number the agent receives equals
   ``score``'s own dimension mean recomputed with and without that event. Asserted against the
   scorer rather than against a literal, so a change to the published weights moves both sides.
3. **To the affected store only.** A second store's agent is running, addressed, and healthy
   throughout, and receives NOTHING. R13 calls a broadcast a leak of one store's trust
   movement to its competitors, so this is a privacy assertion, not a routing nicety.
4. **The send cannot break the door.** With the store agent unreachable, refusing, and slow,
   ``POST /events`` still answers ``201`` and the event is still in the chain and still
   verifies. An audit record is worth less than the ledger it is an audit of.

Both stores are the repo's OWN approved envelope fixtures (``fixtures/envelopes/``), driven
through the real intake, so "honest traffic still passes" is measured on the artifacts this
build ships rather than on a shape invented here.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from store_agent.main import create_app
from store_agent.solicitation import CONTEXT_ENV, configure_solicitation, load_context_from_env
from store_agent.trust_intake import agent_runner
from trust.feedback import ENV_STORE_AGENT_ENDPOINTS, TRUST_EVENT_PATH, delta_for_event
from trust.ledger import observations_from_events
from trust.scoring import score

from proxyshop_support.asgi_server import serve

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two approved-envelope fixtures this repo ships. Real merchant artifacts, not shapes
#: invented for a test: "honest traffic still passes" has to be measured on what the build
#: actually deploys.
ENVELOPES = {
    "store-alpha": REPO_ROOT / "fixtures/envelopes/store-alpha.approved.json",
    "store-beta": REPO_ROOT / "fixtures/envelopes/store-beta.approved.json",
}


class Agent:
    """One served store-agent process, plus a record of every request that reached it."""

    def __init__(self, store_id: str, url: str, app: FastAPI, received: list[str]) -> None:
        self.store_id = store_id
        self.url = url
        self.app = app
        self.received = received

    @property
    def posture(self) -> Any:
        """The runner's posture, read out of the SERVED application's own state.

        Not out of the intake's response body. A door that answers with a posture it computed
        and then dropped would satisfy an assertion on the response and leave the process
        exactly as unchanged as it was before this ticket; the state has to be read back from
        the process to prove the event was absorbed rather than merely parsed.
        """
        return agent_runner(self.app).trust_posture

    def signal(self, dim: str) -> Any:
        return next((s for s in self.posture.signals if str(s.dim) == dim), None)


@contextlib.contextmanager
def _agent(store_id: str, stack: contextlib.ExitStack) -> Iterator[Agent]:
    # Loaded through the SHIPPED loader, with the environment handed in explicitly so two
    # agents can live in one test process. `load_context_from_env` is what fills in the
    # `store_id` an approved-envelope fixture states only inside its envelope, and reading
    # the file with `json.loads` here instead would hand the runner a context the container
    # never sees -- one with no store_id, which every trust event then reads as misrouted.
    context = load_context_from_env({CONTEXT_ENV: str(ENVELOPES[store_id])})
    assert context is not None and context.get("store_id") == store_id
    app = create_app()
    configure_solicitation(app, context=context)
    received: list[str] = []

    @app.middleware("http")
    async def _record(request: Request, call_next: Any) -> Any:
        received.append(request.url.path)
        return await call_next(request)

    url = stack.enter_context(serve(app))
    yield Agent(store_id, url, app, received)


@pytest.fixture
def agents() -> Iterator[dict[str, Agent]]:
    """Both stores' agents, each a real ``store_agent.main:app`` on its own loopback port."""
    with contextlib.ExitStack() as stack:
        built = {}
        for store_id in ENVELOPES:
            built[store_id] = stack.enter_context(_agent(store_id, stack))
        yield built


def _address(agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    """Point the trust service at these agents through its real configuration key."""
    book = {agent.store_id: agent.url for agent in agents.values()}
    book.update(extra)
    monkeypatch.setenv(ENV_STORE_AGENT_ENDPOINTS, json.dumps(book))


def _feedback_event(event_id: str, store_id: str, observation_type: str = "contradicted") -> dict:
    return {
        "event_id": event_id,
        "ts": "2026-01-01T00:00:00Z",
        "kind": "feedback",
        "store_id": store_id,
        "order_ref": "ord-0001",
        "payload": {
            "dim": "feedback_match",
            "type": observation_type,
            "observed_at": "2026-01-01T00:00:00Z",
        },
    }


# =============================================================================================
# Arming. Both of these must pass for the four gates below to mean anything.
# =============================================================================================


def test_the_agents_under_test_serve_the_intake_and_start_with_no_posture(
    agents: dict[str, Agent],
) -> None:
    """An empty posture at the start, and a door to move it — or every gate below is vacuous.

    Two ways the sweep could go quiet rather than red, both observed in this repo: an agent
    that starts with a posture (so "it moved" is unfalsifiable), and an app whose route table
    is empty (so "store-beta received nothing" is true of every store, forever).
    """
    for agent in agents.values():
        assert agent.posture.signals == (), (
            f"{agent.store_id}'s runner already holds a posture before any event was pushed"
        )
        served = httpx.get(f"{agent.url}/openapi.json", timeout=10.0).json()["paths"]
        assert TRUST_EVENT_PATH in served, (
            f"{agent.store_id} serves {sorted(served)} and not {TRUST_EVENT_PATH}; the "
            f"'received nothing' assertions below would hold for a door that does not exist"
        )


@pytest.mark.docker
def test_an_unaddressed_deployment_pushes_nothing_and_still_appends(
    events_client: httpx.Client, agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no address book, the door behaves exactly as it did before R13 was wired.

    This is the default in every deployment that has not been told where a store's agent
    answers, and it is also the control for the privacy gate: it shows that a store agent
    receiving nothing is a *consequence of addressing*, not of a wire that never works.
    """
    monkeypatch.delenv(ENV_STORE_AGENT_ENDPOINTS, raising=False)
    response = events_client.post(
        "/events", json=_feedback_event("ev-wire-unaddressed", "store-alpha")
    )
    assert response.status_code == 201, response.text
    assert agents["store-alpha"].received == []
    assert agents["store-alpha"].posture.signals == ()


# =============================================================================================
# 1 + 2 + 3 — the chain runs, the number is the scorer's, and only one store hears about it
# =============================================================================================


@pytest.mark.docker
def test_a_served_event_moves_the_affected_agent_and_no_other(
    events_client: httpx.Client, agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /events -> delta -> push -> the affected agent's posture. And nobody else's.

    The store-beta half is the privacy property R13 states in ``feedback/engine.py``'s first
    paragraph, and it is asserted twice over: beta's runner holds no signal, AND beta's
    process recorded no inbound request at all. The second is the stronger claim — an agent
    that received the event and refused it would satisfy the first.
    """
    alpha, beta = agents["store-alpha"], agents["store-beta"]
    _address(agents, monkeypatch)

    event = _feedback_event("ev-wire-alpha-1", "store-alpha")
    response = events_client.post("/events", json=event)
    assert response.status_code == 201, response.text
    stored = response.json()["event"]

    signal = alpha.signal("feedback_match")
    assert signal is not None, (
        f"POST /events answered 201 and store-alpha's agent posture is "
        f"{alpha.posture.signals}; the affected store was not told what moved. "
        f"Requests that reached it: {alpha.received}"
    )
    assert signal.observations == 1
    assert signal.net_delta < 0, (
        f"a `contradicted` observation moved feedback_match by {signal.net_delta}, which is "
        f"not a penalty; the sign of the delta is the whole signal"
    )
    assert alpha.posture.stance == "guarded"

    # The number is the SCORER's, recomputed here from the served event rather than compared
    # to a literal: a hard-coded -0.1667 would keep passing after a manifest change that moved
    # what a contradiction costs, which is the drift D49 forbids.
    expected = delta_for_event(stored, history=[])
    assert expected is not None
    assert signal.net_delta == pytest.approx(expected["delta"], rel=0, abs=1e-12)
    assert expected["delta"] == pytest.approx(
        _mean_after(stored) - _mean_before(stored), rel=0, abs=1e-12
    )

    assert beta.posture.signals == (), (
        f"store-beta's agent absorbed a movement that belongs to store-alpha: "
        f"{beta.posture.signals}. R13 addresses the affected store and only the affected store"
    )
    assert TRUST_EVENT_PATH not in beta.received, (
        f"store-beta's process was CONTACTED about store-alpha's trust movement "
        f"({beta.received}); a competitor learning that a rival's score moved is the leak "
        f"R13 exists to prevent, whether or not the agent then refused it"
    )


def _mean_before(stored: dict[str, Any]) -> float:
    dims = score([], as_of=stored["ts"])["dims"]["feedback_match"]
    return float(dims["alpha"]) / (float(dims["alpha"]) + float(dims["beta"]))


def _mean_after(stored: dict[str, Any]) -> float:
    dims = score(observations_from_events([stored]), as_of=stored["ts"])["dims"]["feedback_match"]
    return float(dims["alpha"]) / (float(dims["alpha"]) + float(dims["beta"]))


@pytest.mark.docker
def test_the_delta_is_computed_against_the_store_s_own_history_and_shrinks(
    events_client: httpx.Client, agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second identical event moves the store LESS than the first.

    This is the assertion that separates "recompute the posture and diff it" from a lookup
    table. A Beta posterior that has already absorbed one contradiction moves less when it
    absorbs a second; a per-event price list would push the same number twice, and would agree
    with the scorer about neither.
    """
    alpha = agents["store-alpha"]
    _address(agents, monkeypatch)

    first = events_client.post("/events", json=_feedback_event("ev-wire-hist-1", "store-alpha"))
    assert first.status_code == 201, first.text
    after_one = alpha.signal("feedback_match").net_delta

    second = events_client.post("/events", json=_feedback_event("ev-wire-hist-2", "store-alpha"))
    assert second.status_code == 201, second.text
    signal = alpha.signal("feedback_match")
    assert signal.observations == 2, (
        "the second event did not reach the agent, so nothing below is measuring history"
    )

    second_delta = signal.net_delta - after_one
    assert second_delta < 0
    assert abs(second_delta) < abs(after_one), (
        f"the first contradiction moved feedback_match by {after_one} and the second by "
        f"{second_delta}; equal magnitudes mean the delta was priced from a table rather "
        f"than recomputed against the store's own posterior"
    )


@pytest.mark.docker
def test_a_duplicate_append_does_not_charge_the_store_twice(
    events_client: httpx.Client, agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D16 makes ``event_id`` the idempotency key. A retried delivery is not a second penalty."""
    alpha = agents["store-alpha"]
    _address(agents, monkeypatch)
    event = _feedback_event("ev-wire-dup-1", "store-alpha")

    assert events_client.post("/events", json=event).status_code == 201
    replay = events_client.post("/events", json=event)
    assert replay.status_code == 200, replay.text
    assert replay.headers["Idempotent-Replay"] == "true"

    signal = alpha.signal("feedback_match")
    assert signal.observations == 1, (
        f"one event was appended once and the agent counted it {signal.observations} times; a "
        f"producer retrying a delivery it already made would double-penalise the store"
    )


@pytest.mark.docker
def test_an_event_that_moves_no_dimension_notifies_nobody(
    events_client: httpx.Client, agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most of the ledger is `accepted` / `auction_opened`. Those move nothing and say nothing."""
    alpha = agents["store-alpha"]
    _address(agents, monkeypatch)

    response = events_client.post(
        "/events",
        json={
            "event_id": "ev-wire-quiet-1",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "accepted",
            "store_id": "store-alpha",
            "payload": {"offer_ref": "off-1"},
        },
    )
    assert response.status_code == 201, response.text
    assert alpha.received == [], (
        f"an event carrying no trust observation reached the store agent anyway ({alpha.received})"
    )
    assert alpha.posture.signals == ()


@pytest.mark.docker
def test_a_store_named_only_in_the_payload_gets_no_delta_rather_than_a_wrong_one(
    events_client: httpx.Client, agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No push at all, rather than a delta computed as if the store had no history.

    ``observations_from_events`` attributes an observation to ``event.store_id`` OR
    ``payload.store_id``; the ledger read that supplies the history is by the indexed
    ``store_id`` COLUMN, which the second spelling leaves null. So such an event would score
    against an empty history and tell a store with fifty observations that this one moved it
    as much as its first ever did. The event still lands; only the notification is withheld,
    and the reason is logged.
    """
    alpha = agents["store-alpha"]
    _address(agents, monkeypatch)

    # Prove there IS history to be wrong about, through the door, before the payload-only one.
    seeded = events_client.post("/events", json=_feedback_event("ev-wire-col-1", "store-alpha"))
    assert seeded.status_code == 201, seeded.text
    seeded_signal = alpha.signal("feedback_match")
    assert seeded_signal is not None and seeded_signal.observations == 1

    response = events_client.post(
        "/events",
        json={
            "event_id": "ev-wire-payload-store-1",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "feedback",
            "payload": {
                "store_id": "store-alpha",
                "dim": "feedback_match",
                "type": "contradicted",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        },
    )
    assert response.status_code == 201, response.text
    assert alpha.signal("feedback_match").observations == 1, (
        "a delta was pushed for an event whose store is stated only in its payload; the "
        "history behind it cannot be read, so the number would have been wrong"
    )


# =============================================================================================
# 4 — the send cannot break the door
# =============================================================================================


@pytest.mark.docker
def test_an_unreachable_store_agent_does_not_fail_the_append(
    events_client: httpx.Client, agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent is addressed at a port nothing is listening on. The event still lands.

    Losing a notification is bad; refusing an audit record because a downstream advocate was
    down is worse, and the ledger is the thing this service exists to protect.
    """
    _address(agents, monkeypatch, **{"store-alpha": f"http://127.0.0.1:{_closed_port()}"})
    response = events_client.post("/events", json=_feedback_event("ev-wire-down-1", "store-alpha"))

    assert response.status_code == 201, response.text
    assert events_client.get("/events/ev-wire-down-1").status_code == 200
    assert events_client.get("/events/verify").json()["ok"] is True


@pytest.mark.docker
def test_a_store_agent_answering_500_does_not_fail_the_append(
    events_client: httpx.Client, agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusing agent is an operational problem with a notification, not a bad append."""
    broken = FastAPI()

    @broken.post(TRUST_EVENT_PATH)
    def _explode() -> dict[str, str]:
        raise RuntimeError("this agent is broken")

    with contextlib.ExitStack() as stack:
        url = stack.enter_context(serve(broken))
        _address(agents, monkeypatch, **{"store-alpha": url})
        response = events_client.post(
            "/events", json=_feedback_event("ev-wire-500-1", "store-alpha")
        )

    assert response.status_code == 201, response.text
    assert events_client.get("/events/ev-wire-500-1").status_code == 200


@pytest.mark.docker
def test_a_slow_store_agent_does_not_hold_the_door_open(
    events_client: httpx.Client, agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that never answers is bounded by the push timeout, not by the client's patience.

    The bound asserted is deliberately loose (five seconds against a half-second timeout): the
    property is "a hung agent cannot hold ``POST /events`` open", and a tight assertion would
    make this test fail on a loaded machine for a reason that is not the property.
    """
    stalled = FastAPI()
    release = threading.Event()

    @stalled.post(TRUST_EVENT_PATH)
    def _hang() -> dict[str, str]:
        release.wait(timeout=30.0)
        return {"ok": "eventually"}

    with contextlib.ExitStack() as stack:
        stack.callback(release.set)
        url = stack.enter_context(serve(stalled))
        _address(agents, monkeypatch, **{"store-alpha": url})
        started = time.monotonic()
        response = events_client.post(
            "/events", json=_feedback_event("ev-wire-slow-1", "store-alpha")
        )
        elapsed = time.monotonic() - started

    assert response.status_code == 201, response.text
    assert elapsed < 5.0, (
        f"POST /events took {elapsed:.2f}s while a store agent hung; a slow advocate is "
        f"holding the ledger's door open"
    )


@pytest.mark.docker
def test_an_agent_that_refuses_a_misrouted_event_still_lets_the_append_stand(
    events_client: httpx.Client, agents: dict[str, Agent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Address store-alpha's delta at store-beta's agent: beta refuses, the ledger is unharmed.

    This drives the seal ``AgentRunner._accept`` enforces through the real door, over HTTP,
    which is the only place it had never been exercised. It also pins the direction of the
    failure: a misrouting bug shows up as a 409 and an unmoved posture, never as a neighbour
    quietly absorbing somebody else's evidence.
    """
    beta = agents["store-beta"]
    _address(agents, monkeypatch, **{"store-alpha": beta.url})

    response = events_client.post(
        "/events", json=_feedback_event("ev-wire-misroute-1", "store-alpha")
    )
    assert response.status_code == 201, response.text
    assert beta.posture.signals == (), (
        f"store-beta absorbed a delta addressed to store-alpha: {beta.posture.signals}"
    )
    assert TRUST_EVENT_PATH in beta.received, "the misrouted push never reached beta's door"


def _closed_port() -> int:
    """A loopback port that was bound and released, so a connection to it is refused fast.

    Bound and freed rather than picked at random: an arbitrary high port might belong to
    something else on the machine, and this test would then be measuring that instead.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
