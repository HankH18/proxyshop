"""The ledger seam the exchange writes its state-machine transitions through.

The exchange does **not** own the ledger: ``apps/trust/src/ledger`` does, it is hash-chained
and it is backed by Postgres (D16). What the exchange owns is the decision to record, and a
port narrow enough that recording never becomes a reason for an auction to fail.

:class:`LedgerSink` is that port. :class:`InMemoryLedgerSink` is the trust-API stub the
tests and the default service wiring use — it is a real sink (it keeps every event, in
order, and can be read back) rather than a no-op, so a test that asserts on transitions is
asserting on something that was genuinely emitted.

Two deliberate properties:

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
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from contracts.ledger import LEDGER_EVENT_KINDS

__all__ = [
    "InMemoryLedgerSink",
    "LedgerRecorder",
    "LedgerSink",
    "UnknownLedgerEventKind",
    "build_event",
]


class UnknownLedgerEventKind(ValueError):
    """A kind outside the frozen `LedgerEventKind` vocabulary (D24) was produced."""


class LedgerSink(Protocol):
    """Anything that accepts a ledger event. The trust API client is one; so is the stub."""

    def emit(self, event: Mapping[str, Any]) -> None: ...


class InMemoryLedgerSink:
    """The trust API stub: keeps every event, in emission order, and can be read back."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))

    @property
    def kinds(self) -> list[str]:
        return [str(event["kind"]) for event in self.events]

    def for_auction(self, auction_id: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("auction_id") == auction_id]


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
