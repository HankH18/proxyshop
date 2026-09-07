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

import contextlib
import itertools
import json
from collections.abc import Iterator
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


# --------------------------------------------------------------------------------------
# T-188 — the reconciled verdict has to be able to become a trust observation
#
# `reconciled_event` emits price_honored / discount_honored / price_comparable booleans and
# no `dim` and no `type`. `trust.ledger.replay.observations_from_events` silently skips any
# event whose payload lacks both, so the R4 half of the trust loop dead-ended AT THE SEAM:
# measured, accepted{total 100, discount 20%} + order_paid{total 130, no discount} reconciled
# correctly to price_honored=False / discount_honored=False and then produced
# `observations_from_events(...) == []` and `replay(...) == {}`.
#
# The translation lives on the EMITTER side on purpose. `apps/trust/src/ledger/**` is another
# ticket's scope and `observations_from_events` projects exactly
# {store_id, dim, type, observed_at}; so reconciliation emits events that consumer already
# understands rather than asking it to learn a new shape.
# --------------------------------------------------------------------------------------


def _dishonest_stream(make, *, observed_discount=None):
    """The measured reproduction: promised 100 at 20% off, charged 130.

    ``observed_discount=None`` is the exact stream from the ticket -- the webhook carries no
    ``discountApplications`` at all, so the promised discount is *incomparable*. Pass ``0.0``
    for the webhook that explicitly reports "no percentage discount was applied", which is a
    comparable statement and therefore a real contradiction.
    """
    return [
        _accepted(make, total=100.0, discount=20.0),
        _webhook(make, total=130.0, discount=observed_discount),
    ]


def test_a_dishonest_fulfilment_becomes_trust_observations(e6_make_event, e6_as_of):
    """R4 -> R12: a broken price promise and an ungradeable discount each land on a dim.

    The two halves grade apart on purpose. The webhook charged 130 against a promised 100 --
    comparable, and broken, so ``contradicted`` at the published 2.0. It said nothing at all
    about the discount -- so ``unsupported`` at 0.5, which holds the store's coverage and
    confidence down without accusing it of anything.
    """
    from apps.trust.src.reconcile import observation_events, reconciled_observations

    reconciled = reconcile(_dishonest_stream(e6_make_event))
    payload = _one(reconciled)["payload"]
    assert payload["price_honored"] is False and payload["discount_honored"] is False
    assert payload["price_comparable"] is True and payload["discount_comparable"] is False

    observations = reconciled_observations(reconciled)

    assert [(row["dim"], row["type"]) for row in observations] == [
        ("price_honored", "contradicted"),
        ("discount_honored", "unsupported"),
    ]
    assert all(row["store_id"] == "s-1" for row in observations)
    assert all(row["observed_at"] == _one(reconciled)["ts"] for row in observations)

    # ...and the same verdicts, wrapped as ledger events the existing consumer understands.
    events = observation_events(reconciled)
    assert [event["kind"] for event in events] == ["offer_integrity", "offer_integrity"]
    assert len({event["event_id"] for event in events}) == len(events), (
        "two observation events shared an event_id, which IS the ledger idempotency key"
    )


def test_a_comparable_broken_discount_is_a_contradiction_not_a_gap(e6_make_event):
    """The same order with a webhook that DID report its discounts: 20% promised, 0% applied."""
    from apps.trust.src.reconcile import reconciled_observations

    reconciled = reconcile(_dishonest_stream(e6_make_event, observed_discount=0.0))
    payload = _one(reconciled)["payload"]
    assert payload["discount_comparable"] is True and payload["discount_honored"] is False

    assert [(row["dim"], row["type"]) for row in reconciled_observations(reconciled)] == [
        ("price_honored", "contradicted"),
        ("discount_honored", "contradicted"),
    ]


def test_the_reconciled_verdicts_reach_the_scorer_through_replay(e6_make_event, e6_as_of):
    """The measured dead end, closed: replaying the emitted events moves the store's score."""
    from apps.trust.src.ledger.replay import observations_from_events, replay
    from apps.trust.src.reconcile import observation_events
    from apps.trust.src.scoring import prior_snapshot

    events = observation_events(reconcile(_dishonest_stream(e6_make_event, observed_discount=0.0)))

    projected = observations_from_events(events)
    assert len(projected) == 2, (
        "the emitted events do not carry what observations_from_events already requires"
    )

    snapshots = replay(events, as_of=e6_as_of)
    assert "s-1" in snapshots, "replay produced no snapshot for the dishonest store"

    dims = snapshots["s-1"]["dims"]
    assert float(dims["price_honored"]["beta"]) == 2.0 + 2.0
    assert float(dims["discount_honored"]["beta"]) == 2.0 + 2.0
    assert snapshots["s-1"]["score"] < prior_snapshot(as_of=e6_as_of)["score"], (
        "a dishonest fulfilment did not move the store's score"
    )


def test_an_honest_fulfilment_becomes_positive_evidence(e6_make_event, e6_as_of):
    """The mirror: a kept promise has to be able to EARN a store its score, not just avoid loss."""
    from apps.trust.src.ledger.replay import replay
    from apps.trust.src.reconcile import observation_events
    from apps.trust.src.scoring import prior_snapshot

    honest = reconcile(
        [
            _accepted(e6_make_event, total=100.0, discount=10.0),
            _webhook(e6_make_event, total=100.0, discount=10.0),
        ]
    )
    events = observation_events(honest)
    snapshots = replay(events, as_of=e6_as_of)

    dims = snapshots["s-1"]["dims"]
    assert float(dims["price_honored"]["alpha"]) == 2.0 + 1.0
    assert float(dims["discount_honored"]["alpha"]) == 2.0 + 1.0
    assert snapshots["s-1"]["score"] > prior_snapshot(as_of=e6_as_of)["score"]


def test_an_incomparable_webhook_yields_unsupported_and_never_a_contradiction(e6_make_event):
    """A malformed webhook is a gap, not a fraud.

    ``price_comparable`` is False when the webhook carried no total at all. The verdict field
    still reads False (fail-closed), and a translator that read only the verdict would
    manufacture a 2.0 contradiction out of a missing field -- a penalty the store cannot see
    coming and cannot appeal.
    """
    from apps.trust.src.reconcile import reconciled_observations

    reconciled = reconcile(
        [
            _accepted(e6_make_event, total=100.0, discount=None),
            _webhook(e6_make_event, total=None, discount=None),
        ]
    )
    payload = _one(reconciled)["payload"]
    assert payload["price_comparable"] is False and payload["price_honored"] is False

    observations = reconciled_observations(reconciled)

    assert [(row["dim"], row["type"]) for row in observations] == [("price_honored", "unsupported")]


def test_a_promise_with_no_discount_earns_no_discount_evidence(e6_make_event):
    """A store cannot farm ``discount_honored`` positives by promising no discount.

    ``discount_honored`` is trivially True when nothing was promised -- there is nothing to
    dishonour -- so translating that True into a ``fulfilled`` observation would pay a store
    for a promise it never made, on every single order.
    """
    from apps.trust.src.reconcile import reconciled_observations

    reconciled = reconcile(
        [
            _accepted(e6_make_event, total=100.0, discount=None),
            _webhook(e6_make_event, total=100.0, discount=None),
        ]
    )
    payload = _one(reconciled)["payload"]
    assert payload["discount_honored"] is True

    dims = [row["dim"] for row in reconciled_observations(reconciled)]
    assert dims == ["price_honored"], "an unpromised discount was graded as honored"


def test_the_translation_is_deterministic_and_leaves_reconcile_alone(e6_make_event):
    """Two runs produce identical events, and ``reconcile`` itself still emits only its own kind."""
    from apps.trust.src.reconcile import observation_events

    stream = _dishonest_stream(e6_make_event)
    emitted = reconcile(stream)

    assert [event["kind"] for event in emitted] == [RECONCILED_KIND], (
        "reconcile() must keep emitting exactly one reconciled event per order"
    )
    assert json.dumps(observation_events(emitted), sort_keys=True) == json.dumps(
        observation_events(reconcile(stream)), sort_keys=True
    )


def test_a_colon_in_a_store_or_order_reference_cannot_collapse_two_event_ids(e6_make_event):
    """``event_id`` IS the ledger's idempotency key, so an ambiguous one loses a whole order.

    ``reconciled:{store}:{order}`` scopes by store precisely because a platform ``order_id``
    is a PER-SHOP number -- but the scoping is only as good as the separator. Store ``s:x``
    with order ``1`` and store ``s`` with order ``x:1`` both spell ``reconciled:s:x:1``, and
    the ledger's dedup makes the second append a silent no-op: one shop's integrity finding
    disappears, which is the exact escape hatch the scoping was added to close.

    Found by this lane's own adversarial pass on the T-188 translation, which inherited the
    same scheme and doubled the exposure.
    """
    from apps.trust.src.reconcile import observation_events

    def _pair(store_id, order_ref, token, tag):
        return [
            _accepted(
                e6_make_event,
                store_id=store_id,
                order_ref=order_ref,
                checkout_token=token,
                discount=None,
                event_id=f"ev-accept-{tag}",
            ),
            _webhook(
                e6_make_event,
                store_id=store_id,
                order_ref=order_ref,
                checkout_token=token,
                total=130.0,
                discount=None,
                event_id=f"ev-paid-{tag}",
            ),
        ]

    stream = _pair("s:x", "1", "ck-1", "a") + _pair("s", "x:1", "ck-2", "b")

    reconciled = reconcile(stream)
    assert len(reconciled) == 2, "two distinct orders did not both reconcile"
    reconciled_ids = [event["event_id"] for event in reconciled]
    assert len(set(reconciled_ids)) == 2, (
        f"two unrelated shops' reconciliations share one idempotency key: {reconciled_ids}"
    )

    observation_ids = [event["event_id"] for event in observation_events(reconciled)]
    assert len(set(observation_ids)) == len(observation_ids), (
        f"two unrelated shops' integrity findings share one idempotency key: {observation_ids}"
    )


def test_an_ordinary_reference_keeps_its_plain_readable_event_id(e6_make_event):
    """The disambiguation must not make the common case unreadable.

    An id nobody can read in a log is its own kind of failure, so the escaping is required to
    be a no-op for every reference that carries no separator.
    """
    from apps.trust.src.reconcile import observation_events

    reconciled = reconcile([_accepted(e6_make_event), _webhook(e6_make_event, total=130.0)])

    assert reconciled[0]["event_id"] == "reconciled:s-1:o-1"
    assert [event["event_id"] for event in observation_events(reconciled)] == [
        "offer_integrity:s-1:o-1:price",
        "offer_integrity:s-1:o-1:discount",
    ]


# --------------------------------------------------------------------------------------
# Adversarial pass on the T-188 translation itself. Each of these turns a verdict that was
# merely RECORDED before into evidence that MOVES a score, so a verdict that was wrong in a
# way nobody had to care about is now a penalty or a reward a store cannot appeal.
# --------------------------------------------------------------------------------------


def test_a_promised_discount_at_or_below_the_tolerance_is_not_gradeable(e6_make_event):
    """A promise smaller than the tolerance we compare with is auto-honored: free evidence.

    ``_graded_fields`` gates on ``promised > 0.0`` while the verdict honors on
    ``observed >= promised - DISCOUNT_TOLERANCE``, so every promise in ``(0, 0.01]`` was
    graded AND honored no matter what the webhook reported. Measured: an offer promising
    0.01% off against a webhook reporting no discount at all minted a ``fulfilled`` on
    ``discount_honored`` -- on every order, forever, for a discount worth one ten-thousandth
    of the price. The zero case was already guarded; this is the same hole one epsilon over.
    """
    from apps.trust.src.reconcile import reconciled_observations

    reconciled = reconcile(
        [
            _accepted(e6_make_event, total=100.0, discount=DISCOUNT_TOLERANCE),
            _webhook(e6_make_event, total=100.0, discount=0.0),
        ]
    )
    payload = _one(reconciled)["payload"]
    assert payload["discount_honored"] is True, "the verdict itself is unchanged"

    dims = [row["dim"] for row in reconciled_observations(reconciled)]
    assert dims == ["price_honored"], (
        f"a promise of {DISCOUNT_TOLERANCE}% -- at the comparison tolerance -- was graded and "
        f"auto-honored, minting free positive evidence: {dims}"
    )

    # ...and a discount big enough to actually be compared is still graded, both ways.
    honored = reconcile(
        [
            _accepted(e6_make_event, total=100.0, discount=10.0),
            _webhook(e6_make_event, total=100.0, discount=10.0),
        ]
    )
    broken = reconcile(
        [
            _accepted(e6_make_event, total=100.0, discount=10.0),
            _webhook(e6_make_event, total=100.0, discount=0.0),
        ]
    )
    assert ("discount_honored", "fulfilled") in [
        (row["dim"], row["type"]) for row in reconciled_observations(honored)
    ]
    assert ("discount_honored", "contradicted") in [
        (row["dim"], row["type"]) for row in reconciled_observations(broken)
    ]


def test_a_promised_unit_price_is_not_comparable_to_an_observed_order_total(e6_make_event):
    """An honest three-unit order must not become the heaviest negative in the table.

    ``promised_price`` falls back to the offer's ``unit_price`` when no ``total_price`` was
    promised, and the comparison is against the webhook's ORDER TOTAL. Measured: an offer
    promising 30.00 a unit, three units bought, a webhook total of 90.00 -- an exactly honest
    order -- read ``price_honored: False``. Harmless while nothing scored it; a 2.0
    ``contradicted`` the moment the translation landed.

    A unit price and an order total are not the same quantity, so this is precisely what
    ``price_comparable`` means: the comparison could not be made. The observation is the
    published gap (``unsupported``), never an accusation.
    """
    from apps.trust.src.reconcile import reconciled_observations

    accepted = e6_make_event(
        "ev-accept",
        ACCEPTED_KIND,
        store_id="s-1",
        order_ref="o-1",
        payload={"checkout_token": "ck-1", "offer": {"product_ref": "p-1", "unit_price": 30.0}},
    )
    reconciled = reconcile([accepted, _webhook(e6_make_event, total=90.0, discount=None)])
    payload = _one(reconciled)["payload"]

    assert payload["promised_price"] == 30.0
    assert payload["observed_price"] == 90.0
    assert payload["price_comparable"] is False, (
        "a promised UNIT price was compared against an observed ORDER total"
    )

    assert [(row["dim"], row["type"]) for row in reconciled_observations(reconciled)] == [
        ("price_honored", "unsupported")
    ], "an honest multi-unit order was graded as a contradiction"


def test_an_observation_event_is_never_emitted_without_a_store_to_attribute_it_to(
    e6_make_event,
):
    """An unattributable finding must not be sealed into the chain unscored.

    ``observations_from_events`` skips any event with no ``store_id``, so emitting one writes
    a permanent, immutable, never-scored integrity finding into an append-only ledger. The
    reconciled event still records what happened; there is simply no honest store to charge.
    """
    from apps.trust.src.reconcile import observation_events, reconciled_observations

    accepted = _accepted(e6_make_event, store_id=None, discount=None)
    webhook = _webhook(e6_make_event, store_id=None, total=130.0, discount=None)
    reconciled = reconcile([accepted, webhook])

    assert _one(reconciled)["payload"]["price_honored"] is False, "the verdict still stands"
    assert reconciled_observations(reconciled) == []
    assert observation_events(reconciled) == []


def test_a_non_numeric_promised_discount_does_not_crash_the_translation(e6_make_event):
    """A payload read back out of the ledger is whatever was stored.

    ``reconciled_observations`` is documented to accept a whole ledger page, so a payload
    field that is not a number has to be a field it cannot grade -- not a bare ``ValueError``
    from ``float()`` that no caller's error handling recognises.
    """
    from apps.trust.src.reconcile import reconciled_observations

    stored = {
        "event_id": "reconciled:s-1:o-1",
        "ts": "2026-01-01T00:00:00Z",
        "kind": RECONCILED_KIND,
        "store_id": "s-1",
        "order_ref": "o-1",
        "payload": {
            "order_ref": "o-1",
            "promised_price": 100.0,
            "observed_price": 100.0,
            "price_honored": True,
            "price_comparable": True,
            "promised_discount_percentage": "twenty",
            "discount_honored": False,
            "discount_comparable": False,
        },
    }

    assert [(row["dim"], row["type"]) for row in reconciled_observations(stored)] == [
        ("price_honored", "fulfilled")
    ]


# =====================================================================================
# S1 links 7c and 9 — the SERVED trigger.
#
# Everything above this line proves the engine is correct. None of it proves that anything
# ever calls it, and that was exactly the defect: an audit driving the real product over
# HTTP measured ``reconcile`` with no production caller outside its own package, and one
# served run producing 0 reconciled events and 0 trust observations.
#
# So the tests below are about the wire, not the arithmetic. A request is served, a REAL
# purchase's events go in through the service's own published door, and a moved Beta comes
# back out of the service's own published read. ``_served_trust`` boots the real
# ``trust.main:create_app()`` on a real loopback socket: a ``TestClient`` would prove the
# handler runs, and would not prove the router is mounted by the app a deployment starts.
# =====================================================================================

SERVED_AS_OF = "2026-01-01T00:00:00Z"

#: The prior every dimension starts at (``fixtures/manifest.json``). Asserting a Beta moved
#: means asserting it left THIS, so it is named once rather than spelled 2.0 in six places.
PRIOR_ALPHA = PRIOR_BETA = 2.0

#: The shop the merchant stub answers as, and therefore the ``store_id`` an ``order_paid``
#: ledger event carries: ``merchant_svc.composition._store_id`` writes the shop domain,
#: because an unsigned ``X-Shopify-Shop-Domain`` header is the only shop identity a signed
#: delivery carries at all.
STUB_SHOP_DOMAIN = "proxyshop-demo.myshopify.com"

#: The PLATFORM's id for that same seller — what the exchange stamps into ``accepted``. The
#: two halves of one checkout therefore name the store differently, which is the second half
#: of the join problem and the reason the fold resolves an alias before it groups.
PLATFORM_STORE_ID = "store-northroast"

WEBHOOK_SECRET = "reconciliation-served-secret"
UNIT_PRICE = 389.0
DISCOUNT_PERCENTAGE = 10.0


@contextlib.contextmanager
def _served_trust(**state: Any) -> Iterator[Any]:
    """The real trust application on a real socket, with an in-memory ledger behind it."""
    import httpx
    from trust.events.store import InMemoryEventStore
    from trust.main import create_app

    from proxyshop_support.asgi_server import serve

    app = create_app()
    app.state.event_store = state.pop("event_store", None) or InMemoryEventStore()
    for name, value in state.items():
        setattr(app.state, name, value)
    with serve(app) as url, httpx.Client(base_url=url, timeout=30.0) as client:
        yield client


def _post_events(client: Any, events: Any) -> list[int]:
    """Append every event through the service's own published door. Statuses, in order."""
    statuses = []
    for event in events:
        response = client.post("/events", json=dict(event))
        assert response.status_code in (200, 201), f"{event.get('kind')}: {response.text}"
        statuses.append(response.status_code)
    return statuses


def _dims_of(client: Any, store_id: str = PLATFORM_STORE_ID) -> dict[str, Any]:
    """The store's per-dimension Betas, read back off the service's own replay door."""
    replayed = client.get("/events/replay", params={"snapshots": "true", "as_of": SERVED_AS_OF})
    assert replayed.status_code == 200, replayed.text
    snapshots = replayed.json()["snapshots"]
    assert store_id in snapshots, f"no snapshot for {store_id}: {sorted(snapshots)}"
    return snapshots[store_id]["dims"]


# --------------------------------------------------------------------------------------
# One REAL purchase, driven through the real components, once per session.
#
# Nothing below is a stand-in. The offer, the single-use code and the permalink come out of
# the exchange's own ``CheckoutProvider`` port; the order, the web-pixel beacon and the
# HMAC-signed ``orders/paid`` delivery come out of the real merchant stub over loopback; the
# beacon is parsed by ``merchant_svc.collector`` and the delivery is verified and projected
# by ``merchant_svc.install.webhooks`` + ``merchant_svc.composition.ledger_event`` — which is
# the exact projection the merchant service POSTs to ``/events`` in production.
# --------------------------------------------------------------------------------------
def _exchange_half(*, store_id: str = PLATFORM_STORE_ID) -> Any:
    """The exchange's own checkout port, called the way ``accept()`` calls it."""
    from exchange.checkout import resolve_provider
    from exchange.checkout.provider import CheckoutRequest

    domain = f"{store_id}.example.com"
    offer: dict[str, Any] = {
        "product_ref": "prod-northroast-hx",
        "unit_price": UNIT_PRICE,
        "total_price": UNIT_PRICE,
        "discount": {"type": "percentage", "value": DISCOUNT_PERCENTAGE},
        "checkout_url": f"https://{domain}/cart/44352913:1",
    }
    return resolve_provider("redirect").checkout(
        CheckoutRequest(
            auction_id="auc-served",
            bid_ref="bid-served",
            store_id=store_id,
            store_domain=domain,
            offer=offer,
            mode="redirect",
            now=1_700_000_000.0,
        )
    )


def _permalink_parts(url: str) -> tuple[int, int, str]:
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(url)
    variant, _, quantity = parts.path.rsplit("/", 1)[-1].partition(":")
    return int(variant), int(quantity or 1), (parse_qs(parts.query).get("discount") or [""])[0]


async def _merchant_half(variant_id: int, quantity: int, code: str) -> dict[str, Any]:
    """Redeem that permalink at the real merchant stub and keep the wire bytes."""
    import httpx
    from shopify_stub.app import create_app as create_stub
    from shopify_stub.testing import RecordingReceiver, StubClient

    from proxyshop_support.asgi_server import serve

    collector = RecordingReceiver()
    webhooks = RecordingReceiver()
    with (
        serve(create_stub()) as stub_url,
        serve(collector) as collector_url,
        serve(webhooks) as webhook_url,
    ):
        async with httpx.AsyncClient(base_url=stub_url, follow_redirects=False) as http:
            stub = StubClient(http, stub_url)
            seeded = await stub.seed(
                [
                    {
                        "variant_id": variant_id,
                        "product_id": 8123456,
                        "title": "Heat-exchange espresso machine",
                        "price": f"{UNIT_PRICE:.2f}",
                        "currency": "USD",
                        "sku": "HX-1",
                    }
                ]
            )
            assert seeded.status_code == 200, seeded.text
            await stub.configure(webhook_secret=WEBHOOK_SECRET)
            await stub.install_pixel(f"{collector_url}/collect")
            await stub.subscribe("ORDERS_PAID", f"{webhook_url}/webhooks/shopify")
            created = await stub.create_code(code, percentage=DISCOUNT_PERCENTAGE / 100.0)
            assert created.status_code == 200, created.text
            completion = await stub.buy(variant_id, quantity=quantity, code=code)
    return {
        "completion": completion,
        "beacons": list(collector.requests),
        "deliveries": list(webhooks.requests),
    }


@pytest.fixture(scope="session")
def e6_served_purchase() -> dict[str, Any]:
    """One real purchase, end to end, as the ledger events each side genuinely produces.

    Session-scoped because it starts four servers. Every consumer copies what it needs.
    """
    import asyncio
    import json

    from merchant_svc.collector import accept_pixel_event
    from merchant_svc.composition import ledger_event
    from merchant_svc.install.webhooks import handle_delivery, ledger_record

    checkout = _exchange_half()
    variant_id, quantity, code = _permalink_parts(checkout.permalink_url)
    merchant = asyncio.run(_merchant_half(variant_id, quantity, code))

    delivery = merchant["deliveries"][0]
    decision = handle_delivery(
        body=delivery["body"], headers=delivery["headers"], secret=WEBHOOK_SECRET
    )
    assert decision.accepted, f"{decision.status_code} {decision.reason}"
    order_paid = ledger_event(ledger_record(decision.event))

    observation = accept_pixel_event(json.loads(merchant["beacons"][0]["body"]))
    # `pixel/src/` is an empty `.gitkeep` and `merchant_svc.collector` stops at a
    # `PixelObservation`, so the ledger WRITE for this kind has no owner anywhere in the
    # tree. The observation itself is real — it was parsed out of the beacon the stub really
    # posted — and only the four fields the collector actually extracted are carried here.
    pixel = {
        "event_id": f"checkout_pixel:{observation.checkout_token}",
        "ts": SERVED_AS_OF,
        "kind": "checkout_pixel",
        "order_ref": observation.order_ref,
        "payload": {
            "checkout_token": observation.checkout_token,
            "order_ref": observation.order_ref,
            "client_id": observation.client_id,
        },
    }

    by_kind = {str(event["kind"]): dict(event) for event in checkout.events}
    return {
        "code": code,
        "checkout_token": checkout.checkout_token,
        "accepted": by_kind["accepted"],
        "code_created": by_kind["code_created"],
        "checkout_redirect": by_kind["checkout_redirect"],
        "order_paid": order_paid,
        "checkout_pixel": pixel,
        "completion": merchant["completion"],
    }


# --------------------------------------------------------------------------------------
# The premise, measured on that purchase rather than quoted from the docs.
# --------------------------------------------------------------------------------------
def test_the_two_halves_of_a_real_checkout_share_no_token_and_no_store_name(
    e6_served_purchase,
) -> None:
    """Both join blockers, from one real run, in one place.

    Neither is a claim about the past: a change that closes either of them turns this red,
    which is the point of pinning them here rather than describing them in a comment.
    """
    from apps.trust.src.reconcile import discount_codes_of

    accepted = e6_served_purchase["accepted"]
    order_paid = e6_served_purchase["order_paid"]

    assert accepted["payload"]["checkout_token"] != order_paid["payload"]["checkout_token"], (
        "the exchange and the merchant minted the same checkout token, which they never did"
    )
    assert accepted["store_id"] == PLATFORM_STORE_ID
    assert order_paid["store_id"] == STUB_SHOP_DOMAIN
    # …so the single-use code is the only value on both halves, and it really is on both.
    assert discount_codes_of(e6_served_purchase["code_created"]) == (
        e6_served_purchase["code"].upper(),
    )
    assert discount_codes_of(order_paid) == (e6_served_purchase["code"].upper(),)
    assert discount_codes_of(accepted) == ()


def test_the_route_is_mounted_by_the_app_a_deployment_starts() -> None:
    """``/reconcile`` is served, not merely written. That gap IS the shape of this defect."""
    from trust.main import create_app

    paths = create_app().openapi()["paths"]
    assert "/reconcile" in paths, sorted(paths)


# --------------------------------------------------------------------------------------
# The deliverable: a served request turns a real purchase into a trust update.
# --------------------------------------------------------------------------------------
def test_a_real_purchase_served_over_http_reconciles_and_moves_a_trust_beta(
    e6_served_purchase,
) -> None:
    """The two numbers the audit measured as zero, taken off a real socket.

    Every event posted here was produced by a real component. They go in through the
    service's own ``POST /events``, the fold is triggered through its own ``POST
    /reconcile``, and the trust update is read back through its own ``GET
    /events/replay?snapshots=true`` — three published doors and no in-process shortcut.
    """
    purchase = e6_served_purchase
    with _served_trust(reconcile_store_aliases={STUB_SHOP_DOMAIN: PLATFORM_STORE_ID}) as client:
        _post_events(
            client,
            [
                purchase["accepted"],
                purchase["code_created"],
                purchase["checkout_redirect"],
                purchase["checkout_pixel"],
                purchase["order_paid"],
            ],
        )

        landed = client.post("/reconcile")
        assert landed.status_code == 200, landed.text
        body = landed.json()
        assert body["reconciled"] > 0, f"reconciliation produced nothing: {body}"
        assert body["observations"] > 0, f"no trust observation was translated: {body}"
        assert body["appended"]["reconciled"] > 0, body
        assert body["appended"]["offer_integrity"] > 0, body

        chain = client.get("/events", params={"limit": 500}).json()["events"]
        kinds = [event["kind"] for event in chain]
        assert kinds.count(RECONCILED_KIND) == 1, kinds
        assert "offer_integrity" in kinds, kinds

        verdict = next(event for event in chain if event["kind"] == RECONCILED_KIND)["payload"]
        assert verdict["authority"] == WEBHOOK_KIND
        assert verdict["observed_price"] == pytest.approx(
            float(purchase["completion"]["total_price"])
        )
        assert verdict["pixel_missing"] is False, verdict

        dims = _dims_of(client)
        assert (dims["price_honored"]["alpha"], dims["price_honored"]["beta"]) != (
            PRIOR_ALPHA,
            PRIOR_BETA,
        ), f"the reconciled verdict reached no Beta: {dims['price_honored']}"


def test_the_promised_discount_cannot_be_graded_from_the_projected_webhook(
    e6_served_purchase,
) -> None:
    """A real gap, pinned where it will be noticed instead of read as a pass.

    The offer promised 10% and the merchant really gave 10% — the stub's signed body carries
    ``discount_applications[0].value_type == "percentage"``, which is exactly what
    ``_discount_percentage`` reads. But ``merchant_svc.composition.ledger_payload`` projects
    only ``discount_codes: [{"code": …}]`` onto the ledger event, so the observed percentage
    is unreadable and the honest verdict is ``unsupported`` (0.5) rather than ``fulfilled``.

    That is the fail-SAFE direction — an honest store is not paid for a promise this system
    could not check, and a dishonest one is not penalised for a projection it did not choose
    — and it is the correct behaviour for the input. What is wrong is upstream: the
    projection drops the evidence. If this test goes red because the type became
    ``fulfilled``, the projection has been fixed and this docstring is the record of why.
    """
    purchase = e6_served_purchase
    with _served_trust(reconcile_store_aliases={STUB_SHOP_DOMAIN: PLATFORM_STORE_ID}) as client:
        _post_events(client, [purchase["accepted"], purchase["code_created"]])
        _post_events(client, [purchase["order_paid"]])
        assert client.post("/reconcile").status_code == 200

        chain = client.get("/events", params={"limit": 500}).json()["events"]
        graded = {
            event["payload"]["field"]: event["payload"]["type"]
            for event in chain
            if event["kind"] == "offer_integrity"
        }
        assert graded == {"price": "fulfilled", "discount": "unsupported"}, graded


def test_the_served_fold_is_idempotent_and_lands_nothing_the_second_time(
    e6_served_purchase,
) -> None:
    """Re-running the fold over the same chain must not double-count a purchase.

    ``event_id`` is the ledger's idempotency key and every id this fold mints is derived
    from the order, so the second run appends nothing and the Betas do not move. A fold that
    has to be run exactly once is a fold nothing dares retry — and retrying is the whole
    reason it is safe to put this on a door rather than on the append of the webhook.
    """
    purchase = e6_served_purchase
    with _served_trust(reconcile_store_aliases={STUB_SHOP_DOMAIN: PLATFORM_STORE_ID}) as client:
        _post_events(client, [purchase["accepted"], purchase["code_created"]])
        _post_events(client, [purchase["checkout_pixel"], purchase["order_paid"]])

        first = client.post("/reconcile").json()
        after_first = _dims_of(client)
        second = client.post("/reconcile").json()
        after_second = _dims_of(client)

        assert first["reconciled"] == second["reconciled"] == 1
        assert second["appended"] == {"reconciled": 0, "offer_integrity": 0}, second
        assert second["already_present"] == first["appended"], second
        assert after_second == after_first, "the second fold moved a Beta"


def test_a_get_reports_what_the_chain_says_and_appends_nothing(e6_served_purchase) -> None:
    """The diagnosis half.

    An operator asking "did this purchase grade, and how?" must not have to write to an
    append-only ledger to find out.
    """
    purchase = e6_served_purchase
    with _served_trust(reconcile_store_aliases={STUB_SHOP_DOMAIN: PLATFORM_STORE_ID}) as client:
        _post_events(
            client, [purchase["accepted"], purchase["code_created"], purchase["order_paid"]]
        )
        before = client.get("/events/head").json()["length"]

        report = client.get("/reconcile")
        assert report.status_code == 200, report.text
        body = report.json()
        assert body["reconciled"] == 1, body
        assert body["observations"] == 2, body
        assert [event["kind"] for event in body["events"]] == [RECONCILED_KIND], body

        assert client.get("/events/head").json()["length"] == before, "a GET appended"


# --------------------------------------------------------------------------------------
# R4 on the served path: the webhook is authoritative, and "pixel AND/OR webhook".
# --------------------------------------------------------------------------------------
def _overcharged(purchase: dict[str, Any], total: str) -> dict[str, Any]:
    """The same real webhook event with the merchant's own total changed: an overcharge."""
    event = json.loads(json.dumps(purchase["order_paid"]))
    event["event_id"] = f"{event['event_id']}-overcharge"
    event["payload"]["total_price"] = total
    event["payload"]["current_total_price"] = total
    return event


def _pixel_reporting(purchase: dict[str, Any], total: float) -> dict[str, Any]:
    """The same real beacon, made to agree with the PROMISE rather than with the webhook."""
    event = json.loads(json.dumps(purchase["checkout_pixel"]))
    event["payload"]["total_price"] = total
    return event


def test_where_the_pixel_and_the_webhook_disagree_the_served_verdict_follows_the_webhook(
    e6_served_purchase,
) -> None:
    """R4, on the wire: the webhook is authoritative and the pixel is a witness.

    The pixel echoes the promise exactly — the shape a store's own obliging analytics would
    take — while the webhook says the buyer was charged far more. A reconciler that averaged
    the two, or preferred whichever agreed with the offer, serves a ``fulfilled`` here. The
    TRUST SCORE is the assertion and not just the payload: a verdict nothing scores is not
    an outcome, which is the whole reason this lane exists.
    """
    purchase = e6_served_purchase
    with _served_trust(reconcile_store_aliases={STUB_SHOP_DOMAIN: PLATFORM_STORE_ID}) as client:
        _post_events(
            client,
            [
                purchase["accepted"],
                purchase["code_created"],
                _pixel_reporting(purchase, UNIT_PRICE),
                _overcharged(purchase, "900.00"),
            ],
        )
        body = client.post("/reconcile").json()
        assert body["reconciled"] == 1, body

        chain = client.get("/events", params={"limit": 500}).json()["events"]
        verdict = next(event for event in chain if event["kind"] == RECONCILED_KIND)["payload"]
        assert verdict["price_honored"] is False, verdict
        assert verdict["observed_price"] == pytest.approx(900.0), verdict
        assert verdict["pixel_price"] == pytest.approx(UNIT_PRICE), verdict
        assert verdict["pixel_agrees"] is False, verdict

        integrity = [
            event["payload"]
            for event in chain
            if event["kind"] == "offer_integrity" and event["payload"]["field"] == "price"
        ]
        assert [row["type"] for row in integrity] == ["contradicted"], integrity

        dims = _dims_of(client)
        assert dims["price_honored"]["beta"] > PRIOR_BETA, dims
        assert dims["price_honored"]["alpha"] == PRIOR_ALPHA, dims


def test_a_purchase_the_webhook_alone_observed_still_reconciles_and_still_scores(
    e6_served_purchase,
) -> None:
    """R4 allows "pixel event AND/OR order webhook". This is the AND/OR that must grade.

    A dropped beacon is the normal case — an ad blocker, a closed tab — and refusing to
    reconcile without one would let a store suppress its own grading by breaking its own
    analytics. The gap is recorded (``pixel_missing``), never fatal.
    """
    purchase = e6_served_purchase
    with _served_trust(reconcile_store_aliases={STUB_SHOP_DOMAIN: PLATFORM_STORE_ID}) as client:
        _post_events(
            client, [purchase["accepted"], purchase["code_created"], purchase["order_paid"]]
        )
        body = client.post("/reconcile").json()
        assert body["inputs"]["checkout_pixel"] == 0, body
        assert body["reconciled"] == 1, body
        assert body["observations"] == 2, body

        chain = client.get("/events", params={"limit": 500}).json()["events"]
        verdict = next(event for event in chain if event["kind"] == RECONCILED_KIND)["payload"]
        assert verdict["pixel_missing"] is True, verdict

        assert _dims_of(client)["price_honored"]["alpha"] > PRIOR_ALPHA


def test_a_purchase_only_the_pixel_saw_is_reported_rather_than_silently_dropped(
    e6_served_purchase,
) -> None:
    """The other half of R4's AND/OR, and the honest answer to it.

    With no webhook there is no authority, so there is no integrity verdict to serve: R4
    forbids grading a promise against a client-side beacon and inventing one is the exact
    failure this engine exists to prevent. What must NOT happen is the purchase becoming
    invisible. The served report names every input kind it read, so "one beacon in, no
    webhook, nothing graded" is a number an operator can act on rather than an absence.
    """
    purchase = e6_served_purchase
    with _served_trust(reconcile_store_aliases={STUB_SHOP_DOMAIN: PLATFORM_STORE_ID}) as client:
        _post_events(
            client,
            [purchase["accepted"], purchase["code_created"], purchase["checkout_pixel"]],
        )
        body = client.post("/reconcile").json()
        assert body["reconciled"] == 0, body
        assert body["observations"] == 0, body
        assert body["inputs"]["checkout_pixel"] == 1, body
        assert body["inputs"]["order_paid"] == 0, body
        assert body["inputs"]["accepted"] == 1, body
        # …and the beacon is still in the chain, unconsumed, waiting for its webhook.
        kinds = [event["kind"] for event in client.get("/events").json()["events"]]
        assert kinds.count(PIXEL_KIND) == 1, kinds


# --------------------------------------------------------------------------------------
# The door has to survive what an append-only, unauthenticated ledger will hold.
# --------------------------------------------------------------------------------------
def test_one_unjoinable_webhook_cannot_take_the_reconciliation_door_down(
    e6_served_purchase,
) -> None:
    """``POST /events`` admits an ``order_paid`` naming no order. ``reconcile`` refuses one.

    Both are right, and together they are a permanent denial of service: the row cannot be
    evicted (the ledger's ``BEFORE UPDATE OR DELETE … ENABLE ALWAYS`` trigger), so a single
    unauthenticated append would stop every later purchase in that ledger from ever grading.
    The door therefore sets the unjoinable webhook aside and COUNTS it — the refusal is kept,
    which is what ``ReconciliationInputError`` is for, but it is kept as a number rather than
    as an outage.
    """
    purchase = e6_served_purchase
    with _served_trust(reconcile_store_aliases={STUB_SHOP_DOMAIN: PLATFORM_STORE_ID}) as client:
        poison = {
            "event_id": "order_paid:no-join-key",
            "ts": SERVED_AS_OF,
            "kind": WEBHOOK_KIND,
            "store_id": STUB_SHOP_DOMAIN,
            "payload": {"total_price": "1.00"},
        }
        assert client.post("/events", json=poison).status_code == 201
        _post_events(
            client, [purchase["accepted"], purchase["code_created"], purchase["order_paid"]]
        )

        landed = client.post("/reconcile")
        assert landed.status_code == 200, landed.text
        body = landed.json()
        assert body["unjoinable_webhooks"] == 1, body
        assert body["reconciled"] == 1, "one poison row stopped an honest purchase grading"
        assert _dims_of(client)["price_honored"]["alpha"] > PRIOR_ALPHA


def test_the_fold_refuses_a_ledger_longer_than_it_will_hold_at_once(e6_make_event) -> None:
    """The door is unauthenticated and the join holds every checkout event at once.

    ``GET /events/replay?snapshots=true`` already carries a ceiling for the same reason. The
    ceiling is asserted where it is enforced — by lowering it for one call — rather than by
    writing a hundred thousand rows to watch it trip.
    """
    from fastapi import HTTPException
    from trust.events.store import InMemoryEventStore
    from trust.reconcile.routes import MAX_RECONCILE_INPUT_EVENTS, read_checkout_events

    assert MAX_RECONCILE_INPUT_EVENTS > 0
    store = InMemoryEventStore()
    store.append(_accepted(e6_make_event, event_id="ev-a"))
    store.append(_webhook(e6_make_event, event_id="ev-b"))

    assert read_checkout_events(store, {}, limit=2).counts[ACCEPTED_KIND] == 1
    with pytest.raises(HTTPException) as refused:
        read_checkout_events(store, {}, limit=1)
    assert refused.value.status_code == 422
    assert refused.value.detail["error"] == "ledger_too_long_to_reconcile"


# --------------------------------------------------------------------------------------
# The store alias: the two halves of one checkout name the store differently.
# --------------------------------------------------------------------------------------
def test_without_the_store_alias_the_two_halves_never_meet(e6_served_purchase) -> None:
    """The control. Remove the alias and the same four real events reconcile to nothing.

    ``reconcile`` namespaces every join key by store on purpose — a Shopify ``order_id`` is a
    per-shop number — so an offer filed under ``store-northroast`` and an order filed under
    ``proxyshop-demo.myshopify.com`` cannot meet however good the code bridge is. This is why
    the fold resolves the platform's own roster before it groups, and this is the measurement
    that the resolution is load-bearing rather than decorative.
    """
    purchase = e6_served_purchase
    with _served_trust(reconcile_store_aliases={}) as client:
        _post_events(
            client,
            [
                purchase["accepted"],
                purchase["code_created"],
                purchase["checkout_pixel"],
                purchase["order_paid"],
            ],
        )
        body = client.post("/reconcile").json()
        assert body["store_aliases_applied"] == 0, body
        assert body["reconciled"] == 0, body


def test_an_alias_claimed_by_two_platform_stores_is_refused_rather_than_guessed() -> None:
    """A domain two sellers claim names neither of them.

    Adopting one would file a real order against a store that did not take it, and
    ``app.sellers.domain`` carries no unique index to stop two rows claiming one host. The
    fail-closed answer is to leave the event under the name it arrived with, which reconciles
    to nothing rather than to somebody else's promise.
    """
    from trust.reconcile.routes import resolve_store_aliases

    assert resolve_store_aliases([("store-a", "shared.myshopify.com")]) == {
        "shared.myshopify.com": "store-a"
    }
    assert (
        resolve_store_aliases(
            [("store-a", "shared.myshopify.com"), ("store-b", "shared.myshopify.com")]
        )
        == {}
    )


def test_an_alias_never_renames_a_store_the_roster_already_knows_by_that_name() -> None:
    """A domain that is also some seller's own ``store_id`` is not an alias for anything.

    The map only ever translates a name the platform does NOT know into one it does. A
    ``store_id`` the roster already holds is already the answer, and rewriting it would merge
    two real sellers into one trust subject.
    """
    from trust.reconcile.routes import resolve_store_aliases

    assert resolve_store_aliases(
        [("shop.myshopify.com", "other.example"), ("store-b", "shop.myshopify.com")]
    ) == {"other.example": "shop.myshopify.com"}


# --------------------------------------------------------------------------------------
# The door the exchange actually reads. `GET /events/replay?snapshots=true` proves the
# verdicts are scoreable; `GET /snapshot` is what `exchange.composition.HttpTrustSnapshot`
# calls over HTTP for R12 eligibility, and it reads `ledger.trust_observations` — the table
# `trust.verification.persistence` writes a VERIFICATION outcome into. R12's "one trust
# system, not two" is a claim about that table, so it is asserted against that table.
# --------------------------------------------------------------------------------------
@pytest.mark.docker
def test_a_served_reconcile_moves_the_snapshot_the_exchange_reads(
    e6_served_purchase, ledger_clean: Any, pg_admin: Any, worker_database: str
) -> None:
    """Nothing injected: a real Postgres ledger, the real roster, the real published read.

    The roster row is the one piece of configuration this needs and it is not a fixture
    convenience — it is the statement "platform store ``store-northroast`` IS the shop
    ``proxyshop-demo.myshopify.com``", which is exactly what ``app.sellers`` is for and the
    only authority that can join the exchange's half of a checkout to the merchant's.
    """
    import os

    import httpx
    from trust.main import create_app

    from proxyshop_support.asgi_server import serve
    from proxyshop_support.postgres import role_dsn

    purchase = e6_served_purchase
    with pg_admin.cursor() as cursor:
        cursor.execute(
            "insert into app.sellers (store_id, domain, business_identity, tier) values "
            "(%s, %s, 'co-northroast', 'hosted')",
            (PLATFORM_STORE_ID, STUB_SHOP_DOMAIN),
        )

    app = create_app()
    app.state.ledger_connection = None
    previous = os.environ.get("PROXYSHOP_LEDGER_DSN")
    os.environ["PROXYSHOP_LEDGER_DSN"] = role_dsn("trust_rw", database=worker_database)
    try:
        with serve(app) as url, httpx.Client(base_url=url, timeout=30.0) as client:
            before = client.get("/snapshot", params={"as_of": SERVED_AS_OF})
            assert before.status_code == 200, before.text
            assert before.json()[PLATFORM_STORE_ID]["dims"]["price_honored"] == {
                "alpha": PRIOR_ALPHA,
                "beta": PRIOR_BETA,
                "decayed_at": "2026-01-01T00:00:00.000Z",
            }, before.text

            _post_events(
                client,
                [
                    purchase["accepted"],
                    purchase["code_created"],
                    purchase["checkout_pixel"],
                    purchase["order_paid"],
                ],
            )
            landed = client.post("/reconcile")
            assert landed.status_code == 200, landed.text
            body = landed.json()
            # No alias was injected: this map came out of `app.sellers` on this request's
            # own connection, which is the half an injected one can never prove.
            assert body["store_aliases_applied"] == 1, body
            assert body["reconciled"] == 1, body
            assert body["observations_persisted"] is True, body
            assert body["observations_written"] == 2, body

            after = client.get("/snapshot", params={"as_of": SERVED_AS_OF})
            assert after.status_code == 200, after.text
            price = after.json()[PLATFORM_STORE_ID]["dims"]["price_honored"]
            assert price["alpha"] > PRIOR_ALPHA, price
            assert (
                after.json()[PLATFORM_STORE_ID]["score"] > before.json()[PLATFORM_STORE_ID]["score"]
            ), "a kept promise did not move the score the exchange ranks on"
    finally:
        if previous is None:
            os.environ.pop("PROXYSHOP_LEDGER_DSN", None)
        else:
            os.environ["PROXYSHOP_LEDGER_DSN"] = previous

    with pg_admin.cursor() as cursor:
        cursor.execute(
            "select dim, observation_type, weight, verification_id, event_seq "
            "from ledger.trust_observations order by dim"
        )
        rows = cursor.fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("discount_honored", "unsupported"),
        ("price_honored", "fulfilled"),
    ], rows
    # `weight` absent means exactly 1.0 to the scorer: a machine comparison against the
    # authoritative webhook is worth its full published type weight (the weight channel is
    # R14's, for reports whose SOURCE is discountable).
    assert all(row[2] is None for row in rows), rows
    # No verification behind these, and an `event_seq` that names the `offer_integrity` row
    # they descend from — the column `db/migrations/0002` reserved for an observation whose
    # source is a commerce event, and which nothing in the tree wrote until now.
    assert all(row[3] is None and row[4] is not None for row in rows), rows


@pytest.mark.docker
def test_a_second_served_reconcile_writes_no_second_observation_row(
    e6_served_purchase, ledger_clean: Any, pg_admin: Any, worker_database: str
) -> None:
    """``ledger.trust_observations`` has no unique index over its real columns.

    ``trust.verification.persistence`` defends that table by never emitting the second
    INSERT, which it can do because its own idempotency is arbitrated one table up. This
    fold cannot borrow that: its appends are no-ops on a re-run, so gating the row on "the
    append inserted" would make a failed write unrepeatable. It arbitrates on ``event_seq``
    instead — a real database check — and this is the measurement that it holds.
    """
    import os

    import httpx
    from trust.main import create_app

    from proxyshop_support.asgi_server import serve
    from proxyshop_support.postgres import role_dsn

    purchase = e6_served_purchase
    with pg_admin.cursor() as cursor:
        cursor.execute(
            "insert into app.sellers (store_id, domain, business_identity, tier) values "
            "(%s, %s, 'co-northroast', 'hosted')",
            (PLATFORM_STORE_ID, STUB_SHOP_DOMAIN),
        )

    app = create_app()
    app.state.ledger_connection = None
    previous = os.environ.get("PROXYSHOP_LEDGER_DSN")
    os.environ["PROXYSHOP_LEDGER_DSN"] = role_dsn("trust_rw", database=worker_database)
    try:
        with serve(app) as url, httpx.Client(base_url=url, timeout=30.0) as client:
            _post_events(
                client,
                [purchase["accepted"], purchase["code_created"], purchase["order_paid"]],
            )
            first = client.post("/reconcile").json()
            after_first = client.get("/snapshot", params={"as_of": SERVED_AS_OF}).json()
            second = client.post("/reconcile").json()
            after_second = client.get("/snapshot", params={"as_of": SERVED_AS_OF}).json()
    finally:
        if previous is None:
            os.environ.pop("PROXYSHOP_LEDGER_DSN", None)
        else:
            os.environ["PROXYSHOP_LEDGER_DSN"] = previous

    assert first["observations_written"] == 2, first
    assert second["observations_written"] == 0, second
    assert after_second == after_first, "the second fold double-counted a purchase"

    with pg_admin.cursor() as cursor:
        cursor.execute("select count(*) from ledger.trust_observations")
        assert cursor.fetchone()[0] == 2


@pytest.mark.docker
def test_two_folds_racing_write_one_observation_row_each_and_not_two(
    e6_served_purchase, ledger_clean: Any, pg_admin: Any, worker_database: str
) -> None:
    """The door takes no credential, so two concurrent ``POST /reconcile`` is normal traffic.

    ``WHERE NOT EXISTS`` is a check-then-insert and ``ledger.trust_observations`` has no
    unique index over its real columns, so without ``OBSERVATION_LOCK_KEY`` both folds can
    read "absent" for one ``event_seq`` and both write — a doubled Beta about a real store on
    a money path. The chain appends cannot double (``CHAIN_LOCK_KEY`` plus a UNIQUE
    constraint); the rows derived from them could.

    A race can pass by luck, so read this as a gate that can only go red when the property is
    actually broken — and it does go red often enough to be worth having. Measured on this
    tree with the ``pg_advisory_xact_lock`` line deleted and nothing else changed: three runs,
    two red, the observation table holding THREE rows for two observations
    (``observations_written`` came back ``[1, 2]``). With the line restored, green.
    """
    import os
    import threading

    import httpx
    from trust.main import create_app

    from proxyshop_support.asgi_server import serve
    from proxyshop_support.postgres import role_dsn

    purchase = e6_served_purchase
    with pg_admin.cursor() as cursor:
        cursor.execute(
            "insert into app.sellers (store_id, domain, business_identity, tier) values "
            "(%s, %s, 'co-northroast', 'hosted')",
            (PLATFORM_STORE_ID, STUB_SHOP_DOMAIN),
        )

    app = create_app()
    app.state.ledger_connection = None
    previous = os.environ.get("PROXYSHOP_LEDGER_DSN")
    os.environ["PROXYSHOP_LEDGER_DSN"] = role_dsn("trust_rw", database=worker_database)
    started = threading.Barrier(2)
    answers: list[Any] = []
    try:
        with serve(app) as url, httpx.Client(base_url=url, timeout=30.0) as client:
            _post_events(
                client,
                [purchase["accepted"], purchase["code_created"], purchase["order_paid"]],
            )

            def fold() -> None:
                with httpx.Client(base_url=url, timeout=30.0) as racer:
                    started.wait(timeout=10)
                    answers.append(racer.post("/reconcile"))

            racers = [threading.Thread(target=fold) for _ in range(2)]
            for racer in racers:
                racer.start()
            for racer in racers:
                racer.join(timeout=60)
    finally:
        if previous is None:
            os.environ.pop("PROXYSHOP_LEDGER_DSN", None)
        else:
            os.environ["PROXYSHOP_LEDGER_DSN"] = previous

    assert [answer.status_code for answer in answers] == [200, 200], answers
    written = sorted(answer.json()["observations_written"] for answer in answers)
    assert written == [0, 2], f"both folds claimed to write: {written}"

    with pg_admin.cursor() as cursor:
        cursor.execute("select count(*), count(distinct event_seq) from ledger.trust_observations")
        assert cursor.fetchone() == (2, 2)
