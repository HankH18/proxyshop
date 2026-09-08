"""What voice this process actually has, said out loud once, where an operator can see it.

D55 makes two pieces of prose the product: the platform's own case for a scraped shop, and
the in-network shop's case in its own voice. Both are written by an :class:`~llm.client
.LLMClient`, and D20 makes the offline :class:`~llm.doubles.DeterministicLLM` the default —
correctly, because verification is offline (D3) and a key must never be a precondition for
the suite. The cost of that correct default is that **a container running on templates and a
container running a model are indistinguishable from outside.** Both answer 200, both are
`(healthy)`, and the only difference is the sentence a shopper reads.

That was not a theoretical gap. Until the commit that added this module, no service image
installed the ``anthropic`` distribution at all, so ``LLM_PROVIDER=anthropic`` inside a
container did not select a live model — it selected a ``ModuleNotFoundError`` raised on the
first ``complete()``, three frames below a ``try/except`` that answers with the deterministic
slot-filled fallback. A deployment could be *configured* for a live model, be *charged*
nothing, emit no error, and serve string interpolation for the life of the image.

This module is the fix's other half: one line, at the moment the process decides which voice
it has, naming every input to that decision.

    from llm.boot import describe_llm_runtime, log_llm_runtime

    describe_llm_runtime("store_agent", env={}).live   # -> False
    log_llm_runtime("store_agent")                     # -> one line, INFO or WARNING

The line reads, for a default deployment::

    llm[store_agent] TEMPLATED: provider=double model=claude-sonnet-4-5 sdk=installed
    api_key=absent; LLM_PROVIDER is 'double' (D20's default), so the offline double answers
    and the deterministic slot-filled fallback is what a shopper reads. ...

**The key is never in the line.** :attr:`LLMRuntime.api_key_present` is a bool and the
rendered line says ``api_key=present`` or ``api_key=absent`` — never a value, never a prefix,
never a length. A log line is the single easiest place in a system to leak a credential and
this module exists to be read by operators, so it holds the bool and nothing else.

**Nothing here imports the SDK.** :func:`sdk_installed` asks
:func:`importlib.util.find_spec`, which locates the distribution without executing it, so
describing the runtime costs no import, no client, no socket and no key — the same property
:mod:`llm.client` protects and for the same reason (five packages import this tree and their
suites run with no network).
"""

from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

from llm.config import (
    API_KEY_ENV_VAR,
    PROVIDER_ANTHROPIC,
    PROVIDER_DOUBLE,
    PROVIDER_ENV_VAR,
    resolve_api_key,
    resolve_model,
    resolve_provider,
)
from llm.errors import ProviderNotConfiguredError

__all__ = [
    "LLMRuntime",
    "SDK_DISTRIBUTION",
    "STATUS_LIVE",
    "STATUS_TEMPLATED",
    "describe_llm_runtime",
    "forget_llm_runtime_reports",
    "log_llm_runtime",
    "sdk_installed",
]

#: The import name of the Anthropic SDK. The distribution and the import name agree, so this
#: one string is both what ``pip install`` puts in an image and what ``find_spec`` looks for.
SDK_DISTRIBUTION = "anthropic"

#: The word an operator greps for when a real model is writing.
STATUS_LIVE = "LIVE"

#: The word an operator greps for when the deterministic fallback is what a shopper reads.
STATUS_TEMPLATED = "TEMPLATED"

_log = logging.getLogger(__name__)

#: Lines already emitted, so a per-request call site cannot write one WARNING per request for
#: the life of the container. Keyed by the RENDERED LINE rather than by the role: a process
#: whose configuration changes underneath it (a test, a reload) has something new to say and
#: must be allowed to say it, while a process that keeps answering identically stays quiet.
_reported: set[str] = set()


def sdk_installed(name: str = SDK_DISTRIBUTION) -> bool:
    """Whether ``name`` is importable, **without importing it**.

    ``find_spec`` walks the meta path and returns a spec; it does not execute the module, so
    this is safe to call at boot in a process that must not build a client. It is also the
    only honest way to ask: ``pip show`` is not available to a running service, and a
    ``try: import anthropic`` would execute the SDK's own module-scope work just to answer a
    logging question.

    Returns ``False``, never raises, for every way the question can fail — a parent package
    that is itself missing (``ModuleNotFoundError``), a name that is not a valid module
    (``ValueError``), or a broken importer on the meta path.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001 - "cannot tell" is reported as "not installed"
        return False


@dataclass(frozen=True, slots=True)
class LLMRuntime:
    """Everything that decides whether a role speaks in a model's voice or a template's.

    Attributes:
        role: the :data:`llm.config.KNOWN_ROLES` member described.
        provider: the resolved ``LLM_PROVIDER``, or the rejected value verbatim when it names
            something unimplemented.
        model: the model id :func:`llm.config.resolve_model` would send for this role. It is
            reported even on the double path, because it is what an operator is *trying* to
            configure and a wrong id there is worth seeing before the provider is switched.
        sdk_installed: whether the ``anthropic`` distribution is present in this image.
        api_key_present: whether ``ANTHROPIC_API_KEY`` is set to something non-blank. **A
            bool, never the value** — see the module docstring.
        reason: why this role is on templates, or ``None`` when it is live.
    """

    role: str
    provider: str
    model: str
    sdk_installed: bool
    api_key_present: bool
    reason: str | None

    @property
    def live(self) -> bool:
        """``True`` when a real model writes this role's prose."""
        return self.reason is None

    @property
    def status(self) -> str:
        """:data:`STATUS_LIVE` or :data:`STATUS_TEMPLATED`."""
        return STATUS_LIVE if self.live else STATUS_TEMPLATED

    def line(self) -> str:
        """The one line an operator reads. Stable shape, greppable, key-free."""
        head = (
            f"llm[{self.role}] {self.status}: "
            f"provider={self.provider} "
            f"model={self.model} "
            f"sdk={'installed' if self.sdk_installed else 'MISSING'} "
            f"api_key={'present' if self.api_key_present else 'absent'}"
        )
        if self.live:
            return f"{head}; a real model writes this role's prose."
        return f"{head}; {self.reason}"


def describe_llm_runtime(role: str, env: Mapping[str, str] | None = None) -> LLMRuntime:
    """Describe, without building anything, what ``role`` will actually speak with.

    Args:
        role: one of :data:`llm.config.KNOWN_ROLES`.
        env: environment to read; defaults to ``os.environ``.

    Returns:
        An :class:`LLMRuntime`. ``reason`` is ``None`` only when all three of the conditions
        a live call needs are met: the provider is ``anthropic``, the SDK is installed, and a
        key is set. Each failure has its own sentence, because "no model wrote this" has
        three completely different repairs and an operator should not have to guess which.

    Raises:
        llm.errors.UnknownRoleError: for a role that is not configured. This one is NOT
            softened into a reason string: an unknown role is a caller bug, not a deployment
            state, and reporting it as "templated" would hide a typo behind a plausible line.
    """
    model = resolve_model(role, env)
    installed = sdk_installed()
    key_present = resolve_api_key(env) is not None

    try:
        provider = resolve_provider(env)
    except ProviderNotConfiguredError as unimplemented:
        # `resolve_provider` refuses a typo rather than falling back, which is right — but a
        # boot line that said nothing here would be the one case where the operator's own
        # mistake is invisible. Report the rejected spelling and why.
        return LLMRuntime(
            role=role,
            provider=_stated_provider(env),
            model=model,
            sdk_installed=installed,
            api_key_present=key_present,
            reason=(
                f"{unimplemented}; nothing selects a model, so the deterministic fallback is "
                f"what a shopper reads"
            ),
        )

    if provider != PROVIDER_ANTHROPIC:
        reason = (
            f"{PROVIDER_ENV_VAR} is {provider!r} (D20's default), so the offline double "
            f"answers and the deterministic slot-filled fallback is what a shopper reads. "
            f"Set {PROVIDER_ENV_VAR}={PROVIDER_ANTHROPIC} and {API_KEY_ENV_VAR} for the "
            f"model's own voice"
        )
    elif not installed:
        reason = (
            f"{PROVIDER_ENV_VAR}={PROVIDER_ANTHROPIC} is selected but the {SDK_DISTRIBUTION!r} "
            f"distribution is NOT installed in this image, so the first completion raises "
            f"ModuleNotFoundError and every pitch falls back to the deterministic template. "
            f"Add {SDK_DISTRIBUTION} to this image's pip layer"
        )
    elif not key_present:
        reason = (
            f"{PROVIDER_ENV_VAR}={PROVIDER_ANTHROPIC} is selected but {API_KEY_ENV_VAR} is "
            f"unset, so the first completion raises MissingApiKeyError and every pitch falls "
            f"back to the deterministic template"
        )
    else:
        reason = None

    return LLMRuntime(
        role=role,
        provider=provider,
        model=model,
        sdk_installed=installed,
        api_key_present=key_present,
        reason=reason,
    )


def log_llm_runtime(
    role: str,
    *,
    env: Mapping[str, str] | None = None,
    logger: logging.Logger | None = None,
    once: bool = True,
) -> LLMRuntime:
    """Emit :meth:`LLMRuntime.line` for ``role`` and return the state it described.

    Level carries the meaning, so a deployment that only keeps WARNING still sees the state
    that costs it the product: **INFO when a model writes, WARNING when a template does.**
    ``proxyshop_support.logging_config.configure_logging`` — which every service's
    ``create_app()`` calls before it mounts a router — puts the root logger at INFO, so both
    levels reach the container log.

    Args:
        role: one of :data:`llm.config.KNOWN_ROLES`.
        env: environment to read; defaults to ``os.environ``.
        logger: where to emit. Defaults to this module's logger, which an operator can raise
            or silence by the name ``llm.boot`` without touching the rest of the package.
        once: suppress a line this process has already emitted **verbatim**. On by default
            because the honest call sites are per-request seams (a client is built per render
            so its transcript can be collected), and one WARNING per shortlist for the life
            of a container is how a true line becomes noise nobody reads. A state CHANGE
            still speaks: the guard is keyed on the rendered line, not on the role.
    """
    state = describe_llm_runtime(role, env)
    line = state.line()
    if once:
        if line in _reported:
            return state
        _reported.add(line)
    (logger or _log).log(logging.INFO if state.live else logging.WARNING, "%s", line)
    return state


def forget_llm_runtime_reports() -> None:
    """Clear the ``once=True`` suppression set. For tests, and for a deliberate re-report."""
    _reported.clear()


def _stated_provider(env: Mapping[str, str] | None) -> str:
    """The raw ``LLM_PROVIDER`` spelling, for reporting a value ``resolve_provider`` refused.

    Read the same way :mod:`llm.config` reads it — stripped and lower-cased — so the line
    names the value that was actually compared, not a differently-cased copy of it. Falls
    back to :data:`llm.config.PROVIDER_DOUBLE` for the blank case that never reaches here.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    return (environ.get(PROVIDER_ENV_VAR) or "").strip().lower() or PROVIDER_DOUBLE
