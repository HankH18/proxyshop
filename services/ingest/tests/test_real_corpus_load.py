"""Ten real storefronts, replayed through the live crawl path into the real Neo4j.

WHAT THIS FILE GRADES that nothing else did. ``fixtures/real-catalogs/`` has held 3,093
products from ten real supplement storefronts — verbatim bytes, per-record provenance —
and ``services/ingest`` has been able to crawl a storefront into Neo4j. **Nothing had ever
loaded one into the other.** Every graph demo in the repository ran on
``fixtures/catalog/coffee.json``: four generated product families, one attribute template,
one category. The roster query had never had to reject a shop.

Three properties are asserted here and each was previously unmeasurable:

* **The replay is the crawl.** :class:`~ingest.adapters.recorded.RecordedTransport` replaces
  the *transport* and nothing above it, so provenance, ids, change detection, upsert order
  and embeddings are produced by the same code a live crawl uses. The test drives the
  pagination loop and reads back the URLs the adapter actually asked for.
* **The bytes are the ones the storefront served.** Every page is reassembled from the
  recorded per-product byte spans and checked against the digest the live fetch took over
  the whole response, *before* anything reaches the adapter.
* **Retrieval works on real data, including rejecting.** A milk-thistle query finds the
  stores that stock it and leaves out the two that stock none — which is the half a
  category-biased corpus can never demonstrate, because a shortlist that never rejects
  anybody proves nothing.

WHY THE GRAPH TESTS USE A SUBSET. A whole-corpus load is 44,803 graph writes and takes
about four minutes, and it holds the machine-global D37 flock for all of it — which every
concurrent lane would wait on. The subsets below keep the *shape* that matters (stores that
stock the query, stores that stock none of it, a price spread) at a fifth of the cost. The
whole-corpus load is what ``python -m ingest.scheduler.load_corpus --all`` is for, and its
per-store report is the CLI's own output.

Everything here runs offline (D3/C9): :mod:`ingest.adapters.recorded` imports no networking
library at all, and :func:`test_replaying_the_whole_corpus_opens_no_socket` proves it by
making every socket constructor raise.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

import pytest
from ingest.adapters.recorded import (
    PRODUCTS_PATH,
    CorpusIntegrityError,
    RecordedCorpus,
    RecordedTransport,
    UnrecordedRequest,
)
from ingest.graph import candidate_shops, provenance_violations
from ingest.graph.query import products_missing_embeddings
from ingest.scheduler.load_corpus import (
    corpus_target,
    load_recorded_corpus,
    recorded_runner,
)

#: A shopper query the corpus really can answer. Never compared against a product name —
#: it is embedding input, exactly as it is in production.
LIVER_QUERY = "milk thistle silymarin liver support extract"

#: A query this corpus has no business answering. Ten supplement storefronts stock no
#: engine parts, and the point of asking is that the system should be able to *say* so
#: rather than returning its ten best guesses with a straight face.
OFF_CORPUS_QUERY = "diesel engine turbocharger rebuild kit for a tractor"

#: Four recorded stores that stock milk thistle and two that stock none of it. The 4:2 split
#: is the 8:2 split of the whole corpus at a fifth of the write cost, and it is the shape the
#: roster has to get right: finding the four is easy, leaving out the two is the test.
LIVER_STOCKED = ("gaiaherbs.com", "toniiq.com", "paradiseherbs.com", "oregonswildharvest.com")
LIVER_EMPTY = ("livemomentous.com", "nakednutrition.com")
ROSTER_HOSTS = (*LIVER_STOCKED, *LIVER_EMPTY)

#: A two-store subset for the tests that grade the write rather than the read.
WRITE_HOSTS = ("gaiaherbs.com", "toniiq.com")

#: A category and a brand no supplement storefront carries, and one the corpus really does.
#: R19 makes a hard constraint an eligibility filter, so these are where an honest "no"
#: lives — and the third is the positive control that stops `[]` meaning "the filter is
#: broken". ``Capsules`` is a real `product_type` in this subset; the categories the corpus
#: actually yields are things like `Bulk Herbs`, `Liquid Phyto-Caps®`, `Powder` and
#: `membership`, because `product_type` is a dumping ground rather than a taxonomy.
ABSENT_CATEGORY = "Automotive Parts"
ABSENT_BRAND = "Cummins"
PRESENT_CATEGORY = "Capsules"


@pytest.fixture(scope="module")
def recorded_corpus() -> RecordedCorpus:
    """The whole recording, loaded once. Pure disk reads; no graph, no socket."""
    return RecordedCorpus.load()


def _counts(session: Any) -> tuple[dict[str, int], dict[str, int]]:
    """Node counts by label and relationship counts by type, read back out of Neo4j."""
    nodes = {
        row["label"]: row["count"]
        for row in session.run(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count ORDER BY label"
        ).data()
    }
    edges = {
        row["type"]: row["count"]
        for row in session.run(
            "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY type"
        ).data()
    }
    return nodes, edges


@contextmanager
def _reuse(session: Any) -> Iterator[Any]:
    """Hand the loader the test's own session so its writes land inside the D37 flock."""
    yield session


def _load(session: Any, hosts: tuple[str, ...], **kwargs: Any) -> Any:
    """Replay ``hosts`` into the session this test already holds."""
    corpus = RecordedCorpus.load(hosts=hosts)
    return load_recorded_corpus(corpus, session_factory=lambda: _reuse(session), **kwargs)


# =======================================================================================
# 1. The recording reproduces what was served, before anything is written
# =======================================================================================


def test_every_recorded_page_reassembles_to_the_digest_the_live_fetch_took(
    recorded_corpus: RecordedCorpus,
) -> None:
    """The claim the whole replay rests on, checked rather than asserted in prose.

    Each page is rebuilt as ``{"products":[`` + the verbatim per-product byte slices joined
    by ``,`` + ``]}`` and hashed. ``response_sha256`` in the provenance file is the digest
    the *live* fetch took over the whole response body on 2026-09-07. They match for every
    page of every store, which is what makes "the adapter is parsing the bytes the
    storefront served" a measurement.
    """
    assert recorded_corpus.integrity_problems() == []
    pages = [page for store in recorded_corpus.stores for page in store.pages]
    assert len(pages) == 18, "the collection made 18 catalogue fetches"
    assert all(page.reproduces_recorded_bytes for page in pages)
    assert sum(page.products for page in pages) == recorded_corpus.products_recorded == 3093


def test_a_tampered_record_makes_the_corpus_refuse_to_load(tmp_path: Any) -> None:
    """Sabotage: the integrity check has to be able to fail, or it proves nothing.

    Without this, :func:`test_every_recorded_page_reassembles_to_the_digest_the_live_fetch_took`
    demonstrates only that a comparison of two equal strings returns ``True``.
    """
    import gzip
    import json
    import shutil

    source = RecordedCorpus.load(hosts=("paradiseherbs.com",))
    shutil.copytree(source.root, tmp_path / "corpus")
    products = tmp_path / "corpus" / "stores" / "paradiseherbs.com.products.jsonl.gz"
    lines = gzip.decompress(products.read_bytes()).split(b"\n")
    first = json.loads(lines[0])
    first["title"] = "Tampered With"
    lines[0] = json.dumps(first, separators=(",", ":")).encode("utf-8")
    products.write_bytes(gzip.compress(b"\n".join(lines)))

    with pytest.raises(CorpusIntegrityError, match="do not reproduce what was served"):
        RecordedCorpus.load(tmp_path / "corpus", hosts=("paradiseherbs.com",))


def test_the_replay_refuses_a_url_the_recording_does_not_hold(
    recorded_corpus: RecordedCorpus,
) -> None:
    """A replay that guessed would be a crawl of a storefront that never answered.

    Three refusals, and each closes a way a "recorded" run could quietly stop being one:
    a path nobody recorded, a page size the collector never used (the byte spans are of
    ``limit=250`` pages, so a slice of one is a response nobody sent), and a host outside
    the allow-list the crawl was given.
    """
    store = recorded_corpus.by_host("toniiq.com")
    transport = RecordedTransport(store=store)
    origin = store.origin.rstrip("/")

    with pytest.raises(UnrecordedRequest, match="carries no product pages"):
        transport.fetch(f"{origin}/products/some-handle")
    with pytest.raises(UnrecordedRequest, match="recorded at limit=250"):
        transport.fetch(f"{origin}{PRODUCTS_PATH}?limit=50&page=1")
    with pytest.raises(UnrecordedRequest, match="host-not-allow-listed"):
        transport.fetch(
            f"{origin}{PRODUCTS_PATH}?limit=250&page=1", allowed_hosts=("elsewhere.example",)
        )


def test_replaying_the_whole_corpus_opens_no_socket(recorded_corpus: RecordedCorpus) -> None:
    """D3/C9: the whole 3,093-product corpus through fetch + map, with sockets poisoned.

    ``session_factory=None`` computes every graph write without performing one, so this
    covers the entire read-and-map half of the load — pagination, robots, the
    ``products.json`` parse, identity, change detection, ``build_upserts`` — for all ten
    stores at once. If any of it could reach a storefront, these are real businesses and
    it would reach them.
    """
    with (
        mock.patch.object(socket, "socket", side_effect=AssertionError("opened a socket")),
        mock.patch.object(
            socket, "create_connection", side_effect=AssertionError("opened a connection")
        ),
        mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("resolved a hostname")),
    ):
        report = load_recorded_corpus(recorded_corpus, session_factory=None)

    assert report.products == 3093
    assert report.ops == 44803, "every graph write the whole corpus implies"
    assert len(report.reports) == 10
    # The pagination loop really ran: three stores hold more than one recorded page
    # (bulksupplements 4, nutricost 4, purebulk 3), and the URLs the adapter asked for are
    # the ones the collector fetched. A load handed a list of products instead of driving
    # the crawl would show no `page=2` at all.
    assert sum(1 for url in report.urls_requested if "page=2" in url) == 3
    # 18 catalogue pages + 10 robots.txt = 28 requests, which is *exactly* what the live
    # collection cost (collection.json: "requests_made": 28). The adapter stops on a short
    # page rather than asking for the empty one past the end, so the replay walks the same
    # 18 pages the collector walked and not one more.
    assert sum(1 for url in report.urls_requested if PRODUCTS_PATH in url) == 18
    assert sum(1 for url in report.urls_requested if url.endswith("/robots.txt")) == 10
    assert len(report.urls_requested) == 28
    assert all(
        url.endswith("/robots.txt") or f"{PRODUCTS_PATH}?limit=250" in url
        for url in report.urls_requested
    )


def test_the_replay_obeys_the_recorded_robots_decision(recorded_corpus: RecordedCorpus) -> None:
    """A host the real crawl was allowed stays allowed; the rendering is checked, not trusted.

    The collector recorded each host's robots *decision* and digest but not its body, so the
    replay renders the decision back into robots grammar. That rendering is only honest if
    parsing it reproduces the recorded verdict, which is what
    :meth:`RecordedCorpus.integrity_problems` checks — here for all ten hosts at once.
    """
    assert all(store.products_json_allowed for store in recorded_corpus.stores)
    assert all(store.robots_status == 200 for store in recorded_corpus.stores)
    assert recorded_corpus.integrity_problems() == []
    for store in recorded_corpus.stores:
        assert "not the bytes the host served" in store.robots_body


def test_a_recorded_store_becomes_a_target_the_recording_can_actually_answer() -> None:
    """The three fields the recording forces, pinned so a default cannot silently break it."""
    corpus = RecordedCorpus.load(hosts=("bulksupplements.com",))
    target = corpus_target(corpus.by_host("bulksupplements.com"))
    assert target.store_id == "bulksupplements.com"
    assert target.source == "signed_fetch"
    assert target.fetch_product_pages is False, "the corpus holds no product HTML"
    assert target.max_products >= 805, "a ceiling under the store's size would truncate it"
    assert target.max_products >= 250, "the corpus was recorded at limit=250"


# =======================================================================================
# 2. The load lands a real, provenanced, retrievable catalog in the real graph
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_recorded_crawl_lands_a_provenanced_real_catalog(graph_schema_session: Any) -> None:
    """Two real storefronts through the live write path, graded against their own contents.

    Counts are asserted against what the recording holds rather than against magic numbers,
    so a load that silently dropped a variant or a gallery fails here rather than passing
    with a smaller graph.
    """
    import json

    report = _load(graph_schema_session, WRITE_HOSTS)
    corpus = RecordedCorpus.load(hosts=WRITE_HOSTS)
    expected_products = sum(store.products_recorded for store in corpus.stores)
    expected_variants = sum(
        len(product.get("variants") or [])
        for store in corpus.stores
        for page in store.pages
        for product in json.loads(page.body)["products"]
    )

    nodes, edges = _counts(graph_schema_session)
    assert nodes["Store"] == 2
    assert nodes["Product"] == expected_products == report.products
    assert nodes["Variant"] == expected_variants
    assert edges["SELLS"] == expected_products
    assert edges["HAS_VARIANT"] == expected_variants
    assert nodes["MediaAsset"] > 0 and edges["HAS_MEDIA"] == nodes["MediaAsset"]

    assert provenance_violations(graph_schema_session) == [], (
        "a roster drops an unsourced shop entirely and an unsourced offer's price"
    )
    assert products_missing_embeddings(graph_schema_session) == [], (
        "a product with no vector is invisible to every shopper query"
    )
    assert report.embedded == expected_products
    assert report.warnings == () or all("images" in w for w in report.warnings)


@pytest.mark.docker
@pytest.mark.graph
def test_loading_the_recording_twice_leaves_the_graph_identical(
    graph_schema_session: Any,
) -> None:
    """Both halves of idempotence, because a hash ledger can hide the one that matters.

    * **Differential** — a runner that already loaded these stores feeds its own
      ``hash_index`` back in, so the second load computes **zero** ops and never opens a
      graph session at all.
    * **Convergent** — ``force=True`` drops the remembered hashes and replays every one of
      the ops against the real uniqueness constraints. The node and relationship counts must
      come back byte-identical, which is what actually grades ``MERGE``.
    """
    runner = recorded_runner(
        RecordedCorpus.load(hosts=WRITE_HOSTS),
        session_factory=lambda: _reuse(graph_schema_session),
    )
    first = _load(graph_schema_session, WRITE_HOSTS, runner=runner)
    after_first = _counts(graph_schema_session)

    differential = _load(graph_schema_session, WRITE_HOSTS, runner=runner)
    assert differential.ops == 0, "unchanged content must cost no graph work at all"
    assert differential.written == 0
    assert _counts(graph_schema_session) == after_first

    forced = _load(graph_schema_session, WRITE_HOSTS, runner=runner, force=True)
    assert forced.ops == first.ops > 0, "force must genuinely re-do the work"
    assert _counts(graph_schema_session) == after_first, (
        "re-running the loader must converge, not accumulate"
    )
    assert provenance_violations(graph_schema_session) == []


# =======================================================================================
# 3. Retrieval on real data — what is found, what is thrown out, and what it cannot answer
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_the_roster_finds_the_stores_that_stock_it_and_says_so_when_it_cannot(
    graph_schema_session: Any,
) -> None:
    """The organic half of D55, on real inventory: a roster that has to reject somebody.

    Six real storefronts are loaded — 782 products, 8,327 graph writes. Four stock milk
    thistle; two answer a milk-thistle search with whey and protein stacks and must not
    appear. That exclusion is the whole reason the corpus was collected whole rather than
    sampled by category: a graph in which almost everything matches proves nothing about
    matching.

    Then the same graph is asked something ten supplement shops have no business answering.
    **The honest answer is not an empty vector search**, and this test says so with numbers.
    Measured on exactly this subset with the default ``lexical`` provider:

    ==========================================================  ===============  ==========
    query                                                       best raw cosine  top shop
    ==========================================================  ===============  ==========
    ``milk thistle silymarin liver support extract``            0.3995           oregonswildharvest
    ``24 inch 4k computer monitor with usb-c``                  0.2734           oregonswildharvest
    ``diesel engine turbocharger rebuild kit for a tractor``    0.1719           toniiq
    ==========================================================  ===============  ==========

    The monitor query scores 0.2734 against a catalogue containing no monitors, which is
    within a whisker of the milk-thistle query's own fourth-placed shop at 0.2984. A single
    global cosine threshold therefore does **not** separate answerable from unanswerable
    here, and pretending otherwise would be the more comfortable lie to tell. That
    is not a defect being hidden; it is what the provider's own docstring says it is (D56:
    "there are no semantics in this file", it compares surfaces), and it is precisely why
    R19 makes hard constraints **eligibility filters rather than score terms**. So the
    refusal is asserted where the refusal actually lives: a constraint the catalogue cannot
    satisfy empties the roster outright, with a positive control so ``[]`` cannot be the
    filter simply breaking everything.

    Prices are asserted as a *spread*, never as a dollar amount: this is a point-in-time
    snapshot of other companies' catalogues and every number in it has been drifting since
    the day it was taken.
    """
    _load(graph_schema_session, ROSTER_HOSTS)

    # -- what the corpus can answer -----------------------------------------------------
    roster = candidate_shops(graph_schema_session, query_text=LIVER_QUERY, limit=10)
    found = {shop.store_id for shop in roster}
    assert found == set(LIVER_STOCKED), (
        f"expected exactly the stores that stock it; got {sorted(found)}"
    )
    for host in LIVER_EMPTY:
        assert host not in found, f"{host} stocks no liver support and must be filtered out"

    priced = [shop for shop in roster if shop.lowest_price is not None]
    assert len(priced) >= 3, "a shortlist needs prices the platform actually observed"
    cheapest = min(shop.lowest_price for shop in priced)
    dearest = max(shop.lowest_price for shop in priced)
    assert dearest / cheapest > 1.5, (
        f"the corpus was chosen for a legible price band; got {cheapest}-{dearest}"
    )

    for shop in roster:
        assert shop.source_ids, "an unsourced store is off the roster entirely"
        assert shop.product_ids
        assert shop.scored and shop.best_cosine is not None
        for offer in shop.offers:
            assert offer.source_ids, "an unsourced offer costs the shop its price"

    best_real = max(shop.best_cosine or 0.0 for shop in roster)
    assert best_real > 0.25, f"a query the corpus answers must score like one; got {best_real}"

    # -- what it cannot, on the vector path alone ---------------------------------------
    noise = candidate_shops(graph_schema_session, query_text=OFF_CORPUS_QUERY, limit=10)
    best_noise = max((shop.best_cosine or 0.0 for shop in noise), default=0.0)
    assert best_noise < best_real, (
        f"an unanswerable query must not outscore an answerable one: {best_noise} vs {best_real}"
    )
    assert set(shop.store_id for shop in noise) & set(LIVER_EMPTY), (
        "the shops a nonsense query returns are arbitrary — including the two that stock "
        "nothing the shopper asked for. That is the vector path declining to refuse, and "
        "it is why the refusal below is a constraint rather than a score."
    )

    # -- what it cannot, said outright (R19) --------------------------------------------
    refused = candidate_shops(
        graph_schema_session,
        query_text=OFF_CORPUS_QUERY,
        category=ABSENT_CATEGORY,
        limit=10,
    )
    assert refused == [], (
        f"no product in ten supplement catalogues is in {ABSENT_CATEGORY!r}; the roster "
        f"must be empty rather than five best guesses"
    )
    assert (
        candidate_shops(graph_schema_session, query_text=OFF_CORPUS_QUERY, brand=ABSENT_BRAND) == []
    )

    # Positive control: the same filter shape, naming something the catalogue really carries,
    # still returns shops. Without it, `[]` above would be evidence that the filter refuses
    # everything rather than evidence that the catalogue carries no turbochargers.
    present = candidate_shops(
        graph_schema_session, query_text=LIVER_QUERY, category=PRESENT_CATEGORY, limit=10
    )
    assert present, f"{PRESENT_CATEGORY!r} is a category this corpus really has"
