"""The three order webhooks: which topics may be subscribed, and how a delivery is trusted.

**Exactly three topics, and the ceiling is the point.** SPEC C5 makes the web pixel
extension the only checkout-observation path, so subscribing ``checkouts/create`` or
``carts/update`` would build a second one out of webhooks. :func:`assert_topics_allowed`
refuses any topic outside :data:`WEBHOOK_TOPICS` before a subscription request is ever
sent, rather than leaving it to Shopify (which would happily accept them).

**A delivery is not trusted until its signature is.** Shopify signs the *raw request body
bytes* with the app's client secret. A receiver that parses the JSON and re-serialises it
before hashing computes a different digest whenever key order or spacing differs — the
single most common webhook-verification bug, and one that fails open. Everything here
works on ``bytes``.

Header casing is read case-insensitively on purpose: shopify.dev prints the HMAC header
three different ways across its own pages, and HTTP/2 lower-cases header names anyway.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: The topics this app subscribes, in the order the install registers them.
WEBHOOK_TOPICS: tuple[str, ...] = ("orders/paid", "orders/fulfilled", "refunds/create")

#: ``WebhookSubscriptionTopic`` enum spellings for :data:`WEBHOOK_TOPICS`.
TOPIC_ENUM: dict[str, str] = {
    "orders/paid": "ORDERS_PAID",
    "orders/fulfilled": "ORDERS_FULFILLED",
    "refunds/create": "REFUNDS_CREATE",
}

HEADER_TOPIC = "X-Shopify-Topic"
HEADER_HMAC = "X-Shopify-Hmac-Sha256"
HEADER_SHOP_DOMAIN = "X-Shopify-Shop-Domain"
HEADER_API_VERSION = "X-Shopify-API-Version"
HEADER_WEBHOOK_ID = "X-Shopify-Webhook-Id"
HEADER_TRIGGERED_AT = "X-Shopify-Triggered-At"

#: How many delivered webhooks the default in-process sink keeps.
INBOX_CAPACITY = 512


class ForbiddenWebhookTopic(ValueError):
    """A topic outside the three C5 allows was requested."""

    def __init__(self, offending: Iterable[str]) -> None:
        self.offending: tuple[str, ...] = tuple(sorted(offending))
        super().__init__(
            "C5 allows exactly "
            + ", ".join(WEBHOOK_TOPICS)
            + "; a second checkout-observation path is refused, not merely discouraged. "
            + "Rejected: "
            + ", ".join(self.offending)
        )


def normalize_topic(raw: str) -> str:
    """``ORDERS_PAID``, ``orders/paid`` and ``Orders/Paid`` all name one topic."""
    return str(raw).strip().lower().replace("_", "/")


def assert_topics_allowed(topics: Iterable[str]) -> tuple[str, ...]:
    """Return the normalized topic list, or raise :class:`ForbiddenWebhookTopic`.

    Raises:
        ForbiddenWebhookTopic: any topic outside :data:`WEBHOOK_TOPICS`.
        ValueError: the list is empty.
    """
    normalized = tuple(dict.fromkeys(normalize_topic(topic) for topic in topics))
    if not normalized:
        raise ValueError("at least one webhook topic must be subscribed")
    offending = [topic for topic in normalized if topic not in TOPIC_ENUM]
    if offending:
        raise ForbiddenWebhookTopic(offending)
    return normalized


def sign(body: bytes, secret: str) -> str:
    """Base64 HMAC-SHA256 over the raw body bytes, keyed by the app's client secret."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify(body: bytes, secret: str, signature: str | None) -> bool:
    """Constant-time check of ``X-Shopify-Hmac-Sha256`` against the raw body.

    A missing signature, a blank secret and a wrong digest are all ``False``. The blank
    secret matters: an unconfigured deployment must refuse every delivery, never accept
    every delivery.
    """
    if not signature or not secret:
        return False
    return hmac.compare_digest(sign(body, secret), signature)


@dataclass(frozen=True)
class ReceivedWebhook:
    """One authenticated delivery."""

    topic: str
    shop_domain: str
    webhook_id: str
    payload: dict[str, Any]
    received_at: datetime
    api_version: str = ""
    triggered_at: str = ""

    @property
    def order_ref(self) -> str | None:
        """The order's Admin GraphQL id, when the payload carries one."""
        for key in ("admin_graphql_api_id", "admin_graphql_api_order_id"):
            value = self.payload.get(key)
            if isinstance(value, str) and value:
                return value
        order_id = self.payload.get("order_id", self.payload.get("id"))
        return f"gid://shopify/Order/{order_id}" if order_id is not None else None

    @property
    def checkout_token(self) -> str | None:
        """The pixel↔webhook join key, in the order webhook's spelling."""
        value = self.payload.get("checkout_token")
        return value if isinstance(value, str) else None


class WebhookInbox:
    """A bounded, de-duplicated log of authenticated deliveries.

    De-duplication is on ``X-Shopify-Webhook-Id``: Shopify retries a delivery it did not
    see a 2xx for, and a receiver that double-counts a retried ``orders/paid`` reconciles
    one purchase as two. A repeat is acknowledged (2xx, so the retries stop) and recorded
    once.
    """

    def __init__(self, capacity: int = INBOX_CAPACITY) -> None:
        self.capacity = capacity
        self._events: list[ReceivedWebhook] = []
        self._seen: set[str] = set()

    def record(self, event: ReceivedWebhook) -> bool:
        """Append ``event``. ``False`` when its webhook id was already recorded."""
        if event.webhook_id and event.webhook_id in self._seen:
            return False
        if event.webhook_id:
            self._seen.add(event.webhook_id)
        self._events.append(event)
        if len(self._events) > self.capacity:
            dropped = self._events.pop(0)
            self._seen.discard(dropped.webhook_id)
        return True

    def events(self, topic: str | None = None) -> tuple[ReceivedWebhook, ...]:
        """Everything recorded, optionally filtered to one topic, in arrival order."""
        if topic is None:
            return tuple(self._events)
        wanted = normalize_topic(topic)
        return tuple(event for event in self._events if event.topic == wanted)

    def clear(self) -> None:
        """Forget everything. Used between tests."""
        self._events.clear()
        self._seen.clear()


#: The default sink. Downstream lanes (E6's ledger writer) replace it with
#: :func:`set_webhook_sink` rather than editing this module.
INBOX = WebhookInbox()

_sink: Callable[[ReceivedWebhook], None] | None = None


def set_webhook_sink(sink: Callable[[ReceivedWebhook], None] | None) -> None:
    """Route authenticated deliveries to ``sink`` as well as the inbox (``None`` clears)."""
    global _sink
    _sink = sink


@dataclass
class WebhookDecision:
    """The verdict on one inbound delivery: what to answer, and why."""

    status_code: int
    reason: str
    event: ReceivedWebhook | None = None
    duplicate: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        """Shopify treats any 2xx as accepted and retries everything else."""
        return 200 <= self.status_code < 300


def handle_delivery(
    *,
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    path_topic: str | None = None,
    inbox: WebhookInbox | None = None,
    now: datetime | None = None,
) -> WebhookDecision:
    """Authenticate one delivery and record it. Pure apart from the inbox write.

    The four refusals, each with its own status so a misconfiguration is not mistaken for
    an attack: an unsubscribed topic (404), a topic header that disagrees with the route it
    arrived on (400), a bad or missing signature (401), and a body that is not JSON (400).

    Args:
        body: the exact bytes received. Never a re-serialisation.
        headers: the request headers, read case-insensitively.
        secret: the app's client secret.
        path_topic: the topic the route encodes, when the route encodes one.
        inbox: where to record; defaults to the module :data:`INBOX`.
        now: receipt instant.
    """
    lower = {str(key).lower(): value for key, value in headers.items()}
    target = inbox if inbox is not None else INBOX

    header_topic = lower.get(HEADER_TOPIC.lower())
    topic = normalize_topic(header_topic) if header_topic else None
    if path_topic is not None:
        wanted = normalize_topic(path_topic)
        if topic is not None and topic != wanted:
            return WebhookDecision(
                400,
                "topic-mismatch",
                detail={"path_topic": wanted, "header_topic": topic},
            )
        topic = topic or wanted
    if topic is None:
        return WebhookDecision(400, "missing-topic")
    if topic not in TOPIC_ENUM:
        return WebhookDecision(404, "unsubscribed-topic", detail={"topic": topic})

    if not verify(body, secret, lower.get(HEADER_HMAC.lower())):
        return WebhookDecision(401, "bad-signature", detail={"topic": topic})

    try:
        payload = json.loads(body)
    except ValueError:
        return WebhookDecision(400, "unparseable-body", detail={"topic": topic})
    if not isinstance(payload, dict):
        return WebhookDecision(400, "unparseable-body", detail={"topic": topic})

    event = ReceivedWebhook(
        topic=topic,
        shop_domain=lower.get(HEADER_SHOP_DOMAIN.lower(), ""),
        webhook_id=lower.get(HEADER_WEBHOOK_ID.lower(), ""),
        payload=payload,
        received_at=now or datetime.now(UTC),
        api_version=lower.get(HEADER_API_VERSION.lower(), ""),
        triggered_at=lower.get(HEADER_TRIGGERED_AT.lower(), ""),
    )
    fresh = target.record(event)
    if fresh and _sink is not None:
        _sink(event)
    return WebhookDecision(
        200,
        "recorded" if fresh else "duplicate",
        event=event,
        duplicate=not fresh,
    )
