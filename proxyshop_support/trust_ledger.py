"""Writing ledger events into the trust service's published ``POST /events`` (T-150).

This module owns ONE mechanism: how a ledger event gets from the process that produced it
into ``apps/trust``'s hash-chained ledger, and how a service says so when it cannot. It owns
no event shapes — ``contracts.ledger`` owns those — and it knows nothing about auctions,
discount codes or webhooks, so any service with events to append can adopt it.

WHY IT LIVES HERE AND NOT IN ``packages/contracts``
---------------------------------------------------
Both homes were arguable and the choice is deliberate, so it is written down rather than
left to the next reader to re-litigate.

``packages/contracts`` is the **shape** package: the frozen ``LedgerEventKind`` vocabulary,
the per-kind payload shapes, the OpenAPI documents, the signing rules, the code generator.
Nothing in it opens a socket, and it is imported by every service AND by ``codegen`` — so
putting the first ``httpx``-holding class in there would mean that reading a constant about
what an event *is* drags in a transport for moving one. The protocol package describing a
door is not the same thing as the client that walks through it.

``proxyshop_support`` is where cross-cutting **runtime machinery** that belongs to no feature
package already lives — the Redis client wrapper (D39), the Postgres helpers, the ASGI
server, ``logging_config`` (T-308), the reachability probes. An outbound client for a
service every other service has to reach is exactly that shape. It also has the property the
job actually requires: ``proxyshop_support`` depends on no service package, and
``apps/merchant/svc/src/main.py`` and ``apps/buyer/devstack/run.py`` already import from it —
so merchant and buyer can adopt this module without either of them growing a dependency on
the exchange, which is what would have happened if the mechanism had stayed where it was
written (a private class in ``apps/exchange/src/composition.py``).

WHAT WAS MEASURED, AND WHY THIS IS SHARED AT ALL
------------------------------------------------
Nothing in this repository POSTed to the trust service over HTTP before T-150; the
exchange's sink was the first. Every other ledger "sink" is in-process and drains nowhere —
the exchange's own ``InMemoryLedgerSink`` is a list discarded with the app, and merchant's
``HANDOFF`` (``install/webhooks.py``) is a ring buffer whose ``default_sink`` docstring says
it exists because "E6 is not deployed yet". There is no shared HTTP-client convention
either: ``HttpBidSolicitor`` (exchange), ``HttpExchangeClient`` (buyer) and this were three
hand-rolled clients, each private to one service's composition root. Leaving the FIRST
cross-process ledger writer private to the exchange would have guaranteed a fourth when
merchant's ring finally needs draining, and two spellings of "did the audit trail land".

**Adopting it from merchant** takes no change here: build one
:class:`TrustLedgerPublisher` at merchant's composition root, hand it merchant's logger and
subject, and call :meth:`TrustLedgerPublisher.publish` where ``HANDOFF.append`` is drained.
Merchant's ring keeps its in-process readback exactly as the exchange's sink keeps its own.

THE POSTURE ON A TRUST SERVICE THAT IS NOT ANSWERING
-----------------------------------------------------
The trust ledger is REQUIRED architecture, not an optional integration, and this module
takes that position in three places at once:

* **Failure never propagates.** Losing an audit record is bad; failing a live auction or a
  live checkout because the audit sink hiccuped is worse, and every transport failure mode
  of an outbound POST is an operational problem rather than a reason to refuse a buyer.
* **Failure is never silent, and it is never chatty.** Delivery health is a *state*, so it is
  reported on the two state CHANGES and nowhere else: one ``ERROR`` the moment events stop
  landing, one ``INFO`` the moment they land again. A steady state — healthy or broken —
  writes nothing at all. This replaces the per-sink one-shot ``WARNING`` T-150 shipped, which
  said "the auction stands but its transition is not in the chained ledger, and further
  failures are counted, not logged": that is the posture of a system still deciding whether
  its audit trail matters, it under-reports (an outage that ends and returns is reported
  once, ever) and ``WARNING`` is the wrong level for a required write that is not happening.
* **Failure stays READABLE after the log line scrolls away.** :attr:`undelivered` keeps the
  ids and reasons, :meth:`status` renders the whole condition as a mapping a health route or
  an operator can read, and neither ever carries a payload.

**The one case that is deliberately allowed to be loud**: a trust service that ALTERNATES —
answering, timing out, answering — reports a line per change, so a pathologically flapping
endpoint can approach one line per event. That is a considered trade and not an oversight.
Flapping is a different and worse condition than being down (writes are being lost while
every health check that samples it says "fine"), it is not a state a healthy deployment
reaches, and the volume is bounded by traffic rather than hidden by a timer. A caller who
wants the condition without any log line at all reads :meth:`TrustLedgerPublisher.status`,
which is live whether or not anything was written down.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "DEFAULT_LEDGER_TIMEOUT_SECONDS",
    "DEFAULT_TRUST_URL",
    "ENV_TRUST_URL",
    "MAX_UNDELIVERED_LEDGER_EVENTS",
    "SOURCE_DEFAULT",
    "SOURCE_ENVIRONMENT",
    "SOURCE_STATED",
    "TRUST_EVENTS_PATH",
    "TrustLedgerPublisher",
    "describe_failure_plainly",
    "trust_endpoint",
    "trust_events_url",
]

#: Where the trust service answers when neither the caller nor the environment says.
#:
#: ``trust:8084`` is the compose service name and the published port — ``apps/trust/
#: compose.yaml`` binds ``${TRUST_PORT:-8084}:8084`` and runs ``uvicorn trust.main:app
#: --port 8084``, and ``apps/buyer/compose.yaml`` already spells this exact URL for the same
#: service. A DEFAULT rather than a required setting on purpose: a service that has to be
#: told where to write its audit trail before it writes one is a service that ships not
#: writing one, which is the whole of T-150. Outside compose the name does not resolve, every
#: publish fails, and :class:`TrustLedgerPublisher` reports that instead of raising.
DEFAULT_TRUST_URL = "http://trust:8084"

#: The environment variable that overrides :data:`DEFAULT_TRUST_URL`. Same spelling as the
#: one ``apps/buyer/compose.yaml`` already forwards, so one variable names the service
#: everywhere.
ENV_TRUST_URL = "TRUST_URL"

#: The published append door: ``apps/trust/src/events/routes.py``'s router carries
#: ``prefix="/events"`` and its ``POST ""`` is "Append one ledger event". Appended in exactly
#: one place — :func:`trust_endpoint` — so no two callers can disagree about it.
TRUST_EVENTS_PATH = "/events"

#: The three answers :func:`trust_endpoint` gives for *where the address came from*. Reported
#: at wiring time, because "which trust service is this process writing to, and did anyone
#: choose it" is a configuration question and belongs where the operator is looking.
SOURCE_STATED = "stated"
SOURCE_ENVIRONMENT = "environment"
SOURCE_DEFAULT = "default"

#: How long ONE ledger write may hold the calling thread.
#:
#: Publishing is synchronous and runs inline on a request path: the exchange writes two
#: events in ``POST /auctions`` (``auction_opened``, ``auction_closed``) and a third in
#: ``POST /auctions/{id}/accept``, so this number is multiplied by three in the worst case a
#: buyer can feel and 0.5s bounds that at 1.5s. A ledger append is a small POST to a service
#: on the same network, and one that has not answered in half a second is a service that is
#: down, not a service that is thinking.
DEFAULT_LEDGER_TIMEOUT_SECONDS = 0.5

#: How many undelivered events one publisher remembers for an operator to read.
#:
#: A ring, not a list: this is driven from unauthenticated request paths (``POST /auctions``
#: is one), so an unbounded record of failures against a trust service that is down is a
#: memory leak anybody can fill by posting in a loop, against ``apps/exchange/
#: compose.yaml``'s ``mem_limit: 256m``. Only the event id and the reason are kept — never
#: the body.
MAX_UNDELIVERED_LEDGER_EVENTS = 512


def describe_failure_plainly(exc: BaseException) -> str:
    """``"ConnectError: nodename nor servname provided"`` — the class name, then the message.

    The default renderer for :attr:`TrustLedgerPublisher.undelivered`. It is a *seam* and not
    a fixed rule because a caller may have a stricter one it is already held to: the exchange
    passes ``exchange.describe_exception``, which additionally collapses default ``__repr__``
    addresses out of the message (T-264) — ``str(KeyError(obj))`` simply **is** ``repr(obj)``,
    so a collaborator's own exception can carry an address nothing quoted. A service without
    such a rule gets this, which is still never a payload and never a traceback.
    """
    return f"{type(exc).__name__}: {exc}"


def trust_endpoint(
    base_url: str | None = None, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """``(url, source)`` for the trust service's append door.

    ``base_url`` is what the caller's deployment stated; empty or ``None`` means it stated
    nothing, and then :data:`ENV_TRUST_URL` decides, and then :data:`DEFAULT_TRUST_URL`.
    ``source`` is one of :data:`SOURCE_STATED`, :data:`SOURCE_ENVIRONMENT`,
    :data:`SOURCE_DEFAULT` — the half a wiring-time log line needs and a bare URL cannot say.
    """
    environ = os.environ if env is None else env
    stated = str(base_url or "").strip()
    if stated:
        base, source = stated, SOURCE_STATED
    else:
        from_environment = str(environ.get(ENV_TRUST_URL) or "").strip()
        if from_environment:
            base, source = from_environment, SOURCE_ENVIRONMENT
        else:
            base, source = DEFAULT_TRUST_URL, SOURCE_DEFAULT
    return f"{base.rstrip('/')}{TRUST_EVENTS_PATH}", source


def trust_events_url(base_url: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """:func:`trust_endpoint`'s URL alone, for a caller that does not report its source."""
    return trust_endpoint(base_url, env)[0]


class TrustLedgerPublisher:
    """POSTs one ledger event at a time to the trust service, and keeps score.

    Not a ``LedgerSink`` and deliberately not shaped like one: a sink is one service's port
    (the exchange's takes a ``Mapping`` and returns nothing), while this is the transport
    under it. The exchange's ``HttpTrustLedgerSink`` is an ``InMemoryLedgerSink`` that is
    ALSO one of these, so its in-process readback keeps working — three live tests read
    ``app.state.auction_machine.ledger.sink.kinds`` off the served app — while the mechanism
    is this module's. Merchant's ring buffer would compose one instead of inheriting; both
    reach the same door the same way, which is the point.

    :meth:`publish` never raises and returns whether the event landed.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_LEDGER_TIMEOUT_SECONDS,
        describe_failure: Callable[[BaseException], str] = describe_failure_plainly,
        log: logging.Logger | None = None,
        subject: str = "trust ledger",
    ) -> None:
        self.url = str(url)
        #: Events the chained ledger took. A ``409`` does NOT count: the door answers that for
        #: an id it already holds, which is once-only landing working rather than a delivery,
        #: and an operator reading this wants the number the ledger actually took.
        self.delivered = 0
        #: Events that did not land, ever, on this publisher.
        self.lost = 0
        #: ``(event_id, reason)`` for the events that did not land, newest last. Ids and
        #: reasons only — never a payload, which is the rule ``AuditAnomaly`` follows too.
        self.undelivered: deque[tuple[str, str]] = deque(maxlen=MAX_UNDELIVERED_LEDGER_EVENTS)
        self._timeout = float(timeout)
        self._describe_failure = describe_failure
        self._log = log if log is not None else logging.getLogger(__name__)
        self._subject = str(subject)
        self._client: Any | None = None
        # Starts True so that a process whose every publish succeeds says nothing at all. The
        # first failure is a state CHANGE and is reported; the hundredth is not.
        self._delivering = True
        self._lost_in_this_outage = 0

    @property
    def delivering(self) -> bool:
        """Whether the last publish landed — the condition the two log lines report."""
        return self._delivering

    def publish(self, event: Mapping[str, Any]) -> bool:
        """POST one event. Returns ``True`` if the chained ledger took it.

        Never raises. Every failure mode of an outbound POST — DNS, connect, timeout, a proxy
        answering something unparseable — is an operational problem with the audit trail and
        none of them is a reason to refuse the request that produced the event, so the catch
        is deliberately blanket rather than a list of ``httpx`` exception classes.
        """
        try:
            response = self._http_client().post(self.url, json=dict(event))
        except Exception as exc:
            self._failed(event, self._describe_failure(exc))
            return False
        if response.status_code >= 400:
            self._failed(event, f"trust answered HTTP {response.status_code}")
            return False
        self._landed()
        return True

    def status(self) -> dict[str, Any]:
        """The whole delivery condition as a mapping — for a health route or an operator.

        This is the surface that keeps the removed per-event warning from becoming silence:
        the log reports the two transitions, and this reports the standing state at any
        moment after them. Ids and reasons only; no payload can reach it.
        """
        return {
            "url": self.url,
            "delivering": self._delivering,
            "delivered": self.delivered,
            "lost": self.lost,
            "last_failure": self.undelivered[-1][1] if self.undelivered else None,
        }

    # -- plumbing -------------------------------------------------------------------
    def _failed(self, event: Mapping[str, Any], reason: str) -> None:
        self.undelivered.append((str(event.get("event_id")), reason))
        self.lost += 1
        self._lost_in_this_outage += 1
        if self._delivering:
            self._delivering = False
            self._log.error(
                "%s: %s stopped accepting ledger events (%s, on a %s event). Events produced "
                "from here are NOT in the chained ledger; ids and reasons are on this "
                "publisher's undelivered ring and status() now reads delivering=false. The "
                "next event that lands is logged; the ones in between are counted",
                self._subject,
                self.url,
                reason,
                str(event.get("kind")),
            )

    def _landed(self) -> None:
        self.delivered += 1
        if not self._delivering:
            self._delivering = True
            self._log.info(
                "%s: %s is accepting ledger events again; %d event(s) did not land during that "
                "outage and are not in the chained ledger",
                self._subject,
                self.url,
                self._lost_in_this_outage,
            )
            self._lost_in_this_outage = 0

    def _http_client(self) -> Any:
        """One pooled client for this publisher, built on first use.

        Deferred rather than built in ``__init__`` — the convention ``HttpBidSolicitor``
        already follows — so that composing a service opens no sockets and imports no
        ``httpx``, and a test that never publishes pays for neither.
        """
        if self._client is None:
            import httpx  # noqa: PLC0415 — see the docstring

            self._client = httpx.Client(timeout=self._timeout)
        return self._client
