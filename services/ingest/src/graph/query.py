"""Candidate retrieval: vector similarity **and** attribute nodes (T-012, acceptance 2).

DESIGN, twice over: retrieval is "Neo4j vector + attributes", and products are "never
matched by free-text name alone". Both clauses are enforced here rather than trusted:

* :func:`candidate_products` takes a ``query_text`` only to **embed** it. There is no
  parameter that matches a name, no ``CONTAINS``/``STARTS WITH`` over ``canonical_name``
  anywhere in this module, and adding one would fail
  ``test_graph.py::test_no_module_matches_products_by_free_text_name``.
* A call carrying neither a vector nor a structured predicate raises
  :class:`UnretrievableQuery` instead of degrading into "return everything", which is what
  a name scan is when the caller cannot articulate a constraint.

Why attribute filtering is a post-filter over collected attributes rather than a set of
correlated ``EXISTS`` subqueries: the filters arrive as a **list parameter**, and a subquery
that has to reference the element variable of an enclosing ``all(f IN $filters …)`` is not
something Cypher 5 supports. Collecting each candidate's attributes once and evaluating the
predicate list against that collection is plainly correct, needs no per-filter statement
generation (which would defeat the query cache), and costs one extra expand over the
already-narrow top-k set.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..embeddings import EmbeddingProvider, get_embedding_provider
from .model import canonical_text, category_id, ingredient_id
from .schema import VECTOR_INDEX_NAME

#: How many rows to pull out of the vector index per requested result before the structured
#: filters are applied. A product excluded by an attribute filter still occupies a slot in
#: the index's top-k, so asking for exactly ``limit`` would under-fill any filtered query.
#:
#: KNOWN BOUND, stated rather than buried. ``db.index.vector.queryNodes`` takes a ``k``, not
#: a predicate, so this library **post-filters** a vector top-k: a product that satisfies
#: every structured filter but ranks below ``limit * oversample`` by cosine is not returned.
#: Raising ``oversample`` widens the window at linear cost; the alternative — filter first,
#: then rank — is not expressible against Neo4j's vector index at all. For a *highly
#: selective* structured query (one rare attribute, one ingredient), call
#: :func:`candidate_products` with **no vector**: the structured path is a plain ``MATCH``
#: and has no top-k bound. Pinned by
#: ``test_vector_path_recall_is_bounded_by_the_index_fetch_and_the_structured_path_is_not``.
DEFAULT_OVERSAMPLE = 8

#: Ceiling on the index fetch, so a large ``limit`` with a large oversample cannot turn a
#: candidate query into a full scan.
MAX_INDEX_FETCH = 1000


class UnretrievableQuery(ValueError):
    """A retrieval request with no vector and no structured predicate.

    Raised rather than answered, because the only way to satisfy such a request would be a
    free-text scan over ``Product.canonical_name`` — which DESIGN forbids.
    """


@dataclass(frozen=True)
class AttributeFilter:
    """One structured predicate against an ``AttributeValue`` node.

    A filter matches a product when the product has **any** attribute whose key matches and
    whose value satisfies every component that was supplied. Components left ``None`` are
    not tested, so ``AttributeFilter("spf", min_number=30)`` reads "SPF at least 30".
    """

    key: str
    value_string: str | None = None
    value_bool: bool | None = None
    equals_number: float | None = None
    min_number: float | None = None
    max_number: float | None = None
    unit: str | None = None

    def as_parameter(self) -> dict[str, Any]:
        """The mapping form the Cypher predicate consumes.

        Returns:
            A plain mapping; ``key``/``value_string``/``unit`` are canonicalised so that
            ``"SPF"`` matches an attribute stored as ``"spf"``.
        """
        return {
            "key": canonical_text(self.key),
            "value_string": None
            if self.value_string is None
            else canonical_text(self.value_string),
            "value_bool": self.value_bool,
            "equals_number": None if self.equals_number is None else float(self.equals_number),
            "min_number": None if self.min_number is None else float(self.min_number),
            "max_number": None if self.max_number is None else float(self.max_number),
            "unit": None if self.unit is None else canonical_text(self.unit),
        }


@dataclass(frozen=True)
class Candidate:
    """One retrieved product."""

    product_id: str
    canonical_name: str
    brand: str
    status: str
    score: float
    categories: list[str] = field(default_factory=list)
    attributes: list[dict[str, Any]] = field(default_factory=list)
    ingredients: list[str] = field(default_factory=list)


#: Shared tail: collect each candidate's attribute/ingredient/category context once, then
#: evaluate every structured predicate against that collection. ``$attribute_filters`` is a
#: list of maps, so one cached plan serves every filter combination.
_FILTER_AND_RETURN = """
OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:AttributeValue)
WITH p, score, collect(DISTINCT {
    key: a.canonical_key,
    raw_key: a.key,
    value_string: a.value_string,
    canonical_value_string: a.canonical_value_string,
    value_number: a.value_number,
    value_bool: a.value_bool,
    unit: a.unit,
    canonical_unit: a.canonical_unit
}) AS attrs
OPTIONAL MATCH (p)-[:CONTAINS]->(i:Ingredient)
WITH p, score, attrs, collect(DISTINCT i.ingredient_id) AS ingredient_ids,
     collect(DISTINCT i.name) AS ingredient_names
OPTIONAL MATCH (p)-[:IN_CATEGORY]->(c:Category)
WITH p, score, attrs, ingredient_ids, ingredient_names,
     collect(DISTINCT c.category_id) AS category_ids, collect(DISTINCT c.name) AS category_names
WHERE ($brand IS NULL OR p.brand = $brand)
  AND ($status IS NULL OR p.status = $status)
  AND ($category_id IS NULL OR $category_id IN category_ids)
  AND all(want IN $ingredients_all WHERE want IN ingredient_ids)
  AND none(avoid IN $ingredients_none WHERE avoid IN ingredient_ids)
  AND all(f IN $attribute_filters WHERE any(a IN attrs WHERE
        a.key = f.key
        AND (f.value_string IS NULL OR a.canonical_value_string = f.value_string)
        AND (f.value_bool IS NULL OR a.value_bool = f.value_bool)
        AND (f.equals_number IS NULL OR a.value_number = f.equals_number)
        AND (f.min_number IS NULL OR (a.value_number IS NOT NULL AND a.value_number >= f.min_number))
        AND (f.max_number IS NULL OR (a.value_number IS NOT NULL AND a.value_number <= f.max_number))
        AND (f.unit IS NULL OR a.canonical_unit = f.unit)
  ))
RETURN p.product_id AS product_id,
       p.canonical_name AS canonical_name,
       coalesce(p.brand, '') AS brand,
       coalesce(p.status, '') AS status,
       score,
       [x IN attrs WHERE x.raw_key IS NOT NULL | {
           key: x.raw_key,
           value_string: x.value_string,
           value_number: x.value_number,
           value_bool: x.value_bool,
           unit: x.unit
       }] AS attributes,
       [x IN ingredient_names WHERE x IS NOT NULL] AS ingredients,
       [x IN category_names WHERE x IS NOT NULL] AS categories
ORDER BY score DESC, product_id ASC
LIMIT $limit
"""

_VECTOR_HEAD = f"""
CALL db.index.vector.queryNodes('{VECTOR_INDEX_NAME}', $fetch, $embedding)
YIELD node AS p, score
WITH p, score
"""

#: The structured-only path. ``score`` is 0.0 and the ordering falls back to ``product_id``,
#: which keeps the result deterministic — a retrieval whose order depends on storage order
#: is not reproducible, and every ranking assertion downstream would be flaky.
_STRUCTURED_HEAD = """
MATCH (p:Product)
WITH p, 0.0 AS score
"""


def _run(
    session: Any,
    head: str,
    *,
    parameters: dict[str, Any],
) -> list[Candidate]:
    """Execute a candidate query and materialise the rows.

    Args:
        session: an open ``neo4j.Session``.
        head: the retrieval head (vector or structured).
        parameters: the query parameters.

    Returns:
        The candidates, best score first.
    """
    rows = session.run(head + _FILTER_AND_RETURN, **parameters).data()
    return [
        Candidate(
            product_id=row["product_id"],
            canonical_name=row["canonical_name"],
            brand=row["brand"],
            status=row["status"],
            score=float(row["score"]),
            categories=list(row["categories"]),
            attributes=[dict(attribute) for attribute in row["attributes"]],
            ingredients=list(row["ingredients"]),
        )
        for row in rows
    ]


def candidate_products(
    session: Any,
    *,
    query_text: str | None = None,
    embedding: Sequence[float] | None = None,
    provider: EmbeddingProvider | None = None,
    attribute_filters: Sequence[AttributeFilter] = (),
    category: str | None = None,
    ingredients_all: Sequence[str] = (),
    ingredients_none: Sequence[str] = (),
    brand: str | None = None,
    status: str | None = "active",
    limit: int = 10,
    oversample: int = DEFAULT_OVERSAMPLE,
) -> list[Candidate]:
    """Retrieve candidate products by vector similarity and attribute structure.

    Args:
        session: an open ``neo4j.Session``.
        query_text: text to embed with the configured provider. Used **only** as embedding
            input — it is never compared against a product name.
        embedding: a pre-computed query vector, used verbatim when given. Takes precedence
            over ``query_text``.
        provider: the provider used to embed ``query_text``. Defaults to
            :func:`~ingest.embeddings.get_embedding_provider`, i.e. to
            ``EMBEDDING_PROVIDER`` — which is what makes the provider swap config-only at
            the *read* side too, not only at the write side.
        attribute_filters: structured predicates, all of which must hold (AND).
        category: a category **name**; resolved to its content-hashed ``category_id`` so a
            caller never has to know the hash.
        ingredients_all: ingredient names the product must contain.
        ingredients_none: ingredient names the product must not contain — the
            "fragrance-free", "no parabens" half of a real query.
        brand: exact brand match, when the intent pins one.
        status: exact product status; ``None`` disables the filter. Defaults to ``active``
            so discontinued products do not silently enter a shortlist.
        limit: how many candidates to return.
        oversample: how many index rows to fetch per requested result before filtering.

    Returns:
        Up to ``limit`` :class:`Candidate` rows, best score first. On the vector path the
        score is what ``db.index.vector.queryNodes`` reports, which for a cosine index is
        **rescaled to ``[0, 1]`` as ``(1 + cosine) / 2``** — measured here: an exact
        self-query scores ``0.99997`` (the residue is the index's default float32
        quantization) and an unrelated 1024-d hash vector scores ``≈0.513``, i.e. cosine
        ``≈0.027``. Use :func:`cosine_from_score` to recover the raw cosine. On the
        structured path the score is ``0.0`` for every row and the order is by
        ``product_id``.

    Raises:
        UnretrievableQuery: no vector and no structured predicate was supplied.
        ValueError: ``limit`` or ``oversample`` is not positive.
    """
    if limit <= 0 or oversample <= 0:
        raise ValueError(f"limit and oversample must be positive, got {limit} / {oversample}")

    vector: list[float] | None = None
    if embedding is not None:
        vector = [float(component) for component in embedding]
    elif query_text:
        vector = list((provider or get_embedding_provider()).embed(query_text))

    structured = bool(attribute_filters or category or ingredients_all or ingredients_none or brand)
    if vector is None and not structured:
        raise UnretrievableQuery(
            "a candidate query needs a vector (query_text/embedding) or at least one "
            "structured predicate (attribute_filters, category, ingredients_all, "
            "ingredients_none, brand). Matching products by free-text name alone is "
            "forbidden by DESIGN, so there is no third option."
        )

    parameters: dict[str, Any] = {
        "brand": brand,
        "status": status,
        "category_id": None if category is None else category_id(category),
        "ingredients_all": [ingredient_id(name) for name in ingredients_all],
        "ingredients_none": [ingredient_id(name) for name in ingredients_none],
        "attribute_filters": [f.as_parameter() for f in attribute_filters],
        "limit": int(limit),
    }
    if vector is None:
        return _run(session, _STRUCTURED_HEAD, parameters=parameters)
    parameters["embedding"] = vector
    parameters["fetch"] = min(max(limit * oversample, limit), MAX_INDEX_FETCH)
    return _run(session, _VECTOR_HEAD, parameters=parameters)


def cosine_from_score(score: float) -> float:
    """Recover the raw cosine similarity from a ``queryNodes`` cosine score.

    Neo4j rescales cosine into ``[0, 1]`` so that every similarity function it supports
    reports "bigger is better" on one axis. Downstream ranking (T-031, T-032) wants the real
    cosine, and reading ``0.513`` as "half similar" rather than "orthogonal" would silently
    inflate every retrieval component.

    Args:
        score: a score from :func:`candidate_products` on the vector path.

    Returns:
        The cosine similarity in ``[-1, 1]``.
    """
    return 2.0 * float(score) - 1.0


def products_missing_embeddings(session: Any) -> list[str]:
    """Product ids with no ``embedding`` property yet.

    Args:
        session: an open ``neo4j.Session``.

    Returns:
        The ids, sorted. A non-empty answer after ingest means the re-embed pass has not
        run — and those products are invisible to every vector query, silently.
    """
    return [
        row["product_id"]
        for row in session.run(
            "MATCH (p:Product) WHERE p.embedding IS NULL "
            "RETURN p.product_id AS product_id ORDER BY product_id"
        ).data()
    ]


__all__ = [
    "DEFAULT_OVERSAMPLE",
    "MAX_INDEX_FETCH",
    "AttributeFilter",
    "Candidate",
    "UnretrievableQuery",
    "candidate_products",
    "cosine_from_score",
    "products_missing_embeddings",
]
