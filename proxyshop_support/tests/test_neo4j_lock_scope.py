"""T-214 — the D37 flock must be held for the GRAPH WORK, not for the whole session.

The measured defect
-------------------
``_neo4j_guard`` in the root ``conftest.py`` was ``scope="session"`` and wrapped its whole
body in ``with neo4j_flock():``. pytest builds a session fixture once, on first request, and
tears it down at the *end of the session* — so the very first Neo4j-touching test in a run
took the machine-global flock on ``/tmp/proxyshop-neo4j.lock`` and the run then held it
through every remaining test, graph or not.

That was harmless only while nothing ran graph tests. It stopped being harmless when
``scripts/verify.sh check`` dropped its blanket ``not docker`` deselection: every one of the
113 ``graph``-marked tests is also ``docker``-marked, so ``check`` — which **every lane
runs** — began building the guard. Measured on a real run: the lock was held from t=25.1 s
to t=220.8 s of a 221.78 s session, 88 % of it, while ~3 800 tests that never touch Neo4j
ran inside the hold. Concurrent lanes therefore serialised on a machine-global lock and the
loser failed red with ``Neo4jLockTimeout`` for a machine reason, not for its own code.

What this file pins, and why all three assertions are needed
------------------------------------------------------------
1. :func:`test_the_neo4j_guard_is_not_session_scoped` — the structural cause, read out of
   the conftest source. Cheap, and it names the fix in its failure message.
2. :func:`test_the_flock_is_released_before_the_non_graph_tail_of_a_session` — the
   behavioural gate. A **real child pytest session** driving the **real root conftest** is
   probed from this process at 50 Hz through a private lock file, and must show the lock
   free while the session's non-graph tail runs. This is the assertion the structural one
   cannot make: a fixture could be narrowed in scope and still leak the lock.
3. :func:`test_two_concurrent_sessions_still_exclude_each_other_on_the_graph` — the
   guard-rail on the fix. "Hold it less" has a trivial wrong answer (hold it never), and
   deleting the lock would make (1) and (2) pass. Two child sessions run their graph tests
   at the same time and their in-lock windows must not overlap.

Everything here uses ``$PROXYSHOP_NEO4J_LOCK`` to point the child sessions at a private lock
file under ``tmp_path``. Nothing in this module touches ``/tmp/proxyshop-neo4j.lock``, so it
neither blocks a sibling worker nor is blocked by one.
"""

from __future__ import annotations

import ast
import fcntl
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFTEST = REPO_ROOT / "conftest.py"

#: How long the child's single graph test stays inside the flock.
GRAPH_SECONDS = 0.6

#: How long the child spends afterwards on tests that touch no datastore at all. Under the
#: session-scoped guard the lock was held for every second of this; under a narrowed one it
#: is held for none of it. Long enough that the two outcomes cannot be confused for jitter.
TAIL_SECONDS = 3.0

#: The window at the end of the child session that must be lock-free. Strictly inside
#: ``TAIL_SECONDS`` so it can only ever cover non-graph work.
TAIL_WINDOW = 2.0

#: Probe period for the out-of-process lock sampler.
PROBE_PERIOD = 0.02


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _child_env(lock_path: Path) -> dict[str, str]:
    """The environment a child pytest session runs in.

    ``PYTHONPATH`` is rebuilt with **this** worktree first. The venv ships a
    ``_proxyshop.pth`` naming the primary checkout, so a child that inherited the ambient
    path could import the primary tree's ``proxyshop_support`` and grade code this branch
    never changed. The child asserts the resolved path back (see :func:`_write_child_suite`)
    rather than trusting this.
    """
    env = dict(os.environ)
    roots = f"{REPO_ROOT}{os.pathsep}{REPO_ROOT / '.pkgroot'}"
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{roots}{os.pathsep}{inherited}" if inherited else roots
    env["PROXYSHOP_NEO4J_LOCK"] = str(lock_path)
    env["PROXYSHOP_EXPECTED_SUPPORT_ROOT"] = str(REPO_ROOT)
    env.setdefault("PROXYSHOP_WORKER", "1")
    return env


def _write_child_suite(root: Path, *, tail_tests: int) -> None:
    """A miniature repo: the REAL root conftest plus one graph test and a non-graph tail.

    The conftest is copied byte-for-byte, so the fixtures under test are the ones this
    branch ships. Files are named so pytest's alphabetical collection runs the graph test
    first and the tail after it — the ordering the defect needs to be visible in.
    """
    shutil.copy(CONFTEST, root / "conftest.py")
    (root / "test_a_graph.py").write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def test_holds_the_flock(_neo4j_guard):\n"
        "    from proxyshop_support import neo4j_lock\n"
        "    expected = Path(os.environ['PROXYSHOP_EXPECTED_SUPPORT_ROOT'])\n"
        "    resolved = Path(neo4j_lock.__file__).resolve()\n"
        "    assert expected in resolved.parents, resolved\n"
        "    assert _neo4j_guard is True\n"
        "    assert neo4j_lock.held_depth() >= 1\n"
        f"    time.sleep({GRAPH_SECONDS})\n"
    )
    per_test = TAIL_SECONDS / tail_tests
    body = "import time\n\n\n"
    for index in range(tail_tests):
        body += f"def test_no_datastore_{index}():\n    time.sleep({per_test})\n\n\n"
    (root / "test_b_tail.py").write_text(body)


class _LockProbe:
    """Samples "is the flock held by anyone else" from this process at :data:`PROBE_PERIOD`.

    A separate ``open()`` gives a separate open file description, which is what ``flock``
    arbitrates on — so a non-blocking ``LOCK_EX`` here fails exactly while the child holds
    the lock, and succeeds (and is immediately released) while it does not.
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        #: (monotonic timestamp, held-by-someone-else) in sample order.
        self.samples: list[tuple[float, bool]] = []

    def __enter__(self) -> _LockProbe:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        with self._lock_path.open("a+") as handle:
            while not self._stop.is_set():
                stamp = time.monotonic()
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self.samples.append((stamp, True))
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    self.samples.append((stamp, False))
                time.sleep(PROBE_PERIOD)

    def held_seconds(self, *, since: float = 0.0, until: float = float("inf")) -> float:
        """Seconds the lock was observed held inside ``[since, until]`` (monotonic)."""
        total = 0.0
        for (start, held), (end, _) in zip(self.samples, self.samples[1:], strict=False):
            if not held:
                continue
            overlap = min(end, until) - max(start, since)
            if overlap > 0:
                total += overlap
        return total


def _run_child(root: Path, env: dict[str, str], *, timeout: float = 180.0) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(root)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.returncode, completed.stdout + completed.stderr


# --------------------------------------------------------------------------------------
# 1. the structural cause
# --------------------------------------------------------------------------------------


def _guard_fixture_node() -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(CONFTEST.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "_neo4j_guard":
            return node
    raise AssertionError(f"{CONFTEST} no longer defines `_neo4j_guard`")


def _fixture_scope(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        target = decorator.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name != "fixture":
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "scope" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
    return "function"


def test_the_neo4j_guard_is_not_session_scoped() -> None:
    """The fixture that takes the flock must not outlive the work that needs it."""
    scope = _fixture_scope(_guard_fixture_node())
    assert scope == "function", (
        f"`_neo4j_guard` is scope={scope!r}. It wraps its body in `neo4j_flock()`, and "
        f"pytest tears a non-function-scoped fixture down at the END of that scope — so a "
        f"session-scoped guard holds the MACHINE-GLOBAL lock /tmp/proxyshop-neo4j.lock from "
        f"the first Neo4j-touching test until the session exits (measured: 196 s of a 222 s "
        f"`check` run, 88 %, with ~3 800 non-graph tests running inside the hold). Every "
        f"lane runs `check`, so concurrent lanes serialise on it and the loser fails red "
        f"with Neo4jLockTimeout for a machine reason. Take the flock per test instead."
    )


# --------------------------------------------------------------------------------------
# 2. the behaviour — a real child session, probed from outside
# --------------------------------------------------------------------------------------


@pytest.mark.docker("neo4j-bolt")
def test_the_flock_is_released_before_the_non_graph_tail_of_a_session(tmp_path: Path) -> None:
    """The lock must be free while a session runs work that does not touch the graph."""
    root = tmp_path / "child"
    root.mkdir()
    lock_path = tmp_path / "t214-neo4j.lock"
    _write_child_suite(root, tail_tests=6)

    with _LockProbe(lock_path) as probe:
        started = time.monotonic()
        code, output = _run_child(root, _child_env(lock_path))
        finished = time.monotonic()

    assert code == 0, f"the child session did not pass, so its timing proves nothing:\n{output}"

    session = finished - started
    held = probe.held_seconds()
    tail_held = probe.held_seconds(since=finished - TAIL_WINDOW)
    report = (
        f"session={session:.2f}s held={held:.2f}s "
        f"({held / session:.0%}) held-in-last-{TAIL_WINDOW:g}s={tail_held:.2f}s"
    )

    # The probe has to be able to see the lock at all, or every assertion below is vacuous.
    assert held >= GRAPH_SECONDS / 2, (
        f"the flock was never observed held during a session whose graph test asserts "
        f"held_depth() >= 1 ({report}). The probe, not the fixture, is what is broken — or "
        f"the lock has been removed, which is not the fix (see the exclusion test below)."
    )

    assert tail_held <= PROBE_PERIOD * 3, (
        f"the flock was still held {tail_held:.2f}s into the last {TAIL_WINDOW:g}s of the "
        f"session, which is entirely tests that touch no datastore ({report}). A "
        f"session-scoped guard does exactly this: it takes the lock at the first graph test "
        f"and releases it at session teardown, so every lane's non-graph work runs inside a "
        f"machine-global lock and concurrent lanes serialise on it."
    )

    assert held <= session / 2, (
        f"the flock was held for {held / session:.0%} of the session ({report}). It should "
        f"be held for about the graph tests' own runtime (~{GRAPH_SECONDS:g}s here), not "
        f"for the session's."
    )


# --------------------------------------------------------------------------------------
# 3. the guard-rail — narrower must not mean absent
# --------------------------------------------------------------------------------------


def _write_exclusion_suite(root: Path, journal: Path) -> None:
    """A child whose graph test records the window it spent inside the flock."""
    shutil.copy(CONFTEST, root / "conftest.py")
    (root / "test_a_graph.py").write_text(
        "import os, time\n"
        "\n"
        "\n"
        "def test_inside_the_flock(_neo4j_guard):\n"
        "    from proxyshop_support.neo4j_lock import held_depth\n"
        "    assert _neo4j_guard is True\n"
        "    assert held_depth() >= 1\n"
        "    entered = time.time()\n"
        "    time.sleep(0.5)\n"
        "    left = time.time()\n"
        "    line = f'{os.environ[\"PROXYSHOP_CHILD_NAME\"]} {entered!r} {left!r}\\n'\n"
        "    fd = os.open(os.environ['PROXYSHOP_JOURNAL'], os.O_WRONLY | os.O_APPEND)\n"
        "    os.write(fd, line.encode())\n"
        "    os.close(fd)\n"
    )
    journal.touch()


@pytest.mark.docker("neo4j-bolt")
def test_two_concurrent_sessions_still_exclude_each_other_on_the_graph(tmp_path: Path) -> None:
    """Narrowing the hold must not weaken what the hold is FOR (D37).

    Neo4j Community has exactly one database, so two sessions writing the graph at the same
    time corrupt each other's assertions. Both children below take the same lock file at the
    same moment; their in-lock windows must be disjoint.
    """
    lock_path = tmp_path / "t214-exclusion.lock"
    journal = tmp_path / "windows.txt"
    roots = []
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        _write_exclusion_suite(root, journal)
        roots.append((name, root))

    processes = []
    for name, root in roots:
        env = _child_env(lock_path)
        env["PROXYSHOP_CHILD_NAME"] = name
        env["PROXYSHOP_JOURNAL"] = str(journal)
        processes.append(
            subprocess.Popen(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(root)],
                cwd=root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        )

    outputs = [process.communicate(timeout=180)[0] for process in processes]
    codes = [process.returncode for process in processes]
    assert codes == [0, 0], f"a child session failed:\n{outputs[0]}\n----\n{outputs[1]}"

    windows = {}
    for line in journal.read_text().splitlines():
        name, entered, left = line.split()
        windows[name] = (float(entered), float(left))
    assert sorted(windows) == ["alpha", "beta"], (
        f"both children must have recorded a window, else nothing was compared: {windows}"
    )

    (a_start, a_end), (b_start, b_end) = windows["alpha"], windows["beta"]
    overlap = min(a_end, b_end) - max(a_start, b_start)
    assert overlap <= 0, (
        f"two pytest sessions were inside the D37 flock at the same time for {overlap:.3f}s "
        f"(alpha {a_start:.3f}..{a_end:.3f}, beta {b_start:.3f}..{b_end:.3f}). Neo4j "
        f"Community has ONE database (D4), so the flock is the only isolation that exists — "
        f"holding it for less time must not mean holding it for none."
    )
