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
from exchange.auction.routes import configure_auctions
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
        answered = {r["store_id"] for r in responses}
        assert "store-quick" in answered
        assert "store-hung" not in answered, "a store that missed the window was counted"

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
    responses = parallel_fan_out(roster, solicitor, deadline=time.time() + 2.0)
    entries = {e.store_id: e for e in collect_bids(roster, responses, T_NOW)}

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

    # The state machine ran, and both transitions reached the ledger.
    read_back = client.get(f"/auctions/{body['auction_id']}")
    assert read_back.status_code == 200
    assert read_back.json()["state"] == "closed"
    assert app.state.auction_machine.ledger.sink.kinds == ["auction_opened", "auction_closed"]

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
