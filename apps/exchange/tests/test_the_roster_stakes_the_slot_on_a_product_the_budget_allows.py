"""A SHOP GETS ONE SLOT, AND THE ROSTER SPENT IT ON A CHAIR THE SHOPPER COULD NOT AFFORD.

The regression, on ``POST /auctions`` with ``EXCHANGE_SHOP_ROSTER=graph``, no ``roster`` in the
body, and a stated ceiling — driven against the live nineteen-store demo graph (98,001 nodes)
through the production composition root::

    "an office chair"                       2 slots
      www.branchfurniture.com  Verve Chair                        $599
      floydhome.com            Haworth(R) Fern(TM) Chair w/ Sisu  $1750

    "an office chair"  price_usd lte 500    0 slots   (125 products considered, 2 shops)
      www.branchfurniture.com  offer_price_outside_budget: 599.0 above the ceiling
      floydhome.com            offer_price_outside_budget: 1750.0 above the ceiling

**Both refusals are correct and neither is the defect.** The defect is that
branchfurniture.com was staked on the $599 Verve Chair while the SAME 125-row retrieval window
held its Multitask Chair at $279, its Open Box Task Chair at $239, its Studio Chair at $249,
its Open Box Ergonomic at $287 and its Conference Chair at $319 — six chairs the shopper could
afford, from the shop that just served them a blank page. Read off the live graph, the fits are
0.008 apart::

    fit 0.613  prod_d68e5e59cefcae819ff4d7e2f9bd9668  "Verve Chair"      $599  OVER the ceiling
    fit 0.608  prod_0782b6c26ff69fca4e75b7180169a068  "Multitask Chair"  $279  under it

``retrieval.roster._solicited`` chose by fit alone::

    product_ref = max(keepable or eligible, key=lambda pid: (fit[pid], pid))

and the row then met a rule it had not been chosen against — the same shape as the organic-gate
defect ``keeps`` was added for, one gate further down. ``ranking.filters.budget_reasons`` judges
the row's own offer against the buyer's bound, correctly cuts $599, and the shop dies holding a
chair BOTH layers would have kept.

**Why this file exists as its own repair and could not have been written before.** Until
``HardCriterion.pushdown`` and ``RetrievalQuery.exclusion_reasons`` stopped treating a money
bound as a product attribute, EVERY ceiling returned zero products, so no shop was ever staked
on anything under one and this was unreachable. Closing that defect is what made this one
observable — measured on the live graph with that fix in and this one out, ``"an office chair"``
under $500 answers 2 shops, 125 considered, **0 slots**.

**What this file does NOT change, and the guard is section 2.** A shop with nothing affordable
must still be rostered on its plain best product, still be REPRESENTED in ``entries`` (R10), and
still be refused at the shortlist WITH A REASON. floydhome carries one chair at $1,750 and no
other; dropping it here would empty the same screen while deleting the sentence that explains
it. And WHICH shops make the roster is decided on the shop's best ELIGIBLE fit, never on the
re-pointed row's, so narrowing a shop's product choice cannot push the shop off the cut — the
hazard ``solicit``'s own comment records.

**What is real here and what is a double.** The route, ``GraphShopRoster.solicit``,
``_solicited``, ``collect_bids``' list-price fallback, ``rank_auction``, ``budget_reasons`` and
the shortlist builder are all the shipped code. Exactly two things stand in for a Neo4j:
``GraphCandidateSource`` and ``candidate_shops``. The three products are quoted verbatim off the
live demo graph, and ``similarity`` is chosen to reproduce the ORDER the live fits have — Verve
above Multitask — because which product a shop is staked on is decided by the order alone.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.reasons import REASON_OVER_BUDGET
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from exchange.retrieval import InMemoryCandidateSource
from exchange.retrieval.roster import DEFAULT_SOLICITED_SHOPS, GraphShopRoster
from fastapi.testclient import TestClient
from ingest.graph import ShopCandidate, ShopOffer

CHAIR = "an office chair"

#: The ceiling the live drive used. Above four of branchfurniture's chairs and below its
#: best-fitting one, which is the whole shape of the defect.
CEILING = 500.0

VERVE = {
    "store": "www.branchfurniture.com",
    "product_id": "prod_d68e5e59cefcae819ff4d7e2f9bd9668",
    "canonical_name": "Verve Chair",
    "brand": "Branch",
    "attributes": [
        {"key": "size", "value_string": "Standard"},
        {"key": "color", "value_string": "Lunar"},
        {"key": "color", "value_string": "Wheat"},
    ],
    "variant_names": ("Lunar / Standard", "Wheat / Standard", "Cobalt / Standard"),
    "price": 599.0,
    "similarity": 0.30,
}
MULTITASK = {
    "store": "www.branchfurniture.com",
    "product_id": "prod_0782b6c26ff69fca4e75b7180169a068",
    "canonical_name": "Multitask Chair",
    "brand": "Branch",
    "attributes": [
        {"key": "color", "value_string": "Quarry"},
        {"key": "color", "value_string": "Black"},
    ],
    "variant_names": ("Quarry", "Black"),
    "price": 279.0,
    "similarity": 0.28,
}
#: floydhome's ONE chair, and the reason section 2 is not hypothetical: there is nothing under
#: the ceiling for this shop to be re-pointed onto.
FERN = {
    "store": "floydhome.com",
    "product_id": "prod_33e481a2cf21b9ed7d925d8dcc828e0c",
    "canonical_name": "Haworth® Fern™ Chair with Sisu",
    "brand": "Haworth",
    "attributes": [
        {"key": "base-material", "value_string": "Black"},
        {"key": "color", "value_string": "Blue/Red"},
    ],
    "variant_names": ("Blue/Red / Black", "Black/White / Polished"),
    "price": 1750.0,
    "similarity": 0.26,
}

CRAWL: tuple[dict[str, Any], ...] = (VERVE, MULTITASK, FERN)


def _records(crawl: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [
        {
            "product_id": row["product_id"],
            "canonical_name": row["canonical_name"],
            "brand": row["brand"],
            "status": "active",
            "similarity": row["similarity"],
            "categories": [],
            "attributes": list(row["attributes"]),
            "variant_names": tuple(row["variant_names"]),
        }
        for row in crawl
    ]


def _stores(crawl: tuple[dict[str, Any], ...]) -> list[str]:
    seen: list[str] = []
    for row in crawl:
        if row["store"] not in seen:
            seen.append(row["store"])
    return seen


def _shop_candidates(crawl: tuple[dict[str, Any], ...]) -> list[ShopCandidate]:
    shops: list[ShopCandidate] = []
    for store in _stores(crawl):
        rows = [row for row in crawl if row["store"] == store]
        shops.append(
            ShopCandidate(
                store_id=store,
                domain=store,
                business_identity=store,
                tier=0,
                product_ids=[row["product_id"] for row in rows],
                offers=[
                    ShopOffer(
                        offer_id=f"offer-{row['product_id']}",
                        product_id=row["product_id"],
                        variant_id=f"variant-{row['product_id']}",
                        price=float(row["price"]),
                        currency="USD",
                        availability="in_stock",
                        observed_at="2026-01-01T00:00:00Z",
                        source_ids=[f"src-{row['product_id']}"],
                        native_variant_id=f"native-{row['product_id']}",
                    )
                    for row in rows
                ],
                via=["SELLS"],
                source_ids=[f"src-{store}"],
                best_score=max((1.0 + float(row["similarity"])) / 2.0 for row in rows),
            )
        )
    return shops


def _graph_roster(
    monkeypatch: pytest.MonkeyPatch,
    crawl: tuple[dict[str, Any], ...],
    *,
    limit: int = DEFAULT_SOLICITED_SHOPS,
) -> GraphShopRoster:
    """A real :class:`GraphShopRoster` over doubles for the only two things needing a Neo4j."""
    records = _records(crawl)
    monkeypatch.setattr(
        "exchange.retrieval.roster.GraphCandidateSource",
        lambda session, **kwargs: InMemoryCandidateSource(records),
    )
    monkeypatch.setattr(
        "exchange.retrieval.roster.candidate_shops",
        lambda session, **kwargs: _shop_candidates(crawl),
    )

    @contextmanager
    def sessions():
        yield object()

    return GraphShopRoster(sessions, limit=limit)


def _intent(constraints: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "intent_id": "intent-chair",
        "cluster_id": "cluster-furniture",
        "query": CHAIR,
        "hard_constraints": constraints,
        "preferences": [],
        "currency": "USD",
        "budget_band": "unspecified",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }


def _ceiling(value: float = CEILING) -> list[dict[str, Any]]:
    return [{"field": "price_usd", "op": "lte", "value": value}]


def _app(monkeypatch: pytest.MonkeyPatch, crawl: tuple[dict[str, Any], ...] = CRAWL):
    """The shipped exchange, its shop roster reading the graph, and nobody bidding."""
    stores = _stores(crawl)
    app = create_app()
    configure_auctions(
        app,
        solicitor=lambda store: None,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
        shop_roster=_graph_roster(monkeypatch, crawl),
    )
    configure_ranking(
        app,
        trust_snapshot={
            store: {"blacklisted": False, "score": 0.7, "confidence": 0.7} for store in stores
        },
        registered_domains=StaticRegisteredDomains({store: store for store in stores}),
        catalog=StaticCatalogSnapshots(
            {
                store: {
                    "snapshot_id": f"snap-{store}",
                    "products": [
                        {
                            "product_ref": row["product_id"],
                            "canonical_name": row["canonical_name"],
                            "brand": row["brand"],
                            "evidence_ref": f"snap-{store}#{row['product_id']}",
                            "observed_at": "2026-01-01T00:00:00Z",
                            "attributes": {},
                        }
                        for row in crawl
                        if row["store"] == store
                    ],
                }
                for store in stores
            }
        ),
    )
    return app


def _serve(app, constraints: list[dict[str, Any]]) -> dict[str, Any]:
    posted = TestClient(app).post(
        "/auctions", json={"intent": _intent(constraints), "bid_timeout_seconds": 2.0}
    )
    assert posted.status_code == 201, posted.text
    return dict(posted.json())


def _priced(body: dict[str, Any]) -> dict[str, float]:
    return {
        ((slot.get("product") or {}).get("identity") or {}).get("title"): (slot.get("price") or {})[
            "unit_price"
        ]
        for slot in body["shortlist"]["slots"]
    }


# =====================================================================================
# 1. THE REGRESSION — a shop is staked on a product the shopper can actually buy
# =====================================================================================
def test_a_shop_is_rostered_on_a_product_the_budget_wall_will_actually_keep() -> None:
    """Red before the repair with ``slots == 0``: branchfurniture staked on the $599 Verve.

    The shop carries a $279 chair 0.008 of fit below the one it was staked on, and the shopper
    was shown a blank page instead.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch), _ceiling())

    served = _priced(body)
    assert served == {"Multitask Chair": 279.0}, (
        f"a shop was staked on its best-fitting product without asking whether the shopper "
        f"could afford it, and the one it could afford was in the same window. "
        f"shortlist={served} excluded={body['excluded']}"
    )
    assert all(price <= CEILING for price in served.values()), served


def test_the_shop_is_staked_on_the_affordable_product_before_any_ranking_runs() -> None:
    """The same decision one layer down, so a green shortlist cannot be the ranker's doing.

    ``solicit`` is asked directly: branchfurniture's roster row must name the Multitask Chair
    and carry ITS price and ITS variant. The price, the variant and the product are one
    observation — a row quoting the Verve's price for the Multitask would price the wrong thing.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        roster = _graph_roster(monkeypatch, CRAWL)
        solicited = roster.solicit(_intent(_ceiling()))

    rows = {shop.store_id: shop for shop in solicited.shops}
    assert set(rows) == {"www.branchfurniture.com", "floydhome.com"}, solicited
    branch = rows["www.branchfurniture.com"]
    assert branch.product_ref == MULTITASK["product_id"], solicited
    assert branch.list_price == 279.0, branch
    assert branch.variant_ref == f"native-{MULTITASK['product_id']}", branch
    assert branch.intent_match == pytest.approx(
        next(a.fit_score for a in solicited.fit if a.product_id == branch.product_ref)
    ), (
        "intent_match is the fit of the product this shop was ACTUALLY staked on — a row "
        "carrying the unaffordable product's higher score would publish a measurement of "
        "something the shopper is not being shown"
    )


def test_with_no_ceiling_the_shop_is_still_staked_on_its_best_fitting_product() -> None:
    """The control. Nothing about a budget-free auction changes, and this is what says so."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch), [])

    assert _priced(body) == {
        "Verve Chair": 599.0,
        "Haworth® Fern™ Chair with Sisu": 1750.0,
    }, _priced(body)


# =====================================================================================
# 2. THE FALLBACK — a shop with nothing affordable is rostered exactly as before
# =====================================================================================
def test_a_shop_with_nothing_affordable_is_still_rostered_and_refused_with_a_reason() -> None:
    """R10: representation is not conditional on winning. Green before this change and after.

    floydhome carries one chair at $1,750 and nothing else. It must still reach ``entries``,
    still be refused at the shortlist, and the refusal must SAY the price — a shop that
    disappears with no sentence anywhere is strictly worse than one that is refused with one.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch), _ceiling())

    represented = {row["store_id"] for row in body["entries"]}
    assert "floydhome.com" in represented, body["entries"]

    refusals = [
        reason
        for row in body["excluded"]
        if row["store_id"] == "floydhome.com"
        for reason in row["exclusion_reasons"]
        if reason.startswith(REASON_OVER_BUDGET)
    ]
    assert len(refusals) == 1, body["excluded"]
    assert "1750.0" in refusals[0], refusals


def test_the_shop_with_nothing_affordable_keeps_its_plain_best_product() -> None:
    """The fallback names the shop's best fit, unchanged — not nothing, and not a cheaper miss."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        roster = _graph_roster(monkeypatch, CRAWL)
        solicited = roster.solicit(_intent(_ceiling()))

    floyd = {shop.store_id: shop for shop in solicited.shops}["floydhome.com"]
    assert floyd.product_ref == FERN["product_id"], solicited
    assert floyd.list_price == 1750.0, floyd


# =====================================================================================
# 3. THE CUT — narrowing a shop's product must not cost the shop its place
# =====================================================================================
def test_a_shop_staked_on_a_cheaper_product_keeps_the_roster_place_its_best_fit_earned() -> None:
    """``solicit`` cuts to ``limit``, so the two questions have to take two numbers.

    Which shops are rostered is settled on each shop's best ELIGIBLE fit; only WHICH product
    each carries is settled by what the shopper can afford. Sorting the cut by the re-pointed
    fit instead would let a budget bound push a shop off the roster entirely — reaching neither
    ``entries`` nor ``excluded``, with no reason written anywhere. With ``limit=1`` the shop
    holding the best-matching chair either shop has is branchfurniture, and it must be the one
    that survives even though it is now staked on its second-best product.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        roster = _graph_roster(monkeypatch, CRAWL, limit=1)
        solicited = roster.solicit(_intent(_ceiling()))

    assert [shop.store_id for shop in solicited.shops] == ["www.branchfurniture.com"], solicited
    assert solicited.shops[0].product_ref == MULTITASK["product_id"], solicited


# =====================================================================================
# 4. THE SUBSTITUTION HAS TO BE SAYABLE — a swap the shopper cannot see is not a repair
# =====================================================================================
# Section 1 is the repair and this is its bill. `_solicited` spends a shop's ONE row on a
# different product than the one that best answers the question, and the served response says
# nothing: `roster_source.reason` is `null`, the slot carries the substitute's title and price
# as if the shop had nothing else, and the sentence that would have appeared had the shop been
# refused — `offer_price_outside_budget`, naming the Verve at $599 — is never written, because
# the refusal never happens.
#
# The sibling rule in the same module already answers this. `repoint_organic_products` moves a
# STATED row onto a different product and publishes "...the platform re-pointed N of M row(s)
# onto the product its own crawl says answers this intent... (nutricost.com, toniiq.com)". Both
# are the platform choosing a product the caller did not, and only one of them says so.
#
# Measured on the live nineteen-store graph through `GraphShopRoster.solicit`, with the twelve
# intents `POST /buyer/intent/clarify` produced for twelve shopper sentences, `affords` moved
# five shops across four queries and every one was silent::
#
#     a sleeping bag for car camping under $300  nemoequipment.com    379.95 -> 69.95
#     milk thistle ... under $30                 doublewoodsupple...   54.77 -> 19.95
#     creatine monohydrate powder under $40      nutricost.com         52.97 -> 26.97
#     collagen peptides powder under $45         nakednutrition.com    49.99 -> 41.99
#     collagen peptides powder under $45         purebulk.com        1035.95 ->  8.95
#
# `roster_source.reason` was `null` on all four.


def test_a_shop_staked_off_its_best_answer_by_the_budget_says_so_on_the_response() -> None:
    """Red before this: ``roster_source.reason`` is ``null`` while the swap has happened.

    branchfurniture's best answer to "an office chair" is the Verve at $599. The shopper is
    served the Multitask at $279 and told nothing — not that the shop had a closer answer,
    not that the closer answer was over the ceiling they themselves stated.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch), _ceiling())

    reason = (body.get("roster_source") or {}).get("reason")
    assert reason is not None, (
        f"a shop was moved onto a substitute product and the response says nothing: "
        f"roster_source={body.get('roster_source')} shortlist={_priced(body)}"
    )
    assert "www.branchfurniture.com" in reason, reason
    assert "budget" in reason or "afford" in reason, reason


def test_the_substitution_sentence_is_absent_when_nothing_was_substituted() -> None:
    """The control, and the one that keeps the sentence from becoming noise.

    A budget-free auction stakes every shop on its plain best fit, so there is nothing to
    report and ``reason`` stays ``null`` — the shape
    ``test_graph_auction.py::...roster_source.reason is None`` already pins for the
    unconstrained graph path.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch), [])

    assert (body.get("roster_source") or {}).get("reason") is None, body["roster_source"]


def test_a_shop_with_nothing_affordable_is_not_reported_as_substituted() -> None:
    """floydhome has one chair at $1750 and is staked on it under a $500 ceiling.

    Nothing was substituted for that shop — it is rostered on exactly the product it would
    have been rostered on with no ceiling at all — so naming it here would tell the shopper
    the platform swapped something when it did not. Its refusal is published where refusals
    belong, in ``excluded``.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch), _ceiling())

    reason = (body.get("roster_source") or {}).get("reason") or ""
    assert "floydhome.com" not in reason, reason
    refused = [row["store_id"] for row in body["excluded"]]
    assert "floydhome.com" in refused, body["excluded"]
