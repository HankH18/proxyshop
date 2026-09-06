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
import math
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from ..profile import BuyerProfile, IdentityLeak, publish_profile
from ..vault import PostgresPseudonymStore, PseudonymVault, normalise_buyer_key
from .magic_link import (
    DEFAULT_LINK_TTL,
    AccountDirectory,
    InMemoryAccountDirectory,
    MagicLinkAuth,
    MagicLinkError,
    MagicLinkThrottled,
)
from .sessions import SessionError

_log = logging.getLogger(__name__)

__all__ = [
    "APP_DSN_ENV",
    "DEFAULT_MAGIC_LINK_RATE_LIMIT",
    "DEFAULT_MAGIC_LINK_RATE_SUBJECTS",
    "DEFAULT_MAGIC_LINK_RATE_WINDOW",
    "MAGIC_LINK_RATE_LIMIT_ENV",
    "MAGIC_LINK_RATE_WINDOW_ENV",
    "VAULT_DSN_ENV",
    "WORKER_COUNT_ENVS",
    "MagicLinkRateLimited",
    "MagicLinkRateLimiter",
    "ProcessLocalStateUnsafe",
    "account_directory",
    "auth_service",
    "build_account_directory",
    "build_auth_service",
    "build_profile_publisher",
    "build_rate_limiter",
    "get_auth_service",
    "get_rate_limiter",
    "router",
    "set_account_directory",
    "set_auth_service",
]

router = APIRouter(prefix="/buyer", tags=["buyer-auth"])

#: DSN for the one role D5 lets near ``vault.*``. Documented in ``.env.example``; when it is
#: set the login service keeps its email↔pseudonym history in ``vault.pseudonym_history``
#: rather than in this process's memory.
VAULT_DSN_ENV = "PROXYSHOP_PG_DSN_VAULT"

#: DSN for the ``app`` role — the one D5 lets write ``app.*``. ``apps/buyer/compose.yaml``
#: has been handing this service that variable since the service existed while no line of
#: ``apps/buyer`` read it; with it set, a served profile is also upserted into
#: ``app.buyer_accounts``, the store-visible working set (T-142).
APP_DSN_ENV = "PROXYSHOP_PG_DSN_APP"

#: The variables a process manager sets when it will fork more than one worker. Standard
#: names, not invented ones: uvicorn and gunicorn both read ``WEB_CONCURRENCY``.
WORKER_COUNT_ENVS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS")

#: How many login links one address may be mailed inside :data:`DEFAULT_MAGIC_LINK_RATE_WINDOW`.
#: A buyer who mistypes, loses the mail and retries needs a handful; nobody needs twenty.
DEFAULT_MAGIC_LINK_RATE_LIMIT = 5

#: The window that budget is measured over. Deliberately longer than
#: :data:`~buyer_svc.auth.magic_link.DEFAULT_LINK_TTL` (fifteen minutes), so a refused caller
#: cannot simply wait for their own links to expire and start again at full budget.
DEFAULT_MAGIC_LINK_RATE_WINDOW = timedelta(minutes=15)

#: Ceiling on the number of addresses the limiter tracks at once. The limiter's own table is
#: sized by whoever can reach the unauthenticated route, so it needs a bound for exactly the
#: reason the pending-link table does — a limiter that fixes flooding by growing without
#: limit has moved the denial of service rather than closed it.
DEFAULT_MAGIC_LINK_RATE_SUBJECTS = 100_000

#: Deployment overrides. A limiter whose numbers cannot be changed without a release is one a
#: deployment under attack cannot tighten, and one a load test cannot loosen.
MAGIC_LINK_RATE_LIMIT_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_RATE_LIMIT"
MAGIC_LINK_RATE_WINDOW_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_RATE_WINDOW_SECONDS"

_service: MagicLinkAuth | None = None
_accounts: AccountDirectory | None = None
_accounts_lock = threading.Lock()
_limiter_lock = threading.Lock()


class ProcessLocalStateUnsafe(RuntimeError):
    """The deployment asks for more workers than this service's state model can survive."""


class MagicLinkRateLimited(RuntimeError):
    """This address has been mailed as many login links as its budget allows (T-165).

    Distinct from :class:`~buyer_svc.auth.magic_link.MagicLinkThrottled`, which reports that
    the service's *memory* ceiling was met, and which
    ``magic_link.py`` itself documents as "not a rate limiter". The two answer the same 429
    and mean different things: throttled is "the service is full", limited is "you, in
    particular, have had enough".

    Carries ``retry_after`` in whole seconds so the route can answer with the header a
    well-behaved client already knows how to obey. It never carries the address.
    """

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"this address has reached its login-link budget; retry in {retry_after}s")
        self.retry_after = retry_after


class MagicLinkRateLimiter:
    """A per-address budget on the unauthenticated magic-link door.

    Why this is not in :class:`~buyer_svc.auth.magic_link.MagicLinkAuth`
    -------------------------------------------------------------------
    ``request_login`` already bounds the *table* of unredeemed links, and it does so by
    superseding: a new link for an address drops that address's outstanding one. That is
    correct — a re-requested link must kill the one that may have been intercepted — and it
    is precisely why the memory ceiling structurally cannot see this abuse. Twenty requests
    for one mailbox leave ``pending_links`` at 1 while twenty live tokens land in it.

    Making ``request_login`` itself refuse would conflate two properties that must both hold
    and are not the same one: *the table stays small however many times one address asks*
    (asserted by ``test_pending_links_do_not_accumulate_for_an_unauthenticated_caller``, a
    thousand calls for one address, all of which must succeed) and *the door in front of it
    stops mailing after a handful*. So the budget lives at the door, which is also where
    ``magic_link.py`` says it belongs: "a deployment still wants one in front of the route".

    What it counts
    --------------
    Admissions, not attempts. A refused caller becomes admissible again as soon as their
    oldest admission ages out of the window, which is what makes ``Retry-After`` a real
    number rather than an invitation to a hammering loop that never recovers.

    The subject is the address, normalised through
    :func:`~buyer_svc.vault.normalise_buyer_key`, because the mailbox is the thing being
    protected and ``Dana@Example.com`` reaches the same one as ``dana@example.com``. It is
    kept **only** as a key here and never logged; the limiter answers "how many" and holds
    nothing else about the buyer.

    What it is not
    --------------
    Process-local, like everything else in this service's state model — see
    :class:`ProcessLocalStateUnsafe`, which already refuses a multi-worker configuration
    outright. Behind several replicas each would keep its own budget and the effective limit
    would be ``replicas × limit``; closing that needs the shared store T-165's other half
    names, and the refusal above is what keeps this honest in the meantime.
    """

    def __init__(
        self,
        *,
        limit: int = DEFAULT_MAGIC_LINK_RATE_LIMIT,
        window: timedelta = DEFAULT_MAGIC_LINK_RATE_WINDOW,
        max_subjects: int = DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if limit < 1:
            raise ValueError("a magic-link budget of less than one link refuses every login")
        if window <= timedelta(0):
            raise ValueError("a rate-limit window must be a positive duration")
        if max_subjects < 1:
            raise ValueError("a limiter that tracks no addresses is not a limiter")
        self._limit = limit
        self._window = window
        self._max_subjects = max_subjects
        self._clock = clock if clock is not None else _utcnow
        self._hits: dict[str, list[datetime]] = {}
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        """Links per address per :attr:`window`."""
        return self._limit

    @property
    def window(self) -> timedelta:
        """The span the budget is measured over."""
        return self._window

    @property
    def tracked(self) -> int:
        """How many addresses currently hold a live admission. Bounded; see the class."""
        with self._lock:
            return len(self._hits)

    def _forget_stale(self, now: datetime) -> None:
        """Drop every admission that has left the window, and every address left empty.

        The caller holds ``_lock``. Sweeping on the request keeps the cost proportional to
        the work being asked for rather than needing a timer, exactly as
        ``MagicLinkAuth._forget_stale`` and ``InMemorySessionStore._forget_expired`` do.
        """
        cutoff = now - self._window
        empty = []
        for subject, hits in self._hits.items():
            live = [hit for hit in hits if hit > cutoff]
            if live:
                self._hits[subject] = live
            else:
                empty.append(subject)
        for subject in empty:
            del self._hits[subject]

    def check(self, email: str) -> None:
        """Charge one login link to ``email``'s budget, or refuse.

        Raises:
            MagicLinkRateLimited: the address has spent its budget, or the limiter is
                holding as many addresses as it may and this is a new one. New subjects are
                shed rather than tracked ones evicted, for the same reason ``request_login``
                sheds: evicting the oldest would let an unauthenticated caller clear a chosen
                victim's budget — and, worse, their own — on demand.
            ValueError: ``email`` is not usable as a key. Unreachable from the route, whose
                ``EmailStr`` has already refused an empty body.
        """
        subject = normalise_buyer_key(email)
        now = self._clock()
        with self._lock:
            self._forget_stale(now)
            hits = self._hits.get(subject)
            if hits is None and len(self._hits) >= self._max_subjects:
                raise MagicLinkRateLimited(self._retry_after(now, [now]))
            if hits is not None and len(hits) >= self._limit:
                raise MagicLinkRateLimited(self._retry_after(now, hits))
            self._hits.setdefault(subject, []).append(now)

    def _retry_after(self, now: datetime, hits: list[datetime]) -> int:
        """Whole seconds until the oldest admission leaves the window. At least one."""
        freed = min(hits) + self._window
        return max(1, math.ceil((freed - now).total_seconds()))


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


def build_account_directory() -> AccountDirectory:
    """The account directory a deployment starts with (T-140).

    An :class:`~buyer_svc.auth.magic_link.InMemoryAccountDirectory`, deliberately, and it is
    the *shape* of the seam that is the fix rather than this default. Before this existed
    ``build_auth_service`` decided the vault and nothing else, so every service the process
    built got its own brand-new empty directory: the only writer of a buyer record anywhere
    in the tree was ``redeem``'s ``upsert(email, {"email": email})``, a record can therefore
    never carry anything the login gesture already knew, and ``GET /buyer/profile`` returned
    the same information-free buckets for every buyer alive.

    :func:`account_directory` keeps one of these per process and
    :func:`build_auth_service` hands it to every service it builds, so a populator — an
    importer, an admin path, an order feed — writes once and every service in the process,
    including one rebuilt after this one, reads it. That is what makes a served profile a
    coarsening of a real record instead of a function of the address.

    NOT durable, and the docstring says so rather than the reader discovering it: an
    in-memory directory forgets every buyer on a real process restart. Closing that needs a
    table for the *unredacted* account record, which does not exist — ``app.buyer_accounts``
    holds only (pseudonym, buckets) by design — so it needs a migration, which is outside
    this module. :func:`set_account_directory` is the seam a durable implementation installs
    itself through when it lands.
    """
    return InMemoryAccountDirectory()


def account_directory() -> AccountDirectory:
    """The process-wide account directory, built on first use.

    Process-wide on purpose and not per-service: a directory belonging to one
    ``MagicLinkAuth`` would be forgotten every time the service was rebuilt, which is the
    finding — "a service rebuilt as a restart would rebuild it has forgotten every buyer".
    """
    global _accounts
    if _accounts is None:
        with _accounts_lock:
            if _accounts is None:
                _accounts = build_account_directory()
    return _accounts


def set_account_directory(directory: AccountDirectory | None) -> None:
    """Install (or, with ``None``, forget) the process-wide account directory.

    The deployment seam. A durable directory — DSN-backed, an importer's output, a fake for
    a test — is installed here before the first request, exactly as
    :func:`set_auth_service` installs a login service.
    """
    global _accounts
    _accounts = directory


def _app_connection_from_env() -> Any | None:
    """A connection authenticated as ``app``, when its DSN is configured. Otherwise ``None``.

    ``app`` and not ``buyer_vault``: T-011's grant model gives the vault role only ``SELECT``
    on ``app.*``, so the role that can read the email↔pseudonym mapping deliberately cannot
    write the store-visible table. Two schemas, two roles, and no single connection that can
    join them — which is why this is a second connection rather than the vault's.

    Autocommit for the same reason :func:`_vault_from_env` uses it: a long-lived service that
    left each statement sitting in an open transaction would pin a snapshot and block DDL for
    as long as it ran.
    """
    dsn = os.environ.get(APP_DSN_ENV)
    if not dsn:
        return None
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def build_profile_publisher() -> Callable[[BuyerProfile], None] | None:
    """The callable that puts a served profile into ``app.buyer_accounts`` (T-142).

    ``None`` when :data:`APP_DSN_ENV` is unset, so a database-less dev boot is unchanged and
    ``GET /buyer/profile`` still answers from memory alone.

    Deliberately NOT wrapped in ``try/except``. ``app.buyer_accounts`` is described as "the
    store-visible working set"; a deployment that configured this DSN has asked for buyer
    profiles to reach the stores, and a publish that fails silently would leave every store
    reading a stale row while the buyer is served a fresh one and nothing anywhere says so.
    A failure here is a real failure and is allowed to be loud.
    """
    connection = _app_connection_from_env()
    if connection is None:
        return None

    def _publish(profile: BuyerProfile) -> None:
        publish_profile(connection, profile)

    return _publish


def build_auth_service() -> MagicLinkAuth:
    """Construct the login service this process will serve from.

    Four things are decided here, and before this existed none of them was decided anywhere:
    the service was a bare ``MagicLinkAuth()``, :class:`PostgresPseudonymStore` had no caller
    outside its own tests, and neither did
    :func:`~buyer_svc.profile.publish_profile`.

    * **The vault.** With :data:`VAULT_DSN_ENV` set the email↔pseudonym history lives in
      ``vault.pseudonym_history`` and survives a restart, which is what makes R5's "a
      retired pseudonym is never handed out again" a property of the *system* rather than
      of one process's lifetime — an in-memory vault forgets every pseudonym it ever issued
      on restart, leaving the freshness check nothing to check against. Without the DSN the
      in-memory default is kept, so a database-less dev boot is unchanged.

    * **The account directory** (T-140). Every service built here shares the process's one
      directory, so a buyer record written through it outlives the service that was running
      when it was written. Without this, ``accounts=`` was never passed at all: production
      ran the empty default, and every coarsener therefore ran on an empty account.

    * **The profile publisher** (T-142). With :data:`APP_DSN_ENV` set, a profile served over
      ``GET /buyer/profile`` is also upserted into ``app.buyer_accounts`` through an
      ``app``-role connection. ``apps/buyer/compose.yaml`` has been handing this service that
      DSN while no line of ``apps/buyer`` read it.

    * **The worker count**, below.

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
    accounts = account_directory()
    publish = build_profile_publisher()
    # Spelled out twice rather than assembled into a ``**kwargs`` dict: ``vault`` has a
    # default factory, so there is no value meaning "use the default", and a service built
    # from an unpacked mapping is one no reader — and no static check — can see the wiring of.
    if vault is None:
        return MagicLinkAuth(accounts=accounts, publish=publish)
    return MagicLinkAuth(vault=vault, accounts=accounts, publish=publish)


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


def _positive_int_from_env(name: str) -> int | None:
    """A deployment override, or ``None`` when it is unset or unusable.

    An unparseable or non-positive value is ignored with a log line rather than crashing the
    boot: the failure mode of a typo'd rate limit must not be a service that will not start,
    and it must not silently be "no limit" either. The default stands.
    """
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        _log.warning("%s=%r is not an integer; keeping the default", name, raw)
        return None
    if value < 1:
        _log.warning("%s=%r is not positive; keeping the default", name, raw)
        return None
    return value


def build_rate_limiter() -> MagicLinkRateLimiter:
    """The limiter this process puts in front of ``POST /buyer/auth/magic-link``.

    Reads :data:`MAGIC_LINK_RATE_LIMIT_ENV` and :data:`MAGIC_LINK_RATE_WINDOW_ENV` so a
    deployment can tighten or loosen the budget without a release; unset, it is
    :data:`DEFAULT_MAGIC_LINK_RATE_LIMIT` links per :data:`DEFAULT_MAGIC_LINK_RATE_WINDOW`.
    """
    limit = _positive_int_from_env(MAGIC_LINK_RATE_LIMIT_ENV)
    seconds = _positive_int_from_env(MAGIC_LINK_RATE_WINDOW_ENV)
    return MagicLinkRateLimiter(
        limit=limit if limit is not None else DEFAULT_MAGIC_LINK_RATE_LIMIT,
        window=(
            timedelta(seconds=seconds) if seconds is not None else DEFAULT_MAGIC_LINK_RATE_WINDOW
        ),
    )


def get_rate_limiter(request: Request) -> MagicLinkRateLimiter:
    """FastAPI dependency: the limiter belonging to the application serving this request.

    Held on ``app.state`` rather than in a module global on purpose. The budget is a property
    of one running service, and a module global would make every application built in a
    process — every ``create_app()`` in a test session, every app a future embedder mounts —
    share one table, so an unrelated caller's history could refuse a login. A deployment
    boots one app (``buyer_svc.main.app``), so the production reading is unchanged.

    Override this in tests to pin a clock or a budget, exactly as with
    :func:`get_auth_service`.
    """
    state = request.app.state
    limiter = getattr(state, "magic_link_rate_limiter", None)
    if limiter is None:
        # `def` endpoints run in a threadpool, so two first requests really do arrive at
        # once; without the lock they would build two limiters and one would be discarded
        # along with whatever it had already counted.
        with _limiter_lock:
            limiter = getattr(state, "magic_link_rate_limiter", None)
            if limiter is None:
                limiter = build_rate_limiter()
                state.magic_link_rate_limiter = limiter
    return limiter


ServiceDep = Annotated[MagicLinkAuth, Depends(get_auth_service)]
RateLimiterDep = Annotated[MagicLinkRateLimiter, Depends(get_rate_limiter)]
SessionHeader = Annotated[str | None, Header(alias="X-Buyer-Session")]

#: The one answer ``POST /buyer/auth/magic-link`` gives to every reason it will not mail a
#: link. Two refusals reach it — this address has spent its budget, and the service's pending
#: table is full — and they are deliberately one message, for the reason the redeem route
#: collapses unknown/expired/already-used into one 401: an unauthenticated caller must not be
#: able to read the service's state off the wire. The distinction is kept in the logs.
_LINK_REFUSED_DETAIL = "no login link was sent; please try again later"


def _refuse_link(retry_after: int, cause: Exception) -> HTTPException:
    """A 429 that says when to come back and nothing else.

    ``Retry-After`` is on BOTH refusals so that the header's presence cannot be read as
    "this address in particular has been asking". Its *value* still varies with the reason —
    a rate-limited caller is told when their own oldest admission ages out — and that is a
    deliberate trade rather than an oversight: a client that cannot be told when to return
    retries blindly, which is worse for the service than the residual disclosure, and the
    residual is "somebody recently requested links for this mailbox", which the caller can
    only observe after spending the budget themselves.
    """
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=_LINK_REFUSED_DETAIL,
        headers={"Retry-After": str(max(1, retry_after))},
    )


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
def request_magic_link(
    body: MagicLinkRequest, service: ServiceDep, limiter: RateLimiterDep
) -> MagicLinkAccepted:
    try:
        # T-165. Charged BEFORE the link is minted, because the resource being protected is
        # the buyer's mailbox rather than this process's memory: a link that is issued and
        # then refused has already been mailed. `MagicLinkAuth.max_pending` cannot stand in
        # for this — a repeated address supersedes its own pending link, so the table stays
        # at one entry while twenty live tokens go out.
        limiter.check(str(body.email))
    except MagicLinkRateLimited as exc:
        _log.info("magic-link refused: this address has spent its login-link budget")
        raise _refuse_link(exc.retry_after, exc) from exc
    try:
        issued = service.request_login(str(body.email))
    except MagicLinkThrottled as exc:
        # The route takes no credential, so the size of the pending-link table is chosen by
        # whoever can reach it. At the ceiling the service sheds new requests; it never drops
        # a link somebody is already holding, which would hand an unauthenticated caller a
        # way to cancel a chosen buyer's login.
        #
        # Answered in exactly the shape the budget refusal above uses, and neither message
        # names the address. Retry-After is the link TTL: a full pending table drains as the
        # links in it expire, and nothing shorter is honest.
        _log.warning("magic-link refused: the pending-link table is at its ceiling")
        raise _refuse_link(int(DEFAULT_LINK_TTL.total_seconds()), exc) from exc
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
