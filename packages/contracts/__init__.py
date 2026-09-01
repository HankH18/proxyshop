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

`.pkgroot` is normally already on `sys.path` (the root `pyproject.toml` puts it there for pytest,
and the root distribution's `dev-mode-dirs` puts it there for an installed environment). It is
added here as a fallback so that importing `packages.contracts` works even from a bare
interpreter with only the repo root on the path — which is exactly the situation the frozen
suite's own bootstrap creates.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_PKGROOT = _Path(__file__).resolve().parents[1] / ".pkgroot"
if _PKGROOT.is_dir() and str(_PKGROOT) not in _sys.path:
    _sys.path.insert(0, str(_PKGROOT))

import contracts as _contracts  # noqa: E402  (the path fallback above must run first)

__all__ = list(_contracts.__all__)
__doc__ = _contracts.__doc__

globals().update({_name: getattr(_contracts, _name) for _name in __all__})


def __getattr__(name: str) -> object:
    """Forward anything not in `__all__` (submodules, private helpers) to `contracts`."""
    try:
        return getattr(_contracts, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(dir(_contracts)))
