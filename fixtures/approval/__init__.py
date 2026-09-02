"""The human gate on the fixture manifest (T-080 acceptance 1, SPEC A3).

EXECUTION.md rule 6: *"T-080's manifest approval is a human gate — do not fabricate the
approval artifact."* No agent may run :func:`record_approval`, and nothing in this package
is called by any test, gate or build step. It exists so that the human's action is one
command instead of hand-editing two files and computing a sha256 by hand.

What approving means, exactly
-----------------------------
Recording an approval writes two things that must agree:

1. ``fixtures/manifest.json`` → the ``approval`` block: who approved, when, the path of the
   approval record, and the sha256 of the manifest body (the document minus that block).
2. ``fixtures/approval/manifest-approval.md`` → a committed record quoting the same digest
   and the same approver name.

Because ``golden_set.sha256`` and ``seed_catalog.sha256`` sit *inside* the hashed body,
approving the manifest transitively approves the exact bytes of the golden set and the
category config. Any later edit to any of the three breaks the digest and the acceptance
suite says so — which is the tamper evidence, not proof that a human signed. No offline
check can distinguish a human's signature from an agent typing the human's name; that is
what the procedural gate is for.

What it does NOT mean: it is not a claim that the numbers are optimal, and it is not
irreversible. Re-running it after an edit re-approves the new document.
"""

from __future__ import annotations

__all__ = [
    "APPROVAL_RECORD_PATH",
    "APPROVAL_RECORD_REL",
    "REQUEST_PATH",
    "ApprovalRefused",
    "check_approver",
    "record_approval",
    "render_record",
]

import json
import re
from datetime import UTC, datetime
from typing import Any

from fixtures import REPO_ROOT
from fixtures.manifest import (
    MANIFEST_PATH,
    body_digest,
    load_manifest,
    refresh_digests,
)

APPROVAL_RECORD_REL = "fixtures/approval/manifest-approval.md"
APPROVAL_RECORD_PATH = REPO_ROOT / APPROVAL_RECORD_REL
REQUEST_PATH = REPO_ROOT / "fixtures/approval/REQUEST-manifest-approval.md"

#: Names that are not an approver. The acceptance suite rejects the same set.
_PLACEHOLDERS = {"tbd", "todo", "none", "n/a", "unknown", "null", "me", "human"}

#: Anything that names automation. A3 requires a HUMAN approval, and an agent that typed its
#: own name here would be self-approving the document it is graded against.
_AUTOMATION = re.compile(r"(claude|gpt|llm|\bagent\b|\bbot\b|swarm|automat|\bai\b|\bsystem\b)")


class ApprovalRefused(Exception):
    """The approval was refused. Nothing was written."""


def check_approver(approver: str) -> str:
    """Return the approver's name, or refuse it.

    Refuses an empty name, a placeholder, and any name that reads as automation. The last
    check is the one with teeth: it is what stops an agent from honestly self-attributing.
    """
    if not isinstance(approver, str) or not approver.strip():
        raise ApprovalRefused("--approver must name the human who approved the manifest")
    name = approver.strip()
    if name.lower() in _PLACEHOLDERS:
        raise ApprovalRefused(
            f"{name!r} is a placeholder, not an approver. Use the human's real name."
        )
    if _AUTOMATION.search(name.lower()):
        raise ApprovalRefused(
            f"{name!r} names automation. SPEC A3 requires a HUMAN approval and EXECUTION.md "
            "rule 6 forbids any agent from fabricating this artifact."
        )
    if len(name) < 3:
        raise ApprovalRefused(f"{name!r} is too short to identify the approver")
    return name


def render_record(manifest: dict[str, Any], approver: str, approved_at: str, digest: str) -> str:
    """The committed approval record. It quotes the digest and names the approver."""
    store = manifest["dishonest_store"]
    behaviours = store["behaviours"]
    trajectory = manifest["expected_trust_trajectory"]
    golden = manifest["golden_set"]
    lines = [
        "# Fixture manifest — recorded human approval",
        "",
        f"- **Approver:** {approver}",
        f"- **Approved at:** {approved_at}",
        "- **Document:** `fixtures/manifest.json`",
        f"- **Content digest (sha256 of the manifest body, `approval` excluded):** `{digest}`",
        f"- **Golden set covered:** `{golden['path']}` sha256 `{golden['sha256']}`, "
        f"{golden['count']} pitches",
        "",
        "## What this approval covers",
        "",
        'SPEC A3: "the trust engine catches the dishonest store" is circular unless the',
        "dishonest behaviours are defined outside the trust engine and approved by a human.",
        "This record is that approval. It covers, as ground truth:",
        "",
        f"1. **The dishonest store** `{store['store_id']}` and its "
        f"{len(behaviours)} scripted behaviours:",
    ]
    for behaviour in behaviours:
        lines.append(
            f"   - `{behaviour['kind']}` → dimension `{behaviour['dim']}`, "
            f"observation `{behaviour['type']}`"
        )
    lines += [
        f"2. **The episode budget** ({manifest['episode_budget']}) and the "
        f"**new-store prior N** ({manifest['new_store_prior_n']}).",
        f"3. **The blacklist threshold** ({manifest['blacklist_threshold']}) and the "
        f"**expected trust trajectory**, ending at episode {trajectory[-1]['episode']} "
        f"with score {trajectory[-1]['score']} ± {trajectory[-1]['tolerance']}.",
        "4. **The `claim_type → dimension` table** — typed and exhaustive over the published",
        "   claim-type vocabulary; product-fact types route to `catalog_claim_accuracy` and",
        "   an unmapped claim type raises rather than defaulting (D53).",
        "5. **The golden pitch set**, by digest — its labels are the answer key",
        "   `packages/verification` is graded against and never authors (D11).",
        "6. **The persona scripts** and the fixture intent.",
        "",
        "## What it does not claim",
        "",
        "It does not claim the numbers are optimal, and it is not irreversible: editing any",
        "covered document breaks this digest, and the acceptance suite fails until the",
        "manifest is re-approved. Re-approve; never re-hash.",
        "",
    ]
    return "\n".join(lines)


def record_approval(approver: str, *, approved_at: str | None = None) -> dict[str, str]:
    """Record a human's approval of the current manifest. **Humans only.**

    Refreshes the recorded digests first, so the human approves the documents as they
    actually are on disk rather than as they were when someone last ran a script.
    """
    name = check_approver(approver)

    # Refuse to let anyone approve a manifest that is not usable ground truth.
    refresh_digests()
    manifest = load_manifest()

    when = approved_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = body_digest(manifest)

    APPROVAL_RECORD_PATH.write_text(render_record(manifest, name, when, digest), encoding="utf-8")

    manifest["approval"] = {
        "status": "approved",
        "approver": name,
        "approved_at": when,
        "artifact": APPROVAL_RECORD_REL,
        "content_hash": digest,
        "request": "fixtures/approval/REQUEST-manifest-approval.md",
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"approver": name, "approved_at": when, "content_hash": digest}
