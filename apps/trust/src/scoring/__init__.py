"""The trust engine: one merged observation framework over **six** dimensions.

Owned by T-062 (scope ``apps/trust/src/scoring/**``). Importable both as
``apps.trust.src.scoring`` (repo-root path, how the frozen acceptance suite reaches it) and
as ``trust.scoring`` (via the tracked ``.pkgroot/trust`` symlink, how member packages and
``apps/trust/src/ledger/replay.py`` reach it). Both spellings resolve to this file, and the
block at the bottom makes them resolve to the same *objects* — see :mod:`._binding`.

=====================================  ================================================
:func:`score`                          observations -> one served TrustSnapshot.
:data:`TRUST_DIMENSIONS`               the six, and exactly six (D53).
:data:`OBSERVATION_WEIGHTS`            the approved weight table, read not chosen.
:data:`BLACKLIST_THRESHOLD`            the published score below which a store is out.
:data:`CLAIM_TYPE_DIMENSIONS`          the approved ``claim_type -> dimension`` routing.
:func:`claim_dimension`                one claim type -> one dimension, or a loud raise.
:class:`UnmappedClaimType`             what "exhaustive" costs when it is not.
:class:`Blacklist` / :func:`is_blacklisted`  identity-bound, stateful, fail-closed.
=====================================  ================================================

Where the numbers come from, and where they do not
--------------------------------------------------
Every constant this engine applies — the weights, the prior, the half-life, the blacklist
threshold, the claim-type routing — is READ from the human-approved manifest (T-080) and
never authored here. SPEC A3 is the reason: "the trust engine catches the dishonest store" is
a circular claim if the dishonest behaviours and the weights that catch them both come out of
the trust engine's own config. :mod:`.manifest` carries the one exception and its rationale
(the deployed container does not ship ``fixtures/``, so each constant has a published
fallback and the engine records which one is live).

What this package does NOT do
-----------------------------
* No exploration or ranking policy — the exchange consumes the snapshot (T-064/T-034).
* No hashing, no canonicalisation: D16 puts all of that in ``trust.ledger``.
* No I/O beyond one read of the approved manifest. No datastore, no network, no clock:
  ``as_of`` is always an explicit argument, which is what makes R15/S3's bit-for-bit replay
  assertion a real comparison instead of two approximations of "now".
"""

from __future__ import annotations

import importlib as _importlib
from pathlib import Path as _Path

# --- The two spellings are SEQUENCED before anything else runs (the T-126 pattern) -------
# `.pkgroot/trust` makes this file reachable under two dotted names, and Python executes it
# once per name. The bottom of this file binds the two executions' submodules to one object
# each, but that binding runs LAST while the eager imports below run FIRST — so on its own it
# holds only while the two executions are strictly sequenced. Under a race each execution
# gets past its eager imports before either reaches the bottom, each builds its own copy of
# every submodule, and `setdefault` then declines to overwrite the other's: the binding
# silently does nothing and `except UnmappedClaimType` stops catching across the boundary.
# That failure was MEASURED for `trust.ledger` (5 runs of 5) before it was fixed there.
#
# The fix is an elected primary. One spelling executes freely; the other does nothing at all
# until the primary has finished. Python's per-module import lock does the waiting, and by
# the time it returns the primary has already published every submodule under BOTH spellings,
# so the secondary's eager imports below are `sys.modules` cache hits.
#
# `apps/trust/src/ledger/__init__.py` carries the full measured rationale, including the
# boundary this does NOT fix (first-importing a package by dotted SUBMODULE name from several
# threads at once hits a CPython import cycle that predates this hook). Not duplicated here.
_SPELLINGS: tuple[str, ...] = ("trust.scoring", "apps.trust.src.scoring")
_PRIMARY_SPELLING = _SPELLINGS[0]

if __name__ in _SPELLINGS and __name__ != _PRIMARY_SPELLING:
    try:
        _importlib.import_module(_PRIMARY_SPELLING)
    except ImportError:
        pass

# E402 below is the point of the block above, not an oversight: the sequencing has to run
# BEFORE the first relative import, because it is the eager imports that build the second
# copy of every submodule.
from ._binding import bind_submodules as _bind_submodules  # noqa: E402
from .blacklist import (  # noqa: E402
    BLACKLIST_STATUSES,
    BLOCKING_BLACKLIST_STATUSES,
    CLEARING_BLACKLIST_STATUSES,
    Blacklist,
    BlacklistEntry,
    InvalidBlacklistState,
    business_identity_of,
    is_blacklisted,
)
from .dimensions import (  # noqa: E402
    CATALOG_DIMENSION,
    CLAIM_TYPE_DIMENSIONS,
    CLAIM_TYPE_DIMENSIONS_SOURCE,
    OFFER_INTEGRITY_CLAIM_TYPES,
    PRODUCT_FACT_CLAIM_TYPES,
    PUBLISHED_CLAIM_TYPE_DIMENSIONS,
    TRANSACTION_DIMENSIONS,
    TRUST_DIMENSIONS,
    UnknownTrustDimension,
    UnmappedClaimType,
    claim_dimension,
    is_trust_dimension,
    require_trust_dimension,
)
from .engine import (  # noqa: E402
    BLACKLIST_THRESHOLD,
    CONFIDENCE_EVIDENCE_HALF_LIFE,
    CONFIDENCE_FLOOR,
    DECIDING_OBSERVATION_TYPES,
    HALF_LIFE_DAYS,
    NEW_STORE_PRIOR_N,
    OBSERVATION_POLARITY,
    OBSERVATION_WEIGHTS,
    PRIOR_ALPHA,
    PRIOR_BETA,
    PUBLISHED_OBSERVATION_WEIGHTS,
    SCORE_VERSION,
    UnknownObservationType,
    decay_factor,
    prior_snapshot,
    score,
)
from .manifest import manifest_path, manifest_source  # noqa: E402

__all__ = [
    "BLACKLIST_STATUSES",
    "BLACKLIST_THRESHOLD",
    "BLOCKING_BLACKLIST_STATUSES",
    "CATALOG_DIMENSION",
    "CLAIM_TYPE_DIMENSIONS",
    "CLAIM_TYPE_DIMENSIONS_SOURCE",
    "CLEARING_BLACKLIST_STATUSES",
    "CONFIDENCE_EVIDENCE_HALF_LIFE",
    "CONFIDENCE_FLOOR",
    "DECIDING_OBSERVATION_TYPES",
    "HALF_LIFE_DAYS",
    "NEW_STORE_PRIOR_N",
    "OBSERVATION_POLARITY",
    "OBSERVATION_WEIGHTS",
    "OFFER_INTEGRITY_CLAIM_TYPES",
    "PRIOR_ALPHA",
    "PRIOR_BETA",
    "PRODUCT_FACT_CLAIM_TYPES",
    "PUBLISHED_CLAIM_TYPE_DIMENSIONS",
    "PUBLISHED_OBSERVATION_WEIGHTS",
    "SCORE_VERSION",
    "TRANSACTION_DIMENSIONS",
    "TRUST_DIMENSIONS",
    "Blacklist",
    "BlacklistEntry",
    "InvalidBlacklistState",
    "UnknownObservationType",
    "UnknownTrustDimension",
    "UnmappedClaimType",
    "business_identity_of",
    "claim_dimension",
    "decay_factor",
    "is_blacklisted",
    "is_trust_dimension",
    "manifest_path",
    "manifest_source",
    "prior_snapshot",
    "require_trust_dimension",
    "score",
]


def __dir__() -> list[str]:
    return sorted(__all__)


#: `apps/trust/src/scoring/__init__.py` -> the checkout root. `resolve()` collapses the
#: `.pkgroot` symlink first, so the hop count is the same under both spellings.
_REPO_ROOT = _Path(__file__).resolve().parents[4]
_SPELLING_ROOTS: dict[str, _Path] = {
    "trust.scoring": _REPO_ROOT / ".pkgroot",
    "apps.trust.src.scoring": _REPO_ROOT,
}

_bind_submodules(__name__, __path__, _SPELLINGS, _SPELLING_ROOTS)
