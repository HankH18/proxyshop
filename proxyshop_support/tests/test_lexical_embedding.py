"""``lexical_embed`` — the shared arithmetic behind the default embedding provider (D19).

Why this is a separate file from ``test_shared_runtime.py``: that one is orchestrator-owned
and frozen, and ``hash_embed`` has not moved. This covers the function the D19 amendment
ADDED, and it covers it at the layer the vector is actually produced, so a defect here cannot
hide behind the ingest provider that delegates to it.

The properties below are the ones every downstream consumer already assumes of an embedding
and which nothing would otherwise re-check for a new one: unit length (cosine == dot product
in D6's index), exact width, builtin floats (a ``numpy`` scalar fails the frozen acceptance
test), determinism across processes, and totality on the awkward inputs — empty text, text
that folds away to nothing, text longer than any product title.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from proxyshop_support.embedding import (
    EMBEDDING_DIM,
    LEXICAL_TOKEN_WEIGHT,
    cosine,
    fold_for_lexical,
    hash_embed,
    lexical_embed,
    lexical_tokens,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------------------
# shape: the contract D6's index is built for
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "espresso",
        "Trail Running Shoe",
        "a much longer sentence about single-origin coffee and the grinder it wants",
        "SPF 50",
        "x",
        "Ω≈ç√∫˜µ",
        "!!! --- ???",  # folds away entirely: no words, still must be a usable vector
        "x" * 5000,
        # The two bags cross-collide in these, so the sum arrives OFF unit length and only the
        # final normalisation rescues it. See the docstring — without one of these the whole
        # parametrisation passes with that normalisation deleted.
        "machine daily",  # unnormalised norm 1.0887
        "sunscreen daily",  # unnormalised norm 0.9027
        "oxide keyboard",  # unnormalised norm 0.9093
    ],
)
def test_lexical_embed_is_1024_dimensional_and_unit_length(text: str) -> None:
    """The property that makes cosine similarity equal the dot product in the Neo4j index.

    ``"!!! --- ???"`` is in this list on purpose. It is non-empty text with no alphanumeric
    content, so the word and trigram bags are both empty; a naive implementation answers the
    zero vector, and ``set_product_embedding`` REJECTS a zero-norm vector — such a product
    would become unwritable rather than merely unrankable.

    SO ARE THE LAST THREE, and they are here because deleting ``lexical_embed``'s final
    normalisation was MEASURED not to fail a single test in this repo without them. The two
    feature bags are each unit-normalised and scaled by ``sqrt(0.6)`` / ``sqrt(0.4)``, so the
    sum is exactly unit length **whenever the bags do not share a bucket** — which, being
    sparse, is most texts: every obvious probe ("espresso", "Trail Running Shoe", a composed
    product document) lands on norm 1.0 to twelve decimal places with the normalisation gone.
    Roughly one short product phrase in ten does cross-collide, and then the sum arrives at
    0.90 to 1.09. Those are the only inputs that can see the defect, so the test has to name
    some.
    """
    vector = lexical_embed(text)
    assert len(vector) == EMBEDDING_DIM == 1024
    assert math.isclose(sum(c * c for c in vector), 1.0, rel_tol=1e-9), text[:40]


def test_lexical_embed_components_are_builtin_floats() -> None:
    """``numpy.float32`` fails the frozen acceptance test's ``isinstance(x, float)``, and
    ``numpy.float64`` passes it by accident of subclassing. ``type(x) is float`` fails both."""
    for component in lexical_embed("gentle vitamin c serum for sensitive skin"):
        assert type(component) is float


def test_empty_text_embeds_to_the_zero_vector() -> None:
    """The port's declared contract, kept identical to ``hash_embed``'s."""
    assert lexical_embed("") == [0.0] * EMBEDDING_DIM


def test_a_non_positive_dimension_is_refused() -> None:
    assert len(lexical_embed("espresso", dim=64)) == 64
    with pytest.raises(ValueError):
        lexical_embed("espresso", dim=0)
    with pytest.raises(ValueError):
        lexical_embed("espresso", dim=-1)


# --------------------------------------------------------------------------------------
# determinism: the same text, the same bytes, in any process
# --------------------------------------------------------------------------------------


def test_lexical_embed_is_deterministic_within_a_process() -> None:
    assert lexical_embed("espresso machine") == lexical_embed("espresso machine")
    assert lexical_embed("espresso machine") != lexical_embed("espresso maker")


#: The probes the cross-process determinism test uses, and their density is the whole point.
#:
#: MEASURED, because the obvious short probe does not work. Replacing
#: :func:`lexical_tokens`' ``dict.fromkeys`` with a ``set`` — the exact defect this test
#: exists to catch — leaves ``"Fragrance-Free Daily Moisturiser, SPF 50"`` **byte-identical
#: across six PYTHONHASHSEEDs**, because five tokens in 1024 buckets rarely collide and the
#: reordering has nothing to reorder. It takes a feature-dense text before the seed shows: the
#: composed product document below produced four distinct vectors over six seeds under that
#: sabotage, and the eighty-token one produced six out of six. Both are kept — the first is
#: the shape ``reembed_products`` actually writes, the second makes detection near-certain.
_DETERMINISM_PROBES = (
    "Fragrance-Free Daily Moisturiser, SPF 50",
    (
        "Gentle Vitamin C Serum\nbrand: Northlight\ncategories: Serum\n"
        "attributes: fragrance_free: yes; skin_type: Sensitive; spf: 0; volume: 30 ml\n"
        "ingredients: Ascorbic Acid, Glycerin, Niacinamide"
    ),
    " ".join(f"widget{index} apparatus{index} cfg{index}" for index in range(80)),
)


def test_lexical_embed_is_byte_identical_across_two_interpreters() -> None:
    """Two SEPARATE interpreter invocations, with DIFFERENT ``PYTHONHASHSEED``s.

    Two calls in one process would prove nothing about the failure this guards. The hashing
    trick is one ``hash()`` away from being seed-dependent, and iterating a ``set`` of feature
    strings is enough on its own: within one bucket the accumulation order of differently
    weighted features decides the component's last bits, so a set-ordered implementation drifts
    between processes while every same-process assertion stays green. Vectors written by the
    ingest worker and vectors computed by an exchange query would then disagree in the last
    place, which the Neo4j index's quantization turns into an occasional silent re-ranking.

    The comparison is on the full ``repr`` of all 1024 floats of every probe, so "identical"
    means identical and not "close". See :data:`_DETERMINISM_PROBES` for why the probes are as
    long as they are — a short one does not detect the defect.
    """
    script = textwrap.dedent(
        f"""
        from proxyshop_support.embedding import lexical_embed
        for probe in {_DETERMINISM_PROBES!r}:
            print(repr(lexical_embed(probe)))
        """
    )
    outputs = []
    for seed in ("0", "1", "2", "12345", "424242", "987654"):
        # Deliberately no `env=` replacement: the child INHERITS this session's environment
        # whole (any PYTHONPATH carrying `.pkgroot` survives) and only PYTHONHASHSEED is
        # overridden, which is the variable under test. `cwd` puts the repo root on the path.
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        outputs.append((seed, result.stdout.strip()))

    distinct = {output for _, output in outputs}
    assert len(distinct) == 1, (
        "the vectors changed between interpreters started with different PYTHONHASHSEEDs "
        f"({len(distinct)} distinct results over {len(outputs)} seeds); something in the "
        f"feature path depends on hash randomisation — seeds tried: "
        f"{[seed for seed, _ in outputs]}"
    )
    in_process = "\n".join(repr(lexical_embed(probe)) for probe in _DETERMINISM_PROBES)
    assert outputs[0][1] == in_process, "a fresh interpreter disagrees with this one"


# --------------------------------------------------------------------------------------
# ranking: what the amendment bought, and what it did not
# --------------------------------------------------------------------------------------


def test_lexical_cosine_ranks_a_relevant_title_above_an_irrelevant_one() -> None:
    """The one property ``hash_embed`` does not have, at the arithmetic layer.

    The whole 45-pair corpus lives in ``services/ingest/tests/test_embedding_ranking_gate.py``;
    this is the same claim in miniature, here so that a defect in the shared function fails in
    the shared function's own suite rather than only in ingest's.
    """
    query = "running shoes"
    relevant = cosine(lexical_embed(query), lexical_embed("Trail Running Shoe"))
    irrelevant = cosine(lexical_embed(query), lexical_embed("Espresso Machine"))
    assert relevant > 0.4, f"a relevant title scored {relevant:+.4f}"
    assert irrelevant < 0.1, f"an irrelevant title scored {irrelevant:+.4f}"

    # Same pair, same direction, under `hash_embed`: inverted. Asserted rather than described,
    # so "this is why the default changed" is a fact this suite re-measures every run.
    hash_relevant = cosine(hash_embed(query), hash_embed("Trail Running Shoe"))
    hash_irrelevant = cosine(hash_embed(query), hash_embed("Espresso Machine"))
    assert hash_irrelevant > hash_relevant, (
        f"hash_embed no longer inverts this pair (espresso {hash_irrelevant:+.4f} vs trail "
        f"{hash_relevant:+.4f}); if hash_embed changed, the D19 amendment's measured "
        f"justification changed with it"
    )


def test_lexical_cosine_survives_the_plural_the_hyphen_and_the_word_order() -> None:
    """The cases a whole-token measure gets wrong and a trigram measure gets right."""
    assert cosine(lexical_embed("running shoe"), lexical_embed("running shoes")) > 0.6
    assert cosine(lexical_embed("Fragrance-Free Cream"), lexical_embed("fragrance free cream")) == (
        pytest.approx(1.0)
    )
    assert cosine(lexical_embed("trail runner shoe"), lexical_embed("shoe trail runner")) == (
        pytest.approx(1.0)
    )
    assert cosine(lexical_embed("Éclair"), lexical_embed("eclair")) == pytest.approx(1.0)


def test_lexical_embed_has_no_semantics_and_the_test_suite_says_so_out_loud() -> None:
    """THE LIMITATION, pinned so nobody mistakes this for a model.

    A lexical measure compares surfaces. Two texts that mean the same thing and share no
    letters are, to it, exactly as similar as two unrelated products — which is to say, not at
    all. If someone later swaps in a real semantic model this test SHOULD fail, and the
    failure is the notification that the caveat in every docstring can come out.
    """
    synonym = cosine(lexical_embed("laptop"), lexical_embed("notebook computer"))
    unrelated = cosine(lexical_embed("laptop"), lexical_embed("wool winter scarf"))
    assert synonym == pytest.approx(0.0, abs=1e-9), (
        f"'laptop' vs 'notebook computer' scored {synonym:+.4f}; if that is no longer ~0 the "
        f"provider has gained a property its docstrings deny having"
    )
    assert abs(unrelated) < 0.1
    assert synonym <= abs(unrelated) + 0.1, (
        "a synonym is not meaningfully closer than an unrelated product — that is the cost of "
        "having no semantics, and it must stay visible"
    )


def test_two_unrelated_titles_land_near_orthogonal() -> None:
    """The floor matters as much as the ceiling: signed hashing is what keeps it near zero.

    With unsigned (all-positive) buckets every hash collision would ADD similarity, lifting
    unrelated documents off zero and eating the spread the ranking gate measures.
    """
    for left, right in (
        ("Espresso Machine", "Wool Winter Scarf"),
        ("Bluetooth Mechanical Keyboard", "Daily Mineral Sunscreen SPF 50"),
        ("Cast Iron Skillet 12 inch", "Zinc Oxide Mineral Sunscreen"),
    ):
        score = cosine(lexical_embed(left), lexical_embed(right))
        assert abs(score) < 0.15, f"{left!r} vs {right!r} scored {score:+.4f}"


# --------------------------------------------------------------------------------------
# the pieces, so a failure above localises
# --------------------------------------------------------------------------------------


def test_folding_is_case_accent_and_punctuation_insensitive_but_keeps_digits() -> None:
    assert fold_for_lexical("Fragrance-Free, Daily!") == "fragrance free daily"
    assert fold_for_lexical("  Éclair  ") == "eclair"
    assert fold_for_lexical("SPF 50 / 12 inch") == "spf 50 12 inch"
    assert fold_for_lexical("!!! ---") == ""
    assert fold_for_lexical("") == ""


def test_tokens_are_deduplicated_and_ordered() -> None:
    """Ordered, because a ``set`` here is what would make the vector PYTHONHASHSEED-dependent."""
    assert lexical_tokens("Shoe, Trail Shoe") == ("shoe", "trail")
    assert lexical_tokens("!!!") == ()


def test_the_blend_weights_match_the_entity_resolution_measure() -> None:
    """0.6 words / 0.4 spelling, the same split ``er.similarity.text_similarity`` uses.

    Pinned because the two are supposed to track each other: the ranking gate's positive
    control is ``text_similarity``, and a silent re-weighting here would make the gate's
    "is this corpus satisfiable?" answer stop being about this provider.
    """
    assert LEXICAL_TOKEN_WEIGHT == 0.6
