#!/usr/bin/env python
"""Record a point-in-time snapshot of real Shopify storefronts' ENTIRE catalogues.

**Run by hand. Never by the test suite.** The gates in ``fixtures/tests/test_real_catalogs.py``
(the committed ten-store corpus) and ``fixtures/tests/test_real_catalogs_broad.py`` (the staged
38-store one) read the recorded corpora off disk and open no socket (D3/C9); this script is the
only thing in the repository that talks to those hosts, and it is invoked deliberately by a
person.

Two phases, and the split is deliberate
---------------------------------------
``fetch``
    Walks ``/products.json?limit=250&page=N`` per host to exhaustion and writes every response
    body **verbatim** into a scratch directory, together with a fetch log. This is the only
    phase that opens a socket. It is **resumable and re-runnable**: a store already walked to
    a non-retryable outcome under the same settings costs zero requests on the next run (see
    *Resume*, below), which is what makes an interrupted collection recoverable without
    re-hitting the merchants who already answered.
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
* it carries ``walk_outcome`` — **why the walk stopped**, not the fact that it stopped — and
  the read path re-judges it with ``outcome_is_retryable`` rather than trusting the
  ``complete`` boolean stored beside it. A short page, an empty page, a robots decision, a
  403/404: outcomes, and terminal. A transport error, a 429, a 5xx, an unparseable body, an
  unreadable ``robots.txt``, or **an outcome this version has never heard of**: retryable, and
  the store is walked again. ``complete`` is then required to *agree* with that judgement, so a
  record whose two fields disagree — the shape a hand edit leaves — is refused rather than
  believed;
* it carries the settings that decide the result (page size, page cap, request cap, collector
  version, User-Agent). Raise ``--max-pages`` and every record taken under the old cap is
  invalidated, so a truncated catalogue cannot survive as "already collected";
* every page file it claims is checked to still exist **and still match its recorded digest**
  before the store is skipped.

Anything short of all four re-walks the store and says on stdout which check failed. Reuse is
printed per store; it is never silent.

A walk that FAILS never destroys what an earlier walk collected. Stale page files are removed
only after a walk has produced pages of its own, and ``write_store_record`` refuses to replace
a terminal record with a retryable one — the failed walk is parked beside it as
``store.last-failed-walk.json``. Before this, the stale-page sweep ran before the robots fetch,
so a single ``--no-resume`` run made while the egress IP happened to be blocked emptied the raw
directory host by host while collecting nothing.

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
* At least ``--min-interval`` seconds between requests to the same host, and a declared
  ``Crawl-delay`` widens that and never narrows it. Walking a whole catalogue is more requests
  than sampling one, so the gap matters more, not less. 2.0 s is a **floor, not a default**:
  ``parse_args`` and ``PolitenessBudget`` both put the value through ``is_polite_interval``,
  which requires it to be **finite** and at or above ``MIN_INTERVAL_FLOOR``, so there is no
  argument vector and no direct construction that walks a merchant faster. It used to be only a
  default (``--min-interval 0`` was accepted in silence), and then it was two bare ``<``
  comparisons, which ``nan`` cleared — leaving a walk that never paused at all.
* ``--max-pages`` (default 40, i.e. 10,000 products) is a runaway guard, not a sampling knob.
  A store that hits it has a TRUNCATED catalogue, and both ``collection.json`` and the README say so
  per store rather than presenting a partial catalogue as complete.
* **No retry loop.** Within a run nothing is re-requested: a 403, a 404 or a 429 ends that
  store's walk and is recorded as its outcome. Across runs, ``--resume`` does re-ask a host
  whose recorded outcome was retryable (429, 5xx, transport error) — that is a fresh run, made
  deliberately by a person, not a loop hammering a host that just answered. Saying "nothing
  retries" full stop was false the moment resume landed, and ``collection.json`` now says which
  of the two it means.
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
    The run itself: politeness settings, every host's robots decision, every fetch the
    surviving walk of each host made, the per-store counts, whether any catalogue is truncated,
    and a digest of each file. ``totals.requests_recorded`` is the count the merchants can be
    said to have seen, and it is recomputable from ``stores`` — a re-walk replaces its own
    record, so requests it superseded reach the total through ``requests_charged`` rather than
    through the ``fetches`` rows, which are gone. Whether that carrying-forward actually
    happened for a given store is ``requests_charged_accumulated``, and
    ``totals.requests_recorded_is_floor`` is the same fact for the collection:
    ``requests_charged`` is written for every store, so only those two fields can tell a real
    accumulated count from one ``build`` synthesised out of the surviving walk.
    ``politeness.request_accounting`` states the arithmetic; both corpus gate files run it
    against the artifact that states it.
``stores/<host>.provenance.jsonl.gz``
    Row-aligned with the products file. Per product: the URL, the HTTP status, the fetch
    instant, the digest of the whole response, the byte span inside it, and the digest of the
    product's own bytes. The graph refuses a material fact with no ``SUPPORTED_BY -> Source``
    edge and ``candidate_shops()`` drops an unsourced shop from the roster entirely, so a
    corpus that cannot say where a product came from produces a graph that looks full and
    rosters empty.

Gzip because this is a git repository and this JSON compresses about 10x. Measured on the
38-store corpus in ``fixtures/real-catalogs-broad``: 140,688,214 raw JSONL bytes stored as
13,954,284 on disk, 10.1x at level 9. Reproduce with::

    python -c "import json;m=json.load(open('fixtures/real-catalogs-broad/collection.json'));\
t=m['totals'];print(t['bytes_raw'],t['bytes_on_disk'],round(t['bytes_raw']/t['bytes_on_disk'],2))"

It is a *storage* decision: no product is dropped and no field is trimmed, so the corpus stays
the whole catalogue. ``--no-compress`` writes plain ``.jsonl`` for inspection.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USER_AGENT = "ProxyShopBot/0.1 (catalog research; contact: hank.holcomb@challenger.gauntletai.com)"

CORPUS_VERSION = "2.2.0"

#: The politeness floor, in seconds between requests to one origin. A FLOOR, not a default:
#: ``parse_args`` and ``PolitenessBudget`` both run :func:`is_polite_interval` over the value,
#: so a merchant cannot be walked faster than this by any argument vector or direct call. The
#: manifest declares 2.0 s and the egress IP still works; the feasibility study watched
#: Cloudflare answer parallel probing with an IP-wide 429 in 5.9 seconds, and this gap is 73-74%
#: of the wall clock of a collection precisely because it is what buys that.
#:
#: Raising ``CORPUS_VERSION`` to 2.2.0 is what retires the records taken before the floor
#: existed: ``collector_version`` sits in ``fetch_fingerprint``, so a record written by a
#: collector that could have walked at zero seconds is not reusable by this one.
MIN_INTERVAL_FLOOR = 2.0


def is_polite_interval(seconds: float) -> bool:
    """Finite, and at or above :data:`MIN_INTERVAL_FLOOR`.

    ``isfinite`` is here rather than a bare ``>=`` because a bare ``>=`` was the whole hole.
    Every comparison against NaN is False, so ``--min-interval nan`` cleared the check in
    ``parse_args`` AND the one in ``PolitenessBudget.__post_init__``; ``interval_for`` then
    returned NaN, ``spend`` computed ``wait = nan`` and tested ``if wait > 0``, which is False
    as well — so a six-request walk of one host slept **zero times** under a manifest declaring
    2.0 s. ``max(nan, delay)`` defeated ``honour_crawl_delay`` the same way. Measured against
    the mock transport before this function existed; ``test_no_argument_vector_can_reach_an_
    unenforceable_interval`` and its neighbours are that measurement, kept.

    Infinity is refused for the opposite reason: it passes ``>=`` honestly and then parks the
    walk forever on its second request. Negative zero was already refused (``-0.0 < 2.0``) and
    still is.
    """
    return math.isfinite(seconds) and seconds >= MIN_INTERVAL_FLOOR


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


def _pause(seconds: float) -> None:
    """The only sleep in this file, named so a test can make waiting free.

    A test that patches this does NOT get a shorter interval: ``min_interval`` is floored in
    two places that this function is not one of, and the interval a store was walked at is
    recorded per host in ``robots.crawl_delay_seconds``. So the fake clock buys speed and
    cannot buy an impolite manifest.
    """
    time.sleep(seconds)


@dataclass
class PolitenessBudget:
    """Per-host rate limit and request cap. There is no way to spend past the cap.

    A ``min_interval`` that is not :func:`is_polite_interval` is a ``ValueError`` here as well
    as in ``parse_args``. Two checks rather than one because the CLI is not the only caller:
    this class is constructible directly, and a floor that only exists in argument parsing is a
    default wearing a floor's name. Both checks go through the one predicate so that neither
    can be the loose one — they were two separate ``<`` comparisons, and NaN walked through
    both.
    """

    min_interval: float
    max_requests: int
    _last: dict[str, float] = field(default_factory=dict)
    _count: dict[str, int] = field(default_factory=dict)
    _host_interval: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not is_polite_interval(self.min_interval):
            raise ValueError(
                f"min_interval={self.min_interval} is not a finite interval at or above the "
                f"{MIN_INTERVAL_FLOOR}s politeness floor these are real businesses depend on. "
                f"The floor is not a knob; if a merchant needs a wider gap, raise it, never "
                f"lower it."
            )

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
        """A declared ``Crawl-delay`` widens this host's interval and never narrows it.

        ``robots.txt`` is somebody else's file, so ``delay`` is untrusted input. A non-finite
        one is ignored rather than fatal — refusing to walk a merchant because their
        ``Crawl-delay`` is unparseable would be the wrong answer — and ignoring it leaves this
        host on our own floor. An infinite delay reached ``max`` before this guard and made the
        interval infinite, which is a hang, not a politeness.
        """
        if not math.isfinite(delay):
            delay = 0.0
        self._host_interval[self.key(host)] = max(self.min_interval, MIN_INTERVAL_FLOOR, delay)

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
                _pause(wait)
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


def _requests_in(entry: dict[str, Any]) -> int:
    """HTTP requests this walk really made: the robots fetch, plus every page actually asked for.

    Not ``len(fetches) + 1``. A page the per-host budget refused is recorded in ``fetches`` with
    ``status: -1`` and the note "not fetched", and counting it would inflate what the merchant
    saw; and a walk whose budget ran out before robots.txt has no ``robots`` block at all, so
    the ``+ 1`` would invent a request that never happened.
    """
    fetches = entry.get("fetches") or []
    asked = sum(1 for f in fetches if int(f.get("status", 0)) != -1)
    return asked + (1 if entry.get("robots") else 0)


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
    # Pages of a previous, longer walk are swept in `write_store_record`, once a record that
    # does not claim them has actually been written. This loop used to run HERE — after
    # `mkdir`, before the robots fetch, before any request at all — so a walk that then failed
    # on robots had already deleted everything an earlier successful walk collected. Measured:
    # with a client raising `ConnectError`, floydhome.com's 3.7 MB `page-001.json` was gone and
    # the walk collected nothing, so one `--no-resume` run made while the egress IP happened to
    # be blocked emptied the raw directory host by host. That directory is what makes the
    # corpus's "promotion is offline and costs the merchants nothing" true.

    def skip(reason: str, outcome: str) -> dict[str, Any]:
        entry["skipped"] = {"reason": reason}
        entry["walk_outcome"] = outcome
        entry["requests"] = _requests_in(entry)
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

    entry["requests"] = _requests_in(entry)
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
_HTTP_OUTCOME = re.compile(r"\A(robots_)?http_(\d{3})\Z")


def outcome_is_retryable(outcome: str) -> bool:
    """Is ``walk_outcome`` a machine having a bad minute rather than an answer?

    429 and 5xx are the ones that matter in practice: the feasibility study watched Cloudflare
    return an IP-wide 429 within six seconds of parallel probing, and a corpus that recorded
    that as "this merchant serves nothing" would be recording the crawler's own bad behaviour
    as a fact about somebody's shop. A 403 or a 404 on ``/products.json`` is the merchant's
    server answering about the resource we asked for, and is kept.

    **A 403 on robots.txt is the one exception, and it is a deliberate reversal.** It used to
    fall in with the catalogue 403s, so ``industrywest.com`` — a furniture store, in the
    category this corpus lost most of — was skipped forever as though it had answered. It did
    not answer: the request that got the 403 was for the merchant's *policy file*, and the
    collector's own fail-closed rule then meant ``/products.json`` was **never asked for at
    all**. Recording "we could not read their policy" as "they answered" is the same mistake as
    recording an IP-wide 429 as an empty shop, and the three hosts whose robots.txt was
    unreadable here (403, 429, dropped connection) are one class of event, not two. The other
    two were already retryable; this makes the third agree.

    The counter-argument is real — a 403 IS a refusal, and re-asking a host that refused is
    what the politeness posture exists to prevent — so the reversal is scoped as tightly as it
    can be. It costs the host exactly **one robots.txt GET on a later run started by a person**,
    at the floor interval, and no catalogue request follows unless that robots.txt is both
    readable and permissive. It changes nothing about ``http_403``: a catalogue page that comes
    back 403 stays terminal.

    ``CORPUS_VERSION`` is deliberately NOT bumped for this. A bump retires every record in a
    scratch directory and would charge 53 merchants a fresh walk to correct one host's
    judgement; it is not needed, because a record written under the old table says
    ``{"complete": true, "walk_outcome": "robots_http_403"}`` and ``reusable_entry`` judges the
    outcome first — retryable now — so exactly the affected records, and only those, are
    re-walked.

    The three-way shape is deliberate. An outcome this version has never heard of — one a later
    version invents, or a hand-edited record — is **retryable**, so the failure mode of not
    recognising an ending is re-walking a store rather than skipping one that was never
    finished. Costing a merchant one extra polite walk is the cheap error; putting half a
    catalogue in the corpus with a whole catalogue's label is the expensive one.

    That protection is only real because ``reusable_entry`` CALLS this on the read path. It did
    not: it trusted the ``complete`` boolean ``write_store_record`` had stored beside the
    outcome, so a record saying ``{"complete": true, "walk_outcome": "interrupted"}`` was reused
    with no request and this function never ran. Every resume fixture wrote its record through
    ``write_store_record``, where the two fields cannot disagree, so nothing noticed.
    """
    if outcome in _RETRYABLE_OUTCOMES:
        return True
    if outcome in _TERMINAL_OUTCOMES:
        return False
    match = _HTTP_OUTCOME.match(outcome)
    if match is None:
        return True  # an outcome this version does not understand fails CLOSED
    robots_phase, status = bool(match.group(1)), int(match.group(2))
    if status == 429 or 500 <= status <= 599:
        return True
    # A robots.txt we were refused is a policy we never read. A robots.txt that came back 404 is
    # a policy that does not exist, which IS an answer and stays terminal.
    return robots_phase and status == 403


def fetch_fingerprint(args: argparse.Namespace) -> dict[str, Any]:
    """The settings that decide what a walk *contains*, so a resume cannot cross them.

    ``--min-interval`` and ``--timeout`` are absent because they change how long a walk takes,
    never which products come back — and, for the interval, because two other things now make
    its absence safe rather than merely defensible. It has to satisfy
    :func:`is_polite_interval`, so there is no impolite value to splice in — including the
    non-finite ones, which used to clear both floor checks and then disable every pause; and the
    interval each host was actually walked at is recorded per store in
    ``robots.crawl_delay_seconds``, so a corpus assembled from several runs can be *checked*
    rather than assumed. ``collector_version`` is what retires records taken before the floor
    existed.

    ``--max-pages`` and ``--max-requests`` are present precisely because they can leave a
    catalogue TRUNCATED, and a truncated store that survived a cap being raised would be a
    partial catalogue presented as a whole one.
    """
    return {
        "collector_version": CORPUS_VERSION,
        "user_agent": USER_AGENT,
        "page_size": args.page_size,
        "max_pages": args.max_pages,
        "max_requests": args.max_requests,
    }


FAILED_WALK_RECORD = "store.last-failed-walk.json"


def _read_record(path: Path) -> dict[str, Any] | None:
    """The store record at ``path``, or ``None`` if there is not a parseable one there."""
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return record if isinstance(record, dict) else None


def write_store_record(
    raw_dir: Path, entry: dict[str, Any], fingerprint: dict[str, Any]
) -> dict[str, Any]:
    """Write ``<raw-dir>/<host>/store.json`` atomically, the moment that store finishes.

    Atomically because the whole mechanism turns on "a record that exists is a record that
    parses": a record half-written by a process that died would otherwise be a third state,
    and the resume check would have to guess. ``Path.replace`` is an atomic rename on POSIX,
    so the file either is not there or is complete.

    **A failed walk does not overwrite a finished one.** Every walk used to replace the record
    unconditionally, so a run made while the egress IP was blocked — one ``--no-resume``, or one
    ``--max-pages`` bump on a bad afternoon — replaced each host's good record with a
    ``robots_transport_error`` and the next resume re-fetched all of them. The failed walk is
    kept, because a failure that leaves no trace is its own problem: it goes to
    ``store.last-failed-walk.json`` beside the record it was not allowed to replace.
    """
    outcome = str(entry.get("walk_outcome") or "interrupted")
    path = raw_dir / str(entry["host"]) / STORE_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_record(path)
    if existing is not None and existing.get("schema") != STORE_RECORD_SCHEMA:
        existing = None
    # Every walk of this host that this raw directory has ever seen, added up. `requests` is
    # what THIS walk cost; `requests_charged` is what the host has been charged in total, and it
    # is the only one of the two that survives a re-walk — the record is per store and a re-walk
    # replaces it, so a total derived from `fetches` alone structurally cannot see the walk it
    # replaced. `collection.json` publishes the charged number for exactly that reason.
    made = int(entry.get("requests") or _requests_in(entry))
    entry["requests"] = made
    earlier = int(((existing or {}).get("entry") or {}).get("requests_charged") or 0)
    entry["requests_charged"] = earlier + made
    record = {
        "schema": STORE_RECORD_SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(),
        "walk_outcome": outcome,
        "complete": not outcome_is_retryable(outcome),
        "fingerprint": fingerprint,
        "entry": entry,
    }
    temp = path.with_name(path.name + ".partial")
    if not record["complete"] and existing is not None:
        if not outcome_is_retryable(str(existing.get("walk_outcome") or "interrupted")):
            parked = path.with_name(FAILED_WALK_RECORD)
            parked.write_text(
                json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            # The walk failed; the requests it made still happened. Charging them to the record
            # that survives is the whole point of keeping the number separately from `fetches`:
            # a failed walk leaves no fetch rows behind in the corpus, so an accounting that
            # only ever reads `fetches` under-reports exactly the runs that went wrong.
            kept_entry = existing.get("entry")
            if isinstance(kept_entry, dict):
                kept_entry["requests_charged"] = entry["requests_charged"]
                temp.write_text(
                    json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                temp.replace(path)
            print(
                f"  KEPT the earlier finished record for {entry['host']}: this walk ended on "
                f"{outcome!r}, which is retryable. The failed walk is in {parked.name}, and "
                f"its {made} request(s) are charged to the record that stands."
            )
            return existing
    temp.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)
    # Only now, with a record on disk that does not claim them, are a longer earlier walk's
    # pages stale. Sweeping is tied to the record rather than to the start of a walk because the
    # record is what `build` and the resume both read; a `page-007.json` beside a four-page
    # record is the kind of thing that gets believed, but deleting it before the walk that
    # replaces it has succeeded is how a bad minute became a re-fetch of every merchant.
    keep = {str(page.get("file") or "").rsplit("/", 1)[-1] for page in entry.get("pages") or []}
    for stale in sorted(path.parent.glob("page-*.json")):
        if stale.name not in keep:
            stale.unlink()
    return record


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
    # The OUTCOME is the authority, not the boolean stored beside it. `write_store_record`
    # derives one from the other, so in a record this collector wrote they cannot disagree —
    # which is exactly why trusting the boolean read as safe and was not: nothing on this path
    # ever called `outcome_is_retryable`, so a record with `complete: true` and an outcome of
    # `interrupted`, `transport_error` or a value no version has ever emitted was reused with
    # no request. Judge the outcome first; then require `complete` to agree, because a record
    # whose two fields contradict each other is not a record, it is damage.
    outcome = record.get("walk_outcome")
    if not isinstance(outcome, str) or not outcome:
        return None, "store record does not say why the walk ended (`walk_outcome` is missing)"
    if outcome_is_retryable(outcome):
        return None, f"the previous walk ended on {outcome!r}, which is retryable"
    if record.get("complete") is not True:
        return None, (
            f"store record disagrees with itself: `walk_outcome` {outcome!r} is terminal but "
            f"`complete` is {record.get('complete')!r}"
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


def relabel(entry: dict[str, Any], spec: HostSpec) -> None:
    """The roster is the authority on labels; the record is the authority on bytes.

    Re-categorising a host in the hosts file, or moving it to another role, must land without
    re-fetching it — the category is what makes breadth checkable and it is not worth a request.
    """
    entry["role"] = spec.role
    entry["category"] = spec.category
    entry["note"] = spec.note
    entry["roster_source"] = spec.source


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
                    relabel(entry, spec)
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
            walked = fetch_store(client, spec, budget, args, raw_dir)
            # The RECORD is the authority on what this raw directory now holds, and it is not
            # always the walk that just finished: a failed walk is refused when a finished
            # record is already there. Logging `walked` regardless would drop that store from
            # the corpus on the say-so of the run that failed to collect it.
            record = write_store_record(raw_dir, walked, fingerprint)
            governing = record.get("entry")
            entry = governing if isinstance(governing, dict) else walked
            if entry is not walked:
                print(f"[{spec.host}] logging the earlier finished record, not this failed walk")
            relabel(entry, spec)
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
        # What this host was actually asked, carried across every walk of it this scratch
        # directory has seen — see POLITENESS_POSTURE["request_accounting"].
        "requests": entry.get("requests", _requests_in(entry)),
        "requests_charged": entry.get("requests_charged", _requests_in(entry)),
        # ...and whether that number is the carried-across one or a stand-in. `build` fills the
        # field in for every store, including stores whose scratch record predates it, so the
        # field's PRESENCE stopped meaning anything the moment it became universal: a reader
        # could not tell a genuine accumulated count from one synthesised out of the surviving
        # walk. False means "this store's number is a floor" — the walks that were replaced are
        # not in it — and `totals.requests_recorded_is_floor` is the same fact for the whole
        # collection.
        "requests_charged_accumulated": "requests_charged" in entry,
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
        # `build` is offline and re-runnable, so the version that DERIVED a corpus is routinely
        # newer than the one that fetched its bytes. Recording only one of the two made a
        # rebuilt corpus claim its bytes were taken under rules that did not exist yet.
        "collector_version_at_fetch": str(
            (log.get("fingerprint") or {}).get("collector_version") or "unknown"
        ),
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
        # The numbers a run was given come from its log; the description of what the collector
        # DOES comes from this file. A corpus rebuilt by `build` would otherwise keep whatever
        # the fetching run wrote down about behaviour, including sentences later found false.
        "politeness": {**log["politeness"], **POLITENESS_POSTURE},
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
        # NOT `sum(len(fetches) + 1)`. That counted a budget-refused page as a request, invented
        # a robots fetch for a store whose budget ran out before robots.txt, and — structurally —
        # could not see a re-walk, because the record it read is per store and a re-walk replaces
        # it. `requests_charged` accumulates instead. The name says what it is: the requests this
        # collection can ACCOUNT for, recomputable from `stores` by the recipe in
        # POLITENESS_POSTURE["request_accounting"].
        "requests_recorded": sum(
            int(s.get("requests_charged") or 0) or _requests_in(s) for s in stores
        ),
        # True when ANY store's `requests_charged` was synthesised from its surviving walk
        # rather than carried across walks, which makes the total above a floor rather than a
        # count. Published because the alternative is a sentence in a README claiming a
        # distinction the artifact does not record.
        "requests_recorded_is_floor": not all(
            s.get("requests_charged_accumulated") for s in stores
        ),
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


# The posture: what this collector DOES, as opposed to the numbers a particular run was given.
# Kept as a constant because it is a description of this source file, which means a corpus
# rebuilt by `build` gets the description that matches the code rather than the one the fetching
# run happened to write down. `retries` used to say "none — 403/404/429 is a recorded outcome,
# not something to retry around" flat out, and `fixtures/tests/test_real_catalogs.py` asserted
# that literal string; it stopped being true the day `--resume` landed, because a resume asks a
# 429 host again on the next run. Both halves are stated separately now, because they are
# different promises with different justifications.
POLITENESS_POSTURE: dict[str, Any] = {
    "robots_txt": "fetched and respected per host",
    "crawl_delay": "a declared Crawl-delay widens the interval and never narrows it",
    "retries": (
        "no retry loop within a run — a 403, 404 or 429 ends that store's walk and is recorded "
        "as its outcome, and nothing is re-requested. ACROSS runs, --resume re-walks a store "
        "whose recorded outcome was retryable (429, 5xx, transport error, unparseable body): a "
        "later run started by a person, never a loop against a host that just answered."
    ),
    "scope": "public catalogue data only",
    "concurrency": (
        "none — one host at a time, one request at a time. Measured: 8 concurrent "
        "connections across 30 Cloudflare-fronted zones drew an IP-wide 429 at request 41, "
        "5.9 seconds in; the same 80 zones serially cost 209 requests and zero blocks."
    ),
    "request_accounting": (
        "totals.requests_recorded is the sum of each store's `requests_charged`: one robots.txt "
        "fetch plus every catalogue page actually asked for. Every store carries the field, so "
        "its presence says nothing; `requests_charged_accumulated` is what says whether the "
        "number is real. True: the scratch record carried the count forward across every walk "
        "of that host. False: the scratch record predates that (collector 2.1.0 and earlier), "
        "so `build` synthesised the number from the walk that survived — a FLOOR for that host, "
        "because a re-walk replaced the record of the walk it replaced. "
        "totals.requests_recorded_is_floor is true when any store is in the second case. "
        "Recompute the total from the artifact: "
        "sum(s['requests_charged'] if 'requests_charged' in s else "
        "len([f for f in s['fetches'] if f['status'] != -1]) + bool(s['robots']) "
        "for s in collection['stores']) — the else branch is for manifests written before this "
        "field existed at all."
    ),
    "min_interval_floor_seconds": MIN_INTERVAL_FLOOR,
}


def politeness_block(args: argparse.Namespace) -> dict[str, Any]:
    return {
        **POLITENESS_POSTURE,
        "min_seconds_between_requests_per_host": args.min_interval,
        "max_requests_per_host": args.max_requests,
        "max_pages_per_host": args.max_pages,
        "page_size": args.page_size,
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
    parser.add_argument(
        "--min-interval",
        type=float,
        default=MIN_INTERVAL_FLOOR,
        help=(
            f"seconds between requests to one origin (default and FLOOR: "
            f"{MIN_INTERVAL_FLOOR}). Values below the floor are rejected."
        ),
    )
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
    if not is_polite_interval(args.min_interval):
        # Not clamped with a warning: a clamp makes the manifest and the traffic agree by
        # ignoring what was asked for, and the next reader cannot tell a clamped run from a
        # polite one. `--min-interval 0` used to be accepted in silence, and `--min-interval
        # nan` used to be accepted in silence AND disabled every pause in the walk.
        parser.error(
            f"--min-interval {args.min_interval} is not a finite interval at or above the "
            f"{MIN_INTERVAL_FLOOR}s politeness floor. These are real businesses and the floor "
            f"is the reason the egress IP still works; it can be widened, never narrowed."
        )
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
