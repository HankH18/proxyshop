"""The served accept files the whole C11 trio in the ledger, not only ``accepted``.

WHAT WAS MEASURED
-----------------
``CheckoutProvider.checkout`` really does build all three of
:data:`~exchange.checkout.CHECKOUT_EVENT_KINDS` — ``CheckoutResult.events`` carries
``accepted``, ``code_created`` and ``checkout_redirect``, and
``test_repro_ledger_gates.py`` asserts that trio at the port. ``exchange.accept.routes``
then read exactly ONE of them (``_accepted_offer`` reads the ``accepted`` one for its offer
body) and dropped the other two on the floor, so a served accept put three events on the
wire inside the process and one in the ledger. Driven end to end, a real demo chain was five
events with not one bridge among them.

WHY THE TWO IT DROPPED ARE THE ONES THAT MATTER
-----------------------------------------------
The two checkout tokens in a real Shopify checkout are different values and always were:
the exchange mints its ``checkout_token`` with ``secrets.token_hex(16)`` *after* the merchant
has been called and transmits it nowhere, while the store mints its own when the cart is
visited and *that* is the token that rides onto ``orders/paid``. So
``trust.reconcile.engine.reconcile`` cannot join the two halves of a purchase on the token.
It bridges them on the single-use discount code instead — the one value that genuinely
crossed the wire — and it reads that code off exactly
:data:`~trust.reconcile.engine.CODE_BRIDGE_KINDS`, which is ``code_created`` and
``checkout_redirect``: the two events the route was dropping. An ``accepted`` event and its
``order_paid`` webhook with no bridge between them reconcile to **nothing at all**, which is
what :func:`test_control_without_the_bridge_the_same_purchase_reconciles_to_nothing`
measures rather than asserts.

Every join test here therefore carries its control, so none of them can pass on a reconciler
that would have joined anyway.

THE STORE NAME, WHICH IS A SEPARATE JOIN BLOCKER AND IS NOT THE EXCHANGE'S TO FIX
---------------------------------------------------------------------------------
The exchange stamps its platform ``store_id`` (``store-a``); ``merchant_svc.composition``
writes the shop domain, because an unsigned ``X-Shopify-Shop-Domain`` header is the only
shop identity a signed delivery carries at all. ``reconcile`` namespaces every join key by
store — it must, a Shopify ``order_id`` is a per-shop number — so an offer under one name
and an order under the other never meet however good the code bridge is.

That is closed on the TRUST side and it is closed already:
:func:`trust.reconcile.routes.resolve_store_aliases` maps a seller's registered domain onto
its ``store_id`` off the platform's own roster, and ``POST``/``GET /reconcile`` apply it.
:func:`test_the_store_alias_is_what_lets_the_two_names_meet` is the measurement of that,
kept here because it is the seam between what this route emits and what that fold reads —
and because it is the reason the exchange must keep emitting the platform's name rather than
inventing a domain to match the merchant's.

THE ONE BLOCKER FORWARDING THE BRIDGE DOES NOT CLOSE, MEASURED HERE
--------------------------------------------------------------------
``reconcile`` scopes every join key — the code key included — by the store the event names,
and the ``accepted`` event the ledger receives names **no store at all**:
``AuctionStateMachine._transition`` calls ``self.ledger.record(kind, auction_id=…,
payload=…)`` and passes no ``store_id``, so ``apps/exchange/src/auction/state.py`` has no
occurrence of that field anywhere. The auction is not the wrong place for that to be missing
— an auction has a roster of many stores and only the accept knows the winner — but the
consequence is exact: the offer lands in the unattributed scope while its own bridges land
under ``store-a``, and the code that joins them can only link keys inside one scope.
Measured on this route's real output, ``reconcile`` returns 0 with the served
``accepted`` and 1 with the same event carrying ``store_id``.

So the events below are attributed by :func:`attributed`, which is labelled where it is used
and is NOT a convenience: it stands in for the one field the exchange still does not put on
that envelope. :func:`test_the_join_needs_the_offer_to_name_the_store_its_order_names`
measures that rule directly, so it keeps saying something true whether or not the state
machine is ever given the store to stamp.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from exchange.accept import use_registered_domains
from exchange.accept.routes import InMemoryAuctionBids, configure_accept
from exchange.auction.ledger import InMemoryLedgerSink
from exchange.auction.state import AuctionStateMachine
from exchange.checkout import CHECKOUT_EVENT_KINDS, StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from fastapi.testclient import TestClient
from trust.reconcile.engine import CODE_BRIDGE_KINDS, reconcile
from trust.reconcile.routes import resolve_store_aliases

from .test_accept_routes import (  # the wiring this path is already tested through
    PLATFORM_DOMAINS,
    RecordingCodeCreator,
    honest_bid,
)

#: The shop domain a real ``orders/paid`` delivery would name for ``store-a`` — the merchant's
#: half of the name mismatch. Deliberately not ``PLATFORM_DOMAINS['store-a']``: the whole
#: point is that the two sides spell the same seller differently.
SHOP_DOMAIN = "store-a.myshopify.com"

#: What the merchant's own checkout mints. Unrelated to the exchange's token on purpose.
MERCHANT_TOKEN = "91a1ed79fdb24f61b3e64c91e030b6e9"

ORDER_REF = "gid://shopify/Order/5500000000001"


@pytest.fixture
def unwired() -> Iterator[None]:
    """Leave the process-wide registered-domain registry exactly as this test found it."""
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


def paid_webhook(code: str, *, store_id: str, total_price: float = 100.0) -> dict[str, Any]:
    """An ``order_paid`` event in the shape ``merchant_svc.composition`` projects one into.

    Written by this test because the merchant is a different service; what is under test is
    whether the EXCHANGE's half of the bridge is on the wire. Two properties are faithful to
    the measured article and both are load-bearing: it carries the merchant's OWN checkout
    token (so nothing can join on the exchange's), and it names the discount code only in the
    ``discount_codes`` list, which is where a real ``orders/paid`` body puts it.
    """
    return {
        "event_id": "merchant:order_paid:1",
        "ts": "2026-01-01T00:00:10+00:00",
        "kind": "order_paid",
        "store_id": store_id,
        "order_ref": ORDER_REF,
        "payload": {
            "checkout_token": MERCHANT_TOKEN,
            "order_ref": ORDER_REF,
            "total_price": total_price,
            "discount_codes": [{"code": code}],
        },
    }


def attributed(event: dict[str, Any], store_id: str = "store-a") -> dict[str, Any]:
    """The same event with the store named on its envelope.

    Used on the ``accepted`` event only, and it stands in for the one thing the exchange
    still does not emit — see this module's last docstring section. It is applied by the
    tests that are measuring the CODE bridge, so that the store-scope blocker is held
    constant instead of hiding whether the bridge works.
    """
    return {**event, "store_id": store_id}


def with_offer_attributed(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``chain`` with only its ``accepted`` event attributed; the bridges already are."""
    return [attributed(e) if str(e["kind"]) == "accepted" else e for e in chain]


def served_accept() -> tuple[InMemoryLedgerSink, str]:
    """Accept a bid over HTTP; hand back the machine whose ledger recorded the run.

    The route is the only production caller of ``AuctionStateMachine.accept`` and the only
    place ``CheckoutResult.events`` is ever seen, so the events are read off the sink the
    deployment wired rather than off the port's return value: a port that files a perfect
    trio into a variable nobody reads is exactly the defect.
    """
    sink = InMemoryLedgerSink()
    machine = AuctionStateMachine(ledger=sink)
    auction_id = "auction-bridge"
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
    return sink, auction_id


def checkout_events(sink: InMemoryLedgerSink, auction_id: str) -> list[dict[str, Any]]:
    """The C11 events the ledger sink actually received, in the order it received them."""
    return [
        event
        for event in sink.for_auction(auction_id)
        if str(event["kind"]) in CHECKOUT_EVENT_KINDS
    ]


# =====================================================================================
# what reaches the ledger
# =====================================================================================
def test_the_served_accept_files_the_whole_checkout_trio(unwired: None) -> None:
    """All three C11 kinds, in C11 order, through the sink the deployment wired.

    Before the fix this read ``['accepted']``: the route stamped the auction and dropped
    ``result.events``' other two entries, so the ledger held the promise and neither of the
    two records that can bridge it to an order.
    """
    sink, auction_id = served_accept()

    kinds = [str(event["kind"]) for event in checkout_events(sink, auction_id)]
    assert kinds == list(CHECKOUT_EVENT_KINDS), (
        f"the served accept filed {kinds} in the ledger; the checkout port built "
        f"{list(CHECKOUT_EVENT_KINDS)} and the route is what forwards them"
    )


def test_the_forwarded_events_carry_the_bodies_contracts_publishes(unwired: None) -> None:
    """Forwarded, not rebuilt: the code, the permalink and the token are the minted ones.

    A route that rebuilt these bodies from the bid could disagree with the checkout that was
    actually made — which is the same reason ``_accepted_offer`` reads the offer back off
    ``result.events`` instead of re-reading the bid.
    """
    sink, auction_id = served_accept()
    events = {str(event["kind"]): event for event in checkout_events(sink, auction_id)}

    created = events["code_created"]["payload"]
    redirect = events["checkout_redirect"]["payload"]
    accepted = events["accepted"]["payload"]

    assert created["code"] == RecordingCodeCreator.code
    assert created["code"] in str(created["permalink_url"])
    assert redirect["discount_code"] == RecordingCodeCreator.code
    assert redirect["permalink_url"] == created["permalink_url"]
    # The exchange's own token is on all three, which is what makes the bridge a bridge: it
    # ties the code back to the `accepted` offer inside the exchange's half of the checkout.
    assert created["checkout_token"] == accepted["checkout_token"]
    assert redirect["checkout_token"] == accepted["checkout_token"]


def test_every_forwarded_event_keeps_its_own_identity(unwired: None) -> None:
    """Three events, three ``event_id``s, all of them for this auction and this store.

    ``trust.events`` lands an event once per ``event_id``, so two records sharing one id are
    one record: a forwarding path that re-stamped a single id would silently drop a bridge at
    the trust door rather than in this process, which is far harder to see.
    """
    sink, auction_id = served_accept()
    events = checkout_events(sink, auction_id)

    ids = [str(event["event_id"]) for event in events]
    assert len(set(ids)) == len(ids) == 3, f"the trio shares identities: {ids}"
    assert all(str(event["auction_id"]) == auction_id for event in events), events
    # The two forwarded ones name the store the checkout was made with, which is what makes
    # them scopable by the reconciler. (The `accepted` event does not; see the module
    # docstring's last section — it is written by the auction, not by the checkout.)
    bridges = [event for event in events if str(event["kind"]) in CODE_BRIDGE_KINDS]
    assert [str(event["store_id"]) for event in bridges] == ["store-a", "store-a"], bridges


# =====================================================================================
# the join the bridge exists for — each with its control
# =====================================================================================
def test_reconcile_joins_the_offer_to_the_order_through_the_code(unwired: None) -> None:
    """The purchase reconciles although the two halves name two different checkout tokens."""
    sink, auction_id = served_accept()
    chain = with_offer_attributed(checkout_events(sink, auction_id))
    webhook = paid_webhook(RecordingCodeCreator.code, store_id="store-a")

    emitted = reconcile([*chain, webhook])

    assert len(emitted) == 1, (
        f"the offer and the order did not reach one group: {[e['kind'] for e in chain]} "
        f"against a webhook naming {RecordingCodeCreator.code}"
    )
    payload = emitted[0]["payload"]
    assert payload["price_comparable"] is True and payload["price_honored"] is True
    assert payload["promised_price"] == 100.0 and payload["observed_price"] == 100.0
    assert payload["bid_ref"] == "bid-a"


def test_control_without_the_bridge_the_same_purchase_reconciles_to_nothing(
    unwired: None,
) -> None:
    """Take the two forwarded events back out and the join collapses. This is the defect.

    Not "the verdict changes" — nothing is emitted at all, because the tokens differ and the
    discount code is the only value both halves carry.
    """
    sink, auction_id = served_accept()
    chain = with_offer_attributed(checkout_events(sink, auction_id))
    unbridged = [event for event in chain if str(event["kind"]) not in CODE_BRIDGE_KINDS]
    webhook = paid_webhook(RecordingCodeCreator.code, store_id="store-a")

    assert len(unbridged) == 1, unbridged
    assert reconcile([*unbridged, webhook]) == []


def test_an_overcharge_is_caught_only_because_the_bridge_travelled(unwired: None) -> None:
    """The point of the join: the order says 130 against a promise of 100."""
    sink, auction_id = served_accept()
    chain = with_offer_attributed(checkout_events(sink, auction_id))
    webhook = paid_webhook(RecordingCodeCreator.code, store_id="store-a", total_price=130.0)

    payload = reconcile([*chain, webhook])[0]["payload"]

    assert payload["price_comparable"] is True and payload["price_honored"] is False
    assert payload["observed_price"] == 130.0 and payload["promised_price"] == 100.0


# =====================================================================================
# the two blockers the bridge does not close, each measured on this route's own output
# =====================================================================================
def test_the_join_needs_the_offer_to_name_the_store_its_order_names(unwired: None) -> None:
    """``reconcile`` scopes every key by store, so an offer naming none joins nothing.

    This is the rule, not a bug report about it: keys are namespaced by store because a
    Shopify ``order_id`` is a per-shop number, and an event that names no store is filed in
    the unattributed scope where the code key of an attributed bridge cannot reach it. It is
    stated here because it is the last thing between this route's output and a reconciled
    purchase — and because it is measured on the real served chain rather than argued:
    ``0`` verdicts with the ``accepted`` event exactly as the ledger received it, ``1`` with
    the same event carrying the store the two bridges beside it already carry.

    The exchange's half of that is ``AuctionStateMachine._transition``, which records every
    transition with no ``store_id`` at all. As this file was written that made the first
    assertion below the SERVED chain verbatim and the second one the repair; both are written
    against the same real trio either way, so this keeps measuring the reconciler's rule once
    the state machine is given a store to stamp.
    """
    sink, auction_id = served_accept()
    chain = checkout_events(sink, auction_id)
    webhook = paid_webhook(RecordingCodeCreator.code, store_id="store-a")

    unnamed = [
        {**event, "store_id": None} if str(event["kind"]) == "accepted" else event
        for event in chain
    ]

    assert reconcile([*unnamed, webhook]) == [], (
        "an offer that names no store must not join an order that does: keys are namespaced "
        "by store because a Shopify order_id is a per-shop number"
    )
    assert len(reconcile([*with_offer_attributed(chain), webhook])) == 1


def test_the_store_alias_is_what_lets_the_two_names_meet(unwired: None) -> None:
    """A webhook filed under the shop domain joins only once the roster translates it.

    Both halves of this are measured. Unaliased, a purchase with a perfect code bridge
    reconciles to nothing, because ``reconcile`` scopes every key — the code key included —
    by store. Aliased through the platform's own roster, the same three events and the same
    webhook produce the verdict. So the naming mismatch is real, it is NOT closed by
    forwarding the bridge, and it is closed by a mechanism that already exists on the trust
    side and needs no new field from the exchange.
    """
    sink, auction_id = served_accept()
    chain = with_offer_attributed(checkout_events(sink, auction_id))
    webhook = paid_webhook(RecordingCodeCreator.code, store_id=SHOP_DOMAIN)

    assert reconcile([*chain, webhook]) == [], (
        "an order filed under the shop domain must not join an offer filed under the "
        "platform store_id by itself"
    )

    aliases = resolve_store_aliases([{"store_id": "store-a", "domain": SHOP_DOMAIN}])
    assert aliases == {SHOP_DOMAIN: "store-a"}
    aliased = {**webhook, "store_id": aliases[str(webhook["store_id"])]}

    emitted = reconcile([*chain, aliased])
    assert len(emitted) == 1, "the roster alias did not bring the two names together"
    assert emitted[0]["payload"]["price_honored"] is True
