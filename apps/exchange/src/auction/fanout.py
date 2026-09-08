"""Parallel bid solicitation with a **hard** timeout (R10).

Two fan-out strategies, same signature, so the caller chooses without knowing anything else:

* :func:`sequential_fan_out` — asks in roster order, one at a time, on a worker the caller
  abandons at the deadline. The default, because the ask order is observable (it is the
  order the gate above records as ``solicited``) and a deterministic order makes a failure
  reproducible; N-way dispatch cannot promise that order.
* :func:`parallel_fan_out` — asks everyone at once and stops waiting at the deadline. This
  is the one a live auction uses: N stores answering in 400 ms each must cost 400 ms, not
  400 ms × N.

Both enforce the timeout. They differ in latency, not in whether a store can hold the
auction open — sequential *used* to differ in exactly that, and the gap sat under
:func:`solicit_bids`, the public boundary that carries R10's guarantee.

"Hard" is the word that carries the weight, and it means two separate things here:

**We stop waiting.** The wait ends at the deadline, and a store still mid-answer is left
behind. Deliberately *not* a ``with ThreadPoolExecutor(...)`` block, whose ``__exit__``
joins every running thread — a store that never answers would otherwise hold the auction
open for as long as it liked, which is precisely the failure the timeout exists to prevent.

**Abandoning a worker is not the same as being allowed to create one per request.** This
used to buy the hard timeout by building a fresh ``ThreadPoolExecutor`` per call and then
``shutdown(wait=False, cancel_futures=True)``-ing it. The wait ended, but the *thread* did
not: it was still blocked inside a store that never answered, it was not a daemon, and the
next request built another one. One slow store therefore leaked one non-daemon thread per
request, without bound, until the process died of it. :class:`BoundedFanOutPool` is the
fix: **one process-wide pool with a hard worker ceiling and non-blocking admission.** A
store that cannot get a worker is simply not asked, and falls back to list price exactly
like a store that stayed silent (R10) — a bounded, visible degradation instead of an
unbounded leak. See that class for the one residual risk and what actually closes it.

**A late answer is not used.** Every response is stamped with the instant it actually
completed, and ``collect_bids`` rejects any stamped after the deadline. Stopping the wait
without stamping would still let a straggler that landed a microsecond late be counted; the
stamp is what makes the deadline mean something rather than merely being a hint.

**A late store is not a silent store, and this module is the only thing that knows.** The
wait ends and a store's future is left pending; abandoning it used to mean abandoning the
*fact* as well, so ``collect_bids`` saw nothing for that store and recorded ``no_response`` —
the same word it records for a container that is switched off. Measured on a live hosted
deployment with a real model writing each pitch: four agents logged ``200 OK`` for every
solicitation, 24 samples over the wire ran 1.97 s – 4.73 s against a 3.0 s window, and the
auction reported ``hosted bids=0  fallback reasons=['no_response']``. Nothing was broken and
every diagnostic said the agents were not running. So both strategies now **mint a response
for a store that produced none** — ``{store_id, exchange_timed_out}`` for a future still
pending at the close, ``{store_id, exchange_not_asked}`` for a store no worker was free to
dial — and ``collect_bids`` names them apart (``response_timed_out`` /
``fan_out_capacity_exhausted``). The fallback is unchanged in every case: R10 still puts the
store on the shortlist at its list price. What changed is that the *reason* is true.

**The exchange stamps, never the store.** ``received_at`` is the value the deadline is
enforced on, so a bidder that could set it would be setting its own deadline: answer
whenever you like, claim you answered a second before the close, and ``collect_bids`` counts
you. :func:`_stamped` therefore *overwrites* whatever the payload carried; a store's own
claim is kept, for audit only, under ``store_reported_received_at``. The same reasoning
applies to ``store_id``: an answer is attributed to the store the exchange **asked**, never
to the store the answer names, or one bidder can post a ruinous bid under a rival's name and
displace the rival's real one.

The clock is injected (``clock=``) so a test can drive both without sleeping. Determinism is
what :class:`ArrivalClock` is for: it expresses real elapsed time *in the deadline's own
frame*, so a frozen/logical deadline stays usable while a store that really did take too
long is still detectably late.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

# The names of the two fields this module writes on a response it mints itself. They are
# declared next to the function that READS them (`collect._unusable_because`), exactly as
# `REFUSAL_FIELD` is declared there and written by `composition`'s solicitor — one spelling,
# owned by the reader, so a writer cannot drift away from it silently.
from .collect import NO_AGENT_FIELD, NOT_ASKED_FIELD, TIMED_OUT_FIELD

__all__ = [
    "DEFAULT_BID_WINDOW_SECONDS",
    "MAX_FAN_OUT_WORKERS",
    "ArrivalClock",
    "BoundedFanOutPool",
    "FanOut",
    "ask_store",
    "fan_out_pool",
    "parallel_fan_out",
    "sequential_fan_out",
]

#: A fan-out strategy: ask these stores, return whatever came back in time. Both shipped
#: strategies accept ``deadline=`` and ``clock=``; a replacement must accept them too,
#: because the arrival stamp is the thing the deadline is enforced on.
FanOut = Callable[..., list[Mapping[str, Any]]]

#: A thread per store is fine — these are network waits, not computation — but a runaway
#: roster should not spawn a runaway pool, and neither should a runaway *request rate*.
#: This is a ceiling on outbound solicitation threads for the whole process, not per call.
MAX_FAN_OUT_WORKERS = 32

#: How long a bidding window really lasts, in wall-clock seconds, when the caller does not
#: say. Matches ``auction.routes.DEFAULT_BID_TIMEOUT_SECONDS``: a buyer is synchronously
#: waiting on this. This is its mirror, read by ``orchestration.solicitation`` for a caller
#: that states no window of its own; the measurement behind the number lives beside that
#: constant and is not restated here, because two copies of a measurement drift.
#:
#: One thing worth carrying across, since this module is where it is enforced: 5.0 guarantees
#: the **bid**, not the store's model-written prose. A store reserves 0.35 s of the window
#: before handing the remainder to its model, so at 5.0 s the pitch budget is ~4.64 s against
#: a measured 4.73 s p100 — a store on that tail ships a deterministic fallback pitch and
#: still wins its bid, which is the repair. The offer itself, which is all this fan-out has to
#: get back for the auction to be a market, costs 12.7 ms.
DEFAULT_BID_WINDOW_SECONDS = 5.0


class BoundedFanOutPool:
    """One process-wide pool of reusable workers, with **non-blocking** admission.

    Three properties, and each one is load-bearing:

    **Reused, so the steady state creates no threads.** The pool is built once and lives for
    the process. A hundred auctions in a row against responsive stores run on the same
    handful of workers; nothing is created and nothing is abandoned.

    **Hard-bounded, so a hung store cannot be turned into a thread leak.** At most
    ``max_workers`` threads exist, ever, whatever the request rate. That is the fix for the
    real defect: the previous design abandoned a *fresh* executor on every request, and
    every worker still blocked inside a silent store survived the response it was serving.

    **Admission never blocks, so a full pool degrades instead of queueing.** ``submit``
    returns ``None`` when no worker is free rather than parking the request behind one. A
    queue would be worse than the leak it replaced: the buyer waiting on this request would
    wait for *another* auction's straggler, and the queued ask would eventually fire at a
    store long after the auction it belonged to had closed. A store that could not be asked
    is represented at list price by ``collect_bids``, which is R10's own degradation path.

    The residual risk, stated plainly: a worker blocked in a store that never answers *at
    all* never comes back, so ``max_workers`` such stores permanently reduce capacity to
    zero and every auction degrades to catalog prices. A thread cannot be killed from
    outside in CPython, so the only real fix is upstream — **the outbound bid-request client
    must carry its own connect/read timeout**, and then every worker is returned within it.
    This pool bounds the damage; the socket timeout is what prevents it.
    """

    __slots__ = ("_pool", "_max_workers", "_slots")

    def __init__(
        self,
        max_workers: int = MAX_FAN_OUT_WORKERS,
        *,
        thread_name_prefix: str = "bid-fanout",
    ) -> None:
        workers = max(1, int(max_workers))
        self._max_workers = workers
        self._slots = threading.Semaphore(workers)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_name_prefix)

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def submit(self, fn: Callable[..., Any], /, *args: Any) -> Future[Any] | None:
        """Run ``fn(*args)`` on a free worker, or return ``None`` if there is none.

        The slot is released by the worker itself, in a ``finally``, so a task that raises
        (or one whose future the caller abandoned at the deadline) still gives its capacity
        back the moment the store actually answers.
        """
        if not self._slots.acquire(blocking=False):
            return None

        def task() -> Any:
            try:
                return fn(*args)
            finally:
                self._slots.release()

        try:
            return self._pool.submit(task)
        except RuntimeError:  # the pool was shut down between the acquire and the submit
            self._slots.release()
            return None

    def shutdown(self) -> None:
        """Stop accepting work. Never joins: a hung worker must not block the caller."""
        self._pool.shutdown(wait=False, cancel_futures=True)


_POOL_LOCK = threading.Lock()
_DEFAULT_POOL: BoundedFanOutPool | None = None


def fan_out_pool() -> BoundedFanOutPool:
    """The process-wide fan-out pool, built on first use.

    Lazily, not at import: importing this module must not cost threads in a process that
    never runs an auction (a CLI, a migration, the lint).
    """
    global _DEFAULT_POOL
    with _POOL_LOCK:
        if _DEFAULT_POOL is None:
            _DEFAULT_POOL = BoundedFanOutPool()
        return _DEFAULT_POOL


class ArrivalClock:
    """The exchange's own arrival clock, reported in the deadline's frame.

    The problem this solves: ``deadline`` may be a *logical* epoch a test pinned
    (``1_700_000_000.0``), while the only honest measure of "did this store take too long"
    is real elapsed time. Reading ``time.time()`` and comparing it to a logical deadline
    makes every answer late; trusting the store's own stamp makes none of them late. Neither
    is acceptable.

    So: the window is a real duration, the deadline is the logical instant it ends, and this
    clock reports ``deadline - window + elapsed``. An answer is on time exactly when it took
    less than ``window`` real seconds — true on a frozen clock and true in production, with
    nothing the store sends contributing to the answer.

    ``monotonic`` is injected only so a test can drive elapsed time without sleeping;
    :func:`time.monotonic` is used everywhere else because it cannot be dragged backwards by
    an NTP correction mid-auction.

    ``started_at`` is what makes the hard timeout *hard*, and leaving it out was a real
    defect. The origin used to be ``monotonic()`` at construction, so the clock read
    ``deadline - window`` at the instant it was built — meaning the fan-out was handed a
    **full** ``window`` of real seconds no matter how much of the auction's own deadline had
    already been spent getting there. Opening the auction, writing two ledger events and
    reading eligibility for every rostered store all happen first, and every one of them is
    I/O; a slow eligibility backend alone could double the request. R10's timeout was
    therefore not a bound on the request at all. Anchoring the clock to the monotonic
    reading taken **when the deadline was computed** makes ``deadline - clock()`` the
    *remaining* window, which is the number the fan-out actually waits on.
    """

    __slots__ = ("_monotonic", "_origin", "_start")

    def __init__(
        self,
        deadline: float,
        *,
        window: float = DEFAULT_BID_WINDOW_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        started_at: float | None = None,
    ) -> None:
        self._monotonic = monotonic
        # Default to "the window starts now" so a caller that has nothing else to say gets
        # the old, self-anchored behaviour; a caller that knows when its deadline was struck
        # (the route does) passes that reading and the window is measured from it.
        self._origin = monotonic() if started_at is None else float(started_at)
        self._start = float(deadline) - max(0.0, float(window))

    def __call__(self) -> float:
        return self._start + (self._monotonic() - self._origin)


def ask_store(solicitor: Any, store: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Ask one store for a bid, tolerating either call style on the client.

    The frozen suite's stand-in exposes ``solicit(store)`` with ``__call__`` aliased to it;
    a real ``POST /v1/bid-requests`` client may expose either. A non-mapping answer (a
    decline, ``None``, a timeout sentinel) is normalised to ``None`` — no bid.
    """
    ask = getattr(solicitor, "solicit", None)
    if not callable(ask):
        if not callable(solicitor):
            raise TypeError(
                f"solicitor {solicitor!r} exposes neither solicit(store) nor __call__(store)"
            )
        ask = solicitor
    response = ask(store)
    return response if isinstance(response, Mapping) else None


def _stamped(
    response: Mapping[str, Any] | None,
    store: Mapping[str, Any],
    finished_at: float,
) -> Mapping[str, Any] | None:
    """Stamp arrival and attribution **authoritatively**. Nothing here is taken on trust.

    Two fields decide who a bid belongs to and whether it beat the deadline, and a bidder
    controls neither:

    ``received_at``
        Overwritten with the exchange's own clock reading, always. Honouring a store's own
        stamp — which this used to do whenever one was present — hands the store its own
        deadline: ``collect_bids`` enforces the close on this value, so a bidder that stalls
        past the window and then writes ``deadline - 1`` on its reply is counted as on time
        and wins with a price it chose after the window shut. The store's claim is preserved
        under ``store_reported_received_at`` so an operator can still see the discrepancy;
        nothing reads it for a decision.

    ``store_id``
        Set to the store the exchange **asked**, not the one the reply names. Taking the
        reply's word for it (``setdefault``) lets one bidder answer under a rival's name:
        ``collect_bids`` keys responses by ``store_id`` and keeps the first on-time one, so
        an early-in-roster bidder could post 500.00 as its rival and bury the rival's real
        50.00 bid — winning at its own list price an auction it had already lost.

    The bid body is re-attributed for the same reason: everything downstream that reads
    ``bid["store_id"]`` (the checkout request, the ledger) must name the store that was
    actually asked.

    :data:`~.collect.TIMED_OUT_FIELD`, :data:`~.collect.NOT_ASKED_FIELD` and
    :data:`~.collect.NO_AGENT_FIELD` — the first two this module's own account of a run, the
    third the solicitation gate's account of this deployment, and none of them a store's — are
    **deleted**
    rather than overwritten, and that is the same rule pointed at a field a store has no
    honest use for at all. They are the EXCHANGE's account of stores it heard nothing
    from — two written here and the third by
    :func:`~exchange.orchestration.solicitation.solicit_bids`, which is why this function
    strips a field it never writes — so a store that sends one is describing a run it did not
    observe: ``exchange_timed_out`` on
    an otherwise good reply would talk the collector out of the bid the store just made, and
    on a malformed one it would relabel the store's own serializer bug as the exchange's
    clock. Nothing is preserved under a ``store_reported_`` key because, unlike an arrival
    stamp or a store id, there is no version of this claim an auditor could want: the value
    is not evidence about the store, it is a word about us.
    """
    if response is None:
        return None
    stamped = dict(response)
    stamped.pop(TIMED_OUT_FIELD, None)
    stamped.pop(NOT_ASKED_FIELD, None)
    # And :data:`~.collect.NO_AGENT_FIELD`, on the same argument: it is this exchange's
    # statement that it holds no endpoint for a store, so a store that could set it would be
    # excusing its own silence with a fact about our deployment. That it can only arrive here
    # from a store — the gate mints it for stores this function is never called for — is
    # exactly why it is stripped rather than trusted.
    stamped.pop(NO_AGENT_FIELD, None)

    claimed = stamped.get("received_at")
    if claimed is not None and claimed != finished_at:
        stamped["store_reported_received_at"] = claimed
    stamped["received_at"] = finished_at

    store_id = store.get("store_id")
    if store_id is not None:
        if stamped.get("store_id") != store_id:
            stamped["store_reported_store_id"] = stamped.get("store_id")
        stamped["store_id"] = store_id
        bid = stamped.get("bid")
        if isinstance(bid, Mapping):
            attributed = dict(bid)
            attributed["store_id"] = store_id
            stamped["bid"] = attributed
    return stamped


def _minted(store: Mapping[str, Any], field: str) -> Mapping[str, Any] | None:
    """The exchange's own record of a store it got no answer from, or ``None``.

    Built from the roster row the exchange **asked**, and from nothing else: there is no
    response to copy, so there is nothing a bidder could have contributed. That is the whole
    security argument for the two marker fields, and it is why they are minted here rather
    than defaulted by the collector — the collector cannot tell an absence apart from a store
    it was never told about.

    ``None`` for a roster row carrying no ``store_id``: :func:`collect_bids` keys responses by
    that field and drops any response without one, so an anonymous marker would be a mapping
    built to be discarded. The row cannot be attributed, so nothing is claimed about it.
    """
    store_id = store.get("store_id")
    if store_id is None:
        return None
    return {"store_id": store_id, field: True}


def _minted_for(stores: Sequence[Mapping[str, Any]], field: str) -> list[Mapping[str, Any]]:
    """:func:`_minted` over a run of roster rows, skipping the unattributable ones."""
    minted = (_minted(store, field) for store in stores)
    return [record for record in minted if record is not None]


def sequential_fan_out(
    stores: Sequence[Mapping[str, Any]],
    solicitor: Any,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.time,
    pool: BoundedFanOutPool | None = None,
) -> list[Mapping[str, Any]]:
    """Ask each store in turn, in roster order. Deterministic; the default.

    ``deadline`` **is** enforced, in two places:

    * nobody is asked once the window has closed — the remaining stores fall back to list
      price, exactly as if they had stayed silent, rather than the auction running on past
      its own close because there were more names on the roster;
    * every answer is stamped by :func:`_stamped` with ``clock()``, and ``collect_bids``
      drops anything stamped after the deadline.

    The old objection to comparing against the deadline — that a caller may pass a *logical*
    epoch a test pinned, so a wall-clock comparison would refuse to ask anyone — is answered
    by :class:`ArrivalClock` rather than by skipping the check: ``clock`` reports elapsed
    time in the deadline's own frame, so the comparison is meaningful on a frozen clock and
    in production alike. ``clock`` defaulting to :func:`time.time` is right only for a caller
    that passes a wall-clock deadline; :func:`solicit_bids` injects the arrival clock.

    A store that answers *nothing* cannot be interrupted from inside the loop, so with a
    deadline the loop itself runs on one worker borrowed from :func:`fan_out_pool` and the
    caller abandons it at the close — the same bounded, shared pool that makes
    :func:`parallel_fan_out`'s timeout hard. This is the property that was missing: until it
    existed, a single hung agent held the default solicitation path — and therefore
    :func:`solicit_bids`, the public boundary carrying R10's guarantee — open for as long as
    it liked, whatever deadline the caller passed.

    The worker is *borrowed*, never created here. Creating one per call is what leaked a
    thread per request under a silent store; when the pool has nothing free this returns no
    responses at all and every store on the roster falls back to list price (R10).

    One worker, not one per store, because the ask *order* is observable: it is the order
    :func:`solicit_bids` reports as ``solicited``, and dispatching N stores across N threads
    makes which one calls the solicitor first a race. This buys the hard timeout while
    keeping the order deterministic; it does **not** buy R10's latency property (N stores at
    400 ms still cost N × 400 ms). A live auction passes :func:`parallel_fan_out` for that —
    the route does.

    **What it reports about the stores it got nothing from**, and the one case it deliberately
    stays quiet about. Two facts here are unambiguous and both are minted:

    * the store the loop was *inside* when the window shut — asked, still answering, and
      exactly the case :data:`~.collect.RESPONSE_TIMED_OUT_REASON` names; and
    * every store on the roster when no worker was free at all — nobody was asked, and
      :data:`~.collect.FAN_OUT_CAPACITY_REASON` says so instead of blaming N healthy stores.

    The **tail the loop never reached** is left as ``no_response``, on purpose. A single
    worker walks the roster in order, so the moment the wait ends the loop is either inside a
    store or between two of them, and between two of them "the next store was never asked"
    and "the next store is being asked right now" are the same observable state. Minting a
    marker on that guess would put a fabricated verdict where an honest absence was, which is
    the defect this repair exists to end rather than a smaller version of it — and this is not
    the live path: :func:`parallel_fan_out`, which submits every store up front and therefore
    knows exactly which ones it never submitted, is what the route passes.
    """
    responses: list[Mapping[str, Any]] = []
    lock = threading.Lock()
    # The store the worker is currently blocked inside, or ``None`` between asks. Read under
    # the lock after the wait: it is the one thing this strategy knows for certain about a
    # store that produced nothing.
    pending: Mapping[str, Any] | None = None

    def run() -> None:
        nonlocal pending
        for store in stores:
            if deadline is not None and clock() > float(deadline):
                return
            with lock:
                pending = store
            answer = _stamped(ask_store(solicitor, store), store, clock())
            with lock:
                pending = None
                if answer is not None:
                    responses.append(answer)

    if deadline is None:
        # No window to enforce, so no thread to pay for: this is the shape a unit test
        # driving the loop with a pinned clock gets.
        run()
        return responses

    future = (pool if pool is not None else fan_out_pool()).submit(run)
    if future is None:
        # Every worker is held by a store that has not answered. Asking inline would hand
        # this request the very unbounded wait the pool exists to prevent, so nobody is
        # asked and the whole roster falls back to list price — now saying so, rather than
        # reporting an exhausted pool as N stores that did not pick up.
        return _minted_for(stores, NOT_ASKED_FIELD)

    wait({future}, timeout=max(0.0, float(deadline) - clock()))
    with lock:
        # Whatever landed before the window shut. A store still mid-answer is abandoned;
        # anything it produces later appends to a list nobody reads again, and the worker
        # returns itself to the pool when the store finally answers. The abandoned store is
        # named, because "we walked away from your reply" is a different fact from "you never
        # sent one" and only this function is in a position to tell them apart.
        collected = list(responses)
        abandoned = None if pending is None else _minted(pending, TIMED_OUT_FIELD)
    if abandoned is not None:
        collected.append(abandoned)
    return collected


def parallel_fan_out(
    stores: Sequence[Mapping[str, Any]],
    solicitor: Any,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.time,
    max_workers: int | None = None,
    pool: BoundedFanOutPool | None = None,
) -> list[Mapping[str, Any]]:
    """Ask every store at once; stop waiting at ``deadline``; stamp what came back.

    ``max_workers`` caps how many of these stores are asked *concurrently in this call*; the
    process-wide ceiling is :attr:`BoundedFanOutPool.max_workers` and is what actually
    bounds thread count. ``pool`` is a test seam — production always uses the shared pool
    from :func:`fan_out_pool`, because a per-request pool is exactly the leak this replaced.

    **Every store on the roster is accounted for in the return value**, which is the part that
    was missing and is the whole reason the live market could go 100% list-price without
    anyone noticing. This function submits the whole roster up front, so at the close it knows
    precisely which stores it never submitted (no worker was free, or the per-call cap ran
    out) and which ones it submitted and then abandoned mid-answer. Both used to leave no
    trace at all, and ``collect_bids`` defaulted the store to ``no_response`` — the word for a
    store that was asked and said nothing. Now each gets a mapping the exchange minted for it,
    naming which of the three actually happened. The store still falls back to its list price;
    R10 is untouched. See the module docstring for the hosted measurement.
    """
    roster = list(stores)
    if not roster:
        return []

    workers = max_workers if max_workers is not None else MAX_FAN_OUT_WORKERS
    executor = pool if pool is not None else fan_out_pool()

    # The arrival stamp is taken INSIDE the worker, the moment that store answered —
    # not when we get round to reading the future. Stamping at collection time would
    # date every answer to the deadline itself and reject the whole field.
    def ask(store: Mapping[str, Any]) -> Mapping[str, Any] | None:
        answer = ask_store(solicitor, store)
        return _stamped(answer, store, clock())

    asked: list[tuple[Mapping[str, Any], Future[Mapping[str, Any] | None]]] = []
    unasked: list[Mapping[str, Any]] = []
    for position, store in enumerate(roster):
        if len(asked) >= max(1, int(workers)):
            unasked = roster[position:]
            break
        submitted = executor.submit(ask, store)
        if submitted is None:
            # No free worker anywhere in the process. This store is not asked, and
            # `collect_bids` represents it at its list price — R10's own degradation,
            # rather than a thread created to hold a wait nobody bounded. The whole
            # remaining roster goes with it: admission is non-blocking and the pool does
            # not refill inside this loop, so trying the rest would be N failed acquires.
            unasked = roster[position:]
            break
        asked.append((store, submitted))

    # One wait for the whole field: returns when everyone has answered OR when the
    # window closes, whichever comes first. Whatever is still pending after this is
    # abandoned — that is the hard part of the hard timeout. The workers are NOT
    # abandoned with it: each returns itself to the shared pool when its store answers.
    if asked:
        timeout = None if deadline is None else max(0.0, deadline - clock())
        wait({future for _, future in asked}, timeout=timeout)

    responses: list[Mapping[str, Any]] = []
    for store, future in asked:
        if future.cancelled():
            # The pool was shut down under this call, so the task never ran. That is the
            # exchange having no capacity, not a store that took too long — and it is worth
            # the extra branch precisely because the two used to be one word.
            minted = _minted(store, NOT_ASKED_FIELD)
            if minted is not None:
                responses.append(minted)
            continue
        if not future.done():
            # THE CASE THIS REPAIR EXISTS FOR: asked, still answering, window shut. The
            # future is abandoned exactly as before — nothing here waits on it — but the
            # exchange now writes down that it was in flight rather than letting the store
            # be reported as silent.
            minted = _minted(store, TIMED_OUT_FIELD)
            if minted is not None:
                responses.append(minted)
            continue
        if future.exception() is not None:
            continue  # a store that errored simply did not bid; R10 falls it back
        answer = future.result()
        if answer is not None:
            responses.append(answer)
    responses.extend(_minted_for(unasked, NOT_ASKED_FIELD))
    return responses
