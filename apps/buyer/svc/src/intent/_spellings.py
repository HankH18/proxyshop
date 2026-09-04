"""One module object per file, under both dotted spellings of this tree (T-071).

``apps/buyer/svc/src`` is importable under two names: ``buyer_svc.<mod>`` through the
tracked ``.pkgroot/buyer_svc`` symlink, and ``apps.buyer.svc.src.<mod>`` — the repo-root
path the frozen acceptance suite uses. Left alone, Python EXECUTES each file a **second
time** under the second name, and the two spellings then disagree about object identity::

    >>> import apps.buyer.svc.src.intent as a, buyer_svc.intent as b
    >>> a is b
    False                      # MEASURED on this worktree, before this module existed

That is not a curiosity. Two consequences, both silent:

* ``except ConfirmationWithheld`` imported through one spelling does **not** catch the
  ``ConfirmationWithheld`` the other spelling raises. Same file, same line, two classes.
  A refusal a caller wrote code to handle escapes as an unhandled exception instead.
* the module-level :data:`~buyer_svc.intent.confirmation._LEDGER` exists **twice**, so
  "one confirmation opens one auction" holds *per spelling*. A process that reaches
  ``confirm`` both ways opens two auctions for one intent with every assertion still
  green — and a pytest session running the frozen acceptance suite
  (``apps.buyer.svc.src.intent``) beside the FastAPI app (``buyer_svc.intent``) is exactly
  such a process.

``packages/llm/__init__.py`` documents the same trap and fixes it the same way. Both
halves matter, and both are asserted in ``tests/test_intent_spellings.py``:

* ``sys.modules``, so ``from <the other spelling> import X`` is a cache hit rather than a
  second execution of the file;
* the **attribute** on the parent package, which is what plain ``import a.b.c`` followed
  by ``a.b.c.X`` needs, and which a ``sys.modules`` entry alone does not give you.

Binding is ``setdefault``-shaped throughout: whichever spelling loads first wins, and a
module already registered under a name is never replaced. This module deliberately touches
only the two spellings of *this* directory, listed in :data:`SPELLINGS`.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

__all__ = ["SPELLINGS", "bind_package", "bind_spellings"]

#: The two dotted roots that name ``apps/buyer/svc/src``. Both resolve to that directory:
#: the first through the tracked ``.pkgroot/buyer_svc`` symlink, the second through the
#: repo root. Nothing else may be added here — this is not a general aliasing facility.
SPELLINGS: tuple[str, ...] = ("buyer_svc", "apps.buyer.svc.src")


def _root_of(name: str) -> str | None:
    for root in SPELLINGS:
        if name == root or name.startswith(f"{root}."):
            return root
    return None


def _parent(name: str) -> ModuleType | None:
    """The alias's parent package, imported if it is not loaded yet; ``None`` if it cannot be.

    Importing the parent chain is NOT optional, and getting this wrong is measured rather
    than imagined. ``sys.modules`` is consulted for the FULL dotted name before any parent
    is imported, so registering ``apps.buyer.svc.src.intent`` while ``apps`` is unimported
    makes ``import apps.buyer.svc.src.intent as x`` short-circuit on the ``sys.modules``
    hit, skip the parent imports entirely, and then die in the attribute walk with
    ``ImportError: cannot import name 'buyer' from 'apps'``. An earlier revision of this
    file did exactly that and broke the very spelling the frozen acceptance suite uses,
    but only when ``buyer_svc.intent`` happened to be imported first.

    ``None`` is the honest answer when the other spelling is not importable in this process
    at all — a service started with only ``.pkgroot`` on ``sys.path`` cannot reach
    ``apps.buyer.svc.src``, and inventing a half-built parent for it would be worse than
    leaving that spelling unbound.
    """
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - an unreachable spelling is simply not bound
        return None


def bind_spellings(module: ModuleType) -> tuple[str, ...]:
    """Register ``module`` under every other spelling of its own name.

    Returns the aliases newly registered, which is what the tests assert on. A module
    outside :data:`SPELLINGS` is left alone and reported as binding nothing.
    """
    name = getattr(module, "__name__", "")
    root = _root_of(name)
    if root is None:
        return ()
    suffix = name[len(root) :]

    bound: list[str] = []
    for other in SPELLINGS:
        if other == root:
            continue
        alias = f"{other}{suffix}"
        if sys.modules.get(alias) is module:
            continue
        parent_name, _, leaf = alias.rpartition(".")
        parent = _parent(parent_name) if parent_name else None
        if parent is None:
            continue
        if sys.modules.setdefault(alias, module) is not module:
            # Something else already owns that name — a second execution that beat us to
            # it. Leave it; replacing a live module is worse than the duplication.
            continue
        bound.append(alias)
        if getattr(parent, leaf, None) is None:
            setattr(parent, leaf, module)
    return tuple(bound)


def bind_package(package_name: str) -> tuple[str, ...]:
    """Bind a package **and every submodule of it already loaded**, package first.

    Package first on purpose: a submodule alias such as
    ``apps.buyer.svc.src.intent.clarifier`` needs its parent present in ``sys.modules``
    before the attribute half of the binding can be set on it.

    Only modules already imported are bound. A submodule imported later (``routes``, which
    is loaded by the frozen entrypoint and drags FastAPI in with it) binds itself at the
    bottom of its own file, so importing this package costs no web framework.
    """
    package = sys.modules.get(package_name)
    if package is None:
        return ()
    bound = list(bind_spellings(package))
    prefix = f"{package_name}."
    for name in [key for key in sys.modules if key.startswith(prefix)]:
        submodule = sys.modules.get(name)
        if submodule is not None:
            bound.extend(bind_spellings(submodule))
    return tuple(bound)
