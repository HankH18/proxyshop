"""Reading fields off "whatever the caller had" (T-072).

Everything this package is handed crosses a seam it does not own: a shortlist slot arrives
as the exchange's ``model_dump(mode="json")`` dict in production, as a ``ShortlistSlot``
model in a unit test, and as an ad-hoc object in the frozen acceptance suite. The exchange's
own ranking package solves this with ``exchange.ranking.filters.read``; this is the same
idea, kept local because ``buyer_svc`` importing ``exchange`` would make an HTTP seam into a
Python one.

Two rules that are easy to get wrong and are the reason this is a module rather than a
lambda:

* **Mapping first, attribute second.** A ``dict`` has a ``.get`` attribute and a ``.items``
  attribute; reading ``slot.items`` off one because the key lookup was tried second returns
  a bound method, not data.
* **Never let a caller's ``__getitem__`` or ``__getattr__`` raise out of here.** Both are
  hooks on an object this package did not build. A slot whose ``__getitem__`` raises is a
  slot with no such field, not an exception on the refusal path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["plain", "read", "text"]

_MISSING = object()


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

    ``None`` and a missing field both flatten to ``""`` on purpose: every caller here asks
    "is there a usable string?", and three spellings of absence would be three branches.
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


def plain(value: Any, _depth: int = 0) -> Any:
    """Reduce a model / mapping / sequence to plain JSON-ish data. Cannot raise."""
    if _depth > 8 or value is None or isinstance(value, (str, bool, int, float)):
        return value
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                value = fn()
                break
            except Exception:  # noqa: BLE001 - fall through to the next shape
                continue
    if isinstance(value, Mapping):
        return {str(k): plain(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(v, _depth + 1) for v in value]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return text(value)
