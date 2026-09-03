"""Canonicalisation: make two ways of saying the same thing compare equal.

R18. A seller writes "0.5 kg" and the catalog records ``{"value": 500, "unit": "g"}``. Those
are the same fact, and a verifier that calls the claim *contradicted* because the strings
differ is worse than no verifier at all — it manufactures dishonesty out of formatting and
teaches everyone to ignore it.

So every comparison runs on canonical forms:

* **quantities** are parsed into ``(number, unit)`` and converted to their family's base unit,
  so grams and kilograms are one scale and bar and psi are another;
* **booleans** accept the spellings a pitch actually uses ("Yes", "true", "1");
* **strings** are casefolded and their whitespace collapsed, so "Cotton" and " cotton "
  match while "cotton" and "linen" still do not.

What canonicalisation deliberately does NOT do is guess. A value it cannot parse comes back
as ``None`` and the caller turns that into ``ambiguous`` — an honest "this could not be
decided" — rather than falling through to a string comparison that would call every
unparseable claim contradicted.

Standard library only, and no LLM: a comparator that asked a model whether two values matched
would be a comparator whose verdict an injected instruction in the pitch text could change
(C10).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "BOOLEAN_FALSE",
    "BOOLEAN_TRUE",
    "UNIT_FAMILIES",
    "attribute_value",
    "normalize_boolean",
    "normalize_text",
    "parse_quantity",
    "to_base_unit",
    "unit_family",
]

#: Convertible unit families, each mapping a spelling onto a factor towards the family's base
#: unit (the one with factor 1.0). Currencies are deliberately absent: "USD" is not a physical
#: unit, and a claim of ``389.00`` against ``{"value": 389.0, "unit": "USD"}`` must compare as
#: two bare numbers rather than as a unit mismatch.
UNIT_FAMILIES: dict[str, dict[str, float]] = {
    "mass": {
        "mg": 0.001,
        "g": 1.0,
        "gram": 1.0,
        "grams": 1.0,
        "gramme": 1.0,
        "grammes": 1.0,
        "kg": 1000.0,
        "kilogram": 1000.0,
        "kilograms": 1000.0,
        "oz": 28.349523125,
        "ounce": 28.349523125,
        "ounces": 28.349523125,
        "lb": 453.59237,
        "lbs": 453.59237,
        "pound": 453.59237,
        "pounds": 453.59237,
    },
    "length": {
        "mm": 0.001,
        "cm": 0.01,
        "m": 1.0,
        "metre": 1.0,
        "meter": 1.0,
        "km": 1000.0,
        "in": 0.0254,
        "inch": 0.0254,
        "inches": 0.0254,
        "ft": 0.3048,
        "foot": 0.3048,
        "feet": 0.3048,
    },
    "volume": {
        "ml": 0.001,
        "cl": 0.01,
        "l": 1.0,
        "liter": 1.0,
        "liters": 1.0,
        "litre": 1.0,
        "litres": 1.0,
    },
    "pressure": {
        "bar": 1.0,
        "pa": 1e-5,
        "kpa": 0.01,
        "psi": 0.0689475729,
    },
    "power": {"w": 1.0, "watt": 1.0, "watts": 1.0, "kw": 1000.0},
    "potential": {"v": 1.0, "volt": 1.0, "volts": 1.0, "kv": 1000.0},
    "time": {
        "s": 1.0,
        "sec": 1.0,
        "second": 1.0,
        "seconds": 1.0,
        "min": 60.0,
        "minute": 60.0,
        "minutes": 60.0,
        "h": 3600.0,
        "hr": 3600.0,
        "hour": 3600.0,
        "hours": 3600.0,
        "day": 86400.0,
        "days": 86400.0,
        "week": 604800.0,
        "weeks": 604800.0,
        "month": 2629800.0,
        "months": 2629800.0,
        "year": 31557600.0,
        "years": 31557600.0,
    },
}

#: Spellings a pitch actually uses for a boolean. Not an exhaustive natural-language parser —
#: anything outside these two sets is ``None``, i.e. "could not be decided".
BOOLEAN_TRUE: frozenset[str] = frozenset({"true", "yes", "y", "1", "on", "present", "included"})
BOOLEAN_FALSE: frozenset[str] = frozenset({"false", "no", "n", "0", "off", "absent", "excluded"})

_QUANTITY = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*$")
_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: Any) -> str:
    """Casefolded, whitespace-collapsed text. The canonical form string equality runs on."""
    return _WHITESPACE.sub(" ", str(value).strip()).casefold()


def normalize_boolean(value: Any) -> bool | None:
    """``True``/``False`` for a recognised boolean spelling, else ``None``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = normalize_text(value)
    if text in BOOLEAN_TRUE:
        return True
    if text in BOOLEAN_FALSE:
        return False
    return None


def unit_family(unit: Any) -> tuple[str, float] | None:
    """``(family, factor-to-base)`` for a unit spelling, or ``None`` when it is not one."""
    if unit is None:
        return None
    text = normalize_text(unit).replace(".", "")
    if not text:
        return None
    for family, spellings in UNIT_FAMILIES.items():
        factor = spellings.get(text)
        if factor is not None:
            return family, factor
    return None


def parse_quantity(value: Any) -> tuple[float, str | None] | None:
    """``(number, unit-text-or-None)`` for anything that reads as a quantity.

    ``500`` -> ``(500.0, None)``; ``"0.5 kg"`` -> ``(0.5, "kg")``; ``"389.00"`` ->
    ``(389.0, None)``; ``"your mains voltage"`` -> ``None``. A trailing remainder that is not
    a recognised unit is still returned as the unit text, so the caller can distinguish "no
    unit" from "a unit I do not know" — the second is a reason to be careful, the first is not.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value), None
    match = _QUANTITY.match(str(value))
    if match is None:
        return None
    try:
        number = float(match.group(1))
    except ValueError:  # pragma: no cover - the regex already constrains this
        return None
    remainder = match.group(2).strip()
    return number, remainder or None


def to_base_unit(number: float, unit: Any) -> tuple[float, str | None]:
    """``(value-in-base-units, family)``. An unrecognised unit is returned unconverted."""
    resolved = unit_family(unit)
    if resolved is None:
        return float(number), None
    family, factor = resolved
    return float(number) * factor, family


def attribute_value(attribute: Any) -> tuple[Any, Any]:
    """``(value, unit)`` out of a catalog attribute record.

    Catalog attributes are ``{"value": ..., "unit": ...}`` with ``unit`` optional, but a bare
    scalar (as the ``offer`` block carries) is accepted too, so one comparator serves both.
    """
    if isinstance(attribute, Mapping) and "value" in attribute:
        return attribute.get("value"), attribute.get("unit")
    return attribute, None
