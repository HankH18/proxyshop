"""Structural guards on the scaffold's pytest wiring. Orchestrator-owned (T-000), frozen.

Two invariants that no linter sees and that only ever break silently:

1. **``_neo4j_guard`` is defined exactly once, in the root ``conftest.py``.** It used to
   default to ``False`` there and be overridden to ``True`` in two lane conftests. That is
   unsound because ``neo4j_driver`` — the fixture that consumes it — is *session*-scoped:
   pytest builds it once and caches it, so only the **first** requesting directory's guard
   is ever consulted, and every later lane silently inherits that answer whatever its own
   conftest says. A directory with no override winning that race disarms the lock for the
   whole session. One definition, no overrides, no race.
2. **Every member ``tests/`` directory (and ``e2e/``) has a conftest that loads sibling
   ``_fixtures_*.py``.** Six directories had none, so a ticket dropping the
   ``_fixtures_<topic>.py`` file the scaffold documentation tells it to write would have got
   "fixture not found" with no indication why.

Neither needs the compose stack; both run in ``make check``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Roots searched for ``tests/`` directories that must carry a fixture-loading conftest.
#: ``fixtures`` and ``docs`` have no such directory yet — T-080 and T-085 create them — and
#: are listed so those tickets are told to add the conftest instead of silently shipping a
#: directory where ``_fixtures_*.py`` does nothing.
MEMBER_ROOTS = ("apps", "packages", "services", "fixtures", "docs")

CONFTEST_TEMPLATE = (
    "from proxyshop_support.fixture_loader import load_sibling_fixtures\n\n"
    "globals().update(load_sibling_fixtures(__file__))"
)


def _conftests() -> list[Path]:
    found = [REPO_ROOT / "e2e" / "conftest.py"]
    for root in MEMBER_ROOTS:
        found.extend(sorted((REPO_ROOT / root).rglob("tests/conftest.py")))
    return [path for path in found if path.is_file()]


def _test_directories() -> list[Path]:
    """Every directory the scaffold expects worker-owned fixture files to land in."""
    found = [REPO_ROOT / "e2e"]
    for root in MEMBER_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("tests")):
            if path.is_dir() and any(p.name.startswith("test_") for p in path.glob("*.py")):
                found.append(path)
    return found


def test_the_neo4j_guard_is_defined_only_in_the_root_conftest() -> None:
    """No per-directory override may come back — see invariant 1 in the module docstring."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _conftests()
        if "def _neo4j_guard" in path.read_text()
    ]
    assert offenders == [], (
        f"{offenders} override `_neo4j_guard`. `neo4j_driver` is session-scoped and cached, "
        f"so only the first requesting directory's guard is consulted and the others are "
        f"silently ignored — including, if collection order changes, one that would have "
        f"taken the D37 flock. Delete the override; the root conftest takes the lock for "
        f"every session."
    )


def test_the_root_guard_takes_the_flock_unconditionally() -> None:
    """The root guard must actually hold the lock, not merely claim it."""
    source = (REPO_ROOT / "conftest.py").read_text()
    guard = source.split("def _neo4j_guard(")[1].split("\n@pytest.fixture")[0]
    assert "with neo4j_flock():" in guard, guard
    assert "yield True" in guard, guard
    assert "yield False" not in guard, guard


@pytest.mark.parametrize(
    "directory", _test_directories(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_every_test_directory_loads_sibling_fixture_files(directory: Path) -> None:
    """A `_fixtures_<topic>.py` dropped in any of these must actually be discovered."""
    conftest = directory / "conftest.py"
    relative = directory.relative_to(REPO_ROOT)
    assert conftest.is_file(), (
        f"{relative}/ has tests but no conftest.py, so the `_fixtures_*.py` mechanism every "
        f"ticket is told to use does not work there — a worker's fixtures would simply not "
        f"be found. Create {relative}/conftest.py containing exactly:\n\n"
        f"{CONFTEST_TEMPLATE}\n"
    )
    assert "load_sibling_fixtures(__file__)" in conftest.read_text(), relative
