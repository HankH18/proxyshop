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
* **T-109** — that skip is decided **per service**, not per session. Each item's required
  datastores come from its ``docker`` marker argument (``@pytest.mark.docker("postgres")``)
  or, for the bare marks every merged suite carries, from the datastore fixtures it
  requests; see :mod:`proxyshop_support.service_markers`. Before this, one combined reason
  was stamped on every ``docker`` item as soon as any one endpoint failed to answer, so a
  one-second Redis blip skipped all 47 of them — T-011's whole S7 role-isolation gate —
  and the run exited 0 with nothing red. A skipped datastore test is indistinguishable
  from a passing one in the frozen metrics, which is what made that silent.
* **T-210** — a run whose ONLY failure was cross-worker Neo4j lock contention (D37) exits
  **77** instead of 1, so an orchestrator reading the child process can tell a machine
  condition from a product defect without a human. Fail-closed: any unexplained failing
  report, any non-lock failure, or a status other than pytest's 1 leaves the status alone.
  See :mod:`proxyshop_support.lock_exit_status`; the registration is the five-name import
  below and the comment beside it says why all five are required. **Honest limit**:
  ``build_succeeds`` is ``if make verify; then echo 1; else echo 0; fi``, which reads
  zero-versus-non-zero and never the value, so 77 is still a zero there — this is machine
  triage, not a cure for the false zeros already in ``history.csv``. The cure is
  scheduler-level (never measure concurrently with a lane that touches the graph).
* ``@pytest.mark.needs_model`` tests skip unless ``PROXYSHOP_ALLOW_MODEL=1``. D18 keeps
  ``torch``/``sentence-transformers`` uninstalled by default, so those tests cannot pass
  here; ``make verify`` additionally deselects them with ``-m "not needs_model"``.

* **D38, Postgres** — the per-worker database ``proxyshop_w<N>`` is *created* by the
  ``worker_database`` fixture if it is not already there, and every DSN's database
  component is rewritten to it, so a shared ``.env`` cannot put two workers in one
  database. A database that cannot be created is a **failure**, never a skip: a skipped
  datastore test is indistinguishable from a passing one in the metrics.

* **Process-wide wiring seams are put back after every test** (``_process_wide_wiring_seams``
  below, autouse). ``configure_accept`` writes ``accept.offer._platform_domains`` for the
  whole process on purpose (T-169), so a pytest session — which configures hundreds of apps
  where a deployment configures one — used to hand the *next* test whatever the last one
  wired. Measured at ``5ded118``: that flipped **six** tests of the frozen acceptance suite
  from green to red purely on collection order, and a gate whose answer depends on what ran
  before it is not a gate. ``checkout.registry._REGISTRY`` is the second one, found by
  shuffling the combined suite once the first was closed: hostile test providers registered
  into it outlive their test and turn ``test_fallback_handoff.py``'s sweep red. The
  mechanism, the writers and the demonstration live in
  :mod:`proxyshop_support.process_seams`.

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
``neo4j_session``    function   ``neo4j.Session`` — always inside the D37 flock.
``redis_client``     function   ``WorkerRedis`` — DB ``N``, ``w{N}:`` prefixed, flushed.
``shopify_stub_url`` function   ``str`` — base URL of an in-process shopify-stub on an
                                ephemeral port (D40).
``frozen_clock``     function   ``time_machine.Coordinates`` — global wall clock, frozen.
``manual_clock``     function   ``ManualClock`` — injectable clock, nothing patched.
``llm_double``       function   ``LLMDouble`` — deterministic offline LLM (D19).
``hash_embedding``   session    ``Callable[[str], list[float]]`` — 1024-d unit vector (D6).
===================  =========  ==========================================================

Plus one autouse fixture nothing has to request: ``_process_wide_wiring_seams``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import pytest

from proxyshop_support import process_seams, reachability, service_markers
from proxyshop_support.clock import EPOCH, ManualClock
from proxyshop_support.embedding import EMBEDDING_DIM, hash_embed
from proxyshop_support.llm_double import LLMDouble

# T-210 — register the lock-contention exit status plugin. This file IS a pytest plugin, so
# every ``pytest_*`` name in its namespace is a hook; these five imports are the whole of the
# registration and each one is load-bearing, which is why they are spelled out rather than
# star-imported:
#
#   pytest_sessionstart        starts a fresh tally for this session;
#   pytest_exception_interact  classifies each failure by its real exception — the ONLY
#                              place a Neo4jLockTimeout is positively identified, so without
#                              it ``lock_timeouts`` stays 0 and the re-stamp never fires;
#   pytest_runtest_logreport   counts every failing report, including ones no classifier saw
#                              — without it ``failing_reports`` stays 0, condition 4
#                              (failing_reports == lock_timeouts) is false, and again nothing
#                              fires;
#   pytest_collectreport       folds collection errors into that same count, so an
#                              unexplained failure alongside a lock timeout STOPS the
#                              re-stamp instead of being invisible to it;
#   pytest_sessionfinish       does the re-stamp itself, and is worthless alone.
#
# That last clause is the trap the module's docstring names and this lane re-measured on
# this checkout: importing ``pytest_sessionfinish`` by itself gives an empty tally, so
# ``is_lock_contention_only`` is false by construction — a wiring that imports cleanly,
# lints cleanly, and leaves contention exiting 1. Measured here against a real held flock,
# green / contention / defect / (contention+defect): all five give 0 / 77 / 1 / 1, while
# sessionfinish alone gives 0 / 1 / 1 / 1, i.e. the defect untouched. Do not trim this list.
#
# None of the five collides with a hook defined below (``pytest_configure``,
# ``pytest_collection_modifyitems``).
from proxyshop_support.lock_exit_status import (  # noqa: F401
    pytest_collectreport,
    pytest_exception_interact,
    pytest_runtest_logreport,
    pytest_sessionfinish,
    pytest_sessionstart,
)
from proxyshop_support.neo4j_lock import neo4j_flock, reset_graph
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

    The probes run here, at collection time — deliberately not in ``pytest_runtest_setup``,
    where the socket guard would already be installed and the probe itself would be blocked.

    Each *service* is probed at most once per session (``probed`` below), and each *item*
    then gets the reason built from only the services it needs. That ordering matters both
    ways: probing per item would open three TCP connections per collected test, and probing
    once for the whole stack is the T-109 defect.
    """
    probed: dict[str, reachability.Endpoint | None] = {}

    def docker_reason_for(item: pytest.Item) -> str | None:
        try:
            services = service_markers.services_for(
                [marker.args for marker in item.iter_markers("docker")],
                getattr(item, "fixturenames", ()),
            )
        except ValueError as exc:
            # A typo'd service name must stop the session, not silently widen to the whole
            # stack — a widened skip is invisible in the metrics.
            raise pytest.UsageError(f"{item.nodeid}: {exc}") from exc
        for name in services:
            if name not in probed:
                down = reachability.unreachable(name)
                probed[name] = down[0] if down else None
        return reachability.format_reason(
            [endpoint for name in services if (endpoint := probed[name]) is not None]
        )

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
        if item.get_closest_marker("docker"):
            docker_reason = docker_reason_for(item)
            if docker_reason:
                item.add_marker(pytest.mark.skip(reason=docker_reason))
                item.stash[_DOCKER_SKIP_KEY] = docker_reason
        if model_reason and item.get_closest_marker("needs_model"):
            item.add_marker(pytest.mark.skip(reason=model_reason))


# --------------------------------------------------------------------------------------
# process-wide wiring seams
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _process_wide_wiring_seams() -> Iterator[None]:
    """Every test leaves the exchange's process-wide wiring seams as it found them.

    Autouse and unconditional, because the writer is *product* code and no test opts in to
    it: ``configure_accept`` — and ``composition.py`` through it — writes
    ``accept.offer._platform_domains`` for the whole process by design (T-169), so any test
    that configures an app is a writer whether its author knew it or not, and
    ``register_provider`` fills a process-global table the same way. A per-test
    ``try/finally`` in each of them is the fix that is one forgotten call site away from being
    no fix at all, which is the same argument that made the seam global in the first place.

    Measured at ``5ded118``, this is worth six tests: the frozen acceptance suite passed 120/120
    alone and failed six of them behind ``apps/exchange``, entirely because the last app
    configured before it left a registry that knew nothing about ``store-a``. See
    :mod:`proxyshop_support.process_seams` for the writer, the reader and the demonstration,
    and ``apps/exchange/tests/test_process_seam_isolation.py`` for the regression test that
    drives a real two-suite ordering in a subprocess and proves it can still go red.

    Nothing is imported to do this: a seam whose module is not already in ``sys.modules`` is
    left alone, so a buyer or trust session pays a dict lookup and nothing else.
    """
    taken = process_seams.snapshot()
    try:
        yield
    finally:
        process_seams.restore(taken)


# --------------------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def worker_index() -> int:
    """This run's ``$PROXYSHOP_WORKER`` value (D38). Session-scoped, never defaulted."""
    return worker_id()


def _require_services(*services: str) -> None:
    """Skip cleanly when the datastores THIS fixture needs are down (T-109).

    Named services only. ``_require_services()`` with no argument would mean the whole
    stack, which is what every fixture used to do and is why a Redis outage skipped the
    Postgres fixtures — so every call site below names its own store.
    """
    reason = reachability.skip_reason(*services)
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
    _require_services("postgres")
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
def _neo4j_connection() -> Iterator[Any]:
    """The Bolt connection to the compose Neo4j (D6: 5.26 Community) — **and nothing else**.

    Session-scoped because a TCP connection is expensive and shareable; deliberately
    **lock-free** because the D37 flock is not. Splitting the two is the whole of T-214: the
    connection wants to live for the session, the machine-global lock must not.

    Nothing outside this file should request this. Take :func:`neo4j_driver`, which hands
    you the same connection with the lock held and the graph reset.

    Requires ``@pytest.mark.docker``.
    """
    from neo4j import GraphDatabase

    from proxyshop_support.neo4j_auth import graph_credentials

    _require_services("neo4j-bolt")
    # Resolved through the ONE resolver, not a fourth spelling of the same three defaults.
    # This block used to inline `proxyshop_dev_pw` alongside the two served paths and the
    # readiness probe's `""` — four readings of one credential, and the probe's disagreed. It
    # agreed here by luck rather than by construction, and drift made the graph suite SKIP
    # rather than fail, which is the quietest way for a gate to stop grading anything.
    creds = graph_credentials()
    driver = GraphDatabase.driver(
        creds.uri, auth=(creds.user, creds.password), connection_timeout=5
    )
    try:
        driver.verify_connectivity()
    except Exception as exc:  # pragma: no cover - stack-down path
        driver.close()
        pytest.skip(f"neo4j is not reachable at {creds.describe()}: {exc}")
    try:
        yield driver
    finally:
        driver.close()


@pytest.fixture
def _neo4j_guard() -> Iterator[bool]:
    """Hold the cross-worker Neo4j lock (D37) for **one test**. Always.

    Yields ``True``: every test that touches Neo4j holds the ``flock`` on
    ``/tmp/proxyshop-neo4j.lock`` while it runs, and therefore owns the single
    Community-edition database (D4: there is exactly ONE, so this lock is the only
    isolation that exists) for the duration of that test.

    **Function-scoped, and that is the point (T-214).** This fixture used to be
    ``scope="session"``. A session fixture is torn down at the END of the session, so the
    first Neo4j-touching test took a MACHINE-GLOBAL lock and the run kept it until it
    exited. That was invisible while ``scripts/verify.sh check`` deselected ``docker``
    tests; the moment it stopped (T-117/ESC-005), it became the dominant failure mode.
    Measured on a real ``check``: the flock was held from t=25.1 s to t=220.8 s of a
    221.78 s session — 88 % of the run — with the ~3 800 tests that never touch Neo4j
    executing inside the hold. **Every lane runs ``check``**, so concurrent lanes serialised
    on one lock and the loser died with ``Neo4jLockTimeout`` at 240 s for a machine reason
    that had nothing to do with its code. Narrowing the scope is what fixes that: the lock
    is now held for a graph test's own runtime and released between tests, so a sibling
    worker's wait is bounded by one test rather than by a whole session.

    Because the lock is dropped between tests, another process may write the graph in the
    gap — so the reset moved with it: :func:`neo4j_driver` resets **per acquisition**, not
    once per session. Serialization without a reset at each hand-off would be no isolation
    at all.

    **This used to be an override rather than one unconditional definition, and that shape
    was unsound twice over.** Measured, both:

    1. Any directory without an override — ``e2e/`` most importantly, where three tickets
       read a graph a fourth one writes — got no lock *and* no :func:`reset_graph`. An e2e
       test finished in 0.45 s while an ingest lane held the flock for 6 s, and read a node
       a previous lane had left behind.
    2. ``neo4j_driver`` was **session**-scoped, so pytest built it once and cached it. Only
       the *first* requester's ``_neo4j_guard`` was ever consulted. In a whole-repo run
       collecting ``apps/`` before ``e2e/`` that happened to be a lane with the override —
       but reorder the collection, or run ``pytest e2e apps/exchange``, and the guardless
       directory won for the entire session and silently disarmed the lanes that did care.

    A single unconditional definition removes both failure modes: there is no override to
    resolve, so there is nothing for the cache to pick the wrong answer from.
    ``proxyshop_support/tests/test_scaffold_wiring.py`` keeps it that way.

    Cost is nil when no graph test runs: this fixture is only ever built as a dependency of
    :func:`neo4j_driver`, which nothing but a Neo4j test requests. The stack-reachability
    check happens **before** the lock is taken, so a test running against a down stack skips
    immediately instead of making every sibling worker wait out the timeout.

    The flock is re-entrant within a process (see ``proxyshop_support.neo4j_lock``), so a
    nested acquisition — a test that takes it explicitly, a lane conftest that still wraps
    it — costs nothing and cannot self-deadlock.
    """
    _require_services("neo4j-bolt")
    with neo4j_flock():
        yield True


@pytest.fixture
def neo4j_driver(_neo4j_guard: bool, _neo4j_connection: Any) -> Iterator[Any]:
    """The session's ``neo4j.Driver``, handed out with the D37 lock held and a clean graph.

    Function-scoped, over a session-scoped connection: ``_neo4j_guard`` takes the lock for
    this test, and the graph is reset *inside* it, before the test body runs and while no
    other worker can be writing. The reset is per test rather than per session precisely
    because the lock is now released between tests (T-214) — whatever a sibling worker did
    in the gap is deleted before this test looks at the graph.

    The connection itself is **not** rebuilt per test; only the lock and the reset are.

    Requires ``@pytest.mark.docker`` (and, for writes, ``@pytest.mark.graph``).
    """
    if _neo4j_guard:
        reset_graph(_neo4j_connection)
    yield _neo4j_connection


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
    _require_services("redis")
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

    Raises:
        ImportError / AttributeError, in their own name, when ``shopify_stub.app:app`` cannot
        be reached. **T-205**: this used to ``pytest.skip`` on exactly those two exceptions.
        That was right while ``services/shopify-stub`` was an empty scaffold and T-013 had not
        landed — the condition has been false for a long time, and what was left was a fixture
        that answered "the stub is broken" with the same word it answers "the stub does not
        exist yet". A skip is scored like a pass, so the pinned entry point could break and
        report green. Measured on this tree before the change, with a synthetic
        ``ModuleNotFoundError`` inside ``shopify_stub.app``:
        ``proxyshop_support/tests/test_shared_runtime.py::test_shopify_stub_url_fixture_is_wired_to_the_stub``
        reported **1 passed, rc=0** — the one silently-green test. (The 521 tests under
        ``services/shopify-stub`` do *not* go green: ``tests/_fixtures_stub.py:26`` imports the
        stub at module scope, so that directory dies rc=4 as a collection error, loudly.)
    """
    from proxyshop_support.asgi_server import serve

    # No ``try``. A broken entry point must reach the reporter as an error, and the traceback
    # of the real ``ImportError`` names the module that actually failed — strictly more than
    # the swallowed message ever said.
    #
    # ``__import__`` rather than ``importlib.import_module`` is a real coupling and not style,
    # but only as strong as what was measured. The two guards in
    # ``services/shopify-stub/tests/test_repro_open_tickets.py`` drive this line by patching
    # ``builtins.__import__``, which ``import_module`` does not consult. All four cells were
    # run rather than reasoned about:
    #
    #   __import__    + no swallow          -> passes
    #   import_module + no swallow          -> FAILS "the synthetic import breakage never
    #                                          fired; this probe is wrong"
    #   import_module + swallow reinstated  -> FAILS, same dead-probe message
    #   __import__    + swallow reinstated  -> FAILS "the fixture converted an ImportError
    #                                          into a skip"
    #
    # So swapping in ``import_module`` reds the gate immediately and CANNOT hide a returning
    # swallow — an earlier draft of this comment claimed it could, and the matrix says
    # otherwise. What the swap actually costs is the DIAGNOSIS: the message stops naming the
    # defect and starts saying the probe is broken, pointing the next reader at the wrong
    # file. Change the line if you have a reason; change the guards in the same commit.
    module = __import__("shopify_stub.app", fromlist=["app"])
    app = module.app
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


#: Environment variables that would point a test at a REAL, BILLED model provider.
#:
#: Cleared for every test, autouse, and the reason is not hypothetical. The runbook tells an
#: operator to `set -a && . ./.env && set +a` before working — that is the documented path — and
#: `.env` is where a live `ANTHROPIC_API_KEY` and `LLM_PROVIDER=anthropic` belong. A shell that
#: has followed the runbook and then runs `pytest` was, until this fixture, a shell in which the
#: suite could reach the network and spend money. Measured: two tests
#: (`test_a_recorded_model_writes_the_pitch_through_the_same_served_door` and
#: `test_a_store_taught_the_opposite_moves_the_opposite_way`) failed for exactly that reason and
#: for no other, passing again the moment the variables were absent.
#:
#: D19 and D3 already say the offline double is the default and that tests reach no network. This
#: is what makes the ambient environment unable to override them. A test that genuinely wants a
#: live provider sets it itself with `monkeypatch.setenv`, which still works — this clears the
#: INHERITED value, it does not forbid a deliberate one.
LIVE_MODEL_ENV: tuple[str, ...] = (
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
)


@pytest.fixture(autouse=True)
def _no_ambient_model_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits a live model provider from the shell that started it."""
    for name in LIVE_MODEL_ENV:
        monkeypatch.delenv(name, raising=False)


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
