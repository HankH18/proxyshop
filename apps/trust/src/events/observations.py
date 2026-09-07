"""The door the exchange reads, wired to the door producers write to.

``POST /events`` wrote ``ledger.commerce_events`` and NOTHING else. ``GET /snapshot`` --
which is what ``exchange.composition.HttpTrustSnapshot`` calls over HTTP for R12 eligibility
at all three gates -- reads ``app.sellers LEFT JOIN ledger.trust_observations``. The only two
writers of that table in the whole tree were ``trust.reconcile.routes`` and
``trust.verification.persistence``. So every observation that arrives as a *ledger event*
reached the scorer through ``GET /events/replay?snapshots=true`` and reached the exchange
through nothing at all.

Measured on this tree before this module existed, over the served routes and a real database::

    POST /events {kind: feedback, store_id: store-northroast,
                  payload: {matched_pitch: false, dim: feedback_match,
                            type: mismatch_return}}                       -> 201
    select count(*) from ledger.trust_observations                        -> 0
    GET /snapshot   feedback_match                     -> {alpha 2.0, beta 2.0}, low_data true
    GET /events/replay?snapshots=true  feedback_match   -> {alpha 2.0, beta 2.7854705921154700}

That is not merely a missing row. It is R15/S3 -- *recomputing all trust scores from the
ledger reproduces the served scores exactly* -- failing in the loudest possible way: the two
sides of the assertion disagreed about whether a real buyer's complaint had happened.

The same gap swallowed ``claim_verified`` whole. Its frozen payload shape is
``(claim_ref, status, dim)``, so it names a ``dim`` and no ``type``, and the projection
required both: the exchange's ranker announces one ``claim_verified`` per counted claim on
the served auction path, and every one of them projected to zero observations and replayed to
``{}``. That half is fixed in the projection itself
(:func:`trust.ledger.replay.observations_from_events`), so the replay and this write agree
about it rather than each having an opinion.

What lands here
---------------
:func:`persist_event_observations` -- one appended event -> the rows ``GET /snapshot``
reads -- built to the shape ``trust.reconcile.routes.persist_observations`` already
established, deliberately and point for point:

* the same table and the same ``(store_id, dim, observation_type, weight, observed_at)``
  columns, because R12's "one trust system, not two" is a fact about rows or it is a claim;
* the same ``event_seq`` arbiter. ``ledger.trust_observations`` carries no unique index over
  its real columns, so ``WHERE NOT EXISTS`` is a check-then-insert with nothing to arbitrate
  the loser -- but every row written here descends from exactly one commerce event and
  ``event_seq`` names it, which makes it a real arbiter, served by
  ``trust_observations_event_seq_idx``;
* the same transaction-scoped advisory lock, because this door takes no credential and two
  concurrent appends of the same event must not both write. (They cannot: the chain's UNIQUE
  ``idempotency_key`` means only one of them inserts. The lock covers the *convergence* run
  -- a second, later ``POST`` of the same event id, and any operator re-run -- for which the
  chain's constraint says nothing.);
* the same failure direction. **A failed observation write never fails the append.** The
  event is already sealed into a hash chain by the time this runs; refusing the 201 would
  tell a producer that a durable, chained, verifiable event was rejected. What the caller
  gets instead is the truth in the body: ``observation_rows``.

Why it is on the append path here, when the delisting fold is on a read path
----------------------------------------------------------------------------
``trust.snapshot.routes.blacklist_for`` folds on READ because a fold on the append path must
not fail an append and therefore has to be silent. ``trust.reconcile.routes`` folds on its
OWN door because reconciliation is a JOIN over three deployables' events and folding when one
input arrives grades only the orders whose other inputs came first.

Neither argument reaches this projection, because it is neither a join nor a mutable
upsert: it is a **pure function of one event**, evaluated on the one occasion that event
exists for the first time. There is no "arrived too early" state to be silent about, and
re-running it is a no-op arbitrated by the database. The residual risk -- a write that fails
and is never retried -- is the same one reconciliation carries, and it has the same answer:
the event is in the append-only chain, ``GET /events/replay?snapshots=true`` still folds it,
and the row converges on the next append of that same event id.
"""

from __future__ import annotations

import contextlib
import logging
import zlib
from collections.abc import Iterator, Mapping
from typing import Any

__all__ = [
    "OBSERVATION_LOCK_KEY",
    "observation_connection",
    "persist_event_observations",
    "returned_before",
]

_log = logging.getLogger(__name__)

#: The advisory-lock key this write serialises on, derived from a stable name the house way
#: (``trust.ledger.store.CHAIN_LOCK_KEY``, ``trust.reconcile.routes.OBSERVATION_LOCK_KEY``)
#: so it cannot silently collide with the chain's lock, the migration lock, or the
#: reconciler's. A DIFFERENT key from the reconciler's on purpose: the two folds arbitrate
#: disjoint rows (different ``event_seq`` values), and sharing a key would make an operator's
#: ``POST /reconcile`` over a whole ledger block every arriving event for its duration.
#: Transaction-scoped, so it is released by the commit or the rollback and never outlives the
#: request.
OBSERVATION_LOCK_KEY = zlib.crc32(b"proxyshop.ledger.trust_observations.events") & 0x7FFFFFFF

#: One trust observation, arbitrated by the database on ``event_seq``.
#:
#: Identical in shape to ``trust.reconcile.routes._OBSERVATION_INSERT`` and for the identical
#: reason: gating on "the append said inserted" instead would make a failed relational write
#: unrepeatable, because the second run's append is a no-op. Arbitrating on the ROW makes a
#: repeat converge however often it runs.
_OBSERVATION_INSERT = """
insert into ledger.trust_observations (
    event_seq, store_id, dim, observation_type, weight, observed_at
)
select %(event_seq)s, %(store_id)s, %(dim)s, %(observation_type)s,
       %(weight)s::double precision, %(observed_at)s::timestamptz
where not exists (
    select 1 from ledger.trust_observations where event_seq = %(event_seq)s
)
"""

#: Whether the chain already recorded a return for this order BEFORE this event.
#:
#: ``seq <`` and not ``<=``: an event is never its own antecedent, and the bound is what makes
#: the answer a function of the chain's PREFIX -- the same prefix
#: ``observations_from_events`` sees when it walks the stream, which is what keeps the served
#: row and the replayed observation equal (R15/S3).
#:
#: Scoped by store as well as by order because a platform ``order_ref`` is a per-shop number;
#: ``trust.feedback.weighting.order_identity`` says the same thing about the same pair.
#: Served by ``commerce_events_kind_seq_idx`` (``(kind, seq)``), and ``refund`` is a rare kind,
#: so this never walks a ledger dominated by auction transitions.
_RETURNED_SQL = """
select 1
from ledger.commerce_events
where kind = %(kind)s
  and store_id = %(store_id)s
  and order_ref = %(order_ref)s
  and seq < %(seq)s
limit 1
"""


def _writes_commerce_events(store: Any) -> bool:
    """Whether a ``seq`` from this store names a row in ``ledger.commerce_events``.

    ``ledger.trust_observations.event_seq`` is declared
    ``bigint REFERENCES ledger.commerce_events (seq)``, so an observation is only meaningful
    when the event it descends from is actually IN that table. An
    :class:`~.store.InMemoryEventStore` numbers its own events and writes no row, so its
    ``seq`` references nothing: projecting one would either violate the foreign key or -- far
    worse -- silently bind a store's trust posture to whatever unrelated event happens to
    hold that sequence number in a database it never wrote to.

    Duck-typed on :meth:`~.pg.PostgresEventStore._resolve_dsn` rather than by ``isinstance``,
    so this module does not import psycopg to answer a question about an in-memory object,
    and so a deployment can substitute its own ledger-backed store without editing this file.
    """
    return callable(getattr(store, "_resolve_dsn", None))


@contextlib.contextmanager
def observation_connection(request: Any, store: Any) -> Iterator[Any]:
    """The connection this projection writes on, or ``None`` when there is not one to be had.

    ``None`` rather than a refusal, in three cases and for one reason: **the append has
    already happened and must not be taken back.**

    * the store does not write ``ledger.commerce_events`` (see :func:`_writes_commerce_events`);
    * no relational connection can be opened at all; or
    * this build cannot import the connection helper.

    A caller reports ``observation_rows: 0`` and the event stays in the chain, where
    ``GET /events/replay?snapshots=true`` still folds it and the next append of the same event
    id converges the row. Refusing the request instead would trade a durable, chained,
    verifiable event for a convergence step -- the wrong way round.

    An injected ``app.state.ledger_connection`` wins, exactly as it does for
    ``/claims/verifications`` and ``POST /reconcile``, so this write can be driven against a
    caller-owned transaction; ``connection_for`` hands an injected connection back untouched
    and does not close it.
    """
    injected = getattr(getattr(request, "app", None), "state", None)
    if getattr(injected, "ledger_connection", None) is None and not _writes_commerce_events(store):
        yield None
        return
    try:
        from ..claims.routes import connection_for  # noqa: PLC0415 - see the docstring
    except Exception:  # noqa: BLE001 - an unimportable helper is not a reason to 500 an append
        _log.info("the trust-observation projection has no connection helper in this build")
        yield None
        return
    with contextlib.ExitStack() as stack:
        try:
            connection = stack.enter_context(connection_for(request))
        except Exception as exc:  # noqa: BLE001 - reported in the body, never as an outage
            _log.info(
                "an appended event is folding without a relational connection (%s)",
                type(exc).__name__,
            )
            connection = None
        yield connection


def returned_before(connection: Any, event: Mapping[str, Any], seq: Any) -> bool:
    """Whether the chain recorded a return for this event's order before this event.

    R14's cross-check, asked of the one record that can answer it. ``False`` on any failure
    to ask -- no connection, no order, a database that will not answer -- because the
    consequence of a wrong ``True`` is that a real buyer's complaint is quietly discounted to
    a quarter of its weight, and the consequence of a wrong ``False`` is that a positive
    report the buyer's own return contradicts counts at full strength for one fold. Only the
    first of those silently erases evidence, so the fallback goes the other way.
    """
    from ..feedback.weighting import RETURN_KIND, order_identity  # noqa: PLC0415

    identity = order_identity(event)
    if connection is None or identity is None:
        return False
    store_id, order_ref = identity
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                _RETURNED_SQL,
                {
                    "kind": RETURN_KIND,
                    "store_id": store_id,
                    "order_ref": order_ref,
                    "seq": int(seq),
                },
            )
            return cursor.fetchone() is not None
    except Exception:  # noqa: BLE001 - see the docstring on which way the fallback goes
        with contextlib.suppress(Exception):
            connection.rollback()
        _log.warning(
            "the return cross-check for order %s could not be run; the report is weighted "
            "as uncontradicted",
            order_ref,
        )
        return False


def persist_event_observations(connection: Any, seq: Any, event: Mapping[str, Any]) -> int:
    """Write the observations one appended event carries. Returns rows written.

    Args:
        connection: an open relational connection, or ``None``. ``None`` writes nothing and
            returns 0 -- see :func:`observation_connection`.
        seq: the chain sequence the event landed at. It becomes ``event_seq``, which is both
            the provenance of the row and the arbiter of its idempotency.
        event: the event **as the chain stored it**, not as it arrived. ``normalise_event``
            is what turns a caller's RFC-3339 spelling into the instant the scorer decays
            against, and ``observed_at`` falls back to ``ts``, so folding the raw body would
            write a different instant into the row than the replay reads out of the ledger.

    Returns:
        The number of rows written: 0 or 1 for the events this chain can produce (the
        projection yields at most one observation per event), and 0 on any failure.

    An event that projects to no observation writes nothing and opens no transaction, which
    is the common case by a wide margin -- ``bid_placed``, ``shown``, ``accepted``,
    ``order_paid`` and every auction transition carry no ``dim``.
    """
    if connection is None:
        return 0
    try:
        from ..ledger import observations_from_events  # noqa: PLC0415 - lazy, as elsewhere
    except ImportError:  # pragma: no cover - the projection ships with this package
        return 0

    returned: tuple[tuple[str, str], ...] = ()
    from ..feedback.weighting import FEEDBACK_KIND, order_identity  # noqa: PLC0415

    if str(event.get("kind") or "") == FEEDBACK_KIND and returned_before(connection, event, seq):
        identity = order_identity(event)
        returned = (identity,) if identity is not None else ()

    observations = observations_from_events([event], returned_orders=returned)
    if not observations:
        return 0

    written = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_xact_lock(%s)", (OBSERVATION_LOCK_KEY,))
            for observation in observations:
                cursor.execute(
                    _OBSERVATION_INSERT,
                    {
                        "event_seq": int(seq),
                        "store_id": observation.get("store_id"),
                        "dim": observation.get("dim"),
                        "observation_type": observation.get("type"),
                        # Absent means exactly 1.0 to the scorer, and the projection attaches
                        # a weight only when R14 discounted the report — so a NULL column and
                        # an omitted key say the same thing, which is what keeps the served
                        # row and the replayed observation equal.
                        "weight": observation.get("weight"),
                        "observed_at": observation.get("observed_at") or event.get("ts"),
                    },
                )
                written += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        connection.commit()
    except Exception:  # noqa: BLE001 - convergence is best effort; the chain already holds it
        with contextlib.suppress(Exception):
            connection.rollback()
        _log.warning(
            "the observation for event %s could not be written to ledger.trust_observations; "
            "it is in the chain and the next append of that event id converges the row",
            str(event.get("event_id", "unknown")),
        )
        return 0
    return written
