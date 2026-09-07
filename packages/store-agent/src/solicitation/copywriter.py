"""Where the served advocate gets the copywriter that writes its pitch (D55, C4, D20).

:mod:`store_agent.runtime.pitch` composes the pitch and :mod:`store_agent.runtime.bidding` puts
it on `Bid.message`, but neither of them may decide **which model writes it**, because neither
may read the environment: ``test_the_runtime_reads_no_clock_and_no_randomness`` AST-scans every
file under ``runtime/`` and refuses an ``import os``, and it is right to — an environment read on
the bid path is an ambient input, and a bid that depends on one is not reproducible from its
arguments (S4).

So provider selection lives here, in the composition root, next to the two other deployment
facts this package already resolves from the environment (``STORE_AGENT_CONTEXT`` and
``STORE_AGENT_STORE_DOMAIN`` in :mod:`.serving`). :func:`pitch_client` is called once when the
advocate is built and the result is handed to the `AgentRunner`, which passes it to every
``bid()`` it runs.

Three configurations, and the default is the offline one (D20)
--------------------------------------------------------------

``LLM_PROVIDER`` unset (the default, and what every test and every offline verify run gets)
    :class:`llm.doubles.DeterministicLLM`. Its reply is the marker ``double:store_agent:<16
    hex>``, which is not prose, which :func:`store_agent.runtime.pitch.screen` refuses, and which
    therefore lands on the deterministic non-model fallback. **A run with no API key produces a
    serviceable pitch rather than an error or an empty bid** — that is the point, and the double
    is called rather than skipped so the offline path exercises the same code the live one does.

``STORE_AGENT_PITCH_RECORDINGS=<fixture stem>``
    :class:`llm.doubles.RecordedLLM` over ``packages/llm/fixtures/recorded/<stem>.json`` (D21) —
    reviewed, hand-authored replies, replayed byte for byte with no network. This is how a real
    model's pitch is driven through the served door offline, and how a recorded pitch stays
    honest: `RecordedLLM` keys on the ``(system, prompt)`` **pair**, so a change to
    :data:`~store_agent.runtime.pitch.PITCH_CONTRACT` makes every recording miss, loudly, instead
    of replaying an answer reviewed against a contract that no longer exists. A miss raises,
    `compose_pitch` catches it, and the store bids with the fallback.

``LLM_PROVIDER=anthropic``
    :class:`llm.client.AnthropicLLM` for the ``store_agent`` role, whose model id comes from
    ``STORE_AGENT_MODEL`` (C4 — model ids are config, not code).

The timeout, and what happens on breach
---------------------------------------
The live client is built with a **5-second** timeout (:data:`PITCH_TIMEOUT_SECONDS`, overridable
by :data:`PITCH_TIMEOUT_ENV`), NOT the 60-second `LLM_TIMEOUT_SECONDS` default the package ships
for conversational roles. Bids are solicited synchronously against a hard `respond_by` (R10), so
a copywriter that takes a minute has already lost the auction for the store; five seconds is a
budget a pitch can be written in and a bid can absorb. On breach the SDK raises,
:func:`~store_agent.runtime.pitch.compose_pitch` catches it, and the bid goes out with the
deterministic fallback pitch. **Nothing about the offer changes**, and the store does not lose
the auction because its copywriter was down.

There is deliberately no watchdog thread and no wall-clock deadline on the bid path itself: a
deadline evaluated against a clock would make the served bytes depend on machine load, which is
the one thing S4 forbids. The bound is the provider's own, where the latency actually is — and
on the default offline path there is no I/O to bound at all.

Building the client can itself fail (a typo'd fixture stem, an unimplemented `LLM_PROVIDER`).
That is reported as `None` — no copywriter — and never as an exception, for the same reason
every other failure here is: a misconfigured copywriter must cost the store its prose, never its
bid.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from llm.client import build_llm
from llm.config import (
    PROVIDER_ANTHROPIC,
    PROVIDER_DOUBLE,
    ROLE_STORE_AGENT,
    resolve_provider,
)
from llm.doubles import RecordedLLM

__all__ = [
    "PITCH_RECORDINGS_ENV",
    "PITCH_TIMEOUT_ENV",
    "PITCH_TIMEOUT_SECONDS",
    "PitchClient",
    "pitch_client",
    "resolve_pitch_timeout",
]

_log = logging.getLogger(__name__)

#: A recorded-fixture stem under ``packages/llm/fixtures/recorded``. Double provider only.
PITCH_RECORDINGS_ENV = "STORE_AGENT_PITCH_RECORDINGS"

#: Seconds a pitch generation may take before the provider gives up. See the module docstring.
PITCH_TIMEOUT_SECONDS = 5.0

#: Overrides :data:`PITCH_TIMEOUT_SECONDS`. Deliberately its own variable rather than
#: ``LLM_TIMEOUT_SECONDS``: the shared one defaults to 60 seconds for conversational roles, and
#: inheriting that here would put a minute of latency in front of a synchronously solicited bid.
PITCH_TIMEOUT_ENV = "STORE_AGENT_PITCH_TIMEOUT_SECONDS"


def resolve_pitch_timeout(env: Mapping[str, str] | None = None) -> float:
    """The pitch timeout in seconds. Malformed or non-positive values fall back to the default.

    Falls back rather than raising because this is read while a store's advocate is being built,
    and a typo in a timeout must not be the reason a merchant's agent refuses to exist.
    """
    raw = str((os.environ if env is None else env).get(PITCH_TIMEOUT_ENV) or "").strip()
    if not raw:
        return PITCH_TIMEOUT_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        _log.warning(
            "%s=%r is not a number; using %ss", PITCH_TIMEOUT_ENV, raw, PITCH_TIMEOUT_SECONDS
        )
        return PITCH_TIMEOUT_SECONDS
    if not seconds > 0.0:
        _log.warning(
            "%s=%r must be positive; using %ss", PITCH_TIMEOUT_ENV, raw, PITCH_TIMEOUT_SECONDS
        )
        return PITCH_TIMEOUT_SECONDS
    return seconds


class PitchClient:
    """A client that forgets the calls it recorded, so a long-lived advocate does not grow.

    The offline doubles append a `KeyedLLMCall` to ``.calls`` on **every** completion and never
    drop one — which is exactly right for a test and a leak on a served process that answers a
    solicitation per auction for as long as the container lives. This package already has the
    same problem solved next door (`BoundedBidLog` is a ring for the identical reason); the
    difference is that a bid log is *read* and this record is not, so the cheapest correct size
    for it is zero.

    ``reset()`` clears the recorded calls and any queued replies; it deliberately does not clear
    a `RecordedLLM`'s table or a `when` rule, so replay still works. It is called after the reply
    has been taken, in a `finally`, so a client that raised is cleaned up too. Two threadpool
    workers calling `reset` concurrently can only clear each other's already-unread records,
    which is what both of them were about to do anyway.
    """

    __slots__ = ("inner",)

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    @property
    def model(self) -> str:
        """The underlying client's model label, for logs. ``"unknown"`` if it states none."""
        return str(getattr(self.inner, "model", "") or "unknown")

    def complete(self, prompt: Any, **kwargs: Any) -> str:
        try:
            return str(self.inner.complete(prompt, **kwargs))
        finally:
            forget = getattr(self.inner, "reset", None)
            if callable(forget):
                forget()

    def __repr__(self) -> str:
        return f"<PitchClient {self.inner!r}>"


def pitch_client(env: Mapping[str, str] | None = None) -> PitchClient | None:
    """The copywriter this deployment writes pitches with, or `None` when it has none.

    `None` is not a failure state — it is "no model configured", and
    :func:`store_agent.runtime.pitch.compose_pitch` answers it with the deterministic fallback
    pitch. Every way of failing to build a client resolves to it, loudly in the log and silently
    on the bid.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    try:
        provider = resolve_provider(environ)
        if provider == PROVIDER_DOUBLE:
            fixture = str(environ.get(PITCH_RECORDINGS_ENV) or "").strip()
            if fixture:
                return PitchClient(RecordedLLM.from_fixture(fixture, role=ROLE_STORE_AGENT))
            return PitchClient(build_llm(ROLE_STORE_AGENT, provider=PROVIDER_DOUBLE, env=environ))
        return PitchClient(
            build_llm(
                ROLE_STORE_AGENT,
                provider=PROVIDER_ANTHROPIC,
                env=environ,
                timeout=resolve_pitch_timeout(environ),
            )
        )
    except Exception:  # noqa: BLE001 - a misconfigured copywriter costs prose, never a bid
        _log.warning("no pitch copywriter could be built; bids will carry the fallback pitch")
        return None
