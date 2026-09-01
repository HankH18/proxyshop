"""Sibling ``_fixtures_*.py`` auto-discovery. Orchestrator-owned (T-000), frozen.

Every per-directory ``conftest.py`` this repo ships is orchestrator-owned and frozen, so a
worker must never need to edit one to add a fixture. Instead each worker drops a file it
owns next to the conftest::

    apps/trust/tests/_fixtures_ledger.py     # owned by the ledger ticket

and this loader pulls every ``@pytest.fixture`` out of it into the conftest's namespace,
where pytest picks it up exactly as if it had been written there.

Why not ``pytest_plugins``: pytest forbids ``pytest_plugins`` in a non-rootdir conftest
("Defining 'pytest_plugins' in a non-top-level conftest is no longer supported"), so the
usual plugin-registration route is unavailable here.

Usage, verbatim, at the top level of a per-directory ``conftest.py``::

    from proxyshop_support.fixture_loader import load_sibling_fixtures

    globals().update(load_sibling_fixtures(__file__))
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

#: Files matching this glob, next to the importing conftest, are scanned.
FIXTURE_GLOB = "_fixtures_*.py"


def _is_pytest_fixture(obj: Any) -> bool:
    """Is ``obj`` the product of ``@pytest.fixture``?

    Deliberately version-tolerant. Up to pytest 8.3 the decorator returned the original
    function carrying a ``_pytestfixturefunction`` attribute; from pytest 8.4 (and in the
    9.1.1 pinned here) it returns a ``FixtureFunctionDefinition`` object instead. Checking
    only the old attribute silently discovers **nothing** on modern pytest — the failure
    mode is a "fixture not found" error in whichever ticket added the file, with no hint
    that the loader was the problem — so both shapes are recognised, via pytest's own
    marker accessor where it exists.
    """
    if hasattr(obj, "_pytestfixturefunction"):
        return True
    try:
        from _pytest.fixtures import getfixturemarker
    except ImportError:  # pragma: no cover - pytest is always installed here
        return False
    return getfixturemarker(obj) is not None


def load_sibling_fixtures(conftest_file: str | Path, *, glob: str = FIXTURE_GLOB) -> dict[str, Any]:
    """Import every ``_fixtures_*.py`` beside ``conftest_file`` and return its fixtures.

    Args:
        conftest_file: pass ``__file__`` from the calling ``conftest.py``.
        glob: override the discovery pattern. Defaults to :data:`FIXTURE_GLOB`.

    Returns:
        ``{fixture_name: fixture_function}`` for every ``@pytest.fixture`` found, plus any
        ``pytest_*`` hook functions defined in those modules. Feed it to
        ``globals().update(...)``.

    Raises:
        RuntimeError: two discovered modules define the same fixture name. Silently letting
            one shadow the other is how a worker's fixture mysteriously stops applying.
    """
    directory = Path(conftest_file).resolve().parent
    collected: dict[str, Any] = {}
    origin: dict[str, str] = {}

    for path in sorted(directory.glob(glob)):
        module_name = f"_proxyshop_fixtures.{directory.name}.{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise RuntimeError(f"cannot load fixture module {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        for name in dir(module):
            if name.startswith("__"):
                continue
            obj = getattr(module, name)
            if not (_is_pytest_fixture(obj) or (name.startswith("pytest_") and callable(obj))):
                continue
            if name in collected:
                raise RuntimeError(
                    f"fixture {name!r} is defined in both {origin[name]} and {path.name}; "
                    f"rename one — pytest would otherwise silently use only the last."
                )
            collected[name] = obj
            origin[name] = path.name

    return collected
