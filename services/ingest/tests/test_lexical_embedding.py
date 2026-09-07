"""``LexicalEmbedding`` — the provider the D19 amendment made the default (D6, D19, D55).

``test_graph.py`` covers provider *selection* (the registry, the env var, the refusal to fall
back on a typo) and ``test_embedding_ranking_gate.py`` covers *ranking*. This file covers the
seam between them: that the provider is the shared function and not a second copy of it, that
its vectors satisfy everything the write path demands of a vector, and that the swap did not
quietly change the shape of anything the index or the re-embed pass depends on.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from ingest.embeddings import (
    EMBEDDING_DIM,
    HashEmbedding,
    LexicalEmbedding,
    get_embedding_provider,
)
from ingest.graph import VECTOR_INDEX_DIMENSIONS, embedding_vector_defect
from ingest.graph.reembed import embedding_text

from proxyshop_support.embedding import cosine, lexical_embed

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two probes the frozen acceptance test uses, and the same ones ``test_graph.py`` names.
#: Same length on purpose, so "different vectors" is about content and not about length.
PROBE_A = "gentle vitamin c serum for sensitive skin"
PROBE_B = "heavy fragranced night cream for dry face"


def test_the_provider_is_the_shared_support_implementation() -> None:
    """One definition of "the lexical embedding", exactly as ``HashEmbedding`` has one.

    A second, independently-derived feature scheme here would be a silent integration bug of
    the worst kind: seeded vectors and query vectors in two spaces, retrieval scoring noise,
    and both halves green in their own suites. This is the guard, and it is why
    ``LexicalEmbedding.embed`` delegates rather than re-implements.
    """
    provider = LexicalEmbedding()
    for text in (PROBE_A, PROBE_B, "", "single", "Trail Running Shoe", "!!!"):
        assert provider.embed(text) == lexical_embed(text, dim=EMBEDDING_DIM)


def test_the_provider_is_1024_dimensional_and_l2_normalised() -> None:
    """D19's actual promise, which the amendment kept: the default exercises D6's cosine
    index for real rather than feeding it unit-less noise.

    ``"machine daily"`` and ``"sunscreen daily"`` are not decoration. The two feature bags are
    each unit-normalised and scaled by ``sqrt(0.6)`` / ``sqrt(0.4)``, so their sum is *already*
    exactly unit length for any text whose bags do not share a bucket — which is most texts.
    Deleting ``lexical_embed``'s final normalisation was measured to fail nothing without a
    cross-colliding probe like these two (unnormalised norms 1.0887 and 0.9027).
    """
    provider = LexicalEmbedding()
    assert provider.name == "lexical"
    assert provider.dimension == EMBEDDING_DIM == VECTOR_INDEX_DIMENSIONS == 1024
    for text in (
        PROBE_A,
        PROBE_B,
        "a",
        "Ω≈ç√∫˜µ",
        "x" * 5000,
        "!!! ---",
        "machine daily",
        "sunscreen daily",
    ):
        vector = provider.embed(text)
        assert len(vector) == 1024
        norm = math.sqrt(sum(component * component for component in vector))
        assert norm == pytest.approx(1.0, abs=1e-9), f"{text[:20]!r} has L2 norm {norm}"


def test_every_vector_the_provider_emits_is_writable_into_the_index() -> None:
    """The write path's own validator, run over the awkward inputs (T-012 / W1-30).

    ``embedding_vector_defect`` is what ``set_product_embedding`` consults, and it rejects the
    wrong width, a non-finite component and a **zero L2 norm**. The last one is the trap for a
    lexical provider specifically: text with no alphanumeric characters produces no words and
    no trigrams, and the obvious implementation answers the zero vector — which would make
    such a product unwritable rather than merely unrankable.
    """
    provider = LexicalEmbedding()
    for text in (PROBE_A, "!!!", "—", "🙂", "12", "a", "x" * 5000):
        assert embedding_vector_defect(provider.embed(text)) is None, repr(text)


def test_components_are_builtin_floats_not_numpy_scalars() -> None:
    """``type(x) is float``, stricter than the frozen ``isinstance`` on purpose: it fails on
    ``numpy.float32`` AND on ``numpy.float64``, which subclasses ``float`` and would pass."""
    for component in LexicalEmbedding().embed(PROBE_A):
        assert type(component) is float


def test_embed_batch_is_the_element_wise_embed() -> None:
    """The inherited batching convenience must not become a second, drifting code path."""
    provider = LexicalEmbedding()
    assert provider.embed_batch([PROBE_A, PROBE_B]) == [
        provider.embed(PROBE_A),
        provider.embed(PROBE_B),
    ]
    assert provider.embed_batch([]) == []


def test_the_provider_is_deterministic_across_instances_and_calls() -> None:
    first = LexicalEmbedding().embed(PROBE_A)
    assert LexicalEmbedding().embed(PROBE_A) == first
    provider = LexicalEmbedding()
    assert provider.embed(PROBE_A) == provider.embed(PROBE_A) == first


def test_the_default_provider_embeds_identically_in_a_fresh_interpreter() -> None:
    """Determinism where it actually bites: through ``get_embedding_provider()``, in a NEW
    process, under a different ``PYTHONHASHSEED``.

    Two calls in one process cannot see the failure this guards. The ingest worker writes
    vectors in one process and the exchange embeds queries in another; if the provider's
    feature accumulation depended on hash randomisation the two would disagree in the last
    bits — which the Neo4j index's quantization turns into an occasional, unexplained
    re-ranking rather than an error.

    ``EMBEDDING_PROVIDER`` is REMOVED from the child's environment rather than inherited, so
    this measures D56's default and not whatever the caller happens to have exported —
    otherwise an operator legitimately running ``EMBEDDING_PROVIDER=hash`` would see this go
    red for no defect.
    """
    script = textwrap.dedent(
        """
        from ingest.embeddings import get_embedding_provider
        provider = get_embedding_provider()
        print(provider.name)
        print(repr(provider.embed("Semi Automatic Espresso Machine")))
        """
    )
    child_env = {k: v for k, v in os.environ.items() if k != "EMBEDDING_PROVIDER"}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
        env=child_env,
    )
    name, vector = result.stdout.strip().split("\n", 1)
    assert name == "lexical", f"a fresh interpreter selected {name!r} as the default"
    assert vector == repr(
        get_embedding_provider("lexical").embed("Semi Automatic Espresso Machine")
    )


def test_the_provider_distinguishes_two_texts_of_the_same_length() -> None:
    """Content, not length — and the two frozen probes are the same length on purpose."""
    assert len(PROBE_A) == len(PROBE_B)
    a, b = LexicalEmbedding().embed(PROBE_A), LexicalEmbedding().embed(PROBE_B)
    assert a != b
    assert len(set(a)) > 1 and len(set(b)) > 1, "a constant vector is a fill, not an embedding"


def test_the_lexical_and_hash_providers_are_different_vector_spaces() -> None:
    """Which is exactly why ``reembed_products`` stamps the index with the provider name.

    Both emit 1024 unit-norm floats, so no width guard can tell them apart; a catalogue
    embedded with one and queried with the other would return a fully-populated, silently
    re-ranked shortlist. ``EmbeddingProviderMismatch`` exists for this, and this assertion is
    the reason it has to.
    """
    text = "Merino Wool Winter Scarf"
    lexical, hashed = LexicalEmbedding().embed(text), HashEmbedding().embed(text)
    assert lexical != hashed
    assert abs(cosine(lexical, hashed)) < 0.2, "the two providers must not share a space"


def test_the_composed_product_text_ranks_the_way_a_bare_title_does() -> None:
    """The vector in the index is built from ``embedding_text``, not from the title alone.

    ``reembed_products`` composes canonical name + brand + categories + attributes +
    ingredients, so a provider that ranked bare titles well and composed text badly would pass
    every arithmetic gate in this repo and still rank the served catalogue wrong. This drives
    the real composer.
    """
    relevant = embedding_text(
        {
            "canonical_name": "Trail Running Shoe",
            "brand": "Ridgeline",
            "categories": ["Footwear"],
            "attributes": [],
            "ingredients": [],
        }
    )
    irrelevant = embedding_text(
        {
            "canonical_name": "Espresso Machine",
            "brand": "Bariste",
            "categories": ["Kitchen"],
            "attributes": [],
            "ingredients": [],
        }
    )
    provider = LexicalEmbedding()
    query = provider.embed("running shoes")
    hit = cosine(query, provider.embed(relevant))
    miss = cosine(query, provider.embed(irrelevant))
    assert hit > miss + 0.2, (
        f"composed text inverted or flattened the ranking: relevant {hit:+.4f} vs irrelevant "
        f"{miss:+.4f}\n  relevant text:   {relevant!r}\n  irrelevant text: {irrelevant!r}"
    )
