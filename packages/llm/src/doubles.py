"""Offline test doubles for the LLM client (D20: the double is the default provider).

Two doubles, both of which satisfy :class:`llm.client.LLMClient` and neither of which can
reach the network — there is no socket call anywhere in this module, and no import of
anything that opens one:

* :class:`RecordedLLM` — replays a hand-authored recording table (D21). An exact prompt
  match returns the reviewed reply; **anything else raises**. It never improvises and
  never falls through to a live client, because a test that silently invented an answer
  would be asserting on nothing.
* :class:`DeterministicLLM` — answers any prompt with a stable function of
  ``(role, prompt)``. For code paths that must run offline but whose reply text nobody
  asserts on. The reply shape matches ``proxyshop_support.llm_double.LLMDouble`` so the
  two agree: ``double:<role>:<16 hex chars>``.

Both record every call in ``.calls``, which is how a test asserts on *prompt assembly* —
e.g. that the static store context precedes the dynamic tail (C4).
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from llm.errors import UnrecordedPromptError
from llm.recordings import load_recording

#: How many near-miss prompts an :class:`UnrecordedPromptError` suggests.
SUGGESTION_COUNT = 3


@dataclass(frozen=True)
class LLMCall:
    """One invocation of a double, kept so tests can assert on what was sent.

    ``frozen=True`` stops the *fields* being rebound; it does not deep-freeze
    :attr:`kwargs`, which is a plain dict and can be mutated in place (that also makes
    the instance unhashable). The dict is a copy taken at call time, so mutating it
    cannot reach back into the caller's arguments.
    """

    prompt: str
    role: str | None = None
    system: str | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)


def prompt_text(prompt: Any) -> str:
    """Normalise anything a caller may pass as a prompt to the exact string sent.

    A :class:`llm.prompting.CachedPrompt` becomes its assembled ``text`` — static context
    first — so a recording key is the prompt as assembled, not as split for the wire.
    (:class:`llm.client.AnthropicLLM` sends the two halves as a system block and a user
    turn, so the assembled string is what a human reads and what the doubles key on, not
    a byte-for-byte copy of the request body.)
    """
    if isinstance(prompt, str):
        return prompt
    text = getattr(prompt, "text", None)
    if isinstance(text, str):
        return text
    return str(prompt)


class _RecordingBase:
    """Shared call bookkeeping."""

    def __init__(self) -> None:
        self.calls: list[LLMCall] = []

    def _record(
        self, text: str, role: str | None, system: str | None, kwargs: dict[str, Any]
    ) -> None:
        self.calls.append(LLMCall(prompt=text, role=role, system=system, kwargs=dict(kwargs)))

    @property
    def last_prompt(self) -> str:
        """The most recent prompt string. Raises ``IndexError`` if nothing was asked."""
        return self.calls[-1].prompt

    def prompts(self) -> list[str]:
        """Every prompt string sent to this double, in order."""
        return [call.prompt for call in self.calls]

    def reset(self) -> None:
        """Forget the recorded calls. Recordings are untouched."""
        self.calls.clear()


class RecordedLLM(_RecordingBase):
    """Replays a fixed prompt→reply table. Deterministic, offline, and strict.

    Args:
        recordings: exact prompt → reply. Copied on construction, so a later mutation of
            the caller's dict cannot change what this double replays.
        role: optional role label, recorded on every call for assertions.
        name: optional label used in error messages (the fixture stem, usually).

    Determinism is total: the same prompt returns the same string on every call, on every
    instance built from the same table, in every process. There is no queue, no ordering,
    and no internal state that a reply depends on.

    Example::

        double = RecordedLLM({"summarize the envelope": "floors respected"})
        assert double.complete("summarize the envelope") == "floors respected"
        double.complete("something else")   # -> UnrecordedPromptError
    """

    def __init__(
        self,
        recordings: Mapping[str, str],
        *,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__()
        if not isinstance(recordings, Mapping):
            raise TypeError(
                f"RecordedLLM takes a mapping of prompt -> reply, got {type(recordings).__name__}"
            )
        table: dict[str, str] = {}
        for prompt, reply in recordings.items():
            if not isinstance(prompt, str) or not isinstance(reply, str):
                raise TypeError(
                    f"recordings must map str -> str; got "
                    f"{type(prompt).__name__} -> {type(reply).__name__}"
                )
            table[prompt] = reply
        self._recordings = table
        self.role = role
        self.name = name

    @classmethod
    def from_fixture(cls, name: str, *, role: str | None = None) -> RecordedLLM:
        """Build a double from ``packages/llm/fixtures/recorded/<name>.json`` (D21)."""
        return cls(load_recording(name), role=role, name=name)

    @property
    def recordings(self) -> Mapping[str, str]:
        """Read-only view of the recording table."""
        return MappingProxyType(self._recordings)

    def complete(
        self,
        prompt: Any,
        *,
        role: str | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Return the recorded reply for ``prompt``.

        Args:
            prompt: a string, or a :class:`llm.prompting.CachedPrompt` (its assembled
                text is the lookup key).
            role: recorded on the call; does not affect the lookup.
            system: recorded on the call; does not affect the lookup.
            **kwargs: recorded on the call; accepted so this double is drop-in for the
                live client's signature.

        Raises:
            UnrecordedPromptError: for any prompt with no recording. This is a
                ``KeyError``, never a network error — the double has no client to fall
                back to, by construction.
        """
        text = prompt_text(prompt)
        self._record(text, role if role is not None else self.role, system, kwargs)
        try:
            return self._recordings[text]
        except KeyError:
            raise self._miss(text) from None

    def complete_json(self, prompt: Any, **kwargs: Any) -> Any:
        """:meth:`complete`, parsed as JSON. Raises ``json.JSONDecodeError`` on garbage."""
        return json.loads(self.complete(prompt, **kwargs))

    def _miss(self, text: str) -> UnrecordedPromptError:
        source = f" ({self.name})" if self.name else ""
        suggestions = difflib.get_close_matches(
            text, list(self._recordings), n=SUGGESTION_COUNT, cutoff=0.4
        )
        hint = (
            "\nclosest recorded prompts:\n"
            + "\n".join(f"  - {candidate[:160]!r}" for candidate in suggestions)
            if suggestions
            else "\n(no recorded prompt is close to it)"
        )
        return UnrecordedPromptError(
            f"RecordedLLM{source} has no recording for this prompt, and never invents one "
            f"or calls a live model (D20/D21). Add it to the fixture, or pass it in the "
            f"recordings dict.\nprompt ({len(text)} chars): {text[:400]!r}\n"
            f"{len(self._recordings)} recording(s) available.{hint}",
            prompt=text,
        )

    def __repr__(self) -> str:
        label = f" name={self.name!r}" if self.name else ""
        return f"<RecordedLLM{label} recordings={len(self._recordings)} calls={len(self.calls)}>"


class DeterministicLLM(_RecordingBase):
    """Answers any prompt with a stable, offline, content-derived string.

    Args:
        role: the role label used in the reply and recorded on every call.
        default: a fixed reply for every prompt. ``None`` selects :meth:`deterministic`.

    This is what :func:`llm.client.build_llm` returns for ``LLM_PROVIDER=double`` when no
    recordings were supplied: code that must *run* offline gets a reply, and code that
    asserts on the reply uses :class:`RecordedLLM` instead.
    """

    def __init__(self, *, role: str = "default", default: str | None = None) -> None:
        super().__init__()
        self.role = role
        self._default = default

    @staticmethod
    def deterministic(role: str, prompt: str) -> str:
        """``double:<role>:<16 hex>`` — stable across processes, runs and machines."""
        digest = hashlib.sha256(f"{role}\x00{prompt}".encode()).hexdigest()[:16]
        return f"double:{role}:{digest}"

    def complete(
        self,
        prompt: Any,
        *,
        role: str | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Return the deterministic reply and record the call. Never touches the network."""
        text = prompt_text(prompt)
        effective_role = role if role is not None else self.role
        self._record(text, effective_role, system, kwargs)
        if self._default is not None:
            return self._default
        return self.deterministic(effective_role, text)

    def complete_json(self, prompt: Any, **kwargs: Any) -> Any:
        """:meth:`complete`, parsed as JSON. Only useful with an explicit ``default``."""
        return json.loads(self.complete(prompt, **kwargs))

    def __repr__(self) -> str:
        return f"<DeterministicLLM role={self.role!r} calls={len(self.calls)}>"
