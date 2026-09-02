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

**Only the body is signed, so only the body may identify a delivery.** Every header on an
inbound delivery is chosen by whoever made the request, including the webhook id a receiver
is tempted to de-duplicate on. :class:`WebhookInbox` therefore keys its replay guard on
:func:`delivery_digest` of the signed body, and holds those identities in a window it
evicts on its own clock rather than in step with the event ring.

Header casing is read case-insensitively on purpose: shopify.dev prints the HMAC header
three different ways across its own pages, and HTTP/2 lower-cases header names anyway.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from merchant_svc.install.signatures import secure_equals

_log = logging.getLogger(__name__)

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

#: How many delivery identities the replay guard remembers, evicted on its own clock. It is
#: deliberately far larger than the event ring and deliberately **not** tied to it: a guard
#: that forgets an identity the moment the ring rolls over is a guard an attacker empties by
#: sending traffic, which is the one thing an attacker is always able to do.
SEEN_CAPACITY = 65536


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


def delivery_digest(body: bytes) -> str:
    """A stable identity for one delivery, derived from the material Shopify *signs*.

    The raw body is the only part of a delivery that carries a signature. Every header —
    ``X-Shopify-Webhook-Id`` included — is chosen freely by whoever made the request, so an
    identity read out of a header identifies nothing an attacker has to keep constant.
    """
    return hashlib.sha256(body).hexdigest()


def verify(body: bytes, secret: str, signature: str | None) -> bool:
    """Constant-time check of ``X-Shopify-Hmac-Sha256`` against the raw body.

    A missing signature, a blank secret, a wrong digest and a digest that is not even
    ASCII are all ``False``. The blank secret matters: an unconfigured deployment must
    refuse every delivery, never accept every delivery. The non-ASCII case matters for the
    same reason in the other direction: the header is attacker-chosen, and comparing it as
    ``str`` would raise instead of refusing — see :mod:`merchant_svc.install.signatures`.
    """
    if not signature or not secret:
        return False
    return secure_equals(sign(body, secret), signature)


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
    #: :func:`delivery_digest` of the raw body this delivery was authenticated against.
    #: The de-duplication identity that an attacker cannot vary without invalidating the
    #: signature; empty only when a caller built the event by hand.
    body_digest: str = ""

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

    De-duplication exists because Shopify retries a delivery it did not see a 2xx for, and
    a receiver that double-counts a retried ``orders/paid`` reconciles one purchase as two.
    A repeat is acknowledged (2xx, so the retries stop) and recorded once.

    **What a delivery is identified by, and why it is not the webhook id alone.**
    ``X-Shopify-Webhook-Id`` is an unsigned header: Shopify's HMAC covers the body and
    nothing else, so anyone replaying a captured delivery can put a fresh id on it — or
    drop the header, which the id-only rule never de-duplicated at all — and be counted
    again, once per replay. The identity that cannot be varied is
    :func:`delivery_digest` of the signed body, scoped to the topic it arrived under; the
    webhook id is kept alongside it so an honest retry that Shopify re-bodies still
    de-duplicates.

    **The replay window is bounded on its own clock.** The event ring is a display buffer
    and rolls over at ``capacity``; the seen-set holds :data:`SEEN_CAPACITY` identities and
    is evicted independently. Discarding an identity because the ring rolled over would
    hand an attacker the eviction for free — traffic is the one resource an attacker
    always has.
    """

    def __init__(
        self, capacity: int = INBOX_CAPACITY, *, seen_capacity: int = SEEN_CAPACITY
    ) -> None:
        self.capacity = capacity
        self.seen_capacity = max(seen_capacity, capacity)
        self._events: list[ReceivedWebhook] = []
        #: Used as an insertion-ordered set: the value is never read.
        self._seen: dict[str, None] = {}

    @staticmethod
    def _identities(event: ReceivedWebhook) -> tuple[str, ...]:
        """Every key ``event`` is de-duplicated under, most trustworthy first."""
        keys: list[str] = []
        if event.body_digest:
            keys.append(f"body:{event.topic}:{event.body_digest}")
        if event.webhook_id:
            keys.append(f"id:{event.webhook_id}")
        return tuple(keys)

    def seen(self, event: ReceivedWebhook) -> bool:
        """Whether an equivalent delivery was already recorded."""
        return any(key in self._seen for key in self._identities(event))

    def record(self, event: ReceivedWebhook) -> bool:
        """Append ``event``. ``False`` when an equivalent delivery was already recorded."""
        keys = self._identities(event)
        if any(key in self._seen for key in keys):
            return False
        for key in keys:
            self._seen[key] = None
        while len(self._seen) > self.seen_capacity:
            self._seen.pop(next(iter(self._seen)))
        self._events.append(event)
        if len(self._events) > self.capacity:
            self._events.pop(0)
        return True

    def forget(self, event: ReceivedWebhook) -> None:
        """Undo :meth:`record` for ``event``.

        Used when the delivery could not be handed on after all, so the sender's retry is
        treated as the fresh delivery it is rather than swallowed as a duplicate.
        """
        for key in self._identities(event):
            self._seen.pop(key, None)
        for index in range(len(self._events) - 1, -1, -1):
            if self._events[index] is event:
                del self._events[index]
                break

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
    A fifth answer is not a refusal but a *retry request*: a sink that raises leaves the
    delivery un-recorded and answers 500, because Shopify stops retrying on any 2xx and a
    delivery that is marked seen but never handed on is a delivery nobody will send again.

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
        body_digest=delivery_digest(body),
    )
    fresh = target.record(event)
    if fresh and _sink is not None:
        try:
            _sink(event)
        except Exception:  # noqa: BLE001 - the sink is a downstream lane's; own the failure
            # Recording marked this delivery seen. Leaving that mark in place while
            # answering non-2xx would make Shopify's retry a "duplicate" and the event
            # would be lost for good — a transient ledger outage turned permanent.
            target.forget(event)
            _log.exception(
                "webhook sink refused a %s delivery (%s); answering 500 so it is retried",
                topic,
                event.webhook_id or "<no webhook id>",
            )
            return WebhookDecision(500, "sink-failed", event=event, detail={"topic": topic})
    return WebhookDecision(
        200,
        "recorded" if fresh else "duplicate",
        event=event,
        duplicate=not fresh,
    )
