"""Fixtures for T-050's install tests. Owned by T-050.

Loaded into the frozen ``apps/merchant/svc/tests/conftest.py`` by
``proxyshop_support.fixture_loader``. Every name here carries the ``install_`` prefix so it
cannot collide with a fixture T-051, T-052 or T-053 drops in the same directory.

Everything binds **port 0** (D40) and lives for one test: a fresh ``shopify-stub``, a fresh
merchant app, and — where a test needs to watch the web pixel actually beacon — the stub
package's own :class:`~shopify_stub.testing.RecordingReceiver`, which is the stand-in
``services/shopify-stub`` ships for the merchant collector. Nothing here re-implements any
part of Shopify; the stub is the integration target.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from merchant_svc.install.tokens import InMemoryOfflineTokenStore
from merchant_svc.install.webhooks import INBOX, WebhookInbox
from shopify_stub.app import create_app as create_stub_app
from shopify_stub.state import DEFAULT_WEBHOOK_SECRET
from shopify_stub.testing import SEED_VARIANT, RecordingReceiver, StubClient

from proxyshop_support.asgi_server import serve

#: The HMAC key the stub signs webhook deliveries with; the merchant app's client secret.
INSTALL_SECRET = DEFAULT_WEBHOOK_SECRET


@pytest.fixture
def install_stub_url() -> Iterator[str]:
    """A fresh ``shopify-stub`` on an ephemeral port (D40). Yields its base URL."""
    with serve(create_stub_app()) as base_url:
        yield base_url


@pytest.fixture
async def install_stub(install_stub_url: str) -> AsyncIterator[StubClient]:
    """A :class:`StubClient` on a fresh stub with one catalog variant seeded."""
    async with httpx.AsyncClient(base_url=install_stub_url, follow_redirects=False) as client:
        wrapper = StubClient(client, install_stub_url)
        response = await wrapper.seed([SEED_VARIANT])
        assert response.status_code == 200, response.text
        yield wrapper


@pytest.fixture
def install_app_url() -> Iterator[str]:
    """The merchant service itself, served on an ephemeral port.

    Built through the frozen ``merchant_svc.main.create_app``, so the routes under test are
    reached exactly the way the deployed service reaches them — router discovery included.
    """
    from merchant_svc.main import create_app

    with serve(create_app()) as base_url:
        yield base_url


@pytest.fixture
def install_collector() -> Iterator[tuple[RecordingReceiver, str]]:
    """The stub's own collector stand-in. Yields ``(receiver, url)``."""
    receiver = RecordingReceiver()
    with serve(receiver) as base_url:
        yield receiver, f"{base_url}/collect"


@pytest.fixture
def install_tokens() -> InMemoryOfflineTokenStore:
    """An empty offline-token store, isolated from the process-wide one."""
    return InMemoryOfflineTokenStore()


@pytest.fixture
def install_inbox() -> Iterator[WebhookInbox]:
    """The process-wide webhook inbox, emptied before and after the test."""
    INBOX.clear()
    yield INBOX
    INBOX.clear()


@pytest.fixture
def install_env(
    monkeypatch: pytest.MonkeyPatch, install_stub_url: str, install_app_url: str
) -> dict[str, str]:
    """Point the merchant app at the stub and give it the stub's client secret.

    Returns the values it set, so a test can assert against them without re-reading the
    environment.
    """
    values = {
        "SHOPIFY_STUB_URL": install_stub_url,
        "MERCHANT_APP_URL": install_app_url,
        "SHOPIFY_API_KEY": "install-test-api-key",
        "SHOPIFY_API_SECRET": INSTALL_SECRET,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.fixture
def install_pixel_origin() -> Iterator[tuple[RecordingReceiver, str]]:
    """A receiver standing in for the app's whole public **origin**. Yields ``(receiver, origin)``.

    Where :func:`install_collector` hands back one fixed URL — what a caller passes as
    ``collector=`` — this hands back a bare origin, what a caller passes as ``app_url=``.
    The difference matters: only an origin lets the install itself choose the path, so a
    test can tell "``app_url`` reached the pixel" apart from "the pixel used the configured
    default", which a full collector URL cannot distinguish.
    """
    receiver = RecordingReceiver()
    with serve(receiver) as base_url:
        yield receiver, base_url.rstrip("/")
