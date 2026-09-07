"""``GET /buyer/auctions/{auction_id}`` driven against the REAL exchange, on a real socket.

This is the route a shopper's page reads a shortlist from, and until this file existed nothing
in the repository drove it end to end. ``test_composition_wiring.py`` answers the exchange's
two doors from a hand-written ``FakeExchange`` — deliberately, because what it tests is the
buyer's half of the seam — so every assertion about *slot content* there is an assertion about
a fixture somebody wrote by hand. Here the exchange is ``exchange.main:app`` under a real
``uvicorn`` on 127.0.0.1, the buyer is ``buyer_svc.main:create_app()``, and the only thing
between them is the two environment variables an operator sets. A field asserted below has
travelled from a store's bid, through the exchange's ranking, over a socket, into the JSON the
shopper is handed.

R2 and what a slot shows
------------------------
R2, verbatim: *"present a shortlist of up to 4 differentiated slots (best fit / best value /
most reliable / specialist), each showing PRODUCT, PRICE, COMMITMENTS, STORE TRUST INDICATOR,
and PROVENANCE LABELS ('store-confirmed' vs 'from their website')"*.

MEASURED here, one served slot, verbatim::

    {"slot": "fit", "bid_ref": "auction-bc81…:demo-woolworks", "fit_score": 0.602,
     "trust_summary": {"store_id": "demo-woolworks", "available": true, "score": 0.81,
                       "confidence": 0.7, "low_data": false, "dimensions": ["delivery"]},
     "provenance_labels": ["store-confirmed"]}

R2's **store trust indicator** and **provenance labels** are there, and the tests below assert
they carry real per-store values rather than one constant repeated. R2's **product**, **price**
and **commitments** are not, and they stop at the *protocol schema* rather than anywhere in
this service: ``ShortlistSlot`` in ``packages/contracts/schemas/protocol.schema.json`` declares
exactly ``slot``/``bid_ref``/``fit_score``/``trust_summary``/``provenance_labels`` with
``additionalProperties: false``, and both doors that could carry them are pinned to it —
``GET /auctions/{auction_id}/shortlist`` declares ``response_model=Shortlist`` (an extra key on
a slot is a 500, measured, not a dropped field), and the exchange's ``POST /auctions`` answer
documents its ``shortlist`` as "the same object ``GET /auctions/{auction_id}/shortlist``
serves", with a test asserting the two bodies are equal. All three values are present on the
candidate the ranker held — ``apps/exchange/tests/test_ranking_served.py`` asserts that — so
what is needed is three optional properties on ``ShortlistSlot``, after which
``exchange.ranking.serving.rank_auction`` can fill them and this route serves them unchanged.

**Nothing here asserts their absence.** A test that pinned it would go red on whoever adds
them, which is the wrong direction for a gate to fail in.
"""

from __future__ import annotations

import importlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from apps.buyer.svc.src.intent import reset_confirmations

REPO_ROOT = Path(__file__).resolve().parents[4]

CONFIRM = "/buyer/intent/confirm"
AUCTION_VIEW = "/buyer/auctions"

STORES = ("demo-woolworks", "demo-northface", "demo-fastfashion")
LIST_PRICES = {"demo-woolworks": 80.0, "demo-northface": 95.0, "demo-fastfashion": 40.0}
PRODUCTS = {
    "demo-woolworks": "beanie-1",
    "demo-northface": "beanie-2",
    "demo-fastfashion": "beanie-3",
}
#: Every store bids under its listing, so a price read off a slot can be told apart from the
#: roster's number.
BID_PRICES = {store: round(LIST_PRICES[store] * 0.9, 2) for store in STORES}

#: One trust score per store, all different, so "the indicator is real" is checkable rather
#: than a single number that could be a constant.
TRUST_SCORES = {"demo-woolworks": 0.81, "demo-northface": 0.55, "demo-fastfashion": 0.63}

ROSTER: list[dict[str, Any]] = [
    {
        "store_id": store,
        "tier": 1,
        "product_ref": PRODUCTS[store],
        "list_price": LIST_PRICES[store],
    }
    for store in STORES
]

INTENT = {
    "intent_id": "int-r2-shortlist",
    "query": "a warm merino beanie",
    "budget_band": "0-100",
    "cluster_id": "cluster-warm-layers",
}

#: Exactly what ``contracts.protocol.ShortlistSlot`` declares today. Used as a SUBSET check
#: (``>=``), never an equality: the day the contract carries R2's other three, this file gains
#: assertions rather than losing one.
CONTRACT_SLOT_FIELDS = {"slot", "bid_ref", "fit_score", "trust_summary", "provenance_labels"}


def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def approved_commitments() -> list[dict[str, Any]]:
    """``fixtures/envelopes/store-alpha.approved.json``'s standing commitments, verbatim.

    A real approved envelope rather than a shape invented here: these are the claims a merchant
    onboarding review actually signed off — ``free_returns: 30 days``, ``ships_within:
    2 business days``, with real ``owner_statement`` provenance — and they are what a hosted bid
    carries in ``offer.commitments``. Every store below bids with them, so the traffic driven
    through this route is the shape honest traffic has.
    """
    document = json.loads(
        (REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json").read_text(
            encoding="utf-8"
        )
    )
    return list(document["envelope"]["standing_commitments"])


def _claim(key: str, value: Any) -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "provenance": {"source": "owner_statement", "ref": f"ref:{key}", "authority_rank": 1},
    }


class Bidders:
    """The exchange's outbound bid client: every rostered store answers with a real bid."""

    def __init__(self, *, silent: tuple[str, ...] = ()) -> None:
        self.silent = set(silent)
        self.asked: list[str] = []

    def __call__(self, store: dict[str, Any]) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        self.asked.append(store_id)
        if store_id in self.silent:
            return None
        price = BID_PRICES[store_id]
        return {
            "store_id": store_id,
            "received_at": time.time(),
            "bid": {
                "auction_id": None,
                "store_id": store_id,
                "offer": {
                    "product_ref": PRODUCTS[store_id],
                    "unit_price": price,
                    "total_price": price,
                    "currency": "USD",
                    "commitments": approved_commitments(),
                    "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
                    "expires_at": time.time() + 3600.0,
                },
                "claims": [_claim("capacity_l", 35)],
                "agent_version": "1.0.0",
                "schema_version": "1.0.0",
            },
        }


def _catalog() -> Any:
    from exchange.ranking.verification import StaticCatalogSnapshots

    return StaticCatalogSnapshots(
        {
            store: {
                "snapshot_id": f"snap-{store}",
                "products": [
                    {
                        "product_ref": PRODUCTS[store],
                        "canonical_name": PRODUCTS[store],
                        "evidence_ref": f"snap-{store}#{PRODUCTS[store]}",
                        "attributes": {"capacity_l": {"value": 35}},
                    }
                ],
            }
            for store in STORES
        }
    )


def _wired_exchange(bidders: Bidders) -> Any:
    """``exchange.main:app``, wired the way a deployment wires it."""
    from exchange.auction.routes import configure_auctions
    from exchange.checkout.sellers import StaticRegisteredDomains
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
    from exchange.main import create_app
    from exchange.ranking.serving import configure_ranking

    app = create_app()
    configure_auctions(
        app,
        solicitor=bidders,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in STORES}),
    )
    configure_ranking(
        app,
        trust_snapshot={
            store: {"blacklisted": False, "score": score, "confidence": 0.7}
            for store, score in TRUST_SCORES.items()
        },
        registered_domains=StaticRegisteredDomains({s: _domain(s) for s in STORES}),
        catalog=_catalog(),
    )
    return app


class _LoopbackExchange:
    """A real uvicorn server for the exchange app, on an ephemeral loopback port."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        self._thread: threading.Thread | None = None
        self.url = ""

    def start(self) -> str:
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while not self._server.started:
            if time.monotonic() > deadline:
                raise AssertionError("the exchange never came up on loopback")
            time.sleep(0.02)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10.0)


@pytest.fixture
def bidders() -> Bidders:
    return Bidders()


@pytest.fixture
def exchange(bidders):
    """The real exchange, served over loopback for the duration of one test."""
    server = _LoopbackExchange(_wired_exchange(bidders))
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def buyer_client(exchange, monkeypatch):
    """A buyer service configured the way an operator configures one: two variables.

    Nothing on ``app.state`` is wired by hand — a test that wires the app it is testing is
    measuring the wiring it wrote, which is the reason ``test_composition_wiring.py`` exists.
    """
    from apps.buyer.svc.src.intent.routes import set_buyer_llm

    reset_confirmations()
    set_buyer_llm(None)
    monkeypatch.delenv("BUYER_DEPLOYMENT", raising=False)
    monkeypatch.delenv("BUYER_DEPLOYMENT_JSON", raising=False)
    monkeypatch.delenv("BUYER_ROSTER", raising=False)
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)
    monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(ROSTER))

    app = importlib.import_module("buyer_svc.main").create_app()
    with TestClient(app) as client:
        yield client
    reset_confirmations()


def open_an_auction(client: TestClient) -> str:
    created = client.post(CONFIRM, json={"intent": dict(INTENT), "confirmed": True})
    assert created.status_code == 201, created.text
    return str(created.json()["auction_id"])


def served_view(client: TestClient, auction_id: str) -> dict[str, Any]:
    view = client.get(f"{AUCTION_VIEW}/{auction_id}")
    assert view.status_code == 200, view.text
    return dict(view.json())


def served_slots(client: TestClient, auction_id: str) -> list[dict[str, Any]]:
    body = served_view(client, auction_id)
    assert body["shortlist"] is not None, body
    return list(body["shortlist"]["slots"])


# =====================================================================================
# The shopper's route, end to end against the real exchange
# =====================================================================================
def test_a_confirmed_intent_becomes_a_served_shortlist_with_one_slot_per_bidding_store(
    buyer_client, bidders
):
    """The journey, over two real processes' worth of wiring and one socket."""
    auction_id = open_an_auction(buyer_client)

    body = served_view(buyer_client, auction_id)

    assert bidders.asked == list(STORES), bidders.asked
    assert body["auction_id"] == auction_id
    assert body["solicited"] == list(STORES), body["solicited"]
    slots = body["shortlist"]["slots"]
    assert len(slots) == len(STORES), slots
    # Four differentiated slot NAMES (D29), never the same argument three times.
    assert len({slot["slot"] for slot in slots}) == len(slots), slots
    assert {slot["slot"] for slot in slots} <= {"fit", "value", "reliability", "specialist"}


def test_every_served_slot_carries_the_published_contracts_fields(buyer_client):
    """``>=`` and not ``==``: this goes green, not red, when the contract carries R2's rest."""
    auction_id = open_an_auction(buyer_client)

    for slot in served_slots(buyer_client, auction_id):
        assert set(slot) >= CONTRACT_SLOT_FIELDS, slot
        assert slot["bid_ref"].startswith(f"{auction_id}:"), slot
        assert isinstance(slot["fit_score"], float), slot


def test_the_store_trust_indicator_is_that_stores_own_number_not_one_repeated(buyer_client):
    """R2's trust indicator, read off the served JSON and checked per store.

    Three stores, three different snapshot scores, and each slot must show its own. A slot
    showing a number a human typed once — the failure mode an audit found in
    ``proxyshop_demo/s1.py`` — would show the same value three times here.
    """
    auction_id = open_an_auction(buyer_client)

    shown = {}
    for slot in served_slots(buyer_client, auction_id):
        summary = slot["trust_summary"]
        assert summary["available"] is True, slot
        shown[summary["store_id"]] = summary["score"]

    assert shown == {store: pytest.approx(score) for store, score in TRUST_SCORES.items()}, shown
    assert len(set(shown.values())) == len(shown), f"one number repeated across slots: {shown}"


def test_the_provenance_labels_are_r2s_published_strings(buyer_client):
    """R2's other served field: the exchange produces the label, this service renders it (D30).

    Every bid here carries ``owner_statement`` provenance, whose published buyer label is
    ``store-confirmed`` — asserted against ``contracts.labels`` rather than a string typed here,
    so the exchange that produces it and this assertion cannot drift.
    """
    from contracts.labels import PROVENANCE_BUYER_LABELS

    expected = PROVENANCE_BUYER_LABELS["owner_statement"]
    auction_id = open_an_auction(buyer_client)

    for slot in served_slots(buyer_client, auction_id):
        assert slot["provenance_labels"] == [expected], slot
    assert expected == "store-confirmed", expected


def test_a_silent_store_still_reaches_a_slot_on_its_list_price(monkeypatch):
    """R10 through the whole stack: a store that never answered is still shown, honestly.

    Its entry is marked ``fallback`` in the recorded diagnostics — which is how a reader tells
    a price the STORE quoted from one the exchange manufactured for it — and it still gets a
    slot, because R10 says a silent store can reach the shortlist.
    """
    silent = "demo-northface"
    server = _LoopbackExchange(_wired_exchange(Bidders(silent=(silent,))))
    server.start()
    try:
        from apps.buyer.svc.src.intent.routes import set_buyer_llm

        reset_confirmations()
        set_buyer_llm(None)
        for name in ("BUYER_DEPLOYMENT", "BUYER_DEPLOYMENT_JSON", "BUYER_ROSTER"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("EXCHANGE_URL", server.url)
        monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(ROSTER))

        with TestClient(importlib.import_module("buyer_svc.main").create_app()) as client:
            auction_id = open_an_auction(client)
            body = served_view(client, auction_id)
    finally:
        server.stop()
        reset_confirmations()

    entries = {entry["store_id"]: entry for entry in body["entries"]}
    assert entries[silent]["fallback"] is True, entries[silent]
    assert entries[silent]["unit_price"] == pytest.approx(LIST_PRICES[silent]), entries[silent]
    for store in STORES:
        if store != silent:
            assert entries[store]["fallback"] is False, entries[store]
            assert entries[store]["unit_price"] == pytest.approx(BID_PRICES[store])
    shortlisted = {slot["trust_summary"]["store_id"] for slot in body["shortlist"]["slots"]}
    assert silent in shortlisted, shortlisted


# =====================================================================================
# The live/recorded split this route is built on
# =====================================================================================
def test_the_slots_are_the_live_doors_and_a_forgotten_auction_serves_null_not_the_record(
    buyer_client, exchange
):
    """The invariant ``auctions/routes.py`` exists to keep, driven rather than read.

    The exchange is made to forget the auction while this service's record stays exactly where
    it was. ``shortlist`` must go to ``null`` — a shortlist rebuilt from the record would tell a
    shopper that slots exist which the exchange may since have dropped — while the recorded
    diagnostics beside it are unaffected, because those are this service's own record.
    """
    from exchange.ranking.serving import ShortlistStore, shortlist_store

    auction_id = open_an_auction(buyer_client)
    assert served_slots(buyer_client, auction_id), "nothing to forget"

    assert isinstance(shortlist_store(exchange.app), ShortlistStore)
    exchange.app.state.shortlists = ShortlistStore()

    body = served_view(buyer_client, auction_id)

    assert body["shortlist"] is None, (
        "the exchange has forgotten this auction, so a shortlist served from this service's own "
        f"record would be a stale shortlist wearing a live one's name: {body['shortlist']}"
    )
    assert body["ranked"], body
    assert body["recorded_at"], body


def test_the_recorded_diagnostics_survive_an_exchange_that_has_gone_away(buyer_client, exchange):
    """``outcome_for`` is local, so a shopper can still be told who was asked and why.

    Driven by stopping the exchange's socket outright: the live half becomes a 502 at worst and
    the recorded half is unaffected, which is the whole reason the two are read separately.
    """
    auction_id = open_an_auction(buyer_client)
    assert served_slots(buyer_client, auction_id)

    exchange.stop()

    view = buyer_client.get(f"{AUCTION_VIEW}/{auction_id}")

    # 502, never 500: this request was fine and the upstream's answer was not. The detail
    # names the door that failed and the origin it was reached at, so an operator reading it
    # knows which service to look at.
    assert view.status_code == 502, view.text
    detail = view.json()["detail"]
    assert f"/auctions/{auction_id}/shortlist" in detail, detail
    assert exchange.url in detail, detail
