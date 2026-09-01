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
        block: dict[str, Any] = {"type": "text", "text": self.static_context}
        if cache:
            block["cache_control"] = dict(CACHE_CONTROL)
        blocks = [block]
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


def wire_key(prompt: object, system: str | None = None) -> tuple[str, str]:
    """The ``(system_text, user_text)`` pair a request actually carries.

    This is the canonical identity of a call, and it is what the doubles key on. The two
    halves travel separately — the system blocks and the user turn — so keying on the two
    of them concatenated (which is what :attr:`CachedPrompt.text` is) makes a double blind
    to the system half: an inverted system contract would replay the identical recorded
    answer while a live model returned something else entirely.

    Args:
        prompt: a :class:`CachedPrompt` (its two halves are used as they are sent) or a
            plain string (the whole user turn).
        system: per-call system text, appended after a ``CachedPrompt``'s static context
            in the same order :meth:`CachedPrompt.to_system_blocks` emits it.

    Returns:
        ``(system_text, user_text)``. Several system blocks are joined with
        :data:`SECTION_SEPARATOR` — a canonicalization for lookup, not a claim about the
        wire format, which keeps them as separate blocks.
    """
    if isinstance(prompt, CachedPrompt):
        blocks = prompt.to_system_blocks(cache=False, extra=system)
        return (SECTION_SEPARATOR.join(block["text"] for block in blocks), prompt.dynamic_tail)
    text = prompt if isinstance(prompt, str) else str(prompt)
    return (system or "", text)


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
