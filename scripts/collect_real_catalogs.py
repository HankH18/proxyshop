#!/usr/bin/env python
"""Record a point-in-time snapshot of ten real Shopify storefronts' ENTIRE catalogues.

**Run by hand. Never by the test suite.** The gates in ``fixtures/tests/test_real_catalogs.py``
read the recorded corpus off disk and open no socket (D3/C9); this script is the only thing in
the repository that talks to those hosts, and it is invoked deliberately by a person.

Two phases, and the split is deliberate
---------------------------------------
``fetch``
    Walks ``/products.json?limit=250&page=N`` per host to exhaustion and writes every response
    body **verbatim** into a scratch directory, together with a fetch log. This is the only
    phase that opens a socket, and it should be run exactly once.
``build``
    Reads that scratch directory and derives the committed corpus. It never fetches. Any later
    decision about the corpus's *shape* — compression, which dimensions to trim — is therefore
    a re-run of ``build`` alone and costs those businesses nothing.

::

    .venv/bin/python scripts/collect_real_catalogs.py fetch --raw-dir /tmp/rc-raw
    .venv/bin/python scripts/collect_real_catalogs.py build --raw-dir /tmp/rc-raw \
        --out fixtures/real-catalogs

``all`` runs both in one go.

What is collected: EVERYTHING each store serves
------------------------------------------------
Not a sample, and explicitly not a sample biased toward one product category. A corpus in
which most products match the query proves nothing about matching — the hard part of retrieval
is discriminating relevant inventory from a large body of irrelevant inventory, and the
irrelevant stock is not overhead, it is the test. So the walk paginates until a page comes back
short or empty, and every product on every page is recorded.

Two of the ten stores (livemomentous, nakednutrition) were measured to carry no liver-support
inventory at all. They are kept for the same reason: a shortlist that never has to reject
anybody demonstrates nothing.

Politeness, which is not optional — these are real businesses
-------------------------------------------------------------
* ``robots.txt`` is fetched per host **before anything else** and parsed with ``protego``. A
  host that disallows the path is skipped, and the skip and its reason are recorded. There is
  no override flag, deliberately.
* Every request carries an identifying User-Agent with a contact address (SPEC C6).
* At least ``--min-interval`` seconds (default 2.0) between requests to the same host, and a
  declared ``Crawl-delay`` widens that and never narrows it. Walking a whole catalogue is more
  requests than sampling one, so the gap matters more, not less.
* ``--max-pages`` (default 40, i.e. 10,000 products) is a runaway guard, not a sampling knob.
  A store that hits it has a TRUNCATED catalogue, and both ``collection.json`` and the README say so
  per store rather than presenting a partial catalogue as complete.
* HTTP 403/404/429 is a *recorded outcome*, not a failure to retry around. Nothing retries.
* Public catalogue data only. ``/products.json`` carries no personal data and this script goes
  looking for none.

What is recorded, and why it is shaped this way
-----------------------------------------------
Two gzipped files per store, plus one collection record:

``stores/<host>.products.jsonl.gz``
    Every product, one per line, each line the **exact bytes** the storefront served for that
    product — the byte slice is lifted out of the response with ``raw_decode`` and never
    re-serialised. Nothing is normalised, cleaned or repaired: the mess is the specimen, and
    normalisation belongs downstream in entity resolution.
``collection.json``
    The run itself: politeness settings, every host's robots decision, every HTTP request made,
    the per-store counts, whether any catalogue is truncated, and a digest of each file.
``stores/<host>.provenance.jsonl.gz``
    Row-aligned with the products file. Per product: the URL, the HTTP status, the fetch
    instant, the digest of the whole response, the byte span inside it, and the digest of the
    product's own bytes. The graph refuses a material fact with no ``SUPPORTED_BY -> Source``
    edge and ``candidate_shops()`` drops an unsourced shop from the roster entirely, so a
    corpus that cannot say where a product came from produces a graph that looks full and
    rosters empty.

Gzip because this is a git repository and this JSON compresses 7.6x (measured: 20.4 MB of
catalogue to 2.70 MB on disk, level 9). It is a *storage*
decision: no product is dropped and no field is trimmed, so the corpus stays the whole
catalogue. ``--no-compress`` writes plain ``.jsonl`` for inspection.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USER_AGENT = "ProxyShopBot/0.1 (catalog research; contact: hank.holcomb@challenger.gauntletai.com)"

CORPUS_VERSION = "2.0.0"

# The roster. The last two are DELIBERATE NEGATIVES for a liver-support query: measured, they
# answer a milk-thistle search with protein stacks and whey. They are ordinary stores in every
# other respect, and for a *protein* query the roles invert — which is the point of collecting
# whole catalogues rather than one category.
RELEVANT_HOSTS = (
    "gaiaherbs.com",
    "bulksupplements.com",
    "nutricost.com",
    "oregonswildharvest.com",
    "toniiq.com",
    "doublewoodsupplements.com",
    "purebulk.com",
    "paradiseherbs.com",
)
NEGATIVE_CONTROL_HOSTS = ("livemomentous.com", "nakednutrition.com")

_SAFE_HOST = re.compile(r"\A[a-z0-9.-]+\Z")


# --------------------------------------------------------------------------------------
# politeness
# --------------------------------------------------------------------------------------


@dataclass
class FetchRecord:
    """One HTTP request, recorded whatever its outcome."""

    url: str
    status: int
    fetched_at: str
    sha256: str
    bytes: int
    elapsed_ms: int
    note: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "fetched_at": self.fetched_at,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "elapsed_ms": self.elapsed_ms,
            "note": self.note,
        }


@dataclass
class PolitenessBudget:
    """Per-host rate limit and request cap. There is no way to spend past the cap."""

    min_interval: float
    max_requests: int
    _last: dict[str, float] = field(default_factory=dict)
    _count: dict[str, int] = field(default_factory=dict)
    _host_interval: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def key(host: str) -> str:
        """One bucket per origin server.

        ``gaiaherbs.com`` redirects to ``www.gaiaherbs.com``: robots.txt is requested at the
        bare host and the catalogue pages at the ``www`` one. Keying on the literal hostname
        would give the same machine two independent budgets, so the 2-second gap between the
        robots fetch and the first page would not be enforced at all.
        """
        return host.removeprefix("www.").lower()

    def honour_crawl_delay(self, host: str, delay: float) -> None:
        """A declared ``Crawl-delay`` widens this host's interval and never narrows it."""
        self._host_interval[self.key(host)] = max(self.min_interval, delay)

    def interval_for(self, host: str) -> float:
        return self._host_interval.get(self.key(host), self.min_interval)

    def spend(self, host: str) -> bool:
        """Block until this host may be hit again. Returns False when its budget is gone."""
        host = self.key(host)
        used = self._count.get(host, 0)
        if used >= self.max_requests:
            return False
        last = self._last.get(host)
        if last is not None:
            wait = self.interval_for(host) - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last[host] = time.monotonic()
        self._count[host] = used + 1
        return True

    def spent(self, host: str) -> int:
        return self._count.get(self.key(host), 0)


def fetch(
    client: Any, url: str, budget: PolitenessBudget, host: str
) -> tuple[FetchRecord, bytes] | None:
    """One polite GET. ``None`` means the host's request budget is exhausted."""
    import httpx

    if not budget.spend(host):
        return None
    started = time.monotonic()
    fetched_at = datetime.now(UTC).isoformat()
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:  # a transport failure is an outcome, recorded, not retried
        return (
            FetchRecord(
                url=url,
                status=0,
                fetched_at=fetched_at,
                sha256="",
                bytes=0,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                note=f"{type(exc).__name__}: {exc}",
            ),
            b"",
        )
    body = response.content
    record = FetchRecord(
        url=str(response.url),
        status=response.status_code,
        fetched_at=fetched_at,
        sha256=hashlib.sha256(body).hexdigest() if body else "",
        bytes=len(body),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        note="" if str(response.url) == url else f"redirected from {url}",
    )
    return record, body


# --------------------------------------------------------------------------------------
# phase one: fetch (the only phase that opens a socket)
# --------------------------------------------------------------------------------------


def fetch_store(
    client: Any,
    host: str,
    role: str,
    budget: PolitenessBudget,
    args: argparse.Namespace,
    raw_dir: Path,
) -> dict[str, Any]:
    """Walk one host's whole catalogue into ``raw_dir``. Returns its fetch-log entry."""
    import httpx

    entry: dict[str, Any] = {
        "host": host,
        "role": role,
        "effective_origin": f"https://{host}",
        "robots": {},
        "skipped": None,
        "fetches": [],
        "pages": [],
        "page_cap": args.max_pages,
        "truncated": False,
        "truncation_reason": "",
    }
    store_dir = raw_dir / host
    store_dir.mkdir(parents=True, exist_ok=True)

    def skip(reason: str) -> dict[str, Any]:
        entry["skipped"] = {"reason": reason}
        print(f"  SKIPPED {host}: {reason}")
        return entry

    # ---- robots.txt first, always ----
    robots_url = f"https://{host}/robots.txt"
    fetched = fetch(client, robots_url, budget, host)
    if fetched is None:
        return skip("per-host request budget exhausted before robots.txt")
    robots_record, robots_body = fetched
    (store_dir / "robots.txt").write_bytes(robots_body)
    origin = str(httpx.URL(robots_record.url).copy_with(raw_path=b"/")).rstrip("/")
    entry["effective_origin"] = origin
    effective_host = httpx.URL(robots_record.url).host

    allowed = False
    note = ""
    products_url = f"{origin}/products.json"
    if robots_record.status == 200 and robots_body:
        from protego import Protego

        parser = Protego.parse(robots_body.decode("utf-8", errors="replace"))
        allowed = bool(parser.can_fetch(products_url, USER_AGENT))
        delay = parser.crawl_delay(USER_AGENT)
        if delay:
            note = f"crawl-delay {delay}s declared; honoured as max(delay, --min-interval)"
            budget.honour_crawl_delay(effective_host, float(delay))
    else:
        note = f"robots.txt returned {robots_record.status}"
        # No robots.txt is not consent. An unreadable robots file is a stop, since the
        # alternative is arguing with a file we could not read. RFC 9309 does treat a 404 as
        # unrestricted, and that is the one case we proceed on.
        allowed = robots_record.status == 404
        if allowed:
            note += " (absent robots.txt: RFC 9309 treats 404 as unrestricted)"

    entry["robots"] = {
        "url": robots_record.url,
        "status": robots_record.status,
        "fetched_at": robots_record.fetched_at,
        "sha256": robots_record.sha256,
        "bytes": robots_record.bytes,
        "products_json_allowed": allowed,
        "crawl_delay_seconds": budget.interval_for(effective_host),
        "note": note,
    }
    if not allowed:
        return skip(f"robots.txt disallows {products_url} for {USER_AGENT.split()[0]} — {note}")

    # ---- walk /products.json to exhaustion, under the runaway cap ----
    for page in range(1, args.max_pages + 1):
        url = f"{origin}/products.json?limit={args.page_size}&page={page}"
        fetched = fetch(client, url, budget, effective_host)
        if fetched is None:
            entry["fetches"].append(
                {
                    "url": url,
                    "status": -1,
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "sha256": "",
                    "bytes": 0,
                    "elapsed_ms": 0,
                    "note": "not fetched: per-host request budget exhausted",
                }
            )
            entry["truncated"] = True
            entry["truncation_reason"] = (
                f"per-host request budget of {args.max_requests} exhausted at page {page}"
            )
            break
        record, body = fetched
        entry["fetches"].append(record.as_json())
        print(f"  {record.status} {record.bytes:>9,} B  {url}")
        if record.status != 200 or not body:
            if record.status != 200:
                entry["truncation_reason"] = f"page {page} returned HTTP {record.status}"
                entry["truncated"] = page > 1 and record.status != 404
            break
        name = f"page-{page:03d}.json"
        (store_dir / name).write_bytes(body)
        # Count products cheaply to decide whether to ask for another page. The authoritative
        # extraction happens offline in ``build``.
        try:
            count = len(json.loads(body).get("products") or [])
        except (ValueError, AttributeError) as exc:
            entry["truncation_reason"] = f"page {page} did not parse: {exc}"
            break
        entry["pages"].append(
            {
                "page": page,
                "file": f"{host}/{name}",
                "url": record.url,
                "sha256": record.sha256,
                "fetched_at": record.fetched_at,
                "bytes": record.bytes,
                "products": count,
            }
        )
        if count == 0:
            break  # the catalogue ended exactly on a page boundary
        if count < args.page_size:
            break  # short page: this was the last one
        if page == args.max_pages:
            entry["truncated"] = True
            entry["truncation_reason"] = (
                f"page cap of {args.max_pages} reached while pages were still full — "
                f"this catalogue is INCOMPLETE"
            )

    seen = sum(p["products"] for p in entry["pages"])
    print(
        f"  {len(entry['pages'])} pages, {seen:,} products{'  TRUNCATED' if entry['truncated'] else ''}"
    )
    if not entry["pages"]:
        return skip(entry["truncation_reason"] or "no products were served")
    return entry


def run_fetch(args: argparse.Namespace) -> dict[str, Any]:
    import httpx

    raw_dir: Path = args.raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    budget = PolitenessBudget(min_interval=args.min_interval, max_requests=args.max_requests)
    started = datetime.now(UTC).isoformat()
    entries: list[dict[str, Any]] = []
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        timeout=args.timeout,
        max_redirects=5,
    ) as client:
        for host, role in hosts_for(args):
            print(f"[{host}] ({role})")
            entries.append(fetch_store(client, host, role, budget, args, raw_dir))

    log = {
        "fetched_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "collector": "scripts/collect_real_catalogs.py",
        "user_agent": USER_AGENT,
        "politeness": politeness_block(args),
        "stores": entries,
    }
    (raw_dir / "fetchlog.json").write_text(
        json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return log


# --------------------------------------------------------------------------------------
# phase two: build (offline; never fetches)
# --------------------------------------------------------------------------------------


def product_spans(text: str) -> list[tuple[int, int]]:
    """Byte-exact ``[start, end)`` spans of each element of the top-level ``products`` array.

    ``json.loads`` would give the values but throw away where they came from, and a product
    re-serialised by this process is no longer the bytes the storefront served — which is the
    whole basis for the digests in the provenance file. ``raw_decode`` parses one value at a
    time and reports where it stopped, so the slice can be lifted out untouched.
    """
    key = '"products"'
    start = text.find(key)
    if start < 0:
        return []
    open_bracket = text.find("[", start + len(key))
    if open_bracket < 0:
        return []
    decoder = json.JSONDecoder()
    spans: list[tuple[int, int]] = []
    index = open_bracket + 1
    while True:
        while index < len(text) and text[index] in " \t\r\n,":
            index += 1
        if index >= len(text) or text[index] == "]":
            return spans
        _value, end = decoder.raw_decode(text, index)
        spans.append((index, end))
        index = end


def _write(path: Path, blob: bytes, compress: bool) -> tuple[str, str, int]:
    """Write ``blob``, gzipped or not. Returns (relative name, sha256 on disk, bytes on disk).

    ``mtime=0`` keeps the gzip container byte-identical for identical input, so a re-``build``
    of the same scratch pages produces the same digests and no spurious git diff.
    """
    if compress:
        path = path.with_suffix(path.suffix + ".gz")
        on_disk = gzip.compress(blob, compresslevel=9, mtime=0)
    else:
        on_disk = blob
    path.write_bytes(on_disk)
    return path.name, hashlib.sha256(on_disk).hexdigest(), len(on_disk)


def build_store(entry: dict[str, Any], raw_dir: Path, out: Path, compress: bool) -> dict[str, Any]:
    """Derive one store's corpus slice from its already-fetched pages. No network."""
    host = entry["host"]
    store: dict[str, Any] = {
        "host": host,
        "role": entry["role"],
        "effective_origin": entry["effective_origin"],
        "robots": entry["robots"],
        "skipped": entry["skipped"],
        "fetches": entry["fetches"],
        "pages_fetched": len(entry["pages"]),
        "page_cap": entry["page_cap"],
        "truncated": entry["truncated"],
        "truncation_reason": entry["truncation_reason"],
        "products_seen": 0,
        "products_recorded": 0,
        "duplicates_dropped": 0,
        "files": {},
        "file_sha256": {},
        "bytes": {},
    }
    if entry["skipped"] is not None:
        return store

    product_lines: list[bytes] = []
    provenance_lines: list[bytes] = []
    seen_ids: set[Any] = set()
    seen = 0
    for page in entry["pages"]:
        body = (raw_dir / page["file"]).read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != page["sha256"]:
            raise SystemExit(f"{host}: {page['file']} no longer matches its recorded digest")
        text = body.decode("utf-8")
        if text.encode("utf-8") != body:
            raise SystemExit(f"{host}: page {page['page']} does not round-trip UTF-8")
        spans = product_spans(text)
        for start, end in spans:
            seen += 1
            raw = text[start:end]
            if "\n" in raw or "\r" in raw:
                raise SystemExit(
                    f"{host}: page {page['page']} is pretty-printed; a JSONL line cannot hold "
                    f"a verbatim record. Re-run build with a format that can."
                )
            parsed = json.loads(raw)
            if parsed["id"] in seen_ids:
                store["duplicates_dropped"] += 1
                continue
            seen_ids.add(parsed["id"])
            line = raw.encode("utf-8")
            product_lines.append(line)
            provenance_lines.append(
                json.dumps(
                    {
                        "product_id": parsed["id"],
                        "handle": parsed["handle"],
                        "source_url": page["url"],
                        "http_status": 200,
                        "fetched_at": page["fetched_at"],
                        "response_sha256": page["sha256"],
                        "page": page["page"],
                        "byte_span": [start, end],
                        "sha256": hashlib.sha256(line).hexdigest(),
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            )

    store["products_seen"] = seen
    store["products_recorded"] = len(product_lines)
    if not product_lines:
        store["skipped"] = {"reason": "no products were served"}
        return store

    products_blob = b"\n".join(product_lines) + b"\n"
    provenance_blob = b"\n".join(provenance_lines) + b"\n"
    stores_dir = out / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)
    for role, blob in (("products", products_blob), ("provenance", provenance_blob)):
        name, digest, on_disk = _write(stores_dir / f"{host}.{role}.jsonl", blob, compress)
        store["files"][role] = f"stores/{name}"
        store["file_sha256"][role] = digest
        store["bytes"][role] = {"raw": len(blob), "on_disk": on_disk}
    return store


def run_build(args: argparse.Namespace, log: dict[str, Any]) -> dict[str, Any]:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    stores = [
        build_store(entry, args.raw_dir, out, not args.no_compress) for entry in log["stores"]
    ]

    manifest: dict[str, Any] = {
        "corpus_version": CORPUS_VERSION,
        "collected_at": log["fetched_at"],
        "collector": "scripts/collect_real_catalogs.py",
        "user_agent": log["user_agent"],
        "politeness": log["politeness"],
        "selection": {
            "policy": "entire catalogue — every product on every page, nothing filtered",
            "why": (
                "A category-biased corpus builds a system that can only answer one question "
                "and silently removes the hardest part of retrieval: discriminating relevant "
                "inventory from a large body of irrelevant inventory. The irrelevant stock is "
                "not overhead, it is the test."
            ),
            "dropped": "exact duplicate product ids across pages only; counted per store",
        },
        "storage": {
            "compressed": not args.no_compress,
            "format": "gzip (mtime=0, level 9) over JSONL" if not args.no_compress else "JSONL",
            "note": (
                "A storage decision only. No product is dropped and no field is trimmed, so "
                "what is stored is the whole catalogue."
            ),
        },
        "notes": [
            "POINT-IN-TIME SNAPSHOT of other companies' public catalogues. Prices and "
            "inventory move; assert shape and relationships, never a dollar amount.",
            "Product records are the exact bytes the storefront served, unnormalised.",
            "Collected by hand. No test in this repository opens a socket (D3/C9).",
        ],
        "stores": stores,
    }
    collected = [s for s in stores if s["skipped"] is None]
    manifest["totals"] = {
        "stores_collected": len(collected),
        "stores_skipped": len(stores) - len(collected),
        "stores_truncated": sum(1 for s in stores if s["truncated"]),
        "products": sum(s["products_recorded"] for s in stores),
        "duplicates_dropped": sum(s["duplicates_dropped"] for s in stores),
        "requests_made": sum(len(s["fetches"]) + 1 for s in stores),
        "bytes_raw": sum(v["raw"] for s in stores for v in s["bytes"].values()),
        "bytes_on_disk": sum(v["on_disk"] for s in stores for v in s["bytes"].values()),
    }
    manifest["per_store_counts"] = {
        s["host"]: s["products_recorded"] for s in stores if s["skipped"] is None
    }
    # NOT ``manifest.json``: ``fixtures/manifest.json`` is this repository's single
    # approval-bearing manifest, and ``fixtures/tests/test_manifest.py`` fails on any second
    # file of that name under ``fixtures/`` — deliberately, because two files called
    # "manifest" make every reader's ground truth ambiguous. This one is a collection record.
    (out / "collection.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------


def politeness_block(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "robots_txt": "fetched and respected per host",
        "min_seconds_between_requests_per_host": args.min_interval,
        "max_requests_per_host": args.max_requests,
        "max_pages_per_host": args.max_pages,
        "page_size": args.page_size,
        "crawl_delay": "a declared Crawl-delay widens the interval and never narrows it",
        "retries": "none — 403/404/429 is a recorded outcome, not something to retry around",
        "scope": "public catalogue data only",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "phase",
        nargs="?",
        default="all",
        choices=("fetch", "build", "all"),
        help="fetch = walk the stores into --raw-dir; build = derive the corpus offline",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        required=True,
        help="scratch directory for the verbatim response bodies (NOT committed)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=root / "fixtures" / "real-catalogs",
        help="corpus directory to write (default: fixtures/real-catalogs)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=40, help="runaway guard: pages of products.json per host"
    )
    parser.add_argument("--page-size", type=int, default=250, help="?limit= per page (max 250)")
    parser.add_argument("--min-interval", type=float, default=2.0, help="seconds between hits")
    parser.add_argument("--max-requests", type=int, default=45, help="total requests per host")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout")
    parser.add_argument(
        "--no-compress", action="store_true", help="write plain .jsonl instead of .jsonl.gz"
    )
    parser.add_argument(
        "--only", action="append", default=[], help="restrict to this host (repeatable)"
    )
    args = parser.parse_args(argv)
    for host in args.only:
        if not _SAFE_HOST.match(host):
            parser.error(f"--only {host!r} is not a hostname")
    return args


def hosts_for(args: argparse.Namespace) -> list[tuple[str, str]]:
    roster = [(host, "relevant") for host in RELEVANT_HOSTS]
    roster += [(host, "negative_control") for host in NEGATIVE_CONTROL_HOSTS]
    if args.only:
        roster = [(host, role) for host, role in roster if host in set(args.only)]
    return roster


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.phase in ("fetch", "all"):
        log = run_fetch(args)
    else:
        log = json.loads((args.raw_dir / "fetchlog.json").read_text(encoding="utf-8"))
    if args.phase == "fetch":
        print(f"\nfetched into {args.raw_dir}; run `build` to derive the corpus")
        return 0

    manifest = run_build(args, log)
    print(f"\nwrote {args.out}")
    print(json.dumps(manifest["totals"], indent=2))
    print(json.dumps(manifest["per_store_counts"], indent=2))
    truncated = [s["host"] for s in manifest["stores"] if s["truncated"]]
    if truncated:
        print(f"\nTRUNCATED CATALOGUES: {truncated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
