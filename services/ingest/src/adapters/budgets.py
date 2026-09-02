"""Crawl budgets: pages, depth, wall time, bytes (T-020).

A budget is only worth having if a **hostile** storefront cannot spend past it. Each of the
four dimensions therefore has a specific evasion it is written to defeat:

======  =============================  ====================================================
Budget  Evasion it must survive        How this module survives it
======  =============================  ====================================================
pages   a redirect loop, or a page     every request is charged *before* it is sent, and a
        that links to itself forever   redirect hop is a charged request like any other, so
                                       a loop burns the page budget instead of running free
depth   an infinitely deep link tree   depth rides with the frontier entry, and a link is
        (``/a/b/c/d/...``)             refused *enqueue* past ``max_depth`` rather than
                                       being fetched and discarded
time    a server that answers one      the deadline is checked between requests **and
        byte per second forever        between chunks of a response body**, and the socket
                                       timeout for each request is clamped to what is left
bytes   a gzip bomb; a chunked         wire bytes and *decompressed* bytes are metered
        response with no end; a        separately, both while streaming; ``Content-Length``
        lying ``Content-Length``       is used only as an early reject, never as the meter
======  =============================  ====================================================

The last row is the one that most implementations get wrong. Trusting ``Content-Length``
means a chunked response never gets measured at all, and metering only wire bytes means a
1 KiB gzip payload can still inflate into a gigabyte of RAM. So
:meth:`CrawlLedger.charge_bytes` meters what came off the socket and
:meth:`CrawlLedger.charge_decompressed` meters what came out of the inflater, and the
transport calls both on every chunk.

Time is taken from an injectable monotonic clock so a test can prove the deadline fires
without actually sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "BudgetExceeded",
    "BudgetUsage",
    "CrawlBudget",
    "CrawlLedger",
]


class BudgetExceeded(RuntimeError):
    """A crawl budget ran out.

    Attributes:
        resource: which budget — ``"pages"``, ``"depth"``, ``"time"``, ``"bytes"``,
            ``"response_bytes"``, ``"decompressed_bytes"`` or ``"redirects"``.
        limit: the configured ceiling.
        observed: the value that breached it.
    """

    def __init__(self, resource: str, limit: float, observed: float) -> None:
        super().__init__(f"crawl budget exhausted: {resource} limit={limit} observed={observed}")
        self.resource = resource
        self.limit = limit
        self.observed = observed


@dataclass(frozen=True)
class CrawlBudget:
    """Ceilings for one crawl of one storefront.

    Attributes:
        max_pages: total HTTP requests, redirect hops included.
        max_depth: link depth from the entry point; the entry point itself is depth 0.
        max_seconds: wall time for the whole crawl.
        max_bytes: wire bytes across the whole crawl.
        max_response_bytes: wire bytes for any single response.
        max_decompressed_bytes: bytes any single response may inflate to. Must exceed
            ``max_response_bytes`` to be useful, and is the only thing standing between the
            adapter and a gzip bomb.
        max_redirects: redirect hops tolerated on a single request.
        connect_timeout: per-request socket timeout ceiling, further clamped by the time
            remaining in the crawl.
    """

    max_pages: int = 200
    max_depth: int = 3
    max_seconds: float = 60.0
    max_bytes: int = 32 * 1024 * 1024
    max_response_bytes: int = 4 * 1024 * 1024
    max_decompressed_bytes: int = 16 * 1024 * 1024
    max_redirects: int = 5
    connect_timeout: float = 10.0

    def __post_init__(self) -> None:
        for name in (
            "max_pages",
            "max_depth",
            "max_seconds",
            "max_bytes",
            "max_response_bytes",
            "max_decompressed_bytes",
            "max_redirects",
            "connect_timeout",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"CrawlBudget.{name} must not be negative")


@dataclass(frozen=True)
class BudgetUsage:
    """What a finished (or abandoned) crawl actually spent."""

    pages: int = 0
    bytes_downloaded: int = 0
    bytes_decompressed: int = 0
    seconds: float = 0.0
    max_depth_reached: int = 0
    redirects: int = 0


class CrawlLedger:
    """Mutable spend against a :class:`CrawlBudget`.

    One ledger per crawl. Every method that can refuse raises :class:`BudgetExceeded`
    rather than returning a flag, so a caller cannot spend past a budget by forgetting to
    check a return value — the only silent path is the one that stayed inside the budget.
    """

    def __init__(
        self,
        budget: CrawlBudget | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = budget or CrawlBudget()
        self._monotonic = monotonic
        self.started_at = monotonic()
        self.pages = 0
        self.bytes_downloaded = 0
        self.bytes_decompressed = 0
        self.redirects = 0
        self.max_depth_reached = 0

    # -- time ---------------------------------------------------------------------------

    def elapsed(self) -> float:
        return max(0.0, self._monotonic() - self.started_at)

    def remaining_seconds(self) -> float:
        return max(0.0, self.budget.max_seconds - self.elapsed())

    def check_time(self) -> None:
        """Raise if the crawl deadline has passed. Called between response chunks."""
        elapsed = self.elapsed()
        if elapsed > self.budget.max_seconds:
            raise BudgetExceeded("time", self.budget.max_seconds, elapsed)

    def request_timeout(self) -> float:
        """Socket timeout for the next request: never more than the crawl has left."""
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise BudgetExceeded("time", self.budget.max_seconds, self.elapsed())
        return max(0.001, min(self.budget.connect_timeout, remaining))

    # -- pages, depth, redirects ---------------------------------------------------------

    def charge_page(self) -> None:
        """Account for one request about to be sent.

        Charged before the request goes out, so a response that never arrives still costs
        a page — otherwise a server that hangs up mid-response is free to be retried
        forever.
        """
        self.check_time()
        if self.pages + 1 > self.budget.max_pages:
            raise BudgetExceeded("pages", self.budget.max_pages, self.pages + 1)
        self.pages += 1

    def may_enqueue(self, depth: int) -> bool:
        """Whether a link at ``depth`` is inside the depth budget."""
        return depth <= self.budget.max_depth

    def charge_depth(self, depth: int) -> None:
        if not self.may_enqueue(depth):
            raise BudgetExceeded("depth", self.budget.max_depth, depth)
        self.max_depth_reached = max(self.max_depth_reached, depth)

    def charge_redirect(self, hops: int) -> None:
        if hops > self.budget.max_redirects:
            raise BudgetExceeded("redirects", self.budget.max_redirects, hops)
        self.redirects = max(self.redirects, hops)

    # -- bytes ---------------------------------------------------------------------------

    def charge_bytes(self, count: int, *, response_total: int | None = None) -> None:
        """Meter bytes that came off the socket.

        Args:
            count: bytes in the chunk just read.
            response_total: running wire total for the current response, if the caller is
                enforcing the per-response ceiling too.
        """
        self.bytes_downloaded += max(0, int(count))
        if self.bytes_downloaded > self.budget.max_bytes:
            raise BudgetExceeded("bytes", self.budget.max_bytes, self.bytes_downloaded)
        if response_total is not None and response_total > self.budget.max_response_bytes:
            raise BudgetExceeded("response_bytes", self.budget.max_response_bytes, response_total)

    def charge_decompressed(self, count: int, *, response_total: int | None = None) -> None:
        """Meter bytes that came *out of the inflater* — the gzip-bomb ceiling."""
        self.bytes_decompressed += max(0, int(count))
        if response_total is not None and response_total > self.budget.max_decompressed_bytes:
            raise BudgetExceeded(
                "decompressed_bytes", self.budget.max_decompressed_bytes, response_total
            )

    def snapshot(self) -> BudgetUsage:
        return BudgetUsage(
            pages=self.pages,
            bytes_downloaded=self.bytes_downloaded,
            bytes_decompressed=self.bytes_decompressed,
            seconds=self.elapsed(),
            max_depth_reached=self.max_depth_reached,
            redirects=self.redirects,
        )
