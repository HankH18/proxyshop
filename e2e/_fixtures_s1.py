"""The S1 starting-path run, driven once per session (T-082).

Auto-loaded into ``e2e/conftest.py`` by ``proxyshop_support.fixture_loader``; that conftest
is orchestrator-owned and frozen, so this file is where the e2e lane's fixtures live. Every
name is prefixed ``s1_`` so it cannot collide with another ``_fixtures_*.py`` in this
directory — the loader poisons a duplicated fixture name rather than silently picking one.

The run is session-scoped because it is the expensive thing in this lane: it stands up the
exchange app, the shopify-stub and two recording receivers on loopback sockets. Every test
in ``test_s1_flow.py`` reads the same pass, which is also what "ONE scripted run proves the
full S1 flow" means — twenty tests over twenty runs would prove twenty different things.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(scope="session")
def s1_run() -> Any:
    """One complete pass through the S1 starting path. See ``e2e/support/s1/flow.py``."""
    from e2e.support.s1.flow import run_s1_flow

    return run_s1_flow()


@pytest.fixture(scope="session")
def s1_fixture() -> dict[str, Any]:
    """The run's scenario document, read straight from ``e2e/support/s1/run.json``."""
    from e2e.support.s1.flow import load_run_fixture

    return load_run_fixture()
