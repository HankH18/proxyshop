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

import os
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

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


def test_an_item_that_names_nothing_needs_the_whole_stack() -> None:
    """The conservative default: an unannotated ``docker`` test with no datastore fixture
    could be talking to anything, so it keeps exactly the pre-T-109 behaviour."""
    assert service_markers.services_for([()], ["capsys"]) == reachability.SERVICES
    assert service_markers.services_for([], []) == reachability.SERVICES


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

    # A test that names no service and requests no datastore fixture still needs
    # everything, so it is still skipped. That is the behaviour being narrowed, not lost.
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
