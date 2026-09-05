"""``POST /claims/verifications`` — the door a verification outcome comes in through.

The handler does the two things T-065's objective names and the tree did not do:

1. **persists** the outcome across the five ``ledger.*`` tables, through the one seam
   :func:`trust.verification.persist_claim_verification`; and
2. **emits** the ``claim_verified`` ledger event, through the same
   :func:`trust.events.append` seam every other producer in this service uses.

Order matters and is not arbitrary. The rows go down first, so the event is only ever
appended for an outcome that is actually recorded — an event announcing a verification the
tables do not hold is worse than no event, because a replay would reconstruct a claim the
ledger cannot corroborate. A replayed call writes neither: the database refuses the
verification on ``claim_verifications_idempotency_key``, the seam reports ``written=False``,
and this handler answers 200 with that outcome instead of 201.

``claim_verified`` was, until this route, one of three kinds reserved in all four frozen
vocabularies and produced by nothing (see T-302) — including by
``packages/contracts/openapi/trust.openapi.json``, which uses it as its example body. This
route is the producer that had been missing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..events.errors import EventServiceError, StoreUnavailable
from ..events.store import append
from ..verification import persist_claim_verification

__all__ = ["ClaimVerificationIn", "connection_for", "router", "store_for"]

router = APIRouter(prefix="/claims", tags=["trust"])

#: The ledger kind a recorded verification announces. Frozen vocabulary — see
#: ``trust.events.LEDGER_EVENT_KINDS`` and ``commerce_events_kind_check``.
CLAIM_VERIFIED_KIND = "claim_verified"


class ClaimVerificationIn(BaseModel):
    """One verification outcome, as the verifier decided it.

    Every field the migration marks NOT NULL without a DEFAULT is required here too, so a
    payload this model accepts is a payload Postgres can hold. The optional ones carry the
    same defaults the seam does.
    """

    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, description="The store the claim was made by.")
    claim_ref: str = Field(min_length=1, description="The claim's identity within the store.")
    claim_type: str = Field(description="A member of the approved claim_type vocabulary.")
    key: str = Field(min_length=1, description="The claim's key, e.g. `price.value`.")
    status: str = Field(description="verified | contradicted | unsupported | ambiguous.")
    confidence: float = Field(ge=0.0, le=1.0)
    catalog_snapshot_id: str = Field(description="The catalog snapshot verified against (uuid).")
    verifier_version: str = Field(min_length=1)
    observed_at: str = Field(description="RFC-3339. Explicit, never a clock.")
    evidence_refs: list[str] = Field(default_factory=list)
    value: Any = None
    observed_value: Any = None
    provenance_source: str | None = None
    provenance_ref: str | None = None
    authority_rank: int = 0
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    source_class: str | None = None


def store_for(request: Request) -> Any:
    """The event store this application appends the ``claim_verified`` event to.

    Delegates to :func:`trust.events.routes.store_for` rather than resolving its own, so this
    route and ``/events`` write to the SAME chain. Two resolvers would be two chains.
    """
    from ..events.routes import store_for as events_store_for

    return events_store_for(request)


def connection_for(request: Request) -> Any:
    """The database connection the verification rows are written on.

    Resolution order: whatever was injected on ``app.state.ledger_connection``, otherwise one
    opened against the DSN the ledger writer itself resolved. The DSN comes from
    :class:`~..events.pg.PostgresEventStore` rather than from a second walk of the
    environment on purpose — ``DEFAULT_DSN_ENV``'s ORDER is the thing T-151 got wrong and
    T-181/T-289 are still trying to defend, and a second resolver here would be a second
    place for that order to drift.
    """
    connection = getattr(request.app.state, "ledger_connection", None)
    if connection is not None:
        return connection

    import psycopg

    from ..events.pg import PostgresEventStore

    try:
        dsn = PostgresEventStore()._resolve_dsn()
    except StoreUnavailable as exc:
        raise HTTPException(503, {"error": "store_unavailable", "message": str(exc)}) from exc
    try:
        connection = psycopg.connect(dsn, autocommit=False)
    except Exception as exc:  # noqa: BLE001 - reported as an outage, never as a bad request
        raise HTTPException(
            503,
            {
                "error": "store_unavailable",
                "message": f"the verification writer could not reach the ledger database: {exc}",
            },
        ) from exc
    request.app.state.ledger_connection = connection
    return connection


@router.post(
    "/verifications",
    response_model=None,
    status_code=201,
    summary="Record one claim-verification outcome and announce it on the ledger",
)
def post_claim_verification(request: Request, body: ClaimVerificationIn) -> Any:
    """Persist the outcome, then announce it. 201 when it was new, 200 on a replay."""
    from fastapi.responses import JSONResponse

    payload = body.model_dump(exclude_none=True)
    payload.pop("store_id", None)

    try:
        outcome = persist_claim_verification(
            connection=connection_for(request),
            store_id=body.store_id,
            **payload,
        )
    except HTTPException:
        raise
    except (LookupError, ValueError) as exc:
        # An unmapped claim_type, a status outside the vocabulary, a malformed uuid: all of
        # them are the caller's payload being wrong, not this service being broken.
        #
        # LookupError is not decoration and was not guessed: `trust.scoring`'s vocabulary
        # refusals — UnmappedClaimType, UnknownObservationType, UnknownTrustDimension — all
        # derive from LookupError, NOT from ValueError (only InvalidObservationWeight is a
        # ValueError). Measured against a live TestClient: with `except ValueError` alone, a
        # request naming an unapproved claim_type escaped the handler and became a 500, i.e.
        # this service reporting the caller's bad payload as its own outage.
        raise HTTPException(422, {"error": "unverifiable_claim", "message": str(exc)}) from exc

    recorded = {
        "claim_id": str(outcome.claim_id) if outcome.claim_id is not None else None,
        "verification_id": (
            str(outcome.verification_id) if outcome.verification_id is not None else None
        ),
        "dim": outcome.dim,
        "observation_type": outcome.observation_type,
        "written": outcome.written,
        "score": outcome.score,
        "confidence": outcome.confidence,
    }
    if not outcome.written:
        # Already recorded. No second row, and no second event either — the ledger already
        # carries the announcement this call would have duplicated.
        return JSONResponse(status_code=200, content={"replayed": True, "verification": recorded})

    event = {
        "event_id": f"{CLAIM_VERIFIED_KIND}:{body.store_id}:{body.claim_ref}:{body.verifier_version}",
        "ts": body.observed_at,
        "kind": CLAIM_VERIFIED_KIND,
        "store_id": body.store_id,
        "payload": {
            "store_id": body.store_id,
            "claim_ref": body.claim_ref,
            "claim_type": body.claim_type,
            "status": body.status,
            "dim": outcome.dim,
            "confidence": body.confidence,
            "verifier_version": body.verifier_version,
            "catalog_snapshot_id": body.catalog_snapshot_id,
        },
    }
    # NOTE the absence of `verification_id` from that payload, which is deliberate and was
    # measured. It is `gen_random_uuid()`-derived, so it differs per write; D16 makes
    # `event_id` the idempotency key and requires that re-sending an id re-sends the SAME
    # event, so putting a server-generated id in the body makes the event content
    # irreproducible and any second append of it a 409 rather than a no-op. Everything in
    # this payload is a pure function of the request, which is what makes the announcement
    # replayable. The verification_id is returned to the caller below instead.
    try:
        appended = append(store_for(request), event)
    except EventServiceError as exc:
        from ..events.routes import _refuse

        raise _refuse(exc) from exc

    return {
        "replayed": False,
        "verification": recorded,
        "event": {
            "event_id": event["event_id"],
            "kind": CLAIM_VERIFIED_KIND,
            "inserted": bool(getattr(appended, "inserted", False)),
        },
    }
