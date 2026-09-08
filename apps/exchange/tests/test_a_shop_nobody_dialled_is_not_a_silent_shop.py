"""A shop the exchange never contacted is not a shop that stayed silent.

**The false statement, measured on the deployed droplet.** The ``201``'s ``solicited`` array
listed all six rostered stores — including ``bulksupplements.com`` and ``nutricost.com`` —
and ``market.not_asked`` was ``0``. But the shipped deployment document gives those two no
``bid_endpoint``, and ``composition.HttpBidSolicitor.solicit`` answers ``None`` for a store it
holds no endpoint for *without opening a socket*: "a Tier-0 store, or one the registry holds
no agent for, is represented at list price rather than asked a question nobody is home to
hear." They then reached the buyer carrying ``fallback_reason: no_response``, which the
shopper's own panel glosses as *"switched off or too slow to reach"*.

Nobody had spoken to them.

That is a false statement about an ORGANIC result, in the direction D55 cares most about: an
in-network shop buys the right to make its own case, and a scraped shop carried at its
catalogue price has made no case and declined nothing. Saying it went quiet when asked is the
platform putting words in its mouth — and the shop cannot answer back, because it does not
know the auction happened.

**Both directions, as always.** A store the exchange really did dial and which really did stay
silent must still read ``no_response``; a repair that reported every silence as "we never
called" would close one false statement by opening a worse one. Every test here that asserts
the new answer has a twin asserting the old one still holds.

The cause was one line: ``orchestration.solicitation``'s ``askable`` selected on TIER alone,
so a Tier-1 store with no endpoint was named in ``solicited`` and then, with no response object
to read, took ``collect_bids``' ``no_response`` default. Dropping it from ``askable`` alone
would have moved the false statement rather than ended it — the store still reaches
``collect_bids`` through ``eligible``, and still takes that default. So the gate also mints the
exchange's own record of the stores it did not dial, exactly as the fan-out mints one for the
stores it walked away from.
"""

from __future__ import annotations

from typing import Any

import pytest
from exchange.auction.collect import NO_AGENT_DETAIL, NO_AGENT_FIELD, collect_bids
from exchange.auction.fanout import parallel_fan_out
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.composition import HttpBidSolicitor
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.orchestration import solicit_bids
from exchange.orchestration.solicitation import stores_with_no_agent
from exchange.ranking.serving import configure_ranking
from fastapi.testclient import TestClient

T_NOW = 1_700_000_000.0

#: The shipped demo's own shape: some sellers hold an agent endpoint, some do not, and every
#: one of them is Tier-1. ``deploy/demo/exchange-deployment.json`` is four of ten.
WIRED = "gaiaherbs.com"
UNWIRED = "bulksupplements.com"


def rostered(store_id: str, list_price: float, tier: int = 1) -> dict[str, Any]:
    return {
        "store_id": store_id,
        "tier": tier,
        "product_ref": "product-1",
        "list_price": list_price,
    }


class Reachable:
    """A solicitor that holds an endpoint for some stores and answers for none of them.

    Deliberately silent even where it CAN reach: this file is about the difference between
    "not dialled" and "dialled and heard nothing", so the reachable store has to be one that
    genuinely went quiet. If it bid, the test would prove nothing about the silence.
    """

    def __init__(self, *, endpoints: tuple[str, ...]) -> None:
        self.endpoints = set(endpoints)
        self.asked: list[str] = []

    def can_solicit(self, store_id: Any) -> bool:
        return str(store_id) in self.endpoints

    def solicit(self, store: Any) -> None:
        self.asked.append(str(store["store_id"]))
        return None

    __call__ = solicit


class NoCapabilityQuery:
    """The port as every other solicitor in this repository implements it: ``solicit`` alone."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def solicit(self, store: Any) -> None:
        self.asked.append(str(store["store_id"]))
        return None

    __call__ = solicit


def _served_market(solicitor: Any, roster: list[dict[str, Any]]) -> dict[str, Any]:
    """One served ``POST /auctions`` with the ranking wired, so a shortlist really fills.

    ``configure_ranking`` matters here: ``market.all_fallback`` is a statement about the rows
    the SHOPPER saw, and an exchange with no trust snapshot and no registered domains excludes
    every candidate and shows nobody anything — which would make every assertion about that
    flag true for the wrong reason.
    """
    app = create_app()
    configure_auctions(
        app,
        solicitor=solicitor,
        eligibility=StaticSellerEligibility({row["store_id"]: ELIGIBLE for row in roster}),
    )
    configure_ranking(
        app,
        trust_snapshot={row["store_id"]: {"blacklisted": False, "score": 0.6} for row in roster},
        registered_domains=StaticRegisteredDomains(
            {row["store_id"]: f"{row['store_id']}.example.com" for row in roster}
        ),
    )
    posted = TestClient(app).post(
        "/auctions",
        json={
            "intent": {"intent_id": "i-1", "cluster_id": "c-1", "hard_constraints": []},
            "roster": roster,
            "bid_timeout_seconds": 1.0,
        },
    )
    assert posted.status_code == 201, posted.text
    return posted.json()


def _gate(solicitor: Any, roster: list[dict[str, Any]]) -> Any:
    return solicit_bids(
        roster=roster,
        solicitor=solicitor,
        eligibility=StaticSellerEligibility({row["store_id"]: ELIGIBLE for row in roster}),
        now=T_NOW + 1.0,
        fan_out=parallel_fan_out,
        window=1.0,
    )


# =====================================================================================
# 1 — the gate no longer claims to have asked a store it holds no way to reach
# =====================================================================================
def test_a_store_the_exchange_holds_no_endpoint_for_is_not_reported_as_solicited() -> None:
    """``solicited`` is the auction's claim about who it ASKED, and it must be able to stand.

    Measured before the repair, on this roster: ``solicited == ['gaiaherbs.com',
    'bulksupplements.com']`` while the solicitor's own ``asked`` log named only the first —
    two stores claimed, one socket opened.
    """
    solicitor = Reachable(endpoints=(WIRED,))
    result = _gate(solicitor, [rostered(WIRED, 120.0), rostered(UNWIRED, 130.0)])

    assert result.solicited == [WIRED], result.solicited
    assert solicitor.asked == [WIRED], (
        "the exchange dialled a store it had just said it holds no agent for"
    )


def test_that_store_is_still_represented_at_its_catalogue_price() -> None:
    """R10 is untouched. Not asking a shop is not dropping it — it is still on the shortlist."""
    result = _gate(
        Reachable(endpoints=(WIRED,)), [rostered(WIRED, 120.0), rostered(UNWIRED, 130.0)]
    )
    entries = {entry.store_id: entry for entry in result.entries}

    assert set(entries) == {WIRED, UNWIRED}
    assert entries[UNWIRED].fallback is True
    assert entries[UNWIRED].unit_price == 130.0, "the roster's own list price was not served"


def test_it_is_recorded_as_having_no_agent_rather_than_as_having_said_nothing() -> None:
    """The word on the entry, and the whole point of the repair.

    ``tier_0_no_agent`` is the FAMILY — the shopper-facing gloss for it says "that store has no
    bidding agent for the exchange to ask … without anybody having declined anything", which is
    exactly what happened — and the detail names which of the two conditions produced it, so an
    operator can tell a merchant's catalogue-only choice apart from this deployment's own
    silence about an endpoint.
    """
    result = _gate(
        Reachable(endpoints=(WIRED,)), [rostered(WIRED, 120.0), rostered(UNWIRED, 130.0)]
    )
    entries = {entry.store_id: entry for entry in result.entries}

    assert entries[UNWIRED].fallback_reason == f"tier_0_no_agent:{NO_AGENT_DETAIL}"
    assert "no_response" not in str(entries[UNWIRED].fallback_reason)


def test_a_store_that_was_really_dialled_and_really_said_nothing_is_still_no_response() -> None:
    """THE OTHER DIRECTION, and the one a careless repair breaks.

    ``gaiaherbs.com`` is reachable, was submitted to the fan-out, and answered nothing at all.
    That is a store that is switched off or too slow, it is a real operational fact about that
    store, and it must keep the word that says so.
    """
    result = _gate(
        Reachable(endpoints=(WIRED,)), [rostered(WIRED, 120.0), rostered(UNWIRED, 130.0)]
    )
    entries = {entry.store_id: entry for entry in result.entries}

    assert entries[WIRED].fallback_reason == "no_response"


def test_a_tier_0_store_keeps_the_bare_family_it_always_had() -> None:
    """The merchant's own catalogue-only choice is unchanged and still carries no detail."""
    result = _gate(
        Reachable(endpoints=(WIRED,)), [rostered(WIRED, 120.0), rostered("cat-only", 90.0, tier=0)]
    )
    entries = {entry.store_id: entry for entry in result.entries}

    assert entries["cat-only"].fallback_reason == "tier_0_no_agent"


# =====================================================================================
# 2 — the hook is optional, and absent means reachable
# =====================================================================================
def test_a_solicitor_that_answers_no_capability_question_behaves_exactly_as_before() -> None:
    """Every other solicitor in this tree implements ``solicit`` and nothing else.

    Fail-OPEN here on purpose, and it is not the usual direction. What this hook decides is how
    truthfully a run is REPORTED, not who may trade; the closed answer — "a solicitor that
    cannot say, asks nobody" — would empty the market on every deployment whose solicitor
    predates the hook, which is a far worse fault than the one being fixed.
    """
    solicitor = NoCapabilityQuery()
    result = _gate(solicitor, [rostered(WIRED, 120.0), rostered(UNWIRED, 130.0)])

    assert result.solicited == [WIRED, UNWIRED]
    assert solicitor.asked == [WIRED, UNWIRED]
    assert {e.store_id: e.fallback_reason for e in result.entries} == {
        WIRED: "no_response",
        UNWIRED: "no_response",
    }


def test_a_capability_hook_that_raises_is_treated_as_one_that_is_not_there() -> None:
    """A broken predicate must not take an auction down; it must fail to inform, not to trade."""

    class Exploding(NoCapabilityQuery):
        def can_solicit(self, store_id: Any) -> bool:
            raise RuntimeError("the registry is on fire")

    solicitor = Exploding()
    result = _gate(solicitor, [rostered(WIRED, 120.0)])
    assert result.solicited == [WIRED]
    assert solicitor.asked == [WIRED]


# =====================================================================================
# 3 — the marker is the exchange's own word, and a store may not write it
# =====================================================================================
def test_a_store_cannot_excuse_its_own_silence_with_the_exchanges_deployment() -> None:
    """``NO_AGENT_FIELD`` is stripped from anything a store sent, like the two markers beside it.

    A store that could set it would be relabelling "I did not answer" as "you never called me"
    — the exact inversion this file exists to prevent, pointed the other way.
    """

    class Liar:
        def can_solicit(self, store_id: Any) -> bool:
            return True

        def solicit(self, store: Any) -> dict[str, Any]:
            return {"store_id": store["store_id"], NO_AGENT_FIELD: True, "bid": {}}

        __call__ = solicit

    result = _gate(Liar(), [rostered(WIRED, 120.0)])
    entries = {entry.store_id: entry for entry in result.entries}
    assert entries[WIRED].fallback_reason != f"tier_0_no_agent:{NO_AGENT_DETAIL}", (
        "a store minted the exchange's own verdict about the exchange's own deployment"
    )


def test_the_collector_alone_cannot_tell_the_two_apart_and_does_not_try() -> None:
    """``collect_bids`` is a pure function with no port, so the marker has to arrive as data.

    Written down because it is the reason the repair is split across two modules rather than
    being one condition in the collector: the collector is handed a roster and a pile of
    responses, and a store the exchange never dialled looks identical to one that ignored it.
    Only the gate holds the solicitor.
    """
    roster = [rostered(UNWIRED, 130.0)]
    silent = collect_bids(roster, [], T_NOW)
    minted = collect_bids(roster, [{"store_id": UNWIRED, NO_AGENT_FIELD: True}], T_NOW)

    assert silent[0].fallback_reason == "no_response"
    assert minted[0].fallback_reason == f"tier_0_no_agent:{NO_AGENT_DETAIL}"


# =====================================================================================
# 4 — the producer: the solicitor a real deployment actually runs
# =====================================================================================
def test_the_deployed_solicitor_answers_the_question_the_gate_asks() -> None:
    """The hook has a PRODUCER, and it is the one the docker stack wires.

    A capability nothing implements is a knob switched off in every deployment, which is this
    repository's second-largest defect class. ``HttpBidSolicitor`` is what
    ``exchange.composition`` builds from ``deployment.bid_endpoints``, and the mapping it is
    built from is exactly the partial one the demo document produces.
    """
    solicitor = HttpBidSolicitor({WIRED: "http://store-agent-gaiaherbs:8086/v1/bid-requests"})

    assert solicitor.can_solicit(WIRED) is True
    assert solicitor.can_solicit(UNWIRED) is False
    assert solicitor.can_solicit(None) is False
    # And the binding a served auction actually uses keeps the answer, because `for_auction`
    # hands the same endpoint mapping to the view it returns.
    bound = solicitor.for_auction(auction_id="a", intent={}, profile={}, respond_by=T_NOW)
    assert bound.can_solicit(WIRED) is True
    assert bound.can_solicit(UNWIRED) is False


def test_the_predicate_and_the_call_agree_about_which_stores_are_reachable() -> None:
    """Two answers to "can I reach this store" would put the defect back, wearing a fresh coat."""
    solicitor = HttpBidSolicitor({WIRED: "http://store-agent-gaiaherbs:8086/v1/bid-requests"})
    for store_id in (WIRED, UNWIRED, "", "unknown.example.com"):
        row = {"store_id": store_id, "tier": 1, "list_price": 10.0}
        # `solicit` answers `None` without opening a socket for exactly the stores the
        # predicate refuses. For the reachable one it would open a socket, so it is not called
        # here — `can_solicit` returning True is the assertion, and the served-route test in
        # `test_composition_root.py` is what drives the socket half.
        if not solicitor.can_solicit(store_id):
            assert solicitor.solicit(row) is None


def test_the_gate_reads_the_predicate_off_whatever_solicitor_it_was_handed() -> None:
    """The helper in isolation, so its contract is pinned apart from the gate that calls it."""
    stores = [rostered(WIRED, 1.0), rostered(UNWIRED, 1.0), {"tier": 1, "list_price": 1.0}]
    assert stores_with_no_agent(Reachable(endpoints=(WIRED,)), stores) == {UNWIRED}
    assert stores_with_no_agent(NoCapabilityQuery(), stores) == set()


# =====================================================================================
# 5 — through the served door
# =====================================================================================
@pytest.mark.parametrize("shape", ["served"])
def test_the_201_never_names_a_store_the_exchange_did_not_dial(shape: str) -> None:
    """``POST /auctions``, because that is the body the droplet's shopper was served.

    The roster is the demo's own: Tier-1 stores, some the deployment holds an agent for and
    some it does not. The 201's ``solicited`` and its ``market`` counts have to agree with
    what happened on the wire, and the entry for the undialled store has to say why it was
    never asked rather than that it went quiet.
    """
    roster = [rostered(WIRED, 120.0), rostered(UNWIRED, 130.0)]
    app = create_app()
    configure_auctions(
        app,
        solicitor=Reachable(endpoints=(WIRED,)),
        eligibility=StaticSellerEligibility({row["store_id"]: ELIGIBLE for row in roster}),
    )
    posted = TestClient(app).post(
        "/auctions",
        json={
            "intent": {"intent_id": "i-1", "cluster_id": "c-1", "hard_constraints": []},
            "roster": roster,
            "bid_timeout_seconds": 1.0,
        },
    )
    assert posted.status_code == 201, posted.text
    body = posted.json()

    assert body["solicited"] == [WIRED], body["solicited"]
    assert body["market"]["solicited"] == 1, body["market"]

    reasons = {entry["store_id"]: entry["fallback_reason"] for entry in body["entries"]}
    assert reasons[UNWIRED] == f"tier_0_no_agent:{NO_AGENT_DETAIL}", reasons
    assert reasons[WIRED] == "no_response", reasons
    assert body["market"]["fallback_reasons"] == {"tier_0_no_agent": 1, "no_response": 1}, (
        "the market summary counted the two conditions as one"
    )


def test_dropping_a_store_from_solicited_does_not_switch_the_market_alarm_off() -> None:
    """THE REGRESSION THIS REPAIR ALMOST SHIPPED, pinned so it cannot come back.

    ``all_fallback`` was guarded on ``solicited > 0``, and this change stops counting stores the
    exchange holds no endpoint for as solicited. Combine the two and a roster of nothing but
    unwired Tier-1 sellers — which is a majority of the shipped demo document — shows the
    shopper two list-price rows, no store's own offer, and reports ``all_fallback: false`` with
    an INFO ``market=mixed`` line. Measured live on ``http://127.0.0.1:8083`` before the second
    guard existed::

        solicited: []
        market: {"solicited": 0, "sponsored": 0, "list_price": 2,
                 "shortlisted": 2, "shortlisted_sponsored": 0, "all_fallback": false}

    At HEAD that same auction warned, because the stores were falsely counted as solicited —
    so the first draft closed a false statement by silencing a true alarm. An adversarial
    review that had not written the change found it.

    The flag now asks whether ANYBODY COULD HAVE BID, and a store this deployment holds no
    endpoint for could have: it bids elsewhere, and the missing line is in a file an operator
    owns. What still does not warn is the all-Tier-0 roster below.
    """
    roster = [rostered(UNWIRED, 130.0), rostered("nutricost.com", 90.0)]
    body = _served_market(Reachable(endpoints=()), roster)

    assert body["market"]["solicited"] == 0, body["market"]
    assert body["market"]["no_endpoint"] == 2, body["market"]
    assert body["market"]["shortlisted"] == 2, body["market"]
    assert body["market"]["shortlisted_sponsored"] == 0, body["market"]
    assert body["market"]["all_fallback"] is True, (
        "a market that showed the shopper nothing but catalogue prices was reported as healthy"
    )


def test_a_roster_of_catalogue_only_merchants_is_not_a_failed_market() -> None:
    """The other direction, and the reason the guard is not simply ``list_price > 0``.

    Tier-0 is the MERCHANT's own choice to be catalogue-only. D55 makes an all-organic
    shortlist a legitimate result rather than a degraded one, and an alarm that fired here
    would fire on every organic auction the platform runs — which is how a warning becomes a
    warning people filter. Nothing is broken and nobody has anything to fix.
    """
    roster = [rostered("cat-a", 130.0, tier=0), rostered("cat-b", 90.0, tier=0)]
    body = _served_market(Reachable(endpoints=()), roster)

    assert body["market"]["solicited"] == 0, body["market"]
    assert body["market"]["no_endpoint"] == 0, body["market"]
    assert body["market"]["shortlisted"] == 2, body["market"]
    assert body["market"]["all_fallback"] is False, body["market"]


def test_the_two_catalogue_only_conditions_are_counted_apart() -> None:
    """``fallback_reasons`` groups by FAMILY, so the histogram alone cannot answer the question.

    Six ``tier_0_no_agent`` could be six merchants who chose catalogue-only, or six stores this
    deployment forgot to give a ``bid_endpoint``. Those have completely different fixes —
    nothing, and an edit to the deployment document — so ``no_endpoint`` is counted on the
    whole reason string beside the family histogram.
    """
    roster = [rostered(UNWIRED, 130.0), rostered("cat-only", 90.0, tier=0)]
    market = _served_market(Reachable(endpoints=()), roster)["market"]

    assert market["fallback_reasons"] == {"tier_0_no_agent": 2}, market
    assert market["no_endpoint"] == 1, (
        "the deployment's own gap is indistinguishable from a merchant's choice"
    )
