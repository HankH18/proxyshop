"""T-080 — the approved golden set is a usable answer key for the S8 eval gates.

`packages/verification` (T-065) is *graded against* this document and never authors it
(D11), so everything the verifier will be asked to reproduce has to be present and
self-consistent here first: four statuses, every DESIGN eval gate, a pitch that mixes true
and false claims, product facts in both directions, and a catalog snapshot per pitch that
actually contains the evidence its labels were derived from.
"""

from __future__ import annotations

import re
from datetime import datetime

from fixtures.manifest import (
    CATALOG_DIM,
    PRODUCT_FACT_CLAIM_TYPES,
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


#: Unit spellings that denote the same unit on either side of a comparison.
_UNIT_ALIASES = {"%": "percent"}

#: ``unit -> (dimension, factor to that dimension's base unit)``. Two spellings of the SAME
#: magnitude — "1 kg" and 1000 g — must compare equal, or the answer key cannot express a
#: true claim written in a different unit from the one the snapshot recorded. Note this does
#: NOT soften the wrong-units gate: "120 g" against 120 mg converts to 120 g vs 0.12 g and
#: is still a mismatch, because the magnitudes really do differ. Bare ``m`` is deliberately
#: absent — coffee data spells months "months", and a unit table that guessed metres there
#: would turn a warranty claim into a length.
_UNIT_SCALE = {
    "mg": ("mass", 0.001),
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "ml": ("volume", 1.0),
    "l": ("volume", 1000.0),
    "mm": ("length", 1.0),
    "cm": ("length", 10.0),
}


def _in_base_units(number: float, unit: str | None) -> tuple[str, float] | None:
    """``(dimension, magnitude in the base unit)``, or ``None`` for an unknown unit."""
    entry = _UNIT_SCALE.get(unit or "")
    if entry is None:
        return None
    dimension, factor = entry
    return dimension, number * factor


#: How a boolean attribute is asserted in prose.
_TRUTHY = {"true", "yes", "eligible", "included", "supported"}
_FALSY = {"false", "no", "ineligible", "excluded", "not supported"}

_NUMBER_UNIT = re.compile(r"^\s*\$?(?P<number>-?\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z%µ°/]+)?\s*$")


def _norm_unit(unit: object) -> str | None:
    if unit is None:
        return None
    text = str(unit).strip().lower()
    return _UNIT_ALIASES.get(text, text)


def _norm(value: object) -> str:
    return str(value).strip().lower()


def _claim_agrees_with_evidence(claimed: object, evidence: object) -> bool | None:
    """Does the claim's asserted value agree with the snapshot's recorded one?

    ``None`` means *not comparable* — the caller treats that as a failure, never as a pass.
    A comparator that returned "can't tell" as "fine" would be the same fake guard again.
    """
    if isinstance(evidence, dict):
        actual, unit = evidence.get("value"), _norm_unit(evidence.get("unit"))
    else:
        actual, unit = evidence, None

    if isinstance(actual, bool):
        text = _norm(claimed)
        if text in _TRUTHY:
            return actual is True
        if text in _FALSY:
            return actual is False
        return None
    if isinstance(actual, list):
        return _norm(claimed) in {_norm(item) for item in actual}
    if isinstance(actual, (int, float)):
        match = _NUMBER_UNIT.match(str(claimed))
        if match is None:
            return None
        claimed_unit = _norm_unit(match.group("unit"))
        number = float(match.group("number"))
        if claimed_unit != unit:
            # A number in the wrong unit is a MISMATCH, not a match: "120 g" against 120 mg
            # is precisely the wrong_units gate, and it must not compare equal on the digits.
            # Different SPELLINGS of the same magnitude are a different thing, though, and
            # they must compare equal: "1 kg" against 1000 g is one quantity written two
            # ways. Both are decided here by converting to a common base unit, so the gate
            # keeps its teeth (120 g != 0.12 g) without the answer key being forced to
            # restate every claim in whatever unit the snapshot happened to use.
            claimed_base = _in_base_units(number, claimed_unit)
            actual_base = _in_base_units(float(actual), unit)
            if claimed_base is None or actual_base is None:
                return False
            if claimed_base[0] != actual_base[0]:
                return False
            return claimed_base[1] == actual_base[1]
        return number == float(actual)
    if isinstance(actual, str):
        return _norm(claimed) == _norm(actual)
    return None


def test_verified_and_contradicted_claims_actually_agree_with_their_snapshot() -> None:
    """The answer key's labels must be TRUE of the evidence, not merely adjacent to it.

    This used to assert only that ``claim["key"]`` appeared somewhere in the snapshot. That
    guard could refuse nothing that matters: every ``verified``/``contradicted`` label in the
    document could be inverted — `gp-004-c-caffeine`'s "120 g" against 120 mg relabelled
    ``verified``, `gp-010-c-total`'s "$24.00" against 31.00 relabelled ``verified``,
    `gp-011`'s two true claims relabelled ``contradicted`` — and the whole suite stayed green.
    Since `packages/verification` (T-065) is graded against exactly these labels and never
    authors them (D11), an unguarded answer key mis-grades the verifier silently and forever.

    So: resolve each claim's key to its snapshot value and require agreement iff the label
    says ``verified``. A claim whose value cannot be compared to its evidence at all fails
    here too — an answer key nobody can check is not an answer key.
    """
    checked = 0
    for pitch in PITCHES:
        products = {p["product_ref"]: p for p in pitch["catalog_snapshot"]["products"]}
        for claim in pitch["claims"]:
            if claim["expected_status"] not in {"verified", "contradicted"}:
                continue
            product = products[claim["product_ref"]]
            where = f"{pitch['pitch_id']}#{claim['claim_ref']}"
            attributes, offer = product["attributes"], product.get("offer", {})
            assert claim["key"] in attributes or claim["key"] in offer, (
                f"{where} is labelled {claim['expected_status']!r} on key {claim['key']!r}, "
                "but the snapshot carries neither that attribute nor that offer field"
            )
            evidence = attributes.get(claim["key"], offer.get(claim["key"]))
            agrees = _claim_agrees_with_evidence(claim["value"], evidence)
            assert agrees is not None, (
                f"{where} claims {claim['value']!r} but its evidence {evidence!r} cannot be "
                "compared to it, so the label rests on nothing a reader can check"
            )
            assert agrees is (claim["expected_status"] == "verified"), (
                f"{where} is labelled {claim['expected_status']!r}, but its claimed value "
                f"{claim['value']!r} {'agrees with' if agrees else 'disagrees with'} the "
                f"snapshot evidence {evidence!r}. The answer key is wrong."
            )
            checked += 1
    assert checked >= 20, f"only {checked} decided claims were value-checked"


def test_unsupported_claims_have_no_evidence_to_decide_them() -> None:
    """The mirror image: `unsupported` must mean absent or stale evidence, not laziness."""
    for pitch in PITCHES:
        products = {p["product_ref"]: p for p in pitch["catalog_snapshot"]["products"]}
        for claim in pitch["claims"]:
            if claim["expected_status"] != "unsupported":
                continue
            product = products[claim["product_ref"]]
            attribute = product["attributes"].get(claim["key"])
            # `partial` is deliberately NOT in this disjunction. A snapshot flagged partial
            # would excuse `unsupported` on every attribute it does carry — `gp-008-c-burrs`
            # (64 mm, present and fresh) could be relabelled unsupported and stay green. What
            # makes a claim unsupported is that ITS evidence is absent or stale, so that is
            # what is checked; no committed claim needs the looser rule.
            stale = bool(
                (attribute or {}).get("stale")
                or product.get("stale")
                or pitch["catalog_snapshot"].get("stale")
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


# =====================================================================================
# The eval gates, graded by their CONTENT rather than by their label
# =====================================================================================
# `test_every_design_eval_gate_has_a_case` above asserts that each DESIGN eval gate is
# NAMED by some pitch. That is necessary and it is not close to sufficient: it grades the
# string in `pitch["gates"]`, so the thing the gate exists to catch can be deleted outright
# and the assertion stays green. Measured, before these checks existed: deleting
# `gp-003`'s decoy 1 kg variant — the entire wrong-variant trap — left the suite passing,
# and so did deleting `gp-006-c-fairtrade`, the false half of the claim-splitting case. A
# gate that survives deletion of the thing it gates is grading nothing.
#
# So every canonical gate gets a checker that reads the pitch's own data, and
# `GATE_CONTENT_CHECKS` must cover `EVAL_GATES` exactly — a gate added to the golden set
# with no content grader fails here rather than passing by having been named.


def _snapshot_products(pitch: dict) -> dict:
    return {p["product_ref"]: p for p in pitch["catalog_snapshot"]["products"]}


def _evidence_for(product: dict, key: str):
    """The snapshot value a claim's ``key`` resolves to, or ``None`` if there is none."""
    attributes, offer = product["attributes"], product.get("offer", {})
    if key in attributes:
        return attributes[key]
    return offer.get(key)


def _decided(pitch: dict, status: str) -> list:
    return [c for c in pitch["claims"] if c["expected_status"] == status]


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _disagrees_with_its_own_evidence(pitch: dict, claim: dict) -> bool:
    product = _snapshot_products(pitch)[claim["product_ref"]]
    return (
        _claim_agrees_with_evidence(claim["value"], _evidence_for(product, claim["key"])) is False
    )


def _check_contradiction(pitch: dict) -> None:
    false_claims = [
        c for c in _decided(pitch, "contradicted") if _disagrees_with_its_own_evidence(pitch, c)
    ]
    assert false_claims, (
        f"{pitch['pitch_id']} is tagged `contradiction` but carries no claim that actually "
        "disagrees with its own snapshot evidence"
    )


def _check_stale_evidence(pitch: dict) -> None:
    snapshot = pitch["catalog_snapshot"]
    window = snapshot.get("freshness_window_days")
    assert isinstance(window, int) and not isinstance(window, bool) and window > 0, (
        f"{pitch['pitch_id']} is tagged `stale_evidence` but publishes no positive "
        "freshness_window_days, so nothing in the document says what 'stale' means here"
    )
    captured = _instant(snapshot["captured_at"])
    asserted = max(_instant(c["provenance"]["observed_at"]) for c in pitch["claims"])
    age_days = (asserted - captured).total_seconds() / 86400
    assert age_days > window, (
        f"{pitch['pitch_id']} is tagged `stale_evidence` but its snapshot was captured "
        f"{age_days:.1f} days before the pitch was made, inside its own "
        f"{window}-day freshness window — the evidence is not stale"
    )

    # Stale is not the same failure as absent (that is the timeout gate): the deciding
    # evidence must be PRESENT and marked stale, or the two gates are the same case twice.
    stale_but_present = []
    for claim in _decided(pitch, "unsupported"):
        product = _snapshot_products(pitch)[claim["product_ref"]]
        attribute = product["attributes"].get(claim["key"])
        if attribute is None:
            continue
        if attribute.get("stale") or product.get("stale") or snapshot.get("stale"):
            stale_but_present.append(claim["claim_ref"])
    assert stale_but_present, (
        f"{pitch['pitch_id']} is tagged `stale_evidence` but no `unsupported` claim of its "
        "rests on evidence that is present-and-stale. Stale evidence that is simply missing "
        "is the timeout case, not this one"
    )


def _check_wrong_variant(pitch: dict) -> None:
    """The trap is a DECOY: a sibling variant of which the false claim is true.

    Without the decoy there is nothing to resolve wrongly — the claim is merely false, which
    is the contradiction gate. So the pitch must carry a same-named sibling variant, and a
    `contradicted` claim that is false of the offered variant and TRUE of that sibling. That
    is the exact mistake this gate exists to catch: resolving by `canonical_name` instead of
    by variant.
    """
    products = _snapshot_products(pitch)
    offered = products[pitch["product_ref"]]
    siblings = [
        p
        for ref, p in products.items()
        if ref != pitch["product_ref"] and p["canonical_name"] == offered["canonical_name"]
    ]
    assert siblings, (
        f"{pitch['pitch_id']} is tagged `wrong_variant` but its snapshot holds no OTHER "
        f"variant of {offered['canonical_name']!r} to be confused with "
        f"(products: {sorted(products)}). With no decoy there is nothing to resolve wrongly"
    )

    traps = []
    for claim in _decided(pitch, "contradicted"):
        if claim["product_ref"] != pitch["product_ref"]:
            continue
        if (
            _claim_agrees_with_evidence(claim["value"], _evidence_for(offered, claim["key"]))
            is not False
        ):
            continue
        for sibling in siblings:
            evidence = _evidence_for(sibling, claim["key"])
            if evidence is None:
                continue
            if _claim_agrees_with_evidence(claim["value"], evidence) is True:
                traps.append((claim["claim_ref"], sibling["product_ref"]))
    assert traps, (
        f"{pitch['pitch_id']} is tagged `wrong_variant` but no contradicted claim of its is "
        "TRUE of a same-named sibling variant, so a verifier that resolved by name instead "
        "of by variant would get the right answer by accident and the gate catches nobody"
    )


def _check_wrong_units(pitch: dict) -> None:
    """The digits agree and the unit does not — the whole point of the gate."""
    traps = []
    for claim in _decided(pitch, "contradicted"):
        product = _snapshot_products(pitch)[claim["product_ref"]]
        evidence = _evidence_for(product, claim["key"])
        if not isinstance(evidence, dict) or not isinstance(evidence.get("value"), (int, float)):
            continue
        match = _NUMBER_UNIT.match(str(claim["value"]))
        if match is None:
            continue
        same_digits = float(match.group("number")) == float(evidence["value"])
        different_unit = _norm_unit(match.group("unit")) != _norm_unit(evidence.get("unit"))
        if same_digits and different_unit:
            traps.append(claim["claim_ref"])
    assert traps, (
        f"{pitch['pitch_id']} is tagged `wrong_units` but carries no contradicted claim "
        "whose NUMBER matches the snapshot while its UNIT does not. A verifier that ignored "
        "units entirely would score every claim here correctly"
    )


def _check_injection(pitch: dict) -> None:
    text = pitch["text"].lower()
    assert "ignore" in text and "instruction" in text, (
        f"{pitch['pitch_id']} is tagged `injection` but its text does not try to steer the "
        "verifier, so C10 is never exercised"
    )
    assert _decided(pitch, "contradicted"), (
        f"{pitch['pitch_id']} is tagged `injection` but carries no false claim for the "
        "injection to launder — a verifier that obeyed the text would still be right"
    )


def _check_claim_splitting(pitch: dict) -> None:
    """One sentence, several atomic claims, and not one truth value between them."""
    claims = pitch["claims"]
    assert len(claims) >= 3, (
        f"{pitch['pitch_id']} is tagged `claim_splitting` but carries {len(claims)} claims; "
        "a sentence that decomposes into fewer than three atoms is not a splitting case"
    )
    assert len({c["expected_status"] for c in claims}) >= 2, (
        f"{pitch['pitch_id']} is tagged `claim_splitting` but every one of its claims has "
        "the same expected_status, so scoring the whole sentence as one claim gets them all "
        "right and the gate proves nothing"
    )
    for claim in claims:
        assert len(claim["text"]) < len(pitch["text"]), (
            f"{pitch['pitch_id']}#{claim['claim_ref']} is not an ATOM of its pitch: its text "
            "is as long as the whole pitch"
        )


def _check_verbosity_gaming(pitch: dict) -> None:
    text = pitch["text"]
    assert len(text) >= 400, (
        f"{pitch['pitch_id']} is tagged `verbosity_gaming` but its pitch is only "
        f"{len(text)} characters; there is no volume for the claims to hide in"
    )
    claimed = sum(len(c["text"]) for c in pitch["claims"])
    assert claimed * 4 <= len(text), (
        f"{pitch['pitch_id']} is tagged `verbosity_gaming` but {claimed} of its "
        f"{len(text)} characters carry a labelled claim. Verbosity gaming is volume that "
        "asserts nothing checkable; a pitch that is mostly claims is not gaming anything"
    )
    assert _decided(pitch, "ambiguous"), (
        f"{pitch['pitch_id']} is tagged `verbosity_gaming` but carries no `ambiguous` claim. "
        "The unfalsifiable flourish is the thing the gate is about; without one, a verifier "
        "that decided every sentence confidently would score perfectly"
    )
    assert [c for c in pitch["claims"] if c["expected_status"] != "ambiguous"], (
        f"{pitch['pitch_id']} labels every claim ambiguous, so a verifier that refused to "
        "decide anything would be graded green"
    )


def _check_timeout(pitch: dict) -> None:
    snapshot = pitch["catalog_snapshot"]
    assert snapshot.get("partial") is True, (
        f"{pitch['pitch_id']} is tagged `timeout` but its snapshot is not flagged partial"
    )
    notes = " ".join(snapshot.get("retrieval_notes") or []).lower()
    assert any(word in notes for word in ("timeout", "timed out", "exceeded", "budget")), (
        f"{pitch['pitch_id']} is tagged `timeout` but its snapshot records no retrieval note "
        f"saying what timed out (retrieval_notes: {snapshot.get('retrieval_notes')!r})"
    )
    products = _snapshot_products(pitch)
    absent = [
        c["claim_ref"]
        for c in _decided(pitch, "unsupported")
        if _evidence_for(products[c["product_ref"]], c["key"]) is None
    ]
    assert absent, (
        f"{pitch['pitch_id']} is tagged `timeout` but no `unsupported` claim of its is "
        "missing its evidence. A timeout must degrade a claim to unsupported — if the "
        "evidence is there, nothing timed out"
    )
    assert [c for c in pitch["claims"] if c["expected_status"] in {"verified", "contradicted"}], (
        f"{pitch['pitch_id']} decides nothing at all. A partial retrieval must still decide "
        "the claims it DOES have evidence for, or 'degrade' means 'give up'"
    )


def _check_duplicate_pitch(pitch: dict) -> None:
    """A duplicate must actually be one — same text, same snapshot, same labels."""
    by_id = {p["pitch_id"]: p for p in PITCHES}
    original_id = pitch.get("duplicate_of")
    assert original_id, (
        f"{pitch['pitch_id']} is tagged `duplicate_pitch` but does not say which pitch it "
        "duplicates, so nothing can check that it is one"
    )
    assert original_id in by_id, f"{pitch['pitch_id']}.duplicate_of names {original_id!r}, absent"
    original = by_id[original_id]

    assert pitch["text"] == original["text"], (
        f"{pitch['pitch_id']} claims to duplicate {original_id} but their pitch texts differ"
    )
    assert pitch["catalog_snapshot"] == original["catalog_snapshot"], (
        f"{pitch['pitch_id']} claims to duplicate {original_id} but their catalog snapshots "
        "differ. Verification is idempotent per (pitch, verifier version, SNAPSHOT), so two "
        "pitches that differ in their snapshot are not the same input and must not be "
        "expected to produce the same answer"
    )

    def payload(p: dict) -> list:
        return [
            (c["key"], c["value"], c["claim_type"], c["expected_status"], c["expected_dimension"])
            for c in p["claims"]
        ]

    assert payload(pitch) == payload(original), (
        f"{pitch['pitch_id']} claims to duplicate {original_id} but the two carry different "
        "claims or different labels — 'the SAME outcome must come back' is then untestable"
    )


def _check_expired_offer(pitch: dict) -> None:
    snapshot = pitch["catalog_snapshot"]
    captured = _instant(snapshot["captured_at"])
    expired = []
    for product in snapshot["products"]:
        offer = product.get("offer") or {}
        expires_at = offer.get("expires_at")
        if expires_at and _instant(expires_at) < captured:
            expired.append(product["product_ref"])
    assert expired, (
        f"{pitch['pitch_id']} is tagged `expired_offer` but no offer in its snapshot records "
        "an expires_at already past at capture time, so nothing about it has expired"
    )
    offer_dimensions = {"price_honored", "discount_honored"}
    lapsed = [
        c["claim_ref"]
        for c in _decided(pitch, "contradicted")
        if c["expected_dimension"] in offer_dimensions
    ]
    assert lapsed, (
        f"{pitch['pitch_id']} is tagged `expired_offer` but contradicts no price or discount "
        "claim; the lapsed offer has to be what makes a claim false, or the gate is scenery"
    )


def _check_blacklist_expiry(pitch: dict) -> None:
    state = pitch.get("seller_state") or {}
    blacklist = state.get("blacklist") or {}
    assert blacklist, (
        f"{pitch['pitch_id']} is tagged `blacklist_expiry` but records no seller_state."
        "blacklist, so there is no expired entry for anything to be wrong about"
    )
    expires_at = blacklist.get("expires_at")
    assert expires_at, f"{pitch['pitch_id']}'s blacklist entry has no expires_at"
    captured = _instant(pitch["catalog_snapshot"]["captured_at"])
    assert _instant(expires_at) < captured, (
        f"{pitch['pitch_id']} is tagged `blacklist_expiry` but its blacklist entry expires "
        f"at {expires_at}, which is not before the snapshot's {pitch['catalog_snapshot']['captured_at']}. "
        "An entry that is still live exercises the blacklist, not its expiry"
    )
    assert str(blacklist.get("status", "")).lower() == "expired", (
        f"{pitch['pitch_id']}'s blacklist entry is not recorded as expired"
    )
    undecided = [c["claim_ref"] for c in pitch["claims"] if c["expected_status"] != "verified"]
    assert not undecided, (
        f"{pitch['pitch_id']} is tagged `blacklist_expiry` but labels {undecided} something "
        "other than verified. The case is a rehabilitated seller telling the TRUTH: if its "
        "claims were false, a system that wrongly kept the expired entry live would look "
        "right for the wrong reason"
    )


#: One content grader per canonical eval gate. Keyed by the canonical names in EVAL_GATES.
GATE_CONTENT_CHECKS = {
    "contradiction": _check_contradiction,
    "stale_evidence": _check_stale_evidence,
    "wrong_variant": _check_wrong_variant,
    "wrong_units": _check_wrong_units,
    "injection": _check_injection,
    "claim_splitting": _check_claim_splitting,
    "verbosity_gaming": _check_verbosity_gaming,
    "timeout": _check_timeout,
    "duplicate_pitch": _check_duplicate_pitch,
    "expired_offer": _check_expired_offer,
    "blacklist_expiry": _check_blacklist_expiry,
}


def test_every_eval_gate_has_a_content_grader() -> None:
    """A gate with no content grader would be back to being graded by its label."""
    assert set(GATE_CONTENT_CHECKS) == set(EVAL_GATES), (
        "every DESIGN eval gate needs a checker that reads the pitch's data: missing "
        f"{sorted(set(EVAL_GATES) - set(GATE_CONTENT_CHECKS))}, unknown "
        f"{sorted(set(GATE_CONTENT_CHECKS) - set(EVAL_GATES))}"
    )


def test_every_pitch_tagged_with_a_gate_really_carries_that_gates_case() -> None:
    """Each gate is graded against the pitch's own data, not against the string in `gates`.

    Every pitch that NAMES a gate is checked, not merely one of them: a gate satisfied by
    some other pitch would let the case that names it be hollowed out silently.
    """
    for canonical, aliases in sorted(EVAL_GATES.items()):
        tagged = [p for p in PITCHES if aliases & {_token(g) for g in p["gates"]}]
        assert tagged, f"no golden case for eval gate {canonical!r}"
        for pitch in tagged:
            GATE_CONTENT_CHECKS[canonical](pitch)


def test_the_claim_splitting_case_carries_an_indistinguishable_pair() -> None:
    """The hardest splitting case: two atoms on the SAME key with opposite truth values.

    `gp-006` asserts "certified organic and fair-trade" where the snapshot's certifications
    list holds `organic` only. Both halves are claims about `certifications`, so a verifier
    that scores the sentence as one claim cannot be accidentally right — it either passes
    the false half or fails the true one. Claims on *different* keys do not have that
    property: a verifier could get them right one at a time without splitting anything.

    This is the assertion that made deleting `gp-006-c-fairtrade` turn the suite red.
    """
    for pitch in PITCHES:
        if not (EVAL_GATES["claim_splitting"] & {_token(g) for g in pitch["gates"]}):
            continue
        by_key: dict[str, set] = {}
        for claim in pitch["claims"]:
            by_key.setdefault(claim["key"], set()).add(claim["expected_status"])
        if any({"verified", "contradicted"} <= statuses for statuses in by_key.values()):
            return
    raise AssertionError(
        "no claim-splitting pitch carries two claims on the SAME key with opposite truth "
        "values. Without that pair, every splitting case in the set can be scored correctly "
        "by a verifier that never splits a sentence, and the gate grades nothing"
    )


def test_a_snapshot_id_names_exactly_one_document() -> None:
    """Two pitches carrying the same snapshot_id must carry the same snapshot.

    `snapshot_id` is the evidence identity the verifier is idempotent over; if one id names
    two different documents, "same snapshot, same answer" is not a property the golden set
    can be graded on. `gp-009` used to differ from `gp-008` here — it dropped the
    `retrieval_notes` recording what timed out — while its own rationale said the two were
    byte-identical.
    """
    seen: dict[str, tuple[str, dict]] = {}
    for pitch in PITCHES:
        snapshot = pitch["catalog_snapshot"]
        snapshot_id = snapshot["snapshot_id"]
        if snapshot_id in seen:
            first_pitch, first = seen[snapshot_id]
            assert snapshot == first, (
                f"snapshot_id {snapshot_id!r} names two DIFFERENT documents: one in "
                f"{first_pitch}, another in {pitch['pitch_id']}"
            )
        else:
            seen[snapshot_id] = (pitch["pitch_id"], snapshot)


def test_every_product_fact_claim_type_is_labelled_in_both_directions() -> None:
    """Each of the four product-fact types needs a true case AND a false one.

    `test_product_facts_are_labelled_in_both_directions` proves it for the DIMENSION, which
    `specifications` alone satisfied: `ingredients` and `compatibility` had no case at all,
    and `nutrition` only a false one. So a verifier could contradict every ingredients claim
    it ever saw, or verify every compatibility claim, and be graded green on both.
    """
    for claim_type in PRODUCT_FACT_CLAIM_TYPES:
        statuses = {c["expected_status"] for c in _claims() if c["claim_type"] == claim_type}
        assert {"verified", "contradicted"} <= statuses, (
            f"product-fact claim type {claim_type!r} is labelled {sorted(statuses)} in the "
            "golden set; it needs at least one TRUE and one FALSE case or a verifier that "
            "answers the type the same way every time is graded green"
        )


def test_the_manifests_scripted_catalog_lies_have_a_golden_case() -> None:
    """The answer key must grade the lies the manifest actually scripts.

    Ground-truth direction: the manifest names the dishonest store's behaviours, and the
    golden set is read against it. `misrepresented_ingredients` is the flagship catalog lie
    the manifest scripts, and the golden set had no `ingredients` claim at all — so the demo
    could pass end to end while never grading the one product-fact lie it advertises.
    """
    table = MANIFEST["claim_type_dimensions"]
    scripted = sorted(
        {
            b["claim_type"]
            for b in MANIFEST["dishonest_store"]["behaviours"]
            if b.get("claim_type") and table[b["claim_type"]] == CATALOG_DIM
        }
    )
    assert scripted, "the manifest scripts no catalog-accuracy lie for the golden set to grade"
    for claim_type in scripted:
        assert any(
            c["claim_type"] == claim_type and c["expected_status"] == "contradicted"
            for c in _claims()
        ), (
            f"the manifest scripts a dishonest {claim_type!r} claim "
            f"(dishonest_store.behaviours) but the golden set labels no {claim_type!r} claim "
            "`contradicted`, so nothing grades the verifier on the lie the demo is built on"
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
