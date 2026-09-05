"""The ``accepted`` event carries the join key reconciliation needs (runbook 3.6/3.7).

``apps/exchange/src/auction/ledger.py`` used to file this beside two genuinely cosmetic
omissions — ``auction_opened`` without ``roster_size``, ``auction_closed`` without
``shortlist_size`` — as "an audit-record defect that is nobody's ticket here". It is not the
same kind of thing. ``apps/trust/src/reconcile/engine.py`` builds a checkout's join keys from
``payload['checkout_token']`` first and **drops any event that carries none**, so an
``accepted`` event without it never reaches the group its ``order_paid`` webhook is in:

    for event in events:
        keys = _join_keys(event)
        if not keys:
            ...
            continue          # <- the accepted offer, silently, every time

and ``reconcile`` emits only for a group holding BOTH a webhook and an accepted offer. The
missing key is therefore not a blemish on an audit record; it is the reason sections 3.6 and
3.7 of the runbook produce nothing at all.

Every test here pairs with a **control** that removes the key again and shows the join
collapse, so none of them can pass on a reconciler that would have joined anyway.

What this file does NOT claim: that the deployed loop closes. The exchange mints
``checkout_token`` with ``secrets.token_hex(16)`` *after* the merchant has already been
called (``checkout/provider.py``), and nothing transmits it, so the merchant's own
``orders/paid`` webhook carries an unrelated token of its own. The webhooks below are
therefore written by this test, and they are labelled as such — they demonstrate that the
exchange's half of the join is now present and correct, not that the two halves meet.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from contracts.ledger import validate_ledger_payload
from exchange.auction.state import AuctionStateMachine
from exchange.checkout import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from fastapi.testclient import TestClient
from trust.reconcile.engine import reconcile

from .test_accept_routes import (  # the wiring this path is already tested through
    PLATFORM_DOMAINS,
    RecordingCodeCreator,
    honest_bid,
)
from exchange.accept.routes import InMemoryAuctionBids, configure_accept
from exchange.accept import use_registered_domains

#: What ``secrets.token_hex(16)`` produces, and the only thing a real minted token can look
#: like. Asserted rather than merely "is not None", so a route that wrote the string
#: ``"None"`` or an empty placeholder into the event would still fail.
MINTED_TOKEN = re.compile(r"\A[0-9a-f]{32}\Z")

OFFER = {
    "product_ref": "product-1",
    "unit_price": 100.0,
    "total_price": 100.0,
    "discount": {"type": "percentage", "value": 10.0},
}


@pytest.fixture
def unwired() -> Any:
    """Leave the process-wide registered-domain registry exactly as this test found it."""
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


def paid_webhook(checkout_token: Any, *, total_price: float = 90.0) -> dict[str, Any]:
    """An ``order_paid`` ledger event in the shape the reconciler reads.

    Written by this test on purpose — see the module docstring. It is the *counterparty*, not
    the thing under test: what is being measured is whether the exchange's ``accepted`` event
    can be joined to a webhook that names the same checkout, never whether the merchant
    currently names it.
    """
    return {
        "event_id": "order_paid:1",
        "ts": "2026-01-01T00:00:00+00:00",
        "kind": "order_paid",
        "store_id": "store-a",
        "order_ref": "gid://shopify/Order/5500000000001",
        "payload": {
            "checkout_token": checkout_token,
            "order_ref": "gid://shopify/Order/5500000000001",
            "total_price": total_price,
            "discountApplications": [{"type": "percentage", "value": 10.0}],
        },
    }


def accepted_from_the_machine(**kwargs: Any) -> dict[str, Any]:
    """Drive a real auction to ``accepted`` and hand back the event the ledger got."""
    machine = AuctionStateMachine()
    machine.create("auction-1", intent_id="intent-1", cluster_id="cluster-1", roster=[])
    machine.open("auction-1", now=1_700_000_000.0)
    machine.close("auction-1", now=1_700_000_001.0)
    machine.accept("auction-1", "bid-a", now=1_700_000_002.0, **kwargs)
    events = [event for event in machine.ledger.sink.events if event["kind"] == "accepted"]
    assert len(events) == 1, f"expected one accepted event, got {machine.ledger.sink.kinds}"
    return events[0]


def without(event: dict[str, Any], key: str) -> dict[str, Any]:
    """The same event with one payload key taken back out — the control."""
    stripped = {**event, "payload": {k: v for k, v in event["payload"].items() if k != key}}
    assert key not in stripped["payload"]
    return stripped


# =====================================================================================
# the event body
# =====================================================================================
def test_the_accepted_event_carries_the_checkout_token_and_the_offer() -> None:
    token = "b1946ac92492d2347c6235b4d2611184"
    event = accepted_from_the_machine(checkout_token=token, offer=OFFER)

    assert event["payload"]["checkout_token"] == token
    assert event["payload"]["offer"] == OFFER
    assert event["payload"]["bid_ref"] == "bid-a"


def test_the_accepted_body_is_the_one_contracts_publishes() -> None:
    """``LEDGER_PAYLOAD_SHAPES['accepted'] == ('bid_ref', 'checkout_token', 'offer')``.

    The state machine writes through ``build_event``, which checks only the *kind*, so this
    is the assertion that keeps the body honest. Before the fix it read::

        ["'accepted' payload is missing published key 'checkout_token'",
         "'accepted' payload is missing published key 'offer'"]
    """
    event = accepted_from_the_machine(checkout_token="a" * 32, offer=OFFER)
    assert validate_ledger_payload("accepted", event["payload"]) == []


def test_a_caller_with_no_token_still_accepts_and_still_writes_both_keys() -> None:
    """The simulator and any direct driver must keep working; the keys are present as None.

    An absent key and a key that is present and empty are different facts, and only the
    second one is readable. This is also why the fix is not a validation error: an auction
    that cannot record its acceptance cannot refuse the second accept either.
    """
    event = accepted_from_the_machine()
    assert event["payload"]["checkout_token"] is None
    assert event["payload"]["offer"] is None
    assert validate_ledger_payload("accepted", event["payload"]) == []


# =====================================================================================
# the join — each with its control
# =====================================================================================
def test_reconcile_joins_the_accepted_offer_to_the_webhook_that_names_it() -> None:
    token = "b1946ac92492d2347c6235b4d2611184"
    accepted = accepted_from_the_machine(checkout_token=token, offer=OFFER)

    emitted = reconcile([accepted, paid_webhook(token)])

    assert len(emitted) == 1, "the accepted offer and its webhook did not reach one group"
    payload = emitted[0]["payload"]
    assert payload["checkout_token"] == token
    assert payload["bid_ref"] == "bid-a"
    assert payload["price_honored"] is True and payload["price_comparable"] is True
    assert payload["discount_honored"] is True and payload["discount_comparable"] is True


def test_control_without_the_token_the_same_pair_reconciles_to_nothing() -> None:
    """Remove the field the fix added and the join collapses. This is the defect itself.

    Not "the verdict changes" — the reconciler emits *no event at all*, because an accepted
    offer carrying no join key is dropped before grouping and a group with no accepted offer
    has no promise to grade.
    """
    token = "b1946ac92492d2347c6235b4d2611184"
    accepted = accepted_from_the_machine(checkout_token=token, offer=OFFER)

    assert reconcile([without(accepted, "checkout_token"), paid_webhook(token)]) == []


def test_control_without_the_offer_the_promise_cannot_be_graded() -> None:
    """The second half of the published body. The join survives; the *comparison* does not.

    A reconciled event whose ``price_comparable`` is False translates to ``unsupported``
    (0.5) rather than ``contradicted`` (2.0), so dropping the offer does not merely lose
    information — it silently downgrades every finding to "the webhook did not say".
    """
    token = "b1946ac92492d2347c6235b4d2611184"
    accepted = accepted_from_the_machine(checkout_token=token, offer=OFFER)

    graded = reconcile([accepted, paid_webhook(token)])[0]["payload"]
    ungraded = reconcile([without(accepted, "offer"), paid_webhook(token)])[0]["payload"]

    assert graded["price_comparable"] is True and graded["promised_price"] == 100.0
    assert ungraded["price_comparable"] is False and ungraded["promised_price"] is None


def test_an_overcharge_is_caught_only_because_the_offer_travelled() -> None:
    """The point of the whole join: the webhook says 130 against a promise of 100."""
    token = "b1946ac92492d2347c6235b4d2611184"
    accepted = accepted_from_the_machine(checkout_token=token, offer=OFFER)

    payload = reconcile([accepted, paid_webhook(token, total_price=130.0)])[0]["payload"]

    assert payload["price_comparable"] is True
    assert payload["price_honored"] is False
    assert payload["observed_price"] == 130.0 and payload["promised_price"] == 100.0


# =====================================================================================
# the served route, not just the function
# =====================================================================================
def served_accept(unwired_marker: None) -> dict[str, Any]:
    """Accept a bid over HTTP and hand back the ``accepted`` event the ledger recorded."""
    machine = AuctionStateMachine()
    auction_id = "auction-route-join"
    machine.create(auction_id, intent_id="intent-1", cluster_id="cluster-1", roster=[])
    machine.open(auction_id, now=1_700_000_000.0)
    machine.close(auction_id, now=1_700_000_001.0)
    book = InMemoryAuctionBids()
    book.record(auction_id, [honest_bid()])
    app = create_app()
    configure_accept(
        app,
        machine=machine,
        bids=book,
        code_creator=RecordingCodeCreator(),
        checkout_mode="shopify",
        registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS),
        eligibility=StaticSellerEligibility({"store-a": ELIGIBLE, "store-b": ELIGIBLE}),
    )
    response = TestClient(app).post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-a"})
    assert response.status_code == 200, response.text
    events = [event for event in machine.ledger.sink.events if event["kind"] == "accepted"]
    assert len(events) == 1, f"expected one accepted event, got {machine.ledger.sink.kinds}"
    return events[0]


def test_the_served_accept_records_the_token_the_checkout_actually_minted(
    unwired: None,
) -> None:
    """A unit test that passes the token in cannot show the ROUTE passing it in.

    The route is the only production caller of ``AuctionStateMachine.accept``, and before the
    fix it called it with ``(auction_id, bid_ref, now=now)`` — so the machine could have
    accepted the keyword forever and the served path would still have written nothing.
    """
    event = served_accept(unwired)
    token = event["payload"]["checkout_token"]

    assert isinstance(token, str) and MINTED_TOKEN.match(token), (
        f"the served accept wrote {token!r} rather than a minted checkout token"
    )
    assert event["payload"]["offer"]["total_price"] == 100.0
    assert event["payload"]["offer"]["product_ref"] == "product-1"
    assert validate_ledger_payload("accepted", event["payload"]) == []


def test_the_served_accepts_own_event_reconciles_against_a_webhook_naming_it(
    unwired: None,
) -> None:
    """End of the exchange's half: the event a real HTTP accept wrote is joinable as-is."""
    event = served_accept(unwired)
    token = event["payload"]["checkout_token"]

    emitted = reconcile([event, paid_webhook(token)])

    assert len(emitted) == 1
    assert emitted[0]["payload"]["checkout_token"] == token
    assert emitted[0]["payload"]["price_honored"] is True

    # The control, on the served event rather than a hand-built one.
    assert reconcile([without(event, "checkout_token"), paid_webhook(token)]) == []
