"""Wire-shaped payload builders for every pinned protocol object.

Loaded into `packages/contracts/tests/conftest.py` by `proxyshop_support.fixture_loader`, which
is the sanctioned way to add fixtures without editing the orchestrator-owned conftest.

Every builder returns the object as DESIGN §Interfaces spells it, with the same field names and
value shapes the rest of the system uses, so a test that builds a `Bid` here is exercising the
same thing a store-agent would send.
"""

from __future__ import annotations

from typing import Any

import pytest

HOOK_PROVENANCE: dict[str, Any] = {
    "source": "owner_statement",
    "ref": "envelope:store-1:v3#commitment-2",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 1,
}

ASSERTED_PROVENANCE: dict[str, Any] = {
    "source": "seller_asserted",
    "ref": "pitch:p-1#span-4",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 5,
}

SCRAPED_PROVENANCE: dict[str, Any] = {
    "source": "scraped",
    "ref": "snapshot://store-one.example.com/policies/returns@sha256:0f1e2d3c",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 4,
}

NOT_EXPIRED = "2999-01-01T00:00:00Z"
LONG_EXPIRED = "2000-01-01T00:00:00Z"
NOW = "2026-01-01T00:00:00Z"


def make_claim(key: str = "free_returns", value: Any = "30 days", provenance: Any = ...) -> dict:
    claim: dict[str, Any] = {"key": key, "value": value}
    if provenance is not ...:
        if provenance is not None:
            claim["provenance"] = provenance
    else:
        claim["provenance"] = dict(HOOK_PROVENANCE)
    return claim


def make_offer(expires_at: str | None = NOT_EXPIRED, **overrides: Any) -> dict:
    offer: dict[str, Any] = {
        "product_ref": "prod-1",
        "variant_ref": "44352913",
        "unit_price": 49.0,
        "currency": "USD",
        "discount": {"type": "percentage", "value": 10.0, "provenance": dict(HOOK_PROVENANCE)},
        "commitments": [],
        "total_price": 44.1,
        "expires_at": expires_at,
        "checkout_url": "https://store-one.example.com/cart/44352913:1",
    }
    offer.update(overrides)
    return offer


def make_bid(claims: Any = None, store_id: str = "store-1", **overrides: Any) -> dict:
    bid: dict[str, Any] = {
        "auction_id": "auc-1",
        "store_id": store_id,
        "offer": make_offer(),
        "claims": [make_claim()] if claims is None else list(claims),
        "message": None,
        "agent_version": "store-agent/1.0.0",
        "signature": "sig-deadbeef",
        "schema_version": "1.0.0",
    }
    bid.update(overrides)
    return bid


def make_submission(**overrides: Any) -> dict:
    """A complete external submission: a Bid with the required SigningEnvelope flattened on."""
    payload = make_bid(claims=[make_claim("material", "merino wool", dict(ASSERTED_PROVENANCE))])
    payload.pop("signature", None)
    payload.update(
        {
            "auction_id": "auc-0100",
            "store_id": "store-external-1",
            "signer_id": "store-external-1",
            "key_id": "key-2026-01",
            "issued_at": "2026-01-01T00:00:00Z",
            "nonce": "nonce-ext-0001",
            "schema_version": "1.0.0",
        }
    )
    payload.update(overrides)
    return payload


def make_intent(**overrides: Any) -> dict:
    intent: dict[str, Any] = {
        "intent_id": "int-1",
        "cluster_id": "cluster-serum",
        "query": "gentle vitamin C serum for sensitive skin",
        "category": "skincare",
        "hard_constraints": [{"field": "fragrance_free", "op": "eq", "value": True}],
        "preferences": [{"field": "price", "direction": "minimize", "weight": 0.6}],
        "ship_to": "US-CA",
        "currency": "USD",
        "budget_band": "40-80",
        "created_at": NOW,
        "schema_version": "1.0.0",
    }
    intent.update(overrides)
    return intent


def make_profile() -> dict:
    return {
        "pseudonym": "psn-0001",
        "buckets": {
            "budget_band": "40-80",
            "category_affinity": ["skincare"],
            "frequency_tier": "occasional",
            "region": "US-CA",
            "first_time": False,
        },
    }


def make_trust_dims(**overrides: float) -> dict:
    dims = {
        dim: {"alpha": 2.0, "beta": 1.0, "decayed_at": NOW}
        for dim in (
            "price_honored",
            "discount_honored",
            "shipped_on_time",
            "not_returned",
            "feedback_match",
            "catalog_claim_accuracy",
        )
    }
    for name, alpha in overrides.items():
        dims[name] = {"alpha": alpha, "beta": 1.0, "decayed_at": NOW}
    return dims


def make_trust_snapshot(store_id: str = "store-1", **overrides: Any) -> dict:
    snapshot: dict[str, Any] = {
        "store_id": store_id,
        "score": 0.62,
        "dims": make_trust_dims(),
        "blacklisted": False,
    }
    snapshot.update(overrides)
    return snapshot


def make_snapshot_table() -> dict:
    return {
        "store-1": {"store_id": "store-1", "score": 0.6, "blacklisted": False},
        "store-bad": {"store_id": "store-bad", "score": 0.05, "blacklisted": True},
    }


def make_ledger_event(**overrides: Any) -> dict:
    event: dict[str, Any] = {
        "event_id": "ev-1",
        "ts": NOW,
        "kind": "feedback",
        "auction_id": "auc-1",
        "store_id": "store-1",
        "order_ref": "ord-1",
        "payload": {"matched_pitch": False, "reason": "ships_within missed"},
    }
    event.update(overrides)
    return event


#: A realistic wire payload per pinned protocol object, keyed by class name.
def protocol_payloads() -> dict[str, dict]:
    commitment = make_claim()
    return {
        "Intent": make_intent(),
        "BuyerProfile": make_profile(),
        "BidRequest": {
            "auction_id": "auc-1",
            "intent": make_intent(),
            "profile": make_profile(),
            "respond_by": NOT_EXPIRED,
        },
        "Claim": commitment,
        "Provenance": dict(HOOK_PROVENANCE),
        "Offer": make_offer(),
        "Bid": make_bid(),
        "VerificationResult": {
            "pitch_ref": "pitch:p-1",
            "catalog_snapshot": "snapshot://store-1@sha256:0f1e2d3c",
            "claims": [
                {
                    "claim_ref": "claim-1",
                    "status": "verified",
                    "observed_value": "30 days",
                    "evidence_refs": ["snapshot://store-1/policies/returns"],
                    "confidence": 0.92,
                },
                {
                    "claim_ref": "claim-2",
                    "status": "unsupported",
                    "evidence_refs": [],
                    "confidence": 0.10,
                },
            ],
            "verified_claim_ratio": 0.5,
            "verifier_version": "verification/1.0.0",
        },
        "Shortlist": {
            "auction_id": "auc-1",
            "slots": [
                {
                    "slot": "fit",
                    "bid_ref": "bid-1",
                    "fit_score": 0.82,
                    "trust_summary": {"score": 0.62, "confidence": 0.4},
                    "provenance_labels": ["store-confirmed"],
                }
            ],
        },
        "LossReport": {
            "store_id": "store-1",
            "window": {"start": 1767225600.0, "end": 1767830400.0},
            "by_cluster": [
                {
                    "cluster_id": "cluster-serum",
                    "lost": 3,
                    "reasons": {"fit": 2, "price": 1, "commitments": 0, "trust": 0},
                    "unmet_criteria": ["fragrance_free == True"],
                }
            ],
        },
        "LedgerEvent": make_ledger_event(),
        "TrustSnapshot": make_trust_snapshot(),
        "TrustEventPayload": {
            "store_id": "store-1",
            "event": make_ledger_event(),
            "dim": "shipped_on_time",
            "delta": -0.35,
            "pseudonymous_context": {"cluster_id": "cluster-serum", "pseudonym": "psn-0001"},
        },
        "Envelope": {
            "store_id": "store-1",
            "version": 3,
            "floors": [{"product_ref": "prod-1", "min_price": 30.0}],
            "max_discount_pct": 20.0,
            "budget_cap": 500.0,
            "pursue_clusters": ["cluster-serum"],
            "standing_commitments": [commitment],
            "activation": "shadow",
        },
    }


@pytest.fixture
def protocol_payload_map() -> dict[str, dict]:
    """`{pinned object name: wire payload}` for all fourteen."""
    return protocol_payloads()


@pytest.fixture
def bid_factory():
    """Build a `Bid`-shaped dict; `bid_factory(claims=[...], store_id=...)`."""
    return make_bid


@pytest.fixture
def submission_factory():
    """Build a complete external `SignedBidSubmission`-shaped dict."""
    return make_submission


@pytest.fixture
def snapshot_table() -> dict:
    """The `{store_id: row}` mapping the dual-path boundary reads."""
    return make_snapshot_table()
