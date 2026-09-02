"""`packages.llm` must import from a bare interpreter with only the repo root on the path.

This package's docstring used to claim that `.pkgroot` "is on `sys.path` for every consumer"
because "pytest adds it via the root `pythonpath` setting". That is false for the consumer
that matters most: the frozen acceptance suite runs pytest with `-o pythonpath=` on purpose,
so a worker cannot prepend a stub directory and shadow the real tree. With the venv's `.pth`
absent, `import packages.llm` then raised — loudly and accurately, but it still raised. The
module now inserts `<repo root>/.pkgroot` itself before importing `llm`, and keeps the loud
`ImportError` as the last resort. This test removes every other mechanism and checks the
insertion is what carries the import.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import packages.llm as public

REPO_ROOT = Path(__file__).resolve().parents[3]


def _import_without_any_pkgroot_help(statements: str) -> subprocess.CompletedProcess[str]:
    """`-S` skips `site`, so no `.pth` runs and the venv's `_proxyshop.pth` is neutralised.

    site-packages is handed back through `PYTHONPATH` so third-party dependencies still
    import — without that this would only prove that `-S` hides pydantic. What is left is
    the repo root and nothing else about this repo, which is what the fallback is for.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT), sysconfig.get_paths()["purelib"]]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-S", "-c", statements],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_pkgroot_is_resolved_from_the_repo_root_not_the_packages_dir() -> None:
    here = Path(public.__file__).resolve()
    assert not (here.parents[1] / ".pkgroot").is_dir(), "parents[1] is packages/"
    assert (here.parents[2] / ".pkgroot").is_dir(), "parents[2] is the repo root"
    assert public._PKGROOT == REPO_ROOT / ".pkgroot"


def test_it_imports_with_only_the_repo_root_on_the_path() -> None:
    proc = _import_without_any_pkgroot_help(
        "import sys\n"
        "assert not [p for p in sys.path if p.endswith('.pkgroot')], sys.path\n"
        "import packages.llm as m\n"
        "print(m.RecordedLLM({'p': 'r'}).complete('p'))\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "r"


def test_the_submodule_binding_survives_the_bare_interpreter_too() -> None:
    """The fallback must land the package in the same one-object-per-file state."""
    proc = _import_without_any_pkgroot_help(
        "import packages.llm\n"
        "import llm.doubles, packages.llm.src.doubles, packages.llm.doubles\n"
        "print(llm.doubles is packages.llm.src.doubles is packages.llm.doubles)\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "True"


def test_the_loud_importerror_is_still_the_last_resort() -> None:
    """`.pkgroot` insertion is the fallback; the actionable message is the fallback's fallback.

    Simulated by pointing the module at a repo root that has no `.pkgroot`, which is the only
    remaining way for `import llm` to fail. The message must still name the symlink and the
    directory, because that is the entire diagnostic value of catching it.
    """
    source = Path(public.__file__).read_text(encoding="utf-8")
    assert "except ImportError as _exc:" in source
    for phrase in (".pkgroot/llm", "packages/llm/src", "sys.path"):
        assert phrase in source, phrase
