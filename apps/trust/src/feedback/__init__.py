"""Trust events out to the affected store, buyer feedback back in — both pseudonymous.

Owned by T-063 (scope ``apps/trust/src/feedback/**``). Importable as
``apps.trust.src.feedback`` (repo-root path, how the frozen acceptance suite reaches it) and
as ``trust.feedback`` (via ``.pkgroot/trust``); the block at the bottom makes both spellings
resolve to the same objects — see :mod:`trust._shared._binding`.

==============================  ========================================================
:func:`push_trust_event`        one delta -> one send, to the affected store only.
:func:`trust_event_payload`     that payload, without sending it.
:func:`delta_for_event`         what ONE stored ledger event did to a store's posture.
:func:`announce_trust_event`    that delta, pushed to that store's agent. Never raises.
:class:`StoreAgentSink`         the addressed, bounded, non-raising HTTP transport.
:func:`accept_feedback`         the R14 routed-buyer gate, and the weight it earns.
:func:`feedback_observation`    that verdict -> the weighted observation the scorer reads.
:func:`fold_feedback`           R14's fold-time ``{type, weight}``, for the two projections.
:func:`scrub`                   the recursive buyer-identity scrub the push applies.
==============================  ========================================================

R13/R5: the pushed payload carries the originating event — every field the published
``contracts.LedgerEvent`` declares, the ledger's own ``seq``/``event_hash`` bookkeeping
dropped because the intake forbids extras — and no buyer identity. The
identity keys are removed, not nulled, because a labelled empty box tells the recipient
exactly what to correlate against. R14: only a buyer the network routed may leave feedback,
and a positive report from a buyer who returned the item is downweighted rather than
discarded. :mod:`.engine` and :mod:`.scrub` carry the rationale for each.
"""

from __future__ import annotations

import importlib as _importlib
from pathlib import Path as _Path

# --- The two spellings are SEQUENCED before anything else runs (the T-126 pattern) -------
# See `apps/trust/src/ledger/__init__.py` for the measured rationale; not duplicated here.
_SPELLINGS: tuple[str, ...] = ("trust.feedback", "apps.trust.src.feedback")
_PRIMARY_SPELLING = _SPELLINGS[0]

if __name__ in _SPELLINGS and __name__ != _PRIMARY_SPELLING:
    try:
        _importlib.import_module(_PRIMARY_SPELLING)
    except ImportError:
        pass

# E402 below is the point of the block above: the sequencing has to run BEFORE the first
# relative import, because it is the eager imports that build the second copy.
from .._shared._binding import bind_submodules as _bind_submodules  # noqa: E402
from .deltas import (  # noqa: E402
    MAX_DELTA_HISTORY_EVENTS,
    HistoryTooLong,
    delta_for_event,
    observation_of,
)
from .engine import (  # noqa: E402
    BASE_FEEDBACK_WEIGHT,
    DISCLOSURE_POLICY,
    FEEDBACK_DIMENSION,
    FEEDBACK_NEGATIVE_TYPE,
    FEEDBACK_POSITIVE_TYPE,
    RETURN_CONTRADICTION_FACTOR,
    TRUST_ATTRIBUTION_KEY,
    TRUST_EVENT_SCHEMA_VERSION,
    TRUST_REPORT_KEY,
    FeedbackRejected,
    accept_feedback,
    feedback_observation,
    push_trust_event,
    trust_event_payload,
)
from .notify import (  # noqa: E402
    DEFAULT_PUSH_TIMEOUT_SECONDS,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    ENV_EXCHANGE_OUTCOMES_URL,
    ENV_STORE_AGENT_ENDPOINTS,
    EXCHANGE_OUTCOMES_PATH,
    MAX_PENDING_TRUST_EVENTS,
    MAX_UNDELIVERED_TRUST_EVENTS,
    TRUST_EVENT_PATH,
    ExchangeOutcomeSink,
    StoreAgentSink,
    TrustEventFanout,
    announce_trust_event,
    build_sink,
    exchange_outcomes_url,
    learning_loop_report,
    log_learning_loop_state,
    store_agent_endpoints,
    store_history_reader,
    trust_event_url,
)
from .scrub import (  # noqa: E402
    IDENTITY_KEY_SUBSTRINGS,
    IDENTITY_KEYS,
    REDACTED,
    scrub,
    scrub_report,
)
from .weighting import RETURN_KIND, fold_feedback, order_identity  # noqa: E402

__all__ = [
    "BASE_FEEDBACK_WEIGHT",
    "DEFAULT_PUSH_TIMEOUT_SECONDS",
    "DEFAULT_RETRY_ATTEMPTS",
    "DEFAULT_RETRY_BACKOFF_SECONDS",
    "DISCLOSURE_POLICY",
    "ENV_EXCHANGE_OUTCOMES_URL",
    "ENV_STORE_AGENT_ENDPOINTS",
    "EXCHANGE_OUTCOMES_PATH",
    "FEEDBACK_DIMENSION",
    "FEEDBACK_NEGATIVE_TYPE",
    "FEEDBACK_POSITIVE_TYPE",
    "IDENTITY_KEYS",
    "IDENTITY_KEY_SUBSTRINGS",
    "MAX_DELTA_HISTORY_EVENTS",
    "MAX_PENDING_TRUST_EVENTS",
    "MAX_UNDELIVERED_TRUST_EVENTS",
    "REDACTED",
    "RETURN_CONTRADICTION_FACTOR",
    "RETURN_KIND",
    "TRUST_EVENT_PATH",
    "TRUST_EVENT_SCHEMA_VERSION",
    "TRUST_ATTRIBUTION_KEY",
    "TRUST_REPORT_KEY",
    "ExchangeOutcomeSink",
    "FeedbackRejected",
    "HistoryTooLong",
    "StoreAgentSink",
    "TrustEventFanout",
    "accept_feedback",
    "announce_trust_event",
    "build_sink",
    "delta_for_event",
    "exchange_outcomes_url",
    "feedback_observation",
    "fold_feedback",
    "learning_loop_report",
    "log_learning_loop_state",
    "observation_of",
    "order_identity",
    "push_trust_event",
    "scrub",
    "scrub_report",
    "store_agent_endpoints",
    "store_history_reader",
    "trust_event_payload",
    "trust_event_url",
]


def __dir__() -> list[str]:
    return sorted(__all__)


_REPO_ROOT = _Path(__file__).resolve().parents[4]
_SPELLING_ROOTS: dict[str, _Path] = {
    "trust.feedback": _REPO_ROOT / ".pkgroot",
    "apps.trust.src.feedback": _REPO_ROOT,
}

_bind_submodules(__name__, __path__, _SPELLINGS, _SPELLING_ROOTS)
