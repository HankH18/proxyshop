"""IS THIS RESULT ABOUT WHAT WAS ASKED — the organic half's own honesty check (D55).

What this module is, in one sentence: it decides whether a product the PLATFORM chose to show
is about the query the shopper typed, so that an off-corpus question gets an honest empty
answer with a reason instead of a confident wrong one.

Why it had to exist
-------------------
Measured on the served route before this module, with the recorded corpus loaded (3,093
products, ten real supplement storefronts) and the exchange's own graph roster answering::

    POST /auctions  {"query": "a walnut coffee table for the lounge"}
      -> roster_source {"source": "neo4j", "shops": 4, "products_considered": 25}
      -> 4 slots: The Capsule Machine / Nutricost Protein for Women / Nattokinase / ...

Nothing was broken in the sense of raising. Retrieval asked its vector index for the top
twenty-five products, the index returned twenty-five, and every one of them was scored,
rostered and shown. **The index always returns its top k.** "Top k of nothing relevant" and
"top k of a good match" are the same shape of answer, and until this module nothing in the
exchange could tell them apart.

The number the exchange had in hand did not separate them either, which is the measurement
that decided this module's rule. Over 3,093 products with the configured ``lexical`` provider,
the best cosine for a query the corpus genuinely serves and the best cosine for one it does not
OVERLAP::

    "probiotics for gut health"   best cosine 0.3953   <- served by the corpus
    "a wool overcoat in navy"     best cosine 0.4087   <- not served by anything here

So there is no similarity floor to set. The reason is in ``proxyshop_support.embedding``'s own
header: a lexical cosine is ``0.6 * word agreement + 0.4 * spelling agreement``, and against a
composed product document (title, brand, categories, attributes, ingredients — see
``ingest.graph.reembed.embedding_text``) the spelling half alone has a floor around ``0.25``,
because unrelated English strings share letter runs. A furniture query scores 0.34 on trigram
noise; a real but thin match scores 0.40 on meaning. One threshold cannot have both.

The rule, and why it is this one
--------------------------------
**Words, not spelling.** The half of the retrieval measure that carries meaning is whole-word
agreement, so that is the half this module decides on, exactly and by itself instead of blended
into a float. A product is *about* a query when the platform's own observed identity of it
carries the query's own content words:

* at least :data:`MIN_SHARED_TERMS` of them, **or**
* at least :data:`MIN_SHARED_SHARE` of them, whichever is easier to satisfy.

Both arms are load-bearing and each one exists to stop the other refusing honest traffic. The
count alone would refuse ``"zinc lozenges"`` against ``Zinc Oxide`` — one word out of two, which
is half the question answered. The share alone would refuse ``"im looking for a high strength
milk thistle supplement for liver support that is vegan"``, where nine content words dilute four
real matches to 0.44. A shopper is not penalised for typing a sentence, and a two-word query is
not held to a standard a nine-word one is excused from.

Measured over 67 queries against the recorded corpus — 37 the catalogue genuinely serves, 30 it
does not, both sets written before the thresholds were chosen and both including short keyword
queries and long conversational ones. Each query's top 25 through the real ``product_embedding``
index, and the same ablation for the alternatives that were considered:

    rule                                       honest served   off-corpus served
    ------------------------------------------ --------------  -----------------
    shipped (>= 2 content words, or >= half)    35 / 37             0 / 30
    one shared content word is enough           36 / 37             7 / 30
    the count arm alone (no share arm)          32 / 37             0 / 30
    the share arm alone (no count arm)          30 / 37             0 / 30
    the shipped rule with no stopword list      36 / 37             3 / 30

**Every cell is the recorded corpus and nothing else**, verified at ``MATCH (p:Product) RETURN
count(p)`` = 3,093 immediately before the run. An earlier printing of this table read ``2/30``,
``12/30``, ``2/30``, ``2/30``, ``5/30`` down that column, measured on a shared Neo4j that also
held another lane's 24-product fixture — keyboards, skillets, scarves, espresso machines and
trail running shoes (D37: the graph is one database) — so ``"running shoes for trail marathons"``
and ``"an espresso machine with a milk frother"`` were served by a catalogue that really did
carry them. That contamination inflated the WHOLE column, not just the shipped row it was
annotated on. The arguments below are the ones that survived the re-measurement; only the
numbers moved.

Read the columns together, because each alternative is cheap on one of them and ruinous on the
other. Accepting ONE shared content word answers seven off-corpus queries wrongly for one extra
honest query — the single words it lets through are ``coffee`` (from ``Caffeine Powder Pure
(Natural Coffee Bean)``), ``frother``, ``kids``, ``winter``, ``focus``, ``baking``, ``support``
— which is why :data:`MIN_SHARED_TERMS` is two. Dropping the stopword list is the same trade
smaller: the extra three are queries agreeing with a product on ``for`` and ``a``. And each arm
alone loses honest queries the other keeps: three and five of them respectively.

Zero of thirty is the positive control this rule most needed, and it is the number to re-take
after any change here: it refuses a query the CATALOGUE has nothing for.

The two honest queries that are not served are ``"something to help my joints"`` and ``"a
supplement for hair growth"``, and they are RECALL failures of the retriever rather than
refusals by this rule: the vector index surfaced no joint or hair product to judge. Their answer
before this module was four confidently wrong supplements; their answer now is nothing, with a
reason. That is the trade this module is, stated plainly rather than rounded up.

The variant arm, and the word that was in the index all along
--------------------------------------------------------------
``identity_surface`` reads title and brand; ``candidate_surface`` adds the graph's
categories, ingredients and attribute keys. Neither reads ``Variant.name``, and for a whole
class of product that is where the answer is. Measured over ``fixtures/real-catalogs-broad``
directly — every row of every ``stores/*.products.jsonl.gz``, 17,409 products and 97,217
variant names — **109 products offer a walnut finish and not one of them says so anywhere the
old surfaces read**::

    floydhome.com        "The Lift Off Coffee Table"
        variant  'One Panel - 18" w x 67" l x 15" h / Walnut / Black'
    branchfurniture.com  "Nested Coffee Tables"   variant 'Walnut / Medium'

109 products carry ``walnut`` in a variant name, 20 carry it in the crawled name, brand or
category, and **the two sets are disjoint**. Of the six products whose crawled name contains
``coffee table``, five offer a walnut variant and none of the five names walnut.

So :func:`variant_surface` is read, and :meth:`TopicalRelevance.judge` takes it as a third,
weaker argument. **It may add to a match and may not make one**, and the whole of that
weighting is :func:`head_term`: the platform's own record must already carry the word saying
WHAT KIND OF THING the shopper asked for, before any word out of a listing option is counted
at all. A variant list is colours, sizes, materials and counts — modifiers — so a variant name
may describe a product and may not decide what the product IS.

**The version of this arm that gated on a COUNT of identity words instead had to be thrown
away, and the measurement that killed it is the reason this gate is structural.** With the
arm opened by any one identity word, driven over the broad corpus above (17,409 products,
97,217 variant names) against 22 queries the corpus has nothing for::

    gate                                  furniture rows        apparel rows   off-corpus rows
                                          for 12 furniture queries             for 22 queries
    ------------------------------------- ---------------  ----------------  ----------------
    identity surface alone, no variant arm       73                 0                19
    any one identity word                       171                22               204
    the head term, plus the share arm           142                 0                28

``a large green ceramic plant pot`` answered by ``Women's Afternoon Hoodie in Sage Green``,
on ``green`` from the title and ``large`` from a garment size; ``a small white desk lamp`` by
``Signature Crewneck - Pure White`` on ``white`` plus ``small``. A count of one cannot tell
those from ``a walnut side table`` against ``Nested Coffee Tables``, because the arithmetic is
identical — one identity word, one variant word — and only WHICH word differs. So the gate is
on which word.

The second condition is that the widened agreement must meet :data:`MIN_SHARED_SHARE`; the
COUNT arm is not available to it. That licence — two agreeing words out of nine — belongs to
the product's own crawled identity, and a listing option does not inherit it. Measured, with
the head gate in place and the count arm still available to the widened set, ``Elderberry
Extract Capsules`` answers ``im looking for a high strength milk thistle supplement for liver
support that is vegan`` on ``supplement`` from a category plus ``vegan`` from a variant name.
Neither word is ``milk`` or ``thistle``.

THE SERVED ROUTE, which is where a rule-level sweep has to be re-taken
----------------------------------------------------------------------
:class:`~exchange.retrieval.service.CandidateRetrieval` over
:class:`~exchange.retrieval.sources.GraphCandidateSource`, against a private Neo4j holding
seven stores of ``fixtures/real-catalogs-broad`` — furniture, apparel, coffee equipment and
outdoor gear — verified at ``Product`` 4,253, ``Variant`` 33,110, ``Store`` 7, products
missing an embedding 0 immediately before the run. Twelve queries a furniture catalogue
answers and twenty-two it has nothing for, each arm ablated on the same graph::

    arm                                  honest served   rows    off-corpus served   rows
    ------------------------------------ --------------  -----   -----------------  -----
    identity surface alone               9 / 12            57    4 / 22                 8
    the variant arm gated on a count     10 / 12           87    8 / 22                32
    the variant arm as it stands here    10 / 12           84    4 / 22                 8

**The off-corpus column is byte-identical to the pre-variant rule** — the same four queries,
the same eight rows — while the honest column gains 27 rows and one whole query. The count
gate doubled the off-corpus queries served and quadrupled the rows. Per query, the rows this
arm adds::

    "a white oak dining table"               0 rows  ->   8 rows
    "a walnut coffee table for the lounge"   3 rows  ->   8 rows
    "a walnut side table"                    3 rows  ->  11 rows
    "a standing desk in walnut"              9 rows  ->  13 rows
    "a large green ceramic plant pot"        0 rows  ->   0 rows
    "a small white desk lamp"                4 rows  ->   4 rows

and through :meth:`~exchange.retrieval.roster.GraphShopRoster.solicit` on that same graph,
``"a large green ceramic plant pot"`` solicits nobody both before and after, where the
count-gated arm solicits ``rumpl.com`` on a ``Sage Green`` mat.

What this arm costs is one family of query and it is named rather than averaged away: ``"a
queen bed frame in birch"`` goes 8 rows -> 8 where the count gate gave 11, because the head
of that phrase is ``frame`` and the beds are called ``The Floyd Bed``. See :func:`head_term`.

**What it does NOT widen is RECALL, and that is the honest half.**
``ingest.graph.reembed.embedding_text`` composes a product's vector out of name, brand,
categories, attributes and ingredients, so no walnut word is in any embedding and the vector
index cannot rank on one. This arm only ever rescues a product the index had already returned
for its other words. Reaching the 109 through search itself is a change to that function, in
another package, and not one this module can make.

Re-taken on the recorded corpus for the discipline this widening could have cost — a private
Neo4j holding ``fixtures/real-catalogs``, verified at 3,093 ``Product``, 9,667 ``Variant``,
10 ``Store`` and 0 products missing an embedding immediately before the run, over the 16
honest queries of ``test_organic_relevance.py``'s ``SERVED_PAIRS`` and 18 off-corpus ones::

    arm                                  honest served   rows    off-corpus served   rows
    ------------------------------------ --------------  -----   -----------------  -----
    identity surface alone               15 / 16          169    1 / 18                 5
    the variant arm gated on a count     15 / 16          169    1 / 18                 5
    the variant arm as it stands here    15 / 16          169    1 / 18                 5

**Not one query's result set changed size, in any direction, for any of the three.** A
supplement's variants are its form and its count (``Powder / 250 Grams (8.8 oz)``, ``Capsule
/ 60 Capsules``), so there is nothing in them for a furniture query to agree with — which is
exactly why this corpus cannot be the evidence that a widening is safe. **A corpus whose
variants are doses cannot express the cost of reading colours**, and the count-gated arm
passed here and failed on the corpus above. The one off-corpus query served is ``"a stainless
steel water bottle"``, and it is a fault in the query list rather than a leak: this catalogue
really does sell ``Nutricost Trimr Classic Bottle (Black)`` and a ``5 Piece Stainless Steel
Spice Measuring Set``, before the change and after.

WHAT THE RANKING LAYER THEN DOES WITH THESE ROWS, which is a gap this arm cannot close
--------------------------------------------------------------------------------------
:func:`exchange.ranking.filters.organic_relevance_reason` re-judges an organic FALLBACK row —
one no shop bid for — against :func:`identity_surface`, which is title and brand and neither
categories nor variants. **Every row this arm rescues is refused there**, and it is a theorem
rather than a measurement: the arm is reached only when the wider surface carried fewer than
``min(2, len(asked))`` words and less than :data:`MIN_SHARED_SHARE` of them, and the narrower
surface is a subset of the wider one, so it cannot carry more. The only way through is
``shopper_named_the_product``. Measured over the broad corpus and 12 furniture queries, all
69 of the rows this arm adds are refused there, and so are all 123 the count-gated version
added.

**The gap is not this arm's, and reading it as this arm's would fix the wrong layer.** The
same measurement over the SAME rows with the variant arm removed entirely: 34 of the 93 rows
the identity surface alone keeps (36.6%) are refused by that layer too, on categories it
holds none of — ``"a queen bed frame in birch"`` keeps ``Percale Flat Sheet - Full/Queen`` on
``queen`` and ``bed`` out of a category and the title-only rule refuses it. On the recorded
corpus it is 58 of 1,355 (4.3%). The disagreement is the two-surface design's, it predates
this arm, and that function's own docstring already names the fix — judge both layers on one
surface, rather than loosening the narrower one.

WHOSE WORDS ARE COMPARED, which is the D55 argument
---------------------------------------------------
The query is the SHOPPER's. The surface it is compared against is the PLATFORM's — the crawled
``canonical_name``, ``brand``, categories, ingredients, attribute keys and variant names that
:mod:`~exchange.retrieval.catalogue` and ``ingest.graph`` hold because the platform observed
them. **A shop's own message, pitch or claims are never read here**, and that is not an
oversight to be tidied up later: relevance decided on a seller's prose would let a shop assert
its way onto a shortlist for a query the platform's own crawl says it has nothing for, which is
precisely the blurring of the organic and sponsored voices D55 exists to prevent.

The variant names are the newest thing on that list and the one closest to the line, so the
gate on them is stated rather than implied. ``Variant.name`` is what a shop PUBLISHES on its
own storefront and the crawl reads, which is not the same as a shop telling the platform about
itself — and :data:`~ingest.graph.query.PLATFORM_OBSERVED_SOURCE_CLASSES` is where the
difference is enforced: :data:`exchange.retrieval.sources._VARIANT_NAMES` reads a variant only
when the ``Variant`` node **and** the ``HAS_VARIANT`` edge are each supported by a ``Source``
the platform authored, so a ``seller_asserted`` variant reaches no surface at all. What that
gate does not stop, and no gate at this layer could, is a shop choosing what to publish: a
storefront can spell its variants to catch a query, and :func:`head_term` is the bound on what
that buys — a row whose crawled identity does not say it is the kind of thing asked for is
refused however its variants are spelled, and a row whose crawled identity agrees with the
query on nothing has no head term either.

It follows that this module judges the ORGANIC half. A sponsored row is a shop that was
solicited because the platform assigned the intent to a cluster that shop pursues, and that
chose which of its own products to put forward; the shop is accountable for it, its message is
adversarially checked, and its trust record moves. An organic row is one the platform
manufactured for itself — the platform picked the product and wrote the pitch — so nobody else
is accountable for it and the platform's own crawl has to be the check. The callers wire that
distinction; see ``ranking.rank``'s ``product_identities`` argument.

The three ways this refuses to decide, and why each fails OPEN
--------------------------------------------------------------
A gate written only in the direction of the attack refuses honest traffic in the direction
nobody tested, so the undecidable cases are enumerated here rather than left to fall out:

1. **The query has no content words.** ``"something for the best"`` folds to nothing this can
   compare. Emptying a shortlist on it would be a refusal with no evidence behind it.
2. **The platform observed no identity for this product.** An exchange whose catalogue is
   unwired holds no crawled name for anything, and refusing every row there would turn a
   misconfiguration into "the catalogue serves nothing" — the same direction
   ``ranking.rank``'s ``network_attributes=None`` already refuses to take.
3. **The product's observed identity folds to nothing** — a name of punctuation alone.

In all three :attr:`RelevanceVerdict.decidable` is ``False`` and :attr:`RelevanceVerdict.about`
is ``True``, so the row is kept and the verdict says which side could not be read. This filter
only ever fires when both sides were legible and did not meet.

What a lexical agreement cannot do
----------------------------------
The same limit ``proxyshop_support.embedding.lexical_embed`` states, restated because a filter
inherits its measure's blind spots: **there are no semantics here.** ``"laptop"`` and
``"notebook computer"`` share no word, so this module would call a notebook off-topic for a
laptop query. That is not a new loss — the retriever scores them ``+0.0000`` and would never
surface the notebook in the first place — but it does mean this rule's honesty is exactly the
configured provider's honesty. Under ``EMBEDDING_PROVIDER=local_bge``, where retrieval CAN
surface a paraphrase, this rule would refuse the paraphrase it found; the fix then is a
relevance measure built on the same semantics, not a looser threshold on this one. Stated here
so that a provider swap is known to be a change to this module too.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from proxyshop_support.embedding import fold_for_lexical, lexical_tokens

__all__ = [
    "MIN_SHARED_SHARE",
    "MIN_SHARED_TERMS",
    "NAMING_MODIFIERS",
    "OFF_TOPIC_DETAIL",
    "PHRASE_BREAK_WORDS",
    "STOPWORDS",
    "RelevanceVerdict",
    "TopicalRelevance",
    "candidate_surface",
    "content_terms",
    "head_term",
    "identity_off_topic",
    "identity_surface",
    "query_noun_phrases",
    "shopper_named_the_product",
    "variant_surface",
]

#: How many of the query's content words the platform's observed identity must carry for the
#: product to be about the query, when the query has that many to give.
#:
#: **Two, and the second one is the whole guard.** Measured over 30 off-corpus queries against
#: the recorded corpus alone: accepting ONE shared content word serves 7 of them where this rule
#: serves 0, and buys exactly one extra honest query for it (see the ablation in this module's
#: header, and the note there about the contaminated column this number used to be read off).
#: The single words it lets
#: through are a coffee-bean caffeine powder for a walnut coffee table, a "Nutricost Frother" for
#: an espresso machine, "Winter Wellness Trio" for winter tyres. One word in common is what an
#: unrelated query and a catalogue of 3,093 products share by accident; two is not.
MIN_SHARED_TERMS = 2

#: The other way to be about a query: this share of its content words, for a query too short to
#: reach :data:`MIN_SHARED_TERMS` any other way, and equally for one long enough that a count
#: would be trivially met. One half — so ``"zinc lozenges"`` is answered by ``Zinc Oxide`` on
#: one word out of two, which is half the question and is how a shopper reads it.
MIN_SHARED_SHARE = 0.5

#: English function words, plus the shopping-frame verbs and placeholders a conversational
#: query carries ("looking", "need", "something", "find"). They are dropped from BOTH sides
#: before anything is compared.
#:
#: Why a list rather than a corpus statistic: this module sees one auction's candidates and not
#: the catalogue, so it cannot compute what is common. The cost of the list is that it is
#: English and hand-written; the cost of NOT having it is that ``"a walnut coffee table for the
#: lounge"`` and ``"Nutricost Protein for Women"`` agree on ``for``, and two rows agreeing on a
#: preposition is the noise this module exists to refuse. Measured on the same 30 off-corpus
#: queries against the recorded corpus alone: with the list, 0 keep rows; without it, 3 do, for
#: one extra honest query.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but of for with without to from in into on at by as is are was were be been
    being am do does did doing done can could would should shall will may might must have has had
    this that these those there here it its it's his her him she he they them their our ours your
    yours my mine me us we you i
    about above across after against along among around before behind below beneath beside between
    beyond during except inside near off out outside over past through under until up upon within
    all any both each either every few less many more most much neither no none nor not only other
    others own same several some such too very
    please just really quite rather pretty new good great nice best better cheap cheapest top
    something anything everything nothing someone anyone stuff thing things item items
    product products option options kind sort type
    need needs needed want wants wanted looking look find finding get getting getting buy buying
    purchase order ordering shop shopping recommend recommendation suggest help helps helping
    like love prefer trying try use using
    s t re ve ll d m im ive dont doesnt cant wont
    """.split()
)

#: The function words that END the noun phrase a shopping query opens with, so that
#: :func:`head_term` can find the word saying WHAT KIND OF THING was asked for.
#:
#: Every one of them is also in :data:`STOPWORDS` — asserted by
#: ``test_every_phrase_break_is_a_word_the_rule_already_drops`` — so this set can only decide
#: WHERE the leading phrase ends and can never remove a word the comparison would have used.
#: ``which`` is the one obvious member that is deliberately absent for that reason: it is not
#: a stopword, so breaking on it would drop a content word from the head walk that
#: :func:`content_terms` keeps.
#:
#: It is English and hand-written, on the same terms and for the same reason as
#: :data:`STOPWORDS`: this module sees one auction's candidates and not a grammar. What it
#: buys is measured on :func:`head_term`.
PHRASE_BREAK_WORDS: frozenset[str] = frozenset(
    """
    for with without of to from in into on at by under over near about around that and or but
    """.split()
)

#: The detail written on a refusal, before the words are appended. Read by tests and by the
#: buyer-facing exclusion label (``WhyEmpty.tsx`` prints it verbatim), so it is published once
#: here rather than composed at each call site.
#:
#: **It says who VOUCHES for the row, not who chose it**, and the difference is a claim this
#: sentence used to make and could not check. It read "so the platform — not the shop — chose
#: it", which is true when the platform built the roster and false when the caller supplied
#: one: ``POST /auctions`` accepts a ``roster`` in the request body — that is exactly how
#: buyer-svc drives it — and ``collect_bids`` mints the fallback offer from the caller's row,
#: so for a stated roster the platform chose the OFFER and wrote the PITCH but did not
#: necessarily pick the PRODUCT. What is true of every row this fires on, whatever the roster
#: source, is that no shop bid for it: ``fallback`` is ``collect_bids``' own verdict, so there
#: is no seller's voice behind the row and the platform's crawl is the only thing that could
#: vouch for it. That is what the sentence asserts now.
OFF_TOPIC_DETAIL = (
    "the platform's own record of this product is not about what was asked. This is an "
    "organic result — no shop bid for it, so the platform's own crawl is the only thing "
    "vouching for it, and that crawl does not connect it to this query"
)


#: The SIBILANT singular endings, which take ``-es`` in English: ``boxes``, ``dishes``,
#: ``churches``, ``buzzes``, ``glasses``. That covers the class this module trips over — a
#: singular ending in ``-e``, whose plural is a bare ``-s`` and which the old blanket ``-es``
#: strip split from itself (``capsule``/``capsul``).
#:
#: **It is not the whole of the English ``-es`` rule, and the gap is the ``-o`` nouns.** A
#: singular ending in a consonant + ``-o`` also takes ``-es``, and this set does not list
#: ``o``, so :func:`_stem` sends those to the bare ``-s`` rule and splits them. Measured::
#:
#:     potatoes -> potatoe   potato -> potato      tomatoes -> tomatoe   tomato -> tomato
#:     heroes   -> heroe     hero   -> hero        echoes   -> echoe     echo   -> echo
#:     mangoes  -> mangoe    mango  -> mango       volcanoes -> volcanoe volcano -> volcano
#:
#: The old blanket rule folded that class correctly, so this is a real behaviour change and not
#: only a docstring one. **``o`` is deliberately absent anyway**, because adding it trades a
#: rarer class for a commoner one: ``shoes``/``shoe`` and ``canoes``/``canoe`` are ``-e``
#: singulars whose stems also end in ``o``, so no suffix rule can separate ``sho`` from
#: ``potato``, and ``o`` in this tuple would answer ``shoes -> sho`` against ``shoe -> shoe``.
#: Measured over the tokens of every product name on disk: the shipped 3,093-product corpus
#: (``fixtures/real-catalogs``) contains no ``-oes`` token at all, and the 17,409-product broad
#: corpus (``fixtures/real-catalogs-broad``) contains ``shoes`` x7 and ``heroes`` x1 — so the
#: word the omission costs occurs once, and the word it protects occurs seven times. Both
#: numbers come from tokenising the ``title`` of every row in ``stores/*.products.jsonl.gz``.
ES_PLURAL_STEM_ENDINGS: tuple[str, ...] = ("s", "x", "z", "ch", "sh")


def _stem(token: str) -> str:
    """Fold an English plural onto its singular, and nothing more ambitious than that.

    ``joints`` -> ``joint``, ``capsules`` -> ``capsule``, ``gummies`` -> ``gummy``. The length
    guard keeps ``was``/``its``/``gas`` intact, and no other suffix is touched: an aggressive
    stemmer collapses distinct product words (``vitamins``/``vital``) and a wrong merge here is
    a wrong shortlist, not a wrong ranking. The retrieval measure this rule sits beside handles
    the rest of the morphology through its trigram half; this only has to stop a plural query
    from missing a singular catalogue.

    **The ``-es`` rule is CONDITIONAL, and that condition is the whole of a measured repair.**
    It used to fire on any word ending ``es``, stripping two characters from every noun whose
    singular ends in ``-e`` — so the singular and its own plural landed on different tokens and
    this function did the one thing its last sentence promises it will not::

        _stem('capsule')  -> 'capsule'      _stem('capsules')  -> 'capsul'
        _stem('peptide')  -> 'peptide'      _stem('peptides')  -> 'peptid'
        _stem('lozenge')  -> 'lozenge'      _stem('lozenges')  -> 'lozeng'

    That is not a docstring defect, it is a refusal of honest traffic, and it was reachable
    through the served route: ``TopicalRelevance().judge('collagen peptide powder tub',
    'Collagen Peptides')`` answered ``about=False`` on one shared word out of four, while the
    same question with the plural spelled the same on both sides is served. Measured on the
    recorded corpus through ``POST /auctions``, before this fix: ``'bovine collagen peptides'``
    -> 4 slots / 6 shops, ``'bovine collagen peptide'`` -> 2 slots / 2 shops. The condition
    added is the SIBILANT one (:data:`ES_PLURAL_STEM_ENDINGS`): after a sibilant English adds
    ``-es``, and for the ``-e`` singulars this repair is about the plural is a bare ``-s``,
    which the third rule already handles correctly. It is not the whole of the English ``-es``
    rule — the consonant + ``-o`` plurals also take ``-es`` and are knowingly left out; that
    trade, and what it costs on the corpora on disk, is on
    :data:`ES_PLURAL_STEM_ENDINGS` itself.

    The three guards, and why each length is what it is:

    * ``-ies`` and the bare ``-s`` keep the original four-character floor, which is what keeps
      ``was``/``its``/``gas``/``this`` intact.
    * the sibilant ``-es`` gets a THREE-character floor instead, because the sibilant itself is
      the evidence: nothing in English ends ``-xes`` or ``-ches`` by accident, so ``boxes`` ->
      ``box`` is safe where a blanket two-character strip is not. At four it would have missed
      ``boxes`` and then handed it to the ``-s`` rule, which answers ``boxe``.
    * the bare ``-s`` refuses a word ending ``-ss``, because a doubled s is never a plural
      marker: ``glass``, ``mass``, ``grass`` keep their s and meet ``glasses`` -> ``glass``
      through the rule above.

    What it still does not do, stated rather than left to be discovered: irregular plurals are
    untouched (``feet``, ``mice``), and a stem this leaves is often not a word. Measured::

        _stem('gummies') -> 'gummy'    _stem('gummy')  -> 'gummy'     (agree)
        _stem('studies') -> 'study'    _stem('study')  -> 'study'     (agree)
        _stem('series')  -> 'serie'                                   (not a word)

    ``series`` reaches the bare ``-s`` rule rather than either ``-es`` rule: the ``-ies`` branch
    needs a four-character stem and ``ser`` is three, and ``seri`` is not a sibilant. A stem
    that is not a word costs nothing as long as both spellings of the SAME word reach it, which
    is what the ``-es`` repair above is about. The one class where that still fails is the
    consonant + ``-o`` plural, and it is documented on :data:`ES_PLURAL_STEM_ENDINGS`.
    """
    if token.endswith("ies") and len(token) - 3 >= 4:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) - 2 >= 3 and token[:-2].endswith(ES_PLURAL_STEM_ENDINGS):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) - 1 >= 4:
        return token[:-1]
    return token


def content_terms(text: str) -> tuple[str, ...]:
    """``text``'s meaning-bearing words, folded and stemmed, first-occurrence order preserved.

    Folded by :func:`proxyshop_support.embedding.lexical_tokens` — the SAME folding the
    configured embedding applies — so this rule and the similarity it sits beside are looking at
    one surface rather than at two that disagree about hyphens, case and accents.

    Dropped: :data:`STOPWORDS`, and bare numbers. A number on its own (``5000``, ``1000``) is a
    strength or a count that any product in a catalogue might carry, so agreeing on one is not
    agreeing about the product; a number ATTACHED to a word (``d3``, ``b12``, ``omega3``)
    survives, because that is a name.

    **The stopword test runs on both spellings of a token — as typed and as stemmed** — and
    the second half is a repair. It used to run on the typed word only, so the PLURAL of a
    stopword survived as a content word: ``top`` is in :data:`STOPWORDS` and ``tops`` is not,
    and ``tops`` stems to ``top``, so ``"tank tops"`` contributed a ``top`` term that ``"tank
    top"`` did not. Same for ``types`` against ``type``. Harmless on the recorded corpus —
    checked, only ``same`` and ``love`` occur as whole product words — but it made this rule's
    answer depend on the shopper's plural, which is exactly what :data:`STOPWORDS`' own
    "dropped from BOTH sides before anything is compared" says it does not. The typed spelling
    is still tested FIRST and on its own, because a stopword can stem to a non-stopword
    (``these`` -> ``thes``) and dropping only the stemmed form would have let it through.

    Args:
        text: any text — a shopper's query, or a product's observed identity.

    Returns:
        The content terms, possibly empty. Empty means "nothing here to compare", which is the
        undecidable case rather than the no-match case.
    """
    terms: list[str] = []
    for token in lexical_tokens(text):
        if token in STOPWORDS or token.isdigit():
            continue
        stemmed = _stem(token)
        if not stemmed or stemmed in STOPWORDS or stemmed in terms:
            continue
        terms.append(stemmed)
    return tuple(terms)


def head_term(query_text: str) -> str:
    """WHAT KIND OF THING the shopper asked for: the last content word before the query's
    first function word, stemmed. ``""`` when the query carries no content word at all.

    English noun phrases are head-final, so the last word of the phrase a shopping query opens
    with is the thing itself and everything before it modifies it. ``a large green ceramic
    plant pot`` -> ``pot``; ``a small white desk lamp`` -> ``lamp``; ``a walnut side table``
    -> ``table``. :data:`PHRASE_BREAK_WORDS` is what stops the trailing prepositional phrase
    stealing it: ``a walnut coffee table for the lounge`` -> ``table``, not ``lounge``, and
    ``something to help my joints`` -> ``joint`` because a break before any content word is
    skipped rather than taken.

    **What it is FOR, which is the whole of the variant arm's weighting.** A variant list is
    colours, sizes, materials and counts — modifiers, not products — so a variant name may
    only describe a thing the platform's own record already says this IS. That is the test
    :meth:`TopicalRelevance.judge` applies: the identity must carry the head term before any
    variant word is counted. Measured over ``fixtures/real-catalogs-broad`` (17,409 products,
    97,217 variant names) with 22 queries the corpus has nothing for, comparing the rows the
    rule admits with the variant arm gated on ONE identity word of any kind against this gate::

        gate                                   furniture rows   apparel rows   off-corpus rows
                                               for 12 furniture queries        for 22 queries
        -------------------------------------- ---------------  ------------  ----------------
        identity surface alone (no variant arm)      73                0              19
        any one identity word                       171               22             204
        the head term (this)                        142                0              28

    The 22 apparel rows and most of the 185 extra off-corpus ones are one shape: the query's
    COLOUR matched an apparel title and the query's SIZE matched a garment size in the variant
    names — ``a large green ceramic plant pot`` answered by ``Women's Afternoon Hoodie in Sage
    Green`` on ``green`` plus ``large``, ``a small white desk lamp`` by ``Signature Crewneck -
    Pure White`` on ``white`` plus ``small``. Neither product's own record says it is a pot or
    a lamp, and this gate is that sentence made executable. The 69 furniture rows it keeps are
    the arm's whole purpose: for ``a walnut side table`` it adds 14 rows, among them ``Nested
    Coffee Tables``, ``Personal Table``, ``Bistro Table``, ``The Lift Off Coffee Table`` and
    ``The Modular Table`` — each a table whose walnut is only in its variants.

    It is a HEURISTIC and its cost is stated rather than left to be found. The head of ``a
    queen bed frame in birch`` is ``frame``, so ``The Floyd Bed - Original`` — a birch bed in
    queen, and a genuinely good answer — is refused where the ungated arm kept it; same for
    ``a modular sectional sofa`` against ``The Essential Corner Sectional``, whose own name
    says ``sectional`` and whose variants say ``sofa``. Measured, those two families are 23 of
    the 29 furniture rows this gate costs. A rule with no grammar cannot tell a compound
    noun's two heads apart, and the alternative measured against it — accepting either of the
    last TWO content words — takes the off-corpus rows from 28 back to 57 for 25 more
    furniture rows, which is the wrong side of the same trade this module exists to make.
    """
    head = ""
    # `fold_for_lexical(...).split()` rather than `lexical_tokens`, which is the same folding
    # with a `dict.fromkeys` de-duplication on top. That de-duplication is right for a bag of
    # words and wrong here: it drops the SECOND `for` of "im looking for a ... supplement for
    # liver support", so the walk runs past the break that ends the leading phrase and answers
    # `support` where the phrase head is `supplement`. Position is the whole of this function.
    for token in fold_for_lexical(query_text).split():
        if token in PHRASE_BREAK_WORDS:
            if head:
                return head
            continue
        if token in STOPWORDS or token.isdigit():
            continue
        stemmed = _stem(token)
        if not stemmed or stemmed in STOPWORDS:
            continue
        head = stemmed
    return head


def _joined(parts: Iterable[Any]) -> str:
    return " ".join(str(part) for part in parts if part is not None and str(part).strip())


def candidate_surface(candidate: Any) -> str:
    """Everything the PLATFORM observed about one retrieved candidate, as one text.

    The same components ``ingest.graph.reembed.embedding_text`` composes the product's vector
    from — canonical name, brand, categories, ingredients, attribute keys and their string
    readings — so the words this rule decides on are the words the retrieval that surfaced this
    candidate was scoring. Numbers and units are left out: they reach
    :func:`content_terms` as bare digits and are dropped there anyway.

    **Nothing a seller asserted is read.** A :class:`ingest.graph.Candidate` carries only graph
    facts, and the graph drops any fact whose provenance does not resolve.

    Variant names are deliberately NOT here, and :func:`variant_surface` is where they are
    instead: this is the product's own identity, the surface both count arms decide on, and
    pooling a store's forty listing options into it is the false positive :func:`head_term`
    exists to refuse.
    """
    parts: list[Any] = [
        getattr(candidate, "canonical_name", None),
        getattr(candidate, "brand", None),
    ]
    parts.extend(getattr(candidate, "categories", None) or ())
    parts.extend(getattr(candidate, "ingredients", None) or ())
    for attribute in getattr(candidate, "attributes", None) or ():
        if isinstance(attribute, Mapping):
            parts.append(attribute.get("key"))
            parts.append(attribute.get("value_string"))
    return _joined(parts)


def variant_surface(candidate: Any) -> str:
    """The names of the purchasable VARIANTS the platform observed for one candidate.

    ``Variant{name}`` is a real node in DESIGN's graph and the crawl fills it: a Shopify
    variant title such as ``One Panel - 18" w x 67" l x 15" h / Walnut / Black``. It is
    where a shop states the finish, the colour, the size and the count, and for a whole
    class of product it is the ONLY place those words appear — a coffee table's title says
    "coffee table" and its variants say "walnut".

    It is a SEPARATE function from :func:`candidate_surface`, and separate is the point.
    Variant names are the weaker half of the platform's record of a product and
    :meth:`TopicalRelevance.judge` weighs them as such: a product with forty variants
    contributes forty listings' worth of colour, size and material words, so a rule that
    pooled them with the title would let a corpus of colour words decide relevance. What
    the two surfaces are worth is decided in :meth:`TopicalRelevance.judge`, once, rather
    than by which strings happened to be concatenated here.

    ``candidate`` carries them on ``variant_names``, which
    :class:`~exchange.retrieval.sources.GraphCandidateSource` reads out of the graph under
    the same platform-observed provenance gate ``catalogue_entry`` uses, and which
    :func:`~exchange.retrieval.sources.make_candidate` fills from a record so the offline
    double drives this arm too. Read through :func:`getattr`, so a candidate from any other
    implementation of the :class:`~exchange.retrieval.sources.CandidateSource` protocol —
    which is typed on the plain ``Candidate`` and carries no such field — answers ``""`` and
    nothing changes for it.

    **Nothing a seller asserted is read**, on the same terms as :func:`candidate_surface`:
    the names here are the crawl's reading of the shop's listing options, gated on
    ``Source.source_class`` before they are returned.
    """
    return _joined(getattr(candidate, "variant_names", None) or ())


def identity_surface(identity: Any) -> str:
    """The platform's crawled name for a product, as one text.

    ``identity`` is what :func:`~exchange.ranking.verification.catalog_identity` builds:
    ``title`` and, when the snapshot carried one, ``brand``. ``source`` and ``observed_at`` are
    provenance rather than description and are deliberately not compared — a shopper asking for
    a snapshot id is not a case, and ``snap-gaiaherbs.com`` would otherwise make every product
    of that store agree with a query naming the store.
    """
    if not isinstance(identity, Mapping):
        return ""
    return _joined([identity.get("title"), identity.get("brand")])


_MISSING = object()


def _read(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a mapping or an object, shape-agnostically.

    The same two shapes :func:`identity_surface` already accepts, and the same reason
    :func:`exchange.ranking.filters.read` exists one layer up: the callers of this package hand
    it plain dicts today and pydantic records tomorrow, and neither spelling changes what the
    rules mean. It is spelled here rather than imported from ``ranking.filters`` because this
    module is the LOWER layer — see :func:`identity_off_topic`.
    """
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    value = getattr(obj, name, _MISSING)
    return default if value is _MISSING else value


def _share_phrase(share: float) -> str:
    """:data:`MIN_SHARED_SHARE` as a refusal can say it, whatever it was configured to.

    The shipped 0.5 reads ``half``, which is the word this refusal has always used and the
    word a shopper reads. It used to be a LITERAL in that sentence, so a rule built with a
    different ``min_shared_share`` told the reader it needed half when it needed all — a
    sentence that is true of the default and false of the object that printed it. Unreachable
    through the served route, which builds the default; reachable through the constructor,
    which is public and validates that argument precisely because it is meant to be used.
    """
    if share == 0.5:
        return "half"
    return f"{share:.0%} of them"


def _variant_note(matched: tuple[str, ...], corroborated: tuple[str, ...], head: str) -> str:
    """What a refusal says about the variant words it found, if it found any.

    THREE sentences, one per way the variant arm can decline, because a refusal that named the
    wrong reason is the same defect as a refusal with no reason at all. All three are reachable
    at the shipped thresholds, and each is measured::

        judge('a walnut nightstand', 'Nested Coffee Tables', variant_text='Walnut / Medium')
            -> matched=() corroborated=('walnut',)          "cannot carry a match on its own"
        judge('a small white desk lamp', 'Signature Crewneck - Pure White',
              variant_text='Small')
            -> matched=('white',) corroborated=('small',)   "does not carry lamp"
        judge('a walnut coffee table for the living room', 'The Modular Table',
              variant_text='Small / Walnut')
            -> matched=('table',) corroborated=('walnut',)  "even counted with those it is short"

    The middle one is the sentence this arm's gate needed and did not have: the variant words
    were NOT counted there, because the platform's own record does not say the product is the
    thing that was asked for, and telling that reader they were counted and fell short would
    describe a rule that had not been applied. The third says the opposite, truthfully — the
    head term WAS carried, the words WERE counted, and two of five is still short of half.
    """
    if not corroborated:
        return ""
    words = ", ".join(corroborated)
    if not matched:
        return f". Its observed variant names carry {words}, which cannot carry a match on its own"
    if head not in matched:
        return (
            f". Its observed variant names carry {words}, which are not counted: the "
            f"platform's own record of this product does not carry {head}, the thing the "
            f"query asks for"
        )
    return f". Its observed variant names carry {words}, and even counted with those it is short"


@dataclass(frozen=True)
class RelevanceVerdict:
    """Whether one product is about one query, and what that was decided on.

    Attributes:
        about: whether the product may be shown as an organic answer to this query. ``True``
            whenever the question could not be decided — see this module's header for the three
            undecidable cases and why each keeps the row.
        decidable: whether both sides carried content words to compare. ``False`` means
            :attr:`about` is a default rather than a finding, and the two are separate fields
            precisely so a caller cannot read "kept" as "checked and passed".
        asked: the query's content terms, in order.
        matched: the asked terms the product's own identity carries — its crawled name,
            brand, categories, ingredients and attribute keys.
        corroborated: the asked terms that appear ONLY in the product's observed variant
            names and nowhere in :attr:`matched`. Separate from :attr:`matched` because the
            two are not worth the same and :meth:`TopicalRelevance.judge` does not treat them
            as if they were: a term here never carries a decision on its own. Empty whenever
            no variant text was supplied, which is every caller that judges an
            :func:`identity_surface`.
        detail: a sentence naming what was decided and on which words. Non-empty always: a
            refusal a shopper cannot be given a reason for is the confidently-wrong answer this
            module replaces, wearing an empty shortlist instead of a full one.
    """

    about: bool
    decidable: bool
    asked: tuple[str, ...]
    matched: tuple[str, ...]
    detail: str
    corroborated: tuple[str, ...] = ()

    @property
    def share(self) -> float:
        """The share of the query's content terms this product's own identity carries.

        In ``[0, 1]``, and deliberately NOT widened by :attr:`corroborated`: this is the
        number the count/share arms are decided on, and a share that silently counted variant
        words would report a stronger agreement than the rule actually found.
        """
        if not self.asked:
            return 0.0
        return len(self.matched) / len(self.asked)


class TopicalRelevance:
    """Decides whether a product is about a query, on whole-word agreement alone.

    Stateless, deterministic and cheap — a set intersection over two short token lists, per
    candidate. It is deliberately NOT a second scorer: it answers yes or no and never returns a
    number the ranking could sort by, because a relevance score would be a sixth term in a
    published five-term formula (D50) wearing a filter's name.
    """

    #: Recorded on the audit trail beside a verdict, so a reader can tell which rule produced
    #: it. Versioned because the thresholds are the rule.
    #:
    #: ``/2`` adds the variant arm (:func:`head_term`). ``/1``'s two
    #: thresholds are unchanged and every ``/1`` acceptance is still an acceptance, so the
    #: version moved for what it now ALSO accepts: a product whose own identity carries some
    #: of the query and whose observed variant names carry the rest.
    name = "content-word-agreement/2"

    def __init__(
        self,
        *,
        min_shared_terms: int = MIN_SHARED_TERMS,
        min_shared_share: float = MIN_SHARED_SHARE,
    ) -> None:
        """
        Args:
            min_shared_terms: how many content words must agree. Must be at least 1 — a rule
                satisfied by zero shared words is not a rule, and would be a filter that is
                wired and switched off rather than one that is absent.
            min_shared_share: the share that satisfies the rule instead. Must be in ``(0, 1]``.
        """
        if int(min_shared_terms) < 1:
            raise ValueError(f"min_shared_terms must be at least 1, got {min_shared_terms!r}")
        if not 0.0 < float(min_shared_share) <= 1.0:
            raise ValueError(f"min_shared_share must be in (0, 1], got {min_shared_share!r}")
        self.min_shared_terms = int(min_shared_terms)
        self.min_shared_share = float(min_shared_share)

    def judge(
        self, query_text: str, observed_text: str, *, variant_text: str = ""
    ) -> RelevanceVerdict:
        """Is the product the platform observed as ``observed_text`` about ``query_text``?

        Args:
            query_text: the shopper's own words.
            observed_text: what the PLATFORM observed as this product's own identity — a
                :func:`candidate_surface` or an :func:`identity_surface`. Never a seller's
                message, pitch or claim.
            variant_text: the platform's observed names for this product's purchasable
                variants (:func:`variant_surface`), or ``""`` when the caller holds none.
                See :func:`head_term` for what it can and cannot do.

        Returns:
            The verdict. See :class:`RelevanceVerdict`; an undecidable question answers
            ``about=True, decidable=False`` rather than refusing.
        """
        asked = content_terms(query_text)
        if not asked:
            return RelevanceVerdict(
                about=True,
                decidable=False,
                asked=(),
                matched=(),
                detail=(
                    "this query carries no content words to compare, so relevance is "
                    "undecidable here and nothing is refused on it"
                ),
            )
        observed = set(content_terms(observed_text))
        if not observed:
            return RelevanceVerdict(
                about=True,
                decidable=False,
                asked=asked,
                matched=(),
                detail=(
                    "the platform holds no readable record of this product's identity, so "
                    "relevance is undecidable here and nothing is refused on it"
                ),
            )
        matched = tuple(term for term in asked if term in observed)
        needed = min(self.min_shared_terms, len(asked))
        share = len(matched) / len(asked)
        if len(matched) >= needed or share >= self.min_shared_share:
            return RelevanceVerdict(
                about=True,
                decidable=True,
                asked=asked,
                matched=matched,
                detail=(
                    f"the platform's own record of this product carries "
                    f"{', '.join(matched)} out of {', '.join(asked)}"
                ),
            )
        # The variant arm, reached only after the identity arms have failed. TWO conditions,
        # and neither is a count of words:
        #
        # 1. the platform's own record must carry `head_term` — the thing the shopper asked
        #    FOR, as opposed to a colour or a size that modifies it. A variant name describes
        #    a product; it may not decide what the product IS.
        # 2. the widened agreement must meet the SHARE arm. The count arm's licence to admit
        #    two words out of nine belongs to the product's own crawled identity and is not
        #    inherited by a listing option: with the count arm available here, `head_term`
        #    alone lets `Elderberry Extract Capsules` answer `im looking for a high strength
        #    milk thistle supplement for liver support that is vegan` on `supplement` from a
        #    category plus `vegan` from a variant name — two words of nine, neither of them
        #    `milk` or `thistle`.
        #
        # `_variant_note` says which of the two refused, so a shopper is never told the words
        # were counted when the arm never opened.
        corroborating = set(content_terms(variant_text)) - observed
        corroborated = tuple(term for term in asked if term in corroborating)
        if corroborated and head_term(query_text) in matched:
            widened = len(matched) + len(corroborated)
            if widened / len(asked) >= self.min_shared_share:
                return RelevanceVerdict(
                    about=True,
                    decidable=True,
                    asked=asked,
                    matched=matched,
                    corroborated=corroborated,
                    detail=(
                        f"the platform's own record of this product carries "
                        f"{', '.join(matched)} out of {', '.join(asked)}, and the variants it "
                        f"observed this product listed under carry "
                        f"{', '.join(corroborated)}"
                    ),
                )
        return RelevanceVerdict(
            about=False,
            decidable=True,
            asked=asked,
            matched=matched,
            corroborated=corroborated,
            detail=(
                f"{OFF_TOPIC_DETAIL}: it carries "
                f"{', '.join(matched) if matched else 'none'} of {', '.join(asked)}, and an "
                f"organic result needs {needed} of them or {_share_phrase(self.min_shared_share)}"
                + _variant_note(matched, corroborated, head_term(query_text))
            ),
        )


# =====================================================================================
# THE ORGANIC GATE'S KEEP-RULE — one definition, consulted by two layers
# =====================================================================================
# `exchange.ranking.filters.organic_relevance_reason` refuses an organic shortlist row whose
# crawled identity is not about the query, and `exchange.retrieval.roster._solicited` has to
# know that verdict BEFORE it stakes a shop's one slot on a product. Everything below used to
# live in `ranking/filters.py`, which `retrieval` may not import — `ranking` imports
# `retrieval`, never the other way — so it lives here, in the module that already owns
# `content_terms`, `identity_surface`, `MIN_SHARED_TERMS` and the rule itself, and
# `ranking.filters` imports and re-exports it. Both layers therefore ask ONE function; a copy
# of the rule in the roster would be a second rule that drifts.

#: The words a shopper puts BESIDE a product's name without asking for a different product.
#: Two classes and nothing else: the FORM the thing comes in (``capsules``, ``powder``,
#: ``lozenges``) and the GRADE it is wanted at (``organic``, ``pure``). None of them names a
#: thing on its own, which is the whole test for membership.
#:
#: It is consulted by :func:`shopper_named_the_product` alone, and only for a title that folds
#: to ONE content word — see there for why that is the only case where any of this runs. Its
#: job is to let ``"reishi extract for immune support"`` still name ``Reishi`` while
#: ``"cast iron skillet for camping"`` does not name ``Iron+``.
#:
#: **The two directions this list fails in are not symmetric, and that is why it is short.** A
#: word MISSING from it costs an honest keep — the row falls back to the thresholds, which is
#: exactly where it was before this condition existed. A word wrongly ON it buys a confident
#: wrong answer. So a word earns its place by a measured honest query that needs it, never by
#: sounding plausible. Measured over 8 off-corpus queries shaped
#: ``"<ordinary word> <modifier> ..."`` and about something else entirely — the last three
#: rows of ``test_organic_relevance.CONTAINMENT_LEAKS`` are the ones that survived into the
#: suite: ``blend``, ``complex`` and ``liquid`` each bought a leak and no honest query needed
#: one of them, so none of the three is here. ``extract`` is the one entry that costs
#: something: it keeps ``"reishi extract for immune support"`` and it admits ``"garlic extract
#: for the garden pests"``, and that trade is taken deliberately because the admitted row IS a
#: garlic extract — the query names the product and means it for the roses.
NAMING_MODIFIERS: frozenset[str] = frozenset(
    content_terms(
        "capsule capsules tablet tablets softgel softgels gummy gummies powder drop drops "
        "pill pills lozenge lozenges chew chews tincture resin extract supplement supplements "
        "organic natural pure vegan"
    )
)


def query_noun_phrases(query_text: str) -> tuple[tuple[str, ...], ...]:
    """The query's content-word runs, in order, split wherever a stopword or a number falls.

    ``"cast iron skillet for camping"`` -> ``(('cast', 'iron', 'skillet'), ('camping',))``.
    ``"bacopa best one for daily use under $30"`` -> ``(('bacopa',), ('one',), ('daily',))``,
    because :data:`STOPWORDS` already carries ``best``, ``use``
    and ``under``.

    A crude approximation of an English noun phrase, and crude ON PURPOSE: the only question
    asked of it is which words the shopper grouped together, and a preposition, a determiner,
    a conjunction or a bare number is where a shopper stops describing one thing and starts
    describing the next. Nothing here parses; the stopword list does all the work.

    **Folding is :func:`~proxyshop_support.embedding.fold_for_lexical` and NOT
    :func:`~proxyshop_support.embedding.lexical_tokens`**, which is the same folding
    :func:`content_terms` applies, minus the de-duplication.
    ``lexical_tokens`` drops a repeated word, and a repeated word here is usually a repeated
    STOPWORD — ``"iron for the pan for camping"`` would lose its second ``for`` and read as one
    phrase where the shopper typed two. Order and repeats are the structure this function is
    about, so it reads the folded string directly and lets ``content_terms`` decide each token.

    Args:
        query_text: the shopper's own words.

    Returns:
        The phrases, possibly empty. Each term is stemmed exactly as ``content_terms`` stems
        it, so a phrase's words and a title's words are comparable without a second fold.
    """
    phrases: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in fold_for_lexical(query_text).split():
        terms = content_terms(token)
        if terms:
            current.extend(terms)
        elif current:
            phrases.append(tuple(current))
            current = []
    if current:
        phrases.append(tuple(current))
    return tuple(phrases)


def shopper_named_the_product(
    query_text: str, identity: Any, *, min_named_terms: int = MIN_SHARED_TERMS
) -> bool:
    """Did the shopper ASK FOR the thing the platform calls this product?

    Two conditions, and the second one is the whole of this function's history. The crawled
    ``title`` must be CONTAINED in the query — every one of its content words present — and
    the containment must be evidence rather than coincidence.

    **The refusal it ends, measured on the shipped catalogue.** Through
    ``catalog_identity`` -> :func:`identity_surface` over the
    3,086 products of ``deploy/demo/exchange-deployment.json`` at ``fa900c4``, with the query
    set to each product's OWN crawled title plus four ordinary shopper words
    (``"<title> best one for daily use under $30"``), the thresholds refused **104 of 3,086**
    as not about what was asked. ``Bacopa``, ``Resveratrol``, ``Glutathione 98%``,
    ``Garlic 1%``, ``SleepThru®`` — a shopper typed the product's name verbatim and the
    platform's own gate answered that its own crawl could not connect the product to the
    query. This condition takes that to **1 of 3,086** (``SAMe``, whose title folds to a
    stopword). Re-measured after the second condition below was added: still **1 of 3,086**.

    **Why the thresholds cannot get there on their own.**
    :meth:`TopicalRelevance.judge` needs ``min(2, len(asked))``
    agreeing content words or half of them, counted against the QUERY's length. A
    one-content-word title cannot supply two, so as soon as the shopper types three or more
    content words the row is refused by arithmetic no matter what it names. The surface does
    carry the brand — ``Bacopa Gaia Herbs`` is three terms — but the shopper is under no
    obligation to type the brand, and every one of those 104 has a one-word title.
    Lowering ``MIN_SHARED_TERMS`` to reach them is the trade that module's own ablation
    measured and rejected: one shared content word served 7 of 30 off-corpus queries.

    THIS CONDITION IS ABOUT ONE-WORD TITLES AND NOTHING ELSE
    -------------------------------------------------------
    ``min_named_terms`` is the rule's own ``min_shared_terms``, and a title carrying that many
    content words returns ``True`` on containment alone because THE GATE'S ANSWER CANNOT
    DEPEND ON IT: if every word of a two-word title is in the query, then both are in the
    surface too, so ``judge`` already counts two agreements and keeps the row without ever
    reaching here. The proof holds for any configured threshold. Re-measured over all 3,086
    identities, **0** of the 2,982 carrying two or more content words had their gate answer
    decided by this condition, and
    ``test_the_condition_cannot_change_the_gate_for_a_multi_word_title`` pins that shape on
    the pair that would break first rather than re-taking the sweep. So everything below runs
    only where the title folds to ONE word — 103 of the 3,086 — and that is the only place a
    leak was ever possible.

    **The leak this condition used to be, measured on the served route.** Containment on a
    one-word title means "the query contains this one ordinary word", which is the
    one-shared-word rule the ablation rejected, wearing a keep-condition's name. Two silent
    tier-1 stores rostered on ``Iron+`` and ``SAMe Bulk``, driven through ``POST /auctions``::

        "cast iron skillet for camping"     -> the shopper was shown Iron+ at $29.95
        "an iron bed frame queen size"      -> Iron+
        "bulk storage bins for the garage"  -> SAMe Bulk

    103 of the 3,086 products have a one-content-word title and roughly fifteen of those words
    are ordinary English — iron, bulk, fuel, recovery, ease, longevity, fiber, ginger, nutmeg,
    calcium, zinc, selenium, garlic, omega, multivitamin. Over 25 realistic off-corpus queries
    carrying one of those words, plain containment answered **25 of 25** with a wrong row.

    **What separates the two, and what does not.** The obvious candidate is document frequency
    over the corpus, and on this catalogue it is measurably the WRONG WAY ROUND. Counted over
    all 3,086 crawled titles: ``ease`` appears in 1, ``recovery`` in 2, ``nutmeg`` in 2,
    ``fuel`` in 3, ``longevity`` in 3 — every one of them rare in the catalogue and ordinary in
    English — while ``resveratrol`` appears in 23, ``glutathione`` in 11 and ``bacopa`` in 7.
    A rarity threshold that admits ``Bacopa`` admits ``Ease`` and ``Recovery`` first. The share
    of the query the title accounts for is the other candidate and it does not separate them
    either: ``"cast iron skillet for camping"`` gives the title 1 word of 4 and
    ``"bacopa for memory and focus daily"`` gives it 1 of 4 as well.

    What DOES separate them is not a property of the word at all — it is where the shopper put
    it. **In an English noun phrase a premodifier is not the head**: ``"iron skillet"`` is a
    skillet, ``"garlic bread"`` is bread, ``"bulk storage bins"`` are bins. So the second
    condition is that the query's OPENING noun phrase (:func:`query_noun_phrases`) is the name
    — the title's word, plus nothing but :data:`NAMING_MODIFIERS`. A shopper asking for the
    thing leads with it and then says what it is for; a shopper asking for something made of
    it puts it in front of the thing they actually want.

    **Measured, on the shipped catalogue and on two query sets written before the rule was
    chosen**: 25 off-corpus queries carrying one of those ordinary words, and 25 queries naming
    a real one-word product the way a person types. The rows of each that survived into the
    suite are ``test_organic_relevance.CONTAINMENT_LEAKS`` and ``NAMED_IN_THE_LEAD``.

        rule                          104 refusals   25 honest namings   25 off-corpus
        ----------------------------  ------------   -----------------   -------------
        containment alone (shipped)    103 fixed          25 kept           25 filled
        containment + this condition   103 fixed          25 kept            1 filled
        no keep-condition at all         0 fixed           1 kept            0 filled

    The one off-corpus query still filled is ``"iron on patches for jeans"``: ``on`` is a
    preposition to every tokeniser in this tree, so ``iron`` reads as a complete opening
    phrase and the query reads as ``"iron, for patches, for jeans"``. It is the English
    compound ``iron-on`` split by a rule that has no compounds, and the honest shapes it would
    cost to close it — ``"multivitamin for men over 50"`` has exactly the same structure — are
    worth more than the one row.

    **It reads the TITLE, never the surface.** The brand is in the surface so that a shopper
    naming a brand finds its products; it is not part of the product's name, so requiring the
    shopper to type it would put this condition out of reach of exactly the rows it is for. And
    it is the PLATFORM's crawl on one side and the SHOPPER's own words on the other — no shop
    writes either, so no shop can assert its way through this (D55).

    An identity with no readable title answers ``False``: nothing was named, so nothing was
    named in full, and the row falls through to the thresholds as it did before.

    Args:
        query_text: the shopper's own words.
        identity: the PLATFORM's crawled identity for this product, or ``None``.
        min_named_terms: how many content words a title must carry before containment alone
            settles it. Defaults to the shipped :data:`MIN_SHARED_TERMS`;
            :func:`identity_off_topic` passes the rule's own ``min_shared_terms`` so the
            inertness argument above holds against whatever the caller configured.
    """
    if identity is None:
        return False
    named = content_terms(str(_read(identity, "title", "") or ""))
    if not named:
        return False
    if not set(named) <= set(content_terms(query_text)):
        return False
    if len(named) >= min_named_terms:
        return True
    phrases = query_noun_phrases(query_text)
    if not phrases:
        return False
    opening = phrases[0]
    word = named[0]
    if word not in opening:
        return False
    return all(term in NAMING_MODIFIERS for term in opening if term != word)


def identity_off_topic(
    query_text: str, identity: Any, relevance: TopicalRelevance
) -> RelevanceVerdict | None:
    """The verdict when this crawled identity is NOT about the query, or ``None`` when it is.

    The organic gate's keep-rule, and the whole of it apart from the one condition that is
    about a BID rather than about a product (``candidate.fallback``, which stays at the
    ranking layer where the candidate is). Three of
    :func:`~exchange.ranking.filters.organic_relevance_reason`'s four conditions are here, in
    its order, and each is a KEEP:

    1. an identity that folds to no readable surface is unchecked, and unchecked is kept;
    2. a shopper who asked for the product by the platform's own name has overruled any
       word-count threshold (:func:`shopper_named_the_product`);
    3. and the rule's own verdict decides the rest.

    Two callers, and they need opposite halves of the answer, which is why this returns the
    verdict rather than a bool. ``organic_relevance_reason`` needs
    :attr:`RelevanceVerdict.detail` to tell the shopper WHY a row was refused;
    :func:`~exchange.retrieval.roster._solicited` needs only whether there is a refusal at all,
    so that a shop holding a product this answers ``None`` for is rostered on THAT product
    instead of on a higher-scoring one the gate would then throw away.

    **It is the same call in both places, deliberately.** The roster asking a cheaper
    approximation of this question — the title without the brand, say, or the thresholds
    without the naming condition — would be a second rule, and the two would disagree on
    exactly the rows this exists to get right.

    Args:
        query_text: the shopper's own words.
        identity: the PLATFORM's crawled identity for this product — ``title`` and ``brand``,
            as :func:`~exchange.ranking.verification.catalog_identity` builds it and as
            :func:`identity_surface` reads it. Never a seller's message, pitch or claims.
        relevance: the rule to decide with.

    Returns:
        The refusing :class:`RelevanceVerdict`, or ``None`` when nothing here refuses.
    """
    surface = identity_surface(identity)
    if not surface:
        return None
    if shopper_named_the_product(query_text, identity, min_named_terms=relevance.min_shared_terms):
        return None
    verdict = relevance.judge(query_text, surface)
    return None if verdict.about else verdict
