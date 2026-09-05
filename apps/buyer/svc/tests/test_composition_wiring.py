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
        self._server: ThreadingHTTPServer | None = None

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
                status, answer = exchange.answers.get(self.path, exchange.default_answer)
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

    def build():
        return importlib.import_module("buyer_svc.main").create_app()

    yield build
    reset_confirmations()


def confirm_body(**overrides: Any) -> dict[str, Any]:
    body = {"intent": dict(INTENT), "confirmed": True}
    body.update(overrides)
    return body


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
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))

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
    document.write_text(json.dumps({"exchange_url": exchange.url}), encoding="utf-8")
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
    """
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)

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
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))
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
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))

    with TestClient(app_factory()) as client:
        response = client.post(CLARIFY, json={"turns": ["something for espresso, cheap"]})

    assert response.status_code == 200
    assert response.json()["confirmed"] is False
    assert exchange.requests == [], "clarifying spoke to the exchange"


def test_an_unconfirmed_intent_reaches_no_exchange_when_one_is_configured(
    app_factory, exchange, monkeypatch
) -> None:
    """R1's ordering invariant, now that there is a real client behind the seam."""
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))

    with TestClient(app_factory()) as client:
        response = client.post(CONFIRM, json=confirm_body(confirmed=False))

    assert response.status_code == 409
    assert exchange.requests == [], "an unconfirmed intent opened an auction"


def test_a_client_already_on_app_state_is_never_replaced(
    app_factory, exchange, monkeypatch
) -> None:
    """A deployment or a test that wires it itself wins, so this module can be adopted."""
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))

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

        monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))
        second = client.post(CONFIRM, json=confirm_body())

    assert second.status_code == 201, second.text
    assert exchange.paths == ["/auctions"]


def test_an_exchange_that_is_not_there_is_a_502_and_not_a_500(app_factory, monkeypatch) -> None:
    """With a real outbound client, an exchange outage is now reachable from a deployment."""
    # Port 1 on loopback: nothing binds it, so the connection is refused rather than hung.
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": "http://127.0.0.1:1"}))

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
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))
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
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))
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
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))
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

    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps({"exchange_url": exchange.url}))
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
