"""The Postgres-backed event store: T-011's writer, wearing the service's error vocabulary.

Owned by T-060. This module contains **no SQL that appends and no hashing at all**. Every
write goes through :func:`trust.ledger.append_event`, which is where D16's three
obligations already live -- one global chain ordered by insertion sequence, a lock on the
tail, and ``idempotency_key`` *is* ``event_id``. What this module adds is the part a
*service* needs and a library does not:

* **connections**, so that concurrent HTTP requests are concurrent database writers rather
  than one serialised connection pretending to be many;
* **a vocabulary**, so a caller learns whether to fix the event (:class:`InvalidEvent`),
  stop re-sending it (:class:`IdempotencyConflict`), or retry it (:class:`ChainForked`),
  instead of receiving ``UniqueViolation`` and guessing;
* **an answer when there is no database at all** (:class:`StoreUnavailable`), because a
  service whose datastore is down still has to say so -- see :func:`classify_connection_error`
  and :meth:`PostgresEventStore._connection`;
* **verification that names the broken link**, via :mod:`.integrity`.

Why idempotency is not enforced here
------------------------------------
It would be easy, and wrong, to keep a set of seen ``event_id`` values in this object and
check it before writing. Two workers do not share that set, and one worker's two threads
race it -- so the guarantee would hold in exactly the situation nobody tests and fail in
production. The guarantee belongs to ``commerce_events_idempotency_key_key``, a UNIQUE
constraint the database enforces against every writer that has ever existed, and this
module's job on a duplicate is to *recognise* the constraint firing and turn it into a
no-op. :func:`trust.ledger.append_event` additionally serialises writers on an advisory
lock, so at ``READ COMMITTED`` the second caller usually reads the committed row and never
reaches the constraint at all; the ``UniqueViolation`` branch below is what makes the
outcome the same either way.

The same reasoning covers forks: ``commerce_events_prev_hash_key`` means two events cannot
claim the same predecessor even if a writer skipped every lock, and the ``BEFORE INSERT``
chain-guard trigger refuses a ``prev_hash`` that is not the tail. Both surface here as
:class:`ChainForked` -- a *retry*, because the event was fine and only its predecessor
moved.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ..ledger import LedgerError, append_event, chain_anchor, head_hash, read_events
from ..ledger import stream_hash as _stream_hash
from .errors import (
    ChainForked,
    EventServiceError,
    IdempotencyConflict,
    InvalidEvent,
    StoreUnavailable,
)
from .integrity import anchored_report
from .store import AppendOutcome, normalise_event

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg

__all__ = [
    "DEFAULT_DSN_ENV",
    "PostgresEventStore",
    "classify_connection_error",
    "classify_write_error",
    "default_store",
]

#: Environment variables consulted, in order, for the ledger DSN when none is passed.
#:
#: ``PROXYSHOP_LEDGER_DSN`` first, so a deployment can point the writer somewhere without
#: disturbing the per-role variables the rest of the system reads.
#:
#: ``PROXYSHOP_PG_DSN_TRUST_RW`` second, because that is *this writer's own role*: it is
#: what ``proxyshop_support.postgres.ROLES["trust_rw"]`` designates, what ``.env.example``
#: documents, and what D5 grants the ledger. Leaving it out was a live defect rather than
#: an omission -- a deployment that configured the ledger the documented way got
#: :class:`StoreUnavailable` while the correct DSN sat unread in its environment, and a
#: deployment that set the generic variable too got something worse than an error: a
#: writer that connected happily as ``app``, a role with a different grant set, and said
#: nothing anywhere about having done so. ``apps/trust/compose.yaml`` still carries the
#: workaround that bug forced (an explicit ``PROXYSHOP_LEDGER_DSN``) and is now free to
#: drop it.
#:
#: ``PROXYSHOP_PG_DSN_APP`` last, and only last. It stays because three of the four
#: services hand this process nothing else, and removing it would turn their "wrong role"
#: into "no ledger at all"; it ranks below the per-role variable so that wherever both are
#: present the writer uses the privileges it was actually granted.
DEFAULT_DSN_ENV: tuple[str, ...] = (
    "PROXYSHOP_LEDGER_DSN",
    "PROXYSHOP_PG_DSN_TRUST_RW",
    "PROXYSHOP_PG_DSN_APP",
)

#: Constraints that mean "somebody else is already at this position in the chain". Both are
#: forks, not duplicates: the incoming event is fine and its predecessor moved.
_FORK_CONSTRAINTS = frozenset({"commerce_events_prev_hash_key", "commerce_events_event_hash_key"})

#: The constraint that means "this event is already in the chain".
_IDEMPOTENCY_CONSTRAINT = "commerce_events_idempotency_key_key"


#: The chain guard's message. It is a ``RAISE EXCEPTION`` inside a plpgsql trigger, so
#: nothing but its text distinguishes it from any other error the trigger could raise.
_CHAIN_GUARD_MESSAGE = "does not link to the chain tail"


def _constraint_of(exc: BaseException) -> str:
    """The constraint an integrity error names, or ``""``."""
    diag = getattr(exc, "diag", None)
    return str(getattr(diag, "constraint_name", "") or "")


def classify_write_error(exc: BaseException, event_id: str) -> EventServiceError | None:
    """Translate one database refusal into the service's vocabulary.

    Args:
        exc: what psycopg raised.
        event_id: the event that was being written, for the message.

    Returns:
        The refusal to raise instead, or ``None`` when this is not an error with a service
        meaning and the original should propagate untouched.

    **Why this is a function and not four ``except`` clauses.** The two ways the ledger
    refuses a fork arrive as *different* exception classes, and one of them was wrong here
    until a test caught it: the ``BEFORE INSERT`` chain guard raises
    ``USING ERRCODE = 'integrity_constraint_violation'``, which psycopg maps to
    :class:`psycopg.errors.IntegrityConstraintViolation` (SQLSTATE 23000) and **not** to
    ``RaiseException`` (P0001, what a bare plpgsql ``RAISE`` produces). An ``except
    RaiseException`` clause written for it therefore caught nothing at all, and a genuine
    fork -- the one case where the caller's correct response is simply to retry -- would
    have reached the client as an unhandled 500. Pulling the mapping out here makes it
    testable against the *real* exception objects the database produces, which is how the
    dead branch was found and is what
    ``apps/trust/tests/test_events.py`` now pins.
    """
    import psycopg

    if isinstance(exc, psycopg.errors.UniqueViolation):
        constraint = _constraint_of(exc)
        if constraint in _FORK_CONSTRAINTS:
            return ChainForked(
                f"event {event_id!r} was refused by {constraint}: another writer is already "
                f"at this position in the chain. The event is fine -- re-send it and it "
                f"will be sealed behind the new tail."
            )
        return None
    if isinstance(exc, psycopg.errors.CheckViolation):
        return InvalidEvent(
            f"event {event_id!r} violates {_constraint_of(exc) or 'a CHECK constraint'} on "
            f"ledger.commerce_events: {exc}"
        )
    if isinstance(
        exc, psycopg.errors.IntegrityConstraintViolation | psycopg.errors.RaiseException
    ) and _CHAIN_GUARD_MESSAGE in str(exc):
        return ChainForked(
            f"event {event_id!r} was refused by the chain guard: the tail moved between "
            f"reading it and writing behind it. Re-send the event."
        )
    return None


def classify_connection_error(exc: BaseException) -> StoreUnavailable | None:
    """Translate "there is no working connection" into the service's vocabulary.

    Args:
        exc: whatever failed while acquiring or using a connection.

    Returns:
        A :class:`StoreUnavailable` when ``exc`` means the datastore could not be reached,
        or ``None`` when it means something else and must propagate untouched.

    **Why this is separate from :func:`classify_write_error`, and why the two cannot
    overlap.** psycopg maps SQLSTATE classes 08 (connection exception), 53 (insufficient
    resources), 57 (operator intervention -- ``admin_shutdown``, ``crash_shutdown``,
    ``cannot_connect_now``) and 58 (system error) onto :class:`psycopg.OperationalError`,
    and maps class 23 (integrity constraint violation) onto :class:`psycopg.IntegrityError`.
    The two branches are therefore disjoint by construction: a duplicate ``event_id`` can
    never be mistaken for an outage, and an outage can never be reported as "your event is
    malformed". ``psycopg_pool.PoolTimeout`` / ``PoolClosed`` / ``TooManyRequests`` are all
    subclasses of ``OperationalError``, so waiting the pool out for a database that is down
    lands here too.

    ``InterfaceError`` is included because "the connection is already closed" is the same
    outage seen one moment later, and a bare ``OSError`` because a socket failure that
    escapes the driver is still the datastore being unreachable -- not a bad request.
    """
    import psycopg

    if isinstance(exc, EventServiceError):
        return None
    if isinstance(exc, psycopg.OperationalError | psycopg.InterfaceError | OSError):
        return StoreUnavailable(
            f"the ledger writer could not reach its datastore: {type(exc).__name__}: {exc}".rstrip()
        )
    return None


class PostgresEventStore:
    """An append-only event store over ``ledger.commerce_events``.

    Args:
        dsn: a libpq connection string. Resolved from :data:`DEFAULT_DSN_ENV` when omitted.
        connect: a factory returning an **autocommit** ``psycopg.Connection``, used instead
            of ``dsn``. The store closes what the factory returns, one connection per
            operation; pass a pool-backed factory if that is not what you want.
        max_size: pool size when ``psycopg_pool`` is available. It bounds how many appends
            can be in flight, not how many can succeed -- the chain's advisory lock
            serialises them regardless.
        connect_timeout: seconds libpq waits for a single connection attempt.
        pool_timeout: seconds a caller waits for a pooled connection before the request is
            refused as :class:`StoreUnavailable`. Defaults to twice ``connect_timeout``.

    The connection resource is opened **lazily**, on the first operation. Constructing this
    object therefore never touches the network, which is what lets
    :func:`trust.main.create_app` mount the router in a process that has no database.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        connect: Callable[[], psycopg.Connection] | None = None,
        max_size: int = 16,
        connect_timeout: int = 5,
        pool_timeout: float | None = None,
    ) -> None:
        self._dsn = dsn
        self._connect = connect
        self._max_size = max_size
        self._connect_timeout = connect_timeout
        # `psycopg_pool`'s own default is 30 seconds, which is not a wait -- it is an
        # outage. Against a Postgres that is down, every request parked an HTTP worker for
        # half a minute and then failed anyway, so a restarting database took the writer's
        # whole thread pool with it and the caller's client usually timed out first and saw
        # nothing at all. The healthy-path wait for a free connection is sub-millisecond
        # (the pool holds `max_size` of them and an append is one short transaction), so
        # anything on the order of seconds is already "the datastore is not serving".
        self._pool_timeout = (
            float(pool_timeout) if pool_timeout is not None else max(2.0 * connect_timeout, 1.0)
        )
        self._pool: Any | None = None
        self._pool_attempted = False

    # -- connections ------------------------------------------------------------------
    def _resolve_dsn(self) -> str:
        import os

        if self._dsn:
            return self._dsn
        for name in DEFAULT_DSN_ENV:
            value = os.environ.get(name)
            if value:
                return value
        raise StoreUnavailable(
            f"the ledger writer has no database to write to: no DSN was passed and none of "
            f"{list(DEFAULT_DSN_ENV)} is set. Set one, or inject a store on "
            f"`app.state.event_store`."
        )

    def _ensure_pool(self) -> Any | None:
        """A connection pool, or ``None`` when ``psycopg_pool`` is not installed."""
        if self._pool is not None or self._pool_attempted:
            return self._pool
        self._pool_attempted = True
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:  # pragma: no cover - psycopg_pool ships with this venv
            return None
        self._pool = ConnectionPool(
            self._resolve_dsn(),
            min_size=1,
            max_size=self._max_size,
            kwargs={"autocommit": True, "connect_timeout": self._connect_timeout},
            timeout=self._pool_timeout,
            open=True,
            name="proxyshop-ledger-writer",
        )
        return self._pool

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        """One autocommit connection for one operation, or :class:`StoreUnavailable`.

        Autocommit is not a detail: :func:`trust.ledger.append_event` refuses a connection
        with a transaction already open, because its chain lock is transaction-scoped and
        would otherwise be held until the *caller* commits -- blocking every other ledger
        writer in the system for that whole span.

        **The acquisition is inside the guard, and that is the whole point of this shape.**
        It used to be outside: every method opened its connection here and only the *SQL*
        ran inside :meth:`_append_on`'s ``except`` clauses, so an unreachable or restarting
        Postgres never became a :class:`StoreUnavailable`. ``psycopg.OperationalError`` --
        the single thing a dead database produces -- walked past every handler and reached
        the client as a bare ``500`` with an empty body, which tells a caller nothing and,
        worse, tells it the opposite of the truth: 5xx-without-a-code reads as "your request
        broke the server, do not retry", when the correct reading was "the ledger is down,
        retry". Wrapping the acquisition *and* the body means every operation on this store
        -- append, read, head, verify, replay, get -- reports an outage the one documented
        way, instead of each caller re-discovering it.

        The body is inside the guard as well, deliberately: a connection that dies mid
        statement is the same outage noticed a moment later, and ``classify_connection_error``
        is narrow enough (SQLSTATE 08/53/57/58) that a constraint refusal cannot be
        swallowed by it.
        """
        try:
            if self._connect is not None:
                connection = self._connect()
                try:
                    yield connection
                finally:
                    connection.close()
                return

            pool = self._ensure_pool()
            if pool is not None:
                with pool.connection() as connection:
                    yield connection
                return

            import psycopg

            connection = psycopg.connect(
                self._resolve_dsn(), autocommit=True, connect_timeout=self._connect_timeout
            )
            try:
                yield connection
            finally:
                connection.close()
        except Exception as exc:
            # `EventServiceError` passes straight through -- `classify_connection_error`
            # returns None for it -- so the "no DSN is configured" 503 keeps its own message
            # and is not relabelled as a dead server. The two outages have different fixes.
            unavailable = classify_connection_error(exc)
            if unavailable is None:
                raise
            raise unavailable from exc

    def close(self) -> None:
        """Release the pool, if one was opened. Safe to call more than once."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    # -- the write --------------------------------------------------------------------
    def append(self, event: Mapping[str, Any]) -> AppendOutcome:
        """Append one event to the global chain, or return the one already stored.

        Raises:
            InvalidEvent / UnknownEventKind: the event cannot be stored as sent.
            IdempotencyConflict: this ``event_id`` is in the chain with different content.
            ChainForked: another writer committed behind the tail this linked to; retry.
        """
        body = normalise_event(event)
        with self._connection() as connection:
            result = self._append_on(connection, body)
            return AppendOutcome(
                event=result.event,
                inserted=result.inserted,
                head_hash=result.head_hash,
                seq=int(result.event["seq"]),
                length=int(chain_anchor(connection)["length"]),
            )

    def _append_on(self, connection: psycopg.Connection, body: Mapping[str, Any]) -> Any:
        """``append_event`` with the database's refusals translated. Returns ``AppendResult``."""
        import psycopg

        event_id = str(body["event_id"])
        try:
            return append_event(connection, body)
        except psycopg.errors.UniqueViolation as exc:
            if _constraint_of(exc) == _IDEMPOTENCY_CONSTRAINT:
                # The DATABASE caught the duplicate -- two writers reached the insert
                # concurrently. Re-entering `append_event` now finds the committed row and
                # returns it as the no-op it is; if the content differs, it raises, and the
                # LedgerError handler below turns that into an IdempotencyConflict.
                return self._append_on(connection, body)
            fork = classify_write_error(exc, event_id)
            if fork is None:
                raise
            raise fork from exc
        except psycopg.errors.Error as exc:
            refusal = classify_write_error(exc, event_id)
            if refusal is None:
                raise
            raise refusal from exc
        except LedgerError as exc:
            # `append_event` raises this for an id already present with DIFFERENT content,
            # and for a handful of shapes `normalise_event` has already excluded. The row's
            # existence is the discriminator -- no message matching, which would break the
            # day that message is reworded.
            if self._holds(connection, event_id):
                raise IdempotencyConflict(str(exc)) from exc
            raise InvalidEvent(str(exc)) from exc

    @staticmethod
    def _holds(connection: psycopg.Connection, event_id: str) -> bool:
        """Is ``event_id`` already in the chain? One index lookup, no table scan."""
        with connection.cursor() as cur:
            cur.execute(
                "select 1 from ledger.commerce_events where idempotency_key = %s", (event_id,)
            )
            return cur.fetchone() is not None

    def append_all(self, events: Sequence[Mapping[str, Any]]) -> list[AppendOutcome]:
        """Append a sequence in order, one transaction each (as ``append_events`` does)."""
        return [self.append(event) for event in events]

    # -- reads ------------------------------------------------------------------------
    def read(
        self,
        *,
        after_seq: int = 0,
        store_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """The chain in insertion sequence. A ``store_id`` filter yields a projection, not
        a chain -- the links skip what was filtered out, so never verify the result."""
        with self._connection() as connection:
            return read_events(connection, after_seq=after_seq, store_id=store_id, limit=limit)

    def get(self, event_id: str) -> dict[str, Any] | None:
        """One event by ``event_id``, or ``None``. **One index probe, not a scan.**

        The predicate belongs in the query. This used to read the *entire ledger* --
        ``read_events(connection)`` with no filter and no limit -- and then linear-scan the
        result in Python, once per request, on the endpoint a retrying client hits hardest.
        On an append-only history that only ever grows, "is this one event there" cost
        O(ledger) in rows fetched, bytes off the wire and Python objects built, and the
        answer was one row.

        ``idempotency_key`` carries ``commerce_events_idempotency_key_key``, a UNIQUE index,
        so the filtered read is a single index probe. ``limit=1`` is belt and braces: the
        constraint already makes at most one row possible, and the limit means a future
        schema that relaxed it would still not turn this method back into a scan.
        """
        with self._connection() as connection:
            rows = read_events(connection, event_id=event_id, limit=1)
        return rows[0] if rows else None

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """Every event, oldest first. Read from the ledger on every access -- there is no
        cache, which is what makes the replay endpoint a replay rather than a recital."""
        return tuple(self.read())

    @property
    def head_hash(self) -> str:
        """The stored chain head, or the genesis link for an empty chain."""
        with self._connection() as connection:
            return head_hash(connection)

    @property
    def length(self) -> int:
        """How many events the chain holds, from the anchor."""
        return int(self.anchor()["length"])

    def anchor(self) -> dict[str, Any]:
        """``ledger.chain_head``: ``{head_hash, length, last_seq, updated_at}``.

        Maintained by an ``AFTER INSERT`` trigger, so it is accurate even for rows written
        by something that never called this module.
        """
        with self._connection() as connection:
            return chain_anchor(connection)

    def stream_hash(self) -> str:
        """The stream's identity, recomputed from every row's content."""
        with self._connection() as connection:
            return _stream_hash(read_events(connection))

    def _read_with_anchor(self) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """The rows and the commitment, read on **one** connection in that order.

        Two connections, or two round trips a write could slip between, would compare the
        length of one stream against the anchor of another and report a truncation that
        never happened.
        """
        with self._connection() as connection:
            events = read_events(connection)
            try:
                anchor: dict[str, Any] | None = chain_anchor(connection)
            except LedgerError:
                anchor = None
        return events, anchor

    def verify(self) -> dict[str, Any]:
        """Verify the links **and** the anchor, and name the link that broke."""
        return anchored_report(*self._read_with_anchor())

    def replay(self) -> dict[str, Any]:
        """The whole chain, replayed from the ledger: verification plus every event.

        Nothing here is remembered between calls -- the events come out of Postgres on
        every request. That is the property that makes this a replay: an endpoint serving a
        list it had been holding in memory since the write would reproduce the writer's
        state, not the ledger's, and would go on doing so after the ledger was emptied.
        """
        events, anchor = self._read_with_anchor()
        report = anchored_report(events, anchor)
        report["events"] = events
        return report

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<PostgresEventStore dsn={'set' if self._dsn else 'from env'}>"


def default_store() -> PostgresEventStore:
    """The process-wide writer, built from the environment.

    Constructing it opens nothing, so this is safe at import time and safe in a process
    with no database; the :class:`~.errors.StoreUnavailable` arrives on first use, where it
    can be reported as a 503 rather than crashing the application.
    """
    return PostgresEventStore()
