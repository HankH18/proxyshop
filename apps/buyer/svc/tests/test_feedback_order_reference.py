"""R14's prompt, reachable for a REAL purchase — because the order reference is real.

    PROXYSHOP_WORKER=13 .venv/bin/python -m pytest \\
        apps/buyer/svc/tests/test_feedback_order_reference.py -q

THE DEFECT
----------
The shopper journey ended at the checkout handoff. A buyer accepted a slot, was handed the
exchange's permalink and left holding ``auction_id``, ``bid_ref`` and a URL — **and not one of
those is an order**. So ``POST /buyer/feedback`` could only ever be driven with a seeded
reference, and the input to the whole learning loop was a fixture. The journey panel said so in
the product's own words: *"Mounting the component was never the missing piece — the order
reference is."*

WHAT IS REAL IN THIS FILE
-------------------------
Nothing below stands in for a component that exists:

* the accept is the **served** ``POST /auctions/{auction_id}/accept`` on
  ``exchange.main.create_app()``, so the code, the permalink and the ``bid_ref`` are the ones
  the exchange's own ``CheckoutProvider`` minted and its own ledger recorded;
* the purchase is the real ``services/shopify-stub`` on a real loopback socket: the permalink
  is visited, the single-use code is redeemed, the order is created and the ``orders/paid``
  delivery is **signed by the stub and verified by** ``merchant_svc.install.webhooks
  .handle_delivery`` (HMAC-SHA256 over the raw body) before anything reads a field off it;
* the projection is ``merchant_svc.composition.ledger_event``, the exact body the merchant
  service POSTs to trust in production;
* trust is ``trust.main.create_app()`` on a real loopback socket, reached through its own
  published ``POST /events`` and ``GET /reconcile``;
* the buyer service resolves the order by reading that socket, and answers on its own published
  routes.

The two names for one seller are genuinely different on the two halves — the exchange stamps
``store-northroast`` and the merchant writes ``proxyshop-demo.myshopify.com``, because an
unsigned ``X-Shopify-Shop-Domain`` header is the only shop identity a signed delivery carries —
so ``resolve_store_aliases`` is exercised rather than assumed, and
:func:`test_the_two_names_for_one_seller_really_are_different` measures that rather than
quoting it.

WHAT IS DELIBERATELY NOT ASSERTED
---------------------------------
That the *pixel* took part. The stub emits a beacon, this file does not collect one, and the
reconciled verdict therefore records ``pixel_missing``. That is correct and is not the join:
R4 makes the webhook authoritative and the pixel is recorded-never-consulted.

THE CONTROL IS THE POINT
------------------------
Every admission test here has a refusal beside it driven through the *same* door with the same
body shape, so none of them can pass on a service that says yes to everything. The refusal is
the half R14 actually guarantees.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from buyer_svc.feedback.orders import (
    TrustReconciledOrders,
    auction_of,
    find_routed_order,
    order_from_reconciled,
    routed_orders_for,
)
from buyer_svc.feedback.submission import reset_submitted
from buyer_svc.main import create_app as create_buyer_app
from fastapi.testclient import TestClient

from proxyshop_support.asgi_server import serve

#: The PLATFORM's id for the seller — what the exchange stamps onto its ``accepted`` event.
PLATFORM_STORE_ID = "store-northroast"

#: The same seller as the MERCHANT names it. Different on purpose: this is the second half of
#: the join problem, and the alias step is what lets the two meet.
STUB_SHOP_DOMAIN = "proxyshop-demo.myshopify.com"

STORE_DOMAIN = f"{PLATFORM_STORE_ID}.example.com"
WEBHOOK_SECRET = "order-reference-proof-secret"
UNIT_PRICE = 389.0
DISCOUNT_PERCENTAGE = 10.0
AUCTION_ID = "auc-order-reference-proof"
VARIANT_ID = 44352913

#: An order reference of exactly the shape a real one takes, for a purchase this network never
#: made. Every refusal control uses it, so a refusal can never be "the body was malformed".
UNROUTED_ORDER_REF = "gid://shopify/Order/9999999999999"

#: The answer a buyer gives. The choice id is the library's, read from the prompt this service
#: actually served rather than spelled twice.
ANSWER = {"question_id": "matched_pitch", "choice": "yes_as_described"}


# =====================================================================================
# The purchase — driven once, through every real component, for the whole module
# =====================================================================================
def _served_accept() -> dict[str, Any]:
    """Accept one bid over the exchange's own HTTP door; keep what the buyer walks away with.

    The bid id is minted with ``ranking.candidates.mint_bid_id``, the exchange's own function,
    rather than spelled by hand — the whole resolution turns on ``bid_ref`` being
    ``f"{auction_id}:{store_id}"``, and a hand-written literal here would let that stop being
    true without anything going red.
    """
    from exchange.accept.routes import InMemoryAuctionBids, configure_accept
    from exchange.auction.ledger import InMemoryLedgerSink
    from exchange.auction.state import AuctionStateMachine
    from exchange.checkout import StaticRegisteredDomains
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
    from exchange.main import create_app
    from exchange.ranking.candidates import mint_bid_id

    sink = InMemoryLedgerSink()
    machine = AuctionStateMachine(ledger=sink)
    machine.create(AUCTION_ID, intent_id="intent-1", cluster_id="cluster-coffee", roster=[])
    machine.open(AUCTION_ID, now=1_700_000_000.0)
    machine.close(AUCTION_ID, now=1_700_000_001.0)

    bid_id = mint_bid_id(AUCTION_ID, PLATFORM_STORE_ID)
    book = InMemoryAuctionBids()
    book.record(
        AUCTION_ID,
        [
            {
                "bid_id": bid_id,
                "store_id": PLATFORM_STORE_ID,
                "store_domain": STORE_DOMAIN,
                "offer": {
                    "product_ref": "prod-northroast-hx",
                    "unit_price": UNIT_PRICE,
                    "total_price": UNIT_PRICE,
                    "discount": {"type": "percentage", "value": DISCOUNT_PERCENTAGE},
                    "checkout_url": f"https://{STORE_DOMAIN}/cart/{VARIANT_ID}:1",
                    "expires_at": 2_000_000_000.0,
                },
            }
        ],
    )
    app = create_app()
    configure_accept(
        app,
        machine=machine,
        bids=book,
        checkout_mode="redirect",
        registered_domains=StaticRegisteredDomains({PLATFORM_STORE_ID: STORE_DOMAIN}),
        eligibility=StaticSellerEligibility({PLATFORM_STORE_ID: ELIGIBLE}),
    )
    response = TestClient(app).post(f"/auctions/{AUCTION_ID}/accept", json={"bid_ref": bid_id})
    assert response.status_code == 200, response.text
    return {
        "body": response.json(),
        "bid_ref": bid_id,
        "events": {str(event["kind"]): dict(event) for event in sink.for_auction(AUCTION_ID)},
    }


def _permalink_parts(url: str) -> tuple[int, int, str]:
    parts = urlsplit(url)
    variant, _, quantity = parts.path.rsplit("/", 1)[-1].partition(":")
    return int(variant), int(quantity or 1), (parse_qs(parts.query).get("discount") or [""])[0]


async def _redeem_at_the_store(variant_id: int, quantity: int, code: str) -> dict[str, Any]:
    """Redeem the exchange's permalink at the real stub and keep the signed delivery's bytes."""
    from shopify_stub.app import create_app as create_stub
    from shopify_stub.testing import RecordingReceiver, StubClient

    webhooks = RecordingReceiver()
    with serve(create_stub()) as stub_url, serve(webhooks) as webhook_url:
        async with httpx.AsyncClient(base_url=stub_url, follow_redirects=False) as http:
            stub = StubClient(http, stub_url)
            seeded = await stub.seed(
                [
                    {
                        "variant_id": variant_id,
                        "product_id": 8123456,
                        "title": "Heat-exchange espresso machine",
                        "price": f"{UNIT_PRICE:.2f}",
                        "currency": "USD",
                        "sku": "HX-1",
                    }
                ]
            )
            assert seeded.status_code == 200, seeded.text
            await stub.configure(webhook_secret=WEBHOOK_SECRET)
            await stub.subscribe("ORDERS_PAID", f"{webhook_url}/webhooks/shopify")
            created = await stub.create_code(code, percentage=DISCOUNT_PERCENTAGE / 100.0)
            assert created.status_code == 200, created.text
            completion = await stub.buy(variant_id, quantity=quantity, code=code)
    return {"completion": completion, "deliveries": list(webhooks.requests)}


@contextlib.contextmanager
def _served_trust(**state: Any) -> Iterator[str]:
    """The real trust application on a real socket, with an in-memory chain behind it."""
    from trust.events.store import InMemoryEventStore
    from trust.main import create_app

    app = create_app()
    app.state.event_store = InMemoryEventStore()
    for name, value in state.items():
        setattr(app.state, name, value)
    with serve(app) as url:
        yield url


@pytest.fixture(scope="module")
def purchase() -> Iterator[dict[str, Any]]:
    """One real purchase, with a live trust service holding its chain.

    Module-scoped because it starts three servers and completes a checkout. The trust service
    stays up for the whole module so every test below reads the same chain the purchase
    actually produced, over a socket, rather than a copy of it.
    """
    from merchant_svc.composition import ledger_event
    from merchant_svc.install.webhooks import handle_delivery, ledger_record

    accept = _served_accept()
    variant_id, quantity, code = _permalink_parts(accept["body"]["permalink_url"])
    store = asyncio.run(_redeem_at_the_store(variant_id, quantity, code))

    delivery = store["deliveries"][0]
    decision = handle_delivery(
        body=delivery["body"], headers=delivery["headers"], secret=WEBHOOK_SECRET
    )
    assert decision.accepted, f"{decision.status_code} {decision.reason}"
    order_paid = ledger_event(ledger_record(decision.event))

    with _served_trust(reconcile_store_aliases={STUB_SHOP_DOMAIN: PLATFORM_STORE_ID}) as trust_url:
        with httpx.Client(base_url=trust_url, timeout=30.0) as trust:
            for event in (
                accept["events"]["accepted"],
                accept["events"]["code_created"],
                accept["events"]["checkout_redirect"],
                order_paid,
            ):
                landed = trust.post("/events", json=dict(event))
                assert landed.status_code in (200, 201), f"{event['kind']}: {landed.text}"
            fold = trust.get("/reconcile").json()
        yield {
            "accept": accept,
            "code": code,
            "order_paid": order_paid,
            "fold": fold,
            "trust_url": trust_url,
        }


@pytest.fixture
def buyer(purchase: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The buyer service, pointed at the trust service holding this purchase's chain.

    ``TRUST_URL`` and nothing else: no resolver is injected, so what is exercised is the
    deployment path :func:`buyer_svc.feedback.orders.routed_orders_for` takes when a compose
    stack forwards that one variable — which is what the shipped stack does.
    """
    monkeypatch.setenv("TRUST_URL", purchase["trust_url"])
    reset_submitted()
    try:
        yield TestClient(create_buyer_app())
    finally:
        reset_submitted()


# =====================================================================================
# The premise, measured on this run rather than quoted
# =====================================================================================
def test_the_two_names_for_one_seller_really_are_different(purchase: dict[str, Any]) -> None:
    """The alias step is load-bearing here, not decorative.

    If these two ever became the same string the join would succeed for a reason this test is
    not about, and every admission below would stop measuring the thing it claims to.
    """
    accepted = purchase["accept"]["events"]["accepted"]
    order_paid = purchase["order_paid"]

    assert accepted["store_id"] == PLATFORM_STORE_ID
    assert order_paid["store_id"] == STUB_SHOP_DOMAIN
    assert purchase["fold"]["store_aliases_applied"] >= 1, purchase["fold"]


def test_the_order_reference_comes_from_the_signed_webhook_and_nowhere_earlier(
    purchase: dict[str, Any],
) -> None:
    """The accept mints no order reference, and the webhook is where one first exists.

    This is the constraint the whole design turns on stated as an assertion: the body the
    buyer gets at accept carries a permalink and a discount code, and **no order**. A code
    minted before a purchase is not evidence a purchase happened.
    """
    accept_body = purchase["accept"]["body"]

    assert sorted(accept_body) == ["code", "notice", "permalink_url"], accept_body
    assert accept_body["code"].startswith("PSX-")
    assert purchase["order_paid"]["order_ref"].startswith("gid://shopify/Order/")
    # ...and the two halves share no checkout token, so the code really is the only join.
    exchange_token = purchase["accept"]["events"]["accepted"]["payload"]["checkout_token"]
    assert exchange_token != purchase["order_paid"]["payload"]["checkout_token"]


def test_the_fold_produced_exactly_one_reconciled_order(purchase: dict[str, Any]) -> None:
    """One purchase, one verdict, and it names this auction's bid."""
    fold = purchase["fold"]

    assert fold["reconciled"] == 1, fold
    assert fold["unjoinable_webhooks"] == 0, fold
    verdict = fold["events"][0]
    assert verdict["order_ref"] == purchase["order_paid"]["order_ref"]
    assert verdict["payload"]["bid_ref"] == purchase["accept"]["bid_ref"]


# =====================================================================================
# The deliverable: the buyer ends up holding a reference that identifies THAT order
# =====================================================================================
def test_the_buyer_obtains_the_reference_for_the_order_it_actually_bought(
    buyer: TestClient, purchase: dict[str, Any]
) -> None:
    """``POST /buyer/feedback/order`` answers with the merchant's own order gid.

    The request carries only what ``POST /buyer/shortlist/accept`` already published to this
    buyer — ``auction_id`` and ``bid_ref``. Nothing was minted at accept for the purpose and
    nothing is invented here: the reference comes back out of a fold over a chain whose
    ``order_paid`` row was HMAC-verified before it was written.
    """
    response = buyer.post(
        "/buyer/feedback/order",
        json={"auction_id": AUCTION_ID, "bid_ref": purchase["accept"]["bid_ref"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["found"] is True, body
    assert body["order"] == {
        "order_ref": purchase["order_paid"]["order_ref"],
        "store_id": PLATFORM_STORE_ID,
        "auction_id": AUCTION_ID,
        "bid_ref": purchase["accept"]["bid_ref"],
        "routed": True,
    }


def test_the_control_no_reference_exists_for_a_checkout_the_network_did_not_route(
    buyer: TestClient,
) -> None:
    """The same door, an auction that never happened: 200, ``found: false``, and a reason.

    Not a 404 and not a 500 — "has my order come through yet?" has two correct answers — and
    critically not a reference. A lookup that manufactured one here would be the defect this
    ticket exists to refuse, wearing a different hat.
    """
    response = buyer.post("/buyer/feedback/order", json={"auction_id": "auc-never-happened"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["found"] is False and body["order"] is None, body
    assert "reconciled" in body["reason"]


def test_the_real_prompt_is_offered_for_that_reference(
    buyer: TestClient, purchase: dict[str, Any]
) -> None:
    """R14's one question, for a real order, with no routing claim in the request.

    The body asserts nothing about itself — no ``routed``, no ``auction_id``. The only reason
    a prompt comes back is that trust's fold vouches for this reference, and the prompt names
    the auction the network resolved rather than one the caller supplied.
    """
    order_ref = purchase["order_paid"]["order_ref"]

    response = buyer.post("/buyer/feedback/prompt", json={"order": {"order_ref": order_ref}})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["offered"] is True, body
    prompt = body["prompt"]
    assert prompt["order_ref"] == order_ref
    assert prompt["auction_id"] == AUCTION_ID
    assert prompt["store_id"] == PLATFORM_STORE_ID
    assert prompt["question_id"] == "matched_pitch"
    assert [option["id"] for option in prompt["options"]], prompt


def test_the_control_the_same_prompt_is_refused_for_an_order_the_network_did_not_route(
    buyer: TestClient,
) -> None:
    """The identical request shape, a reference nothing vouches for: ``offered: false``.

    This is R14's negative guarantee. The refusal is the pre-existing one, in the pre-existing
    words, from the pre-existing decider — what changed is that the evidence it judges is now
    the network's rather than the caller's.
    """
    response = buyer.post(
        "/buyer/feedback/prompt", json={"order": {"order_ref": UNROUTED_ORDER_REF}}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["offered"] is False and body["prompt"] is None, body
    assert "network-routed" in body["reason"]


def test_the_real_feedback_route_records_an_answer_for_that_order(
    buyer: TestClient, purchase: dict[str, Any]
) -> None:
    """``POST /buyer/feedback`` — 201, and the event names the order that was really bought."""
    order_ref = purchase["order_paid"]["order_ref"]

    response = buyer.post(
        "/buyer/feedback", json={"order": {"order_ref": order_ref}, "response": ANSWER}
    )

    assert response.status_code == 201, response.text
    event = response.json()
    assert event["kind"] == "feedback"
    assert event["order_ref"] == order_ref
    assert event["auction_id"] == AUCTION_ID
    assert event["store_id"] == PLATFORM_STORE_ID
    assert event["matched_pitch"] is True
    assert event["reason"] == ANSWER["choice"]


def test_the_control_the_same_answer_is_refused_for_an_order_the_network_did_not_route(
    buyer: TestClient,
) -> None:
    """403 and nothing recorded. Same body shape, same route, one field of evidence different."""
    response = buyer.post(
        "/buyer/feedback",
        json={"order": {"order_ref": UNROUTED_ORDER_REF}, "response": ANSWER},
    )

    assert response.status_code == 403, response.text
    assert "network-routed" in json.dumps(response.json())


def test_the_earned_reference_lands_inside_the_trust_chains_hash(
    buyer: TestClient, purchase: dict[str, Any]
) -> None:
    """How an earned reference stays distinguishable from a seeded one, measured end to end.

    ``services/sim/seed/provenance.py`` puts the seeded/earned marker on ``order_ref``
    precisely because ``submit_feedback`` copies that field verbatim onto the ``LedgerEvent``
    and ``trust.ledger.canonical.compute_event_hash`` folds it into the chain — so *"a seeded
    observation cannot be un-marked without breaking ``GET /events/verify``, and a real
    observation cannot be marked without the same break."*

    This drives the earned half of that sentence: the reference the network vouched for
    reaches the chain under the same field, the chain still verifies, and the stored event is
    the merchant's order gid rather than a ``sim-fb-`` reference. Nothing in the buyer service
    tests a prefix — the separation is the hash, plus the fact that no ``reconciled`` record
    exists behind a seeded reference for the network to vouch with.
    """
    order_ref = purchase["order_paid"]["order_ref"]
    submitted = buyer.post(
        "/buyer/feedback", json={"order": {"order_ref": order_ref}, "response": ANSWER}
    )
    assert submitted.status_code == 201, submitted.text
    event_id = submitted.json()["event_id"]

    with httpx.Client(base_url=purchase["trust_url"], timeout=30.0) as trust:
        stored = trust.get(f"/events/{event_id}")
        assert stored.status_code == 200, stored.text
        verified = trust.get("/events/verify")
        assert verified.status_code == 200, verified.text

    body = stored.json()
    event = body.get("event", body)
    assert event["order_ref"] == order_ref
    assert not event["order_ref"].startswith("sim-fb-")
    assert event["event_hash"], event
    assert verified.json().get("ok") is True, verified.json()


# =====================================================================================
# The resolver's own rules — the ways it must refuse to answer
# =====================================================================================
def test_the_auction_is_recovered_from_the_networks_own_bid_reference() -> None:
    """``mint_bid_id`` is ``f"{auction_id}:{store_id}"``, so the store is stripped, not split on.

    Splitting on the first colon would truncate a ``gid://``-shaped auction id; splitting on
    the last would keep a colon belonging to the store. Both readings are driven here.
    """
    assert auction_of("auc-1:store-a", "store-a") == "auc-1"
    assert auction_of("gid://x/1:store-a", "store-a") == "gid://x/1"
    assert auction_of("auc-1:store:a", "store:a") == "auc-1"
    # No separator at all is not an auction, and an empty answer is what `routing()` refuses.
    assert auction_of("auc-1", "store-a") == ""
    assert auction_of("", "") == ""
    assert auction_of(None, None) == ""


def _verdict(**overrides: Any) -> dict[str, Any]:
    event = {
        "kind": "reconciled",
        "store_id": "store-a",
        "order_ref": "gid://shopify/Order/1",
        "payload": {"bid_ref": "auc-1:store-a", "order_ref": "gid://shopify/Order/1"},
    }
    event.update(overrides)
    return event


def test_only_a_reconciled_record_naming_an_order_and_a_bid_becomes_an_order() -> None:
    """Every field the gate reads must be the network's, so a record missing one answers None."""
    assert order_from_reconciled(_verdict())["routed"] is True
    assert order_from_reconciled(_verdict(kind="order_paid")) is None
    assert (
        order_from_reconciled(_verdict(order_ref="", payload={"bid_ref": "auc-1:store-a"})) is None
    )
    assert order_from_reconciled(_verdict(payload={})) is None
    # A `bid_ref` naming no auction is refused rather than resolved: `routing()` will not
    # attribute a trust observation to nothing, and neither will this.
    assert order_from_reconciled(_verdict(payload={"bid_ref": "bid-a"})) is None
    for junk in (None, "reconciled", 7, [], object()):
        assert order_from_reconciled(junk) is None


def test_every_hint_must_match_and_ambiguity_is_refused() -> None:
    """``and``, never ``or`` — and two candidates are no answer at all.

    A lookup that matched *any* hint would let a caller who knows one real reference collect a
    different auction's prompt by naming both. A lookup that picked the first of two matches
    would attribute a trust observation to a coin toss.
    """
    mine = _verdict()
    theirs = _verdict(
        order_ref="gid://shopify/Order/2",
        payload={"bid_ref": "auc-2:store-a", "order_ref": "gid://shopify/Order/2"},
    )

    assert find_routed_order([mine, theirs], auction_id="auc-1")["order_ref"] == mine["order_ref"]
    assert (
        find_routed_order([mine, theirs], order_ref=mine["order_ref"], auction_id="auc-2") is None
    )
    assert (
        find_routed_order([mine, theirs], bid_ref="auc-2:store-a")["order_ref"]
        == "gid://shopify/Order/2"
    )
    # Two records the hints cannot tell apart: no answer.
    assert find_routed_order([mine, _verdict(store_id="store-b")], auction_id="auc-1") is None
    # No hint at all is not "give me anything".
    assert find_routed_order([mine]) is None


def test_a_trust_service_that_cannot_be_read_vouches_for_nothing_and_never_raises() -> None:
    """An audit service being down is an absent vouching, not a 500 on the shopper's prompt."""
    unreachable = TrustReconciledOrders("http://127.0.0.1:1", timeout=0.05)

    assert unreachable.reconciled() == []
    assert unreachable.order_for(auction_id="auc-1") is None


def test_a_deployment_with_no_trust_service_is_left_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``TRUST_URL``, no resolver, and pointedly no invented compose hostname.

    The fallback :mod:`proxyshop_support.trust_ledger` takes for a WRITE is the wrong trade for
    a read on a request path: it would spend a shopper's request on a DNS failure to reach a
    service this deployment never had.
    """
    monkeypatch.delenv("TRUST_URL", raising=False)
    app = create_buyer_app()

    assert routed_orders_for(app) is None
    # Cached, so a deployment with no trust service pays for one failed build, not one per
    # request — and a second call cannot start returning something different.
    assert routed_orders_for(app) is None


def test_a_resolver_a_composition_root_wired_is_never_replaced() -> None:
    """``app.state.routed_orders`` outranks the environment, so a double can be injected."""
    app = create_buyer_app()
    injected = object()
    app.state.routed_orders = injected

    assert routed_orders_for(app, env={"TRUST_URL": "http://elsewhere:8084"}) is injected


def test_a_record_that_already_claims_routing_is_left_untouched(
    buyer: TestClient, purchase: dict[str, Any]
) -> None:
    """The pre-existing path is unchanged, which is what keeps the seeded corpus working.

    ``services/sim/seed`` submits its marked answers through this same real route with a
    record that asserts its own routing. This asserts the network is not consulted for such a
    record and cannot overwrite it: the auction here is one the network has never heard of,
    and the answer is still recorded — exactly as it was before this lane touched the file.
    """
    response = buyer.post(
        "/buyer/feedback",
        json={
            "order": {
                "order_ref": "sim-fb-01-01-store-brightbean",
                "store_id": "store-brightbean",
                "auction_id": "sim-fb-auc-0001",
                "routed": True,
            },
            "response": ANSWER,
        },
    )

    assert response.status_code == 201, response.text
    event = response.json()
    assert event["order_ref"] == "sim-fb-01-01-store-brightbean"
    assert event["auction_id"] == "sim-fb-auc-0001"
    # ...and the real purchase's reference did not leak into it.
    assert event["order_ref"] != purchase["order_paid"]["order_ref"]
