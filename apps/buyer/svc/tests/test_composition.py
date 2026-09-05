"""The buyer service, driven as it BOOTS: no test-side wiring on the path that matters.

Run it on its own::

    PROXYSHOP_WORKER=10 .venv/bin/python -m pytest \\
        apps/buyer/svc/tests/test_composition.py -q

Why this file exists
--------------------
``apps/buyer/svc/src/accept/routes.py`` names the defect in its own docstring: the exchange
client is read off ``app.state`` and *"nothing in this repository sets that attribute yet —
the buyer→exchange seam has no composition root on either side."* Every green buyer test in
this tree supplies that join itself — ``test_intent_routes.py`` assigns
``app.state.auction_client = RecordingExchange()`` in its fixture, ``test_accept_routes.py``
does the same for ``exchange_client`` — and, as ``apps/exchange/tests/test_composition_root.py``
puts it, *a test that wires the app it is testing is measuring the wiring it wrote.*

So the property this file exists for is:

    **on the served path, the only thing that builds the app is ``create_app()``, and
    everything it needs it reads for itself out of the deployment document.**

Stated exactly, because an overclaiming docstring is how the next reader stops looking:
:func:`configure_buyer` — the composition root's OWN function, not an ``app.state``
assignment — is called directly by
``test_the_composition_root_never_overwrites_wiring_somebody_already_chose``, which is a unit
test of that function and issues no request. Every HTTP test here goes through
``composition.create_app()`` with nothing but the environment set, and the two that matter
most drive it over a real loopback socket
(:func:`proxyshop_support.asgi_server.serve`, D40: port 0, reported back) rather than through
``TestClient`` — in-process ASGI transport is exactly the boundary at which this class of
defect hides, because the app object is handed to the test rather than started.

What is real here
-----------------
* the app: ``buyer_svc.composition.create_app()``, which is ``buyer_svc.main.create_app()``
  plus this module's wiring plus the UI mount;
* the exchange: a real ASGI service on a real loopback port, answering the three doors this
  client knows (``POST /auctions``, ``POST /auctions/{id}/accept``,
  ``GET /auctions/{id}/shortlist``) and recording every request it was sent. It is a
  stand-in for ``apps/exchange`` deliberately: a test of the *buyer's* composition root must
  not be able to fail for the exchange's reasons, and the shortlist it serves live is
  different from the one in its ``POST /auctions`` body so that "live" and "recorded" cannot
  be confused for one another;
* the client under test: this module's own ``ExchangeHttpClient``, over httpx, over TCP.

The unit-level cases (the roster rule, the exact accept body, the ring's eviction) drive that
same client through an ``httpx.MockTransport``, which opens no socket and lets the exchange's
answer be chosen byte for byte.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from apps.buyer.svc.src.accept import reset_accepted
from apps.buyer.svc.src.composition import (
    ENV_DEPLOYMENT,
    ENV_DEPLOYMENT_JSON,
    ENV_UI_DIST,
    MAX_RECORDED_AUCTIONS,
    BuyerDeployment,
    DeploymentConfigurationError,
    ExchangeCallFailed,
    ExchangeHttpClient,
    configure_buyer,
    create_app,
    parse_deployment,
    read_deployment,
)
from apps.buyer.svc.src.intent import reset_confirmations
from proxyshop_support.asgi_server import serve

#: How long a request to the served buyer may take. Generous against two loopback hops and
#: finite, so a wedged server fails the test instead of hanging the suite.
REQUEST_TIMEOUT_SECONDS = 30.0

#: The candidate set the DEPLOYMENT states. The browser never sends this; the whole point of
#: the roster rule is that it cannot.
ROSTER: tuple[dict[str, Any], ...] = (
    {
        "store_id": "s1",
        "tier": 1,
        "product_ref": "prod-1",
        "list_price": 100.0,
        "max_discount_pct": 20.0,
    },
    {
        "store_id": "s2",
        "tier": 2,
        "product_ref": "prod-2",
        "list_price": 120.0,
        "max_discount_pct": 10.0,
    },
)

#: The auction the exchange double opens, and the one it will serve a live shortlist for.
AUCTION_ID = "auc-composition-1"

#: An auction the exchange knows and this buyer service never opened — the "one source knows
#: it" case, which must not be a 404.
FOREIGN_AUCTION_ID = "auc-somebody-elses-1"

#: The diagnostics the exchange returns from ``POST /auctions`` and re-serves NOWHERE. This
#: is the measured reason the client records the whole answer: ``GET /auctions/{id}`` on the
#: exchange returns auction state, and the shortlist door returns the shortlist alone.
DIAGNOSTICS: dict[str, Any] = {
    "state": "closed",
    "solicited": ["s1", "s2"],
    "entries": [
        {"store_id": "s1", "fallback": False, "fallback_reason": None},
        {"store_id": "s2", "fallback": True, "fallback_reason": "no_response"},
    ],
    "denied": [{"store_id": "s3", "status": "unavailable", "reason": "unavailable: s3"}],
    "excluded": [{"bid_ref": "bid-2", "exclusion_reasons": ["hard_constraint_unsatisfied"]}],
    "ranked": [{"bid_ref": "bid-1", "store_id": "s1", "score": 0.81}],
    # Deliberately DIFFERENT from what the live shortlist door serves, below.
    "shortlist": {"auction_id": AUCTION_ID, "slots": []},
}

#: What ``GET /auctions/{id}/shortlist`` serves. One slot, where the recorded body above has
#: none, so a route that served the recorded shortlist instead of the live one is visible.
LIVE_SHORTLIST: dict[str, Any] = {
    "auction_id": AUCTION_ID,
    "slots": [{"slot": "fit", "bid_ref": "bid-1", "auction_id": AUCTION_ID, "fit_score": 0.81}],
}

PERMALINK = "https://demo-woolworks.example.com/cart/1:1?discount=PSX-MC4DM9A1"


def _intent(intent_id: str) -> dict[str, Any]:
    """A structured intent good enough for ``confirm`` — R1 needs a query and a band."""
    return {
        "intent_id": intent_id,
        "cluster_id": "cl-4d3c3e4edadaa5e7",
        "query": "a warm merino wool beanie for winter",
        "budget_band": "50-100",
        "hard_constraints": [],
        "preferences": [],
        "currency": "USD",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }


# =====================================================================================
# The exchange, as a real service on a real port
# =====================================================================================
def _exchange_double(seen: list[dict[str, Any]]) -> FastAPI:
    """The three doors this client knows, recording every request into ``seen``."""
    app = FastAPI(title="exchange-double")
    shortlists = {
        AUCTION_ID: LIVE_SHORTLIST,
        FOREIGN_AUCTION_ID: {"auction_id": FOREIGN_AUCTION_ID, "slots": []},
    }

    @app.post("/auctions", status_code=201)
    async def create_auction(body: dict[str, Any]) -> dict[str, Any]:
        seen.append({"door": "POST /auctions", "body": body})
        return {"auction_id": AUCTION_ID, **DIAGNOSTICS}

    @app.post("/auctions/{auction_id}/accept")
    async def accept(auction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        seen.append({"door": f"POST /auctions/{auction_id}/accept", "body": body})
        # `extra="forbid"` on the real exchange's AcceptRequest: an echoed auction_id is a
        # 422 there, so it is a refusal here rather than a silently tolerated extra key.
        if set(body) != {"bid_ref"}:
            raise HTTPException(status_code=422, detail=f"unexpected body keys: {sorted(body)}")
        return {"permalink_url": PERMALINK, "auction_id": auction_id, "bid_ref": body["bid_ref"]}

    @app.get("/auctions/{auction_id}/shortlist")
    async def shortlist(auction_id: str) -> dict[str, Any]:
        seen.append({"door": f"GET /auctions/{auction_id}/shortlist", "body": None})
        if auction_id not in shortlists:
            raise HTTPException(status_code=404, detail=f"no shortlist for {auction_id!r}")
        return shortlists[auction_id]

    return app


@pytest.fixture
def exchange_calls() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def exchange_url(exchange_calls: list[dict[str, Any]]) -> Iterator[str]:
    """The exchange double, on a real loopback port (D40: port 0, reported back)."""
    with serve(_exchange_double(exchange_calls)) as url:
        yield url


@pytest.fixture(autouse=True)
def _forget_ledgers() -> Iterator[None]:
    """Both packages keep process-wide ledgers: one intent opens one auction, one auction
    gets one checkout. Cleared around every test here so a re-run, or a neighbouring file
    that used the same ids, cannot turn a 201 into a 409."""
    reset_confirmations()
    reset_accepted()
    yield
    reset_confirmations()
    reset_accepted()


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """No deployment, no UI: the environment a person gets by typing ``uvicorn``."""
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)
    monkeypatch.delenv(ENV_UI_DIST, raising=False)


def _deployment_document(base_url: str) -> dict[str, Any]:
    """The document a person writes to deploy this buyer service."""
    return {
        "exchange_base_url": base_url,
        "roster": [dict(row) for row in ROSTER],
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
    }


@pytest.fixture
def deployed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exchange_url: str
) -> Iterator[httpx.Client]:
    """A client onto the real served buyer app, configured the way a deployment configures it.

    The ONLY things this fixture does are: write the deployment document, point the
    environment at it, and start ``create_app()``. It sets nothing on ``app.state``.
    """
    document = tmp_path / "buyer-deployment.json"
    document.write_text(json.dumps(_deployment_document(exchange_url), indent=2), encoding="utf-8")
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)
    monkeypatch.delenv(ENV_UI_DIST, raising=False)

    with serve(create_app()) as url:
        with httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            yield client


# =====================================================================================
# Fail closed — the property this module may not break
# =====================================================================================
def test_an_unconfigured_buyer_wires_nothing_and_503s_over_a_real_socket(
    unconfigured: None,
) -> None:
    """env -> create_app() -> served socket -> request. Nothing on app.state, 503 on the wire.

    The 503 is the routes' own, out of ``AuctionClientUnusable``; this test's job is to prove
    the composition root did not quietly invent a client to avoid it.
    """
    app = create_app()
    assert getattr(app.state, "auction_client", None) is None
    assert getattr(app.state, "exchange_client", None) is None

    with serve(app) as url, httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        confirm = client.post(
            "/buyer/intent/confirm",
            json={
                "intent": _intent("intent-unconfigured-1"),
                "confirmed": True,
                "profile": {"pseudonym": "psn-unconfigured-1", "buckets": {}},
            },
        )
        assert confirm.status_code == 503, f"{confirm.status_code}: {confirm.text}"
        assert "auction client" in confirm.text

        accept = client.post(
            "/buyer/shortlist/accept",
            json={"slot": {"slot": "fit", "bid_ref": "bid-1", "auction_id": "auc-1"}},
        )
        assert accept.status_code == 503, f"{accept.status_code}: {accept.text}"

        view = client.get(f"/buyer/auctions/{AUCTION_ID}")
        assert view.status_code == 503, f"{view.status_code}: {view.text}"
        assert ENV_DEPLOYMENT in view.text

        # No deployment and no UI: `/` is a 404 rather than a mount over nothing.
        assert client.get("/").status_code == 404


def test_an_unset_environment_reads_as_no_deployment_rather_than_a_default() -> None:
    assert read_deployment({}) is None
    assert read_deployment({ENV_DEPLOYMENT: "  ", ENV_DEPLOYMENT_JSON: ""}) is None


# =====================================================================================
# A present-but-broken document is an error, never silence
# =====================================================================================
def test_a_named_but_missing_file_is_an_error_naming_its_source(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "buyer-deployment.json"
    with pytest.raises(DeploymentConfigurationError) as caught:
        read_deployment({ENV_DEPLOYMENT: str(missing)})
    message = str(caught.value)
    assert str(missing) in message, message
    assert ENV_DEPLOYMENT in message, message


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param("{ not json", "not valid JSON", id="not-json"),
        pytest.param("[]", "must be a JSON object", id="not-an-object"),
        pytest.param("{}", "exchange_base_url", id="no-exchange-base-url"),
        pytest.param('{"exchange_base_url": ""}', "exchange_base_url", id="blank-base-url"),
        pytest.param(
            '{"exchange_base_url": "ftp://exchange.example.com"}',
            "http(s) URL",
            id="wrong-scheme",
        ),
        pytest.param(
            '{"exchange_base_url": "exchange.example.com:8000"}', "http(s) URL", id="no-scheme"
        ),
        pytest.param('{"exchange_base_url": "http://"}', "http(s) URL", id="no-host"),
        pytest.param(
            '{"exchange_base_url": "http://x.test", "roster": {}}',
            "must be a JSON array",
            id="roster-not-a-list",
        ),
        pytest.param(
            '{"exchange_base_url": "http://x.test", "roster": [{"tier": 1}]}',
            "store_id",
            id="roster-row-names-no-store",
        ),
        pytest.param(
            '{"exchange_base_url": "http://x.test", "request_timeout_seconds": 0}',
            "positive, finite",
            id="zero-timeout",
        ),
        pytest.param(
            '{"exchange_base_url": "http://x.test", "request_timeout_seconds": "30"}',
            "must be a number",
            id="timeout-as-a-string",
        ),
    ],
)
def test_a_present_but_broken_document_raises_and_names_its_source(
    document: str, expected: str
) -> None:
    """Inline, so the source in the message is the variable name rather than a path."""
    with pytest.raises(DeploymentConfigurationError) as caught:
        read_deployment({ENV_DEPLOYMENT_JSON: document})
    message = str(caught.value)
    assert expected in message, message
    assert ENV_DEPLOYMENT_JSON in message, message


def test_a_broken_document_names_the_FILE_it_came_from(tmp_path: Path) -> None:
    """The one thing an operator needs from a configuration error is *which file*."""
    document = tmp_path / "buyer-deployment.json"
    document.write_text('{"roster": []}', encoding="utf-8")
    with pytest.raises(DeploymentConfigurationError) as caught:
        read_deployment({ENV_DEPLOYMENT: str(document)})
    assert str(document) in str(caught.value)


def test_a_broken_document_fails_the_app_at_start_up_rather_than_once_per_buyer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, '{"exchange_base_url": "ftp://exchange.example.com"}')
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    with pytest.raises(DeploymentConfigurationError):
        create_app()


def test_the_document_read_is_size_capped(tmp_path: Path) -> None:
    """A log file named where a config file was meant is refused, not slurped."""
    from apps.buyer.svc.src.composition import MAX_DEPLOYMENT_BYTES

    document = tmp_path / "not-really-config.json"
    document.write_bytes(b"x" * (MAX_DEPLOYMENT_BYTES + 10))
    with pytest.raises(DeploymentConfigurationError) as caught:
        read_deployment({ENV_DEPLOYMENT: str(document)})
    assert "larger than" in str(caught.value)


def test_the_path_variable_outranks_the_inline_one(tmp_path: Path) -> None:
    document = tmp_path / "buyer-deployment.json"
    document.write_text('{"exchange_base_url": "http://from-the-file.test"}', encoding="utf-8")
    deployment = read_deployment(
        {
            ENV_DEPLOYMENT: str(document),
            ENV_DEPLOYMENT_JSON: '{"exchange_base_url": "http://from-the-variable.test"}',
        }
    )
    assert deployment is not None
    assert deployment.exchange_base_url == "http://from-the-file.test"


def test_a_good_document_parses_into_the_published_shape() -> None:
    deployment = parse_deployment(_deployment_document("http://127.0.0.1:8123/"), source="a test")
    assert isinstance(deployment, BuyerDeployment)
    # The trailing slash is stripped once, here, so no call site has to think about it.
    assert deployment.exchange_base_url == "http://127.0.0.1:8123"
    assert deployment.roster == ROSTER
    assert deployment.request_timeout_seconds == REQUEST_TIMEOUT_SECONDS


# =====================================================================================
# The client: the roster rule, the exact accept body, the bounded ring
# =====================================================================================
def _mock_client(handler: Any) -> httpx.Client:
    """An httpx client that opens no socket and answers exactly what ``handler`` says."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_deployments_roster_fills_a_payload_that_carries_none() -> None:
    """The platform's candidate set is deployment data; the browser must not be its author."""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"auction_id": "auc-roster-1"})

    client = ExchangeHttpClient(
        "http://exchange.invalid", roster=ROSTER, client=_mock_client(handler)
    )

    client.create_auction({"intent": _intent("intent-roster-1")})
    assert sent[-1]["roster"] == [dict(row) for row in ROSTER]

    client.create_auction({"intent": _intent("intent-roster-2"), "roster": None})
    assert sent[-1]["roster"] == [dict(row) for row in ROSTER]

    client.create_auction({"intent": _intent("intent-roster-3"), "roster": []})
    assert sent[-1]["roster"] == [dict(row) for row in ROSTER]


def test_a_payload_that_names_its_own_roster_is_sent_unchanged() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"auction_id": "auc-roster-4"})

    client = ExchangeHttpClient(
        "http://exchange.invalid", roster=ROSTER, client=_mock_client(handler)
    )
    chosen = [{"store_id": "s9", "tier": 1, "product_ref": "p9", "list_price": 10.0}]
    client.create_auction({"intent": _intent("intent-roster-4"), "roster": chosen})
    assert sent[-1]["roster"] == chosen


def test_accept_offer_sends_exactly_the_bid_ref_and_puts_the_auction_in_the_path() -> None:
    """The exchange's AcceptRequest is ``extra="forbid"``: an echoed auction_id is a 422."""
    sent: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"permalink_url": PERMALINK})

    client = ExchangeHttpClient("http://exchange.invalid", client=_mock_client(handler))
    answer = client.accept_offer({"auction_id": AUCTION_ID, "bid_ref": "bid-1"})

    assert sent == [("POST", f"/auctions/{AUCTION_ID}/accept", {"bid_ref": "bid-1"})]
    assert answer == {"permalink_url": PERMALINK}


def test_accept_offer_returns_the_409_body_rather_than_raising_on_it() -> None:
    """A 409 is the exchange DECIDING; ``handoff._refuse_if_denied`` reads this body."""
    denial = {"accepted": False, "denial_reason": "unknown_bid: nothing to accept"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=denial)

    client = ExchangeHttpClient("http://exchange.invalid", client=_mock_client(handler))
    assert client.accept_offer({"auction_id": AUCTION_ID, "bid_ref": "bid-1"}) == denial


@pytest.mark.parametrize("code", [400, 404, 422, 500, 502])
def test_any_other_accept_status_raises(code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json={"detail": "no"})

    client = ExchangeHttpClient("http://exchange.invalid", client=_mock_client(handler))
    with pytest.raises(ExchangeCallFailed) as caught:
        client.accept_offer({"auction_id": AUCTION_ID, "bid_ref": "bid-1"})
    assert str(code) in str(caught.value)


def test_the_diagnostics_ring_is_bounded_and_evicts_the_oldest() -> None:
    """The record is a per-process cache on a path a browser drives, so it is bounded."""
    overflow = MAX_RECORDED_AUCTIONS + 6

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        auction_id = body["intent"]["intent_id"]
        return httpx.Response(201, json={"auction_id": auction_id, **DIAGNOSTICS})

    client = ExchangeHttpClient("http://exchange.invalid", client=_mock_client(handler))
    ids = [f"auc-ring-{index:03d}" for index in range(overflow)]
    for auction_id in ids:
        client.create_auction({"intent": _intent(auction_id)})

    kept = [auction_id for auction_id in ids if client.outcome_for(auction_id) is not None]
    assert len(kept) == MAX_RECORDED_AUCTIONS
    assert kept == ids[6:], "the ring evicted something other than the oldest"

    surviving = client.outcome_for(ids[-1])
    assert surviving is not None
    assert surviving["response"]["entries"] == DIAGNOSTICS["entries"]
    assert surviving["recorded_at"].endswith("Z")


def test_an_answer_the_client_cannot_use_is_an_error_rather_than_an_empty_auction() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"<html>a proxy login page</html>")

    client = ExchangeHttpClient("http://exchange.invalid", client=_mock_client(handler))
    with pytest.raises(ExchangeCallFailed):
        client.create_auction({"intent": _intent("intent-garbage-1")})


def test_shortlist_for_answers_none_on_a_404_and_the_body_on_a_200() -> None:
    """``None`` and "a shortlist with no slots" are different answers, and stay different."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/{AUCTION_ID}/shortlist"):
            return httpx.Response(200, json=LIVE_SHORTLIST)
        return httpx.Response(404, json={"detail": "no shortlist"})

    client = ExchangeHttpClient("http://exchange.invalid", client=_mock_client(handler))
    assert client.shortlist_for(AUCTION_ID) == LIVE_SHORTLIST
    assert client.shortlist_for("auc-nobody-has") is None


def test_outcome_for_is_none_for_an_auction_this_client_never_opened() -> None:
    client = ExchangeHttpClient("http://exchange.invalid", client=_mock_client(lambda r: None))
    assert client.outcome_for(AUCTION_ID) is None


# =====================================================================================
# The composition root's own function — a unit test, issuing no request
# =====================================================================================
def test_the_composition_root_never_overwrites_wiring_somebody_already_chose() -> None:
    """A devstack, an e2e harness or a test that wired its own client still wins."""
    app = FastAPI()
    chosen = object()
    app.state.auction_client = chosen
    deployment = parse_deployment({"exchange_base_url": "http://x.test"}, source="a test")

    bound = configure_buyer(app, deployment)

    assert app.state.auction_client is chosen
    assert bound == ("exchange_client",)
    assert isinstance(app.state.exchange_client, ExchangeHttpClient)


def test_one_client_stands_behind_both_seams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two clients would be two opinions about which exchange this service is talking to.

    It matters concretely: ``confirm`` records the diagnostics on ``auction_client`` and the
    auction view reads them off ``exchange_client``.
    """
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(_deployment_document("http://x.test")))
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    monkeypatch.delenv(ENV_UI_DIST, raising=False)

    app = create_app()
    assert isinstance(app.state.auction_client, ExchangeHttpClient)
    assert app.state.exchange_client is app.state.auction_client


# =====================================================================================
# The whole journey, over two real sockets
# =====================================================================================
def test_a_deployed_buyer_confirms_an_intent_and_serves_the_exchanges_diagnostics(
    deployed: httpx.Client, exchange_calls: list[dict[str, Any]]
) -> None:
    """env -> create_app() -> served socket -> confirm -> the auction view.

    Nothing in this test sets ``app.state``. The client that reaches the exchange is the one
    the deployment document caused to exist.
    """
    confirm = deployed.post(
        "/buyer/intent/confirm",
        json={
            "intent": _intent("intent-deployed-1"),
            "confirmed": True,
            "profile": {"pseudonym": "psn-deployed-1", "buckets": {}},
        },
    )
    assert confirm.status_code == 201, f"{confirm.status_code}: {confirm.text}"
    assert confirm.json()["auction_id"] == AUCTION_ID

    # The roster the exchange was solicited with came from the deployment, not the browser.
    opened = [call for call in exchange_calls if call["door"] == "POST /auctions"]
    assert len(opened) == 1
    assert opened[0]["body"]["roster"] == [dict(row) for row in ROSTER]

    view = deployed.get(f"/buyer/auctions/{AUCTION_ID}")
    assert view.status_code == 200, f"{view.status_code}: {view.text}"
    body = view.json()

    assert body["auction_id"] == AUCTION_ID
    # LIVE, from the exchange — not the (empty) shortlist that was in the recorded answer.
    assert body["shortlist"] == LIVE_SHORTLIST
    # RECORDED — the diagnostics the exchange serves from no other door.
    for key in ("entries", "excluded", "denied", "ranked", "solicited"):
        assert body[key] == DIAGNOSTICS[key], key
    assert body["recorded_at"].endswith("Z")


def test_the_deployed_buyer_accepts_a_slot_and_hands_back_the_exchanges_permalink(
    deployed: httpx.Client, exchange_calls: list[dict[str, Any]]
) -> None:
    """R3 end to end: the permalink is the exchange's answer and nothing here built one."""
    response = deployed.post(
        "/buyer/shortlist/accept",
        json={"slot": {"slot": "fit", "bid_ref": "bid-1", "auction_id": AUCTION_ID}},
    )
    assert response.status_code == 200, f"{response.status_code}: {response.text}"
    assert response.json()["permalink_url"] == PERMALINK

    accepted = [call for call in exchange_calls if call["door"].endswith("/accept")]
    assert len(accepted) == 1
    assert accepted[0]["body"] == {"bid_ref": "bid-1"}


def test_an_auction_neither_source_knows_is_a_404(deployed: httpx.Client) -> None:
    response = deployed.get("/buyer/auctions/auc-nobody-has-ever-heard-of")
    assert response.status_code == 404, f"{response.status_code}: {response.text}"


def test_an_auction_only_the_exchange_knows_still_serves_its_live_shortlist(
    deployed: httpx.Client,
) -> None:
    """One source knowing it is not the same as neither knowing it."""
    response = deployed.get(f"/buyer/auctions/{FOREIGN_AUCTION_ID}")
    assert response.status_code == 200, f"{response.status_code}: {response.text}"
    body = response.json()
    assert body["shortlist"] == {"auction_id": FOREIGN_AUCTION_ID, "slots": []}
    assert body["entries"] == []
    assert body["recorded_at"] is None


# =====================================================================================
# The static UI mount
# =====================================================================================
def _built_ui(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>buyer</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("export const journey = 1;\n", encoding="utf-8")
    return dist


def _mounts(app: FastAPI) -> list[str]:
    return [str(getattr(route, "name", "")) for route in app.routes]


def test_no_static_mount_when_the_ui_directory_is_unset(unconfigured: None) -> None:
    assert "buyer-ui" not in _mounts(create_app())


def test_no_static_mount_and_no_crash_when_the_ui_directory_is_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unconfigured: None
) -> None:
    """A UI that was never built is not an error: the API is the service."""
    monkeypatch.setenv(ENV_UI_DIST, str(tmp_path / "never-built" / "dist"))
    app = create_app()
    assert "buyer-ui" not in _mounts(app)

    with serve(app) as url, httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        assert client.get("/").status_code == 404


def test_the_built_ui_is_served_at_the_root_when_the_directory_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unconfigured: None
) -> None:
    monkeypatch.setenv(ENV_UI_DIST, str(_built_ui(tmp_path)))
    app = create_app()
    assert "buyer-ui" in _mounts(app)

    with serve(app) as url, httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        index = client.get("/")
        assert index.status_code == 200, f"{index.status_code}: {index.text}"
        assert "<title>buyer</title>" in index.text
        assert client.get("/assets/app.js").status_code == 200


def test_api_routes_win_over_the_root_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unconfigured: None
) -> None:
    """The mount is added LAST, so a ``Mount`` at ``/`` cannot shadow a route above it."""
    monkeypatch.setenv(ENV_UI_DIST, str(_built_ui(tmp_path)))

    with serve(create_app()) as url:
        with httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            rendered = client.post(
                "/buyer/shortlist/render",
                json={"shortlist": {"auction_id": AUCTION_ID, "slots": []}},
            )
            assert rendered.status_code == 200, f"{rendered.status_code}: {rendered.text}"
            assert rendered.json() == {"slots": []}

            # The composition root's own route, too — and it answers as a route (503, no
            # client wired) rather than as a file the static mount could not find.
            view = client.get(f"/buyer/auctions/{AUCTION_ID}")
            assert view.status_code == 503, f"{view.status_code}: {view.text}"
