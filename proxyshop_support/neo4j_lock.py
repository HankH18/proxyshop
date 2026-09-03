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
:data:`DEFAULT_TIMEOUT` expires, which in an unattended run is indistinguishable from a
hung build. It cannot be caught by any single ticket's verify, because every ticket verify
runs one directory.

So the lock is **re-entrant within a process and exclusive between processes**, which is
exactly what D37 asks for: one interpreter holds one real ``flock`` however many times it
asks for it, and a *second* interpreter still waits. State is keyed by lock path and
guarded by a ``threading.Lock`` so threads inside one process share the single flock too.
"""

from __future__ import annotations

import fcntl
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

#: Deliberately outside the repo: worktrees are per-ticket, but the Neo4j container is not.
LOCK_PATH = Path(os.environ.get("PROXYSHOP_NEO4J_LOCK", "/tmp/proxyshop-neo4j.lock"))

#: How long a contended acquisition waits before raising :class:`Neo4jLockTimeout` (T-191).
#:
#: **This number is not free.** It has to end, with margin, INSIDE ``pyproject.toml``'s
#: repo-wide ``--timeout`` for pytest, because the only production caller —
#: ``_neo4j_guard`` in the root ``conftest.py`` — takes the lock during session-fixture
#: setup, and pytest-timeout covers setup. It used to be 600 against a 300 s budget, which
#: made the raise below unreachable under real contention: pytest killed the run first,
#: with a message naming neither Neo4j nor the lock, ``make verify`` ERRORED rather than
#: failed, and ``build_succeeds`` was recorded 0 for a machine condition. The relationship
#: is pinned by ``proxyshop_support/tests/test_neo4j_lock_diagnosability.py``, which parses
#: the real ``addopts`` — so changing either number, here or there, fails loudly.
DEFAULT_TIMEOUT = 240.0

#: Seconds between "still waiting" heartbeats while contended. A wait that says nothing for
#: four minutes during session setup is indistinguishable from a hang, which is how this
#: cost several unattended runs before anyone looked at the frame it was stopped in.
REPORT_EVERY = 30.0


class Neo4jLockTimeout(RuntimeError):
    """The Neo4j flock could not be acquired in time."""


@dataclass
class _Holding:
    """One process-wide flock, and how many nested acquisitions are standing on it."""

    handle: IO[str]
    depth: int


#: Guards :data:`_HELD`. A thread that arrives while another thread of this process already
#: owns the flock does not wait on the kernel at all — it just increments the depth.
#:
#: **Re-entrant on purpose.** The whole contended wait — up to :data:`DEFAULT_TIMEOUT` —
#: runs inside this lock, and that wait now invokes a caller-supplied ``report`` callback.
#: With a plain ``threading.Lock`` a callback as ordinary as
#: ``lambda m: log.info(m, depth=held_depth())`` would deadlock permanently on its own
#: thread. ``RLock`` makes that same-thread re-entry work. A callback still must not block:
#: a *different* thread calling :func:`held_depth` or releasing waits for the acquisition
#: to finish, which is pre-existing behaviour of the wait itself, not of the callback.
_MUTEX = threading.RLock()

#: resolved lock path -> the flock this process holds on it.
_HELD: dict[Path, _Holding] = {}


def _key(path: Path | str | None) -> Path:
    return Path(path if path is not None else LOCK_PATH).expanduser().resolve()


def lock_holder(path: Path | str | None = None) -> str:
    """Best-effort description of whoever currently holds the lock file.

    The holder stamps ``pid=<n> worker=<n>`` into the file just after taking the flock (see
    :func:`_acquire`), and an unheld lock keeps its last holder's line — which is why every
    caller of this asks only once it already knows it is blocked.

    **Best effort, and not always current.** Taking the flock and stamping the file are two
    steps, so a waiter that reads between them sees the *previous* holder's line (or, in the
    window between ``truncate`` and ``flush``, nothing at all). Treat the answer as a strong
    hint about who to go look at, not as proof. It is a diagnostic string, never a decision
    input.

    Returns:
        The stamped line, or a string containing ``"unknown"`` when the file is missing,
        empty or unreadable. Never raises: a diagnostic that can fail is worse than none,
        and this one is called while *building* a failure message.
    """
    try:
        stamped = _key(path).read_text().strip()
    except Exception:
        # Deliberately broad. OSError is the expected case, but `read_text` also raises
        # UnicodeDecodeError on a corrupt lock file and `_key`'s expanduser() raises
        # RuntimeError with no resolvable home — and either one would replace the whole
        # Neo4jLockTimeout diagnostic with a traceback about the diagnostic.
        return "unknown (the lock file could not be read)"
    if not stamped:
        return "unknown (the holder has not stamped the lock file yet)"
    return stamped.splitlines()[0]


def _report_to_stderr(message: str) -> None:
    """Default progress sink: this process's stderr, flushed per line.

    **Under pytest this is captured, and that caps what it can do.** The only production
    caller is ``_neo4j_guard`` in the root ``conftest.py``, which runs during session-fixture
    setup, and ``scripts/verify.sh`` invokes pytest without ``-s``. So these lines land in
    the item's "Captured stderr setup" buffer and are replayed only if that item fails or
    errors — i.e. on the path that ends in :class:`Neo4jLockTimeout`, which is the path
    where the diagnosis is needed. A wait that succeeds after four minutes still looks
    silent to a live log tail; pass ``report=`` (or run with ``-s``) if you need it live.
    """
    print(message, file=sys.stderr, flush=True)


def _timeout_message(lock_path: Path, timeout: float) -> str:
    """The :class:`Neo4jLockTimeout` text. The FIRST LINE has to be the whole diagnosis.

    An unattended run's log, a CI summary and a ``-q`` pytest error line all show one line.
    Before T-191 that line was ``Failed: Timeout (>300.0s) from pytest-timeout``, which
    reads like a hung build and sent several sessions looking for a product bug that was
    never there.
    """
    return (
        f"another ProxyShop worker process holds the Neo4j lock (D37) — waited "
        f"{timeout:g}s for it and gave up.\n"
        f"  lock file: {lock_path}\n"
        f"  holder:    {lock_holder(lock_path)}\n"
        f"  this run:  pid={os.getpid()} worker={os.environ.get('PROXYSHOP_WORKER')}\n"
        f"Neo4j Community has exactly ONE database (D4), so every pytest session that "
        f"touches the graph is serialized on this single machine-global flock — it is "
        f"deliberately outside every worktree, so a parallel swarm contends on it as a "
        f"matter of course. Nothing is hung and nothing is wrong with the code under test: "
        f"a sibling worker's graph lane is still running. Re-run this lane once it "
        f"finishes, or run it on its own."
    )


def held_depth(path: Path | str | None = None) -> int:
    """How many nested :func:`neo4j_flock` blocks this process currently holds (0 = none)."""
    with _MUTEX:
        holding = _HELD.get(_key(path))
        return holding.depth if holding else 0


@contextmanager
def neo4j_flock(
    timeout: float = DEFAULT_TIMEOUT,
    poll: float = 0.5,
    *,
    path: Path | str | None = None,
    report: Callable[[str], None] | None = None,
    report_every: float = REPORT_EVERY,
) -> Iterator[Path]:
    """Hold an exclusive, process-re-entrant ``flock`` on :data:`LOCK_PATH`.

    Args:
        timeout: seconds to wait for another *process* before giving up. Defaults to
            :data:`DEFAULT_TIMEOUT`, which is chosen to expire INSIDE pytest's own
            per-item budget — read that constant's note before changing it. A nested
            acquisition inside the same process never waits, so this timeout is only ever
            about cross-worker contention.
        poll: seconds between attempts.
        path: override the lock file (tests use this; production always uses the default).
        report: where progress goes while blocked. Defaults to :func:`_report_to_stderr`
            (read its note on pytest capture). Called once when the wait starts and every
            ``report_every`` seconds after that, never when the lock is free — an
            uncontended acquisition is completely silent. It runs while this module's
            re-entrant ``_MUTEX`` is held, so it may call back into this module from its own
            thread but must not block.
        report_every: seconds between heartbeats. ``0`` reports on every poll.

    Yields:
        The lock file path, so a caller can log who is waiting on what.

    Raises:
        Neo4jLockTimeout: another **process** held the lock for longer than ``timeout``.
            This is a loud failure on purpose — silently proceeding would interleave
            writes. Its first line names the contention explicitly; see
            :func:`_timeout_message`.
    """
    lock_path = _key(path)
    _acquire(lock_path, timeout, poll, report or _report_to_stderr, report_every)
    try:
        yield lock_path
    finally:
        _release(lock_path)


def _acquire(
    lock_path: Path,
    timeout: float,
    poll: float,
    report: Callable[[str], None],
    report_every: float,
) -> None:
    with _MUTEX:
        holding = _HELD.get(lock_path)
        if holding is not None:
            holding.depth += 1
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        started = time.monotonic()
        deadline = started + timeout
        announced = False
        next_report = started + report_every
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    now = time.monotonic()
                    if not announced:
                        # Only ever reached when the lock is genuinely held elsewhere, so
                        # an uncontended run stays silent.
                        announced = True
                        report(
                            f"[neo4j-lock] waiting up to {timeout:g}s for {lock_path} — "
                            f"held by {lock_holder(lock_path)}. Neo4j Community has ONE "
                            f"database (D4/D37) so graph sessions run one at a time; this "
                            f"is cross-worker contention, not a hang."
                        )
                    if now >= deadline:
                        raise Neo4jLockTimeout(_timeout_message(lock_path, timeout)) from None
                    if now >= next_report:
                        next_report = now + report_every
                        report(
                            f"[neo4j-lock] still waiting {now - started:.0f}s of "
                            f"{timeout:g}s for {lock_path} — held by "
                            f"{lock_holder(lock_path)}."
                        )
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
