"""Buyer sessions — the pseudonym-side half of the boundary (T-070, SPEC R5).

A :class:`Session` is what the rest of the buyer service, and every service downstream of
it, is allowed to hold. It carries the pseudonym and never the email: the email exists on
one side of :mod:`buyer_svc.vault` and the pseudonym on the other, and the only thing that
can cross is :meth:`buyer_svc.vault.PseudonymVault.resolve`.

The constructor is where that is enforced rather than described. :meth:`SessionStore.open`
refuses a subject that is not a vault-issued pseudonym, so the shortest wrong
implementation — ``sessions.open(email)`` — fails at the boundary instead of quietly
threading an address through every downstream payload.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..vault import PSEUDONYM_PREFIX

__all__ = [
    "DEFAULT_SESSION_TTL",
    "InMemorySessionStore",
    "NotAPseudonym",
    "Session",
    "SessionError",
    "SessionExpired",
    "SessionStore",
    "UnknownSession",
]

#: How long a buyer session lives before a fresh magic link is needed. A session ends the
#: pseudonym's life too, so this is also the maximum lifetime of any one pseudonym.
DEFAULT_SESSION_TTL = timedelta(hours=12)

_SESSION_ID_BYTES = 16


class SessionError(Exception):
    """Base class for every refusal this module makes."""


class UnknownSession(SessionError):
    """No such session id. Never distinguishable from an expired one to a caller."""


class SessionExpired(SessionError):
    """The session existed and has run out."""


class NotAPseudonym(SessionError, ValueError):
    """A session was opened for something that is not a vault-issued pseudonym.

    This is the R5 boundary refusing to be crossed. The realistic mistake it catches is
    ``sessions.open(email)`` — which would produce a perfectly working session whose
    "pseudonym" is the buyer's address, and would put that address into every bid request
    the buyer's intent ever fans out.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Session:
    """One signed-in buyer, known only by the pseudonym this session was issued.

    There is deliberately no ``email`` field, and no account reference of any kind. A
    consumer that needs the human behind a session has to go through the vault, as the only
    role that can is ``buyer_vault``.
    """

    session_id: str
    pseudonym: str
    issued_at: datetime
    expires_at: datetime

    def expired(self, now: datetime | None = None) -> bool:
        """Has this session run out?"""
        return (now if now is not None else _utcnow()) >= self.expires_at


class SessionStore:
    """The seam a persistent session store would implement. See :class:`InMemorySessionStore`."""

    def open(self, pseudonym: str, *, ttl: timedelta | None = None) -> Session:
        raise NotImplementedError

    def get(self, session_id: str) -> Session:
        raise NotImplementedError

    def close(self, session_id: str) -> None:
        raise NotImplementedError


class InMemorySessionStore(SessionStore):
    """Process-local sessions. The default, and what the unit tests drive."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_SESSION_TTL,
    ) -> None:
        self._clock = clock if clock is not None else _utcnow
        self._ttl = ttl
        self._sessions: dict[str, Session] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def __iter__(self) -> Iterator[Session]:
        return iter(list(self._sessions.values()))

    def open(self, pseudonym: str, *, ttl: timedelta | None = None) -> Session:
        """Start a session for a vault-issued ``pseudonym``.

        Raises:
            NotAPseudonym: ``pseudonym`` did not come from the vault. Everything the vault
                issues carries :data:`~buyer_svc.vault.PSEUDONYM_PREFIX`; an email address,
                an account id or a bare name does not, and none of them may become the
                subject of a session.
        """
        if not isinstance(pseudonym, str) or not pseudonym.startswith(PSEUDONYM_PREFIX):
            raise NotAPseudonym(
                f"R5: a session subject must be a vault-issued pseudonym "
                f"(prefix {PSEUDONYM_PREFIX!r}), got {pseudonym!r}"
            )
        issued_at = self._clock()
        session = Session(
            session_id=secrets.token_urlsafe(_SESSION_ID_BYTES),
            pseudonym=pseudonym,
            issued_at=issued_at,
            expires_at=issued_at + (ttl if ttl is not None else self._ttl),
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        """The live session, or a refusal.

        Raises:
            UnknownSession: no such id.
            SessionExpired: the session has run out. It is dropped on the way out, so a
                second lookup reports it as unknown.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise UnknownSession("no such buyer session")
        if session.expired(self._clock()):
            del self._sessions[session_id]
            raise SessionExpired("this buyer session has expired; request a new magic link")
        return session

    def close(self, session_id: str) -> None:
        """End a session. Idempotent — signing out twice is not an error."""
        self._sessions.pop(session_id, None)
