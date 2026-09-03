"""Fixtures for the T-011 schema, grant and ledger-chain tests.

Owned by T-011. Every name here is prefixed ``ledger_`` because ``apps/trust/tests/`` is
shared with T-060, T-062 and T-065 and ``proxyshop_support.fixture_loader`` binds one name
to one fixture across the whole directory.

CARRY-FORWARD CF-4 -- the idle-in-transaction deadlock, and the pattern that avoids it
--------------------------------------------------------------------------------------
``pg_admin`` (root ``conftest.py``:167-190) is **autocommit**. ``pg_role``
(``conftest.py``:194-236) hands back a connection in **default transactional mode**, cached
per role for the whole test. So the canonical privilege assertion --

.. code-block:: python

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg_role("exchange").cursor().execute("select * from sealed.envelopes")

-- leaves that connection **idle in transaction, holding an ``ACCESS SHARE`` lock**. Issue
``TRUNCATE`` / ``DROP`` / ``ALTER`` through ``pg_admin`` after it and the admin statement
blocks until ``lock_timeout``. It does not raise; it *hangs*, which reads as a stuck test
rather than as a lock conflict, and there is nothing in the failure output to point at.

The rule is: **roll back or close the pg_role connection before touching DDL through
pg_admin.** :class:`LedgerRoleRunner` below enforces it structurally rather than by
convention -- every one of its methods rolls the role connection back before it returns, so
a test that goes through it can never leave one open. ``.reset()`` is there for the case a
test opens a role connection by hand. T-011 is the first ticket to combine both fixtures;
around ten later ones depend on ``pg_role``, so the pattern is written down here rather than
in a commit message.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest

#: Tables reset between tests. Order does not matter -- ``CASCADE`` handles the FKs -- but
#: the list does: a table missing from it keeps rows across tests, and a chain that starts
#: from a non-empty table is a different chain.
LEDGER_TABLES = (
    "ledger.commerce_events",
    "ledger.claims",
    "ledger.claim_verifications",
    "ledger.verification_evidence_refs",
    "ledger.trust_observations",
    "ledger.trust_scores",
    "ledger.policy_events",
    "ledger.ranking_runs",
    "ledger.ranking_candidates",
    "ledger.catalog_snapshots",
    "ledger.crawl_jobs",
    "ledger.crawl_pages",
)

APP_TABLES = (
    "app.bid_nonces",
    "app.seller_endpoints",
    "app.seller_blacklist",
    "app.offers",
    "app.pitches",
    "app.pitch_requests",
    "app.intents",
    "app.buyer_accounts",
    "app.sellers",
)

SEALED_TABLES = (
    "sealed.envelopes",
    "sealed.learned_policy",
    "sealed.interview_transcripts",
    "sealed.shadow_bids",
)

VAULT_TABLES = (
    "vault.payment_methods",
    "vault.pseudonym_history",
)


class LedgerRoleRunner:
    """Run statements as a least-privilege role, never leaving a transaction open (CF-4).

    Every method commits or rolls back before returning, so no ``pg_admin`` DDL that follows
    can block on a lock this object left behind.
    """

    def __init__(self, connect: Callable[[str], Any]) -> None:
        self._connect = connect
        self._roles: set[str] = set()

    def connection(self, role: str) -> Any:
        """The cached connection for ``role``. Prefer the methods below to using it raw."""
        self._roles.add(role)
        return self._connect(role)

    def reset(self) -> None:
        """Roll back every role connection this runner has opened.

        Call it before any ``pg_admin`` DDL in a test that also touched a role connection
        directly. The methods below do it for you.
        """
        for role in self._roles:
            self._connect(role).rollback()

    def fetch(self, role: str, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
        """Execute ``sql`` as ``role`` and return every row. Rolls back afterwards."""
        connection = self.connection(role)
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        finally:
            connection.rollback()

    def execute(self, role: str, sql: str, params: Sequence[Any] | None = None) -> None:
        """Execute a non-returning statement as ``role``. Rolls back afterwards.

        The rollback is deliberate: these tests assert on *permission*, and a test that also
        left data behind would make the next test's assertions depend on run order.
        """
        connection = self.connection(role)
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
        finally:
            connection.rollback()

    def denied(self, role: str, sql: str, params: Sequence[Any] | None = None) -> Exception:
        """Assert ``sql`` is refused for ``role``, and return the exception.

        Raises:
            AssertionError: the statement **succeeded**. A privilege test that passes
                because the relation does not exist, or because the role could actually
                read it, is worse than no test at all -- so the message says which.
        """
        import psycopg

        connection = self.connection(role)
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
        except psycopg.errors.InsufficientPrivilege as exc:
            return exc
        except psycopg.Error as exc:  # e.g. UndefinedTable -- not a privilege denial
            raise AssertionError(
                f"as {role!r}, {sql!r} failed with {type(exc).__name__} rather than "
                f"InsufficientPrivilege: {exc}. That is not the denial this asserts."
            ) from exc
        else:
            raise AssertionError(
                f"as {role!r}, {sql!r} SUCCEEDED. The grant model leaks: this role must "
                f"not be able to run it."
            )
        finally:
            connection.rollback()


#: The schemas T-011 owns end to end. Dropped and rebuilt once per session -- see below.
OWNED_SCHEMAS = ("ledger", "sealed", "vault", "app")

#: One table per owned schema that the migrations MUST have produced. Checked after every
#: rebuild -- see :func:`rebuild_owned_schemas`.
REBUILD_WITNESS_TABLES = (
    "ledger.commerce_events",
    "sealed.envelopes",
    "vault.payment_methods",
    "app.sellers",
)

#: How long :func:`rebuild_owned_schemas` waits for the migration advisory lock before it
#: gives up. Generous, because the thing it waits on is another session applying four SQL
#: files; finite, because a wait with no end is a hung unattended build.
REBUILD_LOCK_TIMEOUT_SECONDS = 60.0


class LedgerSchemaRebuildError(RuntimeError):
    """The four owned schemas could not be rebuilt, or were not there afterwards."""


def rebuild_owned_schemas(
    pg_admin: Any, *, lock_timeout: float = REBUILD_LOCK_TIMEOUT_SECONDS
) -> list[str]:
    """Drop and re-apply T-011's four schemas, holding the migration lock the whole time.

    **Why the lock (T-216).** ``DROP SCHEMA ... CASCADE`` followed by four migration files is
    not an instant, it is a *window*, and for as long as it is open the ``ledger`` schema
    genuinely does not exist. Any other session on this same database that touches
    ``ledger.*`` in that window gets
    ``psycopg.errors.InvalidSchemaName: schema "ledger" does not exist`` -- and then passes
    on a retry seconds later, against a database in which the schema demonstrably exists.
    That is the whole shape of the flake this function was extracted to close.

    :func:`apply_migrations` already serialises itself on ``MIGRATION_LOCK_KEY``, a
    session-level advisory lock whose tag carries the database OID. The bare drop in front of
    it took no lock at all, so it could land in the middle of another session's migration
    run. Taking the *same* key here, before dropping, makes drop-plus-apply one indivisible
    step as far as anything else that respects that lock is concerned. Advisory locks are
    re-entrant per session, so ``apply_migrations`` taking it again below is a no-op.

    ``pg_try_advisory_lock`` in a bounded poll rather than the blocking ``pg_advisory_lock``:
    a blocking wait is not covered by ``lock_timeout`` (that governs heavyweight locks only),
    so a stuck holder would hang the session with no diagnostic.

    **Why it still drops.** Every migration is ``CREATE ... IF NOT EXISTS`` and
    ``proxyshop_w<N>`` keeps its schema between runs -- so a fixture that only *applied* the
    migrations was grading whatever the database happened to already contain. Measured: with
    ``apply_migrations`` stubbed to execute nothing at all, 61 of 62 tests stayed green,
    including every schema, foreign-key, index, grant and chain test. Three of T-011's own
    acceptance criteria could then be broken in the SQL with nothing turning red -- dropping
    the ``ledger.claims`` foreign key, making ``app.bid_nonces`` globally unique on ``nonce``
    instead of per signer, keying ``app.seller_endpoints`` on ``key_id`` alone -- each
    verified to kill zero tests. Those are exactly the properties the ticket exists to
    establish. Dropping first makes the schema in front of every test the schema *this
    checkout's SQL produces*.

    The blast radius is this worker's own database and only the four schemas T-011 owns;
    ``db/init/00-roles.sql`` and the cluster-global roles are untouched, and ``DROP SCHEMA``
    also clears the per-grantor ``pg_default_acl`` rows, so the default privileges are
    freshly graded too.

    Args:
        pg_admin: an **autocommit** superuser connection to this worker's database.
        lock_timeout: seconds to wait for the migration advisory lock.

    Returns:
        The migration filenames applied, in order.

    Raises:
        LedgerSchemaRebuildError: the lock could not be taken inside ``lock_timeout``
            (nothing was dropped), or the migrations returned without producing the schemas.
            The second case is the one that arms the flake for every session that follows:
            a database left with no ledger schema at all looks exactly like a fresh one.
    """
    from apps.trust.src.ledger import apply_migrations
    from apps.trust.src.ledger.migrations import MIGRATION_LOCK_KEY

    deadline = time.monotonic() + lock_timeout
    while True:
        with pg_admin.cursor() as cur:
            cur.execute("select pg_try_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
            acquired = bool(cur.fetchone()[0])
        if acquired:
            break
        if time.monotonic() >= deadline:
            raise LedgerSchemaRebuildError(
                f"another session on this database has held the migration advisory lock "
                f"({MIGRATION_LOCK_KEY}) for more than {lock_timeout:g}s, so the four owned "
                f"schemas were NOT dropped. Dropping anyway would tear them out from under "
                f"whatever that session is doing, which is exactly the InvalidSchemaName "
                f"flake T-216 closed. Two pytest sessions sharing one proxyshop_w<N> means "
                f"two runs share a $PROXYSHOP_WORKER (D38)."
            )
        time.sleep(0.05)

    try:
        with pg_admin.cursor() as cur:
            cur.execute(f"drop schema if exists {', '.join(OWNED_SCHEMAS)} cascade")
        applied = apply_migrations(pg_admin)
        missing = _absent_witnesses(pg_admin)
        if missing:
            raise LedgerSchemaRebuildError(
                f"the migrations ran ({', '.join(applied) or 'nothing applied'}) but "
                f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not there "
                f"afterwards. The four owned schemas were dropped and not rebuilt, so this "
                f"worker's database is now indistinguishable from a fresh one and every "
                f"later session that assumes a ledger schema will fail."
            )
    finally:
        with pg_admin.cursor() as cur:
            cur.execute("select pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    return applied


def _absent_witnesses(pg_admin: Any) -> list[str]:
    """Which of :data:`REBUILD_WITNESS_TABLES` the catalog does not hold, in order."""
    with pg_admin.cursor() as cur:
        cur.execute(
            "select ns.nspname || '.' || cl.relname from pg_class cl "
            "  join pg_namespace ns on ns.oid = cl.relnamespace "
            " where ns.nspname = any(%s) and cl.relkind = 'r'",
            (list(OWNED_SCHEMAS),),
        )
        present = {row[0] for row in cur.fetchall()}
    return [name for name in REBUILD_WITNESS_TABLES if name not in present]


@pytest.fixture(scope="session")
def ledger_migrated(pg_admin: Any) -> str:
    """Rebuild T-011's four schemas from ``db/migrations/*.sql``, once per session.

    Every test that reads or writes ``ledger.*``, ``sealed.*``, ``vault.*`` or ``app.*`` over
    a real connection must request this fixture (or :func:`ledger_clean`, which builds on
    it) **even when it only reads**. Requesting it is the only thing that makes the schema
    exist on purpose rather than by inheritance from whatever ran last; a test that skips the
    declaration is graded against the persistent ``proxyshop_w<N>`` database's ambient state,
    and is therefore invisible until that state changes (T-216).

    All the work, and the reasoning behind the drop and the lock, is in
    :func:`rebuild_owned_schemas`.

    Returns:
        The space-separated filenames applied -- used only in failure messages.
    """
    return " ".join(rebuild_owned_schemas(pg_admin))


@pytest.fixture
def ledger_clean(ledger_migrated: str, pg_admin: Any) -> Iterator[Any]:
    """A migrated database with every T-011 table empty, and the sequences restarted.

    Truncation happens at **setup**, before the test body has had a chance to open a
    ``pg_role`` connection, which is what keeps it clear of CF-4. A test that truncates
    again mid-body must call ``ledger_roles.reset()`` first.
    """
    tables = ", ".join((*LEDGER_TABLES, *APP_TABLES, *SEALED_TABLES, *VAULT_TABLES))
    with pg_admin.cursor() as cur:
        cur.execute(f"truncate table {tables} restart identity cascade")  # noqa: S608
    yield pg_admin


@pytest.fixture
def ledger_roles(pg_role: Callable[[str], Any]) -> Iterator[LedgerRoleRunner]:
    """A :class:`LedgerRoleRunner` over the ``pg_role`` factory (CF-4)."""
    runner = LedgerRoleRunner(pg_role)
    try:
        yield runner
    finally:
        runner.reset()


def catalog_fingerprint(connection: Any, schemas: Sequence[str] = OWNED_SCHEMAS) -> dict[str, Any]:
    """A structural fingerprint of ``schemas``: tables, columns, constraints, indexes, triggers.

    Everything the migrations are supposed to produce, read back out of the live catalog
    rather than out of a hand-written model -- so a mutation to the SQL changes the
    fingerprint, and an assertion on it is an assertion about the SQL.
    """
    names = tuple(schemas)
    out: dict[str, Any] = {}
    with connection.cursor() as cur:
        cur.execute(
            "select table_schema || '.' || table_name || '.' || column_name || ':' || "
            "       data_type || case when is_nullable = 'NO' then '!' else '' end "
            "  from information_schema.columns where table_schema = any(%s)",
            (list(names),),
        )
        out["columns"] = sorted(row[0] for row in cur.fetchall())
        cur.execute(
            "select ns.nspname || '.' || cl.relname || '.' || c.conname || ':' || c.contype::text "
            "       || ':' || pg_get_constraintdef(c.oid) "
            "  from pg_constraint c "
            "  join pg_class cl on cl.oid = c.conrelid "
            "  join pg_namespace ns on ns.oid = cl.relnamespace "
            " where ns.nspname = any(%s)",
            (list(names),),
        )
        out["constraints"] = sorted(row[0] for row in cur.fetchall())
        cur.execute(
            "select schemaname || '.' || indexname || ':' || indexdef from pg_indexes "
            " where schemaname = any(%s)",
            (list(names),),
        )
        out["indexes"] = sorted(row[0] for row in cur.fetchall())
        cur.execute(
            "select ns.nspname || '.' || cl.relname || '.' || t.tgname "
            "  from pg_trigger t "
            "  join pg_class cl on cl.oid = t.tgrelid "
            "  join pg_namespace ns on ns.oid = cl.relnamespace "
            " where ns.nspname = any(%s) and not t.tgisinternal",
            (list(names),),
        )
        out["triggers"] = sorted(row[0] for row in cur.fetchall())
    return out


@pytest.fixture
def ledger_second_database(pg_admin: Any, worker_index: int) -> Iterator[str]:
    """A throwaway database on the same cluster, dropped at teardown (D39).

    Named with this worker's index and a random suffix so five workers running at once never
    collide, and created from ``template1`` rather than from ``proxyshop_template`` -- the
    point of the exercise is that the migrations bring a *bare* database up, without help
    from a template that already ran ``db/init/00-roles.sql``.
    """
    name = f"proxyshop_w{worker_index}_migrationcheck_{uuid.uuid4().hex[:8]}"
    with pg_admin.cursor() as cur:
        cur.execute(f'create database "{name}" template template1')
    try:
        yield name
    finally:
        with pg_admin.cursor() as cur:
            cur.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (name,),
            )
            cur.execute(f'drop database if exists "{name}"')
