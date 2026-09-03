"""One module object per submodule, whichever dotted spelling reached it first.

``.pkgroot/trust`` is a tracked symlink to ``apps/trust/src``, so every file in this feature
package is reachable under TWO dotted names:

* ``trust.<feature>.<module>`` — how member packages and ``trust.main``'s router discovery
  reach it, and the spelling ``apps/trust/src/ledger/replay.py`` tries FIRST when it looks
  for the scorer (``SCORING_MODULES = ("trust.scoring", "apps.trust.src.scoring")``);
* ``apps.trust.src.<feature>.<module>`` — how the frozen acceptance suite reaches it.

Left alone, Python executes each file once per name and the two executions do not share
class objects. That is not a theoretical worry in this package: ``replay()`` calls the
scorer under the first spelling while the acceptance suite imports it under the second, so
``except UnmappedClaimType`` written against one would silently stop catching what the other
raises — the exact failure T-119/T-126 measured and fixed for ``trust.ledger`` and
``trust.events``.

This module is that fix, reduced to the shape a stdlib-only feature package needs. Two
differences from ``apps/trust/src/ledger/__init__.py``, both deliberate:

1. **Eager, not lazy.** The ledger publishes un-executed ``LazyLoader`` module objects
   because ``.store``/``.migrations`` drag in psycopg. Nothing here imports anything outside
   the standard library, so the submodules are simply imported and published. ``import
   trust.scoring`` still costs nothing but the stdlib and one small JSON read.
2. **Parameterised, not per-package.** The package passes its own ``__name__``,
   ``__path__`` and spellings in, so the four feature packages that need this carry one copy
   of the logic each rather than four divergent hand-edits of the same 150 lines.

The *elected primary* half of T-126 cannot live here — it has to run before the first
relative import, which is what would load this file — so each package inlines those five
lines at the top of its own ``__init__.py`` and calls :func:`bind_submodules` at the bottom.

What is NOT done, deliberately (same ruling as the ledger): the two PACKAGE objects stay
distinct. Making them one means replacing ``sys.modules[__name__]`` mid-execution, and the
alternative spelling may not be importable at all (a consumer holding only ``.pkgroot`` on
``sys.path`` cannot reach ``apps.``). The property that matters is that the two packages'
attributes are the same objects, which sharing every submodule gives.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import ModuleType

__all__ = ["bind_submodules", "publish", "spellings_of"]


def spellings_of(package_name: str, spellings: Iterable[str]) -> tuple[str, ...]:
    """Every name the package is reachable under, the executing one first."""
    return (package_name, *(name for name in spellings if name != package_name))


def publish(package_name: str, spellings: Iterable[str], sub_name: str, module: ModuleType) -> None:
    """Register one submodule object under every spelling of its package.

    Both halves matter, exactly as in the ledger's block:

    * the ``sys.modules`` entry is what makes ``from apps.trust.src.scoring.blacklist import
      Blacklist`` a cache hit rather than a second execution of the file;
    * the **attribute** on the package object is what makes plain ``import
      apps.trust.src.scoring.blacklist`` followed by ``...blacklist.Blacklist`` work, which
      a ``sys.modules`` entry alone does NOT give you.

    The attribute is never set over a non-module export, so a package that exports a
    function with the same name as one of its submodules keeps the function.
    """
    for spelling in spellings_of(package_name, spellings):
        sys.modules.setdefault(f"{spelling}.{sub_name}", module)
        package = sys.modules.get(spelling)
        if package is None:
            continue
        current = getattr(package, sub_name, None)
        if current is None or isinstance(current, ModuleType):
            setattr(package, sub_name, module)


def _import_alternative_spelling(spelling: str, root: Path | None) -> bool:
    """Import ``spelling``, lending it its ``sys.path`` root only for the attempt.

    The path help is real: a consumer whose ``sys.path`` holds only ``.pkgroot`` can import
    ``trust.scoring`` but not ``apps.trust.src.scoring``, and without the fallback the second
    name is never created — so a *later* import of it, once something else has put the repo
    root on the path, executes this package a second time and rebuilds exactly the duplicate
    submodules this file exists to prevent.

    The entry is appended (never prepended, so nothing already on the path is shadowed) and
    removed again in a ``finally``: after this returns ``sys.path`` is what it was. Nothing
    needs it afterwards — the package and its parents are in ``sys.modules``, and submodules
    resolve through ``__path__``, which is an absolute filesystem path.
    """
    try:
        importlib.import_module(spelling)
    except ImportError:
        pass
    else:
        return True

    if root is None or not root.is_dir() or str(root) in sys.path:
        return False
    entry = str(root)
    sys.path.append(entry)
    try:
        importlib.import_module(spelling)
    except ImportError:
        return False
    finally:
        for index in range(len(sys.path) - 1, -1, -1):
            if sys.path[index] == entry:
                del sys.path[index]
                break
    return True


def bind_submodules(
    package_name: str,
    package_path: Iterable[str],
    spellings: tuple[str, ...],
    spelling_roots: Mapping[str, Path],
) -> None:
    """Make every spelling of every submodule of this package resolve to ONE object.

    Call this from the BOTTOM of the package's ``__init__.py``, after its eager imports:
    every submodule is then already in ``sys.modules`` under the executing spelling, and
    this only has to publish those objects under the other one.

    Args:
        package_name: the executing package's ``__name__``.
        package_path: the executing package's ``__path__``.
        spellings: every dotted name this package is reachable under.
        spelling_roots: the ``sys.path`` entry each spelling needs, used only as a fallback
            when that spelling is not importable from the path the interpreter already has.
    """
    names = [info.name for info in pkgutil.iter_modules(list(package_path))]
    for name in names:
        module = sys.modules.get(f"{package_name}.{name}")
        if module is None:
            try:
                module = importlib.import_module(f"{package_name}.{name}")
            except ImportError:  # pragma: no cover - a submodule that vanished under us
                continue
        publish(package_name, spellings, name, module)

    if package_name not in spellings:  # pragma: no cover - not a supported layout
        return
    for spelling in spellings:
        if spelling == package_name or spelling in sys.modules:
            continue
        if not _import_alternative_spelling(spelling, spelling_roots.get(spelling)):
            # This checkout cannot reach that spelling at all. Withdraw the entries rather
            # than leave `sys.modules` holding submodules of a package that is not there —
            # a half-registered name is worse than an absent one.
            for name in names:
                sys.modules.pop(f"{spelling}.{name}", None)
            continue
        for name in names:
            module = sys.modules.get(f"{spelling}.{name}")
            if module is not None:
                publish(package_name, spellings, name, module)
