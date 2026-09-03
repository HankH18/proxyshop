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
the isolated venv round trip
    :func:`test_the_flat_namespaces_survive_a_pytest_run` sets ``UF_HIDDEN`` on a scratch
    venv's ``site-packages``, proves a bare child then CANNOT import the flat namespaces,
    repairs it through ``bootstrap.sh --check-namespaces``, runs a real pytest suite
    through that interpreter, and only then asserts the bare import — the ordering T-123
    acceptance 5 demands ("proven by a bare ``python -c`` import AFTER the suite, not
    before").

    **T-174: the scratch venv, not the checkout's own.** This test used to run
    ``chflags -R hidden`` on ``$REPO_ROOT/.venv/lib/python3.12/site-packages`` — the one
    piece of state ``PROXYSHOP_WORKER`` cannot isolate — and hold it hidden across a nested
    pytest run with a 600 s timeout. Everything that reaches the flat namespaces through
    the ``.pth`` file, rather than through pytest's ``pythonpath`` ini, was dead for that
    whole window. Measured in this checkout rather than reasoned about, with the window
    opened and closed by hand::

        # inside the window
        (cd / && .venv/bin/python -c 'import contracts')   ModuleNotFoundError
        .venv/bin/python -c 'import exchange.main'         ModuleNotFoundError
        # ...and, for scope, what is NOT affected
        .venv/bin/python -m pytest packages/llm            210 passed
        .venv/bin/mypy proxyshop_support/neo4j_lock.py     Success

    So the window kills every service entry point (``uvicorn exchange.main:app`` and each
    of its siblings) and every bare import from an unrelated cwd — the exact guarantee
    ``bootstrap.sh``'s ``assert_flat_namespaces`` exists to defend, and the exact reason
    that assertion exists at all. A ``pytest``-shaped measurement survives it, because
    pytest re-adds ``.pkgroot`` itself; that is the same blindness that made this a live
    hazard for six cycles without any run going red.

    The guarantee is unchanged and every assertion below is the same assertion; only the
    tree it runs against is isolated. :func:`_isolated_venv_tree` builds a real, working
    interpreter — a venv whose ``.pth`` adds this repo, ``.pkgroot`` and the checkout's
    installed packages — so the mechanism under test (``site.addpackage`` silently skipping
    a HIDDEN ``.pth`` file) is the real one, and the blast radius is one ``tmp_path``.
    :func:`test_the_shared_site_packages_is_never_hidden_by_this_module` keeps it that way.
"""

from __future__ import annotations

import ast
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


def _bare_import(
    cwd: str = "/", python: Path | str | None = None
) -> subprocess.CompletedProcess[str]:
    """``python -c 'import contracts, llm, trust'`` from an unrelated cwd, no PYTHONPATH.

    ``python`` defaults to the interpreter running this suite — the checkout's own venv,
    which is what the T-174 isolation assertion watches. The round-trip test passes the
    scratch venv's interpreter instead.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    return subprocess.run(
        [
            str(python or sys.executable),
            "-c",
            "import contracts, llm, trust; print(contracts.__file__)",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _isolated_venv_tree(tmp_path: Path) -> Path:
    """A worktree-shaped scratch tree with a REAL, working interpreter of its own (T-174).

    Small on purpose — nothing is installed and nothing is copied. The venv is four files:

    * ``bin/python``, a symlink to the same base interpreter the checkout's venv points at,
      so ``sys.prefix`` lands on the scratch tree and its OWN ``site-packages`` is the one
      ``site`` scans;
    * ``pyvenv.cfg``, which is what makes the symlink a venv rather than a bare interpreter;
    * ``lib/python3.12/site-packages/_proxyshop.pth``, the same three-line shape ``uv sync``
      writes — this repo, ``.pkgroot``, and (additionally) the checkout's installed
      packages, so ``python -m pytest`` works here without a second ``uv sync``;
    * ``scripts/bootstrap.sh``, copied, because ``--check-namespaces`` derives its root from
      the script's own location and therefore repairs THIS tree.

    Hiding this site-packages breaks exactly what hiding the real one broke — ``.pth``
    processing — and nothing outside ``tmp_path`` can observe it.
    """
    assert sys.version_info[:2] == (3, 12), (
        f"scripts/bootstrap.sh hard-codes .venv/lib/python3.12/site-packages, so a scratch "
        f"tree built for {sys.version_info[:2]} would not be the tree it repairs"
    )
    base_python = Path(sys.executable).resolve()
    assert base_python.exists(), base_python

    root = tmp_path / "isolated"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(BOOTSTRAP, root / "scripts" / "bootstrap.sh")

    bindir = root / ".venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").symlink_to(base_python)
    (root / ".venv" / "pyvenv.cfg").write_text(
        f"home = {base_python.parent}\n"
        f"implementation = CPython\n"
        f"version_info = {sys.version.split()[0]}\n"
        f"include-system-site-packages = false\n"
    )

    site = root / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "_proxyshop.pth").write_text(
        f"{REPO_ROOT}\n{REPO_ROOT / '.pkgroot'}\n{SITE_PACKAGES}\n"
    )
    return root


@requires_chflags
def test_the_flat_namespaces_survive_a_pytest_run(tmp_path: Path) -> None:
    """Start from the broken state, repair it the way provisioning does, then run pytest.

    The deterministic trigger for the flag coming back was never isolated (two sessions'
    probes genuinely disagreed; see the ticket), so this does not test a cure. It tests the
    mitigation on its own terms: a recursive clear survives a real suite, and the bare
    import that every service entry point depends on works afterwards.

    T-174: the tree put into the broken state is a scratch venv, NOT the checkout's shared
    ``.venv``. Every assertion is the one this test always made; what changed is that the
    UF_HIDDEN window is confined to ``tmp_path``, so it can no longer take this checkout's
    service entry points and bare imports down with it for the duration. The window is
    bounded by the nested suite's own 120 s timeout rather than 600 s, and the ``finally``
    still clears the flag unconditionally.
    """
    assert SITE_PACKAGES.is_dir(), f"{SITE_PACKAGES} is missing; run scripts/bootstrap.sh"
    tree = _isolated_venv_tree(tmp_path)
    scratch_python = tree / ".venv" / "bin" / "python"
    scratch_site = tree / ".venv" / "lib" / "python3.12" / "site-packages"

    # The scratch interpreter has to reproduce the healthy state before breaking it is
    # evidence of anything — otherwise "the import failed" would just mean "this venv was
    # never wired up", and the test would pass while observing nothing.
    healthy = _bare_import(python=scratch_python)
    assert healthy.returncode == 0, (
        f"the scratch venv cannot import the flat namespaces before anything is hidden, so "
        f"it is not a working stand-in for the checkout's venv: {healthy.stderr}"
    )
    assert ".pkgroot" in healthy.stdout, healthy.stdout

    try:
        subprocess.run(["chflags", "-R", "hidden", str(scratch_site)], check=True, timeout=120)
        broken = _bare_import(python=scratch_python)
        assert broken.returncode != 0, (
            "setting UF_HIDDEN did not break the flat namespaces, so this test is not "
            f"observing the mechanism it claims to: {broken.stdout}"
        )
        assert "ModuleNotFoundError" in broken.stderr, broken.stderr

        # T-174, asserted rather than asserted-about: while the scratch tree is broken, the
        # checkout every other process in this machine shares is untouched. Under the
        # previous version of this test this line was the one that failed.
        bystander = _bare_import()
        assert bystander.returncode == 0, (
            f"this test broke the checkout's SHARED {SITE_PACKAGES} rather than its own "
            f"scratch tree. For as long as that window is open, every process in this "
            f"checkout that reaches the flat namespaces through the .pth file — every "
            f"service entry point, and every bare import from an unrelated cwd — dies with "
            f"ModuleNotFoundError (T-174): {bystander.stderr}"
        )

        repaired = subprocess.run(
            [str(tree / "scripts" / "bootstrap.sh"), "--check-namespaces"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert repaired.returncode == 0, repaired.stdout + repaired.stderr

        # The ticket's own verify line, run here rather than assumed: a real suite, and the
        # bare import AFTER it. It runs through the scratch interpreter — which is the one
        # whose site-packages was just repaired — against the real repo.
        suite = subprocess.run(
            [
                str(scratch_python),
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
            timeout=120,
            env={**os.environ, "PROXYSHOP_WORKER": os.environ["PROXYSHOP_WORKER"]},
        )
        assert suite.returncode == 0, suite.stdout[-4000:] + suite.stderr[-2000:]

        after = _bare_import(python=scratch_python)
        assert after.returncode == 0, (
            f"a bare child could not import the flat namespaces after a pytest run: {after.stderr}"
        )
        assert ".pkgroot" in after.stdout, after.stdout
    finally:
        subprocess.run(["chflags", "-R", "nohidden", str(scratch_site)], timeout=120)


def test_the_shared_site_packages_is_never_hidden_by_this_module() -> None:
    """T-174 — no test in this file may put the checkout's SHARED venv into the broken state.

    The guarantee above is real and has to keep being proven, but proving it by hiding
    ``$REPO_ROOT/.venv/lib/python3.12/site-packages`` blinds every other Python process in
    the checkout for the length of the window. ``PROXYSHOP_WORKER`` isolates the Postgres
    database, the Redis DB index and the Redis key prefix; it does not and cannot isolate
    site-packages, so this is the one piece of state a worker can wreck for everybody —
    including for whatever is reading the frozen metrics at that moment.

    Structural rather than behavioural on purpose: the behaviour is a *window*, so a test
    that watched for it would have to race it. Reading the source cannot race.
    """
    module = Path(__file__)
    tree = ast.parse(module.read_text())

    hidden_targets: list[str] = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        argv = call.args[0]
        if not isinstance(argv, ast.List) or not argv.elts:
            continue
        head = argv.elts[0]
        if not (isinstance(head, ast.Constant) and head.value == "chflags"):
            continue
        for element in argv.elts[1:]:
            for name in ast.walk(element):
                if isinstance(name, ast.Name):
                    hidden_targets.append(name.id)

    assert hidden_targets, (
        "no `chflags` invocation was found in this module at all, so this guard is "
        "watching nothing. If the round-trip test stopped exercising UF_HIDDEN, T-123's "
        "guarantee is no longer proven and this check needs rewriting, not deleting."
    )
    assert "SITE_PACKAGES" not in hidden_targets, (
        f"a test in this module runs chflags against SITE_PACKAGES — the checkout's real, "
        f"SHARED {SITE_PACKAGES}. Until the flag is cleared, every process in this checkout "
        f"that reaches the flat namespaces through the .pth file fails with "
        f"ModuleNotFoundError: every service entry point, and every bare import from an "
        f"unrelated cwd. A pytest run survives it, which is precisely why this went "
        f"unnoticed — nothing goes red. Build an isolated tree with _isolated_venv_tree() "
        f"and break that instead (T-174). chflags targets found: "
        f"{sorted(set(hidden_targets))}"
    )
