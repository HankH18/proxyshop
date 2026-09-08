"""D58: the exchange NAMES the product it solicits, and still grades the product it rostered.

Ticket verify::

    PROXYSHOP_WORKER=<n> pytest apps/exchange/tests/test_counter_proposed_product.py -q

What was measured before this file existed
------------------------------------------
``BidRequest``'s published properties were exactly ``['auction_id', 'intent', 'profile',
'respond_by']``, all four required. **It named no product.** So the exchange rostered store X
for product P, solicited X without telling it P, X picked a product Q out of its own catalogue
(``store_agent.runtime.bidding``'s selection key is cheapest-first) and bid that — and the
exchange then graded X's claims about Q against the platform's snapshot of P.

Measured on the served ``POST /auctions``, this repo's own hosted agents on its own demo
market, one store carrying two products and rostered for the dearer one::

    offer.product_ref = beanie-merino-lite   claims list_price 39.00, units_left 12   (all TRUE)
    roster product_ref = beanie-merino-01    the platform lists it at 78.00

    components: {..., "policy_penalties": -0.15, "price_value": 0.15, ...}

Both numbers are wrong and in opposite directions. The penalty is a ``contradicted_claim``
against a store whose every statement was true. The ``price_value`` is a full 0.15, the most
that term can pay, credited for a discount nobody gave — ``(78 − 39)/78``, a ratio between two
different products' prices. On that request they cancelled exactly, so ``rank_score`` matched
the correct one and neither error was visible in the ranking.

**A false contradiction is the worst verdict this system can reach.** D55 makes adversarial
verification of the seller's own message the thing that justifies the sponsored half of the
market; a verification that fires on an honest store is that mechanism turned against the
party it exists to protect.

Why the price wall did not already catch it
-------------------------------------------
``auction/collect.py``'s ``_price_refusal`` was already keyed by the ROSTER's ``product_ref``
and looked the row up by the OFFER's, precisely so "a store answering about a product it was
not asked about finds no row". That wall never ran, because ``_is_judged`` gated it and none of
its five clauses was about the product. Measured, same store, same bid, same price, differing
only in whether the roster row happened to state a ``max_discount_pct``::

    roster max_discount_pct: 20  -> refused, bid_price_unreconcilable, represented at 78.00
    roster states none           -> ADMITTED at 39.00, penalised -0.15, credited 0.15

and :meth:`exchange.retrieval.roster.SolicitedShop.as_roster_row` — the graph-backed roster,
D55's organic half — states no ``max_discount_pct`` at all, so the second row is the one the
product actually served.

What shipped, and the half that was built and WITHDRAWN
-------------------------------------------------------
Shipped: ``BidRequest`` names the product (D58 half (a)), the agent bids it when it can, and a
bid about any other product is uniformly REFUSED and represented at the rostered list price.

Withdrawn: grading and pricing the product the OFFER names. It was implemented, driven through
this route, and taken back out on two measurements — both asserted in section 2 below, because
a hole that was closed by deletion is one the next reader will reopen unless the test says why.
On the hosted path an offer is arbitrary third-party JSON and ``product_ref``, ``variant_ref``
and ``checkout_url`` are three independent store-written strings nothing joins, so letting the
bid name the graded subject let a liar launder a false claim onto a crawled sibling while still
selling the original variant, and let a bidder pick its own ``price_value`` denominator.

What this file asserts, and in which direction
-----------------------------------------------
This repo has a documented blind spot exactly here: its gates check that a refusal fires on the
attack and never that it stays silent on honest traffic. So the served assertions are made in
BOTH directions on the SAME request — honest stores graded ``verified`` with zero
``contradicted_claim`` events and no ``policy_penalties``, and a genuinely dishonest one still
graded ``contradicted`` and still penalised. If the liar's penalty ever disappears, the fix has
stopped being a correction and this file goes red.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest
from exchange.auction.collect import UNRECONCILABLE_PRICE_REASON, collect_bids
from exchange.auction.ledger import InMemoryLedgerSink
from exchange.auction.routes import configure_auctions
from exchange.auction.state import AuctionStateMachine
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from fastapi.testclient import TestClient
from store_agent.runtime import Decline
from store_agent.runtime import bid as run_agent

REPO_ROOT = Path(__file__).resolve().parents[3]
MARKET_FILE = REPO_ROOT / "apps" / "buyer" / "devstack" / "demo-market.json"

#: The store that is ASKED about one product and honestly ANSWERS about another, because the
#: one it was asked about is sold out today. This is the whole D58 case.
COUNTER_PROPOSER = "demo-woolworks"
ROSTERED_PRODUCT = "beanie-merino-01"
COUNTER_PRODUCT = "beanie-merino-lite"
COUNTER_PRICE = 39.0
ROSTERED_PRICE = 78.0

#: The store that bids exactly what it was asked about, honestly. The regression control: D58
#: must not have changed anything for the ordinary case.
STRAIGHT_BIDDER = "demo-alpine-supply"

#: The store that lies. Its own live feed says thirty units left; the platform's crawl of the
#: same product says two. That is exactly the adversarial check D55 buys with a sponsored slot,
#: and it must keep firing.
LIAR = "demo-fastfleece"
LIAR_ROSTERED_PRODUCT = "beanie-fleece-01"
LIAR_TRUE_UNITS = 2

#: A second product of the LIAR's that the platform really did crawl, and whose crawled
#: ``units_left`` happens to equal the number the liar's agent states about the product it was
#: actually asked about. It exists so the sibling-laundering attack has a target: change one
#: field, ``offer.product_ref``, and the false claim becomes true of the row it now resolves
#: against. Nothing else on the bid moves — not the price, not the variant, not the checkout URL.
LIAR_SIBLING = "beanie-fleece-xl"

#: ``claim_type -> trust dimension``. The exchange announces no ``claim_verified`` event until a
#: deployment hands it this routing (``ranking.serving.claim_dimensions_of``), and the verdicts
#: are what this file reads, so the test wires one. The mapping is the approved table's own
#: shape, not an invention: every key is a ``contracts.ClaimType`` and every value a
#: ``contracts.TrustDimension``.
CLAIM_DIMENSIONS: dict[Any, str] = {
    "price": "price_honored",
    "unit_price": "price_honored",
    "total_price": "price_honored",
    "discount": "discount_honored",
    "specifications": "catalog_claim_accuracy",
    None: "catalog_claim_accuracy",
}


# =====================================================================================
# The market, assembled from the shipped demo rather than invented here
# =====================================================================================
def _market(*, force_counter_proposal: bool = False) -> dict[str, Any]:
    """The shipped demo market, optionally with one store given a reason to counter-propose.

    Built from ``apps/buyer/devstack/demo-market.json`` — the same file
    ``test_hosted_bid_boundary.py`` drives honest traffic from — because a market invented in
    this file would prove that this file's fixtures behave, not that the product does.

    With ``force_counter_proposal`` the counter-proposer gets a SECOND, cheaper product in its
    own catalogue and the product it is rostered for goes out of stock, so its advocate has a
    real reason to answer about the other one: ``store_agent.runtime._gather`` drops an
    out-of-stock product from the candidates, the solicited product is therefore inadmissible,
    and ``bidding._chosen`` lets the agent's own pick stand. Without it, every store bids the
    product it was asked about, which is what D58's half (a) is for and what the demo does.
    """
    market = json.loads(MARKET_FILE.read_text(encoding="utf-8"))
    if force_counter_proposal:
        store = next(s for s in market["stores"] if s["store_id"] == COUNTER_PROPOSER)
        cheaper = copy.deepcopy(store["catalog"][ROSTERED_PRODUCT])
        cheaper["product_ref"] = COUNTER_PRODUCT
        cheaper["list_price"] = COUNTER_PRICE
        store["catalog"][COUNTER_PRODUCT] = cheaper
        store["live_state"][COUNTER_PRODUCT] = copy.deepcopy(store["live_state"][ROSTERED_PRODUCT])
        store["live_state"][ROSTERED_PRODUCT]["in_stock"] = False
    return market


def _platform_snapshot(store: dict[str, Any]) -> dict[str, Any]:
    """The PLATFORM's own crawl of one store — never the store's own context.

    Built through ``apps/buyer/devstack/run.py:catalog_snapshot``, which is the shape the
    shipped demo really wires into ``configure_ranking(catalog=…)``, and then diverged from the
    store's own context in exactly one place: the liar's stock. A snapshot copied from the
    store's context could never contradict it, and a test whose liar cannot be caught is not a
    control.
    """
    from devstack.run import catalog_snapshot  # noqa: PLC0415 - see `_devstack_importable`

    snapshot = catalog_snapshot(store)
    if store["store_id"] == COUNTER_PROPOSER and COUNTER_PRODUCT in store["catalog"]:
        other = copy.deepcopy(store)
        other["product_ref"] = COUNTER_PRODUCT
        snapshot["products"].extend(catalog_snapshot(other)["products"])
    if store["store_id"] == LIAR:
        snapshot["products"][0]["attributes"]["units_left"] = {"value": LIAR_TRUE_UNITS}
        sibling = copy.deepcopy(snapshot["products"][0])
        sibling["product_ref"] = LIAR_SIBLING
        sibling["canonical_name"] = LIAR_SIBLING
        # A REAL crawled row, whose real stock is the number the liar states about the OTHER
        # product. This is the attack surface, not a convenience.
        sibling["attributes"] = dict(sibling["attributes"], units_left={"value": 30})
        snapshot["products"].append(sibling)
    return snapshot


@pytest.fixture(scope="module", autouse=True)
def _devstack_importable() -> Any:
    """``apps/buyer/devstack`` on the path, for the two snapshot builders the demo ships.

    Imported rather than reimplemented: ``catalog_snapshot`` is what the demo actually hands
    ``configure_ranking``, and a second copy of it here would let this file pass against a
    snapshot shape the product does not produce.
    """
    import sys  # noqa: PLC0415

    buyer = str(REPO_ROOT / "apps" / "buyer")
    added = buyer not in sys.path
    if added:
        sys.path.insert(0, buyer)
    yield
    if added:
        sys.path.remove(buyer)


class _RealAgents:
    """This repo's own hosted store-agent runtime, answering a real ``BidRequest``.

    Not a double. ``store_agent.runtime.bid`` is the only thing in this tree that produces a
    hosted bid in production; every claim it emits comes out of a tool hook that stamps its own
    provenance, and it is the thing that picks which product to answer about.
    """

    def __init__(self, market: dict[str, Any], tamper: Any = None) -> None:
        self.by_id = {str(row["store_id"]): row for row in market["stores"]}
        self.cluster = str(market["stores"][0]["envelope"]["pursue_clusters"][0])
        self.sent: dict[str, dict[str, Any]] = {}
        self.solicited: dict[str, Any] = {}
        # `(store_id, payload) -> payload`. A hosted bid is arbitrary third-party JSON over
        # HTTP; the exchange validates it against no model on this path. So an attack that
        # edits the honest runtime's reply after the fact is not a contrived fixture — it is
        # exactly what a Tier-1 operator running its own build would send.
        self.tamper = tamper

    def solicit(self, store: dict[str, Any]) -> dict[str, Any] | None:
        from devstack.run import store_context  # noqa: PLC0415 - see `_devstack_importable`

        row = self.by_id[str(store["store_id"])]
        # The published `BidRequest`, including D58's `product_ref`. The exchange's own
        # `composition.HttpBidSolicitor` writes this field off the same roster row.
        request = {
            "auction_id": "auction-d58",
            "intent": {
                "intent_id": "intent-1",
                "cluster_id": self.cluster,
                "use_case": "a warm hat for winter commuting",
                "budget_band": {"min": 0.0, "max": 500.0},
            },
            "product_ref": store.get("product_ref"),
            "profile": {"pseudonym": "psn-d58"},
            "respond_by": "2999-01-01T00:00:00Z",
        }
        self.solicited[str(store["store_id"])] = request["product_ref"]
        answer = run_agent(request, store_context(row))
        assert not isinstance(answer, Decline), (
            f"{store['store_id']} declined ({getattr(answer, 'reason', '?')}); this file needs "
            f"a real hosted pitch, not a manufactured fallback"
        )
        payload = answer.model_dump(mode="json")
        if self.tamper is not None:
            payload = self.tamper(str(store["store_id"]), payload)
        self.sent[str(store["store_id"])] = payload
        return {"store_id": str(store["store_id"]), "received_at": time.time(), "bid": payload}

    __call__ = solicit


def _roster(market: dict[str, Any]) -> list[dict[str, Any]]:
    """The GRAPH-shaped roster: ``{store_id, tier, product_ref, list_price}`` and nothing else.

    Deliberately without ``max_discount_pct``. ``SolicitedShop.as_roster_row`` emits exactly
    these four keys, and the missing fifth is what made the price wall abstain rather than
    refuse — see this module's header. Testing the shape the caller-stated roster happens to
    carry would test the one path where the defect was already half-caught.
    """
    from devstack.run import buyer_roster  # noqa: PLC0415 - see `_devstack_importable`

    return [
        {k: v for k, v in row.items() if k != "max_discount_pct"} for row in buyer_roster(market)
    ]


def _served(
    market: dict[str, Any], agents: _RealAgents
) -> tuple[dict[str, Any], InMemoryLedgerSink]:
    """One real ``POST /auctions``, wired the way the shipped demo wires it."""
    stores = market["stores"]
    roster = _roster(market)
    sink = InMemoryLedgerSink()
    app = create_app()
    configure_auctions(
        app,
        machine=AuctionStateMachine(ledger=sink),
        solicitor=agents,
        eligibility=StaticSellerEligibility({row["store_id"]: ELIGIBLE for row in roster}),
    )
    configure_ranking(
        app,
        trust_snapshot={
            str(s["store_id"]): {"blacklisted": False, "score": float(s["trust"]["score"])}
            for s in stores
        },
        registered_domains=StaticRegisteredDomains(
            {str(s["store_id"]): str(s["store_domain"]) for s in stores}
        ),
        catalog=StaticCatalogSnapshots({str(s["store_id"]): _platform_snapshot(s) for s in stores}),
        claim_dimensions=CLAIM_DIMENSIONS,
    )
    response = TestClient(app).post(
        "/auctions",
        json={
            "intent": {
                "intent_id": "intent-1",
                "cluster_id": agents.cluster,
                "hard_constraints": [],
            },
            "roster": roster,
            "bid_timeout_seconds": 10.0,
        },
    )
    assert response.status_code == 201, response.text
    return response.json(), sink


def _verdicts(sink: InMemoryLedgerSink, store_id: str) -> list[str]:
    return [
        str(event["payload"]["status"])
        for event in sink.events
        if event["kind"] == "claim_verified" and event["store_id"] == store_id
    ]


def _penalties(sink: InMemoryLedgerSink, store_id: str) -> list[dict[str, Any]]:
    return [
        dict(event["payload"])
        for event in sink.events
        if event["kind"] == "policy_event" and event["store_id"] == store_id
    ]


def _ranked(body: dict[str, Any], store_id: str) -> dict[str, Any]:
    for row in body["ranked"]:
        if row["store_id"] == store_id:
            return row
    raise AssertionError(f"{store_id} is not ranked at all: {body['ranked']}")


def _entry(body: dict[str, Any], store_id: str) -> dict[str, Any]:
    return next(row for row in body["entries"] if row["store_id"] == store_id)


@pytest.fixture(scope="module")
def served() -> Any:
    market = _market()
    agents = _RealAgents(market)
    body, sink = _served(market, agents)
    return market, agents, body, sink


@pytest.fixture(scope="module")
def counter_proposed() -> Any:
    market = _market(force_counter_proposal=True)
    agents = _RealAgents(market)
    body, sink = _served(market, agents)
    return market, agents, body, sink


# =====================================================================================
# 1. The served route, both directions, one request
# =====================================================================================
def test_the_solicitation_names_the_product_the_exchange_rostered(served: Any) -> None:
    """(a). The wire contract says what it is soliciting a bid ON.

    Asserted against what the store agents were really handed, because the field being
    *declarable* and the field being *sent* are different claims and only the second one closes
    anything. ``composition.HttpBidSolicitor.solicit`` writes it off the same roster row on the
    HTTP path — measured there over a real socket — and this drives the in-process one.
    """
    market, agents, _body, _sink = served
    assert agents.solicited == {
        str(row["store_id"]): str(row["product_ref"]) for row in _roster(market)
    }, agents.solicited


def test_an_honest_store_told_what_it_was_asked_about_is_graded_verified(served: Any) -> None:
    """**The defect, closed.** The store now bids the product it was asked about, so its true
    claims are checked against the platform's snapshot of that same product.

    This is the whole of half (a) and it is what the reported symptom was: hosted agents were
    taking ``policy_penalties: -0.15`` because nobody had told them which product the auction
    was about, so they picked their own and the exchange graded them against a different one.
    """
    _market_, agents, body, sink = served

    offer = agents.sent[COUNTER_PROPOSER]["offer"]
    assert offer["product_ref"] == ROSTERED_PRODUCT, offer

    entry = _entry(body, COUNTER_PROPOSER)
    assert entry["fallback"] is False, entry

    verdicts = _verdicts(sink, COUNTER_PROPOSER)
    assert "contradicted" not in verdicts, verdicts
    assert verdicts.count("verified") >= 3, verdicts

    assert _penalties(sink, COUNTER_PROPOSER) == [], _penalties(sink, COUNTER_PROPOSER)
    components = _ranked(body, COUNTER_PROPOSER)["components"]
    assert "policy_penalties" not in components, components


def test_a_second_honest_store_on_the_same_request_is_unpenalised_too(served: Any) -> None:
    """The regression control. Nothing about the ordinary case moved."""
    _market_, agents, body, sink = served
    assert agents.sent[STRAIGHT_BIDDER]["offer"]["product_ref"] == "beanie-merino-pro"
    assert _entry(body, STRAIGHT_BIDDER)["fallback"] is False
    assert "contradicted" not in _verdicts(sink, STRAIGHT_BIDDER)
    assert _penalties(sink, STRAIGHT_BIDDER) == []
    assert "policy_penalties" not in _ranked(body, STRAIGHT_BIDDER)["components"]


def test_a_dishonest_store_is_still_contradicted_and_still_penalised(served: Any) -> None:
    """**The control that keeps the fix from being a blanket amnesty.**

    Same request, same ranking, same catalogue wiring. This store's agent states thirty units
    left because its own live feed says so; the platform's crawl of the same product says two.
    D55's whole asymmetry is that a seller's purchased message gets checked against the
    platform's own snapshot, and it does: one ``contradicted`` verdict, one
    ``contradicted_claim`` policy event at the published 0.15, and the penalty on the score.
    """
    _market_, agents, body, sink = served

    claims = {c["key"]: c["value"] for c in agents.sent[LIAR]["claims"] if "key" in c}
    assert claims["units_left"] != LIAR_TRUE_UNITS, claims

    verdicts = _verdicts(sink, LIAR)
    assert verdicts.count("contradicted") == 1, verdicts

    events = _penalties(sink, LIAR)
    assert [event["kind"] for event in events] == ["contradicted_claim"], events
    assert events[0]["count"] == 1, events
    assert events[0]["penalty_per_event"] == pytest.approx(0.15), events

    components = _ranked(body, LIAR)["components"]
    assert components["policy_penalties"] == pytest.approx(-0.15), components


def test_the_honest_and_the_dishonest_verdict_come_from_the_same_served_request(
    served: Any,
) -> None:
    """One auction, one ranking, opposite outcomes — asserted together on purpose.

    A gate that only ever proves the refusal fires is the documented blind spot this repo has
    here. Split across two requests these two assertions could both pass against a build that
    penalises everyone or nobody, depending on which fixture each one wired.
    """
    _market_, _agents, body, sink = served
    penalised = {
        event["store_id"]
        for event in sink.events
        if event["kind"] == "policy_event" and event["payload"]["kind"] == "contradicted_claim"
    }
    assert penalised == {LIAR}, penalised
    assert {row["store_id"] for row in body["entries"]} == {
        COUNTER_PROPOSER,
        STRAIGHT_BIDDER,
        LIAR,
    }


# =====================================================================================
# 2. The two attacks that made D58 withdraw "grade what was offered"
# =====================================================================================
def test_a_liar_cannot_launder_a_false_claim_onto_a_crawled_sibling_product() -> None:
    """**The attack that killed half (b), asserted as CLOSED.**

    An implementation of D58 that graded the product the OFFER named — guarded so that only a
    product the platform's own snapshot carried could be chosen — was built, driven, and
    withdrawn on this measurement. The attack changes exactly ONE field of the bid this repo's
    real hosted runtime produced::

        payload["offer"]["product_ref"] = "beanie-fleece-xl"

    Nothing else moves: not the price, not ``variant_ref``, not ``checkout_url``. Under that
    implementation the store's false stock claim was graded against the sibling's crawled row —
    where thirty really is the number — and came back ``verified`` with the ``contradicted_claim``
    penalty gone, **while still selling the original variant at the original price**. The
    defence in its own docstring ("the graded subject and the purchased subject become the same
    object") is true only of the reference agent: a hosted bid is arbitrary third-party JSON,
    the exchange validates it against no model on this path, and the checkout permalink is
    minted from ``variant_ref`` (``checkout/provider.py``) with nothing joining the two.

    So which product a bid's claims resolve against is the AUCTION's fact. What this asserts is
    that the laundering buys nothing: the false claim is never attested ``verified``, and the
    bid is not ranked. The store's remaining options are to lie about the product it WAS asked
    about — caught, ``contradicted``, penalised, asserted below on the same market — or to lose
    the auction, which is what declining has always cost.
    """
    market = _market()

    def name_the_sibling(store_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if store_id == LIAR:
            payload["offer"]["product_ref"] = LIAR_SIBLING
        return payload

    agents = _RealAgents(market, tamper=name_the_sibling)
    body, sink = _served(market, agents)

    sent = agents.sent[LIAR]
    assert sent["offer"]["product_ref"] == LIAR_SIBLING, sent["offer"]
    # The variant and the checkout URL still name the ORIGINAL product. That is the whole point.
    assert LIAR_SIBLING not in str(sent["offer"].get("checkout_url") or ""), sent["offer"]

    # The laundering does not happen: no `verified` verdict is minted for the false claim, and
    # the bid is not ranked at all. The store is represented at its rostered list price (R10).
    assert "verified" not in _verdicts(sink, LIAR), _verdicts(sink, LIAR)
    entry = _entry(body, LIAR)
    assert entry["fallback"] is True, entry
    assert entry["fallback_reason"] == UNRECONCILABLE_PRICE_REASON, entry

    # **What it costs, stated rather than glossed.** The store also escapes the `contradicted`
    # record it would have taken by bidding the product it was asked about — the SAME record it
    # escapes by not answering at all, which every store has always been able to do for free
    # (see `test_suppressing_a_contradiction_by_not_asserting_is_not_new`). What it does NOT get
    # is the outcome the withdrawn implementation gave it: `verified`, no penalty, and the slot.
    honest_run = _RealAgents(market)
    honest_body, honest_sink = _served(market, honest_run)
    assert _verdicts(honest_sink, LIAR).count("contradicted") == 1
    assert _ranked(honest_body, LIAR)["components"]["policy_penalties"] == pytest.approx(-0.15)
    assert "policy_penalties" not in _ranked(body, LIAR)["components"]


def test_a_bidder_cannot_choose_the_price_value_denominator() -> None:
    """**The second attack that killed half (b).**

    The withdrawn implementation also let the price wall re-price a counter-proposal out of the
    platform's own catalogue, which handed the bidder the ``price_value`` denominator. Measured
    on the served route, THE STORE CHARGING THE SAME 44.00 IN BOTH ROWS::

        bids the rostered product (crawled at   45): price_value = 0.0079   -> last
        names a sibling           (crawled at 5000): price_value = 0.15     -> first

    19x on the term, to its saturated maximum, last place to first, for the same money — chosen
    by one string in an unauthenticated bid body. The denominator is the ROSTER's and stays the
    roster's; an entry admitted at all is about the rostered product, because a bid naming any
    other is refused.
    """
    market = _market()
    liar = next(s for s in market["stores"] if s["store_id"] == LIAR)
    charged = 44.0

    def name_the_dear_sibling(store_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if store_id == LIAR:
            payload["offer"]["product_ref"] = LIAR_SIBLING
            payload["offer"]["unit_price"] = charged
            payload["offer"]["total_price"] = charged
        return payload

    honest = _RealAgents(market)
    honest_body, _ = _served(market, honest)
    honest_pv = _ranked(honest_body, LIAR)["components"]["price_value"]

    agents = _RealAgents(market, tamper=name_the_dear_sibling)
    body, _sink = _served(market, agents)

    # The bid is refused, so the store is represented at its ROSTERED list price and the term
    # is computed on that — never on a listing the bidder nominated.
    entry = _entry(body, LIAR)
    assert entry["fallback"] is True, entry
    assert entry["fallback_reason"] == UNRECONCILABLE_PRICE_REASON, entry
    assert entry["unit_price"] == pytest.approx(
        float(liar["catalog"][LIAR_ROSTERED_PRODUCT]["list_price"])
    )
    assert _ranked(body, LIAR)["components"]["price_value"] == pytest.approx(0.0)
    assert _ranked(body, LIAR)["components"]["price_value"] <= honest_pv + 1e-9


# =====================================================================================
# 3. The honest counter-proposal: represented, not ranked — and NOT falsely contradicted
# =====================================================================================
def test_an_honest_counter_proposal_is_represented_rather_than_falsely_contradicted(
    counter_proposed: Any,
) -> None:
    """What D58 leaves the honest counter-proposer, stated plainly rather than sold as a win.

    The store is asked about a product it cannot sell today, so its advocate answers about
    another one truthfully. The exchange does not rank that answer — it cannot price a product
    it did not solicit against an offer whose ``product_ref`` nothing binds to what the checkout
    sells — and represents the store at its rostered list price (R10).

    **What it does NOT do is accuse it.** No claim of that store's is graded against a product
    it did not bid, so there is no ``contradicted`` verdict, no ``contradicted_claim`` event and
    no ``policy_penalties`` — which is the defect this whole entry is about. And the fabricated
    ``price_value`` is gone too: the entry is priced at its rostered list price, so the term
    reads the honest 0.0 rather than the 0.15 it used to read for a discount nobody gave.

    This is a real cost to a real behaviour the product wants, and D58 names the condition under
    which it stops being necessary.
    """
    _market_, agents, body, sink = counter_proposed

    assert agents.solicited[COUNTER_PROPOSER] == ROSTERED_PRODUCT
    assert agents.sent[COUNTER_PROPOSER]["offer"]["product_ref"] == COUNTER_PRODUCT

    entry = _entry(body, COUNTER_PROPOSER)
    assert entry["fallback"] is True, entry
    assert entry["fallback_reason"] == UNRECONCILABLE_PRICE_REASON, entry
    assert entry["unit_price"] == pytest.approx(ROSTERED_PRICE), entry

    assert _verdicts(sink, COUNTER_PROPOSER) == [], _verdicts(sink, COUNTER_PROPOSER)
    assert _penalties(sink, COUNTER_PROPOSER) == []
    components = _ranked(body, COUNTER_PROPOSER)["components"]
    assert "policy_penalties" not in components, components
    assert components["price_value"] == pytest.approx(0.0), components

    # ...and the liar on that same request is still caught, so the representation above is not
    # the fix quietly turning every verdict off.
    assert _verdicts(sink, LIAR).count("contradicted") == 1
    assert _ranked(body, LIAR)["components"]["policy_penalties"] == pytest.approx(-0.15)


def test_suppressing_a_contradiction_by_not_asserting_is_not_new(served: Any) -> None:
    """The one thing a refused counter-proposal buys a liar, measured against what it already had.

    A store whose bid is refused becomes an R10 fallback, and a fallback carries no claims, so
    no verdict is minted about it. That is a route to "no contradiction on my record" — and it
    is the SAME route a store has always had for free by not answering at all. Both cost the
    auction, and neither is new::

        bids, and lies              -> fallback=False   claims kept, graded, penalised
        simply does not answer      -> fallback=True    reason=no_response          claims=[]
        answers 204 / declines      -> fallback=True    reason=response_carried_no_bid claims=[]

    So the exchange's count of contradictions has always been suppressible by the party being
    measured, at the price of the auction. D58 adds a third way to decline; it adds no cheaper
    one.
    """
    row = {"store_id": "s1", "tier": 1, "product_ref": "prod-p", "list_price": 100.0}
    lying = {
        "auction_id": "a",
        "store_id": "s1",
        "offer": {
            "bid_offer_id": "o",
            "product_ref": "prod-p",
            "unit_price": 100.0,
            "total_price": 100.0,
            "currency": "USD",
            "expires_at": "2999-01-01T00:00:00Z",
        },
        "claims": [
            {
                "key": "units_left",
                "value": 30,
                "provenance": {
                    "source": "pixel_feed",
                    "ref": "pixel:s1#prod-p",
                    "observed_at": "2026-01-01T00:00:00Z",
                    "authority_rank": 1,
                },
            }
        ],
    }
    kept = collect_bids([row], [{"store_id": "s1", "received_at": 1.0, "bid": lying}], 200.0)[0]
    assert kept.fallback is False and kept.claims, kept

    silent = collect_bids([row], [], 200.0)[0]
    assert silent.fallback is True and silent.claims == [], silent
    assert silent.fallback_reason == "no_response", silent

    declined = collect_bids([row], [{"store_id": "s1", "received_at": 1.0, "bid": None}], 200.0)[0]
    assert declined.fallback is True and declined.claims == [], declined


# =====================================================================================
# 4. The price wall, at the unit
# =====================================================================================
def _bid(product_ref: str, price: float) -> dict[str, Any]:
    return {
        "auction_id": "auction-1",
        "store_id": "store-x",
        "offer": {
            "bid_offer_id": "offer-1",
            "product_ref": product_ref,
            "unit_price": price,
            "total_price": price,
            "currency": "USD",
            "expires_at": "2999-01-01T00:00:00Z",
        },
        "claims": [],
    }


def _collected(roster_row: dict[str, Any], bid: dict[str, Any]) -> Any:
    responses = [{"store_id": "store-x", "received_at": 100.0, "bid": bid}]
    return collect_bids([roster_row], responses, 200.0)[0]


@pytest.mark.parametrize("max_discount_pct", [None, 20.0])
def test_a_bid_about_another_product_is_refused_either_way(
    max_discount_pct: float | None,
) -> None:
    """The measured inconsistency, closed.

    Before ``_answers_about_another_product`` joined ``_is_judged``, these two rows disagreed
    about the identical bid: with a ``max_discount_pct`` on the roster the wall refused, without
    one it abstained and the bid was admitted at a price nothing had checked and then credited a
    ``price_value`` computed across two products. The field has nothing to do with which product
    was offered, and ``SolicitedShop.as_roster_row`` — the graph roster, D55's organic half —
    never carries it, so the abstaining row is the one the served product actually got.

    Refusal is the fail-closed direction and the one this wall has always taken for a price it
    cannot read: the store is represented at its ROSTERED list price, R10 unchanged.
    """
    row: dict[str, Any] = {
        "store_id": "store-x",
        "tier": 1,
        "product_ref": "prod-p",
        "list_price": 78.0,
    }
    if max_discount_pct is not None:
        row["max_discount_pct"] = max_discount_pct

    entry = _collected(row, _bid("prod-q", 39.0))
    assert entry.fallback is True
    assert entry.fallback_reason == UNRECONCILABLE_PRICE_REASON
    assert entry.price_reasons, entry.price_reasons
    assert entry.bid["offer"]["unit_price"] == pytest.approx(78.0)
    assert entry.list_price == pytest.approx(78.0)


def test_a_roster_row_that_names_no_product_still_makes_no_claim_about_one() -> None:
    """``RosterEntry.product_ref`` is optional, and a row naming none contradicts nothing.

    Guarded because ``_answers_about_another_product`` is a new clause on a wall that decides
    money: widening it to "the row does not name this product" would refuse every bid on a
    roster that never named a product at all, which is a shape the request door allows and
    which nothing about D58 makes wrong.
    """
    row = {"store_id": "store-x", "tier": 1, "list_price": 78.0}
    entry = _collected(row, _bid("prod-q", 39.0))
    assert entry.fallback is False
    assert entry.list_price == pytest.approx(78.0)


def test_an_offer_naming_no_product_is_not_an_offer_about_another_one() -> None:
    """Absence is not disagreement. An offer stating no ``product_ref`` is the unreadable-offer
    case the other clauses already answer, and this one must not double-refuse it."""
    row = {"store_id": "store-x", "tier": 1, "product_ref": "prod-p", "list_price": 78.0}
    bid = _bid("prod-p", 60.0)
    del bid["offer"]["product_ref"]
    assert _collected(row, bid).fallback is False


def test_a_padded_roster_ref_does_not_refuse_a_store_for_answering_what_was_asked() -> None:
    """The self-inflicted fault D58's readers would have had, closed and pinned.

    ``RosterEntry.product_ref`` has no trim and no ``min_length``, so ``"prod-p "`` arrives
    intact on an unauthenticated ``POST /auctions`` body. The exchange solicits with the trimmed
    ref and the agent matches its catalog key with the trimmed ref, so a wall comparing the
    PADDED roster spelling refuses a store for answering exactly the question it was asked.
    Measured, before the trim existed::

        roster product_ref     : 'beanie-merino-01 '
        BidRequest.product_ref : 'beanie-merino-01 '   (sent unstripped)
        agent offer.product_ref: 'beanie-merino-01'    (matched stripped)
        verdict                : fallback=True bid_price_unreconcilable

    Whitespace is the ONLY normalisation applied; case and unicode form are still different
    keys, because they are different keys to the catalogue too.
    """
    row = {"store_id": "store-x", "tier": 1, "product_ref": " prod-p ", "list_price": 78.0}
    entry = _collected(row, _bid("prod-p", 60.0))
    assert entry.fallback is False, entry
    assert entry.bid["offer"]["unit_price"] == pytest.approx(60.0), entry

    # ...and a genuinely different product is still a different product.
    assert _collected(row, _bid("prod-q", 60.0)).fallback is True

    from exchange.composition import _solicited_product_ref  # noqa: PLC0415

    assert _solicited_product_ref({"product_ref": " prod-p "}) == "prod-p"
    assert _solicited_product_ref({"product_ref": "   "}) is None
    assert _solicited_product_ref({"product_ref": 7}) is None
    assert _solicited_product_ref({}) is None


def test_a_product_ref_nobody_can_read_states_nothing_rather_than_raising() -> None:
    """The new clause is a read that turns a money wall ON, so it fails closed rather than out.

    ``_states_an_authorized_depth`` is wrapped for exactly this reason and this is the same
    shape: an exception escaping ``collect_bids`` ends the auction every other store on the
    roster was bidding in, which is the class of escape ``_unusable_because`` exists to stop.

    **Reachable only by a library caller**, and that is stated rather than overclaimed:
    ``RosterEntry.product_ref`` is typed ``str | None``, so pydantic answers 422 before this
    module sees anything else on the served door.
    """

    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("a hostile __str__")

    class RefRaises(dict):  # type: ignore[type-arg]
        def get(self, key: Any, default: Any = None) -> Any:
            if key == "product_ref":
                raise RuntimeError("a hostile roster row")
            return super().get(key, default)

    row = {"store_id": "store-x", "tier": 1, "product_ref": Unprintable(), "list_price": 78.0}
    assert _collected(row, _bid("prod-q", 39.0)).fallback is False

    raising = RefRaises({"store_id": "store-x", "tier": 1, "list_price": 78.0})
    assert _collected(raising, _bid("prod-q", 39.0)).fallback is False
