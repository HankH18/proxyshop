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
def test_every_recording_carries_a_provenance_header(path: Path) -> None:  # noqa: D401
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
    # "unversioned" is the honest value for the two surfaces whose pages carry NO version:
    # the Web Pixels API and the cart-permalink guide have neither a version selector nor an
    # api_version frontmatter key, unlike every Admin GraphQL page. Stamping "2026-07" on
    # them — which these headers originally did — asserts a provenance the page cannot
    # support, so the vocabulary is closed to exactly two values rather than left free-form.
    assert provenance["api_version"] in {"2026-07", "unversioned"}
    unversioned = {"cart_permalink", "web_pixel_checkout_completed"}
    expected = "unversioned" if path.stem in unversioned else "2026-07"
    assert provenance["api_version"] == expected, (
        f"{path.stem} claims api_version {provenance['api_version']!r}; the Admin GraphQL "
        f"pages are version-stamped and the Web Pixels / cart-permalink pages are not"
    )
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
    # Each of the four assertions above passes `"d": []` and is satisfied entirely by its
    # non-array defect — so the negative control that exists to prove this matcher can fail
    # was demonstrating, four times over, that an empty array is accepted. That is the hole
    # that let the orders/paid webhook ship ZERO line items with the whole suite green. An
    # emptied array must now fail on its own, with nothing else wrong.
    assert diff_shape(template, {"a": 1, "b": {"c": "x"}, "d": []})
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


def test_no_recording_claims_a_correction_it_did_not_make() -> None:
    """The three provenance headers found to be FALSE must stay corrected.

    Adversarial review caught three caveats asserting things the cited pages do not say —
    an invented `usageLimit` deprecation, a fabricated official JavaScript example, and a
    backwards account of which `value_type` the docs show. All three were verified
    independently and rewritten. On a ticket whose whole premise is that hand-authored
    fixtures stand in for an API nobody here can call, a false provenance header is the most
    expensive defect available, so the corrections are pinned rather than trusted to stay.
    """
    discount = load("admin_discount_code_basic_create")
    caveats = " ".join(discount["$provenance"]["caveats"])
    assert "usageLimit` is NOT deprecated" in caveats
    assert "`customerSelection` and `discountClass`" in caveats
    assert "DiscountPercentageInput" in discount["notes"], (
        "the notes must record that this type name 404s, so nobody reintroduces it"
    )

    permalink = load("cart_permalink")
    notes = " ".join(permalink["notes"])
    assert "contains NO JavaScript" in notes
    assert "split" in notes and "no such example exists" in notes

    paid = load("webhook_orders_paid")
    paid_caveats = " ".join(paid["$provenance"]["caveats"])
    assert "described the evidence backwards" in paid_caveats
    assert "BOTH `[]` in the orders/paid sample" in paid_caveats, (
        "the sample's discount arrays are empty; the element shapes come from the REST page"
    )


def test_the_recorded_throttle_block_carries_the_documented_numbers() -> None:
    """The rate-limit page has exactly ONE worked example; the recording uses its numbers.

    The recording previously carried invented values (2000/1989/100) that appear nowhere on
    the cited page — a fixture presenting made-up numbers as documentation-derived. The stub
    itself still emits its own plausible values, because it does not model query cost, and
    the parity check compares key names and types rather than values.
    """
    cost = load("admin_discount_code_basic_create")["response"]["extensions"]["cost"]
    assert cost["requestedQueryCost"] == 101
    assert cost["actualQueryCost"] == 46
    assert cost["throttleStatus"] == {
        "maximumAvailable": 1000,
        "currentlyAvailable": 954,
        "restoreRate": 50,
    }


# ---------------------------------------------------------------------------------------
# The emptied-array hole, and the per-array opt-out that closes it without false positives
# ---------------------------------------------------------------------------------------


def test_an_emptied_array_is_a_difference_unless_the_recording_says_otherwise() -> None:
    """The rule, and the reason it is per-array rather than blanket.

    Blanket ("expected non-empty, actual empty, always a difference") passes every test in
    this file and then false-positives on the first real payload: ``webhook_orders_paid``
    records a populated ``discount_codes``/``discount_applications``, and both are
    legitimately ``[]`` for an undiscounted order. So the recording opts *out* per array.
    """
    template = {"items": [{"sku": "a"}], "notes": [{"n": 1}]}
    assert diff_shape(template, {"items": [{"sku": "b"}], "notes": [{"n": 2}]}) == []

    problems = diff_shape(template, {"items": [], "notes": [{"n": 2}]})
    assert problems and "items" in problems[0]

    # The opt-out is named per key and applies to that key only.
    lenient = {"$may_be_empty": ["items"], **template}
    assert diff_shape(lenient, {"items": [], "notes": [{"n": 2}]}) == []
    assert diff_shape(lenient, {"items": [], "notes": []}), (
        "$may_be_empty must exempt only the arrays it names; 'notes' is not one of them"
    )
    # And the marker is metadata, not shape: it is never required of the response.
    assert diff_shape(lenient, {"items": [{"sku": "b"}], "notes": [{"n": 2}]}) == []


def test_an_empty_recorded_array_still_asserts_only_that_it_is_an_array() -> None:
    """Unchanged, and deliberately so: an empty doc example documents no element shape."""
    assert diff_shape({"userErrors": []}, {"userErrors": []}) == []
    assert diff_shape({"userErrors": []}, {"userErrors": [{"anything": 1}]}) == []
    assert diff_shape({"userErrors": []}, {"userErrors": {}})


async def test_the_orders_paid_webhook_can_never_ship_zero_line_items(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    """Acceptance criterion 1's headline claim, stated so a sabotage cannot pass it.

    Emptying the ``line_items`` comprehension in ``orders.py`` left the suite at 164 passed
    and the parity check reporting ``PASSED with 0 line items``: element-wise comparison
    iterates the *actual* array, so an empty one was compared zero times, and
    ``collect_keys({"line_items": []})`` produces no ``line_items[]`` path for the
    invented-key check to object to. A webhook body with no line items is not a conforming
    orders/paid body under any reading.
    """
    receiver, url = webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    await stub.create_code("PSX-7QK2ZB0M", percentage=0.10)
    await stub.buy(VARIANT_ID, code="PSX-7QK2ZB0M")

    delivered = json.loads(receiver.requests[0]["body"])
    assert delivered["line_items"], "an orders/paid body with no line items sells nothing"
    assert delivered["note_attributes"], (
        "the client_id join key rides in note_attributes; an empty array breaks every "
        "downstream reconciliation without breaking any shape check"
    )
    assert_conforms(load("webhook_orders_paid")["payload"], delivered, label="orders/paid")


async def test_an_undiscounted_order_still_conforms_with_empty_discount_arrays(
    stub: StubClient, webhook_receiver: Receiver
) -> None:
    """The false positive the blanket rule would have caused, asserted as a passing case.

    An order placed with no code carries ``discount_codes: []`` and
    ``discount_applications: []``. Both are recorded non-empty, and both are named in the
    recording's ``$may_be_empty``, so this conforms. Without the per-array opt-out this test
    is what T-050/T-061 would have hit the first time they asserted conformance on an order
    that happened to have no discount.
    """
    receiver, url = webhook_receiver
    await stub.subscribe("ORDERS_PAID", url)
    await stub.buy(VARIANT_ID)  # no code

    delivered = json.loads(receiver.requests[0]["body"])
    assert delivered["discount_codes"] == []
    assert delivered["discount_applications"] == []
    recording = load("webhook_orders_paid")
    assert_conforms(recording["payload"], delivered, label="orders/paid, no discount")
    assert_no_invented_keys(delivered, recording["documented_keys"], label="orders/paid")


async def test_an_undiscounted_order_conforms_in_the_orders_query_too(stub: StubClient) -> None:
    """Same false positive, second surface: ``discountCodes`` and ``discountApplications``."""
    await stub.buy(VARIANT_ID)  # no code
    recording = load("admin_orders_query")
    body = (await stub.graphql(recording["request"]["query"], {"first": 10})).json()

    node = body["data"]["orders"]["edges"][0]["node"]
    assert node["discountCodes"] == []
    assert node["discountApplications"]["edges"] == []
    assert node["lineItems"]["edges"], "an order with no line items is not an order"
    assert_conforms(recording["response"], body, label="orders, no discount")


async def test_an_undiscounted_checkout_conforms_in_the_pixel_event_too(stub: StubClient) -> None:
    """Third surface. ``discountApplications: []`` is what a no-code checkout emits."""
    await stub.buy(VARIANT_ID)  # no code
    (event,) = await stub.events()
    assert event["payload"]["data"]["checkout"]["discountApplications"] == []
    assert_conforms(
        load("web_pixel_checkout_completed")["payload"],
        event["payload"],
        label="checkout_completed, no discount",
    )


def test_every_may_be_empty_marker_names_an_array_the_recording_actually_records() -> None:
    """The opt-out must not rot into a list of names that mean nothing.

    A marker naming a key that is absent, or that is not an array, or that is *already* empty
    in the recording, exempts nothing and reads as though it does — which is how a fail-closed
    check quietly becomes fail-open again.
    """

    def walk(node: Any, where: str) -> list[str]:
        bad: list[str] = []
        if isinstance(node, dict):
            for name in node.get(recordings.MAY_BE_EMPTY_KEY, []):
                value = node.get(name)
                if not isinstance(value, list) or not value:
                    bad.append(f"{where}.{name}: marked $may_be_empty but is {value!r}")
            for key, value in node.items():
                if not key.startswith("$"):
                    bad.extend(walk(value, f"{where}.{key}"))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                bad.extend(walk(item, f"{where}[{index}]"))
        return bad

    problems: list[str] = []
    for path in recording_paths():
        data = json.loads(path.read_text(encoding="utf-8"))
        problems.extend(walk(data, path.stem))
    assert not problems, "\n".join(problems)
