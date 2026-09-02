"""T-080 — the approved golden set is a usable answer key for the S8 eval gates.

`packages/verification` (T-065) is *graded against* this document and never authors it
(D11), so everything the verifier will be asked to reproduce has to be present and
self-consistent here first: four statuses, every DESIGN eval gate, a pitch that mixes true
and false claims, product facts in both directions, and a catalog snapshot per pitch that
actually contains the evidence its labels were derived from.
"""

from __future__ import annotations

import re

from fixtures.manifest import (
    CATALOG_DIM,
    VERIFICATION_STATUSES,
    load_golden_set,
    load_manifest,
)

MANIFEST = load_manifest()
GOLDEN = load_golden_set(MANIFEST)
PITCHES = GOLDEN["pitches"]

#: The eleven eval gates DESIGN's verification strategy enumerates.
EVAL_GATES = {
    "contradiction": {"contradiction", "contradictions", "contradicted"},
    "stale_evidence": {
        "stale_evidence",
        "stale_or_absent_evidence",
        "stale_absent_evidence",
        "absent_evidence",
    },
    "wrong_variant": {"wrong_variant", "variant_mismatch"},
    "wrong_units": {"wrong_units", "wrong_unit", "unit_mismatch"},
    "injection": {"injection", "prompt_injection"},
    "claim_splitting": {"claim_splitting", "claim_split"},
    "verbosity_gaming": {"verbosity_gaming", "verbosity"},
    "timeout": {"timeout", "timeouts"},
    "duplicate_pitch": {"duplicate_pitch", "duplicate_pitches", "duplicate"},
    "expired_offer": {"expired_offer", "expired_offers"},
    "blacklist_expiry": {"blacklist_expiry", "blacklist_expiration"},
}


def _token(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value).lower())).strip("_")


def _claims() -> list:
    return [claim for pitch in PITCHES for claim in pitch["claims"]]


def test_pitch_ids_and_claim_refs_are_unique() -> None:
    ids = [p["pitch_id"] for p in PITCHES]
    assert len(set(ids)) == len(ids)
    for pitch in PITCHES:
        refs = [c["claim_ref"] for c in pitch["claims"]]
        assert len(set(refs)) == len(refs), pitch["pitch_id"]
    all_refs = [c["claim_ref"] for c in _claims()]
    assert len(set(all_refs)) == len(all_refs), "claim_refs are the join key; keep them unique"


def test_every_claim_is_labelled_with_one_of_the_four_statuses() -> None:
    for claim in _claims():
        assert claim["expected_status"] in VERIFICATION_STATUSES
        assert "status" not in claim, (
            "`status` is what the VERIFIER produces; the approved label is `expected_status` "
            "and the two sides of an S8 comparison must never share a spelling"
        )


def test_all_four_statuses_appear_and_one_pitch_carries_all_four() -> None:
    seen = {c["expected_status"] for c in _claims()}
    assert seen == VERIFICATION_STATUSES, f"missing {sorted(VERIFICATION_STATUSES - seen)}"
    assert any(
        {c["expected_status"] for c in pitch["claims"]} == VERIFICATION_STATUSES
        for pitch in PITCHES
    ), "S8 needs one pitch labelled with all four statuses at once"


def test_a_single_pitch_mixes_true_and_false_claims() -> None:
    assert any(
        {"verified", "contradicted"} <= {c["expected_status"] for c in pitch["claims"]}
        for pitch in PITCHES
    ), "S8 names a golden pitch containing BOTH true and false claims"


def test_every_design_eval_gate_has_a_case() -> None:
    seen = {_token(g) for pitch in PITCHES for g in pitch["gates"]}
    missing = [name for name, aliases in EVAL_GATES.items() if not (aliases & seen)]
    assert not missing, f"no golden case for eval gates {missing}; gates present: {sorted(seen)}"


def test_product_facts_are_labelled_in_both_directions() -> None:
    table = MANIFEST["claim_type_dimensions"]
    by_status: dict[str, list] = {}
    for claim in _claims():
        if table[claim["claim_type"]] == CATALOG_DIM:
            by_status.setdefault(claim["expected_status"], []).append(claim["claim_ref"])
    assert by_status.get("contradicted"), (
        "no FALSE product-fact claim — the case the sixth dimension exists for"
    )
    assert by_status.get("verified"), (
        "no TRUE product-fact claim, so a verifier that contradicted every product fact "
        f"would still be graded green on {CATALOG_DIM!r}"
    )


def test_each_pitch_carries_the_catalog_snapshot_its_labels_were_derived_from() -> None:
    for pitch in PITCHES:
        snapshot = pitch["catalog_snapshot"]
        assert snapshot["snapshot_id"] and snapshot["products"]
        refs = {p["product_ref"] for p in snapshot["products"]}
        assert pitch["product_ref"] in refs, (
            f"{pitch['pitch_id']} is pitched about {pitch['product_ref']!r}, which its own "
            f"snapshot does not contain: {sorted(refs)}"
        )
        for claim in pitch["claims"]:
            assert claim["product_ref"] in refs
            assert claim["provenance"]["source"] == "seller_asserted", (
                "a pitch claim arrives asserted by the seller; the verifier is what turns it "
                "into evidence (R8)"
            )


def test_verified_and_contradicted_claims_are_decidable_from_their_snapshot() -> None:
    """Every decided label must have an attribute in the snapshot to decide it against.

    Without this the golden set could ask the verifier for an answer no evidence supports,
    and T-065 would be graded against an unreachable target.
    """
    for pitch in PITCHES:
        products = {p["product_ref"]: p for p in pitch["catalog_snapshot"]["products"]}
        for claim in pitch["claims"]:
            if claim["expected_status"] not in {"verified", "contradicted"}:
                continue
            attributes = products[claim["product_ref"]]["attributes"]
            offer = products[claim["product_ref"]].get("offer", {})
            assert claim["key"] in attributes or claim["key"] in offer, (
                f"{pitch['pitch_id']}#{claim['claim_ref']} is labelled "
                f"{claim['expected_status']!r} on key {claim['key']!r}, but the snapshot "
                f"carries neither that attribute nor that offer field"
            )


def test_unsupported_claims_have_no_evidence_to_decide_them() -> None:
    """The mirror image: `unsupported` must mean absent or stale evidence, not laziness."""
    for pitch in PITCHES:
        products = {p["product_ref"]: p for p in pitch["catalog_snapshot"]["products"]}
        for claim in pitch["claims"]:
            if claim["expected_status"] != "unsupported":
                continue
            product = products[claim["product_ref"]]
            attribute = product["attributes"].get(claim["key"])
            stale = bool(
                (attribute or {}).get("stale")
                or product.get("stale")
                or pitch["catalog_snapshot"].get("stale")
                or pitch["catalog_snapshot"].get("partial")
            )
            assert attribute is None or stale, (
                f"{pitch['pitch_id']}#{claim['claim_ref']} is labelled unsupported, but the "
                "snapshot carries fresh evidence for it"
            )


def test_every_claim_carries_the_dimension_its_type_routes_to() -> None:
    table = MANIFEST["claim_type_dimensions"]
    for pitch in PITCHES:
        for claim in pitch["claims"]:
            assert claim["expected_dimension"] == table[claim["claim_type"]], (
                f"{pitch['pitch_id']}#{claim['claim_ref']} records dimension "
                f"{claim['expected_dimension']!r} but its claim_type routes to "
                f"{table[claim['claim_type']]!r}"
            )


def test_the_injection_case_really_contains_an_injection() -> None:
    injections = [
        p for p in PITCHES if {"injection", "prompt_injection"} & {_token(g) for g in p["gates"]}
    ]
    assert injections
    text = injections[0]["text"].lower()
    assert "ignore" in text and "instruction" in text, (
        "the injection case must carry text that tries to steer the verifier, or C10 is "
        "never exercised"
    )
    assert any(c["expected_status"] == "contradicted" for c in injections[0]["claims"]), (
        "the injected pitch must contain a claim the injection is trying to launder — a "
        "verifier that obeyed the text would return 'verified' for it"
    )


def test_the_golden_intents_are_present_and_referenced() -> None:
    intents = {i["intent_id"] for i in GOLDEN["intents"]}
    assert intents
    assert MANIFEST["fixture_intent"]["intent_id"] in intents
    for pitch in PITCHES:
        assert pitch["intent_ref"] in intents, (
            f"{pitch['pitch_id']} answers intent {pitch['intent_ref']!r}, which the golden "
            "set does not define"
        )
