"""T-081 — the headless market simulation (acceptance 1 and 3, and the S2 payoff).

The ticket's three acceptance criteria are graded here:

1. *a fixed seed reproduces identical event streams twice* — and a different seed does not,
   which is the half that stops "deterministic" from being satisfiable by a constant;
2. *the dishonest script emits exactly the manifest behaviours* — graded next door in
   ``test_dishonest.py`` and by the frozen suite, and cross-checked here against T-080's
   generator, which builds the same replay independently;
3. *the sim runs headless in bounded steps* — no socket, no database, no clock, and a run
   length the manifest fixes.

Beyond the criteria, these tests grade the thing the criteria cannot: whether the rest of
the platform actually **survives** the approved attack. The dishonest store must fall below
the published blacklist threshold inside the approved budget and stop being solicited; the
honest control must not (a scorer that sinks everybody satisfies S2 vacuously); the ledger
chain must verify; every minted discount code must be unguessable and unique; and an
off-domain checkout URL must be refused. Each of those is a claim about somebody else's
module, driven through its public entry point.

Offline: no network, no clock, no database. The only I/O is reading the approved manifest
and the seeded category config.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

#: What ``exchange.checkout.codes.mint_code`` promises: ``PSX-`` and eight Crockford base32
#: characters. Asserted rather than assumed because these tests also assert the codes are
#: NOT reproducible, and "unguessable" is worth nothing if the alphabet is one character.
CODE_PATTERN = re.compile(r"^PSX-[0-9A-HJKMNP-TV-Z]{8}$")


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=None)


# ------------------------------------------------------------------------------------
# Acceptance 1 — one seed, one run
# ------------------------------------------------------------------------------------
def test_a_fixed_seed_reproduces_the_event_stream_byte_for_byte(
    sim_manifest: dict[str, Any],
) -> None:
    """T-081 acceptance 1. The comparison is over the canonical run, not over object ids."""
    from sim.runner import run_simulation

    first = run_simulation(sim_manifest, int(sim_manifest["seed"]))
    second = run_simulation(sim_manifest, int(sim_manifest["seed"]))
    assert _canonical(first.to_json()) == _canonical(second.to_json())


def test_a_different_seed_produces_a_different_run(sim_manifest: dict[str, Any]) -> None:
    """Determinism satisfied by a constant would be no property at all."""
    from sim.runner import run_simulation

    seed = int(sim_manifest["seed"])
    assert _canonical(run_simulation(sim_manifest, seed).to_json()) != _canonical(
        run_simulation(sim_manifest, seed + 1).to_json()
    )


def test_the_minted_codes_are_unique_and_deliberately_not_reproducible(
    sim_manifest: dict[str, Any], sim_run: Any
) -> None:
    """A discount code that two runs of one seed agree on is a guessable discount code.

    D22: ``mint_code`` draws from :mod:`secrets` precisely so the code cannot be derived
    from the offer or the auction. So the run masks codes out of its canonical stream and
    asserts the stronger property here instead — well-formed, never repeated, and different
    the next time the same seed runs.
    """
    from sim.runner import run_simulation

    assert sim_run.codes, "the run minted no discount codes at all"
    assert len(set(sim_run.codes)) == len(sim_run.codes), (
        f"a discount code was minted twice: {sim_run.codes}"
    )
    for code in sim_run.codes:
        assert CODE_PATTERN.fullmatch(code), f"{code!r} is not a well-formed PSX- code"

    again = run_simulation(sim_manifest, int(sim_manifest["seed"]))
    assert set(again.codes).isdisjoint(sim_run.codes), (
        "two runs of the same seed minted the same discount codes — the code is derivable "
        "from the run, which is exactly what D22 forbids"
    )


# ------------------------------------------------------------------------------------
# Acceptance 3 — bounded, headless
# ------------------------------------------------------------------------------------
def test_the_run_is_bounded_by_the_approved_episode_budget(
    sim_manifest: dict[str, Any], sim_run: Any
) -> None:
    """The bound is the human's. A caller may shorten a run; it may not extend one."""
    from sim.runner import SimulationError, run_simulation

    budget = int(sim_manifest["episode_budget"])
    assert [episode.episode for episode in sim_run.episodes] == list(range(1, budget + 1))

    short = run_simulation(sim_manifest, int(sim_manifest["seed"]), episodes=2)
    assert [episode.episode for episode in short.episodes] == [1, 2]

    with pytest.raises(SimulationError, match="episode budget"):
        run_simulation(sim_manifest, int(sim_manifest["seed"]), episodes=budget + 1)


def test_the_replay_matches_the_generators_own_independent_event_script(
    sim_manifest: dict[str, Any],
) -> None:
    """T-080's generator builds the same campaign from the same manifest. They must agree.

    Two implementations, written for two tickets, reading one approved document: if they
    disagree about which behaviour lands in which episode, one of them is wrong and the
    published trust trajectory is being graded against the wrong replay. Only the fields
    both produce are compared — the simulator additionally carries seeded incidentals the
    generator has no notion of.
    """
    from sim.dishonest import run_dishonest_campaign

    from fixtures.generator import generate

    shared = ("episode", "sequence", "store_id", "kind", "dim", "type", "claim_type")
    generated = [
        {key: row.get(key) for key in shared}
        for row in generate(str(sim_manifest["seed_category"]), int(sim_manifest["seed"]))[
            "event_script"
        ]
    ]
    simulated = [
        {key: row.get(key) for key in shared}
        for row in run_dishonest_campaign(sim_manifest, int(sim_manifest["seed"]))
    ]
    assert simulated == generated, (
        "the simulator and T-080's generator disagree about the approved replay schedule"
    )


# ------------------------------------------------------------------------------------
# S2 — does the rest of the platform actually survive the approved attack?
# ------------------------------------------------------------------------------------
def test_the_trust_engine_catches_the_dishonest_store_inside_the_approved_budget(
    sim_manifest: dict[str, Any], sim_run: Any
) -> None:
    """S2. The behaviours are the human's, the threshold is the human's, the verdict is the engine's."""
    from trust.scoring import BLACKLIST_THRESHOLD

    budget = int(sim_manifest["episode_budget"])
    assert sim_run.caught_at is not None, (
        "the dishonest store was never scored below the published blacklist threshold "
        f"({BLACKLIST_THRESHOLD}) in {budget} episodes of the approved script"
    )
    assert 1 <= sim_run.caught_at <= budget
    assert sim_run.episodes[-1].score < BLACKLIST_THRESHOLD


def test_the_run_reproduces_the_approved_trust_trajectory_within_its_own_tolerance(
    sim_manifest: dict[str, Any], sim_run: Any
) -> None:
    """The strongest statement this lane can make, and it is a three-way agreement.

    ``expected_trust_trajectory`` is a human-approved prediction of what the dishonest
    store's score is at named episodes. Reproducing it requires the simulator's replay
    schedule, the manifest's published weights and half-life, and the shipped scoring engine
    all to agree — three separately-owned things, none of which authored the numbers. A
    drift in any one of them shows up here as a band violation, which is precisely the
    failure the manifest's own ESC-006 note says a wrong replay schedule would produce.

    Episode 0 is the prior and is not a simulated episode, so it is checked directly against
    the engine rather than against a run row.
    """
    from sim.runner import episode_instant
    from trust.scoring import score

    by_episode = {episode.episode: episode.score for episode in sim_run.episodes}
    for row in sim_manifest["expected_trust_trajectory"]:
        episode = int(row["episode"])
        tolerance = float(row["tolerance"])
        if episode == 0:
            measured = score([], as_of=episode_instant(sim_manifest, 0))["score"]
        else:
            assert episode in by_episode, f"the run has no episode {episode}"
            measured = by_episode[episode]
        assert abs(measured - float(row["score"])) <= tolerance, (
            f"episode {episode}: the approved trajectory says {row['score']} "
            f"+-{tolerance}, the run measured {measured:.4f}"
        )


def test_the_honest_control_store_stays_well_above_the_threshold(sim_run: Any) -> None:
    """S2 is vacuous if the scorer sinks everybody; the manifest says so in as many words."""
    from trust.scoring import BLACKLIST_THRESHOLD

    stores = sim_run.snapshot["stores"]
    winners = {episode.winner for episode in sim_run.episodes if episode.accepted}
    winners.discard(None)
    assert winners, "no episode ever accepted an offer, so there is no honest control to check"
    for store_id in winners:
        assert stores[store_id]["score"] > BLACKLIST_THRESHOLD, (
            f"the honest store {store_id} that transacted every episode was scored below "
            "the blacklist threshold — a scorer that sinks everybody proves nothing about "
            "the dishonest one"
        )


def test_the_caught_store_stops_being_solicited(sim_manifest: dict[str, Any], sim_run: Any) -> None:
    """Catching the store is only half of S2; the exchange has to stop asking it."""
    dishonest = str(sim_manifest["dishonest_store"]["store_id"])
    caught = sim_run.caught_at
    assert caught is not None

    after = [episode for episode in sim_run.episodes if episode.episode > caught]
    assert after, "the store was caught in the last episode, so nothing observes the consequence"
    for episode in after:
        assert dishonest not in episode.solicited, (
            f"episode {episode.episode} still solicited {dishonest} after it was caught at "
            f"episode {caught}"
        )
        assert dishonest in {store_id for store_id, _ in episode.denied}


def test_a_store_that_never_answers_falls_back_instead_of_winning(sim_run: Any) -> None:
    """R10: the deadline is the exchange's, and a silent store cannot win by being silent."""
    fallbacks = {store_id for episode in sim_run.episodes for store_id, _ in episode.fallbacks}
    assert fallbacks, "no store ever hit the fallback path, so R10's timeout is untested here"
    for episode in sim_run.episodes:
        silent = {store_id for store_id, _ in episode.fallbacks}
        assert episode.winner not in silent, (
            f"episode {episode.episode} accepted {episode.winner}, which never answered"
        )


# ------------------------------------------------------------------------------------
# The record the platform keeps
# ------------------------------------------------------------------------------------
def test_the_ledger_chain_verifies_and_every_kind_is_one_of_the_frozen_kinds(
    sim_run: Any,
) -> None:
    """D24/D16: a hash-chained record of a run that really happened."""
    from contracts.ledger import LEDGER_EVENT_KINDS

    assert sim_run.events, "the run wrote no ledger events at all"
    assert sim_run.chain_ok, "the ledger chain did not verify after the run"
    assert sim_run.head_hash
    unknown = {str(event["kind"]) for event in sim_run.events} - set(LEDGER_EVENT_KINDS)
    assert not unknown, f"the run emitted ledger kinds outside the frozen enum: {sorted(unknown)}"


def test_the_platform_derives_the_offer_integrity_attacks_from_the_event_stream_alone(
    sim_manifest: dict[str, Any],
) -> None:
    """The adversarial question the frozen criterion cannot ask: is the attack *detectable*?

    ``sim.dishonest`` reads the manifest's ``dim``/``type`` off the approved document and
    hands them to the scorer — which is right, because the manifest is ground truth. But it
    means the simulation never asks whether the platform could have worked those out for
    itself from what a dishonest checkout actually leaves behind.

    This asks. It takes the approved ``aggressive`` persona's *pitched* values, plays a
    checkout that breaks both promises, and hands the raw ``accepted`` / ``checkout_pixel``
    / ``order_paid`` triple to ``trust.reconcile`` — nothing tells it what the manifest
    says. The verdicts it returns unaided must be exactly the ``(dim, type)`` pairs the
    manifest declares for ``bait_and_switch_unit_price`` and ``phantom_discount``.

    Only those two of the approved seven are decidable this way: ``RECONCILED_DIMENSIONS``
    grades ``price`` and ``discount`` and nothing else, so dispatch, returns, the two
    catalog claims and buyer feedback reach trust through other seams. That limit is the
    finding this test pins, not a gap in this assertion.
    """
    from trust.reconcile import reconcile, reconciled_observations

    scripted = {
        str(behaviour["kind"]): behaviour
        for behaviour in sim_manifest["dishonest_store"]["behaviours"]
    }
    claims = {
        str(claim["claim_type"]): claim
        for claim in sim_manifest["personas"]["aggressive"]["scripted_claims"]
    }
    pitched_price = float(claims["unit_price"]["value"])
    pitched_discount = float(str(claims["discount"]["value"]).rstrip("%"))
    # What checkout really does: charges more than pitched, applies a fifth of the discount.
    charged = round(pitched_price * 1.2, 2)
    applied = round(pitched_discount / 5.0, 2)
    stamp = str(sim_manifest["approval"]["approved_at"])
    store_id = str(sim_manifest["dishonest_store"]["store_id"])

    def event(event_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "ts": stamp,
            "kind": kind,
            "auction_id": "sim-probe",
            "store_id": store_id,
            "order_ref": "sim-probe-order",
            "payload": payload,
        }

    stream = [
        event(
            "ev-accepted",
            "accepted",
            {
                "bid_ref": "sim-probe-bid",
                "checkout_token": "ck-probe",
                "offer": {
                    "product_ref": "p-1",
                    "unit_price": pitched_price,
                    "total_price": pitched_price,
                    "discount": {"type": "percentage", "value": pitched_discount},
                },
            },
        ),
        event(
            "ev-pixel",
            "checkout_pixel",
            {
                "checkout_token": "ck-probe",
                "clientId": "cid-probe",
                "total_price": charged,
                "discountApplications": [{"type": "percentage", "value": applied}],
            },
        ),
        event(
            "ev-paid",
            "order_paid",
            {
                "checkout_token": "ck-probe",
                "order_ref": "sim-probe-order",
                "total_price": charged,
                "discountApplications": [{"type": "percentage", "value": applied}],
            },
        ),
    ]

    derived = {
        (str(row["dim"]), str(row["type"])) for row in reconciled_observations(reconcile(stream))
    }
    for kind in ("bait_and_switch_unit_price", "phantom_discount"):
        behaviour = scripted[kind]
        expected = (str(behaviour["dim"]), str(behaviour["type"]))
        assert expected in derived, (
            f"the approved behaviour {kind!r} declares {expected}, but reconciling the "
            f"checkout it produces derived only {sorted(derived)}. The platform cannot see "
            "the attack in its own event stream."
        )


#: The ledger kinds that are KNOWN to deviate from the body ``contracts.ledger`` publishes.
#:
#: **Empty, and it got here by working.** It used to hold two:
#:
#:   auction_opened   omitted ``roster_size``     -- apps/exchange/src/auction/state.py
#:   auction_closed   omitted ``shortlist_size``  -- apps/exchange/src/auction/state.py
#:
#: T-302 closed both — the exchange records the close AFTER the ranking now, so the row can
#: carry the size it did not know at open time — and this guard is what said so. Because it
#: asserts an EXACT set rather than an allowlist, the repair turned it red and its own failure
#: message named the two kinds and told the reader to delete them. That is the difference the
#: T-265 rewrite below exists to make, demonstrated on a real repair rather than argued.
#:
#: Leave it empty rather than deleting the constant and its guard: a new deviation must still
#: have somewhere to turn red.
KNOWN_DEVIATING_LEDGER_KINDS: frozenset[str] = frozenset()


def test_no_kind_other_than_code_created_deviates_from_its_published_payload(
    sim_run: Any,
) -> None:
    """Guards the payload contract that ``contracts.ledger`` publishes and nobody enforces.

    Any kind drifting from its published body is new, and this turns red for it — and every
    kind on the known list must STILL be drifting, so a repair upstream turns this red too
    and forces the entry out.

    **T-265 — why this assertion changed, and why the change is a strengthening.** It used
    to read::

        assert deviating <= {"code_created"}

    ``<=`` *permits* ``code_created`` to deviate. It encoded a contract violation as
    acceptable and supplied no case that exercised it, so the guard returned the same verdict
    whether T-235 was live or repaired — and could never be cited as a guard for the defect
    it named. Its own docstring said as much and called it the point: "It also stays green
    when ``code_created`` is repaired." That is precisely the blindness. Measured on this
    branch, ``code_created`` had ALREADY been repaired (``run.ledger_contract_problems``
    reported zero deviating kinds at HEAD) and this test noticed nothing, in either
    direction, for however long that had been true.

    Would the old assertion still be wrong if the T-242 runner change were reverted? Yes —
    ``set() <= {"code_created"}`` passes at HEAD and would pass identically with
    ``code_created`` malformed. ``test_repro_open_tickets.py``'s
    ``test_t265_the_confinement_guard_notices_its_whitelisted_kind_deviating`` demonstrates
    that against the un-fixed tree.

    The replacement asserts the exact set. It is strictly stronger than ``<=`` in both
    directions: a new deviation reds it, AND a repaired one reds it, so no entry can outlive
    its subject the way ``code_created``'s did.
    """
    deviating = {kind for kind, _ in sim_run.ledger_contract_problems}
    assert deviating == KNOWN_DEVIATING_LEDGER_KINDS, (
        "the set of ledger kinds deviating from their published payload shape is not the "
        f"known one. Newly deviating: {sorted(deviating - KNOWN_DEVIATING_LEDGER_KINDS)}. "
        f"No longer deviating: {sorted(KNOWN_DEVIATING_LEDGER_KINDS - deviating)} — if that "
        "second list is non-empty the deviation was REPAIRED upstream and its entry must be "
        "deleted from KNOWN_DEVIATING_LEDGER_KINDS, which is the whole reason this is an "
        "exact set rather than an allowlist (T-265)."
    )


def test_an_off_domain_checkout_url_is_refused_before_a_code_is_minted() -> None:
    """D22/C10, driven straight at ``exchange.accept``: the host must match exactly.

    The simulation's own stores are all on-domain, so the negative control is asserted here
    rather than inferred from a run in which nobody tried it.
    """
    from exchange.accept import accept
    from exchange.checkout.sellers import StaticRegisteredDomains

    now = 1_700_000_000.0
    domains = StaticRegisteredDomains({"store-x": "store-x.example.com"})

    def auction(host: str) -> dict[str, Any]:
        return {
            "auction_id": "sim-spoof",
            "intent_id": "sim-spoof",
            "cluster_id": "coffee",
            "bids": [
                {
                    "bid_id": "bid-x",
                    "store_id": "store-x",
                    "store_domain": "store-x.example.com",
                    "offer": {
                        "product_ref": "p-1",
                        "unit_price": 10.0,
                        "total_price": 10.0,
                        "checkout_url": f"https://{host}/cart/1:1",
                        "expires_at": now + 600,
                    },
                }
            ],
            "accepted_bid_ref": None,
            "now": now,
        }

    honest = accept(
        auction("store-x.example.com"), "bid-x", None, "redirect", registered_domains=domains
    )
    assert honest.accepted, f"the on-domain positive control was refused: {honest.denial_reason}"

    for host in (
        "evil.example.com",
        "store-x.example.com.evil.example.com",
        "store-x.example.com:8443@evil.example.com",
    ):
        refused = accept(auction(host), "bid-x", None, "redirect", registered_domains=domains)
        assert not refused.accepted, f"an off-domain checkout to {host!r} was accepted"
        assert refused.denial_reason


# ------------------------------------------------------------------------------------
# The headless entry point
# ------------------------------------------------------------------------------------
def test_the_cli_runs_headless_and_its_exit_status_reports_the_catch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Acceptance 3. Exit 0 means "caught"; a run that never caught the store must not."""
    from sim.__main__ import main

    assert main(["--episodes", "2"]) == 0
    captured = capsys.readouterr().out
    assert "blacklist threshold" in captured


def test_the_cli_json_output_is_byte_identical_across_two_runs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shell-visible half of acceptance 1."""
    from sim.__main__ import main

    main(["--episodes", "2", "--json"])
    first = capsys.readouterr().out
    main(["--episodes", "2", "--json"])
    second = capsys.readouterr().out
    assert first == second
    assert json.loads(first)["episodes"]
