"""``HashEmbedding`` — a registered, selectable provider; **no longer the default** (D19).

WHAT CHANGED, and what did not. ``EMBEDDING_PROVIDER`` defaulted to ``hash`` until the
ranking gate below measured what that meant, and the default is now ``lexical``
(:mod:`~ingest.embeddings.lexical`). ``hash`` is unchanged, still registered, and still
selected by ``EMBEDDING_PROVIDER=hash``; ``hash_embed`` itself is byte-for-byte the function
it always was.

What has not changed is the reason a hand-written provider is the default at all: the
alternative (``torch`` + ``sentence-transformers``) is +679 MB of wheels and a ~2.2 GB weight
fetch on first call, which D3/C9 forbid offline and which would stall an unattended run. So
the cosine index D6 pins is exercised for real by 1024-d L2-normalised vectors — with unit
vectors, cosine similarity *is* the dot product, and the index machinery (write path, width,
rescaling, top-k) is genuinely exercised rather than stubbed. The replacement default emits
exactly the same shape into exactly the same index; it differs only in what its cosine means.

WHERE THIS PROVIDER IS STILL THE RIGHT ONE. Byte identity is a real property and some tests
want it: a test asserting that two distinct texts land near-orthogonal, or that a vector is
content-addressed and nothing else, is stating something true about ``hash`` and merely
approximate about any lexical measure. It also remains the vector space the root conftest's
``hash_embedding`` fixture seeds, so a test may keep using both together.

WHAT THE VECTORS DO NOT DO, stated here because this docstring used to claim the opposite
("a ranking measured against this provider is a ranking, not a fill") and that claim is
false for anything but an exact string match. ``hash_embed`` is SHA-256 bytes reshaped into
floats, so its cosine measures **byte identity, not meaning**. Measured against the query
``"running shoes"``: the exact string scores ``+1.000``; ``"espresso machine"`` scores
``+0.063``; ``"trail running shoe"`` scores ``-0.008``; and the query's own singular,
``"running shoe"``, scores ``-0.035``. An espresso machine outranks a trail running shoe,
and one character of difference is indistinguishable from a different product category.

So: an ORDER produced under this provider is a ranking of nothing. Exercising the index is
not the same claim as ranking the catalog, and only the first one is true here. The
consequence is load-bearing for any test written against this provider — an assertion of the
form "the relevant product comes back first" holds only when the query text is byte-identical
to the document, and any other such assertion passes or fails on the seed. That is measured
and pinned, not asserted in prose: see
``services/ingest/tests/test_embedding_ranking_gate.py``, which drives a
(query, relevant, irrelevant) corpus through this provider — 28 of 45 pairs inverted, all
five per-query spreads negative, and the served Neo4j vector index answering "espresso
machine" with a merino wool scarf. That measurement is now pinned unconditionally in
``test_the_hash_provider_ranks_by_bytes_and_the_numbers_say_so``; it used to hold the gate
open as a ``strict`` xfail, and the amendment that made ``lexical`` the default is what
retired the marker.

Why the arithmetic is delegated rather than re-implemented
----------------------------------------------------------
``proxyshop_support.embedding.hash_embed`` is the orchestrator-owned definition of "the hash
embedding" — unchanged byte for byte by the amendment — and the root ``conftest.py`` hands it
to every other ticket as the ``hash_embedding`` fixture. A test that seeds
``Product.embedding`` through that fixture and then retrieves must retrieve through *this*
provider, not the configured one, or the two vectors live in different spaces. A second,
independently-derived SHA-256 scheme here would be the same bug one layer down: every seeded
vector orthogonal-ish to every query vector, retrieval scoring noise, both halves passing
their own tests. One definition, therefore, with
``test_graph.py::test_hash_embedding_agrees_with_the_shared_support_implementation``
standing guard over the equality.
"""

from __future__ import annotations

from proxyshop_support.embedding import hash_embed

from .base import EMBEDDING_DIM, EmbeddingProvider


class HashEmbedding(EmbeddingProvider):
    """Deterministic, offline, L2-normalised 1024-d embeddings (D6 / D19).

    Constructs with no arguments and loads nothing. Identical text always embeds
    identically — across instances, processes and machines — because the vector is derived
    from ``SHA-256`` over the text with no PRNG seeding anywhere in the path.
    """

    name = "hash"
    dimension = EMBEDDING_DIM

    def embed(self, text: str) -> list[float]:
        """Embed ``text`` as a :attr:`dimension`-long, L2-normalised vector.

        Args:
            text: the text to embed.

        Returns:
            ``dimension`` floats whose L2 norm is 1.0 (the zero vector for empty text).

        Raises:
            NotImplementedError: never; the signature matches the interface exactly, which
                the acceptance test compares byte for byte.
        """
        return hash_embed(text, dim=self.dimension)
