"""In-process storefront serving policy pages, for the T-021 extraction tests.

``_fixtures_storefront.StorefrontStub`` serves ``/products.json`` and the password form —
the surface T-020's *catalog* fetcher reads. It publishes no ``/policies/*``, so there is
nothing there for a *policy-page* fetcher to fetch. (Verified against that module, not
assumed.) This stub serves the policy surface instead, out of the committed fixture pages
in ``fixtures/pages/``, so the fetch tests and the extraction tests grade the same bytes.

Raw ASGI on purpose, and for the same reason the storefront stub is: robots status codes,
content types and 404s are wire-level facts the fetcher branches on, and a framework that
manages those headers would manage away what is being tested. Bound to loopback on port 0
via ``proxyshop_support.asgi_server.serve`` (D40).

Fixture names here are prefixed ``policy_`` so they cannot collide with the storefront or
graph fixtures this directory's conftest loads alongside them.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest

from proxyshop_support.asgi_server import serve

#: The committed fixture pages this ticket owns.
PAGES_DIR = pathlib.Path(__file__).resolve().parents[3] / "fixtures" / "pages"
STORE_PAGES_DIR = PAGES_DIR / "store-example"
EXPECTATION_FILE = PAGES_DIR / "expected_claims.json"

POLICY_ROBOTS = "User-agent: *\nDisallow: /admin\nAllow: /\n"

#: Served path -> fixture file. The two Shopify-shaped policy paths the production fetcher
#: tries first are wired, and so is the warranty page, so a default crawl finds real pages.
DEFAULT_ROUTES: dict[str, str] = {
    "/policies/shipping-policy": "shipping.html",
    "/policies/refund-policy": "returns.html",
    "/pages/warranty": "warranty.html",
}


def fixture_page(name: str) -> str:
    """The raw text of one committed fixture page.

    Args:
        name: the file name, e.g. ``"shipping.html"``.

    Returns:
        The file's contents.
    """
    return (STORE_PAGES_DIR / name).read_text(encoding="utf-8")


def approved_expectation() -> dict[str, Any]:
    """The human-approved claim expectation for the fixture pages (ticket acceptance 1)."""
    return json.loads(EXPECTATION_FILE.read_text(encoding="utf-8"))


class PolicyStoreStub:
    """A storefront that publishes policy pages, and can change one of them mid-test.

    Attributes:
        requests: every ``(method, path)`` received. The differential tests assert against
            this that a second crawl still *fetches* (cheap) while not re-extracting.
        routes: served path -> page body. Mutate to simulate a policy change.
        robots: the robots.txt body served.
        robots_status: the status robots.txt is served with; drives the fail-closed path.
    """

    def __init__(
        self,
        *,
        routes: dict[str, str] | None = None,
        robots: str = POLICY_ROBOTS,
        robots_status: int = 200,
        media_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.routes: dict[str, str] = (
            {path: fixture_page(name) for path, name in DEFAULT_ROUTES.items()}
            if routes is None
            else dict(routes)
        )
        self.robots = robots
        self.robots_status = robots_status
        self.media_type = media_type
        self.requests: list[tuple[str, str]] = []

    def paths_fetched(self) -> list[str]:
        """Every path requested, in order."""
        return [path for _, path in self.requests]

    def replace(self, path: str, body: str) -> None:
        """Change what one path serves, so its content hash moves."""
        self.routes[path] = body

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        """ASGI entry point."""
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        path = scope["path"]
        self.requests.append((scope["method"], path))

        if path == "/robots.txt":
            await self._send(send, self.robots_status, self.robots.encode(), "text/plain")
            return
        body = self.routes.get(path)
        if body is None:
            await self._send(send, 404, b"not found", "text/plain")
            return
        await self._send(send, 200, body.encode("utf-8"), self.media_type)

    @staticmethod
    async def _send(send: Any, status: int, body: bytes, media: str) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", media.encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


@pytest.fixture
def policy_store() -> Iterator[tuple[str, PolicyStoreStub]]:
    """A storefront publishing the committed policy pages. Yields ``(base_url, stub)``."""
    stub = PolicyStoreStub()
    with serve(stub) as base_url:
        yield base_url, stub


@pytest.fixture
def policy_store_factory() -> Iterator[Any]:
    """Build a policy storefront with bespoke settings: ``policy_store_factory(robots=…)``."""
    from contextlib import ExitStack

    with ExitStack() as stack:

        def build(**kwargs: Any) -> tuple[str, PolicyStoreStub]:
            stub = PolicyStoreStub(**kwargs)
            return stack.enter_context(serve(stub)), stub

        yield build


@pytest.fixture
def policy_pages() -> dict[str, str]:
    """Every committed fixture page, keyed by file name."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(STORE_PAGES_DIR.glob("*.html"))
    }


@pytest.fixture
def policy_expectation() -> dict[str, Any]:
    """The human-approved claim expectation (ticket acceptance 1)."""
    return approved_expectation()
