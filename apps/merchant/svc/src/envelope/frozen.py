"""Deep-frozen JSON values — what makes "an old envelope version is immutable" *structural*.

R9 says a previous envelope version never changes. The cheap way to claim that is to
deep-copy on the way in and hope nothing reaches inside afterwards; the honest way is for
there to be no mutable object to reach. Every nested value an :class:`~merchant_svc.envelope.model.Envelope`
holds goes through :func:`freeze` first, so a floor list is a ``tuple`` and a commitment is a
:class:`FrozenDict`, and the failure mode where two versions share one list — edit v2's
floors, watch v1's floors change — cannot be written.

:func:`thaw` is the only way back out, and it always builds fresh containers, so a caller
that mutates what ``to_dict()`` handed it is mutating its own copy.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any


class FrozenDict(Mapping[str, Any]):
    """An immutable string-keyed mapping whose values are themselves frozen.

    It is a :class:`~collections.abc.Mapping` rather than a ``dict`` subclass on purpose: a
    ``dict`` subclass inherits ``__setitem__``/``update``/``clear`` and would have to
    override each one, and any it missed would be a hole in the guarantee.
    """

    __slots__ = ("_data",)

    #: Annotation only — the slot above is the storage. Declared so a type checker can see
    #: through the ``object.__setattr__`` the immutability guard below forces on ``__init__``.
    _data: dict[str, Any]

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        object.__setattr__(self, "_data", dict(data or {}))

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenDict({self._data!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FrozenDict):
            return self._data == other._data
        if isinstance(other, Mapping):
            return self._data == dict(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(tuple(sorted((key, _hashable(value)) for key, value in self._data.items())))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"FrozenDict is immutable; cannot set {name!r}")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"FrozenDict is immutable; cannot delete {name!r}")

    # An immutable value has nothing to copy. Returning ``self`` also keeps `copy.deepcopy`
    # away from the slots/`__setattr__` reconstruction path, which would otherwise trip the
    # guard above.
    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        return self


def _hashable(value: Any) -> Any:
    """``value`` in a form :func:`hash` accepts — frozen values already are, bar lists."""
    if isinstance(value, list):  # pragma: no cover - freeze() never leaves one behind
        return tuple(_hashable(item) for item in value)
    return value


def freeze(value: Any) -> Any:
    """Return ``value`` with every mapping a :class:`FrozenDict` and every sequence a tuple.

    Args:
        value: JSON-shaped data — mappings, sequences, strings, numbers, booleans, ``None``.

    Returns:
        The same data, deeply immutable.

    Raises:
        TypeError: ``value`` contains something that is not JSON-shaped (bytes, a set, an
            arbitrary object). An envelope is a document, and a document that cannot be
            serialized is not one — refusing here is what stops an unserializable value from
            reaching the digest, where it would raise far from its cause.
    """
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict({str(key): freeze(item) for key, item in value.items()})
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        raise TypeError("an envelope holds JSON, and bytes are not JSON")
    if isinstance(value, Sequence):
        return tuple(freeze(item) for item in value)
    raise TypeError(f"an envelope cannot hold a {type(value).__name__}")


def thaw(value: Any) -> Any:
    """Return ``value`` as ordinary, mutable, JSON-serializable Python.

    Every container is rebuilt, so the result shares nothing with the frozen original.
    """
    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (tuple, list)):
        return [thaw(item) for item in value]
    return value
