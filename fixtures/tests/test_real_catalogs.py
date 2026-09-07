"""Gates on ``fixtures/real-catalogs/`` — ten real storefronts' ENTIRE catalogues.

Why this suite exists
---------------------
Every catalogue byte in this repository was synthetic until this corpus landed.
``fixtures/catalog/coffee.json`` is a *generator config*: four product families, four products
per store, one uniform attribute template, one category, prices from a band. Entity resolution
and claim extraction had therefore never met an empty ``product_type``, a hundred-variant
product, a tag that is a typo, or two stores describing the same active ingredient in different
words. This corpus is 3,093 products recorded verbatim from ten real Shopify storefronts, and
these tests **pin the mess** so downstream work cannot quietly assume clean input.

Three rules this file obeys, all non-negotiable
-----------------------------------------------
1. **Nothing here opens a socket.** The suite runs offline (D3/C9) and the root conftest arms
   ``pytest-socket``. Collection is a by-hand operation in ``scripts/collect_real_catalogs.py``;
   this module only reads bytes off disk. ``test_loading_the_corpus_opens_no_socket`` proves it
   rather than asserting it.
2. **No assertion names a dollar amount.** This corpus is a POINT-IN-TIME snapshot of other
   companies' catalogues. Prices and inventory move, so every economic assertion here is about
   *shape and relationship* — "the spread is wide", "these stores stock none of this" — never
   "milk thistle costs $5.75". See ``fixtures/real-catalogs/README.md``.
3. **Breadth is asserted, not hoped for.** An earlier draft of this corpus sampled each store
   biased toward the liver-supplement category, which would have built a system able to answer
   exactly one question while deleting the hardest part of retrieval: discriminating relevant
   inventory from a large body of irrelevant inventory. The gates under "breadth" below exist
   to stop the corpus silently narrowing back into a single-category sample. The irrelevant
   stock is not overhead — it is the test.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import re
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

CORPUS = Path(__file__).resolve().parents[1] / "real-catalogs"
# NOT ``manifest.json``: ``fixtures/manifest.json`` is this repository's single
# approval-bearing manifest and ``test_manifest.py`` fails on any second file of that name
# under ``fixtures/``. This one records a collection run, which is a different kind of thing.
COLLECTION = CORPUS / "collection.json"
README = CORPUS / "README.md"

# ======================================================================================
# The measurements, taken on THIS corpus on 2026-09-07. A re-collection changes them and
# is *meant* to fail here: updating these numbers is the deliberate act that stops the
# corpus narrowing, losing a store, or drifting back into one category unnoticed.
# ======================================================================================

RECORDED_COUNTS = {
    "gaiaherbs.com": 111,
    "bulksupplements.com": 805,
    "nutricost.com": 755,
    "oregonswildharvest.com": 189,
    "toniiq.com": 103,
    "doublewoodsupplements.com": 201,
    "purebulk.com": 550,
    "paradiseherbs.com": 90,
    "livemomentous.com": 90,
    "nakednutrition.com": 199,
}
RECORDED_TOTAL = 3093

ALL_HOSTS = tuple(RECORDED_COUNTS)

# Roles are declared in the manifest per store, and they are LIVER-SPECIFIC labels: these two
# stores answer a milk-thistle search with whey and protein stacks. For the protein query the
# roles invert — see ``DEMO_QUERIES``. There is no globally irrelevant store in this corpus,
# which is the whole reason for collecting entire catalogues.
LIVER_NEGATIVE_CONTROLS = ("livemomentous.com", "nakednutrition.com")

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


# --------------------------------------------------------------------------------------
# demo queries — several, deliberately. Liver support is ONE of them, not the organising
# principle. Every count below was measured; each assertion is about shape.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoQuery:
    """A question the corpus can really answer, and the shape of its answer.

    ``terms`` match the merchant-authored *title* only. Marketing prose says "supports liver
    health" on half the supplement internet; matching ``body_html`` would file every
    multivitamin as liver support and make the "stocks none of this" claim worthless.
    """

    name: str
    terms: tuple[str, ...]
    min_products: int  # measured, with headroom
    stores_with: int  # measured exactly
    stores_without: int  # measured exactly
    min_price_spread: float  # ratio between cheapest and dearest store, never a dollar amount


DEMO_QUERIES: tuple[DemoQuery, ...] = (
    # measured 35 products / 8 stores with / 2 without / 4.43x spread
    DemoQuery(
        "liver support", ("milk thistle", "silymarin", "silybum", "liver", "tudca"), 20, 8, 2, 3.0
    ),
    # measured 113 / 6 / 4 / 3.67x — the liver negative controls are among the six that HAVE it
    DemoQuery(
        "protein",
        ("whey", "casein", "protein isolate", "protein powder", "pea protein"),
        60,
        6,
        4,
        2.5,
    ),
    # measured 49 / 8 / 2 / 10.03x
    DemoQuery("creatine", ("creatine",), 25, 8, 2, 3.0),
    # measured 92 / 8 / 2 / 6.06x
    DemoQuery("magnesium", ("magnesium",), 50, 8, 2, 3.0),
    # measured 47 / 7 / 3 / 4.02x
    DemoQuery("collagen", ("collagen",), 25, 7, 3, 2.5),
    # measured 21 / 5 / 5 — half the roster has to be thrown out
    DemoQuery(
        "electrolytes",
        ("electrolyte", "hydration", "potassium chloride", "sodium chloride"),
        10,
        5,
        5,
        2.0,
    ),
    # measured 12 / 2 / 8 — the sparsest: almost the whole roster is rejected
    DemoQuery("probiotics", ("probiotic", "lactobacillus", "bifido"), 6, 2, 8, 1.0),
)

# Categories that must all be genuinely stocked for the corpus to count as broad. These are
# NOT demo queries — they are the proof that the corpus is a catalogue and not a shelf.
BREADTH_PROBES: dict[str, tuple[str, ...]] = {
    "liver support": ("milk thistle", "silymarin", "tudca", "liver"),
    "protein": ("whey", "casein", "protein powder", "pea protein"),
    "creatine": ("creatine",),
    "magnesium": ("magnesium",),
    "collagen": ("collagen",),
    "electrolytes": ("electrolyte", "hydration"),
    "probiotics": ("probiotic", "lactobacillus"),
    "vitamin c": ("vitamin c", "ascorbic acid"),
    "vitamin d": ("vitamin d", "cholecalciferol"),
    "zinc": ("zinc",),
    "turmeric": ("turmeric", "curcumin"),
    "ashwagandha": ("ashwagandha", "withania"),
    "mushrooms": ("reishi", "lion's mane", "cordyceps", "chaga"),
    "omega oils": ("fish oil", "omega-3", "omega 3", "krill"),
    "greens": ("greens", "spirulina", "chlorella"),
    "caffeine": ("caffeine",),
}

# Not a supplement in sight. A real storefront sells merchandise, gift cards and a scale, and a
# corpus without them was filtered by somebody's idea of what the catalogue is about.
NON_SUPPLEMENT_TYPES = (
    "Hat",
    "T-Shirt",
    "Merch",
    "Water Bottles",
    "Shaker Bottle",
    "Gift Card",
    "Gift Cards",
    "Scale",
    "membership",
)


# --------------------------------------------------------------------------------------
# loading — pure disk reads, no network, no import from the collector
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreCorpus:
    """One store's slice of the corpus, as it sits on disk."""

    host: str
    role: str
    entry: dict[str, Any]
    raw_lines: tuple[bytes, ...]
    products: tuple[dict[str, Any], ...]
    provenance: tuple[dict[str, Any], ...]

    @property
    def skipped(self) -> bool:
        return self.entry.get("skipped") is not None


@dataclass(frozen=True)
class Corpus:
    manifest: dict[str, Any]
    stores: tuple[StoreCorpus, ...]

    def by_host(self, host: str) -> StoreCorpus:
        for store in self.stores:
            if store.host == host:
                return store
        raise KeyError(host)

    @property
    def collected(self) -> tuple[StoreCorpus, ...]:
        return tuple(store for store in self.stores if not store.skipped)


def _read_jsonl(path: Path) -> tuple[tuple[bytes, ...], tuple[Any, ...]]:
    """Return each record's exact bytes alongside its parse.

    The bytes matter as much as the parse: a product's digest is taken over the exact bytes the
    storefront served, so the digest check is only meaningful if the line was never
    re-serialised on the way back in. Decompression is not re-serialisation — gzip restores the
    original octets exactly — so the digests survive the corpus being stored compressed.
    """
    blob = path.read_bytes()
    if path.suffix == ".gz":
        blob = gzip.decompress(blob)
    if not blob:
        return (), ()
    lines = tuple(line for line in blob.split(b"\n") if line)
    return lines, tuple(json.loads(line) for line in lines)


@functools.cache
def load_corpus() -> Corpus:
    manifest = json.loads(COLLECTION.read_text(encoding="utf-8"))
    stores: list[StoreCorpus] = []
    for entry in manifest["stores"]:
        files = entry.get("files") or {}
        raw_lines: tuple[bytes, ...] = ()
        products: tuple[Any, ...] = ()
        provenance: tuple[Any, ...] = ()
        if "products" in files:
            raw_lines, products = _read_jsonl(CORPUS / files["products"])
        if "provenance" in files:
            _, provenance = _read_jsonl(CORPUS / files["provenance"])
        stores.append(
            StoreCorpus(
                host=entry["host"],
                role=entry["role"],
                entry=entry,
                raw_lines=raw_lines,
                products=products,
                provenance=provenance,
            )
        )
    return Corpus(manifest=manifest, stores=tuple(stores))


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def _iter_products(corpus: Corpus) -> Iterator[tuple[StoreCorpus, dict[str, Any]]]:
    for store in corpus.collected:
        for product in store.products:
            yield store, product


def _title_matches(product: dict[str, Any], terms: Sequence[str]) -> bool:
    title = str(product.get("title") or "").lower()
    return any(term in title for term in terms)


def _variant_prices(product: dict[str, Any]) -> list[float]:
    prices: list[float] = []
    for variant in product.get("variants") or []:
        raw = variant.get("price")
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            prices.append(value)
    return prices


def _hosts_stocking(corpus: Corpus, terms: Sequence[str]) -> dict[str, int]:
    """How many titles each collected store has for these terms — zeroes included."""
    return {
        store.host: sum(1 for product in store.products if _title_matches(product, terms))
        for store in corpus.collected
    }


def _cheapest_per_store(corpus: Corpus, terms: Sequence[str]) -> dict[str, float]:
    cheapest: dict[str, float] = {}
    for store, product in _iter_products(corpus):
        if not _title_matches(product, terms):
            continue
        prices = _variant_prices(product)
        if prices:
            low = min(prices)
            cheapest[store.host] = min(cheapest.get(store.host, low), low)
    return cheapest


# --------------------------------------------------------------------------------------
# the corpus is present, self-describing, and every digest matches its bytes
# --------------------------------------------------------------------------------------


def test_the_corpus_and_its_readme_are_present() -> None:
    assert CORPUS.is_dir(), f"{CORPUS} is missing — run scripts/collect_real_catalogs.py"
    assert COLLECTION.is_file(), f"{COLLECTION} is missing"
    assert README.is_file(), f"{README} is missing"


def test_the_readme_says_the_corpus_is_a_point_in_time_snapshot() -> None:
    """A reader who does not know this will write a test that rots."""
    text = README.read_text(encoding="utf-8").lower()
    assert "point-in-time" in text or "point in time" in text
    assert "re-collect" in text or "recollect" in text


def test_the_readme_publishes_the_per_store_counts_and_the_truncation_status() -> None:
    """The README is where a human learns whether a catalogue is complete. If it does not
    carry the counts and say whether anything was cut off, it is decoration."""
    lines = README.read_text(encoding="utf-8").splitlines()
    for host, count in RECORDED_COUNTS.items():
        # The count must sit on the SAME LINE as its host. Searching the whole file would let
        # any store's number satisfy any other store's assertion — several counts repeat.
        rows = [line for line in lines if host in line]
        assert rows, f"README does not mention {host}"
        assert any(re.search(rf"(?<!\d){count}(?!\d)", row) for row in rows), (
            f"README does not publish {host}'s count of {count}; its rows say {rows}"
        )
    text = "\n".join(lines).lower()
    assert "truncat" in text, "README never says whether any catalogue is truncated"


def test_the_manifest_describes_the_collection_run(corpus: Corpus) -> None:
    manifest = corpus.manifest
    for key in (
        "corpus_version",
        "collected_at",
        "collector",
        "user_agent",
        "politeness",
        "selection",
        "storage",
        "stores",
        "totals",
    ):
        assert key in manifest, f"manifest is missing {key!r}"
    datetime.fromisoformat(manifest["collected_at"])
    assert "contact:" in manifest["user_agent"], "the bot must be identifiable (C6)"
    assert Path(manifest["collector"]).name == "collect_real_catalogs.py"


def test_the_manifest_declares_that_whole_catalogues_were_taken(corpus: Corpus) -> None:
    """The selection policy is recorded so the corpus cannot be narrowed silently. Anything
    other than 'everything' has to be written down here first."""
    selection = corpus.manifest["selection"]
    assert "entire catalogue" in selection["policy"]
    assert "nothing filtered" in selection["policy"]
    assert selection["dropped"].startswith("exact duplicate product ids")
    assert corpus.manifest["totals"]["duplicates_dropped"] == 0


def test_politeness_is_recorded_and_was_actually_respected(corpus: Corpus) -> None:
    """These are real businesses whose whole catalogues were walked. The rate limit is
    evidence in the manifest, not a claim in a docstring."""
    politeness = corpus.manifest["politeness"]
    assert politeness["robots_txt"] == "fetched and respected per host"
    assert politeness["min_seconds_between_requests_per_host"] >= 2.0
    assert politeness["max_pages_per_host"] >= 1
    assert politeness["max_requests_per_host"] >= 1
    assert "none" in politeness["retries"], "403/404/429 must be recorded, never retried around"
    assert politeness["scope"] == "public catalogue data only"
    # Walking whole catalogues is more requests than sampling, so the budget must be visibly
    # modest: ten catalogues cost fewer requests than a single careless crawler's first minute.
    assert corpus.manifest["totals"]["requests_made"] <= 200


def test_every_store_records_its_robots_decision(corpus: Corpus) -> None:
    """Politeness is evidence, not a claim: the robots fetch is recorded like any other."""
    for store in corpus.stores:
        robots = store.entry["robots"]
        assert robots["url"].endswith("/robots.txt")
        assert isinstance(robots["status"], int)
        datetime.fromisoformat(robots["fetched_at"])
        assert isinstance(robots["products_json_allowed"], bool)
        assert robots["crawl_delay_seconds"] >= 2.0, (
            f"{store.host}: the honoured interval fell below the 2s floor"
        )
        if robots["status"] == 200:
            assert _SHA256_RE.match(robots["sha256"]), f"{store.host}: robots.txt digest malformed"
        if not robots["products_json_allowed"]:
            assert store.skipped, f"{store.host}: robots disallowed the path but it was fetched"
            assert store.entry["skipped"]["reason"]


def test_every_fetch_carries_url_status_instant_and_digest(corpus: Corpus) -> None:
    for store in corpus.stores:
        for fetch in store.entry["fetches"]:
            assert fetch["url"].startswith("https://")
            assert isinstance(fetch["status"], int)
            datetime.fromisoformat(fetch["fetched_at"])
            assert isinstance(fetch["bytes"], int)
            if fetch["status"] == 200:
                assert _SHA256_RE.match(fetch["sha256"]), f"{store.host}: fetch digest malformed"
                assert fetch["bytes"] > 0


def test_every_declared_file_exists_and_its_digest_matches_its_bytes(corpus: Corpus) -> None:
    """The manifest's digests are checked against the bytes on disk, not trusted."""
    checked = 0
    for store in corpus.stores:
        digests = store.entry.get("file_sha256") or {}
        for role, relative in (store.entry.get("files") or {}).items():
            path = CORPUS / relative
            assert path.is_file(), f"{store.host}: declared file {relative} is missing"
            assert role in digests, f"{store.host}: {relative} has no recorded sha256"
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            assert actual == digests[role], f"{store.host}: {relative} does not match its sha256"
            checked += 1
    assert checked >= 2 * len(RECORDED_COUNTS), "the corpus declares too few files"


def test_every_product_is_verbatim_json_with_a_matching_digest(corpus: Corpus) -> None:
    """The recorded digest is over the exact bytes the storefront served for that product.

    This is what makes the corpus evidence rather than decoration: the graph refuses a
    material fact with no ``SUPPORTED_BY -> Source`` edge, ``candidate_shops()`` drops an
    unsourced shop from the roster outright and drops the price of a shop whose offer chain is
    unsourced. Every product here traces to a fetch, byte for byte.
    """
    total = 0
    for store in corpus.collected:
        assert len(store.raw_lines) == len(store.provenance), (
            f"{store.host}: {len(store.raw_lines)} products but "
            f"{len(store.provenance)} provenance rows — the files are not row-aligned"
        )
        for line, product, prov in zip(store.raw_lines, store.products, store.provenance):
            assert hashlib.sha256(line).hexdigest() == prov["sha256"], (
                f"{store.host}: product {prov.get('product_id')} does not match its digest — "
                f"the record was altered after collection"
            )
            assert product["id"] == prov["product_id"]
            assert product["handle"] == prov["handle"]
            total += 1
    assert total == RECORDED_TOTAL


def test_every_product_traces_to_a_recorded_fetch(corpus: Corpus) -> None:
    for store in corpus.collected:
        fetch_digests = {fetch["sha256"] for fetch in store.entry["fetches"]}
        fetch_urls = {fetch["url"] for fetch in store.entry["fetches"]}
        for prov in store.provenance:
            assert prov["source_url"] in fetch_urls, (
                f"{store.host}: product {prov['product_id']} cites a URL that was never fetched"
            )
            assert prov["response_sha256"] in fetch_digests, (
                f"{store.host}: product {prov['product_id']} cites a response nobody recorded"
            )
            assert prov["http_status"] == 200
            datetime.fromisoformat(prov["fetched_at"])
            start, end = prov["byte_span"]
            assert 0 <= start < end, f"{store.host}: nonsensical byte span {prov['byte_span']}"
            assert prov["page"] >= 1


def test_nothing_was_normalised_on_the_way_in(corpus: Corpus) -> None:
    """The mess is the specimen. A record whose bytes are the compact re-serialisation of its
    own parse has been through ``json.dumps`` somewhere, and the digests would then attest to
    this process's output rather than to the storefront's."""
    reserialised = 0
    for store in corpus.collected:
        for line, product in zip(store.raw_lines, store.products):
            if line == json.dumps(product, separators=(",", ":")).encode("utf-8"):
                reserialised += 1
    assert reserialised < RECORDED_TOTAL // 2, (
        f"{reserialised} of {RECORDED_TOTAL} records are byte-identical to their own compact "
        f"re-serialisation — something normalised the corpus on the way in"
    )


def test_loading_the_corpus_opens_no_socket() -> None:
    """Proved, not asserted. ``pytest-socket`` is already armed by the root conftest (D19);
    this pins the loader itself so a future 'just fetch it if it is missing' cannot pass."""
    load_corpus.cache_clear()
    try:

        def _forbidden(*args: object, **kwargs: object) -> object:
            raise AssertionError("the corpus loader opened a socket")

        with (
            mock.patch.object(socket, "socket", _forbidden),
            mock.patch.object(socket, "create_connection", _forbidden),
            mock.patch.object(socket, "getaddrinfo", _forbidden),
        ):
            offline = load_corpus()
        assert offline.stores
        assert sum(len(store.products) for store in offline.collected) == RECORDED_TOTAL
    finally:
        load_corpus.cache_clear()


# --------------------------------------------------------------------------------------
# BREADTH — the gates that stop the corpus narrowing back into one category
# --------------------------------------------------------------------------------------


def test_all_ten_stores_are_accounted_for(corpus: Corpus) -> None:
    """Accounted for, not necessarily collected: a store that robots.txt turned away is
    recorded as skipped with its reason, which is a result and not a hole."""
    assert {store.host for store in corpus.stores} == set(ALL_HOSTS)
    for host in LIVER_NEGATIVE_CONTROLS:
        assert corpus.by_host(host).role == "negative_control"


def test_the_per_store_and_total_product_counts_are_pinned(corpus: Corpus) -> None:
    """The anti-narrowing gate, and the reason it is exact.

    A floor alone would let a re-collection quietly halve a store. These are the counts
    measured on 2026-09-07; a re-collection legitimately changes them, and updating this
    constant is the deliberate act that makes somebody look at the new numbers and say what
    they are. See the re-collection section of the corpus README.
    """
    actual = {store.host: len(store.products) for store in corpus.collected}
    assert actual == RECORDED_COUNTS, (
        "the corpus no longer holds the counts it was recorded with:\n"
        f"  recorded: {RECORDED_COUNTS}\n  on disk:  {actual}"
    )
    assert sum(actual.values()) == RECORDED_TOTAL
    assert corpus.manifest["totals"]["products"] == RECORDED_TOTAL
    assert corpus.manifest["per_store_counts"] == RECORDED_COUNTS


def test_every_store_contributes_a_catalogue_rather_than_a_shelf(corpus: Corpus) -> None:
    """Structural, so it survives a re-collection that legitimately shifts the pinned counts:
    ten stores, none of them token, and none of them so dominant that it *is* the corpus."""
    counts = {store.host: len(store.products) for store in corpus.collected}
    assert len(counts) >= 10, f"only {len(counts)} stores were collected"
    assert min(counts.values()) >= 50, f"a store contributed almost nothing: {counts}"
    total = sum(counts.values())
    assert total >= 2500, f"the corpus holds only {total} products"
    assert max(counts.values()) / total <= 0.40, (
        f"one store is {max(counts.values()) / total:.0%} of the corpus: {counts}"
    )


def test_no_catalogue_is_truncated_and_any_that_were_would_say_so(corpus: Corpus) -> None:
    """A store that hits the page cap has an INCOMPLETE catalogue, and the corpus must say
    so per store rather than presenting a partial catalogue as complete."""
    for store in corpus.collected:
        assert isinstance(store.entry["truncated"], bool)
        assert store.entry["page_cap"] >= 1
        if store.entry["truncated"]:
            assert store.entry["truncation_reason"], (
                f"{store.host} is truncated but does not say why"
            )
            assert "truncat" in README.read_text(encoding="utf-8").lower()
    truncated = [s.host for s in corpus.collected if s.entry["truncated"]]
    assert not truncated, f"these catalogues are incomplete: {truncated}"
    assert corpus.manifest["totals"]["stores_truncated"] == 0


def test_the_corpus_spans_many_product_categories(corpus: Corpus) -> None:
    """The breadth assertion. Sixteen unrelated categories, each genuinely stocked, so a
    matcher has a large body of irrelevant inventory to discriminate against — which is the
    part of retrieval a category-biased corpus silently deletes."""
    hits = {
        name: sum(1 for _, p in _iter_products(corpus) if _title_matches(p, terms))
        for name, terms in BREADTH_PROBES.items()
    }
    thin = {name: n for name, n in hits.items() if n < 8}
    assert not thin, f"these categories are barely stocked, so the corpus is not broad: {thin}"
    assert len(hits) >= 15
    # And they are genuinely different products, not one product answering to many names.
    matched = {
        id(p)
        for _, p in _iter_products(corpus)
        for terms in BREADTH_PROBES.values()
        if _title_matches(p, terms)
    }
    assert len(matched) >= 400, f"only {len(matched)} distinct products across 16 categories"


def test_no_single_category_dominates_the_corpus(corpus: Corpus) -> None:
    """The sharpest anti-narrowing gate, and the one that would have failed the corpus this
    lane replaced. If a re-collection biases the sample toward the demo query again, that
    category's share of the corpus jumps and this fails — you cannot satisfy it by editing a
    pinned count."""
    total = sum(1 for _ in _iter_products(corpus))
    for name, terms in BREADTH_PROBES.items():
        share = sum(1 for _, p in _iter_products(corpus) if _title_matches(p, terms)) / total
        assert share <= 0.15, (
            f"{name!r} is {share:.1%} of the corpus — this is a {name} corpus, not a catalogue"
        )
    liver = sum(
        1 for _, p in _iter_products(corpus) if _title_matches(p, BREADTH_PROBES["liver support"])
    )
    assert liver / total <= 0.05, (
        f"liver support is {liver / total:.1%} of the corpus; it is meant to be one demo "
        f"query among several, not the organising principle"
    )


def test_the_corpus_contains_inventory_nobody_would_have_sampled(corpus: Corpus) -> None:
    """Hats, t-shirts, gift cards, a milligram scale, a leaked internal test product. Nobody
    curating a supplement corpus keeps these, which is exactly why their presence proves the
    catalogues were taken whole."""
    merch = [
        (store.host, product["title"])
        for store, product in _iter_products(corpus)
        if str(product.get("product_type") or "") in NON_SUPPLEMENT_TYPES
    ]
    assert len(merch) >= 15, f"almost no non-supplement inventory: {merch}"
    assert len({host for host, _ in merch}) >= 2, "only one store's merchandise survived"


# --------------------------------------------------------------------------------------
# DEMO QUERIES — several. Shape and relationships only; never a dollar amount.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("query", DEMO_QUERIES, ids=lambda q: q.name)
def test_each_demo_query_has_inventory_to_rank(corpus: Corpus, query: DemoQuery) -> None:
    per_store = _hosts_stocking(corpus, query.terms)
    total = sum(per_store.values())
    assert total >= query.min_products, (
        f"{query.name!r} matches only {total} products across the corpus"
    )
    with_stock = sorted(host for host, n in per_store.items() if n)
    assert len(with_stock) == query.stores_with, (
        f"{query.name!r}: {len(with_stock)} stores stock it, expected {query.stores_with} "
        f"({with_stock})"
    )


@pytest.mark.parametrize("query", DEMO_QUERIES, ids=lambda q: q.name)
def test_each_demo_query_has_stores_that_must_be_thrown_out(
    corpus: Corpus, query: DemoQuery
) -> None:
    """The half of retrieval a category-biased corpus deletes. Every demo query here has
    stores with no relevant inventory at all, so a shortlist that returns everybody is
    visibly wrong."""
    per_store = _hosts_stocking(corpus, query.terms)
    without = sorted(host for host, n in per_store.items() if not n)
    assert len(without) == query.stores_without, (
        f"{query.name!r}: {len(without)} stores stock none of it, "
        f"expected {query.stores_without} ({without})"
    )
    assert without, f"{query.name!r} never has to reject anybody, so it demonstrates nothing"


@pytest.mark.parametrize("query", DEMO_QUERIES, ids=lambda q: q.name)
def test_each_demo_query_spans_a_price_band_worth_ranking(corpus: Corpus, query: DemoQuery) -> None:
    """The demo's whole point is quality-for-price, not discount. Asserted as a RATIO between
    the cheapest and dearest store, never as a dollar amount — the amounts will rot."""
    cheapest = _cheapest_per_store(corpus, query.terms)
    assert len(cheapest) >= 2, f"{query.name!r} is priced at fewer than two stores"
    spread = max(cheapest.values()) / min(cheapest.values())
    assert spread >= query.min_price_spread, (
        f"{query.name!r} entry prices only span {spread:.1f}x across {sorted(cheapest)} — "
        f"that is not a market worth ranking"
    )


def test_the_liver_negative_controls_stock_no_liver_support_anywhere(corpus: Corpus) -> None:
    """Checked across each store's WHOLE catalogue, which is only possible because whole
    catalogues were collected. These two answer a milk-thistle search with whey; a shortlist
    that cannot drop them is not filtering."""
    liver = next(q for q in DEMO_QUERIES if q.name == "liver support")
    for host in LIVER_NEGATIVE_CONTROLS:
        store = corpus.by_host(host)
        if store.skipped:
            pytest.skip(f"{host} was not collected: {store.entry['skipped']['reason']}")
        offenders = [p["title"] for p in store.products if _title_matches(p, liver.terms)]
        assert not offenders, f"{host} is a liver negative control but sells {offenders}"
        assert len(store.products) >= 50, f"{host} contributed too little inventory to reject"


def test_relevance_is_per_query_not_per_store(corpus: Corpus) -> None:
    """There is no globally irrelevant store in this corpus, and that is the payoff of taking
    whole catalogues: the two stores that stock no liver support are among the six that DO
    stock protein, so 'irrelevant' has to be recomputed per question rather than baked into
    the roster."""
    liver = next(q for q in DEMO_QUERIES if q.name == "liver support")
    protein = next(q for q in DEMO_QUERIES if q.name == "protein")
    liver_stock = _hosts_stocking(corpus, liver.terms)
    protein_stock = _hosts_stocking(corpus, protein.terms)
    for host in LIVER_NEGATIVE_CONTROLS:
        assert liver_stock[host] == 0
        assert protein_stock[host] > 0, (
            f"{host} stocks neither liver support nor protein — it is globally irrelevant, "
            f"which makes it a hole in the corpus rather than a control"
        )
    inverted = [h for h in protein_stock if protein_stock[h] == 0 and liver_stock[h] > 0]
    assert inverted, "no store stocks liver support but no protein — the roles never invert"


def test_the_same_active_ingredient_wears_different_titles(corpus: Corpus) -> None:
    """Vocabulary collision, which entity resolution has never had to face: the stores that
    stock milk thistle do not name it the same way."""
    liver = next(q for q in DEMO_QUERIES if q.name == "liver support")
    titles_by_store: dict[str, set[str]] = {}
    for store, product in _iter_products(corpus):
        if _title_matches(product, liver.terms):
            titles_by_store.setdefault(store.host, set()).add(str(product["title"]).strip())
    assert len(titles_by_store) >= 6
    distinct = {title.lower() for titles in titles_by_store.values() for title in titles}
    assert len(distinct) >= len(titles_by_store), "titles are suspiciously uniform"


# --------------------------------------------------------------------------------------
# MESSINESS, pinned — every number below was measured on THIS corpus
# --------------------------------------------------------------------------------------


def test_product_type_is_not_a_taxonomy(corpus: Corpus) -> None:
    """``product_type`` is free text a merchant types, and it shows: dosage forms, marketing
    categories, a flavour (``Chocolate Protein``), merchandise, the word ``Product``, a leaked
    ``Bold Test Product``, and — 43% of the time — the empty string. Code that switches on it
    is broken before it is written."""
    values = [str(p.get("product_type") or "") for _, p in _iter_products(corpus)]
    assert "" in values, "no empty product_type — the corpus lost its most useful defect"
    distinct = {v for v in values if v}
    assert len(distinct) >= 50, f"only {len(distinct)} distinct product types: {sorted(distinct)}"
    empty_share = values.count("") / len(values)
    assert empty_share >= 0.25, f"only {empty_share:.1%} of products have an empty product_type"
    # Measured: two stores leave it empty on every product, two fill it on every product.
    per_store_empty = {
        store.host: sum(1 for p in store.products if not str(p.get("product_type") or ""))
        / len(store.products)
        for store in corpus.collected
        if store.products
    }
    assert any(share == 1.0 for share in per_store_empty.values()), (
        f"no store leaves product_type entirely empty: {per_store_empty}"
    )
    assert any(share == 0.0 for share in per_store_empty.values()), (
        f"no store fills product_type on every product: {per_store_empty}"
    )


def test_tags_carry_operational_junk_and_literal_typos(corpus: Corpus) -> None:
    """Tags are an operational dumping ground: back-in-stock flags, discount plumbing,
    internal SKU-ish strings, app namespaces and misspellings sit in the same list as real
    product facts. Measured on this corpus: 28,671 instances, 3,231 distinct."""
    tags = [str(t) for _, p in _iter_products(corpus) for t in (p.get("tags") or [])]
    assert len(tags) >= 10_000, f"only {len(tags)} tag instances — the corpus is not representative"
    distinct = {t.lower() for t in tags}
    assert len(distinct) >= 1_500, f"only {len(distinct)} distinct tags"
    for marker in ("back_in_stock", "discountable"):
        assert marker in distinct, f"operational tag {marker!r} is missing — were tags cleaned?"
    assert any(t.startswith("__") for t in distinct), "no namespaced app tags (__with:, __ppblock:)"
    assert any(":" in t for t in distinct), "no key:value app tags"
    # Literal typos, present verbatim because nothing was repaired on the way in.
    typos = {"bee ropoole", "bee polon", "beepoll", "beet rook", "beetroon"}
    found = typos & distinct
    assert found, f"none of the known typo tags {sorted(typos)} survived — something cleaned them"
    # Internal SKU-ish strings a taxonomy would never contain.
    assert any(re.match(r"\Aels pw \d+\Z", t) for t in distinct), "no internal SKU-ish tags"


def test_body_html_and_tags_are_inversely_rich(corpus: Corpus) -> None:
    """Extraction cannot assume a single rich field. purebulk writes ~4,600 characters of
    prose and tags almost nothing; gaiaherbs writes ~140 characters and carries ~15 tags;
    paradiseherbs uses no tags at all. The naive 'read body_html' extractor returns almost
    nothing for a third of the corpus, and 'read the tags' returns nothing for another store."""
    profile: dict[str, tuple[float, float]] = {}
    for store in corpus.collected:
        if not store.products:
            continue
        bodies = [len(str(p.get("body_html") or "")) for p in store.products]
        tag_counts = [len(p.get("tags") or []) for p in store.products]
        profile[store.host] = (sum(bodies) / len(bodies), sum(tag_counts) / len(tag_counts))
    assert len(profile) >= 10
    prose_heavy = [h for h, (body, tags) in profile.items() if body >= 2000 and tags <= 2]
    tag_heavy = [h for h, (body, tags) in profile.items() if tags >= 12 and body <= 600]
    assert prose_heavy, f"no prose-heavy, tag-poor store in {profile}"
    assert tag_heavy, f"no tag-heavy, prose-poor store in {profile}"
    untagged = [h for h, (_, tags) in profile.items() if tags == 0.0]
    assert untagged, f"every store tags something; the zero-tag extreme is gone: {profile}"
    mean_bodies = [body for body, _ in profile.values()]
    ratio = max(mean_bodies) / max(min(mean_bodies), 1.0)
    assert ratio >= 5.0, f"body_html richness only varies {ratio:.1f}x across stores"


def test_a_single_product_can_carry_many_variants_and_many_images(corpus: Corpus) -> None:
    """One product is not one offer. Measured: the deepest carries 100 variants, the richest
    81 images, and 1,440 of 3,093 products have exactly one variant. Code that reads
    ``variants[0]`` picks an arbitrary size at an arbitrary price, and per-product image loops
    are not free."""
    max_variants = max(len(p.get("variants") or []) for _, p in _iter_products(corpus))
    max_images = max(len(p.get("images") or []) for _, p in _iter_products(corpus))
    assert max_variants >= 50, f"deepest product has only {max_variants} variants"
    assert max_images >= 40, f"richest product has only {max_images} images"
    singles = sum(1 for _, p in _iter_products(corpus) if len(p.get("variants") or []) == 1)
    total = sum(1 for _ in _iter_products(corpus))
    assert 0 < singles < total, "variant depth does not vary across the corpus"
    assert singles / total >= 0.20, "almost nothing is a single-variant product — implausible"


def test_compare_at_price_is_present_natively(corpus: Corpus) -> None:
    """A discount signal already exists in the raw catalogue, on the merchant's own terms.
    Measured: 653 of 9,667 variants. It is null the other 93% of the time, so it is not a
    field anything may rely on."""
    values = [
        v.get("compare_at_price")
        for _, p in _iter_products(corpus)
        for v in (p.get("variants") or [])
    ]
    present = [v for v in values if v not in (None, "")]
    assert present, "no compare_at_price anywhere"
    assert len(present) < len(values) // 2, "compare_at_price is usually set — implausible"


def test_prices_are_strings_and_stock_is_mixed(corpus: Corpus) -> None:
    """Shopify serves prices as decimal STRINGS. Anything doing arithmetic on the raw field
    without a conversion is a bug, and the corpus makes that unavoidable to notice."""
    raw_prices = [
        v.get("price") for _, p in _iter_products(corpus) for v in (p.get("variants") or [])
    ]
    assert len(raw_prices) >= 5_000
    assert all(isinstance(v, str) for v in raw_prices if v is not None)
    availability = [
        bool(v.get("available"))
        for _, p in _iter_products(corpus)
        for v in (p.get("variants") or [])
    ]
    assert set(availability) == {True, False}, "every variant is available — not a real catalogue"
    unavailable = availability.count(False) / len(availability)
    assert unavailable >= 0.10, f"only {unavailable:.1%} of variants are out of stock"


def test_the_corpus_stays_small_enough_to_live_in_git(corpus: Corpus) -> None:
    """A guard on the thing that would otherwise creep. Measured: 20.4 MB of JSON stored as
    2.70 MB of gzip. That headroom is for a re-collection finding more inventory — it is NOT
    a budget to be spent by dropping products, which would reintroduce exactly the category
    bias this corpus exists to remove. If it ever fails, compress harder or take fewer
    stores; never take fewer products."""
    total = sum(path.stat().st_size for path in CORPUS.rglob("*") if path.is_file())
    assert total < 5 * 1024 * 1024, f"the corpus has grown to {total / 1e6:.1f} MB"
    assert corpus.manifest["storage"]["compressed"] is True
    raw = corpus.manifest["totals"]["bytes_raw"]
    on_disk = corpus.manifest["totals"]["bytes_on_disk"]
    assert raw / on_disk >= 5.0, f"compression only achieves {raw / on_disk:.1f}x"
