"""Turning pairwise decisions into ``SAME_AS`` edges (T-022, DESIGN §Data models).

:mod:`ingest.er.matching` decides about *a pair*. This module decides *which pairs to look
at* and *what to write*, which are the two questions that separate a scoring function from
entity resolution.

Blocking: why every pair is not compared
----------------------------------------
Comparing every pair is ``n(n-1)/2`` calls — 50 million for a ten-thousand-product catalog,
essentially all of them between a coffee grinder and a hiking sock. :func:`candidate_pairs`
indexes records under cheap keys and only proposes pairs that share one. A key is a *promise
of nothing*: a shared key means "worth scoring", never "matched". Every proposed pair still
goes through :func:`~ingest.er.matching.match` and clears the threshold on its own merits.

Over-large blocks are dropped rather than scored, and named in the report. A token shared by
four thousand records is not a discriminating key, it is a stopword the catalog happens to
use, and expanding it costs eight million comparisons to find approximately nothing. Dropping
it silently would be the defect; :attr:`ResolutionReport.dropped_blocks` is why it is not.

Edges are pairwise facts and are **not** transitively closed
------------------------------------------------------------
This is the decision most likely to be "improved" later, so it is defended here. Given
``A ~ B`` at 0.9 and ``B ~ C`` at 0.9, a union-find pass would emit ``A ~ C`` — a link no
scoring function ever agreed to, and one that can be *false by construction*: ``A`` and ``C``
may carry different valid GTINs, which :func:`~ingest.er.matching.match` vetoes outright.
Similarity is not transitive, and a resolver that closes over it manufactures exactly the
false merges its threshold exists to prevent. So no edge is ever *invented*: each one emitted
is a pair that was scored and cleared on its own. A consumer that wants a cluster traverses
``SAME_AS`` in Cypher, where the closure is the *query's* explicit choice and its cost is
visible.

That is necessary but not sufficient, because a consumer walking the edges gets the closure
whether the resolver blessed it or not. Take ``A`` and ``C`` carrying two different valid
GTINs — provably different trade items, and :func:`~ingest.er.matching.match` refuses them
outright — with ``B`` between them carrying no GTIN and a title that matches both. Scored
pairwise, ``A ~ B`` and ``B ~ C`` both clear the threshold, and any traversal of the two
edges then reaches the merge the GTIN veto exists to forbid. Vetoing the pair is not enough
when the path around it is two edges long.

So :func:`resolve` admits edges under a **consistency constraint**: strongest confidence
first, and an edge is withheld when joining its two sides would put two different canonical
GTINs into one connected component. Nothing is invented and nothing is closed over — an edge
is declined, and named in :attr:`ResolutionReport.withheld` with the identifiers that
contradicted it, so a withheld link is visible rather than merely absent.

One edge per pair, in a canonical direction
-------------------------------------------
:func:`~ingest.graph.upsert.link_same_as` writes a directed ``MERGE (a)-[:SAME_AS]->(b)``, so
calling it both ways round would leave two relationships for one symmetric fact and double
every count that walks them. Edges are therefore emitted with ``left_id < right_id`` always,
which makes re-running the resolver idempotent, and consumers match ``-[:SAME_AS]-``
undirected — the standard Neo4j idiom for a symmetric relationship.

``SAME_AS`` is in ``MATERIAL_FACT_EDGES``, so ``provenance_violations()`` audits it: an edge
whose ``source_id`` names no ``Source`` node is a reported defect. :func:`link_resolved`
therefore takes a real :class:`~ingest.graph.model.Source` and refuses to invent one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..graph.model import Source
from ..graph.upsert import link_same_as
from .identity import normalize_gtin, tokens
from .matching import (
    DEFAULT_MATCH_THRESHOLD,
    MatchDecision,
    match,
    read_field,
    record_name,
)

__all__ = [
    "DEFAULT_MAX_BLOCK_SIZE",
    "MIN_BLOCK_TOKEN_LENGTH",
    "ResolutionReport",
    "SameAsEdge",
    "blocking_keys",
    "candidate_pairs",
    "edges_as_json",
    "link_resolved",
    "resolve",
]

#: Blocks larger than this are dropped rather than expanded. A block of ``n`` records costs
#: ``n(n-1)/2`` comparisons, so the cost of *not* having this constant is quadratic and the
#: cost of having it is a named entry in the report.
DEFAULT_MAX_BLOCK_SIZE = 200

#: Tokens shorter than this make poor block keys: "xl", "2", "of" appear in a large fraction
#: of any catalog and buy no selectivity. A short-token-only record still blocks on its GTIN.
MIN_BLOCK_TOKEN_LENGTH = 3


@dataclass(frozen=True)
class SameAsEdge:
    """One ``(Product)-[:SAME_AS {confidence}]->(Product)`` the resolver wants written.

    Attributes:
        left_id: the lower ``product_id`` — see the module docstring on canonical direction.
        right_id: the higher ``product_id``.
        confidence: the score the edge carries, in ``[0, 1]``.
        method: which rule linked the pair, for audit. Not written to the graph today:
            :func:`~ingest.graph.upsert.link_same_as` exposes no passthrough for extra edge
            properties, and widening it is outside this ticket's ownership.
    """

    left_id: str
    right_id: str
    confidence: float
    method: str


@dataclass(frozen=True)
class ResolutionReport:
    """What one resolution pass proposed, and what it declined to look at.

    Attributes:
        edges: the linked pairs, ordered by ``(left_id, right_id)``.
        threshold: the floor every edge cleared.
        records: how many records were considered.
        compared: how many candidate pairs were actually scored.
        dropped_blocks: the block keys skipped for exceeding ``max_block_size``, sorted. A
            resolution pass that quietly ignored part of the catalog is indistinguishable
            from one that found nothing there, which is why this is reported and not logged.
        withheld: pairs that cleared the threshold but were refused because linking them
            would have joined two different canonical GTINs into one component. Reported for
            the same reason as ``dropped_blocks``: an invisible refusal is a bug report
            nobody files.
    """

    edges: tuple[SameAsEdge, ...] = ()
    threshold: float = DEFAULT_MATCH_THRESHOLD
    records: int = 0
    compared: int = 0
    dropped_blocks: tuple[str, ...] = field(default_factory=tuple)
    withheld: tuple[SameAsEdge, ...] = field(default_factory=tuple)


def blocking_keys(record: Any) -> frozenset[str]:
    """The cheap keys under which ``record`` is indexed for candidate generation.

    Two kinds, and they fail in different directions on purpose:

    * ``gtin:<canonical>`` — precise. Two records under it almost certainly match, and the
      identity rule will say so.
    * ``tok:<token>`` — recall-oriented. Any shared name word of at least
      :data:`MIN_BLOCK_TOKEN_LENGTH` characters proposes the pair, so a rename, a translated
      title or a different word order still gets scored as long as *one* word survives.

    Returns:
        The keys, possibly empty for a record with neither a usable GTIN nor a name.
    """
    keys = set()
    canonical_gtin = normalize_gtin(read_field(record, "gtin"))
    if canonical_gtin:
        keys.add(f"gtin:{canonical_gtin}")
    for token in tokens(record_name(record)):
        if len(token) >= MIN_BLOCK_TOKEN_LENGTH:
            keys.add(f"tok:{token}")
    return frozenset(keys)


def candidate_pairs(
    records: Sequence[Any], *, max_block_size: int = DEFAULT_MAX_BLOCK_SIZE
) -> tuple[list[tuple[int, int]], list[str]]:
    """Index ``records`` and propose the index pairs worth scoring.

    Args:
        records: the catalog slice to resolve. Positions are the identity used in the result,
            so duplicate ``product_id`` values do not collapse silently.
        max_block_size: blocks with more members than this are dropped.

    Returns:
        ``(pairs, dropped)`` — pairs as ``(i, j)`` with ``i < j``, sorted and unique, and the
        sorted keys of the blocks that were skipped. Sorted output is what makes the whole
        resolver deterministic: a set's iteration order is not a ranking, and letting it
        decide which pair is scored first would let it decide the report's contents.
    """
    blocks: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        for key in blocking_keys(record):
            blocks.setdefault(key, []).append(index)

    pairs: set[tuple[int, int]] = set()
    dropped: list[str] = []
    for key in sorted(blocks):
        members = blocks[key]
        if len(members) > max_block_size:
            dropped.append(key)
            continue
        for position, left in enumerate(members):
            for right in members[position + 1 :]:
                pairs.add((left, right) if left < right else (right, left))
    return sorted(pairs), dropped


class _GtinConsistentComponents:
    """Union-find over record positions that refuses a merge two GTINs contradict.

    Each component carries the canonical GTIN of its members, and by construction it can only
    ever carry one: a join that would give it a second is refused. The invariant is therefore
    checkable at a glance rather than argued about — no connected component of the emitted
    edges ever spans two different trade-item identifiers.
    """

    def __init__(self, records: Sequence[Any]) -> None:
        self._parent = list(range(len(records)))
        self._gtin = [normalize_gtin(read_field(record, "gtin")) for record in records]

    def _find(self, index: int) -> int:
        root = index
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[index] != root:  # path compression
            self._parent[index], index = root, self._parent[index]
        return root

    def join(self, left: int, right: int) -> bool:
        """Merge the two components, unless their GTINs contradict.

        Returns:
            ``True`` when the edge was admitted, ``False`` when it was withheld.
        """
        left_root, right_root = self._find(left), self._find(right)
        if left_root == right_root:
            return True
        left_gtin, right_gtin = self._gtin[left_root], self._gtin[right_root]
        if left_gtin and right_gtin and left_gtin != right_gtin:
            return False
        self._parent[right_root] = left_root
        self._gtin[left_root] = left_gtin or right_gtin
        return True


def resolve(
    records: Sequence[Any],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    *,
    max_block_size: int = DEFAULT_MAX_BLOCK_SIZE,
) -> ResolutionReport:
    """Resolve a catalog slice into the ``SAME_AS`` edges it justifies.

    Pure — this proposes edges, it does not write them; :func:`link_resolved` does that. The
    separation is what lets the resolution be inspected, diffed and tested without a database.

    Args:
        records: the catalog slice. Records from different stores is the point of the
            exercise; nothing here assumes one store.
        threshold: the SAME_AS floor, in ``(0.0, 1.0]``.
        max_block_size: passed through to :func:`candidate_pairs`.

    Returns:
        The :class:`ResolutionReport`. Self-pairs cannot occur (an index is never paired with
        itself), and a pair of records sharing one ``product_id`` is dropped rather than
        turned into a self-loop.

    Raises:
        ValueError: ``threshold`` is outside ``(0.0, 1.0]`` — see
            :func:`~ingest.er.matching.match`.
    """
    pairs, dropped = candidate_pairs(records, max_block_size=max_block_size)
    proposed: list[tuple[SameAsEdge, int, int]] = []
    for left_index, right_index in pairs:
        decision: MatchDecision = match(records[left_index], records[right_index], threshold)
        if not decision.linked:
            continue
        left_id, right_id = decision.pair
        if not left_id or not right_id or left_id == right_id:
            # A self-loop is not a resolution result, and an unnamed node cannot be written.
            continue
        proposed.append(
            (
                SameAsEdge(
                    left_id=left_id,
                    right_id=right_id,
                    confidence=decision.confidence,
                    method=decision.method,
                ),
                left_index,
                right_index,
            )
        )

    # Strongest first, ties broken by identifier so the admitted set never depends on the
    # order the caller happened to hand the records over in.
    proposed.sort(key=lambda item: (-item[0].confidence, item[0].left_id, item[0].right_id))
    components = _GtinConsistentComponents(records)
    edges: list[SameAsEdge] = []
    withheld: list[SameAsEdge] = []
    for edge, left_index, right_index in proposed:
        if components.join(left_index, right_index):
            edges.append(edge)
        else:
            withheld.append(edge)

    edges.sort(key=lambda edge: (edge.left_id, edge.right_id))
    withheld.sort(key=lambda edge: (edge.left_id, edge.right_id))
    return ResolutionReport(
        edges=tuple(edges),
        threshold=float(threshold),
        records=len(records),
        compared=len(pairs),
        dropped_blocks=tuple(dropped),
        withheld=tuple(withheld),
    )


def link_resolved(
    session: Any,
    records: Sequence[Any],
    *,
    source: Source,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    max_block_size: int = DEFAULT_MAX_BLOCK_SIZE,
) -> ResolutionReport:
    """Resolve ``records`` and write each linked pair as a provenanced ``SAME_AS`` edge.

    Args:
        session: an open Neo4j session.
        records: the catalog slice to resolve.
        source: the provenance for every edge written. ``SAME_AS`` is a material-fact edge, so
            ``provenance_violations()`` audits it and an unsourced edge is a reported defect;
            ``source_class="network"`` is the class the graph reserves for a fact this system
            derived rather than observed.
        threshold: the SAME_AS floor.
        max_block_size: passed through to :func:`candidate_pairs`.

    Returns:
        The same report :func:`resolve` produced, after every edge in it has been written.

    Raises:
        ProvenanceRequired: an endpoint ``Product`` does not exist. The graph refuses to MERGE
            a placeholder, so resolve products that have been upserted, not ones you hope are
            there.
    """
    report = resolve(records, threshold, max_block_size=max_block_size)
    for edge in report.edges:
        link_same_as(
            session,
            product_id=edge.left_id,
            other_id=edge.right_id,
            confidence=edge.confidence,
            source=source,
        )
    return report


def edges_as_json(edges: Iterable[SameAsEdge]) -> list[dict[str, Any]]:
    """The edges as plain JSON-able dicts, for the HTTP surface and for fixtures."""
    return [
        {
            "left_id": edge.left_id,
            "right_id": edge.right_id,
            "confidence": edge.confidence,
            "method": edge.method,
        }
        for edge in edges
    ]
