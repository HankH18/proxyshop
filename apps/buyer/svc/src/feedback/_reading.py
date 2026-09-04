"""Reading fields off "whatever the caller had" (T-073).

An order record crosses a seam this package does not own. It arrives as the exchange's
``model_dump(mode="json")`` dict in production, as a plain literal in a unit test, and as an
ad-hoc mapping in the frozen acceptance suite. :mod:`buyer_svc.accept._reading` solves the
same problem for a shortlist slot; this is a deliberate second copy rather than an import of
another package's private module, kept to the three helpers this package actually needs.

Two rules that are easy to get wrong and are the reason this is a module rather than a lambda:

* **Mapping first, attribute second.** A ``dict`` has a ``.get`` attribute and a ``.items``
  attribute; reading ``order.items`` off one because the key lookup was tried second returns a
  bound method, not data.
* **Never let a caller's ``__getitem__`` or ``__getattr__`` raise out of here.** Both are hooks
  on an object this package did not build. An order whose ``__getitem__`` raises is an order
  with no such field, not an exception on the refusal path.

:func:`flag` is the third helper and the one with teeth. It exists because pydantic's lax mode
coerces the JSON strings ``"yes"``, ``"on"`` and ``"1"`` to ``True`` — measured on this tree at
2.13, where it turned a body carrying no boolean into a live auction on T-071's confirm route —
and because plain ``bool(value)`` reads the string ``"false"`` as ``True``. R14's guarantee is a
NEGATIVE one, so a truthiness bug here offers a feedback prompt for an order the network never
routed. :func:`flag` answers ``True``/``False`` only for a real boolean or an unambiguous
spelling of one, and ``None`` — "this record does not say" — for everything else.
"""

from __future__ import annotations

from collections.abc import Mapping
from numbers import Number
from typing import Any

__all__ = ["FALSE_WORDS", "TRUE_WORDS", "first", "flag", "read", "text"]

_MISSING = object()

#: Spellings of ``True`` a JSON body legitimately arrives with. Anything outside these two sets
#: is "this record does not say", never a coerced ``True``.
TRUE_WORDS: frozenset[str] = frozenset({"true", "yes", "y", "t", "1", "on", "routed"})

#: Spellings of ``False``. ``""`` is deliberately absent: an empty string is an absent answer,
#: which :func:`flag` reports as ``None`` so a caller can fall back to other evidence.
FALSE_WORDS: frozenset[str] = frozenset({"false", "no", "n", "f", "0", "off", "unrouted"})


def read(holder: Any, key: str, default: Any = None) -> Any:
    """``holder[key]`` or ``holder.key``, else ``default``. Cannot raise."""
    if holder is None:
        return default
    if isinstance(holder, Mapping):
        try:
            value = holder.get(key, _MISSING)
        except Exception:  # noqa: BLE001 - a caller-supplied mapping is not trusted to work
            value = _MISSING
        if value is not _MISSING:
            return value
        return default
    try:
        value = getattr(holder, key, _MISSING)
    except Exception:  # noqa: BLE001 - `__getattr__` is a caller-supplied hook
        return default
    return default if value is _MISSING else value


def text(value: Any) -> str:
    """``str(value).strip()`` for a value from across the seam, or ``""``. Cannot raise.

    ``None`` and a missing field both flatten to ``""`` on purpose: every caller here asks "is
    there a usable string?", and three spellings of absence would be three branches.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    inner = getattr(value, "value", None)  # StrEnum members and friends
    if isinstance(inner, str):
        return inner.strip()
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001 - an unrenderable value is an absent one
        return ""


def first(holder: Any, keys: tuple[str, ...]) -> str:
    """The first of ``keys`` that reads as non-empty text on ``holder``, else ``""``."""
    for key in keys:
        found = text(read(holder, key))
        if found:
            return found
    return ""


def flag(value: Any) -> bool | None:
    """``True``/``False`` for an unambiguous boolean, ``None`` for "this record does not say".

    Never ``bool(value)``. The string ``"false"`` is truthy in Python and pydantic's lax mode
    turns ``"yes"`` into ``True``; both readings would hand a feedback prompt to an order the
    network did not route, which is the one thing R14 forbids. A ``0``/``1`` of any numeric
    type is accepted (a SQL driver's boolean, a JSON ``0.0``, a ``numpy.bool_`` out of a
    dataframe, a ``Decimal``), and any other number, list or object is ``None`` rather than a
    guess. ``None`` is not a permissive answer here: :func:`buyer_svc.feedback.routing.routing`
    requires an affirmative ``True`` and refuses everything else.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in TRUE_WORDS:
            return True
        if lowered in FALSE_WORDS:
            return False
        return None
    if isinstance(value, (int, float)) and value in (0, 1):
        # ``float`` as well as ``int``, and this was a MEASURED hole rather than a completeness
        # exercise: a numeric column or a JSON encoder that writes ``0.0`` delivers ``routed``
        # as a float, and reading ``0.0`` as "this record does not say" sent it on to be read as
        # ROUTED. ``0.0 == 0`` is true, so the membership test covers both once the isinstance
        # does.
        return bool(value)
    item = getattr(value, "item", None)
    if callable(item):
        # A numpy scalar: ``numpy.False_`` and ``numpy.int64(0)`` are neither ``bool`` nor
        # ``int``/``float`` subclasses, so every check above misses them, and numpy is a
        # dependency of this repo. ``.item()`` is the documented way to get the Python value out
        # of one, and it is duck-typed here rather than imported so this module stays free of
        # numpy. One level only — ``.item()`` returns a builtin.
        try:
            unwrapped = item()
        except Exception:  # noqa: BLE001 - an object with an unusable `.item` is simply unread
            return None
        if isinstance(unwrapped, (bool, int, float, str)):
            return flag(unwrapped)
        return None
    if isinstance(value, Number) and not isinstance(value, complex):
        # ``Decimal("0")`` and ``Fraction(1)`` reach here: numbers that are not ``int``/``float``
        # subclasses. Compared rather than cast, so a value that is neither 0 nor 1 stays unread.
        try:
            if value == 0 or value == 1:
                return bool(value)
        except Exception:  # noqa: BLE001 - a number whose __eq__ raises is not an answer
            return None
    return None
