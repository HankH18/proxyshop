"""T-061 — reconciliation: the ``order_paid`` webhook is the only authority (R4).

The ticket has one rule and two ways of getting it wrong, so this file attacks both:

* a **wrong pixel must not fail an honest store**, and
* a **right pixel must not mask a dishonest webhook**.

The first three tests therefore do not check the two happy paths; they hold the accepted
offer and the webhook fixed, vary the pixel across absent / agreeing / wildly high / wildly
low / price-less, and require the verdict triple to be *invariant*. Any implementation that
averages the two sources, prefers whichever agrees with the offer, or falls back to the pixel
when the webhook is thin fails that loop even though it would pass both happy paths.

The join is attacked separately. ``reconcile`` is two passes over the stream for one measured
reason: two join keys are only known to be the same order once some event carries both, and
that event may arrive LAST. So the union-find tests put the merging event last and then run
every permutation of the stream, which is the shape a one-pass filer silently fails —
silently, because the affected order simply never reconciles rather than raising.

Everything here is pure data: no clock, no socket, no database. Determinism is itself a
requirement (a ``uuid4`` or a ``now()`` in the emitted event would make the ledger
un-replayable), so two runs over one stream are compared for equality.
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace
from typing import Any

import pytest

from apps.trust.src.reconcile import (
    ACCEPTED_KIND,
    DISCOUNT_TOLERANCE,
    PIXEL_KIND,
    PRICE_TOLERANCE,
    RECONCILED_KIND,
    WEBHOOK_KIND,
    ReconciliationInputError,
    reconcile,
)

# --------------------------------------------------------------------------------------
# Builders. They take ``e6_make_event`` (the shared E6 factory) as their first argument so
# they stay plain module-level functions and define no fixture name in this directory's flat
# namespace.
# --------------------------------------------------------------------------------------


def _accepted(
    make,
    *,
    total: float = 100.0,
    discount: float | None = 10.0,
    discount_type: str = "percentage",
    order_ref: str | None = "o-1",
    checkout_token: str | None = "ck-1",
    store_id: str | None = "s-1",
    event_id: str = "ev-accept",
) -> dict[str, Any]:
    """The ``accepted`` offer: the promise being graded, never evidence about the outcome."""
    offer: dict[str, Any] = {"product_ref": "p-1", "unit_price": total, "total_price": total}
    if discount is not None:
        offer["discount"] = {"type": discount_type, "value": discount}
    payload: dict[str, Any] = {"offer": offer}
    if checkout_token is not None:
        payload["checkout_token"] = checkout_token
    return make(event_id, ACCEPTED_KIND, store_id=store_id, order_ref=order_ref, payload=payload)


def _pixel(
    make,
    *,
    total: float | None = 100.0,
    discount: float | None = 10.0,
    order_ref: str | None = "o-1",
    checkout_token: str | None = "ck-1",
    store_id: str | None = "s-1",
    event_id: str = "ev-pixel",
) -> dict[str, Any]:
    """The lossy client-side observation. Recorded, never consulted."""
    payload: dict[str, Any] = {"clientId": "cid-1"}
    if checkout_token is not None:
        payload["checkout_token"] = checkout_token
    if total is not None:
        payload["total_price"] = total
    if discount is not None:
        payload["discountApplications"] = [{"type": "percentage", "value": discount}]
    return make(event_id, PIXEL_KIND, store_id=store_id, order_ref=order_ref, payload=payload)


def _webhook(
    make,
    *,
    total: float | None = 100.0,
    discount: float | None = 10.0,
    order_ref: str | None = "o-1",
    checkout_token: str | None = "ck-1",
    order_id: str | None = None,
    store_id: str | None = "s-1",
    event_id: str = "ev-paid",
    ts: str = "2026-01-02T03:04:05Z",
) -> dict[str, Any]:
    """The ``orders/paid`` webhook: the record every integrity comparison derives from."""
    payload: dict[str, Any] = {}
    if checkout_token is not None:
        payload["checkout_token"] = checkout_token
    if order_id is not None:
        payload["order_id"] = order_id
    if total is not None:
        payload["total_price"] = total
    if discount is not None:
        payload["discountApplications"] = [{"type": "percentage", "value": discount}]
    return make(
        event_id, WEBHOOK_KIND, store_id=store_id, order_ref=order_ref, payload=payload, ts=ts
    )


def _one(emitted: list[dict[str, Any]]) -> dict[str, Any]:
    """The single reconciled event a run must have emitted, asserted to be single."""
    assert isinstance(emitted, list), f"reconcile() returned {type(emitted).__name__}, not a list"
    reconciled = [event for event in emitted if event["kind"] == RECONCILED_KIND]
    assert len(reconciled) == 1, (
        f"expected exactly one {RECONCILED_KIND!r} event for one order, got {len(reconciled)}"
    )
    assert len(reconciled) == len(emitted), "reconcile() emitted events of some other kind"
    return reconciled[0]


def _verdict(emitted: list[dict[str, Any]]) -> tuple[Any, Any, Any]:
    """The three fields the pixel is forbidden from influencing."""
    payload = _one(emitted)["payload"]
    return payload["price_honored"], payload["discount_honored"], payload["observed_price"]


# --------------------------------------------------------------------------------------
# R4 — the authority rule, stated as a property
# --------------------------------------------------------------------------------------


def test_varying_the_pixel_arbitrarily_never_changes_a_verdict(e6_make_event):
    """R4 as a property: with the offer and the webhook fixed, no pixel moves the grade.

    The two happy paths (a matching pixel, a missing pixel) are satisfied by an
    implementation that averages the sources or prefers the source agreeing with the offer.
    This loop is not: it holds one accepted offer and one webhook fixed and swings the pixel
    from absent, through agreeing, to wildly high, to wildly low, to carrying no price at
    all, and requires ``price_honored`` / ``discount_honored`` / ``observed_price`` to be
    identical for every one of them — under an honest webhook AND under an overcharging one.
    """
    pixels = {
        "absent": None,
        "agreeing with the webhook": _pixel(e6_make_event, total=100.0, discount=10.0),
        "wildly high": _pixel(e6_make_event, total=999.99, discount=0.0),
        "wildly low": _pixel(e6_make_event, total=1.0, discount=95.0),
        "echoing the promise": _pixel(e6_make_event, total=100.0, discount=10.0),
        "carrying no price": _pixel(e6_make_event, total=None, discount=None),
    }
    webhooks = {
        "honest": (_webhook(e6_make_event, total=100.0, discount=10.0), (True, True, 100.0)),
        "overcharging": (_webhook(e6_make_event, total=120.0, discount=0.0), (False, False, 120.0)),
    }

    for webhook_name, (webhook, expected) in webhooks.items():
        for pixel_name, pixel in pixels.items():
            stream = [_accepted(e6_make_event, total=100.0, discount=10.0), webhook]
            if pixel is not None:
                stream.insert(1, pixel)
            assert _verdict(reconcile(stream)) == expected, (
                f"a {pixel_name!r} pixel changed the verdict on an {webhook_name} webhook — "
                "every integrity comparison must derive from the webhook alone (R4)"
            )

    # ...and the loop above is not vacuously true: the two webhooks really do grade apart.
    assert webhooks["honest"][1] != webhooks["overcharging"][1]


def test_a_dropped_pixel_still_reconciles_and_records_the_gap(e6_make_event):
    """R4: no pixel is not an error — the order still grades, and ``pixel_missing`` is True.

    Refusing to reconcile without the pixel would hand every store a way to suppress its own
    grading by breaking its own analytics, so the webhook alone must produce a full verdict.
    """
    payload = _one(reconcile([_accepted(e6_make_event), _webhook(e6_make_event)]))["payload"]

    assert payload["pixel_missing"] is True, "a dropped pixel left no visible gap"
    assert payload["pixel_price"] is None
    assert payload["pixel_agrees"] is False, "an absent pixel cannot agree with anything"
    assert payload["price_honored"] is True, "the webhook alone failed to grade the price"
    assert payload["discount_honored"] is True


def test_a_present_pixel_is_recorded_as_present_and_its_agreement_reported(e6_make_event):
    """R4: the pixel is *recorded* — present/absent, its price, and whether it agreed.

    Agreement is an observation about the integration (a persistently disagreeing pixel is a
    finding a later ticket grades) and is reported both ways without touching a verdict.
    """
    agreeing = _one(
        reconcile(
            [_accepted(e6_make_event), _pixel(e6_make_event, total=100.0), _webhook(e6_make_event)]
        )
    )["payload"]
    assert agreeing["pixel_missing"] is False, "a present pixel was recorded as a gap"
    assert agreeing["pixel_price"] == 100.0
    assert agreeing["pixel_agrees"] is True

    disagreeing = _one(
        reconcile(
            [_accepted(e6_make_event), _pixel(e6_make_event, total=90.0), _webhook(e6_make_event)]
        )
    )["payload"]
    assert disagreeing["pixel_missing"] is False
    assert disagreeing["pixel_price"] == 90.0
    assert disagreeing["pixel_agrees"] is False, (
        "a 90.00 pixel was said to agree with a 100.00 webhook"
    )
    assert disagreeing["price_honored"] is True, (
        "a broken pixel was allowed to fail an honest store"
    )


def test_pixel_agreement_is_measured_against_the_webhook_and_not_against_the_promise(e6_make_event):
    """R4: ``pixel_agrees`` compares pixel to *webhook*, so it cannot become a second verdict.

    The subtle mistake is comparing the pixel to the accepted offer: a pixel echoing the
    promise while the webhook overcharged would then read as "agrees", and a consumer would
    have a second, pixel-derived opinion about a promise. Here the pixel matches the promise
    exactly and must still be reported as disagreeing, because the webhook says 120.00.
    """
    payload = _one(
        reconcile(
            [
                _accepted(e6_make_event, total=100.0),
                _pixel(e6_make_event, total=100.0),
                _webhook(e6_make_event, total=120.0),
            ]
        )
    )["payload"]

    assert payload["pixel_agrees"] is False, (
        "the pixel was compared against the promise, not the webhook"
    )
    assert payload["price_honored"] is False, "a matching pixel masked a webhook overcharge"
    assert payload["observed_price"] == 120.0


# --------------------------------------------------------------------------------------
# One event per order, and the identity of that order
# --------------------------------------------------------------------------------------


def test_one_reconciled_event_per_order_names_the_order_store_and_checkout(e6_make_event):
    """R4: exactly one ``reconciled`` event, addressed to the right order, store and checkout.

    The envelope matters as much as the verdict: an event carrying the wrong ``store_id``
    moves a trust dimension on an uninvolved merchant.
    """
    event = _one(
        reconcile([_accepted(e6_make_event), _pixel(e6_make_event), _webhook(e6_make_event)])
    )

    assert event["kind"] == RECONCILED_KIND
    assert event["order_ref"] == "o-1"
    assert event["store_id"] == "s-1"
    assert event["payload"]["order_ref"] == "o-1"
    assert event["payload"]["checkout_token"] == "ck-1"
    assert event["payload"]["product_ref"] == "p-1", "the graded product came from nowhere"
    assert event["payload"]["authority"] == WEBHOOK_KIND, "the event does not name its authority"


def test_a_repeated_webhook_for_one_order_still_emits_exactly_one_reconciled_event(e6_make_event):
    """R4: duplicate inputs do not multiply the grade — one order, one reconciled event.

    Shopify retries ``orders/paid``. Emitting one reconciled event per delivery would count
    the same promise twice against the store's trust score.
    """
    stream = [
        _accepted(e6_make_event),
        _webhook(e6_make_event, event_id="ev-paid-1"),
        _pixel(e6_make_event),
        _webhook(e6_make_event, event_id="ev-paid-2"),
        _accepted(e6_make_event, event_id="ev-accept-2"),
    ]
    event = _one(reconcile(stream))
    assert event["order_ref"] == "o-1"


def test_several_independent_orders_each_reconcile_exactly_once(e6_make_event):
    """R4: a whole ledger page of interleaved orders yields one event per order, no crosstalk.

    ``reconcile`` is handed pages, not curated triples, so the orders in one stream must not
    contaminate each other: order B's overcharge must not follow order A home.
    """
    stream = [
        _accepted(
            e6_make_event, order_ref="o-a", checkout_token="ck-a", event_id="a-acc", total=50.0
        ),
        _accepted(
            e6_make_event, order_ref="o-b", checkout_token="ck-b", event_id="b-acc", total=50.0
        ),
        _pixel(e6_make_event, order_ref="o-b", checkout_token="ck-b", event_id="b-pix"),
        _webhook(
            e6_make_event, order_ref="o-a", checkout_token="ck-a", event_id="a-pay", total=50.0
        ),
        _webhook(
            e6_make_event, order_ref="o-b", checkout_token="ck-b", event_id="b-pay", total=75.0
        ),
    ]

    emitted = reconcile(stream)

    assert [event["order_ref"] for event in emitted] == ["o-a", "o-b"], (
        "orders must be emitted once each, in the order their webhooks arrived"
    )
    by_order = {event["order_ref"]: event["payload"] for event in emitted}
    assert by_order["o-a"]["price_honored"] is True
    assert by_order["o-a"]["pixel_missing"] is True, "order A's pixel came from order B"
    assert by_order["o-b"]["price_honored"] is False, "order B's overcharge was not graded"
    assert by_order["o-b"]["pixel_missing"] is False


# --------------------------------------------------------------------------------------
# The join
# --------------------------------------------------------------------------------------


def test_events_join_on_the_checkout_token_alone(e6_make_event):
    """R4: a checkout that never got an order id still joins, on ``checkout_token``.

    With no order id anywhere, the reconciled event falls back to the join key itself for its
    ``order_ref`` — an identity derived from the input rather than invented.
    """
    stream = [
        _accepted(e6_make_event, order_ref=None, checkout_token="ck-9"),
        _webhook(e6_make_event, order_ref=None, checkout_token="ck-9"),
    ]

    event = _one(reconcile(stream))

    assert event["order_ref"] == "ck-9"
    assert event["payload"]["checkout_token"] == "ck-9"
    assert event["payload"]["price_honored"] is True


def test_events_join_on_the_order_ref_alone(e6_make_event):
    """R4: an order with no checkout token at all still joins, on ``order_ref``."""
    stream = [
        _accepted(e6_make_event, order_ref="o-7", checkout_token=None),
        _pixel(e6_make_event, order_ref="o-7", checkout_token=None),
        _webhook(e6_make_event, order_ref="o-7", checkout_token=None),
    ]

    event = _one(reconcile(stream))

    assert event["order_ref"] == "o-7"
    assert event["payload"]["checkout_token"] is None, "a checkout token was invented"
    assert event["payload"]["pixel_missing"] is False


def test_a_merging_key_arriving_last_still_joins_the_pixel_to_the_webhook(e6_make_event):
    """R4: the union-find path — a pixel keyed only by checkout, a webhook keyed only by order.

    Neither event names the other's key, so they are the same order only because a third
    event (the accepted offer) carries both. That merging event is placed LAST here on
    purpose: a one-pass implementation that files each event as it arrives has already put
    the webhook in its own group, and the order then silently never reconciles — no
    exception, no event, just a missing grade.
    """
    stream = [
        _pixel(e6_make_event, order_ref=None, checkout_token="ck-m"),
        _webhook(e6_make_event, order_ref="o-m", checkout_token=None),
        _accepted(e6_make_event, order_ref="o-m", checkout_token="ck-m"),
    ]

    event = _one(reconcile(stream))

    assert event["order_ref"] == "o-m"
    assert event["payload"]["checkout_token"] == "ck-m", "the offer's checkout token was lost"
    assert event["payload"]["pixel_missing"] is False, (
        "the pixel was not joined to the webhook, so the merge happened too early"
    )
    assert event["payload"]["price_honored"] is True


def test_the_join_does_not_depend_on_the_order_events_arrive_in(e6_make_event):
    """R4: every permutation of one order's three events produces the identical event.

    A ledger page has no arrival guarantee, and the union-find only earns its keep if the
    result is order-independent. All six permutations of the merging stream are compared
    against each other, which also re-states determinism: nothing in the output may come from
    stream position.
    """
    events = [
        _pixel(e6_make_event, order_ref=None, checkout_token="ck-m"),
        _webhook(e6_make_event, order_ref="o-m", checkout_token=None),
        _accepted(e6_make_event, order_ref="o-m", checkout_token="ck-m"),
    ]

    outputs = [reconcile(list(permutation)) for permutation in itertools.permutations(events)]

    assert all(len(output) == 1 for output in outputs), (
        "some arrival order failed to reconcile the order at all"
    )
    first = outputs[0]
    for index, output in enumerate(outputs[1:], start=1):
        assert output == first, f"permutation {index} produced a different reconciled event"


def test_the_stream_may_be_a_one_shot_iterator(e6_make_event):
    """R4: ``reconcile`` consumes its input once, so a generator is a legal stream.

    The two-pass design is over an internal buffer, not over the argument. An implementation
    that iterated ``events`` twice would produce nothing at all here while still passing every
    list-based test — the classic exhausted-generator regression.
    """
    stream = iter([_accepted(e6_make_event), _pixel(e6_make_event), _webhook(e6_make_event)])

    event = _one(reconcile(stream))

    assert event["payload"]["price_honored"] is True


def test_unrelated_event_kinds_are_ignored_rather_than_crashed_on(e6_make_event):
    """R4: whole ledger pages come in, so foreign kinds must pass through untouched.

    The foreign events here deliberately include ones with no join key and no payload at all,
    because a filter that reads the payload before it checks the kind would raise on them.
    """
    stream = [
        e6_make_event("ev-pitch", "pitch_sent", store_id="s-1", payload={"text": "hello"}),
        _accepted(e6_make_event),
        e6_make_event("ev-noise", "shipment_delivered"),
        _pixel(e6_make_event),
        e6_make_event("ev-blank", "claim_verified", payload={}),
        _webhook(e6_make_event),
        e6_make_event(
            "ev-recon", RECONCILED_KIND, order_ref="o-1", payload={"price_honored": False}
        ),
    ]

    event = _one(reconcile(stream))

    assert event["payload"]["price_honored"] is True, (
        "a foreign event leaked into the grade for this order"
    )


def test_an_order_with_no_webhook_emits_nothing(e6_make_event):
    """R4: with no authoritative record there is nothing to grade against, so nothing is said.

    Inventing a verdict from the pixel is the single thing R4 forbids, and "emit an event with
    the pixel's numbers" is exactly how that gets reintroduced.
    """
    assert reconcile([_accepted(e6_make_event), _pixel(e6_make_event)]) == []
    assert reconcile([_pixel(e6_make_event)]) == []


def test_an_order_with_no_accepted_offer_emits_nothing(e6_make_event):
    """R4: with no promise there is no integrity question, so a bare webhook grades nothing.

    A store that never made an offer through the network cannot have broken one, and emitting
    a ``price_honored: False`` for a checkout the network never touched is a fabricated
    penalty.
    """
    assert reconcile([_webhook(e6_make_event), _pixel(e6_make_event)]) == []


def test_a_webhook_carrying_no_join_key_at_all_is_rejected_loudly(e6_make_event):
    """R4: an unattributable webhook raises rather than being dropped.

    Silently dropping it would let a store escape reconciliation entirely by omitting one
    field from its webhook payload, which is a cheaper attack than any price manipulation.
    """
    orphan = _webhook(e6_make_event, order_ref=None, checkout_token=None, order_id=None)

    with pytest.raises(ReconciliationInputError) as excinfo:
        reconcile([_accepted(e6_make_event), orphan])

    assert issubclass(ReconciliationInputError, ValueError), (
        "callers catching ValueError must keep catching this"
    )
    assert WEBHOOK_KIND in str(excinfo.value), "the error does not say which kind of event failed"


def test_an_unjoinable_non_webhook_event_is_skipped_without_taking_the_stream_down(e6_make_event):
    """R4: only the *webhook* is unattributable-fatal; a keyless offer or pixel is just noise.

    An accepted offer with no keys grades nothing on its own, so it is dropped — but the other
    orders in the same page must still reconcile rather than the whole call raising.
    """
    stream = [
        _accepted(e6_make_event, order_ref=None, checkout_token=None, event_id="ev-orphan-acc"),
        _pixel(e6_make_event, order_ref=None, checkout_token=None, event_id="ev-orphan-pix"),
        _accepted(e6_make_event, order_ref="o-ok", checkout_token="ck-ok"),
        _webhook(e6_make_event, order_ref="o-ok", checkout_token="ck-ok"),
    ]

    event = _one(reconcile(stream))

    assert event["order_ref"] == "o-ok"


# --------------------------------------------------------------------------------------
# Tolerances
# --------------------------------------------------------------------------------------


def test_a_charge_at_the_price_tolerance_is_honored_and_a_cent_beyond_it_is_not(e6_make_event):
    """R4: ``PRICE_TOLERANCE`` absorbs a float round-trip and nothing larger.

    The boundary is inclusive — the tolerance exists so a JSON round-trip of 100.00 does not
    read as an overcharge — while one cent over, which is a real overcharge, must fail.
    """
    at_boundary = _one(
        reconcile(
            [
                _accepted(e6_make_event, total=100.0),
                _webhook(e6_make_event, total=100.0 + PRICE_TOLERANCE),
            ]
        )
    )["payload"]
    assert at_boundary["price_honored"] is True, (
        f"a charge exactly {PRICE_TOLERANCE} over the promise must stay inside the tolerance"
    )

    one_cent_over = _one(
        reconcile([_accepted(e6_make_event, total=100.0), _webhook(e6_make_event, total=100.01)])
    )["payload"]
    assert one_cent_over["price_honored"] is False, (
        "a one-cent overcharge slipped past the tolerance"
    )
    assert one_cent_over["observed_price"] == 100.01
    assert one_cent_over["promised_price"] == 100.0, (
        "the promise did not come from the accepted offer"
    )


def test_charging_less_than_promised_is_honored(e6_make_event):
    """R4: the price comparison is one-sided — a goodwill discount is not a broken promise.

    A two-sided comparison would penalise a store for charging its customer *less* than the
    offer said, which is the opposite of the behaviour this system is trying to encourage.
    """
    payload = _one(
        reconcile([_accepted(e6_make_event, total=100.0), _webhook(e6_make_event, total=42.0)])
    )["payload"]

    assert payload["price_honored"] is True, "a store was penalised for undercharging"
    assert payload["observed_price"] == 42.0


def test_a_larger_discount_than_promised_is_honored_and_a_smaller_one_is_not(e6_make_event):
    """R4: the discount comparison is one-sided the other way, with its own tolerance.

    More discount than promised is a promise kept and then some; less is the promise broken.
    The boundary at ``DISCOUNT_TOLERANCE`` below the promise is still honored, one point below
    that is not.
    """

    def discount_honored(observed: float) -> bool:
        payload = _one(
            reconcile(
                [
                    _accepted(e6_make_event, total=100.0, discount=10.0),
                    _webhook(e6_make_event, total=90.0, discount=observed),
                ]
            )
        )["payload"]
        return payload["discount_honored"]

    assert discount_honored(15.0) is True, "a larger discount than promised was graded as broken"
    assert discount_honored(10.0) is True
    assert discount_honored(10.0 - DISCOUNT_TOLERANCE) is True, (
        "the inclusive tolerance boundary was treated as a shortfall"
    )
    assert discount_honored(9.0) is False, "a store promised 10% and gave 9% and was not caught"
    assert discount_honored(0.0) is False, "a store promised 10% and gave none and was not caught"


def test_an_offer_promising_no_percentage_discount_is_graded_on_price_alone(e6_make_event):
    """R4: no percentage promise means no discount verdict to fail.

    Two shapes carry no percentage promise: no ``discount`` block at all, and a fixed-amount
    discount (which lands in the totals and is graded by ``price_honored`` instead — adding a
    currency figure to a percentage figure would produce a number meaning nothing).
    """
    no_discount = _one(
        reconcile(
            [
                _accepted(e6_make_event, total=100.0, discount=None),
                _webhook(e6_make_event, total=100.0, discount=None),
            ]
        )
    )["payload"]
    assert no_discount["promised_discount_percentage"] is None
    assert no_discount["discount_honored"] is True, "an unpromised discount was graded as broken"

    fixed_amount = _one(
        reconcile(
            [
                _accepted(e6_make_event, total=100.0, discount=5.0, discount_type="fixed_amount"),
                _webhook(e6_make_event, total=95.0, discount=None),
            ]
        )
    )["payload"]
    assert fixed_amount["promised_discount_percentage"] is None, (
        "a fixed-amount discount was read as a percentage promise"
    )
    assert fixed_amount["discount_honored"] is True
    assert fixed_amount["price_honored"] is True


def test_percentage_discount_applications_are_summed_and_fixed_amounts_ignored(e6_make_event):
    """R4: Shopify sends a list of applications; only the percentage ones are comparable.

    Two stacked 5% applications satisfy a 10% promise. A fixed-amount application in the same
    list must not be added to that percentage total, which would silently inflate the observed
    discount and let a store buy a passing grade with a $5 coupon.
    """
    webhook = e6_make_event(
        "ev-paid",
        WEBHOOK_KIND,
        store_id="s-1",
        order_ref="o-1",
        payload={
            "checkout_token": "ck-1",
            "total_price": 90.0,
            "discountApplications": [
                {"type": "percentage", "value": 5.0},
                {"type": "fixed_amount", "value": 40.0},
                {"type": "percentage", "value": 5.0},
            ],
        },
    )

    payload = _one(reconcile([_accepted(e6_make_event, total=100.0, discount=10.0), webhook]))[
        "payload"
    ]

    assert payload["observed_discount_percentage"] == 10.0, (
        "a fixed-amount application was summed into the percentage total"
    )
    assert payload["discount_honored"] is True


def test_the_observed_price_falls_back_to_current_total_price(e6_make_event):
    """R4: a webhook spelling its total ``current_total_price`` is still an authoritative total.

    Shopify sends both spellings depending on the payload version, and treating the second as
    "no price" would turn every such order into an ungraded gap.
    """
    webhook = e6_make_event(
        "ev-paid",
        WEBHOOK_KIND,
        store_id="s-1",
        order_ref="o-1",
        payload={"checkout_token": "ck-1", "current_total_price": 120.0},
    )

    payload = _one(reconcile([_accepted(e6_make_event, total=100.0, discount=None), webhook]))[
        "payload"
    ]

    assert payload["observed_price"] == 120.0
    assert payload["price_honored"] is False, "the fallback total was read but not graded"


def test_a_webhook_carrying_no_price_at_all_cannot_honor_the_promise(e6_make_event):
    """R4: with no observed total there is no comparison, and the verdict fails closed.

    The important half is the second assertion: the pixel's price is right there in the same
    stream, and it must not be borrowed to fill the hole the webhook left.
    """
    webhook = e6_make_event(
        "ev-paid",
        WEBHOOK_KIND,
        store_id="s-1",
        order_ref="o-1",
        payload={"checkout_token": "ck-1"},
    )

    payload = _one(
        reconcile(
            [_accepted(e6_make_event, total=100.0), _pixel(e6_make_event, total=100.0), webhook]
        )
    )["payload"]

    assert payload["observed_price"] is None, "a missing webhook total was filled in from the pixel"
    assert payload["price_honored"] is False
    assert payload["pixel_price"] == 100.0, "the pixel is still recorded, just not consulted"


# --------------------------------------------------------------------------------------
# Determinism and serialisability
# --------------------------------------------------------------------------------------


def test_the_emitted_event_id_and_timestamp_derive_from_the_input(e6_make_event):
    """R4: no ``uuid4`` and no clock — the ledger has to be replayable byte for byte.

    A generated id or a wall-clock timestamp would make the same stream produce a different
    ledger on every replay, which breaks the hash chain these events are sealed into.

    The id is scoped by STORE as well as by order, and that is not decoration: ``event_id`` is
    the ledger's idempotency key (D16), while a platform ``order_id`` is a per-shop number, so
    an id built from the order alone collides between two shops and the second shop's
    reconciliation lands as a silent duplicate no-op.
    """
    webhook = _webhook(e6_make_event, ts="2026-02-03T04:05:06Z")

    event = _one(reconcile([_accepted(e6_make_event), _pixel(e6_make_event), webhook]))

    assert event["event_id"] == f"{RECONCILED_KIND}:s-1:o-1", (
        "the emitted event id is not a pure function of the store and order it grades"
    )
    assert event["ts"] == "2026-02-03T04:05:06Z", (
        "the emitted timestamp did not come from the webhook that caused it"
    )

    # The property behind the literal, asserted directly so a future edit to the id FORMAT
    # cannot quietly reintroduce a colliding id: two shops carrying the same platform order
    # number must not produce the same event id.
    def one_shop(store_id: str) -> dict[str, Any]:
        return _one(
            reconcile(
                [
                    _accepted(e6_make_event, store_id=store_id, checkout_token=f"ck-{store_id}"),
                    _webhook(
                        e6_make_event,
                        store_id=store_id,
                        checkout_token=f"ck-{store_id}",
                        order_id="1001",
                    ),
                ]
            )
        )

    first, second = one_shop("shop-a"), one_shop("shop-b")
    assert first["event_id"] != second["event_id"], (
        "two shops sharing a platform order_id produced the same ledger idempotency key, so "
        "one shop's reconciliation would be swallowed as a duplicate of the other's"
    )


def test_two_runs_over_the_same_stream_produce_equal_output(e6_make_event):
    """R4: reconciliation is a pure function of its input, so replay is idempotent.

    Built as two independently constructed but equal streams, so the equality cannot be
    satisfied by returning the same cached object.
    """

    def stream() -> list[dict[str, Any]]:
        return [
            _accepted(e6_make_event, order_ref="o-x", checkout_token="ck-x"),
            _pixel(e6_make_event, order_ref="o-x", checkout_token="ck-x", total=88.0),
            _webhook(e6_make_event, order_ref="o-x", checkout_token="ck-x", total=99.0),
        ]

    first = reconcile(stream())
    second = reconcile(stream())

    assert first == second, (
        "two runs over equal streams disagreed — something non-deterministic leaked in"
    )
    assert first is not second


def test_emitted_events_are_json_serialisable(e6_make_event):
    """R4: the reconciled event goes into the ledger and over the wire, so it must serialise.

    A ``Decimal``, a ``datetime`` or a set anywhere in the payload would pass every equality
    assertion in this file and then fail at the ledger boundary.
    """
    emitted = reconcile(
        [
            _accepted(e6_make_event),
            _pixel(e6_make_event, total=90.0),
            _webhook(e6_make_event, total=120.0),
            _accepted(e6_make_event, order_ref="o-2", checkout_token="ck-2", event_id="a2"),
            _webhook(e6_make_event, order_ref="o-2", checkout_token="ck-2", event_id="w2"),
        ]
    )

    encoded = json.dumps(emitted)

    assert json.loads(encoded) == emitted, "the payload did not survive a JSON round-trip"


def test_records_may_be_objects_rather_than_mappings(e6_make_event):
    """R4: events reach this module as model objects too, not only as dicts.

    ``reconcile`` reads its fields through an accessor that falls back to attributes precisely
    so a caller holding row objects or pydantic models does not have to render them to dicts
    first. A regression to plain subscripting would raise here while every dict test stayed
    green.
    """
    accepted = SimpleNamespace(
        event_id="ev-accept",
        ts="2026-01-01T00:00:00Z",
        kind=ACCEPTED_KIND,
        store_id="s-1",
        order_ref="o-obj",
        payload={
            "checkout_token": "ck-obj",
            "offer": {
                "product_ref": "p-1",
                "unit_price": 100.0,
                "total_price": 100.0,
                "discount": {"type": "percentage", "value": 10.0},
            },
        },
    )
    webhook = SimpleNamespace(
        event_id="ev-paid",
        ts="2026-01-01T01:00:00Z",
        kind=WEBHOOK_KIND,
        store_id="s-1",
        order_ref="o-obj",
        payload={
            "checkout_token": "ck-obj",
            "total_price": 130.0,
            "discountApplications": [{"type": "percentage", "value": 0.0}],
        },
    )

    payload = _one(reconcile([accepted, webhook]))["payload"]

    assert payload["order_ref"] == "o-obj"
    assert payload["observed_price"] == 130.0
    assert payload["price_honored"] is False
    assert payload["discount_honored"] is False
