"""T-210 — give Neo4j lock contention its own process exit status (a pytest plugin).

The defect
----------
``build_succeeds`` is ``if make verify; then echo 1; else echo 0; fi``. A run that failed
only because a sibling worker held the machine-global D37 flock exits **1** — byte for byte
what a run that failed on a real product defect exits — so the frozen probe records 0 for a
machine condition and nothing downstream can tell the two apart. Measured at HEAD by
``proxyshop_support/tests/test_repro_open_tickets.py::
test_t210_lock_contention_and_a_product_defect_do_not_share_an_exit_status``: over 20 drawn
shapes, contention exits ``[1]`` and a product defect exits ``[1]``.

Neo4j Community has exactly ONE database (D4), so every worktree on this host serialises on
one flock and contention is the NORMAL case in a parallel swarm — six false zeros are
already in ``history.csv``.

What this does
--------------
Nothing, until it is registered as a pytest plugin. Registered, it re-stamps
``session.exitstatus`` to :data:`LOCK_CONTENTION_EXIT_STATUS` — and ONLY when the whole
run's evidence says the sole reason it failed was that another process held the lock.

**Machine triage, not a cure, and the difference matters.** ``build_succeeds`` reads
zero-versus-non-zero and never the value, so 77 is still a zero there: this does NOT stop
the false zeros. What it buys is that an orchestrator reading the child process can tell a
machine condition from a defect without a human, which is the prerequisite for the
scheduler-level cure the ticket says is the real fix (never measure concurrently with a
lane that touches the graph).

Why 77, and why not the obvious alternatives
--------------------------------------------
* **not 5** — ``scripts/verify.sh``'s ``run_pytest`` maps pytest's exit 5 to 1, which would
  launder the machine condition straight back into a product failure;
* **not 2** — that is ``verify.sh``'s own FATAL status (``PROXYSHOP_WORKER`` unset, missing
  venv), and colliding with it would misattribute in the other direction;
* **not 0** — a machine condition that reports success is invisible in the metrics rather
  than merely misattributed, which is strictly worse. The same argument rules out skipping;
* **77** survives ``verify.sh``'s ``run_pytest`` unchanged: ``rc=${PIPESTATUS[0]}`` recovers
  it from behind the ``tee`` and the function ``return``s it, and that file is hash-frozen
  so nothing here may depend on changing it.

The evidence gate, which is the whole design
--------------------------------------------
Firing this on anything less than complete evidence would hide real defects, so every
condition below must hold and each one fails CLOSED:

1. pytest's own status is exactly 1 (``TESTS_FAILED``). An internal error, an interrupt, a
   usage error or an empty collection is left alone;
2. at least one failure was a :class:`~proxyshop_support.neo4j_lock.Neo4jLockTimeout`;
3. no failure was anything else;
4. the number of failing reports the session produced equals the number of lock timeouts
   seen. This is the belt to (3)'s braces: a failure that never reaches
   ``pytest_exception_interact`` — an ``XPASS(strict)``, a collection error, a
   ``pytest.fail`` raised in a teardown hook — is counted here and not there, so an
   unexplained failing report stops the re-stamp even though nothing classified it.

Registering it
--------------
Not automatic on purpose: a plugin that installs itself by import is a plugin nobody can
find when it misbehaves. In the root ``conftest.py`` — which is itself a pytest plugin, so
every ``pytest_*`` name in its namespace becomes a hook::

    from proxyshop_support.lock_exit_status import (  # noqa: F401
        pytest_collectreport,
        pytest_exception_interact,
        pytest_runtest_logreport,
        pytest_sessionfinish,
        pytest_sessionstart,
    )

**All five, and that is not tidiness.** Importing ``pytest_sessionfinish`` alone gives a
re-stamp with an empty tally, which by construction never fires — a wiring that looks
right, imports cleanly, passes every lint, and does nothing. Measured, with a real held
lock and a scratch conftest: all five imported gives ``green 0 / contention 77 / defect 1 /
both 1``; ``pytest_sessionfinish`` alone gives ``contention 1``, i.e. the defect unchanged.
None of the five collides with a hook the root conftest already defines
(``pytest_configure``, ``pytest_collection_modifyitems``).

For a single run, ``pytest -p proxyshop_support.lock_exit_status`` registers the whole
module — which is how ``proxyshop_support/tests/test_lock_exit_status.py`` grades it end to
end without touching a file outside this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from proxyshop_support.neo4j_lock import Neo4jLockTimeout

#: The status a run whose ONLY failure was cross-worker Neo4j lock contention exits with.
LOCK_CONTENTION_EXIT_STATUS = 77

#: pytest's ``ExitCode.TESTS_FAILED``. Spelled as an int rather than imported so this module
#: stays importable with no pytest in the environment — it is imported by the root conftest,
#: which runs before plugins are resolved.
_TESTS_FAILED = 1


@dataclass
class _Tally:
    """What the session saw, from two independent channels."""

    #: failures whose exception was a Neo4jLockTimeout.
    lock_timeouts: int = 0
    #: failures whose exception was anything else.
    other_failures: int = 0
    #: every failing/erroring report, classified or not. See condition 4 in the module docs.
    failing_reports: int = 0
    #: node ids of the lock timeouts, for the message.
    contended: list[str] = field(default_factory=list)


_TALLY = _Tally()


def _reset() -> None:
    """Start a fresh tally. Called at session start; exposed for the tests."""
    global _TALLY
    _TALLY = _Tally()


def tally() -> _Tally:
    """The current session's tally. Diagnostic; never an input to product code."""
    return _TALLY


def pytest_sessionstart(session: Any) -> None:  # noqa: ARG001 - pytest hook signature
    _reset()


def pytest_exception_interact(node: Any, call: Any, report: Any) -> None:
    """Classify each failure by the exception that actually caused it.

    ``call.excinfo`` is the real exception, not a rendered string — which matters because
    ``Neo4jLockTimeout`` does not survive to stdout under ``--tb=no`` and is truncated to
    ``proxyshop_support.neo4j_lock.Neo4jLockTime...`` at 80 columns in the short summary.
    Anything this cannot positively identify as a lock timeout counts against firing.
    """
    if getattr(report, "outcome", None) != "failed":
        return
    excinfo = getattr(call, "excinfo", None)
    if excinfo is not None and excinfo.errisinstance(Neo4jLockTimeout):
        _TALLY.lock_timeouts += 1
        _TALLY.contended.append(str(getattr(report, "nodeid", node)))
    else:
        _TALLY.other_failures += 1


def pytest_runtest_logreport(report: Any) -> None:
    """Count every failing report, including ones no classifier ever saw."""
    if getattr(report, "failed", False):
        _TALLY.failing_reports += 1


def pytest_collectreport(report: Any) -> None:
    """A collection error is a failure too, and it never reaches the classifier."""
    if getattr(report, "failed", False):
        _TALLY.failing_reports += 1


def is_lock_contention_only(exitstatus: int, counts: _Tally | None = None) -> bool:
    """Whether the evidence says this run failed for the lock and nothing else.

    Separated from the hook so the decision can be exercised directly, and so the hook
    itself is three lines nobody has to reason about.
    """
    counts = _TALLY if counts is None else counts
    return (
        int(exitstatus) == _TESTS_FAILED
        and counts.lock_timeouts > 0
        and counts.other_failures == 0
        and counts.failing_reports == counts.lock_timeouts
    )


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Re-stamp the session's status, and say so on stderr so a human sees it too."""
    if not is_lock_contention_only(exitstatus):
        return
    session.exitstatus = LOCK_CONTENTION_EXIT_STATUS
    import sys

    print(
        f"[neo4j-lock] exit {LOCK_CONTENTION_EXIT_STATUS}: this run's only failure was "
        f"cross-worker Neo4j lock contention (D37), not a product defect. Contended: "
        f"{', '.join(_TALLY.contended)}. Nothing is wrong with the code under test; a "
        f"sibling worker's graph lane was holding the machine-global flock.",
        file=sys.stderr,
        flush=True,
    )
