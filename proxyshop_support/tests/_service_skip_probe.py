"""Docker-marked probe tests, run only in a subprocess by ``test_reachability_per_service``.

The filename deliberately does not match ``python_files`` (``test_*.py`` / ``*_test.py``),
so the ordinary repo-wide run never collects these. ``test_reachability_per_service.py``
passes this file to pytest **explicitly** — an init-path is collected regardless of
``python_files`` — with the datastore URLs pointed at controlled ports, and then reads each
probe's outcome out of a JUnit XML report.

Why a subprocess rather than in-process stubs: the thing under test is the *root*
``conftest.py``'s ``pytest_collection_modifyitems`` skip decision. A stub item exercising a
copy of that logic would stay green if the real hook were deleted. A real pytest session
making a real decision is the only honest observation.

Each probe body is empty on purpose — the probes with an explicit service argument must not
touch a datastore, because the ports they are pointed at are bare listening sockets with
nothing behind them. ``test_probe_needs_a_postgres_fixture`` is the one exception and is
selected (``-k fixture``) only by the test that runs against the real compose stack.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.docker("postgres")
def test_probe_postgres_only() -> None:
    """Skips only when Postgres is unreachable."""


@pytest.mark.docker("neo4j-bolt")
def test_probe_neo4j_only() -> None:
    """Skips only when the Neo4j bolt port is unreachable."""


@pytest.mark.docker("redis")
def test_probe_redis_only() -> None:
    """Skips only when Redis is unreachable."""


@pytest.mark.docker
def test_probe_whole_stack() -> None:
    """No service named and no datastore fixture requested: the whole stack is required."""


@pytest.mark.docker
def test_probe_needs_a_postgres_fixture(pg_role: Any) -> None:
    """A bare ``docker`` mark whose service is inferred from the fixtures it requests.

    This is the shape every already-merged datastore test in the repo has (T-011's S7
    role-isolation gate included): ``@pytest.mark.docker`` with no argument, plus a
    Postgres fixture. It must survive a Redis outage.
    """
    with pg_role("app").cursor() as cur:
        cur.execute("select 1")
        assert cur.fetchone() == (1,)
