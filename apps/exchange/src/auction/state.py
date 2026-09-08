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

Which store a deployment runs (T-3xx)
-------------------------------------
Until :func:`build_auction_store` and its two callers existed, that sentence described a
choice nobody could make. ``AuctionStateMachine`` defaulted to the in-memory store, the
composition root built a machine without naming one, and neither
``exchange.composition.Deployment`` nor ``configure_exchange`` carried a key that could say
otherwise — so **every** served exchange held every auction it had ever opened in a
process-local ``dict`` with no eviction, while ``apps/exchange/compose.yaml`` told its
operator that ``--redis`` was this state machine's measured dependency. ``POST /auctions`` is
unauthenticated, so that was memory growth any caller could drive, in a container limited to
256 MiB.

Both halves are closed here, and they are independent on purpose:

* :data:`ENV_AUCTION_STORE` (``EXCHANGE_AUCTION_STORE=redis``) and the deployment document's
  ``auction_store`` key select the store; :func:`build_auction_store` builds it and RAISES
  rather than degrading, because silently running a process-local store for a deployment that
  asked for a durable one is the failure the seam exists to prevent.
* :class:`InMemoryAuctionStore` is bounded whether or not anybody uses that seam — capacity,
  TTL, oldest-write-first eviction, and an :meth:`~InMemoryAuctionStore.absence` that names
  eviction by id so the 404 stops blaming a TTL that has not run out. A store nobody can
  replace must not also be a store nobody can bound.

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
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .ledger import LedgerRecorder, LedgerSink

__all__ = [
    "ACCEPTANCE_RESERVATION_NAME",
    "ACCEPTED",
    "ACCEPTED_KIND",
    "AUCTION_CLOSED_KIND",
    "AUCTION_KEY_TEMPLATE",
    "AUCTION_OPENED_KIND",
    "AUCTION_STORE_MEMORY",
    "AUCTION_STORE_REDIS",
    "AUCTION_STORE_WORDS",
    "AUCTION_TTL_SECONDS",
    "CLOSED",
    "CREATED",
    "DEFAULT_AUCTION_CAPACITY",
    "ENV_AUCTION_STORE",
    "EXPIRED",
    "OPEN",
    "RESERVATION_KEY_TEMPLATE",
    "RESERVATION_NAMES_PER_AUCTION",
    "TRANSITIONS",
    "AuctionRecord",
    "AuctionStateMachine",
    "AuctionStore",
    "AuctionStoreUnavailable",
    "IllegalAuctionTransition",
    "InMemoryAuctionStore",
    "RedisAuctionStore",
    "UnknownAuction",
    "absence_of",
    "auction_store_from_env",
    "build_auction_store",
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

#: How many auction records :class:`InMemoryAuctionStore` holds at once.
#:
#: The same number, for the same reason, as ``ranking.serving.DEFAULT_SHORTLIST_CAPACITY``, and
#: it is a SECOND bound rather than a duplicate one: that store holds the shortlist an auction
#: produced, this one holds the auction itself, and until this constant existed the second was
#: unbounded while the first was capped. ``POST /auctions`` is unauthenticated and mints one
#: record (plus up to :data:`RESERVATION_NAMES_PER_AUCTION` reservations) per call, so a store
#: with only a time bound is memory growth any caller can drive inside one TTL window — under
#: the 256 MiB limit ``apps/exchange/compose.yaml`` sets on this container.
#:
#: **The cap is reachable by an unauthenticated caller**, exactly as the shortlist cap is, and
#: that is stated here rather than left as a footnote: 513 cheap posts inside one 15-minute
#: window evict every auction created before them. :meth:`InMemoryAuctionStore.absence` is what
#: keeps that honest — an auction the cap took away says so by name, instead of being reported
#: as a TTL that has not run out yet.
DEFAULT_AUCTION_CAPACITY = 512

#: The most reservation names one auction can hold at once: ``create``, the three exits
#: (:data:`TRANSITIONS` has three non-terminal states) and ``accept``
#: (``exchange.accept.claims.ACCEPTANCE_RESERVATION``).
#:
#: Used as the multiplier on the reservation table's own cap, and the inequality is the point:
#: a live record holds at most this many reservations, so ``capacity * this`` can only be
#: reached by reservations whose records are already gone. The reservation cap is therefore a
#: backstop that never fires while :meth:`InMemoryAuctionStore.save`'s eviction is doing its
#: job — which matters, because a reservation evicted while its record is still ``closed``
#: would hand a second caller the exit from ``closed`` that T-158 exists to make single.
RESERVATION_NAMES_PER_AUCTION = 5

#: The reservation name ``exchange.accept.claims`` holds while the MERCHANT IS MINTING, and
#: the one name eviction must not step on.
#:
#: Restated here rather than imported, and the direction of the dependency is the reason:
#: ``accept`` imports ``auction``, so importing back would be a cycle. It is the same
#: arrangement ``proxyshop_support.postgres`` uses for ``ROLE_PASSWORD_ENV`` — one spelling
#: repeated where it cannot be imported, with a test asserting the two agree
#: (``test_auction_store_bounds.py``).
#:
#: Why eviction has to know about it. ``accept/routes.py`` reads the auction, takes this
#: claim, asks the merchant for a single-use discount code, and only then applies the
#: ``accepted`` transition — the ordering that stops a second buyer getting a second live
#: code (T-158). Between the claim and the transition sits a network round trip to the
#: merchant, and a cap that evicted the record inside that window would leave a MINTED CODE
#: with no accepted auction behind it: the money spent, the buyer refused, and no ``accepted``
#: event for trust's reconciler to grade the promise against. Measured on the served route
#: before this constant existed — one burst during the mint produced ``merchant mints: 1``
#: and an HTTP 500.
ACCEPTANCE_RESERVATION_NAME = "accept"

#: The two words :data:`ENV_AUCTION_STORE` and the deployment document's ``auction_store`` key
#: may carry. ``memory`` is what a process running alone gets; ``redis`` is the DESIGN-pinned
#: store and the only one whose reservations reach across processes.
AUCTION_STORE_MEMORY = "memory"
AUCTION_STORE_REDIS = "redis"
AUCTION_STORE_WORDS: tuple[str, ...] = (AUCTION_STORE_MEMORY, AUCTION_STORE_REDIS)

#: The environment name that selects the store, mirroring ``EXCHANGE_SHOP_ROSTER``.
#:
#: An environment variable AS WELL AS a deployment-document key, because the two reach
#: different deployments: ``apps/exchange/compose.yaml`` forwards named variables into a
#: container whose deployment document is generated by a script, so an operator running the
#: shipped stack can set this and an operator authoring a document can state it. The document
#: wins where both are present — see ``exchange.composition.bind_auction_machine``.
ENV_AUCTION_STORE = "EXCHANGE_AUCTION_STORE"

#: Why an evicted record is gone, as :meth:`InMemoryAuctionStore.absence` reports it.
_GONE_EVICTED = "evicted"
_GONE_EXPIRED = "expired"


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

    There is one OPTIONAL member beyond these four — ``absence(auction_id) -> str``, the
    sentence :func:`absence_of` puts in the 404 when ``load`` answers ``None``. It is not
    declared here on purpose: a store that cannot say anything more than "it is not here" is
    still a usable store, and both in-repository test doubles are exactly that. A store that
    EVICTS, though, has to implement it, or every auction its cap took away is reported to the
    operator as a TTL that has not run out.
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
    """Process-local store, BOUNDED: at most ``capacity`` records, each for ``ttl_seconds``.

    :meth:`reserve` is atomic **within this process** — the lock makes it safe against
    threads, which is what the served app's thread pool actually is — and is honest about
    being nothing more: two processes each holding their own ``InMemoryAuctionStore`` share
    no state at all, so they share no constraint either. That is a property of the store, not
    of the guard built on it; :class:`RedisAuctionStore` gives the same guard a cross-process
    reach because Redis is a shared, durable place to put a ``SET NX``.

    Why it is bounded now
    ---------------------
    It used to be two plain ``dict``s that forgot nothing, and the docstring said so — "a
    deployment that cares runs :class:`RedisAuctionStore`". No deployment could: there was no
    seam in ``exchange.composition.Deployment`` or ``configure_exchange`` through which an
    operator could ask for the Redis store, so *every* served exchange ran this class, and
    ``POST /auctions`` is unauthenticated. One record plus up to
    :data:`RESERVATION_NAMES_PER_AUCTION` reservations per call, retained forever, inside a
    container ``apps/exchange/compose.yaml`` limits to 256 MiB, is memory growth any caller
    can drive. Both halves are fixed together: the seam exists (see
    ``exchange.composition.bind_auction_machine``) and this store no longer needs it to be
    used before it stops growing.

    The bounds are the SAME TWO the Redis store has, so the two implementations agree about
    when an auction stops existing rather than merely about how it is stored: ``ttl_seconds``
    is ``SET … EX``'s, refreshed on every :meth:`save` exactly as ``SET`` refreshes it, and
    ``capacity`` is the one thing Redis gets from the server's own eviction policy and a
    ``dict`` gets from nobody.

    Eviction order is oldest-WRITE first, not oldest-read: an auction is written on every
    transition, so the record evicted is the one that has gone longest without moving.
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_AUCTION_CAPACITY,
        ttl_seconds: float = AUCTION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        # id -> JSON blob, in oldest-WRITE-first order, and `_written_at` is the same table's
        # second column rather than a second table: every mutation below touches both under
        # one lock. Kept apart so `_records` stays `{auction_id: blob}` — the shape
        # `apps/exchange/tests/test_auction.py::_retained_records` sizes to prove an
        # anonymous caller cannot choose how many bytes this process keeps. Folding the
        # timestamp into the value would have left that test summing `len((float, str))` == 2
        # per record: still green, measuring nothing.
        self._records: OrderedDict[str, str] = OrderedDict()
        self._written_at: dict[str, float] = {}
        # Bounded on the same two axes as the records, plus the invariant
        # `RESERVATION_NAMES_PER_AUCTION` states: a reservation must not be evicted while the
        # record it constrains is still here, because the exit from `closed` is what makes
        # `accept` happen at most once (T-158).
        self._reservations: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()
        # What this store has FORGOTTEN and why, ids only, newest last. It is what makes the
        # 404 on an evicted auction truthful instead of a disjunction of four causes; it is
        # bounded by `capacity` like everything else here, and it holds no record body — an
        # auction id is `MAX_IDENTIFIER_LENGTH` bounded by the route that mints it.
        self._gone: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self.evicted = 0
        self.expired = 0
        # Guards BOTH tables now. The old comment here said the record dict was deliberately
        # left unguarded because CPython makes a single dict assignment atomic — true of a
        # single assignment, and no longer what `save` does: it is a pop, an insert and an
        # eviction loop, and two threads interleaving those can drop a record that is not
        # over the cap. It still does not make `load`-decide-`save` safe; that is what
        # :meth:`reserve` is for, and pretending otherwise is the illusion T-158 was about.
        self._lock = threading.Lock()

    # --- the bounds ---------------------------------------------------------------------
    def _forget(self, key: str, reason: str) -> None:
        """Drop one record, its reservations and remember why. Caller holds the lock."""
        self._records.pop(key, None)
        self._written_at.pop(key, None)
        # The reservations go WITH the record, and the order matters: a reservation left
        # behind by an evicted record would refuse a legitimate move on the next auction to
        # reuse the id, and one dropped while its record still sat in `closed` would hand a
        # second caller the exit that makes `accept` at-most-once. Neither can happen while
        # the two tables are emptied together, under one lock.
        #
        # A full scan of the reservation table per forgotten record, and it is measured rather
        # than assumed: at the shipped bounds (512 records, up to 2,560 reservations) a save
        # that evicts costs 74 µs against 9 µs for one that does not, so the whole 512-record
        # burst spends 37.7 ms inside this lock. Negligible beside the HTTP work on the same
        # request, and left simple on purpose — a per-auction name index would be a third
        # table to keep in step with the two the safety property above depends on. It is an
        # O(capacity) cost per eviction, so a deployment that raises
        # `DEFAULT_AUCTION_CAPACITY` by an order of magnitude should re-measure it first.
        for held in [pair for pair in self._reservations if pair[0] == key]:
            self._reservations.pop(held, None)
        self._gone.pop(key, None)
        self._gone[key] = (reason, self._clock())
        while len(self._gone) > self.capacity:
            self._gone.popitem(last=False)

    def _aged_out(self, key: str, moment: float) -> bool:
        """Has ``key``'s record passed its TTL? Lock held."""
        return moment - self._written_at.get(key, 0.0) >= self.ttl_seconds

    def _claimed(self, key: str, moment: float) -> bool:
        """Is a merchant minting against ``key`` right now? Lock held.

        See :data:`ACCEPTANCE_RESERVATION_NAME`. The reservation's own TTL is honoured, so a
        claim abandoned by a crashed process protects its record for at most ``ttl_seconds``
        rather than forever — a stuck claim must not become a hole in the cap.
        """
        held = self._reservations.get((key, ACCEPTANCE_RESERVATION_NAME))
        return held is not None and moment - held[0] < self.ttl_seconds

    def _evict(self) -> None:
        """Bring the record table back under the cap. Lock held.

        Three rules, in order, and each one exists because leaving it out was measurably
        wrong rather than merely less tidy:

        1. **Reap what has aged out first, and count it as expired.** ``load`` is the only
           other reaper and nothing re-reads a finished auction, so without this pass a store
           that has served more than ``capacity`` auctions fills with records whose TTL ran
           out minutes ago and then reports every one of them as an *eviction* — including in
           the 404, which said in as many words "Its 900s TTL had not run out". That is the
           precise falsehood :meth:`absence` exists to remove. Cheap, because the table is in
           oldest-WRITE order and a refreshed record moves to the back: the expired ones are
           exactly the prefix.
        2. **Do not evict a record the merchant is minting against.** See
           :data:`ACCEPTANCE_RESERVATION_NAME` for what that costs when it happens.
        3. **Then the oldest write.** If every candidate is claimed, the oldest goes anyway —
           the cap is the property that must hold, and a store that could be pinned open by
           holding claims would be the unbounded store again with extra steps.
        """
        moment = self._clock()
        while self._records:
            oldest = next(iter(self._records))
            if not self._aged_out(oldest, moment):
                break
            self._forget(oldest, _GONE_EXPIRED)
            self.expired += 1

        while len(self._records) > self.capacity:
            victim = next(
                (key for key in self._records if not self._claimed(key, moment)),
                next(iter(self._records)),
            )
            # Classified by the record's own age, not by which loop dropped it: a record that
            # is over the cap AND past its TTL expired, and calling it an eviction is what put
            # a false sentence in front of the operator in the first place.
            if self._aged_out(victim, moment):
                self._forget(victim, _GONE_EXPIRED)
                self.expired += 1
            else:
                self._forget(victim, _GONE_EVICTED)
                self.evicted += 1

    def load(self, auction_id: str) -> AuctionRecord | None:
        key = str(auction_id)
        with self._lock:
            blob = self._records.get(key)
            if blob is None:
                return None
            # `>=`, not `>`: at exactly `ttl_seconds` the record is gone, which is the
            # boundary `ranking.serving.ShortlistStore.get` already picks and for the same
            # reason — a shortlist must not outlive the auction it describes by an instant.
            if self._clock() - self._written_at.get(key, 0.0) >= self.ttl_seconds:
                self._forget(key, _GONE_EXPIRED)
                self.expired += 1
                return None
        return AuctionRecord.from_json(blob)

    def save(self, record: AuctionRecord) -> None:
        # Serialise on the way in, exactly like the Redis store, so a value that would not
        # survive a round trip fails in the cheap test rather than only under docker. Done
        # OUTSIDE the lock: it is the one expensive step here and it needs no shared state.
        blob = record.to_json()
        key = str(record.auction_id)
        with self._lock:
            # Re-inserting moves the key to the end, so the TTL and the eviction order are
            # both refreshed by a transition — `RedisAuctionStore.save` passes `ex=` on every
            # write, so this is the same rule and not a second one.
            self._records.pop(key, None)
            self._records[key] = blob
            self._written_at[key] = self._clock()
            self._gone.pop(key, None)
            self._evict()

    def reserve(self, auction_id: str, name: str, token: str) -> str | None:
        pair = (str(auction_id), str(name))
        with self._lock:
            held = self._reservations.get(pair)
            if held is not None:
                written_at, holder = held
                if self._clock() - written_at < self.ttl_seconds:
                    return holder
                # Expired exactly as `SET … NX EX`'s key expires, and safe in the same
                # direction the module docstring already argues: a reservation that has
                # lapsed degrades to the checks that were there before it, and a second
                # `accept` also needs a record still sitting in `closed`.
                del self._reservations[pair]
            self._reservations[pair] = (self._clock(), str(token))
            # A backstop, not the working bound — see `RESERVATION_NAMES_PER_AUCTION`. A
            # record's reservations are dropped with the record, so reaching this cap means
            # reservations are outliving records, which is a bug in `_forget`, not traffic.
            #
            # It drops ORPHANS FIRST, and that is a safety property rather than tidiness. The
            # first draft popped the oldest reservation outright, and a reservation dropped
            # while its record is still sitting in `closed` hands the next caller the exit
            # from `closed` that T-158 makes single — measured: `reserve` answered `None` to a
            # second caller where it owed `'accepted'`. A reservation with no record behind it
            # constrains nothing, so it is always the safe one to lose.
            ceiling = self.capacity * RESERVATION_NAMES_PER_AUCTION
            while len(self._reservations) > ceiling:
                orphan = next(
                    (held for held in self._reservations if held[0] not in self._records),
                    next(iter(self._reservations)),
                )
                self._reservations.pop(orphan, None)
            return None

    def release(self, auction_id: str, name: str, token: str) -> None:
        pair = (str(auction_id), str(name))
        with self._lock:
            held = self._reservations.get(pair)
            if held is not None and held[1] == str(token):
                del self._reservations[pair]

    def absence(self, auction_id: str) -> str:
        """Why this store holds no record for ``auction_id`` — named, not guessed.

        The optional hook :func:`absence_of` looks for. A bounded store that answered the
        generic "never created, or its TTL expired" would be telling an operator the wrong
        thing about a 30-second-old auction the cap took away — the same mistake
        ``exchange.ranking.routes.read_shortlist``'s 404 was corrected for, and the reason
        this store remembers ids it has dropped.

        Three answers, and the third is the one that keeps the other two honest:

        * the id is in the tombstone ring as EVICTED — say so, definitively, and say the TTL
          is not implicated;
        * the id is in the ring as EXPIRED — say that, and say eviction is not implicated;
        * the id is in neither, because the ring holds only the last :attr:`capacity` ids this
          store dropped. Then the cause is genuinely unknown, and what the message must not do
          is pick the flattering one. It names eviction FIRST whenever this store has evicted
          anything at all, because a store that has dropped hundreds of records to a cap is a
          store whose missing auction was probably one of them, and it names the TTL only when
          nothing has ever been evicted — in which case the TTL really is the only way a
          record leaves.

        **What is NOT in these sentences: how many auctions this store is holding right now.**
        They reach an unauthenticated caller through ``GET /auctions/{id}``'s 404, so a live
        occupancy figure would be a gauge of other people's traffic that anyone could poll
        with a random id. The cumulative counters stay — a caller who drove the burst already
        knows they drove it, and an operator reading a log needs to see that the cap is being
        hit — but ``len(self._records)`` is deliberately not published.
        """
        key = str(auction_id)
        with self._lock:
            gone = self._gone.get(key)
            evicted, expired = self.evicted, self.expired
        if gone is not None and gone[0] == _GONE_EVICTED:
            return (
                f"auction {key!r} was EVICTED from this process's in-memory auction store to "
                f"make room, not expired: the store holds {self.capacity} auctions and had to "
                f"drop the oldest ({evicted} evicted so far). Its {self.ttl_seconds:g}s TTL "
                f"had not run out — a record that had aged out would have been counted among "
                f"the {expired} expired instead. `POST /auctions` is unauthenticated, so a "
                f"burst of {self.capacity + 1} calls inside one window evicts everything "
                f"created before it; run the exchange on the Redis store "
                f"({ENV_AUCTION_STORE}={AUCTION_STORE_REDIS}) to hold auctions somewhere a "
                f"burst in one process cannot reach"
            )
        if gone is not None:
            return (
                f"auction {key!r} is gone: its {self.ttl_seconds:g}s TTL expired in this "
                f"process's in-memory auction store ({expired} expired so far). It was not "
                f"evicted to make room — the cap is {self.capacity} auctions and this record "
                f"was reaped for age, not displaced"
            )
        if evicted:
            return (
                f"auction {key!r} is not in this process's in-memory auction store, and THE "
                f"CAP IS THE CAUSE TO LOOK AT FIRST: this store's counters read {evicted} "
                f"evicted, {expired} expired, against a cap of {self.capacity}. It remembers "
                f"only the last {self.capacity} ids it dropped and this is not one of them, "
                f"so this id's own eviction cannot be confirmed — but a record that aged out "
                f"would have been counted among the {expired}. `POST /auctions` is "
                f"unauthenticated, so a burst of {self.capacity + 1} calls inside one window "
                f"evicts everything created before it; run the exchange on the Redis store "
                f"({ENV_AUCTION_STORE}={AUCTION_STORE_REDIS}) to hold auctions somewhere a "
                f"burst in one process cannot reach"
            )
        return (
            f"auction {key!r} is not in this process's in-memory auction store — it was never "
            f"created here, or its {self.ttl_seconds:g}s TTL expired more than "
            f"{self.capacity} forgotten auctions ago ({expired} expired so far). It was NOT "
            f"evicted: this store has displaced nothing to stay under its cap of "
            f"{self.capacity} since it started"
        )

    def __len__(self) -> int:
        return len(self._records)


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

    def absence(self, auction_id: str) -> str:
        """Why Redis holds no record at ``auction:{id}`` — the hook :func:`absence_of` wants.

        It names EVICTION nowhere, and that is the answer rather than an omission: this store
        drops nothing of its own accord. There is no capacity here to be reached by a burst of
        unauthenticated ``POST /auctions`` calls — which is the whole reason a deployment
        selects it — so an auction that is gone from Redis expired, or was never written.
        (A server configured with ``maxmemory-policy allkeys-lru`` can still evict; that is the
        operator's own setting on their own server, and it is not this process's cap.)
        """
        return (
            f"auction {auction_id!r} is not in Redis at {self.key(auction_id)} — it was never "
            f"created, or its {self._ttl}s TTL has expired. This store evicts nothing to make "
            f"room, so the record was not displaced by other auctions"
        )


def absence_of(store: Any, auction_id: str) -> str:
    """Why ``store`` holds no record for ``auction_id``, in the store's own words.

    ``absence(auction_id) -> str`` is an OPTIONAL hook on :class:`AuctionStore` rather than a
    member of the protocol, and the reason is that only the store can answer the question
    honestly while only some stores have anything extra to say. A bounded store knows whether
    this id was displaced by a burst or aged out; Redis knows it displaces nothing. A store
    that implements only ``load``/``save``/``reserve``/``release`` — every in-repository test
    double is one — still works, and gets the generic sentence below.

    The generic sentence deliberately does NOT mention eviction. Telling an operator their
    auction "may have been evicted" by a store that evicts nothing is the same species of
    wrong answer as telling them a 30-second-old auction timed out.
    """
    describe = getattr(store, "absence", None)
    if callable(describe):
        note = str(describe(auction_id))
        if note:
            return note
    return (
        f"auction {auction_id!r} is not in the store — it was never created, or its "
        f"{AUCTION_TTL_SECONDS}s TTL has expired"
    )


class AuctionStoreUnavailable(RuntimeError):
    """The store this deployment asked for could not be built.

    Raised rather than degraded to :class:`InMemoryAuctionStore`, and that is the whole point
    of the class. An operator who states ``redis`` has said "these auctions must survive this
    process and must be constrained across every replica"; handing them a process-local store
    because the client would not build satisfies neither, silently, on the money path — the
    exact shape of failure that made the missing seam worth fixing in the first place.
    """


def build_auction_store(word: str, *, env: Mapping[str, str] | None = None) -> AuctionStore:
    """The store named by ``word`` (:data:`AUCTION_STORE_WORDS`). Raises rather than guessing.

    ``redis`` builds a :class:`RedisAuctionStore` over
    ``proxyshop_support.redis_client.worker_redis`` — never a raw ``redis.Redis`` (D39), so the
    ``w{N}:`` key prefix and per-worker logical DB apply to auction records and reservations
    alike.

    **It connects.** ``worker_redis`` asks the server for ``CONFIG GET databases`` before it
    hands back a client, so an unreachable Redis fails HERE, at bind time, rather than on the
    first ``POST /auctions``. That is deliberate and is the opposite choice from
    ``retrieval.roster.graph_sessions_from_env``, which connects to nothing: an exchange whose
    graph is down can still serve every caller who brings a roster in the body, so degrading
    there costs one feature; an exchange whose auction store is down cannot hold an auction at
    all, and the alternative to failing is running the store the operator rejected.
    """
    chosen = str(word).strip().lower()
    if chosen == AUCTION_STORE_MEMORY:
        return InMemoryAuctionStore()
    if chosen != AUCTION_STORE_REDIS:
        raise AuctionStoreUnavailable(
            f"auction store {word!r} is not one of {list(AUCTION_STORE_WORDS)}; an unrecognised "
            f"name has no implementation, and defaulting to {AUCTION_STORE_MEMORY!r} would run "
            f"a process-local store for a deployment that asked for something else"
        )
    # Deferred: `proxyshop_support.redis_client` imports `redis`, and the memory store must
    # stay importable in a process that does not ship the wheel.
    from proxyshop_support.redis_client import worker_redis  # noqa: PLC0415

    source = dict(env) if env is not None else None
    try:
        client = worker_redis(None if source is None else source.get("REDIS_URL"))
    except Exception as exc:  # noqa: BLE001 - every failure here is "the store is unavailable"
        raise AuctionStoreUnavailable(
            f"the {AUCTION_STORE_REDIS!r} auction store could not be built "
            f"({type(exc).__name__}: {exc}). Both names it needs are set by "
            f"`apps/exchange/compose.yaml` — REDIS_URL and PROXYSHOP_WORKER (D38/D39) — so an "
            f"exchange that reaches this line outside compose is missing one of them"
        ) from exc
    return RedisAuctionStore(client)


def auction_store_from_env(env: Mapping[str, str] | None = None) -> AuctionStore | None:
    """The store :data:`ENV_AUCTION_STORE` names, or ``None`` when it names none.

    ``None`` and not "the memory store", so a caller can tell "this environment said nothing"
    apart from "this environment said ``memory``" — the first leaves whatever a deployment
    document stated, the second overrides it. Mirrors ``graph_roster_from_env``'s shape.
    """
    import os  # noqa: PLC0415 — read at call time so a test can drive `env`

    source = dict(os.environ if env is None else env)
    stated = str(source.get(ENV_AUCTION_STORE, "")).strip().lower()
    if not stated:
        return None
    return build_auction_store(stated, env=source)


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
            # The STORE says why, not this class. It used to say "never created, or its TTL
            # expired" for every store and every cause, which was a wrong answer as soon as a
            # store could also evict: an operator handed "your 15-minute TTL ran out" about a
            # 30-second-old auction goes looking at the clock instead of at the cap. This
            # message reaches the caller — `auction/routes.py::read_auction` and
            # `accept/routes.py` both put it in a 404 body — so it is the only account of the
            # eviction anyone outside the process gets.
            raise UnknownAuction(absence_of(self.store, auction_id))
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
