"""Cache-first prompt assembly (C4: "store-agent prompts structured static-context-first").

Anthropic's prompt caching only ever caches a **prefix** of a request: the cached span
runs from the start of the prompt up to a ``cache_control`` breakpoint, and a single byte
changed inside that span invalidates it. So the ordering is not cosmetic — it is the whole
mechanism:

* the **static context** (the store's envelope, its catalog policy, the tool contract —
  the same bytes on every request for that store) goes **first**, and
* the **dynamic tail** (this buyer's request, this turn's history) goes **last**.

Reverse the two and every request has a unique prefix, so nothing is ever a cache hit even
though the assembled text contains exactly the same information. That failure is invisible
in output and shows up only as latency and spend, which is why
``packages/llm/tests/test_llm_prompting.py`` asserts the boundary directly.

Usage::

    prompt = assemble_prompt(store_context, "buyer asks: do you ship to Maine?")
    reply = client.complete(prompt)

    # Same store, next turn: reuse the identical cacheable prefix.
    followup = prompt.with_dynamic("buyer asks: how fast?")
    assert followup.cacheable_prefix == prompt.cacheable_prefix
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from llm.errors import PromptAssemblyError

#: Joins the sections of a prompt. Part of the cacheable prefix, so it is a constant
#: rather than a parameter with a per-call value.
SECTION_SEPARATOR = "\n\n"

#: The cache breakpoint Anthropic understands, attached to the last static block.
CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

#: The system half of a lookup key (:func:`wire_key`): the system **block texts**, kept
#: apart rather than joined into one string.
#:
#: There is deliberately no separator constant here any more. Every join is a lossy
#: encoding of a list of strings, so whichever character is chosen, a block whose own text
#: contains it makes two genuinely different requests canonicalise to one key —
#: ``CachedPrompt("A") + system="B"`` (two blocks, the second uncached) and
#: ``CachedPrompt("A<sep>B")`` (one cached block) — and
#: :class:`llm.doubles.RecordedLLM` then hands back the reply reviewed for the *other*
#: one. ``"\n\n"`` fell to a blank line inside a block (W1-24). NUL fell too (W2-06 /
#: T-104): JSON encodes it as the escape ``"\u0000"``, and
#: :func:`llm.recordings.load_recording_file` has no reason to reject it, so a fixture or
#: a template can carry one. A tuple has no such character; it *is* the block list.
#:
#: The two degenerate arities collapse to a plain ``str``, which costs nothing in
#: injectivity and keeps the pinned spellings intact:
#:
#: * **zero blocks** -> ``""``, so a plain-string prompt with no system keys on exactly
#:   ``("", prompt)`` — what the frozen acceptance suite passes;
#: * **one block** -> that block's text verbatim, so a fixture's ``(system, prompt)`` pair
#:   is written the obvious way;
#: * **two or more** -> the tuple of their texts, in the order they are sent.
#:
#: That is injective because a block is never empty (so a one-block key is never ``""``,
#: the zero-block key) and no ``str`` ever equals a ``tuple``. The emptiness precondition
#: is enforced by :func:`canonical_system_key` itself — every key is built there — and not
#: merely by :meth:`CachedPrompt.to_system_blocks` declining to emit one, because a direct
#: caller never goes through ``to_system_blocks`` (W2/T-118 (b)).
type SystemKey = str | tuple[str, ...]


def _join(value: str | Sequence[str], separator: str) -> str:
    if isinstance(value, str):
        return value
    parts = [part for part in value if part != ""]
    return separator.join(parts)


@dataclass(frozen=True)
class CachedPrompt:
    """An assembled prompt split at its cache boundary.

    Attributes:
        static_context: the reusable prefix — identical for every request against the
            same store, and therefore the part worth caching.
        dynamic_tail: the per-request text. Never appears before the boundary.
        separator: the string joining the two halves; part of the cacheable prefix.

    The invariant every consumer may rely on, for ANY tail including an empty one::

        prompt.text == prompt.cacheable_prefix + prompt.dynamic_tail
        prompt.cache_boundary == len(prompt.cacheable_prefix)
        prompt.text.startswith(prompt.static_context)
        prompt.with_dynamic(anything).cacheable_prefix == prompt.cacheable_prefix

    Note that :attr:`text` is the *assembled* form — one string, static half first. It is
    what the doubles look up and what a human reads. It is NOT byte-identical to the wire
    payload: :meth:`to_system_blocks` sends :attr:`static_context` without the trailing
    separator, and :meth:`to_messages` sends the tail as a separate user turn.
    """

    static_context: str
    dynamic_tail: str = ""
    separator: str = SECTION_SEPARATOR

    @property
    def cacheable_prefix(self) -> str:
        """Everything up to the cache boundary, separator included.

        The separator is part of the prefix **unconditionally**, even when the tail is
        empty. It used to be dropped in that case, which quietly broke the one invariant
        this class exists to provide: ``prompt.with_dynamic("")`` — a turn that clears the
        tail — returned a *different* prefix from its parent, so the cache entry the
        parent had just paid to write could never be read back.
        """
        return self.static_context + self.separator

    @property
    def cache_boundary(self) -> int:
        """Index into :attr:`text` where the cacheable prefix ends."""
        return len(self.cacheable_prefix)

    @property
    def text(self) -> str:
        """The whole prompt as one string: static context first, dynamic tail last."""
        return self.cacheable_prefix + self.dynamic_tail

    @property
    def prefix_digest(self) -> str:
        """Stable fingerprint of the cacheable prefix.

        Two prompts that can share a cache entry have the same digest; two that cannot,
        do not. Cheaper to assert on than the whole prefix, and it says what it means.
        """
        return hashlib.sha256(self.cacheable_prefix.encode("utf-8")).hexdigest()[:16]

    def with_dynamic(self, dynamic: str | Sequence[str]) -> CachedPrompt:
        """A new prompt with the same static context and a different tail."""
        return CachedPrompt(
            static_context=self.static_context,
            dynamic_tail=_join(dynamic, self.separator),
            separator=self.separator,
        )

    def to_system_blocks(
        self, *, cache: bool = True, extra: str | None = None
    ) -> list[dict[str, Any]]:
        """The static context as Anthropic ``system`` blocks, cache breakpoint attached.

        Args:
            cache: attach ``cache_control`` to the store-context block. Off is for
                one-shot calls where a cache write would never be read back.
            extra: per-call system text. It is appended as a **second, uncached block
                after** the store context — never merged in front of it.

        Returns:
            Zero, one or two blocks. An **empty** :attr:`static_context` contributes no
            block at all: an empty cached block is a cache breakpoint over zero bytes,
            which writes an entry nothing can ever read and is not what "no static
            context" should put on the wire. So ``CachedPrompt("", "q")`` sends no system
            at all, exactly as a plain string prompt with no ``system=`` does.

        Why ``extra`` cannot go first: prefix caching matches from byte zero, so a
        per-call system that varies at all — a turn counter, a timestamp, a request id —
        would push the stable store envelope behind bytes that change every call, and it
        could never be a cache hit again. Same information, same answers, no cache: the
        exact failure this module exists to prevent, in the one function that composes
        the two halves.

        The first block carries :attr:`static_context` alone — the trailing separator that
        :attr:`cacheable_prefix` includes is a feature of the assembled string, not of
        the wire format.
        """
        blocks: list[dict[str, Any]] = []
        if self.static_context:
            block: dict[str, Any] = {"type": "text", "text": self.static_context}
            if cache:
                block["cache_control"] = dict(CACHE_CONTROL)
            blocks.append(block)
        if extra:
            blocks.append({"type": "text", "text": extra})
        return blocks

    def to_messages(self) -> list[dict[str, Any]]:
        """The dynamic tail as the Anthropic ``messages`` list (a single user turn).

        Raises:
            PromptAssemblyError: if the tail is empty. The API rejects an empty user
                turn, and a prompt that is *all* static context has nothing to ask.
        """
        if not self.dynamic_tail.strip():
            raise PromptAssemblyError(
                "this prompt has no dynamic tail, so there is no user turn to send; "
                "assemble it with the per-request text as the second argument"
            )
        return [{"role": "user", "content": [{"type": "text", "text": self.dynamic_tail}]}]

    def __str__(self) -> str:
        return self.text


def _compose(
    prompt: object, system: str | None, *, cache: bool
) -> tuple[list[dict[str, Any]], CachedPrompt]:
    """THE composer: how a ``(prompt, system)`` pair becomes system blocks + a user turn.

    Every caller — the live client's request, and the doubles' lookup key — goes through
    this one function, and that is the whole point of it existing. The two used to compose
    the bare-string case *independently*: :func:`wire_key` returned ``(system or "",
    str(prompt))`` while ``AnthropicLLM.complete`` built its own ``CachedPrompt``. Nothing
    forced those to agree, and a one-line change to either (measured: stripping the halves)
    made a recording unreachable through the very client it was recorded for, with the
    whole suite still green. There is now no second composition to drift from.

    Returns:
        ``(system_blocks, cached)`` — the blocks as they go on the wire, and the
        :class:`CachedPrompt` whose :attr:`~CachedPrompt.dynamic_tail` is the user turn.
        The tail is returned rather than the ``messages`` list because the doubles must be
        able to key an *empty* prompt, while :meth:`CachedPrompt.to_messages` (rightly)
        refuses to send one.
    """
    if isinstance(prompt, CachedPrompt):
        return prompt.to_system_blocks(cache=cache, extra=system), prompt
    cached = CachedPrompt(static_context=system or "", dynamic_tail=str(prompt))
    return cached.to_system_blocks(cache=cache), cached


def compose_request(
    prompt: object, system: str | None = None, *, cache: bool = True
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The ``(system_blocks, messages)`` an Anthropic request carries for this call.

    The wire-format half of :func:`_compose`; :func:`wire_key` is the lookup-key half, and
    both are the same composition. ``cache`` decides whether the static block carries the
    :data:`CACHE_CONTROL` breakpoint.

    Raises:
        PromptAssemblyError: if there is no user turn to send (an empty dynamic tail).
    """
    blocks, cached = _compose(prompt, system, cache=cache)
    return blocks, cached.to_messages()


def canonical_system_key(system: str | Sequence[str] | None) -> SystemKey:
    """Canonicalise the system half of a lookup key. See :data:`SystemKey`.

    Accepts every spelling of "the system blocks of a call" and returns the one form
    :func:`wire_key` produces, so a hand-written recording key and a composed call key
    agree without either side knowing how the other was spelled:

    * ``None`` / ``""`` / ``()`` / ``[]`` -> ``""`` (no system blocks at all)
    * ``"a contract"`` / ``("a contract",)`` -> ``"a contract"`` (one block)
    * ``("envelope", "turn 3 of 9")`` -> ``("envelope", "turn 3 of 9")`` (two blocks)

    A ``str`` is returned unchanged rather than wrapped: it is already one block's text,
    and wrapping it would make ``("", prompt)`` — the key the frozen acceptance suite's
    plain-string path composes to — unreachable.

    Raises:
        PromptAssemblyError: if any block in the sequence is empty. The single-element
            collapse returns the block's text verbatim, so ``[""]`` would key on ``""`` —
            which is the key for *no system blocks at all* — and a call carrying one empty
            contract block would replay the reply reviewed for a call carrying no contract.
            :meth:`CachedPrompt.to_system_blocks` never emits an empty block, so nothing
            composed can hit this; a **direct** caller could, and used to (T-118 (b)). A
            bare ``""`` (or ``None``, or ``()``) is untouched: those spell "no system", not
            "one empty block".
    """
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    texts = tuple(str(part) for part in system)
    if not texts:
        return ""
    if "" in texts:
        raise PromptAssemblyError(
            f"a system block may not be empty (block {texts.index('')} of {len(texts)} "
            f"is): the one-block collapse returns a block's text verbatim, so an empty "
            f"block would canonicalise to '' — the key for NO system blocks — and two "
            f"structurally different calls would share one recording. Drop the block "
            f"instead of sending it, exactly as CachedPrompt.to_system_blocks does. "
            f"Got {texts!r}."
        )
    if len(texts) == 1:
        return texts[0]
    return texts


def system_key_text(key: SystemKey) -> str:
    """A :data:`SystemKey` rendered for humans: the blocks joined by their section break.

    For logging, for :meth:`llm.doubles._RecordingBase.when` substring matching, and for
    the ``system`` field of a recorded :class:`llm.doubles.LLMCall`. It is deliberately
    **not** an identity: the join is lossy, which is the entire reason the key itself is a
    tuple. Never compare two calls by this string — compare
    :func:`system_key_blocks`, or the keys themselves.
    """
    if isinstance(key, str):
        return key
    return SECTION_SEPARATOR.join(key)


def system_key_blocks(key: SystemKey) -> tuple[str, ...]:
    """A :data:`SystemKey` as the block list it stands for. The **lossless** rendering.

    It undoes the two degenerate-arity collapses and nothing else::

        ""            -> ()              # no system blocks
        "a contract"  -> ("a contract",) # one block
        ("a", "b")    -> ("a", "b")      # two, unchanged

    so ``canonical_system_key(system_key_blocks(key)) == key`` for every key
    :func:`canonical_system_key` can produce — which is the injectivity of
    :data:`SystemKey` stated as a round trip.

    This is the function to compare two calls' system halves with when the *structure*
    matters. :func:`system_key_text` joins, and any join is lossy: ``("A", "B")`` and
    ``"A\\n\\nB"`` render to the same string and are different calls.
    """
    if isinstance(key, str):
        return (key,) if key else ()
    return key


def wire_key(prompt: object, system: str | None = None) -> tuple[SystemKey, str]:
    """The ``(system_key, user_text)`` pair a request actually carries.

    This is the canonical identity of a call, and it is what the doubles key on. The two
    halves travel separately — the system blocks and the user turn — so keying on the two
    of them concatenated (which is what :attr:`CachedPrompt.text` is) makes a double blind
    to the system half: an inverted system contract would replay the identical recorded
    answer while a live model returned something else entirely.

    It composes through :func:`_compose`, the same function
    :class:`llm.client.AnthropicLLM` builds its request from, so the key and the wire
    cannot disagree about what a call *is*.

    Args:
        prompt: a :class:`CachedPrompt` (its two halves are used as they are sent) or a
            plain string (the whole user turn).
        system: per-call system text, appended after a ``CachedPrompt``'s static context
            in the same order :meth:`CachedPrompt.to_system_blocks` emits it.

    Returns:
        ``(system_key, user_text)``, where ``system_key`` is a :data:`SystemKey` — the
        block **texts themselves**, as a tuple once there is more than one of them, never
        a joined string. Two block lists are equal keys only if they are the same list, so
        no character exists that a block's own text could contain to forge a collision;
        that is the whole point, and it is what :func:`llm.prompting.system_key_text`
        must not be used for. A plain string with no system still keys on exactly
        ``("", prompt)``.
    """
    blocks, cached = _compose(prompt, system, cache=False)
    return (
        canonical_system_key([str(block["text"]) for block in blocks]),
        cached.dynamic_tail,
    )


def assemble_prompt(
    static_context: str | Sequence[str],
    dynamic: str | Sequence[str] = "",
    *,
    separator: str = SECTION_SEPARATOR,
) -> CachedPrompt:
    """Assemble a prompt static-context-first (C4).

    Args:
        static_context: the reusable prefix, as one string or a sequence of sections
            joined by ``separator``. Must contain something: an empty static context has
            no cacheable prefix at all, which is the bug this function exists to prevent.
        dynamic: the per-request tail, as one string or a sequence of sections.
        separator: the join string, also used between the two halves.

    Returns:
        A :class:`CachedPrompt`. The static half is always first.

    Raises:
        PromptAssemblyError: if ``static_context`` is empty or whitespace.
    """
    static_text = _join(static_context, separator)
    if not static_text.strip():
        raise PromptAssemblyError(
            "static_context is empty: there would be no cacheable prefix. Pass the "
            "store envelope / policy / tool contract — the bytes that repeat across "
            "requests — as the first argument, and only this request's text as the tail."
        )
    return CachedPrompt(
        static_context=static_text,
        dynamic_tail=_join(dynamic, separator),
        separator=separator,
    )
