"""T-030 — auction fan-out, the hard timeout, universal representation, and the R12 gate.

Ticket verify: ``pytest apps/exchange/tests/test_auction.py -q``.

Everything here runs on a **frozen clock**: ``T_NOW`` is a fixed float epoch and every
deadline is passed in, so no assertion in this file can pass or fail because of when it ran.
The one exception is the parallel fan-out test, which is about wall-clock behaviour and says
so.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from exchange.auction import (
    ArrivalClock,
    AuctionStateMachine,
    IllegalAuctionTransition,
    InMemoryAuctionStore,
    InMemoryLedgerSink,
    LedgerRecorder,
    RedisAuctionStore,
    UnknownAuction,
    UnknownLedgerEventKind,
    build_event,
    collect_bids,
    parallel_fan_out,
    sequential_fan_out,
)
from exchange.auction.collect import TIMED_OUT_FIELD
from exchange.auction.routes import MAX_IDENTIFIER_LENGTH, configure_auctions
from exchange.eligibility import (
    BLACKLISTED,
    ELIGIBLE,
    SELLER_ELIGIBILITY_INTERFACE_VERSION,
    UNAVAILABLE,
    EligibilityDecision,
    SellerEligibility,
    StaticSellerEligibility,
    read_eligibility,
)
from exchange.main import create_app
from exchange.orchestration import solicit_bids
from fastapi.testclient import TestClient

T_NOW = 1_700_000_000.0


# --- builders -------------------------------------------------------------------------
def rostered(store_id: str, list_price: float, tier: int = 1) -> dict[str, Any]:
    return {
        "store_id": store_id,
        "tier": tier,
        "product_ref": "product-1",
        "list_price": list_price,
    }


def response(store_id: str, price: float, received_at: float) -> dict[str, Any]:
    return {
        "store_id": store_id,
        "received_at": received_at,
        "bid": {
            "auction_id": "auction-1",
            "store_id": store_id,
            "offer": {"product_ref": "product-1", "unit_price": price, "total_price": price},
            "claims": [],
        },
    }


class RecordingSolicitor:
    """Stands in for the outbound ``POST /v1/bid-requests`` client."""

    def __init__(self, prices: dict[str, float], *, silent: tuple[str, ...] = ()) -> None:
        self.prices = dict(prices)
        self.silent = set(silent)
        self.asked: list[str] = []

    def solicit(self, store: dict[str, Any]) -> dict[str, Any] | None:
        store_id = store["store_id"]
        self.asked.append(store_id)
        if store_id in self.silent:
            return None
        return response(store_id, self.prices[store_id], T_NOW - 1.0)

    __call__ = solicit


class TableEligibility(SellerEligibility):
    """A source over a fixed status table; ``raising`` names stores whose read blows up."""

    def __init__(
        self,
        statuses: dict[str, str],
        *,
        raising: tuple[str, ...] = (),
        version: str = SELLER_ELIGIBILITY_INTERFACE_VERSION,
    ) -> None:
        self._statuses = dict(statuses)
        self._raising = set(raising)
        self.interface_version = version
        self.checked: list[str] = []

    def check(self, store_id: str) -> EligibilityDecision:
        self.checked.append(store_id)
        if store_id in self._raising:
            raise RuntimeError(f"eligibility backend unreachable for {store_id}")
        return EligibilityDecision(
            store_id=store_id,
            status=self._statuses[store_id],
            reason=f"table:{self._statuses[store_id]}",
        )


# =====================================================================================
# Acceptance 1 — every store is represented; silent and Tier-0 fall back to list price
# =====================================================================================
def test_three_responsive_one_silent_and_two_tier0_yield_six_entries() -> None:
    roster = [
        rostered("store-r1", 120.0),
        rostered("store-r2", 130.0),
        rostered("store-r3", 140.0),
        rostered("store-silent", 150.0),
        rostered("store-t0a", 160.0, tier=0),
        rostered("store-t0b", 170.0, tier=0),
    ]
    responses = [response(sid, 80.0, T_NOW - 1.0) for sid in ("store-r1", "store-r2", "store-r3")]

    entries = collect_bids(roster, responses, T_NOW)
    by_store = {entry.store_id: entry for entry in entries}

    assert len(entries) == 6
    assert [entry.store_id for entry in entries] == [r["store_id"] for r in roster]

    for sid in ("store-r1", "store-r2", "store-r3"):
        assert by_store[sid].fallback is False
        assert by_store[sid].unit_price == 80.0
        assert by_store[sid].fallback_reason is None

    for sid, price in (("store-silent", 150.0), ("store-t0a", 160.0), ("store-t0b", 170.0)):
        assert by_store[sid].fallback is True
        assert by_store[sid].unit_price == price

    # The reason distinguishes the two ways a store ends at list price.
    assert by_store["store-silent"].fallback_reason == "no_response"
    assert by_store["store-t0a"].fallback_reason == "tier_0_no_agent"


def test_a_tier0_store_that_somehow_answered_is_still_represented_at_list_price() -> None:
    """Tier-0 means catalog-only. An answer under its name does not make it a bidder."""
    roster = [rostered("store-t0", 160.0, tier=0)]
    entries = collect_bids(roster, [response("store-t0", 1.0, T_NOW - 1.0)], T_NOW)
    assert entries[0].fallback is True
    assert entries[0].unit_price == 160.0


def test_a_response_for_a_store_that_is_not_on_the_roster_is_ignored() -> None:
    entries = collect_bids(
        [rostered("store-a", 120.0)],
        [response("store-a", 80.0, T_NOW - 1.0), response("store-gatecrasher", 1.0, T_NOW - 1.0)],
        T_NOW,
    )
    assert [entry.store_id for entry in entries] == ["store-a"]


# =====================================================================================
# Acceptance 2 — the timeout is enforced on a frozen clock; late bids are rejected
# =====================================================================================
def test_a_bid_arriving_after_the_deadline_is_rejected_and_falls_back() -> None:
    roster = [rostered("store-ontime", 120.0), rostered("store-late", 155.0)]
    responses = [
        response("store-ontime", 90.0, T_NOW - 0.5),
        # An irresistible price, one that arrived too late to be one.
        response("store-late", 1.0, T_NOW + 5.0),
    ]

    by_store = {entry.store_id: entry for entry in collect_bids(roster, responses, T_NOW)}

    assert by_store["store-ontime"].fallback is False
    assert by_store["store-ontime"].unit_price == 90.0
    assert by_store["store-late"].fallback is True
    assert by_store["store-late"].unit_price == 155.0
    assert by_store["store-late"].fallback_reason == "response_after_deadline"


def test_a_bid_landing_exactly_on_the_deadline_is_in_time() -> None:
    entries = collect_bids([rostered("store-a", 120.0)], [response("store-a", 80.0, T_NOW)], T_NOW)
    assert entries[0].fallback is False


def test_a_second_response_cannot_displace_the_bid_a_store_already_committed_to() -> None:
    """Otherwise a store could watch the field and resubmit cheaper inside the window."""
    entries = collect_bids(
        [rostered("store-a", 120.0)],
        [response("store-a", 90.0, T_NOW - 2.0), response("store-a", 5.0, T_NOW - 1.0)],
        T_NOW,
    )
    assert entries[0].unit_price == 90.0


def test_a_fallback_entry_carries_no_claims() -> None:
    """R10/R18/R19: catalog data can never be the evidence satisfying a hard constraint."""
    entries = collect_bids([rostered("store-silent", 150.0)], [], T_NOW)
    assert entries[0].bid["claims"] == []
    assert entries[0].bid["fallback"] is True


# =====================================================================================
# Acceptance 3 — the state machine, and its transitions in the ledger
# =====================================================================================
def machine() -> tuple[AuctionStateMachine, InMemoryLedgerSink]:
    sink = InMemoryLedgerSink()
    return AuctionStateMachine(InMemoryAuctionStore(), LedgerRecorder(sink)), sink


def test_the_legal_path_transitions_and_logs_each_move_to_the_ledger() -> None:
    auctions, sink = machine()
    auctions.create("auction-1", intent_id="intent-1", cluster_id="cluster-1")
    assert auctions.state_of("auction-1") == "created"

    auctions.open("auction-1", now=T_NOW)
    auctions.close("auction-1", now=T_NOW + 3.0)
    record = auctions.accept("auction-1", "bid-a", now=T_NOW + 4.0)

    assert record.state == "accepted"
    assert record.accepted_bid_ref == "bid-a"
    assert sink.kinds == ["auction_opened", "auction_closed", "accepted"]
    assert [event["auction_id"] for event in sink.events] == ["auction-1"] * 3
    assert sink.events[-1]["payload"]["bid_ref"] == "bid-a"


@pytest.mark.parametrize(
    ("moves", "illegal"),
    [
        (["open", "close"], "close"),  # closing an already-closed auction
        (["open"], "accept"),  # accepting while bids are still arriving
        (["open", "close", "accept"], "accept"),  # the double accept R3/A5 forbids
        ([], "close"),  # closing one that never opened
    ],
)
def test_an_illegal_transition_is_refused_and_writes_nothing(
    moves: list[str], illegal: str
) -> None:
    auctions, sink = machine()
    auctions.create("auction-1", intent_id="intent-1", cluster_id="cluster-1")
    for move in moves:
        getattr(auctions, move)("auction-1", *(["bid-a"] if move == "accept" else []), now=T_NOW)

    before = list(sink.kinds)
    with pytest.raises(IllegalAuctionTransition):
        getattr(auctions, illegal)("auction-1", *(["bid-a"] if illegal == "accept" else []))

    assert sink.kinds == before, "a refused transition must not appear in the ledger"


def test_an_unknown_auction_id_is_a_distinct_error() -> None:
    auctions, _ = machine()
    with pytest.raises(UnknownAuction):
        auctions.state_of("auction-never-created")


def test_creating_the_same_auction_twice_is_refused() -> None:
    auctions, _ = machine()
    auctions.create("auction-1", intent_id="i", cluster_id="c")
    with pytest.raises(IllegalAuctionTransition):
        auctions.create("auction-1", intent_id="i", cluster_id="c")


def test_the_exchange_cannot_invent_a_ledger_event_kind() -> None:
    """D24 froze the 18 kinds in `contracts`; the exchange may use them, not extend them."""
    with pytest.raises(UnknownLedgerEventKind):
        build_event("auction_reopened", auction_id="auction-1")


def test_a_ledger_sink_that_is_down_does_not_fail_the_auction() -> None:
    """Losing an audit record is bad; failing a live auction over one is worse."""

    class BrokenSink:
        def emit(self, event: dict[str, Any]) -> None:
            raise ConnectionError("trust service unreachable")

    recorder = LedgerRecorder(BrokenSink())
    auctions = AuctionStateMachine(InMemoryAuctionStore(), recorder)
    auctions.create("auction-1", intent_id="i", cluster_id="c")
    auctions.open("auction-1", now=T_NOW)

    assert auctions.state_of("auction-1") == "open"
    assert [reason for _, reason in recorder.failures] == [
        "ConnectionError: trust service unreachable"
    ]


@pytest.mark.docker
def test_the_state_machine_survives_a_round_trip_through_redis(redis_client: Any) -> None:
    """DESIGN pins the store: ``auction:{id}``, TTL 15 minutes, per-worker prefixed (D39)."""
    store = RedisAuctionStore(redis_client)
    auctions = AuctionStateMachine(store, InMemoryLedgerSink())
    auctions.create("auction-redis", intent_id="intent-1", cluster_id="cluster-1")
    auctions.open("auction-redis", now=T_NOW)

    reread = AuctionStateMachine(RedisAuctionStore(redis_client), InMemoryLedgerSink())
    assert reread.state_of("auction-redis") == "open"

    ttl = redis_client.ttl(RedisAuctionStore.key("auction-redis"))
    assert 0 < ttl <= 15 * 60


# =====================================================================================
# Acceptance 4 — the R12 solicitation gate, by denial, with a positive control
# =====================================================================================
def gated_roster() -> list[dict[str, Any]]:
    return [
        rostered("store-ok1", 120.0),
        rostered("store-black", 130.0),
        rostered("store-ok2", 140.0),
        rostered("store-unknown", 150.0),
        rostered("store-boom", 160.0, tier=0),
    ]


def gated_source() -> TableEligibility:
    return TableEligibility(
        {
            "store-ok1": ELIGIBLE,
            "store-ok2": ELIGIBLE,
            "store-black": BLACKLISTED,
            "store-unknown": UNAVAILABLE,
            "store-boom": ELIGIBLE,  # says eligible, but the read itself raises
        },
        raising=("store-boom",),
    )


def test_eligibility_is_read_for_every_rostered_store_before_anyone_is_asked() -> None:
    roster = gated_roster()
    source = gated_source()
    solicitor = RecordingSolicitor({"store-ok1": 80.0, "store-ok2": 85.0})

    result = solicit_bids(roster=roster, solicitor=solicitor, eligibility=source, now=T_NOW)

    assert sorted(source.checked) == sorted(r["store_id"] for r in roster)
    assert solicitor.asked == ["store-ok1", "store-ok2"]
    assert result.solicited == ["store-ok1", "store-ok2"]


def test_an_ineligible_store_does_not_even_appear_as_a_list_price_fallback() -> None:
    """A fallback entry is still an offer a blacklisted store could win with."""
    result = solicit_bids(
        roster=gated_roster(),
        solicitor=RecordingSolicitor({"store-ok1": 80.0, "store-ok2": 85.0}),
        eligibility=gated_source(),
        now=T_NOW,
    )
    assert sorted(entry.store_id for entry in result.entries) == ["store-ok1", "store-ok2"]


def test_the_positive_control_an_eligible_store_is_solicited_and_its_price_survives() -> None:
    """Without this, an orchestration that solicits nobody would satisfy every denial above."""
    result = solicit_bids(
        roster=gated_roster(),
        solicitor=RecordingSolicitor({"store-ok1": 80.0, "store-ok2": 85.0}),
        eligibility=gated_source(),
        now=T_NOW,
    )
    by_store = {entry.store_id: entry for entry in result.entries}
    for store_id, price in (("store-ok1", 80.0), ("store-ok2", 85.0)):
        assert by_store[store_id].fallback is False
        assert by_store[store_id].unit_price == price


@pytest.mark.parametrize(
    ("store_id", "token"),
    [("store-black", "blacklist"), ("store-unknown", "unavailable"), ("store-boom", "unavailable")],
)
def test_every_denial_is_recorded_with_a_reason_naming_its_condition(
    store_id: str, token: str
) -> None:
    result = solicit_bids(
        roster=gated_roster(),
        solicitor=RecordingSolicitor({"store-ok1": 80.0, "store-ok2": 85.0}),
        eligibility=gated_source(),
        now=T_NOW,
    )
    denied = {denial.store_id: denial for denial in result.denied}
    assert sorted(denied) == ["store-black", "store-boom", "store-unknown"]
    assert token in denied[store_id].reason.lower()


def test_an_eligibility_read_that_raises_denies_exactly_like_unavailable() -> None:
    """Fail closed means three things, and this is the one an implementation forgets."""

    class Exploding(SellerEligibility):
        def check(self, store_id: str) -> EligibilityDecision:
            raise RuntimeError("backend down")

    decision = read_eligibility(Exploding(), "store-a")
    assert decision.status == UNAVAILABLE
    assert "unavailable" in decision.reason.lower()


def test_a_source_answering_an_unrecognised_status_denies() -> None:
    decision = read_eligibility(StaticSellerEligibility({"store-a": "probably-fine"}), "store-a")
    assert decision.status == UNAVAILABLE


def test_a_store_the_eligibility_source_has_never_heard_of_denies() -> None:
    """The default of the deterministic double is UNAVAILABLE, not ELIGIBLE."""
    assert StaticSellerEligibility().check("store-nobody").status == UNAVAILABLE


def test_a_source_speaking_another_interface_version_solicits_nobody() -> None:
    solicitor = RecordingSolicitor({"store-ok1": 80.0, "store-ok2": 85.0})
    stale = TableEligibility(
        {"store-ok1": ELIGIBLE, "store-ok2": ELIGIBLE},
        version="0.0.0-not-the-published-interface",
    )

    result = solicit_bids(
        roster=[rostered("store-ok1", 120.0), rostered("store-ok2", 140.0)],
        solicitor=solicitor,
        eligibility=stale,
        now=T_NOW,
    )

    assert solicitor.asked == []
    assert result.solicited == []
    assert result.entries == []
    assert sorted(d.store_id for d in result.denied) == ["store-ok1", "store-ok2"]
    assert stale.checked == [], "a source we cannot read must not be consulted at all"


def test_the_published_interface_version_is_accepted() -> None:
    """The positive control for the version gate: a current source is not refused."""
    solicitor = RecordingSolicitor({"store-ok1": 80.0, "store-ok2": 85.0})
    result = solicit_bids(
        roster=[rostered("store-ok1", 120.0), rostered("store-ok2", 140.0)],
        solicitor=solicitor,
        eligibility=TableEligibility({"store-ok1": ELIGIBLE, "store-ok2": ELIGIBLE}),
        now=T_NOW,
    )
    assert solicitor.asked == ["store-ok1", "store-ok2"]
    assert result.solicited == ["store-ok1", "store-ok2"]


def test_an_unversioned_source_is_refused() -> None:
    class Unversioned:
        interface_version = None

        def check(self, store_id: str) -> EligibilityDecision:
            return EligibilityDecision(store_id=store_id, status=ELIGIBLE, reason="")

    solicitor = RecordingSolicitor({"store-ok1": 80.0})
    result = solicit_bids(
        roster=[rostered("store-ok1", 120.0)],
        solicitor=solicitor,
        eligibility=Unversioned(),
        now=T_NOW,
    )
    assert solicitor.asked == []
    assert result.solicited == []


# =====================================================================================
# Parallel fan-out with a hard timeout (R10). Wall-clock by nature — see the docstring.
# =====================================================================================
def test_parallel_fan_out_stops_waiting_at_the_deadline_and_ignores_the_straggler() -> None:
    release = threading.Event()
    asked: list[str] = []

    def solicitor(store: dict[str, Any]) -> dict[str, Any] | None:
        asked.append(store["store_id"])
        if store["store_id"] == "store-hung":
            release.wait(timeout=5.0)  # answers only long after the window closed
        return {
            "store_id": store["store_id"],
            "bid": {
                "auction_id": "auction-1",
                "store_id": store["store_id"],
                "offer": {"product_ref": "product-1", "unit_price": 1.0, "total_price": 1.0},
                "claims": [],
            },
        }

    roster = [rostered("store-quick", 120.0), rostered("store-hung", 130.0)]
    started = time.time()
    try:
        deadline = started + 0.3
        responses = parallel_fan_out(roster, solicitor, deadline=deadline)
        elapsed = time.time() - started

        assert elapsed < 3.0, "the hard timeout did not stop the wait"
        # `answered` means "came back with something to rank", which is what this assertion
        # has always been about. It used to be spelled `{r["store_id"] for r in responses}`,
        # which was the same set only because a store the exchange abandoned left NOTHING in
        # the responses — the defect: `collect_bids` then recorded it as `no_response`, the
        # word for a store that was asked and never spoke, and a store answering at 4.5s
        # against a 3.0s window became indistinguishable from a container that is switched
        # off. The straggler is now represented, without a bid, and the claim being made here
        # is untouched: its LATE OFFER was not counted.
        answered = {r["store_id"] for r in responses if r.get("bid") is not None}
        assert "store-quick" in answered
        assert "store-hung" not in answered, "a store that missed the window was counted"
        # ...and the new half, which is the point of the repair: the exchange wrote down that
        # it walked away from a reply in flight, rather than losing the fact with the future.
        assert {"store_id": "store-hung", TIMED_OUT_FIELD: True} in responses

        entries = {e.store_id: e for e in collect_bids(roster, responses, deadline)}
        assert entries["store-quick"].fallback is False
        assert entries["store-hung"].fallback is True
        assert entries["store-hung"].unit_price == 130.0
    finally:
        release.set()


def test_a_store_whose_solicitation_raises_simply_does_not_bid() -> None:
    def solicitor(store: dict[str, Any]) -> dict[str, Any]:
        if store["store_id"] == "store-broken":
            raise ConnectionError("agent refused the connection")
        return response(store["store_id"], 80.0, T_NOW - 1.0)

    roster = [rostered("store-ok", 120.0), rostered("store-broken", 130.0)]
    # One deadline, used by both halves. This used to fan out against a wall-clock deadline
    # and then collect against the logical `T_NOW`; the two only agreed because the
    # solicitor's own `received_at` bridged them — which is precisely the forgery the
    # exchange now overwrites. The assertion being made here is about the raising store.
    deadline = time.time() + 2.0
    responses = parallel_fan_out(roster, solicitor, deadline=deadline)
    entries = {e.store_id: e for e in collect_bids(roster, responses, deadline)}

    assert entries["store-ok"].fallback is False
    assert entries["store-broken"].fallback is True
    assert entries["store-broken"].unit_price == 130.0


def test_the_exchange_stamps_an_arrival_time_when_the_store_did_not() -> None:
    """A store that omits `received_at` must not thereby escape the deadline."""

    def solicitor(store: dict[str, Any]) -> dict[str, Any]:
        return {"store_id": store["store_id"], "bid": {"offer": {"unit_price": 1.0}}}

    responses = sequential_fan_out([rostered("store-a", 120.0)], solicitor, clock=lambda: T_NOW)
    assert responses[0]["received_at"] == T_NOW


# =====================================================================================
# The route: the whole path through the real entry point
# =====================================================================================
def test_post_auctions_runs_the_gate_the_fan_out_and_the_state_machine() -> None:
    app = create_app()
    assert "exchange.auction.routes" in app.state.mounted_routers

    solicitor = RecordingSolicitor({"store-a": 80.0}, silent=("store-silent",))
    configure_auctions(
        app,
        solicitor=solicitor,
        eligibility=StaticSellerEligibility(
            {
                "store-a": ELIGIBLE,
                "store-silent": ELIGIBLE,
                "store-t0": ELIGIBLE,
                "store-black": BLACKLISTED,
            }
        ),
    )

    client = TestClient(app)
    posted = client.post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
            "roster": [
                rostered("store-a", 120.0),
                rostered("store-silent", 150.0),
                rostered("store-t0", 160.0, tier=0),
                rostered("store-black", 99.0),
            ],
            "bid_timeout_seconds": 2.0,
        },
    )
    assert posted.status_code == 201
    body = posted.json()

    assert body["state"] == "closed"
    assert body["solicited"] == ["store-a", "store-silent"]
    assert solicitor.asked == ["store-a", "store-silent"]

    entries = {entry["store_id"]: entry for entry in body["entries"]}
    assert sorted(entries) == ["store-a", "store-silent", "store-t0"]
    assert entries["store-a"] == {
        "store_id": "store-a",
        "tier": 1,
        "fallback": False,
        "unit_price": 80.0,
        "total_price": 80.0,
        "fallback_reason": None,
    }
    assert entries["store-silent"]["fallback"] is True
    assert entries["store-silent"]["unit_price"] == 150.0
    assert entries["store-t0"]["fallback_reason"] == "tier_0_no_agent"

    assert [d["store_id"] for d in body["denied"]] == ["store-black"]
    assert "blacklist" in body["denied"][0]["reason"].lower()

    # The state machine ran, and both transitions reached the ledger — with the auction's own
    # evidence between them. This used to assert the sink held EXACTLY the two transitions,
    # which was true only because a served auction recorded nothing about what it collected or
    # what it showed: no `bid_placed` for a bid it received, no `shown` for a slot it filled.
    # The order below is the sequence the ledger has to read in, and each count is checked, so
    # this is a stronger statement than the exact list it replaces rather than a relaxed one.
    read_back = client.get(f"/auctions/{body['auction_id']}")
    assert read_back.status_code == 200
    assert read_back.json()["state"] == "closed"
    kinds = app.state.auction_machine.ledger.sink.kinds
    assert kinds[0] == "auction_opened"
    assert kinds.count("auction_opened") == 1
    assert kinds.count("auction_closed") == 1
    # One receipt per COLLECTED bid — every store the exchange represented, the R10 list-price
    # fallbacks included — written before the close.
    assert kinds.count("bid_placed") == len(body["entries"]) == 3
    assert kinds.index("bid_placed") < kinds.index("auction_closed")
    # One `shown` per shortlist slot — which is ZERO here, and that is the point rather than a
    # gap: this app wires no trust snapshot and no registered domains, so every candidate is
    # excluded and the auction shows nobody (the sibling test below pins that same fail-closed
    # behaviour from the response side). An exchange that announced a slot it did not fill
    # would be announcing something it never served.
    assert body["shortlist"]["slots"] == []
    assert kinds.count("shown") == len(body["shortlist"]["slots"]) == 0
    assert not set(kinds) - {"auction_opened", "bid_placed", "auction_closed", "shown"}, (
        f"a served auction wrote a ledger kind this test does not account for: {kinds}"
    )

    assert client.get("/auctions/auction-never-existed").status_code == 404


def test_an_unwired_exchange_denies_every_store_rather_than_admitting_every_store() -> None:
    """R12's fail-closed rule applied to the deployment, not only to a single read."""
    client = TestClient(create_app())
    body = client.post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
            "roster": [rostered("store-a", 120.0)],
            "bid_timeout_seconds": 0.5,
        },
    ).json()

    assert body["solicited"] == []
    assert body["entries"] == []
    assert [d["store_id"] for d in body["denied"]] == ["store-a"]
    assert "unavailable" in body["denied"][0]["reason"].lower()


# =====================================================================================
# The arrival stamp is the exchange's, never the bidder's (R10)
#
# `collect_bids` enforces the deadline on `received_at`. A store that can set that field is
# a store that sets its own deadline, and `store_id` decides whose bid a reply even is —
# so both are stamped by the exchange, over the top of whatever the payload carried.
# =====================================================================================
class Ticker:
    """A monotonic source the test advances by hand. Nothing sleeps; nothing is patched."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class StallingSolicitor:
    """Answers after ``delays[store_id]`` seconds of the exchange's clock, and lies about it.

    Every reply claims ``received_at = T_NOW - 1.0`` — "I answered a second before the
    close" — whatever the truth was. That is the forgery.
    """

    def __init__(
        self, prices: dict[str, float], tick: Ticker, delays: dict[str, float] | None = None
    ) -> None:
        self.prices = dict(prices)
        self.tick = tick
        self.delays = dict(delays or {})
        self.asked: list[str] = []

    def solicit(self, store: dict[str, Any]) -> dict[str, Any]:
        store_id = store["store_id"]
        self.asked.append(store_id)
        self.tick.advance(self.delays.get(store_id, 0.0))
        return response(store_id, self.prices[store_id], T_NOW - 1.0)

    __call__ = solicit


def test_the_exchange_overwrites_a_store_supplied_arrival_stamp() -> None:
    """The store's claim is kept for audit and used for nothing."""

    def solicitor(store: dict[str, Any]) -> dict[str, Any]:
        return response(store["store_id"], 1.0, T_NOW - 1.0)  # "I was early", says the store

    answered = sequential_fan_out(
        [rostered("store-a", 120.0)], solicitor, clock=lambda: T_NOW + 7.0
    )
    assert answered[0]["received_at"] == T_NOW + 7.0, "the exchange's clock must win"
    assert answered[0]["store_reported_received_at"] == T_NOW - 1.0


def test_a_store_that_answered_late_cannot_forge_its_way_back_inside_the_window() -> None:
    """The confirmed exploit: stall past the close, stamp yourself on time, win at 1.00.

    Deterministic — the window is real seconds but the clock behind it is driven by the
    test, so nothing here sleeps and nothing depends on machine speed.
    """
    tick = Ticker()
    solicitor = StallingSolicitor(
        {"store-honest": 90.0, "store-cheat": 1.0, "store-last": 95.0},
        tick,
        delays={"store-cheat": 5.0},  # five seconds inside a one-second window
    )
    roster = [
        rostered("store-honest", 120.0),
        rostered("store-cheat", 999.0),
        rostered("store-last", 140.0),
    ]

    result = solicit_bids(
        roster=roster,
        solicitor=solicitor,
        eligibility=StaticSellerEligibility(
            dict.fromkeys((r["store_id"] for r in roster), ELIGIBLE)
        ),
        now=T_NOW,
        clock=ArrivalClock(T_NOW, window=1.0, monotonic=tick),
    )
    by_store = {entry.store_id: entry for entry in result.entries}

    # The forged stamp buys nothing: the cheat is late and falls back to its own list price.
    assert by_store["store-cheat"].fallback is True
    assert by_store["store-cheat"].fallback_reason == "response_after_deadline"
    assert by_store["store-cheat"].unit_price == 999.0, "the 1.00 bid must not survive"

    # And the window really closed: nobody after the straggler is asked at all.
    assert solicitor.asked == ["store-honest", "store-cheat"]
    assert by_store["store-last"].fallback is True
    assert by_store["store-last"].fallback_reason == "no_response"

    # The positive control: an on-time bidder is still a bidder, or this proves nothing.
    assert by_store["store-honest"].fallback is False
    assert by_store["store-honest"].unit_price == 90.0


def test_the_default_solicitation_path_measures_real_elapsed_time() -> None:
    """No clock injected: the exploit must fail against the wiring a deployment gets.

    Wall-clock by nature (it is the only way to show the *default* clock is authoritative),
    so the window is short and the stall only has to beat it.
    """

    class LateLiar:
        def solicit(self, store: dict[str, Any]) -> dict[str, Any]:
            if store["store_id"] == "store-cheat":
                time.sleep(0.3)
            return response(store["store_id"], 1.0, T_NOW - 1.0)

        __call__ = solicit

    result = solicit_bids(
        roster=[rostered("store-cheat", 999.0)],
        solicitor=LateLiar(),
        eligibility=StaticSellerEligibility({"store-cheat": ELIGIBLE}),
        now=T_NOW,
        window=0.05,
    )
    assert result.entries[0].fallback is True
    assert result.entries[0].unit_price == 999.0


def test_a_store_cannot_answer_under_a_rivals_name() -> None:
    """One reply, attributed to the store that was ASKED — not the store the reply names.

    Without that, the first store on the roster posts a ruinous bid as its rival: the rival's
    real bid is discarded as a duplicate, and the impersonator wins at its own list price an
    auction it had comfortably lost.
    """

    class Impersonator:
        def solicit(self, store: dict[str, Any]) -> dict[str, Any]:
            if store["store_id"] == "store-cheat":
                return response("store-rival", 500.0, T_NOW - 2.0)  # "signed", store-rival
            return response(store["store_id"], 50.0, T_NOW - 1.0)

        __call__ = solicit

    roster = [rostered("store-cheat", 120.0), rostered("store-rival", 130.0)]
    result = solicit_bids(
        roster=roster,
        solicitor=Impersonator(),
        eligibility=StaticSellerEligibility({"store-cheat": ELIGIBLE, "store-rival": ELIGIBLE}),
        now=T_NOW,
    )
    by_store = {entry.store_id: entry for entry in result.entries}

    assert by_store["store-rival"].unit_price == 50.0, "the rival's real bid was displaced"
    assert by_store["store-cheat"].unit_price == 500.0, "the forged bid is the cheat's own"
    # The bid body is re-attributed too: everything downstream reads it to name the seller.
    assert by_store["store-cheat"].bid["store_id"] == "store-cheat"


def test_the_forged_identity_is_recorded_for_the_operator() -> None:
    def solicitor(store: dict[str, Any]) -> dict[str, Any]:
        return response("store-somebody-else", 1.0, T_NOW - 1.0)

    answered = sequential_fan_out([rostered("store-a", 120.0)], solicitor, clock=lambda: T_NOW)
    assert answered[0]["store_id"] == "store-a"
    assert answered[0]["store_reported_store_id"] == "store-somebody-else"
    assert answered[0]["bid"]["store_id"] == "store-a"


def test_an_arrival_stamp_that_will_not_parse_is_late_rather_than_a_crash() -> None:
    """One malformed reply used to take down the auction every other store was bidding in."""
    roster = [rostered("store-junk", 150.0), rostered("store-ok", 120.0)]
    junk = response("store-junk", 1.0, T_NOW - 1.0)
    junk["received_at"] = "whenever"

    by_store = {
        entry.store_id: entry
        for entry in collect_bids(roster, [junk, response("store-ok", 80.0, T_NOW)], T_NOW)
    }
    assert by_store["store-junk"].fallback is True
    assert by_store["store-junk"].unit_price == 150.0
    assert by_store["store-ok"].fallback is False, "the rest of the field still bid"


def test_the_arrival_clock_reports_elapsed_time_in_the_deadlines_own_frame() -> None:
    """What makes an authoritative stamp compatible with a frozen/logical deadline."""
    tick = Ticker()
    clock = ArrivalClock(T_NOW, window=2.0, monotonic=tick)
    assert clock() == T_NOW - 2.0
    tick.advance(1.5)
    assert clock() == T_NOW - 0.5, "still inside the window"
    tick.advance(1.0)
    assert clock() == T_NOW + 0.5, "past it, and detectably so"


# =====================================================================================
# T-352 — a caller-chosen identifier is a NAME, and this door bounds its length
#
# `CreateAuctionRequest.intent` is `dict[str, Any]`, so pydantic validates NOTHING inside it
# and `_refuse_an_oversized_intent` weighs only `hard_constraints`. `create_auction` then
# writes `intent["intent_id"]` and `intent["cluster_id"]` into the `AuctionRecord`, which is
# saved into a store whose in-memory default is a plain dict its own comment calls
# "Unbounded" — no capacity, no TTL, no eviction — and copied a second time into the
# `auction_opened` and `auction_closed` ledger payloads. `GET /auctions/{auction_id}` hands
# both strings straight back to an anonymous reader.
#
# Measured on this tree before the bound, against the served app with no credential of any
# kind (`create_app()` + `TestClient`, one rostered store, 64 requests):
#
#     honest ids                          -> 201 x64, 64 records,   0.029 MiB retained
#     30,000-char intent_id + cluster_id  -> 201 x64, 64 records,   3.690 MiB retained
#     GET /auctions/{id}                  -> 200, 30,003-char intent_id, 60,233-byte body
#     intent_id as ["A"*128] * 20,000     -> 201 x1,  1 record,  2,640,454 chars retained
#
# The last line is the same hole through a value that is not a string at all: the route
# manufactured the identifier with `str()`, so ONE request retained 2.5 MiB. So the gate
# below grades the type as well as the length — a bound that only looks at `str` values is a
# bound a caller steps around by sending a list.
#
# The ceiling is the package's own `MAX_IDENTIFIER_LENGTH`, the number `RosterEntry.store_id`
# on this very request already holds the same anonymous caller to, and the number the
# external bid door's `_oversized_identifier` imports rather than restates.
# =====================================================================================
HOSTILE_IDENTIFIER_CHARS = 30_000

#: A value that is not a string, whose `str()` spelling is far past the ceiling. This is the
#: shape that made ONE request retain 2.5 MiB, and it is built from the ceiling rather than
#: from a magic number so it cannot drift away from the bound it is probing.
NON_STRING_IDENTIFIER = ["A" * MAX_IDENTIFIER_LENGTH] * 20_000


def _identifier_auction_body(**intent_fields: Any) -> dict[str, Any]:
    """A well-formed `POST /auctions` body whose intent carries `intent_fields`."""
    return {
        "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1", **intent_fields},
        "roster": [rostered("store-a", 120.0)],
        "bid_timeout_seconds": 0.01,
    }


def _retained_records(app: Any) -> dict[str, str]:
    """The JSON blobs the auction store is holding — the thing the caller was sizing."""
    return dict(app.state.auction_machine.store._records)


@pytest.mark.parametrize("field", ["intent_id", "cluster_id"])
def test_an_over_long_caller_chosen_identifier_is_refused_and_retains_nothing(
    field: str,
) -> None:
    """The defect itself: an anonymous caller choosing how much of this process to keep."""
    app = create_app()
    client = TestClient(app)
    hostile = "A" * HOSTILE_IDENTIFIER_CHARS

    posted = client.post("/auctions", json=_identifier_auction_body(**{field: hostile}))

    assert posted.status_code == 422, (
        f"an {HOSTILE_IDENTIFIER_CHARS}-character {field} was accepted with "
        f"{posted.status_code}; a caller-chosen identifier is a name, not a document"
    )
    assert _retained_records(app) == {}, (
        f"the auction record was written anyway, so the oversized {field} is retained for "
        f"the store's whole lifetime in a dict with no eviction"
    )
    assert app.state.auction_machine.ledger.sink.kinds == [], (
        "the refusal happened after the ledger had already copied the identifier into an "
        "auction_opened payload"
    )


@pytest.mark.parametrize("field", ["intent_id", "cluster_id"])
def test_the_refusal_does_not_echo_the_oversized_identifier_back(field: str) -> None:
    """A refusal that quotes the offending input is the amplifier it was meant to close."""
    client = TestClient(create_app())
    hostile = "A" * HOSTILE_IDENTIFIER_CHARS

    posted = client.post("/auctions", json=_identifier_auction_body(**{field: hostile}))

    assert 400 <= posted.status_code < 500, (
        f"refusing an oversized {field} answered {posted.status_code}; a hostile body must "
        f"never reach a 5xx on an unauthenticated door"
    )
    assert "A" * (MAX_IDENTIFIER_LENGTH + 1) not in posted.text, (
        f"the 422 for an oversized {field} echoed the identifier back"
    )
    assert len(posted.content) < 4096, (
        f"the refusal body is {len(posted.content)} bytes; it must not grow with the size of "
        f"what it refused"
    )


@pytest.mark.parametrize("field", ["intent_id", "cluster_id"])
def test_a_caller_chosen_identifier_at_the_ceiling_is_still_accepted(field: str) -> None:
    """The positive control: the bound refuses over-long names, not every name."""
    app = create_app()
    client = TestClient(app)
    at_the_ceiling = "n" * MAX_IDENTIFIER_LENGTH

    posted = client.post("/auctions", json=_identifier_auction_body(**{field: at_the_ceiling}))

    assert posted.status_code == 201, (
        f"a {field} of exactly MAX_IDENTIFIER_LENGTH ({MAX_IDENTIFIER_LENGTH}) was refused "
        f"{posted.status_code}; the bound is a ceiling, not a smaller one"
    )
    read_back = client.get(f"/auctions/{posted.json()['auction_id']}")
    assert read_back.json()[field] == at_the_ceiling


def test_an_identifier_that_is_not_a_string_is_refused_rather_than_manufactured() -> None:
    """`str()` on a caller's structure is the same lever reached through a different type.

    The published ``Intent`` declares ``intent_id`` as a ``string`` and ``cluster_id`` as
    ``string``/``null``, so nothing on contract is lost by refusing a list — and everything
    is lost by spelling one out: measured, ONE request retained 2,640,454 characters.
    """
    app = create_app()
    client = TestClient(app)

    posted = client.post(
        "/auctions", json=_identifier_auction_body(intent_id=NON_STRING_IDENTIFIER)
    )

    assert posted.status_code == 422, (
        f"a list-valued intent_id was accepted with {posted.status_code} and spelled into an "
        f"identifier by str()"
    )
    assert _retained_records(app) == {}
    assert len(posted.content) < 4096, "the refusal echoed the manufactured identifier back"


def test_hostile_identifiers_retain_no_more_of_the_process_than_honest_ones() -> None:
    """The ticket's own reproduction, as a bound rather than as an anecdote.

    64 requests each way. Before the bound the hostile run retained 3.690 MiB against the
    honest run's 0.029 MiB — 127x, linear in the request count with no plateau, in a
    container ``apps/exchange/compose.yaml`` limits to 256 MiB.
    """
    requests = 64
    honest_app = create_app()
    honest_client = TestClient(honest_app)
    for index in range(requests):
        honest_client.post(
            "/auctions",
            json=_identifier_auction_body(
                intent_id=f"intent-{index}", cluster_id=f"cluster-{index}"
            ),
        )
    honest = sum(len(blob) for blob in _retained_records(honest_app).values())

    hostile_app = create_app()
    hostile_client = TestClient(hostile_app)
    for index in range(requests):
        hostile_client.post(
            "/auctions",
            json=_identifier_auction_body(
                intent_id="A" * HOSTILE_IDENTIFIER_CHARS + f"-{index}",
                cluster_id="B" * HOSTILE_IDENTIFIER_CHARS + f"-{index}",
            ),
        )
    hostile = sum(len(blob) for blob in _retained_records(hostile_app).values())

    assert honest > 0, "the honest control retained nothing, so the comparison grades nothing"
    assert hostile <= honest, (
        f"{requests} hostile requests retained {hostile} characters against the honest "
        f"run's {honest}: the caller still chooses how much of this process to keep"
    )


def test_an_identifier_no_response_encoder_can_emit_is_refused_at_the_door() -> None:
    r"""A SECOND defect on the same two fields, found while bounding their length.

    A LONE SURROGATE — ``"\ud800"``, which a caller writes as a plain ``\uXXXX`` escape and
    which ``json.loads`` accepts into a perfectly ordinary ``str`` — is far inside the length
    ceiling and survives ``AuctionRecord.to_json`` (``json.dumps`` defaults to
    ``ensure_ascii=True``, so it is stored as the escape). Starlette renders with
    ``ensure_ascii=False`` and then ``.encode("utf-8")``, which raises ``UnicodeEncodeError``
    on it. Measured on this tree, over the served app with no credential::

        POST /auctions  {"intent": {"intent_id": "x\ud800y", ...}}  -> 201
        GET  /auctions/{auction_id}                                 -> 500

    So an anonymous caller could plant an auction that answers 500 to every subsequent read of
    it, for the record's whole lifetime. It is the same SHAPE as T-270 and as the defect
    ``external_bids/routes.py::_renderable`` closes, reached through the field T-352 is about.

    Refused at the DOOR rather than replaced on the way out, which is the opposite of what
    that sibling does and is the right way round here: the external door must still answer a
    refusal, so it has to render something, whereas an identifier the exchange can never serve
    back is not a name it should have accepted. Nothing on contract is lost — the published
    ``Intent`` declares a JSON string, and no encodable string is affected.
    """
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    raw = (
        b'{"intent": {"intent_id": "x\\ud800y", "cluster_id": "c"}, '
        b'"roster": [{"store_id": "store-a", "tier": 1, "product_ref": "product-1", '
        b'"list_price": 120.0}], "bid_timeout_seconds": 0.01}'
    )

    posted = client.post("/auctions", content=raw, headers={"content-type": "application/json"})

    assert posted.status_code == 422, (
        f"an identifier carrying a lone surrogate was accepted with {posted.status_code}; "
        f"the auction it opened answers 500 to every reader of GET /auctions/{{auction_id}}"
    )
    assert _retained_records(app) == {}


# ---------------------------------------------------------------------------------
# The roster's list price reaches the ranker (R11)
#
# `price_value = clamp((list_price - total_price)/list_price, 0, 1)` (DESIGN.md:127) is one
# of the five published rank features, and `list_price` exists ONLY on the roster row: the
# bid states what the store charges, never what the product lists at. Until `BidEntry`
# carried it, the ranker held one half of that subtraction and the feature was absent on
# every served candidate — so the published formula's offer-value term did nothing at all.
# ---------------------------------------------------------------------------------
def test_every_entry_carries_the_rosters_list_price() -> None:
    """Both branches: a store that answered, and one that did not."""
    entries = collect_bids(
        [rostered("store-a", 120.0), rostered("store-b", 80.0)],
        [response("store-a", 90.0, T_NOW - 1.0)],
        T_NOW,
    )
    by_store = {entry.store_id: entry for entry in entries}

    assert by_store["store-a"].fallback is False
    assert by_store["store-a"].list_price == pytest.approx(120.0)
    assert by_store["store-b"].fallback is True
    assert by_store["store-b"].list_price == pytest.approx(80.0), (
        "a fallback entry lost the roster price it was minted from, so the ranker cannot "
        "compare its list-price offer against anything"
    )


def test_the_entrys_list_price_is_the_rosters_and_never_the_bids() -> None:
    """A store writing a list price into its own reply states nothing.

    It is the same rule as `store_id` and `store_domain`: the list price is the other half of
    the saving the buyer is shown, so a bidder supplying both halves would be publishing its
    own discount.
    """
    answered = response("store-a", 90.0, T_NOW - 1.0)
    answered["bid"]["list_price"] = 9_999.0
    answered["bid"]["offer"]["list_price"] = 9_999.0

    entry = collect_bids([rostered("store-a", 120.0)], [answered], T_NOW)[0]

    assert entry.list_price == pytest.approx(120.0)


@pytest.mark.parametrize("listed", [None, 0.0, -10.0, "cheap", float("nan"), float("inf"), True])
def test_a_roster_row_that_prices_nothing_states_no_list_price(listed) -> None:
    """Absent, zero, negative and unreadable all mean the same thing: no comparison.

    `None` rather than `0.0`, because `0.0` on this field is a list price of nothing, against
    which every bid is a 100% markup — or, taken the other way, a free product. The ranker
    reads the absence as "this exchange cannot price the product" and the feature takes its
    published neutral instead of a number nobody could defend. This is the same set of
    spellings `_list_price_bid` already mints no offer for (T-277/T-224).
    """
    row = rostered("store-a", 100.0)
    if listed is None:
        row.pop("list_price")
    else:
        row["list_price"] = listed

    entry = collect_bids([row], [response("store-a", 90.0, T_NOW - 1.0)], T_NOW)[0]

    assert entry.list_price is None, f"list_price={listed!r} was read as a price"
