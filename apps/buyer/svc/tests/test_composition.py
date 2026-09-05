"""The buyer service, driven as it BOOTS — and the four things the buyer's page needs from it.

Run it on its own::

    PROXYSHOP_WORKER=12 .venv/bin/python -m pytest \\
        apps/buyer/svc/tests/test_composition.py -q

Why this file exists
--------------------
``tests/test_composition_wiring.py`` next door owns the property that a confirmed intent
reaches a real exchange with nothing but an environment variable set, and it may not be
weakened by anything here. This file owns the four things the buyer WEB UI additionally
needs of the composition root, each of which is a fact about a real service rather than a
preference:

1. **the deployment's ``roster``** — ``buyer_svc.intent.routes.ConfirmBody`` accepts a
   ``roster`` straight off the wire, so without a deployment-stated one the browser is the
   author of the platform's candidate set;
2. **the recorded ``POST /auctions`` answer** — ``apps/exchange/src/auction/routes.py``'s
   ``read_auction`` returns auction *state* and no diagnostics at all, so the exchange
   publishes ``entries`` / ``excluded`` / ``denied`` / ``ranked`` exactly once and the answer
   to "why is my shortlist empty" survives nowhere else;
3. **``GET /buyer/auctions/{auction_id}``** — the live shortlist and those recorded
   diagnostics, side by side and never confused for one another;
4. **the static UI mount** — opt-in, mounted LAST, and never by the request-time hooks.

What is real here
-----------------
* the app: ``buyer_svc.main.create_app()`` — the frozen entrypoint, which globs and mounts
  every ``<feature>/routes.py``, plus ``composition.mount_ui`` where a UI is being served.
  Nothing here assigns to ``app.state``, except the one unit test that is *about*
  :func:`configure_buyer` not overwriting what somebody else assigned;
* the exchange: a real ASGI service on a real loopback port, answering the three doors this
  client knows and recording every request. It is a stand-in for ``apps/exchange``
  deliberately — a test of the *buyer's* composition root must not be able to fail for the
  exchange's reasons — and the shortlist it serves live is different from the one inside its
  ``POST /auctions`` body, so a route serving the recorded shortlist as the live one is
  visible rather than plausible;
* the client under test: this module's own ``HttpExchangeClient``, over httpx, over TCP.

The unit-level cases (the roster rule, the ring's eviction, the 404-is-not-empty rule) drive
that same client through an ``httpx.MockTransport``, which opens no socket and lets the
exchange's answer be chosen byte for byte.
"""

from __future__ import annotations

import importlib
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
    ENV_EXCHANGE_URL,
    ENV_UI_DIST,
    MAX_RECORDED_AUCTIONS,
    MAX_ROSTER_ENTRIES,
    Deployment,
    DeploymentConfigurationError,
    ExchangeCallFailed,
    HttpExchangeClient,
    configure_buyer,
    ensure_configured,
    mount_ui,
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

#: The diagnostics the exchange returns from ``POST /auctions`` and re-serves NOWHERE.
DIAGNOSTICS: dict[str, Any] = {
    "state": "closed",
    "solicited": ["s1", "s2"],
    "entries": [
        {"store_id": "s1", "fallback": False, "fallback_reason": None},
        {"store_id": "s2", "fallback": True, "fallback_reason": "no_response"},
    ],
    "denied": [{"store_id": "s3", "status": "unavailable", "reason": "unavailable: s3"}],
    "excluded": [{"bid_ref": "bid-2", "exclusion_reasons": ["hard_constraint_unsatisfied"]}],
    "ranked": [{"bid_ref": "bid-1", "store_id": "s1", "rank_score": 0.81}],
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

AUCTION_VIEW = "/buyer/auctions"


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


def buyer_app() -> FastAPI:
    """The deployable buyer app, built the way ``apps/buyer/devstack/run.py`` builds it.

    ``buyer_svc.main.create_app()`` — the frozen entrypoint ``uvicorn buyer_svc.main:app``
    runs, which globs and mounts every ``<feature>/routes.py`` — and then, LAST,
    :func:`mount_ui`. Nothing is assigned to ``app.state``: everything this app needs to
    reach the exchange it reads for itself, out of the environment, on the first request that
    needs it.
    """
    app: FastAPI = importlib.import_module("buyer_svc.main").create_app()
    mount_ui(app)
    return app


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
    """No deployment, no UI: the environment a person gets by typing ``uvicorn``.

    ``EXCHANGE_URL`` too — main's composition root reads the bare origin the compose fragment
    already sets, so an inherited one would make "this service is unconfigured" depend on the
    shell the suite was started from.
    """
    for name in (ENV_DEPLOYMENT, ENV_DEPLOYMENT_JSON, ENV_EXCHANGE_URL, ENV_UI_DIST):
        monkeypatch.delenv(name, raising=False)


def _deployment_document(exchange_url: str) -> dict[str, Any]:
    """The document a person writes to deploy this buyer service."""
    return {
        "exchange_url": exchange_url,
        "roster": [dict(row) for row in ROSTER],
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
    }


@pytest.fixture
def deployed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exchange_url: str
) -> Iterator[httpx.Client]:
    """A client onto the real served buyer app, configured the way a deployment configures it.

    The ONLY things this fixture does are: write the deployment document, point the
    environment at it, and serve the frozen app. It sets nothing on ``app.state``.
    """
    document = tmp_path / "buyer-deployment.json"
    document.write_text(json.dumps(_deployment_document(exchange_url), indent=2), encoding="utf-8")
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)
    monkeypatch.delenv(ENV_EXCHANGE_URL, raising=False)
    monkeypatch.delenv(ENV_UI_DIST, raising=False)

    with serve(buyer_app()) as url:
        with httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            yield client


# =====================================================================================
# Fail closed — the property this module may not break
# =====================================================================================
def test_an_unconfigured_buyer_wires_nothing_and_503s_over_a_real_socket(
    unconfigured: None,
) -> None:
    """env -> the frozen app -> served socket -> request. Nothing on app.state, 503 on the wire.

    The 503s on ``/confirm`` and ``/accept`` are the routes' own; this test's job is to prove
    the composition root did not quietly invent a client to avoid them, and that the auction
    view added here refuses in the same voice rather than 500ing on a missing client.
    """
    app = buyer_app()
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

        view = client.get(f"{AUCTION_VIEW}/{AUCTION_ID}")
        assert view.status_code == 503, f"{view.status_code}: {view.text}"
        assert ENV_DEPLOYMENT in view.text

        # No deployment and no UI: `/` is a 404 rather than a mount over nothing.
        assert client.get("/").status_code == 404


def test_an_unset_environment_reads_as_no_deployment_rather_than_a_default() -> None:
    assert read_deployment({}) is None
    assert read_deployment({ENV_DEPLOYMENT: "  ", ENV_DEPLOYMENT_JSON: ""}) is None


# =====================================================================================
# The document: `roster` is deployment data, and a malformed one names its source
# =====================================================================================
def test_a_good_document_parses_into_the_published_shape() -> None:
    deployment = parse_deployment(_deployment_document("http://127.0.0.1:8123/"), source="a test")
    assert isinstance(deployment, Deployment)
    # The trailing slash is stripped once, in the parser, so no call site has to think about it.
    assert deployment.exchange_url == "http://127.0.0.1:8123"
    assert deployment.roster == ROSTER
    assert deployment.request_timeout_seconds == REQUEST_TIMEOUT_SECONDS


def test_a_document_with_no_roster_is_legal_and_states_an_empty_one() -> None:
    """The field is additive: a deployment that names no roster behaves as it always did."""
    deployment = parse_deployment({"exchange_url": "http://x.test"}, source="a test")
    assert deployment.roster == ()


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            '{"exchange_url": "http://x.test", "roster": {}}',
            "must be a JSON array",
            id="roster-not-a-list",
        ),
        pytest.param(
            '{"exchange_url": "http://x.test", "roster": "s1,s2"}',
            "must be a JSON array",
            id="roster-as-a-string",
        ),
        pytest.param(
            '{"exchange_url": "http://x.test", "roster": [{"tier": 1}]}',
            "roster[0] names no store_id",
            id="roster-row-names-no-store",
        ),
        pytest.param(
            '{"exchange_url": "http://x.test", "roster": [{"store_id": "  "}]}',
            "roster[0] names no store_id",
            id="roster-row-store-id-is-blank",
        ),
        pytest.param(
            '{"exchange_url": "http://x.test", "roster": [7]}',
            "roster[0] must be a JSON object",
            id="roster-row-is-not-an-object",
        ),
        pytest.param(
            '{"exchange_url": "http://x.test", "rooster": []}',
            "rooster",
            id="a-misspelt-roster-is-refused-rather-than-ignored",
        ),
    ],
)
def test_a_broken_roster_is_refused_naming_its_source(document: str, expected: str) -> None:
    """A roster row this service dropped is a store the operator believes is competing."""
    with pytest.raises(DeploymentConfigurationError) as caught:
        read_deployment({ENV_DEPLOYMENT_JSON: document})
    message = str(caught.value)
    assert expected in message, message
    assert ENV_DEPLOYMENT_JSON in message, message


def test_a_roster_longer_than_the_exchange_accepts_is_refused_once_here() -> None:
    """Otherwise it is a 422 on every confirmation from a service that looks configured."""
    too_many = [{"store_id": f"s{index}"} for index in range(MAX_ROSTER_ENTRIES + 1)]
    with pytest.raises(DeploymentConfigurationError) as caught:
        parse_deployment({"exchange_url": "http://x.test", "roster": too_many}, source="a test")
    assert f"at most {MAX_ROSTER_ENTRIES}" in str(caught.value)


def test_a_broken_document_names_the_FILE_it_came_from(tmp_path: Path) -> None:
    """The one thing an operator needs from a configuration error is *which file*."""
    document = tmp_path / "buyer-deployment.json"
    document.write_text('{"roster": []}', encoding="utf-8")
    with pytest.raises(DeploymentConfigurationError) as caught:
        read_deployment({ENV_DEPLOYMENT: str(document)})
    assert str(document) in str(caught.value)


def test_a_broken_document_is_a_503_on_the_auction_view_and_never_a_500(
    monkeypatch: pytest.MonkeyPatch, unconfigured: None
) -> None:
    """The composition root runs as a request-time hook, so this route takes it too.

    A buyer service told to read a deployment it cannot read is misconfigured, and that is a
    different thing from a buyer service nobody has configured — both are 503, and neither is
    the 500 that an unhandled ``DeploymentConfigurationError`` would be.
    """
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, '{"exchange_url": "http://x.test", "roster": {}}')

    with serve(buyer_app()) as url:
        with httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            view = client.get(f"{AUCTION_VIEW}/{AUCTION_ID}")

    assert view.status_code == 503, f"{view.status_code}: {view.text}"
    assert "must be a JSON array" in view.text


# =====================================================================================
# The client: the roster rule, the bounded ring, the two reads
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

    client = HttpExchangeClient(
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

    client = HttpExchangeClient(
        "http://exchange.invalid", roster=ROSTER, client=_mock_client(handler)
    )
    chosen = [{"store_id": "s9", "tier": 1, "product_ref": "p9", "list_price": 10.0}]
    client.create_auction({"intent": _intent("intent-roster-4"), "roster": chosen})
    assert sent[-1]["roster"] == chosen


def test_a_client_with_no_deployment_roster_adds_no_roster_key_at_all() -> None:
    """Additive, not opinionated: a client the document gave no roster sends what it was given."""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"auction_id": "auc-roster-5"})

    client = HttpExchangeClient("http://exchange.invalid", client=_mock_client(handler))
    client.create_auction({"intent": _intent("intent-roster-5")})
    assert "roster" not in sent[-1]


def test_the_diagnostics_ring_is_bounded_and_evicts_the_oldest() -> None:
    """The record is a per-process cache on a path a browser drives, so it is bounded."""
    overflow = MAX_RECORDED_AUCTIONS + 6

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        auction_id = body["intent"]["intent_id"]
        return httpx.Response(201, json={"auction_id": auction_id, **DIAGNOSTICS})

    client = HttpExchangeClient("http://exchange.invalid", client=_mock_client(handler))
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


def test_an_answer_naming_no_auction_is_recorded_under_nothing() -> None:
    """A record keyed by '' would be handed to the next caller who asked about ''."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"state": "open"})

    client = HttpExchangeClient("http://exchange.invalid", client=_mock_client(handler))
    client.create_auction({"intent": _intent("intent-no-id-1")})
    assert client.outcome_for("") is None


def test_shortlist_for_answers_none_on_a_404_and_the_body_on_a_200() -> None:
    """``None`` and "a shortlist with no slots" are different answers, and stay different."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/{AUCTION_ID}/shortlist"):
            return httpx.Response(200, json=LIVE_SHORTLIST)
        # A body-less 404, which is the harder case: it must never be parsed.
        return httpx.Response(404)

    client = HttpExchangeClient("http://exchange.invalid", client=_mock_client(handler))
    assert client.shortlist_for(AUCTION_ID) == LIVE_SHORTLIST
    assert client.shortlist_for("auc-nobody-has") is None


def test_a_shortlist_read_that_fails_is_an_error_rather_than_an_empty_shortlist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "the exchange is unwell"})

    client = HttpExchangeClient("http://exchange.invalid", client=_mock_client(handler))
    with pytest.raises(ExchangeCallFailed) as caught:
        client.shortlist_for(AUCTION_ID)
    assert "500" in str(caught.value)


def test_outcome_for_is_none_for_an_auction_this_client_never_opened() -> None:
    client = HttpExchangeClient("http://exchange.invalid", client=_mock_client(lambda r: None))
    assert client.outcome_for(AUCTION_ID) is None


# =====================================================================================
# The composition root's own functions — unit tests, issuing no request
# =====================================================================================
def test_the_composition_root_never_overwrites_wiring_somebody_already_chose() -> None:
    """A devstack, an e2e harness or a test that wired its own client still wins."""
    app = FastAPI()
    chosen = object()
    app.state.auction_client = chosen
    deployment = parse_deployment({"exchange_url": "http://x.test"}, source="a test")

    bound = configure_buyer(app, deployment)

    assert app.state.auction_client is chosen
    assert bound == ("exchange_client",)
    assert isinstance(app.state.exchange_client, HttpExchangeClient)


def test_the_bound_client_carries_the_documents_roster() -> None:
    """The seam the roster rule rides on: the document -> the client -> POST /auctions."""
    app = FastAPI()
    configure_buyer(app, parse_deployment(_deployment_document("http://x.test"), source="a test"))
    assert app.state.auction_client._roster == tuple(dict(row) for row in ROSTER)


def test_one_client_stands_behind_both_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two clients would be two opinions about which exchange this service is talking to.

    It matters concretely: ``confirm`` records the diagnostics on ``auction_client`` and the
    auction view reads them off ``exchange_client``.
    """
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(_deployment_document("http://x.test")))
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)

    app = FastAPI()
    ensure_configured(app)
    assert isinstance(app.state.auction_client, HttpExchangeClient)
    assert app.state.exchange_client is app.state.auction_client


# =====================================================================================
# The whole journey, over two real sockets
# =====================================================================================
def test_a_deployed_buyer_confirms_an_intent_and_serves_the_exchanges_diagnostics(
    deployed: httpx.Client, exchange_calls: list[dict[str, Any]]
) -> None:
    """env -> the frozen app -> served socket -> confirm -> the auction view.

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

    view = deployed.get(f"{AUCTION_VIEW}/{AUCTION_ID}")
    assert view.status_code == 200, f"{view.status_code}: {view.text}"
    body = view.json()

    assert body["auction_id"] == AUCTION_ID
    # LIVE, from the exchange — not the (empty) shortlist that was in the recorded answer.
    assert body["shortlist"] == LIVE_SHORTLIST
    assert body["shortlist"] != DIAGNOSTICS["shortlist"], "the RECORDED shortlist was served"
    # RECORDED — the diagnostics the exchange serves from no other door.
    for key in ("entries", "excluded", "denied", "ranked", "solicited"):
        assert body[key] == DIAGNOSTICS[key], key
    assert body["recorded_at"].endswith("Z")

    # The live half really was fetched over the wire on this request.
    assert any(call["door"].endswith(f"/{AUCTION_ID}/shortlist") for call in exchange_calls)


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
    response = deployed.get(f"{AUCTION_VIEW}/auc-nobody-has-ever-heard-of")
    assert response.status_code == 404, f"{response.status_code}: {response.text}"


def test_an_auction_only_the_exchange_knows_still_serves_its_live_shortlist(
    deployed: httpx.Client,
) -> None:
    """One source knowing it is not the same as neither knowing it."""
    response = deployed.get(f"{AUCTION_VIEW}/{FOREIGN_AUCTION_ID}")
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
    assert mount_ui(FastAPI()) is None


def test_no_static_mount_and_no_crash_when_the_ui_directory_is_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unconfigured: None
) -> None:
    """A UI that was never built is not an error: the API is the service."""
    monkeypatch.setenv(ENV_UI_DIST, str(tmp_path / "never-built" / "dist"))
    app = buyer_app()
    assert "buyer-ui" not in _mounts(app)

    with serve(app) as url, httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        assert client.get("/").status_code == 404


def test_the_built_ui_is_served_at_the_root_when_the_directory_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unconfigured: None
) -> None:
    dist = _built_ui(tmp_path)
    monkeypatch.setenv(ENV_UI_DIST, str(dist))
    app = buyer_app()
    assert "buyer-ui" in _mounts(app)

    with serve(app) as url, httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        index = client.get("/")
        assert index.status_code == 200, f"{index.status_code}: {index.text}"
        assert "<title>buyer</title>" in index.text
        assert index.headers["content-type"].startswith("text/html")
        assert client.get("/assets/app.js").status_code == 200


def test_mount_ui_returns_the_directory_it_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unconfigured: None
) -> None:
    dist = _built_ui(tmp_path)
    monkeypatch.setenv(ENV_UI_DIST, str(dist))
    assert mount_ui(FastAPI()) == str(dist)
    # And it takes an explicit environment, so a caller need not mutate `os.environ`.
    assert mount_ui(FastAPI(), {ENV_UI_DIST: str(dist)}) == str(dist)
    assert mount_ui(FastAPI(), {}) is None


def test_the_request_time_hooks_never_mount_the_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unconfigured: None
) -> None:
    """The API image must not grow a demo page because a variable happened to be set.

    ``configure_buyer`` and ``ensure_configured`` run on the REQUEST path, and a route added
    to a live app from inside a request it is serving mutates the router underneath
    Starlette's own matching. Mounting is a launcher's deliberate call, and only that.
    """
    monkeypatch.setenv(ENV_UI_DIST, str(_built_ui(tmp_path)))
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(_deployment_document("http://x.test")))

    app = FastAPI()
    ensure_configured(app)
    configure_buyer(app, parse_deployment(_deployment_document("http://x.test"), source="a test"))

    assert "buyer-ui" not in _mounts(app)


def test_api_routes_win_over_the_root_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unconfigured: None
) -> None:
    """The mount is added LAST, so a ``Mount`` at ``/`` cannot shadow a route above it."""
    monkeypatch.setenv(ENV_UI_DIST, str(_built_ui(tmp_path)))

    with serve(buyer_app()) as url:
        with httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            rendered = client.post(
                "/buyer/shortlist/render",
                json={"shortlist": {"auction_id": AUCTION_ID, "slots": []}},
            )
            assert rendered.status_code == 200, f"{rendered.status_code}: {rendered.text}"
            assert rendered.json() == {"slots": []}

            # The route this ticket added, too — and it answers as a route (503, no client
            # wired) rather than as a file the static mount could not find (404).
            view = client.get(f"{AUCTION_VIEW}/{AUCTION_ID}")
            assert view.status_code == 503, f"{view.status_code}: {view.text}"
