"""``buyer_svc.livecheck`` — the store's pitch, checked against the store's own live page.

The owner's idea, in one line: *whenever a store pitch is about a product, the buyer-side
agent also gets a link to that live product and does a quick check on the page.* This package
is that check, and the four things that make it safe are all structural rather than remembered:

    >>> from buyer_svc.livecheck import queue_live_checks, run_live_checks
    >>> queue_live_checks(shortlist["slots"], auction_id="auc-1",       # the shopper's path
    ...                   registered_domains=domains, product_pages=pages)
    (1, [])
    >>> run_live_checks(fetcher=fetcher, sink=ledger_sink)              # afterwards
    [LiveCheckRecord(... outcome='contradicted' ...)]

**1. It never blocks the shopper.** ``POST /buyer/shortlist/render`` enqueues and returns. The
fetch, the parse and the comparison happen on ``POST /buyer/livecheck/run``. See
:mod:`~buyer_svc.livecheck.deferred` for the measured argument and for why a verdict that
lands after the auction still costs the store.

**2. It reuses ``ingest``'s guarded fetch rather than writing a second one.** See
:mod:`~buyer_svc.livecheck.fetching` — and the lift below, which is the real fix.

**3. The URL is never the store's to choose.** See :mod:`~buyer_svc.livecheck.targets`.

**4. Absence is not guilt.** A page that 404s, times out, blocks the crawler or carries no
structured data yields no verdict and no penalty, and the record says which of those happened.
That rule lives in :mod:`claim_verification.live_page`, one package down, where it is a
property of a pure function rather than a discipline this package has to keep.

What this evidence IS, said here as well as there
--------------------------------------------------
A store's own product page is still the store's own word. This catches DRIFT (a stale
snapshot, a price that moved, an item that sold out) and CROSS-SURFACE CONTRADICTION (the
purchased message and the public page disagree). It does **not** catch a store lying
consistently everywhere; nothing built out of the seller's own surfaces can. Independent
evidence about a seller is the transaction record, which the promise ledger holds. Drift is
most of what actually goes wrong and this is cheap, which is the whole case for building it —
and it is the whole case, so no docstring here claims more.

THE LIFT THIS REPOSITORY SHOULD DO, reported rather than done because ``services/ingest`` is
another lane's file scope
-------------------------------------------------------------------------------------------
``services/ingest/src/adapters/`` holds a **shared crawl core** that now has a second
consumer. Four modules, and their dependency graph is already a clean cut:

===========================  =====  ================================================
module                       lines  what it imports from ingest
===========================  =====  ================================================
``netguard.py``               ~430  nothing (stdlib only)
``robots.py``                 ~140  ``.netguard`` (``safe_split``); ``protego``
``budgets.py``                ~220  nothing (stdlib only)
``transport.py``              ~380  ``.netguard``, ``.budgets``, ``.hashing``, ``.robots``
``hashing.py``                 ~90  nothing (stdlib only)
===========================  =====  ================================================

None of them imports anything else in ``ingest``, so moving all five into a workspace member
(``packages/netfetch``, namespace ``netfetch``, one more ``.pkgroot`` symlink) is a mechanical
move plus an import rewrite in ``ingest.adapters.signed_fetch`` and
``ingest.adapters.catalog_mcp``. The cost after that is one ``COPY`` line and one ``ln -s`` in
``apps/buyer/Dockerfile``, plus ``protego`` in its pip layer.

Two further facts that the copy-set gate MEASURED, and that the first draft of this package
got wrong:

* ``services/ingest/src/adapters/__init__.py`` imports ``catalog_mcp`` and ``signed_fetch`` at
  module scope, and ``signed_fetch`` asks for BeautifulSoup by name. So ``from
  ingest.adapters.netguard import ...`` needs ``beautifulsoup4`` and ``protego``, and copying
  five files without copying that ``__init__``'s dependencies would not work either. There is
  no arrangement of ``COPY`` lines that makes the crawl core importable in this image; the
  lift is the only fix.
* ``packages/verification`` was ALSO absent from this image's COPY set, so this package's own
  ``claim_verification`` import would have crash-looped the service at start-up —
  ``buyer_svc.main.create_app`` calls ``importlib.import_module`` with no ``try``. That one is
  fixed in this change: ``apps/buyer/Dockerfile`` now copies it and links it into
  ``.pkgroot``. It costs nothing in the pip layer (that package's module-scope closure is
  stdlib only), and ``proxyshop_support/tests/test_artifact_copyset.py`` is what proves it.

Until the lift lands, ``live_page_fetcher: "guarded"`` in a deployment document resolves
through :func:`~buyer_svc.livecheck.fetching.set_page_fetcher_factory` — a seam a process that
DOES ship the crawl core installs itself into — and binds
:class:`~buyer_svc.livecheck.fetching.NoPageFetcher` with a WARNING when nothing has. The
working configuration inside the shipped image is ``"recorded:<dir>"``, which is a real
deployment and not only a test double: a platform that has already crawled a store holds the
page bytes.
"""

from __future__ import annotations

from ..intent._spellings import bind_package
from .deferred import (
    ANONYMOUS_CALLER,
    CLAIM_VERIFIED_KIND,
    DEDUP_WINDOW_SECONDS,
    LIVE_CHECK_DIMENSION,
    MAX_FETCHES_PER_ORIGIN,
    MAX_QUEUED_TARGETS,
    MAX_RECORDED_CHECKS,
    ORIGIN_WINDOW_SECONDS,
    FetchBudget,
    LiveCheckLedger,
    LiveCheckQueue,
    LiveCheckRecord,
    live_check_ledger,
    live_check_queue,
    queue_live_checks,
    run_live_checks,
    set_live_check_ledger,
    set_live_check_queue,
)
from .fetching import (
    USER_AGENT,
    FetchedPage,
    GuardedPageFetcher,
    NoPageFetcher,
    PageFetcher,
    RecordedPageFetcher,
    guarded_page_fetcher,
    page_fetcher_factory,
    set_page_fetcher_factory,
)
from .targets import (
    ALLOWED_PAGE_SCHEMES,
    LiveCheckTarget,
    NoProductPages,
    NoRegisteredDomains,
    StaticProductPages,
    StaticRegisteredDomains,
    TargetRefused,
    domain_matches,
    normalise_domain,
    product_pages,
    registered_domains,
    set_product_pages,
    set_registered_domains,
    target_for,
    targets_for,
    usable_page_url,
)

__all__ = [
    "ALLOWED_PAGE_SCHEMES",
    "ANONYMOUS_CALLER",
    "CLAIM_VERIFIED_KIND",
    "DEDUP_WINDOW_SECONDS",
    "LIVE_CHECK_DIMENSION",
    "MAX_FETCHES_PER_ORIGIN",
    "MAX_QUEUED_TARGETS",
    "MAX_RECORDED_CHECKS",
    "ORIGIN_WINDOW_SECONDS",
    "USER_AGENT",
    "FetchBudget",
    "FetchedPage",
    "GuardedPageFetcher",
    "LiveCheckLedger",
    "LiveCheckQueue",
    "LiveCheckRecord",
    "LiveCheckTarget",
    "NoPageFetcher",
    "NoProductPages",
    "NoRegisteredDomains",
    "PageFetcher",
    "RecordedPageFetcher",
    "StaticProductPages",
    "StaticRegisteredDomains",
    "TargetRefused",
    "domain_matches",
    "guarded_page_fetcher",
    "live_check_ledger",
    "live_check_queue",
    "normalise_domain",
    "page_fetcher_factory",
    "product_pages",
    "queue_live_checks",
    "registered_domains",
    "run_live_checks",
    "set_live_check_ledger",
    "set_live_check_queue",
    "set_page_fetcher_factory",
    "set_product_pages",
    "set_registered_domains",
    "target_for",
    "targets_for",
    "usable_page_url",
]

# Both dotted spellings of this directory must resolve to ONE module object per file, or a
# process that reaches this package under both names holds two `LiveCheckQueue` classes and
# two copies of the module-level queue seam — so a render through one spelling would enqueue
# into a queue the drain through the other never sees. See `buyer_svc.intent._spellings`.
bind_package(__name__)
