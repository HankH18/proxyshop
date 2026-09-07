"""R16 driven through ``POST /internal/outcomes`` — the exchange's own learning door.

What this module does, and nothing else: it stands up the real exchange application, tells it
which stores trust considers eligible, and then posts conversion outcomes at its published
outcome door — one HTTP request per outcome, exactly as the trust service would (R13). Between
the two eras it reads back the exposure ranking the exchange's own policy produces for each
cluster. It asserts nothing; ``e2e/test_learning.py`` does that.

The experiment, and why it is shaped this way
---------------------------------------------
S4 asks for a **shift** producing a **reorder**, which needs a stable before and a stable after.
So the run has two eras and they differ in exactly one thing — who converted:

===========  =================================================  ==========================
era 1        ``store-alpha`` converts, ``store-beta`` does not   affected cluster
             ``store-gamma`` splits its record evenly
era 1        ``store-gamma`` converts, ``store-alpha`` does not  control cluster
             ``store-beta`` splits its record evenly
era 2        the affected cluster's record is REVERSED:          affected cluster only
             ``store-beta`` converts, ``store-alpha`` does not
===========  =================================================  ==========================

Nothing at all is posted into the control cluster during era 2. R16's routing rule is that "an
outcome in ``cluster-1`` cannot move ``cluster-2``'s exposure", and the control cluster is how
that is observed rather than assumed: it carries its own strong record, so its ranking is a real
answer that a leak would visibly disturb, not a coin flip between three untouched priors.

**Every store starts from an identical prior.** The trust rows below carry the same ``score`` and
a ``confidence`` of 0.0, so ``bandit.initial_state`` seeds all three ``(cluster, store)`` pairs
with the same ``Beta(1, 1)``. That is deliberate: it leaves the outcomes as the *only* thing that
can separate the stores, so a reorder is attributable to the conversions and to nothing else. A
harness that seeded the eventual winner with a better trust score would measure the trust
snapshot and report it as learning.

**The trust snapshot has to be wired at all**, because R12 is fail-closed inside the bandit: a
store the snapshot holds no row for is treated as blacklisted and ``exposure`` reports it at
exactly 0.0. Against the default empty snapshot every store reads 0.0 in both eras and the whole
experiment is vacuously stable — a green run measuring nothing. :func:`run_exposure_shift`
therefore configures the snapshot through the published seam,
:func:`exchange.ranking.serving.configure_ranking`, which is the same key the auction and ranking
doors read.

Determinism
-----------
``exposure(state, cluster_id, seed)`` is a pure function of its three arguments — its generator is
seeded from a BLAKE2b digest of ``(seed, cluster_id)`` rather than from ``hash()`` — so
:data:`EXPOSURE_SEED` means the same thing in every process. The run reports the raw shares; the
tests compare them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AFFECTED_CLUSTER",
    "CONTROL_CLUSTER",
    "EXCHANGE_STORES",
    "EXPOSURE_SEED",
    "OUTCOME_ROUTE",
    "ExposureShift",
    "exposure_ranking",
    "outcome_payload",
    "run_exposure_shift",
    "trust_snapshot",
]

#: The published operation this run learns through (``operationId: recordOutcome``).
OUTCOME_ROUTE = "/internal/outcomes"

#: The roster, in a fixed order. It is also the order outcomes are first posted in, which fixes
#: the order ``InMemoryBanditPosteriors`` keeps its store book in and therefore the tie-break
#: order inside the sampler. Stated once so the run is reproducible rather than incidentally
#: stable.
EXCHANGE_STORES: tuple[str, ...] = ("store-alpha", "store-beta", "store-gamma")

#: The cluster whose conversion record is shifted between the two eras.
AFFECTED_CLUSTER = "cluster-running-shoes"

#: The cluster nothing is posted into during era 2. R16's isolation is read off this one.
CONTROL_CLUSTER = "cluster-trail-packs"

#: The fixed seed S4 asks for. Any integer would do; what matters is that it never moves, so a
#: reorder is a change in the posteriors rather than a change in the draw.
EXPOSURE_SEED = 4242

#: How many outcomes each arm of era 1 carries. Twelve is enough to separate a Beta(13, 1) from a
#: Beta(1, 13) beyond any doubt at 512 draws, and small enough that the whole run is ~100 HTTP
#: requests.
ERA_ONE_OUTCOMES = 12

#: How many outcomes the era-2 reversal carries. Larger than era 1 on purpose: the shift has to
#: overcome the record era 1 laid down, not merely tie it, or "reorder" would be a coin flip
#: between two stores whose posteriors had been driven back to equality.
ERA_TWO_OUTCOMES = 24


def trust_snapshot(stores: Sequence[str] = EXCHANGE_STORES) -> dict[str, dict[str, Any]]:
    """An eligibility snapshot in which every store is identical and nobody is blacklisted.

    ``confidence`` is 0.0 so ``bandit.PRIOR_WEIGHT`` contributes nothing and every pair starts at
    ``Beta(1, 1)``. ``low_data`` is False so R12's exploration floor never fires — the floor is a
    real rule with its own gate in ``apps/exchange/tests/test_bandit.py``, and letting it pin
    shares here would put a second mechanism inside the one measurement S4 asks for.
    """
    return {
        store_id: {
            "score": 0.5,
            "confidence": 0.0,
            "blacklisted": False,
            "low_data": False,
        }
        for store_id in stores
    }


def outcome_payload(store_id: str, cluster_id: str, converted: bool) -> dict[str, Any]:
    """One ``contracts.protocol.TrustEventPayload``, as the trust service would send it.

    ``TrustEventPayload`` is ``extra="forbid"``, so every field here is required and none is
    decoration. ``delta`` carries the result: the door reads ``delta > 0.0`` as "converted" and
    folds nothing at all for a zero, which is why the sign is what the two eras differ in.
    """
    return {
        "store_id": store_id,
        "event": {
            "event_id": f"ev-{store_id}-{cluster_id}",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "feedback",
            "payload": {},
        },
        "dim": "price_honored",
        "delta": 1.0 if converted else -1.0,
        "pseudonymous_context": {"cluster_id": cluster_id, "pseudonym": "psn-0001"},
    }


def exposure_ranking(shares: Mapping[str, float]) -> tuple[str, ...]:
    """Stores in descending exposure order — the order they draw shortlist opportunity in.

    Ties break on store id so the ranking is total and reproducible. A tie is not expected here
    and would itself be a finding: three stores with a Beta(13, 1), a Beta(1, 13) and a
    Beta(7, 7) do not draw equal shares.
    """
    return tuple(sorted(shares, key=lambda store_id: (-float(shares[store_id]), store_id)))


@dataclass(frozen=True)
class Era:
    """One reading of the policy, taken after a batch of outcomes has been posted."""

    #: ``{cluster_id: {store_id: share}}`` — what ``exposure`` reported at :data:`EXPOSURE_SEED`.
    shares: dict[str, dict[str, float]]
    #: ``{cluster_id: {store_id: (alpha, beta)}}`` — the posteriors those shares came from.
    posteriors: dict[str, dict[str, tuple[float, float]]]
    #: The store book's order, which is the sampler's tie-break order and part of the contract.
    store_order: tuple[str, ...]

    def ranking(self, cluster_id: str) -> tuple[str, ...]:
        return exposure_ranking(self.shares[cluster_id])


@dataclass(frozen=True)
class ExposureShift:
    """Everything one R16 run produced, for the tests to read."""

    before: Era
    after: Era
    #: Every status the door answered, in request order. All 204 on a healthy run.
    statuses: tuple[int, ...] = field(default_factory=tuple)
    #: The status of one deliberately clusterless outcome, posted last so it cannot perturb the
    #: measurement above. R16 routes by cluster, so a report naming none has nowhere to go.
    clusterless_status: int = 0
    #: How many outcomes were posted in total, so a test can check the book actually saw them.
    posted: int = 0


def _post_batch(client: Any, rows: Sequence[tuple[str, str, bool, int]]) -> list[int]:
    """Post ``count`` copies of each ``(store, cluster, converted)`` row and return the statuses."""
    statuses: list[int] = []
    for store_id, cluster_id, converted, count in rows:
        payload = outcome_payload(store_id, cluster_id, converted)
        for _ in range(count):
            statuses.append(client.post(OUTCOME_ROUTE, json=payload).status_code)
    return statuses


def _read_era(app: Any, clusters: Sequence[str], seed: int) -> Era:
    """Read the exposure the exchange's policy reports for each cluster, at a fixed seed."""
    from exchange.policy import exposure

    state = app.state.bandit_posteriors.state()
    return Era(
        shares={cluster: dict(exposure(state, cluster, seed)) for cluster in clusters},
        posteriors={
            cluster: {
                store_id: (
                    float(state.posteriors[cluster][store_id].alpha),
                    float(state.posteriors[cluster][store_id].beta),
                )
                for store_id in state.stores
            }
            for cluster in clusters
        },
        store_order=tuple(state.stores),
    )


def run_exposure_shift(seed: int = EXPOSURE_SEED) -> ExposureShift:
    """Drive the two eras through the served outcome door and report both readings.

    The app is built once and held for the whole run, because the posterior book is process-local
    state on ``app.state`` — two apps would be two models, and "before" and "after" have to be two
    readings of one.
    """
    from exchange.main import create_app
    from exchange.ranking.serving import configure_ranking
    from fastapi.testclient import TestClient

    alpha, beta, gamma = EXCHANGE_STORES
    clusters = (AFFECTED_CLUSTER, CONTROL_CLUSTER)

    era_one: tuple[tuple[str, str, bool, int], ...] = (
        # The affected cluster: alpha converts, beta does not, gamma is genuinely undecided.
        (alpha, AFFECTED_CLUSTER, True, ERA_ONE_OUTCOMES),
        (beta, AFFECTED_CLUSTER, False, ERA_ONE_OUTCOMES),
        (gamma, AFFECTED_CLUSTER, True, ERA_ONE_OUTCOMES // 2),
        (gamma, AFFECTED_CLUSTER, False, ERA_ONE_OUTCOMES // 2),
        # The control cluster: a different, equally strong story, so its ranking is a real answer.
        (gamma, CONTROL_CLUSTER, True, ERA_ONE_OUTCOMES),
        (beta, CONTROL_CLUSTER, True, ERA_ONE_OUTCOMES // 2),
        (beta, CONTROL_CLUSTER, False, ERA_ONE_OUTCOMES // 2),
        (alpha, CONTROL_CLUSTER, False, ERA_ONE_OUTCOMES),
    )
    # The shift. Only the affected cluster is touched, and the two stores are posted in the order
    # they already sit in the store book so the sampler's tie-break order is unchanged between the
    # readings — the reorder has to come from the posteriors, not from the draw.
    era_two: tuple[tuple[str, str, bool, int], ...] = (
        (beta, AFFECTED_CLUSTER, True, ERA_TWO_OUTCOMES),
        (alpha, AFFECTED_CLUSTER, False, ERA_TWO_OUTCOMES),
    )

    app = create_app()
    configure_ranking(app, trust_snapshot=trust_snapshot())

    with TestClient(app) as client:
        statuses = _post_batch(client, era_one)
        before = _read_era(app, clusters, seed)
        statuses += _post_batch(client, era_two)
        after = _read_era(app, clusters, seed)

        # Posted last, and read separately: a report that names no cluster cannot be routed to a
        # posterior, and pooling it would move clusters it never happened in. Sent after both
        # readings so a door that (wrongly) accepted it could not have contaminated them.
        clusterless = dict(outcome_payload(alpha, AFFECTED_CLUSTER, True))
        clusterless["pseudonymous_context"] = {"pseudonym": "psn-0001"}
        clusterless_status = client.post(OUTCOME_ROUTE, json=clusterless).status_code

    return ExposureShift(
        before=before,
        after=after,
        statuses=tuple(statuses),
        clusterless_status=clusterless_status,
        posted=sum(row[3] for row in era_one + era_two),
    )
