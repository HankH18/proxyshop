"""Who empties the graph, and the proof that only one place has to (P5b).

``graph_schema_session`` used to run its own ``MATCH (n) DETACH DELETE n`` on top of the
one the root conftest already runs inside the same D37 flock. That made sense while
``neo4j_driver`` was session-scoped; since T-214 made ``_neo4j_guard`` function-scoped and
moved :func:`proxyshop_support.neo4j_lock.reset_graph` with it, the second wipe was pure
cost with an identical post-state.

Measured before the removal, on ``pytest services/ingest/tests -m graph`` (110 graph tests,
``PROXYSHOP_WORKER=13``, 2026-09-04): the duplicate wipe executed 109 times and deleted
**0 nodes and 0 relationships on every one of them**, while the root reset in the same runs
deleted 1 420 nodes. These tests turn that one-off measurement into a standing contract, so
the invariant it rests on — *the graph is already empty when a per-test graph fixture is
built* — fails loudly here rather than silently inviting the wipe back.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest


def _node_count(session: Any) -> int:
    """How many nodes the graph currently holds."""
    return int(session.run("MATCH (n) RETURN count(n) AS n").single()["n"])


@pytest.mark.docker
@pytest.mark.graph
def test_the_root_conftest_hands_over_an_already_empty_graph(neo4j_session: Any) -> None:
    """The premise the deleted wipe rested on: nothing is left to delete.

    ``neo4j_session`` -> ``neo4j_driver`` -> ``_neo4j_guard`` takes the flock and calls
    ``reset_graph`` before any per-directory fixture runs. If this ever stops being true,
    every fixture in this directory that assumes an empty graph is unsound — which is a
    much bigger finding than a redundant wipe, and it should surface as a failure here.
    """
    assert _node_count(neo4j_session) == 0, (
        "the root conftest's per-test reset_graph did not leave an empty graph, so the "
        "per-directory fixtures cannot assume one"
    )


@pytest.mark.docker
@pytest.mark.graph
def test_graph_schema_session_yields_an_empty_graph_with_the_schema_applied(
    graph_schema_session: Any,
) -> None:
    """The post-state the fixture promises, asserted without its second wipe.

    Empty *and* constrained: dropping the wipe must not quietly also drop the schema
    application, which is the fixture's actual job.
    """
    assert _node_count(graph_schema_session) == 0
    constraints = list(graph_schema_session.run("SHOW CONSTRAINTS"))
    assert constraints, "graph_schema_session yielded a session with no constraints applied"


@pytest.mark.docker
@pytest.mark.graph
def test_a_seeded_graph_is_gone_by_the_next_test(neo4j_session: Any) -> None:
    """Seed a node here; :func:`test_zz_the_seed_from_the_previous_test_is_gone` checks it.

    Two halves of one assertion about *hand-off* rather than about a single test, which is
    the thing a per-test reset actually has to guarantee and the thing the duplicate wipe
    was mistakenly credited with. pytest runs a module's tests in definition order, so the
    checking half is simply written below this one.
    """
    neo4j_session.run("CREATE (:P5bResetCanary {id: 'canary'})").consume()
    assert _node_count(neo4j_session) == 1


@pytest.mark.docker
@pytest.mark.graph
def test_the_seed_from_the_previous_test_is_gone(neo4j_session: Any) -> None:
    """The canary written by the test above must not survive into this one."""
    found = list(neo4j_session.run("MATCH (n:P5bResetCanary) RETURN n"))
    assert found == [], (
        "a node created by a previous test survived into this one: the per-test reset is "
        "not running, and no fixture-level wipe can substitute for it"
    )


def test_the_shared_graph_fixtures_do_not_wipe_the_graph_a_second_time() -> None:
    """Static pin: no ``DETACH DELETE`` may reappear in ``_fixtures_graph.py`` (P5b).

    Deliberately not marked ``docker``: this is the half of the contract that must hold in
    every run, including one with the stack down, because the cost it guards against is
    paid on every acquisition of a machine-global lock.

    Executed Cypher only, never prose: the fixture's docstring *explains* the deleted wipe
    and quotes the statement, and a check that flagged that would be a check nobody could
    keep green while documenting the decision.
    """
    source =Path(__file__).with_name("_fixtures_graph.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = [
        argument.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for argument in node.args
        if isinstance(argument, ast.Constant)
        and isinstance(argument.value, str)
        and "DETACH DELETE" in argument.value.upper()
    ]
    assert offenders == [], (
        "_fixtures_graph.py runs a graph wipe again. The root conftest already reset the "
        "graph inside the same D37 flock before this directory's fixtures were built "
        "(T-214), so a second wipe is measured overhead with an identical post-state — it "
        "deleted 0 nodes on 109 of 109 executions. Delete it; "
        f"found: {offenders!r}"
    )
