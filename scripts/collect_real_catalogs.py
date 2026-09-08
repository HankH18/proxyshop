#!/usr/bin/env python
"""Record a point-in-time snapshot of real Shopify storefronts' ENTIRE catalogues.

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

Which stores: a curated file, not a tuple in this source
--------------------------------------------------------
With no ``--hosts-file`` the roster is the built-in ``RELEVANT_HOSTS`` /
``NEGATIVE_CONTROL_HOSTS`` tuples — the ten supplement storefronts the committed corpus was
built from — so the existing corpus stays reproducible from this file alone.

``--hosts-file`` (repeatable) reads a roster from disk instead. Every line carries a
**category** beside the host, because breadth is the whole point of a multi-category corpus
and a corpus that cannot say which categories it spans cannot be checked for breadth::

    burrow.com            furniture              # a walnut coffee table lives here
    homedepot.com         tools     off_platform_control   # measured: not a Shopify store

``fixtures/real-catalogs/candidate-hosts.txt`` is the curated candidate roster and
``fixtures/real-catalogs/incumbent-hosts.txt`` re-states the built-in ten in the same format;
``hosts_file_matches_builtin_roster`` is what keeps that second file honest.

``--only`` narrows whichever roster is in play. It used to filter against the built-in tuple
*only*, so ``--only allbirds.com`` returned an empty roster and the run fetched nothing while
exiting 0 — a failure indistinguishable from "the roster was empty on purpose". A ``--only``
that names a host the roster does not carry is now a hard error, and so is an empty roster.

Resume: a crash at store 900 must not re-hit 900 merchants
-----------------------------------------------------------
Each store's fetch-log entry is written to ``<raw-dir>/<host>/store.json`` **the moment that
store finishes**, and ``fetchlog.json`` is rewritten after every store rather than once at the
end. A run that dies partway therefore leaves a raw directory ``build`` can still read.

``--resume`` (**on by default**; ``--no-resume`` forces a clean re-walk) skips a store whose
record on disk is *provably* complete. "Provably" is the load-bearing word, because a
half-collected store silently treated as finished would poison the corpus:

* the record is written atomically (temp file + ``rename``), so a record that exists parses;
* it carries ``complete``, which is set from **why the walk stopped**, not from the fact that
  it stopped. A short page, an empty page, a robots decision, a 404 — those are outcomes, and
  they are complete. A transport error, a 429, a 5xx, an unparseable body, or a robots.txt
  that could not be read are *retryable*, and a store that ended on one is re-walked;
* it carries the settings that decide the result (page size, page cap, request cap, collector
  version, User-Agent). Raise ``--max-pages`` and every record taken under the old cap is
  invalidated, so a truncated catalogue cannot survive as "already collected";
* every page file it claims is checked to still exist **and still match its recorded digest**
  before the store is skipped.

Anything short of all four re-walks the store and says on stdout which check failed. Reuse is
printed per store; it is never silent.

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

CORPUS_VERSION = "2.1.0"

# The built-in roster. The DELIBERATE NEGATIVES for a liver-support query are named below, in
# `NEGATIVE_CONTROL_HOSTS`, rather than pointed at by position here: measured, they answer a
# milk-thistle search with protein stacks and whey. They are ordinary stores in every other
# respect, and for a *protein* query the roles invert — which is the point of collecting whole
# catalogues rather than one category.
#
# Every one of them sells supplements, and that is this roster's limitation rather than its
# design: a query for a walnut coffee table has nothing here to match and comes back with
# liver capsules. Breadth is a *hostname* problem, so it is solved with `--hosts-file` and a
# curated list rather than by lengthening this tuple.
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
# The category the built-in ten share. Named so `incumbent-hosts.txt` can restate this roster
# in the hosts-file format exactly, digit for digit.
BUILTIN_CATEGORY = "supplements"
BUILTIN_SOURCE = "built-in roster (RELEVANT_HOSTS / NEGATIVE_CONTROL_HOSTS)"

# `relevant`             expect a catalogue.
# `negative_control`     expect a catalogue that answers the headline query with NOTHING. A
#                        shortlist that never has to reject anybody demonstrates nothing.
# `off_platform_control` expect NO catalogue: this store was measured not to serve
#                        /products.json at all. The point of naming it is that a skip here is
#                        the EXPECTED result, so it can be told apart from a store that was
#                        supposed to answer and did not. A probe whose failure path looks
#                        exactly like its negative result is not a probe.
ROLES = ("relevant", "negative_control", "off_platform_control")

_SAFE_HOST = re.compile(r"\A[a-z0-9.-]+\Z")
_SAFE_CATEGORY = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")


@dataclass(frozen=True)
class HostSpec:
    """One store on the roster, with the category that makes breadth checkable."""

    host: str
    category: str
    role: str
    note: str = ""
    source: str = BUILTIN_SOURCE

    def as_json(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "category": self.category,
            "role": self.role,
            "note": self.note,
            "roster_source": self.source,
        }


def builtin_roster() -> list[HostSpec]:
    """The ten stores the committed corpus was built from, in their committed order."""
    return [HostSpec(host, BUILTIN_CATEGORY, "relevant") for host in RELEVANT_HOSTS] + [
        HostSpec(host, BUILTIN_CATEGORY, "negative_control") for host in NEGATIVE_CONTROL_HOSTS
    ]


def parse_hosts_file(path: Path) -> list[HostSpec]:
    """Read a roster file: ``<host> <category> [<role>]  [# note]`` per line.

    Whitespace-separated columns with ``#`` comments, because this file is curated by hand and
    reviewed in a diff. A trailing ``#`` note on a host's own line is kept and recorded against
    that host in ``collection.json`` — provenance for why the host is on the list at all.

    Every malformed line is a hard error naming the file and the line number. Nothing is
    skipped-with-a-warning: a roster that quietly drops the line you meant to add is the same
    silent-empty failure ``--only`` used to have, one layer down.
    """
    if not path.is_file():
        raise SystemExit(f"--hosts-file {path}: no such file")
    specs: list[HostSpec] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line, _, comment = raw.partition("#")
        if not line.strip():
            continue  # a blank line or a whole-line comment
        fields = line.split()
        if len(fields) not in (2, 3):
            raise SystemExit(
                f"{path}:{number}: expected `<host> <category> [<role>]`, got {len(fields)} "
                f"field(s): {raw.strip()!r}"
            )
        host, category = fields[0], fields[1]
        role = fields[2] if len(fields) == 3 else "relevant"
        if not _SAFE_HOST.match(host):
            raise SystemExit(
                f"{path}:{number}: {host!r} is not a bare lower-case hostname "
                f"(no scheme, no path, no upper case)"
            )
        if not _SAFE_CATEGORY.match(category):
            raise SystemExit(
                f"{path}:{number}: category {category!r} must be a lower-case token "
                f"like `home-kitchen`"
            )
        if role not in ROLES:
            raise SystemExit(f"{path}:{number}: role {role!r} is not one of {list(ROLES)}")
        specs.append(
            HostSpec(host, category, role, note=comment.strip(), source=f"{path.name}:{number}")
        )
    if not specs:
        raise SystemExit(f"--hosts-file {path}: carries no host lines")
    return specs


def hosts_file_matches_builtin_roster(path: Path) -> bool:
    """Does ``path`` restate the built-in roster exactly? Order, category and role included."""
    return [(s.host, s.category, s.role) for s in parse_hosts_file(path)] == [
        (s.host, s.category, s.role) for s in builtin_roster()
    ]


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
    spec: HostSpec,
    budget: PolitenessBudget,
    args: argparse.Namespace,
    raw_dir: Path,
) -> dict[str, Any]:
    """Walk one host's whole catalogue into ``raw_dir``. Returns its fetch-log entry."""
    import httpx

    host = spec.host
    entry: dict[str, Any] = {
        "host": host,
        "role": spec.role,
        "category": spec.category,
        "note": spec.note,
        "roster_source": spec.source,
        "effective_origin": f"https://{host}",
        "robots": {},
        "skipped": None,
        "fetches": [],
        "pages": [],
        "page_cap": args.max_pages,
        "truncated": False,
        "truncation_reason": "",
        # WHY the walk stopped, not merely that it did. `outcome_is_retryable` reads this to
        # decide whether a resume may skip the store, so a value that conflates "the catalogue
        # ended" with "the connection died" would be the poison this whole mechanism exists to
        # avoid. It starts as `interrupted` so a store whose walk never reaches an ending —
        # because the process was killed mid-page — can never read as finished.
        "walk_outcome": "interrupted",
    }
    store_dir = raw_dir / host
    store_dir.mkdir(parents=True, exist_ok=True)
    # A previous, longer walk's pages would otherwise sit here confusing anyone reading the
    # scratch directory by hand. `build` reads the entry rather than the directory listing, so
    # this is hygiene rather than correctness — but a stale `page-007.json` beside a four-page
    # record is exactly the kind of thing that gets believed.
    for stale in sorted(store_dir.glob("page-*.json")):
        stale.unlink()

    def skip(reason: str, outcome: str) -> dict[str, Any]:
        entry["skipped"] = {"reason": reason}
        entry["walk_outcome"] = outcome
        print(f"  SKIPPED {host}: {reason}")
        return entry

    # ---- robots.txt first, always ----
    robots_url = f"https://{host}/robots.txt"
    fetched = fetch(client, robots_url, budget, host)
    if fetched is None:
        return skip("per-host request budget exhausted before robots.txt", "robots_budget")
    robots_record, robots_body = fetched
    (store_dir / "robots.txt").write_bytes(robots_body)
    origin = str(httpx.URL(robots_record.url).copy_with(raw_path=b"/")).rstrip("/")
    entry["effective_origin"] = origin
    effective_host = httpx.URL(robots_record.url).host

    allowed = False
    note = ""
    refusal_outcome = "robots_disallowed"
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
        # A 503 or a dropped connection is a machine having a bad minute, not the merchant
        # saying no, and the two must not leave the same record: a resume re-walks the first
        # and honours the second forever.
        elif robots_record.status == 0:
            refusal_outcome = "robots_transport_error"
        else:
            refusal_outcome = f"robots_http_{robots_record.status}"

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
        # The headline sentence must not claim more than was observed. "disallows" is true
        # only when robots.txt was READ and its rules said no; a 403, a 429 or a dropped
        # connection means the file could not be read at all, and recording that as the
        # merchant's written refusal turns our own bad minute into their stated policy.
        # `refusal_outcome` already tells the two apart — `robots_disallowed` is terminal and
        # is never re-asked, while a transport error or a 429 is retryable — so a reason line
        # that says "disallows" for all of them contradicts the record it sits beside. Both
        # were observed live on 2026-09-08: katzmosestools.com dropped the connection and
        # bombas.com answered 429, and both were logged as having disallowed us.
        headline = (
            f"robots.txt disallows {products_url} for {USER_AGENT.split()[0]}"
            if refusal_outcome == "robots_disallowed"
            else f"robots.txt for {host} could not be read, so {products_url} was not fetched"
        )
        return skip(f"{headline} — {note}", refusal_outcome)

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
            entry["walk_outcome"] = "budget"
            break
        record, body = fetched
        entry["fetches"].append(record.as_json())
        print(f"  {record.status} {record.bytes:>9,} B  {url}")
        if record.status != 200 or not body:
            if record.status != 200:
                entry["truncation_reason"] = f"page {page} returned HTTP {record.status}"
                entry["truncated"] = page > 1 and record.status != 404
                entry["walk_outcome"] = (
                    "transport_error" if record.status == 0 else f"http_{record.status}"
                )
            else:
                # HTTP 200 with an empty body. Not a catalogue that ended — a response that
                # arrived hollow, which is a transport symptom wearing a success code.
                entry["truncation_reason"] = f"page {page} returned HTTP 200 with an empty body"
                entry["truncated"] = page > 1
                entry["walk_outcome"] = "empty_body"
            break
        name = f"page-{page:03d}.json"
        (store_dir / name).write_bytes(body)
        # Count products cheaply to decide whether to ask for another page. The authoritative
        # extraction happens offline in ``build``.
        try:
            count = len(json.loads(body).get("products") or [])
        except (ValueError, AttributeError) as exc:
            entry["truncation_reason"] = f"page {page} did not parse: {exc}"
            entry["walk_outcome"] = "unparseable_page"
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
            entry["walk_outcome"] = "exhausted"
            break  # the catalogue ended exactly on a page boundary
        if count < args.page_size:
            entry["walk_outcome"] = "exhausted"
            break  # short page: this was the last one
        if page == args.max_pages:
            entry["truncated"] = True
            entry["truncation_reason"] = (
                f"page cap of {args.max_pages} reached while pages were still full — "
                f"this catalogue is INCOMPLETE"
            )
            entry["walk_outcome"] = "page_cap"

    seen = sum(p["products"] for p in entry["pages"])
    print(
        f"  {len(entry['pages'])} pages, {seen:,} products{'  TRUNCATED' if entry['truncated'] else ''}"
    )
    if not entry["pages"]:
        return skip(entry["truncation_reason"] or "no products were served", entry["walk_outcome"])
    return entry


# --------------------------------------------------------------------------------------
# resume: what "already collected" is allowed to mean
# --------------------------------------------------------------------------------------

STORE_RECORD = "store.json"
STORE_RECORD_SCHEMA = "proxyshop/real-catalogs/store-record@1"

# A walk that ended on one of these did not end because the catalogue ended. Re-walk it.
_RETRYABLE_OUTCOMES = frozenset(
    {
        "interrupted",  # the process died mid-store: the default, so silence never reads as done
        "transport_error",
        "empty_body",
        "unparseable_page",
        "robots_transport_error",
        "robots_budget",
    }
)
# A walk that ended on one of these ended because the merchant answered. Do not ask again.
_TERMINAL_OUTCOMES = frozenset(
    {
        "exhausted",  # a short or empty page: the catalogue ran out
        "page_cap",  # --max-pages; truncated, and the cap is in the fingerprint
        "budget",  # --max-requests; truncated, and that cap is in the fingerprint too
        "robots_disallowed",  # the merchant said no, in writing
    }
)
_HTTP_OUTCOME = re.compile(r"\A(?:robots_)?http_(\d{3})\Z")


def outcome_is_retryable(outcome: str) -> bool:
    """Is ``walk_outcome`` a machine having a bad minute rather than an answer?

    429 and 5xx are the ones that matter in practice: the feasibility study watched Cloudflare
    return an IP-wide 429 within six seconds of parallel probing, and a corpus that recorded
    that as "this merchant serves nothing" would be recording the crawler's own bad behaviour
    as a fact about somebody's shop. 403 and 404 are answers and are kept.

    The three-way shape is deliberate. An outcome this version has never heard of — one a later
    version invents, or a hand-edited record — is **retryable**, so the failure mode of not
    recognising an ending is re-walking a store rather than skipping one that was never
    finished. Costing a merchant one extra polite walk is the cheap error; putting half a
    catalogue in the corpus with a whole catalogue's label is the expensive one.
    """
    if outcome in _RETRYABLE_OUTCOMES:
        return True
    if outcome in _TERMINAL_OUTCOMES:
        return False
    match = _HTTP_OUTCOME.match(outcome)
    if match is None:
        return True  # an outcome this version does not understand fails CLOSED
    status = int(match.group(1))
    return status == 429 or 500 <= status <= 599


def fetch_fingerprint(args: argparse.Namespace) -> dict[str, Any]:
    """The settings that decide what a walk *contains*, so a resume cannot cross them.

    ``--min-interval`` and ``--timeout`` are deliberately absent: they change how long a walk
    takes and how polite it is, never which products come back. ``--max-pages`` and
    ``--max-requests`` are present precisely because they can leave a catalogue TRUNCATED, and
    a truncated store that survived a cap being raised would be a partial catalogue presented
    as a whole one.
    """
    return {
        "collector_version": CORPUS_VERSION,
        "user_agent": USER_AGENT,
        "page_size": args.page_size,
        "max_pages": args.max_pages,
        "max_requests": args.max_requests,
    }


def write_store_record(raw_dir: Path, entry: dict[str, Any], fingerprint: dict[str, Any]) -> Path:
    """Write ``<raw-dir>/<host>/store.json`` atomically, the moment that store finishes.

    Atomically because the whole mechanism turns on "a record that exists is a record that
    parses": a record half-written by a process that died would otherwise be a third state,
    and the resume check would have to guess. ``Path.replace`` is an atomic rename on POSIX,
    so the file either is not there or is complete.
    """
    outcome = str(entry.get("walk_outcome") or "interrupted")
    record = {
        "schema": STORE_RECORD_SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(),
        "walk_outcome": outcome,
        "complete": not outcome_is_retryable(outcome),
        "fingerprint": fingerprint,
        "entry": entry,
    }
    path = raw_dir / str(entry["host"]) / STORE_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    temp.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def reusable_entry(
    raw_dir: Path, host: str, fingerprint: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """``(entry, "")`` when this store need not be fetched again; ``(None, why-not)`` otherwise.

    Four independent checks, and the store is re-walked unless all four pass. The last one is
    the one that catches the case this function exists for: a record can be perfectly complete
    and still be describing page files that were truncated, edited or lost since, and a store
    skipped on that record would put half a catalogue into the corpus with a whole catalogue's
    label on it.
    """
    path = raw_dir / host / STORE_RECORD
    if not path.is_file():
        return None, "no store record on disk"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return None, f"store record unreadable ({type(exc).__name__}: {exc})"
    if not isinstance(record, dict) or record.get("schema") != STORE_RECORD_SCHEMA:
        return None, f"store record is not {STORE_RECORD_SCHEMA}"
    if not record.get("complete"):
        return None, (
            f"the previous walk ended on {record.get('walk_outcome')!r}, which is retryable"
        )
    if record.get("fingerprint") != fingerprint:
        return None, "the collector's settings changed since that walk (see `fingerprint`)"
    entry = record.get("entry")
    if not isinstance(entry, dict) or entry.get("host") != host:
        return None, "store record carries no entry for this host"
    for page in entry.get("pages") or []:
        page_path = raw_dir / str(page.get("file") or "")
        if not page_path.is_file():
            return None, f"page file {page.get('file')!r} is gone"
        # Digest, not size and not a re-parse: the digest is what `build` will check anyway,
        # and it is the only check that catches a body that was truncated at a byte boundary
        # inside a product record.
        if hashlib.sha256(page_path.read_bytes()).hexdigest() != page.get("sha256"):
            return None, f"page file {page.get('file')!r} no longer matches its recorded digest"
    return entry, ""


def run_fetch(args: argparse.Namespace) -> dict[str, Any]:
    import httpx

    roster = hosts_for(args)
    raw_dir: Path = args.raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = fetch_fingerprint(args)
    budget = PolitenessBudget(min_interval=args.min_interval, max_requests=args.max_requests)
    started = datetime.now(UTC).isoformat()
    entries: list[dict[str, Any]] = []
    reused: list[str] = []
    log_path = raw_dir / "fetchlog.json"

    def flush() -> dict[str, Any]:
        """Rewrite ``fetchlog.json`` after EVERY store, not once at the end.

        It used to be written after the last store only, so a run that died at store 900 left
        a raw directory full of pages and no log, and ``build`` opened the log first and died
        on ``FileNotFoundError`` — hours of deliberately slow fetching thrown away and every
        merchant hit again on the retry. The log is a few hundred kilobytes; rewriting it 45
        times costs nothing next to that.

        ``complete`` says whether the whole roster was walked. A partial log is legitimately
        buildable, but it must never look like a whole one: ``run_build`` reads this key.
        """
        done = {str(e["host"]) for e in entries}
        log: dict[str, Any] = {
            "fetched_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "collector": "scripts/collect_real_catalogs.py",
            "user_agent": USER_AGENT,
            "politeness": politeness_block(args),
            "fingerprint": fingerprint,
            "roster": [spec.as_json() for spec in roster],
            # `--only` narrows the roster, so a run under it can be `complete` while the corpus
            # built from it holds one store. That is correct — the run finished what it was
            # asked to do — but it must be legible, or a one-store corpus reads as a whole one.
            # Re-running `fetch` without `--only` restores the rest for free: the store records
            # are still on disk and the resume costs zero requests.
            "only": list(args.only),
            "complete": len(done) == len(roster),
            "pending": [spec.host for spec in roster if spec.host not in done],
            "reused_from_earlier_runs": list(reused),
            "stores": entries,
        }
        log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return log

    log = flush()  # so an interrupt before the first store still leaves a readable log
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        timeout=args.timeout,
        max_redirects=5,
    ) as client:
        for spec in roster:
            if args.resume:
                entry, why_not = reusable_entry(raw_dir, spec.host, fingerprint)
                if entry is not None:
                    # The roster is the authority on labels; the record is the authority on
                    # bytes. Re-categorising a host in the hosts file must land without
                    # re-fetching it.
                    entry["role"] = spec.role
                    entry["category"] = spec.category
                    entry["note"] = spec.note
                    entry["roster_source"] = spec.source
                    pages = len(entry.get("pages") or [])
                    products = sum(int(p["products"]) for p in entry.get("pages") or [])
                    print(
                        f"[{spec.host}] ({spec.category}/{spec.role}) REUSED — "
                        f"{pages} pages, {products:,} products, no request made"
                    )
                    entries.append(entry)
                    reused.append(spec.host)
                    log = flush()
                    continue
                if (raw_dir / spec.host).exists():
                    print(f"[{spec.host}] re-walking: {why_not}")
            print(f"[{spec.host}] ({spec.category}/{spec.role})")
            entry = fetch_store(client, spec, budget, args, raw_dir)
            write_store_record(raw_dir, entry, fingerprint)
            entries.append(entry)
            log = flush()

    if reused:
        print(f"\nreused {len(reused)} of {len(roster)} stores from an earlier run: {reused}")
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
        # `.get` rather than `[]`: a raw directory fetched by an earlier version of this script
        # has no category, and refusing to build it would throw away exactly the slow, polite
        # fetching this whole file exists to avoid repeating.
        "category": entry.get("category") or "uncategorised",
        "note": entry.get("note", ""),
        "roster_source": entry.get("roster_source", ""),
        "walk_outcome": entry.get("walk_outcome", ""),
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


def collected_span(stores: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Earliest and latest instant any byte in the corpus was fetched.

    Every recorded fetch is consulted, not just the run's start: with ``--resume`` a store can
    be weeks older than the run that assembled the corpus around it, and a single
    ``collected_at`` would quietly present the older bytes as same-day.
    """
    instants: list[datetime] = []
    for store in stores:
        candidates = [str((store.get("robots") or {}).get("fetched_at") or "")]
        candidates += [str(f.get("fetched_at") or "") for f in store.get("fetches") or []]
        for text in candidates:
            try:
                instants.append(datetime.fromisoformat(text))
            except ValueError:
                continue
    if not instants:
        return {"earliest": "", "latest": "", "days": 0.0}
    earliest, latest = min(instants), max(instants)
    return {
        "earliest": earliest.isoformat(),
        "latest": latest.isoformat(),
        "days": round((latest - earliest).total_seconds() / 86_400.0, 3),
    }


def run_build(args: argparse.Namespace, log: dict[str, Any]) -> dict[str, Any]:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    stores = [
        build_store(entry, args.raw_dir, out, not args.no_compress) for entry in log["stores"]
    ]

    manifest: dict[str, Any] = {
        "corpus_version": CORPUS_VERSION,
        "collected_at": log["fetched_at"],
        # `collected_at` is when the RUN started, and with --resume that is not when every
        # store was read. The span is the honest answer: a corpus whose stores were fetched
        # three weeks apart is still a snapshot, but it is a snapshot of three weeks.
        "collected_span": collected_span(stores),
        "run": {
            "complete": bool(log.get("complete", True)),
            "roster_size": len(log.get("roster") or stores),
            "restricted_to": list(log.get("only") or []),
            "pending": list(log.get("pending") or []),
            "reused_from_earlier_runs": list(log.get("reused_from_earlier_runs") or []),
        },
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
    # Breadth, stated rather than hoped for. The reason this corpus exists in more than one
    # category is that a single-category corpus answers "a walnut coffee table for the lounge"
    # with liver capsules; a corpus that cannot report which categories it spans cannot be
    # checked for having stopped spanning them.
    categories: dict[str, dict[str, Any]] = {}
    for s in stores:
        bucket = categories.setdefault(
            str(s.get("category") or "uncategorised"),
            {"stores": [], "stores_with_inventory": [], "products": 0},
        )
        bucket["stores"].append(s["host"])
        if s["skipped"] is None:
            bucket["stores_with_inventory"].append(s["host"])
            bucket["products"] += s["products_recorded"]
    manifest["categories"] = dict(sorted(categories.items()))
    manifest["totals"]["categories"] = len(
        [name for name, b in categories.items() if b["stores_with_inventory"]]
    )
    # A skip is a recorded outcome, and `off_platform_control` hosts are ON the roster to be
    # skipped. Separating the two is what stops "the endpoint is not there" (the expected
    # result of a deliberate miss) reading the same as "the endpoint was there and we failed".
    manifest["totals"]["off_platform_controls"] = sum(
        1 for s in stores if s.get("role") == "off_platform_control"
    )
    manifest["totals"]["off_platform_controls_that_answered"] = sum(
        1 for s in stores if s.get("role") == "off_platform_control" and s["skipped"] is None
    )
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
        "concurrency": (
            "none — one host at a time, one request at a time. Measured: 8 concurrent "
            "connections across 30 Cloudflare-fronted zones drew an IP-wide 429 at request 41, "
            "5.9 seconds in; the same 80 zones serially cost 209 requests and zero blocks."
        ),
        "resume": (
            "a store already walked to a non-retryable outcome under these same settings is "
            "not re-fetched; every skip is checked against the page digests on disk"
            if args.resume
            else "disabled (--no-resume): every store on the roster is walked again"
        ),
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
        "--hosts-file",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help=(
            "roster file, `<host> <category> [<role>]` per line with # comments (repeatable). "
            "Without one the built-in ten-supplement-store roster is used, unchanged."
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help=(
            "restrict to this host (repeatable). It narrows whichever roster is in play; a "
            "host the roster does not carry is an ERROR, not an empty run."
        ),
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "skip a store whose --raw-dir record is provably complete under these same "
            "settings and whose page digests still match (default: on). --no-resume re-walks "
            "every store on the roster."
        ),
    )
    args = parser.parse_args(argv)
    for host in args.only:
        if not _SAFE_HOST.match(host):
            parser.error(f"--only {host!r} is not a hostname")
    return args


def hosts_for(args: argparse.Namespace) -> list[HostSpec]:
    """The roster this run will walk, with every way of ending up with nothing made loud.

    ``--only`` used to be filtered against ``RELEVANT_HOSTS`` + ``NEGATIVE_CONTROL_HOSTS`` and
    nothing else, so ``--only allbirds.com`` produced an EMPTY roster, fetched nothing, wrote a
    log with no stores and exited 0 — a result no reader could tell apart from "the roster was
    deliberately empty". Both ways of arriving at an empty roster are now errors.
    """
    if args.hosts_file:
        roster: list[HostSpec] = []
        seen: dict[str, str] = {}
        for path in args.hosts_file:
            for spec in parse_hosts_file(Path(path)):
                if spec.host in seen:
                    raise SystemExit(
                        f"{spec.source}: {spec.host} is already on the roster from "
                        f"{seen[spec.host]}; a duplicate would be walked twice and would "
                        f"silently take one of its two categories"
                    )
                seen[spec.host] = spec.source
                roster.append(spec)
    else:
        roster = builtin_roster()

    if args.only:
        wanted = {host.lower() for host in args.only}
        unknown = sorted(wanted - {spec.host for spec in roster})
        if unknown:
            where = (
                ", ".join(str(p) for p in args.hosts_file)
                if args.hosts_file
                else "the built-in roster"
            )
            raise SystemExit(
                f"--only names {unknown}, which {where} does not carry. Add the host to a "
                f"--hosts-file rather than reaching for it here: the roster is the record of "
                f"which businesses this crawler is allowed to touch."
            )
        roster = [spec for spec in roster if spec.host in wanted]

    if not roster:
        raise SystemExit("the roster is empty; there is nothing to fetch")
    return roster


def load_fetchlog(raw_dir: Path) -> dict[str, Any]:
    """Read ``fetchlog.json``, and say what to do when it is not there.

    ``run_fetch`` rewrites this file after every store, so its absence means no store has
    finished — not that the fetching was wasted. The bare ``FileNotFoundError`` this replaced
    said neither.
    """
    path = raw_dir / "fetchlog.json"
    if not path.is_file():
        walked = sorted(p.parent.name for p in raw_dir.glob(f"*/{STORE_RECORD}"))
        detail = (
            f"{len(walked)} store record(s) are there ({walked[:5]}...), so re-run `fetch` "
            f"with --resume (the default) to rebuild the log without re-hitting them"
            if walked
            else "no store was walked to completion; re-run `fetch`"
        )
        raise SystemExit(f"{path} is missing — {detail}")
    return dict(json.loads(path.read_text(encoding="utf-8")))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.phase in ("fetch", "all"):
        log = run_fetch(args)
    else:
        log = load_fetchlog(args.raw_dir)
    if args.phase == "fetch":
        print(f"\nfetched into {args.raw_dir}; run `build` to derive the corpus")
        return 0

    manifest = run_build(args, log)
    print(f"\nwrote {args.out}")
    print(json.dumps(manifest["totals"], indent=2))
    print(json.dumps(manifest["per_store_counts"], indent=2))
    print(
        json.dumps(
            {name: bucket["products"] for name, bucket in manifest["categories"].items()},
            indent=2,
        )
    )
    truncated = [s["host"] for s in manifest["stores"] if s["truncated"]]
    if truncated:
        print(f"\nTRUNCATED CATALOGUES: {truncated}")
    if not manifest["run"]["complete"]:
        # Loud, and NOT an error: a partial corpus is a legitimate thing to build. What is not
        # legitimate is a partial corpus that reads as a whole one, which is why the same fact
        # is in collection.json under `run`.
        print(
            f"\nINCOMPLETE RUN: {len(manifest['run']['pending'])} of "
            f"{manifest['run']['roster_size']} roster stores were never walked — "
            f"{manifest['run']['pending'][:10]}. This corpus is a PARTIAL collection; "
            f"re-run `fetch` (--resume is on by default) to finish it."
        )
    surprises = manifest["totals"]["off_platform_controls_that_answered"]
    if surprises:
        print(
            f"\n{surprises} host(s) marked `off_platform_control` served a catalogue. That is "
            f"the control failing open: either the host moved onto the platform, or the role "
            f"is wrong. Fix the roster before trusting the negative-control story."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
