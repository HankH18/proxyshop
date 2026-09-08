"""``GET/PUT /stores/{store_id}/envelope``, ``POST /stores/{store_id}/kill``, ``…/revive``.

Discovered and mounted by the frozen ``merchant_svc.main.create_app`` because this file is
``<feature>/routes.py`` and exports ``router``. The four paths and their shapes come from
``packages/contracts/openapi/merchant.openapi.json``, which
``test_the_served_routes_match_the_pinned_contract`` grades this table against.

**The two switch routes are asymmetric on purpose.** ``kill`` needs no artifact and no state
check — stopping is always allowed. ``revive`` needs none either, because all it reaches is
``shadow``; what it deliberately cannot do is reach ``active``, so a stopped store comes back
un-stopped and still silent until the merchant approves its terms through the same gate every
other live envelope came through. See :func:`revive_store_agent`.

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

**THREE REPRESENTATIONS OF ONE RESOURCE, AND WHY THERE IS NO FOURTH ROUTE (R6).**
``PUT /stores/{id}/envelope`` means *put this store's envelope into the state I describe*, and
a merchant can describe that state in three ways. All three end in the same two objects —
``EnvelopeVersions.put`` and ``EnvelopeVersions.activate`` — so there is exactly one door to
the approval gate and nothing goes around it:

``{"store_id": …, "floors": …, …}``
    the DESIGN Envelope document. What the dashboard's envelope editor sends. Unchanged.

``{"turns": [...], "completed_at": …}``
    a completed plain-language onboarding **interview**. This is R6's missing door: before it,
    the only way to turn an interview into an envelope was ``python -m merchant_svc.onboarding``
    on an operator's laptop, and the merchant's own page told them to call a route
    (``POST /stores/{store_id}/envelope``) that does not exist. The turns the client sends back
    are the ones ``GET /stores/{id}/dashboard`` served it, so no cluster id, product ref,
    commitment key or claim type is ever authored in a browser. Always lands in ``shadow`` —
    :func:`~merchant_svc.onboarding.flow.envelope_from_transcript` will not produce anything
    else, and ``put`` forces it again.

``{"activation": "active"}`` — terms omitted
    **activate the version already on file**, against the ``X-Envelope-Approval`` artifact.
    This exists because activation must not mint a version: the digest the merchant signed
    covers *one* document (``version`` is inside it), so a form that re-submitted the terms
    would create v2 and invalidate the approval collected for v1 — the exact edit-after-approve
    substitution :func:`~merchant_svc.envelope.versions.activate_envelope` refuses. Before this,
    the only callers who could activate over HTTP were ones that computed the canonical digest
    of a version *that did not exist yet*, in Python, in-process. A browser cannot.

The discriminator is deliberately made of fields, not of a mode flag: a body naming any
approved term is an envelope, a body naming turns is an interview, a body naming neither and
asking for ``active`` is an activation. A body that looks like two of them is refused rather
than guessed at, because guessing which one a merchant meant is guessing at their limits.
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
    SHADOW,
    ApprovalRejected,
    EnvelopeError,
    EnvelopeInvalid,
    StoreMismatch,
    UnknownStore,
    VersionWentBackwards,
)
from merchant_svc.envelope.model import ENVELOPE_FIELDS
from merchant_svc.http_limits import BodyTooLarge, read_capped_body
from merchant_svc.install.routes import ADMIN_TOKEN_ENV
from merchant_svc.install.signatures import secure_equals
from merchant_svc.onboarding.flow import envelope_from_transcript
from merchant_svc.onboarding.interview import TranscriptRejected

_log = logging.getLogger(__name__)

router = APIRouter()

#: The header a written approval artifact arrives in. See the module docstring.
APPROVAL_HEADER = "X-Envelope-Approval"

#: The keys that make a body an onboarding **interview** rather than an envelope. The first
#: four are the spellings ``merchant_svc.onboarding.interview.read_transcript`` accepts; the
#: fifth is the wrapper ``fixtures/interviews/`` uses and ``python -m merchant_svc.onboarding``
#: unwraps, admitted here so the document a merchant is handed by support is the document this
#: route takes.
_INTERVIEW_KEYS: frozenset[str] = frozenset(
    {"turns", "transcript", "messages", "dialogue", "interview"}
)

#: The keys that make a body an **envelope document**. Any one of them is enough: a partial
#: envelope must be refused as a bad envelope (naming the field that is wrong) rather than
#: silently re-read as some other representation.
_ENVELOPE_KEYS: frozenset[str] = frozenset(ENVELOPE_FIELDS) - {"activation"}


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


def _representation(submitted: dict[str, Any]) -> str:
    """Which of the three bodies this is: ``"envelope"``, ``"interview"`` or ``"activation"``.

    Raises:
        ValueError: the body is two of them at once, or none of them. Both are refused rather
            than resolved: a document that is half an envelope and half an interview says two
            different things about the merchant's limits, and picking one is picking their
            limits for them.
    """
    keys = {str(key) for key in submitted}
    if submitted.get("approval") is not None:
        # Refused loudly rather than ignored. An artifact in the BODY is the one shape that
        # would look like it worked while being dropped on the floor — ``EnvelopeVersions.put``
        # passes ``approval=None`` and the field simply vanishes — and "the request said it was
        # approved" is exactly the say-so R6 refuses. The artifact travels in a header.
        #
        # Keyed on the VALUE, not on the key. ``Envelope.to_dict()`` emits ``"approval": None``
        # by design ("the recorded artifact is part of what this version *is*"), so every
        # client that round-trips an envelope through it sends the key. Refusing on the key
        # rejected three of this suite's own honest requests — measured, not imagined — which
        # is the same shape of over-broad validator that once refused 538 of 558 bids.
        raise ValueError(
            f"a written approval artifact is not part of an envelope body; send it in the "
            f"{APPROVAL_HEADER} header, which is the only channel that activates anything"
        )
    looks_like_interview = bool(keys & _INTERVIEW_KEYS)
    looks_like_envelope = bool(keys & _ENVELOPE_KEYS)
    if looks_like_interview and looks_like_envelope:
        raise ValueError(
            "the body carries both interview turns and envelope terms; it is one or the "
            f"other, never both: {sorted(keys)}"
        )
    if looks_like_interview:
        return "interview"
    if looks_like_envelope:
        return "envelope"
    if submitted.get("activation") == ACTIVE:
        return "activation"
    raise ValueError(
        "the body is neither an envelope (it names none of "
        f"{sorted(_ENVELOPE_KEYS)}), nor an onboarding interview (none of "
        f"{sorted(_INTERVIEW_KEYS)}), nor a request to activate the version on file "
        f'({{"activation": "{ACTIVE}"}}): {sorted(keys)}'
    )


def _envelope_from_interview(submitted: dict[str, Any]) -> dict[str, Any]:
    """The version-1 envelope a submitted interview produces, as a document ``put`` accepts.

    Raises:
        TranscriptRejected: the interview is unreadable, skipped a question, or answered one
            in a way nothing here can read. Every one of those is a refusal rather than a
            default — see :func:`~merchant_svc.onboarding.flow.envelope_from_transcript`, and
            note that the message is merchant-facing prose ("the price-floor answer for the
            whole store names neither a price nor a refusal"), which is why it is echoed.
    """
    transcript: Any = submitted.get("interview") or submitted
    envelope = envelope_from_transcript(transcript)
    # NOT honoured here and deliberately re-derived by `put`: the version, and the activation.
    # `envelope_from_transcript` already forces SHADOW, and this is the second of the two
    # places that force it — R7 is not a property one function happens to have.
    return dict(envelope.to_dict(), activation=SHADOW)


@router.put("/stores/{store_id}/envelope")
async def write_envelope(store_id: str, request: Request) -> Any:
    """Set the store's envelope: from terms, from an onboarding interview, or activate it.

    The submitted ``version`` is ignored — it is derived from what is already on file, so a
    stale client cannot reinstate limits the merchant has already replaced. The submitted
    ``activation`` is honoured **only** when the request also carries a written approval
    artifact bound to the terms being stored; otherwise the new version is ``shadow``.

    See the module docstring for the three body representations and why activation is one of
    them rather than a fourth route.
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
        representation = _representation(submitted)
    except ValueError as exc:
        return _problem(400, "not-an-envelope", detail=str(exc))

    terms: dict[str, Any] | None = submitted
    if representation == "interview":
        try:
            terms = _envelope_from_interview(submitted)
        except TranscriptRejected as exc:
            # A merchant-facing refusal, echoed verbatim: it names the question and the
            # sentence that could not be read, which is the only thing that tells them what
            # to type instead. An interview that half-parsed would leave a wall unset, and a
            # wall nobody set reads as "no wall".
            return _problem(400, "unreadable-interview", store_id=store_id, detail=str(exc))
    elif representation == "activation":
        # Nothing to store: this body describes no terms. The version already on file is the
        # one the merchant signed for, and minting another would invalidate their approval.
        terms = None

    if terms is not None:
        try:
            stored = ENVELOPES.put(store_id, terms)
        except StoreMismatch as exc:
            return _problem(409, "wrong-store", detail=str(exc))
        except VersionWentBackwards as exc:
            return _problem(409, "version-went-backwards", detail=str(exc))
        except EnvelopeInvalid as exc:
            return _problem(400, "not-an-envelope", detail=str(exc))
    else:
        try:
            stored = ENVELOPES.current(store_id)
        except UnknownStore:
            return _problem(
                404,
                "no-envelope",
                store_id=store_id,
                detail=(
                    "there is no version to activate; answer the onboarding interview first "
                    "(GET /stores/{store_id}/dashboard serves it)"
                ),
            )

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


@router.post("/stores/{store_id}/revive")
async def revive_store_agent(store_id: str, request: Request) -> Any:
    """Lift the kill switch: bring a stopped store back to ``shadow``, still not bidding.

    **The second half of R9's kill switch, and why it is a separate door.** The switch used to
    be a one-way one: ``PUT`` on a killed store minted a version that was born killed and the
    approval that would activate it was refused, so a merchant who stopped their agent could
    never start it again. Reversing that could have been done by letting an approval lift the
    killed state — and was not, on two measured grounds:

    * :func:`~merchant_svc.envelope.versions.kill_envelope` keeps the approval that was on
      file, so at the instant of the kill an artifact bound to those exact terms already
      exists. Letting an approval clear ``killed`` would mean the paperwork signed *before*
      the stop undoes the stop, and any client holding that artifact — a retry, a queued
      request, a console tab left open — resumes the store by replaying it. That is the
      "silently resumes" failure, arriving through the front door.
    * it would put the reversal inside ``activate_envelope``'s killed branch, which is where
      the kill switch's teeth are. They are still there and still refuse every approval; this
      route is the only thing in the service that clears ``killed``, and it clears it to
      ``shadow``.

    So restarting is two deliberate acts by two different doors: **this one un-stops the
    store, and the ordinary approval activates it.** Nothing here puts a store back on the
    network — what comes back carries no approval, ``may_bid`` is still ``False``, and the
    agent still answers ``store_killed``'s sibling refusal until the merchant signs the terms
    again. Reviving a store that is not killed is refused rather than absorbed: on a live
    envelope it would be a deactivation nobody could see in the kill-switch card.

    It takes the same admin bearer as the kill, and no approval artifact of its own —
    ``shadow`` is the state a PUT already reaches with no paperwork, so demanding an approval
    to arrive at it would invent a second meaning for the one artifact this service checks.
    """
    refusal = _refuse_unless_admin(request)
    if refusal is not None:
        return refusal
    try:
        revived = ENVELOPES.revive(store_id)
    except UnknownStore:
        return _problem(404, "no-envelope", store_id=store_id)
    except EnvelopeError as exc:
        # `ReviveRefused` — the store is not killed. 409 rather than 400: the request is
        # well formed and the state is what refuses it, which is the same shape as the kill
        # switch's own refusal and reads the same way to a client.
        return _problem(409, "revive-refused", detail=str(exc))
    _log.warning(
        "store %r envelope v%d revived to %s; it is NOT live until a written approval is "
        "recorded for these terms",
        store_id,
        revived.version,
        revived.activation,
    )
    return JSONResponse(content={"store_id": store_id, "activation": revived.activation})
