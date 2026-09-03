"""T-180 — ``FIXTURE_SERVICES`` must stay complete against the fixtures conftest defines.

The measured defect
-------------------
Sabotage removed ONE entry from :data:`proxyshop_support.service_markers.FIXTURE_SERVICES`
— ``"worker_database": "postgres"`` — and nothing in the suite caught it. The full run
stayed green while the map had silently narrowed, and the narrowing has teeth: without that
key a bare ``@pytest.mark.docker`` test whose only datastore fixture is ``worker_database``
resolves to *no* named service, falls through to the whole-stack default, and therefore
SKIPS whenever Redis or Neo4j is down — the exact T-109 failure the map exists to remove.
A skipped datastore test is indistinguishable from a passing one in the frozen metrics.

Only 4 of the 7 keys were pinned by any assertion (``pg_role``, ``pg_admin``,
``neo4j_session``, ``redis_client``, all in ``test_reachability_per_service.py``);
``worker_database``, ``_neo4j_guard`` and ``neo4j_driver`` were pinned by nothing, and
nothing at all tied the map to the datastore fixtures the root ``conftest.py`` actually
defines. Pinning the three stragglers by hand would fix the sabotage and leave the class of
defect alive — the NEXT fixture added to conftest would be just as unpinned.

The check
---------
So the map is derived from conftest instead of restated. The root ``conftest.py`` is parsed
(never imported — importing a conftest outside pytest's plugin machinery is its own
adventure), every ``@pytest.fixture`` is found, and a fixture counts as a *datastore*
fixture when it calls ``_require_services("...")`` itself or requests, transitively, one
that does. That derived mapping must equal ``FIXTURE_SERVICES`` exactly:

* drop or rename a key → red;
* add a datastore fixture to conftest without mapping it → red;
* leave a key behind that names no conftest fixture at all → red.

A key that names a real conftest fixture the AST did not classify as a datastore one
is deliberately *allowed*: the map may legitimately be wider than the derivation can
see (a fixture that reaches a store without going through ``_require_services``), and
failing that case would punish someone for correctly widening the map.

``conftest.py`` is orchestrator-owned and frozen, which is what makes deriving from it
sound: it is the single place these fixtures can be defined.
"""

from __future__ import annotations

import ast
from pathlib import Path

from proxyshop_support import reachability, service_markers

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFTEST = REPO_ROOT / "conftest.py"

#: The helper every datastore fixture in conftest calls to skip cleanly when its own store
#: is down (T-109). The derivation below keys off it, so a rename must be loud rather than
#: quietly reducing this whole file to "0 fixtures found, all consistent".
GUARD_CALL = "_require_services"

#: How many ``@pytest.fixture`` functions the root conftest defines. A floor, not an
#: equality, so adding a fixture does not fail this file — but a walk that silently finds
#: FEWER than exist is the failure mode that makes every assertion here vacuous, and that
#: is what this catches. Counted from the AST, not from conftest's docstring table (which
#: lists ten and omits ``worker_index``, ``_neo4j_guard`` and ``neo4j_driver``).
CONFTEST_FIXTURE_COUNT = 13

#: ``@pytest.fixture`` applies to both, and they are unrelated AST node types.
_Def = ast.FunctionDef | ast.AsyncFunctionDef


# --------------------------------------------------------------------------------------
# derivation
# --------------------------------------------------------------------------------------


def _is_fixture(node: _Def) -> bool:
    """``@pytest.fixture``/``@pytest.fixture(...)``, and the bare ``@fixture`` import too.

    Both spellings are matched because missing one does not fail — it silently shrinks the
    derived map, which is the exact shape of defect this file exists to catch.
    """
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return True
        if isinstance(target, ast.Name) and target.id == "fixture":
            return True
    return False


def _requested_fixtures(node: _Def) -> list[str]:
    args = node.args
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


def _directly_required_services(node: _Def) -> set[str]:
    """The services this fixture body names in its own ``_require_services(...)`` call.

    A BARE ``_require_services()`` means the whole stack (``conftest.py`` documents it that
    way), so it is expanded rather than read as "no datastore" — otherwise a whole-stack
    fixture would be classified as needing nothing and drop out of the derived map
    entirely, which is the silent narrowing this file is here to prevent.
    """
    services: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        if not (isinstance(call.func, ast.Name) and call.func.id == GUARD_CALL):
            continue
        if not call.args:
            services.update(reachability.SERVICES)
            continue
        for arg in call.args:
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                f"{node.name} calls {GUARD_CALL} with a non-literal argument, so this "
                f"check can no longer tell which datastore it needs: {ast.dump(arg)}"
            )
            services.add(arg.value)
    return services


def _conftest_fixtures() -> dict[str, _Def]:
    """Every ``@pytest.fixture`` the root conftest defines, by name.

    Parsed, never imported: importing a conftest outside pytest's plugin machinery is its
    own adventure, and this only needs the source shape.
    """
    source = CONFTEST.read_text()
    assert GUARD_CALL in source, (
        f"the root conftest no longer mentions `{GUARD_CALL}`, so this file would derive "
        f"an empty map and agree with anything. Point GUARD_CALL at whatever replaced it."
    )
    tree = ast.parse(source)
    fixtures = {
        node.name: node
        for node in ast.walk(tree)
        # AsyncFunctionDef is a SEPARATE node type, not a subclass: `asyncio_mode = "auto"`
        # makes async fixtures idiomatic here, and matching only FunctionDef would let an
        # async datastore fixture go unmapped while this file stayed green.
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_fixture(node)
    }
    assert len(fixtures) >= CONFTEST_FIXTURE_COUNT, (
        f"only {len(fixtures)} fixtures were found in {CONFTEST}, which defines "
        f"{CONFTEST_FIXTURE_COUNT}. The AST shape this file matches on has probably "
        f"changed, and a walk that finds fewer fixtures than exist makes every assertion "
        f"below vacuous rather than red. Found: {sorted(fixtures)}"
    )
    return fixtures


def _conftest_datastore_fixtures() -> dict[str, set[str]]:
    """Every conftest fixture that reaches a datastore -> the services it reaches.

    A fixture qualifies by calling :data:`GUARD_CALL` itself (``worker_database``,
    ``_neo4j_guard``, ``neo4j_driver``, ``redis_client``) or by requesting one that does —
    ``pg_admin`` and ``pg_role`` reach Postgres only through ``worker_database``, and
    ``neo4j_session`` reaches Neo4j only through ``neo4j_driver``.
    """
    fixtures = _conftest_fixtures()
    direct = {name: _directly_required_services(node) for name, node in fixtures.items()}

    # Transitive closure over the fixture request graph, to a fixed point.
    resolved = {name: set(services) for name, services in direct.items()}
    changed = True
    while changed:
        changed = False
        for name, node in fixtures.items():
            for requested in _requested_fixtures(node):
                inherited = resolved.get(requested)
                if inherited and not inherited <= resolved[name]:
                    resolved[name] |= inherited
                    changed = True

    return {name: services for name, services in resolved.items() if services}


# --------------------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------------------


def test_the_fixture_service_map_covers_every_datastore_fixture_conftest_defines() -> None:
    """The completeness assertion the dropped ``worker_database`` entry walked through."""
    derived = _conftest_datastore_fixtures()
    assert derived, (
        "no datastore fixtures were derived from the root conftest, so this check proves "
        "nothing about the map."
    )

    missing = sorted(set(derived) - set(service_markers.FIXTURE_SERVICES))
    assert not missing, (
        f"FIXTURE_SERVICES does not cover {missing}, which the root conftest defines as "
        f"datastore fixture(s) ({ {name: sorted(derived[name]) for name in missing} }). A "
        f"bare @pytest.mark.docker test whose only datastore fixture is one of these now "
        f"resolves to the whole stack, so it SKIPS on any single store's outage instead of "
        f"running — and a skipped datastore test is indistinguishable from a passing one "
        f"in the frozen metrics (T-109/T-180)."
    )

    # A key that names NO conftest fixture at all is a rename or a dead entry, and either
    # way the real fixture is now unmapped. A key that names a fixture this derivation did
    # not classify is NOT an error: the map may legitimately be wider than the AST can see
    # (a fixture that reaches a datastore without going through _require_services). Being
    # strict there would make correctly widening FIXTURE_SERVICES fail this test, which
    # would push the map back towards incomplete — the opposite of the point.
    defined = set(_conftest_fixtures())
    unknown = sorted(set(service_markers.FIXTURE_SERVICES) - defined)
    assert not unknown, (
        f"FIXTURE_SERVICES maps {unknown}, which the root conftest does not define as a "
        f"fixture at all. Either the fixture was renamed — in which case the real name is "
        f"now unmapped and silently widened to the whole stack — or the entry is dead."
    )


def test_every_mapped_fixture_names_the_datastore_it_actually_reaches() -> None:
    """Coverage is not enough: a key mapped to the WRONG store is just as invisible.

    ``"redis_client": "postgres"`` would keep the map complete and still skip every Redis
    test whenever Postgres blinked.
    """
    derived = _conftest_datastore_fixtures()
    for name, services in sorted(derived.items()):
        assert len(services) == 1, (
            f"conftest's `{name}` reaches {sorted(services)}, but FIXTURE_SERVICES is a "
            f"dict[str, str] and can only record one service per fixture — so "
            f"`services_for` would under-report it. Widen the map's value type before "
            f"adding a fixture that spans two datastores."
        )
        expected = next(iter(services))
        assert service_markers.FIXTURE_SERVICES.get(name) == expected, (
            f"conftest's `{name}` guards on {expected!r} but FIXTURE_SERVICES says "
            f"{service_markers.FIXTURE_SERVICES.get(name)!r}. An item requesting it would "
            f"be probed against the wrong datastore and skipped for the wrong outage."
        )
        assert expected in reachability.SERVICES, (
            f"`{name}` guards on {expected!r}, which reachability does not publish"
        )


def test_the_derivation_still_recognises_the_fixtures_it_was_written_for() -> None:
    """A canary on the AST walk itself.

    Every assertion above compares two things this file computes; if the walk silently
    stopped finding ``_require_services`` calls, ``derived`` would go empty and the
    comparisons would agree about nothing. These four are the fixtures that call the guard
    directly, and these three the ones that only inherit it — the two paths the closure has
    to get right.
    """
    derived = _conftest_datastore_fixtures()
    assert derived["worker_database"] == {"postgres"}
    assert derived["_neo4j_guard"] == {"neo4j-bolt"}
    assert derived["neo4j_driver"] == {"neo4j-bolt"}
    assert derived["redis_client"] == {"redis"}

    # inherited only — these three call nothing themselves
    assert derived["pg_admin"] == {"postgres"}, "pg_admin reaches Postgres via worker_database"
    assert derived["pg_role"] == {"postgres"}, "pg_role reaches Postgres via worker_database"
    assert derived["neo4j_session"] == {"neo4j-bolt"}, "via neo4j_driver"

    # and a fixture that touches no datastore must NOT be swept in
    assert "llm_double" not in derived
    assert "frozen_clock" not in derived
    assert "shopify_stub_url" not in derived
