"""``python -m fixtures.manifest`` — inspect, or explicitly re-pin, the manifest's digests.

    ./.venv/bin/python -m fixtures.manifest                     # read-only report
    ./.venv/bin/python -m fixtures.manifest --refresh-digests   # re-pin (writes)

This is **bookkeeping, not approval**, and it is deliberately a separate command from
``python -m fixtures.approval``. Re-pinning records what the documents currently say; it
asserts nothing about whether anyone read them. The approval command therefore verifies the
pinned digests and refuses on drift rather than refreshing them on the way in — an approval
that re-hashed first would silently bless a golden set edited after the request was written,
and its recorded digest would then evidence nothing at all.

So the honest sequence, when ground truth really did change, is::

    ./.venv/bin/python -m fixtures.manifest --refresh-digests          # re-pin
    ./.venv/bin/python -m fixtures.approval --emit-request --write     # re-issue the request
    # the human reads the re-issued request, and `git diff` on it
    ./.venv/bin/python -m fixtures.approval --approver "Their Name"    # the human approves

Never refresh as a step *inside* approving.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from fixtures import REPO_ROOT
from fixtures.manifest import (
    MANIFEST_PATH,
    MANIFEST_REL,
    body_digest,
    file_digest,
    refresh_digests,
)


def _report() -> tuple[str, bool]:
    """The pinned-vs-on-disk table, and whether everything agrees."""
    import json

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows: list[tuple[str, str, str]] = []

    golden = manifest["golden_set"]
    golden_path = REPO_ROOT / golden["path"]
    rows.append((golden["path"], str(golden.get("sha256", "")), file_digest(golden_path)))
    rows.append(
        (
            f"{golden['path']} (pitch count)",
            str(golden.get("count")),
            str(len(json.loads(golden_path.read_text(encoding="utf-8"))["pitches"])),
        )
    )
    catalog = manifest.get("seed_catalog")
    if isinstance(catalog, dict) and catalog.get("path"):
        rows.append(
            (
                catalog["path"],
                str(catalog.get("sha256", "")),
                file_digest(REPO_ROOT / catalog["path"]),
            )
        )
    approval = manifest.get("approval") or {}
    rows.append(
        (
            f"{MANIFEST_REL} (body, `approval` excluded)",
            str(approval.get("content_hash") or ""),
            body_digest(manifest),
        )
    )

    lines = ["Pinned digests vs the bytes on disk:"]
    agree = True
    for subject, pinned, actual in rows:
        ok = pinned == actual
        agree = agree and ok
        lines.append(f"  {'ok   ' if ok else 'DRIFT'} {subject}")
        if not ok:
            lines.append(f"        pinned  {pinned}")
            lines.append(f"        on disk {actual}")
    return "\n".join(lines), agree


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m fixtures.manifest",
        description="Report, or explicitly re-pin, the fixture manifest's recorded digests. "
        "This approves nothing (see python -m fixtures.approval).",
    )
    parser.add_argument(
        "--refresh-digests",
        action="store_true",
        help="Rewrite the recorded digests to match the documents on disk. Writes.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report, agree = _report()
    print(report)
    if not args.refresh_digests:
        if agree:
            return 0
        print(
            "\nThe manifest does not describe the documents on disk. Either restore them, or "
            "re-run with --refresh-digests to re-pin them deliberately.",
            file=sys.stderr,
        )
        return 1

    if not args.yes:
        print(
            "\nRe-pinning records the CURRENT bytes as ground truth. Any approval recorded "
            "against the old pins is void, and\nfixtures/approval/REQUEST-manifest-approval.md "
            "must be re-issued with the new digests and read again before anyone approves."
        )
        answer = input("Type the word refresh to continue: ").strip().lower()
        if answer != "refresh":
            print("Aborted; nothing written.", file=sys.stderr)
            return 4

    result = refresh_digests()
    print(
        "\nRe-pinned fixtures/manifest.json\n"
        f"  golden_set.sha256   {result['golden_set_sha256']}\n"
        f"  seed_catalog.sha256 {result['seed_catalog_sha256']}\n"
        f"  content_hash        {result['content_hash']}\n"
        "This is NOT an approval, and any approval recorded against the old pins is now void.\n"
        "Next:  ./.venv/bin/python -m fixtures.approval --emit-request --write\n"
        "then a human reads the re-issued request and runs "
        '`python -m fixtures.approval --approver "Their Name"`.'
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
