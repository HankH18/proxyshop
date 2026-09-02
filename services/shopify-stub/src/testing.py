"""Helpers for driving the stub: a typed client, a recording receiver, and the documents.

This lives in the package rather than beside the tests for two reasons. The first is
mechanical: ``services/shopify-stub`` has a hyphen and cannot be a Python package, so a test
module cannot import a sibling helper by name under pytest's ``importlib`` import mode. The
second is that the consumers of this stub — T-050's install flow, T-052's code builder,
T-080's seeder — need exactly these helpers, and making them re-derive a client and a
webhook receiver is how four tickets end up with four subtly different ideas of the same
API.

Nothing here is imported by :mod:`shopify_stub.app`; it is a leaf.
"""

from __future__ import annotations

from typing import Any

import httpx
from shopify_stub.state import DEFAULT_ACCESS_TOKEN, DEFAULT_API_VERSION

#: A catalog variant the stub's tests and demos seed. Priced at 100.00 so a percentage
#: discount lands on a round number and an arithmetic slip is visible by eye.
SEED_VARIANT: dict[str, Any] = {
    "variant_id": 44352913,
    "product_id": 8123456,
    "title": "Trail Runner 42",
    "price": "100.00",
    "currency": "USD",
    "sku": "TR-42",
}

#: Every host that is **not** a bare DNS name, keyed by the attack or malformation it
#: represents. Lives in the package rather than in a test module because two test modules
#: need it — ``test_stub_permalink.py`` holds :func:`~shopify_stub.permalink.build_permalink`
#: to it and ``test_stub_domain_guard.py`` holds :class:`~shopify_stub.state.StubConfig`,
#: ``PUT /_stub/config`` and the three live-URL emit sites to the same table — and a test
#: module under pytest's ``importlib`` import mode cannot import a sibling.
#:
#: The two ``userinfo`` entries are the ones that cost something: they render a *live*
#: checkout link whose real host is ``attacker.tld``.
#:
#: The response-splitting entries are the **four** named
#: :data:`RESPONSE_SPLITTING_HOSTS` — a bare LF in a ``Location`` header ends the header,
#: and Python's ``$`` anchor (which :data:`~shopify_stub.permalink._LABEL` used to use)
#: matches immediately before a trailing one. They are named rather than counted because
#: the comment here used to say "the last two", which stopped being true the moment the
#: table grew: ``trailing lf`` and ``trailing crlf`` sit at positions 15 and 16 of 18, so
#: "the last two" pointed at ``embedded lf`` and ``header injection`` while describing the
#: trailing pair. Positional prose about a literal that anyone may append to is prose that
#: goes stale silently.
NON_BARE_HOSTS: dict[str, str] = {
    "userinfo": "good.example.com@attacker.tld",
    "escaped userinfo": "store-a.example.com\\@attacker.tld",
    "explicit port": "store-a.example.com:8443",
    "scheme": "https://store-a.example.com",
    "path": "store-a.example.com/evil",
    "query": "store-a.example.com?x",
    "fragment": "store-a.example.com#f",
    "space": "store-a.example.com evil.tld",
    "leading dot": ".store-a.example.com",
    "empty label": "store-a..example.com",
    "trailing hyphen label": "store-a-.example.com",
    "underscore": "store_a.example.com",
    "ipv6 brackets": "[::1]",
    "empty": "",
    "trailing lf": "store-a.example.com\n",
    "trailing crlf": "store-a.example.com\r\n",
    "embedded lf": "store-a\n.example.com",
    "header injection": "evil.tld\nX-Injected: yes",
}

#: The keys of :data:`NON_BARE_HOSTS` whose value carries a CR or an LF — the
#: response-splitting subset, named so the prose above cannot go stale against the table.
#: ``test_stub_domain_guard.test_the_response_splitting_subset_is_named_not_counted`` holds
#: this set and the table to each other in both directions, so adding a fifth CR/LF entry
#: without naming it here is a red test rather than a quietly wrong comment.
RESPONSE_SPLITTING_HOSTS: frozenset[str] = frozenset(
    {"trailing lf", "trailing crlf", "embedded lf", "header injection"}
)

#: The ``discountCodeBasicCreate`` document, written as a real caller writes it: a named
#: operation with variables and an explicit selection set, so the stub's parser meets the
#: shape it will actually be sent rather than a convenient one.
DISCOUNT_MUTATION = """
mutation CreateOfferCode($basicCodeDiscount: DiscountCodeBasicInput!) {
  discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
    codeDiscountNode {
      id
      codeDiscount {
        ... on DiscountCodeBasic {
          title
          status
          codes(first: 1) { nodes { code } }
          codesCount { count precision }
          startsAt
          endsAt
          usageLimit
          asyncUsageCount
          appliesOncePerCustomer
          combinesWith { orderDiscounts productDiscounts shippingDiscounts }
          customerGets { value { ... on DiscountPercentage { percentage } } }
        }
      }
    }
    userErrors { field message code extraInfo }
  }
}
"""

#: The ``orders`` query document.
ORDERS_QUERY = """
query RecentOrders($first: Int!, $after: String, $query: String) {
  orders(first: $first, after: $after, query: $query) {
    edges {
      cursor
      node {
        id
        legacyResourceId
        name
        checkoutToken
        cartToken
        createdAt
        displayFinancialStatus
        displayFulfillmentStatus
        currencyCode
        discountCodes
        totalPriceSet { shopMoney { amount currencyCode } }
        totalDiscountsSet { shopMoney { amount currencyCode } }
        totalRefundedSet { shopMoney { amount currencyCode } }
        customAttributes { key value }
        discountApplications(first: 5) {
          edges { node { ... on DiscountCodeApplication {
            code index allocationMethod targetSelection targetType } } }
        }
        lineItems(first: 10) { edges { node { id title quantity sku } } }
      }
    }
    pageInfo { hasNextPage hasPreviousPage startCursor endCursor }
  }
}
"""

#: ``webhookSubscriptionCreate``. Uses ``uri``: ``callbackUrl`` is deprecated on
#: ``WebhookSubscriptionInput``, and a helper written against the deprecated spelling would
#: teach it to every consumer that copies this.
SUBSCRIBE_MUTATION = """
mutation Subscribe($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {
    webhookSubscription { id legacyResourceId topic uri format apiVersion { handle }
                          createdAt updatedAt }
    userErrors { field message }
  }
}
"""

#: ``webPixelCreate``. Note the asymmetry the response exposes: ``settings`` goes in as an
#: object and comes back as a serialized JSON string.
WEB_PIXEL_MUTATION = """
mutation InstallPixel($webPixel: WebPixelInput!) {
  webPixelCreate(webPixel: $webPixel) {
    webPixel { id settings }
    userErrors { field message code }
  }
}
"""


class RecordingReceiver:
    """A raw-ASGI endpoint that records every request it is sent.

    Raw ASGI rather than a framework on purpose: a webhook signature covers the **exact
    bytes** on the wire, so the receiver must keep those bytes rather than a re-serialised
    parse of them. A framework that hands you ``request.json()`` makes the most common
    HMAC-verification bug invisible.

    Args:
        status_sequence: statuses to answer with, consumed in order; once exhausted every
            further request gets ``200``. ``[500, 503]`` makes the first two delivery
            attempts fail so a caller can watch the retry.
    """

    def __init__(self, status_sequence: list[int] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status_sequence = list(status_sequence or [])

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        self.requests.append(
            {
                "path": scope["path"],
                "headers": {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in scope["headers"]
                },
                "body": body,
            }
        )
        status = self.status_sequence.pop(0) if self.status_sequence else 200
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})


class StubClient:
    """A typed front door to a running stub, so callers read as behaviour not plumbing."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self.http = client
        self.base_url = base_url

    # -- Shopify's own surface ----------------------------------------------------------

    async def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        token: str | None = DEFAULT_ACCESS_TOKEN,
        version: str = DEFAULT_API_VERSION,
    ) -> httpx.Response:
        headers = {} if token is None else {"X-Shopify-Access-Token": token}
        return await self.http.post(
            f"/admin/api/{version}/graphql.json",
            json={"query": query, "variables": variables or {}},
            headers=headers,
        )

    async def visit_cart(
        self, variant_id: int, quantity: int = 1, code: str | None = None
    ) -> httpx.Response:
        query = "" if code is None else f"?discount={code}"
        return await self.http.get(f"/cart/{variant_id}:{quantity}{query}")

    # -- the four mutations / one query --------------------------------------------------

    async def create_code(
        self,
        code: str,
        *,
        percentage: float = 0.10,
        usage_limit: int | None = 1,
        starts_at: str | None = None,
        ends_at: str | None = None,
        combines_with: dict[str, bool] | None = None,
        offer_id: str | None = None,
        title: str | None = None,
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "title": title or code,
            "code": code,
            "usageLimit": usage_limit,
            "appliesOncePerCustomer": True,
            "combinesWith": combines_with
            or {
                "orderDiscounts": False,
                "productDiscounts": False,
                "shippingDiscounts": False,
            },
            "customerGets": {"value": {"percentage": percentage}, "items": {"all": True}},
        }
        if starts_at is not None:
            payload["startsAt"] = starts_at
        if ends_at is not None:
            payload["endsAt"] = ends_at
        if offer_id is not None:
            payload["tags"] = [f"offer:{offer_id}"]
        return await self.graphql(DISCOUNT_MUTATION, {"basicCodeDiscount": payload})

    async def orders(
        self, first: int = 10, after: str | None = None, query: str | None = None
    ) -> httpx.Response:
        return await self.graphql(ORDERS_QUERY, {"first": first, "after": after, "query": query})

    async def subscribe(self, topic: str, uri: str) -> httpx.Response:
        return await self.graphql(SUBSCRIBE_MUTATION, {"topic": topic, "sub": {"uri": uri}})

    async def install_pixel(self, collector_url: str) -> httpx.Response:
        return await self.graphql(
            WEB_PIXEL_MUTATION, {"webPixel": {"settings": {"collectorUrl": collector_url}}}
        )

    # -- control plane -------------------------------------------------------------------

    async def seed(self, variants: list[dict[str, Any]]) -> httpx.Response:
        return await self.http.post("/_stub/seed", json={"variants": variants})

    async def configure(self, **values: Any) -> httpx.Response:
        return await self.http.put("/_stub/config", json=values)

    async def config(self) -> dict[str, Any]:
        return (await self.http.get("/_stub/config")).json()

    async def complete(self, token: str) -> httpx.Response:
        return await self.http.post(f"/_stub/checkouts/{token}/complete")

    async def checkout(self, token: str) -> httpx.Response:
        return await self.http.get(f"/_stub/checkouts/{token}")

    async def fulfil(self, order_id: int, **body: Any) -> httpx.Response:
        return await self.http.post(f"/_stub/orders/{order_id}/fulfill", json=body)

    async def refund(self, order_id: int, **body: Any) -> httpx.Response:
        return await self.http.post(f"/_stub/orders/{order_id}/refund", json=body)

    async def codes(self) -> dict[str, Any]:
        return (await self.http.get("/_stub/codes")).json()

    async def deliveries(self) -> list[dict[str, Any]]:
        return (await self.http.get("/_stub/webhooks/deliveries")).json()["deliveries"]

    async def events(self) -> list[dict[str, Any]]:
        return (await self.http.get("/_stub/events")).json()["events"]

    async def suppressed(self) -> list[dict[str, Any]]:
        return (await self.http.get("/_stub/events/suppressed")).json()["suppressed"]

    # -- composites ----------------------------------------------------------------------

    async def buy(
        self, variant_id: int, *, quantity: int = 1, code: str | None = None
    ) -> dict[str, Any]:
        """Visit the permalink and complete the checkout. Returns the completion body.

        The cart route answers **303**, so this checks for that explicitly rather than
        calling ``raise_for_status()`` — httpx counts a 3xx as an error when redirects are
        not followed, and a helper that treated the normal path as a failure would be
        useless to the consumers this module exists for.
        """
        cart = await self.visit_cart(variant_id, quantity=quantity, code=code)
        if cart.status_code != 303:
            raise AssertionError(f"cart permalink returned {cart.status_code}: {cart.text}")
        completed = await self.complete(cart.json()["token"])
        if completed.status_code != 201:
            raise AssertionError(
                f"checkout completion returned {completed.status_code}: {completed.text}"
            )
        return completed.json()


__all__ = [
    "DISCOUNT_MUTATION",
    "NON_BARE_HOSTS",
    "ORDERS_QUERY",
    "RESPONSE_SPLITTING_HOSTS",
    "SEED_VARIANT",
    "SUBSCRIBE_MUTATION",
    "WEB_PIXEL_MUTATION",
    "RecordingReceiver",
    "StubClient",
]


class TruncatingReceiver:
    """A receiver that answers normally once, then dies mid-response on every later request.

    The delivery log documents ``status_code`` as "the **last** attempt's status, or
    ``None`` when the last attempt raised before a response (a connection error, a
    timeout)". :class:`RecordingReceiver` cannot produce that: every one of its answers is
    a complete HTTP response, so ``status_code`` is never ``None`` and a delivery whose
    first attempt got a status and whose last did not is unreachable — which is the one
    case that separates "the last attempt's outcome" from "the last outcome there was".

    So this receiver promises a ``Content-Length`` it does not send and lets the server cut
    the connection, which reaches the client as ``httpx.RemoteProtocolError``. Verified
    deterministic over repeated runs against ``proxyshop_support.asgi_server.serve``; the
    truncated turns log an ASGI traceback on the server side, which is the receiver
    working, not the stub failing.

    Args:
        first_status: the status answered to the first request. The default ``500`` makes
            the first attempt a *recorded* failure, so a log that keeps the first attempt
            and a log that keeps the last are two different values.
    """

    def __init__(self, first_status: int = 500) -> None:
        self.requests: list[dict[str, Any]] = []
        self.first_status = first_status

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        self.requests.append({"path": scope["path"], "body": body})
        if len(self.requests) == 1:
            await send(
                {
                    "type": "http.response.start",
                    "status": self.first_status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b"{}"})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"100")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})
