"""HTTP surface for the shopper's follow-up questions (D55, R2).

Discovered and mounted by the frozen :func:`buyer_svc.main.create_app`, which globs
``<feature>/routes.py``::

    POST /buyer/chat/ask   {"auction_id": "...", "question": "..."}   -> 200 an answer

The prefix is free: ``intent/routes.py`` owns ``/buyer/intent``, ``accept/routes.py`` owns
``/buyer/shortlist``, ``auctions/routes.py`` owns ``/buyer/auctions``, ``feedback/routes.py``
owns ``/buyer/feedback`` and ``auth/routes.py`` owns ``/buyer`` with ``/auth/...`` and
``/profile``. Nothing answered under ``/buyer/chat`` before this file.

Why the body carries no shortlist, and this is the whole point
=============================================================
``POST /buyer/shortlist/render`` takes a shortlist off the wire, deliberately: a render is a
*display* of what the caller already holds, and a caller that renders its own document has
misled nobody but itself. This route is different in kind. An answer is the PLATFORM
asserting something, in its own voice, to a shopper who is trusting it — so a page that could
post its own slots could make the platform assert anything about a shop that does not exist.

So the material is fetched HERE, from the exchange, by auction id:
:meth:`~buyer_svc.composition.HttpExchangeClient.shortlist_for` for the live slots and
:meth:`~buyer_svc.composition.HttpExchangeClient.outcome_for` for the recorded ranking rows
the exchange publishes exactly once. The shopper's browser supplies an auction id and a
question, and there is no parameter through which anything it types could become a fact.

The two doors this route does NOT have
======================================
It cannot open an auction — there is no ``create_auction`` call in this module and no path
that reaches one — and it cannot accept an offer. Asking a question about a shortlist may not
become a purchase, and that is structural here rather than promised: the only client methods
named in this file are the two READS.

Wiring
------
The exchange client is read from ``app.state.exchange_client`` and is not constructed here,
for the same reason ``accept/routes.py`` does not construct one. It is bound by
:mod:`buyer_svc.composition` from ``BUYER_DEPLOYMENT`` / ``BUYER_DEPLOYMENT_JSON`` /
``EXCHANGE_URL`` through the request-time hook :func:`_bind_the_deployment`, because
``main.py`` is orchestrator-frozen (B6(iii)). A service with no document configured binds
nothing and answers **503**; a malformed document is a 503 naming the problem, never a 500.

The writer is :func:`buyer_svc.pitch.writer.pitch_writer` — the seam the shortlist's own
platform case already uses, reused rather than duplicated so a deployment has ONE model knob
for the buyer-side agent's voice rather than two that can disagree. With D20's default
``double`` provider it returns a client whose reply fails the answer screen on its first
rule, and the shopper is served the deterministic assembled answer.
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
from pydantic import BaseModel, Field

from ..composition import (
    EXCHANGE_CLIENT_ATTR,
    DeploymentConfigurationError,
    ExchangeCallFailed,
    ensure_configured,
)
from ..intent._spellings import bind_spellings
from ..pitch.writer import pitch_writer
from .answering import MAX_QUESTION_CHARS, answer_about
from .evidence import corpus_for

#: Starlette 0.5x renamed ``HTTP_422_UNPROCESSABLE_ENTITY`` to ``..._CONTENT`` and emits a
#: DeprecationWarning on the old name. Resolved once, here, so this module names neither
#: spelling twice and works on both.
_HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)
#: Starlette renamed ``HTTP_413_REQUEST_ENTITY_TOO_LARGE`` to ``HTTP_413_CONTENT_TOO_LARGE``
#: in the same sweep. Resolved once, here, for the same reason.
_HTTP_413 = getattr(status, "HTTP_413_CONTENT_TOO_LARGE", 413)

__all__ = [
    "EXCHANGE_CLIENT_ATTR",
    "MAX_AUCTION_ID_CHARS",
    "AskBody",
    "AskResponse",
    "router",
]

_log = logging.getLogger(__name__)

#: The most characters of an auction id this route will carry to the exchange.
#:
#: ``buyer_svc.intent.models.MAX_IDENTIFIER_LENGTH``'s 128, restated rather than imported for
#: the reason that module restates the exchange's: this door is a different door and a
#: deployment tightening one should not silently tighten the other. It matters here because
#: the id is percent-encoded into an outbound URL by
#: :meth:`~buyer_svc.composition.HttpExchangeClient.shortlist_for`, so an unbounded one is an
#: unbounded request this service makes on an anonymous caller's say-so.
MAX_AUCTION_ID_CHARS = 128


class _BoundedBodyRoute(APIRoute):
    """Answer a body this service cannot even PARSE as the caller's 4xx, never as a 500.

    Starlette decodes the JSON body **inside** the route handler, before any dependency and
    any validator, so nothing declared below can be reached in time to refuse it. Measured on
    ``buyer_svc.intent.routes``, whose copy of this class carries the numbers: a 4 KB body of
    2 000 nested arrays raised ``RecursionError`` out of ``await request.json()`` and answered
    HTTP 500 on an unauthenticated door. Depth is not size, so no byte ceiling closes it.

    Copied rather than imported: importing a leading-underscore class out of another feature's
    route module would make this door's behaviour depend on a private name that module is free
    to rename. The right home is a shared route base no feature ticket owns, and that move is
    reported rather than made here.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # The default 422 body echoes the offending value back under `input`, which
                # makes refusing cost this service what accepting would have. Only the
                # location and the message survive.
                return JSONResponse(
                    status_code=_HTTP_422,
                    content={
                        "detail": [
                            {
                                "loc": [str(part)[:64] for part in error.get("loc", ())],
                                "msg": str(error.get("msg", ""))[:200],
                                "type": str(error.get("type", ""))[:64],
                            }
                            for error in exc.errors()[:20]
                        ]
                    },
                )
            except RecursionError:
                # `from None`: the context is thousands of identical parser frames and is not
                # information about this request.
                raise HTTPException(
                    status_code=_HTTP_413,
                    detail=(
                        "this body is nested more deeply than this service will parse. "
                        "None of it is quoted back."
                    ),
                ) from None

        return bounded


router = APIRouter(prefix="/buyer/chat", tags=["buyer-chat"], route_class=_BoundedBodyRoute)


class AskBody(BaseModel):
    """One follow-up about one auction's shortlist.

    Two fields, and the absence of a third is the design. There is no ``shortlist``, no
    ``slots`` and no ``facts``: the material is fetched from the exchange by
    :func:`ask_route`, so nothing a page posts can become something the platform asserts.
    """

    #: Bounded at the field so an oversized id is a 422 from FastAPI rather than an
    #: unbounded outbound URL, and refused before any handler line runs.
    auction_id: str = Field(min_length=1, max_length=MAX_AUCTION_ID_CHARS)
    #: Bounded for the same reason and at the same layer. A follow-up is a sentence;
    #: :data:`buyer_svc.chat.answering.MAX_QUESTION_CHARS` is what the reader would have
    #: truncated to anyway, and refusing at the wire means the shopper is told rather than
    #: quietly answered about the first 400 characters of what they asked.
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


class AskResponse(BaseModel):
    """The answer, and everything it stands on.

    ``grounds`` is the receipt: one entry per evidence item the answer was built from or
    screened against, each naming its ``voice`` (``platform`` or ``shop_claim``) and where
    the platform got it. ``shop_messages`` is the OTHER voice — the shop's own prose, whole
    and unedited, carried separately so a page can print it under the shop's name and so
    that no field of this response mixes the two.
    """

    auction_id: str
    question: str
    answer: str
    #: ``assembled`` (built in code from the grounds) or ``written`` (a model's prose that
    #: survived the screen). On the wire so a reader can tell which voice they are reading
    #: without inferring it, exactly as the shortlist's ``platform_case_source`` does.
    answer_source: str
    grounds: list[dict[str, Any]]
    not_held: list[dict[str, Any]]
    shop_messages: list[dict[str, Any]]
    #: Whether this process still holds the exchange's recorded ranking rows for the auction.
    #: ``false`` means a question about *why* one option came first is answered with what is
    #: left rather than with a guess — a different answer from "the ranking was all zeroes".
    ranking_recorded: bool


def _bind_the_deployment(request: Request) -> None:
    """Run the composition root once for this app, before the exchange client is read.

    A malformed deployment document is a **503**, not a 500: a buyer service told to read a
    deployment it cannot read is misconfigured, and that is a different thing from a buyer
    service nobody has configured.
    """
    try:
        ensure_configured(request.app)
    except DeploymentConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.post("/ask", response_model=AskResponse)
async def ask_route(body: AskBody, request: Request) -> AskResponse:
    """Answer a follow-up about the shortlist this auction produced. Creates nothing."""
    _bind_the_deployment(request)
    client = getattr(request.app.state, EXCHANGE_CLIENT_ATTR, None)
    if client is None or not callable(getattr(client, "shortlist_for", None)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "this buyer service has no exchange wired, so it cannot read the shortlist "
                "this question is about. Configure BUYER_DEPLOYMENT / EXCHANGE_URL."
            ),
        )
    try:
        shortlist = client.shortlist_for(body.auction_id)
    except ExchangeCallFailed as exc:
        # 502: the shopper's question was fine and the upstream's answer was not. The same
        # split `POST /buyer/intent/confirm` makes for a failed `POST /auctions`.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if shortlist is None:
        # The exchange's own 404, carried through as one. It is a real and different answer
        # from a shortlist with no slots: `auctions/routes.py` documents the four causes the
        # exchange declines to choose between, and this route does not invent one either.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "the exchange holds no shortlist for this auction, so there is nothing here "
                "to answer about. It does not say why."
            ),
        )

    recorded: Any = None
    reader = getattr(client, "outcome_for", None)
    if callable(reader):
        try:
            recorded = reader(body.auction_id)
        except Exception:  # noqa: BLE001 - the ranking is a bonus; its absence is reported
            _log.warning("could not read the recorded auction outcome", exc_info=True)
            recorded = None

    answer = answer_about(
        body.question,
        corpus_for(shortlist, recorded=recorded),
        writer=pitch_writer(),
    )
    payload = answer.to_dict()
    # The corpus reads the auction id off the exchange's own body; a shortlist that named
    # none is still about the auction the shopper asked after, and answering with an empty
    # string there would make the response unjoinable to the page that asked.
    payload["auction_id"] = payload["auction_id"] or body.auction_id
    return AskResponse(**payload)


# This module is not imported by the package `__init__` (it would drag FastAPI into every
# consumer of `answer_about`), so it binds its own alternate spelling here.
bind_spellings(sys.modules[__name__])
