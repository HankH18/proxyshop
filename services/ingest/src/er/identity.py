"""Identifier and text canonicalisation for entity resolution (T-022, DESIGN §Data models).

Entity resolution is a *matching* problem, and every matching problem is really a
normalisation problem wearing a scoring function. Two records that name the same trade item
almost never say so in the same bytes: one storefront serves the UPC-A ``012345678905`` and
the next serves the GTIN-14 ``00012345678905``; one titles a shoe ``Café Runner`` and the
next ``CAFE RUNNER``. This module is the single place those differences are folded away, so
the scorer in :mod:`ingest.er.matching` compares meanings rather than encodings.

Three decisions here are load-bearing and are defended below rather than in a commit message.

**A GTIN is only usable when its check digit validates.**
    GS1 puts a modulo-10 check digit in the last position of every GTIN precisely so a
    transcription error is detectable. ER without that check is dangerous in a way ER with it
    is not: :func:`normalize_gtin` is the input to an *identity* rule that links two products
    with confidence ``1.0`` and no appeal to their names, so a single mistyped digit shared by
    two unrelated records would merge two different products' offers into one buyer-facing
    listing. A GTIN that does not validate is therefore not "a GTIN we should trust less" — it
    is not an identifier at all, and the pair falls through to the similarity path where the
    names still have to agree. The cost is a missed link on genuinely corrupt data; the
    alternative cost is a silent false merge, and those are not symmetric.

**The all-zero payload is refused even though its check digit is valid.**
    ``00000000000000`` passes the modulo-10 check (an empty sum has check digit zero), and
    stores emit it constantly as the default value of an unset field. Accepting it would link
    *every* product that never filled its GTIN in to every other one — the single worst
    failure this module can have, and one no check digit catches.

**Digits are ASCII digits, not ``str.isdigit()``.**
    ``"１２３".isdigit()`` is ``True`` and ``int("１２３")`` is ``123``: full-width and other
    Unicode digit forms would otherwise reach the check-digit arithmetic through a different
    door than the one this module thinks it opened. NFKC normalisation runs first and folds
    the legitimate full-width case to ASCII on purpose; whatever is *still* non-ASCII after
    that is not an identifier and is refused.

:func:`fold` layers accent-stripping and a Cyrillic/Greek homoglyph table on top of the
graph's own :func:`~ingest.graph.model.canonical_text`. That is not decoration: ``Тrail``
with a Cyrillic Т renders identically to ``Trail`` and is the cheapest way for a knock-off
listing to evade being merged with the product it copies.

    >>> normalize_gtin("0-12345-67890-5")
    '00012345678905'
    >>> normalize_gtin("00099999999999")   # check digit should be 0, not 9
    ''
    >>> tokens("Trail Runner Shoe")
    ('trail', 'runner', 'shoe')
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..graph.model import canonical_text

__all__ = [
    "CONFUSABLE_FOLD",
    "GTIN_LENGTHS",
    "canonical_text",
    "fold",
    "gtin_check_digit",
    "is_valid_gtin",
    "normalize_gtin",
    "tokens",
]

#: The GTIN lengths GS1 defines: GTIN-8 (EAN-8), GTIN-12 (UPC-A), GTIN-13 (EAN-13), GTIN-14.
#: Every one of them zero-pads losslessly to 14, which is why 14 is the canonical form: the
#: check digit is computed from the right, so leading zeros contribute nothing to it and a
#: UPC-A and its GTIN-14 rendering are literally the same number.
GTIN_LENGTHS = frozenset({8, 12, 13, 14})

#: Characters a store may put *between* the digit groups of a printed barcode number:
#: space, no-break space, narrow no-break space, hyphen-minus, Unicode hyphen, en dash
#: and underscore. They are removed; anything else that is not an ASCII digit
#: disqualifies the value outright.
_GTIN_SEPARATORS = frozenset(" \xa0\u202f-\u2010\u2013_")

_ASCII_DIGITS = frozenset("0123456789")

#: Lower-case Cyrillic and Greek letters that are visually indistinguishable from a Latin
#: letter, mapped to the Latin one. Applied *after* case folding, so only the lower-case forms
#: need an entry. Deliberately small: this table exists to defeat homoglyph evasion in product
#: titles, not to transliterate Russian or Greek, and every entry it does not contain simply
#: stays as it is and lowers the similarity of an honest non-Latin title by nothing.
CONFUSABLE_FOLD = str.maketrans(
    {
        "а": "a",
        "в": "b",
        "с": "c",
        "ԁ": "d",
        "е": "e",
        "ѕ": "s",
        "һ": "h",
        "і": "i",
        "ј": "j",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "т": "t",
        "у": "y",
        "х": "x",
        "ѵ": "v",
        "α": "a",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "μ": "m",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "y",
        "χ": "x",
    }
)

#: Token separators. ``\W`` is Unicode-aware for ``str`` patterns, so a non-Latin title
#: keeps its words instead of collapsing into one token, while punctuation, quotes and
#: whitespace all split. ``_`` is added because it is a word character to ``re`` and a
#: separator to every product title ever written.
_NON_TOKEN = re.compile(r"[\W_]+")

#: Unicode general categories that render as nothing: format characters (zero-width
#: space and joiners, soft hyphen, bidi overrides, the BOM) and control characters. They
#: are removed by :func:`fold` — see its docstring for why an invisible title is a real
#: hazard rather than a curiosity.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc"})

_WHITESPACE = re.compile(r"\s+")


def gtin_check_digit(payload: str) -> int:
    """The GS1 modulo-10 check digit for ``payload`` — every digit *except* the check digit.

    Args:
        payload: the leading digits of a GTIN, ASCII, check digit already removed.

    Returns:
        The digit that must occupy the final position.

    Raises:
        ValueError: ``payload`` contains a character that is not an ASCII digit.
    """
    total = 0
    for position, digit in enumerate(reversed(payload)):
        if digit not in _ASCII_DIGITS:
            raise ValueError(f"not an ASCII digit string: {payload!r}")
        total += int(digit) * (3 if position % 2 == 0 else 1)
    return (10 - total % 10) % 10


def normalize_gtin(value: Any) -> str:
    """Reduce a store-supplied GTIN to the canonical GTIN-14, or ``""`` when it is not one.

    ``""`` is the module's single answer to every way a GTIN can fail to be an identifier —
    absent, blank, the wrong length, non-numeric, check-digit invalid, or the all-zero
    placeholder — because the caller's response to all of them is identical: do not use the
    identity rule, fall through to the similarity path. A caller that needs to distinguish
    "absent" from "malformed" is asking a data-quality question, not a matching one.

    Args:
        value: whatever the record carried under ``gtin``. Any type; ``None`` and ``bool`` are
            refused outright (``str(True)`` is ``"True"``, and a store that serves
            ``"gtin": true`` must not be given an identifier).

    Returns:
        Fourteen ASCII digits, or ``""``.
    """
    if value is None or isinstance(value, bool):
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    digits = "".join(character for character in text if character not in _GTIN_SEPARATORS)
    if not digits or any(character not in _ASCII_DIGITS for character in digits):
        return ""
    if len(digits) not in GTIN_LENGTHS:
        return ""
    padded = digits.rjust(14, "0")
    if gtin_check_digit(padded[:-1]) != int(padded[-1]):
        return ""
    if set(padded[:-1]) == {"0"}:
        # A valid check digit over an empty payload. This is the unset-field placeholder, not
        # a trade item, and admitting it would link every GTIN-less product to every other.
        return ""
    return padded


def is_valid_gtin(value: Any) -> bool:
    """Whether ``value`` is a usable GTIN — see :func:`normalize_gtin` for what that means."""
    return bool(normalize_gtin(value))


def fold(value: Any) -> str:
    """Fold text to the form similarity is measured over.

    Builds on the graph's :func:`~ingest.graph.model.canonical_text` (NFKC, case-folded,
    whitespace-collapsed) so that ER and the graph agree on what "the same string" means,
    then adds the three folds a *matcher* needs and a content hash must not have:

    * **combining marks are dropped**, so ``café`` and ``cafe`` are one word rather than two;
    * **homoglyphs are folded to Latin**, so a title cannot dodge resolution by swapping a
      Cyrillic ``о`` for a Latin ``o``;
    * **invisible characters are removed** — Unicode categories ``Cf`` and ``Cc``: zero-width
      space and joiners, soft hyphen, bidi marks, the BOM, C0/C1 controls. This one is a
      guard, not a nicety. ``"\u200b"`` is not whitespace to ``str.strip()`` and not
      whitespace to ``\s``, so a title consisting of one zero-width space survives every
      emptiness check written the obvious way, folds equal to any other such title, and
      would have two invisible-titled products scoring a *perfect* name match against each
      other. Removing them means a name made only of invisibles folds to ``""`` and is
      correctly treated as no name at all.

    All three are lossy, which is exactly why they live here and not in ``canonical_text``: a
    content hash must distinguish bytes the store actually served, and a matcher must not.

    Args:
        value: raw observed text. ``None`` and non-strings fold to ``""``.

    Returns:
        The folded form, or ``""``.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, str):
        return ""
    decomposed = unicodedata.normalize("NFD", canonical_text(value))
    visible = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
        and unicodedata.category(character) not in _INVISIBLE_CATEGORIES
    )
    recomposed = unicodedata.normalize("NFC", visible).translate(CONFUSABLE_FOLD)
    return _WHITESPACE.sub(" ", recomposed).strip()


def tokens(value: Any) -> tuple[str, ...]:
    """The folded word tokens of ``value``, in order, with separators removed.

    Order is preserved even though the scorer compares sets: a tuple keeps this function's
    output usable for anything that cares about sequence, and set construction is the
    caller's cheap step, not this function's opinion.
    """
    folded = fold(value)
    if not folded:
        return ()
    return tuple(part for part in _NON_TOKEN.split(folded) if part)
