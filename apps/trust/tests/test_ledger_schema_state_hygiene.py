"""T-216: no trust test may be graded against a schema another fixture is dropping.

The defect these tests exist to keep fixed
------------------------------------------
``test_events_hardening.py`` has one test that opens a **real** connection to this worker's
Postgres database and asks the server whether ``trust_rw`` may append to
``ledger.commerce_events``. It requested ``worker_database`` and nothing else -- so the
`ledger` schema it depends on was never *declared*, only *assumed*. Whatever the persistent
``proxyshop_w<n>`` database happened to contain at that instant was the thing being graded.

Meanwhile :func:`_fixtures_ledger_schema.ledger_migrated` is **session**-scoped and, by
design, ``DROP SCHEMA ledger, sealed, vault, app CASCADE`` before re-applying
``db/migrations/*.sql``. The drop is load-bearing and must stay (with the migrations applied
but never dropped, 61 of 62 of this package's tests stayed green against a database that had
been migrated by some *earlier* checkout -- see that fixture's docstring). But a drop plus a
re-apply is not one instant: it is a window, and for as long as that window is open the
schema genuinely does not exist.

Put the two together and the observed flake follows exactly:
``psycopg.errors.InvalidSchemaName: schema "ledger" does not exist`` from a test that then
passes standalone seconds later against a database in which the schema demonstrably exists.

Reproduced here, deterministically, by putting the database into the state the drop window
creates -- which is the same state a never-migrated worker database is in -- and running the
real test in a real pytest against it. Nothing is left to ordering luck.

Both halves are gated:

=========================================================  ================================
Every live test declares the schema it needs                :func:`test_every_live_test_
                                                            that_names_an_owned_table_
                                                            declares_the_migration_fixture`
...including the one this ticket is named for               :func:`test_the_live_trust_rw_
                                                            test_declares_the_migration_
                                                            fixture_it_depends_on`
...and it survives a database in the drop window            :func:`test_the_live_trust_rw_
                                                            test_is_not_graded_against_an_
                                                            unmigrated_database`
No other session can SEE the drop window at all             :func:`test_a_concurrent_reader_
                                                            never_sees_the_owned_schemas_
                                                            disappear`
The rebuild will not drop under a concurrent migration      :func:`test_the_schema_rebuild_
                                                            refuses_to_drop_while_another_
                                                            session_holds_the_migration_lock`
...and its drop gives up instead of stalling the database   :func:`test_the_rebuilds_drop_
                                                            gives_up_instead_of_blocking_
                                                            the_whole_database`
...and it refuses to hand back a database it did not build  :func:`test_the_schema_rebuild_
                                                            fails_loudly_when_the_migrations_
                                                            leave_no_ledger_behind`
=========================================================  ================================

A note on the first fix attempt, because the correction is the point
--------------------------------------------------------------------
The first pass at this ticket put the migration **advisory lock** around the drop and called
the flake closed. It was not. Advisory locks are cooperative: they constrain only the
sessions that ask for the same key, and a test body is a reader that never asks. Measured
against that code, with a positive control, the original symptom was unchanged --
``InvalidSchemaName: schema "ledger" does not exist`` from a reader running the target
test's own statement inside the "locked" window.

What actually closes it is that PostgreSQL's DDL is transactional: drop and re-apply inside
**one** transaction and there is no intermediate state for anyone to observe.
:func:`test_a_concurrent_reader_never_sees_the_owned_schemas_disappear` is the measurement
that says so, and it is written to fail against the advisory-lock-only version.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from apps.trust.tests._fixtures_ledger_schema import (
    APP_TABLES,
    LEDGER_TABLES,
    OWNED_SCHEMAS,
    SEALED_TABLES,
    VAULT_TABLES,
    catalog_fingerprint,
)

#: The test this ticket is about, as a pytest node id relative to the repo root.
TARGET_NODEID = (
    "apps/trust/tests/test_events_hardening.py::"
    "test_the_writer_connects_to_the_real_database_as_trust_rw_from_the_per_role_var"
)

#: Any one of these, requested by a test, guarantees the migrations have been applied.
MIGRATION_FIXTURES = frozenset({"ledger_migrated", "ledger_clean"})

#: Requesting any of these gets a test a **real** connection to this worker's database, so a
#: schema-qualified name in its body is a name it will really resolve against the server.
LIVE_DATABASE_FIXTURES = frozenset(
    {"pg_admin", "pg_role", "worker_database", "ledger_roles", *MIGRATION_FIXTURES}
)

#: Every table the four owned schemas hold, plus the migration runner's own bookkeeping.
#: Matching on *these* rather than on ``r"app\.\w+"`` is what keeps ``app.include_router``
#: and friends out of the static gate below.
OWNED_TABLES = frozenset(
    (*LEDGER_TABLES, *APP_TABLES, *SEALED_TABLES, *VAULT_TABLES, "ledger.schema_migrations")
)

#: The statement the flake was originally reported from -- the target test's own privilege
#: check, reduced to the one line that touches the schema.
READER_PRIVILEGE_SQL = (
    "select has_table_privilege(current_user, 'ledger.commerce_events', 'INSERT')"
)

#: The other reader shape, which differs in the lock it takes: this one asks for
#: ``ACCESS SHARE`` on the relation and therefore *waits* for a conflicting DDL transaction
#: rather than reading around it.
READER_SELECT_SQL = "select count(*) from ledger.commerce_events"

#: Server-side cap on any single statement the concurrency gates below issue on ``pg_admin``.
#: This is the thing that makes those gates fail RED instead of hanging: the failure they
#: guard against is a *block inside the server*, which no Python-side timeout can interrupt.
#: Measured -- with the fix reverted,
#: :func:`test_the_schema_rebuild_refuses_to_drop_while_another_session_holds_the_migration_
#: lock` deadlocks permanently without it, because it holds ``MIGRATION_LOCK_KEY`` on a
#: connection it can only close *after* a ``rebuild_owned_schemas`` that is blocking on that
#: same key returns. A gate that hangs is worse than no gate: CI burns to its job timeout,
#: shows no red, and leaves the shared worker database poisoned behind it.
GATE_STATEMENT_TIMEOUT = "10s"


def _repo_root() -> Path:
    """The checkout this file belongs to -- ``apps/trust/tests/`` is three levels down."""
    return Path(__file__).resolve().parents[3]


def _drop_owned_schemas(pg_admin: Any) -> None:
    """Put the database into exactly the state ``ledger_migrated``'s DROP leaves behind.

    This is also, byte for byte, the state a freshly created ``proxyshop_w<n>`` is in.
    """
    with pg_admin.cursor() as cur:
        cur.execute(f"drop schema if exists {', '.join(OWNED_SCHEMAS)} cascade")


def _restore_owned_schemas(pg_admin: Any) -> None:
    """Put the four schemas back with the checkout's own SQL.

    Deliberately calls the migration runner directly rather than the fixture helper the
    tests below grade, so that a teardown here can never be the thing that fails.
    """
    from apps.trust.src.ledger import apply_migrations

    apply_migrations(pg_admin)


def _present_schemas(pg_admin: Any) -> list[str]:
    """Which of the four owned schemas currently exist, sorted."""
    with pg_admin.cursor() as cur:
        cur.execute(
            "select nspname from pg_namespace where nspname = any(%s)", (list(OWNED_SCHEMAS),)
        )
        return sorted(row[0] for row in cur.fetchall())


def _migration_lock_holders(pg_admin: Any) -> int:
    """How many backends hold the migration advisory lock on *this* database, right now.

    Zero is the only acceptable answer once a rebuild has returned. A lock left held is not
    a local failure: it stalls the next session's rebuild for its whole
    ``REBUILD_LOCK_TIMEOUT_SECONDS`` and then fails it.
    """
    from apps.trust.src.ledger.migrations import MIGRATION_LOCK_KEY

    with pg_admin.cursor() as cur:
        cur.execute(
            "select count(*) from pg_locks "
            " where locktype = 'advisory' and granted and objid::bigint = %s "
            "   and database = (select oid from pg_database where datname = current_database())",
            (MIGRATION_LOCK_KEY,),
        )
        return int(cur.fetchone()[0])


def _admin_connection(worker_index: int, worker_database: str) -> Any:
    """A fresh autocommit admin connection, independent of the session-scoped ``pg_admin``.

    The concurrency gates below drive a rebuild from a background thread. They do it on a
    connection of their own rather than on ``pg_admin`` because ``pg_admin`` is *session*
    scoped: a thread that left it mid-transaction would take every later test in the run
    down with it.
    """
    import psycopg

    from proxyshop_support.postgres import role_dsn

    return psycopg.connect(
        role_dsn("admin", worker_index, database=worker_database),
        autocommit=True,
        connect_timeout=5,
    )


def _run_pytest(nodeid: str) -> subprocess.CompletedProcess[str]:
    """Run one test in a **fresh** pytest session against this worker's database.

    A subprocess rather than an in-process re-entry because the thing under test is a
    *session*-scoped fixture: only a new session can decide afresh whether to build it.
    """
    root = _repo_root()
    env = dict(os.environ)
    # The `.pkgroot` packages resolve to THIS checkout, never to whichever tree a stray
    # `.pth` file in the shared virtualenv points at.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / ".pkgroot"), str(root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            nodeid,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


# --------------------------------------------------------------------------------------
# half one: the test that assumed the schema instead of asking for it
# --------------------------------------------------------------------------------------
def test_the_live_trust_rw_test_declares_the_migration_fixture_it_depends_on() -> None:
    """The dependency must be in the signature, not in the run order.

    A pure-static check, so it holds even where Postgres is unreachable and the behavioural
    test below skips. ``ledger_migrated``/``ledger_clean`` are the only two fixtures in this
    package that guarantee ``db/migrations/*.sql`` has been applied to this worker's
    database; a test that touches ``ledger.*`` over a real connection and asks for neither
    is graded by whatever the previous session left behind.
    """
    from apps.trust.tests import test_events_hardening

    name = TARGET_NODEID.rsplit("::", 1)[1]
    target = getattr(test_events_hardening, name)
    requested = set(inspect.signature(target).parameters)

    assert requested & MIGRATION_FIXTURES, (
        f"{name} reads ledger.commerce_events over a real connection but requests "
        f"{sorted(requested)} -- none of {sorted(MIGRATION_FIXTURES)}. Nothing in its "
        f"signature makes the ledger schema exist, so the assertion is graded against "
        f"whatever this persistent worker database happens to hold."
    )


def _tests_missing_their_migration_fixture() -> list[str]:
    """``["file.py::test_name (ledger.commerce_events)", ...]`` for every undeclared reader.

    A test qualifies as a live reader when it requests one of
    :data:`LIVE_DATABASE_FIXTURES` -- that is what gets it a real connection -- and names one
    of :data:`OWNED_TABLES` in a **string literal** in its body. String literals only, and
    known table names only, because ``app.include_router`` and ``ledger.foo`` as Python
    attribute access are not database references and a gate that flagged them would be
    ignored within a week.
    """
    here = Path(__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(here.glob("test_*.py")):
        if path.name == Path(__file__).name:
            # This module owns the rebuild machinery: it drops and restores the schemas by
            # hand, so it is the one place a live test legitimately names an owned table
            # without first asking for the fixture that builds it.
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            requested = {argument.arg for argument in node.args.args}
            if not requested & LIVE_DATABASE_FIXTURES:
                continue
            if requested & MIGRATION_FIXTURES:
                continue
            named = sorted(
                {
                    table
                    for child in ast.walk(node)
                    if isinstance(child, ast.Constant) and isinstance(child.value, str)
                    for table in OWNED_TABLES
                    if table in child.value
                }
            )
            if named:
                offenders.append(f"{path.name}::{node.name} names {', '.join(named[:3])}")
    return offenders


def test_every_live_test_that_names_an_owned_table_declares_the_migration_fixture() -> None:
    """The general form of this ticket, so the next one cannot be written silently.

    Fixing only ``test_events_hardening.py``'s one test fixes one test. What made that test
    a flake was not its subject matter, it was that its dependency on the ``ledger`` schema
    lived in the run order instead of in its signature -- and nothing stopped the next test
    from being written the same way. This walks every ``test_*.py`` in this package and
    fails on any test that takes a real database connection, names a table in one of the
    four owned schemas, and asks for neither ``ledger_migrated`` nor ``ledger_clean``.

    Static, so it holds where Postgres is unreachable; and it is the half of T-216 that
    *cannot be silently violated*, as opposed to the half a cooperative lock only asks
    nicely about.
    """
    offenders = _tests_missing_their_migration_fixture()
    assert offenders == [], (
        "these tests open a real connection and name a table in ledger/sealed/vault/app "
        "without requesting a fixture that makes it exist, so they are graded against "
        "whatever this persistent worker database happens to hold:\n  "
        + "\n  ".join(offenders)
        + f"\nAdd one of {sorted(MIGRATION_FIXTURES)} to the signature."
    )


@pytest.mark.docker("postgres")
def test_the_live_trust_rw_test_is_not_graded_against_an_unmigrated_database(
    pg_admin: Any,
) -> None:
    """The reproduction, made deterministic: drop the schemas, then run the real test.

    The dropped state is not hypothetical -- it is the exact state ``ledger_migrated``
    leaves the database in between its ``DROP SCHEMA ... CASCADE`` and the last migration
    file, and the state every fresh ``proxyshop_w<n>`` starts in. Before the fix this run
    ends ``psycopg.errors.InvalidSchemaName: schema "ledger" does not exist``; after it, the
    test builds the schema it needs and passes.

    The database is rebuilt on the way out whatever happens, so a red here cannot poison the
    rest of the session.
    """
    _drop_owned_schemas(pg_admin)
    assert _present_schemas(pg_admin) == [], "the drop did not take; the premise is not set up"
    try:
        result = _run_pytest(TARGET_NODEID)
    finally:
        _restore_owned_schemas(pg_admin)

    output = result.stdout + result.stderr
    assert "InvalidSchemaName" not in output, (
        "the live trust_rw test was graded against a database with no ledger schema and "
        "died with InvalidSchemaName. It must declare the migration fixture rather than "
        f"inherit the schema from whatever ran before it.\n{output[-4000:]}"
    )
    assert result.returncode == 0, (
        f"running {TARGET_NODEID} against a database in the migration fixture's drop "
        f"window exited {result.returncode}.\n{output[-4000:]}"
    )


# --------------------------------------------------------------------------------------
# half two: the fixture that does the dropping
# --------------------------------------------------------------------------------------
@pytest.mark.docker("postgres")
def test_a_concurrent_reader_never_sees_the_owned_schemas_disappear(
    pg_admin: Any,
    worker_database: str,
    worker_index: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flake itself, closed: there is no window another session can observe.

    This is the assertion the first pass at T-216 did not make, and it is the one that
    matters. Putting the migration **advisory lock** around the drop serialises the rebuild
    against other *migrators* and does nothing whatsoever for *readers*, because an advisory
    lock only constrains sessions that ask for the same key and no test body does. Measured
    against that version, with the positive control below passing first: 12 of 41 concurrent
    reads died ``InvalidSchemaName: schema "ledger" does not exist``.

    The window is opened deterministically rather than raced for: ``apply_migrations`` is
    wrapped so that the rebuild parks *after* its ``DROP SCHEMA`` and *before* its COMMIT,
    which is exactly the state the flake is reported from, and is held there until the
    reads below have happened. So this cannot pass by being lucky about timing.

    Both reader shapes are checked, because they take different locks and fail differently:

    * ``has_table_privilege`` (the target test's own statement) reads the catalog and takes
      no relation lock, so it reads *around* an uncommitted DDL transaction. Before the fix
      it saw the schema gone.
    * ``select count(*)`` takes ``ACCESS SHARE``, so it *waits* for the DDL transaction to
      end and then sees whichever side of it committed. Before the fix it saw the table
      gone.

    A positive control runs first in both cases: a reader that could not read this database
    at all before the rebuild would make a clean run here meaningless.
    """
    import apps.trust.src.ledger as ledger_package
    from apps.trust.tests._fixtures_ledger_schema import rebuild_owned_schemas

    rebuilder = _admin_connection(worker_index, worker_database)
    reader = _admin_connection(worker_index, worker_database)
    blocked_reader = _admin_connection(worker_index, worker_database)
    window_open = threading.Event()
    may_finish = threading.Event()
    rebuild_error: list[BaseException] = []
    blocked_result: list[Any] = []
    real_apply = ledger_package.apply_migrations

    def park_inside_the_drop_window(connection: Any, **kwargs: Any) -> list[str]:
        """Hold the rebuild between its DROP and its COMMIT until the reads have run."""
        window_open.set()
        assert may_finish.wait(60), "the reads never released the rebuild"
        return real_apply(connection, **kwargs)

    def run_rebuild() -> None:
        try:
            rebuild_owned_schemas(rebuilder)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the assertions below
            rebuild_error.append(exc)
        finally:
            window_open.set()
            may_finish.set()

    def run_blocked_read() -> None:
        with blocked_reader.cursor() as cur:
            cur.execute(READER_SELECT_SQL)
            blocked_result.append(cur.fetchone()[0])

    try:
        rebuild_owned_schemas(rebuilder)
        with reader.cursor() as cur:
            cur.execute(READER_PRIVILEGE_SQL)
            control_privilege = cur.fetchone()[0]
            cur.execute(READER_SELECT_SQL)
            control_select = cur.fetchone()[0]
        assert control_privilege is True, (
            "positive control failed: trust_rw cannot be asked about "
            "ledger.commerce_events even with no rebuild running, so a clean result during "
            "one would prove nothing"
        )
        assert control_select == 0, f"positive control failed: {READER_SELECT_SQL} -> nothing"

        monkeypatch.setattr(ledger_package, "apply_migrations", park_inside_the_drop_window)
        rebuild = threading.Thread(target=run_rebuild, daemon=True)
        rebuild.start()
        assert window_open.wait(60), "the rebuild never reached its migration step"
        if rebuild_error:
            raise AssertionError("the rebuild failed before the window opened") from (
                rebuild_error[0]
            )

        # Inside the window now: the DROP has run and has not committed.
        with reader.cursor() as cur:
            cur.execute(READER_PRIVILEGE_SQL)
            during_privilege = cur.fetchone()[0]

        # ...and a reader that does take a relation lock waits for the COMMIT rather than
        # racing it. Started before the window closes, joined after.
        blocked = threading.Thread(target=run_blocked_read, daemon=True)
        blocked.start()
        time.sleep(0.25)
        may_finish.set()
        blocked.join(timeout=60)
        rebuild.join(timeout=60)
    finally:
        may_finish.set()
        for connection in (blocked_reader, reader, rebuilder):
            connection.close()

    assert not rebuild_error, f"the rebuild itself failed: {rebuild_error[0]!r}"
    assert during_privilege is True, (
        f"a concurrent session ran {READER_PRIVILEGE_SQL!r} while the rebuild sat between "
        f"its DROP and its COMMIT and got {during_privilege!r}. That is the T-216 flake: "
        f"the drop window is visible to other sessions. Only running the drop and the "
        f"re-apply in one transaction hides it -- an advisory lock cannot, because readers "
        f"do not take it."
    )
    assert blocked_result == [0], (
        f"a concurrent SELECT that waited out the rebuild's transaction got "
        f"{blocked_result!r} instead of [0] -- it saw the drop rather than the rebuilt "
        f"schema"
    )
    assert _present_schemas(pg_admin) == sorted(OWNED_SCHEMAS)
    assert _migration_lock_holders(pg_admin) == 0, (
        "the rebuild returned with the migration advisory lock still held"
    )


@pytest.mark.docker("postgres")
def test_the_rebuilds_drop_gives_up_instead_of_blocking_the_whole_database(
    pg_admin: Any,
    worker_database: str,
    worker_index: int,
) -> None:
    """A drop that cannot take its locks must fail in seconds, not wait forever.

    ``DROP SCHEMA ... CASCADE`` needs ``ACCESS EXCLUSIVE`` on every table underneath it, so a
    single session merely *reading* one of them blocks it -- and CF-4 at the top of
    ``_fixtures_ledger_schema`` documents how routinely this package leaves exactly such a
    session idle in transaction. Because the rebuild drops while holding
    ``MIGRATION_LOCK_KEY``, an unbounded wait there does not stall one session: every other
    session's migrations queue behind it too. Measured on the version without this bound: a
    rebuild and a second session's ``apply_migrations`` were both still blocked after 8s and
    only moved when the reader rolled back.

    Note what the advisory ``lock_timeout`` argument does *not* cover -- it bounds the
    ``pg_try_advisory_lock`` poll before the drop and nothing about the drop's own relation
    waits. The bound asserted here is ``SET LOCAL lock_timeout``, which is the one the repo
    already uses for this in ``test_scaffold_datastores.py``'s ``d5_grant_model`` teardown.

    Passing ``drop_lock_timeout='1s'`` keeps the test to a second rather than to the
    fifteen the fixture defaults to; the mechanism asserted is the same one. The
    ``statement_timeout`` is this gate's own safety net for the same reason the one below
    has it: the failure being guarded against is *an unbounded block inside the server*, so
    a version with no ``SET LOCAL lock_timeout`` would hang this test rather than fail it,
    and it would hang holding the migration advisory lock.
    """
    import psycopg

    from apps.trust.tests._fixtures_ledger_schema import (
        LedgerSchemaRebuildError,
        rebuild_owned_schemas,
    )

    rebuild_owned_schemas(pg_admin)
    before = _present_schemas(pg_admin)
    assert before == sorted(OWNED_SCHEMAS), f"setup failed: only {before} are present"

    # Deliberately NOT autocommit: the SELECT leaves this session idle in transaction
    # holding ACCESS SHARE on ledger.commerce_events, which is the CF-4 shape exactly.
    from proxyshop_support.postgres import role_dsn

    reader = psycopg.connect(
        role_dsn("admin", worker_index, database=worker_database), connect_timeout=5
    )
    message = ""
    try:
        with reader.cursor() as cur:
            cur.execute(READER_SELECT_SQL)
            cur.fetchone()
        with pg_admin.cursor() as cur:
            cur.execute(f"set statement_timeout = '{GATE_STATEMENT_TIMEOUT}'")  # noqa: S608

        started = time.monotonic()
        try:
            rebuild_owned_schemas(pg_admin, drop_lock_timeout="1s")
        except LedgerSchemaRebuildError as exc:
            message = str(exc)
        except psycopg.errors.QueryCanceled as exc:
            pytest.fail(
                f"the DROP waited on the reader's ACCESS SHARE lock with no bound of its "
                f"own and was stopped only by the {GATE_STATEMENT_TIMEOUT} "
                f"statement_timeout this test sets. Unbounded, it blocks forever while "
                f"holding the migration advisory lock, which stalls every other session on "
                f"this database too. Set lock_timeout before the drop. Server said: {exc}"
            )
        else:
            pytest.fail(
                "the DROP succeeded while another session held ACCESS SHARE on "
                "ledger.commerce_events, which Postgres does not permit -- the premise of "
                "this test is not set up"
            )
        finally:
            elapsed = time.monotonic() - started
            with pg_admin.cursor() as cur:
                cur.execute("reset statement_timeout")
    finally:
        reader.rollback()
        reader.close()
        if _present_schemas(pg_admin) != sorted(OWNED_SCHEMAS):
            _restore_owned_schemas(pg_admin)

    assert elapsed < 8.0, (
        f"the blocked drop took {elapsed:.1f}s to give up. It is holding the migration "
        f"advisory lock the whole time, so that is {elapsed:.1f}s in which no other session "
        f"on this database can migrate either."
    )
    assert "lock" in message.lower(), message
    assert _present_schemas(pg_admin) == before, (
        "the drop timed out and still tore the schemas down. The whole point of running it "
        "inside a transaction is that a drop that cannot complete leaves nothing behind."
    )
    assert _migration_lock_holders(pg_admin) == 0, (
        "the rebuild gave up on the drop and kept the migration advisory lock, which stalls "
        "the next session that tries to rebuild"
    )


@pytest.mark.docker("postgres")
def test_the_schema_rebuild_refuses_to_drop_while_another_session_holds_the_migration_lock(
    pg_admin: Any,
    worker_database: str,
    worker_index: int,
) -> None:
    """Two sessions against one database must not overlap their drop windows.

    ``apply_migrations`` already serialises itself on a per-database advisory lock, but the
    ``DROP SCHEMA`` in front of it took no lock at all -- so a second session could tear the
    schemas out while the first was half way through re-creating them, and the first would
    report success over a database missing whatever the second had just removed. The rebuild
    now takes the same lock the migrations take, before it drops anything.

    Asserted the only way that means anything: hold the lock from another session and check
    that the rebuild **gives up with the schemas still standing** rather than dropping them.

    **Why the ``statement_timeout``.** This test hands its ``blocker`` connection to a
    ``finally`` that cannot run until ``rebuild_owned_schemas`` returns. A rebuild that
    *blocks* on the key the blocker holds -- which is what the pre-fix code does, via
    ``apply_migrations``' unconditional ``pg_advisory_lock`` -- therefore waits on a lock
    only this test can release, and this test cannot release it until the rebuild returns.
    Measured with the fix reverted: the run never returned, ``pg_stat_activity`` showed the
    rebuild parked in ``select pg_advisory_lock($1)`` with the four schemas already dropped,
    and only an external ``timeout 180`` ended it. Capping the statement server-side turns
    that permanent hang into a red test in ten seconds, and the teardown below puts the
    schemas back either way. A gate that hangs reports nothing and poisons the database it
    hangs on; it is a worse outcome than having no gate.
    """
    import psycopg

    from apps.trust.src.ledger.migrations import MIGRATION_LOCK_KEY
    from apps.trust.tests._fixtures_ledger_schema import (
        LedgerSchemaRebuildError,
        rebuild_owned_schemas,
    )
    from proxyshop_support.postgres import role_dsn

    rebuild_owned_schemas(pg_admin)
    before = _present_schemas(pg_admin)
    assert before == sorted(OWNED_SCHEMAS), f"setup failed: only {before} are present"

    blocker = psycopg.connect(
        role_dsn("admin", worker_index, database=worker_database),
        autocommit=True,
        connect_timeout=5,
    )
    message = ""
    try:
        with blocker.cursor() as cur:
            cur.execute("select pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        with pg_admin.cursor() as cur:
            cur.execute(f"set statement_timeout = '{GATE_STATEMENT_TIMEOUT}'")  # noqa: S608
        started = time.monotonic()
        try:
            rebuild_owned_schemas(pg_admin, lock_timeout=1.0)
        except LedgerSchemaRebuildError as exc:
            message = str(exc)
        except psycopg.errors.QueryCanceled as exc:
            pytest.fail(
                f"the rebuild BLOCKED on the migration advisory lock instead of refusing to "
                f"start, and was only stopped by the {GATE_STATEMENT_TIMEOUT} "
                f"statement_timeout this test sets. Without that cap it waits forever on a "
                f"lock this test can only release after it returns -- a permanent deadlock "
                f"that leaves this worker's database with no owned schemas at all. "
                f"Server said: {exc}"
            )
        else:
            pytest.fail(
                "the rebuild returned normally while another session held the migration "
                "advisory lock, so it dropped and re-applied inside that session's window"
            )
        finally:
            elapsed = time.monotonic() - started
            with pg_admin.cursor() as cur:
                cur.execute("reset statement_timeout")
    finally:
        blocker.close()
        if _present_schemas(pg_admin) != sorted(OWNED_SCHEMAS):
            _restore_owned_schemas(pg_admin)

    assert elapsed < 8.0, (
        f"the rebuild took {elapsed:.1f}s to give up on a lock it was told to wait 1.0s for"
    )
    assert "lock" in message.lower(), message
    assert _present_schemas(pg_admin) == before, (
        "the rebuild dropped the four owned schemas while another session held the "
        "migration lock. That is the window a concurrently running test falls into and "
        "reports as InvalidSchemaName."
    )
    assert _migration_lock_holders(pg_admin) == 0, (
        "the blocker's lock was released with the close() above, so anything still held "
        "here is the rebuild's own -- leaked on the path where it gives up"
    )


@pytest.mark.docker("postgres")
def test_the_schema_rebuild_fails_loudly_when_the_migrations_leave_no_ledger_behind(
    pg_admin: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuild that drops and then does not rebuild must raise, not return quietly.

    This is the failure mode that leaves a persistent worker database missing the tables
    every later session assumes -- and therefore the one that arms the flake for everything
    that comes after. Simulated by pointing the migration runner at a *truncated* migration
    set: enough to bootstrap its own bookkeeping table and report four green filenames, and
    nothing else. That is exactly the shape a half-applied or mis-pathed set has from the
    fixture's point of view, and it is the shape the fixture used to accept in silence --
    measured, in that fixture's own docstring: with the runner stubbed out entirely, 61 of
    62 of this package's tests stayed green.
    """
    from apps.trust.src.ledger.migrations import MIGRATIONS_DIR_ENV
    from apps.trust.tests._fixtures_ledger_schema import (
        LedgerSchemaRebuildError,
        rebuild_owned_schemas,
    )

    # The bookkeeping table only -- `apply_migrations` records every file it applies into
    # `ledger.schema_migrations`, so a set that created literally nothing could not even
    # report success. This one succeeds and still produces no ledger.commerce_events.
    (tmp_path / "0001_bookkeeping_only.sql").write_text(
        "CREATE SCHEMA IF NOT EXISTS ledger;\n"
        "CREATE TABLE IF NOT EXISTS ledger.schema_migrations (\n"
        "  filename    text        PRIMARY KEY,\n"
        "  checksum    text        NOT NULL,\n"
        "  applied_at  timestamptz NOT NULL DEFAULT now()\n"
        ");\n",
        encoding="utf-8",
    )
    rebuild_owned_schemas(pg_admin)
    fingerprint_before = catalog_fingerprint(pg_admin)

    monkeypatch.setenv(MIGRATIONS_DIR_ENV, str(tmp_path))
    try:
        with pytest.raises(LedgerSchemaRebuildError) as excinfo:
            rebuild_owned_schemas(pg_admin)
        # Before any teardown: the failing rebuild must have left the database as it found
        # it. The drop and the re-apply share one transaction, so a rebuild that raises
        # rolls its own drop back -- there is no half-torn-down state for the next session
        # to inherit, and nothing for a teardown to have to repair.
        survived = _present_schemas(pg_admin)
        survived_fingerprint = catalog_fingerprint(pg_admin)
    finally:
        monkeypatch.delenv(MIGRATIONS_DIR_ENV, raising=False)
        if _present_schemas(pg_admin) != sorted(OWNED_SCHEMAS):
            _restore_owned_schemas(pg_admin)

    assert "ledger" in str(excinfo.value), (
        "the rebuild returned a database with no ledger schema and said nothing about it"
    )
    assert survived == sorted(OWNED_SCHEMAS), (
        f"the failed rebuild committed its DROP: only {survived} are left. That is the "
        f"state that arms the flake for every session after this one."
    )
    assert survived_fingerprint == fingerprint_before, (
        "the failed rebuild rolled back the schemas but not their contents"
    )
    assert _migration_lock_holders(pg_admin) == 0, (
        "the rebuild raised and kept the migration advisory lock"
    )
    assert _present_schemas(pg_admin) == sorted(OWNED_SCHEMAS)
