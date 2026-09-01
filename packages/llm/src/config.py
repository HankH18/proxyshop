"""Per-role model configuration (C4: model ids are config, not code).

Every call site asks :func:`resolve_model` which model a *role* should use, and the answer
comes from the environment:

==============  ======================  ===========================
role            environment variable    documented default
==============  ======================  ===========================
``buyer``       ``BUYER_MODEL``         :data:`DEFAULT_BUYER_MODEL`
``store_agent`` ``STORE_AGENT_MODEL``   :data:`DEFAULT_STORE_AGENT_MODEL`
``interview``   ``INTERVIEW_MODEL``     :data:`DEFAULT_INTERVIEW_MODEL`
``extract``     ``EXTRACT_MODEL``       :data:`DEFAULT_EXTRACT_MODEL`
==============  ======================  ===========================

The four defaults are exactly the values ``.env.example`` ships (sonnet-class for the two
conversational roles, opus-class for the onboarding interview, haiku-class for extraction
— DESIGN §Decisions). **They are not a fallback nobody hits**: a worktree has no ``.env``
(it is gitignored and never generated), so unless a process exports the variables itself,
these constants are what actually runs.

Hard-coded ids live *only* in the module-level ``DEFAULT_*`` constants below, and that is
enforced mechanically rather than by convention: the frozen acceptance suite AST-scans
every ``.py`` file under ``packages/llm`` and fails on a ``"claude-…"`` literal found
anywhere else — inside a function, in a dict in a class body, in a default argument.
Adding a role therefore means adding a constant here, never a literal at a call site.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from llm.errors import ProviderNotConfiguredError, UnknownRoleError

# --------------------------------------------------------------------------------------
# roles
# --------------------------------------------------------------------------------------

ROLE_BUYER = "buyer"
ROLE_STORE_AGENT = "store_agent"
ROLE_INTERVIEW = "interview"
ROLE_EXTRACT = "extract"

#: role -> the environment variable that overrides its model. Frozen spellings: the
#: acceptance suite asserts this exact mapping.
ROLE_ENV_VARS: Mapping[str, str] = {
    ROLE_BUYER: "BUYER_MODEL",
    ROLE_STORE_AGENT: "STORE_AGENT_MODEL",
    ROLE_INTERVIEW: "INTERVIEW_MODEL",
    ROLE_EXTRACT: "EXTRACT_MODEL",
}

KNOWN_ROLES: tuple[str, ...] = tuple(ROLE_ENV_VARS)

# --------------------------------------------------------------------------------------
# documented defaults — the ONLY place a model id may be written down (C4)
# --------------------------------------------------------------------------------------

DEFAULT_BUYER_MODEL = "claude-sonnet-4-5"
DEFAULT_STORE_AGENT_MODEL = "claude-sonnet-4-5"
DEFAULT_INTERVIEW_MODEL = "claude-opus-4-1"
DEFAULT_EXTRACT_MODEL = "claude-haiku-4-5"

DEFAULT_MODEL_BY_ROLE: Mapping[str, str] = {
    ROLE_BUYER: DEFAULT_BUYER_MODEL,
    ROLE_STORE_AGENT: DEFAULT_STORE_AGENT_MODEL,
    ROLE_INTERVIEW: DEFAULT_INTERVIEW_MODEL,
    ROLE_EXTRACT: DEFAULT_EXTRACT_MODEL,
}

# --------------------------------------------------------------------------------------
# provider selection (D20)
# --------------------------------------------------------------------------------------

PROVIDER_ENV_VAR = "LLM_PROVIDER"
PROVIDER_DOUBLE = "double"
PROVIDER_ANTHROPIC = "anthropic"
SUPPORTED_PROVIDERS: tuple[str, ...] = (PROVIDER_DOUBLE, PROVIDER_ANTHROPIC)

#: D20: the offline double is the default. The live client is only ever selected by an
#: explicit ``LLM_PROVIDER=anthropic``.
DEFAULT_PROVIDER = PROVIDER_DOUBLE

API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
MAX_TOKENS_ENV_VAR = "LLM_MAX_TOKENS"
DEFAULT_MAX_TOKENS = 1024

TIMEOUT_ENV_VAR = "LLM_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 60.0


def _environ(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def model_env_var(role: str) -> str:
    """The environment variable that configures ``role``.

    Raises:
        UnknownRoleError: if ``role`` is not one of :data:`KNOWN_ROLES`. ``TypeError`` is
            caught too, so an unhashable argument gets the same legible message instead of
            a bare "unhashable type" from the dict lookup.
    """
    try:
        return ROLE_ENV_VARS[role]
    except (KeyError, TypeError):
        raise UnknownRoleError(role, KNOWN_ROLES) from None


def default_model(role: str) -> str:
    """The documented default model id for ``role`` (used when its env var is unset)."""
    model_env_var(role)  # validates the role and raises the same legible error
    return DEFAULT_MODEL_BY_ROLE[role]


def resolve_model(role: str, env: Mapping[str, str] | None = None) -> str:
    """Return the model id to use for ``role``.

    Args:
        role: one of :data:`KNOWN_ROLES`.
        env: environment to read; defaults to :data:`os.environ`. Passing a plain dict is
            the way to test configuration without touching process state.

    Returns:
        The value of the role's environment variable **verbatim** when it is set to
        anything non-blank, otherwise the documented default from
        :data:`DEFAULT_MODEL_BY_ROLE`. A variable set to ``""`` or to whitespace is
        treated as unset — an empty ``BUYER_MODEL=`` line in a ``.env`` is a typo, and
        sending an empty model id to the API is a 400 nobody can read.

    Raises:
        UnknownRoleError: if ``role`` is not a configured role.
    """
    env_var = model_env_var(role)
    configured = _environ(env).get(env_var)
    if configured is not None and configured.strip():
        return configured
    return DEFAULT_MODEL_BY_ROLE[role]


def resolve_provider(env: Mapping[str, str] | None = None) -> str:
    """Return the selected provider name, defaulting to ``"double"`` (D20).

    Raises:
        ProviderNotConfiguredError: if ``LLM_PROVIDER`` names something unimplemented.
            Failing here is deliberate: a typo like ``LLM_PROVIDER=antropic`` silently
            falling back to the double would make a live run quietly fake.
    """
    configured = (_environ(env).get(PROVIDER_ENV_VAR) or "").strip().lower()
    if not configured:
        return DEFAULT_PROVIDER
    if configured not in SUPPORTED_PROVIDERS:
        raise ProviderNotConfiguredError(
            f"{PROVIDER_ENV_VAR}={configured!r} is not implemented; "
            f"supported providers: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    return configured


def resolve_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """The Anthropic API key, or ``None``. D3: no key exists in a verify environment."""
    key = (_environ(env).get(API_KEY_ENV_VAR) or "").strip()
    return key or None


def resolve_max_tokens(env: Mapping[str, str] | None = None) -> int:
    """``LLM_MAX_TOKENS`` as an int, or :data:`DEFAULT_MAX_TOKENS`."""
    raw = (_environ(env).get(MAX_TOKENS_ENV_VAR) or "").strip()
    if not raw:
        return DEFAULT_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        raise ProviderNotConfiguredError(
            f"{MAX_TOKENS_ENV_VAR}={raw!r} is not an integer"
        ) from None
    if value <= 0:
        raise ProviderNotConfiguredError(f"{MAX_TOKENS_ENV_VAR}={raw!r} must be positive")
    return value


def resolve_timeout(env: Mapping[str, str] | None = None) -> float:
    """``LLM_TIMEOUT_SECONDS`` as a float, or :data:`DEFAULT_TIMEOUT_SECONDS`."""
    raw = (_environ(env).get(TIMEOUT_ENV_VAR) or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise ProviderNotConfiguredError(f"{TIMEOUT_ENV_VAR}={raw!r} is not a number") from None
    if value <= 0:
        raise ProviderNotConfiguredError(f"{TIMEOUT_ENV_VAR}={raw!r} must be positive")
    return value
