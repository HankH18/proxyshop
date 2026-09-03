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


def test_r10_is_unchanged_for_a_bid_that_declares_no_discount() -> None:
    """The exchange's own auction semantics, pinned by the frozen acceptance suite
    (`test_e3_exchange.py::test_every_store_is_represented_including_silent_and_tier0` keeps an
    80.00 bid against a 120.00 rostered list price). Undercutting your own list price with no
    discount declared is what an auction IS; what the exchange refuses is a store awarding itself
    an authorization nobody granted. So the wall runs on the offers that claim one, and only
    those."""
    for unit in (80.0, 1.0, 0.0):
        entry = only([CAPPED_ROW], [reply(offer(unit, unit))])
        assert entry.fallback is False, (unit, entry.price_reasons)
        assert entry.unit_price == unit

    # A discount declared at exactly zero asserts no authorization either.
    zero = only([CAPPED_ROW], [reply(offer(80.0, 80.0, depth=0.0))])
    assert zero.fallback is False, zero.price_reasons


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
