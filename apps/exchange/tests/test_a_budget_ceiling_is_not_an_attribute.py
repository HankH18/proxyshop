"""A BUDGET CEILING EMPTIED EVERY GRAPH-ROSTERED SHORTLIST, AT EVERY VALUE.

The defect, driven through the served buyer path against the live nineteen-store demo graph
(98,001 nodes) before the repair. ``POST :8081/buyer/intent/clarify`` → ``confirm`` →
``GET :8083/auctions/{id}/shortlist``::

    a sofa                            0 constraints   3 slots
    a sofa under 2000 dollars         1 constraint    0 slots
    vitamin d3                        0 constraints   3 slots
    vitamin d3 under 50 dollars       1 constraint    0 slots
    a sleeping bag                    0 constraints   4 slots
    a sleeping bag under 900 dollars  1 constraint    0 slots

and the same thing one layer down, on the exchange's own graph-rostered route::

    $ curl -s -X POST :8083/auctions -d '{"intent": {"query": "a sofa",
        "hard_constraints": [{"field":"price_usd","op":"lte","value":2000.0}], ...}}'
      roster_source: source=neo4j shops=0 products_considered=0
        reason="this exchange's catalogue graph returned no product at all for this intent
                — the index matched nothing to judge — so there is no shop to solicit"
      entries=0 excluded=0 slots=0

$2,000 is above every sofa but one in the corpus, so this is not a ceiling doing its job.
**Any** ceiling emptied it, at any value: ``price_usd lte 5000``, above every price the graph
holds, returned the same 0 shops / 0 considered.

WHAT THE REPAIR IS ACTUALLY WORTH — measured A/B, and the query shape is half the number
--------------------------------------------------------------------------------------
An earlier headline for this change said "15 of 15 realistic ceiling queries non-empty". That
number is wrong and, more importantly, it was measured on the wrong INPUT: bare nouns with
hand-written ``hard_constraints``. A shopper does not hand this exchange a noun; they hand the
buyer service a sentence, and ``POST :8081/buyer/intent/clarify`` turns it into an intent that
carries things a hand-written body does not — a ``category``, extra ``eq`` constraints, and the
whole sentence as ``query`` text. Those change the answer.

So: twelve shopper sentences, each put through the LIVE ``POST :8081/buyer/intent/clarify``
on the running demo stack, and the twelve intents it returned replayed VERBATIM through
``POST /auctions`` against the live nineteen-store Neo4j. Same corpus, same twelve intents,
same harness, run once at ``3d65cfd`` and once with this change::

                                        HEAD 3d65cfd    with this change
    graph path (no roster in the body)     0 of 12           8 of 12
    stated path (deploy/demo/buyer-roster.json,
      15 rows, the roster buyer-svc sends)  3 of 12          10 of 12

"Non-empty" means ``shortlist.slots`` was not ``[]``. The twelve sentences were "a sofa for my
apartment under $2000", "looking for an office chair under $500", "a coffee table for the
lounge under $400", "a vitamin d3 supplement for under $50", "a sleeping bag for car camping
under $300", "milk thistle liver support supplement under $30", "creatine monohydrate powder
under $40", "a pour over coffee maker under $80", "whole bean dark roast coffee under $25",
"a backpacking tent under $600", "collagen peptides powder under $45", "a dining table under
$1500". Every one produced a ``price_usd lte`` constraint through the clarifier.

**ALL FOUR THAT ARE STILL EMPTY ARE THIS DEFECT'S TWIN, ONE FIELD OVER, AND IT IS STILL OPEN.**
The clarifier emits a ``category`` or an attribute ``eq`` the corpus carries no reading for,
``build_query`` pushes it into the Cypher exactly as it used to push the price, and the window
comes back empty. Bisected by removing one key at a time from the clarifier's own intent and
re-running ``GraphShopRoster.solicit`` against the live graph::

                                              as clarified   minus `category`   minus both
      looking for an office chair under $500     0 /   0        4 / 125          4 / 125
      a coffee table for the lounge under $400   0 / 125        2 / 125          2 / 125
      a pour over coffee maker under $80         0 /   0        0 /   0          2 / 125
      whole bean dark roast coffee under $25     0 /   0        0 /   0          2 / 125
                                                            (shops / products_considered)

The first two are the ``category``: measured on the live graph, ``MATCH (c:Category) RETURN
c.canonical_key`` is ``NULL`` for every category node, so a ``category`` that does not match by
name narrows retrieval to zero — ``'furniture'`` does — and one that matches the wrong thing is
worse: the clarifier classified "coffee table" as ``category='coffee'``, so 125 bags of coffee
were retrieved and correctly judged off-topic. The last two are the ``eq`` constraints
(``brew_method eq 'pour-over'``, ``grind eq 'whole-bean'``, ``roast_level eq 'dark'``): the
corpus carries no such attribute readings, so the ``AttributeFilter`` joins ``HAS_ATTRIBUTE``,
finds nothing, and excludes everybody — the identical mechanism this file is about. Replaying
all twelve intents with both keys stripped takes the graph path from 8 of 12 to **11 of 12**;
the twelfth is "a coffee table for the lounge under $400", which rosters two shops and has
both refused at the shortlist by the organic relevance gate. That repair is NOT part of this
change: unlike a
price, a ``brew_method`` genuinely IS an attribute, so the fix there is a crawl that records
one or a retrieval that degrades a missing reading instead of refusing on it, and neither is a
decision to make inside a budget filter.

Both numbers are the exchange's own list-price fallback rows: the harness ran against the live
graph with the four hosted stores' ``bid_endpoint``s removed, so no store agent was dialled
and no live bid is in either column. Bids change WHICH row wins a slot, not whether the roster
found anybody, which is what this defect was about.

WHY. Price is not an attribute and cannot be. It is a property of the ``Offer`` node —
measured on this graph, ``MATCH (o:Offer) RETURN keys(o)`` is exactly ``['offer_id', 'price',
'observed_at', 'currency', 'availability']`` — because a price belongs to an offer at a
moment, with a currency and a timestamp, not to the thing. ``MATCH (a:AttributeValue) WHERE
toLower(a.canonical_key) CONTAINS 'price' OR ... 'cost'`` returns **0** in a graph holding 147
distinct attribute keys.

So a ``price_usd`` bound is a question about money asked in the vocabulary of product claims,
and both halves of retrieval answered it the way they answer any unanswerable attribute
question — by refusing everybody:

1. :meth:`~exchange.retrieval.criteria.HardCriterion.pushdown` built an ``AttributeFilter`` for
   it. The Cypher joins ``HAS_ATTRIBUTE`` looking for a ``price_usd`` reading, finds none, and
   the retrieval returns **zero rows** — ``products_considered: 0`` above.
2. :meth:`~exchange.retrieval.criteria.RetrievalQuery.exclusion_reasons` re-decided it locally.
   ``HardCriterion.decide`` answers R19's "the candidate carries no such attribute" for every
   product in the corpus, so even with the pushdown declined the whole window is excluded.

**Both halves, or nothing.** Measured in-process against the live graph with only the pushdown
declined: ``a sofa`` under $2,000 goes from ``considered 0, eligible 0`` to ``considered 125,
eligible 0`` — the same empty shortlist, a different sentence. That is why this file asserts
each half separately as well as together: a fix that repairs one is indistinguishable at the
route from no fix at all.

**The correct filter already existed and nothing could reach it.**
:func:`exchange.ranking.filters.budget_reasons` compares the OFFER's own price against the
bound and is wired on the served path — it is what refuses the $4,095 sofa in section 1. The
repair is not "stop filtering on price"; it is "stop asking the catalogue a question only the
offer can answer", so the bound is decided exactly once, where the money is.

WHAT IS REAL HERE AND WHAT IS A DOUBLE. The route, ``GraphShopRoster.solicit``, ``_solicited``,
``build_query``, ``CandidateRetrieval``, the relevance gate, ``collect_bids``' list-price
fallback, ``rank_auction``, ``budget_reasons`` and the shortlist builder are all the shipped
code. Exactly two things stand in for a Neo4j: ``GraphCandidateSource`` and ``candidate_shops``.

The candidate double here is **not** :class:`~exchange.retrieval.sources.InMemoryCandidateSource`
and the difference is the whole point of section 1. That double filters nothing, deliberately —
which means it cannot see a pushdown defect at all, and ``pushdown``'s own docstring says so:
*"no offline test can see it, because the double does not apply the filter at all."*
:class:`AttributeFilteringSource` below applies ``query.attribute_filters`` with the same
predicate ``ingest.graph.query._FILTER_AND_RETURN`` applies, so the pushdown is observable
offline and section 1 goes red for the same reason the live graph does.

The three products are quoted verbatim off the live demo graph — ids, titles, brands,
attribute readings, variant names and the cheapest provenanced offer — and they are the three
the graph path actually rosters for ``"a sofa"`` today.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app

# Imported from ``ranking.filters`` ON PURPOSE, though they are now DEFINED in
# ``retrieval.criteria``: the move must not change what ``ranking.filters`` publishes, and
# this import is what says so.
from exchange.ranking.filters import BUDGET_FIELDS, BUDGET_OPS, is_budget_bound
from exchange.ranking.reasons import REASON_OVER_BUDGET
from exchange.retrieval import (
    CandidateRetrieval,
    HardCriterion,
    InMemoryCandidateSource,
    build_query,
)
from exchange.retrieval.roster import DEFAULT_SOLICITED_SHOPS, GraphShopRoster
from exchange.retrieval.sources import make_candidate
from fastapi.testclient import TestClient
from ingest.graph import Candidate, ShopCandidate, ShopOffer
from ingest.graph.model import canonical_text, slug

SOFA = "a sofa"

#: The three products the graph path rosters for ``"a sofa"``, VERBATIM off the live demo
#: graph. Driven the moment this file was written::
#:
#:     POST :8083/auctions  {"query": "a sofa", "hard_constraints": []}
#:       branchfurniture.com  Focal Sofa                          1614.0
#:       floydhome.com        Sofa 2.0 Legs & Hardware             175.0
#:       sabai.design         The Bacana Sofa in Cactus Leather   4095.0
#:
#: **Not one of them carries a ``price_usd`` attribute**, and that is not a simplification made
#: for the fixture — it is the corpus. Adding one would close the gap this file exists to hold
#: open: the constraint would become decidable against a claim, both halves of retrieval would
#: admit the row, and the test would go green against code that still has the bug.
#:
#: ``similarity`` is the one field not read off the graph. It is chosen to reproduce the ORDER
#: the live fits have rather than their absolute values, because which product a shop is
#: rostered on is decided by the order alone.
FOCAL_SOFA = {
    "store": "www.branchfurniture.com",
    "product_id": "prod_f0271e25d67a64fe3009fc56c786b1d7",
    "canonical_name": "Focal Sofa",
    "brand": "Branch",
    "attributes": [
        {"key": "color", "value_string": "Deep Sea"},
        {"key": "color", "value_string": "Eclipse"},
        {"key": "color", "value_string": "Alloy"},
    ],
    "variant_names": ("Deep Sea", "Eclipse", "Alloy"),
    "price": 1614.0,
    "similarity": 0.30,
}
SOFA_LEGS = {
    "store": "floydhome.com",
    "product_id": "prod_d87c7c3da1bfa32e68c4270cc861ecbc",
    "canonical_name": "Sofa 2.0 Legs & Hardware",
    "brand": "RIZE-Fedex",
    "attributes": [{"key": "hardware-color", "value_string": "Black"}],
    "variant_names": ("Black",),
    "price": 175.0,
    "similarity": 0.28,
}
BACANA_SOFA = {
    "store": "sabai.design",
    "product_id": "prod_c15d92c3210ae0676223eb39a169e5cc",
    "canonical_name": "The Bacana Sofa in Cactus Leather",
    "brand": "Sabai Design",
    "attributes": [{"key": "color", "value_string": "Terra"}],
    "variant_names": ("Terra",),
    "price": 4095.0,
    "similarity": 0.26,
}

CRAWL: tuple[dict[str, Any], ...] = (FOCAL_SOFA, SOFA_LEGS, BACANA_SOFA)

#: A ceiling ABOVE two of the three sofas and below the third. Deliberately not a tight one:
#: the defect is that any ceiling empties the page, so the reproduction has to be a ceiling
#: that plainly should not.
CEILING = 2000.0

#: A floor below two of the three. ``gte`` is the other half of :data:`BUDGET_OPS` and it took
#: the identical path, so it gets the identical assertion.
FLOOR = 1000.0


# =====================================================================================
# The doubles — two readers that need a Neo4j, and one predicate copied out of the Cypher
# =====================================================================================
def _attribute_filter_matches(pushed: Any, attributes: list[dict[str, Any]]) -> bool:
    """``ingest.graph.query._FILTER_AND_RETURN``'s ``$attribute_filters`` clause, in Python.

    Copied rather than approximated, component for component, because an approximation here
    would be a *second* pushdown semantics and the test would then be asserting against its own
    idea of the graph instead of against the graph's. The Cypher, verbatim::

        all(f IN $attribute_filters WHERE any(a IN attrs WHERE
              a.key = f.key
              AND (f.value_string IS NULL OR a.canonical_value_string = f.value_string)
              AND (f.value_bool IS NULL OR a.value_bool = f.value_bool)
              AND (f.equals_number IS NULL OR a.value_number = f.equals_number)
              AND (f.min_number IS NULL OR (a.value_number IS NOT NULL
                                            AND a.value_number >= f.min_number))
              AND (f.max_number IS NULL OR (a.value_number IS NOT NULL
                                            AND a.value_number <= f.max_number))
              AND (f.unit IS NULL OR a.canonical_unit = f.unit)))

    ``a.key`` is the node's ``canonical_key`` and ``a.canonical_value_string`` /
    ``a.canonical_unit`` are its folded readings, so the raw projections a ``Candidate``
    carries are folded here through the same :func:`slug` / :func:`canonical_text` the writer
    used. The load-bearing line for this file is the ``a.value_number IS NOT NULL`` guard on
    the bounds: a product carrying no ``price_usd`` reading at all satisfies no bound, which is
    why a ceiling returns zero rows rather than every row.
    """
    wanted = pushed.as_parameter()
    for attribute in attributes:
        if slug(str(attribute.get("key", ""))) != wanted["key"]:
            continue
        text = attribute.get("value_string")
        if wanted["value_string"] is not None and (
            text is None or canonical_text(str(text)) != wanted["value_string"]
        ):
            continue
        if (
            wanted["value_bool"] is not None
            and attribute.get("value_bool") is not wanted["value_bool"]
        ):
            continue
        number = attribute.get("value_number")
        if wanted["equals_number"] is not None and (
            number is None or float(number) != wanted["equals_number"]
        ):
            continue
        if wanted["min_number"] is not None and (
            number is None or float(number) < wanted["min_number"]
        ):
            continue
        if wanted["max_number"] is not None and (
            number is None or float(number) > wanted["max_number"]
        ):
            continue
        unit = attribute.get("unit")
        if wanted["unit"] is not None and (
            unit is None or canonical_text(str(unit)) != wanted["unit"]
        ):
            continue
        return True
    return False


class AttributeFilteringSource:
    """The double that DOES apply the pushdown — the one thing ``InMemoryCandidateSource`` won't.

    ``InMemoryCandidateSource`` filters nothing on purpose, so that "retrieval returns
    hard-criteria-satisfying candidates whatever the source returned" is observable. That makes
    it the wrong double for a *pushdown* defect: the filter it cannot see is exactly the filter
    that emptied the served page. Both are used in this file, for opposite jobs — this one in
    section 1, where the graph's narrowing is the thing under test, and the lenient one in
    section 3, where the local decision is.
    """

    name = "attribute-filtering"

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        #: Every query this source was asked, in order — so the pushdown is observable from
        #: the route as well as from ``build_query``.
        self.queries: list[Any] = []

    def fetch(self, query: Any) -> list[Candidate]:
        self.queries.append(query)
        rows = [make_candidate(record, query_text=query.query_text) for record in self.records]
        kept = [
            candidate
            for candidate in rows
            if all(
                _attribute_filter_matches(pushed, list(candidate.attributes))
                for pushed in query.attribute_filters
            )
        ]
        return kept[: query.limit]


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
    sources: list[AttributeFilteringSource] | None = None,
) -> GraphShopRoster:
    """A real :class:`GraphShopRoster` over doubles for the only two things needing a Neo4j.

    Args:
        sources: appended to as the roster builds candidate sources, so a caller can read back
            the :class:`~exchange.retrieval.criteria.RetrievalQuery` the route actually handed
            the graph. The pushdown is a property of that query and of nothing the response
            carries, so this is the only way to assert it from the route.
    """
    records = _records(crawl)

    def source(session: Any, **kwargs: Any) -> AttributeFilteringSource:
        built = AttributeFilteringSource(records)
        if sources is not None:
            sources.append(built)
        return built

    monkeypatch.setattr("exchange.retrieval.roster.GraphCandidateSource", source)
    monkeypatch.setattr(
        "exchange.retrieval.roster.candidate_shops",
        lambda session, **kwargs: _shop_candidates(crawl),
    )

    @contextmanager
    def sessions():
        yield object()

    return GraphShopRoster(sessions, limit=DEFAULT_SOLICITED_SHOPS)


def _intent(constraints: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "intent_id": "intent-sofa",
        "cluster_id": "cluster-furniture",
        "query": SOFA,
        "hard_constraints": constraints,
        "preferences": [],
        "currency": "USD",
        "budget_band": "unspecified",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }


def _ceiling(value: float = CEILING) -> list[dict[str, Any]]:
    return [{"field": "price_usd", "op": "lte", "value": value}]


def _floor(value: float = FLOOR) -> list[dict[str, Any]]:
    return [{"field": "price_usd", "op": "gte", "value": value}]


def _app(monkeypatch: pytest.MonkeyPatch, crawl: tuple[dict[str, Any], ...] = CRAWL):
    """The shipped exchange, its shop roster reading the graph, and nobody bidding.

    Tier 0 stores have no agent, so ``collect_bids`` mints a list-price fallback for every row.
    That is the organic case, and it is the one where the price the budget wall judges is the
    platform's OWN crawled ``Offer.price`` rather than a number a bidder chose.
    """
    from exchange.ranking.serving import configure_ranking
    from exchange.ranking.verification import StaticCatalogSnapshots

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
        "/auctions",
        json={"intent": _intent(constraints), "bid_timeout_seconds": 2.0},
    )
    assert posted.status_code == 201, posted.text
    return dict(posted.json())


def _priced(body: dict[str, Any]) -> dict[str, float]:
    return {
        (slot.get("product") or {}).get("identity", {}).get("title"): (slot.get("price") or {})[
            "unit_price"
        ]
        for slot in body["shortlist"]["slots"]
    }


def _budget_refusals(body: dict[str, Any]) -> list[str]:
    return [
        reason
        for row in body["excluded"]
        for reason in row["exclusion_reasons"]
        if reason.startswith(REASON_OVER_BUDGET)
    ]


# =====================================================================================
# 1. THE REGRESSION — a stated ceiling returns a shortlist, and every row is under it
# =====================================================================================
def test_a_graph_rostered_ceiling_returns_a_shortlist_whose_rows_are_all_under_it() -> None:
    """The reproduction. Red before the repair with ``slots == 0`` and no shop rostered.

    This is the assertion the whole change exists for, and it is stated as "every served row
    clears the bound" rather than "these two titles" so it cannot be satisfied by a shortlist
    that merely came back non-empty.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch), _ceiling())

    served = _priced(body)
    assert served, (
        f"a ceiling of ${CEILING:,.0f} emptied the shortlist of a corpus holding two sofas "
        f"under it. roster_source={body.get('roster_source')} "
        f"entries={len(body.get('entries') or [])}"
    )
    assert all(price <= CEILING for price in served.values()), served
    assert served == {"Focal Sofa": 1614.0, "Sofa 2.0 Legs & Hardware": 175.0}, served


def test_the_row_above_the_ceiling_is_refused_for_its_price_not_a_missing_attribute() -> None:
    """The other half of the same claim: the bound still EXCLUDES, and for the right reason.

    Declining ``price_usd`` at retrieval removes a filter, so the change is only correct if
    something else was already applying the bound. It is
    :func:`~exchange.ranking.filters.budget_reasons`, against the offer's own price — and this
    asserts on its reason string, because "the shortlist is short" is equally consistent with
    the bound being dropped on the floor.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch), _ceiling())

    refusals = _budget_refusals(body)
    assert len(refusals) == 1, body["excluded"]
    assert "4095.0" in refusals[0], refusals
    assert "above the buyer's ceiling" in refusals[0], refusals
    assert "The Bacana Sofa in Cactus Leather" not in _priced(body), _priced(body)


def test_a_graph_rostered_floor_serves_the_rows_above_it_and_refuses_the_row_below() -> None:
    """``gte`` is the other member of :data:`BUDGET_OPS` and took the identical broken path."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch), _floor())

    served = _priced(body)
    assert served == {"Focal Sofa": 1614.0, "The Bacana Sofa in Cactus Leather": 4095.0}, served
    assert all(price >= FLOOR for price in served.values()), served
    refusals = _budget_refusals(body)
    assert len(refusals) == 1 and "below the buyer's floor" in refusals[0], body["excluded"]


def test_the_unconstrained_shortlist_is_the_same_three_rows_the_live_graph_serves() -> None:
    """The control, and it rules out the second reading of every zero above.

    "The ceiling emptied the page" and "this fixture has nothing to serve" produce the same
    ``slots == 0``. With no constraint at all the same doubles serve all three sofas, so a zero
    under a ceiling is the ceiling's doing — and the three rows match what the live nineteen-
    store graph answered for ``"a sofa"`` when this file was written.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        body = _serve(_app(monkeypatch), [])

    assert _priced(body) == {
        "Focal Sofa": 1614.0,
        "Sofa 2.0 Legs & Hardware": 175.0,
        "The Bacana Sofa in Cactus Leather": 4095.0,
    }, _priced(body)
    assert not _budget_refusals(body), body["excluded"]


# =====================================================================================
# 2. THE PUSHDOWN HALF — what Cypher cannot say
# =====================================================================================
def test_a_budget_bound_is_not_pushed_down_to_the_graph() -> None:
    """Edit one, asserted where it is decidable: no ``AttributeFilter`` for a money bound.

    Its own docstring states the invariant: *a pushdown may never exclude a candidate the local
    decision would admit*. A ``price_usd`` filter excludes **every** candidate the local
    decision would admit, because no product in any corpus carries a ``price_usd`` attribute —
    the number lives on the ``Offer``. So it joins ``in``, ``contains`` and numeric ``eq`` in
    the list of things Cypher cannot say.
    """
    query = build_query(_intent(_ceiling()))
    assert query.attribute_filters == (), (
        f"a money bound was pushed into the catalogue query as an attribute predicate: "
        f"{[f.as_parameter() for f in query.attribute_filters]}"
    )
    assert [(c.canonical_field, c.op) for c in query.local_only_criteria] == [
        ("price-usd", "lte")
    ], query.local_only_criteria


def test_the_route_asks_the_graph_no_price_question() -> None:
    """The same edit observed from the ROUTE, so it cannot be true only of ``build_query``.

    ``GraphShopRoster.solicit`` calls ``build_query`` itself, with its own ``product_limit``;
    a pushdown correct in the unit and wrong on the wire is exactly the shape of defect this
    file was opened for, so the query the source was actually handed is read back and asserted.
    """
    asked: list[AttributeFilteringSource] = []
    with pytest.MonkeyPatch.context() as monkeypatch:
        roster = _graph_roster(monkeypatch, CRAWL, sources=asked)
        solicited = roster.solicit(_intent(_ceiling()))

    queries = [query for source in asked for query in source.queries]
    assert len(queries) == 1, queries
    assert queries[0].attribute_filters == (), (
        f"the served route pushed a money bound into the catalogue query: "
        f"{[f.as_parameter() for f in queries[0].attribute_filters]}"
    )
    assert {shop.store_id for shop in solicited.shops} == set(_stores(CRAWL)), solicited


def test_a_non_budget_numeric_bound_is_still_pushed_down() -> None:
    """The exemption is a hole, so its edges are asserted as hard as its middle.

    ``lte``/``gte`` on anything that is not the buyer's money bound is a perfectly good
    attribute question, and the graph should still answer it — narrowing before the wire is
    what the pushdown is for.
    """
    query = build_query(
        _intent(
            [
                {"field": "seat_depth_in", "op": "gte", "value": 20},
                {"field": "warranty_years", "op": "lte", "value": 5},
            ]
        )
    )
    pushed = sorted(f.as_parameter()["key"] for f in query.attribute_filters)
    assert pushed == ["seat-depth-in", "warranty-years"], pushed
    assert query.local_only_criteria == (), query.local_only_criteria


# =====================================================================================
# 3. THE LOCAL-DECISION HALF — the edit the pushdown alone does not make
# =====================================================================================
def test_a_budget_bound_does_not_exclude_a_candidate_for_carrying_no_price_attribute() -> None:
    """Edit two, isolated. Red even with edit one applied, which is why it is asserted apart.

    :class:`~exchange.retrieval.sources.InMemoryCandidateSource` filters nothing, so nothing a
    pushdown does or does not do is visible here: this drives only
    ``RetrievalQuery.exclusion_reasons``. Measured in-process against the live graph with the
    pushdown declined and this left alone, ``a sofa`` under $2,000 answered ``considered 125,
    eligible 0`` — every one of the 125 refused with R19's *"the candidate carries no such
    attribute"* for ``price_usd``.
    """
    retrieval = CandidateRetrieval(InMemoryCandidateSource(_records(CRAWL)))
    result = retrieval.retrieve(_intent(_ceiling()), limit=25)

    price_refusals = [
        reason
        for row in result.excluded
        for reason in row.reasons
        if "price_usd" in reason or "price-usd" in reason
    ]
    assert not price_refusals, (
        f"a money bound was decided against product attributes, so every candidate in a corpus "
        f"that stores price on the Offer is undecidable and therefore excluded: {price_refusals}"
    )
    assert result.eligible_count == 3, (result.eligible_count, result.excluded, result.off_topic)


def test_a_hard_constraint_that_is_not_a_budget_still_fails_closed_on_an_absent_attribute() -> None:
    """R19 is untouched for everything else, and this is the guard that says so.

    The repair is not "stop refusing candidates that cannot answer"; it is "stop asking the
    catalogue about money". A ``seat_depth_in`` bound no product carries a reading for must
    still exclude every one of them, with R19's own sentence.
    """
    retrieval = CandidateRetrieval(InMemoryCandidateSource(_records(CRAWL)))
    result = retrieval.retrieve(
        _intent([{"field": "seat_depth_in", "op": "gte", "value": 20}]), limit=25
    )

    assert result.eligible_count == 0, result.assessments
    assert len(result.excluded) == 3, result.excluded
    assert all(
        "carries no such attribute" in reason for row in result.excluded for reason in row.reasons
    ), result.excluded


# =====================================================================================
# 4. THE SPELLINGS — every field/op pair the exemption covers, and every one it must not
# =====================================================================================
#: Every spelling of the buyer's money bound that :func:`~ingest.graph.model.slug` folds onto
#: ``BUDGET_FIELDS``. Written out as literals rather than generated, so the test reds if the
#: FOLD narrows as well as if the exemption does — ``slug`` is the only thing making these one
#: field and not eight, and ``pushdown`` and ``is_budget_bound`` must not be able to disagree
#: about which key is which.
BUDGET_SPELLINGS: tuple[str, ...] = (
    "price_usd",
    "Price USD",
    "price-usd",
    "PRICE_USD",
    " price usd ",
    "Price_Usd",
    "price.usd",
    "price usd",
)

#: Money-shaped fields that are NOT the buyer's budget. ``apps/buyer/svc/src/intent/extraction``
#: mints ``price_usd`` and nothing else, and widening the exemption to these would be strictly
#: wrong: they would ride to ranking, ``is_budget_bound`` would answer False there too, and
#: ``hard_constraint_reasons`` would grade them against verified claims — a different decision
#: about a different quantity. ``priceUsd`` is here because ``slug`` folds it to ``priceusd``,
#: not to ``price-usd``: it is the boundary of the fold, not a synonym.
NOT_BUDGET_SPELLINGS: tuple[str, ...] = (
    "list_price",
    "unit_price",
    "total_price",
    "cost",
    "price",
    "priceUsd",
)


@pytest.mark.parametrize("field", BUDGET_SPELLINGS)
@pytest.mark.parametrize("op", BUDGET_OPS)
def test_every_spelling_of_the_money_bound_declines_the_pushdown(field: str, op: str) -> None:
    """One case per (spelling, op). Reds the moment one spelling stops being covered."""
    criterion = HardCriterion(field=field, op=op, value=50.0)
    assert is_budget_bound(criterion), (field, op, criterion.canonical_field)
    assert criterion.pushdown() is None, (
        f"{field!r} {op} folds to {criterion.canonical_field!r}, which is the buyer's money "
        f"bound, and it was pushed into the catalogue query anyway"
    )


def test_the_exemption_covers_exactly_the_published_budget_vocabulary() -> None:
    """Derived from the constants, so ADDING a budget field without teaching pushdown reds.

    The literal spellings above cannot catch that: they are today's vocabulary. This one reads
    :data:`BUDGET_FIELDS` and :data:`BUDGET_OPS` themselves, so the day a second money field is
    published, this test fails until ``pushdown`` declines it too — which is the whole reason
    the predicate has one owner instead of two lists.
    """
    for canonical in sorted(BUDGET_FIELDS):
        for op in BUDGET_OPS:
            criterion = HardCriterion(field=canonical, op=op, value=1.0)
            assert criterion.pushdown() is None, (canonical, op)


@pytest.mark.parametrize("field", NOT_BUDGET_SPELLINGS)
@pytest.mark.parametrize("op", BUDGET_OPS)
def test_a_money_shaped_field_that_is_not_the_budget_still_pushes_down(field: str, op: str) -> None:
    """The exemption must not widen. A ``list_price`` bound is a claim about a catalogue."""
    criterion = HardCriterion(field=field, op=op, value=50.0)
    assert not is_budget_bound(criterion), (field, op, criterion.canonical_field)
    pushed = criterion.pushdown()
    assert pushed is not None, (
        f"{field!r} {op} is not the buyer's money bound and was silently dropped from the "
        f"catalogue query; a pushdown that declines more than the rule does costs recall for "
        f"nothing"
    )
    assert pushed.as_parameter()["key"] == slug(field)


@pytest.mark.parametrize("op", ("eq", "in", "contains"))
def test_a_non_bound_op_on_the_money_field_keeps_whatever_it_already_did(op: str) -> None:
    """``price_usd eq 40`` is a description of a product, not a ceiling, and is not exempted.

    :data:`BUDGET_OPS` is ``('lte', 'gte')`` for that reason, and the exemption keys on the
    field AND the op. ``eq`` on a number already declined the pushdown for an unrelated reason
    (exact float equality in Cypher against ``math.isclose`` here); ``in`` and ``contains``
    already declined it too. What matters is that none of them becomes a *budget* — they are
    still decided locally against attributes, and this asserts the decision, not the pushdown.
    """
    value: Any = {"eq": 40.0, "in": ("40", "50"), "contains": "40"}[op]
    criterion = HardCriterion(field="price_usd", op=op, value=value)
    assert not is_budget_bound(criterion), criterion
    verdict = criterion.decide([{"key": "color", "value_string": "Terra"}])
    assert not verdict.satisfied and "carries no such attribute" in verdict.reason, verdict
