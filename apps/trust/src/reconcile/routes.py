"""``/reconcile`` — where a completed purchase becomes a trust update (S1 links 7c and 9).

Mounted by the frozen entrypoint ``apps/trust/src/main.py``, which globs
``apps/trust/src/*/routes.py`` and includes the module-level :data:`router` it finds here.

===============================  =====================================================
``GET  /reconcile``              what the chain says about every purchase in it. Reads,
                                 computes, and appends nothing.
``POST /reconcile``              the same fold, landed: the ``reconciled`` verdicts and
                                 the ``offer_integrity`` observations are appended to the
                                 chain and written into ``ledger.trust_observations``.
                                 THREE promises are graded now: price, discount, and —
                                 since the promise ledger landed — delivery, which is the
                                 only transaction producer ``shipped_on_time`` has.
===============================  =====================================================

Why this file exists at all
---------------------------
:mod:`.engine` was built, graded and correct, and **nothing called it**. Measured by an
audit driving the real product over HTTP: outside ``apps/trust/src/reconcile/`` the only
invocation of ``reconcile`` in the tree was a test harness, and one served run produced
exactly 0 reconciled events and 0 trust observations. R4 says the pixel and the webhook are
reconciled with the webhook authoritative; R12 says a transaction outcome moves the same
per-dimension Betas a verification outcome moves — "one trust system, not two". Neither
happened on any served path, so both were properties of functions rather than of the product.

WHERE THE TRIGGER GOES, and why it is not the append path
----------------------------------------------------------
The sibling decision to read against is ``trust.snapshot.routes.blacklist_for``: that fold
went on the READ path because a fold on the append path must not be allowed to fail an
append, so its failure mode is best-effort and therefore silent — a sealed delisting that
never became a row, fail-OPEN, with nothing to notice and nothing to retry.

The same argument reaches a different place here, because reconciliation is a **join** and a
delisting fold is a projection.

* **A fold on the arrival of one input runs at the wrong moment, not merely at a risky one.**
  The three inputs come from three deployables — the exchange's ``accepted`` and
  ``code_created``, the merchant's ``order_paid``, a browser's ``checkout_pixel`` — and
  arrive in any order. Folding when the webhook lands grades the purchases whose offer
  arrived first and *silently produces nothing* for the ones whose did not. That is a
  structural fail-open no error handling on the append path can close.
* **And it must not fail the append.** ``order_paid`` is R4's authority. Losing the
  authoritative record of a real purchase because a trust fold raised is strictly worse than
  a late trust update, so an append-path fold would have to swallow its own failures — the
  silent direction again.
* **A fold on the ``GET /snapshot`` read path is not available either**, and here the
  blacklist's reasoning inverts. That read is the exchange's per-auction eligibility read;
  ``persist_folded_listings`` writes an upsert to a small mutable table, whereas this fold
  APPENDS to a hash-chained, append-only ledger behind an advisory lock. Putting a chain
  append on the hot path of every auction serialises eligibility behind the ledger's writer.

So the fold is **its own door, idempotent, and re-runnable over the whole chain**. Every
``event_id`` it mints is derived from the order (``reconciled:{store}:{order}``,
``offer_integrity:{store}:{order}:{field}``) and ``event_id`` IS the ledger's idempotency
key, so a second run appends nothing and moves no Beta; the observation insert is arbitrated
by the database on ``event_seq``. That is what makes a failed run *recoverable by repeating
it* rather than permanent — the property the blacklist fold could not have on the append
path, and the reason "run it again" is a real answer here and was not one there.

What "reaching the trust score" means, concretely
-------------------------------------------------
Two writes, because the platform has two doors onto a trust score and they read different
tables:

* the ``offer_integrity`` events go into ``ledger.commerce_events``, which is what
  ``GET /events/replay?snapshots=true`` folds (``observations_from_events`` projects any
  event whose payload names a ``dim`` and a ``type``); and
* one row per observation goes into ``ledger.trust_observations``, which is the table
  ``GET /snapshot`` reads — the door ``exchange.composition.HttpTrustSnapshot`` actually
  calls over HTTP for R12 eligibility.

``ledger.trust_observations`` is also where ``trust.verification.persistence`` writes the
*verification* outcomes. Writing reconciliation's outcomes into that same table with the same
``(store_id, dim, observation_type, observed_at)`` shape is R12's "one trust system, not two"
taken literally rather than asserted. The column ``event_seq`` — declared
``bigint REFERENCES ledger.commerce_events (seq)`` with its own index in
``db/migrations/0002`` and, until this module, written by nothing anywhere in the tree — is
the schema's own reserved home for an observation whose source is a commerce event.

**A failed observation write never fails the fold**, for the reason
:func:`~..snapshot.routes.persist_folded_listings` gives: the verdicts are already appended
to the chain, so the trust update is durable and the relational write is a convergence step.
The response says which of the two happened (``observations_persisted``), so a deployment
cannot mistake one for the other, and re-running converges.

The store alias, and why the join needs one
--------------------------------------------
The two halves of one checkout do not name the store the same way, and that is a second join
blocker beside the two checkout tokens. The exchange stamps its platform ``store_id``
(``store-northroast``); ``merchant_svc.composition._store_id`` writes the shop domain
(``…myshopify.com``), because an unsigned ``X-Shopify-Shop-Domain`` header is the only shop
identity a signed delivery carries at all. ``reconcile`` namespaces every join key by store —
it has to, a Shopify ``order_id`` is a per-shop number — so an offer under one name and an
order under the other can never meet, however good the discount-code bridge is.

:func:`resolve_store_aliases` reads the platform's own roster (``app.sellers``) and maps a
seller's ``domain`` onto its ``store_id``. It only ever translates a name the platform does
NOT know into one it does, it refuses a domain two sellers claim (``app.sellers.domain`` has
no unique index), and it never invents a store: an unrecognised name is left exactly as it
arrived, which reconciles to nothing rather than to somebody else's promise.

Nothing on this router is authenticated
---------------------------------------
There is not one ``Depends`` in ``apps/trust``. Three bounds follow, and all three are
load-bearing:

* :data:`MAX_RECONCILE_INPUT_EVENTS`. The join is a union-find that holds every checkout
  event at once, so its cost is proportional to the ledger and not to a page — the same
  shape ``replay?snapshots=true`` carries ``MAX_SNAPSHOT_REPLAY_EVENTS`` for, and lower,
  because a union-find over one page costs more than a projection over it.
* :func:`~.engine.unjoinable_webhook`, asked BEFORE the fold. ``POST /events`` admits an
  ``order_paid`` naming no order (it screens identifiers for length and renderability, not
  for joinability) and the append-only trigger makes the row un-evictable, so without this
  screen one unauthenticated append would 500 this door forever and stop every later purchase
  in that ledger from grading. The refusal is kept — as ``unjoinable_webhooks`` in the served
  body — rather than converted into an outage or into a silent drop.
* :data:`ANNOUNCE_BUDGET_SECONDS`, which is the one that was missing. The two above bound what
  the fold HOLDS and what it will READ; neither bounds how long ``POST /reconcile`` spends
  telling store agents and the exchange what it decided, and that is a synchronous per-event
  cost against peers this service does not control. Measured before the budget existed: ten
  orders, twenty fresh observations, two peers that accept a connection and never answer,
  **20.4s on one unauthenticated request** — and the observation count is bounded only by the
  first bullet, so ~33k observations and hours of hold were reachable the same way.
"""

from __future__ import annotations

import contextlib
import logging
import time
import zlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ..events.errors import EventServiceError, IdempotencyConflict, StoreUnavailable
from ..events.routes import store_for
from ..events.store import append
from .engine import (
    ACCEPTED_KIND,
    CODE_BRIDGE_KINDS,
    FULFILLED_KIND,
    OBSERVATION_KIND,
    PIXEL_KIND,
    RECONCILED_KIND,
    WEBHOOK_KIND,
    ReconciliationInputError,
    observation_events,
    reconcile,
    unjoinable_webhook,
)

__all__ = [
    "ANNOUNCE_BUDGET_SECONDS",
    "DEFAULT_VERDICT_PAGE",
    "INPUT_KINDS",
    "MAX_RECONCILE_INPUT_EVENTS",
    "MAX_VERDICT_PAGE",
    "OBSERVATION_LOCK_KEY",
    "ReconcileInputs",
    "persist_observations",
    "read_checkout_events",
    "resolve_store_aliases",
    "router",
    "store_aliases_for",
]

_log = logging.getLogger(__name__)

router = APIRouter(tags=["reconcile"])

#: The kinds the fold reads. Everything else in the chain — auction transitions, claim
#: verifications, delistings — is walked past, so this door can be handed a whole ledger.
INPUT_KINDS: tuple[str, ...] = (
    ACCEPTED_KIND,
    PIXEL_KIND,
    WEBHOOK_KIND,
    FULFILLED_KIND,
    *CODE_BRIDGE_KINDS,
)

#: The most checkout events one fold will hold at once before refusing.
#:
#: ``trust.events.routes.MAX_SNAPSHOT_REPLAY_EVENTS`` bounds the one other whole-chain fold
#: this service serves, for exactly this reason: a projection that holds an observation per
#: event cannot be paged. This fold is strictly heavier — a union-find over every checkout
#: event, plus the events themselves retained for the second pass — so the ceiling is lower.
#: Only the SIX kinds above count towards it, so a ledger dominated by auction transitions
#: is not refused for events this fold never looks at.
MAX_RECONCILE_INPUT_EVENTS = 50_000

#: How many verdicts ``GET /reconcile`` serialises when the caller does not say. A default is
#: the only cap that binds on an unauthenticated door: a ``Query(None, le=…)`` validator runs
#: only on a value that was supplied, so it constrains exactly the callers who were already
#: being polite. Same lesson as ``events.routes.DEFAULT_EVENT_PAGE``.
DEFAULT_VERDICT_PAGE = 200

#: The most one ``GET /reconcile`` will serialise, even explicitly.
MAX_VERDICT_PAGE = 2_000

#: How long ``POST /reconcile`` will spend TELLING the world what it decided, in total.
#:
#: The third bound on this unauthenticated door, and the one that was missing.
#: :data:`MAX_RECONCILE_INPUT_EVENTS` bounds what the fold HOLDS and
#: :data:`DEFAULT_VERDICT_PAGE` bounds what it SERIALISES; neither bounds how long it holds a
#: worker open pushing notifications, and that turned out to be the largest of the three.
#:
#: The arithmetic, measured rather than assumed. Each fresh observation goes through
#: ``trust.feedback.announce_trust_event``, whose fanout makes TWO inline HTTP attempts — the
#: store's own agent, then the exchange's bandit — each bounded by
#: ``notify.DEFAULT_PUSH_TIMEOUT_SECONDS`` (0.5s). So one observation costs up to 1.0s against
#: peers that accept a connection and then say nothing, and the count is bounded only by
#: :data:`MAX_RECONCILE_INPUT_EVENTS`: 50_000 checkout events is ~16k orders and ~33k
#: observations. Ten orders against two silent peers measured **20.4s on one request**, and
#: nothing in the door stopped that scaling to hours. It takes no credential.
#:
#: Five seconds, because the number has to be argued from what the announce is FOR rather than
#: picked. It is a courtesy on top of an answer the caller has already earned: the verdicts are
#: in the append-only chain and the rows are in ``ledger.trust_observations`` before this phase
#: starts, so spending the caller's request on it is only ever worth a slice. Against healthy
#: peers a push is single-digit milliseconds, so five seconds covers on the order of a thousand
#: observations and the budget is invisible — it binds exactly in the case it exists for, an
#: outage, where it caps the hold at five seconds instead of at the length of the ledger.
#:
#: Checked BEFORE each announce, never during one, so the true worst-case hold is this plus one
#: full fanout (~1.0s). Interrupting an attempt mid-flight would mean a store agent that has
#: received a delta and a fold that believes it has not.
ANNOUNCE_BUDGET_SECONDS = 5.0

#: The platform roster, read for the store alias. Two columns and no filter: the map has to
#: be complete to be able to REFUSE a domain two sellers claim, and a query that returned one
#: row per domain would hide exactly the ambiguity :func:`resolve_store_aliases` exists to
#: catch.
_SELLERS_SQL = "select store_id, domain from app.sellers"

#: The advisory-lock key the observation write serialises on. Derived from a stable name the
#: house way (``trust.ledger.store.CHAIN_LOCK_KEY``), so it cannot silently collide with the
#: chain's own lock or with the migration lock.
#:
#: It is not decoration. ``WHERE NOT EXISTS`` is a check-then-insert and
#: ``ledger.trust_observations`` has no unique index over its real columns to arbitrate the
#: loser, so two concurrent folds could both read "absent" for one ``event_seq`` and both
#: write — double-counting one purchase against a store's Beta, on a money path, through a
#: door that takes no credential. The chain appends cannot double (they are serialised by
#: ``CHAIN_LOCK_KEY`` and backed by a UNIQUE constraint); without this, the rows derived from
#: them could. Transaction-scoped, so it is released by the commit or the rollback below and
#: never outlives the request.
OBSERVATION_LOCK_KEY = zlib.crc32(b"proxyshop.ledger.trust_observations.reconcile") & 0x7FFFFFFF

#: One trust observation, arbitrated by the database rather than by this module.
#:
#: ``ledger.trust_observations`` has no unique index over its real columns —
#: ``trust.verification.persistence`` says so and defends itself by never emitting the second
#: INSERT — but every observation this module writes descends from exactly one
#: ``offer_integrity`` event, and ``event_seq`` names that event. So ``WHERE NOT EXISTS`` on
#: ``event_seq`` IS a real arbiter here, served by ``trust_observations_event_seq_idx``.
#:
#: That matters more than tidiness: gating the insert on "the append said inserted" instead
#: would make a failed relational write unrepeatable, because the second run's append is a
#: no-op. Arbitrating on the row makes ``POST /reconcile`` converge however often it runs.
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


@dataclass(frozen=True)
class ReconcileInputs:
    """What one read of the chain found, and what it had to set aside."""

    events: list[Any] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    unjoinable_webhooks: int = 0
    aliased: int = 0
    scanned: int = 0


# =====================================================================================
# The store alias
# =====================================================================================
def _row_pair(row: Any) -> tuple[Any, Any]:
    """``(store_id, domain)`` whether the driver hands back tuples or mappings."""
    if isinstance(row, Mapping):
        return row.get("store_id"), row.get("domain")
    return row[0], row[1]


def resolve_store_aliases(rows: Iterable[Any]) -> dict[str, str]:
    """``domain -> store_id`` for the sellers whose domain names exactly one of them.

    Three refusals, and each one is a wrong grading it prevents rather than a nicety:

    * **a domain two sellers claim maps to neither.** ``app.sellers.domain`` carries no
      unique index, so this is a state the schema permits; adopting one of the two would file
      a real order against a store that did not take it.
    * **a domain that is also some seller's own ``store_id`` maps to nothing.** The map only
      ever translates a name the platform does not know into one it does, and rewriting a
      name the roster already holds would merge two real sellers into one trust subject.
    * **an empty or non-string value is not a name.** A blank domain would otherwise become a
      key every unattributed event matched.

    Compared case-insensitively on the domain, because a shop domain is a hostname and the
    merchant's own ``normalize_shop_domain`` already lower-cases what it writes.
    """
    store_ids: set[str] = set()
    claims: dict[str, set[str]] = {}
    for row in rows:
        raw_store, raw_domain = _row_pair(row)
        store = str(raw_store).strip() if isinstance(raw_store, str) else ""
        domain = str(raw_domain).strip().lower() if isinstance(raw_domain, str) else ""
        if not store:
            continue
        store_ids.add(store)
        if domain:
            claims.setdefault(domain, set()).add(store)
    return {
        domain: next(iter(owners))
        for domain, owners in claims.items()
        if len(owners) == 1 and domain not in store_ids
    }


def store_aliases_for(request: Request, connection: Any = None) -> dict[str, str]:
    """The ``domain -> store_id`` map this fold resolves store names through.

    Resolution order: ``app.state.reconcile_store_aliases`` (a mapping, or a callable
    returning one), otherwise ``app.sellers`` on the connection this request already holds,
    otherwise nothing.

    "Otherwise nothing" is the honest answer rather than a shortcut. With no roster there is
    no authority saying which platform store a shop domain is, and guessing would attribute a
    real purchase to a store on the strength of a header. The consequence is visible: the
    served body reports ``store_aliases_applied: 0`` and the reconciled count with it.
    """
    injected = getattr(request.app.state, "reconcile_store_aliases", None)
    if injected is not None:
        source = injected() if callable(injected) else injected
        return {str(key).strip().lower(): str(value) for key, value in dict(source).items()}
    if connection is None:
        return {}
    try:
        with connection.cursor() as cursor:
            cursor.execute(_SELLERS_SQL)
            return resolve_store_aliases(cursor.fetchall())
    except Exception:  # noqa: BLE001 - an unreadable roster is "no alias", never a 500
        _log.warning("the seller roster could not be read; no store alias was applied")
        return {}


def _aliased(event: Any, aliases: Mapping[str, str]) -> tuple[Any, bool]:
    """``event`` with its ``store_id`` translated into the platform's name for that store."""
    if not aliases or not isinstance(event, Mapping):
        return event, False
    raw = event.get("store_id")
    if not isinstance(raw, str):
        return event, False
    resolved = aliases.get(raw.strip().lower())
    if resolved is None or resolved == raw:
        return event, False
    return {**event, "store_id": resolved}, True


# =====================================================================================
# Reading the chain
# =====================================================================================
def read_checkout_events(
    store: Any,
    aliases: Mapping[str, str],
    *,
    limit: int = MAX_RECONCILE_INPUT_EVENTS,
) -> ReconcileInputs:
    """Walk the whole chain once, keeping only what reconciliation reads.

    The walk is streamed (``iter_events``, which the Postgres store pages through
    ``LEDGER_SCAN_CHUNK``), so what is held is proportional to the CHECKOUT events rather
    than to the ledger — which only ever grows and is mostly auction transitions.

    Raises:
        HTTPException: 422 when more than ``limit`` checkout events are present. Refused
            rather than allocated: this door is unauthenticated and the join holds all of
            them at once.
    """
    events: list[Any] = []
    counts = dict.fromkeys(INPUT_KINDS, 0)
    unjoinable = 0
    aliased = 0
    scanned = 0
    stream = getattr(store, "iter_events", None)
    if not callable(stream):
        raise StoreUnavailable(
            f"{type(store).__name__} cannot be streamed, so the chain cannot be reconciled"
        )
    for raw in stream():
        scanned += 1
        kind = str(raw.get("kind") if isinstance(raw, Mapping) else "")
        if kind not in counts:
            continue
        counts[kind] += 1
        if unjoinable_webhook(raw):
            # Kept as a number, not raised and not dropped -- see the module docstring.
            unjoinable += 1
            continue
        event, renamed = _aliased(raw, aliases)
        aliased += int(renamed)
        events.append(event)
        if len(events) > limit:
            raise HTTPException(
                422,
                {
                    "error": "ledger_too_long_to_reconcile",
                    "message": (
                        f"reconciliation joins every checkout event in the chain and holds "
                        f"all of them at once, and this door is unauthenticated; the ceiling "
                        f"is {limit}. Reading the chain (GET /events) is paged and stays "
                        f"available at any length."
                    ),
                    "max_events": limit,
                },
            )
    return ReconcileInputs(
        events=events,
        counts=counts,
        unjoinable_webhooks=unjoinable,
        aliased=aliased,
        scanned=scanned,
    )


def _fold(inputs: ReconcileInputs) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(reconciled events, offer_integrity events)`` for one read of the chain."""
    try:
        reconciled = reconcile(inputs.events)
    except ReconciliationInputError as exc:  # pragma: no cover - the screen above prevents it
        # Reaching this means `unjoinable_webhook` and the engine disagree about what is
        # joinable, which is this service's bug and not the caller's -- so a 500 that names
        # it, never a 4xx that blames the request.
        raise HTTPException(
            500,
            {
                "error": "unreconcilable_ledger",
                "message": (
                    f"an event in the chain was refused by the reconciler after this door's "
                    f"own screen admitted it: {exc}"
                ),
            },
        ) from exc
    return reconciled, observation_events(reconciled)


# =====================================================================================
# Landing it
# =====================================================================================
def _refuse(exc: EventServiceError) -> HTTPException:
    """A ledger error raised while appending events THIS module built.

    Deliberately narrower than ``events.routes._refuse``, which maps a caller's input to a
    status. Nothing here came from a caller: a 4xx would blame the request for a body this
    service composed. So the only two answers are "the ledger is unavailable" and "we built
    something it refused", and the second one is ours.
    """
    if isinstance(exc, StoreUnavailable):
        return HTTPException(503, {"error": "store_unavailable", "message": str(exc)})
    return HTTPException(
        500,
        {
            "error": "reconciliation_not_recorded",
            "message": (
                f"the ledger refused an event reconciliation composed: {exc}. The verdicts "
                f"are not recorded; re-running this fold recomputes them."
            ),
        },
    )


@dataclass(frozen=True)
class _Appended:
    """What one batch of composed events did to the chain."""

    #: Rows this run added.
    inserted: int = 0
    #: Rows that were already there, byte-identical or superseding.
    already: int = 0
    #: Rows an EARLIER version of this fold sealed under a different body. See
    #: :func:`_append_all`: the stored verdict stands and this run's is discarded.
    superseded: int = 0
    #: Superseded rows whose STORED body could not be read back off the chain, so their
    #: relational row could not be converged on this run. Counted rather than guessed.
    unreadable: int = 0
    #: ``(seq, event)`` for everything the chain now holds under this run's composition.
    landed: tuple[tuple[int, Mapping[str, Any]], ...] = ()
    #: The subset of ``landed`` that this run INSERTED, which is what may be announced.
    fresh: tuple[tuple[int, Mapping[str, Any]], ...] = ()
    #: The refusal that stopped the batch, if one did — held rather than raised, so the caller
    #: can finish landing what this batch already sealed before it answers. ``None`` on a
    #: complete batch. See :func:`_append_all`.
    error: HTTPException | None = None


def _stored_row(store: Any, event_id: str) -> tuple[int, Mapping[str, Any]] | None:
    """``(seq, event)`` as the CHAIN holds it under ``event_id``, or ``None``.

    Used by the supersession branch below, where the event this run composed is precisely the
    one that must not be used: the chain's row is the one that stands, its ``seq`` is the value
    ``ledger.trust_observations.event_seq`` is a foreign key onto, and the body a later fold
    recomputed is the body the ledger refused. Reading it back is one indexed probe on
    ``commerce_events_idempotency_key_key`` (see ``events.pg.PostgresEventStore.get``).

    ``None`` for every way that read can fail to produce a usable row — no ``get`` on this
    store, a read that raised, a row without a ``seq``. The caller counts those; it never
    invents a number, because a guessed ``seq`` is a foreign key pointed at somebody else's
    event.
    """
    reader = getattr(store, "get", None)
    if not callable(reader):
        return None
    try:
        stored = reader(event_id)
        if not isinstance(stored, Mapping):
            return None
        return int(stored["seq"]), stored
    except Exception:  # noqa: BLE001 - an unreadable row is counted, never guessed at
        _log.warning("the chain's own copy of %s could not be read back", event_id, exc_info=True)
        return None


def _append_all(store: Any, events: Sequence[Mapping[str, Any]]) -> _Appended:
    """Append every event, idempotently. **Collects its refusal rather than raising it.**

    ``landed`` and ``fresh`` are separate lists rather than one list with a flag, because the
    two are read for opposite reasons and the difference is load-bearing.
    ``persist_observations`` wants EVERY landed event — the relational write is arbitrated on
    ``event_seq`` and re-running it converges, so an event that was already in the chain but
    whose row never made it is exactly the case that must be retried.
    :func:`_announce_observations` wants only the ones that INSERTED, because a second
    notification for one event charges a store twice the moment its agent starts counting
    deltas.

    **Why the refusal is returned and not raised.** This function is not atomic and cannot be:
    it appends one event per transaction to a hash-chained, append-only ledger. Raising from
    inside the loop kept everything it had already appended and abandoned the caller's
    remaining work, so the events it HAD sealed were never announced and never persisted —
    measured, a 503 injected on the second ``offer_integrity`` append of one order left
    ``offer_integrity:…:price`` in the chain with no store agent told, and no later run can
    repair that (the second run's append is a no-op, a no-op is not ``fresh``, and this service
    serves no door that re-announces a stored event). So the batch stops at the first refusal —
    the ledger is unavailable or refusing what this module composes, and the next event is
    overwhelmingly likely to meet the same answer — and hands the caller both what it landed
    and the exception to raise once it has finished with it. The caller's status and body are
    unchanged; only the ordering is.
    """
    inserted = 0
    already = 0
    superseded = 0
    unreadable = 0
    landed: list[tuple[int, Mapping[str, Any]]] = []
    fresh: list[tuple[int, Mapping[str, Any]]] = []
    error: HTTPException | None = None
    for event in events:
        try:
            outcome = append(store, event)
        except IdempotencyConflict as exc:
            # THE FOLD CHANGED, AND THE LEDGER IS APPEND-ONLY. `event_id` here is minted by
            # this service from the order (`reconciled:{store}:{order}`), not supplied by a
            # caller, so a conflict cannot be a producer contradicting itself: it can only mean
            # that an EARLIER VERSION of this fold already sealed a verdict for this order and
            # the body it composed then is not byte-identical to the one composed now. D16's
            # refusal is right about a caller's event and wrong about this one -- what it is
            # protecting against, "accepting this append would silently lose it", cannot
            # happen, because the chain keeps the row it already has either way.
            #
            # Left to raise, this takes the WHOLE door down permanently: `_refuse` answers 500,
            # nothing in the fold lands, `persist_observations` never runs, and every later
            # purchase on that ledger is ungraded for as long as the old row exists -- which is
            # for ever, on an append-only chain with an `ENABLE ALWAYS` trigger. Measured on
            # this tree: one pre-existing `reconciled:...` row is enough. So it is counted as
            # present, said out loud, and the fold continues with the rest.
            #
            # It is NOT swallowed. The stored verdict is the one that stands and this run's
            # recomputation of it is discarded, which is a real difference an operator has to
            # be able to see -- hence the WARNING naming the event and the `superseded` count
            # on the response.
            already += 1
            superseded += 1
            _log.warning(
                "the chain already holds %s with a different body: an earlier version of this "
                "fold sealed it, and an append-only ledger cannot restate it. The STORED "
                "verdict stands and this run's recomputation of it is discarded (%s)",
                str(event.get("event_id", "unknown")),
                exc,
            )
            # AND THE RELATIONAL WRITE STILL HAS TO CONVERGE. This branch used to `continue`
            # here, before `landed.append(...)`, so a superseded event never reached
            # `persist_observations` at all -- and because carrying the auction's `cluster_id`
            # changed the body of every `reconciled:*` and `offer_integrity:*` payload while
            # their ids stayed deterministic, EVERY order reconciled before that deploy takes
            # this branch. On any database with history that made the relational convergence
            # permanently off, which is the exact case this function's own docstring calls
            # "the one that must be retried".
            #
            # The STORED row, never the one this run composed: the chain's copy is the one that
            # stands, and `ledger.trust_observations.event_seq` is a foreign key onto it.
            # Deliberately NOT added to `fresh` -- the chain already held this observation, and
            # announcing it charges the store a second time for one purchase.
            stored = _stored_row(store, str(event.get("event_id", "")))
            if stored is None:
                unreadable += 1
                _log.warning(
                    "the chain's stored copy of %s could not be read back, so its "
                    "ledger.trust_observations row cannot converge on this run; reported as "
                    "superseded_unreadable rather than written against a guessed event_seq",
                    str(event.get("event_id", "unknown")),
                )
            else:
                landed.append(stored)
            continue
        except EventServiceError as exc:
            # Held, not raised: see the docstring. What this batch already sealed into the
            # chain still has to be persisted and announced before the caller answers.
            error = _refuse(exc)
            break
        inserted += int(outcome.inserted)
        already += int(not outcome.inserted)
        landed.append((int(outcome.seq), event))
        if outcome.inserted:
            fresh.append((int(outcome.seq), event))
    return _Appended(
        inserted=inserted,
        already=already,
        superseded=superseded,
        unreadable=unreadable,
        landed=tuple(landed),
        fresh=tuple(fresh),
        error=error,
    )


def _announce_observations(
    request: Request, store: Any, fresh: Sequence[tuple[int, Mapping[str, Any]]]
) -> dict[str, Any]:
    """Tell the affected store's agent — and the exchange — what this fold decided.

    **The gap this closes.** ``POST /events`` has notified since R13 was wired, but nothing
    reconciliation appends goes through that door: :func:`_append_all` calls
    ``trust.events.store.append`` directly, so every ``offer_integrity`` observation this
    module has ever minted was sealed into the chain and told to nobody. That is the whole
    transaction half of trust — price honoured, discount honoured, delivery kept — and it is
    the half a store's advocate most needs, because it is the half the store can actually do
    something about. R4 makes ``order_paid`` the authority on a completed purchase; this is
    the line from that authority to the two things that learn from it.

    Only the events that INSERTED, and that is not a nicety: this door is idempotent and
    designed to be re-run, so announcing on every fold would re-charge a store for one
    purchase once per run. It is the same rule ``events.routes._announce`` follows, for the
    same reason, and the reason the caller is handed a separate ``fresh`` list.

    **Never raises, and never delays the answer beyond :data:`ANNOUNCE_BUDGET_SECONDS` plus
    one attempt in flight.** The verdicts are already in the append-only chain by the time this
    runs; a store agent that is down is an operational problem with a notification, and turning
    it into a 5xx would tell the caller that a durable, chained, verifiable fold had been
    refused. Retries of an ATTEMPTED push happen off this thread — see
    ``trust.feedback.notify``.

    The budget is the bound this used to be missing, and the sentence it replaces
    ("never delays the answer beyond one bounded attempt per event") was wrong twice over: the
    fanout makes TWO inline attempts per event, and "per event" is not a bound at all when the
    number of events is bounded only by the length of the ledger. Measured: ten orders, twenty
    fresh observations, two peers that accept and never answer, **20.4s on one unauthenticated
    request**. See :data:`ANNOUNCE_BUDGET_SECONDS` for why five seconds.

    **What the caller is told about the remainder, and what an operator can do about it.** The
    events past the budget were sealed into the chain and NOT announced, and the report says so
    as ``not_attempted`` (with ``budget_seconds``, so the number is readable against the policy
    that produced it). They are not counted as pushed and not counted as failed pushes, because
    they are neither: the invariants are ``attempted + not_attempted == len(fresh)`` and
    ``pushed + not_pushed == attempted``. Those two keys appear only on a fold where the budget
    actually bit — their presence IS the signal, and on the overwhelming majority of folds
    (healthy peers, or nothing to push) the block is the three counts it has always been.

    **There is no recovery for them inside this service, and this comment is the honest
    version of that.** Re-running the fold does not re-announce: the second run's appends are
    no-ops, a no-op is not ``fresh``, and no door in ``apps/trust`` re-announces an event that
    is already stored. Nor do they reach ``trust.feedback.notify``'s retry queue or its
    readable ``undelivered`` ring — both of those are fed by an attempt that FAILED, and these
    were never attempted. So the operator's position is: the trust update is durable and
    verifiable in the ledger (``GET /events``, ``GET /reconcile``), the store's agent and the
    exchange's bandit will never hear about it, and the only real fix is a durable outbox —
    the same table ``trust.feedback.notify`` says it cannot add on its own (a migration, a
    ``trust_rw`` grant, and a claim policy so two processes cannot drain the same row). Until
    that exists the honest answer to "how do I replay these" is: you cannot. What the count is
    for is knowing it happened and how big it was, which is strictly better than the silence
    that was there before, and is not a substitute for the queue.
    """
    report: dict[str, Any] = {"attempted": len(fresh), "pushed": 0, "not_pushed": 0}
    if not fresh:
        return report
    try:
        from ..events.routes import trust_event_sink  # noqa: PLC0415 - sibling feature
        from ..feedback.notify import (  # noqa: PLC0415 - reaches the scorer; see events.routes
            announce_trust_event,
            store_history_reader,
        )
    except Exception:  # noqa: BLE001 - a build that cannot compute deltas still reconciles
        _log.warning(
            "this build cannot compute trust deltas, so %d reconciled observation(s) were "
            "sealed into the chain and no store agent or exchange was told",
            len(fresh),
            exc_info=True,
        )
        report["not_pushed"] = len(fresh)
        return report

    sink = trust_event_sink(request)
    if sink is None:
        report["not_pushed"] = len(fresh)
        return report

    # Checked before each announce and never inside one: a fanout interrupted half way would
    # leave a store agent holding a delta this fold believes it never sent.
    deadline = time.monotonic() + ANNOUNCE_BUDGET_SECONDS
    for index, (seq, event) in enumerate(fresh):
        if time.monotonic() >= deadline:
            remaining = len(fresh) - index
            report["attempted"] = index
            report["not_attempted"] = remaining
            report["budget_seconds"] = ANNOUNCE_BUDGET_SECONDS
            _log.warning(
                "the %.1fs announce budget was spent after %d of %d reconciled observation(s); "
                "the remaining %d are SEALED IN THE CHAIN AND WERE NOT ANNOUNCED. They did not "
                "reach the retry queue either -- that is fed by failed attempts, and these were "
                "not attempted -- and re-running this fold will not re-announce them, because a "
                "second append is a no-op. Recovering them needs a durable outbox this service "
                "does not have. The peers are the thing to fix: %s",
                ANNOUNCE_BUDGET_SECONDS,
                index,
                len(fresh),
                remaining,
                sorted({str(row[1].get("store_id", "unknown")) for row in fresh[index:]}),
            )
            break
        try:
            outcome = announce_trust_event(
                event,
                history_reader=store_history_reader(store, before_seq=seq),
                sink=sink,
            )
        except Exception:  # noqa: BLE001 - see the docstring
            _log.warning(
                "notifying the affected store about reconciled event %s failed; the event is "
                "in the ledger",
                str(event.get("event_id", "unknown")),
                exc_info=True,
            )
            report["not_pushed"] += 1
            continue
        if outcome.get("pushed"):
            report["pushed"] += 1
        else:
            report["not_pushed"] += 1
    return report


def persist_observations(connection: Any, landed: Sequence[tuple[int, Mapping[str, Any]]]) -> int:
    """Write the observations into ``ledger.trust_observations``. Returns rows written.

    This is the write that makes a reconciled verdict visible to ``GET /snapshot``, which is
    the door the exchange reads eligibility from — and it is the same table
    ``trust.verification.persistence`` writes a verification outcome into, which is R12's
    "one trust system, not two" as a fact about rows rather than as a claim.

    Best effort, and never the enforcement: the verdicts are already in the append-only
    chain, so a failed write here loses no decision and the next ``POST /reconcile``
    converges. On any error the transaction is rolled back and 0 is returned — the caller
    reports ``observations_persisted: false`` rather than pretending it happened.

    Serialised on :data:`OBSERVATION_LOCK_KEY` for the length of the transaction, because the
    idempotency here is a check-then-insert against a table with no unique index to arbitrate
    a race. Two concurrent folds are a thing an unauthenticated door has to survive, and the
    thing they would produce is a doubled trust observation about a real store.
    """
    if not landed:
        return 0
    written = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_xact_lock(%s)", (OBSERVATION_LOCK_KEY,))
            for seq, event in landed:
                payload = event.get("payload") or {}
                cursor.execute(
                    _OBSERVATION_INSERT,
                    {
                        "event_seq": seq,
                        "store_id": event.get("store_id"),
                        "dim": payload.get("dim"),
                        "observation_type": payload.get("type"),
                        # No weight. A reconciliation is a machine comparison against the
                        # authoritative webhook and is worth exactly its published type
                        # weight; the scorer reads an absent weight as exactly 1.0.
                        "weight": None,
                        "observed_at": payload.get("observed_at") or event.get("ts"),
                    },
                )
                written += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        connection.commit()
    except Exception:  # noqa: BLE001 - convergence is best effort; the chain already holds it
        with contextlib.suppress(Exception):
            connection.rollback()
        _log.warning("reconciled observations could not be written to ledger.trust_observations")
        return 0
    return written


@contextlib.contextmanager
def _optional_connection(request: Request) -> Iterator[Any]:
    """This request's database connection, or ``None`` when there is not one to be had.

    ``None`` rather than a 503, because everything this door does that MATTERS is done
    against the ledger the events already live in: the alias map degrades to "no alias" and
    the observation write degrades to "not converged yet", and both are reported in the body.
    Refusing the whole fold because a relational connection could not be opened would throw
    away a completed, durable reconciliation over its convergence step.

    That is not a fail-open on R12: ``GET /snapshot`` answers 503 when the same database is
    unreadable, and the exchange reads a 503 there as "deny every store".
    """
    from ..claims.routes import connection_for

    with contextlib.ExitStack() as stack:
        try:
            connection = stack.enter_context(connection_for(request))
        except Exception as exc:  # noqa: BLE001 - reported in the body, never as an outage
            _log.info(
                "reconciliation is folding without a relational connection (%s)",
                type(exc).__name__,
            )
            connection = None
        yield connection


def _report(
    inputs: ReconcileInputs, reconciled: Sequence[Any], observations: Sequence[Any]
) -> dict[str, Any]:
    """The counts every answer carries, whether or not anything was landed."""
    return {
        "ledger_events_scanned": inputs.scanned,
        "inputs": dict(inputs.counts),
        "unjoinable_webhooks": inputs.unjoinable_webhooks,
        "store_aliases_applied": inputs.aliased,
        "reconciled": len(reconciled),
        "observations": len(observations),
    }


@router.get(
    "/reconcile",
    operation_id="readReconciliation",
    response_model=None,
    summary="What the chain says about every purchase in it. Appends nothing.",
)
def get_reconcile(
    request: Request,
    limit: int = Query(
        DEFAULT_VERDICT_PAGE,
        ge=1,
        le=MAX_VERDICT_PAGE,
        description="How many reconciled verdicts to serialise. The fold is never capped.",
    ),
) -> dict[str, Any]:
    """The diagnosis door: fold the chain and answer, without writing to it.

    An operator asking "did this purchase grade, and how?" must not have to append to an
    append-only ledger to find out — and a caller that wants the verdicts *recorded* has
    ``POST`` for that. The counts are computed over the whole chain; ``limit`` bounds only
    what is serialised, and ``verdicts_truncated`` says whether it bit.
    """
    store = store_for(request)
    with _optional_connection(request) as connection:
        aliases = store_aliases_for(request, connection)
        try:
            inputs = read_checkout_events(store, aliases)
        except EventServiceError as exc:
            raise _refuse(exc) from exc
    reconciled, observations = _fold(inputs)
    return {
        **_report(inputs, reconciled, observations),
        "events": list(reconciled[:limit]),
        "observation_events": list(observations[: limit * 2]),
        "verdicts_truncated": len(reconciled) > limit,
    }


@router.post(
    "/reconcile",
    operation_id="reconcileLedger",
    response_model=None,
    summary="Reconcile the chain and land the trust update it implies.",
)
def post_reconcile(request: Request) -> dict[str, Any]:
    """Fold the chain, append what it decided, and write the observations the scorer reads.

    Idempotent by construction — see the module docstring — so this is safe to run on a
    schedule, after a delivery, or twice by mistake. The answer distinguishes what was
    *appended* from what was *already present*, so "nothing landed" and "nothing to land"
    are never the same number.

    **The refusals are raised at the END, and that ordering is the fix rather than a style.**
    :func:`_append_all` is not atomic across a batch, so a ledger error part-way through leaves
    events already sealed into the chain. Raising where it happened meant those events were
    never persisted and never announced, permanently — see that function's docstring. So the
    error is carried back, the fold finishes converging and announcing what it DID land, and
    only then does the caller get the same status and the same body it always got.
    """
    store = store_for(request)
    with _optional_connection(request) as connection:
        aliases = store_aliases_for(request, connection)
        try:
            inputs = read_checkout_events(store, aliases)
        except EventServiceError as exc:
            raise _refuse(exc) from exc
        reconciled, observations = _fold(inputs)

        verdicts = _append_all(store, reconciled)
        if verdicts.error is not None:
            # Nothing to salvage: a `reconciled` verdict carries no observation, so it is
            # neither persisted nor announced, and the observations that descend from these
            # verdicts have not been composed into the chain yet. Composing them now would
            # seal observations naming a verdict the ledger just refused to take.
            raise verdicts.error
        # The observations go in AFTER the verdicts, so `reconciled_event_id` on each one
        # always names a row that is already in the chain.
        landings = _append_all(store, observations)

        persisted = (
            persist_observations(connection, landings.landed) if connection is not None else 0
        )
        # AFTER the relational write, for the same reason `events.routes.post_event` announces
        # after its projection: an agent that reacts by reading `GET /snapshot` must not beat
        # its own observation into the table it is about to read.
        notified = _announce_observations(request, store, landings.fresh)
        report = {
            **_report(inputs, reconciled, observations),
            "appended": {
                RECONCILED_KIND: verdicts.inserted,
                OBSERVATION_KIND: landings.inserted,
            },
            "already_present": {
                RECONCILED_KIND: verdicts.already,
                OBSERVATION_KIND: landings.already,
            },
            # Rows an EARLIER version of this fold sealed under a body this one would not
            # write. The stored verdict stands and this run's recomputation of it was
            # discarded. Reported rather than only logged, because "already present" and
            # "present, and not what this code would write today" are two different states of
            # a ledger and only one of them means the chain agrees with the running fold.
            "superseded": {
                RECONCILED_KIND: verdicts.superseded,
                OBSERVATION_KIND: landings.superseded,
            },
            # The subset of `superseded` whose STORED row could not be read back, so its
            # `ledger.trust_observations` row could not converge on this run. Separate from
            # `superseded` because the two mean opposite things about the relational table:
            # a superseded event is one that DID reach `persist_observations`, and one of these
            # is one that did not and was not guessed at. Non-zero here means re-run the fold.
            "superseded_unreadable": {
                RECONCILED_KIND: verdicts.unreadable,
                OBSERVATION_KIND: landings.unreadable,
            },
            "observations_written": persisted,
            "observations_persisted": connection is not None,
            "reconciled_event_ids": [str(event["event_id"]) for event in reconciled],
            # What the fold told the outside world. Reported for the same reason
            # `observation_rows` is on `POST /events`: this push is best effort by design, so a
            # caller must be able to tell "no observation to push" from "pushed to nobody".
            "notifications": notified,
        }
    _log.info(
        "reconciled %d order(s) from %d checkout event(s): %d verdict(s) and %d observation(s) "
        "appended, %d superseded by an older fold (%d of them unreadable, so their rows did not "
        "converge), %d observation row(s) written, %d of %d new observation(s) pushed to a store "
        "agent, %d left sealed in the chain and unannounced by the %.1fs announce budget "
        "(re-running the fold will not re-announce those)",
        report["reconciled"],
        len(inputs.events),
        verdicts.inserted,
        landings.inserted,
        verdicts.superseded + landings.superseded,
        verdicts.unreadable + landings.unreadable,
        persisted,
        notified["pushed"],
        notified["attempted"],
        notified.get("not_attempted", 0),
        ANNOUNCE_BUDGET_SECONDS,
    )
    if landings.error is not None:
        # Everything this batch DID seal is now persisted and announced; the caller gets the
        # refusal it would always have got, with the same status and the same body.
        raise landings.error
    return report
