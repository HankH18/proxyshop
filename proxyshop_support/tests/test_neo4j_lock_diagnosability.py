"""T-191 — a contended Neo4j lock must fail with its OWN named error, not pytest's timeout.

The measured defect
-------------------
``proxyshop_support/neo4j_lock.py`` waited ``600.0`` seconds by default while
``pyproject.toml``'s ``addopts`` carry a repo-wide ``--timeout=300`` for every pytest item.
The root ``conftest.py``'s ``_neo4j_guard`` takes the lock during **session-fixture setup**,
and pytest-timeout covers setup — verified here rather than assumed::

    $ cat conftest.py
    @pytest.fixture(scope="session")
    def slow_session_fixture():
        time.sleep(8)
        yield True
    $ pytest --timeout=3 -q
    >       time.sleep(8)
    E       Failed: Timeout (>3.0s) from pytest-timeout
    1 error

So at 300 s pytest killed the run before the lock's own 600 s :class:`Neo4jLockTimeout`
could ever fire. Consequences, all three of which corrupt the measurement rather than the
product:

* ``make verify`` ERRORED instead of failing, so ``build_succeeds`` was recorded 0;
* the recorded 0 depended on whether a sibling worktree happened to be running a graph lane
  at that moment — a machine condition, not a property of the code;
* the traceback said ``Failed: Timeout (>300.0s) from pytest-timeout`` and named neither
  Neo4j nor the lock unless you read the frame it stopped in.

The lock is machine-global (``/tmp/proxyshop-neo4j.lock``, deliberately outside any
worktree) and shared by every worktree, so in a parallel swarm contention is the NORMAL
case. Four false zeros in six cycles came through here.

What is pinned below
--------------------
1. the **budget relationship** — the lock's default wait must end, with margin, inside the
   pytest budget that is actually configured. Parsed out of ``pyproject.toml`` rather than
   mirrored, so moving either number is what fails, and neither can drift silently;
2. that the default is the value the root guard actually gets (it calls ``neo4j_flock()``
   with no arguments, a shape ``test_scaffold_wiring.py`` separately pins);
3. that the failure's **first line** says another worker holds the Neo4j lock, and that the
   body names the lock file, the holder and this waiter;
4. that the wait **reports what it is waiting for** while it waits, instead of going silent
   for minutes and looking like a hang.
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
import textwrap
import time
import tomllib
from pathlib import Path

import pytest

from proxyshop_support import neo4j_lock
from proxyshop_support.neo4j_lock import Neo4jLockTimeout, neo4j_flock

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: How much room the lock must leave between raising and pytest pulling the plug. A lock
#: that gave up at 299 s under a 300 s budget would be a coin flip, not a fix: reachability
#: probes, the driver connect and ``reset_graph`` all share the same setup budget.
MIN_MARGIN_SECONDS = 30.0


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _configured_pytest_timeout() -> float:
    """The ``--timeout=N`` every pytest item in this repo actually runs under."""
    config = tomllib.loads(PYPROJECT.read_text())
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    found = re.findall(r"--timeout[= ]([0-9.]+)", addopts)
    assert found, (
        "pyproject.toml's pytest addopts no longer carry a --timeout. This test exists "
        "because the lock's own wait has to end inside that budget; with the budget gone "
        f"the relationship is unpinned. addopts were: {addopts!r}"
    )
    assert len(found) == 1, f"more than one --timeout in addopts: {addopts!r}"
    return float(found[0])


def _hold_the_lock_in_another_process(lock: Path, ready: Path) -> subprocess.Popen[bytes]:
    """A second process holding ``lock``, returned once it has actually taken it.

    Deliberately no ``env=`` replacement (T-122 sweep): the child inherits this session's
    environment whole, and ``cwd`` at the repo root is what makes the import work.
    """
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import pathlib, sys, time
                from proxyshop_support.neo4j_lock import neo4j_flock
                lock, ready = sys.argv[1], sys.argv[2]
                with neo4j_flock(timeout=10.0, path=lock):
                    pathlib.Path(ready).write_text("held")
                    time.sleep(30)
                """
            ),
            str(lock),
            str(ready),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PROXYSHOP_WORKER": "77"},
    )
    deadline = time.monotonic() + 20
    while not ready.exists():
        assert holder.poll() is None, "the holder process died before taking the lock"
        assert time.monotonic() < deadline, "holder never took the lock"
        time.sleep(0.05)
    return holder


# --------------------------------------------------------------------------------------
# 1. the budget relationship
# --------------------------------------------------------------------------------------


def test_the_lock_gives_up_before_pytest_kills_the_run() -> None:
    """The whole defect, in one comparison.

    While ``DEFAULT_TIMEOUT`` sat above the pytest budget, the ``Neo4jLockTimeout`` raise
    was DEAD CODE under real contention — reachable only by a test that passes an explicit
    short timeout, which is exactly why every existing lock test stayed green through four
    false zeros.
    """
    budget = _configured_pytest_timeout()
    assert neo4j_lock.DEFAULT_TIMEOUT + MIN_MARGIN_SECONDS <= budget, (
        f"the Neo4j lock waits {neo4j_lock.DEFAULT_TIMEOUT}s but pytest kills every item "
        f"at {budget}s, so under cross-worker contention pytest-timeout always fires first "
        f"and Neo4jLockTimeout can never be raised. `make verify` then ERRORS with a "
        f"message naming neither Neo4j nor the lock, and build_succeeds is recorded 0 for "
        f"a machine condition rather than for the code. Keep at least "
        f"{MIN_MARGIN_SECONDS}s of margin."
    )


def test_the_default_timeout_is_the_one_the_root_guard_actually_gets() -> None:
    """``_neo4j_guard`` calls ``neo4j_flock()`` bare, so only the default is ever in play.

    Without this, someone could satisfy the test above by lowering ``DEFAULT_TIMEOUT``
    while the signature kept its own larger literal, and production would be unchanged.
    """
    default = inspect.signature(neo4j_flock).parameters["timeout"].default
    assert default == neo4j_lock.DEFAULT_TIMEOUT, (
        f"neo4j_flock's timeout default ({default}) is not DEFAULT_TIMEOUT "
        f"({neo4j_lock.DEFAULT_TIMEOUT}), so the budget check above proves nothing about "
        f"the call the root conftest actually makes."
    )

    guard = (REPO_ROOT / "conftest.py").read_text().split("def _neo4j_guard(")[1]
    guard = guard.split("\n@pytest.fixture")[0]
    assert "with neo4j_flock():" in guard, (
        "the root guard no longer takes the lock with the default timeout, so "
        f"DEFAULT_TIMEOUT is no longer what production waits: {guard}"
    )


# --------------------------------------------------------------------------------------
# 2. the failure is diagnosable
# --------------------------------------------------------------------------------------


def test_the_timeout_says_another_worker_holds_the_lock_in_its_first_line(
    tmp_path: Path,
) -> None:
    """An unattended run's log shows the first line. It has to be the whole diagnosis."""
    lock = tmp_path / "neo4j.lock"
    holder = _hold_the_lock_in_another_process(lock, tmp_path / "ready")
    try:
        with pytest.raises(Neo4jLockTimeout) as caught:
            with neo4j_flock(timeout=0.5, poll=0.05, path=lock, report=lambda _: None):
                pytest.fail("acquired a lock another process holds — D37 is not enforced")
    finally:
        holder.kill()
        holder.wait(timeout=10)

    message = str(caught.value)
    first_line = message.splitlines()[0]
    assert "another ProxyShop worker process holds the Neo4j lock" in first_line, first_line

    # …and the rest of it has to be actionable without opening a debugger.
    assert str(lock) in message, message
    assert f"pid={holder.pid}" in message, (
        f"the failure does not name the process actually holding the lock "
        f"(pid={holder.pid}): {message}"
    )
    assert "worker=77" in message, message
    assert f"pid={os.getpid()}" in message, f"the failure does not name this waiter: {message}"


def test_the_wait_reports_what_it_is_waiting_for_while_it_waits(tmp_path: Path) -> None:
    """Silence for minutes during session setup is indistinguishable from a hang.

    Both the opening announcement and the periodic heartbeat are required: the first says
    what the wait IS, the second proves the process is still alive partway through.
    """
    lock = tmp_path / "neo4j.lock"
    holder = _hold_the_lock_in_another_process(lock, tmp_path / "ready")
    reported: list[str] = []
    try:
        with pytest.raises(Neo4jLockTimeout):
            with neo4j_flock(
                timeout=1.0,
                poll=0.02,
                path=lock,
                report=reported.append,
                report_every=0.2,
            ):
                pytest.fail("acquired a lock another process holds")
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert reported, "the wait said nothing at all while it waited"
    opening = reported[0]
    assert str(lock) in opening, opening
    assert f"pid={holder.pid}" in opening, (
        f"the opening report does not say who it is waiting for: {opening}"
    )
    assert "not a hang" in opening.lower(), opening
    assert len(reported) >= 2, f"no heartbeat during a 1 s wait with report_every=0.2: {reported}"
    assert any("still waiting" in line for line in reported[1:]), reported


def test_the_lock_file_names_its_holder(tmp_path: Path) -> None:
    """The description the reports and the failure are both built from."""
    lock = tmp_path / "neo4j.lock"
    assert "unknown" in neo4j_lock.lock_holder(lock), "an untaken lock cannot name a holder"

    with neo4j_flock(timeout=5.0, path=lock):
        holder = neo4j_lock.lock_holder(lock)
    assert f"pid={os.getpid()}" in holder, holder
    assert f"worker={os.environ['PROXYSHOP_WORKER']}" in holder, holder
