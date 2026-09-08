"""The auction state machine, its Redis home, and the transitions it writes to the ledger.

DESIGN pins the storage: ``auction:{id}`` in Redis, TTL 15 minutes. That TTL is the reason
this is a state machine and not a status column — an auction is a short-lived thing with a
hard deadline, and the interesting bugs are all about *ordering*: closing an auction twice,
accepting a bid on an auction that never opened, re-accepting one that already paid out.

The legal moves, and nothing else, are:

.. code-block:: text

    CREATED --open--> OPEN --close--> CLOSED --accept--> ACCEPTED
                        |                |
                        +---expire-------+---> EXPIRED

Anything else raises :class:`IllegalAuctionTransition`. That is the guard, and it refuses
real inputs: ``close`` on a `CLOSED` auction, ``accept`` on an `OPEN` one (bids are still
arriving — accepting now would accept a partial field), and ``accept`` twice on the same
auction, which is the double-accept R3/A5 forbids.

Every accepted transition is recorded to the ledger through
:class:`apps.exchange.src.auction.ledger.LedgerRecorder`, using the frozen kinds T-010 added
for exactly this (D24): ``auction_opened``, ``auction_closed`` and ``accepted``. A rejected
transition writes nothing — the ledger records what happened, and a refused move did not
happen.

Storage is behind :class:`AuctionStore` with two implementations: the in-memory one (the
default, and what the unit tests drive) and :class:`RedisAuctionStore`, which is the
DESIGN-pinned one. ``RedisAuctionStore`` takes its client from
``proxyshop_support.redis_client.worker_redis`` and never constructs one itself — D39
forbids a raw client here, because an unprefixed key lands in a logical database shared with
every other worker.

Why a store has to do more than ``load``/``save`` (T-158)
---------------------------------------------------------

The paragraph above says ``accept`` twice on the same auction is refused. Until T-158 that
was true only of *sequential* accepts. :meth:`AuctionStateMachine._transition` was a bare
``get()`` -> mutate -> ``save()`` with nothing between the read and the write, and
``RedisAuctionStore`` is literally two separate network round trips, so two concurrent
requests both read ``closed``, both found ``accepted`` legal, and both wrote. Measured on a
store with a 2 ms round trip, before the repair::

    A: ACCEPTED ok -> accepted_bid_ref='bid-a'
    B: ACCEPTED ok -> accepted_bid_ref='bid-b'
    transitions that succeeded  : 2 of 2
    ACCEPTED transition is serialised: False

The ticket's own prescription — "put the guard on the already-serialised ACCEPTED
transition" — therefore could not work: there was no serialisation to put it on. Moving the
check inside ``_transition`` would have relocated the race, not closed it.

What closes it is a **uniqueness constraint in the durable store**, which is what
:meth:`AuctionStore.reserve` is: an at-most-once, atomic, durable reservation of a named
move for one auction id. It is not a lock — nothing blocks, nothing is held for a duration,
there is nothing to time out and nothing to deadlock. Exactly one caller is told it won; every
other caller is told, truthfully, who did. It is one Redis ``SET key value NX EX`` — the one
command Redis makes atomic by construction — and one lock-guarded ``dict`` insertion in
memory. Three call sites use it:

``exit:{state}``
    the single move *out of* a state. Each state in :data:`TRANSITIONS` is left at most once,
    so this is precisely the state machine's own semantics rather than an approximation of
    them, and it correctly makes ``accept`` and ``expire`` race each other for the one exit
    from ``closed`` instead of both silently winning.

``create``
    the ``if not exists: create`` at :meth:`AuctionStateMachine.create`, which was the same
    read-modify-write shape. Not exploitable today — the only HTTP caller mints
    ``auction-{uuid4()}`` — but it is one line away from the primitive now that it exists.

``accept``
    the acceptance claim :mod:`apps.exchange.src.accept.claims` takes **before** the merchant
    is asked to mint. That ordering is the whole of the money fix: an atomic transition that
    happens *after* ``POST /codes`` still leaves two live single-use discount codes behind and
    merely refuses the second buyer a permalink.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .ledger import LedgerRecorder, LedgerSink

__all__ = [
    "ACCEPTED",
    "ACCEPTED_KIND",
    "AUCTION_CLOSED_KIND",
    "AUCTION_KEY_TEMPLATE",
    "AUCTION_OPENED_KIND",
    "AUCTION_TTL_SECONDS",
    "CLOSED",
    "CREATED",
    "EXPIRED",
    "OPEN",
    "RESERVATION_KEY_TEMPLATE",
    "TRANSITIONS",
    "AuctionRecord",
    "AuctionStateMachine",
    "AuctionStore",
    "IllegalAuctionTransition",
    "InMemoryAuctionStore",
    "RedisAuctionStore",
    "UnknownAuction",
    "creation_reservation",
    "exit_reservation",
]

CREATED = "created"
OPEN = "open"
CLOSED = "closed"
ACCEPTED = "accepted"
EXPIRED = "expired"

#: The only legal moves. A state absent from a source's value set is a terminal state.
TRANSITIONS: Mapping[str, frozenset[str]] = {
    CREATED: frozenset({OPEN, EXPIRED}),
    OPEN: frozenset({CLOSED, EXPIRED}),
    CLOSED: frozenset({ACCEPTED, EXPIRED}),
    ACCEPTED: frozenset(),
    EXPIRED: frozenset(),
}

#: The three frozen `LedgerEventKind` values (D24) this module produces, named as values.
#:
#: Named rather than spelled inline in :data:`_TRANSITION_KIND`, and that is not house-keeping.
#: The kind an emitter produces is a searchable fact — ``apps/trust/src/reconcile/engine.py``'s
#: ``PIXEL_KIND``/``FULFILLED_KIND`` and ``apps/exchange/src/ranking/serving.py``'s ``SHOWN_KIND``
#: are the same convention — and a mapping keyed by *state* hid all three of these behind a
#: lookup no reader of the vocabulary could resolve to a producer. T-302 measured exactly that:
#: a sweep of every product module for the kinds it names reported ``auction_opened`` and
#: ``auction_closed`` as produced by nothing, while this file had been writing both on every
#: served ``POST /auctions`` since the route existed.
AUCTION_OPENED_KIND = "auction_opened"
AUCTION_CLOSED_KIND = "auction_closed"
ACCEPTED_KIND = "accepted"

#: The ledger kind each transition writes. `EXPIRED` writes `auction_closed` — an auction
#: that timed out did close; the payload's `reason` is what distinguishes the two.
_TRANSITION_KIND: Mapping[str, str] = {
    OPEN: AUCTION_OPENED_KIND,
    CLOSED: AUCTION_CLOSED_KIND,
    EXPIRED: AUCTION_CLOSED_KIND,
    ACCEPTED: ACCEPTED_KIND,
}

#: DESIGN: `auction:{id}` in Redis, TTL 15 minutes.
AUCTION_KEY_TEMPLATE = "auction:{auction_id}"
AUCTION_TTL_SECONDS = 15 * 60

#: Where a reservation lives: beside the record it constrains, under the same key prefix and
#: the same TTL *budget*. It must not outlive its auction — a stale reservation would refuse a
#: legitimate move on a later auction that reused the id.
#:
#: It does expire EARLIER than the record, and that is measured rather than assumed: `save`
#: refreshes the record's TTL on every transition while `SET … NX EX` sets a reservation's
#: once, so with a 20-second TTL and an auction 3 seconds old the record read 20 and the
#: `create` reservation read 17. Harmless in the direction that matters — an expired
#: reservation degrades to the checks that were there before it (`load() is not None` for
#: `create`, the legality check plus the state re-read for a transition), so it can lose a
#: refusal's *precision* but cannot produce a second accept, because a second accept also
#: needs a record still sitting in `closed`. It is stated here so nobody reads "same TTL" as
#: "same expiry".
RESERVATION_KEY_TEMPLATE = "auction:{auction_id}:reserved:{name}"


def exit_reservation(state: str) -> str:
    """The reservation name for "the one move out of ``state``"."""
    return f"exit:{state}"


#: The reservation name for "this auction id has been created". Not a state exit — there is
#: no state to leave — but the same at-most-once constraint.
def creation_reservation() -> str:
    return "create"


class IllegalAuctionTransition(RuntimeError):
    """A move the state machine does not allow from the auction's current state."""


class UnknownAuction(KeyError):
    """No auction with that id is in the store (never created, or its TTL expired)."""


@dataclass
class AuctionRecord:
    """The whole of an auction's runtime state — this is what lives at ``auction:{id}``."""

    auction_id: str
    intent_id: str
    cluster_id: str
    state: str = CREATED
    roster: list[dict[str, Any]] = field(default_factory=list)
    accepted_bid_ref: str | None = None
    opened_at: float | None = None
    closed_at: float | None = None
    deadline: float | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, blob: str) -> AuctionRecord:
        return cls(**json.loads(blob))


class AuctionStore(Protocol):
    """Where auction records live for their fifteen minutes.

    ``load``/``save`` are the record. :meth:`reserve`/:meth:`release` are the *constraint*,
    and a store that implements the first pair without the second cannot make this state
    machine safe: two callers that both read and both write are two callers that both won.
    See the module docstring for what that cost on the money path (T-158).
    """

    def load(self, auction_id: str) -> AuctionRecord | None: ...

    def save(self, record: AuctionRecord) -> None: ...

    def reserve(self, auction_id: str, name: str, token: str) -> str | None:
        """Claim ``name`` for ``auction_id``, at most once, atomically and durably.

        Returns ``None`` when this caller won the reservation, and otherwise the ``token``
        the caller that already holds it wrote. It must never be possible for two callers to
        both be told they won, *including two callers in different OS processes*: that is the
        entire contract, and an implementation that cannot honour it must not be used to
        hold auctions on a money path.
        """
        ...

    def release(self, auction_id: str, name: str, token: str) -> None:
        """Give back a reservation this caller holds, so a failed attempt can be retried.

        A no-op unless the reservation is currently held with exactly ``token`` — releasing a
        reservation somebody else won is how a "safe" retry mints the second discount code.
        """
        ...


class InMemoryAuctionStore:
    """Process-local store. The default, and what every non-docker test drives.

    :meth:`reserve` is atomic **within this process** — the lock makes it safe against
    threads, which is what the served app's thread pool actually is — and is honest about
    being nothing more: two processes each holding their own ``InMemoryAuctionStore`` share
    no state at all, so they share no constraint either. That is a property of the store, not
    of the guard built on it; :class:`RedisAuctionStore` gives the same guard a cross-process
    reach because Redis is a shared, durable place to put a ``SET NX``.
    """

    def __init__(self) -> None:
        self._records: dict[str, str] = {}
        # Unbounded, exactly as `_records` above is: this store is the process-local default
        # and forgets nothing. Four reservations per auction (`create` plus one exit per
        # state), so it multiplies an existing leak rather than introducing one — measured at
        # 200 records / 800 reservations after 200 auctions. A deployment that cares runs
        # `RedisAuctionStore`, where the 15-minute TTL evicts both.
        self._reservations: dict[tuple[str, str], str] = {}
        # Guards the reservation table only. The record dict is left alone: CPython's own
        # lock already makes a single dict assignment atomic, and pretending a lock here
        # made `load`-decide-`save` safe is exactly the illusion this ticket is about.
        self._lock = threading.Lock()

    def load(self, auction_id: str) -> AuctionRecord | None:
        blob = self._records.get(auction_id)
        return AuctionRecord.from_json(blob) if blob is not None else None

    def save(self, record: AuctionRecord) -> None:
        # Serialise on the way in, exactly like the Redis store, so a value that would not
        # survive a round trip fails in the cheap test rather than only under docker.
        self._records[record.auction_id] = record.to_json()

    def reserve(self, auction_id: str, name: str, token: str) -> str | None:
        with self._lock:
            held = self._reservations.get((str(auction_id), str(name)))
            if held is not None:
                return held
            self._reservations[(str(auction_id), str(name))] = str(token)
            return None

    def release(self, auction_id: str, name: str, token: str) -> None:
        with self._lock:
            key = (str(auction_id), str(name))
            if self._reservations.get(key) == str(token):
                del self._reservations[key]


def _as_text(value: Any) -> str | None:
    """Redis answers ``bytes`` or ``str`` depending on ``decode_responses``. Take both."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class RedisAuctionStore:
    """The DESIGN-pinned store: key ``auction:{id}``, TTL 15 minutes.

    The client must be a ``proxyshop_support.redis_client.WorkerRedis`` (D39): it applies
    this worker's ``w{N}:`` key prefix and logical-DB index centrally, so nothing here
    prefixes a key by hand and nothing here constructs a client.
    """

    def __init__(self, client: Any, *, ttl_seconds: int = AUCTION_TTL_SECONDS) -> None:
        self._client = client
        self._ttl = ttl_seconds

    @staticmethod
    def key(auction_id: str) -> str:
        return AUCTION_KEY_TEMPLATE.format(auction_id=auction_id)

    def load(self, auction_id: str) -> AuctionRecord | None:
        blob = _as_text(self._client.get(self.key(auction_id)))
        if blob is None:
            return None
        return AuctionRecord.from_json(blob)

    def save(self, record: AuctionRecord) -> None:
        self._client.set(self.key(record.auction_id), record.to_json(), ex=self._ttl)

    @staticmethod
    def reservation_key(auction_id: str, name: str) -> str:
        return RESERVATION_KEY_TEMPLATE.format(auction_id=auction_id, name=name)

    def reserve(self, auction_id: str, name: str, token: str) -> str | None:
        """``SET key token NX EX ttl`` — one command, atomic in the server, not in us.

        This is deliberately *not* ``WATCH``/``MULTI`` and not a Lua script. ``SET`` with
        ``NX`` is a single round trip that Redis itself serialises, so there is no optimistic
        retry loop to get wrong, no transaction to abandon, and nothing that behaves
        differently under a proxy or a cluster. ``WATCH`` would also have to reach through
        :class:`~proxyshop_support.redis_client.WorkerRedis`'s ``execute_command`` key
        rewriting via a pipeline object that does not go through it; ``SET`` does go through
        it, so the ``w{N}:`` prefix and the per-worker logical DB (D39) apply to reservations
        exactly as they apply to records.
        """
        key = self.reservation_key(auction_id, name)
        if self._client.set(key, str(token), nx=True, ex=self._ttl):
            return None
        return _as_text(self._client.get(key)) or ""

    def release(self, auction_id: str, name: str, token: str) -> None:
        """Release only what we still hold.

        The read-then-delete here is not the shape this ticket forbids: a reservation cannot
        change hands while it is held, so "it is still ours" cannot become false under us
        except by the TTL expiring — fifteen minutes, against a release that follows its
        reservation within one request.
        """
        key = self.reservation_key(auction_id, name)
        if _as_text(self._client.get(key)) == str(token):
            self._client.delete(key)


class AuctionStateMachine:
    """Drives auctions through their legal states and records each move to the ledger."""

    def __init__(
        self,
        store: AuctionStore | None = None,
        ledger: LedgerSink | LedgerRecorder | None = None,
    ) -> None:
        self.store: AuctionStore = store if store is not None else InMemoryAuctionStore()
        if isinstance(ledger, LedgerRecorder):
            self.ledger = ledger
        else:
            self.ledger = LedgerRecorder(ledger)

    # --- reads ----------------------------------------------------------------------
    def get(self, auction_id: str) -> AuctionRecord:
        record = self.store.load(auction_id)
        if record is None:
            raise UnknownAuction(
                f"auction {auction_id!r} is not in the store — it was never created, or its "
                f"{AUCTION_TTL_SECONDS}s TTL has expired"
            )
        return record

    def state_of(self, auction_id: str) -> str:
        return self.get(auction_id).state

    # --- writes ---------------------------------------------------------------------
    def create(
        self,
        auction_id: str,
        *,
        intent_id: str,
        cluster_id: str,
        roster: list[dict[str, Any]] | None = None,
        deadline: float | None = None,
    ) -> AuctionRecord:
        # Reserve FIRST, then look. `load() is not None` -> `save()` is the same
        # read-modify-write shape as the double accept: two concurrent creates both read
        # `None` and the second overwrites the first's roster, deadline and history. The
        # only caller today mints `auction-{uuid4()}` so it is not reachable over HTTP, but
        # the primitive that closes it is one line away and a client-supplied id would make
        # it live.
        name = creation_reservation()
        if self.store.reserve(auction_id, name, auction_id) is not None:
            raise IllegalAuctionTransition(f"auction {auction_id!r} already exists")
        # Everything from here to a landed `save` is inside the reservation, so it has to be
        # inside a release too. Without it one `ConnectionError` out of `save` left the
        # reservation held with no record behind it, and every retry then answered "already
        # exists" for an auction whose `store.load(...)` was `None` — for 15 minutes against
        # Redis and FOREVER against the in-memory store, which has no TTL. An availability
        # regression, and one whose message blames a collision that never happened.
        try:
            if self.store.load(auction_id) is not None:
                raise IllegalAuctionTransition(f"auction {auction_id!r} already exists")
            record = AuctionRecord(
                auction_id=auction_id,
                intent_id=intent_id,
                cluster_id=cluster_id,
                roster=[dict(entry) for entry in (roster or [])],
                deadline=deadline,
            )
            self.store.save(record)
        except BaseException:
            self.store.release(auction_id, name, auction_id)
            raise
        return record

    def open(self, auction_id: str, *, now: float | None = None) -> AuctionRecord:
        """Open the auction and record ``auction_opened`` with D24's published body.

        ``roster_size`` is not passed in: it is ``len(record.roster)`` read off the record the
        transition just wrote, so the number in the ledger is the roster the auction actually
        ran and cannot disagree with it. ``roster`` — the store ids, in roster order — is the
        extra key that makes the event reconstructible rather than merely well-formed: a
        ``roster_size`` of 4 says four stores were on the list and names none of them, and the
        first question a store that lost asks is whether it was on that list at all.

        Ids only, never the roster ROWS. A row carries a merchant-stated ``list_price`` and a
        ``product_ref``; the ids are bounded by ``routes.MAX_IDENTIFIER_LENGTH`` and the list by
        ``routes.MAX_ROSTER_ENTRIES``, which is what keeps an append-only durable row O(roster)
        in values the platform already published in its own 201 body.
        """
        return self._transition(auction_id, OPEN, now=now)

    def close(
        self,
        auction_id: str,
        *,
        now: float | None = None,
        reason: str = "deadline",
        shortlist_size: int | None = None,
        outcome: Mapping[str, Any] | None = None,
    ) -> AuctionRecord:
        """Close the auction and record ``auction_closed`` with D24's published body.

        ``shortlist_size`` is the second published key and it is the caller's to supply,
        because the auction does not know it: the shortlist is decided by the ranking that
        runs after bidding stops, and this class holds no ranker. A caller that has not ranked
        — the simulator, a test driving the machine directly, ``expire`` below — passes
        nothing and the key is written as ``None``.

        ``None`` rather than ``0``, and the difference is the whole reason this is not
        defaulted to a number: ``0`` is a *measurement* ("the auction ranked and shortlisted
        nobody"), which is a real and common outcome for a fail-closed exchange, and writing it
        for an auction that never ranked would put a false measurement in an append-only log.
        This is the convention :meth:`accept` already follows for ``checkout_token`` — the key
        is present so the body is the published one, and its emptiness is visible in the record
        rather than inferred from a key that is not there.

        ``outcome`` carries the rest of what the close decided — who was solicited, who
        answered, who was excluded and why, which slots were filled. Nothing here invents any
        of it: a caller that measured none of it passes none, and the event then says only what
        a close with no ranker behind it can say.
        """
        payload: dict[str, Any] = {"reason": reason, "shortlist_size": shortlist_size}
        payload.update(dict(outcome or {}))
        return self._transition(auction_id, CLOSED, now=now, payload=payload)

    def expire(self, auction_id: str, *, now: float | None = None) -> AuctionRecord:
        """Expire the auction, which is a close whose ``reason`` says the deadline won.

        ``shortlist_size`` is ``None`` and cannot be anything else: an auction that timed out
        never reached a ranking, so there is no shortlist to have a size — see :meth:`close`
        for why that is not zero.
        """
        return self._transition(
            auction_id,
            EXPIRED,
            now=now,
            payload={"reason": "expired", "shortlist_size": None},
        )

    def accept(
        self,
        auction_id: str,
        bid_ref: str,
        *,
        now: float | None = None,
        checkout_token: str | None = None,
        offer: Mapping[str, Any] | None = None,
        store_id: str | None = None,
    ) -> AuctionRecord:
        """Stamp the auction accepted and record the ``accepted`` event (D24's body).

        ``checkout_token`` and ``offer`` are the other two thirds of that published body, and
        they are not decoration. ``apps/trust/src/reconcile/engine.py`` builds its join keys
        from ``payload['checkout_token']`` first and drops any event that carries none, so an
        ``accepted`` event without it never lands in the same group as the ``order_paid``
        webhook and the order silently never reconciles — there is nothing to grade the
        promise against. ``offer`` is that promise: ``_promised()`` reads ``product_ref``,
        ``unit_price``, ``total_price`` and ``discount`` straight off it, and with no offer
        every comparison reads ``incomparable``.

        Both default to ``None`` because a caller that has neither — the simulator, a test
        driving the machine directly — must still be able to accept an auction. The keys are
        written either way, so the body is the published one and its emptiness is visible in
        the record rather than inferred from a key that is not there.
        """
        return self._transition(
            auction_id,
            ACCEPTED,
            now=now,
            payload={
                "bid_ref": bid_ref,
                "checkout_token": checkout_token,
                "offer": dict(offer) if offer is not None else None,
            },
            bid_ref=bid_ref,
            store_id=store_id,
        )

    def _transition(
        self,
        auction_id: str,
        target: str,
        *,
        now: float | None = None,
        payload: Mapping[str, Any] | None = None,
        bid_ref: str | None = None,
        store_id: str | None = None,
    ) -> AuctionRecord:
        record = self.get(auction_id)
        allowed = TRANSITIONS.get(record.state, frozenset())
        if target not in allowed:
            raise IllegalAuctionTransition(
                f"auction {auction_id!r} is {record.state!r}; {target!r} is not a legal move "
                f"(legal: {sorted(allowed) or 'none — terminal state'})"
            )

        # THE serialisation point (T-158). The check above reads a record that another
        # request may already be rewriting; this line is the only thing in the method that
        # cannot be won twice. Reserving the *exit from a state* rather than the arrival at
        # one is what makes `accept` and `expire` race each other for the single move out of
        # `closed`, instead of both being told they succeeded and the later `save` silently
        # discarding the earlier one.
        source = record.state
        name = exit_reservation(source)
        taken = self.store.reserve(auction_id, name, target)
        if taken is not None:
            raise IllegalAuctionTransition(
                f"auction {auction_id!r} has already left {source!r} for {taken!r}; a "
                f"concurrent request won that move, so {target!r} is not applied"
            )

        # The reservation is given back on ANY failure before the write lands, and that is
        # not defensive padding — without it a single `ConnectionError` out of `save` turned
        # a retryable `close` into a permanent `IllegalAuctionTransition: … already left
        # 'open' for 'closed'; a concurrent request won that move` on the in-memory store,
        # and a 15-minute one against Redis. A reservation is a uniqueness constraint on a
        # move that HAPPENED; a move that raised did not happen and must not hold one.
        # `BaseException`, not `Exception`: a `KeyboardInterrupt` or a cancelled task between
        # the reserve and the save wedges the auction exactly as an I/O error does.
        try:
            # Re-read behind the reservation. Nothing else can be leaving `source` now, so
            # this is the freshest record that can exist, and a stale copy read before the
            # reservation was won would write back fields a concurrent writer had changed.
            record = self.get(auction_id)
            if record.state != source:
                raise IllegalAuctionTransition(
                    f"auction {auction_id!r} moved from {source!r} to {record.state!r} while "
                    f"{target!r} was being applied; it is not applied"
                )

            record.state = target
            if target == OPEN:
                record.opened_at = now
            elif target in (CLOSED, EXPIRED):
                record.closed_at = now
            elif target == ACCEPTED:
                record.accepted_bid_ref = bid_ref
            record.history.append({"state": target, "at": now})
            self.store.save(record)
        except BaseException:
            self.store.release(auction_id, name, target)
            raise

        # The published body (D24) for the kind this transition writes, composed from the
        # record that was just saved rather than from anything the caller said. `intent_id`
        # and `cluster_id` are two of `auction_opened`'s three published keys and were always
        # here; `roster_size` is the third and was not, so every `auction_opened` this service
        # has ever written failed `contracts.ledger.validate_ledger_payload` — a ledger row
        # saying an auction opened and refusing to say on how many stores.
        body: dict[str, Any] = {
            "state": target,
            "intent_id": record.intent_id,
            "cluster_id": record.cluster_id,
        }
        if target == OPEN:
            body["roster_size"] = len(record.roster)
            body["roster"] = [
                str(entry.get("store_id") or "")
                for entry in record.roster
                if isinstance(entry, Mapping)
            ]
        body.update(dict(payload or {}))
        self.ledger.record(
            _TRANSITION_KIND[target],
            auction_id=auction_id,
            store_id=store_id,
            payload=body,
        )
        return record
