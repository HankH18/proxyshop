"""``POST /auctions/{auction_id}/accept`` — the served accept path (T-170, T-169).

Two defects meet in this file and both are about a *deployment*, not about a function:

* **T-170.** ``packages/contracts/openapi/exchange.openapi.json`` publishes
  ``/auctions/{auction_id}/accept``. Before ``accept/routes.py`` existed the booted app
  answered ``['/auctions', '/auctions/{auction_id}']`` and nothing else, so no HTTP request
  could reach ``accept()`` at all.
* **T-169.** Nothing in the repository called ``use_registered_domains``, so the served
  exchange ran with ``registered_domains=None`` and the checkout host guard compared
  ``bid["checkout_url"]`` against ``bid["store_domain"]`` — both written by the bidding
  store. Every test here that matters is therefore a *route* test: the seam worked in
  isolation already; what was missing was a deployment that turned it.

Every test restores the process-wide registry it found, because ``configure_accept`` wires
it deliberately and a leak between tests would let one test pass on another's wiring.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from exchange.accept import platform_registered_domains, use_registered_domains
from exchange.accept.routes import (
    AcceptBidRequest,
    InMemoryAuctionBids,
    NoRecordedBids,
    configure_accept,
)
from exchange.auction.state import AuctionStateMachine
from exchange.checkout import NoRegisteredDomains, StaticRegisteredDomains
from exchange.eligibility import BLACKLISTED, ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from fastapi.testclient import TestClient

SELLER_DOMAIN = "store-a.example.com"
RIVAL_DOMAIN = "attacker.tld"
PLATFORM_DOMAINS = {"store-a": SELLER_DOMAIN, "store-b": "store-b.example.com"}

EXCHANGE_OPENAPI = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "contracts"
    / "openapi"
    / "exchange.openapi.json"
)


@pytest.fixture
def unwired() -> Iterator[None]:
    """Leave the process-wide registry exactly as this test found it."""
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


def honest_bid(bid_id: str = "bid-a", store_id: str = "store-a") -> dict[str, Any]:
    domain = PLATFORM_DOMAINS[store_id]
    return {
        "bid_id": bid_id,
        "store_id": store_id,
        "store_domain": domain,
        "offer": {
            "product_ref": "product-1",
            "unit_price": 100.0,
            "total_price": 100.0,
            "checkout_url": f"https://{domain}/cart/1:1",
            "expires_at": 2_000_000_000.0,
        },
    }


def liar_bid() -> dict[str, Any]:
    """A store that writes the SAME rival host into both halves of its own host check."""
    return {
        "bid_id": "bid-liar",
        "store_id": "store-a",
        "store_domain": RIVAL_DOMAIN,
        "offer": {
            "product_ref": "product-1",
            "unit_price": 10.0,
            "total_price": 10.0,
            "checkout_url": f"https://{RIVAL_DOMAIN}/cart/1:1",
            "expires_at": 2_000_000_000.0,
        },
    }


class RecordingCodeCreator:
    """The merchant ``POST /codes`` client, in process. Records every mint it was asked for.

    It builds its permalink on **the offer's own host** on purpose, exactly as
    ``test_accept.py``'s does: a creator that manufactured an on-domain permalink from the
    registered domain would make every off-domain assertion here unfalsifiable.

    The route is driven in ``shopify`` mode throughout so this client is actually reached —
    the simulated ``redirect`` provider mints locally and never touches it, which would make
    "no code was created" true of every test whether the route worked or not.
    """

    code = "PSX-TESTCODE"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
        self.calls.append((str(store_id), dict(offer)))
        base = str(dict(offer).get("checkout_url") or f"https://{SELLER_DOMAIN}/cart/1:1")
        return {"code": self.code, "permalink_url": f"{base.split('?', 1)[0]}?discount={self.code}"}

    __call__ = create_code


def closed_auction(bids: list[dict[str, Any]]) -> tuple[Any, str, InMemoryAuctionBids]:
    """A machine holding one auction in ``closed`` — the only state ``accepted`` follows."""
    machine = AuctionStateMachine()
    auction_id = "auction-route-1"
    machine.create(auction_id, intent_id="intent-1", cluster_id="cluster-1", roster=[])
    machine.open(auction_id, now=1_700_000_000.0)
    machine.close(auction_id, now=1_700_000_001.0)
    book = InMemoryAuctionBids()
    book.record(auction_id, bids)
    return machine, auction_id, book


def wired_app(
    bids: list[dict[str, Any]],
    *,
    registered_domains: Any,
    eligibility: Any = None,
) -> tuple[Any, str, RecordingCodeCreator]:
    machine, auction_id, book = closed_auction(bids)
    creator = RecordingCodeCreator()
    app = create_app()
    configure_accept(
        app,
        machine=machine,
        bids=book,
        code_creator=creator,
        checkout_mode="shopify",
        registered_domains=registered_domains,
        eligibility=eligibility
        or StaticSellerEligibility({"store-a": ELIGIBLE, "store-b": ELIGIBLE}),
    )
    return app, auction_id, creator


# =====================================================================================
# T-170 — the published path is served
# =====================================================================================
def test_the_served_app_mounts_the_accept_router() -> None:
    app = create_app()
    assert "exchange.accept.routes" in app.state.mounted_routers


def test_the_served_path_is_the_one_the_contract_publishes() -> None:
    """Byte-for-byte the published template, not a near-miss like ``/auctions/{id}/accept``."""
    published = set(json.loads(EXCHANGE_OPENAPI.read_text(encoding="utf-8"))["paths"])
    served = set(create_app().openapi()["paths"])
    assert "/auctions/{auction_id}/accept" in published & served


def test_the_request_body_refuses_a_field_the_contract_does_not_declare() -> None:
    """``additionalProperties: false`` on the published request body, enforced not documented."""
    with pytest.raises(Exception):  # noqa: B017 - pydantic's ValidationError, imported nowhere
        AcceptBidRequest(bid_ref="bid-a", registered_domains="attacker.tld")


def test_an_accept_reaches_the_mint_and_answers_with_the_permalink(unwired: None) -> None:
    """The positive control. Without it every refusal below is satisfied by a dead route."""
    app, auction_id, creator = wired_app(
        [honest_bid()], registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS)
    )
    response = TestClient(app).post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-a"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert urlsplit(body["permalink_url"]).hostname == SELLER_DOMAIN
    assert body["code"] and body["code"] in body["permalink_url"]
    # `notice` joined the published body with the tier-0 fallback ruling: an accept that mints
    # nothing has to be able to SAY so, and a real accept says nothing. It is checked twice on
    # purpose — once against a literal, so the document alone cannot license a new field, and
    # once against the document, so this literal alone cannot license one either.
    assert set(body) == {"permalink_url", "code", "notice"}, (
        "the 200 body carries only what is published"
    )
    declared = set(
        json.loads(EXCHANGE_OPENAPI.read_text(encoding="utf-8"))["paths"][
            "/auctions/{auction_id}/accept"
        ]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    )
    assert set(body) == declared, (
        f"the served 200 body {sorted(body)} is not the published one {sorted(declared)}"
    )
    assert body["notice"] is None, f"a minting accept carries no fallback notice: {body}"


def test_the_acceptance_is_persisted_so_the_second_request_gets_no_second_code(
    unwired: None,
) -> None:
    """``accept()`` stamps the object it is handed; a route that dropped that stamp would
    hand two live single-use discounts to one buyer for one purchase."""
    app, auction_id, creator = wired_app(
        [honest_bid(), honest_bid("bid-b", "store-b")],
        registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS),
    )
    client = TestClient(app)
    assert (
        client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-a"}).status_code == 200
    )

    record = app.state.auction_machine.store.load(auction_id)
    assert record is not None and record.accepted_bid_ref == "bid-a"
    assert record.state == "accepted"

    second = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-b"})
    assert second.status_code == 409, second.text
    assert second.json()["accepted"] is False
    assert len(creator.calls) == 1, f"a second code was minted: {creator.calls}"


def test_an_auction_nobody_created_is_a_404_not_a_refusal(unwired: None) -> None:
    app, _auction_id, creator = wired_app(
        [honest_bid()], registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS)
    )
    response = TestClient(app).post("/auctions/auction-nowhere/accept", json={"bid_ref": "bid-a"})
    assert response.status_code == 404
    assert creator.calls == []


def test_a_bid_this_auction_never_carried_is_refused_without_minting(unwired: None) -> None:
    app, auction_id, creator = wired_app(
        [honest_bid()], registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS)
    )
    response = TestClient(app).post(
        f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-nowhere"}
    )
    assert response.status_code == 409
    assert response.json()["denial_reason"].startswith("unknown_bid")
    assert creator.calls == []


def test_a_store_blacklisted_after_bidding_gets_no_code_through_the_route(
    unwired: None,
) -> None:
    """R12 is re-read at accept time, and the route is where a deployment supplies the source."""
    app, auction_id, creator = wired_app(
        [honest_bid()],
        registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS),
        eligibility=StaticSellerEligibility({"store-a": BLACKLISTED, "store-b": ELIGIBLE}),
    )
    response = TestClient(app).post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-a"})
    assert response.status_code == 409
    assert "blacklist" in response.json()["denial_reason"].lower()
    assert creator.calls == []


# =====================================================================================
# T-169 — the served route wires the platform's registry, and the guard is not decorative
# =====================================================================================
def test_configure_accept_wires_the_process_wide_registered_domain_seam(unwired: None) -> None:
    """The call site T-169 exists for: after this, ``accept()`` with no keyword is bound."""
    platform = StaticRegisteredDomains(PLATFORM_DOMAINS)
    assert platform_registered_domains() is None
    configure_accept(create_app(), registered_domains=platform)
    assert platform_registered_domains() is platform


def test_a_store_that_lies_consistently_is_refused_through_the_served_route(
    unwired: None,
) -> None:
    """The whole of T-169, end to end.

    The liar writes ``attacker.tld`` into ``store_domain`` AND into ``checkout_url``, so it
    supplies both halves of the exact-host comparison and agrees with itself. Unwired, that
    accept succeeded and the buyer was redirected to ``attacker.tld`` holding a real
    single-use discount code. Wired, the platform's table overrides the bid outright.
    """
    app, auction_id, creator = wired_app(
        [liar_bid(), honest_bid()], registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS)
    )
    client = TestClient(app)

    refused = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-liar"})
    assert refused.status_code == 409, refused.text
    assert RIVAL_DOMAIN in refused.json()["denial_reason"]
    assert creator.calls == [], "a discount code was minted for an unregistered host"

    # Positive control: a route that refused every accept would satisfy the assertions above.
    honest = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-a"})
    assert honest.status_code == 200, honest.text
    assert urlsplit(honest.json()["permalink_url"]).hostname == SELLER_DOMAIN


def test_an_unconfigured_deployment_fails_closed_rather_than_trusting_the_bid(
    unwired: None,
) -> None:
    """No ``configure_accept(registered_domains=...)`` at all: the route installs
    ``NoRegisteredDomains`` and mints nothing, instead of falling back to the bid's claim."""
    machine, auction_id, book = closed_auction([liar_bid()])
    creator = RecordingCodeCreator()
    app = create_app()
    configure_accept(
        app,
        machine=machine,
        bids=book,
        code_creator=creator,
        checkout_mode="shopify",
        eligibility=StaticSellerEligibility({"store-a": ELIGIBLE}),
    )
    response = TestClient(app).post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-liar"})

    assert response.status_code == 409, response.text
    assert creator.calls == [], "an unconfigured exchange minted a code for an unregistered host"
    assert isinstance(app.state.registered_domains, NoRegisteredDomains)
    assert platform_registered_domains() is app.state.registered_domains


# =====================================================================================
# The bid port — the honest statement of what this route still needs
# =====================================================================================
def test_the_default_bid_source_records_nothing_and_therefore_accepts_nothing() -> None:
    """Stated as behaviour so nobody mistakes the served path for a wired one.

    Nothing in the repo persists an auction's bids: ``AuctionRecord`` carries ``roster`` and
    no bids, and ``POST /auctions`` drops the ``BidEntry`` list once it has answered. Until a
    deployment fills this port every accept is refused with ``unknown_bid``.
    """
    assert NoRecordedBids().bids_for("auction-1") == ()
    book = InMemoryAuctionBids()
    assert book.bids_for("auction-1") == ()
    book.record("auction-1", [honest_bid()])
    assert [bid["bid_id"] for bid in book.bids_for("auction-1")] == ["bid-a"]
