"""One seeded simulation episode run, reduced to the facts S2 is graded on.

The run itself is :func:`sim.runner.run_simulation` — the same function ``python -m sim``
calls, driving the real exchange (eligibility, solicitation, auction, accept), the real trust
engine and the real hash-chained event store. Nothing here re-implements a stage or stands in
for one; this module reads the run's published output and names the parts S2 talks about.

The two halves S2 names, and where each is visible
--------------------------------------------------
* **"falls below the blacklist threshold within the episode budget"** —
  :attr:`EpisodeFacts.dishonest_score`, per episode, crossed against the *manifest's*
  threshold by :func:`first_episode_below`. Deliberately NOT ``SimulationRun.caught_at``:
  that field is the simulator's own verdict, computed against
  ``trust.scoring.BLACKLIST_THRESHOLD``. The test cross-checks the two, which is a different
  and much stronger thing than reading one of them.
* **"disappears from shortlists"** — inside the simulation this is
  :attr:`EpisodeFacts.solicited` / :attr:`EpisodeFacts.bidders` / :attr:`EpisodeFacts.winner`:
  the run re-reads eligibility from the trust it has accumulated at the top of every episode,
  so a store the engine has caught stops being asked. The buyer-facing shortlist on the served
  ``POST /auctions`` route is :mod:`.served`.

Reading the sealed delisting
----------------------------
:func:`sealed_delistings` reads ``blacklisted`` events out of ``SimulationRun.events`` — the
canonical stream ``python -m sim --json`` writes to stdout — rather than out of
``SimulationRun.snapshot["delistings"]``. The snapshot field is what the trust engine
*recommended*; the canonical stream is what the run actually **sealed into its ledger**
(T-303 a). Grading the recommendation would pass on a run that decided to delist and then
recorded nothing, which is precisely the defect that seam was built to close.
"""

from __future__ import annotations

__all__ = [
    "DishonestEpisodeRun",
    "EpisodeFacts",
    "episodes_below",
    "first_episode_below",
    "run_dishonest_episode",
    "sealed_delistings",
]

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .criterion import S2Criterion

#: The ledger kind the platform writes when it delists a store. Spelled once.
DELISTING_KIND = "blacklisted"


@dataclass(frozen=True)
class EpisodeFacts:
    """One simulated day, as S2 reads it.

    Attributes:
        episode: 1-based episode number; episode 0 is the untouched prior and runs nothing.
        dishonest_score: the adversary's trust score at the END of this episode, from
            ``trust.scoring.score`` over the observations the run had accumulated by then.
        solicited: the stores the R12 gate let the exchange ask for a bid.
        denied: ``{store_id: status}`` for the stores the gate refused.
        bidders: the stores that answered with a real bid (an R10 list-price fallback for a
            silent store is not a bid and is not counted here).
        winner: the store whose offer was selected, or ``None`` if nobody bid.
        accepted: whether that offer survived the accept gate and a checkout was minted.
    """

    episode: int
    dishonest_score: float
    solicited: frozenset[str]
    denied: Mapping[str, str]
    bidders: frozenset[str]
    winner: str | None
    accepted: bool


@dataclass(frozen=True)
class DishonestEpisodeRun:
    """Everything ``e2e/test_dishonest.py`` asserts on, from ONE seeded run.

    Attributes:
        criterion: the approved numbers this run is graded against.
        episodes: one :class:`EpisodeFacts` per episode, in order.
        final_scores: ``{store_id: score}`` from the run's own final TrustSnapshot — every
            rostered store, not just the adversary, so the positive control is available.
        sealed: the ``blacklisted`` ledger events the run sealed into its stream.
        engine_caught_at: ``SimulationRun.caught_at`` — the SIMULATOR's verdict, against the
            trust engine's own constant. Carried so the test can cross-check it against the
            manifest-derived crossing, never so the test can read it instead.
        chain_ok: the hash chain verified after the delistings were sealed into it.
        observations: every trust observation the run produced, for the served half.
    """

    criterion: S2Criterion
    episodes: tuple[EpisodeFacts, ...]
    final_scores: Mapping[str, float]
    sealed: tuple[Mapping[str, Any], ...]
    engine_caught_at: int | None
    chain_ok: bool
    observations: tuple[Mapping[str, Any], ...]

    def observations_for(self, store_id: str) -> list[dict[str, Any]]:
        """One store's trust observations, in the order the run produced them."""
        return [dict(row) for row in self.observations if str(row.get("store_id")) == store_id]


def run_dishonest_episode(
    manifest: Mapping[str, Any], criterion: S2Criterion
) -> DishonestEpisodeRun:
    """Run the whole simulated market for the APPROVED episode budget, on the APPROVED seed.

    ``episodes=`` is left at the manifest's budget rather than shortened: S2's claim is about
    what happens *inside the budget a human approved*, so a run that stopped early could not
    answer it either way.

    Pure and offline (C9): no socket, no clock, no database, no LLM.
    """
    from sim.runner import run_simulation

    run = run_simulation(manifest, criterion.seed, episodes=criterion.episode_budget)

    episodes = tuple(
        EpisodeFacts(
            episode=int(row.episode),
            dishonest_score=float(row.score),
            solicited=frozenset(str(store_id) for store_id in row.solicited),
            denied={str(store_id): str(status) for store_id, status in row.denied},
            bidders=frozenset(str(store_id) for store_id in row.bidders),
            winner=None if row.winner is None else str(row.winner),
            accepted=bool(row.accepted),
        )
        for row in run.episodes
    )

    stores = run.snapshot.get("stores")
    final_scores: dict[str, float] = {}
    if isinstance(stores, Mapping):
        for store_id, entry in stores.items():
            if isinstance(entry, Mapping) and isinstance(entry.get("score"), (int, float)):
                final_scores[str(store_id)] = float(entry["score"])

    return DishonestEpisodeRun(
        criterion=criterion,
        episodes=episodes,
        final_scores=final_scores,
        sealed=sealed_delistings(run.events),
        engine_caught_at=None if run.caught_at is None else int(run.caught_at),
        chain_ok=bool(run.chain_ok),
        observations=tuple(dict(row) for row in run.observations),
    )


def sealed_delistings(events: Any) -> tuple[Mapping[str, Any], ...]:
    """The ``blacklisted`` ledger events a run sealed into its canonical stream."""
    return tuple(
        dict(event)
        for event in events
        if isinstance(event, Mapping) and str(event.get("kind")) == DELISTING_KIND
    )


def episodes_below(run: DishonestEpisodeRun, threshold: float) -> tuple[int, ...]:
    """Every episode in which the adversary scored strictly below ``threshold``.

    ``threshold`` is the caller's — in practice ``criterion.blacklist_threshold``, read from
    the approved manifest. This function knows nothing about ``trust.scoring``'s constant, so
    an engine that changed its own threshold cannot change this answer.
    """
    return tuple(row.episode for row in run.episodes if row.dishonest_score < threshold)


def first_episode_below(run: DishonestEpisodeRun, threshold: float) -> int | None:
    """The first episode the adversary fell below ``threshold``, or ``None`` if it never did.

    ``None`` is S2's failure and must not be mistaken for "no data": a run whose adversary
    never crossed is a run in which the platform did not catch it.
    """
    crossed = episodes_below(run, threshold)
    return crossed[0] if crossed else None
