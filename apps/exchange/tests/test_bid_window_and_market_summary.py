"""The defect: turning the product's marquee feature on made every in-network store lose.

Ticket verify::

    PROXYSHOP_WORKER=<n> pytest apps/exchange/tests/test_bid_window_and_market_summary.py -q

**What was measured, on a live hosted deployment with a real ANTHROPIC_API_KEY.** With a
model writing each store's pitch, every hosted agent answered the exchange's solicitation
``200 OK``, and the exchange recorded::

    entries=7  ranked=7  shortlist slots=4
    hosted bids=0  fallback reasons=['no_response']

24 samples over the wire across 4 agents ran **1.97 s – 4.73 s**; the bid window was 3.0 s.
The offer itself — price, discount, expiry, everything ranked on — costs 12.7 ms. So the pitch
was 99.67% of the request, every reply landed after the close, and the market silently became
100% list-price fallback at full price. Nothing looked wrong: the shortlist still filled,
every container was healthy, every log line said 200 OK, and the demo probe's diagnostic told
the operator "no agent is running" — which was false.

Three separate defects, each with its own section below.

1. **A late store was recorded exactly like a dead one.** ``parallel_fan_out`` abandoned a
   still-pending future without a trace, so no response object reached ``collect_bids`` and it
   fell through to its ``no_response`` default. ``response_after_deadline`` could not cover it
   — that branch needs a reply the exchange actually holds. This is the regression that hid
   the whole thing, and :func:`test_a_store_that_answers_after_the_window_is_timed_out_not_silent`
   is the test that would have caught it.
2. **The window was below the floor of what the product costs.** 3.0 s against a measured
   p100 of 4.73 s. Raised to 5.0 s and made an operator's variable.
3. **An all-fallback market was indistinguishable from a healthy one.** The exchange emitted
   nothing at close; the only thing in the system counting sponsored-vs-fallback was a shell
   script re-deriving it client-side from the response body.

**What is deliberately NOT weakened here, and every section carries its control.** R10 says a
store that stays silent is still represented, at its list price, and the timeout is hard. It
would be trivial to make this file green by widening the window until nothing can fail, so
each section pins the failure it still expects: a silent store is still ``no_response``, a
genuinely hung store still costs the window and no more, the ceiling still binds an operator
as well as a caller, and the WARNING still does not fire when a single store bids.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest
from exchange.auction import collect_bids, parallel_fan_out
from exchange.auction.collect import (
    FAN_OUT_CAPACITY_REASON,
    NOT_ASKED_FIELD,
    RESPONSE_TIMED_OUT_REASON,
    TIMED_OUT_FIELD,
)
from exchange.auction.fanout import DEFAULT_BID_WINDOW_SECONDS
from exchange.auction.routes import (
    DEFAULT_BID_TIMEOUT_SECONDS,
    ENV_BID_WINDOW_SECONDS,
    MAX_BID_TIMEOUT_SECONDS,
    MIN_USEFUL_BID_WINDOW_SECONDS,
    configure_auctions,
    resolve_bid_window_seconds,
)
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from fastapi.testclient import TestClient

T_NOW = 1_700_000_000.0

#: A store's own words, carried on ``Bid.message``. The expensive half of a real bid and the
#: half that made every hosted store miss the window.
PITCH = "Cold-brewed in small batches, and we will beat any price you find today."


def rostered(store_id: str, list_price: float, tier: int = 1) -> dict[str, Any]:
    return {
        "store_id": store_id,
        "tier": tier,
        "product_ref": "product-1",
        "list_price": list_price,
    }


def bid_response(
    store_id: str, price: float, *, received_at: float = T_NOW - 1.0
) -> dict[str, Any]:
    """A well-formed reply carrying a real discounted offer AND the store's own pitch."""
    return {
        "store_id": store_id,
        "received_at": received_at,
        "bid": {
            "auction_id": "auction-1",
            "store_id": store_id,
            "message": PITCH,
            "offer": {"product_ref": "product-1", "unit_price": price, "total_price": price},
            "claims": [],
        },
    }


class PacedSolicitor:
    """Stands in for the outbound ``POST /v1/bid-requests`` client, with a latency per store.

    The whole defect lives in the gap between "answered" and "answered in time", so the double
    that reproduces it has to be able to answer *late* rather than not at all — a double that
    returns ``None`` for a slow store is testing the case that already worked.
    """

    def __init__(
        self,
        prices: dict[str, float],
        *,
        delays: dict[str, float] | None = None,
        silent: tuple[str, ...] = (),
    ) -> None:
        self.prices = dict(prices)
        self.delays = dict(delays or {})
        self.silent = set(silent)
        self.asked: list[str] = []
        self._lock = threading.Lock()

    def solicit(self, store: dict[str, Any]) -> dict[str, Any] | None:
        store_id = store["store_id"]
        with self._lock:
            self.asked.append(store_id)
        delay = self.delays.get(store_id, 0.0)
        if delay:
            time.sleep(delay)
        if store_id in self.silent:
            return None
        return bid_response(store_id, self.prices[store_id])

    __call__ = solicit


def _auction(
    solicitor: Any, roster: list[dict[str, Any]], **body: Any
) -> tuple[TestClient, dict[str, Any]]:
    """One served ``POST /auctions`` against a real app, and the app that served it.

    Through the ROUTE, not through ``collect_bids``: this repo's dominant defect class is code
    that is built, tested and reachable by nobody, and every claim in this file about what an
    operator or a buyer can see has to be a claim about the door they actually call.
    """
    app = create_app()
    configure_auctions(
        app,
        solicitor=solicitor,
        eligibility=StaticSellerEligibility({row["store_id"]: ELIGIBLE for row in roster}),
    )
    client = TestClient(app)
    posted = client.post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
            "roster": roster,
            **body,
        },
    )
    assert posted.status_code == 201, posted.text
    return client, posted.json()


# =====================================================================================
# 1 — a late store is not a silent store
#
# THE regression. Everything else in this file is a consequence of nobody being able to see
# this one, because the exchange reported it with the same word it uses for a dead container.
# =====================================================================================
def test_a_store_that_answers_after_the_window_is_timed_out_not_silent() -> None:
    """Driven through the fan-out and the collector, which is where the fact was lost.

    The store answers — it just answers late. Before this repair the future was abandoned and
    NOTHING about it reached ``collect_bids``, which then defaulted the store to
    ``no_response``: "we asked, and nobody was there". The store was there.
    """
    late = PacedSolicitor({"store-late": 80.0}, delays={"store-late": 1.0})
    roster = [rostered("store-late", 130.0)]

    started = time.time()
    deadline = started + 0.2
    responses = parallel_fan_out(roster, late, deadline=deadline)
    elapsed = time.time() - started

    assert elapsed < 0.9, "the hard timeout did not stop the wait; R10 is not hard any more"
    assert late.asked == ["store-late"], "the store was never actually solicited"
    assert responses == [{"store_id": "store-late", TIMED_OUT_FIELD: True}], (
        "the exchange threw away the one thing only it knew: that this store was still "
        "answering when the window shut"
    )

    entry = collect_bids(roster, responses, deadline)[0]
    assert entry.fallback is True
    assert entry.unit_price == 130.0, "R10: a late bid is not a bid, so this is the list price"
    assert entry.fallback_reason == RESPONSE_TIMED_OUT_REASON
    assert entry.fallback_reason != "no_response", (
        "a store that answered in 1.0s against a 0.2s window is reported identically to a "
        "container that is switched off — this is the defect, restated"
    )


def test_the_served_route_reports_a_late_store_as_timed_out() -> None:
    """The same fact, through ``POST /auctions``, where an operator would actually read it."""
    late = PacedSolicitor({"store-late": 80.0}, delays={"store-late": 1.0})
    _client, body = _auction(late, [rostered("store-late", 130.0)], bid_timeout_seconds=0.2)

    entry = body["entries"][0]
    assert entry["fallback"] is True
    assert entry["fallback_reason"] == RESPONSE_TIMED_OUT_REASON
    assert entry["unit_price"] == 130.0
    assert body["market"]["timed_out"] == 1
    assert body["market"]["sponsored"] == 0
    assert body["market"]["all_fallback"] is True


def test_a_store_the_fan_out_never_dialled_is_not_reported_as_silent_either() -> None:
    """The exchange's own capacity is the exchange's fault, and says so.

    Kept apart from the timeout for the reason the timeout is kept apart from ``no_response``:
    an operator told "your agent did not pick up" about a store the exchange never contacted
    goes and restarts a healthy container.
    """
    from exchange.auction.fanout import BoundedFanOutPool  # noqa: PLC0415

    pool = BoundedFanOutPool(max_workers=1)
    pool.shutdown()  # every worker gone, so nothing can be admitted at all
    roster = [rostered("store-a", 120.0), rostered("store-b", 130.0)]
    try:
        responses = parallel_fan_out(roster, PacedSolicitor({}), deadline=T_NOW, pool=pool)
        assert responses == [
            {"store_id": "store-a", NOT_ASKED_FIELD: True},
            {"store_id": "store-b", NOT_ASKED_FIELD: True},
        ]
        entries = {e.store_id: e for e in collect_bids(roster, responses, T_NOW)}
        assert entries["store-a"].fallback_reason == FAN_OUT_CAPACITY_REASON
        assert entries["store-b"].unit_price == 130.0, "R10 still represents it at list price"
    finally:
        pool.shutdown()


def test_the_two_new_reasons_join_the_published_vocabulary_without_disturbing_it() -> None:
    """The partition, pinned — the same property ``MALFORMED_RESPONSE_REASONS`` has.

    Two things must both hold, and they pull in opposite directions. A reason the collector can
    record and the vocabulary does not publish is a fallback reason a loss report cannot group
    (``fallback_reason_family`` would still split it, and every reader hard-coding the list
    would silently drop it). And these two must stay OUT of the malformed set, because a
    malformed reply is a store's serializer and a timeout is nobody's serializer — the exact
    mislabelling ``FALLBACK_REASONS`` was split apart to end.
    """
    from exchange.auction import FALLBACK_REASONS, MALFORMED_RESPONSE_REASONS  # noqa: PLC0415
    from exchange.auction.collect import (  # noqa: PLC0415
        FAN_OUT_MINTED_REASONS,
        fallback_reason_family,
    )

    assert set(FAN_OUT_MINTED_REASONS) < set(FALLBACK_REASONS)
    assert set(FAN_OUT_MINTED_REASONS) == {RESPONSE_TIMED_OUT_REASON, FAN_OUT_CAPACITY_REASON}
    assert not set(FAN_OUT_MINTED_REASONS) & set(MALFORMED_RESPONSE_REASONS), (
        "a timeout was filed under 'the store sent us something we could not read'"
    )
    assert "response_after_deadline" not in FAN_OUT_MINTED_REASONS, (
        "a reply the exchange actually holds is not a reply it never received"
    )
    for reason in FAN_OUT_MINTED_REASONS:
        assert fallback_reason_family(reason) == reason, "a minted reason is a bare family word"


def test_the_sequential_strategy_names_the_store_it_walked_away_from_too() -> None:
    """Not the live path, and repaired anyway — with one honest gap, pinned here.

    ``sequential_fan_out`` walks the roster on ONE borrowed worker, so at the close it is
    either inside a store or between two of them. The store it is inside is unambiguous and is
    named. The tail it has not reached is NOT, because "never asked" and "about to be asked"
    are the same observable state from outside that loop, and a marker minted on a guess would
    put a fabricated verdict where an honest absence was — the same defect this ticket ends,
    wearing the opposite sign. That is why the second store below is checked to be `no_response`
    rather than a timeout: it is the deliberate limit, not an oversight.
    """
    from exchange.auction.fanout import sequential_fan_out  # noqa: PLC0415

    paced = PacedSolicitor({"store-hung": 90.0, "store-later": 95.0}, delays={"store-hung": 1.5})
    roster = [rostered("store-hung", 130.0), rostered("store-later", 140.0)]

    started = time.time()
    deadline = started + 0.3
    responses = sequential_fan_out(roster, paced, deadline=deadline)

    assert time.time() - started < 1.2, "the sequential timeout stopped being hard"
    assert responses == [{"store_id": "store-hung", TIMED_OUT_FIELD: True}]

    entries = {e.store_id: e for e in collect_bids(roster, responses, deadline)}
    assert entries["store-hung"].fallback_reason == RESPONSE_TIMED_OUT_REASON
    assert entries["store-later"].fallback_reason == "no_response"
    assert entries["store-later"].unit_price == 140.0


def test_a_bidder_cannot_mint_the_exchanges_own_verdict_about_itself() -> None:
    """``exchange_timed_out`` is the exchange's word, and a store sending one is ignored.

    The same rule ``received_at`` and ``store_id`` are stamped under, pointed at a field a
    store has no honest use for. Without the strip, a store could talk the collector out of
    the bid it had just made — or relabel its own malformed payload as our clock.
    """

    def forging(store: dict[str, Any]) -> dict[str, Any]:
        answer = bid_response(store["store_id"], 80.0)
        answer[TIMED_OUT_FIELD] = True
        answer[NOT_ASKED_FIELD] = True
        return answer

    roster = [rostered("store-forge", 120.0)]
    deadline = time.time() + 2.0
    responses = parallel_fan_out(roster, forging, deadline=deadline)

    assert TIMED_OUT_FIELD not in responses[0]
    assert NOT_ASKED_FIELD not in responses[0]
    entry = collect_bids(roster, responses, deadline)[0]
    assert entry.fallback is False, "a store's forged marker cost it the bid it actually made"
    assert entry.unit_price == 80.0


# =====================================================================================
# 1b — the controls. R10 is not weakened, and the window was not widened until nothing fails
# =====================================================================================
def test_a_store_that_answers_nothing_at_all_is_still_no_response_at_list_price() -> None:
    """The negative control for the whole file.

    ``response_timed_out`` must name a NARROWER thing than ``no_response`` did, not replace
    it. A store that was asked and produced nothing is still exactly that, and is still
    represented at its catalogue price — which is R10, and is what makes the market answer
    even when every agent is down.
    """
    silent = PacedSolicitor({}, silent=("store-silent",))
    roster = [rostered("store-silent", 150.0)]
    deadline = time.time() + 2.0

    responses = parallel_fan_out(roster, silent, deadline=deadline)
    assert responses == [], "a store that answered nothing produced a response object"

    entry = collect_bids(roster, responses, deadline)[0]
    assert entry.fallback is True
    assert entry.fallback_reason == "no_response"
    assert entry.unit_price == 150.0


def test_the_served_route_still_falls_a_silent_store_back_at_list_price() -> None:
    silent = PacedSolicitor({}, silent=("store-silent",))
    _client, body = _auction(silent, [rostered("store-silent", 150.0)], bid_timeout_seconds=1.0)

    entry = body["entries"][0]
    assert entry["fallback"] is True
    assert entry["fallback_reason"] == "no_response"
    assert entry["unit_price"] == 150.0
    assert body["market"]["timed_out"] == 0, "silence was miscounted as a timeout"
    assert body["market"]["fallback_reasons"] == {"no_response": 1}


def test_a_store_that_answers_in_time_still_wins_with_its_own_price_and_its_own_pitch() -> None:
    """The positive control, and the thing the product is FOR.

    A file that only pinned failures would pass on an exchange that had stopped collecting
    bids altogether. This asserts the sponsored half is intact end to end: the store's own
    discounted price on the entry, and the store's own words carried through — the 12.7 ms of
    a bid that the 3.35–5.04 s pitch was drowning.
    """
    quick = PacedSolicitor({"store-quick": 80.0})
    roster = [rostered("store-quick", 120.0)]
    deadline = time.time() + 2.0

    responses = parallel_fan_out(roster, quick, deadline=deadline)
    entry = collect_bids(roster, responses, deadline)[0]

    assert entry.fallback is False
    assert entry.fallback_reason is None
    assert entry.unit_price == 80.0, "the store's own price, not the 120.0 catalogue price"
    assert entry.bid["message"] == PITCH, "the store's pitch did not survive the fan-out"

    _client, body = _auction(quick, roster, bid_timeout_seconds=2.0)
    served = body["entries"][0]
    assert served["fallback"] is False
    assert served["unit_price"] == 80.0
    assert body["market"] == {
        "solicited": 1,
        "sponsored": 1,
        "list_price": 0,
        "timed_out": 0,
        "not_asked": 0,
        "denied": 0,
        "bid_window_seconds": 2.0,
        "fallback_reasons": {},
        "all_fallback": False,
    }


def test_a_genuinely_hung_store_still_costs_the_window_and_no_more() -> None:
    """The window is a CEILING, not a fee — the premise the 5.0s default rests on.

    If a wider window cost every auction its full length, raising it would be a real trade and
    the right answer would have been something else. It does not: ``parallel_fan_out`` returns
    the moment everyone has answered, so a fast store pays its own latency and only a store
    that is actually hung pays the ceiling.
    """
    mixed = PacedSolicitor({"store-quick": 80.0, "store-hung": 90.0}, delays={"store-hung": 5.0})
    roster = [rostered("store-quick", 120.0), rostered("store-hung", 130.0)]

    started = time.time()
    deadline = started + 0.3
    responses = parallel_fan_out(roster, mixed, deadline=deadline)
    elapsed = time.time() - started

    assert 0.25 < elapsed < 2.0, f"the window was not the bound on this call: {elapsed:.2f}s"
    entries = {e.store_id: e for e in collect_bids(roster, responses, deadline)}
    assert entries["store-quick"].fallback is False, "a fast store lost to a hung neighbour"
    assert entries["store-quick"].unit_price == 80.0
    assert entries["store-hung"].fallback_reason == RESPONSE_TIMED_OUT_REASON


# =====================================================================================
# 2 — the window: measured, configurable, and still bounded
# =====================================================================================
def test_the_default_window_sits_above_the_measured_hosted_latency() -> None:
    """5.0s, mirrored in both places, under an unchanged ceiling.

    The measurement is 1.97s–4.73s over 24 samples on 4 live agents. 3.0s sat under the FLOOR
    of that range, which is why every hosted store missed. The two constants are asserted
    equal because ``orchestration.solicit_bids`` reads the fan-out's copy for any caller that
    states no window, and a drift between them is a second default nobody would find.

    **A drift guard, and deliberately not the pin.** Restating a constant proves only that
    somebody restated it; what 5.0 has to be worth is asserted behaviourally, through the door,
    in :func:`test_the_default_window_carries_a_real_bid_on_a_served_auction`.
    """
    assert DEFAULT_BID_TIMEOUT_SECONDS == 5.0
    assert DEFAULT_BID_WINDOW_SECONDS == DEFAULT_BID_TIMEOUT_SECONDS
    assert 4.73 < DEFAULT_BID_TIMEOUT_SECONDS, "the default is under the measured p100 again"
    assert MAX_BID_TIMEOUT_SECONDS == 10.0, "the DoS ceiling moved; it must not"
    assert DEFAULT_BID_TIMEOUT_SECONDS < MAX_BID_TIMEOUT_SECONDS


def test_the_default_window_carries_a_real_bid_on_a_served_auction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The BEHAVIOURAL pin on 5.0, and the one the test above is not.

    Measured on this file before this test existed: reverting ``DEFAULT_BID_TIMEOUT_SECONDS``
    to 3.0 reddened exactly ONE test in ``apps/exchange`` — the restatement above, which is
    ``assert DEFAULT_BID_TIMEOUT_SECONDS == 5.0``. 1548 other tests stayed green, because not
    one of them ever ran an auction on the default: every served test in this repository
    either passes ``bid_timeout_seconds`` or sets ``EXCHANGE_BID_WINDOW_SECONDS``. A number
    pinned only by a copy of itself is pinned by nothing — the copy moves with it.

    So this one states NEITHER. It posts the body a buyer actually posts, with the environment
    a default ``docker compose up`` actually has, against a store that answers at 3.5s — a
    latency comfortably inside 5.0 and unconditionally outside 3.0. On 5.0 the store is a real
    sponsored entry at its own discounted price; on 3.0 the fan-out abandons it, the entry
    falls back to the 140.00 catalogue price, and the assertions below fail on the behaviour
    rather than on the constant.

    3.5s is a real sleep because the route takes no clock: ``create_auction`` reads
    ``time.time()``/``time.monotonic()`` directly. The margins are deliberately lopsided — 1.5s
    of slack before the reply could be late on a loaded machine, and none at all on the red
    side, since a 3.5s answer is past a 3.0s deadline however late the worker started.
    """
    monkeypatch.delenv(ENV_BID_WINDOW_SECONDS, raising=False)
    roster = [rostered("store-deliberate", 140.0)]
    solicitor = PacedSolicitor({"store-deliberate": 84.0}, delays={"store-deliberate": 3.5})

    _client, body = _auction(solicitor, roster)  # no bid_timeout_seconds: the DEFAULT window

    entry = body["entries"][0]
    assert entry["fallback"] is False, (
        "a store answering in 3.5s lost its bid, so the served default window is under 3.5s"
    )
    assert entry["fallback_reason"] is None
    assert entry["unit_price"] == 84.0, "the catalogue price was served instead of the bid"
    assert body["market"]["sponsored"] == 1
    assert body["market"]["timed_out"] == 0
    assert body["market"]["all_fallback"] is False
    assert body["market"]["bid_window_seconds"] == 5.0, (
        "the window a default deployment serves on is not the documented 5.0s"
    )


@pytest.mark.parametrize(
    ("stated", "expected"),
    [
        # TWO ROWS ARE PINNED TO THE LITERAL 5.0 ON PURPOSE. Every "→ the code default" row
        # below writes `DEFAULT_BID_TIMEOUT_SECONDS` as its own expected value, which is
        # correct for what those rows test — "this branch takes the code default, whatever it
        # is" — and useless as a guard on the number, because the table then FOLLOWS the
        # constant wherever it moves. Reverting the default to 3.0 left this whole table green.
        # So the two most-travelled branches, unset and malformed, state 5.0 outright.
        (None, 5.0),  # unset — the branch every default deployment takes
        ("5s", 5.0),  # malformed — the branch a typo takes
        ("", DEFAULT_BID_TIMEOUT_SECONDS),  # compose's own spelling of "use the default"
        ("   ", DEFAULT_BID_TIMEOUT_SECONDS),
        ("7.5", 7.5),
        ("2", 2.0),
        ("fast", DEFAULT_BID_TIMEOUT_SECONDS),
        ("-4", DEFAULT_BID_TIMEOUT_SECONDS),  # negative
        ("0", DEFAULT_BID_TIMEOUT_SECONDS),  # zero is nobody's intention
        ("nan", DEFAULT_BID_TIMEOUT_SECONDS),
        ("0.0001", 0.0001),  # under the floor: HONOURED, and warned about — see below
        ("inf", MAX_BID_TIMEOUT_SECONDS),  # clamped, never unbounded
        ("86400", MAX_BID_TIMEOUT_SECONDS),
    ],
)
def test_the_window_resolver_lands_where_the_design_says(
    stated: str | None, expected: float
) -> None:
    """Every branch of :func:`resolve_bid_window_seconds`, including the ones that must not raise.

    Read on a served request, so a configuration typo may never be an exception: an exchange
    that 500s every auction because someone wrote ``5s`` has turned a typo into an outage.
    """
    env = {} if stated is None else {ENV_BID_WINDOW_SECONDS: stated}
    assert resolve_bid_window_seconds(env) == expected


def test_an_operator_cannot_set_an_unbounded_window_either() -> None:
    """The ceiling binds the deployment, not only the caller.

    ``MAX_BID_TIMEOUT_SECONDS`` exists because the window is time a worker is parked on an
    UNAUTHENTICATED route. An environment variable that escaped it would move the same hole
    one file to the left.
    """
    assert resolve_bid_window_seconds({ENV_BID_WINDOW_SECONDS: "600"}) == MAX_BID_TIMEOUT_SECONDS
    assert resolve_bid_window_seconds({ENV_BID_WINDOW_SECONDS: "1e9"}) == MAX_BID_TIMEOUT_SECONDS


def test_a_malformed_window_warns_by_name_instead_of_failing_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A default taken because a setting was unreadable must SAY it was unreadable.

    Otherwise the operator's variable is simply ignored and the deployment behaves like one
    that was never configured — which is the shape of every defect in this file.
    """
    with caplog.at_level(logging.WARNING, logger="exchange.auction.routes"):
        resolved = resolve_bid_window_seconds({ENV_BID_WINDOW_SECONDS: "about-four-seconds"})

    assert resolved == DEFAULT_BID_TIMEOUT_SECONDS
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a window this exchange could not read was taken silently"
    rendered = warnings[-1].getMessage()
    assert ENV_BID_WINDOW_SECONDS in rendered
    assert "about-four-seconds" in rendered


def test_a_clamped_window_warns_too_rather_than_quietly_disagreeing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A setting the exchange declined to honour is not the same as one it honoured.

    An operator who sets 900 and gets 10 has a deployment that reads as configured and behaves
    as something else — which is precisely the class of silence this whole ticket is about, in
    a different file. So the clamp says so, and this is the branch that could most easily have
    been left quiet because it "still works".
    """
    with caplog.at_level(logging.WARNING, logger="exchange.auction.routes"):
        resolved = resolve_bid_window_seconds({ENV_BID_WINDOW_SECONDS: "901"})

    assert resolved == MAX_BID_TIMEOUT_SECONDS
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "901" in warnings[-1].getMessage()
    assert "ceiling" in warnings[-1].getMessage()


def test_a_window_this_exchange_can_honour_is_taken_without_complaint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The control for both warnings above.

    A warning that also fires on a correct setting trains an operator to filter the log, and
    then the malformed one is invisible for a new reason. Unset must be silent too: an empty
    value is compose's own way of saying "use the code default", and it is what this stack
    ships with.
    """
    with caplog.at_level(logging.WARNING, logger="exchange.auction.routes"):
        assert resolve_bid_window_seconds({ENV_BID_WINDOW_SECONDS: "6.5"}) == 6.5
        assert resolve_bid_window_seconds({ENV_BID_WINDOW_SECONDS: ""}) == (
            DEFAULT_BID_TIMEOUT_SECONDS
        )
        assert resolve_bid_window_seconds({}) == DEFAULT_BID_TIMEOUT_SECONDS

    assert not [r for r in caplog.records if r.levelno == logging.WARNING], (
        "a perfectly good bidding window was reported as a configuration problem"
    )


def test_a_window_too_small_for_any_store_is_run_and_still_announced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``0.0001`` and ``0`` produce the same market; only one of them used to say so.

    The resolver refused ``0`` with a WARNING because "nobody sets it on purpose", and took
    ``0.0001`` in silence — two spellings of the same all-fallback market, one loud and one
    mute. The floor closes that: a positive window under
    :data:`~exchange.auction.routes.MIN_USEFUL_BID_WINDOW_SECONDS` is a real setting, so it is
    HONOURED rather than replaced, and it is announced, because an operator who set it wants
    to know that no store can answer inside it.

    Both halves are asserted. Honouring without warning was the old silence; warning without
    honouring would quietly make this branch behave like the zero branch, which is the
    distinction the constant exists to draw.
    """
    with caplog.at_level(logging.WARNING, logger="exchange.auction.routes"):
        resolved = resolve_bid_window_seconds({ENV_BID_WINDOW_SECONDS: "0.0001"})

    assert resolved == 0.0001, "a tiny positive window was replaced instead of honoured"
    assert resolved < MIN_USEFUL_BID_WINDOW_SECONDS
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a window no store on earth can answer inside was accepted in silence"
    rendered = warnings[-1].getMessage()
    assert ENV_BID_WINDOW_SECONDS in rendered
    assert "0.0001" in rendered
    assert "as stated" in rendered, (
        "the line says the exchange ran something 'instead', but it ran what it was handed"
    )


def test_the_environment_moves_the_window_on_a_served_auction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The variable is read per REQUEST, which is what makes it configuration at all.

    A pydantic field default is bound once, at import. Had ``bid_timeout_seconds`` kept a
    literal default, this variable would only ever have applied to a process that imported the
    module after it was set — i.e. never, in a container whose environment is set by compose
    after the image is built and before anything imports anything.
    """
    monkeypatch.setenv(ENV_BID_WINDOW_SECONDS, "1.25")
    _client, body = _auction(PacedSolicitor({"store-a": 80.0}), [rostered("store-a", 120.0)])
    assert body["market"]["bid_window_seconds"] == 1.25

    monkeypatch.setenv(ENV_BID_WINDOW_SECONDS, "9999")
    _client, clamped = _auction(PacedSolicitor({"store-a": 80.0}), [rostered("store-a", 120.0)])
    assert clamped["market"]["bid_window_seconds"] == MAX_BID_TIMEOUT_SECONDS


def test_a_caller_stated_window_still_outranks_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bid_timeout_seconds`` behaves exactly as it did, clamp included."""
    monkeypatch.setenv(ENV_BID_WINDOW_SECONDS, "9.0")
    _client, body = _auction(
        PacedSolicitor({"store-a": 80.0}), [rostered("store-a", 120.0)], bid_timeout_seconds=0.5
    )
    assert body["market"]["bid_window_seconds"] == 0.5

    _client, capped = _auction(
        PacedSolicitor({"store-a": 80.0}),
        [rostered("store-a", 120.0)],
        bid_timeout_seconds=86_400.0,
    )
    assert capped["market"]["bid_window_seconds"] == MAX_BID_TIMEOUT_SECONDS


# =====================================================================================
# 3 — an all-fallback market is LOUD
#
# The constraint that matters most: today an all-fallback market is indistinguishable from a
# healthy one at a glance. Every container green, every log line 200 OK, a full shortlist —
# and not one store's own offer in it.
# =====================================================================================
def test_a_market_where_no_store_bid_is_announced_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    roster = [rostered("store-a", 120.0), rostered("store-b", 130.0)]
    solicitor = PacedSolicitor({}, silent=("store-a", "store-b"))

    with caplog.at_level(logging.INFO, logger="exchange.auction.routes"):
        _client, body = _auction(solicitor, roster, bid_timeout_seconds=1.0)

    assert body["market"]["all_fallback"] is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "the exchange served a market with no bids in it and said nothing"
    rendered = warnings[-1].getMessage()
    assert "LIST PRICES ONLY" in rendered
    assert "solicited=2" in rendered and "sponsored=0" in rendered
    assert "window=1.00s" in rendered, "the count is not actionable without the window"
    assert "no_response" in rendered, "the warning names no reason, so it names no fix"


def test_the_warning_does_not_fire_when_a_single_store_bid(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The control that keeps the warning worth reading.

    A warning that also fires on a healthy auction is a warning that gets filtered, and then
    the all-fallback market is invisible again for a new reason. One real bid out of two is a
    market that happened.
    """
    roster = [rostered("store-a", 120.0), rostered("store-b", 130.0)]
    solicitor = PacedSolicitor({"store-a": 80.0}, silent=("store-b",))

    with caplog.at_level(logging.INFO, logger="exchange.auction.routes"):
        _client, body = _auction(solicitor, roster, bid_timeout_seconds=1.0)

    assert body["market"]["sponsored"] == 1
    assert body["market"]["all_fallback"] is False
    assert not [r for r in caplog.records if r.levelno == logging.WARNING], (
        "an auction one store actually bid in was announced as a failed market"
    )
    informational = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and r.name == "exchange.auction.routes"
    ]
    assert informational, "a healthy auction told the operator nothing at all"
    assert "market=mixed" in informational[-1].getMessage()


def test_an_auction_that_solicited_nobody_is_not_called_a_failed_market(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unconfigured exchange denies every store (R12, fail closed) and serves no list prices.

    That is a different condition with a different fix, and firing the all-fallback alarm on it
    would put the warning on every request such a deployment refuses.
    """
    app = create_app()  # no eligibility wired: UNAVAILABLE for everyone
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="exchange.auction.routes"):
        body = client.post(
            "/auctions",
            json={
                "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
                "roster": [rostered("store-a", 120.0)],
                "bid_timeout_seconds": 0.5,
            },
        ).json()

    assert body["market"]["solicited"] == 0
    assert body["market"]["denied"] == 1
    assert body["market"]["all_fallback"] is False
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_the_summary_reaches_the_ledger_and_the_response_and_agrees_with_itself() -> None:
    """Three readers, one computation.

    The buyer's agent holds the ``201``, the operator reads the log, the auditor reads the
    chain — and a summary that disagreed across them would send someone to debug the
    arithmetic instead of the market. ``auction_closed`` carries it as an extra payload key,
    which is the seam ``auction_outcome`` already uses for seven other unpublished facts; the
    frozen ledger KINDS are untouched.
    """
    roster = [rostered("store-late", 130.0), rostered("store-quick", 120.0)]
    solicitor = PacedSolicitor(
        {"store-late": 90.0, "store-quick": 80.0}, delays={"store-late": 1.5}
    )
    client, body = _auction(solicitor, roster, bid_timeout_seconds=0.3)

    market = body["market"]
    assert market["solicited"] == 2
    assert market["sponsored"] == 1
    assert market["list_price"] == 1
    assert market["timed_out"] == 1
    assert market["all_fallback"] is False
    assert market["fallback_reasons"] == {RESPONSE_TIMED_OUT_REASON: 1}

    app = client.app
    closed = [
        event
        for event in app.state.auction_machine.ledger.sink.for_auction(body["auction_id"])
        if event["kind"] == "auction_closed"
    ]
    assert len(closed) == 1
    assert closed[0]["payload"]["market"] == market, (
        "the ledger and the 201 disagree about what this auction's market was"
    )
    # ...and the kind itself is untouched: D24's enum is frozen and this repair adds keys to a
    # payload, never a kind.
    assert closed[0]["kind"] == "auction_closed"
