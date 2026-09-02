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
#: waiting on this.
DEFAULT_BID_WINDOW_SECONDS = 3.0


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
    """
    if response is None:
        return None
    stamped = dict(response)

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
    """
    responses: list[Mapping[str, Any]] = []
    lock = threading.Lock()

    def run() -> None:
        for store in stores:
            if deadline is not None and clock() > float(deadline):
                return
            answer = _stamped(ask_store(solicitor, store), store, clock())
            if answer is not None:
                with lock:
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
        # asked and the whole roster falls back to list price.
        return []

    wait({future}, timeout=max(0.0, float(deadline) - clock()))
    with lock:
        # Whatever landed before the window shut. A store still mid-answer is abandoned;
        # anything it produces later appends to a list nobody reads again, and the worker
        # returns itself to the pool when the store finally answers.
        return list(responses)


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

    futures: list[Future[Mapping[str, Any] | None]] = []
    for store in roster:
        if len(futures) >= max(1, int(workers)):
            break
        submitted = executor.submit(ask, store)
        if submitted is None:
            # No free worker anywhere in the process. This store is not asked, and
            # `collect_bids` represents it at its list price — R10's own degradation,
            # rather than a thread created to hold a wait nobody bounded.
            break
        futures.append(submitted)

    if not futures:
        return []

    # One wait for the whole field: returns when everyone has answered OR when the
    # window closes, whichever comes first. Whatever is still pending after this is
    # abandoned — that is the hard part of the hard timeout. The workers are NOT
    # abandoned with it: each returns itself to the shared pool when its store answers.
    timeout = None if deadline is None else max(0.0, deadline - clock())
    wait(set(futures), timeout=timeout)

    responses: list[Mapping[str, Any]] = []
    for future in futures:
        if not future.done() or future.cancelled():
            continue
        if future.exception() is not None:
            continue  # a store that errored simply did not bid; R10 falls it back
        answer = future.result()
        if answer is not None:
            responses.append(answer)
    return responses
