"""T-177 at the exchange's own bid intake — the door that is actually on in production.

`contracts.boundary` grew the wall; nothing called it. `git grep list_prices` across the tree
found the parameter, its tests and a docstring, and no caller at all: the protection was an
option, and the default was the hole. This file pins the wiring, because a wall nobody runs is
indistinguishable from no wall.

`collect_bids` is where it belongs: it is the only place in the exchange holding both halves of
the comparison — the store's answer, and the roster row that store was asked from, carrying the
product's `list_price` and the `max_discount_pct` the merchant's approved envelope permits.

Every rejection below is paired with a positive control. A wall that refused every discounted bid
would satisfy the rejections and destroy the auction, and one case here (`R10 unchanged`) is the
frozen acceptance suite's own contract restated: undercutting your list price with no discount
declared is what an auction IS, and it must stay admitted.
"""

from __future__ import annotations

from typing import Any

import pytest

NOW = 1_700_000_000.0

HOOK_PROVENANCE: dict[str, Any] = {
    "source": "envelope_rule",
    "ref": "envelope:store-alpha:v3#max_discount_pct@prod-1",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 1,
}

#: A rostered store the exchange can both price AND authorize: 100.00 list, 20% permitted.
CAPPED_ROW: dict[str, Any] = {
    "store_id": "store-1",
    "tier": 1,
    "product_ref": "prod-1",
    "list_price": 100.0,
    "max_discount_pct": 20.0,
}

#: The same row with the authorization taken out. It prices the product and permits no discount.
UNCAPPED_ROW: dict[str, Any] = {
    "store_id": "store-1",
    "tier": 1,
    "product_ref": "prod-1",
    "list_price": 100.0,
}


def offer(unit: Any, total: Any, depth: Any = None, product_ref: str = "prod-1") -> dict:
    body: dict[str, Any] = {
        "product_ref": product_ref,
        "unit_price": unit,
        "total_price": total,
        "currency": "USD",
        "expires_at": "2999-01-01T00:00:00Z",
    }
    if depth is not None:
        body["discount"] = {
            "type": "percentage",
            "value": depth,
            "provenance": dict(HOOK_PROVENANCE),
        }
    return body


def reply(priced: dict, store_id: str = "store-1") -> dict:
    """A well-formed, on-time answer from `store_id`. Nothing here is late or malformed."""
    return {
        "store_id": store_id,
        "received_at": NOW - 1.0,
        "bid": {
            "auction_id": "auc-1",
            "store_id": store_id,
            "offer": priced,
            "claims": [],
        },
    }


def collect(roster: list[dict], responses: list[dict]):
    from exchange.auction import collect_bids  # noqa: PLC0415

    return list(collect_bids(roster, responses, NOW))


def only(roster: list[dict], responses: list[dict]):
    entries = collect(roster, responses)
    assert len(entries) == 1, entries
    return entries[0]


def test_the_module_under_test_is_this_worktree() -> None:
    """A green run that imported the PRIMARY checkout's `collect.py` is evidence about that tree."""
    from pathlib import Path

    from exchange.auction import collect  # noqa: PLC0415

    here = Path(__file__).resolve().parents[3]
    resolved = Path(collect.__file__).resolve()
    assert here in resolved.parents, f"graded {resolved}, not {here}"


# --- the attack, through the exchange's own intake --------------------------------------------


def test_a_bid_that_underprices_its_declared_discount_falls_back_to_list_price() -> None:
    """THE T-177 REPRODUCTION at the production door.

    Everything about this answer is in order: it arrived inside the window, it parses, the
    discount is hook-provenanced and legibly 20%, the offer is unexpired. It charges 15.00 for a
    product the exchange's own roster prices at 100.00 — 85% off behind a 20% authorization — and
    before this the exchange ranked it, because the only wall comparing what a bid charges with
    what it declares lived inside our store-agent runtime, which a Tier-2 store never runs.
    """
    entry = only([CAPPED_ROW], [reply(offer(15.0, 15.0, depth=20.0))])

    assert entry.fallback is True
    assert entry.fallback_reason == "bid_price_unreconcilable"
    assert entry.price_reasons == ["price_under_declared_depth:offer.unit_price"]
    # It is represented, at the catalog price — R10's own degradation, not a hole in the auction.
    assert entry.unit_price == 100.0


@pytest.mark.parametrize("depth", [20.001, 25.0, 85.0, 99.0, 100.0])
def test_a_deeper_declaration_does_not_buy_a_deeper_discount(depth: float) -> None:
    """The move after the first one, and the one the roster alone did not stop: keep the price at
    15.00 and simply write a bigger number in `discount.value`. The depth is the bid's own claim;
    the cap is the merchant's, and only the cap may bound the price."""
    entry = only([CAPPED_ROW], [reply(offer(15.0, 15.0, depth=depth))])

    assert entry.fallback is True
    assert entry.fallback_reason == "bid_price_unreconcilable"
    assert "discount_over_authorized_depth:offer.discount" in entry.price_reasons


def test_the_free_item_at_a_declared_hundred_percent_is_refused() -> None:
    """100% off is a depth like any other, and it is the one that costs the most."""
    entry = only([CAPPED_ROW], [reply(offer(0.0, 0.0, depth=100.0))])

    assert entry.fallback is True
    assert entry.unit_price == 100.0
    assert "discount_over_authorized_depth:offer.discount" in entry.price_reasons


def test_an_illegible_depth_is_not_an_absent_one() -> None:
    """`"85"` is a string a seller wrote, not a depth. A bid that makes its own paperwork
    unreadable must not thereby skip the wall — that is the cheapest possible bypass."""
    entry = only([CAPPED_ROW], [reply(offer(15.0, 15.0, depth="85"))])

    assert entry.fallback is True
    assert entry.price_reasons == ["price_unreconcilable:offer.discount:depth_not_a_number"]


def test_a_bid_about_a_product_the_store_was_not_asked_about_finds_no_row() -> None:
    """The roster is keyed by the ROW's `product_ref`, not the offer's. Keyed the other way every
    bid would find a row whatever product it named, and the bid would be choosing which catalog
    entry it is measured against."""
    entry = only(
        [dict(CAPPED_ROW, product_ref="prod-other")],
        [reply(offer(15.0, 15.0, depth=20.0, product_ref="prod-1"))],
    )

    assert entry.fallback is True
    assert "price_unreconcilable:offer.unit_price:list_price_unavailable" in entry.price_reasons


# --- the positive controls ---------------------------------------------------------------------


def test_the_honest_discounted_bid_is_kept() -> None:
    """A wall that refuses every discounted bid is not a wall, it is an outage. An authorized 20%
    off 100.00 prices out at 80.00, and that bid is ranked."""
    entry = only([CAPPED_ROW], [reply(offer(80.0, 80.0, depth=20.0))])

    assert entry.fallback is False
    assert entry.fallback_reason is None
    assert entry.price_reasons == []
    assert entry.unit_price == 80.0


def test_r10_still_admits_an_undeclared_undercut_where_nothing_is_authorized() -> None:
    """EXACTLY what the frozen acceptance suite requires, and no wider.

    `test_e3_exchange.py::test_every_store_is_represented_including_silent_and_tier0` rosters
    `{"store_id": "store-r1", "tier": 1, "product_ref": "product-1", "list_price": 120.0}` — a
    list price and NO `max_discount_pct` — answers it with `{"product_ref": "product-1",
    "unit_price": 80.0, "total_price": 80.0}` carrying no `discount` at all, and then asserts
    `fallback is False` and `_unit_price(...) == 80.0`. A 33.3% undeclared undercut on a row that
    authorizes nothing is therefore REQUIRED to be admitted, and with no authorized depth on the
    row there is no line the exchange can draw between that and an unauthorized discount.

    A previous pass of this ticket read that frozen case as also licensing 0.00 and 1.00 on a
    CAPPED row and pinned all three as admitted. It does not: the frozen roster row states no
    cap, and no case in any acceptance suite offers a zero price at all. The two tests below
    hold the ground that reading gave away."""
    for unit in (99.0, 80.0, 1.0):
        entry = only([UNCAPPED_ROW], [reply(offer(unit, unit))])
        assert entry.fallback is False, (unit, entry.price_reasons)
        assert entry.unit_price == unit

    # A discount declared at exactly zero asserts no authorization either.
    zero = only([UNCAPPED_ROW], [reply(offer(80.0, 80.0, depth=0.0))])
    assert zero.fallback is False, zero.price_reasons


def test_an_undeclared_price_is_measured_against_the_cap_the_roster_states() -> None:
    """Silence is not an exemption. The wall used to run only on offers DECLARING a discount, so
    a store that omitted the `discount` block entirely and charged 15.00 for a 100.00 product was
    admitted at the door — while the boundary, handed that same bid and that same row, refuses it
    `price_under_declared_depth:offer.unit_price`. The refusal existed; this module declined to
    ask for it.

    An implicit depth is not a different animal from a declared one, so where the roster STATES
    what is authorized, the undeclared price is measured against exactly that. Both directions are
    pinned here: the honest 80.00 (precisely the 20% the row authorizes, filed with no paperwork)
    must still be admitted, or the wall is closed rather than fail-closed."""
    for unit in (100.0, 80.0):
        admitted = only([CAPPED_ROW], [reply(offer(unit, unit))])
        assert admitted.fallback is False, (unit, admitted.price_reasons)
        assert admitted.unit_price == unit

    for unit in (79.0, 15.0, 1.0):
        refused = only([CAPPED_ROW], [reply(offer(unit, unit))])
        assert refused.fallback is True, (unit, refused.price_reasons)
        assert refused.fallback_reason == "bid_price_unreconcilable"
        assert "price_under_declared_depth:offer.unit_price" in refused.price_reasons
        assert refused.unit_price == 100.0

    # The same on the total: an 80.00 unit with a 0.00 total is the free item wearing the other
    # field, and the total relation only reaches it because the cap is now read at a zero depth.
    split = only([CAPPED_ROW], [reply(offer(80.0, 0.0))])
    assert split.fallback is True, split.price_reasons
    assert "price_unreconcilable:offer.total_price:not_positive" in split.price_reasons


def test_the_free_item_is_refused_on_a_row_that_authorizes_nothing_at_all() -> None:
    """THE FLOOR, and the reason it is not built out of the cap.

    Everything else in the price walk is an inequality against the authorized depth, so a roster
    row saying `max_discount_pct: 100` satisfies all of them at once — `100.00 * (100 - 100) / 100`
    is 0.00 — and `max_discount_pct` arrives on an unauthenticated request body. A wall whose
    deepest setting is "free" is not a wall. A price of nothing is not a deep discount; it is the
    absence of a price, and no authorization makes a product free.

    So it is refused on a row that authorizes nothing, on a row that authorizes 20%, and on a row
    that authorizes everything — declared or silent."""
    for row in (
        UNCAPPED_ROW,
        CAPPED_ROW,
        dict(CAPPED_ROW, max_discount_pct=100.0),
    ):
        for depth in (None, 100.0):
            entry = only([row], [reply(offer(0.0, 0.0, depth=depth))])
            assert entry.fallback is True, (row.get("max_discount_pct"), depth)
            assert entry.fallback_reason == "bid_price_unreconcilable"
            assert "price_unreconcilable:offer.unit_price:not_positive" in entry.price_reasons
            assert entry.unit_price == 100.0, "the refused store is still represented, at list"


def test_a_negative_price_is_refused_and_named_as_negative_not_as_nothing() -> None:
    """Paying the buyer to take it is the free item with a minus sign, and it was admitted for the
    same reason: no depth was declared, so nothing ran. It keeps its own `:negative` name — one bad
    number reported twice is the mislabelling the fallback reasons were split apart to end."""
    entry = only([UNCAPPED_ROW], [reply(offer(-5.0, -5.0))])
    assert entry.fallback is True
    assert "price_unreconcilable:offer.unit_price:negative" in entry.price_reasons
    assert "price_unreconcilable:offer.unit_price:not_positive" not in entry.price_reasons


def test_a_roster_that_prices_a_product_at_zero_has_no_free_item_to_detect() -> None:
    """The floor is a comparison against the roster, not a rule that prices must be positive. A
    catalog that genuinely lists something at 0.00 is not describing a giveaway the exchange is
    being tricked into."""
    free_row = dict(UNCAPPED_ROW, list_price=0.0)
    entry = only([free_row], [reply(offer(0.0, 0.0))])
    assert entry.fallback is False, entry.price_reasons


def test_an_unreadable_price_degrades_the_store_instead_of_raising() -> None:
    """`BidEntry.unit_price` calls `float()` on whatever the store sent, so an unreadable price used
    to be a `ValueError` out of the middle of an auction every other store was bidding in — the
    same shape of outage the unparseable arrival stamp caused. It is a fallback now."""
    for junk in ("cheap", None, float("nan")):
        entry = only([UNCAPPED_ROW], [reply(offer(junk, junk))])
        assert entry.fallback is True, junk
        assert entry.unit_price == 100.0


def test_a_depth_exactly_at_the_authorized_cap_is_authorized() -> None:
    """`authorize_discount` grants AT `max_discount_pct` and denies above it. The emitting wall
    and this one must agree about that boundary, or a bid our own runtime minted is refused by
    the door that checks it."""
    entry = only([CAPPED_ROW], [reply(offer(80.0, 80.0, depth=20.0))])
    assert entry.fallback is False, entry.price_reasons


# --- the fail-closed direction -----------------------------------------------------------------


def test_a_roster_row_that_authorizes_no_depth_refuses_a_declared_discount() -> None:
    """A cap that can be omitted is a cap an attacker omits. A row that prices a product and says
    nothing about how deep a discount may go does not fall back to trusting the bid's number: the
    authorized depth is unknown, and unknown fails closed at zero.

    The cost is bounded and visible — the store is represented at its list price — and the same
    row still admits every undiscounted bid, which is the paired control below."""
    refused = only([UNCAPPED_ROW], [reply(offer(80.0, 80.0, depth=20.0))])
    assert refused.fallback is True
    assert (
        "price_unreconcilable:offer.discount:authorized_depth_unavailable" in refused.price_reasons
    )

    admitted = only([UNCAPPED_ROW], [reply(offer(80.0, 80.0))])
    assert admitted.fallback is False, admitted.price_reasons
    assert admitted.unit_price == 80.0


def test_the_new_reason_is_in_the_published_vocabulary_and_is_not_a_transport_fault() -> None:
    """`fallback_reason` is a fixed vocabulary a loss report aggregates on. This refusal is a
    POLICY one — the store answered in time with a well-formed body — so it must not be reported
    as lateness or as a malformed reply, which is the mislabel this module was already fixed for
    once."""
    from exchange.auction import (  # noqa: PLC0415
        FALLBACK_REASONS,
        MALFORMED_RESPONSE_REASONS,
        UNRECONCILABLE_PRICE_REASON,
    )

    assert UNRECONCILABLE_PRICE_REASON == "bid_price_unreconcilable"
    assert UNRECONCILABLE_PRICE_REASON in FALLBACK_REASONS
    assert UNRECONCILABLE_PRICE_REASON not in MALFORMED_RESPONSE_REASONS
    assert "response_after_deadline" != UNRECONCILABLE_PRICE_REASON


def test_every_rostered_store_is_still_represented_exactly_once() -> None:
    """R10 is not negotiable: refusing a bid must not delete its store from the auction, or a
    seller could vanish a rival by making it bid badly."""
    roster = [
        dict(CAPPED_ROW, store_id="store-honest"),
        dict(CAPPED_ROW, store_id="store-liar"),
        dict(CAPPED_ROW, store_id="store-silent"),
        dict(CAPPED_ROW, store_id="store-tier0", tier=0),
    ]
    entries = collect(
        roster,
        [
            reply(offer(80.0, 80.0, depth=20.0), store_id="store-honest"),
            reply(offer(15.0, 15.0, depth=20.0), store_id="store-liar"),
        ],
    )

    assert [e.store_id for e in entries] == [r["store_id"] for r in roster]
    by_store = {e.store_id: e for e in entries}
    assert by_store["store-honest"].fallback is False
    assert by_store["store-liar"].fallback_reason == "bid_price_unreconcilable"
    assert by_store["store-silent"].fallback_reason == "no_response"
    assert by_store["store-tier0"].fallback_reason == "tier_0_no_agent"
    # A store refused on price is not reported as one that never answered.
    assert by_store["store-liar"].fallback_reason != by_store["store-silent"].fallback_reason


def test_the_wall_never_raises_on_a_hostile_offer() -> None:
    """`collect_bids` is on the request path for a whole auction. One malformed reply took the
    auction down once already (the unparseable arrival stamp); a price walk that threw would do
    it again, for every other store bidding alongside it."""
    hostile: list[Any] = [
        {"discount": {"type": "percentage", "value": 20.0}},
        {"product_ref": ["prod-1"], "unit_price": 1.0, "discount": {"value": 50.0}},
        {"unit_price": float("nan"), "discount": {"type": "pct", "value": 50.0}},
        {"discount": "twenty percent"},
        {"discount": {"value": None}},
    ]
    for body in hostile:
        entries = collect([CAPPED_ROW], [reply(body)])
        assert len(entries) == 1, body
        # Every one of them is refused or admitted, never an exception and never a missing store.
        assert entries[0].store_id == "store-1"


# =====================================================================================
# THROUGH `POST /auctions` — the door that is actually on in production
#
# Every test above drives `collect_bids` directly, which is one import away from the route. The
# verifier that found the two defects this file now pins measured them HERE instead, and got
# `HTTP 201, entries=[{fallback: false, unit_price: 0.0}]` for a free item. A wall proved only at
# the library boundary is a wall whose wiring is untested, which is exactly how T-177 shipped the
# first time — `git grep list_prices` found the parameter, its tests, and no caller.
# =====================================================================================
def door(row: dict, priced: dict):
    """POST one auction with one rostered store answering `priced`, and return its entry."""
    from exchange.auction.routes import configure_auctions  # noqa: PLC0415
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility  # noqa: PLC0415
    from exchange.main import create_app  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    class Solicitor:
        def solicit(self, store: Any) -> dict:
            store_id = store["store_id"] if isinstance(store, dict) else store.store_id
            return {
                "store_id": store_id,
                "bid": {
                    "auction_id": "auc-1",
                    "store_id": store_id,
                    "offer": dict(priced),
                    "claims": [],
                },
            }

        __call__ = solicit

    app = create_app()
    configure_auctions(
        app,
        solicitor=Solicitor(),
        eligibility=StaticSellerEligibility({row["store_id"]: ELIGIBLE}),
    )
    posted = TestClient(app).post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
            "roster": [row],
            "bid_timeout_seconds": 2.0,
        },
    )
    assert posted.status_code == 201, posted.text
    entries = posted.json()["entries"]
    assert len(entries) == 1, entries
    return entries[0]


@pytest.mark.parametrize(
    ("name", "row", "priced"),
    [
        # The ticket's own free item, in the spelling that DECLARES the authorization...
        (
            "declared_100_at_a_cap_of_100",
            dict(CAPPED_ROW, max_discount_pct=100.0),
            offer(0.0, 0.0, depth=100.0),
        ),
        ("declared_100_at_a_cap_of_20", CAPPED_ROW, offer(0.0, 0.0, depth=100.0)),
        # ...and in the spelling that declares NOTHING, which is the one that walked through.
        ("silent_zero_on_a_capped_row", CAPPED_ROW, offer(0.0, 0.0)),
        ("silent_zero_on_an_uncapped_row", UNCAPPED_ROW, offer(0.0, 0.0)),
        # The undeclared 85% undercut the boundary already refused and the route did not ask about.
        ("silent_undercut_past_the_cap", CAPPED_ROW, offer(15.0, 15.0)),
    ],
)
def test_the_free_item_is_refused_through_the_real_http_door(
    name: str, row: dict, priced: dict
) -> None:
    """Measured before this ticket's third pass, through this exact call:

        HTTP 201  entries=[{store_id: 's1', fallback: false, unit_price: 0.0, fallback_reason: null}]

    for `silent_zero_on_a_capped_row`, `silent_zero_on_an_uncapped_row`,
    `silent_undercut_past_the_cap` and `declared_100_at_a_cap_of_100`.
    """
    entry = door(row, priced)
    assert entry["fallback"] is True, (name, entry)
    assert entry["fallback_reason"] == "bid_price_unreconcilable", (name, entry)
    assert entry["unit_price"] == 100.0, (name, entry)
    assert entry["total_price"] == 100.0, (name, entry)


@pytest.mark.parametrize(
    ("name", "row", "priced", "expected"),
    [
        # R10's own shape, at the route: a list price, no authorized depth, an undeclared undercut.
        (
            "r10_undeclared_undercut",
            {"store_id": "store-1", "tier": 1, "product_ref": "prod-1", "list_price": 120.0},
            offer(80.0, 80.0),
            80.0,
        ),
        # The honest discounted bid, filed with paperwork...
        ("honest_declared_discount", CAPPED_ROW, offer(80.0, 80.0, depth=20.0), 80.0),
        # ...and the same price filed with none.
        ("honest_undeclared_discount", CAPPED_ROW, offer(80.0, 80.0), 80.0),
    ],
)
def test_the_door_still_admits_the_bids_r10_requires(
    name: str, row: dict, priced: dict, expected: float
) -> None:
    """The paired control. A wall that refused every cheap bid would satisfy every rejection above
    and destroy the auction, and `r10_undeclared_undercut` is the frozen acceptance case's own
    shape driven through the route rather than through `collect_bids`."""
    entry = door(row, priced)
    assert entry["fallback"] is False, (name, entry)
    assert entry["fallback_reason"] is None, (name, entry)
    assert entry["unit_price"] == expected, (name, entry)


def post_roster(row: dict):
    """POST one auction with `row` and nobody answering; return (status, body)."""
    from exchange.auction.routes import configure_auctions  # noqa: PLC0415
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility  # noqa: PLC0415
    from exchange.main import create_app  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    app = create_app()
    configure_auctions(app, eligibility=StaticSellerEligibility({row.get("store_id"): ELIGIBLE}))
    posted = TestClient(app).post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
            "roster": [row],
            "bid_timeout_seconds": 0.2,
        },
    )
    return posted.status_code, posted.json()


def test_a_roster_row_that_prices_nothing_cannot_open_an_auction() -> None:
    """The free item that needs no bid at all.

    `list_price` used to default to 0.00, so a roster row naming no price produced a 0.00
    *fallback* offer for a silent store — and a fallback is a real, rankable offer, so it wins
    every ranking there is. Measured before this change, with no solicitor wired:

        POST /auctions  roster=[{"store_id": "s1", "tier": 1, "product_ref": "prod-1"}]
        ->  HTTP 201  entries=[{fallback: true, unit_price: 0.0, fallback_reason: "no_response"}]

    A caller that cannot price a product cannot auction it. The whole roster arrives on an
    unauthenticated body, so this is not a defence against a forged price — it is a refusal to
    mint one out of a missing field."""
    status, body = post_roster({"store_id": "s1", "tier": 1, "product_ref": "prod-1"})
    assert status == 422, body
    assert body["detail"][0]["msg"] == "Field required"
    assert body["detail"][0]["loc"][-1] == "list_price"


def test_a_negative_list_price_and_an_impossible_cap_are_refused_at_the_door() -> None:
    """A negative list price is a fallback that pays the buyer to take the product, and a cap
    outside 0..100 is not a percentage. The `Envelope` schema publishes `max_discount_pct` with
    `minimum: 0, maximum: 100`; the route that accepts one on an unauthenticated body must not be
    laxer than the schema the number is named after."""
    status, body = post_roster(
        {"store_id": "s1", "tier": 1, "product_ref": "prod-1", "list_price": -5.0}
    )
    assert status == 422, body
    assert body["detail"][0]["loc"][-1] == "list_price"

    status, body = post_roster(
        {
            "store_id": "s1",
            "tier": 1,
            "product_ref": "prod-1",
            "list_price": 100.0,
            "max_discount_pct": 500.0,
        }
    )
    assert status == 422, body
    assert body["detail"][0]["loc"][-1] == "max_discount_pct"

    # The paired control: a well-formed row still opens an auction.
    status, body = post_roster(
        {"store_id": "s1", "tier": 1, "product_ref": "prod-1", "list_price": 100.0}
    )
    assert status == 201, body
    assert body["entries"][0]["unit_price"] == 100.0


def test_an_offer_the_boundary_cannot_read_at_all_is_not_thereby_admitted() -> None:
    """The free item reached by sending NO price rather than a cheap one.

    `contracts.boundary.price_reasons` answers an offer it cannot read as a record — `"offer":
    []`, `"offer": "free"`, no `offer` key — with an empty list: a boundary that cannot find an
    offer has nothing to say about one. An empty list means "no refusal", so the store was
    admitted, and `BidEntry.unit_price` then read `float(offer.get("unit_price", 0.0))` off an
    empty dict. Measured on a row listing at 100.00 before this:

        offer=[]  ->  fallback=False  unit_price=0.0  price_reasons=[]

    The exchange does not get to read the boundary's silence as an admission."""
    from exchange.auction import ILLEGIBLE_OFFER_REASON  # noqa: PLC0415

    for body in ([], "free", None, 42):
        entry = only([CAPPED_ROW], [reply(body)])
        assert entry.fallback is True, body
        assert entry.fallback_reason == "bid_price_unreconcilable", body
        assert entry.price_reasons == [ILLEGIBLE_OFFER_REASON], body
        assert entry.unit_price == 100.0, body

    # It is named in the boundary's own vocabulary, and it is not one of the fixed fallback
    # reasons a loss report aggregates on — that stays `bid_price_unreconcilable`.
    assert ILLEGIBLE_OFFER_REASON == "price_unreconcilable:offer:illegible"
