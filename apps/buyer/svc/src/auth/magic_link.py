"""Magic-link login for buyers (T-070, SPEC R5).

R5's first clause is "buyers authenticate with email magic link". There is no password, so
the whole security of the scheme is in the token, and this module keeps three properties
that the obvious dict-of-tokens implementation does not:

* **Only a hash is stored.** ``_pending`` is keyed by ``sha256(token)``. A dump of the
  service's state — a log line, a heap snapshot, a leaked backup — yields no usable link.
* **A link is single use and time boxed.** Redeeming marks the record consumed rather than
  deleting it, so a replay is refused as *already used* rather than as *unknown*, and the
  two are distinguishable in the service's own logs while the HTTP layer collapses them.
  Finding, checking and consuming happen under one lock: FastAPI runs a ``def`` endpoint in
  a threadpool, so two requests carrying the same token really do arrive at once, and a
  check-then-mark split across three statements is single use only when nobody races it.
* **The table of unredeemed links is bounded.** ``request_login`` takes no credential, so
  everything about that table's size is chosen by whoever can reach the route. Issuing a
  link drops the address's own outstanding unredeemed links and every link past its expiry,
  and refuses rather than evicting once :data:`DEFAULT_MAX_PENDING` live links are held.
* **The token never rides on the response.** :meth:`MagicLinkAuth.request_login` returns
  only when the link expires; the token itself goes to the injected ``deliver`` callable,
  which in production is an email sender. A route cannot accidentally echo it.

What login *is*, in this system
-------------------------------
Redeeming a link does two things at once, and the second is the point of the ticket: it
opens a session, and it asks :class:`~buyer_svc.vault.PseudonymVault` for a **new**
pseudonym. Sessions do not share pseudonyms and a retired pseudonym is never handed out
again (the vault enforces that, see :class:`~buyer_svc.vault.PseudonymExhausted`), so two
sessions of the same buyer are unlinkable to anybody who cannot read ``vault.*`` — which,
under T-011's grant model, is everybody except ``buyer_vault``.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..profile import BuyerProfile, build_profile
from ..vault import PseudonymVault, normalise_buyer_key
from .sessions import (
    DEFAULT_SESSION_TTL,
    InMemorySessionStore,
    Session,
    SessionStore,
)

__all__ = [
    "DEFAULT_LINK_TTL",
    "DEFAULT_MAX_PENDING",
    "AccountDirectory",
    "InMemoryAccountDirectory",
    "LinkIssued",
    "MagicLinkAlreadyUsed",
    "MagicLinkAuth",
    "MagicLinkError",
    "MagicLinkExpired",
    "MagicLinkThrottled",
    "MagicLinkUnknown",
    "token_fingerprint",
]

#: How long a magic link stays redeemable. Short: the link is a bearer credential sitting
#: in a mailbox.
DEFAULT_LINK_TTL = timedelta(minutes=15)

#: The most unredeemed links the service will hold at once. ``POST /buyer/auth/magic-link``
#: takes no credential, so without a ceiling an unauthenticated caller sizes this table.
#: With :data:`DEFAULT_LINK_TTL` at fifteen minutes and one live link per address, ten
#: thousand is a large real deployment and a few megabytes of memory.
DEFAULT_MAX_PENDING = 10_000

_TOKEN_BYTES = 32


class MagicLinkError(Exception):
    """Base class for every reason a link is not honoured."""


class MagicLinkUnknown(MagicLinkError):
    """No pending link matches this token."""


class MagicLinkExpired(MagicLinkError):
    """The link existed and has passed its expiry."""


class MagicLinkAlreadyUsed(MagicLinkError):
    """The link was redeemed once already. Links are single use."""


class MagicLinkThrottled(MagicLinkError):
    """The pending-link table is full, so no new link was issued.

    Raised by :meth:`MagicLinkAuth.request_login`, never by :meth:`MagicLinkAuth.redeem`: a
    link somebody is already holding is never dropped to make room for a new one. The HTTP
    layer answers ``429``.
    """


def token_fingerprint(token: str) -> str:
    """The stored form of a magic-link token: ``sha256`` hex, never the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class LinkIssued:
    """What :meth:`MagicLinkAuth.request_login` tells its caller.

    Carries no token and no email — everything on it is safe to put in an HTTP response.
    """

    expires_at: datetime


@dataclass
class _PendingLink:
    email: str
    expires_at: datetime
    used_at: datetime | None = None


class AccountDirectory:
    """The seam onto buyer account records. See :class:`InMemoryAccountDirectory`.

    An account record is the *unredacted* thing — email, name, address, order history. It
    lives behind this seam and behind the vault, and the only shape of it that ever leaves
    :class:`MagicLinkAuth` is a :func:`~buyer_svc.profile.build_profile` output.
    """

    def get(self, email: str) -> Mapping[str, Any] | None:
        raise NotImplementedError

    def upsert(self, email: str, account: Mapping[str, Any]) -> Mapping[str, Any]:
        raise NotImplementedError


class InMemoryAccountDirectory(AccountDirectory):
    """Process-local account records, keyed by normalised email."""

    def __init__(self, accounts: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self._accounts: dict[str, dict[str, Any]] = {}
        for email, account in (accounts or {}).items():
            self.upsert(email, account)

    def get(self, email: str) -> Mapping[str, Any] | None:
        return self._accounts.get(normalise_buyer_key(email))

    def upsert(self, email: str, account: Mapping[str, Any]) -> Mapping[str, Any]:
        key = normalise_buyer_key(email)
        record = dict(account)
        record.setdefault("email", key)
        record.setdefault("orders", [])
        self._accounts[key] = record
        return record


def _drop(email: str, token: str, expires_at: datetime) -> None:
    """The default delivery: send the link nowhere.

    Deliberately inert rather than "helpfully" printing the token. A service booted without
    a mail transport should fail to log buyers in, not publish bearer credentials to stdout.
    """


@dataclass
class MagicLinkAuth:
    """Issue and redeem magic links; a redemption is a session plus a fresh pseudonym.

    Args:
        vault: the identity vault. Defaults to an in-memory one.
        sessions: the session store. Defaults to an in-memory one.
        accounts: the account directory. Defaults to an empty in-memory one, in which case
            a first login creates a minimal record — magic-link signup and login are the
            same gesture.
        deliver: called as ``deliver(email, token, expires_at)``. The **only** place the
            token is ever handed out. Defaults to :func:`_drop`.
        clock: injectable ``now``.
        link_ttl / session_ttl: lifetimes.
        max_pending: ceiling on the unredeemed-link table. See :data:`DEFAULT_MAX_PENDING`.
    """

    vault: PseudonymVault = field(default_factory=PseudonymVault)
    sessions: SessionStore = field(default_factory=InMemorySessionStore)
    accounts: AccountDirectory = field(default_factory=InMemoryAccountDirectory)
    deliver: Callable[[str, str, datetime], None] = _drop
    clock: Callable[[], datetime] = _utcnow
    link_ttl: timedelta = DEFAULT_LINK_TTL
    session_ttl: timedelta = DEFAULT_SESSION_TTL
    max_pending: int = DEFAULT_MAX_PENDING
    _pending: dict[str, _PendingLink] = field(default_factory=dict, repr=False)
    #: Serialises every mutation of ``_pending``. FastAPI runs a ``def`` endpoint in a
    #: threadpool, so ``POST /buyer/auth/session`` is genuinely concurrent and an
    #: unsynchronised check-then-mark would let two racing requests both find a link unused.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Hand the session store the vault whose pseudonyms it will be shown (T-163).

        Without this the session boundary could only check a subject's *format*, so
        ``"psn-" + the buyer's own email`` opened a session that authenticated
        ``GET /buyer/profile``: the pseudonym was a bearer credential whose shape was its
        only proof. Binding happens here, at the one place that holds both halves, rather
        than in :class:`SessionStore`'s constructor — which would force every caller of every
        store implementation to thread a vault through, and would still leave this class free
        to pair a store with a vault that did not issue what the store admits.

        Deliberately tolerant of a store that is not one of this module's: a custom
        :class:`SessionStore` that never learned about ``bind_vault`` keeps working, exactly
        as it did before this existed. It does not get the membership check, and the two
        stores this package ships both do.
        """
        binder = getattr(self.sessions, "bind_vault", None)
        if callable(binder):
            binder(self.vault)

    # -- issuing --------------------------------------------------------------------

    @property
    def pending_links(self) -> int:
        """How many links are currently held, redeemed or not. Bounded; see below."""
        with self._lock:
            return len(self._pending)

    def _forget_stale(self, now: datetime, superseded: str | None = None) -> None:
        """Drop every link that can no longer be honoured. The caller holds ``_lock``.

        Two families go:

        * anything past its expiry — expiry used to be checked only on redemption, so a link
          nobody ever clicked was kept for the life of the process;
        * ``superseded``'s outstanding unredeemed links, when a new one is being minted for
          that address. A magic link is a bearer credential in a mailbox: the buyer who
          re-requests one *because* they think the first mail was intercepted must not be
          handing the interceptor a second working credential. Redeemed records are kept
          until they expire so a replay is still recognised as a replay.
        """
        dead = [
            fingerprint
            for fingerprint, link in self._pending.items()
            if now >= link.expires_at
            or (superseded is not None and link.email == superseded and link.used_at is None)
        ]
        for fingerprint in dead:
            del self._pending[fingerprint]

    def request_login(self, email: str) -> LinkIssued:
        """Mint a single-use magic link for ``email`` and hand it to ``deliver``.

        Any link previously issued to ``email`` and not yet redeemed stops working.

        Returns:
            :class:`LinkIssued` — the expiry, and nothing else. The token is not returned.

        Raises:
            ValueError: ``email`` is not usable as a vault key.
            MagicLinkThrottled: the pending-link table is full. New requests are shed rather
                than a live link evicted — evicting the oldest would let an unauthenticated
                caller delete a chosen victim's link on demand, which is the wrong direction
                to fail in. This bounds memory; it is not a rate limiter, and a deployment
                still wants one in front of the route.
        """
        key = normalise_buyer_key(email)
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = self.clock()
        expires_at = now + self.link_ttl
        with self._lock:
            self._forget_stale(now, superseded=key)
            if len(self._pending) >= self.max_pending:
                raise MagicLinkThrottled(
                    f"{len(self._pending)} magic links are already pending; refusing to issue "
                    f"another rather than evicting one somebody is holding"
                )
            self._pending[token_fingerprint(token)] = _PendingLink(email=key, expires_at=expires_at)
        self.deliver(key, token, expires_at)
        return LinkIssued(expires_at=expires_at)

    # -- redeeming ------------------------------------------------------------------

    def redeem(self, token: str) -> Session:
        """Consume ``token`` and start a session under a brand-new pseudonym.

        Returns:
            A :class:`~buyer_svc.auth.sessions.Session`. Its pseudonym has never been issued
            to anybody before, and the pseudonym the buyer's previous session used is now
            retired and will never be issued again.

        Raises:
            MagicLinkUnknown: no pending link matches.
            MagicLinkExpired: the link has passed its expiry.
            MagicLinkAlreadyUsed: the link was redeemed before. Links are single use.
        """
        if not isinstance(token, str) or not token:
            raise MagicLinkUnknown("no such magic link")
        fingerprint = token_fingerprint(token)
        # `clock` is read outside the lock: it is injectable, so calling it while holding the
        # lock would let a caller's clock decide how long every other request blocks.
        now = self.clock()
        with self._lock:
            # Find, check and consume in one critical section. Doing it in three separate
            # steps loses single use exactly when it matters: two requests carrying the same
            # token both see `used_at is None` and both get a session.
            pending = self._pending.get(fingerprint)
            if pending is None:
                raise MagicLinkUnknown("no such magic link")
            if pending.used_at is not None:
                raise MagicLinkAlreadyUsed("this magic link has already been used")
            if now >= pending.expires_at:
                raise MagicLinkExpired("this magic link has expired; request another")
            pending.used_at = now
            email = pending.email

        if self.accounts.get(email) is None:
            # First link redeemed for this address: magic-link signup and login are one
            # gesture, so the account is created here rather than refused.
            self.accounts.upsert(email, {"email": email})
        pseudonym = self.vault.issue(email)
        return self.sessions.open(pseudonym, ttl=self.session_ttl)

    # -- using a session ------------------------------------------------------------

    def session(self, session_id: str) -> Session:
        """The live session for ``session_id``; raises on unknown or expired."""
        return self.sessions.get(session_id)

    def logout(self, session_id: str) -> None:
        """End a session. The pseudonym stays retired; it is never reissued."""
        self.sessions.close(session_id)

    def profile_for(self, session_id: str) -> BuyerProfile:
        """The store-facing :class:`BuyerProfile` for a live session.

        This is the one method that crosses the boundary, and it crosses it *inwards*: it
        resolves the pseudonym back to an email inside the vault, reads the account, and
        returns a profile from which the email cannot be recovered.
        """
        session = self.sessions.get(session_id)
        email = self.vault.resolve(session.pseudonym)
        account: Mapping[str, Any] = {}
        if email is not None:
            account = self.accounts.get(email) or {}
        return build_profile(account, session.pseudonym)
