"""``GET/PUT /stores/{store_id}/envelope`` and ``POST /stores/{store_id}/kill``.

Discovered and mounted by the frozen ``merchant_svc.main.create_app`` because this file is
``<feature>/routes.py`` and exports ``router``. The three paths and their shapes come from
``packages/contracts/openapi/merchant.openapi.json``, which
``test_the_served_routes_match_the_pinned_contract`` grades this table against.

**These routes are administrative.** An envelope is sealed state (S7): it is the merchant's
floor prices, their discount ceiling and their monthly give-away budget. Reading one
anonymously hands a competitor the store's whole negotiating position, and writing one
anonymously sets it. They therefore sit behind the same bearer token
``GET /install/shops`` does, and refuse every caller until it is configured.

**Activation over HTTP.** The pinned contract has no route for approving an envelope and no
field on the ``Envelope`` body to carry an approval artifact, so the artifact arrives in the
``X-Envelope-Approval`` header as JSON. A PUT whose body asks for ``activation: "active"``
without one is stored in ``shadow``: the body is the merchant's *request*, and the approval
is the only thing that grants it. This is written down in the ticket's report as a contract
gap rather than smuggled in — see the module's tests for the behaviour it pins.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from merchant_svc.envelope import (
    ACTIVE,
    ENVELOPES,
    ApprovalRejected,
    EnvelopeError,
    EnvelopeInvalid,
    StoreMismatch,
    UnknownStore,
    VersionWentBackwards,
)
from merchant_svc.http_limits import BodyTooLarge, read_capped_body
from merchant_svc.install.routes import ADMIN_TOKEN_ENV
from merchant_svc.install.signatures import secure_equals

_log = logging.getLogger(__name__)

router = APIRouter()

#: The header a written approval artifact arrives in. See the module docstring.
APPROVAL_HEADER = "X-Envelope-Approval"


def _problem(status: int, reason: str, **detail: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": reason, **detail})


def _refuse_unless_admin(request: Request) -> JSONResponse | None:
    """``None`` when the caller may touch a store's sealed envelope; a refusal otherwise.

    Deliberately a copy of the rule ``merchant_svc.install.routes`` applies rather than a
    call into its private helper: the constant is shared, so the two cannot drift on *which*
    token, and an unconfigured deployment refuses here exactly as it does there.
    """
    configured = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
    if not configured:
        return _problem(
            503,
            "admin-api-not-configured",
            missing=[ADMIN_TOKEN_ENV],
            detail="the envelope routes refuse every caller until a token is configured",
        )
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not secure_equals(configured, supplied.strip()):
        return _problem(401, "unauthorized")
    return None


def _submitted_approval(request: Request) -> Any:
    """The written approval artifact this request carries, or ``None``.

    Raises:
        ValueError: the header is present but is not a JSON object.
    """
    raw = request.headers.get(APPROVAL_HEADER)
    if raw is None or not raw.strip():
        return None
    artifact = json.loads(raw)
    if not isinstance(artifact, dict):
        raise ValueError(
            f"{APPROVAL_HEADER} must carry a JSON object, got {type(artifact).__name__}"
        )
    return artifact


@router.get("/stores/{store_id}/envelope")
async def read_envelope(store_id: str, request: Request) -> Any:
    """The store's current envelope version, as the pinned DESIGN document."""
    refusal = _refuse_unless_admin(request)
    if refusal is not None:
        return refusal
    try:
        envelope = ENVELOPES.current(store_id)
    except UnknownStore:
        return _problem(404, "no-envelope", store_id=store_id)
    return JSONResponse(content=envelope.to_contract().model_dump(mode="json"))


@router.put("/stores/{store_id}/envelope")
async def write_envelope(store_id: str, request: Request) -> Any:
    """Replace the store's envelope terms, creating a new version.

    The submitted ``version`` is ignored — it is derived from what is already on file, so a
    stale client cannot reinstate limits the merchant has already replaced. The submitted
    ``activation`` is honoured **only** when the request also carries a written approval
    artifact bound to the terms being stored; otherwise the new version is ``shadow``.
    """
    refusal = _refuse_unless_admin(request)
    if refusal is not None:
        return refusal

    try:
        raw = await read_capped_body(request)
    except BodyTooLarge:
        return _problem(413, "body-too-large")
    try:
        submitted = json.loads(raw)
    except Exception:  # noqa: BLE001 - any decode failure is the same 400, never a 500
        return _problem(400, "unparseable-body")
    if not isinstance(submitted, dict):
        return _problem(400, "not-an-envelope", detail=f"got a {type(submitted).__name__}")

    try:
        approval = _submitted_approval(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _problem(400, "unreadable-approval", detail=str(exc))

    try:
        stored = ENVELOPES.put(store_id, submitted)
    except StoreMismatch as exc:
        return _problem(409, "wrong-store", detail=str(exc))
    except VersionWentBackwards as exc:
        return _problem(409, "version-went-backwards", detail=str(exc))
    except EnvelopeInvalid as exc:
        return _problem(400, "not-an-envelope", detail=str(exc))

    if submitted.get("activation") == ACTIVE:
        try:
            stored = ENVELOPES.activate(store_id, approval)
        except ApprovalRejected as exc:
            # The new version is already on file, in shadow. That is the safe half of the
            # request, and losing it would leave the merchant's edit unsaved as well as
            # unapproved. The refusal names the missing paperwork, not the terms.
            _log.info("envelope v%d for %r stays in shadow: %s", stored.version, store_id, exc)
            return _problem(
                403,
                "approval-required",
                store_id=store_id,
                version=stored.version,
                activation=stored.activation,
                detail=str(exc),
            )

    return JSONResponse(content=stored.to_contract().model_dump(mode="json"))


@router.post("/stores/{store_id}/kill")
async def kill_store_agent(store_id: str, request: Request) -> Any:
    """Kill switch: move the store's envelope to ``killed`` immediately."""
    refusal = _refuse_unless_admin(request)
    if refusal is not None:
        return refusal
    try:
        killed = ENVELOPES.kill(store_id)
    except UnknownStore:
        return _problem(404, "no-envelope", store_id=store_id)
    except EnvelopeError as exc:  # pragma: no cover - kill refuses nothing else
        return _problem(409, "kill-refused", detail=str(exc))
    _log.warning("store %r envelope v%d killed", store_id, killed.version)
    return JSONResponse(content={"store_id": store_id, "activation": killed.activation})
