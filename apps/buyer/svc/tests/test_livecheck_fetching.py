"""The fetcher: a caller of ``ingest``'s guard, a replay corpus, and no sockets anywhere.

Two things are proved here that nothing else can prove.

**That ``GuardedPageFetcher`` really is a caller of the crawl core rather than a second
fetcher.** The robots parser, the SSRF verdict and the budget ledger used below are
``ingest``'s OWN, imported here in the TEST — this directory is not in the buyer image's COPY
set, so a test may reach for them where ``apps/buyer/svc/src`` may not (see
``buyer_svc.livecheck.fetching``'s docstring for that measurement). Only the transport is a
double, and it opens no socket: the point is that every refusal below comes out of the real
guard, not out of a re-implementation.

**That the replay corpus still matches what was recorded.** ``tests/livepages/`` is
reconstructed markup around values captured from ``fixtures/real-catalogs``. If the price in a
fixture drifts from the price in the recorded catalogue, the fixture has quietly stopped being
evidence of anything, and every assertion built on it becomes decorative.

D3/C9: nothing here opens a socket.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from buyer_svc.livecheck.fetching import (
    USER_AGENT,
    GuardedPageFetcher,
    NoPageFetcher,
    RecordedPageFetcher,
    guarded_page_fetcher,
    set_page_fetcher_factory,
)

from apps.buyer.svc.tests._fixtures_livecheck import CORPUS, STORE_DOMAIN, corpus_manifest

REPO = Path(__file__).resolve().parents[4]


# ==============================================================================================
# The recorded corpus is still what was recorded
# ==============================================================================================


def test_the_replay_corpus_still_matches_the_recorded_real_catalogue() -> None:
    """Re-derive the fixture's values from the gzip they came from.

    Read straight out of ``fixtures/real-catalogs`` rather than from a second copy, so this
    fails the day the corpus and the capture disagree — which is the day the fixtures stop
    being evidence and become decoration.
    """
    provenance = corpus_manifest()["derived_from"]
    path = REPO / provenance["file"]
    assert path.is_file(), path

    with gzip.open(path, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    [product] = [row for row in rows if row["id"] == provenance["product_id"]]
    variant = product["variants"][0]

    assert product["handle"] == provenance["handle"]
    assert product["title"] == provenance["title"]
    assert variant["sku"] == provenance["variant_sku"]
    assert variant["price"] == provenance["variant_price"]
    assert variant["available"] is provenance["variant_available"]

    listed = provenance["variant_price"]
    agrees = (CORPUS / "agrees.html").read_text(encoding="utf-8")
    assert f'"price": "{listed}"' in agrees
    assert provenance["title"] in agrees


def test_the_corpus_says_what_it_is_and_what_it_is_not() -> None:
    """The manifest carries its own honesty, so a reader is not left to infer it."""
    manifest = corpus_manifest()
    assert "reconstruction" in manifest["what_this_is"]
    assert "recorded" in manifest["what_this_is"]
    assert "offline" in manifest["no_network"]


def test_a_url_the_corpus_does_not_hold_is_a_miss_and_never_evidence() -> None:
    fetcher = RecordedPageFetcher(CORPUS)
    page = fetcher.fetch("https://www.gaiaherbs.com/products/nothing-here")
    assert not page.ok
    assert "not in the recorded corpus" in page.reason


def test_the_recorded_fetcher_cannot_read_outside_its_own_corpus_directory() -> None:
    """``body_file`` comes from a manifest, and a manifest is a file somebody edits.

    Confined rather than trusted: the resolved path must sit under the corpus root, so a
    ``../../etc/passwd`` in a hand-edited manifest reads nothing.
    """
    fetcher = RecordedPageFetcher(
        {"https://x/": {"status": 200, "body_file": "../../../../etc/passwd"}}
    )
    assert fetcher.fetch("https://x/").body is None


# ==============================================================================================
# The guarded fetcher, composed with ingest's REAL robots and budget machinery
# ==============================================================================================


class _Transport:
    """A stand-in for ``SafeHTTPClient`` that answers from a table and opens no socket."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def fetch(self, url: str, *, allowed_hosts: Any = (), ledger: Any = None) -> Any:
        self.calls.append((url, tuple(allowed_hosts)))
        answer = self.answers.get(url)
        if answer is None:
            raise KeyError(url)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _result(url: str, status: int, body: bytes, content_type: str = "text/html") -> Any:
    from ingest.adapters.transport import HTTPResult

    return HTTPResult(
        url=url,
        final_url=url,
        status=status,
        headers={"content-type": content_type},
        body=body,
    )


def _fetcher(
    answers: dict[str, Any], *, clock: Any = None
) -> tuple[GuardedPageFetcher, _Transport]:
    from ingest.adapters.budgets import BudgetExceeded, CrawlBudget, CrawlLedger
    from ingest.adapters.netguard import FetchRefused
    from ingest.adapters.robots import (
        crawl_delay,
        may_fetch,
        robots_url,
        robots_verdict_for_status,
    )
    from ingest.adapters.transport import TransportError

    transport = _Transport(answers)
    budget = CrawlBudget(max_pages=4, max_depth=0, max_seconds=20.0, max_redirects=3)
    return (
        GuardedPageFetcher(
            client=transport,
            ledger_factory=lambda: CrawlLedger(budget),
            robots_url=robots_url,
            may_fetch=may_fetch,
            robots_verdict_for_status=robots_verdict_for_status,
            crawl_delay=crawl_delay,
            refusals=(FetchRefused, TransportError, BudgetExceeded),
            clock=clock,
        ),
        transport,
    )


PAGE = f"https://{STORE_DOMAIN}/products/reflux-relief"
ROBOTS = f"https://{STORE_DOMAIN}/robots.txt"


def test_a_page_a_stores_robots_txt_disallows_is_not_fetched() -> None:
    fetcher, transport = _fetcher(
        {
            ROBOTS: _result(ROBOTS, 200, b"User-agent: *\nDisallow: /products/\n", "text/plain"),
            PAGE: _result(PAGE, 200, b"<html></html>"),
        }
    )
    page = fetcher.fetch(PAGE)
    assert not page.ok
    assert "robots.txt disallows" in page.reason
    assert [url for url, _ in transport.calls] == [ROBOTS]


def test_a_robots_txt_that_is_unreachable_disallows_the_whole_origin() -> None:
    """RFC 9309 §2.3.1.4. A 503 is the site saying nothing at all; silence is not consent."""
    fetcher, transport = _fetcher(
        {
            ROBOTS: _result(ROBOTS, 503, b"", "text/plain"),
            PAGE: _result(PAGE, 200, b"<html></html>"),
        }
    )
    page = fetcher.fetch(PAGE)
    assert not page.ok
    assert "unreachable or forbids" in page.reason
    assert [url for url, _ in transport.calls] == [ROBOTS]


def test_a_404_robots_txt_means_no_rules_and_the_page_is_read() -> None:
    """§2.3.1.3. A 404 is the site saying "no rules", not the site saying "go away"."""
    fetcher, _ = _fetcher(
        {
            ROBOTS: _result(ROBOTS, 404, b"", "text/plain"),
            PAGE: _result(PAGE, 200, b"<html>ok</html>"),
        }
    )
    page = fetcher.fetch(PAGE)
    assert page.ok and page.body == b"<html>ok</html>"


def test_a_declared_crawl_delay_is_honoured_by_refusing_rather_than_by_sleeping() -> None:
    """A sleeping fetcher holds a worker for a duration the STORE chooses.

    That is a denial of service a store can aim at the platform. A refusal costs the store
    only a check it was never owed, and the target is picked up on a later pass.
    """
    ticks = iter([100.0, 100.5, 100.6])
    fetcher, transport = _fetcher(
        {
            ROBOTS: _result(
                ROBOTS, 200, b"User-agent: *\nCrawl-delay: 10\nAllow: /\n", "text/plain"
            ),
            PAGE: _result(PAGE, 200, b"<html>ok</html>"),
        },
        clock=lambda: next(ticks),
    )
    assert fetcher.fetch(PAGE).ok
    second = fetcher.fetch(PAGE)
    assert not second.ok
    assert "Crawl-delay" in second.reason
    assert [url for url, _ in transport.calls].count(PAGE) == 1


def test_the_page_fetch_allows_no_host_but_the_one_it_started_on() -> None:
    """A redirect off the registered domain must be refused, not followed.

    ``SafeHTTPClient`` adds the starting host to ``allowed_hosts`` itself and refuses a hop
    anywhere else, so passing an EMPTY allow-list is what pins the chain to the one host the
    registry approved. Asserted on the call, because it is a one-word mistake to widen.
    """
    fetcher, transport = _fetcher(
        {
            ROBOTS: _result(ROBOTS, 404, b"", "text/plain"),
            PAGE: _result(PAGE, 200, b"<html>ok</html>"),
        }
    )
    fetcher.fetch(PAGE)
    assert transport.calls == [(ROBOTS, ()), (PAGE, ())]


def test_an_ssrf_refusal_out_of_the_guard_is_a_no_verdict_and_not_an_exception() -> None:
    from ingest.adapters.netguard import FetchRefused

    fetcher, _ = _fetcher(
        {
            ROBOTS: _result(ROBOTS, 404, b"", "text/plain"),
            PAGE: FetchRefused(PAGE, "blocked-network:169.254.169.254"),
        }
    )
    page = fetcher.fetch(PAGE)
    assert not page.ok
    assert "blocked-network" in page.reason


def test_a_non_200_is_reported_with_its_status_and_decides_nothing() -> None:
    fetcher, _ = _fetcher(
        {
            ROBOTS: _result(ROBOTS, 404, b"", "text/plain"),
            PAGE: _result(PAGE, 500, b"oops"),
        }
    )
    page = fetcher.fetch(PAGE)
    assert not page.ok
    assert "500" in page.reason


def test_an_unforeseen_transport_failure_is_a_no_verdict_too() -> None:
    fetcher, _ = _fetcher(
        {ROBOTS: _result(ROBOTS, 404, b"", "text/plain"), PAGE: OSError("connection reset")}
    )
    page = fetcher.fetch(PAGE)
    assert not page.ok
    assert "OSError" in page.reason


def test_robots_txt_is_read_once_per_origin() -> None:
    fetcher, transport = _fetcher(
        {
            ROBOTS: _result(ROBOTS, 404, b"", "text/plain"),
            PAGE: _result(PAGE, 200, b"<html>ok</html>"),
        }
    )
    fetcher.fetch(PAGE)
    fetcher.fetch(PAGE)
    assert [url for url, _ in transport.calls].count(ROBOTS) == 1


# ==============================================================================================
# C6: these are real stores
# ==============================================================================================


@pytest.mark.parametrize("token", ["mozilla", "applewebkit", "chrome", "safari", "gecko", "edg/"])
def test_the_crawler_never_impersonates_a_browser(token: str) -> None:
    """C6 forbids it, and the acceptance suite greps for exactly these tokens."""
    assert token not in USER_AGENT.lower()


def test_the_user_agent_names_a_product_a_version_and_a_contact() -> None:
    assert USER_AGENT.startswith("ProxyShopBot/")
    assert "http" in USER_AGENT and "@" in USER_AGENT


def test_this_module_s_user_agent_is_the_one_ingest_crawls_under() -> None:
    """One crawler identity, not two.

    A second agent string would be a second thing for a site operator to allow-list, and
    ``robots.py``'s whole point is that a site can address us by one name.
    """
    from ingest.adapters.robots import USER_AGENT as INGEST_USER_AGENT

    assert USER_AGENT == INGEST_USER_AGENT


# ==============================================================================================
# The factory seam
# ==============================================================================================


def test_with_no_factory_installed_the_process_builds_no_fetcher() -> None:
    set_page_fetcher_factory(None)
    assert guarded_page_fetcher() is None


def test_a_factory_that_raises_is_reported_rather_than_failing_composition() -> None:
    def explode() -> Any:
        raise RuntimeError("the crawl core is misconfigured")

    set_page_fetcher_factory(explode)
    try:
        assert guarded_page_fetcher() is None
    finally:
        set_page_fetcher_factory(None)


def test_an_installed_factory_is_what_a_guarded_deployment_gets() -> None:
    made = NoPageFetcher()
    set_page_fetcher_factory(lambda: made)
    try:
        assert guarded_page_fetcher() is made
    finally:
        set_page_fetcher_factory(None)
