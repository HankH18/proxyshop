"""``python -m ingest.scheduler.load_corpus`` — put the ten recorded storefronts in the graph.

WHAT WAS WRONG. ``fixtures/real-catalogs/`` records 3,093 products from ten real supplement
storefronts, verbatim, with per-record provenance. ``ingest.scheduler.crawl`` can crawl a
storefront into Neo4j. **Nothing had ever connected the two**, so every graph demo in this
repository ran on ``fixtures/catalog/coffee.json`` — a generator config with four product
families, one attribute template and one category. Entity resolution, claim extraction and
the roster query had never met an empty ``product_type``, a hundred-variant product, or a
store that stocks none of what the shopper asked for.

WHAT THIS IS, and what it deliberately is not. It is **not** a corpus importer. It replaces
the transport under :class:`~ingest.adapters.signed_fetch.SignedFetchAdapter` with
:class:`~ingest.adapters.recorded.RecordedTransport` and then runs the ordinary
:class:`~ingest.scheduler.catalog.CatalogRefreshRunner` — the same robots posture, the same
pagination, the same ``products.json`` parse, the same identity rules, the same
``build_upserts``, the same ``apply_upserts``, the same provenance, the same embed. A
second write path would have produced a graph no crawl can produce, which is the failure
mode "real data in the graph" exists to avoid.

NO SOCKET. :mod:`ingest.adapters.recorded` imports no networking library at all, so there is
nothing here that *could* reach a storefront (D3/C9). The recorded bytes are checked against
the digest the live fetch took over the whole response before a single one reaches the
adapter, so a corrupted corpus fails at load rather than becoming graph rows.

A SHORT LOAD IS A FAILURE, NOT A WARNING. This module used to hand back a report that looked
identical whether every store had loaded or a store had loaded nothing. Measured on
``fixtures/real-catalogs-broad``, four of its 38 stores — ``sabai.design`` (321 products),
``branchfurniture.com`` (220), ``cotopaxi.com`` (1,432) and ``tenthousand.cc`` (76) — read
**zero**, because their ``products.json`` pages are bigger than the live crawl's per-response
byte ceiling and the replay was being metered against it. 2,049 products were dropped, the
loss was a line in ``report.warnings``, and ``main()`` printed a clean table and exited ``0``.
So the ceiling moved (:func:`~ingest.adapters.recorded.replay_budget` — a page already fetched,
digest-checked and committed is not a body arriving from a host) and, more importantly, the
silence went: :func:`shortfalls_in` compares what each store put in the graph against the row
count its committed recording holds, and :func:`load_recorded_corpus` raises
:class:`CorpusLoadShortfall` rather than return. That comparison is deliberately blind to
*why* a store came up short, because the byte ceiling was only the instance — a robots
refusal, an unparseable page or an entry with no id all ended the same way.

IDEMPOTENCE HAS TWO HALVES and this exercises both.

* **Differential.** A runner that has already loaded a store feeds its own ``hash_index``
  back in, ``changed_products`` is empty, and the second run computes and writes zero ops —
  it never even opens a graph session. That is the cheap half.
* **Convergent.** ``--force`` drops the remembered hashes, so every product is re-read and
  every op is replayed against the real uniqueness constraints. The node and relationship
  counts must come back identical. That is the half that actually grades ``MERGE``, and it
  is the one a hash ledger can hide.

WHERE THE CORPUS HAS TO BE, and what happens when it is not. ``services/ingest/Dockerfile``
copies ``services/ingest/src/``, ``proxyshop_support/`` and ``packages/llm/`` — **not**
``fixtures/``. This module still imports cleanly inside that image (nothing here reads the
corpus at import time), and a run there fails at the first call with a ``FileNotFoundError``
naming the missing directory and the ``PROXYSHOP_RECORDED_CATALOGS`` override, rather than
with a mystery. It is stated because "works in the tree, absent from the image" is a defect
class this repository has been bitten by repeatedly: this is a development and demo loader,
and shipping the 2.7 MB corpus is a deliberate decision nobody has made yet.

Typical use::

    python -m ingest.scheduler.load_corpus --all
    python -m ingest.scheduler.load_corpus --all --force --probe "milk thistle for liver support"
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..adapters.recorded import (
    RecordedCorpus,
    RecordedStore,
    RecordedTransport,
    replay_budget,
)
from ..adapters.signed_fetch import SignedFetchAdapter
from .catalog import (
    SIGNED_FETCH,
    CatalogRefreshReport,
    CatalogRefreshRunner,
    StoreRegistry,
    StoreTarget,
    graph_session,
    observed_now,
)
from .crawl import graph_summary

__all__ = [
    "CorpusLoadReport",
    "CorpusLoadShortfall",
    "CorpusShortfall",
    "build_parser",
    "corpus_registry",
    "corpus_target",
    "load_recorded_corpus",
    "main",
    "recorded_runner",
    "shortfalls_in",
]


@dataclass(frozen=True)
class CorpusShortfall:
    """One store that put fewer products in the graph than the recording holds.

    Attributes:
        host: the store.
        recorded: how many products the corpus holds for it — the row count of its
            ``products.jsonl.gz``, verified row-aligned against its provenance at load.
        loaded: how many the replay actually read.
        warnings: that store's refresh warnings, verbatim, which is where the reason is.
    """

    host: str
    recorded: int
    loaded: int
    warnings: tuple[str, ...] = ()

    @property
    def missing(self) -> int:
        """How many products the corpus holds and the load did not read."""
        return max(0, self.recorded - self.loaded)

    def __str__(self) -> str:
        head = (
            f"{self.host}: the corpus holds {self.recorded} product(s); the load read "
            f"{self.loaded} — {self.missing} missing"
        )
        return "\n".join((head, *(f"    {warning}" for warning in self.warnings)))


class CorpusLoadShortfall(RuntimeError):
    """A load finished with at least one store short of what the recording holds.

    Raised rather than warned, and that is the whole point of the class. The failure it
    replaces was measured on ``fixtures/real-catalogs-broad``: four stores loaded **zero**
    products because their ``products.json`` pages exceeded a crawl-budget ceiling, the loss
    was recorded as a line in ``report.warnings``, and the load printed a clean table and
    exited ``0``. A store with 1,430 products in the corpus and none in the graph is
    indistinguishable from a store that stocks nothing — and every downstream measurement was
    then taken on a corpus nobody had been told was short.

    Attributes:
        shortfalls: one :class:`CorpusShortfall` per store that came up short.
        report: the :class:`CorpusLoadReport` for the load that raised, so the caller keeps
            everything that *did* load and everything measured about it. The graph writes
            already happened; this is a refusal to call the result complete, not a rollback.
    """

    def __init__(
        self, shortfalls: Sequence[CorpusShortfall], report: CorpusLoadReport | None = None
    ) -> None:
        self.shortfalls = tuple(shortfalls)
        self.report = report
        missing = sum(one.missing for one in self.shortfalls)
        super().__init__(
            f"{len(self.shortfalls)} store(s) loaded fewer products than the corpus holds "
            f"({missing} product(s) missing):\n" + "\n".join(str(one) for one in self.shortfalls)
        )


def shortfalls_in(
    corpus: RecordedCorpus, reports: Sequence[CatalogRefreshReport]
) -> tuple[CorpusShortfall, ...]:
    """Every store in ``reports`` that read fewer products than ``corpus`` holds for it.

    The comparison is against the recording rather than against a threshold, which is what
    makes it able to tell the two readings of a zero apart: ``products_recorded`` is the row
    count of that store's committed ``products.jsonl.gz``, checked row-aligned against its
    provenance by :meth:`~ingest.adapters.recorded.RecordedCorpus.load`. A store that really
    stocks nothing records nothing and is not a shortfall; a store with 1,430 rows on disk and
    none in the graph is.

    It is deliberately blind to *why*. The byte ceiling was one cause; a robots refusal, an
    unparseable page, a page size the recording cannot answer and an entry carrying no id all
    end the same way, and every one of them should be as loud as the ceiling was not.
    """
    short: list[CorpusShortfall] = []
    for report in reports:
        try:
            recorded = corpus.by_host(report.store_id).products_recorded
        except KeyError:  # pragma: no cover - a report for a store not in this corpus
            continue
        if report.products < recorded:
            short.append(
                CorpusShortfall(
                    host=report.store_id,
                    recorded=recorded,
                    loaded=report.products,
                    warnings=tuple(report.warnings),
                )
            )
    return tuple(short)


def corpus_target(store: RecordedStore) -> StoreTarget:
    """The :class:`~ingest.scheduler.catalog.StoreTarget` that replays one recorded store.

    Three fields are decided here rather than defaulted, and each for a reason the recording
    forces:

    ``store_id``
        the host. Already unique, already the thing the corpus is keyed by, and stable
        across re-collections — so a re-recorded catalogue lands on the same nodes.

    ``max_products``
        at least 250, because the collector paginated at ``limit=250`` and the recorded byte
        spans are of *those* pages. ``SignedFetchAdapter`` asks for
        ``min(250, max_products)``, so a smaller ceiling would request a page size that was
        never served, and :class:`~ingest.adapters.recorded.RecordedTransport` refuses it
        rather than slicing a recorded page into a response nobody sent.

    ``fetch_product_pages``
        ``False``. The corpus is ``products.json`` and ``robots.txt``; it holds no product
        HTML, so there is no JSON-LD to read. Leaving it on would produce one refusal warning
        per product — 3,093 of them — for pages that were never recorded.
    """
    return StoreTarget(
        store_id=store.store_id,
        base_url=store.origin,
        source=SIGNED_FETCH,
        max_products=max(250, store.products_recorded),
        fetch_product_pages=False,
    )


def corpus_registry(corpus: RecordedCorpus) -> StoreRegistry:
    """A registry holding every store in ``corpus``."""
    return StoreRegistry(corpus_target(store) for store in corpus.stores)


def recorded_runner(
    corpus: RecordedCorpus,
    *,
    session_factory: Any = graph_session,
    clock: Any = observed_now,
) -> CatalogRefreshRunner:
    """A refresh runner whose adapters read the recording instead of the network.

    The runner is the ordinary one. What is swapped is per-store: each ``refresh`` call is
    handed a :class:`~ingest.adapters.signed_fetch.SignedFetchAdapter` built on that store's
    :class:`~ingest.adapters.recorded.RecordedTransport`. Everything downstream of
    ``client.fetch`` is the live path.

    The runner's default budget is :func:`~ingest.adapters.recorded.replay_budget` over the
    whole corpus, so a caller that reaches for ``runner.refresh(host)`` directly — as the
    differential half of the idempotence demonstration does — gets a ceiling that fits the
    recording rather than the live network posture. :func:`load_recorded_corpus` narrows that
    to a per-store budget at each ``refresh`` call.

    Args:
        corpus: the loaded recording.
        session_factory: a context manager yielding a graph session, or ``None`` to compute
            the writes without performing them.
        clock: the observation-timestamp source.

    Returns:
        A runner registered for every store in ``corpus``.
    """
    return CatalogRefreshRunner(
        registry=corpus_registry(corpus),
        session_factory=session_factory,
        budget=replay_budget(corpus),
        clock=clock,
    )


@dataclass(frozen=True)
class CorpusLoadReport:
    """What one load of the recording did, and what the graph holds afterwards."""

    corpus_root: Path
    corpus_version: str
    collected_at: str
    reports: tuple[CatalogRefreshReport, ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)
    transports: tuple[RecordedTransport, ...] = ()
    shortfalls: tuple[CorpusShortfall, ...] = ()
    recorded: int = 0

    @property
    def complete(self) -> bool:
        """Whether every store put everything the corpus holds for it into the graph."""
        return not self.shortfalls

    @property
    def products(self) -> int:
        """How many products the replay read across every store."""
        return sum(report.products for report in self.reports)

    @property
    def written(self) -> int:
        """How many graph ids the replay wrote."""
        return sum(len(report.written) for report in self.reports)

    @property
    def embedded(self) -> int:
        """How many of those products retrieval can now see."""
        return sum(report.embedded for report in self.reports)

    @property
    def ops(self) -> int:
        """How many graph writes the snapshots implied."""
        return sum(report.ops for report in self.reports)

    @property
    def warnings(self) -> tuple[str, ...]:
        """Every warning any store's refresh raised, prefixed with the store."""
        return tuple(
            f"{report.store_id}: {warning}"
            for report in self.reports
            for warning in report.warnings
        )

    @property
    def urls_requested(self) -> tuple[str, ...]:
        """Every URL the replay was asked for, in order.

        The evidence that the pagination loop really ran: a load that had been handed a list
        of products instead of driving the crawl would show no ``?limit=250&page=2``.
        """
        return tuple(url for transport in self.transports for url in transport.requested)


def load_recorded_corpus(
    corpus: RecordedCorpus | None = None,
    *,
    hosts: Sequence[str] = (),
    root: Path | str | None = None,
    session_factory: Any = graph_session,
    runner: CatalogRefreshRunner | None = None,
    force: bool = False,
    apply_schema_first: bool = True,
    clock: Any = observed_now,
    include_media: bool | None = None,
    require_complete: bool = True,
) -> CorpusLoadReport:
    """Replay the recorded corpus into the graph and read the result back.

    Args:
        corpus: an already-loaded recording. Loaded from ``root``/``hosts`` when omitted.
        hosts: load only these hosts. Empty means every recorded store.
        root: the corpus directory, when ``corpus`` is not supplied.
        session_factory: a context manager yielding a graph session.
        runner: reuse this runner, which is how the differential half of idempotence is
            demonstrated — a runner remembers what it hashed last time. A fresh one is built
            per call otherwise.
        force: ignore the remembered hashes and re-read every product, so the *convergent*
            half is exercised against the real uniqueness constraints.
        apply_schema_first: apply the idempotent schema before writing.
        clock: the observation-timestamp source.
        include_media: write ``MediaAsset`` nodes and ``HAS_MEDIA`` edges. ``None`` defers
            to :func:`~ingest.adapters.mapping.media_enabled` — the environment, whose
            default is on — so an unmodified call loads exactly what it always loaded.
        require_complete: raise :class:`CorpusLoadShortfall` when any store read fewer
            products than the corpus holds for it. On by default because the alternative is
            the bug this parameter exists to close: a load that drops a whole store and
            returns a report that looks exactly like a whole one. Pass ``False`` only when a
            partial load is what you asked for, and read ``report.shortfalls``.

    Returns:
        A :class:`CorpusLoadReport`. Every store's budget is
        :func:`~ingest.adapters.recorded.replay_budget` for that store — the ceilings the
        recording needs, not the live network posture, which is what used to refuse four of
        the 38 stores in ``fixtures/real-catalogs-broad`` outright.

    Raises:
        ~ingest.adapters.recorded.CorpusIntegrityError: the corpus on disk does not reproduce
            what the live fetch recorded.
        CorpusLoadShortfall: ``require_complete`` and at least one store came up short. The
            graph writes have already happened; the exception carries the report.
    """
    from ..graph.schema import apply_schema  # noqa: PLC0415 - keeps the driver lazy

    recording = corpus if corpus is not None else RecordedCorpus.load(root, hosts=hosts)
    active = recorded_runner(recording, session_factory=session_factory, clock=clock)
    if runner is not None:
        # A caller-supplied runner is the point of the differential demonstration: it is the
        # hash ledger that has to survive between the two loads. Its registry is replaced so
        # the second load reads the same targets the first one did.
        active = runner
        active.registry = corpus_registry(recording)

    if apply_schema_first and session_factory is not None:
        with session_factory() as session:
            apply_schema(session)

    reports: list[CatalogRefreshReport] = []
    transports: list[RecordedTransport] = []
    for store in recording.stores:
        transport = RecordedTransport(store=store)
        transports.append(transport)
        reports.append(
            active.refresh(
                store.store_id,
                force=force,
                # Sized to THIS store's recording. A page already read off disk and checked
                # against the digest the live fetch took over it is not a response arriving
                # from a host, and metering it against the live per-response ceiling only
                # ever refused inventory this repository owns — see `replay_budget`.
                budget=replay_budget(store),
                adapter=SignedFetchAdapter(
                    client=transport, clock=clock, include_media=include_media
                ),
            )
        )

    summary: dict[str, Any] = {}
    if session_factory is not None:
        with session_factory() as session:
            summary = graph_summary(session)

    shortfalls = shortfalls_in(recording, reports)
    report = CorpusLoadReport(
        corpus_root=recording.root,
        corpus_version=recording.corpus_version,
        collected_at=recording.collected_at,
        reports=tuple(reports),
        summary=summary,
        transports=tuple(transports),
        shortfalls=shortfalls,
        recorded=recording.products_recorded,
    )
    if shortfalls and require_complete:
        raise CorpusLoadShortfall(shortfalls, report)
    return report


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser for ``python -m ingest.scheduler.load_corpus``."""
    parser = argparse.ArgumentParser(
        prog="python -m ingest.scheduler.load_corpus",
        description=(
            "Replay the recorded storefront corpus in fixtures/real-catalogs/ through the "
            "live crawl path into Neo4j, then read the graph back. Opens no socket."
        ),
    )
    parser.add_argument(
        "hosts",
        nargs="*",
        metavar="HOST",
        help="recorded hosts to load; pass --all instead to load every recorded store",
    )
    parser.add_argument(
        "--all",
        dest="all_stores",
        action="store_true",
        help="load every recorded store. Required when no HOST is named.",
    )
    parser.add_argument(
        "--corpus",
        default=None,
        metavar="DIR",
        help="the recorded corpus directory (default: fixtures/real-catalogs)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "re-read every product even if this process already hashed it, which is what "
            "makes a second run grade MERGE rather than the hash ledger"
        ),
    )
    parser.add_argument(
        "--no-schema",
        action="store_true",
        help="skip apply_schema(); only when another process owns the schema",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help=(
            "do not take the D37 cross-process Neo4j flock. Only when you own the database: "
            "measured, a concurrent graph test wiped a full-corpus load halfway through and "
            "the load reported success over a graph holding one store"
        ),
    )
    parser.add_argument(
        "--no-media",
        dest="no_media",
        action="store_true",
        help=(
            "do not write MediaAsset nodes or HAS_MEDIA edges. Measured on the ten-store "
            "corpus they are 15.0%% of the load's statements and the only reason for 17,520 "
            "of its existence probes, and nothing in this repository reads them yet — but "
            "the whole point of them is a media rule that is not built, so this is opt-in "
            "and the default writes them"
        ),
    )
    parser.add_argument(
        "--allow-partial",
        dest="allow_partial",
        action="store_true",
        help=(
            "keep going when a store puts fewer products in the graph than the corpus holds "
            "for it, instead of refusing the load. Still prints every short store and still "
            "exits non-zero (3) — the point is that a short load can never be mistaken for a "
            "whole one, not that it is forbidden"
        ),
    )
    parser.add_argument(
        "--probe",
        default=None,
        metavar="TEXT",
        help=(
            "after loading, run candidate_shops() with this shopper text and print the "
            "roster — the read side of the load, on real data"
        ),
    )
    parser.add_argument(
        "--probe-limit",
        type=int,
        default=10,
        metavar="N",
        help="how many shops the probe prints (default: 10)",
    )
    return parser


@contextmanager
def _session() -> Iterator[Any]:
    with graph_session() as session:
        yield session


@contextmanager
def _graph_lock(*, enabled: bool) -> Iterator[None]:
    """Hold D37's cross-process Neo4j flock for the whole load, or explicitly do not.

    Neo4j Community has one database (D4/D37), and the test fixtures reset it on **every**
    lock acquisition. A four-minute unlocked load therefore races every graph test on the
    machine, and it does not lose loudly: measured on this repository, a concurrent lane
    wiped a whole-corpus load partway through and the loader printed a clean report over a
    graph that then held one store's products. A load that can be silently emptied is worse
    than no load, so this is on by default.

    Imported lazily so that importing this module never needs ``proxyshop_support``, which
    the deployable ingest image is not obliged to ship (see
    ``proxyshop_support/tests/test_artifact_copyset.py``).
    """
    if not enabled:
        yield
        return
    from proxyshop_support.neo4j_lock import neo4j_flock  # noqa: PLC0415 - see docstring

    with neo4j_flock():
        yield


def _probe(query: str, limit: int) -> list[Any]:
    """Run the roster query on the loaded graph, inside the lock the load still holds."""
    from ..graph.query import candidate_shops  # noqa: PLC0415 - keeps the driver lazy

    with _session() as session:
        return candidate_shops(session, query_text=query, limit=limit)


def _print_probe(query: str, limit: int, roster: list[Any]) -> None:
    """Print what the roster query came back with."""
    print(f"\ncandidate_shops(query_text={query!r}, limit={limit}) -> {len(roster)} shop(s)")
    for rank, shop in enumerate(roster, start=1):
        price = "no observed price" if shop.lowest_price is None else f"{shop.lowest_price:.2f}"
        cosine = "n/a" if shop.best_cosine is None else f"{shop.best_cosine:.4f}"
        print(
            f"  {rank:2d}. {shop.store_id:30s} cosine={cosine} "
            f"cheapest={price:>18s} products={len(shop.product_ids)} via={','.join(shop.via)}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Load the recorded corpus and print what the graph holds afterwards.

    Returns:
        ``0`` when every store loaded and the graph reads back clean; ``1`` when the corpus
        could not be read, a store refused, or a store put fewer products in the graph than
        the corpus holds for it; ``2`` when the graph came back with provenance violations or
        unembedded products; ``3`` when a store came up short and ``--allow-partial`` said to
        keep going anyway. Every one of ``1``, ``2`` and ``3`` is a state in which the load
        can look like it worked and the result cannot be used as if it had.
    """
    args = build_parser().parse_args(argv)
    if not args.hosts and not args.all_stores:
        print("name at least one HOST, or pass --all", file=sys.stderr)
        return 1

    try:
        corpus = RecordedCorpus.load(args.corpus, hosts=tuple(args.hosts))
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"corpus: {exc}", file=sys.stderr)
        return 1
    if not corpus.stores:
        print(f"corpus: no recorded stores matched {args.hosts or 'the corpus'}", file=sys.stderr)
        return 1

    probe_roster: list[Any] | None = None
    with _graph_lock(enabled=not args.no_lock):
        try:
            report = load_recorded_corpus(
                corpus,
                force=args.force,
                apply_schema_first=not args.no_schema,
                include_media=False if args.no_media else None,
                require_complete=not args.allow_partial,
            )
        except CorpusLoadShortfall as exc:
            # The report is on the exception precisely so a short load still prints
            # everything a whole one prints. What it must never do is print it and exit 0.
            report = (
                exc.report
                if exc.report is not None
                else CorpusLoadReport(
                    corpus_root=corpus.root,
                    corpus_version=corpus.corpus_version,
                    collected_at=corpus.collected_at,
                )
            )
        else:
            probe_roster = _probe(args.probe, args.probe_limit) if args.probe else None

    print(
        f"corpus {report.corpus_version} collected {report.collected_at} from {report.corpus_root}"
    )
    for one in report.reports:
        recorded = corpus.by_host(one.store_id).products_recorded
        flag = "  SHORT" if one.products < recorded else ""
        print(
            f"  {one.store_id:30s} products={one.products:5d}/{recorded:<5d} "
            f"changed={one.changed:5d} ops={one.ops:6d} written={len(one.written):6d} "
            f"embedded={one.embedded:5d}{flag}"
        )
    print(
        f"  {'TOTAL':30s} products={report.products:5d}/{report.recorded:<5d} "
        f"ops={report.ops:6d} written={report.written:6d} embedded={report.embedded:5d}"
    )
    for warning in report.warnings:
        print(f"  warning: {warning}", file=sys.stderr)

    if report.shortfalls:
        missing = sum(short.missing for short in report.shortfalls)
        print(
            f"\nREFUSED: {len(report.shortfalls)} store(s) put fewer products in the graph than "
            f"{report.corpus_root} holds for them — {missing} product(s) missing. This is not "
            f"a load of the corpus and must not be measured as one.",
            file=sys.stderr,
        )
        for short in report.shortfalls:
            print(f"  {short}", file=sys.stderr)
        return 3 if args.allow_partial else 1

    print("\ngraph:")
    print(f"  nodes         {report.summary.get('nodes')}")
    print(f"  relationships {report.summary.get('relationships')}")
    violations = report.summary.get("provenance_violations") or []
    unembedded = report.summary.get("products_missing_embeddings") or []
    print(f"  provenance violations      {len(violations)}")
    print(f"  products missing embedding {len(unembedded)}")

    if args.probe:
        _print_probe(args.probe, args.probe_limit, probe_roster or [])

    if violations or unembedded:
        for line in list(violations)[:20]:
            print(f"  violation: {line}", file=sys.stderr)
        for pid in list(unembedded)[:20]:
            print(f"  unembedded: {pid}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
