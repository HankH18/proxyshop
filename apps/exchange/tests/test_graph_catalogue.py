"""THE EVIDENCE HALF OF D55 — the exchange grades claims against its own crawl.

Why this file exists, measured on this tree rather than inferred.

``ranking/serving.py::catalog_of`` defaulted to ``NoCatalogSnapshots`` — a catalogue that
holds a snapshot for nobody — and the only other implementation, ``StaticCatalogSnapshots``,
was built from an operator-supplied deployment document. Meanwhile ``services/ingest`` had
loaded ten real supplement storefronts (3,093 products) into Neo4j and ``candidate_shops`` was
walking that graph on every served auction to decide the ROSTER. **So the two halves were
disconnected**: the graph knew the real products and the verifier graded against hand-authored
fiction, or against nothing at all. Every claim verification on the served route was therefore
either vacuous or a measurement of the operator's own document — which is not the adversarial
check D55's whole asymmetry rests on, because the party being checked and the party writing the
evidence were never the platform's crawl.

:class:`~exchange.retrieval.catalogue.GraphCatalogSnapshots` is the join, and this file pins the
three things it must never get wrong:

1. the snapshot it hands the verifier is the shape ``claim_verification.verify`` actually reads,
   so a crawled fact really does decide a claim (sections 1 and 5);
2. it fails CLOSED — no product named, an unreachable graph, a store the crawl never saw all
   answer ``None``, which is ``unsupported``, which R19 refuses to let satisfy a hard
   constraint. An outage must never turn an unverifiable claim into a verified one (section 2);
3. it is REACHABLE from a served request, wired by the composition root off the environment,
   with no operator-supplied catalogue anywhere (sections 4 and 5). *Built, tested, and reached
   by no served request* is this repository's dominant defect class, and a graph-backed
   catalogue nothing calls would be exactly that defect again.

Sections 1-4 run offline against doubles. Section 5 drives a real Neo4j and a real
``POST /auctions``.

The provenance gates themselves — unsourced store, unsourced carrying relation, unsourced offer
chain, unsourced attribute, and the ``seller_asserted`` class gate — are pinned one layer down,
against the real graph, in ``services/ingest/tests/test_graph_catalogue.py``.
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

import pytest
from claim_verification import verify
from claim_verification.verifier import catalog_keys
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.composition import ensure_configured
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.serving import catalog_of, configure_ranking
from exchange.ranking.verification import (
    NoCatalogSnapshots,
    StaticCatalogSnapshots,
    attest_candidate_claims,
)
from exchange.retrieval.catalogue import (
    CATALOG_SNAPSHOT_PREFIX,
    GraphCatalogSnapshots,
    graph_catalog_from_env,
)
from fastapi.testclient import TestClient

OBSERVED_AT = "2026-01-01T00:00:00Z"
STORE = "shop-north"
PRODUCT = "prod-serum-c"


def _just_crawled() -> Any:
    """A clock twenty minutes after :data:`OBSERVED_AT`, i.e. "the platform just looked".

    ``as_snapshot`` stamps ``read_at`` from this, and ``read_at - captured_at`` is what D59's
    stale-evidence floor measures for the LIVE-state keys (``availability`` and the derived
    ``in_stock``). Fixtures here pin a fixed ``OBSERVED_AT``, so against a wall clock every one
    of them would describe a months-old crawl and those two keys would correctly leave the
    decidable vocabulary — which is a true statement about a stale snapshot and the wrong
    scenario for a test about the JOIN. Pinning the clock is how a fixture says "this crawl is
    current"; it was not something a fixture had to say before the floor existed.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    return lambda: datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_the_process_wide_domain_registry() -> Any:
    """Undo the PROCESS-WIDE write ``configure_accept`` makes, after every test here.

    ``composition.configure_exchange`` binds a deployment's ``registered_domain`` rows through
    ``configure_accept``, which calls ``accept.offer.use_registered_domains`` — a module-level
    global, not app state. Sections 4 and 5 below compose real deployments, so without this
    fixture this file's one-seller registry decides the checkout host of every test that runs
    after it in the same interpreter. MEASURED: with it removed, ten tests in
    ``test_orphaned_code.py`` fail with an off-domain ``policy_event`` where a ``code_created``
    should be, and the merchant double is never called at all — a failure that looks like a
    defect in the accept path and is this file leaking.
    """
    from exchange.accept.offer import platform_registered_domains, use_registered_domains

    previous = platform_registered_domains()
    try:
        yield
    finally:
        use_registered_domains(previous)


# =====================================================================================
# Doubles
# =====================================================================================
class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def data(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


class _Session:
    """A ``neo4j.Session`` stand-in that answers with canned rows, or raises.

    Canned rows rather than a mock, because the shape of a row IS the contract between
    ``catalogue_entry``'s Cypher and its Python half, and a mock would let the two drift while
    every test here stayed green. The Cypher itself is driven against a real graph in
    ``services/ingest/tests/test_graph_catalogue.py`` and in section 5 below.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None, raises: Exception | None = None):
        self.rows = rows or []
        self.raises = raises
        self.queries: list[dict[str, Any]] = []
        self.closed = False

    def run(self, query: str, **params: Any) -> _Rows:
        self.queries.append(dict(params))
        if self.raises is not None:
            raise self.raises
        return _Rows(self.rows)

    def close(self) -> None:
        self.closed = True


class _Factory:
    """A sessions factory shaped like ``driver.session`` — a callable yielding a manager."""

    def __init__(self, session: _Session | None = None, raises: Exception | None = None):
        self.session = session
        self.raises = raises
        self.opened = 0

    def __call__(self) -> Any:
        self.opened += 1
        if self.raises is not None:
            raise self.raises
        return _Held(self.session)


class _Held:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __enter__(self) -> Any:
        return self.inner

    def __exit__(self, *exc: Any) -> bool:
        return False


def _attribute(
    key: str,
    *,
    value_string: Any = None,
    value_number: Any = None,
    value_bool: Any = None,
    unit: Any = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "canonical_key": key.replace("_", "-"),
        "value_string": value_string,
        "value_number": value_number,
        "value_bool": value_bool,
        "unit": unit,
        "observed": [OBSERVED_AT],
        "source_ids": [f"src-{key}"],
    }


def _row(*, attributes: list[dict[str, Any]] | None = None, offers: Any = None) -> dict[str, Any]:
    """One ``_CATALOGUE_ENTRY`` row, exactly as the Cypher returns it."""
    if offers is None:
        offers = [
            {
                "offer_id": "off-north-serum",
                "variant_id": "var-serum-30",
                "price": 28.0,
                "currency": "USD",
                "availability": "in_stock",
                "observed_at": OBSERVED_AT,
                "source_ids": ["src-offer"],
            }
        ]
    return {
        "store_id": STORE,
        "domain": "north.example",
        "product_id": PRODUCT,
        "canonical_name": "Vitamin C Serum",
        "brand": "Northlight",
        "status": "active",
        "store_sources": ["src-store"],
        "store_observed": [OBSERVED_AT],
        "product_sources": ["src-product"],
        "product_observed": [OBSERVED_AT],
        "via": ["MAKES_OFFER", "SELLS"],
        "offers": offers,
        "attributes": attributes
        if attributes is not None
        else [_attribute("capacity_ml", value_number=30.0, unit="ml")],
    }


def _claim(key: str, value: Any) -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "provenance": {
            "source": "owner_statement",
            "ref": f"ref:{key}",
            "observed_at": OBSERVED_AT,
            "authority_rank": 1,
        },
    }


def _verdicts(snapshot: Any, *claims: dict[str, Any], product_ref: str = PRODUCT) -> list[str]:
    """The verifier's own statuses for these claims against this snapshot.

    ``product_ref`` is the product the AUCTION names, exactly as ``attest_candidates`` supplies
    it — a pitch naming a product the snapshot does not hold resolves against nothing and
    answers ``unsupported`` for every claim, which is a real behaviour and a trap for a test
    that lets the two drift.
    """
    result = verify(
        {
            "pitch_id": "pitch-1",
            "store_id": STORE,
            "product_ref": product_ref,
            "claims": list(claims),
        },
        snapshot,
        "verification/1.0.0",
    )
    return [str(row["status"]) for row in result["claims"]]


# =====================================================================================
# 1. The snapshot is the document the verifier reads
# =====================================================================================
def test_a_crawled_entry_becomes_a_snapshot_the_verifier_can_decide_a_claim_on() -> None:
    """The join, end to end offline: a graph row decides a store's claim.

    Without this the whole feature is a shape nobody consumes. The assertion that matters is
    the last pair — the SAME key, one claim agreeing with the crawl and one contradicting it,
    coming back with different statuses — because that is the adversarial check D55 says a
    sponsored shop's message must meet.
    """
    session = _Session([_row()])
    catalog = GraphCatalogSnapshots(_Factory(session), clock=_just_crawled())

    snapshot = catalog.snapshot_for(STORE, PRODUCT)
    assert snapshot is not None
    assert snapshot["snapshot_id"] == f"{CATALOG_SNAPSHOT_PREFIX}:{STORE}:{PRODUCT}"
    assert snapshot["captured_at"] == OBSERVED_AT
    assert snapshot["read_at"] == "2026-01-01T00:20:00Z", (
        "the snapshot must say WHEN THIS EXCHANGE READ IT; `captured_at` is the latest "
        "observation behind the entry, so measuring a reading's age against it is measuring "
        "the document against itself and answers zero at any crawl age"
    )
    assert snapshot["evidence_refs"] == ["src-product", "src-store"]
    assert "freshness_window_days" not in snapshot, (
        "no window is published unless the operator states one; inventing one silently turns "
        "honest crawled evidence into `unsupported`"
    )

    (product,) = snapshot["products"]
    assert product["product_ref"] == PRODUCT
    assert product["canonical_name"] == "Vitamin C Serum"
    assert product["brand"] == "Northlight"
    assert product["attributes"] == {
        "capacity_ml": {"value": 30.0, "unit": "ml", "observed_at": OBSERVED_AT}
    }
    assert product["offer"] == {
        "price": 28.0,
        "currency": "USD",
        "availability": "in_stock",
        "observed_at": OBSERVED_AT,
    }

    # The vocabulary this snapshot can decide anything on at all — the verifier's own answer,
    # not this test's opinion of it.
    assert catalog_keys(snapshot, PRODUCT) >= {
        "capacity_ml",
        "price",
        "currency",
        "availability",
        "canonical_name",
        "brand",
        "status",
    }

    assert _verdicts(snapshot, _claim("price", 28.0)) == ["verified"]
    assert _verdicts(snapshot, _claim("price", 14.0)) == ["contradicted"]
    assert _verdicts(snapshot, _claim("capacity_ml", 30.0)) == ["verified"]
    assert _verdicts(snapshot, _claim("capacity_ml", 50.0)) == ["contradicted"]


def test_a_product_the_platform_never_priced_states_no_price_and_is_never_free() -> None:
    """Third provenance tier: the row survives, the price does not.

    ``lowest_price is None`` means "never checked" and must never be read as "free". The
    silent version of this bug states ``price: 0.0``, which is the cheapest number there is:
    it verifies any claim of zero, contradicts every honest price, and wins every comparison a
    shopper makes.
    """
    catalog = GraphCatalogSnapshots(_Factory(_Session([_row(offers=[])])))
    snapshot = catalog.snapshot_for(STORE, PRODUCT)

    assert snapshot is not None
    (product,) = snapshot["products"]
    assert "offer" not in product, product
    assert "price" not in json.dumps(product), product
    assert "price" not in catalog_keys(snapshot, PRODUCT)

    # And the verifier agrees: nothing to compare against, so nothing is confirmed or refuted.
    assert _verdicts(snapshot, _claim("price", 0.0), _claim("price", 28.0)) == [
        "unsupported",
        "unsupported",
    ]

    # CONTROL: the identical row WITH a provenanced offer decides both of them.
    priced = GraphCatalogSnapshots(_Factory(_Session([_row()]))).snapshot_for(STORE, PRODUCT)
    assert _verdicts(priced, _claim("price", 0.0), _claim("price", 28.0)) == [
        "contradicted",
        "verified",
    ]


def test_a_key_the_graph_holds_twice_becomes_a_list_rather_than_the_last_row_read() -> None:
    """A multi-valued attribute must not charge an honest store for the reading that lost.

    The graph stores a multi-valued attribute as several ``AttributeValue`` nodes sharing one
    key (the golden set's own example is ``voltage`` at 120 V and 230 V). A snapshot keyed on a
    plain dict keeps whichever row sorted last, so a store claiming the OTHER true reading is
    attested ``contradicted`` — with the published penalty — for a fact its shelf genuinely
    carries. Both readings must decide the same way.
    """
    session = _Session(
        [
            _row(
                attributes=[
                    _attribute("voltage", value_number=120.0, unit="V"),
                    _attribute("voltage", value_number=230.0, unit="V"),
                ]
            )
        ]
    )
    snapshot = GraphCatalogSnapshots(_Factory(session)).snapshot_for(STORE, PRODUCT)
    assert snapshot is not None
    (product,) = snapshot["products"]
    assert product["attributes"]["voltage"]["value"] == [120.0, 230.0]

    assert _verdicts(snapshot, _claim("voltage", 120.0)) == ["verified"]
    assert _verdicts(snapshot, _claim("voltage", 230.0)) == ["verified"]
    assert _verdicts(snapshot, _claim("voltage", 400.0)) == ["contradicted"]


def test_readings_that_disagree_about_their_unit_keep_it_on_each_member() -> None:
    """One ``unit`` per row and two readings that disagree is a fact with no honest single
    answer, so the unit travels with the member instead of being dropped or guessed."""
    session = _Session(
        [
            _row(
                attributes=[
                    _attribute("net_weight", value_number=500.0, unit="g"),
                    _attribute("net_weight", value_number=2.0, unit="kg"),
                ]
            )
        ]
    )
    snapshot = GraphCatalogSnapshots(_Factory(session)).snapshot_for(STORE, PRODUCT)
    assert snapshot is not None
    (product,) = snapshot["products"]
    # The member ORDER is `catalogue_entry`'s own `(key, str(value))` sort, not the order the
    # graph happened to return the rows in — a snapshot whose contents depend on Neo4j's
    # storage order makes `verification_key` unstable and every verdict unreproducible.
    assert product["attributes"]["net_weight"] == {
        "value": ["2.0 kg", "500.0 g"],
        "unit": None,
        "observed_at": OBSERVED_AT,
    }
    assert _verdicts(snapshot, _claim("net_weight", "0.5 kg")) == ["verified"]
    assert _verdicts(snapshot, _claim("net_weight", "2000 g")) == ["verified"]


# =====================================================================================
# 2. Every way of failing is a DENIAL
# =====================================================================================
def test_naming_no_product_is_a_refusal_and_never_reads_the_whole_shelf() -> None:
    """A crawl holds hundreds of products per store; a claim is graded against ONE.

    Handing back the whole shelf would be an unbounded read on the request path whose only
    effect is that ``_resolve_product`` answers ``ambiguous`` anyway. The refusal must not even
    open a session.
    """
    factory = _Factory(_Session([_row()]))
    catalog = GraphCatalogSnapshots(factory)

    assert catalog.snapshot_for(STORE, None) is None
    assert catalog.snapshot_for(STORE, "") is None
    assert catalog.snapshot_for("", PRODUCT) is None
    assert factory.opened == 0, "a refusal that still queries the graph is not a refusal"

    # CONTROL: naming the product does open one, and does answer.
    assert catalog.snapshot_for(STORE, PRODUCT) is not None
    assert factory.opened == 1


@pytest.mark.parametrize(
    ("name", "catalog", "is_a_fault"),
    [
        (
            "the driver will not build",
            GraphCatalogSnapshots(_Factory(raises=RuntimeError("no route to host"))),
            True,
        ),
        (
            "the query fails",
            GraphCatalogSnapshots(_Factory(_Session(raises=RuntimeError("service unavailable")))),
            True,
        ),
        ("the crawl never saw this pair", GraphCatalogSnapshots(_Factory(_Session([]))), False),
    ],
)
def test_an_unreachable_catalogue_holds_no_snapshot_and_verifies_nothing(
    name: str, catalog: GraphCatalogSnapshots, is_a_fault: bool, caplog: Any
) -> None:
    """The one direction this seam may not fail in, and the one it must not fail in SILENTLY.

    An outage that turned an unverifiable claim into a verified one would be worse than having
    no catalogue at all: R19 lets only a ``verified`` claim satisfy a hard constraint, so a
    permissive failure hands every must-have to whoever is offline-est. The assertion is not
    just ``is None`` — it is that a claim attested through the real attestation path against
    this source is never ``verified``.

    The second half is the ``is_a_fault`` axis, and it is here because the denial is
    indistinguishable from the healthy answer: an unreachable graph and a pair the crawl never
    saw both reach the ranker as ``unsupported``, and only one of them is an operator's
    problem. A fault must say so; a genuine miss must NOT, or the log is noise nobody reads.
    """
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="exchange.retrieval.catalogue"):
        assert catalog.snapshot_for(STORE, PRODUCT) is None, name
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert bool(warned) is is_a_fault, (name, [r.getMessage() for r in warned])
    if warned:
        assert STORE in warned[0].getMessage() and PRODUCT in warned[0].getMessage()

    attested = attest_candidate_claims(
        [_claim("price", 28.0)], store_id=STORE, product_ref=PRODUCT, catalog=catalog
    )
    status = attested[0]["exchange_verification"]["status"]
    assert status != "verified", (name, attested)
    assert status in {"unsupported", "ambiguous"}, (name, attested)


def test_a_claim_graded_against_the_crawl_is_attested_through_the_real_path() -> None:
    """The seam actually used by ``rank_auction``, not just ``snapshot_for`` in isolation.

    ``attest_candidate_claims`` is what mints the verdict a bidder cannot forge, and it reaches
    this source through ``verification.snapshot_for``'s three accepted spellings. If the two
    ever stop fitting, every graph-backed verdict silently becomes ``unsupported``.
    """
    catalog = GraphCatalogSnapshots(_Factory(_Session([_row()])))
    honest, liar = attest_candidate_claims(
        [_claim("price", 28.0), _claim("price", 14.0)],
        store_id=STORE,
        product_ref=PRODUCT,
        catalog=catalog,
    )
    assert honest["exchange_verification"]["status"] == "verified"
    assert liar["exchange_verification"]["status"] == "contradicted"
    assert honest["exchange_verification"]["catalog_snapshot"] == (
        f"{CATALOG_SNAPSHOT_PREFIX}:{STORE}:{PRODUCT}"
    ), "a verdict nobody can trace back to the crawl that decided it is not auditable"


# =====================================================================================
# 3. The environment gate
# =====================================================================================
def test_a_single_class_name_is_refused_rather_than_read_as_seven_letters() -> None:
    """``tuple("scraped")`` is seven one-character classes and matches nothing.

    The silent version of this mistake is a catalogue that answers ``None`` for every pair in
    the graph while looking correctly configured — every claim in every auction ``unsupported``
    — so an operator "widening the policy" with one string gets a hard error at wiring time
    rather than a catalogue that quietly verifies nobody.
    """
    with pytest.raises(TypeError, match="sequence of class names"):
        GraphCatalogSnapshots(_Factory(_Session([_row()])), source_classes="scraped")

    # CONTROL: the sequence spelling of the same intent is accepted and does read.
    widened = GraphCatalogSnapshots(
        _Factory(_Session([_row()])), source_classes=("scraped", "pixel_feed")
    )
    assert widened.snapshot_for(STORE, PRODUCT) is not None


@pytest.mark.parametrize(
    ("env", "wired"),
    [
        ({}, False),
        ({"EXCHANGE_SHOP_ROSTER": "graph"}, True),
        ({"EXCHANGE_RANKING_CATALOG": "graph"}, True),
        ({"EXCHANGE_RANKING_CATALOG": "GRAPH"}, True),
        ({"EXCHANGE_SHOP_ROSTER": "graph", "EXCHANGE_RANKING_CATALOG": "none"}, False),
        ({"EXCHANGE_SHOP_ROSTER": "none"}, False),
        ({"EXCHANGE_RANKING_CATALOG": ""}, False),
    ],
)
def test_the_catalogue_switch_defaults_to_the_roster_s_and_can_be_stated_alone(
    env: dict[str, str], wired: bool
) -> None:
    """Two different powers, each stateable alone, with the working default.

    The roster decides who gets ASKED; the catalogue decides whose claims get BELIEVED. An
    exchange that reads the graph to find shops and holds no catalogue verifies nothing about
    anybody it just found — which is the vacuum this feature closes — so the catalogue defaults
    on with the roster. ``EXCHANGE_RANKING_CATALOG=none`` is the explicit way back off, and it
    must not be overridable by the roster's setting.
    """
    built = graph_catalog_from_env(env)
    assert (built is not None) is wired, (env, built)
    if built is not None:
        assert isinstance(built, GraphCatalogSnapshots)


def test_building_the_source_from_the_environment_connects_to_nothing() -> None:
    """The composition root runs on the first served request. A driver built eagerly against a
    graph that is down would take the whole exchange with it — including the paths that need no
    graph at all — so nothing may connect until a lookup asks it to."""
    built = graph_catalog_from_env(
        {"EXCHANGE_RANKING_CATALOG": "graph", "NEO4J_URI": "bolt://127.0.0.1:1"}
    )
    assert built is not None
    # No connection attempt has been made, and the one this would make cannot succeed; the
    # answer is still a denial rather than an exception escaping into the ranker.
    assert built.snapshot_for(STORE, PRODUCT) is None


# =====================================================================================
# 4. The composition root reaches it
# =====================================================================================
def _deployment(*, catalog: dict[str, Any] | None = None) -> str:
    document: dict[str, Any] = {
        "sellers": [
            {"store_id": STORE, "eligibility": "eligible", "registered_domain": "north.example"}
        ],
        "trust_snapshot": {STORE: {"blacklisted": False, "score": 0.6}},
    }
    if catalog is not None:
        document["catalog"] = catalog
    return json.dumps(document)


def test_a_deployment_that_states_no_catalog_is_wired_to_the_crawl() -> None:
    """The binding this whole ticket exists for.

    Before it, an exchange whose roster came out of a crawl of 3,093 real products verified
    every claim against ``NoCatalogSnapshots`` — a snapshot for nobody — so every claim from
    every shop it had just found came back ``unsupported``. The document below states no
    ``catalog`` key at all, which is what a real deployment looks like.
    """
    app = create_app()
    bound = ensure_configured(
        app,
        {"EXCHANGE_DEPLOYMENT_JSON": _deployment(), "EXCHANGE_SHOP_ROSTER": "graph"},
    )
    assert "ranking_catalog" in bound, bound
    assert isinstance(catalog_of(app), GraphCatalogSnapshots)


def test_an_operator_who_states_a_catalog_document_still_wins() -> None:
    """A document that names snapshots has made a statement about what this exchange holds; a
    ``neo4j`` wheel being present on the box must not overrule it."""
    document = _deployment(
        catalog={
            STORE: {
                "snapshot_id": "snap-operator",
                "products": [{"product_ref": PRODUCT, "attributes": {}}],
            }
        }
    )
    app = create_app()
    ensure_configured(app, {"EXCHANGE_DEPLOYMENT_JSON": document, "EXCHANGE_SHOP_ROSTER": "graph"})
    catalog = catalog_of(app)
    assert isinstance(catalog, StaticCatalogSnapshots)
    assert not isinstance(catalog, GraphCatalogSnapshots)


def test_an_exchange_that_asked_for_neither_holds_a_snapshot_for_nobody() -> None:
    """The unchanged posture, kept as a control: no env, no document catalog, no graph read —
    and the pre-existing fail-closed default, not a permissive one."""
    app = create_app()
    bound = ensure_configured(app, {"EXCHANGE_DEPLOYMENT_JSON": _deployment()})
    assert "ranking_catalog" not in bound, bound
    assert isinstance(catalog_of(app), NoCatalogSnapshots)


# =====================================================================================
# 5. THE SERVED PROOF — a real Neo4j, a real POST /auctions
# =====================================================================================
GRAPH_STORES = ("shop-honest", "shop-liar")
GRAPH_PRODUCT = "prod-milk-thistle"
GRAPH_PRICE = 24.5
GRAPH_INTENT = {
    "intent_id": "intent-graph-catalogue-1",
    "cluster_id": "cluster-1",
    "query": "milk thistle extract for liver support",
    "hard_constraints": [],
    "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
}


def _seed(session: Any) -> None:
    """Two shops, ONE product, ONE price — so the only thing that can differ is the claim.

    Both stores sell the same product at the same observed price, so ``intent_match``,
    ``price_value``, ``trust`` and ``delivery_fit`` are identical for the pair by construction.
    Anything that moves the ranking apart is the claim verdict and nothing else.
    """
    from ingest.embeddings import get_embedding_provider
    from ingest.graph import (
        AttributeValue,
        Offer,
        Source,
        Store,
        Variant,
        apply_schema,
        link_sells,
        reembed_products,
        seed_products,
        upsert_offer,
        upsert_store,
        upsert_variant,
    )

    apply_schema(session)
    source = Source(
        source_id="src-graph-catalogue",
        url="https://crawl.example/products.json",
        content_hash="sha256:" + "c" * 64,
        observed_at=OBSERVED_AT,
        extractor_version="fixture@1",
        confidence=0.9,
        source_class="scraped",
    )
    seed_products(
        session,
        [
            {
                "product_id": GRAPH_PRODUCT,
                "canonical_name": "Milk Thistle Extract",
                "brand": "Northlight",
                "category": "Liver Support",
                "attributes": [AttributeValue("silymarin_mg", value_number=175.0, unit="mg")],
                "ingredients": [],
            }
        ],
        source=source,
    )
    reembed_products(session, get_embedding_provider())

    for index, store_id in enumerate(GRAPH_STORES):
        upsert_store(
            session, Store(store_id, f"{store_id}.example", f"{store_id} Ltd", 1), source=source
        )
        link_sells(session, store_id=store_id, product_id=GRAPH_PRODUCT, source=source)
        upsert_variant(
            session,
            Variant(f"var-{store_id}", f"SKU-{index}", "60 caps"),
            product_id=GRAPH_PRODUCT,
            source=source,
        )
        upsert_offer(
            session,
            Offer(f"off-{store_id}", GRAPH_PRICE, "USD", "in_stock", OBSERVED_AT),
            store_id=store_id,
            variant_id=f"var-{store_id}",
            source=source,
        )


def _graph_app(session: Any, monkeypatch: Any, *, liar: str) -> Any:
    """A served exchange whose CATALOGUE comes from the composition root, off the environment.

    Only the solicitor, the eligibility double and the shop roster are wired by hand — the
    roster so the vector query runs on the session this test already holds the D37 lock for.
    ``ranking_catalog`` is bound by ``ensure_configured`` from ``EXCHANGE_RANKING_CATALOG``, and
    the deployment document below states **no** ``catalog`` key, so nothing an operator typed
    can decide any claim in this test.
    """
    from exchange.retrieval.roster import GraphShopRoster

    monkeypatch.setenv(
        "EXCHANGE_DEPLOYMENT_JSON",
        json.dumps(
            {
                "sellers": [
                    {
                        "store_id": store_id,
                        "eligibility": "eligible",
                        "registered_domain": f"{store_id}.example",
                    }
                    for store_id in GRAPH_STORES
                ],
                "trust_snapshot": {
                    store_id: {"blacklisted": False, "score": 0.6} for store_id in GRAPH_STORES
                },
            }
        ),
    )
    monkeypatch.setenv("EXCHANGE_RANKING_CATALOG", "graph")
    monkeypatch.delenv("EXCHANGE_SHOP_ROSTER", raising=False)

    def bid(store_id: str) -> dict[str, Any]:
        claimed = round(GRAPH_PRICE / 2.0, 2) if store_id == liar else GRAPH_PRICE
        return {
            "auction_id": None,
            "store_id": store_id,
            "offer": {
                "product_ref": GRAPH_PRODUCT,
                "unit_price": GRAPH_PRICE,
                "total_price": GRAPH_PRICE,
                "currency": "USD",
                "checkout_url": f"https://{store_id}.example/cart/1:1",
                "expires_at": time.time() + 3600.0,
            },
            "claims": [_claim("price", claimed)],
            "agent_version": "1.0.0",
            "schema_version": "1.0.0",
        }

    answers = {store_id: bid(store_id) for store_id in GRAPH_STORES}

    def solicit(store: Any) -> Any:
        store_id = str(store["store_id"])
        reply = answers.get(store_id)
        if reply is None:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": dict(reply)}

    app = create_app()
    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({s: ELIGIBLE for s in GRAPH_STORES}),
        shop_roster=GraphShopRoster(lambda: _Held(session)),
    )
    configure_ranking(
        app,
        registered_domains=StaticRegisteredDomains({s: f"{s}.example" for s in GRAPH_STORES}),
    )
    return app


def _ranked(app: Any) -> dict[str, dict[str, Any]]:
    posted = TestClient(app).post(
        "/auctions", json={"intent": dict(GRAPH_INTENT), "bid_timeout_seconds": 2.0}
    )
    assert posted.status_code == 201, posted.text
    body = posted.json()
    assert body["roster_source"]["source"] == "neo4j", body["roster_source"]
    assert isinstance(catalog_of(app), GraphCatalogSnapshots), type(catalog_of(app))
    # Every entry is a real bid: a fallback carries no asserted claims at all, so a test whose
    # bids quietly degraded would compare two stores that said nothing and still pass.
    assert {entry["fallback_reason"] for entry in body["entries"]} == {None}, body["entries"]
    assert {entry["store_id"] for entry in body["entries"]} == set(GRAPH_STORES), body["entries"]
    return {row["store_id"]: row for row in body["ranked"]}


@pytest.mark.docker
@pytest.mark.graph
def test_the_served_auction_grades_a_claim_against_its_own_crawl(
    neo4j_session: Any, monkeypatch: Any
) -> None:
    """THE SERVED PROOF, and the regression test for it.

    Nothing on the evidence path is stubbed: a real ``neo4j.Session``, the real crawl-shaped
    ``(:Store)-[:SELLS]->(:Product)`` and ``MAKES_OFFER -> Offer`` walk, the real
    ``claim_verification`` comparators, the real ranker, and ``POST /auctions`` with an empty
    roster. **No operator-supplied catalogue exists anywhere in this test** — the deployment
    document states no ``catalog`` key — so the only thing either claim can be decided against
    is what the platform crawled.

    Two shops sell the SAME product at the SAME observed price, so every other published term
    is identical by construction. One claims the price the crawl recorded; the other claims
    half of it. The honest one must score higher, and the ordering must FLIP when the lie moves
    — which is what rules out the difference coming from anything else about the two stores.
    """
    _seed(neo4j_session)

    first = _ranked(_graph_app(neo4j_session, monkeypatch, liar="shop-liar"))
    assert set(first) == set(GRAPH_STORES), first
    honest, liar = first["shop-honest"], first["shop-liar"]

    assert honest["components"]["intent_match"] == pytest.approx(liar["components"]["intent_match"])
    assert honest["components"]["price_value"] == pytest.approx(liar["components"]["price_value"])
    assert honest["components"]["trust"] == pytest.approx(liar["components"]["trust"])
    assert honest["components"]["verified_claim_ratio"] > liar["components"]["verified_claim_ratio"]
    assert honest["rank_score"] > liar["rank_score"], first
    assert all(math.isfinite(row["rank_score"]) for row in first.values())

    second = _ranked(_graph_app(neo4j_session, monkeypatch, liar="shop-honest"))
    assert second["shop-liar"]["rank_score"] > second["shop-honest"]["rank_score"], second
    assert second["shop-liar"]["rank_score"] == pytest.approx(honest["rank_score"])
    assert second["shop-honest"]["rank_score"] == pytest.approx(liar["rank_score"])


@pytest.mark.docker
@pytest.mark.graph
def test_the_crawl_alone_decides_and_an_operator_document_is_not_consulted(
    neo4j_session: Any, monkeypatch: Any
) -> None:
    """The catalogue really is the graph's, per (store, product), and it fails closed off it.

    Read directly through the seam ``ranking/serving.py`` uses, against the real graph: a
    rostered pair answers with the crawled price, a store the crawl never saw answers ``None``,
    and a product this store was never observed carrying answers ``None`` — the second and
    third being the fail-closed direction that makes the first trustworthy.
    """
    from exchange.retrieval.catalogue import GraphCatalogSnapshots as Source

    _seed(neo4j_session)
    catalog = Source(lambda: _Held(neo4j_session))

    snapshot = catalog.snapshot_for("shop-honest", GRAPH_PRODUCT)
    assert snapshot is not None
    (product,) = snapshot["products"]
    assert product["offer"]["price"] == pytest.approx(GRAPH_PRICE)
    assert product["attributes"]["silymarin_mg"]["value"] == pytest.approx(175.0)
    assert product["attributes"]["silymarin_mg"]["unit"] == "mg"
    assert _verdicts(snapshot, _claim("price", GRAPH_PRICE), product_ref=GRAPH_PRODUCT) == [
        "verified"
    ]
    assert _verdicts(snapshot, _claim("price", GRAPH_PRICE / 2), product_ref=GRAPH_PRODUCT) == [
        "contradicted"
    ]
    assert _verdicts(snapshot, _claim("silymarin_mg", 175), product_ref=GRAPH_PRODUCT) == [
        "verified"
    ]

    assert catalog.snapshot_for("shop-nobody-crawled", GRAPH_PRODUCT) is None
    assert catalog.snapshot_for("shop-honest", "prod-never-crawled") is None
