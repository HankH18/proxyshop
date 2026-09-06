"""Buyer sessions — the pseudonym-side half of the boundary (T-070, SPEC R5).

A :class:`Session` is what the rest of the buyer service, and every service downstream of
it, is allowed to hold. It carries the pseudonym and never the email: the email exists on
one side of :mod:`buyer_svc.vault` and the pseudonym on the other, and the only thing that
can cross is :meth:`buyer_svc.vault.PseudonymVault.resolve`.

The constructor is where that is enforced rather than described. :meth:`SessionStore.open`
refuses a subject that is not a vault-issued pseudonym, so the shortest wrong
implementation — ``sessions.open(email)`` — fails at the boundary instead of quietly
threading an address through every downstream payload.

Format is not membership (T-163)
--------------------------------
The prefix check alone made the pseudonym a bearer credential whose *shape* was its only
proof: ``psn-`` glued onto the buyer's own email opened a session that authenticated
``GET /buyer/profile``. A store may therefore be given the vault that issued the pseudonyms
it will be shown — see :meth:`SessionStore.bind_vault` — after which a subject the vault has
no record of is refused with :class:`UnissuedPseudonym`.

:class:`~buyer_svc.auth.magic_link.MagicLinkAuth` binds its own vault into whatever store it
was handed, so the production path built by
:func:`~buyer_svc.auth.routes.build_auth_service` always checks membership. A store
constructed on its own has no vault to ask and keeps the older format-only guard: that is
the documented meaning of an unbound store, not a fallback a caller can reach by accident.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from ..vault import PSEUDONYM_PREFIX

__all__ = [
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_SESSION_TTL",
    "InMemorySessionStore",
    "NotAPseudonym",
    "PseudonymRegistry",
    "Session",
    "SessionError",
    "SessionExpired",
    "SessionStore",
    "SessionsExhausted",
    "UnissuedPseudonym",
    "UnknownSession",
]

#: How long a buyer session lives before a fresh magic link is needed. A session ends the
#: pseudonym's life too, so this is also the maximum lifetime of any one pseudonym.
DEFAULT_SESSION_TTL = timedelta(hours=12)

#: Ceiling on the number of sessions one process holds. Redemption is reachable by anyone who
#: can read a mailbox, so the size of this table is chosen by callers, not by the service —
#: and nothing ever removed an entry except a lookup that happened to find it expired, so a
#: session opened and never revisited lived for the life of the process. Generous enough that
#: no real deployment meets it before it meets its own memory, small enough to be a bound.
DEFAULT_MAX_SESSIONS = 50_000

_SESSION_ID_BYTES = 16


class SessionError(Exception):
    """Base class for every refusal this module makes."""


class UnknownSession(SessionError):
    """No such session id. Never distinguishable from an expired one to a caller."""


class SessionExpired(SessionError):
    """The session existed and has run out."""


class SessionsExhausted(SessionError):
    """The store is holding as many sessions as it may. See :data:`DEFAULT_MAX_SESSIONS`.

    Refusing rather than evicting is deliberate and mirrors
    :meth:`~buyer_svc.auth.magic_link.MagicLinkAuth.request_login`: evicting the oldest live
    session would let anyone able to open sessions end a chosen victim's on demand, which
    trades a memory bound for an authenticated-user denial of service.
    """


class NotAPseudonym(SessionError, ValueError):
    """A session was opened for something that is not a vault-issued pseudonym.

    This is the R5 boundary refusing to be crossed. The realistic mistake it catches is
    ``sessions.open(email)`` — which would produce a perfectly working session whose
    "pseudonym" is the buyer's address, and would put that address into every bid request
    the buyer's intent ever fans out.

    Which is exactly why the message never echoes the subject it refused. The value this
    exception exists to catch *is* the buyer's email address; printing it into a log line or
    a traceback would make the R5 guard the shortest path to an R5 disclosure (T-133).
    """


class UnissuedPseudonym(NotAPseudonym):
    """The subject looks like a pseudonym and the vault has never issued it (T-163).

    A subclass of :class:`NotAPseudonym` on purpose: "carries the prefix" was never the
    property this boundary meant to assert, so a caller that already refuses a bad subject
    keeps refusing this one, and one that wants the finer distinction can ask for it.

    Like its base it never echoes the subject. The value that reaches here is, in the case
    worth catching, ``psn-`` concatenated with the buyer's own email address.
    """


@runtime_checkable
class PseudonymRegistry(Protocol):
    """The one question a session store asks the vault: *did you issue this?*

    Deliberately the narrowest seam that answers it. Both
    :class:`~buyer_svc.vault.PseudonymVault` and
    :class:`~buyer_svc.vault.store.PseudonymStore` already satisfy it, so binding one costs
    this module no import of the vault package beyond the prefix it already takes, and hands
    the session store no way to *use* the email it gets back.
    """

    def resolve(self, pseudonym: str) -> str | None:
        """The buyer behind ``pseudonym``, or ``None`` when it was never issued."""


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
    """The seam a persistent session store would implement. See :class:`InMemorySessionStore`.

    The subject guard lives here rather than in one implementation so that every store —
    the in-memory one, a Redis one, anything a later ticket adds — refuses the same subjects
    for the same reasons, and so binding a vault does not have to be re-implemented per
    backend. Subclasses call :meth:`admit` at the top of their own :meth:`open`.
    """

    #: The vault this store checks membership against, or ``None`` for a store that has not
    #: been given one. A class attribute so every subclass has it without a constructor.
    _vault: PseudonymRegistry | None = None

    def bind_vault(self, vault: PseudonymRegistry | None) -> None:
        """Attach the vault whose issuance decides which subjects may open a session.

        Idempotent and one-way in practice: :class:`~buyer_svc.auth.magic_link.MagicLinkAuth`
        binds its own vault into whatever store it was handed, and a store that was already
        given one keeps it, so passing ``sessions=`` and ``vault=`` that disagree does not
        silently retarget the check.
        """
        if vault is None or self._vault is not None:
            return
        self._vault = vault

    @property
    def vault(self) -> PseudonymRegistry | None:
        """The bound vault, if any. Read-only; use :meth:`bind_vault` to set it."""
        return self._vault

    def admit(self, pseudonym: str) -> None:
        """Refuse ``pseudonym`` unless it is a subject a session may be about.

        Two refusals, and the second one is T-163:

        * it does not carry :data:`~buyer_svc.vault.PSEUDONYM_PREFIX`, so it is not even
          shaped like something the vault hands out — the ``sessions.open(email)`` mistake;
        * it is shaped right and the bound vault has no record of ever issuing it, so it is
          a forgery. Before this check the format *was* the proof, and
          ``"psn-" + buyer_email`` authenticated ``GET /buyer/profile``.

        Raises:
            NotAPseudonym: the prefix is missing.
            UnissuedPseudonym: no vault issued it. Only reachable on a bound store.
        """
        if not isinstance(pseudonym, str) or not pseudonym.startswith(PSEUDONYM_PREFIX):
            raise NotAPseudonym(
                f"R5: a session subject must be a vault-issued pseudonym "
                f"(prefix {PSEUDONYM_PREFIX!r}); the value offered is withheld from this "
                f"message because the mistake this catches is passing the buyer's email"
            )
        vault = self._vault
        if vault is not None and vault.resolve(pseudonym) is None:
            raise UnissuedPseudonym(
                "R5: this session subject carries the pseudonym prefix but no vault ever "
                "issued it, so it is a forgery rather than a credential; the value offered "
                "is withheld from this message because a forged subject is built out of the "
                "buyer's own email"
            )

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
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        vault: PseudonymRegistry | None = None,
    ) -> None:
        self._clock = clock if clock is not None else _utcnow
        self._ttl = ttl
        self._max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}
        self._vault: PseudonymRegistry | None = vault

    def __len__(self) -> int:
        return len(self._sessions)

    def __iter__(self) -> Iterator[Session]:
        return iter(list(self._sessions.values()))

    def _forget_expired(self, now: datetime) -> None:
        """Drop every session that has run out.

        Expiry used to be noticed only by :meth:`get`, so a session nobody ever came back to
        was never dropped at all — the table only grew. Sweeping on :meth:`open` keeps the
        cost proportional to the work being asked for rather than needing a timer.
        """
        dead = [
            session_id for session_id, session in self._sessions.items() if session.expired(now)
        ]
        for session_id in dead:
            del self._sessions[session_id]

    def open(self, pseudonym: str, *, ttl: timedelta | None = None) -> Session:
        """Start a session for a vault-issued ``pseudonym``.

        Every expired session is dropped first, so the table is swept by the work rather than
        by a timer, and the ceiling below is measured against sessions that are actually live.

        Raises:
            NotAPseudonym: ``pseudonym`` did not come from the vault. Everything the vault
                issues carries :data:`~buyer_svc.vault.PSEUDONYM_PREFIX`; an email address,
                an account id or a bare name does not, and none of them may become the
                subject of a session.
            UnissuedPseudonym: the prefix is there and this store's bound vault never issued
                the value (T-163). Only reachable on a store that was given a vault.
            SessionsExhausted: the store already holds ``max_sessions`` live sessions. New
                sessions are shed rather than live ones evicted; see the class.
        """
        self.admit(pseudonym)
        issued_at = self._clock()
        self._forget_expired(issued_at)
        if len(self._sessions) >= self._max_sessions:
            raise SessionsExhausted(
                f"{len(self._sessions)} sessions are already open and the ceiling is "
                f"{self._max_sessions}; refusing to open another rather than ending one "
                f"somebody is using"
            )
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
