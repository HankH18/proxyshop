"""T-109 — one datastore's outage must not empty another datastore's gate.

Measured on ``main`` before this file existed, with Postgres and Neo4j untouched and only
``REDIS_URL`` pointed at a closed port::

    $ pytest apps/buyer/svc/tests/test_auth_vault.py -m docker -q
    6 passed                                  # stack fully up
    $ REDIS_URL=redis://localhost:6399/0 pytest apps/buyer/svc/tests/test_auth_vault.py -m docker -q
    6 skipped                                 # exit 0, no product cause

Those six are T-011's S7 role-isolation gate; 47 ``@pytest.mark.docker`` tests behave the
same way. ``reachability.skip_reason()`` took no service argument and returned a single
combined reason as soon as ANY of the three endpoints was down, and the root
``conftest.py`` stamped that one reason onto every ``docker``-marked item.

Three layers of test, deliberately, because two of them can be green while the defect is
live:

* the pure resolution rule (:mod:`proxyshop_support.service_markers`) — cheap, exhaustive;
* the probe subprocess with **no compose stack at all** (bare listening sockets stand in
  for the reachable services) — this is acceptance 2, and it runs inside
  ``verify.sh check``;
* the probe subprocess against the **real** stack with only Redis down, which is the
  literal shape of the S7 regression and therefore carries the ``docker`` mark itself.
"""

from __future__ import annotations

import ast
import json
import os
import re
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import pytest

from proxyshop_support import reachability, service_markers

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "proxyshop_support" / "tests" / "_service_skip_probe.py"


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


@contextmanager
def _listening_port() -> Iterator[int]:
    """A port with a real listener behind it, for the length of the block.

    A bare ``socket.listen`` is enough: :func:`reachability.is_open` only completes a TCP
    connect, and nothing in these tests speaks a datastore protocol to it.
    """
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        yield int(sock.getsockname()[1])
    finally:
        sock.close()


def _closed_port() -> int:
    """A port with nothing behind it: bound to learn the number, then released."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _stack(*, postgres: bool, neo4j: bool, redis: bool) -> Iterator[dict[str, str]]:
    """Environment pointing each datastore URL at an open or a closed port."""
    with ExitStack() as stack:

        def port(up: bool) -> int:
            return stack.enter_context(_listening_port()) if up else _closed_port()

        yield {
            "PROXYSHOP_PG_DSN_ADMIN": f"postgresql://u:p@127.0.0.1:{port(postgres)}/postgres",
            "NEO4J_URI": f"bolt://127.0.0.1:{port(neo4j)}",
            "REDIS_URL": f"redis://127.0.0.1:{port(redis)}/0",
        }


def _run_probe(
    tmp_path: Path, env_overrides: dict[str, str], *, select: str
) -> dict[str, tuple[str, str]]:
    """Run the probe file in a real pytest subprocess; return ``{name: (outcome, message)}``.

    ``outcome`` is one of ``passed``/``skipped``/``failure``/``error``, read out of the
    JUnit report rather than scraped from stdout so that a wording change in pytest's
    summary cannot quietly turn this test into a tautology.
    """
    report = tmp_path / "probe.xml"
    env = {**os.environ, **env_overrides}
    env.pop("PYTEST_CURRENT_TEST", None)
    env.setdefault("PROXYSHOP_WORKER", "1")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(PROBE),
            "-k",
            select,
            "-p",
            "no:cacheprovider",
            "--no-header",
            "-q",
            f"--junitxml={report}",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert report.is_file(), (
        f"the probe subprocess wrote no report (rc={completed.returncode})\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    outcomes: dict[str, tuple[str, str]] = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        name = case.get("name") or "?"
        outcome, message = "passed", ""
        for child in case:
            if child.tag in ("skipped", "failure", "error"):
                outcome, message = child.tag, child.get("message") or ""
        outcomes[name] = (outcome, message)
    assert outcomes, f"the probe subprocess collected nothing\n{completed.stdout}"
    return outcomes


# --------------------------------------------------------------------------------------
# 1. the probe itself is per-service
# --------------------------------------------------------------------------------------


def test_skip_reason_answers_about_one_service_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres up, Neo4j up, Redis down: only the Redis question has an answer."""
    with _stack(postgres=True, neo4j=True, redis=False) as env:
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        assert reachability.skip_reason("postgres", timeout=0.5) is None
        assert reachability.skip_reason("neo4j-bolt", timeout=0.5) is None
        assert reachability.skip_reason("postgres", "neo4j-bolt", timeout=0.5) is None

        redis_reason = reachability.skip_reason("redis", timeout=0.5)
        assert redis_reason is not None
        # Acceptance 3: the message names the service that was actually down.
        assert "redis" in redis_reason
        assert "postgres" not in redis_reason

        # No argument still means "the whole stack", which IS down here.
        assert reachability.skip_reason(timeout=0.5) is not None


def test_skip_reason_rejects_a_service_it_does_not_know() -> None:
    """Including the pre-T-109 call shape ``skip_reason(0.2)``, where 0.2 meant a timeout.

    A silently-accepted stray positional would probe the whole stack while reading like a
    per-service question — the exact ambiguity this ticket exists to remove — so the
    signature change is required to be loud, not merely compatible.
    """
    with pytest.raises(ValueError, match="unknown compose service"):
        reachability.skip_reason("mongo")
    with pytest.raises(ValueError, match="unknown compose service"):
        reachability.skip_reason(0.2)  # type: ignore[arg-type]


def test_every_advertised_service_has_an_endpoint() -> None:
    by_name = reachability.endpoints_by_name()
    assert tuple(by_name) == reachability.SERVICES
    assert set(service_markers.FIXTURE_SERVICES.values()) <= set(reachability.SERVICES)


# --------------------------------------------------------------------------------------
# 2. the resolution rule: which services does one collected item actually need
# --------------------------------------------------------------------------------------


def test_an_explicit_marker_argument_selects_the_service() -> None:
    assert service_markers.services_for([("postgres",)], []) == ("postgres",)
    assert service_markers.services_for([("redis", "postgres")], []) == ("postgres", "redis")


def test_the_service_is_inferred_from_the_datastore_fixtures_the_item_requests() -> None:
    """Every already-merged datastore test carries a BARE ``docker`` mark, so inference
    from the fixture closure — not the marker argument — is what actually repairs them."""
    assert service_markers.services_for([()], ["pg_role", "tmp_path"]) == ("postgres",)
    assert service_markers.services_for([()], ["neo4j_session"]) == ("neo4j-bolt",)
    assert service_markers.services_for([()], ["redis_client"]) == ("redis",)
    assert service_markers.services_for([()], ["pg_admin", "redis_client"]) == (
        "postgres",
        "redis",
    )


def test_an_explicit_argument_beats_the_fixture_closure() -> None:
    assert service_markers.services_for([("redis",)], ["pg_role"]) == ("redis",)


def test_an_item_that_names_nothing_is_refused_rather_than_widened() -> None:
    """Silence about a dependency must not read as depending on everything (T-172).

    **This replaces an assertion, so the replacement is recorded here rather than implied.**
    It used to read, under the name ``test_an_item_that_names_nothing_needs_the_whole_stack``
    and introduced by d46f931 (T-109) as "the conservative default"::

        assert service_markers.services_for([()], ["capsys"]) == reachability.SERVICES
        assert service_markers.services_for([], []) == reachability.SERVICES

    What it claimed: an item passing no marker argument and requesting no datastore fixture
    resolves to the full compose stack, keeping exactly the pre-T-109 behaviour.

    Why that claim is false rather than merely inconvenient: T-172 measured the hole it
    leaves. Eight already-merged ``docker`` items land in this branch — five schema-grants
    and two role-password tests that spin their **own** fresh-volume Postgres container
    (hence no shared fixture to infer from) and one that points at a closed loopback port
    and needs no datastore at all. All eight are Postgres-only or datastore-free, and all
    eight were skipped at exit 0 by a *Redis*-only outage, which is the pre-T-109 defect
    surviving for exactly these items — five of them security checks of the same class T-109
    was written to stop silently skipping. The old assertion did not merely permit that; it
    pinned it as the contract, which is why it has to be replaced and not just deleted.

    Revert check — would the old assertion still be wrong if this change were reverted?
    Yes. The eight items and their exit-0 skip predate this branch: the corpus was collected
    against unmodified ``main`` and named all eight, and ``tickets.json``'s T-172 record
    carries the same measurement independently. Nothing here made it true.

    The whole stack is still perfectly expressible — see ``_service_skip_probe.py``'s
    ``test_probe_whole_stack``, which now names all three services. The difference is that
    it has to be *said*, so it can be told apart from having said nothing.
    """
    with pytest.raises(ValueError, match="declares no compose service"):
        service_markers.services_for([()], ["capsys"])
    with pytest.raises(ValueError, match="declares no compose service"):
        service_markers.services_for([], [])

    # And an item that really does need everything still gets everything — the refusal is
    # about silence, not about breadth.
    assert service_markers.services_for([reachability.SERVICES], []) == reachability.SERVICES


def test_an_unknown_service_in_a_marker_is_an_error_not_a_silent_full_stack() -> None:
    with pytest.raises(ValueError, match="unknown compose service"):
        service_markers.services_for([("postgress",)], [])


# --------------------------------------------------------------------------------------
# 3. acceptance 2, with no compose stack involved at all
# --------------------------------------------------------------------------------------


def test_a_single_service_outage_does_not_skip_the_other_services_tests(
    tmp_path: Path,
) -> None:
    """Redis down, Postgres and Neo4j up: only the Redis-marked probe skips.

    This is the acceptance criterion stated as an executable claim. It needs no compose
    stack — the "reachable" services are bare listening sockets — so it runs in
    ``verify.sh check`` alongside the unit tests, which is precisely where the regression
    it guards against was invisible before.
    """
    with _stack(postgres=True, neo4j=True, redis=False) as env:
        outcomes = _run_probe(tmp_path, env, select="not fixture")

    assert outcomes["test_probe_postgres_only"][0] == "passed", outcomes
    assert outcomes["test_probe_neo4j_only"][0] == "passed", outcomes

    redis_outcome, redis_message = outcomes["test_probe_redis_only"]
    assert redis_outcome == "skipped", outcomes
    assert "redis" in redis_message

    # A test that names all three services still needs all three, so any one of them being
    # down still skips it. That is the behaviour T-109 narrowed and T-172 did not lose: the
    # skip mechanism is untouched, only the way an item SAYS it needs everything changed —
    # this probe declares the three explicitly now instead of declaring nothing and being
    # widened. (This comment used to read "a test that names no service ... is still
    # skipped", which stopped being true when T-172 made silence a refusal; the assertion
    # below never moved.)
    assert outcomes["test_probe_whole_stack"][0] == "skipped", outcomes


def test_a_postgres_outage_skips_postgres_and_leaves_the_rest_running(
    tmp_path: Path,
) -> None:
    """The mirror image, so the previous test cannot pass by hard-coding "redis"."""
    with _stack(postgres=False, neo4j=True, redis=True) as env:
        outcomes = _run_probe(tmp_path, env, select="not fixture")

    pg_outcome, pg_message = outcomes["test_probe_postgres_only"]
    assert pg_outcome == "skipped", outcomes
    assert "postgres" in pg_message
    assert outcomes["test_probe_redis_only"][0] == "passed", outcomes
    assert outcomes["test_probe_neo4j_only"][0] == "passed", outcomes


def test_a_whole_stack_outage_still_skips_every_docker_test(tmp_path: Path) -> None:
    """The non-goal: the ``docker`` marker and the skip mechanism itself are untouched."""
    with _stack(postgres=False, neo4j=False, redis=False) as env:
        outcomes = _run_probe(tmp_path, env, select="not fixture")

    assert {name: outcome for name, (outcome, _) in outcomes.items()} == {
        "test_probe_postgres_only": "skipped",
        "test_probe_neo4j_only": "skipped",
        "test_probe_redis_only": "skipped",
        "test_probe_whole_stack": "skipped",
    }


# --------------------------------------------------------------------------------------
# 4. the S7 shape: a bare `docker` mark plus a Postgres fixture, against the real stack
# --------------------------------------------------------------------------------------


@pytest.mark.docker("postgres")
def test_a_redis_outage_does_not_skip_a_bare_marked_postgres_fixture_test(
    tmp_path: Path,
) -> None:
    """The regression as it actually shipped: ``@pytest.mark.docker`` + ``pg_role``.

    Runs against the live Postgres this session already requires, with only ``REDIS_URL``
    redirected to a closed port — the one-second Redis blip from the ticket. The probe must
    RUN and pass; before T-109 it skipped, and the whole S7 role-isolation gate with it.
    """
    outcomes = _run_probe(
        tmp_path,
        {"REDIS_URL": f"redis://127.0.0.1:{_closed_port()}/0"},
        select="fixture",
    )
    outcome, message = outcomes["test_probe_needs_a_postgres_fixture"]
    assert outcome == "passed", (
        f"a Redis outage skipped a Postgres-fixture test: {outcome} {message!r}"
    )


# --------------------------------------------------------------------------------------
# 5. T-172: an item that declares nothing must STOP THE SESSION, end to end
# --------------------------------------------------------------------------------------

#: A single bare-marked item. Written into ``tmp_path`` and never into the repo: a file like
#: this sitting anywhere under ``testpaths`` would abort every other run in the tree, and
#: several workers share this checkout's siblings.
_UNDECLARED_PROBE = """\
import pytest


@pytest.mark.docker
def test_declares_no_service() -> None:
    pass
"""

_DECLARED_PROBE = """\
import pytest


@pytest.mark.docker("postgres")
def test_declares_postgres() -> None:
    pass
"""


def _run_root_conftest(tmp_path: Path, source: str, name: str) -> subprocess.CompletedProcess[str]:
    """Collect one generated module through the REAL root ``conftest.py``.

    ``-p conftest`` registers the repo's own root conftest as a plugin, so this exercises the
    actual ``pytest_collection_modifyitems`` hook rather than a re-implementation of it. The
    module itself lives outside the repo, which is what makes generating a *bare*-marked item
    safe here.
    """
    probe = tmp_path / name
    probe.write_text(source, encoding="utf-8")
    roots = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / ".pkgroot")])
    inherited = os.environ.get("PYTHONPATH")
    env = dict(
        os.environ,
        PYTHONPATH=f"{roots}{os.pathsep}{inherited}" if inherited else roots,
    )
    env.setdefault("PROXYSHOP_WORKER", "1")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            "-p",
            "conftest",
            "-p",
            "no:cacheprovider",
            "--no-header",
            "-q",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_an_undeclared_docker_item_stops_the_session_rather_than_being_widened(
    tmp_path: Path,
) -> None:
    """T-172, as an end-to-end consequence rather than a property of ``services_for``.

    This exists because converting ``_service_skip_probe.py``'s ``test_probe_whole_stack``
    from a bare mark to an explicit three-service one removed the repo's only live example
    of the undeclared case. Without something here, ``conftest.py``'s
    ``except ValueError: raise pytest.UsageError(...)`` is unpinned: replacing that handler
    with "log it and widen to the whole stack" restores T-172 in full, and MEASURED, every
    other test in this file and the T-172 repro gate both stay green while it does. This is
    the assertion that goes red for that edit.

    The exit status is the claim. ``pytest.UsageError`` is exit 4 — the session refused to
    run — as opposed to 0 (ran, or silently skipped) or 1 (ran and failed).
    """
    refused = _run_root_conftest(tmp_path, _UNDECLARED_PROBE, "test_t172_undeclared_probe.py")
    combined = refused.stdout + refused.stderr

    assert refused.returncode == 4, (
        f"a bare @pytest.mark.docker item did not stop the session: pytest exited "
        f"{refused.returncode}, not 4 (UsageError). An undeclared item that is quietly "
        f"widened to the whole stack is the T-172 defect.\n{combined}"
    )
    assert "declares no compose service" in combined, (
        f"the session stopped, but not for the documented reason, so this test is not "
        f"measuring what it claims:\n{combined}"
    )
    assert "test_declares_no_service" in combined, (
        f"the refusal does not name the offending item, so an author cannot act on it:\n{combined}"
    )

    # POSITIVE CONTROL. Without this, a harness that could not run pytest at all — a bad
    # PYTHONPATH, a missing plugin, an unset PROXYSHOP_WORKER — would also exit non-zero and
    # read as the refusal above.
    accepted = _run_root_conftest(tmp_path, _DECLARED_PROBE, "test_t172_declared_probe.py")
    assert accepted.returncode == 0, (
        f"the control probe, which declares postgres, did not run cleanly either — so the "
        f"exit 4 above is the harness, not the refusal:\n{accepted.stdout}\n{accepted.stderr}"
    )
    assert "1 passed" in accepted.stdout, accepted.stdout


# --------------------------------------------------------------------------------------
# 6. T-172, layer three: a docker item may not be gated on a service its own CODE cannot use
# --------------------------------------------------------------------------------------
#
# Sections 1-5 grade the resolution RULE. This one grades the RESULT: walk every ``docker``
# item the real suite collects and refuse any that is gated on a service its own module has no
# code-level way of reaching. Such an item is skipped at exit 0 every time that store blips —
# the pre-T-109 defect surviving one item at a time.
#
# WHY A SECOND SWEEP EXISTS. ``apps/trust/tests/test_repro_open_tickets.py`` already walks the
# corpus for exactly this, and its docstring says the layer "cannot be satisfied by stamping
# ``@pytest.mark.docker(...)``" onto things. It strips the marker CALL before scanning — that
# much was earned, and is documented there — but it then scans the rest of the module whole,
# comments and docstrings included, and the annotation the same branch added reads::
#
#     @pytest.mark.docker("postgres")  # T-172: declares Postgres; a Redis/Neo4j outage must
#                                      # not skip it
#
# The call goes, the comment stays, and the comment names Redis and Neo4j. MEASURED on this
# tree by re-running that reader with and without the branch's own ``# T-172:`` comments::
#
#     proxyshop_support/tests/test_role_password_end_to_end.py
#         as committed  ['neo4j-bolt', 'postgres', 'redis']
#         w/o comments  ['neo4j-bolt', 'postgres']      <- 'redis' came from the comment alone
#     apps/trust/tests/test_schema_grants.py
#         as committed  ['neo4j-bolt', 'postgres', 'redis']
#         w/o comments  ['postgres', 'redis']           <- 'neo4j-bolt' likewise
#
# Both annotated files therefore read as full-stack-capable BECAUSE they were annotated, so
# that sweep can never again fire on the seven items it was written for. Confirmed by
# mutation, not by reading: gating
# ``test_a_redis_outage_does_not_skip_a_bare_marked_postgres_fixture_test`` above on ``redis``
# — a lie; that test points ``REDIS_URL`` at a closed port and needs Redis DOWN — is collected
# as ``services ['redis']`` and still leaves that gate at ``2 passed``.
#
# THE GROUND TRUTH USED HERE, and why it is not the purer thing a reviewer asks for. Three
# candidates, each run over the live 272-item corpus:
#
#     whole module source (the sweep above)      0 flagged, and forgeable by one comment
#     imports only                              29 flagged, false positives throughout
#     executable source + requested fixtures     0 flagged
#
# "Imports only" does not survive contact: seven items spin their OWN Postgres container
# through ``docker``/``psql`` subprocesses and import no driver at all, and THIS file needs a
# live Postgres while importing nothing Postgres-shaped. What does hold is the union of
# (a) the module's source with comments and docstrings REMOVED — imports, identifiers and real
# string literals survive, prose does not — and (b) the datastore fixtures the item actually
# requests, which is a wiring rather than a claim.


#: Spellings that betray, in a module's own code, that it talks to a compose service.
#: Deliberately generous: the claim being made is only the contrapositive — a module whose code
#: contains NO spelling of a service cannot be talking to it.
_SERVICE_EVIDENCE: dict[str, str] = {
    "postgres": r"postgres|psycopg|\bpg_|libpq",
    "redis": r"redis",
    "neo4j-bolt": r"neo4j|bolt",
}

#: The smallest corpus this sweep will reason about. Measured at 272 on this tree; a collection
#: that silently drops most of the suite has to fail rather than pass over the remainder.
_MIN_DOCKER_ITEMS = 200

#: The plugin the collection subprocess runs under. ``tryfirst``, and the ``docker`` filtering
#: done here rather than with ``-m docker``, so the corpus is complete even when ``conftest.py``
#: turns a refusal into a ``UsageError`` that aborts collection immediately afterwards.
_CORPUS_COLLECTOR = """\
import json
import os
import pathlib

import pytest

from proxyshop_support import service_markers

_RECORDS = []


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        marker_args = [list(mark.args) for mark in item.iter_markers("docker")]
        if not marker_args:
            continue
        try:
            services = list(service_markers.services_for(marker_args, item.fixturenames))
        except Exception:
            services = []  # a refusal gates on nothing, so it can be over-gated on nothing
        _RECORDS.append(
            {
                "nodeid": item.nodeid,
                "services": services,
                "fixture_services": sorted(
                    {
                        service_markers.FIXTURE_SERVICES[name]
                        for name in item.fixturenames
                        if name in service_markers.FIXTURE_SERVICES
                    }
                ),
            }
        )


def pytest_sessionfinish(session, exitstatus):
    pathlib.Path(os.environ["PROXYSHOP_DOCKER_CORPUS_OUT"]).write_text(
        json.dumps(_RECORDS), encoding="utf-8"
    )
"""

#: One collection per process, shared by the armed guard and the sweep.
_CORPUS_CACHE: list[dict[str, Any]] | None = None
_EVIDENCE_CACHE: dict[str, frozenset[str]] = {}


def _docker_corpus(tmp_path: Path) -> list[dict[str, Any]]:
    """Every ``docker``-marked item the REAL suite collects, with what it resolves to.

    A subprocess running the repo's own collection rather than a re-implementation of it: the
    thing being graded is what ``conftest.py`` asks ``services_for`` at collection time, and
    anything short of a real collection grades a model of that instead.
    """
    global _CORPUS_CACHE
    if _CORPUS_CACHE is not None:
        return _CORPUS_CACHE

    plugin_dir = tmp_path / "docker_corpus_plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "proxyshop_docker_corpus.py").write_text(_CORPUS_COLLECTOR, encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugin_dir), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    env.setdefault("PROXYSHOP_WORKER", "1")

    # TWO passes. The default ``python_files`` glob is ``test_*.py``, so the docker-marked item
    # in ``_service_skip_probe.py`` is invisible to a normal collection and would sit outside
    # this sweep entirely. The lint fixtures are deliberately un-importable and are skipped.
    merged: dict[str, dict[str, Any]] = {}
    tails: list[str] = []
    for name, extra in (
        ("default", []),
        ("underscore", ["-o", "python_files=_*.py", "--ignore-glob=*/lint_fixtures/*"]),
    ):
        out = tmp_path / f"docker_corpus_{name}.json"
        env["PROXYSHOP_DOCKER_CORPUS_OUT"] = str(out)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "proxyshop_docker_corpus",
                *extra,
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        tails.append(f"[{name}] rc={completed.returncode}\n{completed.stdout[-1000:]}")
        if out.is_file():
            for record in json.loads(out.read_text(encoding="utf-8")):
                merged.setdefault(str(record["nodeid"]), record)

    # A non-zero return code is TOLERATED as long as a corpus came back. Now that ``services_for``
    # refuses an item that declares nothing, collecting such an item is SUPPOSED to fail the
    # session — and the assertions below name every offender where the return code names one.
    assert merged, (
        "the collection subprocesses produced no docker corpus at all, so every count below "
        "would be silently zero.\n" + "\n".join(tails)
    )
    _CORPUS_CACHE = [merged[nodeid] for nodeid in sorted(merged)]
    return _CORPUS_CACHE


def _executable_source(source: str) -> str:
    """``source`` with every comment, every docstring and every ``docker`` marker call gone.

    What survives is imports, identifiers and real string literals — the module's wiring. The
    comments and docstrings are not stripped by a regex that a cleverer comment could slip
    past: :func:`ast.parse` never records them in the first place, and :func:`ast.unparse`
    regenerates the code from the tree.

    The marker call has to go for the reason the trust sweep already documents — it is source,
    so ``@pytest.mark.docker("postgres", "neo4j-bolt", "redis")`` would otherwise put all three
    spellings into the very module it is lying about.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                # Replaced rather than deleted: a function whose whole body is a docstring
                # would otherwise unparse to an empty suite.
                body[0] = ast.Pass()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = [
                decorator
                for decorator in node.decorator_list
                if not ast.unparse(decorator).startswith("pytest.mark.docker")
            ]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _evidence_in(source: str) -> frozenset[str]:
    """The services ``source`` shows any CODE-level sign of talking to."""
    code = _executable_source(source)
    return frozenset(
        service
        for service, pattern in _SERVICE_EVIDENCE.items()
        if re.search(pattern, code, re.IGNORECASE)
    )


def _module_evidence(path: str) -> frozenset[str]:
    if path not in _EVIDENCE_CACHE:
        _EVIDENCE_CACHE[path] = _evidence_in((REPO_ROOT / path).read_text(encoding="utf-8"))
    return _EVIDENCE_CACHE[path]


def test_prose_and_the_marker_itself_supply_no_evidence() -> None:
    """The canary, and the assertion that goes red if the reader is widened back.

    Every mention of Redis and Neo4j below sits in a docstring, a comment, or the ``docker``
    marker call — the three places a test author writes when annotating rather than wiring.
    The only thing this module actually does is import ``psycopg``. Scan the whole source, as
    the trust sweep does, and it reads as needing all three services; scan the code, and it
    needs Postgres.
    """
    forged = '''\
"""A module that talks to Postgres, and to redis and neo4j only in this sentence."""

import psycopg


# T-172: declares Postgres; a Redis/Neo4j outage must not skip it
@pytest.mark.docker("postgres", "redis", "neo4j-bolt")
def test_annotated_into_the_whole_stack(pg_admin) -> None:
    """Claims redis and neo4j bolt, does neither."""
    psycopg.connect()
'''
    assert _evidence_in(forged) == {"postgres"}, (
        f"prose or the marker call is still supplying evidence: {sorted(_evidence_in(forged))}. "
        f"An annotation that manufactures its own answer key makes the sweep below vacuous."
    )

    # And the reader must still SEE a service that is genuinely wired, or the sweep is vacuous
    # in the other direction — everything would look over-gated and the threshold guard would
    # be the only thing left standing.
    wired = "import redis\nfrom neo4j import GraphDatabase\nimport psycopg\n"
    assert _evidence_in(wired) == set(reachability.SERVICES)


def test_no_docker_item_is_gated_on_a_service_its_own_module_cannot_reach(
    tmp_path: Path,
) -> None:
    """The consequence layer, on ground truth the annotated code cannot forge.

    Two guards before the claim, because a sweep over collected items goes quiet far more
    easily than it goes red — three sweeps in this repo have done exactly that (6 -> 0 of 8,
    70 -> 0 of 79, 48 -> 0 of 66).
    """
    records = _docker_corpus(tmp_path)

    assert len(records) >= _MIN_DOCKER_ITEMS, (
        f"only {len(records)} docker items collected, below the {_MIN_DOCKER_ITEMS} floor "
        f"(272 when this was written). A collection that finds nothing is the cheapest way to "
        f"silence the walk below, so it fails here instead."
    )

    # ARMED, the half that matters after the reader changed: the reader has to answer NARROW
    # for narrow modules. If it started answering "all three" everywhere, no surplus could ever
    # exist and the assertion below would pass vacuously.
    everything = frozenset(reachability.SERVICES)
    narrow = [
        record
        for record in records
        if _module_evidence(str(record["nodeid"]).split("::")[0]) != everything
    ]
    assert len(narrow) >= _MIN_DOCKER_ITEMS, (
        f"only {len(narrow)} of {len(records)} docker items live in a module whose CODE shows "
        f"evidence for fewer than all {len(reachability.SERVICES)} services (258 when this was "
        f"written). The sweep can only fire on those."
    )

    over_gated: list[str] = []
    for record in records:
        path = str(record["nodeid"]).split("::")[0]
        reachable = _module_evidence(path) | frozenset(record["fixture_services"])
        surplus = sorted(set(record["services"]) - reachable)
        if surplus:
            over_gated.append(
                f"{record['nodeid']} gated on {surplus}; its module's code and the fixtures it "
                f"requests reach only {sorted(reachable)}"
            )
    assert over_gated == [], (
        f"{len(over_gated)} docker items are gated on a service that neither their module's "
        f"code nor the fixtures they request can reach, so a store they cannot possibly use "
        f"still skips them at exit 0 — the pre-T-109 defect, surviving for exactly these "
        f"items:\n  " + "\n  ".join(over_gated)
    )
