"""S4 — both learning loops, graded at the doors they are served behind (T-083).

    PROXYSHOP_WORKER=<n> .venv/bin/python -m pytest e2e/test_learning.py -q

SPEC S4, verbatim::

    In simulation, shifting a store's conversion outcomes reorders shortlists for the
    affected intent cluster (R16), and a store agent's discount depth distribution shifts
    in the direction of its own win/loss record (R17). Checkable by simulation assertions
    with fixed seeds.

Two properties, and until this file existed neither had a test. ``e2e/support/learning`` drives
both; this file only asserts, so each criterion is readable as the criterion it is.

**R16** — ``POST /internal/outcomes`` on the real exchange app. Conversion outcomes go in as HTTP
requests, exactly as the trust service sends them (R13), and the exchange's own contextual bandit
reorders the affected cluster's exposure ranking: the order in which stores draw that cluster's
shortlist opportunity. The control cluster, which nothing was posted into, does not move.

**R17** — ``POST /v1/bid-requests`` on the real store-agent app. A store's own win/loss record
travels through ``store_agent.learning`` into the ``unit_price`` and ``offer.discount`` of the bid
the agent actually serves; a rival with the mirror-image record, seeded from the *same* network
prior, moves the other way; and no amount of rival discount data in the platform's cross-store
corpus moves either of them by a single byte.

Direction and a floor, never a magnitude
----------------------------------------
"Measurably reorder" and "shifts in the direction of" are directional claims, so that is the shape
of every assertion below: **which** store leads the ranking, **which** way the depth moved, and a
floor under the size of the move. Nothing here pins an exact share or an exact depth. The
alternative — asserting that beta's exposure is 0.85 — turns every unrelated tuning change
(``DEFAULT_DRAWS``, ``PRIOR_WEIGHT``, a rung added to the depth grid) into a red test that says
nothing about whether the loop still learns, and a suite that cries wolf is a suite people stop
reading. The floors are set at roughly half the measured effect, which is far outside sampling
noise and far inside anything a working loop produces:

======================================  ==========  ============
measured on this tree                   measured    floor here
======================================  ==========  ============
exposure lost by the store that stopped   -0.988      -0.50
exposure gained by the store that won     +0.850      +0.50
served discount depth, cold -> learned   0.0 -> 20%   +10 points
mean sampled depth, cold -> learned      +0.085       +0.04
mean sampled depth, cold -> rival        -0.092       -0.04
======================================  ==========  ============

Offline (C9)
------------
Both applications are reached over ASGI in-process. No socket leaves this process, no LLM is
called, no clock is read on either measured path, and nothing here needs compose to be up.

What this file does NOT claim
-----------------------------
It does not assert that ``GET /auctions/{auction_id}/shortlist`` reorders. That used to be
because it could not — ``exchange.policy.bandit.exposure`` had no production call site and the
served shortlist never read the posterior book. It has one now:
``exchange.policy.exploration.exposure_shares`` reads it, and ``exchange.ranking.serving``
imports that module (``apps/exchange/src/ranking/serving.py:58``) and applies R12's exploration
slice to the shortlist it serves.

What is asserted here is still the exposure ranking the exchange's policy produces — the *share
of shortlist opportunity* per store — and that remains the right measurement for R16, because the
served slice is deliberately bounded (one slot of four, only among the already-eligible, only for
a store the trust snapshot marks ``low_data``). A served-shortlist assertion would be a narrower
claim about that bound, not a stronger one about the learning loop.
"""

from __future__ import annotations

from typing import Any

import pytest

from e2e.support.learning import (
    AFFECTED_CLUSTER,
    CONTROL_CLUSTER,
    EXCHANGE_STORES,
    EXPOSURE_SEED,
    LEARNING_CLUSTER,
    RIVAL_STORE,
    SUBJECT_STORE,
    DepthShift,
    ExposureShift,
    run_depth_shift,
    run_exposure_shift,
    served_discount_pct,
)
from e2e.support.learning.store_policy import canonical, declined_reason, served_unit_price

ALPHA, BETA, GAMMA = EXCHANGE_STORES

#: Floors on the R16 effect. Half the measured move; see the module docstring's table.
MIN_EXPOSURE_LOST = 0.50
MIN_EXPOSURE_GAINED = 0.50

#: Floor on the R17 served move, in percentage points of discount depth.
MIN_DEPTH_POINTS_GAINED = 10.0

#: Floor on the shift in the sampled depth *distribution*, as a fraction.
MIN_MEAN_DEPTH_SHIFT = 0.04


@pytest.fixture(scope="module")
def exposure_shift() -> ExposureShift:
    """One R16 run: two eras of conversion outcomes through the served exchange door."""
    return run_exposure_shift()


@pytest.fixture(scope="module")
def depth_shift() -> DepthShift:
    """One R17 run: two store agents' own records, served as real bids."""
    return run_depth_shift()


# =====================================================================================
# Arming — every assertion below is vacuous if the doors were not actually reached
# =====================================================================================
def test_every_conversion_outcome_was_accepted_by_the_served_door(
    exposure_shift: ExposureShift,
) -> None:
    """204 on every post, and the book holds exactly the outcomes that were sent.

    The count is the load-bearing half. ``POST /internal/outcomes`` answers 204 — the word
    "Recorded." — and a door that answered it while discarding the body would leave every
    posterior at its ``Beta(1, 1)`` prior, every exposure share equal, and every reorder
    assertion below trivially green against a system that learned nothing. So the observations
    are counted out of the posteriors themselves: each Beta starts at (1, 1), so
    ``sum(alpha + beta - 2)`` over every pair is exactly how many outcomes were folded in.
    """
    assert set(exposure_shift.statuses) == {204}, exposure_shift.statuses
    assert len(exposure_shift.statuses) == exposure_shift.posted

    folded = sum(
        (alpha - 1.0) + (beta - 1.0)
        for per_store in exposure_shift.after.posteriors.values()
        for alpha, beta in per_store.values()
    )
    assert folded == float(exposure_shift.posted), (
        f"{exposure_shift.posted} outcomes were posted and {folded:.0f} reached a posterior"
    )


def test_every_solicitation_was_answered_with_a_bid_and_not_a_decline(
    depth_shift: DepthShift,
) -> None:
    """A 204 decline carries no offer, so a run of declines would measure nothing at all.

    Named separately from the depth assertions because the two failures are different repairs: a
    decline means the envelope, the cluster or the catalogue is wrong in the harness, while a
    depth that did not move means the loop is.
    """
    answers = {
        "cold": depth_shift.cold_bid,
        "learned": depth_shift.learned_bid,
        "rival": depth_shift.rival_bid,
        "polluted": depth_shift.polluted_bid,
        "cold-without-rival-discounts": depth_shift.cold_bid_without_rival_discounts,
        "cold-with-deep-rival-discounts": depth_shift.cold_bid_with_deep_rival_discounts,
    }
    declines = {label: declined_reason(bid) for label, bid in answers.items()}
    assert not any(declines.values()), declines
    assert {bid["status_code"] for bid in answers.values()} == {200}


# =====================================================================================
# R16 — the exchange's ranking policy updates from conversion outcomes
# =====================================================================================
def test_shifting_a_stores_conversions_reorders_the_affected_clusters_exposure(
    exposure_shift: ExposureShift,
) -> None:
    """R16/S4. The store that stops converting loses the cluster; the one that starts takes it.

    Every store began from an identical trust-seeded prior (``score`` equal, ``confidence`` 0.0),
    so the outcomes are the only thing that can separate them and the reorder is attributable to
    the conversions alone.

    The ranking flip is asserted at BOTH ends, not just the top. A first place that changes hands
    while the loser stays second would be a much weaker property than the full reversal a
    reversed record should produce, and asserting only ``ranking[0]`` would not tell them apart.
    """
    before = exposure_shift.before.ranking(AFFECTED_CLUSTER)
    after = exposure_shift.after.ranking(AFFECTED_CLUSTER)

    assert before[0] == ALPHA and before[-1] == BETA, before
    assert after[0] == BETA and after[-1] == ALPHA, after
    assert after != before

    was = exposure_shift.before.shares[AFFECTED_CLUSTER]
    now = exposure_shift.after.shares[AFFECTED_CLUSTER]
    assert was[ALPHA] - now[ALPHA] >= MIN_EXPOSURE_LOST, (was[ALPHA], now[ALPHA])
    assert now[BETA] - was[BETA] >= MIN_EXPOSURE_GAINED, (was[BETA], now[BETA])


def test_the_shift_never_reaches_a_cluster_it_did_not_happen_in(
    exposure_shift: ExposureShift,
) -> None:
    """R16 is contextual: an outcome in one cluster cannot move another cluster's exposure.

    The control cluster carries its own strong record and is posted into ONLY in era one, so a
    leak from the affected cluster's reversal would be visible twice over — in its posteriors and
    in the order they produce. Both are checked:

    * the posteriors, **exactly**, because that is where a leak would land and float equality on
      integer-valued Beta parameters is exact;
    * the ranking, rather than the raw shares, because the sampler draws one theta per store in
      the store book's order and that order is recency-keyed — a run that touched the stores in a
      different sequence would move the shares of an untouched cluster without any outcome having
      leaked into it. The ORDER is the property R16 states, and it is the one that survives.
    """
    assert (
        exposure_shift.after.posteriors[CONTROL_CLUSTER]
        == exposure_shift.before.posteriors[CONTROL_CLUSTER]
    )
    assert exposure_shift.after.ranking(CONTROL_CLUSTER) == exposure_shift.before.ranking(
        CONTROL_CLUSTER
    )
    # And the control's own record is a real answer rather than three untouched priors, so a leak
    # would have had something to disturb.
    assert exposure_shift.before.ranking(CONTROL_CLUSTER) == (GAMMA, BETA, ALPHA)


def test_an_outcome_that_names_no_cluster_is_refused_rather_than_pooled(
    exposure_shift: ExposureShift,
) -> None:
    """Exposure is decided WITHIN a cluster, so a report naming none has nowhere to go.

    Accepting it would need a shared bucket, and a shared bucket pools clusters the outcome never
    happened in — which is the property the test above measures. The refusal is what makes that
    property hold for a report the trust service sends malformed rather than only for the ones it
    sends correctly.
    """
    assert exposure_shift.clusterless_status == 400


def test_the_exposure_ranking_is_reproducible_at_a_fixed_seed() -> None:
    """S4 asks for fixed seeds, and this is what makes the assertions above mean anything.

    A second complete run — a new app, a new posterior book, the same outcomes over the same door
    — must report the identical shares to the last bit. ``exposure`` seeds its generator from a
    BLAKE2b digest of ``(seed, cluster_id)`` rather than from ``hash()``, whose string hashing is
    salted per process; without that, "the ranking reordered" would be a claim two runs could
    disagree about and a flaky learning test is worse than none.
    """
    first = run_exposure_shift()
    second = run_exposure_shift()

    assert first.before.shares == second.before.shares
    assert first.after.shares == second.after.shares
    assert first.before.store_order == second.before.store_order

    # Reproducibility satisfied by a constant would be no property at all: the two eras and the
    # two clusters must genuinely differ from one another inside a single run.
    assert first.after.shares[AFFECTED_CLUSTER] != first.before.shares[AFFECTED_CLUSTER]
    assert first.after.shares[AFFECTED_CLUSTER] != first.after.shares[CONTROL_CLUSTER]


def test_a_different_seed_draws_a_different_sample_of_the_same_posteriors() -> None:
    """The seed has to be doing something, or "fixed seed" is decoration.

    Same state, same cluster, a different seed: the shares must move. They must NOT move enough to
    change the ranking — the posteriors are what decide who leads — so both halves are asserted,
    which is the difference between "seeded sampling" and "the seed is ignored".
    """
    from exchange.policy import exposure

    run = run_exposure_shift()
    assert run.after.ranking(AFFECTED_CLUSTER)[0] == BETA

    at_another_seed = dict(exposure(_state_of(run), AFFECTED_CLUSTER, EXPOSURE_SEED + 1))
    assert at_another_seed != run.after.shares[AFFECTED_CLUSTER]
    ranked = tuple(sorted(at_another_seed, key=lambda s: (-at_another_seed[s], s)))
    assert ranked == run.after.ranking(AFFECTED_CLUSTER)


def _state_of(run: ExposureShift) -> Any:
    """Rebuild the state the run ended on, from the posteriors it reported.

    The run does not hand out its ``BanditState`` — the app it lived on is closed by the time a
    test reads the result — so the seed-sensitivity check reconstitutes an equal one through the
    exchange's own constructors. ``initial_state`` seeds from a snapshot in which nobody is
    blacklisted, then the recorded ``(alpha, beta)`` pairs are put back, so what is sampled is the
    state the door produced rather than a state this file invented.
    """
    from exchange.policy import initial_state
    from exchange.policy.bandit import Posterior

    from e2e.support.learning.exchange_policy import trust_snapshot

    state = initial_state(
        run.after.store_order,
        (AFFECTED_CLUSTER, CONTROL_CLUSTER),
        trust_snapshot(run.after.store_order),
    )
    for cluster, per_store in run.after.posteriors.items():
        for store_id, (alpha, beta) in per_store.items():
            state.posteriors[cluster][store_id] = Posterior(alpha, beta)
    return state


# =====================================================================================
# R17 — each store agent updates its own bid policy from its own outcomes
# =====================================================================================
def test_a_store_agents_served_bid_deepens_toward_the_depth_it_converts_at(
    depth_shift: DepthShift,
) -> None:
    """R17/S4, measured on the price a buyer would be shown.

    The subject converted at the deep rung and lost at the shallow one, twenty times each. The
    assertion is directional with a floor — the depth must be at least
    :data:`MIN_DEPTH_POINTS_GAINED` points deeper than the cold agent's — rather than "equals
    20%", so that widening the depth grid or re-tuning the sampler does not turn a working loop
    red.

    The unit price is checked alongside the depth because they are the same fact reaching a buyer
    two ways, and an offer whose ``discount`` moved while its ``unit_price`` did not would be a
    bid nobody could act on.
    """
    cold = served_discount_pct(depth_shift.cold_bid)
    learned = served_discount_pct(depth_shift.learned_bid)

    assert learned - cold >= MIN_DEPTH_POINTS_GAINED, (cold, learned)
    assert served_unit_price(depth_shift.learned_bid) < served_unit_price(depth_shift.cold_bid)

    # The served price and the learned policy are one decision, not two that happen to agree.
    action = depth_shift.policies["learned"]["actions"][LEARNING_CLUSTER]
    assert float(action["discount_pct"]) == learned


def test_a_rival_with_the_mirror_image_record_moves_the_other_way(
    depth_shift: DepthShift,
) -> None:
    """The direction is the store's own, not the network's.

    Both agents were seeded from the SAME ``NetworkPrior`` object and asked the same question. If
    the prior were driving the depth they would land in the same place; they land on opposite
    sides, which is what makes the subject's move attributable to its own win/loss record.
    """
    learned = served_discount_pct(depth_shift.learned_bid)
    rival = served_discount_pct(depth_shift.rival_bid)
    cold = served_discount_pct(depth_shift.cold_bid)

    assert rival < learned, (rival, learned)
    assert rival <= cold, (rival, cold)
    assert depth_shift.policies["learned"]["store_id"] == SUBJECT_STORE
    assert depth_shift.policies["rival"]["store_id"] == RIVAL_STORE


def test_the_depth_distribution_shifts_in_the_direction_of_the_win_loss_record(
    depth_shift: DepthShift,
) -> None:
    """S4 names the *distribution*, not the mode, so this reads the sampler over a seed window.

    ``to_learned_policy`` renders the argmax and that is what the served bid carries; a loop that
    moved only its argmax would satisfy the served-bid assertion above while exploring exactly as
    it did before. So the mean depth ``sample_depth`` draws over 400 fixed seeds is measured too,
    and it has to move both ways: up for the store that converts deep, down for the one that
    converts shallow.
    """
    means = depth_shift.mean_depths
    assert means["learned"] - means["cold"] >= MIN_MEAN_DEPTH_SHIFT, means
    assert means["cold"] - means["rival"] >= MIN_MEAN_DEPTH_SHIFT, means

    # Fixed seeds, so the same window sampled twice is the same window.
    assert depth_shift.mean_depths_again == means


def test_a_store_agent_never_learns_from_a_rivals_discounts(depth_shift: DepthShift) -> None:
    """R17's privacy clause, asserted on the served bid rather than on the state behind it.

    Three ways a rival's discount data could reach this store, and all three are closed:

    1. **Through its own update.** The rival's outcome rows — its depths, its wins — are folded
       into the subject's bound state. A bound state drops every row naming another store, so the
       bid it serves must be byte-identical to the one it served before.
    2. **Through the network prior.** The cross-store corpus carries every rival's real discount
       depth. Stripping those fields must change neither the prior nor the served bid.
    3. **Through a rival that goes very deep.** Rewriting the corpus so every rival discounted to
       80% must also change neither — the mechanism is an allowlist that never reads the field,
       so the *value* cannot matter, and asserting that is what distinguishes blindness from a
       coincidence about the numbers chosen.
    """
    assert canonical(depth_shift.polluted_bid) == canonical(depth_shift.learned_bid)
    assert depth_shift.policies["polluted"] == depth_shift.policies["learned"]

    assert depth_shift.priors["discounts_stripped"] == depth_shift.priors["as_recorded"]
    assert depth_shift.priors["rivals_deepened"] == depth_shift.priors["as_recorded"]

    baseline = canonical(depth_shift.cold_bid)
    assert canonical(depth_shift.cold_bid_without_rival_discounts) == baseline
    assert canonical(depth_shift.cold_bid_with_deep_rival_discounts) == baseline


def test_the_same_solicitation_twice_returns_the_same_bid(depth_shift: DepthShift) -> None:
    """No clock and no RNG on the bid path, which is what lets S4 fix a seed and mean it."""
    assert canonical(depth_shift.learned_bid_again) == canonical(depth_shift.learned_bid)


def test_a_cold_agent_bids_list_price_rather_than_no_bid_at_all(
    depth_shift: DepthShift,
) -> None:
    """The cold arm has to be a real bid, or "the depth moved" is measured against nothing.

    R10's cold start is a deterministic default — zero discount, the envelope's standing
    commitments — not an abstention. The catalogue list price is 100.00, so a cold agent that
    served anything other than 100.00 would mean the baseline this file measures against was
    already carrying a discount from somewhere.
    """
    assert served_discount_pct(depth_shift.cold_bid) == 0.0
    assert served_unit_price(depth_shift.cold_bid) == 100.0
