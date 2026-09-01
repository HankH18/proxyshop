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

import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from apps.trust.src.ledger import migrations as migration_lib

REPO_ROOT = Path(__file__).resolve().parents[3]
LINT_FIXTURES = REPO_ROOT / "apps" / "trust" / "tests" / "lint_fixtures"

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
    """Re-applying the whole set to an already-migrated database is a clean no-op.

    Not a nicety: the runner applies every file on every run, and ``make deps-up`` may run
    against a database five other workers have already touched.
    """
    again = migration_lib.apply_migrations(pg_admin)
    assert again == [path.name for path in migration_lib.migration_files()]


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
        "app.bid_nonces_signer_nonce_key": "unique",
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
def test_exchange_cannot_even_enumerate_the_closed_schemas(ledger_clean, ledger_roles) -> None:
    """D5, verified live: ``information_schema.tables`` returns **0 rows** for those schemas.

    A role that can list a table it cannot read still learns the schema. Postgres filters
    ``information_schema`` by privilege, so no-USAGE means no rows -- and that is a property
    of granting nothing, which a table-level REVOKE would not have given us.
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
            if "grant" not in low or not role_re.search(low):
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

    with pytest.raises(psycopg.errors.CheckViolation):
        with connection.cursor() as cur:
            cur.execute(
                "insert into app.bid_nonces (signer_id, nonce, auction_id, consumed_at, "
                "retain_until) values ('signer-b', 'n-bad', 'a', now(), now() - interval '1 s')"
            )


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
    with pytest.raises(psycopg.errors.UniqueViolation):
        with connection.cursor() as cur:
            cur.execute(insert, ("signer-a", "store-a", "key-2026-01", "pk-other"))


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
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(insert, ("bi-9", None, "probation", None))

    # At most one live entry per business identity: the fail-closed read is a single-row
    # question, not an ordering question.
    with pytest.raises(psycopg.errors.UniqueViolation):
        with connection.cursor() as cur:
            cur.execute(insert, ("bi-0", None, "under_review", None))


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
    assert sys.version_info[:2] == (3, 12)  # D2: never the system 3.9
