"""Fixtures for the T-081 simulation tests.

Every name here is prefixed ``sim_``: this directory's ``conftest.py`` hoists every
``_fixtures_*.py`` into one namespace, and a name two files both define is poisoned for
whichever test asks for it (``proxyshop_support.fixture_loader``). Prefixing is what keeps a
second ticket's fixtures from colliding with these.

The full run is session-scoped because it is a *pure function* of ``(manifest, seed)`` — the
whole point of T-081 acceptance 1 — so recomputing it per test would only prove the same
thing more slowly. Anything that needs to see the run differ builds its own.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(scope="session")
def sim_manifest() -> dict[str, Any]:
    """The human-approved manifest, digest chain verified on the way in."""
    from fixtures.manifest import load_manifest

    return load_manifest()


@pytest.fixture(scope="session")
def sim_run(sim_manifest: dict[str, Any]) -> Any:
    """One full simulation over the approved episode budget, on the approved seed."""
    from sim.runner import run_simulation

    return run_simulation(sim_manifest, int(sim_manifest["seed"]))
