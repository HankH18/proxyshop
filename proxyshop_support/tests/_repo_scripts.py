"""Load a module out of the repo's frozen ``scripts/`` directory BY PATH (T-252).

Why not ``sys.path.insert(0, ...)`` plus a bare ``import``
---------------------------------------------------------
Because a bare top-level name is not an identity. ``import check_verify_contracts`` asks
the import system for whatever currently answers to that name, and three things beat a
freshly inserted ``sys.path`` entry, all measured in this repo:

* an entry already in ``sys.modules`` — the ``insert(0)`` is not even consulted, and in a
  whole-repo run some earlier test may well have bound the name;
* a ``sys.meta_path`` finder, which runs BEFORE ``sys.path`` is looked at at all;
* a file of the same name at a ``sys.path`` position that also happens to be 0.

Measured on the HEAD-shaped idiom with all three present: ``gate.__file__`` came back as
the decoy's, and the two tests that grade
``scripts/check_verify_contracts.py`` — a FROZEN file — silently graded something else and
reported an answer that was not the frozen script's. The same three hijacks against
:func:`load_repo_script` resolve to the real path.

That is T-252's class ("a name-based import that silently starts grading the repo instead
of the tree it means to"), and the answer is the one
:func:`proxyshop_support.fixture_loader.load_sibling_fixtures` already uses: build the spec
from an explicit file PATH, under a namespaced module name that collides with nothing.

This lives under ``tests/`` deliberately. ``proxyshop_support/`` is COPY'd into five
container images and ``shipped_sources`` excludes ``**/tests/**``; a shipped module that
reaches back into the checkout's ``scripts/`` directory would be dead weight in every image
and a new import site for the artifact checker to reason about.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"

#: Prefix for the module names this creates. Namespaced so nothing in the checkout, in
#: ``.pkgroot`` or in site-packages can ever answer to one of them.
MODULE_PREFIX = "_proxyshop_repo_scripts"


def load_repo_script(name: str) -> ModuleType:
    """Import ``scripts/<name>.py`` by path, whatever else claims the bare name.

    Args:
        name: the script's stem, e.g. ``"check_verify_contracts"``. ONE path component —
            see the containment check below.

    Returns:
        The loaded module. Its ``__file__`` is always ``scripts/<name>.py`` under this
        checkout — assert on that if you need proof rather than trust.

    Raises:
        ValueError: ``name`` is not a single path component under ``scripts/``. This
            helper's whole promise is "the frozen file at ``scripts/<name>.py``", and a
            name that traverses is a name that breaks the promise silently.
        FileNotFoundError: there is no such script. A missing frozen gate script is a
            finding, not something to paper over with a fallback import.
    """
    # CONTAINMENT. `name` is interpolated into a path and the result is EXECUTED, so a
    # traversing name is arbitrary code, not a bad lookup: measured before this check
    # existed, `load_repo_script("../conftest")` resolved `scripts/../conftest.py`,
    # exec'd the ROOT conftest, and returned it bound under the module name
    # `_proxyshop_repo_scripts.../conftest`. Every call site today passes a literal, so
    # nothing abused it — but "no caller does this yet" is not a guard.
    #
    # Two independent conditions, because either alone is easy to argue around: the stem
    # must be a single path component, and the file it names must land directly inside
    # SCRIPTS once symlinks are resolved.
    # ``Path("..").name`` is ``".."`` and ``Path(".").name`` is ``""``, so the component
    # test alone lets ``".."`` through to ``scripts/...py`` — contained, but nonsense.
    # Naming both explicitly keeps the refusal about intent rather than about pathlib.
    if not name or name in {".", ".."} or name != Path(name).name:
        raise ValueError(
            f"load_repo_script({name!r}): a script name is ONE path component (a stem like "
            f"'check_verify_contracts'), not a path. This name is interpolated into "
            f"{SCRIPTS}/<name>.py and the result is executed, so a traversing name would "
            f"run a file this helper never promised to load."
        )
    path = SCRIPTS / f"{name}.py"
    if path.resolve().parent != SCRIPTS.resolve():
        raise ValueError(
            f"load_repo_script({name!r}): {path} resolves to {path.resolve()}, which is not "
            f"directly inside {SCRIPTS.resolve()}."
        )
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} does not exist. This helper deliberately has no bare-import fallback: "
            f"falling back is exactly how a test starts grading a different file (T-252)."
        )
    qualified = f"{MODULE_PREFIX}.{name}"
    spec = importlib.util.spec_from_file_location(qualified, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"could not build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered under the NAMESPACED name only. The bare name is never touched, so this
    # neither reads nor pollutes whatever else is using it.
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module
