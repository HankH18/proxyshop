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
:func:`verify_chain`          ``{ok, broken_at, reason, head_hash}`` over a stream.
:func:`stream_hash`           the stream's identity -- the chain head.
:func:`append_event`          the Postgres writer: locked tail, idempotent by event id.
:func:`read_events`           the chain back out, in insertion order.
:func:`replay`                ledger stream -> trust snapshots (delegates all scoring).
:func:`apply_migrations`      apply ``db/migrations/*.sql`` to a database.
============================  =========================================================

Cross-ticket notes
------------------
* **T-060** (``apps/trust/src/events/**``) owns the append-only event store. Its ``append``
  must seal through :func:`seal_event` and take its head hash from :func:`chain_head`:
  :func:`verify_chain` checks a stream against the digests stored *on* its events, so an
  event store that keeps only a running head hash and does not stamp its events leaves the
  verifier with nothing to check.
* **T-062** (``apps/trust/src/scoring/**``) owns the trust maths. D49 puts the ``replay``
  entry point here and the arithmetic there; :mod:`.replay` is the seam and holds no
  scoring. It imports the scorer lazily, so this package is importable before T-062 lands.
* ``verify_chain`` here is the **ledger** verifier. ``packages.verification.verify``
  (T-065) is the *claim* verifier. Different concepts, deliberately not merged.
"""

from __future__ import annotations

from .canonical import (
    CHAIN_FIELDS,
    EVENT_FIELDS,
    GENESIS_HASH,
    CanonicalisationError,
    canonical_bytes,
    canonical_event,
    canonical_json,
    compute_event_hash,
    rfc3339_ms,
)
from .chain import chain_events, chain_head, seal_event, stream_hash, verify_chain
from .errors import TRANSIENT_DATASTORE_ERRORS, LedgerError, is_transient_datastore_error
from .migrations import applied_migrations, apply_migrations, migration_files, migrations_dir
from .replay import observations_from_events, replay  # D49: the one-line re-export
from .store import (
    CHAIN_LOCK_KEY,
    AppendResult,
    append_event,
    append_events,
    chain_tail,
    db_stream_hash,
    head_hash,
    read_events,
    verify_chain_in_db,
)

__all__ = [
    "CHAIN_FIELDS",
    "CHAIN_LOCK_KEY",
    "EVENT_FIELDS",
    "GENESIS_HASH",
    "TRANSIENT_DATASTORE_ERRORS",
    "AppendResult",
    "CanonicalisationError",
    "LedgerError",
    "append_event",
    "append_events",
    "applied_migrations",
    "apply_migrations",
    "canonical_bytes",
    "canonical_event",
    "canonical_json",
    "chain_events",
    "chain_head",
    "chain_tail",
    "compute_event_hash",
    "db_stream_hash",
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
