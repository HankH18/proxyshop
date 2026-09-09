"""THE ANSWER WAS IN THE VARIANT NAMES, AND READING THEM NEARLY COST THE HONESTY GATE.

What this file grades, in one sentence: that the exchange's relevance rule can read the words
a shop puts in its VARIANT titles, and that reading them did not turn a colour word and a
garment size into a relevance judgement.

The reach, measured
-------------------
``fixtures/real-catalogs-broad`` holds 17,409 products and 97,217 variant names. Of the
products whose crawled name contains ``coffee table``, two offer a walnut finish and neither
says so in its name::

    floydhome.com        "The Lift Off Coffee Table"
        variant  'One Panel - 18" w x 67" l x 15" h / Walnut / Black'
    floydhome.com        "The Modular Table"          variant 'Small / Walnut'
    branchfurniture.com  "Side Table"                 variant 'Walnut'
    branchfurniture.com  "Nested Coffee Tables"       variant 'Walnut / Medium'

``exchange.retrieval.relevance.candidate_surface`` reads the crawled name, brand, categories,
ingredients and attribute keys, and none of those carries ``walnut`` for any of them.

THE WEIGHTING, which is the whole of the risk and the whole of this file
------------------------------------------------------------------------
A variant list is colours, sizes, materials and counts, and a corpus of colour words matches
all sorts of queries. The first version of this arm gated on a COUNT — any one word of the
query already carried by the product's own identity opened it — and that is not a bound at
all, because the arithmetic of a good match and a ruinous one is identical. Measured over the
broad corpus above, rule-level, with 12 furniture queries and 22 queries the corpus has
nothing for::

    gate                                    furniture rows   apparel rows   off-corpus rows
    --------------------------------------- --------------  -------------  ----------------
    identity surface alone (no variant arm)        73              0               19
    any one identity word                         171             22              204
    the head term, plus the share arm             142              0               28

The 22 apparel rows are one shape, and every one of them is a real row out of the fixture::

    "a large green ceramic plant pot"  ->  "Women's Afternoon Hoodie in Sage Green"
                                           matched ('green',)  corroborated ('large',)
    "a small white desk lamp"          ->  "Signature Crewneck - Pure White"
                                           matched ('white',)  corroborated ('small',)

The shopper's adjective matched a garment colourway and the shopper's size matched a garment
size, and neither product's own record says it is a pot or a lamp.
:func:`~exchange.retrieval.relevance.head_term` is that sentence made executable — the
platform's own record must carry the word saying WHAT KIND OF THING was asked for before any
variant word is counted — and the widened agreement must then meet the SHARE arm, because the
count arm's licence to admit two words out of nine belongs to a product's own crawled identity
and is not inherited by a listing option.

``ADVERSARIAL_ROWS`` below is that corpus, quoted verbatim, and it is the direction the first
version of this file did not test: it attacked only the ZERO-identity-word case, which the arm
closes by construction.
"""

from __future__ import annotations

from typing import Any

import pytest
from exchange.retrieval import (
    VARIANT_NAMES_PER_PRODUCT,
    CandidateRetrieval,
    InMemoryCandidateSource,
    TopicalRelevance,
    content_terms,
    make_candidate,
)
from exchange.retrieval.relevance import (
    PHRASE_BREAK_WORDS,
    STOPWORDS,
    candidate_surface,
    head_term,
    variant_surface,
)
from exchange.retrieval.sources import VariantCandidate

from apps.exchange.tests.test_organic_relevance import OFF_CORPUS_PAIRS, SERVED_PAIRS

RULE = TopicalRelevance()

#: Verbatim ``Variant.name`` strings out of ``fixtures/real-catalogs-broad``, read off the
#: recorded ``stores/*.products.jsonl.gz`` rows they were collected into. Quoted rather than
#: invented, because the whole argument for this arm is that real shops put the material in
#: the variant and not in the title.
LIFT_OFF_VARIANTS = (
    'One Panel - 18" w x 67" l x 15" h / Birch / Black',
    'One Panel - 18" w x 67" l x 15" h / Walnut / Black',
    'One Panel - 18" w x 67" l x 15" h / Walnut / Almond',
)
NESTED_VARIANTS = ("Walnut / Medium", "Walnut / Large", "Walnut / Set of 2")
MODULAR_VARIANTS = ("Small / Walnut", "Small / Maple", "Large / Walnut", "Large / Maple")

#: THE ONE-IDENTITY-WORD-PLUS-A-COLOUR CORPUS: ``(query, crawled name, variant names)``
#: triples, every one of them a row the count-gated arm SERVED and this rule refuses. Titles
#: and variant names are verbatim from ``fixtures/real-catalogs-broad``; the queries are the
#: colour/size shape a variant surface is most exposed to.
#:
#: Each row agrees with its query on exactly ONE identity word, and that word is an adjective
#: — a colourway in the title, or a size the product happens to be named for — while the noun
#: the shopper actually asked for appears nowhere in the platform's record of it.
ADVERSARIAL_ROWS = (
    (
        "a large green ceramic plant pot",
        "Women's Afternoon Hoodie in Sage Green",
        ("Extra Small / Sage Green", "Large / Sage Green", "2XL / Sage Green"),
    ),
    (
        "a large green ceramic plant pot",
        "Overland Mat - Sage Green",
        ("Large",),
    ),
    (
        "a small white desk lamp",
        "Signature Crewneck - Pure White",
        ("Small / Pure White", "Large / Pure White", "XXL / Pure White -"),
    ),
    (
        "a large grey bathroom mirror",
        "Men's Luxe Jersey Relaxed Tee in Heather Grey",
        ("XS / Heather Grey", "Large / Heather Grey", "4XL / Heather Grey"),
    ),
    (
        "a small black kitchen bin",
        "Men's Cloud 9 Fleece Relaxed Crewneck in Black",
        ("Small / Black", "Medium / Black", "Large / Black"),
    ),
)


def _intent(query: str) -> dict[str, Any]:
    return {
        "intent_id": "intent-variant",
        "cluster_id": "cluster-1",
        "query": query,
        "hard_constraints": [],
        "preferences": [],
        "currency": "USD",
        "budget_band": "unspecified",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }


# =====================================================================================
# 1. The rule: what a variant word can do, and what it may not
# =====================================================================================


def test_a_variant_word_corroborates_a_partial_identity_match() -> None:
    """The reach this arm buys, on a query a furniture corpus genuinely answers.

    ``a walnut side table`` asks for three content words. ``Nested Coffee Tables`` carries one
    of them in the platform's crawled name — one of three is 0.33, under
    :data:`~exchange.retrieval.relevance.MIN_SHARED_SHARE` and under ``MIN_SHARED_TERMS`` — so
    the identity arms refuse it, and the assertion at the end of this test is that they still
    do. The shop put the finish in the variant, where the rule could not see it.

    The word it carries is ``table``, which is the query's head: the platform's own record
    already says this thing is a table, so ``walnut`` out of a listing option describes it
    rather than deciding what it is.
    """
    verdict = RULE.judge(
        "a walnut side table", "Nested Coffee Tables", variant_text=" ".join(NESTED_VARIANTS)
    )
    assert verdict.about, verdict
    assert verdict.decidable, verdict
    assert verdict.matched == ("table",), verdict
    assert verdict.corroborated == ("walnut",), verdict
    assert "variants it observed" in verdict.detail, verdict.detail

    without = RULE.judge("a walnut side table", "Nested Coffee Tables")
    assert not without.about, "the identity surface alone must still refuse this"


def test_a_variant_word_on_its_own_never_carries_a_match() -> None:
    """THE WEIGHTING, and the false positive it exists to refuse.

    ``walnut nightstand`` and ``Nested Coffee Tables`` agree on nothing the platform crawled
    about the product itself. A rule that pooled variant names into
    :func:`~exchange.retrieval.relevance.candidate_surface` would have found ``walnut`` there,
    met :data:`~exchange.retrieval.relevance.MIN_SHARED_SHARE` on a two-word query with a
    colour, and put a coffee table in front of somebody who asked for a nightstand. The head
    of this query is ``nightstand`` and the platform's record of the product does not carry
    it, so the arm is not reached at all.
    """
    verdict = RULE.judge(
        "a walnut nightstand", "Nested Coffee Tables", variant_text=" ".join(NESTED_VARIANTS)
    )
    assert not verdict.about, verdict
    assert verdict.decidable, verdict
    assert verdict.matched == (), verdict
    assert verdict.corroborated == ("walnut",), verdict
    assert "cannot carry a match on its own" in verdict.detail, verdict.detail


@pytest.mark.parametrize(("query", "name", "variants"), ADVERSARIAL_ROWS)
def test_a_colour_in_the_title_and_a_size_in_the_variants_do_not_make_a_match(
    query: str, name: str, variants: tuple[str, ...]
) -> None:
    """THE REGRESSION THIS GATE EXISTS FOR, on the rows that actually happened.

    Every row here is one the count-gated version of this arm SERVED, measured through
    ``GraphShopRoster.solicit`` against a Neo4j holding the broad corpus: two queries that had
    an honest empty answer came back soliciting an apparel store. The arithmetic is one
    identity word plus one variant word — exactly the arithmetic of
    ``test_a_variant_word_corroborates_a_partial_identity_match`` above — so no count can tell
    the two apart, and only WHICH word matched differs.

    Each assertion is checked twice on purpose: that the row is refused, and that it was
    refused for having one identity word rather than none. A rule that refused these because
    the arm was unreachable would pass the first half and be a different rule.
    """
    verdict = RULE.judge(query, name, variant_text=" ".join(variants))
    assert len(verdict.matched) == 1, f"this row must reach the arm with one word: {verdict}"
    assert verdict.corroborated, f"and with a variant word to offer: {verdict}"
    assert head_term(query) not in verdict.matched, verdict
    assert not verdict.about, verdict
    assert "which are not counted" in verdict.detail, verdict.detail


def test_the_head_the_identity_carries_is_what_opens_the_arm() -> None:
    """The same product and the same variant list, twice, with only the query's head moved.

    ``The Modular Table`` is a table whose walnut is in its variants. Asked for a walnut
    TABLE it is an answer; asked for a walnut LAMP it is not, and nothing about the product
    or its variants changed between the two calls — only whether the platform's own record
    says it is the kind of thing that was asked for.
    """
    table = RULE.judge(
        "a walnut side table", "The Modular Table", variant_text=" ".join(MODULAR_VARIANTS)
    )
    assert table.about and table.corroborated == ("walnut",), table

    lamp = RULE.judge(
        "a walnut modular lamp", "The Modular Table", variant_text=" ".join(MODULAR_VARIANTS)
    )
    assert not lamp.about, lamp
    assert lamp.matched == ("modular",), "the same arithmetic, one word of three"
    assert lamp.corroborated == ("walnut",), lamp


def test_a_variant_word_may_not_reach_the_count_arm_a_long_query_is_served_by() -> None:
    """The SECOND condition: the widened set meets the share arm, never the count arm.

    ``min(2, len(asked))`` is what lets a shopper type a sentence without being penalised for
    it — four agreeing words out of nine is 0.44 and the count arm keeps it. That licence
    belongs to the product's own crawled identity. Extended to a listing option it serves this
    row: ``supplement`` off a category and ``vegan`` off a variant name, two words out of
    eight, neither of them ``milk`` or ``thistle``, on a query whose head ``supplement`` every
    supplement in the catalogue carries.

    The control below it is the same rule admitting a three-word query on the same two-word
    widened set, which is where the share arm says yes.
    """
    query = "im looking for a high strength milk thistle supplement for liver support that is vegan"
    assert head_term(query) == "supplement", head_term(query)
    verdict = RULE.judge(
        query, "Elderberry Extract Capsules Supplements", variant_text="Vegan / 120 Capsules"
    )
    assert verdict.matched == ("supplement",), verdict
    assert verdict.corroborated == ("vegan",), verdict
    assert not verdict.about, verdict
    assert "even counted with those it is short" in verdict.detail, verdict.detail

    short = RULE.judge(
        "a walnut side table", "Nested Coffee Tables", variant_text=" ".join(NESTED_VARIANTS)
    )
    assert short.about, "two of three is half, and the share arm keeps it"


def test_a_variant_word_the_identity_already_carries_is_not_counted_twice() -> None:
    """``corroborated`` is what the variants ADD, not what they repeat.

    Two calls with identical arithmetic — one identity word, one variant word reaching the
    query — and opposite verdicts. The only difference is whether the variant's word was
    already in the platform's record of the product: ``Nested Coffee Tables`` carries
    ``table`` itself, so a variant option named ``Large Table`` adds nothing and the row stays
    refused. A variant option naming ``Walnut`` adds a word the identity did not have, and the
    row is served.

    Without the subtraction the first call answers ``about=True`` on ``table`` counted twice —
    once as the identity's and once as its own variant's — which is a furniture query served
    by one word wearing two hats.
    """
    repeated = RULE.judge(
        "a walnut side table for the lounge",
        "Nested Coffee Tables",
        variant_text="Small Table / Large Table",
    )
    assert repeated.matched == ("table",), repeated
    assert repeated.corroborated == (), "the identity already carried it"
    assert not repeated.about, repeated

    added = RULE.judge(
        "a walnut side table for the lounge",
        "Nested Coffee Tables",
        variant_text="Small Walnut / Large Walnut",
    )
    assert added.corroborated == ("walnut",), added
    assert added.about, added


def test_a_one_word_query_answered_only_by_a_variant_is_still_refused() -> None:
    """The CEILING of this arm, stated rather than left to be discovered.

    A shopper who types ``walnut`` and nothing else is asking a question this rule cannot
    answer off variant text. The query's head is ``walnut`` itself, the platform's record of
    ``Nested Coffee Tables`` does not carry it, and the arm never opens — even though a walnut
    variant of that product really does exist. That is the gate seen from the losing side.
    """
    verdict = RULE.judge("walnut", "Nested Coffee Tables", variant_text=" ".join(NESTED_VARIANTS))
    assert head_term("walnut") == "walnut"
    assert not verdict.about, verdict
    assert verdict.matched == (), verdict
    assert verdict.corroborated == ("walnut",), verdict


def test_a_refusal_names_which_of_the_two_conditions_refused_it() -> None:
    """THREE refusal sentences, one per way the arm declines, all three reachable as shipped.

    A refusal that named the wrong reason is the same defect as a refusal with no reason: a
    shopper told the variant words were counted and fell short, when the arm never opened,
    is reading a description of a rule that did not run.
    """
    no_identity = RULE.judge(
        "a walnut nightstand", "Nested Coffee Tables", variant_text=" ".join(NESTED_VARIANTS)
    )
    assert "cannot carry a match on its own" in no_identity.detail, no_identity.detail

    wrong_kind = RULE.judge(
        "a small white desk lamp",
        "Signature Crewneck - Pure White",
        variant_text="Small / Pure White",
    )
    assert "does not carry lamp, the thing the query asks for" in wrong_kind.detail, wrong_kind
    assert "even counted with those" not in wrong_kind.detail, wrong_kind.detail

    counted_and_short = RULE.judge(
        "a walnut coffee table for the living room",
        "The Modular Table",
        variant_text=" ".join(MODULAR_VARIANTS),
    )
    assert counted_and_short.matched == ("table",), counted_and_short
    assert counted_and_short.corroborated == ("walnut",), counted_and_short
    assert "even counted with those it is short" in counted_and_short.detail, counted_and_short

    shipped = RULE.judge("a walnut coffee table", "Milk Thistle")
    assert "needs 2 of them or half" in shipped.detail, shipped.detail


def test_a_refusal_that_did_count_the_variant_words_does_not_claim_it_could_not() -> None:
    """The same third sentence under a tightened rule, and the share phrase beside it.

    ``or half`` is a sentence about ``MIN_SHARED_SHARE`` and used to be a literal, true of
    the default and false of any other rule that printed it.
    """
    strict = TopicalRelevance(min_shared_terms=3, min_shared_share=1.0)
    verdict = strict.judge(
        "a walnut side table", "Nested Coffee Tables", variant_text=" ".join(NESTED_VARIANTS)
    )
    assert not verdict.about, verdict
    assert verdict.matched == ("table",) and verdict.corroborated == ("walnut",), verdict
    assert "even counted with those it is short" in verdict.detail, verdict.detail
    assert "cannot carry a match on its own" not in verdict.detail, verdict.detail
    assert "or half" not in verdict.detail, verdict.detail


def test_the_share_reports_the_identity_agreement_and_not_the_widened_one() -> None:
    """``share`` is the number the count/share arms decide on, and must keep saying so."""
    verdict = RULE.judge(
        "a walnut side table", "Nested Coffee Tables", variant_text=" ".join(NESTED_VARIANTS)
    )
    assert verdict.share == pytest.approx(1 / 3), verdict.share


@pytest.mark.parametrize(("query", "name"), SERVED_PAIRS + OFF_CORPUS_PAIRS)
def test_holding_no_variant_text_answers_exactly_what_the_identity_surface_answered(
    query: str, name: str
) -> None:
    """EVERY CALLER THAT HOLDS NO VARIANTS IS UNTOUCHED, over both fixture corpora.

    ``exchange.ranking.filters.organic_relevance_reason`` judges an
    :func:`~exchange.retrieval.relevance.identity_surface` — title and brand, with no variant
    text to give — and a graph that answers nothing for a product's variants leaves the
    retrieval layer in the same position. This is the regression that says the arm is
    additive: with no variant text the verdict is the pre-variant verdict, on all 31 pairs
    ``test_organic_relevance.py`` grades the thresholds with.
    """
    assert RULE.judge(query, name) == RULE.judge(query, name, variant_text="")
    assert RULE.judge(query, name).corroborated == ()


def test_a_row_the_identity_says_nothing_about_is_not_rescued_by_hostile_variant_text() -> None:
    """KEYWORD STUFFING, driven with the most favourable variant text an attacker has.

    Variant names are the crawl's reading of what a shop PUBLISHED, so a shop can choose them.
    The hostile text here is the shopper's own query, spelled into the variant list — the
    best a stuffer can do — and it is applied to BOTH directions rather than only the easy
    one:

    * rows whose crawled identity agrees with the query on NOTHING. The first version of this
      test attacked only these, which the arm closes by construction.
    * rows that already agree on ONE word, which is where a count-gated arm was open and this
      one is not: stuffing buys a row nothing unless the platform's own record already says
      it is the kind of thing asked for.
    """
    zero_word = one_word = 0
    for query, name in OFF_CORPUS_PAIRS + tuple((q, n) for q, n, _ in ADVERSARIAL_ROWS):
        matched = RULE.judge(query, name).matched
        if len(matched) > 1 or head_term(query) in matched:
            continue
        if matched:
            one_word += 1
        else:
            zero_word += 1
        hostile = " ".join(content_terms(query))
        assert not RULE.judge(query, name, variant_text=hostile).about, (query, name)
    assert zero_word >= 8, f"only {zero_word} pairs have a zero-word identity to test"
    assert one_word >= 5, f"only {one_word} pairs have a one-word identity to test"


def test_every_phrase_break_is_a_word_the_rule_already_drops() -> None:
    """:data:`PHRASE_BREAK_WORDS` may decide WHERE the leading phrase ends, and nothing else.

    Every break word is also a stopword, so no word the comparison would have used can be
    consumed by the head walk. A break word that was NOT a stopword would silently delete a
    content word from :func:`head_term`'s answer while
    :func:`~exchange.retrieval.relevance.content_terms` kept it, and the two sides of this
    rule would be reading different queries.
    """
    assert PHRASE_BREAK_WORDS <= STOPWORDS, sorted(PHRASE_BREAK_WORDS - STOPWORDS)


def test_the_head_of_a_query_is_the_thing_asked_for_and_not_the_last_word_typed() -> None:
    """The trailing prepositional phrase is what this function exists to survive.

    ``a walnut coffee table for the lounge`` ends in ``lounge`` and asks for a table. A break
    BEFORE any content word is skipped rather than taken, which is what keeps ``looking for``
    and ``something to help`` from answering with nothing.
    """
    assert head_term("a walnut coffee table for the lounge") == "table"
    assert head_term("a large green ceramic plant pot") == "pot"
    assert head_term("a small white desk lamp") == "lamp"
    assert head_term("something to help my joints") == "joint"
    assert head_term("running shoes for trail marathons") == "shoe"
    assert head_term("") == ""
    assert head_term("for the of") == ""


# =====================================================================================
# 2. The surface, and the candidate that carries it
# =====================================================================================


def test_variant_surface_is_empty_for_a_candidate_that_carries_no_variants() -> None:
    """A source that does not populate variant names changes nothing, silently and correctly."""
    plain = make_candidate({"product_id": "p-1", "canonical_name": "Milk Thistle"})
    assert variant_surface(plain) == ""
    assert candidate_surface(plain) == "Milk Thistle"


def test_variant_surface_is_separate_from_the_identity_surface() -> None:
    """The two are not one string, because the rule pays them differently.

    A single pooled surface is the change this module deliberately did not make; if these two
    ever return the same text, :func:`~exchange.retrieval.relevance.head_term` has nothing
    left to gate.
    """
    candidate = make_candidate(
        {
            "product_id": "p-lift",
            "canonical_name": "The Lift Off Coffee Table",
            "brand": "RIZE",
            "variant_names": LIFT_OFF_VARIANTS,
        }
    )
    assert isinstance(candidate, VariantCandidate)
    assert "Walnut" in variant_surface(candidate)
    assert "Walnut" not in candidate_surface(candidate)


def test_a_variant_candidate_is_still_a_candidate() -> None:
    """It adds a field and overrides nothing, so every existing consumer keeps working."""
    candidate = make_candidate(
        {"product_id": "p-lift", "canonical_name": "The Modular Table", "similarity": 0.5},
        query_text="a walnut side table",
    )
    assert candidate.cosine == pytest.approx(0.5)
    assert candidate.scored is True
    assert candidate.variant_names == ()
    assert isinstance(candidate, VariantCandidate)


# =====================================================================================
# 3. Through the pipeline, over the deterministic double
# =====================================================================================
#: Two floydhome.com products as the broad corpus records them, with the variant names the
#: crawl actually collected. ``The Modular Table`` is the row the walnut query gains.
FURNITURE = (
    {
        "product_id": "p-lift",
        "canonical_name": "The Lift Off Coffee Table",
        "brand": "RIZE",
        "variant_names": LIFT_OFF_VARIANTS,
    },
    {
        "product_id": "p-nested",
        "canonical_name": "Nested Coffee Tables",
        "brand": "Branch",
        "variant_names": NESTED_VARIANTS,
    },
    {
        "product_id": "p-birch",
        "canonical_name": "The Shelving System",
        "brand": "RIZE",
        "variant_names": ("Birch / Small", "Birch / Large"),
    },
)


def test_retrieval_keeps_a_table_whose_walnut_is_only_in_its_variants() -> None:
    """The served pipeline, not the rule in isolation: the arm is reachable from ``retrieve``.

    ``a walnut side table`` carries ``table`` against both tables' names and ``walnut`` against
    neither — one of three content words, which the identity arms refuse. The shelving system
    is the control: it is birch, it has no ``table`` in its name, and it stays refused.
    """
    result = CandidateRetrieval(InMemoryCandidateSource(FURNITURE)).retrieve(
        _intent("a walnut side table")
    )
    assert set(result.product_ids) == {"p-lift", "p-nested"}, result.product_ids
    assert [row.product_id for row in result.off_topic] == ["p-birch"]


def test_retrieval_does_not_answer_a_nightstand_query_with_a_coffee_table() -> None:
    """The same three products, a query whose only agreement is a colour. Nothing survives."""
    result = CandidateRetrieval(InMemoryCandidateSource(FURNITURE)).retrieve(
        _intent("a walnut nightstand")
    )
    assert result.assessments == (), result.product_ids
    assert {row.product_id for row in result.off_topic} == {"p-lift", "p-nested", "p-birch"}


def test_retrieval_does_not_answer_a_plant_pot_query_with_a_wardrobe_of_hoodies() -> None:
    """THE SERVED-ROUTE REGRESSION, through the pipeline rather than the rule.

    Three real apparel rows out of ``fixtures/real-catalogs-broad``, each agreeing with ``a
    large green ceramic plant pot`` on ``green`` and offering ``large`` in its variant names.
    Measured through ``GraphShopRoster.solicit`` against a Neo4j holding the broad corpus,
    this query went from an honest empty roster to soliciting an apparel store; here it is the
    whole result, so a row surviving anywhere in it fails this test.
    """
    apparel = tuple(
        {
            "product_id": f"p-apparel-{index}",
            "canonical_name": name,
            "variant_names": variants,
        }
        for index, (query, name, variants) in enumerate(ADVERSARIAL_ROWS)
        if query == "a large green ceramic plant pot"
    )
    assert len(apparel) >= 2, "this test needs the plant-pot rows of the adversarial corpus"
    result = CandidateRetrieval(InMemoryCandidateSource(apparel)).retrieve(
        _intent("a large green ceramic plant pot")
    )
    assert result.assessments == (), result.product_ids
    assert len(result.off_topic) == len(apparel)


def test_a_supplements_catalogue_still_answers_a_furniture_query_with_nothing() -> None:
    """The off-corpus direction through the pipeline, with variant names present and unhelpful.

    These are the recorded corpus's own variant spellings (``fixtures/real-catalogs``, 3,093
    products, 9,667 variants): a supplement's variants are its form and its count. Widening
    the surface to include them must not make a supplements catalogue answer a furniture
    query, and this asserts on the whole result rather than on one row.
    """
    supplements = (
        {
            "product_id": "p-milk",
            "canonical_name": "Milk Thistle Gummies",
            "brand": "Gaia Herbs",
            "variant_names": ("Capsule / 60 Capsules", "Powder / 250 Grams (8.8 oz)"),
        },
        {
            "product_id": "p-caffeine",
            "canonical_name": "Caffeine Powder Pure (Natural Coffee Bean)",
            "variant_names": ("Powder / 100 Grams (3.5 oz)", "Powder / 1 Kilogram (2.2 lbs)"),
        },
    )
    result = CandidateRetrieval(InMemoryCandidateSource(supplements)).retrieve(
        _intent("a walnut coffee table for the lounge")
    )
    assert result.assessments == (), result.product_ids
    assert len(result.off_topic) == 2


def test_the_rule_name_moved_because_the_rule_moved() -> None:
    """A behaviour change under an unchanged version string is an audit trail that lies."""
    assert TopicalRelevance.name == "content-word-agreement/2"


# =====================================================================================
# 4. The graph read — the only place the variant names actually come from
# =====================================================================================

_CRAWL_SOURCE_ID = "src-variant-crawl"
_SELLER_SOURCE_ID = "src-variant-seller"


def _sources() -> tuple[Any, Any]:
    """The platform's own crawl, and a seller's assertion, as ``ingest.graph.Source`` rows."""
    from ingest.graph import Source

    crawl = Source(
        source_id=_CRAWL_SOURCE_ID,
        url="https://floydhome.example/products/the-lift-off-coffee-table",
        content_hash="sha256:variantcrawl",
        observed_at="2026-01-01T00:00:00Z",
        extractor_version="variant@1",
        confidence=0.9,
        source_class="scraped",
    )
    asserted = Source(
        source_id=_SELLER_SOURCE_ID,
        url="https://floydhome.example/feed",
        content_hash="sha256:variantseller",
        observed_at="2026-01-01T00:00:00Z",
        extractor_version="variant@1",
        confidence=0.9,
        source_class="seller_asserted",
    )
    return crawl, asserted


def _seed_two_products(session: Any) -> tuple[Any, Any]:
    """Two products under a platform-observed crawl, and that ``Source`` plus a seller one."""
    from ingest.graph import apply_schema, seed_products, upsert_source

    apply_schema(session)
    crawl, asserted = _sources()
    # The seller's `Source` node must EXIST for the two divergence tests below to be about
    # its `source_class` rather than about a dangling id: `_VARIANT_NAMES`' edge conjunct
    # asks whether a Source with that id is platform-observed, and an absent node refuses
    # for the wrong reason.
    upsert_source(session, asserted)
    seed_products(
        session,
        [
            {
                "product_id": "p-lift",
                "canonical_name": "The Lift Off Coffee Table",
                "category": "tables",
            },
            {
                "product_id": "p-nested",
                "canonical_name": "Nested Coffee Tables",
                "category": "tables",
            },
        ],
        source=crawl,
    )
    return crawl, asserted


def _fetch(session: Any, query: str = "a walnut side table") -> dict[str, tuple[str, ...]]:
    """Every candidate the served source returns for ``query``, by product id."""
    from exchange.retrieval import GraphCandidateSource
    from exchange.retrieval.criteria import build_query
    from ingest.embeddings import HashEmbedding

    source = GraphCandidateSource(session, provider=HashEmbedding())
    names: dict[str, tuple[str, ...]] = {}
    for candidate in source.fetch(build_query(_intent(query))):
        assert isinstance(candidate, VariantCandidate), type(candidate)
        names[candidate.product_id] = candidate.variant_names
    return names


@pytest.mark.docker
@pytest.mark.graph
def test_variant_names_reach_the_relevance_rule_from_the_graph(neo4j_session: Any) -> None:
    """THE SERVED SOURCE, not the double: ``GraphCandidateSource`` returns the variant names.

    Everything above this line runs against ``InMemoryCandidateSource``, which is handed its
    variant names in a dict. This is the one node that proves the graph path populates them —
    that ``_VARIANT_NAMES`` runs, matches, and lands on the candidate the relevance rule then
    judges. Without it the whole arm could be wired to a field nothing ever sets.
    """
    from ingest.embeddings import HashEmbedding
    from ingest.graph import Variant, reembed_products, upsert_variant

    crawl, _ = _seed_two_products(neo4j_session)
    upsert_variant(
        neo4j_session,
        Variant(variant_id="var-lift-walnut", seller_sku="LIFT-WAL", name="Walnut / Black"),
        product_id="p-lift",
        source=crawl,
    )
    reembed_products(neo4j_session, HashEmbedding())

    names = _fetch(neo4j_session)
    assert names["p-lift"] == ("Walnut / Black",), names
    assert RULE.judge(
        "a walnut side table",
        "The Lift Off Coffee Table",
        variant_text=" ".join(names["p-lift"]),
    ).corroborated == ("walnut",)


@pytest.mark.docker
@pytest.mark.graph
def test_a_variant_the_platform_did_not_observe_is_not_read(neo4j_session: Any) -> None:
    """D55: a shop's own assertion may not become the platform's own surface.

    ``seller_asserted`` is perfectly good provenance for "the store said so" and is exactly
    what ``PLATFORM_OBSERVED_SOURCE_CLASSES`` excludes. A variant written under it is a shop
    choosing the words the platform's relevance rule decides on, which is the organic voice
    wearing the sponsored one's freedom.

    **Both products are asserted on, and that is what makes this test grade the gate rather
    than the read.** ``variant_names == ()`` is the answer a working gate gives AND the answer
    a broken Cypher gives, so a test naming only the refused product passes over a statement
    that returns nothing at all. The kept row is the control that separates them.
    """
    from ingest.embeddings import HashEmbedding
    from ingest.graph import Variant, reembed_products, upsert_variant

    crawl, asserted = _seed_two_products(neo4j_session)
    upsert_variant(
        neo4j_session,
        Variant(variant_id="var-lift-claim", seller_sku="LIFT-CLAIM", name="Walnut / Black"),
        product_id="p-lift",
        source=asserted,
    )
    upsert_variant(
        neo4j_session,
        Variant(variant_id="var-nested-crawl", seller_sku="NEST-WAL", name="Walnut / Medium"),
        product_id="p-nested",
        source=crawl,
    )
    reembed_products(neo4j_session, HashEmbedding())

    names = _fetch(neo4j_session)
    assert names["p-lift"] == (), names
    assert names["p-nested"] == ("Walnut / Medium",), names


@pytest.mark.docker
@pytest.mark.graph
def test_a_variant_node_the_platform_did_not_support_is_not_read(neo4j_session: Any) -> None:
    """THE FIRST CONJUNCT ALONE: the ``Variant`` node's own ``SUPPORTED_BY``.

    ``upsert_variant`` writes the node's provenance and the edge's from ONE ``source=``
    argument, so no call through that function can produce a variant whose node and edge
    disagree — which is why deleting either half of the gate used to leave the whole suite
    green. The divergent state is written here in Cypher, because it is the state a second
    writer against this graph could produce and the state the gate's second conjunct exists
    for.

    Here the HAS_VARIANT edge stays the platform's and only the node's support is moved to a
    seller. The sibling product keeps both, so a dead statement fails this test too.
    """
    from ingest.embeddings import HashEmbedding
    from ingest.graph import Variant, reembed_products, upsert_variant

    crawl, _ = _seed_two_products(neo4j_session)
    for product_id, variant_id in (("p-lift", "var-lift-node"), ("p-nested", "var-nested-node")):
        upsert_variant(
            neo4j_session,
            Variant(variant_id=variant_id, seller_sku=variant_id, name="Walnut / Black"),
            product_id=product_id,
            source=crawl,
        )
    neo4j_session.run(
        "MATCH (s:Source {source_id: $seller}) WITH s "
        "MATCH (v:Variant {variant_id: 'var-lift-node'})-[r:SUPPORTED_BY]->(:Source) "
        "DELETE r MERGE (v)-[:SUPPORTED_BY]->(s)",
        seller=_SELLER_SOURCE_ID,
    ).consume()
    reembed_products(neo4j_session, HashEmbedding())

    names = _fetch(neo4j_session)
    assert names["p-lift"] == (), names
    assert names["p-nested"] == ("Walnut / Black",), names


@pytest.mark.docker
@pytest.mark.graph
def test_a_variant_edge_the_platform_did_not_author_is_not_read(neo4j_session: Any) -> None:
    """THE SECOND CONJUNCT ALONE: the ``HAS_VARIANT`` edge's ``source_id``.

    The node keeps the platform's own support and only the ATTACHMENT is a seller's. This is
    the failure the second conjunct is argued for in ``_VARIANT_NAMES``' own note — "a store
    could attach its own words to somebody else's crawled product" — and until this test
    nothing in the tree could produce it, so that conjunct could be deleted with every suite
    still green.
    """
    from ingest.embeddings import HashEmbedding
    from ingest.graph import Variant, reembed_products, upsert_variant

    crawl, _ = _seed_two_products(neo4j_session)
    for product_id, variant_id in (("p-lift", "var-lift-edge"), ("p-nested", "var-nested-edge")):
        upsert_variant(
            neo4j_session,
            Variant(variant_id=variant_id, seller_sku=variant_id, name="Walnut / Black"),
            product_id=product_id,
            source=crawl,
        )
    neo4j_session.run(
        "MATCH (:Product {product_id: 'p-lift'})-[hv:HAS_VARIANT]->"
        "(:Variant {variant_id: 'var-lift-edge'}) SET hv.source_id = $seller",
        seller=_SELLER_SOURCE_ID,
    ).consume()
    reembed_products(neo4j_session, HashEmbedding())

    names = _fetch(neo4j_session)
    assert names["p-lift"] == (), names
    assert names["p-nested"] == ("Walnut / Black",), names


@pytest.mark.docker
@pytest.mark.graph
def test_the_ceiling_truncates_a_product_with_more_variants_than_it_allows(
    neo4j_session: Any,
) -> None:
    """:data:`VARIANT_NAMES_PER_PRODUCT` is applied, not merely declared.

    Its only previous test asserted ``1 < VARIANT_NAMES_PER_PRODUCT < 1000`` — a range check
    on a literal, which is green for a ceiling of 999 and for a Cypher with the
    ``names[..$per_product]`` slice deleted. The constant truncates real rows (one product of
    the shipped corpus and 88 of the broad one), so it is driven here: a product carrying more
    variants than the ceiling comes back holding exactly the ceiling, in the ``variant_id``
    order the statement sorts by.

    This node cannot grade a ceiling that is too LOW, because it reads the constant and a
    ceiling of 1 moves the assertion with it. Its sibling below grades that direction.
    """
    from ingest.embeddings import HashEmbedding
    from ingest.graph import Variant, reembed_products, upsert_variant

    crawl, _ = _seed_two_products(neo4j_session)
    over = VARIANT_NAMES_PER_PRODUCT + 3
    for index in range(over):
        upsert_variant(
            neo4j_session,
            Variant(
                variant_id=f"var-lift-{index:04d}",
                seller_sku=f"LIFT-{index:04d}",
                name=f"Walnut / Panel {index:04d}",
            ),
            product_id="p-lift",
            source=crawl,
        )
    reembed_products(neo4j_session, HashEmbedding())

    names = _fetch(neo4j_session)
    assert len(names["p-lift"]) == VARIANT_NAMES_PER_PRODUCT, len(names["p-lift"])
    assert names["p-lift"][0] == "Walnut / Panel 0000", names["p-lift"][:2]
    assert f"Walnut / Panel {over - 1:04d}" not in names["p-lift"]


@pytest.mark.docker
@pytest.mark.graph
def test_the_ceiling_is_above_the_variant_lists_real_stores_publish(neo4j_session: Any) -> None:
    """THE OTHER DIRECTION, which a ceiling read off the constant cannot grade.

    ``floydhome.com`` publishes 36 variants for ``The Lift Off Coffee Table``, and the walnut
    ones are not first: the crawl collects birch before walnut. A ceiling that truncates below
    a real store's variant list does not fail loudly — it silently returns the first few
    options and the relevance rule never sees the material, which is the whole surface this
    module added. So the assertion is behavioural rather than arithmetic: with a real
    36-variant list whose only walnut option is the LAST one, the arm must still corroborate.
    (The names differ in their panel width, because ``_VARIANT_NAMES`` collects DISTINCT names
    and 36 variants spelled alike are one name to it.)

    Measured over ``fixtures/*/stores/*.products.jsonl.gz``: mean 3.13 variants per product on
    the shipped corpus and 5.58 on the broad one, with 36 for this product. A ceiling of 1 —
    or of anything under 36 — turns this red where the sibling above stays green.
    """
    from ingest.embeddings import HashEmbedding
    from ingest.graph import Variant, reembed_products, upsert_variant

    crawl, _ = _seed_two_products(neo4j_session)
    published = 36
    for index in range(published):
        finish = "Walnut" if index == published - 1 else "Birch"
        upsert_variant(
            neo4j_session,
            Variant(
                variant_id=f"var-lift-{index:04d}",
                seller_sku=f"LIFT-{index:04d}",
                name=f'One Panel - {index}" w x 67" l x 15" h / {finish} / Black',
            ),
            product_id="p-lift",
            source=crawl,
        )
    reembed_products(neo4j_session, HashEmbedding())

    names = _fetch(neo4j_session)
    assert len(names["p-lift"]) == published, len(names["p-lift"])
    verdict = RULE.judge(
        "a walnut side table",
        "The Lift Off Coffee Table",
        variant_text=" ".join(names["p-lift"]),
    )
    assert verdict.corroborated == ("walnut",), verdict
    assert verdict.about, verdict
