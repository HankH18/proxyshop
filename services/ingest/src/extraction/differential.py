"""Content-hash-driven re-extraction: unchanged pages cost nothing (T-021, C6).

The ticket's third acceptance criterion is a claim about a *count*: "re-run on unchanged
pages performs no LLM calls (double counts)". That is only checkable if the decision to
extract is taken before the extractor is reached, and if the extractor keeps a counter. So
the decision lives here, in front of everything, and it is a pure hash comparison:
:func:`needs_extraction` opens no socket, calls no model, and reads no clock.

The hash function is T-020's :func:`ingest.adapters.hashing.content_hash` — re-exported,
not reimplemented. Two reasons that matters. The digest carries its ``"sha256:"`` prefix,
so a stored hash names its own algorithm and a future migration fails loudly instead of
silently reporting "unchanged". And the fetcher already stamps every ``FetchedResource``
and ``HTTPResult`` with exactly this digest, so a hash the crawler recorded and a hash the
extractor computes are comparable without anyone converting between two conventions.

**The ledger is a bounded cache, not a log (T-367).** It used to be two plain dicts with no
capacity, no TTL and no sweep, living at module scope in :mod:`ingest.extraction.routes`,
fed by an unauthenticated endpoint whose caller chose both the key and the retained value.
Entry count was therefore exactly ``n`` after ``n`` distinct pages, for every ``n``, for the
life of the interpreter — growth on *ordinary* traffic, not merely under attack, inside a
256 MiB container. It is now an LRU bounded on both axes that can actually run away: how
many pages it remembers (:data:`MAX_LEDGER_PAGES`) and how many bytes of extracted text
those pages' results hold (:data:`MAX_LEDGER_BYTES`). Both axes are needed. Capping entries
alone bounds the ledger's *length* and not its *size*, because one entry retains the
evidence sentences of one page and a caller picks how long those are.

Eviction is least-recently-used, and "used" means what a cache means by it: recording a page
and *hitting* it in :meth:`ExtractionLedger.cached` both mark it fresh, so a page under
steady traffic is the last thing evicted rather than the first. That is what preserves the
guarantee the ledger exists for — an unchanged page performs no extraction — while making
the structure's footprint independent of how long the process has been up.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

from ..adapters.hashing import content_hash, has_changed, snapshot_ref
from .claims import ExtractionResult

__all__ = [
    "MAX_LEDGER_BYTES",
    "MAX_LEDGER_PAGES",
    "ExtractionLedger",
    "content_hash",
    "has_changed",
    "needs_extraction",
    "snapshot_ref",
]

#: How many pages the in-process differential ledger remembers before the least recently
#: used one is evicted.
#:
#: Where the number comes from: one crawl of one storefront may fetch at most
#: ``CrawlBudget.max_pages`` (200, ``ingest/adapters/budgets.py``) pages, so 512 is more than
#: two full crawls' worth of distinct policy pages — the ledger keeps every page any
#: realistic working set actually re-visits, which is the only traffic a differential cache
#: can pay for. The ceiling exists for the other caller: ``POST /extraction/policy-pages`` is
#: unauthenticated and its caller names the page, so without a cap the entry count is
#: whatever that caller decides. The T-367 reproduction measured 1,435-1,482 bytes retained
#: per honest request, so 512 entries is roughly 0.75 MiB against ``compose.yaml``'s
#: ``mem_limit: 256m``.
MAX_LEDGER_PAGES = 512

#: How many bytes of retained extraction *content* the ledger will hold before evicting the
#: least recently used page, regardless of how few pages that is.
#:
#: Where the number comes from: ``compose.yaml`` runs this service under ``mem_limit: 256m``,
#: and a cache is not entitled to most of a process's memory. 16 MiB is 1/16 of that limit,
#: which leaves the ledger a footprint an operator can ignore while still holding the whole
#: extracted text of far more pages than a storefront has. This axis is what keeps the entry
#: cap honest: an ``ExtractionResult`` retains the evidence sentence behind every claim, and
#: a page is as long as whoever sent it decided, so ``MAX_LEDGER_PAGES`` alone would bound
#: 512 entries of unbounded size. The T-367 reproduction measured 61,479 bytes retained per
#: *hostile* request; this budget makes that arithmetic terminate.
MAX_LEDGER_BYTES = 16 * 1024 * 1024

#: Charged on top of the strings an entry actually holds, to cover the per-object cost the
#: string lengths do not see: dict slots in three mappings, and the tuple and dataclass
#: headers of a result. CPython's own floor for a small object is 48-64 bytes, so 256 is a
#: deliberate over-estimate — a retention budget that under-counts is not a budget.
_ENTRY_OVERHEAD_BYTES = 256

#: Same reasoning, per dataclass the walk descends into: an ``ExtractedClaim`` is a frozen
#: dataclass with nine fields, a span tuple and a ``ClaimProvenance``, and every one of them
#: costs whether or not it holds text. Charged once per record — so a claim and the
#: provenance inside it are charged 128 each — which over-estimates on purpose.
_CLAIM_OVERHEAD_BYTES = 128


def _text_cost(value: Any) -> int:
    """Bytes to charge for one retained field. Cheap, and never raises on a hostile value."""
    if value is None:
        return 0
    if isinstance(value, str | bytes):
        return len(value)
    if isinstance(value, bool | int | float):
        return 8
    try:
        return len(str(value))
    except Exception:  # pragma: no cover - a value whose __str__ throws is still chargeable
        return _CLAIM_OVERHEAD_BYTES


#: How deep the cost walk descends before it stops charging. A result holds claims, a claim
#: holds a provenance, a provenance holds strings — three levels. Six is double that, and the
#: cap exists so a cost function reached from a public ``record()`` cannot recurse forever on
#: a shape nobody anticipated.
_MAX_COST_DEPTH = 6


def _dataclass_field_names(value: Any) -> tuple[str, ...]:
    """Every field of a dataclass *instance*, or ``()`` for anything else. Never raises."""
    try:
        if is_dataclass(value) and not isinstance(value, type):
            return tuple(entry.name for entry in fields(value))
    except Exception:  # pragma: no cover - a hostile __class__ is still chargeable
        return ()
    return ()


def _retained_cost(value: Any, depth: int = 0) -> int:
    """What one retained value costs the budget, derived from its shape rather than listed.

    This used to be three hand-written tuples of field names — one for a claim, one for a
    result, one implied for a provenance. It charged what somebody had remembered to type,
    which is the same failure the request-side ceilings had: a claim's ``provenance`` was in
    none of the lists, so a request carrying a 30,000-character ``observed_at`` retained
    90,072 bytes of it (once on the result's provenance and once on each claim's) while this
    budget charged the entry 981 bytes. A budget blind to a retained field is not a budget;
    a budget blind to it *because the field was added after the list was written* is the
    failure mode, and a list cannot be fixed by adding one more name to it.

    So the walk is derived: a dataclass is charged for every field ``dataclasses.fields``
    reports, which means a field added to :class:`~ingest.extraction.claims.ExtractedClaim`
    or :class:`~ingest.extraction.claims.ClaimProvenance` is charged from the moment it
    exists.

    Deliberately tolerant, and deliberately narrow about what it will iterate. ``record`` is
    public and its tests hand it sentinels, so a cost function that raised on one would turn
    a cache write into a 500 on the endpoint that made it; and only concrete containers are
    walked, never arbitrary iterables, so a generator or an endless iterator cannot make
    accounting the expensive part of a request.

    Args:
        value: whatever is being retained.
        depth: recursion depth, capped at :data:`_MAX_COST_DEPTH`.

    Returns:
        A byte estimate that over-counts rather than under-counts.
    """
    if value is None or depth > _MAX_COST_DEPTH:
        return 0
    if isinstance(value, str | bytes):
        return len(value)
    if isinstance(value, bool | int | float):
        return 8
    names = _dataclass_field_names(value)
    if names:
        return _CLAIM_OVERHEAD_BYTES + sum(
            _retained_cost(getattr(value, name, None), depth + 1) for name in names
        )
    if isinstance(value, Mapping):
        return sum(
            _retained_cost(key, depth + 1) + _retained_cost(item, depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple | set | frozenset):
        return sum(_retained_cost(item, depth + 1) for item in value)
    return _text_cost(value)


def _claim_cost(claim: Any) -> int:
    """What one retained claim costs the budget — every field it holds, provenance included."""
    return _retained_cost(claim)


def _result_cost(result: Any) -> int:
    """What one retained :class:`ExtractionResult` costs the budget.

    Deliberately tolerant of things that are not results. ``record`` is public and its tests
    hand it sentinels; a cost function that raised on one would turn a cache write into a 500
    on the endpoint that made it.
    """
    if result is None:
        return 0
    return _ENTRY_OVERHEAD_BYTES + _retained_cost(result)


def needs_extraction(previous_hash: str | None, content: str | bytes) -> bool:
    """Whether ``content`` must be (re-)extracted, given what was last seen.

    Args:
        previous_hash: the digest recorded the last time this page was extracted, or
            ``None``/empty when it has never been seen.
        content: the page as fetched now.

    Returns:
        ``False`` only when the content hashes to exactly what was recorded before —
        which is the "zero re-extraction work" case. ``True`` for a page never seen: with
        nothing to compare against, the only safe answer is to do the work.
    """
    return has_changed(previous_hash, content_hash(content))


@dataclass
class ExtractionLedger:
    """What has already been extracted, keyed by page reference. Bounded, least-recently-used.

    A scheduler (T-024) will persist this; in-process it is a pair of ordered mappings behind
    an LRU. Either way the contract is the same three questions: what hash did we last see
    for this page, does the current content differ, and what did the last run produce.

    The fourth question, which the plain-dict version could not answer, is *how big does this
    get*. It gets exactly as big as ``max_pages`` entries or ``max_bytes`` of retained
    extraction content, whichever binds first; past either, the least recently used page is
    dropped — hash and result together, so the expensive half never outlives the cheap half
    that indexes it. Both ceilings are instance attributes rather than reads of the module
    constants, so a caller with a different memory posture (a batch job, a test) can set its
    own without monkeypatching a module.

    Attributes:
        hashes: ``{page_ref: content_hash}`` for every page currently remembered, in
            least-recently-used-first order. An ``OrderedDict``, so it compares equal to the
            plain dict callers used to hold and ``clear()`` still means clear.
        results: the last :class:`~ingest.extraction.claims.ExtractionResult` per page.
            Held so an unchanged page can be answered from cache rather than re-extracted
            — the cache hit *is* the zero-work guarantee, and returning the old claims is
            what makes it usable rather than merely cheap. A result too large to fit the
            whole budget on its own is not retained at all; its hash still is, so the page
            is still recognised as unchanged, it is merely re-extracted rather than replayed.
        max_pages: entry ceiling. Defaults to :data:`MAX_LEDGER_PAGES`.
        max_bytes: retained-content ceiling. Defaults to :data:`MAX_LEDGER_BYTES`.
    """

    hashes: OrderedDict[str, str] = field(default_factory=OrderedDict)
    results: OrderedDict[str, ExtractionResult] = field(default_factory=OrderedDict)
    max_pages: int = MAX_LEDGER_PAGES
    max_bytes: int = MAX_LEDGER_BYTES
    _costs: dict[str, int] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Accept plain dicts from callers and constructors that predate the LRU."""
        if not isinstance(self.hashes, OrderedDict):
            self.hashes = OrderedDict(self.hashes)
        if not isinstance(self.results, OrderedDict):
            self.results = OrderedDict(self.results)
        self._costs = {}
        self._evict()

    def known_hash(self, page_ref: str) -> str | None:
        """The digest last recorded for ``page_ref``, or ``None``.

        Deliberately does **not** refresh recency: this is also the read
        :func:`needs_extraction` makes before deciding to extract, and it is reached while
        other code holds an iterator over ``hashes``. Marking a page fresh belongs on the
        paths that are unambiguously a cache use — :meth:`record` and :meth:`cached`.
        """
        return self.hashes.get(str(page_ref))

    def needs_extraction(self, page_ref: str, content: str | bytes) -> bool:
        """Whether ``content`` differs from what was last recorded for ``page_ref``."""
        return needs_extraction(self.known_hash(page_ref), content)

    def cached(self, page_ref: str, digest: str) -> ExtractionResult | None:
        """The recorded result for ``page_ref``, but only if ``digest`` still matches.

        Guarding on the digest is what stops a stale cache from answering for changed
        content: the result is keyed by the page, and validated by the hash. A hit also marks
        the page as most recently used, which is what makes eviction discard the pages
        nothing is asking about rather than the ones under load.
        """
        key = str(page_ref)
        if has_changed(self.known_hash(key), digest):
            return None
        result = self.results.get(key)
        if result is not None:
            self._touch(key)
        return result

    def record(self, page_ref: str, digest: str, result: ExtractionResult | None = None) -> None:
        """Record that ``page_ref`` was extracted at ``digest``, evicting if that overflows.

        Args:
            page_ref: the page this run read.
            digest: its content hash.
            result: what the run produced. Omitted when only the hash matters.
        """
        key = str(page_ref)
        self.hashes[key] = str(digest)
        self.hashes.move_to_end(key)
        cost = len(key) + len(str(digest)) + _result_cost(result)
        if result is not None:
            if cost > self.max_bytes:
                # One page cannot spend the whole budget: keep the hash, drop the result.
                self.results.pop(key, None)
                cost = len(key) + len(str(digest))
            else:
                self.results[key] = result
                self.results.move_to_end(key)
        self._costs[key] = cost
        self._evict()

    def forget(self, page_ref: str) -> None:
        """Drop everything recorded for ``page_ref``, forcing the next run to extract."""
        key = str(page_ref)
        self.hashes.pop(key, None)
        self.results.pop(key, None)
        self._costs.pop(key, None)

    def hash_index(self) -> dict[str, str]:
        """The recorded hashes, shaped for a ``CatalogRequest.known_hashes``."""
        return dict(self.hashes)

    def retained_bytes(self) -> int:
        """What the ledger is currently charging itself for, in bytes.

        The number :data:`MAX_LEDGER_BYTES` bounds. An estimate — it counts the text an entry
        holds plus a per-object over-estimate, not CPython's exact heap footprint — but an
        estimate that is monotone in the thing that actually grows, which is what a budget
        needs to be.
        """
        self._reconcile()
        return sum(self._costs.values())

    def _touch(self, key: str) -> None:
        """Mark ``key`` most recently used, on both mappings that hold it."""
        if key in self.hashes:
            self.hashes.move_to_end(key)
        if key in self.results:
            self.results.move_to_end(key)

    def _reconcile(self) -> None:
        """Re-sync the cost table with the mappings.

        ``hashes`` and ``results`` are public and callers do mutate them directly — the
        scheduler clears the whole ledger on a forced refresh
        (``ingest/scheduler/routes.py``). A budget that only ever went up would then evict
        forever, so the accounting is rebuilt from what is actually held rather than trusted.
        """
        for key in [key for key in self._costs if key not in self.hashes]:
            del self._costs[key]
        for key in self.hashes:
            if key not in self._costs:
                self._costs[key] = (
                    len(key) + len(self.hashes[key]) + _result_cost(self.results.get(key))
                )
        for key in [key for key in self.results if key not in self.hashes]:
            del self.results[key]

    def _evict(self) -> None:
        """Drop least-recently-used pages until both ceilings hold."""
        self._reconcile()
        total = sum(self._costs.values())
        while self.hashes and (len(self.hashes) > self.max_pages or total > self.max_bytes):
            oldest, _ = next(iter(self.hashes.items()))
            total -= self._costs.pop(oldest, 0)
            self.hashes.pop(oldest, None)
            self.results.pop(oldest, None)
