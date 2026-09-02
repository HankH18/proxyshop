"""``python -m fixtures.approval`` — the one command a HUMAN runs to approve the manifest.

    PROXYSHOP_WORKER=1 ./.venv/bin/python -m fixtures.approval --approver "Your Name"

No agent may run this (EXECUTION.md rule 6). Nothing else in the repo calls it: it is not
wired into any test, gate, Makefile target or build step, so it can only be reached by a
person typing it.

``--show`` prints what would be approved and writes nothing.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from fixtures.approval import ApprovalRefused, check_approver, record_approval
from fixtures.manifest import body_digest, load_manifest


def _summary() -> str:
    manifest = load_manifest()
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
    args = parser.parse_args(list(argv) if argv is not None else None)

    print(_summary())
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
