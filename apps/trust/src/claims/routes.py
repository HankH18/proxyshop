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

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from contracts.ledger import validate_ledger_payload
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..events.errors import EventServiceError, StoreUnavailable
from ..events.store import append
from ..verification import persist_claim_verification

__all__ = ["ClaimVerificationIn", "connection_for", "router", "store_for"]

router = APIRouter(prefix="/claims", tags=["trust"])

#: The migration that owns the CHECK vocabularies this route validates against.
_MIGRATION = Path(__file__).resolve().parents[4] / "db" / "migrations" / "0002_ledger_tables.sql"

#: The ledger kind a recorded verification announces. Frozen vocabulary — see
#: ``trust.events.LEDGER_EVENT_KINDS`` and ``commerce_events_kind_check``.
CLAIM_VERIFIED_KIND = "claim_verified"


#: The provenance vocabulary ``claims_provenance_source_check`` pins. Duplicated here ONLY as
#: a fallback: :func:`_vocabularies` reads the migration itself, and this is what a deployment
#: that ships without ``db/migrations`` falls back to so the route still refuses a value
#: Postgres would reject with a 500 instead of a 422.
_FALLBACK_PROVENANCE = (
    "scraped",
    "pixel_feed",
    "owner_statement",
    "envelope_rule",
    "learned_policy",
    "network",
    "seller_asserted",
)


def _vocabularies() -> tuple[frozenset[str], frozenset[str]]:
    """``(verification statuses, provenance sources)``, read from the authorities that own them.

    The statuses come from the verifier package, the provenance sources from the migration's
    own CHECK constraint. Both are read rather than typed, because a second copy of a
    vocabulary is a second thing to drift — and where a copy is unavoidable (see
    :data:`_FALLBACK_PROVENANCE`) it is a fallback, never the first answer.
    """
    import re

    statuses: frozenset[str] = frozenset()
    try:
        from claim_verification import VERIFICATION_STATUSES

        statuses = frozenset(VERIFICATION_STATUSES)
    except Exception:  # noqa: BLE001 - the verifier is not on every deployment's path
        statuses = frozenset()

    provenance = frozenset(_FALLBACK_PROVENANCE)
    migration = _MIGRATION
    if migration.is_file():
        found = re.search(
            r"provenance_source\s+in\s*\(([^)]*)\)", migration.read_text(encoding="utf-8"), re.I
        )
        if found:
            parsed = frozenset(re.findall(r"'([^']+)'", found.group(1)))
            if parsed:
                provenance = parsed
    return statuses, provenance


class ClaimVerificationIn(BaseModel):
    """One verification outcome, as the verifier decided it.

    Every field the migration marks NOT NULL without a DEFAULT is required here too, and the
    two fields the migration additionally constrains by CHECK — ``status`` and
    ``provenance_source`` — are validated against the vocabularies that own them. Without
    that, a value only Postgres refuses arrives as a 500 from deep inside the writer instead
    of a 422 naming the field, which is what a caller can act on.
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

    @field_validator("status")
    @classmethod
    def _status_is_published(cls, value: str) -> str:
        statuses, _ = _vocabularies()
        if statuses and value not in statuses:
            raise ValueError(
                f"status {value!r} is not a published verification status "
                f"({sorted(statuses)}); claim_verifications_status_check would refuse it"
            )
        return value

    @field_validator("provenance_source")
    @classmethod
    def _provenance_is_published(cls, value: str | None) -> str | None:
        _, provenance = _vocabularies()
        if value is not None and value not in provenance:
            raise ValueError(
                f"provenance_source {value!r} is not in the approved vocabulary "
                f"({sorted(provenance)}); claims_provenance_source_check would refuse it"
            )
        return value


def store_for(request: Request) -> Any:
    """The event store this application appends the ``claim_verified`` event to.

    Delegates to :func:`trust.events.routes.store_for` rather than resolving its own, so this
    route and ``/events`` write to the SAME chain. Two resolvers would be two chains.
    """
    from ..events.routes import store_for as events_store_for

    return events_store_for(request)


@contextmanager
def connection_for(request: Request) -> Iterator[Any]:
    """The database connection the verification rows are written on. ONE PER REQUEST.

    Resolution order: whatever was injected on ``app.state.ledger_connection``, otherwise one
    opened against the DSN the ledger writer itself resolved — and closed again when this
    request is done. The DSN comes from :class:`~..events.pg.PostgresEventStore` rather than
    from a second walk of the environment on purpose: ``DEFAULT_DSN_ENV``'s ORDER is the
    thing T-151 got wrong and T-181/T-289 are still trying to defend, and a second resolver
    here would be a second place for that order to drift.

    PER-REQUEST, and this is the correction of a measured defect rather than a preference.
    An earlier version cached one ``autocommit=False`` connection on ``app.state`` forever.
    Two things followed, both measured against a live database:

    * one failing statement left it in ``InFailedSqlTransaction``, and since nothing rolled
      it back, EVERY later request died on it — one bad request permanently bricked the
      endpoint; and
    * this handler is a plain ``def``, so FastAPI runs it in a threadpool and concurrent
      requests shared a single transaction: one request's commit committed another's
      half-written rows.

    The sibling writer in this same service already does it this way —
    ``PostgresEventStore._connection()`` takes a connection per operation — so this is the
    house pattern, not an invention. An injected connection is handed back untouched and
    NOT closed: whoever injected it owns its lifetime.
    """
    injected = getattr(request.app.state, "ledger_connection", None)
    if injected is not None:
        yield injected
        return

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
    try:
        yield connection
    finally:
        connection.close()


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
        with connection_for(request) as connection:
            outcome = persist_claim_verification(
                connection=connection,
                store_id=body.store_id,
                **payload,
            )
    except HTTPException:
        raise
    except (LookupError, ValueError) as exc:
        # An unmapped claim_type or a weight out of range: the caller's payload being wrong,
        # not this service being broken.
        #
        # LookupError is not decoration and was not guessed: `trust.scoring`'s vocabulary
        # refusals — UnmappedClaimType, UnknownObservationType, UnknownTrustDimension — all
        # derive from LookupError, NOT from ValueError (only InvalidObservationWeight is a
        # ValueError). Measured against a live TestClient: with `except ValueError` alone, a
        # request naming an unapproved claim_type escaped the handler and became a 500, i.e.
        # this service reporting the caller's bad payload as its own outage.
        raise HTTPException(422, {"error": "unverifiable_claim", "message": str(exc)}) from exc
    except Exception as exc:
        # And the ones the clause above CANNOT see, which is most of them: `psycopg.Error`
        # derives directly from `Exception`, so a CHECK violation, an unknown
        # catalog_snapshot_id (a real FK onto ledger.catalog_snapshots), a malformed uuid and
        # a bad provenance value ALL escaped as 500s. Measured — four of five bad payloads
        # became "this service is broken" when they were "your request is wrong".
        from ..events.pg import classify_connection_error

        unavailable = classify_connection_error(exc)
        if unavailable is not None:
            raise HTTPException(
                503, {"error": "store_unavailable", "message": str(unavailable)}
            ) from exc

        import psycopg

        if isinstance(exc, psycopg.Error):
            raise HTTPException(422, {"error": "unverifiable_claim", "message": str(exc)}) from exc
        raise

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

    # The event id carries EVERY component of the database's own idempotency key —
    # `claim_verifications_idempotency_key UNIQUE (claim_id, catalog_snapshot_id,
    # verifier_version)`. Omitting catalog_snapshot_id was a measured defect: the migration
    # says in as many words that "a snapshot bump is a different row and therefore
    # re-verifies", so a legitimate re-verification against a new snapshot wrote its rows,
    # committed them, and then hit a 409 appending an event id it had already used — leaving
    # rows in the tables that the ledger does not corroborate, which is exactly the
    # divergence the persist-then-announce order exists to prevent, in the mirror direction.
    event_payload: dict[str, Any] = {
        "store_id": body.store_id,
        "claim_ref": body.claim_ref,
        "claim_type": body.claim_type,
        "status": body.status,
        "dim": outcome.dim,
        "confidence": body.confidence,
        "verifier_version": body.verifier_version,
        "catalog_snapshot_id": body.catalog_snapshot_id,
    }
    event: dict[str, Any] = {
        "event_id": (
            f"{CLAIM_VERIFIED_KIND}:{body.store_id}:{body.claim_ref}"
            f":{body.catalog_snapshot_id}:{body.verifier_version}"
        ),
        "ts": body.observed_at,
        "kind": CLAIM_VERIFIED_KIND,
        "store_id": body.store_id,
        "payload": event_payload,
    }
    # NOTE the absence of `verification_id` from that payload, which is deliberate and was
    # measured. It is `gen_random_uuid()`-derived, so it differs per write; D16 makes
    # `event_id` the idempotency key and requires that re-sending an id re-sends the SAME
    # event, so putting a server-generated id in the body makes the event content
    # irreproducible and any second append of it a 409 rather than a no-op. Everything in
    # this payload is a pure function of the request, which is what makes the announcement
    # replayable. The verification_id is returned to the caller below instead.
    # Validate before appending, the way apps/exchange/src/auction/ledger.py,
    # apps/merchant/svc/src/codes/ledger.py, apps/buyer/svc/src/feedback/submission.py and
    # apps/exchange/src/retrieval/fit.py all do at their own producing boundaries. A producer
    # that skips this is how a malformed payload reaches a written row — see T-332/T-333,
    # which measure exactly that for the one trust producer that does not validate. This
    # route is not going to be the second one.
    problems = validate_ledger_payload(CLAIM_VERIFIED_KIND, event_payload)
    if problems:
        raise HTTPException(
            422,
            {
                "error": "invalid_event_payload",
                "message": (
                    f"the claim_verified payload this route built does not satisfy the "
                    f"published shape: {problems}"
                ),
            },
        )

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
