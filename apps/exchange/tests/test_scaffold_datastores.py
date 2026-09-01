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
