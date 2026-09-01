"""The deterministic hash embedding (D18: ``EMBEDDING_PROVIDER=hash``).

Orchestrator-owned (T-000), frozen.

D18 keeps ``torch``/``sentence-transformers`` an optional extra that is *never* installed by
default, so every verify run embeds with this function instead. D6 fixes the vector shape
for the Neo4j index: **1024 dimensions, cosine similarity**. Vectors are therefore returned
L2-normalised, which makes cosine similarity equal to the dot product.

Properties a test may rely on:

* deterministic across processes, machines and runs (SHA-256 based, no PRNG seeding);
* unit length (``sum(x*x) == 1`` to within float tolerance) for any non-empty text;
* identical texts embed identically; different texts almost never collide.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

#: D6: the Neo4j vector index is created with 1024 dimensions and cosine similarity.
EMBEDDING_DIM = 1024


def hash_embed(text: str, *, dim: int = EMBEDDING_DIM) -> list[float]:
    """Embed ``text`` as a deterministic, L2-normalised ``dim``-dimensional vector.

    Args:
        text: the text to embed. The empty string yields the zero vector.
        dim: vector length. Defaults to :data:`EMBEDDING_DIM` (1024, per D6).

    Returns:
        A list of ``dim`` floats with unit L2 norm (all-zero for empty input).
    """
    if dim <= 0:
        raise ValueError("dim must be positive")
    if not text:
        return [0.0] * dim

    payload = text.encode("utf-8")
    needed = dim * 2  # two bytes of entropy per component
    stream = bytearray()
    counter = 0
    while len(stream) < needed:
        stream.extend(hashlib.sha256(counter.to_bytes(4, "big") + payload).digest())
        counter += 1

    vector = [
        (int.from_bytes(stream[i * 2 : i * 2 + 2], "big") / 32767.5) - 1.0 for i in range(dim)
    ]
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:  # astronomically unlikely; keep the contract total
        vector[0] = 1.0
        return vector
    return [component / norm for component in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two vectors. Zero vectors give ``0.0``."""
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
