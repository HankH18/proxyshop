"""The trust service's claim-verification intake (T-065 persistence half, T-256).

One feature package, one job: give the outcome of a claim verification a door into this
service. :mod:`.routes` mounts ``POST /claims/verifications``, writes the outcome through
:func:`trust.verification.persist_claim_verification`, and appends the ``claim_verified``
ledger event the frozen vocabulary has always reserved for it.

Why this exists as a route rather than as a library call: before it, the five tables
``db/migrations/0002_ledger_tables.sql`` reserves for verification outcomes had no writer
anywhere in the product tree, and the writer that T-256 asked for would have been a function
nobody invoked — which persists exactly as much as no function at all. The seam needs a
caller that ships, and a service that verifies claims needs somewhere to be told about one.

Importable as ``apps.trust.src.claims`` (repo-root path) and as ``trust.claims`` (via
``.pkgroot/trust``); the block at the bottom makes both spellings resolve to the same
objects — see :mod:`trust._shared._binding`.
"""

from __future__ import annotations

import importlib as _importlib
from pathlib import Path as _Path

# --- The two spellings are SEQUENCED before anything else runs (the T-126 pattern) -------
# See `apps/trust/src/ledger/__init__.py` for the measured rationale; not duplicated here.
_SPELLINGS: tuple[str, ...] = ("trust.claims", "apps.trust.src.claims")
_PRIMARY_SPELLING = _SPELLINGS[0]

if __name__ in _SPELLINGS and __name__ != _PRIMARY_SPELLING:
    try:
        _importlib.import_module(_PRIMARY_SPELLING)
    except ImportError:
        pass

# E402 below is the point of the block above: the sequencing has to run BEFORE the first
# relative import, because it is the eager imports that build the second copy.
from .._shared._binding import bind_submodules as _bind_submodules  # noqa: E402

__all__: list[str] = []


def __dir__() -> list[str]:
    return sorted(__all__)


_REPO_ROOT = _Path(__file__).resolve().parents[4]
_SPELLING_ROOTS: dict[str, _Path] = {
    "trust.claims": _REPO_ROOT / ".pkgroot",
    "apps.trust.src.claims": _REPO_ROOT,
}

_bind_submodules(__name__, __path__, _SPELLINGS, _SPELLING_ROOTS)
