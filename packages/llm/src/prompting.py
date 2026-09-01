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

    The invariant every consumer may rely on::

        prompt.text == prompt.cacheable_prefix + prompt.dynamic_tail
        prompt.cache_boundary == len(prompt.cacheable_prefix)
        prompt.text.startswith(prompt.static_context)
    """

    static_context: str
    dynamic_tail: str = ""
    separator: str = SECTION_SEPARATOR

    @property
    def cacheable_prefix(self) -> str:
        """Everything up to the cache boundary, separator included."""
        if not self.dynamic_tail:
            return self.static_context
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

    def to_system_blocks(self, *, cache: bool = True) -> list[dict[str, Any]]:
        """The static context as Anthropic ``system`` blocks, cache breakpoint attached.

        Args:
            cache: attach ``cache_control`` to the final static block. Off is for
                one-shot calls where a cache write would never be read back.
        """
        block: dict[str, Any] = {"type": "text", "text": self.static_context}
        if cache:
            block["cache_control"] = dict(CACHE_CONTROL)
        return [block]

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
