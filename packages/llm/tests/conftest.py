"""Per-directory pytest fixtures for ``packages/llm``.

Orchestrator-owned (T-000) and **frozen** — no worker edits this file. To add a fixture,
create a file you own next to this one::

    packages/llm/tests/_fixtures_<topic>.py

and define ordinary ``@pytest.fixture`` functions in it. Everything in every
``_fixtures_*.py`` here is loaded into this conftest's namespace automatically, so pytest
sees your fixtures exactly as if they had been written in this file.

A file that redefines a name another ``_fixtures_*.py`` here already defined is reported
loudly — but only *that* fixture is poisoned, and only when a test asks for it. It cannot
take this directory's other tests down at import time; see
``proxyshop_support.fixture_loader``.

Neo4j needs nothing from this file: the root ``conftest.py``'s ``_neo4j_guard`` takes the
D37 cross-worker flock for every session that touches the graph, from any directory.
"""

from proxyshop_support.fixture_loader import load_sibling_fixtures

globals().update(load_sibling_fixtures(__file__))
