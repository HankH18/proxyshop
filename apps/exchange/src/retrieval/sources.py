"""Where candidates come from: the Neo4j path, and the deterministic double (T-031).

:class:`CandidateSource` is the seam. The exchange's retrieval rule
(:mod:`~exchange.retrieval.criteria`) is deliberately independent of it, so the same R19
decision is asserted offline against a double and online against Neo4j.

* :class:`GraphCandidateSource` is the production path. WHICH products exist for an auction
  is decided entirely by T-012's :func:`ingest.graph.candidate_products` — "Neo4j vector +
  attributes" is that library's job, and re-implementing the retrieval query here would give
  the exchange a second, unversioned copy of it. The one statement this module owns,
  :data:`_VARIANT_NAMES`, cannot be that second copy: it is keyed on the product ids the
  first read returned, it carries no text parameter, and it can only describe a candidate
  more fully to a rule this module's own layer applies. Read its own note for why that
  distinction is what keeps DESIGN's "never match products by free-text name alone" true.
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
from dataclasses import dataclass, field
from typing import Any, Protocol

from ingest.embeddings import EmbeddingProvider, get_embedding_provider
from ingest.graph import Candidate, candidate_products
from ingest.graph.query import (
    DEFAULT_OVERSAMPLE,
    PLATFORM_OBSERVED_SOURCE_CLASSES,
)

from .criteria import RetrievalQuery

__all__ = [
    "LOCAL_FILTER_OVERSAMPLE",
    "VARIANT_NAMES_PER_PRODUCT",
    "CandidateSource",
    "GraphCandidateSource",
    "InMemoryCandidateSource",
    "VariantCandidate",
    "attribute_rows",
    "make_candidate",
]

#: How many rows to ask a source for, per requested result, when the query carries a
#: criterion the source cannot pre-filter on.
LOCAL_FILTER_OVERSAMPLE = 5

#: How many observed variant names one candidate may carry into
#: :func:`~exchange.retrieval.relevance.variant_surface`.
#:
#: A ceiling rather than "all of them", because the surface is tokenised per candidate on
#: every auction and a product's variant count is the store's choice, not this exchange's.
#:
#: **It truncates real rows in both corpora on disk and is not a formality**, which an earlier
#: printing of this note denied in the same sentence that stated its counterexample ("maximum
#: 100 — so this bound is above every product in it"; 64 is not above 100). Measured by
#: counting the ``variants`` array of every row in ``fixtures/*/stores/*.products.jsonl.gz``::
#:
#:     fixtures/real-catalogs        3,093 products   9,667 variants   mean 3.13   max 100
#:                                       1 product over this ceiling
#:     fixtures/real-catalogs-broad 17,409 products  97,217 variants   mean 5.58   max 250
#:                                      88 products over this ceiling
#:
#: So the cut is exercised by the shipped corpus, and ``test_the_ceiling_truncates_a_product
#: _with_more_variants_than_it_allows`` drives a product past it through the graph read rather
#: than asserting the number is in a range.
#:
#: Which names survive the cut is decided in Cypher by ``ORDER BY v.variant_id``, so the same
#: graph answers the same auction the same way; there is no "most relevant variants" ranking
#: here, because a surface that pre-selected by the query would be deciding relevance in the
#: fetch and then judging its own answer.
VARIANT_NAMES_PER_PRODUCT = 64


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


@dataclass(frozen=True)
class VariantCandidate(Candidate):
    """A :class:`ingest.graph.Candidate` carrying the variant names the platform observed.

    A SUBCLASS rather than a change to ``Candidate``, because ``Candidate`` is T-012's
    published shape and lives in ``services/ingest``: the exchange reads variant names for a
    surface of its own (:func:`~exchange.retrieval.relevance.variant_surface`) and nothing in
    the ingest library asks for them. It adds a field and overrides nothing, so every consumer
    of a ``Candidate`` keeps working unchanged.

    Both sources in this module produce these — :func:`make_candidate` always, and so
    :class:`InMemoryCandidateSource`; :class:`GraphCandidateSource` on every read that
    answered. A plain ``Candidate`` still reaches the pipeline from two places, and
    :func:`~exchange.retrieval.relevance.variant_surface` reads ``""`` for both rather than
    raising: the graph read that could not be made at all, and any other implementation of the
    :class:`CandidateSource` protocol, which is typed on ``Candidate`` and always will be.

    Attributes:
        variant_names: the names of the purchasable variants the PLATFORM observed for this
            product, in ``variant_id`` order, at most :data:`VARIANT_NAMES_PER_PRODUCT` of
            them. Never a seller's assertion: the read is gated on
            ``PLATFORM_OBSERVED_SOURCE_CLASSES``. Empty means the platform observed none —
            NOT that nothing looked, which is what a plain ``Candidate`` means.
    """

    variant_names: tuple[str, ...] = field(default=())


def make_candidate(
    record: Mapping[str, Any],
    *,
    scored: bool = True,
    provider: EmbeddingProvider | None = None,
    query_text: str = "",
) -> VariantCandidate:
    """Build one :class:`ingest.graph.Candidate` from a plain product record.

    Args:
        record: ``product_id`` and ``canonical_name`` are required; ``brand``, ``status``,
            ``attributes``, ``categories``, ``ingredients``, ``variant_names`` and
            ``similarity`` are optional.
        scored: whether this candidate came off a vector path. ``False`` reproduces the
            structured path exactly — ``score`` 0.0 and ``Candidate.cosine`` ``None``.
        provider: used to synthesise a similarity when the record does not pin one.
        query_text: what the similarity is measured against.

    Returns:
        The candidate, always a :class:`VariantCandidate` — with empty ``variant_names`` when
        the record states none, so a record that DOES state them drives the variant arm of the
        relevance rule offline, through the same field the graph path fills. ``similarity`` in
        the record is a **raw cosine** in ``[-1, 1]`` and is rescaled to the
        ``(1 + cosine) / 2`` form Neo4j's cosine index reports, so a test that pins a
        similarity is pinning the same number the graph would have produced.
    """
    similarity = record.get("similarity")
    if not scored:
        score = 0.0
    elif similarity is not None:
        score = (1.0 + float(similarity)) / 2.0
    else:
        # Resolve through the registry rather than naming a class. A concrete provider
        # named at a call site is invisible to `test_no_caller_names_a_concrete_provider_class`,
        # which scans `services/ingest/src` only -- so this line silently pinned the exchange
        # to the hash provider while the configured default moved to `lexical` (D56). That
        # would have made the swap config-only everywhere except the one place a shopper's
        # query is actually scored.
        resolved = provider or get_embedding_provider()
        score = (
            1.0 + _cosine(resolved.embed(query_text), resolved.embed(str(record["canonical_name"])))
        ) / 2.0
    return VariantCandidate(
        product_id=str(record["product_id"]),
        canonical_name=str(record["canonical_name"]),
        brand=str(record.get("brand", "")),
        status=str(record.get("status", "active")),
        score=min(max(score, 0.0), 1.0),
        categories=list(record.get("categories", ())),
        attributes=attribute_rows(record.get("attributes")),
        ingredients=list(record.get("ingredients", ())),
        scored=scored,
        variant_names=tuple(str(name) for name in record.get("variant_names", ()) or ()),
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)


#: The variant names of products the retrieval query already chose, as the PLATFORM observed
#: them.
#:
#: **It is keyed on ``$product_ids`` and that is what keeps it inside DESIGN's "never match
#: products by free-text name alone".** There is no ``CONTAINS``, no ``STARTS WITH`` and no
#: text parameter anywhere in it: the shopper's words never reach this statement. It cannot
#: put a product in front of a shopper — the vector index has already chosen which products
#: exist for this auction — it can only describe one of them more fully to the rule that
#: decides whether to keep it. Widening RECALL on variant text would need the variant words
#: inside ``ingest.graph.reembed.embedding_text``, which is where the vector this query cannot
#: see is composed.
#:
#: Both provenance gates are the ones ``ingest.graph.query.catalogue_entry`` uses, spelled out
#: here rather than imported because ``_observed_node``/``_observed_edge`` are that module's
#: privates: the ``Variant`` must be supported by a ``Source`` the platform authored, and so
#: must the ``HAS_VARIANT`` edge that attaches it to this product. Without the second gate a
#: store could attach its own words to somebody else's crawled product and have the platform
#: read them as its own observation, which is the D55 failure this whole surface exists on the
#: right side of.
_VARIANT_NAMES = """
MATCH (p:Product)-[hv:HAS_VARIANT]->(v:Variant)
WHERE p.product_id IN $product_ids
  AND v.name IS NOT NULL AND trim(v.name) <> ''
  AND EXISTS { MATCH (v)-[:SUPPORTED_BY]->(vs:Source)
               WHERE vs.source_class IN $source_classes }
  AND (hv.source_id IS NOT NULL AND EXISTS {
        MATCH (hs:Source {source_id: hv.source_id})
        WHERE hs.source_class IN $source_classes })
WITH p.product_id AS product_id, v
ORDER BY product_id, v.variant_id
WITH product_id, collect(DISTINCT v.name) AS names
RETURN product_id, names[..$per_product] AS variant_names
"""


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
        candidates = candidate_products(
            self.session,
            query_text=query.query_text or None,
            provider=self.provider,
            attribute_filters=query.attribute_filters,
            category=query.category,
            status=self.status,
            limit=fetch_limit,
            oversample=self.oversample,
        )
        return self._with_variant_names(candidates)

    def _with_variant_names(self, candidates: Sequence[Candidate]) -> Sequence[Candidate]:
        """Re-issue ``candidates`` carrying the variant names the platform observed.

        A SECOND read, keyed on the ids the first one returned, rather than a change to
        ``candidate_products``: that function is T-012's published retrieval query and the
        exchange has no business growing its projection for a surface only the exchange
        judges on. The cost is bounded by the first read — at most ``limit`` products, one
        expand each — not by the catalogue, and it is measured rather than argued. Six
        queries, five rounds each, against a private Neo4j holding seven stores of
        ``fixtures/real-catalogs-broad`` (``Product`` 4,253, ``Variant`` 33,110), reading
        :attr:`RetrievalResult.elapsed_ms` with this method replaced by the identity and then
        not::

            without this read   median 17.1 ms   max 22.9 ms
            with it             median 22.5 ms   max 41.7 ms

        against :data:`~exchange.retrieval.service.RETRIEVAL_LATENCY_BUDGET_MS` of 250. It is
        one round trip per retrieval, not one per candidate, which is the shape that budget is
        a regression guard against.

        Never raises into the caller's face and never widens what the exchange believes: a
        graph that will not answer this leaves every candidate exactly as
        ``candidate_products`` returned it, which is the pre-variant surface and therefore
        the pre-variant answer. Losing variant names costs reach; a raise here would cost the
        whole auction, and this read is the strictly less important of the two.
        """
        wanted = [candidate.product_id for candidate in candidates]
        if not wanted:
            return candidates
        try:
            rows = self.session.run(
                _VARIANT_NAMES,
                product_ids=wanted,
                source_classes=sorted(PLATFORM_OBSERVED_SOURCE_CLASSES),
                per_product=VARIANT_NAMES_PER_PRODUCT,
            ).data()
        except Exception:  # noqa: BLE001 — see the docstring: reach, not correctness
            return candidates
        names = {
            str(row["product_id"]): tuple(str(name) for name in row["variant_names"])
            for row in rows
        }
        # Every candidate is re-issued even when `names` is empty, and the empty case is the
        # one that matters: a graph whose variants are all `seller_asserted` returns no rows
        # here, and returning the originals then would hand back a plain `Candidate` — so
        # "the platform observed no variant names for this product" and "this source does not
        # read variant names" would be the same object, distinguishable only by which branch
        # produced it. They are different facts and this seam says so in the type.
        return [
            VariantCandidate(
                product_id=candidate.product_id,
                canonical_name=candidate.canonical_name,
                brand=candidate.brand,
                status=candidate.status,
                score=candidate.score,
                categories=list(candidate.categories),
                attributes=list(candidate.attributes),
                ingredients=list(candidate.ingredients),
                scored=candidate.scored,
                variant_names=names.get(candidate.product_id, ()),
            )
            for candidate in candidates
        ]


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
        self.provider = provider or get_embedding_provider()
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
