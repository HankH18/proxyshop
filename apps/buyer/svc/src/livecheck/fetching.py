"""How a live product page is fetched — a PORT, and three implementations of it.

**There is no second fetcher in this file.** ``services/ingest`` already owns the machinery
for reaching a real storefront safely: an SSRF netguard that resolves-then-pins and refuses
private, link-local and obfuscated hosts; a transport that walks redirects by hand and
re-checks every hop; robots.txt obedience under an identified product token; and per-crawl
budgets metered while the body is read. :class:`GuardedPageFetcher` is a **caller** of that —
about forty lines of policy — and writing a fetch loop here instead would have produced an
SSRF hole pointed at whatever URL a seller puts on a bid, which is the single most likely way
this feature could have gone wrong.

Why this module names ``ingest`` nowhere, not even lazily — MEASURED, and the measurement
changed the design
-------------------------------------------------------------------------------------------
The first version of this file imported ``ingest.adapters`` inside a function, on the
reasoning that a lazy import degrades gracefully where the package is absent.
``proxyshop_support/tests/test_artifact_copyset.py`` rejected it, and it was right to::

    apps/buyer/Dockerfile: the image cannot resolve first-party packages it ships or claims.
      'ingest': no COPY covers services/ingest/src/ — 0 module-scope site(s) and
                4 indented site(s) (lazy, possibly swallowed)

That gate counts a lazy import as a failure ON PURPOSE: T-300 is the record of a lazy
``from llm import ...`` wrapped in ``except ImportError`` that ran the buyer's clarification
loop degraded for the life of an image, with no crash, no failing healthcheck and one WARNING
line in a service that installed no logging handler.

And the degrade would not even have worked. ``services/ingest/src/adapters/__init__.py``
imports ``catalog_mcp`` and ``signed_fetch`` at module scope, and ``signed_fetch`` asks for
BeautifulSoup — so ``from ingest.adapters.netguard import ...`` executes that ``__init__``
and needs ``beautifulsoup4`` and ``protego``, neither of which is in this image's pip layer.
Copying five files would not fix it either, for the same reason. **The buyer service cannot
import the crawl core today in any form**, and pretending otherwise with a ``try`` was the
shape of a lie.

So the real transport is INJECTED. :class:`GuardedPageFetcher` is pure policy over
collaborators handed to it, and :func:`set_page_fetcher_factory` is the seam a process that
DOES ship the crawl core installs itself into — the same module-level factory shape
``buyer_svc.pitch.writer.set_pitch_writer`` already uses. With no factory installed,
``live_page_fetcher: "guarded"`` binds :class:`NoPageFetcher` and says so loudly at wiring
time, which is a stated gap rather than a silent one.

**The fix is a lift, and it is reported rather than done** because ``services/ingest`` is
another lane's file scope: see this package's ``__init__`` for exactly which five modules
should move into a workspace member both consumers can ship, and what that costs.

The recorded fetcher is not only for tests
------------------------------------------
:class:`RecordedPageFetcher` replays a corpus of pages captured earlier. The suite runs
offline (D3/C9) and a test that opens a socket is a defect even when it passes, so every test
here drives this class — but it is also a legitimate deployment: a platform that has already
crawled a store has the page bytes, and replaying them costs the store nothing and reaches the
same verdict. It is the same shape ``fixtures/real-catalogs`` and the LLM doubles already use.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_PAGE_BUDGET_SECONDS",
    "MAX_PAGE_RESPONSE_BYTES",
    "USER_AGENT",
    "FetchedPage",
    "GuardedPageFetcher",
    "NoPageFetcher",
    "PageFetcher",
    "RecordedPageFetcher",
    "guarded_page_fetcher",
    "page_fetcher_factory",
    "set_page_fetcher_factory",
]

_log = logging.getLogger(__name__)

#: The identified crawler token this check fetches under, when ``ingest.adapters.robots`` is
#: not importable to supply its own. C6: a product name, a version and a contact, and none of
#: the tokens a browser sends — impersonating a browser to slip past bot detection is the
#: behaviour C6 forbids, and these are real stores.
USER_AGENT = "ProxyShopBot/1.0 (+https://proxyshop.example/bot; ingest@proxyshop.example)"

#: Wall-clock ceiling for ONE page check, robots.txt fetch included. Nothing waits on this —
#: the shopper's shortlist has already been served — but an unbounded fetch would hold a
#: worker indefinitely against a server that never finishes a response.
DEFAULT_PAGE_BUDGET_SECONDS = 20.0

#: The most bytes one product page may occupy on the wire. A product page is tens of
#: kilobytes; the comparison refuses anything over
#: ``claim_verification.live_page.MAX_PAGE_BYTES`` anyway, and this stops the transfer rather
#: than parsing what arrived.
MAX_PAGE_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """One attempt at reading a live product page. Never an exception, always an answer.

    ``ok`` is ``True`` only for a 200 with a body. Everything else — a 404, a 500, a robots
    refusal, an SSRF refusal, a timeout, a blown budget — arrives here with ``ok`` false and a
    ``reason``, and every one of them becomes NO VERDICT downstream. A store with a slow
    server has not lied about anything.
    """

    url: str
    status: int = 0
    body: bytes | None = None
    encoding: str = "utf-8"
    final_url: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.body)


@runtime_checkable
class PageFetcher(Protocol):
    """Anything that can answer "what does this URL serve?" without raising."""

    def fetch(self, url: str) -> FetchedPage: ...


class NoPageFetcher:
    """The default: fetches nothing, decides nothing, and says so.

    Fail-closed the way ``NoRegisteredDomains`` and ``NoCatalogSnapshots`` are: a deployment
    nobody wired a fetcher into checks no pages, rather than quietly falling back to something
    that opens sockets. The reason it returns is written to be readable in a served record by
    an operator who has not read this file.
    """

    reason = (
        "no live-page fetcher is wired into this buyer service, so no product page was read "
        "and nothing was decided"
    )

    def fetch(self, url: str) -> FetchedPage:
        return FetchedPage(url=str(url), reason=self.reason)


class RecordedPageFetcher:
    """Replay pages captured earlier. No sockets, ever — offline by construction.

    A corpus is ``{url: {"status": int, "body": str|bytes, "encoding": str, "reason": str}}``,
    or a directory holding ``manifest.json`` of that shape with ``body_file`` naming a sibling
    file. A URL the corpus does not hold comes back as a miss with ``ok`` false, which is the
    same no-verdict a 404 produces — a corpus is allowed to be incomplete and an incomplete
    corpus must not be able to manufacture evidence.

    ``requested`` records every URL asked for, in order, so a test can assert what the render
    path did NOT fetch. That is how "the shortlist does not block on a live fetch" is proved
    structurally rather than by timing something on a loaded machine.
    """

    def __init__(self, corpus: Mapping[str, Any] | str | Path | None = None) -> None:
        self.requested: list[str] = []
        self._pages: dict[str, dict[str, Any]] = {}
        self._root: Path | None = None
        if isinstance(corpus, (str, Path)):
            self._load_directory(Path(corpus))
        elif isinstance(corpus, Mapping):
            self._pages = {str(url): dict(row) for url, row in corpus.items()}

    def _load_directory(self, root: Path) -> None:
        manifest = root / "manifest.json"
        if not manifest.is_file():
            return
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        pages = document.get("pages") if isinstance(document, Mapping) else None
        if not isinstance(pages, Mapping):
            return
        self._root = root
        self._pages = {
            str(url): dict(row) for url, row in pages.items() if isinstance(row, Mapping)
        }

    def _body(self, row: Mapping[str, Any]) -> bytes | None:
        raw = row.get("body")
        if isinstance(raw, str):
            return raw.encode(str(row.get("encoding") or "utf-8"), errors="replace")
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
        name = row.get("body_file")
        if isinstance(name, str) and self._root is not None:
            # `name` comes from a manifest this repository ships, not from a request; it is
            # still resolved and confined to the corpus root so a corpus edited by hand
            # cannot read outside its own directory.
            candidate = (self._root / name).resolve()
            if self._root.resolve() in candidate.parents and candidate.is_file():
                return candidate.read_bytes()
        return None

    def fetch(self, url: str) -> FetchedPage:
        target = str(url)
        self.requested.append(target)
        row = self._pages.get(target)
        if row is None:
            return FetchedPage(
                url=target,
                reason="this url is not in the recorded corpus, so nothing was read",
            )
        status = int(row.get("status") or 0)
        return FetchedPage(
            url=target,
            status=status,
            body=self._body(row),
            encoding=str(row.get("encoding") or "utf-8"),
            final_url=str(row.get("final_url") or target),
            reason=str(
                row.get("reason") or ("" if status == 200 else f"the page answered {status}")
            ),
        )


@dataclass
class GuardedPageFetcher:
    """A real fetch, through ``ingest``'s guard, budget and robots machinery. Nothing else.

    Built by whatever a deployment installed through :func:`set_page_fetcher_factory`. Every
    collaborator is INJECTED — the guard, the robots parser, the budget ledger, the clock —
    so this class names ``ingest`` nowhere and carries no dependency, lazy or otherwise, on a
    package this image does not ship. See the module docstring for the measurement that made
    that a rule.

    What it does per URL, in order, and it stops at the first refusal:

    1. robots.txt for that origin, fetched once per host and remembered for this fetcher's
       lifetime. Unreachable (5xx / transport) means disallow-all, per RFC 9309 §2.3.1.4 —
       silence is not consent. 404 means "no rules" and the crawl continues.
    2. :func:`may_fetch` for this exact URL under our product token.
    3. the page, through ``SafeHTTPClient`` with the origin as the only allowed host, so a
       redirect off the registered domain is refused rather than followed.

    A declared ``Crawl-delay`` is honoured by *refusing* rather than by sleeping. A sleeping
    fetcher holds a worker for a duration the store chooses, which is a denial of service a
    store can aim at the platform; a refusal costs the store only a check it was never owed,
    and the target is checked on a later pass.
    """

    client: Any
    ledger_factory: Any
    robots_url: Any
    may_fetch: Any
    robots_verdict_for_status: Any
    crawl_delay: Any
    refusals: tuple[type[BaseException], ...]
    user_agent: str = USER_AGENT
    clock: Any = None
    _robots: dict[str, str] = field(default_factory=dict, init=False)
    _last_fetch: dict[str, float] = field(default_factory=dict, init=False)

    def _origin(self, url: str) -> str:
        text = str(url)
        head, _, rest = text.partition("://")
        return f"{head}://{rest.split('/', 1)[0]}" if rest else text

    def _robots_text(self, url: str) -> tuple[str, str]:
        """``(robots.txt, "")`` or ``("", why this origin may not be crawled)``."""
        origin = self._origin(url)
        if origin in self._robots:
            return self._robots[origin], ""
        ledger = self.ledger_factory()
        try:
            result = self.client.fetch(self.robots_url(origin), allowed_hosts=(), ledger=ledger)
            status: int | None = int(result.status)
            body = result.text()
        except self.refusals as exc:
            status, body = None, ""
            _log.info("live-page check: robots.txt for %s could not be read (%s)", origin, exc)
        posture = self.robots_verdict_for_status(status)
        if posture == "disallow-all":
            return "", (
                f"robots.txt for {origin} is unreachable or forbids this crawler, so the page "
                f"was not fetched"
            )
        text = body if posture == "use" else ""
        self._robots[origin] = text
        return text, ""

    def fetch(self, url: str) -> FetchedPage:
        target = str(url)
        try:
            robots_text, refusal = self._robots_text(target)
        except Exception as exc:  # noqa: BLE001 - a robots read must never raise on this path
            return FetchedPage(url=target, reason=f"robots.txt could not be read ({exc})")
        if refusal:
            return FetchedPage(url=target, reason=refusal)
        try:
            if robots_text and not self.may_fetch(robots_text, target, self.user_agent):
                return FetchedPage(
                    url=target,
                    reason="this store's robots.txt disallows this crawler for that page",
                )
            delay = self.crawl_delay(robots_text, self.user_agent) if robots_text else None
        except Exception as exc:  # noqa: BLE001
            return FetchedPage(url=target, reason=f"robots.txt could not be parsed ({exc})")

        origin = self._origin(target)
        if delay and self.clock is not None:
            now = float(self.clock())
            last = self._last_fetch.get(origin)
            if last is not None and now - last < float(delay):
                return FetchedPage(
                    url=target,
                    reason=(
                        f"this store declares a {float(delay):g}s Crawl-delay and it has not "
                        f"elapsed; the page is left for a later pass rather than waited on"
                    ),
                )
            self._last_fetch[origin] = now

        ledger = self.ledger_factory()
        try:
            result = self.client.fetch(target, allowed_hosts=(), ledger=ledger)
        except self.refusals as exc:
            return FetchedPage(url=target, reason=f"the page could not be fetched ({exc})")
        except Exception as exc:  # noqa: BLE001 - an unforeseen transport failure is a no-verdict
            return FetchedPage(
                url=target, reason=f"the page could not be fetched ({exc.__class__.__name__})"
            )
        charset = "utf-8"
        raw = str(result.headers.get("content-type", ""))
        if "charset=" in raw:
            charset = raw.split("charset=", 1)[1].split(";", 1)[0].strip().strip('"') or "utf-8"
        return FetchedPage(
            url=target,
            status=int(result.status),
            body=result.body,
            encoding=charset,
            final_url=str(result.final_url or target),
            reason="" if int(result.status) == 200 else f"the page answered {result.status}",
        )


_FACTORY_LOCK = threading.Lock()
_FACTORY: Any = None


def set_page_fetcher_factory(factory: Any) -> None:
    """Install the zero-argument callable that builds this process's real page fetcher.

    Called by a process that ships the guarded crawl core — the dev stack, an e2e harness, a
    worker image that also ships ``services/ingest`` — with something like::

        from ingest.adapters.budgets import CrawlBudget, CrawlLedger, BudgetExceeded
        from ingest.adapters.netguard import FetchRefused
        from ingest.adapters.robots import crawl_delay, may_fetch, robots_url, \
            robots_verdict_for_status
        from ingest.adapters.transport import SafeHTTPClient, TransportError

        budget = CrawlBudget(max_pages=4, max_depth=0, max_seconds=20.0, max_redirects=3,
                             max_response_bytes=MAX_PAGE_RESPONSE_BYTES)
        set_page_fetcher_factory(lambda: GuardedPageFetcher(
            client=SafeHTTPClient(user_agent=USER_AGENT),
            ledger_factory=lambda: CrawlLedger(budget),
            robots_url=robots_url, may_fetch=may_fetch,
            robots_verdict_for_status=robots_verdict_for_status, crawl_delay=crawl_delay,
            refusals=(FetchRefused, TransportError, BudgetExceeded),
            clock=time.monotonic,
        ))

    Every one of those names is in ``ingest``, and none of them is imported HERE — see the
    module docstring for the measurement that made that a rule rather than a preference.
    ``None`` clears it.
    """
    global _FACTORY
    with _FACTORY_LOCK:
        _FACTORY = factory


def page_fetcher_factory() -> Any:
    """The installed factory, or ``None``."""
    with _FACTORY_LOCK:
        return _FACTORY


def guarded_page_fetcher() -> Any:
    """The real fetcher this process can build, or ``None`` when nothing installed one.

    ``None`` is not an error and is the default in the shipped ``proxyshop-buyer-svc`` image:
    the caller binds :class:`NoPageFetcher`, every check records "no live-page fetcher is
    wired", and no page is read. A no-verdict costs a store nothing, which is the direction
    this whole feature fails in.
    """
    factory = page_fetcher_factory()
    if factory is None:
        _log.info(
            "live-page check: no page-fetcher factory is installed in this process, so no "
            "product page will be read. Install one with "
            "buyer_svc.livecheck.fetching.set_page_fetcher_factory from a process that ships "
            "the guarded crawl core, or configure live_page_fetcher='recorded:<dir>'."
        )
        return None
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001 - a bad factory must not fail composition
        _log.error("live-page check: the installed page-fetcher factory raised (%s)", exc)
        return None
