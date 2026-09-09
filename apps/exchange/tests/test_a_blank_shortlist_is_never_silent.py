"""A BLANK SHORTLIST IS A PUBLISHED FACT, NEVER ONLY A LOG LINE — and it must not be reachable
by an on-topic roster.

The regression this file gates, measured on ``main`` before it existed. Three stores rostered,
all three asked, all three silent, list-price fallback rows built for every one of them, and
this on the exchange's own log while the shopper's screen held nothing::

    WARNING exchange.auction.routes NOTHING REACHED THE SHOPPER: this auction had stores to
    represent and filled no shortlist slot at all, so the screen was blank — not even a
    catalogue price. market=nothing_shown solicited=3 sponsored=0 shown_sponsored=0 shown=0
    list_price=3 timed_out=0 not_asked=0 no_endpoint=0 denied=0 window=5.00s
    reasons={'no_response': 3}

Two things were wrong with that, and this file is one section for each.

**1. The screen was blank on a roster the platform vouches for.** Bisected across the four
candidate commits with ``apps/buyer/svc/tests/test_auctions_shortlist.py``: ``a338b48`` is
``13 passed``, ``060225e`` — the commit that banked the organic-relevance stream — is the first
``2 failed, 11 passed``, and ``9035181`` and ``dc78760`` inherit it unchanged. The proximate
cause was not the filter's *rule* but the surface it was handed: that suite's catalogue fixture
wrote the product REF into ``canonical_name`` and stated no ``brand``, so
``identity_surface`` folded to the single content word ``beanie`` and
:class:`~exchange.retrieval.relevance.TopicalRelevance` — which needs ``min(2, len(asked))``
agreements or half — could not be satisfied by it for a three-content-word query no matter what
product it named. Measured, because "no real source looks like that" is the whole basis for
having repaired a fixture rather than a rule, through the served
``catalog_identity`` -> ``identity_surface`` -> ``content_terms`` chain:

* ``deploy/demo/exchange-deployment.json`` — 3,086 products, every one resolving an identity,
  minimum 2 content words, **0 below 2**.
* the demo graph ``proxyshop-neo4j-1``, read live — ``MATCH (p:Product) RETURN count(p)`` = 3,093,
  ``p.brand IS NULL OR p.brand = ''`` = 0, minimum 2 content words, **0 below 2**.

Section 1 is therefore the standing gate on the honest direction: a rostered store that was
asked and stayed silent, on a product the platform's own crawl connects to the query, MUST
reach a slot at its catalogue price. That is R10, it long predates the relevance filter, and no
relevance rule may take it away.

**2. Nothing FAILED — it only logged.** The blank screen was decided inside
:func:`~exchange.auction.routes.announce_market`'s own body and rendered into a format string.
The ``201`` carried ``shortlisted: 0`` and left every caller to work out for itself whether
that was a fault or an honest answer, so the single loudest condition in the system was
readable only by whoever thought to grep for it. It is now
:attr:`~exchange.auction.routes.MarketSummaryOut.nothing_shown`, decided once in
:func:`~exchange.auction.routes.with_shortlist_outcome`, published on the response, forwarded
verbatim by the buyer service to the shopper's own route, and merely *said* by the log.
Section 3 pins that the response and the log cannot disagree about it.

**THE PRODUCT QUESTION, and the answer this file writes down.** *Should a rostered store that
was asked and stayed silent reach the shopper at its catalogue price even when the product it
is rostered for is off-topic for the query?* **No** — section 2. A silent store asserted
nothing, so the platform's own crawl is the only thing vouching for that row (D55), and a crawl
that says the product is about something else is the platform declining to vouch. That is not a
softening of R10: R10 says a silent store may reach the shortlist, not that it may reach the
shortlist for a question its product does not answer. The two rules meet exactly here, and the
red tests that provoked this were never asking the question — they were failing on a fixture
that stated an identity no crawl produces.

What that answer costs, stated rather than rounded away: a blank screen becomes a legitimate
outcome, so ``nothing_shown`` is a published fact and not a refusal. The auction that empties
because the corpus genuinely has nothing on the subject and the auction that empties because
somebody unwired the trust snapshot are told apart by the reasons on ``excluded``, which is why
every test below that asserts an empty screen also asserts why it was empty.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking import REASON_OFF_TOPIC_ORGANIC
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from fastapi.testclient import TestClient

#: A beanie roster, and the PLATFORM's crawl of each product: a readable title and a brand,
#: which is the shape ``deploy/demo/exchange-deployment.json`` and the demo graph both carry
#: for every one of their ~3,090 products. Prices differ per store so a slot's price can be
#: told apart from a constant.
ON_TOPIC_ROSTER: tuple[tuple[str, str, str, str, float], ...] = (
    ("demo-woolworks", "beanie-1", "Merino Wool Beanie", "Woolworks", 80.0),
    ("demo-northface", "beanie-2", "Ribbed Merino Beanie", "Northface", 95.0),
    ("demo-fastfashion", "beanie-3", "Warm Knit Merino Beanie", "Fastfashion", 40.0),
)

#: The same three stores, rostered on products the platform's crawl puts in another market
#: entirely. Nothing else about the auction changes — same stores, same silence, same list
#: prices — so a difference in what the shopper is shown is a difference the CRAWL made.
OFF_TOPIC_ROSTER: tuple[tuple[str, str, str, str, float], ...] = (
    ("demo-woolworks", "supp-1", "Milk Thistle Extract Capsules", "BulkSupplements.com", 80.0),
    ("demo-northface", "supp-2", "Dandelion Root, Organic Extract", "Oregon's Wild Harvest", 95.0),
    ("demo-fastfashion", "supp-3", "Glutathione 98%", "Toniiq", 40.0),
)

#: Queries a shopper would really type for the on-topic roster, from a bare keyword to a
#: sentence. The sweep matters: the rule's count arm and its share arm trade off against each
#: other as a query lengthens, and the regression this file gates was invisible to a one-word
#: query — ``"beanie"`` needs ``min(2, 1) = 1`` agreement and passed even on the broken
#: fixture, while ``"a warm merino beanie"`` needs 2 and did not.
HONEST_QUERIES = (
    "beanie",
    "a warm merino beanie",
    "im looking for a warm merino wool beanie for winter",
)


def _intent(query: str) -> dict[str, Any]:
    return {
        "intent_id": "intent-blank-shortlist",
        "cluster_id": "cluster-warm-layers",
        "query": query,
        "hard_constraints": [],
        "preferences": [],
        "currency": "USD",
        "budget_band": "unspecified",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }


def _app(roster: tuple[tuple[str, str, str, str, float], ...], *, bidding: bool):
    """An exchange over ``roster`` whose stores either bid or stay silent.

    ``bidding=False`` is the ORGANIC case and the one this file is about: every store is asked
    and answers nothing, so ``collect_bids`` marks each entry ``fallback`` and the exchange
    stands the roster's list price up in its place. Everything else is the shipped wiring.
    """

    def solicit(store: Any) -> Any:
        if not bidding:
            return None
        store_id = str(store["store_id"])
        ref, price = next((r, p) for sid, r, _, _, p in roster if sid == store_id)
        return {
            "store_id": store_id,
            "received_at": time.time(),
            "bid": {
                "auction_id": None,
                "store_id": store_id,
                "offer": {
                    "product_ref": ref,
                    "unit_price": price - 1.0,
                    "total_price": price - 1.0,
                    "currency": "USD",
                    "checkout_url": f"https://{store_id}.example.com/cart/1:1",
                    "expires_at": time.time() + 3600.0,
                },
                "claims": [],
                "agent_version": "1.0.0",
                "schema_version": "1.0.0",
            },
        }

    stores = [row[0] for row in roster]
    app = create_app()
    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={s: {"blacklisted": False, "score": 0.7, "confidence": 0.7} for s in stores},
        registered_domains=StaticRegisteredDomains({s: f"{s}.example.com" for s in stores}),
        catalog=StaticCatalogSnapshots(
            {
                store: {
                    "snapshot_id": f"snap-{store}",
                    "products": [
                        {
                            "product_ref": ref,
                            "canonical_name": title,
                            "brand": brand,
                            "evidence_ref": f"snap-{store}#{ref}",
                            "attributes": {},
                        }
                    ],
                }
                for store, ref, title, brand, _ in roster
            }
        ),
    )
    return app


def _serve(app, query: str, roster: tuple[tuple[str, str, str, str, float], ...]) -> dict[str, Any]:
    posted = TestClient(app).post(
        "/auctions",
        json={
            "intent": _intent(query),
            "bid_timeout_seconds": 2.0,
            "roster": [
                {"store_id": store, "tier": 1, "product_ref": ref, "list_price": price}
                for store, ref, _, _, price in roster
            ],
        },
    )
    assert posted.status_code == 201, posted.text
    return dict(posted.json())


def _off_topic_reasons(body: dict[str, Any]) -> list[str]:
    return [
        reason
        for row in body["excluded"]
        for reason in row["exclusion_reasons"]
        if reason.startswith(REASON_OFF_TOPIC_ORGANIC)
    ]


# =====================================================================================
# 1. THE GATE — an on-topic roster of silent stores must reach the shopper
# =====================================================================================
@pytest.mark.parametrize("query", HONEST_QUERIES)
def test_a_silent_roster_the_platform_vouches_for_still_fills_the_screen(query: str) -> None:
    """R10 over the served route, on every honest shape of the same question.

    This is the assertion that was RED, and the one that must stay red-able: three stores on
    the roster, every one asked, every one silent, and every one still shown at its catalogue
    price because the platform's own crawl says its product is a beanie and the shopper asked
    for a beanie. A relevance rule that takes this away has not filtered an off-topic result,
    it has emptied a screen the catalogue could genuinely have served — which this repo's own
    ``retrieval.relevance`` header calls a worse defect than the one it replaces.
    """
    body = _serve(_app(ON_TOPIC_ROSTER, bidding=False), query, ON_TOPIC_ROSTER)

    slots = body["shortlist"]["slots"]
    assert slots, (
        f"every store was asked, every store was silent, and the platform's own crawl calls "
        f"all three of them beanies — a blank screen for {query!r} is the regression this "
        f"file gates. excluded={body['excluded']}"
    )
    assert len(slots) == len(ON_TOPIC_ROSTER), body["shortlist"]
    assert all(slot["fallback"] for slot in slots), (
        "nobody bid, so every row must be the exchange standing in at list price"
    )
    assert not _off_topic_reasons(body), body["excluded"]

    shown = {slot["product"]["identity"]["title"] for slot in slots}
    assert shown == {title for _, _, title, _, _ in ON_TOPIC_ROSTER}, shown
    prices = {slot["price"]["unit_price"] for slot in slots}
    assert prices == {price for _, _, _, _, price in ON_TOPIC_ROSTER}, (
        "a fallback row is shown at the ROSTER's list price, not at a manufactured number"
    )

    market = body["market"]
    assert market["nothing_shown"] is False, market
    assert market["shortlisted"] == len(ON_TOPIC_ROSTER), market
    assert market["shortlisted_sponsored"] == 0, market
    assert market["all_fallback"] is True, (
        "not one row was a store's own offer, which is exactly the degraded market this flag "
        "claims — and it can only be claimed because rows reached the screen at all"
    )


# =====================================================================================
# 2. THE PRODUCT ANSWER — a silent store rostered off-topic does NOT reach the shopper
# =====================================================================================
def test_a_silent_store_rostered_on_an_off_topic_product_does_not_reach_the_shopper() -> None:
    """The answer to the question section 1 does not ask, driven rather than argued.

    Identical stores, identical silence, identical list prices, identical trust — the ONLY
    thing changed from the test above is what the platform's crawl says each rostered product
    is. Nobody bid, so nobody's own voice is on these rows: the platform picked the product and
    the platform wrote the pitch (D55), which makes the platform's own crawl the only thing
    that could vouch for them, and it says they are supplements. The screen is empty, and it is
    empty *honestly* — the alternative is three liver supplements under a query for a beanie.
    """
    body = _serve(_app(OFF_TOPIC_ROSTER, bidding=False), "a warm merino beanie", OFF_TOPIC_ROSTER)

    assert body["shortlist"]["slots"] == [], body["shortlist"]
    refusals = _off_topic_reasons(body)
    assert len(refusals) == len(OFF_TOPIC_ROSTER), body["excluded"]
    assert "not about what was asked" in refusals[0], refusals[0]

    # R10's other half is untouched, and this is what keeps the refusal honest: every rostered
    # store is still REPRESENTED on the answer, so the buyer can be shown who was asked, what
    # each of them said, and on what ground the exchange set the row aside. What R10 does not
    # buy is a slot for a product the platform will not connect to the question.
    assert len(body["entries"]) == len(OFF_TOPIC_ROSTER), body["entries"]
    assert all(entry["fallback"] is True for entry in body["entries"]), body["entries"]


def test_the_same_off_topic_product_keeps_its_slot_when_the_store_actually_BIDS() -> None:
    """The D55 asymmetry, on the same rows, so the refusal above is about VOICE not topic.

    A shop that bids is answering in its own voice for a product it chose, and it is
    accountable for that answer — its message is adversarially checked and its trust record
    moves. The platform's crawl is not the only thing vouching for it any more, so the organic
    relevance gate never looks at it. Without this test the section above would read as "the
    exchange refuses off-topic products", which is not the rule.
    """
    body = _serve(_app(OFF_TOPIC_ROSTER, bidding=True), "a warm merino beanie", OFF_TOPIC_ROSTER)

    slots = body["shortlist"]["slots"]
    assert len(slots) == len(OFF_TOPIC_ROSTER), body["shortlist"]
    assert not any(slot["fallback"] for slot in slots), body["shortlist"]
    assert not _off_topic_reasons(body), body["excluded"]
    assert body["market"]["nothing_shown"] is False, body["market"]


# =====================================================================================
# 3. NEVER SILENT — the blank screen is on the answer, not only in the log
# =====================================================================================
def test_the_blank_screen_is_published_on_the_answer_and_not_only_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One verdict, two readers, and they may not disagree.

    Before ``nothing_shown`` existed, the response said ``shortlisted: 0`` and nothing else: a
    caller could not tell an honestly empty answer from a service that had dropped every row,
    and the only place the exchange said which was a WARNING that no test outside this package
    read. A caller reduced to grepping an operator's log is a caller that cannot act on the
    fact at all — the buyer service forwards this mapping to the shopper's own route.
    """
    with caplog.at_level(logging.INFO, logger="exchange.auction.routes"):
        body = _serve(
            _app(OFF_TOPIC_ROSTER, bidding=False), "a warm merino beanie", OFF_TOPIC_ROSTER
        )

    market = body["market"]
    assert market["nothing_shown"] is True, market
    assert market["shortlisted"] == 0, market
    assert market["all_fallback"] is False, (
        "there were no catalogue prices on the screen either, so this is not the market "
        "reverting to catalogue prices — the two conditions have different fixes"
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    said = [r.getMessage() for r in warnings if "NOTHING REACHED THE SHOPPER" in r.getMessage()]
    assert said, "the operator was told nothing about a blank screen"
    assert "market=nothing_shown" in said[-1], said[-1]


def test_a_screen_that_filled_reports_nothing_shown_false_in_both_places(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The positive control. A flag that were always true would pass every test above.

    Asserted against the LOG as well as the response, because the defect this whole field
    closes is a verdict living in one reader and not the other.
    """
    with caplog.at_level(logging.INFO, logger="exchange.auction.routes"):
        body = _serve(_app(ON_TOPIC_ROSTER, bidding=True), "a warm merino beanie", ON_TOPIC_ROSTER)

    assert body["shortlist"]["slots"], body["shortlist"]
    assert body["market"]["nothing_shown"] is False, body["market"]
    assert not [r for r in caplog.records if "NOTHING REACHED THE SHOPPER" in r.getMessage()], (
        "a full screen was announced as a blank one"
    )


#: Three products out of ``deploy/demo/exchange-deployment.json`` whose crawled title is a
#: single content word, with the brand the crawl carries beside each. These are the shape that
#: emptied real screens: the shopper types the product's name, the query runs past two content
#: words, and the thresholds refuse by arithmetic. 104 of the document's 3,086 products are
#: like this.
NAMED_ROSTER: tuple[tuple[str, str, str, str, float], ...] = (
    ("gaiaherbs.com", "prod-bacopa", "Bacopa", "Gaia Herbs", 24.99),
    ("gaiaherbs.com-2", "prod-resveratrol", "Resveratrol", "Gaia Herbs", 31.99),
    ("toniiq.com", "prod-glutathione", "Glutathione 98%", "Toniiq", 20.97),
)


def test_a_silent_store_whose_product_the_shopper_NAMED_reaches_the_shopper() -> None:
    """The false refusal, over the served route rather than against the rule in isolation.

    Every store on this roster is asked, every one is silent, and the shopper typed the
    platform's own name for one of the products and then kept talking the way shoppers do.
    Before :func:`~exchange.ranking.filters.shopper_named_the_product` the whole screen went
    blank — ``Bacopa`` cannot supply the two agreeing content words the thresholds want,
    because it only HAS one and the shopper never typed the brand.

    This drives ``POST /auctions`` because that is where the defect was reachable: the rule,
    the gate, the ranker and the shortlist builder each behaved exactly as documented, and the
    shopper still got nothing.
    """
    query = "bacopa best one for daily use under $30"
    body = _serve(_app(NAMED_ROSTER, bidding=False), query, NAMED_ROSTER)

    slots = body["shortlist"]["slots"]
    assert slots, (
        f"the shopper typed this product's own crawled name and was shown nothing. "
        f"excluded={body['excluded']}"
    )
    shown = {slot["product"]["identity"]["title"] for slot in slots}
    assert "Bacopa" in shown, shown
    assert all(slot["fallback"] for slot in slots), "nobody bid, so every row is a list price"
    assert body["market"]["nothing_shown"] is False, body["market"]

    # The two rows the shopper did NOT name are still judged on the thresholds, and the
    # thresholds still refuse them — so this is a keep-condition on one row, not the gate being
    # switched off for the auction.
    refusals = _off_topic_reasons(body)
    assert len(refusals) == len(NAMED_ROSTER) - 1, body["excluded"]
    assert "Resveratrol" not in shown and "Glutathione 98%" not in shown, shown


#: The same shape as :data:`NAMED_ROSTER`, on the two products whose one crawled content word
#: is ORDINARY ENGLISH rather than a botanical name. Both are real rows of
#: ``deploy/demo/exchange-deployment.json``: ``Iron+`` folds to ``iron`` and ``SAMe Bulk`` folds
#: to ``bulk``, because ``same`` is a stopword.
CONTAINMENT_ROSTER: tuple[tuple[str, str, str, str, float], ...] = (
    ("livemomentous.com", "prod-iron", "Iron+", "Momentous", 29.95),
    ("purebulk.com", "prod-same-bulk", "SAMe Bulk", "PureBulk, Inc.", 41.96),
)

#: Off-corpus queries that happen to carry one of those two words. These are the three the
#: skeptic drove, and they are the reason
#: :func:`~exchange.ranking.filters.shopper_named_the_product` is a position test and not a
#: containment test: at ``fa900c4`` each of them put a supplement on the shopper's screen at
#: its catalogue price.
CONTAINMENT_LEAK_QUERIES = (
    "cast iron skillet for camping",
    "an iron bed frame queen size",
    "bulk storage bins for the garage",
)


@pytest.mark.parametrize("query", CONTAINMENT_LEAK_QUERIES)
def test_an_off_corpus_query_carrying_a_products_whole_name_shows_nothing(query: str) -> None:
    """THE LEAK, over the served route, because that is where it was reachable.

    Driven at ``fa900c4`` against this exact roster, ``POST /auctions`` answered::

        "cast iron skillet for camping"     -> Iron+      at $29.95
        "an iron bed frame queen size"      -> Iron+
        "bulk storage bins for the garage"  -> SAMe Bulk

    On the stated-roster route the organic gate is the ONLY per-query relevance defence — the
    roster is deployment data, not a retrieval result, so nothing upstream has asked whether
    these products answer the question. A keep-condition that fires on plain containment
    hands that defence away for every product whose crawled title is one ordinary word, and
    103 of the document's 3,086 products are exactly that.

    Both stores are refused here, and the screen is honestly empty rather than wrong.
    """
    body = _serve(_app(CONTAINMENT_ROSTER, bidding=False), query, CONTAINMENT_ROSTER)

    assert body["shortlist"]["slots"] == [], (
        f"{query!r} is not about a supplement, and the shopper was shown "
        f"{[s['product']['identity']['title'] for s in body['shortlist']['slots']]}"
    )
    assert len(_off_topic_reasons(body)) == len(CONTAINMENT_ROSTER), body["excluded"]
    assert body["market"]["nothing_shown"] is True, body["market"]


@pytest.mark.parametrize(
    "query",
    (
        "iron best one for daily use under $30",
        "iron supplement for anemia",
        "same bulk best one for daily use under $30",
    ),
)
def test_the_same_roster_still_answers_a_shopper_who_asked_for_the_product(query: str) -> None:
    """The honest direction on the identical roster, and it is the half that must not move.

    Same two stores, same silence, same one-word titles — and here the shopper LED with the
    product's own crawled name. Each of these is refused by the thresholds on arithmetic (one
    agreeing word out of three), so each of them reaches the screen only because the gate
    declines to apply them. Without this half the fix for the leak above is just the revert,
    and the revert puts 104 false refusals back.
    """
    body = _serve(_app(CONTAINMENT_ROSTER, bidding=False), query, CONTAINMENT_ROSTER)

    slots = body["shortlist"]["slots"]
    assert slots, f"the shopper asked for this product by name: excluded={body['excluded']}"
    assert all(slot["fallback"] for slot in slots), "nobody bid, so every row is a list price"
    assert body["market"]["nothing_shown"] is False, body["market"]


def test_an_auction_with_nothing_to_represent_does_not_claim_a_blank_screen(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R12 fail-closed: an unconfigured exchange refuses everybody, and that is not this.

    ``nothing_shown`` means *there were stores to represent and none of them reached the
    screen*. An exchange that denied every store before anyone was asked has no such store, no
    entry and no list price — a different condition with a different fix, and claiming a blank
    screen on it would fire this flag on every request such a deployment correctly refuses,
    which is the one way to make an alarm worth ignoring.
    """
    app = create_app()
    configure_auctions(app, solicitor=lambda store: None, eligibility=None)
    configure_ranking(app, trust_snapshot={}, registered_domains=StaticRegisteredDomains({}))

    with caplog.at_level(logging.INFO, logger="exchange.auction.routes"):
        posted = TestClient(app).post(
            "/auctions",
            json={
                "intent": _intent("a warm merino beanie"),
                "bid_timeout_seconds": 1.0,
                "roster": [
                    {"store_id": store, "tier": 1, "product_ref": ref, "list_price": price}
                    for store, ref, _, _, price in ON_TOPIC_ROSTER
                ],
            },
        )
    assert posted.status_code == 201, posted.text
    market = posted.json()["market"]

    assert market["solicited"] == 0, market
    assert market["list_price"] == 0, market
    assert market["shortlisted"] == 0, market
    assert market["nothing_shown"] is False, (
        "nobody was represented, so nothing failed to reach the shopper"
    )
    assert not [r for r in caplog.records if "NOTHING REACHED THE SHOPPER" in r.getMessage()]
