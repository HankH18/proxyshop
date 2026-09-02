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


@pytest.fixture(scope="session")
def ledger_migrated(pg_admin: Any) -> str:
    """Rebuild T-011's four schemas from ``db/migrations/*.sql``, once per session.

    **Why it drops first.** Every migration is ``CREATE ... IF NOT EXISTS``, and
    ``proxyshop_w<N>`` keeps its schema between runs -- so a fixture that only *applied* the
    migrations was grading whatever the database happened to already contain. Measured: with
    ``apply_migrations`` stubbed to execute nothing at all, 61 of 62 tests stayed green,
    including every schema, foreign-key, index, grant and chain test. Three of this ticket's
    own acceptance criteria could then be broken in the SQL with nothing turning red --
    dropping the ``ledger.claims`` foreign key, making ``app.bid_nonces`` globally unique on
    ``nonce`` instead of per signer, keying ``app.seller_endpoints`` on ``key_id`` alone --
    each verified to kill zero tests. Those are exactly the properties the ticket exists to
    establish.

    Dropping first makes the schema in front of every test the schema *this checkout's SQL
    produces*. The blast radius is this worker's own database and only the four schemas this
    ticket owns; ``db/init/00-roles.sql`` and the cluster-global roles are untouched, and
    ``DROP SCHEMA`` also clears the per-grantor ``pg_default_acl`` rows, so the default
    privileges are freshly graded too.

    Returns:
        The space-separated filenames applied -- used only in failure messages.
    """
    from apps.trust.src.ledger import apply_migrations

    with pg_admin.cursor() as cur:
        cur.execute(f"drop schema if exists {', '.join(OWNED_SCHEMAS)} cascade")
    return " ".join(apply_migrations(pg_admin))


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
