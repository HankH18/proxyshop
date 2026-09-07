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
log the two state CHANGES and not the steady state, and publish the whole condition through
:meth:`StoreAgentSink.status` so an operator can read it after the log line scrolls away.

It is not a subclass and not a reuse of that class: ``TrustLedgerPublisher`` POSTs a
``LedgerEvent`` to one fixed URL and keys its ring on ``event_id``. This POSTs a
``TrustEventPayload`` to a URL chosen per store and has to key its ring on the pair, so the
two share a posture and not an implementation.

**3. Failure is never silent.** ``lost``, ``undelivered`` and ``status()`` are readable at any
moment, and the first failure of an outage is an ``ERROR`` naming the store.

CONFIGURATION, AND WHAT MUST SHIP IT
-------------------------------------
:data:`ENV_STORE_AGENT_ENDPOINTS` is a JSON object mapping ``store_id`` to the base URL of
that store's agent, e.g.::

    TRUST_STORE_AGENT_ENDPOINTS={"store-alpha":"http://store-agent-alpha:8086"}

Unset, or set to ``{}``, the trust service pushes nothing at all and every append behaves
exactly as it did before this module existed. That is the deliberate default: a deployment
that has not been told where a store's agent answers must not guess, and guessing is the
only other option. **``apps/trust/compose.yaml`` and ``.env.example`` must carry this key**
for a composed stack to deliver trust events; a single-agent dev stack sets it to the one
agent it runs. There is no new dependency to ship — ``httpx`` is already in the trust image
(``proxyshop_support.trust_ledger`` imports it the same lazy way).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections import deque
from collections.abc import Callable, Iterable, Mapping
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
    "ENV_STORE_AGENT_ENDPOINTS",
    "MAX_UNDELIVERED_TRUST_EVENTS",
    "TRUST_EVENT_PATH",
    "StoreAgentSink",
    "announce_trust_event",
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

#: How long ONE push may hold the ``POST /events`` worker thread.
#:
#: The same half second ``proxyshop_support.trust_ledger`` allows a ledger write, and for the
#: same reason: an agent on the same network that has not answered in half a second is down,
#: not thinking. At most one push happens per append (one event moves at most one dimension of
#: at most one store), so this is the whole latency this module can add to the door.
DEFAULT_PUSH_TIMEOUT_SECONDS = 0.5

#: How many undelivered pushes one sink remembers for an operator to read. A ring, because
#: ``POST /events`` is a request path and an outage must not grow a list without a ceiling.
MAX_UNDELIVERED_TRUST_EVENTS = 64

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


class StoreAgentSink:
    """Posts one trust delta to ONE store's agent, and keeps score. :meth:`send` never raises.

    Duck-typed against :func:`trust.feedback.push_trust_event`'s ``sink`` parameter — that
    function calls ``sink.send(store_id, payload)`` and deliberately does not swallow an
    ``AttributeError`` from a sink that cannot send, which is why the method is spelled
    exactly that way here.
    """

    def __init__(
        self,
        endpoints: Mapping[str, str],
        *,
        timeout: float = DEFAULT_PUSH_TIMEOUT_SECONDS,
        log: logging.Logger | None = None,
    ) -> None:
        #: The address book, copied. A sink that read a live mapping could be re-addressed
        #: mid-request by whatever owned it, and "which store did this go to" would stop
        #: being answerable from the sink alone.
        self.endpoints: dict[str, str] = {str(k): str(v) for k, v in endpoints.items()}
        #: Deltas a store agent accepted.
        self.delivered = 0
        #: Deltas that did not land, ever, on this sink.
        self.lost = 0
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

    @property
    def delivering(self) -> bool:
        """Whether the last push landed — the condition the two log lines report."""
        return self._delivering

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
            self._failed(
                addressee,
                event_id,
                f"no store-agent endpoint is configured for {addressee!r} "
                f"({ENV_STORE_AGENT_ENDPOINTS} names {sorted(self.endpoints) or 'nothing'})",
            )
            return False

        try:
            response = self._http_client().post(url, json=dict(payload))
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self._failed(addressee, event_id, f"{type(exc).__name__}: {exc}")
            return False
        if response.status_code >= 400:
            self._failed(
                addressee, event_id, f"the store agent answered HTTP {response.status_code}"
            )
            return False
        self._landed()
        return True

    def status(self) -> dict[str, Any]:
        """The whole delivery condition as a mapping — for a health route or an operator.

        Ids and reasons only; no payload and no pseudonym can reach it.
        """
        return {
            "stores": sorted(self.endpoints),
            "delivering": self._delivering,
            "delivered": self.delivered,
            "lost": self.lost,
            "last_failure": self.undelivered[-1][2] if self.undelivered else None,
        }

    def close(self) -> None:
        """Release the pooled client, if one was ever built."""
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    # -- plumbing -------------------------------------------------------------------
    def _failed(self, store_id: str, event_id: str, reason: str) -> None:
        self.undelivered.append((store_id, event_id, reason))
        self.lost += 1
        self._lost_in_this_outage += 1
        if self._delivering:
            self._delivering = False
            self._log.error(
                "trust events stopped reaching store agents (%s, on event %s for store %s). "
                "The event IS in the chained ledger; only the notification did not land. "
                "Ids and reasons are on this sink's undelivered ring and status() now reads "
                "delivering=false. The next delta that lands is logged; the ones in between "
                "are counted",
                reason,
                event_id,
                store_id,
            )

    def _landed(self) -> None:
        self.delivered += 1
        if not self._delivering:
            self._delivering = True
            self._log.info(
                "trust events are reaching store agents again; %d delta(s) did not land "
                "during that outage and no store was told about them",
                self._lost_in_this_outage,
            )
            self._lost_in_this_outage = 0

    def _http_client(self) -> Any:
        """One pooled client for this sink, built on first use.

        Deferred rather than built in ``__init__`` — the convention
        ``proxyshop_support.trust_ledger`` already follows — so that constructing a sink opens
        no sockets and imports no ``httpx``, and a deployment with an empty address book pays
        for neither. It also keeps this package importable by the stdlib-only path
        ``trust.feedback.__init__`` binds its submodules through.
        """
        if self._client is None:
            import httpx  # noqa: PLC0415 - see the docstring

            self._client = httpx.Client(timeout=self._timeout)
        return self._client


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
