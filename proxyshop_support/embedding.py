"""The two offline embeddings every verify run uses (D19, as amended).

Orchestrator-owned (T-000). ``hash_embed`` below is **unchanged, byte for byte**, by the
amendment that added ``lexical_embed``: six tickets seed ``Product.embedding`` through the
root conftest's ``hash_embedding`` fixture, the frozen acceptance test grades
``HashEmbedding`` against it, and the ranking gate's measured numbers are numbers *about* it.
Amending a decision is not licence to move the thing the decision was measured on.

D19 keeps ``torch``/``sentence-transformers`` an optional extra that is *never* installed by
default (+679 MB of wheels, ~2.2 GB of weights on first call), so no verify run embeds with a
model. D6 fixes the vector shape for the Neo4j index: **1024 dimensions, cosine similarity**.
Both functions here therefore return 1024 L2-normalised floats, which makes cosine similarity
equal to the dot product and exercises D6's index for real.

Two functions, because "exercises the index" and "ranks the catalogue" turned out to be
different claims and only the first was ever true of the hash one:

``hash_embed``
    SHA-256 bytes reshaped into floats. Its cosine measures **byte identity, not meaning**:
    against ``"running shoes"`` it scores ``"espresso machine"`` at ``+0.063`` and
    ``"trail running shoe"`` at ``-0.008``. It is still the right instrument wherever a test
    needs a content-addressed, near-orthogonal vector — two distinct texts, two unrelated
    vectors, no lexical structure leaking into the assertion — and it stays selectable as
    ``EMBEDDING_PROVIDER=hash``. It is no longer the default, because an *order* produced
    under it is a ranking of nothing.

``lexical_embed``
    Hashed word and character-trigram features. Its cosine tracks
    ``ingest.er.similarity.text_similarity``, so a relevant document outranks an irrelevant
    one, and it is the configured default. It has **no semantics** — see its own docstring
    for what that costs.

Properties a test may rely on, of both:

* deterministic across processes, machines and runs (SHA-256 based, no PRNG seeding, and
  no dependence on ``PYTHONHASHSEED`` — nothing here iterates a ``set`` or a ``hash()``);
* unit length (``sum(x*x) == 1`` to within float tolerance) for any non-empty text;
* identical texts embed identically.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections.abc import Sequence

#: D6: the Neo4j vector index is created with 1024 dimensions and cosine similarity.
EMBEDDING_DIM = 1024

#: How much of a :func:`lexical_embed` vector is whole-word agreement rather than
#: character-level agreement. Deliberately the same 0.6/0.4 split as
#: ``ingest.er.similarity.text_similarity``'s ``TOKEN_WEIGHT``, and for its reason: words
#: carry the meaning, so they lead, while the character measure is the tie-breaker that
#: survives a plural or a hyphen — and it is held below half because unrelated English
#: strings share letter runs, so a character measure alone has a floor well above zero.
LEXICAL_TOKEN_WEIGHT = 0.6

#: Character n-gram width, matching ``ingest.er.similarity.TRIGRAM_ORDER``. Bigrams collide
#: across unrelated words often enough to lift the floor; 4-grams are too sparse to register
#: the single-character difference (``shoe``/``shoes``) trigrams exist to catch.
LEXICAL_TRIGRAM_ORDER = 3

#: Sentinel padding so a word's first and last characters sit in as many trigrams as its
#: middle ones. It cannot occur in folded text, which is what keeps it a sentinel.
_TRIGRAM_PAD = "\x02"

#: Separates the feature namespace from the feature in the hashed key, so the word ``"abc"``
#: and the trigram ``"abc"`` land in different buckets instead of reinforcing each other.
_FEATURE_SEPARATOR = b"\x1f"


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


def fold_for_lexical(text: str) -> str:
    """Normalise ``text`` down to the surface :func:`lexical_embed` actually compares.

    NFKD, combining marks dropped, case-folded, every non-alphanumeric character treated as a
    separator, runs of whitespace collapsed. So ``"Fragrance-Free"`` and ``"fragrance free"``
    fold to the same string, and ``"Éclair"`` and ``"eclair"`` do too. Digits survive, because
    ``"SPF 50"`` and ``"12 inch"`` are product facts rather than punctuation.

    Args:
        text: raw text.

    Returns:
        The folded form, possibly empty (for text with no alphanumeric characters at all).
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    separated = "".join(ch if ch.isalnum() else " " for ch in without_marks.casefold())
    return " ".join(separated.split())


def lexical_tokens(text: str) -> tuple[str, ...]:
    """``text``'s folded words, de-duplicated, first-occurrence order preserved.

    De-duplicated so a title repeating a word ("Shoe, Trail Shoe") cannot weight that word
    twice; **ordered**, and that half is load-bearing rather than tidy. Words of different
    lengths carry different trigram weights below, so when two of their features collide into
    one bucket the order they are summed in decides that component's last bits. Measured: a
    ``set`` here changes the vector across ``PYTHONHASHSEED`` values — four distinct vectors
    over six seeds for a composed product document, six of six for an eighty-token one.
    """
    return tuple(dict.fromkeys(fold_for_lexical(text).split()))


def _token_trigrams(token: str, order: int = LEXICAL_TRIGRAM_ORDER) -> tuple[str, ...]:
    """The padded character trigrams of one folded word, de-duplicated and ordered.

    Per *word* rather than over the whole phrase, so no trigram straddles a word boundary:
    a phrase-level trigram set makes ``"wool winter"`` and ``"cool winter"`` share the
    boundary grams of both words, which is agreement about spacing, not about words.

    Ordered here for consistency with :func:`lexical_tokens`, not because it is currently
    observable — and the distinction is recorded so nobody has to re-derive it. Every trigram
    of ONE word carries the same weight, so two of them colliding into one bucket sum to the
    same float in either order; substituting a ``set`` here was measured to leave the vector
    byte-identical across six ``PYTHONHASHSEED`` values. That stops being true the moment
    weights vary within a word (IDF, position), which is exactly the sort of change that would
    be made without re-checking this, so the ordering stays.
    """
    padded = _TRIGRAM_PAD * (order - 1) + token + _TRIGRAM_PAD * (order - 1)
    return tuple(dict.fromkeys(padded[i : i + order] for i in range(len(padded) - order + 1)))


def _bucket(namespace: str, feature: str, dim: int) -> tuple[int, float]:
    """Map one feature to ``(index, sign)`` in the ``dim``-dimensional space.

    SHA-256 rather than :func:`hash`, which is randomised per process, and **signed** rather
    than always-positive: two distinct features that collide contribute a cross-term of
    random sign, whose expectation is zero. Unsigned hashing makes every collision *add*
    similarity, which lifts the floor between unrelated documents — the exact failure this
    provider exists to avoid.
    """
    digest = hashlib.sha256(
        namespace.encode("ascii") + _FEATURE_SEPARATOR + feature.encode("utf-8")
    ).digest()
    index = int.from_bytes(digest[:4], "big") % dim
    return index, (1.0 if digest[4] & 1 else -1.0)


def _hashed_bag(features: Sequence[tuple[str, str, float]], dim: int) -> list[float] | None:
    """Accumulate weighted features into a unit vector, or ``None`` if they cancel to zero."""
    dense = [0.0] * dim
    for namespace, feature, weight in features:
        index, sign = _bucket(namespace, feature, dim)
        dense[index] += sign * weight
    norm = math.sqrt(sum(component * component for component in dense))
    if norm == 0.0:
        return None
    return [component / norm for component in dense]


def lexical_embed(text: str, *, dim: int = EMBEDDING_DIM) -> list[float]:
    """Embed ``text`` as a deterministic, L2-normalised ``dim``-dimensional **lexical** vector.

    WHAT IT IS. Two bags of features, hashed into the same space and blended:

    * every folded word of ``text``, and
    * every padded character trigram of every folded word, each word's trigrams scaled by
      ``1/sqrt(number of trigrams)`` so a long word does not outvote a short one.

    Each bag is L2-normalised on its own and then scaled by ``sqrt(LEXICAL_TOKEN_WEIGHT)``
    and ``sqrt(1 - LEXICAL_TOKEN_WEIGHT)``. Because the two namespaces hash to
    near-orthogonal directions, the cosine of two such vectors is approximately
    ``0.6 * (word agreement) + 0.4 * (spelling agreement)`` — the same blend, and the same
    weights, as ``ingest.er.similarity.text_similarity``. Measured over the 45-pair corpus in
    ``services/ingest/tests/test_embedding_ranking_gate.py``: **0 inversions, worst per-query
    spread +0.4936** (``text_similarity`` itself: 0 and +0.4728; ``hash_embed``: 28 of 45
    inverted, every spread negative).

    WHAT IT IS NOT — read this before trusting an order it produced. **This is not a semantic
    model. It has no semantics at all, only surface.** Two texts that mean the same thing and
    share no letters score the same as two unrelated products:

    * ``"laptop"`` vs ``"notebook computer"`` — **+0.0000**
    * ``"running shoes"`` vs ``"espresso machine"`` — **+0.0000**
    * ``"running shoes"`` vs ``"sneakers"`` — **+0.0676**
    * ``"running shoes"`` vs ``"trail running shoe"`` — **+0.5090**

    ``"laptop"`` against ``"notebook computer"`` is the example to keep in mind: a shopper
    asking for a laptop retrieves **nothing** through this provider unless the catalogue also
    says "laptop". Synonymy, hypernymy, translation and paraphrase are all invisible to it;
    ``"sneakers"`` only edges past ``"espresso machine"`` by accident of shared letters, not
    because anything here knows what a sneaker is. Closing that gap needs a real model —
    ``EMBEDDING_PROVIDER=local_bge``, which D19 keeps as an optional extra — or a query
    expansion step in front of retrieval. Do not read a lexical hit as evidence of meaning.

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

    words = lexical_tokens(text)
    if not words:
        # Non-empty text that folds away entirely — "!!!", "—", a lone emoji. It has no
        # lexical content to compare, so it gets a content-addressed unit vector instead of
        # the zero vector: `set_product_embedding` rejects a zero-norm vector outright, and a
        # provider that answered one here would make such a product unwritable rather than
        # merely unrankable. Byte-identical text matches itself; nothing else comes near it.
        index, sign = _bucket("raw", text, dim)
        vector = [0.0] * dim
        vector[index] = sign
        return vector

    word_features = [("w", word, 1.0) for word in words]
    trigram_features: list[tuple[str, str, float]] = []
    for word in words:
        grams = _token_trigrams(word)
        weight = 1.0 / math.sqrt(len(grams))
        trigram_features.extend(("g", gram, weight) for gram in grams)

    word_bag = _hashed_bag(word_features, dim)
    trigram_bag = _hashed_bag(trigram_features, dim)
    vector = [0.0] * dim
    if word_bag is not None:
        scale = math.sqrt(LEXICAL_TOKEN_WEIGHT)
        vector = [component + scale * bag for component, bag in zip(vector, word_bag, strict=True)]
    if trigram_bag is not None:
        scale = math.sqrt(1.0 - LEXICAL_TOKEN_WEIGHT)
        vector = [
            component + scale * bag for component, bag in zip(vector, trigram_bag, strict=True)
        ]

    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:  # every feature cancelled against a collision; keep the contract total
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
