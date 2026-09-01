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

from llm.client import AnthropicLLM, LLMClient, build_llm, response_text
from llm.config import (
    API_KEY_ENV_VAR,
    DEFAULT_BUYER_MODEL,
    DEFAULT_EXTRACT_MODEL,
    DEFAULT_INTERVIEW_MODEL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_BY_ROLE,
    DEFAULT_PROVIDER,
    DEFAULT_STORE_AGENT_MODEL,
    KNOWN_ROLES,
    PROVIDER_ANTHROPIC,
    PROVIDER_DOUBLE,
    PROVIDER_ENV_VAR,
    ROLE_BUYER,
    ROLE_ENV_VARS,
    ROLE_EXTRACT,
    ROLE_INTERVIEW,
    ROLE_STORE_AGENT,
    SUPPORTED_PROVIDERS,
    default_model,
    model_env_var,
    resolve_api_key,
    resolve_max_tokens,
    resolve_model,
    resolve_provider,
)
from llm.doubles import DeterministicLLM, LLMCall, RecordedLLM, prompt_text
from llm.errors import (
    LLMError,
    MissingApiKeyError,
    PromptAssemblyError,
    ProviderNotConfiguredError,
    RecordingError,
    UnknownRoleError,
    UnrecordedPromptError,
)
from llm.prompting import CACHE_CONTROL, SECTION_SEPARATOR, CachedPrompt, assemble_prompt
from llm.recordings import (
    RECORDINGS_DIR,
    REQUIRED_PROVENANCE_FIELDS,
    available_recordings,
    load_provenance,
    load_recording,
    load_recording_file,
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
    "KNOWN_ROLES",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_DOUBLE",
    "PROVIDER_ENV_VAR",
    "RECORDINGS_DIR",
    "REQUIRED_PROVENANCE_FIELDS",
    "ROLE_BUYER",
    "ROLE_ENV_VARS",
    "ROLE_EXTRACT",
    "ROLE_INTERVIEW",
    "ROLE_STORE_AGENT",
    "SECTION_SEPARATOR",
    "SUPPORTED_PROVIDERS",
    "AnthropicLLM",
    "CachedPrompt",
    "DeterministicLLM",
    "LLMCall",
    "LLMClient",
    "LLMError",
    "MissingApiKeyError",
    "PromptAssemblyError",
    "ProviderNotConfiguredError",
    "RecordedLLM",
    "RecordingError",
    "UnknownRoleError",
    "UnrecordedPromptError",
    "assemble_prompt",
    "available_recordings",
    "build_llm",
    "default_model",
    "load_provenance",
    "load_recording",
    "load_recording_file",
    "model_env_var",
    "prompt_text",
    "resolve_api_key",
    "resolve_max_tokens",
    "resolve_model",
    "resolve_provider",
    "response_text",
]
