"""The served solicitation surface: ``POST /v1/bid-requests`` and the wiring it bids from.

``routes`` holds the door the exchange knocks on; ``serving`` holds the answer to "which store is
this process advocating for"; ``advocate`` holds the one
:class:`~store_agent.modes.AgentRunner` built from that store, which is what decides whether an
answer leaves the building at all (R7); ``copywriter`` holds the model that writes the pitch that
runner puts on `Bid.message` — the dedicated advocate a shop buys by joining (D55) — resolved
here rather than on the bid path, which may read no environment. They are separate because the route is discovered by a
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
from .copywriter import (
    PITCH_MAX_RETRIES,
    PITCH_OUTCOMES,
    PITCH_RECORDINGS_ENV,
    PITCH_RESERVE_ENV,
    PITCH_RESERVE_SECONDS,
    PITCH_TIMEOUT_ENV,
    PITCH_TIMEOUT_SECONDS,
    PitchAttempt,
    PitchBudgetExhausted,
    PitchClient,
    is_deadline_miss,
    pitch_budget_seconds,
    pitch_client,
    resolve_pitch_reserve,
    resolve_pitch_timeout,
)
from .refusal import (
    MAX_IDENTIFIER_CHARS,
    MAX_VALIDATION_ERRORS,
    MAX_VALIDATION_MESSAGE_CHARS,
    EnrichedRefusalRoute,
)
from .routes import (
    CANONICAL_BID_REQUEST,
    DECLINE_REASON_HEADER,
    KILLED_REASON,
    NOT_ACTIVATED_REASON,
    UNCONFIGURED_REASON,
    UNDISCLOSED_REASON,
    log_solicitation,
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
    "CANONICAL_BID_REQUEST",
    "CONTEXT_ENV",
    "DECLINE_REASON_HEADER",
    "DOMAIN_ENV",
    "KILLED_REASON",
    "MAX_CONTEXT_BYTES",
    "MAX_IDENTIFIER_CHARS",
    "MAX_INTAKE_LOG_ENTRIES",
    "MAX_VALIDATION_ERRORS",
    "MAX_VALIDATION_MESSAGE_CHARS",
    "NOT_ACTIVATED_REASON",
    "PITCH_MAX_RETRIES",
    "PITCH_OUTCOMES",
    "PITCH_RECORDINGS_ENV",
    "PITCH_RESERVE_ENV",
    "PITCH_RESERVE_SECONDS",
    "PITCH_TIMEOUT_ENV",
    "PITCH_TIMEOUT_SECONDS",
    "UNCONFIGURED_REASON",
    "UNDISCLOSED_REASON",
    "Advocate",
    "BoundedBidLog",
    "EnrichedRefusalRoute",
    "PitchAttempt",
    "PitchBudgetExhausted",
    "PitchClient",
    "ResponseChannel",
    "StoreContextError",
    "advocate",
    "agent_runner",
    "configure_advocate",
    "configure_solicitation",
    "is_deadline_miss",
    "load_context_from_env",
    "log_solicitation",
    "no_submission_reason",
    "pitch_budget_seconds",
    "pitch_client",
    "resolve_pitch_reserve",
    "resolve_pitch_timeout",
    "reset_advocate",
    "router",
    "store_context",
]
