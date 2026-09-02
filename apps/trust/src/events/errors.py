"""How the ledger writer service refuses things. Owned by T-060. Standard library only.

Every name here is an *expected* failure of an append: a caller sent something the ledger
cannot accept, or two callers raced. They are separated from the generic ``RuntimeError``
the ledger library raises so that :mod:`.routes` can map each one to the HTTP status that
tells the caller what to do about it, and so that a caller embedding the store in-process
can tell "your event is wrong" (never retry) from "somebody beat you to the tail" (retry).

The module holds nothing but exception classes and imports nothing but ``__future__``. That
is deliberate and load-bearing: ``apps.trust.src.events`` must stay importable with neither
``psycopg`` nor ``fastapi`` installed (the ledger package's own docstring pins that
requirement on T-060), and an errors module that dragged in a driver would break it for
every consumer that only wants :class:`~.store.InMemoryEventStore`.
"""

from __future__ import annotations

__all__ = [
    "BrokenChain",
    "ChainForked",
    "EventServiceError",
    "IdempotencyConflict",
    "InvalidEvent",
    "StoreUnavailable",
    "UnknownEventKind",
]


class EventServiceError(RuntimeError):
    """Base class for every refusal the ledger writer issues."""


class InvalidEvent(EventServiceError):
    """The event cannot be appended as sent, and re-sending it unchanged will not help.

    Missing ``event_id``/``ts``/``kind``, an unparseable timestamp, a ``payload`` that is
    not a JSON object, or a top-level field outside the frozen ``LedgerEvent`` shape.
    """


class UnknownEventKind(InvalidEvent):
    """``kind`` is outside the frozen ``LedgerEvent`` vocabulary (C11/D24).

    A subclass rather than a separate branch because the caller's remedy is the same --
    fix the event -- but the distinction is worth carrying: this is the one shape error
    the database would also have caught, and catching it here is what turns a bare
    ``CheckViolation`` into a message naming the eighteen kinds that exist.
    """


class IdempotencyConflict(EventServiceError):
    """This ``event_id`` is already in the chain carrying **different** content.

    D16 makes ``event_id`` the idempotency key, so re-sending an id must re-send the same
    event. Reporting this as a successful no-op would silently drop a write, which is the
    worst failure an append-only ledger has: the caller has no way to notice.
    """


class ChainForked(EventServiceError):
    """Another writer committed behind the tail this append was linking to.

    The event itself is fine; it was computed against a predecessor that is no longer the
    tail. Retrying is the correct response, which is why this is *not* an
    :class:`InvalidEvent`. It is unreachable through :class:`~.pg.PostgresEventStore` at
    ``READ COMMITTED`` -- the ledger's advisory lock serialises writers -- and exists
    because ``commerce_events_prev_hash_key`` is the backstop underneath that lock and a
    backstop that reported itself as "your event is malformed" would send the caller to fix
    a correct event.
    """


class BrokenChain(EventServiceError):
    """A stream that was supposed to be a chain does not verify.

    Raised where a broken chain is a *precondition failure* rather than a finding -- for
    instance building an :class:`~.store.InMemoryEventStore` on a prefix that is unsealed
    or already tampered. The verification endpoints do not raise this: they report, because
    "the ledger is broken" is an answer, not an error.
    """


class StoreUnavailable(EventServiceError):
    """The service has no ledger to write to.

    No DSN configured and no store injected. Distinguished from every other failure because
    the response is a 503 -- nothing is wrong with the request.
    """
