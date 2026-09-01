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

import importlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest

# --- Import bootstrap -------------------------------------------------------------
# The frozen suite reaches product code by path (`apps.exchange.src.ranking`), and it
# must do so WITHOUT help from any file a worker can edit. `run.py` already passes
# `--confcutdir` so the project's root conftest is never loaded, and `-o addopts=` so
# its addopts cannot change collection — but pytest still reads `pythonpath` out of the
# project's own `pyproject.toml`, which is worker-owned. Relying on that would mean a
# two-line edit to an unfrozen manifest could decide whether a frozen goal can import
# at all. So the frozen suite puts the repo root on `sys.path` itself.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --- Hyphenated source trees ------------------------------------------------------
# `packages/store-agent`, `apps/seller-reference` and `services/shopify-stub` carry
# hyphens, so `packages.store-agent.src.hooks` is not a legal dotted import and no
# amount of sys.path work makes it one. The directory names cannot change — they are
# what the tickets' scope globs match on. So the frozen suite registers the three
# underscore aliases itself, here, and a test writes
# `from packages.store_agent.src.hooks import ToolHooks` — uniform with every other
# import in the suite, and independent of any packaging decision a worker can edit.
#
# Nothing below touches the filesystem and nothing below raises. At cycle 0 none of
# these directories exists; the alias is registered anyway and the test that imports
# through it fails with a clean ModuleNotFoundError INSIDE the test body. That is the
# point: an unmet goal must be a per-test failure, never a collection error.


def _make_namespace(dotted: str, directory: pathlib.Path) -> types.ModuleType:
    """Register `dotted` as a namespace package rooted at `directory`."""
    spec = importlib.machinery.ModuleSpec(dotted, None, is_package=True)
    spec.submodule_search_locations = [str(directory)]
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    return module


def _alias_hyphenated(dotted: str, directory: pathlib.Path) -> None:
    """Make `dotted` (exactly `<toplevel>.<leaf>`) resolve to `directory`."""
    parent_name, _, leaf = dotted.rpartition(".")
    assert parent_name and "." not in parent_name, "aliases are <toplevel>.<leaf> only"
    parent = sys.modules.get(parent_name)
    if parent is None:
        try:  # prefer the real PEP 420 portion: its __path__ tracks sys.path
            parent = importlib.import_module(parent_name)
        except ImportError:  # the directory does not exist yet (cycle 0)
            parent = _make_namespace(parent_name, REPO_ROOT / parent_name)
    module = sys.modules.get(dotted)
    if module is None:
        module = _make_namespace(dotted, directory)
    else:  # already registered (a test file may install the same alias): widen only
        search = getattr(module, "__path__", None)
        if search is not None and str(directory) not in list(search):
            search.append(str(directory))
    setattr(parent, leaf, module)


_alias_hyphenated("packages.store_agent", REPO_ROOT / "packages" / "store-agent")
_alias_hyphenated("apps.seller_reference", REPO_ROOT / "apps" / "seller-reference")
_alias_hyphenated("services.shopify_stub", REPO_ROOT / "services" / "shopify-stub")

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
