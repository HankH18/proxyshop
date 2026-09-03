"""T-111 / T-123 — provisioning fails loudly instead of warning, and the fix is recursive.

Both tickets were reported closed twice while their defects were live, because both of
their stated gates are vacuous:

* T-111's ``./scripts/bootstrap.sh && ./scripts/verify.sh check`` exits 0 whether or not
  the swallowing exists — provisioning succeeded, so the ``|| true`` never fired;
* T-123's ``./scripts/bootstrap.sh && pytest packages/llm -q && python -c 'import
  contracts, llm, trust'`` exits 0 for the same reason, on a tree whose flag happened to
  be clear already.

So the two behaviours are asserted here instead, against a scratch tree that can be put
into the broken state on purpose:

``--check-namespaces``
    The clear-and-assert phase of ``scripts/bootstrap.sh``, runnable without ``uv sync``
    and ``npm ci`` so it can be tested at all. The full script runs the same two functions.
the scratch tree
    ``scripts/bootstrap.sh`` copied next to a fake ``.venv/bin/python`` and a fake
    ``chflags``, so "the namespaces do not import" and "chflags failed" are producible
    states rather than states we wait for.
the real venv round trip
    :func:`test_the_flat_namespaces_survive_a_pytest_run` sets ``UF_HIDDEN`` on this
    worktree's own ``site-packages``, proves a bare child then CANNOT import the flat
    namespaces, repairs it through ``bootstrap.sh --check-namespaces``, runs a real pytest
    suite, and only then asserts the bare import — the ordering T-123 acceptance 5 demands
    ("proven by a bare ``python -c`` import AFTER the suite, not before").
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
SITE_PACKAGES = REPO_ROOT / ".venv" / "lib" / "python3.12" / "site-packages"

HEALTHY_PYTHON = "#!/bin/sh\necho '/scratch/.pkgroot/contracts/__init__.py'\nexit 0\n"
DEAD_PYTHON = "#!/bin/sh\necho \"ModuleNotFoundError: No module named 'contracts'\" >&2\nexit 1\n"

requires_chflags = pytest.mark.skipif(
    shutil.which("chflags") is None, reason="UF_HIDDEN and chflags are macOS-only (D42)"
)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _scratch_tree(tmp_path: Path, *, python: str) -> Path:
    """A directory shaped enough like a provisioned worktree for the assert phase."""
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(BOOTSTRAP, root / "scripts" / "bootstrap.sh")

    site = root / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "_proxyshop.pth").write_text(f"{root}\n{root}/.pkgroot\n")
    (site / "_virtualenv.pth").write_text("import _virtualenv\n")

    interpreter = root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(python)
    interpreter.chmod(0o755)
    return root


def _fake_chflags(tmp_path: Path, *, body: str) -> dict[str, str]:
    """PATH overriding ``chflags`` with ``body``. Returns the env overlay to pass through."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "chflags"
    fake.write_text(body)
    fake.chmod(0o755)
    return {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}


def _check_namespaces(
    root: Path, env_overlay: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(root / "scripts" / "bootstrap.sh"), "--check-namespaces"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **(env_overlay or {})},
    )


def _script_text() -> str:
    return BOOTSTRAP.read_text()


# --------------------------------------------------------------------------------------
# T-111 acceptance 1 — a dead namespace is a failure, not a warning on stderr
# --------------------------------------------------------------------------------------


def test_provisioning_fails_when_the_flat_namespaces_do_not_import(tmp_path: Path) -> None:
    """``|| echo "    WARNING: ..." >&2`` let bootstrap print ``OK: bootstrap`` and exit 0
    with the ``.pkgroot`` namespaces inert. That reached a measurement twice: it read
    acceptance_pass_rate 5.00 instead of 13.33, and dropped build_succeeds 1 -> 0, with no
    product cause anywhere."""
    root = _scratch_tree(tmp_path, python=DEAD_PYTHON)
    result = _check_namespaces(root)

    assert result.returncode != 0, (
        f"dead namespaces still provisioned successfully\n{result.stdout}{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "ModuleNotFoundError" in combined, combined
    # The operator advice that used to BE the warning must survive as the failure text.
    assert ".pkgroot" in combined, combined
    assert "OK" not in result.stdout, result.stdout


def test_provisioning_succeeds_and_names_the_module_it_imported(tmp_path: Path) -> None:
    """Acceptance 6's "assert the effect": print a resolved ``__file__``, not an exit code."""
    root = _scratch_tree(tmp_path, python=HEALTHY_PYTHON)
    result = _check_namespaces(root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "/scratch/.pkgroot/contracts/__init__.py" in result.stdout, result.stdout


# --------------------------------------------------------------------------------------
# T-111 acceptance 2 / T-123 acceptance 6 — the chflags step reports failure, recursively
# --------------------------------------------------------------------------------------


def test_a_failing_chflags_fails_provisioning(tmp_path: Path) -> None:
    """``chflags nohidden "$pth" || true`` neutralised ``set -euo pipefail`` at :7, so the
    one step that provides the import guarantee could not fail the script that consumes
    it."""
    root = _scratch_tree(tmp_path, python=HEALTHY_PYTHON)
    overlay = _fake_chflags(tmp_path, body='#!/bin/sh\necho "chflags: boom" >&2\nexit 1\n')
    result = _check_namespaces(root, overlay)

    assert result.returncode != 0, (
        f"a failing chflags was swallowed\n{result.stdout}{result.stderr}"
    )


def test_the_hidden_flag_is_cleared_recursively_on_the_site_packages_directory(
    tmp_path: Path,
) -> None:
    """T-123's root cause: the site-packages DIRECTORY carries ``UF_HIDDEN``, not only the
    ``.pth`` files, which is why clearing one file "kept coming back". A recursive clear of
    the directory is the fix; a per-file loop is not."""
    root = _scratch_tree(tmp_path, python=HEALTHY_PYTHON)
    record = tmp_path / "chflags.log"
    overlay = _fake_chflags(tmp_path, body=f'#!/bin/sh\necho "$@" >> "{record}"\nexit 0\n')
    result = _check_namespaces(root, overlay)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = record.read_text().splitlines()
    site = str(root / ".venv" / "lib" / "python3.12" / "site-packages")
    assert any("-R" in call and "nohidden" in call and site in call for call in calls), calls


def test_the_script_carries_no_escape_hatch_on_either_guarantee() -> None:
    """A text assertion on purpose, and only for these two lines.

    The behavioural tests above prove the semantics of the script as it stands; this one
    pins the *specific* swallow that shipped three times and was reported fixed twice, so
    that re-adding ``|| true`` to the chflags step or ``|| echo WARNING`` to the import
    assertion is a red test rather than a silent regression to a vacuous gate.
    """
    for line in _script_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "chflags" in stripped:
            assert "|| true" not in stripped, line
        if "WARNING" in stripped:
            assert not stripped.startswith("||"), line


def test_a_note_records_that_the_measurement_tree_needs_the_same_provisioning() -> None:
    """T-111 acceptance 3. It existed only in ``.swarm-loop/backlog.md``, which no operator
    of this script reads and no gate checks."""
    text = _script_text()
    assert "measurement" in text.lower(), "bootstrap.sh does not mention the measurement tree"


# --------------------------------------------------------------------------------------
# T-123 acceptance 3 & 5 — a bare child imports the namespaces AFTER a real pytest run
# --------------------------------------------------------------------------------------


def _bare_import(cwd: str = "/") -> subprocess.CompletedProcess[str]:
    """``python -c 'import contracts, llm, trust'`` from an unrelated cwd, no PYTHONPATH."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    return subprocess.run(
        [sys.executable, "-c", "import contracts, llm, trust; print(contracts.__file__)"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


@requires_chflags
def test_the_flat_namespaces_survive_a_pytest_run(tmp_path: Path) -> None:
    """Start from the broken state, repair it the way provisioning does, then run pytest.

    The deterministic trigger for the flag coming back was never isolated (two sessions'
    probes genuinely disagreed; see the ticket), so this does not test a cure. It tests the
    mitigation on its own terms: a recursive clear survives a real suite, and the bare
    import that every service entry point depends on works afterwards.

    The ``finally`` clears the flag unconditionally — a failure here must not leave this
    worktree's venv in the state the test deliberately created.
    """
    assert SITE_PACKAGES.is_dir(), f"{SITE_PACKAGES} is missing; run scripts/bootstrap.sh"
    try:
        subprocess.run(["chflags", "-R", "hidden", str(SITE_PACKAGES)], check=True, timeout=120)
        broken = _bare_import()
        assert broken.returncode != 0, (
            "setting UF_HIDDEN did not break the flat namespaces, so this test is not "
            f"observing the mechanism it claims to: {broken.stdout}"
        )
        assert "ModuleNotFoundError" in broken.stderr, broken.stderr

        repaired = subprocess.run(
            [str(BOOTSTRAP), "--check-namespaces"], capture_output=True, text=True, timeout=300
        )
        assert repaired.returncode == 0, repaired.stdout + repaired.stderr

        # The ticket's own verify line, run here rather than assumed: a real suite, and the
        # bare import AFTER it.
        suite = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "packages/llm",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "PROXYSHOP_WORKER": os.environ["PROXYSHOP_WORKER"]},
        )
        assert suite.returncode == 0, suite.stdout[-4000:] + suite.stderr[-2000:]

        after = _bare_import()
        assert after.returncode == 0, (
            f"a bare child could not import the flat namespaces after a pytest run: {after.stderr}"
        )
        assert ".pkgroot" in after.stdout, after.stdout
    finally:
        subprocess.run(["chflags", "-R", "nohidden", str(SITE_PACKAGES)], timeout=120)
