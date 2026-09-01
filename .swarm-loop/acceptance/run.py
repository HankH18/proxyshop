#!/usr/bin/env python3
"""Frozen acceptance-suite runner for the ProxyShop swarm-loop run.

This file and every test beside it are FROZEN GOALS. Workers may run them; nobody
may edit them. `swarmloop.py freeze` hashes this whole directory.

Contract required by references/goal-setting.md:
  * the LAST stdout line is a bare number
  * "the suite ran and everything failed"  -> print 0        (exit 0)
  * "the suite could not run at all"       -> print nothing  (exit non-zero)

The second case matters: a wrapper that turns a broken runner into a quiet 0 poisons
the regression with fake data. So we positively confirm pytest executed and produced a
report before emitting any number.

Modes (each runs the suite once; combine with the filters below):
  --total                 number of acceptance tests defined
  --count-passing         number currently passing
  --pass-rate             percent currently passing
  --json                  full per-test detail to stderr (diagnostics; no number)

Filters (optional, combinable — an empty selection is a loud failure, never a 0):
  --epic Ex               only tests marked @pytest.mark.epic("Ex")
  --blocker S8-n          only tests marked @pytest.mark.blocker("S8-n")

Deliberately ABSENT: any mode that reads a previously-written report instead of
running the suite. It was built and then removed before the freeze. Reusing a report
across metrics would save one suite run per metric, but the report has to live
somewhere on disk, every worker has unrestricted shell, and `check-branch` inspects
committed diffs — so it cannot see a report written straight into the primary tree.
Binding the report to a hash of this directory does not close it either: workers are
allowed to READ these files, so any hash they must match is a hash they can compute.
A metric that reads a file a worker can write is not a frozen metric
(references/goal-setting.md), so every number here is paid for with a real pytest run.

Design rule for the tests themselves (see README.md in this directory): every test
imports the code under test INSIDE the test function, never at module scope. At the
cycle-0 baseline none of the product code exists, and a module-scope import would turn
the whole file into a collection error — which reads as "the runner is broken" instead
of the truth, which is "these goals are not met yet".
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ACCEPTANCE_DIR = Path(__file__).resolve().parent
REPO_ROOT = ACCEPTANCE_DIR.parent.parent

EPICS = ["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "SPEC"]


def _fail(msg: str) -> "NoReturn":  # noqa: F821
    """The suite could not run. Print NOTHING to stdout; explain on stderr; exit 1."""
    print(f"acceptance-runner: {msg}", file=sys.stderr)
    sys.exit(1)


def run_pytest() -> list[dict]:
    """Execute the acceptance suite and return one record per test.

    Uses a JSON report written by a tiny inline conftest plugin rather than a third-party
    reporter, so the runner has no dependency that could itself go missing.
    """
    out_fd, out_path = tempfile.mkstemp(prefix="acceptance-", suffix=".json")
    os.close(out_fd)
    env = dict(os.environ)
    env["ACCEPTANCE_REPORT"] = out_path
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Any installed distribution advertising a [pytest11] entry point autoloads into this
    # run. Measured: a ten-line "worker-helper" plugin cut --total from 8 to 5 and an epic
    # count from 3 to 1, at exit 0, while --pass-rate still printed 100.00. Neither
    # -o addopts=, nor -p no:cacheprovider, nor --confcutdir stopped it. A worker reaches
    # this with two lines in a member pyproject.toml it legitimately owns, and check-branch
    # shows only an advisory '?' whose text is about ignore/omit/exclude, not entry points.
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # Keep the frozen suite independent of the project's own pytest configuration.
    # -o addopts= alone is NOT enough: it neutralizes one ini key, and the project's
    # pyproject.toml is worker-owned. Each -o below closes a measured, count-preserving
    # hole — the count companion never fires on any of them, so none is belt-and-braces:
    #   pythonpath  — prepending a `_stub` dir shadows the real tree with a regular
    #                 package, which terminates the namespace-package search; the epic
    #                 then goes green against hand-written stubs with --total unchanged.
    #   python_files / python_functions / python_classes — narrow the discovery patterns
    #                 and the suite silently shrinks (this one the count DOES catch, but
    #                 pinning it costs nothing and fails louder).
    cmd = [
        sys.executable, "-m", "pytest",
        str(ACCEPTANCE_DIR),
        "-p", "no:cacheprovider",
        "-o", "addopts=",
        "-o", "pythonpath=",
        "-o", "python_files=test_*.py",
        "-o", "python_functions=test_*",
        "-o", "python_classes=Test*",
        # Hermetic: never load the project's own root conftest.py into the frozen
        # suite. The product code is importable because T-000 installs every package
        # editable into the venv — the acceptance suite needs no sys.path help, and
        # must not inherit fixtures a worker could change.
        f"--confcutdir={ACCEPTANCE_DIR}",
        "-q", "--tb=no", "--no-header",
        "-rN",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), env=env,
            capture_output=True, text=True, timeout=1800,
        )
    except FileNotFoundError:
        _fail("python -m pytest is not available")
    except subprocess.TimeoutExpired:
        _fail("acceptance suite exceeded its 1800s timeout")

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        _fail(
            "pytest produced no report — the suite did not run.\n"
            f"  exit={proc.returncode}\n"
            f"  stdout tail: {proc.stdout[-2000:]}\n"
            f"  stderr tail: {proc.stderr[-2000:]}"
        )
    try:
        with open(out_path) as fh:
            records = json.load(fh)
    except json.JSONDecodeError as exc:
        _fail(f"acceptance report was not valid JSON: {exc}")
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    if not isinstance(records, list) or not records:
        _fail("acceptance report contained no tests — collection found nothing")
    return records


def select(records: list[dict], epic: str | None, blocker: str | None = None) -> list[dict]:
    out = records
    if epic is not None:
        out = [r for r in out if r.get("epic") == epic]
    if blocker is not None:
        out = [r for r in out if r.get("blocker") == blocker]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="ProxyShop frozen acceptance runner")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--total", action="store_true")
    mode.add_argument("--count-passing", action="store_true")
    mode.add_argument("--pass-rate", action="store_true")
    mode.add_argument("--json", action="store_true")
    ap.add_argument("--epic", choices=EPICS, default=None)
    ap.add_argument("--blocker", default=None,
                    help="only tests marked @pytest.mark.blocker(\"S8-n\")")
    args = ap.parse_args()

    # An empty --blocker would match every UNMARKED test (conftest defaults the
    # field to ""), silently turning a filtered metric into a whole-suite one.
    if args.blocker is not None and not args.blocker.strip():
        _fail("--blocker requires a non-empty id")

    records = run_pytest()
    subset = select(records, args.epic, args.blocker)

    # Each filter is checked on its own BEFORE the combination, so a miswired
    # marker is named exactly rather than hidden behind an empty intersection.
    if args.epic is not None and not select(records, args.epic):
        _fail(f"no acceptance tests are marked epic={args.epic} — the suite is miswired")
    if args.blocker is not None and not select(records, None, args.blocker):
        _fail(f"no acceptance tests are marked blocker={args.blocker} — the suite is miswired")
    if not subset:
        _fail(
            f"no acceptance tests match epic={args.epic} and blocker={args.blocker} "
            "together — the metric selects an empty set"
        )

    total = len(subset)
    passing = sum(1 for r in subset if r.get("outcome") == "passed")

    if args.json:
        json.dump(records, sys.stderr, indent=1)
        sys.stderr.write("\n")
        return

    if args.total:
        print(total)
    elif args.count_passing:
        print(passing)
    elif args.pass_rate:
        # total is guaranteed > 0 above, so this is safe
        print(f"{100.0 * passing / total:.2f}")


if __name__ == "__main__":
    main()
