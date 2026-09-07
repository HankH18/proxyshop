"""S1 link 7a: a signed order webhook reaches E6's chained ledger, or is visibly lost.

R4 makes the order webhook the AUTHORITATIVE half of the pixel↔webhook reconciliation. Before
this suite existed the merchant verified the delivery, de-duplicated it, built a ledger-shaped
record from it and appended that record to a module-level ring that drains nowhere. Measured
across four processes on loopback — a shopify-stub, the merchant service, a real
``apps/trust`` — with nothing stubbed and every hop over HTTP::

    stub  POST /_stub/checkouts/{token}/complete  -> 201, one orders/paid delivery, accepted
    merchant  POST /webhooks/shopify/orders/paid  -> 200 {"status":"recorded"}
    trust     GET  /events                        -> {"events":[],"count":0}
    merchant  HANDOFF ring                        -> ['gid://shopify/Order/5500000000001']

Every test here drives that same real chain rather than calling the sink directly, because
"the sink writes to the ledger" was never the doubtful part — "the deployed service reaches
the sink, and the sink reaches another deployable" is. The one exception is the totality
property at the bottom, which has to feed the projection bodies no Shopify would send.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from contracts.ledger import validate_ledger_payload
from merchant_svc import composition
from merchant_svc.install import webhooks
from merchant_svc.install.config import WEBHOOK_PATH_PREFIX
from merchant_svc.install.webhooks import WebhookInbox, sign
from shopify_stub.state import DEFAULT_SHOP_DOMAIN
from shopify_stub.testing import SEED_VARIANT, StubClient

#: A fixed receipt instant for the records built by hand below. Nothing here reads a clock.
_INSTANT = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)


async def _subscribe_all(stub: StubClient, merchant_url: str) -> None:
    """Register the three C5 topics with the stub, pointed at the served merchant routes."""
    for topic in ("ORDERS_PAID", "ORDERS_FULFILLED", "REFUNDS_CREATE"):
        path = topic.lower().replace("_", "/")
        response = await stub.subscribe(topic, f"{merchant_url}{WEBHOOK_PATH_PREFIX}/{path}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["webhookSubscriptionCreate"]["userErrors"] == []


async def _chain(trust_url: str) -> list[dict[str, Any]]:
    """Everything ``apps/trust`` holds, read back off its own served route."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{trust_url}/events")
    assert response.status_code == 200, response.text
    events: list[dict[str, Any]] = response.json()["events"]
    return events


async def _verify(trust_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{trust_url}/events/verify")
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


@pytest.fixture
def ledger_handoff() -> Any:
    """The process-wide hand-off ring, emptied around the test."""
    webhooks.HANDOFF.clear()
    yield webhooks.HANDOFF
    webhooks.HANDOFF.clear()


# ======================================================================================
# The gate: a real purchase's webhook is in another deployable's chained ledger
# ======================================================================================
async def test_a_completed_checkout_puts_its_order_paid_webhook_into_the_trust_ledger(
    install_env: dict[str, str],
    install_app_url: str,
    install_inbox: WebhookInbox,
    install_stub: StubClient,
    ledger_trust_wired: str,
    ledger_handoff: Any,
) -> None:
    """The whole of link 7a, over HTTP, with nothing in the chain replaced by a double.

    A shopper completes a checkout at the stub; the stub signs an ``orders/paid`` body with
    the app's client secret and POSTs it to the merchant's served route; the merchant
    authenticates it, records it, and writes it to a real trust service; and the proof is
    that trust's OWN ``GET /events`` and ``GET /events/verify`` hold the row and still
    verify.

    Rejected here and only here: nothing — this is the positive path. What it pins is that
    the authoritative half of R4 leaves the merchant process at all.
    """
    await _subscribe_all(install_stub, install_app_url)
    completed = await install_stub.buy(SEED_VARIANT["variant_id"])

    events = await _chain(ledger_trust_wired)
    assert [event["kind"] for event in events] == ["order_paid"], (
        f"the order webhook never reached the trust ledger: {events}"
    )

    event = events[0]
    assert event["store_id"] == DEFAULT_SHOP_DOMAIN
    assert event["order_ref"] == f"gid://shopify/Order/{completed['order_id']}"
    assert event["payload"]["checkout_token"] == completed["checkout_token"]
    assert event["payload"]["total_price"] == completed["total_price"]
    assert event["payload"]["client_id"] == completed["client_id"], (
        "D24's client_id join key is only on the webhook in a note attribute; without it "
        "the pixel and the webhook can join on a token and never on the client"
    )
    assert validate_ledger_payload("order_paid", event["payload"]) == []

    verified = await _verify(ledger_trust_wired)
    assert verified["ok"] is True, verified
    assert verified["length"] == 1, verified

    # And the in-process readback other tests already depend on still holds the record.
    assert [record["order_ref"] for record in ledger_handoff.records()] == [event["order_ref"]]


async def test_the_fulfilment_and_refund_webhooks_land_as_their_own_frozen_kinds(
    install_env: dict[str, str],
    install_app_url: str,
    install_inbox: WebhookInbox,
    install_stub: StubClient,
    ledger_trust_wired: str,
    ledger_handoff: Any,
) -> None:
    """All three C5 topics, each translated into the kind ``contracts.ledger`` freezes.

    ``LEDGER_KIND_FOR_TOPIC`` maps the vendor's three topic names onto ``order_paid``,
    ``order_fulfilled`` and ``refund``, and every one of those is a published payload shape.
    A translation that only worked for the topic a demo happened to drive would leave two
    thirds of the order lifecycle out of the audit trail.

    Rejected here and only here: nothing. What it pins is that the chain holds one row per
    real event, in arrival order, each satisfying its own published shape.
    """
    await _subscribe_all(install_stub, install_app_url)
    completed = await install_stub.buy(SEED_VARIANT["variant_id"])
    order_id = completed["order_id"]

    fulfilled = await install_stub.fulfil(order_id, tracking_number="TRK-1")
    assert fulfilled.status_code == 200, fulfilled.text
    refunded = await install_stub.refund(order_id, amount="10.00", note="damaged in transit")
    assert refunded.status_code == 201, refunded.text

    events = await _chain(ledger_trust_wired)
    assert [event["kind"] for event in events] == ["order_paid", "order_fulfilled", "refund"]

    by_kind = {event["kind"]: event for event in events}
    order_ref = f"gid://shopify/Order/{order_id}"
    assert {event["order_ref"] for event in events} == {order_ref}
    assert {event["store_id"] for event in events} == {DEFAULT_SHOP_DOMAIN}

    assert by_kind["order_fulfilled"]["payload"]["fulfilled_at"], by_kind["order_fulfilled"]
    assert by_kind["refund"]["payload"]["amount"] == "10.00"
    assert by_kind["refund"]["payload"]["reason"] == "damaged in transit"
    for kind, event in by_kind.items():
        assert validate_ledger_payload(kind, event["payload"]) == [], (kind, event["payload"])

    verified = await _verify(ledger_trust_wired)
    assert verified["ok"] is True and verified["length"] == 3, verified


# ======================================================================================
# The failure path: a trust service that is down must not become a retry storm
# ======================================================================================
async def test_a_trust_service_that_is_down_leaves_the_delivery_accepted_and_the_loss_readable(
    install_env: dict[str, str],
    install_app_url: str,
    install_inbox: WebhookInbox,
    install_stub: StubClient,
    ledger_trust_down: str,
    ledger_handoff: Any,
) -> None:
    """The property that makes publishing from the boot sink safe at all.

    ``handle_delivery`` answers a sink that raises with **500**, and Shopify retries a
    non-2xx for up to 48 hours. So a ledger write that could propagate its failure would
    turn one trust outage into every order webhook being retried forever — against a
    delivery the merchant has already authenticated and already recorded.

    Rejected here and only here: the *silence*. The delivery is accepted (the stub's own
    delivery log says one attempt, 200) and the loss is counted, attributed and readable
    afterwards through ``composition.ledger_status()``.
    """
    await _subscribe_all(install_stub, install_app_url)
    completed = await install_stub.buy(SEED_VARIANT["variant_id"])

    deliveries = await install_stub.deliveries()
    assert [(row["topic"], row["status_code"], row["attempts"]) for row in deliveries] == [
        ("orders/paid", 200, 1)
    ], f"the sender was not answered 2xx first time, so it will retry: {deliveries}"

    status = composition.ledger_status()
    assert status["delivering"] is False, status
    assert status["delivered"] == 0 and status["lost"] == 1, status
    assert status["url"] == f"{ledger_trust_down}/events", status
    assert status["last_failure"], "a lost ledger write with no reason is a silent one"

    # The delivery itself stands: recorded in the inbox, readable in the hand-off ring.
    assert [record["order_ref"] for record in ledger_handoff.records()] == [
        f"gid://shopify/Order/{completed['order_id']}"
    ]
    assert [event.topic for event in install_inbox.events()] == ["orders/paid"]


async def test_the_events_lost_to_an_outage_are_named_not_merely_counted(
    install_env: dict[str, str],
    install_app_url: str,
    install_inbox: WebhookInbox,
    install_stub: StubClient,
    ledger_trust_down: str,
    ledger_handoff: Any,
) -> None:
    """An operator has to be able to say WHICH orders are missing from the chain.

    ``lost: 3`` tells nobody what to re-drive. The shared publisher keeps ``(event_id,
    reason)`` on a bounded ring, and because the id is derived from the signed body's digest
    it names a specific delivery rather than a random number.

    Rejected here and only here: an undelivered ring holding a payload. Ids and reasons
    only — the same rule the trust service's own anomaly records follow.
    """
    await _subscribe_all(install_stub, install_app_url)
    completed = await install_stub.buy(SEED_VARIANT["variant_id"])
    await install_stub.fulfil(completed["order_id"])

    publisher = composition.trust_publisher()
    undelivered = list(publisher.undelivered)
    # The id of every record the ring holds, derived the way the publisher derived it: from
    # the digest of the bytes the stub actually signed, so it names a delivery on disk.
    expected = [
        f"{composition.EVENT_ID_PREFIX}-{record['kind']}-{record['body_digest']}"
        for record in ledger_handoff.records()
    ]
    assert [event_id for event_id, _ in undelivered] == expected, undelivered
    assert len(expected) == 2, "the paid and fulfilled deliveries should both be here"
    assert all(reason for _, reason in undelivered), undelivered
    serialised = json.dumps(undelivered)
    assert completed["checkout_token"] not in serialised, (
        f"the undelivered ring carried payload material: {serialised}"
    )


# ======================================================================================
# Once-only: the id is derived from the signed body, so a restart cannot double-write
# ======================================================================================
async def test_a_delivery_replayed_after_a_restart_is_one_row_in_the_chain(
    install_env: dict[str, str],
    install_app_url: str,
    install_inbox: WebhookInbox,
    install_stub: StubClient,
    ledger_trust_wired: str,
    ledger_handoff: Any,
) -> None:
    """``WebhookInbox`` de-duplicates within one process; the ledger has to survive a restart.

    Shopify retries anything it did not see a 2xx for, and a merchant that restarts
    mid-outage has an empty inbox — so the retry reaches the sink as a fresh delivery. A
    random ``event_id`` would make that a second row for one purchase, in a ledger whose
    ``BEFORE UPDATE OR DELETE ... ENABLE ALWAYS`` trigger means it can never be removed.

    This test empties the inbox between the two deliveries, which is exactly what a restart
    does to it, and re-presents the byte-identical signed body the stub already sent.

    Rejected here and only here: a second row. Still admitted: the retry itself, which is
    answered 200 by the merchant so the sender stops.
    """
    await _subscribe_all(install_stub, install_app_url)
    await install_stub.buy(SEED_VARIANT["variant_id"])

    first = await _chain(ledger_trust_wired)
    assert len(first) == 1, first
    record = ledger_handoff.records()[0]
    body = json.dumps(record["payload"], separators=(",", ":")).encode("utf-8")

    # The process forgets everything it knew about this delivery — a restart, in one line.
    install_inbox.clear()
    ledger_handoff.clear()

    async with httpx.AsyncClient() as client:
        retry = await client.post(
            f"{install_app_url}{WEBHOOK_PATH_PREFIX}/orders/paid",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Topic": "orders/paid",
                "X-Shopify-Hmac-Sha256": sign(body, install_env["SHOPIFY_API_SECRET"]),
                "X-Shopify-Shop-Domain": DEFAULT_SHOP_DOMAIN,
                "X-Shopify-Webhook-Id": "w-after-restart",
            },
        )
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "recorded", retry.text
    assert ledger_handoff.records(), "the retry was not handed to the sink after the restart"

    after = await _chain(ledger_trust_wired)
    assert [event["event_id"] for event in after] == [first[0]["event_id"]], (
        f"a retried delivery minted a second, permanent row: {after}"
    )
    assert composition.trust_publisher().delivered == 1, (
        "a 200 idempotent replay is once-only landing working, not a delivery"
    )


def test_a_refund_names_the_order_it_refunded_and_not_itself() -> None:
    """``admin_graphql_api_id`` is the id of the resource the BODY describes.

    On ``orders/paid`` that is the order. On ``refunds/create`` it is the refund, and
    reading it as an order reference filed the one event that says money went back to a
    buyer under ``gid://shopify/Refund/…`` — a key no order, no pixel and no offer shares.
    ``trust.reconcile.engine`` groups a store's events by checkout token, order reference
    and order id; a refund body carries no checkout token, so that event reconciled against
    nothing at all while looking perfectly well-formed.

    Rejected here and only here: a refund's own id standing in for an order's. Still
    admitted: an order body's ``admin_graphql_api_id``, which IS the order.
    """
    refund_body = {
        "id": 8800000000001,
        "admin_graphql_api_id": "gid://shopify/Refund/8800000000001",
        "order_id": 5500000000001,
        "note": "damaged",
    }
    received = webhooks.ReceivedWebhook(
        topic="refunds/create",
        shop_domain=DEFAULT_SHOP_DOMAIN,
        webhook_id="w-1",
        payload=refund_body,
        received_at=_INSTANT,
    )
    assert received.order_ref == "gid://shopify/Order/5500000000001"

    order_body = {"id": 5500000000001, "admin_graphql_api_id": "gid://shopify/Order/5500000000001"}
    paid = webhooks.ReceivedWebhook(
        topic="orders/paid",
        shop_domain=DEFAULT_SHOP_DOMAIN,
        webhook_id="w-2",
        payload=order_body,
        received_at=_INSTANT,
    )
    assert paid.order_ref == "gid://shopify/Order/5500000000001"

    # Shopify's own spelling on a child resource outranks both.
    explicit = webhooks.ReceivedWebhook(
        topic="refunds/create",
        shop_domain=DEFAULT_SHOP_DOMAIN,
        webhook_id="w-3",
        payload={**refund_body, "admin_graphql_api_order_id": "gid://shopify/Order/77"},
        received_at=_INSTANT,
    )
    assert explicit.order_ref == "gid://shopify/Order/77"


# ======================================================================================
# Totality: the boot sink must never raise, whatever a body holds
# ======================================================================================
def test_an_unverifiable_shop_domain_header_never_reaches_the_ledger_as_a_store(
    ledger_trust_down: str,
) -> None:
    """``store_id`` comes from an UNSIGNED header, and the ledger it lands in is permanent.

    ``X-Shopify-Shop-Domain`` is chosen freely by whoever presents a signed body — the same
    reason ``WebhookInbox`` refuses to key on it. Writing it through unchecked would let one
    captured delivery file rows under any store name, forever, in a store the reconciler
    scopes its joins by.

    Rejected here and only here: a shop claim that is not a bare ``.myshopify.com`` host.
    Still admitted: the event itself — it lands unscoped rather than being dropped, because
    losing an authenticated purchase is worse than losing the name of the shop it came from.
    """
    honest = composition.ledger_event(
        {
            "kind": "order_paid",
            "store_id": DEFAULT_SHOP_DOMAIN,
            "order_ref": "gid://shopify/Order/1",
            "received_at": "2026-09-06T00:00:00+00:00",
            "body_digest": "a" * 64,
            "payload": {"checkout_token": "tok", "total_price": "10.00"},
        }
    )
    assert honest["store_id"] == DEFAULT_SHOP_DOMAIN

    for claim in (
        "evil.example.com",
        "https://shop.myshopify.com",
        "shop.myshopify.com\r\nX-Injected: 1",
        "s" * 200 + ".myshopify.com",
        "",
    ):
        event = composition.ledger_event(
            {
                "kind": "order_paid",
                "store_id": claim,
                "order_ref": "gid://shopify/Order/2",
                "received_at": "2026-09-06T00:00:00+00:00",
                "body_digest": "b" * 64,
                "payload": {"checkout_token": "tok", "total_price": "10.00"},
            }
        )
        assert "store_id" not in event, (claim, event)
        assert event["order_ref"] == "gid://shopify/Order/2", claim


def test_publishing_never_raises_whatever_the_record_holds(ledger_trust_down: str) -> None:
    """A totality property, and the one that keeps a projection bug out of the retry loop.

    ``default_sink`` is answered 500 by ``handle_delivery`` if it raises, and 500 is what
    makes Shopify retry. The projection reads a third party's body, so "no sample
    anticipated this shape" has to be a lost event with a reason, never an exception on the
    request path.

    Rejected here and only here: nothing. What it pins is that no input reaches an
    exception, and the positive control underneath keeps "never raises" from being satisfied
    by refusing everything.
    """
    hostile: list[dict[str, Any]] = [
        {},
        {"kind": "order_paid"},
        {"kind": "order_paid", "received_at": "not-a-timestamp", "payload": None},
        {"kind": "refund", "payload": {"transactions": "not-a-list", "note": {"a": 1}}},
        {"kind": "refund", "payload": {"transactions": [{"amount": "NaN"}]}},
        {"kind": "order_fulfilled", "payload": {"fulfillments": [{"created_at": 5}]}},
        {"kind": "order_paid", "payload": {"discount_codes": [1, None, {"code": "x" * 900}]}},
        {"kind": "order_paid", "payload": {"note_attributes": "nope"}},
        {"kind": "unheard-of", "order_ref": object(), "payload": {}},
        {"kind": "order_paid", "body_digest": "\x00\r\n", "payload": {}},
    ]
    for record in hostile:
        assert composition.publish_ledger_record(record) is False, record

    # Positive control: a well-formed record still reaches the transport and is counted as
    # lost by the publisher (this fixture points at a closed port), not refused before it.
    before = composition.trust_publisher().lost
    assert (
        composition.publish_ledger_record(
            {
                "kind": "order_paid",
                "store_id": DEFAULT_SHOP_DOMAIN,
                "order_ref": "gid://shopify/Order/9",
                "received_at": "2026-09-06T00:00:00+00:00",
                "body_digest": "c" * 64,
                "payload": {"checkout_token": "tok", "total_price": "10.00"},
            }
        )
        is False
    )
    assert composition.trust_publisher().lost == before + 1


def test_the_boot_sink_is_the_one_that_publishes(ledger_trust_down: str) -> None:
    """``main.create_app`` is frozen, so the ledger write has to be the BOOT default.

    Had it been installed by an explicit ``set_webhook_sink`` call, the production path
    would have been the one path that never made it — which is precisely the bug this
    module's own ``_sink`` comment records, one layer up.

    Rejected here and only here: a boot state that does not publish. ``set_webhook_sink
    (None)`` is checked in the same breath, because a ``finally`` that restores ``None`` is
    how a process unwires itself by accident (T-247).
    """
    assert webhooks.webhook_sink() is webhooks.default_sink
    borrowed = webhooks.webhook_sink()
    try:
        webhooks.set_webhook_sink(webhooks.inbox_only_sink)
        webhooks.set_webhook_sink(None)
        assert webhooks.webhook_sink() is webhooks.default_sink
    finally:
        webhooks.set_webhook_sink(borrowed)

    source = webhooks.default_sink.__doc__ or ""
    assert "ledger" in source.lower(), "the boot sink no longer documents where it writes"


def test_status_answers_before_anything_has_published(
    monkeypatch: pytest.MonkeyPatch, ledger_closed_port_url: str
) -> None:
    """A health question must not be the thing that decides which trust service this is.

    ``ledger_status()`` on a process that has published nothing reports the configured
    address and ``delivering: None`` — "no evidence either way" — rather than building a
    publisher as a side effect of being asked.

    Rejected here and only here: inventing counters. Still admitted: reporting the address,
    which is the half an operator needs before the first webhook arrives.
    """
    from proxyshop_support.trust_ledger import ENV_TRUST_URL

    monkeypatch.setenv(ENV_TRUST_URL, ledger_closed_port_url)
    composition.set_trust_publisher(None)
    try:
        status = composition.ledger_status()
        assert status == {
            "url": f"{ledger_closed_port_url}/events",
            "source": "environment",
            "delivering": None,
            "delivered": 0,
            "lost": 0,
            "last_failure": None,
        }
    finally:
        composition.set_trust_publisher(None)
