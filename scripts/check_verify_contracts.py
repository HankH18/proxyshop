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
4. **No test directory is empty.** ``pytest`` exits 0 when *one* directory's tests are
   deleted while others remain, and the root ``vitest`` run carries ``--passWithNoTests``
   (D7), so a ticket can be merged with its tests removed and the gate stays green. Every
   directory named ``tests/`` (and ``e2e/``) must contain at least one test file, and every
   vitest project root declared in ``vitest.config.ts`` must contain at least one
   ``*.test.ts``/``*.test.tsx``.
5. **D39 — no raw Redis client outside the wrapper.** Application code that does
   ``redis.Redis.from_url(os.environ["REDIS_URL"])`` writes *unprefixed* keys to a shared
   logical DB, which the prefixed ``redis_client`` fixture cannot see and a sibling
   worker's ``FLUSHDB`` will delete. Everything goes through
   ``proxyshop_support.redis_client.worker_redis``. (``.importlinter`` forbids importing
   ``redis`` from member *source* at all; this check additionally covers tests, scripts and
   anything else import-linter's root packages do not span.)

Non-fatal report
----------------
6. Ticket verify paths that do not exist yet. This is a scaffold sanity check, printed and
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


#: Directories the filesystem fallback never descends into.
_WALK_SKIP = {
    ".git",
    ".venv",
    ".pkgroot",
    ".swarm-loop",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".react-router",
}


def tracked_files() -> list[str]:
    """Repo-relative paths of every tracked file.

    Falls back to a filesystem walk when git cannot answer — an exported tarball or a
    build artifact directory is not a git repository, and crashing with a traceback there
    would be a far worse failure than scanning a few extra files.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        return [line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        print("  note: not a git checkout; falling back to a filesystem walk.")
        found = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if _WALK_SKIP.intersection(relative.parts):
                continue
            found.append(str(relative))
        return sorted(found)


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

#: Directories that must contain tests even though they are not named ``tests``.
EXTRA_TEST_DIRS = ("e2e",)

PY_TEST_RE = re.compile(r"^test_.*\.py$")
TS_TEST_RE = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx)$")


def _is_test_file(name: str) -> bool:
    return bool(PY_TEST_RE.match(name) or TS_TEST_RE.search(name))


def check_no_empty_test_dirs(failures: list[str]) -> None:
    """Every ``tests/`` directory (and ``e2e/``) holds at least one test file."""
    by_dir: dict[str, list[str]] = {}
    for path in source_files():
        parts = Path(path).parts
        for index, part in enumerate(parts[:-1]):
            if part == "tests" or (index == 0 and part in EXTRA_TEST_DIRS):
                by_dir.setdefault("/".join(parts[: index + 1]), []).append(parts[-1])
    for directory, names in sorted(by_dir.items()):
        if not any(_is_test_file(name) for name in names):
            failures.append(
                f"{directory}/ contains no test file (test_*.py or *.test.ts). An empty "
                f"test directory is exit 0 for pytest and vitest alike, so a ticket whose "
                f"tests were deleted would pass the gate. Delete the directory or restore "
                f"its tests."
            )


VITEST_ROOT_RE = re.compile(r"""root:\s*["']\./([^"']+)["']""")


def check_vitest_projects_have_tests(failures: list[str]) -> None:
    """Every vitest project root declared in ``vitest.config.ts`` has a test file.

    The root vitest run carries ``--passWithNoTests`` per D7, so vitest itself reports
    success on a project with nothing to run. ``tsc`` only accidentally covers the case
    where the *last* ``.ts`` file in a project disappears.
    """
    config = ROOT / "vitest.config.ts"
    if not config.is_file():
        failures.append("vitest.config.ts is missing; the TypeScript gate has no projects.")
        return
    roots = VITEST_ROOT_RE.findall(config.read_text())
    if not roots:
        failures.append("vitest.config.ts declares no project roots; nothing would run.")
        return
    tracked = source_files()
    for project_root in sorted(set(roots)):
        prefix = f"{project_root.rstrip('/')}/"
        if not any(
            path.startswith(prefix) and TS_TEST_RE.search(Path(path).name) for path in tracked
        ):
            failures.append(
                f"vitest project root {project_root} contains no *.test.ts/tsx file. With "
                f"--passWithNoTests (D7) that project would silently contribute zero tests."
            )


# ---------------------------------------------------------------------------- check 5

#: Reaching redis-py at all, from anywhere but the wrapper, is the violation — a client can
#: be built in too many shapes to enumerate (``redis.Redis(...)``, ``Redis.from_url(...)``,
#: ``redis.asyncio.from_url(...)``, a bare ``ConnectionPool``), so the *import* is what is
#: checked. ``redis.exceptions`` is exempt so ``except redis.exceptions.ConnectionError``
#: stays available, matching the ``.importlinter`` contract's ``ignore_imports``.
RAW_REDIS_PATTERNS = (
    re.compile(r"^\s*import\s+redis(?!\.exceptions)\b", re.MULTILINE),
    re.compile(r"^\s*from\s+redis(?!\.exceptions)(\.[\w.]+)?\s+import\s+", re.MULTILINE),
    re.compile(r"(?<![\w.])(Redis|StrictRedis|ConnectionPool)\.from_url\s*\("),
)

#: Only the wrapper itself, and this checker, may name those constructions.
REDIS_WRAPPER_ALLOWED = frozenset(
    {"proxyshop_support/redis_client.py", "scripts/check_verify_contracts.py"}
)


def check_no_raw_redis_clients(failures: list[str]) -> None:
    for path in source_files():
        if path in REDIS_WRAPPER_ALLOWED or not path.endswith(".py"):
            continue
        try:
            text = (ROOT / path).read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in RAW_REDIS_PATTERNS:
            match = pattern.search(text)
            if match:
                line = text[: match.start()].count("\n") + 1
                failures.append(
                    f"D39: {path}:{line} builds a Redis client directly "
                    f"({match.group(0).strip()!r}). A raw client writes UNPREFIXED keys to "
                    f"a logical DB it shares with other workers — invisible to the "
                    f"`redis_client` fixture and erased by any sibling's FLUSHDB. Use "
                    f"`proxyshop_support.redis_client.worker_redis()`."
                )
                break


# ---------------------------------------------------------------------------- check 6


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
    check_no_empty_test_dirs(failures)
    check_vitest_projects_have_tests(failures)
    check_no_raw_redis_clients(failures)
    report_missing_verify_paths(_load_ticket_status(), failures)

    if failures:
        print("\nFAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(
        "  OK: pytest-config, test-path-filter, schema-package, non-empty-test-dir and "
        "raw-Redis-client contracts all hold."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
