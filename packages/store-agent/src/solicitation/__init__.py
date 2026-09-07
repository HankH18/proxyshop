"""The served solicitation surface: ``POST /v1/bid-requests`` and the wiring it bids from.

``routes`` holds the door the exchange knocks on; ``serving`` holds the answer to "which store is
this process advocating for"; ``advocate`` holds the one
:class:`~store_agent.modes.AgentRunner` built from that store, which is what decides whether an
answer leaves the building at all (R7). They are separate because the route is discovered by a
filesystem glob in the frozen ``main.py`` and must therefore be importable with nothing
configured, while the configuration is what a composition root reaches for by name.
"""

from __future__ import annotations

from .advocate import (
    MAX_INTAKE_LOG_ENTRIES,
    Advocate,
    BoundedBidLog,
    ResponseChannel,
    advocate,
    agent_runner,
    configure_advocate,
    reset_advocate,
)
from .routes import (
    DECLINE_REASON_HEADER,
    KILLED_REASON,
    NOT_ACTIVATED_REASON,
    UNCONFIGURED_REASON,
    UNDISCLOSED_REASON,
    no_submission_reason,
    router,
)
from .serving import (
    CONTEXT_ENV,
    DOMAIN_ENV,
    MAX_CONTEXT_BYTES,
    StoreContextError,
    configure_solicitation,
    load_context_from_env,
    store_context,
)

__all__ = [
    "CONTEXT_ENV",
    "DECLINE_REASON_HEADER",
    "DOMAIN_ENV",
    "KILLED_REASON",
    "MAX_CONTEXT_BYTES",
    "MAX_INTAKE_LOG_ENTRIES",
    "NOT_ACTIVATED_REASON",
    "UNCONFIGURED_REASON",
    "UNDISCLOSED_REASON",
    "Advocate",
    "BoundedBidLog",
    "ResponseChannel",
    "StoreContextError",
    "advocate",
    "agent_runner",
    "configure_advocate",
    "configure_solicitation",
    "load_context_from_env",
    "no_submission_reason",
    "reset_advocate",
    "router",
    "store_context",
]
