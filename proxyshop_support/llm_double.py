"""The offline LLM double (D19: ``LLM_PROVIDER=double``). Orchestrator-owned (T-000), frozen.

D3 makes verification offline: no ``ANTHROPIC_API_KEY`` and no network exist in a verify
run. Every code path that would call a model takes an LLM client object; in tests that
object is a :class:`LLMDouble`.

The double is *deterministic*: with no queued response it answers with a stable function of
``(role, prompt)``, so a test that asserts on the reply is reproducible without recording
anything. When a test needs a specific reply it queues one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LLMCall:
    """One recorded invocation of the double."""

    role: str
    prompt: str
    system: str | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)


class LLMDouble:
    """A deterministic, offline stand-in for the Anthropic client.

    Args:
        default: reply used when nothing is queued and no canned answer matches. ``None``
            selects the deterministic hash-derived reply.

    Attributes:
        calls: every :class:`LLMCall` made, in order. Assert on this to check prompt
            assembly (e.g. C4's "static store context precedes the dynamic tail").
    """

    def __init__(self, default: str | None = None) -> None:
        self._default = default
        self._queue: list[str] = []
        self._canned: dict[str, str] = {}
        self.calls: list[LLMCall] = []

    def queue(self, *responses: str) -> LLMDouble:
        """Queue replies, consumed one per :meth:`complete` call, FIFO."""
        self._queue.extend(responses)
        return self

    def when(self, substring: str, response: str) -> LLMDouble:
        """Reply with ``response`` whenever the prompt contains ``substring``."""
        self._canned[substring] = response
        return self

    def complete(
        self,
        prompt: str,
        *,
        role: str = "default",
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Return a reply and record the call. Never touches the network."""
        self.calls.append(LLMCall(role=role, prompt=prompt, system=system, kwargs=dict(kwargs)))
        if self._queue:
            return self._queue.pop(0)
        for needle, response in self._canned.items():
            if needle in prompt:
                return response
        if self._default is not None:
            return self._default
        return self.deterministic(role, prompt)

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        """:meth:`complete`, parsed as JSON. Raises ``json.JSONDecodeError`` on garbage."""
        return json.loads(self.complete(prompt, **kwargs))

    @property
    def last_prompt(self) -> str:
        """The most recent prompt. Raises ``IndexError`` if nothing has been asked yet."""
        return self.calls[-1].prompt

    @staticmethod
    def deterministic(role: str, prompt: str) -> str:
        """The stable default reply for a ``(role, prompt)`` pair.

        Shape: ``"double:<role>:<16 hex chars>"``. Stable across processes and runs.
        """
        digest = hashlib.sha256(f"{role}\x00{prompt}".encode()).hexdigest()[:16]
        return f"double:{role}:{digest}"

    def reset(self) -> None:
        """Drop recorded calls and queued replies; keep canned matches."""
        self.calls.clear()
        self._queue.clear()
