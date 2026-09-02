"""The append seam: one normalisation rule, one idempotency rule, one in-memory chain.

Owned by T-060 (scope ``apps/trust/src/events/**``). Everything here is standard library
plus the *stdlib-only half* of :mod:`trust.ledger` -- the canonicaliser, the sealer and the
verifier. Nothing in this module imports ``psycopg`` or ``fastapi``, which is what lets the
frozen acceptance suite do ``from apps.trust.src.events import InMemoryEventStore, append``
on a machine with no database driver at all.

Three rules, and both stores obey the same three
---------------------------------------------------
1. **One normalisation.** :func:`normalise_event` is the only place an incoming event is
   turned into the body that gets hashed, and it delegates to
   :func:`trust.ledger.canonical_event`. D16 says nothing outside ``trust.ledger`` may
   define its own hashing; this module honours that by not defining any -- it validates,
   and then it calls.
2. **One idempotency key.** ``event_id`` *is* the idempotency key (D16). Re-appending an
   id already in the chain is a no-op that returns the row already there; re-appending an
   id with *different* content is an :class:`~.errors.IdempotencyConflict`, never a
   success-shaped no-op.
3. **One chain.** Appends seal through :func:`trust.ledger.seal_event` and take their
   predecessor from :func:`trust.ledger.chain_head`, so the stored ``prev_hash`` /
   ``event_hash`` on every event are exactly what :func:`trust.ledger.verify_chain` checks.
   A store that kept only a running head hash and did not stamp its events would leave the
   verifier with nothing to check.

Because the normalisation and the sealing are shared, the in-memory store and the Postgres
store produce **the same stream hash for the same input** -- which is a property
``apps/trust/tests/test_events.py`` asserts rather than assumes, because it is the only
evidence that the writer service did not quietly grow a second hashing rule.

The anchor, in memory
---------------------
:func:`trust.ledger.verify_chain` is truncation-blind without a witness recorded outside
the stream; in Postgres that witness is ``ledger.chain_head``. :class:`InMemoryEventStore`
keeps the same witness in :attr:`~InMemoryEventStore.anchor`: a count and a head hash
maintained *only* on a successful insert and never derived from the event list. Cut events
out of the list and the anchor still says how many there were, so
:meth:`~InMemoryEventStore.verify` reports ``truncated`` instead of a flawless prefix.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..ledger import (
    EVENT_FIELDS,
    canonical_event,
    canonical_json,
    chain_head,
    seal_event,
    stream_hash,
)
from .errors import BrokenChain, IdempotencyConflict, InvalidEvent, UnknownEventKind
from .integrity import anchored_report, verify_stream

__all__ = [
    "LEDGER_EVENT_KINDS",
    "AppendOutcome",
    "InMemoryEventStore",
    "append",
    "normalise_event",
]

#: The frozen ``LedgerEvent`` kind vocabulary (C11/D24). Thirteen from DESIGN §Interfaces
#: plus the five D24 added in T-010, and no others.
#:
#: This is a *second* copy of a list whose authority is the ``commerce_events_kind_check``
#: CHECK constraint in ``db/migrations/0002_ledger_tables.sql``, and a second copy of
#: anything is a drift risk. It is here anyway, for one reason: without it an unknown kind
#: reaches Postgres and comes back as a bare ``CheckViolation`` naming a constraint, which
#: the caller of an HTTP API cannot act on. ``test_events.py`` parses the CHECK constraint
#: out of the migration and asserts this set equals it exactly, so the drift is a failing
#: test rather than a silent divergence.
LEDGER_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "accepted",
        "auction_closed",
        "auction_opened",
        "bid_placed",
        "blacklist_expired",
        "blacklisted",
        "checkout_pixel",
        "checkout_redirect",
        "claim_verified",
        "code_created",
        "feedback",
        "offer_integrity",
        "order_fulfilled",
        "order_paid",
        "policy_event",
        "reconciled",
        "refund",
        "shown",
    }
)


@dataclass(frozen=True)
class AppendOutcome:
    """What one append did. The same shape from both stores.

    Attributes:
        event: the stored event, chain fields and ``seq`` included. For a duplicate this is
            the row that was **already there**, which is the one every other reader sees.
        inserted: ``False`` when the ``event_id`` was already in the chain.
        head_hash: the chain head after the call -- unchanged when ``inserted`` is ``False``.
        seq: the event's position in the chain, 1-based.
        length: how many events the chain holds, from the anchor rather than from a count
            of whatever the caller happens to be holding.
    """

    event: dict[str, Any]
    inserted: bool
    head_hash: str
    seq: int
    length: int


def normalise_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``event`` and return the canonical body that will be hashed and stored.

    Args:
        event: the incoming ``LedgerEvent``.

    Returns:
        :func:`trust.ledger.canonical_event`'s output: ``ts`` normalised to UTC RFC-3339
        milliseconds, ``None``-valued optional fields dropped, chain fields stripped and
        ``payload`` defaulted to ``{}``.

    Raises:
        InvalidEvent: the event is not a mapping, is missing ``event_id`` / ``ts`` /
            ``kind``, carries an unparseable timestamp, carries a ``payload`` that is not a
            JSON object, or carries a top-level field outside :data:`EVENT_FIELDS`.
        UnknownEventKind: ``kind`` is outside :data:`LEDGER_EVENT_KINDS`.

    The unknown-field check is the one that looks like bureaucracy and is not. An extra
    top-level key IS hashed (``canonical_event`` carries unknown fields through untouched)
    but has no column to live in, so Postgres stores the event without it and the writer's
    own round-trip guard then rejects the append with a message blaming ``jsonb`` float
    renormalisation -- a diagnosis that has nothing to do with the actual mistake. Rejecting
    it here names the field.
    """
    if not isinstance(event, Mapping):
        raise InvalidEvent(f"a LedgerEvent must be a JSON object, got {type(event).__name__}")

    try:
        body = canonical_event(event)
    except ValueError as exc:
        # `ValueError`, not just `CanonicalisationError` (which is a subclass of it). An
        # out-of-range but well-SHAPED timestamp -- "2026-13-01T00:00:00Z", "2026-02-30",
        # an hour of 25 -- satisfies `rfc3339_ms`'s regex and then fails inside
        # `datetime()`, which raises a bare `ValueError: month must be in 1..12`. Caught
        # only as `CanonicalisationError`, that escapes this function and reaches the caller
        # as a 500 for an input whose only problem is that it is invalid. Every `ValueError`
        # out of canonicalisation means the same thing -- this value has no deterministic
        # canonical form -- so every one of them is a rejected event, not a crash.
        raise InvalidEvent(f"LedgerEvent cannot be canonicalised: {exc}") from exc

    if "ts" not in body:
        raise InvalidEvent("LedgerEvent is missing required field 'ts'")

    event_id = body["event_id"]
    if not isinstance(event_id, str) or not event_id.strip():
        raise InvalidEvent(
            f"LedgerEvent.event_id must be a non-empty string; got {event_id!r}. It IS the "
            f"idempotency key (D16), so an empty one would make two unrelated events the "
            f"same event."
        )

    kind = body["kind"]
    if kind not in LEDGER_EVENT_KINDS:
        raise UnknownEventKind(
            f"{kind!r} is not a LedgerEvent kind. The vocabulary is frozen (C11/D24) and "
            f"holds exactly: {', '.join(sorted(LEDGER_EVENT_KINDS))}."
        )

    payload = body.get("payload")
    if not isinstance(payload, Mapping):
        raise InvalidEvent(
            f"LedgerEvent.payload must be a JSON object, got {type(payload).__name__}. "
            f"`ledger.commerce_events.payload` is jsonb with a "
            f"`jsonb_typeof(payload) = 'object'` CHECK."
        )

    unknown = sorted(set(body) - set(EVENT_FIELDS))
    if unknown:
        raise InvalidEvent(
            f"unknown LedgerEvent field(s) {unknown}. The shape is frozen: "
            f"{list(EVENT_FIELDS)}. An extra top-level field is hashed but has no column to "
            f"be stored in, so the row read back would not hash to the digest written and "
            f"the append would be refused with a message about jsonb number handling."
        )
    return body


class InMemoryEventStore:
    """An append-only, hash-chained event log with no datastore behind it.

    The store the frozen acceptance suite drives, and the one every downstream ticket can
    use to exercise ledger-shaped behaviour without Postgres. It is the *same* code path as
    :class:`~.pg.PostgresEventStore` down to the sealing: both normalise through
    :func:`normalise_event` and seal through :func:`trust.ledger.seal_event`.

    Thread-safe. The lock is not decoration: "a duplicate event_id is a no-op" is a
    check-then-write, and two threads appending the same id without a lock can both observe
    "not present" and both append. ``test_events.py`` drives that race with the switch
    interval turned down and asserts one row.

    Args:
        events: an existing **sealed** prefix to continue -- the in-memory equivalent of a
            service restarting against a ledger that already holds events. Verified on the
            way in: a store built on a prefix that does not verify would hand out a head
            hash for a chain that was already broken.

    Raises:
        BrokenChain: ``events`` is not an intact chain.
    """

    def __init__(self, events: Iterable[Mapping[str, Any]] = ()) -> None:
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = [copy.deepcopy(dict(row)) for row in events]
        self._by_id: dict[str, dict[str, Any]] = {}

        if self._events:
            report = verify_stream(self._events)
            if not report["ok"]:
                raise BrokenChain(
                    f"InMemoryEventStore was handed a prefix that is not an intact chain, so "
                    f"anything appended behind it would be anchored to a broken link. "
                    f"{report['detail']}"
                )
            for index, row in enumerate(self._events):
                row.setdefault("seq", index + 1)
                self._by_id[str(row["event_id"])] = row

        # The witness recorded OUTSIDE the stream (see the module docstring). Written only
        # by a successful insert, never recomputed from `self._events`.
        self._anchor_length = len(self._events)
        self._anchor_head = chain_head(self._events)

    # -- reads ------------------------------------------------------------------------
    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """Every event, oldest first, as **deep copies**.

        Copies because the ledger is append-only in memory as well as on disk: handing out
        the live dicts would let any reader silently rewrite a sealed event, and the store
        would then verify its own forgery. ``test_events.py`` mutates what this returns and
        asserts the store is unmoved.
        """
        with self._lock:
            return tuple(copy.deepcopy(row) for row in self._events)

    @property
    def head_hash(self) -> str:
        """The **stored** head: the last event's ``event_hash``, or the genesis link.

        This is what the next append links behind, so it reads the last event's digest
        rather than recomputing the chain -- :meth:`stream_hash` is the recomputed one, and
        the two answer different questions (see :mod:`trust.ledger.chain`).
        """
        with self._lock:
            return chain_head(self._events)

    @property
    def length(self) -> int:
        """How many events the store holds."""
        with self._lock:
            return len(self._events)

    @property
    def anchor(self) -> dict[str, Any]:
        """The commitment recorded at write time: ``{head_hash, length}``.

        The in-memory counterpart of ``ledger.chain_head``, and the only thing that can
        detect the stream being truncated from the tail.
        """
        with self._lock:
            return {"head_hash": self._anchor_head, "length": self._anchor_length}

    def read(
        self,
        *,
        after_seq: int = 0,
        store_id: str | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """The chain in insertion order, with the same filters as ``ledger.read_events``.

        A ``store_id``-filtered read is **not a chain** -- the links skip the events that
        were filtered out -- so it is for projection only, never for verification.
        ``event_id`` selects the single event carrying that idempotency key, mirroring the
        indexed lookup the Postgres store performs; :meth:`get` is the direct way to ask.
        """
        rows = [
            row
            for row in self.events
            if int(row.get("seq", 0)) > after_seq
            and (store_id is None or row.get("store_id") == store_id)
            and (event_id is None or str(row.get("event_id")) == event_id)
        ]
        return rows if limit is None else rows[:limit]

    def get(self, event_id: str) -> dict[str, Any] | None:
        """One event by its ``event_id``, or ``None``."""
        with self._lock:
            row = self._by_id.get(event_id)
            return None if row is None else copy.deepcopy(row)

    def stream_hash(self) -> str:
        """The stream's identity, **recomputed from content** (never read off a column)."""
        return stream_hash(self.events)

    def verify(self) -> dict[str, Any]:
        """Verify the links **and** the anchor, and name the broken link if there is one.

        Deliberately the *same* function the Postgres store's verification goes through, so
        "the ledger verifies" means one thing in this system rather than two.
        """
        return anchored_report(list(self.events), self.anchor)

    def replay(self) -> dict[str, Any]:
        """The whole chain: the verification report plus every event, in order."""
        report = self.verify()
        report["events"] = list(self.events)
        return report

    # -- the write --------------------------------------------------------------------
    def append(self, event: Mapping[str, Any]) -> AppendOutcome:
        """Append one event, or return the one already stored under its ``event_id``.

        Raises:
            InvalidEvent / UnknownEventKind: see :func:`normalise_event`.
            IdempotencyConflict: the id is present with different content.
        """
        body = normalise_event(event)
        event_id = str(body["event_id"])

        with self._lock:
            existing = self._by_id.get(event_id)
            if existing is not None:
                if canonical_json(canonical_event(existing)) != canonical_json(body):
                    raise IdempotencyConflict(
                        f"event_id {event_id!r} is already in the chain with DIFFERENT "
                        f"content, so accepting this append would silently lose it. D16 "
                        f"makes event_id the idempotency key: re-sending an id must "
                        f"re-send the same event.\n"
                        f"  stored:   {canonical_json(canonical_event(existing))[:300]}\n"
                        f"  incoming: {canonical_json(body)[:300]}"
                    )
                return AppendOutcome(
                    event=copy.deepcopy(existing),
                    inserted=False,
                    head_hash=chain_head(self._events),
                    seq=int(existing["seq"]),
                    length=len(self._events),
                )

            # `seq` is stamped AFTER sealing and is deliberately outside the hash (it is in
            # `trust.ledger.CHAIN_FIELDS`, which `canonical_event` strips). That is the one
            # mutable field the chain verifier cannot check, so the reason has to be stated
            # rather than assumed:
            #
            # 1. **In Postgres it does not exist yet.** `ledger.commerce_events.seq` is a
            #    `bigserial` the database assigns when the row lands, which is strictly
            #    after `compute_event_hash` has run on the body being inserted. An event
            #    cannot commit to a number that will not be chosen until after it is
            #    sealed, and reserving one from the sequence first would hand out a
            #    position that a rolled-back append then leaves as a permanent hole in the
            #    chain -- a gap indistinguishable from a deleted event.
            # 2. **Hashing it here would fork the hashing rule.** This store would seal a
            #    `seq` it invented while Postgres sealed a `seq` the database invented, so
            #    the two writers would produce different digests for the same event and D16's
            #    "one hashing rule, two stores" would be false. `test_events.py`'s
            #    `test_the_in_memory_and_postgres_writers_agree_on_the_stream_hash` is the
            #    assertion that would break.
            #
            # What actually commits to an event's POSITION is `prev_hash`: every event names
            # its predecessor's digest, so the order is sealed even though the label of the
            # order is not. Renumbering `seq` in a way that changes what an `order by seq`
            # read hands back therefore fails verification as `broken_link`; renumbering it
            # in a way that preserves the order changes nothing a reader can observe about
            # the chain. `test_events_hardening.py` pins both halves of that claim.
            sealed = seal_event(body, chain_head(self._events))
            sealed["seq"] = len(self._events) + 1
            self._events.append(sealed)
            self._by_id[event_id] = sealed
            self._anchor_length += 1
            self._anchor_head = str(sealed["event_hash"])
            return AppendOutcome(
                event=copy.deepcopy(sealed),
                inserted=True,
                head_hash=self._anchor_head,
                seq=int(sealed["seq"]),
                length=self._anchor_length,
            )

    def append_all(self, events: Sequence[Mapping[str, Any]]) -> list[AppendOutcome]:
        """Append a sequence in order. Not atomic across the batch (nor is the ledger's)."""
        return [self.append(event) for event in events]

    def __len__(self) -> int:
        return self.length

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<InMemoryEventStore length={self.length} head={self.head_hash[:12]}...>"


def append(store: Any, event: Mapping[str, Any]) -> AppendOutcome:
    """Append ``event`` to ``store`` -- the one entry point, whatever the store is.

    The frozen acceptance suite calls this as ``append(store, event)`` and the service
    calls it on a :class:`~.pg.PostgresEventStore`; both reach the same normalisation, the
    same idempotency rule and the same sealer, because the function does nothing but
    delegate. A second append implementation here is exactly the thing D16 forbids.

    Raises:
        TypeError: ``store`` has no ``append``. Named rather than left as an
            ``AttributeError`` on an arbitrary object, because ``append(event, store)`` with
            the arguments the wrong way round is the mistake this catches.
    """
    appender = getattr(store, "append", None)
    if not callable(appender):
        raise TypeError(
            f"append(store, event) wants an event store as its FIRST argument; "
            f"{type(store).__name__} has no callable `append`."
        )
    return appender(event)
