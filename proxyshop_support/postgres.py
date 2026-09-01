"""Per-worker Postgres databases (D5/D38). Orchestrator-owned (T-000), frozen.

D38: *every worker owns its own database,* ``proxyshop_w<N>``. Nothing in the scaffold used
to create one — ``db/init/00-roles.sql`` creates only cluster-global roles, compose creates
only ``proxyshop_template``, and no script or Make target created the per-worker database at
all. The result was a scaffold in which **every** Postgres test skipped, forever, and a
ticket whose entire subject is the database reported a green verify having never connected.
A test that skips is not a test that passes, so this module exists to make three things
true:

1. the per-worker database **exists by the time tests run** (:func:`ensure_worker_database`,
   called by the session fixtures in the root ``conftest.py`` and by ``make db-init``);
2. connection strings are **derived from the worker index**, never copied statically out of
   ``.env`` — worker 3 could otherwise inherit ``.env``'s ``proxyshop_w1`` and share a
   database with worker 1, which is the exact failure D38 exists to prevent;
3. an absent database or an unset ``.env`` is a **loud failure**, not a skip.

Every value has a working default matching ``docker-compose.yml``, so a fresh clone with no
``.env`` at all still connects. The ``PROXYSHOP_PG_DSN_*`` variables remain honoured for
host/port/credential overrides; only the *database* component is authoritative here.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from proxyshop_support.worker import worker_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg

#: Per-worker database names are ``proxyshop_w0``, ``proxyshop_w1``, ...
DATABASE_PREFIX = "proxyshop_w"

#: Created by compose (``POSTGRES_DB``); ``db/init/00-roles.sql`` runs inside it, so every
#: per-worker database is cloned FROM it and inherits the ``public``-schema revoke.
TEMPLATE_DATABASE = "proxyshop_template"

#: Where ``CREATE DATABASE`` is issued from — you cannot create a database while connected
#: to the one being cloned.
MAINTENANCE_DATABASE = "postgres"

#: role -> (env var carrying an override DSN, username, password). Passwords are the
#: development placeholders from ``.env.example`` / ``db/init/00-roles.sql``; nothing here
#: is a real credential.
ROLES: dict[str, tuple[str, str, str]] = {
    "admin": ("PROXYSHOP_PG_DSN_ADMIN", "proxyshop", "proxyshop_dev_pw"),
    "exchange": ("PROXYSHOP_PG_DSN_EXCHANGE", "exchange", "x"),
    "trust_rw": ("PROXYSHOP_PG_DSN_TRUST_RW", "trust_rw", "x"),
    "buyer_vault": ("PROXYSHOP_PG_DSN_VAULT", "buyer_vault", "x"),
    "app": ("PROXYSHOP_PG_DSN_APP", "app", "x"),
}


class WorkerDatabaseError(RuntimeError):
    """The per-worker Postgres database could not be created or reached.

    Deliberately an error and not a skip: a silently-skipped datastore test looks exactly
    like a passing one in the metrics.
    """


def database_name(worker: int | None = None) -> str:
    """This worker's database name, e.g. ``proxyshop_w3``."""
    return f"{DATABASE_PREFIX}{worker_id() if worker is None else worker}"


def _default_dsn(role: str) -> str:
    _, user, password = ROLES[role]
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{DATABASE_PREFIX}0"


def _with_database(dsn: str, database: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def role_dsn(role: str, worker: int | None = None, *, database: str | None = None) -> str:
    """A DSN for ``role`` against this worker's database.

    Args:
        role: one of :data:`ROLES` — ``admin``, ``exchange``, ``trust_rw``,
            ``buyer_vault``, ``app``. Anything else raises ``KeyError`` rather than
            silently connecting as the wrong principal.
        worker: worker index. Defaults to ``$PROXYSHOP_WORKER``.
        database: override the database component (used for the maintenance connection).

    Returns:
        The DSN from ``$PROXYSHOP_PG_DSN_*`` if that variable is set, otherwise the compose
        default — in **both** cases with the database component rewritten to this worker's
        database, so a shared ``.env`` cannot make two workers collide.
    """
    if role not in ROLES:
        raise KeyError(f"unknown role {role!r}; expected one of {sorted(ROLES)}")
    env_var = ROLES[role][0]
    base = os.environ.get(env_var) or _default_dsn(role)
    return _with_database(base, database or database_name(worker))


def maintenance_dsn(worker: int | None = None) -> str:
    """Admin DSN pointed at the maintenance database, for ``CREATE DATABASE``."""
    return role_dsn("admin", worker, database=MAINTENANCE_DATABASE)


def database_exists(connection: psycopg.Connection, name: str) -> bool:
    """Is there a database called ``name`` in this cluster?"""
    with connection.cursor() as cur:
        cur.execute("select 1 from pg_database where datname = %s", (name,))
        return cur.fetchone() is not None


def ensure_worker_database(worker: int | None = None, *, attempts: int = 4) -> str:
    """Create ``proxyshop_w<N>`` if it does not exist, and return its name.

    Idempotent and safe to call from several workers at once: the create is guarded by an
    existence check and the "somebody else just created it" race is caught explicitly.

    Args:
        worker: worker index. Defaults to ``$PROXYSHOP_WORKER``.
        attempts: retries for ``CREATE DATABASE ... TEMPLATE``, which fails with
            ``ObjectInUse`` while any session is still connected to the template (the
            compose healthcheck connects every 5 s).

    Raises:
        WorkerDatabaseError: the database does not exist and could not be created. Loud on
            purpose — see the module docstring.
    """
    import psycopg

    name = database_name(worker)
    try:
        connection = psycopg.connect(maintenance_dsn(worker), autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        raise WorkerDatabaseError(
            f"cannot reach Postgres to create {name!r}: {exc}\n"
            f"Bring the stack up with `make deps-up` (which also runs `make db-init`)."
        ) from exc

    with connection:
        if database_exists(connection, name):
            return name
        template = TEMPLATE_DATABASE if database_exists(connection, TEMPLATE_DATABASE) else None
        clause = f' TEMPLATE "{template}"' if template else ""
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                with connection.cursor() as cur:
                    cur.execute(f'CREATE DATABASE "{name}"{clause}')
                return name
            except psycopg.errors.DuplicateDatabase:
                return name  # another worker won the race; that is a success
            except psycopg.errors.ObjectInUse as exc:  # template still has a live session
                last = exc
                time.sleep(0.5 * (attempt + 1))
            except psycopg.Error as exc:
                raise WorkerDatabaseError(f"could not create database {name!r}: {exc}") from exc
        raise WorkerDatabaseError(
            f"could not create database {name!r} from template {template!r} after "
            f"{attempts} attempts: {last}"
        )
