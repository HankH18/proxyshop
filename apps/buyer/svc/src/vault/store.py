"""Storage behind :class:`~buyer_svc.vault.PseudonymVault` — the email↔pseudonym history.

Owned by T-070. Two implementations of one protocol:

* :class:`InMemoryPseudonymStore` — the default. Process-local, used by unit tests and by a
  service booted without a database.
* :class:`PostgresPseudonymStore` — writes ``vault.pseudonym_history``, the table T-011
  already shipped (``db/migrations/0003_sealed_vault_app_tables.sql``). It takes a live
  connection rather than a DSN so the *caller* decides which role it authenticates as, and
  the only role that can reach the schema is ``buyer_vault`` (T-011's grant model, D5). A
  service handed an ``exchange`` connection does not get a degraded vault; it gets
  ``InsufficientPrivilege`` on the first statement.

Why "never reissued" is enforced in two places
----------------------------------------------
:meth:`PseudonymStore.is_known` is the application-level check the vault consults before it
hands a pseudonym out. The Postgres table additionally carries
``pseudonym_history_pseudonym_key UNIQUE (pseudonym)``, so a second process racing the
first cannot slip a duplicate past the read. The in-memory store reproduces that constraint
by raising the same error class, so a unit test and a database test fail the same way.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "InMemoryPseudonymStore",
    "PostgresPseudonymStore",
    "PseudonymRecord",
    "PseudonymReissued",
    "PseudonymStore",
]


class PseudonymReissued(Exception):
    """A pseudonym already present in the history was offered a second time (R5)."""


@dataclass(frozen=True)
class PseudonymRecord:
    """One row of the identity vault: which pseudonym a buyer held, and when."""

    history_id: str
    email: str
    pseudonym: str
    issued_at: datetime
    retired_at: datetime | None = None

    @property
    def active(self) -> bool:
        """Is this the buyer's current pseudonym?"""
        return self.retired_at is None


@runtime_checkable
class PseudonymStore(Protocol):
    """The persistence seam the vault talks to."""

    def is_known(self, pseudonym: str) -> bool:
        """Has this pseudonym EVER been issued, to anybody? (R5: never reissued.)"""

    def record(self, email: str, pseudonym: str, issued_at: datetime) -> PseudonymRecord:
        """Retire ``email``'s current pseudonym and append ``pseudonym`` as the new one.

        Raises:
            PseudonymReissued: ``pseudonym`` is already in the history.
        """

    def history(self, email: str) -> list[PseudonymRecord]:
        """Every pseudonym ``email`` has ever held, oldest first."""

    def resolve(self, pseudonym: str) -> str | None:
        """The email behind ``pseudonym``, or ``None``. Reachable only by the vault role."""


class InMemoryPseudonymStore:
    """Process-local history. The default store, and the one unit tests use."""

    def __init__(self) -> None:
        self._rows: list[PseudonymRecord] = []
        self._by_pseudonym: dict[str, PseudonymRecord] = {}

    def __len__(self) -> int:
        return len(self._rows)

    def __iter__(self) -> Iterator[PseudonymRecord]:
        return iter(self._rows)

    def is_known(self, pseudonym: str) -> bool:
        return pseudonym in self._by_pseudonym

    def record(self, email: str, pseudonym: str, issued_at: datetime) -> PseudonymRecord:
        if pseudonym in self._by_pseudonym:
            raise PseudonymReissued(
                f"pseudonym {pseudonym!r} is already in the vault history and can never be "
                f"issued again (R5)"
            )
        retired: list[PseudonymRecord] = []
        for index, row in enumerate(self._rows):
            if row.email == email and row.active:
                closed = PseudonymRecord(
                    history_id=row.history_id,
                    email=row.email,
                    pseudonym=row.pseudonym,
                    issued_at=row.issued_at,
                    retired_at=max(issued_at, row.issued_at),
                )
                self._rows[index] = closed
                retired.append(closed)
        for closed in retired:
            self._by_pseudonym[closed.pseudonym] = closed

        row = PseudonymRecord(
            history_id=uuid.uuid4().hex,
            email=email,
            pseudonym=pseudonym,
            issued_at=issued_at,
        )
        self._rows.append(row)
        self._by_pseudonym[pseudonym] = row
        return row

    def history(self, email: str) -> list[PseudonymRecord]:
        return [row for row in self._rows if row.email == email]

    def resolve(self, pseudonym: str) -> str | None:
        row = self._by_pseudonym.get(pseudonym)
        return None if row is None else row.email


class PostgresPseudonymStore:
    """``vault.pseudonym_history``, reached through a caller-supplied connection.

    The connection carries the identity: hand it one authenticated as ``buyer_vault`` and
    every method works; hand it one authenticated as ``exchange``, ``trust_rw`` or ``app``
    and the first statement raises ``psycopg.errors.InsufficientPrivilege``. That is the
    whole point of D5's grant model and this class deliberately does nothing to soften it.
    """

    #: Fully-qualified table name. Never interpolated from caller input.
    TABLE = "vault.pseudonym_history"

    def __init__(self, connection: Any, *, autocommit: bool = False) -> None:
        self._connection = connection
        self._autocommit = autocommit

    @property
    def connection(self) -> Any:
        return self._connection

    def _commit(self) -> None:
        if self._autocommit:
            self._connection.commit()

    def is_known(self, pseudonym: str) -> bool:
        with self._connection.cursor() as cur:
            cur.execute(
                "select 1 from vault.pseudonym_history where pseudonym = %s limit 1",
                (pseudonym,),
            )
            return cur.fetchone() is not None

    def record(self, email: str, pseudonym: str, issued_at: datetime) -> PseudonymRecord:
        # Imported here rather than at module scope: the in-memory store is the default and
        # must stay importable in a process that never opens a database connection.
        import psycopg

        history_id = uuid.uuid4().hex
        with self._connection.cursor() as cur:
            # Retire whatever the buyer currently holds. `greatest` keeps the
            # `pseudonym_history_window_ordered` CHECK satisfied when a frozen test clock
            # hands us an issued_at that predates the open row.
            cur.execute(
                "update vault.pseudonym_history "
                "   set retired_at = greatest(%s, issued_at) "
                " where email = %s and retired_at is null",
                (issued_at, email),
            )
            try:
                cur.execute(
                    "insert into vault.pseudonym_history "
                    "       (history_id, email, pseudonym, issued_at) "
                    "values (%s, %s, %s, %s)",
                    (history_id, email, pseudonym, issued_at),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise PseudonymReissued(
                    f"pseudonym {pseudonym!r} is already in the vault history and can "
                    f"never be issued again (R5)"
                ) from exc
        self._commit()
        return PseudonymRecord(
            history_id=history_id, email=email, pseudonym=pseudonym, issued_at=issued_at
        )

    def history(self, email: str) -> list[PseudonymRecord]:
        with self._connection.cursor() as cur:
            cur.execute(
                "select history_id, email, pseudonym, issued_at, retired_at "
                "  from vault.pseudonym_history where email = %s "
                " order by issued_at, history_id",
                (email,),
            )
            return [PseudonymRecord(*row) for row in cur.fetchall()]

    def resolve(self, pseudonym: str) -> str | None:
        with self._connection.cursor() as cur:
            cur.execute(
                "select email from vault.pseudonym_history where pseudonym = %s",
                (pseudonym,),
            )
            row = cur.fetchone()
            return None if row is None else str(row[0])
