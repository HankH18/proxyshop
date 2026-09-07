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
    LexicalEmbedding,
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
    products_missing_status,
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

#: The ids the shared graph_seeded_catalog fixture seeds, in one place so a test can
#: say "the whole catalog" without restating them.
GRAPH_SAMPLE_IDS = ("prod-serum-c", "prod-cream-night", "prod-spf-daily", "prod-discontinued")

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

#: ``db.index.vector.queryNodes`` rescales cosine into [0, 1] and the ``vector-2.0`` index
#: ships with ``vector.quantization.enabled: true``, so an exact self-match scores just under
#: 1.0 rather than exactly 1.0.
#:
#: SET FROM MEASUREMENT, and the number moved with D56 because the residue depends on how
#: DENSE the vectors are, not on anything about correctness. Measured on this host over the
#: four sample products, worst case of each:
#:
#:   * ``hash``    — 1024 of 1024 components non-zero — ``1 - cos = 7.0e-05``
#:   * ``lexical`` — 98 to 171 of 1024 non-zero      — ``1 - cos = 1.8e-03``
#:
#: A sparse vector puts the same unit norm into ~15% as many components, so each surviving
#: component is larger and the quantizer's error on it does not average down across as many
#: terms. 5e-3 is ~3x the measured worst case. It does not weaken what the exact-match test
#: discriminates: the next-best candidate for a product's own text scores ~0.53, so anything
#: below ~0.4 separates a self-match from a non-match identically.
QUANTIZATION_TOLERANCE = 5e-3


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


@pytest.mark.parametrize("implementation", [LexicalEmbedding, HashEmbedding, LocalBgeEmbedding])
def test_every_provider_matches_the_port_signature_member_for_member(
    implementation: type,
) -> None:
    """Signature identity for every published member — the frozen assertion, widened.

    The frozen test checks ``HashEmbedding`` only. The other two are checked here because
    D19 makes each a real, selectable provider — and ``LexicalEmbedding`` is now the
    *default*, so a divergent signature there would surface as a ``TypeError`` in every
    re-embed run rather than in any gate.
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


def test_provider_selection_defaults_to_lexical_in_every_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D19 as amended: ``lexical`` is the configured default, not a test-only fallback.

    It was ``hash`` until ``test_embedding_ranking_gate.py`` measured that provider inverting
    28 of 45 relevant/irrelevant pairs with every per-query spread negative. The amendment
    swapped the default and kept everything D19 actually promised: 1024-d, L2-normalised,
    offline, dependency-free, no model weights. ``hash`` stays registered and selectable.
    """
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    assert DEFAULT_PROVIDER == "lexical"
    assert isinstance(get_embedding_provider(), LexicalEmbedding)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "")
    assert isinstance(get_embedding_provider(), LexicalEmbedding)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("lexical", LexicalEmbedding),
        ("hash", HashEmbedding),
        ("local_bge", LocalBgeEmbedding),
        ("  LEXICAL  ", LexicalEmbedding),
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
    assert provider.dimension == 1024, "every provider feeds one 1024-d index (D6)"


def test_no_caller_names_a_concrete_provider_class() -> None:
    """The registry is the only switch: no module outside the embeddings package
    constructs ``HashEmbedding()``, ``LexicalEmbedding()`` or ``LocalBgeEmbedding()`` directly.

    A single ``HashEmbedding()`` at a call site would make that call site immune to
    ``EMBEDDING_PROVIDER`` while every gate stayed green, and the swap would be
    config-only everywhere except the one place that mattered. That is not hypothetical: the
    D19 amendment found exactly this shape at ``apps/exchange/src/retrieval/sources.py:165``
    (``resolved = provider or HashEmbedding()``), which this scan cannot see because it walks
    ``services/ingest/src`` only.
    """
    offenders = []
    for path in SCANNED_SOURCES:
        if path.parent.name == "embeddings":
            continue  # the package that defines them is where they are allowed to be named
        source = path.read_text(encoding="utf-8")
        for pattern in (
            r"\bHashEmbedding\s*\(",
            r"\bLexicalEmbedding\s*\(",
            r"\bLocalBgeEmbedding\s*\(",
        ):
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
    assert "lexical" in str(excinfo.value), "the error should list what is registered"


def test_registered_providers_are_exactly_lexical_hash_and_local_bge() -> None:
    """The registry and the classes' own ``name`` attributes cannot drift apart.

    ``hash`` is listed deliberately. The D19 amendment demoted it from default; deleting it
    would have stranded ``test_embedding_ranking_gate.py``'s measurement of what it does, and
    its byte-identity behaviour is the right instrument for tests that need two unrelated
    texts to land near-orthogonal.
    """
    assert set(PROVIDERS) == {"lexical", "hash", "local_bge"}
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
        results = candidate_products(session, query_text=text, status=None, limit=4)
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

    PINNED TO ``hash`` ON PURPOSE, and re-embedded into that space rather than reusing the
    fixture's. "Unrelated text scores ~0" is a property of a CONTENT-ADDRESSED embedding and
    of nothing else. Under the ``lexical`` default (D56) the floor sits well above zero —
    measured 0.14 to 0.27 against these composed documents — because ``embedding_text``
    surrounds the content with boilerplate ("brand:", "categories:", "ingredients:") and
    because unrelated English shares letter runs, which ``er/similarity.py``'s own docstring
    says outright. Rewriting this test's bound to accommodate that would have quietly
    replaced a real measurement of ``hash`` with a weaker claim about something else. The
    ranking property that IS true of the default is measured where it belongs, in
    ``test_embedding_ranking_gate.py``, on this same served index.
    """
    session = graph_seeded_catalog["session"]
    reembed_products(session, HashEmbedding())  # move this catalogue into the hash space
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
    reembed_products(graph_schema_session, get_embedding_provider())
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
    vector = get_embedding_provider().embed(PROBE_A)
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
    assert report.provider == get_embedding_provider().name
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
    second = reembed_products(session, get_embedding_provider())
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

    ``.env.example`` leaves ``EMBEDDING_PROVIDER`` blank (falling through to ``lexical``, D56)
    and ``make e2e-live`` sets ``local_bge``; both declare ``EMBEDDING_DIM``, as does the
    demoted ``hash``. So the width guard in ``reembed_products``
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
    assert run.provider == get_embedding_provider().name
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
    antipodal = [-component for component in get_embedding_provider().embed(embedding_text(row))]

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


def _plan_operators(profile: Any) -> list[tuple[str, int]]:
    """Flatten a Neo4j profile into ``(operatorType, rows)`` pairs.

    Args:
        profile: the ``ResultSummary.profile`` mapping.

    Returns:
        Every operator in the plan, with the row count it actually produced. The runtime
        suffix Neo4j appends (``NodeByLabelScan@neo4j``) is stripped, so an assertion names
        the operator rather than the runtime that happened to run it.
    """
    operators = [
        (
            str(profile["operatorType"]).split("@", 1)[0],
            int(profile.get("args", {}).get("Rows", 0)),
        )
    ]
    for child in profile.get("children") or ():
        operators.extend(_plan_operators(child))
    return operators


@pytest.mark.docker
@pytest.mark.graph
def test_a_selective_structured_query_seeks_the_index_instead_of_scanning_the_label(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """X2: the structured path aggregated the whole ``:Product`` label before filtering.

    ``test_the_query_predicate_indexes_match_what_the_query_actually_compares`` asserts only
    that the index *names* exist in the database — it never profiles anything, so it was
    green against a plan that never touched ``product_brand`` or ``product_status``.

    Measured here, 30 products, one matching ``brand="Northlight", status="active"``:

    * bare ``MATCH (p:Product)`` head — ``NodeByLabelScan`` over all 30, then three
      ``OptionalExpand(All)``/``OrderedAggregation`` stages **each carrying 30 rows**, and
      only then the ``Filter`` (30→30→30→1);
    * predicates pinned in the MATCH pattern — ``NodeIndexSeek`` producing 1 row, and every
      aggregation carries 1.

    Lifting the predicates as ``WHERE ($brand IS NULL OR p.brand = $brand)`` is *not* enough:
    a disjunction against a parameter cannot be planned as a seek, and that form still left a
    ``NodeByLabelScan`` (measured 116 dbHits against the pattern form's 37). Hence the map.
    """
    from ingest.graph.query import _FILTER_AND_RETURN, _STRUCTURED_HEAD, _structured_head

    session = graph_schema_session
    seed_products(
        session,
        [
            {
                "product_id": f"p-{index:03d}",
                "canonical_name": f"Product {index}",
                "brand": "Northlight" if index == 0 else f"Brand{index}",
                "status": "active" if index % 2 == 0 else "discontinued",
                "attributes": [AttributeValue("spf", value_number=float(index))],
                "ingredients": ["Glycerin"],
            }
            for index in range(30)
        ],
        source=graph_source,
    )
    parameters = {
        "brand": "Northlight",
        "status": "active",
        "category_id": None,
        "ingredients_all": [],
        "ingredients_none": [],
        "attribute_filters": [],
        "limit": 10,
    }

    def profile(head: str) -> list[tuple[str, int]]:
        result = session.run("PROFILE " + head + _FILTER_AND_RETURN, **parameters)
        result.data()
        return _plan_operators(result.consume().profile)

    before = profile(_STRUCTURED_HEAD)
    after = profile(_structured_head(parameters["brand"], parameters["status"]))

    assert any(name == "NodeByLabelScan" for name, _rows in before), (
        "the unfiltered head is expected to scan — it is the 'before' half of this comparison"
    )
    assert max(rows for name, rows in before if name == "OrderedAggregation") == 30, (
        "the defect: every aggregation carried the whole catalog"
    )

    assert not any(name == "NodeByLabelScan" for name, _rows in after), (
        f"a selective structured query still scans the whole :Product label: {after}"
    )
    assert any(name.startswith("NodeIndexSeek") for name, _rows in after), (
        f"product_brand / product_status are still unused: {after}"
    )
    assert all(rows <= 1 for name, rows in after if "Aggregation" in name), (
        f"the aggregations still carry more than the matching rows: {after}"
    )

    # The profiled query is the shipped one: same head, same answer.
    assert [
        c.product_id
        for c in candidate_products(session, brand="Northlight", status="active", limit=10)
    ] == ["p-000"]


@pytest.mark.docker
@pytest.mark.graph
@pytest.mark.parametrize(
    ("brand", "status", "expected"),
    [
        pytest.param(
            None, "active", {"prod-serum-c", "prod-cream-night", "prod-spf-daily"}, id="status-only"
        ),
        pytest.param("Bellmark", None, {"prod-cream-night", "prod-discontinued"}, id="brand-only"),
        pytest.param("Bellmark", "discontinued", {"prod-discontinued"}, id="both"),
        pytest.param(None, None, set(GRAPH_SAMPLE_IDS), id="neither"),
    ],
)
def test_every_pinned_predicate_combination_answers_the_same_question(
    graph_seeded_catalog: dict[str, Any],
    brand: str | None,
    status: str | None,
    expected: set[str],
) -> None:
    """All four heads ``_structured_head`` can build return what the tail's WHERE would.

    The pattern-map form is a *second* place ``brand``/``status`` are applied, so it has to
    agree with the first exactly — including that ``brand=""`` and ``status=None`` mean
    different things ("no brand" versus "any status"), which a truthiness test would confuse.
    """
    session = graph_seeded_catalog["session"]
    found = {
        c.product_id
        for c in candidate_products(
            session,
            brand=brand,
            status=status,
            # `status` alone is not a structured predicate (a status-only query is
            # "everything currently for sale", which is the scan DESIGN forbids), so every
            # case carries a filter that matches the whole sample catalog.
            attribute_filters=[AttributeFilter("fragrance_free")],
            limit=20,
        )
    }
    assert found == expected


@pytest.mark.docker
@pytest.mark.graph
def test_a_product_with_no_status_is_invisible_but_detectable(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """X3: ``p.status = $status`` is null-valued, not false, for a status-less Product.

    Such a product looks perfectly healthy — sourced, embedded, attributed — and is absent
    from every shortlist in the system. ``upsert_product`` always writes a status, so it is
    only reachable from raw Cypher, another library, or an adapter that bypassed this
    package: the same threat model ``provenance_violations`` covers, and the reason this is
    a detector rather than a comment.
    """
    session = graph_schema_session
    upsert_product(session, Product("p-normal", "Normal Product"), source=graph_source)
    session.run(
        "MATCH (src:Source {source_id: $sid}) "
        "CREATE (p:Product {product_id: 'p-statusless', canonical_name: 'No Status'}) "
        "CREATE (p)-[:SUPPORTED_BY]->(src)",
        sid=graph_source.source_id,
    ).consume()
    reembed_products(session, get_embedding_provider())

    assert provenance_violations(session) == [], "it is sourced — the provenance audit is clean"
    assert products_missing_embeddings(session) == [], "and embedded"
    assert products_missing_status(session) == ["p-statusless"], "but invisible, and now said so"

    default_query = {
        c.product_id for c in candidate_products(session, query_text=PROBE_A, limit=10)
    }
    assert default_query == {"p-normal"}, "the status-less product is absent by default"
    unfiltered = {
        c.product_id for c in candidate_products(session, query_text=PROBE_A, status=None, limit=10)
    }
    assert unfiltered == {"p-normal", "p-statusless"}, "status=None must reach it"


@pytest.mark.docker
@pytest.mark.graph
def test_the_surviving_casing_on_a_shared_attribute_does_not_depend_on_ingest_order(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """X4: ``AttributeValue`` is the one shared material-fact node, and it was last-writer-wins.

    Two products asserting ``skin_type="Sensitive"`` and ``skin_type="sensitive"`` correctly
    MERGE onto one node — the identity is the canonicalised content. But the raw reading kept
    for display was overwritten by whoever wrote last, so ``Candidate.attributes`` differed
    between two re-ingests of the same pages. Neither spelling is more correct, so the tie is
    broken by a rule that does not depend on order at all.

    The assertion is the *invariant* (both orders agree), not a hard-coded winner, so it does
    not quietly encode Neo4j's string collation.
    """
    session = graph_schema_session

    def ingest(order: tuple[str, ...]) -> tuple[str, list[str]]:
        session.run("MATCH (n) DETACH DELETE n").consume()
        apply_schema(session)
        for index, spelling in enumerate(order):
            upsert_product(session, Product(f"p-{index}", f"Product {index}"), source=graph_source)
            upsert_attribute(
                session,
                product_id=f"p-{index}",
                attribute=AttributeValue("Skin_Type", value_string=spelling),
                source=graph_source,
            )
        rows = session.run(
            "MATCH (a:AttributeValue) RETURN a.value_string AS value, a.key AS key"
        ).data()
        assert len(rows) == 1, f"one fact, {len(rows)} nodes"
        return rows[0]["value"], [rows[0]["key"]]

    forwards, forwards_key = ingest(("Sensitive", "sensitive"))
    backwards, backwards_key = ingest(("sensitive", "Sensitive"))
    assert forwards == backwards, (
        f"the surviving casing depends on ingest order: {forwards!r} vs {backwards!r}"
    )
    assert forwards in {"Sensitive", "sensitive"}, "and it is still one of the observed readings"
    assert forwards_key == backwards_key, "the raw key must be order-independent too"

    # And the value the retrieval layer hands downstream is the same one.
    reembed_products(session, HashEmbedding())
    candidates = candidate_products(
        session,
        attribute_filters=[AttributeFilter("skin_type", value_string="sensitive")],
        limit=10,
    )
    assert candidates
    assert {a["value_string"] for c in candidates for a in c.attributes} == {forwards}


# =======================================================================================
# 14. Findings from the wave-2 review. Same rule as sections 12 and 13: every test below
#     is a regression guard for a defect that was reproduced live first.
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_partial_reembed_is_never_certified_as_complete(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """W2-03 / T-101: a skipped product silently keeps the PREVIOUS provider's vector.

    Reproduced live before the fix, with exactly this sequence: embed two products with
    ``hash``, blank one product's whole embeddable context, then re-embed with a second
    valid 1024-d provider. The second pass reads two products, writes one, and names the
    other in ``report.skipped`` — ``ReembedReport.complete`` is correctly ``False`` — and
    then stamped the ``EmbeddingRun`` marker ``state='complete'`` anyway. Measured
    consequences of that stamp:

    * ``embedding_run(session).complete is True`` — the marker certified a pass that the
      report it was built from says did not finish;
    * ``_check_vector_path`` therefore raised nothing, because the marker agrees with the
      querying provider and claims to be finished;
    * ``products_missing_embeddings() == []`` — the skipped product *has* an embedding, in
      ``hash`` space, inside an index otherwise full of ``second`` space;
    * ``products_missing_status() == []`` and ``provenance_violations() == []`` too.

    So every detector in the system reported health while one row of the index lived in a
    foreign vector space, and cosine against it is noise that looks like a similarity.

    Two closures are asserted here, because either one alone can be reverted: the marker
    records a terminal state that is *not* ``complete`` and names what it skipped, and the
    stale vector is removed so the *existing* ``products_missing_embeddings`` detector —
    which needs no marker at all — names it.

    T-116 AMENDS THIS TEST. It originally asserted the marker was left ``running`` and that
    the query path then refused. Both are edited below, each with its reasoning at the
    assertion: leaving the marker ``running`` made a *catalog-wide* vector outage out of one
    product losing its name, and the refusal's remediation (re-run the pass) skipped the
    same product again and so never terminated. The requirement the two assertions were
    reaching for — the marker never certifies a partial pass as whole, and nothing ever
    ranks across two vector spaces — is asserted here more precisely than before, not less.
    """
    from ingest.graph import EMBEDDING_RUN_COMPLETE, EMBEDDING_RUN_DEGRADED, embedding_run

    session = graph_schema_session
    upsert_product(session, Product("p-keeps", "Gentle Vitamin C Serum"), source=graph_source)
    upsert_product(session, Product("p-fades", "Soon To Be Nameless"), source=graph_source)
    first = reembed_products(session, HashEmbedding())
    assert first.complete and first.embedded == 2, "the setup pass must itself be complete"
    stale = session.run(
        "MATCH (p:Product {product_id: 'p-fades'}) RETURN p.embedding AS embedding"
    ).single()["embedding"]
    assert stale is not None, "p-fades must carry a hash-space vector going in"

    # The product's whole embeddable context goes away — an upstream page that stopped
    # publishing a name. It is still a Product, still sourced, still in the index.
    session.run("MATCH (p:Product {product_id: 'p-fades'}) SET p.canonical_name = ''").consume()

    second = reembed_products(session, _SecondProvider())
    assert second.products == 2 and second.embedded == 1
    assert second.skipped == ["p-fades"]
    assert second.complete is False, "the report has always been honest; the marker was not"

    run = embedding_run(session)
    assert run is not None
    assert run.provider == "second"
    # T-116, amendment 1 of 2. Was: `assert run.state == EMBEDDING_RUN_RUNNING`, which
    # encoded "a pass that skipped a product is indistinguishable from a pass that died
    # mid-write". That conflation is the defect T-116 names — `_check_vector_path` read it
    # and refused every vector query in the catalog. `running` still means "died mid-write"
    # and is still asserted, unedited, by
    # `test_an_interrupted_reembed_is_visible_rather_than_silent`. The requirement THIS test
    # is named for is that the marker never certifies a partial pass as whole, and that is
    # asserted more tightly here than the old line managed: a distinct terminal state, not
    # `complete`, carrying the finite list of products an operator has to fix.
    assert run.state == EMBEDDING_RUN_DEGRADED, (
        f"a pass that skipped {second.skipped} recorded itself {run.state!r}"
    )
    assert run.state != EMBEDDING_RUN_COMPLETE, "T-101's silent-complete marker must not return"
    assert run.complete is False
    assert run.skipped == ("p-fades",), (
        "the marker must name what it skipped, or the remediation has nothing finite to act on"
    )

    assert products_missing_embeddings(session) == ["p-fades"], (
        "the stale hash-space vector must be gone, so the marker-free detector sees it too"
    )
    assert products_missing_status(session) == [], "the product is otherwise healthy"
    assert provenance_violations(session) == [], "and still fully sourced"

    # T-116, amendment 2 of 2. Was: `with pytest.raises(EmbeddingRunIncomplete,
    # match="running")`, i.e. a catalog-wide vector outage was the contract for one product
    # losing its name. The requirement behind it — never rank across two vector spaces — is
    # delivered by the removal asserted just above: p-fades' hash-space vector is gone, so
    # the index holds only `second`-space vectors and there is no second space to rank
    # across. That is asserted directly here, by the skipped product's absence from a
    # shortlist the rest of the catalog still answers.
    ranked = [
        c.product_id
        for c in candidate_products(
            session, query_text=PROBE_A, provider=_SecondProvider(), limit=10
        )
    ]
    assert ranked == ["p-keeps"], (
        f"one nameless product must degrade itself, not black out the catalog; ranked {ranked}"
    )


@pytest.mark.docker
@pytest.mark.graph
def test_a_repaired_catalog_recovers_from_a_partial_reembed(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """The refusal must be recoverable, or the fix is worse than the defect it closes.

    Leaving the marker ``running`` forever would brick every vector query in the system on
    one nameless product. Restoring the text and re-running the pass has to clear both the
    marker and ``products_missing_embeddings``.
    """
    from ingest.graph import EMBEDDING_RUN_COMPLETE, embedding_run

    session = graph_schema_session
    upsert_product(session, Product("p-ok", "Gentle Vitamin C Serum"), source=graph_source)
    upsert_product(session, Product("p-blank", ""), source=graph_source)
    partial = reembed_products(session, HashEmbedding())
    assert partial.complete is False and partial.skipped == ["p-blank"]
    assert embedding_run(session).complete is False

    session.run(
        "MATCH (p:Product {product_id: 'p-blank'}) SET p.canonical_name = 'Now Named'"
    ).consume()
    repaired = reembed_products(session, HashEmbedding())
    assert repaired.complete is True and repaired.embedded == 2
    run = embedding_run(session)
    assert run is not None and run.state == EMBEDDING_RUN_COMPLETE
    assert products_missing_embeddings(session) == []
    assert [
        c.product_id
        for c in candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=10)
    ], "the catalog is queryable again"


@pytest.mark.docker
@pytest.mark.graph
def test_a_skipped_product_that_never_had_a_vector_is_left_alone(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """Clearing the stale vector must be a no-op where there is nothing to clear.

    ``clear_product_embedding`` reports whether it actually removed anything, and the pass
    over a never-embedded blank product must not invent an error out of a property that was
    never there.
    """
    from ingest.graph import clear_product_embedding

    session = graph_schema_session
    upsert_product(session, Product("p-never", ""), source=graph_source)
    assert clear_product_embedding(session, product_id="p-never") is False
    report = reembed_products(session, HashEmbedding())
    assert report.skipped == ["p-never"]
    assert products_missing_embeddings(session) == ["p-never"]


@pytest.mark.docker
@pytest.mark.graph
def test_clearing_an_embedding_refuses_an_unknown_product(graph_schema_session: Any) -> None:
    """An embedding is derived, never a fact: a typo'd id is reported, not silently ignored."""
    from ingest.graph import clear_product_embedding

    with pytest.raises(ProvenanceRequired, match="p-nonexistent"):
        clear_product_embedding(graph_schema_session, product_id="p-nonexistent")


# =======================================================================================
# 15. Findings from the wave-3 review of the wave-2 fixes. T-116 and T-118(c): the fix for
#     W2-03 satisfied its acceptance criteria and left its objective true by another route,
#     so every test below drives the *combination* of the two closures T-101 shipped rather
#     than either one on its own.
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_one_unembeddable_product_degrades_itself_not_the_catalog_vector_path(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """T-116, acceptance 1 and 3: the SAME-provider skip, which nothing covered before.

    T-101 closed W2-03 twice over: the skipped product's stale vector is removed, *and* the
    ``EmbeddingRun`` marker is left ``running`` so ``_check_vector_path`` refuses. Each
    closure was pinned only in the provider-swap scenario, where the refusal looks
    proportionate because a second vector space really is in play.

    Nothing swaps a provider here. One upstream page stops publishing a name — the single
    most ordinary thing that can happen to a catalog of five products — and the same
    ``hash`` provider re-embeds. Reproduced in this tree before the fix: four products
    perfectly embedded in one space, the fifth correctly reported and stripped of its
    vector, and then ``candidate_products`` raising ``EmbeddingRunIncomplete`` for **every**
    vector query in the system. A one-product problem became a catalog-wide outage.

    Both of T-101's closures still have to hold — the marker must not certify the pass as
    complete, and the stale vector must be gone — and this test pins the second in the
    same-provider case too, so narrowing the removal to "only when the provider changed"
    would fail here.
    """
    from ingest.graph import EMBEDDING_RUN_COMPLETE, EMBEDDING_RUN_DEGRADED, embedding_run

    session = graph_schema_session
    ids = [f"p-cat-{n}" for n in range(5)]
    for product_id in ids:
        upsert_product(
            session,
            Product(product_id, f"Gentle Vitamin C Serum {product_id}"),
            source=graph_source,
        )
    first = reembed_products(session, HashEmbedding())
    assert first.complete and first.embedded == 5, "the setup pass must itself be complete"
    assert embedding_run(session).state == EMBEDDING_RUN_COMPLETE

    # One product's name goes away upstream. Same provider, same space, nothing swapped.
    session.run("MATCH (p:Product {product_id: 'p-cat-3'}) SET p.canonical_name = ''").consume()
    second = reembed_products(session, HashEmbedding())
    assert second.skipped == ["p-cat-3"] and second.embedded == 4
    assert second.complete is False, "the report is honest about not covering the catalog"

    # T-101's second closure, pinned for the same-provider case: the vector is really gone.
    assert products_missing_embeddings(session) == ["p-cat-3"], (
        "the skipped product's vector must be removed whether or not the provider changed"
    )

    # T-101's first closure, in the form that does not take the catalog down with it.
    run = embedding_run(session)
    assert run is not None
    assert run.state == EMBEDDING_RUN_DEGRADED
    assert run.complete is False, "a partial pass is still never certified as whole"
    assert run.skipped == ("p-cat-3",)
    assert run.finished is True, "but it did reach its end, so the index holds ONE space"

    # T-116 acceptance 1. This raised EmbeddingRunIncomplete before the fix.
    ranked = [
        c.product_id
        for c in candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=10)
    ]
    assert ranked, "four healthy products must still be retrievable by vector"
    assert set(ranked) == {"p-cat-0", "p-cat-1", "p-cat-2", "p-cat-4"}
    assert "p-cat-3" not in ranked, "and the degraded product is absent, not silently ranked"


@pytest.mark.docker
@pytest.mark.graph
def test_the_incomplete_reembed_remediation_actually_terminates(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """T-118 (c): the ``EmbeddingRunIncomplete`` message used to name a no-op loop.

    The old text told the operator to "re-run ``python -m ingest.graph.reembed`` **to
    completion** before querying it". In a catalog holding one product with no embeddable
    text, no re-run can reach completion: every pass skips the same product, every pass
    leaves the marker open, and the operator's remediation returns them to the state they
    started in. That is not a slow fix, it is a fixed point.

    So the remediation is followed here literally — the same provider, the whole pass — and
    the refusal has to be gone afterwards even though the unembeddable product is still
    unembeddable. Before the fix this test's second ``candidate_products`` call raised the
    same exception as the first, which is the loop.
    """
    from ingest.graph import EMBEDDING_RUN_DEGRADED, EmbeddingRunIncomplete, embedding_run

    session = graph_schema_session
    for product_id in ("p-alpha", "p-beta", "p-gamma"):
        upsert_product(
            session,
            Product(product_id, f"Gentle Vitamin C Serum {product_id}"),
            source=graph_source,
        )
    assert reembed_products(session, HashEmbedding()).complete

    # p-gamma loses its name permanently, and then a pass dies half-way through re-embedding
    # into a second space. Two spaces in the index AND a product that can never be embedded.
    session.run("MATCH (p:Product {product_id: 'p-gamma'}) SET p.canonical_name = ''").consume()
    with pytest.raises(ConnectionError):
        reembed_products(session, _DiesPartWayThrough(fail_after=1), batch_size=1)

    with pytest.raises(EmbeddingRunIncomplete) as refused:
        candidate_products(session, query_text=PROBE_A, provider=_DiesPartWayThrough(), limit=10)
    message = str(refused.value)

    # The message has to describe an exit that exists. Naming only "completion" describes an
    # exit this catalog cannot reach.
    assert "python -m ingest.graph.reembed" in message, "the remediation must be a command"
    assert EMBEDDING_RUN_DEGRADED in message, (
        "the message must admit the terminal state a catalog with an unembeddable product "
        f"actually reaches, or the remediation it names never terminates: {message}"
    )
    assert "products_missing_embeddings" in message, (
        "and it must point at the finite list of catalog rows to fix, not at another re-embed"
    )

    # Now follow it. Same provider, one whole pass — no catalog edit, p-gamma still nameless.
    repaired = reembed_products(session, _DiesPartWayThrough(fail_after=99))
    assert repaired.skipped == ["p-gamma"] and repaired.complete is False

    run = embedding_run(session)
    assert run is not None and run.state == EMBEDDING_RUN_DEGRADED
    assert run.skipped == ("p-gamma",), "and it names the row an operator has to fix"

    ranked = [
        c.product_id
        for c in candidate_products(
            session, query_text=PROBE_A, provider=_DiesPartWayThrough(fail_after=99), limit=10
        )
    ]
    assert set(ranked) == {"p-alpha", "p-beta"}, (
        f"one pass had to clear the refusal, or the remediation is a loop; ranked {ranked}"
    )
    assert products_missing_embeddings(session) == ["p-gamma"]


@pytest.mark.docker
@pytest.mark.graph
def test_a_degraded_pass_still_refuses_a_query_from_another_provider(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """Narrowing the refusal must not punch a hole in the provider guard.

    ``degraded`` says "one vector space, and it is *this* provider's". A query embedded with
    a different provider is exactly as wrong against a degraded index as against a complete
    one, and reading the state instead of the provider would be the same mistake in the
    opposite direction.
    """
    from ingest.graph import EMBEDDING_RUN_DEGRADED, EmbeddingProviderMismatch, embedding_run

    session = graph_schema_session
    upsert_product(session, Product("p-named", "Gentle Vitamin C Serum"), source=graph_source)
    upsert_product(session, Product("p-nameless", ""), source=graph_source)
    report = reembed_products(session, _SecondProvider())
    assert report.skipped == ["p-nameless"]
    assert embedding_run(session).state == EMBEDDING_RUN_DEGRADED

    with pytest.raises(EmbeddingProviderMismatch, match="second"):
        candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=10)


@pytest.mark.docker
@pytest.mark.graph
def test_a_pass_that_returns_never_leaves_the_marker_open(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """``running`` must mean one thing only: the pass did not come back.

    The whole T-116 fix rests on that reading, so it is asserted as a property of both
    outcomes rather than inferred from the two scenarios above. A returned pass is finished;
    only an exception leaves the marker open.
    """
    from ingest.graph import EMBEDDING_RUN_COMPLETE, EMBEDDING_RUN_DEGRADED, embedding_run

    session = graph_schema_session
    upsert_product(session, Product("p-whole", "Gentle Vitamin C Serum"), source=graph_source)
    whole = reembed_products(session, HashEmbedding())
    assert whole.complete
    run = embedding_run(session)
    assert run.finished and run.state == EMBEDDING_RUN_COMPLETE and run.skipped == ()

    upsert_product(session, Product("p-hollow", ""), source=graph_source)
    partial = reembed_products(session, HashEmbedding())
    assert partial.complete is False
    run = embedding_run(session)
    assert run.finished and run.state == EMBEDDING_RUN_DEGRADED and run.skipped == ("p-hollow",)

    # And the previous pass's skip list does not survive into a later clean one.
    session.run(
        "MATCH (p:Product {product_id: 'p-hollow'}) SET p.canonical_name = 'Named'"
    ).consume()
    assert reembed_products(session, HashEmbedding()).complete
    run = embedding_run(session)
    assert run.state == EMBEDDING_RUN_COMPLETE and run.skipped == (), (
        "a stale skip list would name a product this pass embedded perfectly well"
    )


@pytest.mark.docker
@pytest.mark.graph
def test_a_marker_written_before_the_skip_list_existed_is_still_readable(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """``r.skipped`` is new, and every vector query in the system reads this marker.

    A graph stamped by an older build has no ``skipped`` property at all. Reading it must
    degrade to "no skips recorded" rather than raise out of ``_check_vector_path``, which
    every vector query goes through.
    """
    from ingest.graph import embedding_run

    session = graph_schema_session
    upsert_product(session, Product("p-legacy", "Gentle Vitamin C Serum"), source=graph_source)
    assert reembed_products(session, HashEmbedding()).complete
    session.run("MATCH (r:EmbeddingRun) REMOVE r.skipped").consume()

    run = embedding_run(session)
    assert run is not None and run.skipped == ()
    assert [
        c.product_id
        for c in candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)
    ] == ["p-legacy"]


@pytest.mark.docker
@pytest.mark.graph
def test_the_degraded_state_is_checked_against_the_graph_not_merely_claimed(
    graph_schema_session: Any, graph_source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole T-116 narrowing rests on the removal, so the pass reads it back.

    ``degraded`` tells every vector query "one space in this index, go ahead". T-101's
    marker-open state was a blanket backstop that made that question moot; narrowing it puts
    the entire weight on ``clear_product_embedding`` having really run. If it ever stops
    working, ``degraded`` becomes the silent-complete marker T-101 removed, wearing a
    different name — the exact shape of defect the wave-3 review exists to catch.

    So the removal is broken here on purpose and the pass must notice: with a skipped
    product still carrying a foreign-space vector, the honest state is ``running`` and the
    refusal is the correct outcome. This is the one place T-101's backstop earns its cost,
    and it is kept exactly there and nowhere else.
    """
    from ingest.graph import EMBEDDING_RUN_RUNNING, EmbeddingRunIncomplete, embedding_run

    session = graph_schema_session
    upsert_product(session, Product("p-holds", "Gentle Vitamin C Serum"), source=graph_source)
    upsert_product(session, Product("p-stale", "About To Lose Its Name"), source=graph_source)
    assert reembed_products(session, HashEmbedding()).complete

    session.run("MATCH (p:Product {product_id: 'p-stale'}) SET p.canonical_name = ''").consume()
    # The removal silently stops working — a no-op that even reports "nothing to remove".
    monkeypatch.setattr(
        "ingest.graph.reembed.clear_product_embedding",
        lambda session, *, product_id: False,
    )
    report = reembed_products(session, _SecondProvider())
    assert report.skipped == ["p-stale"]

    # Two spaces really are in the index now, so the marker must not certify one.
    assert products_missing_embeddings(session) == [], "the sabotage left the stale vector"
    run = embedding_run(session)
    assert run is not None
    assert run.state == EMBEDDING_RUN_RUNNING, (
        f"a skipped product still carrying a vector makes 'degraded' a lie; got {run.state!r}"
    )
    with pytest.raises(EmbeddingRunIncomplete):
        candidate_products(session, query_text=PROBE_A, provider=_SecondProvider(), limit=10)


@pytest.mark.docker
@pytest.mark.graph
def test_the_read_back_refusal_does_not_send_the_operator_into_the_removed_loop(
    graph_schema_session: Any, graph_source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-129 (ingest 2): T-118(c) fixed the message on one ``running`` branch, not both.

    ``reembed_products`` records ``running`` twice. The opening stamp, before the first
    batch, is the interrupted-pass case T-118(c) rewrote the remediation for: "re-run the
    pass, it always records an end state". The *closing* stamp records ``running`` too,
    when the read-back finds a product the pass skipped still carrying a vector — and there
    the same message is false twice over. The pass reached its end, so "it started writing
    and never reached its end" is wrong; and re-running it skips the same product for the
    same reason, fails the same read-back and records ``running`` again, so "that
    remediation terminates" is wrong. The no-op loop T-118(c) removed from one branch was
    still being handed to the operator on the other.

    The loop is demonstrated here rather than argued: one full extra pass, followed
    literally, leaves the refusal exactly where it was.
    """
    from ingest.graph import EMBEDDING_RUN_RUNNING, EmbeddingRunIncomplete, embedding_run

    session = graph_schema_session
    upsert_product(session, Product("p-keeps", "Gentle Vitamin C Serum"), source=graph_source)
    upsert_product(session, Product("p-sticky", "About To Lose Its Name"), source=graph_source)
    assert reembed_products(session, HashEmbedding()).complete

    session.run("MATCH (p:Product {product_id: 'p-sticky'}) SET p.canonical_name = ''").consume()
    # The removal stops taking. Not hypothetical: it is the single point of failure the
    # whole `degraded` narrowing rests on, which is why the read-back exists at all.
    monkeypatch.setattr(
        "ingest.graph.reembed.clear_product_embedding",
        lambda session, *, product_id: False,
    )
    assert reembed_products(session, _SecondProvider()).skipped == ["p-sticky"]
    run = embedding_run(session)
    assert run is not None and run.state == EMBEDDING_RUN_RUNNING
    assert run.skipped == ("p-sticky",), "the closing `running` stamp carries the skip list"

    with pytest.raises(EmbeddingRunIncomplete) as refused:
        candidate_products(session, query_text=PROBE_A, provider=_SecondProvider(), limit=10)
    message = str(refused.value)

    # Follow the old remediation literally: same provider, one whole pass, no catalog edit.
    # It is a fixed point — which is exactly what T-118(c) claimed it had removed.
    assert reembed_products(session, _SecondProvider()).skipped == ["p-sticky"]
    assert embedding_run(session).state == EMBEDDING_RUN_RUNNING, (
        "re-running the pass cannot clear this refusal, so the message must not name it"
    )
    with pytest.raises(EmbeddingRunIncomplete):
        candidate_products(session, query_text=PROBE_A, provider=_SecondProvider(), limit=10)

    assert "remediation terminates" not in message, (
        "this branch's re-run is a fixed point; promising it terminates is the loop "
        f"T-118(c) reported closed: {message}"
    )
    assert "p-sticky" in message, (
        "and the operator gets the finite list of vectors to remove — the marker already "
        f"carries it, so withholding it is a choice: {message}"
    )


@pytest.mark.docker
@pytest.mark.graph
def test_the_remediation_command_exits_zero_on_the_state_the_refusal_sends_it_to(
    graph_schema_session: Any, graph_source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-129 (ingest 3): the message promised an end state the CLI reported as a failure.

    ``EmbeddingRunIncomplete`` names ``python -m ingest.graph.reembed --provider X`` and
    tells the operator the pass "always records an end state — ``complete`` ... or
    ``degraded`` ... and both are queryable". Following it on the catalog that message is
    written for — one product with no embeddable text — reached ``degraded``, the catalog
    became queryable, and the command exited **1**. An operator, or a CI step, reading a
    non-zero status as "it failed, run it again" is back in the fixed point T-118(c)
    removed from the prose, re-entered through the exit code.

    ``degraded`` is a success of the pass: one vector space, an end state recorded, and the
    rows it could not embed named. What remains is a catalog edit. So the status is now read
    back out of the marker — the same node every vector query consults — and the one state
    that really is a failure keeps its non-zero.
    """
    from ingest.graph import EMBEDDING_RUN_DEGRADED, EmbeddingRunIncomplete, embedding_run
    from ingest.graph.reembed import main as reembed_main

    session = graph_schema_session
    upsert_product(session, Product("p-cli-named", "Gentle Vitamin C Serum"), source=graph_source)
    upsert_product(session, Product("p-cli-hollow", ""), source=graph_source)

    status = reembed_main(["--provider", "hash"])

    run = embedding_run(session)
    assert run is not None and run.state == EMBEDDING_RUN_DEGRADED
    assert run.skipped == ("p-cli-hollow",)
    assert [
        c.product_id
        for c in candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)
    ] == ["p-cli-named"], "the remediation really did leave the catalog queryable"
    assert status == 0, (
        "the pass reached the terminal state its own refusal message promises and the "
        f"catalog is queryable; exiting {status} tells the operator to run it again"
    )

    # And the message and the command now agree, which is the whole point of the finding.
    session.run("MATCH (r:EmbeddingRun) SET r.state = 'running', r.skipped = []").consume()
    with pytest.raises(EmbeddingRunIncomplete) as refused:
        candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)
    assert "exits 0" in str(refused.value), (
        "the remediation has to say what a successful run looks like, or a non-zero status "
        f"is read as 'run it again': {refused.value}"
    )

    # The one outcome that IS a failure keeps its non-zero: the read-back caught a skipped
    # product still carrying a vector, so the index is not single-space and queries refuse.
    monkeypatch.setattr(
        "ingest.graph.reembed.clear_product_embedding",
        lambda session, *, product_id: False,
    )
    session.run(
        "MATCH (p:Product {product_id: 'p-cli-hollow'}) SET p.embedding = $v",
        v=hash_embed("stale", dim=EMBEDDING_DIM),
    ).consume()
    assert reembed_main(["--provider", "hash"]) == 1, (
        "a pass whose read-back failed leaves the index unqueryable; that is a real failure"
    )


@pytest.mark.docker
@pytest.mark.graph
def test_a_catalog_with_nothing_embeddable_says_so_instead_of_answering_nothing(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """T-129 (ingest 4): the whole-catalog end of the T-116 narrowing, decided and pinned.

    T-116 made one unembeddable product degrade itself rather than the catalog, and it was
    right to. But the narrowing has no floor: when EVERY product is unembeddable the marker
    is still ``degraded``, ``_check_vector_path`` still passes, and ``queryNodes`` over an
    index holding zero vectors hands the caller ``[]``. That is the same value a healthy
    index returns for "your query matched nothing" — the one reading that is certainly
    wrong — and before T-116 this state raised and named its cause.

    The decision, taken deliberately: ``[]`` is refused here, because at
    ``embedded == 0`` the degradation has stopped being per-product. There is no vector path
    left to degrade, so there is nothing the caller can be told by an empty result.
    ``products > 0`` keeps this off a genuinely empty catalog, and the test below proves the
    floor is exactly one product: T-116 is not walked back an inch.
    """
    from ingest.graph import (
        EMBEDDING_RUN_COMPLETE,
        EMBEDDING_RUN_DEGRADED,
        EmbeddingIndexEmpty,
        embedding_run,
    )

    session = graph_schema_session

    # 1. A genuinely empty catalog: `[]` is the whole truth, and must stay unrefused.
    assert reembed_products(session, HashEmbedding()).complete
    assert embedding_run(session).state == EMBEDDING_RUN_COMPLETE
    assert candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5) == []

    # 2. Every product unembeddable. The marker knows exactly why; the caller did not.
    upsert_product(session, Product("p-void-a", ""), source=graph_source)
    upsert_product(session, Product("p-void-b", ""), source=graph_source)
    report = reembed_products(session, HashEmbedding())
    assert report.products == 2 and report.embedded == 0
    run = embedding_run(session)
    assert run is not None and run.state == EMBEDDING_RUN_DEGRADED
    assert products_missing_embeddings(session) == ["p-void-a", "p-void-b"]

    with pytest.raises(EmbeddingIndexEmpty) as refused:
        candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=10)
    message = str(refused.value)
    assert "p-void-a" in message and "p-void-b" in message, (
        f"the marker carries the finite list of rows to fix, so the refusal must too: {message}"
    )

    # 3. The structured path never consults the index, so it is untouched — a caller with a
    #    real predicate still gets its answer out of a catalog with no vectors at all.
    assert [c.product_id for c in candidate_products(session, brand="", limit=10, status=None)] == [
        "p-void-a",
        "p-void-b",
    ]

    # 4. THE FLOOR. One embeddable product reopens the vector path for the whole catalog —
    #    the other row is still unembeddable and still degrades only itself (T-116).
    upsert_product(session, Product("p-void-a", "Gentle Vitamin C Serum"), source=graph_source)
    assert reembed_products(session, HashEmbedding()).skipped == ["p-void-b"]
    assert embedding_run(session).state == EMBEDDING_RUN_DEGRADED
    assert [
        c.product_id
        for c in candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)
    ] == ["p-void-a"], "one embeddable product must be enough; T-116 is not walked back"


@pytest.mark.docker
@pytest.mark.graph
def test_the_read_back_checks_every_id_it_claims_to_not_just_the_first(
    graph_schema_session: Any, graph_source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-129 (ingest 1): the backstop is graded — this widens the grade to the whole list.

    Deleting ``unresolved = _still_embedded(session, report.skipped)`` outright already
    turns ``test_the_degraded_state_is_checked_against_the_graph_not_merely_claimed`` red,
    so the claim that the backstop is ungraded does not survive contact with the tree (the
    RED is quoted in this commit's message). What that test cannot see is a backstop that
    *runs* but checks less than it claims: it skips exactly one product, so reading back
    only the first id is indistinguishable from reading back all of them.

    Here two products are skipped and only the SECOND keeps its vector, which no partial
    read-back can find. ``degraded`` asserts "every product this pass skipped now has no
    vector"; a check over a prefix of that list is a claim about a prefix, and the marker
    does not make a claim about a prefix.
    """
    from ingest.graph import EMBEDDING_RUN_RUNNING, EmbeddingRunIncomplete, embedding_run
    from ingest.graph.upsert import clear_product_embedding as _real_clear

    session = graph_schema_session
    upsert_product(session, Product("p-keeper", "Gentle Vitamin C Serum"), source=graph_source)
    upsert_product(session, Product("p-aa-clears", "Loses Its Name Cleanly"), source=graph_source)
    upsert_product(session, Product("p-zz-sticks", "Loses Its Name Messily"), source=graph_source)
    assert reembed_products(session, HashEmbedding()).complete

    session.run(
        "MATCH (p:Product) WHERE p.product_id IN ['p-aa-clears', 'p-zz-sticks'] "
        "SET p.canonical_name = ''"
    ).consume()

    def _clears_all_but_the_last(session: Any, *, product_id: str) -> bool:
        """Remove every vector except the last skipped product's."""
        if product_id == "p-zz-sticks":
            return False
        return _real_clear(session, product_id=product_id)

    monkeypatch.setattr("ingest.graph.reembed.clear_product_embedding", _clears_all_but_the_last)

    report = reembed_products(session, _SecondProvider())
    assert report.skipped == ["p-aa-clears", "p-zz-sticks"], "sorted: the stale one is second"
    assert products_missing_embeddings(session) == ["p-aa-clears"], (
        "only the first skip really lost its vector; the second is stale in a foreign space"
    )

    run = embedding_run(session)
    assert run is not None and run.state == EMBEDDING_RUN_RUNNING, (
        "a read-back that stops before the end of the skip list certifies a lie about the "
        f"rest of it; got {run.state!r}"
    )
    with pytest.raises(EmbeddingRunIncomplete):
        candidate_products(session, query_text=PROBE_A, provider=_SecondProvider(), limit=10)


@pytest.mark.docker
@pytest.mark.graph
def test_an_all_unembeddable_catalog_gets_one_answer_from_the_cli_and_the_query(
    graph_schema_session: Any,
    graph_source: Source,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T-129 close-out (ingest HIGH 1 + HIGH 2): the two commits disagreed, and the prose lied.

    ``2778e0e`` made ``degraded`` **not** queryable when ``embedded == 0`` —
    ``candidate_products`` raises ``EmbeddingIndexEmpty`` for every vector query. ``22aeb21``
    wrote operator-facing text promising the opposite, in three places at once:

    * ``main()`` exited **0** on an all-unembeddable catalog and printed "The index is
      single-space and queryable without them; fixing them is a catalog edit, not another
      re-embed" — about an index that answers nothing at all;
    * the interrupted-pass refusal promised ``degraded`` "and both are queryable — the
      command exits 0 for either, so a non-zero status ... never means 'run it again'";
    * the stale-vector refusal promised "the next pass then records ``degraded`` and the
      index is queryable".

    All three are read by an operator who is *already* being told a vector query refused, so
    each one sends them to a state they will be refused in again, by a different exception,
    with no warning that it is coming. This drives the real entry point against the real
    database and requires the CLI's words, the CLI's exit status, and the exception a query
    on that identical graph raises to be the same story.
    """
    from ingest.graph import (
        EMBEDDING_RUN_DEGRADED,
        EMBEDDING_RUN_RUNNING,
        EmbeddingIndexEmpty,
        EmbeddingRunIncomplete,
        embedding_run,
        record_embedding_run,
    )
    from ingest.graph.reembed import main as reembed_main

    session = graph_schema_session
    upsert_product(session, Product("p-mute-a", ""), source=graph_source)
    upsert_product(session, Product("p-mute-b", ""), source=graph_source)

    status = reembed_main(["--provider", "hash"])
    printed = capsys.readouterr()

    run = embedding_run(session)
    assert run is not None and run.state == EMBEDDING_RUN_DEGRADED
    assert (run.products, run.embedded) == (2, 0), "the state this whole test is about"

    # 1. What a vector query on this identical graph actually does. Everything below is
    #    graded against THIS, not against a description of it.
    with pytest.raises(EmbeddingIndexEmpty) as refused:
        candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)

    # 2. The CLI must not promise the opposite. "queryable" is the word the removed advisory
    #    turned on, and there is no true sentence containing it for this catalog.
    assert "queryable" not in printed.err, (
        "the pass left an index nothing can query; telling the operator it is queryable is "
        f"the contradiction this test exists for: {printed.err}"
    )
    assert "EmbeddingIndexEmpty" in printed.err, (
        "and it has to name what a query will actually raise, or the operator learns it "
        f"from the traceback instead: {printed.err}"
    )
    assert "state=degraded" in printed.out, "the marker is still reported, unchanged"

    # 3. And the exit status carries the same fact, because a CI step reads only that.
    assert status != 0, (
        "rc 0 is documented as 'a terminal state the vector path really can be queried "
        f"under'; every vector query here refuses, so {status} may not be 0"
    )
    assert status == 3, f"the all-unembeddable outcome has its own status; got {status}"

    # 4. The interrupted-pass refusal — the message the operator reads BEFORE running the
    #    command above — must not promise a queryable end state unconditionally.
    record_embedding_run(
        session, provider="hash", dimension=EMBEDDING_DIM, state=EMBEDDING_RUN_RUNNING
    )
    with pytest.raises(EmbeddingRunIncomplete) as interrupted:
        candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)
    message = str(interrupted.value)
    assert "and both are queryable" not in message, (
        "on this very catalog the promised `degraded` is NOT queryable; the objective "
        f"clause 'the message promises it cannot happen', re-entered verbatim: {message}"
    )
    assert "EmbeddingIndexEmpty" in message and "at least one product embedded" in message, (
        f"the promise has to carry its own exception: {message}"
    )

    # 5. Same for the stale-vector branch, which lands the operator on `degraded` too.
    record_embedding_run(
        session,
        provider="hash",
        dimension=EMBEDDING_DIM,
        state=EMBEDDING_RUN_RUNNING,
        products=2,
        embedded=1,
        skipped=["p-mute-a"],
    )
    with pytest.raises(EmbeddingRunIncomplete) as stale:
        candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)
    stale_message = str(stale.value)
    assert "and the index is queryable." not in stale_message, (
        f"the same unconditional promise, one branch over: {stale_message}"
    )
    assert "EmbeddingIndexEmpty" in stale_message, (
        f"so this branch's `degraded` carries the caveat too: {stale_message}"
    )

    # 6. The floor is still exactly one product: T-116 is not walked back by any of this.
    upsert_product(session, Product("p-mute-a", "Gentle Vitamin C Serum"), source=graph_source)
    assert reembed_main(["--provider", "hash"]) == 0, (
        "one embeddable product makes `degraded` queryable again, and rc 0 says so"
    )
    assert [
        c.product_id
        for c in candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)
    ] == ["p-mute-a"]
    assert "p-void" not in str(refused.value), "sanity: the refusal named this test's rows"
    assert "p-mute-a" in str(refused.value) and "p-mute-b" in str(refused.value)


@pytest.mark.docker
@pytest.mark.graph
def test_an_empty_index_does_not_mask_a_provider_mismatch(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """T-129 close-out (ingest LOW): the two refusals are independent, and order misdirected.

    ``EmbeddingIndexEmpty`` was checked before ``EmbeddingProviderMismatch``. A catalog whose
    every row is unembeddable satisfies both when the reader is on a different provider than
    the marker records — and the empty-index message won. It says "give them embeddable text
    ... and re-run ``--provider <the recorded one>``", which is a complete and correct
    remediation *for the catalog* and says nothing at all about the read path being in
    another vector space. Follow it and you get a fully-populated index and the silently
    re-ranked shortlist ``EmbeddingProviderMismatch`` exists to refuse.

    Whose space the index holds is answerable whether or not anything is in it, so it is
    answered first.
    """
    from ingest.graph import (
        EMBEDDING_RUN_DEGRADED,
        EmbeddingIndexEmpty,
        EmbeddingProviderMismatch,
        embedding_run,
    )

    session = graph_schema_session
    upsert_product(session, Product("p-blank-only", ""), source=graph_source)
    report = reembed_products(session, HashEmbedding())
    assert (report.products, report.embedded) == (1, 0)
    run = embedding_run(session)
    assert run is not None and run.state == EMBEDDING_RUN_DEGRADED and run.provider == "hash"

    with pytest.raises(EmbeddingProviderMismatch) as refused:
        candidate_products(session, query_text=PROBE_A, provider=_SecondProvider(), limit=5)
    message = str(refused.value)
    assert "'hash'" in message and "'second'" in message, (
        f"the mismatch has to name both spaces, which the empty-index message cannot: {message}"
    )

    # The empty-index refusal is not weakened — it is simply second. On the matching
    # provider it is still exactly what comes back.
    with pytest.raises(EmbeddingIndexEmpty):
        candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)


@pytest.mark.docker
@pytest.mark.graph
def test_the_empty_index_refusal_truncates_a_long_skip_list_and_says_how_many_it_dropped(
    graph_schema_session: Any, graph_source: Source
) -> None:
    """T-129 close-out (ingest LOW): the ``and N more`` branch, graded rather than assumed.

    ``EmbeddingIndexEmpty`` names ``run.skipped[:10]`` and appends ``", and N more"`` past
    ten. Every test that reached this refusal used two rows, so the truncation had never once
    executed: deleting both lines, or the ``[:10]``, or the arithmetic, changed nothing any
    test could see. A truncation nothing exercises is a promise about the operator's message
    that the suite does not hold anybody to.

    Twelve unembeddable rows: ten are named, two are not, and the message says how many it
    withheld — so an operator can tell a prefix from the whole set.
    """
    from ingest.graph import EmbeddingIndexEmpty, embedding_run

    session = graph_schema_session
    ids = [f"p-many-{n:02d}" for n in range(1, 13)]
    for product_id in ids:
        upsert_product(session, Product(product_id, ""), source=graph_source)
    report = reembed_products(session, HashEmbedding())
    assert (report.products, report.embedded) == (12, 0)
    run = embedding_run(session)
    assert run is not None and list(run.skipped) == ids, "sorted, so the prefix is knowable"

    with pytest.raises(EmbeddingIndexEmpty) as refused:
        candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)
    message = str(refused.value)

    for named in ids[:10]:
        assert named in message, f"the first ten are named: {message}"
    for withheld in ids[10:]:
        assert withheld not in message, (
            f"past ten the message stops naming rows, or a 5000-row catalog makes a refusal "
            f"nobody reads: {message}"
        )
    assert "and 2 more" in message, (
        f"and it says how many it withheld, so the ten read as a prefix: {message}"
    )
    assert "products_missing_embeddings" in message, (
        f"with a pointer to the unbounded list: {message}"
    )


def test_no_shipped_module_promises_degraded_is_queryable_without_naming_the_refusal() -> None:
    """T-129 close-out (ingest LOW): the prose claim, held to the code that contradicts it.

    ``EMBEDDING_RUN_DEGRADED``'s own docstring said the index is "single-space and honest"
    and the skipped products "simply absent from it" — written when ``degraded`` really was
    always queryable, and left in place by the commit that made it conditional. The same
    sentence had been copied into ``main()``'s advisory and into both
    ``EmbeddingRunIncomplete`` messages.

    A docstring cannot be graded by behaviour, so it is graded here: the two clauses that
    were false are forbidden by their exact text, and every module that names the state has
    to also name the exception that fires when the state is *not* queryable. Restoring any of
    the three original sentences, or dropping the caveat from the schema constant, turns this
    red without any Neo4j at all.
    """
    forbidden = {
        "and both are queryable": (
            "`degraded` is queryable only when the pass embedded at least one product"
        ),
        "The index is single-space and queryable": (
            "single-space is not the same claim as queryable; at embedded == 0 the index is "
            "single-space and holds nothing"
        ),
    }
    for path in SCANNED_SOURCES:
        text = path.read_text(encoding="utf-8")
        for clause, why in forbidden.items():
            assert clause not in text, f"{path.name} claims {clause!r}, but {why}"

    for module in ("reembed.py", "query.py", "schema.py"):
        text = (GRAPH_SRC / module).read_text(encoding="utf-8")
        assert "EMBEDDING_RUN_DEGRADED" in text, "each of these describes the state"
        assert "EmbeddingIndexEmpty" in text, (
            f"{module} tells the operator what `degraded` means; it must also name the "
            f"refusal that state produces when nothing embedded, or it is describing a "
            f"reachable state it says cannot happen"
        )


@pytest.mark.docker
@pytest.mark.graph
def test_the_empty_index_refusal_still_names_a_remediation_when_the_marker_lists_no_rows(
    graph_schema_session: Any,
) -> None:
    """T-129 close-out (ingest LOW, fourth of its kind): the ``or "none recorded"`` fallback.

    ``EmbeddingIndexEmpty`` builds its list as ``", ".join(run.skipped[:10]) or "none
    recorded"``. Every test that reached this refusal got there through ``reembed_products``,
    which always records a non-empty skip list when it embedded nothing — so the ``or`` arm
    had never executed, exactly like the ``and N more`` arm beside it, and deleting it changed
    nothing any test could see.

    It is reachable all the same, and by the one marker most likely to be in a real graph
    during an upgrade: ``embedding_run`` reads ``r.skipped`` as ``row["skipped"] or ()``
    precisely because a marker written before ``skipped`` existed carries none. Such a marker
    with ``products > 0`` and ``embedded == 0`` lands here with an empty list, and without the
    fallback the operator reads "the rows it could not embed: ." — an empty sentence where the
    remediation should be, in the message whose whole job is to say what to do next.
    """
    from ingest.graph import EMBEDDING_RUN_DEGRADED, EmbeddingIndexEmpty, record_embedding_run

    session = graph_schema_session
    record_embedding_run(
        session,
        provider="hash",
        dimension=EMBEDDING_DIM,
        state=EMBEDDING_RUN_DEGRADED,
        products=3,
        embedded=0,
        skipped=(),
    )

    with pytest.raises(EmbeddingIndexEmpty) as refused:
        candidate_products(session, query_text=PROBE_A, provider=HashEmbedding(), limit=5)
    message = str(refused.value)

    assert "none recorded" in message, (
        f"the marker names no rows, so the message has to say so rather than leave the gap "
        f"blank: {message}"
    )
    assert "could not embed: ." not in message, (
        f"the un-fallen-back form, which reads as a truncated sentence: {message}"
    )
    assert "products_missing_embeddings" in message, (
        f"and the pointer to the live list is what makes this refusal actionable when the "
        f"marker itself lists nothing: {message}"
    )
