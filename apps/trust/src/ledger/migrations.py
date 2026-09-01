"""Apply ``db/migrations/*.sql`` to a Postgres database. Owned by T-011.

D39 splits the schema in two halves and this module applies the half that is per-database.
Roles are cluster-global (``pg_authid.relisshared = true``) and are created once by
``db/init/00-roles.sql``; **grants are per-database**, so every ``proxyshop_w<n>`` needs its
own copy and they live in the migrations. The migrations re-create the roles idempotently
too, because a database created from ``template1`` -- or on a volume where the initdb hook
already ran -- never saw that file.

Every migration is written to be re-runnable: ``CREATE ... IF NOT EXISTS``,
``CREATE OR REPLACE``, guarded ``CREATE ROLE``, and GRANT/REVOKE, which are idempotent by
construction. That is what makes D39's acceptance criterion checkable -- *the migrations
apply cleanly a second time, into a second database on the same cluster* -- and it is why
this runner simply executes every file on every run rather than consulting a ledger of
what it applied. :data:`SCHEMA_MIGRATIONS_TABLE` is written for the record, never read to
decide.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg

#: Environment override, so a caller can point the runner at a checkout other than the one
#: this module was imported from (the second-database proof does not need it; a packaged
#: deployment would).
MIGRATIONS_DIR_ENV = "PROXYSHOP_MIGRATIONS_DIR"

#: Repo-relative location of the SQL.
MIGRATIONS_RELATIVE_PATH = "db/migrations"

#: Bookkeeping only. Created by ``0001_schemas_roles_grants.sql``.
SCHEMA_MIGRATIONS_TABLE = "ledger.schema_migrations"


class MigrationDriftError(RuntimeError):
    """A migration file changed after this database had already applied it."""


def repo_root() -> Path:
    """The checkout root, derived from this file's own resolved location.

    ``.pkgroot/trust`` is a symlink to ``apps/trust/src``, so this module can be imported as
    either ``trust.ledger.migrations`` or ``apps.trust.src.ledger.migrations``. ``resolve()``
    collapses the symlink first, which makes the four ``parents`` hops the same either way.
    """
    return Path(__file__).resolve().parents[4]


def migrations_dir() -> Path:
    """Where the ``.sql`` files live -- ``$PROXYSHOP_MIGRATIONS_DIR`` or ``db/migrations``."""
    override = os.environ.get(MIGRATIONS_DIR_ENV)
    if override:
        return Path(override)
    return repo_root() / MIGRATIONS_RELATIVE_PATH


def migration_files(directory: Path | None = None) -> list[Path]:
    """Every migration, in lexicographic filename order.

    The zero-padded ``NNNN_`` prefix is what makes lexicographic order the dependency order:
    ``0001`` creates the schemas the rest need, and ``0004`` grants privileges on the tables
    ``0002`` and ``0003`` create, so it must run last.
    """
    return sorted((directory or migrations_dir()).glob("*.sql"))


def checksum(path: Path) -> str:
    """SHA-256 of a migration file, recorded alongside its name."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migrations(
    connection: psycopg.Connection,
    *,
    files: Sequence[Path] | None = None,
    strict: bool = False,
) -> list[str]:
    """Execute every migration against ``connection`` and return the filenames applied.

    Args:
        connection: an open ``psycopg.Connection`` **with the privileges to create schemas
            and roles** -- in practice the ``admin`` role. Autocommit and transactional
            connections both work; each file runs inside its own explicit transaction, so a
            file that fails half way leaves nothing of itself behind.
        files: override the file list (used by the tests that apply a partial set).
        strict: refuse to apply a file whose content has changed since it was first
            recorded against this database. Off by default -- a developer editing a
            migration and re-running against a live worker database is the normal loop, and
            the files are idempotent -- but on for anything that must not silently accept
            a rewritten history.

    Returns:
        The filenames applied, in order.

    Raises:
        FileNotFoundError: the migrations directory holds no ``.sql`` files. Silently
            applying nothing and reporting success is how a database ends up empty while
            its gate is green.
        MigrationDriftError: ``strict`` and a file's checksum differs from the recorded one.
        psycopg.Error: whatever the server said, unchanged. A migration that cannot apply
            is not something to work around.
    """
    paths = list(files) if files is not None else migration_files()
    if not paths:
        raise FileNotFoundError(
            f"no .sql migrations under {migrations_dir()} -- refusing to report success "
            f"for having applied nothing"
        )
    if strict:
        drift = drifted_migrations(connection, files=paths)
        if drift:
            raise MigrationDriftError(
                "these migrations differ from the version this database recorded:\n  "
                + "\n  ".join(
                    f"{name}: recorded {was[:12]}..., file is now {now[:12]}..."
                    for name, was, now in drift
                )
                + "\nThe recorded checksum is the FIRST-applied one and is never "
                "overwritten, so this is a real edit to an already-applied migration. "
                "Re-create the database, or apply with strict=False if the edit is "
                "intended and the migrations are still idempotent."
            )
    applied: list[str] = []
    for path in paths:
        sql = path.read_text(encoding="utf-8")
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(sql)  # type: ignore[arg-type]
        _record(connection, path)
        applied.append(path.name)
    return applied


def _record(connection: psycopg.Connection, path: Path) -> None:
    """Note that ``path`` was applied.

    ``checksum`` is written **once** and never updated. It used to be overwritten on every
    run, which erased the one thing it exists to detect: a migration edited after it had
    already been applied somewhere. Only ``applied_at`` moves.
    """
    with connection.transaction():
        with connection.cursor() as cur:
            cur.execute(
                f"insert into {SCHEMA_MIGRATIONS_TABLE} (filename, checksum) "
                f"values (%s, %s) "
                f"on conflict (filename) do update set applied_at = now()",  # noqa: S608
                (path.name, checksum(path)),
            )


def applied_migrations(connection: psycopg.Connection) -> list[tuple[str, str]]:
    """``[(filename, checksum), ...]`` as recorded in the database, in filename order.

    The checksum is the one recorded when the file was **first** applied here.
    """
    with connection.cursor() as cur:
        cur.execute(
            f"select filename, checksum from {SCHEMA_MIGRATIONS_TABLE} order by filename"  # noqa: S608
        )
        return [(str(row[0]), str(row[1])) for row in cur.fetchall()]


def drifted_migrations(
    connection: psycopg.Connection, *, files: Sequence[Path] | None = None
) -> list[tuple[str, str, str]]:
    """Files whose content changed since this database first applied them.

    Returns:
        ``[(filename, recorded_checksum, current_checksum), ...]`` -- empty when nothing has
        drifted. A file that has never been applied here is not drift.
    """
    try:
        recorded = dict(applied_migrations(connection))
    except Exception:  # the bookkeeping table does not exist yet: nothing can have drifted
        connection.rollback()
        return []
    paths = list(files) if files is not None else migration_files()
    drift = []
    for path in paths:
        was = recorded.get(path.name)
        now = checksum(path)
        if was is not None and was != now:
            drift.append((path.name, was, now))
    return drift


def statements(sql: str) -> Iterable[str]:
    """Split ``sql`` on top-level semicolons, ignoring dollar-quoted bodies.

    Only used by tests that want to assert on individual statements; the runner hands whole
    files to the server, which parses them properly.
    """
    out: list[str] = []
    buffer: list[str] = []
    tag: str | None = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if tag is None and character == "$":
            end = sql.find("$", index + 1)
            candidate = sql[index : end + 1] if end != -1 else None
            if candidate and (candidate[1:-1] == "" or candidate[1:-1].isidentifier()):
                tag = candidate
                buffer.append(candidate)
                index = end + 1
                continue
        elif tag is not None and sql.startswith(tag, index):
            buffer.append(tag)
            index += len(tag)
            tag = None
            continue
        if tag is None and character == ";":
            statement = "".join(buffer).strip()
            if statement:
                out.append(statement)
            buffer = []
        else:
            buffer.append(character)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        out.append(tail)
    return out
