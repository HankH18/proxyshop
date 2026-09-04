"""The HTTP half of R1, through the frozen entrypoint (T-071).

A unit test of ``clarify``/``confirm`` proves the functions behave. It does not prove they
are *reachable*, and it does not prove the ordering invariant survives the wire — which is
where it would actually be broken, by a handler that resolves the auction client before it
checks the confirmation flag.

So every test here goes through :func:`buyer_svc.main.create_app`, with a recording auction
client on ``app.state``, and asserts on what that client saw.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from apps.buyer.svc.src.intent import reset_confirmations
from apps.buyer.svc.src.intent.routes import set_buyer_llm

CLARIFY = "/buyer/intent/clarify"
CONFIRM = "/buyer/intent/confirm"


class RecordingExchange:
    """Every call lands in ``.calls``. Anything else it is asked for answers nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def create_auction(self, payload):
        self.calls.append(("create_auction", (payload,), {}))
        return {"auction_id": "auc-http-1"}


@pytest.fixture()
def exchange() -> RecordingExchange:
    return RecordingExchange()


@pytest.fixture()
def client(exchange):
    reset_confirmations()
    set_buyer_llm(None)
    main = importlib.import_module("buyer_svc.main")
    app = main.create_app()
    app.state.auction_client = exchange
    with TestClient(app) as test_client:
        yield test_client
    reset_confirmations()


def test_the_frozen_entrypoint_discovers_this_router() -> None:
    main = importlib.import_module("buyer_svc.main")
    assert "buyer_svc.intent.routes" in main.discover_router_modules()
    assert "buyer_svc.intent.routes" in main.create_app().state.mounted_routers


def test_clarifying_over_http_creates_no_auction(client, exchange) -> None:
    response = client.post(CLARIFY, json={"turns": ["hi, I need a gift", "not sure"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["questions"]) <= 3
    assert body["intent"]["query"]
    assert body["intent"]["budget_band"]
    assert body["confirmed"] is False
    assert exchange.calls == [], f"/clarify reached the exchange: {exchange.calls!r}"


def test_a_whole_clarify_dialogue_creates_no_auction(client, exchange) -> None:
    for turns in (
        ["hmm"],
        ["hmm", "not sure"],
        ["hmm", "not sure", "dunno"],
        ["hmm", "not sure", "dunno", "whatever", "no idea"],
        ["I want a light roast under $20"],
    ):
        assert client.post(CLARIFY, json={"turns": turns}).status_code == 200
    assert exchange.calls == [], f"clarifying opened {len(exchange.calls)} auction(s)"


def test_an_empty_turn_list_is_a_422(client, exchange) -> None:
    assert client.post(CLARIFY, json={"turns": []}).status_code == 422
    assert client.post(CLARIFY, json={"turns": ["  "]}).status_code == 422
    assert exchange.calls == []


def test_confirming_creates_exactly_one_auction(client, exchange) -> None:
    intent = client.post(CLARIFY, json={"turns": ["I want a light roast under $20"]}).json()[
        "intent"
    ]
    response = client.post(CONFIRM, json={"intent": intent, "confirmed": True})
    assert response.status_code == 201, response.text
    assert response.json()["auction_id"] == "auc-http-1"
    assert len(exchange.calls) == 1


def test_withholding_confirmation_over_http_creates_nothing(client, exchange) -> None:
    intent = client.post(CLARIFY, json={"turns": ["I want a light roast under $20"]}).json()[
        "intent"
    ]
    response = client.post(CONFIRM, json={"intent": intent, "confirmed": False})
    assert response.status_code == 409, response.text
    assert exchange.calls == []


def test_omitting_the_confirmation_flag_creates_nothing(client, exchange) -> None:
    """A body that forgot to say yes must not be read as one that did."""
    intent = client.post(CLARIFY, json={"turns": ["a wool scarf"]}).json()["intent"]
    assert client.post(CONFIRM, json={"intent": intent}).status_code == 409
    assert exchange.calls == []


@pytest.mark.parametrize(
    "value",
    ["maybe", "false", "yes", "true", "on", "1", "0", 1, 2, [], {}, None],
    ids=str,
)
def test_a_non_boolean_confirmation_never_reaches_the_exchange(client, exchange, value) -> None:
    """A confirmation that is not the boolean ``true`` must never open an auction.

    REGRESSION. ``confirmed`` was declared ``bool``, and pydantic's default lax mode
    coerces the JSON strings ``"yes"``, ``"true"``, ``"on"`` and ``"1"`` straight into
    ``True`` - so ``{"confirmed": "yes"}`` returned **HTTP 201 with a live auction id** and
    one real call to the exchange, for a body that carried no boolean at all. Measured, not
    hypothesised. The field is ``StrictBool`` now and every value below is refused.
    """
    intent = client.post(CLARIFY, json={"turns": ["a wool scarf"]}).json()["intent"]
    response = client.post(CONFIRM, json={"intent": intent, "confirmed": value})
    assert response.status_code in {409, 422}, response.text
    assert exchange.calls == [], f"confirmed={value!r} opened an auction"


def test_confirming_twice_over_http_is_refused(client, exchange) -> None:
    intent = client.post(CLARIFY, json={"turns": ["a wool scarf"]}).json()["intent"]
    assert client.post(CONFIRM, json={"intent": intent, "confirmed": True}).status_code == 201
    second = client.post(CONFIRM, json={"intent": intent, "confirmed": True})
    assert second.status_code == 409, second.text
    assert len(exchange.calls) == 1


def test_confirming_an_intent_that_was_never_clarified_is_refused(client, exchange) -> None:
    assert client.post(CONFIRM, json={"intent": {}, "confirmed": True}).status_code == 422
    assert (
        client.post(CONFIRM, json={"intent": {"query": "coffee"}, "confirmed": True}).status_code
        == 422
    )
    assert exchange.calls == []


def test_a_service_with_no_exchange_wired_says_so(client) -> None:
    intent = client.post(CLARIFY, json={"turns": ["a wool scarf"]}).json()["intent"]
    client.app.state.auction_client = None
    response = client.post(CONFIRM, json={"intent": intent, "confirmed": True})
    assert response.status_code == 503, response.text


def test_the_openapi_schema_documents_both_routes(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert CLARIFY in paths and CONFIRM in paths
