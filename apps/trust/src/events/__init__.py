"""The ProxyShop ledger **writer**: every event lands once, chained, and replays exactly.

Owned by T-060 (scope ``apps/trust/src/events/**``). Built *over* T-011's
:mod:`trust.ledger`, never beside it: D16 says nothing outside ``apps/trust/src/ledger/**``
defines its own hashing, so this package validates, stores, serves and verifies -- and
delegates every digest, every seal and every link check to the ledger library.

=================================  ====================================================
:class:`InMemoryEventStore`        an append-only hash-chained log with no datastore.
:func:`append`                     ``append(store, event)`` -- the one entry point.
:func:`normalise_event`            the one place an event becomes a hashable body.
:data:`LEDGER_EVENT_KINDS`         the frozen 18-kind vocabulary (C11/D24).
:class:`PostgresEventStore`        the same contract over ``ledger.commerce_events``.
:func:`verify_stream`              ``verify_chain`` + the identity of the broken link.
:func:`anchored_report`            links **and** the outside witness, in one verdict.
:data:`router`                     the ``/events`` HTTP surface (mounted by ``main.py``).
:func:`create_events_app`          a standalone app serving only the writer.
=================================  ====================================================

The three guarantees, and where each one actually lives
------------------------------------------------------
**Lands once.** ``event_id`` IS the idempotency key (D16). In Postgres the guarantee is
``commerce_events_idempotency_key_key``, a UNIQUE constraint enforced against every writer
that has ever touched the table -- not a check-then-insert in this process, which two
threads race and two workers do not share at all. :mod:`.pg` recognises that constraint
firing and turns it into the no-op it is.

**Chained.** Appends seal through :func:`trust.ledger.seal_event` and link behind
:func:`trust.ledger.chain_head`, so every stored event carries the ``prev_hash`` /
``event_hash`` a verifier can check. A store that kept only a running head hash and did not
stamp its events would leave :func:`trust.ledger.verify_chain` with nothing to check --
recomputing a chain over mutated events just yields a different, internally consistent
chain. :mod:`.integrity` turns a break into the *name* of the event that broke it.

**Replays exactly.** :meth:`PostgresEventStore.replay` reads every row out of the ledger on
every call and recomputes the stream hash from content. Nothing is cached: an endpoint
serving a list it has been holding since the write would reproduce the writer's memory
rather than the ledger, and would keep doing so after the ledger was emptied.

Import cost
-----------
``from apps.trust.src.events import InMemoryEventStore, append`` costs the standard library
plus the stdlib-only half of :mod:`trust.ledger`. ``psycopg`` and ``fastapi`` load on first
use of a name that needs them (PEP 562), exactly as the ledger package does and for the
same reason: the in-memory store needs neither, and the frozen acceptance suite imports it
on that basis.

The two spellings
-----------------
``.pkgroot/trust`` symlinks to ``apps/trust/src``, so this file is reachable as
``trust.events`` (how member packages and ``trust.main``'s router discovery reach it) and
as ``apps.trust.src.events`` (how the frozen acceptance suite reaches it). Left alone,
Python executes it twice and each execution builds its own copy of every submodule -- so
``except IdempotencyConflict`` written against one spelling silently fails to catch what
the other raises. :mod:`trust.ledger` solved this in T-119/T-126; the block at the bottom
of this file is the same solution, with the same elected-primary sequencing, and
``apps/trust/tests/test_events.py`` grades it. See ``apps/trust/src/ledger/__init__.py``
for the full measured rationale rather than a second copy of it here.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path as _Path
from types import ModuleType
from typing import Any

#: The dotted names this package answers to, primary first.
_SPELLINGS: tuple[str, ...] = ("trust.events", "apps.trust.src.events")

#: The spelling allowed to execute without waiting for anyone (T-126's elected primary).
_PRIMARY_SPELLING = _SPELLINGS[0]


def _sequence_behind_the_primary_spelling() -> None:
    """Block until the primary spelling has finished executing, then do nothing.

    Waiting is the whole job: by the time the primary returns it has already published
    every submodule under BOTH spellings, so the eager imports below are ``sys.modules``
    cache hits and no second copy is built. A no-op for the primary itself, and for a
    checkout that cannot reach it at all.
    """
    if __name__ == _PRIMARY_SPELLING or __name__ not in _SPELLINGS:
        return
    try:
        importlib.import_module(_PRIMARY_SPELLING)
    except ImportError:
        return


_sequence_behind_the_primary_spelling()

# E402 below is the point of the call above, not an oversight: the sequencing has to run
# BEFORE the first relative import, because it is the eager imports that build the second
# copy of every submodule.
from .errors import (  # noqa: E402
    BrokenChain,
    ChainForked,
    EventServiceError,
    IdempotencyConflict,
    InvalidEvent,
    StoreUnavailable,
    UnknownEventKind,
)
from .integrity import anchored_report, describe_break, verify_stream  # noqa: E402
from .store import (  # noqa: E402
    LEDGER_EVENT_KINDS,
    AppendOutcome,
    InMemoryEventStore,
    append,
    normalise_event,
)

# --- Everything above is standard library + the stdlib-only half of `trust.ledger` -----
# `.pg` imports psycopg (through `trust.ledger.store`) and `.routes`/`.service` import
# fastapi. Importing them here would make the in-memory store unimportable without a
# database driver and a web framework, which is exactly what T-060 must not require. So
# those names load on first use instead (PEP 562), the same arrangement `trust.ledger`
# makes for its own database-facing half.
_LAZY: dict[str, str] = {
    "DEFAULT_DSN_ENV": "pg",
    "PostgresEventStore": "pg",
    "default_store": "pg",
    "EventIn": "routes",
    "router": "routes",
    "store_for": "routes",
    "create_events_app": "service",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = _submodule(module_name)
    if module is None:  # pragma: no cover - only if the file vanished under us
        module = importlib.import_module(f".{module_name}", __name__)
    _publish(module_name, module)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "DEFAULT_DSN_ENV",
    "LEDGER_EVENT_KINDS",
    "AppendOutcome",
    "BrokenChain",
    "ChainForked",
    "EventIn",
    "EventServiceError",
    "IdempotencyConflict",
    "InMemoryEventStore",
    "InvalidEvent",
    "PostgresEventStore",
    "StoreUnavailable",
    "UnknownEventKind",
    "anchored_report",
    "append",
    "create_events_app",
    "default_store",
    "describe_break",
    "normalise_event",
    "router",
    "store_for",
    "verify_stream",
]


# --- One module object per spelling (T-119/T-126, applied to this package) --------------
#: `apps/trust/src/events/__init__.py` -> the checkout root. `resolve()` collapses the
#: `.pkgroot` symlink first, so the hop count is the same under both spellings.
_REPO_ROOT = _Path(__file__).resolve().parents[4]

#: The `sys.path` entry each spelling needs, lent for the attempt and taken back after.
_SPELLING_ROOTS: dict[str, _Path] = {
    "trust.events": _REPO_ROOT / ".pkgroot",
    "apps.trust.src.events": _REPO_ROOT,
}


def _spellings() -> tuple[str, ...]:
    """Every name this package is reachable under, the executing one first."""
    return (__name__, *(name for name in _SPELLINGS if name != __name__))


def _publish(name: str, module: ModuleType) -> None:
    """Register one submodule object under every spelling of this package.

    Both halves matter: the ``sys.modules`` entry makes
    ``from apps.trust.src.events.store import append`` a cache hit, and the attribute makes
    plain ``import apps.trust.src.events.store`` followed by attribute access work -- a
    ``sys.modules`` entry alone does not give you that. The attribute is not set over a
    non-module export, because ``store`` is both a submodule and (in other packages) an
    exported name, and the exported meaning must win.
    """
    for spelling in _spellings():
        sys.modules.setdefault(f"{spelling}.{name}", module)
        package = sys.modules.get(spelling)
        if package is None:
            continue
        current = getattr(package, name, None)
        if current is None or isinstance(current, ModuleType):
            setattr(package, name, module)


def _lazy_submodule(name: str) -> ModuleType | None:
    """A module object for ``name`` whose file has not been executed yet.

    The standard :class:`importlib.util.LazyLoader` recipe, so ``.pg`` can be published
    under both spellings without importing psycopg. Returns ``None`` rather than raising: a
    package that binds its spellings must not become a package that fails to import.
    """
    fullname = f"{__name__}.{name}"
    try:
        spec = importlib.util.find_spec(fullname)
        if spec is None or spec.loader is None:
            return None
        loader = importlib.util.LazyLoader(spec.loader)
        spec.loader = loader
        module = importlib.util.module_from_spec(spec)
        # `sys.modules` first: `_LazyModule` refuses to execute if the name it was created
        # under no longer resolves to it, which is what makes the lazy object safe to share.
        sys.modules[fullname] = module
        loader.exec_module(module)
    except (AttributeError, ImportError, TypeError, ValueError):  # pragma: no cover
        sys.modules.pop(fullname, None)
        return None
    return module


def _submodule(name: str) -> ModuleType | None:
    """The one module object for ``name``, whichever spelling first created it."""
    for spelling in _spellings():
        existing = sys.modules.get(f"{spelling}.{name}")
        if existing is not None:
            return existing
    return _lazy_submodule(name)


def _import_alternative_spelling(spelling: str) -> bool:
    """Import ``spelling``, lending it its ``sys.path`` root only for the attempt.

    The loan is real -- a consumer whose path holds only ``.pkgroot`` cannot otherwise
    create the ``apps.`` name, and a *later* import of it would execute this file a second
    time and rebuild the duplicates the binding exists to prevent. Paying for that with a
    permanent change to ``sys.path`` is not acceptable: ``import trust.events`` would
    silently make the entire repository root importable for the rest of the process.
    Appended (never prepended, so nothing already on the path is shadowed) and removed
    again in a ``finally``.
    """
    try:
        importlib.import_module(spelling)
    except ImportError:
        pass
    else:
        return True

    root = _SPELLING_ROOTS.get(spelling)
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


def _bind_submodules() -> None:
    """Make every spelling of every submodule resolve to ONE module object."""
    names = [info.name for info in pkgutil.iter_modules(__path__)]
    for name in names:
        module = _submodule(name)
        if module is not None:
            _publish(name, module)

    if __name__ not in _SPELLINGS:  # pragma: no cover - not a supported layout
        return
    for spelling in _SPELLINGS:
        if spelling == __name__ or spelling in sys.modules:
            continue
        if not _import_alternative_spelling(spelling):
            # This checkout cannot reach that spelling at all. Withdraw the entries rather
            # than leave `sys.modules` holding submodules of a package that is not there --
            # a half-registered name is worse than an absent one.
            for name in names:
                sys.modules.pop(f"{spelling}.{name}", None)
            continue
        for name in names:
            module = sys.modules.get(f"{spelling}.{name}")
            if module is not None:
                _publish(name, module)


_bind_submodules()
