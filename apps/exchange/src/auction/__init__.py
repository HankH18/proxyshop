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

from .collect import (
    FALLBACK_REASONS,
    ILLEGIBLE_OFFER_REASON,
    MALFORMED_RESPONSE_REASONS,
    UNRECONCILABLE_PRICE_REASON,
    BidEntry,
    collect_bids,
)
from .fanout import (
    DEFAULT_BID_WINDOW_SECONDS,
    MAX_FAN_OUT_WORKERS,
    ArrivalClock,
    BoundedFanOutPool,
    FanOut,
    ask_store,
    fan_out_pool,
    parallel_fan_out,
    sequential_fan_out,
)
from .ledger import (
    InMemoryLedgerSink,
    LedgerRecorder,
    LedgerSink,
    MalformedLedgerPayload,
    UnknownLedgerEventKind,
    build_event,
    build_published_event,
    published_body,
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
    "ILLEGIBLE_OFFER_REASON",
    "MALFORMED_RESPONSE_REASONS",
    "MAX_FAN_OUT_WORKERS",
    "OPEN",
    "TRANSITIONS",
    "UNRECONCILABLE_PRICE_REASON",
    "ArrivalClock",
    "AuctionRecord",
    "AuctionStateMachine",
    "AuctionStore",
    "BidEntry",
    "BoundedFanOutPool",
    "FanOut",
    "IllegalAuctionTransition",
    "InMemoryAuctionStore",
    "InMemoryLedgerSink",
    "LedgerRecorder",
    "LedgerSink",
    "MalformedLedgerPayload",
    "RedisAuctionStore",
    "UnknownAuction",
    "UnknownLedgerEventKind",
    "ask_store",
    "build_event",
    "build_published_event",
    "collect_bids",
    "fan_out_pool",
    "parallel_fan_out",
    "published_body",
    "sequential_fan_out",
]
