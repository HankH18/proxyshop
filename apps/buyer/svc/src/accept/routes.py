"""HTTP surface for the shortlist and the checkout handoff (T-072).

Discovered and mounted by the frozen :func:`buyer_svc.main.create_app`.

Two routes, and the split between them is R2 on one side and R3 on the other::

    POST /buyer/shortlist/render   {"shortlist": {...}}          -> 200 labelled slots
    POST /buyer/shortlist/accept   {"slot": {...}}               -> 200 {permalink_url}

``/render`` has no exchange client in scope. Not "does not call one" — the handler cannot
reach one: :func:`buyer_svc.accept.render_shortlist` takes no client, so looking at a
shortlist cannot become accepting one no matter what a future edit does to this file.

``/accept`` never returns a redirect and never sets ``Location``. It returns the exchange's
permalink as data and the client navigates. That is deliberate: an API that 302s is an API
whose *callers* cannot see where they are being sent before they go, and R3's whole subject
is who decides where the buyer goes. The client can log it, show the host, and refuse.

Wiring
------
The exchange client is read from ``app.state.exchange_client`` and is **not** constructed
here, for the same reason T-071's ``/buyer/intent/confirm`` does not construct its auction
client: a buyer service that mints its own exchange client cannot be pointed at a stub, and
a deployment that forgot to wire one should hear about it as a 503 rather than discover it
when the first buyer accepts.

That attribute was set by nothing in this repository when this file was written, and it is
now set by :mod:`buyer_svc.composition` from the deployment document in ``BUYER_DEPLOYMENT``
/ ``BUYER_DEPLOYMENT_JSON``, through the request-time hook :func:`_bind_the_deployment`
below — the same object that ``/buyer/intent/confirm`` opens the auction with, because the
exchange that ran the auction is the only one that can accept a bid in it. A service with no
document configured still binds nothing and answers exactly the 503 it answered before.
``/render`` does **not** take the hook: R2's property is that this handler cannot reach an
exchange client, and a composition hook in it would be an exchange client in its scope.
"""

from __future__ import annotations

import sys
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, StrictBool

from ..composition import DeploymentConfigurationError, ExchangeCallFailed, ensure_configured
from ._spellings import bind_spellings
from .errors import (
    AcceptError,
    AcceptRefusedByExchange,
    ExchangeClientUnusable,
    MissingAuctionReference,
    NoPermalinkReturned,
    OfferAlreadyAccepted,
    UnsafePermalink,
    UnusableSlot,
)
from .handoff import accept
from .labels import render_shortlist

#: Starlette 0.5x renamed ``HTTP_422_UNPROCESSABLE_ENTITY`` to ``..._CONTENT`` and emits a
#: DeprecationWarning on the old name. Resolved once, here, so this module names neither
#: spelling twice and works on both.
_HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

__all__ = [
    "EXCHANGE_CLIENT_ATTR",
    "AcceptBody",
    "AcceptResponse",
    "RenderBody",
    "RenderResponse",
    "RenderedCommitment",
    "RenderedSlot",
    "router",
]

router = APIRouter(prefix="/buyer/shortlist", tags=["buyer-shortlist"])

#: Where the exchange client lives on the app. Set it in your composition root.
EXCHANGE_CLIENT_ATTR = "exchange_client"


class RenderBody(BaseModel):
    """A shortlist as the exchange sent it."""

    shortlist: dict[str, Any]
    #: ``StrictBool``, not ``bool``, and this is not a style preference. Pydantic's default
    #: (lax) mode coerces the JSON strings ``"yes"``, ``"true"``, ``"on"`` and ``"1"`` into
    #: ``True`` — measured on this tree at 2.13, where it turned a body carrying no boolean
    #: at all into an HTTP 201 and a live auction on T-071's confirm route. ``StrictBool``
    #: admits exactly ``true`` and ``false`` and answers 422 to everything else. It is used
    #: here even though the field is harmless, because "which booleans are strict" is not a
    #: judgement a reader of a route module should have to make field by field.
    derive_missing_labels: StrictBool = True


class RenderedCommitment(BaseModel):
    """One promise a store made, and the buyer-facing label for that promise alone."""

    key: str
    value: Any = None
    unit: str | None = None
    label: str


class RenderedSlot(BaseModel):
    """One slot, labelled for display.

    **Every field the screen needs must be declared here.** ``BaseModel``'s ``extra`` defaults
    to ``ignore``, so ``RenderedSlot(**slot.to_dict())`` silently drops a key this class does
    not name — which is how ``product``, ``price`` and ``commitments`` could have been added
    to :class:`~buyer_svc.accept.labels.LabelledSlot`, dumped by its ``to_dict``, and still
    never reached a buyer. No exception, no 500, just a field missing from the body.

    The three are ``None`` when the exchange sent nothing readable, and ``None`` is a
    different answer from ``0`` or ``[]`` in every one of them. See
    :mod:`buyer_svc.accept.labels`.
    """

    slot: str
    bid_ref: str
    auction_id: str
    fit_score: float
    provenance_labels: list[str]
    labels_source: str
    trust_summary: dict[str, Any] = Field(default_factory=dict)
    store_domain: str = ""
    #: R2's PRODUCT: ``{"product_ref": ..., "variant_ref": ... | null}``, or ``null``.
    product: dict[str, Any] | None = None
    #: R2's PRICE: both prices, the currency, the stated discount and the expiry, or ``null``.
    price: dict[str, Any] | None = None
    #: R2's COMMITMENTS. ``null`` means the exchange sent none — never an empty list, which
    #: would read to a shopper as a store that promised nothing.
    commitments: list[RenderedCommitment] | None = None


class RenderResponse(BaseModel):
    slots: list[RenderedSlot]


class AcceptBody(BaseModel):
    """The shortlist slot the buyer chose."""

    slot: dict[str, Any]
    #: The store domain the buyer was shown, if the caller knows it. ``None`` means "use the
    #: slot's ``store_domain`` if it has one". It is never taken from the slot's
    #: ``checkout_url``: R3 makes that value authority for nothing, and a spoofed slot would
    #: otherwise certify its own spoofed permalink.
    expected_domain: str | None = None


class AcceptResponse(BaseModel):
    """Where the buyer goes next — the exchange's permalink, unmodified."""

    permalink_url: str
    auction_id: str
    bid_ref: str
    slot: str
    accepted_at: str


@router.post("/render", response_model=RenderResponse)
async def render_route(body: RenderBody) -> RenderResponse:
    """Label a shortlist for display (R2). Creates nothing, accepts nothing."""
    slots = render_shortlist(body.shortlist, derive=body.derive_missing_labels)
    return RenderResponse(slots=[RenderedSlot(**slot.to_dict()) for slot in slots])


def _bind_the_deployment(request: Request) -> None:
    """Run the composition root once for this app, before the exchange client is read.

    ``apps/buyer/svc/src/main.py`` is orchestrator-frozen (B6(iii)), so a deployment cannot be
    composed inside ``create_app``. This is the start-up hook, taken at the top of the request
    instead; it binds nothing that is already bound, so a test or a deployment that sets
    ``app.state.exchange_client`` itself still wins. A malformed deployment document is a
    **503** naming the problem, never a 500.
    """
    try:
        ensure_configured(request.app)
    except DeploymentConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.post("/accept", response_model=AcceptResponse)
async def accept_route(body: AcceptBody, request: Request) -> AcceptResponse:
    """Accept one slot and hand back the exchange's checkout permalink (R3)."""
    _bind_the_deployment(request)
    client = getattr(request.app.state, EXCHANGE_CLIENT_ATTR, None)
    try:
        accepted = accept(body.slot, client, expected_domain=body.expected_domain)
    except (UnusableSlot, MissingAuctionReference) as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    except OfferAlreadyAccepted as exc:
        # 409 with the permalink already issued in the body: the buyer's checkout exists and
        # sending them an error with no way back to it would be the unhelpful half of right.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "permalink_url": exc.permalink_url},
        ) from exc
    except AcceptRefusedByExchange as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "denial_reason": exc.denial_reason},
        ) from exc
    except ExchangeClientUnusable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except ExchangeCallFailed as exc:
        # 502, for the reason the next clause gives: the buyer's request was fine and the
        # upstream's answer was not. A 404 from the exchange (the auction expired) reaches
        # here rather than being re-dressed as a decision about this buyer's offer — the
        # exchange's own 409, which IS such a decision, is returned by the client as a parsed
        # body and becomes `AcceptRefusedByExchange` above.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except (NoPermalinkReturned, UnsafePermalink) as exc:
        # 502: the buyer's request was fine and the upstream's answer was not. A 500 here
        # would blame this service for a permalink the exchange chose.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except AcceptError as exc:  # pragma: no cover - a refusal added later, not yet mapped
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return AcceptResponse(
        permalink_url=accepted.permalink_url,
        auction_id=accepted.auction_id,
        bid_ref=accepted.bid_ref,
        slot=accepted.slot,
        accepted_at=accepted.accepted_at,
    )


# This module is not imported by the package `__init__` (it would drag FastAPI into every
# consumer of `provenance_label`), so it binds its own alternate spelling here. See
# `_spellings.py` for what goes wrong without it.
bind_spellings(sys.modules[__name__])
