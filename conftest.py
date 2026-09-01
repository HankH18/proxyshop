"""Root pytest configuration for the ProxyShop monorepo.

Orchestrator-owned (T-000) and **frozen**: no feature ticket edits this file. A worker that
needs a new fixture adds it in a ``tests/_fixtures_<topic>.py`` file it owns; the
per-directory ``conftest.py`` files auto-load those (see
``proxyshop_support.fixture_loader``).

What this file guarantees for every pytest run in the repo:

* **D38** — the session fails immediately if ``PROXYSHOP_WORKER`` is unset. Every kind of
  isolation this build has (Postgres database name, Redis logical DB, Redis key prefix)
  derives from that number, so defaulting it would silently let two workers share state.
* **D19/D3** — the pytest-socket guard is installed with
  ``--disable-socket --allow-hosts=127.0.0.1,localhost,::1``. Loopback still works (the
  compose datastores and port-0 ASGI servers live there); everything else raises.
* ``@pytest.mark.docker`` tests **skip with an explicit message** when the compose stack is
  unreachable. The reachability probe is a bounded TCP connect run at collection time, so
  a down stack can never hang the session.
* ``@pytest.mark.needs_model`` tests skip unless ``PROXYSHOP_ALLOW_MODEL=1``. D18 keeps
  ``torch``/``sentence-transformers`` uninstalled by default, so those tests cannot pass
  here; ``make verify`` additionally deselects them with ``-m "not needs_model"``.

* **D38, Postgres** — the per-worker database ``proxyshop_w<N>`` is *created* by the
  ``worker_database`` fixture if it is not already there, and every DSN's database
  component is rewritten to it, so a shared ``.env`` cannot put two workers in one
  database. A database that cannot be created is a **failure**, never a skip: a skipped
  datastore test is indistinguishable from a passing one in the metrics.

The shared fixtures, in the order they are defined below:

===================  =========  ==========================================================
Fixture              Scope      Yields
===================  =========  ==========================================================
``worker_database``  session    ``str`` — the name of this worker's Postgres database,
                                created if absent (D38).
``pg_admin``         session    ``psycopg.Connection`` — autocommit superuser on this
                                worker's database.
``pg_role``          function   ``Callable[[str], psycopg.Connection]`` — a connection as
                                one of the least-privilege roles (D5).
``neo4j_session``    function   ``neo4j.Session`` — inside the D37 flock where one applies.
``redis_client``     function   ``WorkerRedis`` — DB ``N``, ``w{N}:`` prefixed, flushed.
``shopify_stub_url`` function   ``str`` — base URL of an in-process shopify-stub on an
                                ephemeral port (D40).
``frozen_clock``     function   ``time_machine.Coordinates`` — global wall clock, frozen.
``manual_clock``     function   ``ManualClock`` — injectable clock, nothing patched.
``llm_double``       function   ``LLMDouble`` — deterministic offline LLM (D19).
``hash_embedding``   session    ``Callable[[str], list[float]]`` — 1024-d unit vector (D6).
===================  =========  ==========================================================
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import pytest

from proxyshop_support import reachability
from proxyshop_support.clock import EPOCH, ManualClock
from proxyshop_support.embedding import EMBEDDING_DIM, hash_embed
from proxyshop_support.llm_double import LLMDouble
from proxyshop_support.neo4j_lock import reset_graph
from proxyshop_support.postgres import ensure_worker_database, role_dsn
from proxyshop_support.redis_client import WorkerRedis, worker_redis
from proxyshop_support.worker import ENV_VAR, worker_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg

ALLOWED_HOSTS = "127.0.0.1,localhost,::1"

_DOCKER_SKIP_KEY = pytest.StashKey[str]()


# --------------------------------------------------------------------------------------
# session-level policy
# --------------------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Fail fast on an unconfigured worker (D38) and arm the socket guard (D19)."""
    if not os.environ.get(ENV_VAR):
        raise pytest.UsageError(
            f"{ENV_VAR} is unset. Every ProxyShop pytest run is per-worker isolated "
            f"(D38): the Postgres database name, the Redis logical DB index and the "
            f"Redis key prefix all derive from it, so an unset value would let two "
            f"concurrent workers corrupt each other. Export it and re-run, e.g.:\n"
            f"    {ENV_VAR}=1 make verify"
        )
    # pytest-socket reads these in its own pytest_configure, which pluggy calls after this
    # one (conftest plugins are registered last and therefore called first).
    config.option.disable_socket = True
    if not config.option.allow_hosts:
        config.option.allow_hosts = ALLOWED_HOSTS


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark ``docker`` and ``needs_model`` tests for skipping, before any socket guard.

    The stack probe runs here, once per session, at collection time — deliberately not in
    ``pytest_runtest_setup``, where the socket guard would already be installed and the
    probe itself would be blocked.
    """
    docker_reason: str | None = None
    if any(item.get_closest_marker("docker") for item in items):
        docker_reason = reachability.skip_reason()

    model_reason = (
        None
        if os.environ.get("PROXYSHOP_ALLOW_MODEL") == "1"
        else (
            "requires downloaded embedding weights; torch/sentence-transformers are an "
            "optional extra that is never installed by default (D18). Set "
            "PROXYSHOP_ALLOW_MODEL=1 after installing the `embeddings` extra to run these."
        )
    )

    for item in items:
        if docker_reason and item.get_closest_marker("docker"):
            item.add_marker(pytest.mark.skip(reason=docker_reason))
            item.stash[_DOCKER_SKIP_KEY] = docker_reason
        if model_reason and item.get_closest_marker("needs_model"):
            item.add_marker(pytest.mark.skip(reason=model_reason))


# --------------------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def worker_index() -> int:
    """This run's ``$PROXYSHOP_WORKER`` value (D38). Session-scoped, never defaulted."""
    return worker_id()


def _require_stack() -> None:
    reason = reachability.skip_reason()
    if reason:
        pytest.skip(f"{reason} (mark this test @pytest.mark.docker so it skips cleanly)")


# --------------------------------------------------------------------------------------
# 1-2. Postgres
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def worker_database(worker_index: int) -> str:
    """Guarantee ``proxyshop_w<N>`` exists, and return its name (D38).

    This is the fixture that makes every other Postgres fixture *run* rather than skip.
    Nothing else in the repo creates the per-worker database, and a datastore test that
    skips is indistinguishable from one that passes in the frozen metrics — so an absent,
    uncreatable database raises :class:`~proxyshop_support.postgres.WorkerDatabaseError`
    here and takes the run red. ``make deps-up`` / ``make db-init`` do the same thing ahead
    of time; this is the backstop that makes the guarantee unconditional.

    Requires ``@pytest.mark.docker`` — the stack being *down* is still a clean skip.
    """
    _require_stack()
    return ensure_worker_database(worker_index)


@pytest.fixture(scope="session")
def pg_admin(worker_database: str, worker_index: int) -> Iterator[psycopg.Connection]:
    """Autocommit superuser connection to this worker's Postgres database.

    DSN: ``$PROXYSHOP_PG_DSN_ADMIN`` (see ``.env.example``) with its database component
    rewritten to ``proxyshop_w<N>`` for *this* worker — a shared ``.env`` therefore cannot
    put two workers in one database. With no ``.env`` at all the compose defaults are used,
    so a fresh clone connects. Autocommit is on because the things an admin connection is
    for — ``CREATE ROLE``, ``GRANT``, ``TRUNCATE`` — either cannot run in a transaction or
    should not be rolled back by accident.

    Session-scoped: one connection per pytest session. Tests that need transactional
    isolation should open their own connection through :func:`pg_role`, not reuse this one.

    Requires ``@pytest.mark.docker``.
    """
    import psycopg

    conn = psycopg.connect(
        role_dsn("admin", worker_index, database=worker_database),
        autocommit=True,
        connect_timeout=5,
    )
    with conn:
        yield conn


@pytest.fixture
def pg_role(
    worker_database: str, worker_index: int
) -> Iterator[Callable[[str], psycopg.Connection]]:
    """Factory yielding a Postgres connection as one of the least-privilege roles (D5).

    Usage::

        def test_exchange_cannot_read_the_vault(pg_role):
            with pg_role("exchange").cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("select * from vault.payment_methods")

    Args (of the returned callable):
        role: one of ``"exchange"``, ``"trust_rw"``, ``"buyer_vault"``, ``"app"`` — the
            roles created by ``db/init/00-roles.sql``. Any other name raises ``KeyError``
            rather than silently connecting as the wrong principal.

    Returns:
        A ``psycopg.Connection`` in its default (transactional) mode. Connections are
        cached per role for the duration of the test and closed at teardown, so calling
        the factory twice with the same role gives you the same session — which is what
        you want when asserting on privileges.

    Requires ``@pytest.mark.docker``.
    """
    import psycopg

    open_connections: dict[str, psycopg.Connection] = {}

    def connect(role: str) -> psycopg.Connection:
        if role == "admin":
            raise KeyError("pg_role is for the least-privilege roles; use pg_admin instead")
        if role not in open_connections:
            dsn = role_dsn(role, worker_index, database=worker_database)
            open_connections[role] = psycopg.connect(dsn, connect_timeout=5)
        return open_connections[role]

    try:
        yield connect
    finally:
        for conn in open_connections.values():
            conn.close()


# --------------------------------------------------------------------------------------
# 3. Neo4j
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _neo4j_guard() -> Iterator[bool]:
    """Serialization hook for Neo4j (D37).

    Yields whether this session holds the cross-worker lock and therefore owns the single
    Community-edition database exclusively.

    The default is ``False`` — no lock, no reset. ``services/ingest/tests/conftest.py`` and
    ``apps/exchange/tests/conftest.py`` **override** this fixture with one that holds the
    ``flock`` on ``/tmp/proxyshop-neo4j.lock`` for the whole session and yields ``True``.
    The flock is re-entrant within a process (see ``proxyshop_support.neo4j_lock``), so a
    whole-repo run that collects both graph lanes takes it once and does not self-deadlock.
    """
    yield False


@pytest.fixture(scope="session")
def neo4j_driver(_neo4j_guard: bool) -> Iterator[Any]:
    """Session-scoped ``neo4j.Driver`` for the compose Neo4j (D6: 5.26 Community).

    When this session holds the D37 lock (``_neo4j_guard`` is ``True``) the graph is reset
    once, here, *inside* the lock — before any test runs and while no other worker can be
    writing. Without that, one lane's nodes are still present when the next lane asserts.

    Requires ``@pytest.mark.docker`` (and, for writes, ``@pytest.mark.graph``).
    """
    from neo4j import GraphDatabase

    _require_stack()
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "proxyshop_dev_pw")
    driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=5)
    try:
        driver.verify_connectivity()
    except Exception as exc:  # pragma: no cover - stack-down path
        driver.close()
        pytest.skip(f"neo4j is not reachable at {uri}: {exc}")
    try:
        if _neo4j_guard:
            reset_graph(driver)
        yield driver
    finally:
        driver.close()


@pytest.fixture
def neo4j_session(neo4j_driver: Any) -> Iterator[Any]:
    """A ``neo4j.Session`` on the default database, closed at the end of the test.

    Neo4j Community has exactly one database, so isolation is by serialization, not by
    namespace: take ``@pytest.mark.graph`` on any test that writes, and let the D37 flock
    in the graph lanes' conftests do the rest.

    Requires ``@pytest.mark.docker``.
    """
    with neo4j_driver.session() as session:
        yield session


# --------------------------------------------------------------------------------------
# 4. Redis
# --------------------------------------------------------------------------------------


@pytest.fixture
def redis_client(worker_index: int) -> Iterator[WorkerRedis]:
    """This worker's Redis client: logical DB ``N``, ``w{N}:`` key prefix (D39).

    Every key you pass is namespaced transparently, so two workers running at once cannot
    see each other's data even though they share one Redis container. The database is
    reset with ``FLUSHDB`` — never the repo-banned global reset — before **and** after each
    test, so a failing test cannot poison its neighbours.

    Yields:
        :class:`proxyshop_support.redis_client.WorkerRedis` with ``decode_responses=True``.
        ``client.key("cart")`` gives you the on-the-wire key if you need to assert on it.

    Requires ``@pytest.mark.docker``.
    """
    _require_stack()
    client = worker_redis(worker=worker_index)
    try:
        client.flushdb()
    except Exception as exc:  # pragma: no cover - stack-down path
        pytest.skip(f"redis is not reachable: {exc}")
    try:
        yield client
    finally:
        client.flushdb()
        client.close()


# --------------------------------------------------------------------------------------
# 5. Shopify stub
# --------------------------------------------------------------------------------------


@pytest.fixture
def shopify_stub_url() -> Iterator[str]:
    """Base URL of an in-process shopify-stub bound to an ephemeral port (D40).

    The stub is *not* the compose service — that one carries ``profiles: ["e2e"]`` and is
    only used by the e2e tickets. Unit tests get the same ASGI app started in this process
    on port 0, which costs no container RAM and lets each test own its own stub state
    (the pixel-drop-rate knob, the discount-code table).

    Yields:
        e.g. ``"http://127.0.0.1:53412"``, no trailing slash. Point an ``httpx.Client`` at it.

    Skips (never fails) while ``services/shopify-stub`` is still empty, so the scaffold and
    every ticket that lands before T-013 stay green.
    """
    from proxyshop_support.asgi_server import serve

    try:
        module = __import__("shopify_stub.app", fromlist=["app"])
        app = module.app
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"services/shopify-stub does not expose `shopify_stub.app:app` yet ({exc})")
    with serve(app) as base_url:
        yield base_url


# --------------------------------------------------------------------------------------
# 6-7. Clocks
# --------------------------------------------------------------------------------------


@pytest.fixture
def frozen_clock() -> Iterator[Any]:
    """Freeze the **global** wall clock at 2026-01-01T00:00:00Z.

    Backed by ``time-machine``, so ``datetime.now()`` and ``time.time()`` stop — including
    inside third-party libraries you cannot pass a clock to.

    **``time.monotonic()`` does NOT stop** (measured; see
    ``proxyshop_support/tests/test_shared_runtime.py``). Anything that measures a deadline
    monotonically — ``asyncio.wait_for``, most timeout helpers — keeps running in real
    time under this fixture, so a test that "advances an hour" past such a deadline will
    actually wait an hour. Inject :func:`manual_clock` into that code instead.

    Yields:
        The ``time_machine.Coordinates`` object. ``clock.shift(seconds)`` moves time
        forward; ``clock.move_to(instant)`` jumps to an absolute point.

    Prefer :func:`manual_clock` for code you control — patching the global clock is a big
    hammer and it interacts badly with real timeouts.
    """
    import time_machine

    with time_machine.travel(EPOCH, tick=False) as traveller:
        yield traveller


@pytest.fixture
def manual_clock() -> ManualClock:
    """An injectable clock that only moves when the test moves it.

    Nothing global is patched. Pass it into the code under test::

        service = Auction(clock=manual_clock)
        manual_clock.advance(30)

    Returns:
        :class:`proxyshop_support.clock.ManualClock` starting at 2026-01-01T00:00:00Z, with
        ``.now()``, ``.timestamp()``, ``.monotonic()``, ``.advance(seconds)``, ``.set(dt)``.
    """
    return ManualClock()


# --------------------------------------------------------------------------------------
# 8. LLM double
# --------------------------------------------------------------------------------------


@pytest.fixture
def llm_double() -> LLMDouble:
    """The deterministic offline LLM (D19: ``LLM_PROVIDER=double``; D3: no network).

    Returns:
        :class:`proxyshop_support.llm_double.LLMDouble`. Queue exact replies with
        ``.queue("...")``, bind replies to prompt content with ``.when(substring, reply)``,
        and assert on prompt assembly through ``.calls`` / ``.last_prompt``. With nothing
        queued it answers ``"double:<role>:<hash>"``, which is stable across runs.
    """
    return LLMDouble()


# --------------------------------------------------------------------------------------
# 9. Embeddings
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def hash_embedding() -> Callable[[str], list[float]]:
    """The deterministic embedding function (D18: ``EMBEDDING_PROVIDER=hash``).

    Returns:
        ``embed(text: str) -> list[float]`` producing a 1024-dimension, L2-normalised
        vector — the shape D6 fixes for the Neo4j cosine index. Same text always gives the
        same vector, on any machine, with no model download.

    The dimension is available as ``proxyshop_support.embedding.EMBEDDING_DIM``.
    """
    assert EMBEDDING_DIM == 1024, "D6 fixes the vector index at 1024 dimensions"
    return hash_embed
