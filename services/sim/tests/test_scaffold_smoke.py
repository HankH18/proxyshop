"""Scaffold smoke test for ``services/sim``.

Orchestrator-owned (T-000) and **frozen**. Its only job is to make sure this workspace is
*collected and executed* rather than silently empty: B10 notes that `pytest <empty dir>`
exits 5 and the root verify maps that to 0, so without a real test here a broken workspace
would look identical to a green one.

The filename is deliberately not of the form a feature ticket would choose — every ticket
reserves the ``test_<topic>*.py`` prefix for its own topic, and ``scaffold`` is nobody's.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The directories this lane's ticket scope (`services/sim/**`, T-081) names, which
#: `pytest services/sim` must be able to collect from.
#:
#: This list was the empty literal `[]`, written inline in the loop below. A `for` over an
#: empty literal never enters its body, so the assertion never executed and the test reported
#: green unconditionally — it would have passed just as happily against a `services/sim` that
#: did not exist, which is the one thing it was written to catch. Recorded in
#: `.swarm-loop/findings.jsonl` as MEDIUM, in three copies; the other two
#: (`packages/verification/tests/` and `apps/seller-reference/tests/`) are outside this
#: lane's ownership and are reported, not touched. The loop and its assertion are unchanged:
#: only the iterable was empty, and now it is not.
SCOPE_DIRECTORIES = ("services/sim/src", "services/sim/tests")


def test_import_namespace_resolves() -> None:
    """``sim`` imports, and resolves to this member's FLAT ``src/`` directory."""
    module = importlib.import_module("sim")
    assert module.__file__ is not None
    resolved = Path(module.__file__).resolve().parent
    assert resolved == (REPO_ROOT / "services/sim/src").resolve()


def test_scope_directories_exist() -> None:
    """Every directory a ticket scope names is present, so `pytest <path>` cannot exit 4."""
    for relative in SCOPE_DIRECTORIES:
        assert (REPO_ROOT / relative).is_dir(), relative


@pytest.mark.docker
def test_the_sim_lane_holds_the_d37_neo4j_lock(_neo4j_guard) -> None:
    """D37: ``services/sim`` gets the same graph lock every other directory gets.

    ``sim`` drives whole-market simulations that read the graph the ingest lane builds. It
    had no ``conftest.py`` at all — so no ``_fixtures_*.py`` discovery either — and would
    have inherited the old ``False`` default, running unserialized against the single
    Community database (D4).

    No ``@pytest.mark.graph``: this asserts the guard, it does not write.
    """
    from proxyshop_support.neo4j_lock import held_depth

    assert _neo4j_guard is True
    assert held_depth() >= 1
