"""The trust service's seam onto the claim verifier. It holds no verification logic.

Owned by T-065 alongside ``packages/verification/**``. The verifier itself lives THERE, not
here, and that split is deliberate: claim verification is a library — pure, deterministic,
stdlib-only — consumed by the ingest service and the simulator as well as by trust, and
copying it into this service would give the repo two comparators free to disagree about
whether "0.5 kg" matches 500 g. The frozen acceptance suite reaches the verifier as
``packages.verification``; member packages reach it as ``claim_verification``; nothing
imports it through this module.

What this module IS for is the trust-side wiring: the one place a trust module asks "what
does a verification outcome mean here", so that answer is written down once.
:func:`observation_from_claim` is that answer — it turns one verified claim into the trust
observation ``trust.scoring.score`` consumes, routing it through the human-approved
``claim_type -> dimension`` table and raising loudly on a claim type nobody approved.

:func:`persist_claim_verification` is the second such answer (T-256): where a verification
outcome becomes rows in the five ``ledger.*`` tables T-065 reserved for it. It lives in
:mod:`.persistence` and is imported EAGERLY, which the paragraph below does not contradict —
it needs no verifier on the path, only a database connection the caller hands it.

The VERIFIER imports are LAZY (PEP 562), and the reason has CHANGED — read this before
deleting it.

It used to be that ``apps/trust/Dockerfile`` did not copy ``packages/verification`` at all, so
an eager ``import claim_verification`` here would have made the whole trust service
unimportable in its own container. T-193 fixed the image: the Dockerfile now copies
``packages/verification/{__init__.py,src/}`` and links ``.pkgroot/claim_verification``, and
the trust image resolves ``verify`` — pinned by
``apps/trust/tests/test_repro_open_tickets.py::test_the_trust_image_copy_set_can_resolve_the_claim_verifier``
against a container-shaped tree.

What laziness still buys, and why it stays: this seam is imported by every consumer of
``trust.verification``, including ones with no verifier on their path at all (a member that
holds only ``.pkgroot`` for its own package, a probe, a partial deploy). Eager here would
turn "cannot verify" into "cannot import trust", which is a far worse failure and one that
lands at startup rather than at the call that actually needed the verifier. The cost is that a
missing verifier is invisible until first use, which is precisely how T-193 stayed open — so
the image's COPY set has a gate of its own rather than relying on this module to notice.
"""

from __future__ import annotations

import importlib as _importlib
from pathlib import Path as _Path
from typing import Any

# --- The two spellings are SEQUENCED before anything else runs (the T-126 pattern) -------
# See `apps/trust/src/ledger/__init__.py` for the measured rationale; not duplicated here.
# This package grew a submodule with T-256 (`.persistence`), so it now needs the same
# elected-primary binding every other feature package here already has.
_SPELLINGS: tuple[str, ...] = ("trust.verification", "apps.trust.src.verification")
_PRIMARY_SPELLING = _SPELLINGS[0]

if __name__ in _SPELLINGS and __name__ != _PRIMARY_SPELLING:
    try:
        _importlib.import_module(_PRIMARY_SPELLING)
    except ImportError:
        pass

# E402 below is the point of the block above: the sequencing has to run BEFORE the first
# relative import, because it is the eager imports that build the second copy.
from .._shared._binding import bind_submodules as _bind_submodules  # noqa: E402
from .persistence import persist_claim_verification  # noqa: E402

__all__ = [
    "observation_from_claim",
    "persist_claim_verification",
    "satisfies_hard_constraint",
    "verify",
]

_LAZY = {"verify": "verify", "satisfies_hard_constraint": "satisfies_hard_constraint"}


def __getattr__(name: str) -> Any:
    """Forward ``verify`` / ``satisfies_hard_constraint`` to the verification package.

    Raises:
        AttributeError: for any other name.
        ModuleNotFoundError: when the verification package is not on this deployment's path.
            Named rather than swallowed: a trust service running without the verifier should
            say so, not silently treat every product-fact claim as unverifiable.
    """
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    for spelling in ("claim_verification", "packages.verification"):
        try:
            module = importlib.import_module(spelling)
        except ImportError:
            continue
        return getattr(module, _LAZY[name])
    raise ModuleNotFoundError(
        "the claim verifier (packages/verification, T-065) is not importable from this "
        "deployment, under either spelling (claim_verification, packages.verification). "
        "apps/trust/Dockerfile DOES ship it — COPY packages/verification/{__init__.py,src/} "
        "plus `ln -s ../packages/verification/src /app/.pkgroot/claim_verification` — so this "
        "is some other path: check that .pkgroot is on sys.path here. This module "
        "deliberately does not carry a second comparator."
    )


def __dir__() -> list[str]:
    return sorted(__all__)


def observation_from_claim(claim: Any, store_id: str, *, observed_at: Any) -> dict[str, Any]:
    """Turn one verified claim into the trust observation the scorer consumes.

    This is the whole route D53 describes, in one place: the verifier decides a claim, the
    result carries the ``claim_type`` that types it, the human-approved table turns that into
    a dimension, and the status becomes the observation type. Writing it once is the point —
    a second caller that inlined ``"ingredients" -> catalog_claim_accuracy`` would be a
    second, unapproved routing table.

    Args:
        claim: one claim off a ``VerificationResult`` — mapping or object — carrying
            ``claim_type`` and ``status``.
        store_id: the store the claim was made by.
        observed_at: when the claim was verified. Explicit, never a clock: the scorer decays
            against it and a replay has to reproduce the same number.

    Returns:
        ``{store_id, dim, type, observed_at}``.

    Raises:
        UnmappedClaimType: the claim's type is absent from the approved table (or missing
            entirely). Loud on purpose — a silent default would score the store on a
            dimension no human approved.
    """
    from ..scoring import claim_dimension

    if isinstance(claim, dict):
        claim_type, status = claim.get("claim_type"), claim.get("status")
    else:
        claim_type, status = getattr(claim, "claim_type", None), getattr(claim, "status", None)
    return {
        "store_id": store_id,
        "dim": claim_dimension(claim_type),
        "type": str(status),
        "observed_at": observed_at,
    }


_REPO_ROOT = _Path(__file__).resolve().parents[4]
_SPELLING_ROOTS: dict[str, _Path] = {
    "trust.verification": _REPO_ROOT / ".pkgroot",
    "apps.trust.src.verification": _REPO_ROOT,
}

_bind_submodules(__name__, __path__, _SPELLINGS, _SPELLING_ROOTS)
