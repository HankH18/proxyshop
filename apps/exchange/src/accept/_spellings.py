"""One module object per file, under both dotted spellings of this tree (T-169).

``apps/exchange/src`` is importable under two names — ``exchange.<mod>`` through the tracked
``.pkgroot/exchange`` symlink, which is how :mod:`exchange.main` builds the served app, and
``apps.exchange.src.<mod>``, the repo-root path the frozen acceptance suite and this
package's own wiring docstring use. Left alone, Python EXECUTES every file here a **second
time** under the second name. Measured on this worktree, before this module existed::

    >>> import apps.exchange.src.accept as a, exchange.accept as b
    >>> os.path.realpath(a.__file__) == os.path.realpath(b.__file__)
    True
    >>> os.stat(a.__file__).st_ino == os.stat(b.__file__).st_ino
    True
    >>> a is b
    False
    >>> a.accept is b.accept
    False

Same file, same inode, two module objects — and therefore two copies of every module
global. For this package that is not cosmetic, it is the whole of T-169's second half:

:data:`~exchange.accept.offer._platform_domains`
    the process-wide registered-domain source :func:`~exchange.accept.offer.
    use_registered_domains` writes. It exists **twice**, so an integrator who follows this
    package's own docstring and wires ``apps.exchange.src.accept`` leaves ``exchange.accept``
    — the spelling the served app runs on — completely unwired. Every accept through the
    served app then falls back to ``bid["store_domain"]``, a field the bidding store wrote,
    and mints a real single-use discount code for a host the platform never registered. The
    wiring call reports success; nothing anywhere reports that it bound the wrong process.

``_UNSET``
    the sentinel that distinguishes "the caller passed nothing" from "the caller passed
    ``None``". Two ``object()`` instances are never identical, so a ``registered_domains``
    forwarded across the spellings would compare unequal to the sentinel it was meant to
    match and be passed on to the port *as the platform lookup* — which is exactly how
    :mod:`.gate` first shipped, and how it failed.

Binding is ``setdefault``-shaped throughout: whichever spelling loads first wins, and a
module already registered under a name is never replaced. Only the two spellings of *this*
directory, listed in :data:`SPELLINGS`, are ever touched, and only modules of the accept
package are bound — this is not a general aliasing facility for the exchange.

The direct precedent is ``apps/buyer/svc/src/accept/_spellings.py`` (T-072): the same
package name, the same defect, shipped with ``apps/buyer/svc/tests/test_accept_spellings.py``.
``apps/trust/src/ledger/__init__.py`` solves the same hazard a *different* way — an elected
primary spelling (``_PRIMARY_SPELLING``, ``ledger/__init__.py:119-146``) — because a thread
race defeated the alias approach there; the exchange accept package has no such race (its
binding runs once, at the bottom of a module body, under the import lock), so it follows the
buyer precedent rather than the trust one. T-169's ticket text cites
``apps/trust/src/ledger/__init__.py`` as implementing ``_install_canonical_alias()``; it does
not, and the citation is wrong. The defect it names is real regardless.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

__all__ = ["SPELLINGS", "bind_package", "bind_spellings"]

#: The two dotted roots that name ``apps/exchange/src``. Nothing else may be added here.
SPELLINGS: tuple[str, ...] = ("exchange", "apps.exchange.src")


def _root_of(name: str) -> str | None:
    for root in SPELLINGS:
        if name == root or name.startswith(f"{root}."):
            return root
    return None


def _parent(name: str) -> ModuleType | None:
    """The alias's parent package, imported if it is not loaded yet; ``None`` if it cannot be.

    Importing the parent chain is not optional. ``sys.modules`` is consulted for the FULL
    dotted name before any parent is imported, so registering ``apps.exchange.src.accept``
    while ``apps`` is unimported makes ``import apps.exchange.src.accept`` short-circuit on
    the ``sys.modules`` hit, skip the parent imports entirely, and then die in the attribute
    walk — breaking the very spelling the frozen acceptance suite uses.

    ``None`` is the honest answer when the other spelling is not importable in this process
    at all: a service started with only ``.pkgroot`` on ``sys.path`` cannot reach
    ``apps.exchange.src``, and half-building a parent for it would be worse than leaving
    that spelling unbound.
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

    Package first on purpose: a submodule alias such as ``apps.exchange.src.accept.offer``
    needs its parent present in ``sys.modules`` before the attribute half of the binding can
    be set on it.

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
