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
is a separate, explicitly-named operation that a human runs deliberately::

    ./.venv/bin/python -m fixtures.manifest --refresh-digests          # re-pin
    ./.venv/bin/python -m fixtures.approval --emit-request --write     # re-issue the request

after which the request must be read again — ``git diff`` on it included — before anyone
approves. :func:`reissue_request` rewrites only the fenced block of digests, so re-issuing
cannot quietly rewrite the prose describing what the approval means.

The request is addressed by :data:`REQUEST_REL`, never by a path read out of the manifest.
Letting the audited document nominate the document it is audited against is the same
circularity A3 forbids: point ``approval.request`` at a file you wrote and every check passes
by construction.

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
    "covered_documents",
    "digest_report",
    "format_drift",
    "record_approval",
    "reissue_request",
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

#: One ```-fenced block. :func:`reissue_request` rewrites the *one* such block that carries
#: pins and nothing else in the document, so re-issuing can never touch the prose describing
#: what is being approved — only the machine-computed digests.
_FENCED = re.compile(r"^```[^\n]*\n(?P<body>.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)


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

    A request that pins the **same document at two different digests is refused**, not
    resolved. That is not a hypothetical input: the sanctioned recovery from drift is
    "re-pin, then re-issue this request", and a human doing that by hand pastes the new pin
    block in and leaves the old one above it. Taking whichever pin came first would then
    verify against a digest the reader did not read — the reader looks at the bottom block,
    the checker at the top one — which is the entire failure this gate exists to prevent.
    Repeating the *same* digest for a document is harmless and stays legal.
    """
    pins: dict[str, str] = {}
    conflicts: dict[str, set[str]] = {}
    for match in _PIN.finditer(text.lower()):
        path, digest = match.group("path"), match.group("digest")
        seen = pins.setdefault(path, digest)
        if seen != digest:
            conflicts.setdefault(path, {seen}).add(digest)
    if conflicts:
        detail = "; ".join(
            f"{path} pinned at {sorted(digests)}" for path, digests in sorted(conflicts.items())
        )
        raise ApprovalRefused(
            "the approval request pins the same document at more than one digest, so there is "
            "no single set of bytes it asks anyone to approve: "
            + detail
            + ". Re-issue it with one pin per document "
            "(./.venv/bin/python -m fixtures.approval --emit-request --write)."
        )
    return pins


def _digest(path: pathlib.Path) -> str:
    """:func:`fixtures.manifest.file_digest`, but a missing/unreadable covered document is an
    :class:`ApprovalRefused` rather than an ``OSError`` traceback.

    The CLI reports ``REFUSED: <reason>`` by catching :class:`ApprovalRefused`; a document
    that vanished has to arrive there as a refusal like any other drift, not as a stack trace
    that reads like the tool is broken.
    """
    try:
        return file_digest(path)
    except OSError as exc:
        raise ApprovalRefused(
            f"cannot read the covered document {path}: {exc}. A document the approval covers "
            "cannot be approved while it is missing or unreadable."
        ) from exc


def _covered_rel_paths(manifest: Mapping[str, Any]) -> list[str]:
    """The repo-relative paths of every document besides the manifest that it covers."""
    golden_ref = manifest.get("golden_set")
    if not isinstance(golden_ref, Mapping) or not isinstance(golden_ref.get("path"), str):
        raise ApprovalRefused(
            "manifest.golden_set must reference the golden set by {path, sha256, count}"
        )
    rels = [golden_ref["path"]]
    catalog_ref = manifest.get("seed_catalog")
    if isinstance(catalog_ref, Mapping) and isinstance(catalog_ref.get("path"), str):
        rels.append(catalog_ref["path"])
    return rels


def covered_documents(root: pathlib.Path | str | None = None) -> dict[str, str]:
    """``{repo-relative path: sha256}`` for every document this approval covers, computed
    from the bytes on disk right now.

    This is the single definition of "what the request must publish". :func:`reissue_request`
    writes exactly these pins into the request and :func:`digest_report` checks the request
    against exactly these values, so the emitter and the checker cannot drift apart into two
    similar-looking rules.
    """
    base = pathlib.Path(root) if root is not None else REPO_ROOT
    manifest = _read_json(base / MANIFEST_REL)
    if not isinstance(manifest, Mapping):
        raise ApprovalRefused(f"{MANIFEST_REL} must be a JSON object")
    covered = {MANIFEST_REL: body_digest(manifest)}
    for rel in _covered_rel_paths(manifest):
        covered[rel] = _digest(base / rel)
    return covered


def reissue_request(text: str, covered: Mapping[str, str]) -> str:
    """``text`` with its pinned-digest block replaced by ``covered``; everything else kept.

    Only the one fenced block that already carries pins is touched, so re-issuing can never
    rewrite the prose stating *what* is being approved — a machine may recompute the digests,
    but the description of the decision stays under the authorship of whoever wrote it.

    Refuses a document with no pin block, and one with two (which of them is the pin block is
    not a question a tool gets to guess at).
    """
    blocks = [m for m in _FENCED.finditer(text) if _PIN.search(m.group("body").lower())]
    if len(blocks) != 1:
        raise ApprovalRefused(
            f"the approval request must carry exactly ONE ```-fenced block of "
            f"'<repo-relative path> <sha256>' pins; found {len(blocks)}. Re-issuing rewrites "
            "that block and nothing else, so it cannot proceed without knowing which block it is."
        )
    block = blocks[0]
    width = max((len(path) for path in covered), default=0) + 2
    body = "".join(f"{path.ljust(width)}{digest}\n" for path, digest in covered.items())
    return text[: block.start("body")] + body + text[block.end("body") :]


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

    covered = covered_documents(base)

    # 1 — the manifest's own references against the bytes they name.
    golden_ref = manifest["golden_set"]
    golden_rel = golden_ref["path"]
    add(golden_rel, "manifest.golden_set.sha256", golden_ref.get("sha256"), covered[golden_rel])
    pitches = _read_json(base / golden_rel).get("pitches") or []
    add(golden_rel, "manifest.golden_set.count", golden_ref.get("count"), len(pitches))

    catalog_ref = manifest.get("seed_catalog")
    if isinstance(catalog_ref, Mapping) and isinstance(catalog_ref.get("path"), str):
        catalog_rel = catalog_ref["path"]
        add(
            catalog_rel,
            "manifest.seed_catalog.sha256",
            catalog_ref.get("sha256"),
            covered[catalog_rel],
        )

    # 2 — the manifest body against the digest published for it.
    approval = manifest.get("approval")
    approval = approval if isinstance(approval, Mapping) else {}
    add(
        f"{MANIFEST_REL} (body, `approval` excluded)",
        "manifest.approval.content_hash",
        str(approval.get("content_hash") or "").strip().lower(),
        covered[MANIFEST_REL],
    )

    # 3 — everything above against what the request published to the human. This is the
    #     check the whole gate rests on: the request is the document the approver read.
    #
    #     The request is addressed by the module constant, NEVER by a path read out of the
    #     manifest. Letting the document under audit nominate the document it is audited
    #     against is the same circularity SPEC A3 forbids: point `approval.request` at a file
    #     you wrote yourself and every check below passes by construction. A manifest that
    #     names some other request is therefore refused outright rather than quietly ignored.
    declared = approval.get("request")
    if declared is not None and str(declared) != REQUEST_REL:
        raise ApprovalRefused(
            f"manifest.approval.request names {str(declared)!r}, but the approval is always "
            f"verified against the committed request {REQUEST_REL!r}. A manifest that chooses "
            "which document publishes the digests it is checked against is checking itself."
        )
    pins = request_pins(_read_text(base / REQUEST_REL))
    for rel, actual in covered.items():
        add(rel, f"{REQUEST_REL} pins", pins.get(rel.lower(), "<not published>"), actual)

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
        "change is intended — re-pin, re-issue, and read it again:\n"
        "      ./.venv/bin/python -m fixtures.manifest --refresh-digests\n"
        "      ./.venv/bin/python -m fixtures.approval --emit-request --write\n"
        "  then read the re-issued request (and `git diff` on it) and approve THAT."
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
