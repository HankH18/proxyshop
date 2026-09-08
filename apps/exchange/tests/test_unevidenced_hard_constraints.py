"""A must-have nobody in the auction can evidence, driven through ``POST /auctions``.

WHY THIS FILE EXISTS, and why it is here rather than in the buyer service
------------------------------------------------------------------------
A shopper who says "espresso" gets ``brew_method eq espresso`` on their confirmed intent —
``fixtures/dialogues/espresso_needs_a_budget.json`` is the frozen golden that grades exactly
that, and the clarifier's lexicon and the recorded LLM fixture both mint it. Nothing in this
network carries a verified ``brew_method`` reading, so under R19 every candidate fails the
constraint and the shopper is shown an empty shortlist with no explanation.

That was closed once at the WRONG END, by a buyer-service guard that dropped any constraint
naming an attribute ``fixtures/catalog/coffee.json`` did not declare. Two measurements say
that end cannot work:

* it contradicted the frozen golden, which requires the confirmed intent to record what the
  buyer actually said;
* and its premise was false. Driven through this exchange's own ``POST /auctions`` with the
  S1 roster, ``list_price lte 500``, ``boiler_type eq 'heat exchange'`` and ``roast_level eq
  dark`` — all three DECLARED by that catalogue config, the first two carried verbatim in the
  stores' own catalogue rows — produced **0 slots** apiece, exactly as ``brew_method`` did.
  What a catalogue config names is not what a candidate carries as verified evidence, and the
  buyer service holds neither fact.

The deciding fact is per-auction and lives here: what THESE candidates carry. So the tests
below are the re-homed version of that guard, driven over the served door.

The property, stated once
-------------------------
A hard constraint that some candidate can evidence is a filter and stays one — the stores
that fail it are excluded, and that is the buyer's must-have being honoured. A hard
constraint NO candidate carries any reading for narrows nothing; applying it can only empty
the shortlist. Those are set aside, but never silently: each comes back in
``relaxed_constraints`` with the reason, so "we could not check this" is something the
shopper is told rather than something they infer from an empty page.
"""

from __future__ import annotations

import time
from typing import Any

from exchange.auction.ledger import InMemoryLedgerSink
from exchange.auction.routes import configure_auctions
from exchange.auction.state import AuctionStateMachine
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.reasons import REASON_HARD_CONSTRAINT, REASON_UNEVIDENCED_CONSTRAINT
from fastapi.testclient import TestClient

STORE_A = "store-a"
STORE_B = "store-b"

#: Far enough ahead that nothing here expires while the suite runs.
LIVE_FOR_AN_HOUR = 3600.0

#: The buyer's own words, as the clarifier renders them. Not retyped: this is the constraint
#: the frozen golden `fixtures/dialogues/espresso_needs_a_budget.json` pins on the confirmed
#: intent, and the one the recorded LLM fixture proposes.
ESPRESSO = {"field": "brew_method", "op": "eq", "value": "espresso"}


def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def _claim(key: str, value: Any) -> dict[str, Any]:
    """One claim in exactly the shape a BIDDER can write — with no verdict on it (ESC-020)."""
    return {
        "key": key,
        "value": value,
        "provenance": {"source": "owner_statement", "ref": f"ref:{key}", "authority_rank": 1},
    }


def _catalog(capacities: dict[str, int]) -> Any:
    """The catalogue THIS exchange grades each store's claims against.

    Only ``capacity_l`` is in it, for every store. That is the whole scenario: the attribute
    the buyer's must-have names is one no snapshot here holds, so no claim about it could
    ever be verified even by an honest store that wanted to make one.
    """
    from exchange.ranking.verification import StaticCatalogSnapshots

    return StaticCatalogSnapshots(
        {
            store: {
                "snapshot_id": f"snap-{store}",
                "products": [
                    {
                        "product_ref": "product-1",
                        "canonical_name": "product-1",
                        "evidence_ref": f"snap-{store}#product-1",
                        "attributes": {"capacity_l": {"value": capacity}},
                    }
                ],
            }
            for store, capacity in capacities.items()
        }
    )


class _Bidders:
    def __init__(self, bids: dict[str, dict[str, Any]]) -> None:
        self.bids = dict(bids)

    def solicit(self, store: dict[str, Any]) -> dict[str, Any] | None:
        bid = self.bids.get(str(store["store_id"]))
        return (
            None
            if bid is None
            else {"store_id": str(store["store_id"]), "received_at": time.time(), "bid": dict(bid)}
        )

    __call__ = solicit


def _bid(store_id: str, price: float, capacity: int) -> dict[str, Any]:
    return {
        "auction_id": None,
        "store_id": store_id,
        "offer": {
            "product_ref": "product-1",
            "unit_price": price,
            "total_price": price,
            "currency": "USD",
            "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
            "expires_at": time.time() + LIVE_FOR_AN_HOUR,
        },
        "claims": [_claim("capacity_l", capacity)],
        "agent_version": "1.0.0",
        "schema_version": "1.0.0",
    }


def _rostered(store_id: str, list_price: float) -> dict[str, Any]:
    return {
        "store_id": store_id,
        "tier": 1,
        "product_ref": "product-1",
        "list_price": list_price,
    }


def _wired_app(
    *,
    capacities: dict[str, int],
    blacklisted: tuple[str, ...] = (),
) -> Any:
    """A fully wired exchange: eligibility, a bid client, trust, a seller registry, a catalogue."""
    from exchange.ranking.serving import configure_ranking

    stores = tuple(capacities)
    app = create_app()
    configure_auctions(
        app,
        solicitor=_Bidders(
            {
                store: _bid(store, 100.0 + index, capacities[store])
                for index, store in enumerate(stores)
            }
        ),
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={
            store: {"blacklisted": store in blacklisted, "score": 0.6} for store in stores
        },
        registered_domains=StaticRegisteredDomains({store: _domain(store) for store in stores}),
        catalog=_catalog(capacities),
    )
    return app


def _post(app: Any, stores: tuple[str, ...], hard_constraints: Any) -> dict[str, Any]:
    intent: dict[str, Any] = {"intent_id": "intent-1", "cluster_id": "cluster-1"}
    if hard_constraints is not None:
        intent["hard_constraints"] = hard_constraints
    response = TestClient(app).post(
        "/auctions",
        json={
            "intent": intent,
            "roster": [_rostered(store, 200.0) for store in stores],
            "bid_timeout_seconds": 2.0,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _slot_stores(body: dict[str, Any]) -> list[str]:
    """Which store each shortlist slot belongs to, read off the served body."""
    ranked = {row["bid_ref"]: row["store_id"] for row in body["ranked"]}
    return [ranked[slot["bid_ref"]] for slot in body["shortlist"]["slots"]]


# =====================================================================================
# The shopper whose must-have nobody can answer
# =====================================================================================
def test_a_must_have_no_candidate_can_evidence_still_produces_a_shortlist() -> None:
    """The zero-slot journey, over the served door. This is the regression."""
    app = _wired_app(capacities={STORE_A: 35, STORE_B: 35})
    body = _post(app, (STORE_A, STORE_B), [ESPRESSO])

    slots = body["shortlist"]["slots"]
    assert slots, (
        f"a shopper who said 'espresso' was handed an empty shortlist: excluded={body['excluded']}"
    )
    assert sorted(_slot_stores(body)) == [STORE_A, STORE_B]


def test_the_constraint_that_was_set_aside_is_reported_verbatim_with_a_reason() -> None:
    """Dropping a stated must-have without saying so is worse than the empty shortlist."""
    app = _wired_app(capacities={STORE_A: 35, STORE_B: 35})
    body = _post(app, (STORE_A, STORE_B), [ESPRESSO])

    relaxed = body["relaxed_constraints"]
    assert [(r["field"], r["op"], r["value"]) for r in relaxed] == [
        (ESPRESSO["field"], ESPRESSO["op"], ESPRESSO["value"])
    ], relaxed
    reason = relaxed[0]["reason"]
    assert reason.startswith(REASON_UNEVIDENCED_CONSTRAINT), reason
    assert "brew_method" in reason
    assert "NOT applied" in reason, (
        f"the reason does not say the constraint stopped being a filter: {reason!r}"
    )


def test_a_relaxed_constraint_is_never_counted_as_a_verified_hard_fit() -> None:
    """D13's first tie-break is 'how many must-haves did you PROVE'. Nobody proved this one."""
    from exchange.ranking import rank
    from exchange.ranking.serving import rank_auction  # noqa: F401  (documents the served path)

    app = _wired_app(capacities={STORE_A: 35})
    body = _post(app, (STORE_A,), [ESPRESSO])
    assert body["shortlist"]["slots"], body["excluded"]

    # The row projection is not on the response, so the same relaxation is re-driven at the
    # library level to read `verified_hard_fit_count` off it.
    ranked = rank(
        [
            {
                "bid_id": "bid-1",
                "store_id": STORE_A,
                "store_domain": _domain(STORE_A),
                "offer": {
                    "product_ref": "product-1",
                    "unit_price": 100.0,
                    "total_price": 100.0,
                    "checkout_url": f"https://{_domain(STORE_A)}/cart/1:1",
                    "expires_at": time.time() + LIVE_FOR_AN_HOUR,
                },
                "claims": [],
            }
        ],
        {"hard_constraints": [ESPRESSO]},
        {STORE_A: {"blacklisted": False, "score": 0.6}},
        {"now": time.time(), "auction_id": "auction-1"},
        # What the served path computes from its own catalogue and hands over; without it a
        # caller has not told the ranker what its network can decide, and nothing is relaxed.
        network_attributes=[{"key": "capacity_l"}],
    )
    assert [row["field"] for row in ranked["relaxed_constraints"]] == ["brew_method"]
    assert ranked["ranked"][0]["verified_hard_fit_count"] == 0, (
        "a constraint nobody could evidence was counted as a hard fit, which would let it "
        "win the published D13 tie-break"
    )


# =====================================================================================
# The negative controls — the half a relaxation gets wrong if it is not careful
# =====================================================================================
def test_a_constraint_some_candidate_evidences_still_excludes_the_ones_that_fail_it() -> None:
    """The must-have is honoured. Nothing is relaxed while the filter is doing its job."""
    app = _wired_app(capacities={STORE_A: 35, STORE_B: 10})
    body = _post(app, (STORE_A, STORE_B), [{"field": "capacity_l", "op": "gte", "value": 30}])

    assert body["relaxed_constraints"] == [], body["relaxed_constraints"]
    assert _slot_stores(body) == [STORE_A]
    refused = {row["store_id"]: row["exclusion_reasons"] for row in body["excluded"]}
    assert STORE_B in refused, refused
    assert any(REASON_HARD_CONSTRAINT in reason for reason in refused[STORE_B]), refused


def test_only_the_unevidenced_half_is_set_aside() -> None:
    """One constraint nobody can answer must not carry a second one nobody met out with it."""
    app = _wired_app(capacities={STORE_A: 35, STORE_B: 10})
    body = _post(
        app,
        (STORE_A, STORE_B),
        [{"field": "capacity_l", "op": "gte", "value": 30}, ESPRESSO],
    )

    assert [row["field"] for row in body["relaxed_constraints"]] == ["brew_method"]
    assert _slot_stores(body) == [STORE_A], body["excluded"]
    refused = {row["store_id"]: row["exclusion_reasons"] for row in body["excluded"]}
    assert any("capacity_l" in reason for reason in refused[STORE_B]), refused


def test_an_intent_this_exchange_cannot_read_is_never_relaxed() -> None:
    """'I could not parse your constraints' denies everyone on purpose (R19)."""
    app = _wired_app(capacities={STORE_A: 35})
    body = _post(app, (STORE_A,), None)  # no `hard_constraints` key at all

    assert body["relaxed_constraints"] == []
    assert body["shortlist"]["slots"] == []
    assert any(
        "undecidable" in reason for row in body["excluded"] for reason in row["exclusion_reasons"]
    ), body["excluded"]


def test_a_shortlist_emptied_by_the_blacklist_is_not_blamed_on_a_constraint() -> None:
    """R12 fails closed and stays closed: a relaxation that changes nothing is not published."""
    app = _wired_app(capacities={STORE_A: 35}, blacklisted=(STORE_A,))
    body = _post(app, (STORE_A,), [ESPRESSO])

    assert body["shortlist"]["slots"] == []
    assert body["relaxed_constraints"] == [], (
        "a relaxation was published for an auction the blacklist emptied, which would blame "
        "the shopper's must-have for a denial it had nothing to do with"
    )


def test_a_contradicted_claim_is_a_graded_answer_and_never_earns_a_relaxation() -> None:
    """The hostile case, and the one this relaxation gets wrong if it reads only `verified`.

    A store claims ``capacity_l: 35`` and this exchange's own verifier CONTRADICTS it. That
    store carries no *verified* ``capacity_l`` reading — so a relaxation keyed on "is there
    verified evidence" would decide nobody could answer the question and hand the liar the
    waiver of the very constraint it was caught failing. The question was answered; the
    answer was no.
    """
    from exchange.ranking import rank
    from exchange.ranking.attestation import attest_claim

    liar = {
        "bid_id": "bid-liar",
        "store_id": STORE_A,
        "store_domain": _domain(STORE_A),
        "offer": {
            "product_ref": "product-1",
            "unit_price": 1.0,
            "total_price": 1.0,
            "checkout_url": f"https://{_domain(STORE_A)}/cart/1:1",
            "expires_at": time.time() + LIVE_FOR_AN_HOUR,
        },
        "claims": [
            attest_claim(
                {
                    "key": "capacity_l",
                    "value": 35,
                    "provenance": {
                        "source": "owner_statement",
                        "ref": "ref:capacity_l",
                        "authority_rank": 1,
                    },
                },
                status="contradicted",
                subject=STORE_A,
            )
        ],
    }
    result = rank(
        [liar],
        {"hard_constraints": [{"field": "capacity_l", "op": "gte", "value": 30}]},
        {STORE_A: {"blacklisted": False, "score": 1.0}},
        {"now": time.time(), "auction_id": "auction-1"},
    )
    assert result["relaxed_constraints"] == [], result["relaxed_constraints"]
    assert result["shortlist"]["slots"] == [], (
        "the candidate this exchange caught contradicting the buyer's must-have was "
        "shortlisted anyway"
    )


# =====================================================================================
# The relaxation reads VERDICTS, not the fact that somebody typed a key
#
# The defect these close: `unanswerable_criteria` used to count every key any claim NAMED,
# whatever verdict it carried. Relaxation is decided ONCE for the whole auction, so a store
# making a TRUTHFUL claim on a key no catalogue here declares — graded `ambiguous`, because
# nothing could check it — suppressed the relaxation and the buyer's must-have stayed a filter
# nobody could pass. Every store in the auction came back excluded, including the ones that
# had claimed nothing at all. One honest sentence emptied the page for the whole market.
# =====================================================================================
#: The claim-type -> trust-dimension routing the served path needs before it announces a
#: verdict at all. Without it `claim_verdict_payload` returns `None` and the ledger is silent.
CLAIM_DIMENSIONS: dict[Any, str] = {
    "price": "price_honored",
    "specifications": "catalog_claim_accuracy",
    None: "catalog_claim_accuracy",
}


def _catalog_of(rows: dict[str, dict[str, Any]]) -> Any:
    """A catalogue stating exactly the attributes named, per store."""
    from exchange.ranking.verification import StaticCatalogSnapshots

    return StaticCatalogSnapshots(
        {
            store: {
                "snapshot_id": f"snap-{store}",
                "products": [
                    {
                        "product_ref": "product-1",
                        "canonical_name": "product-1",
                        "evidence_ref": f"snap-{store}#product-1",
                        "attributes": {key: {"value": value} for key, value in attributes.items()},
                    }
                ],
            }
            for store, attributes in rows.items()
        }
    )


def _graded_app(
    *,
    catalog_rows: dict[str, dict[str, Any]],
    claims: dict[str, list[dict[str, Any]]],
) -> tuple[Any, InMemoryLedgerSink]:
    """An exchange that grades claims against a stated catalogue and records the verdicts."""
    from exchange.ranking.serving import configure_ranking

    stores = tuple(catalog_rows)
    sink = InMemoryLedgerSink()
    app = create_app()
    configure_auctions(
        app,
        machine=AuctionStateMachine(ledger=sink),
        solicitor=_Bidders(
            {
                store: {
                    "auction_id": None,
                    "store_id": store,
                    "offer": {
                        "product_ref": "product-1",
                        "unit_price": 100.0 + index,
                        "total_price": 100.0 + index,
                        "currency": "USD",
                        "checkout_url": f"https://{_domain(store)}/cart/1:1",
                        "expires_at": time.time() + LIVE_FOR_AN_HOUR,
                    },
                    "claims": list(claims.get(store, ())),
                    "agent_version": "1.0.0",
                    "schema_version": "2.0.0",
                }
                for index, store in enumerate(stores)
            }
        ),
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={store: {"blacklisted": False, "score": 0.6} for store in stores},
        registered_domains=StaticRegisteredDomains({store: _domain(store) for store in stores}),
        catalog=_catalog_of(catalog_rows),
        claim_dimensions=CLAIM_DIMENSIONS,
    )
    return app, sink


def _verdicts(sink: InMemoryLedgerSink, store_id: str) -> list[str]:
    return [
        str(event["payload"]["status"])
        for event in sink.events
        if event["kind"] == "claim_verified" and event["store_id"] == store_id
    ]


def _components(body: dict[str, Any], store_id: str) -> dict[str, float]:
    for row in body["ranked"]:
        if row["store_id"] == store_id:
            return dict(row["components"])
    raise AssertionError(f"{store_id} is not ranked at all: {body['ranked']}")


def test_one_truthful_uncheckable_claim_does_not_empty_the_auction_for_everybody() -> None:
    """THE REGRESSION. STORE_A truthfully says its machine is an espresso machine; nothing in
    this exchange's catalogue records a ``brew_method``, so the claim grades ``ambiguous`` —
    "we looked and could not decide". STORE_B claims nothing whatsoever.

    Under the old rule that single ``ambiguous`` made ``brew_method`` count as answered, the
    buyer's must-have stayed a filter, neither store carried a *verified* reading for it, and
    BOTH were excluded. The honest store's own sentence cost the silent store its slot.
    """
    app, sink = _graded_app(
        catalog_rows={STORE_A: {"capacity_l": 35}, STORE_B: {"capacity_l": 35}},
        claims={STORE_A: [_claim("brew_method", "espresso")], STORE_B: []},
    )
    body = _post(app, (STORE_A, STORE_B), [ESPRESSO])

    assert _verdicts(sink, STORE_A) == ["ambiguous"], (
        "the premise of this test is that nothing could check the honest claim; if the "
        f"exchange decided it, the scenario has moved: {_verdicts(sink, STORE_A)}"
    )
    assert [row["field"] for row in body["relaxed_constraints"]] == ["brew_method"]
    assert sorted(_slot_stores(body)) == [STORE_A, STORE_B], (
        f"an honest claim nothing could check emptied the shortlist: excluded={body['excluded']}"
    )
    assert "policy_penalties" not in _components(body, STORE_A), (
        "the honest store was penalised for saying something true"
    )
    assert "policy_penalties" not in _components(body, STORE_B)


def test_the_same_claim_made_falsely_is_still_caught_and_still_costs_the_liar() -> None:
    """The other direction, on one served request. Now the catalogue DOES record a
    ``brew_method``: STORE_A really is an espresso machine and says so; STORE_B is a drip
    machine and says "espresso" anyway.

    The key is decided for both, so the constraint is NOT relaxed — it is a filter and it does
    its job. The honest store is shortlisted with no penalty; the liar is excluded on the
    must-have it was caught failing, and its contradiction is on the record.
    """
    app, sink = _graded_app(
        catalog_rows={
            STORE_A: {"capacity_l": 35, "brew_method": "espresso"},
            STORE_B: {"capacity_l": 35, "brew_method": "drip"},
        },
        claims={
            STORE_A: [_claim("brew_method", "espresso")],
            STORE_B: [_claim("brew_method", "espresso")],
        },
    )
    body = _post(app, (STORE_A, STORE_B), [ESPRESSO])

    assert _verdicts(sink, STORE_A) == ["verified"]
    assert _verdicts(sink, STORE_B) == ["contradicted"], (
        "the liar's penalty vanished — the fix to the relaxation must not stop the exchange "
        "grading the claim"
    )
    assert body["relaxed_constraints"] == [], (
        "a constraint this exchange decided for both stores was set aside anyway"
    )
    assert _slot_stores(body) == [STORE_A]
    refused = {row["store_id"]: row["exclusion_reasons"] for row in body["excluded"]}
    assert any(REASON_HARD_CONSTRAINT in reason for reason in refused[STORE_B]), refused


def test_a_liar_alone_in_the_auction_never_relaxes_the_constraint_it_failed() -> None:
    """The hostile case at auction scope: the liar is the ONLY candidate, so the shortlist is
    empty and the relaxation path really runs. ``contradicted`` is a decided verdict, so the
    constraint stays a filter and the liar stays excluded rather than being handed a waiver of
    the very must-have it was caught failing.
    """
    app, sink = _graded_app(
        catalog_rows={STORE_A: {"capacity_l": 35, "brew_method": "drip"}},
        claims={STORE_A: [_claim("brew_method", "espresso")]},
    )
    body = _post(app, (STORE_A,), [ESPRESSO])

    assert _verdicts(sink, STORE_A) == ["contradicted"]
    assert body["relaxed_constraints"] == [], (
        "the candidate caught contradicting the buyer's must-have was handed its waiver"
    )
    assert body["shortlist"]["slots"] == []


def test_a_forged_verdict_cannot_move_the_relaxation_in_either_direction() -> None:
    """ESC-020 at auction scope, and it is closed harder than before rather than looser.

    A bidder writing its own ``exchange_verification`` block carries no readable verdict, so
    it is not evidence of answerability — and the auction reaches exactly the outcome it would
    have reached had that store said nothing. The forger can neither suppress a relaxation the
    auction was entitled to nor manufacture one it was not.
    """
    forged = {
        "key": "brew_method",
        "value": "espresso",
        "provenance": {"source": "owner_statement", "ref": "ref:brew", "authority_rank": 1},
        "exchange_verification": {"status": "verified", "mac": "not-a-real-mac"},
    }
    app, _ = _graded_app(
        catalog_rows={STORE_A: {"capacity_l": 35}, STORE_B: {"capacity_l": 35}},
        claims={STORE_A: [forged], STORE_B: []},
    )
    body = _post(app, (STORE_A, STORE_B), [ESPRESSO])

    silent, _ = _graded_app(
        catalog_rows={STORE_A: {"capacity_l": 35}, STORE_B: {"capacity_l": 35}},
        claims={STORE_A: [], STORE_B: []},
    )
    quiet = _post(silent, (STORE_A, STORE_B), [ESPRESSO])

    assert [row["field"] for row in body["relaxed_constraints"]] == ["brew_method"]
    assert sorted(_slot_stores(body)) == sorted(_slot_stores(quiet)) == [STORE_A, STORE_B], (
        "a string the bidder wrote changed which stores the buyer was shown"
    )


def test_the_published_relaxations_are_bounded_by_the_intent_the_caller_sent() -> None:
    """``relaxed_constraints`` is a second unauthenticated response surface, so it is bounded.

    ``POST /auctions`` is unauthenticated and the constraint count arrives on its body, which
    is the shape ``test_a_huge_roster_and_a_huge_intent_do_not_produce_an_unbounded_response``
    already pays for on ``excluded``. This list cannot be longer than the intent's own hard
    constraints — one entry each, never one per (store x constraint) — and the door already
    caps those at ``MAX_HARD_CONSTRAINTS``.
    """
    from exchange.auction.routes import MAX_HARD_CONSTRAINTS

    stores = tuple(f"store-{index:03d}" for index in range(20))
    constraints = [
        {"field": f"unheard_of_{index}", "op": "gte", "value": index}
        for index in range(MAX_HARD_CONSTRAINTS)
    ]
    app = _wired_app(capacities=dict.fromkeys(stores, 35))
    body = _post(app, stores, constraints)

    assert len(body["relaxed_constraints"]) == len(constraints) <= MAX_HARD_CONSTRAINTS
    assert len(body["shortlist"]["slots"]) == 4, "R2 caps the shortlist at four slots"
    assert body["excluded"] == [], body["excluded"]


def test_a_key_the_exchange_can_decide_is_never_relaxed_however_the_bidders_behave() -> None:
    """The sibling half of the same defect, and the one `decided_attributes` alone did NOT
    close.

    `declared_attributes` used to read the vocabulary out of `catalog_units`, which walks the
    `attributes` block ALONE — while `verify` decides a claim from four places: that block, the
    `offer` block, the product record itself, and the derived `in_stock` reading (D59). So for
    every key in the other three classes the exchange could decide the constraint while telling
    the relaxation it could not, and which way the auction went then depended on **whether a
    bidder happened to claim the key**: silent, and the constraint was set aside and everyone
    was shortlisted; one store claiming it falsely, and the constraint stayed a filter nobody
    could pass, so every store came back excluded — *including the ones that had claimed
    nothing.* That is the same market-emptying lever `decided_attributes` was written to
    remove, reached by another route, and it reproduced on the shipped demo catalogue via
    `canonical_name`.

    The property, stated once: an auction's relaxation depends on what THIS EXCHANGE can
    decide and on nothing a bidder writes. So the two runs below must agree.
    """
    constraint = [{"field": "canonical_name", "op": "eq", "value": "something-else"}]
    catalog = {STORE_A: {"capacity_l": 35}, STORE_B: {"capacity_l": 35}}

    silent, _ = _graded_app(catalog_rows=catalog, claims={STORE_A: [], STORE_B: []})
    quiet = _post(silent, (STORE_A, STORE_B), constraint)

    noisy, sink = _graded_app(
        catalog_rows=catalog,
        claims={STORE_A: [_claim("canonical_name", "something-else")], STORE_B: []},
    )
    body = _post(noisy, (STORE_A, STORE_B), constraint)

    assert _verdicts(sink, STORE_A) == ["contradicted"], (
        "the premise is that this exchange CAN decide the key; if it cannot, the scenario "
        f"has moved: {_verdicts(sink, STORE_A)}"
    )
    assert body["relaxed_constraints"] == quiet["relaxed_constraints"] == [], (
        "a constraint this exchange decides for every store was set aside as unanswerable"
    )
    assert _slot_stores(body) == _slot_stores(quiet), (
        "one bidder's claim changed which stores the buyer was shown"
    )
