"""How the trust door reaches the ONE `AgentRunner` this process advocates with.

The runner itself, the bounded log it writes to and the channel it submits by all live in
:mod:`store_agent.solicitation.advocate`, because BOTH served doors need the same one and the
bid door is where the store context is resolved. This module is the trust door's view of that
seam and the two configure functions it publishes under its own names; there is no second
runner and no second ``app.state`` key.

WHY THERE IS EXACTLY ONE, AND WHY IT IS NOT REBUILT PER REQUEST
----------------------------------------------------------------
``AgentRunner`` accumulates. :attr:`~store_agent.modes.AgentRunner.trust_posture` is the fold
of every event it has ingested, and a runner rebuilt per request would ingest one event,
report a posture of exactly that event, and be discarded — a door that validates input and
remembers nothing.

WHAT CHANGED, AND WHY THE OLD JUSTIFICATION IS GONE
-----------------------------------------------------
This module used to build the runner ``shadow`` unconditionally, on the argument that the
process "genuinely has no submitter" because it answers the exchange's solicitation
synchronously and posts nothing anywhere. That was true only because the bid door had not
been connected to the runner yet: for a synchronously solicited agent the submission channel
IS the response body, and ``POST /v1/bid-requests`` now serves what the runner hands its
submitter. So the runner is built in the mode its approved envelope states — which is what
makes R7's shadow mode and R9's kill switch observable from outside this process at all.

Nothing about the posture is authorization. It never re-prices an offer and never widens a
floor; ``AgentRunner``'s own docstring is explicit that an ingested trust event moves the
posture and nothing else.
"""

from __future__ import annotations

from typing import Any

from ..solicitation.advocate import (
    MAX_INTAKE_LOG_ENTRIES,
    RUNNER_ATTR,
    Advocate,
    BoundedBidLog,
    advocate,
    agent_runner,
    configure_advocate,
    reset_advocate,
)

__all__ = [
    "MAX_INTAKE_LOG_ENTRIES",
    "RUNNER_ATTR",
    "Advocate",
    "BoundedBidLog",
    "advocate",
    "agent_runner",
    "configure_trust_intake",
    "reset_trust_intake",
]


def configure_trust_intake(app: Any, *, runner: Any) -> None:
    """Give ``app`` the runner its trust door will ingest into. ``None`` clears it.

    Explicit wiring, for a composition root that already holds the store's runner in memory
    and for tests. The environment path (:func:`agent_runner`) is what the shipped container
    uses, because the Dockerfile runs ``uvicorn store_agent.main:app`` and nothing in that
    path could call this.

    It is the same object the BID door then serves through, because there is one runner per
    process: a wired runner that only the trust door used would leave the two doors disagreeing
    about which store this process advocates for.

    **And that is why no ``pitch=`` is named here.** This call used to hand over a runner and
    nothing else, which built an advocate with ``pitch=None``; the bid door then computed a
    budget from the exchange's `respond_by` and had nothing to arm it on, so the copywriter fell
    back to its fixed ceiling — the exact defect the budget closes, reintroduced by a
    composition root that wired its own runner. :func:`configure_advocate` now recovers the
    copywriter from the runner that holds it, so a caller that wired a `PitchClient` gets the
    budget whether or not it thought to mention it, and one that wired something else is told.
    """
    configure_advocate(app, runner=runner)


def reset_trust_intake(app: Any) -> None:
    """Forget whatever :func:`agent_runner` resolved, so the next call resolves again.

    Only useful to a test that changes the environment mid-process. Deliberately separate
    from ``configure_trust_intake(app, runner=None)``, which is a *decision* that this app has
    no runner and is cached as one.
    """
    reset_advocate(app)
