"""Acceptance criterion 1: request/response parity with the recorded real-API shapes.

Every recording under ``fixtures/recorded/`` is hand-authored from published documentation
(D21) and carries a provenance header naming the pages it came from. Those recordings are
the only description of the real Shopify API this repo has — no credential exists here (D3)
— so they are checked in **both** directions:

**Forward.** Every key path in the recording exists in the stub's live response with the
same JSON type. Catches a stub that dropped or renamed a field the real API returns.

**Backward.** Every key the stub emits is one the recording documents for that path.
Catches a stub that *invented* a field — the failure a one-directional check cannot see,
and the one that quietly teaches T-050, T-052 and T-080 to read something that does not
exist.

The backward direction is why the recordings list more keys than the stub emits: a
``documented_keys`` entry is "these are the real key names at this path", not "these are the
keys the stub must produce". The stub is free to emit a subset; it is not free to emit
anything else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from shopify_stub import recordings
from shopify_stub.recordings import (
    assert_conforms,
    assert_no_invented_keys,
    diff_shape,
    load,
    recording_paths,
    validate_provenance,
)
from shopify_stub.testing import SEED_VARIANT, RecordingReceiver, StubClient

VARIANT_ID = int(SEED_VARIANT["variant_id"])
Receiver = tuple[RecordingReceiver, str]

#: Every recording this ticket is required to ship. Listed explicitly so a deleted file is
#: a failure rather than a silently shorter parametrize list.
EXPECTED_RECORDINGS = {
    "admin_discount_code_basic_create",
    "admin_orders_query",
    "admin_web_pixel_create",
    "admin_webhook_subscription_create",
    "cart_permalink",
    "web_pixel_checkout_completed",
    "webhook_orders_fulfilled",
    "webhook_orders_paid",
    "webhook_refunds_create",
}


# ---------------------------------------------------------------------------------------
# The recordings themselves
# ---------------------------------------------------------------------------------------


def test_every_required_recording_is_present() -> None:
    assert {path.stem for path in recording_paths()} == EXPECTED_RECORDINGS


@pytest.mark.parametrize("path", recording_paths(), ids=lambda p: p.stem)
def test_every_recording_carries_a_provenance_header(path: Path) -> None:
    """D21: a recording without a named doc version is exactly the failure it forbids.

    A fixture invented from memory but labelled documentation-derived is worse than no
    fixture, because four downstream tickets build on it. The header is therefore validated
    mechanically, not left to review.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_provenance(data, path)
    provenance = data["$provenance"]
    assert provenance["source"].startswith("https://shopify.dev/"), (
        "the source must be a published documentation URL"
    )
    assert provenance["api_version"] == "2026-07"
    assert "No live capture" in provenance["derivation"] or "no live" in (
        provenance["derivation"].lower()
    ), "the derivation must state that no live capture was performed (D21/D3)"


def test_a_recording_without_provenance_is_rejected(tmp_path: Path, monkeypatch: Any) -> None:
    """Negative control for the loader: the header check must actually fire."""
    monkeypatch.setattr(recordings, "RECORDINGS_DIR", tmp_path)
    (tmp_path / "bare.json").write_text('{"response": {}}', encoding="utf-8")
    with pytest.raises(recordings.RecordingError, match="provenance"):
        load("bare")

    (tmp_path / "partial.json").write_text(
        '{"$provenance": {"source": "https://shopify.dev/x"}, "response": {}}',
        encoding="utf-8",
    )
    with pytest.raises(recordings.RecordingError, match="api_version"):
        load("partial")


def test_the_shape_matcher_detects_the_differences_that_matter() -> None:
    """A parity test is worthless if its matcher cannot fail. This is that check.

    Every assertion below is a bug the forward check must catch: a missing key, a renamed
    key, a string where a number belongs, a boolean where a number belongs, and a difference
    buried inside an array element.
    """
    template = {"a": 1, "b": {"c": "x"}, "d": [{"e": True}]}
    assert diff_shape(template, {"a": 1, "b": {"c": "x"}, "d": [{"e": False}]}) == []

    assert diff_shape(template, {"b": {"c": "x"}, "d": []})  # missing "a"
    assert diff_shape(template, {"a": 1, "bee": {"c": "x"}, "d": []})  # renamed "b"
    assert diff_shape(template, {"a": "1", "b": {"c": "x"}, "d": []})  # string for number
    assert diff_shape(template, {"a": True, "b": {"c": "x"}, "d": []}), (
        "bool must not satisfy a number slot: isinstance(True, int) is True in Python"
    )
    assert diff_shape(template, {"a": 1, "b": {"c": "x"}, "d": [{"wrong": True}]})
    # A wider response is fine: the stub may answer more than the recording asked for.
    assert diff_shape(template, {"a": 1, "b": {"c": "x", "extra": 0}, "d": [{"e": True}]}) == []


def test_the_invented_key_check_detects_an_undocumented_field() -> None:
    """Negative control for the backward direction."""
    documented = {"": ["id", "name", "items"], "items[]": ["sku"]}
    assert_no_invented_keys({"id": 1, "name": "x", "items": [{"sku": "a"}]}, documented, label="ok")
    with pytest.raises(AssertionError, match="madeUp"):
        assert_no_invented_keys(
            {"id": 1, "name": "x", "madeUp": 2, "items": [{"sku": "a"}]},
            documented,
            label="bad",
        )
    with pytest.raises(AssertionError, match="invented"):
        assert_no_invented_keys(
            {"id": 1, "name": "x", "items": [{"sku": "a", "invented": 1}]},
            documented,
            label="bad",
        )
    # An empty array documents nothing about its element shape, and must not be reported as
    # a violation: `userErrors: []` is what every successful mutation returns.
    assert_no_invented_keys({"id": 1, "name": "x", "items": []}, documented, label="empty")


# ---------------------------------------------------------------------------------------
# The stub against the recordings
# ---------------------------------------------------------------------------------------


async def test_discount_mutation_matches_the_recording(stub: StubClient) -> None:
    recording = load("admin_discount_code_basic_create")
    variables = recording["request"]["variables"]
    response = await stub.graphql(recording["request"]["query"], variables)

    assert response.status_code == 200
    body = response.json()
    assert_conforms(recording["response"], body, label="discountCodeBasicCreate")
    assert_no_invented_keys(body, recording["documented_keys"], label="discountCodeBasicCreate")


async def test_the_duplicate_user_error_matches_the_recording(stub: StubClient) -> None:
    recording = load("admin_discount_code_basic_create")
    query = recording["request"]["query"]
    variables = recording["request"]["variables"]
    await stub.graphql(query, variables)
    body = (await stub.graphql(query, variables)).json()
    assert_conforms(recording["user_error_response"], body, label="userErrors")


async def test_the_unauthenticated_response_matches_the_recording(stub: StubClient) -> None:
    recording = load("admin_discount_code_basic_create")
    expected = recording["unauthenticated_response"]
    response = await stub.graphql(recording["request"]["query"], token=None)
    assert response.status_code == expected["http_status"]
    assert_conforms(expected["body"], response.json(), label="401 body")


async def test_orders_query_matches_the_recording(stub: StubClient) -> None:
    recording = load("admin_orders_query")
    await stub.create_code("PSX-7QK2ZB0M", percentage=0.10)
    await stub.buy(VARIANT_ID, code="PSX-7QK2ZB0M")

    response = await stub.graphql(recording["request"]["query"], recording["request"]["variables"])
    body = response.json()
    assert_conforms(recording["response"], body, label="orders")
    assert_no_invented_keys(body, recording["documented_keys"], label="orders")


async def test_web_pixel_create_matches_the_recording(stub: StubClient) -> None:
    recording = load("admin_web_pixel_create")
    response = await stub.graphql(recording["request"]["query"], recording["request"]["variables"])
    body = response.json()
    assert_conforms(recording["response"], body, label="webPixelCreate")
    assert_no_invented_keys(body, recording["documented_keys"], label="webPixelCreate")
    # The asymmetry the recording calls out: object in, serialized string out.
    settings = body["data"]["webPixelCreate"]["webPixel"]["settings"]
    assert isinstance(settings, str)
    assert json.loads(settings) == recording["request"]["variables"]["webPixel"]["settings"]


async def test_webhook_subscription_create_matches_the_recording(stub: StubClient) -> None:
    recording = load("admin_webhook_subscription_create")
    response = await stub.graphql(recording["request"]["query"], recording["request"]["variables"])
    body = response.json()
    assert_conforms(recording["response"], body, label="webhookSubscriptionCreate")
    assert_no_invented_keys(body, recording["documented_keys"], label="webhookSubscriptionCreate")


async def test_orders_paid_webhook_matches_the_recording(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    recording = load("webhook_orders_paid")
    receiver, url = webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    await stub.create_code("PSX-7QK2ZB0M", percentage=0.10)
    await stub.buy(VARIANT_ID, code="PSX-7QK2ZB0M")

    delivered = json.loads(receiver.requests[0]["body"])
    assert_conforms(recording["payload"], delivered, label="orders/paid")
    assert_no_invented_keys(delivered, recording["documented_keys"], label="orders/paid")

    headers = receiver.requests[0]["headers"]
    for name in recording["headers"]:
        assert name.lower() in headers, name


async def test_orders_fulfilled_webhook_matches_the_recording(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    recording = load("webhook_orders_fulfilled")
    receiver, url = webhook_receiver
    await stub.subscribe("ORDERS_FULFILLED", url)
    order = await stub.buy(VARIANT_ID)
    await stub.fulfil(
        order["order_id"], tracking_company="USPS", tracking_number="1Z1234512345123456"
    )

    delivered = json.loads(receiver.requests[0]["body"])
    assert_conforms(recording["payload"], delivered, label="orders/fulfilled")
    assert_no_invented_keys(delivered, recording["documented_keys"], label="orders/fulfilled")


async def test_refunds_create_webhook_matches_the_recording(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    recording = load("webhook_refunds_create")
    receiver, url = webhook_receiver
    await stub.subscribe("REFUNDS_CREATE", url)
    order = await stub.buy(VARIANT_ID)
    await stub.refund(order["order_id"], note="damaged in transit")

    delivered = json.loads(receiver.requests[0]["body"])
    assert_conforms(recording["payload"], delivered, label="refunds/create")
    assert_no_invented_keys(delivered, recording["documented_keys"], label="refunds/create")


async def test_the_pixel_event_matches_the_recording(stub: StubClient) -> None:
    recording = load("web_pixel_checkout_completed")
    await stub.create_code("PSX-7QK2ZB0M", percentage=0.10)
    await stub.buy(VARIANT_ID, code="PSX-7QK2ZB0M")

    (event,) = await stub.events()
    assert_conforms(recording["payload"], event["payload"], label="checkout_completed")
    assert_no_invented_keys(
        event["payload"], recording["documented_keys"], label="checkout_completed"
    )


async def test_the_collector_payload_matches_the_recording(
    stub: StubClient, collector_receiver: Receiver
) -> None:
    recording = load("web_pixel_checkout_completed")
    receiver, url = collector_receiver
    await stub.install_pixel(url)
    await stub.create_code("PSX-7QK2ZB0M", percentage=0.10)
    await stub.buy(VARIANT_ID, code="PSX-7QK2ZB0M")

    posted = json.loads(receiver.requests[0]["body"])
    expected = {k: v for k, v in recording["collector_payload"].items() if k != "$note"}
    assert set(posted) == set(expected), (
        "the collector payload's four keys are a published join-key contract; neither a "
        "missing nor an extra key is acceptable"
    )
    assert_conforms(expected, posted, label="collector payload")


async def test_the_degraded_collector_payload_matches_the_recording(
    stub: StubClient, collector_receiver: Receiver
) -> None:
    recording = load("web_pixel_checkout_completed")
    receiver, url = collector_receiver
    await stub.install_pixel(url)
    await stub.configure(pixel_mode="partial")
    await stub.buy(VARIANT_ID)

    posted = json.loads(receiver.requests[0]["body"])
    expected = {k: v for k, v in recording["degraded_collector_payload"].items() if k != "$note"}
    assert set(posted) == set(expected)
    assert posted["orderId"] is None
    assert posted["discountApplications"] is None


async def test_the_permalink_the_stub_answers_is_the_recorded_template(
    stub: StubClient,
) -> None:
    """The stub's own cart route must accept the exact template the recording publishes."""
    from shopify_stub.permalink import build_permalink, parse_permalink

    recording = load("cart_permalink")
    assert recording["template"] == (
        "https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}"
    )
    parsed = parse_permalink(recording["example"])
    assert parsed.shop_domain == "proxyshop-demo.myshopify.com"

    await stub.create_code(parsed.code or "PSX-7QK2ZB0M", percentage=0.10)
    url = build_permalink(
        shop_domain=parsed.shop_domain,
        variant_id=VARIANT_ID,
        quantity=parsed.quantity,
        code=parsed.code,
    )
    response = await stub.http.get(url.replace(f"https://{parsed.shop_domain}", ""))
    assert response.status_code == 303
    assert response.json()["discount_code"] == parsed.code
