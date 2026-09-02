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
  ``(role, system, prompt)``. For code paths that must run offline but whose reply text
  nobody asserts on. The reply *shape* matches ``proxyshop_support.llm_double.LLMDouble``
  — ``double:<role>:<16 hex chars>`` — but see that class's docstring for the three
  places it is not a byte-for-byte drop-in.

Both record every call in ``.calls``, which is how a test asserts on *prompt assembly* —
e.g. that the static store context precedes the dynamic tail (C4). Note **where** the two
halves land: a call is recorded split the way it is sent, so the static context is on
``call.system`` and the dynamic tail is on ``call.prompt``. The assertion is therefore
over the pair::

    call = double.calls[-1]
    assert call.system == STORE_CONTEXT           # the cached half, sent first
    assert call.prompt == "buyer asks: ..."       # the dynamic half, sent last

and NOT ``call.prompt.index(STORE_CONTEXT) < call.prompt.index(tail)``, which cannot work:
``call.prompt`` holds the tail alone, so that raises ``ValueError: substring not found``.
Assert on the assembled single string with :func:`llm.prompting.CachedPrompt.text` if that
is what you want to see.

``call.system`` is the blocks **joined** for reading, so it cannot distinguish a call that
sent two blocks from one that sent a single block holding the same joined text. When that
distinction is what you are asserting — a per-call system arriving as its own uncached
block, say — use the structure instead::

    assert call.system_blocks == (STORE_CONTEXT, "turn 3 of 9")   # lossless
    assert call.system == f"{STORE_CONTEXT}\\n\\nturn 3 of 9"       # a rendering

See :class:`KeyedLLMCall`, which is what ``.calls`` actually holds.

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
from llm.prompting import (
    SECTION_SEPARATOR,
    CachedPrompt,
    SystemKey,
    canonical_system_key,
    system_key_blocks,
    system_key_text,
    wire_key,
)
from llm.recordings import load_recording

#: How many near-miss prompts an :class:`UnrecordedPromptError` suggests.
SUGGESTION_COUNT = 3

#: How much of a block's text the miss message prints when it spells out the structure.
BLOCK_PREVIEW_CHARS = 80


def _describe_system_key(key: SystemKey) -> str:
    """How a system contract is printed in a miss message: the text, plus its structure.

    The flat rendering alone is not enough here, and this is T-118 (a) at the *error*
    surface. ``("STORE ENVELOPE", "turn 3 of 9")`` and ``"STORE ENVELOPE\\n\\nturn 3 of
    9"`` are different calls with the same :func:`llm.prompting.system_key_text`, so a
    message that printed only the text announced "a DIFFERENT system contract" directly
    above two identical-looking strings — the single most confusing thing it could say
    about a real and correct miss.

    So a key of more than one block also names its blocks. One block (or none) prints as
    before: there is nothing to disambiguate, and every existing message stays readable.
    """
    text = system_key_text(key)
    rendered = f"({len(text)} chars) {text[:200]!r}"
    blocks = system_key_blocks(key)
    if len(blocks) > 1:
        preview = tuple(block[:BLOCK_PREVIEW_CHARS] for block in blocks)
        rendered += f", sent as {len(blocks)} separate blocks: {preview!r}"
    return rendered


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


@dataclass(frozen=True)
class KeyedLLMCall(LLMCall):
    """An :class:`LLMCall` that also carries the call's **lossless** system identity.

    This is what both doubles actually append to ``.calls``; it is an ``LLMCall`` in every
    respect an assertion can see, plus one field the frozen ``LLMDouble`` has no analogue
    for.

    Why it exists (T-118 (a)): :attr:`LLMCall.system` is the blocks joined by
    :data:`llm.prompting.SECTION_SEPARATOR`, and **any** join is lossy. So the two calls
    below — genuinely different requests, which the lookup key tells apart — recorded
    byte-identical ``LLMCall``\\ s, and the recording surface could not be used to check
    which one was made::

        double.complete(CachedPrompt("A", "q"), system="B")  # two system blocks
        double.complete(CachedPrompt("A\\n\\nB", "q"))         # one system block
        # both recorded system='A\\n\\nB'

    The join cannot be removed from :attr:`~LLMCall.system`: it is ``str | None`` on the
    orchestrator-frozen ``proxyshop_support.llm_double.LLMCall``, downstream tickets assert
    ``call.system == STORE_CONTEXT`` against it, and
    ``packages/llm/tests/test_llm_contract_seam.py`` pins both the flat rendering and its
    ``str``-ness. It is a *rendering*, and a rendering it stays. So the structure is added
    beside it rather than folded into it, and :attr:`system_key` — not
    :attr:`~LLMCall.system` — is the thing to compare when the block structure matters.

    ``LLMCall`` itself keeps exactly the frozen double's four fields, in its order, so
    ``dataclasses.fields(LLMCall)`` still matches field for field and positional
    construction still cannot transpose. The extra field lives here, last, with a default.

    Attributes:
        system_key: the :data:`llm.prompting.SystemKey` this call was looked up under —
            the block texts themselves. ``""`` when the call carried no system contract.

    Note:
        Being a subclass, a ``KeyedLLMCall`` never compares equal to a plain ``LLMCall``
        built from the same four fields (dataclass ``__eq__`` requires the same class).
        Assert on fields, or build a ``KeyedLLMCall``.
    """

    system_key: SystemKey = ""

    @property
    def system_blocks(self) -> tuple[str, ...]:
        """The system contract as the block list that was sent: ``()``, one, or several.

        The readable spelling of :attr:`system_key`, and the one to assert on::

            assert call.system_blocks == ("STORE ENVELOPE", "turn 3 of 9")
        """
        return system_key_blocks(self.system_key)


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


def normalize_recording_key(key: Any) -> tuple[SystemKey, str]:
    """Accept any spelling of a recording key and return the ``(system, prompt)`` pair.

    * ``"just the prompt"`` -> ``("", "just the prompt")`` — a recording with no system
      contract, which is what the frozen acceptance suite passes.
    * ``("system text", "prompt text")`` -> itself: one system block.
    * ``(("envelope", "turn 3 of 9"), "prompt text")`` -> itself: **two** system blocks,
      which is a different call from one block holding both texts, however they are
      spelled. The system half is a :data:`llm.prompting.SystemKey`, so a recording can
      name a multi-block call without any separator character being involved.
    * a :class:`llm.prompting.CachedPrompt` -> its two halves, as sent.
    """
    if isinstance(key, str):
        return ("", key)
    if isinstance(key, tuple):
        if len(key) != 2 or not isinstance(key[1], str):
            raise TypeError(
                f"a tuple recording key must be exactly (system, prompt) with a string "
                f"prompt; got {key!r}"
            )
        system = key[0]
        if not isinstance(system, str) and not (
            isinstance(system, tuple) and all(isinstance(part, str) for part in system)
        ):
            # A tuple and not a list, because a recording key is a mapping key: a list is
            # unhashable and could never have been written here in the first place.
            raise TypeError(
                f"the system half of a recording key must be a string (one block) or a "
                f"tuple of strings (the blocks, in order); got {system!r}"
            )
        return (canonical_system_key(system), key[1])
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
        self.calls: list[KeyedLLMCall] = []
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

        A rule is **persistent and unconditional**: unlike :meth:`queue`, which is
        consumed, a rule keeps matching for the life of the double and :meth:`reset` does
        not clear it. On :class:`RecordedLLM` that is a deliberate hole in strictness — a
        call whose prompt contains ``substring`` gets ``response`` and never consults the
        recorded table, so it can no longer raise :class:`UnrecordedPromptError` for that
        prompt. Strictness is intact for every prompt no rule matches, which is what
        ``test_a_when_rule_does_not_make_the_recorded_double_permissive_for_everything``
        pins. Keep the needle specific, and prefer :meth:`queue` for a one-off.
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
        self, role: str | None, prompt: str, system_key: SystemKey, kwargs: dict[str, Any]
    ) -> None:
        """Append one :class:`KeyedLLMCall`, keyed losslessly and rendered flat.

        It takes the :data:`~llm.prompting.SystemKey` rather than the rendered text so
        the two cannot drift: ``system`` is *derived* here, in the one place, from the
        same key the lookup used. A caller that flattened first would have handed this
        method a string with the structure already destroyed.
        """
        system_text = system_key_text(system_key)
        self.calls.append(
            KeyedLLMCall(
                role=role if role is not None else "default",
                prompt=prompt,
                system=system_text or None,
                kwargs=dict(kwargs),
                system_key=system_key,
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

    @property
    def model(self) -> str:
        """``"double:<role>"`` — the parity of :attr:`llm.client.AnthropicLLM.model`.

        A double sends no request and has no model id, but a consumer that logs
        ``client.model`` should not work under ``LLM_PROVIDER=anthropic`` and raise
        ``AttributeError`` under D20's default. It deliberately does NOT look like a
        model id: reading ``double:buyer`` in a log is the point.
        """
        return f"double:{getattr(self, 'role', None) or 'default'}"

    def reset(self) -> None:
        """Forget the recorded calls and any queued replies.

        Canned :meth:`when` rules and the recorded table survive, matching the frozen
        ``LLMDouble.reset``. That parity is deliberate, and it is also a leak: a
        session- or module-scoped double carrying a :meth:`when` rule keeps answering
        with it in every later test, and ``reset()`` between tests will not save you.
        Register rules on a per-test double, or drop the rule explicitly.
        """
        self.calls.clear()
        self._queue.clear()


class RecordedLLM(_RecordingBase):
    """Replays a fixed prompt→reply table. Deterministic, offline, and strict.

    Args:
        recordings: ``(system, prompt) -> reply``. Keys may be written as a plain prompt
            string (meaning "no system contract"), as a ``(system, prompt)`` tuple whose
            system half is one block's text or a tuple of several blocks' texts, or as a
            :class:`llm.prompting.CachedPrompt`. Copied and normalised on construction,
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
        table: dict[tuple[SystemKey, str], str] = {}
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
    def recordings(self) -> Mapping[tuple[SystemKey, str], str]:
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
        system_key, user_text = key
        # The key is looked up as-is (a tuple of block texts once there is more than one),
        # and it is what `_record` stores on `KeyedLLMCall.system_key`. Only `when`
        # matching and `LLMCall.system` flatten it, and neither of those is an identity.
        system_text = system_key_text(system_key)
        self._record(role if role is not None else self.role, user_text, system_key, kwargs)
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

    def _miss(self, key: tuple[SystemKey, str]) -> UnrecordedPromptError:
        system_key, user_text = key
        source = f" ({self.name})" if self.name else ""

        # Name the function that fixes the single most likely miss. `from_fixture(name)`
        # loads the fixture's recordings but NOT its system contract, so the obvious first
        # call — `RecordedLLM.from_fixture(n).complete(recorded_prompt)` — misses on the
        # system half, and the message used to describe the miss without ever spelling the
        # one-liner that resolves it.
        contract_hint = (
            f"\nThis double was built from the {self.name!r} fixture, whose recordings "
            f"were authored against that file's system contract. Pass it:\n"
            f"    from llm.recordings import load_system_contract\n"
            f"    double.complete(prompt, system=load_system_contract({self.name!r}))"
            if self.name
            else ""
        )

        # The most valuable miss to diagnose: the user turn is recorded, but under a
        # different system contract. That is a prompt-contract change, and saying so is
        # the difference between a two-second fix and an afternoon.
        # The recorded system KEYS, not their flat renderings: two of these can render to
        # the same string and still be the different contracts this message is about.
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
                f"system sent:     {_describe_system_key(system_key)}\n"
                f"system recorded: "
                + "; ".join(_describe_system_key(other) for other in same_prompt[:2])
                + "\nIf the contract genuinely changed, re-review the recording. If it "
                "did not, pass the same system text the fixture was authored against."
                + contract_hint,
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
            f"system {_describe_system_key(system_key)}\n"
            f"{len(self._recordings)} recording(s) available.{hint}{contract_hint}",
            prompt=user_text,
        )

    def __repr__(self) -> str:
        label = f" name={self.name!r}" if self.name else ""
        return f"<RecordedLLM{label} recordings={len(self._recordings)} calls={len(self.calls)}>"


class DeterministicLLM(_RecordingBase):
    """Answers any prompt with a stable, offline, content-derived string.

    Args:
        default: a fixed reply for every prompt. ``None`` selects :meth:`deterministic`.
            Positional, matching ``LLMDouble(default)``.
        role: the role label used in the reply and recorded on every call. Keyword-only;
            the frozen double takes its role per *call* instead.

    This is what :func:`llm.client.build_llm` returns for ``LLM_PROVIDER=double`` when no
    recordings were supplied: code that must *run* offline gets a reply, and code that
    asserts on the reply uses :class:`RecordedLLM` instead. :meth:`queue` and :meth:`when`
    work here too, so a test can script an exchange without leaving the default provider.

    **Not a byte-for-byte drop-in for the frozen ``LLMDouble``**, in three known places:
    the digest covers the system half here and not there, so any call passing ``system=``
    gets a different reply string; :meth:`complete_json` returns a marker object on an
    unscripted call where the frozen one raises ``JSONDecodeError``; and the role is a
    constructor argument here (defaulting to ``"default"``) as well as a per-call one.
    Everything the frozen double's tests actually assert — the ``double:<role>:<hex>``
    shape, :meth:`queue`/:meth:`when` precedence, ``calls``/``last_prompt``/:meth:`reset`,
    and ``deterministic()`` itself — agrees exactly.
    """

    def __init__(self, default: str | None = None, *, role: str = "default") -> None:
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
        system_key, user_text = wire_key(prompt, system)
        system_text = system_key_text(system_key)
        effective_role = role if role is not None else self.role
        self._record(effective_role, user_text, system_key, kwargs)
        scripted = self._scripted(system_text, user_text)
        if scripted is not None:
            return scripted
        if self._default is not None:
            return self._default
        return self.deterministic(effective_role, self._digest_input(system_key, user_text))

    @staticmethod
    def _digest_input(system_key: SystemKey, user_text: str) -> str:
        """What the deterministic hash is taken over.

        With no system half this is the bare prompt, so :meth:`deterministic` agrees with
        the frozen ``LLMDouble`` byte for byte for calls that pass no ``system=``. A call
        that passes one **diverges deliberately**: the frozen double drops the system half
        entirely, so inverting the prompt contract leaves its reply unchanged, and this
        package exists partly to stop exactly that.

        Several system blocks are folded into a length-prefixed digest first, rather than
        flattened with :func:`llm.prompting.system_key_text`. Flattening would hand back
        the same reply for two different block structures whose texts happen to join to
        one string — the collision :data:`llm.prompting.SystemKey` exists to remove — and
        "changing the contract changes the answer" is this double's one claim.
        """
        if isinstance(system_key, tuple):
            folded = hashlib.sha256()
            for block in system_key:
                folded.update(len(block).to_bytes(8, "big"))
                folded.update(block.encode("utf-8"))
            system_key = folded.hexdigest()
        return f"{system_key}{SECTION_SEPARATOR}{user_text}" if system_key else user_text

    def complete_json(self, prompt: Any, **kwargs: Any) -> Any:
        """:meth:`complete`, parsed as JSON. Only the **unscripted** reply is rescued.

        :meth:`complete` has four reply sources, and this method changes exactly one of
        them. A queued reply, a :meth:`when` reply and a ``default=`` reply are parsed
        as-is and raise ``json.JSONDecodeError`` when they are not JSON — you wrote that
        text, so a silent rescue would hide your typo. Only the fall-through
        ``double:<role>:<hex>``, which nobody wrote, is turned into an object:

        ==============  ==================================================
        reply source    non-JSON reply
        ==============  ==================================================
        ``queue()``     raises ``JSONDecodeError``
        ``when()``      raises ``JSONDecodeError``
        ``default=``    raises ``JSONDecodeError``
        unscripted      returns ``{"double": <role>, "digest": <hex>}``
        ==============  ==================================================

        The unscripted rescue exists because ``double:<role>:<hex>`` is not JSON, so this
        used to raise on every call that had no queued, canned or default reply. Two of
        this package's four recorded roles are JSON roles, so any consumer running under
        the default provider (D20) died on its first call.

        The marker shape is stable, but deliberately NOT your schema. A consumer that
        needs a schema-shaped reply wants :class:`RecordedLLM`, or a ``default=`` holding
        the JSON it expects.
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
