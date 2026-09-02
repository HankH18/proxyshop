"""`packages.contracts` — the repo-root dotted path onto this package's flat `src/`.

Two import paths reach the same code and they must reach the SAME objects:

* `contracts` — the flat-src namespace (D42), provided by the tracked `.pkgroot/contracts`
  symlink. This is what product code uses and what `.importlinter` names as a root package.
* `packages.contracts` — the repo-root dotted path. The frozen acceptance suite reaches every
  package this way (`packages.store_agent.src.external`, `apps.exchange.src.ranking`, …), and it
  puts only the repo root on `sys.path` itself.

If this module re-implemented the exports instead of forwarding to `contracts`, the two paths
would produce two distinct sets of classes: `packages.contracts.Bid is not contracts.Bid`, and
`isinstance` across a module boundary would start returning False for objects that are obviously
the same thing. So this file forwards, and there is exactly one `Bid`.

Getting `contracts` importable at all is the first half. `.pkgroot` is usually already on
`sys.path` — the venv's editable install of the root distribution puts it there via
`dev-mode-dirs`, and the root `pyproject.toml` adds it to pytest's `pythonpath` — but neither
mechanism is guaranteed. The frozen acceptance suite runs pytest with `-o pythonpath=` on
purpose (a worker could otherwise prepend a stub directory and shadow the real tree), and it
bootstraps itself with only the repo root on `sys.path`. So this module inserts `.pkgroot`
itself, from `parents[2]` — `parents[0]` is `packages/contracts`, `parents[1]` is `packages`,
and `parents[2]` is the repo root, which is where `.pkgroot` actually lives. Pointing this one
index at `packages` made `is_dir()` permanently False and the fallback dead code; the import
below then raised `ModuleNotFoundError: No module named 'contracts'` in exactly the situation
the fallback exists to cover.

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

import contracts as _contracts  # noqa: E402  (the path fallback above must run first)

__all__ = list(_contracts.__all__)
__doc__ = _contracts.__doc__

globals().update({_name: getattr(_contracts, _name) for _name in __all__})


def _bind_submodules() -> None:
    """Make every spelling of a submodule resolve to ONE module object.

    The flat layout leaves `packages/contracts/src/<module>.py` reachable by two names: as
    `contracts.<module>` through the `.pkgroot` symlink, and — because this package has an
    `__init__.py` and `src/` has one too — as `packages.contracts.src.<module>`. Left alone,
    Python executes those files a SECOND time under the second name, and a consumer that writes
    `from packages.contracts.src.signing import CanonicalisationError` holds a different class
    object from the one `contracts.signing` raises: an `except CanonicalisationError` written
    against one spelling silently fails to catch the other spelling's raise. Both spellings are
    live in this tree (`tests/test_signing_envelope.py` imports the `packages.contracts.src`
    one), so this is measured, not hypothetical.

    Binding the alternative names turns that second import into a cache hit. Both halves matter:

    * `sys.modules` — so `from packages.contracts.src.signing import X` finds the canonical one;
    * the **attribute** on this module — so plain `import packages.contracts.signing` followed by
      `packages.contracts.signing.X` works, which is what the real import machinery does and what
      a `sys.modules` entry alone does NOT give you.

    The names are discovered with :func:`pkgutil.iter_modules` rather than hard-coded, so a module
    added to `src/` later cannot silently miss the treatment.
    """
    root = _sys.modules[__name__]
    _sys.modules.setdefault(f"{__name__}.src", _contracts)
    root.src = _contracts  # type: ignore[attr-defined]
    for info in _pkgutil.iter_modules(_contracts.__path__):
        module = _importlib.import_module(f"contracts.{info.name}")
        _sys.modules.setdefault(f"{__name__}.{info.name}", module)
        _sys.modules.setdefault(f"{__name__}.src.{info.name}", module)
        setattr(root, info.name, module)


_bind_submodules()


def __getattr__(name: str) -> object:
    """Forward anything not in `__all__` (submodules, private helpers) to `contracts`."""
    try:
        return getattr(_contracts, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(dir(_contracts)))
