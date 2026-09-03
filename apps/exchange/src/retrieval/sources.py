"""Where candidates come from: the Neo4j path, and the deterministic double (T-031).

:class:`CandidateSource` is the seam. The exchange's retrieval rule
(:mod:`~exchange.retrieval.criteria`) is deliberately independent of it, so the same R19
decision is asserted offline against a double and online against Neo4j.

* :class:`GraphCandidateSource` is the production path. It is a thin adapter over T-012's
  :func:`ingest.graph.candidate_products` — "Neo4j vector + attributes" is that library's
  job, and re-implementing the Cypher here would give the exchange a second, unversioned
  copy of the retrieval query.
* :class:`InMemoryCandidateSource` is the double A2 requires ("deterministic test doubles
  for reranker and embeddings in all verifies"). It answers with the rows it was given and
  applies **no** filtering, which is the point: a double that pre-filtered would make the
  exchange's own hard-criteria filter unobservable.

Oversampling, and why it is not optional. ``in`` and ``contains`` have no ``AttributeFilter``
spelling, so a graph query carrying one of them is narrower than the intent: Cypher applies
``LIMIT n`` to rows that have *not* been tested against those criteria, and the local
decision then discards some of them. Asking for ``limit`` rows in that case would return
fewer than ``limit`` satisfying candidates whenever any row failed. :data:`LOCAL_FILTER_OVERSAMPLE`
is the multiplier that buys the headroom back.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from ingest.embeddings import EmbeddingProvider, HashEmbedding
from ingest.graph import Candidate, candidate_products
from ingest.graph.query import DEFAULT_OVERSAMPLE

from .criteria import RetrievalQuery

__all__ = [
    "LOCAL_FILTER_OVERSAMPLE",
    "CandidateSource",
    "GraphCandidateSource",
    "InMemoryCandidateSource",
    "attribute_rows",
    "make_candidate",
]

#: How many rows to ask a source for, per requested result, when the query carries a
#: criterion the source cannot pre-filter on.
LOCAL_FILTER_OVERSAMPLE = 5


class CandidateSource(Protocol):
    """Anything that can answer a :class:`RetrievalQuery` with candidate products."""

    name: str

    def fetch(self, query: RetrievalQuery) -> Sequence[Candidate]: ...


def attribute_rows(raw: Any) -> list[dict[str, Any]]:
    """Normalise attributes into the shape :attr:`ingest.graph.Candidate.attributes` uses.

    Accepts what a fixture or a test naturally writes and produces what the graph projects:
    ``{key, value_string, value_number, value_bool, unit}``.

    Args:
        raw: either an already-projected sequence of attribute mappings, or a mapping of
            ``key -> value``. A list value explodes into one row per element (which is how
            the graph stores a multi-valued attribute: several ``AttributeValue`` nodes
            sharing a key). A mapping value may carry ``value`` and ``unit``.

    Returns:
        The projected rows. Never raises on an unfamiliar scalar — it is stringified, which
        is what an unmodelled catalog value would do on the graph path too.
    """
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        rows: list[dict[str, Any]] = []
        for key, value in raw.items():
            rows.extend(_rows_for(str(key), value))
        return rows
    projected: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            projected.append(
                {
                    "key": str(entry.get("key", "")),
                    "value_string": entry.get("value_string"),
                    "value_number": entry.get("value_number"),
                    "value_bool": entry.get("value_bool"),
                    "unit": entry.get("unit"),
                }
            )
            continue
        projected.append(
            {
                "key": str(getattr(entry, "key", "")),
                "value_string": getattr(entry, "value_string", None),
                "value_number": getattr(entry, "value_number", None),
                "value_bool": getattr(entry, "value_bool", None),
                "unit": getattr(entry, "unit", None),
            }
        )
    return projected


def _rows_for(key: str, value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping) and "value" in value:
        return _rows_for_scalar(key, value["value"], unit=value.get("unit"))
    if isinstance(value, (list, tuple, set, frozenset)):
        rows: list[dict[str, Any]] = []
        for element in value:
            rows.extend(_rows_for(key, element))
        return rows
    return _rows_for_scalar(key, value, unit=None)


def _rows_for_scalar(key: str, value: Any, *, unit: Any) -> list[dict[str, Any]]:
    row: dict[str, Any] = {
        "key": key,
        "value_string": None,
        "value_number": None,
        "value_bool": None,
        "unit": None if unit is None else str(unit),
    }
    # bool before number, for the same reason `HardCriterion._equals` does it: `True` is an
    # `int`, and storing it as a number makes `eq True` unsatisfiable.
    if isinstance(value, bool):
        row["value_bool"] = value
    elif isinstance(value, (int, float)):
        row["value_number"] = float(value)
    elif value is not None:
        row["value_string"] = str(value)
    return [row]


def make_candidate(
    record: Mapping[str, Any],
    *,
    scored: bool = True,
    provider: EmbeddingProvider | None = None,
    query_text: str = "",
) -> Candidate:
    """Build one :class:`ingest.graph.Candidate` from a plain product record.

    Args:
        record: ``product_id`` and ``canonical_name`` are required; ``brand``, ``status``,
            ``attributes``, ``categories``, ``ingredients`` and ``similarity`` are optional.
        scored: whether this candidate came off a vector path. ``False`` reproduces the
            structured path exactly — ``score`` 0.0 and ``Candidate.cosine`` ``None``.
        provider: used to synthesise a similarity when the record does not pin one.
        query_text: what the similarity is measured against.

    Returns:
        The candidate. ``similarity`` in the record is a **raw cosine** in ``[-1, 1]`` and is
        rescaled to the ``(1 + cosine) / 2`` form Neo4j's cosine index reports, so a test that
        pins a similarity is pinning the same number the graph would have produced.
    """
    similarity = record.get("similarity")
    if not scored:
        score = 0.0
    elif similarity is not None:
        score = (1.0 + float(similarity)) / 2.0
    else:
        resolved = provider or HashEmbedding()
        score = (
            1.0 + _cosine(resolved.embed(query_text), resolved.embed(str(record["canonical_name"])))
        ) / 2.0
    return Candidate(
        product_id=str(record["product_id"]),
        canonical_name=str(record["canonical_name"]),
        brand=str(record.get("brand", "")),
        status=str(record.get("status", "active")),
        score=min(max(score, 0.0), 1.0),
        categories=list(record.get("categories", ())),
        attributes=attribute_rows(record.get("attributes")),
        ingredients=list(record.get("ingredients", ())),
        scored=scored,
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)


class GraphCandidateSource:
    """The Neo4j path: vector similarity **and** attribute nodes, through T-012's library."""

    name = "neo4j"

    def __init__(
        self,
        session: Any,
        *,
        provider: EmbeddingProvider | None = None,
        oversample: int = DEFAULT_OVERSAMPLE,
        status: str | None = "active",
    ) -> None:
        """
        Args:
            session: an open ``neo4j.Session``.
            provider: the :class:`~ingest.embeddings.EmbeddingProvider` the query text is
                embedded with. Defaults to the configured one, which is what keeps the
                provider swap config-only on the read side (T-012 acceptance 3).
            oversample: index rows fetched per requested result, passed straight through.
            status: product status filter; ``"active"`` so discontinued products cannot
                silently enter a shortlist.
        """
        self.session = session
        self.provider = provider
        self.oversample = oversample
        self.status = status

    def fetch(self, query: RetrievalQuery) -> Sequence[Candidate]:
        """Ask Neo4j for candidates, with headroom for the criteria it cannot apply.

        Raises:
            ingest.graph.UnretrievableQuery: the intent carried neither query text to embed
                nor any predicate this source can push down — e.g. a single ``contains``
                constraint and no category. That is deliberately **not** softened into "fetch
                the whole catalog and post-filter": scanning every product to evaluate a
                substring is the free-text name match DESIGN forbids, wearing a different
                name. Give the intent a category or query text.
        """
        fetch_limit = query.limit
        if query.local_only_criteria:
            fetch_limit = query.limit * LOCAL_FILTER_OVERSAMPLE
        return candidate_products(
            self.session,
            query_text=query.query_text or None,
            provider=self.provider,
            attribute_filters=query.attribute_filters,
            category=query.category,
            status=self.status,
            limit=fetch_limit,
            oversample=self.oversample,
        )


class InMemoryCandidateSource:
    """The deterministic double: answers with the rows it holds, filtering nothing.

    Filtering nothing is deliberate and is what makes the acceptance-1 assertions meaningful.
    The retrieval layer's job is to return hard-criteria-satisfying candidates *whatever the
    source returned*; a double that applied the pushed-down filters itself would leave that
    job untested and a filter that had been deleted would still look green.
    """

    name = "in-memory"

    def __init__(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        scored: bool = True,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        self.records = [dict(record) for record in records]
        self.scored = scored
        self.provider = provider or HashEmbedding()
        #: Every query this source was asked, in order — so a test can assert on the
        #: pushdown without needing a graph to observe it.
        self.queries: list[RetrievalQuery] = []

    def fetch(self, query: RetrievalQuery) -> Sequence[Candidate]:
        self.queries.append(query)
        return [
            make_candidate(
                record,
                scored=self.scored,
                provider=self.provider,
                query_text=query.query_text,
            )
            for record in self.records
        ]
