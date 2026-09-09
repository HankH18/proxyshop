"""A STORE WHOSE RECORDED PAGE IS TOO LARGE LOADED ZERO PRODUCTS, AND THE LOAD SAID FINE.

The defect this file gates, measured before the fix on ``fixtures/real-catalogs-broad``::

    python - <<'PY'
    from pathlib import Path
    from ingest.adapters.recorded import RecordedCorpus
    from ingest.scheduler.load_corpus import load_recorded_corpus
    corpus = RecordedCorpus.load(Path("fixtures/real-catalogs-broad"))
    report = load_recorded_corpus(corpus, session_factory=None, apply_schema_first=False)
    print(report.products, "of", corpus.products_recorded)
    PY
    # before:  15360 of 17409     after:  17409 of 17409

Four of the corpus's 38 stores read **zero** products — ``sabai.design`` (321 recorded),
``branchfurniture.com`` (220), ``cotopaxi.com`` (1,432) and ``tenthousand.cc`` (76) — because
their ``products.json`` pages are larger than ``CrawlBudget.max_response_bytes``, and the
crawl budget was being applied to pages this repository had already fetched, verified and
committed to disk. The load printed a clean per-store table and exited ``0``.

Three separate things had to be true for a store with 1,430 products on disk to become a zero
nobody noticed, and each has its own section below.

1. **The ceiling was defending against nothing.** A byte ceiling on a live fetch stops a
   hostile origin filling memory; the live transport enforces it chunk by chunk while the body
   is arriving. On replay the bytes are already in RAM — ``RecordedCorpus.load`` put them
   there — so charging them can only refuse data we own.
   :func:`~ingest.adapters.recorded.replay_budget` is the ceiling a replay belongs under, and
   the live one has to stay exactly where it is: §2 asserts both halves.
2. **A blown budget threw away the products already read.** ``cotopaxi.com`` blew the ceiling
   on page 5 having already parsed 1,000 products across pages 1-4, and reported 0. §3.
3. **The loss was a warning.** ``report.warnings`` carried it, ``main()`` returned ``0``, and
   every measurement downstream was taken on a corpus nobody had been told was short. §4
   grades the refusal that replaced it.

WHY THESE TESTS IMPORT INSIDE THE FUNCTION. The names added by the fix
(:class:`~ingest.scheduler.load_corpus.CorpusLoadShortfall` and friends) are imported in the
test that needs them rather than at module scope, so that against the pre-fix tree each test
here fails **on its own assertion** instead of the whole file failing to collect on an
``ImportError``. A regression gate that can only say "collection error" cannot say what
regressed.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from ingest.adapters.budgets import CrawlBudget
from ingest.adapters.recorded import RecordedCorpus, RecordedStore, RecordedTransport
from ingest.adapters.signed_fetch import SignedFetchAdapter
from ingest.scheduler.load_corpus import corpus_target, load_recorded_corpus, recorded_runner

#: services/ingest/tests/… -> tests -> ingest -> services -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The 38-store corpus the defect was measured on. Held separately from the ten-supplement
#: corpus ``RecordedCorpus.load()`` defaults to, because only this one has stores big enough
#: to reach the ceiling — the largest page in the supplement corpus is well under it.
BROAD_CORPUS = REPO_ROOT / "fixtures" / "real-catalogs-broad"

#: The four stores that loaded nothing, and the product counts their recordings hold. Written
#: out rather than derived so that a corpus edit which quietly shrinks one of them is a
#: failure here and not a silently weaker test.
FORMERLY_ZERO = {
    "sabai.design": 321,
    "branchfurniture.com": 220,
    "cotopaxi.com": 1432,
    "tenthousand.cc": 76,
}

#: What the whole broad corpus holds, and what a complete load of it must therefore read.
BROAD_PRODUCTS = 17409
BROAD_STORES = 38

#: What a load of it read before the fix. Named so the gate states the size of the loss it is
#: standing in front of: 2,049 products, one of them the coffee tables the corpus exists for.
BROAD_PRODUCTS_BEFORE = 15360


@pytest.fixture(scope="module")
def broad_corpus() -> RecordedCorpus:
    """The 38-store recording, loaded once. Pure disk reads; no graph, no socket."""
    if not (BROAD_CORPUS / "collection.json").is_file():
        pytest.skip(f"the broad corpus is not in this tree at {BROAD_CORPUS}")
    return RecordedCorpus.load(BROAD_CORPUS)


def _replay(store: RecordedStore, *, target: object = None, **kwargs: object) -> object:
    """One store through the live crawl path, under the ORDINARY corpus target.

    No budget is passed unless a test asks for one, which is the point: every replay caller
    gets this right, not only :func:`~ingest.scheduler.load_corpus.load_recorded_corpus`.
    ``ingest/tests/test_catalog_images.py`` builds its request exactly this way.
    """
    adapter = SignedFetchAdapter(client=RecordedTransport(store=store))
    plan = target if target is not None else corpus_target(store)
    return adapter.fetch_catalog(plan.request(**kwargs))  # type: ignore[attr-defined, arg-type]


# =======================================================================================
# 1. The corpus loads, whole
# =======================================================================================


def test_replaying_the_broad_corpus_reads_every_product_the_corpus_holds(
    broad_corpus: RecordedCorpus,
) -> None:
    """The headline: 38 stores in, 17,409 products out, none of them dropped.

    ``session_factory=None`` computes every write without performing one, so this drives the
    whole adapter path — robots, pagination, parse, identity, mapping — with no Neo4j and no
    D37 flock, which is what lets it be an ordinary unit test rather than a four-minute one.
    """
    assert len(broad_corpus.stores) == BROAD_STORES
    assert broad_corpus.products_recorded == BROAD_PRODUCTS

    report = load_recorded_corpus(
        broad_corpus, session_factory=None, apply_schema_first=False, include_media=False
    )

    per_store = {one.store_id: one.products for one in report.reports}
    short = {
        host: (count, broad_corpus.by_host(host).products_recorded)
        for host, count in per_store.items()
        if count < broad_corpus.by_host(host).products_recorded
    }
    assert not short, f"stores read fewer products than the corpus holds: {short}"
    assert report.products == BROAD_PRODUCTS, (
        f"the load read {report.products} of {BROAD_PRODUCTS} recorded products; before the "
        f"replay stopped being metered against the live ceiling it read {BROAD_PRODUCTS_BEFORE}"
    )


@pytest.mark.parametrize(("host", "recorded"), sorted(FORMERLY_ZERO.items()))
def test_a_store_whose_page_exceeds_the_live_ceiling_still_loads(
    broad_corpus: RecordedCorpus, host: str, recorded: int
) -> None:
    """Each of the four zeros, named, with the page size that caused it.

    The assertion on ``largest_page_bytes`` is not decoration: it is what makes this test
    still about the ceiling. If a future corpus re-collection shrinks these stores under
    4 MiB the test would keep passing for the wrong reason, and this line turns that into a
    failure that says so.
    """
    store = broad_corpus.by_host(host)
    live_ceiling = CrawlBudget().max_response_bytes
    assert store.largest_page_bytes > live_ceiling, (
        f"{host}'s largest recorded page is {store.largest_page_bytes} bytes, no longer over "
        f"the live {live_ceiling}-byte per-response ceiling — this test no longer grades the "
        f"defect it was written for"
    )

    snapshot = _replay(store)

    assert len(snapshot.products) == recorded, (  # type: ignore[attr-defined]
        f"{host}: {len(snapshot.products)} of {recorded} recorded products; "  # type: ignore[attr-defined]
        f"warnings={snapshot.warnings}"  # type: ignore[attr-defined]
    )


# =======================================================================================
# 2. The replay's ceiling comes from the recording; the live path's does not move
# =======================================================================================


def test_the_live_crawl_budget_is_exactly_where_it_was(broad_corpus: RecordedCorpus) -> None:
    """The fix must not have widened the defence a live crawl runs under.

    ``CrawlBudget``'s defaults are what stands between the crawler and a hostile origin, and
    they are also reused as the request ceiling on ``POST /extract``
    (``ingest.extraction.routes.MAX_PAGE_BODY_CHARS``). Widening them to make the corpus load
    would have been the wrong fix in a way nothing downstream would have noticed.
    """
    live = CrawlBudget()
    assert live.max_response_bytes == 4 * 1024 * 1024
    assert live.max_decompressed_bytes == 16 * 1024 * 1024
    assert live.max_bytes == 32 * 1024 * 1024
    assert live.max_pages == 200
    assert live.max_seconds == 60.0


def test_the_replay_budget_admits_the_recording_and_narrows_nothing(
    broad_corpus: RecordedCorpus,
) -> None:
    """Every dimension is ``max(live, what this recording needs)`` — never less than live."""
    from ingest.adapters.recorded import replay_budget  # noqa: PLC0415 - see module docstring

    live = CrawlBudget()
    for store in broad_corpus.stores:
        budget = replay_budget(store)
        assert budget.max_response_bytes >= store.largest_page_bytes, store.host
        assert budget.max_bytes >= store.replay_bytes, store.host
        assert budget.max_pages >= store.replay_responses, store.host
        # A gzip-bomb ceiling under the per-response ceiling would mean nothing.
        assert budget.max_decompressed_bytes >= budget.max_response_bytes, store.host
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
            assert getattr(budget, name) >= getattr(live, name), (
                f"replay_budget narrowed {name} for {store.host}: "
                f"{getattr(budget, name)} < {getattr(live, name)}"
            )


def test_the_replay_is_still_metered_as_a_whole(broad_corpus: RecordedCorpus) -> None:
    """Not applying the per-response ceilings is not the same as running unmetered.

    The replay still charges pages, whole-crawl bytes and the clock — so ``snapshot.usage``
    means something, and a replay pointed at a budget that genuinely cannot hold it is still
    cut short instead of running to the end of the corpus.
    """
    store = broad_corpus.by_host("cotopaxi.com")

    snapshot = _replay(store, budget=CrawlBudget(max_pages=2))

    assert snapshot.usage.pages == 2, snapshot.usage  # type: ignore[attr-defined]
    assert len(snapshot.products) < store.products_recorded  # type: ignore[attr-defined]
    assert any("pages" in w for w in snapshot.warnings), snapshot.warnings  # type: ignore[attr-defined]


# =======================================================================================
# 3. A blown budget keeps what it already read
# =======================================================================================


def test_a_budget_blown_mid_crawl_keeps_the_products_already_read(
    broad_corpus: RecordedCorpus,
) -> None:
    """``cotopaxi.com`` blew the ceiling on page 5 and reported 0 having parsed 1,000.

    The budget here is deliberately narrow — it admits the four smaller pages and refuses the
    fifth — which is the live path's own shape, not a replay quirk: the same thing happens to
    a live crawl of a store whose tenth page is fat. ``catalog_mcp`` has always kept what it
    read on a blown budget; ``signed_fetch`` was the one that unwound the lot.
    """
    store = broad_corpus.by_host("cotopaxi.com")
    ordered = [page for page in sorted(store.pages, key=lambda p: p.number)]
    assert len(ordered) >= 4, "cotopaxi no longer paginates, so nothing here is mid-crawl"
    # Robots plus the first two pages exactly: the third page is the one that goes over.
    ceiling = len(store.robots_body.encode("utf-8")) + sum(len(p.body) for p in ordered[:2])
    kept = sum(p.products for p in ordered[:2])

    snapshot = _replay(store, budget=CrawlBudget(max_bytes=ceiling))

    products = len(snapshot.products)  # type: ignore[attr-defined]
    assert products == kept, (
        f"a crawl cut short after two pages read {products} products, not the {kept} those "
        f"pages carried; it should keep the pages it managed and stop, not unwind to zero"
    )
    blamed = [w for w in snapshot.warnings if "budget" in w]  # type: ignore[attr-defined]
    assert blamed, f"the truncation was not reported at all: {snapshot.warnings}"  # type: ignore[attr-defined]
    assert str(products) in blamed[0] and "not its catalogue" in blamed[0], (
        f"the warning does not say how much was kept or that it is a truncation: {blamed[0]}"
    )


def test_stopping_at_the_product_ceiling_says_so(broad_corpus: RecordedCorpus) -> None:
    """``max_products`` truncated a catalogue in total silence — no warning, just a number.

    ``StoreTarget.max_products`` defaults to 250 and ``taylorstitch.com`` has 3,805, so "250
    products" was indistinguishable from a 250-product store.
    """
    store = broad_corpus.by_host("taylorstitch.com")
    assert store.products_recorded > 250

    snapshot = _replay(store, target=dataclasses.replace(corpus_target(store), max_products=250))

    assert len(snapshot.products) == 250  # type: ignore[attr-defined]
    said = [w for w in snapshot.warnings if "max_products" in w]  # type: ignore[attr-defined]
    assert said, (
        f"250 of {store.products_recorded} products were read and nothing said so: "
        f"{snapshot.warnings}"  # type: ignore[attr-defined]
    )
    assert "not the whole catalogue" in said[0], said[0]


def test_a_store_that_fits_the_ceiling_is_not_warned_about(
    broad_corpus: RecordedCorpus,
) -> None:
    """The counterpart: a store read whole must not carry a truncation warning.

    Without this, a warning that fires on every store would "fix" the silence by making it
    unreadable, and the four-zero defect would be just as invisible in the noise.
    """
    store = broad_corpus.by_host("flybyjing.com")
    assert store.products_recorded < 250

    snapshot = _replay(store)

    noisy = [
        w
        for w in snapshot.warnings  # type: ignore[attr-defined]
        if "max_products" in w or "budget" in w
    ]
    assert not noisy, noisy


# =======================================================================================
# 4. A short load is refused, by name, with the numbers
# =======================================================================================


def test_a_short_load_is_refused_and_names_the_store_and_both_counts(
    broad_corpus: RecordedCorpus,
) -> None:
    """The load must not return a report that looks complete when a store came up short.

    The shortfall is manufactured by telling the corpus one store holds five more products
    than its pages carry, which is the exact condition the check is about — *fewer products
    reached the graph than the corpus holds* — reached without depending on which limit
    caused it. That is deliberate: the byte ceiling was one cause, and a robots refusal, an
    unparseable page or an entry with no id would have been just as silent.
    """
    from ingest.scheduler.load_corpus import CorpusLoadShortfall  # noqa: PLC0415 - see docstring

    store = broad_corpus.by_host("flybyjing.com")
    inflated = dataclasses.replace(store, products_recorded=store.products_recorded + 5)
    corpus = dataclasses.replace(broad_corpus, stores=(inflated,))

    with pytest.raises(CorpusLoadShortfall) as exc:
        load_recorded_corpus(
            corpus, session_factory=None, apply_schema_first=False, include_media=False
        )

    message = str(exc.value)
    assert "flybyjing.com" in message
    assert str(inflated.products_recorded) in message, message
    assert str(store.products_recorded) in message, message
    assert exc.value.shortfalls[0].host == "flybyjing.com"
    assert exc.value.shortfalls[0].missing == 5
    # The report survives the refusal: a short load still has to hand back everything it
    # measured, or the operator's only option is to re-run it blind.
    assert exc.value.report is not None
    assert exc.value.report.products == store.products_recorded
    assert exc.value.report.complete is False


def test_a_partial_load_can_be_asked_for_but_never_looks_complete(
    broad_corpus: RecordedCorpus,
) -> None:
    """``require_complete=False`` returns the report — still carrying the shortfall."""
    store = broad_corpus.by_host("flybyjing.com")
    inflated = dataclasses.replace(store, products_recorded=store.products_recorded + 5)
    corpus = dataclasses.replace(broad_corpus, stores=(inflated,))

    report = load_recorded_corpus(
        corpus,
        session_factory=None,
        apply_schema_first=False,
        include_media=False,
        require_complete=False,
    )

    assert report.complete is False
    assert [one.host for one in report.shortfalls] == ["flybyjing.com"]
    assert report.recorded == inflated.products_recorded


def test_the_shortfall_check_carries_the_reason_the_store_came_up_short(
    broad_corpus: RecordedCorpus,
) -> None:
    """A refusal that says "short" and not "why" sends the operator back to the logs.

    Driven through the real runner with a budget narrow enough to cut the crawl, so the
    reason on the shortfall is the adapter's own warning rather than something this test
    composed.
    """
    from ingest.scheduler.load_corpus import shortfalls_in  # noqa: PLC0415 - see docstring

    host = "cotopaxi.com"
    corpus = dataclasses.replace(broad_corpus, stores=(broad_corpus.by_host(host),))
    runner = recorded_runner(corpus, session_factory=None)
    report = runner.refresh(
        host,
        budget=CrawlBudget(max_bytes=1024),
        adapter=SignedFetchAdapter(client=RecordedTransport(store=corpus.by_host(host))),
    )

    short = shortfalls_in(corpus, [report])

    assert len(short) == 1
    assert short[0].host == host
    assert short[0].loaded == 0 and short[0].recorded == FORMERLY_ZERO[host]
    rendered = str(short[0])
    assert "bytes limit=1024" in rendered, rendered
    assert f"the load read {short[0].loaded}" in rendered, rendered


def test_a_store_that_really_stocks_nothing_is_not_a_shortfall() -> None:
    """The zero with two readings, told apart.

    A store whose recording holds no products is a store that stocks none, and must not be
    reported as a loss — otherwise the refusal cries wolf and gets switched off, which is how
    the original warning came to be ignored.
    """
    from ingest.scheduler.load_corpus import shortfalls_in  # noqa: PLC0415 - see docstring

    empty = RecordedStore(
        host="stocksnothing.example",
        origin="https://stocksnothing.example",
        role="control",
        robots_status=200,
        products_json_allowed=True,
        crawl_delay_seconds=0.0,
        pages=(),
        products_recorded=0,
    )
    corpus = RecordedCorpus(
        root=Path("in-memory"), collected_at="", corpus_version="test", stores=(empty,)
    )

    report = load_recorded_corpus(
        corpus, session_factory=None, apply_schema_first=False, include_media=False
    )

    assert report.products == 0
    assert report.complete is True
    assert shortfalls_in(corpus, report.reports) == ()
