"""The ``EmbeddingProvider`` port (T-012, C2/C4, D6, D19).

D19 makes the provider a *configured* choice, not a test seam: ``EMBEDDING_PROVIDER``
selects an implementation and nothing else in the codebase changes. D6 fixes the shape the
Neo4j index is built for — **1024 dimensions, cosine similarity** — so every provider in
this package returns 1024 L2-normalised floats and cosine similarity is the plain dot
product.

Why this is a plain base class and not ``abc.ABC`` / ``typing.Protocol``
-----------------------------------------------------------------------
The frozen acceptance test enumerates the interface with
``inspect.getmembers(EmbeddingProvider, callable)`` filtered on ``not name.startswith("_")``
and then requires every implementation to carry a byte-identical ``inspect.signature`` for
each of those names. ``dir()`` on a class includes its **metaclass** attributes, so an
``abc.ABC`` base silently publishes ``ABCMeta.register`` as a public callable member of the
interface, and ``typing.Protocol`` publishes more still. Neither is part of this port. A
plain class keeps the published surface exactly the two methods declared below.

Every module in this package carries ``from __future__ import annotations``, which is also
load-bearing: under PEP 563 an annotation is the *string* it was written as, so a subclass
in a module **without** the future import would produce ``inspect.signature`` objects
holding real classes while this module held strings, and the two would compare unequal even
though the source text matched. Keep the import in every provider module.
"""

from __future__ import annotations

from collections.abc import Sequence

#: D6: the Neo4j vector index is created with 1024 dimensions and cosine similarity.
EMBEDDING_DIM = 1024


class EmbeddingProvider:
    """Turn text into a fixed-width, L2-normalised vector.

    Attributes:
        name: the ``EMBEDDING_PROVIDER`` value that selects this implementation.
        dimension: the vector width. D6 fixes it at :data:`EMBEDDING_DIM` for every
            provider that writes into the ``product_embedding`` index.
    """

    #: Deliberately non-callable class attributes: the interface's *callable* surface is
    #: enumerated by the acceptance test, and only ``embed``/``embed_batch`` belong to it.
    name = "embedding-provider"
    dimension = EMBEDDING_DIM

    def embed(self, text: str) -> list[float]:
        """Embed ``text`` as a :attr:`dimension`-long, L2-normalised vector.

        Args:
            text: the text to embed.

        Returns:
            ``dimension`` floats whose L2 norm is 1.0 (the zero vector for empty text).

        Raises:
            NotImplementedError: this base class declares the port; it does not implement it.
        """
        raise NotImplementedError("EmbeddingProvider.embed is an interface method")

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed several texts.

        Concrete on purpose so that implementations inherit it unchanged and therefore
        cannot diverge from the interface signature. A provider with real batching (the
        local model one) overrides it.

        Args:
            texts: the texts to embed, in order.

        Returns:
            One vector per input, in the same order.
        """
        return [self.embed(text) for text in texts]


class EmbeddingProviderUnavailable(RuntimeError):
    """A configured provider cannot run here — missing library, or missing weights (D3)."""


class UnknownEmbeddingProvider(ValueError):
    """``EMBEDDING_PROVIDER`` named something that is not registered."""
