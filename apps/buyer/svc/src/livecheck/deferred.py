"""The check happens AFTER the shopper has gone, and this module is why it can.

Hard constraint, stated first because it shaped everything else: **the shortlist must render
at unchanged latency.** A live fetch per candidate is seconds — a robots.txt round trip plus a
page, times four slots, against servers the platform does not control — and a shopper waiting
on four third-party storefronts is a worse product than one that is never checked at all.

So the shopper's path does exactly one thing here: :func:`queue_live_checks` resolves each
slot to a target and appends it to an in-memory queue. No socket, no DNS, no file, no clock
read that anything waits on, and no regular expression over the seller's prose — the pitch is
carried as text and decomposed at drain time, where it is free.

MEASURED on this branch, over the served ``POST /buyer/shortlist/render`` at four slots, by
swapping this function for a no-op inside ONE process and interleaving the two arms (a
between-process comparison measures the machine, not the change — the same route measured 1.03
ms earlier in this session and 2.3 ms later, with six other lanes building):

======================================  ==========  ==========  ==============
arm                                     p50         p95         vs no enqueue
======================================  ==========  ==========  ==============
enqueue replaced by a no-op             2.322 ms    ~3.3 ms     —
4 slots, no store registered (refused)  2.381 ms    ~3.0 ms     +59 us (+2.5%)
4 slots resolved and queued             2.543 ms    ~3.2 ms     +106 us (+4.3%)
======================================  ==========  ==========  ==============

1,600 samples per arm. :func:`queue_live_checks` on its own is 25 us per call at four slots.
For scale: ONE robots.txt round trip against a real storefront is tens of milliseconds before
the page itself, so a synchronous check is not a few percent, it is one to two orders of
magnitude — which is the whole argument below.

**Why a verdict that lands late is still worth having**, which is the part that is easy to
doubt: a contradiction is recorded against the STORE, not against the auction. It lands as a
``claim_verified`` ledger event on ``catalog_claim_accuracy``, which is the trust dimension
``contracts.TrustDimension`` publishes for exactly this — product-fact verification outcomes —
and a store's trust score is an input to every auction it enters afterwards. The shopper who
was mis-quoted is not helped by their own auction; every shopper after them is. That is the
same shape ``exchange.ranking.features.contradicted_claim_events`` gives the 0.15
``contradicted_claim`` penalty inside one auction, one surface and one clock-tick over.

A synchronous check was considered and rejected on those numbers rather than on taste. The
render path is ~2.5 ms; a robots.txt fetch plus a product page against a server the platform
does not control is two orders of magnitude more than the whole request, times the number of
slots, and the check is *optional evidence about a store*. Trading a 100x latency regression
on every shopper for evidence that is equally good one minute later is not a trade this route
may make.

What is deliberately NOT here
-----------------------------
No thread, no timer, no background task started at import. The drain is called — by
``POST /buyer/livecheck/run``, by a worker, or by a test — because a module that starts a
thread when it is imported starts one per test file too, and because a queue drained by
something invisible is a queue nobody can prove was drained. See :mod:`buyer_svc.livecheck
.routes`.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from claim_verification.live_page import (
    AGREES,
    CONTRADICTED,
    LIVE_PAGE_SURFACE,
    NO_VERDICT,
    check_pitch_against_page,
    page_vocabulary,
    read_product_page,
    unreadable_page,
)
from claim_verification.pitch import decompose_pitch, pitch_ref_for

from ..accept._reading import plain, read, text
from .fetching import NoPageFetcher
from .targets import LiveCheckTarget, TargetRefused, targets_for
from .targets import product_pages as _pages
from .targets import registered_domains as _domains

__all__ = [
    "CLAIM_VERIFIED_KIND",
    "LEDGER_SINK_METHODS",
    "LIVE_CHECK_DIMENSION",
    "MAX_PITCH_TEXT_CHARS",
    "MAX_QUEUED_TARGETS",
    "MAX_RECORDED_CHECKS",
    "LiveCheckLedger",
    "LiveCheckQueue",
    "LiveCheckRecord",
    "live_check_ledger",
    "live_check_queue",
    "queue_live_checks",
    "run_live_checks",
    "set_live_check_ledger",
    "set_live_check_queue",
]

_log = logging.getLogger(__name__)

#: The published ledger kind a cross-surface contradiction lands as. It already exists —
#: ``contracts.LedgerEventKind.claim_verified``, payload shape ``(claim_ref, status, dim)`` —
#: so this feature invents no vocabulary anywhere: no new event kind, no new trust dimension,
#: no new status. That is not tidiness. ``packages/contracts`` is another lane's file scope,
#: and a feature that needed a schema change there could not have shipped from here at all.
CLAIM_VERIFIED_KIND = "claim_verified"

#: ``contracts.TrustDimension.catalog_claim_accuracy``, spelled rather than imported for the
#: reason ``feedback.submission`` spells ``"feedback"``: it is a ``StrEnum`` and the wire
#: carries the plain string. D53 puts product-fact verification outcomes here precisely
#: because they have no honest home among the transaction dimensions.
LIVE_CHECK_DIMENSION = "catalog_claim_accuracy"

#: Method names a ledger sink may expose, most specific first. The same tuple
#: ``buyer_svc.feedback.submission`` publishes, because it is the same sink object — one
#: buyer service, one ``app.state.ledger_sink``, one calling convention.
LEDGER_SINK_METHODS: tuple[str, ...] = (
    "append",
    "emit",
    "record",
    "publish",
    "write",
    "log_event",
    "add",
)

#: The most targets the queue holds. A bound on a structure a shopper's traffic grows: four
#: slots per render, and a render is a browser request. Past it the OLDEST target is dropped
#: rather than the newest refused, because the newest is the one somebody is still looking at
#: and the oldest has had its chance — and the drop is COUNTED
#: (:attr:`LiveCheckQueue.dropped`, served on ``POST /buyer/livecheck/run``), because a queue
#: silently shedding work looks exactly like a queue with no work.
#:
#: Worst-case resident size, stated rather than left to be discovered: each target carries the
#: seller's prose up to :data:`MAX_PITCH_TEXT_CHARS`, so 512 x 20,000 characters is ~10 MB if
#: every queued store filled its pitch to the ceiling. Real pitches are a few hundred
#: characters; the ceiling exists because the length is the BIDDER's choice.
MAX_QUEUED_TARGETS = 512

#: The most records kept for read-back. A ring for the same reason ``MAX_RECORDED_AUCTIONS``
#: is one: this is a process-local diagnostic, and the durable copy is the ledger event.
MAX_RECORDED_CHECKS = 256

#: The most pitch characters carried into the queue. ``claim_verification.pitch`` refuses
#: anything over ``MAX_PITCH_CHARS`` (20,000) outright, so text beyond that would be carried
#: across the queue only to be dropped; it is not carried.
MAX_PITCH_TEXT_CHARS = 20_000


# ==============================================================================================
# The queue
# ==============================================================================================


class LiveCheckQueue:
    """Targets waiting to be checked. Bounded, thread-safe, and holding no live resources.

    ``deque(maxlen=...)`` rather than a check-and-refuse, so appending is O(1) and cannot fail
    on the shopper's path. The eviction it implies is the right one: a queue that filled up
    because nothing is draining it should keep the targets a shopper just generated, not the
    ones from an hour ago.
    """

    def __init__(self, maxlen: int = MAX_QUEUED_TARGETS) -> None:
        self._items: deque[LiveCheckTarget] = deque(maxlen=max(1, int(maxlen)))
        self._lock = threading.Lock()
        #: How many targets have been evicted unchecked. Reported by the route, because a
        #: queue that is silently dropping work looks exactly like a queue with no work.
        self.dropped = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def offer(self, targets: Iterable[LiveCheckTarget]) -> int:
        """Append targets. Returns how many were accepted; never raises, never blocks on I/O."""
        accepted = 0
        with self._lock:
            for target in targets:
                if not isinstance(target, LiveCheckTarget):
                    continue
                if len(self._items) == self._items.maxlen:
                    self.dropped += 1
                self._items.append(target)
                accepted += 1
        return accepted

    def drain(self, limit: int | None = None) -> list[LiveCheckTarget]:
        """Take up to ``limit`` targets off the front. Oldest first."""
        taken: list[LiveCheckTarget] = []
        with self._lock:
            ceiling = len(self._items) if limit is None else min(int(limit), len(self._items))
            for _ in range(max(0, ceiling)):
                taken.append(self._items.popleft())
        return taken


# ==============================================================================================
# The record
# ==============================================================================================


@dataclass(frozen=True, slots=True)
class LiveCheckRecord:
    """One completed check: what was compared, both readings, and what it cost.

    ``check`` is :func:`claim_verification.live_page.check_pitch_against_page`'s whole answer,
    kept rather than reduced to a verdict, because a disagreement nobody can re-examine is an
    accusation. It carries the store's asking price AND the page's price, the surface each was
    read from, the tolerance applied and the sentence saying what was compared.
    """

    target: LiveCheckTarget
    check: Any
    checked_at: str
    events: tuple[Mapping[str, Any], ...] = ()
    fetch_reason: str = ""

    @property
    def outcome(self) -> str:
        return str(getattr(self.check, "outcome", NO_VERDICT))

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "target": self.target.to_dict(),
            "outcome": self.outcome,
            "surface": LIVE_PAGE_SURFACE,
            "fetch_reason": self.fetch_reason,
            "check": self.check.to_dict(),
            "ledger_events": [dict(event) for event in self.events],
        }


class LiveCheckLedger:
    """A process-local ring of completed checks, so the route has something to serve.

    Explicitly NOT the durable record. The durable record is the ``claim_verified`` event on
    the trust ledger; this is the readback ``buyer_svc.feedback.submission.FeedbackLedger``
    gives that path, with the same honesty about what it is: one process, bounded, and gone
    when the process is.
    """

    def __init__(self, maxlen: int = MAX_RECORDED_CHECKS) -> None:
        self._items: deque[LiveCheckRecord] = deque(maxlen=max(1, int(maxlen)))
        self._refusals: deque[TargetRefused] = deque(maxlen=max(1, int(maxlen)))
        self._lock = threading.Lock()

    def record(self, entry: LiveCheckRecord) -> None:
        with self._lock:
            self._items.append(entry)

    def refuse(self, entries: Iterable[TargetRefused]) -> None:
        with self._lock:
            for entry in entries:
                self._refusals.append(entry)

    def records(self, auction_id: str = "") -> list[LiveCheckRecord]:
        with self._lock:
            rows = list(self._items)
        if not auction_id:
            return rows
        return [row for row in rows if row.target.auction_id == auction_id]

    def refusals(self, auction_id: str = "") -> list[TargetRefused]:
        with self._lock:
            rows = list(self._refusals)
        if not auction_id:
            return rows
        return [row for row in rows if row.auction_id == auction_id]


# ==============================================================================================
# The module-level seams
# ==============================================================================================
# `POST /buyer/shortlist/render` takes no `Request`, and that is R2's structural guarantee
# rather than an oversight: `buyer_svc.accept.routes` says the handler *cannot reach* an
# exchange client, so looking at a shortlist cannot become accepting one. A queue on
# `app.state` would have to be reached through a `Request`, which would put `app.state` — and
# therefore the exchange client — back in that handler's scope. So the queue is a module-level
# seam, exactly as `buyer_svc.pitch.writer.pitch_writer` is and for the same reason, which
# that module states at length.

_QUEUE_LOCK = threading.Lock()
_QUEUE: LiveCheckQueue | None = None
_LEDGER: LiveCheckLedger | None = None


def live_check_queue() -> LiveCheckQueue:
    """The process's queue, built on first use."""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = LiveCheckQueue()
        return _QUEUE


def set_live_check_queue(queue: LiveCheckQueue | None) -> None:
    """Install a queue (or ``None`` to reset). For composition roots and tests."""
    global _QUEUE
    with _QUEUE_LOCK:
        _QUEUE = queue


def live_check_ledger() -> LiveCheckLedger:
    """The process's record of completed checks, built on first use."""
    global _LEDGER
    with _QUEUE_LOCK:
        if _LEDGER is None:
            _LEDGER = LiveCheckLedger()
        return _LEDGER


def set_live_check_ledger(ledger: LiveCheckLedger | None) -> None:
    global _LEDGER
    with _QUEUE_LOCK:
        _LEDGER = ledger


# ==============================================================================================
# The shopper's path: enqueue only
# ==============================================================================================


def _pitch_text(row: Any) -> str:
    """The store's own message, bounded. Carried as TEXT and decomposed later.

    Decomposition is a regular-expression scan over prose a seller chose the length of, and it
    is the one thing in this whole feature that would have cost the render path measurable
    time. It happens at drain, where it is also *better*: the key aliases resolve against the
    live page's own vocabulary (:func:`claim_verification.live_page.page_vocabulary`), which is
    the same trick ``exchange.ranking.verification`` plays with ``catalog_keys``, and that
    vocabulary does not exist until the page has been read.
    """
    message = text(read(row, "message", ""))
    return message[:MAX_PITCH_TEXT_CHARS] if len(message) <= MAX_PITCH_TEXT_CHARS else ""


def queue_live_checks(
    rows: Any,
    *,
    auction_id: str = "",
    registered_domains: Any = None,
    product_pages: Any = None,
    queue: LiveCheckQueue | None = None,
    ledger: LiveCheckLedger | None = None,
) -> tuple[int, list[TargetRefused]]:
    """Resolve one shortlist's slots to targets and enqueue them. **The whole shopper cost.**

    Returns ``(accepted, refusals)``. Refusals are recorded on the ledger so the route can say
    "no page was checked for this slot, and here is why" — never nothing, because a screen that
    showed a checked slot and an unchecked one identically would be claiming evidence it does
    not have.

    Only slots carrying a store message are queued. That is D55 applied literally: the
    sponsored side is the side with the motive and therefore the side checked adversarially,
    and a scraped shop's pitch was written by the platform out of its own snapshot — checking
    the platform's own prose against the store's page would be grading the wrong party.
    """
    target_queue = queue if queue is not None else live_check_queue()
    record_ledger = ledger if ledger is not None else live_check_ledger()

    entries: list[Any] = []
    if rows is not None and not isinstance(rows, (str, bytes)):
        try:
            entries = list(rows)
        except TypeError:
            entries = []

    sponsored: list[Any] = []
    texts: list[str] = []
    for row in entries:
        message = _pitch_text(row)
        if not message:
            continue
        sponsored.append(row)
        texts.append(message)

    targets, refusals = targets_for(
        sponsored,
        auction_id=auction_id,
        registered_domains=(registered_domains if registered_domains is not None else _domains()),
        product_pages=product_pages if product_pages is not None else _pages(),
    )
    by_slot = {(target.slot, target.bid_ref): index for index, target in enumerate(targets)}
    carried: list[LiveCheckTarget] = []
    for index, row in enumerate(sponsored):
        key = (text(read(row, "slot", "")), text(read(row, "bid_ref", "")))
        position = by_slot.get(key)
        if position is None:
            continue
        target = targets[position]
        commitments = plain(read(row, "commitments", None))
        carried.append(
            LiveCheckTarget(
                store_id=target.store_id,
                product_ref=target.product_ref,
                url=target.url,
                auction_id=target.auction_id,
                bid_ref=target.bid_ref,
                slot=target.slot,
                asking_price=target.asking_price,
                currency=target.currency,
                claims=tuple(row for row in (commitments or ()) if isinstance(row, Mapping)),
                pitch_text=texts[index],
                offered=target.offered,
            )
        )

    accepted = target_queue.offer(carried)
    if refusals:
        record_ledger.refuse(refusals)
    return accepted, refusals


# ==============================================================================================
# The deferred path: fetch, compare, record, publish
# ==============================================================================================


def _sink_call(sink: Any) -> Any:
    """The one callable that records an event on this sink, or ``None``."""
    if sink is None:
        return None
    for name in LEDGER_SINK_METHODS:
        candidate = getattr(sink, name, None)
        if callable(candidate):
            return candidate
    return sink if callable(sink) else None


def _claim_verified_event(
    target: LiveCheckTarget, reading: Any, *, moment: datetime
) -> dict[str, Any]:
    """One contradiction as the published ``claim_verified`` body, and nothing more.

    ``contracts.LEDGER_PAYLOAD_SHAPES["claim_verified"]`` is ``("claim_ref", "status", "dim")``
    and all three are present. The extra keys beside them — the two readings, the surface, what
    was compared — are the evidence, and the payload is an OPEN mapping precisely so a kind can
    carry its own body; what is NOT open is the published three, which is why they are written
    out rather than assembled from whatever happened to be in scope.

    ``surface`` is the key that keeps this honest downstream. A verdict read off the seller's
    own product page is weaker evidence than one read off the exchange's snapshot and much
    weaker than a transaction record, and a row that did not name its surface would let a
    reader treat all three as the same fact.
    """
    claim_ref = getattr(reading, "claim_ref", "") or (
        f"{pitch_ref_for(target.store_id, target.auction_id)}#{getattr(reading, 'key', '')}"
    )
    return {
        "event_id": f"lpc-{uuid.uuid4().hex}",
        "ts": moment.isoformat().replace("+00:00", "Z"),
        "kind": CLAIM_VERIFIED_KIND,
        "auction_id": target.auction_id or None,
        "store_id": target.store_id or None,
        "payload": {
            "claim_ref": str(claim_ref),
            "status": CONTRADICTED,
            "dim": LIVE_CHECK_DIMENSION,
            "surface": LIVE_PAGE_SURFACE,
            "key": str(getattr(reading, "key", "")),
            "pitched_value": getattr(reading, "pitched_value", None),
            "observed_value": getattr(reading, "observed_value", None),
            "page_surface": str(getattr(reading, "surface", "")),
            "compared": str(getattr(reading, "compared", "")),
            "reason": str(getattr(reading, "reason", "")),
            "product_ref": target.product_ref,
            "page_url": target.url,
        },
    }


def run_live_checks(
    *,
    queue: LiveCheckQueue | None = None,
    fetcher: Any = None,
    ledger: LiveCheckLedger | None = None,
    sink: Any = None,
    limit: int | None = None,
    now: Any = None,
) -> list[LiveCheckRecord]:
    """Drain the queue: fetch each page, compare it, record the verdict, publish the cost.

    Runs off the shopper's path — see this module's docstring. Never raises for an ordinary
    outcome: a refused fetch, a 404, a page with no structured data and a page that disagrees
    are all *records*, and only the last of them emits anything.

    Args:
        queue: what to drain; the process seam by default.
        fetcher: a :class:`~buyer_svc.livecheck.fetching.PageFetcher`. The fail-closed
            :class:`~buyer_svc.livecheck.fetching.NoPageFetcher` by default, which reads no
            page and therefore decides nothing.
        ledger: where completed checks are kept for read-back.
        sink: the trust-ledger sink. ``None`` records the verdict locally and publishes
            nothing — a deployment with no ledger wired must still not be able to lose the
            shopper's request, and ``buyer_svc.composition`` already documents that posture.
        limit: how many targets to take this pass.
        now: the clock, injectable so a test can assert a timestamp.

    Returns:
        One :class:`LiveCheckRecord` per target drained, in the order they were drained.
    """
    target_queue = queue if queue is not None else live_check_queue()
    record_ledger = ledger if ledger is not None else live_check_ledger()
    page_fetcher = fetcher if fetcher is not None else NoPageFetcher()
    emit = _sink_call(sink)
    clock = now if callable(now) else (lambda: datetime.now(UTC))

    records: list[LiveCheckRecord] = []
    for target in target_queue.drain(limit):
        moment = clock()
        page = page_fetcher.fetch(target.url)
        if page.ok:
            reading = read_product_page(page.body, url=target.url, encoding=page.encoding)
        else:
            reading = unreadable_page(
                target.url,
                page.reason or f"the page could not be read (status {page.status})",
            )
        prose = target.pitch_text
        claims = list(target.claims)
        if prose:
            claims.extend(
                decompose_pitch(
                    prose,
                    store_id=target.store_id,
                    auction_id=target.auction_id,
                    vocabulary=page_vocabulary(reading),
                )
            )
        check = check_pitch_against_page(
            reading,
            claims=claims,
            asking_price=target.asking_price,
            currency=target.currency,
            store_id=target.store_id,
            product_ref=target.product_ref,
            offered=target.offered,
        )

        events: list[Mapping[str, Any]] = []
        if check.outcome == CONTRADICTED:
            for row in check.contradictions:
                event = _claim_verified_event(target, row, moment=moment)
                events.append(event)
                if emit is None:
                    continue
                try:
                    emit(event)
                except Exception as exc:  # noqa: BLE001 - the audit sink never fails the drain
                    _log.error(
                        "live-page check: the ledger sink raised while recording a "
                        "contradiction for store %s on %s (%s); the verdict stands in this "
                        "process's record and did not reach the chained ledger",
                        target.store_id,
                        target.url,
                        exc,
                    )
            _log.warning(
                "live-page check: %s's own product page contradicts its pitch on %s (%s)",
                target.store_id,
                ", ".join(sorted({row.key for row in check.contradictions})),
                target.url,
            )
        elif check.outcome == AGREES:
            _log.info(
                "live-page check: %s's own product page agrees with its pitch (%s)",
                target.store_id,
                target.url,
            )

        entry = LiveCheckRecord(
            target=target,
            check=check,
            checked_at=moment.isoformat().replace("+00:00", "Z"),
            events=tuple(events),
            fetch_reason=page.reason,
        )
        record_ledger.record(entry)
        records.append(entry)
    return records


def _reset_for_tests() -> None:
    """Drop every process-level seam. Called by this package's test fixtures only."""
    set_live_check_queue(None)
    set_live_check_ledger(None)
