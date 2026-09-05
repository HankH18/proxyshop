"""The served solicitation surface: ``POST /v1/bid-requests`` and the wiring it bids from.

``routes`` holds the door the exchange knocks on; ``serving`` holds the answer to "which store is
this process advocating for". They are separate because the route is discovered by a filesystem
glob in the frozen ``main.py`` and must therefore be importable with nothing configured, while
the configuration is what a composition root reaches for by name.
"""

from __future__ import annotations

from .routes import (
    DECLINE_REASON_HEADER,
    UNCONFIGURED_REASON,
    UNDISCLOSED_REASON,
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
    "MAX_CONTEXT_BYTES",
    "UNCONFIGURED_REASON",
    "UNDISCLOSED_REASON",
    "StoreContextError",
    "configure_solicitation",
    "load_context_from_env",
    "router",
    "store_context",
]
