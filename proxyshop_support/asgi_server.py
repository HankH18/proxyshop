"""Ephemeral in-process ASGI servers (D40). Orchestrator-owned (T-000), frozen.

D40: *any server a test starts binds **port 0** and reports its real port through a
fixture. No hard-coded ports in test code.* This module is the one implementation of that
rule; the ``shopify_stub_url`` fixture in the root ``conftest.py`` is its first consumer.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import uvicorn


class ServerStartTimeout(RuntimeError):
    """The background ASGI server did not report a bound port in time."""


@contextmanager
def serve(app: Any, *, host: str = "127.0.0.1", timeout: float = 20.0) -> Iterator[str]:
    """Run ``app`` on an ephemeral port in a background thread.

    Args:
        app: any ASGI application.
        host: bind address. Stays on loopback so the pytest socket guard permits it.
        timeout: seconds to wait for the port to be bound.

    Yields:
        The base URL, e.g. ``"http://127.0.0.1:53412"`` — no trailing slash.

    Raises:
        ServerStartTimeout: the server never came up; the thread is left to die with the
            process rather than hanging the suite.
    """
    config = uvicorn.Config(app, host=host, port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="proxyshop-asgi", daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout
    port: int | None = None
    while time.monotonic() < deadline:
        if server.started and server.servers:
            sockets = server.servers[0].sockets
            if sockets:
                port = sockets[0].getsockname()[1]
                break
        time.sleep(0.02)
    if port is None:
        server.should_exit = True
        raise ServerStartTimeout(f"ASGI server did not bind a port within {timeout}s")

    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
