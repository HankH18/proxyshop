"""The Postgres side of the hash chain: append, read back, verify. Owned by T-011.

D16 in three obligations, and this module is where each of them is discharged:

1. **One global chain, ordered by insertion sequence.** ``ledger.commerce_events.seq`` is
   that order. Reads come back ``ORDER BY seq``, never by timestamp -- ``ts`` is the
   business instant and two events can legitimately share one.
2. **The writer takes a lock on the chain tail.** :func:`append_event` takes a transaction-
   scoped advisory lock keyed on the chain, *and* selects the tail ``FOR UPDATE``. Both,
   for different reasons: the row lock is the tail lock D16 names, and the advisory lock
   covers the case the row lock cannot -- an empty table, where there is no tail row to
   lock and two concurrent writers would otherwise both compute ``prev_hash = GENESIS``.
   The database refuses the loser either way (``commerce_events_prev_hash_key`` makes a
   fork a unique violation), so the lock is what turns a crash into a wait.
3. **``idempotency_key`` IS ``event_id``.** Re-appending an event already in the chain is a
   no-op that returns the row already there -- the stream length and the head hash do not
   move.

The hash itself is computed by :mod:`.canonical`; nothing here re-derives it. And a value
that has been through ``timestamptz`` and ``jsonb`` canonicalises to the bytes it went in
as, which is why :func:`read_events` output verifies against :func:`~.chain.verify_chain`
without a re-seal.
"""

from __future__ import annotations

import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .canonical import GENESIS_HASH, canonical_event, compute_event_hash, rfc3339_ms
from .chain import stream_hash, verify_chain
from .errors import LedgerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg

__all__ = [
    "CHAIN_LOCK_KEY",
    "AppendResult",
    "append_event",
    "chain_tail",
    "db_stream_hash",
    "read_events",
    "verify_chain_in_db",
]

#: The advisory-lock key for the single global chain. Derived from a stable name rather than
#: written as a magic integer, so it cannot silently collide with another subsystem's lock
#: and cannot drift if the table is ever renamed without a thought.
CHAIN_LOCK_KEY = zlib.crc32(b"proxyshop.ledger.commerce_events") & 0x7FFFFFFF

_COLUMNS = (
    "seq",
    "idempotency_key",
    "kind",
    "auction_id",
    "store_id",
    "order_ref",
    "occurred_at",
    "payload",
    "prev_hash",
    "event_hash",
)

_SELECT = f"select {', '.join(_COLUMNS)} from ledger.commerce_events"


@dataclass(frozen=True)
class AppendResult:
    """What :func:`append_event` did.

    Attributes:
        event: the stored row as a ``LedgerEvent`` dict, chain fields included.
        inserted: ``False`` when the ``event_id`` was already in the chain. The caller gets
            the row that is actually there, which is the one every other reader will see.
        head_hash: the chain head after the call. Unchanged when ``inserted`` is ``False``.
    """

    event: dict[str, Any]
    inserted: bool
    head_hash: str


def _row_to_event(row: Sequence[Any]) -> dict[str, Any]:
    """One ``commerce_events`` row as a ``LedgerEvent`` dict.

    Absent optional columns are dropped rather than carried as ``None`` -- that is the same
    rule :func:`~.canonical.canonical_event` applies, and it is what makes a row read back
    out of Postgres hash to the value that was written.
    """
    (seq, event_id, kind, auction_id, store_id, order_ref, occurred_at, payload, prev, digest) = row
    event: dict[str, Any] = {
        "event_id": event_id,
        "ts": rfc3339_ms(occurred_at),
        "kind": kind,
        "payload": payload if payload is not None else {},
    }
    if auction_id is not None:
        event["auction_id"] = auction_id
    if store_id is not None:
        event["store_id"] = store_id
    if order_ref is not None:
        event["order_ref"] = order_ref
    event["prev_hash"] = prev
    event["event_hash"] = digest
    event["seq"] = seq
    return event


def chain_tail(
    connection: psycopg.Connection, *, for_update: bool = False
) -> dict[str, Any] | None:
    """The last event in the chain, or ``None`` when the chain is empty.

    Args:
        connection: any connection that can read ``ledger.commerce_events``.
        for_update: take a row lock on the tail. This is the lock D16 names; it is only
            meaningful inside a transaction, and :func:`append_event` is the caller that
            wants it.
    """
    suffix = " for update" if for_update else ""
    with connection.cursor() as cur:
        cur.execute(f"{_SELECT} order by seq desc limit 1{suffix}")
        row = cur.fetchone()
    return None if row is None else _row_to_event(row)


def head_hash(connection: psycopg.Connection) -> str:
    """The chain head, or :data:`~.canonical.GENESIS_HASH` for an empty chain."""
    tail = chain_tail(connection)
    return GENESIS_HASH if tail is None else str(tail["event_hash"])


def append_event(connection: psycopg.Connection, event: Mapping[str, Any]) -> AppendResult:
    """Append one ``LedgerEvent`` to the global chain.

    Args:
        connection: a ``psycopg.Connection``. The whole append runs in one explicit
            transaction, so it works on an autocommit connection as well as a transactional
            one.
        event: ``{event_id, ts, kind, auction_id?, store_id?, order_ref?, payload}``.
            ``event_id`` becomes ``idempotency_key`` (D16 -- there is no second identifier),
            and any ``prev_hash``/``event_hash`` already on it is ignored and recomputed.

    Returns:
        An :class:`AppendResult`. ``inserted`` is ``False`` for an ``event_id`` already in
        the chain, and then ``event`` is the row that was already there.

    Raises:
        LedgerError: the event is missing ``event_id``, ``ts`` or ``kind``.
        psycopg.errors.IntegrityError: the database refused the row -- an unknown ``kind``,
            or a ``prev_hash`` that does not link to the tail. Deliberately not caught: a
            rejected link means somebody wrote outside this function.
    """
    body = canonical_event(event)
    if "ts" not in body:
        raise LedgerError("LedgerEvent is missing required field 'ts'")

    with connection.transaction():
        with connection.cursor() as cur:
            # (2) the lock. Advisory first: it is the only one that exists when the table is
            # empty. Transaction-scoped, so it is released by the commit below and never
            # leaks into the caller's next statement.
            cur.execute("select pg_advisory_xact_lock(%s)", (CHAIN_LOCK_KEY,))

            # (3) idempotency. Checked under the lock and *before* the insert, so a
            # re-delivered event never reaches the chain guard with a prev_hash computed
            # against a tail it is not actually behind.
            cur.execute(f"{_SELECT} where idempotency_key = %s", (body["event_id"],))
            existing = cur.fetchone()
            if existing is not None:
                return AppendResult(
                    event=_row_to_event(existing),
                    inserted=False,
                    head_hash=head_hash(connection),
                )

            tail = chain_tail(connection, for_update=True)
            prev = GENESIS_HASH if tail is None else str(tail["event_hash"])
            digest = compute_event_hash(prev, body)

            from psycopg.types.json import Jsonb

            cur.execute(
                "insert into ledger.commerce_events "
                "  (idempotency_key, kind, auction_id, store_id, order_ref, occurred_at, "
                "   payload, prev_hash, event_hash) "
                "values (%s, %s, %s, %s, %s, %s::timestamptz, %s, %s, %s) "
                f"returning {', '.join(_COLUMNS)}",
                (
                    body["event_id"],
                    body["kind"],
                    body.get("auction_id"),
                    body.get("store_id"),
                    body.get("order_ref"),
                    body["ts"],
                    Jsonb(body.get("payload", {})),
                    prev,
                    digest,
                ),
            )
            row = cur.fetchone()
            if row is None:  # pragma: no cover - RETURNING always yields on a plain INSERT
                raise LedgerError("insert into ledger.commerce_events returned no row")
            stored = _row_to_event(row)

            # The stored row must hash to the digest just written. It does for every value
            # `jsonb` round-trips unchanged, which is every payload this system carries --
            # but `jsonb` normalises numbers through `numeric`, so a payload holding, say,
            # 1e100 comes back as a 101-digit integer and canonicalises differently. That
            # would corrupt the chain silently and surface much later as a verification
            # failure with no tampering anywhere, so it is checked here, at the only moment
            # where the cause is still visible, and the transaction is rolled back.
            if compute_event_hash(prev, stored) != digest:
                raise LedgerError(
                    f"event {body['event_id']!r} does not survive a jsonb round trip: the "
                    f"stored row canonicalises differently from the value that was hashed. "
                    f"Its payload holds a value jsonb renormalises (a float outside the "
                    f"range jsonb reproduces exactly, most likely). Store it as a string."
                )
    return AppendResult(event=stored, inserted=True, head_hash=str(stored["event_hash"]))


def append_events(
    connection: psycopg.Connection, events: Sequence[Mapping[str, Any]]
) -> list[AppendResult]:
    """Append a sequence, one transaction each, in order."""
    return [append_event(connection, event) for event in events]


def read_events(
    connection: psycopg.Connection,
    *,
    after_seq: int = 0,
    store_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read the chain back in insertion order.

    Args:
        connection: any connection with ``SELECT`` on ``ledger.commerce_events``.
        after_seq: return only events after this sequence number (0 reads from genesis).
        store_id: restrict to one store. **A filtered read is not a chain** -- the links
            skip the events that were filtered out -- so pass this only for projection, and
            never to :func:`~.chain.verify_chain`.
        limit: cap the number of rows.

    Returns:
        ``LedgerEvent`` dicts carrying their chain fields, oldest first.
    """
    clauses = ["seq > %s"]
    params: list[Any] = [after_seq]
    if store_id is not None:
        clauses.append("store_id = %s")
        params.append(store_id)
    sql = f"{_SELECT} where {' and '.join(clauses)} order by seq"
    if limit is not None:
        sql += " limit %s"
        params.append(limit)
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return [_row_to_event(row) for row in cur.fetchall()]


def verify_chain_in_db(connection: psycopg.Connection) -> dict[str, Any]:
    """Read the whole chain and verify it. See :func:`~.chain.verify_chain` for the shape.

    ``broken_at`` is the index into the stream, which for an untruncated chain read from
    genesis is ``seq - 1``.
    """
    return verify_chain(read_events(connection))


def db_stream_hash(connection: psycopg.Connection) -> str:
    """The stored chain's stream hash -- the value a replay must reproduce."""
    return stream_hash(read_events(connection))
