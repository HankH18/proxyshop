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

from typing import Any

from .canonical import (
    CHAIN_FIELDS,
    EVENT_FIELDS,
    GENESIS_HASH,
    MAX_SAFE_INTEGER,
    CanonicalisationError,
    canonical_bytes,
    canonical_event,
    canonical_json,
    compute_event_hash,
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
    import importlib

    return getattr(importlib.import_module(f".{module_name}", __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "CHAIN_FIELDS",
    "CHAIN_LOCK_KEY",
    "EVENT_FIELDS",
    "GENESIS_HASH",
    "MAX_SAFE_INTEGER",
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
