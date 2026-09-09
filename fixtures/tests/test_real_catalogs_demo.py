"""Gates on ``fixtures/real-catalogs-demo/`` — the curated nineteen-store demo roster.

What this corpus is, and why a third one exists
-----------------------------------------------
``fixtures/real-catalogs/`` is ten supplement storefronts. It is what the compose demo loaded
for its whole life, and it is why *"a walnut coffee table for the lounge"* came back with liver
capsules: there was no coffee table in it to find. ``fixtures/real-catalogs-broad/`` is 38
storefronts across eleven stocked categories and does hold the coffee tables, but at 17,409
products it does not fit the demo — the exchange's ``catalog`` snapshot is capped at
``composition.MAX_DEPLOYMENT_BYTES`` (4 MiB) across every store, and a snapshot product costs a
flat 955 bytes, so 38 stores would leave each one a window of about 137 products.

This directory is the middle: the ten incumbents plus nine organic storefronts, derived from the
broad corpus by ``scripts/build_demo_corpus.py``.

The one claim this file exists to hold, above all the counts
------------------------------------------------------------
**It is DERIVED, and every store file is byte-identical to the broad corpus's.**
:func:`test_every_store_file_is_the_broad_corpus_file_byte_for_byte` is the assertion; the rest
of the file would still pass over a hand-assembled directory, and that one would not. A recorded
corpus that had been edited on the way in would be a recording of nothing — the replay in
``ingest.adapters.recorded`` reassembles each page from these exact bytes and compares the
result against the digest the *live* fetch took over the whole response.

Three rules, the same three both sibling suites obey
-----------------------------------------------------
1. **Nothing here opens a socket.** This module reads bytes off disk (D3/C9).
2. **No assertion names a dollar amount.** These are other people's catalogues, recorded
   2026-09-08.
3. **Every number below was measured on this corpus**, with the command in the docstring.

Reproducing the counts, without pytest::

    ./.venv/bin/python scripts/build_demo_corpus.py --check     # the derivation is in sync
    python - <<'EOF'
    import json, pathlib
    m = json.load(open(pathlib.Path("fixtures/real-catalogs-demo/collection.json")))
    print(len(m["stores"]), m["totals"]["products"])       # 19 4903
    EOF
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import importlib.util
import json
import re
import socket
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

FIXTURES = Path(__file__).resolve().parents[1]
REPO_ROOT = FIXTURES.parent
CORPUS = FIXTURES / "real-catalogs-demo"
SOURCE = FIXTURES / "real-catalogs-broad"
SUPPLEMENT_CORPUS = FIXTURES / "real-catalogs"
COLLECTION = CORPUS / "collection.json"
README = CORPUS / "README.md"

# ======================================================================================
# Measured on this corpus. `scripts/build_demo_corpus.py` derives it from the broad corpus,
# so any of these moving means either the roster changed or the source did, and both are
# deliberate acts that should have to come here and say so.
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
    "branchfurniture.com": 220,
    "sabai.design": 321,
    "deathwishcoffee.com": 142,
    "vervecoffee.com": 175,
    "nemoequipment.com": 97,
    "hyperlitemountaingear.com": 119,
    "fromourplace.com": 123,
    "fellowproducts.com": 441,
}
RECORDED_TOTAL = 4_903

#: Declared category -> (hosts, products). Five stocked categories, measured.
RECORDED_CATEGORIES = {
    "coffee": (2, 317),
    "furniture": (3, 714),
    "home-kitchen": (2, 564),
    "outdoor": (2, 216),
    "supplements": (10, 3092),
}

# --------------------------------------------------------------------------------------
# BREADTH PROBES — one family per category, in THAT CATEGORY'S OWN VOCABULARY.
#
# The same eleven families ``test_real_catalogs_broad.py`` uses, restated rather than imported
# so this file has no import-time dependency on a sibling test module. They are pinned against
# that file's copy by :func:`test_the_probes_are_the_broad_corpus_probes`, so the two cannot
# drift into scoring two corpora with two different rulers.
#
# Word boundaries, not substrings: ``"table" in title`` matches "Adjustable Laptop Stand".
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

#: What ``_probe_hits`` returns on THIS corpus. Pinned, for the reason the broad suite pins its
#: own: a count that only a human re-reads drifts, and one an assertion reads goes red.
RECORDED_PROBE_HITS = {
    "furniture": 237,
    "coffee": 91,
    "outdoor": 73,
    "home-kitchen": 34,
    "pet": 3,
    "sports": 0,
    "beauty": 0,
    "food": 6,
    "tools": 13,
    "apparel": 27,
    "supplements": 265,
}

#: Distinct products matched by at least one family. Smaller than the sum: one product can
#: answer to two (``deathwishcoffee.com`` sells shirts).
RECORDED_PROBE_MATCHES = 749

#: The same probes on ``fixtures/real-catalogs``, which this roster must beat. Pinned so the
#: discrimination test states a measurement rather than an inequality nobody has seen fail.
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

#: A probe family for a category this roster DECLARES must clear this. Borrowed from the broad
#: suite's ``PROBE_FLOOR`` unchanged: the thinnest declared family here is home-kitchen at 34,
#: so it is not a threshold chosen to be cleared.
#:
#: It is NOT applied in the other direction. ``apparel`` scores 27 on this roster without a
#: single store declared for it, because ``deathwishcoffee.com`` sells fifteen t-shirts and
#: hoodies — merchandise in a real catalogue, not the probes going loose. What IS asserted is
#: the ordering: every declared family outscores every undeclared one. See
#: :func:`test_every_category_this_roster_declares_is_genuinely_stocked`.
PROBE_FLOOR = 15

#: No single STOREFRONT may be this much of the corpus. Measured: ``bulksupplements.com`` is
#: 16.4% of 4,903 products.
#:
#: Deliberately a per-STORE rule and not the broad suite's per-CATEGORY one. That corpus's
#: ``MAX_CATEGORY_SHARE`` of 0.40 is the right question for a research corpus collected to be
#: balanced; this one is a demo roster whose subject is a supplement auction, and supplements
#: are 63.1% of it on purpose. Asserting 0.40 here would be importing a rule from a document
#: with a different job and then having to explain away the failure — which is how a gate stops
#: meaning anything. What matters here is that no ONE store is the corpus, and that every
#: category the roster claims is genuinely stocked, which the probe floor above says.
MAX_STORE_SHARE = 0.25

#: What the whole directory may weigh. Measured: 45,771,805 raw JSONL bytes stored as 5,050,390
#: (9.1x at level 9), 5.11 MB on disk including the README and manifest.
#:
#: **This is 8 MB where ``fixtures/real-catalogs``'s equivalent gate says 5 MB, and the ceiling
#: was moved deliberately rather than inherited.** That corpus is ten supplement storefronts at
#: 2.70 MB; this one is nineteen storefronts including furniture and outdoor catalogues, whose
#: ``body_html`` is far heavier per product — 1.3 KB on disk per product against 0.87 for the
#: supplement stores — so 5 MB is not a ceiling this roster can be held to without dropping
#: stores. 8 MB is roughly a 55% headroom over the measurement, which is the same proportion the
#: 5 MB ceiling gave the corpus it guards.
#:
#: **The cost is stated rather than hidden: this directory duplicates 5.05 MB of store files
#: that already exist in ``fixtures/real-catalogs-broad/``, byte for byte.** That is the price
#: of the design, and the design was chosen for it — the alternative was a host filter inside
#: ``ingest.adapters.recorded``, which would have put roster selection in the code that replays
#: bytes, and the whole argument for a recorded corpus is that the replay is the recording.
MAX_BYTES_ON_DISK = 8 * 1024 * 1024

#: The compression the corpus must still be achieving. Measured 9.06x.
MIN_COMPRESSION = 8.0

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


# --------------------------------------------------------------------------------------
# loading — pure disk reads
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


@dataclass(frozen=True)
class Corpus:
    manifest: dict[str, Any]
    stores: tuple[StoreCorpus, ...]

    def by_host(self, host: str) -> StoreCorpus:
        for store in self.stores:
            if store.host == host:
                return store
        raise KeyError(host)


def _read_jsonl(path: Path) -> tuple[tuple[bytes, ...], tuple[Any, ...]]:
    """Each record's exact bytes alongside its parse. Decompression is not re-serialisation."""
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
        if entry.get("skipped") is not None:
            continue
        files = entry.get("files") or {}
        raw_lines, products = _read_jsonl(directory / files["products"])
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


@pytest.fixture(scope="module")
def builder() -> Any:
    """``scripts/build_demo_corpus.py``, loaded by path — ``scripts/`` is not a package."""
    if str(REPO_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "build_demo_corpus", REPO_ROOT / "scripts" / "build_demo_corpus.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("build_demo_corpus", module)
    spec.loader.exec_module(module)
    return module


def _iter_products(corpus: Corpus) -> Iterator[tuple[StoreCorpus, dict[str, Any]]]:
    for store in corpus.stores:
        for product in store.products:
            yield store, product


@functools.cache
def _word(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)


def _title_matches(product: Mapping[str, Any], terms: Sequence[str]) -> bool:
    title = str(product.get("title") or "")
    return any(_word(term).search(title) for term in terms)


def _probe_hits(corpus: Corpus, probes: Mapping[str, Sequence[str]]) -> dict[str, int]:
    return {
        name: sum(1 for _, p in _iter_products(corpus) if _title_matches(p, terms))
        for name, terms in probes.items()
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------
# it is DERIVED — the assertion the rest of the file cannot make
# --------------------------------------------------------------------------------------


def test_every_store_file_is_the_broad_corpus_file_byte_for_byte(corpus: Corpus) -> None:
    """The claim this directory rests on. Compared as bytes, not as parsed records.

    ``ingest.adapters.recorded`` reassembles each recorded page from these exact bytes and
    checks the result against the digest the live fetch took over the whole response, so a
    record that had been re-serialised on the way in would make the replay a recording of
    something nobody served. Copying is the only handling this corpus's products get, and this
    is where that is checked rather than trusted.
    """
    for store in corpus.stores:
        for key in ("products", "provenance"):
            here = CORPUS / store.entry["files"][key]
            there = SOURCE / store.entry["files"][key]
            assert there.is_file(), f"{store.host}: {there} is not in the source corpus"
            assert _digest(here) == _digest(there), (
                f"{store.host}'s {key} file is not the broad corpus's file. This directory is "
                f"derived; re-run scripts/build_demo_corpus.py rather than editing it."
            )


def test_the_directory_is_exactly_what_the_derivation_produces(builder: Any) -> None:
    """``build_demo_corpus.py --check``, run in process. A drifted corpus is a corpus nobody
    can reproduce, and the roster then quietly becomes whatever is on disk."""
    assert builder.main(["--check"]) == 0, (
        "fixtures/real-catalogs-demo is not what scripts/build_demo_corpus.py produces from "
        "fixtures/real-catalogs-broad; re-run it"
    )


def test_the_roster_the_script_states_is_the_roster_on_disk(corpus: Corpus, builder: Any) -> None:
    """A store file left behind by an earlier roster still replays, so the two must agree."""
    on_disk = {store.host for store in corpus.stores}
    assert on_disk == set(builder.ROSTER), (
        f"the corpus holds {sorted(on_disk)} but scripts/build_demo_corpus.py states "
        f"{sorted(builder.ROSTER)}"
    )
    files = {path.name for path in (CORPUS / "stores").glob("*")}
    expected = {
        f"{host}.{kind}.jsonl.gz" for host in builder.ROSTER for kind in ("products", "provenance")
    }
    assert files == expected, f"stray or missing store files: {sorted(files ^ expected)}"


def test_the_manifest_says_it_was_derived_and_from_what(corpus: Corpus) -> None:
    """A corpus that does not say where it came from will be read as one somebody collected."""
    derivation = corpus.manifest["derivation"]
    assert derivation["derived_by"] == "scripts/build_demo_corpus.py"
    assert derivation["derived_from"] == "fixtures/real-catalogs-broad"
    assert _SHA256_RE.match(derivation["source_collection_sha256"])
    assert derivation["source_collection_sha256"] == _digest(SOURCE / "collection.json"), (
        "the source corpus's manifest has changed since this directory was derived"
    )
    assert corpus.manifest["corpus_version"].endswith("-demo")
    assert set(derivation["organic"]) | set(derivation["incumbents"]) == set(derivation["roster"])
    for host, reason in derivation["organic"].items():
        assert len(reason) > 40, f"{host} was promoted without a stated reason"


def test_loading_the_corpus_opens_no_socket() -> None:
    """D3/C9. Every socket entry point poisoned, then the whole corpus read."""
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the corpus reader opened a socket")

    with (
        mock.patch.object(socket, "socket", refuse),
        mock.patch.object(socket, "create_connection", refuse),
        mock.patch.object(socket, "getaddrinfo", refuse),
    ):
        loaded = _load(CORPUS)
    assert sum(len(store.products) for store in loaded.stores) == RECORDED_TOTAL


# --------------------------------------------------------------------------------------
# the counts
# --------------------------------------------------------------------------------------


def test_the_corpus_and_its_readme_are_present() -> None:
    assert CORPUS.is_dir(), f"{CORPUS} is missing"
    assert COLLECTION.is_file(), f"{COLLECTION} is missing"
    assert README.is_file(), f"{README} is missing"


def test_the_per_store_and_total_product_counts_are_pinned(corpus: Corpus) -> None:
    counts = {store.host: len(store.products) for store in corpus.stores}
    assert counts == RECORDED_COUNTS
    assert sum(counts.values()) == RECORDED_TOTAL
    assert corpus.manifest["totals"]["products"] == RECORDED_TOTAL
    assert corpus.manifest["per_store_counts"] == RECORDED_COUNTS
    for store in corpus.stores:
        assert store.entry["products_recorded"] == counts[store.host]
        assert len(store.provenance) == counts[store.host], (
            f"{store.host}: products and provenance must stay row-aligned"
        )


def test_every_store_contributes_a_catalogue_rather_than_a_shelf(corpus: Corpus) -> None:
    """Nineteen stores, none of them token, and none of them so dominant that it IS the corpus.

    The floor is 50 products, the same one ``fixtures/tests/test_real_catalogs.py`` applies to
    the ten-store corpus, and it was a real constraint on the selection rather than a formality
    it happens to clear: ``wildone.com`` (57) and ``flybyjing.com`` (32) were both candidates in
    the categories this roster widened into. The thinnest store here is ``livemomentous.com``
    at 89, an incumbent, and the thinnest promoted one is ``nemoequipment.com`` at 97.
    """
    counts = {store.host: len(store.products) for store in corpus.stores}
    assert len(counts) == 19, f"the roster is {len(counts)} stores"
    assert min(counts.values()) >= 50, f"a store contributed almost nothing: {counts}"
    total = sum(counts.values())
    share = max(counts.values()) / total
    assert share <= MAX_STORE_SHARE, (
        f"one store is {share:.1%} of the corpus, over {MAX_STORE_SHARE:.0%}: {counts}"
    )


def test_no_catalogue_is_truncated(corpus: Corpus) -> None:
    """A store that hit the collector's page cap has an INCOMPLETE catalogue. None here did."""
    truncated = [store.host for store in corpus.stores if store.entry["truncated"]]
    assert not truncated, f"these catalogues are incomplete: {truncated}"
    assert corpus.manifest["totals"]["stores_truncated"] == 0
    assert corpus.manifest["totals"]["stores_skipped"] == 0


def test_each_declared_category_holds_the_stores_and_products_it_claims(corpus: Corpus) -> None:
    counted: dict[str, tuple[int, int]] = {}
    for store in corpus.stores:
        hosts, products = counted.get(store.category, (0, 0))
        counted[store.category] = (hosts + 1, products + len(store.products))
    assert counted == RECORDED_CATEGORIES
    assert corpus.manifest["totals"]["categories"] == len(RECORDED_CATEGORIES)


def test_the_corpus_stays_small_enough_to_live_in_git(corpus: Corpus) -> None:
    """Measured 5.11 MB against a stated 8 MB ceiling. See :data:`MAX_BYTES_ON_DISK` for why
    that ceiling is not the 5 MB the ten-store corpus is held to, and for what the duplication
    against ``fixtures/real-catalogs-broad/`` costs."""
    total = sum(path.stat().st_size for path in CORPUS.rglob("*") if path.is_file())
    assert total < MAX_BYTES_ON_DISK, f"the corpus has grown to {total / 1e6:.2f} MB"
    assert corpus.manifest["storage"]["compressed"] is True
    raw = corpus.manifest["totals"]["bytes_raw"]
    on_disk = corpus.manifest["totals"]["bytes_on_disk"]
    assert raw / on_disk >= MIN_COMPRESSION, f"compression only achieves {raw / on_disk:.1f}x"


# --------------------------------------------------------------------------------------
# BREADTH — and the proof that this roster answers what the ten-store corpus cannot
# --------------------------------------------------------------------------------------


def test_the_probes_are_the_broad_corpus_probes() -> None:
    """Two corpora scored with two different rulers would be two unrelated numbers."""
    spec = importlib.util.spec_from_file_location(
        "_broad_probes", Path(__file__).with_name("test_real_catalogs_broad.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_broad_probes"] = module
    spec.loader.exec_module(module)
    assert CATEGORY_PROBES == module.CATEGORY_PROBES
    assert PROBE_FLOOR == module.PROBE_FLOOR


def test_every_category_this_roster_declares_is_genuinely_stocked(corpus: Corpus) -> None:
    """The whole point of promoting nine stores, as a number rather than as a claim.

    Measured: furniture 237, supplements 265, coffee 91, outdoor 73, home-kitchen 34 — every
    declared category over a floor of 15, on whole-word matches against the merchant's own
    title.

    Two families score without a store declared for them, and neither is the probes going
    loose. **apparel 27** is merchandise: 15 of them are ``deathwishcoffee.com`` t-shirts and
    hoodies, the rest are three ``vervecoffee.com``, four ``nakednutrition.com``, two
    ``bulksupplements.com``, two ``fellowproducts.com`` and one ``hyperlitemountaingear.com`` —
    real garments in real catalogues, none of them a store this roster promoted for apparel.
    **tools 13** is 12 supplement-store hits on the word ``saw`` inside ``sawdust``-free
    marketing and ``Saw Palmetto``, plus one branchfurniture item; it is below the floor and it
    is the reminder that a word is not a category.

    ``taylorstitch.com`` is the store that would make this ambiguous — 3,805 products, 3,253
    apparel hits, and eighteen titles containing the word "walnut" — and it is deliberately not
    on this roster.
    """
    hits = _probe_hits(corpus, CATEGORY_PROBES)
    assert hits == RECORDED_PROBE_HITS, (
        f"the probe counts moved; recompute README.md's table from these rather than editing "
        f"them to taste: {hits}"
    )
    declared = {store.category for store in corpus.stores}
    assert declared == set(RECORDED_CATEGORIES)
    thin = {name: n for name in declared if (n := hits[name]) < PROBE_FLOOR}
    assert not thin, f"these declared categories are barely stocked: {thin}"
    # Every declared category outscores every undeclared one. That is the anti-looseness
    # statement that survives a store selling merch, and it is the one that would go red if a
    # 3,805-product apparel storefront were quietly added to the roster.
    floor_of_declared = min(hits[name] for name in declared)
    loud = {
        name: hits[name]
        for name in hits
        if name not in declared and hits[name] >= floor_of_declared
    }
    assert not loud, (
        f"an undeclared category scores as high as the thinnest declared one ({floor_of_declared}"
        f"): {loud}. Either a store was promoted without declaring what for, or the probes have "
        f"gone loose."
    )
    matched = {
        (store.host, product["id"])
        for store, product in _iter_products(corpus)
        for terms in CATEGORY_PROBES.values()
        if _title_matches(product, terms)
    }
    assert len(matched) == RECORDED_PROBE_MATCHES


def test_this_roster_answers_the_query_the_ten_store_corpus_cannot(corpus: Corpus) -> None:
    """*"A walnut coffee table for the lounge"* — the sentence this whole change exists for.

    Two halves, and the second is what makes the first mean anything: this corpus holds walnut
    coffee tables, and ``fixtures/real-catalogs`` holds no coffee table of any wood. A gate
    that has never been shown to fail on the corpus it rejects is not a gate.
    """
    tables = [
        (store.host, str(product["title"]))
        for store, product in _iter_products(corpus)
        if _title_matches(product, ("coffee table", "coffee tables"))
    ]
    assert tables, "no coffee table in the demo corpus"
    hosts = {host for host, _ in tables}
    assert "branchfurniture.com" in hosts, (
        f"branchfurniture.com is on this roster because it stocks coffee tables; it now "
        f"contributes none. Coffee tables come from {sorted(hosts)}"
    )
    # WALNUT LIVES IN THE VARIANT TITLES, NOT THE PRODUCT TITLE, and that is the measured fact
    # rather than a convenience. Not one title in this corpus carries both "coffee table" and
    # "walnut"; the finish is a variant option, and
    # `exchange.retrieval.relevance.identity_surface` joins title and brand and nothing else.
    # `fixtures/tests/test_real_catalogs_broad.py` records the same finding on the source
    # corpus, per product. Asserting "walnut in the title" here would pin a sentence that is
    # false about every catalogue in this repository.
    with_walnut_variant = sorted(
        (store.host, str(product["title"]))
        for store, product in _iter_products(corpus)
        if _title_matches(product, ("coffee table", "coffee tables"))
        and any(
            _word("walnut").search(str(variant.get("title") or ""))
            for variant in (product.get("variants") or [])
        )
    )
    assert with_walnut_variant == [
        ("branchfurniture.com", "Coffee Table"),
        ("branchfurniture.com", "Nested Coffee Tables"),
        ("branchfurniture.com", "Nested Coffee Tables"),
        ("floydhome.com", "Lift Off Coffee Table - Expansion Kit"),
        ("floydhome.com", "The Lift Off Coffee Table"),
    ], f"the walnut coffee tables this roster exists to hold: {with_walnut_variant}"
    both_in_title = [
        title
        for _, title in tables
        if _word("walnut").search(title)
    ]
    assert both_in_title == [], (
        f"a title now carries both words, so the demo query no longer needs the variant "
        f"surface to be answered: {both_in_title}"
    )

    incumbent = _probe_hits(_load(SUPPLEMENT_CORPUS), CATEGORY_PROBES)
    assert incumbent == INCUMBENT_PROBE_HITS, (
        f"the ten-store corpus's probe counts moved: {incumbent}"
    )
    assert incumbent["furniture"] == 0, (
        "fixtures/real-catalogs now scores on furniture, so this roster no longer demonstrates "
        "anything it does not"
    )
    for category in ("coffee", "home-kitchen"):
        assert incumbent[category] == 0
    assert incumbent["outdoor"] == 3 < PROBE_FLOOR


def test_the_promoted_stores_are_organic_and_the_sponsored_four_are_unchanged(
    corpus: Corpus, builder: Any
) -> None:
    """D55, as an assertion on the corpus rather than a sentence in a README.

    A scraped shop is an ORGANIC result carrying a pitch the PLATFORM wrote; an in-network shop
    is SPONSORED and buys the right to make its own case. Promoting nine storefronts must not
    make any of them sponsored, and the four that are must stay the four.

    The deployment side of the same claim — no `bid_endpoint`, no discount envelope, no store
    context — is asserted in ``scripts/tests/test_build_demo_deployment.py``, which reads the
    generated documents. This half is about which stores exist at all.
    """
    if str(REPO_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "build_demo_deployment", REPO_ROOT / "scripts" / "build_demo_deployment.py"
    )
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("build_demo_deployment", generator)
    spec.loader.exec_module(generator)

    assert set(generator.HOSTED) == {
        "gaiaherbs.com",
        "toniiq.com",
        "paradiseherbs.com",
        "oregonswildharvest.com",
    }, "the sponsored four moved; D55 says which shops buy a voice and these are they"
    promoted = set(builder.ORGANIC)
    assert promoted & set(generator.HOSTED) == set(), (
        f"{sorted(promoted & set(generator.HOSTED))} were promoted as organic AND run a hosted "
        f"store agent; a store cannot be both halves of the market"
    )
    assert promoted == set(generator.CATALOGUE_ACCURACY), (
        "every promoted store is crawl-only and must carry a crawl-only trust posture"
    )
    assert {store.host for store in corpus.stores} == set(builder.INCUMBENTS) | promoted
