"""The lock's per-acquisition cost log (P5a) — shape, attribution, and harmlessness.

Nobody may decide how many Neo4j instances to run until the flock's real cost is measured,
and the figure everyone quotes ("held 88 % of the session") described the *session*-scoped
guard that T-214 replaced. So ``proxyshop_support.neo4j_lock`` now writes one JSON record
per completed acquisition. These tests pin the three things that make such a log worth
reading:

1. it records a **cost per acquisition** — a wait and a hold, not just an event;
2. a reset that ran inside a hold is attributed to *that* hold, with what it deleted;
3. it can never fail, slow, or lengthen an acquisition — instrumentation that can break a
   run is worse than none, and this one sits inside a machine-global lock every graph test
   in the repo takes.

No Docker: every test here drives the lock directly with its own ``tmp_path`` lock file and
a stub driver, so the contract holds in a stack-down run too.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest

from proxyshop_support import neo4j_lock


class _StubCounters:
    def __init__(self, nodes: int, relationships: int) -> None:
        self.nodes_deleted = nodes
        self.relationships_deleted = relationships


class _StubSummary:
    def __init__(self, nodes: int, relationships: int) -> None:
        self.counters = _StubCounters(nodes, relationships)


class _StubSession:
    """Just enough ``neo4j.Session`` for :func:`neo4j_lock.reset_graph`."""

    def __init__(self, nodes: int, relationships: int, delay: float = 0.0) -> None:
        self._nodes = nodes
        self._relationships = relationships
        self._delay = delay
        self.statements: list[str] = []

    def __enter__(self) -> _StubSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def run(self, statement: str, **_: Any) -> _StubSession:
        """``session.run(...)`` returns a result; here the session doubles as its own."""
        self.statements.append(statement)
        if self._delay:
            time.sleep(self._delay)
        return self

    def consume(self) -> _StubSummary:
        return _StubSummary(self._nodes, self._relationships)


class _StubDriver:
    def __init__(self, nodes: int = 0, relationships: int = 0, delay: float = 0.0) -> None:
        self._session = _StubSession(nodes, relationships, delay)

    def session(self) -> _StubSession:
        return self._session


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    """A private lock file, so nothing here contends with a real graph lane."""
    return tmp_path / "neo4j.lock"


def _records(lock_file: Path) -> list[dict[str, Any]]:
    return neo4j_lock.read_lock_log(neo4j_lock.lock_log_path(lock_file))


# --------------------------------------------------------------------------------------
# where the log goes
# --------------------------------------------------------------------------------------


def test_the_log_sits_beside_the_lock_file_by_default(lock_file: Path) -> None:
    """``<lock>.log``, resolved the same way the lock path itself is resolved."""
    assert neo4j_lock.lock_log_path(lock_file) == Path(f"{lock_file.resolve()}.log")


def test_the_log_location_is_overridable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worker measuring one run in isolation must not have to move the lock to do it."""
    elsewhere = tmp_path / "somewhere" / "acquisitions.jsonl"
    monkeypatch.setenv(neo4j_lock.LOG_ENV_VAR, str(elsewhere))
    assert neo4j_lock.lock_log_path() == elsewhere


@pytest.mark.parametrize("value", sorted(neo4j_lock.LOG_DISABLED_VALUES))
def test_the_log_can_be_turned_off(
    value: str, monkeypatch: pytest.MonkeyPatch, lock_file: Path
) -> None:
    """Off means *no file*, not an empty one: a stray file is itself a claim."""
    monkeypatch.setenv(neo4j_lock.LOG_ENV_VAR, value)
    assert neo4j_lock.lock_log_path(lock_file) is None
    with neo4j_lock.neo4j_flock(path=lock_file):
        pass
    assert not Path(f"{lock_file.resolve()}.log").exists()


# --------------------------------------------------------------------------------------
# what one record says
# --------------------------------------------------------------------------------------


def test_one_completed_acquisition_writes_one_record(
    monkeypatch: pytest.MonkeyPatch, lock_file: Path
) -> None:
    monkeypatch.setenv("PROXYSHOP_WORKER", "13")
    with neo4j_lock.neo4j_flock(path=lock_file):
        pass
    written = _records(lock_file)
    assert len(written) == 1
    record = written[0]
    assert record["pid"] == os.getpid()
    assert record["worker"] == "13"
    assert record["lock"] == str(lock_file.resolve())
    for numeric in ("waited_s", "held_s", "reset_ms"):
        assert isinstance(record[numeric], (int, float)), f"{numeric} is not a number"


def test_the_record_measures_the_hold_not_merely_that_one_happened(lock_file: Path) -> None:
    """``held_s`` is the number a sibling worker pays. It has to be a real duration."""
    with neo4j_lock.neo4j_flock(path=lock_file):
        time.sleep(0.05)
    held = _records(lock_file)[0]["held_s"]
    assert held >= 0.05, f"held_s={held} did not cover a 50 ms hold"
    assert held < 5.0, f"held_s={held} is implausibly large for a 50 ms hold"


def test_an_uncontended_acquisition_is_recorded_as_uncontended(lock_file: Path) -> None:
    """Distinguishes "waited 0 s because nobody was there" from "the holder let go fast"."""
    with neo4j_lock.neo4j_flock(path=lock_file):
        pass
    record = _records(lock_file)[0]
    assert record["contended"] is False
    assert record["waited_s"] < 0.5


def test_records_from_several_acquisitions_accumulate(lock_file: Path) -> None:
    """The log is append-only: a second run must not truncate the first one's evidence."""
    for _ in range(3):
        with neo4j_lock.neo4j_flock(path=lock_file):
            pass
    assert len(_records(lock_file)) == 3


def test_a_nested_acquisition_is_folded_into_the_outer_record(lock_file: Path) -> None:
    """Re-entry takes no kernel lock, so it must not be counted as a second hold."""
    with neo4j_lock.neo4j_flock(path=lock_file):
        with neo4j_lock.neo4j_flock(path=lock_file):
            pass
    written = _records(lock_file)
    assert len(written) == 1, "a nested acquisition was logged as a separate hold"
    assert written[0]["reentries"] == 1
    assert written[0]["max_depth"] == 2


# --------------------------------------------------------------------------------------
# the reset, attributed to the hold it ran inside
# --------------------------------------------------------------------------------------


def test_reset_graph_reports_what_it_deleted(lock_file: Path) -> None:
    """The return value is what makes a redundant wipe provable rather than arguable."""
    driver = _StubDriver(nodes=7, relationships=4)
    with neo4j_lock.neo4j_flock(path=lock_file):
        outcome = neo4j_lock.reset_graph(driver)
    assert outcome.nodes_deleted == 7
    assert outcome.relationships_deleted == 4
    assert outcome.reset_ms >= 0.0
    assert driver.session().statements == ["MATCH (n) DETACH DELETE n"]


def test_a_corpus_scale_wipe_says_so_and_a_fixture_scale_one_stays_quiet(
    lock_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Both directions of the warning, because a warning on every reset is no warning.

    The reset is correct and stays: a lane needs a clean graph, and D37 records that Neo4j
    isolation here is scheduler serialisation plus an flock rather than tenant scoping — ONE
    database, shared with any demo stack on the same host. `PROXYSHOP_WORKER` gives a lane its
    own Postgres and Redis and NOT its own graph, so a lane looks isolated while a routine
    `pytest proxyshop_support` takes a loaded corpus with it, the suite passes green, and the
    loss surfaces later when somebody drives the demo. It happened twice in one session.

    So the wipe now says what it took. The quiet direction is the half that makes it useful:
    thousands of resets delete nothing or delete a fixture, and a line printed on all of them
    is a line nobody reads by the time it matters.
    """
    with caplog.at_level(logging.WARNING, logger=neo4j_lock.__name__):
        with neo4j_lock.neo4j_flock(path=lock_file):
            neo4j_lock.reset_graph(
                _StubDriver(nodes=neo4j_lock.CORPUS_SCALE_NODES, relationships=90_000),
                path=lock_file,
            )
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1, [r.getMessage() for r in loud]
    message = loud[0].getMessage()
    assert str(neo4j_lock.CORPUS_SCALE_NODES) in message
    assert "make demo-corpus" in message, (
        "a warning that says something was destroyed and not how to get it back is a warning "
        "that costs the reader a search"
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=neo4j_lock.__name__):
        with neo4j_lock.neo4j_flock(path=lock_file):
            neo4j_lock.reset_graph(
                _StubDriver(nodes=neo4j_lock.CORPUS_SCALE_NODES - 1, relationships=3),
                path=lock_file,
            )
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_reset_inside_a_hold_is_attributed_to_that_hold(lock_file: Path) -> None:
    driver = _StubDriver(nodes=12, relationships=11, delay=0.01)
    with neo4j_lock.neo4j_flock(path=lock_file):
        neo4j_lock.reset_graph(driver, path=lock_file)
    record = _records(lock_file)[0]
    assert record["resets"] == 1
    assert record["nodes_deleted"] == 12
    assert record["relationships_deleted"] == 11
    assert record["reset_ms"] >= 10.0, f"reset_ms={record['reset_ms']} missed a 10 ms wipe"
    assert record["reset_ms"] <= record["held_s"] * 1000 + 1.0


def test_a_reset_that_deleted_nothing_is_recorded_as_zero(lock_file: Path) -> None:
    """Zero is the load-bearing value: it is what proves a second wipe buys nothing."""
    with neo4j_lock.neo4j_flock(path=lock_file):
        outcome = neo4j_lock.reset_graph(_StubDriver(nodes=0, relationships=0), path=lock_file)
    assert outcome.nodes_deleted == 0
    assert _records(lock_file)[0]["nodes_deleted"] == 0


def test_a_reset_outside_any_hold_still_runs_and_is_attributed_to_nothing(
    lock_file: Path,
) -> None:
    """Wrong attribution is worse than none, so an unheld reset writes no record."""
    outcome = neo4j_lock.reset_graph(_StubDriver(nodes=3))
    assert outcome.nodes_deleted == 3
    assert _records(lock_file) == []


# --------------------------------------------------------------------------------------
# it cannot break anything
# --------------------------------------------------------------------------------------


def test_an_unwritable_log_does_not_fail_the_acquisition(
    monkeypatch: pytest.MonkeyPatch, lock_file: Path, tmp_path: Path
) -> None:
    """A full or read-only disk must cost a measurement, never a test run."""
    blocked = tmp_path / "not-a-directory" / "log.jsonl"
    blocked.parent.write_text("this is a file, so mkdir of it will fail")
    monkeypatch.setenv(neo4j_lock.LOG_ENV_VAR, str(blocked))
    with neo4j_lock.neo4j_flock(path=lock_file):
        pass  # must simply not raise
    assert neo4j_lock.held_depth(lock_file) == 0


def test_the_record_is_written_after_the_flock_is_released(
    monkeypatch: pytest.MonkeyPatch, lock_file: Path
) -> None:
    """Ordering matters: logging inside the hold would charge a waiter for the log.

    Checked by observing, from inside the append, that this process no longer holds the
    lock — which is the property the docstring promises and the only one that keeps the
    measurement from perturbing what it measures.
    """
    seen: list[int] = []
    original = neo4j_lock._append_record

    def spy(path: Path, stats: neo4j_lock.Acquisition) -> None:
        seen.append(neo4j_lock.held_depth(path))
        original(path, stats)

    monkeypatch.setattr(neo4j_lock, "_append_record", spy)
    with neo4j_lock.neo4j_flock(path=lock_file):
        pass
    assert seen == [0], f"the record was written while the lock was still held: {seen}"


def test_an_oversized_log_is_rotated_rather_than_growing_without_bound(
    monkeypatch: pytest.MonkeyPatch, lock_file: Path
) -> None:
    """The default log is machine-global and appended to forever. It has to have an end."""
    monkeypatch.setattr(neo4j_lock, "MAX_LOG_BYTES", 200)
    log_path = neo4j_lock.lock_log_path(lock_file)
    assert log_path is not None
    log_path.write_text("x" * 500, encoding="utf-8")
    with neo4j_lock.neo4j_flock(path=lock_file):
        pass
    assert Path(f"{log_path}.1").read_text(encoding="utf-8") == "x" * 500
    assert len(neo4j_lock.read_lock_log(log_path)) == 1


def test_a_half_written_final_line_is_skipped_rather_than_raising(lock_file: Path) -> None:
    """Live processes append while a reader reads; a torn tail is normal, not an error."""
    with neo4j_lock.neo4j_flock(path=lock_file):
        pass
    log_path = neo4j_lock.lock_log_path(lock_file)
    assert log_path is not None
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "truncated mid-')
    assert len(neo4j_lock.read_lock_log(log_path)) == 1


# --------------------------------------------------------------------------------------
# the summary — the number a fan-out decision is actually made on
# --------------------------------------------------------------------------------------


def test_the_summary_reports_a_per_acquisition_cost(lock_file: Path) -> None:
    """An event stream nobody can reduce is not a measurement."""
    for _ in range(4):
        with neo4j_lock.neo4j_flock(path=lock_file):
            neo4j_lock.reset_graph(_StubDriver(nodes=1), path=lock_file)
            time.sleep(0.01)
    summary = neo4j_lock.summarize_lock_log(_records(lock_file))
    assert summary["acquisitions"] == 4
    assert summary["resets"] == 4
    assert summary["held_p50_s"] >= 0.01
    assert summary["held_p95_s"] >= summary["held_p50_s"]
    assert summary["held_total_s"] >= 0.04
    assert summary["nodes_deleted_total"] == 4
    assert summary["contended"] == 0


def test_the_summary_counts_resets_that_reclaimed_nothing(lock_file: Path) -> None:
    """The statistic that turns "the wipe is redundant" into a number."""
    for nodes in (0, 0, 5):
        with neo4j_lock.neo4j_flock(path=lock_file):
            neo4j_lock.reset_graph(_StubDriver(nodes=nodes), path=lock_file)
    summary = neo4j_lock.summarize_lock_log(_records(lock_file))
    assert summary["resets"] == 3
    assert summary["resets_that_deleted_nothing"] == 2


def test_the_summary_of_an_empty_log_is_zeroes_not_an_exception() -> None:
    """A log that does not exist yet is the ordinary state before the first graph test."""
    assert neo4j_lock.summarize_lock_log([])["acquisitions"] == 0


def test_the_cli_prints_the_numbers(lock_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``python -m proxyshop_support.neo4j_lock`` — so the cost is not a bespoke one-liner."""
    with neo4j_lock.neo4j_flock(path=lock_file):
        neo4j_lock.reset_graph(_StubDriver(nodes=2), path=lock_file)
    log_path = neo4j_lock.lock_log_path(lock_file)
    assert log_path is not None
    assert neo4j_lock._main([str(log_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["acquisitions"] == 1
    assert payload["nodes_deleted_total"] == 2
