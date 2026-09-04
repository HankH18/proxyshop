"""The pairwise entity-resolution decision (T-022, DESIGN §Data models).

:func:`match` answers one question about two catalog records — *are these the same trade
item, and how sure are we?* — and it answers it as a confidence plus a comparison, never as
two independent judgements. That is the whole shape of this module:

.. code-block:: text

    linked  ==  (confidence >= threshold)

**for every input, with no exceptions.** The three guarantees T-022 owes are then *theorems*
of that one rule rather than branches that can drift apart from it:

*A GTIN match always links.* An identity match scores exactly ``1.0``, and ``1.0 >= t`` holds
for every ``t`` in the permitted threshold domain, so it links at any configuration. Nothing
special-cases it; it simply cannot fail to clear a bar that is at most ``1.0``.

*A below-threshold pair never links.* Trivially — it is the same comparison.

*A refused pair scores below the threshold that refused it.* Also trivially, and this is the
one the frozen test says out loud: "the confidence and the link decision have to agree". A
matcher that computes a score and then decides separately is a matcher with two opinions, and
the second one is always the one nobody tested.

The threshold domain is ``(0.0, 1.0]``
--------------------------------------
Zero is rejected rather than honoured. ``0.0`` means "link every pair you are shown", which
is not a configuration anyone chooses — it is what an unset config variable coerces to, and
honouring it would silently connect the entire catalog into one product. Values above ``1.0``
are rejected for the mirror-image reason: a threshold written ``90`` instead of ``0.9`` would
otherwise link *nothing*, forever, with no error and a perfectly green test suite. NaN is
rejected because every comparison against it is false, which makes it a third silent
"link nothing". All three fail loudly at the call instead.

What produces a score, and what vetoes one
------------------------------------------
DESIGN §Data models is explicit: **never match products by free-text name alone.** That is
enforced structurally here rather than by convention. The name can contribute at most
:data:`NAME_WEIGHT` of the score, so a pair whose *only* agreement is its title cannot exceed
``0.6`` and cannot clear any sane threshold — including the :data:`DEFAULT_MATCH_THRESHOLD`,
which is set above that ceiling on purpose. Corroboration has to come from somewhere else:
brand, an embedding, or shared attributes.

Corroborating signals combine with ``max``, not with an average. A signal that is *silent* —
an embedding provider with no lexical power, an attribute set with no keys in common — must
not dilute a signal that speaks; averaging would let the absence of one kind of evidence
cancel the presence of another. The strongest independent corroboration is what the pair has.

Conflicts, by contrast, veto outright and drop the confidence to ``0.0``:

============================  ================================================================
two valid, different GTINs    Different trade items. The strongest negative signal in commerce
                              data, and it beats any amount of textual agreement.
two present, unlike brands    Different manufacturers. A conflict is not a low similarity; it
                              is a positive statement that these disagree.
either name missing           No evidence at all. Scoring this on brand alone would link every
                              nameless record of one brand to every other.
============================  ================================================================

An *absent* field is not a conflict. Absence abstains; only disagreement vetoes.

    >>> left = {"product_id": "p-1", "gtin": "00012345678905", "canonical_name": "Trail Runner"}
    >>> right = {"product_id": "p-2", "gtin": "012345678905", "canonical_name": "Zapatilla"}
    >>> decision = match(left, right, 0.95)
    >>> decision.linked, decision.confidence, decision.method
    (True, 1.0, 'gtin')
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from proxyshop_support.embedding import cosine

from .identity import fold, normalize_gtin, tokens
from .similarity import text_similarity

__all__ = [
    "BRAND_CONFLICT_CEILING",
    "CORROBORATION_WEIGHT",
    "DEFAULT_MATCH_THRESHOLD",
    "MAX_SIMILARITY_CONFIDENCE",
    "METHOD_BRAND_CONFLICT",
    "METHOD_GTIN",
    "METHOD_GTIN_CONFLICT",
    "METHOD_NO_EVIDENCE",
    "METHOD_SAME_NODE",
    "METHOD_SIMILARITY",
    "NAME_WEIGHT",
    "MatchDecision",
    "brand_agreement",
    "match",
    "read_field",
    "record_name",
]

#: The share of the score free-text name agreement can contribute. It is deliberately below
#: every threshold this package considers usable, which is what turns DESIGN's "never match
#: products by free-text name alone" from a rule reviewers must remember into arithmetic that
#: cannot be forgotten: with no corroboration at all, a perfect title match scores ``0.6``.
NAME_WEIGHT = 0.6

#: The remaining share, carried by whichever corroborating signal is strongest.
CORROBORATION_WEIGHT = 1.0 - NAME_WEIGHT

#: The default SAME_AS floor (T-022: "threshold config"). Chosen from the arithmetic above
#: rather than by feel: clearing ``0.86`` requires corroboration — the ceiling without it is
#: ``0.60`` — *and* a name similarity of at least ``0.767`` even when corroboration is
#: perfect. T-022 acceptance 1 grades **precision**, and the two errors are not symmetric: a
#: missed link leaves two listings for one product, while a false link merges two different
#: products' offers into a single buyer-facing listing and quotes the wrong price for one of
#: them. This floor is set where a false merge is hard, not where recall is comfortable.
DEFAULT_MATCH_THRESHOLD = 0.86

#: Below this, two *present* brands are treated as a conflict rather than a weak agreement.
#: Above it they merely corroborate weakly. "Cascade" vs "Cascade Outdoors" is the case this
#: number exists for: plainly the same maker, not the same string.
BRAND_CONFLICT_CEILING = 0.5

#: The ceiling on the similarity path. The frozen test calls a GTIN match "a certainty"; a
#: certainty ought to be the *only* thing that scores ``1.0``, so no amount of textual and
#: brand agreement can reach it. It also means a caller who sets ``threshold=1.0`` gets
#: identity-only resolution, which is a useful and otherwise unavailable configuration.
MAX_SIMILARITY_CONFIDENCE = 0.99

#: How many decimal places a similarity confidence is reported to. Float arithmetic is already
#: deterministic for a fixed expression, so this is not what buys determinism — it buys
#: *stability*: a later reordering of the weighted sum cannot silently move a stored edge
#: confidence in the fifteenth decimal place and make two runs of the resolver disagree.
_CONFIDENCE_PLACES = 12

METHOD_SAME_NODE = "same_node"
METHOD_GTIN = "gtin"
METHOD_GTIN_CONFLICT = "gtin_conflict"
METHOD_BRAND_CONFLICT = "brand_conflict"
METHOD_NO_EVIDENCE = "no_evidence"
METHOD_SIMILARITY = "similarity"

#: The record fields consulted for a product's display name, in order of preference. The graph
#: and the frozen acceptance records both say ``canonical_name``; the other two are what raw
#: adapter output tends to call it.
_NAME_FIELDS = ("canonical_name", "name", "title")


@dataclass(frozen=True)
class MatchDecision:
    """One pairwise SAME_AS decision, and the evidence behind it.

    Frozen and fully order-independent: :attr:`pair` is sorted and :attr:`evidence` is sorted,
    so ``match(a, b, t) == match(b, a, t)`` holds as *value equality*, not merely as agreement
    about :attr:`linked`. Symmetry that only holds for the verdict is symmetry that stops
    holding the moment anyone reads a second field.

    Attributes:
        linked: whether a ``SAME_AS`` edge should be written. Always equal to
            ``confidence >= threshold``.
        confidence: the probability-shaped score in ``[0, 1]`` written onto the edge.
        method: which rule decided — one of the ``METHOD_*`` constants. Not part of the
            decision, but the difference between an auditable resolver and an oracle.
        threshold: the floor this decision was taken against, carried so a stored decision
            can be re-judged without guessing what configuration produced it.
        pair: the two ``product_id`` values, sorted.
        evidence: ``(signal, value)`` pairs, sorted by signal.
    """

    linked: bool
    confidence: float
    method: str
    threshold: float
    pair: tuple[str, str]
    evidence: tuple[tuple[str, float], ...] = field(default_factory=tuple)


def read_field(record: Any, name: str, default: Any = "") -> Any:
    """Read ``name`` off a mapping or an object, treating ``None`` as absent.

    The frozen acceptance records are plain dicts, the graph's are frozen dataclasses and an
    adapter's are whatever it built. All three are legitimate inputs to a resolver, so the
    field is the contract and the container is not.
    """
    if isinstance(record, Mapping):
        value = record.get(name, default)
    else:
        value = getattr(record, name, default)
    return default if value is None else value


def record_name(record: Any) -> str:
    """The product's display name, from whichever of :data:`_NAME_FIELDS` it carries."""
    for name_field in _NAME_FIELDS:
        value = read_field(record, name_field)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def brand_agreement(left: Any, right: Any) -> float | None:
    """How far two records agree on their brand, or ``None`` when one of them has no brand.

    ``None`` is not zero. A record that never published a brand *abstains*; a record that
    published a different one *disagrees*, and only disagreement vetoes. Collapsing the two
    would make every brandless product conflict with everything.

    One rule beyond plain text similarity: when one brand's token set is a non-empty subset of
    the other's, the two agree completely. ``"Cascade"`` and ``"Cascade Outdoors"`` are the
    same maker wearing a marketing suffix on one storefront and not the other, and plain
    similarity scores that pair at ``0.507`` — a hair above the conflict ceiling, which is far
    too close to "these are different companies" for a case this common. The subset rule
    decides it on structure instead of on a tuned constant. It is not a licence to link: the
    *name* still has to reach ``0.767`` for the pair to clear the default threshold.

    Returns:
        The agreement in ``[0, 1]``, or ``None`` when either side has no brand.
    """
    left_tokens = frozenset(tokens(read_field(left, "brand")))
    right_tokens = frozenset(tokens(read_field(right, "brand")))
    if not left_tokens or not right_tokens:
        return None
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return 1.0
    return text_similarity(read_field(left, "brand"), read_field(right, "brand"))


def _finite_vector(value: Any) -> tuple[float, ...] | None:
    """``value`` as a tuple of finite floats, or ``None`` if it is not a usable vector.

    NaN and infinity are refused rather than propagated. One NaN component makes the cosine
    NaN, a NaN confidence makes both ``>= threshold`` and ``< threshold`` false, and a
    comparator that can be false in both directions lets the order of its inputs pick the
    answer. That defect is cheaper to refuse at the door than to debug in a graph.
    """
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return None
    if not isinstance(value, Sequence):
        return None
    try:
        components = tuple(float(component) for component in value)
    except (TypeError, ValueError):
        return None
    if not components or not all(math.isfinite(component) for component in components):
        return None
    return components


def _embedding_signal(left: Any, right: Any) -> float | None:
    """Cosine agreement between two record embeddings, or ``None`` when there is not one.

    Negative cosines are clamped to ``0.0`` rather than rescaled into ``[0, 1]``. Rescaling
    (``(c + 1) / 2``) would give every orthogonal pair ``0.5``, and under the default
    ``EMBEDDING_PROVIDER=hash`` provider *every* pair of distinct strings is near-orthogonal —
    measured on this repo, ``"Trail Runner Shoe"`` and ``"Trail Runner Shoes"`` cosine to
    ``-0.024``, indistinguishable from two unrelated products at ``-0.012``. Clamping makes an
    uninformative provider *silent* (contributing nothing) instead of uniformly generous, so
    swapping in a real semantic provider adds recall and swapping it out costs recall, and
    neither one quietly moves every confidence in the catalog.
    """
    left_vector = _finite_vector(read_field(left, "embedding", None))
    right_vector = _finite_vector(read_field(right, "embedding", None))
    if left_vector is None or right_vector is None:
        return None
    if len(left_vector) != len(right_vector):
        return None
    return max(0.0, cosine(left_vector, right_vector))


def _attribute_signal(left: Any, right: Any) -> float | None:
    """Agreement over the attribute keys both records carry, or ``None`` if they share none."""
    left_attributes = read_field(left, "attributes", None)
    right_attributes = read_field(right, "attributes", None)
    if not isinstance(left_attributes, Mapping) or not isinstance(right_attributes, Mapping):
        return None
    left_folded = {fold(key): value for key, value in left_attributes.items() if fold(key)}
    right_folded = {fold(key): value for key, value in right_attributes.items() if fold(key)}
    shared = sorted(set(left_folded) & set(right_folded))
    if not shared:
        return None
    agreed = sum(1 for key in shared if fold(str(left_folded[key])) == fold(str(right_folded[key])))
    return agreed / len(shared)


def _validated_threshold(threshold: Any) -> float:
    """The threshold as a float in ``(0.0, 1.0]``, or a loud failure.

    Raises:
        ValueError: ``threshold`` is not a real number, is not finite, or is outside
            ``(0.0, 1.0]``. Every rejected value is one that would otherwise degrade the
            resolver silently — see the module docstring.
    """
    if isinstance(threshold, bool):
        raise ValueError(f"threshold must be a number, got {threshold!r}")
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        raise ValueError(f"threshold must be a number, got {threshold!r}") from None
    if not math.isfinite(value):
        raise ValueError(
            f"threshold must be finite, got {threshold!r}; every comparison against NaN or "
            "infinity is false, which silently links nothing"
        )
    if not 0.0 < value <= 1.0:
        raise ValueError(
            f"threshold must be in (0.0, 1.0], got {value!r}; 0.0 links the entire catalog "
            "together and anything above 1.0 links nothing, both without an error"
        )
    return value


def _decide(
    *,
    confidence: float,
    method: str,
    threshold: float,
    pair: tuple[str, str],
    evidence: dict[str, float],
) -> MatchDecision:
    """Build the decision, applying the single rule the whole module rests on."""
    bounded = min(1.0, max(0.0, confidence))
    return MatchDecision(
        linked=bounded >= threshold,
        confidence=bounded,
        method=method,
        threshold=threshold,
        pair=pair,
        evidence=tuple(sorted(evidence.items())),
    )


def match(left: Any, right: Any, threshold: Any = DEFAULT_MATCH_THRESHOLD) -> MatchDecision:
    """Decide whether two catalog records name the same product.

    Pure: no network, no clock, no datastore, no randomness. The same two records and the same
    threshold produce an equal :class:`MatchDecision` in every process, which is what lets a
    stored ``SAME_AS`` confidence be re-derived and audited rather than merely trusted.

    Args:
        left: a record carrying ``product_id``, ``gtin``, ``canonical_name`` and ``brand``;
            optionally ``embedding`` (a sequence of floats) and ``attributes`` (a mapping).
            Mappings and objects are both accepted.
        right: the other record, same shape.
        threshold: the SAME_AS floor, in ``(0.0, 1.0]``. Defaults to
            :data:`DEFAULT_MATCH_THRESHOLD`.

    Returns:
        The :class:`MatchDecision`, whose ``linked`` is always ``confidence >= threshold``.

    Raises:
        ValueError: ``threshold`` is not a finite number in ``(0.0, 1.0]``.
    """
    floor = _validated_threshold(threshold)
    left_id = str(read_field(left, "product_id"))
    right_id = str(read_field(right, "product_id"))
    pair = (left_id, right_id) if left_id <= right_id else (right_id, left_id)

    if left_id and left_id == right_id:
        return _decide(
            confidence=1.0,
            method=METHOD_SAME_NODE,
            threshold=floor,
            pair=pair,
            evidence={"identity": 1.0},
        )

    left_gtin = normalize_gtin(read_field(left, "gtin"))
    right_gtin = normalize_gtin(read_field(right, "gtin"))
    if left_gtin and right_gtin:
        if left_gtin == right_gtin:
            return _decide(
                confidence=1.0,
                method=METHOD_GTIN,
                threshold=floor,
                pair=pair,
                evidence={"gtin": 1.0},
            )
        return _decide(
            confidence=0.0,
            method=METHOD_GTIN_CONFLICT,
            threshold=floor,
            pair=pair,
            evidence={"gtin": 0.0},
        )

    left_name, right_name = record_name(left), record_name(right)
    if not fold(left_name) or not fold(right_name):
        # No name on one side, so no primary evidence. Scoring on corroboration alone would
        # link every nameless record of one brand to every other one.
        return _decide(
            confidence=0.0,
            method=METHOD_NO_EVIDENCE,
            threshold=floor,
            pair=pair,
            evidence={"name": 0.0},
        )

    name_similarity = text_similarity(left_name, right_name)
    evidence: dict[str, float] = {"name": name_similarity}
    corroboration: list[float] = []

    brand_similarity = brand_agreement(left, right)
    if brand_similarity is not None:
        evidence["brand"] = brand_similarity
        if brand_similarity < BRAND_CONFLICT_CEILING:
            return _decide(
                confidence=0.0,
                method=METHOD_BRAND_CONFLICT,
                threshold=floor,
                pair=pair,
                evidence=evidence,
            )
        corroboration.append(brand_similarity)

    embedding_similarity = _embedding_signal(left, right)
    if embedding_similarity is not None:
        evidence["embedding"] = embedding_similarity
        corroboration.append(embedding_similarity)

    attribute_similarity = _attribute_signal(left, right)
    if attribute_similarity is not None:
        evidence["attributes"] = attribute_similarity
        corroboration.append(attribute_similarity)

    best_corroboration = max(corroboration) if corroboration else 0.0
    score = NAME_WEIGHT * name_similarity + CORROBORATION_WEIGHT * best_corroboration
    return _decide(
        confidence=round(min(score, MAX_SIMILARITY_CONFIDENCE), _CONFIDENCE_PLACES),
        method=METHOD_SIMILARITY,
        threshold=floor,
        pair=pair,
        evidence=evidence,
    )
