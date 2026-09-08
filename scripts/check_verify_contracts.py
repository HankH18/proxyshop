#!/usr/bin/env python
"""Enforce the structural contracts that no linter can see.

Orchestrator-owned (T-000), frozen. Run from ``make check`` and ``make verify``.

Fatal checks
------------
1. **D36 — exactly one pytest configuration.** Only the root ``pyproject.toml`` may carry
   ``[tool.pytest.ini_options]``; no ``pytest.ini``, ``tox.ini [pytest]`` or
   ``setup.cfg [tool:pytest]`` may exist anywhere. A second configuration silently changes
   ``rootdir`` and with it ``pythonpath``, ``testpaths`` and the import mode.
2. **D37 — no "pixel" outside ``pixel/``.** The pixel ticket's verify is
   ``npx vitest run pixel``, which vitest treats as a *case-insensitive path substring*
   filter applied across every project rather than as a project selector. Any
   ``*.test.``/``*.spec.`` file with a ts/tsx/js/jsx extension elsewhere whose path contains
   "pixel" would silently join that run. Scoped to what vitest can actually collect: a
   ``test_*.py`` file matches no project's ``include`` glob, so it is not a candidate for
   the filter and this check does not flag it.
3. **D1 — the old schema package name is gone.** The schema package is
   ``packages/contracts``; the superseded name must not appear in the source tree.
4. **No test directory is empty.** ``pytest`` exits 0 when *one* directory's tests are
   deleted while others remain. ``run_vitest`` now FATALs when vitest collects zero tests
   overall (ESC-023 removed the ``--passWithNoTests`` D7 recorded), but a single project
   losing its tests while others still report is finer-grained than that and stays invisible
   to it — which is what this check catches. Every
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
6. **No duplicate fixture name in one test directory.** Up to seven tickets share a single
   ``tests/`` directory and each drops its own ``_fixtures_<topic>.py`` there. Two of them
   choosing the same fixture name is a real defect — pytest can only bind one — but it must
   be *the offending ticket's* defect. ``proxyshop_support.fixture_loader`` therefore
   degrades the name to a fixture that raises only when requested, instead of raising at
   conftest-import time and taking down every already-merged ticket's tests in that
   directory. This static check is what makes the defect fatal, in the gate of the ticket
   that introduced it, without any innocent test going red.
7. **The verify pipeline still runs the release blockers, and still refuses a run that
   skipped a whole class.** Both of the properties this file's siblings acquired are
   invisible to every other gate in the repo, and both were absent for the whole build:
   ``make verify`` never collected ``.swarm-loop/acceptance`` — so the three S8 release
   blockers, the tests that decide whether the release is allowed, were not in the file set
   the headline metric is measured over — and a run whose datastores were down reported
   ``22 passed, 36 skipped`` at exit 0, which is indistinguishable from a full pass. Nothing
   would notice either one coming back: deleting the ``run_acceptance`` call leaves every
   test in this repo green, because the deleted thing IS the gate. So the wiring itself is
   asserted here, statically, against ``scripts/verify.sh`` and the root ``pyproject.toml``.

   ``norecursedirs`` is asserted to KEEP excluding ``.swarm-loop``. That looks backwards
   until you measure it: ``norecursedirs`` never suppressed the acceptance suite in the
   first place (``pytest --collect-only .swarm-loop/acceptance`` collects all 120 tests
   today, because that key only stops *recursion*, never a path named on the command line),
   and un-excluding it would invite the frozen suite into the same pytest session as
   ``apps``/``packages``/``services``, where it produces phantom failures that vanish in
   isolation. The repair is the separate process ``verify.sh`` now runs, not a wider walk.

Non-fatal report
----------------
8. Ticket verify paths that do not exist yet. This is a scaffold sanity check, printed and
   never fatal: on a fresh scaffold nearly all of them are legitimately missing.

Note on check 4 vs. the original specification: the intake specified "any ticket whose
status is CLOSED has a verify path that does not exist -> FAIL". There is no ticket-status
store in this repo — ``tickets.json`` tickets carry no ``status`` field and
``.swarm-loop/state.json`` carries no per-ticket status — so that check has no data source.
It is implemented as a clean no-op that says so, and turns itself on automatically if a
status store ever appears (see :func:`_load_ticket_status`).
"""

from __future__ import annotations

import ast
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

#: A test file as each runner recognises one. ``TS_TEST_RE`` is deliberately the *vitest*
#: half and ``PY_TEST_RE`` the *pytest* half: several checks below turn on which runner could
#: collect a given file, and conflating them is what made check 2 fire on a Python file.
PY_TEST_RE = re.compile(r"^test_.*\.py$")
TS_TEST_RE = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx)$")


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
    """Repo-relative paths of every tracked **and every not-yet-committed** file.

    ``--others --exclude-standard`` is not a detail: a worker runs ``make check`` on work it
    has not committed yet, and that is exactly when these contracts are worth enforcing. A
    brand-new ``_fixtures_<topic>.py`` colliding with a merged ticket's fixture name, or a
    new module building a raw Redis client, was invisible to every check here while it sat
    uncommitted — the gate went green and the defect was found later by somebody else.
    ``--exclude-standard`` honours ``.gitignore``, so caches and ``.venv`` stay out.

    Falls back to a filesystem walk when git cannot answer — an exported tarball or a
    build artifact directory is not a git repository, and crashing with a traceback there
    would be a far worse failure than scanning a few extra files.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return sorted({line for line in out.splitlines() if line})
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
                    f"D36: {path} contains [tool.pytest.ini_options]; the root "
                    f"pyproject.toml is the only pytest configuration in this repo."
                )
        if Path(path).name == "pytest.ini":
            failures.append(
                f"D36: {path} exists; pytest configuration lives only in the root manifest."
            )
        if Path(path).name in {"tox.ini", "setup.cfg"}:
            parser = configparser.ConfigParser()
            try:
                parser.read(ROOT / path)
            except configparser.Error:
                continue
            section = "pytest" if Path(path).name == "tox.ini" else "tool:pytest"
            if parser.has_section(section):
                failures.append(f"D36: {path} carries a [{section}] section.")


# ---------------------------------------------------------------------------- check 2


def check_pixel_path_filter(failures: list[str]) -> None:
    """No file *vitest can collect* carries "pixel" in its path from outside ``pixel/``.

    The scope is the vitest-collectable set — ``*.test.``/``*.spec.`` with a ts/tsx/js/jsx
    extension — and NOT every file whose name begins ``test_``. That wider net is what this
    check used to cast, and it was wrong rather than merely strict: it failed the build on
    ``apps/merchant/svc/tests/test_pixel_ledger.py``, a **Python** file that
    ``npx vitest run pixel`` cannot collect and never could. Every project in
    ``vitest.config.ts`` includes only ``*.test.ts``/``*.test.tsx`` under its own root, so no
    ``.py`` path is ever a candidate for the filter to match; measured, ``npx vitest list
    pixel`` collects the four ``*.test.ts`` files under ``pixel/tests/`` and nothing else.
    Applying a JS-tooling constraint to files that tooling cannot see bought no isolation and
    cost the domain's own word — "pixel" names a served route (``POST /pixel/collect``) and a
    canonical ledger kind (``checkout_pixel``), so Python tests about them had nowhere honest
    to go.

    The hazard the check exists for is untouched and is real: vitest's positional argument is
    a case-insensitive substring match over the whole path, applied across every project
    rather than selecting one. Measured, ``npx vitest list PIXEL`` collects the pixel
    project's tests despite the case, and ``npx vitest list app/routes`` reaches into the
    *merchant* project. So a TS test named for T-050's acceptance 2 —
    ``apps/merchant/app/routes/webPixelCreate.test.ts`` — would silently join the pixel
    ticket's verify. That file is named ``install.test.ts`` today for exactly this reason.
    """
    needle = "pixel"
    for path in source_files():
        if not TS_TEST_RE.search(Path(path).name):
            continue
        if path.startswith(f"{needle}/"):
            continue
        if needle in path.lower():
            failures.append(
                f"D37: test file {path} has '{needle}' in its path but lives outside "
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

    ``verify.sh``'s ``run_vitest`` FATALs on a zero TOTAL collection, so the whole-suite
    case is covered there; ESC-023 removed the ``--passWithNoTests`` that D7 recorded. What
    that FATAL cannot see is ONE project going empty while the others still report a nonzero
    total, which is why this check remains. ``tsc`` only accidentally covers the case where
    the *last* ``.ts`` file in a project disappears.
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
                f"vitest project root {project_root} contains no *.test.ts/tsx file. The "
                f"suite-wide zero-collection FATAL in verify.sh cannot see this: the other "
                f"projects still report, so the total stays nonzero and this one silently "
                f"contributes nothing."
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

#: Worker-owned fixture files, discovered by ``proxyshop_support.fixture_loader``.
FIXTURE_FILE_GLOB_PREFIX = "_fixtures_"


def _fixture_names(path: Path) -> list[str]:
    """Names ``fixture_loader`` would export from one ``_fixtures_*.py`` file.

    Parsed with :mod:`ast` rather than imported: this checker runs in ``make check`` on
    every ticket, and importing another ticket's fixture module would execute its
    module-level code (and its imports) here.
    """
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except (OSError, SyntaxError):
        return []
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name.startswith("pytest_"):
            names.append(node.name)
            continue
        for decorator in node.decorator_list:
            call = decorator.func if isinstance(decorator, ast.Call) else decorator
            attribute = getattr(call, "attr", None) or getattr(call, "id", None)
            if attribute == "fixture":
                # `@pytest.fixture(name="x")` renames the fixture; honour it.
                alias = node.name
                if isinstance(decorator, ast.Call):
                    for keyword in decorator.keywords:
                        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                            alias = str(keyword.value.value)
                names.append(alias)
                break
    return names


def check_no_duplicate_fixture_names(failures: list[str]) -> None:
    """No two ``_fixtures_*.py`` files in one directory define the same fixture name."""
    by_directory: dict[str, dict[str, list[str]]] = {}
    for path in source_files():
        name = Path(path).name
        if not (name.startswith(FIXTURE_FILE_GLOB_PREFIX) and name.endswith(".py")):
            continue
        directory = str(Path(path).parent)
        for fixture in _fixture_names(ROOT / path):
            by_directory.setdefault(directory, {}).setdefault(fixture, []).append(path)
    for directory, fixtures in sorted(by_directory.items()):
        for fixture, paths in sorted(fixtures.items()):
            if len(paths) < 2:
                continue
            failures.append(
                f"fixture {fixture!r} is defined {len(paths)} times in {directory}/: "
                f"{', '.join(sorted(paths))}. pytest binds one name to one fixture, so "
                f"these shadow each other. Rename all but one — prefix it with the "
                f"defining ticket's topic. (At runtime the loader degrades the name to a "
                f"fixture that raises only when requested, so the directory's other tests "
                f"keep passing; this gate is what makes the defect fatal for the ticket "
                f"that introduced it.)"
            )


# ---------------------------------------------------------------------------- check 7

#: What ``scripts/verify.sh`` must still contain for the two properties above to hold. Each
#: entry is ``(needle, explanation)``; the needle is matched literally against the script.
#:
#: This is a text check on purpose. The alternative — executing the pipeline and asserting on
#: what it did — is what ``make verify`` already is, and it cannot catch the failure mode
#: here: a pipeline missing its acceptance step is *green*, because the missing thing is the
#: gate. The only moment the absence is visible is while reading the wiring, so the wiring is
#: what gets read. Being text, it is fooled by a caller inside a comment; that is an
#: acceptable floor for a check whose job is to stop a silent deletion, not a determined one.
VERIFY_WIRING_CONTRACTS: tuple[tuple[str, str], ...] = (
    (
        ".swarm-loop/acceptance/run.py",
        "verify.sh no longer invokes the frozen acceptance runner, so the three S8 release "
        "blockers are outside the file set `make verify` measures — the state this contract "
        "was written to end.",
    ),
    (
        "run_acceptance",
        "verify.sh defines no run_acceptance step; the frozen acceptance suite is not run "
        "by the pipeline.",
    ),
    (
        "datastore_coverage_gate",
        "verify.sh defines no datastore-coverage gate, so a run whose datastores were down "
        "reports its skipped least-privilege checks as a pass and exits 0.",
    ),
    (
        # Spelled with the call, not the bare module name: `reachability` on its own also
        # matches the T-109 prose already in that file's comments, so the bare word was
        # satisfied by the very version of verify.sh that had no probe in it at all.
        "reachability.unreachable(",
        "the datastore-coverage gate no longer probes proxyshop_support.reachability, so it "
        "cannot know whether the docker-marked class ran.",
    ),
)

#: Guards that must be reached when ``verify.sh`` runs its ``all`` step. ``all`` is what
#: ``make verify`` runs and what the frozen ``build_succeeds`` metric scores, so a guard the
#: ``all`` step does not reach protects nothing.
VERIFY_ALL_STEP_GUARDS: tuple[str, ...] = ("run_acceptance", "datastore_coverage_gate")

#: How far back from a call site to look for its ``if`` guard. The file's idiom is a
#: one-line ``if ...; then f; fi``, but the first draft of this check only accepted that
#: exact shape and reported a correctly-wired multi-line guard as missing — so the lookback
#: exists because the strict version produced a false positive on its own author's code.
_GUARD_LOOKBACK = 4


def check_verify_runs_the_release_blockers(failures: list[str]) -> None:
    """``scripts/verify.sh`` still runs the frozen suite and still gates on datastores."""
    script = ROOT / "scripts" / "verify.sh"
    if not script.is_file():
        failures.append("scripts/verify.sh is missing; there is no verification pipeline.")
        return
    text = script.read_text(errors="ignore")
    for needle, explanation in VERIFY_WIRING_CONTRACTS:
        if needle not in text:
            failures.append(f"scripts/verify.sh no longer mentions {needle!r}: {explanation}")
    lines = text.splitlines()
    for function in VERIFY_ALL_STEP_GUARDS:
        if not _guarded_call_on_all(lines, function):
            failures.append(
                f"scripts/verify.sh never calls {function} on the `all` step. `all` is what "
                f"`make verify` runs and what the frozen build_succeeds metric scores, so a "
                f"guard it does not reach protects nothing."
            )


def _guarded_call_on_all(lines: list[str], function: str) -> bool:
    """True when ``function`` is called under an ``if`` that admits the ``all`` step.

    A call site is any non-comment line naming the function that is neither its ``def`` nor
    part of the doc block above it. The ``if`` may be on the same line (the file's usual
    one-liner) or up to :data:`_GUARD_LOOKBACK` lines earlier.
    """
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if function not in line or stripped.startswith("#"):
            continue
        if stripped.startswith(f"{function}()") or stripped.startswith(f"#{function}"):
            continue  # the definition, not a call
        window = lines[max(0, index - _GUARD_LOOKBACK) : index + 1]
        for candidate in reversed(window):
            candidate_stripped = candidate.lstrip()
            if candidate_stripped.startswith("#"):
                continue
            if candidate_stripped.startswith("if ") and '"$STEP" = all' in candidate:
                return True
    return False


def check_acceptance_stays_out_of_the_main_pytest_walk(failures: list[str]) -> None:
    """The root pytest config still keeps ``.swarm-loop`` out of the main session.

    Deliberately the opposite of what "make the gate see the acceptance suite" sounds like.
    The suite is run by ``verify.sh`` as its OWN pytest process because sharing a session
    with ``apps``/``packages``/``services`` yields phantom failures that vanish in isolation.
    Someone reading only the headline defect could "fix" it by widening the walk, which would
    trade an invisible suite for a flaky one; this makes that edit fail here instead.
    """
    manifest = ROOT / "pyproject.toml"
    if not manifest.is_file():
        failures.append("pyproject.toml is missing; there is no pytest configuration.")
        return
    text = manifest.read_text(errors="ignore")
    match = re.search(r"^norecursedirs\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    if match is None:
        failures.append(
            "pyproject.toml declares no `norecursedirs`. `.swarm-loop` must stay excluded "
            "from the main pytest walk: the frozen acceptance suite is run by verify.sh in "
            "its own process precisely because it cross-contaminates a shared session."
        )
        return
    if ".swarm-loop" not in match.group(1):
        failures.append(
            "pyproject.toml's `norecursedirs` no longer excludes `.swarm-loop`. That does "
            "NOT make `make verify` grade the acceptance suite — `testpaths` never listed "
            "the directory either — it only lets a bare `pytest .` pull the frozen suite "
            "into the same session as apps/packages/services, where it produces phantom "
            "failures. verify.sh runs it as its own process; leave the walk alone."
        )


# ---------------------------------------------------------------------------- check 8


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
    check_no_duplicate_fixture_names(failures)
    check_verify_runs_the_release_blockers(failures)
    check_acceptance_stays_out_of_the_main_pytest_walk(failures)
    report_missing_verify_paths(_load_ticket_status(), failures)

    if failures:
        print("\nFAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(
        "  OK: pytest-config, test-path-filter, schema-package, non-empty-test-dir, "
        "raw-Redis-client, unique-fixture-name, verify-runs-the-release-blockers and "
        "acceptance-runs-in-its-own-process contracts all hold."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
