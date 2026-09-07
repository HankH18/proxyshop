"""The buyer service's composition root: a confirmed intent reaches a real exchange.

Before ``buyer_svc.composition`` existed, ``POST /buyer/intent/confirm`` answered **503** on
every deployed buyer service, because ``app.state.auction_client`` was read by the route and
set by nothing — measured over loopback against the real processes::

    >> POST http://127.0.0.1:60270/buyer/intent/confirm  -> 503
    "confirm() was given no auction client, so the confirmed intent has nowhere to go.
     Pass the exchange client that owns POST /auctions."

Every other test of ``confirm`` in this directory sets that attribute itself, which is why
none of them noticed: a test that wires the app it is testing is measuring the wiring it
wrote. So the tests here wire **nothing**. They set an environment variable — the only thing
an operator has — and let the service find the exchange on its own.

The exchange is a **real HTTP server on a real socket** rather than a stub object on
``app.state``, for the same reason. The defect was never in ``confirm()``; it was in the
chain between an environment variable and a socket, and every link of that chain
(``read_deployment`` → ``configure_buyer`` → ``HttpExchangeClient`` → ``httpx`` → the wire →
the parsed receipt) is inside these assertions. A double on ``app.state`` skips all of it.
"""

from __future__ import annotations

import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.buyer.svc.src.intent import reset_confirmations

CONFIRM = "/buyer/intent/confirm"
CLARIFY = "/buyer/intent/clarify"
ACCEPT = "/buyer/shortlist/accept"
AUCTION_VIEW = "/buyer/auctions"

#: The candidate set a deployment states, in the shape the exchange's own ``RosterEntry``
#: requires — ``store_id`` **and** a ``list_price`` above zero, because a row without one is
#: a 422 from ``POST /auctions`` rather than a store that competes.
ROSTER: list[dict[str, Any]] = [
    {"store_id": "demo-woolworks", "tier": 1, "product_ref": "beanie-1", "list_price": 80.0},
    {"store_id": "demo-northface", "tier": 1, "product_ref": "beanie-2", "list_price": 95.0},
    {"store_id": "demo-fastfashion", "tier": 2, "product_ref": "beanie-3", "list_price": 40.0},
]
ROSTERED_STORES = [row["store_id"] for row in ROSTER]

#: The 503 the unconfigured service answers, verbatim. Pinned as a whole sentence because it
#: is the thing an operator reads, and a reworded version of it is a different answer.
UNCONFIGURED_DETAIL = (
    "confirm() was given no auction client, so the confirmed intent has nowhere to go. "
    "Pass the exchange client that owns POST /auctions."
)

INTENT = {
    "intent_id": "int-comp-1",
    "query": "a light roast espresso bean",
    "budget_band": "0-50",
    "cluster_id": "cluster-espresso",
}


# =====================================================================================
# A real exchange, on a real socket
# =====================================================================================
class FakeExchange:
    """An HTTP server answering the exchange's two doors, recording what arrived.

    Deliberately **not** the real ``exchange.main:app``: what is under test is the buyer's
    half of the seam, and an assertion about ``POST /auctions`` having been *received, with
    this body, on this path* is stronger evidence about the buyer than any status the real
    exchange happens to answer. The real exchange is exercised end to end in this branch's
    loopback demonstration, which is quoted in ``composition.py``.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        #: ``{path_suffix: (status, body)}``; the default is a created auction.
        self.answers: dict[str, tuple[int, Any]] = {}
        self.default_answer: tuple[int, Any] = (201, {"auction_id": "auction-from-the-wire"})
        #: Opt-in, and OFF for every test that predates the roster gate. With it on,
        #: ``POST /auctions`` answers the way the real exchange was measured to answer —
        #: ``solicited`` / ``entries`` / ``ranked`` computed from the roster it was handed,
        #: and a live shortlist with one slot per rostered store. A roster of nothing
        #: therefore reproduces the audit's finding exactly, on the wire::
        #:
        #:     solicited: []  entries: []  ranked: []  slots: 0
        self.echo_roster = False
        #: ``{auction_id: shortlist}`` — what ``GET /auctions/{id}/shortlist`` serves.
        self.shortlists: dict[str, Any] = {}
        self._server: ThreadingHTTPServer | None = None

    def open_auction(self, body: Any) -> dict[str, Any]:
        """The answer a real exchange gives to the roster it was actually sent.

        Modelled on the measured shape, not an invented one: a store with no bid endpoint
        answers nothing and the exchange mints a list-price fallback entry for it, ranks it,
        and gives it a shortlist slot. Every one of those five arrays is derived from the
        roster, so an auction that named nobody produces the empty ones and no others.
        """
        roster = list((body or {}).get("roster") or [])
        auction_id = f"auction-{len(self.requests)}"
        solicited = [str(row.get("store_id") or "") for row in roster]
        self.shortlists[auction_id] = {
            "auction_id": auction_id,
            "slots": [
                {"slot": "fit", "bid_ref": f"{auction_id}:{store_id}", "store_id": store_id}
                for store_id in solicited
            ],
        }
        return {
            "auction_id": auction_id,
            "solicited": solicited,
            "entries": [
                {"store_id": store_id, "fallback": True, "fallback_reason": "no_response"}
                for store_id in solicited
            ],
            "ranked": [
                {"bid_ref": f"{auction_id}:{store_id}", "store_id": store_id, "rank_score": 0.56}
                for store_id in solicited
            ],
            "denied": [],
            "excluded": [],
            # Deliberately EMPTY, and different from the live door above: the buyer's auction
            # view may never serve the recorded shortlist as if it were the live one.
            "shortlist": {"auction_id": auction_id, "slots": []},
        }

    # -- lifecycle ------------------------------------------------------------------
    def start(self) -> str:
        exchange = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                exchange.requests.append({"path": self.path, "body": body})
                if self.path in exchange.answers:
                    status, answer = exchange.answers[self.path]
                elif exchange.echo_roster and self.path == "/auctions":
                    status, answer = 201, exchange.open_auction(body)
                else:
                    status, answer = exchange.default_answer
                self.reply(status, answer)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
                """``GET /auctions/{id}/shortlist`` — the LIVE half of the buyer's view."""
                exchange.requests.append({"path": self.path, "body": None})
                if self.path in exchange.answers:
                    status, answer = exchange.answers[self.path]
                else:
                    auction_id = self.path.removeprefix("/auctions/").removesuffix("/shortlist")
                    shortlist = exchange.shortlists.get(auction_id)
                    status, answer = (
                        (200, shortlist)
                        if shortlist is not None
                        else (404, {"detail": f"no shortlist for {auction_id!r}"})
                    )
                self.reply(status, answer)

            def reply(self, status: int, answer: Any) -> None:
                payload = (
                    answer
                    if isinstance(answer, (bytes, bytearray))
                    else json.dumps(answer).encode()
                )
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                """Silent. The assertions are the log."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- reading --------------------------------------------------------------------
    @property
    def paths(self) -> list[str]:
        return [call["path"] for call in self.requests]


@pytest.fixture()
def exchange():
    server = FakeExchange()
    server.url = server.start()  # type: ignore[attr-defined]
    yield server
    server.stop()


@pytest.fixture()
def app_factory(monkeypatch):
    """Build a buyer app the way ``uvicorn buyer_svc.main:app`` does — and wire nothing."""
    from apps.buyer.svc.src.intent.routes import set_buyer_llm

    reset_confirmations()
    set_buyer_llm(None)
    # Neither variable, unless a test sets one. Inherited environment would make "this
    # service is unconfigured" depend on the shell the suite was started from.
    monkeypatch.delenv("BUYER_DEPLOYMENT", raising=False)
    monkeypatch.delenv("BUYER_DEPLOYMENT_JSON", raising=False)
    monkeypatch.delenv("EXCHANGE_URL", raising=False)
    monkeypatch.delenv("BUYER_ROSTER", raising=False)
    monkeypatch.delenv("BUYER_ROSTER_JSON", raising=False)

    def build():
        return importlib.import_module("buyer_svc.main").create_app()

    yield build
    reset_confirmations()


def confirm_body(**overrides: Any) -> dict[str, Any]:
    body = {"intent": dict(INTENT), "confirmed": True}
    body.update(overrides)
    return body


def deployment(exchange_url: str, **overrides: Any) -> dict[str, Any]:
    """A deployment document that would actually run an auction.

    It states a ``roster``, and every test below that means "a deployment which works" goes
    through here. That is not decoration: a deployment naming no candidate set opens an
    auction that solicits nobody, and this service now refuses to open one rather than hand
    the shopper an empty shortlist. The tests in the roster section assert that refusal; the
    tests that are about something else state a roster so they keep testing what they test.
    """
    document = {"exchange_url": exchange_url, "roster": [dict(row) for row in ROSTER]}
    document.update(overrides)
    return document


# =====================================================================================
# The two halves of the requirement
# =====================================================================================
def test_an_unconfigured_buyer_service_still_refuses_exactly_as_it_did(app_factory) -> None:
    """The fail-closed default. This is the property the composition root may not break."""
    with TestClient(app_factory()) as client:
        response = client.post(CONFIRM, json=confirm_body())
    assert response.status_code == 503
    assert response.json()["detail"] == UNCONFIGURED_DETAIL


def test_a_configured_buyer_service_carries_the_confirmed_intent_to_the_exchange(
    app_factory, exchange, monkeypatch
) -> None:
    """The whole point. An environment variable, and the intent arrives on the wire.

    This is the test that fails if the wiring regresses: delete the ``_bind_the_deployment``
    call from ``confirm_route``, or the ``auction_client`` binding from ``configure_buyer``,
    and it goes back to the 503 the case above asserts.
    """
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url)))

    with TestClient(app_factory()) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 201, response.text
    assert response.json()["auction_id"] == "auction-from-the-wire"
    assert response.json()["intent_id"] == "int-comp-1"

    # It went to the exchange's own door, on a real socket, carrying the buyer's intent.
    assert exchange.paths == ["/auctions"]
    assert exchange.requests[0]["body"]["intent"]["query"] == INTENT["query"]
    assert exchange.requests[0]["body"]["intent"]["intent_id"] == "int-comp-1"


def test_the_document_may_be_a_file_as_well_as_an_inline_variable(
    app_factory, exchange, monkeypatch, tmp_path
) -> None:
    """``BUYER_DEPLOYMENT`` is the mounted-file half, and it is what compose would use."""
    document = tmp_path / "buyer-deployment.json"
    document.write_text(json.dumps(deployment(exchange.url)), encoding="utf-8")
    monkeypatch.setenv("BUYER_DEPLOYMENT", str(document))

    with TestClient(app_factory()) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 201, response.text
    assert exchange.paths == ["/auctions"]


def test_the_bare_exchange_url_the_compose_fragment_already_sets_is_enough(
    app_factory, exchange, monkeypatch
) -> None:
    """``apps/buyer/compose.yaml:47`` sets ``EXCHANGE_URL`` and nothing had ever read it.

    That is the variable the shipped ``docker compose up`` stack hands this service, with a
    comment saying what it is for. If only ``BUYER_DEPLOYMENT*`` were read, the composition
    root would be correct and the repository's own deployment still could not reach it.

    ``BUYER_ROSTER_JSON`` beside it because a bare origin says where the exchange is and
    nothing about who competes; the roster section below is where that half is asserted.
    """
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)
    monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(ROSTER))

    with TestClient(app_factory()) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 201, response.text
    assert exchange.paths == ["/auctions"]


def test_a_malformed_bare_exchange_url_is_the_same_503_as_one_inside_a_document(
    app_factory, monkeypatch
) -> None:
    """The bare origin takes the SAME validation, so a typo is loud wherever it was written."""
    monkeypatch.setenv("EXCHANGE_URL", "exchange:8083")

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert "EXCHANGE_URL=exchange:8083" in detail
    assert "must be http or https" in detail


def test_a_document_outranks_the_bare_url_so_two_sources_are_not_a_coin_toss(
    exchange, monkeypatch
) -> None:
    from apps.buyer.svc.src.composition import read_deployment

    deployment = read_deployment(
        {
            "BUYER_DEPLOYMENT_JSON": json.dumps({"exchange_url": "http://from-the-document:1"}),
            "EXCHANGE_URL": exchange.url,
        }
    )
    assert deployment is not None
    assert deployment.exchange_url == "http://from-the-document:1"


def test_a_client_explicitly_set_to_none_is_a_refusal_not_an_empty_slot(
    app_factory, exchange, monkeypatch
) -> None:
    """``app.state.auction_client = None`` means "no client", and configuration may not undo it.

    ``test_intent_routes.py::test_a_service_with_no_exchange_wired_says_so`` makes exactly this
    gesture to assert the 503. With an ambient ``EXCHANGE_URL`` now enough to configure this
    service, reading an explicit ``None`` as "unset" would bind a client over a caller that had
    said no — and would turn that existing test's answer into a 201 depending on the shell the
    suite was started from.
    """
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)

    app = app_factory()
    app.state.auction_client = None
    with TestClient(app) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == UNCONFIGURED_DETAIL
    assert exchange.requests == []
    assert app.state.auction_client is None


def test_the_configured_service_also_accepts_through_the_same_exchange(
    app_factory, exchange, monkeypatch
) -> None:
    """One document, both doors: the auction is opened and accepted on ONE exchange.

    Two urls would let a buyer accept an offer in an auction the accepting process has never
    heard of, so the two seams are bound from one value and this asserts they are.
    """
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url)))
    exchange.answers["/auctions/auction-from-the-wire/accept"] = (
        200,
        {"permalink_url": "https://s1.example.com/checkout/abc"},
    )

    app = app_factory()
    with TestClient(app) as client:
        created = client.post(CONFIRM, json=confirm_body())
        assert created.status_code == 201, created.text
        accepted = client.post(
            ACCEPT,
            json={
                "slot": {
                    "auction_id": created.json()["auction_id"],
                    "bid_ref": "bid-1",
                    "slot": "best_price",
                    "store_domain": "s1.example.com",
                }
            },
        )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["permalink_url"] == "https://s1.example.com/checkout/abc"
    assert exchange.paths == ["/auctions", "/auctions/auction-from-the-wire/accept"]
    assert exchange.requests[1]["body"]["bid_ref"] == "bid-1"
    # ONE client object on both attributes, not two clients that happen to agree today.
    assert app.state.auction_client is app.state.exchange_client


# =====================================================================================
# What the composition root must NOT do
# =====================================================================================
def test_clarifying_reaches_no_exchange_even_when_one_is_configured(
    app_factory, exchange, monkeypatch
) -> None:
    """R1 survives the composition root: ``/clarify`` takes no hook and calls nobody."""
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url)))

    with TestClient(app_factory()) as client:
        response = client.post(CLARIFY, json={"turns": ["something for espresso, cheap"]})

    assert response.status_code == 200
    assert response.json()["confirmed"] is False
    assert exchange.requests == [], "clarifying spoke to the exchange"


def test_an_unconfirmed_intent_reaches_no_exchange_when_one_is_configured(
    app_factory, exchange, monkeypatch
) -> None:
    """R1's ordering invariant, now that there is a real client behind the seam."""
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url)))

    with TestClient(app_factory()) as client:
        response = client.post(CONFIRM, json=confirm_body(confirmed=False))

    assert response.status_code == 409
    assert exchange.requests == [], "an unconfirmed intent opened an auction"


def test_a_client_already_on_app_state_is_never_replaced(
    app_factory, exchange, monkeypatch
) -> None:
    """A deployment or a test that wires it itself wins, so this module can be adopted."""
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url)))

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def create_auction(self, payload: Any) -> dict[str, str]:
            self.calls.append(payload)
            return {"auction_id": "auc-from-the-double"}

    wired = Recorder()
    app = app_factory()
    app.state.auction_client = wired
    with TestClient(app) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 201, response.text
    assert response.json()["auction_id"] == "auc-from-the-double"
    assert len(wired.calls) == 1
    assert exchange.requests == [], "the composition root overrode a wired client"
    assert app.state.auction_client is wired


# =====================================================================================
# Misconfiguration is a 503 naming the row; an upstream failure is a 502
# =====================================================================================
@pytest.mark.parametrize(
    ("document", "must_name"),
    [
        ({"exchange_uri": "http://x:1"}, "exchange_uri"),
        ({}, "exchange_url"),
        ({"exchange_url": "exchange:8083"}, "must be http or https"),
        ({"exchange_url": "http://x:1/?a=1"}, "query string"),
        ({"exchange_url": "http://x:1", "request_timeout_seconds": True}, "got bool"),
        ({"exchange_url": "http://x:1", "request_timeout_seconds": 0}, "positive, finite"),
        ({"exchange_url": ""}, "exchange_url is empty"),
        ({"exchange_url": 7}, "must be a string"),
    ],
)
def test_a_malformed_document_is_a_503_naming_the_problem(
    app_factory, monkeypatch, document, must_name
) -> None:
    """Never a 500. A misconfigured service is not a decision about this buyer."""
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(document))

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 503, response.text
    assert must_name in response.json()["detail"]


def test_a_document_that_is_not_json_is_a_503_rather_than_a_500(app_factory, monkeypatch) -> None:
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", "{not json at all")

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 503
    assert "not valid JSON" in response.json()["detail"]


def test_a_named_but_missing_file_is_a_503_and_not_read_as_unconfigured(
    app_factory, monkeypatch, tmp_path
) -> None:
    """A path that does not resolve is a mistake, not a deployment that said nothing."""
    monkeypatch.setenv("BUYER_DEPLOYMENT", str(tmp_path / "nope.json"))

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 503
    assert "could not be read" in response.json()["detail"]
    # NOT the unconfigured message: those are two different states and the operator needs to
    # be able to tell them apart.
    assert response.json()["detail"] != UNCONFIGURED_DETAIL


def test_a_failure_is_not_cached_so_a_fixed_document_serves_the_next_request(
    app_factory, exchange, monkeypatch
) -> None:
    """An operator who fixes the file is served without restarting the process."""
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_uri": "http://x:1"}))
    app = app_factory()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(CONFIRM, json=confirm_body()).status_code == 503

        monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url)))
        second = client.post(CONFIRM, json=confirm_body())

    assert second.status_code == 201, second.text
    assert exchange.paths == ["/auctions"]


def test_an_exchange_that_is_not_there_is_a_502_and_not_a_500(app_factory, monkeypatch) -> None:
    """With a real outbound client, an exchange outage is now reachable from a deployment."""
    # Port 1 on loopback: nothing binds it, so the connection is refused rather than hung.
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment("http://127.0.0.1:1")))

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 502, response.text
    assert "failed before an answer arrived" in response.json()["detail"]


def test_a_non_2xx_from_post_auctions_is_a_502_rather_than_a_201_naming_no_auction(
    app_factory, exchange, monkeypatch
) -> None:
    """The measured hazard behind the per-door status rule.

    ``confirm()`` treats a response carrying no ``auction_id`` as "the exchange accepted the
    auction and returned no id" — it logs a WARNING and answers **201** with an empty
    ``auction_id``. A client that returned the exchange's 422 body verbatim would therefore
    turn a refusal into a created auction that names nothing, and the buyer would be handed a
    receipt for an auction that does not exist.
    """
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url)))
    exchange.answers["/auctions"] = (422, {"detail": "intent.hard_constraints carries 999 entries"})

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert "the exchange answered 422" in detail
    assert "hard_constraints" in detail


def test_the_exchanges_409_on_accept_is_returned_as_a_refusal_not_raised_as_a_failure(
    app_factory, exchange, monkeypatch
) -> None:
    """The accept door's exception, and it is the exchange's own documented answer.

    ``{"accepted": false, "denial_reason": ...}`` at 409 is a decision about this buyer's
    offer. Raising on it would answer 502 and blame the exchange for working correctly.
    """
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url)))
    exchange.answers["/auctions/auc-9/accept"] = (
        409,
        {"accepted": False, "denial_reason": "blacklist: store s1 is blacklisted"},
    )

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(
            ACCEPT,
            json={"slot": {"auction_id": "auc-9", "bid_ref": "bid-1", "slot": "best_price"}},
        )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["denial_reason"] == "blacklist: store s1 is blacklisted"


def test_a_404_on_accept_is_a_502_because_it_is_not_a_decision_about_this_buyer(
    app_factory, exchange, monkeypatch
) -> None:
    """The other side of the same rule: a gone auction is the upstream's state, not a denial."""
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url)))
    exchange.answers["/auctions/auc-gone/accept"] = (404, {"detail": "no such auction"})

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(
            ACCEPT,
            json={"slot": {"auction_id": "auc-gone", "bid_ref": "bid-1", "slot": "best_price"}},
        )

    assert response.status_code == 502, response.text
    assert "answered 404" in response.json()["detail"]


def test_an_oversized_answer_is_refused_rather_than_read_into_memory(
    app_factory, exchange, monkeypatch
) -> None:
    """The response cap is a real cap: it stops the read, and it answers rather than truncating.

    Driven by lowering the ceiling rather than by sending 4 MiB, because what is under test is
    the branch, not the arithmetic — and a test that allocates the real bound to prove the
    real bound is a test that measures this machine's memory.
    """
    from apps.buyer.svc.src import composition

    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url)))
    monkeypatch.setattr(composition, "MAX_EXCHANGE_RESPONSE_BYTES", 16)
    exchange.answers["/auctions"] = (201, {"auction_id": "a" * 200})

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 502, response.text
    assert "exceeded 16 bytes" in response.json()["detail"]


# =====================================================================================
# The seams this module reaches through, pinned
# =====================================================================================
def test_the_state_attributes_are_the_ones_the_routes_actually_read() -> None:
    """``composition`` spells these rather than importing them; this is that duplication's guard.

    Importing the route modules back into ``composition`` would be a cycle, and importing them
    lazily to read two strings would drag FastAPI into every consumer of ``read_deployment``.
    So the names are written twice and pinned here — rename one and this fails, instead of a
    deployed service binding a client to an attribute nobody reads.
    """
    from apps.buyer.svc.src import composition
    from apps.buyer.svc.src.accept.routes import EXCHANGE_CLIENT_ATTR
    from apps.buyer.svc.src.intent.routes import AUCTION_CLIENT_ATTR

    assert composition.AUCTION_CLIENT_ATTR == AUCTION_CLIENT_ATTR
    assert composition.EXCHANGE_CLIENT_ATTR == EXCHANGE_CLIENT_ATTR


def test_the_module_is_one_object_under_both_spellings_of_this_tree() -> None:
    """Two ``ExchangeCallFailed`` classes from one ``class`` statement is a 500 waiting.

    ``apps/buyer/svc/src`` is importable as ``buyer_svc.<mod>`` and as
    ``apps.buyer.svc.src.<mod>``; left alone Python executes each file twice, and an
    ``except ExchangeCallFailed`` written against one spelling does not catch the one the
    other raises — which would take the route's 502 mapping out of the path entirely.
    """
    import buyer_svc.composition as through_pkgroot

    from apps.buyer.svc.src import composition as through_repo_root

    assert through_pkgroot is through_repo_root
    assert through_pkgroot.ExchangeCallFailed is through_repo_root.ExchangeCallFailed


def test_the_client_satisfies_both_published_client_contracts() -> None:
    """One object, two doors — and the names are the ones each package looks for FIRST.

    ``confirm`` and ``accept`` each walk a tuple of method names and take the first callable
    one. If ``HttpExchangeClient`` grew a method earlier in either tuple, that method would
    silently become the door.
    """
    from apps.buyer.svc.src.accept.handoff import EXCHANGE_ACCEPT_METHODS
    from apps.buyer.svc.src.composition import HttpExchangeClient
    from apps.buyer.svc.src.intent.confirmation import AUCTION_CLIENT_METHODS

    client = HttpExchangeClient("http://exchange:8083")
    assert next(n for n in AUCTION_CLIENT_METHODS if callable(getattr(client, n, None))) == (
        "create_auction"
    )
    assert next(n for n in EXCHANGE_ACCEPT_METHODS if callable(getattr(client, n, None))) == (
        "accept_offer"
    )


def test_building_a_client_opens_no_socket_and_imports_no_http_library() -> None:
    """A config check may construct one; constructing one must cost nothing."""
    from apps.buyer.svc.src.composition import HttpExchangeClient

    client = HttpExchangeClient("http://exchange:8083/")
    assert client.base_url == "http://exchange:8083", "the trailing slash would make //auctions"
    assert client._client is None


def test_read_deployment_answers_none_when_nothing_is_configured() -> None:
    """The fail-closed default, at the unit that decides it."""
    from apps.buyer.svc.src.composition import read_deployment

    assert read_deployment({}) is None
    assert read_deployment({"BUYER_DEPLOYMENT": "", "BUYER_DEPLOYMENT_JSON": "  "}) is None


def test_the_path_variable_outranks_the_inline_one(tmp_path) -> None:
    """Documented precedence, asserted, so a container setting both is not a coin toss."""
    from apps.buyer.svc.src.composition import read_deployment

    document = tmp_path / "d.json"
    document.write_text(json.dumps({"exchange_url": "http://from-the-file:1"}), encoding="utf-8")
    deployment = read_deployment(
        {
            "BUYER_DEPLOYMENT": str(document),
            "BUYER_DEPLOYMENT_JSON": json.dumps({"exchange_url": "http://from-the-variable:1"}),
        }
    )
    assert deployment is not None
    assert deployment.exchange_url == "http://from-the-file:1"


def test_an_oversized_document_is_refused_rather_than_parsed_on_the_request_path() -> None:
    from apps.buyer.svc.src.composition import (
        MAX_DEPLOYMENT_BYTES,
        DeploymentConfigurationError,
        read_deployment,
    )

    padded = json.dumps({"exchange_url": "http://x:1" + " " * (MAX_DEPLOYMENT_BYTES + 10)})
    with pytest.raises(DeploymentConfigurationError, match="reads at most"):
        read_deployment({"BUYER_DEPLOYMENT_JSON": padded})


# =====================================================================================
# The roster: a deployment that names nobody opens an auction that solicits nobody
# =====================================================================================
# MEASURED on the real stack — `uvicorn buyer_svc.main:app` and `uvicorn exchange.main:app`,
# both on loopback, the buyer configured with EXCHANGE_URL and nothing else:
#
#     POST /buyer/intent/confirm                       -> 201  {"auction_id": "auction-945c..."}
#     GET  /buyer/auctions/auction-945c...             -> 200
#     {"solicited": [], "entries": [], "ranked": [], "denied": [], "slots": 0}
#
# The same process, with a roster in the confirm body, on the same two servers:
#
#     {"solicited": ["demo-woolworks", "demo-northface", "demo-fastfashion"],
#      "entries": [... 3 ...], "ranked": [... 3 ...], "slots": 3}
#
# So the mechanism is served and correct and the buyer deployable was missing the one
# configuration source that feeds it. These tests are that source, and the refusal that
# replaces the cheerful empty auction when it resolves nothing.
def test_a_deployment_that_names_no_roster_refuses_rather_than_soliciting_nobody(
    app_factory, exchange, monkeypatch
) -> None:
    """FAIL-CLOSED, not fail-empty. The audit's exact deployment: EXCHANGE_URL, alone.

    Before this gate the service answered 201 and the shopper got ``slots: 0`` — which is
    indistinguishable from "no store had anything for you". It is a 503 naming both variables
    that would fix it, and **nothing is sent to the exchange**: an auction nobody can win is
    not worth opening, and opening it would burn an auction id and a ledger claim.
    """
    exchange.echo_roster = True
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert "roster" in detail
    assert "BUYER_ROSTER" in detail and "BUYER_ROSTER_JSON" in detail
    # It names the deployment that came up short, so an operator knows which one to edit.
    assert f"EXCHANGE_URL={exchange.url}" in detail
    assert exchange.requests == [], "an auction that solicits nobody was opened anyway"
    # NOT the unconfigured message: a service with an exchange and no roster is a different
    # state from a service with no exchange, and the operator has to be able to tell them apart.
    assert detail != UNCONFIGURED_DETAIL


def test_the_roster_variable_makes_a_bare_exchange_url_deployment_open_a_real_auction(
    app_factory, exchange, monkeypatch
) -> None:
    """The whole point: EXCHANGE_URL + BUYER_ROSTER_JSON is a complete buyer deployment.

    Driven the way an operator drives it — two environment variables — and read back through
    the served auction view, so what is asserted is the JSON a shopper's page receives.
    """
    exchange.echo_roster = True
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)
    monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(ROSTER))

    with TestClient(app_factory()) as client:
        created = client.post(CONFIRM, json=confirm_body())
        assert created.status_code == 201, created.text
        auction_id = created.json()["auction_id"]
        view = client.get(f"{AUCTION_VIEW}/{auction_id}")

    assert view.status_code == 200, view.text
    seen = view.json()
    assert seen["solicited"] == ROSTERED_STORES
    assert [entry["store_id"] for entry in seen["entries"]] == ROSTERED_STORES
    assert [row["store_id"] for row in seen["ranked"]] == ROSTERED_STORES
    assert len(seen["shortlist"]["slots"]) == len(ROSTER)
    # The roster reached the exchange's own door, on the wire, unabridged.
    opened = next(call for call in exchange.requests if call["path"] == "/auctions")
    assert opened["body"]["roster"] == ROSTER


def test_the_roster_may_be_a_mounted_file_as_well_as_an_inline_variable(
    app_factory, exchange, monkeypatch, tmp_path
) -> None:
    """``BUYER_ROSTER`` is the mounted-file half — the shape a container actually uses.

    A roster is data with a row per store; ``EXCHANGE_DEPLOYMENT`` is mounted for the same
    reason, and a 500-row candidate set does not belong in an environment variable.
    """
    document = tmp_path / "buyer-roster.json"
    document.write_text(json.dumps(ROSTER), encoding="utf-8")
    exchange.echo_roster = True
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)
    monkeypatch.setenv("BUYER_ROSTER", str(document))

    with TestClient(app_factory()) as client:
        created = client.post(CONFIRM, json=confirm_body())

    assert created.status_code == 201, created.text
    opened = next(call for call in exchange.requests if call["path"] == "/auctions")
    assert opened["body"]["roster"] == ROSTER


def test_the_roster_document_may_wrap_its_rows_the_way_the_deployment_document_does(
    app_factory, exchange, monkeypatch
) -> None:
    """``{"roster": [...]}`` and a bare ``[...]`` are the same statement.

    One file has to serve both variables and both spellings, or an operator who copied the
    ``roster`` key out of the deployment document gets a 503 for being consistent.
    """
    exchange.echo_roster = True
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)
    monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps({"roster": ROSTER}))

    with TestClient(app_factory()) as client:
        created = client.post(CONFIRM, json=confirm_body())

    assert created.status_code == 201, created.text
    opened = next(call for call in exchange.requests if call["path"] == "/auctions")
    assert opened["body"]["roster"] == ROSTER


def test_a_roster_in_the_confirm_body_still_opens_the_auction_it_always_did(
    app_factory, exchange, monkeypatch
) -> None:
    """POSITIVE CONTROL. The refusal is about a deployment resolving nothing, not about bodies.

    ``create_auction`` honours a payload that names its own roster — that is how the devstack
    and ``e2e`` drive a different candidate set — and this asserts the new gate did not close
    over that path while closing the empty one.
    """
    exchange.echo_roster = True
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)

    with TestClient(app_factory()) as client:
        created = client.post(CONFIRM, json=confirm_body(roster=ROSTER))
        assert created.status_code == 201, created.text
        view = client.get(f"{AUCTION_VIEW}/{created.json()['auction_id']}")

    assert view.status_code == 200, view.text
    assert view.json()["solicited"] == ROSTERED_STORES
    assert len(view.json()["shortlist"]["slots"]) == len(ROSTER)


def test_the_documents_roster_outranks_the_environments_so_two_sources_are_not_a_coin_toss(
    app_factory, exchange, monkeypatch
) -> None:
    """Document, then environment — the order ``exchange.composition`` resolves ``trust_url``."""
    stated = [{"store_id": "from-the-document", "tier": 1, "list_price": 5.0}]
    exchange.echo_roster = True
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(deployment(exchange.url, roster=stated)))
    monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(ROSTER))

    with TestClient(app_factory()) as client:
        created = client.post(CONFIRM, json=confirm_body())

    assert created.status_code == 201, created.text
    opened = next(call for call in exchange.requests if call["path"] == "/auctions")
    assert opened["body"]["roster"] == stated


def test_a_document_that_states_no_roster_falls_through_to_the_environment(
    app_factory, exchange, monkeypatch
) -> None:
    """The middle rung. A document is not an all-or-nothing statement about the roster."""
    exchange.echo_roster = True
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))
    monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(ROSTER))

    with TestClient(app_factory()) as client:
        created = client.post(CONFIRM, json=confirm_body())

    assert created.status_code == 201, created.text
    opened = next(call for call in exchange.requests if call["path"] == "/auctions")
    assert opened["body"]["roster"] == ROSTER


def test_a_refused_confirmation_does_not_burn_the_intent(
    app_factory, exchange, monkeypatch
) -> None:
    """The claim is released, so configuring the roster serves the SAME shopper's need.

    ``confirm`` claims ``intent_id`` before it calls the client and one intent opens one
    auction, so a refusal that spent the claim would answer 409 for the life of the process —
    the operator would fix the deployment and the buyer would still be refused.
    """
    exchange.echo_roster = True
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)

    app = app_factory()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(CONFIRM, json=confirm_body()).status_code == 503

        monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(ROSTER))
        # The SAME app and the SAME intent id. Two properties in one request: a bind that
        # resolved no roster is not remembered, so this one re-reads the environment and
        # replaces the client it built; and the claim the refused confirmation made was
        # given back, so the id is still spendable.
        second = client.post(CONFIRM, json=confirm_body())

    assert second.status_code == 201, second.text
    assert second.json()["intent_id"] == INTENT["intent_id"]
    opened = next(call for call in exchange.requests if call["path"] == "/auctions")
    assert opened["body"]["roster"] == ROSTER


@pytest.mark.parametrize(
    ("roster", "must_name"),
    [
        pytest.param([{"store_id": "s1", "tier": 1}], "list_price", id="no-list-price"),
        pytest.param([{"store_id": "s1", "list_price": 0}], "list_price", id="priced-at-nothing"),
        pytest.param([{"store_id": "s1", "list_price": -1.0}], "list_price", id="negative-price"),
        pytest.param([{"store_id": "s1", "list_price": True}], "list_price", id="price-is-a-bool"),
        pytest.param([{"store_id": "s1", "list_price": "80"}], "list_price", id="price-is-a-str"),
        pytest.param([{"tier": 1, "list_price": 9.0}], "store_id", id="no-store-id"),
        pytest.param(
            [{"store_id": "s1", "list_price": 9.0, "max_discount_pct": 150}],
            "max_discount_pct",
            id="discount-out-of-range",
        ),
        pytest.param({"store_id": "s1"}, "must be a JSON array", id="not-an-array"),
    ],
)
def test_a_roster_row_the_exchange_would_refuse_is_a_503_here_naming_the_row(
    app_factory, exchange, monkeypatch, roster, must_name
) -> None:
    """Refused ONCE, at configuration, instead of as a 422 on every confirmation.

    Each of these is a real ``RosterEntry`` refusal on the exchange's own door — ``list_price``
    is ``Field(gt=0.0, allow_inf_nan=False)`` and required there. A buyer service that passes
    them through looks configured and answers 502 to every shopper, with a detail quoting a
    pydantic error about a service the operator did not write.
    """
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)
    monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(roster))

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert must_name in detail, detail
    assert "BUYER_ROSTER_JSON" in detail, "the refusal must name the source it came from"
    assert exchange.requests == []


def test_a_roster_file_that_is_not_there_is_a_503_and_not_read_as_no_roster(
    app_factory, exchange, monkeypatch, tmp_path
) -> None:
    """A named-but-missing path is a mistake, not a deployment that stated nothing."""
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)
    monkeypatch.setenv("BUYER_ROSTER", str(tmp_path / "nope.json"))

    with TestClient(app_factory(), raise_server_exceptions=False) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 503, response.text
    assert "could not be read" in response.json()["detail"]


def test_the_roster_path_variable_outranks_the_inline_one(tmp_path) -> None:
    """Documented precedence, asserted, so a container setting both is not a coin toss."""
    from apps.buyer.svc.src.composition import ENV_EXCHANGE_URL, ENV_ROSTER, read_deployment

    document = tmp_path / "roster.json"
    document.write_text(json.dumps([{"store_id": "from-the-file", "list_price": 1.0}]), "utf-8")
    resolved = read_deployment(
        {
            ENV_EXCHANGE_URL: "http://exchange:8083",
            ENV_ROSTER: str(document),
            "BUYER_ROSTER_JSON": json.dumps([{"store_id": "from-the-variable", "list_price": 1.0}]),
        }
    )
    assert resolved is not None
    assert [row["store_id"] for row in resolved.roster] == ["from-the-file"]
    assert str(document) in resolved.roster_source


def test_a_bare_client_nobody_deployed_still_sends_what_it_was_given() -> None:
    """The refusal belongs to a DEPLOYMENT, not to the transport.

    ``HttpExchangeClient`` is the object that knows the exchange's routing; it is not the
    object that judges whether a deployment is complete. One built by hand — by a test, or by
    a caller composing its own — carries no deployment source and keeps the pass-through
    behaviour ``test_composition.py`` pins. Every client the composition root builds carries
    one, which is what makes the gate reachable from a deployment and only from a deployment.
    """
    from apps.buyer.svc.src.composition import HttpExchangeClient, NoRosterBound

    assert HttpExchangeClient("http://exchange:8083").deployment_source == ""
    bound = HttpExchangeClient("http://exchange:8083", deployment_source="EXCHANGE_URL=...")
    with pytest.raises(NoRosterBound, match="roster"):
        bound.create_auction({"intent": {"intent_id": "i-1"}})


def test_the_roster_refusal_is_the_503_the_frozen_confirm_route_answers() -> None:
    """``NoRosterBound`` is an ``AuctionClientUnusable`` and that is load-bearing.

    ``intent/routes.py`` — which this lane does not own — maps ``AuctionClientUnusable`` to
    503 and ``ExchangeCallFailed`` to 502. This condition is the service's configuration and
    not the exchange's health, so 503 is the honest answer and this is the inheritance that
    produces it. Break the base class and the served answer silently becomes "bad gateway",
    which sends an operator to read the exchange's logs about a buyer-side misconfiguration.
    """
    from apps.buyer.svc.src.composition import ExchangeCallFailed, NoRosterBound
    from apps.buyer.svc.src.intent.errors import AuctionClientUnusable

    assert issubclass(NoRosterBound, AuctionClientUnusable)
    assert not issubclass(NoRosterBound, ExchangeCallFailed)


def test_a_roster_alone_configures_nothing_and_the_service_still_refuses(
    app_factory, exchange, monkeypatch
) -> None:
    """NEGATIVE CONTROL. A candidate set is not an exchange address.

    The failure mode a fix like this invites is admitting more than it should: a roster
    variable that also counted as "configured" would bind a client to nowhere, and the
    fail-closed default this module may not break — a buyer service nobody configured must
    not invent an exchange — would be broken by the repair meant to strengthen it.
    """
    monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(ROSTER))

    with TestClient(app_factory()) as client:
        response = client.post(CONFIRM, json=confirm_body())

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == UNCONFIGURED_DETAIL
    assert exchange.requests == []


def test_an_oversized_roster_document_is_refused_rather_than_parsed_on_the_request_path() -> None:
    """Its own ceiling, and a larger one than the deployment document's, on purpose.

    A 500-row roster — the most the exchange accepts — does not fit in ``MAX_DEPLOYMENT_BYTES``,
    so sharing that cap would make the documented maximum unreachable.
    """
    from apps.buyer.svc.src.composition import (
        MAX_DEPLOYMENT_BYTES,
        MAX_ROSTER_BYTES,
        DeploymentConfigurationError,
        read_roster,
    )

    assert MAX_ROSTER_BYTES > MAX_DEPLOYMENT_BYTES
    padded = json.dumps([{"store_id": "s" + " " * MAX_ROSTER_BYTES, "list_price": 1.0}])
    with pytest.raises(DeploymentConfigurationError, match="reads at most"):
        read_roster({"BUYER_ROSTER_JSON": padded})


def test_a_full_length_roster_is_accepted_and_one_row_longer_is_not() -> None:
    """The cap an operator can actually reach, exercised at both ends.

    ``MAX_ROSTER_ENTRIES`` is the exchange's own ``CreateAuctionRequest`` ceiling; a roster
    that fits it must parse, or this service refuses a deployment the exchange would accept.
    """
    from apps.buyer.svc.src.composition import (
        MAX_ROSTER_ENTRIES,
        DeploymentConfigurationError,
        read_roster,
    )

    rows = [{"store_id": f"s{index}", "list_price": 9.99} for index in range(MAX_ROSTER_ENTRIES)]
    roster, source = read_roster({"BUYER_ROSTER_JSON": json.dumps(rows)})
    assert len(roster) == MAX_ROSTER_ENTRIES
    assert source == "BUYER_ROSTER_JSON"

    with pytest.raises(DeploymentConfigurationError, match=f"at most {MAX_ROSTER_ENTRIES}"):
        read_roster({"BUYER_ROSTER_JSON": json.dumps([*rows, {"store_id": "one-too-many"}])})
