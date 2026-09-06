"""T-365 — the collector's body cap bounds the REQUEST; nothing bounded a FIELD inside it.

``POST /pixel/collect`` takes no credential and its body is capped at 1 MiB by
``http_limits.read_capped_body``. That cap is correct and is not what this file is about.
What it does not do is bound anything *inside* the body, and the collector keeps what is
inside the body — so the cap produced the appearance of a bound while the thing actually
retained stayed unbounded.

Two separable halves, both measured against the code as it shipped:

**The store.** ``_scalar()`` checked a value's TYPE (str/int/float) and returned
``value.strip()`` with no length check, so each of the four join keys could carry ~200 KB
under a body that still rode under the 1 MiB cap. Those strings land in
:class:`~merchant_svc.collector.PixelInbox`, a 512-entry ring. The ring bounds the entry
COUNT and nothing bounded the entry SIZE, so the true ceiling was::

    800,970 B retained per observation x 512 entries = 391.10 MiB

against ``apps/merchant/compose.yaml:66  mem_limit: 256m``. **The ceiling was larger than
the container**, which means the process OOMs at roughly 330 anonymous requests and never
reaches its own limit. A bound whose ceiling is outside the box is not a bound.

**The log.** The accept path wrote ``observation.checkout_token`` verbatim into an
``_log.info`` for every beacon with a gap. Measured: a 900,000-char token produced a
900,090-char log record, one per request — and a log file has no capacity at all, so that
half is linear and unbounded in aggregate. It also contradicted the rule the same module
states thirty lines above it, that "an error body is as much a place data lives as a
database is": the refusal path honours that rule and the accept path did not.

What the green here is evidence of, and what it is not: these are unit and in-process ASGI
tests against the real ``create_app()`` router, so they witness the refusal the deployed
route gives. They say nothing about how much memory a real container uses under real load;
the arithmetic above is the claim, and ``test_the_full_ring_of_hostile_beacons_fits_inside``
is what pins it.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from merchant_svc.collector import (
    PIXEL_INBOX,
    PixelEventRejected,
    PixelInbox,
    accept_pixel_event,
)
from merchant_svc.http_limits import MAX_REQUEST_BODY_BYTES
from merchant_svc.install.config import COLLECTOR_PATH

#: The container the collector runs in — ``apps/merchant/compose.yaml:66  mem_limit: 256m``.
#: The number the retention ceiling has to be *smaller* than for the ring to mean anything.
CONTAINER_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024

#: The share of that container the pixel inbox may claim. One eighth: the ring is one buffer
#: inside a process that also runs the install flow, the webhook inbox and an ASGI server,
#: so "fits in the container" is too weak a bar — it has to fit with room for the service.
INBOX_RETENTION_BUDGET_BYTES = CONTAINER_MEMORY_LIMIT_BYTES // 8

#: A refusal body that grows with the payload is an amplifier on an unauthenticated route.
#: ``test_the_collector_refusal_echoes_neither_the_value_nor_the_key`` already pins this for
#: the unknown-key refusal; the length refusal is held to the same number.
MAX_REFUSAL_BODY_BYTES = 512

#: The logger the accept path emits its gap line on.
COLLECTOR_LOG = "merchant_svc.collector.routes"


@pytest.fixture(autouse=True)
def _empty_inbox() -> Iterator[None]:
    """The process-wide inbox, emptied around every test in this file."""
    PIXEL_INBOX.clear()
    yield
    PIXEL_INBOX.clear()


@pytest.fixture
async def collector_client() -> Any:
    """The real merchant app, reached in-process over ASGI.

    ``raise_app_exceptions=False`` on purpose: an unhandled exception has to surface as the
    500 a browser would actually see, not as a test-time traceback. Half of what this file
    asserts is that a hostile beacon gets a clean 4xx and never a 5xx, and that assertion is
    vacuous against a transport that re-raises instead of answering.
    """
    from merchant_svc.main import create_app

    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://merchant-svc") as client:
        yield client


def _retained_bytes(obj: Any, seen: set[int] | None = None) -> int:
    """Deep ``sys.getsizeof`` of ``obj``, counting each object once.

    Identity-deduplicated because that is the honest measure of a ring: CPython interns and
    shares strings, so charging a shared object to every holder would overstate a duplicated
    roster. Here every beacon carries its own text, so nothing is shared and the number is
    the real retention.
    """
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    total = sys.getsizeof(obj)
    if isinstance(obj, str | bytes):
        return total
    if isinstance(obj, dict):
        for key, value in obj.items():
            total += _retained_bytes(key, seen) + _retained_bytes(value, seen)
    elif isinstance(obj, list | tuple | set | frozenset):
        for value in obj:
            total += _retained_bytes(value, seen)
    elif hasattr(obj, "__dict__"):
        total += _retained_bytes(vars(obj), seen)
    return total


def _widest_beacon_under_the_body_cap() -> dict[str, Any]:
    """The most text an attacker can get past ``read_capped_body`` in one legal beacon.

    Four join keys of 200,000 characters each: 800,072 bytes encoded, comfortably under the
    1 MiB body cap — which is the point. The body cap never saw this.

    Each field is a DISTINCT string, because a real sender's four fields are distinct and
    :func:`_retained_bytes` charges a shared object once. Four references to one interned
    filler would measure a quarter of the retention an attacker actually causes.
    """
    return {
        "checkoutToken": "a" * 200_000,
        "clientId": "b" * 200_000,
        "orderId": "c" * 200_000,
        "discountCode": "d" * 200_000,
    }


# ======================================================================================
# The store — the ring bounded the entry count and nothing bounded the entry size
# ======================================================================================
def test_the_full_ring_of_hostile_beacons_fits_inside_the_container() -> None:
    """The retention ceiling was 391.10 MiB inside a 256 MiB container.

    Measured against one observation rather than by filling all 512 slots, because the ring
    is a fixed-capacity list and 512 x 800 KB is the same arithmetic paid in real memory.
    The per-entry number is what the code controls; the multiplication is what the ring does
    with it.

    Rejected here and only here: a per-entry size whose product with the ring's capacity is
    a number the process cannot hold.
    """
    hostile = _widest_beacon_under_the_body_cap()
    encoded = len(json.dumps(hostile).encode("utf-8"))
    assert encoded < MAX_REQUEST_BODY_BYTES, (
        f"the hostile beacon is {encoded} bytes and the body cap is {MAX_REQUEST_BODY_BYTES}; "
        "this vector has to ride UNDER the cap or it proves nothing about a field bound"
    )

    try:
        observation = accept_pixel_event(hostile)
    except PixelEventRejected:
        # Refusing the whole beacon is the fix, and it retains nothing at all.
        return

    per_entry = _retained_bytes(observation)
    ceiling = per_entry * PixelInbox().capacity
    assert ceiling <= INBOX_RETENTION_BUDGET_BYTES, (
        f"one accepted beacon retains {per_entry:,} bytes, so a full {PixelInbox().capacity}"
        f"-entry ring holds {ceiling / (1 << 20):.2f} MiB — against a "
        f"{CONTAINER_MEMORY_LIMIT_BYTES / (1 << 20):.0f} MiB container "
        f"(apps/merchant/compose.yaml:66) and a "
        f"{INBOX_RETENTION_BUDGET_BYTES / (1 << 20):.0f} MiB budget for this buffer. "
        "The ring bounds the entry COUNT; nothing bounds the entry SIZE."
    )


@pytest.mark.parametrize(
    "alias",
    ["checkoutToken", "clientId", "orderId", "discountCode", "currency", "timestamp"],
)
def test_no_accepted_field_may_carry_two_hundred_kilobytes(alias: str) -> None:
    """Every published field, not only the four that happen to be stored today.

    ``currency`` and ``timestamp`` are accepted and currently discarded, which is a property
    of this implementation and not of the contract. A field the collector publishes as
    acceptable is a field a future reader may retain, so the ceiling is on the allowlist and
    not on the subset of it that is read.

    Rejected here and only here: an allowlisted field carrying 200,000 characters.
    """
    beacon: dict[str, Any] = {"checkoutToken": "ck-1"}
    beacon[alias] = "x" * 200_000

    with pytest.raises(PixelEventRejected):
        accept_pixel_event(beacon)


def test_an_over_long_join_key_is_refused_whole_and_nothing_is_recorded() -> None:
    """Refused, not truncated, and not silently emptied.

    A truncated ``checkout_token`` is a *wrong* checkout token: R4 makes the webhook
    authoritative and the pixel lossy, and the token is the only thing the reconciler joins
    the two on. A prefix joins to nothing, or to the wrong order, while looking like a
    complete observation — which is worse than losing one beacon, because a lost beacon is
    already an outcome R4 is built to survive and a mis-joined one is not.

    Rejected here and only here: an over-long join key. What must NOT happen is a truncated
    observation appearing in the inbox.
    """
    with pytest.raises(PixelEventRejected):
        accept_pixel_event({"checkoutToken": "x" * 200_000, "clientId": "cid-1"})

    assert PIXEL_INBOX.observations() == (), "a refused beacon reached the inbox anyway"


def test_a_discount_code_lifted_out_of_discount_applications_is_bounded_too() -> None:
    """The nested path retains a string the top-level path would have refused.

    ``_discount_code`` reaches into a validated ``discountApplications`` entry and lifts
    ``code`` into the observation, so a bound applied only to the top level leaves the same
    string arriving one level down. Entry keys are allowlisted; entry VALUES were not
    bounded at all.

    Rejected here and only here: an over-long ``code`` inside a discount application.
    """
    beacon = {
        "checkoutToken": "ck-1",
        "discountApplications": [{"code": "y" * 200_000, "type": "code"}],
    }
    with pytest.raises(PixelEventRejected):
        accept_pixel_event(beacon)

    assert PIXEL_INBOX.observations() == ()


# ======================================================================================
# The log — the accept path wrote an attacker's text into a sink with no capacity
# ======================================================================================
async def test_the_gap_log_line_does_not_grow_with_the_beacon(
    collector_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """One line per request, and its size was the sender's to choose.

    Both beacons below are *accepted* — this is the accept path, not the refusal path — and
    they differ only in how long the checkout token is. The line the collector writes must
    not differ in length, because a log has no capacity and the sender is anonymous.
    Measured before the fix: a 900,000-char token wrote 900,090 characters, once per request,
    amplification 1.00x and unbounded in aggregate.

    Rejected here and only here: a log line whose length is a function of the beacon's.
    Still admitted: a line that identifies WHICH checkout was incomplete, which is the whole
    reason the line exists.
    """
    short_token = "a" * 64
    long_token = "b" * 448

    messages: list[str] = []
    for token in (short_token, long_token):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=COLLECTOR_LOG):
            response = await collector_client.post(COLLECTOR_PATH, json={"checkoutToken": token})
        assert response.status_code == 204, response.text
        gap_lines = [r.getMessage() for r in caplog.records if r.name == COLLECTOR_LOG]
        assert gap_lines, "the incomplete-observation line was not emitted at all"
        messages.append(gap_lines[0])

    assert len(messages[0]) == len(messages[1]), (
        f"the gap log line grew from {len(messages[0])} to {len(messages[1])} characters "
        f"when the sender lengthened the token by {len(long_token) - len(short_token)}; a log "
        "line's size must not be the anonymous sender's to choose"
    )
    assert long_token not in messages[1], (
        "the accept path wrote the raw attacker-chosen token into the log, which the same "
        "module forbids the refusal path from doing"
    )


# ======================================================================================
# The route — a hostile field is a cheap 4xx, never a 5xx
# ======================================================================================
async def test_the_route_refuses_an_over_long_field_with_a_small_clean_4xx(
    collector_client: httpx.AsyncClient,
) -> None:
    """A browser pixel is fire-and-forget, so the refusal has to be cheap and quiet.

    Rejected here and only here: a 5xx, or a refusal body that carries the offending text or
    grows with it.
    """
    value = "z" * 200_000
    response = await collector_client.post(
        COLLECTOR_PATH, json={"checkoutToken": "ck-1", "clientId": value}
    )

    assert response.status_code == 400, (
        f"an over-long field answered {response.status_code}; an unauthenticated telemetry "
        "endpoint must refuse hostile input with a clean 4xx"
    )
    assert value not in response.text, "the refusal echoed the offending value back"
    assert len(response.text) < MAX_REFUSAL_BODY_BYTES, (
        f"the refusal body is {len(response.text)} bytes; it must not grow with the payload"
    )


async def test_a_beacon_whose_field_is_a_wall_of_digits_is_not_a_five_hundred(
    collector_client: httpx.AsyncClient,
) -> None:
    """A JSON *number* reaches ``_scalar`` too, and ``str(int)`` is not total.

    CPython caps integer-to-string conversion at 4300 digits and raises ``ValueError`` past
    it. ``PixelEventRejected`` subclasses ``ValueError``, so a caller catching the refusal
    type would swallow this one and read it as a refusal it never made — and the route
    catches only ``PixelEventRejected``, so it would answer 500.

    Rejected here and only here: a 5xx from a numeric field, and an accepted observation
    carrying thousands of digits as its order reference.
    """
    for digits in (4_000, 900_000):
        body = '{"checkoutToken": "ck-1", "orderId": ' + ("9" * digits) + "}"
        response = await collector_client.post(
            COLLECTOR_PATH, content=body, headers={"content-type": "application/json"}
        )
        assert response.status_code < 500, (
            f"a {digits}-digit numeric field answered {response.status_code}; a hostile "
            "field on an unauthenticated route must never become a server error"
        )
        assert response.status_code == 400, (
            f"a {digits}-digit numeric field answered {response.status_code}; it is far past "
            "any field ceiling and must be refused, not stored"
        )
        assert PIXEL_INBOX.observations() == (), "the digit wall was recorded anyway"


def test_a_giant_integer_is_refused_without_ever_being_rendered() -> None:
    """The in-process caller sees the refusal type, not CPython's digit-limit ``ValueError``.

    The route is protected by ``json.loads`` refusing the same body first, so this is the
    half of the hazard the route does not witness: anything that hands
    ``accept_pixel_event`` an already-decoded ``int`` — a reconciler, a replay tool, a test —
    reaches ``str(value)`` directly, and past 4300 digits that raises. ``PixelEventRejected``
    subclasses ``ValueError``, so such a caller would read CPython's complaint as a refusal
    this module never made.

    Note the input is built with ``<<`` and ``**`` rather than ``int("9" * n)``: the digit
    limit binds on the way IN as well, so the obvious spelling of this test cannot construct
    its own input.

    Rejected here and only here: an integer past the ceiling, by either branch of the guard —
    the bit-length short-circuit that never renders, and the exact comparison that does.
    Still admitted: an integer whose rendering fits.
    """
    # Far past `_MAX_SCALAR_INT_BITS`: refused by bit_length, never handed to `str()`.
    with pytest.raises(PixelEventRejected):
        accept_pixel_event({"checkoutToken": "ck-1", "orderId": 1 << 3_000_000})

    # 601 digits, ~1995 bits: under the bit-length short-circuit, so this one IS rendered and
    # refused on its exact length. Without this vector the short-circuit alone would pass.
    with pytest.raises(PixelEventRejected):
        accept_pixel_event({"checkoutToken": "ck-1", "orderId": 10**600})

    # 501 digits: inside the ceiling, and accepted as the identifier it renders to.
    accepted = accept_pixel_event({"checkoutToken": "ck-1", "orderId": 10**500})
    assert accepted.order_ref == str(10**500)


# ======================================================================================
# The positive control — everything a real Shopify pixel sends is still accepted whole
# ======================================================================================
async def test_the_real_shopify_shaped_beacon_is_still_accepted_and_kept_intact(
    collector_client: httpx.AsyncClient,
) -> None:
    """The bound is worthless if it refuses the traffic the endpoint exists for.

    Real shapes: a 32-hex checkout token, an Order GID, a UUID client id, and a discount code
    at Shopify's own 255-character maximum — the longest legitimate join key there is.

    Still admitted: all of it, stored verbatim. Nothing here may be truncated.
    """
    beacon = {
        "checkoutToken": "0f2a4c6e8b1d3f5a7c9e0b2d4f6a8c1e",
        "orderId": "gid://shopify/Order/5123456789012",
        "clientId": "7c4a2b10-9e3d-4f81-a6b5-0d2c8e1f3a49",
        "discountCode": "P" * 255,
        "totalPrice": "44.10",
        "currency": "USD",
        "discountApplications": [{"code": "P" * 255, "value": 10.0, "type": "code"}],
    }

    response = await collector_client.post(COLLECTOR_PATH, json=beacon)
    assert response.status_code == 204, response.text

    observation = accept_pixel_event(beacon)
    assert observation.checkout_token == beacon["checkoutToken"]
    assert observation.order_ref == beacon["orderId"]
    assert observation.client_id == beacon["clientId"]
    assert observation.discount_code == beacon["discountCode"], (
        "a 255-character discount code is Shopify's own maximum and was truncated or dropped"
    )
    assert observation.gaps == ()


def test_a_field_at_exactly_the_ceiling_is_accepted_and_one_character_over_is_not() -> None:
    """The boundary itself, so the bound cannot drift into off-by-one either direction.

    Rejected here and only here: ceiling + 1. Still admitted: ceiling.
    """
    from merchant_svc.collector import MAX_PIXEL_FIELD_CHARS  # noqa: PLC0415

    at_ceiling = accept_pixel_event({"checkoutToken": "x" * MAX_PIXEL_FIELD_CHARS})
    assert len(at_ceiling.checkout_token) == MAX_PIXEL_FIELD_CHARS

    with pytest.raises(PixelEventRejected):
        accept_pixel_event({"checkoutToken": "x" * (MAX_PIXEL_FIELD_CHARS + 1)})


def test_the_published_ceiling_leaves_room_for_the_longest_real_join_key() -> None:
    """The number has to be justified by the traffic, not chosen to make a test pass.

    Shopify's discount codes go to 255 characters and that is the longest join key a real
    beacon carries; a checkout token is 32 hex characters and an Order GID is under 40. The
    ceiling must clear the longest real one with headroom, and must still be small enough
    that a full ring fits the container budget.
    """
    from merchant_svc.collector import GAP_KEYS, MAX_PIXEL_FIELD_CHARS  # noqa: PLC0415

    longest_real_join_key = 255  # Shopify's own discount-code maximum
    assert MAX_PIXEL_FIELD_CHARS >= 2 * longest_real_join_key, (
        f"a {MAX_PIXEL_FIELD_CHARS}-character ceiling leaves no headroom over a "
        f"{longest_real_join_key}-character discount code"
    )

    # A crude upper bound on the ring: every join key at the ceiling, in every slot. Real
    # retention is larger by per-object overhead, which `test_the_full_ring_of_hostile_
    # beacons_fits_inside_the_container` measures; this asserts the arithmetic the constant
    # was chosen from.
    text_ceiling = MAX_PIXEL_FIELD_CHARS * len(GAP_KEYS) * PixelInbox().capacity
    assert text_ceiling < INBOX_RETENTION_BUDGET_BYTES, (
        f"{MAX_PIXEL_FIELD_CHARS} chars x {len(GAP_KEYS)} join keys x "
        f"{PixelInbox().capacity} slots = {text_ceiling / (1 << 20):.2f} MiB, over the "
        f"{INBOX_RETENTION_BUDGET_BYTES / (1 << 20):.0f} MiB budget for this buffer"
    )
