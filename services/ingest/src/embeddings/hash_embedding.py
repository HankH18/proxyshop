"""``HashEmbedding`` — the configured default provider (D19).

D19: this is **not** a test-only fallback. ``EMBEDDING_PROVIDER`` defaults to ``hash`` in
every environment because the alternative (``torch`` + ``sentence-transformers``) is +679 MB
of wheels and a ~2.2 GB weight fetch on first call, which D3 forbids offline and which would
stall an unattended run. So the cosine index D6 pins is exercised for real by these vectors,
which is why they are L2-normalised: with unit vectors, cosine similarity *is* the dot
product, and a ranking measured against this provider is a ranking, not a fill.

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
