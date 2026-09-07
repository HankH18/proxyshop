"""Fixtures for S1 link 7a: the merchant's order webhooks reaching E6's chained ledger.

Loaded into the frozen ``apps/merchant/svc/tests/conftest.py`` by
``proxyshop_support.fixture_loader``. Every name here carries the ``ledger_`` prefix so it
cannot collide with the ``install_``, ``codes_`` or ``onboarding_`` fixtures beside it.

**Why a merchant test imports ``trust``.** The thing under test is a cross-process write:
merchant POSTs a ``LedgerEvent`` to another deployable's ``POST /events`` and the proof that
it landed is that deployable's own ``GET /events``. A double standing in for trust would
prove that merchant can reach a double. So the real service is started here, on an ephemeral
port (D40), with :class:`~trust.events.store.InMemoryEventStore` injected on ``app.state`` —
the same store ``apps/trust``'s own suite drives, and the reason no Postgres is needed. The
import is test-only; nothing under ``apps/merchant/svc/src`` imports ``trust``, which is the
property that lets these two stay separate deployables.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from proxyshop_support.asgi_server import serve
from proxyshop_support.trust_ledger import ENV_TRUST_URL


@pytest.fixture
def ledger_trust_url() -> Iterator[str]:
    """A real ``apps/trust`` on an ephemeral port, hash-chaining into memory.

    Yields its base URL — ``http://127.0.0.1:<port>`` — not the ``/events`` path, because
    that is the shape a deployment states and what ``TRUST_URL`` carries.
    """
    from trust.events.store import InMemoryEventStore
    from trust.main import create_app

    app = create_app()
    # Injected rather than left to `store_for()`, whose fallback builds a PostgresEventStore
    # from the environment. This suite touches no datastore and must not start doing so.
    app.state.event_store = InMemoryEventStore()
    with serve(app) as base_url:
        yield base_url


@pytest.fixture
def ledger_closed_port_url() -> str:
    """An origin on loopback that nothing is listening on — a trust service that is down.

    The port is bound and released, so it is a port the kernel just handed out and nothing
    holds: a connect to it is refused immediately rather than hanging, which is what makes
    the outage tests fast and deterministic.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def ledger_trust_wired(monkeypatch: pytest.MonkeyPatch, ledger_trust_url: str) -> Iterator[str]:
    """Point the merchant process at ``ledger_trust_url`` the way a deployment does.

    ``TRUST_URL`` and nothing else: that is the one variable
    :func:`proxyshop_support.trust_ledger.trust_endpoint` reads, so a test that set an
    attribute instead would be testing a path the container has not got.
    """
    yield from _wire(monkeypatch, ledger_trust_url)


@pytest.fixture
def ledger_trust_down(
    monkeypatch: pytest.MonkeyPatch, ledger_closed_port_url: str
) -> Iterator[str]:
    """The same wiring, pointed at an origin nothing is serving."""
    yield from _wire(monkeypatch, ledger_closed_port_url)


def _wire(monkeypatch: pytest.MonkeyPatch, base_url: str) -> Iterator[str]:
    """Set ``TRUST_URL`` and rebuild the process publisher around it, both ways.

    The publisher is a process singleton built on first use — one pooled client per process,
    which is the point of it — so a test that changed ``TRUST_URL`` alone would be answered
    by whichever publisher an earlier test had already built. Dropping it before AND after
    keeps that from leaking in either direction.
    """
    from merchant_svc import composition

    monkeypatch.setenv(ENV_TRUST_URL, base_url)
    composition.set_trust_publisher(None)
    try:
        yield base_url
    finally:
        composition.set_trust_publisher(None)
