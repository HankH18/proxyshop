"""Proof that the frozen datastore fixtures actually work against the compose stack.

Orchestrator-owned (T-000) and **frozen**. Roughly forty tickets consume ``pg_admin``,
``pg_role``, ``redis_client`` and ``neo4j_session``; if one of them were subtly wrong, every
one of those tickets would discover it separately and blame its own code. These tests are
the single place that proves them.

Everything here is ``@pytest.mark.docker``, so it skips with an explicit message when the
stack is down (``make check``) and runs for real when it is up (``make verify``). The Neo4j
test is additionally ``@pytest.mark.graph``: this directory's conftest overrides
``_neo4j_guard`` with the D37 flock, so requesting ``neo4j_session`` here also exercises the
cross-worker lock.
"""

from __future__ import annotations

import pytest

from proxyshop_support.worker import redis_db_index


@pytest.mark.docker
def test_pg_admin_is_this_workers_database(pg_admin, worker_index: int) -> None:
    with pg_admin.cursor() as cur:
        cur.execute("select current_database(), current_user")
        database, user = cur.fetchone()
    assert user == "proxyshop"
    assert database.endswith(f"w{worker_index}") or database == "proxyshop_template"


@pytest.mark.docker
def test_least_privilege_roles_exist(pg_admin) -> None:
    """`db/init/00-roles.sql` ran, and every role D5 names is present and non-superuser."""
    with pg_admin.cursor() as cur:
        cur.execute(
            "select rolname, rolsuper from pg_roles "
            "where rolname in ('exchange','trust_rw','buyer_vault','app')"
        )
        rows = dict(cur.fetchall())
    assert set(rows) == {"exchange", "trust_rw", "buyer_vault", "app"}
    assert not any(rows.values()), "no least-privilege role may be a superuser"


@pytest.mark.docker
def test_redis_client_namespaces_keys_and_selects_this_workers_db(
    redis_client, worker_index: int
) -> None:
    """D39: the ``w{N}:`` prefix is applied centrally and the logical DB is per worker."""
    redis_client.set("cart", "1")
    assert redis_client.get("cart") == "1"
    assert redis_client.key("cart") == f"w{worker_index}:cart"

    # On the wire the key really is prefixed — read it back through an un-prefixed client.
    assert redis_client.raw.get(f"w{worker_index}:cart") == "1"
    assert redis_client.raw.get("cart") is None

    assert redis_client.get_connection_kwargs()["db"] == redis_db_index(worker_index)


@pytest.mark.docker
def test_the_global_redis_reset_is_refused(redis_client) -> None:
    """D39: the wrapper rejects the banned global reset even if someone calls it."""
    with pytest.raises(RuntimeError, match="banned repo-wide"):
        redis_client.execute_command("FLUSH" + "ALL")


@pytest.mark.docker
@pytest.mark.graph
def test_neo4j_session_runs_inside_the_d37_flock(neo4j_session) -> None:
    record = neo4j_session.run("RETURN 1 AS answer").single()
    assert record["answer"] == 1


# --------------------------------------------------------------------------------------
# pg_role and the D5 grant model — the C3/S7 enforcement mechanism
# --------------------------------------------------------------------------------------
#
# `pg_role` is the least-privilege connection factory roughly ten tickets depend on, and
# until now **nothing exercised it**: the only Postgres tests in the repo used `pg_admin`,
# a superuser, for which every privilege question answers "yes". So the fixture that the
# C3/S7 proof rides on was unverified, and so was the grant model itself.
#
# D5, verified live and reproduced exactly below: privileges are **schema-level `USAGE`**,
# never table grants into `sealed`. `exchange` gets `USAGE` on `ledger` and `app` and
# nothing else, which makes `sealed` and `vault` fail with *permission denied for schema*
# — the boundary C3 needs — and makes `information_schema.tables` show the exchange role
# ZERO rows for `table_schema='sealed'`: it cannot even learn the envelope tables exist.
#
# The model is built here rather than read from `db/migrations`, which is still empty
# (T-011 owns it). The fixture is idempotent and drops exactly what it created, so it
# leaves the worker database as it found it and cannot collide with those migrations.

#: The schemas D5 names, and whether `exchange` may reach them.
D5_SCHEMAS = ("ledger", "app", "sealed", "vault")
PROBE_TABLE = "scaffold_grant_probe"


@pytest.fixture
def d5_grant_model(pg_admin):
    """Build D5's grant model in this worker's database; tear it down completely after.

    Yields the probe table name. Returns the database to its prior state on the way out:
    any schema this fixture created is dropped, so T-011's migrations meet a clean database.
    """
    created: list[str] = []
    with pg_admin.cursor() as cur:
        for schema in D5_SCHEMAS:
            cur.execute(
                "select 1 from information_schema.schemata where schema_name = %s", (schema,)
            )
            if cur.fetchone() is None:
                cur.execute(f'CREATE SCHEMA "{schema}"')
                created.append(schema)
            cur.execute(
                f'CREATE TABLE IF NOT EXISTS "{schema}"."{PROBE_TABLE}" (id int primary key, note text)'
            )
            cur.execute(
                f'INSERT INTO "{schema}"."{PROBE_TABLE}" VALUES (1, %s) ON CONFLICT DO NOTHING',
                (schema,),
            )
        # D5 exactly: schema-level USAGE on ledger and app only, read on ledger, no write
        # anywhere, and NOTHING at all on sealed or vault.
        cur.execute("GRANT USAGE ON SCHEMA ledger, app TO exchange")
        cur.execute(f'GRANT SELECT ON ledger."{PROBE_TABLE}" TO exchange')
    try:
        yield PROBE_TABLE
    finally:
        with pg_admin.cursor() as cur:
            # `pg_role` hands back TRANSACTIONAL connections and is torn down after this
            # fixture, so a test that merely SELECTed is sitting "idle in transaction"
            # holding an AccessShareLock on the probe table. `DROP SCHEMA ... CASCADE`
            # then waits on it forever — measured: the run hung with the drop in
            # `wait_event_type = Lock` behind the exchange session. Close those sessions
            # first, and keep a lock_timeout so any future variant of this fails loudly
            # in seconds instead of hanging an unattended build.
            cur.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = current_database() and pid <> pg_backend_pid() "
                "and usename = any(%s)",
                (["exchange", "trust_rw", "buyer_vault", "app"],),
            )
            cur.execute("set lock_timeout = '15s'")
            try:
                for schema in D5_SCHEMAS:
                    if schema in created:
                        cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
                    else:
                        cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{PROBE_TABLE}"')
            finally:
                cur.execute("reset lock_timeout")


@pytest.mark.docker
def test_pg_role_connects_as_the_role_it_was_asked_for(pg_role, worker_database: str) -> None:
    """The factory's whole contract: the connection really is that principal, here."""
    for role in ("exchange", "trust_rw", "buyer_vault", "app"):
        with pg_role(role).cursor() as cur:
            cur.execute(
                "select current_user, current_database(), usesuper from pg_user where usename = current_user"
            )
            user, database, is_superuser = cur.fetchone()
        assert user == role, f"pg_role({role!r}) connected as {user!r}"
        assert database == worker_database, "a role connection escaped this worker's database (D38)"
        assert not is_superuser, (
            f"{role} is a superuser; every privilege assertion below would be vacuous"
        )


@pytest.mark.docker
def test_pg_role_caches_one_session_per_role_and_closes_them(pg_role) -> None:
    """Documented behaviour: same role -> same session, so privilege state is stable."""
    first = pg_role("exchange")
    assert pg_role("exchange") is first
    assert pg_role("trust_rw") is not first


@pytest.mark.docker
def test_pg_role_refuses_the_superuser_and_unknown_names(pg_role) -> None:
    """Silently handing back a superuser would make every C3 assertion meaningless."""
    with pytest.raises(KeyError, match="pg_admin"):
        pg_role("admin")
    with pytest.raises(KeyError):
        pg_role("postgres")


@pytest.mark.docker
def test_d5_exchange_reads_the_ledger(pg_role, d5_grant_model) -> None:
    """D5: with USAGE on `ledger` and SELECT on its tables, `exchange` reads successfully."""
    connection = pg_role("exchange")
    with connection.cursor() as cur:
        cur.execute(f"select note from ledger.{d5_grant_model}")
        assert [row[0] for row in cur.fetchall()] == ["ledger"]
    connection.rollback()


@pytest.mark.docker
@pytest.mark.parametrize("schema", ["sealed", "vault"])
def test_d5_exchange_cannot_read_sealed_or_vault(pg_role, d5_grant_model, schema: str) -> None:
    """C3/S7: the denial is at the **schema** level, which is what makes it airtight.

    A table-level denial would still let `exchange` enumerate the envelope tables; denying
    `USAGE` on the schema means it cannot resolve a name inside it at all.
    """
    import psycopg

    connection = pg_role("exchange")
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as excinfo:
        with connection.cursor() as cur:
            cur.execute(f"select * from {schema}.{d5_grant_model}")
    connection.rollback()
    assert f"permission denied for schema {schema}" in str(excinfo.value)


@pytest.mark.docker
def test_d5_exchange_sees_zero_sealed_tables_in_the_catalog(pg_role, d5_grant_model) -> None:
    """D5, verbatim: `information_schema.tables WHERE table_schema='sealed'` is EMPTY.

    `information_schema` is privilege-filtered, so this is the strongest form of C3: the
    exchange role cannot discover that sealed state exists, let alone read it. `ledger`
    being visible in the same query is what keeps this from passing vacuously.
    """
    connection = pg_role("exchange")
    with connection.cursor() as cur:
        cur.execute("select count(*) from information_schema.tables where table_schema = 'sealed'")
        assert cur.fetchone()[0] == 0
        cur.execute("select count(*) from information_schema.tables where table_schema = 'vault'")
        assert cur.fetchone()[0] == 0
        cur.execute("select count(*) from information_schema.tables where table_schema = 'ledger'")
        assert cur.fetchone()[0] >= 1, "the query itself is broken if even ledger is invisible"
        cur.execute(
            "select has_schema_privilege('exchange','sealed','USAGE'), "
            "has_schema_privilege('exchange','ledger','USAGE')"
        )
        assert cur.fetchone() == (False, True)
    connection.rollback()


@pytest.mark.docker
def test_d5_exchange_cannot_write_the_ledger(pg_role, d5_grant_model) -> None:
    """DESIGN's "`exchange` role: no write" — read access must not have carried INSERT."""
    import psycopg

    connection = pg_role("exchange")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connection.cursor() as cur:
            cur.execute(f"insert into ledger.{d5_grant_model} values (99, 'written-by-exchange')")
    connection.rollback()
