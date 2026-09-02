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
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
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
from .store import LEDGER_EVENT_KINDS, append

__all__ = ["DEFAULT_EVENT_PAGE", "MAX_EVENT_PAGE", "EventIn", "router", "store_for"]

router = APIRouter(prefix="/events", tags=["ledger"])

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
MAX_EVENT_PAGE = 10_000


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
    try:
        outcome = append(store, event.model_dump())
    except EventServiceError as exc:
        raise _refuse(exc) from exc

    response.status_code = 201 if outcome.inserted else 200
    response.headers["Idempotent-Replay"] = "false" if outcome.inserted else "true"
    response.headers["Location"] = f"/events/{outcome.event['event_id']}"
    return {
        "inserted": outcome.inserted,
        "event": outcome.event,
        "head_hash": outcome.head_hash,
        "seq": outcome.seq,
        "length": outcome.length,
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
        # One row past the page: present means there is more, absent means this is all.
        window = store.read(after_seq=after_seq, store_id=store_id, limit=limit + 1)
    except EventServiceError as exc:
        raise _refuse(exc) from exc

    truncated = len(window) > limit
    events = window[:limit]
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
    """Verify links **and** anchor.

    Always ``200``: "the ledger is broken" is an answer to the question, not a failure to
    answer it, and a 5xx here would be indistinguishable from the verifier being down --
    which is precisely the state an attacker who had just tampered with the ledger would
    like it to be confused with. Read ``ok``.
    """
    store = store_for(request)
    try:
        return store.verify()
    except EventServiceError as exc:
        raise _refuse(exc) from exc


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
    *serialises*, because an uncapped one put the entire append-only history into a single
    response body -- the same unbounded-response hole ``GET /events`` had. It does **not**
    bound what was verified or what was folded: ``ok``, ``reason``, ``length``,
    ``head_hash``, ``stream_hash`` and any ``snapshots`` are computed over every event in
    the ledger, before the page is cut, because those five numbers are the evidence this
    endpoint exists to produce and a stream hash over a page is a hash of something nobody
    asked about. ``events_truncated`` / ``events_returned`` / ``next_after_seq`` say which
    slice of that verified stream came back, and ``after_seq`` walks the rest.
    """
    store = store_for(request)
    try:
        report = store.replay()
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
        from ..ledger import replay as ledger_replay

        try:
            # The WHOLE stream, before any paging: a snapshot rebuilt from a page is a
            # snapshot of a ledger that does not exist.
            report["snapshots"] = ledger_replay(report["events"], as_of=as_of)
        except ModuleNotFoundError as exc:
            raise HTTPException(503, {"error": "scorer_unavailable", "message": str(exc)}) from exc
        report["as_of"] = as_of

    if not include_events:
        report.pop("events", None)
        return report

    replayed: list[dict[str, Any]] = list(report.get("events") or [])
    remaining = [row for row in replayed if int(row.get("seq") or 0) > after_seq]
    page = remaining[:limit]
    report["events"] = page
    report["events_returned"] = len(page)
    report["events_truncated"] = len(page) < len(remaining)
    report["next_after_seq"] = int(page[-1]["seq"]) if page else after_seq
    report["limit"] = limit
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
        raise HTTPException(
            404, {"error": "unknown_event", "message": f"no event with event_id {event_id!r}"}
        )
    return {"event": found}
