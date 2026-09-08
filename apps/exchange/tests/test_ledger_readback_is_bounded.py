"""The in-process ledger readback is a MIRROR, and a mirror does not grow without a ceiling.

``POST /auctions`` is unauthenticated and records two ledger events per call. The sink those
events land in kept every one of them for the life of the process, so any caller could grow
this service's memory by making requests it is entitled to make. MEASURED, the same
3,001-request loop against a served app both ways:

    as shipped   6,002 events retained, 3,988 KiB, linear in N, no ceiling at any N
    with a ring  2,048 events retained,   811 KiB, flat in N

The durable record is the trust service's ledger --
:meth:`exchange.composition.HttpTrustLedgerSink.emit` posts every event there *before* this
list is read -- so the ceiling costs a convenience read and no audit.

Two directions are graded here, because this repository's gates have a habit of pointing only
at the attack:

* **The ceiling is really on in the deployment.** A bound that exists in the class and is not
  the one the served app gets is the "wired but switched off" defect, which is what most of
  the ceilings found in this repo turned out to be.
* **The refusal does not fire on honest traffic.** ``for_auction`` raises instead of returning
  a misleading ``[]`` only once something has actually been evicted. A sink that has dropped
  nothing answers ``[]`` for an unknown id exactly as it always did.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from exchange.auction import (
    DEFAULT_LEDGER_READBACK_CAPACITY,
    InMemoryLedgerSink,
    LedgerReadbackEvicted,
    LedgerRecorder,
)
from exchange.main import create_app
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]


def _auction_body(n: int) -> dict[str, Any]:
    return {
        "intent": {"intent_id": f"intent-{n}", "cluster_id": "cluster-1"},
        "roster": [
            {"store_id": "store-a", "tier": 1, "product_ref": "product-1", "list_price": 10.0}
        ],
        "bid_timeout_seconds": 0.05,
    }


def test_the_served_exchange_gets_a_bounded_readback_and_not_merely_a_bounded_class() -> None:
    """The ceiling the class offers is the ceiling the deployment actually runs with.

    Read off the app AFTER a request has been served, because that is when the exchange binds
    its state machine -- reading it off a freshly built app would be checking a seam no
    request had gone through, which is the shape of the defect this file exists to close.
    """
    app = create_app()
    client = TestClient(app)
    assert client.post("/auctions", json=_auction_body(0)).status_code == 201

    sink = app.state.auction_machine.ledger.sink

    assert isinstance(sink, InMemoryLedgerSink)
    assert sink.capacity == DEFAULT_LEDGER_READBACK_CAPACITY, (
        "the served exchange's ledger sink is not running at the documented capacity, so the "
        f"bound is available but not applied: capacity={sink.capacity!r}"
    )
    assert sink.capacity is not None


def test_driving_the_unauthenticated_route_past_the_ceiling_stops_growing_the_sink() -> None:
    """Measured through the served route, not by calling ``emit`` in a loop."""
    app = create_app()
    client = TestClient(app)
    assert client.post("/auctions", json=_auction_body(0)).status_code == 201

    sink = InMemoryLedgerSink(capacity=8)
    app.state.auction_machine.ledger = LedgerRecorder(sink)

    for n in range(1, 21):
        assert client.post("/auctions", json=_auction_body(n)).status_code == 201

    assert len(sink.events) == 8, (
        "the sink kept more than its capacity while a client drove an unauthenticated route: "
        f"{len(sink.events)} events held"
    )
    assert sink.evicted == 40 - 8, (
        "the eviction witness did not count what fell out of the ring: "
        f"evicted={sink.evicted}, held={len(sink.events)}"
    )
    assert sink.kinds == [str(event["kind"]) for event in sink.events]


def test_a_sink_that_has_evicted_nothing_still_answers_empty_for_an_unknown_auction() -> None:
    """The honest-traffic direction. A refusal that fires here would be the real defect."""
    sink = InMemoryLedgerSink(capacity=8)
    assert sink.for_auction("auction-never-existed") == []

    sink.emit({"kind": "auction_opened", "auction_id": "a-1"})
    assert sink.evicted == 0
    assert sink.for_auction("auction-never-existed") == []
    assert [event["auction_id"] for event in sink.for_auction("a-1")] == ["a-1"]


def test_an_evicted_readback_refuses_rather_than_answering_with_a_misleading_empty_list() -> None:
    """``[]`` for an evicted auction and ``[]`` for a silent one are not the same fact."""
    sink = InMemoryLedgerSink(capacity=4)
    for n in range(10):
        sink.emit({"kind": "auction_opened", "auction_id": f"a-{n}"})

    assert sink.evicted == 6
    assert [event["auction_id"] for event in sink.for_auction("a-9")] == ["a-9"]

    with pytest.raises(LedgerReadbackEvicted) as raised:
        sink.for_auction("a-0")

    message = str(raised.value)
    assert "a-0" in message
    assert "6 event" in message
    assert "trust service" in message, (
        "the refusal must name where the durable record actually is, or it is only a failure"
    )


@pytest.mark.parametrize(
    "source",
    [
        "services/sim/src/runner.py",
        "e2e/support/s1/flow.py",
    ],
)
def test_the_two_consumers_that_grade_the_whole_stream_take_an_unbounded_sink(source: str) -> None:
    """A truncated stream is a WRONG VERDICT for these two, not a short answer.

    Both build their own sink and then reconcile every event the run emitted. They are finite
    CLI harnesses with no unauthenticated door, so the ceiling that protects the served
    exchange would only cost them correctness. If either stops passing ``capacity=None``, its
    reconciliation silently starts grading a prefix.
    """
    text = (REPO_ROOT / source).read_text(encoding="utf-8")
    assert "InMemoryLedgerSink(capacity=None)" in text, (
        f"{source} no longer builds an unbounded ledger sink, so the whole-stream audit it "
        "performs would silently grade only the most recent events"
    )
    assert "InMemoryLedgerSink()" not in text
