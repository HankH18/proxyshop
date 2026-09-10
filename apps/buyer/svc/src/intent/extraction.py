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

from contracts.ranking import preference_term_conflict

from ..profile import coarsen_budget_band
from .errors import InvalidConstraint, InvalidPreference
from .models import BUDGET_BAND_UNSPECIFIED, HardConstraint, Preference

__all__ = [
    "GAP_BUDGET",
    "GAP_CONSTRAINTS",
    "GAP_ORDER",
    "GAP_USE_CASE",
    "VOUCHED_FILTER_FIELDS",
    "BudgetReading",
    "Extraction",
    "GapAnswer",
    "IntentDraft",
    "LLMProposal",
    "SoftenedReading",
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

#: An amount written in digits, with or without thousands separators.
#:
#: The grouped alternative comes FIRST and is not optional decoration. Without it the
#: pattern matched the leading group and stopped at the comma, and every caller took that
#: prefix as the whole amount — measured, on the served route: "a sofa under $1,200"
#: produced ``price_usd lte 1.0`` and ``budget_band "0-50"``, an eligibility filter one
#: twelve-hundredth of what the shopper said. Four figures is exactly where a furniture
#: budget lives, and a comma is how people write four figures.
_DIGIT_AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{1,7}(?:\.\d{1,2})?"

#: English number words, and only these. A shopper says "about five hundred dollars" at
#: least as often as "$500" — measured: the S1 run fixture's own shopper says exactly that,
#: and every digit-only pattern in this module read it as *nothing*, so the clarifier
#: answered ``budget_band: "unspecified"`` and then spent all THREE of R1's questions asking
#: for a number it had already been given.
#:
#: The table is closed on purpose, like the rest of this lexicon. What it covers is written
#: down beside :func:`read_budget`; what it does not is written down there too.
_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
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
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

#: The multipliers. ``hundred`` scales what is being accumulated; ``thousand`` banks it.
_SCALE_WORDS: dict[str, int] = {"hundred": 100, "thousand": 1000}

#: Longest-first so ``nineteen`` is never matched as ``nine`` with a stray ``teen`` left
#: behind — the classic way a number-word alternation reads 19 as 9.
_NUMBER_WORD_ALTERNATION = "|".join(sorted((*_NUMBER_WORDS, *_SCALE_WORDS), key=len, reverse=True))

#: ``five hundred``, ``twenty five``, ``a hundred``, ``two hundred and fifty``.
_WORD_AMOUNT = (
    rf"(?:an?\s+)?(?:{_NUMBER_WORD_ALTERNATION})"
    rf"(?:[\s-]+(?:and[\s-]+)?(?:{_NUMBER_WORD_ALTERNATION}))*"
)

#: Either spelling of an amount. One capturing group, so the callers below read ``group(...)``
#: the way they always did.
_AMOUNT = rf"({_DIGIT_AMOUNT}|{_WORD_AMOUNT})"
_AMOUNT_LOW = rf"(?P<low>{_DIGIT_AMOUNT}|{_WORD_AMOUNT})"
_AMOUNT_HIGH = rf"(?P<high>{_DIGIT_AMOUNT}|{_WORD_AMOUNT})"
_AMOUNT_NAMED = rf"(?P<amount>{_DIGIT_AMOUNT}|{_WORD_AMOUNT})"

#: The hedges that turn an amount into a TARGET rather than a bound. See
#: :data:`_APPROX_RE`; the rule is the module's own and predates this list.
_APPROXIMATOR = r"around|about|roughly|approximately|approx|nearly|almost|ballpark|near|close to|~"

#: What a person writes after a number when they mean money and do not reach for ``$``.
_CURRENCY_WORD = r"dollars?|bucks?|usd|quid"

#: ``$20-$50``, ``between $20 and 50``. Anchored on a ``$`` so "size 8 to 10" is not money.
_RANGE_RE = re.compile(rf"(?:between\s+)?\$\s*{_AMOUNT}\s*(?:-|–|—|to|and)\s*\$?\s*{_AMOUNT}")

#: ``between 300 and 500`` — "between" is the money cue that the missing ``$`` was.
_BETWEEN_RANGE_RE = re.compile(rf"between\s+\$?\s*{_AMOUNT}\s*(?:-|–|—|to|and)\s*\$?\s*{_AMOUNT}")

#: ``300 to 500``, ``300-500``. Read as money ONLY while the buyer is answering the budget
#: question, for the same reason :data:`_BARE_NUMBER_RE` is: outside that turn it is a size
#: run, a quantity or a model number.
_PLAIN_RANGE_RE = re.compile(rf"{_AMOUNT_LOW}\s*(?:-|–|—|to|and)\s*{_AMOUNT_HIGH}")

#: ``under $20``, ``no more than 20``, ``budget of $20``, ``up to about five hundred``.
#:
#: The optional ``approx`` group is the whole of the "up to about" fix. A hedge sitting
#: between the cue and the amount is not noise to be skipped over: it says the shopper's
#: ceiling is soft, and this module has always read a hedged amount as a target that fixes
#: the band and rules nothing out ("around $40" does not exclude $42). A ceiling cue in
#: front of the hedge does not make the number exact again.
_CEILING_RE = re.compile(
    r"(?:under|below|less than|lower than|no more than|not more than|at most|up to|"
    r"cheaper than|within|max|maximum|max of|maximum of|budget of|budget is|spend)"
    rf"\s+(?:(?P<approx>{_APPROXIMATOR})\s*)?\$?\s*{_AMOUNT_NAMED}"
)

#: ``over $50``, ``at least $50``, ``starting at $50``.
_FLOOR_RE = re.compile(
    rf"(?:over|above|more than|at least|starting at|starting from|from)\s+\$\s*{_AMOUNT}"
)

#: ``around $40`` — a target, not a ceiling. Sets the band and states no filter.
_APPROX_RE = re.compile(rf"(?:{_APPROXIMATOR})\s*\$?\s*{_AMOUNT}")

#: ``500ish``, ``500-ish``. The same hedge with the same meaning, written as a suffix — and
#: invisible to every other pattern here, because :data:`_BARE_NUMBER_RE`'s trailing
#: ``(?![\w.])`` refuses it and ``$`` is absent.
_ISH_RE = re.compile(rf"{_AMOUNT}\s*-?\s*ish\b")

#: A bare ``$40``.
_DOLLARS_RE = re.compile(rf"\$\s*{_AMOUNT}")

#: ``500 dollars``, ``five hundred bucks``. The currency word is the money cue the ``$``
#: would have been.
_CURRENCY_SUFFIX_RE = re.compile(rf"{_AMOUNT}\s*(?:{_CURRENCY_WORD})\b")

#: A bare ``40``. Only read as money when the buyer is answering the budget question.
_BARE_NUMBER_RE = re.compile(rf"(?<![\w.])({_DIGIT_AMOUNT})(?![\w.])")

#: A bare ``fifty``, same rule. Split from :data:`_BARE_NUMBER_RE` because a word amount has
#: no ``(?<![\w.])`` to lean on.
_BARE_WORD_AMOUNT_RE = re.compile(rf"\b{_AMOUNT}\b")

#: Number words that are ordinary English before they are amounts, so a BARE one of them is
#: not money. Measured: "one of those cheap ones", answered to the budget question, read as
#: a ``price_usd lte 1.0`` ceiling — a budget nobody stated, which is the exact failure the
#: bare-number rule exists to prevent. ``$1`` and "one dollar" still work: this only refuses
#: the word standing alone with no currency marker and no scale beside it.
_AMBIGUOUS_ALONE: frozenset[str] = frozenset({"one", "zero", "a", "an"})

#: Every pattern whose matched span must be blanked before the word lexicon runs, so a
#: price can never be re-read as a size, a quantity or a model number — nor its number words
#: ("five hundred dollars") counted as the content tokens that make an utterance specific.
_MONEY_SPANS = (
    _RANGE_RE,
    _BETWEEN_RANGE_RE,
    _CEILING_RE,
    _FLOOR_RE,
    _APPROX_RE,
    _ISH_RE,
    _DOLLARS_RE,
    _CURRENCY_SUFFIX_RE,
)


#: ``size 10``, ``US 9.5``.
_SIZE_RE = re.compile(r"\bsize\s+(\d{1,2}(?:\.\d)?)\b")
_REGION_SIZE_RE = re.compile(r"\b(?:us|uk|eu)\s*(\d{1,2}(?:\.\d)?)\b")

#: A whole size phrase INCLUDING a run ("size 8 to 10"), for blanking only — no capture, and
#: never used to mint a constraint. :func:`read_budget` needs it because a buyer answering
#: "what's your budget?" with "size 8 to 10" is stating a size, and the bare-number rule
#: below reads a lone number as dollars. Measured at HEAD, before this existed: that answer
#: produced ``price_usd lte 8.0`` and no size at all. Blanking only the ``size 8`` half is
#: not enough — the trailing ``10`` is still a lone number.
_SIZE_SPAN_RE = re.compile(
    r"\b(?:size|us|uk|eu)\s*\d{1,2}(?:\.\d)?(?:\s*(?:-|–|—|to|and)\s*\d{1,2}(?:\.\d)?)?\b"
)

#: A number followed by a unit of MEASURE — for blanking only, exactly like the size span
#: above and for the same reason, one unit further out.
#:
#: Measured on the served route before this existed: the turn "It must be cherry wood, and
#: at least 48 inches wide", absorbed as the answer to the budget question, produced
#: ``price_usd lte 48.0``. The shopper stated a width and was given a $48 budget. The
#: bare-number rule is right — a lone "40" answering "what's your budget?" IS dollars — and
#: it has no way to know that this 48 already has a unit attached to it, so the unit is
#: taken off the table before the money reader runs.
#:
#: Currency words are deliberately absent: "$48" and "48 dollars" are money and must stay
#: readable. This is only for units that make a number NOT money — and the one unit that is
#: also an English word is read through :data:`_INCHES` rather than literally.

#: The number half of a measurement. Thousands separators are accepted for the same
#: reason :data:`_DIGIT_AMOUNT` accepts them: without it the span started AFTER the
#: comma, so "1,200 mm wide" blanked only "200 mm" and left "1," for the money reader to
#: read as a $1 budget.
_MEASURED_NUMBER = r"\b\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?"

#: Every unit EXCEPT ``in``. A number carrying one of these is not money, whatever else the
#: sentence says — including when a money pattern would otherwise have claimed it, which is
#: the whole point: "under 5 lbs" reaches :data:`_CEILING_RE` as "under 5" and has to lose.
_UNAMBIGUOUS_UNITS = (
    r"inch(?:es)?|cm\b|centimet(?:er|re)s?|mm\b|millimet(?:er|re)s?|"
    r"m\b|met(?:er|re)s?|ft\b|feet|foot|yards?|yds?\b|"
    r"lbs?\b|pounds?|oz\b|ounces?|kg\b|kilos?|kilograms?|grams?|g\b|"
    r"litres?|liters?|l\b|ml\b|gallons?|quarts?|"
    r"watts?|w\b|volts?|v\b|amps?|hz\b|hours?|hrs?\b|mins?\b|minutes?|days?|"
    r'"|″|”'
)

_MEASURE_SPAN_RE = re.compile(rf"{_MEASURED_NUMBER}\s*(?:{_UNAMBIGUOUS_UNITS})")

#: A number followed by a bare ``in``. Whether that ``in`` is the unit INCHES or the English
#: preposition is decided by :func:`_measure_spans`, not here.
#:
#: A DIGIT after the word disqualifies it outright, and that is measured rather than tidy:
#: "under 400 in 2 weeks" read the ``in`` as inches, blanked "400 in", and left
#: :data:`_CEILING_RE` to pair the cue with the number on the other side — a $2 ceiling out
#: of a $400 budget, which is worse than losing the budget because it ships a filter nobody
#: stated. No measurement is written "48 in 2".
_INCHES_SPAN_RE = re.compile(rf"{_MEASURED_NUMBER}\s*in(?![a-z])(?!\s*\d)")

#: Dimension words — what is being measured. They settle a bare ``in`` as INCHES, but ONLY
#: when terminal: at the end of the phrase, before punctuation, or before "and"/"or".
#:
#: The terminal test is the whole of what makes this list safe, and it is measured rather
#: than careful. Every word here doubles as a colour, finish or weave adjective sitting in
#: front of a noun, and without the test all thirteen of these lost the stated budget:
#: "a rug under 300 in **deep** blue", "a lamp under 400 in **high** gloss", "a rug under
#: 400 in **thick** wool", "a table under 500 in **long** grain oak", "curtains under 200 in
#: **wide** stripe", "a bowl under 90 in **square** profile", "a mirror under 150 in **tall**
#: format". In each the adjective is followed by another word; in "48 in wide" and "no more
#: than 48 in wide" it is not, and that is the difference the list alone could not see.
_INCH_DIMENSION = (
    r"wide|width|tall|high|height|long|length|deep|depth|thick|thickness|across|diameter|"
    r"square|overall|unassembled|assembled|folded|apart"
)

#: Words the PREPOSITION ``in`` cannot be followed by. "in of", "in on", "in from", "in
#: per", "in when", "in in" are not English, so a bare ``in`` in front of one of them is the
#: unit — with no terminal test and no money test, because there is nothing to weigh.
#:
#: This is the half that reads a measurement written the way people actually write one:
#: "at most 60 in **on** the diagonal", "no more than 36 in **from** the floor", "at most 30
#: in **per** side", "under 48 in **when** folded", "48 in **of** clearance", "about 48 in
#: **in** total". Each carries a ceiling cue, so the money test claimed the number and
#: turned a width into a budget.
#:
#: **Every word here has to be one the preposition genuinely cannot take**, and a first
#: draft of this list was not: ``about``, ``around``, ``over``, ``under``, ``between``,
#: ``into``, ``out``, ``up``, ``down``, ``to``, ``front``, ``back`` and ``through`` all
#: follow it perfectly well — "in about two weeks", "in under an hour", "in front of the
#: window", "in between the studs" — and with them here a stated budget was eaten in each.
#: ``front to back`` is handled as its own phrase below rather than by the bare word.
_AFTER_INCHES_ONLY = r"of|on|from|per|when|in|and|or|at|after|before|plus|off|while|though"

#: What settles a bare ``in`` as INCHES regardless of what precedes it. Matched at the end
#: of the in-span; when it does NOT match, :func:`_measure_spans` falls through to the money
#: test rather than to a verdict, which is what keeps "under 400 in oak" a budget.
#:
#: ``x``/``by`` are here only as the DIMENSION-PAIR shape "24 in by 36 in" — a number and
#: another unit behind them. Bare, they ate "under 400 in by friday"; in front of a bare
#: number they ate "under 400 in by 2 weeks". Bare punctuation is absent for the same
#: reason — it ate "under 400 in, oak". ``front to back`` is the one phrase from the
#: preposition list that earns a place here, because "in front OF" is ordinary English and
#: "in front TO back" is not.
_INCH_DIMENSION_RE = re.compile(
    rf"\s*(?:{_INCH_DIMENSION})\b(?=\s*(?:$|[,.;:)]|(?:and|or)\b))"
    rf"|\s*(?:{_AFTER_INCHES_ONLY})\b"
    rf"|\s*(?:x|by)\s*\d{{1,4}}\s*(?:in\b|inch(?:es)?|\"|″|”)"
    rf"|\s*front\s+to\s+back\b"
    rf"|\s*$"
)

#: A currency marker standing immediately in front of a number, which makes that number
#: MONEY whatever unit-shaped word happens to follow it.
_PRICED_NUMBER_RE = re.compile(r"[$€£]\s*$")


def _overlaps(span: tuple[int, int], spans: Iterable[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in spans)


def _measure_spans(text: str) -> list[tuple[int, int]]:
    """Every span of ``text`` that is a number carrying a unit of MEASURE.

    Blanked before the money reader runs, because a number that already has a unit attached
    is not available to be a price. Measured, before any of this existed: "It must be cherry
    wood, and at least 48 inches wide", absorbed as the answer to the budget question,
    produced ``price_usd lte 48.0``.

    **``in`` is decided differently from every other unit, and the difference is the whole
    of this function.** It is the only entry in the table that is also an ordinary English
    word, and reading it as a unit unconditionally ate a stated budget — the blanking runs
    before ANY money pattern, so the amount was gone before :data:`_CEILING_RE` ever
    looked::

        'a coffee table under $400 in oak'  ->  'a coffee table under $       oak'
        'a rug under $300 in wool'          ->  'a rug under $       wool'

    Served, through ``POST /buyer/intent/clarify``: the turns "a coffee table under $400 in
    oak" / "I already told you" came back with ``hard_constraints: []``, ``budget_band:
    "unspecified"`` and BOTH canned budget questions asked — the owner's own screenshot,
    recreated by the repair written for it.

    **Two signals decide it, in this order, and both were needed.** Each on its own was
    tried and each on its own is wrong in the other direction:

    1. **A dimension word after the ``in`` settles it as inches, whatever precedes.** A
       closed list of those words was the first rule and, used ALONE, it read "48 in of
       clearance", "48 in from the wall", "60 in tv stand" and "48 in or wider" as $48
       budgets — everything outside the list fell to money.
    2. **Otherwise, a number some money pattern already claims is money.** That test alone
       was the second rule, and it is backwards for every ceiling, hedge and range cue: "no
       more than 48 in wide" and "between 40 and 60 in wide" are claimed by
       :data:`_CEILING_RE` and :data:`_BETWEEN_RANGE_RE`, so a stated width became a $48
       ceiling — measured on the served route, overwriting a real $400 budget stated one
       turn earlier. Its apparent success rested on :data:`_FLOOR_RE` needing a literal
       ``$``, which is true of "at least 48 in wide" and of nothing else.

    So the list decides when it matches and the money test decides when it does not, and
    neither is asked to answer a question it gets wrong.

    The currency guard is separate and applies to every unit, ``in`` included: ``$5 m`` and
    ``$300 l`` are money followed by a letter, and blanking either takes the shopper's own
    budget off the table.
    """
    claimed = [match.span() for pattern in _MONEY_SPANS for match in pattern.finditer(text)]
    spans = [
        match.span()
        for match in _MEASURE_SPAN_RE.finditer(text)
        if not _PRICED_NUMBER_RE.search(text[: match.start()])
    ]
    for match in _INCHES_SPAN_RE.finditer(text):
        if _PRICED_NUMBER_RE.search(text[: match.start()]):
            continue  # `$400 in oak` — the currency marker already spent this number
        if _INCH_DIMENSION_RE.match(text, match.end()) is not None:
            spans.append(match.span())  # `48 in wide` — a dimension, whatever precedes it
            continue
        if not _overlaps(match.span(), claimed):
            spans.append(match.span())  # `48 in of clearance` — no money pattern wants it
    return spans


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


def _parse_number_words(phrase: str) -> float | None:
    """``"two hundred and fifty"`` -> ``250.0``; anything outside the table -> ``None``.

    The ordinary accumulate: units and tens add into ``current``, ``hundred`` scales it,
    ``thousand`` banks it. ``a``/``an`` in front of a scale is the ``one`` a person leaves
    out ("a hundred"), and ``and`` is punctuation.
    """
    total = 0.0
    current = 0.0
    seen = False
    for token in re.findall(r"[a-z]+", phrase.casefold()):
        if token in ("and", "a", "an"):
            continue
        if token in _NUMBER_WORDS:
            current += _NUMBER_WORDS[token]
            seen = True
            continue
        if token in _SCALE_WORDS:
            scale = _SCALE_WORDS[token]
            if current == 0.0:
                current = 1.0
            if scale == 100:
                current *= 100.0
            else:
                total += current * scale
                current = 0.0
            seen = True
            continue
        return None
    return total + current if seen else None


def _amount_value(raw: str | None) -> float | None:
    """One matched amount as a number, whichever spelling it arrived in."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if re.fullmatch(_DIGIT_AMOUNT, text):
        # The separators are punctuation, not part of the number. `float("1,200")` raises.
        return float(text.replace(",", ""))
    return _parse_number_words(text)


def _is_scaled_words(raw: str | None) -> bool:
    """Does this amount carry ``hundred``/``thousand``?

    The bar a WORD amount has to clear before a hedge alone is allowed to read it as money.
    ``about five hundred`` is unmistakably an amount; ``about one`` is a turn of phrase, and
    inventing a ``0-50`` budget out of it is exactly the failure the ``allow_bare_number``
    rule already exists to prevent one number-spelling further down.
    """
    if raw is None:
        return False
    return any(word in raw.casefold() for word in _SCALE_WORDS)


def read_budget(text: str, *, allow_bare_number: bool = False) -> tuple[BudgetReading, str]:
    """Read money out of ``text``; return the reading and the text with money blanked out.

    ``allow_bare_number`` is on only while the buyer is answering the budget question,
    where a lone "40" is unambiguous. Everywhere else a lone number is a size, a quantity
    or a model number and reading it as dollars is how a clarifier invents a budget.

    What this reads
    ---------------
    ``under 500`` · ``no more than 500`` · ``up to $500`` · ``budget of 500`` · ``$500`` ·
    ``500 dollars`` · ``five hundred dollars`` · ``twelve hundred`` · ``two hundred fifty``
    · ``a hundred bucks`` · ``between $60 and $120`` · ``300 to 500`` (budget answers only) ·
    and, as **targets that fix the band and state no filter**, ``around $40`` · ``about five
    hundred`` · ``up to about five hundred`` · ``500ish``.

    What it deliberately does not
    -----------------------------
    ``two fifty`` for 250 (indistinguishable from a 2 and a 50 in the same breath), ``5k``,
    ``half a grand``, ``a couple hundred``, ``mid three figures``, number words outside
    English, any currency but USD, and a second amount in the same sentence — the first cue
    still wins, exactly as before. Each of those is a guess, and this module's whole
    contract is that it reports only what the buyer actually typed.
    """
    ceiling: float | None = None
    floor: float | None = None
    band: str | None = None

    # A number that already carries a unit of measure is not available to be money, and it
    # is taken off the table before ANY money pattern runs rather than only before the
    # bare-number one. Both halves of that were measured: "at least 48 inches wide" reached
    # the bare-number rule and produced `price_usd lte 48.0`, and "under 5 lbs" never got
    # that far because the CUED ceiling pattern matched "under 5" first. Blanking in one
    # place would have fixed one of them and left the other exactly as it was.
    #
    # `$` and currency words are not units of measure, so "$500" and "500 dollars" are
    # untouched — see `_MEASURE_SPAN_RE`. The text this scans is a working copy; what the
    # lexicon later scans is still built from the original, so "48 inches wide" keeps
    # contributing its content words to whether the utterance names a product.
    priced = _blank(text, _measure_spans(text))

    range_match = _RANGE_RE.search(priced) or _BETWEEN_RANGE_RE.search(priced)
    range_values = (
        (_amount_value(range_match.group(1)), _amount_value(range_match.group(2)))
        if range_match
        else (None, None)
    )
    if range_values[0] is not None and range_values[1] is not None:
        floor, ceiling = sorted((range_values[0], range_values[1]))
    else:
        # The ceiling is read FIRST and its span is blanked before the floor is looked
        # for. Measured: "no more than $25 a bag" matched the ceiling pattern on
        # "no more than 25" AND the floor pattern on the "more than $25" inside it, so one
        # sentence produced `price_usd lte 25` and `price_usd gte 25` together — a filter
        # that admits exactly $25.00 and nothing else. Searching the two patterns over the
        # same text independently is what made a negation readable as its own opposite.
        work = priced
        ceiling_match = _CEILING_RE.search(work)
        if ceiling_match:
            amount = _amount_value(ceiling_match.group("amount"))
            if amount is not None:
                if ceiling_match.group("approx"):
                    # "up to about five hundred": the hedge is the operative word. It fixes
                    # the band and rules nothing out, because a shopper who says "about"
                    # would not reject a $510 machine — and a filter they would not have
                    # asked for is the difference between a narrowed shortlist and an empty
                    # one.
                    band = band_for_amount(amount)
                else:
                    ceiling = amount
                work = _blank(work, [ceiling_match.span()])
        floor_match = _FLOOR_RE.search(work)
        if floor_match:
            floor = _amount_value(floor_match.group(1))
        if ceiling is None and floor is None and band is None:
            band, ceiling = _read_uncued_money(priced)

    blanked = _blank_money(text)
    if ceiling is None and floor is None and band is None and allow_bare_number:
        ceiling, floor, blanked = _read_bare_money(blanked)

    if band is None:
        anchor = ceiling if ceiling is not None else floor
        if anchor is not None:
            band = band_for_amount(anchor)
    return BudgetReading(ceiling=ceiling, floor=floor, band=band), blanked


def _read_uncued_money(text: str) -> tuple[str | None, float | None]:
    """``(band, ceiling)`` for money written without a ceiling or floor cue."""
    approx = _APPROX_RE.search(text)
    if approx:
        amount = _amount_value(approx.group(1))
        if amount is not None and (
            _is_scaled_words(approx.group(1))
            or re.fullmatch(_DIGIT_AMOUNT, approx.group(1).strip())
        ):
            # A target is not a filter: it fixes the band and states no constraint,
            # because "around $40" does not rule out $42.
            return band_for_amount(amount), None
    ish = _ISH_RE.search(text)
    if ish:
        amount = _amount_value(ish.group(1))
        if amount is not None:
            return band_for_amount(amount), None
    for pattern in (_DOLLARS_RE, _CURRENCY_SUFFIX_RE):
        match = pattern.search(text)
        if match:
            amount = _amount_value(match.group(1))
            if amount is not None:
                return None, amount
    return None, None


def _read_bare_money(blanked: str) -> tuple[float | None, float | None, str]:
    """``(ceiling, floor, blanked)`` for a budget ANSWER that names bare numbers.

    Sizes and measurements are blanked out of the working copy first: "size 8 to 10" is an
    answer a shopper really gives, and both the bare range and the bare number would
    otherwise turn it into a price of $8. "at least 48 inches wide" is the same shape with a
    different unit, and it really did produce ``price_usd lte 48.0`` — measured on the served
    route, from the owner's own second utterance.
    """
    work = _blank(blanked, [match.span() for match in _SIZE_SPAN_RE.finditer(blanked)])
    work = _blank(work, _measure_spans(work))
    span = _PLAIN_RANGE_RE.search(work)
    if span:
        low, high = _amount_value(span.group("low")), _amount_value(span.group("high"))
        if low is not None and high is not None:
            floor, ceiling = sorted((low, high))
            return ceiling, floor, _blank(blanked, [span.span()])
    for pattern in (_BARE_NUMBER_RE, _BARE_WORD_AMOUNT_RE):
        for match in pattern.finditer(work):
            if match.group(1).strip().casefold() in _AMBIGUOUS_ALONE:
                continue
            amount = _amount_value(match.group(1))
            if amount is not None:
                return amount, None, _blank(blanked, [match.span()])
    return None, None, blanked


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
_KIND_SOFT_CONSTRAINT = "soft_constraint"
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
    # NOTHING is added to this table for the cherry-wood defect, and the reason is measured
    # rather than cautious. `intent.category` is not inert: the exchange's retrieval filters
    # on it, and the corpus's own category taxonomy does not use these words. Driven three
    # times against the served exchange with the query "a modular table" (which the corpus
    # answers), varying only this field:
    #
    #     category absent      -> products_considered 25, shortlist 3
    #     category "furniture" -> products_considered  0, shortlist 0
    #     category "coffee"    -> products_considered  1, shortlist 0
    #     category null        -> products_considered 25, shortlist 3
    #
    # So adding "table" here would have taken a working three-slot query to an empty page —
    # the same failure as an undecidable hard constraint, one field over and with no
    # relaxation path at all. (That "coffee", a word this table has always carried, also
    # costs 24 of 25 candidates is a separate defect in the exchange's retrieval and is
    # reported rather than fixed here.) Specificity is instead restored where it broke, in
    # `extract`, by letting a softened match keep contributing its content words.
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

#: Materials the buyer's own words name but this network cannot filter on, so they are read
#: as SOFTENED readings — kept with their value, scored, never a gate. Same destination as
#: an unvouched model proposal, deliberately: one mechanism, so the offline double and the
#: live model cannot disagree about what happens to "cherry wood".
#:
#: Measured on the nineteen-store corpus, which is why these are here and not in
#: ``_CONSTRAINTS``: ``MATCH (a:AttributeValue) WHERE a.canonical_key='material'`` returns
#: **3** nodes in a 98,001-node graph, with the values "powder", "fabric" and "metal". The
#: word "cherry" appears four times and never as a material — twice as a colour ("cherry
#: red"), once as a flavour, once inside "sienna (cherry)". ``wood-type`` carries six
#: values (oak, american oak, walnut, birch, ash, maple) across six variants. A filter on
#: any of that decides nothing and excludes everyone.
#:
#: Bare "cherry" is deliberately absent: on this corpus it is a colour and a flavour more
#: often than a wood, and a lexicon that guesses is the thing this module refuses to be.
#: "cherry wood" and "cherrywood" are unambiguous and are what the shopper typed.
_SOFT_CONSTRAINTS: dict[str, tuple[str, str, Any]] = {
    "cherry wood": ("material", "eq", "cherry-wood"),
    "cherrywood": ("material", "eq", "cherry-wood"),
    "solid wood": ("material", "eq", "solid-wood"),
    "reclaimed wood": ("material", "eq", "reclaimed-wood"),
    "hardwood": ("material", "eq", "hardwood"),
    "plywood": ("material", "eq", "plywood"),
    "oak": ("material", "eq", "oak"),
    "walnut": ("material", "eq", "walnut"),
    "maple": ("material", "eq", "maple"),
    "birch": ("material", "eq", "birch"),
    "teak": ("material", "eq", "teak"),
    "mahogany": ("material", "eq", "mahogany"),
    "rattan": ("material", "eq", "rattan"),
    "velvet": ("material", "eq", "velvet"),
    "linen": ("material", "eq", "linen"),
    "boucle": ("material", "eq", "boucle"),
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
        # Money words that survive the blanking above when the sentence names no amount
        # this module can read ("my budget is tight"). They describe the transaction, never
        # the product, so counting them as content words made a moneyed hedge look like a
        # stated need.
        "buck",
        "bucks",
        "budget",
        "but",
        "buy",
        "by",
        "cost",
        "dollar",
        "dollars",
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
        "usd",
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
    for phrase, spec in _SOFT_CONSTRAINTS.items():
        add(phrase, _KIND_SOFT_CONSTRAINT, spec)
    for phrase, pref in _PREFERENCES.items():
        add(phrase, _KIND_PREFERENCE, pref)
    for word in _VAGUE:
        add(word, _KIND_VAGUE, word)
    for word in _FILLER:
        add(word, _KIND_FILLER, word)
    return {key: tuple(value) for key, value in table.items()}


_PHRASES = _build_phrase_table()
_LONGEST_PHRASE = max(len(key) for key in _PHRASES)


# --------------------------------------------------------------------------------------
# which fields this service is willing to turn into an ELIGIBILITY FILTER
# --------------------------------------------------------------------------------------

#: Every field the deterministic lexicon can mint from the buyer's literal words, plus the
#: two the money and size readers add. Derived from the tables rather than hand-listed, so
#: a new lexicon entry cannot silently fall outside the set that vouches for it.
#:
#: **Why a closed set exists at all, measured rather than reasoned about.** A hard
#: constraint is an eligibility filter (R19), and a filter this network cannot decide does
#: not narrow a shortlist — it empties one. Driven three times each against the served
#: exchange (``POST http://localhost:8083/auctions``) with the query "a sofa" on the
#: nineteen-store corpus:
#:
#: ===========================================  =====  ==================
#: hard constraints on the intent               slots  relaxed_constraints
#: ===========================================  =====  ==================
#: (none)                                           3  []
#: ``color eq terra`` (a value a slot carries)      1  [the constraint]
#: ``color eq navy``                                0  []
#: ``material eq cherry-wood``                      0  []
#: ``width_in gte 48``                              0  []
#: ===========================================  =====  ==================
#:
#: The same query with a ``material``, ``width_in`` or entirely invented ``cherry_wood``
#: PREFERENCE shortlists 3 of 3 every time. So the scoring half of R19 is survivable and
#: the filtering half is not, and the exchange's own relaxation escape hatch fired in
#: exactly one of the four cases — it is a backstop, not a licence.
#:
#: This set is therefore what the BUYER'S OWN WORDS established through a table that was
#: built alongside the fixtures. A model may confirm one of these; it may not invent a new
#: one, because nothing here can check whether the network can answer it. See
#: :meth:`IntentDraft.absorb_proposal`.
VOUCHED_FILTER_FIELDS: frozenset[str] = frozenset(
    {spec[0] for spec in _CONSTRAINTS.values()} | {"size", "price_usd"}
)

#: The (field, value) PAIRS the lexicon can mint, which is the granularity that actually
#: decides this. The field alone is not enough and the table above is the proof: ``color eq
#: terra`` left one slot standing and ``color eq navy`` left none, on the same corpus,
#: through the same field. What separates them is whether any candidate carries the value,
#: and a value is exactly what this service has no way to check.
#:
#: So the rule is the narrowest one that is still true: this service vouches for a filter
#: when its own table, built beside the fixtures, produces that same field AND that same
#: value from a buyer's literal words. Everything else is softened.
#: The OP is part of the term and not a detail. Measured against the live model on the
#: demo's own beanie utterance: with the pair checked but the op ignored, Sonnet's
#: ``{"field": "material", "op": "contains", "value": "merino wool"}`` was vouched — because
#: the lexicon does know ``material``/``merino-wool`` — and landed as a SECOND eligibility
#: filter beside the lexicon's own ``material eq merino-wool``. ``HardConstraint.key`` is
#: ``(field, op)``, so the two did not even collide. `contains` is a filter this lexicon can
#: never mint and this service cannot check, on the demo's headline query.
VOUCHED_FILTER_TERMS: frozenset[tuple[str, str, Any]] = frozenset(
    (spec[0], spec[1], spec[2]) for spec in _CONSTRAINTS.values()
)

#: ``price_usd`` and ``size`` used to sit here in an ``_OPEN_VALUE_TERMS`` exemption that
#: vouched for them on FIELD AND OP ALONE — "their values are numbers, so there is no term
#: to check". The exemption is gone, and the measurement is why.
#:
#: The reasoning was backwards. "Nothing to check" is a reason to trust a model LESS on a
#: field, not more: it is the one case where this service cannot even tell that the value
#: is nonsense. And price is not a safe field to be wrong about — it is the one this corpus
#: cannot decide at all. Driven against the served exchange (``POST
#: http://localhost:8083/auctions``, nineteen-store demo corpus), query "a sofa":
#:
#: ==============================  =====  =====  ===================
#: hard constraints on the intent  slots  shops  products_considered
#: ==============================  =====  =====  ===================
#: (none)                              3      3                   25
#: ``price_usd lte 5000``              0      0                    0
#: ``price_usd lte 200``               0      0                    0
#: ``size eq queen``                   0      0                    0
#: ==============================  =====  =====  ===================
#:
#: $5000 is above every price in the corpus, so the empty page is not a ceiling doing its
#: job. ``MATCH (a:AttributeValue) WHERE a.canonical_key CONTAINS 'price'`` returns **zero**
#: nodes in that graph and no ``Product`` or ``Variant`` node carries a price property, so
#: ``retrieval.criteria.HardCriterion.pushdown`` — which turns an ``lte``/``gte`` into a
#: graph-side ``AttributeFilter`` — matches nothing and the roster comes back empty. A model
#: that names a price bound therefore costs the whole page, which is exactly the outcome
#: softening exists to prevent.
#:
#: **This narrows what a MODEL may propose and nothing else.** :func:`vouched_as_filter` is
#: asked only about :meth:`IntentDraft.absorb_proposal`'s input; a ceiling the SHOPPER typed
#: is minted by :func:`read_budget` inside :func:`extract` and added with
#: ``authoritative=True``, never passing through here. R19 gives the buyer's own stated
#: ceiling the filtering half and ``exchange.ranking.filters.budget_reasons`` decides it
#: against the offer's own price; that is untouched.


def vouched_as_filter(constraint: HardConstraint) -> bool:
    """May this MODEL-PROPOSED constraint be applied as an eligibility filter?

    True only for a term the deterministic lexicon could itself have minted from the
    buyer's own words — same field, same op, same value. See :data:`VOUCHED_FILTER_TERMS`
    for why all three have to match and not just the field, and the note above it for why
    there is no longer an exemption for fields whose values are numbers.

    Only :meth:`IntentDraft.absorb_proposal` asks this question. The buyer's own words are
    vouched by having been minted from a table built beside the fixtures, and they reach
    the draft by a different door.

    **An UNHASHABLE value answers False rather than raising**, and that is a served defect
    rather than defensiveness. ``in`` is a published ``CONSTRAINT_OPS`` member whose value is
    a LIST — ``intent.ts::describeConstraint`` has a branch that joins one, and
    ``test_intent_models`` builds a model reply carrying ``{"op": "in", "value": [...]}`` —
    and a list is not hashable, so the membership test below raised ``TypeError`` straight
    out of the route: measured, ``POST /buyer/intent/clarify`` answered **HTTP 500** for a
    model reply of ``{"field": "material", "op": "in", "value": ["oak", "walnut"]}``. The
    honest answer for a value this lexicon cannot possibly have minted is "no", which is
    what every unhashable value is.
    """
    value = constraint.value
    if isinstance(value, str):
        value = value.strip().casefold().replace(" ", "-")
    try:
        return (constraint.field, constraint.op, value) in VOUCHED_FILTER_TERMS
    except TypeError:
        return False


#: How an op that cannot be a filter is re-read as a score direction. ``eq`` on an
#: unvouched field becomes a soft "I would rather it were this"; a floor becomes "more is
#: better" and a ceiling "less is better". Nothing outside this map is softened at all —
#: it is dropped and recorded, on the same principle that keeps ``lt`` from becoming
#: ``lte``.
_SOFTENED_DIRECTIONS: dict[str, str] = {
    "eq": "prefer",
    "in": "prefer",
    "contains": "prefer",
    "gte": "maximize",
    "lte": "minimize",
}

#: What a softened reading is worth as a score term. Below every weight the lexicon mints
#: from the buyer's own words (the lowest of those is 0.4), because a softened reading is
#: this service saying "somebody said this and we could not verify it".
SOFTENED_WEIGHT = 0.3


@dataclass(frozen=True)
class SoftenedReading:
    """A must-have this service kept but refused to enforce, and why.

    The buyer said it, so it is not dropped. This network cannot decide it, so it is not a
    filter. Both halves have to be said out loud or the intent is lying in one direction or
    the other — silently widening the search, or silently emptying it.

    ``value`` is preserved here and **not** in the preference minted beside it, because
    R19's ``Preference`` is ``field``/``direction``/``weight`` with nowhere to put one. A
    store therefore learns the direction and the shopper learns the whole sentence, which
    is the honest split given the published contract.
    """

    field: str
    op: str
    value: Any
    reason: str
    source: str = "model"
    #: Does the preference minted beside this reading actually SCORE anything?
    #:
    #: False for a field a published ranking term already answers for. Measured through
    #: ``exchange.retrieval.criteria.build_query``, which calls
    #: ``contracts.ranking.preference_term_conflict`` and REFUSES every ``price_usd``,
    #: ``delivery_*`` and ``trust`` preference before scoring: a softened ``price_usd lte
    #: 300`` produced ``preferences: [price_usd minimize 0.3]`` on the intent and
    #: ``preferences kept: []`` in the query built from it. The confirmation screen was
    #: telling the shopper "we use these to rank rather than to exclude" over a reading that
    #: was neither ranking nor excluding, and this is the field that lets it stop.
    scored: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "op": self.op,
            "value": self.value,
            "reason": self.reason,
            "source": self.source,
            "scored": self.scored,
        }

    def describe(self) -> str:
        """The reading in the shopper's terms, for the confirmation screen."""
        spelling = _OP_IN_WORDS.get(self.op, self.op)
        name, unit = _field_in_words(self.field)
        value = _value_in_words(self.value)
        return f"{name} {spelling} {value}{f' {unit}' if unit else ''}".strip()


@dataclass(frozen=True)
class GapAnswer:
    """One question, the shopper's answer to it, and what came of that answer.

    This is the record that makes "We never got an answer about: X" a checkable claim
    rather than a guess. Before it existed there was nothing anywhere that distinguished
    *not answered* from *answered and unusable*, so the page rendered the first sentence
    for both — which is the sentence the owner screenshotted.

    ``gap`` is what was ASKED and ``addressed`` is what the answer turned out to be about.
    They are separate fields because they really do come apart, and a record that carried
    only the first told a second untruth in place of the first one. Measured on the served
    route with the turns "I want a cherry wood table" / "It must be cherry wood, and at
    least 48 inches wide" / "no, that is everything" / "nothing else"::

        {"gap": "budget",
         "question": "What is the most you would want to spend?",
         "answer": "It must be cherry wood, and at least 48 inches wide",
         "used": ["material is cherry-wood"],
         "understood": true}

    A shopper who named a material was recorded as having answered about MONEY — which the
    page renders as "You did answer about budget", and which told :meth:`IntentDraft
    .next_gap` the budget was settled, so it was never asked again. The answer is filed
    against what it produced; ``gap`` stays so the question it followed is still on the
    record.
    """

    gap: str
    question: str
    answer: str
    used: tuple[str, ...] = ()
    #: The gaps this answer actually spoke to. Empty means it produced nothing at all,
    #: which is the honest "you told us and we could not use it" — and the one case where
    #: the row still belongs to the gap it was asked under, because there is nowhere else
    #: for it to go.
    addressed: tuple[str, ...] = ()

    @property
    def understood(self) -> bool:
        return bool(self.used)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap": self.gap,
            "question": self.question,
            "answer": self.answer,
            "used": list(self.used),
            "addressed": list(self.addressed),
            "understood": self.understood,
        }


def soften(
    constraint: HardConstraint, *, reason: str, source: str = "model"
) -> tuple[SoftenedReading | None, Preference | None]:
    """Re-read a filter this service cannot vouch for as a score term plus a record.

    Returns ``(None, None)`` for an op with no honest score reading, so a constraint is
    never bent into a direction it does not mean.

    The preference is withheld — and :attr:`SoftenedReading.scored` set False — when a
    published ranking term already answers for the field, because the exchange refuses such
    a preference anyway (``contracts.ranking.preference_term_conflict``, applied in
    ``exchange.retrieval.criteria.build_query``). Minting one would put a term on the intent
    that is discarded before any candidate is scored, and the shopper would be told it ranks.
    """
    direction = _SOFTENED_DIRECTIONS.get(constraint.op)
    if direction is None:
        return None, None
    scored = preference_term_conflict(constraint.field) is None
    reading = SoftenedReading(
        field=constraint.field,
        op=constraint.op,
        value=constraint.value,
        reason=reason,
        source=source,
        scored=scored,
    )
    if not scored:
        return reading, None
    try:
        preference = Preference(field=constraint.field, direction=direction, weight=SOFTENED_WEIGHT)
    except InvalidPreference:
        return reading, None
    return reading, preference


@dataclass(frozen=True)
class Extraction:
    """Everything one buyer utterance said, in R19's vocabulary."""

    text: str
    category: str | None = None
    constraints: tuple[HardConstraint, ...] = ()
    preferences: tuple[Preference, ...] = ()
    budget: BudgetReading = field(default_factory=BudgetReading)
    content_tokens: tuple[str, ...] = ()
    #: Must-haves this utterance stated that cannot be eligibility filters here. Read from
    #: the buyer's own words, so unlike a model's they are never in doubt about *what was
    #: said* — only about whether this network can answer it.
    softened: tuple[SoftenedReading, ...] = ()

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
    softened: list[SoftenedReading] = []
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
            elif kind == _KIND_SOFT_CONSTRAINT:
                # A softened phrase still DESCRIBES the product, so its words keep counting
                # toward whether this utterance names what the shopper wants. Without this,
                # teaching the lexicon "cherry wood" made "a cherry wood table" LESS
                # specific than before — three loose content words became one, `specific`
                # went false, and the loop opened by asking a shopper who had just said what
                # they wanted what they were shopping for. A hard-constraint phrase does not
                # need this because it is nearly always accompanied by a category word the
                # table knows ("leather backpack"); a softened one, by construction, is a
                # word the network cannot place.
                content.extend(
                    token
                    for token in tokens[index : index + width]
                    if not token.isdigit() and len(token) >= MIN_CONTENT_TOKEN_CHARS
                )
                name, op, value = payload
                reading, preference = soften(
                    HardConstraint(field=name, op=op, value=value),
                    reason=(
                        f"you said {value!r}, and this network has almost no readings for "
                        f"{name} — requiring it would exclude every store rather than narrow "
                        f"the list, so it is used to rank rather than to filter"
                    ),
                    source="buyer",
                )
                if reading is not None:
                    softened.append(reading)
                if preference is not None:
                    preferences.append(preference)
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
        softened=tuple(softened),
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
        # A reply arrived, carried no JSON object and was not a bare question, so the whole
        # of it is being discarded. That used to happen in silence, and the silence is what
        # hid Defect A for as long as it hid: measured against the live model on this
        # stack, the HEAD contract produced a Markdown table reading `| material | = |
        # cherry wood |` and `| width | >= | 48 in |` — both correct — and this branch threw
        # it away with `dropped` empty, so no test, no log and no response field could tell
        # "the model proposed nothing" apart from "the model proposed the right thing and we
        # could not read it". Recording it does not recover the reading; it makes the loss
        # observable, which is the precondition for ever noticing it again.
        return LLMProposal(
            dropped=(
                f"the model's reply carried no JSON object and was not a bare clarifying "
                f"question, so all {len(text)} characters of it were discarded unread; "
                f"first 120: {text[:120]!r}",
            ),
            raw=text,
        )

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
    #: Must-haves kept but not enforced, with the reason. See :data:`VOUCHED_FILTER_FIELDS`.
    softened: list[SoftenedReading] = field(default_factory=list)
    #: Every question put to the shopper and what their answer produced. The record that
    #: makes ``unresolved`` a checkable claim rather than a guess.
    understood: list[GapAnswer] = field(default_factory=list)
    #: Which terms came from the buyer's own literal words. The cluster hash is taken over
    #: these alone — see :func:`buyer_svc.intent.clarifier._cluster_id`.
    authoritative_constraints: set[tuple[str, str]] = field(default_factory=set)
    authoritative_preferences: set[str] = field(default_factory=set)

    def absorb(self, utterance: str, *, gap: str | None = None, question: str = "") -> Extraction:
        """Fold one buyer turn into the draft.

        ``gap`` names the question this turn is answering, and it changes three things: a
        bare number is read as money only while answering the budget question; only a turn
        that describes the *need* joins ``query`` (an answer to "what's your budget?" is
        not part of what the buyer is shopping for); and the turn is filed against the gap
        it answers, so this draft can later tell a question that went unanswered apart from
        one that was answered and could not be used.
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
        self._learn_category(reading.category)
        for constraint in reading.constraints:
            self._add_constraint(constraint, authoritative=True)
        for preference in reading.preferences:
            self._add_preference(preference, authoritative=True)
        for softened in reading.softened:
            self._add_softened(softened)
        self._note_budget(reading.budget)
        # A record is written only when a QUESTION was actually put to the shopper, and the
        # two conditions are not the same thing. `clarifier.clarify` absorbs every turn the
        # buyer volunteered beyond the questions it asked with `gap=GAP_USE_CASE` — not
        # because anybody asked about the use case, but because that is the gap whose
        # answers join `query`. Recording those produced a row reading `{"gap": "use_case",
        # "question": ""}`, which the confirmation screen renders as "You did answer when we
        # asked about what you are shopping for" over a question that was never asked.
        # `_question_for` never returns a blank, so a missing question is exactly this case.
        if gap is not None and question:
            self.understood.append(
                GapAnswer(
                    gap=gap,
                    question=question,
                    answer=text,
                    used=_terms_of(reading),
                    addressed=_gaps_addressed(reading),
                )
            )
        return reading

    def absorb_proposal(self, proposal: LLMProposal) -> None:
        """Fold a model proposal in **additively**; it never overwrites the buyer.

        A proposed constraint on a field :data:`VOUCHED_FILTER_FIELDS` does not carry is
        **softened** rather than applied: kept as a :class:`SoftenedReading`, scored as a
        preference, and never turned into an eligibility filter. The model is reading the
        shopper's prose, which it is good at; it is not reading this network's catalogues,
        which it has never seen. Measured, a filter on a field nothing here can decide
        takes a three-slot shortlist to zero (see :data:`VOUCHED_FILTER_FIELDS`), so the
        cost of trusting the model on that question is the whole page.
        """
        self.dropped.extend(proposal.dropped)
        self._learn_category(proposal.category)
        settled_by_the_buyer = {
            key[0] for key in self.authoritative_constraints if key[1] in _SETTLED_OPS
        }
        for constraint in proposal.constraints:
            if constraint.field in settled_by_the_buyer and (
                constraint.op in _SETTLED_OPS or constraint.field in _NUMERIC_FIELDS
            ):
                # A SECOND definite statement about a subject the shopper has already
                # settled in their own words. `_add_constraint` has always refused to let a
                # model overwrite the buyer, but falling through to `soften` put the second
                # statement on `softened`, and the confirmation screen printed both — the
                # shopper's under "Budget" and the model's under "Noted, but not used to
                # rule anything out". Two headings, one subject, contradicting each other.
                #
                # This is ONE test and not two, and the pair-wise version it replaces is why
                # it is worth saying so: testing `constraint.key in
                # self.authoritative_constraints` catches only the exact echo, and every op
                # that walked past it was measured. With the shopper's `price_usd lte 200`
                # on file, a model `price_usd gte 100` rendered "price at least 100 dollars"
                # beside their own ceiling — a floor they never uttered, with a `maximize
                # price` preference behind it — and a model `price_usd eq 200` rendered
                # "price is 200 dollars" beside it. With `size eq 10` on file, a model `size
                # gte 10` did the same on the one other field `VOUCHED_FILTER_FIELDS` names.
                # The key test is also strictly redundant: every op the buyer's own words can
                # mint is in `_SETTLED_OPS`, so an exact echo is always caught here too.
                #
                # `_SETTLED_OPS` and not every op: a model proposing an op this lexicon
                # cannot mint on a field the buyer named (`material contains "merino wool"`
                # beside their own `material eq merino-wool`) is still softened and still
                # shown, because that is the model being REFUSED rather than the model
                # contradicting the shopper, and the shopper is owed the record of it.
                # "stated ... themselves" and not "settled the subject": a shopper who said
                # "over $100" settled one END of a range, and telling them they had settled
                # the whole of it would be this record making the same class of overclaim
                # the confirmation screen was repaired for.
                self._note_dropped(
                    f"the model proposed {constraint.field} {constraint.op} "
                    f"{constraint.value!r}, and the shopper had stated {constraint.field} "
                    f"themselves; theirs stands and the model's is not shown beside it"
                )
                continue
            if vouched_as_filter(constraint):
                self._add_constraint(constraint, authoritative=False)
                continue
            reading, preference = soften(
                constraint,
                reason=(
                    f"nothing in this service established {constraint.field} "
                    f"{constraint.value!r} from your own words, and it cannot check whether "
                    f"stores can answer it, so it is a preference rather than a filter that "
                    f"might exclude every store"
                ),
            )
            if reading is not None:
                self._add_softened(reading)
            if preference is not None:
                self._add_preference(preference, authoritative=False)
            if reading is None:
                self.dropped.append(
                    f"constraint {constraint.field} carries op {constraint.op!r}, which has "
                    f"no honest reading as a score direction; dropped rather than bent"
                )
        for preference in proposal.preferences:
            self._add_preference(preference, authoritative=False)
        if self.budget_band is None and proposal.budget_band:
            self.budget_band = proposal.budget_band

    def _note_dropped(self, note: str) -> None:
        """Record one refusal, once.

        The clarify loop consults the model once per round and a scripted or deterministic
        model says the same thing each time, so an unguarded ``append`` writes the identical
        sentence two and three times over. ``_add_softened`` next door has always
        de-duplicated; this list did not, and it is read by a human.
        """
        if note not in self.dropped:
            self.dropped.append(note)

    def _add_softened(self, reading: SoftenedReading) -> None:
        if any(
            existing.field == reading.field and existing.op == reading.op
            for existing in self.softened
        ):
            return
        self.softened.append(reading)

    def _learn_category(self, category: str | None) -> None:
        """Take the first category anybody names. Nothing already on file is re-judged.

        There was a re-judge here, and removing it is the point rather than a simplification.
        It dropped any constraint naming an attribute ``fixtures/catalog/coffee.json`` did not
        declare — so a shopper who said "espresso" had ``brew_method eq espresso`` deleted
        from their own confirmed intent, which the frozen golden
        ``fixtures/dialogues/espresso_needs_a_budget.json`` grades as a defect, and rightly:
        a confirmed intent is the record of what the buyer said, and this service is not
        entitled to edit it on a guess.

        The guess was also wrong. Driven through the exchange's own ``POST /auctions`` with
        the S1 roster, ``list_price lte 500``, ``boiler_type eq 'heat exchange'`` and
        ``roast_level eq dark`` — all declared by that same catalogue config — each produced
        0 slots exactly as ``brew_method`` did. What decides the question is whether the
        CANDIDATES in an auction carry a verified reading, which is a fact no buyer service
        holds and the exchange holds by construction. It decides it there now
        (:func:`exchange.ranking.rank`, ``relaxed_constraints``), and tells the buyer.
        """
        if self.category is not None or category is None:
            return
        self.category = category

    def _add_constraint(self, constraint: HardConstraint, *, authoritative: bool) -> None:
        existing = self.constraints.get(constraint.key)
        if existing is not None and not authoritative:
            return
        if authoritative:
            self.authoritative_constraints.add(constraint.key)
        if existing is not None and existing.value == constraint.value:
            return
        self.constraints[constraint.key] = constraint
        if constraint.field == "price_usd" and constraint.op == "lte":
            self._note_budget(BudgetReading(ceiling=_as_float(constraint.value)))
        elif constraint.field == "price_usd" and constraint.op == "gte":
            self._note_budget(BudgetReading(floor=_as_float(constraint.value)))

    def _add_preference(self, preference: Preference, *, authoritative: bool) -> None:
        existing = self.preferences.get(preference.key)
        if authoritative:
            self.authoritative_preferences.add(preference.key)
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
        """Every gap still open, in the order R1's questions close them.

        "Open" means *the intent is still missing this*, which is a fact about the intent
        and not about the conversation. A gap the shopper answered unusably is still open
        by this measure, and deliberately so: a shopper who answers "what's your budget?"
        with "no idea honestly" has answered, and the budget is still missing. Both are
        true and they are different facts, so :attr:`understood` carries the second rather
        than this list shifting its meaning to cover it.
        """
        open_gaps: list[str] = []
        if not self.specific:
            open_gaps.append(GAP_USE_CASE)
        if self.budget_band is None:
            open_gaps.append(GAP_BUDGET)
        if not self._has_narrowing_constraint():
            open_gaps.append(GAP_CONSTRAINTS)
        return tuple(open_gaps)

    def answered_gaps(self) -> frozenset[str]:
        """Gaps the shopper has been asked about and replied to at all."""
        return frozenset(record.gap for record in self.understood)

    def gaps_the_shopper_addressed(self) -> frozenset[str]:
        """Gaps whose answer told this service *something*, however little.

        The distinction between this and :meth:`answered_gaps` is the whole of when a
        question may be asked twice, and it is finer than it first looks.

        "not sure, something nice" is a hedge: every word of it is in ``_VAGUE`` or
        ``_FILLER``, it yields no term of any kind, and asking again is exactly what R1's
        three questions are for — ``fixtures/dialogues/maximally_vague_gift.json`` is the
        golden built around precisely that. (Its ``expected_intent`` is graded, by
        ``.swarm-loop/acceptance/test_e7_buyer.py``; its QUESTION COUNT and order were
        graded by nothing until ``test_every_shipped_dialogue_still_asks_the_number_of
        _questions_it_declares``, which this method's behaviour is what that test protects.)

        "It must be cherry wood, and at least 48 inches wide" is not a hedge. It yields a
        material this service records (softly, because nothing here can filter on it), and
        re-asking it in different words is the app telling the shopper it was not
        listening. That is the second half of the owner's screenshot.

        So: a gap is closed to further questions when the answer produced something FOR
        THAT GAP. Not merely when the answer produced something: keyed that way, "at least
        48 inches wide" answered to "what's your budget?" closed the budget — the shopper
        was never asked about money again, and the page then asserted they had answered
        about it. An answer is filed against what it produced (:attr:`GapAnswer.addressed`),
        so a subject nobody has spoken to yet stays open. A gap answered with nothing at all
        stays open too, which costs the shopper a question but never costs them a false
        claim — what they said is on :attr:`understood` either way.
        """
        return frozenset(
            gap for record in self.understood for gap in record.addressed if record.used
        )

    def next_gap(self) -> str | None:
        """The next gap worth putting to the shopper.

        A gap the shopper has already told this service something about is not asked again,
        even when what they said could not become a filter. Re-asking is how the loop spent
        two of R1's three questions on one gap and still reported it had heard nothing — the
        second half of the owner's screenshot. If the answer was real but unusable, the fix
        is to say so, not to ask the same thing in different words.

        A gap answered with a pure hedge is still open to a second question; see
        :meth:`gaps_the_shopper_addressed` for where that line falls and why.
        """
        addressed = self.gaps_the_shopper_addressed()
        for gap in self.gaps():
            if gap not in addressed:
                return gap
        return None

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

    def stated_constraints(self) -> tuple[HardConstraint, ...]:
        """Only the constraints the buyer's own literal words established."""
        return tuple(
            item for item in self.sorted_constraints() if item.key in self.authoritative_constraints
        )

    def stated_preferences(self) -> tuple[Preference, ...]:
        """Only the preferences the buyer's own literal words established."""
        return tuple(
            item for item in self.sorted_preferences() if item.key in self.authoritative_preferences
        )


def _gaps_addressed(reading: Extraction) -> tuple[str, ...]:
    """Which of R1's three subjects one utterance actually spoke to.

    The mirror of :meth:`IntentDraft.gaps`, one utterance at a time: that method asks what
    the INTENT is still missing, this one asks what this ANSWER was about. The two are
    different questions and the confirmation screen needs both — an answer can speak to a
    subject without closing it (a softened material narrows nothing and is still an answer
    about must-haves), and it can close one it was never asked about.

    Empty means the utterance produced nothing at all — a pure hedge. That is not a failure
    to classify it; it is the fact :meth:`IntentDraft.gaps_the_shopper_addressed` acts on,
    and the reason a hedge may be followed by a second question.
    """
    terms = _terms_of(reading)
    addressed: list[str] = []
    # `specific` is a token COUNT — "two content words that are neither filler nor a hedge"
    # — so on its own it credits utterances that told this service nothing. Measured, all
    # answering the budget question: "no idea honestly" and "not sure" yield no gap, while
    # "i really cannot say" and "depends honestly" clear the two-word floor and used to be
    # filed against the use-case gap. Two hedges that mean the same thing produced opposite
    # sentences on the page. An utterance that yielded no term at all addressed nothing,
    # whatever its word count; a recognised CATEGORY is a term, so it still counts.
    if reading.category is not None or (reading.specific and terms):
        addressed.append(GAP_USE_CASE)
    # Money, in either of the two shapes a money reading can take: a band on its own
    # ("around $40") or the ceiling/floor `extract` mints a `price_usd` constraint from.
    # A "cheaper is better" preference counts too — a shopper who answers "what's your
    # budget?" with "as cheap as possible" has answered about money, and re-asking is the
    # deafness this whole record exists to stop.
    if (
        not reading.budget.empty
        or any(constraint.field == "price_usd" for constraint in reading.constraints)
        or any(preference.field == "price_usd" for preference in reading.preferences)
    ):
        addressed.append(GAP_BUDGET)
    # Must-haves. A price bound is deliberately not one of them, for the same reason
    # `IntentDraft._has_narrowing_constraint` excludes it: a ceiling leaves every product in
    # the category eligible, which is what the third question exists to fix.
    if (
        any(constraint.field != "price_usd" for constraint in reading.constraints)
        or reading.softened
        or any(preference.field != "price_usd" for preference in reading.preferences)
    ):
        addressed.append(GAP_CONSTRAINTS)
    return tuple(addressed)


def _terms_of(reading: Extraction) -> tuple[str, ...]:
    """What one utterance actually yielded, in words a shopper would recognise.

    Empty means the turn produced nothing — which is the fact the confirmation screen needs
    in order to be honest about an answer it could not use.

    Every entry is printed verbatim at the shopper, inside "We kept …", so none of them may
    be a machine term. Measured before :func:`_describe_constraint` existed: the turns "I
    want a cherry wood table" / "oak, under $300" put ``We kept price usd lte 300.0,
    material is oak`` on the confirmation screen — the raw field, the raw op and a float.
    """
    used: list[str] = []
    for constraint in reading.constraints:
        used.append(_describe_constraint(constraint))
    for softened in reading.softened:
        used.append(softened.describe())
    for preference in reading.preferences:
        if any(softened.field == preference.field for softened in reading.softened):
            continue
        # The field goes through `_field_in_words` here too, not only in the constraint
        # branch: "as cheap as possible" was printing "the lowest possible price usd", and
        # "something lightweight" "the lowest possible weight grams".
        name, unit = _field_in_words(preference.field)
        spelling = _PREFERENCE_SPELLINGS.get(preference.direction, preference.direction)
        used.append(f"{spelling} {name}{f' in {unit}' if unit else ''}")
    if reading.category is not None:
        used.append(f"category {reading.category}")
    if reading.budget.band is not None and not reading.constraints:
        used.append(f"a budget around {reading.budget.band}")
    return tuple(used)


#: How an op reads in a SENTENCE, for text put in front of the shopper. Deliberately not
#: named ``_OP_SPELLINGS``: that name is already taken in this module, by the map that reads
#: a MODEL's op spelling back into R19's vocabulary, and shadowing it turned every proposed
#: ``lte`` into the string "at most" and had `_proposed_constraints` drop it as an op the
#: vocabulary cannot express.
_OP_IN_WORDS: dict[str, str] = {
    "eq": "is",
    "in": "is one of",
    "contains": "includes",
    "gte": "at least",
    "lte": "at most",
}

#: The ops that state ONE definite thing about a field — a bound or an exact value. Two of
#: them on the same field are two claims about one subject, which is what a shopper reads as
#: a contradiction. ``in`` and ``contains`` are deliberately absent: this lexicon can never
#: mint either, so a model proposing one on a field of VOCABULARY is being refused rather
#: than disagreeing, and the shopper is owed the record of the refusal.
_SETTLED_OPS: frozenset[str] = frozenset({"lte", "gte", "eq"})

#: The fields whose values are NUMBERS. On these the op does not separate a refusal from a
#: contradiction, so nothing the model says about one the shopper has already settled is
#: shown beside theirs. Measured: with "a linen duvet cover under $200" on file, a model
#: ``price_usd in [100, 200]`` walked past the op test and the page printed "price is one of
#: 100, 200" under `Budget: at most $200`; ``price_usd contains "200"`` did the same, and
#: with "running shoes size 10" on file so did ``size in [10, 11]``. A range and a bound are
#: competing claims about one number however they are spelled.
_NUMERIC_FIELDS: frozenset[str] = frozenset({"price_usd", "size"})

_PREFERENCE_SPELLINGS: dict[str, str] = {
    "minimize": "the lowest possible",
    "maximize": "the highest possible",
    "prefer": "preferably",
}


#: Field-name suffixes that are UNITS rather than part of the name. The contract asks a
#: model to put the unit in the field ("a width in inches is ``width_in``"), and the lexicon
#: mints ``price_usd`` the same way, so the tail has to be read back off or the sentence is
#: "price usd at most 300" instead of "price at most $300". The same table
#: ``intent.ts::FIELD_UNITS`` keeps for the same reason, on the other side of the wire.
_FIELD_UNITS: dict[str, str] = {
    "in": "inches",
    "cm": "cm",
    "mm": "mm",
    "ft": "feet",
    "m": "metres",
    "g": "grams",
    "grams": "grams",
    "kg": "kg",
    "lb": "lb",
    "lbs": "lb",
    "oz": "oz",
    "ml": "ml",
    "l": "litres",
    "usd": "",
    "days": "days",
    "hours": "hours",
}


def _field_in_words(field: str) -> tuple[str, str]:
    """``("width", "inches")`` for ``width_in``; ``("material", "")`` for ``material``."""
    parts = field.split("_")
    if len(parts) > 1 and parts[-1].lower() in _FIELD_UNITS:
        return " ".join(parts[:-1]), _FIELD_UNITS[parts[-1].lower()]
    return " ".join(parts), ""


def _value_in_words(value: Any) -> str:
    """A value as a shopper would write it — never a Python literal.

    Measured: "something lightweight and waterproof" put ``waterproof is True`` on the
    confirmation screen, and "size 10" put ``size is 10.0``. A capitalised ``True`` and a
    float with a trailing zero are both this service showing its insides.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple)):
        return ", ".join(_value_in_words(item) for item in value)
    return str(value)


def _describe_constraint(constraint: HardConstraint) -> str:
    """One hard constraint in the shopper's own terms, money spelled as money."""
    spelling = _OP_IN_WORDS.get(constraint.op, constraint.op)
    if constraint.field == "price_usd":
        amount = _as_float(constraint.value)
        if amount is not None:
            money = f"${amount:,.2f}".removesuffix(".00")
            return f"a budget of {spelling} {money}"
    name, unit = _field_in_words(constraint.field)
    value = _value_in_words(constraint.value)
    return f"{name} {spelling} {value}{f' {unit}' if unit else ''}".strip()


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
