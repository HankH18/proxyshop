"""The `.pkgroot` fallback in `packages/contracts/__init__.py`, and one module per file.

Two defects lived here at once and neither was visible from inside a normal pytest run,
because a normal pytest run already has `.pkgroot` on `sys.path` from the venv's editable
install. Both tests below deliberately remove that help.

1. The fallback pointed at `parents[1]` (`packages/`) instead of `parents[2]` (the repo
   root), so `_PKGROOT.is_dir()` was permanently False, the `sys.path` insertion never ran,
   and `import packages.contracts` raised `ModuleNotFoundError: No module named 'contracts'`
   the moment the venv's `.pth` was not doing the work. The frozen acceptance suite runs
   pytest with `-o pythonpath=` on purpose, so it is exactly that consumer.

2. `contracts.signing` and `packages.contracts.src.signing` were two separately-executed
   module objects with two distinct `CanonicalisationError` classes, so an `except` written
   against one spelling did not catch the other spelling's raise. Both spellings are live in
   this tree — `test_signing_envelope.py` imports the `packages.contracts.src` one.
"""

from __future__ import annotations

import os
import pkgutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import contracts as flat
import packages.contracts as dotted

REPO_ROOT = Path(__file__).resolve().parents[3]


def _hostile_env_import(statements: str) -> subprocess.CompletedProcess[str]:
    """Run `statements` in an interpreter that has NO `.pkgroot` help of any kind.

    `-S` skips `site`, so no `.pth` file is processed and the venv's `_proxyshop.pth` —
    which is what silently made the broken fallback look fine — contributes nothing. The
    site-packages directory itself is handed back through `PYTHONPATH` so third-party
    dependencies (pydantic, …) still import; otherwise this would prove only that `-S`
    hides pydantic, not that the fallback works. That leaves precisely the situation the
    fallback exists for: the repo root on the path, and nothing else about this repo.
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


def test_the_repo_root_is_where_pkgroot_actually_lives_not_the_packages_dir() -> None:
    """The off-by-one, stated as the fact it got wrong."""
    here = Path(dotted.__file__).resolve()
    assert not (here.parents[1] / ".pkgroot").is_dir(), (
        "parents[1] is packages/, and a .pkgroot there would make this test vacuous"
    )
    assert (here.parents[2] / ".pkgroot").is_dir(), "parents[2] is the repo root"
    assert dotted._PKGROOT == REPO_ROOT / ".pkgroot"


def test_packages_contracts_imports_with_only_the_repo_root_on_the_path() -> None:
    proc = _hostile_env_import(
        "import sys\n"
        "assert not [p for p in sys.path if p.endswith('.pkgroot')], sys.path\n"
        "import packages.contracts as c\n"
        "print(c.Bid.__name__)\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "Bid"


def test_packages_llm_imports_with_only_the_repo_root_on_the_path() -> None:
    """The sibling package had no fallback at all; it is fixed the same way."""
    proc = _hostile_env_import(
        "import sys\n"
        "assert not [p for p in sys.path if p.endswith('.pkgroot')], sys.path\n"
        "import packages.llm as m\n"
        "print(m.RecordedLLM.__name__)\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "RecordedLLM"


def test_signing_is_one_module_object_under_both_spellings() -> None:
    import contracts.signing
    import packages.contracts.src.signing

    assert contracts.signing is packages.contracts.src.signing
    assert contracts.signing is sys.modules["packages.contracts.signing"]


def test_boundary_is_one_module_object_under_both_spellings() -> None:
    import contracts.boundary
    import packages.contracts.src.boundary

    assert contracts.boundary is packages.contracts.src.boundary


def test_canonicalisation_error_is_one_class_across_both_spellings() -> None:
    """A cross-spelling `except CanonicalisationError` must actually catch."""
    from contracts.signing import CanonicalisationError as ViaFlat
    from packages.contracts.src.signing import CanonicalisationError as ViaDotted

    assert ViaFlat is ViaDotted
    try:
        raise ViaFlat("raised through the flat spelling")
    except ViaDotted:
        pass
    else:  # pragma: no cover - only reachable if the classes diverge again
        raise AssertionError("the dotted spelling's except did not catch the flat raise")


def test_every_submodule_in_src_binds_to_one_object_not_just_the_two_named_above() -> None:
    """Discovered with pkgutil, so a module added to `src/` later cannot miss the binding."""
    import importlib

    names = [info.name for info in pkgutil.iter_modules(flat.__path__)]
    assert "signing" in names and "boundary" in names, names
    for name in names:
        canonical = importlib.import_module(f"contracts.{name}")
        for spelling in (f"packages.contracts.{name}", f"packages.contracts.src.{name}"):
            assert importlib.import_module(spelling) is canonical, spelling
        assert getattr(dotted, name) is canonical, name
