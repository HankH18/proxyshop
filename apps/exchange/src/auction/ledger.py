"""The ledger seam the exchange writes its state-machine transitions through.

The exchange does **not** own the ledger: ``apps/trust/src/ledger`` does, it is hash-chained
and it is backed by Postgres (D16). What the exchange owns is the decision to record, and a
port narrow enough that recording never becomes a reason for an auction to fail.

:class:`LedgerSink` is that port. :class:`InMemoryLedgerSink` is the trust-API stub the
tests and the default service wiring use — it is a real sink (it keeps every event, in
order, and can be read back) rather than a no-op, so a test that asserts on transitions is
asserting on something that was genuinely emitted.

Three deliberate properties:

* **Events are plain mappings with a `str` kind, validated against the frozen vocabulary.**
  ``kind`` is checked against :data:`contracts.ledger.LEDGER_EVENT_KINDS` at construction,
  so a typo is caught at the producing boundary — where it can still be fixed — rather than
  at the trust service's door. The 18 kinds are frozen by T-010 (D24) and the exchange
  invents none of them.
* **A sink that raises does not take the auction down.** Losing an audit record is bad;
  failing a live auction because the audit sink hiccuped is worse, and the ledger is
  reconstructible from the auction state while the auction is not reconstructible from the
  ledger. :meth:`LedgerRecorder.record` therefore swallows sink failures into
  :attr:`LedgerRecorder.failures` instead of propagating them.
* **Neither does a record that will not validate (T-283).** :func:`build_published_event`
  raises, and goes on raising — a body that is not the published one must never reach the
  trust service pretending to be one. But its two consumers sit in regions that cannot
  absorb a raise (``checkout/provider.py``'s post-mint block and ``accept/offer.py``'s
  ``_refused``, which runs from inside an ``except``), and MEASURED, one missing key turned
  a successfully minted, live, chargeable discount into a refusal handed back to the buyer.
  So the same rule the bullet above states for a sink now holds for the builder: the
  transaction stands, the malformed record is kept in :func:`audit_anomalies` instead of
  being emitted, and the consumer drops **that event** rather than the buyer's code.
"""

from __future__ import annotations

import itertools
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from contracts.ledger import LEDGER_EVENT_KINDS, LEDGER_PAYLOAD_SHAPES, validate_ledger_payload

__all__ = [
    "InMemoryLedgerSink",
    "LedgerRecorder",
    "LedgerSink",
    "MalformedLedgerPayload",
    "UnknownLedgerEventKind",
    "build_event",
    "build_published_event",
    "published_body",
]


class UnknownLedgerEventKind(ValueError):
    """A kind outside the frozen `LedgerEventKind` vocabulary (D24) was produced."""


class MalformedLedgerPayload(ValueError):
    """A payload that does not carry its kind's published body.

    Raised at construction, which is the only place it can still be fixed. It is a
    *programming* error — the producer decides what goes in the body — so it is not
    swallowed the way a sink being down is. Deliberately the same class name and the same
    contract as ``apps/merchant/svc/src/codes/ledger.py``: that service already validates at
    the producing boundary, and a second convention for the same rule is a second thing to
    keep in step.
    """


def published_body(kind: str) -> tuple[str, ...]:
    """The keys ``kind``'s published body carries, straight from ``contracts``."""
    return tuple(LEDGER_PAYLOAD_SHAPES.get(str(kind), ()))


# --- the anomaly channel (T-283) ------------------------------------------------------
#
# NOT added to ``__all__``, and that is deliberate rather than an oversight. The package
# ``exchange/auction/__init__.py`` mirrors this module's ``__all__`` name for name, and
# ``test_repro_ledger_gates.py::test_t282_…`` fails the build the moment the two diverge — so
# a name published here is a two-file change by construction. This seam is not part of the
# package's import surface: it is reached by module path from inside ``apps/exchange`` by the
# two producers that have to survive a refusal, the way ``checkout/provider.py`` already
# publishes ``domain_is_platform_verified`` without listing it.

#: How many malformed-record anomalies one process keeps for an operator to read.
#:
#: The same 512 as ``accept/routes.py``'s ``DEFAULT_BID_BOOK_CAPACITY``, for the same reason
#: and against the same ``apps/exchange/compose.yaml`` ``mem_limit: 256m``: this is written
#: from an unauthenticated path (``POST /auctions/{id}/accept``), and a list with no bound is
#: a memory leak anybody can drive by posting in a loop. The sibling book had to be measured
#: with ``tracemalloc`` because a bid record carries a store's offer; an anomaly carries ids
#: and key NAMES only — never a payload value — so 512 of them is tens of kilobytes.
#:
#: A checkout produces at most three (the C11 trio) and a refusal at most two, so the ring
#: holds every anomaly of ~170 completely broken checkouts. Past that the OLDEST is dropped,
#: which is the right direction for an operator reading a burst, and
#: :attr:`AuditAnomaly.seq` is what says truncation happened rather than quiet.
AUDIT_ANOMALY_CAPACITY = 512


@dataclass(frozen=True)
class AuditAnomaly:
    """One audit record that could not be built, kept because the transaction was not dropped.

    This is the *record of a missing record*. It exists so that "the ledger has no
    ``code_created`` for this checkout" is distinguishable from "no code was ever created",
    which is the only thing that makes dropping the event safer than failing the buyer.

    **What it deliberately does not carry: any payload VALUE.** ``code_created`` bodies hold a
    live single-use discount, and T-215's whole finding is that a live code must not reach a
    place that gets formatted — a log line, a persisted payload, a ``repr`` in a traceback.
    Only the key NAMES are kept, plus whatever redaction-safe join values the consumer passes
    as ``context`` (a fingerprint, a checkout token, an auction id — never the code).
    Reconstructing the body is the consumer's job, from the result it still holds.

    **Stated plainly, the way ``_orphan_record`` states its own limit: nothing consumes this
    yet.** ``apps/exchange`` has no logging call site at all (T-308) and no operator endpoint
    reads this ring, so today it is reachable from a REPL, a test, and any caller that imports
    it. It is the channel that exists rather than the channel that is watched — and it is
    still strictly more than the alternative it replaced, which was destroying a real
    commercial outcome to report a bookkeeping mismatch.
    """

    #: Position in this process's anomaly sequence, from zero. The newest entry's ``seq + 1``
    #: is the total ever recorded, so a full ring — where the oldest retained ``seq`` is not
    #: zero — is distinguishable from a process that simply saw this many.
    seq: int
    #: The producing call site, code-authored (``"CheckoutProvider._events"``), never a name
    #: an adapter or a merchant chose.
    where: str
    #: The frozen kind (D24) whose body would not validate.
    kind: str
    #: :class:`MalformedLedgerPayload`'s own message: which published keys were missing.
    problem: str
    #: What ``contracts`` publishes for :attr:`kind` at the moment the record was refused.
    published: tuple[str, ...]
    #: The keys the producer actually wrote, sorted. Names only.
    written: tuple[str, ...]
    #: Redaction-safe join keys the consumer supplied.
    context: Mapping[str, Any] = field(default_factory=dict)


#: Bounded, and appended to from the fan-out's worker threads as well as the request thread.
#: ``deque.append`` under a ``maxlen`` and ``next()`` on an ``itertools.count`` are single
#: C-level operations, so neither can interleave into a torn write; no lock is taken because
#: the only invariant is "every recorded anomaly is in here or was evicted by a newer one".
_AUDIT_ANOMALIES: deque[AuditAnomaly] = deque(maxlen=AUDIT_ANOMALY_CAPACITY)
_AUDIT_ANOMALY_SEQ = itertools.count()


def record_audit_anomaly(
    where: str,
    kind: str,
    exc: MalformedLedgerPayload,
    *,
    payload: Mapping[str, Any] | None = None,
    **context: Any,
) -> AuditAnomaly:
    """Keep the audit failure, so a consumer can drop the event instead of the transaction.

    Call it from an ``except MalformedLedgerPayload`` around a
    :func:`build_published_event` that sits somewhere a raise would cost a buyer something
    real. ``context`` takes join keys and must already be safe to render (T-215): pass a code
    ``fingerprint``, never the discount itself.
    """
    anomaly = AuditAnomaly(
        seq=next(_AUDIT_ANOMALY_SEQ),
        where=str(where),
        kind=str(kind),
        problem=str(exc),
        published=published_body(kind),
        written=tuple(sorted(str(key) for key in (payload or {}))),
        context={str(key): value for key, value in context.items() if value is not None},
    )
    _AUDIT_ANOMALIES.append(anomaly)
    return anomaly


def audit_anomalies() -> tuple[AuditAnomaly, ...]:
    """Every anomaly this process still holds, oldest first. A snapshot, not the ring."""
    return tuple(_AUDIT_ANOMALIES)


class LedgerSink(Protocol):
    """Anything that accepts a ledger event. The trust API client is one; so is the stub."""

    def emit(self, event: Mapping[str, Any]) -> None: ...


#: Ceiling on the events one process keeps for readback.
#:
#: The sink is a MIRROR. The durable record is the trust service's ledger, which
#: :meth:`exchange.composition.HttpTrustLedgerSink.emit` posts every event to before this list
#: is ever read, so dropping the oldest here costs a convenience read and no audit.
#:
#: Bounded for the reason :class:`InMemoryAuctionStore` and the shortlist store are: ``POST
#: /auctions`` is unauthenticated and records two events per call, so an unbounded list is a
#: memory-growth door open to any caller. MEASURED, the same 3,001-request loop both ways: as
#: shipped, 6,002 events and 3,988 KiB with no ceiling at any N; at this capacity, 2,048
#: events and 811 KiB, flat in N. 2,048 is ~1,024 auctions of history — far more than one
#: shopper's session, which is the only readback anyone drives.
DEFAULT_LEDGER_READBACK_CAPACITY = 2048


class LedgerReadbackEvicted(LookupError):
    """A readback whose honest answer is "I no longer know", rather than an empty list."""


class InMemoryLedgerSink:
    """The trust API stub: keeps the most recent events, in emission order, for readback.

    Bounded by :data:`DEFAULT_LEDGER_READBACK_CAPACITY`. Pass ``capacity=None`` for an
    unbounded sink, and only where a consumer grades the WHOLE stream and a truncated one
    would be a wrong verdict rather than a short answer — ``services/sim``'s runner and the
    e2e S1 flow are the two, and both are CLI harnesses, not served processes. Never in a
    process that answers an unauthenticated request.
    """

    def __init__(self, *, capacity: int | None = DEFAULT_LEDGER_READBACK_CAPACITY) -> None:
        self.capacity = None if capacity is None else max(1, int(capacity))
        self._ring: deque[dict[str, Any]] = deque(maxlen=self.capacity)
        #: How many events have fallen out of the ring. The witness that keeps an empty
        #: readback distinguishable from an auction that emitted nothing.
        self.evicted = 0

    @property
    def events(self) -> list[dict[str, Any]]:
        """Every event still held, oldest first. A list, as it has always been.

        A fresh COPY on each read, so mutating it changes nothing here -- write through
        :meth:`emit`, or, in a subclass that needs to splice a row in, through ``self._ring``.
        Worth stating because a test spy that had been doing ``sink.events.append(...)``
        against the old attribute went on running and silently stored nothing.
        """
        return list(self._ring)

    def emit(self, event: Mapping[str, Any]) -> None:
        if self._ring.maxlen is not None and len(self._ring) == self._ring.maxlen:
            self.evicted += 1
        self._ring.append(dict(event))

    @property
    def kinds(self) -> list[str]:
        return [str(event["kind"]) for event in self._ring]

    def for_auction(self, auction_id: str) -> list[dict[str, Any]]:
        """This auction's events, or a refusal where an empty answer would be a lie.

        Before the ring, an evicted auction and an auction that emitted nothing were the same
        ``[]``, and no caller could tell them apart from the value. A sink that has evicted
        NOTHING still answers ``[]`` for an unknown id -- so a fresh sink is unchanged, and
        the refusal cannot fire on a process that has not yet overflowed.
        """
        rows = [event for event in self._ring if event.get("auction_id") == auction_id]
        if not rows and self.evicted:
            raise LedgerReadbackEvicted(
                f"{self.evicted} event(s) have been recorded past this process's "
                f"{self.capacity}-event readback ring, so an empty answer for "
                f"{auction_id!r} cannot be distinguished from an auction whose events were "
                f"evicted. The durable record is the trust service's ledger."
            )
        return rows


def build_event(
    kind: str,
    *,
    auction_id: str | None = None,
    store_id: str | None = None,
    order_ref: str | None = None,
    payload: Mapping[str, Any] | None = None,
    ts: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build one `LedgerEvent`-shaped mapping, refusing a kind outside the frozen enum."""
    if kind not in LEDGER_EVENT_KINDS:
        raise UnknownLedgerEventKind(
            f"{kind!r} is not one of the frozen LedgerEvent kinds (D24); "
            f"the exchange may not invent a kind"
        )
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "ts": ts or datetime.now(UTC).isoformat(),
        "kind": kind,
        "auction_id": auction_id,
        "store_id": store_id,
        "order_ref": order_ref,
        "payload": dict(payload or {}),
    }


def build_published_event(kind: str, **fields: Any) -> dict[str, Any]:
    """:func:`build_event`, and the body must be the one ``contracts`` publishes for ``kind``.

    ``build_event`` checks only that the *kind* is in the frozen vocabulary — which is how
    one kind came to be emitted with two entirely different bodies (T-235): the successful
    checkout wrote ``{checkout_token, discount_code}`` under ``code_created`` while
    ``contracts.ledger.LEDGER_PAYLOAD_SHAPES`` publishes ``(code, permalink_url,
    expires_at)``, so a consumer reading ``payload['code']`` off a success got nothing and
    nothing anywhere raised. ``contracts/src/ledger.py`` says the check belongs "at the
    PRODUCING boundary"; this is the exchange's half of that, and
    ``apps/merchant/svc/src/codes/ledger.py`` is the in-repo convention it copies.

    Extra keys are welcome and are what make an event actionable rather than merely
    well-formed (the orphan record's ``revocation_required`` is one); a *missing* published
    key is refused.

    This is a **separate entry point** rather than a check folded into :func:`build_event`
    on purpose, and the reason has outlived the two examples it used to name. Those were
    ``auction_opened`` without ``roster_size`` and ``auction_closed`` without
    ``shortlist_size``; both now carry their published bodies (T-302 —
    :meth:`~.state.AuctionStateMachine.open` reads the size off the record it just wrote, and
    ``close`` takes the shortlist's from the caller that ranked). What remains true is the
    rule: :meth:`LedgerRecorder.record` is the audit trail of a live auction, and an exception
    raised there fails the auction for a bookkeeping defect. So validation is offered at a
    separate door that a producer opts into, and every producer that can absorb a raise does.

    ``accepted`` used to be listed beside them and was the one that did **not** belong there,
    because its two missing keys are not an audit blemish. ``apps/trust/src/reconcile/engine.py``
    builds a checkout's join keys from ``payload['checkout_token']`` first and drops any event
    carrying none, so an ``accepted`` event without it can never share a group with its
    ``order_paid`` webhook: the order reconciles to nothing at all, and ``offer`` is the
    promise the webhook would have been graded against. Both keys are now written by
    :meth:`AuctionStateMachine.accept`. The other two remain what this paragraph says they are.

    Raises:
        UnknownLedgerEventKind: the kind is not in the frozen vocabulary.
        MalformedLedgerPayload: the body is missing a key its kind publishes.
    """
    event = build_event(kind, **fields)
    problems = validate_ledger_payload(kind, event["payload"])
    if problems:
        raise MalformedLedgerPayload(
            f"refusing to emit a {kind!r} event whose body is not the published one "
            f"{published_body(kind)}: {'; '.join(problems)}"
        )
    return event


class LedgerRecorder:
    """Emits events to a sink and remembers the ones the sink refused.

    An unknown kind is a **programming** error and still raises: it is caught before the
    event leaves this process, and silently dropping it would hide the bug from the only
    person who can fix it. A sink that is merely *down* is an operational error and is
    recorded in :attr:`failures`.
    """

    def __init__(self, sink: LedgerSink | None = None) -> None:
        self.sink: LedgerSink = sink if sink is not None else InMemoryLedgerSink()
        self.failures: list[tuple[dict[str, Any], str]] = []

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = build_event(kind, **fields)
        try:
            self.sink.emit(event)
        except Exception as exc:  # the audit trail must not be able to fail the auction
            self.failures.append((event, f"{type(exc).__name__}: {exc}"))
        return event
