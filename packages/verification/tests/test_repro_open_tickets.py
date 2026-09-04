"""Reproduction gates for the open `packages/verification` findings.

The test here asserts the behaviour that SHOULD hold and therefore fails against the tree as it
stands. It carries ``xfail(strict=True)`` so an ordinary run reports ``xfailed`` and the
repo-wide build gate stays green, while the ticket's own gate
(``pytest <file> -q --runxfail -k <name>``) reports a real failure with the test SELECTED. When
the defect is repaired the test XPASSes, which ``strict=True`` turns into a failure — so the
marker cannot outlive the bug.

The assertion is made against the **Dockerfile text**, deliberately, rather than against a
reconstructed container: a probe that builds a scratch tree and imports out of it can resolve
through this repo's ``site-packages/_proxyshop.pth``, which pins the primary checkout onto
``sys.path`` for every process using the venv. That is a measured false-green mechanism in this
repository, and a build-recipe claim is exactly the claim that can be settled by reading the
recipe.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

TRUST_DOCKERFILE = REPO_ROOT / "apps/trust/Dockerfile"
TRUST_SEAM = REPO_ROOT / "apps/trust/src/verification/__init__.py"

#: ``COPY [--flag …] <src> <dst>``. The flags are skipped so ``--from=builder`` copies, which
#: name a build stage rather than a repo path, are read for their source like any other.
_COPY = re.compile(r"^\s*COPY\s+(?:--\S+\s+)*(?P<src>\S+)\s+(?P<dst>\S+)\s*$")

#: The import namespace ``packages/verification/src`` is reached through. The distribution ships
#: no files (``bypass-selection = true``), so the package is only ever importable via a
#: ``.pkgroot`` symlink on ``PYTHONPATH`` — copying the source without the link changes nothing.
_NAMESPACE = "claim_verification"


def _copy_sources(dockerfile: Path) -> list[str]:
    return [m.group("src") for line in dockerfile.read_text().splitlines() if (m := _COPY.match(line))]


# =============================================================================================
# T-193 — the trust image ships the seam onto the claim verifier but not the verifier
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-193: apps/trust/Dockerfile copies contracts, proxyshop_support and apps/trust/src "
        "and NOT packages/verification, and links only contracts and trust into /app/.pkgroot, "
        "so trust.verification.verify raises ModuleNotFoundError in the shipped image; remove "
        "this marker with the fix"
    ),
)
def test_the_trust_image_ships_the_verifier_its_own_seam_reaches_for() -> None:
    """T-065's verification engine is absent from the artifact that ships.

    ``apps/trust/src/verification/__init__.py`` exists for exactly one purpose — to reach the
    claim verifier that lives in ``packages/verification`` — and its ``__getattr__`` forwards
    ``verify`` and ``satisfies_hard_constraint`` to it. The image built by
    ``apps/trust/Dockerfile`` contains six COPY sources::

        packages/contracts/__init__.py   packages/contracts/src/
        packages/contracts/schemas/      packages/contracts/generated/python/
        proxyshop_support/               apps/trust/src/

    and links ``/app/.pkgroot/contracts`` and ``/app/.pkgroot/trust`` — no
    ``claim_verification``. So in the deployed container the seam's own ``ModuleNotFoundError``
    is the only thing ``trust.verification.verify`` can produce.

    The lazy PEP-562 import is a real mitigation and this test does not contest it: the
    container starts, ``trust.main:app`` builds, and the failure is deferred from startup to
    first call. What it does not do is put the verifier in the image. Measured against a
    reconstructed image filesystem with ``sys.path`` pinned to ``[/app, /app/.pkgroot]``:
    ``import trust.main`` OK, ``import trust.verification`` OK, ``trust.verification.verify``
    → ``ModuleNotFoundError``.

    Both repairs satisfy this test and it does not choose between them: add the package to the
    image (COPY plus the ``.pkgroot`` symlink, since the distribution ships no files and is
    importable only through that link), or delete the seam that promises a capability the image
    does not have — in which case the guard below makes this test pass vacuously.
    """
    if not TRUST_SEAM.exists():
        pytest.skip("apps/trust no longer ships a verification seam; nothing to reconcile")

    dockerfile = TRUST_DOCKERFILE.read_text()
    sources = _copy_sources(TRUST_DOCKERFILE)

    copied = [src for src in sources if src.startswith("packages/verification")]
    assert copied, (
        "apps/trust ships a seam onto the claim verifier but its image never copies "
        f"packages/verification; COPY sources are {sources}"
    )
    assert _NAMESPACE in dockerfile, (
        f"packages/verification is copied into the image but nothing links it as {_NAMESPACE!r} "
        "on /app/.pkgroot, so it stays unimportable"
    )
