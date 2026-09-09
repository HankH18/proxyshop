"""An in-process Shopify-shaped storefront for the T-020 adapter tests.

Why this exists rather than reusing ``shopify_stub_url``: ``services/shopify-stub`` is an
**Admin GraphQL + control-plane** stub. Its routes are ``/admin/api/…/graphql.json``,
``/cart/{items}`` and ``/_stub/*`` — it publishes no ``/products.json``, no ``/robots.txt``,
no product HTML and no ``/password`` form, so there is nothing there for a *storefront*
fetcher to fetch. (Verified against the current stub, not assumed.)

So this module serves the storefront surface the signed fetcher actually reads, plus the
hostile surfaces the budgets exist to survive. It is deliberately **raw ASGI** rather than
FastAPI: every property under test here is a wire-level property — a gzip bomb needs a
response whose ``Content-Encoding`` and body bytes are chosen by hand, an unbounded page
needs chunks emitted one at a time, and a redirect needs an exact ``Location``. A framework
that helpfully manages those headers would be managing away the thing being tested.

Bound to loopback on port 0 via ``proxyshop_support.asgi_server.serve`` (D40), so it costs
no container RAM and the pytest socket guard permits it.
"""

from __future__ import annotations

import asyncio
import copy
import gzip
import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs

import pytest

from proxyshop_support.asgi_server import serve

STOREFRONT_PASSWORD = "open-sesame"
SESSION_COOKIE = "storefront_digest"

DEFAULT_ROBOTS = "User-agent: *\nDisallow: /admin\nDisallow: /checkout\nAllow: /\n"

#: Two products in Shopify's `/products.json` shape. Prices are strings and `available` is
#: a bool, exactly as Shopify serialises them — the adapter has to coerce both.
DEFAULT_PRODUCTS: list[dict[str, Any]] = [
    {
        "id": 8123456,
        "title": "Trail Runner 42",
        "handle": "trail-runner-42",
        "vendor": "Cascade",
        "product_type": "Footwear",
        "variants": [
            {
                "id": 44352913,
                "title": "US 9",
                "sku": "TR-42-9",
                "price": "129.95",
                "available": True,
            },
            {
                "id": 44352914,
                "title": "US 10",
                "sku": "TR-42-10",
                "price": "129.95",
                "available": False,
            },
        ],
    },
    {
        "id": 8123457,
        "title": "Merino Hiking Sock",
        "handle": "merino-hiking-sock",
        "vendor": "Cascade",
        "product_type": "Socks",
        "variants": [
            {"id": 44352920, "title": "M", "sku": "MHS-M", "price": "18.00", "available": True},
        ],
    },
]


def product_page_html(product: dict[str, Any]) -> str:
    """A themed product page carrying a schema.org ``Product`` in JSON-LD.

    The block is wrapped in ordinary theme markup and preceded by a second, *unrelated*
    JSON-LD block (a ``BreadcrumbList``), because a real Shopify theme emits several and an
    extractor that simply takes the first one is wrong on every real store.
    """
    first = (product.get("variants") or [{}])[0]
    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [{"@type": "ListItem", "position": 1, "name": "Home"}],
    }
    ld = {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": product["title"],
        "brand": {"@type": "Brand", "name": product.get("vendor", "")},
        "offers": [
            {
                "@type": "Offer",
                "sku": v.get("sku"),
                "price": v.get("price"),
                "priceCurrency": "USD",
                "availability": (
                    "https://schema.org/InStock"
                    if v.get("available")
                    else "https://schema.org/OutOfStock"
                ),
            }
            for v in product.get("variants") or []
        ],
    }
    return (
        "<!doctype html><html><head><title>{title}</title>"
        '<script type="application/ld+json">{crumbs}</script>'
        '<script type="application/ld+json">{ld}</script>'
        "</head><body><h1>{title}</h1><p>SKU {sku}</p></body></html>"
    ).format(
        title=product["title"],
        crumbs=json.dumps(breadcrumbs),
        ld=json.dumps(ld),
        sku=first.get("sku", ""),
    )


class StorefrontStub:
    """A storefront whose hostile behaviours are switchable per test.

    Attributes:
        requests: every request received, as ``(method, path, headers)``. Tests assert the
            crawler's identity and its robots-before-catalog ordering against this.
    """

    def __init__(
        self,
        *,
        password: str | None = None,
        robots: str = DEFAULT_ROBOTS,
        products: list[dict[str, Any]] | None = None,
        robots_status: int = 200,
    ) -> None:
        self.password = password
        self.robots = robots
        self.robots_status = robots_status
        # DEEP copy, not `list(...)`. A shallow copy duplicates the outer list and SHARES
        # every product dict, and `test_signed_fetch.py` mutates one in place
        # (`stub.products[0]["variants"][0]["price"] = "139.95"`) to make a re-crawl see
        # changed content. That mutation used to reach the module-level DEFAULT_PRODUCTS and
        # stay there for the rest of the session, so this whole directory's green depended on
        # collection order: measured at the time of this change, reversing collection order
        # gave `2 failed, 560 passed` — test_signed_fetch's own
        # `test_the_adapter_ingests_an_open_storefront` (129.95 read back as 139.95) and
        # test_catalog_mcp's cross-adapter equivalence check, which reads DEFAULT_PRODUCTS
        # directly. A suite whose result depends on the order it happens to run in is not
        # measuring what it claims to measure.
        self.products = copy.deepcopy(DEFAULT_PRODUCTS if products is None else products)
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.unlocked_sessions: set[str] = set()
        self.slow_chunk_seconds = 0.3
        self.endless_chunks = 512
        #: Where ``/redirect/custom`` sends the crawler. Settable so a test can aim a
        #: redirect at a host that is on the caller's allow-list yet resolves into private
        #: space — the one target that only the per-hop SSRF re-check can refuse.
        self.redirect_target = "https://elsewhere.example.com/"
        #: Which ``/products.json`` page comes back padded past any sane
        #: ``max_response_bytes`` — ``None`` for a store that serves every page honestly.
        #: A store whose catalogue is fine until page N is the shape that matters: the
        #: per-response ceiling is raised inside the transport, so the crawl still has
        #: pages, bytes and time left when that one page is refused, and what the adapter
        #: does with the rest of the crawl is then a decision rather than an accident.
        self.oversized_page: int | None = None
        #: Padding on that page. Only its length is read: the transport refuses the
        #: response on its ``Content-Length`` before any of it is parsed.
        self.oversized_page_bytes = 1024 * 1024
        #: How many 1 KiB dribbles ``/slow`` emits. Deliberately far more than any test's
        #: time budget permits: a client that only checks its deadline once the *whole*
        #: body has arrived would sit here for `slow_chunks * slow_chunk_seconds` seconds,
        #: which is what makes the time-budget test able to tell a real deadline from one
        #: that merely fires after the fact.
        self.slow_chunks = 60

    # -- helpers -------------------------------------------------------------------------

    def paths_fetched(self) -> list[str]:
        return [path for _, path, _ in self.requests]

    def _is_unlocked(self, headers: dict[str, str]) -> bool:
        if self.password is None:
            return True
        cookie = headers.get("cookie", "")
        return any(
            part.strip().startswith(f"{SESSION_COOKIE}=")
            and part.strip().split("=", 1)[1] in self.unlocked_sessions
            for part in cookie.split(";")
        )

    # -- ASGI ----------------------------------------------------------------------------

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        path = scope["path"]
        method = scope["method"]
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        self.requests.append((method, path, headers))

        try:
            await self._route(send, method, path, query, headers, body)
        except (RuntimeError, OSError, ConnectionError):
            # The client hung up mid-response — which for the budget tests is the pass
            # condition, not a failure of the server.
            return

    async def _route(self, send, method, path, query, headers, body) -> None:
        if path == "/robots.txt":
            if self.robots_status != 200:
                return await self._send(send, self.robots_status, b"", "text/plain")
            return await self._send(send, 200, self.robots.encode(), "text/plain")

        if path == "/password" and method == "POST":
            fields = parse_qs(body.decode("utf-8", "replace"))
            supplied = (fields.get("password") or [""])[0]
            if self.password is not None and supplied != self.password:
                return await self._send(send, 401, b"wrong password", "text/plain")
            token = "unlocked-token"
            self.unlocked_sessions.add(token)
            return await self._send(
                send,
                302,
                b"",
                "text/html",
                extra=[
                    (b"location", b"/"),
                    (b"set-cookie", f"{SESSION_COOKIE}={token}; path=/".encode()),
                ],
            )

        # Everything below is storefront content and is gated by the password session (A1).
        if not self._is_unlocked(headers):
            return await self._send(
                send, 302, b"", "text/html", extra=[(b"location", b"/password")]
            )

        if path == "/products.json":
            page = int((query.get("page") or ["1"])[0])
            limit = int((query.get("limit") or ["250"])[0])
            start = (page - 1) * limit
            chunk = self.products[start : start + limit]
            body: dict[str, Any] = {"products": chunk}
            if self.oversized_page is not None and page == self.oversized_page:
                body["padding"] = "x" * self.oversized_page_bytes
            payload = json.dumps(body).encode()
            return await self._send(send, 200, payload, "application/json")

        if path.startswith("/products/"):
            handle = path.rsplit("/", 1)[-1]
            for product in self.products:
                if product.get("handle") == handle:
                    return await self._send(
                        send, 200, product_page_html(product).encode(), "text/html"
                    )
            return await self._send(send, 404, b"not found", "text/plain")

        # -- hostile surfaces ----------------------------------------------------------
        if path == "/redirect/metadata":
            return await self._send(
                send,
                302,
                b"",
                "text/html",
                extra=[(b"location", b"http://169.254.169.254/latest/meta-data/")],
            )
        if path == "/redirect/loop":
            return await self._send(
                send, 302, b"", "text/html", extra=[(b"location", b"/redirect/loop")]
            )
        if path == "/redirect/custom":
            return await self._send(
                send,
                302,
                b"",
                "text/html",
                extra=[(b"location", self.redirect_target.encode())],
            )
        if path.startswith("/redirect/chain/"):
            step = int(path.rsplit("/", 1)[-1])
            return await self._send(
                send,
                302,
                b"",
                "text/html",
                extra=[(b"location", f"/redirect/chain/{step + 1}".encode())],
            )
        if path == "/bomb.json":
            # ~8 MiB of zeros, which gzip shrinks to a few KiB: a small download that a
            # naive reader inflates into a large allocation.
            payload = gzip.compress(b"\0" * (8 * 1024 * 1024))
            return await self._send(
                send,
                200,
                payload,
                "application/json",
                extra=[(b"content-encoding", b"gzip")],
            )
        if path == "/endless":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            for _ in range(self.endless_chunks):
                await send({"type": "http.response.body", "body": b"x" * 65536, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        if path == "/slow":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            for _ in range(self.slow_chunks):
                # `asyncio.sleep`, not `time.sleep`: a blocking sleep inside a coroutine
                # stops uvicorn's event loop, so nothing — not even the response headers
                # already handed to `send` — reaches the socket until the handler returns.
                # The client would then see one connect-timeout rather than a slow drip,
                # and this test would be measuring the socket timeout instead of the
                # crawl's time budget.
                await asyncio.sleep(self.slow_chunk_seconds)
                await send({"type": "http.response.body", "body": b"x" * 1024, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        if path == "/lying-length":
            # Claims to be tiny, then sends a great deal more.
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", b"10")],
                }
            )
            await send({"type": "http.response.body", "body": b"x" * 10, "more_body": False})
            return

        return await self._send(send, 200, b"<html><body>storefront</body></html>", "text/html")

    @staticmethod
    async def _send(send, status: int, body: bytes, media: str, extra=None) -> None:
        headers = [(b"content-type", media.encode())] + list(extra or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


@pytest.fixture
def storefront() -> Iterator[tuple[str, StorefrontStub]]:
    """An open (no-password) storefront. Yields ``(base_url, stub)``."""
    stub = StorefrontStub()
    with serve(stub) as base_url:
        yield base_url, stub


@pytest.fixture
def locked_storefront() -> Iterator[tuple[str, StorefrontStub]]:
    """A password-protected dev store (SPEC A1). Yields ``(base_url, stub)``."""
    stub = StorefrontStub(password=STOREFRONT_PASSWORD)
    with serve(stub) as base_url:
        yield base_url, stub


@pytest.fixture
def storefront_factory() -> Iterator[Any]:
    """Build a storefront with bespoke settings: ``storefront_factory(robots=..., ...)``."""
    from contextlib import ExitStack

    with ExitStack() as stack:

        def build(**kwargs: Any) -> tuple[str, StorefrontStub]:
            stub = StorefrontStub(**kwargs)
            return stack.enter_context(serve(stub)), stub

        yield build
