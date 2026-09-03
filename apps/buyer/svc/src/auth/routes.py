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

import logging
import os
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from ..profile import IdentityLeak
from ..vault import PostgresPseudonymStore, PseudonymVault
from .magic_link import MagicLinkAuth, MagicLinkError, MagicLinkThrottled
from .sessions import SessionError

_log = logging.getLogger(__name__)

__all__ = [
    "VAULT_DSN_ENV",
    "WORKER_COUNT_ENVS",
    "ProcessLocalStateUnsafe",
    "auth_service",
    "build_auth_service",
    "get_auth_service",
    "router",
    "set_auth_service",
]

router = APIRouter(prefix="/buyer", tags=["buyer-auth"])

#: DSN for the one role D5 lets near ``vault.*``. Documented in ``.env.example``; when it is
#: set the login service keeps its email↔pseudonym history in ``vault.pseudonym_history``
#: rather than in this process's memory.
VAULT_DSN_ENV = "PROXYSHOP_PG_DSN_VAULT"

#: The variables a process manager sets when it will fork more than one worker. Standard
#: names, not invented ones: uvicorn and gunicorn both read ``WEB_CONCURRENCY``.
WORKER_COUNT_ENVS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS")

_service: MagicLinkAuth | None = None


class ProcessLocalStateUnsafe(RuntimeError):
    """The deployment asks for more workers than this service's state model can survive."""


def _configured_worker_count() -> tuple[str, int] | None:
    """The first worker-count variable that is set and asks for more than one worker."""
    for name in WORKER_COUNT_ENVS:
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            count = int(raw.strip())
        except ValueError:
            continue  # not a worker count; a process manager would ignore it too
        if count > 1:
            return name, count
    return None


def _vault_from_env() -> PseudonymVault | None:
    """The durable vault, when a ``buyer_vault`` DSN is configured. Otherwise ``None``.

    The connection is opened in autocommit mode on purpose: :class:`PostgresPseudonymStore`
    reads on every ``issue``, and a long-lived service that left each read sitting in an
    open transaction would pin a snapshot and block DDL for as long as it ran (CF-4, which
    this lane's fixtures already had to design around).
    """
    dsn = os.environ.get(VAULT_DSN_ENV)
    if not dsn:
        return None
    import psycopg

    return PseudonymVault(PostgresPseudonymStore(psycopg.connect(dsn, autocommit=True)))


def build_auth_service() -> MagicLinkAuth:
    """Construct the login service this process will serve from.

    Two things are decided here, and before this existed neither was decided anywhere: the
    service was a bare ``MagicLinkAuth()`` and :class:`PostgresPseudonymStore` had no
    caller outside its own tests.

    * **The vault.** With :data:`VAULT_DSN_ENV` set the email↔pseudonym history lives in
      ``vault.pseudonym_history`` and survives a restart, which is what makes R5's "a
      retired pseudonym is never handed out again" a property of the *system* rather than
      of one process's lifetime — an in-memory vault forgets every pseudonym it ever issued
      on restart, leaving the freshness check nothing to check against. Without the DSN the
      in-memory default is kept, so a database-less dev boot is unchanged.

    Raises:
        ProcessLocalStateUnsafe: a process manager is configured to fork more than one
            worker. Sessions and unredeemed magic links are held in this process's memory
            and there is no shared store for either yet, so a second worker cannot redeem a
            link the first one issued nor recognise a session it minted: behind a load
            balancer roughly half of logins fail, with a ``401`` indistinguishable from a
            genuinely bad link. Refusing the configuration is louder and more honest than
            serving it at a coin-flip success rate.
    """
    configured = _configured_worker_count()
    if configured is not None:
        name, count = configured
        raise ProcessLocalStateUnsafe(
            f"{name}={count} asks for {count} workers, but buyer sessions and unredeemed "
            f"magic links live in one process's memory: a link issued by one worker cannot "
            f"be redeemed by another and a session minted by one is unknown to the rest. "
            f"Run a single worker, or give this service a shared session store first."
        )
    vault = _vault_from_env()
    return MagicLinkAuth() if vault is None else MagicLinkAuth(vault=vault)


def auth_service() -> MagicLinkAuth:
    """The process-wide login service, built on first use.

    Kept behind a function rather than a module constant so that importing this module has
    no side effects, and so a deployment can swap the delivery transport and the stores with
    :func:`set_auth_service` before the first request.
    """
    global _service
    if _service is None:
        _service = build_auth_service()
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
    try:
        issued = service.request_login(str(body.email))
    except MagicLinkThrottled as exc:
        # The route takes no credential, so the size of the pending-link table is chosen by
        # whoever can reach it. At the ceiling the service sheds new requests; it never drops
        # a link somebody is already holding, which would hand an unauthenticated caller a
        # way to cancel a chosen buyer's login.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login links are pending; please try again shortly",
        ) from exc
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
    except IdentityLeak as exc:
        # The one exception raised *because* buyer identity escaped must not be the thing
        # that carries it out of the process. Unhandled, FastAPI renders it as a 500 whose
        # traceback holds the message; so it is caught here, answered with a body that names
        # nothing, and logged as the account keys involved and never their values (T-133).
        # `from None` is deliberate: chaining would put the original message back into the
        # traceback this exists to keep clean.
        _log.error(
            "R5: refused to serve a buyer profile that failed the identity backstop; "
            "account key(s): %s",
            ", ".join(exc.account_keys) or "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="this profile could not be built safely",
        ) from None
    dumped = profile.model_dump()
    return ProfileView(pseudonym=dumped["pseudonym"], buckets=dumped["buckets"])


@router.delete(
    "/auth/session",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out. The pseudonym stays retired and is never reissued",
)
def close_session(service: ServiceDep, x_buyer_session: SessionHeader = None) -> None:
    service.logout(_require_session_header(x_buyer_session))
