"""``HashEmbedding`` — the configured default provider (D19).

D19: this is **not** a test-only fallback. ``EMBEDDING_PROVIDER`` defaults to ``hash`` in
every environment because the alternative (``torch`` + ``sentence-transformers``) is +679 MB
of wheels and a ~2.2 GB weight fetch on first call, which D3 forbids offline and which would
stall an unattended run. So the cosine index D6 pins is exercised for real by these vectors,
which is why they are L2-normalised: with unit vectors, cosine similarity *is* the dot
product, and the index machinery — the write path, the 1024-d width, the rescaling, the
top-k — is genuinely exercised rather than stubbed.

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
machine" with a merino wool scarf — and which states the ranking gate as a ``strict`` xfail
for exactly as long as ``EMBEDDING_PROVIDER`` is ``hash``.

Why the arithmetic is delegated rather than re-implemented
----------------------------------------------------------
``proxyshop_support.embedding.hash_embed`` is the orchestrator-owned, frozen definition of
"the hash embedding", and the root ``conftest.py`` hands it to every other ticket as the
``hash_embedding`` fixture. Six downstream tickets (T-020…T-024, T-031) seed
``Product.embedding`` through that fixture and then retrieve through *this* provider. A
second, independently-derived SHA-256 scheme here would be a silent integration bug: every
seeded vector would be orthogonal-ish to every query vector and retrieval would score noise
while both halves passed their own tests. One definition, therefore, with
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
