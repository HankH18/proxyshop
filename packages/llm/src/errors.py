"""The exception hierarchy for ``packages/llm``.

Five tickets (T-021, T-041, T-045, T-053, T-071) build on this package, so the failure
modes are named types rather than bare ``ValueError``s: a caller can catch exactly the
thing it can handle, and the message says what to do about the rest.

Every error here is a :class:`LLMError`. Where a builtin already carries the right
meaning the concrete error inherits from it as well, so ordinary ``except KeyError`` /
``except ValueError`` handling keeps working:

======================================  ==============================  ================
Error                                   Also inherits                   Raised by
======================================  ==============================  ================
:class:`UnknownRoleError`               ``KeyError``                    ``llm.config``
:class:`ProviderNotConfiguredError`     ``ValueError``                  ``llm.config``
:class:`MissingApiKeyError`             ``RuntimeError``                ``llm.client``
:class:`PromptAssemblyError`            ``ValueError``                  ``llm.prompting``
:class:`RecordingError`                 ``ValueError``                  ``llm.recordings``
:class:`UnrecordedPromptError`          ``KeyError``                    ``llm.doubles``
======================================  ==============================  ================

:class:`UnrecordedPromptError` is the important one. The recorded double NEVER falls back
to a live model for a prompt it does not know (D20/D3) — it raises this instead, which is
also what the frozen acceptance suite asserts.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class LLMError(Exception):
    """Base class for every error raised by ``packages/llm``."""


class _MessageError(LLMError):
    """An error whose ``str()`` is its message, even when it also inherits ``KeyError``.

    ``KeyError.__str__`` is ``repr(args[0])``, which would wrap a carefully written
    multi-line explanation in quotes and escape every newline. Anything that mixes in
    ``KeyError`` therefore goes through here.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        """Make the error picklable and copyable.

        Every subclass here overrides ``__init__`` with arguments that are not
        ``self.args``, and the default unpickling path calls ``cls(*args)`` — which raised
        ``TypeError: __init__() missing 1 required positional argument`` instead of
        re-raising the original error. Five tickets depend on this package; a retry
        wrapper doing ``copy.copy(exc)``, or any process boundary, would have turned a
        carefully written message into an opaque TypeError.
        """
        return (self.__class__, (self.message,))


class UnknownRoleError(_MessageError, KeyError):
    """A role with no configured model. Raised by :func:`llm.config.resolve_model`."""

    def __init__(self, role: object, known_roles: Iterable[str]) -> None:
        self.known_roles = tuple(known_roles)
        known = ", ".join(sorted(self.known_roles))
        super().__init__(f"unknown LLM role {role!r}; the configured roles are: {known}")
        self.role = role

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        """Keep the error picklable and copyable — see :class:`_MessageError`."""
        return (self.__class__, (self.role, self.known_roles))


class ProviderNotConfiguredError(LLMError, ValueError):
    """``LLM_PROVIDER`` names a provider this package does not implement."""


class MissingApiKeyError(LLMError, RuntimeError):
    """The live provider was selected but no API key is available (D3: none exists here)."""


class PromptAssemblyError(LLMError, ValueError):
    """A prompt could not be assembled — usually an empty static context (C4)."""


class RecordingError(LLMError, ValueError):
    """A recorded-fixture file is missing, malformed, or has no provenance header (D21)."""


class UnrecordedPromptError(_MessageError, KeyError):
    """A double was asked for a prompt it has no recording of.

    This is deliberately fatal. Improvising a reply would make a test pass on a prompt
    nobody reviewed, and falling through to a live client would break D3.
    """

    def __init__(self, message: str, prompt: str = "") -> None:
        super().__init__(message)
        self.prompt = prompt

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        """Keep the error picklable and copyable — see :class:`_MessageError`."""
        return (self.__class__, (self.message, self.prompt))
