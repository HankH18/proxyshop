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
