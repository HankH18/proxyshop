"""The check happens AFTER the shopper has gone, and this module is why it can.

Hard constraint, stated first because it shaped everything else: **the shortlist must render
at unchanged latency.** A live fetch per candidate is seconds — a robots.txt round trip plus a
page, times four slots, against servers the platform does not control — and a shopper waiting
on four third-party storefronts is a worse product than one that is never checked at all.

So the shopper's path does exactly one thing here: :func:`queue_live_checks` resolves each
slot to a target and appends it to an in-memory queue. No socket, no DNS, no file, no clock
read that anything waits on, and no regular expression over the seller's prose — the pitch is
carried as text and decomposed at drain time, where it is free.

MEASURED on this branch, over the served ``POST /buyer/shortlist/render`` at four slots, by
swapping this function for a no-op inside ONE process and interleaving the two arms (a
between-process comparison measures the machine, not the change — the same route measured 1.03
ms earlier in this session and 2.3 ms later, with six other lanes building):

======================================  ==========  ==========  ==============
arm                                     p50         p95         vs no enqueue
======================================  ==========  ==========  ==============
enqueue replaced by a no-op             2.322 ms    ~3.3 ms     —
4 slots, no store registered (refused)  2.381 ms    ~3.0 ms     +59 us (+2.5%)
4 slots resolved and queued             2.543 ms    ~3.2 ms     +106 us (+4.3%)
======================================  ==========  ==========  ==============

1,600 samples per arm. :func:`queue_live_checks` on its own is 25 us per call at four slots.
For scale: ONE robots.txt round trip against a real storefront is tens of milliseconds before
the page itself, so a synchronous check is not a few percent, it is one to two orders of
magnitude — which is the whole argument below.

**Why a verdict that lands late is still worth having**, which is the part that is easy to
doubt: a contradiction is recorded against the STORE, not against the auction. It lands as a
``claim_verified`` ledger event on ``catalog_claim_accuracy``, which is the trust dimension
``contracts.TrustDimension`` publishes for exactly this — product-fact verification outcomes —
and a store's trust score is an input to every auction it enters afterwards. The shopper who
was mis-quoted is not helped by their own auction; every shopper after them is. That is the
same shape ``exchange.ranking.features.contradicted_claim_events`` gives the 0.15
``contradicted_claim`` penalty inside one auction, one surface and one clock-tick over.

A synchronous check was considered and rejected on those numbers rather than on taste. The
render path is ~2.5 ms; a robots.txt fetch plus a product page against a server the platform
does not control is two orders of magnitude more than the whole request, times the number of
slots, and the check is *optional evidence about a store*. Trading a 100x latency regression
on every shopper for evidence that is equally good one minute later is not a trade this route
may make.

What is deliberately NOT here
-----------------------------
No thread, no timer, no background task started at import. The drain is called — by
``POST /buyer/livecheck/run``, by a worker, or by a test — because a module that starts a
thread when it is imported starts one per test file too, and because a queue drained by
something invisible is a queue nobody can prove was drained. See :mod:`buyer_svc.livecheck
.routes`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from claim_verification.live_page import (
    AGREES,
    CONTRADICTED,
    LIVE_PAGE_SURFACE,
    NO_VERDICT,
    check_pitch_against_page,
    page_vocabulary,
    read_product_page,
    unreadable_page,
)
from claim_verification.pitch import decompose_pitch, pitch_ref_for

from ..accept._reading import plain, read, text
from .fetching import NoPageFetcher
from .targets import LiveCheckTarget, TargetRefused, targets_for
from .targets import product_pages as _pages
from .targets import registered_domains as _domains

__all__ = [
    "ANONYMOUS_CALLER",
    "CLAIM_VERIFIED_KIND",
    "DEDUP_WINDOW_SECONDS",
    "LEDGER_SINK_METHODS",
    "LIVE_CHECK_DIMENSION",
    "MAX_FETCHES_PER_ORIGIN",
    "MAX_PITCH_TEXT_CHARS",
    "MAX_QUEUED_TARGETS",
    "MAX_RECORDED_CHECKS",
    "MAX_REMEMBERED_URLS",
    "MAX_TRACKED_BUDGETS",
    "ORIGIN_WINDOW_SECONDS",
    "REFUSED_DUPLICATE",
    "REFUSED_OVER_BUDGET",
    "FetchBudget",
    "LiveCheckLedger",
    "LiveCheckQueue",
    "LiveCheckRecord",
    "live_check_ledger",
    "live_check_queue",
    "queue_live_checks",
    "run_live_checks",
    "set_live_check_ledger",
    "set_live_check_queue",
]

_log = logging.getLogger(__name__)

#: The published ledger kind a cross-surface contradiction lands as. It already exists —
#: ``contracts.LedgerEventKind.claim_verified``, payload shape ``(claim_ref, status, dim)`` —
#: so this feature invents no vocabulary anywhere: no new event kind, no new trust dimension,
#: no new status. That is not tidiness. ``packages/contracts`` is another lane's file scope,
#: and a feature that needed a schema change there could not have shipped from here at all.
CLAIM_VERIFIED_KIND = "claim_verified"

#: ``contracts.TrustDimension.catalog_claim_accuracy``, spelled rather than imported for the
#: reason ``feedback.submission`` spells ``"feedback"``: it is a ``StrEnum`` and the wire
#: carries the plain string. D53 puts product-fact verification outcomes here precisely
#: because they have no honest home among the transaction dimensions.
LIVE_CHECK_DIMENSION = "catalog_claim_accuracy"

#: Method names a ledger sink may expose, most specific first. The same tuple
#: ``buyer_svc.feedback.submission`` publishes, because it is the same sink object — one
#: buyer service, one ``app.state.ledger_sink``, one calling convention.
LEDGER_SINK_METHODS: tuple[str, ...] = (
    "append",
    "emit",
    "record",
    "publish",
    "write",
    "log_event",
    "add",
)

#: The most targets the queue holds. A bound on a structure a shopper's traffic grows: four
#: slots per render, and a render is a browser request. Past it the OLDEST target is dropped
#: rather than the newest refused, because the newest is the one somebody is still looking at
#: and the oldest has had its chance — and the drop is COUNTED
#: (:attr:`LiveCheckQueue.dropped`, served on ``POST /buyer/livecheck/run``), because a queue
#: silently shedding work looks exactly like a queue with no work.
#:
#: Worst-case resident size, stated rather than left to be discovered: each target carries the
#: seller's prose up to :data:`MAX_PITCH_TEXT_CHARS`, so 512 x 20,000 characters is ~10 MB if
#: every queued store filled its pitch to the ceiling. Real pitches are a few hundred
#: characters; the ceiling exists because the length is the BIDDER's choice.
MAX_QUEUED_TARGETS = 512

#: The most records kept for read-back. A ring for the same reason ``MAX_RECORDED_AUCTIONS``
#: is one: this is a process-local diagnostic, and the durable copy is the ledger event.
MAX_RECORDED_CHECKS = 256

#: The most pitch characters carried into the queue. ``claim_verification.pitch`` refuses
#: anything over ``MAX_PITCH_CHARS`` (20,000) outright, so text beyond that would be carried
#: across the queue only to be dropped; it is not carried.
MAX_PITCH_TEXT_CHARS = 20_000


# ==============================================================================================
# What one caller may cost a merchant
# ==============================================================================================

#: Which bound refused a target. A token rather than a sentence: see :meth:`FetchBudget.admit`.
REFUSED_DUPLICATE = "duplicate"
REFUSED_OVER_BUDGET = "over_budget"

#: The caller key every render that carries no live buyer session shares.
#:
#: ONE bucket for the whole anonymous internet, deliberately. See :class:`FetchBudget`.
ANONYMOUS_CALLER = "anonymous"

#: How long the platform remembers having fetched a URL, and declines to fetch it again.
#:
#: Five minutes because of what the check IS: a drift detector over a storefront page. A page
#: that moved its price inside five minutes moved it again by the time a shopper reads the
#: verdict, and re-reading it that often buys the platform nothing it does not already hold —
#: while re-reading it on demand is the cheapest amplifier there is, since replaying one
#: captured request body costs the caller a single POST.
DEDUP_WINDOW_SECONDS = 300.0

#: The most fetches ONE caller may cause at ONE merchant host inside
#: :data:`ORIGIN_WINDOW_SECONDS`.
#:
#: Twelve, against a real shortlist of four slots: a shopper who re-runs their whole shortlist
#: three times a minute against the same store never meets it, and a caller trying to make
#: this platform hammer one storefront meets it on their thirteenth page. The bound that
#: matters is the ANONYMOUS one, which every caller with no session shares — so the ceiling on
#: what the internet at large can point at one merchant through this platform is twelve
#: fetches a minute, whatever the request rate.
MAX_FETCHES_PER_ORIGIN = 12
ORIGIN_WINDOW_SECONDS = 60.0

#: How many recently-fetched URLs are remembered for :data:`DEDUP_WINDOW_SECONDS`.
#:
#: Bounded by making room, not by refusing — a full table must never become "fetch everything
#: again", and it must never become "fetch nothing" either. Evicting the oldest URL does let a
#: caller push a page out of memory and re-fetch it, and what keeps that out of reach is the
#: RATIO rather than the eviction rule, exactly as it is for
#: ``buyer_svc.auth.routes.MagicLinkRateLimiter``: the per-origin budget admits at most
#: ``MAX_FETCHES_PER_ORIGIN`` per host per minute per caller, so one caller across the
#: nineteen registered merchant domains can buy 19 x 12 x 5 = 1,140 entries inside one dedup
#: window, well under this ceiling. A deployment with many more registered domains, or many
#: authenticated callers, can reach it; the failure there is a duplicate fetch, never a
#: refused shopper.
MAX_REMEMBERED_URLS = 4096

#: How many ``(caller, host)`` budgets are tracked at once. Same eviction posture, and the
#: same reason: a full table that refused would be a service-wide denial of the check that an
#: unauthenticated caller could arm for free, which is strictly worse than the flood it would
#: be refusing.
MAX_TRACKED_BUDGETS = 4096


def _host_of(url: str) -> str:
    """The merchant this URL addresses, lower-cased, or ``""`` when it addresses nobody.

    Never raises: ``urlsplit("https://[")`` does, and the value reaching here came off the
    wire. A URL with no readable host is keyed under ``""`` and therefore budgeted with the
    other unreadable ones rather than escaping the budget entirely.
    """
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class FetchBudget:
    """What one caller may cost one merchant. Two bounds, and they close different attacks.

    The live-page check gives anyone who can reach ``POST /buyer/shortlist/render`` a way to
    make this platform issue outbound HTTP to a registered merchant.
    :func:`buyer_svc.livecheck.targets.usable_page_url` already decides WHERE that request may
    go — https, port 443, no userinfo, the registered domain or a proper subdomain of it — and
    that is containment, not a budget: it says nothing about how much, how often, or on whose
    behalf. This class is the other half.

    **Dedup**, keyed on the exact URL. A captured request body replayed in a loop is the
    cheapest amplifier available, and it is also the one shape that buys the platform nothing:
    the second reading of a page inside :data:`DEDUP_WINDOW_SECONDS` answers a question the
    first one already answered. Refused duplicates are NOT charged to the origin budget below,
    because a request that causes no fetch must not be able to spend a real shopper's
    allowance.

    **A per-``(caller, host)`` budget**, for the caller who defeats dedup by varying the URL —
    a query string is free to change and a storefront serves the same page under any number of
    them. It counts ADMISSIONS rather than attempts, in the sense
    ``buyer_svc.auth.routes.MagicLinkRateLimiter`` uses: an admitted target is one this
    platform has undertaken to fetch, and whether ``POST /buyer/livecheck/run`` is ever driven
    is not this class's business. Over-counting a queue nobody drains is the safe direction.

    Why the caller is the key, and why anonymous is ONE key
    ------------------------------------------------------
    R5 gives a buyer a rotating pseudonym and nothing else, so the pseudonym is the only
    stable, service-issued name a caller can have here — and it is not free: it costs a
    mailbox and a redeemed magic link. Every caller with no live session therefore shares a
    single bucket, which is what makes the anonymous ceiling a ceiling on the INTERNET rather
    than a ceiling per attacker: an unauthenticated flood cannot buy more allowance by
    arriving from more addresses, because there is no address in the key.

    What that costs, said plainly: an anonymous flood can spend the anonymous allowance for a
    merchant and a genuine signed-out shopper's slot then goes unchecked for the rest of the
    minute. That is a denial of an optional EVIDENCE feature, and the alternative — a
    per-IP-per-caller key — is an allowance an attacker mints for free. The shortlist itself
    is never refused by anything in this class.

    Process-local, like every other budget in this service. Behind several replicas each keeps
    its own, and the effective ceiling is ``replicas x`` this one. ``ProcessLocalStateUnsafe``
    already refuses a multi-worker buyer process for the session store's sake, which is what
    keeps that honest here too.
    """

    def __init__(
        self,
        *,
        dedup_window: float = DEDUP_WINDOW_SECONDS,
        per_origin: int = MAX_FETCHES_PER_ORIGIN,
        origin_window: float = ORIGIN_WINDOW_SECONDS,
        max_urls: int = MAX_REMEMBERED_URLS,
        max_budgets: int = MAX_TRACKED_BUDGETS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._dedup_window = max(0.0, float(dedup_window))
        self._per_origin = max(1, int(per_origin))
        self._origin_window = max(0.0, float(origin_window))
        self._max_urls = max(1, int(max_urls))
        self._max_budgets = max(1, int(max_budgets))
        #: Monotonic, never wall-clock: a budget measured against a clock an operator can move
        #: backwards is a budget an NTP correction refills.
        self._clock = clock if clock is not None else time.monotonic
        self._recent: OrderedDict[str, float] = OrderedDict()
        self._hits: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def admit(self, url: Any, caller: Any = "") -> tuple[str, str]:
        """``("", "")`` when this fetch may be made, else ``(why not, which bound)``.

        The second element is :data:`REFUSED_DUPLICATE` or :data:`REFUSED_OVER_BUDGET` — the
        bound that refused, as a token rather than as a sentence to be pattern-matched. They
        are different findings and a caller that counts them must not have to read English to
        tell them apart: duplicates mean this platform is being replayed at, a spent budget
        means somebody is concentrating its traffic on one merchant.

        The sentence, like every other refusal in this package, is returned rather than raised
        and is written to be shown to a shopper beside the slot it explains.
        """
        address = text(url)
        if not address:
            return "", ""
        who = text(caller) or ANONYMOUS_CALLER
        now = self._clock()
        with self._lock:
            # The dedup check runs BEFORE the URL is parsed for its host, because the shape it
            # catches — the same shortlist rendered again — is the commonest thing that
            # reaches here, on a path whose whole budget is one append. `urlsplit` on a page
            # this platform has already decided not to read is work bought for nothing.
            seen = self._recent.get(address)
            if seen is not None and now - seen < self._dedup_window:
                return (
                    "this platform read this exact page "
                    f"{max(0, int(now - seen))}s ago and does not read it again inside "
                    f"{int(self._dedup_window)}s",
                    REFUSED_DUPLICATE,
                )
            key = (who, _host_of(address))
            hits = [when for when in self._hits.get(key, ()) if now - when < self._origin_window]
            if len(hits) >= self._per_origin:
                self._hits[key] = hits
                self._hits.move_to_end(key)
                return (
                    f"this platform has already read {len(hits)} pages at "
                    f"{key[1] or 'this host'} for this caller inside "
                    f"{int(self._origin_window)}s, which is its budget",
                    REFUSED_OVER_BUDGET,
                )
            hits.append(now)
            self._hits[key] = hits
            self._hits.move_to_end(key)
            self._recent[address] = now
            self._recent.move_to_end(address)
            self._evict(now)
        return "", ""

    def _evict(self, now: float) -> None:
        """Keep both tables bounded. Caller holds ``_lock``.

        Collects what has expired from the FRONT and stops at the first survivor, so a request
        costs what it collects rather than what the table holds — the shape
        ``MagicLinkRateLimiter._forget_stale`` arrived at after its O(tracked) sweep was
        measured as the amplifier for the flood that filled it. Both tables are kept in
        ascending order of their newest write, which is the order they expire in.
        """
        while self._recent:
            address = next(iter(self._recent))
            if now - self._recent[address] < self._dedup_window:
                break
            del self._recent[address]
        while len(self._recent) > self._max_urls:
            self._recent.popitem(last=False)

        while self._hits:
            key = next(iter(self._hits))
            when = self._hits[key]
            if when and now - when[-1] < self._origin_window:
                break
            del self._hits[key]
        while len(self._hits) > self._max_budgets:
            self._hits.popitem(last=False)


# ==============================================================================================
# The queue
# ==============================================================================================


class LiveCheckQueue:
    """Targets waiting to be checked. Bounded, thread-safe, and holding no live resources.

    ``deque(maxlen=...)`` rather than a check-and-refuse, so appending is O(1) and cannot fail
    on the shopper's path. The eviction it implies is the right one: a queue that filled up
    because nothing is draining it should keep the targets a shopper just generated, not the
    ones from an hour ago.
    """

    def __init__(self, maxlen: int = MAX_QUEUED_TARGETS, budget: FetchBudget | None = None) -> None:
        self._items: deque[LiveCheckTarget] = deque(maxlen=max(1, int(maxlen)))
        self._lock = threading.Lock()
        #: How many targets have been evicted unchecked. Reported by the route, because a
        #: queue that is silently dropping work looks exactly like a queue with no work.
        self.dropped = 0
        #: What one caller may cost one merchant. On the QUEUE rather than in
        #: :func:`queue_live_checks`, so that nothing can enqueue a target without passing it:
        #: a bound reachable only through one function is a bound the next caller added to
        #: this module does not have.
        self.budget = budget if budget is not None else FetchBudget()
        #: How many targets this queue refused, by reason. Counted separately because they are
        #: different findings: duplicates mean the platform is being replayed at, and spent
        #: budgets mean somebody is trying to concentrate its traffic on one merchant.
        self.refused_duplicate = 0
        self.refused_over_budget = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def offer(
        self, targets: Iterable[LiveCheckTarget], *, caller: str = ""
    ) -> tuple[int, list[tuple[LiveCheckTarget, str]]]:
        """Append what the budget admits. ``(accepted, [(target, why it was refused), ...])``.

        Never raises and never blocks on I/O: this runs on the shopper's path, where the whole
        cost of this feature is meant to be an append.

        ``caller`` is the buyer pseudonym behind the render's ``X-Buyer-Session`` header, or
        ``""`` for a render that carried none — which every shipped surface does today, so
        ``""`` is the ordinary case and not an error. See :class:`FetchBudget` for why every
        such caller shares one allowance.
        """
        accepted = 0
        refused: list[tuple[LiveCheckTarget, str]] = []
        for target in targets:
            if not isinstance(target, LiveCheckTarget):
                continue
            # OUTSIDE the queue's own lock, deliberately: `FetchBudget` takes its own, and
            # holding both would order two locks on the shopper's path for no gain.
            why, bound = self.budget.admit(target.url, caller)
            if why:
                refused.append((target, why))
                if bound == REFUSED_DUPLICATE:
                    self.refused_duplicate += 1
                else:
                    self.refused_over_budget += 1
                continue
            with self._lock:
                if len(self._items) == self._items.maxlen:
                    self.dropped += 1
                self._items.append(target)
                accepted += 1
        return accepted, refused

    def drain(self, limit: int | None = None) -> list[LiveCheckTarget]:
        """Take up to ``limit`` targets off the front. Oldest first."""
        taken: list[LiveCheckTarget] = []
        with self._lock:
            ceiling = len(self._items) if limit is None else min(int(limit), len(self._items))
            for _ in range(max(0, ceiling)):
                taken.append(self._items.popleft())
        return taken


# ==============================================================================================
# The record
# ==============================================================================================


@dataclass(frozen=True, slots=True)
class LiveCheckRecord:
    """One completed check: what was compared, both readings, and what it cost.

    ``check`` is :func:`claim_verification.live_page.check_pitch_against_page`'s whole answer,
    kept rather than reduced to a verdict, because a disagreement nobody can re-examine is an
    accusation. It carries the store's asking price AND the page's price, the surface each was
    read from, the tolerance applied and the sentence saying what was compared.
    """

    target: LiveCheckTarget
    check: Any
    checked_at: str
    events: tuple[Mapping[str, Any], ...] = ()
    fetch_reason: str = ""

    @property
    def outcome(self) -> str:
        return str(getattr(self.check, "outcome", NO_VERDICT))

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "target": self.target.to_dict(),
            "outcome": self.outcome,
            "surface": LIVE_PAGE_SURFACE,
            "fetch_reason": self.fetch_reason,
            "check": self.check.to_dict(),
            "ledger_events": [dict(event) for event in self.events],
        }


class LiveCheckLedger:
    """A process-local ring of completed checks, so the route has something to serve.

    Explicitly NOT the durable record. The durable record is the ``claim_verified`` event on
    the trust ledger; this is the readback ``buyer_svc.feedback.submission.FeedbackLedger``
    gives that path, with the same honesty about what it is: one process, bounded, and gone
    when the process is.
    """

    def __init__(self, maxlen: int = MAX_RECORDED_CHECKS) -> None:
        self._items: deque[LiveCheckRecord] = deque(maxlen=max(1, int(maxlen)))
        self._refusals: deque[TargetRefused] = deque(maxlen=max(1, int(maxlen)))
        self._lock = threading.Lock()

    def record(self, entry: LiveCheckRecord) -> None:
        with self._lock:
            self._items.append(entry)

    def refuse(self, entries: Iterable[TargetRefused]) -> None:
        with self._lock:
            for entry in entries:
                self._refusals.append(entry)

    def records(self, auction_id: str = "") -> list[LiveCheckRecord]:
        with self._lock:
            rows = list(self._items)
        if not auction_id:
            return rows
        return [row for row in rows if row.target.auction_id == auction_id]

    def refusals(self, auction_id: str = "") -> list[TargetRefused]:
        with self._lock:
            rows = list(self._refusals)
        if not auction_id:
            return rows
        return [row for row in rows if row.auction_id == auction_id]


# ==============================================================================================
# The module-level seams
# ==============================================================================================
# `POST /buyer/shortlist/render` takes no `Request`, and that is R2's structural guarantee
# rather than an oversight: `buyer_svc.accept.routes` says the handler *cannot reach* an
# exchange client, so looking at a shortlist cannot become accepting one. A queue on
# `app.state` would have to be reached through a `Request`, which would put `app.state` — and
# therefore the exchange client — back in that handler's scope. So the queue is a module-level
# seam, exactly as `buyer_svc.pitch.writer.pitch_writer` is and for the same reason, which
# that module states at length.

_QUEUE_LOCK = threading.Lock()
_QUEUE: LiveCheckQueue | None = None
_LEDGER: LiveCheckLedger | None = None


def live_check_queue() -> LiveCheckQueue:
    """The process's queue, built on first use."""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = LiveCheckQueue()
        return _QUEUE


def set_live_check_queue(queue: LiveCheckQueue | None) -> None:
    """Install a queue (or ``None`` to reset). For composition roots and tests."""
    global _QUEUE
    with _QUEUE_LOCK:
        _QUEUE = queue


def live_check_ledger() -> LiveCheckLedger:
    """The process's record of completed checks, built on first use."""
    global _LEDGER
    with _QUEUE_LOCK:
        if _LEDGER is None:
            _LEDGER = LiveCheckLedger()
        return _LEDGER


def set_live_check_ledger(ledger: LiveCheckLedger | None) -> None:
    global _LEDGER
    with _QUEUE_LOCK:
        _LEDGER = ledger


# ==============================================================================================
# The shopper's path: enqueue only
# ==============================================================================================


def _pitch_text(row: Any) -> str:
    """The store's own message, bounded. Carried as TEXT and decomposed later.

    Decomposition is a regular-expression scan over prose a seller chose the length of, and it
    is the one thing in this whole feature that would have cost the render path measurable
    time. It happens at drain, where it is also *better*: the key aliases resolve against the
    live page's own vocabulary (:func:`claim_verification.live_page.page_vocabulary`), which is
    the same trick ``exchange.ranking.verification`` plays with ``catalog_keys``, and that
    vocabulary does not exist until the page has been read.
    """
    message = text(read(row, "message", ""))
    return message[:MAX_PITCH_TEXT_CHARS] if len(message) <= MAX_PITCH_TEXT_CHARS else ""


def queue_live_checks(
    rows: Any,
    *,
    auction_id: str = "",
    registered_domains: Any = None,
    product_pages: Any = None,
    queue: LiveCheckQueue | None = None,
    ledger: LiveCheckLedger | None = None,
    caller: str = "",
) -> tuple[int, list[TargetRefused]]:
    """Resolve one shortlist's slots to targets and enqueue them. **The whole shopper cost.**

    Returns ``(accepted, refusals)``. Refusals are recorded on the ledger so the route can say
    "no page was checked for this slot, and here is why" — never nothing, because a screen that
    showed a checked slot and an unchecked one identically would be claiming evidence it does
    not have.

    Only slots carrying a store message are queued. That is D55 applied literally: the
    sponsored side is the side with the motive and therefore the side checked adversarially,
    and a scraped shop's pitch was written by the platform out of its own snapshot — checking
    the platform's own prose against the store's page would be grading the wrong party.

    ``caller`` is who asked, for :class:`FetchBudget`: the buyer pseudonym behind the render's
    ``X-Buyer-Session`` header, or ``""`` for a render that carried none. Every shipped buyer
    surface renders a shortlist without a session — ``apps/buyer/app/journey/Journey.tsx``
    says so in as many words — so ``""`` is the ordinary case, and it is the case the tightest
    allowance applies to. A target the budget refuses is recorded on the ledger as a
    :class:`~buyer_svc.livecheck.targets.TargetRefused` beside the refusals
    :func:`~buyer_svc.livecheck.targets.targets_for` made, because "we did not read this page,
    and here is why" is the same answer whether the reason was a hostile URL or a spent
    budget, and ``GET /buyer/livecheck/{auction_id}`` already serves it.
    """
    target_queue = queue if queue is not None else live_check_queue()
    record_ledger = ledger if ledger is not None else live_check_ledger()

    entries: list[Any] = []
    if rows is not None and not isinstance(rows, (str, bytes)):
        try:
            entries = list(rows)
        except TypeError:
            entries = []

    sponsored: list[Any] = []
    texts: list[str] = []
    for row in entries:
        message = _pitch_text(row)
        if not message:
            continue
        sponsored.append(row)
        texts.append(message)

    targets, refusals = targets_for(
        sponsored,
        auction_id=auction_id,
        registered_domains=(registered_domains if registered_domains is not None else _domains()),
        product_pages=product_pages if product_pages is not None else _pages(),
    )
    by_slot = {(target.slot, target.bid_ref): index for index, target in enumerate(targets)}
    carried: list[LiveCheckTarget] = []
    for index, row in enumerate(sponsored):
        key = (text(read(row, "slot", "")), text(read(row, "bid_ref", "")))
        position = by_slot.get(key)
        if position is None:
            continue
        target = targets[position]
        commitments = plain(read(row, "commitments", None))
        carried.append(
            LiveCheckTarget(
                store_id=target.store_id,
                product_ref=target.product_ref,
                url=target.url,
                auction_id=target.auction_id,
                bid_ref=target.bid_ref,
                slot=target.slot,
                asking_price=target.asking_price,
                currency=target.currency,
                claims=tuple(row for row in (commitments or ()) if isinstance(row, Mapping)),
                pitch_text=texts[index],
                offered=target.offered,
            )
        )

    accepted, over_budget = target_queue.offer(carried, caller=caller)
    for target, why in over_budget:
        refusals.append(
            TargetRefused(
                store_id=target.store_id,
                product_ref=target.product_ref,
                reason=why,
                slot=target.slot,
                auction_id=target.auction_id,
                bid_ref=target.bid_ref,
            )
        )
    if refusals:
        record_ledger.refuse(refusals)
    return accepted, refusals


# ==============================================================================================
# The deferred path: fetch, compare, record, publish
# ==============================================================================================


def _sink_call(sink: Any) -> Any:
    """The one callable that records an event on this sink, or ``None``."""
    if sink is None:
        return None
    for name in LEDGER_SINK_METHODS:
        candidate = getattr(sink, name, None)
        if callable(candidate):
            return candidate
    return sink if callable(sink) else None


def _claim_verified_event(
    target: LiveCheckTarget, reading: Any, *, moment: datetime
) -> dict[str, Any]:
    """One contradiction as the published ``claim_verified`` body, and nothing more.

    ``contracts.LEDGER_PAYLOAD_SHAPES["claim_verified"]`` is ``("claim_ref", "status", "dim")``
    and all three are present. The extra keys beside them — the two readings, the surface, what
    was compared — are the evidence, and the payload is an OPEN mapping precisely so a kind can
    carry its own body; what is NOT open is the published three, which is why they are written
    out rather than assembled from whatever happened to be in scope.

    ``surface`` is the key that keeps this honest downstream. A verdict read off the seller's
    own product page is weaker evidence than one read off the exchange's snapshot and much
    weaker than a transaction record, and a row that did not name its surface would let a
    reader treat all three as the same fact.
    """
    claim_ref = getattr(reading, "claim_ref", "") or (
        f"{pitch_ref_for(target.store_id, target.auction_id)}#{getattr(reading, 'key', '')}"
    )
    return {
        "event_id": f"lpc-{uuid.uuid4().hex}",
        "ts": moment.isoformat().replace("+00:00", "Z"),
        "kind": CLAIM_VERIFIED_KIND,
        "auction_id": target.auction_id or None,
        "store_id": target.store_id or None,
        "payload": {
            "claim_ref": str(claim_ref),
            "status": CONTRADICTED,
            "dim": LIVE_CHECK_DIMENSION,
            "surface": LIVE_PAGE_SURFACE,
            "key": str(getattr(reading, "key", "")),
            "pitched_value": getattr(reading, "pitched_value", None),
            "observed_value": getattr(reading, "observed_value", None),
            "page_surface": str(getattr(reading, "surface", "")),
            "compared": str(getattr(reading, "compared", "")),
            "reason": str(getattr(reading, "reason", "")),
            "product_ref": target.product_ref,
            "page_url": target.url,
        },
    }


def run_live_checks(
    *,
    queue: LiveCheckQueue | None = None,
    fetcher: Any = None,
    ledger: LiveCheckLedger | None = None,
    sink: Any = None,
    limit: int | None = None,
    now: Any = None,
) -> list[LiveCheckRecord]:
    """Drain the queue: fetch each page, compare it, record the verdict, publish the cost.

    Runs off the shopper's path — see this module's docstring. Never raises for an ordinary
    outcome: a refused fetch, a 404, a page with no structured data and a page that disagrees
    are all *records*, and only the last of them emits anything.

    Args:
        queue: what to drain; the process seam by default.
        fetcher: a :class:`~buyer_svc.livecheck.fetching.PageFetcher`. The fail-closed
            :class:`~buyer_svc.livecheck.fetching.NoPageFetcher` by default, which reads no
            page and therefore decides nothing.
        ledger: where completed checks are kept for read-back.
        sink: the trust-ledger sink. ``None`` records the verdict locally and publishes
            nothing — a deployment with no ledger wired must still not be able to lose the
            shopper's request, and ``buyer_svc.composition`` already documents that posture.
        limit: how many targets to take this pass.
        now: the clock, injectable so a test can assert a timestamp.

    Returns:
        One :class:`LiveCheckRecord` per target drained, in the order they were drained.
    """
    target_queue = queue if queue is not None else live_check_queue()
    record_ledger = ledger if ledger is not None else live_check_ledger()
    page_fetcher = fetcher if fetcher is not None else NoPageFetcher()
    emit = _sink_call(sink)
    clock = now if callable(now) else (lambda: datetime.now(UTC))

    records: list[LiveCheckRecord] = []
    for target in target_queue.drain(limit):
        moment = clock()
        page = page_fetcher.fetch(target.url)
        if page.ok:
            reading = read_product_page(page.body, url=target.url, encoding=page.encoding)
        else:
            reading = unreadable_page(
                target.url,
                page.reason or f"the page could not be read (status {page.status})",
            )
        prose = target.pitch_text
        claims = list(target.claims)
        if prose:
            claims.extend(
                decompose_pitch(
                    prose,
                    store_id=target.store_id,
                    auction_id=target.auction_id,
                    vocabulary=page_vocabulary(reading),
                )
            )
        check = check_pitch_against_page(
            reading,
            claims=claims,
            asking_price=target.asking_price,
            currency=target.currency,
            store_id=target.store_id,
            product_ref=target.product_ref,
            offered=target.offered,
        )

        events: list[Mapping[str, Any]] = []
        if check.outcome == CONTRADICTED:
            for row in check.contradictions:
                event = _claim_verified_event(target, row, moment=moment)
                events.append(event)
                if emit is None:
                    continue
                try:
                    emit(event)
                except Exception as exc:  # noqa: BLE001 - the audit sink never fails the drain
                    _log.error(
                        "live-page check: the ledger sink raised while recording a "
                        "contradiction for store %s on %s (%s); the verdict stands in this "
                        "process's record and did not reach the chained ledger",
                        target.store_id,
                        target.url,
                        exc,
                    )
            _log.warning(
                "live-page check: %s's own product page contradicts its pitch on %s (%s)",
                target.store_id,
                ", ".join(sorted({row.key for row in check.contradictions})),
                target.url,
            )
        elif check.outcome == AGREES:
            _log.info(
                "live-page check: %s's own product page agrees with its pitch (%s)",
                target.store_id,
                target.url,
            )

        entry = LiveCheckRecord(
            target=target,
            check=check,
            checked_at=moment.isoformat().replace("+00:00", "Z"),
            events=tuple(events),
            fetch_reason=page.reason,
        )
        record_ledger.record(entry)
        records.append(entry)
    return records


def _reset_for_tests() -> None:
    """Drop every process-level seam. Called by this package's test fixtures only."""
    set_live_check_queue(None)
    set_live_check_ledger(None)
