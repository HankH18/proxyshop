#!/usr/bin/env python
"""Apply ``db/migrations/*.sql`` to this worker's database. Added by the deploy lane.

``make deps-up`` creates ``proxyshop_w<N>`` and stops there -- ``scripts/db_init.py`` is
frozen and says so itself: *"this target exists so the database is there before anything
else (psql, a migration runner, a service started by hand) needs it."* Nothing in the repo
was that migration runner. The root ``conftest.py`` applies the migrations for **tests**,
via ``apps/trust/tests/_fixtures_ledger_schema.py``, so the test suite has never depended on
this file existing -- and the deployment, which has no conftest, got an empty database.

Measured on a stack brought up exactly as ``docs/demo/starting-slice.md`` says
(``cp .env.example .env`` -> ``make deps-up`` -> ``docker compose up -d``)::

    $ psql -U proxyshop -d proxyshop_w1 -c '\\dn'
          List of schemas
      Name  |       Owner
    --------+-------------------
     public | pg_database_owner
    (1 row)

    $ curl -s -o /dev/null -w '%{http_code}' localhost:8084/events/head
    503

No ``ledger``, no ``vault``, no ``app``, no ``sealed``: every database-backed route in the
deployment was answering against a database that held nothing, while ``docker compose ps``
reported every row ``(healthy)``.

This is a thin wrapper, deliberately. The migration runner itself is
:func:`apps.trust.src.ledger.apply_migrations`, which already serialises concurrent callers
on a Postgres advisory lock, runs each file in its own transaction, records what it applied
in ``ledger.schema_migrations``, and refuses to report success for having applied nothing.
Re-implementing any of that here would be a second source of truth for the schema.

Idempotent, and safe to run from several workers at once -- the same properties the test
fixtures rely on. Run it after ``make deps-up``::

    PROXYSHOP_WORKER=1 ./.venv/bin/python scripts/db_migrate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxyshop_support.postgres import (  # noqa: E402 - after the sys.path bootstrap
    WorkerDatabaseError,
    database_name,
    ensure_worker_database,
    role_dsn,
)
from proxyshop_support.worker import WorkerNotConfiguredError, worker_id  # noqa: E402


def main() -> int:
    try:
        worker = worker_id()
    except WorkerNotConfiguredError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    # The database has to exist before it can be migrated. Calling this rather than telling
    # the operator to run `make db-init` first keeps the two steps from drifting apart in
    # the runbook, and it is the same idempotent call that target makes.
    try:
        name = ensure_worker_database(worker)
    except WorkerDatabaseError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    # Imported here, not at module scope: `apps.trust.src.ledger` is a member package and
    # this script runs from the repo root, so the sys.path bootstrap above has to happen
    # first. `psycopg` likewise is only needed on the path that actually connects.
    import psycopg

    from apps.trust.src.ledger import apply_migrations

    # `admin` and not one of the four least-privilege roles: the migrations CREATE SCHEMA and
    # GRANT, which is exactly the authority D5 denies the service roles.
    dsn = role_dsn("admin", worker, database=name)
    try:
        with psycopg.connect(dsn, connect_timeout=10) as connection:
            applied = apply_migrations(connection)
    except psycopg.Error as exc:
        print(f"FATAL: could not migrate {name!r}: {exc}", file=sys.stderr)
        return 1

    print(f"OK: {database_name(worker)} migrated ({len(applied)} files: {', '.join(applied)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
