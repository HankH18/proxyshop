"""Parallel bid solicitation with a **hard** timeout (R10).

Two fan-out strategies, same signature, so the caller chooses without knowing anything else:

* :func:`sequential_fan_out` — asks in roster order, one at a time. The default, because
  order is observable (it is the order the gate above records as ``solicited``) and because
  a deterministic order makes a failure reproducible.
* :func:`parallel_fan_out` — asks everyone at once and stops waiting at the deadline. This
  is the one a live auction uses: N stores answering in 400 ms each must cost 400 ms, not
  400 ms × N.

"Hard" is the word that carries the weight, and it means two separate things here:

**We stop waiting.** ``shutdown(wait=False, cancel_futures=True)`` — deliberately *not* the
``with`` block, whose ``__exit__`` joins every running thread. A store that never answers
would otherwise hold the auction open for as long as it liked, which is precisely the
failure the timeout exists to prevent.

**A late answer is not used.** Every response is stamped with the instant it actually
completed, and ``collect_bids`` rejects any stamped after the deadline. Stopping the wait
without stamping would still let a straggler that landed a microsecond late be counted; the
stamp is what makes the deadline mean something rather than merely being a hint.

The clock is injected (``clock=``) so a test can drive both without sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

__all__ = ["FanOut", "ask_store", "parallel_fan_out", "sequential_fan_out"]

#: A fan-out strategy: ask these stores, return whatever came back in time.
FanOut = Callable[..., list[Mapping[str, Any]]]

#: A thread per store is fine — these are network waits, not computation — but a runaway
#: roster should not spawn a runaway pool.
MAX_FAN_OUT_WORKERS = 32


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
    """Attach the instant this answer actually arrived, unless the store stamped it itself.

    A store's own ``received_at`` is honoured when present — the frozen suite's solicitor
    sets one, and so does a real agent that timestamps its reply. Otherwise the exchange
    stamps arrival itself, which is what makes a slow answer detectably slow.
    """
    if response is None:
        return None
    stamped = dict(response)
    stamped.setdefault("store_id", store.get("store_id"))
    if stamped.get("received_at") is None:
        stamped["received_at"] = finished_at
    return stamped


def sequential_fan_out(
    stores: Sequence[Mapping[str, Any]],
    solicitor: Any,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.time,
) -> list[Mapping[str, Any]]:
    """Ask each store in turn, in roster order. Deterministic; the default.

    ``deadline`` is accepted for interface compatibility and is deliberately **not** used to
    abandon anyone here. A sequential fan-out has nobody waiting in parallel to abandon, and
    — more importantly — the deadline a caller passes may be a *logical* epoch on a frozen
    clock rather than a wall-clock instant. Comparing it against ``time.time()`` would make
    this function refuse to ask anyone at all whenever a test pins the clock to the past.
    The deadline is enforced where it can be enforced correctly: on the ``received_at``
    stamp, by ``collect_bids``.
    """
    responses: list[Mapping[str, Any]] = []
    for store in stores:
        answer = _stamped(ask_store(solicitor, store), store, clock())
        if answer is not None:
            responses.append(answer)
    return responses


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
