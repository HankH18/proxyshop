"""Auctions: the state machine, its storage, and the pure fan-out collector (R10, T-030).

``collect_bids(roster, responses, now)`` is the public function this package exists for and
the one the frozen suite calls. Everything else here is the machinery around it:

===========================  =====================================================
:mod:`.collect`              ``collect_bids`` — one entry per rostered store (R10)
:mod:`.state`                the CREATED/OPEN/CLOSED/ACCEPTED machine, Redis-backed
:mod:`.ledger`               the ledger port every transition is written through
:mod:`.routes`               ``POST /auctions`` and ``POST /auctions/{id}/close``
===========================  =====================================================

The R12 eligibility gate is deliberately **not** here. It lives one layer up, in
:mod:`apps.exchange.src.orchestration`, which decides who gets asked; ``collect_bids`` is
handed an already-eligible roster and takes no eligibility argument (D54).
"""

from __future__ import annotations

from .collect import FALLBACK_REASONS, BidEntry, collect_bids
from .fanout import (
    DEFAULT_BID_WINDOW_SECONDS,
    ArrivalClock,
    FanOut,
    ask_store,
    parallel_fan_out,
    sequential_fan_out,
)
from .ledger import (
    InMemoryLedgerSink,
    LedgerRecorder,
    LedgerSink,
    UnknownLedgerEventKind,
    build_event,
)
from .state import (
    ACCEPTED,
    AUCTION_KEY_TEMPLATE,
    AUCTION_TTL_SECONDS,
    CLOSED,
    CREATED,
    EXPIRED,
    OPEN,
    TRANSITIONS,
    AuctionRecord,
    AuctionStateMachine,
    AuctionStore,
    IllegalAuctionTransition,
    InMemoryAuctionStore,
    RedisAuctionStore,
    UnknownAuction,
)

__all__ = [
    "ACCEPTED",
    "AUCTION_KEY_TEMPLATE",
    "AUCTION_TTL_SECONDS",
    "CLOSED",
    "CREATED",
    "DEFAULT_BID_WINDOW_SECONDS",
    "EXPIRED",
    "FALLBACK_REASONS",
    "OPEN",
    "TRANSITIONS",
    "ArrivalClock",
    "AuctionRecord",
    "AuctionStateMachine",
    "AuctionStore",
    "BidEntry",
    "FanOut",
    "IllegalAuctionTransition",
    "InMemoryAuctionStore",
    "InMemoryLedgerSink",
    "LedgerRecorder",
    "LedgerSink",
    "RedisAuctionStore",
    "UnknownAuction",
    "UnknownLedgerEventKind",
    "ask_store",
    "build_event",
    "collect_bids",
    "parallel_fan_out",
    "sequential_fan_out",
]
