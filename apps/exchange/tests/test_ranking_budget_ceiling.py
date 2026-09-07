"""The buyer's price ceiling, enforced against the PRICE BID — driven over the served door.

    PROXYSHOP_WORKER=4 .venv/bin/python -m pytest \
        apps/exchange/tests/test_ranking_budget_ceiling.py -q

**The defect, measured through ``POST /auctions`` before the repair.** With ``price_usd``
declared in the exchange's own catalogue snapshot — so the constraint was *decided* rather
than set aside — a store whose catalogue price is 40.00, claiming ``price_usd: 40`` and
carrying this exchange's own attestation on that claim, **bid 180.00 under a
``price_usd lte 50`` ceiling and took a shortlist slot**::

    excluded:  []
    slot value: {"bid_ref": "…:store-a", "price": {"total_price": 180.0, …}}

R19 was satisfied and the answer was still wrong, because the verified claim describes the
CATALOGUE and the ceiling is about the BID. The two are different numbers and nothing on the
auction path compared the buyer's ceiling to the one the buyer would actually pay. The
shipped demo carries the same shape: ``apps/buyer/devstack/demo-market.json`` states
``price_usd lte 80`` against a catalogue price of 78.00, and the bid could have been anything.

**Why the fix is a separate structural filter and not a hard constraint.** An offer's price is
asserted by the bidder and nothing could verify it — there is no catalogue fact behind "what
I will charge you today". So it is decided at the same gate, from the offer this exchange
holds, and deliberately does NOT feed ``verified_hard_fit_count``: that count is the first
published tie-break (D13), and a tie-break a bidder can set is a tie-break a bidder wins.
For the same reason a ``price_usd`` bound stops being graded against CLAIMS at all — grading
it there is precisely the confusion above, and it is what let a $180 bid answer a $50 question.

**Why ``unanswerable_criteria`` had to move with it.** That function judges answerability from
the catalogue and the claims only. An auction whose every bid is over budget leaves nothing
eligible, and if no catalogue declares ``price_usd`` and no store claims it, ``rank()``'s
relaxation path would call the ceiling unanswerable, set it aside and **re-admit every bid
just excluded** — turning this fix into a no-op in exactly the case it exists for. The
exchange holds every bid's price, so a price bound is answerable by construction and is never
named there. ``test_every_bid_over_budget_empties_the_shortlist_rather_than_relaxing_it``
drives that case; ``test_a_ceiling_is_decided_from_the_bid_when_no_catalogue_declares_a_price``
is its honest-traffic twin and proves the relaxation's *legitimate* rescue was not lost.

Every assertion here drives ``POST /auctions`` and ``GET /auctions/{auction_id}/shortlist`` on
the real ``create_app()`` object, except the two that cannot exist over the door — a bid
carrying an unreadable price, which ``Offer`` forbids at the boundary — and those say so.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.candidates import mint_bid_id
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from fastapi.testclient import TestClient

STORE_A = "store-a"
STORE_B = "store-b"

#: Far enough ahead that nothing here expires while the suite runs.
LIVE_FOR_AN_HOUR = 3600.0

#: The reason prefix a budget refusal must carry, and the one an in-budget bid must never.
BUDGET_PREFIX = "offer_price_outside_budget"
UNREADABLE_PREFIX = "offer_price_unreadable"


# --- builders -------------------------------------------------------------------------
def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def _catalog(prices: dict[str, float | None]) -> Any:
    """A catalogue snapshot per store.

    A price of ``None`` means the snapshot DECLARES ``price_usd`` and holds no value for it;
    leaving a store out of ``prices`` entirely means the snapshot never mentions price at all,
    which is the shape that used to trip the relaxation path.
    """
    products = {}
    for store, price in prices.items():
        attributes: dict[str, Any] = {"capacity_l": {"value": 35}}
        if price is not None:
            attributes["price_usd"] = {"value": price}
        products[store] = {
            "snapshot_id": f"snap-{store}",
            "products": [
                {
                    "product_ref": "product-1",
                    "canonical_name": "product-1",
                    "evidence_ref": f"snap-{store}#product-1",
                    "attributes": attributes,
                }
            ],
        }
    return StaticCatalogSnapshots(products)


def _claim(key: str, value: Any) -> dict[str, Any]:
    """One claim exactly as a BIDDER can write it — no verdict on it (ESC-020)."""
    return {
        "key": key,
        "value": value,
        "provenance": {"source": "owner_statement", "ref": f"ref:{key}", "authority_rank": 1},
    }


def _offer(price: float, store_id: str) -> dict[str, Any]:
    return {
        "product_ref": "product-1",
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
        "expires_at": time.time() + LIVE_FOR_AN_HOUR,
    }


def _bid(store_id: str, price: float, *, claims: list[dict[str, Any]] | None = None) -> dict:
    return {
        "auction_id": None,
        "store_id": store_id,
        "offer": _offer(price, store_id),
        "claims": [_claim("capacity_l", 35)] if claims is None else claims,
        "agent_version": "1.0.0",
        "schema_version": "1.0.0",
    }


class Bidders:
    """The outbound bid client, answering from a table."""

    def __init__(self, bids: dict[str, dict[str, Any]]) -> None:
        self.bids = dict(bids)

    def solicit(self, store: dict[str, Any]) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        bid = self.bids.get(store_id)
        if bid is None:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": dict(bid)}

    __call__ = solicit


def _rostered(store_id: str, list_price: float) -> dict[str, Any]:
    """A roster row stating no authorised discount depth, so the T-177 price wall holds
    nothing to judge and every bid below reaches the ranker as a real bid."""
    return {
        "store_id": store_id,
        "tier": 1,
        "product_ref": "product-1",
        "list_price": list_price,
    }


def _wired_app(
    *,
    bidders: Bidders,
    catalog_prices: dict[str, float | None],
    stores: tuple[str, ...] = (STORE_A, STORE_B),
    trust_snapshot: dict[str, Any] | None = None,
) -> Any:
    app = create_app()
    configure_auctions(
        app,
        solicitor=bidders,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot=(
            {store: {"blacklisted": False, "score": 0.6} for store in stores}
            if trust_snapshot is None
            else trust_snapshot
        ),
        registered_domains=StaticRegisteredDomains({store: _domain(store) for store in stores}),
        catalog=_catalog(catalog_prices),
    )
    return app


def _intent(hard_constraints: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "hard_constraints": hard_constraints,
    }


def _post(app: Any, roster: list[dict[str, Any]], intent: dict[str, Any]) -> dict:
    response = TestClient(app).post(
        "/auctions",
        json={"intent": intent, "roster": roster, "bid_timeout_seconds": 2.0},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _shortlist(app: Any, auction_id: str) -> dict:
    response = TestClient(app).get(f"/auctions/{auction_id}/shortlist")
    assert response.status_code == 200, response.text
    return response.json()


def _reasons_for(body: dict, store_id: str) -> list[str]:
    return [
        reason
        for row in body["excluded"]
        if row["store_id"] == store_id
        for reason in row["exclusion_reasons"]
    ]


def _slot_prices(shortlist: dict) -> list[float | None]:
    return [(slot.get("price") or {}).get("total_price") for slot in shortlist["slots"]]


# =====================================================================================
# 1. The refusal itself
# =====================================================================================
def test_a_bid_above_the_buyers_ceiling_is_excluded_however_verified_its_catalogue_price():
    """The measured defect, asserted as a refusal.

    ``store-a``'s catalogue price is 40.00 and its ``price_usd: 40`` claim carries this
    exchange's own attestation — so R19 was satisfied on verified evidence and the bid was
    still 180.00 against a 50.00 ceiling. The ceiling is about what the buyer pays.
    """
    bidders = Bidders(
        {
            STORE_A: _bid(
                STORE_A, 180.0, claims=[_claim("capacity_l", 35), _claim("price_usd", 40.0)]
            ),
            STORE_B: _bid(
                STORE_B, 45.0, claims=[_claim("capacity_l", 35), _claim("price_usd", 40.0)]
            ),
        }
    )
    app = _wired_app(bidders=bidders, catalog_prices={STORE_A: 40.0, STORE_B: 40.0})

    body = _post(
        app,
        [_rostered(STORE_A, 200.0), _rostered(STORE_B, 50.0)],
        _intent([{"field": "price_usd", "op": "lte", "value": 50.0}]),
    )

    assert [row["store_id"] for row in body["excluded"]] == [STORE_A], body["excluded"]
    reasons = _reasons_for(body, STORE_A)
    assert any(reason.startswith(BUDGET_PREFIX) for reason in reasons), reasons

    # The reason names BOTH numbers. A refusal that says only "over budget" cannot be acted on
    # by the shopper's agent, and this string is the only record of why the bid never scored.
    budget_reason = next(reason for reason in reasons if reason.startswith(BUDGET_PREFIX))
    assert "180" in budget_reason, budget_reason
    assert "50" in budget_reason, budget_reason
    assert "price_usd" in budget_reason, budget_reason

    # And it is off the shortlist, over the published door.
    assert [row["store_id"] for row in body["ranked"]] == [STORE_B], body["ranked"]
    shortlist = _shortlist(app, body["auction_id"])
    assert [slot["bid_ref"] for slot in shortlist["slots"]] == [
        mint_bid_id(body["auction_id"], STORE_B)
    ], shortlist
    assert _slot_prices(shortlist) == [45.0], shortlist


def test_the_ceiling_is_read_off_the_offer_and_not_off_the_stores_claim_about_itself():
    """A store may claim any catalogue price it likes; only the offer decides the ceiling.

    The mirror of the test above. Here the store's claim is *hostile in the other direction* —
    it claims ``price_usd: 400`` while bidding 45.00 — and the bid is in budget, so it is
    shortlisted. Together the two pin that the number being compared is the offer's.
    """
    bidders = Bidders(
        {
            STORE_B: _bid(
                STORE_B, 45.0, claims=[_claim("capacity_l", 35), _claim("price_usd", 400.0)]
            )
        }
    )
    app = _wired_app(bidders=bidders, catalog_prices={STORE_B: 400.0}, stores=(STORE_B,))

    body = _post(
        app,
        [_rostered(STORE_B, 400.0)],
        _intent([{"field": "price_usd", "op": "lte", "value": 50.0}]),
    )

    assert body["excluded"] == [], body["excluded"]
    assert [row["store_id"] for row in body["ranked"]] == [STORE_B], body["ranked"]
    assert _slot_prices(_shortlist(app, body["auction_id"])) == [45.0]


def test_a_priced_bid_never_earns_the_D13_tie_break_from_the_ceiling_it_meets():
    """An in-budget bid is eligible, and its ``verified_hard_fit_count`` is unmoved.

    The price a bidder names is not a verified supporting fact and nothing could verify it, so
    meeting the ceiling must not buy a place in the first published tie-break (D13). Two
    constraints are stated and the count must be **1** — ``capacity_l``, which the catalogue
    really does verify — rather than 2, which is what it would be if the ceiling were also
    being graded as R19 evidence.

    Driven on ``rank()`` rather than over HTTP because the count is not served:
    ``auction.routes.AuctionEntryOut`` publishes ``store_id``/``tier``/``fallback``/prices and
    ``RankedBidOut`` publishes the score and its components, so the number this test is about
    reaches no response body. Every other test in this file drives the door; this one asserts
    the property the door cannot show, and widening a published model to make it visible is a
    change that belongs to whoever owns that model's pinned key set.
    """
    from exchange.ranking import rank  # noqa: PLC0415
    from exchange.ranking.verification import attest_candidates  # noqa: PLC0415

    candidate = {
        "bid_id": "bid-1",
        "store_id": STORE_B,
        "store_domain": _domain(STORE_B),
        "offer": _offer(45.0, STORE_B),
        "claims": [_claim("capacity_l", 35), _claim("price_usd", 40.0)],
    }
    attested = attest_candidates(
        [candidate], catalog=_catalog({STORE_B: 40.0}), product_refs={STORE_B: "product-1"}
    )
    result = rank(
        attested,
        _intent(
            [
                {"field": "capacity_l", "op": "gte", "value": 30},
                {"field": "price_usd", "op": "lte", "value": 50.0},
            ]
        ),
        {STORE_B: {"blacklisted": False, "score": 0.6}},
        {"now": time.time(), "auction_id": "auction-1"},
    )

    row = result["candidates"][0]
    assert row["eligible"], row["exclusion_reasons"]
    assert row["verified_hard_fit_count"] == 1, row

    # The control that makes the 1 mean something: the SAME auction with the ceiling dropped
    # produces the same count, so the ceiling contributed nothing either way.
    without_ceiling = rank(
        attested,
        _intent([{"field": "capacity_l", "op": "gte", "value": 30}]),
        {STORE_B: {"blacklisted": False, "score": 0.6}},
        {"now": time.time(), "auction_id": "auction-1"},
    )
    assert without_ceiling["candidates"][0]["verified_hard_fit_count"] == 1


# =====================================================================================
# 2. Positive controls — the refusal must not eat honest traffic
# =====================================================================================
def test_an_auction_with_no_budget_constraint_is_untouched():
    """The same $180 bid, and no ceiling stated. It ranks, exactly as it did before."""
    bidders = Bidders({STORE_A: _bid(STORE_A, 180.0), STORE_B: _bid(STORE_B, 45.0)})
    app = _wired_app(bidders=bidders, catalog_prices={STORE_A: 40.0, STORE_B: 40.0})

    body = _post(
        app,
        [_rostered(STORE_A, 200.0), _rostered(STORE_B, 50.0)],
        _intent([{"field": "capacity_l", "op": "gte", "value": 30}]),
    )

    assert body["excluded"] == [], body["excluded"]
    assert sorted(row["store_id"] for row in body["ranked"]) == [STORE_A, STORE_B], body["ranked"]
    assert sorted(
        p for p in _slot_prices(_shortlist(app, body["auction_id"])) if p is not None
    ) == [
        45.0,
        180.0,
    ]


def test_a_bid_exactly_at_the_ceiling_is_in_budget():
    """``lte`` is ``<=``. A bid of exactly 50.00 under a 50.00 ceiling is not over budget, and
    an off-by-one here would refuse the shopper the very price they named."""
    bidders = Bidders({STORE_B: _bid(STORE_B, 50.0)})
    app = _wired_app(bidders=bidders, catalog_prices={STORE_B: 50.0}, stores=(STORE_B,))

    body = _post(
        app,
        [_rostered(STORE_B, 50.0)],
        _intent([{"field": "price_usd", "op": "lte", "value": 50.0}]),
    )

    assert body["excluded"] == [], body["excluded"]
    assert _slot_prices(_shortlist(app, body["auction_id"])) == [50.0]


def test_the_shipped_demos_own_winter_hat_auction_still_shortlists_its_bidders():
    """This repo's own demo market, at its own numbers, must still work.

    ``apps/buyer/devstack/demo-market.json`` states ``price_usd lte 80`` and two stores whose
    catalogue price and bid are 78.00 and 72.00. Both are honest traffic under the new filter
    and both must still be shortlisted — a budget filter that closed its hostile case and
    started refusing the shipped demo would be a worse defect than the one it fixed.
    """
    wool, alpine = "demo-woolworks", "demo-alpine-supply"
    bidders = Bidders(
        {
            wool: _bid(wool, 78.0, claims=[_claim("price_usd", 78.0)]),
            alpine: _bid(alpine, 72.0, claims=[_claim("price_usd", 72.0)]),
        }
    )
    app = _wired_app(
        bidders=bidders,
        catalog_prices={wool: 78.0, alpine: 72.0},
        stores=(wool, alpine),
    )

    body = _post(
        app,
        [_rostered(wool, 78.0), _rostered(alpine, 72.0)],
        _intent([{"field": "price_usd", "op": "lte", "value": 80.0}]),
    )

    assert body["excluded"] == [], body["excluded"]
    assert sorted(row["store_id"] for row in body["ranked"]) == [alpine, wool], body["ranked"]
    assert sorted(
        p for p in _slot_prices(_shortlist(app, body["auction_id"])) if p is not None
    ) == [
        72.0,
        78.0,
    ]


def test_a_ceiling_is_decided_from_the_bid_when_no_catalogue_declares_a_price():
    """Honest traffic in the shape that used to be rescued by the relaxation path.

    No catalogue snapshot here declares ``price_usd`` and no store claims it, so before this
    filter existed the ceiling was *unanswerable*, every candidate failed it under R19, and
    ``rank()`` set it aside to avoid publishing an empty shortlist. That rescue must not be
    what is holding these bids up any more — the exchange holds their prices — so they are
    shortlisted, and nothing is reported relaxed.
    """
    bidders = Bidders({STORE_A: _bid(STORE_A, 45.0), STORE_B: _bid(STORE_B, 30.0)})
    app = _wired_app(bidders=bidders, catalog_prices={STORE_A: None, STORE_B: None})

    body = _post(
        app,
        [_rostered(STORE_A, 50.0), _rostered(STORE_B, 50.0)],
        _intent([{"field": "price_usd", "op": "lte", "value": 50.0}]),
    )

    assert body["excluded"] == [], body["excluded"]
    assert sorted(row["store_id"] for row in body["ranked"]) == [STORE_A, STORE_B], body["ranked"]
    assert [entry["field"] for entry in body["relaxed_constraints"]] == [], body[
        "relaxed_constraints"
    ]
    assert sorted(
        p for p in _slot_prices(_shortlist(app, body["auction_id"])) if p is not None
    ) == [
        30.0,
        45.0,
    ]


# =====================================================================================
# 3. The relaxation trap — the case this fix exists for
# =====================================================================================
def test_every_bid_over_budget_empties_the_shortlist_rather_than_relaxing_it():
    """The no-op case, driven.

    Every bid is over budget and no catalogue declares ``price_usd``, which is precisely the
    input that made ``unanswerable_criteria`` call the ceiling undecidable. Setting it aside
    would re-admit both bids at 180.00 and 200.00 against a 50.00 ceiling. The shortlist is
    empty and says so — a real ranking that refused everybody, which is a different answer
    from a 404.
    """
    bidders = Bidders({STORE_A: _bid(STORE_A, 180.0), STORE_B: _bid(STORE_B, 200.0)})
    app = _wired_app(bidders=bidders, catalog_prices={STORE_A: None, STORE_B: None})

    body = _post(
        app,
        [_rostered(STORE_A, 200.0), _rostered(STORE_B, 250.0)],
        _intent([{"field": "price_usd", "op": "lte", "value": 50.0}]),
    )

    assert body["ranked"] == [], body["ranked"]
    assert body["relaxed_constraints"] == [], body["relaxed_constraints"]
    assert sorted(row["store_id"] for row in body["excluded"]) == [STORE_A, STORE_B]
    for store in (STORE_A, STORE_B):
        reasons = _reasons_for(body, store)
        assert any(reason.startswith(BUDGET_PREFIX) for reason in reasons), (store, reasons)

    shortlist = _shortlist(app, body["auction_id"])
    assert shortlist["slots"] == [], shortlist


def test_a_ceiling_no_bid_meets_does_not_drag_a_genuinely_unanswerable_constraint_with_it():
    """The relaxation still works for the constraints it was built for.

    ``brew_method`` is declared by nobody and claimed by nobody — genuinely unanswerable — and
    is set aside as it always was. The ceiling beside it is not, because the exchange can
    answer it; the in-budget bid is shortlisted and the over-budget one stays refused.
    """
    bidders = Bidders({STORE_A: _bid(STORE_A, 180.0), STORE_B: _bid(STORE_B, 45.0)})
    app = _wired_app(bidders=bidders, catalog_prices={STORE_A: 40.0, STORE_B: 40.0})

    body = _post(
        app,
        [_rostered(STORE_A, 200.0), _rostered(STORE_B, 50.0)],
        _intent(
            [
                {"field": "price_usd", "op": "lte", "value": 50.0},
                {"field": "brew_method", "op": "eq", "value": "pour-over"},
            ]
        ),
    )

    assert [entry["field"] for entry in body["relaxed_constraints"]] == ["brew_method"], body[
        "relaxed_constraints"
    ]
    assert [row["store_id"] for row in body["ranked"]] == [STORE_B], body["ranked"]
    assert any(reason.startswith(BUDGET_PREFIX) for reason in _reasons_for(body, STORE_A)), body[
        "excluded"
    ]


# =====================================================================================
# 4. The floor, and the price that cannot be read
# =====================================================================================
def test_a_price_floor_refuses_the_bid_below_it_and_keeps_the_bid_above_it():
    """``fixtures/dialogues/sustainable_daypack_range.json`` ships ``price_usd gte 60`` beside
    its ``lte 120``, so a range is a real intent shape and both ends have to bind."""
    bidders = Bidders({STORE_A: _bid(STORE_A, 45.0), STORE_B: _bid(STORE_B, 100.0)})
    app = _wired_app(bidders=bidders, catalog_prices={STORE_A: 45.0, STORE_B: 100.0})

    body = _post(
        app,
        [_rostered(STORE_A, 50.0), _rostered(STORE_B, 120.0)],
        _intent(
            [
                {"field": "price_usd", "op": "gte", "value": 60.0},
                {"field": "price_usd", "op": "lte", "value": 120.0},
            ]
        ),
    )

    assert [row["store_id"] for row in body["ranked"]] == [STORE_B], body["ranked"]
    reasons = _reasons_for(body, STORE_A)
    assert any(reason.startswith(BUDGET_PREFIX) for reason in reasons), reasons
    assert any("45" in reason and "60" in reason for reason in reasons), reasons
    assert _slot_prices(_shortlist(app, body["auction_id"])) == [100.0]


def test_a_ceiling_that_is_not_a_finite_amount_excludes_rather_than_admitting_everybody():
    """``price_usd lte NaN`` must not be the one ceiling every bid clears.

    ``HardCriterion`` demands a numeric bound for ``lte``/``gte`` and NaN is numeric, so the
    constraint is constructible; ``price > nan`` is ``False``, so a naive comparison admits
    the whole auction. Driven over the door because it is reachable from there, and MEASURED
    rather than assumed: ``json.loads`` — what Starlette parses a request body with — accepts
    the bare ``NaN`` literal, and the auction door answers **201**, so nothing upstream of the
    ranker refuses this and the ranker is where it has to be refused.
    """
    bidders = Bidders({STORE_A: _bid(STORE_A, 180.0)})
    app = _wired_app(bidders=bidders, catalog_prices={STORE_A: 40.0}, stores=(STORE_A,))

    response = TestClient(app).post(
        "/auctions",
        content=json.dumps(
            {
                "intent": _intent([{"field": "price_usd", "op": "lte", "value": float("nan")}]),
                "roster": [_rostered(STORE_A, 200.0)],
                "bid_timeout_seconds": 2.0,
            }
        ),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["ranked"] == [], body["ranked"]
    reasons = _reasons_for(body, STORE_A)
    assert any(reason.startswith(UNREADABLE_PREFIX) for reason in reasons), reasons


@pytest.mark.parametrize("price", [float("nan"), float("inf")])
def test_a_bid_whose_price_cannot_be_read_loses_the_budget_filter(price: float) -> None:
    """No readable price EXCLUDES.

    Driven at the ranker rather than over HTTP on purpose: ``contracts.protocol.Offer``
    declares ``unit_price`` and ``total_price`` as required floats, so the door refuses a bid
    with no price at all and this shape can only arrive from a collaborator inside the
    process. It still has to fail closed — a bid whose price cannot be compared must not win a
    price filter by being unreadable, which is the direction R12 already fails in.
    """
    from exchange.ranking import rank  # noqa: PLC0415
    from exchange.retrieval.criteria import HardCriterion  # noqa: PLC0415

    candidate = {
        "bid_id": "bid-1",
        "store_id": STORE_B,
        "store_domain": _domain(STORE_B),
        "offer": _offer(price, STORE_B),
        "claims": [],
    }
    result = rank(
        [candidate],
        {"hard_constraints": [{"field": "price_usd", "op": "lte", "value": 50.0}]},
        {STORE_B: {"blacklisted": False, "score": 0.6}},
        {"now": time.time(), "auction_id": "auction-1"},
    )

    assert result["ranked"] == [], result["ranked"]
    reasons = result["candidates"][0]["exclusion_reasons"]
    assert any(reason.startswith(UNREADABLE_PREFIX) for reason in reasons), reasons
    # And the criterion itself is decidable — a price bound is never handed to the relaxation.
    from exchange.ranking.filters import unanswerable_criteria  # noqa: PLC0415

    ceiling = HardCriterion(field="price_usd", op="lte", value=50.0)
    assert unanswerable_criteria([candidate], [ceiling], [{"key": "capacity_l"}]) == []
