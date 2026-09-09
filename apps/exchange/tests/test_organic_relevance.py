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
from exchange.retrieval.relevance import ES_PLURAL_STEM_ENDINGS, OFF_TOPIC_DETAIL, _stem
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


@pytest.mark.parametrize(
    ("singular", "plural"),
    [("potato", "potatoes"), ("tomato", "tomatoes"), ("hero", "heroes"), ("echo", "echoes")],
)
def test_the_o_noun_plural_is_a_KNOWN_split_and_the_docstring_must_keep_saying_so(
    singular: str, plural: str
) -> None:
    """A consonant + ``-o`` singular takes ``-es`` too, and this stemmer does not fold it.

    Pinned rather than fixed, and pinned because the docstring on
    :data:`ES_PLURAL_STEM_ENDINGS` claims exactly this. Adding ``"o"`` to that tuple would fold
    these four and break the pair below it, which is the trade the docstring records: measured
    over the tokens of every product ``title`` on disk, the shipped 3,093-product corpus
    (``fixtures/real-catalogs``) contains no ``-oes`` token at all, and the 17,409-product broad
    corpus (``fixtures/real-catalogs-broad``) contains ``shoes`` x7 against ``heroes`` x1.
    """
    assert _stem(plural) == singular + "e", plural
    assert _stem(singular) == singular, singular
    assert "o" not in ES_PLURAL_STEM_ENDINGS, ES_PLURAL_STEM_ENDINGS


@pytest.mark.parametrize(("singular", "plural"), [("shoe", "shoes"), ("canoe", "canoes")])
def test_an_oe_singular_still_folds_onto_its_own_plural(singular: str, plural: str) -> None:
    """The class the ``-o`` omission protects.

    ``shoe`` is an ``-e`` singular whose stem also ends in ``o``, so no suffix rule can tell
    ``sho`` from ``potato``: ``"o"`` in :data:`ES_PLURAL_STEM_ENDINGS` would answer ``shoes ->
    sho`` against ``shoe -> shoe``. Measured on the OLD blanket ``-es`` strip these two behaved
    differently from each other — ``shoes -> shoe`` (folded, because the four-character floor
    sent it to the ``-s`` rule) but ``canoes -> cano`` against ``canoe -> canoe`` (split) — and
    both fold now.
    """
    assert _stem(plural) == singular, (plural, _stem(plural))
    assert _stem(singular) == singular, singular


def test_a_stem_that_is_not_a_word_is_fine_as_long_as_both_spellings_reach_it() -> None:
    """``series`` answers ``serie``, not ``sery`` — the docstring used to name the wrong stem.

    It reaches the bare ``-s`` rule: the ``-ies`` branch needs a four-character stem and ``ser``
    is three, and ``seri`` is not one of :data:`ES_PLURAL_STEM_ENDINGS`. Under the old blanket
    ``-es`` strip it answered ``seri``, so the sentence was never true of either version.
    """
    assert _stem("series") == "serie"
    assert _stem("studies") == "study"
    assert _stem("gummies") == _stem("gummy") == "gummy"


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
# 1b. THE SHOPPER TYPED THE PRODUCT'S NAME — the refusal the thresholds could not avoid
# =====================================================================================
#: Real one-content-word titles out of ``deploy/demo/exchange-deployment.json``, with the brand
#: the crawl carries beside each, and a query that NAMES the product and then goes on talking
#: the way a shopper talks. Every one of these was refused ``organic_result_off_topic`` before
#: :func:`~exchange.ranking.filters.shopper_named_the_product` existed.
#:
#: The arithmetic, so the fixture is not mistaken for an oddity: ``judge`` needs
#: ``min(2, len(asked))`` agreeing content words or half of them, counted against the QUERY's
#: length. A one-word title can supply exactly one, and the brand is in the surface but not in
#: the shopper's typing — so past two content words in the query these are refused by
#: arithmetic, whatever they name. Measured over the whole document: 104 of 3,086 products.
NAMED_OUTRIGHT = (
    ("bacopa best one for daily use under $30", "Bacopa", "Gaia Herbs"),
    ("resveratrol best one for daily use under $30", "Resveratrol", "Gaia Herbs"),
    ("glutathione 98% best one for daily use under $30", "Glutathione 98%", "Toniiq"),
    ("garlic 1% best one for daily use under $30", "Garlic 1%", "BulkSupplements.com"),
    ("tribulus 95% best one for the gym under $30", "Tribulus 95%", "BulkSupplements.com"),
    ("sleepthru best one for daily use under $30", "SleepThru®", "Gaia Herbs"),
)


@pytest.mark.parametrize(("query", "title", "brand"), NAMED_OUTRIGHT)
def test_a_product_the_shopper_named_outright_is_never_refused(
    query: str, title: str, brand: str
) -> None:
    """The false positive this condition ends, on the shipped catalogue's own product names.

    A shopper typed the platform's own name for the product and the platform's own gate
    answered that its crawl could not connect the product to the query. That is the fourth
    dominant defect class in this repo — a refusal firing on honest traffic — and it is not a
    hypothetical shape: these are five of the 104 products in
    ``deploy/demo/exchange-deployment.json`` it fired on.

    Asserted through :func:`~exchange.ranking.filters.organic_relevance_reason` rather than
    through ``judge``, because the thresholds are UNCHANGED and still refuse these: what
    changed is that the gate declines to apply them to a row the shopper named. A test written
    against ``judge`` would pass on a rule that had been loosened instead, which is the change
    that module's own ablation table measured and rejected.
    """
    identity = {"title": title, "brand": brand, "source": "snap-x"}
    candidate = {"fallback": True, "store_id": "s1"}

    assert RULE.judge(query, f"{title} {brand}").about is False, (
        "this fixture is only interesting while the thresholds still refuse it"
    )
    assert (
        organic_relevance_reason(candidate, query_text=query, identity=identity, relevance=RULE)
        is None
    ), f"the shopper typed {title!r} and was told the platform could not connect it to {query!r}"


@pytest.mark.parametrize(("query", "name"), OFF_CORPUS_PAIRS)
def test_naming_rows_the_shopper_did_not_name_are_still_refused(query: str, name: str) -> None:
    """THE POSITIVE CONTROL, re-taken as ``retrieval.relevance``'s header requires.

    That module says zero-of-thirty off-corpus is "the number to re-take after any change
    here". Every one of these pairs is the row the retriever really put at the top for a query
    this catalogue has nothing for, and every one must still be refused: the keep-condition
    needs the whole title contained AND asked for, so a wrong row sharing one coincidental
    word with the query does not satisfy it.

    Measured wider than this parametrisation, over the 16 off-corpus queries (these 15 plus
    ``"garlic bread recipe book"``, written specifically to attack the keep-condition) against
    all 3,086 identities of ``deploy/demo/exchange-deployment.json`` at ``fa900c4`` — 49,376
    verdicts, **zero** rows admitted that the thresholds would have refused. It used to be one:
    ``Garlic 1%`` for the query written to catch it, which plain containment kept and
    :func:`~exchange.ranking.filters.query_noun_phrases` now refuses, because ``garlic``
    premodifies ``bread``.
    """
    identity = {"title": name, "source": "snap-x"}
    candidate = {"fallback": True, "store_id": "s1"}

    reason = organic_relevance_reason(
        candidate, query_text=query, identity=identity, relevance=RULE
    )
    assert reason is not None, f"{name!r} is not what {query!r} asked for"
    assert reason.startswith(REASON_OFF_TOPIC_ORGANIC), reason


def test_naming_only_part_of_the_title_is_not_naming_the_product() -> None:
    """Containment is the WHOLE title, not an overlap — otherwise it is a second threshold.

    ``"milk"`` is one word of ``Milk Thistle Gummies``. A rule satisfied by part of a name
    would admit every product sharing a word with the query, which is the one-shared-word rule
    the ablation measured at 7 of 30 off-corpus queries served.
    """
    from exchange.ranking.filters import shopper_named_the_product

    assert shopper_named_the_product("milk for my cereal", {"title": "Milk Thistle Gummies"}) is (
        False
    )
    assert shopper_named_the_product(
        "milk thistle gummies please", {"title": "Milk Thistle Gummies"}
    )


def test_the_name_that_counts_is_the_titles_and_not_the_brands() -> None:
    """A shopper naming only the BRAND has not named a product.

    The brand is in the judged surface so that naming a brand can help a product agree with a
    query. It is not part of the product's name, and requiring it here would put this condition
    out of reach of exactly the one-word-title rows it exists for — every one of which carries
    a brand the shopper never typed.
    """
    from exchange.ranking.filters import shopper_named_the_product

    identity = {"title": "Bacopa", "brand": "Gaia Herbs"}
    assert shopper_named_the_product("gaia herbs best sellers", identity) is False
    assert shopper_named_the_product("bacopa for memory and focus daily", identity) is True


def test_an_identity_with_no_readable_title_names_nothing() -> None:
    """Nothing was named, so nothing was named in full. The row falls through to the rule."""
    from exchange.ranking.filters import shopper_named_the_product

    assert shopper_named_the_product("anything at all", {"title": ""}) is False
    assert shopper_named_the_product("anything at all", {"brand": "Gaia Herbs"}) is False
    assert shopper_named_the_product("anything at all", None) is False


#: OFF-CORPUS queries that CONTAIN a shipped product's whole crawled title, because that title
#: is one ordinary English word. Every row here is a real product out of
#: ``deploy/demo/exchange-deployment.json`` (``Iron+``, ``SAMe Bulk``, ``Fuel``, ``Ease`` …)
#: and every query is one a person would really type about something else entirely.
#:
#: **This is the leak the keep-condition used to be**, and it was reachable on the demo's own
#: route: with two silent tier-1 stores rostered on ``Iron+`` and ``SAMe Bulk``,
#: ``POST /auctions`` answered ``"cast iron skillet for camping"`` with ``Iron+`` at $29.95.
#: Measured over 25 queries of this shape, plain containment filled **25 of 25**; with
#: :func:`~exchange.ranking.filters.query_noun_phrases` guarding it, **1 of 25** — the one
#: below that is deliberately absent from this table, ``"iron on patches for jeans"``, where
#: ``on`` is a preposition to every tokeniser in this tree and ``iron-on`` is an English
#: compound this rule has no way to see.
#:
#: The last three are the attack on :data:`~exchange.ranking.filters.NAMING_MODIFIERS`
#: specifically: an ordinary word followed by a form word, which is the one shape that list
#: can be talked into. ``blend``, ``complex`` and ``liquid`` are not on it for exactly this
#: reason.
CONTAINMENT_LEAKS = (
    ("cast iron skillet for camping", "Iron+", "Momentous"),
    ("an iron bed frame queen size", "Iron+", "Momentous"),
    ("steam iron with a vertical setting", "Iron+", "Momentous"),
    ("where can i buy scrap iron", "Iron+", "Momentous"),
    ("bulk storage bins for the garage", "SAMe Bulk", "PureBulk, Inc."),
    ("bulk mailing envelopes 500 count", "SAMe Bulk", "PureBulk, Inc."),
    ("garlic bread recipe book", "Garlic 1%", "Toniiq - Elevated Nutrients"),
    ("garlic press stainless steel", "Garlic 1%", "Toniiq - Elevated Nutrients"),
    ("ginger ale soda cans", "Ginger", "Paradise Herbs"),
    ("ginger jar table lamp", "Ginger", "Paradise Herbs"),
    ("fuel injector cleaner for my truck", "Fuel", "Momentous"),
    ("camping stove tablets fuel", "Fuel", "Momentous"),
    ("fiber optic cable 50 ft", "Fiber+", "Momentous"),
    ("a zinc roofing sheet", "Zinc", "Momentous"),
    ("door handles made of zinc", "Zinc", "Momentous"),
    ("calcium remover for shower glass", "Calcium", "Momentous"),
    ("omega seamaster watch strap", "Omega-3", "Momentous"),
    ("keyboard shortcuts for ease", "Ease 50:1", "Toniiq - Elevated Nutrients"),
    ("longevity dog food for senior labradors", "Longevity", "Momentous"),
    ("how do i put my iphone into recovery", "Recovery", "Momentous"),
    ("nutmeg grinder wooden", "Nutmeg", "PureBulk, Inc."),
    ("fuel blend for a two stroke engine", "Fuel", "Momentous"),
    ("recovery complex for a sprained ankle", "Recovery", "Momentous"),
    ("fiber liquid for a broken kayak hull", "Fiber+", "Momentous"),
)


@pytest.mark.parametrize(("query", "title", "brand"), CONTAINMENT_LEAKS)
def test_containing_a_one_word_title_is_not_asking_for_it(
    query: str, title: str, brand: str
) -> None:
    """THE ATTACK DIRECTION on the keep-condition itself, and the reason it has a second half.

    ``Iron+`` folds to the single content word ``iron``, so the shopper's words contain this
    product's whole crawled name — the first assertion pins exactly that, because a fixture
    where containment did not hold would pass this test on the old rule too. What the shopper
    ASKED FOR is a skillet, a bed frame, or an iron to press shirts with, and in every one of
    those the product's word is a premodifier of the thing they actually want.
    """
    from exchange.ranking.filters import shopper_named_the_product

    identity = {"title": title, "brand": brand, "source": "snap-x"}
    assert set(content_terms(title)) <= set(content_terms(query)), (
        "this fixture is only interesting while the query really does contain the whole title"
    )
    assert shopper_named_the_product(query, identity) is False, query

    reason = organic_relevance_reason(
        {"fallback": True, "store_id": "s1"}, query_text=query, identity=identity, relevance=RULE
    )
    assert reason is not None, f"{title!r} is not what {query!r} asked for"
    assert reason.startswith(REASON_OFF_TOPIC_ORGANIC), reason


#: THE HONEST DIRECTION for the same one-word products, and it comes first in weight: every one
#: of these is a query the thresholds refuse by arithmetic (one agreeing word out of three or
#: more) and that this condition must keep. The names lead, the way a shopper leads with what
#: they want, and what follows is the form, the grade, or what it is for.
NAMED_IN_THE_LEAD = (
    ("iron supplement for anemia", "Iron+", "Momentous"),
    ("iron capsules that dont upset my stomach", "Iron+", "Momentous"),
    ("bacopa for memory and focus daily", "Bacopa", "Gaia Herbs"),
    ("organic bacopa capsules 500 mg", "Bacopa", "Gaia Herbs"),
    ("reishi extract for immune support", "Reishi", "Paradise Herbs"),
    ("turmeric capsules for joint pain", "Turmeric", "Paradise Herbs"),
    ("shilajit resin for energy levels", "Shilajit", "Paradise Herbs"),
    ("zinc lozenges for a winter cold", "Zinc", "Momentous"),
    ("calcium tablets for bone density", "Calcium", "Momentous"),
    ("fiber powder for constipation relief", "Fiber+", "Momentous"),
    ("omega supplement for heart health", "Omega-3", "Momentous"),
    ("multivitamin for active men over 50", "Multivitamin", "Momentous"),
    ("recovery supplement for after training", "Recovery", "Momentous"),
    ("longevity supplement for healthy ageing", "Longevity", "Momentous"),
    ("nattokinase for circulation support", "Nattokinase", "Paradise Herbs"),
    ("berberine for blood sugar control", "Berberine", "Momentous"),
    ("ashwagandha for stress and cortisol", "Ashwagandha", "Paradise Herbs"),
    ("ginger capsules for nausea relief", "Ginger", "Paradise Herbs"),
    ("garlic capsules for cholesterol", "Garlic 1%", "Toniiq - Elevated Nutrients"),
)


@pytest.mark.parametrize(("query", "title", "brand"), NAMED_IN_THE_LEAD)
def test_a_one_word_product_the_shopper_led_with_is_still_kept(
    query: str, title: str, brand: str
) -> None:
    """The false refusal must stay fixed. Same products, same rule, the honest half.

    The first assertion is the same guard :data:`NAMED_OUTRIGHT` carries: these are only
    interesting while the thresholds still refuse them, which is what makes this a
    keep-condition rather than a looser threshold.
    """
    identity = {"title": title, "brand": brand, "source": "snap-x"}
    assert RULE.judge(query, f"{title} {brand}").about is False, (
        "this fixture is only interesting while the thresholds still refuse it"
    )
    assert (
        organic_relevance_reason(
            {"fallback": True, "store_id": "s1"},
            query_text=query,
            identity=identity,
            relevance=RULE,
        )
        is None
    ), f"the shopper asked for {title!r} by name and was told the platform could not connect it"


def test_the_condition_cannot_change_the_gate_for_a_multi_word_title() -> None:
    """The scope claim, asserted rather than asserted-in-prose.

    ``shopper_named_the_product`` returns on containment alone once the title carries
    ``min_shared_terms`` content words, and that shortcut is safe because the gate could not
    have refused such a row anyway: containment puts every one of those words in the query AND
    in the surface, so ``judge`` counts at least ``min_shared_terms`` agreements and keeps it.
    Re-measured over all 3,086 identities in ``deploy/demo/exchange-deployment.json``: of the
    2,982 with two or more content words, **0** had their gate answer decided by this
    condition. This node pins the shape on the pair that would break first.
    """
    from exchange.ranking.filters import shopper_named_the_product

    identity = {"title": "Milk Thistle Gummies", "brand": "Gaia Herbs"}
    query = "a supplement containing milk thistle gummies for the liver"

    assert shopper_named_the_product(query, identity) is True
    assert RULE.judge(query, "Milk Thistle Gummies Gaia Herbs").about is True, (
        "the thresholds already keep a row whose whole multi-word name the shopper typed, so "
        "this condition has nothing left to decide for it"
    )


def test_the_query_is_split_where_a_shopper_stops_describing_one_thing() -> None:
    """:func:`~exchange.ranking.filters.query_noun_phrases`, on the two shapes that decide it."""
    from exchange.ranking.filters import query_noun_phrases

    assert query_noun_phrases("cast iron skillet for camping") == (
        ("cast", "iron", "skillet"),
        ("camping",),
    )
    assert query_noun_phrases("bacopa best one for daily use under $30") == (
        ("bacopa",),
        ("one",),
        ("daily",),
    )
    assert query_noun_phrases("") == ()
    # A bare number is a boundary, which is what keeps `Glutathione 98%` naming itself.
    assert query_noun_phrases("glutathione 98% for daily use") == (("glutathione",), ("daily",))


def test_a_missing_modifier_costs_a_keep_and_never_buys_a_leak() -> None:
    """The fail direction of :data:`~exchange.ranking.filters.NAMING_MODIFIERS`, pinned.

    ``chewables`` is not on the list. The row is not refused BY this condition — it falls back
    to the thresholds, exactly where it stood before the condition existed — so the cost of a
    short list is an honest refusal and never a confident wrong answer. Adding a word can only
    move rows the other way, which is why the list is short and every entry on it is a form or
    a grade rather than a thing.
    """
    from exchange.ranking.filters import NAMING_MODIFIERS, shopper_named_the_product

    identity = {"title": "Bacopa", "brand": "Gaia Herbs"}
    assert "chewabl" not in NAMING_MODIFIERS
    assert shopper_named_the_product("bacopa chewables for memory", identity) is False
    assert shopper_named_the_product("bacopa capsules for memory", identity) is True
    assert not {"blend", "complex", "liquid"} & NAMING_MODIFIERS, (
        "each of these bought an off-corpus row in the harness and no honest query needed it"
    )


def test_naming_the_product_does_not_reach_a_row_a_store_actually_bid() -> None:
    """The D55 asymmetry is upstream of this condition and stays that way.

    A sponsored row is never judged for relevance at all, so this condition can neither keep
    one nor refuse one. Asserted so that a later reader cannot mistake the new keep-condition
    for the thing that protects the sponsored half.
    """
    identity = {"title": "Glutathione 98%", "brand": "Toniiq"}
    for named in (True, False):
        query = "glutathione 98% for daily use" if named else "a walnut coffee table"
        assert (
            organic_relevance_reason(
                {"fallback": False, "store_id": "s1"},
                query_text=query,
                identity=identity,
                relevance=RULE,
            )
            is None
        )


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
def _retrieval_result(*, considered: int, off_topic: int, excluded: int = 0) -> RetrievalResult:
    """A retrieval that reached ``considered`` products and refused ``off_topic`` of them."""
    return RetrievalResult(
        intent_id="int-1",
        assessments=(),
        excluded=tuple(
            ExcludedCandidate(f"x-{index}", f"Excluded {index}", ("hard constraint",))
            for index in range(excluded)
        ),
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


def test_the_hard_constraint_emptiness_is_bounded_to_what_was_judged_too() -> None:
    """The SIBLING of the sentence above, three lines down, with the identical overclaim.

    ``_nothing_retrieved_reason`` has three branches on one served field. The ``off_topic`` one
    was repaired and the ``excluded`` one was left reading "no product in this exchange's
    catalogue graph satisfies this intent's hard constraints" — a claim about every product in
    the graph, composed after judging ``result.considered`` of them, which the retrieval caps
    at ``DEFAULT_ROSTER_PRODUCTS``. A product the index never surfaced was never checked
    against a hard constraint either.
    """
    sentence = _nothing_retrieved_reason(_retrieval_result(considered=25, off_topic=0, excluded=25))

    assert "no product in this exchange's catalogue graph satisfies" not in sentence, sentence
    assert "nothing this search reached" in sentence, sentence
    assert "not about the whole catalogue" in sentence, sentence
    assert "25 product(s)" in sentence, sentence
    # Still says WHICH emptiness this is: a hard-constraint refusal is a different sentence to
    # the shopper than an off-topic one, and collapsing them is the defect this branch exists
    # to avoid.
    assert "hard constraints" in sentence, sentence


def _solicited_roster(*shops: SolicitedShop, vouched: tuple[str, ...] = ()) -> ShopRoster:
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
    {
        "store_id": "gaiaherbs.com",
        "tier": 1,
        "product_ref": "prod-milk",
        "list_price": 25.49,
        "max_discount_pct": 15.0,
    },
    {"store_id": "toniiq.com", "tier": 1, "product_ref": "prod-glutathione", "list_price": 20.97},
)


def test_a_stated_row_the_search_did_not_return_is_repointed_at_one_it_did() -> None:
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
    # `max_discount_pct` is carried through untouched, so the caller's cap now applies to a
    # product the caller never named. `auction/collect.py` judges it per ROW, beside the row's
    # `list_price`, which moved with the product — see `repoint_organic_products` for why this
    # is recorded rather than repaired.
    assert rows[0]["max_discount_pct"] == 15.0, rows[0]
    # The shop the graph said nothing about keeps its stated row and is refused on it.
    assert rows[1] == dict(STATED[1]), rows[1]
    assert reason is not None and "gaiaherbs.com" in reason, reason


def test_the_repoint_reason_says_the_search_missed_it_not_that_it_judged_it() -> None:
    """THE SERVED SENTENCE MAY NOT REPORT A JUDGEMENT THAT NEVER HAPPENED.

    ``repoint_organic_products`` decides on ``ShopRoster.fit``, which is
    ``RetrievalResult.assessments``, which ``retrieve()`` truncates to ``DEFAULT_ROSTER_PRODUCTS``
    — so a pinned product absent from it may simply have ranked twenty-sixth. The reason used to
    say "the product each named was not one this retrieval vouched for", which reads as "the
    platform looked at your product and would not stand behind it".

    Measured over 24 in-corpus queries x the demo's six rows against the recorded corpus
    (3,093 ``Product`` nodes in the graph at the time of the run): of 63 moved rows, 0 had a
    pinned product judged off-topic, 0 had one excluded by a hard constraint, and 63 had one
    that was never retrieved and so never judged at all. The sentence a shopper and an operator
    both read must say what happened.

    This double makes that structural rather than corpus-dependent: the roster vouches for
    ``prod-creatine`` alone and knows nothing whatever about ``prod-milk``, so there is no
    verdict on the pinned product for the reason to be reporting.
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

    _rows, reason = repoint_organic_products(source, STATED, _intent("creatine monohydrate"))

    assert reason is not None, reason
    assert "vouch" not in reason, reason
    assert "did not return the product each named" in reason, reason


def test_a_stated_row_the_search_did_return_is_left_exactly_as_stated() -> None:
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

    rows, reason = repoint_organic_products(NoShopRoster(), STATED, _intent("creatine monohydrate"))

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
    served_reason = str(body["roster_source"]["reason"])
    assert "re-pointed" in served_reason, body["roster_source"]
    assert body["roster_source"]["source"] == "request", body["roster_source"]
    # ON THE WIRE, not only in the helper: the sentence a shopper and an operator read must not
    # report a judgement the retrieval never made. `prod-milk` is absent from `fit` because this
    # roster never mentions it, which is what "the search did not return it" means — and is not
    # the same statement as "the search judged it and refused it".
    assert "vouch" not in served_reason, body["roster_source"]
    assert "did not return the product each named" in served_reason, body["roster_source"]


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
