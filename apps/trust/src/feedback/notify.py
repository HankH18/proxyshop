"""The wire from ``POST /events`` to the affected store's agent. R13's missing transport.

Both ends of R13 were built and neither could reach the other:
:func:`trust.feedback.push_trust_event` had no production caller and
``store_agent.modes.AgentRunner.ingest_trust_event`` had no production caller, because
nothing computed a delta, nothing addressed a store agent, and neither service had a door.
This module is the trust half of that wire — it decides WHERE a delta goes and guarantees
that sending it can never cost the caller its append.

THREE PROPERTIES, IN THE ORDER THEY MATTER
-------------------------------------------
**1. To the affected store, and only the affected store.** ``feedback/engine.py`` states this
in its first paragraph: a broadcast is a leak of one store's trust movement to its
competitors. So the address book is a per-store lookup with no default, no wildcard and no
fallback endpoint. A store this deployment holds no address for gets no push — which is a
push that did not happen, not a push that went somewhere else. :meth:`StoreAgentSink.send`
additionally refuses a payload whose own ``store_id`` disagrees with the address it is about
to be sent to, because "addressed correctly" and "addressed to the store the payload names"
are two claims and only the second is the privacy property.

**2. Sending can never fail the append.** ``POST /events`` is the ledger's door. An event that
landed in the hash chain HAS landed, and a store agent that is down, slow, or answering 500
must not turn that into a 5xx for the producer. :func:`announce_trust_event` therefore never
raises — not for a transport failure, not for a scoring failure, not for a bug in this file —
and the socket carries a short timeout so "slow" is bounded rather than open-ended.

This is the same posture ``proxyshop_support.trust_ledger.TrustLedgerPublisher`` takes about
the ledger write itself, and this module deliberately follows its SHAPE rather than inventing
a fifth bespoke client: never raise, count what landed, keep a bounded ring of what did not,
log the two state CHANGES of an outage and not the steady state, and publish the whole
condition through :meth:`StoreAgentSink.report` so an operator can read it after the log line
scrolls away.

It is not a subclass and not a reuse of that class: ``TrustLedgerPublisher`` POSTs a
``LedgerEvent`` to one fixed URL and keys its ring on ``event_id``. This POSTs a
``TrustEventPayload`` to a URL chosen per store and has to key its ring on the pair, so the
two share a posture and not an implementation.

**3. Failure is never silent, and configuration is not filed as failure.** ``lost``,
``undelivered`` and :meth:`StoreAgentSink.report` are readable at any moment, and the report is
SERVED — on ``GET /events/verify``, through ``trust.events.routes.notification_report``. That
last clause is the point: the ring existed, counted correctly, and was reachable by nothing an
operator could call, so "an operator cannot read what was lost" was literally true of every
deployment.

The ``ERROR`` is reserved for an OUTAGE — a peer this deployment addresses that stopped
answering — and is emitted once, on the first failure of one. A store with no address and an
outcome with no cluster are counted (``undeliverable``) and put on the ring with their reason,
and they deliberately do NOT move ``delivering``: a deployment that rosters ten storefronts and
runs four agents would otherwise flap between "stopped reaching store agents" and "reaching
them again" on alternate events, with nothing wrong and ``delivering`` reporting whichever
store the last append happened to be about.

WHAT HAPPENS WHEN A PUSH DOES NOT LAND
---------------------------------------
One inline attempt, bounded by :data:`DEFAULT_PUSH_TIMEOUT_SECONDS`, because that is the only
attempt that can hold ``POST /events`` open. A transport failure or a 5xx then goes on a
bounded retry queue drained by this sink's own daemon thread with exponential backoff
(:data:`DEFAULT_RETRY_ATTEMPTS`, :data:`DEFAULT_RETRY_BACKOFF_SECONDS`), so an agent that was
restarting gets the delta a second later instead of never. A 4xx is not retried: the receiver
is saying the message is wrong, and a wrong message is wrong on the second attempt too.

**The queue does not survive a restart, and nothing here pretends otherwise.** ``report()``
says ``durable: false`` in as many words. Making it durable means a table the trust service
writes on the ``POST /events`` path — a new ``db/migrations/*.sql``, a GRANT for ``trust_rw``,
and a claim policy so two processes cannot retry the same row — which is a database change this
module cannot make on its own, and half of it (a queue written but never re-read at boot) would
be worse than none.

CONFIGURATION, AND WHAT MUST SHIP IT
-------------------------------------
Two variables, two halves of one loop, and either can be set without the other:

:data:`ENV_STORE_AGENT_ENDPOINTS` is a JSON object mapping ``store_id`` to the base URL of
that store's agent, e.g.::

    TRUST_STORE_AGENT_ENDPOINTS={"store-alpha":"http://store-agent-alpha:8086"}

:data:`ENV_EXCHANGE_OUTCOMES_URL` is the exchange's base url, and is where a completed
purchase's trust movement becomes a bandit outcome::

    TRUST_EXCHANGE_OUTCOMES_URL=http://exchange:8083

Unset, or set to ``{}``, the trust service sends nothing to anybody — no guess, no wildcard, no
fallback endpoint, because the alternative to an address is a guess and a guessed address is
how one store's trust movement reaches a competitor. What is NOT the same as before is that the
condition is now visible: the delta is still computed, the non-delivery lands in the ring with
a reason naming the variable that would have addressed it, :func:`log_learning_loop_state`
says so once at boot, and ``GET /events/verify`` publishes it for a reader who arrives later.
A silently disabled learning loop is the defect; an empty variable is a decision.

**``apps/trust/compose.yaml`` and ``.env.example`` carry both keys**, and under compose the
store-agent book defaults to the four ``demo``-profile agents so that the market this repo
ships actually learns. There is no new dependency to ship — ``httpx`` is already in the trust
image (``proxyshop_support.trust_ledger`` imports it the same lazy way).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .deltas import (
    MAX_DELTA_HISTORY_EVENTS,
    bounded_history,
    delta_for_event,
    observation_of,
)
from .engine import push_trust_event

__all__ = [
    "DEFAULT_PUSH_TIMEOUT_SECONDS",
    "DEFAULT_RETRY_ATTEMPTS",
    "DEFAULT_RETRY_BACKOFF_SECONDS",
    "ENV_EXCHANGE_OUTCOMES_URL",
    "ENV_STORE_AGENT_ENDPOINTS",
    "EXCHANGE_OUTCOMES_PATH",
    "MAX_PENDING_TRUST_EVENTS",
    "MAX_RETRY_BACKOFF_SECONDS",
    "MAX_UNDELIVERED_TRUST_EVENTS",
    "TRUST_EVENT_PATH",
    "ExchangeOutcomeSink",
    "StoreAgentSink",
    "TrustEventFanout",
    "announce_trust_event",
    "build_sink",
    "exchange_outcomes_url",
    "learning_loop_report",
    "log_learning_loop_state",
    "store_agent_endpoints",
    "store_history_reader",
    "trust_event_url",
]

#: The address book: a JSON object ``{store_id: base_url}``. See the module docstring.
ENV_STORE_AGENT_ENDPOINTS = "TRUST_STORE_AGENT_ENDPOINTS"

#: The door the store agent serves, published in
#: ``packages/contracts/openapi/store-agent.openapi.json`` and appended in exactly one place
#: (:func:`trust_event_url`) so no two callers can disagree about it.
TRUST_EVENT_PATH = "/v1/trust-events"

#: How long ONE POST to ONE peer may hold the calling thread.
#:
#: The same half second ``proxyshop_support.trust_ledger`` allows a ledger write, and for the
#: same reason: an agent on the same network that has not answered in half a second is down,
#: not thinking.
#:
#: **It is not the whole latency, and this comment used to say it was.** One append moves at
#: most one dimension of one store, but :class:`TrustEventFanout` sends that one delta to TWO
#: peers inline — the store's agent and the exchange — so ``POST /events`` can wait twice this,
#: not once. ``POST /reconcile`` announces N deltas in one request and is bounded by its own
#: budget rather than by this number; see ``trust.reconcile.routes``.
DEFAULT_PUSH_TIMEOUT_SECONDS = 0.5

#: How many undelivered pushes one sink remembers for an operator to read. A ring, because
#: ``POST /events`` is a request path and an outage must not grow a list without a ceiling.
MAX_UNDELIVERED_TRUST_EVENTS = 64

#: How many deltas may be waiting for a retry at once, per sink.
#:
#: Bounded for the same reason the undelivered ring is: an outage must not grow a list without a
#: ceiling on a request path. Unlike the ring, dropping from HERE loses a delivery rather than a
#: record of one, so an overflow is counted (``dropped``) and named in the ring rather than
#: silently overwriting the oldest entry.
MAX_PENDING_TRUST_EVENTS = 64

#: How many times ONE delta is posted before it is given up on: the inline attempt plus three
#: background retries.
#:
#: The inline attempt is the only one that can hold ``POST /events``; every attempt after it runs
#: on this sink's own thread, so raising this number costs the ledger's door nothing. Four is
#: chosen against the failure it exists for — a store agent restarting, or a compose stack whose
#: agents came up after the trust service — which is a matter of seconds, not minutes.
DEFAULT_RETRY_ATTEMPTS = 4

#: The delay before the FIRST retry. Doubles per attempt (0.5s, 1s, 2s), capped by
#: :data:`MAX_RETRY_BACKOFF_SECONDS`, so a retry storm cannot become the outage.
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5

#: The ceiling on that doubling.
MAX_RETRY_BACKOFF_SECONDS = 30.0

#: The exchange's learning door, ``POST /internal/outcomes``. Published in
#: ``packages/contracts/openapi/exchange.openapi.json`` and appended in exactly one place
#: (:func:`exchange_outcomes_url`) so no two callers can disagree about it.
EXCHANGE_OUTCOMES_PATH = "/internal/outcomes"

#: Where the exchange answers, as a BASE url (``http://exchange:8083``) or as the full outcomes
#: url. Empty is "this deployment does not report outcomes to an exchange", which is a working
#: stack whose bandit never learns from a completed purchase — the condition
#: :func:`learning_loop_report` exists to publish rather than leave to be inferred.
ENV_EXCHANGE_OUTCOMES_URL = "TRUST_EXCHANGE_OUTCOMES_URL"

#: HTTP statuses worth posting again. Everything else 4xx is the receiver saying the message is
#: wrong, and a message that is wrong is wrong on the second attempt too — retrying a ``409``
#: misroute or a ``400`` unroutable outcome would turn one operator-visible failure into four.
_RETRYABLE_STATUSES = frozenset({408, 425, 429})

_log = logging.getLogger(__name__)


def store_agent_endpoints(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The ``{store_id: base_url}`` address book, or ``{}`` when there is none.

    Never raises. A malformed value is reported once and read as "no address book": an
    operator's typo must not take ``POST /events`` down, and the ledger is the thing this
    service exists to protect. A value that is valid JSON but not an object of strings is the
    same condition and is treated identically.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_STORE_AGENT_ENDPOINTS) or "").strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        _log.error(
            "%s is not valid JSON (%s); no trust event will be pushed to any store agent",
            ENV_STORE_AGENT_ENDPOINTS,
            exc,
        )
        return {}
    if not isinstance(loaded, Mapping):
        _log.error(
            "%s must hold a JSON object mapping store_id to a base URL, got %s; no trust "
            "event will be pushed to any store agent",
            ENV_STORE_AGENT_ENDPOINTS,
            type(loaded).__name__,
        )
        return {}
    endpoints = {
        str(store_id): str(url).strip()
        for store_id, url in loaded.items()
        if str(url or "").strip()
    }
    return endpoints


def trust_event_url(base_url: str) -> str:
    """``base_url`` plus :data:`TRUST_EVENT_PATH`, with exactly one slash between them."""
    return f"{str(base_url).rstrip('/')}{TRUST_EVENT_PATH}"


def exchange_outcomes_url(environ: Mapping[str, str] | None = None) -> str | None:
    """The exchange's ``POST /internal/outcomes`` url, or ``None`` when none is configured.

    Accepts either the exchange's base url (``http://exchange:8083``) or the full outcomes url,
    because both spellings will be written by somebody and neither is wrong — a value that
    already ends in :data:`EXCHANGE_OUTCOMES_PATH` is taken as-is and anything else has the path
    appended, which makes ``TRUST_EXCHANGE_OUTCOMES_URL=http://exchange:8083`` and
    ``…:8083/internal/outcomes`` the same deployment rather than one working and one 404ing.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_EXCHANGE_OUTCOMES_URL) or "").strip()
    if not raw:
        return None
    trimmed = raw.rstrip("/")
    if trimmed.endswith(EXCHANGE_OUTCOMES_PATH):
        return trimmed
    return f"{trimmed}{EXCHANGE_OUTCOMES_PATH}"


@dataclass
class _Pending:
    """One delta waiting for another attempt. Held in memory only; see :class:`StoreAgentSink`."""

    store_id: str
    event_id: str
    url: str
    payload: dict[str, Any]
    attempts: int
    due_at: float
    reason: str


class StoreAgentSink:
    """Posts one trust delta to ONE store's agent, and keeps score. :meth:`send` never raises.

    Duck-typed against :func:`trust.feedback.push_trust_event`'s ``sink`` parameter — that
    function calls ``sink.send(store_id, payload)`` and deliberately does not swallow an
    ``AttributeError`` from a sink that cannot send, which is why the method is spelled
    exactly that way here.
    """

    #: What this sink calls the thing it posts to, for log lines and ring reasons. Overridden by
    #: :class:`ExchangeOutcomeSink`, which posts the same payload to a different kind of peer.
    peer = "store agent"

    def __init__(
        self,
        endpoints: Mapping[str, str],
        *,
        timeout: float = DEFAULT_PUSH_TIMEOUT_SECONDS,
        log: logging.Logger | None = None,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: The address book, copied. A sink that read a live mapping could be re-addressed
        #: mid-request by whatever owned it, and "which store did this go to" would stop
        #: being answerable from the sink alone.
        self.endpoints: dict[str, str] = {str(k): str(v) for k, v in endpoints.items()}
        #: Deltas a store agent accepted.
        self.delivered = 0
        #: Deltas that did not land, ever, on this sink — after every retry was spent.
        self.lost = 0
        #: Deltas that landed on an attempt AFTER the inline one. The evidence that retrying is
        #: doing something; zero here with a non-zero ``lost`` means retrying is not the answer.
        self.retried = 0
        #: Deltas dropped because the retry queue was full. Distinct from ``lost``: these were
        #: never given their attempts.
        self.dropped = 0
        #: The subset of ``lost`` that had nowhere to go in the first place — no endpoint for
        #: that store, or an outcome naming no cluster. A configuration fact, not an outage,
        #: and deliberately NOT allowed to move ``delivering``. See :meth:`_failed`.
        self.undeliverable = 0
        #: ``(store_id, event_id, reason)`` for the deltas that did not land, newest last.
        #: Ids and reasons only — never a payload, and never a buyer pseudonym.
        self.undelivered: deque[tuple[str, str, str]] = deque(maxlen=MAX_UNDELIVERED_TRUST_EVENTS)
        self._timeout = float(timeout)
        self._log = log if log is not None else _log
        self._client: Any | None = None
        # Starts True so a deployment whose every push lands says nothing at all. The first
        # failure is a state CHANGE and is reported; the hundredth is counted.
        self._delivering = True
        self._lost_in_this_outage = 0
        self._attempts = max(1, int(retry_attempts))
        self._backoff = max(0.0, float(retry_backoff))
        self._now = clock
        # The retry queue carries PAYLOADS, which the readable ring deliberately does not. That
        # is not a relaxation of the ring's rule: what is posted has already been through
        # `trust.feedback.scrub`, and this deque is never rendered by `report()` — only its
        # length is. The ring stays ids-and-reasons because the ring is the thing an operator
        # reads; this is a delivery buffer nobody reads.
        self._pending: deque[_Pending] = deque()
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._closed = False

    @property
    def delivering(self) -> bool:
        """Whether the last push landed — the condition the two log lines report."""
        return self._delivering

    @property
    def pending(self) -> int:
        """How many deltas are waiting for another attempt right now."""
        with self._condition:
            return len(self._pending)

    @property
    def addressed(self) -> bool:
        """Whether this deployment was told where anything answers.

        The distinction the whole loudness of this module rests on: ``False`` is a learning loop
        that is switched OFF, which is a configuration fact and not an outage, and it must not
        read as "delivering fine" merely because nothing has failed yet.
        """
        return bool(self.endpoints)

    def addresses(self, store_id: Any) -> str | None:
        """The URL this sink would POST a delta for ``store_id`` to, or ``None``.

        No default and no wildcard, deliberately. An unknown store is a store this deployment
        was never told the address of, and the only alternative to sending nothing is sending
        one store's trust movement to somebody else's agent.
        """
        base = self.endpoints.get(str(store_id))
        return trust_event_url(base) if base else None

    def send(self, store_id: Any, payload: Mapping[str, Any]) -> bool:
        """POST one ``TrustEventPayload`` to ``store_id``'s agent. Returns whether it landed.

        Never raises. Every failure mode of an outbound POST — DNS, connect, timeout, a proxy
        answering something unparseable — is an operational problem with a notification and
        none of them is a reason to fail an append that already happened, so the catch is
        blanket rather than a list of ``httpx`` exception classes.
        """
        addressee = str(store_id)
        event_id = str((payload.get("event") or {}).get("event_id", "")) or "unknown"

        # The payload names its own store. If that disagrees with the address we were asked
        # to send to, the two are not the same claim and the safe reading is that a routing
        # bug is about to hand store A's movement to store B.
        stated = str(payload.get("store_id", ""))
        if stated and stated != addressee:
            self._failed(
                addressee,
                event_id,
                f"payload names store {stated!r} and the send was addressed to "
                f"{addressee!r}; refusing rather than delivering one store's trust "
                f"movement to another",
            )
            return False

        url = self.addresses(addressee)
        if url is None:
            # NOT an outage — nobody failed to answer, this deployment named nobody to ask.
            self._failed(addressee, event_id, self.unaddressed_reason(addressee), outage=False)
            return False

        landed, reason, retryable = self._attempt(url, payload)
        if landed:
            self._landed()
            return True
        self._queue_or_lose(addressee, event_id, url, payload, reason, retryable)
        return False

    def unaddressed_reason(self, store_id: str) -> str:
        """Why a delta for ``store_id`` has nowhere to go. Read into the ring verbatim.

        It does NOT enumerate the address book, and that omission is the point rather than
        brevity. This string is served on ``GET /events/verify``, ``apps/trust`` has not one
        ``Depends`` anywhere, and the list of stores that HAVE an agent is the network's supply
        roster — which store pays for an advocate. Handing that to any anonymous caller is the
        same class of leak R13 forbids when it refuses to broadcast one store's delta to its
        competitors. The store this event is about is already public on ``GET /events``; who
        else is in the network is not.
        """
        return (
            f"no {self.peer} endpoint is configured for {store_id!r} (see "
            f"{ENV_STORE_AGENT_ENDPOINTS}), so this store's trust movement was told to nobody"
        )

    def deliverable(self, store_id: Any) -> bool:
        """Whether a delta for ``store_id`` has anywhere at all to go.

        Asked BEFORE a delta is computed, so that an unaddressed deployment does not pay a
        database round trip and two scoring passes per observation to produce a number it will
        immediately throw away. :func:`announce_trust_event` is the caller.
        """
        return self.addresses(store_id) is not None

    def note_undeliverable(self, store_id: Any, event_id: str) -> str:
        """Record that there was nowhere to send this store's delta. Returns the reason.

        Not an outage: see :meth:`_failed`.
        """
        reason = self.unaddressed_reason(str(store_id))
        self._failed(str(store_id), event_id, reason, outage=False)
        return reason

    def status(self) -> dict[str, Any]:
        """The whole delivery condition as a mapping — for a health route or an operator.

        Ids and reasons only; no payload and no pseudonym can reach it, and no roster: see
        :meth:`unaddressed_reason` for why ``addressed_stores`` is a COUNT rather than the list
        it used to be.
        """
        with self._condition:
            last = self.undelivered[-1][2] if self.undelivered else None
            return {
                "addressed_stores": len(self.endpoints),
                "delivering": self._delivering,
                "delivered": self.delivered,
                "lost": self.lost,
                "undeliverable": self.undeliverable,
                "last_failure": last,
            }

    def report(self) -> dict[str, Any]:
        """:meth:`status`, plus the undelivered ring itself and the retry queue's depth.

        This is the READBACK the ring existed for and did not have. ``status()`` answers "is it
        delivering"; an operator whose answer is *no* then needs to know **what** was lost, and
        the only place that has ever been recorded is this in-process deque. Ids and reasons
        only, exactly as they are held — no payload and no pseudonym can reach it.

        ``addressed`` is the one that separates a broken loop from a disabled one: false means
        this deployment was never told where anything answers, which is not an outage.

        **The ring is snapshotted under the lock**, not iterated in place. Iterating it live
        raced the retry thread's ``_failed`` and raised ``RuntimeError: deque mutated during
        iteration`` — which ``notification_report`` then served as ``available: false``, so the
        readback failed exactly while the outage it exists to describe was in progress.
        """
        with self._condition:
            pending = len(self._pending)
            ring = list(self.undelivered)
        return {
            **self.status(),
            "peer": self.peer,
            "addressed": self.addressed,
            "retried": self.retried,
            "pending_retry": pending,
            "dropped": self.dropped,
            "retry_attempts": self._attempts,
            "undelivered": [
                {"store_id": store, "event_id": event, "reason": reason}
                for store, event, reason in ring
            ],
            "undelivered_ring_capacity": MAX_UNDELIVERED_TRUST_EVENTS,
            # The ring is in-process and dies with the process. Said out loud on the readback
            # itself, because an operator reading an empty ring after a restart must not read
            # it as "nothing was lost".
            "durable": False,
        }

    def close(self) -> None:
        """Stop the retry worker and release the pooled client, if either was ever built."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        worker = self._worker
        self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout=1.0)
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    # -- plumbing -------------------------------------------------------------------
    def _attempt(self, url: str, payload: Mapping[str, Any]) -> tuple[bool, str, bool]:
        """POST once. Returns ``(landed, reason, worth_retrying)`` and never raises."""
        try:
            response = self._http_client().post(url, json=dict(payload))
        except Exception as exc:  # noqa: BLE001 - see `send`'s docstring
            # Every transport failure is worth another go: DNS that has not propagated, a
            # connection refused by an agent that is still starting, a timeout on a busy box.
            return False, f"{type(exc).__name__}: {exc}", True
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            retryable = status >= 500 or status in _RETRYABLE_STATUSES
            return False, f"the {self.peer} answered HTTP {status}", retryable
        return True, "", False

    def _queue_or_lose(
        self,
        store_id: str,
        event_id: str,
        url: str,
        payload: Mapping[str, Any],
        reason: str,
        retryable: bool,
    ) -> None:
        """Put a failed delta on the retry queue, or give up on it now and say so."""
        if not retryable or self._attempts <= 1:
            self._failed(store_id, event_id, reason)
            return
        with self._condition:
            if len(self._pending) >= MAX_PENDING_TRUST_EVENTS:
                self.dropped += 1
                self._failed(
                    store_id,
                    event_id,
                    f"{reason}; and the retry queue was already holding "
                    f"{MAX_PENDING_TRUST_EVENTS} deltas, so this one was never retried",
                )
                return
            self._pending.append(
                _Pending(
                    store_id=store_id,
                    event_id=event_id,
                    url=url,
                    payload=dict(payload),
                    attempts=1,
                    due_at=self._now() + self._backoff,
                    reason=reason,
                )
            )
            self._condition.notify_all()
        # The outage is announced on the FIRST failure, before any retry has been spent —
        # otherwise an operator learns about it `attempts x backoff` later than the ledger did.
        self._note_outage(store_id, event_id, reason)
        self._start_worker()

    def _start_worker(self) -> None:
        """Start the retry thread if it is not already running. Idempotent.

        The thread is created AND started inside the lock. Starting it outside left a window
        in which a second caller saw a ``self._worker`` that existed but was not yet alive,
        concluded there was none, and started a second drain thread — harmless, because the
        queue itself is guarded, but two threads racing a bounded queue is not a thing to leave
        lying around for the next reader to have to reason about.
        """
        with self._condition:
            if self._closed:
                return
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._drain, name=f"trust-push-retry-{id(self):x}", daemon=True
            )
            self._worker.start()

    def _drain(self) -> None:
        """Re-post everything on the queue, oldest first, backing off between attempts.

        Runs on this sink's own daemon thread, so nothing here can hold ``POST /events`` — the
        one property the inline attempt in :meth:`send` is bounded by a timeout to protect. A
        blanket ``except`` around the whole loop for the same reason as everywhere else in this
        module: a retry worker that dies takes the retries with it and must say so.
        """
        try:
            while True:
                with self._condition:
                    while not self._pending and not self._closed:
                        self._condition.wait(timeout=1.0)
                    if self._closed:
                        return
                    waiting = self._pending[0].due_at - self._now()
                    if waiting > 0:
                        self._condition.wait(timeout=min(waiting, 1.0))
                        continue
                    item = self._pending.popleft()
                landed, reason, retryable = self._attempt(item.url, item.payload)
                if landed:
                    with self._condition:
                        self.retried += 1
                    self._landed()
                    continue
                item.attempts += 1
                item.reason = reason
                if not retryable or item.attempts >= self._attempts:
                    self._failed(
                        item.store_id,
                        item.event_id,
                        f"{reason}; given up after {item.attempts} attempt(s)",
                    )
                    continue
                delay = min(MAX_RETRY_BACKOFF_SECONDS, self._backoff * (2 ** (item.attempts - 1)))
                item.due_at = self._now() + delay
                with self._condition:
                    self._pending.append(item)
        except Exception:  # noqa: BLE001 - a dead retry worker must not be a silent one
            self._log.exception(
                "the trust-event retry worker stopped; deltas still queued for a %s will not "
                "be retried by this process",
                self.peer,
            )

    def _note_outage(self, store_id: str, event_id: str, reason: str) -> None:
        """Report the first failure of an outage. The hundredth is counted, not logged.

        Under the lock, like every other counter mutation here: ``send`` runs on FastAPI's
        threadpool and the retry worker runs on its own thread, so ``self._delivering = False``
        and the ``ERROR`` beside it are a read-modify-write two threads can interleave —
        which would print the outage line twice, or not at all.
        """
        with self._condition:
            if not self._delivering:
                return
            self._delivering = False
        self._log.error(
            "trust events stopped reaching %ss (%s, on event %s for store %s). "
            "The event IS in the chained ledger; only the notification did not land. "
            "Ids and reasons are on this sink's undelivered ring and report() now reads "
            "delivering=false. The next delta that lands is logged; the ones in between "
            "are counted",
            self.peer,
            reason,
            event_id,
            store_id,
        )

    def _failed(self, store_id: str, event_id: str, reason: str, *, outage: bool = True) -> None:
        """Record a delta that did not land. ``outage`` says whether it is EVIDENCE OF ONE.

        The distinction was missing and it inverted the whole health surface. A store this
        deployment holds no address for, and an outcome that names no cluster, are
        CONFIGURATION facts — the class's own ``addressed`` docstring says so — and neither is
        a peer failing to answer. Filed as outages they made ``delivering`` flap per event: the
        demo corpus rosters ten storefronts and ships four agents, so an ordinary run alternated
        ``ERROR trust events stopped reaching store agents`` / ``INFO … reaching them again``
        with nothing wrong, and ``GET /events/verify`` reported whichever store the LAST event
        happened to be about. Every R14 buyer-feedback append did the same to the exchange half,
        because buyer feedback carries no cluster and never will.

        They are still counted in ``lost`` — the delta genuinely did not land — and they are
        still on the ring with their reason. What they no longer do is claim a peer is down.
        """
        with self._condition:
            self.undelivered.append((store_id, event_id, reason))
            self.lost += 1
            if not outage:
                self.undeliverable += 1
                return
            self._lost_in_this_outage += 1
        self._note_outage(store_id, event_id, reason)

    def _landed(self) -> None:
        with self._condition:
            self.delivered += 1
            if self._delivering:
                return
            self._delivering = True
            missed = self._lost_in_this_outage
            self._lost_in_this_outage = 0
        self._log.info(
            "trust events are reaching %ss again; %d delta(s) did not land during that "
            "outage and no store was told about them",
            self.peer,
            missed,
        )

    def _http_client(self) -> Any:
        """One pooled client for this sink, built on first use.

        Deferred rather than built in ``__init__`` — the convention
        ``proxyshop_support.trust_ledger`` already follows — so that constructing a sink opens
        no sockets and imports no ``httpx``, and a deployment with an empty address book pays
        for neither. It also keeps this package importable by the stdlib-only path
        ``trust.feedback.__init__`` binds its submodules through.

        Under the lock, because it is now built from TWO threads: the request thread on the
        inline attempt and the retry worker on every attempt after it. Unguarded, both can pass
        the ``is None`` check and one of the two clients is then leaked with its connection pool
        — which ``close()`` cannot reach, because ``self._client`` names the other one.
        """
        with self._condition:
            if self._client is None:
                import httpx  # noqa: PLC0415 - see the docstring

                self._client = httpx.Client(timeout=self._timeout)
            return self._client


class ExchangeOutcomeSink(StoreAgentSink):
    """Posts the SAME ``TrustEventPayload`` to the exchange's ``POST /internal/outcomes``.

    That door was served with no production producer anywhere in the repository: its only
    callers were tests and the e2e harness, so ``exchange.policy.bandit.update`` never ran in a
    deployment and every posterior sat on its seeded prior for the life of the process. The
    exchange's own summary of the route is "Trust reports an outcome back to the exchange for
    the bandit update" — this class is trust doing that.

    It is a subclass rather than a second implementation because everything that makes the
    store-agent push safe is the same here: never raise, bound the inline attempt, retry with
    backoff off the request path, count what landed, keep a readable ring of what did not, and
    log the two state changes rather than the steady state. What differs is exactly three
    things — one fixed address instead of a per-store address book, no privacy seal (the
    exchange is the platform, and it already knows which store was in which auction), and the
    pre-flight below.

    **The pre-flight, and what it may no longer decide.** ``POST /internal/outcomes`` refuses an
    outcome it cannot route to a posterior, under ``outcome_carries_no_cluster``: exposure is
    decided WITHIN a cluster, and pooling an unroutable outcome into a shared bucket would move
    clusters it never happened in.

    This pre-flight used to refuse, here, every delta whose ``pseudonymous_context.cluster_id``
    was empty, on the reasoning that such a post was "a guaranteed 400". It was — and since
    NOTHING in this system ever populated that field, the guarantee held for every outcome
    without exception: ``delivered: 0``, ``lost: 536``, read off this service's own
    ``GET /events/verify``. The pre-flight was not saving a wasted socket; it was the near end of
    a loop that had never once closed.

    The cluster is the exchange's own fact — it is assigned there, from a catalogue only the
    exchange holds, and stamped on the auction record — so the exchange now resolves it from
    ``event.auction_id`` (``exchange.policy.routes._cluster_of_the_auction``). What this
    pre-flight decides is therefore narrowed to the one case no receiver could ever answer
    either: a delta naming **neither** a cluster nor an auction. That is still refused here,
    still counted, still on the ring with its reason, and the socket is still not opened for it.
    Everything else is posted, and a 400 that comes back is the exchange saying the auction is
    gone or ran in no cluster — a receiver's verdict this service is not entitled to predict.
    """

    peer = "exchange"

    def __init__(self, url: str, **kwargs: Any) -> None:
        super().__init__({}, **kwargs)
        #: The one address. ``None`` is not representable — a sink with no url is not built.
        self.url = str(url)

    @property
    def addressed(self) -> bool:
        return bool(self.url)

    def addresses(self, store_id: Any) -> str | None:
        """One url for every store: the exchange decides exposure across all of them."""
        return self.url or None

    def deliverable(self, store_id: Any) -> bool:
        return bool(self.url)

    def unaddressed_reason(self, store_id: str) -> str:
        return (
            f"no exchange outcomes url is configured ({ENV_EXCHANGE_OUTCOMES_URL} is unset), so "
            f"the outcome for {store_id!r} moved no posterior"
        )

    def send(self, store_id: Any, payload: Mapping[str, Any]) -> bool:
        context = payload.get("pseudonymous_context")
        cluster = ""
        if isinstance(context, Mapping):
            cluster = str(context.get("cluster_id") or "").strip()
        event = payload.get("event")
        event = event if isinstance(event, Mapping) else {}
        auction_id = str(event.get("auction_id") or "").strip()
        if not cluster and not auction_id:
            event_id = str(event.get("event_id", "")) or "unknown"
            # NOT an outage. This is a delta about an event that names neither a cluster nor an
            # auction, so no party in this system holds the fact — filing it as "the exchange
            # stopped answering" made an ordinary healthy append print an ERROR and leave
            # `delivering: false` on the health surface with nothing wrong.
            self._failed(
                str(store_id),
                event_id,
                "the delta names no cluster, and no auction to resolve one from, and exposure "
                "is decided within a cluster — nothing can route this outcome to a posterior, "
                "and pooling it into a shared bucket would move clusters it never happened in",
                outage=False,
            )
            return False
        return super().send(store_id, payload)

    def report(self) -> dict[str, Any]:
        # No `url`. It is the exchange's INTERNAL address and this report is served by an
        # unauthenticated route; `addressed` is the part an operator needs and the hostname is
        # the part an attacker does. See `StoreAgentSink.unaddressed_reason`.
        return super().report()


class TrustEventFanout:
    """One delta, to the store's own agent AND to the exchange's bandit. Never raises.

    Duck-typed against :func:`trust.feedback.push_trust_event`'s ``sink`` parameter exactly as
    :class:`StoreAgentSink` is, and it deliberately reports ``delivered``/``undelivered`` from
    the STORE-AGENT half. :func:`announce_trust_event` reads those two to decide whether the
    push landed, and "the affected store was told" is the claim that function makes; folding the
    exchange's result into the same counter would make an append look delivered because a
    posterior moved, which is a different sentence about a different recipient.
    """

    def __init__(
        self, store_agents: StoreAgentSink, exchange: ExchangeOutcomeSink | None = None
    ) -> None:
        self.store_agents = store_agents
        self.exchange = exchange

    @property
    def delivered(self) -> int:
        return self.store_agents.delivered

    @property
    def undelivered(self) -> deque[tuple[str, str, str]]:
        return self.store_agents.undelivered

    def deliverable(self, store_id: Any) -> bool:
        """Whether EITHER half has somewhere to send this store's delta."""
        if self.store_agents.deliverable(store_id):
            return True
        return self.exchange is not None and self.exchange.deliverable(store_id)

    def note_undeliverable(self, store_id: Any, event_id: str) -> str:
        """Record on both halves that there was nowhere to send this. Returns one reason."""
        reason = self.store_agents.note_undeliverable(store_id, event_id)
        if self.exchange is not None:
            self.exchange.note_undeliverable(store_id, event_id)
        return reason

    def send(self, store_id: Any, payload: Mapping[str, Any]) -> bool:
        landed = self.store_agents.send(store_id, payload)
        if self.exchange is not None:
            # Independently, and after: an exchange that is down must not stop a store agent
            # being told, and vice versa. Neither call raises, so no ordering here can fail.
            #
            # TWO INLINE ATTEMPTS, and a caller budgeting the door's latency has to count both:
            # each half allows itself `DEFAULT_PUSH_TIMEOUT_SECONDS`, so one fanned-out delta is
            # worth up to twice that, not once. `reconcile.routes` is the caller for which that
            # matters — it announces N of these in one request.
            self.exchange.send(store_id, payload)
        return landed

    def report(self) -> dict[str, Any]:
        """The whole learning loop's delivery condition, both halves."""
        return {
            "enabled": self.store_agents.addressed or bool(self.exchange),
            "store_agents": self.store_agents.report(),
            "exchange_outcomes": self.exchange.report() if self.exchange is not None else None,
        }

    def close(self) -> None:
        self.store_agents.close()
        if self.exchange is not None:
            self.exchange.close()


def build_sink(environ: Mapping[str, str] | None = None, **kwargs: Any) -> TrustEventFanout:
    """The sink this service pushes through, built from the environment. Never raises.

    A sink is ALWAYS built, including when nothing is addressed, and that is the change this
    module needed most. Before it, an unaddressed deployment resolved its sink to ``None`` and
    ``POST /events`` skipped the whole announce — so no delta was computed, nothing was
    recorded, and the state "this stack's learning loop is switched off" was invisible from
    inside the process. Now every delta is computed and every non-delivery lands in a ring an
    operator can read, with the reason naming the variable that would have addressed it.
    """
    env = os.environ if environ is None else environ
    outcomes = exchange_outcomes_url(env)
    return TrustEventFanout(
        StoreAgentSink(store_agent_endpoints(env), **kwargs),
        ExchangeOutcomeSink(outcomes, **kwargs) if outcomes else None,
    )


def learning_loop_report(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What this deployment's configuration says about the learning loop. Reads no socket.

    Two halves, and a stack can have either without the other:

    * **store agents** — where each store's own advocate is told what its score did (R13). This
      is the input a shop's dedicated advocate learns from, and D55 makes that advocate the
      thing a shop buys by joining the network.
    * **the exchange** — where a completed purchase's trust movement becomes a bandit outcome,
      which is the only thing that ever moves exposure with results.
    """
    env = os.environ if environ is None else environ
    endpoints = store_agent_endpoints(env)
    outcomes = exchange_outcomes_url(env)
    return {
        "enabled": bool(endpoints) or bool(outcomes),
        "store_agent_push": {
            "enabled": bool(endpoints),
            "variable": ENV_STORE_AGENT_ENDPOINTS,
            "stores": sorted(endpoints),
        },
        "exchange_outcomes": {
            "enabled": bool(outcomes),
            "variable": ENV_EXCHANGE_OUTCOMES_URL,
            "url": outcomes,
        },
    }


def log_learning_loop_state(
    environ: Mapping[str, str] | None = None, log: logging.Logger | None = None
) -> dict[str, Any]:
    """Say, at boot, whether this process can learn anything. Returns the report it logged.

    **A silently disabled learning loop is the defect, not the empty variable.** Both halves
    default to unset and both are legitimate deployments — the alternative to an address is a
    guess, and a guessed address is how one store's trust movement reaches a competitor — but a
    started process that will never tell a store agent anything and will never move a posterior
    must SAY so once, at the moment it starts, rather than leaving an operator to infer it from
    an absence of traffic weeks later.
    """
    logger = log if log is not None else _log
    report = learning_loop_report(environ)
    agents = report["store_agent_push"]
    outcomes = report["exchange_outcomes"]
    if agents["enabled"]:
        # ADDRESSED, not reachable, and the wording has to keep that distinction because this
        # line reads the environment and opens no socket. It said "deltas will be pushed to 4
        # store agent(s)" and, in the default `docker compose up` — where the four demo agents
        # are behind `--profile demo` and do not exist — that was a confident falsehood printed
        # at boot, which is worse than the silence it replaced. Whether they ANSWER is a
        # different question with a different answer, and `GET /events/verify` is where it is
        # answered, continuously, from counters rather than from configuration.
        logger.info(
            "trust learning loop: %d store agent(s) are ADDRESSED (%s). Whether any of them "
            "answers is not known until a delta is pushed; GET /events/verify reports what "
            "landed and what did not",
            len(agents["stores"]),
            ", ".join(agents["stores"]),
        )
    else:
        logger.warning(
            "trust learning loop: NO store agent is addressed (%s is unset or empty), so every "
            "trust delta this process computes will be recorded as undelivered and no store's "
            "advocate will ever learn what its score did. Set it to a JSON object "
            '{"store_id": "http://its-agent:8086"} to turn the loop on',
            ENV_STORE_AGENT_ENDPOINTS,
        )
    if outcomes["enabled"]:
        logger.info(
            "trust learning loop: an exchange is ADDRESSED for completed-purchase outcomes "
            "(%s); only outcomes that name a cluster can be routed to a posterior",
            outcomes["url"],
        )
    else:
        logger.warning(
            "trust learning loop: NO exchange is addressed (%s is unset), so no completed "
            "purchase will ever move a bandit posterior and exposure will not shift with "
            "results. Set it to the exchange's base url (http://exchange:8083)",
            ENV_EXCHANGE_OUTCOMES_URL,
        )
    return report


def store_history_reader(
    store: Any, *, before_seq: int, limit: int = MAX_DELTA_HISTORY_EVENTS
) -> Callable[[str], list[dict[str, Any]]]:
    """A callable returning ONE store's ledger rows from before ``before_seq``.

    It takes the ``store_id`` rather than closing over one, because the store an event is
    ABOUT is decided by ``trust.ledger.replay.observations_from_events`` (which falls back to
    ``payload.store_id``) and a second copy of that rule here would be free to drift from the
    one the scorer actually groups by. :func:`announce_trust_event` resolves the store once
    and hands it in.

    The read is deferred into a callable so that an event carrying no observation costs no
    database round trip at all: it is asked for only after the event is known to move a
    dimension, which is a minority of ledger traffic.

    ``limit + 1`` rows are asked for on purpose — see :func:`~.deltas.bounded_history` — and
    the filter is by ``seq`` rather than by ``event_id`` because the caller has just appended
    the event and needs the record as it stood *before* it, not the record with one row
    plucked out of the middle.
    """

    def read(store_id: str) -> list[dict[str, Any]]:
        rows = store.read(store_id=str(store_id), limit=limit + 1)
        prior = [row for row in rows if int(row.get("seq", 0)) < int(before_seq)]
        return bounded_history(prior, limit=limit)

    return read


def _can_deliver(sink: Any, store_id: str) -> bool:
    """Whether ``sink`` has anywhere to send ``store_id``'s delta. Optimistic when unknown.

    Duck-typed and defaulting to ``True``, because ``sink`` is documented as "anything exposing
    ``send(store_id, payload)``" and a recording double in a test exposes nothing else. A sink
    that cannot answer the question is asked to send, exactly as before.
    """
    asker = getattr(sink, "deliverable", None)
    if not callable(asker):
        return True
    try:
        return bool(asker(store_id))
    except Exception:  # noqa: BLE001 - a sink that cannot answer is asked to send
        return True


def _record_undeliverable(sink: Any, store_id: str, event_id: str) -> str:
    """Ask ``sink`` to record that there was nowhere to send this. Returns the reason."""
    recorder = getattr(sink, "note_undeliverable", None)
    if callable(recorder):
        try:
            return str(recorder(store_id, event_id))
        except Exception:  # noqa: BLE001 - a recorder that cannot record is not a failed append
            pass
    return (
        f"this deployment addresses no recipient for store {store_id!r}, so the delta was not "
        f"computed and nobody was told"
    )


def announce_trust_event(
    event: Mapping[str, Any],
    *,
    history_reader: Callable[[str], Iterable[Mapping[str, Any]]],
    sink: Any,
) -> dict[str, Any]:
    """Tell the affected store what this event did to its posture. **Never raises.**

    Args:
        event: the stored ledger row, as the append returned it.
        history_reader: called with the affected ``store_id`` and yielding that store's prior
            rows. Called at most once, and only for an event that actually carries an
            observation.
        sink: anything exposing ``send(store_id, payload)`` — :class:`StoreAgentSink` in
            production, a recording double in a test.

    Returns:
        A small mapping describing what happened, for a caller that wants to log or assert
        on it: ``{"pushed", "reason", "store_id", "dim", "delta"}``. ``pushed`` is ``False``
        with a ``reason`` for every non-delivery, including the ordinary one — an event that
        moves no dimension.

    This function is the guarantee that ``POST /events`` cannot be broken by anything
    downstream of the append, so its ``except`` is blanket by design. A scoring failure over
    a legacy row, an address book that changed shape, a bug in this module: all of them are
    an undelivered notification and none of them is a reason to tell a producer that an event
    which IS in the hash chain was refused.

    Whether the delta LANDED is read off the sink's own ``delivered`` counter, before and
    after, rather than off the fact that :func:`push_trust_event` returned. That function
    returns the payload it sent and deliberately reports nothing about delivery — a sink whose
    whole contract is "never raise" would otherwise make every push look successful, which is
    exactly the silence this module exists to end. A sink with no counter is reported as
    delivered, because a send that did not raise is the only evidence such a sink offers.
    """
    event_id = str(event.get("event_id", "unknown"))

    # BEFORE the history read, and that ordering is the point: the overwhelming majority of
    # ledger traffic (`accepted`, `auction_opened`, `code_created`) moves no dimension, and
    # those events must cost `POST /events` no database round trip and no socket.
    observation = observation_of(event)
    if observation is None:
        return {
            "pushed": False,
            "reason": "the event carries no trust observation, so no dimension moved",
            "store_id": None,
            "dim": None,
            "delta": None,
        }
    affected = str(observation["store_id"])

    # A store named ONLY inside `payload` is a store whose history cannot be read. The
    # projection accepts that fallback (``event.store_id or payload.store_id``) but the ledger
    # read is by the indexed `store_id` COLUMN, which such a row leaves null -- so the history
    # would come back empty and the delta would be computed as if the store had never been
    # seen before. That is a confidently wrong number, and the rule this module follows about
    # those is the one `deltas.bounded_history` follows: say so and push nothing.
    if affected != str(event.get("store_id") or ""):
        reason = (
            f"event {event_id} states store {affected!r} only in its payload, so the store's "
            f"prior ledger rows cannot be read by the indexed store_id column and any delta "
            f"would be computed against an empty history"
        )
        _log.warning("%s; no trust event was pushed", reason)
        return {"pushed": False, "reason": reason, "store_id": affected, "dim": None, "delta": None}

    # BEFORE the history read and the two scoring passes, and that ordering is the whole reason
    # this check exists rather than letting `send` discover it. `delta_for_event` reads up to
    # `MAX_DELTA_HISTORY_EVENTS + 1` of this store's ledger rows out of Postgres and scores them
    # twice — a real query on `POST /events`'s request path. A deployment that addresses nobody
    # would pay that on every observation-carrying append to produce a number no recipient will
    # ever see. The non-delivery is still RECORDED, with its reason, so the loudness this module
    # exists for is unchanged; what is skipped is only the arithmetic nobody reads.
    if not _can_deliver(sink, affected):
        reason = _record_undeliverable(sink, affected, event_id)
        return {"pushed": False, "reason": reason, "store_id": affected, "dim": None, "delta": None}

    try:
        delta = delta_for_event(event, history=history_reader(affected))
    except Exception as exc:  # noqa: BLE001 - see the docstring
        reason = f"{type(exc).__name__}: {exc}"
        _log.warning(
            "no trust delta was computed for event %s (%s); the event IS in the ledger and "
            "the affected store was not told about it",
            event_id,
            reason,
        )
        return {"pushed": False, "reason": reason, "store_id": None, "dim": None, "delta": None}

    if delta is None:  # pragma: no cover - `observation_of` above already decided this
        return {
            "pushed": False,
            "reason": "the event carries no trust observation, so no dimension moved",
            "store_id": None,
            "dim": None,
            "delta": None,
        }

    before = int(getattr(sink, "delivered", 0) or 0)
    try:
        push_trust_event(delta, sink)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        reason = f"{type(exc).__name__}: {exc}"
        _log.warning("the trust delta for event %s could not be pushed (%s)", event_id, reason)
        return {
            "pushed": False,
            "reason": reason,
            "store_id": delta["store_id"],
            "dim": delta["dim"],
            "delta": delta["delta"],
        }

    counted = hasattr(sink, "delivered")
    pushed = (int(sink.delivered) > before) if counted else True
    return {
        "pushed": pushed,
        "reason": ""
        if pushed
        else (sink.undelivered[-1][2] if getattr(sink, "undelivered", None) else "not delivered"),
        "store_id": delta["store_id"],
        "dim": delta["dim"],
        "delta": delta["delta"],
    }
