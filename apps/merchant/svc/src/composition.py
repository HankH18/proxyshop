"""The merchant composition root: where a checkout observation reaches E6's ledger.

TWO PRODUCERS, ONE PUBLISHER
-----------------------------
This module wires both halves of R4's pixel↔webhook reconciliation into ``apps/trust``:

* the **authoritative** half — an authenticated ``orders/paid`` delivery — through
  :func:`publish_ledger_record`, which ``install/webhooks.default_sink`` calls;
* the **lossy** half — a client-side web-pixel beacon — through
  :func:`publish_pixel_observation`, which ``collector/routes.collect_pixel_event`` calls.

They share ONE :class:`~proxyshop_support.trust_ledger.TrustLedgerPublisher` per process, so
``ledger_status()`` answers one delivery condition for the service rather than two that can
disagree, and a trust outage is reported once rather than twice.


WHAT WAS MEASURED, AND WHY THIS MODULE EXISTS (the webhook half)
-----------------------------------------------------------------
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

WHAT WAS MEASURED, AND WHY THE PIXEL HALF WAS ADDED (T-051 → E6)
------------------------------------------------------------------
The lossy half terminated in a display buffer too, and for longer. ``collector/routes.py``
accepted a beacon, recorded it into :data:`~merchant_svc.collector.PIXEL_INBOX` — a 512-slot
in-process ring — and stopped; its own docstring named "E6's reconciler" as the ring's
reader, which E6 is not and cannot be, since they are separate deployables. Measured on the
served routes::

    merchant  POST /pixel/collect  -> 204
    merchant  PIXEL_INBOX          -> one joinable PixelObservation
    trust     GET  /events         -> {"events":[],"count":0}
    trust     GET  /reconcile      -> every purchase "pixel_missing": true

So the frozen ledger kind ``checkout_pixel`` had **no producer anywhere in the tree**, and
``trust.reconcile.engine`` — a THREE-way join over ``accepted`` / ``checkout_pixel`` /
``order_paid`` — ran permanently on two inputs. :func:`publish_pixel_observation` is the
missing producer; ``apps/merchant/svc/tests/test_pixel_ledger.py`` is the gate.

THE ONE PINNED KEY THE SHIPPED EMITTER DOES NOT SEND
------------------------------------------------------
``LEDGER_PAYLOAD_SHAPES["checkout_pixel"]`` is ``(checkout_token, client_id, total_price)``.
The extension emits four keys — ``COLLECTOR_BODY_KEYS`` in ``pixel/src/beacon.ts`` is
``clientId, checkoutToken, orderId, discountApplications`` — and **the order total is not
one of them**, so measured against the real pixel's real body::

    projected payload  {"checkout_token": "0f3d…", "client_id": "6f1a…",
                        "total_price": null, "gaps": []}
    validate_ledger_payload("checkout_pixel", …)
        -> ["'checkout_pixel' payload is missing published key 'total_price'"]

That is a gap in the EMITTER, not in this projection: the collector's door already accepts
``totalPrice`` (``collector.CARRIED_FIELDS``), :func:`pixel_ledger_payload` already carries
it, and with a beacon that sends one the same check returns ``[]``. What it costs while it
is missing is ``reconciled_event``'s ``pixel_price`` and ``pixel_agrees`` — measured on a
served ``GET /reconcile``, permanently ``null`` and ``false`` — which is exactly the "is
this store's pixel integration honest" diagnostic. Closing it is a two-lane change:
``pixel/src/beacon.ts`` (add ``totalPrice`` from ``data.checkout.totalPrice.amount``, a
``MoneyV2`` with a NUMERIC amount) plus the recorded fixture and stub payload it is graded
against, ``services/shopify-stub/fixtures/recorded/web_pixel_checkout_completed.json`` and
``shopify_stub.telemetry.collector_payload``, neither of which is this lane's to edit.
``test_a_beacon_that_carries_the_total_makes_pixel_agrees_a_live_diagnostic`` proves the
whole path works the moment it arrives.

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
  the id is minted here — and it is DERIVED rather than random, so it is stable across
  processes and a replay collapses to one row in an append-only chain. The webhook's comes
  from the signed body's digest (:func:`ledger_event_id`); the pixel has no signed bytes, so
  its comes from the projected body's (:func:`pixel_ledger_event`).
* **The projection.** Shopify's and the browser's vocabularies into ``contracts.ledger``'s
  frozen one. Never the raw body: see :func:`ledger_payload` and
  :data:`PUBLISHED_PIXEL_FIELDS`.

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

import hashlib
import json
import logging
import math
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from merchant_svc.collector import GAP_KEYS, MAX_PIXEL_FIELD_CHARS
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
    "PIXEL_KIND",
    "PUBLISHED_PIXEL_FIELDS",
    "TRUST_EVENTS_PATH",
    "ledger_event",
    "ledger_event_id",
    "ledger_payload",
    "ledger_status",
    "pixel_ledger_event",
    "pixel_ledger_payload",
    "publish_ledger_record",
    "publish_pixel_observation",
    "set_trust_publisher",
    "trust_publisher",
]

#: How this process names itself in the publisher's two state-change log lines.
#:
#: "checkout observations", not "order webhooks", since the pixel half started publishing
#: through the same publisher. Measured with the old wording, against a dead trust port::
#:
#:     ERROR merchant_svc.composition  merchant order webhooks: http://…/events stopped
#:     accepting ledger events (ConnectError: …, on a checkout_pixel event)
#:
#: — a line that names the wrong producer in its subject and the right one in its detail,
#: which is exactly the sentence an operator reads twice and then mistrusts.
LEDGER_SUBJECT = "merchant checkout observations"

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

#: The frozen ``LedgerEventKind`` a web-pixel observation lands under (C11/D24).
PIXEL_KIND = "checkout_pixel"

#: Every key :func:`pixel_ledger_payload` may put on a ``checkout_pixel`` row, and no sixth.
#:
#: **This is a privacy boundary, not a convenience.** The beacon behind it runs in a
#: shopper's browser during checkout on the merchant's own store; ``POST /pixel/collect``
#: takes no credential; and the ledger it lands in is append-only (a ``BEFORE UPDATE OR
#: DELETE ... ENABLE ALWAYS`` trigger), unauthenticated to read, and therefore public
#: forever. R5 promises stores never receive buyer identity. A key that reaches this row
#: cannot be taken back, so the projection is built FROM this list rather than filtered
#: against it — the same construction, and for the same reason, as
#: ``merchant_svc.collector.ACCEPTED_FIELDS`` and ``pixel/src/beacon.ts``'s
#: ``COLLECTOR_BODY_KEYS``. A copy-then-delete accepts every field nobody thought of.
#:
#: Why each one is safe to publish:
#:
#: ``checkout_token``
#:     The platform's per-checkout token. Network-issued, scoped to one checkout, names no
#:     person, and already public on the ``order_paid`` half of the same purchase. It is the
#:     value both halves share, so it is what makes them meet.
#: ``client_id``
#:     D24's pinned join key, and R5's "rotating pseudonym" rather than an identity. The
#:     ORDER webhook already publishes it (:data:`CLIENT_ID_ATTRIBUTE`, lifted out of the
#:     cart note attributes); filing the pixel half under any other key would leave the two
#:     halves of one purchase unable to meet, which is the whole defect being fixed.
#: ``total_price``
#:     A fact about an order, not about a person, and the only thing
#:     ``trust.reconcile.engine.reconciled_event`` reads off a pixel (``pixel_price`` /
#:     ``pixel_agrees`` — the "is this store's integration honest" diagnostic).
#: ``gaps``
#:     Which join keys the beacon did not carry, drawn from the collector's own fixed
#:     :data:`~merchant_svc.collector.GAP_KEYS` vocabulary. This repository's words, never
#:     the sender's, and the reason ``PixelObservation.gaps`` exists at all: "a dropped
#:     beacon is a visible gap, not a default", which is only true if the gap travels with
#:     the observation.
#:
#: **What is deliberately absent**, each for a stated reason:
#:
#: * the raw beacon body — the events door refuses >64 KiB and nothing on that router is
#:   authenticated, so a forwarded body is both sometimes silently lost and permanently
#:   public. ``contracts.ledger`` says the shape check belongs "at the PRODUCING boundary";
#:   this is that boundary, exactly as :func:`ledger_payload` is for the webhook.
#: * ``order_ref`` — see :func:`pixel_ledger_payload`.
#: * the discount code — ``trust.reconcile.engine`` excludes ``checkout_pixel`` from
#:   ``_CODE_BEARING_KINDS`` by design ("honouring a code off the pixel would hand a
#:   client-side beacon the power to decide which order gets graded"), so publishing one
#:   buys no join at all while putting a live redeemable code on a second public row.
#: * ``currency`` and ``timestamp`` — accepted at the door and already discarded by
#:   ``accept_pixel_event``; nothing reads them and the receipt instant is the event's own
#:   ``ts``.
PUBLISHED_PIXEL_FIELDS: tuple[str, ...] = ("checkout_token", "client_id", "total_price", "gaps")

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


# =====================================================================================
# The pixel half: a browser's checkout observation into the same frozen vocabulary
# =====================================================================================
def _pixel_text(value: Any) -> str | None:
    """One pixel join key as text the ledger can hold, or ``None``.

    Bounded by :data:`~merchant_svc.collector.MAX_PIXEL_FIELD_CHARS` rather than by
    :data:`MAX_IDENTIFIER_LENGTH`, and the difference is not an oversight. The 128-character
    bound belongs to the ledger's four TOP-LEVEL identifiers, which are interpolated into a
    ``Location`` header; these are payload values, and the collector has already refused
    anything over 512 characters at the door. Restating the collector's own published
    ceiling keeps one number, and re-checking it here keeps this function total over a
    record some caller built by hand rather than one ``accept_pixel_event`` produced.

    Dropped rather than truncated, for the reason ``_identifier`` gives: a truncated join key
    is not a shorter join key, it is a different one, and it joins to nothing or to somebody
    else's order while still reading as a complete observation.

    A container is ``None`` rather than its ``repr`` — the same rule :func:`_text` follows on
    the webhook half, and for the same reason: ``str(["PSX-A"])`` is a perfectly good-looking
    string that would become the join key ``['PSX-A']`` and match nothing. ``bool`` too:
    ``str(True)`` is ``"True"``, which is a join key that names nothing.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (Mapping, Sequence)) and not isinstance(value, str):
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_PIXEL_FIELD_CHARS:
        return None
    return text


def _pixel_number(value: Any) -> float | None:
    """One pixel money field as a finite float, or ``None``.

    ``accept_pixel_event`` already guarantees finiteness; this repeats the check because a
    non-finite float survives ``json.dumps`` as bare ``Infinity``, which is not JSON, and
    would poison ``reconciled_event``'s ``pixel_agrees`` comparison if it ever landed.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _pixel_gaps(value: Any) -> list[str]:
    """The named gaps, filtered to the collector's fixed vocabulary and de-duplicated.

    Filtered rather than forwarded: :data:`~merchant_svc.collector.GAP_KEYS` is four fixed
    strings this repository chose, so a projection that passes them through unchecked would
    publish whatever a hand-built observation put there — sender-chosen text on a public,
    un-evictable row, which is the one thing this whole projection exists to prevent.
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    named = {str(entry) for entry in value}
    return [key for key in GAP_KEYS if key in named]


def pixel_ledger_payload(observation: Any) -> dict[str, Any]:
    """The frozen ``checkout_pixel`` body for one accepted beacon.

    Built key by key from :data:`PUBLISHED_PIXEL_FIELDS`, which documents why each of the
    four is safe to make permanently public and why everything else is not.

    **``order_ref`` is the one absence worth arguing here**, because the beacon does carry it
    and the ring does keep it. ``trust.reconcile.engine`` unions every identifier an event
    names, so an event carrying a checkout token AND an order reference teaches the join that
    those two values name one order. The engine's own comments record what that cost when a
    *bridge* event was allowed to do it: two unrelated orders merged, "2 reconciled events
    before the code join, 1 after", and the vanished one's 10x overcharge was never graded.

    ``POST /pixel/collect`` takes no credential and answers a browser, so publishing
    ``order_ref`` off it would hand that merge to anyone who can reach the collector, at the
    cost of one request. Publishing only the checkout token bounds the worst case to
    attaching a bogus pixel to one existing group — and R4 keeps the pixel out of every
    comparison, so that is a false diagnostic rather than an erased reconciliation. The join
    loses nothing: the pixel and the webhook share the platform's own checkout token, which
    is the value they were always meant to meet on (D24).
    """
    return {
        "checkout_token": _pixel_text(getattr(observation, "checkout_token", None)),
        "client_id": _pixel_text(getattr(observation, "client_id", None)),
        "total_price": _pixel_number(getattr(observation, "total_price", None)),
        "gaps": _pixel_gaps(getattr(observation, "gaps", ())),
    }


def _pixel_body_digest(payload: Mapping[str, Any]) -> str:
    """A digest over the projected body — the pixel's stand-in for a signed delivery.

    ``ensure_ascii`` is left at its default, so the dumped text is pure ASCII and the encode
    below cannot raise on a lone surrogate — which a JSON body really can decode to, and
    which the collector really does accept as a join key.
    """
    canonical = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def pixel_ledger_event(observation: Any) -> dict[str, Any]:
    """One accepted beacon as the ``LedgerEvent`` body ``POST /events`` stores.

    **The id is derived from the projected body, not minted**, and that is what keeps a
    re-fired beacon from becoming a second un-evictable row. ``keepalive`` requests can be
    re-issued, a shopper can reload the thank-you page, and nothing between a browser and
    this route de-duplicates; a random id would put one purchase in an append-only chain
    twice. The digest is over the PROJECTION rather than over the request body — the webhook
    half digests the SIGNED bytes, which is stronger, and a beacon has no signed bytes to
    digest. Over the projection it still has the property that matters: an identical
    observation collapses to one row (answered ``200 Idempotent-Replay: true``), while a
    genuinely different one — the beacon that raced order creation, then the one that did
    not — is its own row rather than being silently dropped in favour of whichever arrived
    first.

    The digest is deliberately NOT also put in the payload. On the webhook half
    ``body_digest`` ties the row to bytes an auditor can independently re-hash; here it would
    be a hash of the very fields sitting beside it, which is decoration, and every extra key
    on this row is permanently public.

    **No ``store_id`` and no ``order_ref``**, both for the same reason: nothing trustworthy
    said. The beacon carries no shop domain, the route takes no credential, and
    ``reconcile`` already handles an unattributed pixel properly — it adopts the store only
    when exactly one store claims one of its keys, which is the honest answer and better than
    a guess this module would have to invent. For ``order_ref`` see
    :func:`pixel_ledger_payload`.
    """
    payload = pixel_ledger_payload(observation)
    received_at = getattr(observation, "received_at", None)
    ts = received_at.isoformat() if isinstance(received_at, datetime) else str(received_at or "")
    return {
        "event_id": ledger_event_id(
            {"kind": PIXEL_KIND, "body_digest": _pixel_body_digest(payload)}
        ),
        "ts": ts,
        "kind": PIXEL_KIND,
        "payload": payload,
    }


def publish_pixel_observation(observation: Any) -> bool:
    """Write one accepted web-pixel observation to the trust ledger. ``True`` when it landed.

    **Never raises, and here that is not a courtesy at all.** The caller is
    ``collector.routes.collect_pixel_event``, which answers a beacon fired from a shopper's
    browser during checkout on the merchant's own store. A 500 on that route is a failure on
    a real customer's purchase page, and it buys nothing — R4 already makes the ``orders/paid``
    webhook authoritative, so the correct behaviour when the audit write cannot happen is for
    the write to be lost visibly and the shopper to see nothing at all.

    :meth:`TrustLedgerPublisher.publish` supplies half of that guarantee; this supplies the
    other half, which that class cannot: the projection above reads an object built from a
    browser's JSON, and a shape no beacon was expected to produce must be a lost event with a
    reason attached rather than an exception on a checkout page.

    **A beacon with no checkout token is not published at all.**
    ``accept_pixel_event`` refuses one before it can get here, so this is belt to that
    braces — but an event with no token joins to nothing by construction, and an unjoinable
    row appended to an append-only public ledger from an unauthenticated route is pure
    permanent noise. Returning ``False`` without appending is the honest outcome, and it is
    distinguishable in ``ledger_status()`` from a delivery that was attempted and lost only
    in that it moves no counter — which is right, because nothing was lost.

    **The write is synchronous and inline**, one POST bounded by
    :data:`~proxyshop_support.trust_ledger.DEFAULT_LEDGER_TIMEOUT_SECONDS` (0.5s), holding
    the merchant's event loop for that long in the worst case — the same posture, through the
    same publisher, as the webhook half above. Handing it to the anyio threadpool was
    considered and rejected: it would let two beacons, or a beacon and a webhook, race
    :class:`TrustLedgerPublisher`'s unsynchronised ``delivered`` / ``lost`` / ``_delivering``
    counters, and trading the honesty of the outage signal for latency on the audit write is
    the wrong trade. The cost is bounded and written down instead.
    """
    try:
        event = pixel_ledger_event(observation)
    except Exception:  # noqa: BLE001 - see the docstring: this must not reach the route
        _log.exception(
            "a web-pixel observation could not be projected into a LedgerEvent; it is NOT in "
            "the chained ledger. The 204 stands and the observation is in the in-process ring"
        )
        return False
    if not event["payload"].get("checkout_token"):
        _log.warning(
            "a web-pixel observation carries no usable checkout_token, so it is not appended "
            "to the chained ledger: it could never be joined to an order and the ledger "
            "cannot forget a row"
        )
        return False
    return trust_publisher().publish(event)
