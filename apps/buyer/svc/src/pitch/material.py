"""What the buyer-side agent is allowed to argue from, and nothing else (D55).

This module turns ONE shortlist slot plus ONE shopper's stated need into
:class:`CaseMaterial`: a ranked list of true things the platform already holds about that
candidate. Everything downstream — the prompt, the deterministic assembly, the screen — is
built from this object, so "the buyer-side agent may not introduce a fact the platform has
not checked" is a property of what this function can *reach* rather than a rule somebody
remembered to apply.

Where the material comes from, exhaustively
-------------------------------------------
A :class:`~buyer_svc.accept.labels.LabelledSlot` and nothing else. That object is the
exchange's own published reading of the bid — ``price``, ``commitments`` (each with the
buyer-facing provenance label ``contracts.labels`` publishes for its source),
``trust_summary``, ``provenance_labels`` — and this module reads its fields, renders them,
and stops. There is no catalogue call here, no second look at the bid, no network, no
clock, no RNG. A fact this module cannot find on the slot is a fact the case cannot make.

**The store's own pitch is deliberately not material.** ``Bid.message`` is the sponsored
half: it carries the seller's motive, it is what R18 verifies adversarially, and C10 calls
all pitch content untrusted data that is never an instruction. It is carried to the shopper
verbatim by :mod:`buyer_svc.pitch.writing`, in the store's own voice and labelled as such,
and it never enters this material or the prompt built from it.

Where the buyer-side agent differs from the store-side one, and it is not a detail
----------------------------------------------------------------------------------
``store_agent.runtime.pitch`` forbids its writer to touch price at all — three independent
filters — because a store's advocate that can move price can breach the merchant's approved
floor with prose. **This agent has the opposite job.** The price on a slot is the
exchange's own published number, one of the four axes a shortlist is differentiated on, and
a shopper choosing between four shops is choosing on it. So price, discount and the offer's
expiry are first-class material here. What replaces the store side's money ban is
:func:`~buyer_svc.pitch.writing.screen_reasons`' numeric grounding: every number in a
generated case must already appear in this material, so the writer may lead with the price
and may not invent one.

Identity (R5, rule 3)
---------------------
:data:`PROFILE_BUCKET_KEYS` is an **allowlist**, so the coarse profile is read key by key
rather than dumped: ``pseudonym`` is not on it, and neither is ``email``, ``buyer_id`` or
anything else a caller invents. Everything the posted profile carries outside that allowlist
becomes a :attr:`CaseMaterial.forbidden_tokens` entry — none of them reach the prompt,
because nothing reads them, and the screen refuses any of them in a generated case as well.
A fact whose own rendered text contains one of those strings is dropped here rather than
shown, which is the only way a *slot* could smuggle an identifier onto the buyer's screen.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..accept._reading import read, text

__all__ = [
    "AFFINITY_BONUS",
    "BASE_BY_KIND",
    "BUDGET_BONUS",
    "CONSTRAINT_BONUS",
    "FIRST_TIME_BONUS",
    "FREQUENCY_BONUS",
    "FREQUENT_TIERS",
    "KIND_CAVEAT",
    "KIND_COMMITMENT",
    "KIND_ORDER",
    "KIND_PRICE",
    "KIND_TRUST",
    "MAX_CASE_FACTS",
    "MAX_FACT_VALUE_CHARS",
    "MAX_PROMPT_FACTS",
    "MAX_QUERY_CHARS",
    "MIN_MATCH_TOKEN",
    "PREFERENCE_BASE",
    "PREFERENCE_WEIGHTED",
    "PROFILE_BUCKET_KEYS",
    "QUERY_BONUS",
    "CaseMaterial",
    "PlatformFact",
    "humanised",
    "match_tokens",
    "material_for",
    "numeric_tokens",
    "rank",
    "render_number",
]

# ==============================================================================================
# shapes and limits
# ==============================================================================================

#: A number the exchange published about the offer: the price, the stated discount, the expiry.
KIND_PRICE = "price"
#: A promise the store made, carried as a published ``Claim`` with its own provenance label.
KIND_COMMITMENT = "commitment"
#: The trust snapshot's own numbers. Never re-derived here; R15 makes the served score the
#: only honest one, and a second computation of it would be a second answer.
KIND_TRUST = "trust"
#: A true thing that argues AGAINST this candidate — today, only "few observations so far".
#:
#: It is a kind of its own because of what the buyer-side agent's job is. The agent pitches
#: every candidate as well as it can HONESTLY be pitched, so a caveat must be shown and must
#: not be led with: :data:`BASE_BY_KIND` scores it low enough that it never reaches the
#: assembled case's top three while any real fact exists, and it is still served in
#: :attr:`CaseMaterial.facts` where the shopper reads it. Hiding it would be the platform
#: overselling in its own voice; leading with it would be advocacy nobody asked for.
#: Measured on the served route before this kind existed: a first-time shopper's scraped slot
#: read "Price: 54.00 USD. Also observations: few so far; reliability: 61%." and dropped
#: "100% merino wool" and the shipping window to make room for it.
KIND_CAVEAT = "caveat"

#: Tie-break order between facts that score the same, so a rendering never depends on the
#: order a dict happened to iterate in (offline determinism, rule 1).
KIND_ORDER: Mapping[str, int] = {
    KIND_PRICE: 0,
    KIND_COMMITMENT: 1,
    KIND_TRUST: 2,
    KIND_CAVEAT: 3,
}

#: What a fact is worth before this shopper is considered at all.
BASE_BY_KIND: Mapping[str, float] = {
    KIND_PRICE: 2.0,
    KIND_COMMITMENT: 2.0,
    KIND_TRUST: 1.5,
    KIND_CAVEAT: 0.25,
}

#: The shopper called this out as a MUST-HAVE (R19 hard constraint). Leading with the thing
#: they said would rule an option out is the whole of "which true facts to lead with".
CONSTRAINT_BONUS = 3.0
#: They are weighing it (R19 preference), plus a term scaled by the weight they put on it.
PREFERENCE_BASE = 1.5
PREFERENCE_WEIGHTED = 2.0
#: They stated a budget band, so what it costs is on their mind.
BUDGET_BONUS = 1.0
#: Their own words name this fact.
QUERY_BONUS = 1.0
#: A first-time buyer has no history with anybody, so reliability is what they have.
FIRST_TIME_BONUS = 1.5
#: A frequent buyer has seen the catalogue; value is what moves them.
FREQUENCY_BONUS = 0.75
#: Their coarse category affinity names this fact.
AFFINITY_BONUS = 0.5

#: Coarse frequency tiers that read as "this shopper buys often".
FREQUENT_TIERS: frozenset[str] = frozenset({"frequent", "regular", "high", "weekly"})

#: Shortest token that may match between a fact and an intent. Three-letter tokens match
#: everything and would make every fact look relevant to every shopper.
MIN_MATCH_TOKEN = 4

#: How many facts the assembled case actually says. The rest stay in
#: :attr:`CaseMaterial.facts`, which is served beside the case so a reader can see the copy
#: is a SUBSET of what the platform holds rather than a summary of it.
MAX_CASE_FACTS = 3

#: How many facts the prompt lists. A writer handed fifty facts writes about none of them.
MAX_PROMPT_FACTS = 12

#: Longest rendered fact value. A longer one is DROPPED, never truncated: half a commitment
#: is a different commitment, and this agent may not author one.
MAX_FACT_VALUE_CHARS = 80

#: How much of the shopper's own words reach the prompt. They are DATA there, never quoted.
MAX_QUERY_CHARS = 200

#: The coarse profile facets DESIGN pins on ``BuyerProfile.buckets`` — and the WHOLE of what
#: this module will read out of a posted profile. An allowlist, so a key a caller invents
#: contributes nothing to a prompt because nothing looks it up (R5, rule 3).
PROFILE_BUCKET_KEYS: tuple[str, ...] = (
    "budget_band",
    "category_affinity",
    "frequency_tier",
    "region",
    "first_time",
)

#: A budget band that says nothing. ``buyer_svc.intent.models.BUDGET_BAND_UNSPECIFIED``,
#: spelled rather than imported so this module stays free of the intent package's imports.
BUDGET_BAND_UNSPECIFIED = "unspecified"

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_WORD = re.compile(r"[a-z0-9]+")


def numeric_tokens(value: Any) -> frozenset[str]:
    """Every number in ``value``, normalised so two spellings of one number are one token.

    ``78.00``, ``78.0`` and ``78`` all become ``"78"``; ``09`` becomes ``"9"``. That is what
    lets the screen compare a writer's prose against the material without the comparison
    turning on formatting — a case that says "78 USD" is quoting the ``78.00`` on the slot.
    """
    found: set[str] = set()
    for raw in _NUMBER.findall(str(value)):
        try:
            number = float(raw)
        except ValueError:  # pragma: no cover - the pattern only matches parseable numbers
            continue
        if math.isfinite(number):
            found.add(f"{number:g}")
    return frozenset(found)


def match_tokens(value: Any) -> frozenset[str]:
    """Matchable words in ``value``: lower-cased, at least :data:`MIN_MATCH_TOKEN` long.

    Public because :mod:`buyer_svc.pitch.writing` frames the assembled case on the SAME
    comparison :func:`rank` scored the fact on. Two tokenisers would let the framing claim a
    match ("you said free returns was a must-have") that the score was never given for.
    """
    return frozenset(
        word for word in _WORD.findall(str(value).casefold()) if len(word) >= MIN_MATCH_TOKEN
    )


_tokens = match_tokens


def humanised(key: str) -> str:
    """``free_returns`` -> ``free returns``. The one place a field name becomes English."""
    return str(key).replace("_", " ").strip()


def render_number(value: Any) -> str:
    """A number as a shopper reads it: ``2``, ``9.5``, ``10``. ``""`` when it is not one."""
    if isinstance(value, bool) or value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:g}" if math.isfinite(number) else ""


# ==============================================================================================
# a fact
# ==============================================================================================


@dataclass(frozen=True, slots=True)
class PlatformFact:
    """One true thing the platform already holds about this candidate.

    Attributes:
        key: what the fact is about, in English (``free returns``, ``price``).
        value: the platform's own value for it, rendered.
        kind: :data:`KIND_PRICE`, :data:`KIND_COMMITMENT` or :data:`KIND_TRUST`.
        label: the buyer-facing provenance label for THIS fact, when it has one — a
            commitment is a published ``Claim`` and carries its source's label. ``None`` for
            the exchange's own published fields (price, trust), which are not a store's
            claim and must not borrow a store's badge; the shopper sees ``null`` there and
            the slot's aggregate ``provenance_labels`` beside it.
        score: how strongly this fact speaks to THIS shopper. Emphasis, which D55 says the
            buyer-side agent may choose freely.
    """

    key: str
    value: str
    kind: str
    label: str | None = None
    score: float = 0.0

    @property
    def order(self) -> tuple[float, int, str]:
        """Strongest first, then kind, then key — a total order with no dict iteration in it."""
        return (-self.score, KIND_ORDER.get(self.kind, len(KIND_ORDER)), self.key)

    def phrase(self) -> str:
        """The fact as a clause: ``free returns: yes``."""
        return f"{self.key}: {self.value}"

    def line(self) -> str:
        """The fact as a prompt line, with its evidence where it has any."""
        evidence = f" (evidence: {self.label})" if self.label else ""
        return f"- {self.kind} | {self.key} = {self.value}{evidence}"

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "kind": self.kind, "label": self.label}

    def with_score(self, score: float) -> PlatformFact:
        return PlatformFact(
            key=self.key, value=self.value, kind=self.kind, label=self.label, score=score
        )


@dataclass(frozen=True, slots=True)
class CaseMaterial:
    """Everything the buyer-side agent may argue from for one slot, and nothing else.

    Attributes:
        bid_ref: which slot this is material for. Used for nothing but legibility; it is
            never in a prompt, because it identifies a bid rather than a shopper.
        facts: every fact the platform holds about this candidate, ranked for this shopper.
        query: the shopper's stated need, length-capped. DATA in the prompt, never quoted
            back at them (:func:`~buyer_svc.pitch.writing.screen_reasons`).
        category: the intent's category, when it names one.
        constraint_fields: the R19 hard-constraint fields — what they said would rule an
            option out.
        preference_fields: the R19 preference fields, strongest weight first.
        buckets: the allowlisted coarse profile facets. See :data:`PROFILE_BUCKET_KEYS`.
        forbidden_tokens: every string the posted profile carried OUTSIDE that allowlist.
            None of them reached the prompt; the screen refuses them in the output too.
    """

    bid_ref: str = ""
    facts: tuple[PlatformFact, ...] = ()
    query: str = ""
    category: str = ""
    constraint_fields: tuple[str, ...] = ()
    preference_fields: tuple[str, ...] = ()
    buckets: tuple[tuple[str, str], ...] = ()
    forbidden_tokens: tuple[str, ...] = ()

    @property
    def leading(self) -> tuple[PlatformFact, ...]:
        """The facts the assembled case actually says: the top :data:`MAX_CASE_FACTS`."""
        return self.facts[:MAX_CASE_FACTS]

    @property
    def numbers(self) -> frozenset[str]:
        """Every number the platform holds here — the whole of what a case may quote."""
        found: set[str] = set()
        for fact in self.facts:
            found |= numeric_tokens(fact.value)
        return frozenset(found)

    @property
    def vocabulary(self) -> frozenset[str]:
        """Every matchable word in the material. A case naming none of them is ungrounded."""
        found: set[str] = set()
        for fact in self.facts:
            found |= _tokens(fact.key) | _tokens(fact.value)
        return frozenset(found)


# ==============================================================================================
# reading the slot
# ==============================================================================================


def _money(value: Any, currency: str) -> str:
    """``78.0`` + ``USD`` -> ``78.00 USD``. ``""`` when the amount is not a finite number."""
    if isinstance(value, bool) or value is None:
        return ""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(amount):
        return ""
    rendered = f"{amount:.2f}"
    return f"{rendered} {currency}" if currency else rendered


def _price_facts(price: Any) -> list[PlatformFact]:
    """The exchange's published price, discount and expiry, as facts. ``[]`` when it sent none.

    ``label`` is ``None`` on all three: these are the exchange's own reading of the offer,
    not a store's claim with provenance, and dressing them in a store's badge would tell a
    shopper something had been checked that nobody checked.
    """
    if not isinstance(price, Mapping):
        return []
    currency = text(price.get("currency"))
    unit = _money(price.get("unit_price"), currency)
    total = _money(price.get("total_price"), currency)
    facts: list[PlatformFact] = []
    if total and unit == total:
        facts.append(PlatformFact(key="price", value=total, kind=KIND_PRICE))
    else:
        if unit:
            facts.append(PlatformFact(key="unit price", value=unit, kind=KIND_PRICE))
        if total:
            facts.append(PlatformFact(key="total price", value=total, kind=KIND_PRICE))

    discount = price.get("discount")
    if isinstance(discount, Mapping):
        depth = render_number(discount.get("value"))
        kind = text(discount.get("type")).casefold()
        if depth:
            rendered = f"{depth}% off" if kind in {"percent", "percentage"} else f"{depth} off"
            facts.append(PlatformFact(key="discount", value=rendered, kind=KIND_PRICE))

    expires = text(price.get("expires_at"))
    if expires:
        # The date, not the instant: a shopper reads "held until the 6th", and the seconds
        # would put four more numbers into the material for a writer to quote.
        facts.append(
            PlatformFact(key="offer held until", value=expires.split("T")[0], kind=KIND_PRICE)
        )
    return facts


def _commitment_value(commitment: Any) -> str:
    """One promise as a shopper reads it: ``yes``, ``2 days``, ``30``. ``""`` if unreadable."""
    value = read(commitment, "value", None)
    unit = text(read(commitment, "unit", None))
    if isinstance(value, bool):
        rendered = "yes" if value else "no"
    elif isinstance(value, (int, float)):
        rendered = render_number(value)
    else:
        rendered = text(value)
    if not rendered:
        return ""
    return f"{rendered} {unit}" if unit else rendered


def _commitment_facts(commitments: Any) -> list[PlatformFact]:
    """Each published commitment, keeping the per-claim provenance label the exchange gave it."""
    if commitments is None or isinstance(commitments, (str, bytes)):
        return []
    try:
        rows = list(commitments)
    except TypeError:
        return []
    facts: list[PlatformFact] = []
    for row in rows:
        key = humanised(text(read(row, "key", "")))
        value = _commitment_value(row)
        label = text(read(row, "label", "")) or None
        if key and value:
            facts.append(PlatformFact(key=key, value=value, kind=KIND_COMMITMENT, label=label))
    return facts


def _percentage(value: Any) -> str:
    """A 0..1 trust number as ``86%``.

    A unit conversion of the platform's own number, not a new fact: the raw score is served
    on the slot's ``trust_summary`` in the same response, and a shopper reads a percentage.
    """
    if isinstance(value, bool) or value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return ""
    return f"{number * 100:.0f}%"


def _trust_facts(summary: Any) -> list[PlatformFact]:
    """The trust snapshot's own numbers. Never recomputed here — R15 makes the served one final.

    ``low_data`` is included, and it is the honest half: it is the one fact in the material
    that argues AGAINST the candidate, and a platform-authored case that hid it would be
    overselling with the platform's own voice, which is the failure D55 names.
    """
    if not isinstance(summary, Mapping):
        return []
    facts: list[PlatformFact] = []
    score = _percentage(summary.get("score"))
    if score:
        facts.append(PlatformFact(key="reliability", value=score, kind=KIND_TRUST))
    confidence = _percentage(summary.get("confidence"))
    if confidence:
        facts.append(PlatformFact(key="reliability confidence", value=confidence, kind=KIND_TRUST))
    if summary.get("low_data") is True:
        facts.append(PlatformFact(key="observations", value="few so far", kind=KIND_CAVEAT))
    return facts


def slot_facts(slot: Any) -> list[PlatformFact]:
    """Every fact the platform holds about one labelled slot, unranked.

    ``slot`` is a :class:`~buyer_svc.accept.labels.LabelledSlot` — the exchange's published
    reading, already carrying each commitment's buyer-facing provenance label. Reading the
    labelled object rather than the raw slot is what stops this module deriving a second,
    disagreeing answer about provenance (D30).
    """
    facts = [
        *_price_facts(read(slot, "price", None)),
        *_commitment_facts(read(slot, "commitments", None)),
        *_trust_facts(read(slot, "trust_summary", None)),
    ]
    return [fact for fact in facts if len(fact.value) <= MAX_FACT_VALUE_CHARS]


# ==============================================================================================
# reading the shopper
# ==============================================================================================


def _clamped(value: Any) -> float:
    """A preference weight as a finite 0..1 number. Anything else contributes nothing."""
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _rows(value: Any) -> list[Any]:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _constraint_fields(intent: Any) -> tuple[str, ...]:
    fields = [text(read(row, "field", "")) for row in _rows(read(intent, "hard_constraints", None))]
    return tuple(dict.fromkeys(field for field in fields if field))


def _preference_weights(intent: Any) -> dict[str, float]:
    weights: dict[str, float] = {}
    for row in _rows(read(intent, "preferences", None)):
        field = text(read(row, "field", ""))
        if field:
            weights[field] = max(weights.get(field, 0.0), _clamped(read(row, "weight", 0.0)))
    return weights


def _buckets(profile: Any) -> tuple[tuple[str, str], ...]:
    """The allowlisted coarse facets, read key by key. Never a dump of the posted object.

    Both shapes are accepted: ``GET /buyer/profile``'s ``{"pseudonym", "buckets"}`` and a
    bare buckets object. The pseudonym is not read under either — it is not in
    :data:`PROFILE_BUCKET_KEYS`, so there is no branch that could return it.
    """
    if not isinstance(profile, Mapping):
        return ()
    inner = profile.get("buckets")
    source = inner if isinstance(inner, Mapping) else profile
    rendered: list[tuple[str, str]] = []
    for key in PROFILE_BUCKET_KEYS:
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            shown = "yes" if value else "no"
        elif isinstance(value, (list, tuple)):
            shown = ", ".join(text(item) for item in value if text(item))
        else:
            shown = text(value)
        if shown:
            rendered.append((key, shown))
    return tuple(rendered)


def _forbidden_tokens(profile: Any) -> tuple[str, ...]:
    """Every string the profile carries outside the allowlist, which is to say: identity.

    Walks the whole posted object rather than a known field list, because the point is to
    catch the field nobody thought of — ``buyer_id``, ``session_id``, an email a caller
    tacked on. Strings inside ``buckets`` under an allowlisted key are exempt: those are the
    coarse facets that are *meant* to be usable, and they are already public in a bid request.
    """
    found: list[str] = []

    def walk(node: Any, allowlisted: bool) -> None:
        if isinstance(node, Mapping):
            inner = node.get("buckets")
            for key, value in node.items():
                if key == "buckets" and isinstance(inner, Mapping):
                    walk(value, False)
                    continue
                walk(value, allowlisted or str(key) in PROFILE_BUCKET_KEYS)
            return
        if isinstance(node, (list, tuple, set, frozenset)):
            for item in node:
                walk(item, allowlisted)
            return
        if allowlisted or isinstance(node, (bool, int, float)) or node is None:
            return
        rendered = text(node)
        # Two characters or fewer cannot identify anybody and would make the screen refuse
        # honest prose that happened to contain them.
        if len(rendered) > 2 and rendered not in found:
            found.append(rendered)

    walk(profile, False)
    return tuple(found)


# ==============================================================================================
# ranking — emphasis, which is the part D55 leaves free
# ==============================================================================================


def rank(
    facts: Sequence[PlatformFact],
    *,
    query: str = "",
    category: str = "",
    constraint_fields: Sequence[str] = (),
    preference_weights: Mapping[str, float] | None = None,
    buckets: Sequence[tuple[str, str]] = (),
    budget_band: str = "",
) -> tuple[PlatformFact, ...]:
    """Score every fact for THIS shopper and return them strongest first.

    A pure function of its arguments — no clock, no environment, no randomness — so the same
    shopper and the same slot produce the same order in every process (rule 1). Ties break on
    ``(kind, key)`` rather than on input order, because a case whose lead fact depended on the
    order a mapping iterated in would be reproducible only by accident.
    """
    weights = dict(preference_weights or {})
    query_tokens = _tokens(query) | _tokens(category)
    constraint_tokens = {field: _tokens(field) for field in constraint_fields}
    preference_tokens = {field: _tokens(field) for field in weights}
    facets = dict(buckets)
    affinity_tokens = _tokens(facets.get("category_affinity", ""))
    first_time = facets.get("first_time") == "yes"
    frequent = facets.get("frequency_tier", "").casefold() in FREQUENT_TIERS
    stated_budget = bool(budget_band) and budget_band != BUDGET_BAND_UNSPECIFIED

    scored: list[PlatformFact] = []
    for fact in facts:
        tokens = _tokens(fact.key)
        score = BASE_BY_KIND.get(fact.kind, 1.0)
        if any(tokens & needle for needle in constraint_tokens.values() if needle):
            score += CONSTRAINT_BONUS
        for field, needle in preference_tokens.items():
            if needle and tokens & needle:
                score += PREFERENCE_BASE + PREFERENCE_WEIGHTED * weights.get(field, 0.0)
        if query_tokens and tokens & query_tokens:
            score += QUERY_BONUS
        if affinity_tokens and tokens & affinity_tokens:
            score += AFFINITY_BONUS
        if fact.kind == KIND_PRICE and stated_budget:
            score += BUDGET_BONUS
        if fact.kind == KIND_PRICE and frequent:
            score += FREQUENCY_BONUS
        if fact.kind == KIND_TRUST and first_time:
            score += FIRST_TIME_BONUS
        scored.append(fact.with_score(score))
    return tuple(sorted(scored, key=lambda fact: fact.order))


# ==============================================================================================
# the entry point
# ==============================================================================================


def material_for(slot: Any, *, intent: Any = None, profile: Any = None) -> CaseMaterial:
    """Everything the buyer-side agent may say about one slot to one shopper.

    Args:
        slot: a :class:`~buyer_svc.accept.labels.LabelledSlot` (or anything shaped like one).
            The WHOLE of the factual material; nothing else is read.
        intent: the confirmed structured intent, as ``Intent.to_dict()`` spells it. Optional
            — with none, the facts are ranked by kind alone and the case is the platform's
            unconditioned reading, which is still the organic result.
        profile: the coarsened profile (``{"pseudonym", "buckets"}`` or a bare buckets
            object). Only :data:`PROFILE_BUCKET_KEYS` are read out of it.

    Returns:
        A :class:`CaseMaterial` whose ``facts`` are ranked for this shopper, and which
        carries no string the profile supplied outside the allowlist.
    """
    forbidden = _forbidden_tokens(profile)
    folded = [token.casefold() for token in forbidden]
    facts = [
        fact
        for fact in slot_facts(slot)
        if not any(token in f"{fact.key} {fact.value}".casefold() for token in folded)
    ]
    weights = _preference_weights(intent)
    buckets = _buckets(profile)
    query = text(read(intent, "query", ""))[:MAX_QUERY_CHARS]
    category = text(read(intent, "category", ""))
    constraints = _constraint_fields(intent)
    return CaseMaterial(
        bid_ref=text(read(slot, "bid_ref", "")),
        facts=rank(
            facts,
            query=query,
            category=category,
            constraint_fields=constraints,
            preference_weights=weights,
            buckets=buckets,
            budget_band=text(read(intent, "budget_band", "")),
        ),
        query=query,
        category=category,
        constraint_fields=constraints,
        preference_fields=tuple(sorted(weights, key=lambda field: (-weights[field], field))),
        buckets=buckets,
        forbidden_tokens=forbidden,
    )
