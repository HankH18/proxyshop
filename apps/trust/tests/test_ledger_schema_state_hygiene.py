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
The test declares the schema it needs                      :func:`test_the_live_trust_rw_
                                                           test_declares_the_migration_
                                                           fixture_it_depends_on`
...and survives a database in the drop window              :func:`test_the_live_trust_rw_
                                                           test_is_not_graded_against_an_
                                                           unmigrated_database`
The rebuild will not drop under a concurrent migration     :func:`test_the_schema_rebuild_
                                                           refuses_to_drop_while_another_
                                                           session_holds_the_migration_lock`
...and refuses to hand back a database it did not build    :func:`test_the_schema_rebuild_
                                                           fails_loudly_when_the_migrations_
                                                           leave_no_ledger_behind`
=========================================================  ================================
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.trust.tests._fixtures_ledger_schema import OWNED_SCHEMAS

#: The test this ticket is about, as a pytest node id relative to the repo root.
TARGET_NODEID = (
    "apps/trust/tests/test_events_hardening.py::"
    "test_the_writer_connects_to_the_real_database_as_trust_rw_from_the_per_role_var"
)

#: Any one of these, requested by a test, guarantees the migrations have been applied.
MIGRATION_FIXTURES = frozenset({"ledger_migrated", "ledger_clean"})


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
    try:
        with blocker.cursor() as cur:
            cur.execute("select pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        with pytest.raises(LedgerSchemaRebuildError) as excinfo:
            rebuild_owned_schemas(pg_admin, lock_timeout=1.0)
    finally:
        blocker.close()

    assert "already" in str(excinfo.value) or "lock" in str(excinfo.value).lower()
    assert _present_schemas(pg_admin) == before, (
        "the rebuild dropped the four owned schemas while another session held the "
        "migration lock. That is the window a concurrently running test falls into and "
        "reports as InvalidSchemaName."
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
    monkeypatch.setenv(MIGRATIONS_DIR_ENV, str(tmp_path))
    try:
        with pytest.raises(LedgerSchemaRebuildError) as excinfo:
            rebuild_owned_schemas(pg_admin)
    finally:
        monkeypatch.delenv(MIGRATIONS_DIR_ENV, raising=False)
        _restore_owned_schemas(pg_admin)

    assert "ledger" in str(excinfo.value), (
        "the rebuild returned a database with no ledger schema and said nothing about it"
    )
    assert _present_schemas(pg_admin) == sorted(OWNED_SCHEMAS)
