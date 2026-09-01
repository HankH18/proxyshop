"""Per-directory pytest fixtures for ``services/ingest`` — plus the Neo4j lock (D37).

Orchestrator-owned (T-000) and **frozen** — no worker edits this file. To add a fixture,
create a file you own next to this one::

    services/ingest/tests/_fixtures_<topic>.py

and define ordinary ``@pytest.fixture`` functions in it. Everything in every
``_fixtures_*.py`` here is loaded into this conftest's namespace automatically.

This directory is one of the two Neo4j lanes, so it **overrides** the root
``_neo4j_guard`` fixture with one that holds the cross-worker ``flock`` on
``/tmp/proxyshop-neo4j.lock`` for the whole session and performs the database reset
*inside* the lock (D37: isolation is scheduler serialization **plus** the flock, because
Neo4j Community has a single database that every worker would otherwise share).
"""

from collections.abc import Iterator

import pytest

from proxyshop_support.fixture_loader import load_sibling_fixtures
from proxyshop_support.neo4j_lock import neo4j_flock

globals().update(load_sibling_fixtures(__file__))


@pytest.fixture(scope="session")
def _neo4j_guard() -> Iterator[None]:
    """Hold the D37 flock for this session; reset the graph inside it.

    The reset is deliberately a no-op until there is a driver to reset with: it runs lazily
    the first time :func:`neo4j_driver` is built, which is itself gated on the compose
    stack being reachable. Holding the lock costs nothing when no graph test runs.
    """
    with neo4j_flock():
        yield None
