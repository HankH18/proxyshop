"""Standing the ledger writer up as an application. Owned by T-060.

Two ways in, and they are not alternatives:

* :func:`trust.main.create_app` is how the service runs. It globs
  ``apps/trust/src/*/routes.py`` and mounts :data:`trust.events.routes.router` alongside
  every other trust feature. That file is frozen and knows nothing about this ticket.
* :func:`create_events_app` is how the writer is *tested* and how another process embeds
  just the ledger. It mounts the same router on an application of its own and lets the
  caller inject the store.

The injection is what makes "the chain continues across a restart" testable at all. Two
applications built here have two stores and share no memory, so an assertion that the
second continues the first's chain is an assertion about the *ledger*, not about a variable
that happened to survive.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from .routes import router

__all__ = ["TITLE", "create_events_app"]

TITLE = "proxyshop-trust-events"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Release the store's connection resources when the application stops.

    A store that opened a pool and never closed it leaks a handful of Postgres backends per
    server, which in a test suite that stands up an application per test is how a run ends
    with ``FATAL: sorry, too many clients already`` several hundred tests after the fault.
    """
    try:
        yield
    finally:
        store = getattr(app.state, "event_store", None)
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()


def create_events_app(store: Any | None = None, *, title: str = TITLE) -> FastAPI:
    """An application serving only the ledger writer.

    Args:
        store: the event store to write to -- an :class:`~.store.InMemoryEventStore`, a
            :class:`~.pg.PostgresEventStore`, or anything with the same surface. When
            omitted the routes fall back to a Postgres store built from the environment,
            resolved lazily on the first request so that constructing the app needs no
            database.
        title: OpenAPI title.
    """
    app = FastAPI(title=title, lifespan=_lifespan)
    app.include_router(router)
    if store is not None:
        app.state.event_store = store
    return app
