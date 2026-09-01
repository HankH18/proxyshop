"""Offline test doubles for the LLM client (D20: the double is the default provider).

Two doubles, both of which satisfy :class:`llm.client.LLMClient` and neither of which can
reach the network — there is no socket call anywhere in this module, and no import of
anything that opens one:

* :class:`RecordedLLM` — replays a hand-authored recording table (D21). An exact match on
  the ``(system, prompt)`` pair returns the reviewed reply; **anything else raises**. It
  never improvises and never falls through to a live client, because a test that silently
  invented an answer would be asserting on nothing.

  Keying on the **pair** is the point. The system half is the prompt contract — "copy
  values verbatim; never infer", the envelope rules, the tool list — and against a live
  model it is what decides the answer. A double that keys on the user half alone replays
  the identical reply after that contract is inverted, so every downstream ticket testing
  through this seam turns a whole class of prompt-contract regressions into permanent
  green. :func:`llm.prompting.wire_key` computes the pair exactly as
  :class:`llm.client.AnthropicLLM` sends it, so the two cannot drift.
* :class:`DeterministicLLM` — answers any prompt with a stable function of
  ``(role, prompt)``. For code paths that must run offline but whose reply text nobody
  asserts on. The reply shape matches ``proxyshop_support.llm_double.LLMDouble`` so the
  two agree: ``double:<role>:<16 hex chars>``.

Both record every call in ``.calls``, which is how a test asserts on *prompt assembly* —
e.g. that the static store context precedes the dynamic tail (C4).

Both also implement the full surface of the orchestrator-frozen
``proxyshop_support.llm_double.LLMDouble`` — :meth:`queue`, :meth:`when`, ``calls``,
``last_prompt``, ``reset``, ``complete_json`` — so a ticket whose tests were written
against the frozen fixture double works unchanged with whatever :func:`llm.client.build_llm`
hands it. ``queue`` wins over ``when``, which wins over the recorded table (or the
deterministic reply).
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
from llm.prompting import SECTION_SEPARATOR, CachedPrompt, wire_key
from llm.recordings import load_recording

#: How many near-miss prompts an :class:`UnrecordedPromptError` suggests.
SUGGESTION_COUNT = 3


@dataclass(frozen=True)
class LLMCall:
    """One invocation of a double, kept so tests can assert on what was sent.

    Field order is ``(role, prompt, system, kwargs)`` — the same order as the
    orchestrator-frozen ``proxyshop_support.llm_double.LLMCall``. It used to lead with
    ``prompt``, which every keyword construction survives and every POSITIONAL one
    silently transposes.

    ``frozen=True`` stops the *fields* being rebound; it does not deep-freeze
    :attr:`kwargs`, which is a plain dict and can be mutated in place (that also makes
    the instance unhashable). The dict is a copy taken at call time, so mutating it
    cannot reach back into the caller's arguments.
    """

    role: str = "default"
    prompt: str = ""
    system: str | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)


def prompt_text(prompt: Any) -> str:
    """The human-readable assembled form of a prompt: static context first, tail last.

    Useful for logging and for :meth:`when` matching. It is **not** the lookup key — see
    :func:`llm.prompting.wire_key`, which keeps the system and user halves separate
    because that is how they are sent.
    """
    if isinstance(prompt, str):
        return prompt
    text = getattr(prompt, "text", None)
    if isinstance(text, str):
        return text
    return str(prompt)


def normalize_recording_key(key: Any) -> tuple[str, str]:
    """Accept any spelling of a recording key and return the ``(system, prompt)`` pair.

    * ``"just the prompt"`` -> ``("", "just the prompt")`` — a recording with no system
      contract, which is what the frozen acceptance suite passes.
    * ``("system text", "prompt text")`` -> itself.
    * a :class:`llm.prompting.CachedPrompt` -> its two halves, as sent.
    """
    if isinstance(key, str):
        return ("", key)
    if isinstance(key, tuple):
        if len(key) != 2 or not all(isinstance(half, str) for half in key):
            raise TypeError(
                f"a tuple recording key must be exactly (system, prompt) with two "
                f"strings; got {key!r}"
            )
        return (key[0], key[1])
    if isinstance(key, CachedPrompt):
        return wire_key(key)
    raise TypeError(
        f"recording keys must be a prompt string, a (system, prompt) tuple, or a "
        f"CachedPrompt; got {type(key).__name__}"
    )


class _RecordingBase:
    """Shared call bookkeeping, plus the frozen ``LLMDouble`` scripting surface.

    ``proxyshop_support.llm_double.LLMDouble`` is the fixture double every lane already
    has, and tickets write tests against its API. Both doubles here implement the same
    :meth:`queue` / :meth:`when` methods with the same precedence, so a ticket that swaps
    the fixture for a :func:`llm.client.build_llm` result does not get an
    ``AttributeError`` on its first line.
    """

    def __init__(self) -> None:
        self.calls: list[LLMCall] = []
        self._queue: list[str] = []
        self._canned: dict[str, str] = {}

    def queue(self, *responses: str) -> _RecordingBase:
        """Queue replies, consumed one per :meth:`complete` call, FIFO.

        Queued replies take precedence over everything else, including a recorded table —
        the point of queueing is to script one specific exchange.
        """
        self._queue.extend(responses)
        return self

    def when(self, substring: str, response: str) -> _RecordingBase:
        """Reply with ``response`` whenever ``substring`` appears in the prompt.

        Matched against the whole prompt *including the system half*, so a rule keyed on
        contract text ("never infer") fires the way a reader expects.
        """
        self._canned[substring] = response
        return self

    def _scripted(self, system: str, prompt: str) -> str | None:
        """The queued or canned reply for this call, or ``None`` to fall through."""
        if self._queue:
            return self._queue.pop(0)
        haystack = f"{system}{SECTION_SEPARATOR}{prompt}" if system else prompt
        for needle, response in self._canned.items():
            if needle in haystack:
                return response
        return None

    def _record(
        self, role: str | None, prompt: str, system: str | None, kwargs: dict[str, Any]
    ) -> None:
        self.calls.append(
            LLMCall(
                role=role if role is not None else "default",
                prompt=prompt,
                system=system,
                kwargs=dict(kwargs),
            )
        )

    @property
    def last_prompt(self) -> str:
        """The most recent prompt string. Raises ``IndexError`` if nothing was asked."""
        return self.calls[-1].prompt

    def prompts(self) -> list[str]:
        """Every prompt string sent to this double, in order."""
        return [call.prompt for call in self.calls]

    @staticmethod
    def deterministic(role: str, prompt: str) -> str:
        """``double:<role>:<16 hex>`` — stable across processes, runs and machines.

        Lives on the base class so BOTH doubles expose it, matching the frozen
        ``LLMDouble``: a ticket calling ``double.deterministic(...)`` on whatever
        :func:`llm.client.build_llm` handed it should not have to care which one it got.
        """
        digest = hashlib.sha256(f"{role}\x00{prompt}".encode()).hexdigest()[:16]
        return f"double:{role}:{digest}"

    def reset(self) -> None:
        """Forget the recorded calls and any queued replies.

        Canned :meth:`when` rules and the recorded table survive, matching the frozen
        ``LLMDouble.reset``.
        """
        self.calls.clear()
        self._queue.clear()


class RecordedLLM(_RecordingBase):
    """Replays a fixed prompt→reply table. Deterministic, offline, and strict.

    Args:
        recordings: ``(system, prompt) -> reply``. Keys may be written as a plain prompt
            string (meaning "no system contract"), as a ``(system, prompt)`` tuple, or as
            a :class:`llm.prompting.CachedPrompt`. Copied and normalised on construction,
            so a later mutation of the caller's dict cannot change what this replays.
        role: optional role label, recorded on every call for assertions.
        name: optional label used in error messages (the fixture stem, usually).

    Determinism is total: the same call returns the same string on every call, on every
    instance built from the same table, in every process. Nothing but the queue (which is
    opt-in, via :meth:`queue`) carries state between calls.

    Example::

        double = RecordedLLM({"summarize the envelope": "floors respected"})
        assert double.complete("summarize the envelope") == "floors respected"
        double.complete("something else")                    # -> UnrecordedPromptError
        double.complete("summarize the envelope", system="x")  # -> UnrecordedPromptError

    That last line is the whole design: the system half is part of the identity of a
    call, so changing the prompt contract cannot silently keep replaying the old answer.
    """

    def __init__(
        self,
        recordings: Mapping[Any, str],
        *,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__()
        if not isinstance(recordings, Mapping):
            raise TypeError(
                f"RecordedLLM takes a mapping of prompt -> reply, got {type(recordings).__name__}"
            )
        table: dict[tuple[str, str], str] = {}
        for key, reply in recordings.items():
            if not isinstance(reply, str):
                raise TypeError(
                    f"a recorded reply must be a str; got {type(reply).__name__} for key {key!r}"
                )
            table[normalize_recording_key(key)] = reply
        self._recordings = table
        self.role = role
        self.name = name

    @classmethod
    def from_fixture(cls, name: str, *, role: str | None = None) -> RecordedLLM:
        """Build a double from ``packages/llm/fixtures/recorded/<name>.json`` (D21)."""
        return cls(load_recording(name), role=role, name=name)

    @property
    def recordings(self) -> Mapping[tuple[str, str], str]:
        """Read-only view of the recording table, keyed by ``(system, prompt)``."""
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
            prompt: a string, or a :class:`llm.prompting.CachedPrompt` (whose two halves
                are used exactly as the live client sends them).
            role: recorded on the call; does not affect the lookup.
            system: **part of the lookup key.** A different system contract is a
                different call, and gets a different recording or an error.
            **kwargs: recorded on the call; accepted so this double is drop-in for the
                live client's signature. They do not affect the lookup — they are
                sampling knobs, not contract.

        Raises:
            UnrecordedPromptError: for any ``(system, prompt)`` pair with no recording.
                This is a ``KeyError``, never a network error — the double has no client
                to fall back to, by construction.
        """
        key = wire_key(prompt, system)
        system_text, user_text = key
        self._record(
            role if role is not None else self.role, user_text, system_text or None, kwargs
        )
        scripted = self._scripted(system_text, user_text)
        if scripted is not None:
            return scripted
        try:
            return self._recordings[key]
        except KeyError:
            raise self._miss(key) from None

    def complete_json(self, prompt: Any, **kwargs: Any) -> Any:
        """:meth:`complete`, parsed as JSON. Raises ``json.JSONDecodeError`` on garbage."""
        return json.loads(self.complete(prompt, **kwargs))

    def _miss(self, key: tuple[str, str]) -> UnrecordedPromptError:
        system_text, user_text = key
        source = f" ({self.name})" if self.name else ""

        # The most valuable miss to diagnose: the user turn is recorded, but under a
        # different system contract. That is a prompt-contract change, and saying so is
        # the difference between a two-second fix and an afternoon.
        same_prompt = [
            recorded_system
            for (recorded_system, recorded_prompt) in self._recordings
            if recorded_prompt == user_text
        ]
        if same_prompt:
            return UnrecordedPromptError(
                f"RecordedLLM{source} has this prompt recorded, but under a DIFFERENT "
                f"system contract, so the recorded reply is not valid for this call "
                f"(D21: recordings are reviewed answers to a specific contract).\n"
                f"prompt: {user_text[:200]!r}\n"
                f"system sent    ({len(system_text)} chars): {system_text[:200]!r}\n"
                f"system recorded: "
                + "; ".join(f"({len(other)} chars) {other[:200]!r}" for other in same_prompt[:2])
                + "\nIf the contract genuinely changed, re-review the recording. If it "
                "did not, pass the same system text the fixture was authored against.",
                prompt=user_text,
            )

        suggestions = difflib.get_close_matches(
            user_text, [prompt for _, prompt in self._recordings], n=SUGGESTION_COUNT, cutoff=0.4
        )
        hint = (
            "\nclosest recorded prompts:\n"
            + "\n".join(f"  - {candidate[:160]!r}" for candidate in suggestions)
            if suggestions
            else "\n(no recorded prompt is close to it)"
        )
        return UnrecordedPromptError(
            f"RecordedLLM{source} has no recording for this (system, prompt) pair, and "
            f"never invents one or calls a live model (D20/D21). Add it to the fixture, "
            f"or pass it in the recordings dict.\n"
            f"prompt ({len(user_text)} chars): {user_text[:400]!r}\n"
            f"system ({len(system_text)} chars): {system_text[:200]!r}\n"
            f"{len(self._recordings)} recording(s) available.{hint}",
            prompt=user_text,
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
    asserts on the reply uses :class:`RecordedLLM` instead. :meth:`queue` and :meth:`when`
    work here too, so a test can script an exchange without leaving the default provider.
    """

    def __init__(self, *, role: str = "default", default: str | None = None) -> None:
        super().__init__()
        self.role = role
        self._default = default

    def complete(
        self,
        prompt: Any,
        *,
        role: str | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Return the deterministic reply and record the call. Never touches the network.

        The reply is a function of ``(role, system, prompt)``, so — unlike a double that
        ignores the system half — changing the prompt contract changes the answer here
        too.
        """
        system_text, user_text = wire_key(prompt, system)
        effective_role = role if role is not None else self.role
        self._record(effective_role, user_text, system_text or None, kwargs)
        scripted = self._scripted(system_text, user_text)
        if scripted is not None:
            return scripted
        if self._default is not None:
            return self._default
        return self.deterministic(effective_role, self._digest_input(system_text, user_text))

    @staticmethod
    def _digest_input(system_text: str, user_text: str) -> str:
        """What the deterministic hash is taken over.

        With no system half this is the bare prompt, so :meth:`deterministic` agrees with
        the frozen ``LLMDouble`` byte for byte for the calls that double can make.
        """
        return f"{system_text}{SECTION_SEPARATOR}{user_text}" if system_text else user_text

    def complete_json(self, prompt: Any, **kwargs: Any) -> Any:
        """:meth:`complete`, parsed as JSON — and it always parses.

        A deterministic reply is ``double:<role>:<hex>``, which is not JSON, so this used
        to raise ``JSONDecodeError`` on every call that had no queued, canned or default
        reply. Two of this package's four recorded roles are JSON roles, so any consumer
        running under the default provider (D20) died on its first call. It now returns
        the same information as an object.

        The shape is ``{"double": <role>, "digest": <hex>}`` — stable, but deliberately
        NOT your schema. A consumer that needs a schema-shaped reply wants
        :class:`RecordedLLM`, or a ``default=`` holding the JSON it expects.
        """
        reply = self.complete(prompt, **kwargs)
        try:
            return json.loads(reply)
        except json.JSONDecodeError:
            marker, _, rest = reply.partition(":")
            if marker != "double":
                raise
            role, _, digest = rest.partition(":")
            return {"double": role, "digest": digest}

    def __repr__(self) -> str:
        return f"<DeterministicLLM role={self.role!r} calls={len(self.calls)}>"
