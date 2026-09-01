"""The pinned protocol objects: they validate, they round-trip, and their vocabularies are closed.

Presence is not the contract. Every one of the fourteen has to accept the object as DESIGN spells
it, keep every field through a serialize/deserialize round trip, and REFUSE things that are not
that object — an empty payload, an out-of-set enum value, an extra key. A package of permissive
`dict`-shaped classes would expose all fourteen names and guarantee none of it.
"""

from __future__ import annotations

import json

import pytest

import packages.contracts as contracts
from packages.contracts import (
    Bid,
    Claim,
    Envelope,
    Intent,
    LedgerEvent,
    Offer,
    Provenance,
    TrustSnapshot,
)
from packages.contracts.tests._fixtures_protocol import (
    ASSERTED_PROVENANCE,
    HOOK_PROVENANCE,
    make_bid,
    make_claim,
    make_intent,
    make_offer,
    make_trust_dims,
    make_trust_snapshot,
    protocol_payloads,
)

PINNED = contracts.PINNED_PROTOCOL_OBJECTS


def test_all_fourteen_pinned_objects_are_exported_model_types() -> None:
    missing = [name for name in PINNED if not hasattr(contracts, name)]
    assert missing == [], f"packages.contracts is missing pinned objects: {missing}"
    assert len(PINNED) == 14
    for name in PINNED:
        cls = getattr(contracts, name)
        assert isinstance(cls, type), f"{name} is not a class"
        assert hasattr(cls, "model_validate"), f"{name} is not a validating model type"


@pytest.mark.parametrize("name", PINNED)
def test_pinned_object_validates_its_design_payload_and_round_trips(name: str) -> None:
    cls = getattr(contracts, name)
    payload = protocol_payloads()[name]

    model = cls.model_validate(payload)
    dumped = model.model_dump()

    # model_dump() must be JSON-native: no datetime, no enum wrapper, nothing that needs a
    # coercion pass before it can go on a wire. `default=` is deliberately NOT passed — if it
    # were, a field that had silently become a datetime would still serialize and hide the bug.
    text = json.dumps(dumped)
    assert cls.model_validate(json.loads(text)) == model, (
        f"{name} did not survive dump -> json -> load with equality intact"
    )


@pytest.mark.parametrize("name", PINNED)
def test_pinned_object_rejects_an_empty_payload(name: str) -> None:
    cls = getattr(contracts, name)
    with pytest.raises(Exception):
        cls.model_validate({})


@pytest.mark.parametrize("name", PINNED)
def test_pinned_object_rejects_an_unknown_field(name: str) -> None:
    """Closed property sets. An unexpected key is a schema error, not a field that rides along."""
    cls = getattr(contracts, name)
    payload = {**protocol_payloads()[name], "smuggled_in": "should not be accepted"}
    with pytest.raises(Exception):
        cls.model_validate(payload)


# --- closed enums -----------------------------------------------------------------------


def test_provenance_source_is_a_closed_enum() -> None:
    assert Provenance.model_validate(HOOK_PROVENANCE).source == "owner_statement"
    with pytest.raises(Exception):
        Provenance.model_validate({**HOOK_PROVENANCE, "source": "made-up-source"})
    # Every source the system actually uses is accepted, so the enum is closed and not merely small.
    for source in (
        "owner_statement",
        "envelope_rule",
        "learned_policy",
        "pixel_feed",
        "network",
        "scraped",
        "seller_asserted",
    ):
        assert Provenance.model_validate({**HOOK_PROVENANCE, "source": source}).source == source


def test_provenance_accepts_the_pinned_keyword_spelling() -> None:
    """Two other tickets construct `Provenance` positionally by keyword; the spelling is fixed."""
    provenance = Provenance(
        source="scraped",
        ref="snapshot://store.example.com/policies/shipping@sha256:0f1e2d3c",
        observed_at="2026-01-01T00:00:00Z",
        authority_rank=1,
    )
    assert provenance.source == "scraped"
    assert provenance.authority_rank == 1


def test_envelope_activation_is_exactly_shadow_active_killed() -> None:
    payload = protocol_payloads()["Envelope"]
    for activation in ("shadow", "active", "killed"):
        assert (
            Envelope.model_validate({**payload, "activation": activation}).activation == activation
        )
    with pytest.raises(Exception):
        Envelope.model_validate({**payload, "activation": "whenever"})


def test_hard_constraints_are_filters_and_carry_no_weight() -> None:
    """R19: a hard constraint is an eligibility filter, and a weighted one is a schema error."""
    intent = Intent.model_validate(make_intent())
    constraint = intent.hard_constraints[0]
    assert (constraint.field, constraint.op, constraint.value) == ("fragrance_free", "eq", True)
    assert not hasattr(constraint, "weight")

    with pytest.raises(Exception):
        Intent.model_validate(
            make_intent(
                hard_constraints=[
                    {"field": "fragrance_free", "op": "eq", "value": True, "weight": 0.9}
                ]
            )
        )


def test_constraint_ops_and_preference_directions_are_closed() -> None:
    for op in ("eq", "lte", "gte", "in", "contains"):
        Intent.model_validate(make_intent(hard_constraints=[{"field": "f", "op": op, "value": 1}]))
    with pytest.raises(Exception):
        Intent.model_validate(
            make_intent(hard_constraints=[{"field": "name", "op": "regex", "value": "^serum"}])
        )

    for direction in ("maximize", "minimize", "prefer"):
        Intent.model_validate(
            make_intent(preferences=[{"field": "price", "direction": direction, "weight": 0.5}])
        )
    with pytest.raises(Exception):
        Intent.model_validate(
            make_intent(preferences=[{"field": "price", "direction": "sideways", "weight": 0.5}])
        )


def test_preferences_carry_a_numeric_weight() -> None:
    preference = Intent.model_validate(make_intent()).preferences[0]
    assert preference.direction == "minimize"
    assert float(preference.weight) == pytest.approx(0.6)
    with pytest.raises(Exception):
        Intent.model_validate(
            make_intent(preferences=[{"field": "price", "direction": "minimize"}])
        )


# --- D53: six trust dimensions, no more and no fewer -------------------------------------


def test_trust_snapshot_carries_exactly_the_six_dimensions() -> None:
    snapshot = TrustSnapshot.model_validate(make_trust_snapshot())
    dumped = snapshot.model_dump()
    assert set(dumped["dims"]) == set(contracts.TRUST_DIMENSIONS)
    assert len(contracts.TRUST_DIMENSIONS) == 6
    assert "catalog_claim_accuracy" in contracts.TRUST_DIMENSIONS


def test_trust_snapshot_without_catalog_claim_accuracy_is_invalid() -> None:
    """D53: product-fact verification is its own dimension. A five-dim snapshot is not a snapshot."""
    dims = make_trust_dims()
    dims.pop("catalog_claim_accuracy")
    with pytest.raises(Exception):
        TrustSnapshot.model_validate(make_trust_snapshot(dims=dims))


def test_trust_snapshot_with_a_seventh_dimension_is_invalid() -> None:
    dims = make_trust_dims()
    dims["invented_dimension"] = {"alpha": 1.0, "beta": 1.0, "decayed_at": "2026-01-01T00:00:00Z"}
    with pytest.raises(Exception):
        TrustSnapshot.model_validate(make_trust_snapshot(dims=dims))


def test_trust_snapshot_dimension_state_requires_alpha_beta_and_decay() -> None:
    dims = make_trust_dims()
    dims["price_honored"] = {"alpha": 2.0, "beta": 1.0}
    with pytest.raises(Exception):
        TrustSnapshot.model_validate(make_trust_snapshot(dims=dims))


def test_trust_snapshot_keeps_its_optional_replay_metadata() -> None:
    """D48's metadata half survives D53: `computed_through_event` is what makes S3 checkable."""
    snapshot = TrustSnapshot.model_validate(
        make_trust_snapshot(
            confidence=0.4,
            effective_sample_size=12.5,
            score_version="1.0.0",
            snapshot_version="1.0.0",
            computed_through_event="ev-0003",
            low_data=False,
        )
    )
    assert snapshot.computed_through_event == "ev-0003"
    assert snapshot.effective_sample_size == pytest.approx(12.5)


# --- nesting survives serialization -------------------------------------------------------


def test_bid_nested_offer_discount_provenance_survives_the_round_trip() -> None:
    bid = Bid.model_validate(make_bid())
    dumped = bid.model_dump()
    assert dumped["offer"]["discount"]["provenance"]["source"] == "owner_statement", (
        "the nested Provenance collapsed instead of serializing"
    )
    assert dumped["offer"]["product_ref"] == "prod-1"
    text = json.dumps(dumped)
    assert Bid.model_validate(json.loads(text)) == bid


def test_ledger_event_payload_keeps_arbitrary_nesting() -> None:
    """The ledger records what happened. A closed payload union would make it lossy."""
    event = LedgerEvent.model_validate(
        {
            "event_id": "ev-1",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "bid_placed",
            "auction_id": "auc-1",
            "store_id": "store-1",
            "payload": {"bid_ref": "bid-1", "nested": {"components": [1, 2, 3]}},
        }
    )
    dumped = event.model_dump()
    assert dumped["payload"]["nested"]["components"] == [1, 2, 3]
    assert LedgerEvent.model_validate(json.loads(json.dumps(dumped))) == event


def test_ledger_event_auction_and_order_refs_are_optional() -> None:
    """Not every event belongs to an auction: a blacklist expiry belongs to a store alone."""
    event = LedgerEvent.model_validate(
        {
            "event_id": "ev-9",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "blacklist_expired",
            "store_id": "store-bad",
            "payload": {"store_id": "store-bad", "reason_code": "counterfeit"},
        }
    )
    assert event.auction_id is None
    assert event.order_ref is None


# --- fields the frozen hosted-bid shape omits must stay optional --------------------------


def test_offer_accepts_the_minimal_design_shape() -> None:
    """The protocol Offer predates `variant_ref`; requiring it would reject a legal hosted bid."""
    offer = Offer.model_validate(
        {
            "product_ref": "prod-1",
            "unit_price": 49.0,
            "discount": {"type": "percentage", "value": 10.0},
            "commitments": [],
            "total_price": 44.1,
            "expires_at": "2999-01-01T00:00:00Z",
            "checkout_url": "https://store-one.example.com/cart/1:1",
        }
    )
    assert offer.variant_ref is None
    assert offer.bid_offer_id is None


def test_offer_with_a_null_discount_is_valid() -> None:
    assert Offer.model_validate(make_offer(discount=None)).discount is None


def test_claim_identity_and_type_are_optional_on_the_wire() -> None:
    """A hook-minted commitment has no pitch to hash against, so `claim_id` cannot be required."""
    claim = Claim.model_validate(make_claim())
    assert claim.claim_id is None
    assert claim.claim_type is None


def test_claim_type_is_the_closed_d53_vocabulary() -> None:
    for claim_type in ("ingredients", "return_policy", "shipping_speed"):
        assert (
            Claim.model_validate({**make_claim(), "claim_type": claim_type}).claim_type
            == claim_type
        )
    with pytest.raises(Exception):
        Claim.model_validate({**make_claim(), "claim_type": "vibes"})


def test_bid_carries_no_signing_envelope_field() -> None:
    """D52: the envelope belongs to a SUBMISSION. A hosted agent has no key to sign with."""
    fields = set(Bid.model_fields)
    assert fields.isdisjoint({"signer_id", "key_id", "issued_at", "nonce"}), (
        "the signing envelope leaked onto Bid; a hosted Tier-1 bid would stop validating"
    )
    assert "schema_version" in fields


def test_a_bid_carrying_a_seller_asserted_claim_is_still_schema_valid() -> None:
    """R8 is a BOUNDARY rule, not a schema rule: the shape is legal, the hosted path refuses it."""
    Bid.model_validate(make_bid(claims=[make_claim("spf", 30, dict(ASSERTED_PROVENANCE))]))
