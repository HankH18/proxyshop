"""The door trust pushes this store's own trust deltas through (R13, receiving end).

``store_agent.modes.AgentRunner.ingest_trust_event`` was built, tested and reachable by
nobody: every call site in the repository was a test. There was no route under
``packages/store-agent/src`` that could hand it a pushed event, so the trust service had
nothing to POST to even once it had a delta to send.

This package is that route and the process-level runner behind it. It holds no scoring, no
policy and no state of its own — see :mod:`store_agent.trust_intake.runner` for how the one
runner this process advocates with is resolved, and :mod:`store_agent.trust_intake.routes`
for the door.
"""

from __future__ import annotations

from .runner import (
    MAX_INTAKE_LOG_ENTRIES,
    agent_runner,
    configure_trust_intake,
    reset_trust_intake,
)

__all__ = [
    "MAX_INTAKE_LOG_ENTRIES",
    "agent_runner",
    "configure_trust_intake",
    "reset_trust_intake",
]
