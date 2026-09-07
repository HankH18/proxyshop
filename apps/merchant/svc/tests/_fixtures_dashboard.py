"""Fixtures for R9's merchant dashboard. Owned by the R9 lane.

Loaded into the frozen ``apps/merchant/svc/tests/conftest.py`` by
``proxyshop_support.fixture_loader``. Every name carries the ``dash_`` prefix so it cannot
collide with a fixture another ticket drops in this directory.

Nothing here opens a socket. The upstream services the dashboard reads — the exchange's
``GET /reports/losses``, the trust service's ``GET /snapshot`` and ``GET /events``, and the
store agent's ``POST /v1/bid-requests`` — are reached through
:func:`merchant_svc.dashboard.upstream.open_client`, which these fixtures replace with an
``httpx.ASGITransport`` over an app handed in by the test. Two of those apps are STUBS that
answer exactly what the real routes answer; the store agent is the REAL
``store_agent.main:app``, built over this repo's own
``fixtures/envelopes/store-alpha.approved.json``, because the kill switch is the one control
whose proof must not be a stub agreeing with itself.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from merchant_svc.envelope.store import EnvelopeVersions
from merchant_svc.install.routes import ADMIN_TOKEN_ENV

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: This repo's own approved-envelope fixture, which doubles as a store-agent context.
STORE_ALPHA_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json"

#: The bearer the dashboard routes are driven with here.
DASH_ADMIN_TOKEN = "dashboard-test-token"

#: The bearer the stub exchange accepts, and the store it resolves it to.
DASH_REPORT_TOKEN = "report-token-for-alpha"


@pytest.fixture
def dash_admin_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure the administrative bearer every dashboard route requires."""
    monkeypatch.setenv(ADMIN_TOKEN_ENV, DASH_ADMIN_TOKEN)
    return DASH_ADMIN_TOKEN


@pytest.fixture
def dash_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[EnvelopeVersions]:
    """An empty envelope history, swapped in everywhere the routes read the process-wide one.

    Both route modules are patched, and that is not belt-and-braces: the dashboard reads the
    envelope the ``onboarding`` routes write, so a test that patched only one would be driving
    two different histories through one HTTP client.
    """
    fresh = EnvelopeVersions()
    monkeypatch.setattr("merchant_svc.onboarding.routes.ENVELOPES", fresh)
    monkeypatch.setattr("merchant_svc.dashboard.routes.ENVELOPES", fresh)
    monkeypatch.setattr("merchant_svc.bidding.gate.ENVELOPES", fresh)
    yield fresh


@pytest.fixture
def dash_journal(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fresh solicitation journal, so one test's probes are not another test's history."""
    from merchant_svc.dashboard.journal import SolicitationJournal

    fresh = SolicitationJournal()
    monkeypatch.setattr("merchant_svc.dashboard.routes.SOLICITATIONS", fresh)
    return fresh


@pytest.fixture
def dash_upstreams(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, Any], None]:
    """Register an ASGI app as the service living at a base URL, for one test.

    Returns a ``register(base_url, app)`` callable. Anything not registered is left to the
    real :func:`~merchant_svc.dashboard.upstream.open_client`, so a test that forgets to
    register an upstream gets a connection error rather than a silent stub.
    """
    registry: dict[str, Any] = {}
    real = __import__("merchant_svc.dashboard.upstream", fromlist=["open_client"]).open_client

    def fake_open_client(base_url: str, *, timeout: float = 5.0) -> httpx.AsyncClient:
        app = registry.get(base_url.rstrip("/"))
        if app is None:
            return real(base_url, timeout=timeout)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base_url, timeout=timeout
        )

    monkeypatch.setattr("merchant_svc.dashboard.upstream.open_client", fake_open_client)

    def register(base_url: str, app: Any) -> None:
        registry[base_url.rstrip("/")] = app

    return register


def dash_loss_report(store_id: str) -> dict[str, Any]:
    """A `LossReport` body in exactly the shape the exchange's own route serves.

    Reason categories and unmet criteria only. There is no rival id and no amount in it,
    because there is none in what the exchange serves — which is the property the dashboard
    must not undo on the way to the browser.
    """
    return {
        "store_id": store_id,
        "window": {"start": 0.0, "end": 4.0},
        "by_cluster": [
            {
                "cluster_id": "trail-running-shoes",
                "lost": 3,
                "reasons": {"fit": 1, "price": 1, "commitments": 1, "trust": 0},
                "unmet_criteria": ["waterproof = true"],
            }
        ],
    }


def dash_trust_snapshot() -> dict[str, dict[str, Any]]:
    """``GET /snapshot``'s body: EVERY store, which is the shape the dashboard must narrow.

    Copied from a RUNNING trust service rather than invented — ``dims`` (not ``dimensions``),
    Beta ``alpha``/``beta`` with a ``decayed_at`` and no mean anywhere. The first draft of this
    fixture guessed ``dimensions``/``mean``, the merchant tests passed against the guess, and
    the per-dimension table rendered empty against the real service.
    """
    return {
        "store-alpha": {
            "store_id": "store-alpha",
            "score": 0.72,
            "confidence": 0.31,
            "score_version": "trust-score-1.0.0",
            "snapshot_version": "trust-snapshot-1.0.0",
            "low_data": False,
            "dims": {
                "price_honored": {
                    "alpha": 8.0,
                    "beta": 2.0,
                    "decayed_at": "2026-09-07T19:19:29.680Z",
                },
                "catalog_claim_accuracy": {
                    "alpha": 3.0,
                    "beta": 3.0,
                    "decayed_at": "2026-09-07T19:19:29.680Z",
                },
            },
            "blacklisted": False,
        },
        "store-rival": {
            "store_id": "store-rival",
            "score": 0.99,
            "confidence": 0.91,
            "low_data": False,
            "dims": {
                "price_honored": {
                    "alpha": 99.0,
                    "beta": 1.0,
                    "decayed_at": "2026-09-07T19:19:29.680Z",
                }
            },
            "blacklisted": False,
        },
    }


def dash_stub_exchange(report: Mapping[str, Any], *, token: str = DASH_REPORT_TOKEN) -> Any:
    """A stand-in for the exchange's `GET /reports/losses`, with its real auth shape.

    The bearer resolves the store; there is no field in which to ask for another merchant's
    data. Answers 401 for a wrong bearer and 503 when this stub was built with no token,
    exactly as ``exchange.reports.routes`` does.
    """
    app = FastAPI()

    @app.get("/reports/losses")
    def losses(request: Request, start: float, end: float) -> Any:
        if not token:
            return JSONResponse(status_code=503, content={"error": "reports-not-configured"})
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or supplied.strip() != token:
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        body = dict(report)
        body["window"] = {"start": start, "end": end}
        return JSONResponse(content=body)

    return app


def dash_stub_trust(snapshot: Mapping[str, Any], events: list[dict[str, Any]]) -> Any:
    """A stand-in for the trust service's `GET /snapshot` and `GET /events`."""
    app = FastAPI()

    @app.get("/snapshot")
    def snap() -> Any:
        return JSONResponse(content=dict(snapshot))

    @app.get("/events")
    def read_events(store_id: str | None = None, limit: int = 50, after_seq: int = 0) -> Any:
        rows = [e for e in events if store_id is None or e.get("store_id") == store_id]
        return JSONResponse(
            content={
                "events": rows[:limit],
                "count": len(rows[:limit]),
                "limit": limit,
                "truncated": False,
                "next_after_seq": after_seq,
                "is_chain": False,
            }
        )

    return app


def dash_store_agent_app(activation: str) -> Any:
    """The REAL store-agent app, advocating for this repo's own store-alpha fixture.

    ``activation`` is written into the fixture's envelope before the app is given it, so the
    three states R7/R9 name — shadow, active, killed — are the fixture's own envelope in three
    states rather than three different documents.

    The context is handed over with ``configure_solicitation`` rather than through
    ``STORE_AGENT_CONTEXT``. The environment path resolves LAZILY, on the first request, and
    caches whatever it found — so a fixture that set the variable, built the app and restored
    the variable produced an agent that answered ``store_context_unconfigured`` to everything.
    Measured, and the reason this is spelled out: an agent with no store declines every
    solicitation, which looks exactly like a store that chose not to bid.
    """
    from store_agent.main import create_app as create_store_agent
    from store_agent.solicitation.serving import configure_solicitation

    document = json.loads(STORE_ALPHA_FIXTURE.read_text(encoding="utf-8"))
    document["envelope"] = dict(document["envelope"])
    document["envelope"]["activation"] = activation
    app = create_store_agent()
    configure_solicitation(app, context=document)
    return app


@pytest.fixture
async def dash_client(dash_store: EnvelopeVersions) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process client against the frozen entrypoint's app, with an empty store."""
    from merchant_svc.main import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://merchant") as client:
        yield client


@pytest.fixture
def dash_built_bundle(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A directory shaped like a real `vite build` output, pointed at by the env var."""
    from merchant_svc.dashboard.config import UI_DIST_ENV

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><title>ProxyShop merchant</title>"
        '<script type="module" src="/dashboard/assets/index.js"></script></head>'
        '<body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / "index.js").write_text("export const built = true\n", encoding="utf-8")
    monkeypatch.setenv(UI_DIST_ENV, str(dist))
    return dist
