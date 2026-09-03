"""T-080 — the manifest is loadable, self-consistent ground truth, and honestly unapproved.

Two kinds of assertion live here and they are different in kind:

* **Consistency**, which a machine can prove: the digest chain covers the golden set and the
  category config, the claim-type table is typed and exhaustive, the trajectory crosses the
  published threshold inside the budget.
* **Approval state**, which a machine cannot prove. No offline test can distinguish a human's
  signature from an agent typing the human's name. What this file asserts is only that the
  manifest does not *claim* an approval it does not have — the block says
  ``pending_human_approval`` and names no approver. That check flips to red the moment an
  agent fabricates one, which is the only thing a test can usefully contribute to a human
  gate (EXECUTION.md rule 6).
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from fixtures import REPO_ROOT
from fixtures.approval import ApprovalRefused, check_approver
from fixtures.manifest import (
    CATALOG_DIM,
    GOLDEN_SET_PATH,
    MANIFEST_PATH,
    OFFER_FACT_CLAIM_DIMENSIONS,
    PRODUCT_FACT_CLAIM_TYPES,
    PUBLISHED_CLAIM_TYPES,
    TRUST_DIMENSIONS,
    VERIFICATION_STATUSES,
    DigestMismatchError,
    ManifestError,
    body_digest,
    file_digest,
    load_golden_set,
    load_manifest,
)

MANIFEST = load_manifest()


def test_exactly_one_manifest_exists_under_fixtures() -> None:
    strays = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in sorted((REPO_ROOT / "fixtures").rglob("manifest.json"))
        if p.is_file() and p != MANIFEST_PATH
    ]
    assert not strays, f"a second manifest makes every reader's ground truth ambiguous: {strays}"


def test_the_digest_chain_covers_the_golden_set_and_the_category_config() -> None:
    assert MANIFEST["golden_set"]["sha256"] == file_digest(GOLDEN_SET_PATH)
    assert MANIFEST["seed_catalog"]["sha256"] == file_digest(
        REPO_ROOT / MANIFEST["seed_catalog"]["path"]
    )
    assert MANIFEST["approval"]["content_hash"] == body_digest(MANIFEST)
    assert MANIFEST["golden_set"]["count"] == len(load_golden_set(MANIFEST)["pitches"])


def test_the_digest_chain_refuses_a_golden_set_edited_after_the_fact() -> None:
    """The guard has to be able to refuse something, or it is decoration.

    Nothing else in the repo catches this: the doctored manifest is valid JSON, passes every
    structural check, and points at a golden set that exists and parses. Only the recorded
    digest notices that the bytes are not the bytes that were approved.
    """
    doctored = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    doctored["golden_set"]["sha256"] = hashlib.sha256(b"a different answer key").hexdigest()
    with pytest.raises(DigestMismatchError):
        load_golden_set(doctored)


def test_the_claim_type_table_is_typed_and_exhaustive() -> None:
    table = MANIFEST["claim_type_dimensions"]
    assert set(PUBLISHED_CLAIM_TYPES) <= set(table), (
        f"unmapped: {sorted(set(PUBLISHED_CLAIM_TYPES) - set(table))}"
    )
    assert set(table.values()) <= TRUST_DIMENSIONS
    for claim_type in PRODUCT_FACT_CLAIM_TYPES:
        assert table[claim_type] == CATALOG_DIM, (
            f"product fact {claim_type!r} maps to {table[claim_type]!r}; a claim about what "
            "the goods ARE must not be scored as a broken promise"
        )
    for claim_type, dimension in OFFER_FACT_CLAIM_DIMENSIONS.items():
        assert table[claim_type] == dimension
    assert "feedback_match" not in set(table.values()), (
        "feedback_match takes no verification outcome: it is buyer-reported (R14/D53)"
    )


def test_the_table_states_the_treatment_of_every_verification_outcome() -> None:
    treatment = MANIFEST["claim_outcome_treatment"]
    assert set(treatment) == VERIFICATION_STATUSES
    assert treatment["ambiguous"]["moves_mean"] is False
    assert treatment["contradicted"]["weight"] == 2.0
    assert 0 < treatment["unsupported"]["weight"] < treatment["contradicted"]["weight"], (
        "unsupported is a published fractional weight STRICTLY BELOW contradicted (D53)"
    )
    for status in ("unsupported", "ambiguous"):
        assert treatment[status]["satisfies_hard_constraint"] is False, (
            f"{status} must never satisfy a hard constraint (R19)"
        )


def test_the_trajectory_starts_above_and_ends_below_the_blacklist_threshold() -> None:
    threshold = MANIFEST["blacklist_threshold"]
    trajectory = MANIFEST["expected_trust_trajectory"]
    episodes = [p["episode"] for p in trajectory]
    assert episodes == sorted(set(episodes)) and len(episodes) >= 2
    assert max(episodes) <= MANIFEST["episode_budget"]
    assert trajectory[0]["score"] > threshold, (
        "a trajectory that begins below the threshold proves nothing about the engine"
    )
    last = trajectory[-1]
    assert last["score"] + last["tolerance"] < threshold, (
        "S2: the trajectory must end below the threshold even at the top of its band"
    )


def test_load_manifest_refuses_a_five_dimension_manifest() -> None:
    """D53's amendment has to bite: a manifest written against the old vocabulary fails."""
    doctored = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for behaviour in doctored["dishonest_store"]["behaviours"]:
        if behaviour["dim"] == CATALOG_DIM:
            behaviour["dim"] = "feedback_match"
    with pytest.raises(ManifestError, match=CATALOG_DIM):
        from fixtures.manifest import validate_manifest

        validate_manifest(doctored)


def test_the_personas_and_the_fixture_intent_are_scripted_in_the_manifest() -> None:
    personas = MANIFEST["personas"]
    assert "aggressive" in personas, "T-045's adversarial persona is scripted here"
    scripted = personas["aggressive"]["scripted_claims"]
    assert scripted and all({"key", "value", "claim_type"} <= set(c) for c in scripted)
    for claim in scripted:
        assert claim["claim_type"] in MANIFEST["claim_type_dimensions"]
    assert any(c["truthful"] is False for c in scripted), (
        "the adversarial persona must actually assert something false"
    )
    intent = MANIFEST["fixture_intent"]
    assert isinstance(intent, dict) and intent.get("intent_id")


# ---------------------------------------------------------------------------------
# The human gate — what a test can and cannot say about it
# ---------------------------------------------------------------------------------
def test_the_manifest_does_not_claim_an_approval_it_does_not_have() -> None:
    approval = MANIFEST["approval"]
    if approval.get("status") == "approved":
        # A human has approved it. Then the two documents must agree — this is the branch
        # the frozen acceptance test grades.
        artifact = REPO_ROOT / approval["artifact"]
        assert artifact.is_file()
        record = artifact.read_text(encoding="utf-8")
        assert approval["content_hash"].lower() in record.lower()
        assert approval["approver"].strip() in record
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            approval["approved_at"].strip(),
        )
        assert approval["content_hash"] == body_digest(MANIFEST)
        return

    assert approval["status"] == "pending_human_approval"
    assert approval["approver"] is None, (
        "an agent has written an approver into a manifest nobody approved — "
        "EXECUTION.md rule 6 forbids fabricating this artifact"
    )
    assert approval["approved_at"] is None
    assert approval["artifact"] is None
    assert (REPO_ROOT / approval["request"]).is_file()


def test_the_approval_request_quotes_the_current_digest() -> None:
    """If the manifest changes, the thing a human is being asked to approve changes too."""
    request = (REPO_ROOT / MANIFEST["approval"]["request"]).read_text(encoding="utf-8")
    assert body_digest(MANIFEST) in request, (
        "the approval request quotes a digest that is no longer the manifest's — re-generate "
        "the request rather than letting a human approve a stale summary"
    )


def test_the_approval_tool_refuses_to_let_automation_self_approve() -> None:
    """The one check on this path with teeth. It must reject, not merely exist."""
    for name in ("Claude", "claude opus", "the swarm agent", "an AI", "gpt-5", "automation"):
        with pytest.raises(ApprovalRefused, match="automation"):
            check_approver(name)
    for name in ("TBD", "todo", "none", "n/a", ""):
        with pytest.raises(ApprovalRefused):
            check_approver(name)
    assert check_approver("  Ada Lovelace  ") == "Ada Lovelace"


# ---------------------------------------------------------------------------------
# The guards added after the T-080 audit. Each one must REFUSE something, or it is
# decoration: a validator nobody has watched reject a bad document is an assertion
# that the document happens to be good today.
# ---------------------------------------------------------------------------------
def _doctored() -> dict:
    """A fresh, mutable copy of the committed manifest to break in one specific way."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _staged_tree(tmp_path, manifest: dict | None = None):
    """A minimal repo-shaped tree: `<root>/fixtures/{manifest.json,golden/,catalog/}`.

    Referenced documents are repo-relative, so a manifest only means anything underneath a
    root laid out this way. Staging one is how the resolution boundary gets tested without
    touching this checkout.
    """
    from fixtures.manifest import GOLDEN_SET_REL, MANIFEST_REL

    root = tmp_path / "staged"
    for rel in (MANIFEST_REL, GOLDEN_SET_REL, MANIFEST["seed_catalog"]["path"]):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes((REPO_ROOT / rel).read_bytes())
    if manifest is not None:
        (root / MANIFEST_REL).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return root / MANIFEST_REL


def test_load_manifest_verifies_the_manifests_own_body_digest(tmp_path) -> None:
    """`approval.content_hash` is the digest the other two hang off, and no runtime consumer
    checked it.

    `golden_set.sha256` and `seed_catalog.sha256` were both verified on load; the digest
    covering the manifest's OWN body was not, so every program that read the manifest
    trusted a `content_hash` its own code path had never compared against the document. The
    acceptance suite checks it once; this makes every read check it, which is what a
    consumer running outside the suite needs.
    """
    doctored = _doctored()
    doctored["episode_budget"] = 99  # inside the body the digest covers
    doctored["expected_trust_trajectory"] = MANIFEST["expected_trust_trajectory"]
    path = _staged_tree(tmp_path, doctored)

    with pytest.raises(DigestMismatchError, match="content_hash"):
        load_manifest(path)

    # ...and the same document loads once its digest is honestly re-pinned.
    from fixtures.manifest import refresh_digests

    refresh_digests(path)
    assert load_manifest(path)["episode_budget"] == 99


def test_load_manifest_resolves_referenced_documents_against_the_manifests_own_root(
    tmp_path,
) -> None:
    """A foreign manifest must be graded against ITS documents, not against this repo's.

    Referenced paths were resolved against `fixtures.REPO_ROOT`, so
    `load_manifest('/somewhere/else/fixtures/manifest.json')` validated somebody else's
    document — including whatever `approval` block it chose to carry — against THIS
    repository's approved golden set and category config, and reported it sound.
    """
    path = _staged_tree(tmp_path)
    golden = path.parent / "golden" / "golden_set.json"
    golden.write_text('{"pitches": [{"pitch_id": "fabricated"}]}\n', encoding="utf-8")

    with pytest.raises(DigestMismatchError, match="golden_set"):
        load_manifest(path)

    # The repo's own golden set was never consulted and is untouched.
    assert file_digest(GOLDEN_SET_PATH) == MANIFEST["golden_set"]["sha256"]


def test_load_manifest_refuses_a_manifest_that_has_no_root_to_resolve_against(tmp_path) -> None:
    stray = tmp_path / "manifest.json"
    stray.write_bytes(MANIFEST_PATH.read_bytes())
    with pytest.raises(ManifestError, match="fixtures/manifest.json"):
        load_manifest(stray)


def test_a_behaviour_may_not_land_on_a_dimension_its_claim_type_does_not_route_to() -> None:
    """D53 on the SCRIPTED side: `behaviours[].claim_type` was carried and never checked.

    A behaviour could therefore name `ingredients` while declaring `dim: price_honored`, and
    the simulator (which reads `kind`) and the trust engine (which reads `dim`) would
    disagree about the same scripted lie while both truthfully saying "the manifest says so".
    """
    from fixtures.manifest import validate_manifest

    doctored = _doctored()
    for behaviour in doctored["dishonest_store"]["behaviours"]:
        if behaviour["kind"] == "misrepresented_ingredients":
            behaviour["dim"] = "price_honored"
    with pytest.raises(ManifestError, match="routes it to"):
        validate_manifest(doctored)


def test_only_feedback_match_may_script_a_behaviour_with_no_claim_type() -> None:
    from fixtures.manifest import UnmappedClaimTypeError, validate_manifest

    doctored = _doctored()
    for behaviour in doctored["dishonest_store"]["behaviours"]:
        if behaviour["kind"] == "phantom_discount":
            behaviour["claim_type"] = None
    with pytest.raises(ManifestError, match="feedback_match"):
        validate_manifest(doctored)

    doctored = _doctored()
    for behaviour in doctored["dishonest_store"]["behaviours"]:
        if behaviour["kind"] == "phantom_discount":
            behaviour["claim_type"] = "loyalty_points"
    with pytest.raises(UnmappedClaimTypeError):
        validate_manifest(doctored)


def test_the_treatment_of_each_outcome_is_checked_not_merely_present() -> None:
    """`claim_outcome_treatment` was validated for KEY PRESENCE only.

    `{"ambiguous": {}}` satisfied that, while T-080 acceptance 4 is about what each outcome
    DOES to trust — and the trust engine is graded against these numbers.
    """
    from fixtures.manifest import validate_manifest

    doctored = _doctored()
    doctored["claim_outcome_treatment"]["ambiguous"]["moves_mean"] = True
    with pytest.raises(ManifestError, match="ambiguous"):
        validate_manifest(doctored)

    doctored = _doctored()
    doctored["claim_outcome_treatment"]["unsupported"]["weight"] = 2.5
    with pytest.raises(ManifestError, match="unsupported"):
        validate_manifest(doctored)

    doctored = _doctored()
    doctored["claim_outcome_treatment"]["unsupported"]["satisfies_hard_constraint"] = True
    with pytest.raises(ManifestError, match="hard constraint"):
        validate_manifest(doctored)

    doctored = _doctored()
    del doctored["claim_outcome_treatment"]["contradicted"]["weight"]
    with pytest.raises(ManifestError, match="weight"):
        validate_manifest(doctored)


def test_the_store_roster_is_validated_at_all() -> None:
    """`stores` was entirely unvalidated, and the generator seeds from it."""
    from fixtures.manifest import validate_manifest

    doctored = _doctored()
    doctored["stores"] = [s for s in doctored["stores"] if s["honest"] is False]
    with pytest.raises(ManifestError, match="no honest store"):
        validate_manifest(doctored)

    doctored = _doctored()
    doctored["stores"] = [
        s for s in doctored["stores"] if s["store_id"] != doctored["dishonest_store"]["store_id"]
    ]
    with pytest.raises(ManifestError, match="not on manifest.stores"):
        validate_manifest(doctored)

    doctored = _doctored()
    for store in doctored["stores"]:
        if store["store_id"] == doctored["dishonest_store"]["store_id"]:
            store["honest"] = True
    with pytest.raises(ManifestError, match="flagged honest"):
        validate_manifest(doctored)


def test_the_persona_scripts_are_validated_at_all() -> None:
    """`personas` was entirely unvalidated, and T-045 replays them verbatim."""
    from fixtures.manifest import UnmappedClaimTypeError, validate_manifest

    doctored = _doctored()
    for claim in doctored["personas"]["aggressive"]["scripted_claims"]:
        claim["truthful"] = True
    with pytest.raises(ManifestError, match="asserts nothing false"):
        validate_manifest(doctored)

    doctored = _doctored()
    doctored["personas"]["aggressive"]["scripted_claims"][0]["claim_type"] = "vibes"
    with pytest.raises(UnmappedClaimTypeError):
        validate_manifest(doctored)

    doctored = _doctored()
    doctored["personas"]["honest"]["store_id"] = "store-that-does-not-exist"
    with pytest.raises(ManifestError, match="not on the roster"):
        validate_manifest(doctored)

    doctored = _doctored()
    del doctored["personas"]["honest"]
    with pytest.raises(ManifestError, match="entirely truthful"):
        validate_manifest(doctored)


def test_the_fixture_intent_is_validated_at_all() -> None:
    """`fixture_intent` was entirely unvalidated; every persona answers it."""
    from fixtures.manifest import validate_manifest

    doctored = _doctored()
    doctored["fixture_intent"]["category"] = "tea"
    with pytest.raises(ManifestError, match="seed_category"):
        validate_manifest(doctored)

    doctored = _doctored()
    for constraint in doctored["fixture_intent"]["constraints"]:
        constraint["hard"] = False
    with pytest.raises(ManifestError, match="HARD constraint"):
        validate_manifest(doctored)

    doctored = _doctored()
    doctored["fixture_intent"]["budget_cents"] = 0
    with pytest.raises(ManifestError, match="budget_cents"):
        validate_manifest(doctored)

    doctored = _doctored()
    doctored["fixture_intent"]["preferences"][0]["weight"] = 1.5
    with pytest.raises(ManifestError, match="weight"):
        validate_manifest(doctored)


def test_a_half_written_approval_is_refused() -> None:
    """An approval assembled field by field is how a fabricated one looks while it is being
    written. Neither half-state is a coherent decision, so the loader refuses both.

    This adds nothing to the human gate's authenticity — no offline check can — but it means
    an agent that wrote a name into a pending manifest breaks the loader for every consumer,
    not only the one test that reads the block.

    Every state below is built by REPLACING the approval block outright rather than by
    nudging one field of whatever the committed one happens to say. "Half-written" is a
    claim about a block relative to a known starting state, so nudging only produces one
    while the committed manifest is unapproved: the moment a human approved it,
    ``approver = "Grace Hopper"`` stopped being half a decision (it is a whole one,
    misattributed) and ``status = "approved"`` stopped changing anything at all. Both then
    validated, and this test went red while the guard it grades was working perfectly.
    """
    from fixtures.manifest import APPROVAL_DECISION_FIELDS, PENDING_APPROVAL, validate_manifest

    filled = {
        "approver": "Grace Hopper",
        "approved_at": "2026-09-03T00:00:00Z",
        "artifact": "fixtures/approval/manifest-approval.md",
    }

    def staged(**approval) -> dict:
        """The committed manifest carrying exactly the approval block described."""
        doctored = _doctored()
        doctored["approval"] = {
            "status": PENDING_APPROVAL,
            **dict.fromkeys(APPROVAL_DECISION_FIELDS),
            "content_hash": doctored["approval"]["content_hash"],
            "request": doctored["approval"]["request"],
            **approval,
        }
        return doctored

    # The two coherent states, first: a test that refused every input would refuse these too.
    validate_manifest(staged())
    validate_manifest(staged(status="approved", **filled))

    # Pending, with any ONE decision field filled: an approval caught mid-fabrication.
    for field, value in filled.items():
        with pytest.raises(ManifestError, match="half-written approval"):
            validate_manifest(staged(**{field: value}))

    # Approved, with any ONE decision field missing or blank: a decision with a hole in it.
    for field in APPROVAL_DECISION_FIELDS:
        for hole in (None, "   "):
            with pytest.raises(ManifestError, match=field):
                validate_manifest(staged(status="approved", **{**filled, field: hole}))

    # ...and a status nobody knows how to grade is refused however complete the rest is.
    with pytest.raises(ManifestError, match="status"):
        validate_manifest(staged(status="looks_fine_to_me", **filled))
