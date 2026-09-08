"""The one seam between the buyer-side agent and a model (D20, C4, rule 2).

    LLM_PROVIDER unset  ->  llm.doubles.DeterministicLLM  ->  offline, no key, no socket
    LLM_PROVIDER=anthropic  ->  llm.client.AnthropicLLM, with THIS module's timeout

Why the seam is module-level and not ``app.state``
--------------------------------------------------
``POST /buyer/shortlist/render`` takes no ``Request``, deliberately: ``buyer_svc.accept.routes``
documents that the handler *cannot reach* an exchange client — "not 'does not call one'" — and
that is R2's property, because looking at a shortlist must not be able to become accepting one.
A handler holding a ``Request`` holds ``app.state`` and therefore holds the exchange client, so
wiring the writer there would have traded R2's structural guarantee for a convenience. This is
the shape ``buyer_svc.composition.set_buyer_llm`` / ``buyer_llm`` already uses for the
clarifier, for the same kind of reason.

The timeout, stated
-------------------
:data:`PITCH_TIMEOUT_SECONDS` is 4 seconds, per model call, and it is the wall-clock bound on
the render path. It is not ``llm.config.DEFAULT_TIMEOUT_SECONDS`` (60), which is a sane bound
for a background job and an absurd one for a shopper waiting on a shortlist: four slots at 60
seconds is a five-minute page. On breach the SDK raises, :func:`buyer_svc.pitch.writing
.compose_case` catches it, and the shopper gets the deterministic assembled case for that slot
with every other slot untouched.

There is deliberately **no watchdog thread and no wall-clock budget** across slots. A deadline
evaluated on the render path would make the served bytes depend on machine load, and two
identical requests must render byte-identically; the number of model calls one render may make
is bounded by COUNT instead (:data:`buyer_svc.pitch.writing.MAX_WRITTEN_SLOTS`).

Failure is silent by design
---------------------------
:func:`pitch_writer` returns ``None`` rather than raising when ``packages/llm`` is not
importable, when ``LLM_PROVIDER`` names something unimplemented, or when building the client
fails for any other reason. A buyer service that cannot build a writer still serves every slot
with the deterministic case; refusing the shortlist because the copywriter is unavailable is
exactly rule 2's failure.

What is kept between requests, and what is not
----------------------------------------------
The LIVE client is memoised; the OFFLINE double is rebuilt per render. That asymmetry is not
an oversight — the double is a *recording* double whose ``calls`` list grows by one full
prompt transcript per model call and is never trimmed, and D20 makes it the default provider
on a served route. See :func:`pitch_writer`, and ``tests/test_pitch_writer_seam.py``, which
fails if the double is memoised again.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

__all__ = [
    "LLM_ROLE",
    "PITCH_TIMEOUT_SECONDS",
    "build_pitch_writer",
    "pitch_writer",
    "set_pitch_writer",
]

_log = logging.getLogger(__name__)

#: ``llm.config.ROLE_BUYER``. Spelled rather than imported so this module is importable with
#: no ``packages/llm`` present; :func:`build_pitch_writer` validates it against the real
#: roster by passing it to ``build_llm``, which raises ``UnknownRoleError`` on every provider
#: path — including the offline double.
LLM_ROLE = "buyer"

#: Per-call wall-clock bound on the render path, in seconds. See the module docstring.
PITCH_TIMEOUT_SECONDS = 4.0

#: A caller-installed writer, or ``None`` for "use the configured default".
_override: Any = None

#: The memoised LIVE client. See :func:`pitch_writer` for why only the live one is kept.
_live: Any = None
_live_built = False

#: Whether the "could not build a writer" warning has already been emitted. A misconfigured
#: provider must not write one WARNING line per served shortlist for the life of the process.
_warned = False


def set_pitch_writer(client: Any) -> None:
    """Install the client the buyer-side agent writes with. ``None`` restores the default.

    The whole seam for tests and for a deployment that wants to inject its own client. It
    does **not** clear the memoised live client, so installing and removing an override
    leaves the configured one exactly as it was.
    """
    global _override
    _override = client


def _warn_once(message: str, *args: Any) -> None:
    global _warned
    if not _warned:
        _warned = True
        _log.warning(message, *args)


def build_pitch_writer(env: Mapping[str, str] | None = None) -> Any:
    """Build the configured client afresh, or ``None`` if one cannot be built.

    ``timeout=`` is passed only on the ``anthropic`` path, because ``llm.client.build_llm``
    refuses a keyword the selected provider would silently discard — the offline double has
    no socket to time out on, and passing it there is a ``TypeError`` rather than a no-op.

    **It also says which voice this process has.** ``llm.boot.log_llm_runtime`` emits one
    line — INFO when a real model writes the platform's case, WARNING when the deterministic
    assembly does — naming the provider, the model id, whether the ``anthropic`` distribution
    is installed in this image and whether a key is set (a bool; never the key). Without it a
    buyer container serving nothing but ``assemble()`` is indistinguishable from one serving
    written prose: both answer 200 and both report healthy. Suppressed after the first
    identical line, because D20's default double is rebuilt per render (see
    :func:`pitch_writer`) and this function therefore runs on the served path.
    """
    try:
        from llm import PROVIDER_ANTHROPIC, build_llm, log_llm_runtime, resolve_provider
    except Exception:  # noqa: BLE001 - a service with no `packages/llm` still serves slots
        _warn_once("packages/llm is not importable; shortlist cases will be assembled")
        return None
    try:
        log_llm_runtime(LLM_ROLE, env=env)
    except Exception:  # noqa: BLE001 - rule 2: a logging line never costs the shopper a slot
        _log.warning("could not report the %r LLM runtime state", LLM_ROLE, exc_info=True)
    try:
        if resolve_provider(env) == PROVIDER_ANTHROPIC:
            return build_llm(LLM_ROLE, timeout=PITCH_TIMEOUT_SECONDS, env=env)
        return build_llm(LLM_ROLE, env=env)
    except Exception:  # noqa: BLE001 - rule 2: never cost the shopper the shortlist
        _warn_once("could not build the %r LLM client; shortlist cases will be assembled", LLM_ROLE)
        return None


def _is_live(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``LLM_PROVIDER`` selects the live client. ``False`` for anything unreadable."""
    try:
        from llm import PROVIDER_ANTHROPIC, resolve_provider

        return bool(resolve_provider(env) == PROVIDER_ANTHROPIC)
    except Exception:  # noqa: BLE001 - an unreadable provider is not the live one
        return False


def pitch_writer(env: Mapping[str, str] | None = None) -> Any:
    """The client to write this render's cases with, or ``None``.

    An installed override always wins. Otherwise:

    **The LIVE client is built once and reused.** ``llm.client.AnthropicLLM`` builds its SDK
    client on first use and caches it, so a fresh one per render would mean a fresh
    connection pool and a fresh TLS handshake per shortlist. It accumulates nothing.

    **The OFFLINE double is built fresh every render, and that is not a symmetry oversight.**
    ``llm.doubles.DeterministicLLM`` is a *recording* double: ``complete`` appends a
    ``KeyedLLMCall`` — role, prompt, system blocks, kwargs — to ``self.calls`` on every call,
    and nothing ever trims it. D20 makes that double the DEFAULT provider, and this seam is on
    a served route that calls it once per slot, so a memoised double would grow one full
    prompt transcript per slot of every shortlist ever rendered, for the life of the process.
    Measured on the first draft of this module, which memoised both. Constructing a double is
    three empty containers, so building one per render costs nothing and the transcript is
    collected with it. ``buyer_svc.composition.buyer_llm`` has always built per call for the
    clarifier; this is the same shape, with the reason written down.

    A build that fails returns ``None`` and the shortlist is served with assembled cases
    (rule 2). The warning for that is emitted once per process, not once per request.
    """
    global _live, _live_built
    if _override is not None:
        return _override
    if _live_built:
        return _live
    client = build_pitch_writer(env)
    if client is not None and _is_live(env):
        _live, _live_built = client, True
    return client
