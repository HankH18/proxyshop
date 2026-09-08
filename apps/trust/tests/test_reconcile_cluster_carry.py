"""The minted trust observations carry the cluster the auction was decided in.

WHY THIS IS A TEST AND NOT A FIELD
----------------------------------
``trust.feedback.engine`` routes a computed trust delta to a bandit arm by reading
``payload["cluster_id"]`` off the event that carried it. Every ``accepted`` event the
exchange writes already names its cluster --- ``apps/exchange/src/auction/state.py`` composes
``{state, intent_id, cluster_id}`` from the auction record for *every* transition --- but the
``reconciled`` verdict and the ``offer_integrity`` observations minted from it carried no
cluster at all, so the one signal that says whether a purchase was honest reached no arm.

Two failure directions, and only one of them is loud:

* **not carrying it** is inert: the arm learns nothing from a real purchase;
* **carrying the wrong one** moves a posterior for a cluster the purchase never happened in,
  and nothing downstream can tell that value apart from a real one.

So the second direction is what most of this file attacks. The cluster may come from the
joined ``accepted`` event and from nothing else --- not the webhook, not the pixel, not the
fulfilment, and not a neighbouring auction's offer --- and when the offer named none, the key
is **absent**, never ``null`` / ``""`` / a stand-in.

Everything here is pure data: no clock, no socket, no database. The builders follow
``test_reconciliation.py``'s convention (plain module-level functions taking the shared
``e6_make_event`` factory as their first argument) so this file defines no fixture name in
this directory's flat namespace.
"""

from __future__ import annotations

from typing import Any

from contracts.ledger import validate_ledger_payload

from apps.trust.src.reconcile import (
    ACCEPTED_KIND,
    OBSERVATION_KIND,
    PIXEL_KIND,
    RECONCILED_KIND,
    WEBHOOK_KIND,
    observation_events,
    reconcile,
)

# The delivery kind is not re-exported by the package (`apps/trust/src/reconcile/__init__.py`
# publishes the other five), so it is taken from the module that defines it rather than
# spelled a second time here.
from apps.trust.src.reconcile.engine import FULFILLED_KIND

# --------------------------------------------------------------------------------------
# What the engine minted BEFORE the cluster was carried, measured by running it. These are
# the ledger's idempotency keys (D16), so a re-run over a chain holding the pre-change rows
# must still spell exactly these; pinning the literals is what makes that checkable without
# trusting the current engine to author its own expectation.
# --------------------------------------------------------------------------------------
PRE_CHANGE_RECONCILED_ID = "reconciled:s-1:o-1"
PRE_CHANGE_OBSERVATION_IDS = (
    "offer_integrity:s-1:o-1:price",
    "offer_integrity:s-1:o-1:discount",
)

#: The exact key set of a ``reconciled`` payload before the carry, in order. Pinned so that
#: "add one key" cannot quietly become "add one key and drop another".
PRE_CHANGE_RECONCILED_KEYS = (
    "order_ref",
    "checkout_token",
    "product_ref",
    "bid_ref",
    "price_honored",
    "discount_honored",
    "price_comparable",
    "discount_comparable",
    "observed_price",
    "observed_discount_percentage",
    "promised_price",
    "promised_price_basis",
    "promised_discount_percentage",
    "shipped_on_time",
    "delivery_comparable",
    "promised_delivery_days",
    "observed_dispatch_days",
    "fulfilled_at",
    "fulfilment_missing",
    "authority",
    "pixel_missing",
    "pixel_price",
    "pixel_agrees",
)

#: The same, for one ``offer_integrity`` observation.
PRE_CHANGE_OBSERVATION_KEYS = (
    "bid_ref",
    "field",
    "promised",
    "observed",
    "dim",
    "type",
    "observed_at",
    "reconciled_event_id",
    "authority",
)

PAID_TS = "2026-01-02T03:04:05Z"


# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------
def _accepted(
    make,
    *,
    cluster_id: Any = "cl-7",
    total: float = 100.0,
    discount: float | None = 10.0,
    delivery_days: float | None = None,
    order_ref: str | None = "o-1",
    checkout_token: str | None = "ck-1",
    store_id: str | None = "s-1",
    bid_ref: str = "bid-1",
    event_id: str = "ev-accept",
) -> dict[str, Any]:
    """The ``accepted`` offer, optionally naming the cluster its auction was decided in.

    ``cluster_id=None`` omits the key entirely, which is what an offer minted before the
    exchange stamped clusters --- or a hand-posted one --- actually looks like.
    """
    offer: dict[str, Any] = {"product_ref": "p-1", "unit_price": total, "total_price": total}
    if discount is not None:
        offer["discount"] = {"type": "percentage", "value": discount}
    if delivery_days is not None:
        offer["delivery_estimate_days"] = delivery_days
    payload: dict[str, Any] = {"bid_ref": bid_ref, "offer": offer}
    if checkout_token is not None:
        payload["checkout_token"] = checkout_token
    if cluster_id is not None:
        payload["cluster_id"] = cluster_id
    return make(event_id, ACCEPTED_KIND, store_id=store_id, order_ref=order_ref, payload=payload)


def _webhook(
    make,
    *,
    total: float | None = 120.0,
    order_ref: str | None = "o-1",
    checkout_token: str | None = "ck-1",
    store_id: str | None = "s-1",
    cluster_id: Any = None,
    event_id: str = "ev-paid",
    ts: str = PAID_TS,
) -> dict[str, Any]:
    """The ``orders/paid`` webhook. ``cluster_id`` here is the STORE's word, never a route."""
    payload: dict[str, Any] = {}
    if checkout_token is not None:
        payload["checkout_token"] = checkout_token
    if total is not None:
        payload["total_price"] = total
    if cluster_id is not None:
        payload["cluster_id"] = cluster_id
    return make(
        event_id, WEBHOOK_KIND, store_id=store_id, order_ref=order_ref, payload=payload, ts=ts
    )


def _pixel(
    make,
    *,
    total: float | None = 120.0,
    order_ref: str | None = "o-1",
    checkout_token: str | None = "ck-1",
    store_id: str | None = "s-1",
    cluster_id: Any = None,
    event_id: str = "ev-pixel",
) -> dict[str, Any]:
    """The lossy client-side beacon. Its cluster would be the BROWSER's word."""
    payload: dict[str, Any] = {"clientId": "cid-1"}
    if checkout_token is not None:
        payload["checkout_token"] = checkout_token
    if total is not None:
        payload["total_price"] = total
    if cluster_id is not None:
        payload["cluster_id"] = cluster_id
    return make(event_id, PIXEL_KIND, store_id=store_id, order_ref=order_ref, payload=payload)


def _fulfilled(
    make,
    *,
    order_ref: str = "o-1",
    store_id: str = "s-1",
    cluster_id: Any = None,
    fulfilled_at: str = "2026-01-03T03:04:05Z",
    event_id: str = "ev-shipped",
) -> dict[str, Any]:
    """The delivery notice. Also the store's word, and also not a routing decision."""
    payload: dict[str, Any] = {"order_ref": order_ref, "fulfilled_at": fulfilled_at}
    if cluster_id is not None:
        payload["cluster_id"] = cluster_id
    return make(
        event_id,
        FULFILLED_KIND,
        store_id=store_id,
        order_ref=order_ref,
        payload=payload,
        ts=fulfilled_at,
    )


def _fold(stream: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The real fold: ``(reconciled events, offer_integrity events)`` for one stream."""
    reconciled = reconcile(stream)
    return reconciled, observation_events(reconciled)


def _one(reconciled: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(reconciled) == 1, f"expected one {RECONCILED_KIND!r} event, got {len(reconciled)}"
    return reconciled[0]


# --------------------------------------------------------------------------------------
# The carry
# --------------------------------------------------------------------------------------
def test_the_cluster_on_the_accepted_offer_reaches_the_verdict_and_every_observation(
    e6_make_event,
) -> None:
    """The join back to the bandit arm: one cluster on the offer, on every minted payload.

    Graded through the real fold rather than by calling the builder directly, because the
    thing being asserted is that ``reconcile`` picks the cluster off the *joined* ``accepted``
    event and that ``observation_events`` carries it on to what the feedback engine reads.
    """
    reconciled, observations = _fold(
        [_accepted(e6_make_event, cluster_id="cl-7"), _webhook(e6_make_event)]
    )

    verdict = _one(reconciled)
    assert verdict["payload"]["cluster_id"] == "cl-7", (
        "the reconciled verdict lost the cluster the auction was decided in, so nothing "
        "downstream can route this purchase's trust delta to the arm that chose the store"
    )

    assert [event["kind"] for event in observations] == [OBSERVATION_KIND] * 2
    assert [event["payload"]["cluster_id"] for event in observations] == ["cl-7", "cl-7"], (
        "an offer_integrity observation reached the arm router with no cluster on it"
    )
    # ...and the carry did not disturb what was already there.
    assert tuple(verdict["payload"])[: len(PRE_CHANGE_RECONCILED_KEYS)] == (
        PRE_CHANGE_RECONCILED_KEYS
    )
    for event in observations:
        assert tuple(event["payload"])[: len(PRE_CHANGE_OBSERVATION_KEYS)] == (
            PRE_CHANGE_OBSERVATION_KEYS
        )


def test_the_cluster_survives_a_full_checkout_with_a_pixel_and_a_delivery(e6_make_event) -> None:
    """The four-signal join, not the two-signal one: three graded fields, one cluster each.

    ``delivery`` is the third minted observation and it only exists when the offer promised a
    dispatch window AND the order shipped, so a two-event stream never reaches it. All three
    land on the same arm because all three grade promises made by the same auction.
    """
    stream = [
        _accepted(e6_make_event, cluster_id="cl-9", delivery_days=2.0),
        _pixel(e6_make_event),
        _webhook(e6_make_event),
        _fulfilled(e6_make_event),
    ]
    reconciled, observations = _fold(stream)

    assert _one(reconciled)["payload"]["cluster_id"] == "cl-9"
    assert [event["payload"]["field"] for event in observations] == [
        "price",
        "discount",
        "delivery",
    ], "the fold did not grade all three promises, so this test asserts less than it claims"
    assert {event["payload"]["cluster_id"] for event in observations} == {"cl-9"}


# --------------------------------------------------------------------------------------
# Absent is absent
# --------------------------------------------------------------------------------------
def test_an_offer_naming_no_cluster_mints_no_cluster_key_at_all(e6_make_event) -> None:
    """Not ``None``, not ``""``, not ``"unknown"`` --- the key is not there.

    ``trust.feedback.engine`` reads the cluster with ``payload.get("cluster_id")`` and hands
    what it finds to the arm router. A minted ``null`` is a value that reader has to know to
    special-case; an absent key is the honest statement that the record does not say.
    """
    reconciled, observations = _fold(
        [_accepted(e6_make_event, cluster_id=None), _webhook(e6_make_event)]
    )

    verdict_payload = _one(reconciled)["payload"]
    assert "cluster_id" not in verdict_payload, (
        f"the verdict invented a cluster for an offer that named none: "
        f"{verdict_payload.get('cluster_id')!r}"
    )
    assert tuple(verdict_payload) == PRE_CHANGE_RECONCILED_KEYS

    assert observations, "no observations were minted, so this test would assert nothing"
    for event in observations:
        assert "cluster_id" not in event["payload"], (
            f"an observation invented a cluster: {event['payload'].get('cluster_id')!r}"
        )
        assert tuple(event["payload"]) == PRE_CHANGE_OBSERVATION_KEYS


def test_a_blank_cluster_is_absent_rather_than_carried(e6_make_event) -> None:
    """``""`` and whitespace name no arm, and a consumer cannot tell them from a real id."""
    for blank in ("", "   ", "\t"):
        reconciled, observations = _fold(
            [_accepted(e6_make_event, cluster_id=blank), _webhook(e6_make_event)]
        )
        assert "cluster_id" not in _one(reconciled)["payload"], (
            f"a blank cluster {blank!r} was carried into the verdict as a routing key"
        )
        for event in observations:
            assert "cluster_id" not in event["payload"]


# --------------------------------------------------------------------------------------
# It comes from the accepted event, and from nothing else
# --------------------------------------------------------------------------------------
def test_a_cluster_on_the_webhook_the_pixel_or_the_delivery_is_never_carried(
    e6_make_event,
) -> None:
    """The exchange's routing decision, not the store's or the browser's claim about it.

    All three of these bodies are open (``extra keys are welcome`` at every producing
    boundary), so a store that wanted its integrity findings to land on a different arm's
    posterior would only have to name one in its own webhook.
    """
    sources = {
        "the webhook": [
            _accepted(e6_make_event, cluster_id=None),
            _webhook(e6_make_event, cluster_id="cl-store"),
        ],
        "the pixel": [
            _accepted(e6_make_event, cluster_id=None),
            _pixel(e6_make_event, cluster_id="cl-browser"),
            _webhook(e6_make_event),
        ],
        "the delivery": [
            _accepted(e6_make_event, cluster_id=None),
            _webhook(e6_make_event),
            _fulfilled(e6_make_event, cluster_id="cl-shipper"),
        ],
    }
    for name, stream in sources.items():
        reconciled, observations = _fold(stream)
        assert "cluster_id" not in _one(reconciled)["payload"], (
            f"a cluster named by {name} became the arm this purchase's trust delta routes to"
        )
        for event in observations:
            assert "cluster_id" not in event["payload"], (
                f"a cluster named by {name} reached an offer_integrity observation"
            )


def test_the_accepted_offers_cluster_wins_over_a_disagreeing_webhook(e6_make_event) -> None:
    """Both present and disagreeing: the offer's cluster is the one that is published."""
    reconciled, observations = _fold(
        [
            _accepted(e6_make_event, cluster_id="cl-exchange"),
            _webhook(e6_make_event, cluster_id="cl-store"),
        ]
    )
    assert _one(reconciled)["payload"]["cluster_id"] == "cl-exchange"
    assert {event["payload"]["cluster_id"] for event in observations} == {"cl-exchange"}


# --------------------------------------------------------------------------------------
# The negative: no other auction's cluster
# --------------------------------------------------------------------------------------
def test_another_auctions_cluster_does_not_leak_onto_this_orders_observations(
    e6_make_event,
) -> None:
    """Two whole checkouts in one page: each order publishes ITS auction's cluster.

    ``reconcile`` is handed whole ledger pages, so the offer that names ``cl-A`` and the
    offer that names ``cl-B`` are folded in the same call and share every intermediate
    structure in it. A cluster read from the wrong side of that fold is the failure that
    moves a posterior for an auction the purchase never happened in --- and it is invisible
    in every other field of the emitted event.
    """
    stream = [
        _accepted(
            e6_make_event,
            cluster_id="cl-A",
            order_ref="o-A",
            checkout_token="ck-A",
            bid_ref="bid-A",
            event_id="ev-accept-a",
        ),
        _accepted(
            e6_make_event,
            cluster_id="cl-B",
            order_ref="o-B",
            checkout_token="ck-B",
            bid_ref="bid-B",
            event_id="ev-accept-b",
        ),
        _webhook(e6_make_event, order_ref="o-A", checkout_token="ck-A", event_id="ev-paid-a"),
        _webhook(e6_make_event, order_ref="o-B", checkout_token="ck-B", event_id="ev-paid-b"),
    ]
    reconciled, observations = _fold(stream)

    assert {event["order_ref"] for event in reconciled} == {"o-A", "o-B"}, (
        "the two orders did not both reconcile, so the leak this test attacks is untestable"
    )
    by_order = {event["order_ref"]: event["payload"]["cluster_id"] for event in reconciled}
    assert by_order == {"o-A": "cl-A", "o-B": "cl-B"}

    seen: dict[str, set[str]] = {}
    for event in observations:
        seen.setdefault(str(event["order_ref"]), set()).add(event["payload"]["cluster_id"])
    assert seen == {"o-A": {"cl-A"}, "o-B": {"cl-B"}}


def test_a_neighbouring_auctions_cluster_does_not_fill_in_for_an_offer_that_named_none(
    e6_make_event,
) -> None:
    """The same page, with one offer naming no cluster: it stays absent, it is not borrowed."""
    stream = [
        _accepted(
            e6_make_event,
            cluster_id="cl-A",
            order_ref="o-A",
            checkout_token="ck-A",
            bid_ref="bid-A",
            event_id="ev-accept-a",
        ),
        _accepted(
            e6_make_event,
            cluster_id=None,
            order_ref="o-B",
            checkout_token="ck-B",
            bid_ref="bid-B",
            event_id="ev-accept-b",
        ),
        _webhook(e6_make_event, order_ref="o-A", checkout_token="ck-A", event_id="ev-paid-a"),
        _webhook(e6_make_event, order_ref="o-B", checkout_token="ck-B", event_id="ev-paid-b"),
    ]
    reconciled, observations = _fold(stream)

    verdicts = {event["order_ref"]: event["payload"] for event in reconciled}
    assert verdicts["o-A"]["cluster_id"] == "cl-A"
    assert "cluster_id" not in verdicts["o-B"], (
        f"order o-B borrowed a neighbouring auction's cluster: "
        f"{verdicts['o-B'].get('cluster_id')!r}"
    )
    for event in observations:
        if event["order_ref"] == "o-B":
            assert "cluster_id" not in event["payload"]
        else:
            assert event["payload"]["cluster_id"] == "cl-A"


# --------------------------------------------------------------------------------------
# The contract, and the idempotency keys
# --------------------------------------------------------------------------------------
def test_both_minted_kinds_still_satisfy_their_published_ledger_shape(e6_make_event) -> None:
    """``validate_ledger_payload`` reports nothing, with the extra key and without it.

    ``contracts.ledger`` allows extra keys by design ("a vendor body carries plenty") and
    pins ``reconciled`` at ``(price_honored, discount_honored, pixel_missing, order_ref)`` and
    ``offer_integrity`` at ``(bid_ref, field, promised, observed)``. This asserts the carry is
    contract-legal at the PRODUCING boundary, which is the only place that check runs.
    """
    for label, cluster in (("with a cluster", "cl-7"), ("without one", None)):
        reconciled, observations = _fold(
            [_accepted(e6_make_event, cluster_id=cluster), _webhook(e6_make_event)]
        )
        verdict = _one(reconciled)
        assert validate_ledger_payload(RECONCILED_KIND, verdict["payload"]) == [], (
            f"a reconciled payload minted {label} no longer satisfies its published shape"
        )
        assert observations
        for event in observations:
            assert validate_ledger_payload(OBSERVATION_KIND, event["payload"]) == [], (
                f"an offer_integrity payload minted {label} broke its published shape"
            )


def test_the_minted_event_ids_are_byte_identical_to_the_ids_minted_before_the_carry(
    e6_make_event,
) -> None:
    """``event_id`` IS the ledger's idempotency key, so the carry may not touch one.

    The expected strings are the literals the engine produced BEFORE the cluster was carried,
    measured by running it --- not re-derived from the engine, which would assert nothing.
    """
    for cluster in ("cl-7", None):
        reconciled, observations = _fold(
            [_accepted(e6_make_event, cluster_id=cluster), _webhook(e6_make_event)]
        )
        assert _one(reconciled)["event_id"] == PRE_CHANGE_RECONCILED_ID
        assert tuple(event["event_id"] for event in observations) == PRE_CHANGE_OBSERVATION_IDS


def test_the_cluster_changes_nothing_about_an_emitted_event_but_the_one_key(
    e6_make_event,
) -> None:
    """Same stream, cluster and no cluster: the two mints differ in exactly that key.

    A re-run must still append nothing where the ids match, so anything else that moved with
    the cluster --- a timestamp, an order reference, a verdict --- would be a second, silent
    change riding along on this one.
    """
    with_cluster, with_observations = _fold(
        [_accepted(e6_make_event, cluster_id="cl-7"), _webhook(e6_make_event)]
    )
    without, without_observations = _fold(
        [_accepted(e6_make_event, cluster_id=None), _webhook(e6_make_event)]
    )

    stripped = {
        key: (
            {name: value for name, value in val.items() if name != "cluster_id"}
            if key == "payload"
            else val
        )
        for key, val in _one(with_cluster).items()
    }
    assert stripped == _one(without), (
        "carrying the cluster changed something other than the cluster on the verdict"
    )

    assert len(with_observations) == len(without_observations)
    for carried, bare in zip(with_observations, without_observations, strict=True):
        stripped_observation = {
            key: (
                {name: value for name, value in val.items() if name != "cluster_id"}
                if key == "payload"
                else val
            )
            for key, val in carried.items()
        }
        assert stripped_observation == bare


def test_the_fold_is_still_deterministic_with_a_cluster_on_the_offer(e6_make_event) -> None:
    """Two runs over one stream produce equal events --- the ledger is replayable or it is not."""
    stream = [
        _accepted(e6_make_event, cluster_id="cl-7"),
        _pixel(e6_make_event),
        _webhook(e6_make_event),
        _fulfilled(e6_make_event),
    ]
    first_reconciled, first_observations = _fold(stream)
    second_reconciled, second_observations = _fold(stream)

    assert first_reconciled == second_reconciled
    assert first_observations == second_observations
