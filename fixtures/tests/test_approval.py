"""T-080 — approving the manifest VERIFIES the pinned digests; it never re-seals them.

The defect these tests exist for: ``record_approval()`` used to call ``refresh_digests()``
before checking anything, so the one command a human is told to run silently recomputed
``golden_set.sha256`` and re-sealed whatever happened to be on disk at that instant. A golden
set edited between "here is what I am asking you to approve" and "approved" was blessed
rather than caught, and the recorded digest then proved nothing — it evidenced only that the
file had not changed since the approval command itself read it, milliseconds earlier.

Every test here runs against a **copy** of the fixture tree in ``tmp_path``. Nothing in this
file can write the repository's own manifest: an approval recorded by a test would be a
fabricated approval artifact, which EXECUTION.md rule 6 forbids outright.
"""

from __future__ import annotations

import json
import shutil

import pytest

from fixtures import FIXTURES_DIR, REPO_ROOT
from fixtures.approval import (
    APPROVAL_RECORD_REL,
    REQUEST_PATH,
    ApprovalRefused,
    digest_report,
    record_approval,
    request_pins,
    verify_pinned_digests,
)
from fixtures.manifest import MANIFEST_PATH, MANIFEST_REL, body_digest, file_digest

GOLDEN_REL = "fixtures/golden/golden_set.json"
CATALOG_REL = "fixtures/catalog/coffee.json"


def _sandbox(tmp_path):
    """A byte-identical copy of the approvable tree, rooted at a throwaway directory."""
    root = tmp_path / "repo"
    (root / "fixtures").mkdir(parents=True)
    shutil.copy2(MANIFEST_PATH, root / MANIFEST_REL)
    for sub in ("golden", "catalog", "approval"):
        shutil.copytree(
            FIXTURES_DIR / sub,
            root / "fixtures" / sub,
            ignore=shutil.ignore_patterns("__pycache__", "*.py"),
        )
    return root


def _read(root, rel):
    return json.loads((root / rel).read_text(encoding="utf-8"))


def _write(root, rel, doc):
    (root / rel).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _mutate_golden_label(root) -> str:
    """Tamper with the answer key the way that matters: change a label, keep valid JSON."""
    golden = _read(root, GOLDEN_REL)
    claim = golden["pitches"][0]["claims"][0]
    was = claim["expected_status"]
    claim["expected_status"] = "verified" if was != "verified" else "contradicted"
    _write(root, GOLDEN_REL, golden)
    return was


# ------------------------------------------------------------------------------------
# The reproduction: drift between the request and the approval must be REFUSED
# ------------------------------------------------------------------------------------
def test_record_approval_refuses_a_golden_set_mutated_after_the_request_was_issued(tmp_path):
    root = _sandbox(tmp_path)
    pinned = _read(root, MANIFEST_REL)["golden_set"]["sha256"]
    _mutate_golden_label(root)
    drifted = file_digest(root / GOLDEN_REL)
    assert drifted != pinned

    with pytest.raises(ApprovalRefused) as excinfo:
        record_approval("Ada Lovelace", root=root)

    message = str(excinfo.value)
    assert GOLDEN_REL in message, "the refusal must name the drifted file"
    assert pinned in message and drifted in message, "the refusal must show the diff"


def test_a_refused_approval_writes_nothing_and_re_seals_nothing(tmp_path):
    """The regression proper: refusing must leave the pinned digest ALONE.

    The old code refreshed first, so this same input left `golden_set.sha256` rewritten to
    the tampered file's digest and the manifest marked approved.
    """
    root = _sandbox(tmp_path)
    before_manifest = (root / MANIFEST_REL).read_text(encoding="utf-8")
    pinned = _read(root, MANIFEST_REL)["golden_set"]["sha256"]
    _mutate_golden_label(root)

    with pytest.raises(ApprovalRefused):
        record_approval("Ada Lovelace", root=root)

    assert (root / MANIFEST_REL).read_text(encoding="utf-8") == before_manifest
    after = _read(root, MANIFEST_REL)
    assert after["golden_set"]["sha256"] == pinned, (
        "approving re-sealed the tampered golden set — the digest now proves nothing"
    )
    assert after["approval"]["approver"] is None
    assert not (root / APPROVAL_RECORD_REL).exists()


def test_record_approval_refuses_a_manifest_body_edited_after_the_request_was_issued(tmp_path):
    """Editing the manifest itself drifts from both the published body digest and the request."""
    root = _sandbox(tmp_path)
    manifest = _read(root, MANIFEST_REL)
    manifest["blacklist_threshold"] = 0.34
    _write(root, MANIFEST_REL, manifest)

    with pytest.raises(ApprovalRefused, match=r"fixtures/manifest\.json"):
        record_approval("Ada Lovelace", root=root)
    assert _read(root, MANIFEST_REL)["approval"]["approver"] is None


def test_record_approval_refuses_when_the_request_pins_a_different_golden_set(tmp_path):
    """The manifest may be perfectly self-consistent and still not be what the human read.

    Nothing else catches this: re-pinning the manifest without re-issuing the request leaves
    every internal check green, and only the request's own published digest disagrees.
    """
    root = _sandbox(tmp_path)
    _mutate_golden_label(root)
    manifest = _read(root, MANIFEST_REL)
    manifest["golden_set"]["sha256"] = file_digest(root / GOLDEN_REL)
    manifest["approval"]["content_hash"] = body_digest(manifest)
    _write(root, MANIFEST_REL, manifest)

    # Self-consistent now: the manifest's own references all agree with the bytes.
    rows = {r["field"]: r for r in digest_report(root)}
    assert rows["manifest.golden_set.sha256"]["ok"] == "yes"
    assert rows["manifest.approval.content_hash"]["ok"] == "yes"

    with pytest.raises(ApprovalRefused) as excinfo:
        record_approval("Ada Lovelace", root=root)
    assert "REQUEST-manifest-approval.md pins" in str(excinfo.value)


# ------------------------------------------------------------------------------------
# ...and an untouched set still approves cleanly
# ------------------------------------------------------------------------------------
def test_an_unmutated_set_approves_cleanly(tmp_path):
    root = _sandbox(tmp_path)
    before = _read(root, MANIFEST_REL)

    result = record_approval("Ada Lovelace", root=root, approved_at="2026-09-02T00:00:00Z")

    after = _read(root, MANIFEST_REL)
    assert after["approval"]["status"] == "approved"
    assert after["approval"]["approver"] == "Ada Lovelace"
    assert after["approval"]["approved_at"] == "2026-09-02T00:00:00Z"
    assert after["approval"]["artifact"] == APPROVAL_RECORD_REL
    assert after["approval"]["content_hash"] == result["content_hash"] == body_digest(after)

    # Approving recorded the human's decision; it did not recompute the ground truth.
    assert after["golden_set"] == before["golden_set"]
    assert after["seed_catalog"] == before["seed_catalog"]
    assert result["content_hash"] == before["approval"]["content_hash"]

    record = (root / APPROVAL_RECORD_REL).read_text(encoding="utf-8")
    assert result["content_hash"] in record
    assert after["golden_set"]["sha256"] in record
    assert "Ada Lovelace" in record


def test_the_approval_tool_still_refuses_automation_before_it_looks_at_anything(tmp_path):
    root = _sandbox(tmp_path)
    with pytest.raises(ApprovalRefused, match="automation"):
        record_approval("the swarm agent", root=root)
    assert _read(root, MANIFEST_REL)["approval"]["approver"] is None


# ------------------------------------------------------------------------------------
# The committed request is the pin set, so it has to stay in step with the manifest
# ------------------------------------------------------------------------------------
def test_the_committed_request_pins_every_digest_the_approval_covers() -> None:
    pins = request_pins(REQUEST_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert pins.get(MANIFEST_REL) == body_digest(manifest)
    assert pins.get(GOLDEN_REL) == file_digest(REPO_ROOT / GOLDEN_REL)
    assert pins.get(CATALOG_REL) == file_digest(REPO_ROOT / CATALOG_REL)


def test_the_repository_tree_itself_verifies_against_its_request() -> None:
    """Whatever a human runs the command on must be exactly what this request published."""
    rows = verify_pinned_digests()
    assert rows and all(row["ok"] == "yes" for row in rows)
