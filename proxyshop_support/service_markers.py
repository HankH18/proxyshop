"""Which compose services one collected test actually needs (T-109).

The root ``conftest.py`` used to compute a single session-wide reason and stamp it on every
``@pytest.mark.docker`` item, so a Redis outage skipped Postgres tests. Deciding per item
needs an answer to "which datastore does *this* test talk to", and the answer has to work
for tests **already merged**, which carry a bare ``@pytest.mark.docker`` and no argument.

Two sources, in priority order:

1. **An explicit marker argument** — ``@pytest.mark.docker("postgres")``. Unambiguous, and
   the right thing for a test that reaches a datastore without going through a shared
   fixture. Registering the marker in ``pyproject.toml`` already permits arguments;
   ``--strict-markers`` only validates the name.
2. **The fixture closure** — ``pg_role`` in ``item.fixturenames`` means Postgres, and
   ``fixturenames`` is the *transitive* closure, so a lane fixture built on ``pg_admin``
   is recognised without that lane editing anything. This is the path that actually repairs
   the merged suites: every one of T-011's S7 role-isolation tests is a bare
   ``@pytest.mark.docker`` plus a Postgres fixture.

Anything else keeps the pre-T-109 meaning — the whole stack — because a ``docker`` test
that names no service and requests no datastore fixture could be talking to any of them.
Narrowing *that* case by guessing would trade a false skip for a false failure.

This module deliberately takes plain data (marker argument tuples, fixture names) rather
than a ``pytest.Item``, so the rule is unit-testable without constructing a pytest session.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from proxyshop_support import reachability

#: Shared datastore fixture (from the root ``conftest.py``) -> the service it connects to.
#: Keys are fixture names, so a lane fixture that *depends* on one of these is covered by
#: the transitive ``item.fixturenames`` closure without appearing here.
FIXTURE_SERVICES: dict[str, str] = {
    "worker_database": "postgres",
    "pg_admin": "postgres",
    "pg_role": "postgres",
    "_neo4j_guard": "neo4j-bolt",
    "neo4j_driver": "neo4j-bolt",
    "neo4j_session": "neo4j-bolt",
    "redis_client": "redis",
}


def services_for(
    marker_args: Iterable[Sequence[object]], fixture_names: Iterable[str]
) -> tuple[str, ...]:
    """The compose services one item needs, in :data:`reachability.SERVICES` order.

    Args:
        marker_args: the ``args`` tuple of every ``docker`` marker on the item, closest
            first — i.e. ``[m.args for m in item.iter_markers("docker")]``. A bare mark
            contributes an empty tuple.
        fixture_names: the item's fixture closure, i.e. ``item.fixturenames``.

    Returns:
        A de-duplicated tuple of service names. Never empty: with nothing to go on it is
        the full stack.

    Raises:
        ValueError: if a marker names a service that does not exist. Silently widening a
            typo to "the whole stack" would reintroduce exactly the failure this module
            removes, and would look like it was working.
    """
    named: set[str] = set()
    for args in marker_args:
        for arg in args:
            if not isinstance(arg, str) or arg not in reachability.SERVICES:
                raise ValueError(
                    f"unknown compose service {arg!r} in @pytest.mark.docker; known "
                    f"services are {', '.join(reachability.SERVICES)}"
                )
            named.add(arg)

    if not named:
        named = {
            service for name in fixture_names if (service := FIXTURE_SERVICES.get(name)) is not None
        }

    if not named:
        return reachability.SERVICES
    return tuple(service for service in reachability.SERVICES if service in named)
