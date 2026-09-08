"""A trust delta must say WHICH of the store's own decisions earned it (impression -> outcome).

    PROXYSHOP_WORKER=13 .venv/bin/python -m pytest \
        apps/trust/tests/test_impression_attribution.py -q

**The defect this file grades, measured on the live ledger.** The chain already records the
whole story and nothing read the first link of it::

    shown(bid_ref, slot) -> accepted(bid_ref) -> reconciled(bid_ref, order_ref)
      -> order_paid(order_ref) -> feedback(order_ref, auction_id, store_id)

Every auction writes one ``shown`` per filled slot, carrying ``{slot, bid_ref}``. Outside the
write site, **nothing in the tree consumed it**, and nothing walked a bid to the order it became.
So a store agent was told "your ``feedback_match`` moved" and never which pitch earned it — and
its learned policy is keyed by cluster and discount rung, so a per-store-per-dimension aggregate
is precisely the shape it cannot attribute to an arm. It could not learn from its own wins.

**What the repair is.** :func:`trust.feedback.attribution.impression_for` joins the affected
store's own ledger rows on ``auction_id`` and reports the impression: the arm played
(``bid_placed.payload.offer``), whether it was shown and in which slot, whether it converted, and
the order it became. :func:`~trust.feedback.deltas.delta_for_event` attaches it, and
``engine._wire_event`` parks it under :data:`~trust.feedback.TRUST_ATTRIBUTION_KEY` beside the
existing ``trust_report`` — so it rides to both recipients on doors that are already served.

**It costs no query.** The rows are the ones ``delta_for_event`` is already handed and already
scores twice. That is asserted below rather than asserted in prose, by handing the function a
history that raises if it is walked more than once.

**Verified live**, against the real chain rather than these fixtures: driving
``POST /buyer/feedback`` and running the read model in the trust container over 193 real rows for
``gaiaherbs.com`` produced ``slot: "fit"``, ``converted: true``, ``unit_price: 25.49``,
``order_ref: "drive-d0c4dc4febfe"``.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from fastapi import FastAPI
from trust.feedback import TRUST_ATTRIBUTION_KEY, TRUST_EVENT_PATH, StoreAgentSink
from trust.feedback.attribution import ATTRIBUTION_SCHEMA_VERSION, impression_for
from trust.feedback.deltas import delta_for_event
from trust.feedback.engine import trust_event_payload

from proxyshop_support.asgi_server import serve

AUCTION = "auction-0001"
STORE = "shop.example"
OTHER_AUCTION = "auction-9999"


def _row(kind: str, *, auction_id: str = AUCTION, **payload: Any) -> dict[str, Any]:
    """One ledger row for ``STORE``, shaped as the live chain writes it."""
    return {
        "event_id": f"{kind}-{auction_id}",
        "ts": "2026-01-01T00:00:00Z",
        "kind": kind,
        "auction_id": auction_id,
        "store_id": STORE,
        "payload": dict(payload),
    }


def _offer(**over: Any) -> dict[str, Any]:
    base = {
        "discount": {"type": "percentage", "value": 20.0},
        "unit_price": 16.78,
        "currency": "USD",
        "product_ref": "prod-1",
    }
    base.update(over)
    return base


def _bid(auction_id: str = AUCTION, **over: Any) -> dict[str, Any]:
    return _row(
        "bid_placed",
        auction_id=auction_id,
        bid_ref=f"{auction_id}:{STORE}",
        offer=_offer(**over),
    )


def _shown(slot: str = "value", auction_id: str = AUCTION) -> dict[str, Any]:
    return _row("shown", auction_id=auction_id, slot=slot, bid_ref=f"{auction_id}:{STORE}")


def _accepted(auction_id: str = AUCTION, **over: Any) -> dict[str, Any]:
    return _row(
        "accepted",
        auction_id=auction_id,
        bid_ref=f"{auction_id}:{STORE}",
        offer=_offer(**over),
    )


def _feedback_event(order_ref: str | None = "ord-1") -> dict[str, Any]:
    """The observation-carrying row a delta is computed from."""
    return {
        "event_id": "fb-0001",
        "ts": "2026-01-02T00:00:00Z",
        "kind": "feedback",
        "auction_id": AUCTION,
        "store_id": STORE,
        "order_ref": order_ref,
        "payload": {"dim": "feedback_match", "type": "fulfilled", "matched_pitch": True},
    }


# =====================================================================================
# The join
# =====================================================================================
def test_the_whole_chain_is_reported_from_one_walk_of_the_stores_own_rows() -> None:
    """The demo beat: this pitch, in this slot, converted, and became this order."""
    found = impression_for(_feedback_event(), [_bid(), _shown("value"), _accepted()])

    assert found == {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "auction_id": AUCTION,
        "bid_ref": f"{AUCTION}:{STORE}",
        "slot": "value",
        "order_ref": "ord-1",
        "shown": True,
        "converted": True,
        "arm": {
            "discount": {"type": "percentage", "value": 20.0},
            "unit_price": 16.78,
            "currency": "USD",
            "product_ref": "prod-1",
        },
    }


def test_a_loss_is_learnable_because_the_arm_played_is_recorded_on_the_bid() -> None:
    """Shown and not accepted must still name the rung, or the agent learns only from wins.

    An arm that appears only on ``accepted`` would make every posterior a count of successes
    with no denominator — the same monotonic ratchet ``accept/routes.py`` refuses for the
    bandit, reached through the store agent instead.
    """
    found = impression_for(_feedback_event(), [_bid(), _shown("fit")])

    assert found["shown"] is True
    assert found["converted"] is False
    assert found["arm"]["unit_price"] == 16.78


def test_a_benched_store_is_reported_as_never_shown_rather_than_as_slotless() -> None:
    """Bid, never shown: ``shown`` false and ``slot`` NULL — not an empty-string slot.

    ``""`` would sort and group with a real slot name; ``None`` is the honest "there was no
    impression", which is a different outcome from "there was one and we lost the name".
    """
    found = impression_for(_feedback_event(), [_bid()])

    assert found["shown"] is False
    assert found["slot"] is None
    assert found["converted"] is False


def test_the_accepted_offer_wins_over_the_bid_when_the_terms_were_counter_proposed() -> None:
    """The terms that earned the outcome are the ones transacted, not the ones first pitched."""
    found = impression_for(
        _feedback_event(),
        [_bid(unit_price=25.00), _shown(), _accepted(unit_price=16.78)],
    )

    assert found["arm"]["unit_price"] == 16.78


# =====================================================================================
# Precision — the join must not pull in a different auction
# =====================================================================================
def test_another_auctions_impression_is_not_attributed_to_this_one() -> None:
    """The store's history spans every auction it ever bid in. Only THIS one may be read.

    Getting this wrong is not a cosmetic error: it would tell the agent that the arm it played
    last week earned an outcome it did not, which is a learning signal pointed at the wrong arm.
    """
    found = impression_for(
        _feedback_event(),
        [
            _shown("specialist", auction_id=OTHER_AUCTION),
            _accepted(auction_id=OTHER_AUCTION, unit_price=99.99),
            _bid(),
            _shown("value"),
        ],
    )

    assert found["slot"] == "value"
    assert found["converted"] is False, "an accept in a DIFFERENT auction was counted as this one"
    assert found["arm"]["unit_price"] == 16.78


def test_an_observation_that_names_no_auction_attributes_nothing() -> None:
    """A ``claim_verified`` from the registry has no impression behind it. Invent none."""
    event = _feedback_event()
    event["auction_id"] = None

    assert impression_for(event, [_bid(), _shown(), _accepted()]) is None


# =====================================================================================
# It must be total, and it must be free
# =====================================================================================
@pytest.mark.parametrize(
    ("case", "history"),
    [
        ("rows that are not mappings", ["not-a-row", 7, None]),
        ("a row with no payload", [{"kind": "shown", "auction_id": AUCTION}]),
        (
            "a payload that is not a mapping",
            [{"kind": "shown", "auction_id": AUCTION, "payload": 5}],
        ),
        ("an offer that is not a mapping", [_row("bid_placed", bid_ref="b", offer="nope")]),
    ],
)
def test_a_malformed_row_from_an_older_build_never_raises(case: str, history: list[Any]) -> None:
    """This runs on ``POST /events``'s request path. A raise here is an append that 5xxs."""
    found = impression_for(_feedback_event(), history)

    assert found is not None
    assert found["auction_id"] == AUCTION


def test_the_history_is_walked_once_and_no_ledger_read_is_added() -> None:
    """The claim that this costs no query, asserted rather than promised.

    ``delta_for_event`` materialises the rows once and hands the SAME list to the scorer and to
    this join. A second walk of a generator would arrive empty; a re-read would be a database
    round trip on the append path. The counter below catches both.
    """
    walks = {"n": 0}

    class _CountingHistory:
        def __iter__(self) -> Any:
            walks["n"] += 1
            return iter([_bid(), _shown(), _accepted()])

    impression_for(_feedback_event(), _CountingHistory())

    assert walks["n"] == 1, f"the history was walked {walks['n']} times"


def test_the_delta_path_attaches_it_without_re_reading_the_history() -> None:
    """``delta_for_event`` must expose the attribution, from the rows it already had."""
    rows = [_bid(), _shown("reliability"), _accepted()]
    delta = delta_for_event(_feedback_event(), history=iter(rows))

    assert delta is not None
    assert delta["attribution"]["slot"] == "reliability"
    assert delta["attribution"]["converted"] is True


# =====================================================================================
# It has to reach a recipient, over a real socket — not merely be computed
# =====================================================================================
def test_it_rides_to_the_store_agent_on_the_wire_under_its_own_namespaced_key() -> None:
    """The whole point. A read model the recipient never receives is the defect, not the fix.

    Driven over a real loopback HTTP push through the production sink, so what is asserted is
    what a store agent's intake actually parses — not what a function returned.
    """
    received: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post(TRUST_EVENT_PATH)
    def _intake(body: dict[str, Any]) -> dict[str, str]:
        received.append(body)
        return {"ok": "true"}

    delta = delta_for_event(_feedback_event(), history=[_bid(), _shown("value"), _accepted()])
    payload = trust_event_payload(delta)

    with contextlib.ExitStack() as stack:
        url = stack.enter_context(serve(app))
        sink = StoreAgentSink({STORE: url})
        try:
            assert sink.send(STORE, payload) is True
        finally:
            sink.close()

    assert len(received) == 1
    attribution = received[0]["event"]["payload"][TRUST_ATTRIBUTION_KEY]
    assert attribution["slot"] == "value"
    assert attribution["converted"] is True
    assert attribution["bid_ref"] == f"{AUCTION}:{STORE}"
    assert attribution["order_ref"] == "ord-1"


def test_the_order_ref_survives_the_scrub_that_would_have_redacted_it() -> None:
    """A ten-digit order id must arrive intact, not as ``[redacted]``.

    ``scrub`` redacts any run of nine or more digits, and a redacted-but-truthy value fires no
    fallback — so the store would be told its score moved on an order it cannot identify. The
    same hazard, and the same ruling, as the ``order_ref`` field beside it.
    """
    delta = delta_for_event(
        _feedback_event(order_ref="5678901234"), history=[_bid(), _shown(), _accepted()]
    )
    payload = trust_event_payload(delta)

    assert payload["event"]["payload"][TRUST_ATTRIBUTION_KEY]["order_ref"] == "5678901234"


def test_the_existing_trust_report_is_not_displaced_by_the_new_key() -> None:
    """Two namespaced keys, both present. The audit report and the attribution are separate."""
    from trust.feedback import TRUST_REPORT_KEY

    delta = delta_for_event(_feedback_event(), history=[_bid(), _shown(), _accepted()])
    body = trust_event_payload(delta)["event"]["payload"]

    assert TRUST_REPORT_KEY in body
    assert TRUST_ATTRIBUTION_KEY in body
