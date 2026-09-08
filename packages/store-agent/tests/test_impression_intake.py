"""The last link of R17's loop: the agent RECEIVES an impression attribution and learns from it.

``trust.feedback.attribution.impression_for`` joins a store's own ledger rows on ``auction_id``
and says which of the store's decisions earned a trust movement — the rung it quoted, whether the
offer was ``shown``, whether it ``converted``. ``trust.feedback.engine._wire_event`` parks that
record in the pushed event's payload under ``trust_attribution``, the store agent answered
``200``, the trust service logged ``delivered``, and **nothing read it**: the runner folded
``payload.delta`` into a per-dimension aggregate and credited the arm it played by that delta's
SIGN.

WHY THE SIGN IS THE WRONG COORDINATE, MEASURED HERE
-----------------------------------------------------
The two disagree, and this file's crux —
:func:`test_shown_and_not_converted_is_a_loss_and_the_same_delta_alone_is_a_win` — drives the
disagreement through the door. **One positive delta, two readings**: with the attribution saying
``shown: true, converted: false`` the rung's posterior goes DOWN, and with the attribution
stripped off the very same payload it goes UP. A store's reputation moving up and a store's offer
failing to close are different facts, and only the second is what
:func:`~store_agent.learning.state.sample_depth` is a posterior over.

The consequence is the one that matters: under the sign alone a rung accumulates a win whenever
the platform said something nice and a loss whenever it said something unkind, and never "this
arm was in front of a shopper and did not close". A posterior fed only wins is a counter.

WHAT IS GRADED HERE, AND HOW
------------------------------
Every number below is read off an HTTP response body from one of the two doors this agent
publishes. Nothing is injected into a state object; the teaching loop answers a solicitation at
``POST /v1/bid-requests``, reads the arm off the served bid, and pushes the outcome at
``POST /v1/trust-events`` with an attribution built by ``impression_for`` itself — the real
producer, from real ledger rows, for the reason ``test_trust_intake`` gives about T-259.

The per-rung TALLIES are read back off ``agent_runner(app).learning`` AFTER the responses, never
before and never instead: "the rung nothing happened on did not move" is a statement about a
Beta posterior, and the served distribution is its pushforward rather than the thing itself. That
is the same arrangement ``test_trust_intake`` uses for the posture.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest
from fastapi.testclient import TestClient
from store_agent.learning import (
    ATTRIBUTION_KEY,
    ATTRIBUTION_SOURCE,
    DEFAULT_DEPTH_BUCKETS,
    OUTCOME_SOURCE,
    PRIOR_LOSSES,
    PRIOR_WINS,
)
from store_agent.main import create_app
from store_agent.modes import (
    CREDIT_ALREADY,
    CREDIT_DELTA_SIGN,
    CREDIT_IMPRESSION,
    CREDIT_UNREMEMBERED,
)
from store_agent.solicitation import configure_solicitation
from store_agent.trust_intake import agent_runner
from trust.feedback import TRUST_ATTRIBUTION_KEY, trust_event_payload
from trust.feedback.attribution import ATTRIBUTION_SCHEMA_VERSION, impression_for

from .test_solicitation import CLUSTER, STORE_ID, context, request_body

BID_ROUTE = "/v1/bid-requests"
TRUST_ROUTE = "/v1/trust-events"

#: How many auctions each measurement window runs. The same size ``test_learning_loop`` uses, for
#: the same reason: a distribution over five rungs needs more than a handful of draws.
WINDOW = 60

#: The rung the store is taught to CONVERT at, as a percent. Exactly the deepest rung of
#: ``DEFAULT_DEPTH_BUCKETS`` and exactly the ``store-alpha`` envelope's approved cap, so a bid at
#: it is authorizable and the shift is visible in the served price rather than only in a tally.
WIN_PCT = 20.0

#: The rung the store is taught to be SHOWN at and not close. A real rung, well inside the cap.
LOSS_PCT = 5.0

#: A rung nothing is ever said about. The control for "the posterior moved where the evidence
#: was, and nowhere else".
QUIET_PCT = 10.0


def _pct(depth: float) -> float:
    return round(depth * 100.0, 6)


# =============================================================================================
# helpers — the two served doors, and the real producer on the other side of the trust one
# =============================================================================================


def client(**overrides: Any) -> TestClient:
    app = create_app()
    configure_solicitation(app, context=context(**overrides))
    return TestClient(app)


def served_bid(api: TestClient, auction_id: str) -> dict[str, Any]:
    response = api.post(BID_ROUTE, json=request_body(auction_id=auction_id))
    assert response.status_code == 200, (
        f"{BID_ROUTE} answered {response.status_code} for {auction_id}: {response.text}"
    )
    body: dict[str, Any] = response.json()
    return body


def policy_action(body: dict[str, Any]) -> dict[str, Any]:
    actions = [c for c in body["claims"] if c["key"] == "policy_action"]
    assert len(actions) == 1, f"expected exactly one policy_action claim, got {len(actions)}"
    value = actions[0]["value"]
    assert isinstance(value, dict), f"policy_action value is not a mapping: {value!r}"
    return value


def served_rung(api: TestClient, auction_id: str) -> float:
    """The discount depth this agent PLAYS in one auction, as a percent, off the served bid."""
    return round(float(policy_action(served_bid(api, auction_id))["discount_pct"]), 6)


def rung_shares(api: TestClient, *, tag: str, window: int = WINDOW) -> Counter[float]:
    """The rung distribution across a fixed window of auction ids — the sampler's pushforward.

    The auction id IS the seed (:meth:`store_agent.modes.AgentRunner._select_arm`), so the same
    tag before and after teaching draws from the same seeds and the ONLY thing that can move the
    counts is the state.
    """
    return Counter(served_rung(api, f"auc-{tag}-{i:04d}") for i in range(window))


def ledger_rows(
    auction_id: str, *, pct: float | None, shown: bool, converted: bool
) -> list[dict[str, Any]]:
    """The store's own ledger rows for one auction, in the three kinds ``impression_for`` reads.

    ``bid_placed`` carries the arm, ``shown`` the impression, ``accepted`` the conversion — see
    that module's own table. Built here rather than mocked so the attribution under test is one
    the real join actually produces from a real chain.
    """
    offer: dict[str, Any] = {"product_ref": "prod-cap", "unit_price": 100.0, "currency": "USD"}
    if pct is not None:
        offer["discount"] = {"type": "percentage", "value": pct}
    rows = [
        {
            "kind": "bid_placed",
            "auction_id": auction_id,
            "store_id": STORE_ID,
            "payload": {"bid_ref": f"{auction_id}:{STORE_ID}", "offer": offer},
        }
    ]
    if shown:
        rows.append(
            {
                "kind": "shown",
                "auction_id": auction_id,
                "store_id": STORE_ID,
                "payload": {"bid_ref": f"{auction_id}:{STORE_ID}", "slot": "value"},
            }
        )
    if converted:
        rows.append(
            {
                "kind": "accepted",
                "auction_id": auction_id,
                "store_id": STORE_ID,
                "payload": {"bid_ref": f"{auction_id}:{STORE_ID}", "offer": offer},
            }
        )
    return rows


def ledger_event(auction_id: str, *, matched: bool) -> dict[str, Any]:
    """A ``feedback`` row — R14's dimension, and the kind that only exists for a routed buyer."""
    return {
        "event_id": f"evt-{auction_id}",
        "ts": "2026-01-02T00:00:00Z",
        "kind": "feedback",
        "auction_id": auction_id,
        "store_id": STORE_ID,
        "order_ref": f"ord-{auction_id}",
        "payload": {"dim": "feedback_match", "type": "verified", "matched_pitch": matched},
    }


def pushed_payload(
    auction_id: str,
    *,
    delta: float,
    pct: float | None = WIN_PCT,
    shown: bool = True,
    converted: bool = True,
    attribution: Any = ...,
) -> dict[str, Any]:
    """One ``TrustEventPayload`` as the trust service really emits it, attribution and all.

    ``attribution=...`` means "whatever ``impression_for`` computes"; pass a mapping to bend the
    record, or ``None`` to send the payload a build without the join would have sent.
    """
    event = ledger_event(auction_id, matched=delta > 0)
    if attribution is ...:
        attribution = impression_for(
            event, ledger_rows(auction_id, pct=pct, shown=shown, converted=converted)
        )
    return trust_event_payload(
        {
            "store_id": STORE_ID,
            "dim": "feedback_match",
            "delta": delta,
            "reason_code": "verified" if delta > 0 else "contradicted",
            "attribution": attribution,
            "event": event,
        }
    )


def push(api: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = api.post(TRUST_ROUTE, json=payload)
    assert response.status_code == 200, (
        f"{TRUST_ROUTE} answered {response.status_code}: {response.text}"
    )
    body: dict[str, Any] = response.json()
    return body


def teach(api: TestClient, auction_id: str, **kwargs: Any) -> dict[str, Any]:
    """Answer an auction at the served door, then push its outcome at the served trust door."""
    served_bid(api, auction_id)
    return push(api, pushed_payload(auction_id, **kwargs))


def tally(api: TestClient, pct: float) -> tuple[int, int]:
    """``(wins, losses)`` on one rung of this store's own learned state, read back off the app."""
    learned = agent_runner(api.app).learning.cluster(CLUSTER)
    if learned is None:
        return 0, 0
    for rung in learned.depths:
        if _pct(rung.depth) == pct:
            return rung.wins, rung.losses
    raise AssertionError(f"no rung at {pct}% in {[r.depth for r in learned.depths]}")


def posterior_mean(api: TestClient, pct: float) -> float:
    """The Beta(wins + 1, losses + 1) mean on one rung — what ``sample_depth`` draws from."""
    wins, losses = tally(api, pct)
    return (wins + PRIOR_WINS) / (wins + losses + PRIOR_WINS + PRIOR_LOSSES)


# =============================================================================================
# 1. arming — the fact the whole file rests on, and the two spellings that must agree
# =============================================================================================


def test_the_key_and_version_this_agent_reads_are_the_ones_the_trust_service_writes() -> None:
    """The join is a string in an open mapping on one side and a constant on the other.

    ``store_agent`` does not import ``trust`` and must not, so the address and the schema version
    are spelled independently in the two services. Independent spellings that are never compared
    are how a producer and a consumer stay green for weeks while the wire between them is dead —
    which is exactly the state this file's ticket found. This is the comparison.
    """
    assert ATTRIBUTION_KEY == TRUST_ATTRIBUTION_KEY, (
        f"the agent reads {ATTRIBUTION_KEY!r} and the trust service writes "
        f"{TRUST_ATTRIBUTION_KEY!r}: the attribution is delivered into a key nobody opens"
    )
    from store_agent.learning import SUPPORTED_SCHEMA_VERSIONS

    assert ATTRIBUTION_SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS, (
        f"the trust service emits schema_version {ATTRIBUTION_SCHEMA_VERSION} and this agent "
        f"understands {sorted(SUPPORTED_SCHEMA_VERSIONS)}, so every record is ignored as unknown"
    )


def test_the_percentage_vocabulary_matches_the_one_the_bid_path_walls_a_discount_with() -> None:
    """``learning.attribution`` re-spells ``hooks.provenance``'s set; the two must not drift.

    They are spelled twice because ``hooks`` imports ``learning`` and the reverse closes an import
    cycle. A rung read here under a spelling the bid path would not authorize is a rung the loop
    could learn to want and could never play.
    """
    from store_agent.hooks.provenance import PERCENTAGE_DISCOUNT_TYPES as WALLED
    from store_agent.learning.attribution import PERCENTAGE_DISCOUNT_TYPES as READ

    assert READ == WALLED, f"the reader accepts {sorted(READ)}, the wall accepts {sorted(WALLED)}"


def test_the_payload_this_file_drives_really_carries_an_attribution() -> None:
    """Otherwise every assertion below would grade the delta-sign path under a new name."""
    payload = pushed_payload("auc-arm-0001", delta=0.05)
    record = payload["event"]["payload"][ATTRIBUTION_KEY]
    assert record["schema_version"] == ATTRIBUTION_SCHEMA_VERSION
    assert record["auction_id"] == "auc-arm-0001"
    assert record["converted"] is True and record["shown"] is True
    assert record["arm"]["discount"] == {"type": "percentage", "value": WIN_PCT}


# =============================================================================================
# 2. the crux — one delta, two readings, opposite directions
# =============================================================================================


def test_shown_and_not_converted_is_a_loss_and_the_same_delta_alone_is_a_win() -> None:
    """The disagreement, driven through the door. This is the ticket in one assertion.

    Both agents receive the identical ``+0.05`` on the identical auction. One payload carries the
    impression the trust service attributed it to (``shown``, not ``converted``); the other is the
    same payload with the record stripped, which is what a build predating the join would send.
    The rung's posterior goes down for the first and up for the second.
    """
    attributed = client()
    teach(attributed, "auc-crux", delta=0.05, pct=WIN_PCT, shown=True, converted=False)

    unattributed = client()
    served_bid(unattributed, "auc-crux")
    push(unattributed, pushed_payload("auc-crux", delta=0.05, attribution=None))

    assert tally(attributed, WIN_PCT) == (0, 1), (
        "an impression that was shown and did not convert is the LOSS that makes this a "
        f"posterior rather than a counter; the {WIN_PCT}% rung tallied {tally(attributed, WIN_PCT)}"
    )
    assert posterior_mean(attributed, WIN_PCT) == pytest.approx(1 / 3)
    # The control: the identical delta, read the way it was read before this file existed.
    assert tally(unattributed, 0.0) == (1, 0), (
        "the delta-sign path must still credit a win for a positive delta; the aggregate "
        "behaviour that works today is not allowed to change when no attribution rides along"
    )
    assert posterior_mean(unattributed, 0.0) == pytest.approx(2 / 3)


def test_the_rung_credited_is_the_one_actually_quoted_not_the_one_sampled() -> None:
    """A cold store samples the 0% rung; the record says the offer that converted was at 20%.

    That is the counter-proposal / walled-ask case ``impression_for`` names — "the ACCEPTED offer
    wins over the bid's" — and it is why the rung is read off the wire rather than off the arm.
    """
    api = client()
    assert served_rung(api, "auc-quote") == 0.0, "a cold store must ask for no discount"
    body = teach(api, "auc-quote", delta=0.05, pct=WIN_PCT, converted=True)

    assert body["learning"]["reason"] == CREDIT_IMPRESSION
    assert body["learning"]["source"] == ATTRIBUTION_SOURCE
    assert body["learning"]["discount_depth"] == pytest.approx(WIN_PCT / 100.0)
    assert tally(api, WIN_PCT) == (1, 0)
    assert tally(api, 0.0) == (0, 0), "the sampled rung must not be credited for the quoted one"


def test_an_offer_at_list_price_is_a_real_rung_and_not_a_missing_one() -> None:
    """``discount: null`` is a bid at the catalog price — the shallowest rung, and evidence."""
    api = client()
    teach(api, "auc-list", delta=0.05, pct=None, converted=True)
    assert tally(api, 0.0) == (1, 0), (
        "an offer that carried no discount converted; dropping it would teach the loop that the "
        "0% rung never wins, which is the opposite of what happened"
    )


# =============================================================================================
# 3. the loop, end to end: cold -> taught -> the served price moves
# =============================================================================================


def taught(api: TestClient, *, rounds: int = 20) -> None:
    """Conversions at :data:`WIN_PCT`, impressions that did not close at :data:`LOSS_PCT`.

    Nothing at all is said about :data:`QUIET_PCT`, which is the control.
    """
    for index in range(rounds):
        teach(api, f"auc-win-{index:03d}", delta=0.05, pct=WIN_PCT, converted=True)
        teach(
            api,
            f"auc-loss-{index:03d}",
            delta=0.05,
            pct=LOSS_PCT,
            shown=True,
            converted=False,
        )


def test_the_depth_distribution_moves_to_the_rung_that_converted() -> None:
    """S4 through the door: same seeds, moved state, and the sampler follows the evidence."""
    api = client()
    before = rung_shares(api, tag="w")
    assert dict(before) == {0.0: WINDOW}, (
        f"a store with no record of its own must ask for nothing: {dict(before)}"
    )

    taught(api)
    after = rung_shares(api, tag="w")

    assert after[WIN_PCT] > WINDOW / 2, (
        f"a store taught that {WIN_PCT}% converts must mostly play it: {dict(after)}"
    )
    assert after.most_common(1)[0][0] == WIN_PCT, dict(after)
    assert after[LOSS_PCT] == 0, (
        f"the rung this store was SHOWN at and did not close must lose share, not keep it: "
        f"{dict(after)}"
    )


def test_the_posterior_moves_on_the_rungs_with_evidence_and_nowhere_else() -> None:
    """The tallies, read back off the app after the pushes. The control is the quiet rung.

    ``sample_depth`` draws each rung's plausible win rate from its own Beta(wins + 1, losses + 1),
    so "the posterior moved" is a statement about these three numbers and not about the served
    distribution, which is their pushforward.
    """
    api = client()
    taught(api)

    assert tally(api, WIN_PCT) == (20, 0)
    assert posterior_mean(api, WIN_PCT) == pytest.approx(21 / 22)
    assert tally(api, LOSS_PCT) == (0, 20)
    assert posterior_mean(api, LOSS_PCT) == pytest.approx(1 / 22)
    assert tally(api, QUIET_PCT) == (0, 0), (
        f"the {QUIET_PCT}% rung was never quoted, shown or converted, and its tally moved anyway"
    )
    assert posterior_mean(api, QUIET_PCT) == pytest.approx(0.5), (
        "a rung nothing happened on must keep the flat prior it started with"
    )
    for quiet in (rung for rung in DEFAULT_DEPTH_BUCKETS if _pct(rung) not in (WIN_PCT, LOSS_PCT)):
        assert tally(api, _pct(quiet)) == (0, 0), f"{_pct(quiet)}% moved with no evidence"


def test_the_shift_reaches_the_price_a_shopper_would_be_shown() -> None:
    """The arm is not bookkeeping. The learned rung goes through hook 5's envelope wall and out.

    ``store-alpha``'s approved cap is 20% of a 100.00 list price, which is exactly
    :data:`WIN_PCT`, so a store taught to convert there prices at 80.00 — and the discount on the
    served offer cites the envelope rule that authorised it, not the loop that asked for it.
    """
    api = client()
    cold = served_bid(api, "auc-price")
    assert cold["offer"]["unit_price"] == 100.0
    assert cold["offer"]["discount"] is None

    taught(api)
    warm = served_bid(api, "auc-price")
    assert warm["offer"]["unit_price"] == 80.0, warm["offer"]
    assert warm["offer"]["discount"]["value"] == pytest.approx(WIN_PCT)
    assert warm["offer"]["discount"]["provenance"]["source"] == "envelope_rule"


def test_two_identical_runs_are_byte_identical() -> None:
    """No clock, no ambient RNG on either door — the whole loop reproduces (S4)."""

    def run() -> str:
        api = client()
        trace: list[Any] = [served_bid(api, "auc-pre")]
        for index in range(8):
            auction_id = f"auc-det-{index:04d}"
            trace.append(served_bid(api, auction_id))
            trace.append(
                push(
                    api,
                    pushed_payload(
                        auction_id,
                        delta=0.05,
                        pct=WIN_PCT if index % 2 else LOSS_PCT,
                        converted=bool(index % 2),
                    ),
                )
            )
        trace.append(served_bid(api, "auc-post"))
        return json.dumps(trace, sort_keys=True)

    assert run() == run()


# =============================================================================================
# 4. what is refused, and the aggregate path that must survive every refusal
# =============================================================================================


@pytest.mark.parametrize(
    ("case", "kwargs", "expected"),
    [
        # An unrecognised version. `impression_for`'s own header says a recipient that does not
        # recognise the version ignores the record.
        ("unrecognised_schema_version", {"schema_version": 99}, "unrecognised_schema_version"),
        # A record with no arm: `impression_for` returns this when the chain carried no offer.
        ("no_arm", {"arm": None}, "no_arm"),
        # A rung past the merchant's approved cap AND past the top of the grid.
        (
            "unauthorized_depth",
            {"arm": {"discount": {"type": "percentage", "value": 90.0}}},
            "unauthorized_depth",
        ),
        # A discount this reader cannot turn into a depth without inventing the list price.
        (
            "fixed_amount",
            {"arm": {"discount": {"type": "fixed_amount", "value": 5.0}}},
            "unreadable_discount",
        ),
        # A record about a different auction than the event it rode in on.
        ("auction_mismatch", {"auction_id": "auc-somewhere-else"}, "auction_mismatch"),
        # Bid and benched: no shopper saw this arm, so there is nothing to grade it on.
        ("not_shown", {"shown": False, "converted": False}, "not_shown"),
    ],
)
def test_a_malformed_attribution_is_ignored_safely_and_says_so(
    case: str, kwargs: dict[str, Any], expected: str
) -> None:
    """Four properties per case, and the third is the one a refusal usually breaks.

    The door still answers 200; the posture still moves by the full delta; the arm is credited
    ONLY by the delta's sign, at the depth this agent itself played rather than the one the wire
    claimed; and the response names which reading was refused, so an operator can tell "ignored"
    from "there was nothing to read".
    """
    api = client()
    auction_id = f"auc-bad-{case}"
    played = served_rung(api, auction_id)
    record = {
        **impression_for(
            ledger_event(auction_id, matched=True),
            ledger_rows(auction_id, pct=WIN_PCT, shown=True, converted=True),
        ),
        **kwargs,
    }
    body = push(api, pushed_payload(auction_id, delta=-0.05, attribution=record))

    assert body["learning"]["attribution"] == expected, body["learning"]
    assert body["posture"]["signals"][0]["net_delta"] == pytest.approx(-0.05), (
        "a refused attribution must not cost the posture the delta it rode in on"
    )
    assert body["learning"]["reason"] == CREDIT_DELTA_SIGN
    assert body["learning"]["source"] == OUTCOME_SOURCE
    assert body["learning"]["discount_depth"] == pytest.approx(played / 100.0), (
        "a refused record must not decide the rung; the fallback credits the arm this agent "
        "actually played, which is local state the wire cannot reach"
    )
    assert tally(api, played) == (0, 1)
    if case == "unauthorized_depth":
        # 90% is not a rung at all, and `bucket_index` scores rungs by distance and snaps to the
        # NEAREST one — so a reader that merely declined to *trust* the number, without refusing
        # it, would have filed this on the deepest rung of the grid and called it evidence.
        assert tally(api, WIN_PCT) == (0, 0), (
            "a 90% depth off the wire landed on the 20% rung; an out-of-grid depth is not "
            "refused by the bucketing, it is silently rounded into the nearest real rung"
        )


def test_an_event_with_no_attribution_at_all_behaves_exactly_as_it_did() -> None:
    """The ordinary case for a trust build that predates the join, and it must not regress."""
    api = client()
    served_bid(api, "auc-plain")
    body = push(api, pushed_payload("auc-plain", delta=-0.05, attribution=None))
    assert body["learning"]["attribution"] == "absent"
    assert body["learning"]["reason"] == CREDIT_DELTA_SIGN
    assert tally(api, 0.0) == (0, 1)


def test_an_attribution_for_an_auction_this_agent_never_answered_credits_nothing() -> None:
    """The cluster lives on the ARM, so an auction the runner does not hold cannot be joined.

    ``cluster_id`` is deliberately not on the attribution: it lives on ``auction_opened``, whose
    ``store_id`` is null, so it is not in the store's own ledger history and the trust service
    cannot supply it. The posture still moves — that is the trust door's business — and the
    response says why nothing was credited rather than reporting a silent success.
    """
    api = client()
    body = push(api, pushed_payload("auc-never-answered", delta=0.05))
    assert body["learning"]["reason"] == CREDIT_UNREMEMBERED
    assert body["learning"]["cluster_id"] is None
    assert body["learning"]["credited"] is False
    assert body["posture"]["signals"][0]["net_delta"] == pytest.approx(0.05)
    assert agent_runner(api.app).learning.observations == 0


def test_one_impression_is_one_trial_however_many_events_report_it() -> None:
    """A converted auction goes on to produce several trust events, each re-reporting the sale.

    ``order_paid``, ``order_fulfilled``, ``feedback`` — the join walks the whole chain every time,
    so every one of them arrives saying ``converted: true``. Booking each would turn one sale into
    four wins on one rung, which is the "posterior that only ever rises" this reader exists to
    avoid.
    """
    api = client()
    first = teach(api, "auc-dupe", delta=0.05, pct=WIN_PCT, converted=True)
    assert first["learning"]["reason"] == CREDIT_IMPRESSION

    for _ in range(3):
        again = push(api, pushed_payload("auc-dupe", delta=0.05, pct=WIN_PCT, converted=True))
        assert again["learning"]["reason"] == CREDIT_ALREADY
        assert again["learning"]["credited"] is False

    assert tally(api, WIN_PCT) == (1, 0), (
        f"four events reporting one sale booked {tally(api, WIN_PCT)} on the rung"
    )
    # The posture is the aggregate and DOES count every event; the two are different questions.
    assert agent_runner(api.app).trust_posture.signals[0].observations == 4


def test_the_envelope_cap_walls_the_rung_a_record_may_teach() -> None:
    """A merchant who approves 10% cannot have a 20% rung taught to their agent from the wire.

    The grid's top rung is 20%, so this is the envelope's wall and not the grid's: the same
    record is credited by an agent whose approved cap is 20% and refused by one whose cap is 10%.
    Read live off the context, like every other envelope field, so narrowing the cap takes effect
    on the next event rather than on the next deploy.
    """
    approved = client()
    narrow = client(envelope={**context()["envelope"], "max_discount_pct": 10.0})

    for api in (approved, narrow):
        served_bid(api, "auc-cap")
    accepted = push(approved, pushed_payload("auc-cap", delta=0.05, pct=20.0, converted=True))
    refused = push(narrow, pushed_payload("auc-cap", delta=0.05, pct=20.0, converted=True))

    assert accepted["learning"]["reason"] == CREDIT_IMPRESSION
    assert accepted["learning"]["discount_depth"] == pytest.approx(0.20)
    assert refused["learning"]["attribution"] == "unauthorized_depth"
    assert tally(narrow, 20.0) == (0, 0), (
        "a rung the merchant never authorised became evidence about a rung the merchant never "
        f"authorised: {tally(narrow, 20.0)}"
    )
