"""The blast radius of a duplicate fixture name. Orchestrator-owned (T-000), frozen.

Up to seven tickets share one ``tests/`` directory, each dropping a ``_fixtures_<topic>.py``
it owns, and there is no reserved-name registry. Two of them picking the same fixture name
is a matter of time. The loader used to ``raise`` on that at **conftest-import time**, which
meant the offending ticket did not fail its own test — it failed every already-merged
ticket's tests in that directory, before collection, with a traceback naming a conftest none
of them had written.

These tests hold the new contract in place: the collision is *local* (only tests that ask
for the ambiguous name fail), *named* (the error carries both defining files), and *loud*
(a warning at import, and a fatal static gate in ``scripts/check_verify_contracts.py``).

The central test runs a **real pytest subprocess** on a scratch directory. Nothing less
proves the claim: the failure mode being guarded against happens during conftest import, so
it can only be observed by actually collecting a directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from proxyshop_support.fixture_loader import (
    DuplicateFixtureWarning,
    load_sibling_fixtures,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

CONFTEST = """\
from proxyshop_support.fixture_loader import load_sibling_fixtures

globals().update(load_sibling_fixtures(__file__))
"""

ALPHA = """\
import pytest


@pytest.fixture
def alpha_only() -> str:
    return "alpha"


@pytest.fixture
def shared_name() -> str:
    return "from-alpha"
"""

BETA = """\
import pytest


@pytest.fixture
def beta_only() -> str:
    return "beta"


@pytest.fixture
def shared_name() -> str:
    return "from-beta"
"""

PROBE_TESTS = '''\
def test_unrelated_alpha(alpha_only):
    """An already-merged ticket's test. It must not care that someone else collided."""
    assert alpha_only == "alpha"


def test_unrelated_beta(beta_only):
    """A second already-merged ticket's test, in the same directory."""
    assert beta_only == "beta"


def test_uses_the_duplicated_name(shared_name):
    """The only test that may fail: it is the one asking for the ambiguous fixture."""
    assert shared_name
'''


def _write_directory(directory: Path, *, duplicate: bool) -> None:
    (directory / "conftest.py").write_text(CONFTEST)
    (directory / "_fixtures_alpha.py").write_text(ALPHA)
    (directory / "_fixtures_beta.py").write_text(
        BETA if duplicate else BETA.replace("shared_name", "beta_shared_name")
    )
    (directory / "test_probe.py").write_text(
        PROBE_TESTS
        if duplicate
        else PROBE_TESTS.replace(
            "def test_uses_the_duplicated_name(shared_name):",
            "def test_uses_the_duplicated_name(beta_shared_name):",
        ).replace("assert shared_name", "assert beta_shared_name")
    )


def _run_pytest(directory: Path) -> subprocess.CompletedProcess[str]:
    """Collect and run ``directory`` in a fresh interpreter, outside this repo's rootdir.

    T-122: the environment is hermetic on purpose — the whole point is a pytest run that
    inherits nothing from this session — but the import path names BOTH the repo root and
    ``.pkgroot``. The root alone reaches ``proxyshop_support``, which is all today's probe
    needs; ``.pkgroot`` is what reaches every flat package spelling (``contracts``,
    ``trust``, ...), and a probe that grows one import of one of those would otherwise fail
    with ``ModuleNotFoundError`` on any machine where the venv's ``_proxyshop.pth`` does not
    apply, which is exactly how the same defect took ``make verify`` down once already.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-v", "."],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / ".pkgroot")]),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )


def test_a_duplicate_does_not_stop_unrelated_tests_in_the_directory(tmp_path: Path) -> None:
    """The whole point: one ticket's name collision cannot take the directory down.

    Two unrelated tests, owned by two other tickets, must still be collected and pass while
    a third fixture name is ambiguous. Before the fix this run produced **zero** passing
    tests — the conftest raised during import and pytest reported a collection error for the
    entire directory.
    """
    _write_directory(tmp_path, duplicate=True)
    result = _run_pytest(tmp_path)
    output = result.stdout + result.stderr

    assert "test_unrelated_alpha PASSED" in output, output
    assert "test_unrelated_beta PASSED" in output, output
    assert "2 passed" in output, output
    assert result.returncode != 0, "a duplicate must not be silently green"


def test_the_duplicate_failure_names_both_definitions_with_their_paths(tmp_path: Path) -> None:
    """Requirement (b): the error must point at both files, not just say "duplicate"."""
    _write_directory(tmp_path, duplicate=True)
    result = _run_pytest(tmp_path)
    output = result.stdout + result.stderr

    assert "test_uses_the_duplicated_name" in output, output
    assert "DuplicateFixtureError" in output, output
    assert "_fixtures_alpha.py" in output, output
    assert "_fixtures_beta.py" in output, output
    assert "shared_name" in output, output


def test_no_duplicate_means_no_warning_and_no_poison(tmp_path: Path) -> None:
    """The happy path is untouched: distinct names all load and all three tests pass."""
    _write_directory(tmp_path, duplicate=False)
    result = _run_pytest(tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "3 passed" in output, output
    assert "DuplicateFixture" not in output, output


def test_loading_a_duplicate_warns_at_import_and_never_raises(tmp_path: Path) -> None:
    """Requirement: loud, but not fatal. The import returns; a warning carries the detail."""
    _write_directory(tmp_path, duplicate=True)

    with pytest.warns(DuplicateFixtureWarning) as recorded:
        loaded = load_sibling_fixtures(tmp_path / "conftest.py")

    assert set(loaded) == {"alpha_only", "beta_only", "shared_name"}
    message = str(recorded[0].message)
    assert "shared_name" in message
    assert "_fixtures_alpha.py" in message
    assert "_fixtures_beta.py" in message


def test_the_static_gate_makes_a_duplicate_fatal(tmp_path: Path) -> None:
    """`scripts/check_verify_contracts.py` is what turns the local failure into a red gate.

    Without this the collision could hide indefinitely behind a fixture nobody requests.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_verify_contracts as gate
    finally:
        sys.path.pop(0)

    (tmp_path / "_fixtures_alpha.py").write_text(ALPHA)
    (tmp_path / "_fixtures_beta.py").write_text(BETA)
    names_alpha = gate._fixture_names(tmp_path / "_fixtures_alpha.py")
    names_beta = gate._fixture_names(tmp_path / "_fixtures_beta.py")
    assert "shared_name" in names_alpha and "shared_name" in names_beta
    assert set(names_alpha) == {"alpha_only", "shared_name"}


def test_the_static_gate_reads_the_name_keyword(tmp_path: Path) -> None:
    """``@pytest.fixture(name="x")`` renames the fixture; the gate must compare on ``x``."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_verify_contracts as gate
    finally:
        sys.path.pop(0)

    path = tmp_path / "_fixtures_renamed.py"
    path.write_text(
        textwrap.dedent(
            """
            import pytest


            @pytest.fixture(name="ledger_row")
            def _make_row() -> str:
                return "row"


            def pytest_configure(config) -> None:
                pass
            """
        )
    )
    assert gate._fixture_names(path) == ["ledger_row", "pytest_configure"]
