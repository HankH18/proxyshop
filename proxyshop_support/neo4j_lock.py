"""The cross-worker Neo4j lock (D37). Orchestrator-owned (T-000), frozen.

D37: *Neo4j isolation = scheduler serialization **plus** an flock.* The single Neo4j
container has one database on Community Edition, so two workers writing at once corrupt
each other's assertions no matter how careful each is. The scheduler already avoids
running two graph tickets together; this ``flock`` is the backstop for when it does not
(a rerun, a manual invocation, an audit loop).

The lock is taken **session-scoped** by ``services/ingest/tests/conftest.py`` and
``apps/exchange/tests/conftest.py``, and any database reset happens *inside* it.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Deliberately outside the repo: worktrees are per-ticket, but the Neo4j container is not.
LOCK_PATH = Path(os.environ.get("PROXYSHOP_NEO4J_LOCK", "/tmp/proxyshop-neo4j.lock"))


class Neo4jLockTimeout(RuntimeError):
    """The Neo4j flock could not be acquired in time."""


@contextmanager
def neo4j_flock(timeout: float = 600.0, poll: float = 0.5) -> Iterator[Path]:
    """Hold an exclusive ``flock`` on :data:`LOCK_PATH` for the duration of the block.

    Args:
        timeout: seconds to wait before giving up. The default is generous because the
            holder is a whole pytest session on the graph lane.
        poll: seconds between attempts.

    Yields:
        The lock file path, so a caller can log who is waiting on what.

    Raises:
        Neo4jLockTimeout: another worker held the lock for longer than ``timeout``. This
            is a loud failure on purpose — silently proceeding would interleave writes.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Neo4jLockTimeout(
                        f"could not acquire {LOCK_PATH} within {timeout}s — another "
                        f"ProxyShop worker is still using Neo4j (D37)."
                    ) from None
                time.sleep(poll)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} worker={os.environ.get('PROXYSHOP_WORKER')}\n")
            handle.flush()
            yield LOCK_PATH
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
