"""T-011: the Postgres schemas, the role isolation that S7 turns on, and the D35 proof.

Owned by T-011. What each block establishes:

* **Acceptance 1** -- the migrations apply, they apply *again* into a second database on the
  same cluster (D39), and every foreign key and every blacklist / idempotency index the
  table set promises is really there.
* **Acceptance 2 / C3 / S7** -- ``exchange`` reads ``ledger`` and ``app`` and is refused on
  ``sealed`` and ``vault``. Both directions, because a denial test on a role that can read
  nothing at all is not evidence of isolation.
* **Acceptance 4 / D52** -- the two ``app.*`` structures no frozen acceptance test grades:
  nonces scoped per signer, and one endpoint row per live key.
* **D35** -- the import-lint gate T-000 wired into ``make verify`` actually fires, plus the
  CF-2 regression guard on the ``redis.exceptions`` carve-out.

The privilege assertions all go through the ``ledger_roles`` runner rather than raw
``pg_role`` connections. That is carry-forward CF-4: a ``pytest.raises`` on a role connection
leaves it idle-in-transaction holding an ``ACCESS SHARE`` lock, and the next ``pg_admin``
DDL then blocks until ``lock_timeout`` -- a hang, not an error. See
``_fixtures_ledger_schema.py`` for the pattern.
"""

from __future__ import annotations

import configparser
import contextlib
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from apps.trust.src.ledger import migrations as migration_lib

REPO_ROOT = Path(__file__).resolve().parents[3]
LINT_FIXTURES = REPO_ROOT / "apps" / "trust" / "tests" / "lint_fixtures"
DB_INIT_ROLES_SQL = REPO_ROOT / "db" / "init" / "00-roles.sql"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

#: The partner-reconciled table set, by schema (DESIGN §Data models).
EXPECTED_TABLES = {
    "ledger": {
        "commerce_events",
        "claims",
        "claim_verifications",
        "verification_evidence_refs",
        "trust_observations",
        "trust_scores",
        "policy_events",
        "ranking_runs",
        "ranking_candidates",
        "catalog_snapshots",
        "crawl_jobs",
        "crawl_pages",
        "chain_head",
        "schema_migrations",
    },
    "sealed": {"envelopes", "learned_policy", "interview_transcripts", "shadow_bids"},
    "vault": {"pseudonym_history", "payment_methods"},
    "app": {
        "sellers",
        "seller_endpoints",
        "seller_blacklist",
        "bid_nonces",
        "buyer_accounts",
        "intents",
        "pitch_requests",
        "pitches",
        "offers",
    },
}

#: Foreign keys the table set promises. ``(schema.table, column, schema.referenced_table)``.
EXPECTED_FOREIGN_KEYS = {
    ("ledger.claims", "event_seq", "ledger.commerce_events"),
    ("ledger.claim_verifications", "claim_id", "ledger.claims"),
    ("ledger.claim_verifications", "catalog_snapshot_id", "ledger.catalog_snapshots"),
    ("ledger.verification_evidence_refs", "verification_id", "ledger.claim_verifications"),
    ("ledger.trust_observations", "event_seq", "ledger.commerce_events"),
    ("ledger.trust_observations", "verification_id", "ledger.claim_verifications"),
    ("ledger.trust_scores", "computed_through_event", "ledger.commerce_events"),
    ("ledger.policy_events", "event_seq", "ledger.commerce_events"),
    ("ledger.ranking_candidates", "run_id", "ledger.ranking_runs"),
    ("ledger.crawl_pages", "job_id", "ledger.crawl_jobs"),
    ("vault.payment_methods", "pseudonym", "vault.pseudonym_history"),
    ("app.seller_endpoints", "store_id", "app.sellers"),
    ("app.seller_blacklist", "store_id", "app.sellers"),
    ("app.pitch_requests", "intent_id", "app.intents"),
    ("app.pitch_requests", "store_id", "app.sellers"),
    ("app.pitches", "pitch_request_id", "app.pitch_requests"),
    ("app.offers", "pitch_id", "app.pitches"),
}


def _venv_bin(name: str) -> Path:
    return REPO_ROOT / ".venv" / "bin" / name


def _run_lint_imports(config: Path, *, pythonpath: Path) -> subprocess.CompletedProcess[str]:
    """Run ``lint-imports`` against ``config`` in a subprocess, and return the result.

    A subprocess and not an in-process call on purpose: D35's criterion is that *the check
    exits non-zero*, and the exit status is the thing ``make verify`` reads.

    T-122: ``pythonpath`` is APPENDED to whatever the parent had rather than replacing it,
    and every real call site names ``REPO_ROOT / ".pkgroot"`` explicitly, so the child can
    reach the flat package spellings whether or not the venv's ``_proxyshop.pth`` applies.
    The two lint fixture configs pass their own directory on purpose — they are graded on a
    contrived import graph, not on this repo's.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{pythonpath}{os.pathsep}{existing}" if existing else str(pythonpath)
    return subprocess.run(
        [str(_venv_bin("lint-imports")), "--config", str(config)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


# =======================================================================================
# Acceptance 1 -- the migrations apply, twice, into two databases
# =======================================================================================


@pytest.mark.docker
def test_migrations_apply_and_record_every_file(ledger_migrated: str, pg_admin) -> None:
    """Every ``db/migrations/*.sql`` file applied, and each is recorded with its checksum."""
    files = [path.name for path in migration_lib.migration_files()]
    assert files, "db/migrations holds no .sql files"
    recorded = dict(migration_lib.applied_migrations(pg_admin))
    assert set(files) <= set(recorded), (
        f"applied {ledger_migrated!r} but {sorted(set(files) - set(recorded))} are not "
        f"recorded in {migration_lib.SCHEMA_MIGRATIONS_TABLE}"
    )
    for path in migration_lib.migration_files():
        assert recorded[path.name] == migration_lib.checksum(path), (
            f"{path.name} was recorded with a different checksum than the file now has"
        )


@pytest.mark.docker
def test_migrations_are_idempotent_in_the_same_database(ledger_migrated: str, pg_admin) -> None:
    """Re-applying the whole set is a clean no-op -- structurally, and for the data.

    "Did not raise" is not idempotence, and asserting it was worse than nothing: the old
    form compared ``apply_migrations``'s return value to ``migration_files()``, which is the
    list ``apply_migrations`` builds it from -- ``f(x) == x``. Prepending a
    ``DROP TABLE ... CASCADE`` to a migration, so every apply destroyed and rebuilt a table,
    left the whole suite green.

    So this compares a full catalog fingerprint across the re-run, and checks that a row
    written before it is still there afterwards. A destructive migration fails both.
    """
    from apps.trust.tests._fixtures_ledger_schema import catalog_fingerprint

    with pg_admin.cursor() as cur:
        cur.execute(
            "insert into app.sellers (store_id, domain, business_identity, tier) "
            "values ('idempotency-probe', 'p.example', 'bi-probe', 'external') "
            "on conflict (store_id) do nothing"
        )
    before = catalog_fingerprint(pg_admin)

    again = migration_lib.apply_migrations(pg_admin)
    assert again == [path.name for path in migration_lib.migration_files()]

    after = catalog_fingerprint(pg_admin)
    for section in ("columns", "constraints", "indexes", "triggers"):
        assert after[section] == before[section], (
            f"re-applying the migrations changed the {section} of the schema"
        )
    with pg_admin.cursor() as cur:
        cur.execute("select count(*) from app.sellers where store_id = 'idempotency-probe'")
        assert cur.fetchone() == (1,), "re-applying the migrations destroyed existing data"
        cur.execute("delete from app.sellers where store_id = 'idempotency-probe'")


@pytest.mark.docker
def test_a_migration_edited_after_it_was_applied_is_detectable(
    ledger_migrated: str, pg_admin, tmp_path: Path
) -> None:
    """The recorded checksum has to mean something.

    It used to be overwritten on every run, which erased the single thing it exists to
    detect, and the test that "checked" it compared ``recorded[name]`` against
    ``checksum(path)`` -- the value ``_record`` had just written. Now the first-applied
    checksum is kept, ``drifted_migrations`` reports the difference, and ``strict=True``
    refuses to apply over it.
    """
    edited = tmp_path / "0001_schemas_roles_grants.sql"
    original = migration_lib.migrations_dir() / "0001_schemas_roles_grants.sql"
    edited.write_text(original.read_text(encoding="utf-8") + "\n-- an edit\n", encoding="utf-8")

    assert migration_lib.drifted_migrations(pg_admin) == [], "the shipped files have not drifted"
    drift = migration_lib.drifted_migrations(pg_admin, files=[edited])
    assert [name for name, _, _ in drift] == ["0001_schemas_roles_grants.sql"]
    recorded, current = drift[0][1], drift[0][2]
    assert recorded == migration_lib.checksum(original)
    assert current == migration_lib.checksum(edited) != recorded

    with pytest.raises(migration_lib.MigrationDriftError) as raised:
        migration_lib.apply_migrations(pg_admin, files=[edited], strict=True)
    assert "0001_schemas_roles_grants.sql" in str(raised.value)


@pytest.mark.docker
def test_migrations_apply_into_a_second_database_on_the_same_cluster(
    ledger_second_database: str, worker_index: int
) -> None:
    """D39's acceptance criterion, in full.

    Roles are cluster-global, so the guarded ``CREATE ROLE`` in ``0001`` must not error the
    second time it runs against a cluster where the roles already exist -- and the GRANTs,
    which are per-database, must produce a *correct* copy in a database created from bare
    ``template1``, with no help from ``db/init/00-roles.sql``.
    """
    from proxyshop_support.postgres import role_dsn

    dsn = role_dsn("admin", worker_index, database=ledger_second_database)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as fresh:
        applied = migration_lib.apply_migrations(fresh)
        assert applied == [path.name for path in migration_lib.migration_files()]
        with fresh.cursor() as cur:
            cur.execute(
                "select nspname from pg_namespace where nspname in "
                "('ledger', 'sealed', 'vault', 'app')"
            )
            assert {row[0] for row in cur.fetchall()} == {"ledger", "sealed", "vault", "app"}
            # The grant model came with it, in this database's own catalog.
            cur.execute(
                "select has_schema_privilege('exchange', 'ledger', 'usage'), "
                "       has_schema_privilege('exchange', 'app', 'usage'), "
                "       has_schema_privilege('exchange', 'sealed', 'usage'), "
                "       has_schema_privilege('exchange', 'vault', 'usage'), "
                "       has_table_privilege('exchange', 'ledger.commerce_events', 'select'), "
                "       has_table_privilege('exchange', 'ledger.commerce_events', 'insert')"
            )
            row = cur.fetchone()
        assert row == (True, True, False, False, True, False), (
            f"the second database's grant model differs from the first: {row}"
        )

    # ...and it is idempotent there too.
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as again:
        assert migration_lib.apply_migrations(again)


@pytest.mark.docker
def test_a_bare_database_gets_structurally_the_same_schema(
    ledger_second_database: str, worker_index: int, ledger_migrated: str, pg_admin
) -> None:
    """Everything the migrations produce, compared column-for-constraint-for-index.

    The second-database test above checks four schema names and six privilege booleans, and
    reads no table, column, foreign key, index or trigger -- so it would not notice a
    migration that stopped creating half of them. This compares the complete catalog
    fingerprint of a database built from bare ``template1`` against the session database,
    which makes every structural promise in ``db/migrations`` an assertion.
    """
    from apps.trust.tests._fixtures_ledger_schema import catalog_fingerprint
    from proxyshop_support.postgres import role_dsn

    dsn = role_dsn("admin", worker_index, database=ledger_second_database)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as fresh:
        migration_lib.apply_migrations(fresh)
        built = catalog_fingerprint(fresh)
    reference = catalog_fingerprint(pg_admin)

    for section in ("columns", "constraints", "indexes", "triggers"):
        assert built[section] == reference[section], (
            f"a database built from bare template1 has different {section} than this "
            f"worker's: {sorted(set(reference[section]) ^ set(built[section]))[:10]}"
        )
    assert len(built["columns"]) > 100, "the fingerprint is suspiciously small to be meaningful"


# =======================================================================================
# Acceptance 1 -- schemas, tables, foreign keys, indexes
# =======================================================================================


@pytest.mark.docker
def test_every_expected_schema_and_table_exists(ledger_migrated: str, pg_admin) -> None:
    """The partner-reconciled table set, present in the four schemas DESIGN names."""
    with pg_admin.cursor() as cur:
        cur.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema in ('ledger', 'sealed', 'vault', 'app')"
        )
        found: dict[str, set[str]] = {}
        for schema, table in cur.fetchall():
            found.setdefault(schema, set()).add(table)
    for schema, expected in EXPECTED_TABLES.items():
        missing = expected - found.get(schema, set())
        assert not missing, f"{schema} is missing {sorted(missing)}"


@pytest.mark.docker
def test_every_expected_foreign_key_is_present(ledger_migrated: str, pg_admin) -> None:
    """Acceptance 1: "all FKs ... present"."""
    with pg_admin.cursor() as cur:
        cur.execute(
            """
            select ns.nspname || '.' || cl.relname,
                   att.attname,
                   fns.nspname || '.' || fcl.relname
              from pg_constraint c
              join pg_class cl       on cl.oid = c.conrelid
              join pg_namespace ns   on ns.oid = cl.relnamespace
              join pg_class fcl      on fcl.oid = c.confrelid
              join pg_namespace fns  on fns.oid = fcl.relnamespace
              join unnest(c.conkey) as k(attnum) on true
              join pg_attribute att  on att.attrelid = cl.oid and att.attnum = k.attnum
             where c.contype = 'f'
               and ns.nspname in ('ledger', 'sealed', 'vault', 'app')
            """
        )
        found = {(row[0], row[1], row[2]) for row in cur.fetchall()}
    missing = EXPECTED_FOREIGN_KEYS - found
    assert not missing, f"foreign keys missing: {sorted(missing)}"


@pytest.mark.docker
def test_blacklist_and_idempotency_indexes_are_present(ledger_migrated: str, pg_admin) -> None:
    """Acceptance 1: "blacklist/idempotency indexes present".

    Each entry names the property the index encodes, not just its name, so a rename that
    keeps the property is a one-line edit and a rename that loses it is a failure.
    """
    with pg_admin.cursor() as cur:
        cur.execute(
            "select schemaname || '.' || indexname, indexdef from pg_indexes "
            "where schemaname in ('ledger', 'app')"
        )
        indexes = dict(cur.fetchall())

    required = {
        # idempotency: LedgerEvent.event_id IS commerce_events.idempotency_key (D16)
        "ledger.commerce_events_idempotency_key_key": "unique",
        "ledger.commerce_events_event_hash_key": "unique",
        "ledger.commerce_events_prev_hash_key": "unique",
        # verification idempotency (T-065 acceptance 2)
        "ledger.claim_verifications_idempotency_key": "unique",
        # blacklist
        "app.seller_blacklist_identity_idx": "business_identity",
        "app.seller_blacklist_one_live_entry_idx": "unique",
        "app.seller_blacklist_expiry_idx": "expires_at",
        # D52
        "app.bid_nonces_signer_nonce_key": "(signer_id, nonce)",
        "app.seller_endpoints_live_idx": "signer_id",
    }
    for name, needle in required.items():
        assert name in indexes, f"missing index {name}; have {sorted(indexes)}"
        assert needle in indexes[name].lower(), (
            f"{name} exists but no longer encodes {needle!r}: {indexes[name]}"
        )
    assert "business_identity" in indexes["app.seller_blacklist_one_live_entry_idx"], (
        "the live-blacklist index must key on business_identity, not store_id: a "
        "re-registered store under a new store_id is the same business"
    )


# =======================================================================================
# Acceptance 2 / C3 / S7 -- the grant model, both directions
# =======================================================================================


@pytest.mark.docker
def test_exchange_can_read_ledger_and_app(ledger_clean, ledger_roles) -> None:
    """The positive control. Without it, every denial below could be vacuous.

    CF-3: this is the half D5's "schema-level USAGE only" wording gets wrong. USAGE alone
    resolves the name and then fails with `permission denied for table`; the migrations
    issue table-level SELECT as well.
    """
    assert ledger_roles.fetch("exchange", "select count(*) from ledger.commerce_events") == [(0,)]
    assert ledger_roles.fetch("exchange", "select count(*) from ledger.trust_scores") == [(0,)]
    assert ledger_roles.fetch("exchange", "select count(*) from app.sellers") == [(0,)]
    assert ledger_roles.fetch("exchange", "select count(*) from app.seller_blacklist") == [(0,)]


@pytest.mark.docker
def test_exchange_select_on_sealed_fails(ledger_clean, ledger_roles) -> None:
    """Acceptance 2, named verbatim in the ticket. S7's release blocker."""
    exc = ledger_roles.denied("exchange", "select * from sealed.envelopes")
    assert "sealed" in str(exc)
    for table in ("learned_policy", "interview_transcripts", "shadow_bids"):
        ledger_roles.denied("exchange", f"select * from sealed.{table}")  # noqa: S608


@pytest.mark.docker
def test_exchange_select_on_vault_fails(ledger_clean, ledger_roles) -> None:
    """The buyer identity schema is closed to the auction just as firmly (D5)."""
    exc = ledger_roles.denied("exchange", "select * from vault.payment_methods")
    assert "vault" in str(exc)
    ledger_roles.denied("exchange", "select * from vault.pseudonym_history")


@pytest.mark.docker
def test_the_denial_tests_name_relations_that_actually_exist(
    ledger_migrated: str, pg_admin
) -> None:
    """The backstop under every ``sealed``/``vault`` denial in this file.

    Postgres checks schema ``USAGE`` **before** it checks whether a relation exists, so
    ``select * from sealed.this_never_existed`` is refused with the very same
    ``InsufficientPrivilege`` as a real table. Every denial assertion here would therefore
    survive a renamed, misspelled or deleted table and keep reporting isolation. This is
    what makes those assertions mean what they say.
    """
    named = [
        ("sealed", "envelopes"),
        ("sealed", "learned_policy"),
        ("sealed", "interview_transcripts"),
        ("sealed", "shadow_bids"),
        ("vault", "payment_methods"),
        ("vault", "pseudonym_history"),
    ]
    with pg_admin.cursor() as cur:
        for schema, table in named:
            cur.execute(
                "select 1 from information_schema.tables "
                "where table_schema = %s and table_name = %s",
                (schema, table),
            )
            assert cur.fetchone() is not None, f"{schema}.{table} does not exist"
            cur.execute(f"select count(*) from {schema}.{table}")  # noqa: S608
            assert cur.fetchone() is not None


@pytest.mark.docker
def test_exchange_cannot_enumerate_the_closed_schemas_through_information_schema(
    ledger_clean, ledger_roles
) -> None:
    """D5, verified live: ``information_schema.tables`` returns **0 rows** for those schemas.

    A role that can list a table it cannot read still learns the schema. Postgres filters
    ``information_schema`` by privilege, so no-USAGE means no rows -- and that is a property
    of granting nothing, which a table-level REVOKE would not have given us.

    Scope, stated precisely because the test name used to over-claim: this is
    ``information_schema``, which is ACL-filtered. ``pg_catalog`` is **not**, and cannot be
    closed without breaking every client library, so relation names, column names and
    ``CHECK`` bodies in the closed schemas remain readable there by any role that can
    connect. Row *content* is what S7 protects and what the denials above establish; the
    metadata exposure is a documented property of PostgreSQL, not of this grant model.
    """
    rows = ledger_roles.fetch(
        "exchange",
        "select count(*) from information_schema.tables where table_schema in ('sealed', 'vault')",
    )
    assert rows == [(0,)]
    # ...while the schemas it may use are visible, so the count is measuring privilege and
    # not an empty catalog.
    rows = ledger_roles.fetch(
        "exchange",
        "select count(*) from information_schema.tables where table_schema = 'ledger'",
    )
    assert rows[0][0] > 0


@pytest.mark.docker
def test_exchange_has_no_write_anywhere(ledger_clean, ledger_roles) -> None:
    """DESIGN §Data models: "``exchange`` role: no write". On every schema it can read."""
    ledger_roles.denied(
        "exchange",
        "insert into ledger.policy_events (store_id, kind, opened_at) "
        "values ('s-1', 'severe_policy_violation', now())",
    )
    ledger_roles.denied("exchange", "delete from ledger.commerce_events")
    ledger_roles.denied("exchange", "update ledger.trust_scores set score = 1.0")
    ledger_roles.denied(
        "exchange",
        "insert into app.sellers (store_id, domain, business_identity, tier) "
        "values ('s-x', 'x.example', 'bi-x', 'external')",
    )
    ledger_roles.denied("exchange", "create table ledger.smuggled (x int)")


@pytest.mark.docker
def test_exchange_holds_no_role_membership_that_could_inherit_a_grant(
    ledger_migrated: str, pg_admin
) -> None:
    """A membership in ``trust_rw`` or ``app`` would hand it the closed schemas sideways.

    The frozen migration scan rejects a role-membership grant statically; this rejects one
    that arrived any other way.
    """
    with pg_admin.cursor() as cur:
        cur.execute(
            "select r.rolname from pg_auth_members m "
            "join pg_roles r on r.oid = m.roleid "
            "join pg_roles member on member.oid = m.member "
            "where member.rolname = 'exchange'"
        )
        assert cur.fetchall() == [], "the exchange role is a member of another role"
        cur.execute("select rolsuper, rolbypassrls from pg_roles where rolname = 'exchange'")
        assert cur.fetchone() == (False, False)


@pytest.mark.docker
def test_exchange_cannot_take_the_ledgers_write_lock(ledger_clean, ledger_roles) -> None:
    """A role documented "no write, anywhere" must not hold the writers' mutex.

    ``CHAIN_LOCK_KEY`` is a published constant derived from the table name and
    ``pg_advisory_lock`` is ``EXECUTE``-to-PUBLIC by default, so any role that could connect
    -- the read-only auction role included -- could take the ledger's advisory lock and sit
    on it, blocking every append in the database. Advisory locks are cooperative; the only
    defence is the function privilege.
    """
    from apps.trust.src.ledger.store import CHAIN_LOCK_KEY

    ledger_roles.denied("exchange", "select pg_advisory_xact_lock(%s)", (CHAIN_LOCK_KEY,))
    ledger_roles.denied("exchange", "select pg_try_advisory_lock(%s)", (CHAIN_LOCK_KEY,))

    # ALL EIGHT bigint overloads, not the two above and not the four 0004 originally
    # revoked. Postgres has ONE 8-byte advisory-lock space and every one of these functions
    # takes a lock in it; `ShareLock` conflicts with `ExclusiveLock`, so the four `_shared`
    # forms stall an append exactly as the exclusive forms do. They were EXECUTE-to-PUBLIC,
    # and proven live: `exchange` took `pg_advisory_lock_shared(776167449)` -- a published
    # constant -- and `app`'s next append BLOCKED. With no `lock_timeout` anywhere in the
    # repo that block was unbounded, so one SELECT from the internet-facing read path halted
    # every ledger append in the database, permanently. Enumerated rather than spot-checked,
    # because a missing name is silent.
    advisory_lock_functions = (
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
        "pg_try_advisory_lock",
        "pg_try_advisory_xact_lock",
        "pg_advisory_lock_shared",
        "pg_advisory_xact_lock_shared",
        "pg_try_advisory_lock_shared",
        "pg_try_advisory_xact_lock_shared",
    )
    assert len(advisory_lock_functions) == 8
    for function in advisory_lock_functions:
        ledger_roles.denied("exchange", f"select {function}(%s)", (CHAIN_LOCK_KEY,))
        assert ledger_roles.fetch(
            "exchange",
            "select has_function_privilege('exchange', %s, 'EXECUTE')",
            (f"{function}(bigint)",),
        ) == [(False,)], f"the exchange role may execute {function}(bigint)"

    # ...while the roles that actually append still can. Only the transaction-scoped `try`
    # form is exercised: a session-scoped lock taken here would outlive the rollback.
    for role in ("trust_rw", "app"):
        assert ledger_roles.fetch(role, "select pg_try_advisory_xact_lock(%s)", (CHAIN_LOCK_KEY,))[
            0
        ] == (True,)
        for function in advisory_lock_functions:
            assert ledger_roles.fetch(
                role,
                "select has_function_privilege(%s, %s, 'EXECUTE')",
                (role, f"{function}(bigint)"),
            ) == [(True,)], f"{role} lost EXECUTE on {function}(bigint) and cannot append"


@pytest.mark.docker
def test_no_object_in_the_four_schemas_is_granted_to_public(ledger_migrated: str, pg_admin) -> None:
    """The hole neither static scan can see.

    Both the frozen S7 criterion and this file's local mirror only read statements that name
    the auction role, so ``GRANT SELECT ON ALL TABLES IN SCHEMA sealed TO PUBLIC`` hands it
    the closed schemas with the scans still green -- and, before 0004 grew its PUBLIC
    revokes, re-applying the migration set did not even repair it. A live ACL assertion is
    the only thing that catches this shape, so it is asserted live.
    """
    with pg_admin.cursor() as cur:
        cur.execute(
            """
            select ns.nspname || '.' || cl.relname, cl.relacl::text
              from pg_class cl
              join pg_namespace ns on ns.oid = cl.relnamespace
             where ns.nspname in ('ledger', 'sealed', 'vault', 'app')
               and cl.relacl is not null
               and exists (select 1 from aclexplode(cl.relacl) a where a.grantee = 0)
            """
        )
        assert cur.fetchall() == [], "an object in a T-011 schema is granted to PUBLIC"
        cur.execute(
            "select nspname, nspacl::text from pg_namespace "
            "where nspname in ('ledger', 'sealed', 'vault', 'app') and nspacl is not null "
            "  and exists (select 1 from aclexplode(nspacl) a where a.grantee = 0)"
        )
        assert cur.fetchall() == [], "a T-011 schema is granted to PUBLIC"


@pytest.mark.docker
def test_the_migration_audit_record_is_not_writable_by_the_roles_it_audits(
    ledger_clean, ledger_roles
) -> None:
    """``ledger.schema_migrations`` is evidence, and evidence a subject can edit is not.

    It lives in ``ledger``, so the blanket ledger write grant reached it until 0004 revoked
    it back.
    """
    for role in ("trust_rw", "app"):
        ledger_roles.denied(role, "delete from ledger.schema_migrations")
        ledger_roles.denied(role, "update ledger.schema_migrations set checksum = 'x'")
        assert ledger_roles.fetch(role, "select count(*) from ledger.schema_migrations")[0][0] > 0


@pytest.mark.docker
def test_trust_rw_reads_and_writes_the_ledger(ledger_clean, ledger_roles) -> None:
    """The ledger's writer role really can write it -- the counterpart to the denials above.

    Written against the runner's connection directly, and rolled back in a ``finally``,
    because the write and the read of it have to be the same transaction. That ``finally``
    is the CF-4 discipline in its raw form: leave the connection open here and the next
    test's ``TRUNCATE`` through ``pg_admin`` hangs on the lock rather than failing.
    """
    connection = ledger_roles.connection("trust_rw")
    try:
        with connection.cursor() as cur:
            cur.execute(
                "insert into ledger.policy_events (store_id, kind, penalty, opened_at) "
                "values ('s-1', 'severe_policy_violation', 0.30, now())"
            )
            cur.execute("select count(*) from ledger.policy_events")
            assert cur.fetchone() == (1,)
    finally:
        connection.rollback()
    ledger_roles.denied("trust_rw", "select * from sealed.envelopes")
    ledger_roles.denied("trust_rw", "select * from vault.pseudonym_history")


@pytest.mark.docker
def test_buyer_vault_is_the_only_role_that_reaches_the_identity_schema(
    ledger_clean, ledger_roles
) -> None:
    """DESIGN: ``vault.*`` is "buyer role only"."""
    assert ledger_roles.fetch("buyer_vault", "select count(*) from vault.pseudonym_history") == [
        (0,)
    ]
    for role in ("exchange", "trust_rw", "app"):
        ledger_roles.denied(role, "select * from vault.pseudonym_history")


@pytest.mark.docker
def test_app_role_reaches_sealed_and_the_exchange_does_not(ledger_clean, ledger_roles) -> None:
    """Seller strategy is closed to the auction, not to everything.

    S7 is about one boundary. A ``sealed`` schema nothing at all could read would satisfy
    the denial test and make the merchant surfaces unbuildable, so the reachable side is
    asserted here too.
    """
    assert ledger_roles.fetch("app", "select count(*) from sealed.envelopes") == [(0,)]
    ledger_roles.execute(
        "app",
        "insert into sealed.envelopes (store_id, version, activation) values ('s-1', 1, 'active')",
    )
    ledger_roles.denied("exchange", "select * from sealed.envelopes")


@pytest.mark.docker
def test_default_privileges_keep_a_future_table_closed(
    ledger_clean, pg_admin, ledger_roles
) -> None:
    """A table added by a *later* migration must inherit the same model, not an open one.

    ``ALTER DEFAULT PRIVILEGES`` is what makes that true between migration runs. The check
    creates a table in each schema and reads the resulting ACL rather than trusting the
    statement to have meant what it says.

    This is also the test that demonstrates CF-4 end to end: it makes a denial assertion on
    a ``pg_role`` connection *first*, and then issues DDL through the autocommit ``pg_admin``
    connection. Written naively -- ``pytest.raises`` straight onto ``pg_role("exchange")``,
    then ``CREATE TABLE`` -- that second statement blocks on the ``ACCESS SHARE`` lock the
    idle-in-transaction role connection is still holding, and the test hangs until
    ``lock_timeout`` with nothing in the output to explain it. It does not hang here because
    ``ledger_roles`` rolled the connection back before returning.
    """
    ledger_roles.denied("exchange", "select * from sealed.envelopes")
    ledger_roles.reset()  # belt: nothing is open, and nothing may be, before the DDL below

    with pg_admin.cursor() as cur:
        cur.execute("create table if not exists ledger.future_table (x int)")
        cur.execute("create table if not exists sealed.future_table (x int)")
        try:
            cur.execute(
                "select has_table_privilege('exchange', 'ledger.future_table', 'select'), "
                "       has_table_privilege('exchange', 'ledger.future_table', 'insert'), "
                "       has_table_privilege('exchange', 'sealed.future_table', 'select')"
            )
            assert cur.fetchone() == (True, False, False)
        finally:
            cur.execute("drop table if exists ledger.future_table")
            cur.execute("drop table if exists sealed.future_table")


def test_migration_sql_grants_the_exchange_exactly_ledger_and_app() -> None:
    """A local mirror of the frozen static scan, so a bad edit fails T-011's own verify.

    The frozen suite owns the criterion; this owns the feedback loop. Both read the same
    property: the union of schemas granted to the auction role is exactly ``{ledger, app}``,
    no statement granting it anything mentions the closed schemas, and no grant to it is a
    role membership.
    """
    granted: set[str] = set()
    problems: list[str] = []
    role_re = re.compile(r"\bexchange[a-z0-9_]*\b")
    files = migration_lib.migration_files()
    assert files, "db/migrations holds no .sql files"
    for path in files:
        text = re.sub(r"--[^\n]*", " ", path.read_text(encoding="utf-8"))
        for statement in migration_lib.statements(text):
            low = re.sub(r"\s+", " ", statement).strip().lower()
            if "grant" not in low or low.startswith("revoke"):
                continue
            # The blind spot in BOTH static scans, closed here: a grant to PUBLIC reaches
            # every role including the auction one, and names none of them. The frozen
            # criterion cannot see it (it only reads statements containing "exchange"), and
            # it is a real, live breach -- so this mirror widens the net rather than
            # matching the frozen scan's blind spot exactly.
            if re.search(r"\bto\s+public\b", low) and ("sealed" in low or "vault" in low):
                problems.append(
                    f"{path.name}: grant to PUBLIC reaches a closed schema: {low[:120]!r}"
                )
            if not role_re.search(low):
                continue
            if "sealed" in low or "vault" in low:
                problems.append(f"{path.name}: {low[:120]!r}")
            if " on " not in f" {low} ":
                problems.append(f"{path.name}: role-membership grant: {low[:120]!r}")
            match = re.search(r"\bon\s+schema\s+([a-z0-9_,\s\"]+?)\s+to\s", low)
            if match:
                granted.update(part.strip().strip('"') for part in match.group(1).split(","))
    assert not problems, "grant statements reach a closed schema:\n  " + "\n  ".join(problems)
    assert granted == {"ledger", "app"}, f"granted schemas are {sorted(granted)}"


# =======================================================================================
# Acceptance 4 / D52 -- the two structures the frozen suite cannot grade
# =======================================================================================


@pytest.fixture
def _two_sellers(ledger_clean, pg_admin):
    with pg_admin.cursor() as cur:
        cur.executemany(
            "insert into app.sellers (store_id, domain, business_identity, tier) "
            "values (%s, %s, %s, %s)",
            [
                ("store-a", "a.example", "bi-a", "external"),
                ("store-b", "b.example", "bi-b", "external"),
            ],
        )
    return pg_admin


@pytest.mark.docker
def test_bid_nonces_are_unique_per_signer_and_not_globally(_two_sellers) -> None:
    """D52: uniqueness is ``(signer_id, nonce)``. Per signer, never global.

    Both halves are the criterion. A schema with a global unique index on ``nonce`` passes
    the rejection half and fails the acceptance half; a schema with no constraint at all
    does the reverse.
    """
    connection = _two_sellers
    insert = (
        "insert into app.bid_nonces (signer_id, nonce, auction_id, retain_until) "
        "values (%s, %s, %s, now() + interval '1 hour')"
    )
    with connection.cursor() as cur:
        cur.execute(insert, ("signer-a", "nonce-1", "auction-1"))

        # The same nonce string under a DIFFERENT signer is accepted.
        cur.execute(insert, ("signer-b", "nonce-1", "auction-1"))

        # A different nonce from the same signer is accepted.
        cur.execute(insert, ("signer-a", "nonce-2", "auction-1"))

    with pytest.raises(psycopg.errors.UniqueViolation) as raised:
        with connection.cursor() as cur:
            cur.execute(insert, ("signer-a", "nonce-1", "auction-2"))
    assert "bid_nonces_signer_nonce_key" in str(raised.value), (
        "the duplicate must be refused by the (signer_id, nonce) constraint specifically"
    )

    with connection.cursor() as cur:
        cur.execute(
            "select signer_id, nonce from app.bid_nonces order by signer_id, nonce",
        )
        assert cur.fetchall() == [
            ("signer-a", "nonce-1"),
            ("signer-a", "nonce-2"),
            ("signer-b", "nonce-1"),
        ]


@pytest.mark.docker
def test_bid_nonces_are_retained_past_the_auction_deadline(_two_sellers) -> None:
    """D52: ``retain_until`` outlives ``respond_by``, and ``purge_expired`` reads it.

    A row whose retention ends at or before it was consumed is refused outright, because a
    nonce that is forgotten the moment it is recorded reopens the replay window it exists to
    close.
    """
    connection = _two_sellers
    with connection.cursor() as cur:
        cur.execute(
            "insert into app.bid_nonces (signer_id, nonce, auction_id, consumed_at, retain_until) "
            "values ('signer-a', 'n-live', 'auction-1', now(), now() + interval '30 minutes')"
        )
        cur.execute(
            "insert into app.bid_nonces (signer_id, nonce, auction_id, consumed_at, retain_until) "
            "values ('signer-a', 'n-stale', 'auction-0', now() - interval '2 hours', "
            "        now() - interval '1 hour')"
        )
        # purge_expired(as_of) is exactly this predicate.
        cur.execute("delete from app.bid_nonces where retain_until <= now() returning nonce")
        assert [row[0] for row in cur.fetchall()] == ["n-stale"]
        cur.execute("select nonce from app.bid_nonces")
        assert cur.fetchall() == [("n-live",)]

    with pytest.raises(psycopg.errors.CheckViolation) as raised:
        with connection.cursor() as cur:
            cur.execute(
                "insert into app.bid_nonces (signer_id, nonce, auction_id, consumed_at, "
                "retain_until) values ('signer-b', 'n-bad', 'a', now(), now() - interval '1 s')"
            )
    assert "bid_nonces_retained_past_consumption" in str(raised.value)


@pytest.mark.docker
def test_seller_endpoints_hold_one_row_per_live_key(_two_sellers) -> None:
    """D52 / acceptance 4: rotation, and ``key_id`` unique only *within* a signer.

    Exactly the shape the ticket spells out: two live rows for one signer under different
    ``key_id`` values, PLUS a row where a different signer reuses one of those ``key_id``
    strings. A schema keyed on ``key_id`` alone, or one key per store, cannot hold this.
    """
    connection = _two_sellers
    insert = (
        "insert into app.seller_endpoints (signer_id, store_id, key_id, public_key, status) "
        "values (%s, %s, %s, %s, 'active')"
    )
    with connection.cursor() as cur:
        cur.execute(insert, ("signer-a", "store-a", "key-2026-01", "pk-a1"))
        cur.execute(insert, ("signer-a", "store-a", "key-2026-02", "pk-a2"))
        # A DIFFERENT signer reuses one of those key_id strings, with a different secret.
        cur.execute(insert, ("signer-b", "store-b", "key-2026-01", "pk-b1"))

        cur.execute(
            "select key_id, public_key from app.seller_endpoints "
            "where signer_id = 'signer-a' and status = 'active' order by key_id"
        )
        assert cur.fetchall() == [("key-2026-01", "pk-a1"), ("key-2026-02", "pk-a2")], (
            "a signer must be able to hold more than one live key -- that is what rotation is"
        )

        # The keyring lookup is (signer_id, key_id) and never key_id alone.
        cur.execute(
            "select public_key from app.seller_endpoints where signer_id = %s and key_id = %s",
            ("signer-b", "key-2026-01"),
        )
        assert cur.fetchall() == [("pk-b1",)]

    # ...and the same (signer_id, key_id) twice is still a duplicate.
    with pytest.raises(psycopg.errors.UniqueViolation) as duplicate:
        with connection.cursor() as cur:
            cur.execute(insert, ("signer-a", "store-a", "key-2026-01", "pk-other"))
    assert "seller_endpoints_pkey" in str(duplicate.value)


@pytest.mark.docker
def test_bid_nonce_retention_must_outlive_the_auction_deadline(_two_sellers) -> None:
    """D52's actual property, made checkable.

    The header and the retention test both said "retained past the auction's respond_by",
    and the only constraint that existed was ``retain_until > consumed_at`` -- which a
    one-microsecond retention satisfies, and a nonce forgotten a microsecond after it is
    consumed reopens exactly the replay window D52 exists to close. Recording the deadline on
    the row is what turns the prose into a constraint.
    """
    connection = _two_sellers
    insert = (
        "insert into app.bid_nonces "
        "(signer_id, nonce, auction_id, consumed_at, respond_by, retain_until) "
        "values (%s, %s, 'auction-1', '2026-01-01T00:00:00Z', %s, %s)"
    )
    with connection.cursor() as cur:
        cur.execute(
            insert,
            ("signer-a", "n-ok", "2026-01-01T00:05:00Z", "2026-01-01T00:35:00Z"),
        )
        cur.execute("select count(*) from app.bid_nonces where nonce = 'n-ok'")
        assert cur.fetchone() == (1,)

    with pytest.raises(psycopg.errors.CheckViolation) as raised:
        with connection.cursor() as cur:
            cur.execute(
                insert,
                ("signer-a", "n-short", "2026-01-01T00:05:00Z", "2026-01-01T00:04:59Z"),
            )
    assert "bid_nonces_retained_past_the_auction" in str(raised.value)

    # The deadline stays optional, because T-044 owns the writer and its frozen
    # `NonceStore.seen(signer_id, nonce)` signature does not carry one.
    with connection.cursor() as cur:
        cur.execute(
            "insert into app.bid_nonces (signer_id, nonce, auction_id, retain_until) "
            "values ('signer-b', 'n-nodeadline', 'auction-1', now() + interval '1 hour')"
        )


@pytest.mark.docker
def test_a_seller_endpoint_cannot_be_active_and_retired_at_once(_two_sellers) -> None:
    """The keyring's "is this key live?" question must have exactly one answer.

    The one-directional constraint accepted ``status = 'active'`` alongside a past
    ``retired_at``, so a reader filtering on ``status`` and a reader filtering on
    ``retired_at`` would disagree about the same row -- at the signature-verification
    boundary, where disagreeing about which keys are live is the whole risk.
    """
    connection = _two_sellers
    with pytest.raises(psycopg.errors.CheckViolation) as raised:
        with connection.cursor() as cur:
            cur.execute(
                "insert into app.seller_endpoints "
                "(signer_id, store_id, key_id, public_key, status, retired_at) "
                "values ('signer-a', 'store-a', 'k1', 'pk', 'active', now())"
            )
    assert "seller_endpoints_retirement_matches_status" in str(raised.value)

    with pytest.raises(psycopg.errors.CheckViolation):
        with connection.cursor() as cur:
            cur.execute(
                "insert into app.seller_endpoints "
                "(signer_id, store_id, key_id, public_key, status) "
                "values ('signer-a', 'store-a', 'k2', 'pk', 'revoked')"  # revoked, no retired_at
            )

    with connection.cursor() as cur:
        cur.execute(
            "insert into app.seller_endpoints "
            "(signer_id, store_id, key_id, public_key, status, retired_at) "
            "values ('signer-a', 'store-a', 'k3', 'pk', 'revoked', now())"
        )
        cur.execute("select count(*) from app.seller_endpoints where key_id = 'k3'")
        assert cur.fetchone() == (1,)


@pytest.mark.docker
def test_seller_blacklist_carries_the_four_review_states(_two_sellers) -> None:
    """The ticket's "seller_blacklist with review states", and its identity binding."""
    connection = _two_sellers
    insert = (
        "insert into app.seller_blacklist "
        "(business_identity, store_id, reason_code, source, status, starts_at, expires_at) "
        "values (%s, %s, 'severe_policy_violation', 'trust', %s, now(), %s)"
    )
    with connection.cursor() as cur:
        for index, status in enumerate(("active", "under_review", "appealed", "expired")):
            cur.execute(insert, (f"bi-{index}", None, status, None))
        cur.execute("select distinct status from app.seller_blacklist order by status")
        assert [row[0] for row in cur.fetchall()] == [
            "active",
            "appealed",
            "expired",
            "under_review",
        ]
        # An unknown state is refused rather than stored and silently ignored later.
        with pytest.raises(psycopg.errors.CheckViolation) as bad_status:
            cur.execute(insert, ("bi-9", None, "probation", None))
        assert "seller_blacklist_status_check" in str(bad_status.value)

    # At most one live entry per business identity: the fail-closed read is a single-row
    # question, not an ordering question.
    with pytest.raises(psycopg.errors.UniqueViolation) as live:
        with connection.cursor() as cur:
            cur.execute(insert, ("bi-0", None, "under_review", None))
    assert "seller_blacklist_one_live_entry_idx" in str(live.value)


# =======================================================================================
# D35 -- the import-lint gate fires; CF-2 -- the redis.exceptions carve-out is exercised
# =======================================================================================


def test_import_lint_passes_on_the_shipped_configuration() -> None:
    """The positive control for the two tests below, and for ``make verify`` itself."""
    result = _run_lint_imports(REPO_ROOT / ".importlinter", pythonpath=REPO_ROOT / ".pkgroot")
    assert result.returncode == 0, (
        f"the repo's own import contracts are broken:\n{result.stdout}\n{result.stderr}"
    )
    assert "0 broken" in result.stdout, result.stdout


def test_import_lint_exits_non_zero_on_a_deliberate_violation() -> None:
    """D35: T-000 shipped the checker; this is T-011's proof that it fires.

    A gate that has never been observed to fail is a gate nobody has evidence about. The
    fixture under ``lint_fixtures/`` imports a stand-in sealed surface from a stand-in
    exchange, deliberately, outside every root package the real configuration names -- so
    the real gate stays green while this one goes red.
    """
    config = LINT_FIXTURES / "importlinter_fixture.ini"
    assert config.is_file(), f"the D35 fixture configuration is missing: {config}"
    result = _run_lint_imports(config, pythonpath=LINT_FIXTURES)
    assert result.returncode != 0, (
        f"lint-imports exited 0 on a module that violates its contract -- the C3/S7 import "
        f"gate does not fire:\n{result.stdout}\n{result.stderr}"
    )
    assert "BROKEN" in result.stdout
    assert "d35_exchange_fixture -> d35_sealed_fixture.envelope" in result.stdout, (
        f"the violation was not the one the fixture plants:\n{result.stdout}"
    )


def test_redis_exceptions_carve_out_is_exercised_by_a_committed_module(tmp_path: Path) -> None:
    """CF-2: narrowing ``**`` back to ``*`` must now break the build.

    ``.importlinter`` forbids every member package from importing ``redis``, exempting
    ``** -> redis.exceptions`` so ``except redis.exceptions.ConnectionError`` stays legal.
    ``*`` matches ONE segment and ``**`` matches zero or more; the single star was a real
    bug that broke four tickets. But with no tracked file importing ``redis.exceptions``,
    reverting the fix still passed ``make verify`` -- the carve-out had no regression guard,
    and the next ticket to catch a Redis error would have inherited the break.

    ``trust.ledger.errors`` is that guard: three segments deep, so ``**`` forgives it and
    ``*`` does not. This test narrows a copy of the configuration and asserts the build
    goes red, naming that module.
    """
    source = (REPO_ROOT / ".importlinter").read_text(encoding="utf-8")
    assert "** -> redis.exceptions" in source, (
        "the D39 carve-out is no longer spelled `** -> redis.exceptions`; this guard, and "
        "the reason CF-2 exists, need re-reading before the expectation is edited"
    )
    narrowed = tmp_path / "importlinter_single_star.ini"
    narrowed.write_text(source.replace("** -> redis.exceptions", "* -> redis.exceptions"))

    result = _run_lint_imports(narrowed, pythonpath=REPO_ROOT / ".pkgroot")
    assert result.returncode != 0, (
        "narrowing the carve-out to a single star still passes, so no committed file "
        "exercises it and the fix is unguarded again -- which is exactly CF-2"
    )
    assert "trust.ledger.errors -> redis.exceptions" in result.stdout, (
        f"the single-star run broke on something other than the guard module:\n{result.stdout}"
    )


def test_the_carve_out_module_imports_only_the_exceptions_submodule() -> None:
    """The guard must stay *legal*: an exception import, never a client construction.

    ``scripts/check_verify_contracts.py`` check 5 bans building a Redis client anywhere but
    the wrapper, and it scans tests and fixtures too. If this module ever grew a real client
    it would take ``make verify`` red for a reason that has nothing to do with CF-2.
    """
    import ast

    from apps.trust.src.ledger import errors as ledger_errors

    tree = ast.parse(Path(ledger_errors.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    redis_imports = {name for name in imported if name == "redis" or name.startswith("redis.")}
    assert redis_imports == {"redis.exceptions"}, (
        f"the guard module must import the exceptions submodule and nothing else from "
        f"redis; it imports {sorted(redis_imports)}"
    )
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "from_url" not in attributes, "the guard module must not build a Redis client"

    # ...and it is a real, used import: the exception classes are reachable through the
    # module's own published tuple, so the import is not decoration a linter could drop.
    dotted = {
        f"{cls.__module__}.{cls.__qualname__}" for cls in ledger_errors.TRANSIENT_DATASTORE_ERRORS
    }
    assert "redis.exceptions.ConnectionError" in dotted, dotted
    redis_error = next(
        cls
        for cls in ledger_errors.TRANSIENT_DATASTORE_ERRORS
        if cls.__module__.startswith("redis")
    )
    assert ledger_errors.is_transient_datastore_error(redis_error("connection lost"))
    assert not ledger_errors.is_transient_datastore_error(ValueError("nope"))


def test_the_lint_fixture_lives_outside_every_real_root_package() -> None:
    """The D35 fixture must never join the real gate's graph.

    ``trust`` as a root package is ``apps/trust/src``; the fixture is under
    ``apps/trust/tests``. Asserting the location rather than trusting it means a future move
    of the fixture into ``src/`` fails here instead of turning ``make verify`` permanently
    red for everyone.
    """
    assert LINT_FIXTURES.is_dir()
    assert (REPO_ROOT / ".pkgroot" / "trust").resolve() == (REPO_ROOT / "apps/trust/src").resolve()
    assert "src" not in LINT_FIXTURES.relative_to(REPO_ROOT).parts


# =======================================================================================
# Wave-1 verification findings 4, 6, 7 and 9 -- the suite's own blind spots, closed.
# =======================================================================================

#: The C3/S7 contract's forbidden list, pinned EXACTLY. `.importlinter` is a root-manifest
#: file this ticket does not own, so the contract cannot be defended by editing it -- it is
#: defended by asserting on its content from here. Pinning the exact list is what makes a
#: deletion, an emptying, a reordering-to-nothing and a typo all fail loudly; the D35 proof
#: below runs against a two-package toy fixture in which the word `exchange` never appears,
#: and its positive control asserts only `returncode == 0` and `"0 broken" in stdout` -- both
#: of which get EASIER to satisfy as contracts are removed. Deleting the real contract left
#: that gate green.
#:
#: `trust.ledger.sealed` is KNOWN-VACUOUS TODAY: `apps/trust/src/ledger/` holds only
#: __init__, canonical, chain, errors, migrations, replay and store. import-linter tolerates
#: a forbidden module that does not exist yet, so the clause is protection that switches on
#: when sealed-state trust work lands. It is pinned, deliberately NOT removed, and not
#: required to be importable.
EXPECTED_C3_FORBIDDEN_MODULES = (
    "store_agent.modes",
    "merchant_svc.envelope",
    "merchant_svc.onboarding",
    "trust.ledger.sealed",
)

#: The contract names `lint-imports` must report on the shipped configuration.
EXPECTED_CONTRACT_NAMES = (
    "C3/S7: exchange must never import sealed-state or envelope modules",
    "D39: only proxyshop_support may construct a Redis client",
)


def _importlinter_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    read = parser.read(REPO_ROOT / ".importlinter", encoding="utf-8")
    assert read, f"{REPO_ROOT / '.importlinter'} is missing or unreadable"
    return parser


def test_the_c3_s7_import_contract_is_still_declared_and_still_names_every_module() -> None:
    """Finding 4: deleting the release-blocking C3/S7 contract shipped 100% green.

    Nothing in the suite read the *real* configuration. This does, and pins the three things
    a silent removal would move: the root package, the contract's existence and source, and
    the exact forbidden list.
    """
    parser = _importlinter_config()
    roots = parser["importlinter"]["root_packages"].split()
    assert "exchange" in roots, (
        "`exchange` is no longer a root package, so every contract sourced from it is "
        "vacuous and lint-imports still exits 0"
    )
    assert "trust" in roots

    forbidden = {
        section: parser[section]
        for section in parser.sections()
        if section.startswith("importlinter:contract:")
        and parser[section].get("type", "").strip() == "forbidden"
        and "exchange" in parser[section].get("source_modules", "").split()
    }
    assert forbidden, "no `forbidden` contract is sourced from `exchange` any more (C3/S7)"

    matching = {
        section: config
        for section, config in forbidden.items()
        if tuple(config["forbidden_modules"].split()) == EXPECTED_C3_FORBIDDEN_MODULES
    }
    assert len(matching) == 1, (
        "the C3/S7 forbidden list is not what it was. Expected exactly\n  "
        + "\n  ".join(EXPECTED_C3_FORBIDDEN_MODULES)
        + "\nfound:\n  "
        + "\n  ".join(
            f"[{section}] {config.get('forbidden_modules', '').split()}"
            for section, config in forbidden.items()
        )
    )
    section = next(iter(matching))
    assert parser[section]["name"].strip() == EXPECTED_CONTRACT_NAMES[0]


def test_the_shipped_import_contracts_are_reported_as_kept_by_name() -> None:
    """The positive control, strengthened so removal makes it HARDER to pass, not easier.

    ``returncode == 0`` and ``"0 broken"`` are both satisfied by a configuration with no
    contracts at all. Naming the contracts is what makes a deletion red.
    """
    result = _run_lint_imports(REPO_ROOT / ".importlinter", pythonpath=REPO_ROOT / ".pkgroot")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    for name in EXPECTED_CONTRACT_NAMES:
        assert f"{name} KEPT" in result.stdout, (
            f"lint-imports did not report {name!r} as KEPT -- the contract is gone, renamed "
            f"or no longer evaluated:\n{result.stdout}"
        )
    assert "Contracts: 2 kept, 0 broken." in result.stdout, result.stdout


def test_the_ledger_package_has_no_sealed_module_yet_and_that_is_reported_not_hidden() -> None:
    """The pinned `trust.ledger.sealed` clause is vacuous TODAY, on purpose.

    import-linter tolerates a forbidden module that does not exist, so the clause costs
    nothing and switches on the moment sealed-state trust work lands. Asserted here so the
    vacuity is a recorded fact rather than a surprise -- and so that the day the module DOES
    land, this test is the one that says the contract just became live.
    """
    from apps.trust.src import ledger

    modules = sorted(
        path.stem
        for path in Path(ledger.__file__).parent.glob("*.py")
        if not path.stem.startswith("__")
    )
    assert modules == ["canonical", "chain", "errors", "migrations", "replay", "store"]
    assert "sealed" not in modules
    assert "trust.ledger.sealed" in EXPECTED_C3_FORBIDDEN_MODULES, (
        "the clause was removed rather than left dormant; it is protection that is supposed "
        "to exist when the module lands"
    )


def test_every_migration_bounds_its_own_lock_and_statement_waits() -> None:
    """Finding 6: no ``lock_timeout`` existed anywhere in the repo.

    The runner holds every lock a file takes until end-of-file, so one blocked statement
    stalls the whole migration -- and unbounded, "stalls" means forever. This project has
    already lost 600 seconds to a lock-shaped stall.
    """
    files = migration_lib.migration_files()
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert re.search(r"set\s+local\s+lock_timeout\s*=", text, re.IGNORECASE), (
            f"{path.name} sets no lock_timeout: a blocked statement waits forever"
        )
        assert re.search(r"set\s+local\s+statement_timeout\s*=", text, re.IGNORECASE), (
            f"{path.name} sets no statement_timeout"
        )


def test_the_concurrent_role_creation_guard_catches_the_sqlstate_the_race_raises() -> None:
    """Finding 9: the guard caught the wrong SQLSTATE.

    ``duplicate_object`` (42710) comes from ``CREATE ROLE``'s own pre-insert catalog lookup
    -- the sequential re-run case, where no guard is needed. In the genuinely concurrent case
    the loser blocks on ``pg_authid_rolname_index`` and raises ``unique_violation`` (23505),
    which was not caught. The guard missed the exact race it was written for.
    """
    creators = [
        path
        for path in migration_lib.migration_files()
        if re.search(r"create\s+role", path.read_text(encoding="utf-8"), re.IGNORECASE)
    ]
    assert creators, "no migration creates the least-privilege roles"
    for path in creators:
        text = path.read_text(encoding="utf-8")
        assert re.search(
            r"exception\s+when\s+duplicate_object\s+or\s+unique_violation",
            text,
            re.IGNORECASE,
        ), (
            f"{path.name} does not catch unique_violation, so the CONCURRENT CREATE ROLE "
            f"race -- the only one the guard exists for -- still fails the loser"
        )


@pytest.mark.docker
def test_re_running_the_migrations_does_not_rebuild_the_two_check_constraints(
    ledger_migrated: str, pg_admin
) -> None:
    """Finding 6: every run re-validated two constraints under ``ACCESS EXCLUSIVE``.

    ``DROP CONSTRAINT IF EXISTS`` + ``ADD CONSTRAINT ... CHECK`` without ``NOT VALID`` is a
    full sequential scan under ``ACCESS EXCLUSIVE``, held to end-of-file, on every single
    run -- and ``app.bid_nonces`` grows with every signed bid the system ever receives. The
    constraint's OID is the evidence: an unconditional drop-and-recreate assigns a new one,
    a genuine no-op keeps it.
    """
    names = [
        "seller_endpoints_retirement_matches_status",
        "bid_nonces_retained_past_the_auction",
    ]

    def constraint_rows() -> list[tuple]:
        with pg_admin.cursor() as cur:
            cur.execute(
                "select c.conname, c.oid, c.convalidated from pg_constraint c "
                "  join pg_class t on t.oid = c.conrelid "
                "  join pg_namespace n on n.oid = t.relnamespace "
                " where n.nspname = 'app' and c.conname = any(%s) order by c.conname",
                (names,),
            )
            return list(cur.fetchall())

    before = constraint_rows()
    assert [row[0] for row in before] == sorted(names), before
    assert all(row[2] for row in before), "a constraint was left NOT VALID: it enforces nothing"

    migration_lib.apply_migrations(pg_admin)
    after = constraint_rows()
    assert after == before, (
        "re-running the migrations dropped and rebuilt a CHECK constraint (the OID moved). "
        "That is a full table scan under ACCESS EXCLUSIVE on every run.\n"
        f"  before: {before}\n  after:  {after}"
    )


@pytest.mark.docker
def test_drift_detection_fails_loudly_rather_than_reporting_no_drift(
    ledger_migrated: str, worker_index: int
) -> None:
    """Finding 7: ``strict=True`` failed OPEN.

    ``except Exception: return []`` made "permission denied for ledger.schema_migrations", a
    dropped connection and a renamed column indistinguishable from "the table does not exist
    yet" -- so ``strict`` passed **vacuously** and applied an edited migration, which is the
    exact opposite of what it was asked to do. ``[]`` here is read as permission to proceed,
    so it must mean "I looked and found nothing", never "I could not look".
    """
    from proxyshop_support.postgres import role_dsn

    dead = psycopg.connect(role_dsn("admin", worker_index), connect_timeout=5)
    dead.close()
    with pytest.raises(psycopg.Error):
        migration_lib.drifted_migrations(dead)


def test_drift_detection_still_treats_an_absent_bookkeeping_table_as_no_drift() -> None:
    """The one error that genuinely does mean "nothing can have drifted here yet"."""

    class _NoBookkeepingTable:
        rolled_back = False

        def cursor(self):
            raise psycopg.errors.UndefinedTable(
                'relation "ledger.schema_migrations" does not exist'
            )

        def rollback(self) -> None:
            self.rolled_back = True

    connection = _NoBookkeepingTable()
    assert migration_lib.drifted_migrations(connection, files=[]) == []
    assert connection.rolled_back, "the aborted transaction must be cleared"


def test_drift_detection_propagates_a_privilege_denial_instead_of_swallowing_it() -> None:
    """The precise shape the old ``except Exception`` hid: evidence the subject can deny."""

    class _Denied:
        def cursor(self):
            raise psycopg.errors.InsufficientPrivilege(
                "permission denied for table schema_migrations"
            )

        def rollback(self) -> None:  # pragma: no cover - must not be reached
            raise AssertionError("a privilege denial must propagate, not be rolled back away")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        migration_lib.drifted_migrations(_Denied(), files=[])


@pytest.mark.docker
def test_a_bare_database_locks_down_all_eight_advisory_lock_overloads(
    ledger_second_database: str, worker_index: int
) -> None:
    """Finding 1, asserted where the migration's own text is the only thing that decides.

    Function ACLs live in ``pg_proc.proacl``. They are per-database like every other grant,
    but -- unlike table grants -- they are NOT cleared by ``DROP SCHEMA``, because
    ``pg_advisory_lock`` lives in ``pg_catalog``. So on this worker's long-lived database a
    REVOKE issued by an *earlier* run survives the deletion of the statement that issued it,
    and the session tests above would stay green against a 0004 that had stopped revoking.
    Measured: deleting the four ``*_shared`` REVOKE lines left every session-database
    assertion passing.

    A database created from bare ``template1`` has the stock ``EXECUTE`` to PUBLIC on all
    eight overloads, so here the migration is the only thing that can take them away.
    """
    from proxyshop_support.postgres import role_dsn

    overloads = [
        f"{name}(bigint)"
        for name in (
            "pg_advisory_lock",
            "pg_advisory_xact_lock",
            "pg_try_advisory_lock",
            "pg_try_advisory_xact_lock",
            "pg_advisory_lock_shared",
            "pg_advisory_xact_lock_shared",
            "pg_try_advisory_lock_shared",
            "pg_try_advisory_xact_lock_shared",
        )
    ]
    dsn = role_dsn("admin", worker_index, database=ledger_second_database)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as fresh:
        with fresh.cursor() as cur:
            cur.execute(
                "select bool_and(has_function_privilege('exchange', f, 'EXECUTE')) "
                "  from unnest(%s::text[]) as f",
                (overloads,),
            )
            assert cur.fetchone() == (True,), (
                "the premise of this test is wrong: a bare database is supposed to start "
                "with EXECUTE to PUBLIC on the advisory-lock functions"
            )

        migration_lib.apply_migrations(fresh)

        with fresh.cursor() as cur:
            cur.execute(
                "select f, has_function_privilege('exchange', f, 'EXECUTE') "
                "  from unnest(%s::text[]) as f order by f",
                (overloads,),
            )
            still_public = [name for name, allowed in cur.fetchall() if allowed]
        assert still_public == [], (
            f"the read-only auction role may still execute {still_public} in a freshly "
            f"migrated database. Every one of these takes a lock in the SAME 8-byte space as "
            f"CHAIN_LOCK_KEY, and ShareLock conflicts with ExclusiveLock -- so holding any "
            f"one of them halts every append in the database."
        )

        with fresh.cursor() as cur:
            for role in ("trust_rw", "app"):
                cur.execute(
                    "select f from unnest(%s::text[]) as f "
                    " where not has_function_privilege(%s, f, 'EXECUTE')",
                    (overloads, role),
                )
                assert cur.fetchall() == [], (
                    f"{role} cannot take the chain lock and so cannot append"
                )


# =======================================================================================
# T-110 -- the role password has ONE source of truth
# =======================================================================================
#
# ``db/init/00-roles.sql`` and ``db/migrations/0001`` are the two halves of D39's split
# schema and both create the four LOGIN roles. 0001 sources the password from
# ``$PROXYSHOP_ROLE_PASSWORD``; ``00-roles.sql`` pinned all four to the literal ``'x'``
# unconditionally on its else-branch, so a fresh volume produced roles the environment
# variable could not reach -- two sources of truth, with the literal winning. Reproduced
# against a throwaway container before the fix: with
# ``PROXYSHOP_ROLE_PASSWORD=t110-not-the-default`` set, ``exchange`` authenticated with
# ``'x'`` and was REFUSED the value the environment asked for.
#
# The two static tests grade the file's text. The three docker tests grade a REAL fresh
# volume, because that is the only state in which the initdb hook runs at all: each spins a
# private, port-less postgres container from the image docker-compose.yml pins, mounts
# ``db/init`` where the entrypoint reads it, and removes it at teardown. The shared stack is
# never contacted and no cluster-global role on it is created, altered or dropped.

#: The four LOGIN roles ``db/init/00-roles.sql`` creates, in the order it creates them.
_INIT_ROLES = ("exchange", "trust_rw", "buyer_vault", "app")

#: The documented dev default -- and the literal every role used to be pinned to.
_HISTORICAL_DEV_PASSWORD = "x"


def _strip_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _sql_statements_only(text: str) -> str:
    """``text`` with every ``--`` comment removed, so assertions grade code and not prose."""
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _compose_postgres_settings() -> dict[str, str]:
    """The image and ``POSTGRES_*`` values the real stack uses, read from docker-compose.yml.

    Hard-coding them here would let the throwaway container drift away from the container
    the project actually runs, and then prove something about neither. Compose is frozen
    (T-000), so it is the source of truth for both.
    """
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    settings: dict[str, str] = {}
    for key, pattern in (
        ("image", r"^\s*image:\s*(postgres:[^\s#]+)"),
        ("user", r"POSTGRES_USER:\s*([^\s,}#]+)"),
        ("password", r"POSTGRES_PASSWORD:\s*([^\s,}#]+)"),
        ("database", r"POSTGRES_DB:\s*([^\s,}#]+)"),
    ):
        match = re.search(pattern, text, re.MULTILINE)
        assert match is not None, f"docker-compose.yml no longer declares the postgres {key}"
        settings[key] = match.group(1)
    return settings


def _docker(*argv: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    # T-122 sweep: deliberately no PYTHONPATH. The child is the `docker` CLI, not a Python
    # interpreter, so `.pkgroot` would be noise on its environment.
    return subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=timeout, check=False
    )


def _require_docker_cli() -> None:
    """Skip only when there is no docker daemon to talk to -- never for a failing container."""
    if shutil.which("docker") is None:
        pytest.skip("the docker CLI is not on PATH")
    probe = _docker("info", "--format", "{{.ServerVersion}}", timeout=60)
    if probe.returncode != 0:
        pytest.skip(f"the docker daemon is not reachable: {probe.stderr.strip()[:200]}")


@contextlib.contextmanager
def _fresh_volume_postgres(
    worker_index: int, environment: dict[str, str]
) -> Iterator[tuple[str, dict[str, str]]]:
    """A private postgres container that has just run ``db/init`` on a genuinely fresh volume.

    Everything about it is scoped to this test: a name carrying this worker's index and a
    random suffix, **no published ports** (so it cannot collide with the shared stack's
    5432 or be reached from outside), its own anonymous volume, and ``docker rm -f`` at
    teardown. It is the only honest way to exercise the initdb hook: on the shared cluster
    that hook ran when its volume was created and never runs again, and the roles it makes
    are cluster-global -- so proving anything there would mean rewriting credentials the
    other live lanes are connected with.
    """
    _require_docker_cli()
    settings = _compose_postgres_settings()
    name = f"proxyshop_w{worker_index}_t110_{uuid.uuid4().hex[:8]}"
    argv = [
        "run", "-d", "--name", name, "--memory", "512m",
        "-e", f"POSTGRES_USER={settings['user']}",
        "-e", f"POSTGRES_PASSWORD={settings['password']}",
        "-e", f"POSTGRES_DB={settings['database']}",
    ]  # fmt: skip
    for key, value in environment.items():
        argv += ["-e", f"{key}={value}"]
    argv += ["-v", f"{REPO_ROOT / 'db' / 'init'}:/docker-entrypoint-initdb.d:ro", settings["image"]]

    created = _docker(*argv)
    assert created.returncode == 0, f"could not start the throwaway postgres: {created.stderr}"
    try:
        deadline = time.monotonic() + 120
        while True:
            ready = _docker(
                "exec", name, "pg_isready", "-h", "127.0.0.1",
                "-U", settings["user"], "-d", settings["database"], timeout=60,
            )  # fmt: skip
            if ready.returncode == 0:
                break
            state = _docker("inspect", "-f", "{{.State.Running}}", name, timeout=60)
            assert state.stdout.strip() == "true", (
                f"the throwaway postgres exited during init:\n{_docker('logs', name).stderr}"
            )
            assert time.monotonic() < deadline, (
                f"the throwaway postgres never accepted TCP:\n{_docker('logs', name).stderr}"
            )
            time.sleep(0.5)

        # The mount is load-bearing: a container that silently ignored db/init would make
        # every assertion below pass for the wrong reason.
        logs = _docker("logs", name)
        assert "/docker-entrypoint-initdb.d/00-roles.sql" in logs.stdout + logs.stderr, (
            "the initdb hook never ran 00-roles.sql -- the db/init mount did not take"
        )
        yield name, settings
    finally:
        # `-v`, and it is not decoration. The postgres image declares a VOLUME on its data
        # directory, so `docker run` without an explicit mount creates an ANONYMOUS volume
        # -- and `docker rm -f` does not remove it. Measured on this host: each full
        # `pytest apps/trust` run left five orphaned ~200MB volumes behind, one per use of
        # this fixture, and the docstring above says "its own anonymous volume, and
        # `docker rm -f` at teardown" in the belief that the second clause disposes of the
        # first. It does not. The VM disk had already been filled once by exactly this,
        # with 254 volumes reclaimed by hand. `-v` removes the anonymous volumes the
        # container owns and nothing else -- the `db/init` bind mount is a host path, not a
        # volume, so it cannot be touched by this.
        _docker("rm", "-f", "-v", name, timeout=120)


#: POSIX sh that leaves ONE usable address in ``$addr``, or exits 64 saying why (T-118 g).
#:
#: ``hostname -i`` prints a **space-separated list**, not an address: a container on a second
#: docker network, or one with IPv6 enabled, gets several. Measured on this host::
#:
#:     $ docker network connect <second> <container>
#:     $ docker exec <container> hostname -i
#:     172.17.0.2 172.27.0.2
#:
#: The probe's old form interpolated that whole list into ``psql -h "$(hostname -i)"``, which
#: is not a hostname; the login attempt then fails to resolve and every ``_assert_accepts``
#: turns red for a reason that has nothing to do with a password.
#:
#: Loopback is filtered out rather than merely deprioritised, and that is the load-bearing
#: part: the postgres image's ``pg_hba.conf`` **trusts** ``127.0.0.1/32`` and ``::1/128``, so
#: a probe that fell back to loopback would report SUCCESS for every password ever tried and
#: quietly turn the whole T-110 block into a test of nothing. Link-local ``fe80::`` addresses
#: are skipped too: they need a scope id libpq is not being given.
_ROUTABLE_ADDRESS_SH = (
    'addr=""; '
    "for candidate in $(hostname -i); do "
    'case "$candidate" in 127.*|::1|0:0:0:0:0:0:0:1|fe80:*|localhost) continue ;; esac; '
    'addr="$candidate"; break; '
    "done; "
    '[ -n "$addr" ] || { echo "no routable address in: $(hostname -i)" >&2; exit 64; }'
)


def _login_attempt(container: str, role: str, password: str, database: str) -> tuple[bool, str]:
    """Try a password-authenticated connection as ``role``, from inside ``container``.

    Over the container's own routable address and never ``127.0.0.1``: the postgres image
    ships a ``pg_hba.conf`` whose loopback lines are ``trust``, so a loopback connection
    succeeds with *any* password and proves nothing. ``hostname -i`` lands on the
    ``scram-sha-256`` line, where the stored verifier is what decides.
    """
    proc = _docker(
        "exec", "-e", f"PGPASSWORD={password}", "-e", f"T110_ROLE={role}",
        "-e", f"T110_DB={database}", container, "sh", "-c",
        f'{_ROUTABLE_ADDRESS_SH}; psql -h "$addr" -U "$T110_ROLE" -d "$T110_DB" -tAc "select 1"',
    )  # fmt: skip
    return proc.returncode == 0, f"{proc.stdout}{proc.stderr}".strip()


def _assert_accepts(container: str, role: str, password: str, database: str, why: str) -> None:
    accepted, output = _login_attempt(container, role, password, database)
    assert accepted, f"{why}: {output}"


def _assert_refuses(container: str, role: str, password: str, database: str, why: str) -> None:
    accepted, output = _login_attempt(container, role, password, database)
    assert not accepted, why
    assert "password authentication failed" in output, (
        f"{role} refused {password!r} for the wrong reason -- this is not evidence about "
        f"the password at all: {output}"
    )


def test_db_init_reads_the_role_password_from_the_same_source_as_the_migrations() -> None:
    r"""T-110 acceptance 1: the same environment variable, GUC and default as ``0001``.

    Not "it mentions the variable somewhere" -- the *default expression itself* is compared
    between the two files with whitespace removed, so either half drifting to a different
    fallback (or dropping the fallback) fails here rather than on someone's fresh volume.
    """
    init_sql = DB_INIT_ROLES_SQL.read_text(encoding="utf-8")
    migration = (migration_lib.migrations_dir() / "0001_schemas_roles_grants.sql").read_text(
        encoding="utf-8"
    )

    assert f"\\getenv proxyshop_role_password {migration_lib.ROLE_PASSWORD_ENV}" in init_sql, (
        f"db/init/00-roles.sql does not read ${migration_lib.ROLE_PASSWORD_ENV}; the initdb "
        f"hook is the only thing that runs on a fresh volume, so a password set there and "
        f"nowhere else is a password no fresh volume will ever have"
    )
    assert migration_lib.ROLE_PASSWORD_SETTING in init_sql

    shared_default = _strip_whitespace(
        f"coalesce(nullif(current_setting('{migration_lib.ROLE_PASSWORD_SETTING}',true),''),"
        f"'{_HISTORICAL_DEV_PASSWORD}')"
    )
    for name, text in (("db/init/00-roles.sql", init_sql), ("0001", migration)):
        assert shared_default in _strip_whitespace(text), (
            f"{name} no longer resolves the role password with the shared expression "
            f"{shared_default!r}. Two files that create the same cluster-global roles with "
            f"two different defaults is the defect T-110 exists to close."
        )


def test_db_init_holds_no_password_literal_beyond_the_documented_dev_default() -> None:
    """T-110 acceptance 3: one literal in the file, and it is the documented default.

    The old shape was ``ALTER ROLE <r> LOGIN PASSWORD 'x' ...`` on the else-branch of four
    separate blocks -- eight literals, any of which could drift. Both halves are asserted:
    no statement pairs ``PASSWORD`` with a literal, and no ``ALTER ROLE`` mentions a
    password at all (these roles are cluster-global, so re-running this file must never
    reset a credential another worker's live connections are authenticating with).
    """
    code = _sql_statements_only(DB_INIT_ROLES_SQL.read_text(encoding="utf-8"))

    # The keyword, not the tail of an identifier: `\set proxyshop_role_password ''` is the
    # mechanism, and a lookbehind is what separates it from `PASSWORD 'x'`.
    literal = re.search(r"(?<![A-Za-z0-9_])PASSWORD\s+'", code, re.IGNORECASE)
    if literal is not None:
        context = code[max(0, literal.start() - 60) : literal.start() + 60]
        raise AssertionError(
            f"db/init/00-roles.sql pairs the PASSWORD keyword with a literal: ...{context}..."
            f" The password has one source of truth ($PROXYSHOP_ROLE_PASSWORD, defaulting to "
            f"the documented dev value), and it reaches CREATE ROLE through format(%L)."
        )

    alters = re.findall(r"ALTER\s+ROLE[^';]*", code, re.IGNORECASE)
    assert alters, "the else-branch that keeps this file idempotent has gone missing"
    for statement in alters:
        assert "PASSWORD" not in statement.upper(), (
            f"an ALTER ROLE in db/init/00-roles.sql still writes a password: {statement!r}. "
            f"The roles are cluster-global; re-running this file would cut every live "
            f"connection that authenticated with the old one."
        )

    quoted = re.findall(r"'([^']*)'", code)
    assert quoted.count(_HISTORICAL_DEV_PASSWORD) == 1, (
        f"expected exactly one {_HISTORICAL_DEV_PASSWORD!r} literal (the documented dev "
        f"default in the coalesce), found {quoted.count(_HISTORICAL_DEV_PASSWORD)}: {quoted}"
    )


@pytest.mark.docker("postgres")  # T-172: declares Postgres; a Redis/Neo4j outage must not skip it
def test_a_fresh_volume_init_keeps_a_non_default_role_password(worker_index: int) -> None:
    """T-110 acceptance 2, on a real fresh volume rather than on the file's text.

    ``$PROXYSHOP_ROLE_PASSWORD`` is set to a value that is not the default, the initdb hook
    runs ``db/init/00-roles.sql``, and every one of the four roles then authenticates with
    that value -- and is refused the historical literal. Both directions, because "the role
    can log in" is also true of a role that ignored the environment entirely.
    """
    password = f"t110-{uuid.uuid4().hex}"
    assert password != _HISTORICAL_DEV_PASSWORD
    with _fresh_volume_postgres(worker_index, {"PROXYSHOP_ROLE_PASSWORD": password}) as (
        container,
        settings,
    ):
        for role in _INIT_ROLES:
            _assert_accepts(
                container, role, password, settings["database"],
                f"{role} was created on a fresh volume with PROXYSHOP_ROLE_PASSWORD set, "
                f"but does not accept that password",
            )  # fmt: skip
            _assert_refuses(
                container, role, _HISTORICAL_DEV_PASSWORD, settings["database"],
                f"{role} still accepts the literal {_HISTORICAL_DEV_PASSWORD!r} -- the "
                f"environment variable is not the only source of truth",
            )  # fmt: skip


@pytest.mark.docker("postgres")  # T-172: declares Postgres; a Redis/Neo4j outage must not skip it
def test_a_fresh_volume_init_without_the_variable_keeps_the_documented_dev_default(
    worker_index: int,
) -> None:
    """T-110 acceptance 1's other half: a checkout with no environment behaves as before.

    ``.env.example`` and ``proxyshop_support.postgres.ROLES`` both still carry ``'x'``, so a
    default that silently changed would break every role connection in the repo.
    """
    with _fresh_volume_postgres(worker_index, {}) as (container, settings):
        for role in _INIT_ROLES:
            _assert_accepts(
                container, role, _HISTORICAL_DEV_PASSWORD, settings["database"],
                f"with no PROXYSHOP_ROLE_PASSWORD set, {role} must keep the documented dev "
                f"default -- .env.example and proxyshop_support.postgres depend on it",
            )  # fmt: skip


@pytest.mark.docker("postgres")  # T-172: declares Postgres; a Redis/Neo4j outage must not skip it
def test_re_running_db_init_by_hand_never_resets_a_live_role_password(worker_index: int) -> None:
    """The else-branch, which is the half that actually shipped broken.

    The file promises it can be re-run by hand without erroring. It must ALSO not rewrite
    the credentials of roles that already exist: they are cluster-global, shared by every
    ``proxyshop_w<n>``, and a reset cuts every live connection using the old password. So
    the file is re-run inside the container with a *different* ``$PROXYSHOP_ROLE_PASSWORD``,
    and the password the roles were created with has to survive -- as must its exit status.
    """
    created_with = f"t110-init-{uuid.uuid4().hex}"
    rerun_with = f"t110-rerun-{uuid.uuid4().hex}"
    with _fresh_volume_postgres(worker_index, {"PROXYSHOP_ROLE_PASSWORD": created_with}) as (
        container,
        settings,
    ):
        rerun = _docker(
            "exec", "-e", f"PROXYSHOP_ROLE_PASSWORD={rerun_with}", container,
            "psql", "-v", "ON_ERROR_STOP=1", "-U", settings["user"], "-d", settings["database"],
            "-f", "/docker-entrypoint-initdb.d/00-roles.sql",
        )  # fmt: skip
        assert rerun.returncode == 0, (
            f"db/init/00-roles.sql is not re-runnable by hand any more: {rerun.stderr}"
        )
        for role in _INIT_ROLES:
            _assert_accepts(
                container, role, created_with, settings["database"],
                f"re-running db/init/00-roles.sql reset {role}'s password. These roles are "
                f"cluster-global: that cuts every worker holding a live connection",
            )  # fmt: skip
            _assert_refuses(
                container, role, rerun_with, settings["database"],
                f"the re-run rewrote {role}'s password to the new environment value",
            )  # fmt: skip
            _assert_refuses(
                container, role, _HISTORICAL_DEV_PASSWORD, settings["database"],
                f"the re-run reset {role} to the literal {_HISTORICAL_DEV_PASSWORD!r} -- "
                f"this is the T-110 defect itself",
            )  # fmt: skip


# =======================================================================================
# T-118 (g) -- the login probe survives a container with more than one address
# =======================================================================================
#
# `hostname -i` prints a space-separated LIST. Reproduced on this host, verbatim:
#
#     $ docker network create <n> && docker run -d --name <c> alpine:3 sleep 60
#     $ docker network connect <n> <c>
#     $ docker exec <c> hostname -i
#     172.17.0.2 172.27.0.2
#     $ docker exec <c> sh -c 'echo "psql -h \"$(hostname -i)\""'
#     psql -h "172.17.0.2 172.27.0.2"
#
# So the probe every T-110 assertion runs through breaks the moment the container has a
# second network attached or IPv6 enabled -- and it breaks in the direction that makes
# `_assert_accepts` red for a reason that is not about passwords at all. `_ROUTABLE_ADDRESS_SH`
# picks exactly one, skipping loopback because the image's pg_hba TRUSTS it.


def _run_address_picker(fake_hostname_output: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run ``_ROUTABLE_ADDRESS_SH`` against a stubbed ``hostname -i``.

    A shell-level test rather than a container one, so the address-selection rule is graded
    on inputs a real container is awkward to produce on demand (IPv6, loopback-only) and in
    a test that survives ``verify.sh check``'s docker deselection.
    """
    stub = tmp_path / "hostname"
    stub.write_text(f'#!/bin/sh\necho "{fake_hostname_output}"\n', encoding="utf-8")
    stub.chmod(0o755)
    # T-122 sweep: deliberately no PYTHONPATH. The child is `/bin/sh` running a shell
    # fragment, and PATH — not the import path — is the variable under test here.
    return subprocess.run(
        ["sh", "-c", f'{_ROUTABLE_ADDRESS_SH}; printf %s "$addr"'],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
    )


@pytest.mark.parametrize(
    ("addresses", "chosen"),
    [
        ("172.17.0.2", "172.17.0.2"),
        ("172.17.0.2 172.27.0.2", "172.17.0.2"),
        ("127.0.0.1 172.19.0.3", "172.19.0.3"),
        ("127.0.0.1 ::1 fe80::42:acff:fe13:3 172.19.0.3", "172.19.0.3"),
        ("::1 2001:db8::5", "2001:db8::5"),
    ],
)
def test_the_login_probe_picks_exactly_one_routable_address(
    addresses: str, chosen: str, tmp_path: Path
) -> None:
    """One address, never the list, and never a loopback the image's pg_hba would trust."""
    picked = _run_address_picker(addresses, tmp_path)
    assert picked.returncode == 0, picked.stderr
    assert picked.stdout == chosen, (
        f"`hostname -i` printed {addresses!r} and the probe resolved {picked.stdout!r}. "
        f"psql -h takes ONE host: a list does not resolve, and a loopback address lands on "
        f"the image's `trust` pg_hba lines where any password succeeds."
    )


def test_the_login_probe_refuses_to_guess_when_only_loopback_exists(tmp_path: Path) -> None:
    """No routable address is a loud failure, not a silent loopback that trusts everything.

    This is the half that keeps the fix honest: falling back to 127.0.0.1 would make every
    ``_assert_accepts`` pass and every ``_assert_refuses`` fail, i.e. it would look like a
    password bug rather than a probe bug.
    """
    picked = _run_address_picker("127.0.0.1 ::1", tmp_path)
    assert picked.returncode == 64
    assert picked.stdout == ""
    assert "no routable address" in picked.stderr


@pytest.mark.docker("postgres")  # T-172: declares Postgres; a Redis/Neo4j outage must not skip it
def test_the_login_probe_still_authenticates_on_a_multi_homed_container(
    worker_index: int,
) -> None:
    """The reproduction itself: a real fresh-volume postgres on two docker networks.

    The shell-level tests above grade the selection rule; this one grades the thing that
    actually broke -- ``_login_attempt`` against a container whose ``hostname -i`` really
    does print two addresses. Both directions, so a probe that stopped reaching postgres at
    all cannot pass by having every login "refused".
    """
    password = f"t118g-{uuid.uuid4().hex}"
    with _fresh_volume_postgres(worker_index, {"PROXYSHOP_ROLE_PASSWORD": password}) as (
        container,
        settings,
    ):
        network = f"proxyshop_w{worker_index}_t118g_{uuid.uuid4().hex[:8]}"
        created = _docker("network", "create", network)
        assert created.returncode == 0, f"could not create a second network: {created.stderr}"
        try:
            attached = _docker("network", "connect", network, container)
            assert attached.returncode == 0, f"could not attach it: {attached.stderr}"

            addresses = _docker("exec", container, "hostname", "-i").stdout.split()
            assert len(addresses) > 1, (
                f"the premise of this test is wrong: a container on two docker networks is "
                f"supposed to report several addresses, got {addresses}"
            )

            for role in _INIT_ROLES:
                _assert_accepts(
                    container, role, password, settings["database"],
                    f"{role} cannot be reached once the container has {len(addresses)} "
                    f"addresses -- `psql -h \"$(hostname -i)\"` was passed the whole list",
                )  # fmt: skip
                _assert_refuses(
                    container, role, _HISTORICAL_DEV_PASSWORD, settings["database"],
                    f"{role} accepted the historical literal over the second network -- the "
                    f"probe is not landing on the scram-sha-256 pg_hba line",
                )  # fmt: skip
        finally:
            _docker("network", "disconnect", "-f", network, container, timeout=120)
            _docker("network", "rm", network, timeout=120)


# =======================================================================================
# T-118 (h) -- the ELSE arm's attribute normalisation is graded
# =======================================================================================
#
# After T-110 the else-branch of db/init/00-roles.sql does exactly one thing:
#
#     ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT
#
# and nothing tested it. Every T-110 test grades the PASSWORD half -- that the arm does not
# reset one -- so the arm could have been deleted outright, or drifted to a weaker attribute
# set, with the whole block still green. That matters because the attributes are the
# privilege model's floor: a drifted `exchange` with SUPERUSER bypasses every GRANT and
# every REVOKE in the repo, and C3/S7's "exchange must never read sealed or vault" becomes
# unenforceable no matter what the migrations say.

#: The attributes the file normalises, and what each must be in ``pg_roles`` afterwards.
_NORMALISED_ATTRIBUTES = {
    "rolcanlogin": True,
    "rolsuper": False,
    "rolcreatedb": False,
    "rolcreaterole": False,
    "rolinherit": True,
}

#: How to drift each one away from its normal value, as ALTER ROLE keywords.
_DRIFTED_ATTRIBUTES = "NOLOGIN SUPERUSER CREATEDB CREATEROLE NOINHERIT"


def _attribute_keywords(statement: str) -> set[str]:
    """The role-attribute keywords named in a CREATE/ALTER ROLE format string."""
    vocabulary = {
        "LOGIN", "NOLOGIN", "SUPERUSER", "NOSUPERUSER", "CREATEDB", "NOCREATEDB",
        "CREATEROLE", "NOCREATEROLE", "INHERIT", "NOINHERIT",
    }  # fmt: skip
    return {word for word in re.findall(r"[A-Z]+", statement.upper()) if word in vocabulary}


def test_db_init_normalises_the_same_attributes_it_creates() -> None:
    """A role that already exists must end up identical to one created fresh today.

    Two arms that name two different attribute sets means the privileges a role has depend
    on whether its volume happened to be new -- which is not a property anybody can reason
    about, and is invisible to every other test in this file because they all grade the
    password.
    """
    code = _sql_statements_only(DB_INIT_ROLES_SQL.read_text(encoding="utf-8"))
    creates = re.findall(r"'CREATE ROLE[^']*'", code, re.IGNORECASE)
    alters = re.findall(r"'ALTER ROLE[^']*'", code, re.IGNORECASE)
    assert len(creates) == 1 and len(alters) == 1, (
        f"expected one CREATE ROLE and one ALTER ROLE statement template, found "
        f"{len(creates)} and {len(alters)}"
    )

    created = _attribute_keywords(creates[0])
    altered = _attribute_keywords(alters[0])
    assert created == altered, (
        f"db/init/00-roles.sql creates roles with {sorted(created)} but normalises existing "
        f"ones to {sorted(altered)}. The difference is a privilege a role keeps or loses "
        f"depending only on whether the pgdata volume was fresh."
    )
    assert altered == {"LOGIN", "NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "INHERIT"}, (
        f"the else-branch normalises {sorted(altered)}. Dropping NOSUPERUSER in particular "
        f"makes every GRANT and REVOKE in db/migrations unenforceable: a superuser role "
        f"bypasses all privilege checks, so C3/S7's sealed/vault isolation would be a "
        f"comment rather than a control."
    )


def _role_attributes(container: str, settings: dict[str, str]) -> dict[str, dict[str, bool]]:
    """``{role: {rolcanlogin: bool, ...}}`` straight out of ``pg_roles`` in the container."""
    columns = sorted(_NORMALISED_ATTRIBUTES)
    proc = _docker(
        "exec", container, "psql", "-U", settings["user"], "-d", settings["database"],
        "-t", "-A", "-F", ",", "-c",
        f"select rolname, {', '.join(columns)} from pg_roles "
        f"where rolname = any(array{list(_INIT_ROLES)}::text[]) order by rolname",
    )  # fmt: skip
    assert proc.returncode == 0, f"could not read pg_roles: {proc.stderr}"
    attributes: dict[str, dict[str, bool]] = {}
    for line in proc.stdout.strip().splitlines():
        name, *values = line.split(",")
        attributes[name] = dict(zip(columns, [v == "t" for v in values], strict=True))
    assert sorted(attributes) == sorted(_INIT_ROLES), (
        f"expected every role in {_INIT_ROLES} to exist, found {sorted(attributes)}"
    )
    return attributes


@pytest.mark.docker("postgres")  # T-172: declares Postgres; a Redis/Neo4j outage must not skip it
def test_re_running_db_init_normalises_role_attributes_that_have_drifted(
    worker_index: int,
) -> None:
    """The else-branch's one remaining job, graded on a real cluster.

    All four roles are deliberately driven to the WORST attribute set they can have --
    superuser, unable to log in, able to create roles and databases, not inheriting -- and
    db/init/00-roles.sql is then re-run by hand, which is the documented repair path. Every
    attribute must come back, and the password must still NOT be reset, because those two
    requirements pull in opposite directions and only testing one of them is how the arm
    ended up untested.
    """
    created_with = f"t118h-{uuid.uuid4().hex}"
    with _fresh_volume_postgres(worker_index, {"PROXYSHOP_ROLE_PASSWORD": created_with}) as (
        container,
        settings,
    ):
        for role, values in _role_attributes(container, settings).items():
            assert values == _NORMALISED_ATTRIBUTES, (
                f"the premise of this test is wrong: a freshly created {role} is supposed "
                f"to start normalised, got {values}"
            )

        drift = "; ".join(f'ALTER ROLE "{role}" {_DRIFTED_ATTRIBUTES}' for role in _INIT_ROLES)
        broken = _docker(
            "exec", container, "psql", "-v", "ON_ERROR_STOP=1",
            "-U", settings["user"], "-d", settings["database"], "-c", drift,
        )  # fmt: skip
        assert broken.returncode == 0, f"could not drift the attributes: {broken.stderr}"
        drifted = _role_attributes(container, settings)
        assert all(values != _NORMALISED_ATTRIBUTES for values in drifted.values()), (
            f"the drift did not take, so the repair below would prove nothing: {drifted}"
        )

        rerun = _docker(
            "exec", "-e", f"PROXYSHOP_ROLE_PASSWORD={created_with}", container,
            "psql", "-v", "ON_ERROR_STOP=1", "-U", settings["user"], "-d", settings["database"],
            "-f", "/docker-entrypoint-initdb.d/00-roles.sql",
        )  # fmt: skip
        assert rerun.returncode == 0, f"re-running db/init/00-roles.sql failed: {rerun.stderr}"

        for role, values in _role_attributes(container, settings).items():
            assert values == _NORMALISED_ATTRIBUTES, (
                f"re-running db/init/00-roles.sql left {role} with {values}. The else-branch "
                f"exists to normalise exactly these attributes; SUPERUSER surviving it makes "
                f"every GRANT in db/migrations advisory."
            )

        for role in _INIT_ROLES:
            _assert_accepts(
                container, role, created_with, settings["database"],
                f"{role}'s attributes were repaired but its password was reset -- these "
                f"roles are cluster-global, so that cuts every worker holding a live "
                f"connection. The arm must normalise attributes and ONLY attributes",
            )  # fmt: skip
