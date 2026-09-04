"""The HTTP half of R2/R3, through the frozen entrypoint (T-072).

A unit test of ``provenance_label``/``accept`` proves the functions behave. It does not
prove they are *reachable*, and the swarm-loop reachability checker cannot follow a
``@router.post`` decorator hop — it reports a route handler as UNREACHED whether or not the
app serves it. So reachability is proved the only way that is actually evidence: a real
request through :func:`buyer_svc.main.create_app`, with a recording exchange client on
``app.state``, asserting on what that client saw.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from apps.buyer.svc.src.accept import reset_accepted

RENDER = "/buyer/shortlist/render"
ACCEPT = "/buyer/shortlist/accept"

PERMALINK = "https://store-x.example.com/cart/44352913:1?discount=PS-ABC123"

SLOT = {
    "slot": "fit",
    "bid_ref": "bid-http-7",
    "auction_id": "auc-http-3",
    "fit_score": 0.91,
    "trust_summary": {"score": 0.72, "confidence": 0.4},
    "checkout_url": "https://attacker.example/cart/1:1?discount=PS-ABC123",
}


class RecordingExchange:
    """Every call lands in ``.calls``."""

    def __init__(self, result=None) -> None:
        self.calls: list[dict] = []
        self.result = result if result is not None else {"permalink_url": PERMALINK}

    def accept_offer(self, payload):
        self.calls.append(payload)
        return self.result


@pytest.fixture()
def exchange() -> RecordingExchange:
    return RecordingExchange()


@pytest.fixture()
def client(exchange):
    reset_accepted()
    main = importlib.import_module("buyer_svc.main")
    app = main.create_app()
    app.state.exchange_client = exchange
    with TestClient(app) as test_client:
        yield test_client
    reset_accepted()


def test_the_frozen_entrypoint_discovers_this_router() -> None:
    main = importlib.import_module("buyer_svc.main")
    assert "buyer_svc.accept.routes" in main.discover_router_modules()
    assert "buyer_svc.accept.routes" in main.create_app().state.mounted_routers


# --- rendering -------------------------------------------------------------------------


def test_rendering_a_shortlist_labels_every_slot_and_calls_nothing(client, exchange) -> None:
    response = client.post(
        RENDER,
        json={
            "shortlist": {
                "auction_id": "auc-http-3",
                "slots": [
                    {
                        "slot": "fit",
                        "bid_ref": "bid-1",
                        "fit_score": 0.8,
                        "trust_summary": {"score": 0.6},
                        "provenance_labels": ["store-confirmed"],
                    },
                    {
                        "slot": "value",
                        "bid_ref": "bid-2",
                        "fit_score": 0.7,
                        "trust_summary": {"score": 0.5},
                        "provenance_labels": ["from their website"],
                    },
                ],
            }
        },
    )
    assert response.status_code == 200
    slots = response.json()["slots"]
    assert [slot["provenance_labels"] for slot in slots] == [
        ["store-confirmed"],
        ["from their website"],
    ]
    assert all(slot["auction_id"] == "auc-http-3" for slot in slots)
    assert exchange.calls == []


def test_rendering_never_leaks_a_slots_checkout_url(client) -> None:
    response = client.post(
        RENDER, json={"shortlist": {"auction_id": "auc-http-3", "slots": [SLOT]}}
    )
    assert response.status_code == 200
    assert "attacker.example" not in response.text


@pytest.mark.parametrize("coercible", ["yes", "true", "1", "on", 1])
def test_a_boolean_field_admits_no_string_that_pydantic_would_coerce(client, coercible) -> None:
    """`StrictBool`. Measured on 2.13: a plain `bool` turns "yes" into True."""
    response = client.post(
        RENDER,
        json={"shortlist": {"auction_id": "a", "slots": []}, "derive_missing_labels": coercible},
    )
    assert response.status_code == 422


# --- accepting -------------------------------------------------------------------------


def test_accepting_over_http_follows_the_exchanges_permalink(client, exchange) -> None:
    response = client.post(ACCEPT, json={"slot": SLOT})
    assert response.status_code == 200
    body = response.json()
    assert body["permalink_url"] == PERMALINK
    assert exchange.calls == [{"auction_id": "auc-http-3", "bid_ref": "bid-http-7"}]
    assert "attacker.example" not in response.text


def test_the_route_hands_back_data_and_never_a_redirect(client) -> None:
    """An API that 302s is one whose callers cannot see where they are being sent."""
    response = client.post(ACCEPT, json={"slot": SLOT}, follow_redirects=False)
    assert response.status_code == 200
    assert "location" not in {key.lower() for key in response.headers}


def test_an_empty_auction_id_is_a_422_and_the_exchange_is_never_called(client, exchange) -> None:
    response = client.post(ACCEPT, json={"slot": {**SLOT, "auction_id": ""}})
    assert response.status_code == 422
    assert exchange.calls == []


def test_a_second_accept_is_a_409_carrying_the_permalink_already_issued(client) -> None:
    first = client.post(ACCEPT, json={"slot": SLOT})
    assert first.status_code == 200
    second = client.post(ACCEPT, json={"slot": SLOT})
    assert second.status_code == 409
    assert second.json()["detail"]["permalink_url"] == PERMALINK


def test_an_off_domain_permalink_is_a_502_not_a_redirect(client) -> None:
    client.app.state.exchange_client = RecordingExchange(
        {"permalink_url": "https://store-x.example.com.evil.tld/c"}
    )
    response = client.post(ACCEPT, json={"slot": SLOT, "expected_domain": "store-x.example.com"})
    assert response.status_code == 502


def test_an_exchange_that_answered_without_a_permalink_is_a_502(client) -> None:
    client.app.state.exchange_client = RecordingExchange({"code": "PS-ABC123"})
    response = client.post(ACCEPT, json={"slot": SLOT})
    assert response.status_code == 502


def test_a_denied_accept_is_a_409_with_the_exchanges_reason(client) -> None:
    client.app.state.exchange_client = RecordingExchange(
        {"accepted": False, "denial_reason": "blacklist"}
    )
    response = client.post(ACCEPT, json={"slot": SLOT})
    assert response.status_code == 409
    assert response.json()["detail"]["denial_reason"] == "blacklist"


def test_an_unwired_exchange_client_is_a_503_rather_than_a_500(client) -> None:
    """A deployment that forgot the composition root should hear about it as unavailable."""
    client.app.state.exchange_client = None
    response = client.post(ACCEPT, json={"slot": SLOT})
    assert response.status_code == 503
