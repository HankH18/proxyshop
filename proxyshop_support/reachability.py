"""Non-blocking datastore probes. Orchestrator-owned (T-000), frozen.

Used by the root ``conftest.py`` to *skip* ``@pytest.mark.docker`` tests with an explicit
message when the compose stack is not up. The hard requirement is that it **never hangs**:
a hung probe in an unattended overnight run is indistinguishable from a hung build, so
every probe is a bounded TCP connect with an explicit timeout.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 1.0


@dataclass(frozen=True)
class Endpoint:
    """A ``(name, host, port)`` the compose stack is expected to publish."""

    name: str
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.name} ({self.host}:{self.port})"


def _split(url: str | None, default_host: str, default_port: int) -> tuple[str, int]:
    if not url:
        return default_host, default_port
    parsed = urlparse(url)
    return (parsed.hostname or default_host), (parsed.port or default_port)


def compose_endpoints() -> list[Endpoint]:
    """The three datastore endpoints, read from the environment with compose defaults."""
    pg_host, pg_port = _split(os.environ.get("PROXYSHOP_PG_DSN_ADMIN"), "localhost", 5432)
    neo_host, neo_port = _split(os.environ.get("NEO4J_URI"), "localhost", 7687)
    redis_host, redis_port = _split(os.environ.get("REDIS_URL"), "localhost", 6379)
    return [
        Endpoint("postgres", pg_host, pg_port),
        Endpoint("neo4j-bolt", neo_host, neo_port),
        Endpoint("redis", redis_host, redis_port),
    ]


def is_open(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """``True`` if a TCP connection to ``host:port`` completes within ``timeout``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def unreachable(timeout: float = DEFAULT_TIMEOUT) -> list[Endpoint]:
    """Return the compose endpoints that did not answer. Empty list means the stack is up."""
    return [e for e in compose_endpoints() if not is_open(e.host, e.port, timeout)]


def skip_reason(timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """A human-readable skip reason, or ``None`` when the whole stack is reachable."""
    down = unreachable(timeout)
    if not down:
        return None
    names = ", ".join(str(e) for e in down)
    return f"compose datastore stack is not reachable: {names}. Run `make deps-up` first."
