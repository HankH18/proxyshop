"""The ONE `AgentRunner` this process advocates with, and where its store comes from.

A hosted agent process is one store's advocate — ``solicitation/serving.py`` says so and the
exchange relies on it, choosing which agent to solicit by choosing which URL to POST to. The
same fact decides everything here: there is exactly one runner per application, it is built
from the same store context the bid door reads, and it is the object whose posture a pushed
trust event moves.

WHY THE RUNNER IS CACHED ON ``app.state`` AND NOT REBUILT PER REQUEST
----------------------------------------------------------------------
``AgentRunner`` accumulates. :attr:`~store_agent.modes.AgentRunner.trust_posture` is the fold
of every event it has ingested, and a runner rebuilt per request would ingest one event,
report a posture of exactly that event, and be discarded — which is a door that validates
input and remembers nothing. That is the failure mode this whole ticket is about, one layer
in, so the runner lives for as long as the process.

WHY IT IS CONSTRUCTED IN ``shadow``, WHATEVER THE ENVELOPE SAYS
----------------------------------------------------------------
``AgentRunner.mode``'s setter refuses any submitting mode when the runner has no usable
submitter, because "an activated store whose submitter is None looks live and bids nowhere"
is the one failure it will not allow to be constructed. This process genuinely has no
submitter: it answers the exchange's solicitation synchronously on ``POST /v1/bid-requests``
and posts nothing anywhere. So the runner is built ``shadow`` explicitly rather than from the
envelope's activation, and that is an honest description of this process rather than a
downgrade of the store's approval — the merchant's activation governs whether the *exchange*
uses this agent's bid, and it is read on the bid path, not here.

Nothing about the posture is authorization. It never re-prices an offer and never widens a
floor; ``AgentRunner``'s own docstring is explicit that an ingested trust event moves the
posture and nothing else.

THE SINK
--------
``AgentRunner`` requires one — shadow mode *is* the audit log, and a runner with nowhere to
write has nothing to show a merchant. This process has no durable log to hand it, so it gets
a bounded in-memory ring: readable for as long as the process lives, and incapable of growing
without a ceiling on a request path. The runner writes to it only from ``run()``, which the
trust door never calls; it exists so that the runner is constructible and so that a later
ticket wiring the bid path through the runner finds a sink already there.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any

from ..modes import AgentRunner
from ..solicitation.serving import store_context

__all__ = [
    "MAX_INTAKE_LOG_ENTRIES",
    "RUNNER_ATTR",
    "BoundedBidLog",
    "agent_runner",
    "configure_trust_intake",
    "reset_trust_intake",
]

#: Set on ``app.state``. Read through :func:`agent_runner` rather than by name, so "does this
#: process have a runner" is one question with one answer.
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


def configure_trust_intake(app: Any, *, runner: Any) -> None:
    """Give ``app`` the runner its trust door will ingest into. ``None`` clears it.

    Explicit wiring, for a composition root that already holds the store's runner in memory
    and for tests. The environment path (:func:`agent_runner`) is what the shipped container
    uses, because the Dockerfile runs ``uvicorn store_agent.main:app`` and nothing in that
    path could call this.
    """
    setattr(app.state, RUNNER_ATTR, runner)


def reset_trust_intake(app: Any) -> None:
    """Forget whatever :func:`agent_runner` resolved, so the next call resolves again.

    Only useful to a test that changes the environment mid-process. Deliberately separate
    from ``configure_trust_intake(app, runner=None)``, which is a *decision* that this app has
    no runner and is cached as one.
    """
    if hasattr(app.state, RUNNER_ATTR):
        delattr(app.state, RUNNER_ATTR)


def agent_runner(app: Any) -> Any:
    """The runner this app ingests trust events into, or ``None`` when it has no store.

    Resolved once per application and then cached — including the ``None``. An agent that was
    never given a store context has no store to be told about, and inventing one would be
    inventing the merchant's approval; ``solicitation/serving.py`` takes the same position for
    the bid door and this reads the *same* context, so one process cannot end up bidding for
    one store and absorbing another's feedback.

    Raises:
        StoreContextError: the configured context cannot be read. Propagated, not swallowed,
            exactly as the bid door propagates it: an operator's typo'd
            ``STORE_AGENT_CONTEXT`` must be a loud 500 and never indistinguishable from a
            deployment that was deliberately left unconfigured.
    """
    if hasattr(app.state, RUNNER_ATTR):
        return getattr(app.state, RUNNER_ATTR)
    context = store_context(app)
    runner = None if context is None else _runner_for(context)
    setattr(app.state, RUNNER_ATTR, runner)
    return runner


def _runner_for(context: Mapping[str, Any]) -> AgentRunner:
    """One runner for ``context``, in shadow, with a bounded log. See the module docstring."""
    return AgentRunner(context, sink=BoundedBidLog(), submitter=None, mode="shadow")
