"""Fixtures for T-060's ledger writer. Owned by T-060.

Loaded into ``apps/trust/tests/conftest.py`` by ``proxyshop_support.fixture_loader``; that
conftest is orchestrator-owned and frozen. Every name here is prefixed ``events_`` because
two sibling ``_fixtures_*.py`` files defining one name poison that fixture for the whole
directory -- and ``_fixtures_ledger_schema.py`` (T-011) already owns ``ledger_*``.

The writer is served over a **real loopback socket** on an ephemeral port (D40/D41), not
through an in-process transport, for one reason that matters to this ticket: the concurrency
claims are about several requests being in flight at once, and an ASGI transport that awaits
one call at a time would make a serialised suite look exactly like a concurrent one.

``events_service`` is a *factory* rather than a single server. "The chain continues across a
restart" is only a real assertion if the second writer shares no memory with the first, so
the test builds two, and the factory is what makes the second one genuinely new.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from trust.events import InMemoryEventStore, PostgresEventStore, create_events_app

from proxyshop_support.asgi_server import serve
from proxyshop_support.postgres import role_dsn

#: The recorded fixture stream and tampering fixture (T-060 acceptance 2 and 3).
EVENTS_FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent / "data" / "ledger_fixture_stream.json"
)

#: The role the writer connects as. ``app`` is the append-only principal 0004 grants
#: SELECT + INSERT on ``ledger`` and UPDATE on ``ledger.chain_head`` -- deliberately not
#: ``trust_rw`` (which can also DELETE), so the tests exercise the privileges the writer
#: actually ships with rather than a superset that would hide a missing grant.
EVENTS_ROLE = "app"


@pytest.fixture(scope="session")
def events_fixture_stream() -> dict[str, Any]:
    """The recorded fixture stream: raw events, the sealed chain, and a tampered copy.

    Read, never written. The recorded ``stream_hash`` is the frozen expectation for T-060
    acceptance 3: it was computed once by this code and committed, so a change to the
    canonicaliser or the sealer turns a test red instead of quietly re-baselining itself.
    """
    if not EVENTS_FIXTURE_PATH.is_file():
        raise AssertionError(f"missing fixture stream {EVENTS_FIXTURE_PATH}")
    return json.loads(EVENTS_FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def events_memory() -> InMemoryEventStore:
    """A fresh in-memory event store."""
    return InMemoryEventStore()


@pytest.fixture
def events_dsn(worker_database: str, worker_index: int) -> str:
    """The writer's DSN: role ``app`` against **this worker's** database (D38)."""
    return role_dsn(EVENTS_ROLE, worker_index, database=worker_database)


@pytest.fixture
def events_service(
    ledger_clean: Any, events_dsn: str
) -> Iterator[Callable[[], tuple[httpx.Client, PostgresEventStore]]]:
    """Factory building a **fresh** writer -- new store, new app, new server -- per call.

    Depends on ``ledger_clean`` so the migrations are applied and every ledger table is
    empty at setup, before any connection this fixture opens exists. Everything the factory
    creates is closed at teardown in reverse order, so no pool outlives the test and the
    next test's ``TRUNCATE`` is not waiting on an idle-in-transaction backend (CF-4).
    """
    stack = contextlib.ExitStack()

    def build() -> tuple[httpx.Client, PostgresEventStore]:
        store = PostgresEventStore(events_dsn)
        stack.callback(store.close)
        base_url = stack.enter_context(serve(create_events_app(store)))
        client = stack.enter_context(httpx.Client(base_url=base_url, timeout=30.0))
        return client, store

    with stack:
        yield build


@pytest.fixture
def events_running(
    events_service: Callable[[], tuple[httpx.Client, PostgresEventStore]],
) -> tuple[httpx.Client, PostgresEventStore]:
    """One running writer: ``(client, store)``."""
    return events_service()


@pytest.fixture
def events_client(events_running: tuple[httpx.Client, PostgresEventStore]) -> httpx.Client:
    """An HTTP client pointed at a running ledger writer."""
    return events_running[0]


@pytest.fixture
def events_store(
    events_running: tuple[httpx.Client, PostgresEventStore],
) -> PostgresEventStore:
    """The Postgres event store the running writer is serving."""
    return events_running[1]
