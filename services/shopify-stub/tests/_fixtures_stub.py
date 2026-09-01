"""Fixtures for the shopify-stub tests. Owned by T-013.

Loaded into ``services/shopify-stub/tests/conftest.py`` by
``proxyshop_support.fixture_loader``; that conftest is orchestrator-owned and frozen.

Every fixture here builds a **fresh** stub with :func:`shopify_stub.app.create_app` and
serves it on an ephemeral port through ``proxyshop_support.asgi_server.serve`` (D41 — no
hard-coded ports, so D9's busy-port list cannot be hit by a port the kernel chose). The root
``conftest.py``'s ``shopify_stub_url`` fixture serves the *module-level* ``app``, one
instance shared by every test in a session; ``test_stub_contract.py`` exercises that fixture
deliberately to prove the pinned import path works, and everything else uses these so one
test's discount-code table cannot leak into the next.

The client, the receiver and the GraphQL documents live in :mod:`shopify_stub.testing`
rather than here, because a test module cannot import a sibling helper under pytest's
``importlib`` import mode when the containing directory is not a package — and because the
consuming tickets need the same helpers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from proxyshop_support.asgi_server import serve
from shopify_stub.app import create_app
from shopify_stub.testing import SEED_VARIANT, RecordingReceiver, StubClient


@pytest.fixture
def stub_server() -> Iterator[str]:
    """A fresh stub on an ephemeral port (D41). Yields its base URL, no trailing slash."""
    with serve(create_app()) as base_url:
        yield base_url


@pytest.fixture
async def stub(stub_server: str) -> AsyncIterator[StubClient]:
    """A :class:`StubClient` on a fresh stub, with one catalog variant already seeded."""
    async with httpx.AsyncClient(base_url=stub_server, follow_redirects=False) as client:
        wrapper = StubClient(client, stub_server)
        response = await wrapper.seed([SEED_VARIANT])
        assert response.status_code == 200, response.text
        yield wrapper


@pytest.fixture
def webhook_receiver() -> Iterator[tuple[RecordingReceiver, str]]:
    """A recording webhook endpoint on its own ephemeral port. Yields ``(receiver, url)``."""
    receiver = RecordingReceiver()
    with serve(receiver) as base_url:
        yield receiver, f"{base_url}/webhooks/shopify"


@pytest.fixture
def flaky_webhook_receiver() -> Iterator[tuple[RecordingReceiver, str]]:
    """A receiver that rejects the first two deliveries and accepts the third."""
    receiver = RecordingReceiver(status_sequence=[500, 503])
    with serve(receiver) as base_url:
        yield receiver, f"{base_url}/webhooks/shopify"


@pytest.fixture
def dead_webhook_receiver() -> Iterator[tuple[RecordingReceiver, str]]:
    """A receiver that rejects every delivery, so the retry budget is exhausted."""
    receiver = RecordingReceiver(status_sequence=[500] * 50)
    with serve(receiver) as base_url:
        yield receiver, f"{base_url}/webhooks/shopify"


@pytest.fixture
def collector_receiver() -> Iterator[tuple[RecordingReceiver, str]]:
    """A stand-in for the merchant app's ``POST /pixel/collect`` endpoint."""
    receiver = RecordingReceiver()
    with serve(receiver) as base_url:
        yield receiver, f"{base_url}/collect"
