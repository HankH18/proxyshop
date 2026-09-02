"""Webhook delivery — the **always delivered** half of the stub.

The asymmetry with :mod:`shopify_stub.telemetry` is acceptance criterion 3 and it is not an
accident of implementation: Shopify retries a failed webhook delivery on an escalating
schedule for up to 48 hours before it gives up, so from an app's point of view the order
webhook is the record of truth and the pixel is a hint. This module reproduces that
contract in the small: a delivery is retried until the receiver accepts it or the attempt
budget is exhausted, and **every delivery** is recorded in
:attr:`~shopify_stub.state.StubState.deliveries` whether it succeeded or not.

One row per delivery, carrying the attempt *count* — not one row per attempt. The retry
loop below runs inside a single :class:`~shopify_stub.state.WebhookDelivery`, which is
appended once, after the loop, with ``attempts`` set and ``status_code``/``error`` holding
the **last** attempt's outcome. Two 500s followed by a 200 leave one row reading
``attempts=3, delivered=True, status_code=200``, not three rows.

There is deliberately **no drop-rate knob here.** A configurable webhook loss rate would let
a consumer's test pass while its production reconciliation was wrong, because it would make
"the webhook never arrived" a normal state. It is not a normal state, and the stub refuses
to simulate it.

Signing
-------
Every delivery carries ``X-Shopify-Hmac-Sha256``: the base64 of HMAC-SHA256 over the
**raw request body bytes**, keyed by the app's client secret. "Raw bytes" is the whole
subtlety — a receiver that re-serialises the parsed JSON and signs that will produce a
different digest for the same payload whenever key order or whitespace differs, which is
the single most common webhook-verification bug. The stub therefore signs the exact bytes
it puts on the wire and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

import httpx
from shopify_stub.state import StubState, WebhookDelivery, WebhookTopic

#: Header names Shopify sends on every webhook POST.
#:
#: Casing note, measured rather than assumed: shopify.dev prints the HMAC header three
#: different ways across its own pages (``X-Shopify-Hmac-Sha256``, ``X-Shopify-Hmac-SHA256``,
#: ``x-shopify-hmac-sha256``) and states outright that header names must be treated as
#: case-insensitive because HTTP/2 lowercases them. The stub *sends* the majority spelling
#: and every receiver in this system must *read* case-insensitively.
HEADER_TOPIC = "X-Shopify-Topic"
HEADER_HMAC = "X-Shopify-Hmac-Sha256"
HEADER_SHOP_DOMAIN = "X-Shopify-Shop-Domain"
HEADER_API_VERSION = "X-Shopify-API-Version"
HEADER_WEBHOOK_ID = "X-Shopify-Webhook-Id"
HEADER_EVENT_ID = "X-Shopify-Event-Id"
HEADER_TRIGGERED_AT = "X-Shopify-Triggered-At"

#: How many times a delivery is attempted before the stub records it as failed. Real
#: Shopify retries 19 times over 48 hours; a unit test cannot wait that long, so the stub
#: keeps the *shape* (retry until accepted) and shortens the budget.
MAX_ATTEMPTS = 3

#: Per-attempt timeout. Short because every receiver in this system is on loopback.
DELIVERY_TIMEOUT_SECONDS = 5.0


def sign(body: bytes, secret: str) -> str:
    """Base64 HMAC-SHA256 of the raw body, keyed by the app's client secret.

    Args:
        body: the exact bytes that go on the wire.
        secret: the app client secret.

    Returns:
        The base64 digest, as it appears in ``X-Shopify-Hmac-Sha256``.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify(body: bytes, secret: str, header_value: str) -> bool:
    """Constant-time check of a received signature.

    Provided so consumers (and this stub's own tests) verify a delivery the same way the
    merchant app must: over the raw bytes, with :func:`hmac.compare_digest`.
    """
    expected = sign(body, secret)
    return hmac.compare_digest(expected, header_value)


def canonical_body(payload: dict[str, object]) -> bytes:
    """Serialise a webhook payload to the exact bytes that will be signed and sent.

    ``separators`` is pinned so the same payload always produces the same bytes — otherwise
    a Python version that changes default spacing would silently change every signature.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=False).encode("utf-8")


def delivery_headers(
    *,
    topic: WebhookTopic,
    shop_domain: str,
    api_version: str,
    webhook_id: str,
    signature: str,
    triggered_at: datetime,
) -> dict[str, str]:
    """The header block Shopify puts on a webhook POST."""
    return {
        "Content-Type": "application/json",
        HEADER_TOPIC: topic.value,
        HEADER_HMAC: signature,
        HEADER_SHOP_DOMAIN: shop_domain,
        HEADER_API_VERSION: api_version,
        HEADER_WEBHOOK_ID: webhook_id,
        HEADER_EVENT_ID: webhook_id,
        HEADER_TRIGGERED_AT: triggered_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


class WebhookDispatcher:
    """Delivers a payload to every subscription registered for a topic.

    Delivery is awaited inside the request that triggers it. That is a deliberate departure
    from real Shopify, which delivers asynchronously: an async background task would make
    every consumer test race the dispatcher and need a sleep or a poll loop, and a test that
    sleeps is a test that is flaky on a loaded machine. The observable contract — the
    receiver got the payload, signed, before anything downstream looked — is preserved.
    """

    def __init__(self, state: StubState) -> None:
        self._state = state

    async def dispatch(
        self,
        *,
        topic: WebhookTopic,
        payload: dict[str, object],
        now: datetime | None = None,
    ) -> list[WebhookDelivery]:
        """POST ``payload`` to every subscriber of ``topic``.

        Args:
            topic: which webhook topic this is.
            payload: the JSON body, already in Shopify's snake_case shape.
            now: the trigger instant; defaults to the stub's clock.

        Returns:
            One :class:`~shopify_stub.state.WebhookDelivery` per subscription, in
            registration order. An empty list means nothing was subscribed — which is a
            *configuration* fact the caller can assert on, never a silent success.
        """
        moment = now or self._state.now()
        body = canonical_body(payload)
        signature = sign(body, self._state.config.webhook_secret)
        records: list[WebhookDelivery] = []
        for subscription in self._state.subscriptions_for(topic):
            webhook_id = str(uuid.uuid4())
            headers = delivery_headers(
                topic=topic,
                shop_domain=self._state.config.shop_domain,
                api_version=self._state.config.api_version,
                webhook_id=webhook_id,
                signature=signature,
                triggered_at=moment,
            )
            attempts = 0
            delivered = False
            status_code: int | None = None
            error: str | None = None
            async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
                while attempts < MAX_ATTEMPTS and not delivered:
                    attempts += 1
                    try:
                        response = await client.post(
                            subscription.callback_url, content=body, headers=headers
                        )
                        status_code = response.status_code
                        # Shopify treats any 2xx as accepted and retries everything else.
                        delivered = 200 <= response.status_code < 300
                        error = None if delivered else f"HTTP {response.status_code}"
                    except httpx.HTTPError as exc:
                        status_code = None
                        error = f"{type(exc).__name__}: {exc}"
            record = WebhookDelivery(
                id=self._state.next_delivery_id(),
                topic=topic,
                callback_url=subscription.callback_url,
                payload=payload,
                webhook_id=webhook_id,
                hmac=signature,
                attempts=attempts,
                delivered=delivered,
                status_code=status_code,
                error=error,
                sent_at=moment,
            )
            self._state.deliveries.append(record)
            records.append(record)
        return records
