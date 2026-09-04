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
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, StrictBool

from ._spellings import bind_spellings
from .clarifier import clarify
from .confirmation import confirm
from .errors import (
    AuctionClientUnusable,
    ConfirmationWithheld,
    IntentAlreadyConfirmed,
    IntentError,
    UnstructuredIntent,
)

_log = logging.getLogger(__name__)

#: Starlette 0.5x renamed ``HTTP_422_UNPROCESSABLE_ENTITY`` to ``..._CONTENT`` and emits a
#: DeprecationWarning on the old name. Resolved once, here, so this module names neither
#: spelling twice and works on both.
_HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

__all__ = [
    "AUCTION_CLIENT_ATTR",
    "LLM_ROLE",
    "ClarifyBody",
    "ClarifyResponse",
    "ConfirmBody",
    "ConfirmResponse",
    "buyer_llm",
    "router",
    "set_buyer_llm",
]

router = APIRouter(prefix="/buyer/intent", tags=["buyer-intent"])

#: ``llm.config.ROLE_BUYER``. Spelled rather than imported so this module stays importable
#: with only FastAPI available; :func:`buyer_llm` validates it against the real roster the
#: moment it resolves a client, so a typo here surfaces as a logged failure and a
#: model-free loop, never as a silent wrong-model call.
LLM_ROLE = "buyer"

#: Where the exchange client lives on the app. Set it in your composition root.
AUCTION_CLIENT_ATTR = "auction_client"

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
    bid_timeout_seconds: float | None = None


class ConfirmResponse(BaseModel):
    auction_id: str
    intent_id: str
    created_at: str


@router.post("/clarify", response_model=ClarifyResponse)
async def clarify_route(body: ClarifyBody) -> ClarifyResponse:
    """Ask at most three questions and hand back the structured intent (R1).

    Creates nothing. There is no auction client in this function's scope.
    """
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


@router.post("/confirm", response_model=ConfirmResponse, status_code=status.HTTP_201_CREATED)
async def confirm_route(body: ConfirmBody, request: Request) -> ConfirmResponse:
    """Create the one auction a confirmed intent is entitled to (R1)."""
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
    except AuctionClientUnusable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return ConfirmResponse(
        auction_id=created.auction_id,
        intent_id=created.intent_id,
        created_at=created.created_at,
    )


# This module is not imported by the package `__init__` (it would drag FastAPI into every
# consumer of `clarify`), so it binds its own alternate spelling here. See `_spellings.py`
# for what goes wrong without it.
bind_spellings(sys.modules[__name__])
