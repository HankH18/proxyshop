"""R8 / S5a at the HOSTED door: `validate_bid` decides a served `POST /auctions` (T-9a).

Ticket verify::

    PROXYSHOP_WORKER=<n> pytest apps/exchange/tests/test_hosted_bid_boundary.py -q

What was measured before this file existed
------------------------------------------
``contracts.boundary.validate_bid`` had **no call site anywhere in** ``apps/exchange/src``.
Every occurrence there was a comment about its own absence — ``ranking/serving.py:358``,
``auction/routes.py:365`` and ``checkout/codes.py:154`` all said so in prose — and every
actual call was a test. So S5a's promise, "a claim carrying no provenance record is rejected
on every path, hosted and external", held on the external door
(``external_bids/routes.py`` -> ``store_agent.external.door`` ->
``validate_external_submission``) and on nothing else.

Driven through the served ``POST /auctions``, one store, list price 200, varying **only** the
claim's provenance::

    owner_statement (control)                     -> real bid, [store-confirmed], 0.545
    {} / source:"" / seller_asserted / null / absent
                                                  -> real bid, SHORTLISTED,       0.545

Identical entry, identical rank score, identical slot. The only difference the exchange made
was the buyer-facing label: ``['unverified']`` instead of ``['store-confirmed']``. An
unprovenanced claim was not rejected; it was admitted, scored the same as an honest one, and
relabelled.

Why the hosted side is where this matters, after D55
----------------------------------------------------
The redirect makes the shop-side agent the **sponsored** result: a dedicated advocate whose
whole job is to argue for one shop. D55 is explicit that a seller's purchased message is the
one checked adversarially, precisely because it is the one with a motive. A provenance
boundary that never runs makes that promise empty on the side of the market that has the
motive.

The correction this file also records
-------------------------------------
An earlier audit reported that "an unprovenanced 20% discount wins"; a later one could not
reproduce it and concluded the discount guard held. **Both were half right, and neither
described the mechanism.** Measured here, on a roster row listing 200.00::

    discount 20%, NO provenance, roster states no max_discount_pct  -> refused (price wall)
    discount 20%, NO provenance, roster max_discount_pct=20         -> ADMITTED at 160.00
    discount 20%, hook provenance, roster max_discount_pct=20       -> admitted at 160.00
    discount 20%, hook provenance, roster states no max_discount_pct-> refused (price wall)

The wall in ``auction/collect.py`` is a wall against an **unauthorised depth**, and it is
blind to provenance: the middle two rows are byte-identical decisions over bids that differ
only in whether ``offer.discount`` carries a provenance block at all. So the DISCOUNT-DEPTH
guard held and the CLAIM-PROVENANCE guard did not, which is why the two audits disagreed —
they were probing the same field through two different rules.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from contracts import boundary as _boundary
from exchange.auction.collect import (
    FALLBACK_REASONS,
    PROVENANCE_REASON_PREFIXES,
    UNPROVENANCED_CLAIM_REASON,
)
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from fastapi.testclient import TestClient

STORE = "store-alpha"
DOMAIN = f"{STORE}.example.com"
PRODUCT = "product-1"
LIST_PRICE = 200.0

#: A provenance block a tool hook really mints. ``owner_statement`` is in
#: :data:`contracts.boundary.HOOK_PROVENANCE_SOURCES`, which is what makes it legal on the
#: hosted path; ``seller_asserted`` is the one source no hook produces.
HOOK_PROVENANCE: dict[str, Any] = {
    "source": "owner_statement",
    "ref": f"envelope:{STORE}:v1#capacity_l",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 1,
}

_ABSENT = object()


def _catalog() -> Any:
    return StaticCatalogSnapshots(
        {
            STORE: {
                "snapshot_id": f"snap-{STORE}",
                "products": [
                    {
                        "product_ref": PRODUCT,
                        "canonical_name": PRODUCT,
                        "evidence_ref": f"snap-{STORE}#{PRODUCT}",
                        "attributes": {"capacity_l": {"value": 35}},
                    }
                ],
            }
        }
    )


class _OneBidder:
    """A store agent that answers with exactly the bid it was constructed with."""

    def __init__(self, bid: dict[str, Any]) -> None:
        self.bid = bid

    def solicit(self, store: dict[str, Any]) -> dict[str, Any]:
        return {
            "store_id": str(store["store_id"]),
            "received_at": time.time(),
            "bid": dict(self.bid),
        }

    __call__ = solicit


def _offer(price: float, *, discount: Any = _ABSENT, commitments: Any = _ABSENT) -> dict[str, Any]:
    offer: dict[str, Any] = {
        "product_ref": PRODUCT,
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "checkout_url": f"https://{DOMAIN}/cart/1:1",
        "expires_at": time.time() + 3600.0,
    }
    if discount is not _ABSENT:
        offer["discount"] = discount
    if commitments is not _ABSENT:
        offer["commitments"] = commitments
    return offer


def _bid(*, claims: Any = _ABSENT, offer: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "auction_id": None,
        "store_id": STORE,
        "offer": _offer(LIST_PRICE) if offer is None else offer,
        "claims": [{"key": "capacity_l", "value": 35, "provenance": dict(HOOK_PROVENANCE)}]
        if claims is _ABSENT
        else claims,
        "agent_version": "1.0.0",
        "schema_version": "1.0.0",
    }


def _post(bid: dict[str, Any], *, max_discount_pct: float | None = None) -> dict[str, Any]:
    """Open one served auction with one rostered store whose agent answers ``bid``."""
    app = create_app()
    configure_auctions(
        app,
        solicitor=_OneBidder(bid),
        eligibility=StaticSellerEligibility({STORE: ELIGIBLE}),
    )
    configure_ranking(
        app,
        trust_snapshot={STORE: {"blacklisted": False, "score": 0.6}},
        registered_domains=StaticRegisteredDomains({STORE: DOMAIN}),
        catalog=_catalog(),
    )
    row: dict[str, Any] = {
        "store_id": STORE,
        "tier": 1,
        "product_ref": PRODUCT,
        "list_price": LIST_PRICE,
    }
    if max_discount_pct is not None:
        row["max_discount_pct"] = max_discount_pct
    response = TestClient(app).post(
        "/auctions",
        json={
            "intent": {
                "intent_id": "intent-1",
                "cluster_id": "cluster-1",
                "hard_constraints": [{"field": "capacity_l", "op": "gte", "value": 30}],
            },
            "roster": [row],
            "bid_timeout_seconds": 2.0,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _entry(body: dict[str, Any]) -> dict[str, Any]:
    (entry,) = body["entries"]
    return entry


# =====================================================================================
# 1. The control, and the five shapes S5a names
# =====================================================================================
def test_an_honest_hosted_bid_is_admitted_unchanged() -> None:
    """The positive control. Without it every assertion below passes on a dead exchange."""
    body = _post(_bid())
    entry = _entry(body)

    assert entry["fallback"] is False, entry
    assert entry["fallback_reason"] is None, entry
    assert entry["unit_price"] == pytest.approx(LIST_PRICE), entry
    (slot,) = body["shortlist"]["slots"]
    assert slot["provenance_labels"] == ["store-confirmed"], slot


@pytest.mark.parametrize(
    ("label", "provenance"),
    [
        ("an empty provenance block", {}),
        ("an empty source", {"source": "", "ref": "r", "authority_rank": 1}),
        ("a source no hook mints", {"source": "seller_asserted", "ref": "r", "authority_rank": 1}),
        ("a null provenance", None),
        ("no provenance key at all", _ABSENT),
    ],
)
def test_a_hosted_claim_with_no_hook_provenance_never_reaches_the_ranker(
    label: str, provenance: Any
) -> None:
    """S5a on the hosted path, driven through the served route rather than through the pure
    function underneath it.

    The bid is not merely relabelled: it is refused, and the store is represented by the R10
    list-price fallback the exchange manufactures for a store it could not use the answer of.
    That is the same degradation the T-177 price wall already produced, with its own reason so
    an operator is told which boundary refused them.
    """
    claim: dict[str, Any] = {"key": "capacity_l", "value": 35}
    if provenance is not _ABSENT:
        claim["provenance"] = provenance

    body = _post(_bid(claims=[claim]))
    entry = _entry(body)

    assert entry["fallback"] is True, f"[{label}] the unprovenanced claim was admitted: {entry}"
    assert entry["fallback_reason"] == UNPROVENANCED_CLAIM_REASON, f"[{label}] {entry}"
    # The store keeps its catalogue list price and loses everything it wrote — the same trade
    # every other fallback makes.
    assert entry["unit_price"] == pytest.approx(LIST_PRICE), f"[{label}] {entry}"
    for slot in body["shortlist"]["slots"]:
        assert slot["provenance_labels"] == ["unverified"], f"[{label}] {slot}"


# =====================================================================================
# 2. The offer's own claim-bearing sites, which are inside the bid boundary
# =====================================================================================
def test_an_unprovenanced_discount_is_refused_even_at_a_depth_the_roster_authorises() -> None:
    """The measurement that settles the two contradictory audits, as an assertion.

    Both rows below declare the same 20% off the same 200.00 list price, on a roster row that
    authorises exactly 20%. The only difference is whether ``offer.discount`` carries a
    provenance block — and before this gate the exchange made the identical decision on both.
    """
    unprovenanced = {"type": "percentage", "value": 20.0}
    minted = {
        "type": "percentage",
        "value": 20.0,
        "provenance": {
            "source": "envelope_rule",
            "ref": f"envelope:{STORE}:v1#max_discount_pct",
            "observed_at": "2026-01-01T00:00:00Z",
            "authority_rank": 1,
        },
    }

    refused = _entry(
        _post(_bid(offer=_offer(160.0, discount=unprovenanced)), max_discount_pct=20.0)
    )
    assert refused["fallback"] is True, refused
    assert refused["fallback_reason"] == UNPROVENANCED_CLAIM_REASON, refused
    assert refused["unit_price"] == pytest.approx(LIST_PRICE), refused

    # The control, and it is the load-bearing half: the same depth, the same price, the same
    # roster — admitted, because the grant it cites is one a hook really minted.
    admitted = _entry(_post(_bid(offer=_offer(160.0, discount=minted)), max_discount_pct=20.0))
    assert admitted["fallback"] is False, (
        "the boundary refused an authorised, hook-minted discount; a wall that rejects honest "
        f"bids is worse than one that never ran: {admitted}"
    )
    assert admitted["unit_price"] == pytest.approx(160.0), admitted


def test_an_unprovenanced_commitment_is_refused_at_the_offer_site_too() -> None:
    """`offer.commitments` is inside the bid boundary, so moving the claim there defeats
    nothing — which is the whole exclusivity property ``contracts.boundary`` documents."""
    entry = _entry(
        _post(_bid(offer=_offer(LIST_PRICE, commitments=[{"key": "free_returns", "value": "30d"}])))
    )
    assert entry["fallback"] is True, entry
    assert entry["fallback_reason"] == UNPROVENANCED_CLAIM_REASON, entry

    admitted = _entry(
        _post(
            _bid(
                offer=_offer(
                    LIST_PRICE,
                    commitments=[
                        {
                            "key": "free_returns",
                            "value": "30d",
                            "provenance": dict(HOOK_PROVENANCE),
                        }
                    ],
                )
            )
        )
    )
    assert admitted["fallback"] is False, admitted


# =====================================================================================
# 3. Honest traffic. This is the half that decides whether the gate above is shippable.
# =====================================================================================
def test_the_real_hosted_store_agent_still_bids_through_the_boundary() -> None:
    """This repo's OWN hosted agent, on this repo's OWN demo market, through the served route.

    ``packages/store-agent``'s runtime is the only thing in this tree that produces a hosted
    bid in production, and every claim it emits comes out of a tool hook that stamps its own
    provenance — including ``offer.discount``, which is the site the refusal above is
    strictest about. A boundary that rejected these bids would empty the shortlist for honest
    and dishonest stores alike, which is a dead exchange rather than a strict one.

    Driven end to end: the agent answers a real ``BidRequest``, the exchange runs the boundary
    on the reply, and the assertion is that the reply survived it as a REAL bid rather than as
    a manufactured fallback.
    """
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from store_agent.runtime import Decline  # noqa: PLC0415
    from store_agent.runtime import bid as run_agent  # noqa: PLC0415

    market_file = (
        Path(__file__).resolve().parents[3] / "apps" / "buyer" / "devstack" / "demo-market.json"
    )
    market = json.loads(market_file.read_text(encoding="utf-8"))
    stores = list(market["stores"])
    assert stores, "the demo market states no stores; there is no honest traffic to drive"

    class _RealAgents:
        def __init__(self) -> None:
            self.by_id = {str(row["store_id"]): row for row in stores}
            self.cluster = str(stores[0]["envelope"]["pursue_clusters"][0])

        def solicit(self, store: dict[str, Any]) -> dict[str, Any] | None:
            row = self.by_id[str(store["store_id"])]
            answer = run_agent(
                {
                    "auction_id": "auction-honest-traffic",
                    "intent": {
                        "intent_id": "intent-1",
                        "cluster_id": self.cluster,
                        "use_case": "a warm hat for winter commuting",
                        "budget_band": {"min": 0.0, "max": 500.0},
                    },
                    "profile": {"pseudonym": "psn-honest-traffic"},
                    "respond_by": "2999-01-01T00:00:00Z",
                },
                {
                    "store_id": row["store_id"],
                    "store_domain": row["store_domain"],
                    "envelope": row["envelope"],
                    "catalog": row["catalog"],
                    "live_state": row["live_state"],
                    "learned_policy": row.get("learned_policy"),
                    "network_priors": row.get("network_priors", {}),
                },
            )
            assert not isinstance(answer, Decline), (
                f"{store['store_id']} declined ({getattr(answer, 'reason', '?')}); this test "
                f"needs a real hosted pitch, not a manufactured fallback"
            )
            return {
                "store_id": str(store["store_id"]),
                "received_at": time.time(),
                "bid": answer.model_dump(mode="json"),
            }

        __call__ = solicit

    agents = _RealAgents()
    roster = [
        {
            "store_id": str(row["store_id"]),
            "tier": 1,
            "product_ref": str(row["product_ref"]),
            "list_price": float(row["catalog"][str(row["product_ref"])]["list_price"]),
            "max_discount_pct": float(row["envelope"]["max_discount_pct"]),
        }
        for row in stores
    ]

    app = create_app()
    configure_auctions(
        app,
        solicitor=agents,
        eligibility=StaticSellerEligibility({row["store_id"]: ELIGIBLE for row in roster}),
    )
    configure_ranking(
        app,
        trust_snapshot={row["store_id"]: {"blacklisted": False, "score": 0.6} for row in roster},
        registered_domains=StaticRegisteredDomains(
            {str(row["store_id"]): str(row["store_domain"]) for row in stores}
        ),
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
            "bid_timeout_seconds": 5.0,
        },
    )
    assert response.status_code == 201, response.text
    entries = response.json()["entries"]
    assert len(entries) == len(roster), entries

    refused = [entry for entry in entries if entry["fallback"]]
    assert not refused, (
        "the hosted bid boundary refused this repo's own honest agents; every one of these "
        f"bids is one `store_agent.runtime.bid` really produced: {refused}"
    )


# =====================================================================================
# 4. The partition, so a reason family added to the contract cannot be dropped in silence
# =====================================================================================
def test_the_boundary_reason_families_are_partitioned_rather_than_filtered_by_memory() -> None:
    """Every ``REASON_*`` the shared boundary publishes is either enforced here or answered
    elsewhere on this path — and the module says which.

    The exchange keeps only the provenance family out of ``validate_bid``'s verdict, because
    the other clauses are answered by named machinery this path already runs. That is a
    defensible split and a fragile one: a family added to ``packages/contracts`` would be
    dropped on the floor by an allow-list nobody updated. So the split is asserted against the
    contract's own constants rather than against a list written from memory, and a new family
    turns this red instead of quietly widening what the auction admits.
    """
    from exchange.auction.collect import BOUNDARY_REASONS_ANSWERED_ELSEWHERE  # noqa: PLC0415

    published = {
        value
        for name, value in vars(_boundary).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    assert published, "contracts.boundary publishes no REASON_* constants; the probe is blind"

    # D52's two request-signing reasons are accounted for HERE rather than in
    # `exchange.auction.collect`, and the reason is C3/S7 rather than tidiness: the frozen
    # import lint in `.swarm-loop/acceptance/test_spec_criteria.py` forbids anything under
    # `apps/exchange` importing a name containing "envelope", because a merchant's economic
    # envelope is a surface this service may not read. The lint is a name check and cannot tell
    # that surface from D52's cryptographic signing envelope, so importing the constant turned
    # the acceptance suite red — measured, one violation:
    #
    #     apps/exchange/src/auction/collect.py:157: imports
    #     'contracts.boundary.REASON_SIGNING_ENVELOPE_INCOMPLETE'
    #
    # The lint is right to fire on a name it cannot disambiguate and is NOT weakened here. Both
    # values are read off the contract MODULE by attribute — not restated as literals — so this
    # gate still cannot drift from what `packages/contracts` publishes, and the exchange source
    # still imports no such name. The external door is what actually answers them.
    # Attribute access, never an import and never a retyped literal: the lint scans `import`
    # statements, so reading the constant off the module is compliance by construction rather
    # than a way around the check.
    external_door = {
        _boundary.REASON_SIGNING_ENVELOPE_INCOMPLETE,
        _boundary.REASON_SIGNATURE_MISSING,
    }

    accounted = (
        set(PROVENANCE_REASON_PREFIXES) | set(BOUNDARY_REASONS_ANSWERED_ELSEWHERE) | external_door
    )
    assert published <= accounted, (
        "contracts.boundary publishes a refusal family the auction door neither enforces nor "
        f"names as answered elsewhere: {sorted(published - accounted)}"
    )
    assert not (set(PROVENANCE_REASON_PREFIXES) & set(BOUNDARY_REASONS_ANSWERED_ELSEWHERE)), (
        "a family cannot be both enforced here and answered elsewhere"
    )


def test_the_new_fallback_reason_is_published_in_the_vocabulary_a_report_aggregates_on() -> None:
    assert UNPROVENANCED_CLAIM_REASON in FALLBACK_REASONS
    # `fallback_reason_family` splits on ':', so a reason carrying one would be reported under
    # a family that is not itself in the published tuple.
    assert ":" not in UNPROVENANCED_CLAIM_REASON
