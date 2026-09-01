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
    constraint_name,
    constraint_statements,
    cosine_from_score,
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

GRAPH_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "graph"

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
    for path in sorted(GRAPH_SRC.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for pattern in (r"\bHashEmbedding\s*\(", r"\bLocalBgeEmbedding\s*\("):
            if re.search(pattern, source):
                offenders.append(f"{path.name}: {pattern}")
    assert not offenders, f"graph modules must go through get_embedding_provider(): {offenders}"


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
    for path in sorted(GRAPH_SRC.glob("*.py")):
        offenders += _free_text_offenders(path.read_text(encoding="utf-8"), path.name)
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
    builtin = {"index_343aff4e", "index_f7700477"}
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
    ("filter_kwargs", "expected"),
    [
        ({"value_bool": False}, {"prod-cream-night"}),
        ({"value_bool": True}, {"prod-serum-c", "prod-spf-daily"}),
        ({"equals_number": 0}, set()),
        ({"min_number": 0}, set()),
    ],
)
def test_a_falsy_filter_component_is_still_a_filter(
    graph_seeded_catalog: dict[str, Any], filter_kwargs: dict[str, Any], expected: set[str]
) -> None:
    """``False``, ``0`` and ``""`` are *values*, not "unset" — the classic falsy-vs-None bug.

    ``AttributeFilter.as_parameter`` distinguishes them with ``is None`` and the Cypher
    guards are ``f.X IS NULL OR …``. Written with a truthiness test on either side,
    ``value_bool=False`` would silently degrade into "no fragrance filter at all" and the
    query would return the fragranced cream to a buyer who asked for fragrance-free —
    a wrong answer that no error message would ever mention.
    """
    session = graph_seeded_catalog["session"]
    key = "fragrance_free" if "value_bool" in filter_kwargs else "spf"
    results = candidate_products(
        session,
        query_text=PROBE_A,
        attribute_filters=[AttributeFilter(key, **filter_kwargs)],
        limit=10,
    )
    returned = {c.product_id for c in results}
    if key == "spf":
        # Only prod-serum-c carries spf, and it carries 0.0 — so both `equals_number=0` and
        # `min_number=0` must select exactly it. A truthiness bug would return all three.
        assert returned == {"prod-serum-c"}
    else:
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
    seed_products(
        graph_schema_session,
        [
            {
                "product_id": f"bulk-{i:03d}",
                "canonical_name": f"Bulk Product {i}",
                "attributes": ([AttributeValue("rare", value_bool=True)] if i == 29 else []),
            }
            for i in range(30)
        ],
        source=graph_source,
    )
    reembed_products(graph_schema_session, HashEmbedding())
    rare = [AttributeFilter("rare", value_bool=True)]

    # The bound: a fetch of one row can return at most one row, whatever matches below it.
    tiny = candidate_products(
        graph_schema_session, query_text=PROBE_A, attribute_filters=rare, limit=1, oversample=1
    )
    assert len(tiny) <= 1

    # The structured path has no top-k, so selectivity alone finds it, every time.
    assert [
        c.product_id
        for c in candidate_products(graph_schema_session, attribute_filters=rare, limit=10)
    ] == ["bulk-029"]

    # And at this catalog size the default oversample already covers the whole catalog.
    assert [
        c.product_id
        for c in candidate_products(
            graph_schema_session, query_text=PROBE_A, attribute_filters=rare, limit=10
        )
    ] == ["bulk-029"]


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
