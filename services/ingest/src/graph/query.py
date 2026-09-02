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
from .model import (
    EMBEDDING_DIMENSIONS,
    InvalidEmbeddingVector,
    canonical_text,
    category_id,
    embedding_vector_defect,
    ingredient_id,
    slug,
)
from .schema import (
    EMBEDDING_RUN_COMPLETE,
    EMBEDDING_RUN_DEGRADED,
    EMBEDDING_RUN_RUNNING,
    VECTOR_INDEX_NAME,
    embedding_run,
)

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


class VectorIndexUnusable(RuntimeError):
    """The ``product_embedding`` index cannot honestly answer a vector query right now.

    Not a bad argument — the *call* is well formed and the graph is in a state where any
    answer to it would be wrong, which is why this is a ``RuntimeError`` rather than a
    ``ValueError``. Both subclasses describe a condition the previous code answered with a
    plausible-looking, silently wrong ranking.
    """


class EmbeddingProviderMismatch(VectorIndexUnusable):
    """The vectors in the index were written by a *different* provider than the querying one.

    Every registered provider declares :data:`~ingest.graph.model.EMBEDDING_DIMENSIONS`, so
    the width guard on the write side cannot detect a ``hash`` ↔ ``local_bge`` swap. Measured
    on this repo: re-embedding the catalog with a second valid 1024-d provider and then
    querying with the default one raised nothing and moved a product from rank 1 to rank 3.
    """


class EmbeddingRunIncomplete(VectorIndexUnusable):
    """The recorded re-embed pass left the index holding more than one vector space.

    The per-product writes auto-commit, so an interruption part-way leaves two vector spaces
    inside one index while ``products_missing_embeddings()`` still reports ``[]``. Cosine
    across two spaces is noise, so the query refuses rather than ranks.

    Two graph states raise this, and their remediations are opposites — the message says
    which one it is, and ``EmbeddingRun.skipped`` is the discriminator:

    * an **interrupted** pass (the opening ``running`` stamp, empty skip list): re-running
      the pass fixes it, and always terminates;
    * a pass that **reached its end** and whose read-back found products it skipped still
      carrying a vector (the closing ``running`` stamp, non-empty skip list): re-running is
      a fixed point — it skips the same products for the same reason. The named vectors
      have to be removed, or the reason the removal did not take found.

    Deliberately **not** raised for a pass that reached its end having skipped products it
    could not embed (:data:`~ingest.graph.schema.EMBEDDING_RUN_DEGRADED`). That pass left one
    vector space in the index — the skipped products' stale vectors are removed, not left
    behind — so every cosine over it is honest, and the products it could not embed are
    absent from the index and named by :func:`products_missing_embeddings`. Raising here for
    that case (T-116) turned one Product with an empty ``canonical_name`` into a refusal of
    every vector query in the catalog, under a remediation — re-run the pass — that skips
    the same product again and so never terminates.
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
            ``"SPF"`` matches an attribute stored as ``"spf"``. The key fold is
            :func:`~ingest.graph.model.slug` — the same fold
            :func:`~ingest.graph.model.attribute_value_id` hashes and
            ``AttributeValue.as_properties`` writes to ``canonical_key`` — so a filter for
            ``"fragrance free"`` reaches the node ``"fragrance_free"`` wrote.
        """
        return {
            "key": slug(self.key),
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
    """One retrieved product.

    WHY ``scored`` EXISTS, and why a downstream ticket should threshold on :attr:`cosine`
    rather than on :attr:`score`. The structured-only path has no similarity to report and
    used ``0.0`` as its sentinel — but ``0.0`` is also a perfectly real vector score: a
    query vector antipodal to a product's embedding scores **exactly 0.0** on the vector
    path (measured; ``cosine_from_score`` maps both to ``-1.0``). Nothing inside T-012
    thresholds on it, so this was never a live defect here — but six downstream tickets read
    ``Candidate.score``, and "the least similar possible match" and "no similarity was
    measured" are not the same fact, so one of them would eventually read the sentinel as a
    similarity. :attr:`scored` distinguishes them, and :attr:`cosine` is ``None`` rather than
    a number when nothing was measured, so the ambiguity is not expressible in the accessor
    a ranking actually consumes.

    Attributes:
        score: what ``db.index.vector.queryNodes`` reported, in ``[0, 1]``. Meaningless (and
            ``0.0``) when :attr:`scored` is ``False``.
        scored: whether a similarity was actually measured, i.e. whether this row came off
            the vector path.
    """

    product_id: str
    canonical_name: str
    brand: str
    status: str
    score: float
    categories: list[str] = field(default_factory=list)
    attributes: list[dict[str, Any]] = field(default_factory=list)
    ingredients: list[str] = field(default_factory=list)
    scored: bool = True

    @property
    def cosine(self) -> float | None:
        """The raw cosine similarity, or ``None`` when none was measured.

        Returns:
            ``cosine_from_score(self.score)`` on the vector path; ``None`` on the structured
            path, where no comparison happened. A caller that thresholds on this cannot
            silently read the structured sentinel as "maximally dissimilar" — ``None``
            raises on any comparison rather than quietly ranking last.
        """
        return None if not self.scored else cosine_from_score(self.score)


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

#: The structured-only path. Nothing is compared here, so there is no similarity to report:
#: the ``0.0`` below is a placeholder that ``_run`` pairs with ``scored=False``, and
#: ``Candidate.cosine`` is ``None`` for every row it produces. The ordering falls back to
#: ``product_id``, which keeps the result deterministic — a retrieval whose order depends on
#: storage order is not reproducible, and every ranking assertion downstream would be flaky.
_STRUCTURED_HEAD = """
MATCH (p:Product)
WITH p, 0.0 AS score
"""


def _structured_head(brand: str | None, status: str | None) -> str:
    """The structured-only head, with ``brand``/``status`` pushed into the MATCH pattern.

    X2, measured on this repo with a 40-product catalog and ``brand="Northlight",
    status="active"`` (one match). With the bare ``MATCH (p:Product)`` head, ``PROFILE``
    showed ``NodeByLabelScan`` over every product followed by three
    ``OptionalExpand(All)``/``OrderedAggregation`` stages **all carrying 40 rows**, and only
    then the ``Filter`` that applies ``brand``/``status`` (40→40→40→1): the whole catalog's
    attributes, ingredients and categories were collected and aggregated before anything was
    discarded, and ``product_brand``/``product_status`` were never touched. That is the
    opposite of what :data:`DEFAULT_OVERSAMPLE`'s note recommends this path *for* — "a highly
    selective structured query" was the case it handled worst. Total dbHits: **1284**.

    Writing the predicates as a map in the pattern rather than as
    ``WHERE ($brand IS NULL OR p.brand = $brand)`` is what makes the index usable: the
    disjunction with a parameter cannot be planned as a seek, and merely lifting it above the
    aggregations still left a ``NodeByLabelScan`` (dbHits 116). The map form plans as
    ``NodeIndexSeek`` — dbHits **37**, and the aggregations carry one row instead of forty.

    Only property *names* are interpolated, and only from this function's own literals, so
    there is no injection surface; the values stay parameters. Four possible strings means at
    most four cached plans rather than one, which is the whole cost.

    Args:
        brand: the brand predicate, or ``None`` when not filtering by brand. ``""`` is a real
            predicate ("products with no brand"), not an absent one.
        status: the status predicate, or ``None`` when not filtering by status.

    Returns:
        A retrieval head that binds ``p`` and ``score``, for concatenation with
        :data:`_FILTER_AND_RETURN` (which re-checks both predicates, harmlessly, because the
        vector head cannot push them).
    """
    pinned = [
        f"{name}: ${name}"
        for name, value in (("brand", brand), ("status", status))
        if value is not None
    ]
    if not pinned:
        return _STRUCTURED_HEAD
    return f"\nMATCH (p:Product {{{', '.join(pinned)}}})\nWITH p, 0.0 AS score\n"


def _run(
    session: Any,
    head: str,
    *,
    parameters: dict[str, Any],
    scored: bool,
) -> list[Candidate]:
    """Execute a candidate query and materialise the rows.

    Args:
        session: an open ``neo4j.Session``.
        head: the retrieval head (vector or structured).
        parameters: the query parameters.
        scored: whether ``head`` measured a similarity. Passed in rather than sniffed from
            the score, because the whole point is that ``0.0`` does not distinguish them.

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
            scored=scored,
        )
        for row in rows
    ]


def _check_vector_path(session: Any, vector: list[float], *, provider_name: str) -> None:
    """Refuse a vector query the ``product_embedding`` index cannot honestly answer.

    Reads the :class:`~ingest.graph.schema.EmbeddingRun` marker
    :func:`ingest.graph.reembed.reembed_products` writes, and compares it against the
    provider this call would rank with.

    Args:
        session: an open ``neo4j.Session``.
        vector: the query vector.
        provider_name: the name of the provider whose space ``vector`` lives in.

    Raises:
        InvalidEmbeddingVector: the vector is the wrong width, non-finite, or zero-length.
            Checked *here*, before any parameter is built, because the raw path answered a
            512-d vector with ``neo4j.exceptions.ClientError`` — neither of the exceptions
            the docstring declares, and not even a ``ValueError``.
        EmbeddingRunIncomplete: the index holds two vector spaces — either because the pass
            never reached its end, or because it did and its read-back caught skipped
            products still carrying a vector. The message distinguishes them, because the
            remediations are opposites. A pass that reached its end having *skipped*
            products whose vectors were really removed does not raise: see the class
            docstring.
        EmbeddingProviderMismatch: the vectors were written by another provider.
    """
    run = embedding_run(session)
    # The marker records the width the vectors were actually written at, which is what a
    # query vector has to match — that is the live index's width after a
    # `rebuild_vector_index`, not necessarily D6's. With no marker, D6 is the only claim
    # available.
    dimensions = EMBEDDING_DIMENSIONS if run is None else run.dimension
    defect = embedding_vector_defect(vector, dimensions=dimensions)
    if defect is not None:
        raise InvalidEmbeddingVector(
            f"refusing to run a vector query with a vector that {defect[1]}"
        )
    if run is None:
        # No pass recorded. A graph seeded straight through `set_product_embedding` — which
        # is what the adapter tickets do — has vectors of genuinely unknown provenance, and
        # refusing every such query would break the seam this library exists to provide.
        return
    # `finished`, NOT `complete`. The question a vector query has to ask is "does this index
    # hold one vector space or two", and only an unfinished pass answers "two". `complete`
    # answers the *stronger* question "did the pass cover every product", and reading it
    # here (T-116) let one Product with an empty canonical_name black out vector search for
    # the entire catalog. A finished-but-degraded pass removed the skipped products'
    # vectors, so the index is single-space; those products are absent from it and
    # `products_missing_embeddings()` names them. That is a per-product degradation and it
    # is already visible without refusing anybody else's query.
    if not run.finished:
        # TWO different graph states reach this line, and they need OPPOSITE remediations.
        #
        # `reembed_products` writes `running` twice: once as the opening stamp, with an
        # empty skip list, and once from the closing stamp when its read-back found a
        # product it skipped *still carrying a vector*. So `run.skipped` is the
        # discriminator, and it is exact: the opening stamp always writes `skipped=[]`, and
        # the closing `running` is only ever reached with a non-empty skip list.
        #
        # T-118(c) rewrote this message for the first state — an interrupted pass — and
        # promised, unconditionally, that re-running the pass "always records an end state".
        # For the second state that promise is false twice over: the pass *did* reach its
        # end, and re-running it skips the same products for the same reason, fails the same
        # read-back and records `running` again. The message T-118(c) removed from one
        # branch was still being handed to the operator on the other, which is the same
        # no-op loop wearing the fixed message.
        if run.skipped:
            raise EmbeddingRunIncomplete(
                f"the last re-embed of {run.index} (provider {run.provider!r}) is recorded "
                f"as {run.state!r}: the pass DID reach its end, but its read-back found "
                f"{len(run.skipped)} product(s) it skipped still carrying a vector — "
                f"{', '.join(run.skipped)} — written by an earlier pass into a space this "
                f"one did not fill, so every cosine across the index is noise. "
                f"Re-running `python -m ingest.graph.reembed` is NOT the remediation here: "
                f"it skips the same products for the same reason, its read-back fails "
                f"again and it records {EMBEDDING_RUN_RUNNING!r} again. Remove those "
                f"vectors first — `clear_product_embedding(session, product_id=...)` for "
                f"each id above — or find out why the removal did not take; the next pass "
                f"then records {EMBEDDING_RUN_DEGRADED!r} and the index is queryable."
            )
        raise EmbeddingRunIncomplete(
            f"the last re-embed of {run.index} (provider {run.provider!r}) is recorded as "
            f"{run.state!r}: it started writing and never reached its end, so the index "
            f"holds vectors from more than one pass and every cosine across them is noise. "
            f"Re-run `python -m ingest.graph.reembed --provider {run.provider}`. That "
            f"remediation terminates: the pass rewrites every product into one space and "
            f"always records an end state — {EMBEDDING_RUN_COMPLETE!r} when it embedded them "
            f"all, or {EMBEDDING_RUN_DEGRADED!r} listing the products it could not embed, "
            f"and both are queryable — the command exits 0 for either, so a non-zero status "
            f"means something else went wrong and never means 'run it again'. Products with "
            f"no embeddable text no longer hold this refusal open; "
            f"`products_missing_embeddings(session)` names that finite set, and fixing them "
            f"is a catalog edit, not another re-embed."
        )
    if run.provider != provider_name:
        raise EmbeddingProviderMismatch(
            f"{run.index} holds vectors written by provider {run.provider!r} but this query "
            f"was embedded with {provider_name!r}. Both are {run.dimension}-d, so Neo4j will "
            f"happily return a fully-populated, silently re-ranked shortlist. Either set "
            f"EMBEDDING_PROVIDER={run.provider} or re-embed: "
            f"`python -m ingest.graph.reembed --provider {provider_name}`."
        )


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
            so discontinued products do not silently enter a shortlist. NOTE (X3): the
            comparison is ``p.status = $status``, which is *null-valued* for a ``Product``
            carrying no ``status`` property at all — such a product is invisible to every
            default query. ``upsert_product`` always writes one, so this is only reachable
            from raw Cypher; :func:`products_missing_status` is the detector for it.
        limit: how many candidates to return.
        oversample: how many index rows to fetch per requested result before filtering.

    Returns:
        Up to ``limit`` :class:`Candidate` rows, best score first. On the vector path the
        score is what ``db.index.vector.queryNodes`` reports, which for a cosine index is
        **rescaled to ``[0, 1]`` as ``(1 + cosine) / 2``** — measured here: an exact
        self-query scores ``0.99997`` (the residue is the index's default float32
        quantization) and an unrelated 1024-d hash vector scores ``≈0.513``, i.e. cosine
        ``≈0.027``. Use :attr:`Candidate.cosine` (or :func:`cosine_from_score`) to recover
        the raw cosine. On the structured path nothing is compared, so ``scored`` is
        ``False``, ``cosine`` is ``None``, the score field holds a meaningless ``0.0`` and
        the order is by ``product_id`` — a retrieval whose order depends on storage order is
        not reproducible, and every ranking assertion downstream would be flaky.

    Raises:
        UnretrievableQuery: no vector and no structured predicate was supplied.
        ValueError: ``limit`` or ``oversample`` is not positive.
        InvalidEmbeddingVector: ``embedding`` (or the vector ``query_text`` embeds to) is
            the wrong width, carries a non-finite component, or has a zero L2 norm. Also a
            ``ValueError``, so the declaration above stays true.
        EmbeddingProviderMismatch: the vectors in the index were written by a different
            provider than the one this query embeds with.
        EmbeddingRunIncomplete: the recorded re-embed pass never reached its end, so the
            index holds more than one vector space. A pass that reached its end having
            skipped products it could not embed does **not** raise: those products carry no
            vector at all (:func:`products_missing_embeddings` names them) and the rest of
            the catalog stays retrievable.
    """
    if limit <= 0 or oversample <= 0:
        raise ValueError(f"limit and oversample must be positive, got {limit} / {oversample}")

    vector: list[float] | None = None
    provider_name = ""
    if embedding is not None:
        # The caller supplied the vector, so the only statement available about which space
        # it lives in is the process's configured provider — which is exactly the statement
        # `reembed_products` records on the write side, so the two are comparable.
        vector = [float(component) for component in embedding]
        provider_name = (provider or get_embedding_provider()).name
    elif query_text:
        resolved = provider or get_embedding_provider()
        provider_name = resolved.name
        vector = list(resolved.embed(query_text))

    # `is not None`, never truthiness: Product.brand DEFAULTS to "", so `brand=""` is the
    # legitimate query "products with no brand" — and `bool("")` made it unaskable, raising
    # UnretrievableQuery on a perfectly well-formed request. Same trap for `category=""`.
    structured = (
        len(attribute_filters) > 0
        or len(ingredients_all) > 0
        or len(ingredients_none) > 0
        or category is not None
        or brand is not None
    )
    if vector is None and not structured:
        raise UnretrievableQuery(
            "a candidate query needs a vector (query_text/embedding) or at least one "
            "structured predicate (attribute_filters, category, ingredients_all, "
            "ingredients_none, brand). Matching products by free-text name alone is "
            "forbidden by DESIGN, so there is no third option."
        )

    if vector is not None:
        _check_vector_path(session, vector, provider_name=provider_name)

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
        return _run(session, _structured_head(brand, status), parameters=parameters, scored=False)
    parameters["embedding"] = vector
    parameters["fetch"] = min(max(limit * oversample, limit), MAX_INDEX_FETCH)
    return _run(session, _VECTOR_HEAD, parameters=parameters, scored=True)


def cosine_from_score(score: float | None) -> float:
    """Recover the raw cosine similarity from a ``queryNodes`` cosine score.

    Neo4j rescales cosine into ``[0, 1]`` so that every similarity function it supports
    reports "bigger is better" on one axis. Downstream ranking (T-031, T-032) wants the real
    cosine, and reading ``0.513`` as "half similar" rather than "orthogonal" would silently
    inflate every retrieval component.

    Args:
        score: a score from :func:`candidate_products` on the vector path. ``None`` — a
            :attr:`Candidate.score` that was never measured — is refused rather than
            mapped, because ``cosine_from_score(0.0)`` is ``-1.0`` and returning "maximally
            dissimilar" for "not compared" is the exact confusion :attr:`Candidate.scored`
            exists to prevent.

    Returns:
        The cosine similarity in ``[-1, 1]``.

    Raises:
        ValueError: ``score`` is ``None``.
    """
    if score is None:
        raise ValueError(
            "no similarity was measured for this candidate (Candidate.scored is False, i.e. "
            "it came off the structured path); there is no cosine to recover"
        )
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


_PRODUCTS_MISSING_STATUS = """
MATCH (p:Product)
WHERE p.status IS NULL
RETURN p.product_id AS product_id
ORDER BY product_id
"""


def products_missing_status(session: Any) -> list[str]:
    """Product ids with no ``status`` property — invisible to every default query.

    X3. :func:`candidate_products` defaults to ``status="active"`` and the comparison is
    ``p.status = $status``, which is **null-valued**, not false, for a product that has no
    ``status`` at all. Such a product is silently absent from every shortlist in the system
    while looking perfectly healthy: it is embedded, it is sourced, it has attributes, and
    ``products_missing_embeddings()`` reports nothing.

    :func:`ingest.graph.upsert.upsert_product` always writes a ``status`` (``Product.status``
    defaults to ``"active"``), so this is only reachable from raw Cypher, another library, or
    an adapter that bypassed this package — which is exactly the threat model
    :func:`ingest.graph.upsert.provenance_violations` exists for, and the reason this is a
    graph-reading detector rather than a comment. It is deliberately a *sibling* of
    :func:`products_missing_embeddings` rather than a new
    :class:`~ingest.graph.upsert.ProvenanceViolation` kind: a missing status is not an
    unsourced claim, and folding it into the provenance audit would blur what that audit
    means.

    Args:
        session: an open ``neo4j.Session``.

    Returns:
        The ids, sorted. Empty is the healthy answer.
    """
    return [row["product_id"] for row in session.run(_PRODUCTS_MISSING_STATUS).data()]


__all__ = [
    "DEFAULT_OVERSAMPLE",
    "MAX_INDEX_FETCH",
    "AttributeFilter",
    "Candidate",
    "EmbeddingProviderMismatch",
    "EmbeddingRunIncomplete",
    "UnretrievableQuery",
    "VectorIndexUnusable",
    "candidate_products",
    "cosine_from_score",
    "products_missing_embeddings",
    "products_missing_status",
]
