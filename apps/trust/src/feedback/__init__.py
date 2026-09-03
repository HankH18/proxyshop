"""Trust events out to the affected store, buyer feedback back in — both pseudonymous.

Owned by T-063 (scope ``apps/trust/src/feedback/**``). Importable as
``apps.trust.src.feedback`` (repo-root path, how the frozen acceptance suite reaches it) and
as ``trust.feedback`` (via ``.pkgroot/trust``); the block at the bottom makes both spellings
resolve to the same objects — see :mod:`._binding`.

==============================  ========================================================
:func:`push_trust_event`        one delta -> one send, to the affected store only.
:func:`trust_event_payload`     that payload, without sending it.
:func:`accept_feedback`         the R14 routed-buyer gate, and the weight it earns.
:func:`feedback_observation`    that verdict -> the weighted observation the scorer reads.
:func:`scrub`                   the recursive buyer-identity scrub the push applies.
==============================  ========================================================

R13/R5: the pushed payload carries the FULL originating event and no buyer identity — the
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
from ._binding import bind_submodules as _bind_submodules  # noqa: E402
from .engine import (  # noqa: E402
    BASE_FEEDBACK_WEIGHT,
    FEEDBACK_DIMENSION,
    FEEDBACK_NEGATIVE_TYPE,
    FEEDBACK_POSITIVE_TYPE,
    RETURN_CONTRADICTION_FACTOR,
    TRUST_EVENT_SCHEMA_VERSION,
    FeedbackRejected,
    accept_feedback,
    feedback_observation,
    push_trust_event,
    trust_event_payload,
)
from .scrub import (  # noqa: E402
    IDENTITY_KEY_SUBSTRINGS,
    IDENTITY_KEYS,
    REDACTED,
    scrub,
    scrub_report,
)

__all__ = [
    "BASE_FEEDBACK_WEIGHT",
    "FEEDBACK_DIMENSION",
    "FEEDBACK_NEGATIVE_TYPE",
    "FEEDBACK_POSITIVE_TYPE",
    "IDENTITY_KEYS",
    "IDENTITY_KEY_SUBSTRINGS",
    "REDACTED",
    "RETURN_CONTRADICTION_FACTOR",
    "TRUST_EVENT_SCHEMA_VERSION",
    "FeedbackRejected",
    "accept_feedback",
    "feedback_observation",
    "push_trust_event",
    "scrub",
    "scrub_report",
    "trust_event_payload",
]


def __dir__() -> list[str]:
    return sorted(__all__)


_REPO_ROOT = _Path(__file__).resolve().parents[4]
_SPELLING_ROOTS: dict[str, _Path] = {
    "trust.feedback": _REPO_ROOT / ".pkgroot",
    "apps.trust.src.feedback": _REPO_ROOT,
}

_bind_submodules(__name__, __path__, _SPELLINGS, _SPELLING_ROOTS)
