"""``python -m fixtures.approval`` — the one command a HUMAN runs to approve the manifest.

    PROXYSHOP_WORKER=1 ./.venv/bin/python -m fixtures.approval --approver "Your Name"

No agent may run this (EXECUTION.md rule 6). Nothing else in the repo calls it: it is not
wired into any test, gate, Makefile target or build step, so it can only be reached by a
person typing it.

``--show`` prints what would be approved, verifies the pinned digests, and writes nothing.

Approving VERIFIES the digests the approval request published and REFUSES on any drift. It
never refreshes them: re-hashing on the way in would bless a document nobody read. Re-pinning
is a separate command, ``python -m fixtures.manifest --refresh-digests``.

``--emit-request`` prints the request re-issued against the documents currently on disk, and
``--emit-request --write`` saves it. It rewrites *only* the fenced block of pins, so the prose
saying what approving means stays as its author wrote it. It exists because the sanctioned
recovery from drift ends in "…then re-issue the request", and a human re-issuing 64-hex
digests by hand mistypes one, or pastes the new block above the old and leaves both.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from fixtures.approval import (
    REQUEST_PATH,
    REQUEST_REL,
    ApprovalRefused,
    check_approver,
    covered_documents,
    digest_report,
    record_approval,
    reissue_request,
    verify_pinned_digests,
)
from fixtures.manifest import ManifestError, body_digest, load_manifest


def _summary() -> str:
    # check_digests=False on purpose: a drifted document must reach the explicit REFUSED
    # below with its diff, not die here in a loader exception.
    manifest = load_manifest(check_digests=False)
    store = manifest["dishonest_store"]
    trajectory = manifest["expected_trust_trajectory"]
    golden = manifest["golden_set"]
    return "\n".join(
        [
            "About to approve fixtures/manifest.json as ground truth:",
            f"  seed_category / seed   {manifest['seed_category']} / {manifest['seed']}",
            f"  dishonest store        {store['store_id']} "
            f"({len(store['behaviours'])} scripted behaviours)",
            f"  dimensions exercised   {sorted({b['dim'] for b in store['behaviours']})}",
            f"  episode budget         {manifest['episode_budget']}",
            f"  new-store prior N      {manifest['new_store_prior_n']}",
            f"  blacklist threshold    {manifest['blacklist_threshold']}",
            f"  trajectory ends        episode {trajectory[-1]['episode']} at "
            f"{trajectory[-1]['score']} +/- {trajectory[-1]['tolerance']}",
            f"  claim-type table       {len(manifest['claim_type_dimensions'])} types mapped",
            f"  golden set             {golden['count']} pitches, sha256 {golden['sha256']}",
            f"  body digest            {body_digest(manifest)}",
        ]
    )


def _report() -> str:
    rows = digest_report()
    lines = ["", "Pinned-vs-on-disk check (what the approval request published):"]
    for row in rows:
        mark = "ok  " if row["ok"] == "yes" else "DRIFT"
        lines.append(f"  {mark} {row['subject']}  [{row['field']}]")
        if row["ok"] != "yes":
            lines.append(f"        pinned  {row['pinned']}")
            lines.append(f"        on disk {row['actual']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m fixtures.approval",
        description="Record a HUMAN's approval of fixtures/manifest.json (T-080 acc 1).",
    )
    parser.add_argument("--approver", default=None, help="The approving human's real name.")
    parser.add_argument(
        "--show", action="store_true", help="Print what would be approved and exit."
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the interactive confirmation prompt."
    )
    parser.add_argument(
        "--emit-request",
        action="store_true",
        help="Print the approval request re-issued against the documents on disk. Approves "
        "nothing and, without --write, writes nothing.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"With --emit-request, save the re-issued request to {REQUEST_REL}.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.emit_request:
        try:
            reissued = reissue_request(
                REQUEST_PATH.read_text(encoding="utf-8"), covered_documents()
            )
        except ApprovalRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        if not args.write:
            print(reissued, end="")
            print(
                f"\n(nothing written — re-run with --write to save this to {REQUEST_REL})",
                file=sys.stderr,
            )
            return 0
        REQUEST_PATH.write_text(reissued, encoding="utf-8")
        print(
            f"Re-issued {REQUEST_REL} against the documents on disk. This is NOT an approval "
            "and it is not a review:\nthe digests changed because the documents did, so read "
            "the request again — and read `git diff` on what it now pins —\nbefore anyone runs "
            "--approver."
        )
        return 0

    try:
        print(_summary())
    except ManifestError as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 3

    # Verify BEFORE anything else is offered to the human: if the documents are not the ones
    # the request pinned, there is nothing here worth confirming.
    try:
        print(_report())
        verify_pinned_digests()
    except ApprovalRefused as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 3

    if args.show:
        return 0
    if not args.approver:
        print(
            '\nNothing written. Re-run with --approver "Your Name" to record the approval.',
            file=sys.stderr,
        )
        return 2
    try:
        name = check_approver(args.approver)
    except ApprovalRefused as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 3

    if not args.yes:
        print(
            f'\nRecording this as approved by "{name}" writes fixtures/manifest.json and '
            "fixtures/approval/manifest-approval.md."
        )
        answer = input("Type the word approve to continue: ").strip().lower()
        if answer != "approve":
            print("Aborted; nothing written.", file=sys.stderr)
            return 4

    try:
        result = record_approval(name)
    except ApprovalRefused as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 3
    print(
        f"\nApproved by {result['approver']} at {result['approved_at']}\n"
        f"  content_hash {result['content_hash']}\n"
        "  wrote fixtures/manifest.json and fixtures/approval/manifest-approval.md\n"
        "Commit both."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
