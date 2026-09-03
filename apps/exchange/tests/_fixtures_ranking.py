"""Shared builders for the T-032 ranking tests (auto-loaded by the exchange conftest).

Two audiences, one set of builders:

* :func:`rank_candidate`, :func:`rank_intent`, :func:`rank_trust_snapshot` and
  :func:`rank_config` are ordinary fixtures, so any test in ``apps/exchange/tests`` can ask
  for a ranking input without importing anything.
* the plain functions they wrap are importable directly, which is what
  ``test_ranking.py`` does — a builder that exists twice is a builder that can disagree with
  itself about what a candidate looks like.

The shapes here are the ones the frozen acceptance suite drives ``rank()`` with: hard-
constraint evidence carried by ``claims[i]["status"]``, the seller's registered domain at
``store_domain``, a float epoch ``expires_at``, and ``config={"now": ...}``. Nothing reads
the wall clock.
"""

from __future__ import annotations

from typing import Any

import pytest

T_NOW = 1_700_000_000.0
T_PAST = 1_600_000_000.0
T_FUTURE = 2_000_000_000.0

TRUST_DIMENSIONS = (
    "price_honored",
    "discount_honored",
    "shipped_on_time",
    "not_returned",
    "feedback_match",
)


def make_claim(
    key: str, value: Any, *, source: str = "owner_statement", status: str = "verified"
) -> dict[str, Any]:
    """One supporting fact. `status` is the only evidence R19 reads."""
    return {
        "key": key,
        "value": value,
        "provenance": {
            "source": source,
            "ref": f"ref:{key}",
            "observed_at": T_PAST,
            "authority_rank": 1,
        },
        "status": status,
    }


def make_candidate(
    bid_id: str,
    store_id: str | None = None,
    *,
    unit_price: float = 100.0,
    total_price: float | None = None,
    claims: list[dict[str, Any]] | None = None,
    expires_at: float = T_FUTURE,
    checkout_url: str | None = None,
    tier: int = 1,
    network_fee: float = 0.0,
    fee_rate: float = 0.0,
    **features: Any,
) -> dict[str, Any]:
    """One bid candidate as the exchange hands it to the ranker.

    Any published feature may be overridden by keyword; a feature left out is left OUT of
    the record rather than defaulted here, so a test can drive the "absent reads neutral"
    rule by simply not naming it.
    """
    store_id = store_id if store_id is not None else bid_id.replace("bid", "store")
    store_domain = f"{store_id}.example.com"
    if checkout_url is None:
        checkout_url = f"https://{store_domain}/cart/1:1?discount=NET"
    candidate: dict[str, Any] = {
        "bid_id": bid_id,
        "store_id": store_id,
        "store_domain": store_domain,
        "tier": tier,
        "network_fee": network_fee,
        "fee_rate": fee_rate,
        "envelope_max_discount_pct": 0.0,
        "envelope_budget_cap": 0.0,
        "offer": {
            "product_ref": "product-1",
            "unit_price": unit_price,
            "total_price": unit_price if total_price is None else total_price,
            "checkout_url": checkout_url,
            "expires_at": expires_at,
        },
        "claims": [make_claim("capacity_l", 35), make_claim("ships_in_days", 2)]
        if claims is None
        else list(claims),
    }
    candidate.update(features)
    return candidate


def make_intent(hard_constraints: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if hard_constraints is None:
        hard_constraints = [{"field": "capacity_l", "op": "gte", "value": 30}]
    return {
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "query": "a 30 litre commuter backpack",
        "category": "backpacks",
        "hard_constraints": list(hard_constraints),
        "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
        "ship_to": "US",
        "currency": "USD",
        "budget_band": "100-200",
        "created_at": T_PAST,
        "schema_version": "1.0.0",
    }


def make_trust_snapshot(
    store_ids,
    *,
    scores: dict[str, float] | None = None,
    blacklisted: tuple[str, ...] = (),
    low_data: tuple[str, ...] = (),
) -> dict[str, Any]:
    scores = scores or {}
    return {
        sid: {
            "store_id": sid,
            "score": float(scores.get(sid, 0.5)),
            "confidence": 0.4,
            "dims": {
                dim: {"alpha": 2.0, "beta": 2.0, "decayed_at": T_PAST} for dim in TRUST_DIMENSIONS
            },
            "blacklisted": sid in blacklisted,
            "low_data": sid in low_data,
        }
        for sid in store_ids
    }


def make_config(**overrides: Any) -> dict[str, Any]:
    """Ranker config. `now` is supplied so no test depends on the wall clock."""
    return {"now": T_NOW, **overrides}


@pytest.fixture
def rank_claim():
    return make_claim


@pytest.fixture
def rank_candidate():
    return make_candidate


@pytest.fixture
def rank_intent():
    return make_intent


@pytest.fixture
def rank_trust_snapshot():
    return make_trust_snapshot


@pytest.fixture
def rank_config():
    return make_config
