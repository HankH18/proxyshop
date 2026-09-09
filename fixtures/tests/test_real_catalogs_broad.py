"""Gates on ``fixtures/real-catalogs-broad/`` — 38 storefronts across 12 declared categories.

Why this file exists
--------------------
The broad corpus shipped with **zero gates**. All the assertions in
``test_real_catalogs.py`` point at ``fixtures/real-catalogs``, and every test in
``scripts/tests/test_collect_real_catalogs.py`` runs against synthetic storefronts and opens no
collected corpus, so 14 MB of recorded catalogue could have been corrupted, truncated or
silently narrowed and nothing in this repository would have said a word. "Built, tested,
reachable by nothing" — the corpus was the third. (A count of those collector tests used to sit
in this sentence; it was stale within a day, and a number nothing checks is exactly what this
file is about.)

It also exists because the *point* of the broad corpus had no assertion anywhere. The demo's
visible flaw was that ``"a walnut coffee table for the lounge"`` came back with liver capsules,
which is not a retrieval defect: the committed corpus is ten supplement storefronts and there
was no coffee table in it to find. This corpus has the coffee tables. The gate that was supposed
to notice a single-category corpus could not see either fact, because every one of its sixteen
probes was supplement vocabulary — it scored a corpus with zero furniture exactly as broad as
one with 714 furniture products. :func:`test_the_corpus_spans_genuinely_different_categories` is
the replacement, and :func:`test_the_breadth_gate_would_have_failed_the_supplement_corpus` runs
it against ``fixtures/real-catalogs`` and requires it to FAIL. A gate that has never been shown
to discriminate is a gate nobody has measured.

Three rules, the same three the incumbent suite obeys
-----------------------------------------------------
1. **Nothing here opens a socket.** Collection is a by-hand operation in
   ``scripts/collect_real_catalogs.py``; this module reads bytes off disk (D3/C9).
2. **No assertion names a dollar amount.** This is a POINT-IN-TIME SNAPSHOT of other companies'
   catalogues, taken 2026-09-08. Shape and relationships only.
3. **Every number below was measured on this corpus, and the docstring says with what.**

Reproducing the counts, without pytest::

    python - <<'EOF'
    import gzip, json, pathlib
    d = pathlib.Path("fixtures/real-catalogs-broad")
    m = json.load(open(d / "collection.json"))
    per = {s["host"]: s["products_recorded"] for s in m["stores"] if s["skipped"] is None}
    print(len(per), sum(per.values()))          # 38 17409
    EOF
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import re
import socket
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

FIXTURES = Path(__file__).resolve().parents[1]
CORPUS = FIXTURES / "real-catalogs-broad"
COLLECTION = CORPUS / "collection.json"
README = CORPUS / "README.md"

#: The incumbent ten-supplement-store corpus, read here for exactly one purpose: to show that
#: the breadth gate below can tell the two apart. Nothing else in this file touches it.
SUPPLEMENT_CORPUS = FIXTURES / "real-catalogs"

# ======================================================================================
# Measured on this corpus on 2026-09-08. A re-collection changes them and is MEANT to fail
# here: updating these constants is the deliberate act that stops the corpus narrowing,
# losing a store, or drifting back toward one category unnoticed.
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
    "livemomentous.com": 89,
    "nakednutrition.com": 199,
    "floydhome.com": 173,
    "sabai.design": 321,
    "branchfurniture.com": 220,
    "onyxcoffeelab.com": 248,
    "deathwishcoffee.com": 142,
    "vervecoffee.com": 175,
    "counterculturecoffee.com": 176,
    "taylorstitch.com": 3805,
    "marinelayer.com": 2556,
    "allbirds.com": 294,
    "cotopaxi.com": 1432,
    "nemoequipment.com": 97,
    "hyperlitemountaingear.com": 119,
    "rumpl.com": 248,
    "wildone.com": 57,
    "maxbone.com": 96,
    "fablepets.com": 67,
    "pupford.com": 315,
    "fellowproducts.com": 441,
    "fromourplace.com": 123,
    "bridgecitytools.com": 77,
    "flybyjing.com": 32,
    "graza.co": 80,
    "iliabeauty.com": 80,
    "versedskin.com": 76,
    "fringesport.com": 227,
    "titan.fitness": 2564,
    "tenthousand.cc": 76,
}
RECORDED_TOTAL = 17_409
ROSTER_SIZE = 53
RECORDED_PAGES = 90
RECORDED_REQUESTS = 155

#: The 15 roster hosts that produced no catalogue, each with the outcome the collector recorded.
#: These are RESULTS, not holes: a candidate roster that hides the hosts which refused teaches
#: the next person the wrong hit rate. Measured 2026-09-08.
RECORDED_MISSES = {
    "burrow.com": "http_404",
    "polyandbark.com": "http_404",
    "carawayhome.com": "http_404",
    "taylortoolworks.com": "http_404",
    "omsom.com": "http_404",
    "nomadgoods.com": "http_404",
    "peakdesign.com": "http_404",
    "homedepot.com": "http_404",
    "chewy.com": "http_404",
    "thuma.co": "http_404",
    "madeincookware.com": "http_403",
    "youthtothepeople.com": "http_403",
    "industrywest.com": "robots_http_403",
    "bombas.com": "robots_http_429",
    "katzmosestools.com": "robots_transport_error",
}

#: Declared category -> (roster hosts, hosts with inventory, products). Measured 2026-09-08.
RECORDED_CATEGORIES = {
    "apparel": (4, 3, 6655),
    "beauty": (3, 2, 156),
    "coffee": (4, 4, 741),
    "electronics": (2, 0, 0),
    "food": (3, 2, 112),
    "furniture": (7, 3, 714),
    "home-kitchen": (4, 2, 564),
    "outdoor": (4, 4, 1896),
    "pet": (5, 4, 535),
    "sports": (3, 3, 2867),
    "supplements": (10, 10, 3092),
    "tools": (4, 1, 77),
}

# --------------------------------------------------------------------------------------
# BREADTH PROBES — one family per declared category, in THAT CATEGORY'S OWN VOCABULARY.
#
# This is the whole repair. The gate these replace used sixteen probe families that were
# sixteen sub-categories of supplements (creatine, magnesium, collagen, ashwagandha,
# cholecalciferol, withania), so it could not see the single-category corpus it was written to
# catch, and cannot see this one either — apparel is 38.2% of these products and the old gate
# is silent about it. No probe below is supplement vocabulary except the supplement one.
#
# Word boundaries, not substrings. ``"table" in title`` matches "Adjustable Footrest" and
# "Adjustable Laptop Stand", which is how a count of table products in the three furniture
# stores came out at 31 where ``\btables?\b`` finds 24 — the seven extras are those two, their
# two Open Box twins, a portable lamp, "Adjustable Headboard Hardware" and "The Adjustable
# Base". (Corpus-wide the substring finds 204, against 25 whole-word; a comment here once
# called the 31 the corpus-wide figure, which it is not.) Every count in this file is a
# ``\b``-anchored match against the merchant's own title.
# --------------------------------------------------------------------------------------

CATEGORY_PROBES: dict[str, tuple[str, ...]] = {
    "furniture": ("coffee table", "side table", "sofa", "sectional", "desk", "ottoman"),
    "coffee": ("espresso", "coffee beans", "single origin", "cold brew", "roast", "french press"),
    "outdoor": ("tent", "sleeping bag", "backpack", "sleeping pad", "hammock", "daypack"),
    "home-kitchen": ("skillet", "saucepan", "dutch oven", "cookware", "kettle", "cutting board"),
    "pet": ("leash", "collar", "dog bed", "harness", "puppy", "treat"),
    "sports": ("barbell", "dumbbell", "kettlebell", "weight plate", "squat rack", "jump rope"),
    "beauty": ("serum", "moisturizer", "cleanser", "sunscreen", "toner", "lip"),
    "food": ("hot sauce", "olive oil", "chili crisp", "noodle", "seasoning", "spice"),
    "tools": ("chisel", "hand plane", "saw", "clamp", "marking gauge", "sharpening"),
    "apparel": ("shirt", "pant", "sweater", "jacket", "sock", "hoodie", "denim"),
    "supplements": ("milk thistle", "creatine", "magnesium", "collagen", "probiotic", "whey"),
}

#: What :func:`_probe_hits` returns for :data:`CATEGORY_PROBES` on THIS corpus, and on the
#: ten-supplement-store one. Pinned, because the previous round published a set of counts in a
#: docstring and in ``README.md`` that the function does not return — outdoor 333 against 135,
#: sports 580 against 501, apparel 3,984 against 3,253 — and nothing went red. Every probe
#: number in either README is one of these; a re-collection moves them and is MEANT to fail
#: here, the same deliberate act ``RECORDED_COUNTS`` demands.
RECORDED_PROBE_HITS = {
    "furniture": 258,
    "coffee": 149,
    "outdoor": 135,
    "home-kitchen": 35,
    "pet": 132,
    "sports": 501,
    "beauty": 20,
    "food": 24,
    "tools": 24,
    "apparel": 3253,
    "supplements": 274,
}
#: Distinct products matched by at least one probe family. Smaller than the sum of the hits:
#: one product can answer to two families.
RECORDED_PROBE_MATCHES = 4766
#: The same probes on ``fixtures/real-catalogs`` — the corpus the breadth gate must REJECT.
INCUMBENT_PROBE_HITS = {
    "furniture": 0,
    "coffee": 0,
    "outdoor": 3,
    "home-kitchen": 0,
    "pet": 1,
    "sports": 0,
    "beauty": 0,
    "food": 5,
    "tools": 12,
    "apparel": 6,
    "supplements": 265,
}

#: A probe family below this many hits is not a category the corpus can be said to hold.
#: Measured on this corpus, the thinnest family is beauty at 20 and the next two are food and
#: tools at 24, so the floor has real headroom without being decorative. On the
#: ten-supplement-store corpus FIVE families score exactly 0 — see the discrimination test.
PROBE_FLOOR = 15

#: No declared category may hold more than this share of the corpus. Measured: apparel is 38.2%
#: (taylorstitch 3,805 + marinelayer 2,556 + allbirds 294), which is already the thing to watch,
#: so the headroom here is deliberately thin. A single-category corpus scores 100%.
MAX_CATEGORY_SHARE = 0.40
MAX_TOP_THREE_SHARE = 0.80

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


# --------------------------------------------------------------------------------------
# loading — pure disk reads, no network, no import from the collector
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreCorpus:
    host: str
    role: str
    category: str
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
    """Each record's exact bytes alongside its parse.

    The bytes matter as much as the parse: a product's digest is over the exact bytes the
    storefront served, so the digest check is meaningful only if the line was never
    re-serialised on the way back in. Decompression is not re-serialisation.
    """
    blob = path.read_bytes()
    if path.suffix == ".gz":
        blob = gzip.decompress(blob)
    if not blob:
        return (), ()
    lines = tuple(line for line in blob.split(b"\n") if line)
    return lines, tuple(json.loads(line) for line in lines)


def _load(directory: Path) -> Corpus:
    manifest = json.loads((directory / "collection.json").read_text(encoding="utf-8"))
    stores: list[StoreCorpus] = []
    for entry in manifest["stores"]:
        files = entry.get("files") or {}
        raw_lines: tuple[bytes, ...] = ()
        products: tuple[Any, ...] = ()
        provenance: tuple[Any, ...] = ()
        if "products" in files:
            raw_lines, products = _read_jsonl(directory / files["products"])
        if "provenance" in files:
            _, provenance = _read_jsonl(directory / files["provenance"])
        stores.append(
            StoreCorpus(
                host=entry["host"],
                role=entry["role"],
                category=str(entry.get("category") or "uncategorised"),
                entry=entry,
                raw_lines=raw_lines,
                products=products,
                provenance=provenance,
            )
        )
    return Corpus(manifest=manifest, stores=tuple(stores))


@functools.cache
def load_corpus() -> Corpus:
    return _load(CORPUS)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def _iter_products(corpus: Corpus) -> Iterator[tuple[StoreCorpus, dict[str, Any]]]:
    for store in corpus.collected:
        for product in store.products:
            yield store, product


@functools.cache
def _word(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)


def _title_matches(product: Mapping[str, Any], terms: Sequence[str]) -> bool:
    """Whole-word match against the merchant's own title, and only the title.

    Marketing prose says almost anything on half the retail internet, and ``body_html`` would
    file every product as every category. The title is what the merchant chose to call the
    thing, which is also what the exchange's ``identity_surface`` reads.
    """
    title = str(product.get("title") or "")
    return any(_word(term).search(title) for term in terms)


def _probe_hits(corpus: Corpus, probes: Mapping[str, Sequence[str]]) -> dict[str, int]:
    return {
        name: sum(1 for _, p in _iter_products(corpus) if _title_matches(p, terms))
        for name, terms in probes.items()
    }


def _category_shares(
    corpus: Corpus, category_of: Mapping[str, str] | None = None
) -> dict[str, float]:
    """Share of the corpus's products held by each DECLARED category.

    Declared, not inferred from vocabulary — that is the difference between this and the gate it
    replaces. The roster says what category a host was put on the list for, the collector copies
    that onto the store record, and the share is then a fact about the corpus rather than a
    guess made with somebody's word list.
    """
    counts: dict[str, int] = {}
    total = 0
    for store in corpus.collected:
        name = (category_of or {}).get(store.host, store.category)
        counts[name] = counts.get(name, 0) + len(store.products)
        total += len(store.products)
    return {name: n / total for name, n in counts.items()}


# --------------------------------------------------------------------------------------
# present, self-describing, and every digest matches its bytes
# --------------------------------------------------------------------------------------


def test_the_corpus_and_its_readme_are_present() -> None:
    assert CORPUS.is_dir(), f"{CORPUS} is missing"
    assert COLLECTION.is_file(), f"{COLLECTION} is missing"
    assert README.is_file(), f"{README} is missing"


def test_the_manifest_describes_the_collection_run(corpus: Corpus) -> None:
    manifest = corpus.manifest
    for key in (
        "corpus_version",
        "collected_at",
        "collected_span",
        "run",
        "collector",
        "user_agent",
        "politeness",
        "selection",
        "storage",
        "stores",
        "totals",
        "categories",
    ):
        assert key in manifest, f"manifest is missing {key!r}"
    datetime.fromisoformat(manifest["collected_at"])
    assert "contact:" in manifest["user_agent"], "the bot must be identifiable (C6)"
    assert Path(manifest["collector"]).name == "collect_real_catalogs.py"
    assert manifest["run"]["complete"] is True
    assert manifest["run"]["pending"] == []
    assert manifest["run"]["roster_size"] == ROSTER_SIZE


def test_the_manifest_declares_that_whole_catalogues_were_taken(corpus: Corpus) -> None:
    selection = corpus.manifest["selection"]
    assert "entire catalogue" in selection["policy"]
    assert "nothing filtered" in selection["policy"]
    assert corpus.manifest["totals"]["duplicates_dropped"] == 0


def test_every_declared_file_exists_and_its_digest_matches_its_bytes(corpus: Corpus) -> None:
    """The manifest's digests are checked against the bytes on disk, not trusted. Without this,
    a truncated or corrupted 14 MB corpus is indistinguishable from a healthy one."""
    checked = 0
    for store in corpus.stores:
        digests = store.entry.get("file_sha256") or {}
        for role, relative in (store.entry.get("files") or {}).items():
            path = CORPUS / relative
            assert path.is_file(), f"{store.host}: declared file {relative} is missing"
            assert _SHA256_RE.match(digests.get(role, "")), f"{store.host}: {relative} digest"
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            assert actual == digests[role], f"{store.host}: {relative} does not match its sha256"
            checked += 1
    assert checked == 2 * len(RECORDED_COUNTS), f"the corpus declares {checked} files"


def test_no_store_file_on_disk_is_unaccounted_for(corpus: Corpus) -> None:
    """The other direction. A digest check over the manifest's list cannot see a file the
    manifest does not mention, which is what a half-finished re-build leaves behind."""
    declared = {
        (CORPUS / relative).resolve()
        for store in corpus.stores
        for relative in (store.entry.get("files") or {}).values()
    }
    on_disk = {path.resolve() for path in (CORPUS / "stores").iterdir() if path.is_file()}
    assert on_disk == declared, f"undeclared files in stores/: {sorted(on_disk - declared)}"


def test_every_product_is_verbatim_json_with_a_matching_digest(corpus: Corpus) -> None:
    """The recorded digest is over the exact bytes the storefront served for that product. This
    is what makes the corpus evidence rather than decoration: the graph refuses a material fact
    with no ``SUPPORTED_BY -> Source`` edge."""
    total = 0
    for store in corpus.collected:
        assert len(store.raw_lines) == len(store.provenance), (
            f"{store.host}: {len(store.raw_lines)} products but "
            f"{len(store.provenance)} provenance rows — the files are not row-aligned"
        )
        for line, product, prov in zip(
            store.raw_lines, store.products, store.provenance, strict=True
        ):
            assert hashlib.sha256(line).hexdigest() == prov["sha256"], (
                f"{store.host}: product {prov.get('product_id')} does not match its digest — "
                f"the record was altered after collection"
            )
            assert product["id"] == prov["product_id"]
            assert product["handle"] == prov["handle"]
            total += 1
    assert total == RECORDED_TOTAL


def test_every_page_reassembles_byte_for_byte_into_the_response_that_was_served(
    corpus: Corpus,
) -> None:
    """The property that lets the recording be replayed through the LIVE crawl path: the records
    of one page, joined with ``,`` inside ``{"products":[`` … ``]}``, reproduce the whole
    response body the storefront served."""
    pages = 0
    for store in corpus.collected:
        by_page: dict[int, list[bytes]] = {}
        digests: dict[int, str] = {}
        for raw, prov in zip(store.raw_lines, store.provenance, strict=True):
            page = int(prov["page"])
            by_page.setdefault(page, []).append(raw)
            digests.setdefault(page, prov["response_sha256"])
        for page, records in sorted(by_page.items()):
            body = b'{"products":[' + b",".join(records) + b"]}"
            assert hashlib.sha256(body).hexdigest() == digests[page], (
                f"{store.host} page {page} does not reassemble into the response that was "
                f"served; the recording cannot be replayed through the crawl path"
            )
            pages += 1
    assert pages == RECORDED_PAGES


def test_nothing_was_normalised_on_the_way_in(corpus: Corpus) -> None:
    """The mess is the specimen. A record whose bytes are the compact re-serialisation of its
    own parse has been through ``json.dumps`` somewhere."""
    reserialised = sum(
        1
        for store in corpus.collected
        for line, product in zip(store.raw_lines, store.products, strict=True)
        if line == json.dumps(product, separators=(",", ":")).encode("utf-8")
    )
    assert reserialised < RECORDED_TOTAL // 2, (
        f"{reserialised} of {RECORDED_TOTAL} records are byte-identical to their own compact "
        f"re-serialisation — something normalised the corpus on the way in"
    )


def test_loading_the_corpus_opens_no_socket() -> None:
    """Proved, not asserted. ``pytest-socket`` is armed by the root conftest (D19); this pins
    the loader itself so a future "just fetch it if it is missing" cannot pass."""
    load_corpus.cache_clear()
    try:

        def _forbidden(*args: object, **kwargs: object) -> object:
            raise AssertionError("the corpus loader opened a socket")

        with (
            mock.patch.object(socket, "socket", _forbidden),
            mock.patch.object(socket, "create_connection", _forbidden),
            mock.patch.object(socket, "getaddrinfo", _forbidden),
        ):
            offline = _load(CORPUS)
        assert sum(len(store.products) for store in offline.collected) == RECORDED_TOTAL
    finally:
        load_corpus.cache_clear()


# --------------------------------------------------------------------------------------
# the counts, pinned — the anti-narrowing gate
# --------------------------------------------------------------------------------------


def test_the_per_store_and_total_product_counts_are_pinned(corpus: Corpus) -> None:
    """A floor alone would let a re-collection quietly halve a store. These are the counts
    measured on 2026-09-08; a re-collection legitimately changes them, and updating this
    constant is the deliberate act that makes somebody look at the new numbers."""
    actual = {store.host: len(store.products) for store in corpus.collected}
    assert actual == RECORDED_COUNTS, (
        "the corpus no longer holds the counts it was recorded with:\n"
        f"  missing: {sorted(set(RECORDED_COUNTS) - set(actual))}\n"
        f"  extra:   {sorted(set(actual) - set(RECORDED_COUNTS))}\n"
        f"  changed: "
        f"{ {h: (RECORDED_COUNTS.get(h), n) for h, n in actual.items() if RECORDED_COUNTS.get(h) != n} }"
    )
    assert sum(actual.values()) == RECORDED_TOTAL
    assert corpus.manifest["totals"]["products"] == RECORDED_TOTAL
    assert corpus.manifest["per_store_counts"] == RECORDED_COUNTS
    assert corpus.manifest["totals"]["stores_collected"] == len(RECORDED_COUNTS)


def test_every_roster_host_is_accounted_for_including_the_ones_that_refused(
    corpus: Corpus,
) -> None:
    """A skip is a RESULT. 15 of 53 hosts served no catalogue and each one's reason is on the
    record, so "this merchant does not serve the endpoint" can be told apart from "the collector
    broke" — and from a host quietly dropped off the roster."""
    assert len(corpus.stores) == ROSTER_SIZE
    misses = {store.host: store.entry["walk_outcome"] for store in corpus.stores if store.skipped}
    assert misses == RECORDED_MISSES
    assert corpus.manifest["totals"]["stores_skipped"] == len(RECORDED_MISSES)
    for store in corpus.stores:
        if store.skipped:
            assert store.entry["skipped"]["reason"], f"{store.host} skipped without a reason"


def test_the_deliberate_misses_did_not_answer(corpus: Corpus) -> None:
    """``off_platform_control`` hosts are on the roster to be skipped: a probe whose failure
    path looks exactly like its negative result is not a probe. If one of them ever serves a
    catalogue the control has failed open and the roster is wrong until somebody looks."""
    controls = sorted(s.host for s in corpus.stores if s.role == "off_platform_control")
    assert controls == ["chewy.com", "homedepot.com", "thuma.co"]
    assert corpus.manifest["totals"]["off_platform_controls"] == 3
    assert corpus.manifest["totals"]["off_platform_controls_that_answered"] == 0
    for host in controls:
        assert corpus.by_host(host).skipped, f"{host} is a deliberate miss but served a catalogue"


def test_no_catalogue_is_truncated_and_any_that_were_would_say_so(corpus: Corpus) -> None:
    for store in corpus.collected:
        assert isinstance(store.entry["truncated"], bool)
        assert store.entry["page_cap"] >= 1
        if store.entry["truncated"]:
            assert store.entry["truncation_reason"], f"{store.host} is truncated but not why"
    truncated = [s.host for s in corpus.collected if s.entry["truncated"]]
    assert not truncated, f"these catalogues are incomplete: {truncated}"
    assert corpus.manifest["totals"]["stores_truncated"] == 0


def test_the_corpus_stays_small_enough_to_live_in_git(corpus: Corpus) -> None:
    """A guard on the thing that would otherwise creep. Measured: 140,688,214 raw JSONL bytes
    stored as 13,954,284 — 10.1x at level 9, and 14.1 MB on disk including the README. The
    headroom is for a re-collection finding more inventory; it is NOT a budget to be spent by
    dropping products, which would reintroduce the category bias this corpus exists to remove."""
    total = sum(path.stat().st_size for path in CORPUS.rglob("*") if path.is_file())
    assert total < 20 * 1024 * 1024, f"the corpus has grown to {total / 1e6:.1f} MB"
    assert corpus.manifest["storage"]["compressed"] is True
    raw = corpus.manifest["totals"]["bytes_raw"]
    on_disk = corpus.manifest["totals"]["bytes_on_disk"]
    assert raw / on_disk >= 9.0, f"compression only achieves {raw / on_disk:.1f}x"


# --------------------------------------------------------------------------------------
# BREADTH — the gate the old one could not be, and the proof that it discriminates
# --------------------------------------------------------------------------------------


def test_the_corpus_spans_genuinely_different_categories(corpus: Corpus) -> None:
    """Eleven probe families in eleven different categories' own vocabulary, every one of them
    genuinely stocked. The thinnest is beauty at 20 against a floor of 15.

    The per-family counts are PINNED in :data:`RECORDED_PROBE_HITS` rather than described in
    this docstring, because a docstring is where the last set of them went wrong: it published
    outdoor 333, home-kitchen 41, sports 580, beauty 21, apparel 3,984 and more, none of which
    ``_probe_hits`` returns on the corpus in this directory. Numbers that only a human re-reads
    drift; numbers an assertion reads go red. Every count in ``README.md`` beside this gate is
    now the one this test enforces.
    """
    hits = _probe_hits(corpus, CATEGORY_PROBES)
    assert hits == RECORDED_PROBE_HITS, (
        f"the probe counts moved; recompute the README's tables from these rather than "
        f"editing them to taste: {hits}"
    )
    thin = {name: n for name, n in hits.items() if n < PROBE_FLOOR}
    assert not thin, f"these categories are barely stocked, so the corpus is not broad: {thin}"
    assert len(hits) == len(CATEGORY_PROBES)
    matched = {
        (store.host, product["id"])
        for store, product in _iter_products(corpus)
        for terms in CATEGORY_PROBES.values()
        if _title_matches(product, terms)
    }
    assert len(matched) == RECORDED_PROBE_MATCHES, (
        f"{len(matched)} distinct products are matched by at least one family, not "
        f"{RECORDED_PROBE_MATCHES}"
    )


def test_no_single_declared_category_dominates_the_corpus(corpus: Corpus) -> None:
    """The anti-narrowing gate, computed over the category each host was put on the roster for
    rather than over somebody's word list. A corpus that is 100% one category scores 100% here
    and fails, which is the whole thing its predecessor could not do.

    Measured: apparel 38.2%, supplements 17.8%, sports 16.5% — top three 72.5%.
    """
    shares = _category_shares(corpus)
    worst, share = max(shares.items(), key=lambda kv: kv[1])
    assert share <= MAX_CATEGORY_SHARE, (
        f"{worst!r} is {share:.1%} of the corpus — this is a {worst} corpus, not a catalogue: "
        f"{ {k: f'{v:.1%}' for k, v in sorted(shares.items(), key=lambda kv: -kv[1])} }"
    )
    top_three = sum(sorted(shares.values(), reverse=True)[:3])
    assert top_three <= MAX_TOP_THREE_SHARE, (
        f"three categories hold {top_three:.1%} of the corpus; the rest are decoration"
    )
    assert len([name for name, s in shares.items() if s >= 0.01]) >= 8, (
        f"fewer than eight categories reach 1% of the corpus: {shares}"
    )


def test_the_breadth_gate_would_have_failed_the_supplement_corpus() -> None:
    """The measurement that makes the two gates above worth anything.

    Their predecessor, ``test_no_single_category_dominates_the_corpus``, passed on BOTH corpora:
    all sixteen of its probes were supplement vocabulary, so it scored ten supplement
    storefronts exactly as broad as 38 stores across twelve categories. A gate that cannot fail
    on the corpus it was written to reject is not a gate, and nothing about that is fixable by
    changing its threshold.

    So the probes and the share rule are run against ``fixtures/real-catalogs`` here and are
    required to FAIL it. Measured on the ten-store corpus and pinned in
    :data:`INCUMBENT_PROBE_HITS`: **five** families score exactly 0 — beauty, coffee, furniture,
    home-kitchen and sports — and **ten of the eleven** fall below the floor of 15, the only
    exception being supplements at 265. Supplements is also 100% of its declared categories.

    The count of empty families was published as four here and in ``README.md``, with sports
    left out of a list it belongs in; both are now read off the pinned dict.
    """
    incumbent = _load(SUPPLEMENT_CORPUS)
    hits = _probe_hits(incumbent, CATEGORY_PROBES)
    assert hits == INCUMBENT_PROBE_HITS, (
        f"the supplement corpus's probe counts moved; the README's discrimination table is "
        f"computed from these: {hits}"
    )
    empty = sorted(name for name, n in hits.items() if n == 0)
    assert empty == ["beauty", "coffee", "furniture", "home-kitchen", "sports"], (
        f"the probes find inventory in categories the supplement corpus does not stock: {hits}"
    )
    thin = {name: n for name, n in hits.items() if n < PROBE_FLOOR}
    assert len(thin) == 10 and set(hits) - set(thin) == {"supplements"}, (
        f"{len(thin)} of {len(hits)} probe families fall below the floor on a corpus of ten "
        f"supplement storefronts; this gate cannot tell the two corpora apart: {hits}"
    )

    # ...and the share rule. That manifest predates the `categories` block, so the category
    # comes from the roster file the collector reads, which is where it comes from anyway.
    roster = (SUPPLEMENT_CORPUS / "incumbent-hosts.txt").read_text(encoding="utf-8")
    declared = {
        line.split()[0]: line.split()[1]
        for line in roster.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    shares = _category_shares(incumbent, declared)
    assert shares == {"supplements": pytest.approx(1.0)}, shares
    assert max(shares.values()) > MAX_CATEGORY_SHARE, (
        "a corpus that is 100% supplements must fail the share ceiling this corpus passes"
    )


def test_the_declared_categories_agree_with_the_products_on_disk(corpus: Corpus) -> None:
    """The ``categories`` block is what a reader consults instead of recomputing, so it has to
    agree with the files. Electronics is the interesting row: both candidates answered 404, so
    the category is on the roster with zero products — recorded rather than quietly dropped."""
    declared = corpus.manifest["categories"]
    assert set(declared) == set(RECORDED_CATEGORIES)
    for name, (roster, stocked, products) in RECORDED_CATEGORIES.items():
        bucket = declared[name]
        assert len(bucket["stores"]) == roster, f"{name}: roster hosts"
        assert len(bucket["stores_with_inventory"]) == stocked, f"{name}: stocked hosts"
        assert bucket["products"] == products, f"{name}: product count"
        counted = sum(len(store.products) for store in corpus.collected if store.category == name)
        assert counted == products, f"{name}: manifest says {products}, files hold {counted}"
    assert corpus.manifest["totals"]["categories"] == sum(
        1 for _, stocked, _ in RECORDED_CATEGORIES.values() if stocked
    )
    assert declared["electronics"]["products"] == 0, "electronics is the recorded empty category"


# --------------------------------------------------------------------------------------
# the query this corpus was collected for
# --------------------------------------------------------------------------------------


def test_the_corpus_can_answer_the_query_the_supplement_corpus_could_not(
    corpus: Corpus,
) -> None:
    """``"a walnut coffee table for the lounge"``. The committed supplement corpus answered it
    with four liver capsules because there was no coffee table on disk to find; there are six
    here, in two furniture stores, and 25 table products in all.

    Measured 2026-09-08 with whole-word matching. 24 of the 25 tables are in the three furniture
    stores; the 25th is nemoequipment.com's ``Moonlander™ Dual-Height Camp Table``, which is a
    camp table in the outdoor category and a good reminder that a word is not a category.
    """
    tables = [
        (s.host, p["title"])
        for s, p in _iter_products(corpus)
        if _title_matches(p, ("table", "tables"))
    ]
    assert len(tables) == 25, f"{len(tables)} table products: {tables}"
    in_furniture = [host for host, _ in tables if corpus.by_host(host).category == "furniture"]
    assert len(in_furniture) == 24
    assert sorted(set(in_furniture)) == [
        "branchfurniture.com",
        "floydhome.com",
        "sabai.design",
    ]

    coffee_tables = [
        (s.host, p["title"])
        for s, p in _iter_products(corpus)
        if _title_matches(p, ("coffee table", "coffee tables"))
    ]
    assert len(coffee_tables) == 6, f"{len(coffee_tables)} coffee tables: {coffee_tables}"
    assert sorted({host for host, _ in coffee_tables}) == [
        "branchfurniture.com",
        "floydhome.com",
    ]


def test_the_walnut_answer_exists_on_disk_and_the_identity_surface_cannot_see_it(
    corpus: Corpus,
) -> None:
    """The finding this corpus is actually worth, and it is not the one the README used to make.

    The README said "no coffee table here is made of walnut" and built a story about a hard
    discrimination on it. That is false. Measured: five of the six products whose title names a
    coffee table sell a **walnut** finish — floydhome's ``The Lift Off Coffee Table`` (12
    walnut variants, each a size crossed with a finish) and its ``Lift Off Coffee Table -
    Expansion Kit`` (4), branchfurniture's two ``Nested Coffee Tables`` (3 and 2) and its
    ``Coffee Table`` (``Walnut/White``, ``Walnut/Charcoal``). The sixth is floydhome's
    ``Serviceability - Coffee Table``, a service line with no variants at all.

    An earlier draft of this docstring credited ``The Lift Off Coffee Table`` with the variant
    title ``Walnut / Black``, which belongs to the Expansion Kit, and left the Expansion Kit
    out of the five it was counting.

    What is true, and harder, is where the word sits. **Not one title in this corpus contains
    both "coffee table" and "walnut."** The finish lives in variant titles, and
    ``exchange.retrieval.relevance.identity_surface`` joins ``title`` and ``brand`` and nothing
    else. Read it by name (``grep -n 'def identity_surface'``), not by line range: an earlier
    draft of this docstring cited a thirteen-line window in that file, the function was not in
    it, and the function moved another fifty lines while this sentence was being written. So
    the literal query has a correct
    answer sitting in the corpus that title-and-brand retrieval cannot reach, while eighteen
    taylorstitch products DO say "walnut" in the title (fourteen
    garments in a colourway called Walnut, four pieces of walnut-wood homeware) and are exactly
    what a title matcher will return instead.

    This is an observation about the corpus, recorded where the fact lives. Nothing in
    ``apps/exchange`` is changed by it.
    """
    coffee_tables = [
        (store, product)
        for store, product in _iter_products(corpus)
        if _title_matches(product, ("coffee table", "coffee tables"))
    ]
    with_walnut_variant = sorted(
        (store.host, product["title"], count)
        for store, product in coffee_tables
        if (
            count := sum(
                1
                for variant in (product.get("variants") or [])
                if _word("walnut").search(str(variant.get("title") or ""))
            )
        )
    )
    # Pinned per product, not just counted, because the per-product numbers are quoted in this
    # file's README and a count of five cannot catch them being attributed to the wrong product
    # — which is how ``Walnut / Black``, an Expansion Kit variant, ended up described as a
    # variant of ``The Lift Off Coffee Table``.
    assert with_walnut_variant == [
        ("branchfurniture.com", "Coffee Table", 2),
        ("branchfurniture.com", "Nested Coffee Tables", 2),
        ("branchfurniture.com", "Nested Coffee Tables", 3),
        ("floydhome.com", "Lift Off Coffee Table - Expansion Kit", 4),
        ("floydhome.com", "The Lift Off Coffee Table", 12),
    ], (
        f"walnut coffee tables are the answer this corpus was collected to hold: {with_walnut_variant}"
    )

    both_in_title = [
        (store.host, product["title"])
        for store, product in _iter_products(corpus)
        if _title_matches(product, ("coffee table", "coffee tables"))
        and _title_matches(product, ("walnut",))
    ]
    assert both_in_title == [], (
        f"a title now carries both words, so the finding this test records — that the answer is "
        f"reachable only through variant titles — is no longer true: {both_in_title}"
    )

    walnut_titles = [
        (store.host, product["title"])
        for store, product in _iter_products(corpus)
        if _title_matches(product, ("walnut",))
    ]
    assert len(walnut_titles) == 20
    taylorstitch = [title for host, title in walnut_titles if host == "taylorstitch.com"]
    assert len(taylorstitch) == 18, "taylorstitch is the distractor set nobody designed"
    homeware = [t for t in taylorstitch if _title_matches({"title": t}, ("board", "tray"))]
    assert len(homeware) == 4, (
        f"'walnut is a clothing colour in this corpus' overstates it: {homeware} are walnut "
        f"WOOD homeware, and only the other {len(taylorstitch) - len(homeware)} are garments"
    )


# --------------------------------------------------------------------------------------
# politeness — recomputed from the artifact, never read off it
# --------------------------------------------------------------------------------------


def recorded_requests(manifest: Mapping[str, Any]) -> int:
    """The requests this collection can account for, recomputed from ``stores``.

    This is the recipe ``politeness.request_accounting`` states, run against the artifact that
    states it. Whether the result is a total or a FLOOR is not something this function can tell
    from the presence of ``requests_charged`` — ``build`` writes that field for every store —
    so the artifact says it outright, per store in ``requests_charged_accumulated`` and for the
    collection in ``totals.requests_recorded_is_floor``. See
    ``test_the_manifest_says_which_of_its_request_counts_are_floors``.
    """
    total = 0
    for store in manifest["stores"]:
        if "requests_charged" in store:
            total += int(store["requests_charged"])
            continue
        asked = sum(1 for f in store.get("fetches") or [] if int(f.get("status", 0)) != -1)
        total += asked + (1 if store.get("robots") else 0)
    return total


def test_politeness_is_recorded_and_the_count_can_be_recomputed(corpus: Corpus) -> None:
    """The number the manifest publishes must be readable back out of the manifest.

    ``totals.requests_made`` was ``sum(len(fetches) + 1)``, which counts a page the per-host
    budget REFUSED as a request and invents a robots fetch for a store whose budget ran out
    before robots.txt. It is ``requests_recorded`` now, and this recomputes it.
    """
    politeness = corpus.manifest["politeness"]
    assert politeness["robots_txt"] == "fetched and respected per host"
    assert politeness["min_seconds_between_requests_per_host"] >= 2.0
    assert politeness["min_interval_floor_seconds"] == 2.0
    assert politeness["scope"] == "public catalogue data only"
    assert "request_accounting" in politeness

    counted = recorded_requests(corpus.manifest)
    assert counted == RECORDED_REQUESTS
    assert corpus.manifest["totals"]["requests_recorded"] == counted
    assert "requests_made" not in corpus.manifest["totals"], "the name that overclaimed is gone"
    # 53 whole catalogues for fewer requests than a careless crawler's first ten seconds.
    assert counted <= 250


def test_the_manifest_says_which_of_its_request_counts_are_floors(corpus: Corpus) -> None:
    """``requests_charged`` is on all 53 stores, so its presence cannot mean anything.

    ``build_store`` synthesises the field for every store — ``entry.get("requests_charged",
    _requests_in(entry))`` — so a reader had no way to tell a count carried across every walk
    of a host from one derived from the single walk that happened to survive, while
    ``politeness.request_accounting`` described exactly that distinction. Either the artifact
    records it or the sentence goes; the artifact records it.

    These 53 records were written by collector 2.1.0, which did not carry the number forward,
    so every one of them is a floor and the collection's total is a floor. A single-pass
    collection by 2.2.0 or later is the other case, and
    ``test_the_ten_store_corpus_can_be_rebuilt_in_one_pass_without_a_gate_going_red`` in
    ``scripts/tests/`` is where that one is measured.
    """
    accounting = corpus.manifest["politeness"]["request_accounting"]
    assert "requests_charged_accumulated" in accounting, (
        "the accounting sentence describes a distinction; it must name the field that records it"
    )
    for store in corpus.manifest["stores"]:
        assert "requests_charged_accumulated" in store, store["host"]
        assert isinstance(store["requests_charged_accumulated"], bool), store["host"]
    floors = [s["host"] for s in corpus.manifest["stores"] if not s["requests_charged_accumulated"]]
    assert len(floors) == ROSTER_SIZE, (
        f"these were all fetched by collector 2.1.0, so all {ROSTER_SIZE} counts are floors; "
        f"{len(floors)} say so"
    )
    assert corpus.manifest["totals"]["requests_recorded_is_floor"] is True
    # And the floor is a floor of something: no store may be charged less than its own rows.
    for store in corpus.manifest["stores"]:
        asked = sum(1 for f in store.get("fetches") or [] if int(f.get("status", 0)) != -1)
        asked += 1 if store.get("robots") else 0
        assert int(store["requests_charged"]) >= asked, store["host"]


def test_the_retry_posture_says_what_a_resume_actually_does(corpus: Corpus) -> None:
    """51 of these 53 stores came from an earlier run, so a posture claiming "retries: none"
    full stop would be describing a collection that did not happen. Within the run nothing was
    re-requested — checked below rather than read — and across runs the resume re-walks a host
    whose recorded outcome was retryable, which the posture now says."""
    retries = corpus.manifest["politeness"]["retries"]
    assert "within a run" in retries and "resume" in retries.lower(), retries
    assert len(corpus.manifest["run"]["reused_from_earlier_runs"]) == 51

    for store in corpus.stores:
        urls = [fetch["url"] for fetch in store.entry["fetches"]]
        repeated = {url for url in urls if urls.count(url) > 1}
        assert not repeated, f"{store.host} requested the same URL more than once: {repeated}"


def test_every_store_records_its_robots_decision(corpus: Corpus) -> None:
    """Politeness is evidence, not a claim: the robots fetch is recorded like any other, and the
    interval each host was walked at is recorded with it — which is what lets a corpus assembled
    over several runs be checked against the floor rather than assumed to have honoured it."""
    for store in corpus.stores:
        robots = store.entry["robots"]
        assert robots["url"].endswith("/robots.txt")
        assert isinstance(robots["status"], int)
        datetime.fromisoformat(robots["fetched_at"])
        assert isinstance(robots["products_json_allowed"], bool)
        assert robots["crawl_delay_seconds"] >= 2.0, (
            f"{store.host}: the honoured interval fell below the 2s floor"
        )
        if not robots["products_json_allowed"]:
            assert store.skipped, f"{store.host}: robots refused the path but it was fetched"


def test_a_robots_file_that_could_not_be_read_is_not_recorded_as_a_refusal(
    corpus: Corpus,
) -> None:
    """Three hosts here have no readable robots.txt: industrywest.com answered 403, bombas.com
    answered 429, katzmosestools.com dropped the connection. None of them said no — recording
    our own bad minute as their written policy would drop live storefronts from a future roster
    as having refused."""
    for host in ("industrywest.com", "bombas.com", "katzmosestools.com"):
        store = corpus.by_host(host)
        reason = store.entry["skipped"]["reason"]
        assert "disallow" not in reason.lower(), f"{host} was recorded as a refusal: {reason!r}"
        assert "could not be read" in reason
    disallowed = [s.host for s in corpus.stores if s.entry["walk_outcome"] == "robots_disallowed"]
    assert disallowed == [], f"no merchant in this corpus published a rule against us: {disallowed}"


# --------------------------------------------------------------------------------------
# reachable: the corpus loads through the same loader the incumbent one does
# --------------------------------------------------------------------------------------


def test_the_repositorys_own_loader_reads_this_corpus(corpus: Corpus) -> None:
    """A recorded catalogue nothing can open is 14 MB of comment.

    ``RecordedCorpus`` is the loader that feeds ``RecordedTransport``, which replays a recording
    through the live crawl path. Importing it rather than reimplementing it is the point: a
    second reader would agree with the corpus on the day it was written and drift on the next
    re-collection. This is also the check that a promotion of this corpus would not immediately
    break — it is read by exactly the code that reads the committed one.
    """
    from ingest.adapters.recorded import RecordedCorpus

    loaded = RecordedCorpus.load(CORPUS)
    assert set(loaded.hosts) == set(RECORDED_COUNTS)
    assert loaded.products_recorded == RECORDED_TOTAL
    assert loaded.integrity_problems() == []
    for store in loaded.stores:
        assert store.products_recorded == RECORDED_COUNTS[store.host]
        assert sum(page.products for page in store.pages) == RECORDED_COUNTS[store.host]


# --------------------------------------------------------------------------------------
# the README is a claim surface, so it is gated like one
# --------------------------------------------------------------------------------------


def test_the_readme_publishes_the_counts_and_says_the_corpus_is_a_snapshot() -> None:
    """The README is where a human learns what this corpus is. A number in it that the corpus
    does not hold is worse than no number, because it will be quoted."""
    text = README.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "point-in-time" in lowered or "point in time" in lowered
    assert "truncat" in lowered, "the README never says whether any catalogue is truncated"
    assert f"{RECORDED_TOTAL:,}" in text, "the README does not publish the product total"
    assert str(ROSTER_SIZE) in text
    lines = text.splitlines()
    for host, count in RECORDED_COUNTS.items():
        if count < 1000:
            continue  # the README tabulates categories, and names only the largest stores
        rows = [line for line in lines if host in line]
        assert rows, f"README does not mention {host}"
        assert any(re.search(rf"(?<!\d){count:,}(?!\d)", row) for row in rows), (
            f"README does not publish {host}'s count of {count:,}; its rows say {rows}"
        )


def test_the_readmes_discrimination_table_agrees_with_the_pinned_probe_counts() -> None:
    """The table that was wrong in four places at once, gated against the dicts it summarises.

    It said "probe families scoring 0 | furniture, coffee, home-kitchen, beauty" (four names for
    a set of five — sports was missing), "8 of 11 below the floor" (ten), and "thinnest: beauty,
    21" (twenty). Every one of those came from a docstring nothing executed. This reads the
    table's own two rows and requires them to agree with :data:`INCUMBENT_PROBE_HITS` and
    :data:`RECORDED_PROBE_HITS`, which the gates above require to agree with the bytes on disk.
    """
    rows = [
        line
        for line in README.read_text(encoding="utf-8").splitlines()
        if line.startswith("| probe families")
    ]
    assert len(rows) == 2, f"the discrimination table's shape changed: {rows}"
    zero_row, thin_row = rows

    empty = sorted(name for name, n in INCUMBENT_PROBE_HITS.items() if n == 0)
    assert f"{len(empty)} of {len(INCUMBENT_PROBE_HITS)}" in zero_row, zero_row
    # An EQUALITY, both ways. Requiring only that every zero-scoring family is named let the row
    # name families that score: measured, adding `tools` (12 hits on the incumbent corpus) to
    # this row passed. A count that agrees while the list does not is the same wrong table.
    named = {
        name
        for name in INCUMBENT_PROBE_HITS
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", zero_row)
    }
    assert named == set(empty), (
        f"the README's zero-scoring list is {sorted(named)} but the families that actually "
        f"score 0 on the incumbent corpus are {empty}: {zero_row}"
    )

    thin = {name: n for name, n in INCUMBENT_PROBE_HITS.items() if n < PROBE_FLOOR}
    survivor = sorted(set(INCUMBENT_PROBE_HITS) - set(thin))
    assert f"{len(thin)} of {len(INCUMBENT_PROBE_HITS)}" in thin_row, thin_row
    assert f"floor of {PROBE_FLOOR}" in thin_row, thin_row
    for name in survivor:
        assert name in thin_row and str(INCUMBENT_PROBE_HITS[name]) in thin_row, thin_row

    thinnest, count = min(RECORDED_PROBE_HITS.items(), key=lambda kv: kv[1])
    assert f"{thinnest}, {count}" in thin_row, (
        f"the README calls a different family the thinnest one: {thin_row}"
    )


def test_the_readme_never_asserts_the_walnut_claim_that_was_measured_false() -> None:
    """A named regression guard, because this exact sentence was in the README and was wrong:
    "no coffee table here is made of walnut". Five of the six coffee tables offer a walnut
    finish, and the claim is cheap to retype and expensive to believe.

    The sentence is allowed to appear as a QUOTATION being corrected — the README repeats it in
    order to say it is false, which is more useful to the next reader than deleting it — so the
    gate is on the correction sitting with it, not on the words being absent.
    """
    text = README.read_text(encoding="utf-8").lower()
    claim = "no coffee table here is made of walnut"
    while (where := text.find(claim)) >= 0:
        window = text[max(0, where - 200) : where + 300]
        assert "false" in window, (
            "the README states, without correcting it, a claim measured false on this corpus: "
            f"...{window}..."
        )
        text = text[where + len(claim) :]

    body = README.read_text(encoding="utf-8").lower()
    assert "variant" in body, (
        "the README should say where the walnut answer actually lives — in variant titles, "
        "which is the half of the finding that is true"
    )
