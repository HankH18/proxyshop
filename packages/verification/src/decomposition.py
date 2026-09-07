"""The generic text-to-claims engine: sentences with offsets, hedges, rules, a scan.

This is ``services/ingest/src/extraction/rules.py``'s engine, moved DOWN to the leaf package
that both consumers can ship. It is not a rewrite and it is not meant to be a second one —
:mod:`~claim_verification.pitch` uses it, ``ingest.extraction.rules`` should be changed to
import it, and until that change lands
``test_pitch.py::test_the_shared_engine_and_the_ingest_rule_engine_do_not_drift`` compares the
two definition by definition so the duplicate cannot quietly diverge.

Why it moved, which was measured rather than reasoned about
-----------------------------------------------------------
The first version of :mod:`~claim_verification.pitch` did the obvious thing and imported
``ingest.extraction.rules`` directly. That is right about reuse and **wrong about the
direction of the arrow**, and two Dockerfiles say so:

* ``apps/trust/Dockerfile`` ships ``packages/verification`` and no ``ingest`` at all. With the
  import in place, ``apps/trust/tests/test_repro_open_tickets.py::
  test_the_trust_image_copy_set_can_resolve_the_claim_verifier`` went RED — the deployed trust
  service could no longer resolve ``trust.verification.verify``, so **every product-fact claim
  would have been unverifiable in production, silently**. That gate exists because this has
  happened before.
* ``apps/exchange/Dockerfile`` ships ``services/ingest/src/{__init__.py,graph/,embeddings/}``
  and NOT ``extraction/`` or ``adapters/``. Reproduced in a subprocess with those modules made
  unimportable: ``import claim_verification`` raises ``ModuleNotFoundError: No module named
  'ingest.extraction.claims'``. Nothing in the exchange's own suite would have caught that —
  the decomposition would have been green in the tree and absent from the artifact that ships,
  which is the defect class this repository keeps finding.

``packages/verification`` is a leaf: stdlib-only, shipped by the trust image and the exchange
image, and named in the docstring of both as the one comparator engine. ``services/ingest`` is
a crawler service whose extraction module reaches ``adapters.netguard`` (SSRF policy, sockets)
and ``graph.model``. A leaf verification package depending on a crawler service is backwards;
a crawler service depending on the verification leaf is not. So the four generic objects live
here and the POLICY-PAGE RULE TABLE stays where it belongs, in ingest.

**The follow-up, stated precisely so it is a deletion rather than a rediscovery.** In
``services/ingest/src/extraction/rules.py`` replace the definitions of ``HEDGE_TERMS``,
``HEDGE_DISCOUNT``, ``sentences``, ``hedge_discount``, ``PolicyRule`` and ``RuleExtractor``'s
scan loop with imports from this module, keeping ``RULES``, ``CLAIM_TYPES`` and the
``_DurationRule``/``_MoneyRule``/``_FlagRule``/``_PhraseRule`` table there. ``RawClaim`` stays
in ``ingest.extraction.claims`` — it validates against DESIGN's provenance shape and is
ingest's own output type; :class:`TextReading` here is the pre-provenance reading and is
deliberately smaller.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CONFIDENCE_FLOOR",
    "HEDGE_DISCOUNT",
    "HEDGE_TERMS",
    "Rule",
    "TextReading",
    "hedge_discount",
    "scan",
    "sentences",
]

#: Words that turn a commitment into an aspiration. Matched on word boundaries against the
#: lower-cased sentence. Byte-identical to ``ingest.extraction.rules.HEDGE_TERMS`` and held to
#: that by a test — a hedge vocabulary that drifts between two decomposers means the same
#: sentence is a promise on one path and marketing on the other.
HEDGE_TERMS: tuple[str, ...] = (
    "may",
    "might",
    "maybe",
    "occasionally",
    "usually",
    "typically",
    "generally",
    "normally",
    "often",
    "sometimes",
    "should",
    "aim to",
    "aims to",
    "try to",
    "tries to",
    "hope to",
    "up to",
    "as fast as",
    "where possible",
    "subject to",
    "in most cases",
    "we believe",
)

#: What a hedge does to a reading's confidence. One hedge is enough: the sentence has stopped
#: being a promise, and a second qualifier does not make it less of one.
HEDGE_DISCOUNT = 0.4

#: C10's published confidence floor — anything strictly below it is not admitted as a fact.
#: The same number ``ingest.extraction.claims.DEFAULT_CONFIDENCE_FLOOR`` publishes, and held to
#: it by a test. What each side DOES below the floor differs and is each caller's decision:
#: ingest quarantines for review, :func:`~claim_verification.pitch.decompose_pitch` drops.
CONFIDENCE_FLOOR = 0.6

_HEDGE_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(term) for term in HEDGE_TERMS) + r")(?![a-z])"
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def sentences(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield ``(start, end, sentence)`` for each sentence in ``text``.

    Splits on terminal punctuation *and* on semicolons, because "shipping is free over $50;
    expedited costs $12" is two commitments in one line and atomic claims are the unit that
    gets stored and graded. Offsets index into ``text`` so a claim can point at the exact
    characters it was read from — a span that points nowhere makes a claim unverifiable in
    exactly the case verification matters most.
    """
    position = 0
    for chunk in _SENTENCE_SPLIT.split(text):
        if chunk is None:
            continue
        start = text.find(chunk, position)
        if start < 0:  # pragma: no cover - only reachable if the split loses text
            start = position
        end = start + len(chunk)
        position = end
        stripped = chunk.strip()
        if stripped:
            offset = chunk.find(stripped)
            yield (start + offset, start + offset + len(stripped), stripped)


def hedge_discount(sentence: str) -> float:
    """:data:`HEDGE_DISCOUNT` when the sentence hedges, ``1.0`` when it commits."""
    return HEDGE_DISCOUNT if _HEDGE_RE.search(sentence.lower()) else 1.0


@dataclass(frozen=True)
class TextReading:
    """One atomic reading, before any provenance is bound to it.

    Deliberately smaller than ``ingest.extraction.claims.RawClaim``, which is ingest's own
    output type and validates against DESIGN's provenance shape. This is the shape a rule
    produces; what a caller stamps on it is the caller's decision, and that is the whole
    difference between a scraped policy page and a seller's pitch.
    """

    key: str
    value: Any
    claim_type: str
    confidence: float
    span: tuple[int, int] = (0, 0)
    evidence: str = ""
    unit: str | None = None


@dataclass(frozen=True)
class Rule:
    """One pattern and the atomic reading it takes out of a matching sentence.

    Attributes:
        name: the rule's id, for diagnostics.
        pattern: compiled, matched against the lower-cased sentence.
        claim_type: the published ``claim_type`` the reading carries. An unpublished term has
            no route into a trust dimension and ``trust.scoring.claim_dimension`` raises on it.
        base_confidence: the score before hedging is applied.
        unit: the unit a reading carries when the rule's own values have one.
    """

    name: str
    pattern: re.Pattern[str]
    claim_type: str
    base_confidence: float
    unit: str | None = None

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        """``(key, value, unit)``, or ``None`` to decline this match.

        **Must never raise.** :func:`scan` runs on a live request path over text a third party
        wrote; a rule that raises takes the whole decomposition with it, so one unreadable
        phrase in one sentence costs the author everything they said. Declining is how a rule
        says "I matched, and I could not read it", and declining is worth exactly what not
        having written the sentence is worth.
        """
        raise NotImplementedError  # pragma: no cover - every concrete rule overrides


def _hashable(value: Any) -> Any:
    """A dict-key-safe view of a reading's value."""
    if isinstance(value, (list, dict, set)):
        return repr(value)
    return value


def scan(text: str, rules: tuple[Rule, ...]) -> list[TextReading]:
    """Every reading ``rules`` finds in ``text``, deduplicated on ``(key, value)``.

    Pure: no network, no clock, no model, no randomness. Readings come back in the order the
    rules declare them, keeping the most confident reading of any repeated ``(key, value)`` —
    the same resolution ``ingest.extraction.rules.RuleExtractor.extract`` applies, and it is
    what stops a fact stated twice, once hedged, from being scored as the hedged one.
    """
    found: dict[tuple[str, Any], TextReading] = {}
    for start, _end, sentence in sentences(text):
        lowered = sentence.lower()
        discount = hedge_discount(lowered)
        for rule in rules:
            for match in rule.pattern.finditer(lowered):
                read = rule.read(match)
                if read is None:
                    continue
                key, value, unit = read
                reading = TextReading(
                    key=key,
                    value=value,
                    claim_type=rule.claim_type,
                    confidence=round(rule.base_confidence * discount, 4),
                    span=(start + match.start(), start + match.end()),
                    evidence=sentence,
                    unit=unit,
                )
                slot = (reading.key, _hashable(reading.value))
                previous = found.get(slot)
                if previous is None or reading.confidence > previous.confidence:
                    found[slot] = reading
    return list(found.values())
