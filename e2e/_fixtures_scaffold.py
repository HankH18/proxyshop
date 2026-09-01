"""A worker-owned fixture file, in the shape every ticket will use.

Orchestrator-owned (T-000) and **frozen**. This exists to keep the ``_fixtures_*.py``
auto-discovery mechanism *proved* rather than assumed: ~40 tickets add their fixtures this
way instead of editing a shared conftest, and if the loader silently stopped working every
one of them would fail in a confusing place. ``e2e/test_scaffold_smoke.py`` asserts that the
fixture below is visible to tests, which only happens if ``e2e/conftest.py`` loaded it.

Copy this shape: a file named ``_fixtures_<topic>.py`` next to a ``conftest.py``, containing
ordinary ``@pytest.fixture`` functions. Nothing else is needed.
"""

import pytest


@pytest.fixture
def scaffold_fixture_probe() -> str:
    """Returns a sentinel proving sibling `_fixtures_*.py` discovery is wired up."""
    return "loaded-from-_fixtures_scaffold"
