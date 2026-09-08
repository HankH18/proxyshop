"""A shopper who types the product's NAME must reach the merchants who sell it.

:data:`~exchange.retrieval.clusters.MIN_ASSIGNMENT_SCORE` refuses to address an auction to a
cluster on the strength of *"a single incidental word"*, and its own worked counter-example is
one word: the row ``{"cluster_id": "cluster-coffee"}`` against *"a walnut coffee table for the
lounge"*, a furniture shopper who must not be handed to a coffee merchant's approval record.

That rule was never about a PHRASE. Two adjacent words in order cannot fall out of a sentence
about furniture -- :func:`~exchange.retrieval.clusters._term_hits` matches a phrase as a whole
word SEQUENCE -- so a two-word term is at least as much evidence as two independent one-word
hits, which the bar already admits. Weighing a term per word, capped at two, is that rule
applied to phrases rather than a loosening of it.

MEASURED on the served buyer path before this, against the demo catalogue whose
``cluster-liver-support`` row lists ``milk thistle`` among its terms::

    query                            hosted bids   reason
    "milk thistle"                        0        store_declined:cluster_not_pursued
    "milk thistle capsules"               0        store_declined:cluster_not_pursued
    "liver support supplement"            4        -

An unassigned intent keeps the content-hash cluster the buyer service minted for it, which no
merchant's approval record can name, so every solicited store declined and the shortlist
carried catalogue prices only. The phrasing that failed is the one a shopper is most likely to
use.

Both directions are graded. The one-word counter-example is repeated here verbatim, because a
change that made phrases count would be worthless if it also let one common word through.
"""

from __future__ import annotations

import pytest
from exchange.retrieval.clusters import (
    MIN_ASSIGNMENT_SCORE,
    SOURCE_ASSIGNED,
    SOURCE_UNASSIGNED,
    TERM_PHRASE_WORD_CAP,
    TERM_WEIGHT,
    ClusterRow,
    StaticIntentClusterCatalogue,
    assign_cluster,
)

#: The demo deployment's own row, copied from ``deploy/demo/exchange-deployment.json`` so this
#: grades the catalogue a demo actually runs with rather than one invented here.
LIVER = ClusterRow(
    cluster_id="cluster-liver-support",
    label="Liver support supplements",
    category="supplements",
    terms=(
        "milk thistle",
        "silymarin",
        "liver support",
        "liver",
        "detox",
        "dandelion root",
        "artichoke extract",
    ),
)

#: The bar's own counter-example: one common word, no category, nothing else.
COFFEE = ClusterRow(cluster_id="cluster-coffee", label="Coffee", terms=("coffee",))


def _assign(query: str, *rows: ClusterRow):
    """Assign with NO category and NO constraints, so terms are the only evidence in play."""
    return assign_cluster(
        {"intent_id": "i-1", "cluster_id": "cl-content-hash", "query": query},
        StaticIntentClusterCatalogue(rows),
    )


@pytest.mark.parametrize(
    "query",
    [
        "milk thistle",
        "milk thistle capsules",
        "I need a milk thistle capsule",
        "dandelion root, whatever brand",
        "artichoke extract please",
    ],
)
def test_a_multi_word_product_name_alone_addresses_the_cluster(query: str) -> None:
    assignment = _assign(query, LIVER)
    assert assignment.source == SOURCE_ASSIGNED, (
        f"{query!r} names a two-word term of this cluster and still went unassigned, so the "
        f"intent keeps a content-hash cluster no merchant envelope can pursue: {assignment}"
    )
    assert assignment.cluster_id == "cluster-liver-support"
    assert assignment.score >= MIN_ASSIGNMENT_SCORE


@pytest.mark.parametrize(
    "query",
    [
        "a walnut coffee table for the lounge",
        "coffee",
        "somewhere to put my coffee",
    ],
)
def test_one_common_word_still_cannot_address_an_auction_to_a_cluster(query: str) -> None:
    """The frozen counter-example. This is the direction the bar exists for."""
    assignment = _assign(query, COFFEE)
    assert assignment.source == SOURCE_UNASSIGNED, (
        f"{query!r} was addressed to {assignment.cluster_id!r} on the evidence "
        f"{assignment.evidence} -- one incidental word is exactly what MIN_ASSIGNMENT_SCORE "
        f"refuses, and a furniture shopper has been handed to a coffee merchant"
    )
    assert assignment.cluster_id is None


def test_a_single_word_term_is_still_worth_exactly_one_term_weight() -> None:
    """The cap is on the phrase, not on the bar: nothing about one-word evidence moved."""
    both = _assign("liver support supplement", LIVER)
    assert both.source == SOURCE_ASSIGNED
    # "liver support" (2 words, capped at 2) and "liver" (1) both hit.
    assert both.score == pytest.approx(TERM_WEIGHT * (TERM_PHRASE_WORD_CAP + 1))


def test_a_long_phrase_is_capped_rather_than_dominating() -> None:
    """A five-word term must not outweigh a category match plus a constraint."""
    wordy = ClusterRow(
        cluster_id="cluster-wordy",
        label="Wordy",
        terms=("one two three four five",),
    )
    assignment = _assign("one two three four five", wordy)
    assert assignment.score == pytest.approx(TERM_WEIGHT * TERM_PHRASE_WORD_CAP), (
        f"a {len('one two three four five'.split())}-word term scored {assignment.score}, so a "
        f"catalogue could buy any assignment it liked by writing a longer term"
    )
