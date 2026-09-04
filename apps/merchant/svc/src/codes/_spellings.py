"""One module object per file, under both dotted spellings of this package (T-052).

``apps/merchant/svc/src`` is importable under two names: ``merchant_svc.<mod>`` through the
tracked ``.pkgroot/merchant_svc`` symlink, and ``apps.merchant.svc.src.<mod>`` — the
repo-root path the frozen acceptance suite uses. Left alone, Python EXECUTES each file a
**second time** under the second name, and the two spellings then disagree about object
identity. Measured on this worktree, before this module existed::

    >>> import apps.merchant.svc.src.envelope as a, merchant_svc.envelope as b
    >>> a is b
    False

For this package that is not a curiosity, it is the ticket's own guarantee failing:

* the module-level :data:`~merchant_svc.codes.redemption.REDEMPTIONS` register would exist
  **twice**, so "a minted code may be redeemed once" would hold *per spelling*. A process
  that reaches :func:`~merchant_svc.codes.redemption.on_redemption` both ways — a pytest
  session running the frozen acceptance suite (``apps.merchant.svc.src.codes``) beside the
  FastAPI app (``merchant_svc.codes``) is exactly such a process — would redeem one
  single-use code twice with every assertion still green.
* ``except DiscountCodeRefused`` imported through one spelling would not catch the
  ``DiscountCodeRefused`` the other spelling raises. Same file, same line, two classes.

``apps/buyer/svc/src/intent/_spellings.py`` (T-071) and ``packages/llm/__init__.py``
document the same trap and fix it the same way, and both halves matter:

* ``sys.modules``, so ``from <the other spelling> import X`` is a cache hit rather than a
  second execution of the file;
* the **attribute** on the parent package, which is what plain ``import a.b.c`` followed by
  ``a.b.c.X`` needs, and which a ``sys.modules`` entry alone does not give you.

Binding is ``setdefault``-shaped throughout: whichever spelling loads first wins, and a
module already registered under a name is never replaced. This module deliberately touches
only the two spellings of *this* package, listed in :data:`SPELLINGS` — the rest of the
merchant service is not ours to alias.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

__all__ = ["SPELLINGS", "bind_package", "bind_spellings"]

#: The two dotted roots that name ``apps/merchant/svc/src/codes``. Both resolve to that
#: directory: the first through the tracked ``.pkgroot/merchant_svc`` symlink, the second
#: through the repo root. Nothing else may be added here — this is not a general aliasing
#: facility, and this ticket owns only ``codes/``.
SPELLINGS: tuple[str, ...] = ("merchant_svc.codes", "apps.merchant.svc.src.codes")


def _root_of(name: str) -> str | None:
    for root in SPELLINGS:
        if name == root or name.startswith(f"{root}."):
            return root
    return None


def _parent(name: str) -> ModuleType | None:
    """The alias's parent package, imported if it is not loaded yet; ``None`` if it cannot be.

    Importing the parent chain is NOT optional. ``sys.modules`` is consulted for the FULL
    dotted name before any parent is imported, so registering
    ``apps.merchant.svc.src.codes`` while ``apps`` is unimported would make
    ``import apps.merchant.svc.src.codes`` short-circuit on the ``sys.modules`` hit, skip
    the parent imports entirely, and then die in the attribute walk.

    ``None`` is the honest answer when the other spelling is not importable in this process
    at all — a service started with only ``.pkgroot`` on ``sys.path`` cannot reach
    ``apps.merchant.svc.src``, and inventing a half-built parent for it would be worse than
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
    ``apps.merchant.svc.src.codes.redemption`` needs its parent present in ``sys.modules``
    before the attribute half of the binding can be set on it.

    Only modules already imported are bound. A submodule imported later (``routes``, which
    drags FastAPI in with it) binds itself at the bottom of its own file, so importing this
    package costs no web framework.
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
