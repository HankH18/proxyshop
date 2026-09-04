"""``scripts/db_reclaim.py`` — the refusals, not the reclamation (P3b).

Every test here is about something the sweep must *decline* to do. That bias is the point:
a reclamation tool's failure mode is not "missed a stale database", it is "dropped a live
lane's state", and a lane sitting between two tests holds no connection, so its database
looks exactly like an abandoned one at that instant.

No Docker: the classifier is pure and the CLI's refusals happen before any connection is
opened, so all of it is checkable with the stack down. The parts that genuinely need a
cluster — the per-candidate liveness re-read and the plain ``DROP`` — are exercised against
the live cluster in the change's verification, and the shape of the code that does it is
pinned statically at the bottom of this file.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "db_reclaim.py"
HYGIENE_TEST = REPO_ROOT / "apps" / "trust" / "tests" / "test_ledger_schema_state_hygiene.py"


def _load() -> ModuleType:
    """Import the script by path — ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("db_reclaim_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


db_reclaim = _load()


def _classify(name: str, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "protected_indexes": set(),
        "leased": None,
        "include_scratch": False,
    }
    kwargs.update(overrides)
    return db_reclaim.classify(name, 1024, **kwargs)


# --------------------------------------------------------------------------------------
# what may never be considered
# --------------------------------------------------------------------------------------


def test_worker_zero_is_never_a_candidate() -> None:
    """``PROXYSHOP_WORKER=0 make verify`` is the frozen ``build_succeeds`` metric."""
    verdict = _classify("proxyshop_w0")
    assert verdict.verdict == "protected"
    assert "build_succeeds" in verdict.reason


def test_worker_zero_is_not_a_candidate_even_with_every_flag_set() -> None:
    """No flag combination may reach it — the protection is unconditional, not a default."""
    verdict = _classify("proxyshop_w0", include_scratch=True, leased=set())
    assert verdict.verdict == "protected"


def test_the_template_and_maintenance_databases_are_not_worker_databases() -> None:
    for name in ("proxyshop_template", "postgres", "template0", "template1"):
        assert _classify(name).verdict == "protected"


def test_an_unrecognised_shape_is_protected_rather_than_swept() -> None:
    """A sweeper that deletes what it cannot name is not a sweeper."""
    verdict = _classify("proxyshop_wibble")
    assert verdict.verdict == "protected"
    assert "unrecognised" in verdict.reason


def test_a_leased_slot_is_protected() -> None:
    assert _classify("proxyshop_w7", leased={7}).verdict == "protected"


def test_an_explicit_protect_list_is_honoured() -> None:
    verdict = _classify("proxyshop_w29", protected_indexes={29})
    assert verdict.verdict == "protected"
    assert "protect list" in verdict.reason


def test_an_unleased_unprotected_worker_database_is_a_candidate() -> None:
    """The negative control: without this, every test above could pass vacuously."""
    assert _classify("proxyshop_w29", leased={7}).verdict == "candidate"


# --------------------------------------------------------------------------------------
# the two per-test scratch shapes — the race hazard
# --------------------------------------------------------------------------------------


def test_the_reserved_scratch_band_is_protected_by_default() -> None:
    """``_throwaway_worker_database`` mints one of these per test and drops it in a
    ``finally``; it may be seconds old and about to be connected to."""
    verdict = _classify("proxyshop_w800013")
    assert verdict.verdict == "protected"
    assert "scratch band" in verdict.reason


@pytest.mark.parametrize("index", [800_000, 812_345, 899_999])
def test_the_whole_scratch_band_is_covered(index: int) -> None:
    assert _classify(f"proxyshop_w{index}").verdict == "protected"


@pytest.mark.parametrize("index", [799_999, 900_000])
def test_the_band_edges_do_not_over_reach(index: int) -> None:
    """An over-wide band silently spares real strays forever, which is its own defect."""
    assert _classify(f"proxyshop_w{index}").verdict == "candidate"


def test_the_migrationcheck_shape_is_protected_by_default() -> None:
    """``ledger_second_database``'s per-test database (``_fixtures_ledger_schema.py``)."""
    verdict = _classify("proxyshop_w4_migrationcheck_ab12cd34")
    assert verdict.verdict == "protected"
    assert "ledger_second_database" in verdict.reason


def test_a_migrationcheck_name_is_not_mistaken_for_its_worker_index() -> None:
    """``proxyshop_w4_migrationcheck_...`` must not be read as worker 4 and swept as one."""
    verdict = _classify("proxyshop_w4_migrationcheck_ab12cd34", leased=set())
    assert verdict.index is None
    assert verdict.verdict == "protected"


def test_scratch_shapes_become_candidates_only_when_asked_for_explicitly() -> None:
    for name in ("proxyshop_w800013", "proxyshop_w4_migrationcheck_ab12cd34"):
        assert _classify(name, include_scratch=True).verdict == "candidate"


def test_the_reserved_band_matches_the_fixture_that_mints_into_it() -> None:
    """Pin the band to ``SCRATCH_WORKER_BASE`` at its definition site.

    Read out of the source with ``ast`` rather than imported: ``apps/trust/tests`` is a test
    directory, not an importable package, and a script that imported one to learn a
    constant would be a worse coupling than this one. If the fixture moves its band, this
    fails and names both halves.
    """
    tree = ast.parse(HYGIENE_TEST.read_text(encoding="utf-8"))
    found = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SCRATCH_WORKER_BASE"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
    ]
    assert found == [db_reclaim.SCRATCH_WORKER_BASE], (
        f"{HYGIENE_TEST.name} mints throwaway databases from SCRATCH_WORKER_BASE={found}, "
        f"but db_reclaim.py reserves the band starting at "
        f"{db_reclaim.SCRATCH_WORKER_BASE}. A sweep would consider databases a live test "
        f"is about to use."
    )


def test_the_scratch_band_covers_every_index_the_fixture_can_mint() -> None:
    """``BASE + worker*1000 + randbelow(1000)``, for any worker the band is meant to hold."""
    base = db_reclaim.SCRATCH_WORKER_BASE
    for worker in (0, 1, 15, 99):
        for offset in (0, 999):
            assert db_reclaim.in_scratch_band(base + worker * 1000 + offset)


# --------------------------------------------------------------------------------------
# --apply cannot fire by accident
# --------------------------------------------------------------------------------------


def test_dry_run_is_the_default() -> None:
    assert db_reclaim.build_parser().parse_args([]).apply is False


def test_dry_run_and_apply_are_mutually_exclusive() -> None:
    """So ``--dry-run --apply`` is a parse error rather than a silent precedence rule."""
    with pytest.raises(SystemExit):
        db_reclaim.build_parser().parse_args(["--dry-run", "--apply"])


def test_apply_is_refused_without_a_lease_source(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second gate. Refused **before** any connection is opened, so it cannot be
    reached by a cluster that happens to look idle."""
    monkeypatch.delenv(db_reclaim.LEASE_COMMAND_ENV, raising=False)
    monkeypatch.setattr(
        db_reclaim,
        "read_leases",
        lambda command: (None, "no lease source configured"),
    )

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("db_reclaim opened a connection on a refused --apply")

    monkeypatch.setattr("psycopg.connect", explode)
    assert db_reclaim.main(["--apply"]) == 3
    assert "REFUSING --apply" in capsys.readouterr().err


def test_the_refusal_explains_the_lane_between_two_tests(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal has to say *why*, or the next person just adds the flag."""
    monkeypatch.setattr(db_reclaim, "read_leases", lambda command: (None, "none"))
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: pytest.fail("connected"))
    db_reclaim.main(["--apply"])
    assert "between two tests" in capsys.readouterr().err


def test_a_bad_protect_entry_is_an_error_not_a_silent_skip() -> None:
    """Ignoring an unparsable ``--protect`` would spare nothing while looking like it did."""
    with pytest.raises(db_reclaim.ReclaimError):
        db_reclaim.parse_protect(["notanindex"])


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (["3"], {3}),
        (["w3"], {3}),
        (["proxyshop_w3"], {3}),
        (["3,7", "w29"], {3, 7, 29}),
    ],
)
def test_protect_accepts_the_spellings_a_human_has_in_front_of_them(
    given: list[str], expected: set[int]
) -> None:
    assert db_reclaim.parse_protect(given) == expected


def test_the_running_workers_own_database_is_protected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whoever runs this is presumably about to use their own database."""
    monkeypatch.setenv("PROXYSHOP_WORKER", "13")
    seen: dict[str, Any] = {}

    def fake_sweep(_connection: object, **kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(db_reclaim, "sweep", fake_sweep)
    monkeypatch.setattr(db_reclaim, "database_count", lambda _connection: 0)
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: _NullConnection())
    assert db_reclaim.main([]) == 0
    assert 13 in seen["protected_indexes"]


class _NullConnection:
    def __enter__(self) -> _NullConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# --------------------------------------------------------------------------------------
# lease parsing
# --------------------------------------------------------------------------------------


def test_no_lease_command_is_reported_rather_than_assumed_empty() -> None:
    """``None`` means unknown. An empty set would mean "nothing is leased", which is a very
    different and much more dangerous claim."""
    leased, note = db_reclaim.read_leases(None)
    assert leased is None
    assert "no lease source" in note


def test_a_failing_lease_command_yields_unknown_not_empty() -> None:
    leased, note = db_reclaim.read_leases("exit 7")
    assert leased is None
    assert "exited 7" in note


def test_non_json_output_yields_unknown() -> None:
    leased, _ = db_reclaim.read_leases("echo not json")
    assert leased is None


@pytest.mark.parametrize(
    "command",
    [
        """python3 -c 'print("[1, 2, 3]")'""",
        """python3 -c 'print("[{\\"worker\\": 1}, {\\"worker\\": 2}, {\\"worker\\": 3}]")'""",
        """python3 -c 'print("{\\"slots\\": [{\\"index\\": 1}, {\\"index\\": 2}, 3]}")'""",
    ],
)
def test_leases_are_read_from_the_shapes_a_slot_pool_might_print(command: str) -> None:
    leased, _ = db_reclaim.read_leases(command)
    assert leased == {1, 2, 3}


# --------------------------------------------------------------------------------------
# the shape of the destructive path, pinned statically
# --------------------------------------------------------------------------------------


def test_the_drop_is_plain_with_no_force_and_no_terminate_backend() -> None:
    """Postgres refusing to drop a database with backends IS the safety mechanism.

    ``WITH (FORCE)`` or a ``pg_terminate_backend`` sweep would remove exactly the last line
    of defence, so this is checked in the source rather than trusted to review.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    executed = [
        argument.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    executed += [
        part.value.strip()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.JoinedStr)
        for part in node.values
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    ]
    offenders = [
        statement
        for statement in executed
        if "pg_terminate_backend" in statement.lower()
        or ("drop database" in statement.lower() and "force" in statement.lower())
    ]
    assert offenders == [], f"the sweep gained a forced drop path: {offenders!r}"


# --------------------------------------------------------------------------------------
# the destructive path, driven hermetically
#
# Never against a real cluster: the swarm's lanes share one, and a test that drops a
# database to prove it can drop a database is the accident it is meant to rule out. A fake
# connection is enough, because what matters is the ORDER of the reads and the drop.
# --------------------------------------------------------------------------------------


class _FakeConnection:
    """Records SQL, and answers the liveness query from a scripted list of results."""

    def __init__(self, databases: list[tuple[str, int]], liveness: list[list[tuple[Any, ...]]]):
        self._databases = databases
        self._liveness = list(liveness)
        self.executed: list[str] = []
        self.liveness_reads = 0

    # -- cursor protocol -------------------------------------------------------------
    def cursor(self) -> _FakeConnection:
        return self

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: str, params: Any = None) -> None:
        self.executed.append(" ".join(statement.split()))
        self._last = statement

    def fetchall(self) -> list[tuple[Any, ...]]:
        if "pg_stat_activity" in self._last:
            self.liveness_reads += 1
            return self._liveness.pop(0) if self._liveness else []
        return [(name, size) for name, size in self._databases]

    def fetchone(self) -> tuple[Any, ...]:
        return (len(self._databases),)


def _sweep(connection: _FakeConnection, **overrides: Any) -> list[Any]:
    kwargs: dict[str, Any] = {
        "protected_indexes": set(),
        "leased": set(),
        "include_scratch": False,
        "apply": True,
    }
    kwargs.update(overrides)
    return db_reclaim.sweep(connection, **kwargs)


def test_apply_drops_a_candidate_with_a_plain_statement() -> None:
    connection = _FakeConnection([("proxyshop_w29", 1024)], liveness=[[], []])
    results = _sweep(connection)
    assert [c.verdict for c in results] == ["dropped"]
    dropped = [s for s in connection.executed if s.startswith("DROP DATABASE")]
    assert dropped == ['DROP DATABASE "proxyshop_w29"']


def test_liveness_is_read_twice_before_a_drop() -> None:
    """Once to decide, once immediately before acting. The gap between them is the race."""
    connection = _FakeConnection([("proxyshop_w29", 1024)], liveness=[[], []])
    _sweep(connection)
    assert connection.liveness_reads == 2


def test_a_backend_appearing_after_the_verdict_stops_the_drop() -> None:
    """The scenario the whole design is for: idle when judged, connected a moment later.

    The first read sees nothing, the second sees a backend — exactly a lane picking its
    database back up between two tests. Nothing may be dropped.
    """
    connection = _FakeConnection(
        [("proxyshop_w29", 1024)],
        liveness=[[], [(4242, "proxyshop", "pytest", "active", None)]],
    )
    results = _sweep(connection)
    assert [c.verdict for c in results] == ["protected"]
    assert "between the check and the drop" in results[0].reason
    assert "4242" in results[0].reason
    assert not [s for s in connection.executed if s.startswith("DROP DATABASE")]


def test_a_dry_run_never_reaches_a_drop_statement() -> None:
    connection = _FakeConnection([("proxyshop_w29", 1024)], liveness=[[]])
    results = _sweep(connection, apply=False)
    assert [c.verdict for c in results] == ["would-drop"]
    assert not [s for s in connection.executed if s.startswith("DROP DATABASE")]


def test_a_dry_run_still_re_reads_liveness_per_candidate() -> None:
    """The dry run has to exercise the same evidence path, or it reports a different tool."""
    connection = _FakeConnection([("proxyshop_w29", 1024)], liveness=[[]])
    _sweep(connection, apply=False)
    assert connection.liveness_reads == 1


def test_a_protected_database_is_never_even_probed() -> None:
    """Worker 0 must not reach the liveness read: it is not a candidate to begin with."""
    connection = _FakeConnection([("proxyshop_w0", 1024)], liveness=[[]])
    results = _sweep(connection)
    assert [c.verdict for c in results] == ["protected"]
    assert connection.liveness_reads == 0


def test_drop_refuses_a_name_it_cannot_recognise() -> None:
    """Belt and braces on the one place a database name is interpolated into SQL."""
    with pytest.raises(db_reclaim.ReclaimError):
        db_reclaim.drop(_FakeConnection([], liveness=[]), 'evil"; drop database "x')


def test_liveness_is_rechecked_per_candidate_rather_than_snapshotted() -> None:
    """Two reads inside the loop — before the verdict, and again before the drop.

    A single read at the top of the run is the reads-clean-when-stale shape: it is right at
    the moment it is taken and wrong by the time it is used.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    sweep_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "sweep"
    )
    loop = next(node for node in ast.walk(sweep_fn) if isinstance(node, ast.For))
    calls = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "live_backends"
    ]
    assert len(calls) >= 2, (
        "sweep() consults live_backends fewer than twice inside its loop, so either the "
        "per-candidate check or the re-check immediately before the drop is gone"
    )
    outside = [
        node
        for node in ast.walk(sweep_fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "live_backends"
    ]
    assert len(outside) == len(calls), "a liveness read escaped the per-candidate loop"
