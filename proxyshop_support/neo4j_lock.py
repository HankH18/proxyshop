"""The cross-worker Neo4j lock (D37). Orchestrator-owned (T-000), frozen.

D37: *Neo4j isolation = scheduler serialization **plus** an flock.* The single Neo4j
container has one database on Community Edition, so two workers writing at once corrupt
each other's assertions no matter how careful each is. The scheduler already avoids
running two graph tickets together; this ``flock`` is the backstop for when it does not
(a rerun, a manual invocation, an audit loop).

The lock is taken **per test** by ``_neo4j_guard`` in the root ``conftest.py``, and the
graph reset happens *inside* it (:func:`reset_graph`, called by the ``neo4j_driver``
fixture on every acquisition).

Held for the graph work, not for the session (T-214)
----------------------------------------------------
``_neo4j_guard`` was ``scope="session"`` until T-214, which meant the first Neo4j-touching
test in a run took this MACHINE-GLOBAL lock and the run then held it to teardown. Measured
on a real ``scripts/verify.sh check``: held from t=25.1 s to t=220.8 s of a 221.78 s
session — 88 % of it — with ~3 800 tests that never touch Neo4j running inside the hold.
Every lane runs ``check``, so concurrent lanes serialised on one lock and the loser raised
:class:`Neo4jLockTimeout` for a machine reason. The guard is function-scoped now, so a
sibling worker waits out one graph test rather than a whole suite.

The consequence for callers: the lock is **dropped between tests**, so another process may
write the graph in the gap. That is why :func:`reset_graph` runs on every acquisition
rather than once per session — serialization without a reset at each hand-off is not
isolation. ``proxyshop_support/tests/test_neo4j_lock_scope.py`` pins both halves: the hold
is short, and two concurrent sessions still exclude each other.

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

What an acquisition COSTS is measured, not asserted (P5a)
---------------------------------------------------------
Every completed acquisition appends one JSON object to :func:`lock_log_path` — by default
``<lock file>.log``, i.e. ``/tmp/proxyshop-neo4j.lock.log``. See :class:`Acquisition` for
the fields. The point is a **per-acquisition cost**, not an event stream: ``waited_s`` is
what a sibling worker lost to contention, ``held_s`` is how long this process kept every
other one out, and ``reset_ms``/``nodes_deleted`` say how much of that hold was the
:func:`reset_graph` wipe rather than the test.

This exists because the number everyone quotes — "the flock is held 88 % of the session" —
was measured against the **session-scoped** guard that T-214 replaced. It describes a
system that no longer runs. Nothing should decide how many Neo4j instances to stand up
until this log has said what the function-scoped lock actually costs; run
``python -m proxyshop_support.neo4j_lock`` (or :func:`summarize_lock_log`) to reduce the
log to that number.

**What it does not cover**, stated plainly so nobody over-reads it:

* Only *completed* acquisitions are recorded. A wait that ends in
  :class:`Neo4jLockTimeout` writes nothing, and neither does a process killed mid-hold —
  so the log under-counts exactly the pathological cases.
* ``waited_s`` is measured from the first ``flock`` attempt. It cannot see time a caller
  spent queued *before* asking (pytest collection, fixture setup ahead of
  ``_neo4j_guard``, the ``_require_services`` probe).
* A nested acquisition inside one process is folded into the outer record
  (``reentries``/``max_depth``); it takes no kernel lock and costs no sibling anything.
* It is a *local* record: each process writes its own lines, so a cluster-wide picture
  needs the logs concatenated. Lines are single short appends to an ``O_APPEND`` file,
  which does not interleave in practice, but the file is a diagnostic and never an input
  to a decision the code makes at runtime.
* Writing the record happens **after** the flock is released, so the instrumentation
  cannot lengthen anybody's wait; the cost it reports therefore excludes itself.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

#: Deliberately outside the repo: worktrees are per-ticket, but the Neo4j container is not.
LOCK_PATH = Path(os.environ.get("PROXYSHOP_NEO4J_LOCK", "/tmp/proxyshop-neo4j.lock"))

#: Overrides where :func:`lock_log_path` writes. Set it to ``""``, ``"0"``, ``"off"`` or
#: ``"none"`` to turn the instrumentation off entirely; unset, the log sits beside the lock
#: file, which is already machine-global and already written on every acquisition.
LOG_ENV_VAR = "PROXYSHOP_NEO4J_LOCK_LOG"

#: Values of :data:`LOG_ENV_VAR` that mean "write nothing".
LOG_DISABLED_VALUES = frozenset({"", "0", "off", "no", "none", "false"})

#: How long a contended acquisition waits before raising :class:`Neo4jLockTimeout` (T-191).
#:
#: **This number is not free.** It has to end, with margin, INSIDE ``pyproject.toml``'s
#: repo-wide ``--timeout`` for pytest, because the only production caller —
#: ``_neo4j_guard`` in the root ``conftest.py`` — takes the lock during fixture setup, and
#: pytest-timeout covers setup. It used to be 600 against a 300 s budget, which
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
class Acquisition:
    """The measured cost of ONE completed hold of the flock (P5a).

    One of these becomes one JSON line in :func:`lock_log_path`. Every duration is in
    seconds except :attr:`reset_ms`, which is in milliseconds because it is expected to be
    small and a reader comparing it against :attr:`held_s` should not have to count zeroes.

    Attributes:
        pid: the holding process.
        worker: ``$PROXYSHOP_WORKER`` as a string, or ``None`` when unset. Kept as the raw
            string: the log records what the process was actually told, not a parse of it.
        test: ``$PYTEST_CURRENT_TEST`` at acquisition — the nodeid whose *setup* took the
            lock, when there is one. ``None`` outside pytest.
        waited_s: seconds spent blocked on another **process**'s flock. ~0 uncontended.
        held_s: seconds from acquiring the flock to releasing it. This is the number a
            sibling worker pays for.
        contended: whether the acquisition ever had to wait (i.e. the first non-blocking
            ``flock`` failed). Distinguishes "0.0004 s because nobody was there" from
            "0.0004 s because the holder let go immediately".
        reentries: nested acquisitions folded into this record. They take no kernel lock.
        max_depth: the deepest nesting reached during this hold (1 = never nested).
        resets: how many :func:`reset_graph` calls ran inside this hold.
        reset_ms: total milliseconds those resets spent in ``MATCH (n) DETACH DELETE n``.
        nodes_deleted: nodes those resets actually removed. **Zero is the interesting
            value**: it means the graph was already empty and the wipe bought nothing.
        relationships_deleted: as above, for relationships.
    """

    pid: int
    worker: str | None
    test: str | None
    waited_s: float
    held_s: float = 0.0
    contended: bool = False
    reentries: int = 0
    max_depth: int = 1
    resets: int = 0
    reset_ms: float = 0.0
    nodes_deleted: int = 0
    relationships_deleted: int = 0
    #: monotonic stamp taken the instant the flock was won; not serialised.
    acquired_at: float = field(default=0.0, repr=False)

    def as_record(self, lock_path: Path) -> dict[str, Any]:
        """This acquisition as the dict that gets serialised, newest fields last."""
        return {
            "ts": datetime.now(UTC).isoformat(),
            "lock": str(lock_path),
            "pid": self.pid,
            "worker": self.worker,
            "test": self.test,
            "waited_s": round(self.waited_s, 6),
            "held_s": round(self.held_s, 6),
            "contended": self.contended,
            "reentries": self.reentries,
            "max_depth": self.max_depth,
            "resets": self.resets,
            "reset_ms": round(self.reset_ms, 3),
            "nodes_deleted": self.nodes_deleted,
            "relationships_deleted": self.relationships_deleted,
        }


@dataclass
class ResetOutcome:
    """What one :func:`reset_graph` call actually did.

    Returned so a caller can assert on it. ``nodes_deleted == 0`` is the standing proof
    that a wipe was redundant — which is how the duplicate wipe in
    ``services/ingest/tests/_fixtures_graph.py`` was shown to be free to delete (P5b).
    """

    reset_ms: float
    nodes_deleted: int
    relationships_deleted: int


@dataclass
class _Holding:
    """One process-wide flock, and how many nested acquisitions are standing on it."""

    handle: IO[str]
    depth: int
    stats: Acquisition


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


def lock_log_path(path: Path | str | None = None) -> Path | None:
    """Where per-acquisition records go, or ``None`` when instrumentation is off.

    Resolution order: ``$PROXYSHOP_NEO4J_LOCK_LOG`` (one of
    :data:`LOG_DISABLED_VALUES` turns it off), otherwise ``<lock file>.log``.

    The default is derived from the *resolved* lock path, so on macOS — where ``/tmp`` is a
    symlink to ``/private/tmp`` — the log for ``/tmp/proxyshop-neo4j.lock`` is
    ``/private/tmp/proxyshop-neo4j.lock.log``. That is the same file either way; it is
    named here so a reader looking for the log by its literal ``/tmp`` spelling is not
    surprised by the resolved one.
    """
    raw = os.environ.get(LOG_ENV_VAR)
    if raw is not None:
        if raw.strip().lower() in LOG_DISABLED_VALUES:
            return None
        return Path(raw).expanduser()
    return Path(f"{_key(path)}.log")


def _append_record(lock_path: Path, stats: Acquisition) -> None:
    """Append one acquisition to the log. Never raises.

    Called from :func:`_release` **after** the flock is dropped, so a slow or failing disk
    cannot lengthen a sibling worker's wait. Instrumentation that can fail a test run is
    worse than no instrumentation, so every error here is swallowed: the caller is a
    ``finally``-path release and the alternative is turning a passing graph test into a
    traceback about the logging of it.
    """
    try:
        log_path = lock_log_path(lock_path)
        if log_path is None:
            return
        line = json.dumps(stats.as_record(lock_path), separators=(",", ":")) + "\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # One short append per record: O_APPEND makes concurrent writers from separate
        # processes land whole lines rather than interleaved fragments.
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:  # pragma: no cover - defensive; see the docstring
        pass


def read_lock_log(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Every well-formed record in the log, oldest first.

    Args:
        path: the **log** file. Defaults to :func:`lock_log_path`.

    Returns:
        The parsed records. A truncated or half-written final line is skipped rather than
        raising: the log is appended to by live processes, so reading it while a run is in
        flight is the normal case.
    """
    log_path = Path(path) if path is not None else lock_log_path()
    if log_path is None or not log_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile. Empty -> 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def summarize_lock_log(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Reduce acquisition records to the numbers a fan-out decision needs.

    Returns:
        ``acquisitions``, ``contended`` (count and share), wait p50/p95/max, hold
        p50/p95/max, the total seconds the lock was held by these records, and how much of
        that was the graph reset — plus ``resets_that_deleted_nothing``, which is the
        measure of how often the wipe is pure overhead.
    """
    waits = [float(r.get("waited_s", 0.0)) for r in records]
    holds = [float(r.get("held_s", 0.0)) for r in records]
    resets = [r for r in records if int(r.get("resets", 0) or 0) > 0]
    reset_seconds = sum(float(r.get("reset_ms", 0.0)) for r in records) / 1000.0
    total_held = sum(holds)
    contended = [r for r in records if r.get("contended")]
    return {
        "acquisitions": len(records),
        "contended": len(contended),
        "contended_share": (len(contended) / len(records)) if records else 0.0,
        "wait_p50_s": _percentile(waits, 0.50),
        "wait_p95_s": _percentile(waits, 0.95),
        "wait_max_s": max(waits, default=0.0),
        "wait_total_s": sum(waits),
        "held_p50_s": _percentile(holds, 0.50),
        "held_p95_s": _percentile(holds, 0.95),
        "held_max_s": max(holds, default=0.0),
        "held_total_s": total_held,
        "reset_total_s": reset_seconds,
        "reset_share_of_hold": (reset_seconds / total_held) if total_held else 0.0,
        "resets": len(resets),
        "resets_that_deleted_nothing": sum(
            1 for r in resets if int(r.get("nodes_deleted", 0) or 0) == 0
        ),
        "nodes_deleted_total": sum(int(r.get("nodes_deleted", 0) or 0) for r in records),
    }


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
            holding.stats.reentries += 1
            holding.stats.max_depth = max(holding.stats.max_depth, holding.depth)
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
            won_at = time.monotonic()
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} worker={os.environ.get('PROXYSHOP_WORKER')}\n")
            handle.flush()
        except BaseException:
            handle.close()
            raise
        stats = Acquisition(
            pid=os.getpid(),
            worker=os.environ.get("PROXYSHOP_WORKER"),
            test=os.environ.get("PYTEST_CURRENT_TEST"),
            waited_s=won_at - started,
            contended=announced,
            acquired_at=won_at,
        )
        _HELD[lock_path] = _Holding(handle=handle, depth=1, stats=stats)


def _release(lock_path: Path) -> None:
    with _MUTEX:
        holding = _HELD.get(lock_path)
        if holding is None:  # pragma: no cover - defensive
            return
        holding.depth -= 1
        if holding.depth > 0:
            return
        del _HELD[lock_path]
        # Stamped before the unlock so it measures the hold, not the bookkeeping.
        holding.stats.held_s = time.monotonic() - holding.stats.acquired_at
        try:
            fcntl.flock(holding.handle.fileno(), fcntl.LOCK_UN)
        finally:
            holding.handle.close()
        # After the unlock: a waiting sibling is already free before this line runs.
        _append_record(lock_path, holding.stats)


def _record_reset(path: Path | str | None, outcome: ResetOutcome) -> None:
    """Attribute one reset to the acquisition it ran inside, if that is unambiguous.

    :func:`reset_graph` is handed a driver, not a lock path, so attribution is by
    elimination: an explicit ``path`` wins; otherwise a single held lock is the one; a
    process holding several distinct lock files at once (only tests do that) attributes to
    the default path if it is held, and to nothing otherwise. Guessing wrong would put a
    reset's cost on the wrong hold, and a wrong number is worse than a missing one.
    """
    with _MUTEX:
        holding: _Holding | None
        if path is not None:
            holding = _HELD.get(_key(path))
        elif len(_HELD) == 1:
            holding = next(iter(_HELD.values()))
        else:
            holding = _HELD.get(_key(None)) if _HELD else None
        if holding is None:
            return
        holding.stats.resets += 1
        holding.stats.reset_ms += outcome.reset_ms
        holding.stats.nodes_deleted += outcome.nodes_deleted
        holding.stats.relationships_deleted += outcome.relationships_deleted


def reset_graph(driver: Any, *, path: Path | str | None = None) -> ResetOutcome:
    """Delete every node and relationship in the single Neo4j database (D37).

    Call this only while holding :func:`neo4j_flock` — the ``neo4j_driver`` fixture does,
    which is why the reset lives here rather than in a test. Community Edition has exactly
    one database, so without a reset one lane's nodes are still present when the next
    lane's assertions run, and a "precision" number measured on that graph is meaningless.

    Constraints and indexes are deliberately **not** dropped: they are schema, created once
    by the graph ticket, and re-creating them on every session would be pure cost.

    Args:
        path: which held lock this reset's cost belongs to. Defaults to letting
            :func:`_record_reset` work it out; only a process holding two different lock
            files at once needs to say.

    Returns:
        What the wipe actually did. ``nodes_deleted == 0`` means the graph was already
        empty and this call bought nothing — the measurement that makes a *second* wipe in
        a fixture provably redundant rather than arguably so.
    """
    started = time.perf_counter()
    with driver.session() as session:
        summary = session.run("MATCH (n) DETACH DELETE n").consume()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    counters = getattr(summary, "counters", None)
    outcome = ResetOutcome(
        reset_ms=elapsed_ms,
        nodes_deleted=int(getattr(counters, "nodes_deleted", 0) or 0),
        relationships_deleted=int(getattr(counters, "relationships_deleted", 0) or 0),
    )
    _record_reset(path, outcome)
    return outcome


def _main(argv: Sequence[str] | None = None) -> int:
    """``python -m proxyshop_support.neo4j_lock [log path]`` — the P5a numbers.

    Deliberately tiny and dependency-free: the whole point of 5a is that the cost of the
    lock stops being a claim, and a claim that needs a bespoke one-liner to check is one
    step from being quoted second-hand forever (which is what happened to the 88 % figure).
    """
    import argparse

    parser = argparse.ArgumentParser(prog="proxyshop_support.neo4j_lock", description=__doc__)
    parser.add_argument("log", nargs="?", default=None, help="log file (default: beside the lock)")
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    args = parser.parse_args(argv)

    records = read_lock_log(args.log)
    summary = summarize_lock_log(records)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    where = args.log or lock_log_path()
    if not records:
        print(f"no acquisitions recorded in {where}")
        return 0
    print(f"{summary['acquisitions']} acquisitions in {where}")
    print(
        f"  wait   p50 {summary['wait_p50_s']:.4f}s  p95 {summary['wait_p95_s']:.4f}s  "
        f"max {summary['wait_max_s']:.4f}s  total {summary['wait_total_s']:.3f}s"
    )
    print(
        f"  held   p50 {summary['held_p50_s']:.4f}s  p95 {summary['held_p95_s']:.4f}s  "
        f"max {summary['held_max_s']:.4f}s  total {summary['held_total_s']:.3f}s"
    )
    print(
        f"  reset  {summary['reset_total_s']:.3f}s of that hold "
        f"({summary['reset_share_of_hold'] * 100:.1f}%), {summary['resets']} resets, "
        f"{summary['resets_that_deleted_nothing']} deleted nothing, "
        f"{summary['nodes_deleted_total']} nodes deleted in total"
    )
    print(
        f"  contended {summary['contended']}/{summary['acquisitions']} "
        f"({summary['contended_share'] * 100:.1f}%)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(_main())
