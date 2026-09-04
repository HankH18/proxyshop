"""The merchant's producing boundary for ledger events — where a malformed body is refused.

``packages/contracts/src/ledger.py`` publishes one body per kind and says where the check
belongs: "callable at the PRODUCING boundary where a malformed body can still be refused".
This module is that boundary for everything ``merchant_svc.codes`` emits, and
:func:`build_event` runs :func:`~contracts.ledger.validate_ledger_payload` on every event
before it exists. There is no path here that emits an unvalidated body.

**Why that is written down rather than assumed.** T-235 is the measured counterexample, and
it is one function call away from this code: ``apps/exchange/src/checkout/provider.py:983``
emits a ``code_created`` carrying ``{"checkout_token", "discount_code"}`` while
``ledger.py:63`` publishes ``("code", "permalink_url", "expires_at")`` — none of the three.
The orphan path at ``apps/exchange/src/accept/offer.py:294-303`` writes the published body
and says so in a comment. One kind, two bodies, because nothing on that path validates. A
consumer reading ``payload["code"]`` off a successful ``code_created`` gets nothing, which
is precisely the consumer that would revoke a live discount. This package emits the
**published** body, and validating is what keeps that true after the next edit rather than
only today.

The store itself is process-local, exactly like ``merchant_svc.envelope.ENVELOPES`` and
``merchant_svc.collector.PIXEL_INBOX``: a real append-only structure with the real
invariants, so the rules are written and tested here rather than discovered later inside a
migration. Losing it on a restart can only cost history, never a guarantee — the redemption
register is what enforces single use.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from contracts.ledger import LEDGER_EVENT_KINDS, LEDGER_PAYLOAD_SHAPES, validate_ledger_payload
from contracts.protocol import LedgerEvent

__all__ = [
    "CODE_LEDGER",
    "CodeLedger",
    "MalformedLedgerPayload",
    "UnknownLedgerEventKind",
    "build_event",
    "published_body",
]


class UnknownLedgerEventKind(ValueError):
    """A kind outside the frozen `LedgerEventKind` vocabulary (D24) was produced."""


class MalformedLedgerPayload(ValueError):
    """A payload that does not carry its kind's published body.

    Raised at construction, which is the only place it can still be fixed. It is a
    *programming* error — the producer decides what goes in the body — so it is not
    swallowed the way a sink being down is.
    """


def published_body(kind: str) -> tuple[str, ...]:
    """The keys ``kind``'s published body carries, straight from ``contracts``."""
    return tuple(LEDGER_PAYLOAD_SHAPES.get(str(kind), ()))


def build_event(
    kind: str,
    *,
    payload: Mapping[str, Any],
    auction_id: str | None = None,
    store_id: str | None = None,
    order_ref: str | None = None,
    ts: datetime | str | None = None,
    event_id: str | None = None,
) -> LedgerEvent:
    """Build one validated :class:`~contracts.protocol.LedgerEvent`.

    Args:
        kind: one of the 18 frozen kinds. The merchant invents none.
        payload: the body. It must carry every key its kind publishes; extras are welcome
            (a vendor body carries plenty) and are what make an event actionable rather
            than merely well-formed.
        auction_id, store_id, order_ref: the top-level correlation fields.
        ts: the instant to record. A ``datetime`` is rendered ISO-8601; left unset, the
            wall clock is read — so a caller that has been *given* a reference instant
            should pass it, and every caller in this package does.
        event_id: an explicit id, for a caller that needs the event to be reproducible.

    Raises:
        UnknownLedgerEventKind: the kind is not in the frozen vocabulary.
        MalformedLedgerPayload: the body is missing a key its kind publishes.
    """
    if str(kind) not in LEDGER_EVENT_KINDS:
        raise UnknownLedgerEventKind(
            f"{kind!r} is not one of the frozen LedgerEvent kinds (D24); the merchant "
            f"service may not invent a kind"
        )
    body = dict(payload)
    problems = validate_ledger_payload(kind, body)
    if problems:
        raise MalformedLedgerPayload(
            f"refusing to emit a {kind!r} event whose body is not the published one "
            f"{published_body(kind)}: {'; '.join(problems)}"
        )
    moment = ts if isinstance(ts, str) else (ts or datetime.now(UTC)).isoformat()
    return LedgerEvent(
        event_id=event_id or str(uuid.uuid4()),
        ts=moment,
        kind=str(kind),  # type: ignore[arg-type]
        auction_id=auction_id,
        store_id=store_id,
        order_ref=order_ref,
        payload=body,
    )


class CodeLedger:
    """Every code event this service produced, in order, nothing ever rewritten."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[LedgerEvent] = []

    def emit(self, event: LedgerEvent) -> LedgerEvent:
        """Append one event. Never raises for an event :func:`build_event` produced."""
        with self._lock:
            self._events.append(event)
        return event

    def events(self) -> tuple[LedgerEvent, ...]:
        """Every event recorded, oldest first."""
        with self._lock:
            return tuple(self._events)

    def kinds(self) -> tuple[str, ...]:
        """The kinds recorded, in order — the cheap thing a test wants to assert on."""
        return tuple(str(event.kind.value) for event in self.events())

    def for_code(self, code: str) -> tuple[LedgerEvent, ...]:
        """Every event whose body names ``code``, in order."""
        wanted = str(code)
        return tuple(
            event
            for event in self.events()
            if str(event.payload.get("code") or "") == wanted
            or str(event.payload.get("discount_code") or "") == wanted
        )

    def clear(self) -> None:
        """Forget everything. For tests only — a ledger a caller can empty is not a ledger."""
        with self._lock:
            self._events.clear()


#: The service-wide record. Process-local, exactly like the envelope history and the pixel
#: inbox; see the module docstring for why that is enough here.
CODE_LEDGER = CodeLedger()
