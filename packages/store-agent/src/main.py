"""FastAPI entrypoint for ``proxyshop-store-agent``.

Orchestrator-owned (T-000) and **frozen** — B6(iii): no worker edits this file. A feature
ticket adds its routes inside the subdirectory it already owns::

    packages/store-agent/src/<feature>/routes.py     ->  must export a module-level `router`

and :func:`create_app` finds it. Discovery is a filesystem glob evaluated at import time,
so a route module added after ``uv sync`` needs no re-sync, no manifest edit and no change
here.

Import namespace: ``store_agent`` (this directory is ``packages/store-agent/src``, reached through the tracked
symlink ``.pkgroot/store_agent`` — see the member ``pyproject.toml`` for why the layout is flat).
"""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi import FastAPI

PACKAGE = __package__ or "store_agent"
TITLE = "proxyshop-store-agent"


def discover_router_modules() -> list[str]:
    """Return the dotted names of every ``<feature>/routes.py`` beside this file, sorted."""
    here = Path(__file__).resolve().parent
    return [f"{PACKAGE}.{path.parent.name}.routes" for path in sorted(here.glob("*/routes.py"))]


def create_app() -> FastAPI:
    """Build the application, mounting every discovered feature router.

    A feature module that has no ``router`` attribute is skipped rather than crashing the
    service, so a half-finished ticket cannot take the whole app down.
    """
    app = FastAPI(title=TITLE)
    mounted: list[str] = []
    for module_name in discover_router_modules():
        module = importlib.import_module(module_name)
        router = getattr(module, "router", None)
        if router is None:
            continue
        app.include_router(router)
        mounted.append(module_name)
    app.state.mounted_routers = mounted
    return app


app = create_app()
