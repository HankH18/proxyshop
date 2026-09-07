"""HTTP surface for R1's clarification loop (T-071).

Discovered and mounted by the frozen :func:`buyer_svc.main.create_app`.

Two routes, and the split between them **is** the R1 invariant::

    POST /buyer/intent/clarify   {"turns": [...]}                  -> 200 questions+intent
    POST /buyer/intent/confirm   {"intent": {...}, "confirmed": true} -> 201 {auction_id}

``/clarify`` has no auction client in scope. Not "does not call one" — the handler cannot
reach one: :func:`buyer_svc.intent.clarify` takes no such parameter and this module does
not look the client up on that path at all. A reviewer does not have to trust a branch.

``/confirm`` is the only handler that touches the exchange, and it refuses a body whose
``confirmed`` is not exactly ``true``. The field is typed ``bool``, so FastAPI rejects
``"maybe"`` at the wire with a 422 before any handler code runs, and
:func:`buyer_svc.intent.confirm` re-checks ``confirmed is True`` behind it. Two layers,
because the wire layer is the one a future refactor most easily loosens.

Wiring
------
The auction client is read from ``app.state.auction_client`` and is **not** constructed
here: a buyer service that mints its own exchange client cannot be pointed at a stub, and
a deployment that forgot to wire one should hear about it as a 503 rather than discover it
when the first buyer confirms. The LLM client is resolved lazily through
:func:`buyer_llm`, defaulting to ``build_llm("buyer")`` — which is D20's offline double
unless ``LLM_PROVIDER`` says otherwise — and a loop with no model still works.

**Who sets that attribute:** :mod:`buyer_svc.composition`, from the deployment document in
``BUYER_DEPLOYMENT`` / ``BUYER_DEPLOYMENT_JSON``, through the request-time hook
:func:`_bind_the_deployment` below. ``create_app`` cannot do it — ``main.py`` is
orchestrator-frozen (B6(iii)) — and a service with no document configured binds nothing and
answers exactly the 503 it answered before that module existed. ``/clarify`` does **not**
take the hook: R1's property is that this handler cannot reach an auction client, and a
composition hook in it would be an auction client in its scope.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, StrictBool

from ..composition import (
    MAX_ROSTER_ENTRIES,
    DeploymentConfigurationError,
    ExchangeCallFailed,
    ensure_configured,
)
from ._spellings import bind_spellings
from .clarifier import clarify
from .confirmation import confirm
from .errors import (
    AuctionClientUnusable,
    ConfirmationWithheld,
    IntentAlreadyConfirmed,
    IntentError,
    IntentTooLarge,
    UnstructuredIntent,
)
from .models import (
    MAX_CLARIFYING_QUESTIONS,
    MAX_FREE_TEXT_CHARS,
    MAX_IDENTIFIER_LENGTH,
    MAX_INTENT_BYTES,
    payload_weight,
)

_log = logging.getLogger(__name__)

#: Starlette 0.5x renamed ``HTTP_422_UNPROCESSABLE_ENTITY`` to ``..._CONTENT`` and emits a
#: DeprecationWarning on the old name. Resolved once, here, so this module names neither
#: spelling twice and works on both.
_HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

#: Starlette renamed ``HTTP_413_REQUEST_ENTITY_TOO_LARGE`` to ``HTTP_413_CONTENT_TOO_LARGE``
#: in the same sweep. Resolved once, here, for the same reason.
_HTTP_413 = getattr(status, "HTTP_413_CONTENT_TOO_LARGE", 413)

__all__ = [
    "AUCTION_CLIENT_ATTR",
    "LLM_ROLE",
    "MAX_CLARIFY_TURNS",
    "MAX_CONFIRM_BODY_BYTES",
    "MAX_PROFILE_BYTES",
    "MAX_VALIDATION_ERRORS",
    "MAX_VALIDATION_MESSAGE_CHARS",
    "ClarifyBody",
    "ClarifyResponse",
    "ConfirmBody",
    "ConfirmResponse",
    "buyer_llm",
    "router",
    "set_buyer_llm",
]

#: The most of FastAPI's own field errors one refusal will carry.
#:
#: A body with 5 000 bad fields is 5 000 error objects, each with a location and a message,
#: and the caller only ever needed to be told the request was unprocessable. Twenty is more
#: than a human debugging a client ever reads at once.
MAX_VALIDATION_ERRORS = 20

#: The most characters of one of those messages this router passes on. Pydantic's messages
#: are short sentences ("Input should be a valid list"); this is a ceiling on a string this
#: service did not write, not a budget it expects to spend.
MAX_VALIDATION_MESSAGE_CHARS = 200


def _validation_detail(exc: RequestValidationError) -> list[dict[str, Any]]:
    """FastAPI's 422 body, minus the ``input`` it normally quotes back.

    Two reasons, and BOTH were measured on this unauthenticated door.

    1. The default body embeds the offending value under ``input``, so a hostile field comes
       straight back at full length — which makes refusing cost this service exactly what
       accepting it would have, and hands an amplifier to anyone who wants one.
    2. That echo is not always encodable. ``jsonable_encoder`` walks the value recursively,
       so ``{"intent": [[[[...]]]]}`` nested 2 000 deep — a 4 KB body — raised
       ``RecursionError`` **inside the 422 handler** and answered HTTP 500; and ``1e400``,
       legal RFC-8259 JSON that parses to ``inf``, raised ``ValueError: Out of range float
       values are not JSON compliant`` for the same reason. A validation refusal that the
       body can turn into a server fault is not a refusal.

    What survives is what a caller can act on: where the problem is and what it is.
    """
    detail: list[dict[str, Any]] = []
    for error in exc.errors()[:MAX_VALIDATION_ERRORS]:
        detail.append(
            {
                "loc": [str(part)[:MAX_IDENTIFIER_LENGTH] for part in error.get("loc", ())],
                "msg": str(error.get("msg", ""))[:MAX_VALIDATION_MESSAGE_CHARS],
                "type": str(error.get("type", ""))[:MAX_IDENTIFIER_LENGTH],
            }
        )
    return detail


class _BoundedBodyRoute(APIRoute):
    """Answer a body this service cannot even PARSE as the caller's 4xx, never as a 500.

    Starlette reads and decodes the JSON body **inside** the route handler, before any
    dependency, any validator and any line of this module runs — so nothing declared here can
    be reached in time to refuse it. Measured on this branch: a 4 KB body of 2 000 nested
    arrays raised ``RecursionError`` out of ``await request.json()`` and answered **HTTP 500**
    on this unauthenticated door. Depth is not size, so no byte ceiling closes it; catching
    the parser's own refusal here does.

    A pre-parse ceiling on the raw body belongs further out still — in the ASGI stack or in
    ``buyer_svc.main``, which is orchestrator-frozen (B6(iii)) — so this closes the answer,
    not the allocation.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Answered HERE rather than by the app-level handler, which is where the
                # echo — and the two 500s it caused — live. `buyer_svc.main` is
                # orchestrator-frozen (B6(iii)), so this router carries its own.
                return JSONResponse(
                    status_code=_HTTP_422, content={"detail": _validation_detail(exc)}
                )
            except RecursionError:
                # `from None`: the context is thousands of identical parser frames and is
                # not information about this request.
                raise HTTPException(
                    status_code=_HTTP_413,
                    detail=(
                        "this body is nested more deeply than this service will parse. "
                        "None of it is quoted back."
                    ),
                ) from None

        return bounded


router = APIRouter(prefix="/buyer/intent", tags=["buyer-intent"], route_class=_BoundedBodyRoute)

#: ``llm.config.ROLE_BUYER``. Spelled rather than imported so this module stays importable
#: with only FastAPI available; :func:`buyer_llm` validates it against the real roster the
#: moment it resolves a client, so a typo here surfaces as a logged failure and a
#: model-free loop, never as a silent wrong-model call.
LLM_ROLE = "buyer"

#: Where the exchange client lives on the app. Set it in your composition root.
AUCTION_CLIENT_ATTR = "auction_client"

#: The most bytes one ``POST /confirm`` body may weigh, over all three of its halves.
#:
#: Neither of these doors takes a credential, so the size of what an anonymous caller can
#: make this service hold is a number somebody has to choose (T-368). The intent has its own
#: 32 KiB budget (:data:`~buyer_svc.intent.models.MAX_INTENT_BYTES`); a roster is at most
#: :data:`~buyer_svc.composition.MAX_ROSTER_ENTRIES` rows of identifiers and prices, which
#: the deployment normally supplies rather than the browser; a profile is a pseudonym and a
#: handful of coarse buckets. 256 KiB covers all three several times over and is the same
#: ceiling the exchange puts on one store's whole bid reply
#: (``exchange.composition.MAX_BID_RESPONSE_BYTES``), so the two services agree about what
#: "one document" is worth.
#:
#: **What this bound is not.** The body is already parsed into Python objects by the time a
#: handler runs, so this refuses what is KEPT and forwarded, not what is allocated by the
#: JSON parse. A pre-parse ceiling belongs in the ASGI stack — ``buyer_svc.main`` is
#: orchestrator-frozen (B6(iii)), so it is not written here.
MAX_CONFIRM_BODY_BYTES = 256 * 1024

#: The most utterances one ``POST /clarify`` dialogue may carry.
#:
#: R1 caps the loop at three questions, so a dialogue is a handful of turns; the shipped
#: golden dialogues run to four. 64 is far more than any buyer types and far fewer than any
#: attack needs. The dialogue is also bounded in TOTAL length, at
#: :data:`~buyer_svc.intent.models.MAX_FREE_TEXT_CHARS`, because the clarifier joins the
#: buyer's need utterances into ``Intent.query`` verbatim: without that, ``/clarify`` would
#: happily mint an intent whose query is over the ceiling ``/confirm`` enforces, and hand the
#: buyer a confirmation screen this service refuses.
MAX_CLARIFY_TURNS = 64

#: The most bytes the ``profile`` half of a confirmation may weigh.
#:
#: Its own ceiling rather than a share of :data:`MAX_CONFIRM_BODY_BYTES`, because a profile is
#: not a document the buyer writes: T-070 mints it, and a ``BuyerProfile`` is two fields — a
#: pseudonym and the five coarse labels in ``buyer_svc.profile.BUCKET_KEYS``, whose category
#: slugs are themselves capped at ``CATEGORY_SLUG_MAX_CHARS`` (32) and ``CATEGORY_LIMIT`` (3).
#: A realistic one measures 197 bytes. 4 KiB is twenty times that and still refuses the shape
#: this bound exists for: an anonymous caller posting 30 KB of free text inside ``buckets`` and
#: having this service carry it to the exchange, which is neither a pseudonym nor a bucket.
MAX_PROFILE_BYTES = 4 * 1024


def _refuse_oversized_dialogue(turns: list[str]) -> None:
    """Bound what one anonymous clarification may cost, before any of it is read."""
    if len(turns) > MAX_CLARIFY_TURNS:
        raise HTTPException(
            status_code=_HTTP_413,
            detail=(
                f"this dialogue carries {len(turns)} turns; R1's loop asks at most "
                f"{MAX_CLARIFYING_QUESTIONS} questions and this service reads at most "
                f"{MAX_CLARIFY_TURNS} turns."
            ),
        )
    if payload_weight(turns, MAX_FREE_TEXT_CHARS) > MAX_FREE_TEXT_CHARS:
        raise HTTPException(
            status_code=_HTTP_413,
            detail=(
                f"this dialogue is longer than the {MAX_FREE_TEXT_CHARS} characters this "
                f"service will carry into an intent's query. None of it is quoted back."
            ),
        )


def _refuse_oversized_confirmation(body: ConfirmBody) -> None:
    """Bound the whole body before the deployment is read or the ledger is touched.

    The detail names the ceiling and the measurement and never the body: a 4xx that quotes a
    hostile document back costs this service what accepting it would have cost.
    """
    if body.roster is not None and len(body.roster) > MAX_ROSTER_ENTRIES:
        raise HTTPException(
            status_code=_HTTP_413,
            detail=(
                f"this confirmation carries {len(body.roster)} roster rows; the exchange "
                f"auctions at most {MAX_ROSTER_ENTRIES} and so does this service."
            ),
        )
    if (
        body.profile is not None
        and payload_weight(body.profile, MAX_PROFILE_BYTES) > MAX_PROFILE_BYTES
    ):
        raise HTTPException(
            status_code=_HTTP_413,
            detail=(
                f"this buyer profile weighs more than {MAX_PROFILE_BYTES} bytes; a "
                f"pseudonym and a handful of coarse buckets do not."
            ),
        )
    parts = [body.intent, body.profile, body.roster]
    if payload_weight(parts, MAX_CONFIRM_BODY_BYTES) > MAX_CONFIRM_BODY_BYTES:
        raise HTTPException(
            status_code=_HTTP_413,
            detail=(
                f"this confirmation weighs more than {MAX_CONFIRM_BODY_BYTES} bytes; one "
                f"shopping need, its buyer profile and its roster do not. The intent itself "
                f"is bounded separately at {MAX_INTENT_BYTES} bytes."
            ),
        )


_llm_override: Any = None


def set_buyer_llm(client: Any) -> None:
    """Inject the model client this router uses. ``None`` restores the default."""
    global _llm_override
    _llm_override = client


def buyer_llm() -> Any:
    """The buyer-role model client, or ``None`` when one cannot be built.

    Resolved lazily and defensively. The clarification loop degrades to its own wording
    and its own extraction when this returns ``None``, so a missing key, an unset provider
    or a broken import costs question phrasing — never the loop.
    """
    if _llm_override is not None:
        return _llm_override
    try:
        from llm import build_llm
    except ImportError:  # pragma: no cover - only with a broken sys.path
        _log.warning("packages/llm is not importable; clarifying without a model")
        return None
    try:
        return build_llm(LLM_ROLE)
    except Exception:  # noqa: BLE001 - a client we cannot build is a client we do without
        _log.warning("could not build the %r LLM client; clarifying without a model", LLM_ROLE)
        return None


class ClarifyBody(BaseModel):
    """The buyer's utterances so far, oldest first."""

    turns: list[str] = Field(min_length=1)


class ClarifyResponse(BaseModel):
    """What the buyer is shown, and what they are being asked.

    ``confirmed`` is here and is always ``false``: the client that renders this screen
    reads one field to know whether anything has been committed, rather than inferring it.
    """

    questions: list[str]
    intent: dict[str, Any]
    unresolved: list[str]
    #: There is deliberately NO field here for "must-haves this network cannot satisfy", and
    #: its absence is the correction rather than an omission. This service holds no
    #: candidates, so the only thing it could report is a guess from a catalogue CONFIG — and
    #: measured through ``POST /auctions``, that guess named the wrong constraints in both
    #: directions. Whether a stated must-have could be decided is an auction-wide fact, and
    #: the exchange answers it on the auction response's ``relaxed_constraints``.
    confirmed: bool = False


class ConfirmBody(BaseModel):
    """The intent the buyer just confirmed, and their confirmation."""

    intent: dict[str, Any]
    #: ``StrictBool``, not ``bool``, and this was MEASURED rather than reasoned about.
    #: Pydantic's default (lax) mode coerces the JSON strings ``"yes"``, ``"true"``,
    #: ``"on"`` and ``"1"`` into ``True``, so a plain ``bool`` field turned
    #: ``{"confirmed": "yes"}`` into a live auction: HTTP 201, one call to the exchange,
    #: for a body that never carried a boolean at all. ``StrictBool`` admits exactly
    #: ``true`` and ``false`` and answers 422 to everything else, which is what makes the
    #: wire layer a real guard rather than a second spelling of the same guess.
    confirmed: StrictBool = False
    profile: dict[str, Any] | None = None
    roster: list[dict[str, Any]] | None = None
    #: Bounded AND finite, and the ``allow_inf_nan`` half was MEASURED rather than reasoned
    #: about. ``1e400`` is legal RFC-8259 JSON that Python parses to ``inf``, so a plain
    #: ``float`` field accepted an INFINITE bid window on this unauthenticated door and
    #: forwarded it to the exchange — which holds the request open for R10's whole window:
    #: measured, HTTP 201 with ``bid_timeout_seconds: inf`` in the payload it sent. ``gt=0.0``
    #: does not catch that on its own, because ``inf > 0.0`` is ``True``; it is the same
    #: lesson ``exchange.auction.routes.RosterEntry`` records on ``list_price``. Zero or less
    #: is not a window either, so the floor is strict.
    bid_timeout_seconds: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)


class ConfirmResponse(BaseModel):
    auction_id: str
    intent_id: str
    created_at: str


@router.post("/clarify", response_model=ClarifyResponse)
async def clarify_route(body: ClarifyBody) -> ClarifyResponse:
    """Ask at most three questions and hand back the structured intent (R1).

    Creates nothing. There is no auction client in this function's scope.
    """
    _refuse_oversized_dialogue(body.turns)
    try:
        outcome = clarify(body.turns, buyer_llm())
    except IntentError as exc:
        # EmptyDialogue included: a dialogue with nothing in it is an unprocessable body,
        # not a server fault.
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    return ClarifyResponse(
        questions=list(outcome.questions),
        intent=outcome.intent.to_dict(),
        unresolved=list(outcome.unresolved),
        confirmed=outcome.confirmed,
    )


def _bind_the_deployment(request: Request) -> None:
    """Run the composition root once for this app, before the auction client is read.

    ``apps/buyer/svc/src/main.py`` is orchestrator-frozen (B6(iii)), so a deployment cannot
    be composed inside ``create_app`` — which is exactly why nothing composed it, and why a
    deployed buyer service answered 503 to every confirmed intent. This is the start-up hook,
    taken at the top of the request instead, and it binds nothing that is already bound, so a
    test or a deployment that sets ``app.state.auction_client`` itself still wins.

    A malformed deployment document is a **503**, not a 500 and not a silent fail-closed
    answer: a buyer service told to read a deployment it cannot read is misconfigured, and
    that is a different thing from a buyer service nobody has configured.
    """
    try:
        ensure_configured(request.app)
    except DeploymentConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.post("/confirm", response_model=ConfirmResponse, status_code=status.HTTP_201_CREATED)
async def confirm_route(body: ConfirmBody, request: Request) -> ConfirmResponse:
    """Create the one auction a confirmed intent is entitled to (R1)."""
    # FIRST, and before the deployment document is even read: a body this service will not
    # keep should cost it as little as possible, and every line below this one is work an
    # anonymous caller would otherwise have chosen for it (T-368).
    _refuse_oversized_confirmation(body)
    # Before the client is read. R1 is untouched by this: binding a client is not creating an
    # auction — `HttpExchangeClient` opens no socket until something calls it — and
    # `confirm()`'s FIRST statement is still `confirmed is not True`.
    _bind_the_deployment(request)
    client = getattr(request.app.state, AUCTION_CLIENT_ATTR, None)
    try:
        created = confirm(
            body.intent,
            client,
            confirmed=body.confirmed,
            profile=body.profile,
            roster=body.roster,
            bid_timeout_seconds=body.bid_timeout_seconds,
        )
    except ConfirmationWithheld as exc:
        # 409, not 400: the request is well-formed and the buyer simply has not said yes.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntentAlreadyConfirmed as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UnstructuredIntent as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    except IntentTooLarge as exc:
        # 413, not 422: the body is well-formed and simply bigger than this service stores
        # for one shopping need. Raised before the ledger claim, so nothing was kept.
        raise HTTPException(status_code=_HTTP_413, detail=str(exc)) from exc
    except AuctionClientUnusable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except ExchangeCallFailed as exc:
        # 502: the buyer's request was fine and the upstream's answer was not — the same
        # split T-072's accept route already makes for `NoPermalinkReturned`. Before a real
        # outbound client existed this could not happen from a deployment, only from a test
        # double; with one wired, an exchange that is down or answers 422 would otherwise
        # escape as a **500**, which blames this service for the exchange's state.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except IntentError as exc:
        # LAST, and fail-CLOSED. `InvalidConstraint` and `InvalidPreference` are refusals
        # about the caller's own body that this ladder never named, so they escaped as an
        # unauthenticated **500**: measured on this branch, `{"op": "lt"}` and
        # `{"weight": null}` each answered HTTP 500. A refusal this package can express is a
        # 4xx by definition — every one of them says the request was unprocessable — and
        # naming the base class means a refusal added later cannot reopen that hole.
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    return ConfirmResponse(
        auction_id=created.auction_id,
        intent_id=created.intent_id,
        created_at=created.created_at,
    )


# This module is not imported by the package `__init__` (it would drag FastAPI into every
# consumer of `clarify`), so it binds its own alternate spelling here. See `_spellings.py`
# for what goes wrong without it.
bind_spellings(sys.modules[__name__])
