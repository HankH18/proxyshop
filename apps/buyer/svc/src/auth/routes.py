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
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from ..profile import BuyerProfile, IdentityLeak, publish_profile
from ..vault import PostgresPseudonymStore, PseudonymVault, normalise_buyer_key
from .delivery import (
    MagicLinkDeliveryFailed,
    MagicLinkTransportMisconfigured,
    MagicLinkUndeliverable,
    build_magic_link_delivery,
)
from .magic_link import (
    DEFAULT_LINK_TTL,
    DEFAULT_MAX_PENDING,
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
    "BUYER_SVC_MEMORY_LIMIT_BYTES",
    "DEFAULT_MAGIC_LINK_RATE_LIMIT",
    "DEFAULT_MAGIC_LINK_RATE_SUBJECTS",
    "DEFAULT_MAGIC_LINK_RATE_WINDOW",
    "DOT_INSENSITIVE_MAIL_DOMAINS",
    "MAGIC_LINK_RATE_LIMIT_ENV",
    "MAGIC_LINK_RATE_SUBJECTS_ENV",
    "MAGIC_LINK_RATE_SUBJECT_BYTES",
    "MAGIC_LINK_RATE_WINDOW_ENV",
    "MAX_ENV_INT_DIGITS",
    "MAX_MAGIC_LINK_RATE_SUBJECTS",
    "MAX_MAGIC_LINK_RATE_WINDOW",
    "MIN_MAGIC_LINK_RATE_WINDOW",
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
    "rate_limit_subject",
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

#: The window that budget is measured over. EQUAL to
#: :data:`~buyer_svc.auth.magic_link.DEFAULT_LINK_TTL`, both fifteen minutes.
#:
#: This comment used to claim the window was "deliberately longer ... so a refused caller
#: cannot simply wait for their own links to expire and start again at full budget". The
#: constants have never had that property and the claim was never true: MEASURED, a caller who
#: spends the budget at ``t0`` is admitted again at exactly ``t0 + DEFAULT_LINK_TTL``, and if
#: they spent it in a burst the WHOLE budget returns at that instant, because every admission
#: ages out together.
#:
#: The claim is removed rather than the constant changed, because that is the budget working
#: rather than being escaped: five links per fifteen minutes is what it promises, and a caller
#: taking five, waiting fifteen minutes and taking five more is being held to exactly that
#: rate. The sliding window's value is the spread-out case, where admissions return one at a
#: time instead of the whole allowance arriving at a boundary.
#: ``test_the_budget_window_is_not_longer_than_the_link_ttl`` pins both behaviours so that a
#: future change to either constant is a deliberate one.
DEFAULT_MAGIC_LINK_RATE_WINDOW = timedelta(minutes=15)

#: Ceiling on the number of addresses the limiter tracks at once. The limiter's own table is
#: sized by whoever can reach the unauthenticated route, so it needs a bound for exactly the
#: reason the pending-link table does — a limiter that fixes flooding by growing without
#: limit has moved the denial of service rather than closed it.
#:
#: Meeting this ceiling costs a login *nothing*: see :meth:`MagicLinkRateLimiter.check`, which
#: makes room instead of refusing. It bounds memory and it is not a second refusal.
#:
#: Ten times :data:`~buyer_svc.auth.magic_link.DEFAULT_MAX_PENDING`, and the ratio is the
#: point rather than the number. A tracked address is one this service mailed a link to, and
#: the pending ceiling — which refunds what it refuses — caps how many links can be live at
#: once, so at these defaults the tracked table saturates far below this bound and the
#: eviction branch is never reached. Set this at or below the pending ceiling and the limiter
#: starts evicting entries whose budgets are still binding, which loosens the budget instead
#: of the memory. Overridable so a deployment can raise it; see
#: :data:`MAGIC_LINK_RATE_SUBJECTS_ENV`.
DEFAULT_MAGIC_LINK_RATE_SUBJECTS = 100_000

#: Deployment overrides. A limiter whose numbers cannot be changed without a release is one a
#: deployment under attack cannot tighten, and one a load test cannot loosen.
MAGIC_LINK_RATE_LIMIT_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_RATE_LIMIT"
MAGIC_LINK_RATE_WINDOW_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_RATE_WINDOW_SECONDS"

#: The tracked-address ceiling. Overridable for the same reason the other two are, and it had
#: been left out: it is the one number that decides whether the limiter's memory bound can
#: start costing budget accuracy (see :data:`DEFAULT_MAGIC_LINK_RATE_SUBJECTS`), and without
#: this an operator who needed it raised could only get it in a release.
MAGIC_LINK_RATE_SUBJECTS_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_RATE_SUBJECTS"

#: The longest window :data:`MAGIC_LINK_RATE_WINDOW_ENV` may ask for (T-374). EQUAL to
#: :data:`~buyer_svc.auth.magic_link.DEFAULT_LINK_TTL`, and that is the derivation rather than
#: a coincidence.
#:
#: :class:`MagicLinkRateLimiter` explains that its eviction branch is safe not because of the
#: eviction rule but because the branch is unreachable at the shipped ratio of ceilings. That
#: argument has a hidden premise, and the premise is this constant: a tracked address is one
#: this service mailed a link to *inside the window*, so while the window is no longer than
#: the link TTL every tracked entry still has a live record in the pending table, and the
#: tracked table therefore cannot outgrow
#: :data:`~buyer_svc.auth.magic_link.DEFAULT_MAX_PENDING` — an order of magnitude below the
#: subject ceiling. Stretch the window past the TTL and the two tables decouple: pending stays
#: pinned at its ceiling while tracked climbs one flood per TTL, reaches the subject ceiling,
#: and ``max_subjects`` fresh admissions then hand a chosen victim their budget back. MEASURED
#: at scaled proportions (window 4x the TTL, pending ceiling 10): tracked climbed
#: 10 -> 20 -> 30 -> 40 while pending stayed at 10.
#:
#: An operator who wants a *tighter* budget therefore lowers
#: :data:`MAGIC_LINK_RATE_LIMIT_ENV` rather than lengthening the window; a longer window is
#: not a tightening this service can hold safely.
MAX_MAGIC_LINK_RATE_WINDOW = DEFAULT_LINK_TTL

#: The shortest window that override may ask for (T-374), and the other way the same knob
#: switches the limiter off. ``_positive_int_from_env`` refuses only values below 1, so a
#: window of one second was accepted: at :data:`DEFAULT_MAGIC_LINK_RATE_LIMIT` that is five
#: login links per second into one mailbox, which is the flood the limiter exists to stop
#: rather than a budget. One minute is the shortest span over which the shipped budget is
#: still a rate a mailbox survives — 300 links an hour at the default limit — and it is the
#: value the existing override test uses, so the knob keeps the range an operator was already
#: given.
MIN_MAGIC_LINK_RATE_WINDOW = timedelta(seconds=60)

#: Bytes one tracked address costs at a full budget. MEASURED with :mod:`tracemalloc` over
#: 100,000 entries of the real shape — an ``OrderedDict`` keyed by an address string, valued
#: by a list of :data:`DEFAULT_MAGIC_LINK_RATE_LIMIT` distinct ``datetime`` objects: 522.4
#: bytes per entry on CPython 3.13, rounded up. It is the input to
#: :data:`MAX_MAGIC_LINK_RATE_SUBJECTS` and exists so that bound is arithmetic rather than
#: taste.
MAGIC_LINK_RATE_SUBJECT_BYTES = 525

#: The memory this service is given. ``apps/buyer/compose.yaml`` sets ``mem_limit: 256m`` on
#: ``buyer-svc`` (D10), so this is not a guess about the host — it is the number the container
#: is killed at.
BUYER_SVC_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024

#: The largest tracked-address ceiling :data:`MAGIC_LINK_RATE_SUBJECTS_ENV` may ask for
#: (T-376). The override had a floor and no roof, so
#: ``PROXYSHOP_BUYER_MAGIC_LINK_RATE_SUBJECTS=99999999999999999999999`` was accepted verbatim
#: and one typo'd extra digit removed the memory bound the ceiling exists to provide — "a
#: limiter that fixes flooding by growing without limit has moved the denial of service rather
#: than closed it", which is :data:`DEFAULT_MAGIC_LINK_RATE_SUBJECTS`'s own docstring
#: describing what the constant is for.
#:
#: Derived, not chosen: a full table here is ``250,000 * 525 B`` = 125 MiB, just under half of
#: :data:`BUYER_SVC_MEMORY_LIMIT_BYTES`, leaving the other half for the pending-link table,
#: the session store and everything else the process holds. Raising the ceiling stays
#: available — it is 2.5x the shipped default — and the direction that ends in an OOM kill
#: does not.
MAX_MAGIC_LINK_RATE_SUBJECTS = 250_000

#: The longest a configuration integer may be, in characters, before it is refused WITHOUT
#: being parsed (see :func:`_positive_int_from_env`).
#:
#: This is a bound on the DIGIT COUNT and not on the value, because a bound on the value is
#: reached too late. Python integers are arbitrary precision, so ``int("9" * 400)`` succeeds
#: and every range check downstream then happens on a number that arithmetic cannot survive:
#: ``timedelta`` overflows at ~1e14 seconds and ``int / int`` at ~1.8e308, and both of those
#: sat *inside* configuration paths reached from the unauthenticated magic-link route, so an
#: over-long override produced an unhandled 500 rather than the refusal the check intended.
#: Refusing on length converts the input's SIZE — the only thing that is unbounded — into a
#: cheap comparison made before any conversion happens, which is the one form of the check
#: that holds at every magnitude. "Use a bigger float" is not an alternative; there is no
#: float big enough.
#:
#: Twenty admits every value that fits in an unsigned 64-bit integer, and the largest of these
#: knobs can legitimately be asked for is :data:`MAX_MAGIC_LINK_RATE_SUBJECTS` at six digits,
#: so nothing an operator could mean is refused here — only input that was already going to be
#: refused on range, refused before it costs anything.
MAX_ENV_INT_DIGITS = 20

#: Mail domains whose provider documents dots in the local part as insignificant, so
#: ``d.a.n.a@`` and ``dana@`` are ONE mailbox and therefore one budget (T-356).
#:
#: Deliberately a short allow-list rather than a rule applied everywhere. Stripping dots
#: universally is the normalisation that is too aggressive: at most domains ``john.smith@``
#: and ``johnsmith@`` are two different people, and folding them gives one stranger the power
#: to spend the other's login budget. Plus-tagging is handled for every domain because
#: sub-addressing is RFC 5233's convention and is what the measured attack used; dot-folding
#: is handled only where the provider says it is true.
DOT_INSENSITIVE_MAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})

#: Domains that are a second spelling of another provider's. Google delivers both to one
#: mailbox, so keying them apart would leave the same escape open one substitution wider.
_MAIL_DOMAIN_ALIASES = {"googlemail.com": "gmail.com"}

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


def rate_limit_subject(email: str) -> str:
    """The budget key for ``email``: one key per MAILBOX rather than one per spelling (T-356).

    :func:`~buyer_svc.vault.normalise_buyer_key` strips and case-folds, which is the whole of
    what it did, so ``dana+1@x``, ``dana+2@x`` … were separate keys with separate budgets all
    landing in one physical mailbox. MEASURED against the served app: 40 of 40 requests for
    ``dana.reyes+0@`` through ``dana.reyes+39@`` accepted under a configured budget of five.
    Case and whitespace were already handled; the domain of the normalisation was simply too
    narrow for what the limiter claims to protect.

    The rule, stated so its risk is stateable:

    * case-fold and strip, exactly as before — this function starts from the vault's own
      normalisation rather than replacing it;
    * drop a ``+`` tag from the local part, at every domain. Sub-addressing is RFC 5233's
      convention and every large provider implements it;
    * drop dots from the local part ONLY at :data:`DOT_INSENSITIVE_MAIL_DOMAINS`, where the
      provider documents them as insignificant, and fold ``googlemail.com`` onto ``gmail.com``.

    **The risk, and why it is bounded.** Normalisation that is too aggressive merges DISTINCT
    mailboxes, and the cost of that is a real buyer locked out of logging in by a stranger's
    traffic — worse than the abuse being closed. So the aggressive half (dots) is limited to
    the providers where it is a documented fact, and the general half (plus tags) is limited
    to a convention that providers implement rather than a guess. Where the rule is
    nonetheless wrong — a domain that really does deliver ``a+b@`` to a different person than
    ``a@`` — two mailboxes share five links per fifteen minutes. That is a delay measured in
    minutes, never a lockout, it is confined to one domain, and it never merges two buyers'
    identities: this key is used for **nothing but the budget**.

    That last sentence is the load-bearing one. ``request_login``, the account directory and
    the vault all keep the address the buyer actually typed, so the mail goes to the mailbox
    they named and ``dana+work@`` and ``dana+home@`` remain two accounts with two pseudonym
    histories. Widening :func:`~buyer_svc.vault.normalise_buyer_key` itself would merge those
    records repo-wide, which is why T-356 records it as its own ticket rather than a lane edit.

    Raises:
        ValueError: ``email`` is not usable as a key — inherited from
            :func:`~buyer_svc.vault.normalise_buyer_key`, and unreachable from the route,
            whose ``EmailStr`` has already refused an empty body.
    """
    key = normalise_buyer_key(email)
    local, at, domain = key.rpartition("@")
    if not at or not local or not domain:
        # Not an address this function can reason about (no ``@``, or nothing on one side of
        # it). Key it exactly as given rather than inventing a mailbox for it.
        return key
    domain = _MAIL_DOMAIN_ALIASES.get(domain, domain)
    tag = local.find("+")
    if tag > 0:
        # ``> 0`` and not ``>= 0``: a local part that BEGINS with a plus has no tag to strip,
        # and stripping anyway would collapse every such address at a domain onto one key.
        local = local[:tag]
    if domain in DOT_INSENSITIVE_MAIL_DOMAINS:
        local = local.replace(".", "")
    if not local:
        return key
    return f"{local}@{domain}"


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
    Admissions, not attempts. A caller whose request is refused by the pending-link ceiling —
    which happens before a token is minted, let alone mailed — gets the charge back through
    :meth:`refund`, because an admission that bought no link is a slot in this table an
    attacker filled for free. Only that refusal: see the route's own comment for why refunding
    a delivery failure too turns a flaky mail transport into an unbounded mailbomb.
    A refused caller becomes admissible again as soon as their oldest admission ages out of
    the window, which is what makes ``Retry-After`` a real number rather than an invitation
    to a hammering loop that never recovers.

    Its own table cannot become a login outage
    ------------------------------------------
    ``_hits`` is bounded, and the bound is enforced by making room rather than by refusing:
    at the ceiling a new address evicts the address whose admissions are nearest to leaving
    the window anyway. Refusing instead — which is what this did until the flood was measured
    — makes filling the table a *service-wide* denial of login: every buyer the table has not
    already seen is answered 429 for a whole window by an unauthenticated caller, which is
    strictly worse than the abuse the limiter exists to bound.

    Being honest about what eviction costs, because it is not nothing. Evicting the front does
    NOT mean evicting an entry that was about to expire: the front is the nearest to expiry
    **of the addresses currently tracked**, and a table filled in a burst has a front entry
    with most of its window still to run. Worse, a victim who has spent their budget and
    stopped asking becomes the front *by construction*, so a caller who wants to reset one
    does not need to be clever — ``max_subjects`` fresh admissions do it. MEASURED at
    ``max_subjects=50``: a victim refused with ``retry_after=896`` had their entry evicted by
    fifty admissions and took ten links in 55 seconds against a ceiling of five.

    So what keeps this branch safe is NOT the eviction rule. It is the ratio between this
    ceiling and the pending-link ceiling. Every tracked address is one this service actually
    mailed a link to, and :data:`~buyer_svc.auth.magic_link.DEFAULT_MAX_PENDING` caps how many
    links can be live at once while the route refunds every request it refuses — so at the
    defaults a maximal flood saturates this table an order of magnitude below its bound and
    the eviction branch is never entered. :data:`DEFAULT_MAGIC_LINK_RATE_SUBJECTS` is ten
    times the pending ceiling for exactly that reason, and :func:`build_rate_limiter` refuses
    an override that would invert it, because inverting it is what arms the reset above.
    See :meth:`check`.

    What one request costs
    ----------------------
    Not the size of the table. The sweep used to walk every tracked address on every request
    under this lock — measured between 12.8 ms and 62.5 ms per ``check()`` at a hundred
    thousand tracked depending on the machine, serialised — so
    the limiter became the bottleneck a flood exploits and the flood paid for it once and
    made everybody else pay for it forever. ``_hits`` is therefore kept in ascending order of
    each address's newest admission, which is exactly the order they become forgettable in,
    so a request collects what has expired and stops.

    The subject is the address normalised through :func:`rate_limit_subject`, because the
    mailbox is the thing being protected and ``Dana@Example.com``,
    ``dana+shopping@example.com`` and ``dana@example.com`` all reach the same one. This used to
    be :func:`~buyer_svc.vault.normalise_buyer_key`, which case-folds and strips and stops
    there, so the sentence above was a claim the code did not keep: it protected the STRING,
    and forty plus-tags bought forty budgets into one mailbox (T-356, measured over the real
    route). :func:`rate_limit_subject` states the rule and its risk. It is kept **only** as a
    key here and never logged; the limiter answers "how many" and holds nothing else about the
    buyer.

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
        #: address -> its live admissions, oldest first. Ordered by each address's NEWEST
        #: admission, ascending, which is the order the addresses expire in and therefore
        #: also the order they may be evicted in. :meth:`check` maintains that order.
        self._hits: OrderedDict[str, list[datetime]] = OrderedDict()
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

    @property
    def max_subjects(self) -> int:
        """How many addresses may be tracked at once. Meeting it evicts, never refuses."""
        return self._max_subjects

    def _forget_stale(self, now: datetime) -> None:
        """Drop every address whose whole history has left the window. Caller holds ``_lock``.

        Costs what it collects, not what it holds. The old sweep rebuilt every address's hit
        list on every request — O(tracked) inside the global lock, measured between 12.8 ms
        and 62.5 ms per ``check()`` with a hundred thousand addresses tracked, which made the
        limiter itself the amplifier for the flood that filled it.

        ``_hits`` is ordered by each address's newest admission and an address is forgettable
        exactly ``window`` after that admission, so the forgettable ones are a *prefix* and
        this stops at the first survivor. The admissions still inside an address's own list
        are trimmed by :meth:`check` when that address is next looked at, which is the only
        moment their count is used for anything.

        (:meth:`refund` can leave an entry standing later in the order than its remaining
        admissions deserve. That delays its collection by at most one further window and
        never affects a budget — a trimmed list is what :meth:`check` counts — so it is not
        worth an O(tracked) repair of the ordering.)
        """
        cutoff = now - self._window
        while self._hits:
            subject = next(iter(self._hits))
            hits = self._hits[subject]
            if hits and hits[-1] > cutoff:
                return
            del self._hits[subject]

    def check(self, email: str) -> None:
        """Charge one login link to ``email``'s budget, or refuse.

        The ONLY reason this refuses is that ``email`` itself has spent its budget. Meeting
        the ceiling on tracked addresses does not refuse anybody: a new address evicts the
        address nearest to leaving the window instead, because a full table answering 429 to
        every address it has not already seen is an unauthenticated, service-wide denial of
        login, and filling that table is cheaper than the login it denies.

        That eviction CAN clear a chosen victim's budget wherever it is reachable — a victim
        who has spent theirs and stopped asking is the front by construction. What keeps it
        out of reach is the ratio between this ceiling and the pending-link ceiling, which
        :func:`build_rate_limiter` enforces. See the class.

        Raises:
            MagicLinkRateLimited: this address has spent its budget inside the window.
            ValueError: ``email`` is not usable as a key. Unreachable from the route, whose
                ``EmailStr`` has already refused an empty body.
        """
        subject = rate_limit_subject(email)
        now = self._clock()
        with self._lock:
            self._forget_stale(now)
            hits = self._hits.get(subject)
            if hits is None:
                if len(self._hits) >= self._max_subjects:
                    # The front is the address whose newest admission is oldest. NOT
                    # necessarily one that was about to expire, and NOT beyond a caller's
                    # reach: a victim who has spent their budget and stopped asking is the
                    # front by construction, so `max_subjects` fresh admissions evict them
                    # (measured at 50: ten links into one mailbox in 55s against a ceiling of
                    # five). This branch is safe because it is unreachable at the shipped
                    # ratio of ceilings, not because the choice of victim is denied.
                    self._hits.popitem(last=False)
                self._hits[subject] = [now]
                return
            live = [hit for hit in hits if hit > now - self._window]
            if len(live) >= self._limit:
                # Keep the trim, but do NOT move the entry: a refusal is not an admission and
                # must not extend the address's place in the expiry order.
                self._hits[subject] = live
                raise MagicLinkRateLimited(self._retry_after(now, live))
            live.append(now)
            self._hits[subject] = live
            self._hits.move_to_end(subject)

    def refund(self, email: str) -> None:
        """Give back the admission :meth:`check` just charged ``email``.

        For the caller that charged the budget and then could not spend it: the pending-link
        ceiling refused, the delivery transport threw, anything at all went wrong after the
        charge and before a link reached the mailbox. Without this, filling the limiter's
        table is *free* — past
        :data:`~buyer_svc.auth.magic_link.DEFAULT_MAX_PENDING` every request is refused by the
        service and every one of them still leaves an admission behind, so an attacker sizes
        this table without a single further link being mailed.

        Not a way to buy budget: it is reachable only from a request that failed, and it can
        only remove an admission that request had just added. In a race between two
        admissions for one address the newest is removed rather than "this caller's" — they
        are the same value to a hundredth of a second and to every question this table
        answers. Silently does nothing when there is nothing to give back.

        Keyed through :func:`rate_limit_subject`, the same function :meth:`check` charges
        through — the two have to agree, or a refund lands on a key nothing was charged to and
        the admission it was meant to return stays spent for a whole window.
        """
        subject = rate_limit_subject(email)
        with self._lock:
            hits = self._hits.get(subject)
            if not hits:
                return
            hits.pop()
            if not hits:
                del self._hits[subject]

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
        text = raw.strip()
        if len(text) > MAX_ENV_INT_DIGITS:
            # Same shape as `_positive_int_from_env`, gated the same way and for the same
            # reason: `count` is interpolated into the `ProcessLocalStateUnsafe` message
            # below, twice, and `int(<n digits>)` is quadratic in n. Neither the parse nor
            # the message should be sized by an environment variable. A process manager
            # would not read a 400-digit worker count as a worker count either.
            continue
        try:
            count = int(text)
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

    from proxyshop_support.postgres import with_role_password

    # The credential is resolved here rather than assumed to be in the variable, because every
    # DSN this repository ships is password-free (T-112) -- `.env.example`'s and
    # `apps/buyer/compose.yaml`'s alike. Reading the variable raw meant libpq saw an EXPLICITLY
    # EMPTY password and refused with `fe_sendauth: no password supplied` before the request
    # ever reached the server, so this vault could not open in ANY deployment using the shipped
    # configuration. `role_dsn` is not the call here: an unset variable has to keep meaning
    # "no database at all", which `role_dsn` cannot say.
    return PseudonymVault(
        PostgresPseudonymStore(
            psycopg.connect(with_role_password("buyer_vault", dsn), autocommit=True)
        )
    )


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

    from proxyshop_support.postgres import with_role_password

    # Same resolution as the vault's, and for the same measured reason -- see
    # :func:`_vault_from_env`.
    return psycopg.connect(with_role_password("app", dsn), autocommit=True)


def build_profile_publisher() -> Callable[[BuyerProfile], None] | None:
    """The callable that puts a served profile into ``app.buyer_accounts`` (T-142).

    ``None`` when :data:`APP_DSN_ENV` is unset, so a database-less dev boot is unchanged and
    ``GET /buyer/profile`` still answers from memory alone.

    A failure is never swallowed, and a failure is never permanent
    ------------------------------------------------------------
    Both, and they are not in tension. ``app.buyer_accounts`` is "the store-visible working
    set", so a publish that failed silently would leave every store reading a stale row while
    the buyer is served a fresh one and nothing downstream could detect that: the publish is
    therefore never wrapped in a ``try/except`` that *absorbs* it, and a publish this callable
    cannot complete raises, which the route turns into a 500 rather than a 200 over a row that
    was never written.

    But a connection is not a publish. This used to hold one connection for the life of the
    process and open no other, so a single transient blip — a failover, a restart, an idle
    socket the server closed — left ``GET /buyer/profile`` answering 500 to every buyer
    forever, long after the database came back, and the only cure was restarting the service.
    The connection is therefore *replaceable*: a publish that fails discards the connection it
    failed on and retries exactly once on one opened now. The statement underneath is a single
    idempotent upsert, so a retry cannot write twice.

    If that retry fails too, the failure is the answer — loudly, with the original failure
    still on the exception chain — and the next request starts again from a clean slot rather
    than from the connection that is known to be broken.
    """
    connection = _app_connection_from_env()
    if connection is None:
        return None

    # One slot behind one lock, and the lock is NEVER held across the connect. `def` endpoints
    # run in a threadpool, so during an outage every thread in it is in here at once: holding
    # the lock while `psycopg.connect` blocks on a dead host would queue all of them behind
    # one OS-length TCP timeout apiece and starve the threadpool, which would turn a publisher
    # outage into a whole-service outage — routes with nothing to do with profiles included.
    # The cost of connecting outside the lock is that two threads can race to open one; the
    # loser closes its own rather than leaking it.
    held: dict[str, Any] = {"connection": connection}
    slot = threading.Lock()

    def _current() -> Any:
        with slot:
            existing = held["connection"]
        if existing is not None:
            return existing

        fresh = _app_connection_from_env()
        if fresh is None:
            # The DSN went away under a running process. Refusing is the honest answer: a
            # served profile that reaches no store is the silent divergence this publisher
            # exists to prevent.
            raise RuntimeError(
                f"{APP_DSN_ENV} is no longer set, so the profile publisher cannot "
                f"reopen the app-role connection it lost; refusing to serve a "
                f"profile that would never reach app.buyer_accounts"
            )

        with slot:
            if held["connection"] is None:
                held["connection"] = fresh
                return fresh
            winner = held["connection"]
        try:
            fresh.close()
        except Exception:  # pragma: no cover - closing a connection nobody used
            _log.debug("closing a raced-away app-role connection raised", exc_info=True)
        return winner

    def _discard(broken: Any) -> None:
        """Drop a connection the driver has already closed. Deliberately does not close it.

        Only reached for a connection reporting ``closed``, which psycopg sets *because* it
        closed the socket, and psycopg's own ``close()`` returns immediately on one — so
        calling it here would be a no-op rather than a hazard. It is left out because there is
        nothing to do, not because it would break anything: both call sites gate on ``closed``
        first, so this can never be handed a live connection.

        (An earlier version of this comment claimed a shared-connection hazard here. That was
        overstated — it is real only if the ``closed`` gate above is removed, which is what
        makes that gate load-bearing.) Dropping the reference is enough.
        """
        with slot:
            if held["connection"] is broken:
                held["connection"] = None

    def _publish(profile: BuyerProfile) -> None:
        connection = _current()
        try:
            publish_profile(connection, profile)
        except Exception:
            # A dead socket and a rejected statement are not the same failure and must not get
            # the same answer. A constraint violation, a missing grant, a value the column will
            # not take — none of them is a reason to throw away a connection that is still
            # perfectly good, and doing so churns roughly two new connections per request for
            # as long as one buyer's row cannot be written (MEASURED: 40 opened and 40 closed
            # over 20 reads of one unwritable profile), against a role whose `max_connections`
            # is finite. The driver already knows which happened: psycopg marks a connection
            # `closed` when the socket is gone and leaves it open when the server merely said
            # no. So the retry is for the connection, and a statement failure is re-raised on
            # the spot — still loud, still a 500, but without a reconnect it did not need.
            if not getattr(connection, "closed", False):
                raise
            _discard(connection)
            _log.warning(
                "the app-role connection was closed under a buyer-profile publish; "
                "reconnecting and retrying once",
                exc_info=True,
            )
            replacement = _current()
            try:
                publish_profile(replacement, profile)
            except Exception:
                if getattr(replacement, "closed", False):
                    _discard(replacement)
                raise

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

    * **The delivery transport** (T-361), and it is the one that was missing entirely. The
      builder decided everything above and never decided where the link goes, so
      ``build_auth_service().deliver is magic_link._drop`` was True on every production path:
      the route answered ``202 Accepted`` and the token was discarded in-process, which means
      no deployment of this service could complete a login. It is now
      :func:`~buyer_svc.auth.delivery.build_magic_link_delivery`'s output — a real SMTP
      sender when the transport variables are set, and a callable that REFUSES when they are
      not. Never ``_drop``: a ``deliver`` that returns normally having sent nothing is
      indistinguishable from a working one to everything above it, which is exactly how this
      went unnoticed long enough to be closed as refuted.

    * **The worker count**, below.

    Raises:
        ProcessLocalStateUnsafe: a process manager is configured to fork more than one
            worker. Sessions and unredeemed magic links are held in this process's memory
            and there is no shared store for either yet, so a second worker cannot redeem a
            link the first one issued nor recognise a session it minted: behind a load
            balancer roughly half of logins fail, with a ``401`` indistinguishable from a
            genuinely bad link. Refusing the configuration is louder and more honest than
            serving it at a coin-flip success rate.
        MagicLinkTransportMisconfigured: a mail transport is half described — an MTA named
            with no sender, a base URL that is not a URL. Refused here rather than demoted to
            "no transport", because a deployment that meant to send mail and silently does
            not is the state T-361 is about.
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
    # Built BEFORE the service, so a half-configured transport stops the boot rather than the
    # first login: a service that exists and cannot mail is the shape of this defect.
    deliver = build_magic_link_delivery()
    # Spelled out twice rather than assembled into a ``**kwargs`` dict: ``vault`` has a
    # default factory, so there is no value meaning "use the default", and a service built
    # from an unpacked mapping is one no reader — and no static check — can see the wiring of.
    if vault is None:
        return MagicLinkAuth(accounts=accounts, publish=publish, deliver=deliver)
    return MagicLinkAuth(vault=vault, accounts=accounts, publish=publish, deliver=deliver)


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
    """FastAPI dependency. Override this in tests, not the module global.

    Translates :class:`~buyer_svc.auth.delivery.MagicLinkTransportMisconfigured` into the same
    ``503`` "this deployment cannot deliver a login link" that an unconfigured transport gets.
    It has to happen HERE and not in a handler body: FastAPI resolves dependencies before it
    calls the handler, so a ``try`` around ``service.request_login`` never sees this one.

    MEASURED: a half-configured transport does NOT stop the process. ``create_app()`` returns,
    the ASGI lifespan completes and the container healthcheck passes, because nothing builds
    the login stack at start-up — :func:`auth_service` builds it lazily on first use. So the
    refusal lands on the first sign-in request, and before this it landed there as an
    unhandled ``500``: a traceback in the log and a buyer told the service is broken.

    The refusal is not softened. No login is accepted and nothing is delivered; ``str(exc)``
    names the variable that is wrong and reaches the LOG, never the response, which stays the
    same non-oracle answer every caller gets.
    """
    try:
        return auth_service()
    except MagicLinkTransportMisconfigured as exc:
        _log.error("magic-link transport is misconfigured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "no login link was sent: this deployment's magic-link mail transport is not usable"
            ),
            headers={"Retry-After": str(int(DEFAULT_LINK_TTL.total_seconds()))},
        ) from exc


def _positive_int_from_env(name: str) -> int | None:
    """A deployment override, or ``None`` when it is unset or unusable.

    An unparseable, non-positive or over-long value is ignored with a log line rather than
    crashing the boot: the failure mode of a typo'd rate limit must not be a service that will
    not start, and it must not silently be "no limit" either. The default stands.

    The LENGTH check is the one that has to come first, and it is made *before* ``int()``
    rather than after. This function used to hand every caller an integer of unbounded
    magnitude, and each caller then did arithmetic on it: :func:`build_rate_limiter`
    multiplied it by a per-address byte cost and divided the product into a float,
    :func:`_bounded_window_from_env` passed it to ``timedelta``. Both raise ``OverflowError``
    past a magnitude — ~1.8e308 for the float, ~1e14 seconds for ``timedelta`` — and both sit
    on the boot path of an unauthenticated route, so a long enough number in one environment
    variable turned each of those refusals into an unhandled 500. A guard whose refusal path
    crashes is worse than the hole it closed.

    Bounding :data:`MAX_ENV_INT_DIGITS` characters of INPUT, rather than any ceiling on the
    parsed value, is what makes that hold at *every* magnitude including inputs of thousands
    of digits: past the bound nothing is converted, so no arithmetic downstream is ever handed
    a number it cannot survive, and the refusal costs one ``len()``. Every value inside the
    bound is small enough that no arithmetic in this module can overflow on it.

    The refused value is echoed back only in a truncated prefix, for the same reason it is
    refused: it is the one thing here whose size the caller chose, and repeating megabytes of
    it into the log turns a refusal into the amplifier.
    """
    raw = os.environ.get(name)
    if not raw:
        return None
    text = raw.strip()
    if len(text) > MAX_ENV_INT_DIGITS:
        _log.warning(
            "%s is %d characters, past the %d-digit ceiling on a configuration integer; "
            "keeping the default (value begins %r)",
            name,
            len(text),
            MAX_ENV_INT_DIGITS,
            text[:MAX_ENV_INT_DIGITS],
        )
        return None
    try:
        value = int(text)
    except ValueError:
        _log.warning("%s=%r is not an integer; keeping the default", name, text)
        return None
    if value < 1:
        _log.warning("%s=%r is not positive; keeping the default", name, text)
        return None
    return value


def _bounded_window_from_env() -> int | None:
    """The rate-limit window an operator asked for, in seconds, or ``None`` to keep the default.

    ``None`` for unset, for unparseable, and — this is T-374 — for a value outside
    ``MIN_MAGIC_LINK_RATE_WINDOW .. MAX_MAGIC_LINK_RATE_WINDOW``. That range is not
    conservatism: both ends are a documented mechanism.

    * **Above the roof** the tracked table stops draining with the pending table. A tracked
      address is one this service mailed a link to inside the window, so while the window is
      within the link TTL every tracked entry has a live pending record and the tracked table
      is capped by the pending ceiling — an order of magnitude below ``max_subjects``, which
      is exactly why :meth:`MagicLinkRateLimiter.check`'s eviction branch is unreachable.
      Stretch the window and tracked climbs one flood per TTL while pending stays pinned, the
      branch becomes reachable, and ``max_subjects`` fresh admissions reset a chosen victim's
      budget — the attack the subject-ceiling floor was added to stop, re-armed through a
      different variable.
    * **Below the floor** the same knob switches the limiter off. Five links per second into
      one mailbox is the flood, not the budget.

    Refused values keep :data:`DEFAULT_MAGIC_LINK_RATE_WINDOW` and log at WARNING, matching
    ``_positive_int_from_env``'s rule that a typo'd rate limit must not stop the service
    booting and must not silently mean "no limit" either.
    """
    seconds = _positive_int_from_env(MAGIC_LINK_RATE_WINDOW_ENV)
    if seconds is None:
        return None
    # Both bounds are compared in INTEGER SECONDS, and no ``timedelta`` is built from the
    # override until it is known to be in range. `timedelta` keeps its day count in a C int
    # and raises `OverflowError: Python int too large to convert to C int` past roughly 1e14
    # seconds — a twenty-digit override reached it — so constructing one first meant the
    # conversion blew up BEFORE either check below could refuse the value, on the boot path of
    # a route that takes no credential: an unhandled 500 in place of a clean refusal. The
    # comparison never needed a duration object; only the accepted value does, and that one is
    # built by `build_rate_limiter` from the seconds returned here.
    max_seconds = int(MAX_MAGIC_LINK_RATE_WINDOW.total_seconds())
    min_seconds = int(MIN_MAGIC_LINK_RATE_WINDOW.total_seconds())
    if seconds > max_seconds:
        _log.warning(
            "%s=%d is longer than the %ds link TTL, which decouples the limiter's tracked "
            "table from the pending-link table and re-arms a targeted budget reset; keeping "
            "the default of %ds. Tighten %s instead.",
            MAGIC_LINK_RATE_WINDOW_ENV,
            seconds,
            max_seconds,
            int(DEFAULT_MAGIC_LINK_RATE_WINDOW.total_seconds()),
            MAGIC_LINK_RATE_LIMIT_ENV,
        )
        return None
    if seconds < min_seconds:
        _log.warning(
            "%s=%d is shorter than the %ds floor, which admits %d links per %ds into one "
            "mailbox and is the flood rather than the budget; keeping the default of %ds",
            MAGIC_LINK_RATE_WINDOW_ENV,
            seconds,
            min_seconds,
            DEFAULT_MAGIC_LINK_RATE_LIMIT,
            seconds,
            int(DEFAULT_MAGIC_LINK_RATE_WINDOW.total_seconds()),
        )
        return None
    return seconds


def build_rate_limiter() -> MagicLinkRateLimiter:
    """The limiter this process puts in front of ``POST /buyer/auth/magic-link``.

    Reads :data:`MAGIC_LINK_RATE_LIMIT_ENV`, :data:`MAGIC_LINK_RATE_WINDOW_ENV` and
    :data:`MAGIC_LINK_RATE_SUBJECTS_ENV` so a deployment can tighten or loosen the budget
    without a release; unset, it is :data:`DEFAULT_MAGIC_LINK_RATE_LIMIT` links per
    :data:`DEFAULT_MAGIC_LINK_RATE_WINDOW` over at most
    :data:`DEFAULT_MAGIC_LINK_RATE_SUBJECTS` addresses.

    Every one of the three is checked against something other than "is it a positive
    integer", because all three have a range outside which they stop being the knob they look
    like:

    * the **subject ceiling** is refused at or below
      :data:`~buyer_svc.auth.magic_link.DEFAULT_MAX_PENDING`, which arms the targeted budget
      reset described on :class:`MagicLinkRateLimiter`, and above
      :data:`MAX_MAGIC_LINK_RATE_SUBJECTS`, past which the table it bounds no longer fits in
      the memory this service is given (T-376);
    * the **window** is refused outside
      ``MIN_MAGIC_LINK_RATE_WINDOW .. MAX_MAGIC_LINK_RATE_WINDOW``. Longer than the link TTL
      it decouples the tracked table from the pending table and re-arms that same reset from
      the other direction; shorter than a minute the budget stops being a rate a mailbox
      survives (T-374).

    An earlier version of this docstring called the subject ceiling "the one override that
    can make the service LESS safe", which was false in a way that pointed a reader away from
    the window rather than at it: the window was unvalidated, and 86400 and 31536000 were both
    accepted in silence.

    Out of range is REFUSED and logged, never clamped: the safe default stands. Clamping to a
    number nobody asked for is how an operator ends up believing a setting took effect when
    something else did — and the default is the value this module's reasoning is written
    about, so falling back to it is the one choice that keeps the code and its documentation
    describing the same service.
    """
    limit = _positive_int_from_env(MAGIC_LINK_RATE_LIMIT_ENV)
    seconds = _bounded_window_from_env()
    subjects = _positive_int_from_env(MAGIC_LINK_RATE_SUBJECTS_ENV)
    if subjects is not None and subjects > MAX_MAGIC_LINK_RATE_SUBJECTS:
        # T-376. The floor below is about safety; this is about the process surviving its own
        # ceiling. `_positive_int_from_env` used to accept any positive integer, so one extra
        # digit — 99999999999999999999999 was the reproduction — removed the memory bound the
        # constant exists to be. Python's ints do not overflow, so nothing complained until the
        # table was large enough to be OOM-killed, and a limiter that dies of its own table has
        # moved the denial of service rather than closed it.
        #
        # That input is now stopped one step earlier, by `MAX_ENV_INT_DIGITS`, and this branch
        # is what refuses everything between `MAX_MAGIC_LINK_RATE_SUBJECTS` and twenty digits.
        # Both are needed and neither replaces the other: the length gate is what makes the
        # *arithmetic* below safe at every magnitude, and this comparison is what makes the
        # *memory bound* mean 250,000 rather than 10^20.
        #
        # The MiB figures are integer arithmetic on purpose. A logging argument is evaluated
        # EAGERLY — the expression runs before the logger is consulted, so it runs even under
        # `logging.disable(CRITICAL)` and even at a level nothing is emitted at — and the
        # float form of this line (`subjects * BYTES / 1024 / 1024`) raised
        # "OverflowError: integer division result too large for a float" once the product
        # passed the float ceiling. That made THIS refusal the crash: a 500 on an
        # unauthenticated route, reached by making the refused number bigger. `//` on Python
        # ints cannot overflow at any magnitude, `MAX_ENV_INT_DIGITS` keeps `subjects` short
        # enough to format, and the two together are why this line is now safe at any input.
        _log.warning(
            "%s=%d would let the limiter hold %d MiB of tracked addresses against a %d "
            "MiB container limit; keeping the default of %d",
            MAGIC_LINK_RATE_SUBJECTS_ENV,
            subjects,
            subjects * MAGIC_LINK_RATE_SUBJECT_BYTES // (1024 * 1024),
            BUYER_SVC_MEMORY_LIMIT_BYTES // (1024 * 1024),
            DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
        )
        subjects = None
    if subjects is not None and subjects <= DEFAULT_MAX_PENDING:
        # Refused, not honoured. A ceiling at or below the pending-link ceiling is the one
        # setting that arms a targeted budget reset: the tracked table can then be filled with
        # links the service really mailed, and evicting the front is how a caller hands a
        # chosen victim their budget back. MEASURED with a ceiling of 500 against a pending
        # ceiling of 10,000: 500 attacker requests bought five more links into a chosen
        # mailbox, repeatable, for 25 links against a nominal 5.
        #
        # "Bound the memory harder" is a reasonable thing for an operator to want and a
        # dangerous thing to get silently, so this is loud and keeps the safe default rather
        # than clamping to some number nobody asked for. Raising the ceiling is unaffected.
        _log.warning(
            "%s=%d is at or below the pending-link ceiling of %d, which would let a caller "
            "fill the limiter's table with links the service actually mailed and evict a "
            "chosen address's budget; keeping the default of %d",
            MAGIC_LINK_RATE_SUBJECTS_ENV,
            subjects,
            DEFAULT_MAX_PENDING,
            DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
        )
        subjects = None
    return MagicLinkRateLimiter(
        limit=limit if limit is not None else DEFAULT_MAGIC_LINK_RATE_LIMIT,
        window=(
            timedelta(seconds=seconds) if seconds is not None else DEFAULT_MAGIC_LINK_RATE_WINDOW
        ),
        max_subjects=subjects if subjects is not None else DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
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
        #
        # Charged first, therefore refunded below on the one path that provably mails nothing:
        # an admission the service then refused at its pending ceiling was a slot in the
        # limiter's table bought for nothing, and buying them for nothing is how the table
        # gets filled. Only that path — see the refund's own comment for why generalising it
        # is a worse bug than the one it fixes.
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
        # Refunded here and ONLY here, because this is the only refusal that provably happened
        # before a link was mailed: `request_login` raises it at the pending ceiling, before it
        # mints a token and before it calls `deliver`.
        #
        # A blanket `except BaseException: refund` is the obvious generalisation and it is a
        # WORSE defect than the one it closes. `deliver` is the last thing `request_login`
        # does, and mail delivery is at-least-once: an SMTP transport that hands the message
        # over and then loses the connection reading the `250` has put the link in the mailbox
        # AND raised. MEASURED with such a transport, budget of five: 200 requests for one
        # address mailed 200 links and left `limiter.tracked` at 0 — the budget switched off
        # entirely, and an unbounded mailbomb against any chosen address as soon as the MTA
        # gets flaky. The route cannot tell "raised before sending" from "sent, then raised",
        # so it keeps the charge whenever it cannot know. The asymmetry decides it: an
        # un-refunded charge costs one buyer one of five links; a wrong refund costs a victim
        # an unbounded flood.
        limiter.refund(str(body.email))
        _log.warning("magic-link refused: the pending-link table is at its ceiling")
        raise _refuse_link(int(DEFAULT_LINK_TTL.total_seconds()), exc) from exc
    except MagicLinkUndeliverable as exc:
        # T-361. This deployment has no mail transport, so the link was minted and handed to a
        # transport that refuses. Answer 503 — the buyer must not be told a mail is coming.
        # Before this the answer was 202 and `_drop` swallowed the token, which is the whole
        # ticket: the login gesture could not complete in any deployment and nothing said so.
        #
        # NOT collapsed into the 429 above, and not `_LINK_REFUSED_DETAIL`. Those two refusals
        # are one message because telling them apart would let an unauthenticated caller read
        # the service's STATE — how full its table is, whether this address has been asking.
        # This one is not state: it is the same answer to every caller for every address until
        # an operator changes the configuration, so it is no oracle, and saying it plainly is
        # what turns a silent outage into a fixable one. `str(exc)` names the variables and
        # nothing about the request; see `_undeliverable`.
        #
        # The admission is NOT refunded and the pending record is left to expire. Refunding
        # here would be safe on its own terms — nothing was mailed — but it would put a second
        # "refund on failure" path next to the one whose comment above explains why
        # generalising it turns a flaky transport into a mailbomb, and it buys a caller
        # nothing on a service where every login is refused anyway. The pending table stays
        # bounded by `max_pending` either way.
        _log.error("magic-link refused: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "no login link was sent: this deployment has no magic-link mail transport "
                "configured"
            ),
            headers={"Retry-After": str(int(DEFAULT_LINK_TTL.total_seconds()))},
        ) from exc
    except MagicLinkDeliveryFailed as exc:
        # A transport IS configured and the conversation with the MTA did not complete: it
        # refused the connection, the certificate did not verify, the credential was rejected,
        # or it timed out. Before this the exception escaped the handler entirely and FastAPI
        # answered a bare `500 Internal Server Error` with a traceback in the log and nothing
        # in it an operator could act on — MEASURED against a sink that would not offer
        # STARTTLS. It is a different answer from the 503 above on purpose: that one says this
        # deployment has no transport, which is a standing configuration fact, and this one
        # says the transport it has could not be reached right now.
        #
        # `502` and not `503`: the dependency that failed is upstream of this service and the
        # service itself is healthy. Retry-After is the link TTL, matching the two refusals
        # above, because nothing here knows when the MTA comes back.
        #
        # `str(exc)` is logged and NOT put on the response. It names the MTA's host, its port
        # and its own words — exactly what an operator needs and exactly what an
        # unauthenticated caller must not be handed, because "535 authentication failed"
        # against "connection refused" is a description of this deployment's plumbing. It
        # carries no password, no token and no address; see `MagicLinkDeliveryFailed`.
        #
        # The admission is NOT refunded, for the reason spelled out at the `MagicLinkThrottled`
        # clause: SMTP is at-least-once, so an MTA that took the message and then broke reading
        # the `250` has put a live link in the mailbox AND raised. Refunding on the path that
        # cannot tell those apart is what turns a flaky MTA into an unbounded mailbomb against
        # any chosen address.
        _log.error("magic-link delivery failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "no login link was sent: this deployment's mail transport could not be "
                "reached. It may still arrive; try again in a few minutes"
            ),
            headers={"Retry-After": str(int(DEFAULT_LINK_TTL.total_seconds()))},
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
