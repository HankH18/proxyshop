"""`packages.verification` — the repo-root dotted path onto this package's flat `src/`.

Two import paths reach the same code and they must reach the SAME objects:

* `claim_verification` — the flat-src namespace (D42), provided by the tracked
  `.pkgroot/claim_verification` symlink. This is what product code uses and what
  `.importlinter` names as a root package.
* `packages.verification` — the repo-root dotted path. The frozen acceptance suite reaches
  every package this way (`from packages.verification import verify`), and it puts only the
  repo root on `sys.path` itself.

Without this file the second spelling resolves as a PEP 420 *namespace* package with no
attributes at all, and `from packages.verification import verify` fails with the peculiarly
unhelpful `cannot import name 'verify' from 'packages.verification' (unknown location)` — the
directory exists, so nothing suggests the missing piece is an `__init__.py`.

If this module re-implemented the exports instead of forwarding, the two paths would produce
two distinct sets of classes: `packages.verification.ComparisonOutcome is not
claim_verification.ComparisonOutcome`, and an `except InvalidVerificationStatus` written
against one spelling would silently stop catching the other's raise. So this file forwards,
and there is exactly one of everything. It is `packages/contracts/__init__.py`'s convention,
deliberately not a second design.

Getting `claim_verification` importable at all is the first half. `.pkgroot` is usually
already on `sys.path` — the venv's editable install of the root distribution puts it there via
`dev-mode-dirs`, and the root `pyproject.toml` adds it to pytest's `pythonpath` — but neither
is guaranteed: the frozen acceptance suite runs pytest with `-o pythonpath=` on purpose (a
worker could otherwise prepend a stub directory and shadow the real tree) and bootstraps
itself with only the repo root on `sys.path`. So this module inserts `.pkgroot` itself, from
`parents[2]` — `parents[0]` is `packages/verification`, `parents[1]` is `packages`, and
`parents[2]` is the repo root, which is where `.pkgroot` actually lives.

Binding the submodules is the second half — see `_bind_submodules`.
"""

from __future__ import annotations

import importlib as _importlib
import pkgutil as _pkgutil
import sys as _sys
from pathlib import Path as _Path

_PKGROOT = _Path(__file__).resolve().parents[2] / ".pkgroot"
if _PKGROOT.is_dir() and str(_PKGROOT) not in _sys.path:
    _sys.path.insert(0, str(_PKGROOT))

import claim_verification as _claim_verification  # noqa: E402  (the path fallback must run first)

__all__ = list(_claim_verification.__all__)
__doc__ = _claim_verification.__doc__

globals().update({_name: getattr(_claim_verification, _name) for _name in __all__})


def _bind_submodules() -> None:
    """Make every spelling of a submodule resolve to ONE module object.

    The flat layout leaves `packages/verification/src/<module>.py` reachable by two names: as
    `claim_verification.<module>` through the `.pkgroot` symlink, and — because this package
    has an `__init__.py` and `src/` has one too — as `packages.verification.src.<module>`.
    Left alone, Python executes those files a SECOND time under the second name, and a
    consumer holding `packages.verification.src.statuses.InvalidVerificationStatus` has a
    different class object from the one `claim_verification.statuses` raises.

    Binding the alternative names turns that second import into a cache hit. Both halves
    matter:

    * `sys.modules` — so `from packages.verification.src.comparators import compare` finds
      the canonical module;
    * the **attribute** on this module — so plain `import packages.verification.comparators`
      followed by `packages.verification.comparators.compare` works, which is what the real
      import machinery does and what a `sys.modules` entry alone does NOT give you.

    The names are discovered with :func:`pkgutil.iter_modules` rather than hard-coded, so a
    module added to `src/` later cannot silently miss the treatment.
    """
    root = _sys.modules[__name__]
    _sys.modules.setdefault(f"{__name__}.src", _claim_verification)
    root.src = _claim_verification  # type: ignore[attr-defined]
    for info in _pkgutil.iter_modules(_claim_verification.__path__):
        module = _importlib.import_module(f"claim_verification.{info.name}")
        _sys.modules.setdefault(f"{__name__}.{info.name}", module)
        _sys.modules.setdefault(f"{__name__}.src.{info.name}", module)
        setattr(root, info.name, module)


_bind_submodules()


def __getattr__(name: str) -> object:
    """Forward anything not in `__all__` (submodules, private helpers) to the flat package."""
    try:
        return getattr(_claim_verification, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(dir(_claim_verification)))
