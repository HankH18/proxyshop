"""Pitch decomposition: a seller's prose into atomic, span-addressed ``seller_asserted`` claims.

R18 / D55. The exchange checks the SPONSORED side adversarially because that is the side
carrying the seller's motive, and until this module the sponsored side's actual artefact — the
purchased message a store's dedicated advocate writes for this shopper — was checked by
nothing. ``Bid.message`` was documented "never a source of claims", :func:`claim_verification.
verify` takes pre-decomposed claims, and ``exchange.ranking.verification`` handed the verifier
``"text": ""``. So a bidder submitted the structured ``claims`` it had selected for itself and
its prose was dropped before ranking: the one bidder class the spec singles out for scrutiny
was graded only on its own self-selected assertions.

This module is the missing first half. The second half — the one that actually turns prose into
evidence — is the comparison, and it is deliberately NOT here: see "why extraction is not
verification" below.

Reused, not rewritten
---------------------
The decomposition ENGINE is ``ingest.extraction.rules``' (T-021) — sentence splitting with
character offsets, the hedge discount, a rule's shape, and the scan-and-dedupe loop — and this
module supplies only a rule TABLE and a provenance stamp on top of it. Writing a second
decomposer would be the duplication this repository keeps finding.

The engine itself now lives in :mod:`claim_verification.decomposition`, one package DOWN from
both users, and it moved there because the first version of this module imported it straight
out of ``ingest`` and **that broke two shipped images** — the trust image ships no ``ingest``
at all and the exchange image ships only ``ingest/graph`` and ``ingest/embeddings``. Read that
module's docstring for the measurement and for the one-file follow-up that makes
``ingest.extraction.rules`` import the engine instead of defining it. Until that lands the two
definitions are held together by
``test_pitch.py::test_the_shared_engine_and_the_ingest_rule_engine_do_not_drift``.

What differs from the policy-page path, and each difference is load-bearing:

**The provenance is ``seller_asserted``, and it is stamped HERE.** ``ingest.extraction`` stamps
``scraped``, which means "the platform observed this on the store's site". A pitch is the
platform observing that the seller SAID something, which is a different fact and the one D55's
whole asymmetry rests on. Stamping ``scraped`` on a seller's assertion would launder it into an
observation the platform made and vouches for — precisely the substitution the organic side
declines to make. ``seller_asserted`` is also the only source
``contracts.NON_HOOK_PROVENANCE_SOURCES`` lets an unharnessed seller assert, so an external
bid's decomposed prose carries the same provenance its structured claims do.

**The key a reading lands on is the PLATFORM's decision.** A rule proposes a canonical key and
:data:`KEY_ALIASES` gives the other spellings the same fact goes by; :func:`decompose_pitch`
resolves that list against the ``vocabulary`` its caller passes, which is
:func:`claim_verification.verifier.catalog_keys` over the exchange's own snapshot. A seller
therefore cannot choose which catalogue field its prose is graded against — the same lever
``exchange.ranking.verification.STORE_SUPPLIED_FIELDS_DROPPED`` closes one field over for
``product_ref``. When nothing in the vocabulary matches, the canonical key is kept and the
exchange answers "this catalogue could never decide that", which costs the seller nothing.

**A hedge is not a commitment.** The shared engine's hedge discount multiplies a reading's
confidence by :data:`~claim_verification.decomposition.HEDGE_DISCOUNT` when the sentence
hedges, and anything landing under :data:`PITCH_CONFIDENCE_FLOOR` (C10's published floor) is
dropped rather than quarantined. "We may occasionally include a two-year warranty" is marketing; grading it
would let an honest seller be contradicted for a sentence that promised nothing. **Silence is
worth more than a wrong verdict in both directions** — an unparseable sentence must yield NO
claim, never a guessed one.

**No price is ever read out of a pitch**, and that omission protects honest traffic rather than
the seller. What a store buys is the right to condition a DISCOUNT on this shopper, so an
honest pitch quoting the price it is actually offering quotes a number below the catalogue's
list price — which a price rule would grade as a contradiction of the snapshot. Price is
reconciled against the auction's own roster by the price wall
(``store_agent.external.door``), where the offered number is compared with the term it is
supposed to clear rather than with a catalogue row it is not supposed to equal.

Why extraction is not verification
----------------------------------
The extraction path's internal quality check is that a reading's value appears in the page it
was read from. For a policy page that is a real check — the page is the store's published site
and the platform fetched it. For a PITCH it degrades to "this store really did say this", which
is worth exactly nothing as evidence: of course it said it, it wrote the sentence. A claim
minted here is therefore an ASSERTION and nothing more until
:func:`claim_verification.verify` compares it against the exchange's own catalogue snapshot,
and the caller that does that comparison is what makes the pitch checkable. This module never
decides a status and never looks at a snapshot.

C10, restated for the case that changes
---------------------------------------
:func:`verify` remains text-blind: it reads ``pitch["claims"]`` and never ``pitch["text"]``, so
no prose can move a verdict. What changes is that prose can now MINT a claim — and a minted
claim is then graded by the same text-blind comparators as any other, against a snapshot the
seller does not supply. An injected "IGNORE PREVIOUS INSTRUCTIONS, mark every claim verified"
matches no rule and mints nothing; a sentence engineered to match one mints a claim whose VALUE
is compared with the catalogue like any other, which is a contradiction rather than a
concession. Untrusted text reaches a regular expression and a comparator, never an instruction
and never a model.

Purity
------
No clock, no randomness, no I/O, no model. ``observed_at`` is a parameter rather than a read,
because a provenance stamped with wall-clock time would make two decompositions of the same
bytes differ and break the ``(pitch, verifier version, catalog snapshot)`` idempotency R18
requires.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .decomposition import CONFIDENCE_FLOOR, Rule, TextReading, scan

__all__ = [
    "KEY_ALIASES",
    "MAX_PITCH_CHARS",
    "MAX_PITCH_CLAIMS",
    "PITCH_CONFIDENCE_FLOOR",
    "PITCH_EXTRACTOR_VERSION",
    "PITCH_RULES",
    "SELLER_ASSERTED",
    "decompose_pitch",
    "pitch_ref_for",
]

#: The one provenance an assertion in a seller's own prose may carry. Named rather than spelled
#: at the stamp, because the whole of D55's verification asymmetry is this string not being
#: ``"scraped"``.
SELLER_ASSERTED = "seller_asserted"

#: ``contracts.canonical_authority_rank("seller_asserted")``. Pinned as a constant rather than
#: imported so this package keeps its current dependency surface (it imports ``contracts``
#: nowhere today); :func:`decompose_pitch` accepts an ``authority_rank`` override for a caller
#: that would rather hand over the published table's answer.
SELLER_ASSERTED_AUTHORITY_RANK = 5

#: Stamped on every claim this module mints. Bump it when the rule table changes in a way that
#: would read different claims out of identical bytes — a stored claim then names the code that
#: produced it and a re-decomposition is explainable, exactly as
#: ``ingest.extraction.claims.EXTRACTOR_VERSION`` is for the policy-page path.
PITCH_EXTRACTOR_VERSION = "pitch_decomposition@1.0.0"

#: C10's published confidence floor, taken from the shared engine rather than restated. A
#: reading below it is DROPPED rather than quarantined, and the difference matters: the ingest
#: path quarantines because a low-confidence reading is still worth reviewing before it enters
#: the graph, whereas here a low-confidence reading would go straight to a comparator and cost
#: an honest seller a contradiction for a sentence that hedged.
PITCH_CONFIDENCE_FLOOR = CONFIDENCE_FLOOR

#: The longest pitch this module will read. Both bounds below exist because the length of the
#: prose and the number of claims it mints are the BIDDER's choice: ``exchange.composition.
#: MAX_BID_RESPONSE_BYTES`` is 256 KiB and the external door admits a 100 KB ``message``
#: verbatim (truncating it would change what was signed). Longer text is decomposed to NOTHING
#: rather than truncated — a half-read sentence is exactly the "guessed claim" this module
#: refuses to mint, and a seller with 20,000 characters to say has said enough by then.
MAX_PITCH_CHARS = 20_000

#: The most claims one pitch may mint. The rule scan is linear in the text, but a pitch listing
#: ten thousand distinct pump pressures would mint ten thousand claims and each one costs a
#: comparator run, a MAC and a ledger row on the request path. Claims past the cap are simply
#: not made, which is the same as the seller not having written them.
MAX_PITCH_CLAIMS = 64

_SPELLED_NUMBERS: Mapping[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "eighteen": 18,
    "twenty-four": 24,
    "thirty-six": 36,
}

_NUMBER_WORD = "|".join(_SPELLED_NUMBERS)


def _amount(raw: Any) -> float | None:
    """A captured number, in digits or in words, or ``None`` when it is neither.

    ``None`` rather than a raise, and this is the parser half of "silence beats a wrong
    verdict". A rule that raises takes the WHOLE decomposition down with it, and the caller of
    a decomposition is a live bid path: one unparseable capture in one sentence would cost an
    honest seller its entire pitch, or — worse, on a path that catches broadly — cost every
    seller in the auction theirs. Measured: an alternation whose second branch captured the
    number left ``match.group(1)`` as ``None``, and ``float(None)`` raised out of
    ``RuleExtractor.extract`` through every frame above it.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower().replace(",", "")
    if text in _SPELLED_NUMBERS:
        return float(_SPELLED_NUMBERS[text])
    try:
        return float(text)
    except ValueError:
        return None


def _captured(match: re.Match[str], index: int, default: str = "") -> str:
    """The declared capture group, falling back to the first branch that actually captured.

    A pattern spelling one fact two ways (``only 3 left`` | ``just 3 left``) has one numbered
    group per branch, and only the branch that matched captured anything. Naming a fixed index
    reads ``None`` from the other branch; walking the groups reads the number that is there.
    ``IndexError`` is caught because a group index is a rule-table statement, and a mistake in
    it must degrade to "this rule read nothing" rather than take down the whole pitch.
    """
    try:
        declared = match.group(index)
    except IndexError:  # pragma: no cover - only a mis-declared rule reaches this
        declared = None
    if declared is not None:
        return str(declared)
    found = next((group for group in match.groups() if group is not None), None)
    return default if found is None else str(found)


@dataclass(frozen=True)
class _DurationRule(Rule):
    """A term quoted in years or months, normalised to whole months."""

    key_template: str = ""
    amount_group: int = 1
    unit_group: int = 2

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        amount = _amount(_captured(match, self.amount_group))
        if amount is None:
            return None
        word = (_captured(match, self.unit_group, "month") or "month").strip().rstrip("s")
        months = amount * 12.0 if word.startswith("year") else amount
        return (self.key_template, int(months), "months")


@dataclass(frozen=True)
class _DaysRule(Rule):
    """A window quoted in days, business days or weeks, normalised to days."""

    key_template: str = ""
    amount_group: int = 1
    unit_group: int = 2

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        amount = _amount(_captured(match, self.amount_group))
        if amount is None:
            return None
        word = (_captured(match, self.unit_group, "day") or "day").strip().rstrip("s")
        business = word.startswith("business")
        days = amount * 7.0 if word.startswith("week") else amount
        return (self.key_template, int(days), "business_days" if business else "days")


@dataclass(frozen=True)
class _QuantityRule(Rule):
    """A bare measured quantity — a pressure, a capacity, a voltage, a count."""

    key_template: str = ""
    amount_group: int = 1
    integral: bool = False

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        amount = _amount(_captured(match, self.amount_group))
        if amount is None:
            return None
        return (self.key_template, int(amount) if self.integral else amount, self.unit)


@dataclass(frozen=True)
class _FlagRule(Rule):
    """A boolean the sentence states outright — "in stock", "sold out"."""

    key_template: str = ""
    flag: bool = True

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        return (self.key_template, self.flag, None)


@dataclass(frozen=True)
class _VocabularyRule(Rule):
    """A named value out of a closed vocabulary, normalised to its catalogue spelling.

    The normalisation is the point rather than tidiness. A catalogue records ``"heat
    exchange"``; a seller writes "heat exchanger" or "heat-exchange", and the comparator's
    string family is casefolded whitespace-collapsed EQUALITY, so an un-normalised reading of
    an honest sentence comes back ``contradicted``. Spelling is not a lie.
    """

    key_template: str = ""
    canonical: Mapping[str, str] | None = None

    def read(self, match: re.Match[str]) -> tuple[str, Any, str | None] | None:
        raw = re.sub(r"[\s-]+", " ", match.group(0).strip().lower())
        table = self.canonical or {}
        return (self.key_template, table.get(raw, raw), None)


#: The rule table. Every ``claim_type`` is a term from DESIGN's published vocabulary (the copy
#: ``ingest.extraction.rules.CLAIM_TYPES`` carries), because an unpublished one has no route
#: into a trust dimension and ``trust.scoring.claim_dimension`` raises on it (D53).
#:
#: The keys are drawn from the catalogue vocabulary this repository's own snapshots carry
#: (``e2e/support/s1/flow.py``, ``apps/buyer/devstack/run.py``) and from the fields
#: :data:`claim_verification.FIELD_TOLERANCES` already publishes a tolerance for. A rule whose
#: key no catalogue here carries is not useless — :data:`KEY_ALIASES` lets another deployment's
#: spelling win — but it decides nothing on this one, and deciding nothing costs the seller
#: nothing.
PITCH_RULES: tuple[Rule, ...] = (
    _DurationRule(
        name="warranty_term",
        pattern=re.compile(
            rf"\b(\d+|{_NUMBER_WORD})[\s-]*(year|month)s?\b[^.;!?]{{0,40}}?"
            r"\b(?:warranty|guarantee|guaranteed|cover|coverage)\b"
        ),
        claim_type="warranty",
        base_confidence=0.9,
        key_template="warranty_months",
    ),
    _DurationRule(
        name="warranty_term_trailing",
        pattern=re.compile(
            rf"\b(?:warranty|guarantee|coverage)\b[^.;!?]{{0,20}}?\b(\d+|{_NUMBER_WORD})"
            r"[\s-]*(year|month)s?\b"
        ),
        claim_type="warranty",
        base_confidence=0.9,
        key_template="warranty_months",
    ),
    _VocabularyRule(
        name="boiler_type",
        pattern=re.compile(
            r"\b(?:heat[\s-]?exchanger?|dual[\s-]?boiler|single[\s-]?boiler|thermo[\s-]?block)\b"
        ),
        claim_type="specifications",
        base_confidence=0.88,
        key_template="boiler_type",
        canonical={
            "heat exchange": "heat exchange",
            "heat exchanger": "heat exchange",
            "heatexchange": "heat exchange",
            "heatexchanger": "heat exchange",
            "dual boiler": "dual boiler",
            "dualboiler": "dual boiler",
            "single boiler": "single boiler",
            "singleboiler": "single boiler",
            "thermo block": "thermoblock",
            "thermoblock": "thermoblock",
        },
    ),
    _QuantityRule(
        name="pump_pressure",
        pattern=re.compile(r"\b(\d+(?:\.\d+)?)\s*[- ]?bars?\b"),
        claim_type="specifications",
        base_confidence=0.88,
        key_template="pump_pressure_bar",
        unit="bar",
    ),
    _QuantityRule(
        name="water_tank",
        pattern=re.compile(
            r"\b(\d+(?:\.\d+)?)\s*(?:l|litres?|liters?)\b[^.;!?]{0,20}?\b(?:tank|reservoir)\b"
        ),
        claim_type="specifications",
        base_confidence=0.85,
        key_template="water_tank_l",
        unit="L",
    ),
    _QuantityRule(
        name="mains_voltage",
        pattern=re.compile(r"\b(\d{2,3})\s*(?:v|volts?)\b"),
        claim_type="specifications",
        base_confidence=0.8,
        key_template="voltage",
        unit="V",
    ),
    _QuantityRule(
        name="units_left",
        pattern=re.compile(
            r"\bonly\s+(\d+)\s+(?:left|remaining|units?\s+(?:left|remaining))\b"
            r"|\b(?:just|still)\s+(\d+)\s+(?:left|remaining)\b"
        ),
        claim_type="specifications",
        base_confidence=0.8,
        key_template="units_left",
        integral=True,
    ),
    _FlagRule(
        name="in_stock",
        pattern=re.compile(r"\bin\s+stock\b|\bavailable\s+now\b|\bready\s+to\s+ship\b"),
        claim_type="specifications",
        base_confidence=0.82,
        key_template="in_stock",
        flag=True,
    ),
    _FlagRule(
        name="out_of_stock",
        pattern=re.compile(r"\bout\s+of\s+stock\b|\bsold\s+out\b|\bback[\s-]?ordered\b"),
        claim_type="specifications",
        base_confidence=0.82,
        key_template="in_stock",
        flag=False,
    ),
    _DaysRule(
        name="dispatch_window",
        pattern=re.compile(
            r"\b(?:ship|ships|shipped|shipping|dispatch|dispatches|dispatched)\b"
            r"[^.;!?]{0,40}?\bwithin\s+(\d+)\s+(business\s+day|day|week)s?\b"
        ),
        claim_type="dispatch_window",
        base_confidence=0.9,
        key_template="dispatch_window",
    ),
    _DaysRule(
        name="return_window",
        pattern=re.compile(
            r"\breturns?\b[^.;!?]{0,40}?\b(?:within|for|up\s+to)\s+(\d+)\s+"
            r"(business\s+day|day|week|month)s?\b"
        ),
        claim_type="return_policy",
        base_confidence=0.9,
        key_template="return_window_days",
    ),
)

#: Other spellings the SAME fact goes by, in the order a catalogue's own vocabulary is searched.
#:
#: The canonical key comes first, so a deployment whose snapshot uses it is unaffected. What the
#: rest buy is a deployment whose catalogue spells the fact differently — that store's honest
#: prose lands on a key its own catalogue can decide instead of on one nothing carries.
#:
#: **``free_returns`` is deliberately NOT an alias of ``return_window_days``**, and the omission
#: was measured on this repository's own S1 fixture. That snapshot records ``free_returns`` as
#: the STRING ``"30 return window"``; a reading of "free returns for 30 days" is the number 30,
#: and the comparator's string family would call an honest seller ``contradicted`` over a
#: spelling. A key whose recorded value is prose is a key this decomposer must leave alone.
KEY_ALIASES: Mapping[str, tuple[str, ...]] = {
    "warranty_months": ("warranty_months", "warranty", "warranty_duration_months"),
    "boiler_type": ("boiler_type", "boiler"),
    "pump_pressure_bar": ("pump_pressure_bar", "pump_pressure"),
    "water_tank_l": ("water_tank_l", "water_tank_litres", "tank_capacity_l"),
    "voltage": ("voltage", "mains_voltage"),
    "units_left": ("units_left", "inventory_units"),
    "in_stock": ("in_stock",),
    "dispatch_window": ("dispatch_window", "dispatch_window_days", "shipping_speed"),
    "return_window_days": ("return_window_days", "return_policy", "returns_window_days"),
}


def pitch_ref_for(store_id: Any, auction_id: Any = None) -> str:
    """This exchange's reference for one pitch: ``pitch:{auction}:{store}``.

    Minted from the two identifiers the PLATFORM owns rather than read off the bid, for the
    reason ``exchange.ranking.verification.claim_ref_for`` gives about ``claim_ref``: a
    reference the counterparty chose is a reference two claims can share.
    """
    auction = str(auction_id or "").strip() or "auction"
    return f"pitch:{auction}:{store_id}"


def _resolve_key(key: str, vocabulary: Iterable[str]) -> str:
    """The spelling THIS catalogue uses for the fact ``key`` names, else ``key`` itself."""
    known = frozenset(str(name) for name in vocabulary or ())
    if not known:
        return key
    for candidate in KEY_ALIASES.get(key, (key,)):
        if candidate in known:
            return candidate
    return key


def _claim(
    raw: TextReading,
    *,
    key: str,
    pitch_ref: str,
    observed_at: Any,
    authority_rank: int,
) -> dict[str, Any]:
    """One reading as the ``contracts.Claim`` shape the exchange's ranker already reads."""
    provenance: dict[str, Any] = {
        "source": SELLER_ASSERTED,
        "ref": f"{pitch_ref}#{key}",
        "authority_rank": int(authority_rank),
        # Not part of ``contracts.Provenance``; carried so an auditor reading a verdict can
        # tell WHICH rule generation minted the claim it was decided on, the same statement
        # ``ingest.extraction.claims.EXTRACTOR_VERSION`` makes on the policy path.
        "extractor_version": PITCH_EXTRACTOR_VERSION,
    }
    if observed_at is not None:
        provenance["observed_at"] = str(observed_at)
    return {
        "key": key,
        "claim_type": raw.claim_type,
        "value": raw.value,
        "unit": raw.unit,
        "confidence": float(raw.confidence),
        "evidence": raw.evidence,
        "source_span": {
            "pitch_ref": pitch_ref,
            "start": int(raw.span[0]),
            "end": int(raw.span[1]),
        },
        "provenance": provenance,
    }


def decompose_pitch(
    text: Any,
    *,
    store_id: Any = "",
    auction_id: Any = None,
    pitch_ref: str | None = None,
    vocabulary: Iterable[str] = (),
    observed_at: Any = None,
    authority_rank: int = SELLER_ASSERTED_AUTHORITY_RANK,
    confidence_floor: float = PITCH_CONFIDENCE_FLOOR,
    max_claims: int = MAX_PITCH_CLAIMS,
    max_chars: int = MAX_PITCH_CHARS,
) -> list[dict[str, Any]]:
    """The atomic claims a seller's prose asserts, stamped ``seller_asserted``.

    Args:
        text: the pitch, exactly as the seller wrote it. Anything that is not a usable string
            — ``None``, a number, a mapping, prose longer than ``max_chars`` — decomposes to
            nothing. A pitch this module cannot read is a pitch that asserted nothing, which
            is the direction that costs an honest seller least.
        store_id: whose pitch it is. Used only to address the claims.
        auction_id: which auction it was written for, likewise.
        pitch_ref: the reference every span and provenance hangs off. Defaults to
            :func:`pitch_ref_for`.
        vocabulary: the keys the grader's OWN catalogue can decide — pass
            :func:`claim_verification.verifier.catalog_keys`. See :data:`KEY_ALIASES`.
        observed_at: the instant to stamp on the provenance. Omitted from the stamp when
            ``None``; never read from a clock, because that would make two decompositions of
            the same bytes differ (R18 acc 2).
        authority_rank: the published rank for ``seller_asserted``.
        confidence_floor: readings strictly below it are dropped — a hedge is not a commitment.
        max_claims, max_chars: the two bidder-chosen factors, bounded. See their constants.

    Returns:
        Claims in the order they appear in the text, each in the ``contracts.Claim`` shape the
        exchange's ranker already reads, each carrying a ``source_span`` that indexes ``text``.
        **Never a status**: this module asserts, it does not decide. Only comparison against a
        catalogue snapshot the seller did not supply turns one of these into evidence.
    """
    if not isinstance(text, str):
        return []
    prose = text.strip()
    if not prose or len(text) > int(max_chars):
        return []

    ref = pitch_ref or pitch_ref_for(store_id, auction_id)
    readings = [
        reading
        for reading in scan(text, PITCH_RULES)
        if float(reading.confidence) >= float(confidence_floor)
    ]
    # Text order, then key, so the emitted list is a stable function of the bytes rather than of
    # the rule table's declaration order — two rules matching the same sentence must not be able
    # to swap places when the table is edited.
    readings.sort(key=lambda reading: (int(reading.span[0]), str(reading.key)))

    claims: list[dict[str, Any]] = []
    for reading in readings[: max(0, int(max_claims))]:
        claims.append(
            _claim(
                reading,
                key=_resolve_key(str(reading.key), vocabulary),
                pitch_ref=ref,
                observed_at=observed_at,
                authority_rank=authority_rank,
            )
        )
    return claims
