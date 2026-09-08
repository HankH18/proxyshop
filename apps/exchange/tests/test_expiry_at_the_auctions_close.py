"""An offer that stood for the whole auction is live for that auction's shortlist.

**The defect, measured on the deployed droplet across 12 runs.** A hosted store agent stamps
its offer's ``expires_at`` with the auction's own ``respond_by`` — that is what
``store_agent.runtime.context.AuctionContext.offer_expires_at`` falls back to when the
merchant's context states no expiry of its own, and no store context in ``deploy/demo``
states one. The exchange then judged liveness with a clock read taken AFTER the fan-out
returned, against ``expires_at <= now`` in :func:`exchange.ranking.filters.expiry_reason`.
Whenever the fan-out overran its window by even milliseconds, EVERY sponsored bid was
excluded. Verbatim from the droplet::

    expired_offer: the offer expired at 1788872951.113, which is not after the
    caller-supplied now=1788872951.1345184

That is 21 ms, and the shopper was served a shortlist of nothing but the exchange's own
manufactured list-price rows — which survive because ``auction.collect.fallback_expires_at``
gives them ``deadline + 900 s``. A demo of the timeout rather than of the market.

**Why it needs its own file rather than a row in an existing one.** The race is
timing-dependent, so an auction that merely runs proves nothing: on a fast machine the
fan-out returns before the deadline and the bug is invisible, which is exactly why 120
acceptance tests never saw it. Every test here constructs the overrun DELIBERATELY — one
through the served door with a real slow solicitor, the rest by driving the ranker with an
explicit ``now`` past the deadline — and every one of them pins BOTH directions. A fix that
admits an offer which genuinely died mid-auction has not fixed a false refusal, it has
opened a hole, and this repo's fourth dominant defect class is a refusal firing on honest
traffic.

The honest-traffic case is named in the tests below as a *well-behaved third-party store*:
one that answers inside the window and stamps its offer at exactly the deadline it was asked
to answer by. That store did nothing wrong, and no amount of exchange-side latency may cost
it the shortlist.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from exchange.auction.collect import BidEntry
from exchange.auction.routes import (
    configure_auctions,
    publishable_deadline,
    rfc3339_deadline,
)
from exchange.checkout.codes import expiry_epoch
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking import rank
from exchange.ranking.filters import expiry_reason
from exchange.ranking.serving import configure_ranking, rank_auction
from fastapi.testclient import TestClient

#: The auction in the unit tests below: opened at ``T_OPEN``, a one-second window, and a
#: fan-out that hands control back 21 ms late — the droplet's own overrun.
T_OPEN = 1_700_000_000.0
WINDOW = 1.0
DEADLINE = T_OPEN + WINDOW
OVERRAN_BY = 0.021
CLOSED_AT = DEADLINE + OVERRAN_BY

HONEST_STORE = "honest-store"
HONEST_DOMAIN = f"{HONEST_STORE}.example.com"


def _offer(expires_at: Any, *, price: float = 100.0) -> dict[str, Any]:
    return {
        "product_ref": "product-1",
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "checkout_url": f"https://{HONEST_DOMAIN}/cart/1:1",
        "expires_at": expires_at,
    }


def _candidate(expires_at: Any) -> dict[str, Any]:
    return {
        "bid_id": "bid-1",
        "store_id": HONEST_STORE,
        "store_domain": HONEST_DOMAIN,
        "offer": _offer(expires_at),
        "claims": [],
    }


def _entry(expires_at: Any) -> BidEntry:
    """One collected bid, in the shape ``rank_auction`` is actually handed on the served path."""
    return BidEntry(
        store_id=HONEST_STORE,
        tier=1,
        fallback=False,
        bid={
            "auction_id": "auction-1",
            "store_id": HONEST_STORE,
            "offer": _offer(expires_at, price=80.0),
            "claims": [],
        },
        received_at=T_OPEN + 0.1,
        list_price=100.0,
    )


def _ranked_auction(entry: BidEntry, *, now: float, deadline: float | None) -> dict[str, Any]:
    return rank_auction(
        [entry],
        auction_id="auction-1",
        intent=INTENT,
        now=now,
        deadline=deadline,
        trust_snapshot=SNAPSHOT,
        registered_domains=StaticRegisteredDomains({HONEST_STORE: HONEST_DOMAIN}),
    )


SNAPSHOT = {HONEST_STORE: {"blacklisted": False, "score": 0.6}}
INTENT = {"intent_id": "intent-1", "cluster_id": "cluster-1", "hard_constraints": []}


# =====================================================================================
# 1 — the filter itself, at the boundary nothing used to pin
# =====================================================================================
def test_an_offer_that_stood_until_the_judging_instant_is_live_at_it() -> None:
    """``expires_at == now`` is the whole race, and no test in this tree pinned it.

    Measured before the repair: ``expiry_reason(_offer(DEADLINE), DEADLINE)`` returned
    ``"expired_offer: the offer expired at 1700000001.0, which is not after the
    caller-supplied now=1700000001.0"``. Every fixture in this repository used a strictly
    past or strictly future instant, so the one comparison the served path actually lands on
    was the one nothing graded.
    """
    assert expiry_reason(_offer(DEADLINE), DEADLINE) is None


def test_an_offer_that_died_before_the_judging_instant_is_still_refused() -> None:
    """The other direction, and it is the one that must not be widened by the repair."""
    reason = expiry_reason(_offer(DEADLINE - 0.001), DEADLINE)
    assert reason is not None and "expired_offer" in reason


@pytest.mark.parametrize("spelling", ["missing", "null"])
def test_an_offer_with_no_expiry_at_all_still_fails_closed(spelling: str) -> None:
    """Unchanged, and deliberately so.

    ``exchange.checkout.provider`` does NOT refuse a missing expiry at accept time —
    ``code_expiry`` hands back the flat 48-hour ceiling and the window reads as open — so
    this filter is the only thing in the system that requires an offer to say when it stops
    standing. Relaxing the boundary comparison must not relax presence with it.
    """
    offer = _offer(DEADLINE)
    if spelling == "missing":
        del offer["expires_at"]
    else:
        offer["expires_at"] = None
    reason = expiry_reason(offer, DEADLINE)
    assert reason is not None and "expired_offer" in reason


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_expiry_is_still_not_an_instant(bad: float) -> None:
    """Also unchanged. A NaN expiry mints a live discount code at accept time.

    Measured on the served accept door: an offer with ``expires_at`` NaN answers ``200``
    with a usable ``PSX-`` code, because ``min(ceiling, nan)`` is the ceiling and the
    accept-time window check abstains on an unreadable number. The ranker's finiteness test
    is the only gate that stops it, so it stays exactly as strict as it was.
    """
    reason = expiry_reason(_offer(bad), DEADLINE)
    assert reason is not None and "expired_offer" in reason


# =====================================================================================
# 1b — the deadline the exchange states is the deadline it judges against
# =====================================================================================
@pytest.mark.parametrize(
    "moment",
    [
        1_788_876_293.595_267_8,  # the live reproduction's own deadline
        1_700_000_000.000_000_9,
        # THE ROUND-UP CLASS, and the reason it is spelled with this many digits.
        # `datetime.fromtimestamp` rounds to the nearest MICROsecond before `timespec` truncates
        # to milliseconds, so a moment just under a millisecond boundary renders as the boundary
        # — later than the deadline it came from. `1_700_000_000.999_999_9` does NOT test this:
        # it is not representable and collapses to the double `1700000001.0`, an exact second,
        # which is the easy case. This value is 477 ns below the boundary and survives as
        # itself.
        1_700_000_000.999_999_5,
        1_700_000_000.999_999_0,
        1_700_000_000.0,
    ],
)
def test_a_published_deadline_survives_the_wire_it_is_published_on(moment: float) -> None:
    """``respond_by`` goes out as milliseconds, so the exchange must judge at milliseconds.

    The half of the race that a deadline CAP alone does not close, and it is only visible
    against a real agent. ``BidRequest.respond_by`` is a ``format: date-time`` string rendered
    with ``timespec="milliseconds"``; a hosted agent stamps its offer's ``expires_at`` with the
    string it was handed; the exchange parses that back and compares it against the
    full-precision double it minted. Measured on the live stack with one of four agents paused
    so the fan-out ran the whole window::

        expired_offer: the offer expired at 1788876293.595, which is before the instant it
        had to be standing at, now=1788876293.5952678

    268 microseconds. Every store that answered in time was excluded, and the shortlist held
    one row: the exchange's own stand-in for the paused agent.

    Both directions are here in one assertion. ``publishable_deadline`` must be an instant the
    wire can carry EXACTLY — so a store's echo of it lands on the same number — and it must
    still be a deadline, never later than the moment it was derived from.
    """
    published = publishable_deadline(moment)
    assert expiry_epoch(rfc3339_deadline(published)) == published, (
        "the exchange minted a deadline its own wire format cannot state"
    )
    assert published <= moment, "truncation moved the deadline LATER than the window allows"
    assert moment - published < 0.001, "a millisecond format lost more than a millisecond"


def test_the_route_mints_a_deadline_a_store_can_echo_back() -> None:
    """The property above, through the door, against the renderer the deployment really uses.

    ``exchange.composition``'s outbound solicitor renders ``respond_by`` with this exact
    function — it holds no copy of its own any more — so a store's ``expires_at`` is this
    string, and this is what the ranker parses. Driving it here is what keeps the two from
    drifting apart the next time somebody changes a ``timespec``.
    """
    from exchange.composition import _rfc3339

    minted = publishable_deadline(time.time() + 5.0)
    assert _rfc3339(minted) == rfc3339_deadline(minted)
    assert expiry_epoch(_rfc3339(minted)) == minted


# =====================================================================================
# 2 — the ranker, handed an auction whose fan-out overran
# =====================================================================================
def test_a_fan_out_overrun_no_longer_voids_an_offer_that_stood_for_the_whole_auction() -> None:
    """``rank_auction`` is told when the auction was DUE to close, not only when it did.

    This is the repair stated as an assertion: the auction published ``respond_by =
    DEADLINE``, the store's offer stood until exactly that instant, the exchange's own
    fan-out handed back 21 ms late, and the shortlist keeps the bid.
    """
    ranking = _ranked_auction(_entry(DEADLINE), now=CLOSED_AT, deadline=DEADLINE)
    excluded = {
        row["store_id"]: row["exclusion_reasons"]
        for row in ranking["candidates"]
        if not row["eligible"]
    }
    assert excluded == {}, excluded
    assert [row["store_id"] for row in ranking["ranked"]] == [HONEST_STORE]


def test_an_offer_that_expired_inside_the_window_is_still_excluded_by_name() -> None:
    """The overrun buys an offer nothing; what it buys is the deadline as a ceiling.

    A store that stamped its offer to die halfway through the bidding window made an offer
    that had already lapsed when the auction closed, and the deadline cap does not rescue
    it. Without this row the repair would read as "expiry is no longer checked".
    """
    ranking = _ranked_auction(_entry(T_OPEN + WINDOW / 2), now=CLOSED_AT, deadline=DEADLINE)
    reasons = [
        reason
        for row in ranking["candidates"]
        for reason in row["exclusion_reasons"]
        if "expired_offer" in reason
    ]
    assert reasons, ranking["candidates"]
    assert ranking["ranked"] == []


def test_the_cap_never_admits_an_offer_a_shorter_run_would_have_refused() -> None:
    """A fan-out that finished EARLY keeps judging at the instant it actually closed.

    The cap is ``min(closed_at, deadline)`` and not ``deadline`` alone, so it can only ever
    move the judging instant earlier than the wall clock — it never extends an auction's
    reach past the moment it really ended. An offer that lapsed before an early close is
    excluded here exactly as it was before the repair.
    """
    ranking = _ranked_auction(_entry(T_OPEN + 0.25), now=T_OPEN + 0.5, deadline=DEADLINE)
    assert ranking["ranked"] == []


@pytest.mark.parametrize(
    "bad",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        # FINITE AND ABSURD, which the first version of this guard let through. `min(now, 0.0)`
        # is `0.0`, judging liveness at the epoch, and then every readable expiry passes with
        # no exclusion reason at all — the gate is not weakened there, it is gone. An
        # adversarial review found this by trying the two values a finiteness test cannot see.
        0.0,
        -1.0,
        -1_700_000_000.0,
    ],
)
def test_a_deadline_that_is_not_an_instant_is_ignored_rather_than_believed(bad: float) -> None:
    """A cap that is not a positive finite instant is not a cap, and must not become the clock.

    ``nan`` is refused explicitly rather than left to ``min``: ``min(now, nan)`` answers ``now``
    while ``min(nan, now)`` answers ``nan``, so safety would otherwise be an accident of
    argument order — and a ``nan`` judging instant makes EVERY expiry comparison False, which
    reads as "nothing has expired". ``0.0`` and the negatives are the other direction: believed,
    they judge every offer at (or before) the epoch and admit a corpse.

    Every row is asserted through an offer that must stay REFUSED, so this is a hole test and
    not a smoke test. The offer here died halfway through the bidding window and no reading of
    this auction makes it live.
    """
    ranking = _ranked_auction(_entry(T_OPEN + WINDOW / 2), now=CLOSED_AT, deadline=bad)
    assert ranking["ranked"] == [], f"a deadline that is not an instant was believed: {bad!r}"


def test_a_readable_but_wrong_deadline_is_trusted_exactly_as_a_wrong_now_is() -> None:
    """The limit of the guard, stated rather than left for someone to discover.

    A deadline of ``1.0`` — one second after the epoch — is a positive finite instant, so it is
    believed, and every expiry then reads as live. That is not a hole this function can close:
    the parameter says WHICH AUCTION is being ranked, a caller that lies about it is ranking a
    different auction, and ``now`` has always carried exactly the same trust. Pinned so the
    guard is not mistaken for a validator it is not.
    """
    ranking = _ranked_auction(_entry(T_OPEN + WINDOW / 2), now=CLOSED_AT, deadline=1.0)
    assert [row["store_id"] for row in ranking["ranked"]] == [HONEST_STORE]


def test_a_caller_that_states_no_deadline_is_answered_exactly_as_before() -> None:
    """``deadline`` is optional, and omitting it is the published four-argument behaviour."""
    config = {"now": CLOSED_AT, "auction_id": "auction-1"}
    assert rank([_candidate(DEADLINE)], INTENT, SNAPSHOT, config)["ranked"] == []


# =====================================================================================
# 3 — the served door, with a real overrun
# =====================================================================================
class HonestThirdPartyStore:
    """A store that answers inside the window and stamps its offer at the deadline it was given.

    Two things make this the double that matters. It implements ``for_auction``, which is how
    the real outbound client learns the auction's ``respond_by`` — so the expiry it stamps is
    the exchange's own number rather than a constant a test chose. And one of its stores is
    slow enough to hold the fan-out open past that deadline, which is the only way to make
    ``closed_at > respond_by`` over a door that reads ``time.time()`` itself.
    """

    def __init__(self, *, prompt: str, slow: str, delay: float) -> None:
        self.prompt = prompt
        self.slow = slow
        self.delay = delay
        self.respond_by: float | None = None
        self.asked: list[str] = []
        self._lock = threading.Lock()

    def for_auction(self, **context: Any) -> HonestThirdPartyStore:
        self.respond_by = float(context["respond_by"])
        return self

    def solicit(self, store: Any) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        with self._lock:
            self.asked.append(store_id)
        if store_id == self.slow:
            time.sleep(self.delay)
            return None
        return {
            "store_id": store_id,
            "received_at": time.time(),
            "bid": {
                "auction_id": "auction-1",
                "store_id": store_id,
                "offer": {
                    "product_ref": "product-1",
                    "unit_price": 80.0,
                    "total_price": 80.0,
                    "currency": "USD",
                    "checkout_url": f"https://{store_id}.example.com/cart/1:1",
                    # THE STAMP THIS FILE IS ABOUT. The agent was authorized to promise
                    # nothing beyond the window it was asked to answer in, so it says so.
                    "expires_at": self.respond_by,
                },
                "claims": [],
                "message": "We will beat any price you find today.",
            },
        }

    __call__ = solicit


def _served(solicitor: Any, stores: list[str], *, window: float) -> dict[str, Any]:
    app = create_app()
    configure_auctions(
        app,
        solicitor=solicitor,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={store: {"blacklisted": False, "score": 0.6} for store in stores},
        registered_domains=StaticRegisteredDomains(
            {store: f"{store}.example.com" for store in stores}
        ),
    )
    posted = TestClient(app).post(
        "/auctions",
        json={
            "intent": INTENT,
            "roster": [
                {"store_id": store, "tier": 1, "product_ref": "product-1", "list_price": 100.0}
                for store in stores
            ],
            "bid_timeout_seconds": window,
        },
    )
    assert posted.status_code == 201, posted.text
    return posted.json()


def test_a_well_behaved_store_reaches_the_shopper_even_when_the_fan_out_runs_long() -> None:
    """The reproduction, through ``POST /auctions``, and the reason this file exists.

    ``prompt`` answers immediately with an offer stamped at ``respond_by``; ``slow`` holds a
    fan-out worker until well past the window, which forces ``closed_at > respond_by`` on
    every run rather than on the two in twelve the droplet happened to catch.

    Measured before the repair, on this machine, with this test::

        excluded: {'prompt-store': ['expired_offer: the offer expired at 1757…, which is
                   not after the caller-supplied now=1757…']}
        shortlist slots: 2, sponsored slots: 0

    After it, the same run shortlists the store's own 80.00 offer.
    """
    solicitor = HonestThirdPartyStore(prompt="prompt-store", slow="slow-store", delay=0.6)
    body = _served(solicitor, ["prompt-store", "slow-store"], window=0.2)

    excluded = {row["store_id"]: row["exclusion_reasons"] for row in body["excluded"]}
    expired = {
        store: reasons
        for store, reasons in excluded.items()
        if any("expired_offer" in reason for reason in reasons)
    }
    assert expired == {}, f"a store that answered in time was voided by the exchange: {expired}"

    slots = body["shortlist"]["slots"]
    sponsored = [slot for slot in slots if not slot.get("fallback")]
    # A slot names its store through `trust_summary.store_id` and `store_domain`; `ShortlistSlot`
    # is a pinned `additionalProperties: false` shape and carries no bare `store_id`.
    assert [slot["trust_summary"]["store_id"] for slot in sponsored] == ["prompt-store"], slots
    assert sponsored[0]["price"]["unit_price"] == 80.0, sponsored


def test_the_summary_cannot_say_a_market_happened_when_no_sponsored_row_was_shown() -> None:
    """``market`` carries who BID and who REACHED THE SHOPPER, and warns on the second.

    The diagnostic contradicted the screen: ``market_summary`` counts ``result.entries`` —
    who bid — and reported ``"sponsored": 4`` on droplet runs where the shopper saw zero
    sponsored rows and ``all_fallback`` stayed ``false``. Both facts are worth having, so
    both are published; ``all_fallback`` is the shopper's.
    """
    solicitor = HonestThirdPartyStore(prompt="prompt-store", slow="slow-store", delay=0.6)
    body = _served(solicitor, ["prompt-store", "slow-store"], window=0.2)
    market = body["market"]

    assert market["sponsored"] == 1, market
    assert market["shortlisted_sponsored"] == 1, market
    assert market["all_fallback"] is False, market
    assert market["shortlisted"] == len(body["shortlist"]["slots"]), market


def test_a_market_whose_bids_all_lost_to_the_ranking_is_reported_as_a_failed_market() -> None:
    """Every store bid and not one of their offers reached a slot: the summary must say BOTH.

    This is the droplet's screen reconstructed from a cause the expiry repair does not touch,
    so it keeps proving the diagnostic after the race is gone. Two stores bid with a checkout
    URL on somebody else's host, so both sponsored candidates are refused ``off_domain``; two
    say nothing, and the exchange's own list-price stand-ins for them — completed with the
    PLATFORM's registered domain before the filter ever sees them — are the only rows left to
    fill the slots. The shopper is served catalogue prices and no store's own offer.

    ``sponsored: 2`` and ``shortlisted_sponsored: 0`` are both true and they are different
    facts: the stores answered, and this exchange dropped every one of them. Reporting only
    the first is what let ``all_fallback`` stay ``false`` on a screen with no market on it.
    """
    bidders = ["a-store", "b-store"]
    silent = ["quiet-store", "hushed-store"]
    stores = bidders + silent

    class OffDomainBidder:
        """Answers in time, with a destination on a host that is not its registered domain."""

        def solicit(self, store: Any) -> dict[str, Any] | None:
            store_id = str(store["store_id"])
            if store_id in silent:
                return None
            return {
                "store_id": store_id,
                "received_at": time.time(),
                "bid": {
                    "auction_id": "auction-1",
                    "store_id": store_id,
                    "offer": {
                        "product_ref": "product-1",
                        "unit_price": 80.0,
                        "total_price": 80.0,
                        "currency": "USD",
                        "checkout_url": "https://somebody-elses-host.example.net/cart/1:1",
                        "expires_at": "2038-01-01T00:00:00Z",
                    },
                    "claims": [],
                },
            }

        __call__ = solicit

    body = _served(OffDomainBidder(), stores, window=1.0)
    market = body["market"]
    slots = body["shortlist"]["slots"]

    assert market["sponsored"] == len(bidders), market
    assert market["list_price"] == len(silent), market
    assert slots and all(slot["fallback"] for slot in slots), slots
    assert market["shortlisted"] == len(slots), market
    assert market["shortlisted_sponsored"] == 0, market
    assert market["all_fallback"] is True, (
        "every store bid and not one row the shopper saw was a store's own offer"
    )


def test_an_empty_shortlist_after_real_bids_is_the_loudest_line_on_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing reached the shopper at all, and it gets its own WARNING and its own word.

    This is the failure an adversarial review found still silent after the first draft of the
    summary repair: two stores bid, the ranker refused every candidate INCLUDING the exchange's
    own list-price stand-ins, the shopper got a blank screen — and the market line said
    ``market=mixed`` at INFO, because ``all_fallback`` is guarded on there being rows to call
    fallbacks. The guard is right; the silence was not.

    ``nothing_shown`` rather than ``all_fallback`` because the two have different fixes and a
    shared word would hide that: one says the market reverted to catalogue prices, the other
    says there were no catalogue prices either.
    """
    import logging

    stores = ["a-store", "b-store"]
    app = create_app()
    configure_auctions(
        app,
        solicitor=HonestThirdPartyStore(prompt="a-store", slow="__none__", delay=0.0),
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    # No registered domain for anybody, so even the exchange's own stand-ins are refused
    # ``off_domain`` and not one slot fills.
    configure_ranking(
        app,
        trust_snapshot={store: {"blacklisted": False, "score": 0.6} for store in stores},
        registered_domains=StaticRegisteredDomains({}),
    )
    with caplog.at_level(logging.INFO, logger="exchange.auction.routes"):
        posted = TestClient(app).post(
            "/auctions",
            json={
                "intent": INTENT,
                "roster": [
                    {"store_id": store, "tier": 1, "product_ref": "product-1", "list_price": 100.0}
                    for store in stores
                ],
                "bid_timeout_seconds": 1.0,
            },
        )
    assert posted.status_code == 201, posted.text
    market = posted.json()["market"]

    assert market["sponsored"] == len(stores), market
    assert market["shortlisted"] == 0, market
    assert market["all_fallback"] is False, "an empty shortlist is not a list-price market"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "the shopper got a blank screen and the operator was told nothing"
    said = warnings[-1].getMessage()
    assert "NOTHING REACHED THE SHOPPER" in said, said
    assert "market=nothing_shown" in said, said


def test_an_auction_with_nothing_to_represent_is_not_called_a_failed_market(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither warning fires on an exchange that refused everybody before anyone was asked.

    R12 fail-closed: an unwired exchange denies every store, so there are no entries, no slots
    and nobody solicited. Alarming there would alarm on every request such a deployment
    refuses, which is the one reliable way to make a warning worth ignoring.
    """
    import logging

    app = create_app()  # no eligibility wired: UNAVAILABLE for everyone
    with caplog.at_level(logging.INFO, logger="exchange.auction.routes"):
        posted = TestClient(app).post(
            "/auctions",
            json={
                "intent": INTENT,
                "roster": [
                    {
                        "store_id": "a-store",
                        "tier": 1,
                        "product_ref": "product-1",
                        "list_price": 1.0,
                    }
                ],
                "bid_timeout_seconds": 0.5,
            },
        )
    body = posted.json()
    assert body["market"]["denied"] == 1, body["market"]
    assert body["market"]["shortlisted"] == 0, body["market"]
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_an_empty_shortlist_is_not_called_an_all_fallback_market() -> None:
    """The other direction on the flag, and the reason it carries a second guard.

    "Every row was a fallback" is vacuously true of a shortlist with no rows, and an auction
    that showed the shopper nothing has a different cause and a different fix — here, an
    exchange holding no registered domain for anybody, so even its own stand-ins are refused.
    Firing the all-fallback alarm on that is the same mistake the ``solicited > 0`` guard
    exists to prevent. Measured before this guard was added: four served tests in
    ``test_bid_window_and_market_summary.py`` flipped to WARNING for exactly this reason.
    """
    stores = ["a-store"]
    app = create_app()
    configure_auctions(
        app,
        solicitor=HonestThirdPartyStore(prompt="a-store", slow="__none__", delay=0.0),
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={store: {"blacklisted": False, "score": 0.6} for store in stores},
        registered_domains=StaticRegisteredDomains({}),
    )
    posted = TestClient(app).post(
        "/auctions",
        json={
            "intent": INTENT,
            "roster": [
                {"store_id": store, "tier": 1, "product_ref": "product-1", "list_price": 100.0}
                for store in stores
            ],
            "bid_timeout_seconds": 1.0,
        },
    )
    assert posted.status_code == 201, posted.text
    market = posted.json()["market"]

    assert market["shortlisted"] == 0, market
    assert market["shortlisted_sponsored"] == 0, market
    assert market["all_fallback"] is False, market


def test_a_ranking_that_raises_still_tells_the_operator_what_the_bid_layer_did(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The market line survives the one path that has no shortlist to report.

    ``announce_market`` moved to AFTER the ranking so it can say what the shopper got, and the
    reason it used to sit before the ranking is still real: a ranking that raises is a 500 that
    leaves the auction OPEN, and it is exactly the run an operator goes to the log for. So the
    move is wrapped rather than plain, and this is the assertion that the wrapper works.

    ``shown_sponsored=?`` rather than ``0``: the shopper's half was never computed, and a zero
    there would be a measurement of something that did not happen.
    """
    import logging

    from exchange.auction import routes as auction_routes

    def explode(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("the ranker fell over")

    monkeypatch.setattr(auction_routes, "rank_auction", explode)

    stores = ["a-store"]
    app = create_app()
    configure_auctions(
        app,
        solicitor=HonestThirdPartyStore(prompt="a-store", slow="__none__", delay=0.0),
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="exchange.auction.routes"):
        posted = client.post(
            "/auctions",
            json={
                "intent": INTENT,
                "roster": [
                    {"store_id": store, "tier": 1, "product_ref": "product-1", "list_price": 100.0}
                    for store in stores
                ],
                "bid_timeout_seconds": 1.0,
            },
        )

    assert posted.status_code == 500, posted.status_code
    lines = [r.getMessage() for r in caplog.records if r.name == "exchange.auction.routes"]
    assert lines, "a ranking that raised told the operator nothing about the market"
    assert "sponsored=1" in lines[-1], lines
    assert "shown_sponsored=?" in lines[-1], lines

    # And the auction is left OPEN, which is the state the TTL collects — a CLOSED auction with
    # no stored shortlist would have nothing for the accept door to read.
    record = (
        app.state.auction_machine._records
        if hasattr(app.state.auction_machine, "_records")
        else None
    )
    if record is not None:
        assert all(getattr(r, "state", "") != "CLOSED" for r in record.values()), record
