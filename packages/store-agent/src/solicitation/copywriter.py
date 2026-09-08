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

The budget, where it comes from, and what happens on breach
-----------------------------------------------------------
The copywriter gets a **per-request budget**, not a fixed timeout, and the budget is the
exchange's own deadline. Every `BidRequest` carries `respond_by` — the instant the exchange
abandons this solicitation, written as ``opened_at + window`` by the auction route — and until
:func:`pitch_budget_seconds` existed nothing on this side read it as a *time budget* at all:
:mod:`store_agent.runtime.context` uses it only as a fallback offer expiry. The copywriter was
bounded instead by a fixed five seconds nobody had reconciled with the auction window.

TWO MEASUREMENTS, TWO POPULATIONS — and they are quoted separately on purpose
-----------------------------------------------------------------------------
Both were taken against the live stack with a real key and ``LLM_PROVIDER=anthropic``. They
answer different questions and neither substitutes for the other; reading one range as if it
bounded the other is what made them look contradictory:

**In process, one store, n=5.** The `gaiaherbs` store context driven directly through
``AgentRunner.run`` with a live Anthropic client: the pitch took **3.35–5.04 s**. The paired
offer-only run on the SAME population — same context, same driver, ``llm=None`` — took **12.7 ms
median (n=20)**. That pair, and only that pair, is where **99.67%** comes from:
``(3.79 − 0.0127) / 3.79``. The figure belongs to these two numbers because they are the two
halves of one measured request; it is not quoted against the over-the-wire range below, which
has no paired offer-only measurement of its own.

**Over the wire, four stores, n=24.** ``POST /v1/bid-requests`` against all FOUR hosted
containers, end to end including HTTP: **1.97–4.73 s**. Per agent, `gaiaherbs` 3.12–4.73,
`toniiq` 2.05–2.76, `paradiseherbs` 2.16–2.51, `oregonswildharvest` 1.97–2.39. The floor is
lower than the in-process floor above and that is not a contradiction: the in-process range is
`gaiaherbs` alone, which is the slow store, and the other three drag the wider population's
range down underneath it.

Against a 3-second exchange window, every hosted store missed the deadline on both
measurements, and a market of four bidding agents silently became 100% list-price fallback
(R10) — the exact condition R10 exists to cover for a store that is *not answering*.

So :func:`pitch_budget_seconds` answers ``min(ceiling, respond_by - now - reserve)``, floored at
zero, and :meth:`PitchClient.arm` puts that number in front of exactly one request:

* ``ceiling`` is :data:`PITCH_TIMEOUT_SECONDS` (5s, overridable by :data:`PITCH_TIMEOUT_ENV`),
  NOT the 60-second `LLM_TIMEOUT_SECONDS` the package ships for conversational roles: a minute
  of latency in front of a synchronously solicited bid has already lost the auction.
* ``reserve`` (:data:`PITCH_RESERVE_SECONDS`, 0.35s, overridable by :data:`PITCH_RESERVE_ENV`)
  is what this agent still needs AFTER the model answers — screening the prose, assembling the
  `Bid`, serialising it and getting the bytes on the wire — plus margin for clock skew between
  the exchange's container and this one.
* A `respond_by` that is absent or unreadable yields the ceiling — **defence for a solicitor
  that is not the exchange, and not ordinary behaviour.** The published contract makes
  `respond_by` a required, non-empty string, and the exchange always sends a parseable one.
  Driven against the live agent, none of the states this branch covers is reachable through the
  served door: an omitted field is ``422 {"type":"missing"}``, an empty one ``422
  {"type":"string_too_short"}``, a numeric epoch ``422 {"type":"string_type"}``. A *garbage
  string* does get past the schema and still never reaches the copywriter, because `respond_by`
  has a second job — it is the fallback the offer's expiry falls back to — so an unreadable one
  costs the store its whole bid at the offer-expiry gate (a 204 ``unstatable_offer_expiry``)
  before any prose is asked for. `HttpBidSolicitor` sends only ``{auction_id, intent, profile,
  respond_by[, product_ref]}``, so the ceiling branch is reachable only for a caller that also
  supplies `offer_expires_at` — which is to say, for a solicitor that is not the exchange. It is
  kept because such a caller would otherwise be punished for a field the exchange owns.
* **A budget of zero means no model call at all** (:class:`PitchBudgetExhausted`). That case
  was the most expensive one: a store whose auction had already closed still spent five seconds
  writing prose nobody would read.

``max_retries=0`` is what makes the budget honest
-------------------------------------------------
The live client is built with ``max_retries=0``. `anthropic==1.2.0` ships
``DEFAULT_MAX_RETRIES = 2`` and **retries `APITimeoutError`**, so a per-call timeout bounds
nothing until the retries are pinned: measured directly against the pinned SDK with a per-call
timeout of 0.8s, ``max_retries=2`` raised `APITimeoutError` after **3.92s** and
``max_retries=0`` raised it after **0.84s**. The five-second budget this module documented was
really a fifteen-second-plus one. ``anthropic.Messages.create`` accepts a per-request
``timeout`` but **not** a per-request ``max_retries``, so it is pinned on the client, which is
the only place the SDK keeps it.

On breach the SDK raises, :func:`~store_agent.runtime.pitch.compose_pitch` catches it, and the
bid goes out with the deterministic fallback pitch — composed locally from the store's own
verified facts, in microseconds. **Nothing about the offer changes**, and the store does not
lose the auction because its copywriter was slow or down.

What is, and is not, promised about determinism
------------------------------------------------
There is still deliberately **no watchdog thread**: nothing here interrupts the bid path, and
the bound remains the provider's own, where the latency actually is. What this module used to
claim beside that — "no wall-clock deadline on the bid path", and therefore that the served
bytes do not depend on machine load — was already false before this change and is stated
honestly now. The pre-existing five-second SDK timeout ALREADY chose between the model's prose
and the deterministic fallback on latency; what changed is only that the number now comes from
the exchange's stated `respond_by` instead of from a constant nobody had reconciled with the
auction window. So, plainly: **which of the two pitches a bid carries can depend on how loaded
this machine is, and always could.**

What does not depend on it is the thing S4 is actually protecting — the offer. `unit_price`,
`discount`, `total_price`, `commitments` and `expires_at` are computed with no model call at
all (12.7 ms median, n=20 in process — see the measurement section above), the same in every
process on every run; and both candidate pitches go through the same
:func:`~store_agent.runtime.pitch.screen`, so a slow copywriter changes which
true sentences a shopper reads and never what the store charges. On the default offline path
there is no I/O to bound and no budget is spent.

Building the client can itself fail (a typo'd fixture stem, an unimplemented `LLM_PROVIDER`).
That is reported as `None` — no copywriter — and never as an exception, for the same reason
every other failure here is: a misconfigured copywriter must cost the store its prose, never its
bid.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from contracts.boundary import parse_timestamp
from llm.boot import log_llm_runtime
from llm.client import build_llm
from llm.config import (
    PROVIDER_ANTHROPIC,
    PROVIDER_DOUBLE,
    ROLE_STORE_AGENT,
    resolve_provider,
)
from llm.doubles import RecordedLLM

__all__ = [
    "PITCH_MAX_RETRIES",
    "PITCH_OUTCOMES",
    "PITCH_RECORDINGS_ENV",
    "PITCH_RESERVE_ENV",
    "PITCH_RESERVE_SECONDS",
    "PITCH_TIMEOUT_ENV",
    "PITCH_TIMEOUT_SECONDS",
    "PitchAttempt",
    "PitchBudgetExhausted",
    "PitchClient",
    "is_deadline_miss",
    "pitch_budget_seconds",
    "pitch_client",
    "resolve_pitch_reserve",
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

#: Seconds subtracted from the exchange's `respond_by` before the remainder is handed to the
#: copywriter — the work this agent still has to do AFTER the model answers: screen the prose,
#: assemble the `Bid`, serialise it, get the bytes on the wire; plus margin for clock skew
#: between the exchange's container and this one. The offer itself costs 12.7 ms (median, n=20
#: in process), so this is dominated by serialisation and skew rather than by computation.
PITCH_RESERVE_SECONDS = 0.35

#: Overrides :data:`PITCH_RESERVE_SECONDS`. Its own variable rather than a fraction of the
#: timeout: the two answer different questions ("how long may prose take" vs "how long does the
#: rest of the response take"), and a deployment that moves one rarely wants the other moved.
PITCH_RESERVE_ENV = "STORE_AGENT_PITCH_RESERVE_SECONDS"

#: Pinned on the LIVE client only. `anthropic==1.2.0` defaults to 2 and retries
#: `APITimeoutError`, which turns a 0.8s per-call timeout into a measured 3.92s wall clock; with
#: this at 0 the same call raised at 0.84s. A budget that a retry can multiply is not a budget.
#: See the module docstring. Nothing else in `packages/llm` changes: the argument is additive
#: and every other role still gets the SDK's own default.
PITCH_MAX_RETRIES = 0


def is_deadline_miss(error: BaseException) -> bool:
    """True when ``error`` says the BUDGET ran out, false when it says the copywriter is broken.

    The distinction is the whole of :attr:`PitchAttempt.missed`. Both outcomes are WARNINGs and
    both cost the merchant its prose, but they point an operator at opposite things: a miss at
    the auction window and the model, a fault at a credential, a network or a provider.

    **The SDK class is looked up in ``sys.modules``, never imported.** ``packages/llm`` keeps
    ``import anthropic`` inside :meth:`llm.client.AnthropicLLM._ensure_client` on purpose (D20)
    so an offline image needs no SDK at all, and an import here would undo that for every
    offline run and every test. The lookup is also sufficient rather than merely cheap: an
    `anthropic.APITimeoutError` instance cannot exist unless ``anthropic`` is already imported,
    so a lookup that finds nothing is proof this error did not come from the SDK. A stubbed
    ``anthropic`` without the attribute (several tests install one) answers `False` and is
    treated as a fault, which is the safe direction — a fault is never silently called a miss.

    The builtin `TimeoutError` counts because it is what "timed out" means in Python generally:
    `socket.timeout` and `asyncio.TimeoutError` are both aliases of it, and it is what the
    offline stand-in copywriters raise when they honour the per-request timeout they were given.
    """
    if isinstance(error, TimeoutError):
        return True
    sdk = sys.modules.get("anthropic")
    timeout_error = getattr(sdk, "APITimeoutError", None)
    return isinstance(timeout_error, type) and isinstance(error, timeout_error)


class PitchBudgetExhausted(Exception):
    """Raised INSTEAD of calling the model when this request has no time left.

    Not an error condition of the model and not a failure of the store: it is the copywriter
    declining to start work whose result would arrive after the exchange had already abandoned
    the solicitation. :func:`store_agent.runtime.pitch.compose_pitch` catches it exactly as it
    catches a provider timeout and answers with the deterministic fallback pitch, so the bid is
    unaffected — it simply ships microseconds sooner instead of five seconds later.
    """


@dataclass(frozen=True, slots=True)
class PitchAttempt:
    """What the copywriter did during one armed request, for the operator log.

    Returned by :meth:`PitchClient.disarm` so the route can report it and then forget it. It is
    a *record*, never an input to anything: nothing on the bid path reads it, and a bid is
    byte-identical whether or not anyone looks at this.
    """

    #: Seconds the copywriter was given, or ``None`` if the client was never armed.
    budget: float | None = None
    #: Wall seconds the model call actually took, or ``None`` if it was never made.
    elapsed: float | None = None
    #: One of :data:`PITCH_OUTCOMES`.
    outcome: str = "not_attempted"

    @property
    def missed(self) -> bool:
        """True when the served bid carries the fallback because of TIME, and only then.

        This used to say the same sentence while counting ``failed`` — which is set for ANY
        exception out of the model call — so a permanently wrong API key, a reset connection and
        a provider 500 all read as latency. That is the misdiagnosis class this whole budget
        exists to end, reproduced inside its own instrument, so the two are now separate
        outcomes and this property counts only the temporal ones: the budget was refused before
        the call (``skipped``) or spent during it (``timed_out``).

        `screen` can still refuse a reply that arrived inside its budget — that is a content
        decision and not a missed deadline, so it is deliberately not counted here either.
        """
        return self.outcome in ("skipped", "timed_out")

    @property
    def faulted(self) -> bool:
        """True when the copywriter broke for a reason no auction window would have fixed.

        An auth failure, a connection reset, a provider 500, a malformed reply the client
        refused. The distinction from :attr:`missed` is operational and it is the point: a miss
        says *tune the window or the model*, a fault says *this container's copywriter is
        broken and every bid it serves will carry template prose until someone fixes it*.
        """
        return self.outcome == "failed"

    @property
    def degraded(self) -> bool:
        """True when the merchant is not getting the thing it joined the network to buy.

        The union of :attr:`missed` and :attr:`faulted`, and the reason the route logs at
        WARNING. Both are deployment facts and neither is a bid fact — the bid is fine in both
        cases, D55's sponsored half is not.
        """
        return self.missed or self.faulted


#: The values :attr:`PitchAttempt.outcome` can take.
#:
#: ``not_attempted``
#:     no model call was made during the armed window — no copywriter is configured, the pitch
#:     was replayed from the store context (rule F), or the bid had no supportable facts.
#: ``skipped``
#:     the budget was zero or negative, so :class:`PitchBudgetExhausted` was raised before any
#:     I/O. The auction had already closed, or was about to. A TIME fact.
#: ``timed_out``
#:     the model was called and gave up on the clock — an `anthropic.APITimeoutError` at the
#:     budget, or a builtin `TimeoutError` from an offline stand-in honouring the same bound.
#:     Also a TIME fact, and the one a wider auction window or a faster model would fix.
#: ``failed``
#:     the model was called and raised something that is NOT about time: an auth error, a
#:     connection reset, a provider 500, a reply the client refused. No auction window fixes
#:     this, and reporting it as a miss is how a dead API key gets diagnosed as latency.
#: ``ok``
#:     the model answered inside its budget. Whether its words reached the wire is then
#:     `screen`'s decision, not this module's.
PITCH_OUTCOMES = ("not_attempted", "skipped", "timed_out", "failed", "ok")


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


def resolve_pitch_reserve(env: Mapping[str, str] | None = None) -> float:
    """The post-model reserve in seconds. Malformed or non-positive values fall back.

    Mirrors :func:`resolve_pitch_timeout` deliberately, including the falling back: this is read
    while an advocate is being built and once per solicitation, and a typo in a reserve must
    cost neither the merchant's agent nor the auction. A reserve of zero is refused with the
    others — "the response costs nothing after the model returns" is not a true statement about
    any deployment, and a deployment that means "as little as possible" can say ``0.01``.
    """
    raw = str((os.environ if env is None else env).get(PITCH_RESERVE_ENV) or "").strip()
    if not raw:
        return PITCH_RESERVE_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        _log.warning(
            "%s=%r is not a number; using %ss", PITCH_RESERVE_ENV, raw, PITCH_RESERVE_SECONDS
        )
        return PITCH_RESERVE_SECONDS
    if not seconds > 0.0:
        _log.warning(
            "%s=%r must be positive; using %ss", PITCH_RESERVE_ENV, raw, PITCH_RESERVE_SECONDS
        )
        return PITCH_RESERVE_SECONDS
    return seconds


def pitch_budget_seconds(
    respond_by: Any,
    *,
    now: float,
    ceiling: float | None = None,
    reserve: float | None = None,
) -> float:
    """How many seconds the copywriter may take for THIS solicitation. Never raises.

    ``min(ceiling, respond_by - now - reserve)``, floored at ``0.0``.

    Args:
        respond_by: the `BidRequest` field, as it came off the wire. Parsed with
            :func:`contracts.boundary.parse_timestamp` — the SAME parser
            :mod:`store_agent.runtime.context` uses for this field's other job — so a store
            cannot read the deadline one way for its budget and another way for its offer
            expiry. Anything unreadable, including an empty string, yields ``ceiling`` — see
            the note below on who can actually reach that.
        now: epoch seconds, supplied by the caller. Not read from a clock here on purpose: this
            function is then a pure function of its arguments, testable without freezing time,
            and the one clock read on the whole path stays in the route where it is visible.
        ceiling: the most the copywriter may take however far away the deadline is. Defaults to
            :func:`resolve_pitch_timeout`, i.e. ``STORE_AGENT_PITCH_TIMEOUT_SECONDS``.
        reserve: seconds withheld for the rest of the response. Defaults to
            :func:`resolve_pitch_reserve`, i.e. ``STORE_AGENT_PITCH_RESERVE_SECONDS``.

    Returns:
        Seconds, ``>= 0.0``. **Zero is meaningful**: it says the deadline has passed (or is
        inside the reserve) and the model must not be called at all — see
        :class:`PitchBudgetExhausted`.

    The ceiling branch is defence, not behaviour. The published `BidRequest` contract makes
    `respond_by` a required, non-empty string and the exchange always sends a parseable one, so
    "no deadline was stated" is not a state the exchange can put this store in: driven against
    the live agent, an omitted field is ``422 {"type":"missing"}``, an empty one ``422
    {"type":"string_too_short"}`` and a numeric epoch ``422 {"type":"string_type"}``. A garbage
    *string* clears the schema and still never gets here — `respond_by` is also the offer's
    expiry fallback, so an unreadable one declines the whole bid ``unstatable_offer_expiry``
    upstream of the copywriter. What remains is a solicitor that is NOT the exchange and that
    states `offer_expires_at` itself; the branch exists so that caller keeps its configured
    budget instead of being punished for a field it was never asked for.
    """
    limit = resolve_pitch_timeout() if ceiling is None else float(ceiling)
    margin = resolve_pitch_reserve() if reserve is None else float(reserve)
    try:
        deadline = parse_timestamp(respond_by)
        remaining = None if deadline is None else deadline.timestamp() - float(now) - margin
    except Exception:  # noqa: BLE001 - an unreadable deadline costs prose, never a bid
        _log.warning("could not read respond_by as a deadline; using the %ss ceiling", limit)
        return limit
    if remaining is None:
        return limit
    return max(0.0, min(limit, remaining))


class PitchClient:
    """A client that forgets the calls it recorded, and that holds one request's time budget.

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

    THE BUDGET IS THREAD-LOCAL, AND IT HAS TO BE
    ---------------------------------------------
    ``store_agent.solicitation.routes.answer_bid_request`` is a plain ``def``, so FastAPI runs
    it on the threadpool and two auctions can be in flight at once against this ONE
    process-cached client. A budget stored on ``self`` would be whichever auction armed last —
    a store answering a 4-second auction would hand its budget to a 0.2-second one, or take
    it. So the armed budget lives in a :class:`threading.local`, exactly as
    :class:`~store_agent.solicitation.advocate.ResponseChannel`'s pending answer does and for
    the same reason, and every request arms and disarms its own slot.

    **Unarmed, this class behaves exactly as it did before the budget existed**: no ``timeout``
    is injected, nothing is refused, and the only bound is whatever the client was constructed
    with. That is the path every offline test and every in-process caller takes.
    """

    __slots__ = ("_armed", "inner")

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._armed = threading.local()

    @property
    def model(self) -> str:
        """The underlying client's model label, for logs. ``"unknown"`` if it states none."""
        return str(getattr(self.inner, "model", "") or "unknown")

    @property
    def budget(self) -> float | None:
        """Seconds this thread's request may spend on the model, or ``None`` when unarmed."""
        armed: float | None = getattr(self._armed, "budget", None)
        return armed

    def arm(self, budget: float) -> None:
        """Give THIS thread's next completion ``budget`` seconds. Clears the previous record.

        Clearing matters for the same reason `ResponseChannel.take` clears: a threadpool thread
        outlives the request that used it, and a stale measurement left in the slot would be
        reported against the next auction as if it were its own.
        """
        self._armed.budget = float(budget)
        self._armed.elapsed = None
        self._armed.outcome = "not_attempted"

    def disarm(self) -> PitchAttempt:
        """Take this thread's budget away and report what happened under it.

        Always safe to call, including when :meth:`arm` was never called (the attempt is then
        all-``None``) and including from a ``finally`` after the bid path raised.
        """
        attempt = PitchAttempt(
            budget=getattr(self._armed, "budget", None),
            elapsed=getattr(self._armed, "elapsed", None),
            outcome=str(getattr(self._armed, "outcome", "not_attempted")),
        )
        self._armed.budget = None
        self._armed.elapsed = None
        self._armed.outcome = "not_attempted"
        return attempt

    def complete(self, prompt: Any, **kwargs: Any) -> str:
        """Complete inside this thread's budget, then forget. A raise is re-raised, and SAID.

        ``store_agent.runtime.pitch.compose_pitch`` catches everything this can throw and
        answers with the deterministic fallback, which is right for the bid and was silent
        for the operator: a container whose live client raised on every solicitation shipped
        template prose for the life of the image with nothing in the log to point at. The
        exception's TYPE is logged and its message is not, because a provider's error text is
        the string in this path most likely to carry a credential on an auth failure.

        **A raise is also CLASSIFIED**, by :func:`is_deadline_miss`, into ``timed_out`` (the
        budget ran out) and ``failed`` (the copywriter is broken). Both are WARNINGs — the
        merchant is not getting what it pays for either way — and they name different repairs.
        Collapsing them, which this method used to do, made a dead API key indistinguishable
        from a slow model in the one log line an operator has.

        Three things the budget adds, and nothing else changes:

        1. **No budget left, no call.** A non-positive armed budget raises
           :class:`PitchBudgetExhausted` before any I/O. The exchange has already abandoned
           this solicitation (or will before a reply could land), and the five seconds a live
           client would otherwise spend are five seconds of a served process's threadpool.
        2. **The budget is passed down as ``timeout``**, and only when the caller did not name
           one itself — an explicit ``timeout=`` from a caller is that caller's decision.
           ``timeout`` is not in `llm.client.RESERVED_REQUEST_FIELDS`, so `AnthropicLLM`
           forwards it to ``messages.create`` as a per-request timeout, and both offline
           doubles accept it and ignore it.
        3. **Wall time is measured** around the call, so the route can log what the copywriter
           actually cost against what it was given. Nothing on the bid path reads it.
        """
        budget = self.budget
        if budget is not None and not budget > 0.0:
            self._record(0.0, "skipped")
            _log.warning(
                "the pitch copywriter (%s) was NOT CALLED: %.3fs of budget left before the "
                "exchange's respond_by; this bid carries the deterministic fallback pitch",
                self.model,
                budget,
            )
            raise PitchBudgetExhausted(
                "no time left before respond_by; the pitch was not attempted"
            )
        if budget is not None and "timeout" not in kwargs:
            kwargs["timeout"] = budget

        started = time.monotonic()
        try:
            reply = str(self.inner.complete(prompt, **kwargs))
        except Exception as error:
            elapsed = time.monotonic() - started
            spent = "unbounded" if budget is None else f"{budget:.3f}s"
            if is_deadline_miss(error):
                self._record(elapsed, "timed_out")
                _log.warning(
                    "the pitch copywriter (%s) RAN OUT OF TIME: %s after %.3fs of a %s budget; "
                    "this bid carries the deterministic fallback pitch",
                    self.model,
                    type(error).__name__,
                    elapsed,
                    spent,
                )
            else:
                self._record(elapsed, "failed")
                _log.warning(
                    "the pitch copywriter (%s) IS BROKEN, and this is NOT a missed deadline: "
                    "%s after %.3fs of a %s budget. A credential, a network or the provider — "
                    "no auction window fixes it, and every bid this container serves carries "
                    "the deterministic fallback pitch until someone does",
                    self.model,
                    type(error).__name__,
                    elapsed,
                    spent,
                )
            raise
        else:
            self._record(time.monotonic() - started, "ok")
            return reply
        finally:
            forget = getattr(self.inner, "reset", None)
            if callable(forget):
                forget()

    def _record(self, elapsed: float, outcome: str) -> None:
        """Remember what this thread's call cost — but ONLY when a budget was armed.

        An unarmed client leaves no trace at all, which is what makes "unarmed behaves exactly
        as it did before budgets existed" a property rather than a hope: every in-process
        caller and every offline suite takes that path.
        """
        if getattr(self._armed, "budget", None) is None:
            return
        self._armed.elapsed = elapsed
        self._armed.outcome = outcome

    def __repr__(self) -> str:
        return f"<PitchClient {self.inner!r}>"


def pitch_client(env: Mapping[str, str] | None = None) -> PitchClient | None:
    """The copywriter this deployment writes pitches with, or `None` when it has none.

    `None` is not a failure state — it is "no model configured", and
    :func:`store_agent.runtime.pitch.compose_pitch` answers it with the deterministic fallback
    pitch. Every way of failing to build a client resolves to it, loudly in the log and silently
    on the bid.

    **This is also where the advocate says which voice it has.** ``llm.boot.log_llm_runtime``
    emits one line — INFO when a real model writes the store's own case, WARNING when the
    deterministic fallback does — naming the provider, the model id, whether the ``anthropic``
    distribution is installed in this image and whether a key is set (a bool; never the key).
    It belongs here rather than in ``store_agent.main`` because this function IS the decision:
    `solicitation.advocate` calls it once per application and caches the result on
    ``app.state``, so the line is emitted while the advocate is being built and is not
    repeated per solicitation. D55 makes this pitch the thing an in-network shop is buying,
    and until this line existed a container that had silently lost it looked identical to one
    that had not.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    try:
        log_llm_runtime(ROLE_STORE_AGENT, env=environ)
    except Exception:  # noqa: BLE001 - a logging line never costs the store its copywriter
        _log.warning("could not report the store-agent LLM runtime state", exc_info=True)
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
                # The ceiling, and the thing that makes the ceiling true. `build_llm` forwards
                # `**kwargs` to `AnthropicLLM` and raises TypeError if they reach the double
                # path, so both of these are on the anthropic branch only — the offline default
                # is untouched and needs neither. See the module docstring for the 3.92s/0.84s
                # measurement that says why a timeout without this is not a bound.
                max_retries=PITCH_MAX_RETRIES,
            )
        )
    except Exception:  # noqa: BLE001 - a misconfigured copywriter costs prose, never a bid
        _log.warning("no pitch copywriter could be built; bids will carry the fallback pitch")
        return None
