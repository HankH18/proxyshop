"""Telling seeded feedback from earned feedback, forever, on an append-only chain.

This is the module that makes the difference between *seed* data and *fake* data, and the
reason it exists is a door that only opens one way.

The problem, stated once
------------------------
:mod:`seed.population` manufactures buyer sentiment. Every answer it sends travels through
``POST /buyer/feedback`` — the real route — and becomes a hash-chained ``feedback`` event on
the trust ledger, which moves the store's published posture and therefore its eligibility to
be solicited at all. **A ledger has no delete.** So on the day real shoppers arrive, one of
two things is true about every store's score, permanently:

* the observations behind it can be separated into *manufactured* and *earned*, or
* they cannot, and the platform can never again say how much of any store's reputation it
  made up.

There is no later repair for the second case. It is not "hard to fix"; the events are sealed
and the chain is verified by hash, so the information simply is not there to recover. That is
why the marker is written at submission time by the thing that manufactures the signal, and
why this module — the reader — exists beside it.

The marker
----------
``order_ref``, and deliberately not a payload flag or a side table.

* Every reference :mod:`seed.shoppers` mints begins with
  :data:`seed.shoppers.SIMULATED_ORDER_PREFIX` (``sim-fb-``);
  :class:`seed.population._Services` refuses to submit anything else, before the first byte
  leaves.
* ``apps/buyer/svc/src/feedback/routes.py`` copies ``order_ref`` **verbatim** onto the ledger
  event (measured: the sealed event carries ``"order_ref": "sim-fb-01-01-store-brightbean"``
  at top level, beside ``event_hash`` and ``prev_hash``).
* It is therefore inside the hash chain. A seeded observation cannot be un-marked without
  breaking ``GET /events/verify``, and a real observation cannot be marked without the same
  break.

A payload flag would have been the obvious alternative and it is worse in the way that
matters: ``submit_feedback`` builds the payload, so a marker there would be a claim this
package asked the *service* to make about its caller, and the route would have had to learn
what a simulator is. ``order_ref`` is a value the caller already owns and the route already
copies, so **the served route needs no notion of simulation at all** — which is the whole of
requirement 3's live switch. See :mod:`seed` for the seam.

What a reader can compute
-------------------------
:func:`posture_split` answers the question the marker exists for — *what would this store's
posture be if the manufactured observations had never happened?* — by replaying the chain
twice through the platform's own scorer: once whole, once with the seeded events filtered
out. Nothing is re-simulated and nothing is approximated: both passes go through
``trust.snapshot.build_snapshot``, which is the function ``GET /snapshot`` itself calls, at an
explicit ``as_of`` so the answer is reproducible rather than a function of when it was asked.

Offline and pure (D3/C9)
------------------------
No sockets, no clock, no database. The trust engine is imported and called directly here
because the arithmetic is what is wanted, not the door — and the equivalence between the two
is not assumed: ``test_seed_store`` asserts that this projection reproduces the postures the
served ``GET /snapshot`` returned during the run, byte for byte, for the same ``as_of``.
"""

from __future__ import annotations

__all__ = [
    "SEED_MARKER_FIELD",
    "SEED_MARKER_PREFIX",
    "StorePosture",
    "as_of_of",
    "is_seeded_event",
    "observation_census",
    "partition_events",
    "posture_split",
    "postures",
    "thin_roster",
]

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .shoppers import SIMULATED_ORDER_PREFIX, is_simulated_reference

#: The ledger event field the marker lives in. Named as data rather than spelled inline
#: because :mod:`seed.store` records it in the artifact's provenance block: a reader who finds
#: the artifact years from now is told which field to look at, without reading this source.
SEED_MARKER_FIELD = "order_ref"

#: The prefix that field carries when the observation was manufactured. One value, shared with
#: the writer — :mod:`seed.shoppers` mints it and :class:`seed.population._Services` enforces
#: it — so the reader and the writer cannot drift apart.
SEED_MARKER_PREFIX = SIMULATED_ORDER_PREFIX


def _order_ref_of(event: Mapping[str, Any]) -> Any:
    """An event's ``order_ref``, top level first and payload second.

    The same fallback order ``trust.ledger.replay.observations_from_events`` uses for
    ``store_id`` and ``order_ref``. Reading only the top level would have been simpler and
    would have silently classified a payload-carried reference as organic — which is the
    failure direction that matters, because an unmarked seeded observation is exactly the
    thing this module exists to prevent.
    """
    top = event.get(SEED_MARKER_FIELD)
    if top is not None:
        return top
    payload = event.get("payload")
    return payload.get(SEED_MARKER_FIELD) if isinstance(payload, Mapping) else None


def is_seeded_event(event: Mapping[str, Any]) -> bool:
    """Was this ledger event manufactured by this package?

    The check is :func:`seed.shoppers.is_simulated_reference` over the event's ``order_ref``,
    and it is deliberately a *prefix on a value the producer minted* rather than anything
    about the event's kind, its store, or when it arrived. An event with no reference at all
    is organic by this test, which is the fail-safe direction for a reader: an unrecognised
    event is never quietly excluded from a store's "earned" posture.
    """
    return is_simulated_reference(_order_ref_of(event))


def partition_events(
    events: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(seeded, organic)`` — the chain split on the marker, order preserved in both halves.

    Order matters and is not incidental: decay is a function of the recorded instants and the
    sequence they arrived in, so a projection over a re-ordered stream is a different
    projection. Both lists are copies, so a caller cannot mutate the chain it was handed.
    """
    seeded: list[dict[str, Any]] = []
    organic: list[dict[str, Any]] = []
    for event in events:
        (seeded if is_seeded_event(event) else organic).append(dict(event))
    return seeded, organic


def thin_roster(roster: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """``[{store_id, business_identity}, ...]`` — the only two roster fields a posture needs.

    Stored in the artifact so a replay needs the artifact and nothing else: no manifest, no
    generator, no catalogue. A store with zero observations still gets a row, and that is the
    point rather than tidiness — ``low_data`` and the exploration slice exist for exactly
    those stores, and a roster that omitted them would delete the case they are for.
    """
    return [
        {
            "store_id": str(row["store_id"]),
            "business_identity": str(row.get("business_identity") or row["store_id"]),
        }
        for row in roster
    ]


def as_of_of(snapshot: Mapping[str, Any]) -> str:
    """The instant a served ``GET /snapshot`` body was decayed against.

    Read back off the served bytes rather than taken from a clock, because it is the one
    value that makes the whole store-and-replay path reproducible: ``GET /snapshot?as_of=…``
    is a published query parameter, ``build_snapshot`` refuses to invent one, and a replay
    that decayed against ``now()`` would produce different numbers every time it ran and
    would prove nothing about the stored chain.

    One snapshot is scored in a single ``build_snapshot`` call, so every store and every
    dimension in it shares one ``decayed_at``. This reads the first one it finds and does not
    check the rest: a body with two of them would be a defect in the trust service, not
    something for this reader to paper over.

    Raises:
        ValueError: the body carries no dimension state, so there is no instant to record.
    """
    for entry in snapshot.values():
        if not isinstance(entry, Mapping):
            continue
        dims = entry.get("dims")
        if not isinstance(dims, Mapping):
            continue
        for state in dims.values():
            if isinstance(state, Mapping) and state.get("decayed_at"):
                return str(state["decayed_at"])
    raise ValueError(
        "this snapshot body carries no dimension `decayed_at`, so the instant it was decayed "
        "against cannot be recovered and a replay of it could not be reproduced"
    )


def postures(
    events: Iterable[Mapping[str, Any]],
    roster: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
) -> dict[str, dict[str, Any]]:
    """Every rostered store's posture, projected off a ledger stream. Offline and pure.

    This is ``GET /snapshot`` minus the socket: the same projection
    (``trust.ledger.replay.observations_from_events``, reached through
    :func:`seed.local_stack.store_rows`) into the same builder
    (``trust.snapshot.build_snapshot``) at an explicit ``as_of``. Measured equal to the served
    body for the same chain and the same instant — see
    ``test_the_offline_projection_reproduces_what_the_served_snapshot_returned``, which is
    what keeps this from being a second, divergent scorer.

    Args:
        events: the trust chain, in insertion order.
        roster: ``{store_id, business_identity}`` rows; see :func:`thin_roster`.
        as_of: the explicit instant decay is evaluated against.

    Returns:
        ``{store_id: entry}`` with the full entry ``build_snapshot`` produces — ``score``,
        ``confidence``, ``low_data``, ``blacklisted`` and all six ``dims``.
    """
    from trust.scoring import Blacklist
    from trust.snapshot import build_snapshot

    from .local_stack import store_rows

    rows = store_rows(_FrozenChain(events), thin_roster(roster))
    built = build_snapshot(rows, blacklist=Blacklist(), as_of=as_of)
    stores = built["stores"]
    return {str(store_id): dict(entry) for store_id, entry in stores.items()}


class _FrozenChain:
    """A read-only stand-in for the trust event store, holding a fixed list of events.

    :func:`seed.local_stack.store_rows` asks its argument for ``read()`` and nothing else, so
    a replay does not need an ``InMemoryEventStore``, a chain writer, or a database — which is
    what lets a stored artifact be replayed by a reader that has only the artifact.
    """

    __slots__ = ("_events",)

    def __init__(self, events: Iterable[Mapping[str, Any]]) -> None:
        self._events = [dict(event) for event in events]

    def read(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events]


@dataclass(frozen=True)
class StorePosture:
    """One store's posture with the manufactured observations, and without them."""

    store_id: str
    #: The posture the platform actually publishes today: the whole chain.
    with_seed: float
    #: What it would be if the seeded observations had never been written.
    without_seed: float
    #: How much of the published posture is manufactured. Positive means the seed helped the
    #: store; negative means it hurt it, which is the direction an over-promising store's
    #: seeded feedback moves it and is the demonstration this whole feature exists to make.
    attributable_to_seed: float
    seeded_observations: int
    organic_observations: int
    low_data_with_seed: bool
    low_data_without_seed: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "with_seed": self.with_seed,
            "without_seed": self.without_seed,
            "attributable_to_seed": self.attributable_to_seed,
            "seeded_observations": self.seeded_observations,
            "organic_observations": self.organic_observations,
            "low_data_with_seed": self.low_data_with_seed,
            "low_data_without_seed": self.low_data_without_seed,
        }


def observation_census(
    events: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    """``{store_id: {"seeded": n, "organic": n}}`` counted over events that carry an observation.

    Counted over the events that *become observations* — the ones whose payload names a
    ``dim`` — rather than over every event on the chain, so the census answers the same
    question the scorer does. An ``accepted`` or ``order_paid`` event moves no posture and is
    not evidence of anything a store earned.
    """
    census: dict[str, dict[str, int]] = {}
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("dim") is None:
            continue
        store_id = event.get("store_id") or payload.get("store_id")
        if store_id is None:
            continue
        counts = census.setdefault(str(store_id), {"seeded": 0, "organic": 0})
        counts["seeded" if is_seeded_event(event) else "organic"] += 1
    return census


def posture_split(
    events: Iterable[Mapping[str, Any]],
    roster: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
) -> dict[str, StorePosture]:
    """Every store's posture computed twice: with the seeded observations, and without them.

    The whole of requirement 3's audit, and the reason the marker is worth writing. Two full
    passes through the platform's own scorer over the same chain, differing only in which
    events are admitted, at one explicit ``as_of`` so the two are comparable.

    Both passes are given the *whole* roster, so a store whose only observations were seeded
    comes back in the "without" pass at its low-data prior rather than vanishing. "This store
    has no earned reputation" and "this store does not exist" are different sentences and a
    reader must be able to tell them apart.
    """
    chain = [dict(event) for event in events]
    _, organic = partition_events(chain)
    census = observation_census(chain)
    whole = postures(chain, roster, as_of=as_of)
    earned = postures(organic, roster, as_of=as_of)

    split: dict[str, StorePosture] = {}
    for store_id in sorted(whole):
        counts = census.get(store_id, {"seeded": 0, "organic": 0})
        with_seed = float(whole[store_id].get("score") or 0.0)
        without_seed = float(earned.get(store_id, {}).get("score") or 0.0)
        split[store_id] = StorePosture(
            store_id=store_id,
            with_seed=with_seed,
            without_seed=without_seed,
            attributable_to_seed=with_seed - without_seed,
            seeded_observations=counts["seeded"],
            organic_observations=counts["organic"],
            low_data_with_seed=bool(whole[store_id].get("low_data")),
            low_data_without_seed=bool(earned.get(store_id, {}).get("low_data")),
        )
    return split
