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

**We stop waiting.** ``shutdown(wait=False, cancel_futures=True)`` — deliberately *not* the
``with`` block, whose ``__exit__`` joins every running thread. A store that never answers
would otherwise hold the auction open for as long as it liked, which is precisely the
failure the timeout exists to prevent.

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
    "ArrivalClock",
    "FanOut",
    "ask_store",
    "parallel_fan_out",
    "sequential_fan_out",
]

#: A fan-out strategy: ask these stores, return whatever came back in time. Both shipped
#: strategies accept ``deadline=`` and ``clock=``; a replacement must accept them too,
#: because the arrival stamp is the thing the deadline is enforced on.
FanOut = Callable[..., list[Mapping[str, Any]]]

#: A thread per store is fine — these are network waits, not computation — but a runaway
#: roster should not spawn a runaway pool.
MAX_FAN_OUT_WORKERS = 32

#: How long a bidding window really lasts, in wall-clock seconds, when the caller does not
#: say. Matches ``auction.routes.DEFAULT_BID_TIMEOUT_SECONDS``: a buyer is synchronously
#: waiting on this.
DEFAULT_BID_WINDOW_SECONDS = 3.0


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
    """

    __slots__ = ("_monotonic", "_origin", "_start")

    def __init__(
        self,
        deadline: float,
        *,
        window: float = DEFAULT_BID_WINDOW_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._monotonic = monotonic
        self._origin = monotonic()
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
    deadline the loop itself runs on one worker thread and the caller abandons it at the
    close — the same ``shutdown(wait=False, cancel_futures=True)`` that makes
    :func:`parallel_fan_out`'s timeout hard. This is the property that was missing: until it
    existed, a single hung agent held the default solicitation path — and therefore
    :func:`solicit_bids`, the public boundary carrying R10's guarantee — open for as long as
    it liked, whatever deadline the caller passed.

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

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bid-fanout-seq")
    try:
        future = pool.submit(run)
        wait({future}, timeout=max(0.0, float(deadline) - clock()))
        with lock:
            # Whatever landed before the window shut. A store still mid-answer is abandoned;
            # anything it produces later appends to a list nobody reads again.
            return list(responses)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def parallel_fan_out(
    stores: Sequence[Mapping[str, Any]],
    solicitor: Any,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.time,
    max_workers: int | None = None,
) -> list[Mapping[str, Any]]:
    """Ask every store at once; stop waiting at ``deadline``; stamp what came back."""
    roster = list(stores)
    if not roster:
        return []

    workers = max_workers or min(MAX_FAN_OUT_WORKERS, len(roster))
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bid-fanout")
    futures: list[Future[Mapping[str, Any] | None]] = []
    try:
        # The arrival stamp is taken INSIDE the worker, the moment that store answered —
        # not when we get round to reading the future. Stamping at collection time would
        # date every answer to the deadline itself and reject the whole field.
        def ask(store: Mapping[str, Any]) -> Mapping[str, Any] | None:
            answer = ask_store(solicitor, store)
            return _stamped(answer, store, clock())

        futures = [pool.submit(ask, store) for store in roster]

        # One wait for the whole field: returns when everyone has answered OR when the
        # window closes, whichever comes first. Whatever is still pending after this is
        # abandoned — that is the hard part of the hard timeout.
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
    finally:
        # NOT `with ThreadPoolExecutor(...)`: its __exit__ joins every running thread, which
        # would hand a silent store the power to hold the auction open indefinitely.
        pool.shutdown(wait=False, cancel_futures=True)
