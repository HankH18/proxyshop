"""The Anthropic client wrapper, and the provider dispatch (D20).

**Nothing in this module imports the ``anthropic`` SDK at module scope, and nothing
constructs a client at import time.** That is D20, and it is load-bearing rather than
stylistic: five packages import this one, their tests run with no API key and no network
(D3), and an import-time client would take every one of those suites down on this machine.
``import anthropic`` happens inside :meth:`AnthropicLLM._ensure_client`, which only runs
when someone actually calls a live model.

Choose an implementation with :func:`build_llm`, which reads ``LLM_PROVIDER`` and defaults
to the offline double::

    client = build_llm("store_agent")                     # -> DeterministicLLM (env unset)
    client = build_llm("store_agent", recordings=table)   # -> RecordedLLM
    client = build_llm("store_agent", provider="anthropic")  # -> AnthropicLLM
    # ...or the same first call, with LLM_PROVIDER=anthropic exported (D20).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from llm.config import (
    MAX_TOKENS_ENV_VAR,
    PROVIDER_ANTHROPIC,
    PROVIDER_DOUBLE,
    SUPPORTED_PROVIDERS,
    resolve_api_key,
    resolve_max_tokens,
    resolve_model,
    resolve_provider,
    resolve_timeout,
)
from llm.config import model_env_var as _validate_role
from llm.doubles import DeterministicLLM, RecordedLLM
from llm.errors import (
    EmptyReplyError,
    MissingApiKeyError,
    ModelOverrideError,
    ProviderNotConfiguredError,
    TruncatedReplyError,
)
from llm.prompting import compose_request

#: Request fields this wrapper owns; a per-call keyword may not overwrite them.
#:
#: ``model`` is the one that matters: C4 makes model ids config rather than code, and a
#: ``complete(prompt, model="...")`` keyword walks past both :mod:`llm.config` and the
#: frozen AST scan — the scan can only see literals in this package, never a value a
#: caller passes in. ``messages`` is here because overwriting it would silently discard
#: the cache boundary this module just built.
#:
#: ``system`` is listed for **documentation only** and the guard can never fire for it:
#: :meth:`AnthropicLLM.complete` declares ``system`` as a keyword-only *named* parameter,
#: so it is bound there and can never land in ``**kwargs``. Deleting it from this set
#: would change no behaviour whatsoever — which is exactly why
#: ``test_the_reserved_field_guard_is_live_for_every_field_that_can_reach_kwargs``
#: parametrizes over the fields that CAN reach ``**kwargs`` rather than over this set, and
#: a separate test pins the signature that makes ``system`` unreachable. Keep the name
#: here so a reader adding a field knows ``system`` is owned too, not merely forgotten.
RESERVED_REQUEST_FIELDS: frozenset[str] = frozenset({"model", "messages", "system"})

#: ``stop_reason`` values that mean "there is no usable reply here".
TRUNCATED_STOP_REASON = "max_tokens"


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
        cache_system: bool = True,
        **kwargs: Any,
    ) -> str:
        """Send ``prompt`` and return the reply text.

        Args:
            prompt: a :class:`llm.prompting.CachedPrompt` — strongly preferred, because it
                carries the cache boundary and this method turns it into a cached system
                prefix plus a dynamic user turn (C4) — or a plain string.
            system: static system text.

                * With a plain **string** prompt there is no other static half, so
                  ``system`` **is** the static context: it becomes the request's one
                  system block and, by default, the cached prefix.
                * With a :class:`~llm.prompting.CachedPrompt` the prompt already owns the
                  static context, so ``system`` is **appended after** it as a second,
                  uncached block — never merged in front of it, because anything in front
                  of the store envelope pushes it off byte zero and no request is ever a
                  cache hit again.
            max_tokens: per-call override.
            cache_system: attach the ``cache_control`` breakpoint to the static block.

                Leave it on for text that repeats across calls; turn it **off** for a
                string-prompt call whose ``system`` varies per call (a turn counter, a
                timestamp, a request id), because that writes a cache entry no later call
                can read. The flag exists because ``system=`` alone does not say which of
                those two it is, and the answer used to depend on the runtime type of a
                *different* argument: a ``CachedPrompt`` prompt made ``system`` uncached,
                a string prompt made the same text cached.
            **kwargs: passed through to ``messages.create``.

        Raises:
            ModelOverrideError: if a per-call keyword names a field in
                :data:`RESERVED_REQUEST_FIELDS` that can reach ``**kwargs``.
            llm.errors.PromptAssemblyError: if there is no user turn to send.
        """
        reserved = RESERVED_REQUEST_FIELDS & set(kwargs)
        if reserved:
            raise ModelOverrideError(
                f"{', '.join(sorted(reserved))} cannot be passed as a per-call keyword: "
                f"this wrapper owns those fields. The model comes from "
                f"{self.role!r}'s environment variable (C4 — model ids are config, not "
                f"code), and messages/system carry the cache boundary. Configure the "
                f"env var, or construct AnthropicLLM(role, model=...) deliberately."
            )

        # ONE composer, shared with llm.prompting.wire_key — which is what the offline
        # doubles key on. Composing the request here instead is how a recording becomes
        # unreachable through the very client it was recorded for, silently.
        system_blocks, messages = compose_request(prompt, system, cache=cache_system)

        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "messages": messages,
        }
        if system_blocks:
            request["system"] = system_blocks
        request.update(kwargs)
        response = self._ensure_client().messages.create(**request)
        return self._reply_text(response, request["max_tokens"])

    def _reply_text(self, response: Any, max_tokens: int) -> str:
        """Extract the reply, refusing to hand back a fragment or an empty string.

        Two 200-response shapes have no usable reply, and both used to come back as a
        plain string with no exception:

        * ``stop_reason == "max_tokens"`` — the model was cut off mid-reply. For the
          extraction role this is a truncated JSON document, and the consumer sees a
          ``JSONDecodeError`` at a random column that names nothing.
        * a ``refusal`` or ``tool_use`` stop with no text block at all — a store agent
          would answer a buyer with ``""``. The store-agent fixtures advertise three
          provenance tools, so ``tool_use`` is reachable, and this wrapper does not run
          tool loops.
        """
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason is None and isinstance(response, Mapping):
            stop_reason = response.get("stop_reason")
        text = response_text(response)

        if stop_reason == TRUNCATED_STOP_REASON:
            raise TruncatedReplyError(
                f"the model stopped at max_tokens={max_tokens}, so this reply is a "
                f"fragment ({len(text)} chars) and must not be parsed or shown. Raise "
                f"{MAX_TOKENS_ENV_VAR}, pass max_tokens=..., or ask for less. The "
                f"partial text is on the exception's `.partial`.",
                text,
            )
        if not text:
            raise EmptyReplyError(
                f"the response carried no text block (stop_reason={stop_reason!r}), so "
                f"there is nothing to return. A `refusal` stop needs a prompt change; a "
                f"`tool_use` stop needs a tool loop, which this wrapper deliberately does "
                f"not implement (T-041 owns the advocate runtime).",
                stop_reason if isinstance(stop_reason, str) else None,
            )
        return text

    def __repr__(self) -> str:
        built = "built" if self._client is not None else "not built"
        return f"<AnthropicLLM role={self.role!r} model={self._model!r} client={built}>"


def build_llm(
    role: str,
    *,
    provider: str | None = None,
    recordings: Mapping[Any, str] | None = None,
    env: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> LLMClient:
    """Return the client for ``role`` selected by ``LLM_PROVIDER`` (D20: default double).

    Args:
        role: one of :data:`llm.config.KNOWN_ROLES`.
        provider: overrides ``LLM_PROVIDER``. Normalised the same way
            :func:`llm.config.resolve_provider` normalises the env var — stripped and
            lower-cased — so ``provider="Anthropic"`` and ``LLM_PROVIDER=Anthropic``
            select the same thing. They used to disagree: the env var was normalised and
            the keyword was matched verbatim, so the keyword raised
            ``ProviderNotConfiguredError`` on a spelling the env var accepted.
        recordings: **double-only.** Replay this table strictly
            (:class:`~llm.doubles.RecordedLLM`) instead of answering deterministically.
            Keys may be prompt strings, ``(system, prompt)`` pairs, or ``CachedPrompt``s.
            Passing it with a non-double provider raises rather than silently dropping the
            recordings and returning a live client.
        env: environment mapping to resolve from.
        **kwargs: forwarded to :class:`AnthropicLLM`, so they are accepted **only** on the
            ``anthropic`` path. On the double path they would be ignored, so they raise:
            ``build_llm("buyer", model="…", api_key="…")`` used to return a double that
            had quietly discarded every one of them.

    Returns:
        Whichever client the provider selects. All three expose ``.complete()``
        (:class:`LLMClient`) and all three expose ``.model``; on a double that is the
        label ``"double:<role>"`` rather than an Anthropic model id, because a double
        sends no request and has no model — see :attr:`llm.doubles._RecordingBase.model`.

    Raises:
        UnknownRoleError: if ``role`` is not one of :data:`llm.config.KNOWN_ROLES` —
            on every provider path, including the offline double.
        ProviderNotConfiguredError: for an unimplemented provider name.
        TypeError: for a keyword the selected provider would ignore — an unusable
            ``recordings=``, or any ``**kwargs`` on a path that does not forward them.
    """
    # Validate the role on EVERY path. It used to be checked only where the live client
    # resolved a model, so `build_llm("store-agent")` — the hyphenated directory name —
    # worked offline and raised only under LLM_PROVIDER=anthropic. Every test in this
    # repo runs offline (D3), so the typo would have reached production unexercised.
    _validate_role(role)

    name = provider.strip().lower() if provider is not None else resolve_provider(env)
    if name not in SUPPORTED_PROVIDERS:
        raise ProviderNotConfiguredError(
            f"unknown LLM provider {name!r}; supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    # Refuse a keyword this provider would throw away. Silently accepting one is worse
    # than a TypeError: `build_llm(role, model="claude-x")` under D20's default read as a
    # configured client and was a DeterministicLLM that had discarded the model, and
    # `recordings=` with a live provider read as offline replay and was a live client.
    if recordings is not None and name != PROVIDER_DOUBLE:
        raise TypeError(
            f"recordings= only applies to the offline double, but provider {name!r} was "
            f"selected, so this table would be discarded and a live client returned. "
            f"Pass provider={PROVIDER_DOUBLE!r} (or leave LLM_PROVIDER unset — D20 makes "
            f"the double the default), or drop recordings=."
        )
    if kwargs and name != PROVIDER_ANTHROPIC:
        raise TypeError(
            f"{', '.join(sorted(kwargs))} cannot be passed with provider {name!r}: those "
            f"keywords are forwarded to AnthropicLLM and provider {name!r} ignores them, "
            f"so they would be silently discarded. Configure the double explicitly "
            f"(recordings=, or llm.doubles.DeterministicLLM(role=..., default=...)), or "
            f"select provider={PROVIDER_ANTHROPIC!r}."
        )

    if name == PROVIDER_DOUBLE:
        if recordings is not None:
            return RecordedLLM(recordings, role=role)
        return DeterministicLLM(role=role)
    return AnthropicLLM(role, env=env, **kwargs)
