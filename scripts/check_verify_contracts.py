#!/usr/bin/env python
"""Enforce the structural contracts that no linter can see.

Orchestrator-owned (T-000), frozen. Run from ``make check`` and ``make verify``.

Fatal checks
------------
1. **D35 — exactly one pytest configuration.** Only the root ``pyproject.toml`` may carry
   ``[tool.pytest.ini_options]``; no ``pytest.ini``, ``tox.ini [pytest]`` or
   ``setup.cfg [tool:pytest]`` may exist anywhere. A second configuration silently changes
   ``rootdir`` and with it ``pythonpath``, ``testpaths`` and the import mode.
2. **D36 — no "pixel" outside ``pixel/``.** The pixel ticket's verify is
   ``npx vitest run pixel``, which vitest treats as a *case-insensitive path substring*
   filter. Any ``*.test.*``/``*.spec.*`` file elsewhere whose path contains "pixel" would
   silently join that run.
3. **D1 — the old schema package name is gone.** The schema package is
   ``packages/contracts``; the superseded name must not appear in the source tree.

Non-fatal report
----------------
4. Ticket verify paths that do not exist yet. This is a scaffold sanity check, printed and
   never fatal: on a fresh scaffold nearly all of them are legitimately missing.

Note on check 4 vs. the original specification: the intake specified "any ticket whose
status is CLOSED has a verify path that does not exist -> FAIL". There is no ticket-status
store in this repo — ``tickets.json`` tickets carry no ``status`` field and
``.swarm-loop/state.json`` carries no per-ticket status — so that check has no data source.
It is implemented as a clean no-op that says so, and turns itself on automatically if a
status store ever appears (see :func:`_load_ticket_status`).
"""

from __future__ import annotations

import configparser
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Files that legitimately *discuss* the banned strings: the frozen harness, the plan
#: documents, and this checker. Everything else is the source tree.
EXCLUDED_PREFIXES = (".swarm-loop/",)
EXCLUDED_FILES = frozenset(
    {
        "SPEC.md",
        "DESIGN.md",
        "TASKS.md",
        "EXECUTION.md",
        "tickets.json",
        "scripts/check_verify_contracts.py",
        "scripts/verify.sh",
    }
)

#: D1's superseded package name, assembled so this file does not match its own check.
SUPERSEDED_SCHEMA_DIR = "packages/" + "protocol"

TEST_FILE_RE = re.compile(r"\.(test|spec)\.[^.]+$")


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line]


def source_files() -> list[str]:
    result = []
    for path in tracked_files():
        if path in EXCLUDED_FILES or path.startswith(EXCLUDED_PREFIXES):
            continue
        if not (ROOT / path).is_file():  # symlinks into .pkgroot, submodules
            continue
        result.append(path)
    return result


# ---------------------------------------------------------------------------- check 1


def check_single_pytest_config(failures: list[str]) -> None:
    for path in source_files():
        if path.endswith("pyproject.toml") and path != "pyproject.toml":
            if "[tool.pytest.ini_options]" in (ROOT / path).read_text():
                failures.append(
                    f"D35: {path} contains [tool.pytest.ini_options]; the root "
                    f"pyproject.toml is the only pytest configuration in this repo."
                )
        if Path(path).name == "pytest.ini":
            failures.append(
                f"D35: {path} exists; pytest configuration lives only in the root manifest."
            )
        if Path(path).name in {"tox.ini", "setup.cfg"}:
            parser = configparser.ConfigParser()
            try:
                parser.read(ROOT / path)
            except configparser.Error:
                continue
            section = "pytest" if Path(path).name == "tox.ini" else "tool:pytest"
            if parser.has_section(section):
                failures.append(f"D35: {path} carries a [{section}] section.")


# ---------------------------------------------------------------------------- check 2


def check_pixel_path_filter(failures: list[str]) -> None:
    needle = "pixel"
    for path in source_files():
        if not TEST_FILE_RE.search(Path(path).name) and not Path(path).name.startswith("test_"):
            continue
        if path.startswith(f"{needle}/"):
            continue
        if needle in path.lower():
            failures.append(
                f"D36: test file {path} has '{needle}' in its path but lives outside "
                f"{needle}/. `npx vitest run {needle}` filters by case-insensitive path "
                f"substring, so this file would silently join that ticket's run."
            )


# ---------------------------------------------------------------------------- check 3


def check_superseded_schema_dir(failures: list[str]) -> None:
    for path in source_files():
        try:
            text = (ROOT / path).read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        if SUPERSEDED_SCHEMA_DIR in text:
            failures.append(
                f"D1: {path} references '{SUPERSEDED_SCHEMA_DIR}'. The schema package is "
                f"packages/contracts."
            )


# ---------------------------------------------------------------------------- check 4


def _load_ticket_status() -> dict[str, str]:
    """Return ``{ticket_id: status}`` from whichever status store exists, or ``{}``.

    Two sources are probed, both optional. If one ever starts carrying statuses this check
    turns itself on with no edit here.
    """
    statuses: dict[str, str] = {}
    tickets_path = ROOT / "tickets.json"
    if tickets_path.is_file():
        data = json.loads(tickets_path.read_text())
        for ticket in data.get("tickets", []):
            if "status" in ticket:
                statuses[ticket["id"]] = str(ticket["status"]).upper()
    state_path = ROOT / ".swarm-loop" / "state.json"
    if not statuses and state_path.is_file():
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return statuses
        candidate = state.get("tickets") or state.get("ticket_status") or {}
        if isinstance(candidate, dict):
            for ticket_id, value in candidate.items():
                status = value.get("status") if isinstance(value, dict) else value
                if isinstance(status, str):
                    statuses[ticket_id] = status.upper()
    return statuses


def verify_paths(command: str) -> list[str]:
    """Extract the path-like operands from a verify command string."""
    found = []
    for token in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command):
        for word in token.split():
            if word.startswith("-") or "=" in word:
                continue
            if word in {"pytest", "npx", "vitest", "run", "make", "python", "-m"}:
                continue
            if "/" in word:
                found.append(word)
    return found


def report_missing_verify_paths(statuses: dict[str, str], failures: list[str]) -> None:
    tickets_path = ROOT / "tickets.json"
    if not tickets_path.is_file():
        print("  note: tickets.json not found; skipping the verify-path report.")
        return
    data = json.loads(tickets_path.read_text())
    missing: list[tuple[str, str]] = []
    for ticket in data.get("tickets", []):
        for path in verify_paths(ticket.get("verify", "")):
            target = ROOT / path
            if target.exists():
                continue
            missing.append((ticket["id"], path))
            if statuses.get(ticket["id"]) == "CLOSED":
                failures.append(
                    f"{ticket['id']} is CLOSED but its verify path {path} does not exist."
                )
    if not statuses:
        print(
            "  note: no ticket-status store found (tickets.json has no `status` field and "
            ".swarm-loop/state.json carries no per-ticket status), so the "
            "'CLOSED ticket with a missing verify path' check is inert. The scaffold "
            "report below still runs."
        )
    if missing:
        print(
            f"  {len(missing)} ticket verify path(s) not created yet (expected on a fresh scaffold):"
        )
        for ticket_id, path in missing:
            print(f"    {ticket_id}  {path}")
    else:
        print("  every ticket verify path exists.")


# ---------------------------------------------------------------------------- main


def main() -> int:
    failures: list[str] = []
    print("check_verify_contracts:")
    check_single_pytest_config(failures)
    check_pixel_path_filter(failures)
    check_superseded_schema_dir(failures)
    report_missing_verify_paths(_load_ticket_status(), failures)

    if failures:
        print("\nFAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("  OK: pytest-config, test-path-filter and schema-package contracts all hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
