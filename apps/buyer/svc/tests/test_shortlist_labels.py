"""R2: what a shortlist slot's provenance says to the buyer (T-072).

Two claims are worth testing separately, and only the first is in the frozen suite:

* the **table** — six sources to "store-confirmed"/"from their website", and
  ``seller_asserted`` to neither of them;
* the **source of the labels** — D30 says the exchange produces them and the buyer renders
  what it was given. A renderer that quietly re-derives them from the claims is a second
  answer to the same question, and the whole point of putting the map in
  ``packages/contracts`` was that there be one.
"""

from __future__ import annotations

import pytest
from contracts.labels import PROVENANCE_BUYER_LABELS

from apps.buyer.svc.src.accept import (
    LABEL_FROM_THEIR_WEBSITE,
    LABEL_STORE_CONFIRMED,
    LABEL_UNVERIFIED,
    LABELS_ABSENT,
    LABELS_DERIVED,
    LABELS_SUPPLIED,
    AcceptError,
    UnknownProvenanceSource,
    labels_for_claims,
    provenance_label,
    render_shortlist,
    slot_labels,
)


def prov(source: str, **extra):
    return {"source": source, "ref": "src-1", "observed_at": "2026-01-01T00:00:00Z", **extra}


# --- the table ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("owner_statement", LABEL_STORE_CONFIRMED),
        ("envelope_rule", LABEL_STORE_CONFIRMED),
        ("learned_policy", LABEL_STORE_CONFIRMED),
        ("pixel_feed", LABEL_STORE_CONFIRMED),
        ("network", LABEL_STORE_CONFIRMED),
        ("scraped", LABEL_FROM_THEIR_WEBSITE),
        ("seller_asserted", LABEL_UNVERIFIED),
    ],
)
def test_every_pinned_source_has_its_published_label(source, expected) -> None:
    assert provenance_label(prov(source)) == expected


def test_the_table_is_contracts_and_not_a_second_copy_here() -> None:
    """If this package ever restates the map, this is what catches the drift."""
    for source, expected in PROVENANCE_BUYER_LABELS.items():
        assert provenance_label(source) == expected


def test_a_seller_asserted_claim_is_labelled_neither_of_r2s_two_strings() -> None:
    label = provenance_label(prov("seller_asserted"))
    assert label not in {LABEL_STORE_CONFIRMED, LABEL_FROM_THEIR_WEBSITE}
    assert "unverified" in label.lower()


def test_a_provenance_object_reads_the_same_as_its_dict() -> None:
    plain = type("_Prov", (), {"source": "scraped", "ref": "r", "authority_rank": 1})()
    assert provenance_label(plain) == provenance_label(prov("scraped"))


def test_an_unpinned_source_refuses_rather_than_defaulting_to_store_confirmed() -> None:
    """A `.get(..., "store-confirmed")` here would be exactly the R2 failure."""
    with pytest.raises(UnknownProvenanceSource) as caught:
        provenance_label(prov("vibes"))
    assert isinstance(caught.value, AcceptError)
    assert not isinstance(caught.value, KeyError)


# --- where the labels came from --------------------------------------------------------


def test_the_exchanges_labels_are_rendered_verbatim_even_when_claims_disagree() -> None:
    """D30: the exchange produces, the buyer renders. It does not audit and it does not vote."""
    slot = {
        "slot": "fit",
        "bid_ref": "bid-1",
        "provenance_labels": ["store-confirmed"],
        "claims": [{"provenance": prov("scraped")}],
    }
    assert slot_labels(slot) == (("store-confirmed",), LABELS_SUPPLIED)


def test_labels_are_derived_only_when_the_exchange_sent_none() -> None:
    slot = {"slot": "value", "bid_ref": "bid-2", "claims": [{"provenance": prov("scraped")}]}
    assert slot_labels(slot) == ((LABEL_FROM_THEIR_WEBSITE,), LABELS_DERIVED)


def test_derivation_can_be_switched_off_for_the_strict_reading_of_d30() -> None:
    slot = {"slot": "value", "bid_ref": "bid-2", "claims": [{"provenance": prov("scraped")}]}
    assert slot_labels(slot, derive=False) == ((LABEL_UNVERIFIED,), LABELS_ABSENT)


def test_a_slot_with_no_provenance_at_all_reads_as_unverified_not_as_nothing() -> None:
    """An empty label list would read to a buyer as a slot with nothing to hide."""
    assert slot_labels({"slot": "fit", "bid_ref": "bid-3"}) == ((LABEL_UNVERIFIED,), LABELS_ABSENT)


def test_derived_labels_are_deduplicated_and_ordered() -> None:
    claims = [
        {"provenance": prov("scraped")},
        {"provenance": prov("owner_statement")},
        {"provenance": prov("scraped")},
    ]
    assert labels_for_claims(claims) == (LABEL_FROM_THEIR_WEBSITE, LABEL_STORE_CONFIRMED)


def test_a_claim_with_an_unpinned_source_is_skipped_not_guessed_at() -> None:
    claims = [{"provenance": prov("vibes")}, {"provenance": prov("network")}]
    assert labels_for_claims(claims) == (LABEL_STORE_CONFIRMED,)


@pytest.mark.parametrize("junk", [None, "scraped", 7, object()])
def test_unreadable_claims_derive_nothing_rather_than_raising(junk) -> None:
    assert labels_for_claims(junk) == ()


# --- the whole shortlist ---------------------------------------------------------------


def test_the_shortlists_auction_id_reaches_every_slot() -> None:
    """A slot accepted against the wrong auction is what re-attaching it by hand produces."""
    shortlist = {
        "auction_id": "auc-9",
        "slots": [
            {"slot": "fit", "bid_ref": "bid-1", "fit_score": 0.8, "provenance_labels": ["x"]},
            {"slot": "value", "bid_ref": "bid-2", "fit_score": 0.7, "provenance_labels": ["y"]},
        ],
    }
    rendered = render_shortlist(shortlist)
    assert [s.auction_id for s in rendered] == ["auc-9", "auc-9"]
    assert [s.bid_ref for s in rendered] == ["bid-1", "bid-2"]


def test_a_shortlist_collapses_rather_than_pads() -> None:
    """One eligible store yields one slot; the renderer must not assume four."""
    assert len(render_shortlist({"auction_id": "auc-9", "slots": []})) == 0
    one = render_shortlist(
        {"auction_id": "auc-9", "slots": [{"slot": "fit", "bid_ref": "b", "fit_score": 0.5}]}
    )
    assert len(one) == 1


def test_a_rendered_slot_never_carries_a_checkout_url() -> None:
    """R3: there is nothing on the render object for a template to link to by accident."""
    rendered = render_shortlist(
        {
            "auction_id": "auc-9",
            "slots": [
                {
                    "slot": "fit",
                    "bid_ref": "bid-1",
                    "fit_score": 0.8,
                    "provenance_labels": ["store-confirmed"],
                    "checkout_url": "https://attacker.example/cart/1:1",
                }
            ],
        }
    )
    assert "attacker.example" not in repr([slot.to_dict() for slot in rendered])
    assert "checkout_url" not in rendered[0].to_dict()


def test_a_bare_sequence_of_slots_renders_too() -> None:
    rendered = render_shortlist([{"slot": "fit", "bid_ref": "b", "fit_score": 0.5}])
    assert [slot.slot for slot in rendered] == ["fit"]


def test_a_rendered_slot_is_subscriptable_and_serialisable() -> None:
    rendered = render_shortlist(
        {"auction_id": "auc-9", "slots": [{"slot": "fit", "bid_ref": "b", "fit_score": "0.5"}]}
    )[0]
    assert rendered["fit_score"] == 0.5  # coerced, not crashed on
    assert rendered.to_dict()["labels_source"] == LABELS_ABSENT
