"""``buyer_svc.auth`` — magic-link login and pseudonymous sessions (T-070, SPEC R5).

Public surface::

    from apps.buyer.svc.src.auth import MagicLinkAuth

    auth = MagicLinkAuth(deliver=send_email)
    auth.request_login("dana.reyes@example.com")   # token goes to the mailbox, not here
    session = auth.redeem(token_from_the_link)     # session + a BRAND-NEW pseudonym
    session.pseudonym                              # the only handle anything downstream gets

``routes`` is deliberately not imported here. :func:`buyer_svc.main.create_app` imports it by
path when it mounts the app, and keeping it out of this ``__init__`` means importing the auth
domain does not drag FastAPI in behind it.

Imports are relative on purpose; see :mod:`buyer_svc.vault` for why (this tree is reachable
as both ``buyer_svc.auth`` and ``apps.buyer.svc.src.auth``).
"""

from __future__ import annotations

from .magic_link import (
    DEFAULT_LINK_TTL,
    DEFAULT_MAX_PENDING,
    AccountDirectory,
    InMemoryAccountDirectory,
    LinkIssued,
    MagicLinkAlreadyUsed,
    MagicLinkAuth,
    MagicLinkError,
    MagicLinkExpired,
    MagicLinkThrottled,
    MagicLinkUnknown,
    token_fingerprint,
)
from .sessions import (
    DEFAULT_SESSION_TTL,
    InMemorySessionStore,
    NotAPseudonym,
    PseudonymRegistry,
    Session,
    SessionError,
    SessionExpired,
    SessionsExhausted,
    SessionStore,
    UnissuedPseudonym,
    UnknownSession,
)

__all__ = [
    "DEFAULT_LINK_TTL",
    "DEFAULT_MAX_PENDING",
    "DEFAULT_SESSION_TTL",
    "AccountDirectory",
    "InMemoryAccountDirectory",
    "InMemorySessionStore",
    "LinkIssued",
    "MagicLinkAlreadyUsed",
    "MagicLinkAuth",
    "MagicLinkError",
    "MagicLinkExpired",
    "MagicLinkThrottled",
    "MagicLinkUnknown",
    "NotAPseudonym",
    "PseudonymRegistry",
    "Session",
    "SessionError",
    "SessionExpired",
    "SessionStore",
    "SessionsExhausted",
    "UnissuedPseudonym",
    "UnknownSession",
    "token_fingerprint",
]
