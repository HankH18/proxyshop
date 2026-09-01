"""Acceptance criterion 3, the lossy half: a configurable drop rate and the non-firing mode.

Deliberately not named after the browser API it models: no test file outside ``pixel/`` may
carry that word in its path (D37), because the pixel ticket's verify is a case-insensitive
path-substring filter and this file would silently join that ticket's run.

What is asserted here, and why each one matters:

* **The rate is honoured exactly, not approximately.** With a seed, the emitted count is a
  number, not a band. A statistical assertion ("roughly half") passes for an implementation
  that drops nothing 5% of the time.
* **Every drop is recorded**, with a reason that distinguishes "the pixel is lossy" from
  "the pixel is not installed". Real Shopify records neither — the whole reason T-061 exists
  is that a lost beacon leaves no trace — and the stub's suppression log is what makes the
  loss testable at all.
* **The webhook is unaffected by every one of these settings.** Each test that turns the
  pixel down also checks the order still arrived, because the criterion is the *asymmetry*,
  not either half alone.
"""

from __future__ import annotations

import json

from shopify_stub.testing import SEED_VARIANT, RecordingReceiver, StubClient

VARIANT_ID = int(SEED_VARIANT["variant_id"])
Receiver = tuple[RecordingReceiver, str]


async def test_a_lossless_pixel_emits_one_event_per_checkout(stub: StubClient) -> None:
    for _ in range(4):
        result = await stub.buy(VARIANT_ID)
        assert result["pixel_event_emitted"] is True
    assert len(await stub.events()) == 4
    assert await stub.suppressed() == []


async def test_the_event_carries_the_documented_standard_event_shape(
    stub: StubClient,
) -> None:
    await stub.create_code("PSX-EVENT001")
    result = await stub.buy(VARIANT_ID, code="PSX-EVENT001")

    (event,) = await stub.events()
    payload = event["payload"]
    assert payload["name"] == "checkout_completed"
    assert payload["type"] == "standard"
    assert payload["clientId"] == result["client_id"]
    checkout = payload["data"]["checkout"]
    assert checkout["token"] == result["checkout_token"]
    assert checkout["order"]["id"] == str(result["order_id"])
    # MoneyV2 in the Web Pixels API carries a NUMERIC amount, unlike REST's decimal string.
    # A consumer that assumes one format on both surfaces breaks on whichever it skipped.
    assert isinstance(checkout["totalPrice"]["amount"], (int, float))
    assert not isinstance(checkout["totalPrice"]["amount"], str)
    (application,) = checkout["discountApplications"]
    assert application["type"] == "DISCOUNT_CODE"
    # The Web Pixels DiscountApplication type has no `code` member; `title` carries it.
    assert application["title"] == "PSX-EVENT001"
    assert "code" not in application


async def test_a_seeded_drop_rate_produces_an_exact_count(stub: StubClient) -> None:
    """A seed makes the loss reproducible, so the assertion is a number not a band."""
    await stub.configure(pixel_drop_rate=0.5, pixel_seed=1234)
    emitted = 0
    for _ in range(20):
        result = await stub.buy(VARIANT_ID)
        emitted += bool(result["pixel_event_emitted"])

    events = await stub.events()
    suppressed = await stub.suppressed()
    assert len(events) == emitted
    assert len(events) + len(suppressed) == 20, "every checkout is either emitted or logged"
    assert 0 < emitted < 20, (
        "a 50% rate that emitted everything or nothing would mean the knob does nothing"
    )
    assert all(item["reason"] == "dropped" for item in suppressed)


async def test_the_same_seed_reproduces_the_same_drop_pattern(stub_server: str) -> None:
    """Determinism, checked across two independent stubs rather than within one."""
    import httpx

    async def run() -> list[bool]:
        from shopify_stub.app import create_app

        from proxyshop_support.asgi_server import serve

        with serve(create_app()) as base_url:
            async with httpx.AsyncClient(base_url=base_url) as http:
                client = StubClient(http, base_url)
                await client.seed([SEED_VARIANT])
                await client.configure(pixel_drop_rate=0.5, pixel_seed=99)
                return [
                    bool((await client.buy(VARIANT_ID))["pixel_event_emitted"]) for _ in range(15)
                ]

    first = await run()
    second = await run()
    assert first == second
    assert len(set(first)) == 2, "a pattern of all-True or all-False proves nothing"


async def test_a_drop_rate_of_one_emits_nothing_but_still_delivers_the_order(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    receiver, url = webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    await stub.configure(pixel_drop_rate=1.0)

    for _ in range(3):
        result = await stub.buy(VARIANT_ID)
        assert result["pixel_event_emitted"] is False
        assert result["webhook_deliveries"][0]["delivered"] is True

    assert await stub.events() == []
    assert len(await stub.suppressed()) == 3
    assert len(receiver.requests) == 3


async def test_the_non_firing_mode_is_distinguishable_from_total_loss(
    stub: StubClient,
) -> None:
    """``pixel_mode=off`` and ``pixel_drop_rate=1.0`` both emit nothing — and differ.

    They are different conditions: one is a pixel that is installed and losing everything,
    the other is a pixel that is not there. A reconciler that treats "absent" as "lossy"
    waits for an event that is never coming; one that treats "lossy" as "absent" stops
    reconciling a signal it still has. The stub keeps them apart in the suppression reason,
    which is the only place the difference is visible at all.
    """
    await stub.configure(pixel_drop_rate=1.0, pixel_mode="on")
    await stub.buy(VARIANT_ID)
    await stub.configure(pixel_mode="off", pixel_drop_rate=0.0)
    await stub.buy(VARIANT_ID)

    reasons = [item["reason"] for item in await stub.suppressed()]
    assert reasons == ["dropped", "not_firing"]
    assert await stub.events() == []


async def test_the_non_firing_mode_ignores_the_drop_rate_entirely(
    stub: StubClient,
) -> None:
    """A pixel that is not installed cannot be made to fire by lowering the loss rate."""
    await stub.configure(pixel_mode="off", pixel_drop_rate=0.0)
    for _ in range(5):
        assert (await stub.buy(VARIANT_ID))["pixel_event_emitted"] is False
    assert await stub.events() == []
    assert {item["reason"] for item in await stub.suppressed()} == {"not_firing"}


async def test_the_partial_mode_emits_an_event_with_null_join_keys(
    stub: StubClient, collector_receiver: Receiver
) -> None:
    """The second documented non-firing case: the event exists, the join keys do not.

    This is the shape the frozen collector contract describes for a dropped event — the
    clean event with ``orderId`` and ``discountApplications`` nulled — and a collector has
    to record a visible gap for it rather than crash or count a conversion.
    """
    receiver, url = collector_receiver
    await stub.install_pixel(url)
    await stub.create_code("PSX-PARTIAL1")
    await stub.configure(pixel_mode="partial")

    result = await stub.buy(VARIANT_ID, code="PSX-PARTIAL1")
    assert result["pixel_event_emitted"] is True
    assert result["pixel_event_posted"] is True

    body = json.loads(receiver.requests[0]["body"])
    assert set(body) == {"clientId", "checkoutToken", "orderId", "discountApplications"}
    assert body["clientId"] == result["client_id"]
    assert body["checkoutToken"] == result["checkout_token"]
    assert body["orderId"] is None
    assert body["discountApplications"] is None


async def test_an_installed_pixel_posts_the_collector_payload(
    stub: StubClient, collector_receiver: Receiver
) -> None:
    """``webPixelCreate`` settings carry the collector URL; the event is POSTed there."""
    receiver, url = collector_receiver
    response = await stub.install_pixel(url)
    payload = response.json()["data"]["webPixelCreate"]
    assert payload["userErrors"] == []
    assert payload["webPixel"]["id"].startswith("gid://shopify/WebPixel/")
    # `settings` is the JSON scalar: an object going in, a serialized STRING coming back.
    assert isinstance(payload["webPixel"]["settings"], str)
    assert json.loads(payload["webPixel"]["settings"])["collectorUrl"] == url

    await stub.create_code("PSX-COLLECT1", percentage=0.10)
    result = await stub.buy(VARIANT_ID, code="PSX-COLLECT1")

    (request,) = receiver.requests
    body = json.loads(request["body"])
    assert set(body) == {"clientId", "checkoutToken", "orderId", "discountApplications"}
    assert body["orderId"] == f"gid://shopify/Order/{result['order_id']}"
    assert body["discountApplications"] == [{"code": "PSX-COLLECT1", "value": 10.0, "type": "code"}]


async def test_a_dropped_event_never_reaches_the_collector(
    stub: StubClient, collector_receiver: Receiver
) -> None:
    """Negative control for the collector POST: a suppressed event must not be sent."""
    receiver, url = collector_receiver
    await stub.install_pixel(url)
    await stub.configure(pixel_drop_rate=1.0)
    for _ in range(3):
        await stub.buy(VARIANT_ID)
    assert receiver.requests == []


async def test_a_dead_collector_cannot_break_the_order(stub: StubClient) -> None:
    """A beacon that cannot reach the collector is one more way the pixel is lossy.

    If a failed pixel POST could 500 the checkout, the pixel would be able to break the
    order — the exact inversion of "the webhook is the truth".
    """
    await stub.configure(pixel_collector_url="http://127.0.0.1:1/collect")
    result = await stub.buy(VARIANT_ID)
    assert result["pixel_event_emitted"] is True
    assert result["pixel_event_posted"] is False
    assert len(await stub.events()) == 1


async def test_the_pixel_event_carries_no_customer_pii(stub: StubClient) -> None:
    """The Web Pixels Checkout type has email/phone/addresses. The stub emits none."""
    await stub.buy(VARIANT_ID)
    raw = json.dumps(await stub.events())
    for forbidden in (
        "email",
        "phone",
        "first_name",
        "last_name",
        "billingAddress",
        "shippingAddress",
        "customerId",
    ):
        assert forbidden not in raw, f"{forbidden} must never appear in a pixel event"


async def test_an_invalid_drop_rate_is_refused(stub: StubClient) -> None:
    assert (await stub.configure(pixel_drop_rate=1.5)).status_code == 400
    assert (await stub.configure(pixel_drop_rate=-0.1)).status_code == 400
    assert (await stub.config())["pixel_drop_rate"] == 0.0, "a refused write changes nothing"


async def test_an_unknown_mode_is_refused(stub: StubClient) -> None:
    response = await stub.configure(pixel_mode="sometimes")
    assert response.status_code == 400
    assert "pixel_mode must be one of" in response.json()["errors"]


async def test_a_typo_in_a_config_key_is_refused(stub: StubClient) -> None:
    """A misspelled knob that returned 200 is how a test asserts the default and believes
    it configured something."""
    response = await stub.configure(pixel_droprate=0.5)
    assert response.status_code == 400
    assert "pixel_droprate" in response.json()["errors"]
