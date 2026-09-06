"""End-to-end grading of the T-210 exit-status plugin, in real child pytest processes.

Why children rather than a fake ``Session`` object: the thing under test IS a process exit
status, and a hand-built session proves only that a function assigns an attribute. Every
case below runs ``python -m pytest -p proxyshop_support.lock_exit_status`` and reads
``returncode`` — the same and only channel the frozen ``build_succeeds`` probe reads.

**What this is NOT.** It is not T-210's gate. That gate lives at
``proxyshop_support/tests/test_repro_open_tickets.py::
test_t210_lock_contention_and_a_product_defect_do_not_share_an_exit_status`` and is still
RED, because it runs its children with a fixed argv that carries no ``-p`` and therefore
picks the plugin up only from the root ``conftest.py`` — a file outside this lane's write
scope. This module grades the implementation so that the one-line wiring is a decision
somebody makes with evidence rather than a hope.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from proxyshop_support import lock_exit_status
from proxyshop_support.lock_exit_status import LOCK_CONTENTION_EXIT_STATUS

REPO_ROOT = Path(__file__).resolve().parents[2]

_HOLDER = """
import sys, time
from proxyshop_support.neo4j_lock import neo4j_flock
lock, sentinel, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
with neo4j_flock(timeout=30.0, poll=0.05, path=lock, report=lambda message: None):
    with open(sentinel, "w", encoding="utf-8") as handle:
        handle.write("held")
    time.sleep(hold)
"""

#: A child module whose graph fixture takes the REAL lock. ``{lock!r}`` decides whether it
#: is contended; ``{defect}`` adds a genuine product failure alongside it.
_CHILD = """
import pytest
from proxyshop_support.neo4j_lock import neo4j_flock

@pytest.fixture
def graph_guard():
    with neo4j_flock(timeout=0.4, poll=0.05, path={lock!r}, report=lambda m: None):
        yield True

def test_plain():
    assert True

def test_touches_the_graph(graph_guard):
    assert graph_guard
{defect}
"""

_DEFECT = """
def test_a_real_product_defect():
    assert False, "simulated product defect"
"""


def _hold(lock: Path, sentinel: Path, scratch: Path, seconds: float) -> subprocess.Popen[bytes]:
    script = scratch / "hold_for_exit_status.py"
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
        assert holder.poll() is None, "the holder died before taking the lock"
        assert time.monotonic() < deadline, "the holder never took the lock"
        time.sleep(0.02)
    return holder


def _reap(holder: subprocess.Popen[bytes]) -> None:
    holder.terminate()
    try:
        holder.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - the OS has bigger problems
        holder.kill()
        holder.wait(timeout=30)


def _run(module: Path, *, plugin: bool) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "pytest", str(module), "-q", "-p", "no:cacheprovider"]
    if plugin:
        argv += ["-p", "proxyshop_support.lock_exit_status"]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv + ["-o", "addopts=", "--timeout=60", "--tb=no"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PROXYSHOP_NEO4J_LOCK_LOG": "0"},
        capture_output=True,
        text=True,
        timeout=180,
    )


def _statuses(*, plugin: bool) -> dict[str, int]:
    """``{green, contention, defect, both}`` -> exit status, from four real child runs."""
    out: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)
        lock = scratch / "exit_status.lock"

        clean = scratch / "test_clean_child.py"
        clean.write_text(_CHILD.format(lock=str(lock), defect=""), encoding="utf-8")
        broken = scratch / "test_broken_child.py"
        broken.write_text(_CHILD.format(lock=str(lock), defect=_DEFECT), encoding="utf-8")

        out["green"] = _run(clean, plugin=plugin).returncode
        out["defect"] = _run(broken, plugin=plugin).returncode

        holder = _hold(lock, scratch / "exit_status.held", scratch, 60.0)
        try:
            out["contention"] = _run(clean, plugin=plugin).returncode
            out["both"] = _run(broken, plugin=plugin).returncode
        finally:
            _reap(holder)
    return out


def test_the_exit_status_children_are_armed() -> None:
    """Without the plugin the four cases collapse to two statuses — the T-210 defect.

    This is the baseline the plugin is measured against, and it is also the vacuity guard:
    if the holder failed to hold, or the generated child were broken, ``contention`` would
    equal ``green`` here and every assertion in the next test would be about nothing.
    """
    unpatched = _statuses(plugin=False)
    assert unpatched["green"] == 0, f"the generated child does not pass clean: {unpatched}"
    assert unpatched["contention"] != 0, (
        f"a contended run exited 0, so the holder never held and there is no contention "
        f"to grade: {unpatched}"
    )
    assert unpatched["contention"] == unpatched["defect"], (
        f"without the plugin, contention and a product defect are ALREADY "
        f"distinguishable ({unpatched}) — then T-210 is not what this file says it is"
    )


def test_the_plugin_gives_contention_its_own_status_and_only_then() -> None:
    """Four outcomes, and the machine condition is the only one that moves."""
    patched = _statuses(plugin=True)
    assert patched["green"] == 0, f"the plugin broke a clean run: {patched}"
    assert patched["contention"] == LOCK_CONTENTION_EXIT_STATUS, (
        f"lock contention did not get its own status: {patched}"
    )
    assert patched["defect"] == 1, (
        f"a real product defect no longer exits 1 — the plugin is firing on evidence it "
        f"should not have: {patched}"
    )
    assert patched["both"] == 1, (
        f"a run containing BOTH a lock timeout and a real product defect was laundered "
        f"into the machine-condition status ({patched}). That hides a defect, which is "
        f"strictly worse than the ambiguity T-210 is about."
    )
    assert LOCK_CONTENTION_EXIT_STATUS not in (0, 1, 2, 5), (
        "the machine-condition status collides with a status that already means something: "
        "0 is success, 1 is a test failure, 2 is verify.sh's own FATAL, and verify.sh "
        "remaps pytest's 5 to 1"
    )


@pytest.mark.parametrize(
    ("status", "timeouts", "others", "reports", "expected"),
    [
        (1, 1, 0, 1, True),  # the only case that fires
        (0, 0, 0, 0, False),  # a green run
        (1, 0, 1, 1, False),  # a product defect
        (1, 1, 1, 2, False),  # both: never launder a real failure
        (2, 1, 0, 1, False),  # verify.sh's FATAL, or a usage error
        (5, 1, 0, 1, False),  # an empty collection: verify.sh already remaps this to 1
        (3, 1, 0, 1, False),  # INTERNALERROR
        (1, 1, 0, 2, False),  # an unexplained failing report: fail CLOSED
    ],
)
def test_the_evidence_gate_fires_on_exactly_one_shape(
    status: int, timeouts: int, others: int, reports: int, expected: bool
) -> None:
    """The decision itself, case by case, including the ones that must NOT fire.

    The end-to-end tests above cannot reach an INTERNALERROR or a torn report count on
    demand; this reaches every branch. It grades :func:`is_lock_contention_only` rather than
    a string, so there is no source text anywhere in the comparison.
    """
    counts = lock_exit_status._Tally(
        lock_timeouts=timeouts, other_failures=others, failing_reports=reports
    )
    assert lock_exit_status.is_lock_contention_only(status, counts) is expected
