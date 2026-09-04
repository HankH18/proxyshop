"""Pure, order-independent text similarity for entity resolution (T-022).

Everything here answers one question — *how alike are these two strings?* — and answers it
with arithmetic over set sizes, which buys three properties the frozen goal depends on:

**Symmetric.** Every measure is built from ``|A ∩ B|``, ``|A ∪ B|`` and ``|A| + |B|``, none of
which can tell which argument came first. ``similarity(a, b)`` and ``similarity(b, a)`` are
the same float, not merely the same to a tolerance.

**Deterministic.** No hashing of iteration order, no floating-point accumulation over a set
(only over its *size*), no clock, no PRNG. The same two strings score identically in every
process on every machine, which is what makes a SAME_AS confidence reproducible and a
disagreement between two runs a real defect rather than noise.

**Total, and never NaN.** Every ratio's denominator is guarded, and an undefined comparison
answers ``0.0`` rather than dividing by zero or propagating a NaN. That matters more than it
looks: a NaN score makes ``score >= threshold`` false *and* ``score < threshold`` false, so a
comparator that admits one lets the order of its inputs decide its output.

Two measures, blended, because each fails where the other works:

``token_alignment``
    Sees words, and does not require them to be spelled identically. Each word is scored
    against its *best* counterpart on the other side and those scores are averaged both ways
    round — the symmetrised Monge-Elkan measure. Word order is irrelevant to it, so
    ``"trail runner shoe"`` and ``"shoe trail runner"`` are the same phrase.

    Plain :func:`token_jaccard` is kept as a primitive but is **not** what
    :func:`text_similarity` blends, and the reason is a measured one: token equality treats a
    single plural as a whole missing word, scoring ``"trail runner shoe"`` against
    ``"trail runner shoes"`` at ``0.5``, which then drags the pair below any threshold worth
    configuring. Two storefronts pluralising one product differently is not an unusual case
    to get wrong — it is the *common* case.

``trigram_dice``
    Sees spelling across the whole phrase. It catches the reordering, the hyphen and the
    abbreviation that survive word alignment, and in exchange it happily scores two unrelated
    English strings at ``0.2`` purely because English shares letter runs. Weighted below the
    word measure for exactly that reason.

    >>> round(text_similarity("Trail Runner Shoe", "Trail Runner Shoes"), 3)
    0.871
    >>> text_similarity("Ceramic Burr Coffee Grinder", "") == 0.0
    True
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from .identity import fold, tokens

__all__ = [
    "TOKEN_WEIGHT",
    "TRIGRAM_ORDER",
    "dice",
    "jaccard",
    "text_similarity",
    "token_alignment",
    "trigram_dice",
    "trigrams",
    "token_jaccard",
]

#: How much of :func:`text_similarity` is word agreement rather than whole-phrase spelling
#: agreement. Words carry the meaning, so they lead; the phrase-level character measure is the
#: tie-breaker, and is held below half because unrelated English text shares letter runs and a
#: character measure alone has a floor well above zero.
TOKEN_WEIGHT = 0.6

#: Character n-gram width. Three is the standard choice for short strings: bigrams collide
#: across unrelated words often enough to lift the floor, and 4-grams are too sparse to
#: register the single-character differences (``shoe``/``shoes``) trigrams exist to catch.
TRIGRAM_ORDER = 3

#: Sentinel padding, so a string's first and last characters participate in as many n-grams as
#: its middle ones do. They cannot occur in folded text, which is what keeps them sentinels.
_PAD_START = "\x02"
_PAD_END = "\x03"


def jaccard(left: Collection[Any], right: Collection[Any]) -> float:
    """``|A ∩ B| / |A ∪ B|``, and ``0.0`` when both sides are empty.

    Two empty sets are *not* similar here. Set theory would call them identical, and for a
    matcher that is the wrong answer: an empty token set means the record had no name, and
    "neither of these products has a name" is an absence of evidence, never evidence of
    sameness. Returning ``1.0`` would link every unnamed product in the catalog to every other.
    """
    a, b = set(left), set(right)
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def dice(left: Collection[Any], right: Collection[Any]) -> float:
    """``2|A ∩ B| / (|A| + |B|)``, and ``0.0`` when both sides are empty (see :func:`jaccard`)."""
    a, b = set(left), set(right)
    total = len(a) + len(b)
    if total == 0:
        return 0.0
    return 2 * len(a & b) / total


def trigrams(value: Any, order: int = TRIGRAM_ORDER) -> frozenset[str]:
    """The padded character n-grams of ``value``'s folded form.

    Args:
        value: raw text; it is folded through :func:`~ingest.er.identity.fold` first.
        order: n-gram width. Defaults to :data:`TRIGRAM_ORDER`.

    Returns:
        Every window of ``order`` characters over the padded string, or the empty set when
        ``value`` folds to nothing.
    """
    folded = fold(value)
    if not folded:
        return frozenset()
    padded = _PAD_START * (order - 1) + folded + _PAD_END * (order - 1)
    return frozenset(padded[i : i + order] for i in range(len(padded) - order + 1))


def token_jaccard(left: Any, right: Any) -> float:
    """Word-level agreement by exact token equality, order-insensitive.

    A primitive, not the measure :func:`text_similarity` uses — see the module docstring for
    why exact token equality is the wrong instrument for product titles.
    """
    return jaccard(tokens(left), tokens(right))


def _unique_tokens(value: Any) -> tuple[str, ...]:
    """``value``'s tokens, de-duplicated, first occurrence order preserved.

    De-duplicated so a title that repeats a word ("Shoe, Trail Shoe") cannot weight that word
    twice in the average; ordered so the sum is taken in the same sequence every run and the
    result does not move in its last bit between processes.
    """
    return tuple(dict.fromkeys(tokens(value)))


def _directional_alignment(source: tuple[str, ...], target: tuple[str, ...]) -> float:
    """Mean over ``source`` of each token's best :func:`trigram_dice` against ``target``."""
    if not source or not target:
        return 0.0
    return sum(max(trigram_dice(token, other) for other in target) for token in source) / len(
        source
    )


def token_alignment(left: Any, right: Any) -> float:
    """Symmetrised Monge-Elkan word agreement between two texts, in ``[0, 1]``.

    Each word is matched to its most similar counterpart on the other side rather than to an
    identical one, so a plural, a spelling variant or a truncation costs a fraction of a word
    instead of a whole one. Averaged in both directions, which is what makes it symmetric:
    the one-directional form rewards a *short* title matched into a long one, so
    ``"Shoe"`` against ``"Trail Runner Shoe"`` would otherwise score 1.0 in one direction and
    0.5 in the other, and which one you got would depend on argument order.

    Args:
        left: one record's text.
        right: the other's.

    Returns:
        The mean of the two directional alignments; ``0.0`` if either side has no tokens.
    """
    left_tokens, right_tokens = _unique_tokens(left), _unique_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    forward = _directional_alignment(left_tokens, right_tokens)
    backward = _directional_alignment(right_tokens, left_tokens)
    return (forward + backward) / 2.0


def trigram_dice(left: Any, right: Any) -> float:
    """Spelling-level agreement between two texts."""
    return dice(trigrams(left), trigrams(right))


def text_similarity(left: Any, right: Any) -> float:
    """How alike two free-text fields are, in ``[0, 1]``.

    ``0.0`` whenever either side folds to nothing — an absent name is not a weak match, it is
    no evidence — and exactly ``1.0`` when the two fold to the same string, so that a title
    differing only in case, accents, punctuation or a homoglyph is not merely *close* to
    identical but identical.

    Args:
        left: one record's text.
        right: the other's.

    Returns:
        ``TOKEN_WEIGHT`` parts word agreement to ``1 - TOKEN_WEIGHT`` parts spelling
        agreement, clamped into ``[0, 1]``.
    """
    left_folded, right_folded = fold(left), fold(right)
    if not left_folded or not right_folded:
        return 0.0
    if left_folded == right_folded:
        return 1.0
    blended = TOKEN_WEIGHT * token_alignment(left, right) + (1.0 - TOKEN_WEIGHT) * trigram_dice(
        left, right
    )
    return min(1.0, max(0.0, blended))
