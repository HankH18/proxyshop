"""``LexicalEmbedding`` — the configured default provider (D19 as amended; D6).

WHY THIS EXISTS, and why it is not a reversal of D19. D19's promise about the default
provider was **index coverage**: ``EMBEDDING_PROVIDER`` defaults to something that emits
L2-normalised 1024-d float vectors "so the real cosine index (D6) is exercised", because the
alternative (``torch`` + ``sentence-transformers``) is +679 MB of wheels and a ~2.2 GB weight
fetch on first call, which C9 forbids offline. That promise is kept here unchanged — this
provider is dependency-free, offline, deterministic, and emits the same 1024 unit-norm floats
into the same index.

What D19 never promised, and what six downstream tickets read out of it anyway, is **ranking
quality**. ``hash_embed`` is SHA-256 bytes reshaped into floats, so its cosine measures byte
identity: against the query ``"running shoes"`` it scores ``"espresso machine"`` at ``+0.063``
and ``"trail running shoe"`` at ``-0.008``, and over the 45-pair corpus in
``services/ingest/tests/test_embedding_ranking_gate.py`` it inverts **28 of 45** pairs with
all five per-query spreads negative. An order produced under it is a ranking of nothing. Since
D55 made graph retrieval the spine of the product, that stopped being tolerable.

This provider's cosine tracks ``ingest.er.similarity.text_similarity`` instead: **0 of 45
inversions, worst per-query spread +0.4936** against a threshold of 0.20. ``hash`` stays
registered and selectable — its byte-identity behaviour is genuinely useful, and deleting it
would strand the gate that measures it.

WHAT A LEXICAL MEASURE CANNOT DO, stated here because the previous default's docstring
overclaimed and that is exactly what made its output untrustworthy. **There are no semantics
in this file.** It compares surfaces — words and character trigrams — and nothing else:

* ``"laptop"`` vs ``"notebook computer"`` scores **+0.0000**, indistinguishable from an
  unrelated product. A shopper asking for a laptop retrieves nothing here unless the
  catalogue itself says "laptop".
* ``"running shoes"`` vs ``"sneakers"`` scores **+0.0676** — barely above the ``+0.0000`` it
  gives ``"espresso machine"``, and that margin is shared letters, not shared meaning.
* ``"running shoes"`` vs ``"trail running shoe"`` scores **+0.5090**, which is the case it
  *does* handle: a plural, a hyphen, a word order change, a spelling variant.

So: this ranks the catalogue by surface agreement with the query, and a miss here is evidence
of different wording, never of a different product. Synonymy and paraphrase need a real model
(``EMBEDDING_PROVIDER=local_bge``, which D19 keeps as an optional extra and which is untouched
by this amendment) or a query-expansion step in front of retrieval.

Why the arithmetic is delegated rather than re-implemented
----------------------------------------------------------
``proxyshop_support.embedding.lexical_embed`` is the shared definition, for the same reason
``hash_embed`` is: a second, independently-derived scheme here would put seeded vectors and
query vectors in two different spaces, and retrieval would score noise while both halves
passed their own tests. One definition, with
``test_lexical_embedding.py::test_the_provider_is_the_shared_support_implementation``
standing guard over the equality.
"""

from __future__ import annotations

from proxyshop_support.embedding import lexical_embed

from .base import EMBEDDING_DIM, EmbeddingProvider


class LexicalEmbedding(EmbeddingProvider):
    """Deterministic, offline, dependency-free, L2-normalised 1024-d embeddings (D6 / D19).

    Constructs with no arguments and loads nothing — no wheels, no model weights, no network,
    no PRNG. Identical text always embeds identically across instances, processes and
    machines: every feature is placed by SHA-256 and every accumulation runs over an ordered
    sequence, so nothing in the path depends on ``PYTHONHASHSEED``.
    """

    name = "lexical"
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
        return lexical_embed(text, dim=self.dimension)
