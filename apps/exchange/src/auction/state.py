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
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .ledger import LedgerRecorder, LedgerSink

__all__ = [
    "ACCEPTED",
    "AUCTION_KEY_TEMPLATE",
    "AUCTION_TTL_SECONDS",
    "CLOSED",
    "CREATED",
    "EXPIRED",
    "OPEN",
    "TRANSITIONS",
    "AuctionRecord",
    "AuctionStateMachine",
    "AuctionStore",
    "IllegalAuctionTransition",
    "InMemoryAuctionStore",
    "RedisAuctionStore",
    "UnknownAuction",
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

#: The ledger kind each transition writes. `EXPIRED` writes `auction_closed` — an auction
#: that timed out did close; the payload's `reason` is what distinguishes the two.
_TRANSITION_KIND: Mapping[str, str] = {
    OPEN: "auction_opened",
    CLOSED: "auction_closed",
    EXPIRED: "auction_closed",
    ACCEPTED: "accepted",
}

#: DESIGN: `auction:{id}` in Redis, TTL 15 minutes.
AUCTION_KEY_TEMPLATE = "auction:{auction_id}"
AUCTION_TTL_SECONDS = 15 * 60


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
    """Where auction records live for their fifteen minutes."""

    def load(self, auction_id: str) -> AuctionRecord | None: ...

    def save(self, record: AuctionRecord) -> None: ...


class InMemoryAuctionStore:
    """Process-local store. The default, and what every non-docker test drives."""

    def __init__(self) -> None:
        self._records: dict[str, str] = {}

    def load(self, auction_id: str) -> AuctionRecord | None:
        blob = self._records.get(auction_id)
        return AuctionRecord.from_json(blob) if blob is not None else None

    def save(self, record: AuctionRecord) -> None:
        # Serialise on the way in, exactly like the Redis store, so a value that would not
        # survive a round trip fails in the cheap test rather than only under docker.
        self._records[record.auction_id] = record.to_json()


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
        blob = self._client.get(self.key(auction_id))
        if blob is None:
            return None
        if isinstance(blob, bytes):
            blob = blob.decode("utf-8")
        return AuctionRecord.from_json(blob)

    def save(self, record: AuctionRecord) -> None:
        self._client.set(self.key(record.auction_id), record.to_json(), ex=self._ttl)


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
        return record

    def open(self, auction_id: str, *, now: float | None = None) -> AuctionRecord:
        record = self._transition(auction_id, OPEN, now=now)
        return record

    def close(
        self, auction_id: str, *, now: float | None = None, reason: str = "deadline"
    ) -> AuctionRecord:
        return self._transition(auction_id, CLOSED, now=now, payload={"reason": reason})

    def expire(self, auction_id: str, *, now: float | None = None) -> AuctionRecord:
        return self._transition(auction_id, EXPIRED, now=now, payload={"reason": "expired"})

    def accept(self, auction_id: str, bid_ref: str, *, now: float | None = None) -> AuctionRecord:
        return self._transition(
            auction_id, ACCEPTED, now=now, payload={"bid_ref": bid_ref}, bid_ref=bid_ref
        )

    def _transition(
        self,
        auction_id: str,
        target: str,
        *,
        now: float | None = None,
        payload: Mapping[str, Any] | None = None,
        bid_ref: str | None = None,
    ) -> AuctionRecord:
        record = self.get(auction_id)
        allowed = TRANSITIONS.get(record.state, frozenset())
        if target not in allowed:
            raise IllegalAuctionTransition(
                f"auction {auction_id!r} is {record.state!r}; {target!r} is not a legal move "
                f"(legal: {sorted(allowed) or 'none — terminal state'})"
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

        self.ledger.record(
            _TRANSITION_KIND[target],
            auction_id=auction_id,
            payload={
                "state": target,
                "intent_id": record.intent_id,
                "cluster_id": record.cluster_id,
                **dict(payload or {}),
            },
        )
        return record
