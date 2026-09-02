"""The ProxyShop ledger: one global hash chain, its canonical form, and its replay.

Owned by T-011 (scope ``apps/trust/src/ledger/**``). Importable both as
``apps.trust.src.ledger`` (repo-root path, which is how the frozen acceptance suite reaches
it) and as ``trust.ledger`` (via the tracked ``.pkgroot/trust`` symlink, which is how member
packages reach each other). Both spellings resolve to this file.

What lives here, and why it all lives in one place
--------------------------------------------------
D16 says: *nothing outside* ``apps/trust/src/ledger/**`` *defines its own hashing.* Two
implementations of "canonical JSON" that disagree about key ordering, about ``1.0`` versus
``1``, or about whether an absent field is ``null`` produce different digests for the same
event -- and the disagreement shows up as a chain that fails verification with no tampering
anywhere. So the canonicaliser, the sealer, the verifier, the Postgres writer and the
replay seam are one package with one rule.

============================  =========================================================
:func:`canonical_json`        RFC-8785 JCS, the exact bytes that get hashed.
:func:`compute_event_hash`    ``sha256(prev_hash || canonical_json(event))`` (D16).
:func:`seal_event`            stamp ``prev_hash`` / ``event_hash`` onto an event.
:func:`verify_chain`          ``{ok, broken_at, reason, head_hash, verified}``.
:func:`chain_head`            the STORED head -- the last event's ``event_hash``.
:func:`stream_hash`           the stream's identity, RECOMPUTED from every event.
:func:`append_event`          the Postgres writer: locked tail, idempotent by event id.
:func:`read_events`           the chain back out, in insertion order.
:func:`verify_chain_in_db`    links **and** the stored anchor, so truncation is caught.
:func:`replay`                ledger stream -> trust snapshots (delegates all scoring).
:func:`apply_migrations`      apply ``db/migrations/*.sql`` to a database.
============================  =========================================================

:func:`chain_head` and :func:`stream_hash` are not the same function and must not be used
interchangeably. ``chain_head`` reads the last row's stored digest -- what the next append
links behind. ``stream_hash`` recomputes the whole chain from genesis. Comparing a stored
head against a stored head proves nothing, which is what "replay reproduces the stream
hash" quietly meant until it was fixed.

Cross-ticket notes
------------------
* **T-060** (``apps/trust/src/events/**``) owns the append-only event store. Its ``append``
  must seal through :func:`seal_event` and take its head hash from :func:`chain_head`:
  :func:`verify_chain` checks a stream against the digests stored *on* its events, so an
  event store that keeps only a running head hash and does not stamp its events leaves the
  verifier with nothing to check. Nothing T-060 needs imports psycopg or redis -- see the
  lazy-import note below, which exists to keep that true.
* **T-062** (``apps/trust/src/scoring/**``) owns the trust maths. D49 puts the ``replay``
  entry point here and the arithmetic there; :mod:`.replay` is the seam and holds no
  scoring. It imports the scorer lazily, so this package is importable before T-062 lands.
* ``verify_chain`` here is the **ledger** verifier. ``packages.verification.verify``
  (T-065) is the *claim* verifier. Different concepts, deliberately not merged.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path as _Path
from types import ModuleType
from typing import Any

from .canonical import (
    CHAIN_FIELDS,
    EVENT_FIELDS,
    GENESIS_HASH,
    CanonicalisationError,
    canonical_bytes,
    canonical_event,
    canonical_json,
    compute_event_hash,
    is_representable_as_double,
    rfc3339_ms,
)
from .chain import (
    ChainIntegrityError,
    chain_events,
    chain_head,
    seal_event,
    stream_hash,
    verify_chain,
)
from .replay import observations_from_events, replay  # D49: the one-line re-export

# --- Everything above is STANDARD LIBRARY ONLY, and that is a requirement --------------
# `.errors` imports psycopg and redis (the latter deliberately, for the CF-2 carve-out
# guard) and `.store` and `.migrations` import psycopg. Importing them here would have made
# the canonicaliser, the sealer and the verifier unimportable without a database driver and
# a Redis client installed -- which is exactly what T-060's in-memory event store needs to
# do, and it needs neither. So the database-facing names load on first use instead (PEP
# 562). `from apps.trust.src.ledger import verify_chain` costs nothing but the stdlib;
# `from apps.trust.src.ledger import append_event` pulls in psycopg, at that moment.
#
# This does not weaken CF-2: import-linter builds its graph by parsing every module in the
# package, not by importing the package, so the `trust.ledger.errors -> redis.exceptions`
# edge the carve-out has to forgive is found either way.
_LAZY: dict[str, str] = {
    "TRANSIENT_DATASTORE_ERRORS": "errors",
    "LedgerError": "errors",
    "is_transient_datastore_error": "errors",
    "MigrationDriftError": "migrations",
    "applied_migrations": "migrations",
    "apply_migrations": "migrations",
    "drifted_migrations": "migrations",
    "migration_files": "migrations",
    "migrations_dir": "migrations",
    "CHAIN_LOCK_KEY": "store",
    "AppendResult": "store",
    "append_event": "store",
    "append_events": "store",
    "chain_anchor": "store",
    "chain_tail": "store",
    "db_stream_hash": "store",
    "head_hash": "store",
    "read_events": "store",
    "verify_chain_in_db": "store",
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
    "CHAIN_FIELDS",
    "CHAIN_LOCK_KEY",
    "EVENT_FIELDS",
    "GENESIS_HASH",
    "TRANSIENT_DATASTORE_ERRORS",
    "AppendResult",
    "CanonicalisationError",
    "ChainIntegrityError",
    "LedgerError",
    "MigrationDriftError",
    "append_event",
    "append_events",
    "applied_migrations",
    "apply_migrations",
    "canonical_bytes",
    "canonical_event",
    "canonical_json",
    "chain_anchor",
    "chain_events",
    "chain_head",
    "chain_tail",
    "compute_event_hash",
    "db_stream_hash",
    "drifted_migrations",
    "head_hash",
    "is_representable_as_double",
    "is_transient_datastore_error",
    "migration_files",
    "migrations_dir",
    "observations_from_events",
    "read_events",
    "replay",
    "rfc3339_ms",
    "seal_event",
    "stream_hash",
    "verify_chain",
    "verify_chain_in_db",
]


# --- One module object per spelling (T-119) --------------------------------------------
# `.pkgroot/trust` is a tracked symlink to `apps/trust/src`, so THIS FILE is reachable
# under two dotted names and Python executes it TWICE: once as `trust.ledger` (how every
# member package reaches the ledger) and once as `apps.trust.src.ledger` (how the frozen
# acceptance suite and this repo's own tests reach it). Left alone, each execution imports
# its own copy of every submodule, and the copies do not share class objects. Measured:
#
#     trust.ledger.canonical is not apps.trust.src.ledger.canonical      -> False
#     trust.ledger.canonical.CanonicalisationError
#         is apps.trust.src.ledger.canonical.CanonicalisationError       -> False
#
# so a `try: ... except CanonicalisationError` written against one spelling silently fails
# to catch what the other raises -- a refusal that reads as a crash, in the one package
# D16 says must be the single source of hashing truth.
#
# T-014 solved the identical problem for `packages/llm` with `_bind_submodules`. This is
# that block, with the two differences the layout forces:
#
# 1. `packages/llm/__init__.py` is a SEPARATE file re-exporting a flat `llm` package, so it
#    always knows which name is canonical. Here one file serves both names, so the binding
#    is SYMMETRIC: whichever spelling loads first publishes its submodules under the other
#    and then imports it, and that second execution's `from .canonical import ...` is a
#    sys.modules cache hit rather than a second read of the file.
# 2. llm's loop imports every submodule eagerly. This one cannot -- `.errors`, `.store` and
#    `.migrations` pull in psycopg and redis, and the `__getattr__` above exists precisely
#    so that importing the canonicaliser needs neither. A submodule that has not been
#    imported yet is therefore published as a LAZY module object: ONE object, shared by
#    both spellings, whose file is not executed until something touches an attribute of it.
#    `import trust.ledger` still costs nothing but the standard library.
#
# What is NOT done, deliberately: the two PACKAGE objects stay distinct. Making them one
# would mean replacing `sys.modules[__name__]` mid-execution, and the alternative spelling
# might not be importable at all (a consumer with only `.pkgroot` on `sys.path` cannot
# reach `apps.`). The property that matters is that the two packages' attributes are the
# same objects, which is what sharing every submodule gives.

#: The dotted names this package answers to. `__name__` is one of them in every supported
#: layout; anything else (someone putting `apps/trust/src` itself on `sys.path`) still gets
#: correct submodule sharing, just no eager import of the two names below.
_SPELLINGS: tuple[str, ...] = ("trust.ledger", "apps.trust.src.ledger")

#: `apps/trust/src/ledger/__init__.py` -> the checkout root. `resolve()` collapses the
#: `.pkgroot` symlink first, so the hop count is the same under both spellings (the same
#: trick `migrations.repo_root` uses, and for the same reason).
_REPO_ROOT = _Path(__file__).resolve().parents[4]

#: The `sys.path` entry each spelling needs. Appended (never prepended) as a fallback when
#: the alternative spelling is not importable, so nothing already on the path is shadowed.
_SPELLING_ROOTS: dict[str, _Path] = {
    "trust.ledger": _REPO_ROOT / ".pkgroot",
    "apps.trust.src.ledger": _REPO_ROOT,
}


def _spellings() -> tuple[str, ...]:
    """Every name this package is reachable under, the executing one first."""
    return (__name__, *(name for name in _SPELLINGS if name != __name__))


def _publish(name: str, module: ModuleType) -> None:
    """Register one submodule object under every spelling of this package.

    Both halves matter, exactly as in T-014's block: the ``sys.modules`` entry is what
    makes ``from apps.trust.src.ledger.store import append_event`` a cache hit, and the
    attribute is what makes plain ``import apps.trust.src.ledger.store`` followed by
    ``apps.trust.src.ledger.store.append_event`` work -- a ``sys.modules`` entry alone does
    not give you that.

    The attribute is NOT set over a non-module export. ``replay`` is both a submodule and
    an exported function, and ``from apps.trust.src.ledger import replay`` has always meant
    the function; ordinary submodule import has the same collision and resolves it the same
    way (the ``from .replay import replay`` above runs after the machinery sets the module
    attribute, and wins).
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

    The standard :class:`importlib.util.LazyLoader` recipe. Returns ``None`` rather than
    raising if the module cannot be located -- a package that binds its spellings must not
    become a package that fails to import.
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
        try:
            importlib.import_module(spelling)
        except ImportError:
            root = str(_SPELLING_ROOTS[spelling])
            if root not in sys.path and _SPELLING_ROOTS[spelling].is_dir():
                sys.path.append(root)
            try:
                importlib.import_module(spelling)
            except ImportError:
                # This checkout cannot reach that spelling at all. Withdraw the entries
                # rather than leave `sys.modules` holding submodules of a package that is
                # not there -- a half-registered name is worse than an absent one.
                for name in names:
                    sys.modules.pop(f"{spelling}.{name}", None)
                continue
        for name in names:
            module = sys.modules.get(f"{spelling}.{name}")
            if module is not None:
                _publish(name, module)


_bind_submodules()
