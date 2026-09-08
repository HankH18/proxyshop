"""A QUERY NOBODY BID ON STILL GETS USEFUL RESULTS — or an honest empty answer (D55).

The defect these tests pin, measured on the served route before any of this existed, with the
recorded corpus in Neo4j (3,093 products, ten real supplement storefronts):

    POST /auctions  {"query": "a walnut coffee table for the lounge"}   (graph roster)
        -> roster_source {"source": "neo4j", "shops": 4, "products_considered": 25}
        -> 4 slots: The Capsule Machine, Nutricost Protein for Women, Nattokinase, ...

    POST /buyer/intent/confirm  "a walnut coffee table for the lounge"  (request roster)
        -> 4 slots, all fallback: Milk Thistle Gummies, Dandelion Root, Milk Thistle,
           Glutathione 98%

Nothing raised, nothing was empty, and every row was wrong. A vector index answers every query
with its top ``k``, and a roster is a fixed list, so "we have nothing on this" and "here are
the four best" arrive in exactly the same shape. That is worse than an empty answer: it is a
confident one.

**The honest direction is graded as hard as the attack**, because a relevance filter that
empties a shortlist the catalogue could genuinely have served is a worse defect than the one it
replaces. Two whole sections are about queries that must KEEP their rows —
:func:`test_a_product_the_catalogue_genuinely_serves_is_about_the_query` runs a fixture corpus
of real queries against real product names out of the shipped catalogue,
:func:`test_a_sponsored_shortlist_keeps_all_four_slots` drives the three queries the owner
named over the served route, and :func:`test_a_sponsored_row_is_never_refused_for_relevance` is
the D55 asymmetry that makes those three whole.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking import REASON_OFF_TOPIC_ORGANIC, rank
from exchange.ranking.filters import organic_relevance_reason
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from exchange.retrieval import (
    MIN_SHARED_TERMS,
    CandidateRetrieval,
    InMemoryCandidateSource,
    TopicalRelevance,
    content_terms,
)
from exchange.retrieval.fit import FitAssessment, FitFeatures
from exchange.retrieval.relevance import OFF_TOPIC_DETAIL
from exchange.retrieval.roster import (
    ShopRoster,
    SolicitedShop,
    _nothing_retrieved_reason,
    repoint_organic_products,
)
from exchange.retrieval.service import ExcludedCandidate, RetrievalResult
from fastapi.testclient import TestClient

RULE = TopicalRelevance()


# =====================================================================================
# 1. The rule itself, in both directions
# =====================================================================================
#: Real product names out of ``deploy/demo/exchange-deployment.json`` and the recorded
#: corpus, paired with queries the catalogue genuinely serves. Every one of these must be
#: kept: this half is the false-positive corpus, and a rule that only ever refuses would pass
#: the section below it and fail every shopper.
SERVED_PAIRS = (
    ("milk thistle", "Milk Thistle Gummies"),
    (
        "milk thistle silymarin liver support extract under $50",
        "Milk Thistle Extract (80% Silymarin)",
    ),
    ("liver support supplement", "Grass Fed Beef Liver Supplement"),
    ("something for liver health", "Liver Health"),
    ("zinc lozenges", "Zinc Oxide"),
    ("electrolyte powder", "Nutricost Taurine Powder"),
    ("biotin for hair", "Nutricost Biotin for Women"),
    ("dandelion root tea", "Dandelion Root Capsules"),
    ("probiotics for gut health", "Gut Health Stack"),
    ("a good multivitamin for women", "Multivitamin For Women Softgels"),
    ("vitamin d3 5000 iu", "Vitamin D3 5000 IU"),
    ("omega 3 fish oil", "Omega-3 Fish Oil"),
    ("b12 methylcobalamin", "Nutricost Organic Vitamin B12 (Methylcobalamin) Liquid Drops"),
    ("something to help my joints", "Nutricost Joint Support Capsules"),
    ("apple cider vinegar capsules", "Nutricost Apple Cider Vinegar Capsules"),
    # A long, conversational query. The SHARE arm would refuse this one — four matches out of
    # nine content words is 0.44 — and the COUNT arm keeps it. A shopper is not penalised for
    # typing a sentence.
    (
        "im looking for a high strength milk thistle supplement for liver support that is vegan",
        "Nutricost Organic Milk Thistle Powder",
    ),
)

#: Queries this catalogue has nothing for, paired with the row the retriever actually put at
#: the top for them (measured, not invented — see the module header). Every one of these is
#: exactly the confidently-wrong answer the shopper used to be given.
OFF_CORPUS_PAIRS = (
    ("a walnut coffee table for the lounge", "Caffeine Powder Pure (Natural Coffee Bean)"),
    ("a walnut coffee table for the lounge", "Milk Thistle Gummies"),
    ("running shoes for trail marathons", "Multivitamin for Men | Naked Men's Multi"),
    ("a laptop for video editing", "Palmitate (Vitamin A)"),
    ("noise cancelling headphones", "Nutricost Manganese Capsules"),
    ("a leather sofa for the living room", "The Capsule Machine"),
    ("winter tyres for a hatchback", "Winter Wellness Trio"),
    ("an espresso machine with a milk frother", "Nutricost Frother"),
    ("kids lego star wars set", "Nutricost Kids Fiber Gummies"),
    ("a gaming chair with lumbar support", "Testosterone Support"),
    ("a used ford focus", "NutriZen Focus"),
    ("a stand mixer for baking", "Nutricost Baking Soda"),
    ("dog food for a labrador puppy", "Nutricost Protein for Women"),
    ("curtains for a bay window", "Nutricost Vitamin A Softgels"),
    ("plane tickets to tokyo", "NMN Complex 12-in-1"),
)


@pytest.mark.parametrize(("query", "name"), SERVED_PAIRS)
def test_a_product_the_catalogue_genuinely_serves_is_about_the_query(query: str, name: str) -> None:
    """THE HONEST DIRECTION. Every one of these must survive, and this half comes first.

    A gate written pointing only at the attack passes its own tests and refuses real traffic in
    the direction nobody drove. These twenty pairs are the shipped catalogue's own product
    names against queries a shopper would type at a supplements marketplace.
    """
    verdict = RULE.judge(query, name)
    assert verdict.about, verdict
    assert verdict.decidable, verdict
    assert verdict.matched, verdict


@pytest.mark.parametrize(("query", "name"), OFF_CORPUS_PAIRS)
def test_a_product_from_another_market_is_not_about_the_query(query: str, name: str) -> None:
    """The attack direction: the row the retriever actually returned, refused with a reason."""
    verdict = RULE.judge(query, name)
    assert not verdict.about, verdict
    assert verdict.decidable, verdict
    assert len(verdict.matched) < MIN_SHARED_TERMS, verdict
    assert "not about what was asked" in verdict.detail, verdict.detail


def test_one_coincidental_word_is_not_agreement_and_two_is() -> None:
    """The threshold itself, on the pair that decided it.

    ``Nutricost Frother`` and ``an espresso machine with a milk frother`` share exactly one
    content word. Measured over 30 off-corpus queries through the real vector index against the
    recorded corpus alone, a rule that accepted ONE shared word served 7 of them where this rule
    serves 0, and bought exactly one extra honest query for it — the full ablation is in
    ``exchange.retrieval.relevance``'s
    header. A single word in common is what an unrelated query and a catalogue of 3,093 products
    share by accident.
    """
    one = RULE.judge("an espresso machine with a milk frother", "Nutricost Frother")
    assert one.matched == ("frother",), one
    assert not one.about, one

    two = RULE.judge("an espresso machine with a milk frother", "Frother Machine")
    assert set(two.matched) == {"machine", "frother"}, two
    assert two.about, two


def test_a_stopword_in_common_is_not_a_reason_to_show_a_product() -> None:
    """``for`` is not agreement. Without the stopword list this pair reads as a match."""
    assert content_terms("a walnut coffee table for the lounge") == (
        "walnut",
        "coffee",
        "table",
        "lounge",
    )
    assert not RULE.judge(
        "a walnut coffee table for the lounge", "Nutricost Protein for Women"
    ).about


def test_a_plural_query_still_finds_a_singular_catalogue() -> None:
    """``joints``/``joint`` and ``capsules``/``capsule``: morphology is not a topic change.

    The two whole-query assertions are the shipped shape, and neither of them ISOLATES the
    morphology: ``milk thistle capsule`` against ``Milk Thistle Capsules`` passes on ``milk``
    and ``thistle`` whatever the stemmer does with the third word. The isolated pair below is
    what actually pins it, and it was red before the ``-es`` rule was made conditional —
    ``capsules`` stemmed to ``capsul`` while ``capsule`` stayed whole, so the singular and its
    own plural were different words.
    """
    assert RULE.judge("something to help my joints", "Nutricost Joint Support Capsules").about
    assert RULE.judge("milk thistle capsule", "Milk Thistle Capsules").about
    assert RULE.judge("capsule", "Capsules").about, "the plural of the query's own word"
    assert RULE.judge("joints", "Joint").about


#: Singular/plural pairs the rule must treat as one word. The second column is the class the
#: blanket ``-es`` rule split apart — every English noun whose singular ends in ``-e`` — and
#: the third is the sibilant class that genuinely takes ``-es`` and must still fold.
@pytest.mark.parametrize(
    ("singular", "plural"),
    [
        ("joint", "joints"),
        ("gummy", "gummies"),
        ("vitamin", "vitamins"),
        ("capsule", "capsules"),
        ("peptide", "peptides"),
        ("lozenge", "lozenges"),
        ("tincture", "tinctures"),
        ("bottle", "bottles"),
        ("table", "tables"),
        ("machine", "machines"),
        ("box", "boxes"),
        ("dish", "dishes"),
        ("church", "churches"),
        ("glass", "glasses"),
    ],
)
def test_a_singular_and_its_own_plural_are_one_content_word(singular: str, plural: str) -> None:
    """One word, whichever way the shopper or the storefront spells it."""
    assert content_terms(singular) == content_terms(plural), (singular, plural)


@pytest.mark.parametrize("word", ["was", "its", "gas", "this", "yes", "mass", "gras", "news"])
def test_a_short_word_that_merely_ends_in_s_is_not_treated_as_a_plural(word: str) -> None:
    """The length guard, and the ``-ss`` guard that keeps ``mass`` from becoming ``mas``."""
    assert content_terms(word) in ((word,), ()), word


def test_a_genuinely_relevant_product_is_not_refused_over_a_plural() -> None:
    """The concrete refusal the ``-es`` rule cost, isolated.

    Measured on the served route before the fix, against the recorded corpus: ``"bovine
    collagen peptides"`` returned 4 slots from 6 shops and ``"bovine collagen peptide"`` — the
    same question, singular — returned 2 from 2, because the query folded to ``peptide`` and
    the catalogue folded to ``peptid``.
    """
    verdict = RULE.judge("collagen peptide powder tub", "Collagen Peptides")
    assert verdict.about, verdict
    assert set(verdict.matched) == {"collagen", "peptide"}, verdict


def test_the_plural_of_a_stopword_is_still_a_stopword() -> None:
    """``STOPWORDS`` says it drops from BOTH sides; the plural used to survive as content.

    ``type`` is a stopword and ``types`` was not, so ``"types of collagen"`` carried a ``type``
    term that ``"type of collagen"`` did not, and the rule's answer depended on the shopper's
    plural. The membership test runs on the typed word AND on the stemmed one now.
    """
    assert content_terms("types ii collagen") == content_terms("type ii collagen")
    assert "type" not in content_terms("what types of collagen")


def test_a_stopword_that_stems_to_a_non_stopword_is_still_dropped() -> None:
    """The reason the typed spelling is tested first and on its own.

    ``these`` stems to ``thes``, which is in no list. Testing only the stemmed form would have
    let every ``these`` through as a content word.
    """
    assert content_terms("these gummies") == ("gummy",)


def test_the_refusal_does_not_say_who_chose_the_product() -> None:
    """D55: the served sentence claims what is true of every organic row, and no more.

    ``POST /auctions`` takes a ``roster`` in the request body — that is how buyer-svc drives it
    — so "the platform, not the shop, chose it" is false for a caller-supplied row. What is
    true of every row this fires on is that no shop bid for it.
    """
    assert "chose it" not in OFF_TOPIC_DETAIL, OFF_TOPIC_DETAIL
    assert "no shop bid for it" in OFF_TOPIC_DETAIL, OFF_TOPIC_DETAIL
    assert OFF_TOPIC_DETAIL in RULE.judge("a walnut coffee table", "Milk Thistle").detail


def test_a_query_with_no_content_words_refuses_nothing() -> None:
    """Undecidable is KEPT, and says so. The first of three fail-open cases."""
    verdict = RULE.judge("something for the best", "Milk Thistle Gummies")
    assert verdict.about and not verdict.decidable, verdict
    assert "no content words" in verdict.detail, verdict.detail


def test_a_product_the_platform_never_observed_refuses_nothing() -> None:
    """The second: an exchange holding no crawled identity checks nothing and refuses nothing."""
    verdict = RULE.judge("a walnut coffee table", "")
    assert verdict.about and not verdict.decidable, verdict
    assert "no readable record" in verdict.detail, verdict.detail


def test_the_rule_refuses_to_be_configured_into_a_no_op() -> None:
    """A threshold of zero shared words is a filter that is wired and switched off."""
    with pytest.raises(ValueError):
        TopicalRelevance(min_shared_terms=0)
    with pytest.raises(ValueError):
        TopicalRelevance(min_shared_share=0.0)


# =====================================================================================
# 2. Retrieval — the organic half, over the deterministic double
# =====================================================================================
SUPPLEMENTS = (
    {"product_id": "p-milk", "canonical_name": "Milk Thistle Gummies", "brand": "Gaia Herbs"},
    {"product_id": "p-thistle", "canonical_name": "Milk Thistle", "brand": "Paradise Herbs"},
    {"product_id": "p-dandelion", "canonical_name": "Dandelion Root, Organic Extract"},
    {"product_id": "p-glut", "canonical_name": "Glutathione 98%", "brand": "Toniiq"},
)


def _intent(query: str) -> dict[str, Any]:
    return {
        "intent_id": "intent-relevance",
        "cluster_id": "cluster-1",
        "query": query,
        "hard_constraints": [],
        "preferences": [],
        "currency": "USD",
        "budget_band": "unspecified",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }


def test_retrieval_returns_nothing_for_an_off_corpus_query_and_reports_every_refusal() -> None:
    """The honest empty answer, and the reason that makes it honest rather than merely short.

    The source is deliberately LENIENT — it hands back its whole catalogue whatever was asked,
    which is what a top-``k`` vector index does — so this is the exact shape the served route
    produced. Every refusal lands on ``off_topic`` with its own sentence; an empty result whose
    emptiness cannot be explained is the same defect wearing a shorter list.
    """
    retrieval = CandidateRetrieval(InMemoryCandidateSource(SUPPLEMENTS))
    result = retrieval.retrieve(_intent("a walnut coffee table for the lounge"))

    assert result.assessments == (), result.assessments
    assert result.considered == 4
    assert {row.product_id for row in result.off_topic} == {
        "p-milk",
        "p-thistle",
        "p-dandelion",
        "p-glut",
    }
    assert result.excluded == (), "an off-topic product is not a hard-constraint failure"
    assert result.relevance == TopicalRelevance.name
    for row in result.off_topic:
        assert "not about what was asked" in row.reasons[0], row


def test_retrieval_keeps_what_the_catalogue_genuinely_answers() -> None:
    """The same source, the same rule, a query this catalogue serves — three rows survive.

    ``Glutathione 98%`` is the one that goes, and it is the honest call: the platform's own
    record of it says nothing about milk or thistle, so the platform will not manufacture a
    pitch connecting it to this query. A shopper gets three results that are about what they
    asked instead of four of which one is not.
    """
    retrieval = CandidateRetrieval(InMemoryCandidateSource(SUPPLEMENTS))
    result = retrieval.retrieve(_intent("milk thistle"))

    assert set(result.product_ids) == {"p-milk", "p-thistle"}, result.product_ids
    assert {row.product_id for row in result.off_topic} == {"p-dandelion", "p-glut"}


def test_the_relevance_filter_runs_after_the_hard_filter_and_never_double_reports() -> None:
    """A product refused for a must-have is not ALSO reported as off-topic.

    The two sentences say opposite things to a shopper — "your must-have excluded this" versus
    "this catalogue is about something else" — and a row in both lists would tell them both.
    """
    retrieval = CandidateRetrieval(
        InMemoryCandidateSource(
            [
                {
                    "product_id": "p-walnut",
                    "canonical_name": "Walnut Coffee Table",
                    "attributes": {"finish": "matte"},
                }
            ]
        )
    )
    intent = _intent("a walnut coffee table for the lounge")
    intent["hard_constraints"] = [{"field": "finish", "op": "eq", "value": "gloss"}]
    result = retrieval.retrieve(intent)

    assert result.assessments == ()
    assert [row.product_id for row in result.excluded] == ["p-walnut"]
    assert result.off_topic == (), "a hard-constraint failure was reported as off-topic too"


def test_an_off_topic_result_is_not_reachable_through_fit_for() -> None:
    """``fit_for`` names where a product went, so a refused one cannot read as unretrieved."""
    retrieval = CandidateRetrieval(InMemoryCandidateSource(SUPPLEMENTS))
    result = retrieval.retrieve(_intent("a walnut coffee table"))
    with pytest.raises(KeyError) as raised:
        result.fit_for("p-milk")
    assert "off_topic" in str(raised.value), raised.value


# =====================================================================================
# 3. Ranking — the D55 asymmetry, on the served projection
# =====================================================================================
STORE = "gaiaherbs.com"
DOMAIN = "gaiaherbs.com"
IDENTITY = {"title": "Milk Thistle Gummies", "brand": "Gaia Herbs", "source": "snap-gaiaherbs.com"}


def _candidate(*, fallback: bool) -> dict[str, Any]:
    return {
        "bid_id": "bid-1",
        "store_id": STORE,
        "store_domain": DOMAIN,
        "fallback": fallback,
        "fallback_reason": "store_declined:cluster_not_pursued" if fallback else None,
        "offer": {
            "product_ref": "prod-milk",
            "unit_price": 25.49,
            "total_price": 25.49,
            "currency": "USD",
            "checkout_url": f"https://{DOMAIN}/cart/1:1",
            "expires_at": time.time() + 3600.0,
        },
        "claims": [],
        "intent_match": 0.48,
    }


TRUST = {STORE: {"blacklisted": False, "score": 0.77}}
WALNUT = _intent("a walnut coffee table for the lounge")


def test_an_organic_row_the_platform_cannot_connect_to_the_query_is_excluded() -> None:
    """The row the shopper was shown four of, refused with a prefix a loss report can read."""
    ranked = rank(
        [_candidate(fallback=True)],
        WALNUT,
        TRUST,
        {"now": time.time(), "auction_id": "auction-1"},
        product_identities={STORE: IDENTITY},
    )
    (row,) = ranked["candidates"]
    assert row["eligible"] is False, row
    assert any(r.startswith(REASON_OFF_TOPIC_ORGANIC) for r in row["exclusion_reasons"]), row
    assert ranked["ranked"] == []
    assert ranked["shortlist"]["slots"] == []


def test_a_sponsored_row_is_never_refused_for_relevance() -> None:
    """D55's asymmetry, and the thing that keeps the honest queries whole.

    The SAME candidate, the SAME product, the SAME query — the only difference is that a store
    bid it. A sponsored row was solicited because the platform assigned this intent to a
    cluster that store pursues and the store chose what to put forward in its own voice; the
    store is accountable for it and its message is adversarially checked. The platform does not
    get to second-guess a cluster it assigned itself.

    Measured on the demo roster, this is why ``"milk thistle"``, ``"milk thistle silymarin
    liver support extract under $50"`` and ``"liver support supplement"`` still return four
    sponsored slots each — two of those four products share no word with those queries.
    """
    ranked = rank(
        [_candidate(fallback=False)],
        WALNUT,
        TRUST,
        {"now": time.time(), "auction_id": "auction-1"},
        product_identities={STORE: IDENTITY},
    )
    (row,) = ranked["candidates"]
    assert row["eligible"] is True, row["exclusion_reasons"]
    assert len(ranked["shortlist"]["slots"]) == 1


def test_a_caller_that_holds_no_catalogue_refuses_nothing() -> None:
    """``product_identities`` absent, or holding no row for this store: unchecked, never refused.

    Both are the fail-open direction, and both are asserted because they are what an exchange
    with an unwired catalogue actually looks like — the misconfiguration must not read as "this
    catalogue serves nothing".
    """
    config = {"now": time.time(), "auction_id": "auction-1"}
    unstated = rank([_candidate(fallback=True)], WALNUT, TRUST, config)
    assert unstated["candidates"][0]["eligible"] is True, unstated["candidates"][0]

    empty = rank([_candidate(fallback=True)], WALNUT, TRUST, config, product_identities={})
    assert empty["candidates"][0]["eligible"] is True, empty["candidates"][0]

    other = rank(
        [_candidate(fallback=True)],
        WALNUT,
        TRUST,
        config,
        product_identities={"someone-else.com": IDENTITY},
    )
    assert other["candidates"][0]["eligible"] is True, other["candidates"][0]


def test_an_organic_row_about_the_query_is_kept() -> None:
    """The honest direction at the ranking layer: same fallback row, a query it answers."""
    ranked = rank(
        [_candidate(fallback=True)],
        _intent("milk thistle"),
        TRUST,
        {"now": time.time(), "auction_id": "auction-1"},
        product_identities={STORE: IDENTITY},
    )
    assert ranked["candidates"][0]["eligible"] is True, ranked["candidates"][0]
    assert len(ranked["shortlist"]["slots"]) == 1


def test_the_filter_reads_the_platforms_crawl_and_not_the_stores_own_words() -> None:
    """D55: a shop cannot assert its way onto an organic slot.

    The candidate's own claims and pitch say ``walnut coffee table`` in as many words. The
    filter is handed the platform's crawled identity and nothing else, so the row is still
    refused — which is the whole reason the identity comes from
    ``catalogue_readings`` rather than off the bid.
    """
    candidate = _candidate(fallback=True)
    candidate["claims"] = [{"key": "product_type", "value": "walnut coffee table lounge"}]
    candidate["message"] = "the finest walnut coffee table for any lounge"
    reason = organic_relevance_reason(
        candidate,
        query_text="a walnut coffee table for the lounge",
        identity=IDENTITY,
        relevance=RULE,
    )
    assert reason is not None and reason.startswith(REASON_OFF_TOPIC_ORGANIC), reason


# =====================================================================================
# 4. The served route — POST /auctions, the demo's own roster shape
# =====================================================================================
#: The four hosted demo stores and the products they are rostered on, verbatim from
#: ``deploy/demo/buyer-roster.json`` and ``deploy/demo/exchange-deployment.json``. This is the
#: roster the reproduction in the module header was measured against.
DEMO_ROSTER = (
    ("gaiaherbs.com", "prod-milk-gummies", "Milk Thistle Gummies", 25.49),
    ("oregonswildharvest.com", "prod-dandelion", "Dandelion Root, Organic Extract", 18.95),
    ("paradiseherbs.com", "prod-thistle", "Milk Thistle", 11.99),
    ("toniiq.com", "prod-glutathione", "Glutathione 98%", 20.97),
)


def _served_app(*, bidding: bool):
    """An exchange over the demo roster where the stores either bid or decline.

    ``bidding=False`` is the ORGANIC case and is what the reproduction was: every store answers
    ``204 cluster_not_pursued``, so ``collect_bids`` marks every entry ``fallback`` and the
    exchange stands the roster's list price up in each one's place. Nothing about the request
    changes; only who authored the rows.
    """

    def solicit(store):
        if not bidding:
            return None
        store_id = str(store["store_id"])
        product = next(ref for sid, ref, _, _ in DEMO_ROSTER if sid == store_id)
        price = next(p for sid, _, _, p in DEMO_ROSTER if sid == store_id)
        return {
            "store_id": store_id,
            "received_at": time.time(),
            "bid": {
                "auction_id": None,
                "store_id": store_id,
                "offer": {
                    "product_ref": product,
                    "unit_price": price - 1.0,
                    "total_price": price - 1.0,
                    "currency": "USD",
                    "checkout_url": f"https://{store_id}/cart/1:1",
                    "expires_at": time.time() + 3600.0,
                },
                "claims": [],
                "agent_version": "1.0.0",
                "schema_version": "1.0.0",
            },
        }

    stores = [row[0] for row in DEMO_ROSTER]
    app = create_app()
    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={s: {"blacklisted": False, "score": 0.7} for s in stores},
        registered_domains=StaticRegisteredDomains({s: s for s in stores}),
        catalog=StaticCatalogSnapshots(
            {
                store: {
                    "snapshot_id": f"snap-{store}",
                    "products": [
                        {
                            "product_ref": ref,
                            "canonical_name": name,
                            "evidence_ref": f"snap-{store}#{ref}",
                            "attributes": {},
                        }
                    ],
                }
                for store, ref, name, _ in DEMO_ROSTER
            }
        ),
    )
    return app


def _serve(app, query: str):
    body = {
        "intent": _intent(query),
        "bid_timeout_seconds": 2.0,
        "roster": [
            {"store_id": store, "tier": 1, "product_ref": ref, "list_price": price}
            for store, ref, _, price in DEMO_ROSTER
        ],
    }
    posted = TestClient(app).post("/auctions", json=body)
    assert posted.status_code == 201, posted.text
    return posted.json()


def test_the_served_route_answers_a_furniture_query_with_nothing_and_says_why() -> None:
    """THE REPRODUCTION, over a real request. Four wrong slots become zero and a reason.

    Everything here is the shipped route: ``POST /auctions``, the real roster shape, the real
    fan-out, the real catalogue snapshots, the real ranker and the real shortlist builder. The
    only thing driven is that the four stores decline, which is what they actually did — the
    cluster is not theirs, so no merchant may bid, and the owner is right that that is correct
    behaviour. What the shopper used to be given for it was four liver supplements.
    """
    body = _serve(_served_app(bidding=False), "a walnut coffee table for the lounge")

    assert body["shortlist"]["slots"] == [], body["shortlist"]
    assert body["ranked"] == [], body["ranked"]
    # R10 is untouched: every rostered store is still REPRESENTED, so the buyer can be shown
    # who was asked and what each of them said.
    assert len(body["entries"]) == len(DEMO_ROSTER), body["entries"]
    refusals = [
        reason
        for row in body["excluded"]
        for reason in row["exclusion_reasons"]
        if reason.startswith(REASON_OFF_TOPIC_ORGANIC)
    ]
    assert len(refusals) == len(DEMO_ROSTER), body["excluded"]
    assert "not about what was asked" in refusals[0], refusals[0]


def test_the_served_route_still_answers_the_queries_this_catalogue_serves() -> None:
    """The honest direction over the same route: an organic query that IS answered.

    ``Glutathione 98%`` is refused and the three milk-thistle/dandelion rows are not — which is
    the point of the whole stream. An off-corpus query gets nothing; a real one gets the rows
    that are actually about it, from stores that never bid a penny.
    """
    body = _serve(_served_app(bidding=False), "milk thistle")

    titles = {
        slot["product"]["identity"]["title"]
        for slot in body["shortlist"]["slots"]
        if slot.get("product")
    }
    assert "Milk Thistle Gummies" in titles, body["shortlist"]
    assert "Milk Thistle" in titles, body["shortlist"]
    assert "Glutathione 98%" not in titles, body["shortlist"]
    assert all(slot["fallback"] for slot in body["shortlist"]["slots"]), body["shortlist"]


@pytest.mark.parametrize(
    "query",
    [
        "milk thistle",
        "milk thistle silymarin liver support extract under $50",
        "liver support supplement",
    ],
)
def test_a_sponsored_shortlist_keeps_all_four_slots(query: str) -> None:
    """The three queries that must not lose a row, driven over the served route.

    Two of these four products — ``Dandelion Root, Organic Extract`` and ``Glutathione 98%`` —
    share no content word with ``"milk thistle"``. They keep their slots because their stores
    BID: the platform assigned the cluster, the store chose the product, and the store's own
    message is what the shopper reads. This is the assertion that would go red if the relevance
    filter were ever widened onto the sponsored half.
    """
    body = _serve(_served_app(bidding=True), query)

    assert len(body["shortlist"]["slots"]) == len(DEMO_ROSTER), body["shortlist"]
    assert not any(slot["fallback"] for slot in body["shortlist"]["slots"]), body["shortlist"]
    assert not any(
        reason.startswith(REASON_OFF_TOPIC_ORGANIC)
        for row in body["excluded"]
        for reason in row["exclusion_reasons"]
    ), body["excluded"]


# =====================================================================================
# 5. The empty answer's own sentence, and the roster that made it necessary
# =====================================================================================
def _retrieval_result(*, considered: int, off_topic: int) -> RetrievalResult:
    """A retrieval that reached ``considered`` products and refused ``off_topic`` of them."""
    return RetrievalResult(
        intent_id="int-1",
        assessments=(),
        excluded=(),
        considered=considered,
        eligible_count=0,
        elapsed_ms=1.0,
        budget_ms=100.0,
        source="graph",
        reranker="identity",
        off_topic=tuple(
            ExcludedCandidate(f"p-{index}", f"Product {index}", ("off topic",))
            for index in range(off_topic)
        ),
        relevance=RULE.name,
    )


def test_the_empty_answer_claims_only_what_the_search_actually_reached() -> None:
    """A statement about 25 rows must not be served as a statement about 3,093.

    MEASURED on ``POST /auctions`` against the recorded corpus before this: the query
    ``"something to help my joints"`` was answered "This is an answer about the catalogue, not
    a failure to search it — the products this exchange holds are about something else", while
    the catalogue held four ACTIVE products whose platform-crawled name carries ``Joint`` and
    this module's own rule accepts them. The retriever's top-25 window missed them, which is a
    recall failure and not a fact about the catalogue — and the exchange asserted the opposite
    in the platform's own voice, on a served response.
    """
    sentence = _nothing_retrieved_reason(_retrieval_result(considered=25, off_topic=25))

    assert "not a failure to search it" not in sentence, sentence
    assert "the products this exchange holds are about something else" not in sentence, sentence
    assert "nothing this search reached" in sentence, sentence
    assert "not about the whole catalogue" in sentence, sentence
    assert "25 product(s)" in sentence, sentence
    # The rule that produced the verdict is still named, because an audit has to be able to say
    # WHICH rule refused. That half was right and is not being loosened here.
    assert RULE.name in sentence, sentence


def _solicited_roster(
    *shops: SolicitedShop, vouched: tuple[str, ...] = ()
) -> ShopRoster:
    """A graph solicitation naming ``shops``, having vouched for ``vouched`` product refs."""
    return ShopRoster(
        shops=shops,
        source="neo4j",
        considered=25,
        fit=tuple(
            FitAssessment(
                product_id=ref,
                canonical_name=ref,
                fit_score=0.5,
                features=FitFeatures(similarity=0.5, preference_alignment=0.5),
                reranker="identity",
            )
            for ref in vouched
        ),
    )


class _Source:
    """A roster source that answers one prepared roster, or raises.

    ``repoints_stated_rosters`` is the deployment's opt-in, and it defaults to ``True`` HERE
    only because every test in this section is about the opt-in path. A source built anywhere
    else in this tree carries the shipped default of ``False``.
    """

    name = "double"

    def __init__(self, answer: Any, *, repoints_stated_rosters: bool = True) -> None:
        self.answer = answer
        self.calls = 0
        self.repoints_stated_rosters = repoints_stated_rosters

    def solicit(self, intent: Any, *, limit: int | None = None) -> ShopRoster:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


STATED = (
    {"store_id": "gaiaherbs.com", "tier": 1, "product_ref": "prod-milk", "list_price": 25.49,
     "max_discount_pct": 15.0},
    {"store_id": "toniiq.com", "tier": 1, "product_ref": "prod-glutathione", "list_price": 20.97},
)


def test_a_stated_row_the_platform_cannot_vouch_for_is_repointed_at_one_it_can() -> None:
    """THE BLANK SHORTLIST, at its root: a fixed roster answering a question it predates.

    The demo's roster is six shops each pinned to their liver-cluster lead, sent on every
    confirmation. Measured on this exchange over 24 queries the 3,093-product corpus genuinely
    serves, 3 of 24 returned any slot at all through that roster, against 24 of 24 through the
    exchange's own graph roster. The shops are the caller's statement and are untouched; which
    of a shop's products answers the question is the platform's.
    """
    source = _Source(
        _solicited_roster(
            SolicitedShop(
                store_id="gaiaherbs.com",
                tier=1,
                product_ref="prod-creatine",
                intent_match=0.8,
                list_price=19.99,
                currency="USD",
            ),
            vouched=("prod-creatine",),
        )
    )

    rows, reason = repoint_organic_products(source, STATED, _intent("creatine monohydrate"))

    assert [row["store_id"] for row in rows] == [row["store_id"] for row in STATED]
    assert rows[0]["product_ref"] == "prod-creatine", rows[0]
    # The PRICE moves with the product, or the row would state one product's price beside
    # another's reference.
    assert rows[0]["list_price"] == 19.99, rows[0]
    assert rows[0]["currency"] == "USD", rows[0]
    # A statement about the SHOP, not about the product, and it survives.
    assert rows[0]["max_discount_pct"] == 15.0, rows[0]
    # The shop the graph said nothing about keeps its stated row and is refused on it.
    assert rows[1] == dict(STATED[1]), rows[1]
    assert reason is not None and "gaiaherbs.com" in reason, reason


def test_a_stated_row_the_platform_vouched_for_is_left_exactly_as_stated() -> None:
    """A no-op on every auction that already works, which is what keeps this additive.

    Measured on ``"milk thistle liver support"`` against the demo roster: four of six rows are
    products the exchange's own retrieval just judged relevant, and none of them moves.
    """
    source = _Source(
        _solicited_roster(
            SolicitedShop(
                store_id="gaiaherbs.com",
                tier=1,
                product_ref="prod-other-thistle",
                intent_match=0.9,
                list_price=9.99,
            ),
            vouched=("prod-milk", "prod-other-thistle"),
        )
    )

    rows, reason = repoint_organic_products(source, STATED, _intent("milk thistle"))

    assert rows == [dict(row) for row in STATED], rows
    assert reason is None, reason


def test_an_alternative_the_platform_never_priced_is_not_substituted() -> None:
    """An unpriced row mints no offer at all, so re-pointing onto one trades wrong for nothing.

    ``_list_price_bid`` reads an absent ``list_price`` as "no offer to mint": no ``unit_price``
    and no ``expires_at``, which every downstream filter refuses. The stated row is kept and
    refused honestly instead.
    """
    source = _Source(
        _solicited_roster(
            SolicitedShop(
                store_id="gaiaherbs.com",
                tier=1,
                product_ref="prod-creatine",
                intent_match=0.8,
                list_price=None,
            ),
            vouched=("prod-creatine",),
        )
    )

    rows, reason = repoint_organic_products(source, STATED, _intent("creatine monohydrate"))

    assert rows == [dict(row) for row in STATED], rows
    assert reason is None, reason


def test_an_off_corpus_query_moves_nothing() -> None:
    """The direction that matters: this cannot manufacture a shortlist the corpus cannot back.

    A graph that rosters nobody for a furniture query leaves every stated row alone, so the
    honest empty answer survives. Measured over 10 off-corpus queries against the recorded
    corpus: zero rows moved on any of them.
    """
    source = _Source(_solicited_roster())

    rows, reason = repoint_organic_products(
        source, STATED, _intent("a walnut coffee table for the lounge")
    )

    assert rows == [dict(row) for row in STATED], rows
    assert reason is None, reason


def test_a_roster_source_that_raises_leaves_the_roster_exactly_as_stated() -> None:
    """Rule 2 of ``retrieval.roster``: a source that fails finds nobody, it does not 5xx.

    A caller who brought its own roster needs no graph at all, and an unreachable one must not
    take down a door that was working for them.
    """
    source = _Source(RuntimeError("bolt://nowhere"))

    rows, reason = repoint_organic_products(source, STATED, _intent("creatine monohydrate"))

    assert rows == [dict(row) for row in STATED], rows
    assert reason is None, reason
    assert source.calls == 1


def test_a_source_that_has_not_opted_in_is_never_even_asked() -> None:
    """THE COMPATIBILITY GUARANTEE, asserted by sabotage rather than by inspection.

    ``retrieval.roster``'s rule 1 says a stated roster does not consult the graph, and
    ``test_graph_auction.py::test_a_stated_roster_never_consults_the_graph`` holds it for the
    route. This holds it here: a source whose ``solicit`` would RAISE is not reached at all,
    because it never carried the deployment's opt-in. The shipped default of
    ``GraphShopRoster.repoints_stated_rosters`` is ``False``; ``graph_roster_from_env`` is the
    only thing in this tree that turns it on.
    """
    source = _Source(RuntimeError("this must never be called"), repoints_stated_rosters=False)

    rows, reason = repoint_organic_products(source, STATED, _intent("creatine monohydrate"))

    assert rows == [dict(row) for row in STATED], rows
    assert reason is None, reason
    assert source.calls == 0, "a source that did not opt in was consulted anyway"


def test_the_graph_roster_built_from_the_environment_opts_in_with_the_graph() -> None:
    """And the switch is SET by the deployment that already asks for organic discovery.

    A capability nothing turns on is a capability that does not exist; this is the read that
    makes ``EXCHANGE_SHOP_ROSTER=graph`` — which ``apps/exchange/compose.yaml`` already sets —
    enough, and ``EXCHANGE_REPOINT_ORGANIC_PRODUCTS=none`` the way back to the old behaviour.
    """
    from exchange.retrieval.roster import graph_roster_from_env

    graph = {"EXCHANGE_SHOP_ROSTER": "graph", "NEO4J_URI": "bolt://127.0.0.1:1"}
    assert graph_roster_from_env({}) is None
    built = graph_roster_from_env(graph)
    assert built is not None and built.repoints_stated_rosters is True
    off = graph_roster_from_env({**graph, "EXCHANGE_REPOINT_ORGANIC_PRODUCTS": "none"})
    assert off is not None and off.repoints_stated_rosters is False


def test_an_unwired_exchange_moves_nothing_and_asks_nobody() -> None:
    """``NoShopRoster`` is the wired default; a deployment with no graph is unchanged."""
    from exchange.retrieval.roster import NoShopRoster

    rows, reason = repoint_organic_products(
        NoShopRoster(), STATED, _intent("creatine monohydrate")
    )

    assert rows == [dict(row) for row in STATED], rows
    assert reason is None, reason


def test_the_served_route_answers_a_query_outside_the_rosters_cluster() -> None:
    """END TO END: the blank screen becomes a real shortlist, over ``POST /auctions``.

    The request is the demo's own — a stated roster of four liver products — and the query is
    one the roster predates. Without the re-pointing every row is honestly off-topic and the
    shopper sees nothing; with it, the one shop whose catalogue the platform can answer from
    is shown its own creatine product at the price the platform observed, and the other three
    are still refused because their shops stock nothing on the subject.
    """
    app = _served_app(bidding=False)
    # The demo's own snapshots, plus the one product the platform would re-point gaiaherbs onto.
    # A snapshot that held ONLY the substitute would leave the other three stores unnameable,
    # and an absent identity is the filter's fail-open — every row would be kept and this test
    # would pass for the wrong reason.
    snapshots: dict[str, Any] = {
        store: {
            "snapshot_id": f"snap-{store}",
            "products": [
                {
                    "product_ref": ref,
                    "canonical_name": name,
                    "evidence_ref": f"snap-{store}#{ref}",
                    "attributes": {},
                }
            ],
        }
        for store, ref, name, _ in DEMO_ROSTER
    }
    snapshots["gaiaherbs.com"]["products"].append(
        {
            "product_ref": "prod-creatine",
            "canonical_name": "Creatine Monohydrate Powder",
            "evidence_ref": "snap-gaiaherbs.com#prod-creatine",
            "attributes": {},
        }
    )
    configure_ranking(app, catalog=StaticCatalogSnapshots(snapshots))
    configure_auctions(
        app,
        shop_roster=_Source(
            _solicited_roster(
                SolicitedShop(
                    store_id="gaiaherbs.com",
                    tier=1,
                    product_ref="prod-creatine",
                    intent_match=0.8,
                    list_price=19.99,
                    currency="USD",
                ),
                vouched=("prod-creatine",),
            )
        ),
    )

    body = _serve(app, "creatine monohydrate powder")

    slots = body["shortlist"]["slots"]
    assert len(slots) == 1, body["shortlist"]
    assert slots[0]["product"]["identity"]["title"] == "Creatine Monohydrate Powder", slots[0]
    assert slots[0]["price"]["unit_price"] == 19.99, slots[0]
    # The other three shops stock nothing the platform can answer this with, so they are still
    # refused — the honest half of the same change.
    refusals = [
        reason
        for row in body["excluded"]
        for reason in row["exclusion_reasons"]
        if reason.startswith(REASON_OFF_TOPIC_ORGANIC)
    ]
    assert len(refusals) == len(DEMO_ROSTER) - 1, body["excluded"]
    # The response says the platform re-chose a product, so a reader is not left to infer it.
    assert "re-pointed" in str(body["roster_source"]["reason"]), body["roster_source"]
    assert body["roster_source"]["source"] == "request", body["roster_source"]


def test_the_served_route_still_answers_a_furniture_query_with_nothing() -> None:
    """The same wiring, the off-corpus direction: nothing is manufactured.

    The graph rosters nobody, so no row moves, every stated row is refused and the screen is
    honestly empty — which is the answer the whole stream exists to produce.
    """
    app = _served_app(bidding=False)
    configure_auctions(app, shop_roster=_Source(_solicited_roster()))

    body = _serve(app, "a walnut coffee table for the lounge")

    assert body["shortlist"]["slots"] == [], body["shortlist"]
    assert len(body["entries"]) == len(DEMO_ROSTER), body["entries"]
    assert body["roster_source"]["reason"] is None, body["roster_source"]
