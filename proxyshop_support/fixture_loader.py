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

Duplicate fixture names — why they do NOT raise here
----------------------------------------------------
Seven tickets share ``apps/trust/tests/`` and there is no reserved-name registry, so two of
them picking ``ledger_row`` for different fixtures is a matter of time, not of luck. This
loader used to ``raise RuntimeError`` on that collision. The raise happened at **conftest
import time**, which is before pytest has collected anything — so the seventh agent to
choose a taken name did not fail its own test, it failed *every* test in the directory,
including every already-merged ticket's, with a traceback that pointed at a conftest none
of them had touched. That is the worst possible blast radius for the cheapest possible
mistake.

So a collision is now **local and lazy**: the name is bound to a poisoned fixture that
raises :class:`DuplicateFixtureError` naming both defining files *when a test actually asks
for it*. Only tests that use the ambiguous name fail; the directory's other tests run
normally. It is still not silent —

* a :class:`DuplicateFixtureWarning` is emitted at import, so it lands in pytest's warnings
  summary on every run; and
* ``scripts/check_verify_contracts.py`` scans sibling ``_fixtures_*.py`` files statically
  and **fails the gate**, so the ticket that introduced the duplicate goes red on its own
  ``make check`` and nobody else's suite is harmed.

Hook functions (``pytest_*``) cannot be poisoned lazily — a hook that raises when pluggy
calls it takes the session down, which is the blast radius this design exists to avoid — so
a duplicate hook keeps the first definition, warns, and is caught fatally by the same static
gate.
"""

from __future__ import annotations

import importlib.util
import sys
import warnings
from pathlib import Path
from typing import Any

# `pytest` is deliberately NOT imported at module scope. `COPY proxyshop_support/` puts this
# file into SIX images — buyer, exchange, merchant, trust, ingest and sim — and none of them
# installs pytest, so a column-0 `import pytest` here made every one of those artifacts ship
# a module that cannot be imported — measured by
# `proxyshop_support/tests/test_artifact_copyset.py::test_t301_every_shipped_module_imports_inside_the_container_shaped_tree`,
# which walls the probe off from the dev venv precisely so this is visible. Nothing here
# needs pytest until :func:`_poisoned_fixture` actually builds a fixture, which only happens
# under a pytest session, so the import moves there. Behaviour under pytest is unchanged;
# the difference is that importing this module no longer requires pytest to be installed.

#: Files matching this glob, next to the importing conftest, are scanned.
FIXTURE_GLOB = "_fixtures_*.py"


class DuplicateFixtureError(RuntimeError):
    """Two sibling ``_fixtures_*.py`` files define the same fixture name.

    Raised when a test *requests* the ambiguous name — never at import time, so the
    collision cannot take down the rest of the directory. See the module docstring.
    """


class DuplicateFixtureWarning(UserWarning):
    """Import-time notice that a fixture name is defined twice in one directory."""


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


def duplicate_message(name: str, paths: list[str]) -> str:
    """The one message both the warning and the poisoned fixture use.

    Args:
        name: the fixture name defined more than once.
        paths: every defining file, repo-relative where that could be worked out, in
            discovery order. The first entry is the definition that got there first.
    """
    listing = "\n".join(f"      - {path}" for path in paths)
    return (
        f"fixture {name!r} is defined {len(paths)} times in the same test directory:\n"
        f"{listing}\n"
        f"    pytest can only have one, so this name resolves to none of them. Rename all "
        f"but one — a `_fixtures_*.py` file is owned by a single ticket, so prefix the "
        f"name with your ticket's topic (e.g. `ledger_{name}`) and nothing else has to "
        f"change. Only tests that request {name!r} fail; the rest of this directory is "
        f"unaffected. `scripts/check_verify_contracts.py` fails the gate on this, so it "
        f"must be fixed rather than tolerated."
    )


def _poisoned_fixture(name: str, paths: list[str]) -> Any:
    """A real pytest fixture that raises, naming both definitions, only when requested."""
    import pytest  # local: see the note where the module-scope import used to be

    message = duplicate_message(name, paths)

    @pytest.fixture(name=name)
    def _duplicate() -> Any:
        raise DuplicateFixtureError(message)

    return _duplicate


def _relative(path: Path) -> str:
    """``path`` relative to the repo root when that is knowable, else the full path."""
    root = Path(__file__).resolve().parents[1]
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:  # pragma: no cover - a fixture file outside the repo (tmp_path)
        return str(path)


def load_sibling_fixtures(conftest_file: str | Path, *, glob: str = FIXTURE_GLOB) -> dict[str, Any]:
    """Import every ``_fixtures_*.py`` beside ``conftest_file`` and return its fixtures.

    Args:
        conftest_file: pass ``__file__`` from the calling ``conftest.py``.
        glob: override the discovery pattern. Defaults to :data:`FIXTURE_GLOB`.

    Returns:
        ``{fixture_name: fixture_function}`` for every ``@pytest.fixture`` found, plus any
        ``pytest_*`` hook functions defined in those modules. Feed it to
        ``globals().update(...)``.

        A name defined by two or more of those files maps to a poisoned fixture that raises
        :class:`DuplicateFixtureError` when a test requests it. Nothing raises at import
        time: a collision must not take down the tests of every other ticket sharing the
        directory.
    """
    directory = Path(conftest_file).resolve().parent
    collected: dict[str, Any] = {}
    origins: dict[str, list[str]] = {}
    hook_names: set[str] = set()

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
            is_hook = name.startswith("pytest_") and callable(obj)
            if not (_is_pytest_fixture(obj) or is_hook):
                continue
            if name in origins:
                origins[name].append(_relative(path))
                continue
            origins[name] = [_relative(path)]
            if is_hook:
                hook_names.add(name)
            collected[name] = obj

    for name, paths in origins.items():
        if len(paths) == 1:
            continue
        warnings.warn(duplicate_message(name, paths), DuplicateFixtureWarning, stacklevel=2)
        if name not in hook_names:
            # Hooks keep the first definition: a hook that raises when pluggy calls it
            # would take the whole session down, which is the blast radius this avoids.
            collected[name] = _poisoned_fixture(name, paths)

    return collected
