"""`claim_id` — the content hash that makes re-extraction idempotent (D25).

The property being bought is narrow and specific: extract the same pitch twice and the same claim
must get the same id, so a verification result written against it still points at something. Every
test below is about that one property, from both sides — what must NOT change the id, and what
must.
"""

from __future__ import annotations

from contracts.claims import canonicalize_value
from packages.contracts import claim_id, claim_id_for


def base(**overrides):
    material = {
        "pitch_ref": "pitch:p-1",
        "key": "free_returns",
        "value": "30 days",
        "claim_type": "return_policy",
    }
    material.update(overrides)
    return claim_id(**material)


def test_the_same_content_hashes_the_same_every_time() -> None:
    assert base() == base()


def test_the_id_is_self_describing() -> None:
    identifier = base()
    assert identifier.startswith("claim:sha256:")
    assert len(identifier) > 40


def test_re_extraction_that_produces_an_equal_value_produces_an_equal_id() -> None:
    """Whitespace and case are extraction noise, not different claims."""
    assert base(value="  30 Days ") == base(value="30 days")


def test_an_integral_float_and_an_int_are_the_same_value() -> None:
    assert base(value=30.0) == base(value=30)


def test_key_order_inside_a_structured_value_does_not_change_the_id() -> None:
    assert base(value={"days": 30, "restocking_fee": 0}) == base(
        value={"restocking_fee": 0, "days": 30}
    )


def test_list_order_inside_a_value_does_change_the_id() -> None:
    """A list of ingredients is ordered; treating it as a set would merge two different claims."""
    assert base(value=["water", "glycerin"]) != base(value=["glycerin", "water"])


def test_each_component_of_the_hash_actually_participates() -> None:
    reference = base()
    assert base(pitch_ref="pitch:p-2") != reference, "the pitch scopes the claim"
    assert base(key="free_shipping") != reference
    assert base(value="14 days") != reference
    assert base(claim_type="warranty") != reference, (
        "a 30-day return policy and a 30-day warranty are different claims"
    )


def test_provenance_is_not_part_of_the_hash() -> None:
    """The same claim re-observed from a fresher snapshot is the same claim; folding provenance in
    would mint a new id on every crawl and defeat the whole point."""
    fresh = {
        "key": "free_returns",
        "value": "30 days",
        "claim_type": "return_policy",
        "source_span": {"pitch_ref": "pitch:p-1", "start": 0, "end": 8},
        "provenance": {
            "source": "scraped",
            "ref": "snapshot://store/policies@sha256:aaaa",
            "observed_at": "2026-01-01T00:00:00Z",
            "authority_rank": 4,
        },
    }
    stale = {
        **fresh,
        "provenance": {
            "source": "owner_statement",
            "ref": "envelope:store-1:v3#c-2",
            "observed_at": "2020-01-01T00:00:00Z",
            "authority_rank": 1,
        },
    }
    assert claim_id_for(fresh) == claim_id_for(stale)


def test_claim_id_for_reads_the_pitch_off_the_source_span() -> None:
    claim = {
        "key": "free_returns",
        "value": "30 days",
        "claim_type": "return_policy",
        "source_span": {"pitch_ref": "pitch:p-1", "start": 0, "end": 8},
    }
    assert claim_id_for(claim) == base()
    assert claim_id_for(claim, pitch_ref="pitch:p-2") == base(pitch_ref="pitch:p-2")


def test_claim_id_for_reads_a_model_as_well_as_a_mapping() -> None:
    from packages.contracts import Claim
    from packages.contracts.tests._fixtures_protocol import make_claim

    payload = {**make_claim(), "claim_type": "return_policy"}
    assert claim_id_for(Claim.model_validate(payload)) == claim_id_for(payload)


def test_a_claim_with_no_pitch_still_gets_a_stable_id() -> None:
    """A hook-minted commitment has no pitch, and must still be addressable."""
    assert claim_id(pitch_ref=None, key="free_returns", value="30 days") == claim_id(
        pitch_ref="", key="free_returns", value="30 days"
    )


def test_canonicalization_leaves_booleans_alone() -> None:
    """`True` is an `int` subclass; collapsing it to 1 would merge "is vegan" with "count is 1"."""
    assert canonicalize_value(True) is True
    assert base(value=True) != base(value=1)
