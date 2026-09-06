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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..profile import BuyerProfile, build_profile, k_anonymity_floor
from ..vault import PseudonymVault, normalise_buyer_key
from .sessions import (
    DEFAULT_SESSION_TTL,
    InMemorySessionStore,
    RetiredPseudonym,
    Session,
    SessionError,
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


def _issue_order(record: Any) -> int | None:
    """``record``'s place in this service's issue order, or ``None`` when it has none.

    ``None`` is not "oldest". A record whose issue order cannot be read is a record this
    service cannot compare, and the supersede guard treats that as a reason to REFUSE a
    re-arm rather than to allow one — see :meth:`MagicLinkAuth._superseded`. Reading it as
    zero, which a bare ``getattr(record, "sequence", 0)`` does, makes an unstamped record the
    oldest thing in the table and therefore incapable of blocking anything: a fail-open
    default on a guard whose whole job is to fail closed.
    """
    order = getattr(record, "sequence", None)
    return order if isinstance(order, int) and not isinstance(order, bool) else None


@dataclass
class _PendingLink:
    email: str
    expires_at: datetime
    used_at: datetime | None = None
    #: Issue order across this service, assigned under the lock. What makes "has this address
    #: been given a NEWER link?" answerable — which is the question the supersede rule really
    #: asks, and which neither ``used_at`` nor ``expires_at`` can answer: a newer link that has
    #: already been redeemed has ``used_at`` set, and a frozen or coarse clock gives two links
    #: the same ``expires_at``. Assigned after construction rather than passed, so a substitute
    #: ``_PendingLink`` (the concurrency test installs one) keeps working; readers use
    #: ``getattr`` with a default for the same reason.
    sequence: int = 0


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

    def cohort(self) -> Sequence[Mapping[str, Any]]:
        """Every account this directory would release together — the k-anonymity population.

        A floor is a property of a *release over a population*, so
        :func:`~buyer_svc.profile.build_profile` cannot enforce one until somebody tells it
        who else is being released. This is that seam (T-221, T-363), and it is read by
        :meth:`MagicLinkAuth.profile_for` only when a floor above 1 is actually configured.

        The default is **nothing**, and that is the safe direction rather than an omission. A
        directory that cannot enumerate itself hands the builder a release of one, which
        satisfies no floor above 1, and the documented answer to a floor that cannot be met is
        to withhold every facet — not to publish the fine-grained tuple. So a directory that
        forgets to override this loses utility and never privacy. One that can enumerate
        itself overrides it and gets real generalisation instead of suppression.
        """
        return ()


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

    def cohort(self) -> Sequence[Mapping[str, Any]]:
        """The live records, not copies — :meth:`get` already hands the same objects out.

        Identity matters here: ``build_profile`` drops the buyer's own record out of the
        cohort by ``is``, and it can only do that if the record it was handed and the one in
        this list are the same object.
        """
        return list(self._accounts.values())


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
        publish: called as ``publish(profile)`` with every profile
            :meth:`profile_for` builds, before it is returned. ``None`` — the default —
            publishes nowhere, which is what a database-less dev boot wants. The deployment
            passes :func:`~buyer_svc.auth.routes.build_profile_publisher`'s output, which
            upserts into ``app.buyer_accounts``. Note the *shape*: a callable taking only
            the profile, so this class never holds a connection and never learns which
            table, role or database the store-visible working set lives in.
        clock: injectable ``now``.
        link_ttl / session_ttl: lifetimes.
        max_pending: ceiling on the unredeemed-link table. See :data:`DEFAULT_MAX_PENDING`.
    """

    vault: PseudonymVault = field(default_factory=PseudonymVault)
    sessions: SessionStore = field(default_factory=InMemorySessionStore)
    accounts: AccountDirectory = field(default_factory=InMemoryAccountDirectory)
    deliver: Callable[[str, str, datetime], None] = _drop
    publish: Callable[[BuyerProfile], None] | None = None
    clock: Callable[[], datetime] = _utcnow
    link_ttl: timedelta = DEFAULT_LINK_TTL
    session_ttl: timedelta = DEFAULT_SESSION_TTL
    max_pending: int = DEFAULT_MAX_PENDING
    _pending: dict[str, _PendingLink] = field(default_factory=dict, repr=False)
    #: Monotonic issue counter, read and bumped only under ``_lock``. See ``_PendingLink``.
    _sequence: int = field(default=0, repr=False, compare=False)
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
            self._sequence += 1
            link = _PendingLink(email=key, expires_at=expires_at)
            link.sequence = self._sequence
            self._pending[token_fingerprint(token)] = link
        self.deliver(key, token, expires_at)
        return LinkIssued(expires_at=expires_at)

    def _superseded(self, fingerprint: str, spent: _PendingLink, email: str) -> bool:
        """Has ``email`` been given a link this service cannot prove is older? Holds ``_lock``.

        The question the supersede rule really asks, and the reason it is issue ORDER rather
        than used-ness: a newer link that has already been redeemed has ``used_at`` set, so
        "is another link unredeemed?" answers *no* exactly when the answer matters most.
        MEASURED with that guard: L1's redemption parked, the buyer re-requesting AND redeeming
        L2, then L1 failing, left ``_pending`` holding {L1: unused, L2: used}, and replaying L1
        opened a second session under an independent pseudonym over real HTTP while the
        buyer's own session kept working.

        Two deliberate conservatisms, both of which cost at most one burned link and buy a
        credential staying dead:

        * ``>=`` and not ``>``. Within one service the counter strictly increases under this
          lock, so a tie is unreachable here and the two operators are equivalent — proved by
          a differential fuzz over twenty thousand operations. They stop being equivalent the
          moment two services share one ``_pending`` table, which
          ``MagicLinkAuth(_pending=shared)`` and :func:`dataclasses.replace` both produce:
          independent counters, one table, and two links for one address both stamped 1.
          At a tie ``>`` re-arms the superseded link and ``>=`` refuses it.
        * a record whose order cannot be read BLOCKS. See :func:`_issue_order`.
        """
        mine = _issue_order(spent)
        if mine is None:
            return True
        return any(
            other.email == email and ((order := _issue_order(other)) is None or order >= mine)
            for other_fingerprint, other in self._pending.items()
            if other_fingerprint != fingerprint
        )

    # -- redeeming ------------------------------------------------------------------

    def redeem(self, token: str) -> Session:
        """Consume ``token`` and start a session under a brand-new pseudonym.

        Returns:
            A :class:`~buyer_svc.auth.sessions.Session`. Its pseudonym has never been issued
            to anybody before, and the pseudonym the buyer's previous session used is now
            retired and will never be issued again.

        A link is consumed for a session, and only for a session. Marking it used happens
        under the lock — it must, or two racing requests both find it unused — but the work it
        was consumed *for* happens after the lock is released, and that work can fail: the
        session store can be at its ceiling, the vault can be exhausted, and with
        ``PROXYSHOP_PG_DSN_VAULT`` set every vault call is a database round trip that a blip
        can break. MEASURED before this was guarded, with a session store momentarily full:
        the redemption raised, the buyer retried once there was room, and the retry was
        refused as *already used* — a failure that granted them nothing had destroyed the only
        credential they had, and the only way back is the rate-limited door.

        So a failure re-arms the link — but only a failure that provably created nothing. The
        mark is exclusive, so a concurrent second request has already been refused; what is
        left to establish is that no session exists, and that is not true of every failure.
        A store that refuses (:class:`~buyer_svc.auth.sessions.SessionError` — the ceiling, a
        subject it will not admit) has opened nothing by its own contract. A store that
        *fails* — a durable one that wrote the row and then lost the answer on the way back,
        which is the case such a store exists for — may well have opened one, and re-arming
        there is how one token becomes two sessions. Only the refusals give the link back.

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

        entered_open = False
        try:
            if self.accounts.get(email) is None:
                # First link redeemed for this address: magic-link signup and login are one
                # gesture, so the account is created here rather than refused.
                self.accounts.upsert(email, {"email": email})
            pseudonym = self.vault.issue(email)
            for final in (False, True):
                entered_open = True
                try:
                    return self.sessions.open(pseudonym, ttl=self.session_ttl)
                except RetiredPseudonym:
                    # Lost a race: a concurrent redemption for this address rotated the vault
                    # past the pseudonym drawn a moment ago, between the store resolving it and
                    # asking which one is current. Draw again rather than answer a 500 to a
                    # buyer holding a perfectly valid link — `redeem` is the only production
                    # caller of that check, and it does not catch `SessionError`.
                    if final:
                        raise
                    # Back out of the store before redrawing: a failure from `issue` below is
                    # a failure that opened nothing, and must still give the link back.
                    entered_open = False
                    pseudonym = self.vault.issue(email)
            raise AssertionError("unreachable: the loop above either returns or raises")
        except BaseException as exc:
            if entered_open and not isinstance(exc, SessionError):
                # The store was entered and did not refuse — it failed. A store that persisted
                # the session and then lost the answer on the way back (the case a durable
                # store exists for) HAS created one, and re-arming a link whose session may
                # exist is how one token becomes two sessions. Only a `SessionError` is a
                # refusal, and a refusal is the store's contract for "nothing was opened".
                raise
            # The link was spent on a session that does not exist. Give it back rather than
            # burning the buyer's only credential for work that failed — the same rule the
            # magic-link door applies to a rate-limit admission charged for a link it then
            # refused to mail.
            #
            # `BaseException` and not `Exception`, for the reason `auth/routes.py` gives at
            # the same shape of guard: a cancelled task or a KeyboardInterrupt between the
            # mark and the session destroys the link exactly as an I/O error does.
            with self._lock:
                spent = self._pending.get(fingerprint)
                # `used_at == now` and not merely `is not None`: only the request that marked
                # it may un-mark it, so a record swept and re-made in between is left alone.
                #
                # And NOT if the address has been given a NEWER link in the meantime. A
                # consumed record survives `_forget_stale(superseded=...)`, so re-arming
                # unconditionally would resurrect a credential that a later `request_login`
                # had already killed — and killing it is a security property, not tidiness:
                # the buyer who re-requests a link *because* they think the first mail was
                # intercepted must not have the first one handed back to the interceptor.
                #
                # The test is ORDERING, and it has to be. Asking instead "is there another
                # UNREDEEMED link for this address?" is wrong in both directions. It answers
                # "no" once the newer link has been redeemed — MEASURED: with L1's redemption
                # parked, the buyer re-requesting AND redeeming L2, then L1 failing, `_pending`
                # held {L1: unused, L2: used} and replaying L1 opened a SECOND session under an
                # independent pseudonym, while the buyer's own session kept working so nothing
                # signalled it. And simply dropping the used-ness test re-opens the defect this
                # re-arm closes, because a returning buyer always has a consumed record of
                # their own. Neither `used_at` nor `expires_at` can carry this: a redeemed
                # newer link has `used_at` set, and a frozen or coarse clock gives two links
                # the same expiry. A monotonic sequence can.
                #
                # The scan is O(pending) and runs only on a redemption that already failed.
                if (
                    spent is not None
                    and spent.used_at == now
                    and not self._superseded(fingerprint, spent, email)
                ):
                    spent.used_at = None
            raise

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

        It is also where the profile is **published** (T-142), when a publisher was wired.
        This is the one moment in the service's life when a ``BuyerProfile`` exists, so it
        is the only place the store-visible working set can be brought up to date; the
        alternative — publishing at redemption — has no profile to publish, because a
        redemption produces a session and a pseudonym and never builds one.

        A publisher that raises is *not* caught. ``app.buyer_accounts`` is what a store
        reads; serving the buyer a fresh profile while the store's row silently stayed stale
        is a divergence nothing downstream could detect, and a deployment that configured
        the app DSN asked for the publish to happen.

        **Cost, when and only when a floor above 1 is configured.** The whole directory is
        generalised on every profile read, because that is what makes the answer a property
        of a real release rather than of an arbitrary sample: every request computes the same
        deterministic release over the same population, so the profiles actually published
        across buyers form one k-anonymous release. MEASURED on this machine, one
        ``profile_for`` call: 10 ms at 60 accounts, 92 ms at 600, 391 ms at 2400 — linear.
        A deployment large enough for that to hurt wants the release memoised against a
        directory version, **not** a truncated cohort: sampling the population would trade a
        guarantee that holds for one that merely looks like it, which is the exact shape of
        the defect this method was fixed for.
        """
        session = self.sessions.get(session_id)
        email = self.vault.resolve(session.pseudonym)
        account: Mapping[str, Any] = {}
        if email is not None:
            account = self.accounts.get(email) or {}
        # T-221/T-363: this is where a configured k-anonymity floor is spent, because this is
        # the only place production builds a profile. The floor is a property of a release
        # over a population, so the population has to be handed over — `build_profile` cannot
        # invent one from a single buyer. The directory is enumerated ONLY when a floor above
        # 1 is configured; at the documented default of 1 the cohort is ignored by the builder
        # and reading it would be work thrown away on every profile read.
        floor = k_anonymity_floor()
        cohort: Sequence[Mapping[str, Any]] = self.accounts.cohort() if floor > 1 else ()
        profile = build_profile(account, session.pseudonym, k=floor, cohort=cohort)
        if self.publish is not None:
            # After build_profile, never before: build_profile is what raises IdentityLeak,
            # and a profile that failed the identity backstop must not be the thing this
            # writes into a table every store-facing role can read.
            self.publish(profile)
        return profile
