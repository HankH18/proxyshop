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

Anything else is **refused** (T-172). Until then this branch kept the pre-T-109 meaning —
the whole stack — on the reasoning that a ``docker`` test naming no service and requesting
no datastore fixture could be talking to any of them, and that narrowing it by guessing
would trade a false skip for a false failure. Measured, that reasoning had an 8-item hole
and every item in it was Postgres-only: the tests that spin their **own** fresh-volume
Postgres container go through no shared fixture at all, so they landed in the fallback and
a Redis-only outage skipped them at exit 0 — five schema-grants/role-password *security*
checks among them, the very class of silent skip T-109 was written to stop.

Refusing is not a third guess; it is the only answer that keeps "I said nothing" and "I
said everything" distinguishable. ``@pytest.mark.docker("postgres", "neo4j-bolt", "redis")``
remains available and still means the whole stack — the difference is that it is now
*stated*, so the widening is visible in the source instead of being inferred from silence.
The root ``conftest.py`` already turns this ``ValueError`` into a ``pytest.UsageError``
naming the item, which is the same treatment a typo'd service name gets and for the same
reason: a widened skip is invisible in the frozen metrics.

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
    "_neo4j_connection": "neo4j-bolt",
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
        A de-duplicated tuple of service names, never empty. Every returned service was
        *declared* — by a marker argument or by a mapped fixture — so the tuple can be
        trusted as "what this item really talks to" rather than "what it might".

    Raises:
        ValueError: if a marker names a service that does not exist. Silently widening a
            typo to "the whole stack" would reintroduce exactly the failure this module
            removes, and would look like it was working.
        ValueError: if the item declares nothing at all (T-172). See the module docstring:
            answering "the whole stack" here is indistinguishable from an item that asked
            for the whole stack on purpose, and it is what let eight Postgres-only tests
            skip at exit 0 on a Redis-only outage.
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
        raise ValueError(
            "this @pytest.mark.docker item declares no compose service: it passes no "
            "marker argument and requests no fixture in "
            f"service_markers.FIXTURE_SERVICES ({', '.join(sorted(FIXTURE_SERVICES))}). "
            "Silence used to be read as 'the whole stack', which made it indistinguishable "
            "from an item that asked for the whole stack on purpose and let a Redis-only "
            "outage skip Postgres-only tests at exit 0 (T-172). Say what it needs — "
            'e.g. @pytest.mark.docker("postgres") for a test that drives its own Postgres '
            "container — or drop the docker mark if it needs no datastore at all. A test "
            "that really needs everything writes @pytest.mark.docker("
            f"{', '.join(repr(service) for service in reachability.SERVICES)})."
        )
    return tuple(service for service in reachability.SERVICES if service in named)
