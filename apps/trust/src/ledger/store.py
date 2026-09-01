"""The Postgres side of the hash chain: append, read back, verify. Owned by T-011.

D16 in three obligations, and this module is where each of them is discharged:

1. **One global chain, ordered by insertion sequence.** ``ledger.commerce_events.seq`` is
   that order. Reads come back ``ORDER BY seq``, never by timestamp -- ``ts`` is the
   business instant and two events can legitimately share one.
2. **The writer takes a lock on the chain tail.** :func:`append_event` takes a transaction-
   scoped advisory lock keyed on the chain, and reads the tail under it. It deliberately
   does **not** take a row lock as well: ``SELECT ... FOR UPDATE`` is gated on the UPDATE
   privilege, which the append-only ``app`` role does not have and must not be given, so
   the row lock made the ledger's own appender unable to append. The advisory lock is also
   the only one that works on an empty table, where there is no tail row to lock and two
   writers would otherwise both compute ``prev_hash = GENESIS``.
   ``commerce_events_prev_hash_key`` is the backstop under both: the database refuses a
   fork whether or not anybody took a lock, so the lock is what turns a crash into a wait.
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

from .canonical import (
    GENESIS_HASH,
    CanonicalisationError,
    canonical_event,
    canonical_json,
    compute_event_hash,
    rfc3339_ms,
)
from .chain import stream_hash, verify_chain
from .errors import LedgerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg

__all__ = [
    "CHAIN_LOCK_KEY",
    "AppendResult",
    "append_event",
    "append_events",
    "chain_anchor",
    "chain_tail",
    "db_stream_hash",
    "head_hash",
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


def append_event(
    connection: psycopg.Connection,
    event: Mapping[str, Any],
    *,
    join_open_transaction: bool = False,
) -> AppendResult:
    """Append one ``LedgerEvent`` to the global chain.

    Args:
        connection: a ``psycopg.Connection``. The append runs in its own transaction, so an
            autocommit connection is the ordinary case.
        event: ``{event_id, ts, kind, auction_id?, store_id?, order_ref?, payload}``.
            ``event_id`` becomes ``idempotency_key`` (D16 -- there is no second identifier),
            and any ``prev_hash``/``event_hash`` already on it is ignored and recomputed.
        join_open_transaction: acknowledge that the connection already has a transaction
            open, and that the chain lock will therefore be held until **the caller**
            commits. See below.

    Returns:
        An :class:`AppendResult`. ``inserted`` is ``False`` for an ``event_id`` already in
        the chain, and then ``event`` is the row that was already there.

    Raises:
        LedgerError: the event is missing a required field; an ``event_id`` already in the
            chain arrived carrying *different* content; the stored row does not hash to the
            digest written; or the connection has a transaction open and
            ``join_open_transaction`` was not passed.
        psycopg.errors.IntegrityError: the database refused the row -- an unknown ``kind``,
            or a ``prev_hash`` that does not link to the tail. Deliberately not caught: a
            rejected link means somebody wrote outside this function.

    **Isolation level.** This is correct at ``READ COMMITTED`` (Postgres's default), where
    the tail read after taking the lock sees the winner's committed row. Under
    ``REPEATABLE READ`` or ``SERIALIZABLE`` the snapshot is pinned before the lock is taken,
    so a concurrent append is invisible, the tail read is stale, and only
    ``commerce_events_prev_hash_key`` catches it -- as a ``UniqueViolation`` that
    :func:`~.errors.is_transient_datastore_error` classifies as NOT retryable, because in
    every other context an integrity violation is a bug rather than a race. Append at
    ``READ COMMITTED``.

    **Why the open-transaction guard exists.** ``connection.transaction()`` opens a
    SAVEPOINT, not a top-level transaction, when one is already in progress. The chain lock
    below is ``pg_advisory_xact_lock`` -- scoped to the *transaction*, not the savepoint --
    so on a pooled non-autocommit connection the first append would hold the single global
    chain lock for the entire life of the caller's transaction, and every other writer in
    the system would block behind it. That is sometimes exactly what you want (the append
    is then atomic with the caller's other work) and sometimes a system-wide stall, and the
    difference is a decision the caller has to make rather than one to discover in
    production. So it is opt-in and named.
    """
    body = canonical_event(event)
    if "ts" not in body:
        raise LedgerError("LedgerEvent is missing required field 'ts'")

    import psycopg as _psycopg

    if (
        connection.info.transaction_status != _psycopg.pq.TransactionStatus.IDLE
        and not join_open_transaction
    ):
        raise LedgerError(
            "append_event was handed a connection with a transaction already open. "
            "`connection.transaction()` would open a SAVEPOINT inside it, while the chain's "
            "advisory lock is transaction-scoped -- so the single global chain lock would be "
            "held until YOUR commit, blocking every other ledger writer for that whole span. "
            "Pass an autocommit connection, or pass join_open_transaction=True if appending "
            "atomically with the rest of your transaction is what you actually want."
        )

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
                stored_body = canonical_event(_row_to_event(existing))
                if canonical_json(stored_body) != canonical_json(body):
                    # Same id, different content. Returning `inserted=False` here would be a
                    # success-shaped result for a write that was silently dropped -- the
                    # worst possible failure mode for an append-only ledger, because the
                    # caller has no way to notice.
                    raise LedgerError(
                        f"event_id {body['event_id']!r} is already in the chain with "
                        f"DIFFERENT content, so this append would be silently lost. D16 "
                        f"makes event_id the idempotency key, which means re-sending an id "
                        f"must re-send the same event.\n"
                        f"  stored:   {canonical_json(stored_body)[:300]}\n"
                        f"  incoming: {canonical_json(body)[:300]}"
                    )
                return AppendResult(
                    event=_row_to_event(existing),
                    inserted=False,
                    head_hash=head_hash(connection),
                )

            # The tail is read WITHOUT `FOR UPDATE`. A row lock here looked like belt and
            # braces and was actually a privilege bug: `SELECT ... FOR UPDATE` is gated on
            # the UPDATE privilege, and `app` -- the role 0004 gives SELECT+INSERT on the
            # ledger precisely so it can append and nothing else -- holds no UPDATE. So the
            # one role documented as the ledger's appender could not append, and the denial
            # arrived as a bare `permission denied for table commerce_events` that named
            # neither FOR UPDATE nor the privilege it wanted.
            #
            # Nothing is lost. The advisory lock above already serialises writers, and it
            # covers the case a row lock cannot: an empty table has no tail row to lock.
            # `commerce_events_prev_hash_key` is the backstop underneath both -- a second
            # writer that somehow computed the same `prev_hash` is refused by the database,
            # not merely by the convention that everyone took the lock.
            tail = chain_tail(connection)
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
            try:
                restored = compute_event_hash(prev, stored)
            except CanonicalisationError as exc:
                raise LedgerError(
                    f"event {body['event_id']!r} does not survive a jsonb round trip: the "
                    f"stored row cannot be canonicalised at all ({exc}). jsonb renders "
                    f"numbers through `numeric`, so a float such as 1e23 comes back as a "
                    f"24-digit integer outside the IEEE-754 safe range. Store it as a string."
                ) from exc
            if restored != digest:
                raise LedgerError(
                    f"event {body['event_id']!r} does not survive a jsonb round trip: the "
                    f"stored row canonicalises differently from the value that was hashed. "
                    f"Its payload holds a value jsonb renormalises. Store it as a string."
                )
    return AppendResult(event=stored, inserted=True, head_hash=str(stored["event_hash"]))


def append_events(
    connection: psycopg.Connection, events: Sequence[Mapping[str, Any]]
) -> list[AppendResult]:
    """Append a sequence, in order, **one transaction each**.

    Deliberately not atomic across the batch: a failure part-way leaves the events before it
    committed. That is the right shape for an append-only ledger -- the events that happened
    happened -- but it means a caller who needs all-or-nothing must open its own transaction
    and pass ``join_open_transaction=True`` to :func:`append_event`, accepting that the chain
    lock is then held for that whole span.
    """
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


def chain_anchor(connection: psycopg.Connection) -> dict[str, Any]:
    """The stored commitment: ``{head_hash, length, last_seq, updated_at}``.

    Maintained by an ``AFTER INSERT`` trigger on every row that lands, so it is accurate
    even for a writer that never called this module.
    """
    with connection.cursor() as cur:
        cur.execute(
            "select head_hash, length, last_seq, updated_at from ledger.chain_head "
            "where chain = 'commerce_events'"
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover - the migration seeds the row
        raise LedgerError("ledger.chain_head holds no row for the commerce_events chain")
    return {"head_hash": row[0], "length": row[1], "last_seq": row[2], "updated_at": row[3]}


def verify_chain_in_db(connection: psycopg.Connection) -> dict[str, Any]:
    """Verify the stored chain against its own links **and** against the stored anchor.

    :func:`~.chain.verify_chain` alone cannot detect truncation from the END of the stream:
    delete the last forty of a hundred events and the remaining sixty verify perfectly,
    because a truncated chain's digest is a perfectly valid chain digest and there is
    nothing to check its length against. ``ledger.chain_head`` is that something.

    Returns:
        The :func:`~.chain.verify_chain` result plus ``anchor`` (the stored commitment),
        ``recomputed`` (:func:`~.chain.stream_hash` over the rows) and ``anchor_ok``. ``ok``
        is ``True`` only when the links verify **and** the recomputed head and the row count
        both match the anchor -- so ``reason`` may be ``"truncated"`` for a stream that is
        internally flawless.
    """
    events = read_events(connection)
    result = verify_chain(events)
    anchor = chain_anchor(connection)
    recomputed = stream_hash(events)
    anchor_ok = bool(
        recomputed == str(anchor["head_hash"]) and len(events) == int(anchor["length"])
    )
    result["anchor"] = anchor
    result["recomputed"] = recomputed
    result["anchor_ok"] = anchor_ok
    if result["ok"] and not anchor_ok:
        result["ok"] = False
        result["reason"] = "truncated"
        result["broken_at"] = len(events)
    return result


def db_stream_hash(connection: psycopg.Connection) -> str:
    """The stored chain's stream hash, **recomputed** from the rows (never read off one)."""
    return stream_hash(read_events(connection))
