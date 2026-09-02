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

Approving VERIFIES; it never re-seals
-------------------------------------
:func:`record_approval` recomputes every digest and compares it against what the approval
request published — the digests the human actually read — and **refuses with a diff** if
they disagree. It does not call :func:`fixtures.manifest.refresh_digests`.

That is the whole point of the artifact. An approval command that refreshed the digests
first would silently re-hash whatever happened to be on disk at the moment it ran, so a
golden set edited between "here is what I am asking you to approve" and "approved" would be
blessed rather than caught, and the recorded digest would prove only that the file had not
changed *since the approval command read it* — which is nothing. Re-pinning drifted digests
is a separate, explicitly-named operation (``python -m fixtures.manifest --refresh-digests``)
that a human runs deliberately, after which the request must be re-issued and re-read.

What it does NOT mean: it is not a claim that the numbers are optimal, and it is not
irreversible. Re-running it after a re-pinned, re-issued request re-approves the new document.
"""

from __future__ import annotations

__all__ = [
    "APPROVAL_RECORD_PATH",
    "APPROVAL_RECORD_REL",
    "REQUEST_PATH",
    "REQUEST_REL",
    "ApprovalRefused",
    "check_approver",
    "digest_report",
    "format_drift",
    "record_approval",
    "render_record",
    "request_pins",
    "verify_pinned_digests",
]

import json
import pathlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fixtures import REPO_ROOT
from fixtures.manifest import (
    MANIFEST_REL,
    ManifestError,
    body_digest,
    file_digest,
    golden_claim_types,
    validate_claim_types,
    validate_manifest,
)

APPROVAL_RECORD_REL = "fixtures/approval/manifest-approval.md"
APPROVAL_RECORD_PATH = REPO_ROOT / APPROVAL_RECORD_REL
REQUEST_REL = "fixtures/approval/REQUEST-manifest-approval.md"
REQUEST_PATH = REPO_ROOT / REQUEST_REL

#: Names that are not an approver. The acceptance suite rejects the same set.
_PLACEHOLDERS = {"tbd", "todo", "none", "n/a", "unknown", "null", "me", "human"}

#: Anything that names automation. A3 requires a HUMAN approval, and an agent that typed its
#: own name here would be self-approving the document it is graded against.
_AUTOMATION = re.compile(r"(claude|gpt|llm|\bagent\b|\bbot\b|swarm|automat|\bai\b|\bsystem\b)")

#: One ``<repo-relative path> <sha256>`` pin, as the approval request publishes it. The
#: request is prose a human reads, so the pins are parsed out of it rather than kept in a
#: second machine file nobody would look at.
_PIN = re.compile(r"(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*\.json)\s+(?P<digest>[0-9a-f]{64})")


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


# --- what the human was asked to approve ---------------------------------------------
def _read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ApprovalRefused(f"missing ground-truth document {path}") from exc
    except json.JSONDecodeError as exc:
        raise ApprovalRefused(f"{path} is not readable JSON: {exc}") from exc


def _read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ApprovalRefused(
            f"missing approval request {path} — there is nothing recording what the human "
            "was asked to approve, so there is nothing to verify the documents against"
        ) from exc


def request_pins(text: str) -> dict[str, str]:
    """The ``{document path: sha256}`` pins the approval request published, in reading order.

    These are the digests a human actually saw before typing the approve command. They are
    the *expectation*; the bytes on disk are the thing being checked against it.
    """
    pins: dict[str, str] = {}
    for match in _PIN.finditer(text.lower()):
        pins.setdefault(match.group("path"), match.group("digest"))
    return pins


def digest_report(root: pathlib.Path | str | None = None) -> list[dict[str, str]]:
    """Every digest the approval covers: what was pinned, what is on disk, and whether they
    agree. Pure inspection — reads only, writes nothing, refreshes nothing.

    Each row is ``{subject, field, pinned, actual, ok}``. ``ok`` is ``"yes"``/``"no"`` so the
    row prints as-is; callers test ``row["pinned"] == row["actual"]``.
    """
    base = pathlib.Path(root) if root is not None else REPO_ROOT
    manifest = _read_json(base / MANIFEST_REL)
    if not isinstance(manifest, Mapping):
        raise ApprovalRefused(f"{MANIFEST_REL} must be a JSON object")

    rows: list[dict[str, str]] = []

    def add(subject: str, field: str, pinned: object, actual: object) -> None:
        rows.append(
            {
                "subject": subject,
                "field": field,
                "pinned": str(pinned),
                "actual": str(actual),
                "ok": "yes" if str(pinned) == str(actual) else "no",
            }
        )

    # 1 — the manifest's own references against the bytes they name.
    golden_ref = manifest.get("golden_set")
    if not isinstance(golden_ref, Mapping) or not isinstance(golden_ref.get("path"), str):
        raise ApprovalRefused(
            "manifest.golden_set must reference the golden set by {path, sha256, count}"
        )
    golden_rel = golden_ref["path"]
    golden_path = base / golden_rel
    add(golden_rel, "manifest.golden_set.sha256", golden_ref.get("sha256"), file_digest(golden_path))
    pitches = _read_json(golden_path).get("pitches") or []
    add(golden_rel, "manifest.golden_set.count", golden_ref.get("count"), len(pitches))

    catalog_ref = manifest.get("seed_catalog")
    catalog_rel = None
    if isinstance(catalog_ref, Mapping) and isinstance(catalog_ref.get("path"), str):
        catalog_rel = catalog_ref["path"]
        add(
            catalog_rel,
            "manifest.seed_catalog.sha256",
            catalog_ref.get("sha256"),
            file_digest(base / catalog_rel),
        )

    # 2 — the manifest body against the digest published for it.
    body = body_digest(manifest)
    approval = manifest.get("approval")
    approval = approval if isinstance(approval, Mapping) else {}
    add(
        f"{MANIFEST_REL} (body, `approval` excluded)",
        "manifest.approval.content_hash",
        str(approval.get("content_hash") or "").strip().lower(),
        body,
    )

    # 3 — everything above against what the request published to the human. This is the
    #     check the whole gate rests on: the request is the document the approver read.
    request_rel = str(approval.get("request") or REQUEST_REL)
    pins = request_pins(_read_text(base / request_rel))
    wanted = [(MANIFEST_REL, body), (golden_rel, file_digest(golden_path))]
    if catalog_rel is not None:
        wanted.append((catalog_rel, file_digest(base / catalog_rel)))
    for rel, actual in wanted:
        add(rel, f"{request_rel} pins", pins.get(rel.lower(), "<not published>"), actual)

    return rows


def format_drift(rows: list[dict[str, str]]) -> str:
    """A readable diff of the rows that disagree."""
    lines = []
    for row in rows:
        if row["ok"] == "yes":
            continue
        lines.append(f"  {row['subject']}")
        lines.append(f"      {row['field']}")
        lines.append(f"      pinned  {row['pinned']}")
        lines.append(f"      on disk {row['actual']}")
    return "\n".join(lines)


def verify_pinned_digests(root: pathlib.Path | str | None = None) -> list[dict[str, str]]:
    """Refuse unless every document is byte-identical to what the request pinned.

    Returns the report on success. Raises :class:`ApprovalRefused` naming each drifted
    document on failure. **Never rewrites anything** — a verifier that repaired what it
    found would be recording its own output rather than the human's decision.
    """
    rows = digest_report(root)
    drifted = [row for row in rows if row["ok"] == "no"]
    if not drifted:
        return rows
    names = sorted({row["subject"] for row in drifted})
    raise ApprovalRefused(
        "the documents on disk are NOT the documents this approval request pinned, so "
        "approving them would bless a change nobody read. Drifted: "
        + ", ".join(names)
        + "\n"
        + format_drift(rows)
        + "\n  Nothing was written. Either restore the drifted document(s), or — if the "
        "change is intended — re-pin and re-issue the request:\n"
        "      ./.venv/bin/python -m fixtures.manifest --refresh-digests\n"
        "  then update the request's pinned-digest block, read it, and approve that."
    )


def render_record(manifest: Mapping[str, Any], approver: str, approved_at: str, digest: str) -> str:
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
        "Every digest above was VERIFIED against the approval request before this record was",
        "written; the approval command refuses rather than re-hashing a document that drifted.",
        "",
        "## What it does not claim",
        "",
        "It does not claim the numbers are optimal, and it is not irreversible: editing any",
        "covered document breaks this digest, and the acceptance suite fails until the",
        "manifest is re-approved. Re-approve; never re-hash.",
        "",
    ]
    return "\n".join(lines)


def record_approval(
    approver: str,
    *,
    approved_at: str | None = None,
    root: pathlib.Path | str | None = None,
) -> dict[str, str]:
    """Record a human's approval of the manifest **as it was pinned**. Humans only.

    Verifies first and refuses on any drift: the digests recorded in the manifest and
    published in the approval request must match the bytes on disk. It deliberately does
    NOT refresh them — an approval that re-hashed the documents would silently re-seal
    whatever was on disk at that instant and its digest would evidence nothing.

    ``root`` exists so the whole path can be exercised against a copy of the tree; it
    defaults to the repository root and every real run leaves it alone.
    """
    name = check_approver(approver)
    base = pathlib.Path(root) if root is not None else REPO_ROOT

    # Refuse to approve documents that are not the ones the request pinned...
    verify_pinned_digests(base)

    # ...and refuse to approve a manifest that is not usable ground truth at all.
    manifest = _read_json(base / MANIFEST_REL)
    try:
        validate_manifest(manifest)
        golden = _read_json(base / manifest["golden_set"]["path"])
        validate_claim_types(manifest, golden_claim_types(golden))
    except ManifestError as exc:
        raise ApprovalRefused(f"the manifest is not usable ground truth: {exc}") from exc

    when = approved_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = body_digest(manifest)

    record_path = base / APPROVAL_RECORD_REL
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(render_record(manifest, name, when, digest), encoding="utf-8")

    manifest["approval"] = {
        "status": "approved",
        "approver": name,
        "approved_at": when,
        "artifact": APPROVAL_RECORD_REL,
        "content_hash": digest,
        "request": REQUEST_REL,
    }
    (base / MANIFEST_REL).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"approver": name, "approved_at": when, "content_hash": digest}
