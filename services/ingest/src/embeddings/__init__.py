"""Embedding providers for the catalog graph (T-012; C2/C4; D6; D19).

The public surface is the :class:`EmbeddingProvider` port, its two implementations, and
:func:`get_embedding_provider` — the *only* place in the codebase that reads
``EMBEDDING_PROVIDER``. That is what makes D19's "provider swap is config-only" a
mechanical property rather than a promise: no caller names a concrete provider class, so
changing one environment variable changes every embedding in the system.

    >>> import os
    >>> os.environ["EMBEDDING_PROVIDER"] = "hash"
    >>> provider = get_embedding_provider()
    >>> provider.name, provider.dimension
    ('hash', 1024)

D6 pins the index this feeds: 1024 dimensions, cosine similarity. Every registered provider
returns vectors of :data:`EMBEDDING_DIM` floats with unit L2 norm.
"""

from __future__ import annotations

import os

from .base import (
    EMBEDDING_DIM,
    EmbeddingProvider,
    EmbeddingProviderUnavailable,
    UnknownEmbeddingProvider,
)
from .hash_embedding import HashEmbedding
from .local_bge import LocalBgeEmbedding

#: D19: ``hash`` in every environment unless ``EMBEDDING_PROVIDER`` says otherwise.
DEFAULT_PROVIDER = "hash"

#: ``EMBEDDING_PROVIDER`` value -> implementation. Adding a provider is a registry entry
#: plus a class; it is never a change at a call site.
PROVIDERS: dict[str, type[EmbeddingProvider]] = {
    HashEmbedding.name: HashEmbedding,
    LocalBgeEmbedding.name: LocalBgeEmbedding,
}


def get_embedding_provider(name: str | None = None) -> EmbeddingProvider:
    """Build the configured embedding provider.

    Args:
        name: an explicit provider name. When omitted, ``EMBEDDING_PROVIDER`` is read, and
            when that is unset or blank, :data:`DEFAULT_PROVIDER` is used (D19).

    Returns:
        A ready :class:`EmbeddingProvider`. Construction never loads a model and never
        touches the network, so selecting ``local_bge`` succeeds offline and only
        :meth:`EmbeddingProvider.embed` reports the missing weights.

    Raises:
        UnknownEmbeddingProvider: ``name`` (or ``EMBEDDING_PROVIDER``) is not registered.
            Failing loudly beats falling back to ``hash``, which would silently embed a
            production run with the double.
    """
    requested = name if name is not None else os.environ.get("EMBEDDING_PROVIDER")
    key = (requested or DEFAULT_PROVIDER).strip().lower()
    try:
        provider_class = PROVIDERS[key]
    except KeyError:
        raise UnknownEmbeddingProvider(
            f"unknown EMBEDDING_PROVIDER {key!r}; registered: {sorted(PROVIDERS)}"
        ) from None
    return provider_class()


__all__ = [
    "EMBEDDING_DIM",
    "PROVIDERS",
    "DEFAULT_PROVIDER",
    "EmbeddingProvider",
    "EmbeddingProviderUnavailable",
    "UnknownEmbeddingProvider",
    "HashEmbedding",
    "LocalBgeEmbedding",
    "get_embedding_provider",
]
