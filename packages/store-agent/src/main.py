"""FastAPI entrypoint for ``proxyshop-store-agent``.

Orchestrator-owned (T-000) and **frozen** — B6(iii): no worker edits this file. A feature
ticket adds its routes inside the subdirectory it already owns::

    packages/store-agent/src/<feature>/routes.py     ->  must export a module-level `router`

and :func:`create_app` finds it. Discovery is a filesystem glob evaluated at import time,
so a route module added after ``uv sync`` needs no re-sync, no manifest edit and no change
here.

Import namespace: ``store_agent`` (this directory is ``packages/store-agent/src``, reached through the tracked
symlink ``.pkgroot/store_agent`` — see the member ``pyproject.toml`` for why the layout is flat).

Application logging is configured here (T-308), by orchestrator direction and as the single
exception to the freeze above, because this file *is* this service's whole in-process startup
path: ``uvicorn``, ``proxyshop_support.asgi_server`` and the module-level ``app`` at the bottom
all reach the application through :func:`create_app`. Before it, a started process had
``root.handlers == []`` at effective level ``WARNING``, so every ``_log.info`` in the package
was dropped before a record was constructed.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

from fastapi import FastAPI

from proxyshop_support.logging_config import RequestIdMiddleware, configure_logging

PACKAGE = __package__ or "store_agent"
TITLE = "proxyshop-store-agent"

#: This module's logger. Named ``store_agent.main`` by ``__name__``, which is the name an operator
#: greps for to learn what a started process is actually serving, and the name a deployment
#: raises or silences on its own without touching the rest of the package.
_log = logging.getLogger(__name__)


def discover_router_modules() -> list[str]:
    """Return the dotted names of every ``<feature>/routes.py`` beside this file, sorted."""
    here = Path(__file__).resolve().parent
    return [f"{PACKAGE}.{path.parent.name}.routes" for path in sorted(here.glob("*/routes.py"))]


def create_app() -> FastAPI:
    """Build the application, mounting every discovered feature router.

    A feature module that has no ``router`` attribute is skipped rather than crashing the
    service, so a half-finished ticket cannot take the whole app down. That skip is reported
    now: a route module that is present on disk and serves nothing is indistinguishable, from
    outside, from a route module nobody ever wrote, and the resulting 404s look like a routing
    bug rather than a half-landed ticket.

    :func:`~proxyshop_support.logging_config.configure_logging` is called first, before the
    application exists, so that anything the router imports below can log while it is being
    imported. It is idempotent, so a process that reaches this factory twice — the module-level
    ``app`` plus an explicit ``create_app()`` in a test — installs one handler, not two.
    """
    configure_logging()
    app = FastAPI(title=TITLE)
    # Mints or adopts one correlation id per request and echoes it in `x-request-id`, so a
    # single request can be followed across services by grepping one id. An incoming header
    # is sanitised, never adopted verbatim; see `logging_config.sanitise_request_id`.
    app.add_middleware(RequestIdMiddleware)
    mounted: list[str] = []
    skipped: list[str] = []
    for module_name in discover_router_modules():
        module = importlib.import_module(module_name)
        router = getattr(module, "router", None)
        if router is None:
            skipped.append(module_name)
            continue
        app.include_router(router)
        mounted.append(module_name)
    app.state.mounted_routers = mounted
    # Both lines interpolate module names derived from this package's own directory layout,
    # never from a request, so there is nothing caller-controlled or unbounded in them.
    _log.info(
        "%s: %d feature router(s) mounted: %s",
        TITLE,
        len(mounted),
        ", ".join(mounted) or "none",
    )
    if skipped:
        _log.warning(
            "%s: %d discovered route module(s) export no `router` and serve nothing: %s",
            TITLE,
            len(skipped),
            ", ".join(skipped),
        )
    return app


app = create_app()
