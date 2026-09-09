"""A SHOP GETS ONE SLOT, AND THE ROSTER USED TO SPEND IT ON A PRODUCT THE GATE THEN REFUSED.

The regression, on ``POST /auctions`` with ``EXCHANGE_SHOP_ROSTER=graph`` and no ``roster`` in
the body, against the live nineteen-store demo graph (98,001 nodes)::

    "a walnut coffee table for the lounge"
      f9dcc1a  2 slots   branchfurniture.com "Coffee Table"              $599
                         floydhome.com       "The Lift Off Coffee Table" $700
      e9d7c8c  1 slot    branchfurniture.com "Coffee Table"              $599
                         floydhome.com  ->  organic_result_off_topic

The ``f9dcc1a`` row is that commit's own recorded reading; the ``e9d7c8c`` row was re-taken on
this graph while writing this file, and the two slots came back — same products, same prices,
same variant refs — the moment ``_solicited`` learned to ask the gate.

Nothing in the ranking gate changed. What changed is which of floydhome's products the roster
staked the shop's one slot on. :func:`~exchange.retrieval.roster._solicited` chose it by cosine
alone::

    product_ref = max(eligible, key=lambda pid: (fit[pid], pid))

and floydhome's eligible set carries both, read off the live graph::

    fit 0.634558   prod_5fe31298e40f72ce04d493c1f783ad94  "The Modular Table"        $1275
    fit 0.626495   prod_5f82be92cbc586c8f322422b03a11df2  "The Lift Off Coffee Table"  $700

0.008 apart, and the roster took the first. ``exchange.ranking.filters.organic_relevance_reason``
then judges the rostered row on ``identity_surface`` — title and brand, all
``catalog_identity`` returns — which for that row is ``"The Modular Table RIZE"``: one content
word of ``{walnut, coffee, table, lounge}`` where the rule needs two or half. So the shop died
holding a product BOTH layers would have kept.

**The refusal is correct and this file does not touch it.** "The Modular Table" is a walnut
table and is not a coffee table, and its title does not say otherwise. What is wrong is
spending the shop's only slot on it while the shop carries a coffee table the same crawl names
as one. The fix is in the roster: prefer the highest-fit product the gate will actually keep,
and fall back to the plain ``max`` when the shop has none.

**Sections 2 and 3 are the two ways this fix could have made things worse, and they are the
reason the file is longer than the one-line repair.** Both are about a shop DISAPPEARING with
no sentence anywhere, which is strictly worse than being refused with one:

* Section 2 — a shop with nothing keepable must still be rostered on its plain best fit, still
  be REPRESENTED in ``entries`` (R10), and still be refused at the shortlist WITH A REASON.
  It is a must-not-change guard: green before this change and green after, by construction.
* Section 3 — a shop that is RE-POINTED must keep the roster place its best product earned.
  ``solicit`` cuts its rows to ``limit``, so lowering a row's published fit can push a shop
  past the cut and out of the response entirely. This one WAS red: with ``limit=1`` and the
  refused product at the top of the shop set, floydhome was dropped altogether. Which shops
  are rostered is settled on the shop's best eligible product, exactly as it always was; only
  which product each carries is settled by the gate.

**What is real here and what is a double.** The route, ``GraphShopRoster.solicit``,
``_solicited``, ``collect_bids``' list-price fallback, ``rank_auction``, the organic gate and
the shortlist builder are all the shipped code. Exactly two things are replaced: the two
readers that need a Neo4j — ``GraphCandidateSource`` and ``candidate_shops`` — which stand in
for a live graph with the rows measured off one. That keeps the assertions runnable with no
container while still driving the seam the defect lives in; the live-graph reading above is
what says the rows are the real ones.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking import REASON_OFF_TOPIC_ORGANIC
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from exchange.retrieval import InMemoryCandidateSource
from exchange.retrieval.roster import DEFAULT_SOLICITED_SHOPS, GraphShopRoster
from fastapi.testclient import TestClient
from ingest.graph import ShopCandidate, ShopOffer

WALNUT = "a walnut coffee table for the lounge"

#: The four crawled products this file needs, VERBATIM off the live demo graph — title, brand,
#: categories, attribute keys and readings, variant names and the cheapest provenanced offer.
#:
#: They are quoted in full rather than simplified because the defect lives in the GAP between
#: two surfaces, and a simplified row closes the gap by accident: strip ``The Modular Table``'s
#: ``wood-type Walnut`` and its ``Small / Walnut`` variants and the retrieval layer refuses it
#: too, the shop is never rostered on it, and the test goes green against code that still has
#: the bug. ``retrieval.relevance`` judges all of this; ``ranking.filters`` judges the title and
#: the brand alone. That is the whole mechanism.
#:
#: ``similarity`` is the one field not read off the graph: it is chosen to reproduce the ORDER
#: the live fits have — Modular Table 0.634558 above Lift Off Coffee Table 0.626495, 0.008
#: apart — rather than their absolute values, because which product a shop is rostered on is
#: decided by the order alone.
COFFEE_TABLE = {
    "store": "branchfurniture.com",
    "product_id": "prod_545a4f81c417bb81b39be3838611ed32",
    "canonical_name": "Coffee Table",
    "brand": "Branch",
    "attributes": [{"key": "color", "value_string": "Walnut/Charcoal"}],
    "variant_names": ("Walnut/Charcoal", "White/White"),
    "price": 599.0,
    "similarity": 0.30,
}
MODULAR_TABLE = {
    "store": "floydhome.com",
    "product_id": "prod_5fe31298e40f72ce04d493c1f783ad94",
    "canonical_name": "The Modular Table",
    "brand": "RIZE",
    "categories": ["Tables"],
    "attributes": [
        {"key": "wood-type", "value_string": "Walnut"},
        {"key": "size", "value_string": "Small"},
    ],
    "variant_names": ("Small / Walnut", "Large / Maple"),
    "price": 1275.0,
    "similarity": 0.28,
}
LIFT_OFF = {
    "store": "floydhome.com",
    "product_id": "prod_5f82be92cbc586c8f322422b03a11df2",
    "canonical_name": "The Lift Off Coffee Table",
    "brand": "RIZE",
    "categories": ["Tables"],
    "attributes": [
        {"key": "wood-type", "value_string": "Walnut"},
        {"key": "size", "value_string": 'One Panel - 18" w x 67" l x 15" h'},
    ],
    "variant_names": ('One Panel - 18" w x 67" l x 15" h / Walnut / Stainless',),
    "price": 700.0,
    "similarity": 0.26,
}
#: A BED, and the row that says why the fallback branch is not hypothetical. Its identity is
#: ``"The Studio RIZE"`` and the gate refuses it — while ``retrieval.relevance`` keeps it,
#: because an attribute KEY named ``pop-bedside-table-type`` supplies the word ``table`` and a
#: ``panel-hardware-color`` reading supplies ``Walnut``. That is the arm
#: ``ranking.filters.organic_relevance_reason``'s docstring flags as unmeasured and risky, met
#: in the corpus it predicted it would be met in.
THE_STUDIO = {
    "store": "floydhome.com",
    "product_id": "prod_c5ab78e9e6596749692ceecdd4fb628a",
    "canonical_name": "The Studio",
    "brand": "RIZE",
    "categories": ["Beds"],
    "attributes": [
        {"key": "panel-hardware-color", "value_string": "Walnut/Black"},
        {"key": "pop-bedside-table-type", "value_string": "With Light & Charger"},
    ],
    "variant_names": ("King + Headboard / Walnut/Black / With Charger Only",),
    "price": 2510.0,
    "similarity": 0.24,
}

#: The two shops the live graph rosters for this query, each with the products it carries.
CRAWL: tuple[dict[str, Any], ...] = (COFFEE_TABLE, MODULAR_TABLE, LIFT_OFF)

#: One shop, and not one product of it the organic gate would keep — a true subset of what the
#: live graph holds for ``floydhome.com`` on this query.
NOTHING_KEEPABLE: tuple[dict[str, Any], ...] = (MODULAR_TABLE, THE_STUDIO)

#: The same three real products, with the similarities rearranged so the REFUSED one is the
#: best-matching product in the whole shop set. Nothing else about them changes.
#:
#: This is the arrangement that exposes the roster's own cut. ``solicit`` sorts the rows and
#: keeps ``limit`` of them, so a shop re-pointed onto a lower-fitting product does not merely
#: carry a smaller number — it can fall off the end of the list, out of ``entries`` and out of
#: the response entirely, with no exclusion reason anywhere. That is the shop-dropping outcome
#: this whole change exists to avoid, arrived at from the other direction, and it is why WHICH
#: shops are rostered is decided on a different number from the one each row publishes.
_BY_FIT = {
    COFFEE_TABLE["product_id"]: 0.28,
    MODULAR_TABLE["product_id"]: 0.30,
    LIFT_OFF["product_id"]: 0.26,
}
TRUNCATION_CRAWL: tuple[dict[str, Any], ...] = tuple(
    {**row, "similarity": _BY_FIT[row["product_id"]]} for row in CRAWL
)


def _intent(query: str) -> dict[str, Any]:
    return {
        "intent_id": "intent-walnut",
        "cluster_id": "cluster-furniture",
        "query": query,
        "hard_constraints": [],
        "preferences": [],
        "currency": "USD",
        "budget_band": "unspecified",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }


def _stores(crawl: tuple[dict[str, Any], ...]) -> list[str]:
    seen: list[str] = []
    for row in crawl:
        if row["store"] not in seen:
            seen.append(row["store"])
    return seen


def _shop_candidates(crawl: tuple[dict[str, Any], ...]) -> list[ShopCandidate]:
    """What ``ingest.graph.candidate_shops`` answers for ``crawl`` — one row per store."""
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
    records = [
        {
            "product_id": row["product_id"],
            "canonical_name": row["canonical_name"],
            "brand": row["brand"],
            "status": "active",
            "similarity": row["similarity"],
            "categories": list(row.get("categories") or ()),
            "attributes": list(row.get("attributes") or ()),
            "variant_names": tuple(row.get("variant_names") or ()),
        }
        for row in crawl
    ]
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


def _app(monkeypatch: pytest.MonkeyPatch, crawl: tuple[dict[str, Any], ...] = CRAWL):
    """The shipped exchange, its shop roster reading the graph, and nobody bidding.

    Tier 0 stores have no agent, so ``collect_bids`` builds a list-price fallback for every
    row — which is the ORGANIC case, and the only case ``organic_relevance_reason`` looks at.
    """
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


def _serve(app, query: str = WALNUT) -> dict[str, Any]:
    posted = TestClient(app).post(
        "/auctions", json={"intent": _intent(query), "bid_timeout_seconds": 2.0}
    )
    assert posted.status_code == 201, posted.text
    return dict(posted.json())


def _titles(body: dict[str, Any]) -> list[str]:
    return [
        (slot.get("product") or {}).get("identity", {}).get("title")
        for slot in body["shortlist"]["slots"]
    ]


def _off_topic_reasons(body: dict[str, Any]) -> list[str]:
    return [
        reason
        for row in body["excluded"]
        for reason in row["exclusion_reasons"]
        if reason.startswith(REASON_OFF_TOPIC_ORGANIC)
    ]


# =====================================================================================
# 1. THE REGRESSION — the shop's one slot goes to a product the gate will keep
# =====================================================================================
def test_a_shop_is_rostered_on_a_product_the_organic_gate_will_actually_keep() -> None:
    """The reproduction: floydhome must reach the screen with its Lift Off Coffee Table.

    Red before the fix with ``slots == 1`` — the roster spent floydhome's slot on "The Modular
    Table" and the gate refused it — and this is the whole of the defect, because the shop was
    holding "The Lift Off Coffee Table" the entire time and both layers vouch for it.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch))

    titles = _titles(body)
    assert titles == ["Coffee Table", "The Lift Off Coffee Table"], (
        f"the roster staked each shop's one slot on its best-fitting product without asking "
        f"whether the organic gate would keep it. shortlist={titles} "
        f"refusals={_off_topic_reasons(body)}"
    )
    assert len(body["shortlist"]["slots"]) == 2, body["shortlist"]
    assert not _off_topic_reasons(body), body["excluded"]
    assert all(slot["fallback"] for slot in body["shortlist"]["slots"]), body["shortlist"]

    priced = {slot["price"]["unit_price"] for slot in body["shortlist"]["slots"]}
    assert priced == {599.0, 700.0}, (
        "each row is priced at the cheapest provenanced offer for the product the shop was "
        f"rostered on, so a re-pointed shop must carry ITS price too: {priced}"
    )


def test_the_shop_is_rostered_on_the_keepable_product_before_any_ranking_runs() -> None:
    """The same decision one layer down, so a green shortlist cannot be the ranker's doing.

    ``solicit`` is asked directly: floydhome's roster row must name the Lift Off Coffee Table
    and carry ITS price and ITS variant, never the Modular Table's — the price, the variant and
    the product are one observation (see :class:`~exchange.retrieval.roster.SolicitedShop`).
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        roster = _graph_roster(monkeypatch, CRAWL)
        solicited = roster.solicit(_intent(WALNUT))

    rows = {shop.store_id: shop for shop in solicited.shops}
    assert set(rows) == {"branchfurniture.com", "floydhome.com"}, solicited
    floyd = rows["floydhome.com"]
    assert floyd.product_ref == "prod_5f82be92cbc586c8f322422b03a11df2", solicited
    assert floyd.list_price == 700.0, floyd
    assert floyd.variant_ref == "native-prod_5f82be92cbc586c8f322422b03a11df2", floyd
    assert floyd.intent_match == pytest.approx(
        next(a.fit_score for a in solicited.fit if a.product_id == floyd.product_ref)
    ), (
        "intent_match is the fit of the product this shop was ACTUALLY rostered on — a row "
        "carrying the refused product's higher score would publish a measurement of something "
        "the shopper is not being shown"
    )


# =====================================================================================
# 2. THE FALLBACK — a shop with nothing keepable is rostered exactly as before
# =====================================================================================
def test_a_shop_with_no_keepable_product_is_still_rostered_and_refused_with_a_reason() -> None:
    """The direction that must NOT change, and the worse regression this fix could have traded
    for.

    ``floydhome.com`` here carries two products the search reached and the platform's own crawl
    says neither is about the query — "The Modular Table" is a table and not a coffee table,
    "The Studio" is a bed. The shop must still be rostered, on its best-fitting product and
    exactly as before, so it is REPRESENTED in ``entries`` (R10) and reaches the shortlist as a
    list-price row where the gate refuses it and SAYS SO. Dropping the shop at roster time
    would empty the same screen while deleting the sentence that explains it.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        roster = _graph_roster(monkeypatch, NOTHING_KEEPABLE)
        solicited = roster.solicit(_intent(WALNUT))

    assert [shop.store_id for shop in solicited.shops] == ["floydhome.com"], solicited
    assert solicited.shops[0].product_ref == MODULAR_TABLE["product_id"], (
        f"with nothing keepable the roster falls back to the plain best fit, unchanged: "
        f"{solicited.shops}"
    )
    assert solicited.shops[0].list_price == 1275.0, solicited.shops[0]
    assert solicited.reason is None, solicited.reason

    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch, NOTHING_KEEPABLE))

    assert [row["store_id"] for row in body["entries"]] == ["floydhome.com"], body["entries"]
    assert body["shortlist"]["slots"] == [], body["shortlist"]
    refusals = _off_topic_reasons(body)
    assert len(refusals) == 1, body["excluded"]
    assert "not about what was asked" in refusals[0], refusals[0]
    assert body["market"]["nothing_shown"] is True, body["market"]


# =====================================================================================
# 3. THE CUT — re-pointing a shop must not cost it its place on the roster
# =====================================================================================
def test_a_re_pointed_shop_keeps_the_roster_place_its_best_product_earned() -> None:
    """The regression this fix could have introduced, and nearly did.

    ``solicit`` sorts the rows it built and keeps ``limit`` of them. A row's sort key is its
    published ``intent_match``, which is the fit of the product the shop is rostered ON — so
    the moment re-pointing lowers that number, a shop can be pushed past the cut. It then
    reaches nothing: not the shortlist, not ``entries``, not ``excluded``, and no reason is
    written anywhere, because a shop that was never solicited has nothing to be refused for.
    That is strictly worse than the defect being repaired, which at least left a sentence.

    Arranged so one shop is at the cut: ``limit=1``, and floydhome's REFUSED product is the
    best-matching thing either shop carries. Before this change floydhome was the one shop
    rostered — on "The Modular Table". It must still be the one shop rostered, now on "The
    Lift Off Coffee Table". **Which shops** is settled on the shop's best eligible product,
    exactly as it always was; only **which product** is settled by the gate.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        roster = _graph_roster(monkeypatch, TRUNCATION_CRAWL, limit=1)
        solicited = roster.solicit(_intent(WALNUT))

    assert [shop.store_id for shop in solicited.shops] == ["floydhome.com"], (
        f"floydhome carries the best-matching product either shop has, so it is the one shop "
        f"a limit of 1 rosters — and asking the gate which of ITS products to name must not "
        f"change that. Rostered instead: {[s.store_id for s in solicited.shops]}"
    )
    floyd = solicited.shops[0]
    assert floyd.product_ref == LIFT_OFF["product_id"], solicited
    assert floyd.list_price == 700.0, floyd
    assert floyd.intent_match == pytest.approx(
        next(a.fit_score for a in solicited.fit if a.product_id == LIFT_OFF["product_id"])
    ), (
        "the row publishes the fit of the product it NAMES, not the fit of the one that won "
        "it the place — the second would be a measurement of something nobody is shown"
    )


def test_the_kept_rows_are_ordered_by_the_fit_of_the_product_they_name() -> None:
    """``ShopRoster``'s own documented ordering, on rows the cut did not remove.

    Selection and ordering are answered by two different numbers now, so the ordering half is
    pinned rather than left to follow from the selection half: what comes back is sorted by the
    published ``intent_match`` descending, then ``store_id``.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        roster = _graph_roster(monkeypatch, TRUNCATION_CRAWL)
        solicited = roster.solicit(_intent(WALNUT))

    assert [shop.store_id for shop in solicited.shops] == [
        "branchfurniture.com",
        "floydhome.com",
    ], solicited
    assert [round(shop.intent_match, 6) for shop in solicited.shops] == sorted(
        (round(shop.intent_match, 6) for shop in solicited.shops), reverse=True
    ), solicited
