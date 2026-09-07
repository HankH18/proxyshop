"""S1 link 7b: the web pixel's observation reaches E6's chained ledger, or is visibly lost.

THE OTHER HALF OF R4, AND WHY IT WAS NOT THERE
-----------------------------------------------
``test_webhook_ledger.py`` is link 7a: the AUTHORITATIVE half of the pixel↔webhook
reconciliation leaving the merchant process. This file is 7b, the lossy half, and until it
existed the half did not leave at all. Measured on the served routes, nothing stubbed::

    merchant  POST /pixel/collect   -> 204
    merchant  PIXEL_INBOX           -> one PixelObservation, joinable, correct
    trust     GET  /events          -> {"events":[],"count":0}
    trust     GET  /reconcile       -> every purchase "pixel_missing": true

``collector/routes.py`` recorded the observation into a 512-slot in-memory ring whose only
reader was a test, and its own docstring named "E6's reconciler" as the reader. E6 does not
read it and cannot: they are separate deployables and the ring is process memory.

The consequence is not cosmetic. ``trust.reconcile.engine`` is a THREE-way join —
``accepted`` (the promise), ``order_paid`` (the truth), ``checkout_pixel`` (the client-side
observation) — and the frozen ledger kind ``checkout_pixel`` had **no producer anywhere in
the tree**, so the engine ran permanently on two inputs and every purchase in the system
reconciled as ``pixel_missing: true``. Under the redirect (SPEC "Core tenet") the pixel is
how the platform observes whether a PITCH CONVERTED, so this is the outcome half of the
impression→outcome loop that pitch optimisation is supposed to learn from.

WHAT EVERY TEST HERE DRIVES
----------------------------
The served route, always. ``POST /pixel/collect`` on a real merchant app on a real loopback
port, and the proof is another deployable's own ``GET /events`` / ``GET /events/verify`` /
``GET /reconcile``. Two of them additionally run the **real web pixel** — ``node`` executing
``pixel/src/*.ts`` unchanged, its own ``fetch(..., {keepalive: true})`` over a real socket —
because a collector and a hand-typed body can agree with each other forever while the thing
that actually POSTs sends something else.

WHAT THE PUBLISHED ROW MAY CARRY, AND WHY IT IS SAFE
-----------------------------------------------------
This beacon runs in a shopper's browser during checkout on the merchant's own store, and the
ledger it lands in is append-only, unauthenticated to read, and public forever. R5 promises
stores never receive buyer identity. The projection is therefore an ALLOWLIST of four keys,
pinned by ``test_the_published_row_carries_only_join_keys_and_purchase_facts``:

``checkout_token``  Shopify's per-checkout token. Network-issued, scoped to one checkout,
                    names no person. It is the value the ``orders/paid`` webhook also
                    carries, which is what makes the two halves meet.
``client_id``       D24's pinned join key: a rotating pseudonym (R5), not an identity. It is
                    already on the ``order_paid`` half of every purchase; filing the pixel
                    half under a DIFFERENT key would leave the two halves unable to meet.
``total_price``     A purchase fact about an order, not about a person.
``gaps``            Which join keys the beacon did not carry, drawn from the collector's own
                    fixed ``GAP_KEYS`` vocabulary — this repo's words, never the sender's.

Nothing else. Not the raw beacon body (the events door refuses >64 KiB and nothing on that
router is authenticated, so a forwarded body is public forever), not ``order_ref`` (see
``test_a_hostile_beacon_cannot_merge_two_unrelated_orders``), not the discount code (the
reconciler refuses by design to read a code off a pixel, so publishing a live redeemable
code on a second public row buys nothing and costs something).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from contracts.ledger import LEDGER_PAYLOAD_SHAPES, join_key_view
from merchant_svc import composition
from merchant_svc.collector import GAP_KEYS, PIXEL_INBOX, PixelObservation
from merchant_svc.install.config import COLLECTOR_PATH, WEBHOOK_PATH_PREFIX
from merchant_svc.install.webhooks import WebhookInbox
from shopify_stub.state import DEFAULT_SHOP_DOMAIN
from shopify_stub.testing import SEED_VARIANT, StubClient

from proxyshop_support.asgi_server import serve

#: Repo root: ``apps/merchant/svc/tests/`` is four levels down.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: The harness that runs the real pixel. Its own docstring states the protocol.
PIXEL_BEACON_DRIVER = REPO_ROOT / "pixel" / "tests" / "beacon-driver.ts"

#: The recorded Shopify surfaces, authored by the stub lane from Shopify's published type
#: tables (D21/D3) — evidence rather than a restatement of the code under test.
RECORDINGS = REPO_ROOT / "services" / "shopify-stub" / "fixtures" / "recorded"

#: A cold ``node`` start plus a loopback POST. Generous on purpose: a flaky timeout in an
#: unattended run is worse than a slow test.
PIXEL_DRIVER_TIMEOUT_SECONDS = 120.0

#: The frozen kind this file exists to give a producer.
PIXEL_KIND = "checkout_pixel"

#: Exactly the keys the projection may put on a public, append-only row. See the header.
PUBLISHED_PIXEL_KEYS = frozenset({"checkout_token", "client_id", "total_price", "gaps"})


@pytest.fixture(autouse=True)
def _empty_inbox() -> Iterator[None]:
    """The process-wide collector ring, emptied around every test in this file."""
    PIXEL_INBOX.clear()
    yield
    PIXEL_INBOX.clear()


@pytest.fixture
def merchant_origin() -> Iterator[str]:
    """The real merchant app on a real loopback port (D40).

    Not the in-process ASGI transport: the claim is that the request a browser beacon really
    builds is one this deployed route accepts and publishes onward, and a transport that
    never encodes a request cannot witness that.
    """
    from merchant_svc.main import create_app  # noqa: PLC0415

    with serve(create_app()) as base_url:
        yield base_url


@pytest.fixture(scope="module")
def node_binary() -> str:
    """The ``node`` that runs the pixel. Missing ``node`` FAILS rather than skips.

    A skip here would turn "the emitter was never exercised" into a green run, which is the
    exact shape of vacuous gate this file exists to replace.
    """
    found = shutil.which("node")
    if found is None:
        pytest.fail(
            "`node` is not on PATH, so the real web pixel cannot be run and the emitter half "
            "of this gate would go unexercised."
        )
    return found


def recorded(name: str) -> dict[str, Any]:
    """One recorded Shopify surface, or a loud failure naming the file that is missing."""
    path = RECORDINGS / name
    if not path.is_file():
        pytest.fail(f"the recorded Shopify fixture {path} is missing; nothing to grade against")
    return json.loads(path.read_text(encoding="utf-8"))


def drive_the_pixel(node: str, collector_url: str, event: Any) -> dict[str, Any]:
    """Run the real web pixel over ``event`` and return what it did.

    A non-zero exit is reported with node's stderr, because a pixel that crashed and a pixel
    that decided not to send are the same silence at the collector and must never be the same
    test result.
    """
    request = json.dumps({"collectorUrl": collector_url, "event": event})
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, repo-local script
        [node, str(PIXEL_BEACON_DRIVER)],
        input=request,
        capture_output=True,
        text=True,
        timeout=PIXEL_DRIVER_TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
    )
    if completed.returncode != 0:
        pytest.fail(f"the web pixel failed to run:\n{completed.stderr[-4000:]}")
    return json.loads(completed.stdout)


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


async def _reconciliation(trust_url: str) -> dict[str, Any]:
    """``GET /reconcile`` — the served fold. Reads the chain, appends nothing."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{trust_url}/reconcile")
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def _append(trust_url: str, event: dict[str, Any]) -> httpx.Response:
    """Append one event to the served ledger, the way any producer does."""
    async with httpx.AsyncClient() as client:
        return await client.post(f"{trust_url}/events", json=event)


async def _beacon(merchant_url: str, body: dict[str, Any]) -> httpx.Response:
    """One beacon at the served collector, the way a browser sends it."""
    async with httpx.AsyncClient() as client:
        return await client.post(f"{merchant_url}{COLLECTOR_PATH}", json=body)


def _accepted_event(*, checkout_token: str, total_price: str) -> dict[str, Any]:
    """The exchange's half of the checkout: the promise the reconciler grades against.

    Hand-built here because the exchange is not one of this suite's processes. It carries the
    published ``accepted`` body — ``(bid_ref, checkout_token, offer)`` — and nothing else, so
    the fold has a promise to compare the webhook against. Without one ``reconcile`` emits
    nothing at all and the pixel half would have nothing to be missing FROM.
    """
    return {
        "event_id": f"exchange-accepted-{checkout_token}",
        "ts": "2026-09-06T00:00:00+00:00",
        "kind": "accepted",
        "store_id": DEFAULT_SHOP_DOMAIN,
        "payload": {
            "bid_ref": "bid-pixel-ledger-1",
            "checkout_token": checkout_token,
            "offer": {
                "product_ref": "prod-1",
                "total_price": float(total_price),
                "discount": {"type": "percentage", "value": 0},
            },
        },
    }


# ======================================================================================
# The gate: a real beacon's observation is in another deployable's chained ledger
# ======================================================================================
async def test_a_real_beacon_lands_a_checkout_pixel_row_in_the_chained_ledger(
    node_binary: str,
    merchant_origin: str,
    ledger_trust_wired: str,
) -> None:
    """Link 7b end to end, with nothing in the chain replaced by a double.

    The real web pixel transforms the recorded ``checkout_completed`` event, POSTs it over a
    real socket to the merchant's served collector, and the proof is the trust service's own
    ``GET /events``: a ``checkout_pixel`` row, in a chain that still verifies.

    Rejected here and only here: nothing — this is the positive path. What it pins is that
    the lossy half of R4 leaves the merchant process at all, which it never did.
    """
    event = recorded("web_pixel_checkout_completed.json")["payload"]
    webhook = recorded("webhook_orders_paid.json")["payload"]

    result = drive_the_pixel(node_binary, f"{merchant_origin}{COLLECTOR_PATH}", event)
    assert result["outcome"] == {"delivered": True, "reason": None, "status": 204}, result

    events = await _chain(ledger_trust_wired)
    assert [row["kind"] for row in events] == [PIXEL_KIND], (
        f"the web pixel's observation never reached the trust ledger: {events}"
    )

    row = events[0]
    assert row["payload"]["checkout_token"] == webhook["checkout_token"], (
        "the pixel must file under the token the authoritative webhook carries, or the two "
        "halves of one purchase can never meet"
    )
    propagated = [
        attribute["value"]
        for attribute in webhook["note_attributes"]
        if attribute["name"] == composition.CLIENT_ID_ATTRIBUTE
    ]
    assert row["payload"]["client_id"] == propagated[0], (
        "D24's client_id join key must be the SAME value on both halves; the order webhook "
        "reads it out of note_attributes and the pixel out of the browser event"
    )

    # The published reader, not this file's own spelling of the keys.
    view = join_key_view(row)
    assert view["checkout_token"] == webhook["checkout_token"]
    assert view["client_id"] == propagated[0]

    verified = await _verify(ledger_trust_wired)
    assert verified["ok"] is True, verified
    assert verified["length"] == 1, verified


async def test_every_recorded_beacon_this_repo_has_still_lands_a_joinable_row(
    node_binary: str,
    merchant_origin: str,
    ledger_trust_wired: str,
) -> None:
    """The honest-traffic check on a projection that newly drops fields and newly refuses.

    A projection that publishes an allowlist is a refusal, and the way a refusal goes wrong
    here is silently: honest beacons stop being joinable and every purchase reads
    ``pixel_missing`` again, which is indistinguishable from the defect this change fixed.
    So both recorded surfaces this repository has are driven through the REAL pixel, over a
    real socket, and both must land a row that still carries the checkout token.

    The degraded one is the interesting half: a beacon that left the browser before the order
    existed carries ``orderId: null`` and ``discountApplications: null``, which is the
    documented lossy case (R4) and must be a row WITH its gaps named, not a dropped write.

    Rejected here and only here: nothing. That is the point — nothing honest may be rejected.
    """
    event = recorded("web_pixel_checkout_completed.json")["payload"]
    raced = json.loads(json.dumps(event))
    raced["data"]["checkout"]["order"] = None
    raced["data"]["checkout"]["discountApplications"] = None

    for beacon in (event, raced):
        outcome = drive_the_pixel(node_binary, f"{merchant_origin}{COLLECTOR_PATH}", beacon)
        assert outcome["outcome"]["status"] == 204, outcome

    rows = await _chain(ledger_trust_wired)
    assert [row["kind"] for row in rows] == [PIXEL_KIND, PIXEL_KIND], rows
    token = recorded("webhook_orders_paid.json")["payload"]["checkout_token"]
    assert {row["payload"]["checkout_token"] for row in rows} == {token}, (
        "a beacon that raced order creation is still the same checkout, and must still join"
    )

    complete, degraded = rows
    assert complete["payload"]["gaps"] == []
    assert degraded["payload"]["gaps"] == ["order_ref", "discount_code"], (
        "the lossy beacon's gaps must travel with the observation; a gap the reconciler "
        "cannot see is the same as no gap at all"
    )
    assert set(degraded["payload"]["gaps"]) <= set(GAP_KEYS), (
        "gaps are this repository's fixed vocabulary, never the sender's text"
    )

    verified = await _verify(ledger_trust_wired)
    assert verified["ok"] is True and verified["length"] == 2, verified


async def test_the_ring_still_holds_the_observation_whether_or_not_the_write_lands(
    merchant_origin: str,
    ledger_trust_down: str,
) -> None:
    """The in-process readback is not replaced by the ledger write; it precedes it.

    ``PIXEL_INBOX`` is read by a fresh-process hardening probe, so a refactor that published
    INSTEAD of recording would take that readback away — and a refactor that published FIRST
    would lose the record on every failed write, which is exactly when an operator needs it.

    Rejected here and only here: nothing. What it pins is the ordering.
    """
    response = await _beacon(merchant_origin, {"checkoutToken": "ck-ring-1", "clientId": "cid-1"})
    assert response.status_code == 204, response.text

    (observation,) = PIXEL_INBOX.observations()
    assert observation.checkout_token == "ck-ring-1"
    assert observation.client_id == "cid-1"
    assert composition.ledger_status()["delivered"] == 0, (
        "trust is down in this test; if anything was delivered the wiring is not the one the "
        "container uses"
    )


# ======================================================================================
# The point of the whole thing: the reconciler stops running on two inputs
# ======================================================================================
async def test_a_purchase_reconciles_three_ways_once_the_pixel_is_in_the_chain(
    install_env: dict[str, str],
    install_app_url: str,
    install_inbox: WebhookInbox,
    install_stub: StubClient,
    ledger_trust_wired: str,
) -> None:
    """``pixel_missing`` goes false, over four processes, on served routes only.

    A shopper completes a checkout at the stub. The stub beacons the collector payload at the
    merchant's served ``POST /pixel/collect`` and signs an ``orders/paid`` delivery to the
    merchant's served webhook route — the same ordering a real store has, browser first. Both
    halves publish into a real ``apps/trust``. The exchange's ``accepted`` promise is appended
    through trust's own published door. The verdict is then read off trust's served
    ``GET /reconcile``.

    Rejected here and only here: nothing. What it pins is the three-way join — the reconciler
    was built for three inputs and, with no producer for ``checkout_pixel`` anywhere in the
    tree, had permanently been running on two.
    """
    for topic in ("ORDERS_PAID",):
        path = topic.lower().replace("_", "/")
        subscribed = await install_stub.subscribe(
            topic, f"{install_app_url}{WEBHOOK_PATH_PREFIX}/{path}"
        )
        assert subscribed.status_code == 200, subscribed.text

    configured = await install_stub.configure(
        pixel_collector_url=f"{install_app_url}{COLLECTOR_PATH}"
    )
    assert configured.status_code == 200, configured.text

    completed = await install_stub.buy(SEED_VARIANT["variant_id"])
    token = completed["checkout_token"]

    appended = await _append(
        ledger_trust_wired,
        _accepted_event(checkout_token=token, total_price=completed["total_price"]),
    )
    assert appended.status_code in (200, 201), appended.text

    kinds = sorted(row["kind"] for row in await _chain(ledger_trust_wired))
    assert kinds == ["accepted", "checkout_pixel", "order_paid"], (
        f"the three-way join needs three kinds in the chain and has {kinds}"
    )

    report = await _reconciliation(ledger_trust_wired)
    assert report["reconciled"] == 1, report
    (verdict,) = report["events"]
    assert verdict["payload"]["pixel_missing"] is False, (
        "the purchase still reconciles as pixel_missing, so the pixel half is not in the "
        f"chain or does not join to the webhook: {verdict['payload']}"
    )
    assert verdict["payload"]["order_ref"] == f"gid://shopify/Order/{completed['order_id']}"
    assert verdict["payload"]["checkout_token"] == token

    verified = await _verify(ledger_trust_wired)
    assert verified["ok"] is True, verified


async def test_a_beacon_that_carries_the_total_makes_pixel_agrees_a_live_diagnostic(
    merchant_origin: str,
    ledger_trust_wired: str,
) -> None:
    """``pixel_price`` / ``pixel_agrees`` — the "is this store's integration honest" signal.

    ``reconciled_event`` reads exactly one thing off a pixel: ``total_price``. With no
    producer for ``checkout_pixel`` that read was dead code; with one, it is live the moment
    a beacon carries the total. This drives that the whole way: a beacon with a total through
    the served collector, the promise and the authoritative webhook through trust's own
    published append door, and the verdict off ``GET /reconcile``.

    The ``order_paid`` row here is appended directly rather than driven through a stub
    purchase because the merchant's webhook projection is already gated end to end by
    ``test_webhook_ledger.py``; what is unproven, and what this pins, is that the two rows
    MEET and that the pixel's number reaches the verdict.

    Rejected here and only here: nothing. What it pins is that the diagnostic is not dead.
    """
    token = "ck-agree-1"
    store = "proxyshop-demo.myshopify.com"

    posted = await _beacon(
        merchant_origin, {"checkoutToken": token, "clientId": "cid-agree", "totalPrice": "90.00"}
    )
    assert posted.status_code == 204, posted.text

    for event in (
        _accepted_event(checkout_token=token, total_price="90.00"),
        {
            "event_id": f"merchant-order_paid-{token}",
            "ts": "2026-09-06T00:00:01+00:00",
            "kind": "order_paid",
            "store_id": store,
            "order_ref": "gid://shopify/Order/5500000000042",
            "payload": {
                "checkout_token": token,
                "order_ref": "gid://shopify/Order/5500000000042",
                "total_price": "90.00",
                "current_total_price": "90.00",
                "discount_codes": [],
            },
        },
    ):
        appended = await _append(ledger_trust_wired, event)
        assert appended.status_code in (200, 201), appended.text

    report = await _reconciliation(ledger_trust_wired)
    assert report["reconciled"] == 1, report
    payload = report["events"][0]["payload"]
    assert payload["pixel_missing"] is False, payload
    assert payload["pixel_price"] == 90.0, payload
    assert payload["pixel_agrees"] is True, (
        "the pixel's total reached the verdict and agreed with the authoritative webhook; "
        f"this is the signal that was dead while checkout_pixel had no producer: {payload}"
    )


# ======================================================================================
# The failure path: a shopper's purchase page must never see a 500
# ======================================================================================
async def test_the_collector_answers_204_when_trust_is_down_and_the_loss_is_readable(
    merchant_origin: str,
    ledger_trust_down: str,
) -> None:
    """A trust outage is an operational problem, never a JavaScript error during payment.

    This route answers a beacon fired from a shopper's browser on the merchant's own store.
    A 500 here is a failure on a real customer's purchase page, and it buys nothing: R4
    already makes the ``orders/paid`` webhook authoritative.

    Rejected here and only here: nothing is rejected — that is the assertion. The loss is
    counted and named rather than raised.
    """
    response = await _beacon(
        merchant_origin, {"checkoutToken": "ck-outage-1", "clientId": "cid-outage"}
    )
    assert response.status_code == 204, response.text
    assert response.content == b""

    (observation,) = PIXEL_INBOX.observations()
    assert observation.checkout_token == "ck-outage-1"

    status = composition.ledger_status()
    assert status["delivering"] is False, status
    assert status["lost"] == 1, status
    assert status["delivered"] == 0, status
    assert status["last_failure"], "an outage must be readable after the log line scrolls away"


def test_publishing_a_pixel_observation_never_raises_whatever_it_holds(
    ledger_trust_down: str,
) -> None:
    """Totality over the projection, not only over the transport.

    :meth:`TrustLedgerPublisher.publish` catches every transport failure. It cannot catch a
    projection that trips over a shape no beacon was expected to produce, and that half runs
    on the request path of a live checkout page.

    Rejected here and only here: nothing — every one of these must return, not raise.
    """
    hostile: list[Any] = [
        PixelObservation(checkout_token="ck-1"),
        PixelObservation(checkout_token="ck-2", client_id="cid", total_price=float("inf")),
        PixelObservation(checkout_token="\ud800", client_id="\ud800"),
        PixelObservation(checkout_token="ck-3", gaps=tuple(GAP_KEYS)),
        object(),
        None,
    ]
    for observation in hostile:
        assert composition.publish_pixel_observation(observation) is False, observation


# ======================================================================================
# What the public row may carry
# ======================================================================================
async def test_the_published_row_carries_only_join_keys_and_purchase_facts(
    merchant_origin: str,
    ledger_trust_wired: str,
) -> None:
    """The projection is an allowlist, and this is the list.

    The ledger is append-only, its read door takes no credential, and this beacon runs in a
    shopper's browser. A key that reaches this row is public forever, so the set is pinned
    here rather than left to whatever the projection happens to build.

    Rejected here and only here: every field of the beacon that is not one of the four.
    """
    await _beacon(
        merchant_origin,
        {
            "checkoutToken": "ck-shape-1",
            "clientId": "cid-shape",
            "orderId": "gid://shopify/Order/5500000000001",
            "totalPrice": "90.00",
            "discountApplications": [{"code": "PSX-SHAPE01", "value": 10, "type": "code"}],
            "currency": "USD",
            "timestamp": "2026-09-06T00:00:00Z",
        },
    )

    (row,) = await _chain(ledger_trust_wired)
    assert row["kind"] == PIXEL_KIND
    assert set(row["payload"]) == PUBLISHED_PIXEL_KEYS, row["payload"]
    assert row["payload"]["total_price"] == 90.0
    assert row["payload"]["gaps"] == []

    # The pixel does not name a store: the beacon carries no shop domain and the route takes
    # no credential, so an invented one would be a claim nothing supports.
    assert "store_id" not in row or row["store_id"] is None, row

    # The published shape for this kind is satisfied when the beacon carries the total.
    assert set(LEDGER_PAYLOAD_SHAPES[PIXEL_KIND]) <= set(row["payload"])

    verified = await _verify(ledger_trust_wired)
    assert verified["ok"] is True, verified


async def test_a_hostile_beacon_cannot_merge_two_unrelated_orders(
    merchant_origin: str,
    ledger_trust_wired: str,
) -> None:
    """``order_ref`` is deliberately NOT published off this unauthenticated route.

    ``trust.reconcile.engine`` unions every identifier an event carries, so an event carrying
    a checkout token AND an order reference teaches the join that those two name one order.
    The engine's own comments record what that cost when a *bridge* event was allowed to do
    it: "two reconciled events became one", and the second order's overcharge disappeared
    with no error and no trace.

    This route takes no credential and answers a browser. Publishing ``order_ref`` off it
    would hand anyone who can reach the collector that same merge, for free. Publishing only
    the checkout token bounds the damage to attaching a bogus pixel to one existing group —
    and the pixel is never consulted for a verdict (R4), so that is a diagnostic lie rather
    than an erased reconciliation.

    Rejected here and only here: the beacon's ``orderId``, which the collector still keeps in
    its ring and still refuses to broadcast.
    """
    await _beacon(
        merchant_origin,
        {
            "checkoutToken": "ck-merge-1",
            "orderId": "gid://shopify/Order/9999999999999",
            "clientId": "cid-merge",
        },
    )

    (observation,) = PIXEL_INBOX.observations()
    assert observation.order_ref == "gid://shopify/Order/9999999999999", (
        "the ring keeps what the beacon said; only the ledger projection drops it"
    )

    (row,) = await _chain(ledger_trust_wired)
    assert "order_ref" not in row["payload"], row["payload"]
    assert row.get("order_ref") in (None, ""), row
    assert join_key_view(row)["order_ref"] is None, join_key_view(row)


async def test_the_same_beacon_twice_is_one_row_in_the_chain(
    merchant_origin: str,
    ledger_trust_wired: str,
) -> None:
    """A repeated beacon must not become a second un-evictable row.

    ``keepalive`` fetches can be re-issued, and nothing between a browser and this route
    de-duplicates. The ledger is append-only: a random event id would put one purchase in the
    chain twice, and the chain cannot forget either copy.

    Rejected here and only here: the duplicate append, by the ledger's own idempotency key.
    """
    body = {"checkoutToken": "ck-dup-1", "clientId": "cid-dup", "totalPrice": "12.50"}
    first = await _beacon(merchant_origin, body)
    second = await _beacon(merchant_origin, body)
    assert (first.status_code, second.status_code) == (204, 204)

    events = await _chain(ledger_trust_wired)
    assert [row["kind"] for row in events] == [PIXEL_KIND], events
    assert composition.ledger_status()["delivered"] == 1, (
        "the second append is a 200 Idempotent-Replay, which the publisher does not count as "
        "a delivery"
    )

    # A genuinely DIFFERENT observation of the same checkout is its own row: the beacon that
    # raced order creation and the one that did not are two different facts, and collapsing
    # them would keep whichever arrived first.
    await _beacon(merchant_origin, {**body, "totalPrice": "13.50"})
    assert len(await _chain(ledger_trust_wired)) == 2
