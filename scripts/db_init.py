#!/usr/bin/env python
"""Create this worker's Postgres database (D38). Orchestrator-owned (T-000), frozen.

Run by ``make db-init``, and by ``make deps-up`` right after the stack reports healthy.
Idempotent: running it twice, or from six workers at once, is fine.

The root ``conftest.py`` calls the same :func:`~proxyshop_support.postgres.
ensure_worker_database` from a session fixture, so tests do not *depend* on this having
been run — this target exists so the database is there before anything else (psql, a
migration runner, a service started by hand) needs it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxyshop_support.postgres import (  # noqa: E402 - after the sys.path bootstrap
    WorkerDatabaseError,
    ensure_worker_database,
    maintenance_dsn,
)
from proxyshop_support.worker import WorkerNotConfiguredError, worker_id  # noqa: E402


def main() -> int:
    try:
        worker = worker_id()
    except WorkerNotConfiguredError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    try:
        name = ensure_worker_database(worker)
    except WorkerDatabaseError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    print(f"OK: database {name} exists (via {maintenance_dsn(worker)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
