"""Shared builders for the T-032 ranking tests (auto-loaded by the exchange conftest).

One set of builders, imported directly. :func:`make_claim`, :func:`make_candidate`,
:func:`make_intent`, :func:`make_trust_snapshot` and :func:`make_config` are plain functions
and ``test_ranking.py`` imports them by name — a builder that exists twice is a builder that
can disagree with itself about what a candidate looks like.

There used to be a second audience: five one-line ``@pytest.fixture`` wrappers
(``rank_claim``, ``rank_candidate``, ``rank_intent``, ``rank_trust_snapshot``, ``rank_config``)
that returned the builder above them, so a test could request one by parameter name without an
import. They were removed because no test in ``apps/exchange/tests`` ever requested one —
measured over every function signature, ``usefixtures`` marker and ``getfixturevalue`` call in
the directory. Add a wrapper back only alongside the test that asks for it; the loader in
``proxyshop_support.fixture_loader`` picks up anything decorated here with no other wiring.

The shapes here are the ones the frozen acceptance suite drives ``rank()`` with: the seller's
registered domain at ``store_domain``, a float epoch ``expires_at``, and
``config={"now": ...}``. Nothing reads the wall clock.

Hard-constraint evidence is the one shape that CHANGED (ESC-020). It used to be a plain
``claims[i]["status"]`` string, which is a field the published ``Claim`` does not declare and
which nothing but the bidder ever wrote — so a store satisfied any hard constraint by
asserting that it had. :func:`make_claim` therefore mints the verdict through
:func:`exchange.ranking.attestation.attest_claim`, which attests it with this process's key.
The fixture is standing in for the exchange here, which is exactly what it is entitled to do:
it is inside the trust boundary, and a bid arriving over HTTP is not. A test that wants to
drive the FORGERY writes ``status`` on the claim by hand and asserts it buys nothing — see
``test_ranking_claim_forgery.py``.
"""

from __future__ import annotations

from typing import Any

from exchange.ranking.attestation import attest_claim

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
    """One supporting fact, carrying the exchange's attested verdict on it.

    `status` is the verdict the fixture is asking the exchange to have reached — it is passed
    to the attester, never written onto the claim, because a `status` on the claim is the one
    thing R19 may not read (ESC-020).
    """
    return attest_claim(
        {
            "key": key,
            "value": value,
            "provenance": {
                "source": source,
                "ref": f"ref:{key}",
                "observed_at": T_PAST,
                "authority_rank": 1,
            },
        },
        status=status,
    )


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
