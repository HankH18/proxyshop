"""``LocalBgeEmbedding`` — the optional real model provider (D19, DESIGN §Cross-cutting).

Selected by ``EMBEDDING_PROVIDER=local_bge``. DESIGN pins "default local bge-m3-class
(1024d)", which is why this provider exists at all and why it shares D6's 1024/cosine shape
with :class:`~ingest.embeddings.hash_embedding.HashEmbedding`: swapping providers must not
require rebuilding the index for a *different* width.

Three rules this module obeys, all of them consequences of D3 (offline) and D19:

1. **Nothing is imported at module import time.** ``sentence_transformers`` pulls in
   ``torch``; importing it here would take down every ``pytest services/ingest`` run on a
   machine that deliberately does not have it — which is every machine, by default.
2. **Constructing the provider loads nothing.** ``get_embedding_provider("local_bge")``
   returns an instance offline, with no weights present, so "the provider swap is
   config-only" is demonstrable without a 2.2 GB download.
3. **The failure is legible and typed.** The first call to :meth:`embed` raises
   :class:`~ingest.embeddings.base.EmbeddingProviderUnavailable` naming the missing extra,
   and the marker-gated test *skips* on :meth:`probe` rather than failing.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from .base import EMBEDDING_DIM, EmbeddingProvider, EmbeddingProviderUnavailable

#: DESIGN's "local bge-m3-class (1024d)". Override with ``EMBEDDING_MODEL``.
DEFAULT_MODEL = "BAAI/bge-m3"

#: The optional extra that has to be installed for this provider to run. Declared in the
#: root manifest as ``[project.optional-dependencies] embeddings`` and NEVER installed by
#: default (D19).
REQUIRED_EXTRA = "embeddings"


class LocalBgeEmbedding(EmbeddingProvider):
    """A ``sentence-transformers`` bge-m3-class provider, loaded lazily on first embed."""

    name = "local_bge"
    dimension = EMBEDDING_DIM

    def __init__(self, model_name: str | None = None) -> None:
        """Record which model to load. Loads nothing; see the module docstring.

        Args:
            model_name: the model id. Defaults to ``EMBEDDING_MODEL``, then
                :data:`DEFAULT_MODEL`.
        """
        self.model_name = model_name or os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL
        self._model: Any | None = None

    @staticmethod
    def probe() -> str | None:
        """Why this provider cannot run here, or ``None`` if it can.

        Cheap and side-effect free: it does not import ``torch`` and does not touch the
        network. Marker-gated tests call this and ``pytest.skip`` on a non-``None`` answer,
        which is what makes "skips cleanly when weights are absent" true rather than hoped
        for.

        Returns:
            A human-readable reason string, or ``None`` when the extra is importable.
        """
        import importlib.util

        for module in ("torch", "sentence_transformers"):
            if importlib.util.find_spec(module) is None:
                return (
                    f"{module} is not installed; LocalBgeEmbedding needs the optional "
                    f"'{REQUIRED_EXTRA}' extra, which D19 deliberately leaves uninstalled "
                    f"(+679 MB of wheels and a ~2.2 GB weight fetch)"
                )
        return None

    def _load(self) -> Any:
        """Import ``sentence_transformers`` and materialise the model, once.

        Returns:
            The loaded ``SentenceTransformer``.

        Raises:
            EmbeddingProviderUnavailable: the extra is missing, or the weights are not on
                disk and D3 forbids fetching them.
        """
        if self._model is not None:
            return self._model
        reason = self.probe()
        if reason is not None:
            raise EmbeddingProviderUnavailable(reason)
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:  # pragma: no cover - probe() already covers this
            raise EmbeddingProviderUnavailable(f"sentence_transformers unusable: {exc}") from exc
        try:
            self._model = SentenceTransformer(self.model_name)
        except Exception as exc:
            raise EmbeddingProviderUnavailable(
                f"could not load {self.model_name!r} offline: {exc}. D3 forbids a weight "
                f"download during verification; pre-populate the HuggingFace cache first."
            ) from exc
        return self._model

    def embed(self, text: str) -> list[float]:
        """Embed ``text`` as a :attr:`dimension`-long, L2-normalised vector.

        Args:
            text: the text to embed.

        Returns:
            ``dimension`` floats whose L2 norm is 1.0 (the zero vector for empty text).

        Raises:
            EmbeddingProviderUnavailable: the optional extra or the weights are absent.
        """
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed several texts in one forward pass.

        Args:
            texts: the texts to embed, in order.

        Returns:
            One 1024-d L2-normalised vector per input, in the same order.

        Raises:
            EmbeddingProviderUnavailable: the optional extra or the weights are absent.
        """
        if not texts:
            return []
        model = self._load()
        encoded = model.encode(list(texts), normalize_embeddings=True)
        vectors = [[float(component) for component in row] for row in encoded]
        for vector in vectors:
            if len(vector) != self.dimension:
                raise EmbeddingProviderUnavailable(
                    f"{self.model_name!r} produced {len(vector)} dimensions; the "
                    f"product_embedding index is {self.dimension}-d (D6)"
                )
        return vectors
