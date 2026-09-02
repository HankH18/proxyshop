"""HTTP surface for buyer login (T-070, SPEC R5).

Discovered and mounted by the frozen :func:`buyer_svc.main.create_app`.

Four routes, and the shape of their responses is the ticket::

    POST /buyer/auth/magic-link   {"email": ...}   -> 202 {"expires_at": ...}
    POST /buyer/auth/session      {"token": ...}   -> 201 {"session_id", "pseudonym", ...}
    GET  /buyer/auth/session      X-Buyer-Session  -> 200 {"session_id", "pseudonym", ...}
    GET  /buyer/profile           X-Buyer-Session  -> 200 {"pseudonym", "buckets"}

Every response model below is explicit and none of them has a field that could hold an
email, a name or an address. That is deliberate: FastAPI serializes through
``response_model``, so even a handler that returned the whole account record by mistake
would be filtered down to the declared fields. The pseudonym boundary is therefore enforced
by the wire contract as well as by the code behind it.

The token is never in a response. ``POST /buyer/auth/magic-link`` answers ``202 Accepted``
with an expiry; the link itself goes to the service's ``deliver`` callable (an email
sender in production). A caller that could read the token out of the response would not
need the mailbox, and the whole scheme would be an open door.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from .magic_link import MagicLinkAuth, MagicLinkError
from .sessions import SessionError

__all__ = ["auth_service", "get_auth_service", "router", "set_auth_service"]

router = APIRouter(prefix="/buyer", tags=["buyer-auth"])

_service: MagicLinkAuth | None = None


def auth_service() -> MagicLinkAuth:
    """The process-wide login service, built on first use.

    Kept behind a function rather than a module constant so that importing this module has
    no side effects, and so a deployment can swap the delivery transport and the stores with
    :func:`set_auth_service` before the first request.
    """
    global _service
    if _service is None:
        _service = MagicLinkAuth()
    return _service


def set_auth_service(service: MagicLinkAuth | None) -> None:
    """Install (or, with ``None``, forget) the process-wide login service."""
    global _service
    _service = service


def get_auth_service() -> MagicLinkAuth:
    """FastAPI dependency. Override this in tests, not the module global."""
    return auth_service()


ServiceDep = Annotated[MagicLinkAuth, Depends(get_auth_service)]
SessionHeader = Annotated[str | None, Header(alias="X-Buyer-Session")]


class MagicLinkRequest(BaseModel):
    """What a buyer sends to start a login."""

    email: EmailStr


class MagicLinkAccepted(BaseModel):
    """The answer. No token, by construction."""

    expires_at: datetime


class RedeemRequest(BaseModel):
    """The token out of the emailed link."""

    token: str = Field(min_length=1)


class SessionView(BaseModel):
    """A session as the client is allowed to see it: a pseudonym and two timestamps."""

    session_id: str
    pseudonym: str
    issued_at: datetime
    expires_at: datetime


class ProfileView(BaseModel):
    """The store-facing profile. Exactly the two keys DESIGN pins on ``BuyerProfile``."""

    pseudonym: str
    buckets: dict[str, Any]


def _require_session_header(session_id: str | None) -> str:
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="an X-Buyer-Session header is required",
        )
    return session_id


@router.post(
    "/auth/magic-link",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MagicLinkAccepted,
    summary="Send a single-use login link to a buyer's mailbox",
)
def request_magic_link(body: MagicLinkRequest, service: ServiceDep) -> MagicLinkAccepted:
    issued = service.request_login(str(body.email))
    return MagicLinkAccepted(expires_at=issued.expires_at)


@router.post(
    "/auth/session",
    status_code=status.HTTP_201_CREATED,
    response_model=SessionView,
    summary="Redeem a login link; starts a session under a brand-new pseudonym",
)
def redeem_magic_link(body: RedeemRequest, service: ServiceDep) -> SessionView:
    try:
        session = service.redeem(body.token)
    except MagicLinkError as exc:
        # Unknown / expired / already used are one answer on the wire. Telling a caller
        # which of the three it was turns the endpoint into an oracle over other people's
        # links; the service's own logs keep the distinction.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="this login link is not valid"
        ) from exc
    return SessionView(
        session_id=session.session_id,
        pseudonym=session.pseudonym,
        issued_at=session.issued_at,
        expires_at=session.expires_at,
    )


@router.get(
    "/auth/session",
    response_model=SessionView,
    summary="The live session behind an X-Buyer-Session header",
)
def read_session(service: ServiceDep, x_buyer_session: SessionHeader = None) -> SessionView:
    try:
        session = service.session(_require_session_header(x_buyer_session))
    except SessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="no live buyer session"
        ) from exc
    return SessionView(
        session_id=session.session_id,
        pseudonym=session.pseudonym,
        issued_at=session.issued_at,
        expires_at=session.expires_at,
    )


@router.get(
    "/profile",
    response_model=ProfileView,
    summary="The coarsened, identity-free profile a store may be shown",
)
def read_profile(service: ServiceDep, x_buyer_session: SessionHeader = None) -> ProfileView:
    try:
        profile = service.profile_for(_require_session_header(x_buyer_session))
    except SessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="no live buyer session"
        ) from exc
    dumped = profile.model_dump()
    return ProfileView(pseudonym=dumped["pseudonym"], buckets=dumped["buckets"])


@router.delete(
    "/auth/session",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out. The pseudonym stays retired and is never reissued",
)
def close_session(service: ServiceDep, x_buyer_session: SessionHeader = None) -> None:
    service.logout(_require_session_header(x_buyer_session))
