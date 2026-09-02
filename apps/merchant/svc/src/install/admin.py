"""The Admin GraphQL client and the two documents the install issues.

SPEC C5 pins *GraphQL Admin only*, so there is no REST path here and no second transport.
The two documents are written the way a real caller writes them — a named operation with
typed variables and an explicit selection set — rather than in whatever shorthand happens
to satisfy a parser, because the shape a stub is *sent* is the only part of a stub run that
carries over to production.

``settings`` is passed as an **object** in variables, which is what Shopify's own
``webPixelCreate`` example does; the response returns it back as a serialized JSON string.
That asymmetry is real and ``services/shopify-stub`` reproduces it, so a caller that
round-trips the response through ``JSON.parse`` here keeps working there.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from merchant_svc.install.config import admin_base_url, app_config
from merchant_svc.install.shop import normalize_shop_domain

#: ``webPixelCreate``. Installs the web pixel extension (C5: the only checkout-observation
#: path this app has).
WEB_PIXEL_CREATE = """
mutation InstallWebPixel($webPixel: WebPixelInput!) {
  webPixelCreate(webPixel: $webPixel) {
    webPixel { id settings }
    userErrors { field message code }
  }
}
"""

#: ``webhookSubscriptionCreate``. Uses ``uri``; ``callbackUrl`` is deprecated on
#: ``WebhookSubscriptionInput`` and a document written against the deprecated spelling
#: teaches it to everything that copies this one.
WEBHOOK_SUBSCRIPTION_CREATE = """
mutation SubscribeToShopEvents(
  $topic: WebhookSubscriptionTopic!
  $webhookSubscription: WebhookSubscriptionInput!
) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhookSubscription) {
    webhookSubscription {
      id
      legacyResourceId
      topic
      uri
      format
      apiVersion { handle }
      createdAt
      updatedAt
    }
    userErrors { field message }
  }
}
"""

#: The header Shopify authenticates an Admin API call with.
ACCESS_TOKEN_HEADER = "X-Shopify-Access-Token"


class AdminAPIError(RuntimeError):
    """The Admin API answered something that is not a usable response.

    Two very different failures land here and the distinction is worth keeping: a non-2xx
    HTTP status (``401`` for a bad or missing access token — Shopify answers that one with
    ``{"errors": "<a bare string>"}``, not the list of objects a GraphQL error produces),
    and a 200 carrying a top-level ``errors`` array.
    """

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AdminGraphQLClient:
    """A minimal Admin GraphQL client: one endpoint, one header, one method.

    Args:
        shop_domain: the ``<name>.myshopify.com`` host being administered.
        access_token: the shop's **offline** Admin API token.
        base_url: origin override. Defaults to ``SHOPIFY_STUB_URL`` when that is set and
            ``https://<shop_domain>`` otherwise, which is what lets every offline run (C9)
            drive ``services/shopify-stub`` through the same code production uses.
        api_version: Admin API version path segment.
        timeout: per-request timeout, seconds.
        client: an existing ``httpx.Client`` to borrow (not closed by this object).
    """

    def __init__(
        self,
        *,
        shop_domain: str,
        access_token: str,
        base_url: str | None = None,
        api_version: str | None = None,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.shop_domain = normalize_shop_domain(shop_domain)
        if not access_token:
            raise ValueError("an Admin API call needs an access token")
        self.access_token = access_token
        self.api_version = api_version or app_config().api_version
        self.base_url = (base_url or admin_base_url(self.shop_domain)).rstrip("/")
        self._owned = client is None
        self._client = client or httpx.Client(timeout=timeout)

    @property
    def endpoint(self) -> str:
        """The Admin GraphQL URL this client posts to."""
        return f"{self.base_url}/admin/api/{self.api_version}/graphql.json"

    def execute(self, document: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run one operation and return its ``data`` block.

        Raises:
            AdminAPIError: non-2xx status, or a 200 carrying top-level ``errors``.
        """
        response = self._client.post(
            self.endpoint,
            json={"query": document, "variables": dict(variables or {})},
            headers={ACCESS_TOKEN_HEADER: self.access_token},
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise AdminAPIError(
                f"Admin API returned a non-JSON body ({response.status_code})",
                status_code=response.status_code,
            ) from exc
        if response.status_code >= 400:
            raise AdminAPIError(
                f"Admin API rejected the call with HTTP {response.status_code}: "
                f"{body.get('errors') if isinstance(body, dict) else body!r}",
                status_code=response.status_code,
                body=body,
            )
        if isinstance(body, dict) and body.get("errors"):
            raise AdminAPIError(
                f"Admin API returned GraphQL errors: {body['errors']!r}",
                status_code=response.status_code,
                body=body,
            )
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else {}

    def close(self) -> None:
        """Close the underlying transport, if this object owns it."""
        if self._owned:
            self._client.close()

    def __enter__(self) -> AdminGraphQLClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
