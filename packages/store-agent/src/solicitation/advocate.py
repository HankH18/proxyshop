"""The ONE `AgentRunner` this process advocates with, and the channel its answer leaves by.

A hosted agent process is one store's advocate — :mod:`store_agent.solicitation.serving` says
so and the exchange relies on it, choosing which agent to solicit by choosing which URL to
POST to. The same fact decides everything here: there is exactly one runner per application,
it is built from the same store context both served doors read, and it is the object that
decides whether an answer leaves the building.

WHY THE RUNNER IS CACHED ON ``app.state`` AND NOT REBUILT PER REQUEST
----------------------------------------------------------------------
``AgentRunner`` accumulates. :attr:`~store_agent.modes.AgentRunner.trust_posture` is the fold
of every event it has ingested, and a runner rebuilt per request would ingest one event,
report a posture of exactly that event, and be discarded — a door that validates input and
remembers nothing. The bid log has the same shape: R7's shadow log is the audit trail a
merchant reads across many auctions to decide whether to activate at all, and a log rebuilt
per solicitation is a log of one row.

WHY THE SUBMITTER IS THE HTTP RESPONSE
----------------------------------------
``AgentRunner`` is "the only place in the store agent where 'did anything leave the building'
is decided" (its own docstring), and it decides it by handing the answer to a `submitter`.
For a *synchronously solicited* agent the submission channel is the response body itself:
the exchange asks ``POST /v1/bid-requests`` and the answer travels back down that request.
So :class:`ResponseChannel` is a real submitter and not a stand-in — it is where the bid goes
when the runner decides it goes anywhere, and the route serves exactly what it received.

This is why the runner is no longer constructed ``shadow`` regardless of the envelope, which
is what it used to do and what made the activation state unobservable from outside: the
reason given at the time was that this process "genuinely has no submitter", and that was
true only because nothing had connected the runner to the door yet.

Reading the answer OFF the channel rather than off the returned log entry is deliberate.
Both carry the same bid; only one of them carries it *because the submission gate let it
through*. A route that served ``entry.answer`` would still have a fully-formed bid in hand
after a shadow run, one ``if`` away from the wire.

**Per thread, because the route is per thread.** ``answer_bid_request`` is a plain ``def``, so
FastAPI runs it in its threadpool and two solicitations can be in flight at once against this
one process-level runner. The pending answer therefore lives in a
:class:`threading.local`, and every request clears its own slot before running and takes the
answer out of it after — a threadpool thread is reused, and a value left behind on one is a
bid from a previous auction waiting to be served to the next.

THE SINK
--------
``AgentRunner`` requires one — shadow mode *is* the audit log, and a runner with nowhere to
write has nothing to show a merchant. This process has no durable log to hand it, so it gets
a bounded in-memory ring: readable for as long as the process lives, and incapable of growing
without a ceiling on a request path.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..modes import AgentRunner
from .serving import store_context

__all__ = [
    "MAX_INTAKE_LOG_ENTRIES",
    "RUNNER_ATTR",
    "Advocate",
    "BoundedBidLog",
    "ResponseChannel",
    "advocate",
    "agent_runner",
    "configure_advocate",
    "reset_advocate",
]

#: Set on ``app.state``. Read through :func:`advocate` rather than by name, so "does this
#: process have a runner" is one question with one answer. The name is historical — the trust
#: door introduced this seam — and is left alone because it is what a running process's state
#: is already keyed by; renaming it would buy nothing and desynchronise nothing but memory.
RUNNER_ATTR = "trust_intake_runner"

#: How many bid-log entries the process-level sink keeps. A ring: this is a long-lived
#: object on a served process and an unbounded list behind a request path is a leak.
MAX_INTAKE_LOG_ENTRIES = 256


class BoundedBidLog:
    """The runner's required sink: a bounded, readable, in-process ring.

    ``AgentRunner`` accepts a plain callable or any object exposing one of
    ``store_agent.modes.COLLABORATOR_METHODS``; ``append`` is one of them, which is why that
    is the method spelled here rather than a bespoke name.
    """

    __slots__ = ("entries",)

    def __init__(self, maxlen: int = MAX_INTAKE_LOG_ENTRIES) -> None:
        self.entries: deque[Any] = deque(maxlen=maxlen)

    def append(self, entry: Any) -> None:
        self.entries.append(entry)

    def __len__(self) -> int:
        return len(self.entries)


class ResponseChannel:
    """The submitter of a synchronously solicited agent: the bid leaves by the HTTP response.

    ``submit`` is one of ``store_agent.modes.COLLABORATOR_METHODS``, so the runner delivers
    through it without this class having to be callable — and deliberately NOT ``send`` or
    ``put``, which the runner would resolve first and which read like a queue this process
    does not have.
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending = threading.local()

    def submit(self, answer: Any) -> None:
        self._pending.answer = answer

    def take(self) -> Any:
        """Whatever was submitted on THIS thread since the last take, and clear the slot.

        Clearing is the point. A threadpool thread outlives the request that used it, so an
        answer left in the slot is a previous auction's bid sitting in front of the next
        caller — and the next caller may be a store that has since been killed.
        """
        answer = getattr(self._pending, "answer", None)
        self._pending.answer = None
        return answer


@dataclass(frozen=True, slots=True)
class Advocate:
    """One process's advocate: the runner, the log it writes to, the channel it submits by.

    ``log`` and ``channel`` are optional because :func:`configure_advocate` accepts a runner a
    composition root already built and wired to collaborators of its own. Everything this
    module builds itself carries all three.
    """

    runner: Any
    log: BoundedBidLog | None = None
    channel: ResponseChannel | None = None


def configure_advocate(
    app: Any,
    *,
    runner: Any,
    log: BoundedBidLog | None = None,
    channel: ResponseChannel | None = None,
) -> None:
    """Give ``app`` the runner both its served doors use. ``None`` clears it.

    Explicit wiring, for a composition root that already holds the store's runner in memory
    and for tests. The environment path (:func:`advocate`) is what the shipped container
    uses, because the Dockerfile runs ``uvicorn store_agent.main:app`` and nothing in that
    path could call this.
    """
    resolved = None if runner is None else Advocate(runner=runner, log=log, channel=channel)
    setattr(app.state, RUNNER_ATTR, resolved)


def reset_advocate(app: Any) -> None:
    """Forget whatever :func:`advocate` resolved, so the next call resolves again.

    Called by :func:`~store_agent.solicitation.serving.configure_solicitation`, so the context
    an app bids from and the runner it advocates with can never disagree: a runner built from
    an earlier context holds a reference to that dict and would keep bidding from it.

    Deliberately separate from ``configure_advocate(app, runner=None)``, which is a *decision*
    that this app has no runner and is cached as one.
    """
    if hasattr(app.state, RUNNER_ATTR):
        delattr(app.state, RUNNER_ATTR)


def advocate(app: Any) -> Advocate | None:
    """The advocate this app serves with, or ``None`` when it has no store.

    Resolved once per application and then cached — including the ``None``. An agent that was
    never given a store context has no store to advocate for, and inventing one would be
    inventing the merchant's approval; :func:`~store_agent.solicitation.serving.store_context`
    takes the same position for both doors and this reads the *same* context, so one process
    cannot end up bidding for one store and absorbing another's feedback.

    Raises:
        StoreContextError: the configured context cannot be read. Propagated, not swallowed:
            an operator's typo'd ``STORE_AGENT_CONTEXT`` must be a loud 500 and never
            indistinguishable from a deployment that was deliberately left unconfigured.
    """
    if hasattr(app.state, RUNNER_ATTR):
        cached: Advocate | None = getattr(app.state, RUNNER_ATTR)
        return cached
    context = store_context(app)
    resolved = None if context is None else _advocate_for(context)
    setattr(app.state, RUNNER_ATTR, resolved)
    return resolved


def agent_runner(app: Any) -> Any:
    """The `AgentRunner` this app advocates with, or ``None``. See :func:`advocate`."""
    resolved = advocate(app)
    return None if resolved is None else resolved.runner


def _advocate_for(context: Mapping[str, Any]) -> Advocate:
    """One runner for ``context``, in the mode its approved envelope states.

    ``mode=None`` is the runner's own "read the envelope" default, and it fails closed at both
    ways an envelope can fail to say anything — a missing ``activation`` and an unreadable one
    both resolve to ``shadow``. That default is why this passes no mode of its own: a mode
    chosen here would be this module's opinion about a merchant's approval.
    """
    log = BoundedBidLog()
    channel = ResponseChannel()
    runner = AgentRunner(context, sink=log, submitter=channel, mode=None)
    return Advocate(runner=runner, log=log, channel=channel)
