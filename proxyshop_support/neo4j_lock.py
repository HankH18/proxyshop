"""The cross-worker Neo4j lock (D37). Orchestrator-owned (T-000), frozen.

D37: *Neo4j isolation = scheduler serialization **plus** an flock.* The single Neo4j
container has one database on Community Edition, so two workers writing at once corrupt
each other's assertions no matter how careful each is. The scheduler already avoids
running two graph tickets together; this ``flock`` is the backstop for when it does not
(a rerun, a manual invocation, an audit loop).

The lock is taken **session-scoped** by ``services/ingest/tests/conftest.py`` and
``apps/exchange/tests/conftest.py``, and the graph reset happens *inside* it
(:func:`reset_graph`, called by the ``neo4j_driver`` fixture).

Re-entrancy — the reason this is not a bare ``fcntl.flock`` call
---------------------------------------------------------------
``fcntl.flock`` is per *open file description*, not per process: two ``open()`` calls in
one process produce two descriptions, and the second ``LOCK_EX`` blocks on the first. A
whole-repo ``pytest`` run collects **both** graph lanes, so both session-scoped guards are
instantiated in one interpreter — and a naive implementation self-deadlocks there until
the 600 s timeout, which in an unattended run is indistinguishable from a hung build. It
cannot be caught by any single ticket's verify, because every ticket verify runs one
directory.

So the lock is **re-entrant within a process and exclusive between processes**, which is
exactly what D37 asks for: one interpreter holds one real ``flock`` however many times it
asks for it, and a *second* interpreter still waits. State is keyed by lock path and
guarded by a ``threading.Lock`` so threads inside one process share the single flock too.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

#: Deliberately outside the repo: worktrees are per-ticket, but the Neo4j container is not.
LOCK_PATH = Path(os.environ.get("PROXYSHOP_NEO4J_LOCK", "/tmp/proxyshop-neo4j.lock"))


class Neo4jLockTimeout(RuntimeError):
    """The Neo4j flock could not be acquired in time."""


@dataclass
class _Holding:
    """One process-wide flock, and how many nested acquisitions are standing on it."""

    handle: IO[str]
    depth: int


#: Guards :data:`_HELD`. Held only for the duration of an acquisition attempt, so a thread
#: that arrives while another thread of this process already owns the flock does not wait
#: on the kernel at all — it just increments the depth.
_MUTEX = threading.Lock()

#: resolved lock path -> the flock this process holds on it.
_HELD: dict[Path, _Holding] = {}


def _key(path: Path | str | None) -> Path:
    return Path(path if path is not None else LOCK_PATH).expanduser().resolve()


def held_depth(path: Path | str | None = None) -> int:
    """How many nested :func:`neo4j_flock` blocks this process currently holds (0 = none)."""
    with _MUTEX:
        holding = _HELD.get(_key(path))
        return holding.depth if holding else 0


@contextmanager
def neo4j_flock(
    timeout: float = 600.0,
    poll: float = 0.5,
    *,
    path: Path | str | None = None,
) -> Iterator[Path]:
    """Hold an exclusive, process-re-entrant ``flock`` on :data:`LOCK_PATH`.

    Args:
        timeout: seconds to wait for another *process* before giving up. The default is
            generous because the holder is a whole pytest session on the graph lane. A
            nested acquisition inside the same process never waits, so this timeout is
            only ever about cross-worker contention.
        poll: seconds between attempts.
        path: override the lock file (tests use this; production always uses the default).

    Yields:
        The lock file path, so a caller can log who is waiting on what.

    Raises:
        Neo4jLockTimeout: another **process** held the lock for longer than ``timeout``.
            This is a loud failure on purpose — silently proceeding would interleave
            writes.
    """
    lock_path = _key(path)
    _acquire(lock_path, timeout, poll)
    try:
        yield lock_path
    finally:
        _release(lock_path)


def _acquire(lock_path: Path, timeout: float, poll: float) -> None:
    with _MUTEX:
        holding = _HELD.get(lock_path)
        if holding is not None:
            holding.depth += 1
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise Neo4jLockTimeout(
                            f"could not acquire {lock_path} within {timeout}s — another "
                            f"ProxyShop worker process is still using Neo4j (D37)."
                        ) from None
                    time.sleep(poll)
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} worker={os.environ.get('PROXYSHOP_WORKER')}\n")
            handle.flush()
        except BaseException:
            handle.close()
            raise
        _HELD[lock_path] = _Holding(handle=handle, depth=1)


def _release(lock_path: Path) -> None:
    with _MUTEX:
        holding = _HELD.get(lock_path)
        if holding is None:  # pragma: no cover - defensive
            return
        holding.depth -= 1
        if holding.depth > 0:
            return
        del _HELD[lock_path]
        try:
            fcntl.flock(holding.handle.fileno(), fcntl.LOCK_UN)
        finally:
            holding.handle.close()


def reset_graph(driver: Any) -> None:
    """Delete every node and relationship in the single Neo4j database (D37).

    Call this only while holding :func:`neo4j_flock` — the ``neo4j_driver`` fixture does,
    which is why the reset lives here rather than in a test. Community Edition has exactly
    one database, so without a reset one lane's nodes are still present when the next
    lane's assertions run, and a "precision" number measured on that graph is meaningless.

    Constraints and indexes are deliberately **not** dropped: they are schema, created once
    by the graph ticket, and re-creating them on every session would be pure cost.
    """
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
