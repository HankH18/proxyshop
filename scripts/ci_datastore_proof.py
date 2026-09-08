#!/usr/bin/env python
"""Prove the datastore-backed tests **executed**, and that CI's stack still matches compose's.

This is the CI half of the rule ``scripts/verify.sh`` states for the local half: *a green
gate must name what it did not run*, and *a run in which a whole class of checks never
executed must not exit 0*.

WHY IT IS NOT A DUPLICATE OF ``verify.sh``'s ``datastore_coverage_gate``
-----------------------------------------------------------------------
That gate opens a TCP connection to postgres, neo4j-bolt and redis and refuses when one of
them does not answer. Reachability is *necessary* and it is not *sufficient*: three ports
answering says nothing about whether the ``docker``-marked tests were selected, collected,
or executed. Every one of these leaves that gate green while the class evaporates:

* the marker is stripped off a file, so the tests are no longer in the class at all;
* a fixture starts skipping for a reason that is not reachability (a missing role, a
  migration that did not apply, an image whose default configuration differs);
* a marker expression somewhere deselects them.

So this script asserts the *outcome* instead of the *precondition*: it runs the class and
requires that every member PASSED — zero skipped, zero failed, zero errored, and at least
``--min`` of them collected.

THE MEASUREMENT THIS EXISTS FOR (taken on this tree, worker 10, 2026-09-07)
---------------------------------------------------------------------------
With the compose stack up::

    pytest apps/trust/tests/test_schema_grants.py -q     ->  58 passed              exit 0
    pytest -q -m "docker and not needs_model"            ->  309 passed, 0 skipped  exit 0

With ``PROXYSHOP_PG_DSN_ADMIN`` pointed at a closed port and nothing else changed::

    pytest apps/trust/tests/test_schema_grants.py -q     ->  22 passed, 36 skipped  exit 0

Thirty-six live-Postgres least-privilege checks for C3/S7 did not run, and the shell saw
success. ``22 passed`` is indistinguishable from a full pass to any reader, human or
machine, unless something refuses. This refuses.

WHY ``docker and not needs_model``
----------------------------------
``needs_model`` wants downloaded embedding weights, which no offline CI job has; ``make
verify`` deselects it for the same reason. Every other ``docker``-marked test is expected to
run and pass on a job that has the datastores. Measured: ``-m docker`` collects 309 of 7663,
and ``-m "docker and not needs_model"`` collects the same 309 minus none — the needs_model
test is not docker-marked today, and the conjunction is written for the day it is.

``check-config``
----------------
The GitLab job declares the three datastores as CI *services* rather than through
``docker-compose.yml`` (a GitLab docker-executor job cannot reach a compose stack on its own
``localhost``, and DinD would need a privileged runner). That buys a runner requirement of
"ordinary" and costs one copy of the image tags and the two dev credentials. ``check-config``
turns that copy from a silent drift hazard into a checked one: it fails when a CI file and
``docker-compose.yml`` stop agreeing.

It also pins two things measured the hard way while this pipeline was being built:

* **The worker index must be 1..15.** 0 is reserved for the frozen ``build_succeeds``
  measurement (``scripts/db_reclaim.py`` protects ``proxyshop_w0`` under every flag), and
  ``redis_db_index`` REFUSES an index at or above the running server's logical-DB ceiling.
  A stock ``redis:7-alpine`` reports ``databases 16`` — measured, this host — so an index of
  16 or more turns every Redis test into an error the moment someone drops the ``--databases
  64`` override out of a service definition.
* **A CI file must not set the four role DSN variables.** The ORIGINAL reason is fixed and
  this rule is not. What was measured: ``PROXYSHOP_PG_DSN_APP``, set to the password-free
  shape ``.env.example`` ships, broke two buyer-service tests outright, because
  ``buyer_svc.auth.routes`` reads that variable directly rather than through
  ``proxyshop_support.postgres.role_dsn`` and so never got the password —
  ``fe_sendauth: no password supplied``. Every direct reader now resolves the credential
  through ``proxyshop_support.postgres.with_role_password``, graded by the direct-reader
  section of ``proxyshop_support/tests/test_role_password_end_to_end.py``, so that particular
  failure is gone. What remains is the DATABASE: unlike ``role_dsn``, a direct reader keeps
  whatever database the variable names, so a CI file that sets one pins those services to it
  while the rest of the job runs against ``proxyshop_w$PROXYSHOP_WORKER`` — two halves of one
  test against two databases, which surfaces as flakiness rather than as a configuration
  error. Only ``PROXYSHOP_PG_DSN_ADMIN`` may be set, and only because
  ``proxyshop_support.reachability`` reads host and port from it and from nowhere else — it
  does not consult ``PGHOST``/``PG_PORT``, so without it the coverage gate probes
  ``localhost:5432`` on a job whose Postgres is at ``postgres:5432``.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

#: The marker expression whose members must all have run.
MARKER = "docker and not needs_model"

#: Floor on the size of that class. Measured 309 on 2026-09-07; the floor sits below it so
#: adding tests never breaks CI, while deleting or unmarking a meaningful slice does.
DEFAULT_MIN = 300

#: CI files this repository ships, and the job whose environment `check-config` reads.
GITLAB_CI = ".gitlab-ci.yml"
GITHUB_CI = ".github/workflows/verify.yml"

#: Set by a CI file at your peril — see the module docstring.
FORBIDDEN_DSN_VARS = (
    "PROXYSHOP_PG_DSN_EXCHANGE",
    "PROXYSHOP_PG_DSN_TRUST_RW",
    "PROXYSHOP_PG_DSN_VAULT",
    "PROXYSHOP_PG_DSN_APP",
)

#: Redis's own default logical-DB count, which is the ceiling a stock image enforces.
REDIS_DEFAULT_DB_COUNT = 16


def _fail(*lines: str) -> int:
    print("", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    for line in lines:
        print(line, file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    return 1


# ------------------------------------------------------------------------------------
# wait
# ------------------------------------------------------------------------------------


def cmd_wait(args: argparse.Namespace) -> int:
    """Block until all three datastores answer, or refuse after ``--timeout`` seconds.

    Uses ``proxyshop_support.reachability`` — the same module the root ``conftest.py`` skips
    on and the same one ``verify.sh``'s coverage gate refuses on — so a job that gets past
    this cannot then discover a different opinion about where its datastores are.
    """
    sys.path.insert(0, str(ROOT))
    from proxyshop_support import reachability

    deadline = time.monotonic() + args.timeout
    down = reachability.unreachable(timeout=2.0)
    while down and time.monotonic() < deadline:
        time.sleep(2.0)
        down = reachability.unreachable(timeout=2.0)
    if down:
        return _fail(
            f"DATASTORES DID NOT COME UP within {args.timeout}s.",
            "  unreachable: " + ", ".join(str(e) for e in down),
            "",
            "  These addresses are read from PROXYSHOP_PG_DSN_ADMIN, NEO4J_URI and",
            "  REDIS_URL (proxyshop_support/reachability.py:61). A job whose services are",
            "  healthy but whose variables name somewhere else looks exactly like a job",
            "  with no services at all, so this refuses rather than proceeding into a run",
            "  that would report several hundred skips nobody reads.",
        )
    for endpoint in reachability.compose_endpoints():
        print(f"    up: {endpoint}")
    print("OK: all three datastores answered.")
    return 0


# ------------------------------------------------------------------------------------
# prove
# ------------------------------------------------------------------------------------


def _junit_totals(path: Path) -> dict[str, int]:
    """Read counts out of a JUnit XML report.

    pytest writes one ``<testsuite>`` inside a ``<testsuites>`` root. Both shapes are
    accepted because the wrapper has come and gone across pytest versions, and a parse that
    silently returned zeros would be the exact failure this file exists to prevent.
    """
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ValueError(f"no <testsuite> element in {path}")
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, "0"))
    return totals


def cmd_prove(args: argparse.Namespace) -> int:
    worker = os.environ.get("PROXYSHOP_WORKER", "").strip()
    if not worker:
        return _fail(
            "PROXYSHOP_WORKER is unset (D38).",
            "  Every ProxyShop run is per-worker isolated: the Postgres database name, the",
            "  Redis logical DB index and the Redis key prefix all derive from it.",
        )

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "docker-marked.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            "-m",
            args.marker,
            f"--junitxml={report}",
        ]
        print(f"==> {shlex.join(command)}")
        completed = subprocess.run(command, cwd=ROOT, check=False)
        rc = completed.returncode

        if rc == 5:
            return _fail(
                f"pytest COLLECTED NOTHING for -m {args.marker!r} (exit 5).",
                "  The datastore-backed class is empty. An empty class is not a passing",
                "  class: either the markers were stripped or the tests were deleted, and",
                "  both are the finding.",
            )
        if not report.exists():
            return _fail(
                f"pytest wrote no JUnit report to {report} (exit {rc}).",
                "  Coverage of the datastore-backed class is therefore UNKNOWN, and unknown",
                "  is not allowed to exit 0 here.",
            )
        try:
            totals = _junit_totals(report)
        except (ET.ParseError, ValueError) as exc:
            return _fail(
                f"the JUnit report was unreadable: {exc}",
                "  Treat this run's datastore coverage as unknown; it does not pass.",
            )

    ran = totals["tests"] - totals["skipped"]
    print("")
    print("DATASTORE PROOF")
    print(f"    marker expression : -m {args.marker!r}")
    print(f"    collected         : {totals['tests']}")
    print(f"    executed          : {ran}")
    print(f"    skipped           : {totals['skipped']}")
    print(f"    failed / errored  : {totals['failures']} / {totals['errors']}")
    print(f"    pytest exit       : {rc}")

    problems: list[str] = []
    if totals["skipped"]:
        problems.append(
            f"{totals['skipped']} datastore-backed tests SKIPPED. On a job that has the "
            f"datastores, a skip here means the class did not execute — which is the shape "
            f"of a green build that proves nothing about database least-privilege (C3/S7), "
            f"the graph, or the ledger."
        )
    if totals["failures"] or totals["errors"]:
        problems.append(
            f"{totals['failures']} failed and {totals['errors']} errored. Their output is above."
        )
    if totals["tests"] < args.min:
        problems.append(
            f"only {totals['tests']} tests carry the marker, below the floor of {args.min}. "
            f"Measured 309 on 2026-09-07; a drop this large means markers were removed or "
            f"tests deleted, not that the suite got faster."
        )
    if rc != 0 and not problems:
        problems.append(f"pytest exited {rc} with nothing in the report to explain it.")

    if problems:
        return _fail(
            "DATASTORE PROOF FAILED — the datastore-backed checks did not all execute.",
            *(f"  * {p}" for p in problems),
            "",
            "  This is the class scripts/verify.sh's coverage gate can only prove is",
            "  REACHABLE. Reachability is not execution; this step is the difference.",
        )
    print(f"OK: all {ran} datastore-backed tests executed and passed.")
    return 0


# ------------------------------------------------------------------------------------
# check-config
# ------------------------------------------------------------------------------------


def _load(path: Path) -> Any:
    with path.open() as handle:
        return yaml.safe_load(handle)


def _gitlab_service_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{alias: service}`` for every service the verify job declares."""
    job = config.get("verify") or {}
    services: dict[str, dict[str, Any]] = {}
    for entry in job.get("services") or []:
        if isinstance(entry, str):
            services[entry.split(":")[0]] = {"name": entry}
        elif isinstance(entry, dict):
            alias = entry.get("alias") or str(entry.get("name", "")).split(":")[0]
            services[alias] = entry
    return services


def _gitlab_declared_variables(config: dict[str, Any]) -> dict[str, str]:
    """The job's ``variables:`` — the ones GitLab also injects into SERVICE containers."""
    declared: dict[str, str] = {}
    for source in (config.get("variables"), (config.get("verify") or {}).get("variables")):
        for key, value in (source or {}).items():
            declared[str(key)] = str(value)
    return declared


def _gitlab_exported_variables(config: dict[str, Any]) -> dict[str, str]:
    """Names the job exports in its shell — visible to the tests, not to the services.

    Read from the parsed YAML rather than from the file's text, so a name that appears only
    in a comment explaining why it must NOT be set does not read as it being set.
    """
    exported: dict[str, str] = {}
    job = config.get("verify") or {}
    for key in ("before_script", "script", "after_script"):
        for line in job.get(key) or []:
            for token in str(line).split():
                if token.startswith("export"):
                    continue
                name, sep, value = token.partition("=")
                if sep and name.isidentifier():
                    exported[name] = value.strip("\"'")
    return exported


def _compose_images(compose: dict[str, Any]) -> dict[str, str]:
    return {
        name: str(spec.get("image", ""))
        for name, spec in (compose.get("services") or {}).items()
        if name in {"postgres", "redis", "neo4j"}
    }


def _github_env_by_job(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    """``{job name: the environment THAT JOB's steps actually see}``.

    PER JOB, and the flattening this replaces was a real hole rather than a tidier
    spelling. The old version merged the workflow env, every job's env and every step's env
    into ONE dict, last write winning. With a single job that was harmless. The moment a
    second job was added, its env silently overwrote the first's for every shared name —
    and MEASURED, with `PROXYSHOP_WORKER: "0"` in the `verify` job and `"12"` in the job
    below it, the merged value was `12` and `check-config` reported OK. Worker 0 is the one
    value this check exists to refuse: it is reserved for the frozen `build_succeeds`
    measurement and protected by `scripts/db_reclaim.py` under every flag. A second job
    must not be able to vouch for the first.

    Within a job the layering is still correct and still last-wins, because that is
    GitHub's own precedence: workflow env, then the job's, then the step's.
    """
    workflow: dict[str, str] = {
        str(key): str(value) for key, value in (config.get("env") or {}).items()
    }
    by_job: dict[str, dict[str, str]] = {}
    for name, job in (config.get("jobs") or {}).items():
        env = dict(workflow)
        env.update({str(key): str(value) for key, value in (job.get("env") or {}).items()})
        for step in job.get("steps") or []:
            env.update({str(key): str(value) for key, value in (step.get("env") or {}).items()})
        by_job[str(name)] = env
    return by_job or {"<no jobs>": workflow}


def _check_worker_index(label: str, env: dict[str, str], failures: list[str]) -> None:
    raw = env.get("PROXYSHOP_WORKER")
    if raw is None:
        failures.append(
            f"{label}: PROXYSHOP_WORKER is not set. scripts/verify.sh exits 2 immediately "
            f"without it (D38) — the job would fail before running a single check."
        )
        return
    try:
        index = int(raw)
    except ValueError:
        failures.append(f"{label}: PROXYSHOP_WORKER={raw!r} is not an integer.")
        return
    if index == 0:
        failures.append(
            f"{label}: PROXYSHOP_WORKER=0 is RESERVED for the frozen build_succeeds "
            f"measurement. scripts/db_reclaim.py protects proxyshop_w0 under every flag; a "
            f"CI job writing into it corrupts the number this project is graded on."
        )
    if index >= REDIS_DEFAULT_DB_COUNT:
        failures.append(
            f"{label}: PROXYSHOP_WORKER={index} is at or above Redis's default ceiling of "
            f"{REDIS_DEFAULT_DB_COUNT} logical DBs. proxyshop_support.worker.redis_db_index "
            f"REFUSES such an index rather than silently sharing one, so every Redis test "
            f"would error the moment a stock redis image is used without --databases."
        )


def _check_forbidden_dsns(label: str, env: dict[str, str], failures: list[str]) -> None:
    for name in FORBIDDEN_DSN_VARS:
        if name in env:
            failures.append(
                f"{label}: {name} must not be set. Measured on this tree: setting it to the "
                f"password-free shape .env.example ships fails "
                f"apps/buyer/svc/tests/test_auth_vault.py with 'fe_sendauth: no password "
                f"supplied', because buyer_svc reads the variable directly instead of "
                f"through proxyshop_support.postgres.role_dsn. Leave it unset and role_dsn "
                f"builds the DSN from PGHOST/PG_PORT with the right password."
            )


def cmd_check_config(args: argparse.Namespace) -> int:
    del args
    failures: list[str] = []
    compose_path = ROOT / "docker-compose.yml"
    compose = _load(compose_path)
    images = _compose_images(compose)

    gitlab_path = ROOT / GITLAB_CI
    if not gitlab_path.exists():
        failures.append(f"{GITLAB_CI} is missing — the submission target has no pipeline.")
    else:
        gitlab = _load(gitlab_path)
        declared = _gitlab_declared_variables(gitlab)
        exported = _gitlab_exported_variables(gitlab)
        _check_worker_index(GITLAB_CI, declared, failures)
        _check_forbidden_dsns(GITLAB_CI, {**declared, **exported}, failures)

        # MEASURED, and fatal: GitLab injects a job's `variables:` into its SERVICE
        # containers, and the Neo4j image turns every NEO4J_* variable into a neo4j.conf
        # setting. `docker run -e NEO4J_URI=bolt://neo4j:7687 neo4j:5.26-community` exits 1
        # with "Failed to read config: Unrecognized setting. No declared setting with name:
        # URI." A job declaring one would lose its graph service while every other service
        # stayed healthy — the graph tests would skip and the run would look fine.
        for name in sorted(declared):
            if name.startswith("NEO4J_") or name == "REDIS_URL":
                failures.append(
                    f"{GITLAB_CI}: {name} is declared in `variables:`, which GitLab also "
                    f"injects into the service containers. NEO4J_* names are parsed as "
                    f"neo4j.conf settings and an unrecognised one kills the service on "
                    f"startup (measured on neo4j:5.26-community). Export it in "
                    f"`before_script` instead, where only the test process sees it."
                )

        services = _gitlab_service_map(gitlab)
        for alias, compose_name in (
            ("postgres", "postgres"),
            ("redis", "redis"),
            ("neo4j", "neo4j"),
        ):
            service = services.get(alias)
            if service is None:
                failures.append(f"{GITLAB_CI}: no service aliased {alias!r}.")
                continue
            runs = str(service.get("name", ""))
            expected = images.get(compose_name, "")
            if runs != expected:
                failures.append(
                    f"{GITLAB_CI}: service {alias!r} runs {runs!r} but "
                    f"docker-compose.yml runs {expected!r}. CI must exercise the images "
                    f"developers do, or a green pipeline is evidence about a different "
                    f"stack than the one the project ships."
                )

        pg_vars = {
            str(k): str(v) for k, v in (services.get("postgres", {}).get("variables") or {}).items()
        }
        compose_pg = {
            str(k): str(v)
            for k, v in (
                (compose.get("services") or {}).get("postgres", {}).get("environment") or {}
            ).items()
        }
        for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            if pg_vars.get(key) != compose_pg.get(key):
                failures.append(
                    f"{GITLAB_CI}: postgres {key}={pg_vars.get(key)!r} but docker-compose.yml "
                    f"says {compose_pg.get(key)!r}. proxyshop_support.postgres.ROLES pins the "
                    f"admin credential to compose's value; a CI copy that drifts authenticates "
                    f"as nobody."
                )

        neo_vars = {
            str(k): str(v) for k, v in (services.get("neo4j", {}).get("variables") or {}).items()
        }
        compose_neo = {
            str(k): str(v)
            for k, v in (
                (compose.get("services") or {}).get("neo4j", {}).get("environment") or {}
            ).items()
        }
        if neo_vars.get("NEO4J_AUTH") != compose_neo.get("NEO4J_AUTH"):
            failures.append(
                f"{GITLAB_CI}: neo4j NEO4J_AUTH={neo_vars.get('NEO4J_AUTH')!r} but "
                f"docker-compose.yml says {compose_neo.get('NEO4J_AUTH')!r}. conftest.py "
                f"defaults NEO4J_USER/NEO4J_PASSWORD to compose's pair, so a drifted CI value "
                f"makes every graph test skip on an authentication failure."
            )

    github_path = ROOT / GITHUB_CI
    if not github_path.exists():
        failures.append(f"{GITHUB_CI} is missing — the mirror has no pipeline.")
    else:
        by_job = _github_env_by_job(_load(github_path))
        if not by_job:
            failures.append(f"{GITHUB_CI} declares no jobs, so it grades nothing.")
        for job_name, env in sorted(by_job.items()):
            # The job NAME is in the label: with more than one job, "PROXYSHOP_WORKER is 0"
            # is not actionable unless it says which job's.
            _check_worker_index(f"{GITHUB_CI} (job {job_name})", env, failures)
            _check_forbidden_dsns(f"{GITHUB_CI} (job {job_name})", env, failures)

    if failures:
        return _fail(
            "CI CONFIGURATION DRIFT — a pipeline no longer matches this repository.",
            *(f"  * {f}" for f in failures),
        )
    print(f"OK: {GITLAB_CI} and {GITHUB_CI} agree with docker-compose.yml.")
    print(f"    images: {images}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    wait = sub.add_parser("wait", help="block until the three datastores answer")
    wait.add_argument("--timeout", type=float, default=180.0)
    wait.set_defaults(func=cmd_wait)

    prove = sub.add_parser("prove", help="run the datastore-backed class and require it executed")
    prove.add_argument("--marker", default=MARKER)
    prove.add_argument("--min", type=int, default=DEFAULT_MIN)
    prove.set_defaults(func=cmd_prove)

    check = sub.add_parser("check-config", help="CI files must agree with docker-compose.yml")
    check.set_defaults(func=cmd_check_config)

    args = parser.parse_args()
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
