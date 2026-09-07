"""The trust loop, closed: an observation that arrives as an event reaches the exchange.

WHAT WAS BROKEN, MEASURED OVER THE SERVED ROUTES BEFORE ANY OF THIS EXISTED
---------------------------------------------------------------------------
``GET /snapshot`` reads ``app.sellers LEFT JOIN ledger.trust_observations`` and is the door
``exchange.composition.HttpTrustSnapshot`` calls over HTTP for R12 eligibility at all three
gates. ``POST /events`` wrote ``ledger.commerce_events`` and projected nothing into that
table; its only two writers were reconciliation and verification persistence. So::

    POST /events {kind: feedback, store_id: store-northroast, order_ref: o-1001,
                  payload: {matched_pitch: false, reason: no_not_as_described,
                            dim: feedback_match, type: mismatch_return}}   -> 201
    select count(*) from ledger.trust_observations                          -> 0
    GET /snapshot   feedback_match -> {alpha 2.0, beta 2.0}, low_data true, score 0.5
    GET /events/replay?snapshots=true
                    feedback_match -> {alpha 2.0, beta 2.7854705921154700}

Two failures in one, and the second is the worse: a real buyer's complaint moved no
eligibility decision anywhere, AND R15/S3 — *recomputing all trust scores from the ledger
reproduces the served scores exactly* — was false by a whole observation.

The same gap swallowed ``claim_verified`` entirely. Its frozen shape is
``(claim_ref, status, dim)``, so it names a ``dim`` and no ``type``, and the projection
required both: measured, a served ``contradicted`` verdict answered ``201`` and
``replay?snapshots=true`` came back ``{}``.

And R14's weighting had no production caller at all: ``accept_feedback`` /
``feedback_observation`` implement "weighted by buyer track record, cross-checked against
return behavior" and were reached only by tests.

THE PROMISE LEDGER
------------------
``shipped_on_time`` is one of the three transaction dimensions and it had **no transaction
producer**: nothing anywhere compared ``order_fulfilled.fulfilled_at`` to the delivery
promise on the ``accepted`` offer. A store could promise two-day dispatch, take three weeks,
and sit on the untouched neutral prior forever — so trust graded consistency with a
catalogue the store itself authored, and not honesty.

HOW THESE TESTS ARE BUILT
-------------------------
Two layers, and the docker layer is the one that decides. The projection layer grades the
pure fold (it runs everywhere); the served layer drives ``POST /events`` and
``POST /reconcile`` into a real database as ``trust_rw`` and then reads ``GET /snapshot``
back — because the whole defect was that each door worked and they were not connected, which
is precisely what a test of either door alone cannot see.

Every served assertion also compares the snapshot against ``GET /events/replay`` on the same
instant. A projection that wrote a row the replay disagrees with would close the exchange's
door and break R15 in the same commit, and only the pairing catches it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

# ======================================================================================
# The projection — no database, no server. Runs everywhere.
# ======================================================================================


def _project(events: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    from trust.ledger import observations_from_events

    return observations_from_events(events, **kwargs)


def test_a_claim_verified_event_projects_its_status_as_the_observation_type(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """``(claim_ref, status, dim)`` is the FROZEN shape, so ``status`` is the type or nothing.

    Before this, every ``claim_verified`` in the chain projected to zero observations —
    including the ones ``apps/exchange/src/ranking/verification.py`` announces on the served
    auction path, one per counted claim.
    """
    event = loop_event(
        "claim_verified:s-1:c-1",
        "claim_verified",
        store_id="s-1",
        payload={
            "claim_ref": "c-1",
            "status": "contradicted",
            "dim": "catalog_claim_accuracy",
            "claim_type": "ingredients",
        },
    )
    assert _project([event]) == [
        {
            "store_id": "s-1",
            "dim": "catalog_claim_accuracy",
            "type": "contradicted",
            "observed_at": event["ts"],
        }
    ]


def test_a_status_on_some_other_kind_does_not_mint_an_observation(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """The fallback is gated on the KIND, so a vendor body carrying a status is inert.

    Ledger payloads are lossless by design and "extra keys are welcome" at every producing
    boundary, so an ``order_paid`` whose merchant body happens to carry ``status`` and a
    ``dim`` must not become a trust observation about that store.
    """
    event = loop_event(
        "order_paid:s-1:o-1",
        "order_paid",
        store_id="s-1",
        order_ref="o-1",
        payload={
            "checkout_token": "T",
            "order_ref": "o-1",
            "total_price": 10.0,
            "status": "contradicted",
            "dim": "price_honored",
        },
    )
    assert _project([event]) == []


def test_a_feedback_event_with_no_type_derives_one_from_the_buyers_own_answer(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """``(matched_pitch, reason)`` is the published body; ``type`` is an extra the emitter adds.

    A producer that emits only the published keys is not misbehaving, and R14's own rule
    already knows what "it did not match the pitch" is worth.
    """
    event = loop_event(
        "feedback:s-1:o-1",
        "feedback",
        store_id="s-1",
        order_ref="o-1",
        payload={"matched_pitch": False, "reason": "no_not_as_described", "dim": "feedback_match"},
    )
    assert _project([event]) == [
        {
            "store_id": "s-1",
            "dim": "feedback_match",
            "type": "mismatch_return",
            "observed_at": event["ts"],
        }
    ]


def test_a_feedback_event_that_states_no_answer_stays_invisible(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """Silence is not a complaint.

    ``_is_positive`` answers ``False`` for a payload naming neither ``matched_pitch`` nor
    ``answer``, which is the right default for a report that is present and unparseable — but
    read as a derived TYPE it would turn "this event says nothing" into ``mismatch_return``, a
    published 1.5 negative, against a real store on the strength of an empty body.
    """
    event = loop_event(
        "feedback:s-1:o-1",
        "feedback",
        store_id="s-1",
        order_ref="o-1",
        payload={"dim": "feedback_match"},
    )
    assert _project([event]) == []


def test_a_positive_report_the_buyers_own_return_contradicts_is_discounted(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """R14's cross-check, reaching a Beta for the first time.

    ``RETURN_CONTRADICTION_FACTOR`` is 0.25 and was computed by ``accept_feedback`` for
    nobody: the only callers in the tree were tests. The refund is EARLIER in the stream, so
    the weight is a function of the chain's prefix and replays identically.
    """
    from trust.feedback import RETURN_CONTRADICTION_FACTOR

    refund = loop_event(
        "refund:s-1:o-1",
        "refund",
        store_id="s-1",
        order_ref="o-1",
        payload={"order_ref": "o-1", "amount": 100.0, "reason": "returned"},
    )
    feedback = loop_event(
        "feedback:s-1:o-1",
        "feedback",
        store_id="s-1",
        order_ref="o-1",
        payload={
            "matched_pitch": True,
            "reason": "yes_as_described",
            "dim": "feedback_match",
            "type": "fulfilled",
        },
    )
    projected = _project([refund, feedback])
    assert projected == [
        {
            "store_id": "s-1",
            "dim": "feedback_match",
            "type": "fulfilled",
            "observed_at": feedback["ts"],
            "weight": RETURN_CONTRADICTION_FACTOR,
        }
    ]


def test_an_uncontradicted_report_projects_with_no_weight_at_all(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """Absent means exactly 1.0, and S3's assertion is an ``==`` over these values.

    A ``weight: 1.0`` key would score the same and compare unequal to what every producer in
    the tree emits, so the fold attaches one only when R14 actually discounted the report.
    """
    feedback = loop_event(
        "feedback:s-1:o-1",
        "feedback",
        store_id="s-1",
        order_ref="o-1",
        payload={
            "matched_pitch": True,
            "reason": "yes_as_described",
            "dim": "feedback_match",
            "type": "fulfilled",
        },
    )
    assert _project([feedback]) == [
        {
            "store_id": "s-1",
            "dim": "feedback_match",
            "type": "fulfilled",
            "observed_at": feedback["ts"],
        }
    ]


def test_a_return_of_a_DIFFERENT_store_does_not_discount_this_report(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """A platform ``order_ref`` is a per-shop number, so the cross-check is scoped by store.

    Unscoped, one shop's refund of its order ``o-1`` would quarter the weight of every other
    shop's feedback about its own ``o-1``.
    """
    refund = loop_event(
        "refund:s-2:o-1",
        "refund",
        store_id="s-2",
        order_ref="o-1",
        payload={"order_ref": "o-1", "amount": 100.0, "reason": "returned"},
    )
    feedback = loop_event(
        "feedback:s-1:o-1",
        "feedback",
        store_id="s-1",
        order_ref="o-1",
        payload={"matched_pitch": True, "dim": "feedback_match", "type": "fulfilled"},
    )
    assert "weight" not in _project([refund, feedback])[0]


def test_a_return_recorded_after_the_report_does_not_rewrite_it(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """The limit stated in ``trust.feedback.weighting``, asserted rather than assumed.

    The chain is append-only, so a weight that depended on events AFTER its own would fold to
    two different numbers depending on when it was folded — which is what S3 forbids. The
    return is not lost; it is its own evidence, through its own producer.
    """
    feedback = loop_event(
        "feedback:s-1:o-1",
        "feedback",
        store_id="s-1",
        order_ref="o-1",
        payload={"matched_pitch": True, "dim": "feedback_match", "type": "fulfilled"},
    )
    refund = loop_event(
        "refund:s-1:o-1",
        "refund",
        store_id="s-1",
        order_ref="o-1",
        payload={"order_ref": "o-1", "amount": 100.0, "reason": "returned"},
    )
    assert "weight" not in _project([feedback, refund])[0]


def test_a_caller_folding_one_event_can_supply_the_returns_the_chain_already_holds(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """``POST /events`` has the chain BEHIND it, not in hand. This is how it says so.

    Without this parameter the append path and the replay would answer differently about the
    same order — the append seeing no refund because it holds one event, the replay seeing one
    because it holds the chain — and that difference is R15 breaking.
    """
    from trust.feedback import RETURN_CONTRADICTION_FACTOR

    feedback = loop_event(
        "feedback:s-1:o-1",
        "feedback",
        store_id="s-1",
        order_ref="o-1",
        payload={"matched_pitch": True, "dim": "feedback_match", "type": "fulfilled"},
    )
    assert _project([feedback])[0].get("weight") is None
    supplied = _project([feedback], returned_orders=[("s-1", "o-1")])
    assert supplied[0]["weight"] == RETURN_CONTRADICTION_FACTOR


def test_a_producer_that_computed_its_own_weight_is_not_overruled(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """The fold fills a gap; it does not hold a second opinion.

    ``trust.scoring`` owns what an admissible weight is, and a projection that recomputed a
    weight the producer already stated would make the stored event and the folded observation
    disagree about the same report.
    """
    feedback = loop_event(
        "feedback:s-1:o-1",
        "feedback",
        store_id="s-1",
        order_ref="o-1",
        payload={
            "matched_pitch": True,
            "dim": "feedback_match",
            "type": "fulfilled",
            "weight": 0.5,
        },
    )
    assert _project([feedback], returned_orders=[("s-1", "o-1")])[0]["weight"] == 0.5


# ======================================================================================
# HONEST TRAFFIC — this repo's own recorded fixture stream, through the new fold
# ======================================================================================


def test_the_repos_own_recorded_event_stream_still_projects_exactly_as_before(
    events_fixture_stream: dict[str, Any],
) -> None:
    """The fixture corpus this repo already ships, driven through the changed projection.

    ``fixtures/events/*`` is the recorded stream T-060's chain tests are graded against — real
    shapes, recorded rather than invented here. Two properties are asserted, and the second is
    the one a new fallback can quietly break:

    * every observation the projection found before is still found, unchanged; and
    * the projection invents nothing new out of events that carry no observation.

    The expectation is derived from the stream itself (``dim`` and ``type`` both present),
    which is the ONE rule that held before this change — so this is a genuine before/after
    comparison and not a restatement of the code.
    """
    events = list(events_fixture_stream["events"])
    assert events, "the recorded fixture stream is empty, so this proves nothing"

    expected = [
        {
            "store_id": event.get("store_id") or (event.get("payload") or {}).get("store_id"),
            "dim": (event.get("payload") or {})["dim"],
            "type": (event.get("payload") or {})["type"],
            "observed_at": (event.get("payload") or {}).get("observed_at") or event.get("ts"),
            **(
                {"weight": (event.get("payload") or {})["weight"]}
                if (event.get("payload") or {}).get("weight") is not None
                else {}
            ),
        }
        for event in events
        if isinstance(event.get("payload"), dict)
        and event["payload"].get("dim") is not None
        and event["payload"].get("type") is not None
        and (event.get("store_id") or event["payload"].get("store_id")) is not None
    ]
    assert _project(events) == expected


def test_the_repos_own_recorded_stream_is_still_admitted_by_the_write_door(
    events_fixture_stream: dict[str, Any],
) -> None:
    """Honest traffic still passes the poison screen the projection feeds.

    ``POST /events`` refuses a payload the trust replay could not later interpret, and it
    decides that by running THIS projection over the arriving event. Widening the projection
    therefore widens the refusal: kinds that used to sail past unread are now screened. Every
    event in the repo's own recorded stream must still be admitted.
    """
    from trust.events.routes import _unreplayable_field
    from trust.events.store import normalise_event

    refused = [
        (event.get("event_id"), field)
        for event in events_fixture_stream["events"]
        if (field := _unreplayable_field(normalise_event(event))) is not None
    ]
    assert refused == [], (
        f"the widened poison screen now refuses events from this repo's own recorded "
        f"stream: {refused}"
    )


@pytest.mark.parametrize("status", ["verified", "contradicted", "unsupported", "ambiguous"])
def test_every_published_verification_status_is_still_admitted(
    status: str, loop_event: Callable[..., dict[str, Any]]
) -> None:
    """The four ``ClaimVerificationStatus`` values are four of the seven observation types.

    That equality is what makes reading ``status`` as ``type`` sound, and it is asserted here
    rather than assumed: if the two vocabularies ever diverged, this new fallback would start
    refusing real ``claim_verified`` events at the write door.
    """
    from trust.events.routes import _unreplayable_field
    from trust.events.store import normalise_event
    from trust.scoring import OBSERVATION_WEIGHTS

    assert status in OBSERVATION_WEIGHTS
    event = loop_event(
        f"claim_verified:s-1:{status}",
        "claim_verified",
        store_id="s-1",
        payload={"claim_ref": "c-1", "status": status, "dim": "price_honored"},
    )
    assert _unreplayable_field(normalise_event(event)) is None


def test_a_claim_verified_carrying_an_unscoreable_status_is_refused_at_the_door(
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """The positive control for the refusal above.

    The screen exists because the ledger is append-only under a
    ``BEFORE UPDATE OR DELETE ... ENABLE ALWAYS`` trigger: a payload the scorer cannot read
    would make every later snapshot replay a 500 that nothing could ever clear. Now that
    ``claim_verified`` reaches the scorer at all, it has to be screened like everything else.
    """
    from trust.events.routes import _unreplayable_field
    from trust.events.store import normalise_event

    event = loop_event(
        "claim_verified:s-1:bogus",
        "claim_verified",
        store_id="s-1",
        payload={"claim_ref": "c-1", "status": "probably_fine", "dim": "price_honored"},
    )
    assert _unreplayable_field(normalise_event(event)) == "type"


# ======================================================================================
# THE PROMISE LEDGER — the fold, without a database
# ======================================================================================


def _reconcile(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from trust.reconcile.engine import reconcile

    return reconcile(events)


def _observations(events: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    from trust.reconcile.engine import observation_events, reconcile

    return [
        (str(event["store_id"]), event["payload"]["dim"], event["payload"]["type"])
        for event in observation_events(reconcile(events))
    ]


def test_a_broken_delivery_promise_becomes_a_shipped_on_time_contradiction(
    loop_purchase: Callable[..., list[dict[str, Any]]],
) -> None:
    """The dimension's first transaction producer.

    Promised three days, shipped in twenty-one. Nothing in the tree compared those two
    numbers before: ``shipped_on_time`` was reachable only from a *verification* of a delivery
    CLAIM, so a store could be verified as having promised two-day dispatch and never graded
    on whether it managed it.
    """
    events = loop_purchase(
        "s-late",
        order_ref="o-1",
        checkout_token="T1",
        delivery_estimate_days=3,
        fulfilled_at="2026-02-22T00:00:00.000Z",
    )
    assert ("s-late", "shipped_on_time", "contradicted") in _observations(events)


def test_a_kept_delivery_promise_earns_a_positive(
    loop_purchase: Callable[..., list[dict[str, Any]]],
) -> None:
    """A kept promise must EARN a score, not merely avoid costing one.

    Otherwise an honest store never leaves the neutral prior and ``low_data`` never clears —
    the same argument ``HONORED_OBSERVATION_TYPE`` already makes for price.
    """
    events = loop_purchase(
        "s-quick",
        order_ref="o-1",
        checkout_token="T1",
        delivery_estimate_days=3,
        fulfilled_at="2026-02-03T00:00:00.000Z",
    )
    assert ("s-quick", "shipped_on_time", "fulfilled") in _observations(events)


def test_shipping_early_is_not_a_broken_promise(
    loop_purchase: Callable[..., list[dict[str, Any]]],
) -> None:
    """One-sided, exactly as ``price_honored`` is: beating your own estimate is not a fault."""
    events = loop_purchase(
        "s-early",
        order_ref="o-1",
        checkout_token="T1",
        delivery_estimate_days=10,
        fulfilled_at="2026-02-02T00:00:00.000Z",
    )
    assert ("s-early", "shipped_on_time", "fulfilled") in _observations(events)


def test_an_order_that_has_not_shipped_yet_is_not_graded_on_delivery(
    loop_purchase: Callable[..., list[dict[str, Any]]],
) -> None:
    """Not ``unsupported``, which is a published 0.5 NEGATIVE.

    Every order is unshipped for a while. Grading absence would charge every store a small
    penalty on every order between payment and dispatch, and then charge it again when the
    delivery landed.
    """
    events = loop_purchase(
        "s-pending", order_ref="o-1", checkout_token="T1", delivery_estimate_days=3
    )
    dims = [dim for _store, dim, _type in _observations(events)]
    assert "shipped_on_time" not in dims, dims


def test_an_offer_that_promised_no_delivery_date_is_not_graded_on_delivery(
    loop_purchase: Callable[..., list[dict[str, Any]]],
) -> None:
    """A verdict is only evidence about a store when there was a promise behind it.

    ``Offer.delivery_estimate_days`` is optional in the published schema, and most of this
    repo's own fixtures omit it.
    """
    events = loop_purchase(
        "s-silent",
        order_ref="o-1",
        checkout_token="T1",
        fulfilled_at="2026-02-25T00:00:00.000Z",
    )
    dims = [dim for _store, dim, _type in _observations(events)]
    assert "shipped_on_time" not in dims, dims


def test_a_fulfilment_with_an_unreadable_instant_is_unsupported_not_contradicted(
    loop_purchase: Callable[..., list[dict[str, Any]]],
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """A record that exists and cannot be read is a gap, not a broken promise.

    The same distinction ``price_comparable`` draws: turning "we cannot tell" into a 2.0
    contradiction would let a malformed fulfilment body manufacture a penalty the store can
    neither see coming nor appeal.
    """
    events = loop_purchase(
        "s-garbled", order_ref="o-1", checkout_token="T1", delivery_estimate_days=3
    )
    events.append(
        loop_event(
            "order_fulfilled:s-garbled:o-1",
            "order_fulfilled",
            store_id="s-garbled",
            order_ref="o-1",
            payload={"order_ref": "o-1", "fulfilled_at": "whenever"},
        )
    )
    assert ("s-garbled", "shipped_on_time", "unsupported") in _observations(events)


def test_a_fulfilment_cannot_merge_two_orders(
    loop_purchase: Callable[..., list[dict[str, Any]]],
    loop_event: Callable[..., dict[str, Any]],
) -> None:
    """A delivery notice is a statement ABOUT an order, never an identity of its own.

    Extra keys are a sanctioned shape at every producing boundary in this repo, so a
    fulfilment carrying another checkout's ``checkout_token`` is not an attack — and honouring
    it would merge two orders into one group, at which point ``setdefault`` keeps one webhook
    and the other order silently stops being graded. A vanished order is the failure this
    module treats as worse than a wrong verdict.
    """
    events = loop_purchase(
        "s-1", order_ref="o-1", checkout_token="T1", paid_price=500.0, total_price=100.0
    )
    events += loop_purchase(
        "s-1", order_ref="o-2", checkout_token="T2", paid_price=900.0, total_price=100.0
    )
    events.append(
        loop_event(
            "order_fulfilled:s-1:o-2",
            "order_fulfilled",
            store_id="s-1",
            order_ref="o-2",
            # The forgery: this delivery notice also names the OTHER checkout's token.
            payload={"order_ref": "o-2", "fulfilled_at": "2026-02-02T00:00:00.000Z"},
            checkout_token="T1",
        )
    )
    reconciled = _reconcile(events)
    assert sorted(event["payload"]["order_ref"] for event in reconciled) == ["o-1", "o-2"], (
        f"a fulfilment merged two orders and one of them stopped being graded: {reconciled}"
    )


def test_the_delivery_finding_names_the_bid_that_made_the_promise(
    loop_purchase: Callable[..., list[dict[str, Any]]],
) -> None:
    """``offer_integrity`` publishes ``bid_ref``: a finding a store cannot trace is one it
    can neither check nor contest."""
    from trust.reconcile.engine import observation_events

    events = loop_purchase(
        "s-late",
        order_ref="o-1",
        checkout_token="T1",
        delivery_estimate_days=1,
        fulfilled_at="2026-02-20T00:00:00.000Z",
    )
    delivery = [
        event
        for event in observation_events(_reconcile(events))
        if event["payload"]["field"] == "delivery"
    ]
    assert len(delivery) == 1, delivery
    payload = delivery[0]["payload"]
    assert payload["bid_ref"] == "bid-s-late"
    assert payload["promised"] == 1.0
    assert payload["observed"] == 19.0
    assert delivery[0]["event_id"] == "offer_integrity:s-late:o-1:delivery"


def test_the_price_and_discount_verdicts_are_unchanged_by_the_delivery_promise(
    loop_purchase: Callable[..., list[dict[str, Any]]],
) -> None:
    """Honest traffic through the OLD half of the reconciler, with the new input present.

    An order that reconciles cleanly on price must go on reconciling cleanly on price whether
    or not a fulfilment record exists, and the delivery finding must not displace it.
    """
    without = loop_purchase("s-1", order_ref="o-1", checkout_token="T1")
    with_delivery = loop_purchase(
        "s-1",
        order_ref="o-1",
        checkout_token="T1",
        delivery_estimate_days=3,
        fulfilled_at="2026-02-02T00:00:00.000Z",
    )
    base = _reconcile(without)[0]["payload"]
    extended = _reconcile(with_delivery)[0]["payload"]
    for field in (
        "price_honored",
        "discount_honored",
        "price_comparable",
        "discount_comparable",
        "observed_price",
        "promised_price",
    ):
        assert base[field] == extended[field], field
    assert ("s-1", "price_honored", "fulfilled") in _observations(with_delivery)


# ======================================================================================
# SERVED — the door the exchange reads, over a real database as `trust_rw`
# ======================================================================================


def _dim(entry: dict[str, Any], name: str) -> dict[str, Any]:
    return entry["dims"][name]


def _snapshot(client: Any, as_of: str) -> dict[str, Any]:
    response = client.get("/snapshot", params={"as_of": as_of})
    assert response.status_code == 200, response.text
    return response.json()


def _replayed(client: Any, as_of: str) -> dict[str, Any]:
    response = client.get("/events/replay", params={"snapshots": "true", "as_of": as_of})
    assert response.status_code == 200, response.text
    return response.json()["snapshots"]


def _assert_snapshot_matches_replay(client: Any, as_of: str, store_id: str, dim: str) -> None:
    """R15/S3 over the two doors this lane connected, on the one dimension under test."""
    served = _dim(_snapshot(client, as_of)[store_id], dim)
    replayed = _dim(_replayed(client, as_of)[store_id], dim)
    for field in ("alpha", "beta", "decayed_at"):
        assert served[field] == replayed[field], (
            f"the served {dim}.{field} for {store_id} is {served[field]!r} and the replayed "
            f"one is {replayed[field]!r}. R15 says recomputing from the ledger reproduces the "
            f"served score exactly."
        )


@pytest.mark.docker
def test_a_served_feedback_event_moves_the_store_posture_the_exchange_reads(
    loop_client: Any,
    loop_seed_sellers: Callable[..., None],
    loop_post: Callable[..., Any],
    loop_event: Callable[..., dict[str, Any]],
    loop_rows: Callable[[], list[tuple]],
    loop_as_of: str,
) -> None:
    """The assertion a lane could not make and reported honestly instead of faking.

    One buyer report, through the served ``POST /events``, read back through the served
    ``GET /snapshot`` — the door ``exchange.composition.HttpTrustSnapshot`` calls. Measured
    before the projection existed: ``count(*) == 0``, ``feedback_match == {2.0, 2.0}``,
    ``low_data`` true, ``score == 0.5``, while the replay said ``beta 2.785…``.
    """
    loop_seed_sellers("store-northroast")
    before = _snapshot(loop_client, loop_as_of)["store-northroast"]
    assert _dim(before, "feedback_match") == {
        "alpha": 2.0,
        "beta": 2.0,
        "decayed_at": before["dims"]["feedback_match"]["decayed_at"],
    }, before

    response = loop_post(
        loop_event(
            "feedback:store-northroast:o-1001",
            "feedback",
            store_id="store-northroast",
            order_ref="o-1001",
            payload={
                "matched_pitch": False,
                "reason": "no_not_as_described",
                "dim": "feedback_match",
                "type": "mismatch_return",
            },
        )
    )
    assert response.json()["observation_rows"] == 1, response.text
    assert loop_rows() == [("store-northroast", "feedback_match", "mismatch_return", None)]

    after = _snapshot(loop_client, loop_as_of)["store-northroast"]
    assert _dim(after, "feedback_match")["beta"] > 2.0, after
    assert after["score"] < before["score"], (after["score"], before["score"])
    _assert_snapshot_matches_replay(loop_client, loop_as_of, "store-northroast", "feedback_match")


@pytest.mark.docker
def test_a_served_claim_verified_event_moves_the_dimension_its_status_names(
    loop_client: Any,
    loop_seed_sellers: Callable[..., None],
    loop_post: Callable[..., Any],
    loop_event: Callable[..., dict[str, Any]],
    loop_as_of: str,
) -> None:
    """Measured before: ``201`` from the door and ``{}`` from the replay. Both, silently."""
    loop_seed_sellers("store-northroast")
    response = loop_post(
        loop_event(
            "claim_verified:store-northroast:c-1",
            "claim_verified",
            store_id="store-northroast",
            payload={
                "claim_ref": "c-1",
                "status": "contradicted",
                "dim": "catalog_claim_accuracy",
                "claim_type": "ingredients",
            },
        )
    )
    assert response.json()["observation_rows"] == 1, response.text
    entry = _snapshot(loop_client, loop_as_of)["store-northroast"]
    assert _dim(entry, "catalog_claim_accuracy")["beta"] > 2.0, entry
    _assert_snapshot_matches_replay(
        loop_client, loop_as_of, "store-northroast", "catalog_claim_accuracy"
    )


@pytest.mark.docker
def test_reposting_the_same_event_writes_no_second_observation(
    loop_client: Any,
    loop_seed_sellers: Callable[..., None],
    loop_post: Callable[..., Any],
    loop_event: Callable[..., dict[str, Any]],
    loop_rows: Callable[[], list[tuple]],
) -> None:
    """``event_seq`` is the arbiter, so a re-delivered event moves no Beta twice.

    ``ledger.trust_observations`` carries no unique index over its real columns, and this door
    takes no credential; a doubled observation about a real store is what a check-then-insert
    with nothing to arbitrate the loser produces.
    """
    loop_seed_sellers("store-northroast")
    event = loop_event(
        "feedback:store-northroast:o-1001",
        "feedback",
        store_id="store-northroast",
        order_ref="o-1001",
        payload={"matched_pitch": False, "dim": "feedback_match", "type": "mismatch_return"},
    )
    loop_post(event)
    replayed = loop_post(dict(event), expect=200)
    assert replayed.headers["Idempotent-Replay"] == "true"
    assert len(loop_rows()) == 1, loop_rows()


@pytest.mark.docker
def test_a_first_attempt_that_could_not_write_converges_on_the_next_delivery(
    loop_client: Any,
    loop_seed_sellers: Callable[..., None],
    loop_post: Callable[..., Any],
    loop_event: Callable[..., dict[str, Any]],
    loop_rows: Callable[[], list[tuple]],
    # `ledger_clean` is reached through `loop_client` too, and is named here as well because
    # this body issues its own DELETE against an owned table: T-216's hygiene gate reads the
    # SIGNATURE, and a test graded against whatever the persistent worker database happens to
    # hold is invisible until that state changes.
    ledger_clean: Any,
    pg_admin: Any,
) -> None:
    """The reason the projection runs on a 200 as well as a 201.

    The write is best effort — the event is in the hash chain before it runs — so a failed
    relational write must be recoverable by repeating the delivery. Simulated here by deleting
    the row (which the append-only trigger permits on ``trust_observations``, and forbids on
    ``commerce_events``), which is exactly the state a failed first attempt leaves.
    """
    loop_seed_sellers("store-northroast")
    event = loop_event(
        "feedback:store-northroast:o-1001",
        "feedback",
        store_id="store-northroast",
        order_ref="o-1001",
        payload={"matched_pitch": False, "dim": "feedback_match", "type": "mismatch_return"},
    )
    loop_post(event)
    with pg_admin.cursor() as cursor:
        cursor.execute("delete from ledger.trust_observations")
    assert loop_rows() == []

    replayed = loop_post(dict(event), expect=200)
    assert replayed.json()["observation_rows"] == 1, replayed.text
    assert len(loop_rows()) == 1, loop_rows()


@pytest.mark.docker
def test_an_event_that_carries_no_observation_writes_no_row(
    loop_client: Any,
    loop_seed_sellers: Callable[..., None],
    loop_post: Callable[..., Any],
    loop_event: Callable[..., dict[str, Any]],
    loop_rows: Callable[[], list[tuple]],
) -> None:
    """The common case by a wide margin, and the one a projection must not invent out of."""
    loop_seed_sellers("store-northroast")
    response = loop_post(
        loop_event(
            "bid_placed:store-northroast:b-1",
            "bid_placed",
            store_id="store-northroast",
            payload={"bid_ref": "b-1", "store_id": "store-northroast", "offer": {}},
        )
    )
    assert response.json()["observation_rows"] == 0
    assert loop_rows() == []


@pytest.mark.docker
def test_the_append_still_succeeds_when_the_observation_cannot_be_written(
    loop_client: Any,
    loop_seed_sellers: Callable[..., None],
    loop_post: Callable[..., Any],
    loop_event: Callable[..., dict[str, Any]],
    loop_rows: Callable[[], list[tuple]],
) -> None:
    """A durable, chained, verifiable event must never be refused over a convergence step.

    The connection is injected as an object whose cursor raises, which is what a database
    that has gone away looks like from inside the projection. The append answers ``201``, the
    body reports ``observation_rows: 0`` so a deployment can tell this from "no observation in
    this event", and the event is readable back out of the chain.
    """

    class _Broken:
        def cursor(self) -> Any:
            raise RuntimeError("the ledger database went away")

        def rollback(self) -> None:
            return None

        def commit(self) -> None:  # pragma: no cover - never reached
            return None

    loop_seed_sellers("store-northroast")
    loop_client.app.state.ledger_connection = _Broken()
    try:
        response = loop_post(
            loop_event(
                "feedback:store-northroast:o-1001",
                "feedback",
                store_id="store-northroast",
                order_ref="o-1001",
                payload={
                    "matched_pitch": False,
                    "dim": "feedback_match",
                    "type": "mismatch_return",
                },
            )
        )
    finally:
        loop_client.app.state.ledger_connection = None
    assert response.json()["observation_rows"] == 0, response.text
    assert loop_rows() == []
    stored = loop_client.get("/events/feedback:store-northroast:o-1001")
    assert stored.status_code == 200, stored.text


@pytest.mark.docker
def test_a_return_already_in_the_chain_discounts_the_report_on_the_served_path(
    loop_client: Any,
    loop_seed_sellers: Callable[..., None],
    loop_post: Callable[..., Any],
    loop_event: Callable[..., dict[str, Any]],
    loop_rows: Callable[[], list[tuple]],
    loop_as_of: str,
) -> None:
    """R14's cross-check, reaching a served Beta — the half that had no production caller.

    The weight lands in the row AND in the replay, which is the only way it can land: a weight
    applied on one side only is R15 breaking.
    """
    from trust.feedback import RETURN_CONTRADICTION_FACTOR

    loop_seed_sellers("store-northroast")
    loop_post(
        loop_event(
            "refund:store-northroast:o-1001",
            "refund",
            store_id="store-northroast",
            order_ref="o-1001",
            payload={"order_ref": "o-1001", "amount": 100.0, "reason": "returned"},
        )
    )
    loop_post(
        loop_event(
            "feedback:store-northroast:o-1001",
            "feedback",
            store_id="store-northroast",
            order_ref="o-1001",
            payload={
                "matched_pitch": True,
                "reason": "yes_as_described",
                "dim": "feedback_match",
                "type": "fulfilled",
            },
        )
    )
    assert loop_rows() == [
        ("store-northroast", "feedback_match", "fulfilled", RETURN_CONTRADICTION_FACTOR)
    ]
    _assert_snapshot_matches_replay(loop_client, loop_as_of, "store-northroast", "feedback_match")


@pytest.mark.docker
def test_a_kept_promise_and_a_broken_one_serve_different_postures(
    loop_client: Any,
    loop_seed_sellers: Callable[..., None],
    loop_post: Callable[..., Any],
    loop_purchase: Callable[..., list[dict[str, Any]]],
    loop_rows: Callable[[], list[tuple]],
    loop_as_of: str,
) -> None:
    """The promise ledger, end to end over the served routes.

    Two real purchases with the same promise and different outcomes: one shop shipped in two
    days against a three-day estimate, the other took twenty-one. ``POST /reconcile`` grades
    them, and ``GET /snapshot`` — the exchange's own door — serves two different postures on
    ``shipped_on_time``, a dimension that had no transaction producer at all before this.
    """
    loop_seed_sellers("store-keeper", "store-breaker")
    for event in loop_purchase(
        "store-keeper",
        order_ref="o-keep",
        checkout_token="T-keep",
        delivery_estimate_days=3,
        fulfilled_at="2026-02-03T00:00:00.000Z",
    ) + loop_purchase(
        "store-breaker",
        order_ref="o-break",
        checkout_token="T-break",
        delivery_estimate_days=3,
        fulfilled_at="2026-02-22T00:00:00.000Z",
    ):
        loop_post(event)

    response = loop_client.post("/reconcile")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["reconciled"] == 2, report
    assert report["observations_written"] == 4, report

    assert ("store-breaker", "shipped_on_time", "contradicted", None) in loop_rows()
    assert ("store-keeper", "shipped_on_time", "fulfilled", None) in loop_rows()

    served = _snapshot(loop_client, loop_as_of)
    keeper = _dim(served["store-keeper"], "shipped_on_time")
    breaker = _dim(served["store-breaker"], "shipped_on_time")
    assert keeper["alpha"] > 2.0 and keeper["beta"] == 2.0, keeper
    assert breaker["beta"] > 2.0 and breaker["alpha"] == 2.0, breaker
    assert served["store-keeper"]["score"] > served["store-breaker"]["score"]

    for store_id in ("store-keeper", "store-breaker"):
        _assert_snapshot_matches_replay(loop_client, loop_as_of, store_id, "shipped_on_time")


@pytest.mark.docker
def test_reconciling_twice_does_not_double_the_delivery_finding(
    loop_client: Any,
    loop_seed_sellers: Callable[..., None],
    loop_post: Callable[..., Any],
    loop_purchase: Callable[..., list[dict[str, Any]]],
    loop_rows: Callable[[], list[tuple]],
) -> None:
    """ "Run it again" has to stay a real answer, and a doubled Beta is a real penalty."""
    loop_seed_sellers("store-breaker")
    for event in loop_purchase(
        "store-breaker",
        order_ref="o-break",
        checkout_token="T-break",
        delivery_estimate_days=3,
        fulfilled_at="2026-02-22T00:00:00.000Z",
    ):
        loop_post(event)

    first = loop_client.post("/reconcile").json()
    second = loop_client.post("/reconcile").json()
    assert first["observations_written"] == 2, first
    assert second["observations_written"] == 0, second
    assert second["already_present"]["offer_integrity"] == 2, second
    assert sorted(loop_rows()) == [
        ("store-breaker", "price_honored", "fulfilled", None),
        ("store-breaker", "shipped_on_time", "contradicted", None),
    ]
