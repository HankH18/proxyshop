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
    on purpose. Two of the state machine's own transitions still do not carry their published
    bodies (``auction_opened`` omits ``roster_size``, ``auction_closed`` omits
    ``shortlist_size``), and turning those into exceptions would fail live auctions for an
    audit-record defect that is nobody's ticket here. They are reported, not silently swept in.

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
