"""THE GRAPH ON THE SERVED AUCTION — D55's organic half, and the end of a constant term.

Two defects are pinned here, and both were measured on this tree rather than inferred.

**1. The exchange had never read the graph on a real request.**
``apps/exchange/src/retrieval/`` shipped 1,128 lines with ZERO production call sites, and
``GraphCandidateSource`` — this repository's only Neo4j reader — was called by nobody. A
served auction was handed a roster by its caller and queried no index, so the platform had no
organic side at all: it could rank what somebody else had already chosen, and it could not
answer "which shops sell this". Under D55 that is the product's first step, because visibility
is earned by MATCHING and what a shop buys by joining is an advocate, not a place in the list.

**2. ``intent_match`` — ``w_m = 0.35``, the largest weight in the published formula — was a
constant.** Not "usually neutral": the same 0.5 on every candidate of every served auction,
because nothing produced it. ``ranking/features.py`` says so in as many words, and
``test_ranking.py::test_intent_match_has_no_served_producer_and_says_so_by_staying_neutral``
records it with the sentence "the day a retrieval source is wired into the auction route, this
assertion should start failing". It is still green, and that is correct: the source is opt-in
and that test wires none. Section 2 below is the other half of it — the same assertion, driven
over an app that DOES have one, where the constant is gone.

Everything in sections 1-3 runs offline against doubles. Section 4 drives the real Neo4j: a
seeded catalogue, a seeded marketplace, and ``POST /auctions`` with an empty ``roster``.
"""

from __future__ import annotations

import math
import time
from typing import Any

import pytest
from contracts.ranking import DEFAULT_RANKING_WEIGHTS, INTENT_MATCH_WHEN_ABSENT
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from exchange.retrieval import (
    CandidateRetrieval,
    FitAssessment,
    FitFeatures,
    InMemoryCandidateSource,
    NoShopRoster,
    ShopRoster,
    SolicitedShop,
    build_query,
)
from fastapi.testclient import TestClient

W_M = float(DEFAULT_RANKING_WEIGHTS.w_m)

#: The three shops every offline test in sections 1-2 works with.
STORES = ("shop-alpha", "shop-bravo", "shop-charlie")


def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def _shop(store_id: str, *, fit: float, price: float | None, product: str = "product-1"):
    """One row exactly as ``GraphShopRoster`` builds it off the graph."""
    return SolicitedShop(
        store_id=store_id,
        tier=1,
        product_ref=product,
        intent_match=fit,
        domain=_domain(store_id),
        business_identity=f"{store_id} Ltd",
        list_price=price,
        currency=None if price is None else "USD",
        source_ids=(f"src-{store_id}",),
    )


class FakeRoster:
    """A shop-roster source that answers with what a test handed it, and records the ask.

    Deliberately NOT a graph: sections 1-2 are about what the AUCTION ROUTE does with an
    answer, and driving them through Neo4j would make them slow, docker-gated, and unable to
    state a fit score exactly. Section 4 is the same route over the real graph.
    """

    name = "neo4j"

    def __init__(self, shops=(), *, reason: str | None = None, raises: bool = False):
        self.shops = tuple(shops)
        self.reason = reason
        self.raises = raises
        self.asked: list[Any] = []

    def solicit(self, intent, *, limit=None):
        self.asked.append(intent)
        if self.raises:
            raise RuntimeError("the graph fell over")
        return ShopRoster(
            shops=self.shops,
            source=self.name,
            considered=len(self.shops),
            reason=self.reason if not self.shops else None,
            elapsed_ms=1.25,
            # The retrieval's own per-PRODUCT records, exactly as `GraphShopRoster` carries
            # them: one per distinct rostered product, holding the features that were
            # measured rather than a number reassembled from a composite score.
            fit=tuple(
                FitAssessment(
                    product_id=shop.product_ref,
                    canonical_name=shop.product_ref,
                    fit_score=shop.intent_match,
                    features=FitFeatures(similarity=shop.intent_match, preference_alignment=0.5),
                    reranker="deterministic",
                )
                for shop in self.shops
            ),
        )


def _bid(store_id: str, price: float, *, product: str = "product-1"):
    return {
        "auction_id": None,
        "store_id": store_id,
        "offer": {
            "product_ref": product,
            "unit_price": price,
            "total_price": price,
            "currency": "USD",
            "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
            "expires_at": time.time() + 3600.0,
        },
        "claims": [],
        "agent_version": "1.0.0",
        "schema_version": "1.0.0",
    }


def _app(*, bids=None, roster_source=None, stores=STORES, trust=None):
    """A fully wired exchange whose ONLY unusual collaborator is the shop-roster source."""
    answers = dict(bids or {})

    def solicit(store):
        store_id = str(store["store_id"])
        reply = answers.get(store_id)
        if reply is None:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": dict(reply)}

    app = create_app()
    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
        shop_roster=roster_source,
    )
    configure_ranking(
        app,
        trust_snapshot={
            store: {"blacklisted": False, "score": float((trust or {}).get(store, 0.6))}
            for store in stores
        },
        registered_domains=StaticRegisteredDomains({store: _domain(store) for store in stores}),
        catalog=StaticCatalogSnapshots(
            {
                store: {
                    "snapshot_id": f"snap-{store}",
                    "products": [
                        {
                            "product_ref": "product-1",
                            "canonical_name": "product-1",
                            "evidence_ref": f"snap-{store}#product-1",
                            "attributes": {},
                        }
                    ],
                }
                for store in stores
            }
        ),
    )
    return app


#: An intent with no hard constraints — an EMPTY list, never an absent one, because
#: ``read_criteria`` treats an absent ``hard_constraints`` as an unreadable intent and denies
#: every candidate. That is the right rule and it is not what these tests are about.
INTENT = {
    "intent_id": "intent-1",
    "cluster_id": "cluster-1",
    "query": "a 35 litre cabin backpack",
    "hard_constraints": [],
}


def _post(app, *, roster=None, intent=None):
    body: dict[str, Any] = {"intent": dict(intent or INTENT), "bid_timeout_seconds": 2.0}
    if roster is not None:
        body["roster"] = roster
    return TestClient(app).post("/auctions", json=body)


# =====================================================================================
# 1. The graph answers "which shops", and every way of failing is an ANSWER
# =====================================================================================
def test_post_auctions_with_no_roster_asks_the_graph_which_shops_to_solicit() -> None:
    """The spine: a request that names no store is answered from the platform's own crawl.

    Before this wiring the same request opened an auction with nobody in it, because
    ``roster`` defaulted to ``[]`` and nothing else could supply one. What is asserted is the
    whole chain — the source was ASKED with this buyer's intent, the shops it named were
    solicited, their bids were collected, and they were ranked — because "the source has a
    caller" is true of dead code by construction.
    """
    source = FakeRoster(
        [
            _shop("shop-alpha", fit=0.90, price=120.0),
            _shop("shop-bravo", fit=0.60, price=100.0),
        ]
    )
    app = _app(
        bids={"shop-alpha": _bid("shop-alpha", 95.0), "shop-bravo": _bid("shop-bravo", 90.0)},
        roster_source=source,
    )
    posted = _post(app)
    assert posted.status_code == 201, posted.text
    body = posted.json()

    assert [ask["intent_id"] for ask in source.asked] == ["intent-1"], source.asked
    assert body["solicited"] == ["shop-alpha", "shop-bravo"], body["solicited"]
    assert sorted(e["store_id"] for e in body["entries"]) == ["shop-alpha", "shop-bravo"]
    assert sorted(r["store_id"] for r in body["ranked"]) == ["shop-alpha", "shop-bravo"], body
    assert body["roster_source"] == {
        "source": "neo4j",
        "shops": 2,
        "products_considered": 2,
        "reason": None,
        "elapsed_ms": 1.25,
    }, body["roster_source"]


def test_a_stated_roster_never_consults_the_graph() -> None:
    """The compatibility guarantee, asserted by SABOTAGE rather than by inspection.

    The wired source raises on every call, so if the route consulted it for a request that
    named its own stores the auction would be answered from the ``except`` branch and the
    reason would say so. It is not consulted, the auction is exactly the one this exchange
    served before the seam existed, and ``roster_source`` says ``request``.
    """
    exploding = FakeRoster(raises=True)
    roster = [
        {"store_id": "shop-alpha", "tier": 1, "product_ref": "product-1", "list_price": 120.0}
    ]
    app = _app(bids={"shop-alpha": _bid("shop-alpha", 95.0)}, roster_source=exploding)

    posted = _post(app, roster=roster)
    assert posted.status_code == 201, posted.text
    body = posted.json()
    assert exploding.asked == [], "a request that states its own roster asked the graph anyway"
    assert body["solicited"] == ["shop-alpha"]
    assert body["entries"][0]["unit_price"] == 95.0
    assert body["roster_source"] == {
        "source": "request",
        "shops": 1,
        "products_considered": 0,
        "reason": None,
        "elapsed_ms": 0.0,
    }, body["roster_source"]

    # And byte-identically the same auction on an exchange with NO source wired at all — the
    # positive control for "additive", since every other test in this repository is that app.
    plain = _post(_app(bids={"shop-alpha": _bid("shop-alpha", 95.0)}), roster=roster).json()
    assert plain["entries"] == body["entries"]
    assert [r["components"] for r in plain["ranked"]] == [r["components"] for r in body["ranked"]]


def test_an_exchange_with_no_graph_answers_the_auction_and_says_why_it_found_nobody() -> None:
    """An empty roster is a 201 with a REASON, never a 5xx and never a silent empty list.

    Two causes look identical from outside — "the platform knows no shop that sells this" and
    "nobody wired a graph into this exchange" — and a buyer's agent that cannot tell them
    apart will retry the second one forever. The default source names itself.
    """
    posted = _post(_app())
    assert posted.status_code == 201, posted.text
    body = posted.json()
    assert body["state"] == "closed"
    assert body["solicited"] == [] and body["entries"] == [] and body["ranked"] == []
    assert body["roster_source"]["source"] == "unwired"
    assert body["roster_source"]["shops"] == 0
    assert body["roster_source"]["reason"] == NoShopRoster.REASON
    assert "app.state.shop_roster" in body["roster_source"]["reason"]


def test_a_graph_that_finds_nothing_is_distinguishable_from_one_that_is_not_there() -> None:
    """A wired source that matched nobody says something different from an unwired one."""
    empty = FakeRoster(reason="no product in this exchange's catalogue graph matches this intent")
    body = _post(_app(roster_source=empty)).json()
    assert body["roster_source"]["source"] == "neo4j"
    assert body["roster_source"]["reason"] == empty.reason
    assert body["roster_source"]["reason"] != NoShopRoster.REASON
    assert empty.asked, "the wired source was never asked"


def test_a_roster_source_that_raises_cannot_fail_the_auction() -> None:
    """A broken graph must not take down a door that needs no graph at all.

    ``GraphShopRoster`` catches its own failures; this asserts the route does not TRUST that,
    because the seam is public and a deployment may wire something else into it.
    """
    posted = _post(_app(roster_source=FakeRoster(raises=True)))
    assert posted.status_code == 201, posted.text
    reason = posted.json()["roster_source"]["reason"]
    assert "raised rather than answering" in reason, reason
    assert "RuntimeError" in reason, reason


def test_an_unpriced_organic_shop_is_represented_and_is_never_free() -> None:
    """D55's hardest case: a crawled shop whose price the platform never observed.

    ``lowest_price is None`` means "never checked", never "free". The shop is REPRESENTED —
    R10 says a store the exchange selected gets an entry either way, and the organic result is
    the whole reason the graph is being read — and it reaches the buyer carrying **no price**:

    * ``entries`` publishes ``unit_price: null``, not ``0.00``. That is this ticket's own
      repair: ``_entries_out`` read ``float(offer.get("unit_price", 0.0))``, which was
      unreachable while ``RosterEntry.list_price`` was ``Field(gt=0.0)`` and became reachable
      the moment a roster could come from anywhere else.
    * it cannot be ranked, so it cannot win a slot. ``_list_price_bid`` mints no
      ``expires_at`` for an unpriced row and ``ranking.filters.expiry_reason`` fails closed.

    The paired control is the shop next to it, priced, which is ranked normally — so this is a
    statement about the unpriced row rather than about a broken auction.
    """
    source = FakeRoster(
        [
            _shop("shop-alpha", fit=0.90, price=None),
            _shop("shop-bravo", fit=0.60, price=100.0),
        ]
    )
    body = _post(_app(roster_source=source)).json()

    entries = {entry["store_id"]: entry for entry in body["entries"]}
    assert sorted(entries) == ["shop-alpha", "shop-bravo"], entries
    unpriced = entries["shop-alpha"]
    assert unpriced["fallback"] is True
    assert unpriced["unit_price"] is None, f"an unobserved price was published as {unpriced}"
    assert unpriced["total_price"] is None, unpriced
    assert all(
        entry["unit_price"] != 0.0 and entry["total_price"] != 0.0 for entry in body["entries"]
    ), body["entries"]

    assert [row["store_id"] for row in body["ranked"]] == ["shop-bravo"], body["ranked"]
    excluded = {row["store_id"] for row in body["excluded"]}
    assert "shop-alpha" in excluded, body["excluded"]
    assert not body["shortlist"]["slots"] or all(
        slot["bid_ref"].endswith("shop-bravo") for slot in body["shortlist"]["slots"]
    ), body["shortlist"]


def test_a_graph_row_naming_an_absurd_identifier_is_dropped_rather_than_served() -> None:
    """The graph is not a trusted source of NAMES, and this door bypasses ``RosterEntry``.

    ``store_id`` and ``product_ref`` reach the response once per unsatisfied hard constraint
    (``ranking/filters.py`` builds one reason string per failure), and both are written by the
    crawler out of a page a hostile site controls. ``RosterEntry`` bounds both at
    ``MAX_IDENTIFIER_LENGTH`` on the request body; nothing bounded them on the graph body
    until ``_bounded``. The paired control is the ordinary shop beside the oversized one,
    which is solicited normally — so this is a statement about the bad row.
    """
    from exchange.auction.routes import MAX_IDENTIFIER_LENGTH

    source = FakeRoster(
        [
            _shop("x" * (MAX_IDENTIFIER_LENGTH + 1), fit=0.99, price=100.0),
            _shop("shop-alpha", fit=0.50, price=100.0, product="p" * (MAX_IDENTIFIER_LENGTH + 1)),
            _shop("shop-bravo", fit=0.40, price=100.0),
        ]
    )
    body = _post(_app(bids={"shop-bravo": _bid("shop-bravo", 90.0)}, roster_source=source)).json()
    assert body["solicited"] == ["shop-bravo"], body["solicited"]
    assert [e["store_id"] for e in body["entries"]] == ["shop-bravo"], body["entries"]
    assert body["roster_source"]["shops"] == 1, body["roster_source"]


def test_two_identical_requests_against_one_graph_answer_identically() -> None:
    """A shortlist assembled from the graph is reproducible from its inputs (R11/A2).

    The re-rank runs a second ordering pass, so this is the guard against it introducing an
    order that depends on anything but the measurements — dict iteration, insertion order, or
    the instant the request arrived.
    """
    fits = {"shop-alpha": 0.71, "shop-bravo": 0.71, "shop-charlie": 0.30}
    bids = {store: _bid(store, 95.0) for store in fits}

    def once():
        source = FakeRoster([_shop(s, fit=f, price=120.0) for s, f in fits.items()])
        body = _post(_app(bids=bids, roster_source=source)).json()
        return (
            [r["store_id"] for r in body["ranked"]],
            [round(r["rank_score"], 12) for r in body["ranked"]],
            [(sl["slot"], sl["bid_ref"].split(":")[-1]) for sl in body["shortlist"]["slots"]],
        )

    first, second = once(), once()
    assert first == second, (first, second)
    # An exact tie on fit still resolves, and resolves the same way both times.
    assert first[0][:2] == ["shop-alpha", "shop-bravo"], first


# =====================================================================================
# 2. `intent_match` stops being a constant
# =====================================================================================
def _components(body) -> dict[str, dict[str, float]]:
    return {row["store_id"]: row["components"] for row in body["ranked"]}


def test_intent_match_carries_real_per_candidate_variance_on_a_served_auction() -> None:
    """The headline: three candidates, three different fits, three different scores.

    Every served auction in this repository's history answered ``intent_match: 0.5`` for every
    candidate — 35 % of the published score, identical for everyone, so the term discriminated
    nobody and the "best fit" shortlist slot was decided by the tie-breaks. The component is
    ``w_m * intent_match``, so the assertion is exact rather than merely "they differ".
    """
    fits = {"shop-alpha": 0.90, "shop-bravo": 0.55, "shop-charlie": 0.20}
    source = FakeRoster([_shop(store, fit=fit, price=120.0) for store, fit in fits.items()])
    app = _app(
        bids={store: _bid(store, 95.0) for store in fits},
        roster_source=source,
    )
    body = _post(app).json()

    components = _components(body)
    assert sorted(components) == sorted(fits), components
    for store, fit in fits.items():
        assert components[store]["intent_match"] == pytest.approx(W_M * fit), (store, components)
    measured = {round(row["intent_match"], 9) for row in components.values()}
    assert len(measured) == 3, f"the largest term is still a constant: {measured}"
    assert round(W_M * INTENT_MATCH_WHEN_ABSENT, 9) not in measured, components


def test_a_stated_roster_still_reads_the_published_neutral() -> None:
    """The negative control, and the reason the assertion above is about the GRAPH.

    An auction whose roster came from the request body queries no index, so nothing measured
    fit and the term takes ``INTENT_MATCH_WHEN_ABSENT``. That is unchanged, deliberately: a
    number invented for an unmeasured candidate would move rankings for a reason nobody could
    audit, which is worse than the neutral the formula already has a rule for.
    """
    roster = [
        {"store_id": store, "tier": 1, "product_ref": "product-1", "list_price": 120.0}
        for store in STORES
    ]
    app = _app(bids={store: _bid(store, 95.0) for store in STORES})
    components = _components(_post(app, roster=roster).json())
    assert {round(row["intent_match"], 9) for row in components.values()} == {
        round(W_M * INTENT_MATCH_WHEN_ABSENT, 9)
    }, components


def test_a_candidate_the_retrieval_did_not_measure_keeps_the_published_neutral() -> None:
    """A mixed auction records what it measured and no more.

    A store the graph rostered but did not score — and a fallback minted for one — must not be
    handed a fabricated fit. It reads the neutral, exactly as it does on a stated roster,
    while its measured neighbours do not.
    """
    source = FakeRoster([_shop("shop-alpha", fit=0.9, price=120.0)])
    app = _app(bids={"shop-alpha": _bid("shop-alpha", 95.0)}, roster_source=source)
    body = _post(app).json()
    assert _components(body)["shop-alpha"]["intent_match"] == pytest.approx(W_M * 0.9)

    # The same route, same wiring, a source that names a store it did not score. The roster
    # row exists (so the store is solicited and ranked) and the fit map does not mention it.
    class Unscored(FakeRoster):
        def solicit(self, intent, *, limit=None):
            found = super().solicit(intent, limit=limit)
            return ShopRoster(
                shops=(),  # rows still reach the roster below; nothing is scored
                source=found.source,
                considered=found.considered,
                reason=None,
                elapsed_ms=found.elapsed_ms,
            )

    roster = [
        {"store_id": "shop-alpha", "tier": 1, "product_ref": "product-1", "list_price": 120.0}
    ]
    plain = _post(
        _app(bids={"shop-alpha": _bid("shop-alpha", 95.0)}, roster_source=Unscored()),
        roster=roster,
    ).json()
    assert _components(plain)["shop-alpha"]["intent_match"] == pytest.approx(
        W_M * INTENT_MATCH_WHEN_ABSENT
    )


def test_the_measured_fit_actually_decides_the_best_fit_slot() -> None:
    """Sabotage control: invert the fit and the shortlist's fit slot moves with it.

    ``ranking/shortlist.py`` awards the ``fit`` slot on ``intent_match``. With the term
    constant, that slot was decided entirely by the published tie-breaks — so a test asserting
    only that the numbers differ would not show the ranking had changed. This drives the same
    three bids twice, changing nothing but the fit, and asserts the winner swaps.
    """
    bids = {store: _bid(store, 95.0) for store in STORES}

    def slot_winner(fits):
        source = FakeRoster([_shop(store, fit=fit, price=120.0) for store, fit in fits.items()])
        body = _post(_app(bids=bids, roster_source=source)).json()
        by_name = {slot["slot"]: slot for slot in body["shortlist"]["slots"]}
        assert "fit" in by_name, body["shortlist"]
        return by_name["fit"]["bid_ref"], body

    high_alpha, first = slot_winner({"shop-alpha": 0.95, "shop-bravo": 0.30, "shop-charlie": 0.10})
    high_charlie, second = slot_winner(
        {"shop-alpha": 0.10, "shop-bravo": 0.30, "shop-charlie": 0.95}
    )
    assert high_alpha.endswith("shop-alpha"), high_alpha
    assert high_charlie.endswith("shop-charlie"), high_charlie
    assert _components(first) != _components(second)


def test_the_bid_receipt_carries_the_fit_that_produced_the_placement() -> None:
    """R10's receipt stops saying ``fit_unavailable`` once something has been measured.

    ``retrieval/fit.py`` has always specified this shape — ANNOTATE the ``bid_placed`` the
    auction was going to write, never emit a second one, because that kind's count is
    load-bearing in two frozen criteria. The count is asserted here as well, for that reason.
    """
    source = FakeRoster(
        [
            _shop("shop-alpha", fit=0.9, price=120.0, product="product-1"),
            _shop("shop-bravo", fit=0.4, price=120.0, product="product-2"),
        ]
    )
    app = _app(
        bids={
            "shop-alpha": _bid("shop-alpha", 95.0, product="product-1"),
            "shop-bravo": _bid("shop-bravo", 90.0, product="product-2"),
        },
        roster_source=source,
    )
    posted = _post(app)
    auction_id = posted.json()["auction_id"]

    events = [
        event
        for event in app.state.auction_machine.ledger.sink.for_auction(auction_id)
        if event["kind"] == "bid_placed"
    ]
    assert len(events) == 2, [e["kind"] for e in events]
    fits = {event["payload"]["store_id"]: event["payload"]["fit_score"] for event in events}
    assert fits == {"shop-alpha": pytest.approx(0.9), "shop-bravo": pytest.approx(0.4)}, fits
    assert all("fit_unavailable" not in event["payload"] for event in events), events

    # And the control: an auction whose roster came from the body still records the absence.
    stated = _app(bids={"shop-alpha": _bid("shop-alpha", 95.0)})
    body = _post(
        stated,
        roster=[
            {"store_id": "shop-alpha", "tier": 1, "product_ref": "product-1", "list_price": 120.0}
        ],
    ).json()
    receipts = [
        event
        for event in stated.state.auction_machine.ledger.sink.for_auction(body["auction_id"])
        if event["kind"] == "bid_placed"
    ]
    assert receipts and receipts[0]["payload"]["fit_score"] is None, receipts
    assert "fit_unavailable" in receipts[0]["payload"], receipts


def test_the_route_holds_no_second_ranking_pass_and_no_second_shortlist_join() -> None:
    """The drift guard, now that there is only one implementation to drift from.

    This used to compare ``auction/routes.py``'s own copy of the offer-field join against
    ``ranking.serving``'s and assert they agreed — the honest thing to do while the route was
    re-ranking. Both copies existed because ``rank_auction`` had no seam for ``intent_match``,
    so the route applied the term by calling the published ``rank()`` a SECOND time over the
    candidates that function had already returned, and a re-ranked auction has a NEW shortlist
    that needs the join re-applied.

    ``rank_auction(..., intent_match=...)`` is that seam, so the second pass and its join are
    deleted rather than pinned. What is asserted is the stronger property the pin was standing
    in for: the route holds no copy at all, and the published readers it used to build one from
    are no longer even imported here.
    """
    from exchange.auction import routes
    from exchange.ranking import serving

    for gone in (
        "_with_graph_fit",
        "_with_offer_fields",
        "_slot_offer_fields",
        "rank",
        "shortlist_product",
        "shortlist_price",
        "shortlist_commitments",
        "declared_attributes",
        "Shortlist",
    ):
        assert not hasattr(routes, gone), (
            f"auction/routes.py still holds {gone!r}; the second ranking pass is back"
        )
    # And the one implementation is where it always should have been.
    assert callable(serving._with_offer_fields)


# =====================================================================================
# 3. `intent_match` must not become price under a new name (contracts.PREFERENCE_FIELD_TERMS)
# =====================================================================================
def test_build_query_refuses_every_preference_a_published_term_already_scores() -> None:
    """The rule ``contracts.ranking`` publishes, obeyed at the one place fit is produced.

    ``PREFERENCE_FIELD_TERMS`` says a producer of ``intent_match`` drops every preference the
    map answers for, and names the term that took it. Both halves are asserted: the field is
    gone from ``preferences`` (so it cannot reach the score) and it is REPORTED (so a store
    operator asking why their price preference moved nothing can be told).
    """
    intent = {
        "intent_id": "intent-1",
        "query": "a cabin backpack",
        "hard_constraints": [],
        "preferences": [
            {"field": "price", "direction": "minimize", "weight": 1.0},
            {"field": "Delivery Days", "direction": "minimize", "weight": 1.0},
            {"field": "trust_score", "direction": "maximize", "weight": 1.0},
            {"field": "discount_pct", "direction": "maximize", "weight": 1.0},
            {"field": "capacity_l", "direction": "maximize", "weight": 1.0},
        ],
    }
    query = build_query(intent)

    assert [p.field for p in query.preferences] == ["capacity_l"], query.preferences
    assert {(row.field, row.term) for row in query.refused_preferences} == {
        ("price", "price_value"),
        ("Delivery Days", "delivery_fit"),
        ("trust_score", "trust"),
        ("discount_pct", "price_value"),
    }, query.refused_preferences
    assert "price_value" in query.refused_preferences[0].reason


def test_a_price_preference_cannot_move_a_fit_score() -> None:
    """The measured hazard, driven rather than described.

    S1's intent carries exactly ONE preference, ``{price, minimize, 1.0}``, and
    ``_preference_alignments`` min-max normalises across the eligible set — so an
    ``intent_match`` wired naively over that intent IS normalised inverse price, and price's
    share of the published weight goes from ``w_v = 0.15`` to ``w_v + w_m = 0.50``. The three
    candidates below differ ONLY in a ``price`` attribute; with the refusal, their fit is
    identical to what an intent stating no preference at all produces.
    """
    records = [
        {
            "product_id": f"product-{index}",
            "canonical_name": "cabin backpack",
            "attributes": {"price": float(price)},
            "similarity": 0.5,
        }
        for index, price in enumerate((10.0, 50.0, 250.0), start=1)
    ]
    base = {"intent_id": "intent-1", "query": "cabin backpack", "hard_constraints": []}

    def fits(intent):
        result = CandidateRetrieval(InMemoryCandidateSource(records)).retrieve(intent)
        return {a.product_id: a.fit_score for a in result.assessments}

    priced = fits(
        {**base, "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}]}
    )
    silent = fits(base)
    assert priced == silent, (priced, silent)
    assert len(set(priced.values())) == 1, (
        f"price leaked into the fit score: {priced}. intent_match would be inverse price, and "
        f"price's share of the published weight would be w_v + w_m = 0.50"
    )

    # The paired control: a preference NO published term scores still discriminates, so the
    # assertion above is about the refusal rather than about a fit score that never moves.
    weighted = fits(
        {
            **base,
            "preferences": [{"field": "capacity_l", "direction": "maximize", "weight": 1.0}],
        }
    )
    assert weighted == silent, "no candidate carries capacity_l, so nothing to discriminate on"
    discriminating = CandidateRetrieval(
        InMemoryCandidateSource(
            [
                {**record, "attributes": {"capacity_l": 20.0 * index}}
                for index, record in enumerate(records, start=1)
            ]
        )
    ).retrieve(
        {**base, "preferences": [{"field": "capacity_l", "direction": "maximize", "weight": 1.0}]}
    )
    assert len({a.fit_score for a in discriminating.assessments}) == 3, discriminating.assessments


# =====================================================================================
# 4. THE REAL GRAPH. Seeded Neo4j, empty `roster`, one served request.
# =====================================================================================
OBSERVED_AT = "2026-01-01T00:00:00+00:00"

GRAPH_PRODUCTS = (
    {
        "product_id": "prod-trail-shoe",
        "canonical_name": "Trail Running Shoe",
        "brand": "Fellstride",
        "category": "Footwear",
        "attributes": [("drop_mm", {"value_number": 8.0}), ("waterproof", {"value_bool": True})],
        "ingredients": [],
    },
    {
        "product_id": "prod-road-shoe",
        "canonical_name": "Road Running Shoe",
        "brand": "Fellstride",
        "category": "Footwear",
        "attributes": [("drop_mm", {"value_number": 10.0})],
        "ingredients": [],
    },
    {
        "product_id": "prod-espresso",
        "canonical_name": "Espresso Machine",
        "brand": "Bellmark",
        "category": "Appliance",
        "attributes": [("capacity_l", {"value_number": 2.0})],
        "ingredients": [],
    },
)


def _seed_graph(session):
    """A catalogue, three shops over it, and one shop the platform never priced."""
    from ingest.embeddings import get_embedding_provider
    from ingest.graph import (
        AttributeValue,
        Offer,
        Source,
        Store,
        Variant,
        apply_schema,
        link_sells,
        reembed_products,
        seed_products,
        upsert_offer,
        upsert_store,
        upsert_variant,
    )

    apply_schema(session)
    source = Source(
        source_id="src-graph-auction",
        url="https://fellstride.example/products.json",
        content_hash="sha256:" + "a" * 64,
        observed_at=OBSERVED_AT,
        extractor_version="fixture@1",
        confidence=0.9,
        source_class="scraped",
    )
    records = []
    for spec in GRAPH_PRODUCTS:
        record = {key: value for key, value in spec.items() if key != "attributes"}
        record["attributes"] = [AttributeValue(k, **kw) for k, kw in spec["attributes"]]
        records.append(record)
    seed_products(session, records, source=source)
    reembed_products(session, get_embedding_provider())

    for store in (
        Store("shop-fell", "fell.example", "Fellstride Direct", 1),
        Store("shop-summit", "summit.example", "Summit Outfitters", 1),
        Store("shop-quiet", "quiet.example", "Quiet Goods", 2),
        Store("shop-beans", "beans.example", "Bean Machines", 1),
    ):
        upsert_store(session, store, source=source)

    link_sells(session, store_id="shop-fell", product_id="prod-trail-shoe", source=source)
    link_sells(session, store_id="shop-summit", product_id="prod-road-shoe", source=source)
    link_sells(session, store_id="shop-quiet", product_id="prod-trail-shoe", source=source)
    link_sells(session, store_id="shop-beans", product_id="prod-espresso", source=source)

    upsert_variant(
        session,
        Variant("var-trail-42", "SKU-TRAIL-42", "42"),
        product_id="prod-trail-shoe",
        source=source,
    )
    upsert_variant(
        session,
        Variant("var-road-42", "SKU-ROAD-42", "42"),
        product_id="prod-road-shoe",
        source=source,
    )
    upsert_offer(
        session,
        Offer("off-fell-trail", 128.0, "USD", "in_stock", OBSERVED_AT),
        store_id="shop-fell",
        variant_id="var-trail-42",
        source=source,
    )
    upsert_offer(
        session,
        Offer("off-summit-road", 104.0, "USD", "in_stock", OBSERVED_AT),
        store_id="shop-summit",
        variant_id="var-road-42",
        source=source,
    )
    # `shop-quiet` gets NO offer: the crawled shop whose price was never observed (D55).
    return source


def _graph_app(session, *, stores, silent=()):
    from exchange.retrieval.roster import GraphShopRoster

    class _Held:
        """A one-session factory: the test owns the session and the flock, so the source
        must not close it. `GraphShopRoster` closes what its factory hands back, so the
        context manager here is a no-op wrapper rather than the session itself."""

        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self.inner

        def __exit__(self, *exc):
            return False

    # A SILENT store is the organic case: a shop the platform crawled that has no agent of its
    # own, so nothing answers for it and R10 represents it from the catalogue instead.
    bids = {
        store: _bid(store, 95.0, product=product)
        for store, product in stores.items()
        if store not in set(silent)
    }
    app = _app(
        bids=bids, roster_source=GraphShopRoster(lambda: _Held(session)), stores=tuple(stores)
    )
    return app


@pytest.mark.docker
@pytest.mark.graph
def test_the_served_auction_finds_its_own_shops_in_a_seeded_neo4j(neo4j_session) -> None:
    """THE SERVED PROOF. ``POST /auctions`` with no roster, against a real, seeded graph.

    Nothing is stubbed on the read path: a real ``neo4j.Session``, the real
    ``product_embedding`` cosine index, the configured embedding provider (D56's ``lexical``),
    the real ``(:Store)-[:SELLS]->(:Product)`` and ``MAKES_OFFER->Offer->FOR->Variant`` walk,
    and the real ranker. What the request carries is a shopper's sentence and nothing else.
    """
    _seed_graph(neo4j_session)
    stores = {
        "shop-fell": "prod-trail-shoe",
        "shop-summit": "prod-road-shoe",
        "shop-quiet": "prod-trail-shoe",
        "shop-beans": "prod-espresso",
    }
    app = _graph_app(neo4j_session, stores=stores, silent=("shop-quiet",))

    posted = _post(
        app,
        intent={
            "intent_id": "intent-graph-1",
            "cluster_id": "cluster-1",
            "query": "trail running shoes for wet ground",
            "hard_constraints": [],
        },
    )
    assert posted.status_code == 201, posted.text
    body = posted.json()

    assert body["roster_source"]["source"] == "neo4j", body["roster_source"]
    assert body["roster_source"]["reason"] is None, body["roster_source"]
    found = set(body["solicited"]) | {entry["store_id"] for entry in body["entries"]}
    assert "shop-fell" in found, body
    assert found <= set(stores), found

    # The graph's OWN provenance decided the roster, so the shop the crawl never priced is
    # here without a price and the espresso seller is not preferred over the shoe sellers.
    entries = {entry["store_id"]: entry for entry in body["entries"]}
    if "shop-quiet" in entries:
        assert entries["shop-quiet"]["unit_price"] is None, entries["shop-quiet"]
    ranked = {row["store_id"]: row["components"]["intent_match"] for row in body["ranked"]}
    assert ranked, body["ranked"]
    assert all(math.isfinite(value) for value in ranked.values()), ranked
    if "shop-beans" in ranked and "shop-fell" in ranked:
        assert ranked["shop-fell"] > ranked["shop-beans"], ranked


@pytest.mark.docker
@pytest.mark.graph
def test_a_graph_sourced_auction_carries_per_candidate_intent_match(neo4j_session) -> None:
    """The constant is gone on the real graph too, and it is the graph that decided it.

    Two shops selling two DIFFERENT products against one query: the fit that reaches the
    formula is the retrieval's per-product measurement, so the two components differ and
    neither is the published neutral.
    """
    _seed_graph(neo4j_session)
    stores = {"shop-fell": "prod-trail-shoe", "shop-beans": "prod-espresso"}
    app = _graph_app(neo4j_session, stores=stores)

    body = _post(
        app,
        intent={
            "intent_id": "intent-graph-2",
            "cluster_id": "cluster-1",
            "query": "trail running shoe",
            "hard_constraints": [],
        },
    ).json()
    components = _components(body)
    assert len(components) >= 2, body["ranked"]
    measured = {round(row["intent_match"], 9) for row in components.values()}
    assert len(measured) == len(components), f"the graph produced one constant: {components}"
    assert round(W_M * INTENT_MATCH_WHEN_ABSENT, 9) not in measured, components


@pytest.mark.docker
@pytest.mark.graph
def test_an_empty_neo4j_answers_the_auction_instead_of_failing_it(neo4j_session) -> None:
    """The graph is EMPTY until somebody seeds it, and that must not read as a failure.

    ``neo4j_session`` hands back a reset graph, so this is the real driver against a real
    server holding nothing. The auction opens, closes, and says why it found nobody.
    """
    from ingest.graph import apply_schema

    apply_schema(neo4j_session)
    app = _graph_app(neo4j_session, stores={"shop-fell": "prod-trail-shoe"})

    posted = _post(app)
    assert posted.status_code == 201, posted.text
    body = posted.json()
    assert body["state"] == "closed"
    assert body["entries"] == [] and body["ranked"] == []
    assert body["roster_source"]["source"] == "neo4j"
    assert body["roster_source"]["reason"], body["roster_source"]


def test_an_unreachable_graph_answers_the_auction_instead_of_failing_it() -> None:
    """A driver pointed at a closed port is an empty roster with a reason, not a 5xx."""
    from exchange.retrieval.roster import GraphShopRoster

    def broken():
        raise ConnectionRefusedError("nothing is listening on bolt://127.0.0.1:1")

    app = _app(roster_source=GraphShopRoster(broken))
    posted = _post(app)
    assert posted.status_code == 201, posted.text
    reason = posted.json()["roster_source"]["reason"]
    assert "could not be read" in reason and "ConnectionRefusedError" in reason, reason
