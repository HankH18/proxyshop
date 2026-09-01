"""Acceptance criterion 3, the truthful half: **webhooks are always delivered**.

Three topics, one delivery contract. What "always" means here, concretely:

* a registered subscriber receives the POST inside the call that triggers it — no polling,
  no sleep, no race;
* a receiver that rejects the delivery is **retried** until it accepts;
* every attempt is recorded whether or not it succeeded, so a test asserts on the stub's
  own delivery log rather than on the receiver, and a receiver bug cannot be mistaken for a
  stub bug;
* the signature covers the **raw bytes** on the wire.

And the negative control that gives the criterion its teeth: there is no knob anywhere that
makes a webhook not arrive. ``test_no_configuration_can_make_a_webhook_lossy`` asserts that
directly, because "webhooks always delivered" is only meaningful if it cannot be turned off.
"""

from __future__ import annotations

import json

from shopify_stub.testing import SEED_VARIANT, RecordingReceiver, StubClient
from shopify_stub.webhooks import (
    HEADER_API_VERSION,
    HEADER_EVENT_ID,
    HEADER_HMAC,
    HEADER_SHOP_DOMAIN,
    HEADER_TOPIC,
    HEADER_TRIGGERED_AT,
    HEADER_WEBHOOK_ID,
    MAX_ATTEMPTS,
    sign,
    verify,
)

VARIANT_ID = int(SEED_VARIANT["variant_id"])
Receiver = tuple[RecordingReceiver, str]


async def test_subscription_registers_and_orders_paid_is_delivered(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    receiver, url = webhook_receiver
    response = await stub.subscribe("ORDERS_PAID", url)
    payload = response.json()["data"]["webhookSubscriptionCreate"]
    assert payload["userErrors"] == []
    assert payload["webhookSubscription"]["uri"] == url
    assert payload["webhookSubscription"]["topic"] == "ORDERS_PAID"

    result = await stub.buy(VARIANT_ID)
    assert result["webhook_deliveries"][0]["delivered"] is True

    assert len(receiver.requests) == 1
    request = receiver.requests[0]
    body = json.loads(request["body"])
    assert body["checkout_token"] == result["checkout_token"]
    assert body["id"] == result["order_id"]
    assert body["financial_status"] == "paid"


async def test_delivery_headers_are_the_documented_ones(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    receiver, url = webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    await stub.buy(VARIANT_ID)

    headers = receiver.requests[0]["headers"]
    for name in (
        HEADER_TOPIC,
        HEADER_HMAC,
        HEADER_SHOP_DOMAIN,
        HEADER_API_VERSION,
        HEADER_WEBHOOK_ID,
        HEADER_EVENT_ID,
        HEADER_TRIGGERED_AT,
    ):
        assert name.lower() in headers, name
    assert headers[HEADER_TOPIC.lower()] == "orders/paid"
    assert headers[HEADER_SHOP_DOMAIN.lower()] == "proxyshop-demo.myshopify.com"
    assert headers[HEADER_TRIGGERED_AT.lower()].endswith("Z")


async def test_the_signature_covers_the_raw_bytes(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    """HMAC-SHA256 over the exact body bytes, base64, keyed by the client secret.

    The re-serialisation check at the end is the point of the test. Verifying a signature
    against ``json.dumps(json.loads(body))`` is the single most common webhook bug: it works
    until key order or separator spacing differs, and then it fails in production only.
    """
    receiver, url = webhook_receiver
    secret = "shpss_a_specific_secret_for_this_test"
    await stub.configure(webhook_secret=secret)
    await stub.subscribe("ORDERS_PAID", url)
    await stub.buy(VARIANT_ID)

    request = receiver.requests[0]
    raw = request["body"]
    signature = request["headers"][HEADER_HMAC.lower()]
    assert verify(raw, secret, signature)
    assert sign(raw, secret) == signature
    assert not verify(raw, "the-wrong-secret", signature)
    assert not verify(raw + b" ", secret, signature)

    re_serialised = json.dumps(json.loads(raw)).encode("utf-8")
    assert re_serialised != raw, (
        "this test is only meaningful while a re-serialised body differs from the raw one; "
        "if they are equal the raw-bytes requirement is untested here"
    )
    assert not verify(re_serialised, secret, signature)


async def test_a_rejected_delivery_is_retried_until_accepted(
    stub: StubClient, flaky_webhook_receiver: Receiver
) -> None:
    """Two 5xx responses, then a 200. The stub must keep going."""
    receiver, url = flaky_webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    result = await stub.buy(VARIANT_ID)

    (delivery,) = result["webhook_deliveries"]
    assert delivery["delivered"] is True
    assert delivery["attempts"] == 3
    assert len(receiver.requests) == 3
    bodies = {request["body"] for request in receiver.requests}
    assert len(bodies) == 1, "every retry must carry the identical body"


async def test_an_exhausted_retry_budget_is_recorded_as_undelivered(
    stub: StubClient, dead_webhook_receiver: Receiver
) -> None:
    """A permanently broken receiver must leave a loud, false ``delivered`` flag.

    Negative control for the delivery log: if the stub recorded ``delivered: true``
    unconditionally, every other test in this file would still pass.
    """
    receiver, url = dead_webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    result = await stub.buy(VARIANT_ID)

    (delivery,) = result["webhook_deliveries"]
    assert delivery["delivered"] is False
    assert delivery["attempts"] == MAX_ATTEMPTS
    assert delivery["status_code"] == 500
    assert delivery["error"] == "HTTP 500"
    assert len(receiver.requests) == MAX_ATTEMPTS


async def test_no_configuration_can_make_a_webhook_lossy(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    """The asymmetry, asserted directly: the pixel has a loss knob, the webhook has none.

    Turning the pixel off entirely and setting a 100% drop rate must change nothing about
    webhook delivery. If a future change added a webhook drop rate, this fails.
    """
    receiver, url = webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    await stub.configure(pixel_mode="off", pixel_drop_rate=1.0)

    for _ in range(5):
        result = await stub.buy(VARIANT_ID)
        assert result["pixel_event_emitted"] is False
        assert result["webhook_deliveries"][0]["delivered"] is True
    assert len(receiver.requests) == 5

    rejected = await stub.configure(webhook_drop_rate=0.5)
    assert rejected.status_code == 400, (
        "there must be no webhook loss knob; an unknown config key is a 400"
    )


async def test_orders_fulfilled_is_delivered_with_a_fulfilment(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    receiver, url = webhook_receiver
    await stub.subscribe("ORDERS_FULFILLED", url)
    order = await stub.buy(VARIANT_ID)
    assert receiver.requests == [], "orders/paid must not reach an orders/fulfilled subscriber"

    response = await stub.fulfil(
        order["order_id"], tracking_company="USPS", tracking_number="1Z1234512345123456"
    )
    assert response.status_code == 200
    assert response.json()["fulfillment_status"] == "fulfilled"

    (request,) = receiver.requests
    assert request["headers"][HEADER_TOPIC.lower()] == "orders/fulfilled"
    body = json.loads(request["body"])
    assert body["fulfillment_status"] == "fulfilled"
    (fulfillment,) = body["fulfillments"]
    assert set(fulfillment) == {
        "created_at",
        "id",
        "order_id",
        "status",
        "tracking_company",
        "tracking_number",
        "updated_at",
    }
    assert fulfillment["tracking_number"] == "1Z1234512345123456"


async def test_refunds_create_is_delivered_and_moves_the_financial_status(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    receiver, url = webhook_receiver
    await stub.subscribe("REFUNDS_CREATE", url)
    order = await stub.buy(VARIANT_ID)

    partial = await stub.refund(order["order_id"], amount="40.00", note="damaged")
    assert partial.status_code == 201
    assert partial.json()["financial_status"] == "partially_refunded"

    body = json.loads(receiver.requests[0]["body"])
    assert body["order_id"] == order["order_id"]
    assert body["note"] == "damaged"
    assert body["transactions"][0]["amount"] == "40.00"
    assert body["transactions"][0]["kind"] == "refund"

    rest = await stub.refund(order["order_id"])
    assert rest.json()["amount"] == "60.00"
    assert rest.json()["financial_status"] == "refunded"


async def test_a_refund_beyond_the_remaining_total_is_refused(stub: StubClient) -> None:
    """Silently clamping an over-refund would let a consumer's arithmetic bug pass."""
    order = await stub.buy(VARIANT_ID)
    response = await stub.refund(order["order_id"], amount="1000.00")
    assert response.status_code == 409
    assert "exceeds" in response.json()["errors"]


async def test_topics_are_isolated(stub: StubClient) -> None:
    """A subscriber to one topic must never receive another's payload."""
    paid = RecordingReceiver()
    refunds = RecordingReceiver()
    from proxyshop_support.asgi_server import serve

    with serve(paid) as paid_url, serve(refunds) as refunds_url:
        await stub.subscribe("ORDERS_PAID", f"{paid_url}/hook")
        await stub.subscribe("REFUNDS_CREATE", f"{refunds_url}/hook")
        order = await stub.buy(VARIANT_ID)
        assert len(paid.requests) == 1
        assert refunds.requests == []

        await stub.refund(order["order_id"])
        assert len(paid.requests) == 1
        assert len(refunds.requests) == 1


async def test_an_unsupported_topic_is_refused(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    """Only the three topics. Subscribing ``checkouts/*`` would be a second observation path.

    C5 permits exactly one checkout-observation path (the web pixel), so the stub refuses to
    register a topic that would create another — a refusal that has to live here, because
    nothing downstream can un-observe a checkout it was told about.
    """
    _, url = webhook_receiver
    response = await stub.subscribe("CHECKOUTS_CREATE", url)
    payload = response.json()["data"]["webhookSubscriptionCreate"]
    assert payload["webhookSubscription"] is None
    (error,) = payload["userErrors"]
    assert error["field"] == ["topic"]
    assert set(error) == {"field", "message"}, (
        "WebhookSubscriptionInput's UserError has no `code` member, unlike the web pixel's"
    )


async def test_a_duplicate_subscription_is_refused(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    """Two subscriptions on one (topic, address) would double-deliver every event."""
    _, url = webhook_receiver
    assert (await stub.subscribe("ORDERS_PAID", url)).json()["data"]["webhookSubscriptionCreate"][
        "userErrors"
    ] == []
    second = await stub.subscribe("ORDERS_PAID", url)
    (error,) = second.json()["data"]["webhookSubscriptionCreate"]["userErrors"]
    assert "already been taken" in error["message"]


async def test_a_blank_uri_is_refused(stub: StubClient) -> None:
    response = await stub.subscribe("ORDERS_PAID", "")
    (error,) = response.json()["data"]["webhookSubscriptionCreate"]["userErrors"]
    assert error["field"] == ["webhookSubscription", "uri"]


async def test_no_subscriber_means_no_delivery_and_an_empty_log(stub: StubClient) -> None:
    """An empty delivery list is a *configuration* fact, never a silent success."""
    result = await stub.buy(VARIANT_ID)
    assert result["webhook_deliveries"] == []
    assert await stub.deliveries() == []


async def test_no_customer_pii_is_ever_delivered(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    """C5: the app holds no protected-customer-data scopes, so the stub emits no PII.

    Asserted over the raw body text rather than over parsed keys, so a PII field nested
    anywhere — inside a line item, a fulfillment, a note attribute — is caught too.
    """
    receiver, url = webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    await stub.buy(VARIANT_ID)

    raw = receiver.requests[0]["body"].decode("utf-8")
    for forbidden in (
        "email",
        "phone",
        "first_name",
        "last_name",
        "address",
        "customer_id",
        "customerId",
    ):
        assert forbidden not in raw, f"{forbidden} must never appear in a stub payload"
    assert json.loads(raw)["customer"] is None
