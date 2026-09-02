"""T-012 — the Neo4j attribute-node catalog and the embedding layer.

The frozen acceptance suite covers exactly one thing in this ticket:
``HashEmbedding`` is a deterministic 1024-dimensional ``EmbeddingProvider``. **Nothing
frozen touches the graph** — not the constraints, not the vector index, not the upserts,
not the candidate query. This file is therefore the only evidence those exist and work, and
it is written to be read adversarially. Two habits follow from that:

* **Every invariant is tested by sabotage as well as by success.** A test that only seeds a
  correct graph and finds no violations proves that the *audit* returns an empty list, not
  that the invariant holds — so each such test has a twin that breaks the invariant with
  raw Cypher and requires the audit to catch it. The uniqueness constraints, the provenance
  rule and the free-text prohibition are all covered this way.
* **Nothing asserts semantics ``HashEmbedding`` cannot deliver.** A SHA-256 hash embedding
  is content-addressed, not semantic: two different texts land ~orthogonal, so "the
  semantically right product ranks first for a paraphrased query" is *false* under the
  configured default provider and a test asserting it would be a lie that happened to pass
  on a lucky seed. What is true, and is asserted, is that the cosine index scores an exact
  match at ~1.0, scores an unrelated product at ~0.0, and that the attribute half of the
  query does the discriminating. See ``test_vector_query_*``.

Run: ``PROXYSHOP_WORKER=<n> ./.venv/bin/python -m pytest services/ingest/tests/test_graph.py -q``
"""

from __future__ import annotations

import inspect
import math
import pathlib
import re
import sys
from collections.abc import Sequence
from typing import Any

import pytest
from ingest.embeddings import (
    DEFAULT_PROVIDER,
    EMBEDDING_DIM,
    PROVIDERS,
    EmbeddingProvider,
    EmbeddingProviderUnavailable,
    HashEmbedding,
    LocalBgeEmbedding,
    UnknownEmbeddingProvider,
    get_embedding_provider,
)
from ingest.graph import (
    ID_PROPERTY,
    LOOKUP_INDEXES,
    MATERIAL_FACT_EDGES,
    MATERIAL_FACT_LABELS,
    SOURCE_CLASSES,
    VECTOR_INDEX_DIMENSIONS,
    VECTOR_INDEX_NAME,
    VECTOR_INDEX_SIMILARITY,
    VECTOR_INDEX_STATEMENT,
    VOCABULARY_LABELS,
    AttributeFilter,
    AttributeValue,
    Category,
    Ingredient,
    IntentCluster,
    Offer,
    PolicyPage,
    Product,
    ProvenanceRequired,
    Source,
    Store,
    UnretrievableQuery,
    Variant,
    apply_schema,
    assert_provenance_complete,
    attribute_value_id,
    candidate_products,
    category_id,
    constraint_name,
    constraint_statements,
    cosine_from_score,
    ingredient_id,
    link_category,
    link_compatible_with,
    link_ingredient,
    link_same_as,
    link_sells,
    link_states,
    lookup_index_statements,
    products_missing_embeddings,
    provenance_violations,
    rebuild_vector_index,
    reembed_products,
    schema_report,
    schema_statements,
    seed_products,
    set_product_embedding,
    slug,
    upsert_attribute,
    upsert_offer,
    upsert_policy_page,
    upsert_product,
    upsert_store,
    upsert_variant,
)
from ingest.graph import upsert as upsert_module
from ingest.graph.reembed import build_parser, embedding_text, read_products

from proxyshop_support.embedding import EMBEDDING_DIM as SUPPORT_EMBEDDING_DIM
from proxyshop_support.embedding import cosine, hash_embed

INGEST_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
GRAPH_SRC = INGEST_SRC / "graph"

#: Every shipped module of this service, recursively. The static scans below used
#: ``GRAPH_SRC.glob("*.py")`` — five files, non-recursive — so a concrete provider
#: constructed in ``src/adapters/``, or name-matching Cypher added in a future
#: ``src/graph/retrieval/`` subpackage, was invisible to both of them.
SCANNED_SOURCES = tuple(
    path for path in sorted(INGEST_SRC.rglob("*.py")) if "__pycache__" not in path.parts
)

#: The two probes the frozen acceptance test uses, kept here so a change to either side is
#: visible against the other. They are the same length on purpose (see the frozen test).
PROBE_A = "gentle vitamin c serum for sensitive skin"
PROBE_B = "heavy fragranced night cream for dry face"

#: ``db.index.vector.queryNodes`` rescales cosine into [0, 1] and the index quantizes to
#: float32 by default, so an exact self-match scores ~0.99997 rather than exactly 1.0.
QUANTIZATION_TOLERANCE = 1e-3


# =======================================================================================
# 1. The embedding port  (C2 / C4 / D6 / D19)
# =======================================================================================


def _public_callables(cls: type) -> set[str]:
    """The public callable members of ``cls``, exactly as the frozen test enumerates them."""
    return {name for name, member in inspect.getmembers(cls, callable) if not name.startswith("_")}


def test_the_embedding_port_publishes_exactly_embed_and_embed_batch() -> None:
    """The interface's public surface is the two methods, and nothing a metaclass added.

    ``dir()`` on a class includes its metaclass's attributes, so an ``abc.ABC`` base would
    silently publish ``ABCMeta.register`` as part of this port and every implementation
    would be required to carry a matching signature for it. The frozen acceptance test
    enumerates the port this way and then demands signature identity for every name it
    finds, so an accidental ``ABC``/``Protocol`` base is not a style question — it changes
    what implementations must provide.
    """
    assert _public_callables(EmbeddingProvider) == {"embed", "embed_batch"}
    assert "register" not in dir(EmbeddingProvider), "EmbeddingProvider must not be an abc.ABC"


@pytest.mark.parametrize("implementation", [HashEmbedding, LocalBgeEmbedding])
def test_every_provider_matches_the_port_signature_member_for_member(
    implementation: type,
) -> None:
    """Signature identity for every published member — the frozen assertion, widened.

    The frozen test checks ``HashEmbedding`` only. ``LocalBgeEmbedding`` is checked here
    because D19 makes it a real, selectable provider: a divergent signature there would
    surface as a ``TypeError`` in an operator's re-embed run rather than in any gate.
    """
    for name in sorted(_public_callables(EmbeddingProvider)):
        assert hasattr(implementation, name)
        assert inspect.signature(getattr(implementation, name)) == inspect.signature(
            getattr(EmbeddingProvider, name)
        ), f"{implementation.__name__}.{name}() diverges from EmbeddingProvider"


def test_the_port_declares_embed_and_refuses_to_answer_it_itself() -> None:
    """The base class is a declaration, not a silently-working default."""
    with pytest.raises(NotImplementedError):
        EmbeddingProvider().embed("anything")


def test_hash_embedding_is_1024_dimensional_and_l2_normalised() -> None:
    """D19: the configured default emits L2-normalised 1024-d vectors, so D6's cosine
    index is exercised for real rather than fed unit-less noise."""
    provider = HashEmbedding()
    assert provider.dimension == EMBEDDING_DIM == VECTOR_INDEX_DIMENSIONS == 1024
    for text in (PROBE_A, PROBE_B, "a", "Ω≈ç√∫˜µ", "x" * 5000):
        vector = provider.embed(text)
        assert len(vector) == 1024
        norm = math.sqrt(sum(component * component for component in vector))
        assert norm == pytest.approx(1.0, abs=1e-9), f"{text[:20]!r} has L2 norm {norm}"


def test_hash_embedding_components_are_builtin_floats_not_numpy_scalars() -> None:
    """``isinstance(x, float)`` is what the frozen test asserts, and ``numpy.float32``
    fails it while ``numpy.float64`` passes by accident of subclassing.

    Pinning ``type(x) is float`` is deliberately stricter than the frozen assertion: it
    fails on *either* NumPy scalar type, so a future rewrite that reaches for
    ``np.linalg.norm`` cannot pass here and then break the acceptance suite on a machine
    where the intermediate dtype differs.
    """
    for component in HashEmbedding().embed(PROBE_A):
        assert type(component) is float


def test_hash_embedding_is_deterministic_across_instances_and_calls() -> None:
    """Same text, same vector — across instances, across calls, with no PRNG in the path."""
    first = HashEmbedding().embed(PROBE_A)
    assert HashEmbedding().embed(PROBE_A) == first
    provider = HashEmbedding()
    assert provider.embed(PROBE_A) == provider.embed(PROBE_A) == first


def test_hash_embedding_distinguishes_two_texts_of_the_same_length() -> None:
    """Content, not length: the two frozen probes are the same length and must differ.

    Also asserts the two vectors are close to orthogonal, which is the *honest*
    characterisation of a hash embedding and the reason no test in this file asserts
    semantic ranking.
    """
    assert len(PROBE_A) == len(PROBE_B)
    a, b = HashEmbedding().embed(PROBE_A), HashEmbedding().embed(PROBE_B)
    assert a != b
    assert len(set(a)) > 1 and len(set(b)) > 1, "a constant vector is a fill, not an embedding"
    assert abs(cosine(a, b)) < 0.2, (
        f"two unrelated texts hashed to cosine {cosine(a, b):.4f}; the hash embedding is "
        f"content-addressed and should land near-orthogonal"
    )


def test_hash_embedding_agrees_with_the_shared_support_implementation() -> None:
    """The provider and the ``hash_embedding`` conftest fixture must be one function.

    Six downstream tickets seed ``Product.embedding`` through the root conftest's
    ``hash_embedding`` fixture and then retrieve through this provider. Two independently
    derived SHA-256 schemes would make every seeded vector orthogonal to every query vector
    — retrieval would score noise while both halves passed their own tests. This is the
    guard over that integration, and it is why ``HashEmbedding.embed`` delegates rather
    than re-implements.
    """
    assert SUPPORT_EMBEDDING_DIM == EMBEDDING_DIM == 1024
    for text in (PROBE_A, PROBE_B, "", "single"):
        assert HashEmbedding().embed(text) == hash_embed(text)


def test_empty_text_embeds_to_the_zero_vector() -> None:
    """The contract stays total: empty text is representable, and obviously unrankable."""
    vector = HashEmbedding().embed("")
    assert len(vector) == 1024
    assert set(vector) == {0.0}


def test_embed_batch_is_the_element_wise_embed() -> None:
    """The batching convenience must not be a second, drifting code path."""
    provider = HashEmbedding()
    assert provider.embed_batch([PROBE_A, PROBE_B]) == [
        provider.embed(PROBE_A),
        provider.embed(PROBE_B),
    ]
    assert provider.embed_batch([]) == []


def test_the_two_import_paths_to_the_embeddings_package_are_the_same_file() -> None:
    """``ingest.embeddings`` and ``services.ingest.src.embeddings`` must not diverge.

    The frozen acceptance test imports the second form; everything else in the repo imports
    the first, through the ``.pkgroot/ingest`` symlink. They are two module objects over one
    file, which is fine — but if the symlink ever pointed elsewhere they would be two
    *different* files and the acceptance suite would be grading code nothing else runs.
    """
    import ingest.embeddings as via_pkgroot

    import services.ingest.src.embeddings as via_path

    assert via_path.__file__ is not None and via_pkgroot.__file__ is not None
    assert pathlib.Path(via_path.__file__).resolve() == pathlib.Path(via_pkgroot.__file__).resolve()


# =======================================================================================
# 2. Provider selection is config-only  (D19, acceptance 3)
# =======================================================================================


def test_provider_selection_defaults_to_hash_in_every_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D19: ``hash`` is the configured default, not a test-only fallback."""
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    assert DEFAULT_PROVIDER == "hash"
    assert isinstance(get_embedding_provider(), HashEmbedding)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "")
    assert isinstance(get_embedding_provider(), HashEmbedding)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("hash", HashEmbedding),
        ("local_bge", LocalBgeEmbedding),
        ("  HASH  ", HashEmbedding),
        ("Local_BGE", LocalBgeEmbedding),
    ],
)
def test_provider_swap_is_config_only(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: type
) -> None:
    """One environment variable, no code change, no import of a concrete class by a caller.

    ``local_bge`` is selectable **offline with no weights present**, because construction
    loads nothing. That is what makes "the swap is config-only" checkable here at all: if
    selecting the model provider required the 2.2 GB fetch D19 forbids, this assertion
    could not exist and the property would be untested.
    """
    monkeypatch.setenv("EMBEDDING_PROVIDER", configured)
    provider = get_embedding_provider()
    assert isinstance(provider, expected)
    assert provider.dimension == 1024, "both providers feed one 1024-d index (D6)"


def test_no_caller_names_a_concrete_provider_class() -> None:
    """The registry is the only switch: no module outside the embeddings package
    constructs ``HashEmbedding()`` or ``LocalBgeEmbedding()`` directly.

    A single ``HashEmbedding()`` at a call site would make that call site immune to
    ``EMBEDDING_PROVIDER`` while every gate stayed green, and the swap would be
    config-only everywhere except the one place that mattered.
    """
    offenders = []
    for path in SCANNED_SOURCES:
        if path.parent.name == "embeddings":
            continue  # the package that defines them is where they are allowed to be named
        source = path.read_text(encoding="utf-8")
        for pattern in (r"\bHashEmbedding\s*\(", r"\bLocalBgeEmbedding\s*\("):
            if re.search(pattern, source):
                offenders.append(f"{path.relative_to(INGEST_SRC)}: {pattern}")
    assert not offenders, f"every caller must go through get_embedding_provider(): {offenders}"


def test_an_unknown_provider_fails_loudly_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd ``EMBEDDING_PROVIDER`` must not silently embed production with the double."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "bge-m3")
    with pytest.raises(UnknownEmbeddingProvider) as excinfo:
        get_embedding_provider()
    assert "bge-m3" in str(excinfo.value)
    assert "hash" in str(excinfo.value), "the error should list what is registered"


def test_registered_providers_are_exactly_hash_and_local_bge() -> None:
    """The registry and the classes' own ``name`` attributes cannot drift apart."""
    assert set(PROVIDERS) == {"hash", "local_bge"}
    for name, provider_class in PROVIDERS.items():
        assert provider_class.name == name


def test_local_bge_construction_imports_no_model_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D3/D19: selecting the model provider must not import ``torch`` or fetch weights.

    Importing ``sentence_transformers`` at module scope would take down every
    ``pytest services/ingest`` run on a machine that deliberately lacks it — which,
    per D19, is every machine by default.
    """
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local_bge")
    provider = get_embedding_provider()
    assert isinstance(provider, LocalBgeEmbedding)
    assert "torch" not in sys.modules
    assert "sentence_transformers" not in sys.modules


def test_local_bge_reports_the_missing_extra_legibly_rather_than_crashing() -> None:
    """With weights absent, the failure names the extra and the reason it is absent."""
    reason = LocalBgeEmbedding.probe()
    if reason is None:  # pragma: no cover - only on a machine with the optional extra
        pytest.skip("the optional 'embeddings' extra is installed here")
    with pytest.raises(EmbeddingProviderUnavailable) as excinfo:
        LocalBgeEmbedding().embed(PROBE_A)
    message = str(excinfo.value)
    assert "embeddings" in message and ("torch" in message or "sentence_transformers" in message)


@pytest.mark.needs_model
def test_local_bge_embeds_1024_normalised_dimensions_when_weights_are_present() -> None:
    """The real provider, exercised only where the weights exist.

    ``needs_model`` is deselected by ``make verify`` (``-m "not needs_model"``), and this
    test *skips* rather than fails when the optional extra is absent — which is the state
    D19 pins for every default environment, including this one.
    """
    reason = LocalBgeEmbedding.probe()
    if reason is not None:
        pytest.skip(reason)
    provider = LocalBgeEmbedding()
    vector = provider.embed(PROBE_A)
    assert len(vector) == 1024
    assert all(type(component) is float for component in vector)
    assert math.sqrt(sum(c * c for c in vector)) == pytest.approx(1.0, abs=1e-4)
    assert provider.embed(PROBE_A) == vector


# =======================================================================================
# 3. The model: stable IDs, provenance vocabulary, D25's three Offers
# =======================================================================================


def test_every_label_in_the_model_has_exactly_one_stable_id_property() -> None:
    """DESIGN: "Uniqueness constraints on every stable ID." ``ID_PROPERTY`` is that list,
    and every label the model knows about has to appear in it — a label that does not is a
    node type entity resolution (T-022) cannot key on."""
    known = MATERIAL_FACT_LABELS | VOCABULARY_LABELS | {"Source"}
    assert set(ID_PROPERTY) == known
    assert len(set(ID_PROPERTY.values())) == len(ID_PROPERTY), "id properties must be distinct"


def test_material_fact_edge_endpoints_are_labels_the_model_knows() -> None:
    """Every relationship endpoint resolves to a label with a stable ID, so
    ``_fact_edge`` can always MATCH both ends by ID rather than by shape."""
    for edge, (start, end) in MATERIAL_FACT_EDGES.items():
        assert start in ID_PROPERTY, f"{edge} starts at unknown label {start}"
        assert end in ID_PROPERTY, f"{edge} ends at unknown label {end}"


def test_the_design_edge_vocabulary_is_covered() -> None:
    """The nine edges DESIGN names, plus SUPPORTED_BY, and nothing invented beyond them."""
    assert set(MATERIAL_FACT_EDGES) == {
        "SELLS",
        "MAKES_OFFER",
        "FOR",
        "HAS_VARIANT",
        "IN_CATEGORY",
        "HAS_ATTRIBUTE",
        "CONTAINS",
        "COMPATIBLE_WITH",
        "SAME_AS",
        "STATES",
    }


def test_source_class_is_the_design_provenance_vocabulary() -> None:
    """``Source ≡ our Provenance``; ``source_class`` carries the ``Provenance.source`` enum."""
    assert SOURCE_CLASSES == {
        "scraped",
        "pixel_feed",
        "owner_statement",
        "envelope_rule",
        "learned_policy",
        "network",
        "seller_asserted",
    }


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"source_class": "guessed"}, "source_class"),
        ({"confidence": 1.5}, "confidence"),
        ({"confidence": -0.1}, "confidence"),
        ({"source_id": ""}, "source_id"),
    ],
)
def test_source_rejects_values_outside_its_contract(kwargs: dict[str, Any], fragment: str) -> None:
    """A ``Source`` that cannot be trusted is refused at construction, not at write time."""
    base = {
        "source_id": "src-1",
        "url": "https://example.test/p",
        "content_hash": "sha256:aa",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "extractor_version": "v1",
        "confidence": 0.5,
        "source_class": "scraped",
    }
    with pytest.raises(ValueError) as excinfo:
        Source(**{**base, **kwargs})
    assert fragment in str(excinfo.value)


def test_the_graph_offer_is_the_observed_listing_not_the_protocol_offer() -> None:
    """D25: three different objects are called "Offer"; this one is the store's *listing*.

    Giving the graph node the protocol object's fields is exactly the conflation D25 warns
    about, and it would be invisible until T-032's expiry filter read an ``expires_at`` that
    the graph never populates.
    """
    fields = set(Offer.__dataclass_fields__)
    assert fields == {"offer_id", "price", "currency", "availability", "observed_at"}
    for protocol_only in ("total_price", "commitments", "checkout_url", "expires_at", "discount"):
        assert protocol_only not in fields, f"{protocol_only} belongs to the protocol Offer (D25)"


# =======================================================================================
# 4. Content-hashed IDs
# =======================================================================================


def test_attribute_ids_are_content_hashes_that_converge_on_one_node() -> None:
    """Two adapters observing the same reading must land on the same ``AttributeValue``.

    Without this, "SPF 50" becomes a private node per product, and the attribute half of
    the candidate query degenerates into a scan.
    """
    assert attribute_value_id("SPF", value_number=50) == attribute_value_id(
        "spf", value_number=50.0
    )
    assert attribute_value_id("spf", value_number=50) == attribute_value_id(
        " spf ", value_number=50
    )
    assert (
        AttributeValue("Skin Type", value_string="Sensitive").attr_id
        == AttributeValue("skin  type", value_string="  SENSITIVE ").attr_id
    )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (("spf", {"value_number": 50}), ("spf", {"value_number": 30})),
        (("spf", {"value_number": 50}), ("spf", {"value_number": 50, "unit": "x"})),
        (("spf", {"value_number": 50}), ("uv", {"value_number": 50})),
        (("ff", {"value_bool": True}), ("ff", {"value_bool": False})),
        (("v", {"value_string": "50"}), ("v", {"value_number": 50})),
    ],
)
def test_different_attribute_content_gets_different_ids(
    left: tuple[str, dict[str, Any]], right: tuple[str, dict[str, Any]]
) -> None:
    """Every value component participates in the identity, including the unit and the type."""
    assert (
        AttributeValue(left[0], **left[1]).attr_id != AttributeValue(right[0], **right[1]).attr_id
    )


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"value_number": float("nan")}, {"value_number": float("inf")}],
)
def test_an_attribute_with_no_usable_value_is_refused(kwargs: dict[str, Any]) -> None:
    """A valueless or non-finite attribute has no stable identity, so it cannot be a node.

    ``NaN != NaN``, so a NaN reading would hash differently on every observation and
    ``MERGE`` would create an unbounded number of duplicate nodes under a uniqueness
    constraint that could never fire.
    """
    with pytest.raises(ValueError):
        AttributeValue("k", **kwargs)


def test_vocabulary_ids_are_readable_and_collision_safe() -> None:
    """``Category``/``Ingredient`` ids carry a slug for humans and a digest for safety."""
    assert Category(name="Face Serum").category_id.startswith("cat_face-serum_")
    assert Category(name="face  SERUM").category_id == Category(name="Face Serum").category_id
    assert (
        Category(name="Serum", parent="Skincare").category_id != Category(name="Serum").category_id
    )
    assert Ingredient(name="Ascorbic Acid").ingredient_id.startswith("ing_ascorbic-acid_")
    assert (
        Ingredient(name="ASCORBIC ACID").ingredient_id
        == Ingredient(name="Ascorbic Acid").ingredient_id
    )


# =======================================================================================
# 5. Schema statements  (acceptance 1 — the static half)
# =======================================================================================


def test_every_schema_statement_is_written_to_be_re_runnable() -> None:
    """D38(c): a re-entrant session must never error, so every statement is IF NOT EXISTS.

    This is the static half of "created idempotently"; the live half is
    ``test_apply_schema_from_an_empty_database_is_idempotent`` below.
    """
    for statement in schema_statements():
        assert "IF NOT EXISTS" in statement, statement


def test_there_is_one_uniqueness_constraint_per_stable_id() -> None:
    """The constraint list is generated from ``ID_PROPERTY``, so it cannot drift from it."""
    statements = constraint_statements()
    assert len(statements) == len(ID_PROPERTY)
    for label, prop in ID_PROPERTY.items():
        expected = (
            f"CREATE CONSTRAINT {constraint_name(label)} IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
        )
        assert expected in statements


def test_the_vector_index_statement_pins_exactly_the_d6_parameters() -> None:
    """D6: ``product_embedding`` on ``Product.embedding``, 1024 dimensions, cosine.

    The statement text differs from D6's quotation in one respect and one only: the two
    dotted ``indexConfig`` keys are backtick-quoted rather than single-quoted, because
    single quotes make a Cypher *string* and a string is not a map key. D6's literal text
    raises ``Neo.ClientError.Statement.SyntaxError`` on neo4j 5.26.30 Community — measured
    on this host. Every parameter it fixes is preserved, which is what this test asserts.
    """
    assert VECTOR_INDEX_NAME == "product_embedding"
    assert VECTOR_INDEX_DIMENSIONS == 1024
    assert VECTOR_INDEX_SIMILARITY == "cosine"
    assert "CREATE VECTOR INDEX product_embedding IF NOT EXISTS" in VECTOR_INDEX_STATEMENT
    assert "FOR (p:Product) ON (p.embedding)" in VECTOR_INDEX_STATEMENT
    assert "`vector.dimensions`: 1024" in VECTOR_INDEX_STATEMENT
    assert "`vector.similarity_function`: 'cosine'" in VECTOR_INDEX_STATEMENT
    assert "'vector.dimensions'" not in VECTOR_INDEX_STATEMENT, (
        "single-quoted map keys do not parse on neo4j 5.26.30"
    )


def test_the_lookup_indexes_cover_the_predicates_the_candidate_query_filters_on() -> None:
    """Read-path indexes exist for every structured predicate the query supports."""
    statements = " ".join(lookup_index_statements())
    for label, prop in (
        ("AttributeValue", "canonical_key"),
        ("AttributeValue", "value_number"),
        ("Ingredient", "canonical_name"),
        ("Category", "slug"),
        ("Product", "status"),
        ("Product", "brand"),
    ):
        assert f"FOR (n:{label}) ON (n.{prop})" in statements


# =======================================================================================
# 6. "Never match products by free-text name alone"
# =======================================================================================

#: Cypher shapes that would make ``canonical_name`` (or ``brand``, or a ``Variant.name``) a
#: *matching* predicate rather than a projected value. Kept as regexes over the real source
#: because the prohibition is about what the library is *able* to do, not about what today's
#: call sites happen to pass.
_FREE_TEXT_MATCH_PATTERNS = (
    # `p.canonical_name <op> …`, tolerating one closing paren so a folding wrapper
    # (`toLower(p.canonical_name) = $q`) is caught rather than waved through.
    r"(?i)\.canonical_name\s*\)?\s*(=~|=|<>|IN\b|CONTAINS|STARTS\s+WITH|ENDS\s+WITH)",
    # Any string-folding function applied to the name — the usual smuggling route.
    r"(?i)\b(toLower|toUpper|trim|replace|substring|left|right|split)\s*\(\s*\w*\.canonical_name",
    # Inline map match: `(p:Product {canonical_name: …})`, `(v:Variant {name: …})`.
    r"(?i):Product\s*\{[^}]*canonical_name",
    r"(?i):Variant\s*\{[^}]*\bname\b",
    # A full-text index over names would be the same prohibition by another route.
    r"(?i)db\.index\.fulltext",
)

#: Name-matching Cypher this scan must reject. Kept as data so the *detector* is tested,
#: not merely run: a regex tripwire that catches nothing is indistinguishable from a passing
#: test, and the first version of this list quietly let four of these nine through.
_FREE_TEXT_ATTACKS = (
    "MATCH (p:Product) WHERE p.canonical_name CONTAINS $q RETURN p",
    "MATCH (p:Product) WHERE p.canonical_name = $q RETURN p",
    "MATCH (p:Product) WHERE p.canonical_name =~ $rx RETURN p",
    "MATCH (p:Product) WHERE p.canonical_name IN $names RETURN p",
    "MATCH (p:Product) WITH p WHERE p.canonical_name <> '' RETURN p",
    "MATCH (p:Product) WHERE toLower(p.canonical_name) = $q RETURN p",
    "MATCH (p:Product {canonical_name: $q}) RETURN p",
    "MATCH (v:Variant {name: $q}) RETURN v",
    "CALL db.index.fulltext.queryNodes('pname', $q) YIELD node RETURN node",
)


def _free_text_offenders(source: str, label: str) -> list[str]:
    """Every free-text name match in ``source``, as ``label:line: matched-text``."""
    found = []
    for pattern in _FREE_TEXT_MATCH_PATTERNS:
        for match in re.finditer(pattern, source):
            found.append(
                f"{label}:{source[: match.start()].count(chr(10)) + 1}: {match.group(0)!r}"
            )
    return found


@pytest.mark.parametrize("attack", _FREE_TEXT_ATTACKS)
def test_the_free_text_detector_actually_detects(attack: str) -> None:
    """The tripwire is armed: every known way to match a product by name trips it.

    Without this, ``test_no_graph_module_matches_products_by_free_text_name`` would pass
    just as happily against a regex list that matches nothing at all.
    """
    assert _free_text_offenders(attack, "attack"), f"detector missed: {attack}"


def test_no_graph_module_matches_products_by_free_text_name() -> None:
    """DESIGN: "never match products by free-text name alone."

    ``canonical_name`` is fine to *return*; it is not fine to match on. If a future ticket
    adds a name lookup "just for debugging", this fails in that ticket's gate.

    Honest about what this is: a **tripwire over the shipped Cypher**, not a proof. A
    determined author can still assemble a name query from string fragments at runtime, and
    no static scan over source text will see that. The load-bearing guarantee is the one
    below — the retrieval API has no parameter that could carry a name to match — and this
    scan exists to catch the ordinary mistake before it reaches review.
    """
    offenders = []
    for path in SCANNED_SOURCES:
        offenders += _free_text_offenders(
            path.read_text(encoding="utf-8"), str(path.relative_to(INGEST_SRC))
        )
    assert not offenders, "free-text product matching is forbidden by DESIGN:\n" + "\n".join(
        offenders
    )


def test_the_candidate_query_exposes_no_name_matching_parameter() -> None:
    """The real enforcement: a caller cannot *ask* for a name match.

    ``query_text`` reaches the database only as an embedding vector, and every other
    parameter is a structured facet resolved through a content-hashed ID. There is no knob
    that carries a name through to Cypher, so name matching is not a discipline the callers
    have to keep — it is not reachable.
    """
    parameters = set(inspect.signature(candidate_products).parameters)
    assert parameters & {"name", "canonical_name", "name_contains", "text_match"} == set()
    assert "query_text" in parameters and "attribute_filters" in parameters


def test_embedding_text_uses_structured_context_not_the_name_alone() -> None:
    """The write side of the same rule: a vector built from the name alone turns the cosine
    index into a fuzzy name matcher, which is the forbidden behaviour wearing a disguise."""
    row = {
        "product_id": "p",
        "canonical_name": "Gentle Vitamin C Serum",
        "brand": "Northlight",
        "categories": ["Serum"],
        "attributes": [
            {"key": "spf", "value_number": 50.0, "unit": None},
            {"key": "fragrance_free", "value_bool": True},
            {"key": "volume", "value_number": 30.0, "unit": "ml"},
        ],
        "ingredients": ["Niacinamide", "Ascorbic Acid"],
    }
    text = embedding_text(row)
    for expected in ("Gentle Vitamin C Serum", "Northlight", "Serum", "spf: 50", "volume: 30 ml"):
        assert expected in text
    assert "fragrance_free: yes" in text
    assert "Ascorbic Acid, Niacinamide" in text, "list components must be sorted for determinism"


def test_embedding_text_is_order_independent() -> None:
    """Two ingests that discovered the same facts in a different order embed identically."""
    base = {
        "canonical_name": "X",
        "brand": "B",
        "categories": ["A", "B"],
        "attributes": [{"key": "k1", "value_string": "v1"}, {"key": "k2", "value_string": "v2"}],
        "ingredients": ["i1", "i2"],
    }
    shuffled = {
        **base,
        "categories": ["B", "A"],
        "attributes": list(reversed(base["attributes"])),
        "ingredients": ["i2", "i1"],
    }
    assert embedding_text(base) == embedding_text(shuffled)


def test_the_reembed_cli_offers_provider_override_and_index_rebuild() -> None:
    """The shipped script DESIGN asks for: "swapping providers = re-embed + rebuild index"."""
    args = build_parser().parse_args([])
    assert args.provider is None and args.rebuild_index is False
    args = build_parser().parse_args(["--provider", "local_bge", "--rebuild-index"])
    assert args.provider == "local_bge" and args.rebuild_index is True


# =======================================================================================
# 7. Live schema  (acceptance 1 — the measured half)
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_apply_schema_from_an_empty_database_is_idempotent(neo4j_session: Any) -> None:
    """Drop everything, create it, create it again — both runs succeed and agree.

    Applying the schema onto a database that already has it proves nothing about the first
    run, so this test genuinely empties the schema first. The ``finally`` restores it, so a
    failure here cannot cascade into every later test in the file.
    """
    # Neo4j's two built-in token-lookup indexes must survive: nothing recreates them, and
    # dropping them permanently degrades a database four agents share. Their generated
    # names differ per container, so they are identified by TYPE, never by a hardcoded name
    # — an earlier version pinned `index_343aff4e`/`index_f7700477`, which are correct only
    # on this one container.
    builtin = {
        name
        for name, kind in schema_report(neo4j_session).indexes.items()
        if kind.upper() == "LOOKUP"
    }
    assert len(builtin) == 2, f"expected the two token-lookup indexes, found {builtin}"
    try:
        for name in list(schema_report(neo4j_session).constraints):
            neo4j_session.run(f"DROP CONSTRAINT {name} IF EXISTS").consume()
        for name in list(schema_report(neo4j_session).indexes):
            if name not in builtin:
                neo4j_session.run(f"DROP INDEX {name} IF EXISTS").consume()
        emptied = schema_report(neo4j_session)
        assert emptied.constraints == {}, "the database must genuinely start without the schema"
        assert VECTOR_INDEX_NAME not in emptied.indexes

        first = apply_schema(neo4j_session)
        second = apply_schema(neo4j_session)
    finally:
        apply_schema(neo4j_session)

    assert first == second, "a second apply_schema() must be a no-op, not a divergence"
    assert set(first.constraints) == {constraint_name(label) for label in ID_PROPERTY}
    for label, prop in ID_PROPERTY.items():
        assert first.constraints[constraint_name(label)] == f"{label}.{prop}"
    for name, _label, _prop in LOOKUP_INDEXES:
        assert name in first.indexes
    assert builtin <= set(first.indexes), "the built-in token-lookup indexes must survive"
    assert first.indexes[VECTOR_INDEX_NAME] == "VECTOR"
    assert first.vector_dimensions == 1024
    assert first.vector_similarity == "cosine"


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.parametrize("label", sorted(ID_PROPERTY))
def test_the_uniqueness_constraint_actually_rejects_a_duplicate_stable_id(
    graph_schema_session: Any, label: str
) -> None:
    """Sabotage: create the same stable ID twice with raw Cypher and require a failure.

    A constraint that exists in ``SHOW CONSTRAINTS`` but does not fire is worth nothing, and
    the only way to tell the difference is to violate it. Raw Cypher on purpose — going
    through the upsert library would ``MERGE`` and never attempt a duplicate.
    """
    from neo4j.exceptions import ConstraintError

    prop = ID_PROPERTY[label]
    graph_schema_session.run(f"CREATE (n:{label} {{{prop}: 'dup-probe'}})").consume()
    with pytest.raises(ConstraintError):
        graph_schema_session.run(f"CREATE (n:{label} {{{prop}: 'dup-probe'}})").consume()


@pytest.mark.docker
@pytest.mark.graph
def test_rebuild_vector_index_recreates_it_with_the_d6_parameters(
    graph_schema_session: Any,
) -> None:
    """The opt-in rebuild path used when a new provider's dimension differs.

    This is the *only* drop in the library, and it is never reached from
    :func:`apply_schema` — a re-run error is never fixed by dropping an index.
    """
    report = rebuild_vector_index(graph_schema_session)
    assert report.indexes[VECTOR_INDEX_NAME] == "VECTOR"
    assert report.vector_dimensions == 1024
    assert report.vector_similarity == "cosine"


# =======================================================================================
# 8. Provenance  (acceptance 1 — "every material fact upserts with a SUPPORTED_BY Source")
# =======================================================================================


def _seed_full_model(session: Any, source: Source) -> None:
    """Write at least one instance of every node label and every edge type."""
    upsert_store(session, Store("store-a", "store-a.example", "Store A Ltd", 1), source=source)
    upsert_product(session, Product("p-a", "Alpha Serum", brand="Northlight"), source=source)
    upsert_product(session, Product("p-b", "Beta Serum", brand="Bellmark"), source=source)
    upsert_variant(session, Variant("v-a", "SKU-A", "30 ml"), product_id="p-a", source=source)
    upsert_offer(
        session,
        Offer("off-a", 24.5, "USD", "in_stock", "2026-01-01T00:00:00+00:00"),
        store_id="store-a",
        variant_id="v-a",
        source=source,
    )
    link_sells(session, store_id="store-a", product_id="p-a", source=source)
    link_category(session, product_id="p-a", category=Category(name="Serum"), source=source)
    upsert_attribute(
        session,
        product_id="p-a",
        attribute=AttributeValue("spf", value_number=50, unit="index"),
        source=source,
    )
    link_ingredient(
        session, product_id="p-a", ingredient=Ingredient(name="Niacinamide"), source=source
    )
    link_same_as(session, product_id="p-a", other_id="p-b", confidence=0.87, source=source)
    link_compatible_with(session, product_id="p-a", other_id="p-b", source=source)
    upsert_policy_page(session, PolicyPage("page-a", "returns", "sha256:ff"), source=source)
    link_states(
        session,
        page_id="page-a",
        attribute=AttributeValue("returns_window_days", value_number=30),
        source=source,
    )


@pytest.mark.docker
@pytest.mark.graph
def test_every_material_fact_in_a_fully_seeded_graph_is_supported_by_a_source(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """Acceptance 1: one instance of every node label and every edge, all provenanced.

    The audit reads the graph, so this also covers the edge half of the rule, which cannot
    be a ``SUPPORTED_BY`` relationship (Neo4j has no relationship-on-a-relationship) and is
    instead a ``source_id`` that must resolve to a real ``Source``.
    """
    _seed_full_model(graph_schema_session, graph_source)
    assert provenance_violations(graph_schema_session) == []
    assert_provenance_complete(graph_schema_session)

    counts = graph_schema_session.run(
        "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) "
        "RETURN labels(n)[0] AS label, count(*) AS total",
        labels=sorted(MATERIAL_FACT_LABELS),
    ).data()
    seen = {row["label"] for row in counts}
    assert seen == MATERIAL_FACT_LABELS, f"seed missed {MATERIAL_FACT_LABELS - seen}"

    edge_types = {
        row["type"]
        for row in graph_schema_session.run(
            "MATCH ()-[r]->() WHERE type(r) IN $types RETURN DISTINCT type(r) AS type",
            types=sorted(MATERIAL_FACT_EDGES),
        ).data()
    }
    assert edge_types == set(MATERIAL_FACT_EDGES), (
        f"seed missed {set(MATERIAL_FACT_EDGES) - edge_types}"
    )

    unsupported = graph_schema_session.run(
        "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) "
        "AND NOT EXISTS { (n)-[:SUPPORTED_BY]->(:Source) } RETURN count(n) AS c",
        labels=sorted(MATERIAL_FACT_LABELS),
    ).single()["c"]
    assert unsupported == 0


@pytest.mark.docker
@pytest.mark.graph
def test_the_provenance_audit_catches_a_node_written_without_a_source(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """Sabotage: a raw-Cypher ``Product`` with no ``SUPPORTED_BY`` must be reported.

    Without this, the previous test proves only that ``provenance_violations`` can return an
    empty list — which a ``return []`` would also do.
    """
    _seed_full_model(graph_schema_session, graph_source)
    graph_schema_session.run(
        "CREATE (p:Product {product_id: 'p-smuggled', canonical_name: 'Smuggled'})"
    ).consume()

    violations = provenance_violations(graph_schema_session)
    assert [v.identity for v in violations] == ["p-smuggled"]
    assert violations[0].kind == "unsourced_node"
    assert violations[0].label_or_type == "Product"
    with pytest.raises(ProvenanceRequired, match="p-smuggled"):
        assert_provenance_complete(graph_schema_session)


@pytest.mark.docker
@pytest.mark.graph
def test_the_provenance_audit_catches_an_edge_whose_source_does_not_resolve(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """Sabotage, edge half: a ``source_id`` naming a ``Source`` that does not exist.

    This is the realistic corruption — an adapter that copies a ``source_id`` string around
    instead of upserting the ``Source`` node. It is indistinguishable from a correct edge
    unless the audit follows the reference.
    """
    _seed_full_model(graph_schema_session, graph_source)
    graph_schema_session.run(
        "MATCH (:Product {product_id:'p-a'})-[r:CONTAINS]->() SET r.source_id = 'src-ghost'"
    ).consume()
    violations = provenance_violations(graph_schema_session)
    assert [v.kind for v in violations] == ["unsourced_edge"]
    assert violations[0].label_or_type == "CONTAINS"
    assert "src-ghost" in violations[0].detail

    graph_schema_session.run(
        "MATCH (:Product {product_id:'p-a'})-[r:CONTAINS]->() REMOVE r.source_id"
    ).consume()
    violations = provenance_violations(graph_schema_session)
    assert violations[0].detail == "missing source_id"


@pytest.mark.docker
@pytest.mark.graph
def test_the_provenance_audit_catches_a_supported_by_pointing_at_a_non_source(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """Sabotage: a ``SUPPORTED_BY`` edge is only provenance if it lands on a ``Source``."""
    _seed_full_model(graph_schema_session, graph_source)
    graph_schema_session.run(
        "MATCH (p:Product {product_id:'p-b'}), (q:Product {product_id:'p-a'}) "
        "MERGE (p)-[:SUPPORTED_BY]->(q)"
    ).consume()
    kinds = {v.kind for v in provenance_violations(graph_schema_session)}
    assert "supported_by_non_source" in kinds


@pytest.mark.docker
@pytest.mark.graph
def test_a_fact_observed_from_two_sources_keeps_both_supports(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """Provenance is a set, not a last-writer-wins slot — for NODES.

    A product asserted by three stores has three supports, and that is what makes
    ``authority_rank`` arbitration possible downstream. It is bounded by the number of
    *distinct* ``source_id`` values, which is why ``source_id`` must be content-addressed
    (url + content_hash) and never scoped to a crawl run: a run-scoped id would grow this
    set without bound on every re-crawl while every test here still passed.

    The edge half is deliberately different and is asserted below: ``MERGE`` collapses the
    relationship, so an edge keeps only its most recent ``source_id``. Both behaviours are
    pinned so neither can change silently.
    """
    second = Source(
        source_id="src-second",
        url="https://other.example/p",
        content_hash="sha256:bb",
        observed_at="2026-02-02T00:00:00+00:00",
        extractor_version="fixture@2",
        confidence=0.5,
        source_class="pixel_feed",
    )
    product = Product("p-multi", "Observed Twice")
    upsert_product(graph_schema_session, product, source=graph_source)
    upsert_product(graph_schema_session, product, source=second)

    supports = graph_schema_session.run(
        "MATCH (:Product {product_id:'p-multi'})-[:SUPPORTED_BY]->(s:Source) "
        "RETURN s.source_id AS id ORDER BY id"
    ).value()
    assert supports == ["src-catalog-fixture", "src-second"]

    # Re-asserting from a source already recorded must not add a duplicate edge.
    upsert_product(graph_schema_session, product, source=graph_source)
    assert (
        graph_schema_session.run(
            "MATCH (:Product {product_id:'p-multi'})-[r:SUPPORTED_BY]->() RETURN count(r) AS c"
        ).single()["c"]
        == 2
    )

    # The edge half: one relationship per (a, type, b), carrying the latest source_id.
    upsert_product(graph_schema_session, Product("p-other", "Other"), source=graph_source)
    link_compatible_with(
        graph_schema_session, product_id="p-multi", other_id="p-other", source=graph_source
    )
    link_compatible_with(
        graph_schema_session, product_id="p-multi", other_id="p-other", source=second
    )
    edges = graph_schema_session.run(
        "MATCH (:Product {product_id:'p-multi'})-[r:COMPATIBLE_WITH]->() "
        "RETURN r.source_id AS source_id"
    ).value()
    assert edges == ["src-second"], "an edge keeps one relationship and its latest source"
    assert provenance_violations(graph_schema_session) == []


@pytest.mark.docker
@pytest.mark.graph
def test_writing_a_fact_without_a_source_is_not_expressible(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """``source`` is a required keyword-only argument on every write in the library.

    The API shape is the primary enforcement; the audit is the backstop. Both are needed:
    the shape stops the mistake, the audit catches what bypassed the shape.
    """
    with pytest.raises(TypeError):
        upsert_product(graph_schema_session, Product("p-x", "X"))  # type: ignore[call-arg]
    with pytest.raises(ProvenanceRequired):
        upsert_product(graph_schema_session, Product("p-x", "X"), source=None)  # type: ignore[arg-type]
    assert graph_schema_session.run("MATCH (p:Product) RETURN count(p) AS c").single()["c"] == 0, (
        "a refused write must leave nothing behind"
    )

    for name, function in sorted(vars(upsert_module).items()):
        if (
            not callable(function)
            or name.startswith("_")
            or not name.startswith(("upsert_", "link_"))
        ):
            continue
        if name in {
            "upsert_source",
            "upsert_category",
            "upsert_ingredient",
            "upsert_intent_cluster",
        }:
            continue  # a Source is its own provenance; vocabulary terms are not facts
        parameter = inspect.signature(function).parameters.get("source")
        assert parameter is not None, f"{name}() takes no source"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, f"{name}(source=…) must be kw-only"
        assert parameter.default is inspect.Parameter.empty, f"{name}(source=…) must be required"


@pytest.mark.docker
@pytest.mark.graph
def test_upserts_converge_rather_than_duplicate(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """Re-ingesting unchanged pages must not grow the graph — T-021 re-extracts routinely."""
    _seed_full_model(graph_schema_session, graph_source)
    before = graph_schema_session.run("MATCH (n) RETURN count(n) AS nodes").single()["nodes"]
    before_edges = graph_schema_session.run("MATCH ()-[r]->() RETURN count(r) AS edges").single()[
        "edges"
    ]

    _seed_full_model(graph_schema_session, graph_source)

    assert (
        graph_schema_session.run("MATCH (n) RETURN count(n) AS nodes").single()["nodes"] == before
    )
    assert (
        graph_schema_session.run("MATCH ()-[r]->() RETURN count(r) AS edges").single()["edges"]
        == before_edges
    )
    assert provenance_violations(graph_schema_session) == []


@pytest.mark.docker
@pytest.mark.graph
def test_two_products_with_the_same_reading_share_one_attribute_node(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """The attribute-node model: identity is the content, so the node is shared.

    This is what makes "find everything with SPF 50" one hop instead of a scan, and it is
    the reason ``attr_id`` is a content hash rather than an allocated key.
    """
    upsert_product(graph_schema_session, Product("p-1", "One"), source=graph_source)
    upsert_product(graph_schema_session, Product("p-2", "Two"), source=graph_source)
    reading = AttributeValue("spf", value_number=50)
    for product_id in ("p-1", "p-2"):
        upsert_attribute(
            graph_schema_session,
            product_id=product_id,
            attribute=AttributeValue("SPF", value_number=50.0),
            source=graph_source,
        )
    rows = graph_schema_session.run(
        "MATCH (a:AttributeValue {attr_id: $id})<-[:HAS_ATTRIBUTE]-(p:Product) "
        "RETURN count(DISTINCT a) AS attrs, count(DISTINCT p) AS products",
        id=reading.attr_id,
    ).single()
    assert rows["attrs"] == 1 and rows["products"] == 2


@pytest.mark.docker
@pytest.mark.graph
def test_an_edge_whose_endpoint_is_missing_is_refused_rather_than_merged(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """``MERGE`` on both endpoints would invent an unsourced node out of a typo'd id."""
    upsert_product(graph_schema_session, Product("p-real", "Real"), source=graph_source)
    with pytest.raises(ProvenanceRequired, match="do not exist"):
        link_sells(
            graph_schema_session, store_id="store-typo", product_id="p-real", source=graph_source
        )
    assert graph_schema_session.run("MATCH (s:Store) RETURN count(s) AS c").single()["c"] == 0


@pytest.mark.docker
@pytest.mark.graph
def test_the_offer_is_wired_store_makes_offer_offer_for_variant(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """DESIGN's ``MAKES_OFFER→FOR`` chain, and the ``SAME_AS`` confidence property."""
    _seed_full_model(graph_schema_session, graph_source)
    row = graph_schema_session.run(
        "MATCH (s:Store)-[:MAKES_OFFER]->(o:Offer)-[:FOR]->(v:Variant)<-[:HAS_VARIANT]-(p:Product) "
        "RETURN s.store_id AS store, o.offer_id AS offer, o.price AS price, "
        "v.variant_id AS variant, p.product_id AS product"
    ).single()
    assert row["store"] == "store-a" and row["offer"] == "off-a"
    assert row["variant"] == "v-a" and row["product"] == "p-a"
    assert row["price"] == pytest.approx(24.5)

    same_as = graph_schema_session.run(
        "MATCH (:Product {product_id:'p-a'})-[r:SAME_AS]->(:Product {product_id:'p-b'}) "
        "RETURN r.confidence AS confidence"
    ).single()
    assert same_as["confidence"] == pytest.approx(0.87)


@pytest.mark.docker
@pytest.mark.graph
def test_same_as_confidence_outside_zero_to_one_is_refused(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """T-022 keys entity resolution off this number; an out-of-range value is a bug."""
    upsert_product(graph_schema_session, Product("p-1", "One"), source=graph_source)
    upsert_product(graph_schema_session, Product("p-2", "Two"), source=graph_source)
    with pytest.raises(ValueError, match="confidence"):
        link_same_as(
            graph_schema_session,
            product_id="p-1",
            other_id="p-2",
            confidence=1.4,
            source=graph_source,
        )


# =======================================================================================
# 9. Embeddings in the graph, and the vector index  (acceptance 2)
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_stored_product_embeddings_are_1024_dimensional_and_unit_length(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """D19 checked at the storage layer, not just at the provider's return value.

    ``db.create.setNodeVectorProperty`` is what actually lands the vector; a provider that
    normalised correctly and a write path that mangled the value would still produce a
    useless index, and only reading it back out distinguishes the two.
    """
    session = graph_seeded_catalog["session"]
    rows = session.run(
        "MATCH (p:Product) WHERE p.embedding IS NOT NULL "
        "RETURN p.product_id AS product_id, p.embedding AS embedding"
    ).data()
    assert len(rows) == len(graph_seeded_catalog["product_ids"])
    for row in rows:
        vector = list(row["embedding"])
        assert len(vector) == 1024, row["product_id"]
        norm = math.sqrt(sum(float(c) * float(c) for c in vector))
        assert norm == pytest.approx(1.0, abs=1e-5), f"{row['product_id']} L2 norm {norm}"


@pytest.mark.docker
@pytest.mark.graph
def test_the_vector_index_returns_a_near_one_cosine_for_an_exact_match(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """``db.index.vector.queryNodes`` returns sensible cosine scores for a 1024-d vector.

    Queried with a product's own embedding text, that product must come back first with a
    cosine of ~1.0. The residue from 1.0 is the index's default float32 quantization, not a
    correctness problem — measured at ~3e-5 on this host.
    """
    session = graph_seeded_catalog["session"]
    for row in read_products(session, limit=100):
        text = embedding_text(row)
        results = candidate_products(
            session, query_text=text, provider=HashEmbedding(), status=None, limit=4
        )
        assert results, f"no candidate for {row['product_id']}"
        assert results[0].product_id == row["product_id"]
        assert cosine_from_score(results[0].score) == pytest.approx(
            1.0, abs=QUANTIZATION_TOLERANCE
        ), f"{row['product_id']} self-match cosine {cosine_from_score(results[0].score)}"


@pytest.mark.docker
@pytest.mark.graph
def test_the_vector_index_scores_an_unrelated_product_near_zero_cosine(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """The index discriminates: a hash embedding of unrelated text lands ~orthogonal.

    This is the honest counterpart to the exact-match test. It deliberately does **not**
    assert that "gentle vitamin c serum for sensitive skin" ranks the vitamin C serum first
    — ``HashEmbedding`` is content-addressed, not semantic, and that assertion would be
    false. What the cosine index has to do is separate identical from non-identical, and
    that is what is measured here.
    """
    session = graph_seeded_catalog["session"]
    results = candidate_products(
        session, query_text=PROBE_A, provider=HashEmbedding(), status=None, limit=10
    )
    assert {c.product_id for c in results} == set(graph_seeded_catalog["product_ids"]), (
        "every seeded product must be reachable through the vector index"
    )
    for candidate in results:
        assert abs(cosine_from_score(candidate.score)) < 0.2, (
            f"{candidate.product_id} scored cosine {cosine_from_score(candidate.score)} against "
            f"unrelated text"
        )


@pytest.mark.docker
@pytest.mark.graph
def test_setting_an_embedding_on_an_unknown_product_is_refused(
    graph_schema_session: Any,
) -> None:
    """An embedding is derived from a fact and must never create one."""
    with pytest.raises(ProvenanceRequired, match="p-nope"):
        set_product_embedding(graph_schema_session, product_id="p-nope", embedding=[0.0] * 1024)


# =======================================================================================
# 10. The candidate query  (acceptance 2)
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_the_candidate_query_returns_seeded_products_under_hash_embedding(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """Acceptance 2, stated plainly: vector + attribute retrieval finds the seeded catalog."""
    session = graph_seeded_catalog["session"]
    results = candidate_products(session, query_text=PROBE_A, limit=10)
    returned = {c.product_id for c in results}
    assert "prod-serum-c" in returned and "prod-spf-daily" in returned
    assert "prod-discontinued" not in returned, "the default status filter is `active`"
    for candidate in results:
        assert candidate.canonical_name and candidate.attributes and candidate.categories


@pytest.mark.docker
@pytest.mark.graph
def test_the_attribute_half_of_the_query_does_the_discriminating(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """Same query vector, different attribute filters, different answers.

    Under ``HashEmbedding`` the vector cannot separate these products (they are all
    ~orthogonal to the query), so any narrowing that happens is the attribute half doing it
    — which is precisely the "vector **+** attributes" retrieval DESIGN specifies, isolated.
    """
    session = graph_seeded_catalog["session"]
    unfiltered = {c.product_id for c in candidate_products(session, query_text=PROBE_A, limit=10)}
    assert len(unfiltered) == 3

    fragrance_free = {
        c.product_id
        for c in candidate_products(
            session,
            query_text=PROBE_A,
            attribute_filters=[AttributeFilter("fragrance_free", value_bool=True)],
            limit=10,
        )
    }
    assert fragrance_free == {"prod-serum-c", "prod-spf-daily"}

    high_spf = {
        c.product_id
        for c in candidate_products(
            session,
            query_text=PROBE_A,
            attribute_filters=[AttributeFilter("spf", min_number=30)],
            limit=10,
        )
    }
    assert high_spf == {"prod-spf-daily"}

    both = {
        c.product_id
        for c in candidate_products(
            session,
            query_text=PROBE_A,
            attribute_filters=[
                AttributeFilter("spf", min_number=30),
                AttributeFilter("skin_type", value_string="sensitive"),
                AttributeFilter("volume", equals_number=50, unit="ml"),
            ],
            limit=10,
        )
    }
    assert both == {"prod-spf-daily"}, "multiple filters must AND together"


@pytest.mark.docker
@pytest.mark.graph
def test_the_query_filters_by_category_ingredient_and_brand(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """The structured predicates a real intent carries, including the exclusion half."""
    session = graph_seeded_catalog["session"]
    assert {
        c.product_id for c in candidate_products(session, query_text=PROBE_A, category="Serum")
    } == {"prod-serum-c"}
    # Glycerin is in all three active products, and the lowercase spelling still matches the
    # "Glycerin" that was seeded: an ingredient filter narrows by ingredient, not by case.
    assert {
        c.product_id
        for c in candidate_products(
            session, query_text=PROBE_A, ingredients_all=["glycerin"], limit=10
        )
    } == {"prod-serum-c", "prod-cream-night", "prod-spf-daily"}
    assert {
        c.product_id
        for c in candidate_products(
            session, query_text=PROBE_A, ingredients_all=["Zinc Oxide"], limit=10
        )
    } == {"prod-spf-daily"}
    assert {
        c.product_id
        for c in candidate_products(
            session, query_text=PROBE_A, ingredients_all=["Glycerin", "Zinc Oxide"], limit=10
        )
    } == {"prod-spf-daily"}, "several required ingredients must AND together"
    assert {
        c.product_id
        for c in candidate_products(session, query_text=PROBE_A, brand="Northlight", limit=10)
    } == {"prod-serum-c", "prod-spf-daily"}


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.parametrize(
    ("key", "filter_kwargs", "expected"),
    [
        # prod-cream-night is the only fragranced product: `False` must select it, and must
        # NOT be read as "no fragrance filter supplied".
        ("fragrance_free", {"value_bool": False}, {"prod-cream-night"}),
        ("fragrance_free", {"value_bool": True}, {"prod-serum-c", "prod-spf-daily"}),
        # prod-serum-c carries spf=0, prod-spf-daily carries spf=50, prod-cream-night has no
        # spf attribute at all. So `equals_number=0` selects exactly the SPF-zero product,
        # and `min_number=0` selects both products that carry an spf reading.
        ("spf", {"equals_number": 0}, {"prod-serum-c"}),
        ("spf", {"min_number": 0}, {"prod-serum-c", "prod-spf-daily"}),
        ("spf", {"max_number": 0}, {"prod-serum-c"}),
    ],
)
def test_a_falsy_filter_component_is_still_a_filter(
    graph_seeded_catalog: dict[str, Any],
    key: str,
    filter_kwargs: dict[str, Any],
    expected: set[str],
) -> None:
    """``False`` and ``0`` are *values*, not "unset" — the classic falsy-vs-None bug.

    ``AttributeFilter.as_parameter`` distinguishes them with ``is None`` and the Cypher
    guards are ``f.X IS NULL OR …``. Written with a truthiness test on either side,
    ``value_bool=False`` would silently degrade into "no fragrance filter at all" and the
    query would hand the fragranced cream to a buyer who asked for fragrance-free — a wrong
    answer that no error message would ever mention. ``min_number=0`` and ``max_number=0``
    are the same trap on the numeric side.
    """
    session = graph_seeded_catalog["session"]
    results = candidate_products(
        session,
        query_text=PROBE_A,
        attribute_filters=[AttributeFilter(key, **filter_kwargs)],
        limit=10,
    )
    returned = {c.product_id for c in results}
    assert returned == expected
    assert returned != {"prod-serum-c", "prod-cream-night", "prod-spf-daily"}, (
        "the filter was ignored — a falsy component was treated as unset"
    )


@pytest.mark.docker
@pytest.mark.graph
def test_vector_path_recall_is_bounded_by_the_index_fetch_and_the_structured_path_is_not(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """A known, documented limit of post-filtering a vector top-k — pinned, not hidden.

    ``db.index.vector.queryNodes`` takes a ``k``, not a predicate, so the structured filters
    are applied *after* the index has already chosen its top ``limit * oversample`` rows. A
    product that satisfies the filter but ranks below that cut is not returned. With the
    default oversample of 8 this is invisible at realistic catalog sizes, and with a
    deliberately tiny fetch it is provable.

    The mitigation is in the library, not in a comment: a highly selective structured query
    can be issued with **no vector at all**, and that path has no top-k bound. This test
    asserts both halves so a future reader knows which tool to reach for.
    """
    # 120 products; every EVEN-numbered one carries the filter attribute, so 60 match.
    seed_products(
        graph_schema_session,
        [
            {
                "product_id": f"bulk-{i:03d}",
                "canonical_name": f"Bulk Product {i}",
                "attributes": ([AttributeValue("half", value_bool=True)] if i % 2 == 0 else []),
            }
            for i in range(120)
        ],
        source=graph_source,
    )
    reembed_products(graph_schema_session, HashEmbedding())
    half = [AttributeFilter("half", value_bool=True)]

    matching = candidate_products(graph_schema_session, attribute_filters=half, limit=200)
    assert len(matching) == 60, "the structured path sees every matching product"

    # THE BOUND, demonstrated rather than asserted-into-existence. fetch = limit*oversample
    # = 20 rows out of 120, and roughly half of those survive the filter — so the vector
    # path returns FEWER than the 20 requested even though 60 products qualify. An earlier
    # version of this test asserted `len(result) <= limit`, which the Cypher `LIMIT $limit`
    # makes unconditionally true and which would have passed with a fetch of a million.
    starved = candidate_products(
        graph_schema_session, query_text=PROBE_A, attribute_filters=half, limit=20, oversample=1
    )
    assert len(starved) < 20, (
        f"expected the post-filter to starve a fetch of 20 over 120 products, got "
        f"{len(starved)} — if this stops being true the oversample bound has changed"
    )
    assert {c.product_id for c in starved} <= {c.product_id for c in matching}

    # Widening the window recovers them, which is what `oversample` is for.
    wide = candidate_products(
        graph_schema_session, query_text=PROBE_A, attribute_filters=half, limit=20, oversample=8
    )
    assert len(wide) == 20 > len(starved)


@pytest.mark.docker
@pytest.mark.graph
def test_the_query_can_exclude_an_ingredient(graph_seeded_catalog: dict[str, Any]) -> None:
    """ "No parfum" is a hard constraint (R19), and it has to be expressible."""
    session = graph_seeded_catalog["session"]
    results = candidate_products(
        session, query_text=PROBE_A, status=None, ingredients_none=["Parfum"], limit=10
    )
    returned = {c.product_id for c in results}
    assert "prod-cream-night" not in returned
    assert "prod-serum-c" in returned


@pytest.mark.docker
@pytest.mark.graph
def test_the_structured_only_path_returns_seeded_products_without_a_vector(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """Attributes alone are a legitimate retrieval; a name alone is not."""
    session = graph_seeded_catalog["session"]
    results = candidate_products(
        session, attribute_filters=[AttributeFilter("fragrance_free", value_bool=True)], limit=10
    )
    assert [c.product_id for c in results] == ["prod-serum-c", "prod-spf-daily"]
    assert all(c.score == 0.0 for c in results), "the structured path reports no similarity"


@pytest.mark.docker
@pytest.mark.graph
def test_a_query_with_neither_a_vector_nor_a_predicate_is_refused(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """The only way to answer it would be the free-text scan DESIGN forbids."""
    with pytest.raises(UnretrievableQuery, match="free-text"):
        candidate_products(graph_seeded_catalog["session"], limit=5)
    with pytest.raises(UnretrievableQuery):
        candidate_products(graph_seeded_catalog["session"], query_text="", limit=5)


@pytest.mark.docker
@pytest.mark.graph
def test_a_precomputed_embedding_takes_precedence_over_query_text(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """T-031 embeds once per intent and reuses the vector across stores; that has to work."""
    session = graph_seeded_catalog["session"]
    vector = HashEmbedding().embed(PROBE_A)
    by_vector = candidate_products(session, embedding=vector, limit=10)
    by_text = candidate_products(session, query_text=PROBE_A, limit=10)
    assert [(c.product_id, round(c.score, 6)) for c in by_vector] == [
        (c.product_id, round(c.score, 6)) for c in by_text
    ]


@pytest.mark.docker
@pytest.mark.graph
def test_the_query_reports_products_that_were_never_embedded(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """An unembedded product is invisible to every vector query — silently, unless asked.

    This is the failure the re-embed script exists to prevent, so it needs a detector that
    is not itself the re-embed script.
    """
    upsert_product(graph_schema_session, Product("p-dark", "Never Embedded"), source=graph_source)
    assert products_missing_embeddings(graph_schema_session) == ["p-dark"]
    assert candidate_products(graph_schema_session, query_text=PROBE_A, limit=5) == []


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.parametrize(("limit", "oversample"), [(0, 8), (5, 0), (-1, 1)])
def test_the_query_refuses_a_nonsensical_page_size(
    graph_seeded_catalog: dict[str, Any], limit: int, oversample: int
) -> None:
    """A zero or negative fetch would make ``queryNodes`` raise deep inside the driver."""
    with pytest.raises(ValueError):
        candidate_products(
            graph_seeded_catalog["session"],
            query_text=PROBE_A,
            limit=limit,
            oversample=oversample,
        )


# =======================================================================================
# 11. The re-embed pass  (acceptance 3)
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_reembed_passes_on_the_sample_catalog_and_leaves_nothing_unembedded(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """Acceptance 3: the re-embed script passes on sample data."""
    report = graph_seeded_catalog["report"]
    assert report.provider == "hash"
    assert report.dimension == 1024
    assert report.products == len(graph_seeded_catalog["product_ids"]) == 4
    assert report.embedded == report.products
    assert report.skipped == []
    assert report.complete is True
    assert products_missing_embeddings(graph_seeded_catalog["session"]) == []


@pytest.mark.docker
@pytest.mark.graph
def test_reembed_is_deterministic_and_converges(graph_seeded_catalog: dict[str, Any]) -> None:
    """Running it twice must produce byte-identical vectors, not merely valid ones."""
    session = graph_seeded_catalog["session"]
    before = dict(
        session.run(
            "MATCH (p:Product) RETURN p.product_id AS id, p.embedding AS embedding"
        ).values()
    )
    second = reembed_products(session, HashEmbedding())
    after = dict(
        session.run(
            "MATCH (p:Product) RETURN p.product_id AS id, p.embedding AS embedding"
        ).values()
    )
    assert second.complete
    assert {k: list(v) for k, v in before.items()} == {k: list(v) for k, v in after.items()}


@pytest.mark.docker
@pytest.mark.graph
def test_reembed_batches_over_more_products_than_one_batch_holds(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """The SKIP/LIMIT paging must not drop or double-count products.

    A batching bug that re-reads page 0 forever is an infinite loop; one that skips the tail
    leaves products silently unembedded. Both are invisible with a fixture of four rows.
    """
    seed_products(
        graph_schema_session,
        [{"product_id": f"bulk-{i:03d}", "canonical_name": f"Bulk Product {i}"} for i in range(25)],
        source=graph_source,
    )
    report = reembed_products(graph_schema_session, HashEmbedding(), batch_size=4)
    assert report.products == 25 and report.embedded == 25 and report.complete
    assert products_missing_embeddings(graph_schema_session) == []


@pytest.mark.docker
@pytest.mark.graph
def test_reembed_reports_rather_than_hides_a_product_with_no_embeddable_text(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """A zero vector cannot be ranked by cosine, so writing one would hide the product.

    Leaving it unembedded and naming it in ``report.skipped`` (and in the non-zero CLI exit
    status) is the honest behaviour: the operator finds out.
    """
    upsert_product(graph_schema_session, Product("p-blank", ""), source=graph_source)
    report = reembed_products(graph_schema_session, HashEmbedding())
    assert report.products == 1
    assert report.embedded == 0
    assert report.skipped == ["p-blank"]
    assert report.complete is False


@pytest.mark.docker
@pytest.mark.graph
def test_reembed_follows_the_environment_with_no_code_change(
    graph_seeded_catalog: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 3: the provider swap is config-only, proven at the re-embed call site.

    ``reembed_products`` is called with **no provider argument** both times. Setting
    ``EMBEDDING_PROVIDER=local_bge`` reroutes it to the model provider, which then fails on
    the absent weights — that failure is the evidence the environment alone selected a
    different provider. Restoring ``hash`` restores the pass.
    """
    session = graph_seeded_catalog["session"]
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")
    assert reembed_products(session).provider == "hash"

    monkeypatch.setenv("EMBEDDING_PROVIDER", "local_bge")
    if LocalBgeEmbedding.probe() is None:  # pragma: no cover - only with the optional extra
        assert reembed_products(session).provider == "local_bge"
    else:
        with pytest.raises(EmbeddingProviderUnavailable):
            reembed_products(session)

    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")
    assert reembed_products(session).complete


@pytest.mark.docker
@pytest.mark.graph
def test_reembed_refuses_a_nonsensical_batch_size(graph_schema_session: Any) -> None:
    """A batch size of zero would page forever."""
    with pytest.raises(ValueError, match="batch_size"):
        reembed_products(graph_schema_session, HashEmbedding(), batch_size=0)


@pytest.mark.docker
@pytest.mark.graph
def test_the_whole_pipeline_is_provenanced_end_to_end(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """One final read of the seeded, embedded catalog: no unsourced fact anywhere.

    The seeded catalog is what T-020…T-031 will build on, so the invariant is asserted on
    the *fixture* as well as on the hand-written full-model seed — a fixture that quietly
    produced unsourced facts would hand every downstream ticket a broken graph.
    """
    assert_provenance_complete(graph_seeded_catalog["session"])
    assert provenance_violations(graph_seeded_catalog["session"]) == []


# =======================================================================================
# 12. Findings from the adversarial review — every one of these is a regression guard
#     for a defect that shipped and was caught by a verifier, not by the suite above.
# =======================================================================================


#: Frozen golden values for every content-hashed stable ID. These are **persisted in a
#: shared database** and six downstream tickets key on them, so the derivation is a
#: published format, not an implementation detail: change it and every previously-written
#: AttributeValue / Category / Ingredient node is orphaned while the graph still looks
#: healthy. Three separate mutations of the digest (always-``repr`` numbers, a different
#: prefix, a reordered digest tuple) passed the entire suite before these existed.
#:
#: TWO OF THESE VALUES CHANGED, DELIBERATELY (W1-32). ``attribute_value_id`` built its
#: readable prefix from ``slug(key)`` and its digest from ``canonical_text(key)``, and those
#: two folds disagree on separators. ``"fragrance_free"`` and ``"fragrance free"`` therefore
#: produced the *same* prefix ``av_fragrance-free_`` and *different* digests: one fact became
#: two nodes and an attribute filter partitioned the catalog by separator
#: (``'fragrance_free' -> ['p-under']``, ``'fragrance free' -> ['p-space']``). The digest now
#: hashes ``slug(key)``, so prefix and digest agree, and the two keys converge on one node —
#: pinned by ``test_a_separator_variant_of_a_key_converges_on_one_node`` below.
#:
#: The old values encoded a generator that split one fact in two, so they were wrong before
#: this change and would still be wrong if it were reverted. **Existing graphs need a
#: re-ingest, not a migration**: the pre-change ids are not recoverable from the post-change
#: ones (the fold is lossy), so there is nothing to rewrite them *to* — re-run the adapters.
#: Only the two keys whose ``slug`` differs from their ``canonical_text`` moved; ``spf``,
#: ``volume``, and every Category/Ingredient id are byte-for-byte unchanged, which is itself
#: the evidence that only the separator half of the fold was broken.
GOLDEN_IDS = (
    (lambda: attribute_value_id("spf", value_number=50), "av_spf_28ad4f935b11a89e782fad898c0bb8cd"),
    (
        # was av_fragrance-free_a98d914b39260a515c3cf4c8638cdc22 — a digest over
        # canonical_text("fragrance_free"), i.e. over a fold the prefix never showed.
        lambda: attribute_value_id("fragrance_free", value_bool=True),
        "av_fragrance-free_4107460e89d3dc7c080b1744ff5bb31f",
    ),
    (
        # was av_skin-type_0d80ebc63724e24211d495207b37dea6 — same defect.
        lambda: attribute_value_id("skin_type", value_string="Sensitive"),
        "av_skin-type_1cc8559076bf559096d8f4eb5e68f29f",
    ),
    (
        lambda: attribute_value_id("volume", value_number=30, unit="ml"),
        "av_volume_a2230b2389169769fafc60a8f121c397",
    ),
    (lambda: category_id("Serum"), "cat_serum_cb1b92104bae4ba70f1f20d3160967f2"),
    (
        lambda: category_id("Serum", parent="Skincare"),
        "cat_serum_c5f463f0ea8ecd14f1db201d0b25be48",
    ),
    (lambda: ingredient_id("Ascorbic Acid"), "ing_ascorbic-acid_08442d1bb64c5a7db4358d2fc760cfce"),
)


@pytest.mark.parametrize(("compute", "expected"), GOLDEN_IDS, ids=[g[1][:14] for g in GOLDEN_IDS])
def test_stable_ids_match_their_frozen_golden_values(compute: Any, expected: str) -> None:
    """The ID derivation is a published format, not an implementation detail.

    If this fails, the change is not "the test is stale" — it is a migration. Every node
    already in the graph keeps its old id, so the new derivation silently forks the catalog
    in two and entity resolution (T-022) resolves against half of it.
    """
    assert compute() == expected


def test_the_number_token_distinguishes_integral_from_fractional_readings() -> None:
    """``50`` and ``50.0`` are one reading; ``50`` and ``50.5`` are not.

    Pinned separately from the golden values because the collapse is the *purpose* of
    ``_number_token``: a mutation making it always ``repr()`` still produces stable, unique
    ids — just different ones — so only a golden value or this equivalence catches it.
    """
    assert attribute_value_id("spf", value_number=50) == attribute_value_id(
        "spf", value_number=50.0
    )
    assert attribute_value_id("spf", value_number=50) != attribute_value_id(
        "spf", value_number=50.5
    )


@pytest.mark.parametrize(
    ("factory", "expected_properties"),
    [
        (
            lambda: Category(name="Serum", parent="Skincare"),
            {"category_id", "name", "slug", "parent"},
        ),
        (lambda: Ingredient(name="Niacinamide"), {"ingredient_id", "name", "canonical_name"}),
        (lambda: IntentCluster(cluster_id="c-1", label="serums"), {"cluster_id", "label"}),
    ],
)
def test_vocabulary_nodes_carry_only_term_derived_properties(
    factory: Any, expected_properties: set[str]
) -> None:
    """The invariant that makes the ``SUPPORTED_BY`` carve-out safe, pinned exactly.

    ``Category`` / ``Ingredient`` / ``IntentCluster`` are excluded from the node provenance
    audit because they are *terms*, not observations — the observation is the edge, and the
    edge does carry a resolvable ``source_id``. That reasoning holds only while every
    property is derived from the term itself. Add ``Ingredient.is_allergen`` or
    ``Category.regulated_in`` and a real-world claim lands in the graph unsourced with
    :func:`provenance_violations` still returning ``[]``.

    So widening one of these dataclasses fails here, deliberately, and the fix is to move
    the label into ``MATERIAL_FACT_LABELS`` rather than to update this expectation.
    """
    assert set(factory().as_properties()) == expected_properties


def test_the_vector_index_statement_is_parameterised_by_width() -> None:
    """``rebuild_vector_index`` exists to change the width, so the statement must vary.

    It previously re-ran a module constant with 1024 baked in, which made the function a
    no-op for its only purpose: a rebuild for a 512-d provider produced another 1024-d
    index, and the subsequent re-embed wrote a whole catalog that the index could never
    match — exit status 0, no warning that mattered.
    """
    from ingest.graph.schema import vector_index_statement

    assert vector_index_statement() == VECTOR_INDEX_STATEMENT
    assert "`vector.dimensions`: 512" in vector_index_statement(512)
    assert "`vector.dimensions`: 1024" not in vector_index_statement(512)
    assert "IF NOT EXISTS" in vector_index_statement(512)
    with pytest.raises(ValueError, match="positive"):
        vector_index_statement(0)


def test_a_hollow_source_is_refused() -> None:
    """ "Every material fact has a Source" must not be satisfiable by a Source pointing
    at nothing.

    Before this, ``Source("x", "", "", "", "", 0.0, "seller_asserted")`` constructed
    happily, and a Product supported by it passed ``assert_provenance_complete()``. That
    made the entire provenance rule greenable by an adapter that recorded no provenance.
    """
    base = {
        "source_id": "src-1",
        "url": "https://example.test/p",
        "content_hash": "sha256:aa",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "extractor_version": "v1",
        "confidence": 0.5,
        "source_class": "scraped",
    }
    for blank in ("url", "content_hash", "observed_at", "extractor_version"):
        with pytest.raises(ValueError, match=blank):
            Source(**{**base, blank: "   "})


def test_the_source_scans_cover_every_module_in_the_service_not_just_src_graph() -> None:
    """The static scans must be recursive and service-wide, or they check almost nothing.

    Both scans used ``GRAPH_SRC.glob("*.py")`` — five files, non-recursive. A
    ``HashEmbedding()`` in ``src/adapters/``, or name-matching Cypher in a future
    ``src/graph/retrieval/`` subpackage, was simply invisible to them.
    """
    assert len(SCANNED_SOURCES) > 5
    scanned = {p.name for p in SCANNED_SOURCES}
    assert {"query.py", "upsert.py", "schema.py", "model.py", "reembed.py"} <= scanned
    assert any("embeddings" in str(p) for p in SCANNED_SOURCES)


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.parametrize("length", [512, 1023, 1025])
def test_a_wrong_length_embedding_is_refused_rather_than_silently_stored(
    graph_schema_session: Any, graph_source: Source, length: int
) -> None:
    """Measured: Neo4j accepts a 512-d or 1025-d vector into a 1024-d index without error.

    Nothing downstream complains either — ``queryNodes`` just never returns that node,
    ``candidate_products`` never surfaces it, and ``products_missing_embeddings()`` calls it
    embedded because it *has* an embedding. The product is silently unrankable, which is the
    single worst failure mode in this library: it looks exactly like a correct catalog. So
    the width is refused in Python before the write reaches the procedure.
    """
    from ingest.graph import EmbeddingDimensionMismatch

    upsert_product(graph_schema_session, Product("p-dim", "Dimension Probe"), source=graph_source)
    with pytest.raises(EmbeddingDimensionMismatch, match=str(length)):
        set_product_embedding(graph_schema_session, product_id="p-dim", embedding=[0.1] * length)
    assert products_missing_embeddings(graph_schema_session) == ["p-dim"], (
        "a refused write must leave the product visibly unembedded, not half-embedded"
    )


@pytest.mark.docker
@pytest.mark.graph
def test_a_partial_upsert_never_leaves_an_orphaned_node(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """``upsert_offer`` is three statements; a bad endpoint must write none of them.

    Before the pre-flight check, a typo'd ``store_id`` let the Offer node land and *then*
    raised — leaving an orphaned, correctly-sourced Offer attached to nothing, which
    ``provenance_violations()`` quite correctly reported as clean. The audit was not wrong;
    the write was.
    """
    upsert_store(graph_schema_session, Store("st-real", "real.example"), source=graph_source)

    with pytest.raises(ProvenanceRequired, match="Variant"):
        upsert_offer(
            graph_schema_session,
            Offer("off-x", 9.99, "USD", "in_stock", "2026-01-01T00:00:00+00:00"),
            store_id="st-real",
            variant_id="v-missing",
            source=graph_source,
        )
    assert graph_schema_session.run("MATCH (o:Offer) RETURN count(o) AS c").single()["c"] == 0, (
        "the Offer node was written before the endpoint check failed"
    )

    with pytest.raises(ProvenanceRequired, match="Product"):
        upsert_variant(
            graph_schema_session,
            Variant("v-x", "SKU-X"),
            product_id="p-missing",
            source=graph_source,
        )
    assert graph_schema_session.run("MATCH (v:Variant) RETURN count(v) AS c").single()["c"] == 0, (
        "the Variant node was written before the endpoint check failed"
    )
    assert provenance_violations(graph_schema_session) == []


@pytest.mark.docker
@pytest.mark.graph
def test_an_empty_brand_is_a_query_not_an_error(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """``Product.brand`` defaults to ``""``, so "products with no brand" must be askable.

    The structured-predicate gate was a truthiness test, so ``brand=""`` fell through to
    "no predicate supplied" and raised ``UnretrievableQuery`` on a perfectly well-formed
    request.
    """
    seed_products(
        graph_schema_session,
        [
            {"product_id": "p-nobrand", "canonical_name": "Unbranded Thing"},
            {"product_id": "p-branded", "canonical_name": "Branded Thing", "brand": "Northlight"},
        ],
        source=graph_source,
    )
    reembed_products(graph_schema_session, HashEmbedding())
    assert [c.product_id for c in candidate_products(graph_schema_session, brand="")] == [
        "p-nobrand"
    ]
    assert [c.product_id for c in candidate_products(graph_schema_session, brand="Northlight")] == [
        "p-branded"
    ]


@pytest.mark.docker
@pytest.mark.graph
def test_rebuilding_the_index_at_another_width_actually_changes_the_width(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """The provider-swap path, end to end, for a provider that is NOT 1024-d.

    Three things have to hold together or a non-1024 swap fails silently: the rebuild has
    to produce an index of the requested width, the re-embed has to refuse to write vectors
    that do not match the live index, and the refusal has to be loud.
    """
    from ingest.graph import EmbeddingDimensionMismatch

    upsert_product(graph_schema_session, Product("p-w", "Width Probe"), source=graph_source)
    try:
        report = rebuild_vector_index(graph_schema_session, dimensions=512)
        assert report.vector_dimensions == 512
        assert report.vector_similarity == "cosine"

        # HashEmbedding is 1024-d; against a 512-d index that must be refused, not written.
        with pytest.raises(EmbeddingDimensionMismatch, match="512"):
            reembed_products(graph_schema_session, HashEmbedding())
        assert products_missing_embeddings(graph_schema_session) == ["p-w"]
    finally:
        rebuild_vector_index(graph_schema_session, dimensions=VECTOR_INDEX_DIMENSIONS)
    assert schema_report(graph_schema_session).vector_dimensions == 1024


@pytest.mark.docker
@pytest.mark.graph
def test_reembed_refuses_to_write_into_an_index_that_does_not_exist(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """Without the index, every vector written is unreachable — say so instead of writing."""
    upsert_product(graph_schema_session, Product("p-noidx", "No Index"), source=graph_source)
    graph_schema_session.run(f"DROP INDEX {VECTOR_INDEX_NAME} IF EXISTS").consume()
    try:
        with pytest.raises(RuntimeError, match=VECTOR_INDEX_NAME):
            reembed_products(graph_schema_session, HashEmbedding())
    finally:
        apply_schema(graph_schema_session)
    assert schema_report(graph_schema_session).vector_dimensions == 1024


@pytest.mark.docker
@pytest.mark.graph
def test_apply_schema_actually_waits_for_the_indexes_it_creates(
    graph_schema_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``await_indexes`` was replaceable with ``return None`` and the suite stayed green.

    That left the awaits untested in both directions: either they are load-bearing (and the
    suite was only accidentally green, ready to flake on a slower host) or they are dead
    blocking calls. This pins the plumbing — the call happens, and ``await_online=False``
    genuinely skips it — so a future removal is a deliberate change to this test.
    """
    from ingest.graph import schema as schema_module

    calls: list[int] = []
    real = schema_module.await_indexes

    def spy(session: Any, *, timeout: int = 300) -> None:
        calls.append(timeout)
        real(session, timeout=timeout)

    monkeypatch.setattr(schema_module, "await_indexes", spy)
    schema_module.apply_schema(graph_schema_session)
    assert calls == [300], "apply_schema must wait for index population by default"

    calls.clear()
    schema_module.apply_schema(graph_schema_session, await_online=False)
    assert calls == [], "await_online=False must actually skip the wait"

    # And the real procedure must be callable and cheap on a settled database.
    real(graph_schema_session, timeout=30)


@pytest.mark.docker
@pytest.mark.graph
def test_the_query_predicate_indexes_match_what_the_query_actually_compares(
    graph_schema_session: Any,
) -> None:
    """The index list is checked against the shipped Cypher, not restated from itself.

    The previous version of this test asserted that ``LOOKUP_INDEXES`` contained what
    ``LOOKUP_INDEXES`` contained. It therefore never noticed that the query compares
    ``a.canonical_value_string`` while the index was on ``AttributeValue.value_string``, nor
    that ``ingredient_canonical_name`` and ``category_slug`` are never touched by the query
    at all (it matches ingredients and categories by content-hashed id).
    """
    from ingest.graph.query import _FILTER_AND_RETURN
    from ingest.graph.schema import QUERY_PREDICATE_INDEXES

    # The filters run against a *collected map*, whose fields are renamed node properties
    # (`key: a.canonical_key`). Resolve that renaming first, or the check compares map field
    # names against node property names and is wrong in both directions.
    aliases = dict(re.findall(r"(\w+):\s*a\.(\w+)", _FILTER_AND_RETURN))
    assert aliases.get("key") == "canonical_key", "the collect-map aliasing changed"

    compared = set(re.findall(r"\ba\.(\w+)\s*(?:=|>=|<=)\s*f\.", _FILTER_AND_RETURN))
    compared |= {prop for prop in ("brand", "status") if f"p.{prop} = $" in _FILTER_AND_RETURN}
    compared = {aliases.get(prop, prop) for prop in compared}
    indexed = {prop for _name, _label, prop in QUERY_PREDICATE_INDEXES}
    assert compared, "failed to extract any predicate from the shipped Cypher"
    assert compared <= indexed, f"the query filters on unindexed properties: {compared - indexed}"

    live = set(schema_report(graph_schema_session).indexes)
    for name, _label, _prop in QUERY_PREDICATE_INDEXES:
        assert name in live, f"{name} is declared but not present in the database"


# =======================================================================================
# 13. Findings from the second adversarial review (wave 1). Same rule as section 12:
#     every test below is a regression guard for a defect that was reproduced live.
# =======================================================================================


class _SecondProvider(EmbeddingProvider):
    """A second, entirely valid, 1024-d provider — the shape of the ``local_bge`` swap.

    ``.env.example`` sets ``EMBEDDING_PROVIDER=hash`` and ``make e2e-live`` sets
    ``local_bge``; both declare ``EMBEDDING_DIM``. So the width guard in ``reembed_products``
    is structurally incapable of noticing the swap, and a double that declares a *different*
    width would not reproduce the defect at all.
    """

    name = "second"
    dimension = EMBEDDING_DIM

    def embed(self, text: str) -> list[float]:
        """Embed into a different 1024-d space than ``HashEmbedding``."""
        return hash_embed("SECOND::" + text, dim=self.dimension)


class _DiesPartWayThrough(EmbeddingProvider):
    """A provider that fails after the first batch — a network blip, not a misconfiguration."""

    name = "flaky"
    dimension = EMBEDDING_DIM

    def __init__(self, *, fail_after: int = 1) -> None:
        self.fail_after = fail_after
        self.batches = 0

    def embed(self, text: str) -> list[float]:
        """Embed one text into the second provider's space."""
        return hash_embed("SECOND::" + text, dim=self.dimension)

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Answer ``fail_after`` batches, then raise."""
        self.batches += 1
        if self.batches > self.fail_after:
            raise ConnectionError("the embedding backend went away mid-pass")
        return [self.embed(text) for text in texts]


@pytest.mark.docker
@pytest.mark.graph
def test_a_completed_reembed_records_which_provider_wrote_the_vectors(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """W1-27, half one: the graph now says who wrote the vectors it holds."""
    from ingest.graph import EMBEDDING_RUN_COMPLETE, embedding_run

    run = embedding_run(graph_seeded_catalog["session"])
    assert run is not None, "a completed re-embed must leave a marker"
    assert run.index == VECTOR_INDEX_NAME
    assert run.provider == "hash"
    assert run.dimension == VECTOR_INDEX_DIMENSIONS
    assert run.state == EMBEDDING_RUN_COMPLETE
    assert run.complete is True
    assert run.products == run.embedded == len(graph_seeded_catalog["product_ids"])


@pytest.mark.docker
@pytest.mark.graph
def test_querying_vectors_written_by_another_provider_is_refused(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """W1-27: a second valid 1024-d provider re-embeds; the default one must not rank it.

    Measured before the fix: no exception, ``report.complete is True``,
    ``products_missing_embeddings() == []``, the index ``ONLINE`` — and the catalog silently
    re-ranked, because cosine between two unrelated 1024-d spaces is noise that looks
    exactly like a similarity. The width guard cannot fire: ``HashEmbedding.dimension`` and
    ``LocalBgeEmbedding.dimension`` are both ``EMBEDDING_DIM``.
    """
    from ingest.graph import EmbeddingProviderMismatch, embedding_run

    session = graph_seeded_catalog["session"]
    before = [c.product_id for c in candidate_products(session, query_text=PROBE_A, limit=10)]
    assert before, "the fixture's own provider must be able to query its own vectors"

    reembed_products(session, _SecondProvider())
    assert products_missing_embeddings(session) == [], (
        "the point of this defect is that every existing detector stays clean"
    )
    run = embedding_run(session)
    assert run is not None and run.provider == "second"

    with pytest.raises(EmbeddingProviderMismatch, match="second"):
        candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=10)
    # The precomputed-vector entry point takes precedence over query_text, so it needs the
    # same guard or the check is bypassed by the parameter a caller is likelier to use.
    with pytest.raises(EmbeddingProviderMismatch):
        candidate_products(
            session, embedding=HashEmbedding().embed(PROBE_A), provider=HashEmbedding(), limit=10
        )
    # And the provider that actually wrote them still works.
    assert [
        c.product_id
        for c in candidate_products(
            session, query_text=PROBE_A, provider=_SecondProvider(), limit=10
        )
    ]


@pytest.mark.docker
@pytest.mark.graph
def test_an_interrupted_reembed_is_visible_rather_than_silent(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """X1: a failure on page two leaves two vector spaces in one index.

    ``reembed_products`` pages with SKIP/LIMIT and the per-product writes auto-commit, so
    nothing rolls back. Before the marker, the only trace of a half-finished pass was a
    catalog that ranked wrongly: ``products_missing_embeddings()`` reported ``[]`` and the
    exception was the operator's only warning — and ``main()`` caught only
    ``EmbeddingDimensionMismatch``, so a ``ConnectionError`` here was not even that.
    """
    from ingest.graph import EMBEDDING_RUN_RUNNING, EmbeddingRunIncomplete, embedding_run

    session = graph_seeded_catalog["session"]
    with pytest.raises(ConnectionError):
        reembed_products(session, _DiesPartWayThrough(fail_after=1), batch_size=1)

    assert products_missing_embeddings(session) == [], (
        "the interrupted pass leaves every product carrying *some* vector — that is the trap"
    )
    run = embedding_run(session)
    assert run is not None
    assert run.state == EMBEDDING_RUN_RUNNING, "the half-finished pass must still be marked open"
    assert run.complete is False

    with pytest.raises(EmbeddingRunIncomplete, match="running"):
        candidate_products(session, query_text=PROBE_A, provider=_DiesPartWayThrough(), limit=10)

    # Completing a pass clears it.
    reembed_products(session, HashEmbedding())
    assert embedding_run(session).complete is True
    assert candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=10)


@pytest.mark.docker
@pytest.mark.graph
def test_vectors_of_unrecorded_provenance_are_still_queryable(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """The guard must not break the seam six downstream tickets use.

    T-020…T-024 and T-031 seed ``Product.embedding`` through ``set_product_embedding`` with
    the shared ``hash_embedding`` fixture and never run the re-embed pass, so no marker
    exists. Unknown provenance is not the same as *wrong* provenance, and refusing here
    would have made the fix worse than the defect.
    """
    from ingest.graph import embedding_run

    upsert_product(
        graph_schema_session, Product("p-seeded", "Directly Seeded"), source=graph_source
    )
    set_product_embedding(
        graph_schema_session, product_id="p-seeded", embedding=hash_embed("Directly Seeded")
    )
    assert embedding_run(graph_schema_session) is None
    assert [
        c.product_id
        for c in candidate_products(graph_schema_session, query_text="Directly Seeded", limit=5)
    ] == ["p-seeded"]


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.parametrize(
    ("vector", "fragment"),
    [
        pytest.param(lambda: hash_embed(""), "L2 norm", id="the-empty-text-vector"),
        pytest.param(lambda: [0.0] * EMBEDDING_DIM, "L2 norm", id="all-zero"),
        pytest.param(lambda: [1e-200] * EMBEDDING_DIM, "L2 norm", id="denormal-underflow"),
        pytest.param(lambda: [float("nan")] + [0.1] * (EMBEDDING_DIM - 1), "non-finite", id="nan"),
        pytest.param(lambda: [float("inf")] + [0.1] * (EMBEDDING_DIM - 1), "non-finite", id="inf"),
    ],
)
def test_a_degenerate_vector_is_refused_by_the_write_path(
    graph_schema_session: Any, graph_source: Source, vector: Any, fragment: str
) -> None:
    """W1-30: ``hash_embed("")`` is exactly the all-zero 1024-d vector, and it was stored.

    Measured before the fix: ``set_product_embedding`` wrote it without complaint, the
    product then disappeared from ``candidate_products`` permanently, and
    ``products_missing_embeddings()`` still reported ``[]`` because the product *has* an
    embedding. That is verbatim the failure this function's own docstring says the guard
    exists to prevent — ``reembed_products`` guarded it in the caller instead.

    ``[1e-200] * 1024`` is the case a ``not any(vector)`` guard misses: every component is
    non-zero, every square underflows to 0.0, and the L2 norm is exactly 0.0.
    """
    from ingest.graph import InvalidEmbeddingVector

    upsert_product(graph_schema_session, Product("p-degen", "Degenerate"), source=graph_source)
    with pytest.raises(InvalidEmbeddingVector, match=fragment):
        set_product_embedding(graph_schema_session, product_id="p-degen", embedding=vector())
    assert products_missing_embeddings(graph_schema_session) == ["p-degen"], (
        "a refused write must leave the product visibly unembedded"
    )


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.parametrize(
    ("vector", "fragment"),
    [
        pytest.param(lambda: [0.1] * 512, "512-d", id="too-short"),
        pytest.param(lambda: [0.0] * EMBEDDING_DIM, "L2 norm", id="all-zero"),
        pytest.param(lambda: [1e-200] * EMBEDDING_DIM, "L2 norm", id="denormal-underflow"),
        pytest.param(lambda: [float("nan")] + [0.1] * (EMBEDDING_DIM - 1), "non-finite", id="nan"),
    ],
)
def test_a_degenerate_vector_is_refused_by_the_read_path(
    graph_seeded_catalog: dict[str, Any], vector: Any, fragment: str
) -> None:
    """W1-31: the ``embedding=`` entry point took precedence and was completely unguarded.

    Measured before the fix, ``candidate_products(embedding=[0.1] * 512)`` raised
    ``neo4j.exceptions.ClientError`` — neither of the two exceptions the docstring declares,
    and ``isinstance(exc, ValueError)`` was ``False``, so the ``Raises:`` block was
    machine-contradicted. The ``query_text`` half was guarded; the half a caller is likelier
    to use was not.
    """
    from ingest.graph import InvalidEmbeddingVector

    with pytest.raises(InvalidEmbeddingVector, match=fragment) as caught:
        candidate_products(graph_seeded_catalog["session"], embedding=vector(), limit=5)
    assert isinstance(caught.value, ValueError), "the docstring declares ValueError"


def test_one_definition_of_a_legitimate_vector_serves_both_entry_points() -> None:
    """Both doors into the index consult the same predicate, so they cannot drift apart."""
    from ingest.graph import embedding_vector_defect
    from ingest.graph import query as query_module
    from ingest.graph import upsert as upsert_module_local

    assert query_module.embedding_vector_defect is embedding_vector_defect
    assert upsert_module_local.embedding_vector_defect is embedding_vector_defect
    assert embedding_vector_defect([0.1] * EMBEDDING_DIM) is None
    assert embedding_vector_defect(hash_embed("real text")) is None
    assert embedding_vector_defect([0.1] * 512)[0] == "dimension"
    assert embedding_vector_defect([0.0] * EMBEDDING_DIM)[0] == "value"
    # The bound the length check is taken against is a parameter, not D6's 1024, so a
    # rebuilt index of another width validates against *its* width.
    assert embedding_vector_defect([0.1] * 512, dimensions=512) is None


def test_the_attribute_id_prefix_and_digest_hash_the_same_fold() -> None:
    """W1-32: same prefix, different digest — one fact stored as two nodes.

    ``slug`` folds separators (``_``, spaces, punctuation) to ``-``; ``canonical_text`` does
    not. Building the readable prefix from one and the digest from the other made
    ``"fragrance_free"`` and ``"fragrance free"`` render identically and hash differently,
    which is the worst shape a stable id can have: a human reading the graph sees one id and
    the database holds two nodes. The case/whitespace half converged correctly all along,
    so only a same-prefix/different-id assertion catches it.
    """
    variants = ["fragrance_free", "fragrance free", "Fragrance-Free", "fragrance   FREE"]
    ids = {attribute_value_id(key, value_bool=True) for key in variants}
    assert len(ids) == 1, f"one fact, {len(ids)} ids: {sorted(ids)}"
    only = ids.pop()
    assert only.startswith("av_fragrance-free_")
    # And the digest is taken over the fold the prefix shows.
    assert only == f"av_{slug('fragrance_free')}_{only.rsplit('_', 1)[1]}"
    # Genuinely different keys must still be different nodes.
    assert attribute_value_id("fragrance_free", value_bool=True) != attribute_value_id(
        "fragrance_freedom", value_bool=True
    )


@pytest.mark.docker
@pytest.mark.graph
def test_a_separator_variant_of_a_key_converges_on_one_node(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """The write and read sides fold the key the same way, so the catalog is not partitioned.

    Before the fix this returned ``'fragrance_free' -> ['p-under']`` and
    ``'fragrance free' -> ['p-space']``: two AttributeValue nodes for one fact, and a filter
    that could only ever see the half spelled the way the caller happened to spell it.
    """
    seed_products(
        graph_schema_session,
        [
            {
                "product_id": "p-under",
                "canonical_name": "Underscore Spelling",
                "attributes": [AttributeValue("fragrance_free", value_bool=True)],
            },
            {
                "product_id": "p-space",
                "canonical_name": "Space Spelling",
                "attributes": [AttributeValue("fragrance free", value_bool=True)],
            },
        ],
        source=graph_source,
    )
    attribute_nodes = graph_schema_session.run(
        "MATCH (a:AttributeValue) RETURN count(a) AS c"
    ).single()["c"]
    assert attribute_nodes == 1, "one stated fact must be one node, however it was spelled"

    for spelling in ("fragrance_free", "fragrance free", "Fragrance-Free"):
        found = {
            c.product_id
            for c in candidate_products(
                graph_schema_session,
                attribute_filters=[AttributeFilter(spelling, value_bool=True)],
                limit=10,
            )
        }
        assert found == {"p-under", "p-space"}, f"{spelling!r} saw only {sorted(found)}"


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.parametrize("argv", [[], ["--rebuild-index"]], ids=["plain", "rebuild-index"])
def test_the_reembed_cli_restores_the_whole_schema_however_it_is_invoked(
    graph_schema_session: Any, argv: list[str]
) -> None:
    """W1-33: ``--rebuild-index`` took ``apply_schema`` away instead of adding to it.

    Driven live against a stripped schema, ``main(["--rebuild-index"])`` returned **rc 0
    with zero constraints**, and a second ``CREATE (p:Product {product_id:'cli-1'})`` then
    succeeded — the MERGE-based idempotence of the entire upsert library lost its backstop
    while the command reported success.

    ``main()` had also never executed in this suite at all: sabotage confirmed it, with
    ``raise AssertionError`` as its first statement leaving the suite at 136 passed. So this
    drives the real entrypoint, against the real database, through ``graph_driver()``.

    The shared graph is restored in ``finally`` regardless of outcome — every other worker
    reads this database.
    """
    from ingest.graph.reembed import main as reembed_main

    expected = set(schema_report(graph_schema_session).constraints)
    assert len(expected) == len(ID_PROPERTY), "the fixture must start from a whole schema"
    try:
        for name in sorted(expected):
            graph_schema_session.run(f"DROP CONSTRAINT {name} IF EXISTS").consume()
        assert schema_report(graph_schema_session).constraints == {}, "the strip must land"

        assert reembed_main([*argv, "--provider", "hash"]) == 0
        assert set(schema_report(graph_schema_session).constraints) == expected, (
            "the CLI must leave the constraint set whole, not just the vector index"
        )

        # And the restored constraints are load-bearing, not merely present.
        graph_schema_session.run("CREATE (p:Product {product_id:'cli-1'})").consume()
        with pytest.raises(Exception, match="onstraint|already exists"):
            graph_schema_session.run("CREATE (p:Product {product_id:'cli-1'})").consume()
    finally:
        apply_schema(graph_schema_session)
    live = schema_report(graph_schema_session)
    assert set(live.constraints) == expected
    assert live.vector_dimensions == VECTOR_INDEX_DIMENSIONS


class _LyingProvider(EmbeddingProvider):
    """Declares 1024-d and emits 512-d.

    Not a straw man: a provider's ``dimension`` is a class attribute and its ``embed`` is a
    call into a model, so "declares one width, returns another" is exactly what a wrong
    model revision, a truncated download or a config typo produces.
    """

    name = "liar"
    dimension = EMBEDDING_DIM

    def embed(self, text: str) -> list[float]:
        """Emit half the declared width."""
        return hash_embed(text, dim=EMBEDDING_DIM // 2)


@pytest.mark.docker
@pytest.mark.graph
def test_the_live_vector_width_is_checked_against_the_index_not_the_vector(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """W1-34: the only thing standing between a lying provider and a total blackout.

    ``reembed.py`` passes ``dimensions=index_dimensions`` to ``set_product_embedding``.
    Changing that to ``dimensions=len(vector)`` left the whole suite green — and with this
    provider the sabotaged version stores 512-wide vectors against a 1024-d index, reports
    ``complete=True``, exits 0, and ``candidate_products`` then returns ``[]`` for every
    query in the system. The pre-flight guard at ``reembed.py:190`` cannot help: this
    provider *declares* 1024, so ``resolved.dimension == index_dimensions`` holds.
    """
    from ingest.graph import EmbeddingDimensionMismatch

    session = graph_seeded_catalog["session"]
    provider = _LyingProvider()
    assert provider.dimension == VECTOR_INDEX_DIMENSIONS, "the declaration must pass pre-flight"
    assert len(provider.embed("x")) == 512, "and the emission must not"

    with pytest.raises(EmbeddingDimensionMismatch, match="512"):
        reembed_products(session, provider)

    # The refusal has to be total: not one 512-d vector may have landed.
    widths = {
        len(row["embedding"])
        for row in session.run(
            "MATCH (p:Product) WHERE p.embedding IS NOT NULL RETURN p.embedding AS embedding"
        ).data()
    }
    assert widths == {VECTOR_INDEX_DIMENSIONS}, f"a narrow vector was stored: {widths}"


@pytest.mark.docker
@pytest.mark.graph
def test_the_provenance_audit_covers_every_material_label_and_every_material_edge(
    graph_schema_session: Any,
) -> None:
    """W1-28: the audit could be narrowed invisibly.

    Narrowing ``upsert.py``'s ``labels=`` to ``["Product"]`` and ``types=`` to
    ``["CONTAINS"]`` took a six-violation graph to **zero** violations with the suite
    byte-identical at 136 passed. The audit code was correct; nothing would have noticed if
    it stopped being. So the reported vocabularies are asserted to *equal* the declared ones,
    against a graph that plants one unsourced instance of every member of both.
    """
    session = graph_schema_session
    for index, label in enumerate(sorted(MATERIAL_FACT_LABELS)):
        session.run(
            f"CREATE (n:{label} {{{ID_PROPERTY[label]}: $id}})", id=f"raw-node-{index}"
        ).consume()
    for index, (edge, (start, end)) in enumerate(sorted(MATERIAL_FACT_EDGES.items())):
        session.run(
            f"CREATE (a:{start} {{{ID_PROPERTY[start]}: $a_id}})"
            f"-[:{edge}]->"
            f"(b:{end} {{{ID_PROPERTY[end]}: $b_id}})",
            a_id=f"raw-{edge}-a-{index}",
            b_id=f"raw-{edge}-b-{index}",
        ).consume()

    violations = provenance_violations(session)
    reported_labels = {v.label_or_type for v in violations if v.kind == "unsourced_node"}
    reported_edges = {v.label_or_type for v in violations if v.kind == "unsourced_edge"}
    assert reported_labels == set(MATERIAL_FACT_LABELS), (
        f"the node audit does not cover {set(MATERIAL_FACT_LABELS) - reported_labels}"
    )
    assert reported_edges == set(MATERIAL_FACT_EDGES), (
        f"the edge audit does not cover {set(MATERIAL_FACT_EDGES) - reported_edges}"
    )
    with pytest.raises(ProvenanceRequired):
        assert_provenance_complete(session)


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        pytest.param(
            AttributeFilter("SPF", min_number=30),
            AttributeFilter("spf", min_number=30),
            {"prod-spf-daily"},
            id="key-case",
        ),
        pytest.param(
            AttributeFilter("Skin_Type", value_string="sensitive"),
            AttributeFilter("skin_type", value_string="sensitive"),
            {"prod-serum-c", "prod-spf-daily"},
            id="key-case-and-separator",
        ),
        pytest.param(
            AttributeFilter("volume", equals_number=50, unit="ML"),
            AttributeFilter("volume", equals_number=50, unit="ml"),
            {"prod-cream-night", "prod-spf-daily"},
            id="unit-case",
        ),
    ],
)
def test_the_attribute_filter_canonicalisation_fold_is_load_bearing(
    graph_seeded_catalog: dict[str, Any],
    left: AttributeFilter,
    right: AttributeFilter,
    expected: set[str],
) -> None:
    """W1-29: both halves of ``AttributeFilter.as_parameter``'s fold were untested.

    Sabotaging the key fold turned three working filters into ``[]`` each with the suite
    green, and sabotaging the *unit* fold alone did the same — the value fold was the only
    third covered (``AttributeFilter("skin_type", value_string="sensitive")``). A filter that
    silently returns nothing is the worst failure a retrieval library has, because an empty
    shortlist reads as "no such product" rather than "your query did not canonicalise".

    Each case asserts the folded spelling and the already-canonical spelling return the
    **same non-empty** set, so a fold that stops folding fails rather than merely differing.
    """
    session = graph_seeded_catalog["session"]
    left_ids = {
        c.product_id for c in candidate_products(session, attribute_filters=[left], limit=10)
    }
    right_ids = {
        c.product_id for c in candidate_products(session, attribute_filters=[right], limit=10)
    }
    assert left_ids == expected, f"{left} returned {sorted(left_ids)}"
    assert right_ids == expected, f"{right} returned {sorted(right_ids)}"


@pytest.mark.docker
@pytest.mark.graph
def test_a_measured_zero_score_is_distinguishable_from_no_measurement(
    graph_seeded_catalog: dict[str, Any],
) -> None:
    """W1-35: ``0.0`` was both the structured-path sentinel and a real vector score.

    A query vector antipodal to a product's embedding scores ``0.0`` on the vector path —
    the same value ``_STRUCTURED_HEAD`` emitted as its "no similarity" sentinel — and
    ``cosine_from_score`` maps both to ``-1.0``. (Measured here the antipodal score is
    ``8.5e-05`` rather than a bit-exact ``0.0``: that is the index's float32 quantization,
    the same ~1e-4 residue the exact-match test allows for, and it puts the two values well
    inside any threshold a caller would write.) Nothing inside T-012 thresholds on it, so
    this was never a live defect *here*; it is a contract hazard for the six downstream
    tickets that read ``Candidate.score``, and it is far cheaper to close before they land
    than after.

    ``Candidate.scored`` distinguishes the two, and ``Candidate.cosine`` is ``None`` rather
    than a number when nothing was compared, so a downstream threshold cannot read the
    sentinel as "maximally dissimilar".
    """
    session = graph_seeded_catalog["session"]
    row = read_products(session, limit=1)[0]
    antipodal = [-component for component in HashEmbedding().embed(embedding_text(row))]

    measured = candidate_products(session, embedding=antipodal, status=None, limit=10)
    worst = next(c for c in measured if c.product_id == row["product_id"])
    assert worst.scored is True
    assert worst.score == pytest.approx(0.0, abs=QUANTIZATION_TOLERANCE), (
        f"an antipodal vector must score ~0.0, got {worst.score}"
    )
    assert worst.cosine == pytest.approx(-1.0, abs=QUANTIZATION_TOLERANCE)

    structured = candidate_products(
        session,
        attribute_filters=[AttributeFilter("fragrance_free", value_bool=True)],
        status=None,
        limit=10,
    )
    assert structured, "the structured path must still return rows"
    assert all(c.scored is False for c in structured)
    assert all(c.cosine is None for c in structured), (
        "no similarity was measured, so there is no number to report"
    )

    # The two are now distinguishable even though the raw score field says the same thing.
    assert worst.score == pytest.approx(structured[0].score, abs=QUANTIZATION_TOLERANCE)
    assert cosine_from_score(worst.score) == pytest.approx(-1.0, abs=QUANTIZATION_TOLERANCE)
    assert worst.scored != structured[0].scored
    with pytest.raises(ValueError, match="no similarity was measured"):
        cosine_from_score(structured[0].cosine)
