"""Scaffold smoke test for ``apps/buyer/svc``.

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

from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_import_namespace_resolves() -> None:
    """``buyer_svc`` imports, and resolves to this member's FLAT ``src/`` directory."""
    module = importlib.import_module("buyer_svc")
    assert module.__file__ is not None
    resolved = Path(module.__file__).resolve().parent
    assert resolved == (REPO_ROOT / "apps/buyer/svc/src").resolve()


def test_app_starts_with_the_routers_it_discovers() -> None:
    """The frozen entrypoint builds an app and serves a schema.

    On the empty scaffold it discovers zero routers; as feature tickets land it discovers
    theirs. Either way the assertion that matters is the same one: the app starts.
    """
    main = importlib.import_module("buyer_svc.main")
    app = main.create_app()
    assert isinstance(app, FastAPI)
    assert isinstance(app.state.mounted_routers, list)
    assert app.openapi()["info"]["title"] == "proxyshop-buyer-svc"


def test_router_discovery_is_a_glob_not_a_hard_coded_list() -> None:
    """Every discovered name is ``buyer_svc.<feature>.routes`` — the contract workers rely on."""
    main = importlib.import_module("buyer_svc.main")
    for name in main.discover_router_modules():
        assert name.startswith("buyer_svc.")
        assert name.endswith(".routes")
