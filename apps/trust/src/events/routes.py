"""HTTP surface of the ledger writer. Owned by T-060.

Mounted by the frozen entrypoint ``apps/trust/src/main.py``, which globs
``apps/trust/src/*/routes.py`` and includes the module-level :data:`router` it finds here.

===========================  =========================================================
``POST /events``             append one ``LedgerEvent``; ``201`` when it lands, ``200``
                             when the ``event_id`` was already in the chain.
``GET  /events``             the chain in insertion order (filterable, for projection),
                             **paged** -- see :data:`DEFAULT_EVENT_PAGE`.
``GET  /events/head``        the stored head and the anchored length.
``GET  /events/verify``      links **and** anchor, with the broken link named.
``GET  /events/replay``      the whole chain replayed out of the ledger, with the
                             recomputed stream hash. This is the S3 evidence endpoint.
                             Verification covers every event; the events it *serialises*
                             are paged like ``GET /events``.
``GET  /events/{event_id}``  one event by its idempotency key (one index probe).
===========================  =========================================================

Two decisions worth stating, because both are load-bearing:

**The handlers are ``def``, not ``async def``.** Starlette runs a synchronous handler in a
worker thread, so N concurrent POSTs are N concurrent database writers. Written
``async def``, the only DB driver installed here (psycopg 3, synchronous) would block the
event loop and the requests would serialise -- and a serialised "concurrent duplicate" test
proves nothing at all, while looking exactly like one that does.

**A duplicate is ``200``, not ``409``.** ``409`` is reserved for the genuinely broken case:
the same ``event_id`` carrying *different* content. Collapsing the two would leave a caller
retrying a delivery it already made unable to tell "you already sent this, all is well"
from "you have two different events under one id", which is the difference between a
successful retry and silent data loss.

Nothing on this router is authenticated
---------------------------------------
There is not one ``Depends`` in ``apps/trust``, and ``apps/trust/compose.yaml`` publishes the
port. Every bound below therefore has to hold against a caller who has supplied no
credential and is not going to stop:

* **What one request may send** -- :data:`MAX_EVENT_BODY_BYTES`, enforced by
  :class:`_BoundedBodyRoute` while the body is still arriving, plus
  :data:`MAX_IDENTIFIER_LENGTH` on the four identifiers the caller chooses (T-366). The
  ledger is append-only and its ``BEFORE UPDATE OR DELETE ... ENABLE ALWAYS`` trigger makes
  eviction impossible, so a byte admitted here is a byte kept forever.
* **What CHARACTERS those identifiers may carry** -- :func:`_refuse_unrenderable_identifier`.
  Length was bounded and the character set was not, and ``Location`` is built by
  interpolating ``event_id``: an ``event_id`` outside Latin-1 raised ``UnicodeEncodeError``
  *after* the row had committed, and one carrying CR/LF or NUL made uvicorn drop the
  connection with no response at all -- both after the un-evictable row was already written.
* **What one request may make a LATER request cost** -- :func:`_unreplayable_field`. A
  payload naming a ``dim`` and a ``type`` is projected into a trust observation by
  ``replay?snapshots=true``; a ``dim``, ``type``, ``weight`` or ``observed_at`` the scorer
  cannot interpret made that endpoint raise out of the scorer forever, because the row
  cannot be evicted. 148 bytes, unauthenticated, permanent.
* **What one request may cost to answer** -- every whole-chain read pages through
  :data:`~.store.LEDGER_SCAN_CHUNK`, every response is bounded by
  :data:`~.store.MAX_RESPONSE_EVENT_BYTES` as well as by ``limit``, and the one fold that is
  proportional to the ledger rather than to a page (``replay?snapshots=true``) refuses past
  :data:`MAX_SNAPSHOT_REPLAY_EVENTS` (T-364).

What a refusal here looks like
------------------------------
Every refusal carries ``{"error", "message"}`` and **never the offending input**: a refusal
that quotes what it refused is an amplifier with better manners.

It is **not** true that every failure here is a 4xx, and this paragraph used to claim it
was. :func:`_refuse` maps :class:`~.errors.StoreUnavailable` and
:class:`~.errors.ChainForked` to ``503`` and :class:`~.errors.BrokenChain` to ``500`` on
purpose -- those say something about the ledger, not about the request. What IS true, and
what the sentence was reaching for, is the narrower claim worth holding: **no input a
caller can send may produce a 5xx.** That claim was measured false twice (the two bullets
above), which is why it is now a claim with gates behind it rather than a comment.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from .errors import (
    BrokenChain,
    ChainForked,
    EventServiceError,
    IdempotencyConflict,
    InvalidEvent,
    StoreUnavailable,
    UnknownEventKind,
)
from .store import LEDGER_EVENT_KINDS, append, normalise_event

__all__ = [
    "BOUNDED_IDENTIFIERS",
    "DEFAULT_EVENT_PAGE",
    "MAX_EVENT_BODY_BYTES",
    "MAX_EVENT_PAGE",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_SNAPSHOT_REPLAY_EVENTS",
    "TRUST_EVENT_SINK_ATTR",
    "UNRENDERABLE_IDENTIFIER_CHARACTERS",
    "EventIn",
    "notification_report",
    "router",
    "store_for",
    "trust_event_sink",
]

#: How many events a read returns when the caller does not say. **A default is not a
#: nicety here, it is the only cap that binds.** ``limit`` was previously
#: ``Query(None, ge=1, le=10_000)``, which reads like a ceiling and is not one: the
#: validator only runs on a value that was *supplied*, so the ten-thousand-row limit
#: constrained exactly the callers who had already chosen to be polite, and an
#: unparameterised ``GET /events`` serialised the entire append-only history into one
#: response body. On a ledger that only grows, that is unbounded server memory and
#: unbounded bytes on the wire, reachable by anyone who can open the port and costing them
#: one request.
#:
#: A thousand is chosen to be far above any interactive page and far below "the ledger":
#: every ordinary caller sees a complete, untruncated answer (``truncated: false``), and
#: the ones that genuinely want history walk it with ``after_seq`` -- which the response
#: hands back as ``next_after_seq`` so paging needs no arithmetic on the client's part.
DEFAULT_EVENT_PAGE = 1_000

#: The most a caller may ask for in one response, even explicitly. Unchanged from the
#: ceiling that was already declared; what changed is that it is no longer the *only* one.
#:
#: It bounds ROWS, which is not the same as bounding bytes -- see
#: :data:`~.store.MAX_RESPONSE_EVENT_BYTES`, which bounds the other half.
MAX_EVENT_PAGE = 10_000

#: The most one ``POST /events`` body may weigh, refused while it is still ARRIVING.
#:
#: There was no cap of any kind (T-366). ``payload`` is ``dict[str, Any]``, checked only for
#: being a JSON object, and written verbatim into ``jsonb``; measured, single payloads of
#: 1M, 10M and 50M characters all returned ``201``, and three of them took the table to
#: 77 MB. What makes that permanent rather than merely rude is the schema: an append-only
#: ``BEFORE UPDATE OR DELETE`` trigger declared ``ENABLE ALWAYS`` means nothing can evict a
#: row, ``TRUNCATE`` is refused by a foreign key, and the growth measured linear at ~34 KB
#: of table per anonymous request.
#:
#: 64 KiB is the tighter of the two body caps this repo already ships on unauthenticated
#: doors -- ``exchange.policy.routes.DEFAULT_MAX_OUTCOME_BYTES`` and
#: ``buyer.composition.MAX_DEPLOYMENT_BYTES`` are both 64 KiB, while
#: ``exchange.external_bids.routes.MAX_SUBMISSION_BYTES`` is 256 KiB. The tighter one,
#: deliberately: those doors' bytes are transient and these are not. A ``LedgerEvent`` is a
#: fact about one commerce action; 64 KiB is orders of magnitude above every kind in
#: :data:`~.store.LEDGER_EVENT_KINDS` and far below "somebody is using the ledger as a disk".
MAX_EVENT_BODY_BYTES = 64 * 1024

#: The ceiling on a caller-chosen identifier: ``event_id``, ``auction_id``, ``store_id``,
#: ``order_ref`` and the ``Idempotency-Key`` header.
#:
#: The same number ``exchange.auction.routes.MAX_IDENTIFIER_LENGTH`` already holds for "a
#: string some caller picked", restated rather than imported because ``trust`` and
#: ``exchange`` are separate deployables and an import edge between two services to share a
#: constant costs more than the duplication does.
#:
#: Not decoration: all four columns are INDEXED (``commerce_events_idempotency_key_key`` is
#: UNIQUE), so past roughly 2,691 characters Postgres refuses the row with
#: ``ProgramLimitExceeded`` -- and ``psycopg.errors.ProgramLimitExceeded`` is a subclass of
#: ``OperationalError``, so :func:`~.pg.classify_connection_error` turned a malformed
#: unauthenticated request into ``503 store_unavailable``: a 5xx, blaming the datastore for
#: the caller's input. Between 128 and 2,691 characters the row was simply accepted, forever.
MAX_IDENTIFIER_LENGTH = 128

#: The identifiers this door bounds. Every one of them is chosen by the caller, indexed by
#: the ledger, and interpolated into at least one refusal message.
BOUNDED_IDENTIFIERS = ("event_id", "auction_id", "store_id", "order_ref")

#: Characters a caller-chosen identifier may not carry, because a response header cannot
#: carry them: the C0 controls (which includes CR, LF and NUL), DEL, and the C1 controls.
#:
#: The ceiling above bounds an identifier's LENGTH and said nothing about its CHARACTER SET,
#: and ``Location`` is built by interpolating ``event_id`` into an f-string. Two measured
#: consequences, both reachable unauthenticated and both leaving the row committed:
#:
#: * an ``event_id`` outside Latin-1 -- ``"заказ-1"``, CJK, an emoji -- raised
#:   ``UnicodeEncodeError: 'latin-1' codec can't encode`` inside Starlette's header
#:   assignment, *after* the append had already succeeded. The caller was told ``500`` (the
#:   write failed) while the ledger held the row forever, and the retry 500ed too because
#:   the idempotent-replay path sets the same header. ``"café-1"`` (U+00E9, inside Latin-1)
#:   returned ``201``, so the boundary was exactly the header codec.
#: * an ``event_id`` carrying ``\r\n``, a bare ``\n`` or ``\x00`` is *encodable* and is not
#:   a legal header value: uvicorn dropped the connection with no response at all -- the
#:   client saw ``RemoteProtocolError``, not even a status -- and the row still committed.
#:
#: So the guard is BOTH halves, and the control-character half is not redundant. This is the
#: same lesson ``store_agent.solicitation.routes._REASON_CHARACTERS`` records: a screen for
#: Latin-1 encodability alone passes ``"a\r\nX-Injected: 1"`` verbatim.
UNRENDERABLE_IDENTIFIER_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: The longest ledger ``replay?snapshots=true`` will fold before refusing.
#:
#: Every other whole-chain answer is bounded by a page (:data:`~.store.LEDGER_SCAN_CHUNK`),
#: because verification is a fold that keeps nothing. Rebuilding snapshots is not: it
#: projects every event that carries a ``dim`` and a ``type`` into a trust observation and
#: holds all of them at once, so its cost is proportional to the ledger no matter how the
#: rows are read. Measured with ``tracemalloc``: 193 bytes per observation for the projection
#: alone, call it ~400 with the event strings each observation retains, so 100,000 is roughly
#: 40 MiB against the ``mem_limit: 256m`` in ``apps/trust/compose.yaml`` -- headroom for the
#: scorer's own state and for the request that arrives while this one is running.
#:
#: Past it the answer is a 422 naming both numbers, not a slow 503 and not a snapshot of a
#: prefix: a snapshot rebuilt from part of the ledger is a snapshot of a ledger that does not
#: exist, and D17/S3 compare it against the served one bit for bit.
MAX_SNAPSHOT_REPLAY_EVENTS = 100_000

_TOO_LARGE = (
    "a LedgerEvent body may be at most {cap} bytes. The ledger is append-only -- an "
    "`ENABLE ALWAYS` trigger refuses UPDATE and DELETE -- so an accepted byte is kept "
    "forever, and this door is unauthenticated. Send a smaller payload."
)

_INCOMPLETE_BODY = (
    "the request body never finished arriving: the connection closed part-way through it. "
    "Nothing was appended. Re-send the event."
)


async def _receive_bounded_body(request: Request) -> None:
    """Read the body, refusing it the moment it crosses :data:`MAX_EVENT_BODY_BYTES`.

    STREAMED AND COUNTED, not buffered and then measured. ``await request.body()`` -- which
    is what FastAPI does before any dependency, any validator and any handler on this module
    runs -- pulls whatever arrives before anything can object, so a length check afterwards
    bounds the REFUSAL and not the MEMORY. That ordering is also why this cannot be a
    ``Depends``: by the time a dependency is solved the body is already in the process.

    The bytes are cached on the request as ``_body``, which is exactly where
    ``Request.body()`` looks first, so FastAPI's own parsing and validation run unchanged on
    a body this function has already vetted -- and ``EventIn``'s error shapes are untouched.
    """
    from starlette.requests import ClientDisconnect  # noqa: PLC0415 - only needed here

    if hasattr(request, "_body"):  # already read (a re-entered handler); nothing to bound
        return

    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_EVENT_BODY_BYTES:
                # The offending chunk is dropped rather than kept: at most one window past
                # the cap is ever held, and none of it is echoed back.
                raise HTTPException(
                    413,
                    {
                        "error": "body_too_large",
                        "message": _TOO_LARGE.format(cap=MAX_EVENT_BODY_BYTES),
                        "max_bytes": MAX_EVENT_BODY_BYTES,
                    },
                )
            chunks.append(chunk)
    except ClientDisconnect as exc:
        # A caller hanging up mid-upload is a fact of the internet, not a server fault, and
        # `ClientDisconnect` is not a `ValueError` -- an `except ValueError` written for
        # malformed bodies does not catch it and it reaches the client as a 500.
        raise HTTPException(400, {"error": "incomplete_body", "message": _INCOMPLETE_BODY}) from exc
    request._body = b"".join(chunks)


class _BoundedBodyRoute(APIRoute):
    """Every route on this router, with its request body bounded before FastAPI reads it.

    A route class rather than middleware, because middleware installed by
    :func:`~.service.create_events_app` would not be there in production: the service runs
    through the frozen ``trust.main.create_app``, which globs ``*/routes.py`` and mounts the
    ``router`` object it finds. The bound has to travel with the router, so it lives on the
    router.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            await _receive_bounded_body(request)
            return await handler(request)

        return bounded


#: This module's logger. Named ``trust.events.routes`` by ``__name__``, which is what an
#: operator greps for to see why a store was, or was not, told its score moved.
_log = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["ledger"], route_class=_BoundedBodyRoute)


class EventIn(BaseModel):
    """One ``LedgerEvent`` as it arrives over HTTP (DESIGN §Interfaces).

    ``extra="allow"`` is deliberate. Rejecting unknown fields in the model would produce
    pydantic's generic "Extra inputs are not permitted"; letting them through to
    :func:`~.store.normalise_event` produces a message that names the field *and* says why
    an extra field is not merely unwanted but unstorable -- it is hashed, it has no column,
    and the row read back therefore would not hash to the digest that was written.

    ``prev_hash`` / ``event_hash`` / ``seq`` are not fields here and are not accepted from a
    client under any spelling: the chain stamps them, and an event cannot commit to its own
    digest.
    """

    model_config = ConfigDict(extra="allow")

    event_id: str = Field(description="The idempotency key. D16: there is no second one.")
    ts: str = Field(description="RFC-3339. Normalised to UTC milliseconds before hashing.")
    kind: str = Field(description=f"One of: {', '.join(sorted(LEDGER_EVENT_KINDS))}")
    auction_id: str | None = None
    store_id: str | None = None
    order_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def store_for(request: Request) -> Any:
    """The event store this application writes to.

    Resolution order: whatever was injected on ``app.state.event_store``, otherwise a
    :class:`~.pg.PostgresEventStore` built from the environment and cached on the
    application. Per-application rather than per-process, so two apps in one interpreter --
    which is how "restart the service and continue the chain" is tested -- genuinely do not
    share the writer's state.
    """
    store = getattr(request.app.state, "event_store", None)
    if store is None:
        from .pg import default_store

        store = default_store()
        request.app.state.event_store = store
    return store


#: Set on ``app.state`` once the address book has been resolved, so an application with no
#: store agents configured re-reads the environment once rather than on every append.
#: ``None`` is a resolved answer and is cached as one.
TRUST_EVENT_SINK_ATTR = "trust_event_sink"


def trust_event_sink(request: Request) -> Any:
    """The sink R13 pushes one store's trust delta through, or ``None``.

    Resolution order mirrors :func:`store_for`: whatever was injected on
    ``app.state.trust_event_sink`` wins, otherwise one is built from the environment
    (:data:`~trust.feedback.notify.ENV_STORE_AGENT_ENDPOINTS` and
    :data:`~trust.feedback.notify.ENV_EXCHANGE_OUTCOMES_URL`) and cached on the application.

    **A sink is now built even when nothing is addressed, and that is the fix rather than a
    detail.** This used to resolve to ``None`` whenever the address book was empty, and
    :func:`_announce` then skipped the announce entirely — so an unaddressed deployment recorded
    nothing and had no way, from inside the process, to tell "the learning loop is switched off"
    from "nothing has happened yet". Now the non-delivery lands in a ring naming the variable
    that would have addressed it, and ``GET /events/verify`` publishes the whole condition.

    **What that costs, stated rather than implied.** An unaddressed sink opens no socket — the
    alternative to an address is a guess, and a guessed address is how one store's trust
    movement reaches a competitor — and it does not compute a delta either:
    :func:`~trust.feedback.notify.announce_trust_event` asks the sink whether it can deliver
    before it reads any history, so an append that nobody is listening for costs one dictionary
    lookup and a ring entry, not a database round trip. What IS new in a stack with
    ``TRUST_EXCHANGE_OUTCOMES_URL`` set and no store agents is a socket to the EXCHANGE, which
    is the point of that variable.

    ``None`` now means one of two things, and both are the same thing to this door: nothing was
    injected and this build cannot compute deltas at all (``trust.feedback`` did not import), or
    a caller injected ``app.state.trust_event_sink = None`` deliberately — which is how the
    fixtures switch the push off. Either way the service appends and does not notify; neither
    is a service that refuses appends.

    The import is function-local for the same reason ``_unreplayable_field``'s is: the ledger
    writer must stay importable and usable when the scorer is not installed, and
    ``trust.feedback`` reaches the scorer to compute a delta.
    """
    if hasattr(request.app.state, TRUST_EVENT_SINK_ATTR):
        return getattr(request.app.state, TRUST_EVENT_SINK_ATTR)
    sink: Any = None
    try:
        from ..feedback.notify import build_sink  # noqa: PLC0415

        sink = build_sink()
    except Exception:  # noqa: BLE001 - a notifier that cannot be built notifies nobody
        _log.warning(
            "the trust-event sink could not be built; appends continue and no store agent "
            "will be told what its score did",
            exc_info=True,
        )
        sink = None
    setattr(request.app.state, TRUST_EVENT_SINK_ATTR, sink)
    return sink


def notification_report(request: Request) -> dict[str, Any]:
    """What this process's learning-loop pushes have done, for a reader. Never raises.

    The READBACK the undelivered ring never had. Before this the ring was a ``deque`` on an
    object reachable from nothing an operator can call: a delta that did not land was counted in
    memory and could not be asked about, so "an operator cannot read what was lost" was literally
    true. This is that question, answered.

    It is served on ``GET /events/verify`` rather than on a route of its own, and that placement
    is a constraint rather than a preference: ``apps/trust/tests/test_contract_surface.py``
    requires the served surface to equal ``packages/contracts/openapi/trust.openapi.json``
    exactly, and declaring a new operation means editing three files under
    ``packages/contracts/`` — which this lane does not own. ``/events/verify`` is the service's
    only always-200 "what is the state of this service" door, and "the notifications this ledger
    produced did not land" is a break it should name.
    """
    sink = trust_event_sink(request)
    if sink is None:
        return {
            "available": False,
            "reason": (
                "this build could not import trust.feedback, so no delta is computed and no "
                "store agent or exchange is told anything"
            ),
        }
    reporter = getattr(sink, "report", None)
    if not callable(reporter):
        return {
            "available": False,
            "reason": f"the wired sink {type(sink).__name__!r} reports nothing",
        }
    try:
        return {"available": True, **reporter()}
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not 500 the diagnostic door
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def _announce(request: Request, store: Any, outcome: Any) -> dict[str, Any]:
    """Tell the affected store what the event just appended did to its posture.

    **This cannot fail the append, and that is the whole reason it is a function.** The event
    is already in the hash chain by the time this runs; a store agent that is down, slow or
    answering 500 is an operational problem with a notification, and turning it into a 5xx
    would tell the producer that a durable, chained, verifiable event was refused.
    ``trust.feedback.announce_trust_event`` documents itself as never raising and the
    ``except`` here is the second belt on the same trousers — it covers this function's own
    argument-building, which is code that can be wrong too.

    Only on an INSERT. A duplicate ``event_id`` answers ``200`` off the row already stored,
    and re-notifying on it would charge a store twice for one event the moment its agent
    starts counting deltas -- the exact "exactly once, to exactly one" property
    ``push_trust_event`` is written around.
    """
    if not outcome.inserted:
        return {"pushed": False, "reason": "this event_id was already in the chain"}
    sink = trust_event_sink(request)
    if sink is None:
        return {
            "pushed": False,
            "reason": "this build cannot compute trust deltas; nothing was notified",
        }
    try:
        from ..feedback.notify import announce_trust_event, store_history_reader  # noqa: PLC0415

        return announce_trust_event(
            outcome.event,
            history_reader=store_history_reader(store, before_seq=outcome.seq),
            sink=sink,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        _log.warning(
            "notifying the affected store about event %s failed; the event is in the ledger",
            str(outcome.event.get("event_id", "unknown")),
            exc_info=True,
        )
        return {"pushed": False, "reason": f"{type(exc).__name__}: {exc}"}


def _project(request: Request, store: Any, outcome: Any) -> int:
    """Land the trust observation this event carries, and never fail the append over it.

    The event is already sealed into the hash chain by the time this runs, so every failure
    below is reported as ``observation_rows: 0`` rather than as a status: a 5xx here would
    tell the producer that a durable, chained, verifiable event had been refused, which is
    false and unrecoverable. :mod:`.observations` argues the same point from the other side.

    The ``except`` is the second belt on the same trousers as ``persist_event_observations``'
    own: it covers this function's argument-building and the connection resolution, which is
    code that can be wrong too.
    """
    try:
        from .observations import (  # noqa: PLC0415 - lazy, like every db-facing import here
            observation_connection,
            persist_event_observations,
        )

        with observation_connection(request, store) as connection:
            return persist_event_observations(connection, outcome.seq, outcome.event)
    except Exception:  # noqa: BLE001 - see the docstring
        _log.warning(
            "the trust observation for event %s was not written; the event is in the ledger",
            str(outcome.event.get("event_id", "unknown")),
            exc_info=True,
        )
        return 0


def _refuse(exc: EventServiceError) -> HTTPException:
    """Map a service error to the status that tells the caller what to do about it."""
    if isinstance(exc, StoreUnavailable):
        return HTTPException(503, {"error": "store_unavailable", "message": str(exc)})
    if isinstance(exc, ChainForked):
        return HTTPException(
            503,
            {"error": "chain_forked", "message": str(exc), "retryable": True},
            headers={"Retry-After": "0"},
        )
    if isinstance(exc, IdempotencyConflict):
        return HTTPException(409, {"error": "idempotency_conflict", "message": str(exc)})
    if isinstance(exc, UnknownEventKind):
        return HTTPException(
            422,
            {
                "error": "unknown_event_kind",
                "message": str(exc),
                "kinds": sorted(LEDGER_EVENT_KINDS),
            },
        )
    if isinstance(exc, InvalidEvent):
        return HTTPException(422, {"error": "invalid_event", "message": str(exc)})
    if isinstance(exc, BrokenChain):
        return HTTPException(500, {"error": "broken_chain", "message": str(exc)})
    return HTTPException(500, {"error": "ledger_error", "message": str(exc)})


def _refuse_long_identifier(field: str, value: Any) -> None:
    """Refuse a caller-chosen identifier longer than :data:`MAX_IDENTIFIER_LENGTH`.

    The refusal reports the field and the two numbers and **not the value**: this exists
    because a 30,000-character identifier was previously admitted (or, past the btree limit,
    turned into a ``503``), and a refusal that quoted it back would trade one amplifier for
    a politer one.
    """
    if not isinstance(value, str) or len(value) <= MAX_IDENTIFIER_LENGTH:
        return
    raise HTTPException(
        422,
        {
            "error": "identifier_too_long",
            "field": field,
            "length": len(value),
            "max_length": MAX_IDENTIFIER_LENGTH,
            "message": (
                f"{field} is {len(value)} characters; the ceiling is "
                f"{MAX_IDENTIFIER_LENGTH}. Every identifier on a LedgerEvent is indexed, "
                f"so an unbounded one is refused by Postgres itself past the btree entry "
                f"limit -- as an outage-shaped 503 rather than as the bad request it is."
            ),
        },
    )


def _refuse_unrenderable_identifier(field: str, value: Any) -> None:
    """Refuse a caller-chosen identifier a response header could not carry.

    Length was bounded (:func:`_refuse_long_identifier`) and the character set was not, and
    ``post_event`` builds ``Location`` by interpolating ``event_id``. See
    :data:`UNRENDERABLE_IDENTIFIER_CHARACTERS` for the two measured 5xx paths that closes.

    **Why all four identifiers and not only ``event_id``.** Only ``event_id`` reaches a
    header today. But the ledger is append-only under an ``ENABLE ALWAYS`` trigger, so the
    rows admitted today are permanent, and the guard that would matter is the one that was
    in place *before* somebody interpolates ``store_id`` into a header. A control character
    in an indexed identifier is never a thing a caller meant, and the cost of refusing it is
    a 422 the caller can act on; the cost of admitting it is a row nobody can delete.

    The refusal names the field and the *class* of the problem and **never the value** --
    the same rule the length ceiling follows, and doubly so here, where echoing the value
    would mean writing the caller's control characters into this process's response.
    """
    if not isinstance(value, str):
        return
    if UNRENDERABLE_IDENTIFIER_CHARACTERS.search(value):
        reason, why = (
            "control_character",
            "carries a control character (C0, DEL or C1). A header value cannot hold one: "
            "CR/LF is a response split and uvicorn answers such a response by closing the "
            "connection with no status at all, which it did -- after the append had "
            "committed to an append-only table",
        )
    else:
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            reason, why = (
                "not_latin_1",
                "is outside Latin-1. HTTP header values are Latin-1 on the wire, and this "
                "route reports the created event in `Location: /events/{event_id}`, so such "
                "an identifier raised UnicodeEncodeError *after* the row had already been "
                "written -- telling the caller the write failed while the ledger kept it "
                "forever",
            )
        else:
            return
    raise HTTPException(
        422,
        {
            "error": "identifier_not_renderable",
            "field": field,
            "reason": reason,
            "message": (
                f"{field} {why}. Identifiers on a LedgerEvent must be renderable into a "
                f"response header: Latin-1 encodable and free of control characters."
            ),
        },
    )


#: Which payload field the trust replay choked on, per exception the scorer raises. The
#: scorer's own messages are excellent and cannot be forwarded: they interpolate the
#: offending value (``f"{name!r} is not one of the six trust dimensions"``), and a payload
#: field is caller-controlled and may be tens of kilobytes. So the exception is turned into
#: a field NAME here, and this route writes its own message from server-owned vocabulary.
_UNREPLAYABLE_TIMESTAMP_FIELD = "observed_at"


def _unreplayable_field(body: Mapping[str, Any]) -> str | None:
    """Which field makes ``body`` un-replayable, or ``None`` when the replay can read it.

    ``GET /events/replay?snapshots=true`` projects every event whose payload names a ``dim``
    and a ``type`` into a trust observation (``trust.ledger.replay.observations_from_events``)
    and folds it with ``trust.scoring.score``. Both vocabularies are CLOSED and both raise
    rather than drop -- deliberately, because an observation type nobody weighted would
    otherwise score a dishonest store as a clean one. Nothing on the write path checked
    either, so 148 bytes of legal-looking JSON

        {"event_id": "e2", "ts": "...", "kind": "feedback", "store_id": "s-1",
         "payload": {"dim": "price_honored", "type": "positive"}}

    returned ``201`` and made every subsequent snapshot replay a ``500`` -- permanently,
    because the ledger's ``BEFORE UPDATE OR DELETE ... ENABLE ALWAYS`` trigger means the row
    cannot be evicted. Four fields reach the scorer and all four were unchecked: ``dim``,
    ``type``, ``weight`` and ``observed_at``.

    **The check is the reader, run over one event.** It projects with the reader's own
    projection and folds with the reader's own scorer, then throws the numbers away. That is
    the point: a hand-written copy of "the six dimensions and the seven types" in this file
    would be a second vocabulary free to drift from the one the replay actually uses, and
    the drift would show up as exactly this defect again. Nothing here decides a score
    (D49); it decides only whether a score is *computable*, which is a property of the door.

    Why not :func:`contracts.ledger.validate_ledger_payload`, which ``claims/routes.py``
    calls: it answers a different question and is neither sufficient nor necessary for this
    one. Not sufficient -- ``{"matched_pitch": true, "reason": "x", "dim": "bogus",
    "type": "positive"}`` satisfies the published ``feedback`` shape (extra keys are allowed
    by design) and still bricks the replay. Not necessary -- it would refuse ``{"n": 1}``,
    which replays perfectly well, and refusing it here would contradict the rule
    ``packages/contracts/src/ledger.py`` states in its own header: the shape check belongs at
    the PRODUCING boundary, and the ledger must stay lossless so that a vendor body with an
    unexpected key is recorded rather than dropped. ``claims/routes.py`` IS a producer -- it
    builds the payload it validates. ``POST /events`` is the store's door, and what a store's
    door owes is that what it admits can still be read back.

    Returns:
        The name of the offending payload field, or ``None``. Returns ``None`` when the
        scorer is not importable at all: in that configuration ``replay?snapshots=true``
        already answers ``503 scorer_unavailable`` rather than ``500``, so there is no 5xx
        to prevent, and refusing every write because the scorer is absent would take the
        ledger down for a reason that is not the caller's.
    """
    try:  # noqa: PLC0415 - lazy for the same reason `get_replay`'s import is lazy
        from ..ledger import observations_from_events
        from ..scoring import (
            InvalidObservationWeight,
            UnknownObservationType,
            UnknownTrustDimension,
            score,
        )
    except ImportError:
        return None

    observations = observations_from_events([body])
    if not observations:
        return None
    try:
        # `as_of` is the event's own normalised `ts`, so the fold is well defined and the
        # only thing that can raise is the event. The snapshot is discarded.
        score(observations, as_of=body.get("ts"))
    except UnknownTrustDimension:
        return "dim"
    except UnknownObservationType:
        return "type"
    except InvalidObservationWeight:
        return "weight"
    except ValueError:
        # `trust.scoring.engine._parse_instant` on an unparseable `payload.observed_at`.
        return _UNREPLAYABLE_TIMESTAMP_FIELD
    except LookupError:  # pragma: no cover - the scorer documents no other LookupError
        return "payload"
    return None


def _permitted_values(field: str) -> list[str] | None:
    """The closed vocabulary a refusal may quote for ``field``. Server-owned, never input."""
    try:  # noqa: PLC0415 - the scorer is optional; see `_unreplayable_field`
        from ..scoring import OBSERVATION_WEIGHTS, TRUST_DIMENSIONS
    except ImportError:  # pragma: no cover - unreachable once the scorer is importable
        return None
    if field == "dim":
        return sorted(TRUST_DIMENSIONS)
    if field == "type":
        return sorted(OBSERVATION_WEIGHTS)
    return None


_UNREPLAYABLE_ADVICE = {
    "dim": "`dim` must name one of the closed six trust dimensions (D53).",
    "type": "`type` must be an observation type with a published weight.",
    "weight": (
        "`weight` is a RELATIVE multiplier in [0.0, 1.0] scaling the published weight of "
        "`type` -- not the published weight itself. R14: one report cannot outvote the "
        "network, so a weight above one is refused rather than clamped."
    ),
    _UNREPLAYABLE_TIMESTAMP_FIELD: (
        "`observed_at` must be an RFC-3339 instant. Trust decay is a function of it, so an "
        "unparseable one cannot be silently read as 'now' without making the replay differ "
        "from the serve."
    ),
    "payload": "the payload cannot be projected into a trust observation.",
}


def _refuse_unreplayable_ledger(store: Any, exc: BaseException) -> HTTPException:
    """A ``422`` naming the stored row the trust scorer cannot interpret.

    The write path refuses such payloads now, but this is the OTHER half of that fix and it
    is the half that matters to anybody already running the service: the ledger's
    ``BEFORE UPDATE OR DELETE ... ENABLE ALWAYS`` trigger means a row admitted before the
    guard existed cannot be updated, deleted or truncated away. Left alone, every
    ``replay?snapshots=true`` on such a deployment is a ``500`` forever, and a ``500`` is
    indistinguishable from the service being down.

    **What this does NOT do is skip the row and serve a number.** Both closed vocabularies
    raise rather than drop precisely so that a behaviour nobody weighted cannot score as a
    clean record, and a snapshot folded over "the events we could read" is a snapshot of a
    ledger that does not exist -- the same reason ``MAX_SNAPSHOT_REPLAY_EVENTS`` refuses
    rather than snapshotting a prefix. So the endpoint still declines to produce snapshots
    over a poisoned ledger. What changes is that it declines *legibly*: a 4xx that says the
    stored data is un-interpretable and names the row, instead of a 5xx that says nothing
    and blames the server.

    The offending row is located by a second pass with :func:`_unreplayable_field`, the same
    predicate the door uses, so the two cannot disagree about what "poison" means. That pass
    costs a walk of the ledger -- paid only on a ledger that is already broken, bounded by
    the :data:`MAX_SNAPSHOT_REPLAY_EVENTS` check that has already run above, and stopping at
    the first offender.
    """
    seq: Any = None
    event_id: Any = None
    field: str | None = None
    try:
        for stored in store.iter_events():
            found = _unreplayable_field(stored)
            if found is not None:
                seq, event_id, field = stored.get("seq"), stored.get("event_id"), found
                break
    except EventServiceError:  # pragma: no cover - the read that just succeeded, failing
        pass

    if isinstance(event_id, str):
        # Rows written before the T-366 ceiling can be arbitrarily long; a diagnosis is not
        # a licence to echo one back. Post-fix identifiers are 128 characters at most.
        event_id = event_id[:MAX_IDENTIFIER_LENGTH]

    return HTTPException(
        422,
        {
            "error": "unreplayable_ledger",
            "seq": seq,
            "event_id": event_id,
            "field": field,
            "reason": type(exc).__name__,
            "message": (
                "this ledger holds an event whose payload the trust scorer cannot interpret, "
                "so it has no snapshot to replay. Such an event is refused at the door now; "
                "this one predates that guard, and the append-only trigger means it cannot "
                "be deleted through this service. Verification (snapshots=false) is "
                "unaffected and stays available. The offending row is named above; repairing "
                "it is a datastore operation, not an API one."
            ),
        },
    )


def _refuse_unreplayable_payload(field: str) -> HTTPException:
    """A ``422`` for a payload the replay could not later interpret. Quotes no input."""
    detail: dict[str, Any] = {
        "error": "unreplayable_payload",
        "field": field,
        "message": (
            f"this payload names both `dim` and `type`, so GET /events/replay?snapshots=true "
            f"will project it into a trust observation -- and the trust scorer cannot "
            f"interpret its `{field}`. {_UNREPLAYABLE_ADVICE[field]} The ledger is "
            f"append-only under a `BEFORE UPDATE OR DELETE ... ENABLE ALWAYS` trigger, so "
            f"accepting this event would make every later snapshot replay a 500 that nothing "
            f"could ever clear."
        ),
    }
    permitted = _permitted_values(field)
    if permitted is not None:
        detail["permitted"] = permitted
    return HTTPException(422, detail)


@router.post("", response_model=None, status_code=201, summary="Append one ledger event")
def post_event(
    request: Request,
    response: Response,
    event: EventIn,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional. Must equal the body's event_id -- D16 admits no second key.",
    ),
) -> dict[str, Any]:
    """Append one event, or recognise the one already stored under its ``event_id``.

    Returns ``201`` with the sealed event when it lands and ``200`` with the *stored* event
    when the id was already in the chain. ``Idempotent-Replay: true`` marks the second case
    for a caller that would rather read a header than a status code.
    """
    # Length first, and mismatch second. Both messages below quote an identifier back at
    # the caller, so the ceiling has to bind before anything is interpolated -- otherwise
    # refusing a 30,000-character event_id would echo 30,000 characters.
    _refuse_long_identifier("Idempotency-Key", idempotency_key)
    for field in BOUNDED_IDENTIFIERS:
        _refuse_long_identifier(field, getattr(event, field, None))

    # Character set second, for the same ordering reason and one more: an identifier
    # carrying CR/LF must not reach the `!r` interpolation in the mismatch message below.
    _refuse_unrenderable_identifier("Idempotency-Key", idempotency_key)
    for field in BOUNDED_IDENTIFIERS:
        _refuse_unrenderable_identifier(field, getattr(event, field, None))

    if idempotency_key is not None and idempotency_key != event.event_id:
        raise HTTPException(
            422,
            {
                "error": "idempotency_key_mismatch",
                "message": (
                    f"Idempotency-Key {idempotency_key!r} does not match event_id "
                    f"{event.event_id!r}. D16 makes event_id the idempotency key; two "
                    f"disagreeing keys means the caller believes something untrue about "
                    f"which event this is."
                ),
            },
        )

    store = store_for(request)
    body = event.model_dump()

    # Normalised HERE, and appended below unchanged. `store.append` normalises again -- the
    # rule stays "one normalisation, in `normalise_event`" (D16) and this call does not
    # become a second one -- but the poison check has to see the body that will actually be
    # stored, not the one that arrived: `ts` is the fallback `observed_at`, and it is the
    # canonicaliser that turns a caller's RFC-3339 spelling into the instant the scorer
    # reads. Doing this before the append is the whole point: a poison row cannot be
    # deleted afterwards, so the only place it can be stopped is before it exists.
    try:
        normalised = normalise_event(body)
    except EventServiceError as exc:
        raise _refuse(exc) from exc

    unreplayable = _unreplayable_field(normalised)
    if unreplayable is not None:
        raise _refuse_unreplayable_payload(unreplayable)

    try:
        outcome = append(store, body)
    except EventServiceError as exc:
        raise _refuse(exc) from exc

    # R12: an event that carries a trust observation becomes a row in
    # `ledger.trust_observations`, which is the table `GET /snapshot` reads and therefore the
    # only thing `exchange.composition.HttpTrustSnapshot` can see. Before this, the ledger
    # writer wrote `ledger.commerce_events` and nothing else, so a served buyer complaint
    # moved the replayed score and left the exchange's eligibility read untouched.
    #
    # It runs on every request, not only on an insert. A duplicate `event_id` answers 200 off
    # the stored row and appends nothing, but its observation row may still be missing -- a
    # first attempt whose relational write failed is exactly that state -- and re-running the
    # write is a no-op arbitrated on `event_seq`. That is the difference between this and
    # `_announce` above, which must fire once because a second notification is a second
    # penalty; a second INSERT of the same `event_seq` is not a second anything.
    observation_rows = _project(request, store, outcome)

    response.status_code = 201 if outcome.inserted else 200
    response.headers["Idempotent-Replay"] = "false" if outcome.inserted else "true"
    response.headers["Location"] = f"/events/{outcome.event['event_id']}"

    # R13: an event that moves a trust dimension is told to the affected store, and to no
    # other. Inline rather than backgrounded, so the notification is bounded by the same
    # request the event arrived on; `_announce` is what guarantees it cannot cost that
    # request its 201. AFTER the projection, so an agent that reacts by reading
    # `GET /snapshot` cannot beat its own observation into the table.
    notification = _announce(request, store, outcome)

    return {
        "inserted": outcome.inserted,
        "event": outcome.event,
        "head_hash": outcome.head_hash,
        "seq": outcome.seq,
        "length": outcome.length,
        # What the notification did, said out loud on the same response. `observation_rows`
        # below already establishes the rule this follows: a side effect the door performs
        # best-effort must report what it did, because the alternative is that a producer
        # cannot tell "no delta in this event" from "the delta was told to nobody" — and the
        # second was, measured, the state of every shipped configuration.
        "notification": notification,
        # What actually reached the door the exchange reads. Reported rather than assumed:
        # the write is best effort by design (the chain already holds the event), so a
        # deployment must be able to tell "no observation in this event" from "the
        # observation did not land", and 0 against a `dim`-carrying payload is the second.
        "observation_rows": observation_rows,
    }


@router.get("", response_model=None, summary="Read the chain in insertion order")
def get_events(
    request: Request,
    after_seq: int = Query(0, ge=0, description="Return events after this sequence number."),
    store_id: str | None = Query(None, description="Projection filter. NOT a chain."),
    limit: int = Query(
        DEFAULT_EVENT_PAGE,
        ge=1,
        le=MAX_EVENT_PAGE,
        description="Rows per response. Capped by default; see DEFAULT_EVENT_PAGE.",
    ),
) -> dict[str, Any]:
    """The chain, oldest first, **one page at a time**.

    ``store_id`` yields a **projection, not a chain** -- the links skip whatever the filter
    removed -- so the response says so in ``is_chain`` rather than letting a caller hand the
    result to a verifier and get a ``broken_link`` it caused itself.

    ``truncated`` is the other half of that honesty, and it is why the read asks the store
    for ``limit + 1`` rows: a page that is silently short is indistinguishable from a ledger
    that is short, and a caller told "here are 1000 events, ok" about a ledger of 40000 has
    been misled about the one thing this endpoint exists to report. The extra row is the
    cheapest possible way to know which of the two happened, and it is discarded.

    ``is_chain`` therefore means *complete and unfiltered*, not *unlimited*: a page that
    happened to fit is still the whole chain, and a page that was cut is not -- which is a
    strictly better answer than the old ``limit is None``, under which asking for a limit of
    ten against a ledger of three declared the complete result "not a chain".
    """
    store = store_for(request)
    try:
        events, truncated = store.read_page(after_seq=after_seq, store_id=store_id, limit=limit)
    except EventServiceError as exc:
        raise _refuse(exc) from exc

    return {
        "events": events,
        "count": len(events),
        "limit": limit,
        "truncated": truncated,
        "next_after_seq": int(events[-1]["seq"]) if events else after_seq,
        "is_chain": store_id is None and after_seq == 0 and not truncated,
    }


@router.get("/head", response_model=None, summary="The stored chain head and anchor")
def get_head(request: Request) -> dict[str, Any]:
    """``{head_hash, length}`` -- the link the next append writes behind, and the count."""
    store = store_for(request)
    try:
        return {"head_hash": store.head_hash, "length": store.length}
    except EventServiceError as exc:
        raise _refuse(exc) from exc


@router.get("/verify", response_model=None, summary="Verify the chain and name any break")
def get_verify(request: Request) -> dict[str, Any]:
    """Verify links **and** anchor, and report what the ledger's outbound pushes did.

    Always ``200``: "the ledger is broken" is an answer to the question, not a failure to
    answer it, and a 5xx here would be indistinguishable from the verifier being down --
    which is precisely the state an attacker who had just tampered with the ledger would
    like it to be confused with. Read ``ok``.

    ``store_agent_notifications`` is the second break this door can now name, and the reason it
    is here is argued in :func:`notification_report`. It costs no database statement -- the
    numbers are counters on an in-process sink -- so the bound
    ``test_events_dos_surface.test_no_route_on_this_router_issues_an_unbounded_read`` holds this
    route to is untouched.
    """
    store = store_for(request)
    try:
        report = store.verify()
    except EventServiceError as exc:
        raise _refuse(exc) from exc
    report["store_agent_notifications"] = notification_report(request)
    return report


@router.get("/replay", response_model=None, summary="Replay the chain out of the ledger")
def get_replay(
    request: Request,
    include_events: bool = Query(True, description="Include the replayed events themselves."),
    limit: int = Query(
        DEFAULT_EVENT_PAGE,
        ge=1,
        le=MAX_EVENT_PAGE,
        description="How many replayed events to serialise. Verification is never capped.",
    ),
    after_seq: int = Query(0, ge=0, description="Start the serialised page after this seq."),
    snapshots: bool = Query(False, description="Also rebuild trust snapshots (T-062)."),
    as_of: str | None = Query(None, description="Required with snapshots=true. Never a clock."),
) -> dict[str, Any]:
    """Replay the ledger and report the stream's identity.

    ``stream_hash`` is **recomputed from every event's content**, from genesis, ignoring the
    digests stored on the rows -- so comparing it against ``head_hash`` (which is read off
    the last row) has content on both sides. Comparing a stored head against a stored head
    proves nothing, and that is what "replay reproduces the stream hash" quietly meant
    before it was fixed in T-011.

    ``snapshots=true`` delegates to :func:`trust.ledger.replay`, which delegates every
    number to T-062's scorer. This ticket computes no scores; when the scorer is not built
    the answer is a ``503`` naming it, never an empty mapping that would compare equal to
    nothing and read as a pass.

    **What is capped and what is not.** ``limit`` bounds the events this endpoint
    *serialises*. It does **not** bound what was verified or what was folded: ``ok``,
    ``reason``, ``length``, ``head_hash``, ``stream_hash`` and any ``snapshots`` are computed
    over every event in the ledger, because those five numbers are the evidence this endpoint
    exists to produce and a stream hash over a page is a hash of something nobody asked
    about. ``events_truncated`` / ``events_returned`` / ``next_after_seq`` say which slice of
    that verified stream came back, and ``after_seq`` walks the rest.

    That sentence used to be a *half*-truth, and the half it left out was the defect. The
    events were serialised by the page and **loaded** in full: the store answered with one
    unbounded ``read_events(connection)``, so an 8-byte anonymous GET made the process
    allocate proportionally to everything ever written -- 525 MiB on the measured ledger,
    inside a 256 MiB container. Verification now walks the chain in windows of
    :data:`~.store.LEDGER_SCAN_CHUNK` and keeps two events, so "computed over every event"
    is still exactly true and no longer costs every event.

    ``snapshots=true`` is the one fold that cannot be paged -- an observation per qualifying
    event, all live at once -- so it carries its own ceiling,
    :data:`MAX_SNAPSHOT_REPLAY_EVENTS`, and refuses past it rather than allocating.
    """
    store = store_for(request)
    try:
        # The store verifies every event through a paged reader and hands back ONE page of
        # them. `limit` used to bound only the slicing, which happened after the whole
        # ledger was already a list of Python dicts (T-364).
        report = store.replay(after_seq=after_seq, limit=limit, include_events=include_events)
    except EventServiceError as exc:
        raise _refuse(exc) from exc

    if snapshots:
        if as_of is None:
            raise HTTPException(
                422,
                {
                    "error": "as_of_required",
                    "message": (
                        "snapshots=true needs an explicit as_of instant: trust decay is a "
                        "function of it, and a replay evaluated against the wall clock is "
                        "not reproducible (D17/S3)."
                    ),
                },
            )
        length = int(report.get("length") or 0)
        if length > MAX_SNAPSHOT_REPLAY_EVENTS:
            raise HTTPException(
                422,
                {
                    "error": "ledger_too_long_to_replay",
                    "message": (
                        f"rebuilding snapshots folds every one of this ledger's {length} "
                        f"events into trust observations and holds all of them at once, "
                        f"and this door is unauthenticated; the ceiling is "
                        f"{MAX_SNAPSHOT_REPLAY_EVENTS}. Verification "
                        f"(snapshots=false) is paged and stays available at any length."
                    ),
                    "length": length,
                    "max_length": MAX_SNAPSHOT_REPLAY_EVENTS,
                },
            )
        from ..ledger import replay as ledger_replay

        try:
            # The WHOLE stream, before any paging: a snapshot rebuilt from a page is a
            # snapshot of a ledger that does not exist. Streamed rather than listed, so what
            # is held is the observations (which the ceiling above bounds) and not the raw
            # events as well.
            report["snapshots"] = ledger_replay(store.iter_events(), as_of=as_of)
        except ModuleNotFoundError as exc:
            raise HTTPException(503, {"error": "scorer_unavailable", "message": str(exc)}) from exc
        except EventServiceError as exc:
            raise _refuse(exc) from exc
        except (LookupError, ValueError) as exc:
            # A row the scorer cannot interpret. The write path refuses these now, but a
            # ledger written before that fix already holds them and CANNOT be repaired
            # through this service -- the append-only trigger refuses UPDATE and DELETE -- so
            # guarding only the door would leave every such deployment permanently 500ing.
            raise _refuse_unreplayable_ledger(store, exc) from exc
        report["as_of"] = as_of

    return report


@router.get("/{event_id}", response_model=None, summary="One event by its idempotency key")
def get_event(request: Request, event_id: str) -> dict[str, Any]:
    """The stored event, or ``404``. Declared last so it cannot shadow the routes above."""
    store = store_for(request)
    try:
        found = store.get(event_id)
    except EventServiceError as exc:
        raise _refuse(exc) from exc
    if found is None:
        # The echoed id is CUT to the ceiling the write door enforces. `event_id` here is a
        # PATH parameter, so neither `_refuse_long_identifier` nor
        # `_refuse_unrenderable_identifier` has run on it -- this was the one refusal on the
        # module that quoted an unbounded caller string back, which contradicts the rule
        # this module's own docstring states. It goes into a JSON body and never a header,
        # so it was never a 5xx; it was just an echo nobody had bounded. An id longer than
        # the ceiling cannot name a stored event anyway, because no such row can exist.
        raise HTTPException(
            404,
            {
                "error": "unknown_event",
                "message": f"no event with event_id {event_id[:MAX_IDENTIFIER_LENGTH]!r}",
            },
        )
    return {"event": found}


# ==========================================================================================
# BOOT
# ==========================================================================================
# Said ONCE, when the process starts, and this is the only place it can be said: `trust.main`
# is orchestrator-owned and frozen (B6(iii)), so a worker cannot add a startup hook to it --
# but `main.create_app()` imports this module by glob to mount `router`, and that import IS
# this service's startup path. Two lines of environment, read at the moment a deployment
# begins serving, before any traffic can make their absence look like quiet.
#
# The whole point is that "no store agent is addressed" and "no exchange is addressed" are
# CONFIGURATION and were previously invisible: nothing logged them, nothing counted them and
# nothing health-reported them, so a stack whose learning loop was switched off looked exactly
# like a stack in which nothing had happened yet. `GET /events/verify` carries the same
# condition for a reader who arrives after this line has scrolled away.
#
# Wrapped, because `trust.feedback` reaches the scorer: a build that ships the ledger writer
# without it must still serve `POST /events`, which is the rule `trust_event_sink` follows for
# the same import.
try:  # pragma: no cover - exercised by every started process, and by `create_app()` in tests
    from ..feedback.notify import log_learning_loop_state as _log_learning_loop_state

    _log_learning_loop_state(log=_log)
except Exception:  # noqa: BLE001 - a service that cannot describe its loop still serves appends
    _log.warning(
        "the trust learning-loop configuration could not be read at boot; appends continue",
        exc_info=True,
    )
