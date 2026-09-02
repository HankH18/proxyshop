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
inbound delivery is chosen by whoever made the request — the webhook id a receiver is
tempted to de-duplicate on, the topic, and the shop domain alike. :class:`WebhookInbox`
therefore keys its replay guard on :func:`delivery_digest` of the signed body and on
nothing else, and holds those identities in a window it evicts on its own clock rather
than in step with the event ring.

**The topic is read from one place, and the URL is not it.** ``handle_delivery`` used to
fall back to the route's path segment whenever ``X-Shopify-Topic`` was missing
(``topic = topic or wanted``). Nothing signs a URL, so that made the *label* on an
authenticated body free for the taking: a captured ``orders/paid`` POSTed to
``…/webhooks/shopify/refunds/create`` with the header omitted was filed as a refund, and —
because the replay guard keys on the body — the genuine ``orders/paid`` that arrived
afterwards was answered "duplicate" and dropped. One request both fabricated a refund and
destroyed the real order. The header is now the only source of a topic and its absence is a
refusal; the path segment may only *disagree* (400), never *decide*.

Neither the header nor the path is authenticated, so the residual is stated rather than
implied: an attacker holding a validly-signed body can still present it under a topic of
their choice, and if they win the race against Shopify's own delivery the genuine one is
refused as the cross-topic replay of theirs. What is closed is the free version of that
attack — no header at all — and the fabrication half: :meth:`WebhookInbox.bound_topic`
binds a signed body to the first topic it was recorded under, so the same body can never
become a second event under a second topic, and a collision is reported rather than
silently answered like an honest retry.

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

from merchant_svc.install.signatures import secure_equals, signature_bytes

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

#: How many delivery identities the replay guard remembers, evicted on its own clock. One
#: identity per delivery, so this is a count of deliveries. It is deliberately far larger
#: than the event ring and deliberately **not** tied to it: a guard that forgets an identity
#: the moment the ring rolls over is a guard an attacker empties by sending traffic, which
#: is the one thing an attacker is always able to do.
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
    digest = hmac.new(signature_bytes(secret), body, hashlib.sha256).digest()
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
    #: The whole de-duplication identity, and the only part of a delivery an attacker
    #: cannot vary without invalidating the signature; empty only when a caller built the
    #: event by hand rather than letting :func:`handle_delivery` authenticate it.
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

    **A delivery is identified by the signed body and by nothing else.** The identity is
    :func:`delivery_digest` of the raw body — precisely the bytes the HMAC covers. Three
    tempting additions are all refused, and the reason is the same each time:

    * ``X-Shopify-Webhook-Id`` is an unsigned header, so a replayer renames it (or omits
      it, which an id-only rule never de-duplicated at all) and is counted again per
      replay. It is also not safe as a *secondary* key: a genuinely new body arriving
      under an already-seen id would be dropped.
    * ``X-Shopify-Topic`` and the route path are unsigned too, so folding the topic into
      the key lets one captured signed body replay as three distinct "fresh" events — a
      paid order becoming a fabricated fulfilment and a fabricated refund. The topic is
      instead *bound* to the digest: the first topic a body is recorded under is the only
      topic it will ever be recorded under, and :meth:`bound_topic` reports which that was
      so a cross-topic replay is distinguishable from Shopify's own honest retry instead of
      being answered identically.
    * ``X-Shopify-Shop-Domain`` is unsigned as well, and the app holds **one**
      ``SHOPIFY_API_SECRET`` for every shop, so the signature cannot authenticate a shop
      either. There is no trustworthy shop identity at this layer; scoping by it would let
      any replayer mint unlimited fresh deliveries by varying that header.

    The residual, stated rather than discovered: two *genuinely distinct* deliveries whose
    bodies are byte-identical collapse into one. Real Shopify payloads carry globally
    unique resource ids and timestamps, so this is not reachable in production; it is
    reachable against ``services/shopify-stub`` when its per-instance counters restart at
    fixed values under a frozen clock.

    **The replay window is bounded on its own clock.** The event ring is a display buffer
    and rolls over at ``capacity``; the seen-set holds :data:`SEEN_CAPACITY` identities —
    one per delivery, so the window really is that many deliveries — and is evicted
    independently. Discarding an identity because the ring rolled over would hand an
    attacker the eviction for free, and traffic is the one resource an attacker always has.
    """

    def __init__(
        self, capacity: int = INBOX_CAPACITY, *, seen_capacity: int = SEEN_CAPACITY
    ) -> None:
        # At least one: a zero-capacity ring would evict an event in the same call that
        # recorded it, leaving an identity behind that :meth:`forget` could never reclaim.
        self.capacity = max(1, capacity)
        self.seen_capacity = max(seen_capacity, self.capacity)
        self._events: list[ReceivedWebhook] = []
        #: Insertion-ordered ``{body digest: the topic it was recorded under}``. The KEY is
        #: the whole de-duplication identity — the value is never consulted to decide
        #: freshness, only to *report* which topic already owns a body, so a cross-topic
        #: replay can be told apart from an honest retry. One entry per delivery, so FIFO
        #: eviction retires whole deliveries and can never strand a half-forgotten one that
        #: a replay would then slip past.
        self._seen: dict[str, str] = {}

    @staticmethod
    def _identity(event: ReceivedWebhook) -> str:
        """The one key ``event`` is de-duplicated under: its signed body, or nothing."""
        return event.body_digest

    def bound_topic(self, body_digest: str) -> str | None:
        """The topic this signed body was first recorded under, or ``None`` if unseen.

        The binding is what makes a relabelled replay *visible*. A digest is spoken for by
        exactly one topic; a later arrival of the same body under a different topic is
        still a duplicate — it must be, or one captured body would count three times — but
        the caller can now say which topic it collided with instead of answering it exactly
        as it answers Shopify's own retry.
        """
        if not body_digest:
            return None
        return self._seen.get(body_digest)

    def record(self, event: ReceivedWebhook) -> bool:
        """Append ``event``. ``False`` when an equivalent delivery was already recorded.

        An event carrying no ``body_digest`` — one a caller built by hand rather than one
        :func:`handle_delivery` authenticated — is recorded every time. There is nothing
        trustworthy to de-duplicate it on, and inventing something from its headers is the
        bug this class exists to avoid.
        """
        identity = self._identity(event)
        if identity:
            if identity in self._seen:
                return False
            self._seen[identity] = event.topic
            while len(self._seen) > self.seen_capacity:
                self._seen.pop(next(iter(self._seen)))
        self._events.append(event)
        if len(self._events) > self.capacity:
            self._events.pop(0)
        return True

    def forget(self, event: ReceivedWebhook) -> None:
        """Undo :meth:`record` for ``event``. A no-op unless this inbox recorded it.

        Used when the delivery could not be handed on after all, so the sender's retry is
        treated as the fresh delivery it is rather than swallowed as a duplicate.

        Ownership is checked first, and that check is load-bearing: a delivery *refused*
        as a duplicate never added the identity it matched, so releasing that identity on
        its behalf would hand the original's replay protection to whoever sent the
        duplicate.
        """
        for index in range(len(self._events) - 1, -1, -1):
            if self._events[index] is event:
                del self._events[index]
                break
        else:
            return
        identity = self._identity(event)
        if identity:
            self._seen.pop(identity, None)

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

#: The ``LedgerEvent.kind`` each subscribed topic becomes on the way downstream. The names
#: are ``contracts.ledger``'s frozen vocabulary, not spellings invented here.
LEDGER_KIND_FOR_TOPIC: dict[str, str] = {
    "orders/paid": "order_paid",
    "orders/fulfilled": "order_fulfilled",
    "refunds/create": "refund",
}

#: How many hand-off records the default sink keeps. Bounded for the same reason the event
#: ring is: an unbounded buffer fed by an internet-facing route is a memory exhaustion
#: primitive for anyone who can make the app accept a delivery.
HANDOFF_CAPACITY = 512


def ledger_record(event: ReceivedWebhook) -> dict[str, Any]:
    """The ledger-shaped hand-off for one authenticated delivery.

    Deliberately a plain mapping rather than a ``contracts.protocol.LedgerEvent``: E6 owns
    the ledger and its identifiers, and minting an ``event_id`` here would make this module
    the author of records another domain is responsible for. What it does own is the
    translation from Shopify's vocabulary to the frozen one — the topic becomes a
    :data:`LEDGER_KIND_FOR_TOPIC` kind, and the join keys D24 pins are lifted out of the
    body so a consumer never has to guess which spelling this vendor used.
    """
    return {
        "kind": LEDGER_KIND_FOR_TOPIC.get(event.topic, event.topic),
        "topic": event.topic,
        "store_id": event.shop_domain,
        "order_ref": event.order_ref,
        "checkout_token": event.checkout_token,
        "webhook_id": event.webhook_id,
        "received_at": event.received_at.isoformat(),
        "body_digest": event.body_digest,
        "payload": event.payload,
    }


class WebhookHandoff:
    """The bounded buffer the default sink writes to, and the seam E6 replaces.

    It exists because "no sink configured" was silently equivalent to "throw the delivery
    away". A default that keeps the authenticated, de-duplicated, ledger-shaped record —
    and says so in the log — makes an unwired downstream a *visible* gap rather than an
    invisible one, which is the same rule R4 applies to a dropped pixel event.
    """

    def __init__(self, capacity: int = HANDOFF_CAPACITY) -> None:
        self.capacity = max(1, capacity)
        self._records: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> None:
        """Keep ``record``, retiring the oldest once the buffer is full."""
        self._records.append(record)
        while len(self._records) > self.capacity:
            self._records.pop(0)

    def records(self) -> tuple[dict[str, Any], ...]:
        """Everything handed on, oldest first."""
        return tuple(self._records)

    def clear(self) -> None:
        """Forget everything. Used between tests."""
        self._records.clear()


#: Where :func:`default_sink` puts what it was handed.
HANDOFF = WebhookHandoff()


def default_sink(event: ReceivedWebhook) -> None:
    """The sink a service with no downstream wired still gets.

    Never raises. A sink that raises is answered 500 so Shopify retries (see
    :func:`handle_delivery`), and a *default* that could do that would turn "E6 is not
    deployed yet" into "every order webhook is retried forever".
    """
    HANDOFF.append(ledger_record(event))
    _log.info(
        "webhook %s handed to the default sink (order=%s checkout=%s)",
        event.topic,
        event.order_ref or "<none>",
        event.checkout_token or "<none>",
    )


#: The active sink. It starts as :func:`default_sink` rather than ``None``: ``main.create_app``
#: is frozen and mounts routers only, so a sink that had to be installed by a caller was a
#: sink no production path ever installed — every authenticated delivery the deployed
#: service received was verified, recorded in a display ring and handed to nobody.
#: :func:`set_webhook_sink` still replaces it, and ``None`` still means "inbox only", but
#: that is now a deliberate act rather than the state the app boots in.
_sink: Callable[[ReceivedWebhook], None] | None = default_sink


def set_webhook_sink(sink: Callable[[ReceivedWebhook], None] | None) -> None:
    """Route authenticated deliveries to ``sink`` as well as the inbox (``None`` clears)."""
    global _sink
    _sink = sink


def webhook_sink() -> Callable[[ReceivedWebhook], None] | None:
    """The sink deliveries are currently handed to, or ``None`` if it was cleared."""
    return _sink


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

    The five refusals, each with its own status so a misconfiguration is not mistaken for
    an attack: no ``X-Shopify-Topic`` header at all (400), a topic header that disagrees
    with the route it arrived on (400), an unsubscribed topic (404), a bad or missing
    signature (401), and a body that is not JSON (400). A sixth answer is not a refusal but
    a *retry request*: a sink that raises leaves the delivery un-recorded and answers 500,
    because Shopify stops retrying on any 2xx and a delivery that is marked seen but never
    handed on is a delivery nobody will send again.

    ``path_topic`` is a **constraint, never a source**. A missing topic header used to fall
    back to it, which let an unsigned URL segment choose the label on an authenticated body;
    see this module's header for the exploit that opened. It may now only contradict the
    header, and a contradiction is a refusal.

    Args:
        body: the exact bytes received. Never a re-serialisation.
        headers: the request headers, read case-insensitively.
        secret: the app's client secret.
        path_topic: the topic the route encodes, checked against the header when both exist.
        inbox: where to record; defaults to the module :data:`INBOX`.
        now: receipt instant.
    """
    lower = {str(key).lower(): value for key, value in headers.items()}
    target = inbox if inbox is not None else INBOX

    header_topic = lower.get(HEADER_TOPIC.lower())
    topic = normalize_topic(header_topic) if header_topic else None
    if topic is None:
        # Deliberately NOT `topic or normalize_topic(path_topic)`. The route path is not
        # covered by the HMAC, so falling back to it would let the sender label a signed
        # body with any topic they like just by omitting a header.
        return WebhookDecision(
            400,
            "missing-topic",
            detail={"path_topic": normalize_topic(path_topic)} if path_topic else {},
        )
    if path_topic is not None:
        wanted = normalize_topic(path_topic)
        if topic != wanted:
            return WebhookDecision(
                400,
                "topic-mismatch",
                detail={"path_topic": wanted, "header_topic": topic},
            )
    if topic not in TOPIC_ENUM:
        return WebhookDecision(404, "unsubscribed-topic", detail={"topic": topic})

    if not verify(body, secret, lower.get(HEADER_HMAC.lower())):
        return WebhookDecision(401, "bad-signature", detail={"topic": topic})

    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        # RecursionError, not just ValueError: a deeply nested body exhausts the decoder's
        # stack rather than failing to parse, and it arrives with a VALID signature — the
        # sender is authenticated, so this is not a forgery, it is a body that can never
        # parse. Letting it escape answered 5xx, which Shopify retries forever; the same
        # 400 every other unparseable body gets is what stops the loop.
        return WebhookDecision(400, "unparseable-body", detail={"topic": topic})
    if not isinstance(payload, dict):
        return WebhookDecision(400, "unparseable-body", detail={"topic": topic})

    digest = delivery_digest(body)
    event = ReceivedWebhook(
        topic=topic,
        shop_domain=lower.get(HEADER_SHOP_DOMAIN.lower(), ""),
        webhook_id=lower.get(HEADER_WEBHOOK_ID.lower(), ""),
        payload=payload,
        received_at=now or datetime.now(UTC),
        api_version=lower.get(HEADER_API_VERSION.lower(), ""),
        triggered_at=lower.get(HEADER_TRIGGERED_AT.lower(), ""),
        body_digest=digest,
    )
    # Read before recording: afterwards the binding is this delivery's own and says nothing.
    bound = target.bound_topic(digest)
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

    detail: dict[str, Any] = {}
    if not fresh and bound is not None and bound != topic:
        # Not Shopify retrying: Shopify retries a delivery under the topic it sent it on.
        # The same signed body under a second topic is somebody re-presenting a captured
        # delivery, and it is refused as a duplicate — but loudly, and it says so.
        detail["recorded_as"] = bound
        _log.warning(
            "a signed body already recorded as %s was re-presented as %s (%s); refused as "
            "a duplicate — the topic is not covered by the signature",
            bound,
            topic,
            event.webhook_id or "<no webhook id>",
        )
    return WebhookDecision(
        200,
        "recorded" if fresh else "duplicate",
        event=event,
        duplicate=not fresh,
        detail=detail,
    )
