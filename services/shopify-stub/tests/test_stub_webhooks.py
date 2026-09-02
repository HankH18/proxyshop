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
import uuid
from datetime import UTC, datetime

from shopify_stub.testing import (
    SEED_VARIANT,
    RecordingReceiver,
    StubClient,
    TruncatingReceiver,
)
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


async def test_two_subscribers_on_one_topic_both_receive_the_same_body(
    stub: StubClient,
) -> None:
    """Fan-out on a single topic — the case ``test_topics_are_isolated`` cannot see.

    That test uses two *different* topics, so it passes unchanged against a
    ``subscriptions_for`` that keys by topic and therefore returns at most one subscriber.
    Sabotaging it exactly that way dropped a subscriber in silence: ``deliveries reported:
    1``, receiver A got 0, receiver B got 1, suite fully green.

    ``graphql_admin`` deliberately allows two URLs on one topic — it refuses only a duplicate
    ``(topic, address)`` pair, which is Shopify's own rule — so fan-out is a supported
    configuration with nothing asserting it worked. That is the whole defect: production
    behaviour here is already correct, and it was correct by luck rather than by test.
    """
    first = RecordingReceiver()
    second = RecordingReceiver()
    from proxyshop_support.asgi_server import serve

    with serve(first) as first_url, serve(second) as second_url:
        await stub.subscribe("ORDERS_PAID", f"{first_url}/hook")
        await stub.subscribe("ORDERS_PAID", f"{second_url}/hook")
        await stub.buy(VARIANT_ID)

        assert len(first.requests) == 1, "the first subscriber must not be dropped"
        assert len(second.requests) == 1, "the second subscriber must not be dropped"
        assert first.requests[0]["body"] == second.requests[0]["body"], (
            "both subscribers see the same event; a per-subscriber body would mean the "
            "signature covers different bytes for each"
        )
        assert len(await stub.deliveries()) == 2, (
            "the delivery log must count both, not report one delivery for two subscribers"
        )


# ---------------------------------------------------------------------------------------
# T-105: the header block asserted by VALUE
# ---------------------------------------------------------------------------------------


async def test_every_delivery_header_carries_its_real_value(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    """Every header, against the value it is supposed to be derived from.

    ``test_delivery_headers_are_the_documented_ones`` above checks that seven header *names*
    are present and pins three of the values. That leaves ``X-Shopify-API-Version``,
    ``X-Shopify-Webhook-Id`` and ``X-Shopify-Event-Id`` asserted only as "some string is
    here" — and measured on this suite, replacing all three with the literal ``"CONSTANT"``
    in ``delivery_headers`` shipped **297 passed**. A header whose value is never read is a
    header a consumer cannot rely on, and the two ids are how a consumer deduplicates a
    retried delivery.

    So each value is compared to its source: the api version and the shop domain to the
    *reconfigured* config (a default would prove nothing), the ids to the id the stub itself
    recorded in its delivery log, and the signature to a recomputation over the raw bytes.
    """
    receiver, url = webhook_receiver
    version = "2025-01"
    domain = "store-headers.example.com"
    secret = "shpss_headers_specific_secret_0001"
    # Subscribe *before* moving the api version: the Admin GraphQL route answers only on the
    # configured version, and `StubClient.subscribe` addresses the default one.
    await stub.subscribe("ORDERS_PAID", url)
    assert (await stub.configure(api_version=version)).status_code == 200
    assert (await stub.configure(shop_domain=domain)).status_code == 200
    assert (await stub.configure(webhook_secret=secret)).status_code == 200

    await stub.buy(VARIANT_ID)

    (delivery,) = await stub.deliveries()
    request = receiver.requests[0]
    headers = request["headers"]

    assert headers["content-type"] == "application/json"
    assert headers[HEADER_TOPIC.lower()] == "orders/paid"
    # Read from the config, not from a constant: both were just changed away from default.
    assert headers[HEADER_API_VERSION.lower()] == version
    assert headers[HEADER_SHOP_DOMAIN.lower()] == domain
    # The ids are the stub's own recorded id, and the two spellings agree.
    assert headers[HEADER_WEBHOOK_ID.lower()] == delivery["webhook_id"]
    assert headers[HEADER_EVENT_ID.lower()] == delivery["webhook_id"]
    assert uuid.UUID(headers[HEADER_WEBHOOK_ID.lower()]).version == 4
    # The signature is the HMAC of these exact bytes under the configured secret.
    assert headers[HEADER_HMAC.lower()] == sign(request["body"], secret)
    assert headers[HEADER_HMAC.lower()] == delivery["hmac"]
    # And the trigger time is a real instant, not a placeholder.
    triggered = headers[HEADER_TRIGGERED_AT.lower()]
    assert triggered.endswith("Z")
    parsed = datetime.fromisoformat(triggered.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 120


async def test_the_webhook_id_is_fresh_for_every_delivery(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    """Two deliveries, two ids. A constant would satisfy every per-delivery check above.

    This is the assertion a hard-coded id cannot pass however plausible the constant looks,
    and it is the property a consumer's deduplication actually depends on: the same id twice
    means "this is a retry of one event", so a shared id would make two real orders collapse
    into one.
    """
    receiver, url = webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    await stub.buy(VARIANT_ID)
    await stub.buy(VARIANT_ID)

    first, second = await stub.deliveries()
    assert first["webhook_id"] != second["webhook_id"]
    sent = [request["headers"][HEADER_WEBHOOK_ID.lower()] for request in receiver.requests]
    assert sent == [first["webhook_id"], second["webhook_id"]]
    assert len(set(sent)) == 2, "a constant webhook id makes two orders look like one retry"


async def test_a_retried_delivery_keeps_one_id_and_one_signature(
    stub: StubClient, flaky_webhook_receiver: Receiver
) -> None:
    """The other half of the id contract: a *retry* must reuse the id, not mint a new one.

    ``test_the_webhook_id_is_fresh_for_every_delivery`` pins "different events differ"; this
    pins "the same event, delivered three times, is one event". Together they say the id
    identifies the event rather than the attempt — which is exactly what makes it usable for
    deduplication.
    """
    receiver, url = flaky_webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    await stub.buy(VARIANT_ID)

    (delivery,) = await stub.deliveries()
    assert delivery["attempts"] == 3
    assert delivery["delivered"] is True
    assert len(receiver.requests) == 3
    ids = {request["headers"][HEADER_WEBHOOK_ID.lower()] for request in receiver.requests}
    signatures = {request["headers"][HEADER_HMAC.lower()] for request in receiver.requests}
    assert ids == {delivery["webhook_id"]}
    assert signatures == {delivery["hmac"]}


async def test_the_topic_header_names_the_topic_that_fired(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    """``orders/paid``, ``orders/fulfilled`` and ``refunds/create``, each by value.

    The existing coverage asserts the first two on separate deliveries and never the third.
    All three subscribe to one receiver here so the header is what distinguishes them.
    """
    receiver, url = webhook_receiver
    for topic in ("ORDERS_PAID", "ORDERS_FULFILLED", "REFUNDS_CREATE"):
        await stub.subscribe(topic, url)

    result = await stub.buy(VARIANT_ID)
    order_id = result["order_id"]
    assert (await stub.fulfil(order_id)).status_code == 200
    assert (await stub.refund(order_id, amount="10.00")).status_code == 201

    topics = [request["headers"][HEADER_TOPIC.lower()] for request in receiver.requests]
    assert topics == ["orders/paid", "orders/fulfilled", "refunds/create"]


async def test_the_delivery_log_holds_one_row_per_delivery_not_per_attempt(
    stub: StubClient, flaky_webhook_receiver: Receiver
) -> None:
    """The shape of the log, which `WebhookDelivery`'s docstring used to describe wrongly.

    It said "the record of one delivery attempt … every attempt lands here", which reads as
    a row per attempt. `WebhookDispatcher.dispatch` retries *inside* one record and appends
    it once, after the loop, so three attempts are one row with `attempts == 3` — and
    `status_code`/`error` hold the LAST attempt's outcome, not the first failure's.

    Asserted against `/_stub/webhooks/deliveries` (the whole log) rather than the
    completion response, because the completion response only ever carries the deliveries
    from its own dispatch and could not see a per-attempt row anyway.
    """
    receiver, url = flaky_webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    await stub.buy(VARIANT_ID)

    assert len(receiver.requests) == 3, "three POSTs really were made — 500, 503, then 200"

    log = await stub.deliveries()
    assert len(log) == 1, f"one row per delivery, not one per attempt; got {len(log)}"
    (row,) = log
    assert row["attempts"] == 3, "the count is what carries the retries"
    assert row["delivered"] is True
    assert row["status_code"] == 200, "the row holds the LAST attempt's status, not the 500"
    assert row["error"] is None, "and the last attempt succeeded, so no error survives"


async def test_a_second_subscriber_is_a_second_row(
    stub: StubClient, webhook_receiver: Receiver, flaky_webhook_receiver: Receiver
) -> None:
    """The other half of "one row per (dispatch, subscription)".

    Without this, `len(log) == 1` above is equally consistent with "the log only ever holds
    one row", which would make that assertion prove nothing.
    """
    good_receiver, good_url = webhook_receiver
    flaky_receiver, flaky_url = flaky_webhook_receiver
    await stub.subscribe("ORDERS_PAID", good_url)
    await stub.subscribe("ORDERS_PAID", flaky_url)
    await stub.buy(VARIANT_ID)

    log = await stub.deliveries()
    assert len(log) == 2, "two subscriptions, one dispatch, two rows"
    by_url = {row["callback_url"]: row for row in log}
    assert by_url[good_url]["attempts"] == 1
    assert by_url[flaky_url]["attempts"] == 3
    assert len(good_receiver.requests) == 1
    assert len(flaky_receiver.requests) == 3


# ---------------------------------------------------------------------------------------
# T-129 (stub 2): "the LAST attempt's outcome", with a fixture that can tell last from first
# ---------------------------------------------------------------------------------------
#
# The delivery-log fix (effe6b0) added a load-bearing claim to three docstrings —
# `status_code` and `error` hold the LAST attempt's outcome, and "an earlier failure that a
# later attempt recovered from leaves no trace here beyond `attempts` being greater than
# one" — and shipped no fixture that can distinguish last from first:
#
#   * `webhook_receiver` answers 200 once, so there is one attempt;
#   * `flaky_webhook_receiver` answers [500, 503, 200], and the loop breaks on success, so
#     the last attempt is the accepted one by construction;
#   * `dead_webhook_receiver` answers [500] * 50, so first, last, highest and lowest are
#     all the same number.
#
# Reproduced: an implementation that records the FIRST failure of a delivery that never
# succeeded passes all 498 tests in this package.
#
# The two receivers below make the failure path legible. `escalating_dead_webhook_receiver`
# answers three different failing statuses; `truncating_webhook_receiver` answers one real
# status and then cuts the connection, which is the only way `status_code` becomes None
# while an earlier attempt had a status.


async def test_a_failed_delivery_records_the_last_attempt_not_the_first(
    stub: StubClient, escalating_dead_webhook_receiver: Receiver
) -> None:
    """`503, 500, 502`: four candidate answers, and only "the last" is 502."""
    receiver, url = escalating_dead_webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    result = await stub.buy(VARIANT_ID)

    assert len(receiver.requests) == MAX_ATTEMPTS, "all three attempts really were made"

    (delivery,) = result["webhook_deliveries"]
    assert delivery["delivered"] is False
    assert delivery["attempts"] == MAX_ATTEMPTS
    assert delivery["status_code"] == 502, (
        "the LAST attempt's status: 503 would be the first, 500 the second, "
        "503 the highest and 500 the lowest"
    )
    assert delivery["error"] == "HTTP 502", "and `error` is that same attempt's"

    # Read back over `/_stub/webhooks/deliveries` too: the claim is about what the log
    # reports, not only about what the completion response happens to carry.
    (row,) = await stub.deliveries()
    assert row["status_code"] == 502
    assert row["error"] == "HTTP 502"
    assert row["attempts"] == MAX_ATTEMPTS
    assert row["delivered"] is False


async def test_a_status_from_an_earlier_attempt_does_not_survive_a_transport_failure(
    stub: StubClient, truncating_webhook_receiver: tuple[TruncatingReceiver, str]
) -> None:
    """The clause the escalating fixture still cannot reach: ``status_code`` is ``None``.

    `WebhookDelivery.status_code` is documented as "the **last** attempt's status, or
    ``None`` when the last attempt raised before a response". Attempt 1 here answers a real
    500; attempts 2 and 3 die mid-response. "The last attempt's status" is therefore
    ``None`` and "the last status there was" is 500 — the two readings finally disagree,
    and every other fixture in the package makes them identical.

    `error` must name the transport failure rather than the 500, for the same reason.
    """
    receiver, url = truncating_webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    result = await stub.buy(VARIANT_ID)

    assert len(receiver.requests) == MAX_ATTEMPTS, "the truncated turns are still attempts"

    (delivery,) = result["webhook_deliveries"]
    assert delivery["delivered"] is False
    assert delivery["attempts"] == MAX_ATTEMPTS
    assert delivery["status_code"] is None, (
        "the last attempt raised before a response, so there is no status; 500 here would "
        "mean the log kept the last attempt that happened to produce one"
    )
    assert delivery["error"] is not None
    assert not delivery["error"].startswith("HTTP "), (
        f"the last attempt's failure was a transport error, not a status: {delivery['error']}"
    )
    assert "Error" in delivery["error"], (
        f"`error` names the exception type for a transport failure: {delivery['error']}"
    )
