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

MEASURED here, one served slot, re-taken through ``POST /auctions`` on this file's own fixture
— every value below is the exchange's, none is typed by hand::

    {"slot": "fit", "bid_ref": "auction-16bd…:demo-woolworks", "fit_score": 0.512909090909091,
     "trust_summary": {"store_id": "demo-woolworks", "available": true, "score": 0.81,
                       "confidence": 0.7},
     "provenance_labels": ["store-confirmed"],
     "product": {"product_ref": "beanie-1", "variant_ref": null,
                 "identity": {"title": "Merino Wool Beanie", "brand": "Woolworks",
                              "source": "snap-demo-woolworks", "observed_at": null}},
     "price": {"unit_price": 72.0, "total_price": 72.0, "currency": "USD",
               "discount": null, "expires_at": "2026-09-09T02:10:14.458772Z"},
     "commitments": [{"key": "free_returns", "value": "30 days", "claim_id": null,
                      "claim_type": null, "unit": null, "source_span": null,
                      "provenance": {"source": "owner_statement", "authority_rank": 1,
                                     "ref": "envelope:store-alpha:v3#free_returns",
                                     "observed_at": "2026-01-01T00:00:00Z"}}, …],
     "message": null, "fallback": false, "fallback_reason": null,
     "store_domain": "demo-woolworks.example.com"}

Twelve keys, not five: R2's five are the ones the requirement is about and the ones this file
grades, and the slot has since learned to carry the PLATFORM's crawled ``product.identity``
(D55's organic half, published so a shopper compares two named products rather than two opaque
refs), the store's own ``message``, and the ``fallback`` / ``fallback_reason`` pair that says
whether a row is a store's own quote or the exchange standing in at the roster's list price.
``CONTRACT_SLOT_FIELDS`` is therefore a SUBSET check and not an equality, so the next thing a
slot learns to show makes this file gain an assertion rather than lose one.

The tests below assert each of R2's five carries a real per-store value rather than one constant
repeated: the product is that store's rostered ``product_ref``, the price is what that store BID
(10% under its listing, so it cannot be confused with the roster's number), and the commitments
are the approved envelope's, dropped through the pinned ``Claim``.

**Where the other three used to stop, and why this file is where it shows.** They were dropped at
the *protocol schema*: ``ShortlistSlot`` declared exactly
``slot``/``bid_ref``/``fit_score``/``trust_summary``/``provenance_labels`` with
``additionalProperties: false``, and both doors that could carry them are pinned to it — ``GET
/auctions/{auction_id}/shortlist`` declares ``response_model=Shortlist``, so an extra key on a
stored slot was a **500**, measured, not a dropped field. Widening the contract with three
optional properties is what let ``exchange.ranking.serving.rank_auction`` fill them, and this
route then serves them unchanged.

``null`` is the spelling for "the exchange had nothing to put here" — see the R10 test below,
where a store that never answered gets a slot with a real price and ``commitments: null``. It is
never a zero and never an empty list, and the two exchange doors agree on it because the producer
stores a fixed point of the pinned model.
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

#: WHAT THE PLATFORM CRAWLED about each rostered product — the organic half of D55, and the
#: surface :mod:`exchange.retrieval.relevance` judges an organic row on. A title and a brand,
#: because that is what a crawl and a deployment document both carry; see :func:`_catalog` for
#: the measurement that says so and for what a ref-in-the-title-field cost this file.
#:
#: All three are beanies and the query asks for a beanie, so every one of them is genuinely on
#: topic. That is deliberate: this file's subject is R10 — a silent store still reaching the
#: shopper — and a roster that was ALSO off-topic would make a passing test unable to say which
#: rule it had confirmed. The off-topic case is a real and different rule, and it is pinned on
#: the exchange's own route in ``apps/exchange/tests/test_a_blank_shortlist_is_never_silent.py``.
CRAWLED_TITLES = {
    "demo-woolworks": "Merino Wool Beanie",
    "demo-northface": "Ribbed Merino Beanie",
    "demo-fastfashion": "Warm Knit Merino Beanie",
}
CRAWLED_BRANDS = {
    "demo-woolworks": "Woolworks",
    "demo-northface": "Northface",
    "demo-fastfashion": "Fastfashion",
}

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

#: Exactly what ``contracts.protocol.ShortlistSlot`` declares today, which is now all five of
#: R2's fields. Used as a SUBSET check (``>=``), never an equality, so a sixth thing a slot
#: learns to show makes this file gain assertions rather than lose one.
CONTRACT_SLOT_FIELDS = {
    "slot",
    "bid_ref",
    "fit_score",
    "trust_summary",
    "provenance_labels",
    "product",
    "price",
    "commitments",
}

#: R2's five, named as R2 names them, mapped to the slot keys that carry them. This is the list
#: the requirement is actually about; ``CONTRACT_SLOT_FIELDS`` is the schema's spelling of it.
R2_SLOT_FIELDS = {
    "PRODUCT": "product",
    "PRICE": "price",
    "COMMITMENTS": "commitments",
    "STORE TRUST INDICATOR": "trust_summary",
    "PROVENANCE LABELS": "provenance_labels",
}


def canonical_commitments() -> list[dict[str, Any]]:
    """The approved envelope's commitments as the pinned ``Claim`` serializes them.

    Dropped through ``contracts.protocol.Claim`` rather than compared against the fixture's raw
    JSON, because the exchange validates each commitment against that model before publishing it
    — so a claim's unstated optionals come back as explicit ``null``. Restating the expanded shape
    here by hand would be this test asserting against a spelling somebody typed.
    """
    from contracts.protocol import Claim

    return [Claim.model_validate(c).model_dump(mode="json") for c in approved_commitments()]


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
    """The PLATFORM's crawl of each rostered product, shaped the way every real source shapes it.

    ``canonical_name`` used to be ``PRODUCTS[store]`` — the product REF written into the title
    field — and ``brand`` was absent. That was harmless while the field was only a key claim
    verification joined on, and it stopped being harmless when
    :func:`~exchange.ranking.verification.catalog_identity` made the same field the PLATFORM's
    crawled NAME and :mod:`exchange.retrieval.relevance` began judging organic rows against it:
    ``identity_surface`` folded ``"beanie-2"`` to the single content word ``beanie`` (the bare
    digit is dropped), and ``TopicalRelevance`` needs ``min(2, len(asked))`` agreements or half,
    so against this file's three-content-word query (``warm``, ``merino``, ``beanie``) a
    one-word identity is refused **whatever product it names**. All three silent stores were
    excluded ``organic_result_off_topic`` and the shopper got a blank screen.

    MEASURED, because "no real crawl looks like that" is the whole basis for changing a fixture
    rather than a rule. Through the served ``catalog_identity`` -> ``identity_surface`` ->
    ``content_terms`` chain:

    * ``deploy/demo/exchange-deployment.json`` — 3,086 products, every one resolving an
      identity, minimum 2 content words, **0 below 2**.
    * the demo graph ``proxyshop-neo4j-1``, read live — ``MATCH (p:Product) RETURN count(p)`` =
      3,093, ``p.brand IS NULL OR p.brand = ''`` = 0, minimum 2 content words, **0 below 2**.

    Titles like ``Bacopa`` and ``Resveratrol`` do occur, and they clear the rule because the
    crawl also carries a brand: ``Bacopa Gaia Herbs`` folds to three terms. A ref in the title
    field and no brand at all is a shape neither source produces.

    So the products below are what the demo roster actually holds — beanies, for a beanie query
    — named and branded the way the crawl names and brands them. Nothing about what these tests
    ASSERT changed; what changed is that the fixture stopped stating something about the
    platform's crawl that no crawl states. ``product_ref`` is untouched, so every claim the
    verifier grades still joins on exactly the key it did.
    """
    from exchange.ranking.verification import StaticCatalogSnapshots

    return StaticCatalogSnapshots(
        {
            store: {
                "snapshot_id": f"snap-{store}",
                "products": [
                    {
                        "product_ref": PRODUCTS[store],
                        "canonical_name": CRAWLED_TITLES[store],
                        "brand": CRAWLED_BRANDS[store],
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


def slot_identity(slot: dict[str, Any]) -> str | None:
    """The PLATFORM's crawled title carried on one slot, or ``None`` if it carries none."""
    identity = (slot.get("product") or {}).get("identity") or {}
    return identity.get("title")


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
    """``>=`` and not ``==``: this goes green, not red, when a slot learns to show a sixth thing."""
    auction_id = open_an_auction(buyer_client)

    for slot in served_slots(buyer_client, auction_id):
        assert set(slot) >= CONTRACT_SLOT_FIELDS, slot
        assert slot["bid_ref"].startswith(f"{auction_id}:"), slot
        assert isinstance(slot["fit_score"], float), slot


def test_a_served_slot_shows_all_five_of_r2s_fields_with_real_values(buyer_client):
    """R2 verbatim, on the shopper's own route: PRODUCT, PRICE, COMMITMENTS, TRUST, PROVENANCE.

    Every store here answers with a real bid, so every slot must show all five and none of them
    may be ``null`` — a slot carrying the key with nothing in it is the field being declared and
    still not served, which is the shape this whole file exists to catch.
    """
    auction_id = open_an_auction(buyer_client)
    slots = served_slots(buyer_client, auction_id)
    assert len(slots) == len(STORES), slots

    for slot in slots:
        for requirement, key in R2_SLOT_FIELDS.items():
            assert key in slot, f"R2 asks a slot to show {requirement}; it has no {key!r}: {slot}"
            assert slot[key] is not None, (
                f"R2's {requirement} is declared on the contract but served empty: {slot}"
            )


def test_the_product_price_and_commitments_are_this_stores_own_bid_not_a_placeholder(buyer_client):
    """The three new fields, checked per store against what that store actually bid.

    Every number here is distinguishable on purpose: each store bids 10% UNDER its own listing, so
    a price read off a slot can be told apart both from the roster's list price and from the other
    two stores'. A field filled in with a constant — the failure an audit found in
    ``proxyshop_demo/s1.py`` — shows up as three identical values.
    """
    auction_id = open_an_auction(buyer_client)

    shown: dict[str, dict[str, Any]] = {}
    for slot in served_slots(buyer_client, auction_id):
        shown[slot["trust_summary"]["store_id"]] = slot

    assert set(shown) == set(STORES), shown
    for store, slot in shown.items():
        assert slot["product"]["product_ref"] == PRODUCTS[store], slot
        assert slot["product"]["variant_ref"] is None, slot
        # D55: the exchange also publishes the PLATFORM's own crawled name for the product, so
        # the shopper compares two products rather than two opaque refs.
        assert slot["product"]["identity"]["title"], slot
        assert slot["fallback"] is False, "this store bid; the slot must not read as a stand-in"
        price = slot["price"]
        assert price["unit_price"] == pytest.approx(BID_PRICES[store]), slot
        assert price["total_price"] == pytest.approx(BID_PRICES[store]), slot
        assert price["unit_price"] != pytest.approx(LIST_PRICES[store]), (
            "the slot is showing the ROSTER's list price, not what the store bid"
        )
        assert price["currency"] == "USD", slot
        # ISO-8601 UTC, never the float epoch the bidder above actually sent.
        assert isinstance(price["expires_at"], str) and price["expires_at"].endswith("Z"), slot
        assert slot["commitments"] == canonical_commitments(), slot

    prices = [slot["price"]["unit_price"] for slot in shown.values()]
    assert len(set(prices)) == len(prices), f"one price repeated across slots: {prices}"
    refs = [slot["product"]["product_ref"] for slot in shown.values()]
    assert len(set(refs)) == len(refs), f"one product repeated across slots: {refs}"


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

    **The slot's own three R2 fields are what this test is really guarding now.** A silent store
    asserted nothing: it has a catalogue product and the roster's LIST price, and it has no
    commitments at all. So its slot must show the product and the list price — not the 10%-under
    number the other two bid, and not a manufactured ``0.00`` — and must say ``commitments: null``
    rather than ``[]``, which would read as "this store promises nothing" when what is true is
    that it promised nothing *because it never spoke*. Enriching a slot from an offer that is not
    there is the likeliest way to break honest traffic, so it is driven rather than reasoned about.
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
    assert body["market"]["nothing_shown"] is False, body["market"]
    slots = {slot["trust_summary"]["store_id"]: slot for slot in body["shortlist"]["slots"]}
    assert silent in slots, sorted(slots)
    # The platform's own crawled name for the silent store's product, on the slot. A fallback
    # row is the one row nobody but the platform vouches for, so the name it is shown under has
    # to be the platform's — and it is the same surface the relevance filter judged it on.
    assert slot_identity(slots[silent]) == CRAWLED_TITLES[silent], slots[silent]

    slot = slots[silent]
    assert slot["product"]["product_ref"] == PRODUCTS[silent], slot
    assert slot["product"]["variant_ref"] is None, slot
    # R10's whole point, now sayable on the slot itself: the price below is the EXCHANGE's, and
    # a card that could not tell it from a quote had to refuse to say anything about either.
    assert slot["fallback"] is True, slot
    assert slot["fallback_reason"] == "no_response", slot
    assert slot["price"]["unit_price"] == pytest.approx(LIST_PRICES[silent]), slot
    assert slot["price"]["total_price"] == pytest.approx(LIST_PRICES[silent]), slot
    assert slot["price"]["unit_price"] != pytest.approx(BID_PRICES[silent]), (
        "a store that never answered is being shown a price as if it had bid one"
    )
    assert slot["price"]["unit_price"] != 0.0, "a manufactured 0.00 would beat every real bid"
    assert slot["commitments"] is None, (
        f"a silent store committed to nothing because it never spoke; `{slot['commitments']!r}` "
        "tells the shopper it made a promise"
    )
    # And the stores that DID answer are unaffected by the one that did not.
    for store in STORES:
        if store != silent:
            assert slots[store]["commitments"] == canonical_commitments(), slots[store]
            assert slots[store]["price"]["unit_price"] == pytest.approx(BID_PRICES[store])


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


# =====================================================================================
# The market verdict: the one field that separates "nobody bid" from "the market ran"
#
# When every solicited store falls back, the shortlist a shopper is served is a normal-looking
# list of catalogue prices — same shape, same slot count, no error anywhere. The exchange
# computes a one-line verdict for exactly that case and publishes it three ways from one
# computation (the 201, the `auction_closed` ledger entry, one log line) so they cannot
# disagree. It reached this service's record and stopped there: `AuctionView` forwarded five
# ARRAYS and `market` is a mapping, so the signal built to make a silently-organic market
# visible was invisible on the only route a shopper's page reads.
#
# Measured on the served buyer route, two queries against the demo stack:
#
#     'milk thistle'                     sponsored=4 list_price=2 all_fallback=false
#     'a walnut coffee table ...'        sponsored=0 list_price=6 all_fallback=true
#                                        fallback_reasons={no_response:2, store_declined:4}
# =====================================================================================
def test_the_market_verdict_the_exchange_published_reaches_the_shopper(buyer_client):
    """Every store bids, so the verdict must say so — and must actually be present."""
    auction_id = open_an_auction(buyer_client)

    market = served_view(buyer_client, auction_id)["market"]

    assert isinstance(market, dict), (
        "the exchange publishes its market verdict once, on the answer this service records; "
        "a shopper's page cannot tell an all-fallback market from a normal one without it"
    )
    assert market["all_fallback"] is False
    assert market["sponsored"] == len(STORES)
    assert market["list_price"] == 0
    assert market["solicited"] == len(STORES)
    # The window is a MEASUREMENT an operator may move, so it travels with the verdict rather
    # than being something a reader has to go and look up in a compose file.
    assert market["bid_window_seconds"] > 0


def test_a_market_where_every_store_fell_back_says_so_on_the_buyers_own_route(
    exchange, bidders, monkeypatch
):
    """The case the field exists for. Silent stores, a full shortlist, and a verdict.

    The shortlist is deliberately asserted to be NON-EMPTY first: an all-fallback market does
    not look like a failure from the outside, and if it did there would be no need for this
    field at all.
    """
    from apps.buyer.svc.src.intent.routes import set_buyer_llm

    reset_confirmations()
    set_buyer_llm(None)
    for name in ("BUYER_DEPLOYMENT", "BUYER_DEPLOYMENT_JSON", "BUYER_ROSTER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)
    monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(ROSTER))
    bidders.silent = set(STORES)

    app = importlib.import_module("buyer_svc.main").create_app()
    try:
        with TestClient(app) as client:
            auction_id = open_an_auction(client)
            body = served_view(client, auction_id)
    finally:
        bidders.silent = set()
        reset_confirmations()

    assert body["shortlist"]["slots"], (
        "an all-fallback market still serves a full shortlist at catalogue prices — that is "
        "why it is invisible without the verdict, and why this test asserts it first"
    )
    market = body["market"]
    assert market["all_fallback"] is True, market
    assert market["sponsored"] == 0, market
    assert market["list_price"] == len(STORES), market
    assert market["fallback_reasons"], "an all-fallback market that names no reason says nothing"
    # The blank screen this test was RED on, now sayable on the shopper's own route instead of
    # only in an exchange log line. `all_fallback` cannot carry it: it is guarded on there
    # being rows, so it reads `false` both when the market ran well and when nothing reached
    # the screen at all, and those are the two states this file must never confuse.
    assert market["nothing_shown"] is False, (
        "every store fell back and every fallback row reached a slot, so the screen was not "
        "blank — a true here is the regression this file exists to catch"
    )
    assert market["shortlisted"] == len(STORES), market
    assert market["shortlisted_sponsored"] == 0, market


def test_an_answer_that_carried_no_market_reports_none_rather_than_an_empty_one():
    """An exchange too old to publish a verdict, and one reporting an empty market, differ.

    Only one of those exists, and ``{}`` would report the one that does not.
    """
    from apps.buyer.svc.src.auctions.routes import RECORDED_MAPPING_KEY, _recorded_mapping

    assert _recorded_mapping(None, RECORDED_MAPPING_KEY) is None
    assert _recorded_mapping({"response": {}}, RECORDED_MAPPING_KEY) is None
    assert _recorded_mapping({"response": {"market": []}}, RECORDED_MAPPING_KEY) is None
    assert _recorded_mapping(
        {"response": {"market": {"all_fallback": True}}}, RECORDED_MAPPING_KEY
    ) == {"all_fallback": True}


def test_every_diagnostic_the_exchange_publishes_once_reaches_the_shopper(buyer_client, exchange):
    """The sweep, rather than the instance: nothing on the 201 may be silently dropped.

    ``market`` was found missing by comparing the exchange's own answer with this view's keys,
    and three more were missing beside it. A per-field test would have caught none of them,
    because the defect is a field NOT being listed — so this compares the two key sets and
    fails on whatever is dropped next.

    ``state`` is the one deliberate exclusion, and the assertion names it rather than
    subtracting it silently: it is the only field whose value can have changed since it was
    recorded, and this view keeps its live half and its recorded half labelled as such.
    """
    import urllib.request

    auction_id = open_an_auction(buyer_client)
    view = served_view(buyer_client, auction_id)

    # The exchange's own answer for the SAME roster, read off the exchange directly, so the
    # comparison is against what it really publishes rather than against a list written here.
    # The intent as the BUYER sends it, not the raw fixture: `INTENT` omits
    # `hard_constraints`, and an intent the exchange cannot read excludes every store (R19),
    # which would make this comparison one between two different auctions.
    sweep_intent = dict(INTENT) | {"hard_constraints": [], "preferences": []}
    payload = json.dumps(
        {"intent": sweep_intent | {"intent_id": "sweep-1"}, "roster": ROSTER}
    ).encode()
    request = urllib.request.Request(
        f"{exchange.url}/auctions", data=payload, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        published = set(json.load(response))

    #: Carried live rather than recorded, or deliberately excluded — each with its reason.
    ELSEWHERE = {
        "auction_id",  # the path parameter this view was asked about
        "shortlist",  # read LIVE from the exchange on every request, never off the record
        "state",  # see RECORDED_MAPPING_KEYS: a recorded state would read as a live one
    }

    dropped = published - set(view) - ELSEWHERE
    assert dropped == set(), (
        f"the exchange publishes {sorted(dropped)} on its POST /auctions answer and this view "
        f"drops them. Those diagnostics are published exactly once and survive nowhere else, "
        f"so a shopper cannot be told what they say. Add each to RECORDED_KEYS (arrays) or "
        f"RECORDED_MAPPING_KEYS (mappings), or add it to ELSEWHERE above with the reason."
    )

    # DECLARED IS NOT FORWARDED. A field on the response model that nothing ever fills reads
    # as `null` and satisfies the key comparison above while carrying no information at all --
    # which is the exact defect shape this repository keeps producing. So where the exchange
    # said something, this view must say it too. Only where it said something: `exploration`
    # is genuinely `null` on an auction with no explored slot, and `relaxed_constraints` is
    # genuinely `[]` when nothing was relaxed, and demanding a value for those would be
    # demanding the service invent one.
    with urllib.request.urlopen(
        urllib.request.Request(
            f"{exchange.url}/auctions",
            data=json.dumps(
                {"intent": sweep_intent | {"intent_id": "sweep-2"}, "roster": ROSTER}
            ).encode(),
            headers={"content-type": "application/json"},
        ),
        timeout=30,
    ) as response:
        answer = json.load(response)

    # STRUCTURAL, and it is what the value comparison below cannot do. A field is forwarded by
    # being listed in one of two tuples, and whether the check below bites depends on whether
    # this fixture happens to produce a non-empty value for it -- `relaxed_constraints` is `[]`
    # here because nothing needed relaxing, so a value comparison alone would let it be dropped
    # silently. Classifying by the type the exchange actually published catches that.
    from apps.buyer.svc.src.auctions.routes import (  # noqa: PLC0415
        RECORDED_KEYS,
        RECORDED_MAPPING_KEYS,
    )

    for key, value in answer.items():
        if key in ELSEWHERE:
            continue
        if isinstance(value, list):
            assert key in RECORDED_KEYS, (
                f"the exchange publishes {key!r} as an array and it is not in RECORDED_KEYS, so "
                f"this view answers a default rather than what the exchange said"
            )
        elif isinstance(value, dict):
            assert key in RECORDED_MAPPING_KEYS, (
                f"the exchange publishes {key!r} as a mapping and it is not in "
                f"RECORDED_MAPPING_KEYS, so this view answers a default rather than what the "
                f"exchange said"
            )
        # `None` and scalars are left alone: a `null` carries no type to classify by, and the
        # only scalar on this answer is `state`, which is in ELSEWHERE with its reason.

    said = {key: value for key, value in answer.items() if key not in ELSEWHERE and value}
    assert said, "the exchange published nothing at all, so this half grades nothing"
    empty = sorted(key for key in said if not view.get(key))
    assert empty == [], (
        f"the exchange published a value for {empty} and this view answers empty for them, so "
        f"the field is declared and never filled -- which reads as 'the exchange said nothing' "
        f"and is indistinguishable from it. Exchange said: "
        f"{ {k: said[k] for k in empty} }"
    )
    # ...and the exclusions are real rather than a way of passing: each must actually be
    # published, or this list has started hiding a field that no longer exists.
    assert ELSEWHERE <= published, sorted(ELSEWHERE - published)
