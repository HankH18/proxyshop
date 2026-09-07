"""``python -m ingest.scheduler.crawl`` — run a crawl in anger and read the graph back.

WHY THIS EXISTS. Every piece of catalog ingestion was reachable from a test or from an HTTP
request, and **nothing could run one from a shell**. ``refresh_store``'s own docstring
offered itself as the "one-shot convenience from a shell or a cron entry" and there was no
``__main__`` anywhere under ``ingest.scheduler`` to invoke it with. The measurable
consequence, on the live instance the compose stack has been running all along: ten
uniqueness constraints and twenty-six indexes, all ONLINE, ``MATCH (n) RETURN count(n)``
returning **0**, and ``CALL db.relationshipTypes()`` returning nothing at all. The schema had
never carried a row. A roster query against that graph answers "no shops" — correctly,
forever.

WHAT IT PRINTS, and why it is not just the reports. A crawl that says "11 written" is a claim
about a call that returned. This reads the graph back afterwards — node counts by label,
relationship counts by type, :func:`~ingest.graph.upsert.provenance_violations`, and the
products no vector index can see — because those are the three ways a full-looking graph
still rosters empty:

* an unsourced ``Store`` or ``SELLS`` edge, which ``candidate_shops`` drops entirely;
* an unsourced offer chain, which costs the shop its price while it stays on the roster;
* a product with no embedding, which no vector query can reach.

THE SSRF POSTURE IS NOT LOOSENED FROM THE ENVIRONMENT. ``FetchPolicy`` deliberately has no
environment variable and no test mode — "a guard with a global backdoor is a guard whose
default nobody can rely on". Reaching a storefront on loopback (the repo's own stub, a dev
box) therefore needs ``--allow-network 127.0.0.0/8`` typed into the command, where it is
visible in the process's own argv, rather than a variable some deployment inherits.

Typical use::

    export PROXYSHOP_INGEST_STORES='[{"store_id":"demo","base_url":"http://127.0.0.1:9100"}]'
    python -m ingest.scheduler.crawl --all --allow-network 127.0.0.0/8 --allow-any-port
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from ..adapters.netguard import FetchPolicy
from .catalog import (
    CatalogRefreshReport,
    CatalogRefreshRunner,
    StoreRegistry,
    UnknownStore,
    graph_session,
)

__all__ = ["build_parser", "crawl", "graph_summary", "main"]


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser.

    Returns:
        An ``ArgumentParser`` for ``python -m ingest.scheduler.crawl``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ingest.scheduler.crawl",
        description=(
            "Crawl the stores named in PROXYSHOP_INGEST_STORES into Neo4j, then read the "
            "graph back and report what a roster query would actually be able to see."
        ),
    )
    parser.add_argument(
        "store_ids",
        nargs="*",
        metavar="STORE_ID",
        help="stores to crawl; pass --all instead to crawl every registered store",
    )
    parser.add_argument(
        "--all",
        dest="all_stores",
        action="store_true",
        help=(
            "crawl every registered store. Required when no STORE_ID is named: a bare "
            "invocation of a crawler must not silently reach every configured merchant."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore the differential ledger and re-read every product",
    )
    parser.add_argument(
        "--no-schema",
        action="store_true",
        help=(
            "skip apply_schema(). The schema is idempotent and applying it first is what "
            "makes this command safe on a fresh database; skip it only when another process "
            "owns the schema."
        ),
    )
    parser.add_argument(
        "--allow-network",
        action="append",
        default=[],
        metavar="CIDR",
        help=(
            "re-admit a CIDR the SSRF guard blocks by default, e.g. 127.0.0.0/8 for a "
            "storefront served on loopback. Deliberately a flag and not an environment "
            "variable: the loosening belongs in the command that wanted it."
        ),
    )
    parser.add_argument(
        "--allow-any-port",
        action="store_true",
        help="permit any port, not only 80/443 — an ephemeral-port stub needs this",
    )
    return parser


def graph_summary(session: Any) -> dict[str, Any]:
    """Read back what the graph now holds, rather than what the crawl said it wrote.

    Args:
        session: an open ``neo4j.Session``.

    Returns:
        ``{"nodes": {label: count}, "relationships": {type: count},
        "provenance_violations": [...], "products_missing_embeddings": [...]}``.
    """
    from ..graph.query import products_missing_embeddings
    from ..graph.upsert import provenance_violations

    nodes = {
        row["label"]: row["count"]
        for row in session.run(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count ORDER BY label"
        ).data()
    }
    relationships = {
        row["type"]: row["count"]
        for row in session.run(
            "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY type"
        ).data()
    }
    return {
        "nodes": nodes,
        "relationships": relationships,
        "provenance_violations": [str(v) for v in provenance_violations(session)],
        "products_missing_embeddings": products_missing_embeddings(session),
    }


def crawl(
    store_ids: Sequence[str],
    *,
    policy: FetchPolicy,
    force: bool = False,
    apply_schema_first: bool = True,
) -> tuple[list[CatalogRefreshReport], dict[str, Any]]:
    """Crawl ``store_ids`` into the configured Neo4j and read the result back.

    Args:
        store_ids: registered store ids; empty means every registered store.
        policy: the SSRF posture for these crawls.
        force: ignore the differential ledger.
        apply_schema_first: apply the idempotent schema before crawling.

    Returns:
        ``(reports, summary)``.

    Raises:
        UnknownStore: a named store is not in ``PROXYSHOP_INGEST_STORES``.
    """
    from ..graph.schema import apply_schema

    warnings: list[str] = []
    registry = StoreRegistry.from_env(warnings=warnings)
    for problem in warnings:
        print(f"configuration: {problem}", file=sys.stderr)

    wanted = list(store_ids) or list(registry.store_ids)
    runner = CatalogRefreshRunner(registry=registry, policy=policy)

    if apply_schema_first:
        with graph_session() as session:
            apply_schema(session)

    reports = [runner.refresh(store_id, force=force) for store_id in wanted]
    with graph_session() as session:
        return reports, graph_summary(session)


def main(argv: Sequence[str] | None = None) -> int:
    """Run a crawl and print what the graph holds afterwards.

    Args:
        argv: command-line arguments; ``sys.argv[1:]`` when omitted.

    Returns:
        ``0`` when every store crawled cleanly and the graph reads back clean; ``1`` when a
        store was unknown or unreachable; ``2`` when the graph itself came back with
        provenance violations or unembedded products, because both are states in which the
        crawl "succeeded" and the roster still cannot use the result.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.store_ids and not args.all_stores:
        parser.error("name at least one STORE_ID, or pass --all to crawl every registered store")
    policy = FetchPolicy(
        allowed_ports=None if args.allow_any_port else FetchPolicy().allowed_ports,
        extra_allowed_networks=tuple(args.allow_network),
    )
    try:
        reports, summary = crawl(
            args.store_ids,
            policy=policy,
            force=args.force,
            apply_schema_first=not args.no_schema,
        )
    except UnknownStore as exc:
        print(f"unknown store: {exc}", file=sys.stderr)
        return 1

    if not reports:
        print("no stores configured in PROXYSHOP_INGEST_STORES; nothing to crawl")
        return 1

    failed = False
    for report in reports:
        print(
            f"{report.store_id}: {report.products} product(s) read, {report.changed} changed, "
            f"{report.ops} op(s), {len(report.written)} written, {report.embedded} embedded "
            f"[{report.adapter}] {report.snapshot_ref}"
        )
        for warning in report.warnings:
            failed = True
            print(f"  warning: {warning}", file=sys.stderr)

    print(f"graph nodes:         {summary['nodes']}")
    print(f"graph relationships: {summary['relationships']}")
    print(f"provenance violations: {len(summary['provenance_violations'])}")
    print(f"products missing embeddings: {len(summary['products_missing_embeddings'])}")

    if failed:
        return 1
    if summary["provenance_violations"] or summary["products_missing_embeddings"]:
        # Both states pass every write-side check and still make the roster wrong: an
        # unsourced fact is dropped from it, an unembedded product is unreachable by it.
        for violation in summary["provenance_violations"]:
            print(f"  provenance violation: {violation}", file=sys.stderr)
        for product_id in summary["products_missing_embeddings"]:
            print(
                f"  no embedding, unreachable by every vector query: {product_id}", file=sys.stderr
            )
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
