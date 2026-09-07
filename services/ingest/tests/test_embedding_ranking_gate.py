"""The missing gate: does the configured embedding provider RANK, or only match bytes?

WHAT WAS MISSING, and why a green suite did not notice. Every similarity assertion in this
repo before this file asserted a *bound* or an *identity*: an exact self-match scores ~1.0,
an unrelated string scores ~0 (``test_graph.py::test_the_vector_index_scores_an_unrelated_
product_near_zero_cosine``), a structured-path candidate reports ``cosine is None``. Noise
satisfies every one of those. Not one assertion anywhere said **a relevant document must
outrank an irrelevant one**, which is the only property a retrieval ranking is actually for.

WHAT IS TRUE OF THE DEFAULT PROVIDER, measured here and reproduced by the tests below.
``EMBEDDING_PROVIDER`` defaults to ``hash`` (D19) and ``hash_embed`` is SHA-256 bytes reshaped
into 1024 floats, so its cosine is a function of *byte identity*, not of meaning. Against the
query ``"running shoes"``:

===========  ==============================
 cosine       document
===========  ==============================
 +1.0000      ``running shoes``   (exact bytes)
 +0.0632      ``espresso machine``
 -0.0082      ``trail running shoe``
 -0.0348      ``running shoe``    (the singular!)
 -0.0365      ``sneakers for jogging``
===========  ==============================

An espresso machine outranks a trail running shoe, and the singular of the query itself
scores *negative*. Over the corpus below the hash provider inverts **28 of 45**
relevant/irrelevant pairs and every one of its five per-query spreads is negative.

WHAT THIS FILE THEREFORE DOES. It builds the corpus, proves the corpus is satisfiable offline
by driving it through a real lexical measure that already exists in this repo
(``ingest.er.similarity.text_similarity``: 0 of 45 inversions, minimum spread +0.473), proves
the corpus checker itself has teeth by driving deliberately broken measures through it, and
then states the gate — ``a relevant document outranks an irrelevant one under the CONFIGURED
provider`` — as a **strict** ``xfail`` while that provider is ``hash``.

Strict, and why that is a claim rather than a dispensation. ``strict=True`` means the suite
goes RED the day the hash provider starts satisfying this corpus, so the marker cannot rot
into a silent pass; and the condition is ``provider.name == "hash"``, so the moment anyone
sets ``EMBEDDING_PROVIDER`` to anything else the marker evaporates and this is an ordinary
hard gate. The xfail asserts one measured fact — *this* provider cannot rank — and asserts it
in the direction that fails loudly if it stops being true.

``pytest --runxfail services/ingest/tests/test_embedding_ranking_gate.py`` reports the
underlying failures as ordinary failures; that is the red this file is evidence of.

WHAT IT DOES NOT DO. It does not change the default. D19 is frozen and distributed to six
tickets through the root ``conftest.py``; swapping the provider is an amendment, not a test
edit. The report accompanying this file states what such an amendment would have to say.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from ingest.embeddings import EmbeddingProvider, get_embedding_provider
from ingest.er.similarity import text_similarity
from ingest.graph import (
    Source,
    candidate_products,
    reembed_products,
    seed_products,
)

from proxyshop_support.embedding import cosine

#: A measure takes two texts and answers "how alike", bigger meaning more alike. Both the
#: embedding path (cosine of two vectors) and the lexical path fit it, which is what lets one
#: corpus checker be pointed at either.
Measure = Callable[[str, str], float]

#: The minimum acceptable gap between the mean relevant score and the mean irrelevant score,
#: per query. Chosen from measurement rather than taste: ``text_similarity``'s worst spread
#: over this corpus is **+0.473** and the hash provider's BEST is **-0.004**, so any threshold
#: in that gulf separates them. 0.20 sits well clear of both, which is the point — a
#: threshold tuned to sit just under the passing measure would go red on a harmless corpus
#: edit and teach the next reader to loosen it.
MIN_SPREAD = 0.20


@dataclass(frozen=True)
class RankingCase:
    """One shopper query with documents that should and should not be retrieved for it.

    Deliberately shopping-shaped and deliberately *lexically honest*: every relevant document
    shares real words with the query, and no irrelevant document does. That keeps the corpus
    satisfiable by a lexical measure, which is what makes the positive control below possible
    — a corpus that only a semantic model could satisfy would leave "is this gate even
    satisfiable?" unanswerable offline, and an unsatisfiable gate is indistinguishable from a
    broken one.
    """

    query: str
    relevant: tuple[str, ...]
    irrelevant: tuple[str, ...]


#: The corpus. Five queries, three relevant and three irrelevant documents each: 45
#: relevant/irrelevant pairs, every one of which is an ordering the retrieval must get right.
#:
#: Cross-referenced on purpose: the irrelevant documents for one query are the relevant
#: documents of another ("Espresso Machine" is a distractor for ``running shoes`` and a target
#: for ``espresso machine``). A corpus whose distractors were all from one unused category
#: would be satisfied by a measure that had merely learned to dislike that category.
RANKING_CORPUS: tuple[RankingCase, ...] = (
    RankingCase(
        query="running shoes",
        relevant=(
            "Trail Running Shoe",
            "Lightweight Road Running Shoes",
            "Mens Running Shoe, Neutral",
        ),
        irrelevant=("Espresso Machine", "Ceramic Burr Coffee Grinder", "Wool Winter Scarf"),
    ),
    RankingCase(
        query="fragrance free moisturizer for sensitive skin",
        relevant=(
            "Fragrance Free Daily Moisturiser",
            "Sensitive Skin Moisturizer, Unscented",
            "Gentle Fragrance-Free Face Cream",
        ),
        irrelevant=(
            "Bluetooth Mechanical Keyboard",
            "Cast Iron Skillet 12 inch",
            "Running Shoe Insoles",
        ),
    ),
    RankingCase(
        query="mineral sunscreen spf 50",
        relevant=(
            "Daily Mineral Sunscreen SPF 50",
            "Zinc Oxide Mineral Sunscreen",
            "Sunscreen Lotion SPF 50 Mineral",
        ),
        irrelevant=(
            "Stainless Steel Water Bottle",
            "Noise Cancelling Headphones",
            "Vitamin C Serum 30 ml",
        ),
    ),
    RankingCase(
        query="espresso machine",
        relevant=(
            "Semi Automatic Espresso Machine",
            "Espresso Machine with Steam Wand",
            "Compact Espresso Maker",
        ),
        irrelevant=(
            "Trail Running Shoe",
            "Fragrance Free Daily Moisturiser",
            "Wool Winter Scarf",
        ),
    ),
    RankingCase(
        query="wool winter scarf",
        relevant=("Merino Wool Winter Scarf", "Chunky Knit Wool Scarf", "Winter Scarf, Lambswool"),
        irrelevant=(
            "Semi Automatic Espresso Machine",
            "Daily Mineral Sunscreen SPF 50",
            "Bluetooth Mechanical Keyboard",
        ),
    ),
)


@dataclass(frozen=True)
class RankingReport:
    """What a measure did to the corpus.

    Attributes:
        inversions: one readable line per relevant/irrelevant pair the measure got backwards.
        pairs: how many pairs were tested, so ``len(inversions)`` has a denominator.
        spreads: per query, mean relevant score minus mean irrelevant score.
    """

    inversions: tuple[str, ...]
    pairs: int
    spreads: dict[str, float]

    @property
    def worst_spread(self) -> float:
        """The least separation any query achieved."""
        return min(self.spreads.values())

    def summary(self) -> str:
        """A failure message that names the actual comparisons, not just a count."""
        head = (
            f"{len(self.inversions)}/{self.pairs} relevant/irrelevant pairs inverted; "
            f"worst per-query spread {self.worst_spread:+.4f} (need >= {MIN_SPREAD})"
        )
        return "\n".join((head, *self.inversions[:12]))


def evaluate(measure: Measure, corpus: tuple[RankingCase, ...] = RANKING_CORPUS) -> RankingReport:
    """Score ``corpus`` with ``measure`` and report every ordering it got wrong.

    Args:
        measure: any "bigger means more alike" function of two texts.
        corpus: the cases to check. Parameterised so the checker can be pointed at a
            deliberately broken corpus in its own self-test.

    Returns:
        A :class:`RankingReport`.
    """
    inversions: list[str] = []
    pairs = 0
    spreads: dict[str, float] = {}
    for case in corpus:
        relevant = {doc: measure(case.query, doc) for doc in case.relevant}
        irrelevant = {doc: measure(case.query, doc) for doc in case.irrelevant}
        for doc, score in relevant.items():
            for other, other_score in irrelevant.items():
                pairs += 1
                if score <= other_score:
                    inversions.append(
                        f"  {case.query!r}: {other!r} ({other_score:+.4f}) ranks at or above "
                        f"{doc!r} ({score:+.4f})"
                    )
        spreads[case.query] = sum(relevant.values()) / len(relevant) - sum(
            irrelevant.values()
        ) / len(irrelevant)
    return RankingReport(tuple(inversions), pairs, spreads)


def embedding_measure(provider: EmbeddingProvider) -> Measure:
    """Cosine similarity between two texts under ``provider``.

    Every provider in this package returns L2-normalised vectors, so this is the dot product
    the Neo4j cosine index (D6) computes — the same number the served retrieval ranks on.
    """

    def measure(left: str, right: str) -> float:
        return cosine(provider.embed(left), provider.embed(right))

    return measure


#: Read once at import so the marker below and the tests agree about which provider is
#: configured, even if something mutates the environment mid-session.
CONFIGURED_PROVIDER = get_embedding_provider()

#: The whole reason the gate below is xfailed rather than simply absent. Kept as a constant
#: so the reason string cannot drift away from the condition that triggers it.
HASH_CANNOT_RANK = (
    "EMBEDDING_PROVIDER is 'hash'. hash_embed is SHA-256 bytes reshaped into 1024 floats, so "
    "its cosine measures byte identity, not meaning: over this corpus it inverts 28 of 45 "
    "relevant/irrelevant pairs and every per-query spread is negative. D19 makes that the "
    "production default and is frozen, so this cannot be fixed by a test edit — it needs an "
    "amendment. strict=True: if hash ever satisfies this corpus, THIS TEST FAILS."
)


# =======================================================================================
# 1. The corpus, and the checker, are themselves sound
# =======================================================================================


def test_the_corpus_is_well_formed() -> None:
    """A malformed corpus would make every result below meaningless.

    Specifically: a document listed as both relevant and irrelevant for one query makes that
    query unsatisfiable by any measure, and would turn a real gate into permanent noise.
    """
    assert len(RANKING_CORPUS) >= 5
    for case in RANKING_CORPUS:
        assert len(case.relevant) >= 3 and len(case.irrelevant) >= 3, case.query
        assert set(case.relevant).isdisjoint(case.irrelevant), case.query
        assert len(set(case.relevant)) == len(case.relevant), case.query
        assert len(set(case.irrelevant)) == len(case.irrelevant), case.query
    queries = [case.query for case in RANKING_CORPUS]
    assert len(set(queries)) == len(queries)

    # The distractors are other queries' targets, not a single unused category.
    targets = {doc for case in RANKING_CORPUS for doc in case.relevant}
    distractors = {doc for case in RANKING_CORPUS for doc in case.irrelevant}
    assert targets & distractors, "no distractor is another query's target; add cross-references"


@pytest.mark.parametrize(
    ("name", "measure"),
    [
        ("constant", lambda a, b: 0.5),
        ("reversed", lambda a, b: -text_similarity(a, b)),
        ("length", lambda a, b: float(len(b))),
    ],
)
def test_the_checker_actually_catches_a_measure_that_cannot_rank(
    name: str, measure: Measure
) -> None:
    """The checker has teeth: three broken measures, three reports full of inversions.

    Without this, :func:`evaluate` returning an empty inversion list would be as convincing
    as a ``return RankingReport((), 0, {})``. The same reasoning as ``test_graph.py``'s
    ``test_the_free_text_detector_actually_detects``: a tripwire nobody has tripped on
    purpose is indistinguishable from one that is not armed.
    """
    report = evaluate(measure)
    assert report.pairs == 45
    assert report.inversions, f"the {name} measure passed the corpus — the checker is inert"
    assert report.worst_spread < MIN_SPREAD


def test_the_corpus_is_satisfiable_offline_by_a_real_lexical_measure() -> None:
    """POSITIVE CONTROL, and the answer to "is this gate even achievable here?".

    ``ingest.er.similarity.text_similarity`` is this repo's own entity-resolution measure —
    pure, deterministic, offline, no weights, no network. It clears the corpus with **zero**
    inversions and a worst spread of about +0.47, so the gate below is failing because of the
    provider, not because the bar is impossible. That distinction is the whole difference
    between a defect and an unreasonable test.
    """
    report = evaluate(text_similarity)
    assert report.inversions == (), report.summary()
    assert report.worst_spread >= MIN_SPREAD, report.summary()


# =======================================================================================
# 2. The gate
# =======================================================================================


@pytest.mark.xfail(CONFIGURED_PROVIDER.name == "hash", reason=HASH_CANNOT_RANK, strict=True)
def test_the_configured_provider_ranks_a_relevant_document_above_an_irrelevant_one() -> None:
    """THE GATE. A retrieval ranking must put relevant above irrelevant. Nothing else.

    Xfailed while ``EMBEDDING_PROVIDER=hash`` (D19's frozen default) and a hard gate under
    any other provider. Run with ``--runxfail`` to see the real failure.
    """
    report = evaluate(embedding_measure(CONFIGURED_PROVIDER))
    assert report.inversions == (), report.summary()
    assert report.worst_spread >= MIN_SPREAD, report.summary()


def test_the_hash_provider_ranks_by_bytes_and_the_numbers_say_so() -> None:
    """The measurement the gate above is xfailed *on*, asserted directly so it cannot drift.

    Passing this test is not good news. It pins the specific inversions — an espresso machine
    beating a trail running shoe for "running shoes", and the query's own singular scoring
    below both — so that "the hash provider cannot rank" is a fact this suite re-measures on
    every run rather than a claim in a docstring. If ``hash_embed`` ever changes, this goes
    red and the xfail above stops being justified in the same run.
    """
    provider = get_embedding_provider("hash")
    measure = embedding_measure(provider)

    assert measure("running shoes", "running shoes") == pytest.approx(1.0), (
        "exact byte identity is the one thing the hash provider does do"
    )
    espresso = measure("running shoes", "espresso machine")
    trail = measure("running shoes", "trail running shoe")
    singular = measure("running shoes", "running shoe")
    assert espresso > trail, f"espresso {espresso:+.4f} vs trail {trail:+.4f}"
    assert espresso > singular, f"espresso {espresso:+.4f} vs singular {singular:+.4f}"
    assert singular < 0.0, f"the query's own singular scored {singular:+.4f}"

    report = evaluate(measure)
    assert len(report.inversions) == 28, report.summary()
    assert report.worst_spread < 0.0, report.summary()
    assert max(report.spreads.values()) < 0.0, (
        "every per-query spread is negative: irrelevant documents beat relevant ones on "
        f"average for all five queries — {report.spreads}"
    )


# =======================================================================================
# 3. The same gate, driven through the real Neo4j vector index
# =======================================================================================


CORPUS_SOURCE = Source(
    source_id="src-ranking-corpus",
    url="https://corpus.example/ranking",
    content_hash="sha256:2222222222222222222222222222222222222222222222222222222222222222",
    observed_at="2026-01-01T00:00:00+00:00",
    extractor_version="ranking-corpus@1",
    confidence=0.9,
    source_class="scraped",
)


def _seed_corpus(session: Any) -> dict[str, str]:
    """Seed every corpus document as a bare ``Product`` and embed it.

    Bare on purpose: with no brand, category, attributes or ingredients,
    ``reembed.embedding_text`` composes exactly the ``canonical_name``, so the vector in the
    index is the vector of the corpus document and nothing else. Any ranking failure here is
    the provider's, not the document builder's.
    """
    documents = sorted(
        {doc for case in RANKING_CORPUS for doc in (*case.relevant, *case.irrelevant)}
    )
    ids = {doc: f"corpus-{index:02d}" for index, doc in enumerate(documents)}
    seed_products(
        session,
        [{"product_id": pid, "canonical_name": doc} for doc, pid in ids.items()],
        source=CORPUS_SOURCE,
    )
    reembed_products(session, get_embedding_provider("hash"))
    return ids


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.xfail(CONFIGURED_PROVIDER.name == "hash", reason=HASH_CANNOT_RANK, strict=True)
def test_the_served_vector_index_ranks_a_relevant_product_above_an_irrelevant_one(
    graph_schema_session: Any,
) -> None:
    """The same gate on the path production actually uses: ``db.index.vector.queryNodes``.

    The arithmetic gate above could in principle be satisfied while the served retrieval
    still ranked badly — different oversampling, different rescaling, a different vector in
    the index than the one the measure computed. This drives the real index, through the real
    :func:`~ingest.graph.candidate_products`, and asks the one question that matters: for
    each corpus query, does the top-ranked product belong to that query's relevant set?
    """
    ids = _seed_corpus(graph_schema_session)
    misses: list[str] = []
    for case in RANKING_CORPUS:
        results = candidate_products(
            graph_schema_session, query_text=case.query, status=None, limit=3
        )
        assert results, f"no candidate at all for {case.query!r}"
        relevant_ids = {ids[doc] for doc in case.relevant}
        top = results[0]
        if top.product_id not in relevant_ids:
            misses.append(
                f"  {case.query!r}: rank 1 is {top.canonical_name!r} "
                f"(cosine {top.cosine:+.4f}), which is not in the relevant set"
            )
    assert not misses, "the served vector index ranked an irrelevant product first:\n" + "\n".join(
        misses
    )
