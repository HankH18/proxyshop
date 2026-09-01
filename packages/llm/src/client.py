"""The Anthropic client wrapper, and the provider dispatch (D20).

**Nothing in this module imports the ``anthropic`` SDK at module scope, and nothing
constructs a client at import time.** That is D20, and it is load-bearing rather than
stylistic: five packages import this one, their tests run with no API key and no network
(D3), and an import-time client would take every one of those suites down on this machine.
``import anthropic`` happens inside :meth:`AnthropicLLM._ensure_client`, which only runs
when someone actually calls a live model.

Choose an implementation with :func:`build_llm`, which reads ``LLM_PROVIDER`` and defaults
to the offline double::

    client = build_llm("store_agent")            # -> DeterministicLLM (LLM_PROVIDER unset)
    client = build_llm("store_agent", recordings=table)   # -> RecordedLLM
    client = build_llm("store_agent")            # -> AnthropicLLM, iff LLM_PROVIDER=anthropic
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from llm.config import (
    PROVIDER_ANTHROPIC,
    PROVIDER_DOUBLE,
    SUPPORTED_PROVIDERS,
    resolve_api_key,
    resolve_max_tokens,
    resolve_model,
    resolve_provider,
    resolve_timeout,
)
from llm.doubles import DeterministicLLM, RecordedLLM
from llm.errors import MissingApiKeyError, ProviderNotConfiguredError
from llm.prompting import CachedPrompt


@runtime_checkable
class LLMClient(Protocol):
    """What every consumer of this package may depend on.

    :class:`AnthropicLLM`, :class:`llm.doubles.RecordedLLM` and
    :class:`llm.doubles.DeterministicLLM` all satisfy it, so a caller annotates against
    this and is handed whichever one the environment selected.
    """

    def complete(self, prompt: Any, **kwargs: Any) -> str:
        """Send a prompt, return the reply text."""
        ...


def response_text(response: Any) -> str:
    """Concatenate the text blocks of an Anthropic Messages response.

    Tolerates both the SDK's objects and plain dicts, which is what lets a test inject a
    fake client without importing the SDK at all.
    """
    content = getattr(response, "content", None)
    if content is None and isinstance(response, Mapping):
        content = response.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, Mapping):
            if block.get("type", "text") == "text":
                parts.append(str(block.get("text", "")))
            continue
        if getattr(block, "type", "text") == "text":
            parts.append(str(getattr(block, "text", "")))
    return "".join(parts)


class AnthropicLLM:
    """A thin, lazy wrapper over ``anthropic.Anthropic`` for one role.

    Args:
        role: one of :data:`llm.config.KNOWN_ROLES`. Decides the model via
            :func:`llm.config.resolve_model` — i.e. from the environment (C4).
        model: overrides the resolved model id. Rare; prefer configuring the env var.
        api_key: overrides ``ANTHROPIC_API_KEY``.
        max_tokens: overrides ``LLM_MAX_TOKENS``.
        timeout: overrides ``LLM_TIMEOUT_SECONDS``.
        client: a pre-built client object. Injecting one is how tests exercise this class
            with no SDK, no key and no network.
        env: environment mapping to resolve from; defaults to ``os.environ``.

    Constructing this object performs **no** I/O and imports **no** SDK — see the module
    docstring. The first :meth:`complete` call is what builds the real client.
    """

    def __init__(
        self,
        role: str,
        *,
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        client: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.role = role
        self._env = env
        self._model = model if model else resolve_model(role, env)
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._client = client

    @property
    def model(self) -> str:
        """The model id this client will send, resolved from the environment (C4)."""
        return self._model

    @property
    def max_tokens(self) -> int:
        return self._max_tokens if self._max_tokens is not None else resolve_max_tokens(self._env)

    @property
    def timeout(self) -> float:
        return self._timeout if self._timeout is not None else resolve_timeout(self._env)

    def _ensure_client(self) -> Any:
        """Build the SDK client on first use. The only place the SDK is imported."""
        if self._client is None:
            api_key = self._api_key or resolve_api_key(self._env)
            if not api_key:
                raise MissingApiKeyError(
                    "ANTHROPIC_API_KEY is not set, so the live Anthropic client cannot be "
                    "built. Verification is offline (D3): leave LLM_PROVIDER at its "
                    "default `double`, or pass a double explicitly."
                )
            # D20: the SDK import lives HERE, not at module scope. Moving it to the top
            # of the file is what breaks every offline suite that imports this package.
            import anthropic

            self._client = anthropic.Anthropic(api_key=api_key, timeout=self.timeout)
        return self._client

    def complete(
        self,
        prompt: Any,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> str:
        """Send ``prompt`` and return the reply text.

        Args:
            prompt: a :class:`llm.prompting.CachedPrompt` — strongly preferred, because it
                carries the cache boundary and this method turns it into a cached system
                prefix plus a dynamic user turn (C4) — or a plain string.
            system: static system text. With a string prompt this becomes the cacheable
                prefix; with a :class:`~llm.prompting.CachedPrompt` it is prepended to the
                prompt's own static context.
            max_tokens: per-call override.
            **kwargs: passed through to ``messages.create``.
        """
        cached = prompt if isinstance(prompt, CachedPrompt) else None
        if cached is None:
            cached = CachedPrompt(static_context=system or "", dynamic_tail=str(prompt))
            system_blocks = cached.to_system_blocks() if system else []
        else:
            if system:
                cached = CachedPrompt(
                    static_context=f"{system}{cached.separator}{cached.static_context}",
                    dynamic_tail=cached.dynamic_tail,
                    separator=cached.separator,
                )
            system_blocks = cached.to_system_blocks()

        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "messages": cached.to_messages(),
        }
        if system_blocks:
            request["system"] = system_blocks
        request.update(kwargs)
        response = self._ensure_client().messages.create(**request)
        return response_text(response)

    def __repr__(self) -> str:
        built = "built" if self._client is not None else "not built"
        return f"<AnthropicLLM role={self.role!r} model={self._model!r} client={built}>"


def build_llm(
    role: str,
    *,
    provider: str | None = None,
    recordings: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> LLMClient:
    """Return the client for ``role`` selected by ``LLM_PROVIDER`` (D20: default double).

    Args:
        role: one of :data:`llm.config.KNOWN_ROLES`.
        provider: overrides ``LLM_PROVIDER``.
        recordings: when the double is selected, replay this table strictly
            (:class:`~llm.doubles.RecordedLLM`) instead of answering deterministically.
        env: environment mapping to resolve from.
        **kwargs: forwarded to :class:`AnthropicLLM` when the live provider is selected.

    Raises:
        ProviderNotConfiguredError: for an unimplemented provider name.
    """
    name = provider if provider is not None else resolve_provider(env)
    if name == PROVIDER_DOUBLE:
        if recordings is not None:
            return RecordedLLM(recordings, role=role)
        return DeterministicLLM(role=role)
    if name == PROVIDER_ANTHROPIC:
        return AnthropicLLM(role, env=env, **kwargs)
    raise ProviderNotConfiguredError(
        f"unknown LLM provider {name!r}; supported: {', '.join(SUPPORTED_PROVIDERS)}"
    )
