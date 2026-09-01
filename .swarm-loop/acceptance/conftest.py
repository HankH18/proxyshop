"""Frozen-suite plugin: emit one JSON record per acceptance test.

FROZEN. Hashed by `swarmloop.py freeze`; workers may run this suite but never edit it.

Every acceptance test carries two markers:
    @pytest.mark.epic("E3")      -> which per-epic metric it counts toward
    @pytest.mark.ticket("T-032") -> which ticket is responsible for making it pass

`run.py` reads the resulting report to produce its bare numbers. Writing our own tiny
reporter (rather than depending on pytest-json-report) keeps the measuring stick free of
any dependency that could go missing mid-run and turn a real number into a fake zero.
"""
from __future__ import annotations

import json
import os

import pytest

_RESULTS: dict[str, dict] = {}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "epic(name): epic this acceptance test counts toward")
    config.addinivalue_line("markers", "ticket(id): ticket responsible for this criterion")
    config.addinivalue_line("markers", "blocker(id): SPEC S8 release blocker this guards")


def _marker_arg(item: pytest.Item, name: str, default: str) -> str:
    marker = item.get_closest_marker(name)
    if marker is None or not marker.args:
        return default
    return str(marker.args[0])


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    outcome = yield
    report = outcome.get_result()

    rec = _RESULTS.setdefault(
        item.nodeid,
        {
            "nodeid": item.nodeid,
            "name": item.name,
            "epic": _marker_arg(item, "epic", "UNMARKED"),
            "ticket": _marker_arg(item, "ticket", "UNASSIGNED"),
            "blocker": _marker_arg(item, "blocker", ""),
            "outcome": "passed",
            "phase_failed": "",
            "longrepr": "",
        },
    )

    # A test counts as passing only if EVERY phase succeeded. setup/teardown errors are
    # failures, not passes — an acceptance criterion whose fixture blew up is not met.
    if report.outcome == "failed":
        rec["outcome"] = "failed"
        rec["phase_failed"] = report.when or ""
        rec["longrepr"] = str(report.longrepr)[:600]
    elif report.outcome == "skipped" and rec["outcome"] == "passed":
        # Skipped is NOT passed. A goal you skipped is a goal you did not meet.
        rec["outcome"] = "skipped"
        rec["phase_failed"] = report.when or ""
        rec["longrepr"] = str(report.longrepr)[:600]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    path = os.environ.get("ACCEPTANCE_REPORT")
    if not path:
        return
    # Include collected-but-never-run tests (e.g. session aborted) as failures, so the
    # denominator is always the full goal set rather than only what got as far as running.
    for item in session.items:
        _RESULTS.setdefault(
            item.nodeid,
            {
                "nodeid": item.nodeid,
                "name": item.name,
                "epic": _marker_arg(item, "epic", "UNMARKED"),
                "ticket": _marker_arg(item, "ticket", "UNASSIGNED"),
                "blocker": _marker_arg(item, "blocker", ""),
                "outcome": "failed",
                "phase_failed": "not-run",
                "longrepr": "test was collected but never executed",
            },
        )
    with open(path, "w") as fh:
        json.dump(list(_RESULTS.values()), fh, indent=1)
