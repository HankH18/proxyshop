"""T-084 — SPEC S2, both halves, graded against the human-approved manifest.

S2, verbatim:

    The scripted dishonest store (behaviors defined in the human-approved fixture manifest)
    falls below the blacklist threshold within the simulation episode budget and disappears
    from shortlists. **Checkable against the manifest, not the trust engine's own config.**

The producer half of S2 was already built and already graded: :mod:`sim.dishonest` scripts the
adversary out of ``fixtures/manifest.json`` and
``services/sim/tests/test_dishonest.py::test_dishonest_script_emits_exactly_the_manifest_behaviours``
checks the emitted stream element for element. Nothing checked that the platform then
**catches** it. This file is that check.

Where every verdict below gets its numbers
-------------------------------------------
From ``fixtures/manifest.json``, through :func:`e2e.support.dishonest.s2_criterion`, which
refuses to default any of them. The threshold a store is graded against is
``manifest["blacklist_threshold"]``; the deadline is ``manifest["episode_budget"]``; the
adversary and the honest control roster are ``manifest["stores"]``.

``trust.scoring.BLACKLIST_THRESHOLD`` is asserted **equal** to the manifest's, by
:func:`test_the_engines_own_threshold_is_the_one_a_human_approved`, and is used nowhere else.
That direction matters: a test that read the engine's threshold and then checked the engine
met it would be equally true of an engine that scored every store ``0.0`` against a threshold
of ``1.0``. It is the tautology this project keeps finding in its own gates, and the reason
S2's final clause exists.

What runs
---------
ONE seeded run of :func:`sim.runner.run_simulation` — the same function ``python -m sim``
calls — driving the real R12 eligibility gate, the real solicitation and auction, the real
accept path, the real trust engine and the real hash-chained event store, for exactly the
approved episode budget. Then one auction on the **served** exchange
(``POST /auctions`` on ``exchange.main:create_app()``, over a real loopback socket) carrying
the delisting that run sealed into its ledger, to check the half a buyer can see.

Positive control, in the same run
----------------------------------
Every assertion about the adversary has its opposite asserted about the honest stores in the
same episode. A platform that blacklists everybody satisfies S2's first half and destroys the
product; a gate that refuses the whole roster produces an empty shortlist the adversary is
trivially absent from. Both of those fail here.

Offline (C9): no live Shopify call, no cloud, no network egress. The simulation is a pure
function of ``(manifest, seed)``; the served half runs three real ASGI apps on loopback
(D40: port 0, reported back).

Reachable how
-------------
* operator: ``.venv/bin/python -m sim`` — exit ``0`` iff the store was caught inside the
  approved budget, exit ``1`` if it never was. Asserted by
  :func:`test_the_operator_command_exits_zero_because_the_store_was_caught`.
* buyer: ``POST /auctions`` on the served exchange -> ``body["shortlist"]["slots"]``.
  Asserted by :func:`test_the_delisted_store_disappears_from_the_served_exchanges_shortlist`.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from e2e.support.dishonest import (
    UNWIRED_HOP,
    DishonestEpisodeRun,
    S2Criterion,
    denied_store_ids,
    first_episode_below,
    open_shortlist_over,
    ranked_store_ids,
    run_dishonest_episode,
    s2_criterion,
    shortlist_store_ids,
    solicited_store_ids,
)

#: How far the simulator's own per-episode bookkeeping may differ from ``build_snapshot``'s
#: independent pass over the same observations before the two are calling it a disagreement.
#: The episode score is rounded to six places by ``sim.runner``; nothing else should move.
SCORE_AGREEMENT = 1e-5


@pytest.fixture(scope="module")
def manifest() -> Mapping[str, Any]:
    """The human-approved ground truth, digest-verified by its own loader (SPEC A3)."""
    from fixtures.manifest import load_manifest

    return load_manifest()


@pytest.fixture(scope="module")
def criterion(manifest: Mapping[str, Any]) -> S2Criterion:
    """S2's pass/fail numbers, read out of the manifest and never defaulted."""
    return s2_criterion(manifest)


@pytest.fixture(scope="module")
def episode_run(manifest: Mapping[str, Any], criterion: S2Criterion) -> DishonestEpisodeRun:
    """ONE seeded simulation over the approved episode budget.

    Module-scoped because every assertion in this file is about the SAME episode: seven tests
    over seven runs would prove seven different things, and the positive control only means
    anything when the honest stores were in the room while the adversary was caught.
    """
    return run_dishonest_episode(manifest, criterion)


@pytest.fixture
def unwired_domains() -> Iterator[None]:
    """Unbind the process-wide registered-domain seam for the duration of a served test.

    ``exchange.accept.offer`` keeps the registered-domain source in a module-level slot, so a
    test file that wired it and did not restore it changes what every later served request in
    this process reads. The served run below states its own registry in its deployment
    document and must read that one.
    """
    from exchange.accept.offer import use_registered_domains

    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


# =====================================================================================
# S2, first half: the score falls below the APPROVED threshold inside the APPROVED budget
# =====================================================================================
def test_the_dishonest_store_falls_below_the_approved_threshold_inside_the_approved_budget(
    episode_run: DishonestEpisodeRun, criterion: S2Criterion
) -> None:
    """The manifest's adversary is scored below the manifest's threshold, and stays there.

    The crossing episode is computed HERE, from the per-episode scores and
    ``criterion.blacklist_threshold`` — the approved number. ``SimulationRun.caught_at`` (the
    simulator's own verdict, taken against ``trust.scoring``'s constant) is cross-checked
    against it below rather than read in its place.

    "Falls below and stays below" is the property, not "dips below once": a score that
    crosses and recovers inside the budget leaves the store back on the shortlist, which is
    not what S2 promises a buyer.
    """
    budget = criterion.episode_budget
    ran = [row.episode for row in episode_run.episodes]
    assert ran == list(range(1, budget + 1)), (
        f"the run covered episodes {ran}, not the approved 1..{budget}. {criterion.describe()}"
    )

    crossing = first_episode_below(episode_run, criterion.blacklist_threshold)
    trajectory = [(row.episode, round(row.dishonest_score, 4)) for row in episode_run.episodes]
    assert crossing is not None, (
        f"S2 FAILS: {criterion.dishonest_store_id!r} was never scored below the approved "
        f"blacklist_threshold={criterion.blacklist_threshold} in any of the {budget} approved "
        f"episodes. trajectory={trajectory}"
    )
    assert crossing <= budget, f"crossing={crossing} is outside the approved budget {budget}"

    after = [row for row in episode_run.episodes if row.episode >= crossing]
    recovered = [
        (row.episode, row.dishonest_score)
        for row in after
        if row.dishonest_score >= criterion.blacklist_threshold
    ]
    assert not recovered, (
        f"{criterion.dishonest_store_id!r} crossed at episode {crossing} and then climbed back "
        f"to or above the approved threshold {criterion.blacklist_threshold} at {recovered}; a "
        f"store that recovers is back on the shortlist. trajectory={trajectory}"
    )

    final = episode_run.episodes[-1].dishonest_score
    assert final < criterion.blacklist_threshold, (
        f"the final episode scored {final} against the approved threshold "
        f"{criterion.blacklist_threshold}. trajectory={trajectory}"
    )


def test_the_simulators_per_episode_scores_agree_with_the_snapshot_built_over_the_same_evidence(
    episode_run: DishonestEpisodeRun, criterion: S2Criterion
) -> None:
    """The run's own bookkeeping is checked against ``build_snapshot``'s independent pass.

    ``Episode.score`` is written by the simulation loop; ``snapshot["stores"][id]["score"]``
    is produced afterwards by ``trust.snapshot.build_snapshot`` over the same observations at
    the same instant, through a different call path. A simulator that reported a trajectory
    it did not compute would pass every other test in this file and fail this one.
    """
    store_id = criterion.dishonest_store_id
    assert store_id in episode_run.final_scores, (
        f"the run's final TrustSnapshot carries no entry for {store_id!r}: "
        f"{sorted(episode_run.final_scores)}"
    )
    reported = episode_run.episodes[-1].dishonest_score
    snapshot = episode_run.final_scores[store_id]
    assert abs(reported - snapshot) <= SCORE_AGREEMENT, (
        f"the simulation reported {reported} for its last episode while build_snapshot scored "
        f"the same observations at the same instant {snapshot}"
    )


def test_the_engines_own_threshold_is_the_one_a_human_approved(criterion: S2Criterion) -> None:
    """``trust.scoring.BLACKLIST_THRESHOLD`` must equal ``manifest.blacklist_threshold``.

    The authority runs from the approved document to the code, which is why this is the only
    place in this file the engine's constant is read at all. If the engine drifts, S2 is being
    graded against a number no human approved — and the drift is a defect in the engine, not a
    reason to re-point the test.
    """
    from trust.scoring import BLACKLIST_THRESHOLD

    assert float(BLACKLIST_THRESHOLD) == pytest.approx(criterion.blacklist_threshold), (
        f"trust.scoring.BLACKLIST_THRESHOLD={BLACKLIST_THRESHOLD} has drifted from the approved "
        f"manifest.blacklist_threshold={criterion.blacklist_threshold}; the manifest is ground "
        f"truth (SPEC A3) and the engine is what has to move"
    )


def test_the_simulators_own_verdict_agrees_with_the_manifest_derived_crossing(
    episode_run: DishonestEpisodeRun, criterion: S2Criterion
) -> None:
    """``caught_at`` — which ``python -m sim``'s exit status is built on — must not disagree.

    The operator command reports success on ``caught_at``. This file grades on the manifest.
    If those two ever part company the demo would report a catch that S2 does not recognise,
    so they are compared rather than trusted separately.
    """
    crossing = first_episode_below(episode_run, criterion.blacklist_threshold)
    assert episode_run.engine_caught_at == crossing, (
        f"the simulator reports caught_at={episode_run.engine_caught_at} while the approved "
        f"threshold {criterion.blacklist_threshold} was first crossed at episode {crossing}"
    )


# =====================================================================================
# The positive control: a platform that catches everybody has not satisfied S2
# =====================================================================================
def test_the_honest_stores_in_the_same_episode_are_not_caught(
    episode_run: DishonestEpisodeRun, criterion: S2Criterion
) -> None:
    """Every honest store on the approved roster stays at or above the approved threshold.

    Same run, same episode, same engine. Without this, a trust engine that returned ``0.0``
    for every store would satisfy every other assertion in this file.
    """
    assert criterion.honest_store_ids, "the approved roster has no honest store to control on"

    caught = {
        store_id: episode_run.final_scores.get(store_id)
        for store_id in criterion.honest_store_ids
        if episode_run.final_scores.get(store_id) is None
        or float(episode_run.final_scores[store_id]) < criterion.blacklist_threshold
    }
    assert not caught, (
        f"honest stores were scored below the approved blacklist_threshold "
        f"{criterion.blacklist_threshold} in the same episode that caught the adversary: "
        f"{caught}. A platform that delists the whole roster satisfies S2's first half and "
        f"destroys the product"
    )

    ever_denied = {
        row.episode: {
            store_id: status
            for store_id, status in row.denied.items()
            if store_id in criterion.honest_store_ids
        }
        for row in episode_run.episodes
    }
    offending = {episode: denials for episode, denials in ever_denied.items() if denials}
    assert not offending, (
        f"honest stores were refused at the simulation's R12 gate: {offending}. "
        f"{criterion.describe()}"
    )


# =====================================================================================
# S2, second half (a): the caught store stops being asked, inside the simulation
# =====================================================================================
def test_the_caught_store_stops_being_solicited_and_the_honest_ones_keep_trading(
    episode_run: DishonestEpisodeRun, criterion: S2Criterion
) -> None:
    """Once caught, the adversary is denied at the gate, bids in nothing and wins nothing.

    The simulation re-reads eligibility at the top of every episode from the trust it has
    accumulated so far, so this is the R12 gate reacting to the engine's own verdict rather
    than to a flag the simulator set. The honest half is asserted in the same episodes: they
    keep being solicited, one of them keeps winning, and the sale keeps being accepted.
    """
    store_id = criterion.dishonest_store_id
    crossing = first_episode_below(episode_run, criterion.blacklist_threshold)
    assert crossing is not None, "the adversary was never caught; S2's first half must pass first"

    after = [row for row in episode_run.episodes if row.episode > crossing]
    assert after, (
        f"the adversary crossed at episode {crossing}, the last of the approved budget "
        f"{criterion.episode_budget}, so no episode remains in which it could disappear. The "
        f"approved budget has to leave at least one episode after the crossing for S2's second "
        f"half to be observable"
    )

    still_asked = [row.episode for row in after if store_id in row.solicited]
    assert not still_asked, f"{store_id!r} was still solicited after being caught, at {still_asked}"

    unrefused = [row.episode for row in after if row.denied.get(store_id) != "blacklisted"]
    assert not unrefused, (
        f"{store_id!r} was not refused `blacklisted` at the gate in episodes {unrefused}; "
        f"denials were {[(row.episode, dict(row.denied)) for row in after]}"
    )

    still_bidding = [row.episode for row in after if store_id in row.bidders]
    assert not still_bidding, (
        f"{store_id!r} was still bidding after being caught, at {still_bidding}"
    )

    still_winning = [row.episode for row in after if row.winner == store_id]
    assert not still_winning, f"{store_id!r} still won the auction at {still_winning}"

    # The positive control, in the same episodes.
    honest = set(criterion.honest_store_ids)
    missing = {
        row.episode: sorted(honest - row.solicited)
        for row in episode_run.episodes
        if not honest <= row.solicited
    }
    assert not missing, (
        f"honest stores were not solicited: {missing}. A gate that refuses the whole roster "
        f"produces an empty shortlist the adversary is trivially absent from"
    )

    stalled = [
        (row.episode, row.winner, row.accepted)
        for row in after
        if row.winner not in honest or not row.accepted
    ]
    assert not stalled, (
        f"after the adversary was caught the market stopped clearing to an honest store: "
        f"{stalled}. Catching the liar must not cost the buyer the purchase"
    )


# =====================================================================================
# The seam the served half rides on, and the one hop no product code makes yet
# =====================================================================================
def test_the_run_seals_the_delisting_a_registry_writer_would_consume(
    episode_run: DishonestEpisodeRun, criterion: S2Criterion
) -> None:
    """The delisting is SEALED into the run's ledger, and carries what a registry row needs.

    Two separate things, and the order is the point. ``build_snapshot`` *recommends*
    delistings; ``sim.runner`` appends them to the hash-chained event store and to the
    canonical stream ``python -m sim --json`` publishes (T-303 a). This reads the sealed
    stream, so a run that decided to delist and recorded nothing fails here.

    The payload assertions pin the seam named in
    :data:`e2e.support.dishonest.served.UNWIRED_HOP`: the fields a real writer needs to turn
    this event into a blacklist row. They are asserted now, while the writer is still missing,
    so the seam cannot quietly change shape while nobody is reading it.
    """
    assert episode_run.chain_ok, (
        "the run's hash chain did not verify with the delistings sealed into it; a delisting "
        "the ledger cannot vouch for is not a record an auditor or an appeal can read"
    )

    sealed = episode_run.sealed
    subjects = [str(row.get("store_id")) for row in sealed]
    assert subjects == [criterion.dishonest_store_id], (
        f"the run sealed delistings for {subjects} and S2 names exactly "
        f"{[criterion.dishonest_store_id]}. {criterion.describe()}"
    )

    payload = sealed[0].get("payload")
    assert isinstance(payload, Mapping), f"the sealed delisting carries no payload: {sealed[0]!r}"
    assert str(payload.get("business_identity")) == criterion.dishonest_identity, (
        f"the sealed delisting is bound to {payload.get('business_identity')!r} rather than the "
        f"approved business_identity {criterion.dishonest_identity!r}; the blacklist is "
        f"identity-bound so a delisted operator cannot return under a fresh store_id"
    )
    assert str(payload.get("reason_code") or "").strip(), (
        f"the sealed delisting states no reason_code, which a registry row requires: {dict(payload)!r}"
    )
    assert "expires_at" in payload, (
        f"the sealed delisting omits `expires_at`, so a writer could not tell a permanent "
        f"delisting from one that lapses: {dict(payload)!r}. {UNWIRED_HOP}"
    )

    score = payload.get("score")
    threshold = payload.get("threshold")
    assert isinstance(score, (int, float)) and isinstance(threshold, (int, float)), (
        f"the sealed delisting does not state the score and threshold it was taken on: "
        f"{dict(payload)!r}"
    )
    assert float(threshold) == pytest.approx(criterion.blacklist_threshold), (
        f"the sealed delisting records threshold={threshold} while the approved manifest says "
        f"{criterion.blacklist_threshold}"
    )
    assert float(score) < criterion.blacklist_threshold, (
        f"the sealed delisting records score={score}, which is not below the approved threshold "
        f"{criterion.blacklist_threshold} it claims to have been taken on"
    )


# =====================================================================================
# S2, second half (b): the buyer-facing shortlist, on the served route
# =====================================================================================
def test_the_delisted_store_disappears_from_the_served_exchanges_shortlist(
    episode_run: DishonestEpisodeRun,
    criterion: S2Criterion,
    manifest: Mapping[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unwired_domains: None,
) -> None:
    """``POST /auctions`` on a booted exchange: the adversary is gone, the honest stay on.

    The chain under test: the simulation's own trust observations feed a real
    ``trust.main:create_app()``; the delisting **that run sealed** becomes the blacklist
    registry it answers from; a real ``exchange.main:create_app()`` reads R12 from that
    service over a loopback socket and produces the shortlist a buyer is shown.

    One link in that chain is still a stand-in, and it is named rather than hidden — see
    :data:`e2e.support.dishonest.served.UNWIRED_HOP` and
    ``registry_from_sealed_delistings``. Everything on either side of it is product code.

    The buyer states no hard constraints, so every solicited store that bids reaches the
    shortlist and the adversary's absence is attributable to trust alone.
    """
    body = open_shortlist_over(
        episode_run,
        manifest,
        criterion,
        deployment_dir=tmp_path,
        setenv=monkeypatch.setenv,
        delenv=lambda name: monkeypatch.delenv(name, raising=False),
    )

    store_id = criterion.dishonest_store_id
    honest = set(criterion.honest_store_ids)
    denials = denied_store_ids(body)
    solicited = solicited_store_ids(body)
    ranked = ranked_store_ids(body)
    slots = shortlist_store_ids(body)

    # 1. the adversary, refused with a reason that says WHICH source refused it
    assert store_id in denials, (
        f"the store the trust engine delisted was not refused by the served exchange. "
        f"denied={sorted(denials)} solicited={sorted(solicited)}. {UNWIRED_HOP}"
    )
    assert denials[store_id].get("status") == "blacklisted", denials[store_id]
    assert "trust-eligibility" in str(denials[store_id].get("reason")), (
        f"the denial did not come from the trust-backed R12 source: {denials[store_id]!r}"
    )
    assert store_id not in solicited, "a delisted store was still asked to bid"
    assert store_id not in slots, (
        f"S2 FAILS on the served path: {store_id!r} reached the buyer's shortlist "
        f"{sorted(slots)} after the trust engine delisted it"
    )

    # 2. the positive control, in the SAME served run
    assert honest <= solicited, (
        f"the served exchange did not solicit {sorted(honest - solicited)}: "
        f"{[(k, v.get('reason')) for k, v in denials.items()]}"
    )
    assert honest <= ranked, (
        f"honest stores {sorted(honest - ranked)} were solicited and then dropped by the "
        f"ranking gate: excluded={body.get('excluded')}"
    )
    assert slots, (
        f"nobody reached the shortlist, so the adversary's absence proves nothing. "
        f"ranked={sorted(ranked)} excluded={body.get('excluded')}"
    )
    assert slots <= honest, (
        f"the buyer's shortlist carries a store trust did not clear: {sorted(slots - honest)}"
    )


# =====================================================================================
# The operator's own command
# =====================================================================================
def test_the_operator_command_exits_zero_because_the_store_was_caught(
    criterion: S2Criterion,
) -> None:
    """``python -m sim`` — the demo command — reports S2 in its exit status, not in prose.

    ``0`` means the manifest's dishonest store was caught inside the approved budget; ``1``
    means it never was and must not look like success. This is the operator-visible surface of
    everything above, so it is exercised rather than assumed, and its human summary is checked
    to name the crossing episode it claims.
    """
    from sim.__main__ import main

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        status = main([])
    printed = buffer.getvalue()

    assert status == 0, (
        f"`python -m sim` exited {status}; exit 1 is the S2 failure — the dishonest store was "
        f"never scored below the blacklist threshold inside the approved budget.\n{printed}"
    )
    assert "S2 NOT SATISFIED" not in printed, printed
    assert "fell below the blacklist threshold at episode" in printed, (
        f"the operator summary does not report the crossing episode:\n{printed}"
    )
    for kind in criterion.scripted_kinds:
        assert kind in printed, (
            f"the operator summary does not name the approved behaviour {kind!r}, so a reader "
            f"cannot check the attack replayed was the attack a human signed:\n{printed}"
        )
