#!/usr/bin/env python
"""Derive ``fixtures/real-catalogs-demo/`` — the curated roster the compose demo runs on.

WHY A THIRD CORPUS DIRECTORY EXISTS, since two already did.

``fixtures/real-catalogs/`` is ten supplement storefronts. It is what the demo has always
loaded, and it is why *"a walnut coffee table for the lounge"* came back with liver capsules:
there is no coffee table in it to find. ``fixtures/real-catalogs-broad/`` is 38 storefronts
across eleven stocked categories and does hold the coffee tables, but it is 17,409 products —
the exchange's ``catalog`` snapshot is capped at ``composition.MAX_DEPLOYMENT_BYTES`` (4 MiB)
across every store, and at 38 stores that ceiling works out to about 137 products each, which
is a window too narrow to hold what a shopper asks any of them for.

So this script cuts the broad corpus down to a **curated roster** — the ten incumbents plus
nine organic storefronts chosen to make the breadth demonstrable — and writes it as a corpus
directory of its own. See :data:`ROSTER` for which nine and why each one.

WHY IT IS A DIRECTORY AND NOT A FLAG ON THE LOADER. ``ingest.scheduler.load_corpus`` already
takes a corpus directory (``PROXYSHOP_RECORDED_CATALOGS``, or the repo-relative default), and
``ingest.adapters.recorded.RecordedCorpus.load`` already reads whichever one it is pointed at.
A host filter inside the load path would be a second selection mechanism living in the code
that replays bytes, and the whole argument for the recorded corpus is that the replay is the
recording and nothing else. Producing a corpus and pointing the existing knob at it changes
nothing in the load path.

THE STORE FILES ARE BYTE-IDENTICAL, and that is checked rather than asserted.
``stores/<host>.products.jsonl.gz`` and ``stores/<host>.provenance.jsonl.gz`` are copied
verbatim out of ``fixtures/real-catalogs-broad/stores/`` and every copy's SHA-256 is compared
against the digest the broad corpus's own manifest records for it. Nothing here parses,
re-serialises, filters or re-orders a product record: the replay reassembles each recorded
page from those exact bytes and compares the result against the digest the live fetch took
over the whole response, so a corpus whose records had been touched would fail to load at all.

WHAT IS REWRITTEN is the manifest, and only the parts that are arithmetic over the roster:
``stores`` (filtered), ``totals``, ``categories``, ``per_store_counts`` and ``run``. Every
per-store record — robots decision, fetch log, digests, byte counts, walk outcome — is the
broad corpus's record, copied. A ``derivation`` block is added naming this script, the source
corpus, its version, and the digest of the source manifest, so a reader can tell this
directory apart from one somebody collected.

Usage::

    ./.venv/bin/python scripts/build_demo_corpus.py
    ./.venv/bin/python scripts/build_demo_corpus.py --check   # rebuild in memory, report drift
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The corpus this one is cut from.
SOURCE = REPO_ROOT / "fixtures" / "real-catalogs-broad"

#: Where the curated roster lands.
OUT = REPO_ROOT / "fixtures" / "real-catalogs-demo"

#: The ten incumbent supplement storefronts, unchanged and in the order
#: ``fixtures/real-catalogs/collection.json`` carries them.
#:
#: They stay because they are what every demo document, every trust posture and every
#: sponsored beat in this repository is built on: the four that run a hosted store agent
#: (``build_demo_deployment.HOSTED``) and the six that do not. Dropping any of them would move
#: the demo's own subject, which is not what a breadth change is for.
INCUMBENTS: tuple[str, ...] = (
    "gaiaherbs.com",
    "bulksupplements.com",
    "nutricost.com",
    "oregonswildharvest.com",
    "toniiq.com",
    "doublewoodsupplements.com",
    "purebulk.com",
    "paradiseherbs.com",
    "livemomentous.com",
    "nakednutrition.com",
)

#: The nine organic storefronts promoted out of the broad corpus, each with the reason.
#:
#: **Every one of these is ORGANIC (D55).** A scraped shop is an organic result carrying a
#: pitch the PLATFORM wrote; only the four hosted agents are sponsored, and this change adds no
#: sponsored store. Nothing here gets a ``bid_endpoint``, a discount envelope or a store agent.
#:
#: Chosen for catalogue quality rather than size, which is a real distinction on this corpus:
#: ``sabai.design`` publishes 110 rows of ``product_type: Component``, 26 ``Swatch``, 6
#: ``Protection Plan`` and 3 ``Gift Cards`` — 145 of its 321 — and ``fellowproducts.com``
#: publishes 102 ``Replacement Part`` and 37 ``Internal`` of its 441. Both are taken anyway and
#: for stated reasons; what those rows cost is a WINDOW problem, and
#: ``build_demo_deployment._store_catalog`` is where it is answered.
ORGANIC: dict[str, str] = {
    # ---- furniture: all three stocked stores in the broad corpus ----------------------
    "floydhome.com": (
        "furniture. 173 products, 148 priced, 38 whole-word furniture-probe hits (sofas, "
        "sectionals, beds, tables). The cleanest of the three: 29 of its rows are "
        "Serviceability or Swatch and the rest are furniture."
    ),
    "branchfurniture.com": (
        "furniture. One of only two storefronts in the entire broad corpus that stocks a "
        "COFFEE TABLE, which is the query this roster exists to answer. 220 products, 207 "
        "priced, 48 furniture-probe hits. Replays to zero products under the pre-fix ingest "
        "response ceiling, which is why that fix and this roster land together."
    ),
    "sabai.design": (
        "furniture. 321 products, 296 priced, 151 furniture-probe hits — the deepest furniture "
        "shelf in the corpus by a wide margin. 145 of its rows are components, swatches, "
        "protection plans and gift cards; taken anyway because the other 176 are sofas, "
        "sectionals, ottomans and side tables and no other store carries that depth."
    ),
    # ---- coffee: two, and the two with the most on-topic titles -----------------------
    "deathwishcoffee.com": (
        "coffee. 142 products, 40 coffee-probe hits — the strongest coffee vocabulary in the "
        "corpus (roast, cold brew, espresso). 1 junk row."
    ),
    "vervecoffee.com": (
        "coffee. 175 products, 15 coffee-probe hits, 1 junk row. Picked over "
        "onyxcoffeelab.com and counterculturecoffee.com, which are equally clean but name "
        "their coffees after farms — 4 probe hits each — so they read as breadth this corpus "
        "cannot demonstrate."
    ),
    # ---- outdoor: two ----------------------------------------------------------------
    "nemoequipment.com": (
        "outdoor. 97 products, 63 outdoor-probe hits and ZERO junk rows — tents, sleeping "
        "bags, sleeping pads. The purest catalogue in the broad corpus."
    ),
    "hyperlitemountaingear.com": (
        "outdoor. 119 products, packs and shelters. Picked over cotopaxi.com (1,432 products, "
        "51 outdoor hits, 627 APPAREL hits and only 711 distinct titles among 1,431 priced "
        "rows) because cotopaxi would have made outdoor the largest category in this corpus "
        "on the strength of colourway duplicates of t-shirts."
    ),
    # ---- home-kitchen: two -----------------------------------------------------------
    "fromourplace.com": (
        "home-kitchen. 123 products, 123 distinct titles, 1 junk row, 14 home-kitchen-probe "
        "hits. The clean one."
    ),
    "fellowproducts.com": (
        "home-kitchen. 441 products, of which 139 are Replacement Part or Internal. Taken as "
        "the SECOND kitchen store rather than the first: home-kitchen is the thinnest probe "
        "family in this roster and fromourplace.com alone scores 14, under the broad corpus's "
        "measured floor of 15. With this store the family scores 32."
    ),
}

#: Every host in the curated roster, incumbents first.
ROSTER: tuple[str, ...] = (*INCUMBENTS, *ORGANIC)

#: The two files each store contributes, and the manifest keys they hang off.
STORE_FILES: tuple[str, ...] = ("products", "provenance")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_manifest() -> dict[str, Any]:
    path = SOURCE / "collection.json"
    if not path.is_file():
        raise SystemExit(f"FATAL: no source corpus at {SOURCE} (expected {path})")
    return json.loads(path.read_text(encoding="utf-8"))


def _picked(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The source's own store records for :data:`ROSTER`, in roster order.

    Roster order rather than manifest order, so the directory reads as the curation it is: the
    ten incumbents, then the nine promoted stores grouped by the category they were promoted
    for. Nothing downstream depends on the order — ``RecordedCorpus.load`` keys by host — but a
    person opening ``collection.json`` should see the decision.
    """
    by_host = {str(store["host"]): store for store in manifest.get("stores") or []}
    missing = [host for host in ROSTER if host not in by_host]
    if missing:
        raise SystemExit(f"FATAL: the source corpus does not carry {missing}")
    skipped = [host for host in ROSTER if by_host[host].get("skipped") is not None]
    if skipped:
        raise SystemExit(
            f"FATAL: {skipped} produced no catalogue in the source corpus, so promoting them "
            f"would put a store with no products on the demo roster"
        )
    return [by_host[host] for host in ROSTER]


def _category_of(stores: list[dict[str, Any]]) -> dict[str, str]:
    return {str(store["host"]): str(store.get("category") or "uncategorised") for store in stores}


def manifest_for(source: dict[str, Any], stores: list[dict[str, Any]]) -> dict[str, Any]:
    """The curated ``collection.json``.

    Everything about a STORE is the source's record, copied. Everything about the COLLECTION
    is arithmetic over the roster, recomputed — a manifest that still claimed 38 stores and
    17,409 products would be a lie the loader would not notice, because ``RecordedCorpus.load``
    reads only the ``stores`` array.
    """
    counts = {str(store["host"]): int(store["products_recorded"]) for store in stores}
    categories: dict[str, dict[str, Any]] = {}
    for store in stores:
        name = str(store.get("category") or "uncategorised")
        row = categories.setdefault(
            name, {"stores": [], "stores_with_inventory": [], "products": 0}
        )
        host = str(store["host"])
        row["stores"].append(host)
        if counts[host]:
            row["stores_with_inventory"].append(host)
        row["products"] += counts[host]

    raw = sum(int(store["bytes"][key]["raw"]) for store in stores for key in STORE_FILES)
    on_disk = sum(int(store["bytes"][key]["on_disk"]) for store in stores for key in STORE_FILES)

    curated = dict(source)
    curated["corpus_version"] = f"{source['corpus_version']}-demo"
    curated["derivation"] = {
        "derived_by": "scripts/build_demo_corpus.py",
        "derived_from": "fixtures/real-catalogs-broad",
        "source_corpus_version": source["corpus_version"],
        "source_collection_sha256": _digest(SOURCE / "collection.json"),
        "policy": (
            "a curated roster cut from the broad corpus. Store files are copied VERBATIM and "
            "their digests checked against the source manifest; no product record is parsed, "
            "filtered, re-ordered or re-serialised. Nothing here was collected: re-run "
            "scripts/build_demo_corpus.py to reproduce this directory byte for byte."
        ),
        "roster": list(ROSTER),
        "incumbents": list(INCUMBENTS),
        "organic": {host: reason for host, reason in ORGANIC.items()},
    }
    curated["run"] = {
        **source["run"],
        "complete": True,
        "roster_size": len(ROSTER),
        "restricted_to": list(ROSTER),
        "pending": [],
        "reused_from_earlier_runs": list(ROSTER),
    }
    curated["stores"] = stores
    curated["totals"] = {
        "stores_collected": len(stores),
        "stores_skipped": 0,
        "stores_truncated": sum(1 for store in stores if store.get("truncated")),
        "products": sum(counts.values()),
        "duplicates_dropped": 0,
        "requests_recorded": sum(int(store.get("requests") or 0) for store in stores),
        "requests_recorded_is_floor": True,
        "bytes_raw": raw,
        "bytes_on_disk": on_disk,
        "categories": len(categories),
        "off_platform_controls": 0,
        "off_platform_controls_that_answered": 0,
    }
    curated["per_store_counts"] = counts
    curated["categories"] = dict(sorted(categories.items()))
    return curated


def readme_for(curated: dict[str, Any]) -> str:
    """``README.md`` for the curated directory. Every number in it comes from ``curated``."""
    totals = curated["totals"]
    lines = [
        "# `fixtures/real-catalogs-demo/` — the curated demo roster",
        "",
        "**Derived, not collected.** Every byte of every store file in `stores/` is a verbatim",
        "copy of the same file in `fixtures/real-catalogs-broad/stores/`, checked by SHA-256",
        "against that corpus's own manifest. Nothing here opened a socket. Reproduce with:",
        "",
        "```",
        "./.venv/bin/python scripts/build_demo_corpus.py",
        "./.venv/bin/python scripts/build_demo_corpus.py --check   # report drift, write nothing",
        "```",
        "",
        f"{totals['stores_collected']} storefronts, {totals['products']:,} products, "
        f"{totals['categories']} categories, {totals['bytes_on_disk'] / 1e6:.2f} MB on disk.",
        "",
        "## Why this directory exists",
        "",
        '`fixtures/real-catalogs/` is ten supplement storefronts, which is why *"a walnut',
        'coffee table for the lounge"* came back with liver capsules — there was no coffee',
        "table in it to find. `fixtures/real-catalogs-broad/` has the coffee tables and 38",
        "stores, and the exchange's `catalog` snapshot is capped at 4 MiB across every store,",
        "so at 38 stores each one's window is about 137 products. This roster is the middle:",
        "the ten incumbents plus nine organic storefronts, wide enough to answer a furniture,",
        "coffee, outdoor or kitchen query and narrow enough that each store's window holds",
        "what a shopper asks it for.",
        "",
        "## The roster",
        "",
        "| host | category | products | role |",
        "| --- | --- | ---: | --- |",
    ]
    counts = curated["per_store_counts"]
    for store in curated["stores"]:
        host = str(store["host"])
        role = "incumbent" if host in INCUMBENTS else "organic (promoted)"
        lines.append(f"| `{host}` | {store.get('category')} | {counts[host]:,} | {role} |")
    lines += [
        "",
        "### Why each promoted store",
        "",
    ]
    for host, reason in ORGANIC.items():
        lines.append(f"* **`{host}`** — {reason}")
    lines += [
        "",
        "## What is NOT here",
        "",
        "**No store promoted here is sponsored.** D55: a scraped shop is an ORGANIC result",
        "carrying a pitch the platform wrote; an in-network shop is SPONSORED and buys the",
        "right to make its own case. The four hosted store agents — `gaiaherbs.com`,",
        "`toniiq.com`, `paradiseherbs.com`, `oregonswildharvest.com` — are the sponsored ones",
        "and stay the only sponsored ones. Nothing promoted here gets a `bid_endpoint`, a",
        "discount envelope or a store agent.",
        "",
        "The gates are `fixtures/tests/test_real_catalogs_demo.py`.",
        "",
    ]
    return "\n".join(lines)


def build() -> tuple[dict[str, Any], str, list[tuple[Path, Path, str]]]:
    """``(collection.json, README.md, [(source file, destination, expected digest)])``."""
    source = _source_manifest()
    stores = _picked(source)
    copies: list[tuple[Path, Path, str]] = []
    for store in stores:
        for key in STORE_FILES:
            relative = str(store["files"][key])
            origin = SOURCE / relative
            if not origin.is_file():
                raise SystemExit(f"FATAL: {store['host']} declares {relative}, which is missing")
            copies.append((origin, OUT / relative, str(store["file_sha256"][key])))
    curated = manifest_for(source, stores)
    return curated, readme_for(curated), copies


def render(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="derive fixtures/real-catalogs-demo/")
    parser.add_argument(
        "--check",
        action="store_true",
        help="rebuild in memory and report drift instead of writing",
    )
    args = parser.parse_args(argv)

    curated, readme, copies = build()
    drifted: list[str] = []

    for origin, destination, expected in copies:
        actual = _digest(origin)
        if actual != expected:
            # The SOURCE is wrong, not the copy. Refusing here rather than propagating it is
            # the whole reason this check exists: a corrupted record would still copy cleanly.
            raise SystemExit(
                f"FATAL: {origin.relative_to(REPO_ROOT)} hashes to {actual}, but the broad "
                f"corpus's manifest records {expected}. The SOURCE corpus is corrupt; this "
                f"script will not copy it."
            )
        if args.check:
            if not destination.is_file() or _digest(destination) != expected:
                drifted.append(str(destination.relative_to(REPO_ROOT)))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, destination)

    for relative, rendered in (("collection.json", render(curated)), ("README.md", readme)):
        path = OUT / relative
        if args.check:
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            if current != rendered:
                drifted.append(str(path.relative_to(REPO_ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")

    # A store file left behind by an earlier roster is a store the loader would still replay,
    # so the roster would silently be whatever the directory holds rather than what ROSTER
    # says. Named individually rather than swept, because deleting by pattern in a script that
    # writes into `fixtures/` is how a corpus gets lost.
    wanted = {destination for _, destination, _ in copies}
    for path in sorted((OUT / "stores").glob("*")) if (OUT / "stores").is_dir() else []:
        if path.is_file() and path not in wanted:
            if args.check:
                drifted.append(f"{path.relative_to(REPO_ROOT)} (not on the roster)")
            else:
                path.unlink()
                print(f"removed {path.relative_to(REPO_ROOT)} — not on the roster")

    if args.check:
        if drifted:
            print(
                "fixtures/real-catalogs-demo differs from what the roster implies:", file=sys.stderr
            )
            for line in drifted:
                print(f"  {line}", file=sys.stderr)
            return 1
        print("fixtures/real-catalogs-demo is in sync with the broad corpus and the roster")
        return 0

    totals = curated["totals"]
    print(
        f"wrote fixtures/real-catalogs-demo: {totals['stores_collected']} stores, "
        f"{totals['products']:,} products, {totals['categories']} categories, "
        f"{totals['bytes_on_disk'] / 1e6:.2f} MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
