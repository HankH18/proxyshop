"""T-171 — a contended Neo4j lock must stay diagnosable after the item has spent budget.

What T-191 fixed, and where it stopped
--------------------------------------
T-191 lowered :data:`~proxyshop_support.neo4j_lock.DEFAULT_TIMEOUT` from 600 to 240 so it
would end inside ``pyproject.toml``'s repo-wide ``--timeout=300``, and
``test_neo4j_lock_diagnosability.py`` pins that arithmetic. But the arithmetic is only
sound for an item that spends nothing else. pytest-timeout's budget covers **setup, call
and teardown together**, so what the lock actually gets is the budget MINUS whatever the
item has already burned — and ``_neo4j_guard`` is not the first thing an item does
(``_require_services`` probes the stack, the driver connects, ``reset_graph`` runs inside
the acquisition). 300 - 240 leaves 60 s for all of that. A sibling lane in this cycle has a
gate that takes 101 s on its own.

Measured at the parent of this commit, with the real primitive and a real pytest-timeout::

    budget 10 s, 5 s burned before the guard, lock wait 7 s (which satisfies T-191's
    "240 + 30 <= 300"-shaped margin rule against that budget)
    ->  proxyshop_support/neo4j_lock.py:539: Failed: Timeout (>10.0s) from pytest-timeout

That is T-171 verbatim: "under contention the ``_neo4j_guard`` fixture dies inside
``time.sleep(poll)`` with ``Failed: Timeout (>300.0s) from pytest-timeout`` instead of the
diagnosable ``Neo4jLockTimeout`` the module exists to raise". The lock is machine-global by
DESIGN — Neo4j Community has exactly one database (D4), so serialising every worktree on
one flock is the only isolation there is — which means contention is the NORMAL case in a
swarm and every one of those reds is a machine condition wearing a product defect's
clothes. Two lanes lost time to it in this cycle alone.

What is pinned here
-------------------
1. that a wait clipped by the ambient deadline still ends in ``Neo4jLockTimeout``, in a
   real child pytest, under a real pytest-timeout, with the real primitive (below);
2. that the clip does NOT fire when the item has headroom — otherwise "always give up
   instantly" would pass leg 1 while destroying the lock's actual job;
3. that the lock stays MACHINE-GLOBAL. This is the wrong repair that the ticket's own
   title invites ("PROXYSHOP_WORKER does not isolate it"), and taking it would silently
   delete D37: one Neo4j database cannot be shared by per-worker locks.

None of these assert on source text. Leg 1 reads the child's own output, whose "this is a
pytest-timeout kill" half is written by ``pytest_timeout.py`` in site-packages — outside
this lane's write scope — and whose "this is the lock" half is the exception type.
"""

from __future__ import annotations

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

#: The child's pytest budget, the seconds it burns before asking for the lock, and the wait
#: it then asks for. Chosen so that, on its own, ``LOCK_WAIT`` is a PERFECTLY LEGAL number
#: by T-191's rule (LOCK_WAIT + AMBIENT_DEADLINE_MARGIN <= CHILD_BUDGET) — the overrun comes
#: only from the burn, which is the whole point. Unfixed, the wait runs to
#: BURN + LOCK_WAIT = 23 s and pytest kills the item at 20 s, three seconds clear of any
#: scheduler noise. Fixed, the clip leaves a real 6 s wait rather than an instant give-up,
#: so leg 1 exercises the waiting path and not just the raise.
CHILD_BUDGET = 20.0
CHILD_BURN = 9.0
CHILD_LOCK_WAIT = 14.0

#: pytest-timeout's own kill message — the thing that must NOT appear in the child.
#:
#: A regex on the FULL shape, not the bare substring ``"from pytest-timeout"``. That
#: substring collided with the repair's own diagnostic, which explains what it is sparing
#: the reader from and therefore quotes the phrase: the gate went red on its own
#: explanation. Requiring the ``(>N.Ns)`` that only pytest-timeout's real message carries
#: separates the kill from any prose about it. Written by ``pytest_timeout.py`` in
#: site-packages, so the oracle for this half is outside this lane's write scope.
PYTEST_TIMEOUT_KILL = re.compile(r"Timeout \(>[0-9.]+s\) from pytest-timeout")

#: The first line of :func:`~proxyshop_support.neo4j_lock._timeout_message`.
DIAGNOSIS = "another ProxyShop worker process holds the Neo4j lock"

_HOLDER = """
import sys, time
from proxyshop_support.neo4j_lock import neo4j_flock
lock, sentinel, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
with neo4j_flock(timeout=30.0, poll=0.05, path=lock, report=lambda message: None):
    with open(sentinel, "w", encoding="utf-8") as handle:
        handle.write("held")
    time.sleep(hold)
"""

#: The child pytest module. It imports the REAL primitive rather than reimplementing it,
#: and its shape mirrors production: something slow runs first, then the guard takes the
#: lock during fixture SETUP, which is what pytest-timeout's budget covers.
_CHILD_MODULE = """
import time
import pytest
from proxyshop_support.neo4j_lock import neo4j_flock

@pytest.fixture
def graph_guard():
    time.sleep({burn!r})            # _require_services, the driver connect, earlier fixtures
    with neo4j_flock(timeout={wait!r}, poll=0.1, path={lock!r}, report=lambda m: None):
        yield True

def test_touches_the_graph(graph_guard):
    assert graph_guard
"""


def _hold_the_lock(lock: Path, sentinel: Path, seconds: float) -> subprocess.Popen[bytes]:
    """A second process holding ``lock``, returned only once it demonstrably has it.

    The sentinel is not politeness: a holder that failed to take the flock turns every
    "contended" measurement below into an ordinary uncontended run that passes for the
    wrong reason, which is the standard way a gate like this goes quietly vacuous.
    """
    script = sentinel.parent / "hold_the_lock_t171.py"
    script.write_text(_HOLDER, encoding="utf-8")
    holder = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(script), str(lock), str(sentinel), str(seconds)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PROXYSHOP_NEO4J_LOCK_LOG": "0"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30.0
    while not sentinel.exists():
        assert holder.poll() is None, "the holder process died before taking the lock"
        assert time.monotonic() < deadline, "the holder never took the lock"
        time.sleep(0.02)
    return holder


def _reap(holder: subprocess.Popen[bytes]) -> None:
    """Never let the holder's teardown replace the result the test was about to report."""
    holder.terminate()
    try:
        holder.wait(timeout=30)
    except subprocess.TimeoutExpired:
        holder.kill()
        try:
            holder.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - the OS has bigger problems
            pass


def _default_lock_path_for_worker(worker: str) -> str:
    """What ``LOCK_PATH`` resolves to in a fresh process badged as ``worker``."""
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-c",
            "from proxyshop_support.neo4j_lock import LOCK_PATH; print(LOCK_PATH)",
        ],
        cwd=str(REPO_ROOT),
        env={
            key: value
            for key, value in {**os.environ, "PROXYSHOP_WORKER": worker}.items()
            if key != "PROXYSHOP_NEO4J_LOCK"
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout.strip()


# --------------------------------------------------------------------------------------
# the control: everything leg 1 silently depends on
# --------------------------------------------------------------------------------------


def test_t171_the_ambient_deadline_probe_is_armed(tmp_path: Path) -> None:
    """Not part of the property — the things whose failure would make it vacuous.

    Four of them, and each has a way of failing that leaves the gate GREEN and blind:

    1. **pytest-timeout must be on its ``signal`` method here.** The whole repair reads
       ``ITIMER_REAL``. pytest-timeout picks ``thread`` when ``SIGALRM`` is missing or a
       debugger is attached, and under that method there is no interval timer to read: the
       clip never fires, and leg 1 would be measuring nothing while passing.
    2. **The budget must cover SETUP**, or ``_neo4j_guard``'s wait is not under it at all.
    3. **The clip must not fire when there is headroom.** "Give up instantly, always"
       passes leg 1 and destroys the lock. This asserts a wait with room really waits.
    4. **The lock must stay machine-global.** The ticket's title invites the wrong repair
       — make ``PROXYSHOP_WORKER`` isolate it — and Neo4j Community has exactly ONE
       database (D4), so per-worker locks would serialise nothing and let two workers
       write the same graph. That is a silent correctness loss, so it is pinned here.
    """
    # 1. the interval timer this repair reads is really armed for this item.
    remaining = neo4j_lock.ambient_deadline_remaining()
    assert remaining is not None and remaining > 0, (
        "no ITIMER_REAL is armed for this pytest item, so pytest-timeout is not using its "
        "`signal` method here and `ambient_deadline_remaining()` cannot see the deadline. "
        "Leg 1 of this gate would pass while measuring nothing. Check --timeout-method / "
        "whether a debugger is attached."
    )

    # 2. and it covers setup, which is where the lock is taken.
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    ini = pyproject["tool"]["pytest"]["ini_options"]
    assert not ini.get("timeout_func_only"), (
        "pyproject.toml sets `timeout_func_only`, so the budget no longer covers fixture "
        "setup — where `_neo4j_guard` takes the lock — and this whole file is moot."
    )
    budget = re.findall(r"--timeout[= ]([0-9.]+)", ini["addopts"])
    assert len(budget) == 1, f"expected exactly one --timeout in addopts: {ini['addopts']!r}"
    assert float(budget[0]) > CHILD_BUDGET, (
        f"the repo budget ({budget[0]}s) is no larger than the child's ({CHILD_BUDGET}s), "
        f"so leg 1's own item could be killed before its child finishes"
    )

    # 3. with headroom, the wait is NOT clipped: it really waits what it asked for.
    lock = tmp_path / "headroom.lock"
    holder = _hold_the_lock(lock, tmp_path / "headroom.held", 30.0)
    try:
        started = time.monotonic()
        with pytest.raises(Neo4jLockTimeout):
            with neo4j_flock(timeout=1.5, poll=0.05, path=lock, report=lambda _m: None):
                pytest.fail("acquired a lock another process holds — D37 is not enforced")
        waited = time.monotonic() - started
    finally:
        _reap(holder)
    assert waited >= 1.2, (
        f"a 1.5s wait with {remaining:.0f}s of ambient budget left gave up after "
        f"{waited:.2f}s. The ambient clip is firing when it should not, which would make "
        f"leg 1 pass for the wrong reason and would make every real contended acquisition "
        f"give up before the holder could plausibly finish."
    )

    # 4. the lock is the same file whatever worker asks for it.
    paths = {worker: _default_lock_path_for_worker(worker) for worker in ("3", "7")}
    assert len(set(paths.values())) == 1, (
        f"the default Neo4j lock path now varies with PROXYSHOP_WORKER ({paths}). Neo4j "
        f"Community has exactly ONE database (D4), so per-worker locks serialise nothing: "
        f"two workers would hold 'their own' lock and write the same graph at the same "
        f"time. T-171's harm is the UNDIAGNOSABLE red, not the sharing — the sharing is "
        f"D37 working as designed."
    )


# --------------------------------------------------------------------------------------
# the property
# --------------------------------------------------------------------------------------


def test_t171_a_clipped_wait_still_raises_the_named_error(tmp_path: Path) -> None:
    """The whole defect, end to end, in a child pytest under a real pytest-timeout.

    A child rather than this process because the property IS about pytest killing an item:
    it cannot be observed from inside the item pytest would kill. The child imports the
    real ``neo4j_flock`` and runs under a real ``--timeout``; the only thing constructed
    for the test is the shape of the item (burn, then take the lock during setup), which is
    the shape production already has.

    ``timeout`` here is the caller's, not :data:`DEFAULT_TIMEOUT`, and deliberately so: a
    fix that merely lowered ``DEFAULT_TIMEOUT`` again would leave this red. What has to
    change is the mechanism — the wait has to know what deadline it is already under.
    """
    lock = tmp_path / "clipped.lock"
    module = tmp_path / "test_t171_child.py"
    module.write_text(
        _CHILD_MODULE.format(burn=CHILD_BURN, wait=CHILD_LOCK_WAIT, lock=str(lock)),
        encoding="utf-8",
    )
    assert CHILD_LOCK_WAIT + neo4j_lock.AMBIENT_DEADLINE_MARGIN <= CHILD_BUDGET, (
        "the child's lock wait is not a legal number even before the burn, so this would "
        "measure T-191's arithmetic rather than T-171's"
    )
    assert CHILD_BURN + CHILD_LOCK_WAIT > CHILD_BUDGET, (
        "the child cannot overrun its budget, so there is nothing here to diagnose"
    )

    holder = _hold_the_lock(lock, tmp_path / "clipped.held", CHILD_BUDGET + 20.0)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                "pytest",
                str(module),
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                f"--timeout={CHILD_BUDGET}",
                "-rA",
            ],
            cwd=str(REPO_ROOT),
            env={
                **os.environ,
                "PYTHONPATH": str(REPO_ROOT),
                "PROXYSHOP_NEO4J_LOCK_LOG": "0",
            },
            capture_output=True,
            text=True,
            timeout=CHILD_BUDGET + 90.0,
        )
    finally:
        _reap(holder)

    output = completed.stdout + completed.stderr
    context = textwrap.indent(output[-3000:], "    ")

    assert completed.returncode != 0, (
        "the child acquired a lock another process holds, so D37 is not being enforced "
        f"and nothing below means anything:\n{context}"
    )
    assert not PYTEST_TIMEOUT_KILL.search(output), (
        f"a pytest item that had burned {CHILD_BURN:g}s of its {CHILD_BUDGET:g}s budget "
        f"before asking for the Neo4j lock, then waited a per-call {CHILD_LOCK_WAIT:g}s "
        f"(itself a legal number against that budget), was killed by pytest-timeout inside "
        f"`time.sleep(poll)`. The message names neither Neo4j nor the lock, so cross-worker "
        f"contention — the NORMAL case for a machine-global flock in a parallel swarm — is "
        f"indistinguishable from a product defect, `make verify` ERRORS, and build_succeeds "
        f"records 0 for a machine condition.\n"
        f"Repair: the wait must be clipped to what is left of the deadline the process is "
        f"already under (`ambient_deadline_remaining()`), so Neo4jLockTimeout is raised "
        f"while there is still time to raise it. Lowering DEFAULT_TIMEOUT does not fix "
        f"this — the caller's own timeout is what overran here.\n{context}"
    )
    assert DIAGNOSIS in output, (
        f"the child failed without saying the Neo4j lock was the reason, so an unattended "
        f"run still cannot tell contention from a product defect:\n{context}"
    )
