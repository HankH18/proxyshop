"""HTTP surface for R14's post-purchase feedback (T-073).

Discovered and mounted by the frozen :func:`buyer_svc.main.create_app`.

Two routes, and the split between them is "may I ask?" on one side and "this is the answer" on
the other::

    POST /buyer/feedback/prompt   {"order": {...}}                 -> 200 {offered, prompt|null}
    POST /buyer/feedback          {"order": {...}, "response": {}} -> 201 {event_id, ...}

``/prompt`` answers **200 with ``offered: false``** for an order the network did not route,
rather than 404. "Is there a feedback prompt for this order?" is a question with two correct
answers, and an order list rendering fifty rows should not have to treat half of them as errors.
The ``reason`` field carries the refusal in words a screen can show, so the buyer is told *why*
there is nothing to fill in — which for an un-routed order is the honest and slightly
reassuring answer that we did not send them there.

``/prompt`` cannot write. It reaches no ledger sink at all: :func:`buyer_svc.feedback.
feedback_prompt` takes none, so asking whether a prompt exists cannot become recording feedback
no matter what a future edit does to this file.

Three things about the boundary
-------------------------------
**``routed`` is a ``StrictBool``.** Not a style preference. Pydantic's default (lax) mode
coerces the JSON strings ``"yes"``, ``"true"``, ``"on"`` and ``"1"`` into ``True`` — measured on
this tree at 2.13, where it turned a body carrying no boolean at all into an HTTP 201 and a live
auction on T-071's confirm route. Here the same coercion would offer a feedback prompt for an
order the network never routed, which is the one thing R14 forbids, so the order body is a typed
model rather than an open ``dict``. **All three** routing spellings the library reads
(``routed``, ``network_routed``, ``routed_by_network``) are declared as ``StrictBool``: the body
allows extras, so declaring only one of them left the other two arriving untyped and being read
leniently — measured, ``{"network_routed": "yes"}`` was a 200 with a prompt while
``{"routed": "yes"}`` was a 422.

**The response body is a closed model with no free-text field.** FastAPI serializes through
``response_model``, so even a handler that returned the whole order record by mistake is filtered
down to the declared fields. That is the same reason T-070's auth routes declare theirs
explicitly: the R5 boundary is enforced by the wire contract as well as by the code behind it.

**Ownership is checked only when the session can be resolved.** ``X-Buyer-Session`` is optional
here, exactly as it is on T-072's accept routes, because nothing in this repository logs a buyer
in before they reach a shortlist. When it IS supplied it is resolved to a pseudonym and passed
to :func:`~buyer_svc.feedback.submission.submit_feedback`, which refuses feedback about an order
routed for somebody else. The gap — that an unauthenticated caller who can name an order ref is
not stopped here — is real, is the reason ``FeedbackNotYours`` exists, and is reported in this
ticket's NEEDS rather than papered over with a check that cannot run.

Wiring
------
The ledger sink is read from ``app.state.ledger_sink`` and is **not** constructed here, for the
same reason T-072's ``/buyer/shortlist/accept`` does not construct its exchange client: a buyer
service that mints its own ledger client cannot be pointed at a stub, and a deployment that
forgot to wire one should hear about it as a 503 rather than discover it when the first buyer
answers a prompt.

It IS constructed by a composition root now. ``buyer_svc.composition.ensure_ledger_sink`` is the
request-time start-up hook this route takes before reading the attribute — the same hook, in the
same place and for the same B6(iii) reason, that ``/buyer/shortlist/accept`` takes for its
exchange client — and it binds the trust service's published ``POST /events`` by default while
never replacing anything already on ``app.state``. Until it existed the sentence that used to
end this paragraph was true: *nothing in this repository sets that attribute*, so this route
answered **503 in every deployment**, and the one channel where a human grades a pitch was dead
on arrival.

Retrying an uncertain write
---------------------------
The 503 for an uncertain write has always said "retry with this ``event_id``", and until the
composition root landed there was no way to do it: ``SubmitBody`` carried no such field, so an
outage left the order permanently claimed in this process and every later submission was a 409.
The body now takes an optional ``event_id``, and it is honoured **only** when it equals the id
this process already holds for that order and that id has not landed — so a re-attempt writes
the same row (which a ledger keyed on ``event_id`` collapses to one) while a caller cannot
choose the id of a row on an append-only ledger. See :func:`_re_attempt_id`.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..composition import ensure_ledger_sink
from ._spellings import bind_spellings
from .errors import (
    ContradictoryFeedback,
    FeedbackAlreadySubmitted,
    FeedbackError,
    FeedbackNotYours,
    LedgerSinkUnusable,
    LedgerWriteUncertain,
    MalformedFeedbackEvent,
    MissingFeedbackChoice,
    OrderNotRouted,
    UnknownFeedbackChoice,
    UnknownFeedbackQuestion,
    UnusableOrder,
)
from .prompt import feedback_prompt
from .routing import routing
from .submission import event_view, submit_feedback, submitted

_log = logging.getLogger(__name__)

#: Starlette 0.5x renamed ``HTTP_422_UNPROCESSABLE_ENTITY`` to ``..._CONTENT`` and emits a
#: DeprecationWarning on the old name. Resolved once, here, so this module names neither
#: spelling twice and works on both.
_HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

__all__ = [
    "LEDGER_SINK_ATTR",
    "MAX_REFERENCE",
    "MAX_STATUS",
    "REFERENCE_PATTERN",
    "FeedbackEventView",
    "OrderBody",
    "PromptBody",
    "PromptResponse",
    "PromptView",
    "SubmitBody",
    "router",
]

router = APIRouter(prefix="/buyer/feedback", tags=["buyer-feedback"])

#: Where the ledger sink lives on the app. Set it in your composition root.
LEDGER_SINK_ATTR = "ledger_sink"

#: Longest identifier this route will copy onto a ledger event. See :class:`OrderBody`.
MAX_REFERENCE = 200

#: Longest order status. A lifecycle word, not a sentence.
MAX_STATUS = 64

#: The shape an identifier this route copies onto a ledger event may take.
#:
#: Length alone was not enough, and this is the finding that says so: `order_ref` is written
#: verbatim onto an append-only `LedgerEvent` and into this service's logs, so
#: `{"order_ref": "Dana Reyes dana.reyes@example.com 555-0134 the seller lied to me"}` was a
#: free-text field with a buyer's name and email in it, reaching the ledger under a kind whose
#: whole point is that it carries no prose. Nothing downstream can take it back out again.
#:
#: The class is what a real reference is made of: a Shopify order name (`#1001`), a GID
#: (`gid://shopify/Order/4435291300000`), a UUID, one of this repo's `ord-e7-101` refs. No
#: spaces, no `@`, so neither a sentence nor an email address can be one.
REFERENCE_PATTERN = rf"^[A-Za-z0-9_.:#/-]{{1,{MAX_REFERENCE}}}$"


class OrderBody(BaseModel):
    """One order, as much of it as this service needs to answer R14's question.

    ``extra="allow"`` because a real order record carries plenty more and truncating it here
    would make the route's answer differ from the library's for the same order. Only the fields
    below are ever read.
    """

    model_config = ConfigDict(extra="allow")

    #: Bounded AND shaped, and both are about the LEDGER rather than about memory. `order_ref` is
    #: copied verbatim onto an append-only `LedgerEvent` and into this service's log lines, and
    #: nothing downstream can take it back out — measured, a 100 000-character `order_ref` and an
    #: `order_ref` reading "Dana Reyes dana.reyes@example.com 555-0134 the seller lied to me"
    #: were both accepted, echoed by `/prompt`, and written to the ledger by `/feedback`. See
    #: :data:`REFERENCE_PATTERN`.
    order_ref: str = Field(min_length=1, max_length=MAX_REFERENCE, pattern=REFERENCE_PATTERN)
    store_id: str = Field(default="", max_length=MAX_REFERENCE, pattern=rf"{REFERENCE_PATTERN}|^$")
    auction_id: str = Field(
        default="", max_length=MAX_REFERENCE, pattern=rf"{REFERENCE_PATTERN}|^$"
    )
    #: ``StrictBool``, never ``bool`` — see this module's docstring. ``None`` means "the record
    #: does not say", which R14's gate refuses rather than resolves.
    routed: StrictBool | None = None
    #: The other two spellings :data:`buyer_svc.feedback.ROUTED_FIELDS` reads. They are DECLARED
    #: here, and that is the point rather than completeness: `model_config` allows extras, so an
    #: undeclared `network_routed` used to arrive untyped and be read by the library's lax
    #: `flag()` — measured, `{"network_routed": "yes"}` was HTTP 200 `offered: true` while
    #: `{"routed": "yes"}` was a 422. A strict boolean on one spelling of a field is not a strict
    #: boundary; it is a strict boundary with a door beside it.
    network_routed: StrictBool | None = None
    routed_by_network: StrictBool | None = None
    status: str = Field(default="", max_length=MAX_STATUS)
    buyer_pseudonym: str = Field(
        default="", max_length=MAX_REFERENCE, pattern=rf"{REFERENCE_PATTERN}|^$"
    )


class PromptBody(BaseModel):
    """The order a prompt is being asked about."""

    order: OrderBody


class SubmitBody(BaseModel):
    """The order, and the answered prompt."""

    order: OrderBody
    #: The answer. An open mapping because the choice vocabulary is published by the library
    #: (``buyer_svc.feedback.CHOICE_IDS``) and re-stating it as an enum here would be a second
    #: copy of the option table — the drift D30 exists to prevent.
    response: dict[str, Any]
    #: The id of a submission this client is RE-ATTEMPTING, from a previous 503's
    #: ``{"retry_with_event_id": true, "event_id": ...}``. Ignored unless it is exactly the id
    #: this process already holds, unlanded, for this order — see :func:`_re_attempt_id`. It is
    #: bounded and shaped like every other identifier on this route because it is compared to a
    #: value that reaches an append-only ledger.
    event_id: str = Field(default="", max_length=MAX_REFERENCE, pattern=rf"{REFERENCE_PATTERN}|^$")


class OptionView(BaseModel):
    """One option on the prompt. Three fields, none of which admits typing."""

    id: str
    label: str
    matched_pitch: bool


class PromptView(BaseModel):
    """The one prompt. A single object, never a list — R14 offers one question, not a survey."""

    order_ref: str
    store_id: str
    auction_id: str
    question_id: str
    question: str
    input_type: str
    options: list[OptionView]


class PromptResponse(BaseModel):
    """Whether there is a prompt for this order, and if not, why not."""

    offered: bool
    reason: str = ""
    prompt: PromptView | None = None


class FeedbackEventView(BaseModel):
    """The ledger event the submission became, as the client is allowed to see it."""

    event_id: str
    kind: str
    ts: str
    order_ref: str
    store_id: str
    auction_id: str
    matched_pitch: bool
    reason: str


def _pseudonym_for(session_id: str | None) -> str:
    """The pseudonym behind an ``X-Buyer-Session`` header, or ``""`` when there is no header.

    A header that is present and not a live session is a 401: a caller that sent a credential
    is telling us who they are, and quietly ignoring a bad one would mean the ownership check is
    skipped in exactly the case an attacker controls.
    """
    if not session_id:
        return ""
    try:
        from ..auth.routes import get_auth_service
        from ..auth.sessions import SessionError
    except Exception as exc:  # pragma: no cover - the login package is part of this service
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="buyer sessions are unavailable, so this session could not be checked",
        ) from exc
    try:
        return str(get_auth_service().session(session_id).pseudonym)
    except SessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="no live buyer session"
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # A login service that cannot be built (T-070 refuses a multi-worker deployment) is a
        # 503 rather than a 500: the request was fine and this deployment is not.
        _log.warning("could not resolve a buyer session for a feedback request: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="buyer sessions are unavailable, so this session could not be checked",
        ) from exc


def _re_attempt_id(order: Any, offered: str) -> str | None:
    """The event id to re-attempt this order's submission under, or ``None`` for a fresh one.

    The whole check is "does the id the client offered equal the one THIS PROCESS already holds,
    unlanded, for this order?" — which is a lookup in ``FeedbackLedger``, not a decision about a
    caller-supplied value. So a client can re-attempt exactly the submission a previous 503 told
    it about, and cannot do anything else: an id for an order with no claim, an id that does not
    match the claim, and an id for a submission already known to have landed are all ignored,
    and the request proceeds as a fresh submission (which is then refused with the 409 it
    deserves, carrying the id that already exists).

    That distinction matters more than it looks. ``event_id`` becomes the row's identity on an
    append-only, hash-chained ledger with no delete; a route that let a caller name it would let
    anyone squat on an id, or write a second event under an id trust has already sealed.
    """
    wanted = offered.strip()
    if not wanted:
        return None
    try:
        order_ref = routing(order).order_ref
    except FeedbackError:
        # An unusable order is the submit path's refusal to make, with its own message.
        return None
    if not order_ref:
        return None
    book = submitted()
    if book.landed(order_ref):
        return None
    held = book.event_for(order_ref)
    return held if held is not None and held == wanted else None


@router.post("/prompt", response_model=PromptResponse)
async def prompt_route(body: PromptBody) -> PromptResponse:
    """The one structured prompt for an order, or a reason there is none (R14).

    Writes nothing and can reach no ledger sink.
    """
    order = body.order.model_dump()
    try:
        prompt = feedback_prompt(order)
    except UnusableOrder as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    if prompt is None:
        return PromptResponse(offered=False, reason=routing(order).reason, prompt=None)
    return PromptResponse(offered=True, reason="", prompt=PromptView(**prompt.to_dict()))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=FeedbackEventView)
async def submit_route(
    body: SubmitBody,
    request: Request,
    x_buyer_session: str | None = Header(default=None, alias="X-Buyer-Session"),
) -> FeedbackEventView:
    """Record one answered prompt as a ``feedback`` ledger event (R14)."""
    # The composition root, taken here for the reason `/buyer/shortlist/accept` takes its own:
    # `main.py` is orchestrator-frozen, so there is no start-up hook to bind a seam in. It never
    # replaces a sink somebody else wired, so a test's double and a deployment's own client both
    # still win.
    ensure_ledger_sink(request.app)
    sink = getattr(request.app.state, LEDGER_SINK_ATTR, None)
    pseudonym = _pseudonym_for(x_buyer_session)
    order = body.order.model_dump()
    try:
        event = submit_feedback(
            order,
            body.response,
            sink,
            buyer_pseudonym=pseudonym,
            event_id=_re_attempt_id(order, body.event_id),
        )
    except UnusableOrder as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    except OrderNotRouted as exc:
        # 403, not 404 and not 422. The order exists and the request is well formed; what is
        # refused is the buyer's standing to leave feedback about it, which is R14's gate.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": str(exc), "reason": exc.reason},
        ) from exc
    except FeedbackNotYours as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except FeedbackAlreadySubmitted as exc:
        # 409 with the event id already recorded: the buyer's feedback exists, and an error with
        # no reference to it would be the unhelpful half of right.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "event_id": exc.event_id},
        ) from exc
    except UnknownFeedbackChoice as exc:
        raise HTTPException(
            status_code=_HTTP_422,
            detail={"message": str(exc), "options": list(exc.options)},
        ) from exc
    except (
        MissingFeedbackChoice,
        UnknownFeedbackQuestion,
        ContradictoryFeedback,
    ) as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    except LedgerSinkUnusable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except LedgerWriteUncertain as exc:
        # 503 and NOT 500, and it carries the event id. The sink raised, so this service is the
        # one that is unavailable — but the write may have landed, and a client that retries
        # blindly would ask for a second trust observation. The id is what makes the retry safe;
        # the sink's own message (which routinely names a host or a DSN) is logged, not sent.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": str(exc), "event_id": exc.event_id, "retry_with_event_id": True},
        ) from exc
    except MalformedFeedbackEvent as exc:
        # 500 and not 422: the body this service built is wrong, which is nobody's fault but
        # ours, and blaming the caller for it would send them away to fix a request that is fine.
        _log.error("refused to emit a malformed feedback event: %s", "; ".join(exc.problems))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="this feedback could not be recorded in the published shape",
        ) from exc
    except FeedbackError as exc:  # pragma: no cover - a refusal added later, not yet mapped
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    return FeedbackEventView(**event_view(event))


# This module is not imported by the package `__init__` (it would drag FastAPI into every
# consumer of `feedback_prompt`), so it binds its own alternate spelling here. See
# `_spellings.py` for what goes wrong without it.
bind_spellings(sys.modules[__name__])
