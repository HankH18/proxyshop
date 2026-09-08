"""``packages/llm`` — the Anthropic client wrapper, prompt assembly, and the test doubles.

Import namespace note: the source layout is FLAT (``packages/llm/src/<module>.py``, D42),
and the tracked symlink ``.pkgroot/llm`` makes it importable as ``llm.<module>``. The same
public surface is re-exported from ``packages.llm`` for the acceptance suite; both names
resolve to *these* module objects, so there is exactly one ``RecordedLLM`` class.

The five things most callers want::

    from llm import RecordedLLM, assemble_prompt, build_llm, resolve_model

* :func:`llm.config.resolve_model` — role -> model id, from the environment (C4).
* :func:`llm.prompting.assemble_prompt` — static store context first, dynamic tail last,
  so the prefix is cacheable (C4).
* :class:`llm.doubles.RecordedLLM` — strict, offline replay of reviewed fixtures (D21).
* :class:`llm.doubles.DeterministicLLM` — offline, content-derived replies.
* :func:`llm.client.build_llm` — provider dispatch; the double is the default (D20).

Importing this package touches no network and does not import the ``anthropic`` SDK.
"""

from __future__ import annotations

from llm.boot import (
    SDK_DISTRIBUTION,
    STATUS_LIVE,
    STATUS_TEMPLATED,
    LLMRuntime,
    describe_llm_runtime,
    forget_llm_runtime_reports,
    log_llm_runtime,
    sdk_installed,
)
from llm.client import (
    RESERVED_REQUEST_FIELDS,
    AnthropicLLM,
    LLMClient,
    build_llm,
    response_text,
)
from llm.config import (
    API_KEY_ENV_VAR,
    DEFAULT_BUYER_MODEL,
    DEFAULT_EXTRACT_MODEL,
    DEFAULT_INTERVIEW_MODEL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_BY_ROLE,
    DEFAULT_PROVIDER,
    DEFAULT_STORE_AGENT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    KNOWN_ROLES,
    MAX_TOKENS_ENV_VAR,
    PROVIDER_ANTHROPIC,
    PROVIDER_DOUBLE,
    PROVIDER_ENV_VAR,
    ROLE_BUYER,
    ROLE_ENV_VARS,
    ROLE_EXTRACT,
    ROLE_INTERVIEW,
    ROLE_STORE_AGENT,
    SUPPORTED_PROVIDERS,
    TIMEOUT_ENV_VAR,
    default_model,
    model_env_var,
    resolve_api_key,
    resolve_max_tokens,
    resolve_model,
    resolve_provider,
    resolve_timeout,
)
from llm.doubles import (
    DeterministicLLM,
    KeyedLLMCall,
    LLMCall,
    RecordedLLM,
    normalize_recording_key,
    prompt_text,
)
from llm.errors import (
    EmptyReplyError,
    LLMError,
    MissingApiKeyError,
    ModelOverrideError,
    PromptAssemblyError,
    ProviderNotConfiguredError,
    RecordingError,
    TruncatedReplyError,
    UnknownRoleError,
    UnrecordedPromptError,
)
from llm.prompting import (
    CACHE_CONTROL,
    SECTION_SEPARATOR,
    CachedPrompt,
    SystemKey,
    assemble_prompt,
    canonical_system_key,
    compose_request,
    system_key_blocks,
    system_key_text,
    wire_key,
)
from llm.recordings import (
    RECORDINGS_DIR,
    REQUIRED_PROVENANCE_FIELDS,
    available_recordings,
    load_all_recordings,
    load_provenance,
    load_recording,
    load_recording_file,
    load_system_contract,
    recording_path,
)

__all__ = [
    "API_KEY_ENV_VAR",
    "CACHE_CONTROL",
    "DEFAULT_BUYER_MODEL",
    "DEFAULT_EXTRACT_MODEL",
    "DEFAULT_INTERVIEW_MODEL",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL_BY_ROLE",
    "DEFAULT_PROVIDER",
    "DEFAULT_STORE_AGENT_MODEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "KNOWN_ROLES",
    "MAX_TOKENS_ENV_VAR",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_DOUBLE",
    "PROVIDER_ENV_VAR",
    "RESERVED_REQUEST_FIELDS",
    "RECORDINGS_DIR",
    "REQUIRED_PROVENANCE_FIELDS",
    "ROLE_BUYER",
    "ROLE_ENV_VARS",
    "ROLE_EXTRACT",
    "ROLE_INTERVIEW",
    "ROLE_STORE_AGENT",
    "SDK_DISTRIBUTION",
    "SECTION_SEPARATOR",
    "STATUS_LIVE",
    "STATUS_TEMPLATED",
    "SUPPORTED_PROVIDERS",
    "TIMEOUT_ENV_VAR",
    "AnthropicLLM",
    "CachedPrompt",
    "DeterministicLLM",
    "EmptyReplyError",
    "KeyedLLMCall",
    "LLMCall",
    "LLMClient",
    "LLMError",
    "LLMRuntime",
    "MissingApiKeyError",
    "ModelOverrideError",
    "PromptAssemblyError",
    "ProviderNotConfiguredError",
    "RecordedLLM",
    "RecordingError",
    "SystemKey",
    "TruncatedReplyError",
    "UnknownRoleError",
    "UnrecordedPromptError",
    "assemble_prompt",
    "available_recordings",
    "build_llm",
    "canonical_system_key",
    "compose_request",
    "default_model",
    "describe_llm_runtime",
    "forget_llm_runtime_reports",
    "load_all_recordings",
    "load_provenance",
    "load_recording",
    "load_system_contract",
    "load_recording_file",
    "log_llm_runtime",
    "model_env_var",
    "normalize_recording_key",
    "prompt_text",
    "recording_path",
    "resolve_api_key",
    "resolve_max_tokens",
    "resolve_model",
    "resolve_provider",
    "resolve_timeout",
    "response_text",
    "sdk_installed",
    "system_key_blocks",
    "system_key_text",
    "wire_key",
]
