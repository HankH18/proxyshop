"""R14's loop, closed by a population of returning shoppers — and the ways it could be fake.

Three groups, and the third is the one that decides whether the first two mean anything:

1. **the population's decisions** — pure, no services. Does an answer track the outcome, and do
   the two knobs (response rate, noise) do what their names say?
2. **the served loop** — two real ASGI servers on loopback. Does the answer travel through
   ``POST /buyer/feedback``, become a ledger event, and move ``GET /snapshot``?
3. **the falsifications** — could this suite pass if the population were decorative? The
   inverted-noise run is the important one: with every answer flipped, the postures must
   separate in the OPPOSITE direction. A demonstration that survives that is a demonstration
   that the answers are what moved the store, rather than the store's identity, its order
   volume, or anything else the run happens to correlate with.

Offline (D3/C9): every URL here is a loopback port the test itself bound, which is what the
repository's own socket guard permits (``--allow-hosts=127.0.0.1,localhost,::1``). Nothing
reaches a network.
"""

from __future__ import annotations

from typing import Any

import pytest

# ------------------------------------------------------------------------------------
# 1. The population's decisions
# ------------------------------------------------------------------------------------
GOOD_ORDER = {
    "fulfilment_missing": False,
    "price_comparable": True,
    "price_honored": True,
    "discount_comparable": True,
    "discount_honored": True,
    "delivery_comparable": True,
    "shipped_on_time": True,
}


def _verdict(**overrides: Any) -> dict[str, Any]:
    return {**GOOD_ORDER, **overrides}


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (_verdict(), "yes_as_described"),
        (_verdict(fulfilment_missing=True), "never_arrived"),
        (_verdict(price_honored=False), "not_as_described"),
        (_verdict(discount_honored=False), "not_as_described"),
        (_verdict(shipped_on_time=False), "as_described_but_late"),
        # Both broken: the money complaint outranks lateness, because `matched_pitch` is about
        # the pitch and `shipped_on_time` is a separate dimension graded from fulfilment stamps.
        (_verdict(price_honored=False, shipped_on_time=False), "not_as_described"),
        # A verdict of False on an INCOMPARABLE field means "the record did not say", never
        # "the store broke its promise". A shopper who complained here would be complaining
        # about a malformed webhook.
        (_verdict(price_honored=False, price_comparable=False), "yes_as_described"),
        (_verdict(shipped_on_time=False, delivery_comparable=False), "yes_as_described"),
        (_verdict(discount_honored=False, discount_comparable=False), "yes_as_described"),
    ],
)
def test_the_answer_is_derived_from_the_reconciled_verdict(
    verdict: dict[str, Any], expected: str
) -> None:
    """The whole reason this population is not decorative: the answer tracks the measurement."""
    from seed.shoppers import outcome_choice

    assert outcome_choice(verdict) == expected


def test_the_population_never_invents_wrong_item() -> None:
    """Nothing in this system observes what was in the box, so nothing here may claim it did.

    ``product_ref`` on a reconciled verdict is carried from the accepted OFFER; the webhook
    never reports what shipped. A population that emitted ``wrong_item`` would be manufacturing
    a fact no component checked — which is the exact failure mode this ticket exists to avoid.
    """
    from seed.shoppers import outcome_choice

    choices = {
        outcome_choice(_verdict(**{field: False}))
        for field in ("price_honored", "discount_honored", "shipped_on_time")
    }
    choices.add(outcome_choice(_verdict()))
    choices.add(outcome_choice(_verdict(fulfilment_missing=True)))
    assert "wrong_item" not in choices


def _prompt() -> dict[str, Any]:
    """The prompt shape ``POST /buyer/feedback/prompt`` serves."""
    from buyer_svc.feedback.prompt import FEEDBACK_CHOICES, PROMPT_QUESTION_ID

    return {
        "question_id": PROMPT_QUESTION_ID,
        "options": [choice.to_dict() for choice in FEEDBACK_CHOICES],
    }


def test_zero_noise_encodes_the_outcome_exactly() -> None:
    from seed.shoppers import ShopperPolicy, answer_for

    policy = ShopperPolicy(response_rate=1.0, noise_rate=0.0)
    for index in range(200):
        verdict = _verdict(price_honored=index % 3 == 0)
        answer = answer_for(_prompt(), verdict, policy, 7, f"sim-fb-{index}")
        assert answer.responded
        assert not answer.noisy
        assert answer.choice == answer.outcome_choice


def test_full_noise_inverts_every_answer_and_always_flips_matched_pitch() -> None:
    """Noise the trust engine cannot see is not noise; it is decoration.

    A flipped answer has to change ``matched_pitch``, because that boolean is the only thing
    ``trust.feedback.engine`` reads. This pins that ``as_described_but_late`` is treated as a
    POSITIVE (its own option label says the pitch was matched), so a harsh shopper flips it to a
    negative rather than to another positive.
    """
    from buyer_svc.feedback.prompt import CHOICES_BY_ID
    from seed.shoppers import ShopperPolicy, answer_for

    policy = ShopperPolicy(response_rate=1.0, noise_rate=1.0)
    seen = set()
    for index in range(200):
        verdict = _verdict(price_honored=index % 2 == 0, shipped_on_time=index % 3 == 0)
        answer = answer_for(_prompt(), verdict, policy, 11, f"sim-fb-{index}")
        assert answer.noisy
        assert answer.choice != answer.outcome_choice
        assert (
            CHOICES_BY_ID[str(answer.choice)].matched_pitch
            is not CHOICES_BY_ID[answer.outcome_choice].matched_pitch
        )
        seen.add((answer.outcome_choice, answer.choice))
    assert len(seen) > 1, "the probe never produced two different outcomes to flip"


def test_the_response_rate_is_what_decides_who_answers_at_all() -> None:
    from seed.shoppers import ShopperPolicy, answer_for

    refs = [f"sim-fb-{index}" for index in range(400)]
    silent = ShopperPolicy(response_rate=0.0, noise_rate=0.5)
    always = ShopperPolicy(response_rate=1.0, noise_rate=0.0)
    assert not any(answer_for(_prompt(), _verdict(), silent, 3, r).responded for r in refs)
    assert all(answer_for(_prompt(), _verdict(), always, 3, r).responded for r in refs)

    middle = ShopperPolicy(response_rate=0.55, noise_rate=0.15)
    answered = [answer_for(_prompt(), _verdict(), middle, 3, r) for r in refs]
    rate = sum(1 for a in answered if a.responded) / len(refs)
    assert 0.45 < rate < 0.65, f"the response draw is biased: {rate}"


def test_a_silent_shopper_writes_nothing_rather_than_a_positive() -> None:
    """Silence is not a good review, and this is the assertion that says so."""
    from seed.shoppers import ShopperPolicy, answer_for

    answer = answer_for(_prompt(), _verdict(), ShopperPolicy(response_rate=0.0), 3, "sim-fb-1")
    assert answer.responded is False
    assert answer.choice is None


def test_noise_is_symmetric_so_it_cannot_bias_a_store() -> None:
    """The same rate in both directions: a harsh answer about a good order, and the mirror."""
    from seed.shoppers import ShopperPolicy, answer_for

    policy = ShopperPolicy(response_rate=1.0, noise_rate=0.15)
    refs = [f"sim-fb-{index}" for index in range(600)]
    good = [answer_for(_prompt(), _verdict(), policy, 5, r) for r in refs]
    bad = [answer_for(_prompt(), _verdict(price_honored=False), policy, 5, r) for r in refs]
    harsh = sum(1 for a in good if a.noisy) / len(refs)
    forgiving = sum(1 for a in bad if a.noisy) / len(refs)
    assert abs(harsh - forgiving) < 1e-9, "the two directions drew from different streams"
    assert 0.10 < harsh < 0.21, f"the noise draw is biased: {harsh}"


@pytest.mark.parametrize("bad", [-0.1, 1.1, "0.5", None, True])
def test_a_policy_nobody_could_defend_is_refused(bad: Any) -> None:
    from seed.shoppers import ShopperPolicy, ShopperPolicyError

    with pytest.raises(ShopperPolicyError):
        ShopperPolicy(response_rate=bad)
    with pytest.raises(ShopperPolicyError):
        ShopperPolicy(noise_rate=bad)


def test_the_answer_is_checked_against_the_prompt_the_service_really_served() -> None:
    """A second copy of the option table here would be the drift D30 exists to prevent."""
    from seed.shoppers import ShopperPolicy, ShopperPolicyError, answer_for

    trimmed = {"question_id": "matched_pitch", "options": [{"id": "yes_as_described"}]}
    with pytest.raises(ShopperPolicyError):
        answer_for(trimmed, _verdict(price_honored=False), ShopperPolicy(1.0, 0.0), 1, "sim-fb-1")


def test_the_dispatch_promise_is_read_out_of_the_approved_persona(
    pop_manifest: dict[str, Any],
) -> None:
    """ "ships within 24 hours" is one day and "2 business days" is two. Neither is guessed."""
    from seed.shoppers import DEFAULT_PROMISED_DISPATCH_DAYS, promised_dispatch_days

    assert promised_dispatch_days(pop_manifest, "store-brightbean") == pytest.approx(1.0)
    assert promised_dispatch_days(pop_manifest, "store-northroast") == pytest.approx(2.0)
    assert promised_dispatch_days(pop_manifest, "store-harborline") == (
        DEFAULT_PROMISED_DISPATCH_DAYS
    )


def test_an_unparseable_promise_raises_rather_than_becoming_the_default() -> None:
    """A store graded against a promise no human approved is a store graded against nothing."""
    from seed.shoppers import ShopperPolicyError, promised_dispatch_days

    manifest = {
        "personas": {
            "vague": {
                "store_id": "store-x",
                "scripted_claims": [{"claim_type": "dispatch_window", "value": "ships soon"}],
            }
        }
    }
    with pytest.raises(ShopperPolicyError):
        promised_dispatch_days(manifest, "store-x")


def test_the_delivery_profile_is_the_manifests_own_honest_flag(
    pop_roster: list[dict[str, Any]],
) -> None:
    """A3: the simulator does not get to choose which store delivers badly."""
    from seed.shoppers import KEEPS_PROMISES, OVER_PROMISES, delivery_profile

    by_id = {str(row["store_id"]): row for row in pop_roster}
    assert delivery_profile(by_id["store-brightbean"]) is OVER_PROMISES
    assert delivery_profile(by_id["store-northroast"]) is KEEPS_PROMISES


def test_a_breach_charges_more_and_ships_later_than_the_promise() -> None:
    """One breach decision per order, applied to every graded field of it."""
    from seed.shoppers import KEEPS_PROMISES, OVER_PROMISES, delivered_outcome

    promise = {"total_price": 100.0, "delivery_estimate_days": 2.0, "discount_percentage": 20.0}
    breaches = [delivered_outcome(OVER_PROMISES, promise, 4, f"sim-fb-{i}") for i in range(200)]
    kept = [delivered_outcome(KEEPS_PROMISES, promise, 4, f"sim-fb-{i}") for i in range(200)]
    assert 0.70 < sum(1 for row in breaches if row["breached"]) / len(breaches) < 0.90
    assert sum(1 for row in kept if row["breached"]) / len(kept) < 0.15
    for row in breaches:
        if row["breached"]:
            assert row["charged_total"] > promise["total_price"]
            assert row["dispatch_days"] > promise["delivery_estimate_days"]
            assert float(row["applied_discount_percentage"]) < promise["discount_percentage"]
        else:
            assert row["charged_total"] == pytest.approx(promise["total_price"])
            assert row["dispatch_days"] <= promise["delivery_estimate_days"]


# ------------------------------------------------------------------------------------
# 2. Where this may point
# ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000", "http://localhost:9", "https://127.0.0.5/", "http://[::1]:1"]
)
def test_loopback_is_admitted(url: str) -> None:
    from seed.targets import require_local_target

    assert require_local_target(url, what="probe")


@pytest.mark.parametrize(
    "url",
    [
        "http://trust.example.com",
        "http://10.0.0.1:8084",
        # The classic: a hostname that merely BEGINS with a loopback literal.
        "http://127.0.0.1.evil.example.com",
        "http://example.com@127.0.0.1",
    ],
)
def test_a_non_loopback_target_is_refused_without_the_explicit_flag(url: str) -> None:
    """This tool manufactures trust signal onto a ledger with no delete. The default is local."""
    from seed.targets import UnsafeTarget, require_local_target

    with pytest.raises(UnsafeTarget):
        require_local_target(url, what="probe")


def test_the_flag_is_what_permits_a_remote_target_and_nothing_else() -> None:
    from seed.targets import require_local_target

    assert require_local_target("http://trust.example.com", allow_remote=True, what="probe")


@pytest.mark.parametrize(
    "url", ["", "   ", "file:///etc/passwd", "unix:/tmp/s.sock", "http://user:pw@127.0.0.1"]
)
def test_an_unusable_target_is_refused_outright(url: str) -> None:
    from seed.targets import UnsafeTarget, require_local_target

    with pytest.raises(UnsafeTarget):
        require_local_target(url, what="probe")


def test_the_guard_runs_inside_run_population_not_only_in_the_cli(
    pop_manifest: dict[str, Any],
) -> None:
    """An in-process caller gets the same refusal an operator does, before any request."""
    from seed.population import run_population
    from seed.targets import UnsafeTarget

    class Exploding:
        def get(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a request was made to a refused target")

        post = get

    with pytest.raises(UnsafeTarget):
        run_population(
            pop_manifest,
            1,
            buyer_url="http://buyer.example.com",
            trust_url="http://127.0.0.1:1",
            client=Exploding(),
        )


# ------------------------------------------------------------------------------------
# 3. The served loop
# ------------------------------------------------------------------------------------
def test_every_routed_order_was_offered_a_prompt_and_every_answer_was_recorded(
    pop_served: Any,
) -> None:
    """R14 through the real doors: 200 with a prompt, then 201 with the ledger event."""
    run, _services = pop_served
    accepted = [purchase for purchase in run.purchases if purchase.accepted]
    assert accepted, "the population never bought anything, so no prompt was ever offered"
    for purchase in accepted:
        assert purchase.prompt_status == 200
        assert purchase.prompt_offered
        if purchase.responded:
            assert purchase.submit_status == 201


def test_what_the_ledger_recorded_is_what_the_shopper_chose(pop_served: Any) -> None:
    """The published verdict for the chosen option, not this package's opinion of it."""
    from buyer_svc.feedback.prompt import CHOICES_BY_ID

    run, _services = pop_served
    answered = [p for p in run.purchases if p.responded]
    assert answered
    for purchase in answered:
        expected = CHOICES_BY_ID[str(purchase.choice)].matched_pitch
        assert purchase.recorded_matched_pitch is expected
        assert purchase.recorded_reason == purchase.choice


def test_the_answers_really_left_the_buyer_process_and_landed_on_the_chain(
    pop_served: Any,
) -> None:
    """Not a double someone handed the app: the bytes are read back off the trust chain.

    ``local_stack`` binds the buyer service's real composition root to the trust service's
    published ``POST /events``, so a ``feedback`` row in this chain is a row that travelled
    over a socket out of one process and into the other.
    """
    run, services = pop_served
    chain = [dict(event) for event in services.event_store.read()]
    feedback = [event for event in chain if str(event.get("kind")) == "feedback"]
    answered = [p for p in run.purchases if p.responded]
    assert len(feedback) == len(answered)
    assert {str(e["order_ref"]) for e in feedback} == {p.order_ref for p in answered}
    assert services.event_store.verify()["ok"], "the ledger chain did not verify"


def test_every_recorded_answer_is_labelled_simulated(pop_served: Any) -> None:
    """Rule 3. A posture built partly from synthetic feedback must be inspectable as such."""
    from seed.shoppers import SIMULATED_ORDER_PREFIX, is_simulated_reference

    run, services = pop_served
    chain = [dict(event) for event in services.event_store.read()]
    feedback = [event for event in chain if str(event.get("kind")) == "feedback"]
    assert feedback
    for event in feedback:
        assert is_simulated_reference(event["order_ref"]), event["order_ref"]
        assert str(event["order_ref"]).startswith(SIMULATED_ORDER_PREFIX)
    for purchase in run.purchases:
        assert is_simulated_reference(purchase.order_ref)
    assert run.to_json()["simulated"] is True
    assert all(row["simulated"] is True for row in run.to_json()["purchases"])


def test_an_unlabelled_submission_is_refused_before_it_is_sent(pop_served: Any) -> None:
    """The label is not decoration on the report; it is a precondition of the write."""
    from seed.population import PopulationError, _Services

    _run, services = pop_served
    door = _Services("http://127.0.0.1:1", "http://127.0.0.1:1", object())
    with pytest.raises(PopulationError, match="prefix"):
        door.submit({"order_ref": "ord-real-1001"}, {"choice": "yes_as_described"})


def test_only_feedback_match_moved(pop_served: Any) -> None:
    """The separation this run reports is FEEDBACK's, and this is what makes that checkable.

    The reconciled verdicts are computed and never posted (see :mod:`seed.population`), so if any
    other dimension moved, something in this run is writing observations it did not declare.
    """
    from seed.population import posture_view

    run, _services = pop_served
    before = posture_view(run.opening_snapshot)
    after = posture_view(run.closing_snapshot)
    moved = {
        (store_id, dim)
        for store_id, entry in after.items()
        for dim, state in entry["dims"].items()
        if before.get(store_id, {}).get("dims", {}).get(dim) != state
    }
    assert moved, "no dimension moved at all, so the population wrote nothing"
    assert {dim for _store, dim in moved} == {"feedback_match"}


def test_the_over_promising_store_ends_below_the_approved_control(
    pop_served: Any, pop_manifest: dict[str, Any]
) -> None:
    """The sentence the whole feature exists to make true, on the door the exchange reads."""
    from seed.population import control_pair

    run, _services = pop_served
    bad, good = control_pair(run, pop_manifest)
    bad_score = float(run.closing_snapshot[bad]["score"])
    good_score = float(run.closing_snapshot[good]["score"])
    assert good_score > bad_score, (
        f"{bad} pitched well and delivered badly and was scored {bad_score:.4f}; the approved "
        f"control {good} kept its promises and was scored {good_score:.4f}"
    )
    assert bad_score < float(run.opening_snapshot[bad]["score"]), (
        "the over-promising store's posture did not fall at all"
    )


def test_the_real_ranker_puts_the_control_above_it(
    pop_served: Any, pop_manifest: dict[str, Any]
) -> None:
    """Two offers identical except for the store, ranked by ``exchange.ranking.rank``.

    The only input that differs is the served trust posture, so the reorder is caused by the
    feedback and by nothing else about the offers.
    """
    from seed.population import _episode_epoch, control_pair, ranking_probe

    run, _services = pop_served
    bad, good = control_pair(run, pop_manifest)
    now = _episode_epoch(pop_manifest, 1)
    after = ranking_probe(run.closing_snapshot, good, bad, now=now)
    assert after["order"][0] == good, after
    assert after["scores"][good] > after["scores"][bad]


def test_a_store_that_never_answers_a_solicitation_accrues_no_feedback(pop_served: Any) -> None:
    """R10's timeout store is on the roster, is eligible, and simply never transacts."""
    run, _services = pop_served
    assert "store-slowreply" not in {p.store_id for p in run.purchases}
    before = float(run.opening_snapshot["store-slowreply"]["score"])
    after = float(run.closing_snapshot["store-slowreply"]["score"])
    assert before == pytest.approx(after, abs=1e-6)


def test_the_minted_secrets_are_well_formed_and_never_repeat(pop_served: Any) -> None:
    """A reproducible discount code would be a guessable one (D22), so these are not compared."""
    from seed.population import minted_values

    run, _services = pop_served
    minted = minted_values(run)
    assert minted["codes"], "the run minted no discount codes at all"
    assert minted["unique_codes"], "a discount code was minted twice"
    assert all(code.startswith("PSX-") for code in minted["codes"])
    assert minted["event_ids"], "no feedback event id came back from the route"
    assert minted["unique_event_ids"], "two feedback submissions share an event id"


# ------------------------------------------------------------------------------------
# 4. Determinism
# ------------------------------------------------------------------------------------
def test_two_runs_of_one_seed_are_byte_identical(pop_run_factory: Any) -> None:
    """S4 / C9. Two whole populations, two fresh stacks, one digest."""
    from seed.population import transcript, transcript_digest

    first, _ = pop_run_factory(episodes=2)
    second, _ = pop_run_factory(episodes=2)
    assert transcript_digest(first) == transcript_digest(second)
    assert transcript(first) == transcript(second)


def test_a_different_seed_produces_a_different_population(pop_run_factory: Any) -> None:
    """Determinism that is really just "the run ignores its seed" would pass the test above."""
    from seed.population import transcript_digest

    first, _ = pop_run_factory(episodes=2, seed=20260901)
    second, _ = pop_run_factory(episodes=2, seed=20260902)
    assert transcript_digest(first) != transcript_digest(second)


# ------------------------------------------------------------------------------------
# 5. The falsifications
# ------------------------------------------------------------------------------------
def test_inverting_every_answer_reverses_the_separation(
    pop_run_factory: Any, pop_manifest: dict[str, Any]
) -> None:
    """The load-bearing negative control.

    With ``noise_rate=1.0`` every shopper says the OPPOSITE of what happened to their order:
    the store that over-promised is praised and the stores that delivered are complained about.
    If the postures still separated the same way, the separation would be caused by something
    other than what the shoppers said — the store's identity, its order volume, the order of
    the roster — and the whole demonstration would be decorative. They must reverse.
    """
    from seed.population import control_pair

    run, _services = pop_run_factory(response_rate=1.0, noise_rate=1.0)
    bad, good = control_pair(run, pop_manifest)
    assert float(run.closing_snapshot[bad]["score"]) > float(run.closing_snapshot[good]["score"])


def test_a_population_that_never_answers_moves_nothing(
    pop_run_factory: Any, pop_manifest: dict[str, Any]
) -> None:
    """Silence writes no observation, so every posture stays exactly where it started."""
    from seed.population import posture_view

    run, services = pop_run_factory(response_rate=0.0)
    assert not [p for p in run.purchases if p.responded]
    assert not [e for e in services.event_store.read() if str(e.get("kind")) == "feedback"]
    assert posture_view(run.opening_snapshot) == posture_view(run.closing_snapshot)


def test_the_run_reports_failure_rather_than_success_when_nothing_separates(
    pop_run_factory: Any, pop_manifest: dict[str, Any]
) -> None:
    """A population that proved nothing must not exit 0. This is that exit status."""
    from seed.population import _separated

    silent, _ = pop_run_factory(response_rate=0.0)
    assert _separated(silent, pop_manifest) is False


# ------------------------------------------------------------------------------------
# 6. What the served path grades — the dispatch promise, end to end
# ------------------------------------------------------------------------------------
#
# THE DEFECT THESE TWO WERE WRITTEN AGAINST, now closed:
#
#   `apps/exchange/src/checkout/provider.py` projected the accepted offer down to exactly
#   {product_ref, unit_price, total_price, discount} when it wrote the `accepted` ledger
#   event. `Offer.delivery_estimate_days` — a published, optional field of the protocol
#   offer, which the bids this population sends DO carry — was dropped there.
#
#   `trust.reconcile.reconciled_event` reads the delivery promise off precisely that event
#   (`_promised(accepted)`), so on the served checkout path it always saw
#   `promised_delivery_days: null`, hence `delivery_comparable: false`, hence
#   `shipped_on_time: false` for every order no matter when it actually shipped. One of the
#   six trust dimensions was ungradeable end to end: a store could promise next-day dispatch,
#   be believed, take three weeks, and pay nothing for it.
#
# The projection now carries the field, and these two grade both halves of the repair — the
# promise reaching the ledger, and the reconciler returning a real verdict about it. They ran
# `xfail(strict=True)` until the fix landed and are plain tests now. The `_is_armed` control
# beside them stays: it fails in its own name if the population ever stops sending a dispatch
# promise, which would make the two below measure the absence of a field nobody stated.
def _accepted_offer_probe(manifest: dict[str, Any], roster: list[dict[str, Any]]) -> dict[str, Any]:
    """Drive the real accept path with an offer that states a dispatch promise."""
    from exchange.accept import accept
    from exchange.checkout.sellers import StaticRegisteredDomains
    from seed.population import (
        DEFAULT_MAX_DISCOUNT_PCT,
        EPISODE_SECONDS,
        _episode_epoch,
        _PopulationSolicitor,
    )

    rows = [dict(row, max_discount_pct=DEFAULT_MAX_DISCOUNT_PCT) for row in roster]
    store = rows[0]
    store_id = str(store["store_id"])
    now = _episode_epoch(manifest, 1)
    domains = StaticRegisteredDomains({str(r["store_id"]): str(r["store_domain"]) for r in rows})
    bid = _PopulationSolicitor(
        auction_id="probe-auction",
        pitched_prices={store_id: 20.0},
        silent=frozenset(),
        expires_at=now + EPISODE_SECONDS,
        promised_days={store_id: 2.0},
        observed_at="2026-09-01T00:00:00Z",
    ).solicit(store)
    assert bid is not None
    offer = dict(bid["bid"]["offer"])
    record = {
        "auction_id": "probe-auction",
        "intent_id": "probe-auction",
        "cluster_id": "coffee",
        "bids": [
            {
                "bid_id": "probe-bid",
                "store_id": store_id,
                "store_domain": domains.domain_for(store_id),
                "offer": offer,
            }
        ],
        "accepted_bid_ref": None,
        "now": now,
    }
    outcome = accept(record, "probe-bid", None, "redirect", registered_domains=domains)
    return {
        "sent_offer": offer,
        "accepted": bool(outcome.accepted),
        "events": [dict(event) for event in outcome.events],
    }


def test_the_delivery_promise_probe_is_armed(
    pop_manifest: dict[str, Any], pop_roster: list[dict[str, Any]]
) -> None:
    """The graded test below measures nothing unless all three of these hold."""
    probe = _accepted_offer_probe(pop_manifest, pop_roster)
    assert probe["sent_offer"].get("delivery_estimate_days") == pytest.approx(2.0), (
        "the bid this population sends carries no dispatch promise, so the gate below would "
        "be measuring the absence of a field nobody stated"
    )
    assert probe["accepted"], "the probe's acceptance was refused, so no `accepted` event exists"
    assert [e for e in probe["events"] if str(e.get("kind")) == "accepted"]


def test_the_accept_path_carries_the_delivery_promise_onto_the_ledger(
    pop_manifest: dict[str, Any], pop_roster: list[dict[str, Any]]
) -> None:
    """A dispatch promise the buyer was shown must reach the record that grades it."""
    probe = _accepted_offer_probe(pop_manifest, pop_roster)
    accepted = next(e for e in probe["events"] if str(e.get("kind")) == "accepted")
    assert accepted["payload"]["offer"].get("delivery_estimate_days") == pytest.approx(2.0)


def test_the_reconciler_can_grade_a_dispatch_promise_the_market_really_made(
    pop_manifest: dict[str, Any], pop_roster: list[dict[str, Any]]
) -> None:
    """End to end: a real acceptance, a real late fulfilment, and a gradeable verdict."""
    from seed.population import _merchant_events
    from trust.reconcile import reconcile

    probe = _accepted_offer_probe(pop_manifest, pop_roster)
    events = probe["events"]
    store_id = str(events[0]["store_id"])
    merchant = _merchant_events(
        manifest=pop_manifest,
        episode=1,
        store_id=store_id,
        order_ref="sim-fb-probe-late",
        accept_events=events,
        delivered={
            "charged_total": float(probe["sent_offer"]["total_price"]),
            "applied_discount_percentage": float(probe["sent_offer"]["discount"]["value"]),
            "dispatch_days": 21.0,
            "breached": True,
        },
    )
    verdict = reconcile([*events, *merchant])[0]["payload"]
    assert verdict["delivery_comparable"] is True
    assert verdict["shipped_on_time"] is False


def test_the_run_grades_dispatch_promises_rather_than_reporting_a_gap(
    pop_served: Any,
) -> None:
    """A population that quietly never says 'late' looks like a market where nothing is late.

    This assertion used to run the other way round: no order in a whole run had a gradeable
    dispatch promise, so ``shipped_on_time`` was False everywhere regardless of when anything
    shipped, and the run printed ``DELIVERY_UNGRADEABLE_NOTICE`` to name the gap rather than
    letting it read as a market where nothing is ever late. The cause was in ``apps/exchange``
    and is closed: the ``accepted`` event now carries ``delivery_estimate_days``.

    So the property is stated the way it should always have been — the run measures dispatch,
    and a store that blew its own window is heard about — and it is asserted on the run's own
    data rather than on the notice's text, which is why it inverted rather than had to be
    rewritten from scratch. The notice's own print is conditional on this same emptiness, so
    a regression re-arms it instead of leaving the gap silent.
    """
    run, _services = pop_served
    accepted = [purchase for purchase in run.purchases if purchase.accepted]
    assert accepted
    gradeable = [purchase for purchase in accepted if purchase.delivery_comparable]
    assert gradeable, (
        "not one order in this run had a gradeable dispatch promise, so `shipped_on_time` is "
        "False for every order regardless of when it shipped — the accepted event has stopped "
        "carrying `delivery_estimate_days` again"
    )
    assert any(p.shipped_on_time for p in gradeable), "no order in this run graded as shipped"
    assert any(not p.shipped_on_time for p in gradeable), (
        "every order in this run graded as shipped on time, so `shipped_on_time` could still be "
        "a constant rather than a measurement"
    )
    # And the verdict tracks the merchant's real behaviour rather than merely varying: one
    # hour of tolerance, the same `DELIVERY_TOLERANCE_DAYS` the reconciler grades with.
    for purchase in gradeable:
        assert purchase.shipped_on_time == (
            purchase.dispatch_days <= purchase.promised_delivery_days + 1.0 / 24.0
        ), (
            f"{purchase.order_ref} promised {purchase.promised_delivery_days}d, dispatched in "
            f"{purchase.dispatch_days}d, and graded shipped_on_time={purchase.shipped_on_time}"
        )
