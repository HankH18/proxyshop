"""Non-blocking, **per-service** datastore probes. Orchestrator-owned (T-000).

Used by the root ``conftest.py`` to *skip* ``@pytest.mark.docker`` tests with an explicit
message when the compose stack is not up. Two hard requirements:

* **It never hangs.** A hung probe in an unattended overnight run is indistinguishable from
  a hung build, so every probe is a bounded TCP connect with an explicit timeout.
* **It answers about one service at a time (T-109).** The original
  ``skip_reason()`` took no service argument and reported the stack as down as soon as ANY
  of the three endpoints failed to answer. Measured consequence: with Postgres and Neo4j
  untouched and only ``REDIS_URL`` pointed at a closed port, all 47 ``@pytest.mark.docker``
  tests skipped — T-011's entire S7 role-isolation gate among them — and the run exited 0
  reporting ``63 passed, 47 skipped``. A one-second Redis blip could empty the security
  gate with no product cause and nothing red anywhere.

  So a caller now asks about the store it actually needs::

      skip_reason("postgres")               # None while Postgres answers, whatever Redis does
      skip_reason("postgres", "redis")      # the two of them
      skip_reason()                         # the whole stack, the pre-T-109 meaning

**Signature change, deliberately breaking.** ``skip_reason(0.2)`` used to mean "probe
everything with a 0.2 s timeout"; the first positional is now a service NAME and the
timeout is keyword-only. A stray float therefore raises :class:`ValueError` rather than
silently reverting to an all-or-nothing answer that reads like a per-service question.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 1.0

#: Every service name this module knows, in the order messages list them.
SERVICES: tuple[str, ...] = ("postgres", "neo4j-bolt", "redis")


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


def endpoints_by_name() -> dict[str, Endpoint]:
    """:func:`compose_endpoints` keyed by service name, in :data:`SERVICES` order."""
    return {endpoint.name: endpoint for endpoint in compose_endpoints()}


def resolve(*services: str) -> list[Endpoint]:
    """The endpoints for ``services``; every endpoint when nothing is named.

    Raises:
        ValueError: for a name this module does not publish. Loud on purpose — a typo'd
            or silently-ignored service name would degrade back into the all-or-nothing
            behaviour T-109 exists to remove, and would do it invisibly.
    """
    by_name = endpoints_by_name()
    if not services:
        return list(by_name.values())
    chosen: dict[str, Endpoint] = {}
    for name in services:
        if name not in by_name:
            raise ValueError(
                f"unknown compose service {name!r}; known services are "
                f"{', '.join(by_name)}. Note the timeout is keyword-only: pass "
                f"`timeout=` rather than a bare positional."
            )
        chosen[name] = by_name[name]
    return [by_name[name] for name in by_name if name in chosen]


def is_open(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """``True`` if a TCP connection to ``host:port`` completes within ``timeout``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def unreachable(*services: str, timeout: float = DEFAULT_TIMEOUT) -> list[Endpoint]:
    """The named endpoints that did not answer. Empty list means they are all up."""
    return [e for e in resolve(*services) if not is_open(e.host, e.port, timeout)]


def format_reason(down: Sequence[Endpoint]) -> str | None:
    """The skip message for a set of already-probed endpoints, or ``None`` if none are down.

    Split out from :func:`skip_reason` so a caller that probes each service once per session
    — the root ``conftest.py``, across thousands of collected items — can build per-item
    messages without re-probing anything.
    """
    if not down:
        return None
    names = ", ".join(str(e) for e in down)
    return f"compose datastore stack is not reachable: {names}. Run `make deps-up` first."


def skip_reason(*services: str, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """A human-readable skip reason for ``services``, or ``None`` when they are reachable.

    The message names the endpoints that were actually down, so a skip says *which* store
    is missing rather than "the stack".
    """
    return format_reason(unreachable(*services, timeout=timeout))
