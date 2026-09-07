"""The merchant composition root: where an authenticated order webhook reaches E6's ledger.

WHAT WAS MEASURED, AND WHY THIS MODULE EXISTS
---------------------------------------------
``install/webhooks.py`` verifies a signed Shopify delivery, de-duplicates it, and builds a
ledger-shaped record from it — and then handed that record to :func:`~merchant_svc.install.
webhooks.default_sink`, which appended it to the module-level ``HANDOFF`` ring and stopped.
That ring drains nowhere. Its own docstring said why it was acceptable: it is "the sink a
service with no downstream wired still gets", written while E6 was not deployed.

E6 is deployed. Measured before this module existed, four processes on loopback — a
shopify-stub, the merchant service, a real ``apps/trust``, all reached over HTTP — a shopper
completing a real checkout produced::

    stub  POST /_stub/checkouts/{token}/complete  -> 201, one orders/paid delivery, accepted
    merchant  POST /webhooks/shopify/orders/paid  -> 200 {"status":"recorded"}
    trust     GET  /events                        -> {"events":[],"count":0}
    merchant  HANDOFF ring                        -> ['gid://shopify/Order/5500000000001']

R4 makes the order webhook the **authoritative** half of the pixel↔webhook reconciliation,
and the authoritative half was terminating in a display buffer.

WHAT IT DOES, AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------
It owns three things and no more:

* **The publisher.** One :class:`~proxyshop_support.trust_ledger.TrustLedgerPublisher` per
  process, built from configuration on first use. That class is the SHARED cross-service
  ledger writer — the exchange writes through the same one — so merchant grows no fourth
  hand-rolled HTTP client and no dependency on ``apps/exchange``, which is the property
  ``proxyshop_support.trust_ledger``'s own header records as the reason it was lifted out.
* **The event id.** ``install/webhooks.ledger_record`` deliberately mints none: "E6 owns the
  ledger and its identifiers". A composition root is where a service decides who it is, so
  the id is minted here — and it is derived from the SIGNED BODY's digest, so it is stable
  across processes. See :func:`ledger_event_id`.
* **The projection.** Shopify's vocabulary into ``contracts.ledger``'s frozen one. Not the
  raw vendor body: see :func:`ledger_payload`.

It does not decide whether a delivery is authentic (``install/webhooks.py`` does), it does
not reconcile anything (``apps/trust``'s ``reconcile`` engine does), and it never fails a
request. :meth:`TrustLedgerPublisher.publish` never raises and :func:`publish_ledger_record`
adds the same guarantee over the projection, because the caller is ``default_sink`` and a
default sink that can raise turns "trust is down" into "every order webhook is retried
forever" — the exact failure that docstring was written to prevent.

WHAT THIS COSTS THE REQUEST PATH, STATED RATHER THAN DISCOVERED
----------------------------------------------------------------
The write is synchronous and inline: one POST per delivery, bounded by
:data:`~proxyshop_support.trust_ledger.DEFAULT_LEDGER_TIMEOUT_SECONDS` (0.5s). That is the
same posture the exchange takes through the same class, with one difference worth naming —
``install/routes.py``'s ``receive_webhook`` is an ``async def``, so the write holds the
merchant's event loop for that long in the worst case (a trust host that is routable but
silent; a refused connection returns immediately, which is what the loopback outage test
measures).

It is deliberately NOT moved to a worker thread, and the reason is not laziness:
``handle_delivery`` calls the sink INSIDE the window where it has already recorded the
delivery, and ``WebhookInbox``'s replay guard is a check-then-write over a plain dict with
no lock. Today it is safe because an ``async def`` route runs it on one thread; handing that
call to the anyio threadpool would make two concurrent deliveries race the seen-set, and a
replay guard that loses a race is a replay guard. Trading a correctness property for latency
on the audit write is the wrong trade, so the latency is bounded and written down instead.

WHERE THE ADDRESS COMES FROM, AND WHAT MUST SHIP IT
----------------------------------------------------
:func:`~proxyshop_support.trust_ledger.trust_endpoint` resolves ``TRUST_URL`` and then falls
back to ``http://trust:8084`` — the compose service name and published port of
``apps/trust/compose.yaml``. So **inside the shipped compose network this needs no
configuration at all**, which is deliberate: a service that has to be told where to write its
audit trail before it writes one is a service that ships not writing one.

The one artifact that would make the address *operator-settable* is
``apps/merchant/compose.yaml`` (owned by another lane, unchanged here), which today forwards
no ``TRUST_URL``. Adding::

    TRUST_URL: "${TRUST_URL:-http://trust:8084}"

to its ``environment:`` block is the whole change, and until it is there the default is what
the container uses. No new dependency ships either: ``proxyshop_support`` is already on the
merchant image (``main.py`` imports ``proxyshop_support.logging_config``) and ``httpx`` is
already a merchant dependency (``install/routes.py`` imports it) — the publisher imports it
lazily on first publish, so composing the service still opens no socket.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from merchant_svc.install.shop import InvalidShopDomain, normalize_shop_domain

from proxyshop_support.trust_ledger import (
    DEFAULT_LEDGER_TIMEOUT_SECONDS,
    DEFAULT_TRUST_URL,
    ENV_TRUST_URL,
    SOURCE_DEFAULT,
    SOURCE_ENVIRONMENT,
    SOURCE_STATED,
    TRUST_EVENTS_PATH,
    TrustLedgerPublisher,
    trust_endpoint,
)

_log = logging.getLogger(__name__)

__all__ = [
    "CLIENT_ID_ATTRIBUTE",
    "DEFAULT_LEDGER_TIMEOUT_SECONDS",
    "DEFAULT_TRUST_URL",
    "ENV_TRUST_URL",
    "EVENT_ID_PREFIX",
    "LEDGER_SUBJECT",
    "MAX_DISCOUNT_CODES",
    "MAX_IDENTIFIER_LENGTH",
    "TRUST_EVENTS_PATH",
    "ledger_event",
    "ledger_event_id",
    "ledger_payload",
    "ledger_status",
    "publish_ledger_record",
    "set_trust_publisher",
    "trust_publisher",
]

#: How this process names itself in the publisher's two state-change log lines.
LEDGER_SUBJECT = "merchant order webhooks"

#: The first component of every ``event_id`` this service mints. It says which service
#: authored the row, in a ledger three services write to.
EVENT_ID_PREFIX = "merchant"

#: The ceiling ``trust.events.routes.MAX_IDENTIFIER_LENGTH`` puts on ``event_id``,
#: ``store_id`` and ``order_ref``, restated because merchant is the PRODUCER and an
#: identifier refused at the door is an event that did not land.
#:
#: It is not decoration on this side either. ``store_id`` is read from
#: ``X-Shopify-Shop-Domain``, and that header is **not covered by the HMAC** — see
#: ``install/webhooks.py``'s header on why no header can identify a delivery. Whoever can
#: present a signed body chooses it freely, and the ledger it would land in is append-only:
#: a ``BEFORE UPDATE OR DELETE ... ENABLE ALWAYS`` trigger means a row admitted once is
#: there forever. So the shop domain is put through merchant's own
#: :func:`~merchant_svc.install.shop.normalize_shop_domain` and dropped if it is not a bare
#: ``.myshopify.com`` host, rather than forwarded.
MAX_IDENTIFIER_LENGTH = 128

#: Characters an identifier may not carry, because ``POST /events`` interpolates ``event_id``
#: into a ``Location`` header: the C0 controls (CR, LF and NUL among them), DEL, and the C1
#: controls. Same expression ``trust.events.routes`` screens with; measured there, a
#: control character in an identifier made uvicorn drop the connection with no response at
#: all *after* the row had committed.
_UNRENDERABLE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: Everything outside this is stripped out of the ``kind`` component of an ``event_id``.
_ID_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")

#: The cart note attribute the web pixel writes the ProxyShop client id into
#: (``pixel/tests/beacon.test.ts`` asserts the beacon propagates it; the stub's
#: ``shopify_stub.orders.CLIENT_ID_ATTRIBUTE`` is the same string). It is D24's ``client_id``
#: join key in the only spelling an order webhook carries it, and nothing in this repository
#: read it off a webhook before — so the ledger's two checkout halves could join on a token
#: and never on the client.
CLIENT_ID_ATTRIBUTE = "proxyshop_client_id"

#: How many discount codes one projected payload may name. The reconciler reads codes as
#: join keys and caps each at 255 characters; this caps the count, so a signed body carrying
#: ten thousand of them cannot push one event past the door's 64 KiB body limit and be lost.
MAX_DISCOUNT_CODES = 10

#: How a wiring-time log line spells where the address came from.
_SOURCE_PHRASE = {
    SOURCE_STATED: "stated by the caller",
    SOURCE_ENVIRONMENT: f"the {ENV_TRUST_URL} environment variable",
    SOURCE_DEFAULT: "the built-in default",
}

_publisher: TrustLedgerPublisher | None = None
_publisher_lock = threading.Lock()


# =====================================================================================
# The publisher
# =====================================================================================
def trust_publisher() -> TrustLedgerPublisher:
    """The one publisher this process writes ledger events through, built on first use.

    Lazy for the reason ``main.create_app`` is frozen: there is no start-up hook to build it
    in, and building it at import time of this module would put a decision about another
    service's address into the import graph of anything that merely wants
    :func:`ledger_event`. Lazy is also free — the publisher's HTTP client is itself deferred
    to its first publish, so a process that receives no webhook opens no socket.
    """
    global _publisher
    with _publisher_lock:
        if _publisher is None:
            url, source = trust_endpoint()
            _publisher = TrustLedgerPublisher(url, log=_log, subject=LEDGER_SUBJECT)
            _log.info(
                "%s are written to the trust ledger at %s (%s)",
                LEDGER_SUBJECT,
                url,
                _SOURCE_PHRASE.get(source, source),
            )
        return _publisher


def set_trust_publisher(publisher: TrustLedgerPublisher | None) -> None:
    """Install ``publisher``; ``None`` drops it so the next call rebuilds from configuration.

    ``None`` is a *rebuild*, never an unwiring — the same rule
    :func:`~merchant_svc.install.webhooks.set_webhook_sink` follows and for the same reason:
    a caller restoring what it borrowed writes ``set_trust_publisher(None)`` in a ``finally``
    meaning "put it back", and a process that read that as "stop writing the audit trail"
    would be silently unwired by its own cleanup (T-247).
    """
    global _publisher
    with _publisher_lock:
        _publisher = publisher


def ledger_status() -> dict[str, Any]:
    """The delivery condition of this process's ledger writes, for an operator or a health route.

    Never builds a publisher: a status call is a question, and a question must not be the
    thing that decides which trust service this process talks to. A process that has not
    published yet says so rather than inventing counters.
    """
    with _publisher_lock:
        publisher = _publisher
    if publisher is None:
        url, source = trust_endpoint()
        return {
            "url": url,
            "source": source,
            "delivering": None,
            "delivered": 0,
            "lost": 0,
            "last_failure": None,
        }
    return publisher.status()


# =====================================================================================
# The projection: Shopify's vocabulary into contracts.ledger's frozen one
# =====================================================================================
def _identifier(value: Any) -> str | None:
    """``value`` as an identifier the ledger door will accept, or ``None``.

    Dropped rather than truncated. Truncating an identifier does not make it shorter, it
    makes it a *different* identifier — one that names nothing and joins to nothing — and
    the reconciler would then group two unrelated orders under the same prefix.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_IDENTIFIER_LENGTH or _UNRENDERABLE.search(text):
        return None
    return text


def _store_id(record: Mapping[str, Any]) -> str | None:
    """The shop this delivery claims to be from, if it is a shop domain at all.

    The claim arrives in an unsigned header, so it is checked against merchant's own domain
    rule rather than forwarded. A value that is not a bare ``.myshopify.com`` host leaves
    ``store_id`` absent, which files the event in the reconciler's unscoped bucket — the
    honest answer to "which store is this?" when nothing trustworthy said.
    """
    raw = record.get("store_id")
    if raw is None:
        return None
    try:
        domain = normalize_shop_domain(str(raw))
    except InvalidShopDomain:
        _log.warning(
            "an authenticated %s delivery carried a shop-domain header that is not a bare "
            "myshopify host; the ledger event is written without a store_id rather than "
            "with an unverifiable one",
            record.get("topic") or "webhook",
        )
        return None
    return _identifier(domain)


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    """One scalar field as text, or ``None``.

    A container is ``None`` rather than its ``repr``: ``str(["PSX-A"])`` is a perfectly
    good-looking string that would become the join key ``['PSX-A']`` and match nothing —
    the same trap ``trust.reconcile.engine.discount_codes_of`` documents on its own side.
    """
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, (Mapping, Sequence)) and not isinstance(value, str):
        return None
    text = str(value).strip()
    return text or None


def _discount_codes(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """The order's discount codes, in the list shape a real ``orders/paid`` body carries.

    Kept because it is the *bridge* key: ``trust.reconcile.engine.discount_codes_of`` reads
    ``discount_codes[].code`` to join a merchant's order to the exchange's offer, and a real
    webhook body carries no scalar ``discount_code`` at all. Dropping the list here would
    leave the code readable on the exchange's half of the checkout and nowhere on the
    merchant's, which is indistinguishable from no join.
    """
    entries = payload.get("discount_codes")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return []
    codes: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        code = _text(entry, "code")
        if code is None or len(code) > 255:
            continue
        codes.append({"code": code})
        if len(codes) >= MAX_DISCOUNT_CODES:
            break
    return codes


def _client_id(payload: Mapping[str, Any]) -> str | None:
    """D24's ``client_id`` join key, lifted out of the cart note attributes."""
    attributes = payload.get("note_attributes")
    if not isinstance(attributes, Sequence) or isinstance(attributes, (str, bytes)):
        return None
    for attribute in attributes:
        if not isinstance(attribute, Mapping):
            continue
        name = attribute.get("name") or attribute.get("key")
        if str(name) == CLIENT_ID_ATTRIBUTE:
            return _text(attribute, "value")
    return None


def _fulfilled_at(payload: Mapping[str, Any]) -> str | None:
    """When the order was fulfilled: the newest fulfillment's stamp, else the order's own."""
    fulfillments = payload.get("fulfillments")
    if isinstance(fulfillments, Sequence) and not isinstance(fulfillments, (str, bytes)):
        stamps: list[str] = []
        for entry in fulfillments:
            if not isinstance(entry, Mapping):
                continue
            stamp = _text(entry, "created_at")
            if stamp is not None:
                stamps.append(stamp)
        if stamps:
            return max(stamps)
    return _text(payload, "updated_at")


def _refund_amount(payload: Mapping[str, Any]) -> str | None:
    """What the refund moved, summed over its transactions, as a decimal STRING.

    A string because that is what Shopify sends (``"12.50"``, never ``12.5``) and because
    the ledger hashes what it stores: a float would be re-normalised by ``jsonb`` and the
    row read back would not hash to the digest written.
    """
    transactions = payload.get("transactions")
    if not isinstance(transactions, Sequence) or isinstance(transactions, (str, bytes)):
        return None
    total = Decimal("0")
    seen = False
    for entry in transactions:
        if not isinstance(entry, Mapping):
            continue
        raw = _text(entry, "amount")
        if raw is None:
            continue
        try:
            total += Decimal(raw)
        except (InvalidOperation, ValueError):
            continue
        seen = True
    return f"{total:f}" if seen else None


def ledger_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    """The frozen per-kind payload for one hand-off record.

    **A projection, not the vendor body, and that is the deliberate half.** Three reasons,
    in the order they bite:

    * ``POST /events`` refuses a body over 64 KiB and the ledger is append-only, so a
      forwarded ``orders/paid`` — 88 top-level keys on a real one, with every line item —
      is an event that is sometimes silently *lost* rather than sometimes large.
    * Nothing on that router is authenticated, so whatever is forwarded is readable by
      anyone who can reach the trust service, forever. C5 keeps merchant off the
      protected-customer-data scopes precisely so it never holds that data; forwarding the
      body wholesale would be the one place it started.
    * ``contracts.ledger.LEDGER_PAYLOAD_SHAPES`` publishes the shape of each kind and says
      the check belongs "at the PRODUCING boundary". This is that boundary.

    Every key the trust reconciler actually reads off an ``order_paid`` is here —
    ``checkout_token``, ``order_ref``, ``total_price``, ``current_total_price`` and the
    ``discount_codes`` bridge — plus ``body_digest``, which ties the row to the exact signed
    bytes it was derived from, so an auditor can hold the ledger against the raw delivery
    rather than taking merchant's word for the translation.
    """
    kind = str(record.get("kind") or "")
    body = record.get("payload")
    payload: Mapping[str, Any] = body if isinstance(body, Mapping) else {}
    order_ref = _identifier(record.get("order_ref"))

    common: dict[str, Any] = {"order_ref": order_ref}
    digest = _text(record, "body_digest")
    if digest is not None:
        common["body_digest"] = digest

    if kind == "order_paid":
        return {
            **common,
            "checkout_token": _text(record, "checkout_token") or _text(payload, "checkout_token"),
            "total_price": _text(payload, "total_price"),
            "current_total_price": _text(payload, "current_total_price"),
            "discount_codes": _discount_codes(payload),
            "client_id": _client_id(payload),
        }
    if kind == "order_fulfilled":
        return {**common, "fulfilled_at": _fulfilled_at(payload)}
    if kind == "refund":
        return {
            **common,
            "amount": _refund_amount(payload),
            "reason": _text(payload, "note"),
        }
    # An unmapped topic cannot reach here through `handle_delivery` — it refuses anything
    # outside the three C5 allows — so this is the hand-built-record case, and the honest
    # answer is the join keys and nothing invented.
    return common


def ledger_event_id(record: Mapping[str, Any]) -> str:
    """The idempotency key for one delivery: ``merchant-<kind>-<digest of the signed body>``.

    **Derived, not minted, and that is what makes a restart safe.** Shopify retries any
    delivery it did not see a 2xx for. ``WebhookInbox`` de-duplicates those — but only
    within one process's memory, so a merchant that restarts mid-outage hands the retry to
    the sink as a fresh delivery. A random id would then be a second row for one purchase,
    in a ledger that cannot delete it. The body digest is the same on both, so the second
    write is answered ``200 Idempotent-Replay: true`` and the chain holds one row.

    A record with no digest — one a caller built by hand rather than one ``handle_delivery``
    authenticated — gets a random id, because there is nothing stable to derive one from and
    inventing stability is worse than admitting there is none.
    """
    kind = _ID_SAFE.sub("", str(record.get("kind") or "webhook"))[:32] or "webhook"
    digest = _ID_SAFE.sub("", str(record.get("body_digest") or ""))[:64]
    suffix = digest or uuid.uuid4().hex
    return f"{EVENT_ID_PREFIX}-{kind}-{suffix}"


def ledger_event(record: Mapping[str, Any]) -> dict[str, Any]:
    """One hand-off record as the ``LedgerEvent`` body ``POST /events`` stores.

    Exactly ``trust.ledger.canonical.EVENT_FIELDS`` and nothing else: an extra top-level key
    IS hashed but has no column to live in, so the row read back would not hash to the digest
    written and the append is refused. ``auction_id`` is absent on purpose — merchant does
    not know which auction produced a checkout, and a ``None`` would be dropped anyway.
    """
    event: dict[str, Any] = {
        "event_id": ledger_event_id(record),
        "ts": str(record.get("received_at") or ""),
        "kind": str(record.get("kind") or ""),
        "payload": ledger_payload(record),
    }
    store_id = _store_id(record)
    if store_id is not None:
        event["store_id"] = store_id
    order_ref = _identifier(record.get("order_ref"))
    if order_ref is not None:
        event["order_ref"] = order_ref
    return event


def publish_ledger_record(record: Mapping[str, Any]) -> bool:
    """Write one hand-off record to the trust ledger. ``True`` when the chain took it.

    **Never raises, and that is a requirement rather than a courtesy.** Its caller is
    :func:`~merchant_svc.install.webhooks.default_sink`, and ``handle_delivery`` answers a
    sink that raises with **500** so the sender retries. Shopify retries a webhook for up to
    48 hours; a projection bug or a trust outage that could raise here would therefore turn
    one bad delivery into an unbounded retry storm against a delivery the merchant has
    already accepted and already recorded.

    :meth:`TrustLedgerPublisher.publish` supplies half of that guarantee — every transport
    failure mode of an outbound POST is caught there and counted. This function supplies the
    other half, which that class cannot: the projection above reads a vendor body, and a body
    shaped in a way no sample anticipated must be a lost event with a reason attached, not an
    exception on the request path.
    """
    try:
        event = ledger_event(record)
    except Exception:  # noqa: BLE001 - see the docstring: this must not reach the route
        _log.exception(
            "a %s hand-off record could not be projected into a LedgerEvent; it is NOT in "
            "the chained ledger. The delivery itself stands and is in the in-process ring",
            record.get("topic") or record.get("kind") or "webhook",
        )
        return False
    return trust_publisher().publish(event)
