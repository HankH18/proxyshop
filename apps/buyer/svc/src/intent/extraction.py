"""Turning buyer utterances into R19's filter/score split (T-071, SPEC R1).

Two extractors run over every buyer turn and their results are merged, in this order of
authority:

1. **The rules in this module** — a small, closed, deterministic lexicon over the buyer's
   literal words. It is authoritative because it can only ever report something the buyer
   actually typed.
2. **The model** (:func:`parse_llm_reply`) — additive only. It may propose a field nobody
   has constrained yet; it may never overwrite one the buyer stated in their own words.

That ordering is the point of the design rather than a performance trick. The buyer is
shown this intent and asked to confirm it, so an intent that contradicts what they typed is
worse than an intent that is merely thin, and a model that "improves" `under $20` into
`under $30` must not be able to.

The lexicon is deliberately small
---------------------------------
It covers the fixture vocabulary and the obvious cross-category words (money, colours,
sizes, materials, a handful of boolean features and preference adjectives) and nothing
else. It is the **offline floor** — what the loop still gets right with ``LLM_PROVIDER``
at D20's default double, with no key and no network — not a pretence at general language
understanding. Its known limits are written down where they live: a colour word is read as
a colour wherever it appears, so "green tea" contributes ``color eq green``; and a phrase
this table does not know contributes nothing at all rather than a guess.

What is NEVER coerced
---------------------
A model-proposed ``op`` outside R19's closed set is **dropped and recorded**, never mapped
onto a neighbouring one. ``lt`` is not ``lte``: coercing it would quietly widen or narrow
what the buyer asked for by one unit of price, and the buyer would confirm a filter nobody
wrote. Pure *spellings* of the same comparison (``<=``, ``equals``, ``one_of``) are
normalised, because those change nothing. The same rule holds for preference directions,
where ``min``/``max`` are spellings and everything unknown is dropped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..profile import coarsen_budget_band
from .errors import InvalidConstraint, InvalidPreference
from .models import BUDGET_BAND_UNSPECIFIED, HardConstraint, Preference

__all__ = [
    "GAP_BUDGET",
    "GAP_CONSTRAINTS",
    "GAP_ORDER",
    "GAP_USE_CASE",
    "BudgetReading",
    "Extraction",
    "IntentDraft",
    "LLMProposal",
    "band_for_amount",
    "extract",
    "parse_llm_reply",
]

# --------------------------------------------------------------------------------------
# the three gaps R1's questions exist to close, in the order they are asked
# --------------------------------------------------------------------------------------

#: The buyer has not said *what* they are shopping for.
GAP_USE_CASE = "use_case"

#: The buyer has not said what they are willing to spend.
GAP_BUDGET = "budget"

#: Nothing but price narrows the search, so every product in the category is eligible.
GAP_CONSTRAINTS = "constraints"

#: Asked in this order. Use case first: a budget for an unknown thing is not information.
GAP_ORDER: tuple[str, ...] = (GAP_USE_CASE, GAP_BUDGET, GAP_CONSTRAINTS)


# --------------------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------------------

_AMOUNT = r"(\d{1,7}(?:\.\d{1,2})?)"

#: ``$20-$50``, ``between $20 and 50``. Anchored on a ``$`` so "size 8 to 10" is not money.
_RANGE_RE = re.compile(rf"(?:between\s+)?\$\s*{_AMOUNT}\s*(?:-|–|—|to|and)\s*\$?\s*{_AMOUNT}")

#: ``under $20``, ``no more than 20``, ``budget of $20``.
_CEILING_RE = re.compile(
    r"(?:under|below|less than|lower than|no more than|not more than|at most|up to|"
    r"cheaper than|within|max|maximum|max of|maximum of|budget of|budget is|spend)"
    rf"\s+\$?\s*{_AMOUNT}"
)

#: ``over $50``, ``at least $50``, ``starting at $50``.
_FLOOR_RE = re.compile(
    rf"(?:over|above|more than|at least|starting at|starting from|from)\s+\$\s*{_AMOUNT}"
)

#: ``around $40`` — a target, not a ceiling. Sets the band and states no filter.
_APPROX_RE = re.compile(
    rf"(?:around|about|roughly|approximately|~|near|close to|ballpark)\s*\$?\s*{_AMOUNT}"
)

#: A bare ``$40``.
_DOLLARS_RE = re.compile(rf"\$\s*{_AMOUNT}")

#: A bare ``40``. Only read as money when the buyer is answering the budget question.
_BARE_NUMBER_RE = re.compile(rf"(?<![\w.]){_AMOUNT}(?![\w.])")

#: Every pattern whose matched span must be blanked before the word lexicon runs, so a
#: price can never be re-read as a size, a quantity or a model number.
_MONEY_SPANS = (_RANGE_RE, _CEILING_RE, _FLOOR_RE, _APPROX_RE, _DOLLARS_RE)


@dataclass(frozen=True)
class BudgetReading:
    """What one utterance said about money."""

    ceiling: float | None = None
    floor: float | None = None
    band: str | None = None

    @property
    def empty(self) -> bool:
        return self.ceiling is None and self.floor is None and self.band is None


def band_for_amount(amount: float) -> str:
    """Snap an amount onto T-070's canonical band vocabulary.

    Delegates to :func:`buyer_svc.profile.coarsen_budget_band` rather than re-deriving the
    thresholds, so ``Intent.budget_band`` and ``BuyerProfile.buckets["budget_band"]``
    cannot disagree about where ``$100`` falls.
    """
    band = coarsen_budget_band({"budget_band": float(amount)})
    return band or BUDGET_BAND_UNSPECIFIED


def read_budget(text: str, *, allow_bare_number: bool = False) -> tuple[BudgetReading, str]:
    """Read money out of ``text``; return the reading and the text with money blanked out.

    ``allow_bare_number`` is on only while the buyer is answering the budget question,
    where a lone "40" is unambiguous. Everywhere else a lone number is a size, a quantity
    or a model number and reading it as dollars is how a clarifier invents a budget.
    """
    ceiling: float | None = None
    floor: float | None = None
    band: str | None = None

    range_match = _RANGE_RE.search(text)
    if range_match:
        low, high = sorted((float(range_match.group(1)), float(range_match.group(2))))
        floor, ceiling = low, high
    else:
        # The ceiling is read FIRST and its span is blanked before the floor is looked
        # for. Measured: "no more than $25 a bag" matched the ceiling pattern on
        # "no more than 25" AND the floor pattern on the "more than $25" inside it, so one
        # sentence produced `price_usd lte 25` and `price_usd gte 25` together — a filter
        # that admits exactly $25.00 and nothing else. Searching the two patterns over the
        # same text independently is what made a negation readable as its own opposite.
        work = text
        ceiling_match = _CEILING_RE.search(work)
        if ceiling_match:
            ceiling = float(ceiling_match.group(1))
            work = _blank(work, [ceiling_match.span()])
        floor_match = _FLOOR_RE.search(work)
        if floor_match:
            floor = float(floor_match.group(1))
        if ceiling is None and floor is None:
            approx = _APPROX_RE.search(text)
            if approx:
                # A target is not a filter: it fixes the band and states no constraint,
                # because "around $40" does not rule out $42.
                band = band_for_amount(float(approx.group(1)))
            else:
                dollars = _DOLLARS_RE.search(text)
                if dollars:
                    ceiling = float(dollars.group(1))

    blanked = _blank_money(text)
    if ceiling is None and floor is None and band is None and allow_bare_number:
        bare = _BARE_NUMBER_RE.search(blanked)
        if bare:
            ceiling = float(bare.group(1))
            blanked = _blank(blanked, [bare.span()])

    if band is None:
        anchor = ceiling if ceiling is not None else floor
        if anchor is not None:
            band = band_for_amount(anchor)
    return BudgetReading(ceiling=ceiling, floor=floor, band=band), blanked


def _blank_money(text: str) -> str:
    spans = [match.span() for pattern in _MONEY_SPANS for match in pattern.finditer(text)]
    return _blank(text, spans)


def _blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for index in range(start, min(end, len(chars))):
            chars[index] = " "
    return "".join(chars)


# --------------------------------------------------------------------------------------
# the word lexicon
# --------------------------------------------------------------------------------------

_KIND_CATEGORY = "category"
_KIND_CONSTRAINT = "constraint"
_KIND_PREFERENCE = "preference"
_KIND_VAGUE = "vague"
_KIND_FILLER = "filler"

_CATEGORIES: dict[str, str] = {
    "coffee": "coffee",
    "coffee beans": "coffee",
    "beans": "coffee",
    "espresso": "coffee",
    "cold brew": "coffee",
    # A roast level or a brew method names the category as surely as the word "coffee"
    # does; a phrase may carry more than one meaning and `_build_phrase_table` keeps both.
    "light roast": "coffee",
    "medium roast": "coffee",
    "dark roast": "coffee",
    "whole bean": "coffee",
    "pour over": "coffee",
    "french press": "coffee",
    "decaf": "coffee",
    "tea": "tea",
    "kettle": "kitchen",
    "mug": "kitchen",
    "shoes": "footwear",
    "sneakers": "footwear",
    "trainers": "footwear",
    "running shoes": "footwear",
    "trail running shoes": "footwear",
    "boots": "footwear",
    "headphones": "audio",
    "earbuds": "audio",
    "speaker": "audio",
    "backpack": "bags",
    "daypack": "bags",
    "luggage": "bags",
    "suitcase": "bags",
    "jacket": "outerwear",
    "coat": "outerwear",
    "hoodie": "outerwear",
    "scarf": "accessories",
    "gloves": "accessories",
    "desk": "furniture",
    "chair": "furniture",
    "lamp": "furniture",
    "keyboard": "computing",
    "monitor": "computing",
    "laptop": "computing",
    "bike": "cycling",
    "bicycle": "cycling",
    "helmet": "cycling",
    "yoga mat": "fitness",
    "dumbbells": "fitness",
}

_CONSTRAINTS: dict[str, tuple[str, str, Any]] = {
    # brew / roast
    "espresso": ("brew_method", "eq", "espresso"),
    "pour over": ("brew_method", "eq", "pour-over"),
    "french press": ("brew_method", "eq", "french-press"),
    "light roast": ("roast_level", "eq", "light"),
    "medium roast": ("roast_level", "eq", "medium"),
    "dark roast": ("roast_level", "eq", "dark"),
    "whole bean": ("grind", "eq", "whole-bean"),
    "pre ground": ("grind", "eq", "ground"),
    "decaf": ("caffeine", "eq", "decaf"),
    "single origin": ("single_origin", "eq", True),
    # boolean features
    "waterproof": ("waterproof", "eq", True),
    "water resistant": ("water_resistant", "eq", True),
    "wireless": ("wireless", "eq", True),
    "noise cancelling": ("noise_cancelling", "eq", True),
    "noise canceling": ("noise_cancelling", "eq", True),
    "machine washable": ("machine_washable", "eq", True),
    "dishwasher safe": ("dishwasher_safe", "eq", True),
    "in stock": ("in_stock", "eq", True),
    "organic": ("organic", "eq", True),
    "fair trade": ("fair_trade", "eq", True),
    "vegan": ("vegan", "eq", True),
    "gluten free": ("gluten_free", "eq", True),
    # materials
    "leather": ("material", "eq", "leather"),
    "cotton": ("material", "eq", "cotton"),
    "wool": ("material", "eq", "wool"),
    "merino": ("material", "eq", "merino"),
    "merino wool": ("material", "eq", "merino-wool"),
    "bamboo": ("material", "eq", "bamboo"),
    "stainless steel": ("material", "eq", "stainless-steel"),
    "titanium": ("material", "eq", "titanium"),
    # colours; see the module docstring for the known "green tea" limit
    "black": ("color", "eq", "black"),
    "white": ("color", "eq", "white"),
    "navy": ("color", "eq", "navy"),
    "grey": ("color", "eq", "grey"),
    "gray": ("color", "eq", "grey"),
    "beige": ("color", "eq", "beige"),
    "olive": ("color", "eq", "olive"),
    "burgundy": ("color", "eq", "burgundy"),
    "teal": ("color", "eq", "teal"),
}

_PREFERENCES: dict[str, tuple[str, str, float]] = {
    "cheap": ("price_usd", "minimize", 1.0),
    "cheapest": ("price_usd", "minimize", 1.0),
    "inexpensive": ("price_usd", "minimize", 1.0),
    "affordable": ("price_usd", "minimize", 1.0),
    "budget friendly": ("price_usd", "minimize", 1.0),
    "good value": ("price_usd", "minimize", 0.8),
    "fast shipping": ("delivery_days", "minimize", 0.8),
    "quick delivery": ("delivery_days", "minimize", 0.8),
    "arrives quickly": ("delivery_days", "minimize", 0.8),
    "recently roasted": ("days_since_roast", "minimize", 0.7),
    "freshly roasted": ("days_since_roast", "minimize", 0.7),
    "highly rated": ("rating", "maximize", 0.6),
    "well reviewed": ("rating", "maximize", 0.6),
    "top rated": ("rating", "maximize", 0.6),
    "durable": ("durability", "maximize", 0.5),
    "long lasting": ("durability", "maximize", 0.5),
    "comfortable": ("comfort", "maximize", 0.5),
    "comfy": ("comfort", "maximize", 0.5),
    "lightweight": ("weight_grams", "minimize", 0.5),
    "quiet": ("noise_db", "minimize", 0.5),
    "sustainable": ("sustainability", "prefer", 0.4),
    "eco friendly": ("sustainability", "prefer", 0.4),
    "recycled": ("sustainability", "prefer", 0.4),
}

#: Words that say "I have not decided yet". Their presence never makes an opener specific.
_VAGUE: frozenset[str] = frozenset(
    {
        "something",
        "anything",
        "some",
        "stuff",
        "thing",
        "things",
        "gift",
        "present",
        "idea",
        "ideas",
        "suggestion",
        "suggestions",
        "recommendation",
        "recommendations",
        "whatever",
        "nice",
        "good",
        "great",
        "cool",
        "fancy",
        "sure",
        "idk",
    }
)

#: Grammar and shopping filler. Carries no product information in either direction.
_FILLER: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "buy",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "get",
        "getting",
        "give",
        "going",
        "has",
        "have",
        "he",
        "hello",
        "help",
        "her",
        "hey",
        "hi",
        "his",
        "how",
        "i",
        "if",
        "im",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "know",
        "like",
        "looking",
        "me",
        "more",
        "much",
        "my",
        "need",
        "needs",
        "new",
        "no",
        "not",
        "of",
        "off",
        "on",
        "one",
        "only",
        "or",
        "our",
        "out",
        "please",
        "maybe",
        "rather",
        "really",
        "right",
        "shopping",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "up",
        "us",
        "very",
        "want",
        "wanted",
        "was",
        "we",
        "well",
        "what",
        "when",
        "which",
        "who",
        "will",
        "with",
        "would",
        "yes",
        "you",
        "your",
    }
)

_SIZE_RE = re.compile(r"\bsize\s+(\d{1,2}(?:\.\d)?)\b")
_REGION_SIZE_RE = re.compile(r"\b(?:us|uk|eu)\s*(\d{1,2}(?:\.\d)?)\b")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Apostrophes are removed rather than split on, so "I'd" is the single token ``id`` and
#: not the pair ``i``/``d`` — a stray one-letter "word" that no filler list can catch and
#: that used to be enough on its own to make a vague opener look specific.
_APOSTROPHES = str.maketrans({"'": "", "\u2019": "", "\u02bc": ""})

#: How long a token must be before it counts as naming a product. Two letters is noise
#: ("id", "ok", "hm"); three is the shortest real product word in the lexicon ("tea").
MIN_CONTENT_TOKEN_CHARS = 3


def _build_phrase_table() -> dict[tuple[str, ...], tuple[tuple[str, Any], ...]]:
    """Merge the four word tables into one phrase-keyed scan table.

    A phrase may legitimately carry more than one meaning — "espresso" names a category
    *and* a brew-method filter — so the value is a tuple of payloads rather than a single
    one. Building this by ``dict`` update instead would silently keep whichever table was
    merged last and lose the other, which is exactly the kind of quiet drop this scan must
    not have.
    """
    table: dict[tuple[str, ...], list[tuple[str, Any]]] = {}

    def add(phrase: str, kind: str, payload: Any) -> None:
        key = tuple(_TOKEN_RE.findall(phrase))
        if not key:
            raise ValueError(f"empty lexicon phrase: {phrase!r}")
        table.setdefault(key, []).append((kind, payload))

    for phrase, category in _CATEGORIES.items():
        add(phrase, _KIND_CATEGORY, category)
    for phrase, spec in _CONSTRAINTS.items():
        add(phrase, _KIND_CONSTRAINT, spec)
    for phrase, pref in _PREFERENCES.items():
        add(phrase, _KIND_PREFERENCE, pref)
    for word in _VAGUE:
        add(word, _KIND_VAGUE, word)
    for word in _FILLER:
        add(word, _KIND_FILLER, word)
    return {key: tuple(value) for key, value in table.items()}


_PHRASES = _build_phrase_table()
_LONGEST_PHRASE = max(len(key) for key in _PHRASES)


@dataclass(frozen=True)
class Extraction:
    """Everything one buyer utterance said, in R19's vocabulary."""

    text: str
    category: str | None = None
    constraints: tuple[HardConstraint, ...] = ()
    preferences: tuple[Preference, ...] = ()
    budget: BudgetReading = field(default_factory=BudgetReading)
    content_tokens: tuple[str, ...] = ()

    @property
    def specific(self) -> bool:
        """Does this utterance name *what* the buyer wants?

        A recognised category settles it. Failing that, two content words that are neither
        filler nor a hedge ("merino base layer") is the floor — one ("shoes") would make
        every stray noun a product and "not sure" a shopping need.
        """
        return self.category is not None or len(self.content_tokens) >= 2


def extract(utterance: str, *, budget_answer: bool = False) -> Extraction:
    """Read one buyer utterance with the deterministic lexicon."""
    text = " ".join(str(utterance).split())
    lowered = text.lower().translate(_APOSTROPHES)
    budget, blanked = read_budget(lowered, allow_bare_number=budget_answer)

    constraints: list[HardConstraint] = []
    preferences: list[Preference] = []
    category: str | None = None

    for match in _SIZE_RE.finditer(blanked):
        constraints.append(HardConstraint(field="size", op="eq", value=float(match.group(1))))
    blanked = _blank(blanked, [m.span() for m in _SIZE_RE.finditer(blanked)])
    for match in _REGION_SIZE_RE.finditer(blanked):
        constraints.append(HardConstraint(field="size", op="eq", value=float(match.group(1))))
    blanked = _blank(blanked, [m.span() for m in _REGION_SIZE_RE.finditer(blanked)])

    tokens = _TOKEN_RE.findall(blanked)
    content: list[str] = []
    index = 0
    while index < len(tokens):
        payloads, width = _match_phrase(tokens, index)
        if payloads is None:
            token = tokens[index]
            if not token.isdigit() and len(token) >= MIN_CONTENT_TOKEN_CHARS:
                content.append(token)
            index += 1
            continue
        for kind, payload in payloads:
            if kind == _KIND_CATEGORY and category is None:
                category = str(payload)
            elif kind == _KIND_CONSTRAINT:
                name, op, value = payload
                constraints.append(HardConstraint(field=name, op=op, value=value))
            elif kind == _KIND_PREFERENCE:
                name, direction, weight = payload
                preferences.append(
                    Preference(field=name, direction=direction, weight=float(weight))
                )
        index += width

    if budget.ceiling is not None:
        constraints.append(HardConstraint(field="price_usd", op="lte", value=budget.ceiling))
    if budget.floor is not None:
        constraints.append(HardConstraint(field="price_usd", op="gte", value=budget.floor))

    return Extraction(
        text=text,
        category=category,
        constraints=tuple(constraints),
        preferences=tuple(preferences),
        budget=budget,
        content_tokens=tuple(content),
    )


def _match_phrase(
    tokens: Sequence[str], index: int
) -> tuple[tuple[tuple[str, Any], ...] | None, int]:
    """Longest phrase in the lexicon starting at ``index``, and how many tokens it ate."""
    for width in range(min(_LONGEST_PHRASE, len(tokens) - index), 0, -1):
        payloads = _PHRASES.get(tuple(tokens[index : index + width]))
        if payloads is not None:
            return payloads, width
    return None, 1


# --------------------------------------------------------------------------------------
# the model's half
# --------------------------------------------------------------------------------------

#: Spellings of a comparison that mean exactly the same thing. Normalising these loses
#: nothing. Everything absent from this map is DROPPED, never mapped onto a neighbour.
_OP_SPELLINGS: dict[str, str] = {
    "eq": "eq",
    "=": "eq",
    "==": "eq",
    "equal": "eq",
    "equals": "eq",
    "is": "eq",
    "lte": "lte",
    "<=": "lte",
    "at_most": "lte",
    "max": "lte",
    "gte": "gte",
    ">=": "gte",
    "at_least": "gte",
    "min": "gte",
    "in": "in",
    "one_of": "in",
    "any_of": "in",
    "contains": "contains",
    "includes": "contains",
    "has": "contains",
}

_DIRECTION_SPELLINGS: dict[str, str] = {
    "maximize": "maximize",
    "maximise": "maximize",
    "max": "maximize",
    "higher": "maximize",
    "more": "maximize",
    "minimize": "minimize",
    "minimise": "minimize",
    "min": "minimize",
    "lower": "minimize",
    "less": "minimize",
    "prefer": "prefer",
    "prefers": "prefer",
    "preferred": "prefer",
    "soft": "prefer",
}

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")

#: Longest reply this module will accept as a bare clarifying question.
MAX_QUESTION_CHARS = 240


@dataclass(frozen=True)
class LLMProposal:
    """What the model offered for one round. Every field is optional and additive."""

    question: str | None = None
    constraints: tuple[HardConstraint, ...] = ()
    preferences: tuple[Preference, ...] = ()
    category: str | None = None
    budget_band: str | None = None
    dropped: tuple[str, ...] = ()
    raw: str = ""

    @property
    def empty(self) -> bool:
        return not (self.question or self.constraints or self.preferences or self.category)


def parse_llm_reply(reply: Any) -> LLMProposal:
    """Read a model reply into an :class:`LLMProposal`; never raise on a bad one.

    Two reply shapes are understood, both offline-safe:

    * a JSON object carrying ``constraints``/``hard_constraints``, ``preferences``,
      ``clarifying_question`` and optionally ``category``/``budget_band`` — the contract
      recorded in ``packages/llm/fixtures/recorded/buyer_intent.json``;
    * a bare line of text **ending in a question mark**, which is read as the clarifying
      question and nothing else.

    Anything else — an empty string, prose, an offline double's ``double:<role>:<hex>``
    marker, a truncated JSON fragment — yields an empty proposal, and the loop falls back
    to its own wording. The "ends in ?" rule is what keeps the deterministic double's
    marker from being shown to a buyer as a question.
    """
    text = reply if isinstance(reply, str) else _stringify(reply)
    text = text.strip()
    if not text:
        return LLMProposal(raw="")

    payload = _load_json_object(text)
    if payload is None:
        if text.endswith("?") and "\n" not in text and len(text) <= MAX_QUESTION_CHARS:
            return LLMProposal(question=text, raw=text)
        return LLMProposal(raw=text)

    dropped: list[str] = []
    constraints = _proposed_constraints(payload, dropped)
    preferences = _proposed_preferences(payload, dropped)
    question = payload.get("clarifying_question") or payload.get("question")
    question_text = " ".join(str(question).split()) if isinstance(question, str) else ""
    return LLMProposal(
        question=question_text[:MAX_QUESTION_CHARS] or None,
        constraints=tuple(constraints),
        preferences=tuple(preferences),
        category=_text_or_none(payload.get("category")),
        budget_band=_text_or_none(payload.get("budget_band")),
        dropped=tuple(dropped),
        raw=text,
    )


def _stringify(reply: Any) -> str:
    if reply is None:
        return ""
    if isinstance(reply, Mapping):
        try:
            return json.dumps(reply)
        except (TypeError, ValueError):
            return ""
    return str(reply)


def _load_json_object(text: str) -> Mapping[str, Any] | None:
    candidate = _FENCE_RE.sub("", text).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        loaded = json.loads(candidate[start : end + 1])
    except (ValueError, RecursionError):
        return None
    return loaded if isinstance(loaded, Mapping) else None


def _text_or_none(value: Any) -> str | None:
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    return text or None


def _proposed_constraints(payload: Mapping[str, Any], dropped: list[str]) -> list[HardConstraint]:
    raw = payload.get("hard_constraints")
    if not isinstance(raw, list):
        raw = payload.get("constraints")
    out: list[HardConstraint] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, Mapping):
            dropped.append(f"constraint is not an object: {item!r}")
            continue
        name = " ".join(str(item.get("field", "")).split())
        op = _OP_SPELLINGS.get(str(item.get("op", "")).strip().lower())
        if op is None:
            dropped.append(
                f"constraint {name or '<unnamed>'} carries op {item.get('op')!r}, which R19 "
                f"cannot express as an eligibility filter; dropped rather than coerced"
            )
            continue
        try:
            out.append(HardConstraint(field=name, op=op, value=item.get("value")))
        except InvalidConstraint as exc:
            dropped.append(str(exc))
    return out


def _proposed_preferences(payload: Mapping[str, Any], dropped: list[str]) -> list[Preference]:
    raw = payload.get("preferences")
    out: list[Preference] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, Mapping):
            dropped.append(f"preference is not an object: {item!r}")
            continue
        name = " ".join(str(item.get("field", "")).split())
        direction = _DIRECTION_SPELLINGS.get(str(item.get("direction", "")).strip().lower())
        if direction is None:
            dropped.append(
                f"preference {name or '<unnamed>'} carries direction "
                f"{item.get('direction')!r}, which R19 does not score; dropped"
            )
            continue
        try:
            out.append(Preference(field=name, direction=direction, weight=item.get("weight", 0.5)))
        except InvalidPreference as exc:
            dropped.append(str(exc))
    return out


# --------------------------------------------------------------------------------------
# the accumulating draft
# --------------------------------------------------------------------------------------


@dataclass
class IntentDraft:
    """Everything the loop knows so far, and what it still needs to ask about.

    The draft is the only mutable thing in this package, and it holds **no** auction
    client, no exchange handle and no HTTP session. There is nothing here that could open
    an auction even by accident, which is half of why R1's "no auction before confirmation"
    holds structurally rather than by review.
    """

    need_utterances: list[str] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    constraints: dict[tuple[str, str], HardConstraint] = field(default_factory=dict)
    preferences: dict[str, Preference] = field(default_factory=dict)
    category: str | None = None
    budget_ceiling: float | None = None
    budget_floor: float | None = None
    budget_band: str | None = None
    specific: bool = False
    dropped: list[str] = field(default_factory=list)

    def absorb(self, utterance: str, *, gap: str | None = None) -> Extraction:
        """Fold one buyer turn into the draft.

        ``gap`` names the question this turn is answering, and it changes two things: a
        bare number is read as money only while answering the budget question, and only a
        turn that describes the *need* joins ``query``. An answer to "what's your budget?"
        is not part of what the buyer is shopping for.
        """
        text = " ".join(str(utterance).split())
        if not text:
            return Extraction(text="")
        self.transcript.append(text)
        if gap is not None:
            self.answers.append(text)
        reading = extract(text, budget_answer=gap == GAP_BUDGET)

        if gap is None or gap == GAP_USE_CASE:
            self.need_utterances.append(text)
        if reading.specific:
            self.specific = True
        if self.category is None and reading.category is not None:
            self.category = reading.category
        for constraint in reading.constraints:
            self._add_constraint(constraint, authoritative=True)
        for preference in reading.preferences:
            self._add_preference(preference, authoritative=True)
        self._note_budget(reading.budget)
        return reading

    def absorb_proposal(self, proposal: LLMProposal) -> None:
        """Fold a model proposal in **additively**; it never overwrites the buyer."""
        self.dropped.extend(proposal.dropped)
        if self.category is None and proposal.category is not None:
            self.category = proposal.category
        for constraint in proposal.constraints:
            self._add_constraint(constraint, authoritative=False)
        for preference in proposal.preferences:
            self._add_preference(preference, authoritative=False)
        if self.budget_band is None and proposal.budget_band:
            self.budget_band = proposal.budget_band

    def _add_constraint(self, constraint: HardConstraint, *, authoritative: bool) -> None:
        existing = self.constraints.get(constraint.key)
        if existing is not None and not authoritative:
            return
        if existing is not None and existing.value == constraint.value:
            return
        self.constraints[constraint.key] = constraint
        if constraint.field == "price_usd" and constraint.op == "lte":
            self._note_budget(BudgetReading(ceiling=_as_float(constraint.value)))
        elif constraint.field == "price_usd" and constraint.op == "gte":
            self._note_budget(BudgetReading(floor=_as_float(constraint.value)))

    def _add_preference(self, preference: Preference, *, authoritative: bool) -> None:
        existing = self.preferences.get(preference.key)
        if existing is not None and not authoritative:
            return
        self.preferences[preference.key] = preference

    def _note_budget(self, reading: BudgetReading) -> None:
        if reading.ceiling is not None:
            self.budget_ceiling = reading.ceiling
        if reading.floor is not None:
            self.budget_floor = reading.floor
        anchor = self.budget_ceiling if self.budget_ceiling is not None else self.budget_floor
        if anchor is not None:
            self.budget_band = band_for_amount(anchor)
        elif reading.band is not None:
            self.budget_band = reading.band

    # -- what is still missing ---------------------------------------------------------

    def gaps(self) -> tuple[str, ...]:
        """Every gap still open, in the order R1's questions close them."""
        open_gaps: list[str] = []
        if not self.specific:
            open_gaps.append(GAP_USE_CASE)
        if self.budget_band is None:
            open_gaps.append(GAP_BUDGET)
        if not self._has_narrowing_constraint():
            open_gaps.append(GAP_CONSTRAINTS)
        return tuple(open_gaps)

    def next_gap(self) -> str | None:
        gaps = self.gaps()
        return gaps[0] if gaps else None

    def _has_narrowing_constraint(self) -> bool:
        """Is anything but price narrowing the search?

        A price ceiling alone leaves every product in the category eligible, which is what
        the third question exists to fix — so a budget must not be allowed to answer it.
        """
        return any(key[0] != "price_usd" for key in self.constraints)

    # -- the finished article ----------------------------------------------------------

    def query(self) -> str:
        """The buyer's own words for what they need. Never a paraphrase."""
        return "; ".join(self.need_utterances)

    def sorted_constraints(self) -> tuple[HardConstraint, ...]:
        return tuple(sorted(self.constraints.values(), key=lambda item: (item.field, item.op)))

    def sorted_preferences(self) -> tuple[Preference, ...]:
        return tuple(sorted(self.preferences.values(), key=lambda item: item.field))


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
