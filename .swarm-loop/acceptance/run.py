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

Modes:
  --total                 number of acceptance tests defined
  --count-passing         number currently passing        (optionally --epic Ex)
  --pass-rate             percent currently passing       (optionally --epic Ex)
  --json                  full per-test detail to stderr (diagnostics; no number)

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
    # Keep the frozen suite independent of the project's own pytest configuration:
    # -p no:cacheprovider avoids writing into the repo, -o addopts= drops any inherited
    # addopts that would change collection.
    cmd = [
        sys.executable, "-m", "pytest",
        str(ACCEPTANCE_DIR),
        "-p", "no:cacheprovider",
        "-o", "addopts=",
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


def select(records: list[dict], epic: str | None) -> list[dict]:
    if epic is None:
        return records
    return [r for r in records if r.get("epic") == epic]


def main() -> None:
    ap = argparse.ArgumentParser(description="ProxyShop frozen acceptance runner")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--total", action="store_true")
    mode.add_argument("--count-passing", action="store_true")
    mode.add_argument("--pass-rate", action="store_true")
    mode.add_argument("--json", action="store_true")
    ap.add_argument("--epic", choices=EPICS, default=None)
    args = ap.parse_args()

    records = run_pytest()
    subset = select(records, args.epic)

    if args.epic is not None and not subset:
        _fail(f"no acceptance tests are marked epic={args.epic} — the suite is miswired")

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
