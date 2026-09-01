"""Per-directory pytest fixtures for ``apps/exchange`` — plus the Neo4j lock (D37).

Orchestrator-owned (T-000) and **frozen** — no worker edits this file. To add a fixture,
create a file you own next to this one::

    apps/exchange/tests/_fixtures_<topic>.py

and define ordinary ``@pytest.fixture`` functions in it. Everything in every
``_fixtures_*.py`` here is loaded into this conftest's namespace automatically.

This directory is one of the two Neo4j lanes, so it **overrides** the root
``_neo4j_guard`` fixture with one that holds the cross-worker ``flock`` on
``/tmp/proxyshop-neo4j.lock`` for the whole session (D37: isolation is scheduler
serialization **plus** the flock, because Neo4j Community has a single database that every
worker would otherwise share). Yielding ``True`` tells ``neo4j_driver`` that this session
owns the graph exclusively, so it performs the reset *inside* the lock.

A whole-repo run collects **both** graph lanes, so both of these session guards are
instantiated in one interpreter. ``neo4j_flock`` is re-entrant within a process for
exactly that reason — see ``proxyshop_support.neo4j_lock``.
"""

from collections.abc import Iterator

import pytest

from proxyshop_support.fixture_loader import load_sibling_fixtures
from proxyshop_support.neo4j_lock import neo4j_flock

globals().update(load_sibling_fixtures(__file__))


@pytest.fixture(scope="session")
def _neo4j_guard() -> Iterator[bool]:
    """Hold the D37 flock for this session and claim exclusive ownership of the graph.

    Yields ``True``, which is what makes ``neo4j_driver`` run :func:`reset_graph` inside
    the lock the first time it is built (itself gated on the compose stack being
    reachable). Holding the lock costs nothing when no graph test runs, and re-entering it
    from the other graph lane in the same interpreter is free.
    """
    with neo4j_flock():
        yield True
