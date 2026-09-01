"""Provenance labelling (R2/D30) and `authority_rank` semantics.

The label map is published once, here, because two tickets read it: the exchange produces
`Shortlist.slots[].provenance_labels` and the buyer app renders them. A second copy would be a
second answer to "did the store say this, or did we read it off their site?".

The table asserted below is the FROZEN acceptance suite's, not D30's prose — they disagree about
`pixel_feed`, `learned_policy` and `network`. See `contracts/labels.py` and this ticket's report.
"""

from __future__ import annotations

import pytest

from packages.contracts import (
    BUYER_PROVENANCE_LABELS,
    LABEL_FROM_THEIR_WEBSITE,
    LABEL_STORE_CONFIRMED,
    PROVENANCE_AUTHORITY_RANK,
    PROVENANCE_BUYER_LABELS,
    Provenance,
    ProvenanceSource,
    buyer_label,
    canonical_authority_rank,
)

FROZEN_LABELS = {
    "owner_statement": "store-confirmed",
    "envelope_rule": "store-confirmed",
    "learned_policy": "store-confirmed",
    "pixel_feed": "store-confirmed",
    "network": "store-confirmed",
    "scraped": "from their website",
}


@pytest.mark.parametrize(("source", "expected"), sorted(FROZEN_LABELS.items()))
def test_each_source_maps_to_its_published_label(source: str, expected: str) -> None:
    assert buyer_label(source) == expected


def test_spec_r2_fixes_exactly_two_buyer_facing_label_strings() -> None:
    assert BUYER_PROVENANCE_LABELS == {LABEL_STORE_CONFIRMED, LABEL_FROM_THEIR_WEBSITE}


def test_a_seller_asserted_claim_gets_neither_provenance_label() -> None:
    """R2/R8: it has no provenance a buyer should read as evidence, so it surfaces the R18 badge."""
    label = buyer_label("seller_asserted")
    assert label not in BUYER_PROVENANCE_LABELS
    assert "unverified" in label.lower()


def test_the_map_is_total_over_the_closed_provenance_enum() -> None:
    """A renderer must never have to invent a label for a source the schema allows."""
    assert set(PROVENANCE_BUYER_LABELS) == {member.value for member in ProvenanceSource}


def test_an_unknown_source_raises_rather_than_defaulting() -> None:
    """A default of "store-confirmed" for an unrecognised source is exactly the failure R2 exists
    to prevent: it would tell a buyer the store vouched for something it never saw."""
    with pytest.raises(KeyError):
        buyer_label("invented-source")


def test_the_label_reads_a_provenance_model_a_mapping_or_a_bare_string() -> None:
    payload = {
        "source": "scraped",
        "ref": "snapshot://store.example.com/policies/returns@sha256:0f1e",
        "observed_at": "2026-01-01T00:00:00Z",
        "authority_rank": 4,
    }
    assert buyer_label(Provenance.model_validate(payload)) == LABEL_FROM_THEIR_WEBSITE
    assert buyer_label(payload) == LABEL_FROM_THEIR_WEBSITE
    assert buyer_label("scraped") == LABEL_FROM_THEIR_WEBSITE


# --- D30: authority_rank has defined semantics, or it should not exist ---------------------


def test_authority_rank_is_published_for_every_source() -> None:
    assert set(PROVENANCE_AUTHORITY_RANK) == {member.value for member in ProvenanceSource}


def test_one_is_the_most_authoritative_and_larger_is_weaker() -> None:
    assert canonical_authority_rank("owner_statement") == 1
    assert canonical_authority_rank("envelope_rule") == 1
    assert canonical_authority_rank("seller_asserted") == 5
    assert canonical_authority_rank("scraped") > canonical_authority_rank("pixel_feed")
    assert canonical_authority_rank("seller_asserted") > canonical_authority_rank("scraped")


def test_every_hook_source_outranks_the_only_non_hook_source() -> None:
    """The ordering has to agree with the boundary rule, or two parts of the system disagree about
    which evidence is worth more."""
    from packages.contracts import HOOK_PROVENANCE_SOURCES

    asserted = canonical_authority_rank("seller_asserted")
    for source in HOOK_PROVENANCE_SOURCES:
        assert canonical_authority_rank(source) < asserted


def test_the_schema_requires_a_rank_of_at_least_one() -> None:
    payload = {
        "source": "owner_statement",
        "ref": "envelope:store-1:v3#c-1",
        "observed_at": "2026-01-01T00:00:00Z",
    }
    with pytest.raises(Exception):
        Provenance.model_validate(payload)
    with pytest.raises(Exception):
        Provenance.model_validate({**payload, "authority_rank": 0})
    assert Provenance.model_validate({**payload, "authority_rank": 1}).authority_rank == 1


def test_a_hook_may_down_rank_a_stale_observation() -> None:
    """The canonical table is guidance, not a schema constraint: `authority_rank` is validated as
    `>= 1`, so a hook can legitimately weaken a rank it knows to be stale."""
    payload = {
        "source": "owner_statement",
        "ref": "envelope:store-1:v1#c-1",
        "observed_at": "2020-01-01T00:00:00Z",
        "authority_rank": 4,
    }
    assert Provenance.model_validate(payload).authority_rank == 4
    assert canonical_authority_rank("owner_statement") == 1
