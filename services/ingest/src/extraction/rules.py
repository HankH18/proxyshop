"""Deterministic claim decomposition for policy pages (T-021).

A policy page is prose that states a handful of commitments — a dispatch window, a free
shipping threshold, a returns window, a warranty term. Ticket acceptance 1 grades the
extracted claims against a **human-approved expectation file**, so the default extractor
here is a rule table rather than a model: an approved expectation is only meaningful if
re-running the extractor on the same bytes produces the same claims, and this one is a
pure function of the text.

The rule table is also what makes the LLM path checkable. :mod:`ingest.extraction.model`
runs a Haiku-class model behind a strict schema and validates its output; this module is
what the model's output is compared against in tests, and what the pipeline falls back to
when no client is configured (D3: a verify run has no API key and no network).

Confidence, and why it is not a constant
----------------------------------------
C10 quarantines low-confidence extraction, which is vacuous if every reading scores the
same. Each rule carries a base confidence reflecting how unambiguous its pattern is, and
every reading is discounted when its sentence hedges — "we *may occasionally* be able to
ship faster" is a marketing sentence, not a commitment, and it must not enter the graph
as one. The discount is multiplicative and the result is rounded, so the score is a
reproducible function of the sentence.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .claims import RawClaim

__all__ = [
    "CLAIM_TYPES",
    "HEDGE_DISCOUNT",
    "HEDGE_TERMS",
    "PolicyRule",
    "RULES",
    "RuleExtractor",
    "hedge_discount",
    "sentences",
]

#: DESIGN's published ``claim_type`` vocabulary (the ``claim_type -> trust dimension``
#: table). Every rule below emits one of these; a term outside it would fail
#: ``fixtures.manifest``'s exhaustiveness guard at trust-scoring time, which is a much
#: worse place to find out.
CLAIM_TYPES: frozenset[str] = frozenset(
    {
        "price",
        "unit_price",
        "total_price",
        "discount",
        "promo_eligibility",
        "delivery",
        "shipping_speed",
        "dispatch_window",
        "return_policy",
        "warranty",
        "ingredients",
        "compatibility",
        "nutrition",
        "specifications",
    }
)

#: Words that turn a commitment into an aspiration. Matched on word boundaries against the
#: lower-cased sentence.
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

#: What a hedge does to a reading's confidence. One hedge is enough: the sentence has
#: stopped being a promise, and a second qualifier does not make it less of one.
HEDGE_DISCOUNT = 0.4

_HEDGE_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(term) for term in HEDGE_TERMS) + r")(?![a-z])"
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")

_DAYS_PER_UNIT = {"day": 1, "week": 7, "month": 30, "year": 365}


def _number(raw: str) -> float:
    """Parse a captured numeric literal, tolerating thousands separators."""
    return float(str(raw).replace(",", "").rstrip("."))


def _int(raw: str) -> int:
    return int(_number(raw))


def sentences(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield ``(start, end, sentence)`` for each sentence in ``text``.

    Splits on terminal punctuation *and* on semicolons, because "shipping is free over
    $50; expedited costs $12" is two commitments in one line and atomic claims are the
    unit the graph stores. Offsets index into ``text`` so a claim can point at the exact
    characters it was read from.
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
    """``HEDGE_DISCOUNT`` when the sentence hedges, ``1.0`` when it commits."""
    return HEDGE_DISCOUNT if _HEDGE_RE.search(sentence.lower()) else 1.0


@dataclass(frozen=True)
class PolicyRule:
    """One pattern and the atomic claim it reads out of a matching sentence.

    Attributes:
        name: the rule's id, used in diagnostics and in the approved expectation file.
        pattern: compiled, matched against the lower-cased sentence.
        claim_type: the DESIGN ``claim_type`` the reading carries.
        base_confidence: the score before hedging is applied.
    """

    name: str
    pattern: re.Pattern[str]
    claim_type: str
    base_confidence: float
    key: str = ""
    unit: str | None = None

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        """Turn a match into ``(key, value, unit)``, or ``None`` to decline it."""
        raise NotImplementedError  # pragma: no cover - every concrete rule overrides


@dataclass(frozen=True)
class _DurationRule(PolicyRule):
    """A number of days/weeks/months/years, normalised to days."""

    key_template: str = ""
    unit_group: int = 2

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        amount = _int(match.group(1))
        unit_word = (match.group(self.unit_group) or "day").strip().rstrip("s")
        business = unit_word.startswith("business")
        base = unit_word.replace("business ", "").strip() or "day"
        factor = _DAYS_PER_UNIT.get(base)
        if factor is None:
            return None
        days = amount * factor
        unit = "business_days" if business else "days"
        return (self.key_template, days, unit)


@dataclass(frozen=True)
class _MoneyRule(PolicyRule):
    """A dollar amount, optionally qualified by the shipping tier that carries it."""

    key_template: str = ""
    qualifier_group: int = 0
    amount_group: int = 1
    default_qualifier: str = ""

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        qualifier = self.default_qualifier
        if self.qualifier_group:
            captured = match.group(self.qualifier_group)
            if captured:
                qualifier = captured.strip().lower().replace(" ", "_")
        amount = _number(match.group(self.amount_group))
        return (self.key_template.format(qualifier=qualifier), amount, "USD")


@dataclass(frozen=True)
class _FlagRule(PolicyRule):
    """A boolean commitment — "no restocking fee", "free returns"."""

    key_template: str = ""
    flag: bool = True

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        return (self.key_template, self.flag, None)


@dataclass(frozen=True)
class _PhraseRule(PolicyRule):
    """An unquantified marketing statement. Deliberately low-confidence.

    These exist so that a vague promise is *recorded and quarantined* rather than
    silently dropped: "we may ship faster" is exactly the kind of sentence a seller later
    points at, and a claim nobody extracted cannot be verified or contradicted.
    """

    key_template: str = ""

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        return (self.key_template, match.group(0).strip(), None)


RULES: tuple[PolicyRule, ...] = (
    _DurationRule(
        name="dispatch_window",
        pattern=re.compile(
            r"\bship(?:s|ped|ping)?\b[^.;]{0,40}?\bwithin\s+(\d+)\s+(business\s+day|day)s?\b"
        ),
        claim_type="dispatch_window",
        base_confidence=0.92,
        key_template="shipping.dispatch_window_days",
    ),
    _DurationRule(
        name="returns_window",
        pattern=re.compile(
            r"\breturns?\b[^.;]{0,40}?\b(?:within|for)\s+(\d+)\s+(business\s+day|day|week|month)s?\b"
        ),
        claim_type="return_policy",
        base_confidence=0.93,
        key_template="returns.window_days",
    ),
    _DurationRule(
        name="refund_window",
        pattern=re.compile(
            r"\brefunds?\b[^.;]{0,40}?\bwithin\s+(\d+)\s+(business\s+day|day|week)s?\b"
        ),
        claim_type="return_policy",
        base_confidence=0.9,
        key_template="returns.refund_days",
    ),
    _DurationRule(
        name="warranty_term",
        pattern=re.compile(
            r"\b(\d+)[\s-]*(year|month|day)s?\b[^.;]{0,30}?\b(?:warranty|guarantee)\b"
        ),
        claim_type="warranty",
        base_confidence=0.9,
        key_template="warranty.duration_days",
    ),
    _MoneyRule(
        name="free_shipping_threshold",
        pattern=re.compile(
            r"\b(standard|expedited|express|return|domestic)?\s*shipping\s+is\s+free\s+"
            r"(?:on|for)\s+orders?\s+(?:over|above)\s+\$?([\d,.]+\d)"
        ),
        claim_type="promo_eligibility",
        base_confidence=0.9,
        key_template="shipping.free_over_amount_{qualifier}",
        qualifier_group=1,
        amount_group=2,
        default_qualifier="standard",
    ),
    _MoneyRule(
        name="shipping_price",
        pattern=re.compile(
            r"\b(standard|expedited|express|overnight|domestic)\s+shipping\s+costs?\s+"
            r"\$?([\d,.]+\d)"
        ),
        claim_type="price",
        base_confidence=0.88,
        key_template="shipping.{qualifier}_price",
        qualifier_group=1,
        amount_group=2,
    ),
    _FlagRule(
        name="no_restocking_fee",
        pattern=re.compile(r"\bwithout\s+a\s+restocking\s+fee\b|\bno\s+restocking\s+fee\b"),
        claim_type="return_policy",
        base_confidence=0.75,
        key_template="returns.restocking_fee",
        flag=False,
    ),
    _PhraseRule(
        name="vague_shipping_speed",
        pattern=re.compile(r"\bship(?:s|ping)?\s+(?:faster|quicker|sooner|same[- ]day)\b"),
        claim_type="shipping_speed",
        base_confidence=0.5,
        key_template="shipping.speed_claim",
    ),
    _PhraseRule(
        name="vague_warranty_extension",
        pattern=re.compile(r"\bextend(?:ed)?\s+(?:the\s+)?(?:coverage|warranty|guarantee)\b"),
        claim_type="warranty",
        base_confidence=0.5,
        key_template="warranty.extension_claim",
    ),
)


class RuleExtractor:
    """The default, offline, deterministic claim extractor.

    Attributes:
        name: identifies this extractor in an :class:`~ingest.extraction.claims.
            ExtractionResult`.
        calls: how many times :meth:`extract` has run. Counted for the same reason the
            model client's calls are: the differential contract is "unchanged content
            does no extraction work", and a claim about zero needs a counter.
    """

    name = "rules"

    #: This extractor calls no model, so an ``ExtractionResult`` it produces reports zero
    #: model calls however many times it ran.
    uses_model = False

    def __init__(self, rules: tuple[PolicyRule, ...] = RULES) -> None:
        self.rules = rules
        self.calls = 0

    def extract(self, text: str, *, url: str = "", kind: str = "") -> list[RawClaim]:
        """Read every atomic claim the rule table finds in ``text``.

        Args:
            text: the page's plain text.
            url: accepted and ignored — the signature is shared with
                :class:`~ingest.extraction.llm_extractor.LLMClaimExtractor`, which passes
                the page's identity to the model as context. A rule reads only the text.
            kind: accepted and ignored, for the same reason.

        Returns:
            Claims in the order the rules declare them, deduplicated on ``(key, value)``
            keeping the most confident reading. Pure: no network, no clock, no model.
        """
        self.calls += 1
        found: dict[tuple[str, Any], RawClaim] = {}
        for start, _end, sentence in sentences(text):
            lowered = sentence.lower()
            discount = hedge_discount(lowered)
            for rule in self.rules:
                for match in rule.pattern.finditer(lowered):
                    read = rule.read(match)
                    if read is None:
                        continue
                    key, value, unit = read
                    claim = RawClaim(
                        key=key,
                        value=value,
                        claim_type=rule.claim_type,
                        confidence=round(rule.base_confidence * discount, 4),
                        span=(start + match.start(), start + match.end()),
                        evidence=sentence,
                        unit=unit,
                    )
                    slot = (claim.key, _hashable(claim.value))
                    previous = found.get(slot)
                    if previous is None or claim.confidence > previous.confidence:
                        found[slot] = claim
        return list(found.values())


def _hashable(value: Any) -> Any:
    """A dict-key-safe view of a claim value."""
    if isinstance(value, list | dict | set):
        return repr(value)
    return value
