"""Per-directory pytest fixtures for ``e2e``.

Orchestrator-owned (T-000) and **frozen** — no worker edits this file. To add a fixture,
create a file you own next to this one::

    e2e/_fixtures_<topic>.py

and define ordinary ``@pytest.fixture`` functions in it. Everything in every
``_fixtures_*.py`` here is loaded into this conftest's namespace automatically, so pytest
sees your fixtures exactly as if they had been written in this file. Two files defining the
same fixture name is a loud error, not a silent shadow.
"""

from proxyshop_support.fixture_loader import load_sibling_fixtures

globals().update(load_sibling_fixtures(__file__))
