"""Magic-link login for buyers (T-070, SPEC R5).

R5's first clause is "buyers authenticate with email magic link". There is no password, so
the whole security of the scheme is in the token, and this module keeps three properties
that the obvious dict-of-tokens implementation does not:

* **Only a hash is stored.** ``_pending`` is keyed by ``sha256(token)``. A dump of the
  service's state — a log line, a heap snapshot, a leaked backup — yields no usable link.
* **A link is single use and time boxed.** Redeeming marks the record consumed rather than
  deleting it, so a replay is refused as *already used* rather than as *unknown*, and the
  two are distinguishable in the service's own logs while the HTTP layer collapses them.
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
    "AccountDirectory",
    "InMemoryAccountDirectory",
    "LinkIssued",
    "MagicLinkAlreadyUsed",
    "MagicLinkAuth",
    "MagicLinkError",
    "MagicLinkExpired",
    "MagicLinkUnknown",
    "token_fingerprint",
]

#: How long a magic link stays redeemable. Short: the link is a bearer credential sitting
#: in a mailbox.
DEFAULT_LINK_TTL = timedelta(minutes=15)

_TOKEN_BYTES = 32


class MagicLinkError(Exception):
    """Base class for every reason a link is not honoured."""


class MagicLinkUnknown(MagicLinkError):
    """No pending link matches this token."""


class MagicLinkExpired(MagicLinkError):
    """The link existed and has passed its expiry."""


class MagicLinkAlreadyUsed(MagicLinkError):
    """The link was redeemed once already. Links are single use."""


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
    """

    vault: PseudonymVault = field(default_factory=PseudonymVault)
    sessions: SessionStore = field(default_factory=InMemorySessionStore)
    accounts: AccountDirectory = field(default_factory=InMemoryAccountDirectory)
    deliver: Callable[[str, str, datetime], None] = _drop
    clock: Callable[[], datetime] = _utcnow
    link_ttl: timedelta = DEFAULT_LINK_TTL
    session_ttl: timedelta = DEFAULT_SESSION_TTL
    _pending: dict[str, _PendingLink] = field(default_factory=dict, repr=False)

    # -- issuing --------------------------------------------------------------------

    def request_login(self, email: str) -> LinkIssued:
        """Mint a single-use magic link for ``email`` and hand it to ``deliver``.

        Returns:
            :class:`LinkIssued` — the expiry, and nothing else. The token is not returned.

        Raises:
            ValueError: ``email`` is not usable as a vault key.
        """
        key = normalise_buyer_key(email)
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        expires_at = self.clock() + self.link_ttl
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
        pending = self._pending.get(token_fingerprint(token))
        if pending is None:
            raise MagicLinkUnknown("no such magic link")
        now = self.clock()
        if pending.used_at is not None:
            raise MagicLinkAlreadyUsed("this magic link has already been used")
        if now >= pending.expires_at:
            raise MagicLinkExpired("this magic link has expired; request another")

        pending.used_at = now
        if self.accounts.get(pending.email) is None:
            # First link redeemed for this address: magic-link signup and login are one
            # gesture, so the account is created here rather than refused.
            self.accounts.upsert(pending.email, {"email": pending.email})
        pseudonym = self.vault.issue(pending.email)
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
