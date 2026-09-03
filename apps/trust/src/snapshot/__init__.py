"""One versioned TrustSnapshot shape, and it is the only trust shape the exchange consumes.

Owned by T-064 (scope ``apps/trust/src/snapshot/**``). Importable as
``apps.trust.src.snapshot`` (repo-root path, how the frozen acceptance suite reaches it) and
as ``trust.snapshot`` (via ``.pkgroot/trust``); the block at the bottom makes both spellings
resolve to the same objects — see :mod:`._binding`.

============================  ==========================================================
:func:`build_snapshot`        stores -> ``{version, score_version, as_of, stores}``.
:func:`store_entry`           one store's entry, if you already have the store.
:func:`clean_episodes`        the ``low_data`` count, and how it is derived.
:data:`EPISODE_FLOOR_DIMENSIONS`  the dimensions that count is derived over, and why.
:data:`SNAPSHOT_VERSION`      what the exchange client caches on and refreshes against.
============================  ==========================================================

Every entry carries all SIX dimensions with ``alpha``/``beta``/``decayed_at``/``coverage``,
plus ``blacklisted`` (resolved through the identity-bound, fail-closed blacklist) and
``low_data`` (below the manifest's ``new_store_prior_n`` clean episodes). :mod:`.builder`
carries the rationale for both flags — in particular why ``low_data`` cannot be recovered
from the score, since an unknown store and a genuinely mixed one both serve ~0.5.
"""

from __future__ import annotations

import importlib as _importlib
from pathlib import Path as _Path

# --- The two spellings are SEQUENCED before anything else runs (the T-126 pattern) -------
# See `apps/trust/src/ledger/__init__.py` for the measured rationale; not duplicated here.
_SPELLINGS: tuple[str, ...] = ("trust.snapshot", "apps.trust.src.snapshot")
_PRIMARY_SPELLING = _SPELLINGS[0]

if __name__ in _SPELLINGS and __name__ != _PRIMARY_SPELLING:
    try:
        _importlib.import_module(_PRIMARY_SPELLING)
    except ImportError:
        pass

# E402 below is the point of the block above: the sequencing has to run BEFORE the first
# relative import, because it is the eager imports that build the second copy.
from ._binding import bind_submodules as _bind_submodules  # noqa: E402
from .builder import (  # noqa: E402
    EPISODE_FLOOR_DIMENSIONS,
    SNAPSHOT_VERSION,
    build_snapshot,
    clean_episodes,
    store_entry,
)

__all__ = [
    "EPISODE_FLOOR_DIMENSIONS",
    "SNAPSHOT_VERSION",
    "build_snapshot",
    "clean_episodes",
    "store_entry",
]


def __dir__() -> list[str]:
    return sorted(__all__)


_REPO_ROOT = _Path(__file__).resolve().parents[4]
_SPELLING_ROOTS: dict[str, _Path] = {
    "trust.snapshot": _REPO_ROOT / ".pkgroot",
    "apps.trust.src.snapshot": _REPO_ROOT,
}

_bind_submodules(__name__, __path__, _SPELLINGS, _SPELLING_ROOTS)
