"""A shopper's "under $50" must not disqualify a $25 product.

The single most common thing a shopper says is a price ceiling. It reaches a store agent as a
hard constraint whose field is spelled the way `packages/contracts`' published buyer OpenAPI
spells it -- ``price_usd`` -- while a store's catalog carries the number under ``list_price``.
This runtime looked the constraint up by its own spelling, found nothing, and applied R19's
correct rule ("unverified data cannot satisfy a hard constraint") to a fact the catalog
actually had and had ALREADY READ two lines earlier.

The result was not a wrong price. It was every hosted store declining ``no_matching_product``
on every priced query, so the market served catalogue prices only while every agent answered
200 and every container reported healthy -- this repository's recurring shape, and the
honest-traffic direction of a filter written to keep unevidenced claims out.

Measured against the running gaiaherbs container, one bid request differing in nothing but the
field name::

    field=list_price   http=200   a real bid, unit_price 25.49
    field=price        http=204   declined
    field=unit_price   http=204   declined
    field=price_usd    http=204   declined

Both directions are graded here. A price spelling must find the catalog's price; a NON-price
field must still be looked up under its own name and must still disqualify a product the
catalog says nothing about, because that rule is R19 working rather than failing.
"""

from __future__ import annotations

import pytest
from contracts.ranking import PREFERENCE_FIELD_TERMS, canonical_field
from store_agent.runtime.bidding import (
    LIST_PRICE_KEY,
    PRICE_CONSTRAINT_FIELDS,
    catalog_key_for,
)


@pytest.mark.parametrize(
    "spelling",
    [
        "price_usd",
        "price",
        "unit_price",
        "total_price",
        "list_price",
        "cost",
        "Price USD",
        "price-usd",
    ],
)
def test_every_published_spelling_of_a_price_addresses_the_catalog_price(spelling: str) -> None:
    assert catalog_key_for(spelling) == LIST_PRICE_KEY, (
        f"a hard constraint spelled {spelling!r} was looked up under {catalog_key_for(spelling)!r}, "
        f"which no catalog carries, so every product is disqualified for 'no evidence'"
    )


@pytest.mark.parametrize(
    "field", ["material", "roast_level", "brew_method", "capacity_l", "in_stock"]
)
def test_a_field_that_is_not_a_price_is_looked_up_under_its_own_name(field: str) -> None:
    """The honest direction of the OTHER rule: R19 must keep disqualifying unevidenced fields."""
    assert catalog_key_for(field) == field


def test_the_price_spellings_are_the_ones_the_ranker_already_treats_as_one_field() -> None:
    """One vocabulary, not two.

    ``contracts.ranking.PREFERENCE_FIELD_TERMS`` already maps every price spelling to the single
    ``price_value`` term. If this runtime recognised a NARROWER set, a shopper could state a
    ceiling the ranker scores and the store cannot read -- which is exactly the split this
    module's constant exists to close. A wider set here would be this runtime inventing a
    vocabulary the contract does not publish.
    """
    published = {field for field, term in PREFERENCE_FIELD_TERMS.items() if term == "price_value"}
    # `discount` and `discount-pct` score `price_value` without BEING the price, so they are the
    # one documented difference: a constraint on a discount is not a constraint on the price.
    assert PRICE_CONSTRAINT_FIELDS == published - {"discount", "discount-pct"}, (
        f"this runtime's price spellings {sorted(PRICE_CONSTRAINT_FIELDS)} have drifted from the "
        f"contract's {sorted(published)}"
    )
    assert all(canonical_field(field) == field for field in PRICE_CONSTRAINT_FIELDS), (
        "a spelling here is not canonical, so canonical_field() can never match it"
    )


# =====================================================================================
# The behavioural half: the same question asked of `bid()` rather than of the helper
#
# The parametrized tests above grade `catalog_key_for`, which is a function no request calls
# directly. A helper that answers correctly while the bid path still declines is this
# repository's largest defect class, so the ceiling is put to the real entry point below.
# =====================================================================================

import json  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from store_agent.runtime import bid, is_decline  # noqa: E402

#: The same merchant-approved envelope `test_runtime.py` bids inside. READ from the fixture
#: rather than restated, for the reason that file gives: the walls a store bids inside are an
#: approved artifact, and a test that retyped them would grade the code against a copy nobody
#: approved. Built here rather than imported from the sibling test module, which pytest's
#: import mode does not put on the path.
REPO_ROOT = Path(__file__).resolve().parents[3]
ENVELOPE_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json"
CLUSTER = "cluster-warm-layers"
STORE_ID = "store-alpha"
LIST_PRICE = 100.0


def _fixture() -> dict[str, Any]:
    return json.loads(ENVELOPE_FIXTURE.read_text(encoding="utf-8"))


def _context() -> dict[str, Any]:
    fixture = _fixture()
    return {
        "store_id": STORE_ID,
        "envelope": fixture["envelope"],
        "catalog": fixture["catalog"],
        "live_state": {
            "prod-cap": {"in_stock": True, "units_left": 7},
            "prod-floor": {"in_stock": True, "units_left": 3},
        },
        "learned_policy": None,
        "network_priors": {CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }


def _request(constraint: dict[str, Any]) -> dict[str, Any]:
    return {
        "auction_id": "auc-price-spelling",
        "intent": {
            "intent_id": "int-price-spelling",
            "cluster_id": CLUSTER,
            "query": "a warm mid-layer for cold commutes",
            "category": "outerwear",
            "hard_constraints": [constraint],
            "preferences": [],
            "ship_to": "US-CA",
            "currency": "USD",
            "budget_band": "50-150",
            "created_at": "2026-01-01T00:00:00Z",
            "schema_version": "1",
        },
        "profile": {
            "pseudonym": "pseu-price-spelling",
            "buckets": {"budget_band": "50-150", "first_time": True},
        },
        "respond_by": "2999-01-01T00:00:00Z",
    }


@pytest.mark.parametrize("spelling", ["price_usd", "price", "unit_price", "list_price"])
def test_a_price_ceiling_a_product_clears_produces_a_bid_through_the_real_entry_point(
    spelling: str,
) -> None:
    """`bid()`, not `catalog_key_for`. The ceiling is above the list price, so it must bid."""
    ceiling = LIST_PRICE + 50.0
    answer: Any = bid(
        _request({"field": spelling, "op": "lte", "value": ceiling}),
        _context(),
    )
    assert not is_decline(answer), (
        f"a {ceiling} ceiling spelled {spelling!r} declined a product listed at {LIST_PRICE}: "
        f"{answer}"
    )
    assert answer.offer.unit_price <= ceiling


@pytest.mark.parametrize("spelling", ["price_usd", "price", "unit_price", "list_price"])
def test_a_price_ceiling_the_product_really_misses_still_declines(spelling: str) -> None:
    """The other direction, which is what makes the test above mean something.

    A ceiling BELOW the list price must still disqualify the product. Without this, resolving
    the spelling could have been implemented by ignoring price constraints entirely and both
    halves of this file would still be green.
    """
    answer: Any = bid(
        _request({"field": spelling, "op": "lte", "value": LIST_PRICE - 50.0}),
        _context(),
    )
    assert is_decline(answer), (
        f"a ceiling of {LIST_PRICE - 50.0} spelled {spelling!r} was satisfied by a product "
        f"listed at {LIST_PRICE}, so the constraint is not being applied at all: {answer}"
    )
