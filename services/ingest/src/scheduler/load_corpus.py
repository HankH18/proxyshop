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

from ..adapters.recorded import RecordedCorpus, RecordedStore, RecordedTransport
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
    "build_parser",
    "corpus_registry",
    "corpus_target",
    "load_recorded_corpus",
    "main",
    "recorded_runner",
]


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

    Returns:
        A :class:`CorpusLoadReport`.

    Raises:
        ~ingest.adapters.recorded.CorpusIntegrityError: the corpus on disk does not reproduce
            what the live fetch recorded.
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
                adapter=SignedFetchAdapter(
                    client=transport, clock=clock, include_media=include_media
                ),
            )
        )

    summary: dict[str, Any] = {}
    if session_factory is not None:
        with session_factory() as session:
            summary = graph_summary(session)

    return CorpusLoadReport(
        corpus_root=recording.root,
        corpus_version=recording.corpus_version,
        collected_at=recording.collected_at,
        reports=tuple(reports),
        summary=summary,
        transports=tuple(transports),
    )


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
        could not be read or a store refused; ``2`` when the graph came back with provenance
        violations or unembedded products — both are states in which the load "succeeded"
        and the roster still cannot use the result.
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

    with _graph_lock(enabled=not args.no_lock):
        report = load_recorded_corpus(
            corpus,
            force=args.force,
            apply_schema_first=not args.no_schema,
            include_media=False if args.no_media else None,
        )
        probe_roster = _probe(args.probe, args.probe_limit) if args.probe else None

    print(
        f"corpus {report.corpus_version} collected {report.collected_at} from {report.corpus_root}"
    )
    for one in report.reports:
        print(
            f"  {one.store_id:30s} products={one.products:5d} changed={one.changed:5d} "
            f"ops={one.ops:6d} written={len(one.written):6d} embedded={one.embedded:5d}"
        )
    print(
        f"  {'TOTAL':30s} products={report.products:5d} ops={report.ops:6d} "
        f"written={report.written:6d} embedded={report.embedded:5d}"
    )
    for warning in report.warnings:
        print(f"  warning: {warning}", file=sys.stderr)

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
