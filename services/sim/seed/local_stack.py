"""Two real services on loopback, so the population can drive the routes and not the library.

The population's whole claim is that it goes through ``POST /buyer/feedback`` — the served
route, with its ``StrictBool`` body, its routed-buyer gate, its once-per-order ledger, its
composition root and its published response model — rather than through
:func:`buyer_svc.feedback.submit_feedback` behind it. Calling the library would prove nothing
about R14 and would skip the one gate R14 is: an order the network did not route gets no prompt.

So this module stands up the two applications that own those doors, exactly as
``create_app()`` builds them, on ephemeral loopback ports (D40 — port 0, real port reported
back), and wires them to each other the way a deployment does:

    ``POST /buyer/feedback`` -> ``buyer_svc.composition.bind_ledger_sink(trust_url)``
                             -> ``POST /events`` on the trust service
                             -> the hash-chained event store
                             -> ``GET /snapshot``

Nothing is stubbed on that path. What IS injected is the two datastores, and only because
there is no third option offline:

* ``app.state.event_store`` — a :class:`trust.events.InMemoryEventStore`. The route's own
  ``store_for`` documents this seam ("whatever was injected ... otherwise a PostgresEventStore
  built from the environment"), and the in-memory store is the same code path down to the
  sealing: both normalise through ``normalise_event`` and seal through ``trust.ledger.seal_event``.
* ``app.state.snapshot_stores`` / ``snapshot_blacklist`` — the seam ``GET /snapshot`` publishes
  for exactly this ("so the route can be driven without a database").

**The honest limit, stated once here rather than implied.** In a deployment with Postgres,
``POST /events`` projects each observation into ``ledger.trust_observations`` and ``GET
/snapshot`` reads that table. Offline there is no table, so :func:`store_rows` performs the same
projection — ``trust.ledger.replay.observations_from_events``, which is the projection the
scorer, ``GET /events/replay?snapshots=true`` and ``POST /events``' own poison check all run —
over the chain the route just wrote, and hands the result through the published seam. It is the
same function over the same events; it is not the same table. A run that wants the table is a
run pointed at a real trust service with ``--trust-url``, which this package supports and which
:mod:`seed.targets` guards.
"""

from __future__ import annotations

__all__ = ["LocalStack", "local_stack", "store_rows"]

from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any


def store_rows(event_store: Any, roster: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``GET /snapshot``'s store records, projected off the chain the route has written.

    One row per rostered store — including a store with no observations at all, which is the
    point rather than an edge case: R12's low-data prior and the exploration slice both exist
    for stores nobody has transacted with, and a projection that omitted them would quietly
    delete the case they are for.
    """
    from trust.ledger.replay import observations_from_events

    reader = getattr(event_store, "read", None)
    events = list(reader()) if callable(reader) else []
    by_store: dict[str, list[dict[str, Any]]] = {}
    for observation in observations_from_events(events):
        by_store.setdefault(str(observation.get("store_id") or ""), []).append(dict(observation))
    return [
        {
            "store_id": str(row["store_id"]),
            "business_identity": str(row.get("business_identity") or row["store_id"]),
            "observations": by_store.get(str(row["store_id"]), []),
        }
        for row in roster
    ]


@dataclass(frozen=True)
class LocalStack:
    """The two base URLs, and the chain behind them.

    ``event_store`` is exposed so a run can read back the events the routes really wrote — the
    population asserts on those bytes rather than on its own record of what it sent.
    """

    buyer_url: str
    trust_url: str
    event_store: Any


@contextmanager
def local_stack(roster: Sequence[Mapping[str, Any]]) -> Iterator[LocalStack]:
    """Serve a buyer service and a trust service on loopback for the duration of the block.

    ``roster`` is the store list the snapshot is built over: ``{store_id, business_identity}``
    at minimum, which is the shape :func:`sim.runner.build_roster` already produces.
    """
    from buyer_svc.composition import bind_ledger_sink
    from buyer_svc.feedback import reset_submitted
    from buyer_svc.main import create_app as create_buyer_app
    from trust.events import InMemoryEventStore
    from trust.main import create_app as create_trust_app
    from trust.scoring import Blacklist

    from proxyshop_support.asgi_server import serve

    # R14's once-per-order book is PROCESS-global, not app-scoped: `FeedbackLedger` lives at
    # module level in `buyer_svc.feedback.submission`, so a second `create_app()` in the same
    # interpreter inherits the first app's claimed orders. Measured — a second population run
    # in one process was answered **409 Conflict** on its first submission, for an order the
    # previous run had claimed. A fresh stack is a fresh service, so it starts with a fresh
    # book, exactly as a restarted process would.
    #
    # The caveat, because it is real: this clears the book for the WHOLE interpreter, so a
    # second buyer app that is live at the same moment loses its claims too. That is a
    # property of the service's state being process-global rather than of this call, it is
    # why `reset_submitted` is exported at all, and this package's stacks are used serially.
    reset_submitted()

    trust_app = create_trust_app()
    event_store = InMemoryEventStore()
    trust_app.state.event_store = event_store
    trust_app.state.snapshot_stores = lambda: store_rows(event_store, roster)
    # An empty registry, and NOT "no registry". `GET /snapshot` answers 503 when it cannot read
    # a blacklist, which is the correct fail-closed posture for a deployment whose database is
    # down and the wrong answer for one that has no database at all. The registry is empty
    # rather than absent because this run starts nobody on the blacklist and then folds the
    # delisting events the chain accumulates — `blacklist_for` does that fold against
    # `app.state.event_store`, which is bound above.
    trust_app.state.snapshot_blacklist = Blacklist()

    with ExitStack() as stack:
        trust_url = stack.enter_context(serve(trust_app))
        buyer_app = create_buyer_app()
        # The real composition root, given the address explicitly rather than through TRUST_URL.
        # Two reasons it is not the environment variable: a process-wide mutation would leak
        # into anything else sharing this interpreter (a pytest session runs many apps), and an
        # address stated in the call is one a reader can see without knowing what the
        # environment held. `ensure_ledger_sink` never replaces a bound sink, so the route's own
        # request-time hook finds this one and leaves it alone.
        buyer_app.state.ledger_sink = bind_ledger_sink(trust_url)
        buyer_url = stack.enter_context(serve(buyer_app))
        yield LocalStack(buyer_url=buyer_url, trust_url=trust_url, event_store=event_store)
