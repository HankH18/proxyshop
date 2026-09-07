"""The two seams ``rank_auction`` was missing: the largest feature, and the seller's prose.

Both defects had the same shape — a value that EXISTS, is produced by a real component, and
cannot reach the function that consumes it — and both were being worked around one frame away
from the module that should have carried them.

**1. ``intent_match`` could not be handed to ``rank_auction``.** It carries ``w_m = 0.35``, the
largest weight in the published formula. The graph lane made it real (``retrieval/roster.py``
measures it per shop) but had no seam to deliver it through, because ``rank_auction`` builds its
candidates internally from ``BidEntry`` objects through a projection that names its fields and
deliberately copies no published feature — the R11 property that stops a bidder writing
``intent_match: 1.0`` into its own reply. So ``auction/routes.py`` called the public ``rank()``
a SECOND time, on the candidates ``rank_auction`` had already returned. Correct, and redundant:
one served auction ran the whole filter/score/shortlist pipeline twice.

**2. ``Bid.message`` — the artefact a shop BUYS (D55) — did not reach verification.**
``ranking/verification.py`` decomposes and grades a seller's pitch, and said in its own source
that the projection dropped the field one frame above it. ``test_ranking_verification.py``
carried a monkeypatch fixture, ``the_projection_carries_the_pitch``, whose docstring said to
delete it once the real line landed. It is deleted; §3 of that file now drives the real
projection.

What is asserted here is the SEAM in both directions: the platform's measurement gets in, and a
bidder's does not.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from contracts.ranking import DEFAULT_RANKING_WEIGHTS, INTENT_MATCH_WHEN_ABSENT
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.candidates import CANDIDATE_FIELDS, candidate_from_entry
from exchange.ranking.serving import configure_ranking, rank_auction
from exchange.ranking.verification import StaticCatalogSnapshots
from exchange.retrieval import ShopRoster, SolicitedShop
from fastapi.testclient import TestClient

W_M = float(DEFAULT_RANKING_WEIGHTS.w_m)

STORES = ("shop-alpha", "shop-bravo", "shop-charlie")


def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


class _Entry:
    """The two attributes ``candidates_from_entries`` reads off a ``BidEntry``."""

    fallback = False

    def __init__(self, store_id: str, bid: dict[str, Any], list_price: float | None = 120.0):
        self.store_id = store_id
        self.bid = bid
        self.claims = bid.get("claims")
        self.list_price = list_price


def _offer(store_id: str, price: float = 95.0) -> dict[str, Any]:
    return {
        "product_ref": "product-1",
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
        "expires_at": time.time() + 3600.0,
    }


def _bid(store_id: str, price: float = 95.0, **extra: Any) -> dict[str, Any]:
    return {
        "auction_id": None,
        "store_id": store_id,
        "offer": _offer(store_id, price),
        "claims": [],
        "agent_version": "1.0.0",
        "schema_version": "1.0.0",
        **extra,
    }


def _snapshot(store_id: str) -> dict[str, Any]:
    return {
        "snapshot_id": f"snap-{store_id}",
        "store_id": store_id,
        "products": [
            {
                "product_ref": "product-1",
                "canonical_name": "product-1",
                "evidence_ref": f"snap-{store_id}#product-1",
                "attributes": {"warranty_months": {"value": 24}},
            }
        ],
    }


class _Roster:
    """A shop-roster source answering with a fixed set of shops and their measured fits."""

    name = "neo4j"

    def __init__(self, fits: dict[str, float]) -> None:
        self.fits = dict(fits)

    def solicit(self, intent: Any, *, limit: Any = None) -> ShopRoster:
        return ShopRoster(
            shops=tuple(
                SolicitedShop(
                    store_id=store,
                    tier=1,
                    product_ref="product-1",
                    intent_match=fit,
                    domain=_domain(store),
                    business_identity=f"{store} Ltd",
                    list_price=120.0,
                    currency="USD",
                    source_ids=(f"src-{store}",),
                )
                for store, fit in self.fits.items()
            ),
            source=self.name,
            considered=len(self.fits),
        )


def _app(*, bids: dict[str, dict[str, Any]], roster_source: Any = None) -> Any:
    def solicit(store: Any) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        reply = bids.get(store_id)
        if reply is None:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": dict(reply)}

    app = create_app()
    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in bids}),
        shop_roster=roster_source,
    )
    configure_ranking(
        app,
        trust_snapshot={store: {"blacklisted": False, "score": 0.6} for store in bids},
        registered_domains=StaticRegisteredDomains({store: _domain(store) for store in bids}),
        catalog=StaticCatalogSnapshots({store: _snapshot(store) for store in bids}),
    )
    return app


INTENT = {
    "intent_id": "intent-1",
    "cluster_id": "cluster-1",
    "query": "a 35 litre cabin backpack",
    "hard_constraints": [],
}


def _post(app: Any, *, roster: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"intent": dict(INTENT), "bid_timeout_seconds": 2.0}
    if roster is not None:
        body["roster"] = roster
    response = TestClient(app).post("/auctions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# =====================================================================================
# 1. `intent_match` reaches the ranker through `rank_auction`, once
# =====================================================================================
def test_rank_auction_accepts_intent_match_and_scores_it() -> None:
    """The seam itself, exercised directly. RED before: ``rank_auction`` had no such parameter."""
    fits = {"shop-alpha": 0.90, "shop-bravo": 0.20}
    entries = [_Entry(store, _bid(store)) for store in fits]
    ranked = rank_auction(
        entries,
        auction_id="auction-seam",
        intent=dict(INTENT),
        now=time.time(),
        trust_snapshot={store: {"blacklisted": False, "score": 0.6} for store in fits},
        registered_domains=StaticRegisteredDomains({s: _domain(s) for s in fits}),
        intent_match=fits,
    )
    components = {row["store_id"]: row["components"] for row in ranked["ranked"]}
    assert sorted(components) == sorted(fits), components
    for store, fit in fits.items():
        assert components[store]["intent_match"] == pytest.approx(W_M * fit), components


def test_a_candidate_the_platform_did_not_measure_keeps_the_published_neutral() -> None:
    """Absent beats invented. A store the map does not name reads ``INTENT_MATCH_WHEN_ABSENT``."""
    entries = [_Entry(store, _bid(store)) for store in ("shop-alpha", "shop-bravo")]
    ranked = rank_auction(
        entries,
        auction_id="auction-seam",
        intent=dict(INTENT),
        now=time.time(),
        trust_snapshot={
            store: {"blacklisted": False, "score": 0.6} for store in ("shop-alpha", "shop-bravo")
        },
        registered_domains=StaticRegisteredDomains(
            {s: _domain(s) for s in ("shop-alpha", "shop-bravo")}
        ),
        intent_match={"shop-alpha": 0.9},
    )
    components = {row["store_id"]: row["components"] for row in ranked["ranked"]}
    assert components["shop-alpha"]["intent_match"] == pytest.approx(W_M * 0.9)
    assert components["shop-bravo"]["intent_match"] == pytest.approx(W_M * INTENT_MATCH_WHEN_ABSENT)


@pytest.mark.parametrize("supplied", [None, float("nan"), float("inf"), "0.99", [1.0]])
def test_an_unreadable_measurement_is_absent_rather_than_scored(supplied: Any) -> None:
    """A fit the platform could not state as a finite number is not a fit.

    Refused at THIS seam rather than left to ``scoring.feature_vector``'s guard, so the
    candidate reaches the scorer with the feature genuinely absent — which is the state
    ``ranking/features.py`` documents as "we do not know" — rather than carrying a key holding
    a value nothing can read.
    """
    entries = [_Entry("shop-alpha", _bid("shop-alpha"))]
    ranked = rank_auction(
        entries,
        auction_id="auction-seam",
        intent=dict(INTENT),
        now=time.time(),
        trust_snapshot={"shop-alpha": {"blacklisted": False, "score": 0.6}},
        registered_domains=StaticRegisteredDomains({"shop-alpha": _domain("shop-alpha")}),
        intent_match={"shop-alpha": supplied},
    )
    assert "intent_match" not in ranked["projected"][0]
    assert ranked["ranked"][0]["components"]["intent_match"] == pytest.approx(
        W_M * INTENT_MATCH_WHEN_ABSENT
    )


def test_the_served_auction_ranks_once_and_the_route_holds_no_second_pass() -> None:
    """The redundancy is gone: ONE ``rank()`` per served auction, and it is handed the fit.

    RED before this change on both halves — ``auction/routes.py`` defined ``_with_graph_fit``
    and called the published ``rank()`` itself, and the candidates ``ranking.serving`` handed
    to its own single pass carried no ``intent_match`` at all.
    """
    from exchange.auction import routes as auction_routes
    from exchange.ranking import serving

    assert not hasattr(auction_routes, "_with_graph_fit"), (
        "the auction route still re-ranks the candidates rank_auction returned"
    )
    assert not hasattr(auction_routes, "rank"), (
        "the auction route still imports the published ranker; rank_auction is the seam"
    )

    fits = {"shop-alpha": 0.90, "shop-bravo": 0.55, "shop-charlie": 0.20}
    app = _app(bids={store: _bid(store) for store in fits}, roster_source=_Roster(fits))

    seen: list[list[Any]] = []
    real = serving.rank

    def counting(candidates: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(list(candidates))
        return real(candidates, *args, **kwargs)

    serving.rank = counting  # type: ignore[assignment]
    try:
        body = _post(app)
    finally:
        serving.rank = real  # type: ignore[assignment]

    assert len(seen) == 1, f"the served auction ran the ranker {len(seen)} times"
    handed = {str(c["store_id"]): c.get("intent_match") for c in seen[0]}
    assert handed == pytest.approx(fits), handed

    components = {row["store_id"]: row["components"] for row in body["ranked"]}
    for store, fit in fits.items():
        assert components[store]["intent_match"] == pytest.approx(W_M * fit), components


def test_a_bidder_still_cannot_supply_its_own_intent_match() -> None:
    """R11's blindness, re-asserted over the new seam.

    The projection names its fields, the feature map is the PLATFORM's, and the two are
    applied in that order — so a reply writing the largest published term into itself moves
    nothing. Driven over the HTTP door with a stated roster, so nothing measured a fit and the
    only candidate for the value is the one the store wrote.
    """
    liar, honest = "shop-alpha", "shop-bravo"
    bids = {
        liar: _bid(liar, intent_match=1.0, price_value=1.0, verified_claim_ratio=1.0, trust=1.0),
        honest: _bid(honest),
    }
    app = _app(bids=bids)
    roster = [
        {"store_id": store, "tier": 1, "product_ref": "product-1", "list_price": 120.0}
        for store in bids
    ]
    body = _post(app, roster=roster)

    components = {row["store_id"]: row["components"] for row in body["ranked"]}
    assert set(components) == {liar, honest}, body["excluded"]
    assert components[liar] == components[honest], components
    assert components[liar]["intent_match"] == pytest.approx(W_M * INTENT_MATCH_WHEN_ABSENT)


# =====================================================================================
# 2. The pitch is carried by the projection, not by a test fixture
# =====================================================================================
def test_the_projection_carries_the_message_and_says_so_in_its_field_set() -> None:
    """RED before: ``message`` was neither in ``CANDIDATE_FIELDS`` nor on the record."""
    assert "message" in CANDIDATE_FIELDS
    entry = _Entry("shop-alpha", _bid("shop-alpha", message="a two-year warranty"))
    record = candidate_from_entry(entry, auction_id="auction-1")
    assert tuple(sorted(record)) == tuple(sorted(CANDIDATE_FIELDS))
    assert record["message"] == "a two-year warranty"


def test_a_bid_with_no_message_carries_none_rather_than_an_invented_pitch() -> None:
    record = candidate_from_entry(_Entry("shop-alpha", _bid("shop-alpha")), auction_id="auction-1")
    assert record["message"] is None


def test_the_message_is_still_not_a_published_feature_channel() -> None:
    """The narrow projection stayed narrow: prose got in, a self-graded score did not."""
    entry = _Entry(
        "shop-alpha",
        _bid("shop-alpha", message="hi", intent_match=1.0, price_value=1.0, trust=1.0),
    )
    record = candidate_from_entry(entry, auction_id="auction-1")
    assert set(record) == set(CANDIDATE_FIELDS)
    assert not {"intent_match", "price_value", "trust", "verified_claim_ratio"} & set(record)


def test_the_pitch_reaches_verification_over_the_served_route_with_no_fixture() -> None:
    """§3 of ``test_ranking_verification.py``, driven with the monkeypatch deleted.

    Two stores, identical offers, identical structured claims, identical trust rows, and one
    word of difference in the prose. The exchange decomposes ``Bid.message``, grades it against
    its OWN catalogue snapshot (24 months), and the store that said "five-year" is caught.
    """
    honest, liar = "shop-alpha", "shop-bravo"
    shared = (
        "This is a heat exchange machine sized for an office queue. "
        "It runs a 9 bar pump and comes with a {} warranty."
    )
    bids = {
        honest: _bid(honest, message=shared.format("two-year")),
        liar: _bid(liar, message=shared.format("five-year")),
    }
    app = _app(bids=bids)
    roster = [
        {"store_id": store, "tier": 1, "product_ref": "product-1", "list_price": 120.0}
        for store in bids
    ]
    intent = {**INTENT, "query": "an office espresso machine with a long warranty"}
    response = TestClient(app).post(
        "/auctions", json={"intent": intent, "roster": roster, "bid_timeout_seconds": 2.0}
    )
    assert response.status_code == 201, response.text
    body = response.json()

    rows = {row["store_id"]: row for row in body["ranked"]}
    assert set(rows) == {honest, liar}, body["excluded"]
    assert rows[honest]["rank_score"] > rows[liar]["rank_score"], body["ranked"]

    # The WITNESS, and it is the published penalty rather than a bare score gap: a
    # `contradicted_claim` is the only thing in the formula that can make `policy_penalties`
    # appear, both bids carry the identical structured claim, and nothing but one word of
    # prose differs. `components` sum to `rank_score`, so this is the exchange itself saying
    # which term produced the placement.
    assert rows[liar]["components"].get("policy_penalties", 0.0) < 0.0, rows[liar]
    assert "policy_penalties" not in rows[honest]["components"], rows[honest]
    # And the honest store EARNS on the same sentence: the shopper asked about the warranty,
    # so its verified pitch claim lands on something this buyer actually asked about.
    assert (
        rows[honest]["components"]["verified_claim_ratio"]
        > rows[liar]["components"]["verified_claim_ratio"]
    ), (rows[honest], rows[liar])
