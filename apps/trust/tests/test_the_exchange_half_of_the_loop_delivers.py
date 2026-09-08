"""The exchange half of the learning loop must actually post, not pre-refuse every delta.

    PROXYSHOP_WORKER=13 .venv/bin/python -m pytest \
        apps/trust/tests/test_the_exchange_half_of_the_loop_delivers.py -q

**The defect this file grades, measured on the running stack.** ``GET /events/verify`` reported
the two halves of the loop side by side::

    store_agent_notifications.store_agents:      delivering=False  delivered=528  lost=8
    store_agent_notifications.exchange_outcomes: delivering=False  delivered=0    lost=536

The store-agent half worked. The exchange half had delivered **0 of 536, ever** — not one
outcome in the life of the deployment — and every one of them carried the same reason::

    the delta names no cluster, and exposure is decided within a cluster — the exchange cannot
    route this outcome to a posterior, and pooling it into a shared bucket would move clusters
    it never happened in

That refusal is correct about the hazard and was wrong about the remedy.
:meth:`~trust.feedback.ExchangeOutcomeSink.send` pre-refused, without opening a socket, every
payload whose ``pseudonymous_context.cluster_id`` was empty — on the reasoning that the exchange
would 400 it anyway. It would have. But **nothing in this system has ever populated that
field**: a delta is computed from a ledger event, and a ledger event carries ``auction_id``,
``store_id`` and ``order_ref`` and no cluster. So the pre-flight's condition was true of 100% of
traffic, and the "saved socket" was the near end of a loop that had never once closed.

**Where the fact actually lives.** The cluster is the exchange's own: assigned there by
``exchange.retrieval.clusters.assign_cluster`` against a catalogue only the exchange holds, and
stamped onto the auction record. It is *not* the buyer's ``cluster_id`` — that field exists, but
``buyer_svc.intent.clarifier`` derives it as a content hash over the shopper's words, and driving
the served stack shows the exchange discarding it: an auction opened with
``cluster_id: cl-a1e22cd5cf224f6c`` came back from ``GET /auctions/{id}`` as
``cluster-liver-support``. So the exchange resolves the cluster from ``event.auction_id``
(``exchange.policy.routes._cluster_of_the_auction``), and this sink's job is to *deliver the
observation* rather than to predict the receiver's verdict about it.

**What this sink may still decide, and must.** Exactly one case: a delta naming **neither** a
cluster nor an auction. No receiver could answer that either, so it is refused here, counted,
and put on the ring with its reason — and no socket is opened for it. That half is pinned by
``test_learning_loop_visibility.py`` and is deliberately not restated here; this file grades the
half that was missing.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from fastapi import FastAPI, Response
from trust.feedback import EXCHANGE_OUTCOMES_PATH, ExchangeOutcomeSink

from proxyshop_support.asgi_server import serve


def _payload(
    *,
    cluster: str | None,
    auction_id: str | None,
    store_id: str = "s-1",
    event_id: str = "ev-1",
) -> dict[str, Any]:
    """The shape ``push_trust_event`` hands a sink, reduced to what a sink reads off it.

    ``event.auction_id`` is the field this file turns on. It is a real ``LedgerEvent`` field —
    ``contracts.protocol.LedgerEvent`` declares it ``str | None`` — and ``feedback.engine``
    projects it onto the wire through ``_WIRE_EVENT_FIELDS``, so a sink genuinely sees it.
    """
    return {
        "store_id": store_id,
        "event": {
            "event_id": event_id,
            "ts": "2026-01-01T00:00:00Z",
            "kind": "feedback",
            "auction_id": auction_id,
        },
        "dim": "feedback_match",
        "delta": -0.25,
        "pseudonymous_context": {"cluster_id": cluster, "pseudonym": None},
    }


@contextlib.contextmanager
def _door() -> Any:
    """A stand-in exchange that records what reached it and answers 204."""
    posted: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post(EXCHANGE_OUTCOMES_PATH, status_code=204)
    def _receive(body: dict[str, Any]) -> Response:
        posted.append(body)
        return Response(status_code=204)

    with contextlib.ExitStack() as stack:
        url = stack.enter_context(serve(app))
        sink = ExchangeOutcomeSink(f"{url}{EXCHANGE_OUTCOMES_PATH}")
        try:
            yield sink, posted
        finally:
            sink.close()


def test_a_delta_with_no_cluster_but_an_auction_is_posted_rather_than_pre_refused() -> None:
    """The whole defect. Before the repair this returned ``False`` and opened no socket.

    This is the shape of essentially all real traffic — every delta the trust service computes
    from a ledger event — so this single case is what ``delivered: 0, lost: 536`` was made of.
    """
    with _door() as (sink, posted):
        landed = sink.send("s-1", _payload(cluster=None, auction_id="auction-abc123"))

    assert landed is True, (
        "the sink refused to post a delta the exchange can resolve; this is the pre-flight "
        "that kept the exchange half of the loop at delivered=0 for the life of the deployment"
    )
    assert len(posted) == 1
    assert posted[0]["event"]["auction_id"] == "auction-abc123"
    assert posted[0]["pseudonymous_context"]["cluster_id"] is None, (
        "the sink must not invent a cluster on the way out — resolving it is the exchange's "
        "job, and a guessed one here is the pooling the refusal exists to prevent"
    )


def test_the_auction_id_survives_onto_the_wire() -> None:
    """The resolution key must actually reach the receiver, not be dropped in projection.

    Asserted separately from the test above because "the sink posted" and "the receiver got the
    one field it needs to answer" are two claims, and only the second closes the loop.
    """
    with _door() as (sink, posted):
        sink.send("s-1", _payload(cluster=None, auction_id="auction-xyz789"))

    assert posted[0]["event"]["auction_id"] == "auction-xyz789"


def test_the_counters_report_it_as_delivered_and_not_as_lost() -> None:
    """``GET /events/verify`` reads these numbers, and they are the ones that were wrong.

    A sink that posted but still counted the delta as lost would leave the served health
    surface saying the loop is broken while it works, which is the same defect pointed the
    other way.
    """
    with _door() as (sink, _posted):
        sink.send("s-1", _payload(cluster=None, auction_id="auction-abc123"))
        report = sink.report()

    assert report["delivered"] == 1, report
    assert report["lost"] == 0, report
    assert report["undelivered"] == [], report


@pytest.mark.parametrize(
    ("case", "cluster", "auction_id"),
    [
        ("an explicit cluster and no auction", "c-1", None),
        ("an explicit cluster and an auction", "c-1", "auction-abc123"),
    ],
)
def test_a_delta_that_already_names_a_cluster_is_still_posted(
    case: str, cluster: str, auction_id: str | None
) -> None:
    """The path that already worked must keep working, with or without an auction beside it."""
    with _door() as (sink, posted):
        assert sink.send("s-1", _payload(cluster=cluster, auction_id=auction_id)) is True

    assert len(posted) == 1
    assert posted[0]["pseudonymous_context"]["cluster_id"] == cluster


def test_a_delta_naming_neither_a_cluster_nor_an_auction_is_still_refused_here() -> None:
    """The refusal must survive for the one case no receiver could answer either.

    Not a duplicate of ``test_learning_loop_visibility.py``'s: that file pins the behaviour
    against a payload that happens to carry no ``auction_id``, while this one states the
    narrowed rule explicitly — BOTH names absent — so a later change that widened the
    pre-flight back out would be caught by a test that says why.
    """
    with _door() as (sink, posted):
        assert sink.send("s-1", _payload(cluster=None, auction_id=None)) is False
        report = sink.report()

    assert posted == [], "the unroutable outcome opened a socket it did not need to"
    assert report["lost"] == 1
    assert "names no cluster" in report["undelivered"][0]["reason"]


def test_an_empty_auction_id_is_not_an_auction() -> None:
    """A blank string is absence, not a name. Posting it would be a guaranteed round trip.

    The same whitespace-stripping ruling the exchange door applies to ``cluster_id``, applied
    on this side to the field this sink now reads.
    """
    with _door() as (sink, posted):
        assert sink.send("s-1", _payload(cluster=None, auction_id="   ")) is False

    assert posted == []
