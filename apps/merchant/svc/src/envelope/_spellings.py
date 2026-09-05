"""One module object per file, under both dotted spellings of this package (T-243).

``apps/merchant/svc/src`` is importable under two names: ``merchant_svc.<mod>`` through the
tracked ``.pkgroot/merchant_svc`` symlink, and ``apps.merchant.svc.src.<mod>`` — the
repo-root path the frozen E5 acceptance suite uses. Left alone, Python EXECUTES each file a
**second time** under the second name, and the two spellings then disagree about object
identity. Measured on this worktree before this module existed::

    >>> import apps.merchant.svc.src.envelope.store as a, merchant_svc.envelope.store as b
    >>> a is b, a.ENVELOPES is b.ENVELOPES
    (False, False)

For this package that is not a curiosity, it is the sealed envelope's own guarantee failing:

* :data:`~merchant_svc.envelope.store.ENVELOPES` — the append-only history that answers
  "may this store bid" — would exist **twice**. A kill recorded through one spelling leaves
  the other spelling's copy of the same store still ``active``, and the kill switch is the
  one control that has to work when everything else is wrong.
* ``except UnknownStore`` / ``VersionWentBackwards`` / ``StoreMismatch`` imported through one
  spelling would not catch the class the other spelling raises. Same file, same line, two
  classes — so a 409 the route promises becomes an unhandled 500.
* the version-never-backwards rule is enforced per history, so two histories means a stale
  writer's v3 lands cleanly beside another spelling's v4.

``apps/merchant/svc/src/codes/_spellings.py`` (T-052) and
``apps/buyer/svc/src/intent/_spellings.py`` (T-071) document the same trap and fix it the
same way, and both halves matter:

* ``sys.modules``, so ``from <the other spelling> import X`` is a cache hit rather than a
  second execution of the file;
* the **attribute** on the parent package, which is what plain ``import a.b.c`` followed by
  ``a.b.c.X`` needs, and which a ``sys.modules`` entry alone does not give you.

The other half of the repair lives in the package's own modules: every intra-package import
here is **relative**. An absolute ``from merchant_svc.envelope.model import ...`` inside a
file being executed under the *long* spelling re-enters the short package half-way through
the long one's ``__init__``, and the binding below then finds a partially-initialized module
already sitting under the name it wants and correctly declines to replace it — leaving the
package split after all. Relative imports keep whichever spelling loaded first as the only
one that ever executes.

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

#: The two dotted roots that name ``apps/merchant/svc/src/envelope``. Both resolve to that
#: directory: the first through the tracked ``.pkgroot/merchant_svc`` symlink, the second
#: through the repo root. Nothing else may be added here — this is not a general aliasing
#: facility, and this ticket owns only ``envelope/``.
SPELLINGS: tuple[str, ...] = ("merchant_svc.envelope", "apps.merchant.svc.src.envelope")


def _root_of(name: str) -> str | None:
    for root in SPELLINGS:
        if name == root or name.startswith(f"{root}."):
            return root
    return None


def _parent(name: str) -> ModuleType | None:
    """The alias's parent package, imported if it is not loaded yet; ``None`` if it cannot be.

    Importing the parent chain is NOT optional. ``sys.modules`` is consulted for the FULL
    dotted name before any parent is imported, so registering
    ``apps.merchant.svc.src.envelope`` while ``apps`` is unimported would make
    ``import apps.merchant.svc.src.envelope`` short-circuit on the ``sys.modules`` hit, skip
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
    ``apps.merchant.svc.src.envelope.store`` needs its parent present in ``sys.modules``
    before the attribute half of the binding can be set on it.

    Only modules already imported are bound. The envelope package's ``__init__`` imports
    every one of its modules before it calls this, so "already imported" is "all of them".
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
